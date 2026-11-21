"""links.parquet + links_est.parquet -> web/data/roughness.pmtiles.

One feature per link and direction a bike may ride; for direction -1 the geometry is reversed, so every feature runs
in the direction of travel: `line-offset` in web/map.js puts each direction on its own side of the street, and the
one-way arrows follow it. Links without passes on minor ways (footway, service, track, path, ...) appear from zoom
MINOR_ZOOM only, which keeps the low zooms light. GeoJSON lines -> tippecanoe -> one PMTiles file.

    venv/bin/python pipeline/tiles.py
"""
from __future__ import annotations

import json
import subprocess

import numpy as np
import pandas as pd

from common import DATA, LINKS, LINKS_EST, PMTILES, Log
from config import MAX_ZOOM, MIN_ZOOM, MINOR_ZOOM

GEOJSON = DATA / "links.geojsonl"
MAJOR = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
         "residential", "living_street", "cycleway"}
NUM = ["est", "se", "information", "prior", "tau", "comb", "comb_se", "z"]


def features(links: pd.DataFrame, est: pd.DataFrame):
    df = est.merge(links, on="link_id", how="left", suffixes=("", "_l"))
    df[NUM] = df[NUM].round(2)
    for r in df.itertuples(index=False):
        lat, lon = (r.lat, r.lon) if r.direction == 1 else (r.lat[::-1], r.lon[::-1])
        props = {"way": r.way_id, "dir": r.direction, "passes": r.passes,
                 "hw": r.highway, "name": r.name, "surface": r.surface, "smoothness": r.smoothness,
                 "oneway": 1 if r.oneway else None,
                 "bysurf": 1 if r.by_surface else None}
        props.update({k: getattr(r, k) for k in NUM})
        props = {k: v for k, v in props.items() if v is not None and not (isinstance(v, float) and np.isnan(v))}
        f = {"type": "Feature", "properties": props,
             "geometry": {"type": "LineString", "coordinates": np.c_[lon, lat].round(6).tolist()}}
        if r.passes == 0 and r.highway not in MAJOR:
            f["tippecanoe"] = {"minzoom": MINOR_ZOOM}
        yield f


def main():
    log = Log()
    links = pd.read_parquet(LINKS, columns=["link_id", "way_id", "lat", "lon", "highway", "name", "surface",
                                           "smoothness", "oneway"])
    est = pd.read_parquet(LINKS_EST)
    with open(GEOJSON, "w") as fh:
        for n, f in enumerate(features(links, est)):
            fh.write(json.dumps(f, ensure_ascii=False, default=int) + "\n")
    log(f"{n + 1:,} features -> {GEOJSON}")
    PMTILES.parent.mkdir(exist_ok=True)
    subprocess.run(["tippecanoe", "-o", str(PMTILES), "--force", "-l", "links",
                    "-Z", str(MIN_ZOOM), "-z", str(MAX_ZOOM), "--drop-densest-as-needed",
                    "--extend-zooms-if-still-dropping", "-P", "--quiet", str(GEOJSON)],
                   check=True)
    GEOJSON.unlink()
    log(f"-> {PMTILES} ({PMTILES.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
