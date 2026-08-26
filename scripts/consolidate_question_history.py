#!/usr/bin/env python3
"""按新的 Skill Bank 目录保存 Q3--Q32 历史 trace。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from taskfoundry.history import consolidate


def main() -> int:
    """规划或执行确定性的 Q3--Q32 历史整理。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--question-root", type=Path, default=Path("/personal/codex-workspace/question-from-questions"))
    parser.add_argument("--runs-root", type=Path, default=Path("/personal/TaskFoundry/runs"))
    parser.add_argument("--workbench-root", type=Path, default=Path("/personal/TaskFoundry/workbench"))
    args = parser.parse_args()
    ledger = consolidate(
        range(3, 33),
        question_root=args.question_root,
        runs_root=args.runs_root,
        workbench_root=args.workbench_root,
        apply=args.apply,
    )
    print(json.dumps(ledger, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
