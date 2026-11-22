"""Figures for looking at the results; a notebook docs/FIGURES_<topic>.ipynb (code + the rendered figure)
or a page in docs/.

    venv/bin/python pipeline/figures.py   # -> docs/FIGURES_osm_classes.ipynb, docs/same_tags.md + docs/same_tags.png
"""
from __future__ import annotations

import base64
import inspect
import io

import matplotlib
import nbformat as nbf
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from common import LINKS, LINKS_EST, ROOT  # noqa: E402
from config import DRIFT, MIN_INFO, SURE_INFO, SURE_M, SURE_SE  # noqa: E402

BLUE, ORANGE, INK, MUTED = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e"


SMOOTHNESS = ["excellent", "good", "intermediate", "bad", "very_bad", "horrible", "very_horrible", "impassable", "—"]


def osm_classes(top: int = 24, min_measured: int = 10):
    """predicted (OSM prior) vs measured roughness for the most common surface x smoothness tag classes with
    data (>= min_measured link directions of information >= MIN_INFO and a prior), one block per surface"""
    links = pd.read_parquet(LINKS, columns=["link_id", "length", "surface", "smoothness"])
    est = pd.read_parquet(LINKS_EST).merge(links, on="link_id")
    est[["surface", "smoothness"]] = est[["surface", "smoothness"]].fillna("—")
    key = ["surface", "smoothness"]
    measured = est[est.information >= MIN_INFO].groupby(key).est
    t = pd.DataFrame({"km": est.groupby(key).length.sum() / 2000,      # two directions per two-way link
                      "prior": est.groupby(key).prior.median(), "n": measured.size(),
                      "q25": measured.quantile(0.25), "med": measured.median(), "q75": measured.quantile(0.75)})
    t = t[(t.n >= min_measured) & t.prior.notna()].sort_values("km", ascending=False).head(top).reset_index()
    t[["prior", "q25", "med", "q75"]] = np.exp(t[["prior", "q25", "med", "q75"]])     # x as rough as good asphalt
    t["first"] = t.groupby("surface").prior.transform("min")                          # blocks: smoothest first
    t["order"] = t.smoothness.map({s: i for i, s in enumerate(SMOOTHNESS)}).fillna(len(SMOOTHNESS) - 1.5)
    t = t.sort_values(["first", "surface", "order"])

    y, labels, heads, rows = 0, [], [], []
    for surface, g in t.groupby("surface", sort=False):
        heads.append(y)
        labels.append(surface if surface != "—" else "no surface tag")
        for sm in g.smoothness:
            y += 1
            rows.append(y)
            labels.append(sm if sm != "—" else "no smoothness tag")
        y += 1.4
    t["y"] = rows

    fig, ax = plt.subplots(figsize=(8, 0.27 * y + 1.6))
    ax.hlines(t.y, t.q25, t.q75, color=BLUE, alpha=0.35, lw=6,
              label=f"measured: middle 50 % of links (information ≥ {MIN_INFO:g})")
    ax.plot(t.med, t.y, "o", color=BLUE, ms=6, label="measured: median")
    ax.plot(t.prior, t.y, "D", color=ORANGE, ms=6, mec="white", mew=1.2, label="predicted from OSM (class prior)")
    ax.axvline(1, color=MUTED, lw=0.8, ls=":")
    for h in heads[1:]:
        ax.axhline(h - 0.7, color="#d6d4cd", lw=0.6)
    ax.set_yticks(heads + rows, [labels[i] for i in np.argsort(np.argsort(heads + rows))])
    for lbl in ax.get_yticklabels():
        if lbl.get_position()[1] in heads:
            lbl.set(fontweight="bold", color=INK)
    for r in t.itertuples():
        ax.annotate(f"{r.km:,.0f} km, {r.n:,.0f} measured", (1.0, r.y), xycoords=("axes fraction", "data"),
                    xytext=(6, 0), textcoords="offset points", va="center", fontsize=8, color=MUTED)
    ax.set_ylim(y - 0.9, -0.8)
    ax.set_xscale("log")
    ticks = [0.8, 1, 1.25, 1.5, 2, 2.5]
    ax.set_xticks(ticks, [f"×{v:g}" for v in ticks])
    ax.minorticks_off()
    ax.set_xlabel("roughness, × as rough as good asphalt (log scale)", color=INK)
    ax.set_title("OSM surface · smoothness: predicted vs measured", color=INK, loc="left", fontsize=11)
    ax.grid(axis="x", color="#e5e4df", lw=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=MUTED, length=0)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.6 / (0.27 * y + 1.6)), ncol=3, frameon=False, fontsize=8)
    fig.tight_layout()
    return fig, t.set_index(key)[["km", "prior", "n", "q25", "med", "q75"]]


def same_tags(pairs: int = 6, min_links: int = 50):
    """measured roughness of the well-measured link directions (SURE_*) in the most common surface/smoothness tag
    pairs (>= min_links each), one direction per link (the better measured); sorted by the OSM value"""
    links = pd.read_parquet(LINKS, columns=["link_id", "length", "surface", "smoothness"])
    e = pd.read_parquet(LINKS_EST).merge(links, on="link_id")
    noise = np.sqrt(e.se ** 2 - DRIFT ** 2)                  # the spread at one time: without the drift
    e = e[(e.information >= SURE_INFO) & (noise <= SURE_SE) & (e.length >= SURE_M)
          & e.surface.notna() & e.smoothness.notna()]
    e = e.sort_values("information", ascending=False).drop_duplicates("link_id")
    e["pair"] = e.surface + "/" + e.smoothness
    q = lambda p: lambda s: np.exp(s.quantile(p))
    t = e.groupby("pair").agg(n=("est", "size"), median=("est", q(0.5)), p10=("est", q(0.1)), p90=("est", q(0.9)),
                              lowest=("est", q(0)), highest=("est", q(1)), prior=("prior", q(0.5)))
    t = t[t.n >= min_links].sort_values("n", ascending=False).head(pairs).sort_values("prior")   # smoothest first

    fig, ax = plt.subplots(figsize=(8, 0.36 * len(t) + 1.0))
    rng = np.random.default_rng(0)
    for y, (pair, r) in enumerate(t.iterrows()):
        x = np.exp(e[e.pair == pair].est)
        ax.scatter(x, y + rng.uniform(-0.3, 0.3, len(x)), s=2.5, color=BLUE, alpha=0.25, lw=0,
                   label="one street segment" if y == 0 else None)
        ax.hlines(y, r.p10, r.p90, color=INK, lw=1.6, label="middle 80 %" if y == 0 else None)
        ax.plot(r["median"], y, "|", color=INK, ms=10, mew=2, label="median" if y == 0 else None)
        ax.plot(r.prior, y - 0.42, "v", color=ORANGE, ms=6, mec="white", mew=0.8,
                label="the map's OSM value" if y == 0 else None)
        ax.annotate(f"{r.n:,.0f}", (1.0, y), xycoords=("axes fraction", "data"), xytext=(6, 0),
                    textcoords="offset points", va="center", fontsize=8, color=MUTED)
    ax.set_yticks(range(len(t)), t.index)
    ax.set_ylim(len(t) - 0.5, -0.8)
    ax.set_xscale("log")
    ticks = [0.5, 0.75, 1, 1.5, 2, 3]
    ax.set_xticks(ticks, [f"×{v:g}" for v in ticks])
    ax.minorticks_off()
    ax.set_xlabel("roughness, × as rough as good asphalt (log scale)", color=INK)
    ax.set_title("Same OSM tags, different roughness", color=INK, loc="left", fontsize=11)
    ax.grid(axis="x", color="#e5e4df", lw=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=MUTED, length=0)
    leg = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.75 / (0.36 * len(t) + 1.0)), ncol=4, frameon=False,
                    fontsize=8, markerscale=1)
    for h in leg.legend_handles:
        h.set_alpha(1)
    fig.tight_layout()
    return fig, t


def same_tags_page():
    """docs/same_tags.png and docs/same_tags.md: the figure and the table"""
    fig, t = same_tags()
    fig.savefig(ROOT / "docs/same_tags.png", dpi=130, bbox_inches="tight")
    g = t.loc["asphalt/good"] if "asphalt/good" in t.index else t.iloc[0]
    md = [f"""# Same tags, different roughness

OpenStreetMap's `smoothness` tag sorts streets into a few classes (`excellent`, `good`, `intermediate`, ...), each
judged by a mapper. The rides measure every street on one scale. Within one pair of `surface` and `smoothness` tags
the measured streets spread widely: among the {g.n:,.0f} well-measured segments tagged `{g.name}`, the middle 80 %
range from ×{g.p10:.2f} to ×{g.p90:.2f}, the extremes from ×{g.lowest:.2f} to ×{g.highest:.2f}. The map's OSM view
gives each of them the same value.

> **A proof of concept, not a final result.** The numbers here are not exact yet: parts of the method are still open
> (matching where a road, its cycle track and a footway lie close together; cycle tracks mapped only as tags on their
> road; slow streets and short segments reading somewhat rougher), and its calibration may still change. They show
> that the method works and that streets with the same tags differ; the exact ranges and the values of single streets
> may still change.

![Measured roughness per OSM tag pair: one dot per street segment](same_tags.png)

Shown are only well-measured segments: information ≥ {SURE_INFO:g} (about ten new rides), a standard error of at most
×{1 + SURE_SE:g} (small against the spread, so the spread is the streets', not noise), at least {SURE_M:g} m long
(short segments near junctions read rougher), tagged with both `surface` and `smoothness`; one riding direction per
segment, the better measured one. The tag pairs are the six with the most such segments, sorted by the map's OSM
value, smoothest first. Right: the number of segments.

| tag pair | segments | median | middle 80 % | lowest | highest | OSM value on the map |
|---|---|---|---|---|---|---|"""]
    for pair, r in t.iterrows():
        md.append(f"| `{pair}` | {r.n:,.0f} | ×{r['median']:.2f} | ×{r.p10:.2f} – ×{r.p90:.2f} | ×{r.lowest:.2f} | "
                  f"×{r.highest:.2f} | ×{r.prior:.2f} |")
    md.append("""
This doesn't make the tags wrong: a class is a range, and the measurement places each street within it, on a scale
that compares streets and cities. An extreme value is a hint to look again: the street may have changed since it was
tagged, or a ride was matched to the wrong way (a road and its cycle track side by side).""")
    (ROOT / "docs/same_tags.md").write_text("\n".join(md) + "\n")
    print(f"-> {ROOT / 'docs/same_tags.md'}")


def notebook(name: str, func, text: str):
    """a notebook with the text, the function's code and its figure + table as outputs"""
    fig, table = func()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    png = base64.b64encode(buf.getvalue()).decode()
    code = nbf.v4.new_code_cell(f"import sys\nsys.path.insert(0, \"../pipeline\")\nfrom figures import *\n\n{inspect.getsource(func)}\nfig, table = {func.__name__}()\ntable")
    code.outputs = [nbf.v4.new_output("display_data", data={"image/png": png, "text/plain": "<Figure>"}),
                    nbf.v4.new_output("execute_result", data={"text/plain": table.round(2).to_string()},
                                      execution_count=1)]
    code.execution_count = 1
    nb = nbf.v4.new_notebook(cells=[nbf.v4.new_markdown_cell(text), code])
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3 (venv)", "language": "python"}
    nbf.write(nb, ROOT / f"docs/FIGURES_{name}.ipynb")
    print(f"-> {ROOT / f'docs/FIGURES_{name}.ipynb'}")


if __name__ == "__main__":
    same_tags_page()
    notebook("osm_classes", osm_classes, """# OSM classes: predicted vs measured roughness

One block per `surface` value, its `smoothness` values below. The **prior** (orange) is what the map's *OSM* mode
shows: per class (one per surface x smoothness tag pair) the median measured roughness of its link directions with
information ≥ 3 (estimate.py). A class with fewer than 20 such link directions takes the prior of all links of its
surface (the diamond then shows that), and with fewer than 20 on the surface too it isn't predictable (grey on the
map). Shown: the 24 most common combinations (by length) with at least 10
measured link directions. Right: km of links with this combination, and how many link directions are measured
well enough (information ≥ 3).""")
