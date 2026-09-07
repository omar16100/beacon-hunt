"""Room sweep for nearby BLE advertisers, with Apple Find My beacon flagging.

Modes:
    sweep        - scan for N seconds, print every advertiser by signal strength
    watch        - lock onto one device id, print a live hot/cold RSSI meter
    measure      - record one device's median RSSI at a known position
    trilaterate  - solve for the emitter's position from 3+ recorded positions

Hard limitation, stated up front: macOS Core Bluetooth never exposes hardware MAC
addresses to userspace. Devices are identified by a per-host CoreBluetooth UUID,
so a result here can NOT be matched against a paired Bluetooth MAC. Apple Find My
beacons additionally rotate an encrypted key, so they cannot be attributed to an
owner without that owner's private key.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner

from apple_ble import (
    APPLE_COMPANY_ID,
    classify_device,
    decode_apple_payload,
    estimate_distance_m,
    is_valid_rssi,
    kind_priority,
    merge_kind,
)

# Relative to this file, so the tool runs from any checkout without editing.
BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = str(BASE_DIR / "ble_sweep.log")
READINGS_PATH = str(BASE_DIR / "readings.json")

log = logging.getLogger("ble_sweep")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(sys.stderr),
        ],
    )


class Sighting:
    """Rolling record of one advertiser seen during a scan."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.name = None
        self.rssi_samples = []
        self.first_seen = time.time()
        self.last_seen = self.first_seen
        self.kind = "unknown"
        self.apple_decoded = None

    def update(self, device, adv_data) -> None:
        self.last_seen = time.time()
        # Bluetooth HCI uses 127 as "RSSI not available". Core Bluetooth passes
        # it straight through, and it poisons max()/median if kept.
        if is_valid_rssi(adv_data.rssi):
            self.rssi_samples.append(adv_data.rssi)
        if adv_data.local_name:
            self.name = adv_data.local_name
        elif device.name and not self.name:
            self.name = device.name
        # Sticky, priority-ranked. Not last-packet-wins: a device that emitted a
        # separated Find My frame then a bare packet must not degrade to
        # "non_apple" and vanish from the Find My summary.
        self.kind = merge_kind(self.kind, classify_device(adv_data))
        apple = adv_data.manufacturer_data.get(APPLE_COMPANY_ID)
        if apple is not None:
            decoded = decode_apple_payload(apple)
            # Retain a Find My decode over a later non-Find-My one.
            if decoded["is_find_my"] or self.apple_decoded is None:
                self.apple_decoded = decoded

    @property
    def has_signal(self) -> bool:
        return bool(self.rssi_samples)

    @property
    def best_rssi(self) -> int:
        return max(self.rssi_samples) if self.rssi_samples else -127

    @property
    def median_rssi(self) -> int:
        if not self.rssi_samples:
            return -127
        ordered = sorted(self.rssi_samples)
        return ordered[len(ordered) // 2]

    @property
    def count(self) -> int:
        return len(self.rssi_samples)


async def sweep(duration: int, apple_only: bool) -> dict[str, Sighting]:
    """Passively collect advertisements for `duration` seconds."""
    seen: dict[str, Sighting] = {}

    def on_detection(device, adv_data):
        key = str(device.address)
        if key not in seen:
            seen[key] = Sighting(key)
            log.debug("new advertiser: %s rssi=%s", key, adv_data.rssi)
        seen[key].update(device, adv_data)

    log.info("starting %ds passive BLE sweep", duration)
    scanner = BleakScanner(detection_callback=on_detection)
    async with scanner:
        await asyncio.sleep(duration)
    log.info("sweep complete: %d distinct advertisers", len(seen))

    if apple_only:
        seen = {k: v for k, v in seen.items() if v.kind != "non_apple"}
        log.info("filtered to %d Apple advertisers", len(seen))
    return seen


def print_report(seen: dict[str, Sighting]) -> None:
    if not seen:
        print("\nNo advertisers detected.")
        return

    rows = sorted(seen.values(), key=lambda s: s.best_rssi, reverse=True)
    print(f"\n{'DEVICE ID':38} {'BEST':>5} {'MED':>5} {'~m':>6} {'PKTS':>5}  KIND / NAME")
    print("-" * 108)
    for s in rows:
        label = s.kind
        if s.name:
            label += f"  \"{s.name}\""
        if s.has_signal:
            best = f"{s.best_rssi:>5}"
            med = f"{s.median_rssi:>5}"
            approx = f"{estimate_distance_m(s.best_rssi):>6.1f}"
        else:
            # Seen, but every packet had an invalid RSSI. Printing a distance
            # here would be fabricated.
            best = med = approx = f"{'n/a':>5}"
        print(f"{s.device_id:38} {best} {med} {approx:>6} {s.count:>5}  {label}")

    find_my = [s for s in seen.values() if s.kind.startswith("apple_find_my")]
    # Separated beacons first: those are the ones away from their owner.
    find_my.sort(key=lambda s: (-kind_priority(s.kind), -s.best_rssi))
    print(f"\nFind My beacons detected: {len(find_my)}")
    for s in find_my:
        fm = (s.apple_decoded or {}).get("find_my") or {}
        dist = (
            f"(~{estimate_distance_m(s.best_rssi):.1f}m)" if s.has_signal else "(no rssi)"
        )
        rssi = s.best_rssi if s.has_signal else "n/a"
        line = (
            f"  {s.device_id}  rssi={rssi} {dist}  "
            f"state={fm.get('state', 'unknown')}  "
            f"battery_bits={fm.get('battery_bits', '?')}"
        )
        if fm.get("truncated"):
            line += "  [TRUNCATED FRAME]"
        print(line)
    if find_my:
        print(
            "\n  NOTE: these keys are encrypted and rotate. None of them can be\n"
            "  attributed to you or to any specific device from this scan."
        )


def format_watch_line(when: datetime, raw_rssi: int, smoothed: float) -> str:
    """Render one packet as a standalone, timestamped log line.

    Both the raw packet and the smoothed average are shown. At the packet rates
    a separated Find My beacon manages (~6/min) the 5-sample window lags by the
    better part of a minute, so an average moving on its own tells you nothing
    about whether the last step helped. The raw column is what you read while
    walking; the average is what you trust once you have stood still.
    """
    approx = estimate_distance_m(int(smoothed))
    bar_len = max(0, min(40, int((smoothed + 100) * 0.8)))
    bar = "#" * bar_len
    return (
        f"{when:%H:%M:%S}  raw {raw_rssi:>4}  avg {smoothed:>6.1f} dBm  "
        f"~{approx:5.1f}m  |{bar:<40}|"
    )


async def watch(device_id: str, interval: float) -> None:
    """Live hot/cold meter for a single device id, one line per packet."""
    history = defaultdict(list)

    def on_detection(device, adv_data):
        if str(device.address) != device_id:
            return
        if not is_valid_rssi(adv_data.rssi):
            return
        history[device_id].append(adv_data.rssi)
        window = history[device_id][-5:]
        smoothed = sum(window) / len(window)
        print(format_watch_line(datetime.now(), adv_data.rssi, smoothed), flush=True)

    log.info("watching %s (Ctrl-C to stop)", device_id)
    scanner = BleakScanner(detection_callback=on_detection)
    async with scanner:
        while True:
            await asyncio.sleep(interval)


async def measure(device_id: str, duration: int) -> tuple[int, int]:
    """Sample one device's RSSI for `duration` seconds. Returns (median, count)."""
    samples = []

    def on_detection(device, adv_data):
        if str(device.address) != device_id:
            return
        if is_valid_rssi(adv_data.rssi):
            samples.append(adv_data.rssi)

    log.info("measuring %s for %ds", device_id, duration)
    scanner = BleakScanner(detection_callback=on_detection)
    async with scanner:
        await asyncio.sleep(duration)

    if not samples:
        raise RuntimeError(
            f"no packets from {device_id} in {duration}s. The key may have rotated; "
            "re-run sweep mode to get the current device id."
        )
    ordered = sorted(samples)
    return ordered[len(ordered) // 2], len(samples)


# Find My keys rotate roughly every 15 minutes. Readings older than this are
# very unlikely to belong to the same advertised identity.
STALE_READING_SECONDS = 20 * 60


def load_readings() -> list[dict]:
    if not os.path.exists(READINGS_PATH):
        return []
    try:
        with open(READINGS_PATH) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("could not read %s: %s", READINGS_PATH, exc)
        print(
            f"error: {READINGS_PATH} is unreadable ({exc}). Fix or delete it, or "
            "re-record with --reset.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not isinstance(data, list):
        print(f"error: {READINGS_PATH} does not contain a list.", file=sys.stderr)
        sys.exit(2)
    return data


def select_readings(saved: list[dict], device: str | None) -> list[dict]:
    """Pick one device's readings. Never fuse readings from different emitters.

    A mid-session key rotation gives the same physical emitter a new id, so
    readings.json can legitimately hold several. Trilaterating across them would
    fuse RSSI from different sources into one confident, wrong fix.
    """
    by_device = defaultdict(list)
    for r in saved:
        by_device[r.get("device", "unknown")].append(r)

    if device:
        if device not in by_device:
            print(
                f"error: no saved readings for device {device}. Present: "
                f"{', '.join(by_device) or 'none'}",
                file=sys.stderr,
            )
            sys.exit(2)
        chosen = device
    else:
        chosen = max(by_device, key=lambda d: len(by_device[d]))
        if len(by_device) > 1:
            others = {d: len(v) for d, v in by_device.items() if d != chosen}
            print(
                f"  WARNING: readings.json holds {len(by_device)} device ids. Using "
                f"{chosen} ({len(by_device[chosen])} readings) and IGNORING {others}.\n"
                f"  Readings from different ids are different emitters and must not "
                f"be fused. Pass --device to choose, or --reset and re-record.\n"
            )

    picked = by_device[chosen]
    now = time.time()
    stale = [r for r in picked if now - r.get("ts", now) > STALE_READING_SECONDS]
    if stale:
        oldest = max(now - r.get("ts", now) for r in stale)
        print(
            f"  WARNING: {len(stale)} of {len(picked)} readings are older than "
            f"{STALE_READING_SECONDS // 60} min (oldest {oldest / 60:.0f} min). Find My "
            f"keys rotate ~every 15 min, so these may be a different emitter.\n"
        )
    return picked


def save_readings(readings: list[dict]) -> None:
    with open(READINGS_PATH, "w") as fh:
        json.dump(readings, fh, indent=2)


def parse_xy(text: str) -> tuple[float, float]:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("position must be 'x,y' in metres")
    return float(parts[0]), float(parts[1])


def parse_reading(text: str) -> tuple[float, float, float]:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("reading must be 'x,y,rssi'")
    return float(parts[0]), float(parts[1]), float(parts[2])


def run_trilaterate(readings: list[tuple[float, float, float]]) -> None:
    from trilaterate import render_map, trilaterate

    if len(readings) < 3:
        print(
            f"error: need at least 3 readings to solve for position and scale, "
            f"got {len(readings)}.",
            file=sys.stderr,
        )
        sys.exit(2)

    result = trilaterate(readings)

    for w in result["warnings"]:
        print(f"  WARNING: {w}")
    if result["warnings"]:
        print()

    cx, cy = result["centre"]
    print(f"  Best estimate:      x={cx:.2f}m  y={cy:.2f}m")
    print(f"  50% confidence:     within {result['r50']:.2f}m of that point")
    print(f"  90% confidence:     within {result['r90']:.2f}m of that point")
    print(f"  Solved samples:     {result['samples_solved']}")
    if result["ambiguous_fraction"] > 0.2:
        print(
            f"  AMBIGUOUS: {result['ambiguous_fraction']:.0%} of samples had a rival "
            "solution. Add a reading from a position off the current line."
        )
    print("\n  Implied distance from each reading position:")
    for (x, y, rssi), dist in zip(readings, result["anchor_distances"]):
        print(f"    ({x:>6.2f}, {y:>6.2f})  rssi={rssi:>6.1f}  ->  {dist:>5.2f}m")
    print()
    print(render_map(result, readings))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["sweep", "watch", "measure", "trilaterate"])
    parser.add_argument("--duration", type=int, default=30, help="sweep seconds")
    parser.add_argument("--device", help="device id for watch/measure mode")
    parser.add_argument("--at", type=parse_xy, help="your position in metres, 'x,y'")
    parser.add_argument(
        "--reading",
        type=parse_reading,
        action="append",
        default=[],
        help="explicit 'x,y,rssi' triple; repeatable. Overrides saved readings.",
    )
    parser.add_argument("--reset", action="store_true", help="clear saved readings")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--apple-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.mode == "sweep":
        seen = asyncio.run(sweep(args.duration, args.apple_only))
        print_report(seen)

    elif args.mode == "watch":
        if not args.device:
            parser.error("watch mode requires --device")
        try:
            asyncio.run(watch(args.device, args.interval))
        except KeyboardInterrupt:
            print("\nstopped")

    elif args.mode == "measure":
        if not args.device or args.at is None:
            parser.error("measure mode requires --device and --at 'x,y'")
        median, count = asyncio.run(measure(args.device, args.duration))
        readings = [] if args.reset else load_readings()
        readings.append(
            {
                "x": args.at[0],
                "y": args.at[1],
                "rssi": median,
                "packets": count,
                "device": args.device,
                "ts": time.time(),
            }
        )
        save_readings(readings)
        print(
            f"recorded: ({args.at[0]}, {args.at[1]}) rssi={median} "
            f"from {count} packets  [{len(readings)} reading(s) saved]"
        )
        if len(readings) >= 3:
            print("\nEnough readings to solve. Run:  ble_sweep.py trilaterate\n")

    elif args.mode == "trilaterate":
        if args.reading:
            readings = args.reading
        else:
            picked = select_readings(load_readings(), args.device)
            if len(picked) < 3:
                parser.error(
                    f"need at least 3 readings for one device, have {len(picked)}. "
                    "Use measure mode or pass --reading 'x,y,rssi' three or more times."
                )
            readings = [(r["x"], r["y"], r["rssi"]) for r in picked]
        run_trilaterate(readings)


if __name__ == "__main__":
    main()
