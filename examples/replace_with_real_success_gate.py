#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    """Example success gate.

    Replace this with a real product or research completion check. A good gate
    should fail for shallow progress and pass only when the objective is truly
    done.
    """
    marker = Path("SUCCESS_MARKER.txt")
    if not marker.exists():
        print("Missing SUCCESS_MARKER.txt")
        return 1
    text = marker.read_text(encoding="utf-8").strip()
    if "done" not in text.lower():
        print("SUCCESS_MARKER.txt exists but does not contain 'done'")
        return 1
    print("Success gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
