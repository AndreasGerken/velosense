"""passes.parquet + links.parquet -> links_est.parquet: one value per link and riding direction.

1. Pass weight w = age x length x match:
   age: 1 / (1 + (age / AGE_HALF)^AGE_POWER) (nearly full for a year, x0.84 at 2 years, half at 3, x0.11 at 5: old
   passes barely count; age from the newest month in the data);
   length: (metres / LENGTH_REF_M)^LENGTH_EXP (a longer pass averages more seconds);
   match: match_p (match.py: how likely the pass is on this line; the GPS error enters there).
   calibrate.py shows the data behind them.
   Per ride (`ride_fit`): how the ride fits the other rides on its route. Each pass's residual against the other
   passes on its link direction (where they have information >= FIT_MIN_OTHERS) gives
   offset: the ride's mean residual, shrunk toward 0 by n0 = var(residual) / var(true offsets) passes. The true
   offsets' variance is measured as the covariance of each ride's mean residual on its even and odd links (rides with
   >= FIT_HALF_MIN on both): a consistent offset shows on both, noise doesn't. Normalizing by the ride's own median
   leaves one, e.g. a ride with much sett on it reads too smooth everywhere. The offset is taken off its values.
   noise: the ride's mean squared residual after the offset, shrunk toward the median ride's by NOISE_PRIOR_N
   passes; the pass weight is multiplied by median / the ride's (inverse variance: a ride that scatters more counts
   less). Both FIT_ROUNDS times, each round against the corrected others.
2. Per (link, direction): est = Σ w·value / Σ w; information = Σ w (the passes, each counted by its weight);
   se = √(max(sd, sd_one)² / information + DRIFT²) (sd = the spread of the pass values; sd_one = its median over
   link directions with information >= MIN_INFO: a few passes that happen to agree don't make the estimate sure;
   DRIFT, fitted by calibrate.py: how far a street moves from the older years to the newest one, so even many old
   passes don't tell today's value exactly).
3. OSM prior per class (raw surface x smoothness tag): the median est of its link directions with information
   >= MIN_INFO; tau = how much their true values spread: their spread minus the measurement noise (median se² without
   the drift: past and today's values spread alike over a class).
   The spread is read from the range of the middle 1 - 2·TAU_TAIL of them (as the sd of a normal distribution with
   that range), not from the middle half: streets with the same tags have more outliers than a normal distribution,
   and a prediction should say how far off it can be.
   Without a smoothness tag, or with fewer than MIN_LINKS such link directions, the prior of all link directions
   of its surface (same rule; `by_surface`); without a surface tag (or too few) none: not predictable from OSM.
4. Combined = the prior and est weighted by 1/tau² and 1/se² (= the prior without passes, est without a prior).
5. Difference z = (est - prior) / √(tau² + se² - DRIFT²) (both from the same years: the drift is no part of their
   gap).
All values are logs relative to asphalt/good (its prior = 0), so exp(value) = "x as rough as good asphalt".

Every link direction a bike may ride is in the output (one-way links in one direction only).

    venv/bin/python pipeline/estimate.py

The numbers (AGE_HALF, ...) are in config.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm

from common import LINKS, LINKS_EST, PASSES, Log
from config import (AGE_HALF, AGE_POWER, DRIFT, FIT_HALF_MIN, FIT_MIN_OTHERS, FIT_ROUNDS, LENGTH_EXP, LENGTH_REF_M,
                    MIN_INFO, MIN_LINKS, NOISE_PRIOR_N, TAU_MIN, TAU_TAIL)

KEY = ["link_id", "direction"]


def age_years(p: pd.DataFrame) -> np.ndarray:
    """age of each pass in years, from the newest month in the data"""
    month = pd.PeriodIndex(p.month.astype(str), freq="M")
    return (month.max() - month).map(lambda d: d.n).to_numpy() / 12


def age_weight(age, half=AGE_HALF):
    return 1 / (1 + (age / half) ** AGE_POWER)


def length_weight(metres, k=LENGTH_EXP):
    return (metres / LENGTH_REF_M) ** k


def pass_weights(p: pd.DataFrame) -> np.ndarray:
    """w = age x length x match certainty"""
    return age_weight(age_years(p)) * length_weight(p.metres.to_numpy()) * p.match_p.to_numpy()


def per_ride(ride: np.ndarray, x: np.ndarray | None, rides: int) -> np.ndarray:
    """sum of x (or the count) per ride"""
    return np.bincount(ride, x, rides)


def ride_fit(p: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """passes (with w) -> the passes with each ride's offset taken off `value` and its noise weight in `w`; and the
    numbers to report"""
    ride = p.ride_key.astype("category").cat.codes.to_numpy()
    rides, even = ride.max() + 1, p.link_id.to_numpy() % 2 == 0
    value, w0 = p.value.to_numpy().copy(), p.w.to_numpy()
    w, report = w0.copy(), {}
    for _ in range(FIT_ROUNDS):
        # residual of each pass against the other passes on its link direction
        g = pd.DataFrame({"link_id": p.link_id.to_numpy(), "direction": p.direction.to_numpy(), "w": w,
                          "wv": w * value}).groupby(KEY)[["w", "wv"]].transform("sum")
        others = g.w.to_numpy() - w
        ok = others >= FIT_MIN_OTHERS
        r = value[ok] - (g.wv.to_numpy()[ok] - (w * value)[ok]) / others[ok]
        k, n = ride[ok], per_ride(ride[ok], None, rides)

        # how much true offsets spread: the covariance of each ride's mean residual on its even and odd links
        means, enough = [], True
        for half in (even[ok], ~even[ok]):
            c = per_ride(k[half], None, rides)
            means.append(per_ride(k[half], r[half], rides) / np.maximum(c, 1))
            enough = enough & (c >= FIT_HALF_MIN)
        var_true = max(np.cov(means[0][enough], means[1][enough])[0, 1], 0.0)
        n0 = r.var() / var_true if var_true > 0 else np.inf          # shrinkage, in passes
        offset = per_ride(k, r, rides) / (n + n0)

        # noise: mean squared residual after the offset, shrunk toward the median ride's
        sq = per_ride(k, (r - offset[k]) ** 2, rides)
        typical = np.median(sq[n >= FIT_HALF_MIN] / n[n >= FIT_HALF_MIN])
        noise = (sq + NOISE_PRIOR_N * typical) / (n + NOISE_PRIOR_N)
        value -= offset[ride]
        w = w0 * (typical / noise)[ride]
        report.setdefault("sd_true", np.sqrt(var_true))
        report.setdefault("n0", n0)
    taken = per_ride(ride, p.value.to_numpy() - value, rides) / np.maximum(per_ride(ride, None, rides), 1)
    report.update(sd_offset=taken.std(), weight=np.percentile(w / w0, [10, 50, 90]))
    return p.assign(value=value, w=w), report


def weighted(p: pd.DataFrame) -> pd.DataFrame:
    """passes (with w) -> per (link_id, direction): est, sd, information, passes"""
    e = p.assign(wv=p.w * p.value, wvv=p.w * p.value ** 2).groupby(KEY).agg(
        information=("w", "sum"), swv=("wv", "sum"), swvv=("wvv", "sum"), passes=("value", "size"))
    e["est"] = e.swv / e.information
    var = np.maximum(e.swvv / e.information - e.est ** 2, 0) * e.passes / (e.passes - 1)   # nan for one pass
    e["sd"] = np.sqrt(var.where(e.passes > 1))
    return e[["est", "sd", "information", "passes"]].reset_index()


def standard_error(e: pd.DataFrame, sd_one: float, drift: float = DRIFT) -> pd.Series:
    noise = np.maximum(e.sd.fillna(0), sd_one) ** 2 / e.information.where(e.information > 0)
    return np.sqrt(noise + drift ** 2)


def combine(e: pd.DataFrame) -> pd.DataFrame:
    """est, se, prior, tau -> comb, comb_se, z"""
    w_p, w_e = (1 / e.tau ** 2).fillna(0), (1 / e.se ** 2).fillna(0)
    w = (w_p + w_e).where(w_p + w_e > 0)
    return pd.DataFrame({"comb": (e.prior.fillna(0) * w_p + e.est.fillna(0) * w_e) / w, "comb_se": 1 / np.sqrt(w),
                         "z": (e.est - e.prior) / np.sqrt(e.tau ** 2 + e.se ** 2 - DRIFT ** 2)}, index=e.index)


def wide_sd(x) -> float:
    """the sd of a normal distribution whose middle 1 - 2·TAU_TAIL has the same range as x's"""
    lo, hi = np.quantile(x, [TAU_TAIL, 1 - TAU_TAIL])
    return float((hi - lo) / (2 * norm.ppf(1 - TAU_TAIL)))


def priors(e: pd.DataFrame) -> pd.DataFrame:
    """per link direction: prior, tau and by_surface. From its OSM class (surface/smoothness) when both tags are
    there and the class has >= MIN_LINKS link directions of information >= MIN_INFO, else from all link directions
    of its surface (same rule), else none. Without a surface tag: none (a smoothness alone is too rare to learn
    from)"""
    tags = e.osm_class.str.split("/", expand=True)
    e = e.assign(surface=tags[0], se2=e.se ** 2 - DRIFT ** 2)    # the noise: drift doesn't widen a class
    teach = e[e.information >= MIN_INFO]

    def per(key):
        s = teach.groupby(key).agg(n=("est", "size"), prior=("est", "median"), spread=("est", wide_sd),
                                   noise=("se2", "median"))
        s["tau"] = np.sqrt(np.maximum(s.spread ** 2 - s.noise, TAU_MIN ** 2))
        return e[[key]].join(s.loc[s.n >= MIN_LINKS, ["prior", "tau"]], on=key)[["prior", "tau"]]

    cls, surface = per("osm_class").where(tags[1] != "-"), per("surface").where(tags[0] != "-")
    out = cls.fillna(surface)
    out["by_surface"] = cls.prior.isna() & surface.prior.notna()
    return out


def main():
    log = Log()
    links = pd.read_parquet(LINKS, columns=["link_id", "oneway", "osm_class"])
    p = pd.read_parquet(PASSES)
    p["w"] = pass_weights(p)
    log(f"{len(p):,} passes, newest month {p.month.astype(str).max()}")
    p, fit = ride_fit(p)
    log(f"rides: true offsets sd {fit['sd_true']:.3f} (n0 {fit['n0']:.0f} passes), offsets taken off sd "
        f"{fit['sd_offset']:.3f}; noise weight 10/50/90 % {fit['weight'].round(2).tolist()}")

    # every link direction a bike may ride
    both = links[links.oneway == 0]
    dirs = pd.concat([both.assign(direction=1), both.assign(direction=-1),
                      links[links.oneway != 0].assign(direction=links.oneway)], ignore_index=True)
    e = dirs[["link_id", "direction", "osm_class"]].merge(weighted(p), on=KEY, how="left")
    e["passes"] = e.passes.fillna(0).astype(int)
    e["information"] = e.information.fillna(0.0)

    sd_one = e.sd[e.information >= MIN_INFO].median()
    e["se"] = standard_error(e, sd_one)
    e = e.join(priors(e))

    ref = e.prior[e.osm_class == "asphalt/good"].iloc[0]               # 0 = good asphalt
    e[["est", "prior"]] -= ref
    e = e.join(combine(e))
    cols = ["link_id", "direction", "est", "se", "information", "passes", "prior", "tau", "comb",
            "comb_se", "z", "osm_class", "by_surface"]
    e[cols].to_parquet(LINKS_EST, index=False)

    has = e.passes > 0
    log(f"{len(e):,} link directions, {has.sum():,} with passes, "
        f"{(e.information >= MIN_INFO).sum():,} with information >= {MIN_INFO:g}; sd of one pass {sd_one:.2f}")
    log(f"OSM prior from the surface alone on {e.by_surface.sum():,}, none (no surface tag or too rarely measured) "
        f"on {e.prior.isna().sum():,}; |z| >= 2 on {(e.z.abs() >= 2).sum():,} -> {LINKS_EST}")
    cls = e[e.information >= MIN_INFO].groupby("osm_class").agg(
        n=("est", "size"), est=("est", "median"), prior=("prior", "first"), tau=("tau", "first"))
    log("classes (log units, 0 = asphalt/good; x as rough = exp):\n" +
        cls[cls.n >= MIN_LINKS].sort_values("est").round(2).to_string())


if __name__ == "__main__":
    main()
