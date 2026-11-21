"""Shared paths and helpers."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from config import LAT0

ROOT = Path(__file__).resolve().parents[1]     # the repository
DATA = ROOT / "data"

OSM_PBF = DATA / "osm.pbf"                       # symlink, read only
SIMRA_RAW = DATA / "simra_raw"                   # symlink to the region's SimRa archives, read only

SECONDS = DATA / "seconds.parquet"
LINKS = DATA / "links.parquet"
PASSES = DATA / "passes.parquet"
LINKS_EST = DATA / "links_est.parquet"
PMTILES = ROOT / "web/data/roughness.pmtiles"   # next to the map page that shows it


M_LAT = 111_195.0                                # metres per degree of latitude (sphere)
M_LON = M_LAT * np.cos(np.radians(LAT0))


def xy(lat, lon):
    """degrees -> local metres (equirectangular around LAT0; < 0.1 % error within ~50 km of it)"""
    return np.asarray(lon, dtype=float) * M_LON, np.asarray(lat, dtype=float) * M_LAT


class Log:
    """print with the seconds since start"""

    def __init__(self):
        self.t0 = time.time()

    def __call__(self, *a):
        print(f"[{time.time() - self.t0:7.1f} s]", *a, flush=True)


def sample(lats: list, lons: list, step: float) -> dict:
    """points every ~step metres along each line (midpoints of an even split of each segment): x, y (metres), the
    unit direction ux, uy (the line's drawn direction), and `line`, the index of the line in the input"""
    xs, ys, us, vs, owner = [], [], [], [], []
    for i, (lat, lon) in enumerate(zip(lats, lons)):
        x, y = xy(lat, lon)
        dx, dy = np.diff(x), np.diff(y)
        d = np.hypot(dx, dy)
        ok = d > 0
        dx, dy, d, x0, y0 = dx[ok], dy[ok], d[ok], x[:-1][ok], y[:-1][ok]
        n = np.maximum(1, np.ceil(d / step).astype(int))       # points per segment
        seg = np.repeat(np.arange(len(d)), n)
        f = (np.arange(len(seg)) - np.repeat(np.cumsum(n) - n, n) + 0.5) / n[seg]   # position along the segment
        xs.append(x0[seg] + f * dx[seg]); ys.append(y0[seg] + f * dy[seg])
        us.append(dx[seg] / d[seg]); vs.append(dy[seg] / d[seg]); owner.append(np.full(len(seg), i))
    cat = lambda a: np.concatenate(a) if a else np.zeros(0)
    return {"x": cat(xs), "y": cat(ys), "ux": cat(us), "uy": cat(vs), "line": cat(owner).astype(np.int64)}
