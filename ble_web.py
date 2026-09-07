"""Live browser dashboard for the separated-beacon hunt.

Why this exists rather than `watch --device <id>`: watch locks onto one
CoreBluetooth id, and a Find My key rotation retires that id without any error.
The meter then sits there looking alive while showing a number that will never
update again. This tracks the strongest *separated* beacon instead, which
survives rotation, because a rotated beacon is still a separated beacon.

Run it, then open http://127.0.0.1:8765 on the machine you are carrying.

    uv run python ble_web.py

Serves on loopback only. Nothing is written to disk and nothing leaves the
machine.
"""

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bleak import BleakScanner

from apple_ble import classify_device, estimate_distance_m, is_valid_rssi, merge_kind

log = logging.getLogger("ble_web")

# A separated beacon this quiet for this long has almost certainly rotated its
# key. Generous, because the emitter we are chasing has gone 90s silent and come
# back on the same id, and dropping a live target is worse than a stale label.
DEFAULT_STALE_AFTER = 180.0

# Packets kept for the history chart. At ~6 packets/min from a separated beacon
# this is roughly the last half hour, which comfortably spans a room-to-room walk.
HISTORY_LEN = 240

SEPARATED_PREFIX = "apple_find_my_separated"


# MARK: - Pure logic


def pick_target(candidates, now, stale_after=DEFAULT_STALE_AFTER):
    """Choose which advertiser the dashboard should point at.

    Separated beacons only: a `nearby_owner` frame means the thing is sitting
    with its owner, so however loud it is, it is not what we are hunting.
    Staleness is what makes this survive a key rotation: the retired id ages out
    and the freshly rotated one takes over on its own.
    """
    live = [
        c
        for c in candidates
        if str(c.get("kind", "")).startswith(SEPARATED_PREFIX)
        and now - c.get("last_seen", 0) <= stale_after
    ]
    if not live:
        return None
    return max(live, key=lambda c: (c.get("smoothed", c["rssi"]), c["last_seen"]))


def signal_band(rssi):
    """Map dBm to a (label, status role) pair.

    The label is not decoration. Status colour never carries meaning on its own,
    so the band names itself in text beside the bar.
    """
    if rssi >= -60:
        return ("Very close", "good")
    if rssi >= -75:
        return ("Warm", "warning")
    if rssi >= -90:
        return ("Cold", "serious")
    return ("Very cold", "critical")


def signal_percent(rssi):
    """Meter fill, 0-100. -100 dBm is empty, -45 dBm is full."""
    return max(0.0, min(100.0, (rssi + 100) * (100 / 55)))


def summarise_by_location(history):
    """Best and median per room, strongest room first.

    Ranked on the median rather than the best, because a single lucky packet
    routinely lands 10 dB above a room's typical read and would otherwise
    promote a cold room to the top of the list. Computed across every separated
    beacon rather than just the current target, so the comparison survives a key
    rotation mid-walk.
    """
    groups = defaultdict(list)
    for point in history:
        groups[point.get("location", "unknown")].append(point["rssi"])

    rows = [
        {
            "location": location,
            "packets": len(values),
            "best": max(values),
            "median": sorted(values)[len(values) // 2],
        }
        for location, values in groups.items()
    ]
    rows.sort(key=lambda row: (-row["median"], -row["best"]))
    return rows


# MARK: - Reading log


class ReadingLog:
    """Append-only JSONL record of every separated-beacon packet.

    Hunt data cannot be reproduced. A beacon rotates its key every few minutes,
    so a reading taken at 21:26 can never be taken again, and a crash or a
    restart that loses it loses it permanently. Hence append mode and a flush per
    line rather than buffering.

    A `None` path makes this a no-op sink, so call sites never branch on it.
    """

    def __init__(self, path):
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, reading):
        if self.path is None:
            return
        with open(self.path, "a") as handle:
            handle.write(json.dumps(reading) + "\n")
            handle.flush()


def load_saved_readings(path):
    """Read a JSONL reading log back into memory.

    Tolerant by design: a log written by a process that was killed mid-flush can
    end in a half-written line, and losing an entire hunt to that would be
    absurd. Bad lines are skipped, everything else is kept.
    """
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("skipping unparseable line in %s", path)
    return rows


# MARK: - Tracker


class Tracker:
    """Rolling state for every advertiser seen, plus the current target.

    Safe to read from the HTTP thread while the asyncio scanner writes to it.
    """

    def __init__(
        self,
        stale_after=DEFAULT_STALE_AFTER,
        history_len=HISTORY_LEN,
        reading_log=None,
    ):
        self.stale_after = stale_after
        self.history_len = history_len
        self.reading_log = reading_log or ReadingLog(None)
        self._seen = {}
        self._history = []
        self._lock = threading.Lock()
        self._target_id = None
        self._rotations = 0
        self._location = "unknown"

    def set_location(self, name):
        """Label subsequent readings. Blank input is ignored, never a wipe."""
        cleaned = (name or "").strip()
        if not cleaned:
            return self._location
        with self._lock:
            self._location = cleaned
        log.info("location set to %r", cleaned)
        return cleaned

    def replay(self, readings):
        """Load a saved hunt back in, for screenshots and write-ups.

        Each point keeps the location it was captured under, not the tracker's
        current one, so a replayed walk still shows the rooms it was walked
        through. Timestamps are restamped as recent so the result reads as a
        live target rather than a beacon that went stale years ago.

        Deliberately does NOT go through `record`: that would append every
        replayed point back to the reading log, doubling the file each run.
        """
        if not readings:
            return
        now = time.time()
        span = max(1.0, readings[-1]["t"] - readings[0]["t"])
        with self._lock:
            for point in readings:
                offset = (point["t"] - readings[0]["t"]) - span
                self._history.append(
                    {
                        "t": now + offset,
                        "id": point["id"],
                        "rssi": point["rssi"],
                        "location": point.get("location", "unknown"),
                    }
                )
            del self._history[: -self.history_len]

            last = readings[-1]
            self._seen[last["id"]] = {
                "id": last["id"],
                "kind": last.get("kind", SEPARATED_PREFIX),
                "rssi": last["rssi"],
                "best": max(r["rssi"] for r in readings if r["id"] == last["id"]),
                "packets": sum(1 for r in readings if r["id"] == last["id"]),
                "first_seen": now - span,
                "last_seen": now,
                "samples": [last["rssi"]],
                "smoothed": float(last["rssi"]),
            }
        log.info("replayed %d readings", len(readings))

    def record(self, device_id, rssi, kind, now=None):
        now = time.time() if now is None else now
        with self._lock:
            entry = self._seen.get(device_id)
            if entry is None:
                entry = {
                    "id": device_id,
                    "kind": kind,
                    "rssi": rssi,
                    "best": rssi,
                    "packets": 0,
                    "first_seen": now,
                    "samples": [],
                }
                self._seen[device_id] = entry

            entry["kind"] = merge_kind(entry["kind"], kind)
            entry["rssi"] = rssi
            entry["best"] = max(entry["best"], rssi)
            entry["last_seen"] = now
            entry["packets"] += 1
            entry["samples"].append(rssi)
            del entry["samples"][:-5]
            entry["smoothed"] = round(sum(entry["samples"]) / len(entry["samples"]), 1)

            # Only separated traffic reaches the chart. With duplicates enabled a
            # busy room emits hundreds of packets a minute and the rest of them
            # would bury the one line we care about.
            if entry["kind"].startswith(SEPARATED_PREFIX):
                point = {
                    "t": now,
                    "id": device_id,
                    "rssi": rssi,
                    "location": self._location,
                }
                self._history.append(point)
                del self._history[: -self.history_len]
                self.reading_log.append(
                    dict(point, kind=entry["kind"], smoothed=entry["smoothed"])
                )

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            candidates = [dict(entry) for entry in self._seen.values()]
            history = list(self._history)

            target = pick_target(candidates, now, self.stale_after)
            if target is not None and target["id"] != self._target_id:
                if self._target_id is not None:
                    self._rotations += 1
                    log.info("target changed %s -> %s", self._target_id, target["id"])
                self._target_id = target["id"]

            rotations = self._rotations

        payload = {
            "now": now,
            "target": None,
            "location": self._location,
            "by_location": summarise_by_location(history),
            "history": [p for p in history if target and p["id"] == target["id"]],
            "rotations": rotations,
            "separated_count": sum(
                1
                for c in candidates
                if str(c["kind"]).startswith(SEPARATED_PREFIX)
                and now - c.get("last_seen", 0) <= self.stale_after
            ),
            "advertisers": len(candidates),
        }

        if target is not None:
            label, role = signal_band(target["smoothed"])
            payload["target"] = {
                "id": target["id"],
                "rssi": target["rssi"],
                "smoothed": target["smoothed"],
                "best": target["best"],
                "packets": target["packets"],
                "age": round(now - target["last_seen"], 1),
                "metres": round(estimate_distance_m(int(target["smoothed"])), 1),
                "percent": round(signal_percent(target["smoothed"]), 1),
                "band": label,
                "role": role,
            }
        return payload


# MARK: - Page

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Beacon hunt</title>
<style>
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb;
    --plane: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --grid: #e1e0d9;
    --axis: #c3c2b7;
    --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6;
    --good: #0ca30c;
    --warning: #fab219;
    --serious: #ec835a;
    --critical: #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface-1: #1a1a19;
      --plane: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --grid: #2c2c2a;
      --axis: #383835;
      --border: rgba(255,255,255,0.10);
      --series-1: #3987e5;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --plane: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--plane);
    color: var(--text-primary);
  }
  .viz-root { min-height: 100vh; padding: 20px; background: var(--plane); }
  .wrap { max-width: 720px; margin: 0 auto; display: grid; gap: 14px; }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
  }
  h1 { font-size: 15px; font-weight: 600; margin: 0; letter-spacing: -0.01em;
       color: var(--text-primary); }
  .sub { font-size: 13px; color: var(--text-secondary); margin-top: 3px; }
  .muted { color: var(--text-muted); }

  .hero { display: flex; align-items: baseline; gap: 12px; }
  .hero-value { font-size: 68px; font-weight: 600; line-height: 1; letter-spacing: -0.03em;
                color: var(--text-primary); }
  .hero-unit { font-size: 17px; color: var(--text-secondary); }

  .band { display: inline-flex; align-items: center; gap: 7px; font-size: 14px;
          font-weight: 600; margin-top: 12px; color: var(--text-primary); }
  .dot { width: 11px; height: 11px; border-radius: 50%; flex: none; }

  .meter-track { height: 14px; border-radius: 7px; margin-top: 10px; overflow: hidden; }
  .meter-fill { height: 100%; border-radius: 7px; transition: width .35s ease; }

  .tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px;
           background: var(--border); border-radius: 10px; overflow: hidden; }
  .tile { background: var(--surface-1); padding: 12px 14px; }
  .tile-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase;
                letter-spacing: .05em; }
  .tile-value { font-size: 21px; font-weight: 600; margin-top: 3px;
                color: var(--text-primary); }

  svg { display: block; width: 100%; height: 190px; touch-action: none; }
  .tip {
    position: absolute; pointer-events: none; background: var(--surface-1);
    border: 1px solid var(--border); border-radius: 7px; padding: 6px 9px;
    font-size: 12px; box-shadow: 0 3px 12px rgba(0,0,0,.16); opacity: 0;
    transition: opacity .1s; white-space: nowrap; color: var(--text-primary);
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px;
          font-variant-numeric: tabular-nums; }
  th { text-align: left; font-size: 11px; color: var(--text-muted);
       text-transform: uppercase; letter-spacing: .05em; padding: 5px 8px;
       border-bottom: 1px solid var(--grid); }
  td { padding: 5px 8px; border-bottom: 1px solid var(--grid);
       color: var(--text-secondary); }
  summary { cursor: pointer; font-size: 13px; color: var(--text-secondary); }
  code { font-size: 12px; color: var(--text-secondary); word-break: break-all; }
  .lost { font-size: 14px; color: var(--text-secondary); line-height: 1.5; }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
  .row { display: flex; gap: 8px; margin-top: 12px; }
  input, button {
    font: inherit; font-size: 14px; border-radius: 8px;
    border: 1px solid var(--border); padding: 9px 12px;
    background: var(--plane); color: var(--text-primary);
  }
  input { flex: 1; min-width: 0; }
  button { cursor: pointer; font-weight: 600; }
  button:hover { border-color: var(--axis); }
  .ghost { font-size: 12px; padding: 6px 10px; font-weight: 500;
           color: var(--text-secondary); white-space: nowrap; }
  .here { font-size: 13px; color: var(--text-secondary); margin-top: 9px; }
  .here b { color: var(--text-primary); }
  .win { color: var(--text-primary); font-weight: 600; }
</style>
</head>
<body>
<div class="viz-root"><div class="wrap">

  <div class="card">
    <div class="head">
      <div>
        <h1>Beacon hunt</h1>
        <div class="sub" id="sub">Waiting for the first packet...</div>
      </div>
      <button class="ghost" id="theme-btn" title="Light, dark, or follow the system">Theme: auto</button>
    </div>
    <div class="row">
      <input id="loc-input" placeholder="Where are you? e.g. desk room" autocomplete="off">
      <button id="loc-set">I'm here</button>
    </div>
    <div class="here">Tagging readings as <b id="here">unknown</b></div>
  </div>

  <div class="card" id="meter-card">
    <div class="tile-label">Signal strength, latest packet</div>
    <div class="hero">
      <span class="hero-value" id="hero">--</span>
      <span class="hero-unit">dBm</span>
    </div>
    <div class="band"><span class="dot" id="band-dot"></span><span id="band">No target</span></div>
    <div class="meter-track" id="track"><div class="meter-fill" id="fill" style="width:0%"></div></div>
  </div>

  <div class="tiles">
    <div class="tile"><div class="tile-label">Best</div><div class="tile-value" id="best">--</div></div>
    <div class="tile"><div class="tile-label">Average</div><div class="tile-value" id="avg">--</div></div>
    <div class="tile"><div class="tile-label">Packets</div><div class="tile-value" id="pkts">--</div></div>
    <div class="tile"><div class="tile-label">Last seen</div><div class="tile-value" id="age">--</div></div>
  </div>

  <div class="card" style="position:relative">
    <h1>Signal over time</h1>
    <div class="sub">Every packet from the beacon being tracked. Higher is closer.</div>
    <svg id="chart" viewBox="0 0 720 190" preserveAspectRatio="none"></svg>
    <div class="tip" id="tip"></div>
  </div>

  <div class="card">
    <h1>Rooms compared</h1>
    <div class="sub">Ranked by median, strongest first. The top row is where to search.</div>
    <table style="margin-top:12px"><thead><tr><th>Where</th><th>Median</th><th>Best</th><th>Packets</th></tr></thead>
    <tbody id="rooms"><tr><td colspan="4" class="muted">No rooms tagged yet</td></tr></tbody></table>
  </div>

  <div class="card">
    <details>
      <summary>Readings table</summary>
      <table><thead><tr><th>Time</th><th>dBm</th><th>Band</th><th>Where</th></tr></thead>
      <tbody id="rows"></tbody></table>
    </details>
    <div class="sub" style="margin-top:12px">Tracking <code id="tid">nothing yet</code></div>
  </div>

</div></div>
<script>
const $ = id => document.getElementById(id);

// Theme: auto follows the OS, light/dark are explicit stamps that beat it.
const THEMES = ['auto', 'light', 'dark'];
function applyTheme(t) {
  if (t === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('bw-theme', t);
  $('theme-btn').textContent = 'Theme: ' + t;
}
applyTheme(localStorage.getItem('bw-theme') || 'auto');
$('theme-btn').onclick = () => {
  const cur = localStorage.getItem('bw-theme') || 'auto';
  applyTheme(THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]);
};

async function setLocation() {
  const name = $('loc-input').value.trim();
  if (!name) return;
  await fetch('/api/location', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ location: name })
  });
  $('loc-input').value = '';
  tick();
}
$('loc-set').onclick = setLocation;
$('loc-input').addEventListener('keydown', e => { if (e.key === 'Enter') setLocation(); });
const roleVar = r => getComputedStyle(document.documentElement).getPropertyValue('--' + r).trim();
let last = [];

function band(v) {
  if (v >= -60) return ['Very close', 'good'];
  if (v >= -75) return ['Warm', 'warning'];
  if (v >= -90) return ['Cold', 'serious'];
  return ['Very cold', 'critical'];
}

function draw(points) {
  const svg = $('chart'), W = 720, H = 190, P = { t: 14, r: 14, b: 22, l: 38 };
  svg.innerHTML = '';
  const ns = 'http://www.w3.org/2000/svg';
  const mk = (n, a) => { const e = document.createElementNS(ns, n);
    for (const k in a) e.setAttribute(k, a[k]); return e; };

  if (points.length < 2) {
    const t = mk('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle',
      fill: 'var(--text-muted)', 'font-size': 13 });
    t.textContent = points.length ? 'One packet so far, waiting for more' : 'No packets yet';
    svg.appendChild(t); return;
  }

  const lo = Math.min(-100, ...points.map(p => p.rssi)) - 2;
  const hi = Math.max(-40, ...points.map(p => p.rssi)) + 2;
  const x = i => P.l + (i / (points.length - 1)) * (W - P.l - P.r);
  const y = v => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);

  for (let v = Math.ceil(lo / 20) * 20; v <= hi; v += 20) {
    svg.appendChild(mk('line', { x1: P.l, x2: W - P.r, y1: y(v), y2: y(v),
      stroke: 'var(--grid)', 'stroke-width': 1 }));
    const lb = mk('text', { x: P.l - 7, y: y(v) + 4, 'text-anchor': 'end',
      fill: 'var(--text-muted)', 'font-size': 11 });
    lb.textContent = v; svg.appendChild(lb);
  }

  const d = points.map((p, i) => (i ? 'L' : 'M') + x(i) + ' ' + y(p.rssi)).join(' ');
  svg.appendChild(mk('path', { d, fill: 'none', stroke: 'var(--series-1)',
    'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));

  const n = points.length - 1;
  svg.appendChild(mk('circle', { cx: x(n), cy: y(points[n].rssi), r: 5,
    fill: 'var(--series-1)', stroke: 'var(--surface-1)', 'stroke-width': 2 }));

  const cross = mk('line', { stroke: 'var(--axis)', 'stroke-width': 1, opacity: 0 });
  svg.appendChild(cross);
  svg.onpointerleave = () => { cross.setAttribute('opacity', 0); $('tip').style.opacity = 0; };
  svg.onpointermove = ev => {
    const box = svg.getBoundingClientRect();
    const px = (ev.clientX - box.left) / box.width * W;
    let i = Math.round((px - P.l) / (W - P.l - P.r) * (points.length - 1));
    i = Math.max(0, Math.min(points.length - 1, i));
    const p = points[i];
    cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i));
    cross.setAttribute('y1', P.t); cross.setAttribute('y2', H - P.b);
    cross.setAttribute('opacity', 1);
    const tip = $('tip');
    tip.textContent = new Date(p.t * 1000).toLocaleTimeString() + '  ' + p.rssi + ' dBm';
    tip.style.opacity = 1;
    tip.style.left = Math.min(box.width - 130, x(i) / W * box.width + 12) + 'px';
    tip.style.top = (y(p.rssi) / H * box.height) + 'px';
  };
}

function renderRooms(s) {
  $('here').textContent = s.location;
  const rows = s.by_location || [];
  $('rooms').innerHTML = rows.length
    ? rows.map((r, i) =>
        '<tr><td class="' + (i === 0 ? 'win' : '') + '">' + r.location +
        (i === 0 ? ' &larr; warmest' : '') + '</td><td>' + r.median + '</td><td>' +
        r.best + '</td><td>' + r.packets + '</td></tr>').join('')
    : '<tr><td colspan="4" class="muted">No rooms tagged yet</td></tr>';
}

async function tick() {
  let s;
  try { s = await (await fetch('/api/state')).json(); } catch (e) { return; }
  renderRooms(s);
  const t = s.target;

  if (!t) {
    $('sub').innerHTML = '<span class="lost">No separated beacon in range right now. ' +
      s.advertisers + ' advertisers seen. Keep the scanner running: this beacon has gone ' +
      'quiet for over a minute before and come back.</span>';
    $('hero').textContent = '--';
    $('band').textContent = 'No target';
    $('band-dot').style.background = 'var(--text-muted)';
    $('fill').style.width = '0%';
    return;
  }

  const [label, role] = band(t.smoothed);
  const colour = roleVar(role);
  $('sub').textContent = s.separated_count + ' separated beacon' +
    (s.separated_count === 1 ? '' : 's') + ' in range, ' + s.advertisers +
    ' advertisers total' + (s.rotations ? '  ·  ' + s.rotations + ' key rotation(s) followed' : '');
  $('hero').textContent = t.rssi;
  $('band').textContent = label + '  ·  avg ' + t.smoothed + ' dBm';
  $('band-dot').style.background = colour;
  $('fill').style.width = t.percent + '%';
  $('fill').style.background = colour;
  $('track').style.background = colour + '26';
  $('best').textContent = t.best;
  $('avg').textContent = t.smoothed;
  $('pkts').textContent = t.packets;
  $('age').textContent = t.age + 's';
  $('tid').textContent = t.id;

  if (s.history.length !== last.length || s.history.length === 0) draw(s.history);
  last = s.history;

  $('rows').innerHTML = s.history.slice(-40).reverse().map(p =>
    '<tr><td>' + new Date(p.t * 1000).toLocaleTimeString() + '</td><td>' + p.rssi +
    '</td><td>' + band(p.rssi)[0] + '</td><td>' + (p.location || 'unknown') +
    '</td></tr>').join('');
}

tick(); setInterval(tick, 1000);
</script>
</body>
</html>
"""


# MARK: - Server


def make_handler(tracker):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/state"):
                body = json.dumps(tracker.snapshot()).encode()
                ctype = "application/json"
            elif self.path in ("/", "/index.html"):
                body = PAGE.encode()
                ctype = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/api/location":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.send_error(400, "expected JSON")
                return
            current = tracker.set_location(payload.get("location"))
            body = json.dumps({"location": current}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # one line per poll per second would drown the scan log

    return Handler


async def run(host, port, log_path, replay_path=None):
    tracker = Tracker(reading_log=ReadingLog(log_path))
    if replay_path:
        saved = load_saved_readings(replay_path)
        tracker.replay(saved)
        print(f"  Replayed {len(saved)} saved readings from {replay_path}", flush=True)
    if log_path:
        print(f"  Appending every reading to {log_path}", flush=True)
    server = ThreadingHTTPServer((host, port), make_handler(tracker))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"\n  Beacon hunt dashboard:  http://{host}:{port}\n", flush=True)
    log.info("serving on http://%s:%d", host, port)

    def on_detection(device, adv_data):
        if not is_valid_rssi(adv_data.rssi):
            return
        tracker.record(str(device.address), adv_data.rssi, classify_device(adv_data))

    if replay_path:
        # Screenshot / write-up mode. A live scan running alongside a replay
        # adds today's traffic to yesterday's hunt and quietly inflates every
        # count on the page.
        log.info("replay mode, radio stays off")
        while True:
            await asyncio.sleep(3600)

    scanner = BleakScanner(detection_callback=on_detection)
    async with scanner:
        while True:
            await asyncio.sleep(3600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1", help="loopback by default")
    parser.add_argument(
        "--log",
        default="hunt_logs/readings.jsonl",
        help="append every separated-beacon reading here (JSONL)",
    )
    parser.add_argument("--no-log", action="store_true", help="keep it all in memory")
    parser.add_argument(
        "--replay",
        help="load a saved JSONL reading log on startup (for screenshots/write-ups)",
    )
    args = parser.parse_args()

    Path("hunt_logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("hunt_logs/ble_web.log"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    try:
        asyncio.run(
            run(
                args.host,
                args.port,
                None if args.no_log else args.log,
                args.replay,
            )
        )
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
