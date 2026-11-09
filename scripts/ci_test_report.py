"""Run the Windows test gate and export only test IDs and outcomes."""

from __future__ import annotations

import argparse
import json
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


def main(path: Path, *, markers: str = "") -> int:
    results = Results()
    arguments = ["-q", "-ra", "--tb=short", "tests/unit", "tests/integration"]
    if markers:
        arguments.extend(["-m", markers])
    status = pytest.main(arguments, plugins=[results])
    totals = Counter(results.outcomes.values())
    counts = {key: totals[key] for key in ("passed", "failed", "skipped")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pytest_exit_code": int(status),
        "marker_expression": markers,
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("-m", "--markers", default="")
    args = parser.parse_args()
    raise SystemExit(main(args.path, markers=args.markers))
