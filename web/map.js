// Roughness values are logs relative to good asphalt: exp(v) = "x as rough as good asphalt".
const ROUGH = { stops: [-0.1, 0.1, 0.3, 0.5, 0.8], colors: ["#1a9850", "#a6d96a", "#fee08b", "#fc8d59", "#d73027"],
                label: v => "×" + Math.exp(v).toFixed(1) };
const INFO = { stops: [0.1, 1, 10, 100, 1000], colors: ["#edf8fb", "#b3cde3", "#8c96c6", "#8856a7", "#810f7c"],
               label: v => v, log: true };
// lines fade with the uncertainty of the value they show (one standard error, log units):
// full up to ±15 % (well measured: the year-to-year drift alone is ±11 %), faint at ±50 % (one ride) (choice)
const FADE = { stops: [Math.log(1.15), Math.log(1.25), Math.log(1.5)], opacity: [1, 0.4, 0.12],
               label: v => "±" + Math.round((Math.exp(v) - 1) * 100) + " %" };
const MISSING = "#a8a8a8";
const MEASURED = [">", ["get", "passes"], 0];
const RATIO = "× as rough as good asphalt";
// the views, in the order the method goes: the raw rides, per street, the tags, both merged, where they disagree
const MODES = {
  rides: { name: "Rides", hint: "raw recordings", prop: "n", scale: ROUGH, layer: "rides", unit: "× the ride's median",
           note: "What it starts from: the SimRa rides, each second's shaking relative to the ride's own median (ends "
               + "cut off, parts left out)." },
  est:   { name: "Measured", hint: "from the rides", prop: "est", scale: ROUGH, filter: MEASURED, fade: "se",
           unit: RATIO,
           note: "Per street and riding direction, from all rides that passed; faint where few did." },
  prior: { name: "OSM tags", hint: "predicted", prop: "prior", scale: ROUGH, missing: "not predictable", fade: "tau",
           unit: RATIO,
           note: "What the street's OpenStreetMap surface and smoothness tags predict; faint where streets with these "
               + "tags differ a lot." },
  comb:  { name: "Combined", hint: "best guess", prop: "comb", scale: ROUGH, fade: "comb_se", unit: RATIO,
           note: "Measurement and OSM tags merged, each by how sure it is: a value for every street; faint where it "
               + "rests on the tags or few rides." },
  z:     { name: "Measured vs. OSM", hint: "disagreement", prop: "z", filter: MEASURED, fade: "se",
           scale: { stops: [-4, -2, 0, 2, 4], colors: ["#2166ac", "#67a9cf", "#d9d9d9", "#ef8a62", "#b2182b"],
                    label: v => v }, unit: "measured minus OSM, in standard errors (z)",
           note: "Blue: smoother than its tags say, red: rougher. Beyond ±2 they clearly disagree." },
  information: { name: "Data amount", hint: "how sure", prop: "information", filter: MEASURED, scale: INFO,
           unit: "information (1 ≈ one recent ride)",
           note: "How much the measurement rests on: the rides that passed, weighted by age, length, match certainty "
               + "and how much the ride scatters." },
};

// MapLibre's CSP build: its worker is a file of this site, not a blob, so the page's Content-Security-Policy can stay strict
maplibregl.setWorkerUrl("lib/maplibre-gl-csp-worker.js");
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);
const map = new maplibregl.Map({
  container: "map", style: "https://tiles.openfreemap.org/styles/positron",
  center: [13.40, 52.51], zoom: 12, hash: true,   // the start view: the middle of the current region (Berlin)
  attributionControl: { compact: true,   // the legal pages are the site's own (not in this repository)
    customAttribution: '<a href="impressum.html">Impressum</a> · <a href="privacy.html">Privacy</a>' },
});
map.addControl(new maplibregl.GeolocateControl());   // "where am I", mostly for phones

// a finger is less precise than a mouse: look for a street within a few pixels of the point, then a few more
const PHONE_PX = 600;      // narrower maps count as phones (as in the CSS): the popup opens below the street
const PICK_PX = [3, 12];   // search radii in screen pixels: a mouse click lands within ~3, a tap within ~12
function pick(pt, layer = "links") {
  for (const r of PICK_PX) {
    const f = map.queryRenderedFeatures([[pt.x - r, pt.y - r], [pt.x + r, pt.y + r]], { layers: [layer] });
    if (f.length) return f[0];
  }
}

// interpolate a property over stops (on a log10 axis if log); values beyond the ends keep the end's value
function ramp(prop, stops, outputs, log) {
  const x = ["coalesce", ["get", prop], 0];
  const e = ["interpolate", ["linear"], log ? ["log10", ["max", x, stops[0]]] : x];
  stops.forEach((v, i) => e.push(log ? Math.log10(v) : v, outputs[i]));
  return e;
}

function setMode(key) {
  const m = MODES[key], s = m.scale, layer = m.layer || "links";
  const color = ramp(m.prop, s.stops, s.colors, s.log);
  for (const l of ["links", "oneway", "rides"])
    map.setLayoutProperty(l, "visibility", (l === "rides") === (layer === "rides") ? "visible" : "none");
  if (layer === "links") {
    map.setPaintProperty("links", "line-color", m.missing ? ["case", ["has", m.prop], color, MISSING] : color);
    const show = ["all", m.missing ? true : ["has", m.prop], m.filter || true];
    map.setFilter("links", show);
    map.setFilter("oneway", ["all", ["has", "oneway"], show]);
    map.setPaintProperty("links", "line-opacity", m.fade ? ramp(m.fade, FADE.stops, FADE.opacity) : 1);
  } else map.setPaintProperty("rides", "line-color", color);
  document.querySelectorAll("#modes button").forEach(b => b.classList.toggle("on", b.dataset.k === key));
  const bar = (colors, ticks) => `<div class="bar" style="background:linear-gradient(to right,${colors.join(",")})"></div>
     <div class="ticks">${ticks.map(t => `<span>${t}</span>`).join("")}</div>`;
  document.getElementById("legend").innerHTML = `<div class="unit">${m.unit}</div>` + bar(s.colors, s.stops.map(s.label))
    + (m.missing ? `<div class="grey"><span style="display:inline-block;width:18px;height:6px;background:${MISSING};margin-right:4px"></span>${m.missing}</div>` : "")
    + (m.fade ? `<div class="fade">${bar(FADE.opacity.map(o => `rgba(60,60,60,${o})`), FADE.stops.map(FADE.label))}
                 <div class="grey">faint: unsure (± one standard error)</div></div>` : "");
  document.getElementById("note").textContent = m.note;
  try { localStorage.setItem("mode", key); } catch (e) {}
}

for (const [k, m] of Object.entries(MODES)) {
  const b = document.createElement("button");
  b.innerHTML = `${m.name} <span class="hint">${m.hint}</span>`; b.dataset.k = k; b.onclick = () => setMode(k);
  document.getElementById("modes").appendChild(b);
}
// windows: links with data-open open one; its backdrop, a data-close button or Escape closes it
// (an open window makes the page itself scroll: phone browsers scroll a page to its end, a scrolling box not always)
function show(id, on) {
  window.scrollTo(0, 0);   // first: once the page can't scroll, some browsers keep the old position
  document.getElementById(id).style.display = on ? "block" : "none";
  document.documentElement.classList.toggle("reading", [...document.querySelectorAll(".window")]
    .some(w => w.style.display === "block"));
}
document.querySelectorAll("[data-open]").forEach(a => a.onclick = ev => {
  ev.preventDefault(); document.querySelectorAll(".window").forEach(w => show(w.id, false)); show(a.dataset.open, true);
});
document.querySelectorAll(".window").forEach(w => w.onclick = ev => {
  if (ev.target === w || ev.target.matches("[data-close]")) show(w.id, false);
});
document.addEventListener("keydown", ev => {
  if (ev.key === "Escape") document.querySelectorAll(".window").forEach(w => show(w.id, false));
});
// the address is put together here, so bots that collect addresses from the page source don't find it
const mail = ["info", "velosense.org"].join("@");
document.getElementById("mail").innerHTML = `<a href="mailto:${mail}">${mail}</a>`;
// the about window welcomes first-time visitors
let welcomed = false;
try { welcomed = localStorage.getItem("welcomed") === "1"; localStorage.setItem("welcomed", "1"); } catch (e) {}
if (!welcomed) show("about", true);

map.on("load", () => {
  // phones: the credits start folded into their (i) button, as they fold anyway once the map is moved
  if (map.getContainer().clientWidth < PHONE_PX)
    document.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
  const attribution = 'Rides: <a href="https://github.com/simra-project/dataset" target="_blank" rel="noopener">SimRa</a> '
    + '(<a href="https://creativecommons.org/licenses/by-nc/4.0/" target="_blank" rel="noopener">CC BY-NC 4.0</a>)';
  const tiles = file => "pmtiles://" + new URL("data/" + file, location.href).href;
  map.addSource("links", { type: "vector", url: tiles("roughness.pmtiles"), attribution });
  map.addLayer({
    id: "links", type: "line", source: "links", "source-layer": "links",
    layout: { "line-cap": "round" },
    paint: {
      "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1, 15, 3.5, 18, 6],
      "line-offset": ["interpolate", ["linear"], ["zoom"], 11, 0.5, 15, 2.5, 18, 5],
    },
  });
  // the Rides view: the rides' seconds (zoomed out, 10 s per line), drawn thin
  map.addSource("rides", { type: "vector", url: tiles("rides.pmtiles"), attribution });
  map.addLayer({
    id: "rides", type: "line", source: "rides", "source-layer": "rides", layout: { "line-cap": "round" },
    paint: { "line-width": ["interpolate", ["linear"], ["zoom"], 11, 0.8, 15, 2, 18, 4], "line-opacity": 0.85 },
  });
  // one-way arrows: the features run in the direction of travel; shifted right like the line (line-offset)
  map.addLayer({
    id: "oneway", type: "symbol", source: "links", "source-layer": "links", minzoom: 16,
    filter: ["has", "oneway"],
    layout: {
      "symbol-placement": "line", "symbol-spacing": 70, "text-field": "→", "text-font": ["Noto Sans Regular"],
      "text-size": 14, "text-keep-upright": false, "text-rotation-alignment": "map", "text-allow-overlap": true,
      "text-offset": ["interpolate", ["linear"], ["zoom"], 16, ["literal", [0, 0.25]], 18, ["literal", [0, 0.35]]],
    },
    paint: { "text-color": "#333", "text-halo-color": "#fff", "text-halo-width": 1.2 },
  });
  let mode = "est";
  try { mode = localStorage.getItem("mode") || mode; } catch (e) {}
  setMode(MODES[mode] ? mode : "est");

  for (const l of ["links", "rides"]) {
    map.on("mouseenter", l, () => map.getCanvas().style.cursor = "pointer");
    map.on("mouseleave", l, () => map.getCanvas().style.cursor = "");
  }
  map.on("click", ev => {
    if (map.getLayoutProperty("rides", "visibility") === "visible") {
      const r = pick(ev.point, "rides");
      if (r) new maplibregl.Popup().setLngLat(ev.lngLat).setHTML(`A piece of a SimRa ride: <b>×${
        Math.exp(r.properties.n).toFixed(2)}</b> <span class="grey">the ride's median shaking</span>`).addTo(map);
      return;
    }
    const f = pick(ev.point);
    if (!f) return;
    // OSM names and tags are written by anyone: escape them before they go into the popup's HTML
    const esc = v => String(v ?? "").replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
    const p = Object.fromEntries(Object.entries(f.properties).map(([k, v]) => [k, typeof v === "string" ? esc(v) : v]));
    // ×ratio ± its uncertainty in the same units (exp(v) x se: the log's standard error carried over)
    const rough = (v, se) => v === undefined ? "–" : "×" + Math.exp(v).toFixed(2)
      + (se === undefined ? "" : ` <span class="grey">± ${(Math.exp(v) * se).toFixed(2)}</span>`);
    const grey = t => ` <span class="grey">${t}</span>`;
    const rows = [
      ["street", (p.name || "") + grey(p.hw)],
      ["direction", p.dir > 0 ? "as drawn" : "against drawn"],
      ["measured", rough(p.est, p.se)],
      ["OSM tags", p.prior === undefined ? "not predictable" : rough(p.prior, p.tau) + (p.bysurf ? grey(`all ${p.surface}`) : "")],
      ["combined", rough(p.comb, p.comb_se)],
      ["measured vs. OSM", p.z === undefined ? "–" : "z = " + p.z.toFixed(1)],
      ["passes", p.passes + grey("rides on this segment")],
      ["data amount", (p.information === undefined ? 0 : p.information.toFixed(1)) + grey("information")],
      ["OSM class", `${p.surface || "–"} / ${p.smoothness || "–"}` + grey("surface / smoothness for bikes")],
      ["OSM way", `<a href="https://www.openstreetmap.org/way/${p.way}" target="_blank" rel="noopener">${p.way}</a>`],
    ];
    // on a phone the popup is as wide as the screen: move the street to the top middle, the popup goes below it
    const phone = map.getContainer().clientWidth < PHONE_PX;
    if (phone) map.easeTo({ center: ev.lngLat, offset: [0, -map.getContainer().clientHeight / 4] });
    new maplibregl.Popup({ maxWidth: "min(360px, calc(100vw - 20px))", anchor: phone ? "top" : undefined })
      .setLngLat(ev.lngLat)
      .setHTML("<table>" + rows.map(r => `<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join("") + "</table>")
      .addTo(map);
  });
});
