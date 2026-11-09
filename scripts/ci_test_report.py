"""Run the Windows test gate and export only test IDs and outcomes."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest


class Results:
    def __init__(self) -> None:
        self.outcomes: dict[str, str] = {}

    def pytest_runtest_logreport(self, report) -> None:
        if report.failed:
            self.outcomes[report.nodeid] = "failed"
        elif report.skipped and self.outcomes.get(report.nodeid) != "failed":
            self.outcomes[report.nodeid] = "skipped"
        elif report.when == "call":
            self.outcomes.setdefault(report.nodeid, "passed")

    def pytest_collectreport(self, report) -> None:
        if report.failed or report.skipped:
            self.outcomes[report.nodeid] = report.outcome


def main(path: Path) -> int:
    results = Results()
    status = pytest.main(["-q", "-ra", "tests/unit", "tests/integration"], plugins=[results])
    totals = Counter(results.outcomes.values())
    counts = {key: totals[key] for key in ("passed", "failed", "skipped")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pytest_exit_code": int(status),
        "counts": counts,
        "tests": [
            {"test": nodeid.split("[", 1)[0], "outcome": outcome}
            for nodeid, outcome in results.outcomes.items()
        ],
    }, indent=2) + "\n", encoding="utf-8")
    print("CI test counts:", counts)
    if counts["skipped"]:
        print("Windows CI requires the Tk, Chrome and HTTPS fixtures. Skipped tests block packaging.")
    return int(status) or int(bool(counts["skipped"] or counts["failed"]))


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
