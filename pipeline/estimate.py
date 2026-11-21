"""passes.parquet + links.parquet -> links_est.parquet: one value per link and riding direction.

1. Pass weight w = age x length x match:
   age: 1 / (1 + (age / AGE_HALF)^AGE_POWER) (nearly full for a year, x0.84 at 2 years, half at 3, x0.11 at 5: old
   passes barely count; age from the newest month in the data);
   length: (metres / LENGTH_REF_M)^LENGTH_EXP (a longer pass averages more seconds);
   match: match_p (match.py: how likely the pass is on this line; the GPS error enters there).
   calibrate.py shows the data behind them.
2. Per (link, direction): est = Σ w·value / Σ w; information = Σ w (the passes, each counted by its weight);
   se = max(sd, sd_one) / √information (sd = the spread of the pass values; sd_one = its median over link
   directions with information >= MIN_INFO: a few passes that happen to agree don't make the estimate sure).
3. OSM prior per class (raw surface x smoothness tag): the median est of its link directions with information
   >= MIN_INFO; tau = how much their true values spread: their spread (1.4826 x MAD, the robust sd) minus the
   measurement noise (median se²).
   Without a smoothness tag, or with fewer than MIN_LINKS such link directions, the prior of all link directions
   of its surface (same rule; `by_surface`); without a surface tag (or too few) none: not predictable from OSM.
4. Combined = the prior and est weighted by 1/tau² and 1/se² (= the prior without passes, est without a prior).
5. Difference z = (est - prior) / √(tau² + se²).
All values are logs relative to asphalt/good (its prior = 0), so exp(value) = "x as rough as good asphalt".

Every link direction a bike may ride is in the output (one-way links in one direction only).

    venv/bin/python pipeline/estimate.py

The numbers (AGE_HALF, ...) are in config.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import LINKS, LINKS_EST, PASSES, Log
from config import AGE_HALF, AGE_POWER, LENGTH_EXP, LENGTH_REF_M, MIN_INFO, MIN_LINKS, TAU_MIN

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


def weighted(p: pd.DataFrame) -> pd.DataFrame:
    """passes (with w) -> per (link_id, direction): est, sd, information, passes"""
    e = p.assign(wv=p.w * p.value, wvv=p.w * p.value ** 2).groupby(KEY).agg(
        information=("w", "sum"), swv=("wv", "sum"), swvv=("wvv", "sum"), passes=("value", "size"))
    e["est"] = e.swv / e.information
    var = np.maximum(e.swvv / e.information - e.est ** 2, 0) * e.passes / (e.passes - 1)   # nan for one pass
    e["sd"] = np.sqrt(var.where(e.passes > 1))
    return e[["est", "sd", "information", "passes"]].reset_index()


def standard_error(e: pd.DataFrame, sd_one: float) -> pd.Series:
    return np.maximum(e.sd.fillna(0), sd_one) / np.sqrt(e.information.where(e.information > 0))


def combine(e: pd.DataFrame) -> pd.DataFrame:
    """est, se, prior, tau -> comb, comb_se, z"""
    w_p, w_e = (1 / e.tau ** 2).fillna(0), (1 / e.se ** 2).fillna(0)
    w = (w_p + w_e).where(w_p + w_e > 0)
    return pd.DataFrame({"comb": (e.prior.fillna(0) * w_p + e.est.fillna(0) * w_e) / w, "comb_se": 1 / np.sqrt(w),
                         "z": (e.est - e.prior) / np.sqrt(e.tau ** 2 + e.se ** 2)}, index=e.index)


def mad(x) -> float:
    return 1.4826 * float(np.median(np.abs(x - np.median(x))))


def priors(e: pd.DataFrame) -> pd.DataFrame:
    """per link direction: prior, tau and by_surface. From its OSM class (surface/smoothness) when both tags are
    there and the class has >= MIN_LINKS link directions of information >= MIN_INFO, else from all link directions
    of its surface
    (same rule), else none. Without a surface tag: none (a smoothness alone is too rare to learn from)"""
    tags = e.osm_class.str.split("/", expand=True)
    e = e.assign(surface=tags[0], se2=e.se ** 2)
    teach = e[e.information >= MIN_INFO]

    def per(key):
        s = teach.groupby(key).agg(n=("est", "size"), prior=("est", "median"), mad=("est", mad),
                                   noise=("se2", "median"))
        s["tau"] = np.sqrt(np.maximum(s.mad ** 2 - s.noise, TAU_MIN ** 2))
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
    cls = e[e.information >= MIN_INFO].groupby("osm_class").agg(n=("est", "size"), est=("est", "median"),
                                                        prior=("prior", "first"), tau=("tau", "first"))
    log("classes (log units, 0 = asphalt/good; x as rough = exp):\n" +
        cls[cls.n >= MIN_LINKS].sort_values("est").round(2).to_string())


if __name__ == "__main__":
    main()
