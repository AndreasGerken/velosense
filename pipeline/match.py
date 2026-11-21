"""seconds.parquet + links.parquet -> passes.parquet.

1. Every link is sampled every SAMPLE_M metres; one cKDTree over all samples.
2. Candidates per second: of the K nearest link samples, those within R = 2 x h_acc (R_MIN to R_MAX) whose
   direction fits the ride's heading (within MAX_DEG of the link's drawn direction: +1, of the opposite: -1; not
   against a bike one-way). Cost (d / sigma)² / 2 with sigma = the GPS error clip(h_acc, SIGMA_MIN, MAX_H_ACC), plus
   PATH_BESIDE_COST on a road whose bikes ride on a separate path beside it (osm.py `path_beside`) and FOOT_COST on
   a footway that doesn't invite bikes (`foot_only`). With a poor fix the distance counts less, so where bikes are
   meant to ride counts more. The C cheapest link directions are kept, plus "off the network" (NONE_COST).
3. Route (an HMM, solved by Viterbi for all rides at once): the cheapest sequence of candidates, where moving on to
   a link direction that touches the last one (its end node is the next one's start node, or a link shorter than
   SKIP_M joins them) is free and any other move costs JUMP_COST (half of it on or off the network). So GPS that
   wanders to a parallel line for a few seconds doesn't move the ride there. After a gap > FREE_GAP_S, moves
   are free.
   Certainty per second: the chosen candidate's likelihood exp(-cost) against those of all candidates on other
   lines (link directions that share no node with it; the same street going on doesn't compete): 1 = no other
   line near, 0.5 = a parallel line just as close (a road and its cycle track).
4. A ride with less than MIN_MATCHED of its seconds matched gives no passes (off the network, or broken GPS).
5. Along the ride: each second's `norm` and `value` become their mean over the ride's seconds within ±SMOOTH_M / 2
   metres (distance from the speed; a gap > FREE_GAP_S ends the stretch). So a short link gets the street's value
   around it, not just a kerb on its end.
6. Passes: consecutive seconds of one ride on one (link, direction), gaps <= MAX_GAP_S; all kept, even one second
   on a short link (estimate.py weights a pass by its length). Pass value = median `norm`, raw = median `value` (for
   the normalization check), h_acc = median, match_p = mean certainty (estimate.py weights the pass by it).

    venv/bin/python pipeline/match.py

The numbers (SAMPLE_M, ...) are in config.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from common import DATA, LINKS, PASSES, SECONDS, Log, sample, xy
from config import (CHUNK, FOOT_COST, FREE_GAP_S, JUMP_COST, MAX_DEG, MAX_GAP_S, MAX_H_ACC, MIN_MATCHED, NONE_COST,
                    PATH_BESIDE_COST, R_MAX, R_MIN, SAMPLE_M, SIGMA_MIN, SKIP_M, SMOOTH_M, C, K)

RIDE_MATCH = DATA / "rides_matched.parquet"


def candidates(sec: pd.DataFrame, links: pd.DataFrame, s: dict, tree: cKDTree) -> tuple[np.ndarray, np.ndarray]:
    """per second the C cheapest states (link_id x 2 + (direction > 0); -1: none) and their costs"""
    x, y = xy(sec.lat.to_numpy(), sec.lon.to_numpy())
    radius = np.clip(2 * sec.h_acc.to_numpy(), R_MIN, R_MAX)
    sigma = np.clip(sec.h_acc.to_numpy(), SIGMA_MIN, MAX_H_ACC)
    hx, hy = np.sin(np.radians(sec.heading.to_numpy())), np.cos(np.radians(sec.heading.to_numpy()))
    oneway = links.oneway.to_numpy()
    extra = PATH_BESIDE_COST * links.path_beside.to_numpy() + FOOT_COST * links.foot_only.to_numpy()
    state = np.full((len(sec), C), -1, np.int32)
    cost = np.full((len(sec), C), np.inf, np.float32)
    cos_max = np.cos(np.radians(MAX_DEG))
    for a in range(0, len(sec), CHUNK):
        b = min(a + CHUNK, len(sec))
        d, j = tree.query(np.c_[x[a:b], y[a:b]], k=K, distance_upper_bound=R_MAX)
        found = np.isfinite(d)
        j = np.where(found, j, 0)
        line = s["line"][j]
        cos = hx[a:b, None] * s["ux"][j] + hy[a:b, None] * s["uy"][j]       # heading vs the link's drawn direction
        dj = np.where(cos >= cos_max, 1, np.where(cos <= -cos_max, -1, 0))
        ok = found & (d <= radius[a:b, None]) & (dj != 0) & ((oneway[line] == 0) | (oneway[line] == dj))
        co = np.where(ok, (np.where(found, d, 0) / sigma[a:b, None]) ** 2 / 2 + extra[line], np.inf)
        st = line * 2 + (dj > 0)
        o = np.lexsort((co, st), axis=1)                                      # by state, then cost
        st, co = np.take_along_axis(st, o, 1), np.take_along_axis(co, o, 1)
        co[:, 1:][st[:, 1:] == st[:, :-1]] = np.inf                           # each state once, at its cheapest
        o = np.argsort(co, axis=1)[:, :C]
        st, co = np.take_along_axis(st, o, 1), np.take_along_axis(co, o, 1)
        state[a:b] = np.where(np.isfinite(co), st, -1)
        cost[a:b] = co
    return state, cost


def viterbi(state: np.ndarray, cost: np.ndarray, ride: np.ndarray, t: np.ndarray,
            links: pd.DataFrame) -> np.ndarray:
    """per second the state on the cheapest route through its ride's candidates (-1: off the network)"""
    nodes, ends = np.unique(np.r_[links.u, links.v], return_inverse=True)
    u, v = ends[:len(links)], ends[len(links):]
    entry, exit_ = np.c_[v, u].ravel(), np.c_[u, v].ravel()           # per state: -1 runs v -> u, +1 runs u -> v
    short = links.length.to_numpy() < SKIP_M
    n = len(nodes)
    pairs = np.unique(np.r_[u[short] * n + v[short], v[short] * n + u[short]])

    def move(prev, cur):                                               # cost of prev -> cur
        a, b = exit_[np.maximum(prev, 0)], entry[np.maximum(cur, 0)]
        i = np.minimum(np.searchsorted(pairs, a * n + b), len(pairs) - 1)
        touch = (a == b) | (pairs[i] == a * n + b)
        return np.where(prev == cur, 0.0, np.where((prev < 0) | (cur < 0), JUMP_COST / 2,
                                                   np.where(touch, 0.0, JUMP_COST)))

    S = np.c_[state, np.full(len(state), -1, np.int32)]                # + "off the network"
    E = np.c_[cost, np.full(len(cost), NONE_COST, np.float32)]
    first = np.r_[0, np.flatnonzero(np.diff(ride)) + 1]
    length = np.diff(np.r_[first, len(ride)])
    o = np.argsort(-length, kind="stable")                             # longest first: rides going at step k
    first, length = first[o], length[o]                                # are the first going(k)
    going = lambda k: np.searchsorted(-length, -k, side="left")        # rides with length > k
    back = np.zeros(S.shape, np.int8)
    final = np.zeros((len(first), C + 1))                              # D at each ride's last second
    D = E[first].astype(float)
    for k in range(1, length[0] + 1):
        m = going(k)
        final[m:going(k - 1)] = D[m:]                                  # rides that ended at step k - 1
        if k == length[0]:
            break
        rows = first[:m] + k
        free = (t[rows] - t[rows - 1] > FREE_GAP_S)[:, None, None]
        tot = D[:m, :, None] + np.where(free, 0.0, move(S[rows - 1][:, :, None], S[rows][:, None, :]))
        b = tot.argmin(1)
        back[rows] = b
        D = np.take_along_axis(tot, b[:, None, :], 1)[:, 0, :] + E[rows]
    out = np.empty(len(state), np.int64)
    choice = np.zeros(len(first), np.int64)
    for k in range(length[0] - 1, -1, -1):
        m = going(k)
        ending = slice(going(k + 1), m)
        choice[ending] = final[ending].argmin(1)
        rows = first[:m] + k
        out[rows] = S[rows, choice[:m]]
        choice[:m] = back[rows, choice[:m]]
    return out


def certainty(state: np.ndarray, cost: np.ndarray, label: np.ndarray, links: pd.DataFrame) -> np.ndarray:
    """per second: likelihood of the chosen candidate / (it + those on lines that share no node with it)"""
    u, v = links.u.to_numpy(), links.v.to_numpy()
    out = np.full(len(label), np.nan, np.float32)
    for a in range(0, len(label), CHUNK):
        st, co, la = state[a:a + CHUNK], cost[a:a + CHUNK], label[a:a + CHUNK]
        with np.errstate(invalid="ignore"):                              # rows without candidates
            lik = np.exp(-(co - co.min(1, keepdims=True)))               # inf cost -> 0
        k, c = np.maximum(la, 0)[:, None] // 2, np.maximum(st, 0) // 2
        touch = (u[c] == u[k]) | (u[c] == v[k]) | (v[c] == u[k]) | (v[c] == v[k])
        chosen = np.where(st == la[:, None], lik, 0).sum(1)
        other = np.where(touch, 0, lik).sum(1)
        out[a:a + CHUNK] = np.where(la >= 0, chosen / (chosen + other), np.nan)
    return out


def along_ride(sec: pd.DataFrame, col: str) -> np.ndarray:
    """per second: the mean of col over its ride's seconds within ±SMOOTH_M / 2 metres (sec sorted by ride, t)"""
    r, t, v = sec.ride_key.cat.codes.to_numpy(), sec.t.to_numpy(), sec.speed.to_numpy(float)
    stretch = np.cumsum(np.r_[True, (r[1:] != r[:-1]) | (np.diff(t) > FREE_GAP_S)])
    at = pd.Series(v).groupby(stretch).cumsum().to_numpy() - v / 2 + stretch * 1e7   # metres; stretches apart
    lo = np.searchsorted(at, at - SMOOTH_M / 2, "left")
    hi = np.searchsorted(at, at + SMOOTH_M / 2, "right")
    c = np.r_[0, np.cumsum(sec[col].to_numpy(float))]
    return (c[hi] - c[lo]) / (hi - lo)


def main():
    log = Log()
    links = pd.read_parquet(LINKS, columns=["link_id", "u", "v", "length", "lat", "lon", "oneway", "path_beside",
                                           "foot_only"])
    assert (links.link_id.to_numpy() == np.arange(len(links))).all()     # link_id = row
    s = sample(links.lat, links.lon, SAMPLE_M)
    tree = cKDTree(np.c_[s["x"], s["y"]])
    log(f"{len(links):,} links, {len(s['x']):,} samples")

    sec = pd.read_parquet(SECONDS).sort_values(["ride_key", "t"], kind="stable").reset_index(drop=True)
    sec = sec[np.isfinite(sec.heading)].reset_index(drop=True)
    log(f"{len(sec):,} seconds, {sec.ride_key.nunique():,} rides")
    state, cost = candidates(sec, links, s, tree)
    log(f"candidates: {np.mean(state[:, 0] >= 0):.1%} of seconds have one")
    label = viterbi(state, cost, sec.ride_key.cat.codes.to_numpy(), sec.t.to_numpy(), links)
    sec["link_id"], sec["direction"] = np.where(label >= 0, label // 2, -1), np.where(label % 2 == 1, 1, -1)
    sec["certainty"] = certainty(state, cost, label, links)
    log(f"route: {np.mean(label >= 0):.1%} of seconds matched, {np.mean(label != state[:, 0]):.1%} not on their "
        f"cheapest candidate")

    # 4. rides that don't fit the network
    share = (sec.link_id >= 0).groupby(sec.ride_key, observed=True).mean().rename("matched").reset_index()
    share["dropped"] = share.matched < MIN_MATCHED
    share.to_parquet(RIDE_MATCH, index=False)
    keep = ~sec.ride_key.isin(share.ride_key[share.dropped])
    log(f"rides dropped (matched < {MIN_MATCHED}): {share.dropped.sum():,} of {len(share):,} "
        f"({share.dropped.mean():.1%}); matched share on the kept rides: "
        f"{(sec.link_id[keep] >= 0).mean():.1%}")

    # 5. along the ride
    sec["norm"], sec["value"] = along_ride(sec, "norm"), along_ride(sec, "value")

    # 6. passes
    m = sec[keep & (sec.link_id >= 0)].reset_index(drop=True)
    gap = np.r_[True, np.diff(m.t.to_numpy()) > MAX_GAP_S]
    lab = (m.link_id * 2 + (m.direction > 0)).to_numpy()
    new = np.r_[True, (m.ride_key.cat.codes.to_numpy()[1:] != m.ride_key.cat.codes.to_numpy()[:-1])
                | (lab[1:] != lab[:-1])] | gap
    m["pass_id"] = np.cumsum(new) - 1
    p = m.groupby("pass_id").agg(ride_key=("ride_key", "first"), month=("month", "first"),
                                 link_id=("link_id", "first"), direction=("direction", "first"),
                                 seconds=("t", "size"), metres=("speed", "sum"), value=("norm", "median"),
                                 raw=("value", "median"), h_acc=("h_acc", "median"),
                                 match_p=("certainty", "mean"))
    p = p.reset_index()
    p.to_parquet(PASSES, index=False)
    log(f"{len(p):,} passes on {p.groupby(['link_id', 'direction']).ngroups:,} link directions "
        f"({p.link_id.nunique():,} links) -> {PASSES}")


if __name__ == "__main__":
    main()
