#!/usr/bin/env python3
"""检查每个已发布 ``new-question`` 条目都是合法题族。"""

from __future__ import annotations

import argparse
import json
import sys

from question_family_validation import FamilyValidationError, QUESTION_ROOT, validate_family


def parse_args() -> argparse.Namespace:
    """解析发布检查器的迁移选项。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="迁移期间仅检查发布目录洁净度，不要求 32 个题族全部就绪",
    )
    return parser.parse_args()


def main() -> int:
    """使用与晋级器相同的 validator 校验全部发布条目。"""
    args = parse_args()
    failures: list[str] = []
    families = 0
    for number in range(1, 33):
        publication = QUESTION_ROOT / str(number) / "new-question"
        if not publication.is_dir():
            failures.append(f"Q{number:02d}：缺少 new-question 目录")
            continue
        entries = sorted(publication.iterdir(), key=lambda path: path.name)
        if len(entries) > 1 or (not args.allow_empty and len(entries) != 1):
            expected = "零个或一个" if args.allow_empty else "恰好一个"
            failures.append(f"Q{number:02d}：应有{expected}已发布题族，实际为 {len(entries)} 个")
        for entry in entries:
            try:
                validate_family(number, entry, staging_required=False)
            except (OSError, ValueError, json.JSONDecodeError, FamilyValidationError) as error:
                failures.append(f"Q{number:02d}/{entry.name}：{error}")
            else:
                families += 1
    if not args.allow_empty and families != 32:
        failures.append(f"全局：应有 32 个已发布题族，实际为 {families} 个")
    print(json.dumps({"families": families, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
