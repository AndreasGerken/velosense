"""Checks on the pipeline's outputs (matching is reported by match.py, the fitted numbers by calibrate.py).

1. Normalization: on link directions with >= CHECK_MIN_PASSES passes, the spread of pass values is smaller with
   `norm` (per-ride median removed) than with the raw `value`.
2. Reliability: rides split in two halves (even / odd SimRa id), the ride fit (offset and noise per ride) run in each
   half on its own; on link directions with information >= CHECK_LEVELS in both halves, the correlation of the two
   halves' estimates, with and without the ride fit.
3. Weight of the measurement against the OSM prior, for today: the newest year's passes are held out; on link
   directions with them and with >= n older passes, n random older passes are measured. The best weight (the slope
   of held-out - prior on measured - prior) should match the weight the model gives (1/se² against 1/tau²), which
   counts the older passes less by their age. The ride fit runs on the older and on the newest passes separately.

    venv/bin/python pipeline/checks.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import LINKS_EST, PASSES
from config import CHECK_LEVELS, CHECK_MIN_PASSES, MIN_INFO, NEWEST_MONTHS
from estimate import KEY, age_years, pass_weights, ride_fit, standard_error, weighted


def main():
    p = pd.read_parquet(PASSES)
    p["w"] = pass_weights(p)

    # 1. normalization
    busy = p.groupby(KEY).value.transform("size") >= CHECK_MIN_PASSES
    sd = p[busy].groupby(KEY).agg(norm=("value", "std"), raw=("raw", "std"))
    print(f"1. normalization, {len(sd):,} link directions with >= {CHECK_MIN_PASSES} passes: median sd between passes "
          f"norm {sd.norm.median():.3f} vs raw {sd.raw.median():.3f}; norm smaller on {(sd.norm < sd.raw).mean():.0%}")

    # 2. split-half reliability
    rid = p.ride_key.astype(str).str.extract(r"VM2_(-?\d+)")[0].astype(np.int64)
    halves = [p[(rid % 2 == k).to_numpy()] for k in (0, 1)]
    plain = [weighted(h).set_index(KEY) for h in halves]
    fitted = [weighted(ride_fit(h)[0]).set_index(KEY) for h in halves]
    for n in CHECK_LEVELS:
        sel = plain[0].index[plain[0].information >= n].intersection(plain[1].index[plain[1].information >= n])
        r = [np.corrcoef(h[0].est.reindex(sel), h[1].est.reindex(sel))[0, 1] for h in (fitted, plain)]
        print(f"2. split halves, information >= {n:g} in both: {len(sel):,} link directions, "
              f"r = {r[0]:.3f} (without the ride fit {r[1]:.3f})")

    # 3. weight of the measurement against the prior, for today
    full = weighted(ride_fit(p)[0]).set_index(KEY)
    sd_one = full.sd[full.information >= MIN_INFO].median()
    e = pd.read_parquet(LINKS_EST).set_index(KEY)
    prior = e.prior + (full.est - e.est).median()            # back to the passes' units (links_est: 0 = asphalt)
    new = age_years(p) < NEWEST_MONTHS / 12
    held = weighted(ride_fit(p[new])[0]).set_index(KEY).est.rename("held")
    old = ride_fit(p[~new])[0].sample(frac=1, random_state=0)
    rank = old.groupby(KEY).cumcount()
    for n in CHECK_LEVELS:
        a = weighted(old[rank < n]).set_index(KEY)
        a = a[a.passes == n].join(held, how="inner")
        a = a[prior.reindex(a.index).notna()]                     # only where OSM predicts
        x, y = a.est - prior, a.held - prior
        w_est = 1 / standard_error(a, sd_one) ** 2
        model = (w_est / (w_est + 1 / e.tau.reindex(a.index) ** 2)).median()
        print(f"3. {n:2d} older passes (information {a.information.median():.2f}), {len(a):,} link directions: "
              f"best weight of the measurement for the newest year {(x * y).sum() / (x * x).sum():.2f}, "
              f"model {model:.2f}")


if __name__ == "__main__":
    main()
