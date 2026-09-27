# Documentation index: beacon-hunt

Passive Bluetooth tools for finding a lost Apple device inside a building. Start with
the [README](../README.md) for usage; this folder holds the architecture and build
records.

Status: current. Last updated 27 Sep 2026.

## Conventions

- Dated docs (a plan, a decision, a point-in-time record): `DDMMYYYY_topic.md`, for
  example `27092026_c4_index_ci_plan.md`. A plan doc is kept updated while its work
  is in progress; after that, dated docs are not rewritten. If the facts change, add
  a new dated doc and link it.
- Evergreen docs (describe something that keeps changing with the code): `topic.md`,
  for example `c4model.md`. Keep them current.
- Every doc opens with its purpose, status and last-updated date.
- `c4model.md` is the architecture source of truth. Read it before an architecture
  change and update it for every change to containers, components, dependencies or
  data flows.
- `docs/` is gitignored except for files allow-listed in `../.gitignore`, because the
  hunt's private notes (MAC addresses, IPs, hostnames, household device names) live
  here too. Add a `!docs/<file>` line there for every new public doc, and register it
  in the table below.

## Categories

| Category | Required sections |
|---|---|
| Architecture | Context, Containers, Components, Data flows, Change log |
| Plan | Goal, Scope, Decisions, Status (plus Deviations when the work departed from the plan) |

## Documents

| Path | Category | Description | Date |
|---|---|---|---|
| [c4model.md](c4model.md) | Architecture | Context, containers, components and data flows of the sweep CLI, the dashboard and the two shared modules, plus tests and CI. Evergreen. | Created 27 Sep 2026 |
| [27092026_c4_index_ci_plan.md](27092026_c4_index_ci_plan.md) | Plan | Adding this index, the C4 model and the GitHub Actions test workflow; why `docs/` uses an allow-list and why CI runs on Linux. | 27 Sep 2026 |
