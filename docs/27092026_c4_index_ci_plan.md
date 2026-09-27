# Plan: docs index, C4 model and CI (27 Sep 2026)

Purpose: build record for adding `docs/index.md`, `docs/c4model.md` and a minimal
GitHub Actions workflow to beacon-hunt.

Status: done on branch `docs/c4-index-ci`, pending review and merge. Last updated
27 Sep 2026.

## Goal

- Give the repo a documentation index and an architecture source of truth that
  match the code as it is today.
- Add CI so every push to `main` and every pull request runs the existing test
  suite, making "green CI" meaningful for later changes.
- Confirm the README's test count against a real run.

## Scope

In scope:

- `docs/index.md`, `docs/c4model.md`, this plan doc.
- `.github/workflows/ci.yml` (checkout, setup-uv, `uv sync --locked`,
  `uv run pytest`).
- `.gitignore`: allow-list the three public docs above.
- `todo.md` at the repo root.
- README: one attribution for a figure from the hunt, and a note that the tests
  need no Bluetooth hardware.
- `ble_web.py` module docstring: correct the claim that nothing is written to disk.

Out of scope: any behaviour change to the tools or tests, packaging, releases.

## Decisions

1. **Keep private docs ignored.** `.gitignore` excluded all of `docs/` because the
   hunt's internal notes hold MAC addresses, IPs, hostnames and household device
   names. Rather than un-ignoring the folder, the rule becomes `docs/*` plus an
   explicit `!docs/<file>` line per public doc, so a stray private note is still
   ignored by default.
2. **ubuntu-latest, not macOS.** No test constructs a `BleakScanner`, and bleak
   imports its CoreBluetooth or BlueZ backend only when a scanner is created. The
   suite therefore needs no Bluetooth hardware, permission or macOS frameworks, and
   no test had to be skipped or marked for CI.
3. **Action pins.** `actions/checkout@v7` (latest major). `astral-sh/setup-uv` has no
   floating major tag for its current release line, so it is pinned to the v10.2.0
   release commit SHA, the style the action's own README uses.
4. **`uv sync --locked` before tests.** Fails the build if `uv.lock` drifts from
   `pyproject.toml`, instead of silently re-locking in CI.
5. **README test count.** A local `uv run pytest` on 27 Sep 2026 collected and passed
   78 tests, so the README figure stands unchanged.
6. **README hunt figures.** "Five rooms, 120 packets, about a metre" and "four
   rotations" come from the hunt, whose logs are deliberately not in the repo. The
   linked write-up states both (checked 27 Sep 2026), so they stay, with the
   rotation claim now pointing at the write-up too.

## Status

| Step | State |
|---|---|
| Read code, run tests (78 passed) | Done |
| docs/index.md, docs/c4model.md, plan doc, todo.md | Done |
| CI workflow, verified by running its commands locally | Done |
| Codex review (no blocker or major; minor and nit findings applied, one rejected), re-review clean | Done |
| gitleaks (git and dir) and personal-data grep over the outgoing diff and log | Clean |
| PR, CI green, squash merge, CI green on `main` | In the PR |
| GitHub topics | After merge |

## Deviations

- The task assumed docs could simply be added. `.gitignore` ignored the whole
  `docs/` folder, so decision 1 was needed first.
