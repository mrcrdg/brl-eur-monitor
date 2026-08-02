"""Tests for the ingestion pipeline and the release controller.

Network calls are stubbed. The point is not to test that requests works, but
that the invariants the scheduler depends on hold:

  * a re-run over settled dates writes nothing (no empty commits)
  * a date the publishers skipped never stalls the queue
  * the derived table only joins dates where both publishers reported
  * the release controller does not leak the weekly market calendar
"""

from __future__ import annotations

import collections
import datetime as dt
import json

import pytest

from pipeline import run as run_mod
from pipeline import sources as sources_mod
from pipeline import state as state_mod

HOLIDAYS = {dt.date(2026, 7, 9)}
ECB_BRL = 5.8535
PTAX_EUR = 5.8467


class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def fake_get(url: str, params=None, **_kwargs):
    """Mimics both upstreams, including their real quirks."""
    if "frankfurter" in url:
        day = dt.date.fromisoformat(url.rsplit("/", 1)[-1])
        if day in HOLIDAYS:
            # Frankfurter answers a non-publication date with the prior day.
            prior = (day - dt.timedelta(days=1)).isoformat()
            return FakeResponse(200, {"base": "EUR", "date": prior, "rates": {"BRL": ECB_BRL}})
        return FakeResponse(
            200,
            {
                "amount": 1.0,
                "base": "EUR",
                "date": day.isoformat(),
                "rates": {"BRL": ECB_BRL, "CHF": 0.9324, "GBP": 0.85715, "USD": 1.1476},
            },
        )

    code = int(url.split("sgs.")[1].split("/")[0])
    day = dt.datetime.strptime(params["dataInicial"], "%d/%m/%Y").date()
    # SGS returns 404 for an empty window rather than an empty list.
    if day in HOLIDAYS:
        return FakeResponse(404, None)
    if code == 433 and day.day != 1:  # IPCA is monthly
        return FakeResponse(404, None)
    value = {1: "5.0739", 21619: f"{PTAX_EUR:.7f}", 432: "15.00", 433: "0.24"}[code]
    return FakeResponse(200, [{"data": params["dataInicial"], "valor": value}])


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Point every write at a temp dir and stub the network."""
    monkeypatch.setattr(sources_mod.requests, "get", fake_get)
    monkeypatch.setattr(run_mod, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(run_mod, "CURATED_DIR", tmp_path / "curated")
    monkeypatch.setattr(state_mod, "STATE_PATH", tmp_path / "_state.json")
    monkeypatch.setattr(state_mod, "HISTORY_START", dt.date(2026, 7, 6))
    return tmp_path


def read_csv(path):
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------


def test_seed_populates_all_three_tables(project):
    run_mod.main(["seed", "--hold", "0", "--sleep", "0"])
    curated = project / "curated"
    assert {p.name for p in curated.iterdir()} == {
        "fx_rates.csv",
        "indicators.csv",
        "reconciliation.csv",
    }
    fx = read_csv(curated / "fx_rates.csv")
    assert {row["publisher"] for row in fx} == {"ecb", "bcb_ptax"}


def test_rerun_writes_nothing(project):
    """The invariant the whole scheduler rests on: no diff, no commit."""
    run_mod.main(["seed", "--hold", "0", "--sleep", "0"])
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}

    state = state_mod.load_state()
    day, added = run_mod.release_one(state)
    state_mod.save_state(state)

    assert day is None and added == 0
    after = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert before == after


def test_holiday_is_recorded_and_does_not_stall_the_queue(project):
    run_mod.main(["seed", "--hold", "0", "--sleep", "0"])
    state = json.loads((project / "_state.json").read_text())
    holiday = next(iter(HOLIDAYS)).isoformat()

    assert holiday in state["no_data"]
    assert holiday not in state["released"]
    # The queue moved past it rather than stopping there.
    assert max(state["released"]) > holiday


def test_ecb_rows_are_not_misdated_on_a_holiday(project):
    """Frankfurter echoes the prior day; that must not be filed under today."""
    _payload, rows = sources_mod.fetch_ecb(next(iter(HOLIDAYS)))
    assert rows == []


def test_reconciliation_joins_both_publishers(project):
    run_mod.main(["seed", "--hold", "0", "--sleep", "0"])
    rows = read_csv(project / "curated" / "reconciliation.csv")
    assert rows
    for row in rows:
        assert float(row["ecb_eur_brl"]) == pytest.approx(ECB_BRL)
        assert float(row["ptax_eur_brl"]) == pytest.approx(PTAX_EUR)
        # Real-world spread sits well inside a percent.
        assert abs(float(row["spread_pct"])) < 1.0


def test_hold_leaves_a_backlog(project):
    run_mod.main(["seed", "--hold", "5", "--sleep", "0"])
    assert len(state_mod.backlog(state_mod.load_state())) >= 5


# --------------------------------------------------------------------------
# Release controller
# --------------------------------------------------------------------------


def test_empty_queue_releases_nothing():
    state = {"released": [], "no_data": []}
    today = state_mod.HISTORY_START
    assert state_mod.plan_release(state, today=today) == 0


def test_backstop_overrides_the_controller():
    """A long outage must not leave the dataset permanently behind."""
    state = {"released": [], "no_data": []}
    today = state_mod.HISTORY_START + dt.timedelta(days=400)
    assert state_mod.plan_release(state, today=today) == state_mod.MAX_PER_RUN


def _simulate(plan_fn, days: int = 760) -> collections.Counter[int]:
    """Run the workflow's slot loop against a release policy."""
    state = {"released": [], "no_data": []}
    by_weekday: collections.Counter[int] = collections.Counter()
    day = state_mod.HISTORY_START + dt.timedelta(days=30)
    end = state_mod.HISTORY_START + dt.timedelta(days=days)
    while day <= end:
        for _ in range(state_mod.SLOTS_PER_DAY):
            pending = state_mod.backlog(state, today=day)
            count = plan_fn(state, day, pending)
            for released in pending[:count]:
                state["released"].append(released.isoformat())
                by_weekday[day.weekday()] += 1
        day += dt.timedelta(days=1)
    return by_weekday


def _naive_plan(state, day, pending):
    """The rejected design: release whatever exceeds a random reserve."""
    reserve = state_mod._rng.randint(3, 12)
    releasable = max(0, len(pending) - reserve)
    if not releasable:
        return 0
    return min(releasable, state_mod._rng.choices((0, 1, 2, 3), weights=(55, 25, 15, 5))[0])


def _skew(counter: collections.Counter[int]) -> float:
    total = sum(counter.values())
    shares = [counter[i] / total for i in range(7)]
    return max(shares) / min(shares)


def test_release_schedule_does_not_track_the_market_calendar():
    """Commits must not cluster on any weekday.

    Supply arrives Mon-Fri only, so the backlog peaks Saturday and bottoms out
    Monday; a scheduler driven by instantaneous backlog inherits that cycle.

    This pools several independent runs on purpose. A single two-year sample
    yields only ~500 commits, about 75 per weekday, so its max/min ratio
    swings between roughly 1.3 and 2.0 on noise alone -- asserting on one run
    tests the random seed, not the controller.
    """
    pooled: collections.Counter[int] = collections.Counter()
    for _ in range(8):
        pooled += _simulate(lambda s, d, p: state_mod.plan_release(s, today=d, pending=p))

    assert sum(pooled.values()) > 3000, "controller released far too little"
    assert _skew(pooled) < 1.30, f"residual weekday bias too high: {pooled}"


def test_controller_beats_the_naive_reserve_scheduler():
    """Guards the design decision itself, not just its current output."""
    pooled_naive: collections.Counter[int] = collections.Counter()
    pooled_real: collections.Counter[int] = collections.Counter()
    for _ in range(8):
        pooled_naive += _simulate(_naive_plan)
        pooled_real += _simulate(lambda s, d, p: state_mod.plan_release(s, today=d, pending=p))

    assert _skew(pooled_real) < _skew(pooled_naive) / 1.5


def test_queue_stays_bounded():
    state = {"released": [], "no_data": []}
    day = state_mod.HISTORY_START + dt.timedelta(days=30)
    end = state_mod.HISTORY_START + dt.timedelta(days=760)
    while day <= end:
        for _ in range(state_mod.SLOTS_PER_DAY):
            count = state_mod.plan_release(state, today=day)
            for pending in state_mod.backlog(state, today=day)[:count]:
                state["released"].append(pending.isoformat())
        day += dt.timedelta(days=1)
    assert len(state_mod.backlog(state, today=end)) < state_mod.MAX_BACKLOG
