"""Where the pass weights (estimate.py) and the speed exponent (simra.py) come from: each is measured on the passes
and printed next to the value in use.

Pass weights: a pass should count by how closely its value tells the link direction's value (inverse variance),
relative to the best passes. Per bin of age and of length (and, to show why they are no such weight, GPS error and
match certainty):
    scatter = mean over its passes of (value - reference)² - the reference's own noise (its variance / n)
    weight  = scatter of the best bin / scatter of this bin
Reference: for length, GPS and match, the mean of the other passes on the same link direction (>= CAL_MIN_PASSES of
them); for age, the mean of the newest year's passes there (>= CAL_MIN_NEW; a newest-year pass is left out of its own
reference), since the age weight is about how well a pass tells today's value. Each curve's parameter is fitted to
the bins (least squares, bins weighted by their passes). The data only reaches 3.3 years back.
GPS error and match certainty don't change the scatter: the value comes from the accelerometer, so the GPS can only
put a pass on the wrong line, which the matcher's certainty (match_p, with the GPS error in its distance cost) is
for. That weight is a probability (half likely on the other line), which this test can't see: the reference, the
other passes on the same line, has the same mix.

Speed: the slope of log(swell) on log(speed) over passes, with ride and link-direction fixed effects (the same rider
on the same surface, faster or slower).

Drift: the estimate from the older years and from the newest year (the ride fit run on each), on link directions
with information >= CAL_DRIFT_INFO in both. Their difference spreads more than their standard errors say: the street
moved (resurfaced, worn) or the riders and phones changed. DRIFT = √(var(difference) - mean(se_old² + se_new²)).
Two halves of the rides from the same years differ by less than their standard errors (checked), so the extra is
the time.

The fitted numbers (SPEED_EXP, LENGTH_EXP, DRIFT) are written to calibration.json, which config.py reads. Run it after
match.py: estimate.py then uses the new length exponent; a new speed exponent needs simra.py and the steps after it
again (it changes little: the fit doesn't depend on the exponent in use). The age weight is printed, not written: it
is a choice (config.py AGE_HALF).

    venv/bin/python pipeline/calibrate.py
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from common import PASSES
from config import (AGE_HALF, AGE_POWER, CAL_DRIFT_INFO, CAL_MIN_NEW, CAL_MIN_PASSES, CAL_SPEED_MIN_S, CALIBRATION,
                    DRIFT, LENGTH_EXP, LENGTH_REF_M, MIN_INFO, NEWEST_MONTHS, SPEED_EXP)
from estimate import KEY, age_weight, age_years, length_weight, pass_weights, ride_fit, standard_error, weighted


def scatter(p: pd.DataFrame, ref: np.ndarray, min_n: int) -> pd.Series:
    """per pass: (value - mean of the reference passes on its link direction, itself left out)² - that mean's noise"""
    r = p[ref].groupby(KEY).value.agg(["sum", "count", "var"])
    j = p[KEY].join(r, on=KEY)
    n = j["count"] - ref
    mean = (j["sum"] - np.where(ref, p.value, 0)) / n
    return ((p.value - mean) ** 2 - j["var"] / n).where(n >= min_n)


def bins(x: pd.Series, sq: pd.Series, edges: list, best: int) -> pd.DataFrame:
    """bins of x: passes, mean x, measured weight (relative to the best bin)"""
    ok = sq.notna()
    t = pd.DataFrame({"x": x[ok], "sq": sq[ok]}).groupby(pd.cut(x[ok], edges), observed=True).agg(
        passes=("sq", "size"), x=("x", "mean"), sq=("sq", "mean"))
    t["measured"] = t.sq.iloc[best] / t.sq
    return t.drop(columns="sq")


def curve(name: str, x: pd.Series, sq: pd.Series, edges: list, best: int, f, grid, used: float, unit: str):
    """bins of x with the measured weight and the curve's (both relative to the best bin); the fitted parameter"""
    t = bins(x, sq, edges, best)
    rel = lambda g: f(t.x, g) / f(t.x.iloc[best], g)
    fit = min(grid, key=lambda g: ((t.measured - rel(g)) ** 2 * t.passes).sum())
    t[f"curve ({used:g} {unit})"], t[f"fitted ({fit:g} {unit})"] = rel(used), rel(fit)
    print(f"\n{name}: fitted {fit:g} {unit}, in use {used:g} {unit}")
    print(t.round(2).to_string())
    return round(float(fit), 2)


def speed(p: pd.DataFrame) -> float:
    q = p[p.seconds >= CAL_SPEED_MIN_S]
    x = np.log(q.metres / q.seconds)
    y = q.raw + SPEED_EXP * x                                        # raw = median log(swell / speed^SPEED_EXP)
    keys = [pd.factorize(q.ride_key)[0], pd.factorize(q.link_id * 2 + (q.direction > 0))[0]]
    for _ in range(30):                                              # alternating demeaning = both fixed effects
        for k in keys:
            y, x = y - y.groupby(k).transform("mean"), x - x.groupby(k).transform("mean")
    fit = round(float((x * y).sum() / (x * x).sum()), 2)
    print(f"speed exponent, ride + link direction fixed effects, {len(q):,} passes: fitted {fit:g}, "
          f"in use {SPEED_EXP:g}")
    return fit


def drift(p: pd.DataFrame) -> float:
    new = age_years(p) < NEWEST_MONTHS / 12
    parts = []
    for part in (p[~new], p[new]):
        e = weighted(ride_fit(part.assign(w=pass_weights(part)))[0])
        e["se"] = standard_error(e, e.sd[e.information >= MIN_INFO].median(), drift=0).to_numpy()
        parts.append(e.set_index(KEY))
    j = parts[0].join(parts[1], how="inner", lsuffix="_old", rsuffix="_new")
    j = j[(j.information_old >= CAL_DRIFT_INFO) & (j.information_new >= CAL_DRIFT_INFO)]
    extra = (j.est_old - j.est_new).var() - (j.se_old ** 2 + j.se_new ** 2).mean()
    fit = round(float(np.sqrt(max(extra, 0))), 2)
    print(f"drift, {len(j):,} link directions with information >= {CAL_DRIFT_INFO:g} before and in the newest year: "
          f"fitted {fit:g}, in use {DRIFT:g}")
    return fit


def main():
    p = pd.read_parquet(PASSES)
    fitted = {"SPEED_EXP": speed(p)}
    age = age_years(p)
    curve(f"age weight 1 / (1 + (age / AGE_HALF)^{AGE_POWER}), reference: the newest year (a choice in config.py, "
          "not written)",
          pd.Series(age), scatter(p, age < NEWEST_MONTHS / 12, CAL_MIN_NEW), [0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5], 0,
          age_weight, np.arange(1, 20.1, 0.5), AGE_HALF, "years")
    sq = scatter(p, np.ones(len(p), bool), CAL_MIN_PASSES)
    fitted["LENGTH_EXP"] = curve(
        f"pass length weight (metres / {LENGTH_REF_M:g})^k, reference: the other passes",
        p.metres, sq, [0, 5, 10, 15, 20, 30, 50, 100, 1e5], 3, length_weight, np.arange(0, 1.01, 0.02),
        LENGTH_EXP, "= k")
    print("\nGPS error h_acc (no weight: no effect on the scatter)")
    print(bins(p.h_acc, sq, [0, 4, 6, 8, 10, 13, 16, 20], 0).round(2).to_string())
    print("\nmatch certainty (the weight is this probability itself; no effect on the scatter, see above)")
    print(bins(p.match_p, sq, [0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.97, 1.001], -1).round(2).to_string())
    fitted["DRIFT"] = drift(p)
    CALIBRATION.write_text(json.dumps(fitted, indent=2) + "\n")
    in_use = {"SPEED_EXP": SPEED_EXP, "LENGTH_EXP": LENGTH_EXP, "DRIFT": DRIFT}
    print(f"\n{fitted} -> {CALIBRATION}" + ("" if fitted == in_use else
          "\nchanged: rerun simra.py (speed) or estimate.py (length, drift) and the steps after it"))


if __name__ == "__main__":
    main()
