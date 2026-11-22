"""Every number the pipeline uses, in one place, with its unit and where it comes from.

    fitted:  measured on the data by calibrate.py, which writes them to calibration.json
    choice:  a decision, with the reason
    checked: a decision that checks.py tests
The map's colour scales are in web/map.js (display only).
"""
import json
from pathlib import Path

CALIBRATION = Path(__file__).resolve().parent / "calibration.json"
FITTED = json.loads(CALIBRATION.read_text())     # written by calibrate.py

# ---- region: where the rides are from (now Berlin, the SimRa rides); another region needs only these three ---------
REGION = "Berlin"       # the region's name in the SimRa archives: <REGION>_*.zip with <REGION>/Rides/... inside
BBOX = (52.33, 52.68, 13.08, 13.77)  # lat_lo, lat_hi, lon_lo, lon_hi: the region's bounding box
LAT0 = 52.5             # degrees: the latitude the local metric projection is centred on (the region's middle)

# ---- simra.py: ride seconds ------------------------------------------------------------------------------------------
IMU_HZ = 4              # accelerometer rows per second in SimRa files (format)
MAX_FIX_GAP_S = 10.0    # s: GPS fixes come every ~3 s; a longer gap is lost signal: no position (choice)
HALF_S = 1.5            # s: speed and heading from the positions 1.5 s before and after (one fix interval, choice)
MIN_MOVE_M = 0.5        # m: moved less within those 3 s: no heading (choice)
GRAVITY_S = 31          # s: gravity = the phone's median direction over half a minute; a bike's tilts average out
SWELL_S = 4             # s: RMS window of the vertical acceleration, as SimRa's own roughness value
HANDLED_DEG = 30.0      # °: the phone points this far from gravity: in a hand or swinging, not on the bike (choice)
TURN_DEG = 30.0         # °: gravity turns this much within GRAVITY_S: the phone was moved (choice)
HANDLED_PAD_S = 5       # s: also dropped on either side of a handled second (choice)
MIN_SPEED = 2.0         # m/s: slower is walking or standing, and the speed correction grows large (choice)
MAX_SPEED = 15.0        # m/s (54 km/h): faster is a GPS jump (choice)
MAX_H_ACC = 20.0        # m: a worse GPS fix can't tell a street from its neighbour (choice)
SPEED_EXP = FITTED["SPEED_EXP"]   # vibration ~ speed^SPEED_EXP (fitted, about the square root)
MIN_KEPT = 60           # s: a ride needs a minute of kept seconds; its median is the normalization (choice)
MAX_SPAN_S = 6 * 3600   # s: a longer ride has clock jumps: rejected
WORKERS = 8             # parallel processes reading the archives

# ---- match.py: rides onto links --------------------------------------------------------------------------------------
SAMPLE_M = 5.0          # m: links sampled every 5 m, so the nearest sample is at most 2.5 m off the line
K = 48                  # nearest samples looked at per second: enough for all lines within R_MAX
R_MIN, R_MAX = 15.0, 30.0  # m: candidates within 2 x h_acc, at least a street's width, at most 30 m (choice)
SIGMA_MIN = 5.0         # m: the GPS error in the cost is never below this; h_acc can be optimistic (choice)
MAX_DEG = 35.0          # °: the ride's heading within this of a link's direction: bends, heading noise (choice)
PATH_BESIDE_COST = 1.0  # extra cost on a road whose bikes ride on a separate path (= ~1.4 GPS errors farther away)
FOOT_COST = 1.0         # the same on a footway without bike access
C = 6                   # candidate link directions kept per second
NONE_COST = 6.0         # cost of a second off the network (= a candidate ~3.5 GPS errors away)
JUMP_COST = 6.0         # cost of moving to a link direction that doesn't touch the last one (half of it on / off
                        # the network): GPS drifting to a parallel line for a few seconds doesn't move the ride
SKIP_M = 30.0           # m: a link shorter than this may be skipped between two touching ones (junctions)
FREE_GAP_S = 5          # s: after a longer gap between seconds any move is free
MIN_MATCHED = 0.7       # a ride with less of its seconds matched gives no passes: off the network or broken GPS
MAX_GAP_S = 20          # s: a longer gap between seconds on the same link starts a new pass
CHUNK = 200_000         # seconds per query (memory: CHUNK x K x 16 bytes)
SMOOTH_M = 60.0         # m: a second's value = the mean of its ride within ±30 m: a short link takes the street's
                        # value around it, not just a kerb on its end (choice: street scale; against no averaging
                        # the split-half r and the agreement with the OSM tags rise, little more beyond 60 m)

# ---- estimate.py: one value per link direction -----------------------------------------------------------------------
AGE_HALF = 3.0          # years: a pass this old counts half (choice: at 5 years x0.11, old passes barely count;
                        # fitted: 4, but the data only reaches 3.3 years back)
AGE_POWER = 4           # how sharply the age weight drops around AGE_HALF: nearly full for a year (choice)
LENGTH_REF_M = 20.0     # m: a pass this long counts 1
LENGTH_EXP = FITTED["LENGTH_EXP"] # pass weight ~ (metres / LENGTH_REF_M)^LENGTH_EXP (fitted)
MIN_INFO = 3.0          # information a link direction needs to teach the OSM prior: about 3 new 20 m passes (choice)
MIN_LINKS = 20          # a class needs this many such link directions for a prior: a stable median (choice)
TAU_MIN = 0.10          # the prior's uncertainty never below this: x1.1 (choice, for small classes)

# ---- tiles.py --------------------------------------------------------------------------------------------------------
MIN_ZOOM, MAX_ZOOM = 10, 15  # tile zooms (the map zooms in further on the zoom 15 tiles)
MINOR_ZOOM = 13         # minor ways without passes show from this zoom on: keeps the low zooms light
RIDE_SAMPLE = 1000      # rides drawn in the map's Rides view: enough to see what the measurement starts from, few
                        # enough for light tiles (choice; all ~22,000 would be ~22 million ride seconds)
RIDE_SEED = 1           # the order rides are offered in: a fixed seed, so every run draws the same ones (any number)
RIDE_RANDOM = 500       # the first this many as they come (a random sample: busy streets show as busy), then ...
RIDE_FULL = 2           # spread over the streets: a street segment counts as full once this many drawn rides pass it ...
RIDE_NEW_SHARE = 0.5    # ... and a ride is drawn only if at least this share of its matched length is on segments not
                        # yet full (choices: vs. a plain random sample of 1,000, 2.4 x the street segments with few
                        # rides show, and busy segments still carry 8 drawn rides on average instead of 16)
RIDE_CUT_M = 500.0      # m: left out at both ends of a drawn ride, so no start or end (home, work) shows (choice)
RIDE_PIECE_M = 500.0    # m: a drawn ride is cut into pieces this long and every other one is left out, so the
                        # pieces can't be followed into one ride (choice; the SimRa terms forbid tracing riders)

# ---- checks.py and calibrate.py --------------------------------------------------------------------------------------
NEWEST_MONTHS = 12      # check 3 and the age curve hold out the newest year
CHECK_LEVELS = (1, 3, 10)  # check 2: information in both halves; check 3: number of older passes
CHECK_MIN_PASSES = 10   # check 1: link directions with this many passes
CAL_MIN_PASSES = 20     # calibrate: a reference mean needs this many other passes (its own noise is then small)
CAL_MIN_NEW = 10        # calibrate, age: this many newest-year passes (fewer exist per link direction)
CAL_SPEED_MIN_S = 5     # calibrate, speed: passes of at least 5 s (a steady speed)

# ---- figures.py: the spread within one tag pair (docs/same_tags.md) --------------------------------------------------
SURE_INFO = 10.0        # only well-measured link directions: about ten new 20 m passes (choice)
SURE_SE = 0.08          # and a standard error of at most x1.08: small against the spread shown (choice)
SURE_M = 30.0           # m: and at least this long, away from the junction effect of short links (choice)
