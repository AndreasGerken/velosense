"""links.parquet + links_est.parquet -> web/data/roughness.pmtiles; seconds.parquet -> web/data/rides.pmtiles.

One feature per link and direction a bike may ride; for direction -1 the geometry is reversed, so every feature runs
in the direction of travel: `line-offset` in web/map.js puts each direction on its own side of the street, and the
one-way arrows follow it. Links without passes on minor ways (footway, service, track, path, ...) appear from zoom
MINOR_ZOOM only, which keeps the low zooms light. GeoJSON lines -> tippecanoe, one run per first zoom (tippecanoe's
per-feature minzoom loses lines that touch other lines) -> tile-join -> one PMTiles file.

The Rides view: what the estimates start from, every ride. From zoom RIDE_CLOSE_ZOOM on one line per pair of
consecutive ride seconds with the later second's `norm` (its shaking relative to the ride's median); below, one line per
RIDE_FAR_S seconds with their mean (finer is not seen there). Where a tile can't hold them all, tippecanoe leaves out
lines where they are densest, so busy streets thin out and quiet ones keep their rides. To keep the riders untraceable (SimRa's terms), RIDE_CUT_M are left out at both ends of each ride, and the
rest is cut into pieces of RIDE_PIECE_M of which every other one is left out; no times, no ride ids.

    venv/bin/python pipeline/tiles.py
"""
from __future__ import annotations

import json
import subprocess

import numpy as np
import pandas as pd

from common import DATA, LINKS, LINKS_EST, PMTILES, RIDES, RIDES_PMTILES, SECONDS, Log, xy
from config import (MAX_ZOOM, MIN_ZOOM, MINOR_ZOOM, RIDE_BATCH, RIDE_CLOSE_ZOOM, RIDE_CUT_M, RIDE_FAR_S, RIDE_MAX_ZOOM,
                    RIDE_PIECE_M)

MAJOR = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
         "residential", "living_street", "cycleway"}
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
        if len(pairs) == 0:
            continue
        run = np.cumsum(np.r_[True, np.diff(pairs) > 1])                     # unbroken runs of pairs
        first = pairs[np.searchsorted(run, run)]                             # each run's first pair
        piece = run * len(t) + (pairs - first) // step
        for p in np.split(pairs, np.flatnonzero(np.diff(piece)) + 1):
            i, j = p[0], p[-1] + 1
            yield {"type": "Feature", "properties": {"n": round(float(norm[i + 1:j + 1].mean()), 2)},
                   "geometry": {"type": "LineString", "coordinates": np.c_[lon[i:j + 1], lat[i:j + 1]].round(6).tolist()}}


def rides_features(keys: list, step: int = 1):
    """the rides' features, read RIDE_BATCH rides at a time"""
    for i in range(0, len(keys), RIDE_BATCH):
        yield from ride_features(pd.read_parquet(SECONDS, columns=["ride_key", "t", "lat", "lon", "norm"],
                                                 filters=[("ride_key", "in", keys[i:i + RIDE_BATCH])]), step)


def tiles(groups: dict, layer: str, out, log):
    """{(first zoom, last zoom): GeoJSON features} -> one PMTiles file: tippecanoe per group, then tile-join"""
    out.parent.mkdir(exist_ok=True)
    parts = []
    for (zoom, last), feats in groups.items():
        geojson, part = DATA / f"{layer}_{zoom}.geojsonl", DATA / f"{layer}_{zoom}.pmtiles"
        with open(geojson, "w") as fh:
            n = 0
            for n, f in enumerate(feats, 1):
                fh.write(json.dumps(f, ensure_ascii=False, default=int) + "\n")
        log(f"{n:,} features, zoom {zoom} to {last}")
        subprocess.run(["tippecanoe", "-o", str(part), "--force", "-l", layer, "-Z", str(zoom), "-z", str(last),
                        "--drop-densest-as-needed", "-P", "--quiet", str(geojson)],
                       check=True)
        geojson.unlink()
        parts.append(part)
    # -pk: no tile size limit here, tippecanoe already kept each part's tiles small enough
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
    minor = (df.passes == 0) & ~df.highway.isin(MAJOR)
    tiles({(MIN_ZOOM, MAX_ZOOM): features(df[~minor]), (MINOR_ZOOM, MAX_ZOOM): features(df[minor])}, "links", PMTILES,
          log)

    rides = pd.read_parquet(RIDES, columns=["ride_key", "status"])
    keys = rides.ride_key[rides.status == "ok"].tolist()
    log(f"{len(keys):,} rides")
    tiles({(MIN_ZOOM, RIDE_CLOSE_ZOOM - 1): rides_features(keys, RIDE_FAR_S),
           (RIDE_CLOSE_ZOOM, RIDE_MAX_ZOOM): rides_features(keys)}, "rides", RIDES_PMTILES, log)


if __name__ == "__main__":
    main()
