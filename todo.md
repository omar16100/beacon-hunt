# todo

Change log and open items for beacon-hunt. Newest first.

## 27 Sep 2026: docs index, C4 model, CI

Plan: [docs/27092026_c4_index_ci_plan.md](docs/27092026_c4_index_ci_plan.md)

- [x] Ran `uv run pytest`: 78 collected, 78 passed. README count confirmed.
- [x] Added `docs/index.md` (naming, categories, doc table).
- [x] Added `docs/c4model.md` from the code at `6bd437d`.
- [x] Added the plan doc and registered both docs in `docs/index.md`.
- [x] `.gitignore`: `docs/` is now `docs/*` with an allow-list for the public docs.
- [x] Added `.github/workflows/ci.yml` (ubuntu-latest, `uv sync --locked`,
      `uv run pytest`). No test needed skipping: none opens a Bluetooth scanner.
- [x] README: rotation figure now points at the write-up; test line notes no
      Bluetooth hardware is needed.
- [x] `ble_web.py` docstring: it said nothing is written to disk, but by default it
      appends readings to `hunt_logs/readings.jsonl` and logs to
      `hunt_logs/ble_web.log`. Docstring corrected, no code change.

## Open

- [ ] README calls the tools "passive". They never connect, pair or write, but
      `BleakScanner` runs with bleak's default `scanning_mode="active"`. Decide
      whether to reword or to pass `scanning_mode="passive"` where the OS supports it
      (bleak documents passive scanning as unsupported on macOS).
- [ ] `ble_sweep.py` says Find My keys rotate "roughly every 15 minutes" while
      `ble_web.py` and the README say "every few minutes". Pick one wording.
