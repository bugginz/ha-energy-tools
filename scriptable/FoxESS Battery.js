// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: green; icon-glyph: battery-three-quarters;

// FoxESS battery widget for Scriptable (2026-07-31)
//
// Lock Screen: accessoryCircular (gauge ring), accessoryRectangular (3 lines),
// accessoryInline (one line). Home Screen: small / medium, in full colour.
//
// One HTTP request per refresh: it POSTs a Jinja template to Home Assistant's
// /api/template endpoint and gets back a ~130 byte JSON blob. That keeps the
// logic (including the coast-to-10am health colour) in HA where it already
// lives, instead of duplicating it here and letting the two drift.

// ---------------------------------------------------------------- config ----

// Tailscale MagicDNS name: resolves both on the home LAN and remotely on the
// tailnet. A plain LAN address would leave the widget stale whenever you're out.
const HA_URL = "http://ha.tail78279c.ts.net:8123";

// Leave empty and you'll be prompted once; the token is then kept in the
// Scriptable keychain on this device. Run the script inside the Scriptable app
// once before adding the widget, so the prompt can appear.
const TOKEN_INLINE = "";
const KEYCHAIN_KEY = "foxctl_ha_token";

// Battery is considered "not on track" when HA's coast helper says so.
const COLOURS = {
  green: new Color("#22c55e"),
  orange: new Color("#f59e0b"),
  red: new Color("#ef4444"),
  track: new Color("#ffffff", 0.22),
  dim: new Color("#ffffff", 0.65),
};

const TEMPLATE = `
{% set soc = states('sensor.foxess_foxctl_battery_soc') | float(-1) %}
{% set chg = states('sensor.foxess_foxctl_battery_charge_power') | float(0) %}
{% set dis = states('sensor.foxess_foxctl_battery_discharge_power') | float(0) %}
{% set car = states('sensor.foxess_foxctl_ev_charger_power') | float(0) %}
{% set load = states('sensor.foxess_foxctl_house_load') | float(0) %}
{% set solar = states('sensor.foxess_foxctl_solar_power') | float(0) %}
{% set grid = states('sensor.foxess_foxctl_grid_import') | float(0) %}
{{ {'soc': soc|round(0), 'chg': chg|round(2), 'dis': dis|round(2),
    'car': car|round(2), 'load': load|round(2), 'solar': solar|round(2),
    'grid': grid|round(2),
    'health': states('sensor.kiosk_battery_soc_health')} | tojson }}
`.trim();

// ------------------------------------------------------------------ data ----

async function getToken() {
  if (TOKEN_INLINE) return TOKEN_INLINE;
  if (Keychain.contains(KEYCHAIN_KEY)) return Keychain.get(KEYCHAIN_KEY);
  // Widgets cannot show prompts, so this only works when run inside the app.
  const a = new Alert();
  a.title = "Home Assistant token";
  a.message =
    "Paste a long-lived access token (Profile → Security → Long-lived access " +
    "tokens). It is stored in this device's keychain only.";
  a.addSecureTextField("token", "");
  a.addAction("Save");
  a.addCancelAction("Cancel");
  if ((await a.presentAlert()) === -1) throw new Error("no token supplied");
  const t = a.textFieldValue(0).trim();
  if (!t) throw new Error("empty token");
  Keychain.set(KEYCHAIN_KEY, t);
  return t;
}

function cacheFile() {
  const fm = FileManager.local();
  return fm.joinPath(fm.cacheDirectory(), "foxctl_battery_widget.json");
}

function readCache() {
  try {
    const fm = FileManager.local();
    const p = cacheFile();
    if (!fm.fileExists(p)) return null;
    const blob = JSON.parse(fm.readString(p));
    blob.ageMin = Math.round((Date.now() - blob.at) / 60000);
    blob.stale = true;
    return blob;
  } catch (e) {
    return null;
  }
}

function writeCache(data) {
  try {
    FileManager.local().writeString(
      cacheFile(), JSON.stringify({ ...data, at: Date.now() }));
  } catch (e) {
    // A cache write failing must never take the widget down.
  }
}

async function fetchState() {
  const token = await getToken();
  const req = new Request(`${HA_URL}/api/template`);
  req.method = "POST";
  req.headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
  req.body = JSON.stringify({ template: TEMPLATE });
  req.timeoutInterval = 12;
  const text = await req.loadString();
  const data = JSON.parse(text);
  if (typeof data.soc !== "number") throw new Error("bad payload");
  data.stale = false;
  data.ageMin = 0;
  writeCache(data);
  return data;
}

// Off the tailnet the request fails; showing the last good reading with its age
// beats an empty widget, as long as it is honestly labelled as stale.
async function loadData() {
  try {
    return await fetchState();
  } catch (e) {
    const cached = readCache();
    if (cached) return cached;
    return { error: String(e.message || e) };
  }
}

// --------------------------------------------------------------- helpers ----

function flow(d) {
  if (d.chg > 0.05) return { dir: "up", kw: d.chg, word: "charging", sign: "▲" };
  if (d.dis > 0.05) return { dir: "down", kw: d.dis, word: "discharging", sign: "▼" };
  if (d.soc >= 98.5) return { dir: "full", kw: 0, word: "full", sign: "·" };
  return { dir: "idle", kw: 0, word: "idle", sign: "·" };
}

function socColour(d) {
  // Charging or full is always fine; otherwise defer to HA's coast helper.
  if (d.chg > 0.05 || d.soc >= 98.5) return COLOURS.green;
  return COLOURS[d.health] || COLOURS.green;
}

// Ring gauge. Lock Screen accessory widgets render monochrome, so on the lock
// screen this reads as shape only — the colour matters on the Home Screen.
function gauge(pct, size, fg, lineWidth) {
  const dc = new DrawContext();
  dc.size = new Size(size, size);
  dc.opaque = false;
  dc.respectScreenScale = true;
  const r = (size - lineWidth) / 2;
  const cx = size / 2;
  const cy = size / 2;

  const arc = (fromDeg, toDeg, colour) => {
    dc.setStrokeColor(colour);
    dc.setLineWidth(lineWidth);
    const path = new Path();
    const steps = Math.max(2, Math.round(Math.abs(toDeg - fromDeg) / 3));
    for (let i = 0; i <= steps; i++) {
      const deg = fromDeg + (toDeg - fromDeg) * (i / steps);
      const rad = (deg * Math.PI) / 180;
      const pt = new Point(cx + r * Math.cos(rad), cy + r * Math.sin(rad));
      if (i === 0) path.move(pt);
      else path.addLine(pt);
    }
    dc.addPath(path);
    dc.strokePath();
  };

  arc(-90, 270, COLOURS.track);
  const clamped = Math.max(0, Math.min(100, pct));
  if (clamped > 0) arc(-90, -90 + (360 * clamped) / 100, fg);
  return dc.getImage();
}

function line(stack, text, size, colour, bold) {
  const t = stack.addText(text);
  t.font = bold ? Font.boldSystemFont(size) : Font.systemFont(size);
  t.textColor = colour;
  t.lineLimit = 1;
  t.minimumScaleFactor = 0.7;
  return t;
}

// -------------------------------------------------------------- renderers ---

function renderError(w, msg) {
  w.addSpacer();
  line(w, "⚠︎ FoxESS", 12, Color.white(), true);
  line(w, msg.slice(0, 40), 10, COLOURS.dim);
  w.addSpacer();
  return w;
}

function renderCircular(w, d) {
  const f = flow(d);
  const stack = w.addStack();
  stack.layoutVertically();
  stack.setPadding(0, 0, 0, 0);
  stack.addSpacer();
  const img = stack.addImage(gauge(d.soc, 100, socColour(d), 10));
  img.imageSize = new Size(52, 52);
  img.centerAlignImage();
  stack.addSpacer();

  // Number over the ring.
  const overlay = w.addStack();
  overlay.layoutVertically();
  overlay.addSpacer();
  const row = overlay.addStack();
  row.addSpacer();
  line(row, `${d.soc}`, 15, Color.white(), true);
  row.addSpacer();
  const sub = overlay.addStack();
  sub.addSpacer();
  line(sub, f.sign, 9, COLOURS.dim);
  sub.addSpacer();
  overlay.addSpacer();
  return w;
}

function renderRectangular(w, d) {
  const f = flow(d);
  w.addSpacer();
  line(w, `Battery ${d.soc}%`, 15, Color.white(), true);
  line(w, `${f.sign} ${f.word}${f.kw ? ` ${f.kw.toFixed(1)}kW` : ""}`, 12, COLOURS.dim);
  const extra = d.car > 0.3
    ? `car ${d.car.toFixed(1)}kW`
    : `load ${(d.load || 0).toFixed(1)} · pv ${(d.solar || 0).toFixed(1)}kW`;
  line(w, d.stale ? `${extra} · ${d.ageMin}m old` : extra, 11, COLOURS.dim);
  w.addSpacer();
  return w;
}

function renderInline(w, d) {
  const f = flow(d);
  const txt = f.kw
    ? `${d.soc}% ${f.sign} ${f.kw.toFixed(1)}kW`
    : `${d.soc}% ${f.word}`;
  line(w, d.stale ? `${txt} (${d.ageMin}m)` : txt, 12, Color.white());
  return w;
}

function renderHome(w, d, medium) {
  const f = flow(d);
  w.setPadding(12, 14, 12, 14);
  const head = w.addStack();
  head.centerAlignContent();
  const img = head.addImage(gauge(d.soc, 120, socColour(d), 12));
  img.imageSize = new Size(medium ? 62 : 54, medium ? 62 : 54);
  head.addSpacer(10);
  const col = head.addStack();
  col.layoutVertically();
  line(col, `${d.soc}%`, medium ? 30 : 26, Color.white(), true);
  line(col, `${f.sign} ${f.word}`, 12, socColour(d));
  head.addSpacer();

  w.addSpacer(8);
  const rows = [
    ["Solar", `${(d.solar || 0).toFixed(2)} kW`],
    ["House", `${(d.load || 0).toFixed(2)} kW`],
  ];
  if (d.car > 0.3) rows.push(["Car", `${d.car.toFixed(2)} kW`]);
  else rows.push(["Grid", `${(d.grid || 0).toFixed(2)} kW`]);

  for (const [k, v] of rows.slice(0, medium ? 3 : 2)) {
    const r = w.addStack();
    line(r, k, 11, COLOURS.dim);
    r.addSpacer();
    line(r, v, 11, Color.white());
  }
  if (d.stale) {
    w.addSpacer(4);
    line(w, `stale · ${d.ageMin}m old`, 9, COLOURS.dim);
  }
  return w;
}

// ------------------------------------------------------------------ main ----

const data = await loadData();
const widget = new ListWidget();
widget.backgroundColor = new Color("#14181e");
// Gives lock-screen widgets the standard rounded backdrop on iOS 16+.
if (widget.addAccessoryWidgetBackground !== undefined) {
  widget.addAccessoryWidgetBackground = true;
}
// Refresh hint: iOS decides, but asking for 10 minutes keeps it as fresh as the
// system budget allows.
widget.refreshAfterDate = new Date(Date.now() + 10 * 60 * 1000);

const family = config.widgetFamily || "accessoryRectangular";

if (data.error) {
  renderError(widget, data.error);
} else if (family === "accessoryCircular") {
  renderCircular(widget, data);
} else if (family === "accessoryInline") {
  renderInline(widget, data);
} else if (family === "accessoryRectangular") {
  renderRectangular(widget, data);
} else {
  renderHome(widget, data, family !== "small");
}

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  // Tapping Run in the app previews the lock-screen rectangular layout.
  await widget.presentMedium();
}
Script.complete();
