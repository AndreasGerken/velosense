"""links.parquet + links_est.parquet -> web/data/roughness.pmtiles; seconds.parquet -> web/data/rides.pmtiles.

One feature per link and direction a bike may ride; for direction -1 the geometry is reversed, so every feature runs
in the direction of travel: `line-offset` in web/map.js puts each direction on its own side of the street, and the
one-way arrows follow it. Zoomed out, main roads and cycle tracks only; all measured links from MEASURED_ZOOM, all
links from MINOR_ZOOM. So the far zooms show whole streets instead of the pieces tippecanoe would keep of all of them.

The Rides view: what the estimates start from, every ride, one line per RIDE_LINE_PX screen pixels (at least one per
pair of consecutive seconds) with the mean `norm` of its seconds (their shaking relative to the ride's median). Where a
tile can't hold them all, tippecanoe leaves out lines where they are densest, so busy streets thin out and quiet ones
keep their rides. To keep the riders untraceable (SimRa's terms), RIDE_CUT_M are left out at both ends of each ride,
and the rest is cut into pieces of RIDE_PIECE_M of which every other one is left out; no times, no ride ids.

GeoJSON lines -> tippecanoe, one run per zoom -> tile-join -> one PMTiles file per layer. (One run per zoom: within a
run, the lines tippecanoe leaves out at a full zoom stay out at the closer ones, which breaks rides into dashes.
tippecanoe's per-feature minzoom would be simpler, but it loses lines that touch other lines.)

    venv/bin/python pipeline/tiles.py
"""
from __future__ import annotations

import json
import subprocess
from itertools import groupby

import numpy as np
import pandas as pd

from common import DATA, LINKS, LINKS_EST, PMTILES, RIDES, RIDES_PMTILES, SECONDS, Log, xy
from config import (LAT0, MAX_ZOOM, MEASURED_ZOOM, MIN_ZOOM, MINOR_ZOOM, RIDE_BATCH, RIDE_CUT_M, RIDE_LINE_PX,
                    RIDE_MAX_ZOOM, RIDE_PIECE_M, RIDE_SPEED, TILE_MAX_KB, WORLD_M)

MAIN = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link", "cycleway"}
NUM = ["est", "se", "information", "prior", "tau", "comb", "comb_se", "z"]


def features(df: pd.DataFrame):
    """link directions (estimates merged with their links) -> GeoJSON features"""
    for r in df.itertuples(index=False):
        lat, lon = (r.lat, r.lon) if r.direction == 1 else (r.lat[::-1], r.lon[::-1])
        props = {"way": r.way_id, "dir": r.direction, "passes": r.passes,
                 "hw": r.highway, "name": r.name, "surface": r.surface, "smoothness": r.smoothness,
                 "oneway": 1 if r.oneway else None,
                 "bysurf": 1 if r.by_surface else None}
        props.update({k: getattr(r, k) for k in NUM})
        props = {k: v for k, v in props.items() if v is not None and not (isinstance(v, float) and np.isnan(v))}
        yield {"type": "Feature", "properties": props,
               "geometry": {"type": "LineString", "coordinates": np.c_[lon, lat].round(6).tolist()}}


def shown(t, lat, lon) -> np.ndarray:
    """per ride second (sorted by t): drawn? Not within RIDE_CUT_M of either end, and in an even RIDE_PIECE_M piece"""
    x, y = xy(lat, lon)
    d = np.r_[0, np.cumsum(np.hypot(np.diff(x), np.diff(y)))]           # metres along the ride
    inner = (d >= RIDE_CUT_M) & (d <= d[-1] - RIDE_CUT_M)
    return inner & ((d - RIDE_CUT_M) // RIDE_PIECE_M % 2 == 0)


def ride_features(sec: pd.DataFrame, step: int = 1):
    """one line per `step` consecutive drawn ride seconds, with their mean `norm`"""
    for _, g in sec.groupby("ride_key", sort=False, observed=True):
        g = g.sort_values("t")
        t, lat, lon, norm = (g[c].to_numpy() for c in ("t", "lat", "lon", "norm"))
        keep = shown(t, lat, lon)
        pairs = np.flatnonzero(keep[:-1] & keep[1:] & (np.diff(t) == 1))    # second i -> i + 1, both drawn
        for run in np.split(pairs, np.flatnonzero(np.diff(pairs) > 1) + 1):  # unbroken runs of pairs
            for k in range(0, len(run), step):
                i, j = run[k], run[min(k + step, len(run)) - 1] + 1           # seconds i to j
                yield {"type": "Feature", "properties": {"n": round(float(norm[i + 1:j + 1].mean()), 2)},
                       "geometry": {"type": "LineString",
                                    "coordinates": np.c_[lon[i:j + 1], lat[i:j + 1]].round(6).tolist()}}


def ride_step(zoom: int) -> int:
    """seconds per ride line at this zoom: RIDE_LINE_PX screen pixels at the median speed, at least 1"""
    m_per_px = WORLD_M * np.cos(np.radians(LAT0)) / (512 * 2 ** zoom)
    return max(1, round(RIDE_LINE_PX * m_per_px / RIDE_SPEED))


def rides_features(keys: list, step: int = 1):
    """the rides' features, read RIDE_BATCH rides at a time"""
    for i in range(0, len(keys), RIDE_BATCH):
        yield from ride_features(pd.read_parquet(SECONDS, columns=["ride_key", "t", "lat", "lon", "norm"],
                                                 filters=[("ride_key", "in", keys[i:i + RIDE_BATCH])]), step)


def tiles(groups: dict, layer: str, out, log):
    """{(first zoom, last zoom): GeoJSON features} -> one PMTiles file: tippecanoe per zoom, then tile-join"""
    out.parent.mkdir(exist_ok=True)
    parts = []
    for (first, last), feats in groups.items():
        geojson = DATA / f"{layer}_{first}.geojsonl"
        with open(geojson, "w") as fh:
            n = 0
            for n, f in enumerate(feats, 1):
                fh.write(json.dumps(f, ensure_ascii=False, default=int) + "\n")
        log(f"{n:,} features, zoom {first} to {last}")
        for zoom in range(first, last + 1):
            part = DATA / f"{layer}_{first}_z{zoom}.pmtiles"
            subprocess.run(["tippecanoe", "-o", str(part), "--force", "-l", layer, "-Z", str(zoom), "-z", str(zoom),
                            "-M", str(TILE_MAX_KB * 1000), "--drop-densest-as-needed", "-P", "--quiet", str(geojson)],
                           check=True)
            parts.append(part)
        geojson.unlink()
    # -pk: no tile size limit here, tippecanoe already kept each part's tiles small enough. tile-join warns that the
    # parts end at different zooms: harmless, each zoom's tiles come from the part that has it
    subprocess.run(["tile-join", "-o", str(out), "--force", "-pk", "--quiet", *map(str, parts)], check=True)
    for part in parts:
        part.unlink()
    log(f"-> {out} ({out.stat().st_size / 1e6:.0f} MB)")


def main():
    log = Log()
    links = pd.read_parquet(LINKS, columns=["link_id", "way_id", "lat", "lon", "highway", "name", "surface",
                                           "smoothness", "oneway"])
    df = pd.read_parquet(LINKS_EST).merge(links, on="link_id", how="left", suffixes=("", "_l"))
    df[NUM] = df[NUM].round(2)
    main = df.highway.isin(MAIN)
    tiles({(MIN_ZOOM, MEASURED_ZOOM - 1): features(df[main]),
           (MEASURED_ZOOM, MINOR_ZOOM - 1): features(df[main | (df.passes > 0)]),
           (MINOR_ZOOM, MAX_ZOOM): features(df)}, "links", PMTILES, log)

    rides = pd.read_parquet(RIDES, columns=["ride_key", "status"])
    keys = rides.ride_key[rides.status == "ok"].tolist()
    log(f"{len(keys):,} rides")
    groups = [(step, list(zooms)) for step, zooms in groupby(range(MIN_ZOOM, RIDE_MAX_ZOOM + 1), ride_step)]
    log("seconds per ride line: " + ", ".join(f"{s} at zoom {z[0]}-{z[-1]}" for s, z in groups))
    tiles({(z[0], z[-1]): rides_features(keys, s) for s, z in groups}, "rides", RIDES_PMTILES, log)


if __name__ == "__main__":
    main()
