# Open data

Two downloads from the map's *Data* window (or `pipeline/export.py` after the pipeline), for routers, cities,
mappers and anyone who wants to work with the values.

> **A demo, not a final result.** The values may contain errors and biases: rides matched to the wrong one of a road
> and its cycle track, few rides on a segment, slow streets and short segments reading somewhat rougher. The method
> and its calibration may still change. Check the values before you build on them, and use `information` to weigh
> them.

## roughness.csv.gz

One row per measured street segment and riding direction (a segment is a piece of an OSM way between junctions).
Roughness is **"× as rough as good asphalt"**: ×1.5 shakes a bike 1.5 times as much as a street tagged
`surface=asphalt, smoothness=good` at the same speed. A `_pm` column is the value's uncertainty in the same units
(one standard error).

| column | |
|---|---|
| `way_id` | the OSM way |
| `from_node`, `to_node` | the OSM nodes the segment runs between, in riding direction |
| `direction` | `as drawn` or `against drawn`: the riding direction relative to the way's drawing direction |
| `name`, `highway`, `surface`, `smoothness` | OSM tags (`surface:bicycle` / `smoothness:bicycle` where the way has them) |
| `length_m` | the segment's length |
| `passes` | rides that passed it in this direction |
| `information` | how much the measurement rests on: the passes weighted by age, length, match certainty and how much their ride scatters (1 ≈ one recent, clearly matched 20 m pass of a typical ride) |
| `measured`, `measured_pm` | roughness measured from the rides; its ± is about today, so it includes how much a street changes from year to year (never below about ±11 %) |
| `osm`, `osm_pm` | what the OSM tags predict (empty: not predictable from the tags); its ± is how much streets with these tags differ |
| `combined`, `combined_pm` | both merged, each weighted by how sure it is |
| `z` | measured minus OSM, in standard errors (without the year-to-year change: both describe the same years): beyond ±2 they clearly disagree |
| `geometry` | the segment's line in riding direction (WKT, WGS 84) |

How each value is calculated: [how_it_works.md](how_it_works.md).

```python
import pandas as pd
d = pd.read_csv("roughness.csv.gz")
sure = d[d.information >= 3]                  # about three recent rides or more
```

In QGIS: *Layer → Add Layer → Add Delimited Text Layer*, geometry definition *Well known text (WKT)*, column
`geometry`, CRS EPSG:4326.

## tag_roughness.csv

The roughness the map's *OSM tags* view gives a street, per OSM `surface` and `smoothness` tag pair: the median of the
well-measured segments with that pair. A router can use it as a cost table where no ride has passed.

| column | |
|---|---|
| `surface`, `smoothness` | the tag pair; smoothness `*`: the surface alone (any other or no smoothness tag); empty: the tag is missing |
| `osm`, `osm_pm` | its roughness, and how much the streets with these tags really differ |
| `measured_from` | the well-measured segments (information ≥ 3) the value is the median of |
| `link_directions` | the segments and riding directions it applies to |

How much the streets within one tag pair differ: [same_tags.md](same_tags.md).

## Licence

The values come from the [SimRa rides](https://github.com/simra-project/dataset) and from OpenStreetMap and keep
their terms:

* SimRa: [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) and SimRa's terms of use: credit SimRa, no
  commercial use, and no attempt to identify riders or their movement profiles. If you publish the data again, the
  same terms must hold for your copy.
* OpenStreetMap: [ODbL](https://www.openstreetmap.org/copyright): credit "© OpenStreetMap contributors", share-alike.

The values are averages per street segment and direction; they hold no ride, time or route.
