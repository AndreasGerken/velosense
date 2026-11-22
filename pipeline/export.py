"""links.parquet + links_est.parquet -> the downloads (described in docs/data.md):

* web/data/roughness.csv.gz: one row per measured link direction (passes > 0): its OSM way and nodes in riding
  direction, tags, the values as "x as rough as good asphalt" with their ± (one standard error), and the line (WKT).
* web/data/tag_roughness.csv: per OSM surface/smoothness tag pair the value the map's OSM view gives a street with it
  (rows with smoothness "*": the surface alone, for rare pairs or no smoothness tag).

    venv/bin/python pipeline/export.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import LINKS, LINKS_EST, WEB_DATA, Log
from config import MIN_INFO

ROUGHNESS_CSV = WEB_DATA / "roughness.csv.gz"
TAGS_CSV = WEB_DATA / "tag_roughness.csv"


def ratio(v, se=None):
    """log values -> x as rough as good asphalt (and its ±, as in the map's popup)"""
    return np.exp(v).round(3) if se is None else (np.exp(v) * se).round(3)


def wkt(lat, lon, direction) -> str:
    pts = np.c_[lon, lat] if direction == 1 else np.c_[lon, lat][::-1]
    return "LINESTRING (" + ", ".join(f"{x:.6f} {y:.6f}" for x, y in pts) + ")"


def segments(links: pd.DataFrame, est: pd.DataFrame) -> pd.DataFrame:
    e = est[est.passes > 0].merge(links, on="link_id")
    fwd = e.direction == 1
    return pd.DataFrame({
        "way_id": e.way_id, "from_node": np.where(fwd, e.u, e.v), "to_node": np.where(fwd, e.v, e.u),
        "direction": np.where(fwd, "as drawn", "against drawn"),
        "name": e.name, "highway": e.highway, "surface": e.surface, "smoothness": e.smoothness,
        "length_m": e.length.round(1), "passes": e.passes, "information": e.information.round(2),
        "measured": ratio(e.est), "measured_pm": ratio(e.est, e.se),
        "osm": ratio(e.prior), "osm_pm": ratio(e.prior, e.tau),
        "combined": ratio(e.comb), "combined_pm": ratio(e.comb, e.comb_se), "z": e.z.round(2),
        "geometry": [wkt(la, lo, d) for la, lo, d in zip(e.lat, e.lon, e.direction)],
    }).sort_values(["way_id", "from_node"])


def tag_table(est: pd.DataFrame) -> pd.DataFrame:
    e = est[est.prior.notna()].copy()
    e["surface"] = e.osm_class.str.split("/").str[0]
    e["smoothness"] = np.where(e.by_surface, "*", e.osm_class.str.split("/").str[1])
    t = e.groupby(["surface", "smoothness"]).agg(osm=("prior", "first"), tau=("tau", "first"),
                                                  link_directions=("prior", "size"))
    taught = est[est.information >= MIN_INFO].assign(surface=lambda d: d.osm_class.str.split("/").str[0],
                                                     smoothness=lambda d: d.osm_class.str.split("/").str[1])
    by_pair = taught.groupby(["surface", "smoothness"]).size()
    by_surface = taught.groupby("surface").size()
    t["measured_from"] = [by_surface[s] if sm == "*" else by_pair[(s, sm)] for s, sm in t.index]
    t["osm_pm"] = ratio(t.osm, t.tau)
    t["osm"] = ratio(t.osm)
    t = t.reset_index().replace({"surface": {"-": ""}, "smoothness": {"-": ""}})      # empty: the tag is missing
    return t.sort_values("osm")[["surface", "smoothness", "osm", "osm_pm", "measured_from", "link_directions"]]


def main():
    log = Log()
    links = pd.read_parquet(LINKS, columns=["link_id", "way_id", "u", "v", "lat", "lon", "length", "highway", "name",
                                           "surface", "smoothness"])
    est = pd.read_parquet(LINKS_EST)
    WEB_DATA.mkdir(exist_ok=True)
    seg = segments(links, est)
    seg.to_csv(ROUGHNESS_CSV, index=False)
    log(f"{len(seg):,} measured link directions -> {ROUGHNESS_CSV} ({ROUGHNESS_CSV.stat().st_size / 1e6:.0f} MB)")
    tags = tag_table(est)
    tags.to_csv(TAGS_CSV, index=False)
    log(f"{len(tags)} tag pairs -> {TAGS_CSV}")


if __name__ == "__main__":
    main()
