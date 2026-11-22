"""SimRa ride archives -> seconds.parquet + rides.parquet.

Input: data/simra_raw/<REGION>_*.zip (symlink, read only), files `<REGION>/Rides/<yyyy>/<mm>/VM2_<id>[#n]`. A ride file:
a version line (`<app version>#<file version>`, iOS versions start with 'i'), the incidents CSV, a line of '=' (25 from
Android, 19 from iOS), a version line, then the ride CSV `lat,lon,X,Y,Z,timeStamp,acc,...`: accelerometer rows (X, Y, Z
in m/s², with gravity, smoothed by the app) every ~250 ms, every ~3 s a row with a GPS fix (`acc` = accuracy in m),
`timeStamp` in ms.

Rides from the iOS app (~10 % of the files) are rejected: their accelerometer rows (every ~100 ms, unsmoothed) give
roughness values on another scale that agree poorly with Android rides on the same streets, and adding them makes
the estimates agree less with held-out Android rides.

Per ride:
1. GPS fixes interpolated to the middle of each second (NaN where the fixes around it are > MAX_FIX_GAP_S apart);
   speed and heading from the positions HALF_S before and after.
2. Gravity = the rolling median (GRAVITY_S) of the per-second mean acceleration; vertical = acceleration projected on
   it, minus the ride's median; `swell` = RMS of the vertical over SWELL_S (SimRa's own roughness value).
3. Phone not on the bike (handled, in a pocket that swings, re-mounted): a second whose mean direction is more than
   HANDLED_DEG from the gravity estimate, or where the gravity estimate turns by more than TURN_DEG within
   GRAVITY_S, is not used, with HANDLED_PAD_S around it.
4. Kept seconds: usable (3.), inside the region's box (BBOX), MIN_SPEED < speed <= MAX_SPEED, finite swell > 0,
   h_acc <= MAX_H_ACC.
5. value = log(swell / speed^SPEED_EXP); norm = value - median(value over the ride's kept seconds). SPEED_EXP: on
   the same link and ride, vibration grows with about the square root of speed (calibrate.py fits it).
6. A ride with fewer than MIN_KEPT kept seconds is skipped (with the reason in rides.parquet).

Duplicates: a ride whose ride CSV equals one seen before (SimRa publishes some rides twice, as '#2', and archives
overlap) is skipped. The month comes from the archive folder (`Rides/<yyyy>/<mm>`), not the phone clock.

    venv/bin/python pipeline/simra.py [--workers N] [--limit N]

The numbers (MIN_SPEED, ...) are in config.py.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import zipfile
from multiprocessing import get_context
from pathlib import Path

import numpy as np
import pandas as pd

from common import RIDES, SECONDS, SIMRA_RAW, Log, xy
from config import (BBOX, REGION, GRAVITY_S, HALF_S, HANDLED_DEG, HANDLED_PAD_S, IMU_HZ, MAX_FIX_GAP_S, MAX_H_ACC,
                    MAX_SPAN_S, MAX_SPEED, MIN_KEPT, MIN_MOVE_M, MIN_SPEED, SPEED_EXP, SWELL_S, TURN_DEG, WORKERS)

SEP = re.compile(r"^=+[ \t\r]*$", re.M)   # the line between incidents and ride data
COLS = ["lat", "lon", "X", "Y", "Z", "timeStamp", "acc"]


def members() -> list[tuple[str, str]]:
    """(archive, member) of every ride file, archives in name order"""
    out = []
    for z in sorted(SIMRA_RAW.glob(f"{REGION}_*.zip")):
        with zipfile.ZipFile(z) as f:
            out += [(str(z), i.filename) for i in f.infolist() if "/Rides/" in i.filename and not i.is_dir()]
    return out


def parse(text: str) -> pd.DataFrame:
    """ride file text -> ride rows (lat, lon, X, Y, Z, timeStamp, acc)"""
    parts = SEP.split(text, maxsplit=1)
    body = parts[1].lstrip().partition("\n")[2] if len(parts) == 2 else ""   # after the separator and the version line
    if not body.strip():                                           # no separator or no rows: no ride data
        return pd.DataFrame(columns=COLS, dtype=float)
    return pd.read_csv(io.StringIO(body), usecols=COLS, dtype=float)


def gps_per_second(fix: pd.DataFrame, sec: np.ndarray) -> pd.DataFrame:
    """fixes (t, lat, lon, acc) -> lat, lon, h_acc, speed, heading at the middle of each second"""
    t = fix.t.to_numpy()
    mid = sec + 0.5
    i = np.searchsorted(t, mid)
    ic = np.clip(i, 1, len(t) - 1)
    covered = (i > 0) & (i < len(t)) & (t[ic] - t[ic - 1] <= MAX_FIX_GAP_S)
    lat, lon = fix.lat.to_numpy(float), fix.lon.to_numpy(float)
    out = {c: np.where(covered, np.interp(mid, t, v), np.nan)
           for c, v in (("lat", lat), ("lon", lon), ("h_acc", fix.acc.to_numpy(float)))}
    xa, ya = xy(np.interp(mid - HALF_S, t, lat), np.interp(mid - HALF_S, t, lon))
    xb, yb = xy(np.interp(mid + HALF_S, t, lat), np.interp(mid + HALF_S, t, lon))
    dx, dy = xb - xa, yb - ya
    d = np.hypot(dx, dy)
    ok = covered & (mid - HALF_S >= t[0]) & (mid + HALF_S <= t[-1])
    out["speed"] = np.where(ok, d / (2 * HALF_S), np.nan)
    moved = d > MIN_MOVE_M
    out["heading"] = np.where(ok & moved, np.degrees(np.arctan2(dx, dy)) % 360, np.nan)
    return pd.DataFrame(out, index=sec)


def _angle(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    na, nb = np.linalg.norm(a, axis=1), np.linalg.norm(b, axis=1)
    c = np.einsum("ij,ij->i", a, b) / (na * nb)
    return np.degrees(np.arccos(np.clip(c, -1, 1)))


def vertical(imu: pd.DataFrame) -> pd.DataFrame:
    """IMU rows (t, X, Y, Z) -> per second: swell (RMS of the vertical over SWELL_S), handled (phone not on the bike)"""
    s = np.floor(imu.t.to_numpy()).astype(np.int64)
    a = imu[["X", "Y", "Z"]].to_numpy(float)
    per = pd.DataFrame(a, columns=["X", "Y", "Z"]).groupby(s).mean()
    full = per.reindex(np.arange(per.index.min(), per.index.max() + 1))       # one row per second, gaps NaN
    g = full.rolling(GRAVITY_S, center=True, min_periods=GRAVITY_S // 3).median()
    gn = g.to_numpy() / np.linalg.norm(g.to_numpy(), axis=1, keepdims=True)
    # projection of each row on its second's gravity direction
    row_g = gn[s - full.index[0]]
    up = np.einsum("ij,ij->i", a, row_g)
    up = up - np.nanmedian(up)
    sq = pd.Series(up ** 2).groupby(s).agg(["sum", "count"]).reindex(full.index, fill_value=0)
    roll = sq.rolling(SWELL_S, center=True, min_periods=1).sum()
    swell = np.sqrt(roll["sum"] / roll["count"].replace(0, np.nan))
    # handling: the second's direction far from gravity, or gravity turning
    dev = _angle(full.to_numpy(), g.to_numpy())
    h = GRAVITY_S // 2
    gv = g.to_numpy()
    turn = np.full(len(gv), np.nan)
    if len(gv) > 2 * h:
        turn[h:-h] = _angle(gv[: len(gv) - 2 * h], gv[2 * h:])
    bad = (dev > HANDLED_DEG) | (turn > TURN_DEG) | ~np.isfinite(dev)
    bad = pd.Series(bad.astype(float)).rolling(2 * HANDLED_PAD_S + 1, center=True, min_periods=1).max() > 0
    return pd.DataFrame({"swell": swell.to_numpy(), "handled": bad.to_numpy()}, index=full.index)


def process(job: tuple[str, str]) -> tuple[dict, pd.DataFrame | None]:
    """one ride file -> (status row, kept seconds or None); a ride that fails is rejected with the error"""
    try:
        return process_one(*job)
    except Exception as e:
        return {**new_row(*job), "status": "rejected", "reason": f"error: {type(e).__name__}"}, None


def new_row(archive: str, name: str) -> dict:
    """the ride's status row; the month comes from the folder Rides/<yyyy>/<mm>"""
    parts = name.split("/")
    return {"archive": Path(archive).name, "member": name, "simra_id": parts[-1], "month": f"{parts[2]}-{parts[3]}",
            "app": "", "hash": "", "seconds": 0, "handled": 0, "kept": 0, "status": "", "reason": ""}


def process_one(archive: str, name: str) -> tuple[dict, pd.DataFrame | None]:
    row = new_row(archive, name)
    text = _zip(archive).read(name).decode("utf-8", errors="replace")
    row["app"] = text.partition("#")[0].strip()                     # app version, iOS ones start with "i"
    row["hash"] = hashlib.sha256(SEP.split(text, 1)[-1].encode()).hexdigest()
    if row["app"].startswith("i"):
        return {**row, "status": "rejected", "reason": "iOS app: its accelerometer values don't match Android's"}, None
    df = parse(text)
    df = df[np.isfinite(df.timeStamp)]
    if df.empty:
        return {**row, "status": "rejected", "reason": "no ride data"}, None
    t0 = df.timeStamp.min()
    df = df.assign(t=(df.timeStamp - t0) / 1000.0)
    if df.t.max() > MAX_SPAN_S:
        return {**row, "status": "rejected", "reason": f"clock jumps (longer than {MAX_SPAN_S / 3600:g} h)"}, None
    fix = df[df.lat.notna() & df.lon.notna() & (df.lat != 0)].drop_duplicates("timeStamp").sort_values("t")
    imu = df[df.X.notna() & df.Y.notna() & df.Z.notna()].sort_values("t")
    if len(fix) < 2 or len(imu) < IMU_HZ * MIN_KEPT:
        return {**row, "status": "rejected", "reason": "no GPS or no IMU"}, None
    v = vertical(imu)
    sec = v.join(gps_per_second(fix, v.index.to_numpy(float)))
    row["seconds"], row["handled"] = len(sec), int(sec.handled.sum())
    lat_lo, lat_hi, lon_lo, lon_hi = BBOX
    keep = (~sec.handled & sec.lat.between(lat_lo, lat_hi) & sec.lon.between(lon_lo, lon_hi)
            & np.isfinite(sec.swell) & (sec.swell > 0)
            & (sec.speed > MIN_SPEED) & (sec.speed <= MAX_SPEED) & (sec.h_acc <= MAX_H_ACC))
    sec = sec[keep]
    row["kept"] = len(sec)
    if len(sec) < MIN_KEPT:
        return {**row, "status": "skipped", "reason": f"{len(sec)} kept seconds < {MIN_KEPT}"}, None
    value = np.log(sec.swell) - SPEED_EXP * np.log(sec.speed)
    out = pd.DataFrame({"t": sec.index.astype(np.int32),               # float32 positions: < 0.5 m steps
                        "lat": sec.lat.astype(np.float32), "lon": sec.lon.astype(np.float32),
                        "heading": sec.heading.astype(np.float32), "speed": sec.speed.astype(np.float32),
                        "h_acc": sec.h_acc.astype(np.float32), "swell": sec.swell.astype(np.float32),
                        "value": value.astype(np.float32), "norm": (value - value.median()).astype(np.float32)})
    return {**row, "status": "ok"}, out.reset_index(drop=True)


_ZIPS: dict[str, zipfile.ZipFile] = {}


def _zip(path: str) -> zipfile.ZipFile:
    if path not in _ZIPS:
        _ZIPS[path] = zipfile.ZipFile(path)
    return _ZIPS[path]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--limit", type=int, default=0, help="only the first N ride files (a test)")
    a = ap.parse_args(argv)
    log = Log()
    jobs = members()
    if a.limit:
        jobs = jobs[:: max(1, len(jobs) // a.limit)][: a.limit]
    log(f"{len(jobs):,} ride files")
    rows, frames, seen, keys = [], [], set(), set()
    with get_context("fork").Pool(a.workers) as pool:
        for k, (row, sec) in enumerate(pool.imap(process, jobs, chunksize=8)):
            key = row["simra_id"].replace("#", "_")
            if row["hash"] and row["hash"] in seen:
                row["status"], row["reason"], sec = "duplicate", "same ride CSV as an earlier file", None
            seen.add(row["hash"])
            if key in keys:                                  # the same id with other content: a second recording
                key = f"{key}_{Path(row['archive']).stem}"
            keys.add(key)
            row["ride_key"] = key
            rows.append(row)
            if sec is not None:
                frames.append(sec.assign(ride_key=key, month=row["month"]))
            if (k + 1) % 1000 == 0:
                log(f"  {k + 1:,} / {len(jobs):,} files, {len(frames):,} rides ok")
    rides = pd.DataFrame(rows)
    seconds = pd.concat(frames, ignore_index=True)
    cols = ["ride_key", "month", "t", "lat", "lon", "heading", "speed", "h_acc", "swell", "value", "norm"]
    seconds = seconds[cols].astype({"ride_key": "category", "month": "category"})
    for df, path in ((seconds, SECONDS), (rides, RIDES)):
        tmp = path.with_suffix(".part")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    log(f"rides: {rides.status.value_counts().to_dict()}")
    reasons = rides[rides.status != "ok"].reason.str.replace(r"^\d+ kept", "N kept", regex=True)
    log("skipped / rejected reasons:\n" + reasons.value_counts().head(10).to_string())
    ok = rides[rides.status == "ok"]
    log(f"seconds: {rides.seconds.sum():,} in all, handled {rides.handled.sum():,}, kept {len(seconds):,} "
        f"({ok.kept.median():.0f} per ok ride, median)")
    log(f"-> {SECONDS}, {RIDES}")


if __name__ == "__main__":
    main()
