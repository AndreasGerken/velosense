# How it works

The technical side of the Velosense roughness map. Install, run and known issues: [README](../README.md).

## Pipeline

1. **Segments** (`osm.py`): the ways bikes may ride, split at junctions. Cycle tracks stay separate from their road.
   Each segment's OSM class is its tag pair `<surface>/<smoothness>`, or `surface:bicycle` / `smoothness:bicycle`
   where a road has its own surface for bikes.
2. **Ride seconds** (`simra.py`):
   * The value per second is the vertical shaking (RMS of the acceleration along gravity over 4 s), corrected
     for speed: `log(shaking) − 0.5·log(speed)`.
   * Then the ride's median is subtracted, which removes phone, mount, bike and rider (what it leaves: step 4).
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
   * **Each ride is compared with the other rides on its streets** (each pass against the other passes on its
     segment):
     * **offset**: a ride that reads rougher or smoother than the others all along is shifted back. Subtracting the
       ride's median leaves such an offset, e.g. a ride over much sett reads too smooth everywhere. The offsets spread
       by 0.08; each ride's is pulled toward 0 according to how many passes it has.
     * **noise**: a ride whose values scatter more counts less (inverse variance; the middle 80 % of passes weigh
       ×0.5 to ×1.7).

     This makes two halves of the rides agree better (see *Checks*).
   * **Information** is the sum of the weights (the map's *Data amount* view).
   * **Uncertainty** (the ± in the popup, one standard error): the spread of the passes over √information, plus a
     **drift**: a street measured in the older years differs from the newest year by ±0.11 beyond that noise
     (resurfaced, worn, other riders and phones), however many rides there were. So even a busy street is never
     surer than about ±11 % for today. Two halves of the rides from the same years differ by less than their ± says:
     the extra is the time.
5. **OSM prediction**: the median estimate of the segments with the same tag pair, or of the same surface when the
   pair is rare or has no smoothness tag. There is none without a surface tag. Its uncertainty is how much these
   segments really differ (their spread minus the measurement noise), read from their middle 80 %: same-tag streets
   have more outliers than a normal distribution, and the middle half alone would hide them. **Combined** weights the
   prediction and the estimate by their uncertainties (its ± follows from both). **Measured vs. OSM** is their gap in
   standard errors (z), without the drift (both describe the same years).
   The map fades every view by the uncertainty of the value it shows.
6. **Tiles** (`tiles.py`): segments and values -> `web/data/roughness.pmtiles`, which `web/map.html` (with `map.js`)
   shows. The *Rides* view, `web/data/rides.pmtiles`: 1,000 rides, each second's value minus its ride's median: half
   a random sample (busy streets show as busy), half chosen so quiet streets show too (a ride is drawn if at least half
   of it runs on segments fewer than two drawn rides pass). So that no rider can be traced (SimRa's terms), the first
   and last 500 m of every ride and every other 500 m piece are left out, and no times or ride ids are kept.
7. **Downloads** (`export.py`): the measured segments and the roughness per tag pair as CSV, see [data.md](data.md).

All values are logs relative to good asphalt. Good asphalt is also the typical street: the median over the measured
street length is ×1.02.

## Where the numbers come from

Every number is in [`config.py`](../pipeline/config.py), with its unit and its reason. The fitted ones (speed and length
exponents and the drift) are written to `calibration.json` by `calibrate.py`. The speed exponent is used before the
passes exist, so it comes from the previous run; it is stable at 0.5.

## Checks

`checks.py` tests the result:

* **Normalization:** removing the ride median shrinks the spread between passes from 0.59 to 0.31.
* **Reliability:** estimates from two halves of the rides correlate at r = 0.86 (information ≥ 3; 0.84 without
  comparing the rides with each other).
* **Weighting:** the weight given to the measurement vs the OSM prediction matches the best weight on held-out
  newest-year passes (0.20 / 0.36 / 0.58 vs 0.18 / 0.41 / 0.61 at one / three / ten older passes).
* **Uncertainty of the OSM prediction:** on well-measured segments, 5.8 % lie more than two standard errors from
  their prediction (a normal distribution: 4.6 %; with the spread read from the middle half: 7.4 %).

How much streets with the same tags differ: [Same tags, different roughness](same_tags.md), drawn by `figures.py`.
