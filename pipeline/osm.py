"""OSM PBF -> links.parquet.

1. Ways bikes may ride (RIDING highway types, not access=no / private unless bikes are allowed) with any node in
   the region's box (BBOX; the way is kept whole). Left out: parking aisles and driveways (they run beside roads and would
   take the road's riders) and sidewalks / crossings that don't allow bikes. Separately mapped cycle tracks stay
   their own links, so a road isn't measured by its cycle track.
2. The ways are split at every node shared with another kept way (a junction) -> links.
3. Per link: the OSM class `<surface>/<smoothness>` (the raw tag values, '-' when missing; `surface:bicycle` /
   `smoothness:bicycle` instead where tagged: a road with its own strip for bikes), the riding direction
   bikes may use (`oneway`), and two hints for match.py, which makes such links dearer: `path_beside` (the road
   says its bikes ride on a separate path beside it: `bicycle=use_sidepath|optional_sidepath`,
   `cycleway[:side]=separate`) and `foot_only` (a footway / pedestrian street without
   `bicycle=yes|designated|permissive`).

    venv/bin/python pipeline/osm.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import osmium
import pandas as pd

from common import LINKS, OSM_PBF, Log, xy
from config import BBOX

RIDING = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
          "residential", "living_street", "service", "pedestrian", "track", "cycleway", "path", "footway",
          "bridleway"}
KEEP_TAGS = ("highway", "surface", "smoothness", "surface:bicycle", "smoothness:bicycle", "oneway", "oneway:bicycle", "name", "footway",
             "bicycle", "junction", "cycleway", "cycleway:right", "cycleway:left", "cycleway:both")
MINOR_SERVICE = {"parking_aisle", "driveway", "drive-through", "emergency_access"}   # left out: beside roads
BIKE_OK = {"yes", "designated", "permissive"}
YES = {"yes", "1", "true"}


def rideable(t: dict) -> bool:
    if t.get("highway") not in RIDING or t.get("area") == "yes" or t.get("bicycle") == "no" \
            or t.get("service") in MINOR_SERVICE \
            or (t.get("footway") in ("sidewalk", "crossing") and t.get("bicycle") not in BIKE_OK):
        return False
    return t.get("access") not in ("no", "private") or t.get("bicycle") in BIKE_OK


def bike_oneway(t: dict) -> int:
    """+1: bikes only in the drawn direction, -1: only against it, 0: both"""
    ob = t.get("oneway:bicycle")
    if ob == "no" or str(t.get("cycleway", "")).startswith("opposite"):
        return 0
    if ob in YES:
        return 1
    if ob == "-1":
        return -1
    o = t.get("oneway")
    if o in YES:
        return 1
    if o == "-1":
        return -1
    if o is None and t.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


def foot_only(t: dict) -> bool:
    """a footway or pedestrian street that doesn't invite bikes"""
    return t.get("highway") in ("footway", "pedestrian") and t.get("bicycle") not in BIKE_OK


def path_beside(t: dict) -> bool:
    """the road says its bikes ride on a separately mapped path beside it"""
    return t.get("bicycle") in ("use_sidepath", "optional_sidepath") or "separate" in (
        t.get("cycleway"), t.get("cycleway:right"), t.get("cycleway:left"), t.get("cycleway:both"))


# ---- 1. read ---------------------------------------------------------------------------------------------------------

def read_ways(pbf: Path, bbox=BBOX, log=print) -> list[dict]:
    lat_lo, lat_hi, lon_lo, lon_hi = bbox
    fp = osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY).with_locations() \
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY)).with_filter(osmium.filter.KeyFilter("highway"))
    ways, n_seen, n_cut = [], 0, 0
    for w in fp:
        t = {tg.k: tg.v for tg in w.tags}
        if not rideable(t):
            continue
        n_seen += 1
        try:
            lat = np.array([n.location.lat for n in w.nodes])
            lon = np.array([n.location.lon for n in w.nodes])
        except osmium.InvalidLocationError:              # a node outside the extract
            n_cut += 1
            continue
        if len(lat) < 2 or not np.any((lat >= lat_lo) & (lat <= lat_hi) & (lon >= lon_lo) & (lon <= lon_hi)):
            continue
        keep = {k: v for k, v in t.items() if k in KEEP_TAGS}
        ways.append({"way_id": w.id, "tags": keep, "refs": np.array([n.ref for n in w.nodes], dtype=np.int64),
                     "lat": lat, "lon": lon})
        if len(ways) % 50_000 == 0:
            log(f"  {len(ways):,} ways kept")
    log(f"read: {n_seen:,} rideable highway ways, {len(ways):,} in the box, {n_cut} cut at the extract's edge")
    return ways


def bike_surface(t: dict) -> tuple:
    """(surface, smoothness) bikes ride on: the :bicycle tags where a surface:bicycle is given, else the way's"""
    if t.get("surface:bicycle"):
        return t["surface:bicycle"], t.get("smoothness:bicycle")
    return t.get("surface"), t.get("smoothness")


# ---- 2. links --------------------------------------------------------------------------------------------------------

def split_links(ways: list[dict]) -> pd.DataFrame:
    """split at every node used more than once among the kept ways (a junction, or a loop closing on itself)"""
    allrefs = np.concatenate([w["refs"] for w in ways])
    uniq, cnt = np.unique(allrefs, return_counts=True)
    rows = []
    for w in ways:
        refs = w["refs"]
        c = cnt[np.searchsorted(uniq, refs)]
        cuts = np.r_[0, 1 + np.flatnonzero(c[1:-1] >= 2), len(refs) - 1]
        x, y = xy(w["lat"], w["lon"])
        seg = np.r_[0, np.cumsum(np.hypot(np.diff(x), np.diff(y)))]
        t = w["tags"]
        surface, smoothness = bike_surface(t)
        for a, b in zip(cuts[:-1], cuts[1:]):
            if b <= a:
                continue
            rows.append({"way_id": w["way_id"], "u": refs[a], "v": refs[b],
                         "lat": w["lat"][a:b + 1], "lon": w["lon"][a:b + 1], "length": seg[b] - seg[a],
                         "highway": t.get("highway"), "name": t.get("name"), "surface": surface,
                         "smoothness": smoothness,
                         "oneway": bike_oneway(t), "path_beside": path_beside(t), "foot_only": foot_only(t)})
    df = pd.DataFrame(rows)
    df = df[df.length > 0].reset_index(drop=True)
    df["link_id"] = np.arange(len(df), dtype=np.int64)                # = the row
    df["osm_class"] = df.surface.fillna("-") + "/" + df.smoothness.fillna("-")
    cols = ["link_id", "way_id", "u", "v", "lat", "lon", "length", "highway", "name", "surface", "smoothness",
            "osm_class", "oneway", "path_beside", "foot_only"]
    return df[cols]


def main():
    log = Log()
    links = split_links(read_ways(OSM_PBF, log=log))
    links.to_parquet(LINKS, index=False)
    log(f"{len(links):,} links, {links.length.sum() / 1000:,.0f} km -> {LINKS}; with a path beside: "
        f"{links.path_beside.sum():,} ({links.length[links.path_beside].sum() / 1000:,.0f} km); foot only: "
        f"{links.foot_only.sum():,} ({links.length[links.foot_only].sum() / 1000:,.0f} km)")
    log("highway:\n" + links.groupby("highway").length.agg(["size", "sum"]).sort_values("sum", ascending=False)
        .to_string())
    log("osm_class (km):\n" + (links.groupby("osm_class").length.sum() / 1000).round(0).sort_values(ascending=False)
        .head(20).to_string())


if __name__ == "__main__":
    main()
