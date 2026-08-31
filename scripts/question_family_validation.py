#!/usr/bin/env python3
"""兼容人工调用的薄入口；正式调度直接调用 PublicationContract v2。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from taskfoundry.publication import finalize_published_family  # noqa: E402
from taskfoundry.store import RunStore  # noqa: E402


def main() -> int:
    """Validate and seal one canonical published question family."""
    parser = argparse.ArgumentParser()
    parser.add_argument("question", type=int)
    parser.add_argument("family", type=Path)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run = RunStore(args.run_dir).read_snapshot()
    if run is None:
        raise SystemExit("run is not initialized")
    finalize_published_family(args.question, args.family, run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
