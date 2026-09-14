#!/usr/bin/env python3
"""Check all source with pinned Pyright, rejecting new or stale baseline entries.

Run with the development environment's Python. --update-baseline records the
current diagnostics for review; it does not suppress them in Pyright itself.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "pyright-baseline.json"


def diagnostic_counts(diagnostics: list[dict], root: Path) -> Counter[tuple[str, str, str, str]]:
    """Keep multiplicity; ignore line shifts when unrelated code moves."""
    return Counter(
        (
            Path(item["file"]).relative_to(root).as_posix(),
            item["severity"],
            item.get("rule", ""),
            item["message"],
        )
        for item in diagnostics
    )


def check(*, baseline_path: Path = BASELINE, update: bool = False) -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson", "src/sparkrun"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        print(result.stderr or result.stdout or "Pyright failed without diagnostics", file=sys.stderr)
        return 2
    try:
        output = json.loads(result.stdout)
        summary = output["summary"]
        if summary["filesAnalyzed"] <= 0:
            raise ValueError("Pyright did not analyze any source files")
        current = diagnostic_counts(output["generalDiagnostics"], ROOT)
        version = output["version"]
        if update:
            baseline_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "pyright": version,
                        "diagnostics": [
                            {"file": key[0], "severity": key[1], "rule": key[2], "message": key[3], "count": count}
                            for key, count in sorted(current.items())
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n"
            )
            print("Updated %s; review diagnostic changes before committing." % baseline_path.name)
            return 0
        baseline = json.loads(baseline_path.read_text())
        if baseline["schema"] != 1 or baseline["pyright"] != version:
            raise ValueError("Baseline schema or Pyright version changed; review and regenerate the baseline")
        known = Counter(
            {(item["file"], item["severity"], item["rule"], item["message"]): item["count"] for item in baseline["diagnostics"]}
        )
    except (KeyError, OSError, ValueError, TypeError) as error:
        print("Cannot check type baseline: %s" % error, file=sys.stderr)
        return 2
    added = current - known
    removed = known - current
    print(
        "Full-source Pyright: %s errors, %s warnings across %s files."
        % (
            summary["errorCount"],
            summary["warningCount"],
            summary["filesAnalyzed"],
        )
    )
    for (path, severity, rule, message), count in sorted(added.items()):
        print("NEW %s (%s), %s occurrence(s): %s\n%s" % (severity, rule, count, path, message))
    if removed:
        print("%s resolved diagnostics remain in the baseline; run --update-baseline and review the reduction." % sum(removed.values()))
    if added or removed:
        return 1
    print("Baseline matches; no new diagnostics. Existing debt remains visible in pyright-baseline.json.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-baseline", action="store_true", help="record current diagnostics for review")
    args = parser.parse_args()
    return check(update=args.update_baseline)


if __name__ == "__main__":
    raise SystemExit(main())
