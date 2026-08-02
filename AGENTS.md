# AGENTS.md

Working notes for AI agents in this repo. The README explains the project to a
human reader; this file records the things that are easy to break silently.

## What this is

A scheduled pipeline that records the BRL–EUR corridor from two central banks
and publishes the result to git on a randomized schedule. Two moving parts:

1. **Ingestion** — fetch a date from the ECB and the Banco Central do Brasil,
   archive the raw payloads, upsert tidy rows into per-table CSVs.
2. **Release** — a queue that decides *when* already-collected dates get
   committed, deliberately decorrelated from when they were published.

The second part is cosmetic and the repo says so out loud. Keep it that way; do
not quietly re-describe it as a data requirement.

## Layout

```
pipeline/sources.py   ECB + BCB SGS fetchers; one row-set per date
pipeline/state.py     release queue and its controller
pipeline/run.py       CLI: plan / release / seed / status
tests/                pytest, network stubbed
data/                 committed output; raw/, curated/, _state.json
```

## Commands

Dependencies are managed with uv. There is no `requirements.txt`, no manual
venv step, and no `pip install` in this project.

```bash
uv sync --all-groups
uv run pytest
uv run ruff check .
uv run ruff format .
uv run monitor status
```

Before any commit: `uv run ruff check . && uv run ruff format --check . && uv run pytest`.

## Hard invariants

Breaking any of these produces a repo that looks fine and is wrong.

- **A no-op run must leave the working tree byte-identical.** The workflow
  commits whenever `git diff --cached` is non-empty, so anything that writes a
  wall-clock timestamp, reorders a CSV, or rewrites JSON with different key
  ordering turns every scheduled run into a commit. `save_state()` compares
  rendered output before writing for exactly this reason. Guarded by
  `test_rerun_writes_nothing`.
- **Rows keep the date they belong to.** Frankfurter answers a non-publication
  date with the *previous* working day's rates. `fetch_ecb` checks the returned
  date and returns no rows on a mismatch. Never relax that check. Guarded by
  `test_ecb_rows_are_not_misdated_on_a_holiday`.
- **Commit author dates are never rewritten.** Backdating to fill past squares
  is fabricating history that did not happen. The release lag only chooses when
  real work is pushed. Do not add `--date`, `GIT_AUTHOR_DATE`, or any rebase
  that rewrites timestamps.
- **`SLOTS_PER_DAY` in `state.py` must equal the number of cron entries in
  `.github/workflows/ingest.yml`.** The controller derives its per-slot firing
  rate from it. Change one, change the other, or the release rate stops
  tracking supply.
- **Today's date is never fetched.** Neither publisher has closed its books;
  a provisional number stored as final is a silent data error.
- **Bash steps run under `bash -e`.** `[ cond ] && exit 0` fails the step when
  the condition is false. Use `if ... then ... fi`. This bug was already
  shipped once.

## Verified facts

Checked against live endpoints on 2026-08-02. If you change an endpoint,
re-verify and update this block.

| Thing | Value |
|---|---|
| ECB EUR/BRL, 2026-07-30, `api.frankfurter.dev/v1` | 5.8535 |
| Same date, ECB Data Portal `EXR.D.BRL.EUR.SP00.A` | 5.8535 (identical) |
| BCB SGS **21619** (euro selling) | 5.8467 |
| BCB SGS **1** (USD/BRL) | 5.0739 |
| Resulting spread | −0.116% |

Notes:

- `api.frankfurter.app` 301-redirects to `api.frankfurter.dev`; paths are
  versioned (`/v1/{date}`). The `/v2` API serves *blended* multi-provider
  rates — `/v1` matches official ECB reference rates exactly, which is what
  makes the reconciliation table meaningful. Do not migrate to `/v2` without
  re-running the ECB comparison.
- SGS returns **404**, not an empty list, for a window with no data. Holidays
  and weekends look like errors and must not be treated as failures.
- A normal ECB-vs-PTAX spread is roughly ±0.1–0.3%. A spread near zero, or
  above one percent, means the series mapping is wrong, not the market.

## Statistics discipline

The release controller's weekday distribution is noisy. A single two-year
simulation yields ~500 commits, ~75 per weekday, and its max/min ratio swings
between 1.3 and 2.0 on counting noise alone. An earlier README quoted 1.30x
from one run as though it were a property of the design; the pooled figure over
25 runs is **1.14x**.

So: never assert on, or report, a single simulation run. Pool independent runs
and say how many. `test_release_schedule_does_not_track_the_market_calendar`
pools 8; `test_controller_beats_the_naive_reserve_scheduler` guards the design
choice rather than its current output.

Simulation tests evaluate ~30k slots, so keep `available_dates()` cached and
pass a precomputed `pending` into `plan_release()`. Removing either takes the
suite from 10s to 55s.

## Adding a source

1. Write `fetch_x(day) -> (payload, rows)` in `sources.py`, returning `[]` when
   nothing was published for that date.
2. Add a `Source(...)` entry and, if it needs a new table, a `KEYS` entry.
3. No API keys. Both current sources are keyless and it should stay that way;
   a secret in this repo means the pipeline stops working for anyone who forks
   it.
4. Add a fixture branch to `fake_get` in the tests, including the upstream's
   no-data behaviour.

`run.py` should not need changes.

## Conventions

- Python ≥3.12, ruff (line length 100), config in `pyproject.toml`.
- Comments explain *why*, especially where the code works around an upstream
  quirk. Do not strip those — several encode bugs that already bit.
- Prefer editing `state.py` constants over adding new tuning knobs.

## Gotchas that are not bugs

- `plan` returning 0 for weeks after seeding is correct: the queue fills to
  `TARGET_BACKLOG` before it starts releasing.
- ~50% of days have no commits. That is the ceiling — five working days of data
  per week cannot fill seven squares. Do not "fix" it by inventing data.
- GitHub disables scheduled workflows after 60 days of repo inactivity, and the
  workflow's own pushes do not reliably reset that timer.
