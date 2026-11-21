"""Small tests of the pipeline's logic on made-up data:  venv/bin/python -m pytest -q tests"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import estimate  # noqa: E402
import match  # noqa: E402
import osm  # noqa: E402
import simra  # noqa: E402


# --- simra.py

RIDE = ("100#1\nkey,lat,lon,ts,bike,childCheckBox,trailerCheckBox,pLoc,incident\n,,,,1,0,0,1,-5\n\n"
        f"{'=' * 25}\n100#1\nlat,lon,X,Y,Z,timeStamp,acc,a,b,c\n"
        "52.5,13.4,0.1,0.2,9.8,1000,5.0,0,0,0\n,,0.1,0.3,9.7,1250,,0,0,0\n")


def test_parse_reads_the_ride_rows():
    df = simra.parse(RIDE)
    assert list(df.columns) == ["lat", "lon", "X", "Y", "Z", "timeStamp", "acc"]
    assert len(df) == 2 and df.acc.iloc[0] == 5.0 and np.isnan(df.lat.iloc[1])


def test_parse_reads_ios_files():
    # iOS writes 19 '=' as the separator
    df = simra.parse(RIDE.replace("=" * 25, "=" * 19))
    assert len(df) == 2 and df.acc.iloc[0] == 5.0


def test_parse_without_ride_data_is_empty():
    df = simra.parse(RIDE.partition("=" * 25)[0])
    assert df.empty


# --- osm.py

def test_bike_oneway():
    assert osm.bike_oneway({"oneway": "yes"}) == 1
    assert osm.bike_oneway({"oneway": "-1"}) == -1
    assert osm.bike_oneway({"oneway": "yes", "oneway:bicycle": "no"}) == 0
    assert osm.bike_oneway({"junction": "roundabout"}) == 1
    assert osm.bike_oneway({"highway": "residential"}) == 0


def test_footway_tags():
    assert osm.foot_only({"highway": "footway"})
    assert not osm.foot_only({"highway": "footway", "bicycle": "yes"})
    assert osm.path_beside({"highway": "secondary", "cycleway:right": "separate"})


def test_bike_surface_tags_win():
    assert osm.bike_surface({"surface": "sett", "smoothness": "bad"}) == ("sett", "bad")
    t = {"surface": "sett", "smoothness": "bad", "surface:bicycle": "asphalt", "smoothness:bicycle": "excellent"}
    assert osm.bike_surface(t) == ("asphalt", "excellent")
    assert osm.bike_surface({"surface": "sett", "smoothness": "bad", "surface:bicycle": "asphalt"}) == ("asphalt", None)


def test_split_links_at_junctions():
    way = lambda i, refs, lon0: {"way_id": i, "refs": np.array(refs), "lat": np.full(3, 52.5),
                                 "lon": lon0 + np.arange(3) * 0.001, "tags": {"highway": "residential"}}
    links = osm.split_links([way(1, [1, 2, 3], 13.4), way(2, [4, 2, 5], 13.4)])
    assert len(links) == 4                                       # both ways cut at the shared node 2
    assert set(links.u) | set(links.v) == {1, 2, 3, 4, 5}
    assert (links.link_id == np.arange(4)).all()


# --- match.py: the route through the candidates

def route(seconds: list[dict], links: pd.DataFrame) -> np.ndarray:
    """seconds: one {state: cost} per second, rides given by a "ride" key -> the chosen state per second"""
    n = len(seconds)
    state, cost = np.full((n, match.C), -1, np.int32), np.full((n, match.C), np.inf, np.float32)
    for i, sec in enumerate(seconds):
        for k, (s, c) in enumerate((s, c) for s, c in sec.items() if s != "ride"):
            state[i, k], cost[i, k] = s, c
    ride = np.array([sec["ride"] for sec in seconds])
    return match.viterbi(state, cost, ride, np.arange(n), links)


# road: link 0 (node 1 -> 2), link 1 (2 -> 3); footway link 2 (10 -> 11) beside it, not connected;
# link 3 (2 -> 4, 10 m: a junction) and link 4 (4 -> 5) go on from the road's end; link 5 elsewhere
LINKS = pd.DataFrame({"u": [1, 2, 10, 2, 4, 20], "v": [2, 3, 11, 4, 5, 21],
                      "length": [100.0, 100.0, 100.0, 10.0, 100.0, 100.0]})
ROAD0, ROAD1, FOOT, ON, OTHER = 1, 3, 5, 9, 11                 # states: link x 2 + 1 (riding as drawn)


def test_route_ignores_a_short_hop_to_a_parallel_line():
    secs = [{"ride": 0, ROAD0: 0.1} for _ in range(4)] + [{"ride": 0, ROAD0: 1.5, FOOT: 0.2} for _ in range(2)] \
        + [{"ride": 0, ROAD1: 1.5, FOOT: 0.2} for _ in range(2)] + [{"ride": 0, ROAD1: 0.1} for _ in range(4)]
    assert route(secs, LINKS).tolist() == [ROAD0] * 6 + [ROAD1] * 6


def test_route_follows_a_long_move_and_rides_of_different_lengths():
    a = [{"ride": 0, ROAD0: 0.1} for _ in range(3)] + [{"ride": 0, ROAD0: 2.0, FOOT: 0.1} for _ in range(30)]
    b = [{"ride": 1, ROAD0: 0.1} for _ in range(5)]
    out = route(a + b, LINKS)
    assert out[:3].tolist() == [ROAD0] * 3 and (out[5:33] == FOOT).all() and (out[33:] == ROAD0).all()


def test_route_may_skip_a_short_junction_link():
    secs = [{"ride": 0, ROAD0: 0.1, OTHER: 0.15} for _ in range(6)] + [{"ride": 0, ON: 0.1, OTHER: 0.15}
                                                                     for _ in range(6)]
    assert route(secs, LINKS).tolist() == [ROAD0] * 6 + [ON] * 6


# --- estimate.py

def test_information_counts_passes_by_their_weight():
    p = pd.DataFrame({"link_id": [7, 7, 7], "direction": [1, 1, 1], "value": [0.0, 0.2, 0.4],
                      "w": [1.0, 0.5, 0.5]})
    e = estimate.weighted(p).iloc[0]
    assert e.passes == 3 and abs(e.information - 2) < 1e-9 and abs(e.est - 0.15) < 1e-9


def test_pass_weight_falls_with_age():
    month = pd.Series(["2025-01", "2024-01", "2022-01", "2020-01"], dtype="category")
    p = pd.DataFrame({"month": month, "metres": 20.0, "match_p": 1.0})
    w = estimate.pass_weights(p)
    assert w[0] == 1 and 0.98 < w[1] < 1 and abs(w[2] - 0.5) < 1e-9 and w[3] < 0.12


def test_pass_weight_grows_with_length():
    p = pd.DataFrame({"month": pd.Series(["2025-01"] * 3, dtype="category"), "metres": [5.0, 20.0, 100.0],
                      "match_p": [1.0, 1.0, 0.5]})
    w = estimate.pass_weights(p)
    assert w[0] < 1 and w[1] == 1 and abs(w[2] - 0.5 * 5 ** estimate.LENGTH_EXP) < 1e-9


def test_along_ride_averages_within_the_window_and_ride():
    # ride a: 10 m per second, so ±30 m takes 3 seconds on either side; ride b is apart, and so is a's last
    # second after a gap
    sec = pd.DataFrame({"ride_key": pd.Categorical(["a"] * 8 + ["b"]), "t": [0, 1, 2, 3, 4, 5, 6, 30, 0],
                        "speed": 10.0, "x": [0, 0, 0, 7, 0, 0, 0, 5, 9.0]})
    m = match.along_ride(sec, "x")
    assert np.allclose(m, [7 / 4, 7 / 5, 7 / 6, 1, 7 / 6, 7 / 5, 7 / 4, 5, 9])


def test_match_certainty():
    # second 0: road link 0 chosen, its continuation link 1 as close (same street: no doubt);
    # second 1: road link 0 chosen, the unconnected footway link 2 as close (a parallel line: 0.5)
    state = np.array([[1, 3, -1, -1, -1, -1], [1, 5, -1, -1, -1, -1]], np.int32)
    cost = np.array([[0.1, 0.1] + [np.inf] * 4, [0.1, 0.1] + [np.inf] * 4], np.float32)
    p = match.certainty(state, cost, np.array([1, 1]), LINKS)
    assert np.allclose(p, [1.0, 0.5])


def test_combine_without_prior_or_passes():
    e = pd.DataFrame({"est": [0.5, np.nan, 0.5], "se": [0.1, np.nan, 0.1], "prior": [np.nan, 0.2, 0.0],
                      "tau": [np.nan, 0.2, 0.1]})
    c = estimate.combine(e)
    assert np.allclose(c.comb, [0.5, 0.2, 0.25])
    assert np.isnan(c.z[0]) and np.isnan(c.z[1])



def test_prior_falls_back_to_the_surface():
    # asphalt/good: 20 measured link directions; asphalt/bad: 5 (too few: all asphalt); sett/bad: 5 (no prior);
    # asphalt/-: 20, but no smoothness tag: all asphalt; -/-: 25 (no surface tag: no prior)
    cls = ["asphalt/good"] * 20 + ["asphalt/bad"] * 5 + ["sett/bad"] * 5 + ["asphalt/-"] * 20 + ["-/-"] * 25
    est = [0.0] * 20 + [0.5] * 5 + [0.6] * 5 + [0.1] * 20 + [0.3] * 25
    e = pd.DataFrame({"osm_class": cls, "est": est, "se": 0.1, "information": 5.0})
    p = estimate.priors(e)
    assert np.allclose(p.prior[:20], 0.0) and p.prior[25:30].isna().all() and p.prior[50:].isna().all()
    assert np.allclose(p.prior[20:25], 0.1) and np.allclose(p.prior[30:50], 0.1)      # median of all 45 asphalt
    assert p.by_surface[30:50].all()
    assert not p.by_surface[0] and p.by_surface[20] and not p.by_surface[25]
