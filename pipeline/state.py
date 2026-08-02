"""The release queue.

Ingestion and publication are deliberately decoupled. Dates become *available*
on the market calendar, but they are *released* on a randomized schedule that
trails behind by a deep buffer. Because publication is driven by the buffer
rather than by the calendar, a burst of releases can land on any day of the
week, including days on which nothing was published anywhere.

This is a publishing policy, not a data requirement: the dataset would be
identical if every date were released the moment it became available. Its
purpose is to keep the commit history from mirroring the TARGET/BCB business
calendar. The data itself is never altered -- rows keep the date they belong
to, and commit author dates are never rewritten.

Why the buffer is deep
----------------------
The naive version of this -- "release whatever exceeds a random reserve" --
still leaks the weekly cycle. Supply arrives only on weekdays, so the backlog
peaks on Saturday and bottoms out on Monday, and releases follow it: simulated
over two years that produced 2.4x more Saturday commits than Monday ones.

The fix is a proportional controller against a *deep* target. Release pressure
depends on how far the backlog sits from TARGET_BACKLOG, and because the
weekly ripple is small relative to the target, the day-of-week signal damps
below sampling noise. The feedback keeps the queue from drifting.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import random

STATE_PATH = pathlib.Path(__file__).resolve().parents[1] / "data" / "_state.json"

# Oldest date the pipeline will ever try to collect.
HISTORY_START = dt.date(2026, 2, 2)

# Cron slots per day in the workflow. Must match ingest.yml, or the release
# rate will not track supply.
SLOTS_PER_DAY = 5

# Available dates added per week by the market calendar (Mon-Fri).
SUPPLY_PER_WEEK = 5

# Backlog the controller steers toward, in dates. Larger = flatter graph but
# staler publication; ~20 puts the median publish lag around a month.
TARGET_BACKLOG = 20

# Chance a firing slot releases an extra date, applied up to twice. Keeps most
# releases singletons while still producing the occasional 2-3 commit day.
BURST_P = 0.10

# Backstop: past this the controller is overridden, so a long outage cannot
# leave the dataset permanently behind.
MAX_BACKLOG = 60
MAX_PER_RUN = 3

_rng = random.SystemRandom()


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"released": [], "no_data": []}


def save_state(state: dict) -> bool:
    """Write state. Returns True if the file content actually changed."""
    state["released"] = sorted(set(state.get("released", [])))
    state["no_data"] = sorted(set(state.get("no_data", [])))
    rendered = json.dumps(state, indent=2, sort_keys=True) + "\n"
    if STATE_PATH.exists() and STATE_PATH.read_text(encoding="utf-8") == rendered:
        return False
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(rendered, encoding="utf-8")
    return True


_calendar_cache: dict[tuple[dt.date, dt.date], list[dt.date]] = {}


def available_dates(today: dt.date | None = None) -> list[dt.date]:
    """Weekdays from HISTORY_START up to yesterday.

    Today is excluded because neither publisher has closed its books yet;
    fetching it risks recording a provisional number as final.

    Cached on (HISTORY_START, end) rather than with lru_cache, so that tests
    which patch HISTORY_START get their own entry instead of a stale one.
    """
    end = (today or dt.date.today()) - dt.timedelta(days=1)
    key = (HISTORY_START, end)
    if key not in _calendar_cache:
        out, cursor = [], HISTORY_START
        while cursor <= end:
            if cursor.weekday() < 5:
                out.append(cursor)
            cursor += dt.timedelta(days=1)
        _calendar_cache[key] = out
    return _calendar_cache[key]


def backlog(state: dict, today: dt.date | None = None) -> list[dt.date]:
    """Available dates that have neither been released nor found empty."""
    settled = set(state.get("released", [])) | set(state.get("no_data", []))
    return [d for d in available_dates(today) if d.isoformat() not in settled]


def plan_release(
    state: dict,
    today: dt.date | None = None,
    pending: list[dt.date] | None = None,
) -> int:
    """How many dates to publish on this run.

    `pending` may be passed in by a caller that has already computed the
    backlog, which matters in simulations that evaluate thousands of slots.
    """
    if pending is None:
        pending = backlog(state, today)
    if not pending:
        return 0

    if len(pending) > MAX_BACKLOG:
        return min(MAX_PER_RUN, len(pending))

    # Per-slot firing rate that releases SUPPLY_PER_WEEK dates per week when
    # the backlog sits exactly on target.
    nominal = SUPPLY_PER_WEEK / (SLOTS_PER_DAY * 7)
    probability = min(0.9, nominal * len(pending) / TARGET_BACKLOG)
    if _rng.random() >= probability:
        return 0

    count = 1
    while count < MAX_PER_RUN and _rng.random() < BURST_P:
        count += 1
    return min(count, len(pending))
