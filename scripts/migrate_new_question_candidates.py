#!/usr/bin/env python3
"""把旧发布规则下的 ``new-question`` 条目完整归档，且不删除历史。

迁移使用同文件系统 rename，在首次移动前写预迁移清单，逐项记录已完成移动，并在
移动后复验全部节点及其摘要。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any


QUESTION_ROOT = Path("/personal/codex-workspace/question-from-questions")
REPO_ROOT = Path("/personal/TaskFoundry")
ARCHIVE_ROOT = REPO_ROOT / "archive" / "question-authoring"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def node_snapshot(root: Path) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def visit(path: Path, relative: str) -> None:
        info = path.lstat()
        base: dict[str, Any] = {
            "path": relative,
            "mode": f"{stat.S_IMODE(info.st_mode):04o}",
            "uid": info.st_uid,
            "gid": info.st_gid,
        }
        if stat.S_ISREG(info.st_mode):
            base.update(
                kind="file",
                size=info.st_size,
                sha256=sha256_file(path),
            )
            nodes.append(base)
            return
        if stat.S_ISLNK(info.st_mode):
            base.update(kind="symlink", target=os.readlink(path))
            nodes.append(base)
            return
        if stat.S_ISDIR(info.st_mode):
            base.update(kind="directory")
            nodes.append(base)
            with os.scandir(path) as entries:
                children = sorted(entries, key=lambda item: item.name)
            for child in children:
                child_relative = child.name if relative == "." else f"{relative}/{child.name}"
                visit(path / child.name, child_relative)
            return
        base.update(kind="special", rdev=info.st_rdev)
        nodes.append(base)

    visit(root, ".")
    return nodes


def snapshot_digest(nodes: list[dict[str, Any]]) -> str:
    payload = json.dumps(nodes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def ensure_archive_is_git_ignored() -> None:
    rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    if "archive/" not in {line.strip() for line in rules}:
        raise RuntimeError("archive/ is not excluded by TaskFoundry/.gitignore")


def build_plan(batch_id: str) -> dict[str, Any]:
    questions: list[dict[str, Any]] = []
    for number in range(1, 33):
        source = QUESTION_ROOT / str(number) / "new-question"
        if not source.is_dir():
            raise RuntimeError(f"missing publication directory: {source}")
        entries: list[dict[str, Any]] = []
        for item in sorted(source.iterdir(), key=lambda path: path.name):
            nodes = node_snapshot(item)
            entries.append(
                {
                    "name": item.name,
                    "source": str(item),
                    "destination": str(
                        ARCHIVE_ROOT
                        / f"q{number:02d}"
                        / batch_id
                        / "new-question-contents"
                        / item.name
                    ),
                    "node_count": len(nodes),
                    "file_bytes": sum(node.get("size", 0) for node in nodes),
                    "tree_sha256": snapshot_digest(nodes),
                    "nodes": nodes,
                }
            )
        questions.append(
            {
                "question": f"Q{number:02d}",
                "source_directory": str(source),
                "entry_count": len(entries),
                "entries": entries,
            }
        )
    return {
        "schema_version": 1,
        "policy": "final-question-family-v1",
        "batch_id": batch_id,
        "operation": "same-filesystem-rename-with-hash-verification",
        "destructive_deletion": False,
        "questions": questions,
    }


def apply_plan(plan: dict[str, Any]) -> Path:
    ensure_archive_is_git_ignored()
    batch_id = plan["batch_id"]
    batch_root = ARCHIVE_ROOT / "_migration_batches" / batch_id
    if batch_root.exists():
        raise RuntimeError(f"batch already exists: {batch_root}")
    batch_root.mkdir(parents=True)
    atomic_json(batch_root / "PRE_MIGRATION_MANIFEST.json", plan)
    journal_path = batch_root / "MIGRATION_JOURNAL.jsonl"

    moved = 0
    for question in plan["questions"]:
        for entry in question["entries"]:
            source = Path(entry["source"])
            destination = Path(entry["destination"])
            if destination.exists() or destination.is_symlink():
                raise RuntimeError(f"destination conflict: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.lstat().st_dev != destination.parent.stat().st_dev:
                raise RuntimeError(f"cross-filesystem move refused: {source}")
            os.rename(source, destination)
            with journal_path.open("a", encoding="utf-8") as journal:
                journal.write(
                    json.dumps(
                        {
                            "source": str(source),
                            "destination": str(destination),
                            "expected_tree_sha256": entry["tree_sha256"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
                journal.flush()
                os.fsync(journal.fileno())
            moved += 1

    verification: list[dict[str, Any]] = []
    for question in plan["questions"]:
        source_directory = Path(question["source_directory"])
        remaining = sorted(path.name for path in source_directory.iterdir())
        if remaining:
            raise RuntimeError(f"publication directory is not empty: {source_directory}: {remaining}")
        for entry in question["entries"]:
            destination = Path(entry["destination"])
            nodes = node_snapshot(destination)
            observed = snapshot_digest(nodes)
            if observed != entry["tree_sha256"]:
                raise RuntimeError(f"post-move hash mismatch: {destination}")
            verification.append(
                {
                    "destination": str(destination),
                    "tree_sha256": observed,
                    "verified": True,
                }
            )

    result = {
        "schema_version": 1,
        "batch_id": batch_id,
        "status": "COMPLETE",
        "moved_entry_count": moved,
        "all_new_question_directories_empty": True,
        "all_tree_hashes_verified": True,
        "verification": verification,
    }
    atomic_json(batch_root / "FINAL_MIGRATION_MANIFEST.json", result)
    return batch_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    batch_id = args.batch_id or dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ-clean-publication-v1")
    plan = build_plan(batch_id)
    summary = {
        "batch_id": batch_id,
        "questions": len(plan["questions"]),
        "entries": sum(item["entry_count"] for item in plan["questions"]),
        "bytes": sum(
            entry["file_bytes"]
            for question in plan["questions"]
            for entry in question["entries"]
        ),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.apply:
        print(apply_plan(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
