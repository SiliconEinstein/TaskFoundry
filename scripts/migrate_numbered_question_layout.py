#!/usr/bin/env python3
"""把 Q3--Q32 迁入 Skill Bank、双 trace 与 question-pack 目录合同。"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile


QUESTION_ROOT = Path("/personal/codex-workspace/question-from-questions")
LEGACY_NAMES = ("harbor-runs", "progression", "reports", "review", "artifacts", "new-question")


def atomic_json(path: Path, value: dict[str, object]) -> None:
    """通过同目录原子替换写入迁移记录。"""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate_question(number: int, *, apply: bool) -> dict[str, object]:
    """规划或执行一个编号题目录的同文件系统迁移。"""
    root = QUESTION_ROOT / str(number)
    if not root.is_dir():
        raise RuntimeError(f"numbered question root is missing: {root}")
    authoring = root / "trace/authoring"
    final = root / "trace/final"
    publication = root / "question-pack"
    moves: list[dict[str, object]] = []
    for name in LEGACY_NAMES:
        source = root / name
        if not source.exists():
            continue
        destination = authoring / "pre-skillbank/legacy" / name
        if destination.exists():
            raise RuntimeError(f"migration destination already exists: {destination}")
        moves.append({"source": str(source), "destination": str(destination)})
    if not apply:
        return {"question": f"Q{number:02d}", "moves": moves}
    for directory in (authoring / "skill-activations", final, publication):
        directory.mkdir(parents=True, exist_ok=True)
    for move in moves:
        source = Path(str(move["source"]))
        destination = Path(str(move["destination"]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        before = source.stat()
        os.replace(source, destination)
        after = destination.stat()
        move["same_inode"] = (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        if move["same_inode"] is not True:
            raise RuntimeError(f"legacy move was not an atomic same-filesystem rename: {source}")
    record = {
        "schema_version": 1,
        "question": f"Q{number:02d}",
        "migrated_at": datetime.now(UTC).isoformat(),
        "brief_contract": ["QuestionDesignBrief.json", "QuestionDesignBrief.md"],
        "trace_contract": ["trace/authoring", "trace/final"],
        "publication_root": "question-pack",
        "difficulty_levels": ["high", "medium", "low"],
        "moves": moves,
    }
    atomic_json(authoring / "pre-skillbank/MIGRATION.json", record)
    return record


def main() -> int:
    """迁移 Q3--Q32；默认仅输出规划，显式 apply 才写入。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    records = [migrate_question(number, apply=args.apply) for number in range(3, 33)]
    print(json.dumps({"applied": args.apply, "questions": records}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
