"""Ask a free-tier model to triage a failed `monitor` run.

Reads the failure log (already fetched by the workflow step) plus this
repo's CLAUDE.md, optionally probes the failing upstream URL directly, and
asks Gemini's free tier to classify the failure and -- only for a genuine
code defect -- propose a minimal patch and a regression test.

Deliberately dumber than an agentic coding assistant: one prompt, one
response, no file exploration or shell access for the model itself. All
evidence-gathering happens here in Python, so a free/smaller model only has
to reason over text it has already been handed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
MODEL = "gemini-2.5-flash"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category": {
            "type": "STRING",
            "enum": ["infrastructure", "transient_upstream", "code_defect"],
        },
        "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "summary": {
            "type": "STRING",
            "description": (
                "Plain language for someone reading this in an email who was not "
                "watching it happen. Not the traceback repeated."
            ),
        },
        "evidence": {"type": "STRING"},
        "patch_diff": {
            "type": "STRING",
            "description": (
                "Unified diff (git apply format, paths relative to repo root) fixing "
                "the defect. Empty string unless category is code_defect."
            ),
        },
        "test_diff": {
            "type": "STRING",
            "description": (
                "Unified diff adding a regression test to tests/test_pipeline.py that "
                "fails before the fix and passes after. Empty string unless category "
                "is code_defect."
            ),
        },
    },
    "required": ["category", "confidence", "summary", "evidence", "patch_diff", "test_diff"],
}

HARD_LIMITS = """
Never propose a fix that does any of these -- CLAUDE.md documents each as a
bug that already shipped once in this repo:
- Swallows an exception, broadens a try/except, or records a failed fetch as
  `no_data`. That converts a loud failure into silent, permanent data loss:
  the date gets marked settled and is never retried.
- Relaxes the date-mismatch guard in fetch_ecb (Frankfurter answers a
  non-publication date with the previous working day's rates; that check is
  the only thing stopping misdated rows).
- Treats a 404 from BCB SGS as an error, or a non-404 as "no data" (SGS
  reports an empty window as 404).
- Writes a wall-clock timestamp, reorders a CSV, or changes JSON key
  ordering (breaks the byte-identical no-op invariant).
- Adds --date, GIT_AUTHOR_DATE, or any history-rewriting rebase.
- Changes SLOTS_PER_DAY without changing the cron entry count in
  .github/workflows/ingest.yml, or vice versa.
- Fetches today's date, or adds an API key.
- Weakens, skips, or deletes a test to make the suite pass.
If the only fix you can see requires one of the above, that is not a code
defect -- classify it as transient_upstream or infrastructure instead and
say why in evidence.
"""


def probe_upstream(log: str) -> str:
    """Best-effort: hit the exact URL from the traceback, if there is one."""
    match = re.search(r"https://api\.(?:bcb\.gov\.br|frankfurter\.dev)/\S+", log)
    if not match:
        return "(no upstream URL found in the log to probe)"
    url = match.group(0).rstrip("'\")")
    try:
        resp = requests.get(url, timeout=15)
        return (
            f"Live probe of {url} just now: HTTP {resp.status_code}, "
            f"body starts with {resp.text[:200]!r}"
        )
    except requests.RequestException as exc:
        return f"Live probe of {url} just now: request failed ({exc})"


def set_output(name: str, value: str) -> None:
    delim = "EOF_AUTOFIX"
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def build_prompt(log: str, claude_md: str, probe: str) -> str:
    return f"""A scheduled run of the `monitor` workflow in this repo failed. Classify
it and, only if it is a genuine code defect, propose a fix.

## Repo context (CLAUDE.md, authoritative)

{claude_md}

## The failure log (tail)

{log}

## Live evidence

{probe}

## Classification

Put the failure in exactly one bucket:
- infrastructure: the job never reached this repo's code (runner not
  acquired, checkout failure, uv sync failing on a registry blip).
- transient_upstream: the code ran and a data source misbehaved in a way
  expected to pass on retry. pipeline/sources.py already retries 5xx and
  connection errors three times with backoff; if the live probe above
  succeeded just now, that is strong evidence for this bucket even when the
  original error wasn't a 5xx (e.g. a 200 with an empty body).
- code_defect: the pipeline did the wrong thing given valid input -- a real
  logic error, not upstream flakiness.

{HARD_LIMITS}

Only for code_defect: patch_diff must be a minimal unified diff that fixes
the actual defect, and test_diff must add a regression test to
tests/test_pipeline.py. Otherwise leave both as empty strings.

summary is read by a human in an email notification who was not watching
this happen. Say what happened, why it's in this bucket, and -- for
transient_upstream -- that no action is needed because it already recovered
or will recover on the next scheduled run.
"""


def main() -> int:
    log = Path("failure_excerpt.log").read_text(errors="replace")
    claude_md = (ROOT / "CLAUDE.md").read_text()
    probe = probe_upstream(log)
    prompt = build_prompt(log, claude_md, probe)

    resp = requests.post(
        API_URL,
        params={"key": os.environ["GEMINI_API_KEY"]},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
            },
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    result = json.loads(text)

    Path("patch.diff").write_text(result["patch_diff"] or "")
    Path("test.diff").write_text(result["test_diff"] or "")
    Path("diagnosis.md").write_text(
        f"**Category:** {result['category']} (confidence: {result['confidence']})\n\n"
        f"{result['summary']}\n\n"
        f"<details><summary>Evidence</summary>\n\n{result['evidence']}\n\n</details>\n\n"
        f"Diagnosed by {MODEL} from run {os.environ.get('RUN_ID', '?')}.\n"
    )

    set_output("category", result["category"])
    set_output("confidence", result["confidence"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
