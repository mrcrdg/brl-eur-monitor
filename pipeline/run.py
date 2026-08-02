"""CLI for the BRL-EUR corridor monitor.

    python -m pipeline.run plan              -> prints how many dates to release
    python -m pipeline.run release           -> publishes the oldest pending date
    python -m pipeline.run seed --hold 15    -> backfills history, holding a tail back
    python -m pipeline.run status

`release` publishes exactly one date and is meant to be called in a loop, one
git commit per call. Everything is idempotent and keyed, so re-running over a
date already present writes nothing and leaves the tree clean.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
import time

from .sources import KEYS, SOURCES
from .state import (
    HISTORY_START,
    available_dates,
    backlog,
    load_state,
    plan_release,
    save_state,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
CURATED_DIR = ROOT / "data" / "curated"


def archive_raw(source_name: str, day: dt.date, payload: dict) -> None:
    partition = RAW_DIR / source_name / f"{day.year:04d}" / f"{day.month:02d}"
    path = partition / f"{day.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def upsert(table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    key = KEYS[table]
    path = CURATED_DIR / f"{table}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            existing = list(csv.DictReader(handle))

    index = {tuple(row[col] for col in key): row for row in existing}
    added = 0
    for row in rows:
        stringified = {col: str(value) for col, value in row.items()}
        identity = tuple(stringified[col] for col in key)
        if identity in index:
            continue
        index[identity] = stringified
        added += 1

    if added == 0:
        return 0

    merged = sorted(index.values(), key=lambda row: tuple(row[col] for col in key))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(merged)
    return added


def collect(day: dt.date) -> dict[str, list[dict]]:
    """Fetch every source for one date. A failing source aborts the date."""
    by_table: dict[str, list[dict]] = {}
    for source in SOURCES:
        payload, rows = source.fetch(day)
        archive_raw(source.name, day, payload)
        by_table.setdefault(source.table, []).extend(rows)
    return by_table


def rebuild_reconciliation() -> None:
    """Compare the two independent EUR/BRL quotes, per date."""
    fx_path = CURATED_DIR / "fx_rates.csv"
    if not fx_path.exists():
        return

    with fx_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    quotes: dict[str, dict[str, float]] = {}
    for row in rows:
        if row["base"] == "EUR" and row["quote"] == "BRL":
            quotes.setdefault(row["date"], {})[row["publisher"]] = float(row["rate"])

    out = []
    for date in sorted(quotes):
        pair = quotes[date]
        ecb, ptax = pair.get("ecb"), pair.get("bcb_ptax")
        if ecb is None or ptax is None:
            continue  # only one publisher has reported for this date
        out.append(
            {
                "date": date,
                "ecb_eur_brl": f"{ecb:.6f}",
                "ptax_eur_brl": f"{ptax:.6f}",
                "spread_brl": f"{ptax - ecb:+.6f}",
                "spread_pct": f"{(ptax - ecb) / ecb * 100:+.4f}",
            }
        )

    if not out:
        return
    path = CURATED_DIR / "reconciliation.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(out[0].keys()))
        writer.writeheader()
        writer.writerows(out)


def release_one(state: dict, today: dt.date | None = None) -> tuple[dt.date | None, int]:
    """Publish the oldest pending date that actually has data.

    Dates the publishers skipped (holidays) are recorded as empty and stepped
    over within the same run, so a holiday never stalls the queue.
    """
    for day in backlog(state, today):
        by_table = collect(day)
        added = sum(upsert(table, rows) for table, rows in by_table.items())
        if added == 0:
            state.setdefault("no_data", []).append(day.isoformat())
            continue
        state.setdefault("released", []).append(day.isoformat())
        rebuild_reconciliation()
        return day, added
    return None, 0


def cmd_plan(args: argparse.Namespace) -> int:
    state = load_state()
    pending = backlog(state)
    count = plan_release(state, pending=pending)
    print(count)
    print(f"backlog={len(pending)} release={count}", file=sys.stderr)
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    state = load_state()
    day, added = release_one(state)
    changed = save_state(state)
    if day is None:
        print("nothing releasable" if changed else "nothing releasable, no state change")
        return 0
    print(f"released {day.isoformat()} rows={added}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """Backfill history in one pass, leaving `hold` dates in the queue.

    Author dates are left alone: backdating commits to paint past squares
    would be fabricating a history that did not happen.
    """
    state = load_state()
    settled = set(state.get("released", [])) | set(state.get("no_data", []))
    targets = [d for d in available_dates() if d.isoformat() not in settled]
    if args.hold:
        targets = targets[: -args.hold] if args.hold < len(targets) else []
    if args.limit:
        targets = targets[: args.limit]

    for index, day in enumerate(targets):
        if index and args.sleep:
            time.sleep(args.sleep)  # both APIs are free; do not hammer them
        try:
            by_table = collect(day)
        except Exception as exc:
            # Stop rather than skip: a gap in the middle of history is worse
            # than a short history, and the next pass resumes from here.
            print(f"{day} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            break
        added = sum(upsert(table, rows) for table, rows in by_table.items())
        bucket = "released" if added else "no_data"
        state.setdefault(bucket, []).append(day.isoformat())
        print(f"{day} {bucket} rows={added}")

    rebuild_reconciliation()
    save_state(state)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state()
    pending = backlog(state)
    released = sorted(state.get("released", []))
    print(f"history start   : {HISTORY_START}")
    print(f"released dates  : {len(released)}")
    print(f"latest released : {released[-1] if released else '-'}")
    print(f"empty dates     : {len(state.get('no_data', []))}")
    print(f"backlog         : {len(pending)}")
    if pending:
        print(f"oldest pending  : {pending[0]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BRL-EUR corridor monitor.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan").set_defaults(func=cmd_plan)
    sub.add_parser("release").set_defaults(func=cmd_release)
    sub.add_parser("status").set_defaults(func=cmd_status)
    seed = sub.add_parser("seed")
    seed.add_argument("--hold", type=int, default=15, help="dates to leave unreleased")
    seed.add_argument("--limit", type=int, default=0, help="max dates this pass (0 = all)")
    seed.add_argument("--sleep", type=float, default=0.4, help="pause between dates, seconds")
    seed.set_defaults(func=cmd_seed)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
