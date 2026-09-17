"""Decide whether an autofix PR is safe to auto-merge.

Safe means it doesn't touch the specific logic CLAUDE.md calls out as easy
to break silently. Anything else waits for a human, regardless of whether
tests pass -- tests are the floor, not the whole check. That gap is exactly
why this repo used to run a second, LLM-based PR review pass separate from
ci.yml; this grep-based check replaces it for the narrow case of a bot's own
proposed fix, deterministically instead of by asking a model to be careful.

Run from a branch that has exactly one commit on top of main (the autofix
commit), after that commit has been made.
"""

from __future__ import annotations

import os
import subprocess

GUARDED_FUNCTIONS = {
    "fetch_ecb",
    "_sgs",
    "_get",
    "save_state",
    "plan_release",
    "available_dates",
}
GUARDED_FILES = {"pipeline/sources.py", "pipeline/state.py"}
GUARDED_PATH_PREFIXES = ("data/", ".github/workflows/")

BASE = "HEAD^"
HEAD = "HEAD"


def changed_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", BASE, HEAD], capture_output=True, text=True, check=True
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def function_context_diff(path: str) -> str:
    out = subprocess.run(
        ["git", "diff", "--function-context", BASE, HEAD, "--", path],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def assert_delta(path: str) -> tuple[int, int]:
    out = subprocess.run(
        ["git", "diff", BASE, HEAD, "--", path], capture_output=True, text=True, check=True
    ).stdout
    removed = sum(
        1
        for line in out.splitlines()
        if line.startswith("-") and not line.startswith("---") and "assert" in line
    )
    added = sum(
        1
        for line in out.splitlines()
        if line.startswith("+") and not line.startswith("+++") and "assert" in line
    )
    return removed, added


def check() -> list[str]:
    reasons = []
    files = changed_files()

    for f in files:
        if f.startswith(GUARDED_PATH_PREFIXES):
            reasons.append(f"touches a guarded path: {f}")

    for f in files:
        if f in GUARDED_FILES:
            diff = function_context_diff(f)
            for fn in sorted(GUARDED_FUNCTIONS):
                if f"def {fn}(" in diff:
                    reasons.append(f"touches guarded function {fn}() in {f}")

    if "tests/test_pipeline.py" in files:
        removed, added = assert_delta("tests/test_pipeline.py")
        if removed > added:
            reasons.append(
                "removes more assertions than it adds in tests/test_pipeline.py "
                f"({removed} removed vs {added} added)"
            )

    return reasons


def main() -> int:
    reasons = check()
    safe = "false" if reasons else "true"
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"safe={safe}\n")
        fh.write("reasons<<EOF_GUARD\n")
        fh.write("; ".join(reasons) if reasons else "no guarded logic touched")
        fh.write("\nEOF_GUARD\n")
    print("BLOCKED:" if reasons else "SAFE:", "; ".join(reasons) or "no guarded logic touched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
