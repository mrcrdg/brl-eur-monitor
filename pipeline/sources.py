"""Data sources for the BRL-EUR corridor monitor.

Every source is date-addressable: given a calendar date, it returns the rows
published for that date (possibly none, on holidays). That uniformity is what
lets the release queue in state.py treat a date as the unit of work.

Two independent publishers quote EUR/BRL:
  * the ECB, as part of its daily euro reference rates
  * the Banco Central do Brasil, as the PTAX closing rate
They are computed from different windows and different panels of banks, so
they disagree slightly. reconciliation.csv tracks that spread.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

import requests

TIMEOUT = 25
HEADERS = {"User-Agent": "brl-eur-monitor/1.0 (github actions)"}


@dataclass(frozen=True)
class Source:
    name: str
    table: str  # which curated CSV the rows belong to
    fetch: Callable[[dt.date], tuple[dict, list[dict]]]


# ---------------------------------------------------------------------------
# ECB euro reference rates, via api.frankfurter.dev. No API key.
# Published each TARGET working day around 16:00 CET.
# ---------------------------------------------------------------------------

ECB_QUOTES = ("BRL", "USD", "GBP", "CHF")


def fetch_ecb(day: dt.date) -> tuple[dict, list[dict]]:
    # The project moved from api.frankfurter.app to api.frankfurter.dev and
    # versioned its paths. The old host 301-redirects; requests follows
    # redirects by default, but pointing at the current URL avoids the extra
    # round trip and the risk of the redirect being retired.
    resp = requests.get(
        f"https://api.frankfurter.dev/v1/{day.isoformat()}",
        params={"base": "EUR", "symbols": ",".join(ECB_QUOTES)},
        timeout=TIMEOUT,
        headers=HEADERS,
    )
    resp.raise_for_status()
    payload = resp.json()

    # Frankfurter answers a non-publication date with the previous working
    # day. Treat that as "nothing published for the date we asked about"
    # rather than silently misdating the row.
    if payload.get("date") != day.isoformat():
        return payload, []

    rows = [
        {
            "date": day.isoformat(),
            "publisher": "ecb",
            "base": "EUR",
            "quote": quote,
            "rate": f"{rate:.6f}",
        }
        for quote, rate in sorted(payload["rates"].items())
    ]
    return payload, rows


# ---------------------------------------------------------------------------
# Banco Central do Brasil, SGS time series API. No API key.
#
# Series codes are listed here so they are easy to verify against
# https://www3.bcb.gov.br/sgspub/ before you trust the numbers.
# ---------------------------------------------------------------------------

SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"

PTAX_SERIES = {
    1: ("USD", "BRL"),  # Dolar comercial, venda
    21619: ("EUR", "BRL"),  # Euro, venda
}

INDICATOR_SERIES = {
    432: ("selic_target", "pct_per_year"),  # Meta Selic definida pelo Copom
    433: ("ipca_monthly", "pct_per_month"),  # IPCA, variacao mensal
}


def _sgs(code: int, day: dt.date) -> list[dict]:
    """One SGS series for one day. Returns [] when nothing was published."""
    stamp = day.strftime("%d/%m/%Y")
    resp = requests.get(
        SGS_URL.format(code=code),
        params={"formato": "json", "dataInicial": stamp, "dataFinal": stamp},
        timeout=TIMEOUT,
        headers=HEADERS,
    )
    # SGS answers an empty window with 404 rather than an empty list.
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, list) else []


def fetch_ptax(day: dt.date) -> tuple[dict, list[dict]]:
    payload: dict = {}
    rows: list[dict] = []
    for code, (base, quote) in PTAX_SERIES.items():
        records = _sgs(code, day)
        payload[str(code)] = records
        for record in records:
            rows.append(
                {
                    "date": day.isoformat(),
                    "publisher": "bcb_ptax",
                    "base": base,
                    "quote": quote,
                    "rate": f"{float(record['valor']):.6f}",
                }
            )
    return payload, rows


def fetch_indicators(day: dt.date) -> tuple[dict, list[dict]]:
    payload: dict = {}
    rows: list[dict] = []
    for code, (name, unit) in INDICATOR_SERIES.items():
        records = _sgs(code, day)
        payload[str(code)] = records
        for record in records:
            rows.append(
                {
                    "date": day.isoformat(),
                    "indicator": name,
                    "value": f"{float(record['valor']):.4f}",
                    "unit": unit,
                    "series_code": str(code),
                }
            )
    return payload, rows


SOURCES: tuple[Source, ...] = (
    Source("ecb_reference", "fx_rates", fetch_ecb),
    Source("bcb_ptax", "fx_rates", fetch_ptax),
    Source("bcb_indicators", "indicators", fetch_indicators),
)

# Uniqueness key per curated table.
KEYS = {
    "fx_rates": ("date", "publisher", "base", "quote"),
    "indicators": ("date", "indicator"),
}
