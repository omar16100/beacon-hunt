# beacon-hunt

Passive Bluetooth tools for finding a lost Apple device inside a building, when
Find My has already told you everything it knows and that turns out to be a
street address.

Built in an evening to find a flat iPhone. It worked: five rooms, 120 packets,
and a final answer good to about a metre. Write-up:
[Find My found my house. Bluetooth found the pillow](https://omarshabab.com/lost-iphone-bluetooth-hunt/).

## What it does

A phone that is offline or out of battery keeps broadcasting a Find My beacon,
and any Mac can hear it. These tools filter a room full of Bluetooth noise down
to the beacons that are *separated* from their owner, then help you work out
which room one of them is in by walking around with a laptop.

```sh
uv run python ble_sweep.py sweep --duration 60      # census of every advertiser
uv run python ble_sweep.py watch --device <ID>      # live meter for one beacon
uv run python ble_web.py                            # browser dashboard, recommended
```

The dashboard is the one to use. Open <http://127.0.0.1:8765>, type the room you
are standing in, walk to the next room, type that. It ranks the rooms for you.

| File | What it is |
|---|---|
| `apple_ble.py` | Decoder for Apple's Continuity and Find My advertisement payloads |
| `ble_sweep.py` | CLI: `sweep`, `watch`, `measure`, `trilaterate` |
| `ble_web.py` | Local dashboard: auto-reacquiring tracker, room comparison, replay |
| `trilaterate.py` | Least-squares position solve with calibrated confidence bands |

## The one thing worth stealing

**Do not track a Find My beacon by its device id.** The key rotates every few
minutes and the identifier the OS gives you is derived from it, so your target
silently retires while the meter carries on displaying a number that will never
update again. That failure mode cost me the first version of this.

Track the *strongest separated beacon* instead, whatever it currently calls
itself. A rotated beacon is still a separated beacon, so the new identity takes
over on its own. `ble_web.py` followed four rotations in one evening without
noticing any of them (details in the write-up linked above).

## What it cannot do

Stated up front because they are structural, not bugs:

- **No MAC addresses.** macOS gives out a per-host UUID, so nothing here can be
  matched against a device you have paired.
- **No attribution, ever.** Find My keys are encrypted and rotate. No passive
  scan can establish that a beacon is *yours*. What you get is co-location: the
  strongest separated beacon was here, and your phone turned out to be here too.
- **No real distance.** The advertisement carries no transmit power, so the
  metres figure rests on an assumed constant. Read the dBm, ignore the metres.

## Requires

macOS, Python 3.12, and [uv](https://docs.astral.sh/uv/). `uv run` handles the
dependencies. Grant Bluetooth permission to your terminal when prompted: it is
per-application, and an SSH session cannot get it, so run these from a terminal
you are sitting in front of.

## Tests

```sh
uv run pytest        # 78 tests, no Bluetooth hardware needed
```

## Licence

MIT
