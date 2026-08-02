# brl-eur-monitor

A daily record of the BRL–EUR corridor, assembled from two central banks that
quote the same pair independently.

| Table                 | Contents                                                       |
|-----------------------|----------------------------------------------------------------|
| `fx_rates.csv`        | EUR→BRL/USD/GBP/CHF from the ECB; USD/BRL and EUR/BRL PTAX from the BCB |
| `indicators.csv`      | Selic target (% p.a.) and IPCA (% m/m), from BCB SGS            |
| `reconciliation.csv`  | Derived: ECB vs PTAX EUR/BRL, absolute and percentage spread     |

The reconciliation table is the reason these sources belong in one repo. The
ECB fixes its reference rates from a 14:15 CET concertation; the BCB computes
PTAX from a different panel and window. Both are correct, and they disagree —
so the spread between them is a real, non-trivial series that neither publisher
provides. Everything else exists to produce it.

Neither API requires a key.

## Layout

```
data/
  raw/<source>/<yyyy>/<mm>/<date>.json   archived responses, never rewritten
  curated/<table>.csv                    tidy, deduplicated, key-sorted
  _state.json                            which dates are published
pipeline/
  sources.py    ECB + BCB SGS fetchers, one row-set per date
  state.py      the release queue and its controller
  run.py        plan / release / seed / status
```

## Running it

Dependencies are managed with [uv](https://docs.astral.sh/uv/) and pinned in
`uv.lock`. There is no `requirements.txt` and no manual venv step.

```bash
uv sync --all-groups        # creates .venv from the lockfile
uv run pytest
uv run ruff check .
```

```bash
uv run monitor status               # queue depth, latest published
uv run monitor plan                 # how many dates this run would publish
uv run monitor release              # publish the oldest pending date
uv run monitor seed --hold 20       # backfill history, keep a tail queued
uv run monitor seed --limit 100     # resume a long backfill in chunks
```

`monitor` is the console script declared in `pyproject.toml`;
`uv run python -m pipeline.run ...` is equivalent.

## The release policy

Dates become available on the Mon–Fri market calendar, but they are not
published when they arrive. They enter a queue, and a proportional controller
drains it toward a target depth of 20 working days. Each of the five daily cron
slots rolls independently; most do nothing.

**This is cosmetic, and worth being honest about.** The dataset would be
identical if every date were published immediately. The lag exists so the
commit history does not trace the TARGET/BCB business calendar. What it does
*not* do is invent anything: rows keep the date they belong to, nothing is
published that was not fetched, and commit author dates are never backdated.
Backdating would be fabricating a history that did not happen; this only
chooses when real work is pushed.

The naive version of this leaks anyway. Releasing whatever exceeds a random
reserve still tracks the weekly supply cycle — the backlog peaks Saturday and
bottoms out Monday, and over a simulated two years that produced **2.4× more
Saturday commits than Monday ones**. Steering against a deep target instead
damps the weekly ripple below sampling noise. Simulated over three years with
the shipped controller:

```
Mon 13.4%  Tue 14.4%  Wed 14.3%  Thu 14.1%  Fri 14.3%  Sat 15.3%  Sun 14.4%
residual weekday bias 1.14x   |   50% of days quiet   |   1-3 commits on active days
median publish lag 27 days    |   backlog stable at ~17 against a target of 20
```

Those shares are pooled over 25 independent two-year simulations, about 13,000
commits. That pooling matters: a *single* two-year run yields roughly 500
commits, only ~75 per weekday, and its max/min ratio swings between 1.3 and 2.0
on counting noise alone. An earlier draft of this README quoted 1.30x from one
such run as though it were a property of the controller. It was a draw. The
honest number is 1.14x, and it is still faintly Monday-low / Saturday-high --
the weekly cycle is damped, not eliminated. For comparison, real developers'
graphs are far more weekday-skewed than either figure.

`tests/test_pipeline.py` asserts on the pooled figure and separately checks
that the controller beats the naive reserve scheduler by a wide margin, so the
design decision itself is guarded rather than just its current output.

Tuning knobs in `state.py`: raise `TARGET_BACKLOG` for a flatter graph at the
cost of staler publication; raise `BURST_P` for more multi-commit days and
fewer active days. If you change the cron count in the workflow, change
`SLOTS_PER_DAY` to match or the release rate stops tracking supply.

Roughly half of all days will have no commits. That is the ceiling: five
working days of data per week means about five commits per week, and no amount
of scheduling makes 260 commits fill 365 squares. A graph with gaps also looks
considerably more like a person than one with none.

## Setup

1. Push to a **public, non-fork** repo with `main` as the default branch.
2. Settings → Emails: copy your `ID+username@users.noreply.github.com` address.
3. Repo Settings → Secrets and variables → Actions:
   - `GIT_AUTHOR_NAME`
   - `GIT_AUTHOR_EMAIL` — the noreply address above
4. Settings → Actions → General → Workflow permissions → **Read and write**.
5. Actions → `monitor` → Run workflow → mode `seed`. Start with
   `limit = 100` to confirm both APIs respond as expected before committing to
   the full history from `HISTORY_START` in `pipeline/state.py`.

## Caveats

- Commits only count toward the contribution graph when the author email
  belongs to your account and they land on the default branch of a non-fork
  repo. The default `github-actions[bot]` identity credits the bot instead —
  hence the two secrets.
- Verified against live endpoints on 2026-08-02: Frankfurter `/v1` returned
  EUR/BRL 5.8535 for 2026-07-30, identical to the ECB Data Portal series
  `EXR.D.BRL.EUR.SP00.A`, confirming `/v1` serves official reference rates
  rather than the blended rates the newer `/v2` API describes. SGS series
  **21619** returned 5.8467 the same day — the euro selling rate, distinct from
  series 1 (USD/BRL, 5.0739). If you change either endpoint, re-run that
  comparison; the codes are in one dict at the top of `sources.py`.
- The ECB fixes its reference rates at 14:15 CET; the BCB computes PTAX from a
  different panel and window, so a spread of roughly ±0.1-0.3% is normal. A
  spread near zero, or one above a percent or two, means something is wrong
  with the series mapping rather than with the market.
- GitHub disables scheduled workflows after 60 days of repository inactivity,
  and the workflow's own pushes do not reliably reset that timer. Push
  something by hand every couple of months.
- Cron on shared runners is best effort; slots get delayed or dropped. The
  queue absorbs it — a missed run just means a slightly deeper backlog.
- Today's date is never fetched: neither publisher has closed its books, and a
  provisional number recorded as final is a silent data error.
