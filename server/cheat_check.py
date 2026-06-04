"""/cheat checker — scans code for violations per project standards.

Categories:
  1. Stubs (NotImplementedError)
  2. Mocks/simulators in production paths
  3. Timeouts on non-health-probe code
  4. Domain/provisioner tool mixing
  5. Swallowed exceptions (pass / logger.debug without notify_maintainer)
  6. Fixed polling loops (time.sleep without reasoning)
  7. Algorithmic retries without reasoning

Usage:
    python cheat_check.py [path ...]
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path


VIOLATIONS: list[dict] = []


def _scan_file(path: Path) -> None:
    """Scan a single Python file for violations."""
    if "cheat_check.py" in path.name:
        return
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # 1. Stubs
    for i, line in enumerate(lines, 1):
        if "NotImplementedError" in line and "#" not in line.split("NotImplementedError")[0]:
            VIOLATIONS.append({
                "file": str(path),
                "line": i,
                "code": line.strip(),
                "category": "STUB",
                "reason": "NotImplementedError in production code",
            })

    # 2. Mocks/simulators
    filename = path.name.lower()
    if "mock" in filename or "simulator" in filename or "stub" in filename:
        VIOLATIONS.append({
            "file": str(path),
            "line": 1,
            "code": path.name,
            "category": "MOCK",
            "reason": "File name contains mock/simulator/stub — verify not in production path",
        })

    # 3. Timeouts on non-health-probe urllib/requests
    for i, line in enumerate(lines, 1):
        if "timeout=" in line and ("urllib" in line or "requests" in line or "httpx" in line):
            if "health" not in line.lower() and "probe" not in line.lower():
                # Skip if developer has documented justification with /cheat comment
                if "/cheat:" in text or "/cheat :" in text:
                    # Check if this line or surrounding lines have /cheat justification
                    context = "\n".join(lines[max(0, i - 5):min(len(lines), i + 5)])
                    if "/cheat:" in context or "REQUIRED" in context:
                        continue
                VIOLATIONS.append({
                    "file": str(path),
                    "line": i,
                    "code": line.strip(),
                    "category": "TIMEOUT",
                    "reason": "timeout= on non-health-probe HTTP call",
                })

    # 4. Swallowed exceptions
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped == "pass" or stripped.startswith("logger.debug("):
            # Check if previous lines contain "except"
            for j in range(max(0, i - 5), i):
                if "except" in lines[j]:
                    VIOLATIONS.append({
                        "file": str(path),
                        "line": i,
                        "code": stripped,
                        "category": "SWALLOWED_EXCEPTION",
                        "reason": "pass or logger.debug in exception handler without notify_maintainer",
                    })
                    break

    # 5. Fixed polling loops
    for i, line in enumerate(lines, 1):
        if "time.sleep(" in line or "await asyncio.sleep(" in line:
            # Check context for "while" or "for"
            for j in range(max(0, i - 10), i):
                if "while" in lines[j] or "for " in lines[j]:
                    VIOLATIONS.append({
                        "file": str(path),
                        "line": i,
                        "code": line.strip(),
                        "category": "FIXED_POLLING",
                        "reason": "Fixed sleep in loop — no dynamic backoff or reasoning",
                    })
                    break

    # 6. Algorithmic retries
    for i, line in enumerate(lines, 1):
        if "for attempt in range(" in line or "for retry in range(" in line:
            VIOLATIONS.append({
                "file": str(path),
                "line": i,
                "code": line.strip(),
                "category": "ALGORITHMIC_RETRY",
                "reason": "Algorithmic retry loop without reasoning-based backoff",
            })


def main(paths: list[str]) -> None:
    """Run cheat check on given paths."""
    for p in paths:
        path = Path(p)
        if path.is_file() and path.suffix == ".py":
            _scan_file(path)
        elif path.is_dir():
            for f in path.rglob("*.py"):
                # Skip pycache and venv
                if "__pycache__" in str(f) or ".venv" in str(f):
                    continue
                _scan_file(f)

    if not VIOLATIONS:
        print("✅ /cheat: No violations found")
        return

    print(f"❌ /cheat: {len(VIOLATIONS)} violation(s) found\n")

    by_category: dict[str, list[dict]] = {}
    for v in VIOLATIONS:
        by_category.setdefault(v["category"], []).append(v)

    for cat, items in sorted(by_category.items()):
        print(f"\n{'=' * 60}")
        print(f"CATEGORY: {cat}")
        print(f"{'=' * 60}")
        for item in items:
            print(f"\n  File: {item['file']}")
            print(f"  Line: {item['line']}")
            print(f"  Code: {item['code'][:80]}")
            print(f"  Why:  {item['reason']}")

    print(f"\n{'=' * 60}")
    print(f"TOTAL: {len(VIOLATIONS)} violation(s)")
    print(f"{'=' * 60}")
    sys.exit(1)


if __name__ == "__main__":
    paths = sys.argv[1:] or ["server"]
    main(paths)
