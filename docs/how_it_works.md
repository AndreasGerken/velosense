# How it works

The technical side of the Velosense roughness map. Install, run and known issues: [README](../README.md).

## Pipeline

1. **Segments** (`osm.py`): the ways bikes may ride, split at junctions. Cycle tracks stay separate from their road.
   Each segment's OSM class is its tag pair `<surface>/<smoothness>`, or `surface:bicycle` / `smoothness:bicycle`
   where a road has its own surface for bikes.
2. **Ride seconds** (`simra.py`):
   * The value per second is the vertical shaking (RMS of the acceleration along gravity over 4 s), corrected
     for speed: `log(shaking) − 0.5·log(speed)`.
   * Then the ride's median is subtracted, which removes phone, mount, bike and rider.
   * Seconds where the phone was handled, the bike was slower than 2 m/s or the GPS was worse than 20 m are
     dropped.
   * Rides from the iOS app (about 10 %) are left out: their accelerometer values are on another scale, and adding
     them made the estimates agree less with held-out Android rides.
3. **Matching** (`match.py`):
   * A hidden Markov model puts each second on a segment. It prefers routes along connected segments, so GPS
     drifting to a parallel street doesn't move the ride there.
   * Each second is averaged with its ride's seconds within ±30 m, so a short segment gets the street around it,
     not just the kerb at its end.
   * A **pass** is one ride's seconds on one segment and direction; its value is their median.
4. **Estimate** (`estimate.py`):
   * The estimate is the weighted mean of the passes.
   * A pass's weight is **age × length × match certainty**:
     * it counts half at 3 years old and ×0.1 at 5;
     * it counts a little more the longer it is: `(metres / 20)^0.14`;
     * it counts less when a parallel line was just as close.
   * **Information** is the sum of the weights (the map's *Data amount* view).
5. **OSM prediction**: the median estimate of the segments with the same tag pair, or of the same surface when the
   pair is rare or has no smoothness tag. There is none without a surface tag. **Combined** weights the
   prediction and the estimate by their uncertainties. **Rides vs. OSM** is their gap in standard errors (z).
6. **Tiles** (`tiles.py`): segments and values -> `web/data/roughness.pmtiles`, which `web/map.html` (with `map.js`)
   shows. The *Rides* view, `web/data/rides.pmtiles`: 1,000 rides, each second's value minus its ride's median: half
   a random sample (busy streets show as busy), half chosen so quiet streets show too (a ride is drawn if at least half
   of it runs on segments fewer than two drawn rides pass). So that no rider can be traced (SimRa's terms), the first
   and last 500 m of every ride and every other 500 m piece are left out, and no times or ride ids are kept.
7. **Downloads** (`export.py`): the measured segments and the roughness per tag pair as CSV, see [data.md](data.md).

All values are logs relative to good asphalt. Good asphalt is also the typical street: the median over the measured
street length is ×1.00.

## Where the numbers come from

Every number is in [`config.py`](../pipeline/config.py), with its unit and its reason. The fitted ones (speed and length
exponents) are written to `calibration.json` by `calibrate.py`. The speed exponent is used before the passes
exist, so it comes from the previous run; it is stable at 0.5.

## Checks

`checks.py` tests the result:

* **Normalization:** removing the ride median shrinks the spread between passes from 0.59 to 0.31.
* **Reliability:** estimates from two halves of the rides correlate at r = 0.84 (information ≥ 3).
* **Weighting:** the weight given to the measurement vs the OSM prediction matches the best weight on held-out
  newest-year passes (0.34 vs 0.37 at three older passes).

How much streets with the same tags differ: [Same tags, different roughness](same_tags.md), drawn by `figures.py`.
