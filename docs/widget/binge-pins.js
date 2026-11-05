// Binge Stats pins: iOS home screen widget for Scriptable.
// Your next pinned episode, and how many shows are ready, downloading, stalled or missing.
//
// Install:
//   1. Get Scriptable from the App Store (free).
//   2. New script, paste this in, name it "Binge Pins".
//   3. Long-press the home screen → + → Scriptable → pick a size.
//   4. Edit the widget: Script = Binge Pins, Parameter = https://binge.example.com|your-api-token
//
// The parameter is "url|token", or "url|token|user_id" to show someone else's pins (the id is on
// their Users page in the address bar). Make the token in Binge Stats: Settings → API tokens; a
// read-only one is enough. Keeping it in the widget's parameter rather than in this script means the
// token isn't in a file you might paste somewhere, and one script can drive several widgets.
//
// The phone has to reach Binge Stats: on the LAN, over Tailscale, or through your tunnel.

const REFRESH_MINUTES = 15;

// Optional, and only for testing. Running the script inside Scriptable (the ▶ button) has no widget
// to read a parameter from, so without these the only diagnosis you'd get is a home screen tile four
// words wide. The widget's own Parameter always wins.
const TEST_URL = "";
const TEST_TOKEN = "";
const TEST_USER = "";

// ── Configuration ────────────────────────────────

function settings() {
  const raw = (args.widgetParameter || "").trim();
  const [url, token, user] = raw
    ? raw.split("|").map((s) => (s || "").trim())
    : [TEST_URL.trim(), TEST_TOKEN.trim(), TEST_USER.trim()];
  if (!url || !token) return null;
  return { url: url.replace(/\/+$/, ""), token, user: user || "" };
}

function widgetPath(cfg) {
  return "/api/v1/pins/widget" + (cfg.user ? `?user_id=${encodeURIComponent(cfg.user)}` : "");
}

function headers(cfg, json) {
  const h = { Authorization: `Bearer ${cfg.token}` };
  if (json) h.Accept = "application/json";
  return h;
}

// ── Fetching ─────────────────────────────────────

// loadString, not loadJSON. loadJSON throws "the data couldn't be read because it isn't in the
// correct format" and takes the response with it, which is the one thing that would tell you why.
// A login page, a router admin screen and a 502 from a proxy all fail identically through it.
async function fetchJSON(cfg, path) {
  const req = new Request(cfg.url + path);
  req.headers = headers(cfg, true);
  req.timeoutInterval = 15;

  const body = await req.loadString();
  const status = req.response ? req.response.statusCode : 0;

  let data;
  try {
    data = JSON.parse(body);
  } catch (err) {
    throw new Error(`HTTP ${status} but not JSON: ${describe(body)}`);
  }
  if (status >= 400) {
    const msg = data && data.error ? data.error.message : body.slice(0, 80);
    throw new Error(`HTTP ${status}: ${msg}`);
  }
  // Valid JSON from something that isn't Binge Stats is still the wrong answer, and says so more
  // usefully than a missing field would later.
  if (!data || typeof data.headline !== "string") {
    throw new Error(`HTTP ${status}, JSON, but not Binge Stats: check the URL`);
  }
  return data;
}

// What came back, in the few words a home screen has room for.
function describe(body) {
  const head = (body || "").trim().slice(0, 60).replace(/\s+/g, " ");
  if (/^<!doctype html|^<html/i.test(head)) {
    return "an HTML page. Wrong address, or a login or proxy in the way";
  }
  if (!head) return "an empty response";
  return head;
}

async function fetchImage(cfg, path) {
  try {
    const req = new Request(cfg.url + path);
    req.headers = headers(cfg, false);
    req.timeoutInterval = 15;
    return await req.loadImage();
  } catch (err) {
    // A missing poster is not a reason to show no widget.
    return null;
  }
}

// ── Drawing ──────────────────────────────────────

const INK = Color.dynamic(new Color("#14181d"), new Color("#e6edf3"));
const MUTED = Color.dynamic(new Color("#5d6874"), new Color("#9aa7b4"));
const ACCENT = Color.dynamic(new Color("#7c3aed"), new Color("#a78bfa"));
const BAD = Color.dynamic(new Color("#b3261e"), new Color("#f0857d"));

function shell() {
  const w = new ListWidget();
  w.backgroundColor = Color.dynamic(new Color("#ffffff"), new Color("#12121a"));
  w.setPadding(12, 12, 12, 12);
  w.refreshAfterDate = new Date(Date.now() + REFRESH_MINUTES * 60 * 1000);
  return w;
}

function line(stack, text, { size = 12, color = INK, bold = false, max = 1 } = {}) {
  const t = stack.addText(text);
  t.font = bold ? Font.semiboldSystemFont(size) : Font.systemFont(size);
  t.textColor = color;
  t.lineLimit = max;
  return t;
}

function brand(w, stale) {
  const row = w.addStack();
  row.centerAlignContent();
  line(row, "BINGE · PINS", { size: 9, color: MUTED, bold: true });
  row.addSpacer();
  // Says so rather than quietly showing yesterday: iOS decides when a widget
  // refreshes, and "no data" and "old data" look identical otherwise.
  if (stale) line(row, "stale", { size: 9, color: BAD, bold: true });
  w.addSpacer(6);
}

function stats(w, list, columns) {
  const row = w.addStack();
  row.spacing = 10;
  for (const stat of list.slice(0, columns)) {
    const col = row.addStack();
    col.layoutVertically();
    line(col, stat.value, { size: 17, bold: true, color: stat.alert ? BAD : INK });
    line(col, stat.label, { size: 9, color: MUTED });
    if (stat.detail) line(col, stat.detail, { size: 9, color: MUTED });
    row.addSpacer();
  }
}

function problem(message, hint) {
  const w = shell();
  brand(w, false);
  line(w, message, { size: 13, bold: true, max: 2 });
  if (hint) {
    w.addSpacer(4);
    line(w, hint, { size: 10, color: MUTED, max: 4 });
  }
  return w;
}

async function build(cfg, data) {
  const w = shell();
  const age = (Date.now() - data.generated_at * 1000) / 60000;
  const stale = age > (data.stale_after_minutes || 45);

  brand(w, stale);

  const small = config.widgetFamily === "small";
  const large = config.widgetFamily === "large";

  const head = w.addStack();
  head.spacing = 10;

  const words = head.addStack();
  words.layoutVertically();
  line(words, data.headline, { size: small ? 13 : 15, bold: true, max: 2 });
  if (data.subhead) {
    line(words, data.subhead, { size: small ? 10 : 12, color: ACCENT, max: 1 });
  }
  if (!small && data.next && data.next.title) {
    line(words, data.next.title, { size: 11, color: MUTED, max: 1 });
  }

  if (!small && data.next && data.next.poster) {
    head.addSpacer();
    const art = await fetchImage(cfg, data.next.poster);
    if (art) {
      const img = head.addImage(art);
      img.imageSize = new Size(46, 69);
      img.cornerRadius = 4;
    }
  }

  w.addSpacer();
  stats(w, data.stats || [], small ? 2 : 4);

  if (large && data.tonight) {
    w.addSpacer(6);
    line(w, `${data.tonight} airing today`, { size: 11, color: MUTED });
  }

  w.url = data.url || cfg.url;
  return w;
}

// ── Go ───────────────────────────────────────────

let widget;
const cfg = settings();

if (!cfg) {
  widget = problem(
    "Not configured",
    "Long-press the widget → Edit Widget → Parameter, and set it to " +
      "https://your-binge-stats|your-api-token",
  );
} else {
  try {
    const data = await fetchJSON(cfg, widgetPath(cfg));
    widget = await build(cfg, data);
  } catch (err) {
    // "Unreachable" is wrong when the server plainly answered, and sending
    // somebody to check their network over a 401 wastes an evening.
    const detail = String(err).replace(/^Error:\s*/, "");
    const answered = detail.startsWith("HTTP");
    widget = problem(
      answered ? "Binge Stats said no" : "Binge Stats unreachable",
      detail.slice(0, 160),
    );
  }
}

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  // Run from inside Scriptable, the useful output is the diagnosis, not a
  // picture of a widget. A home screen tile has room for four words; this
  // has room for the reason.
  if (cfg) {
    console.log(`GET ${cfg.url}${widgetPath(cfg)}`);
    try {
      const req = new Request(cfg.url + widgetPath(cfg));
      req.headers = headers(cfg, true);
      req.timeoutInterval = 15;
      const body = await req.loadString();
      const status = req.response ? req.response.statusCode : 0;
      const type = req.response && req.response.headers
        ? req.response.headers["Content-Type"] ||
          req.response.headers["content-type"] || "?"
        : "?";
      console.log(`HTTP ${status}  ${type}`);
      console.log(body.slice(0, 400));
    } catch (err) {
      console.log(`Never got a reply: ${err}`);
      console.log(
        "The phone could not reach that address at all — different network, " +
          "or nothing listening there.",
      );
    }
  } else {
    console.log("No parameter and no TEST_URL/TEST_TOKEN set.");
  }
  await widget.presentMedium();
}
Script.complete();
