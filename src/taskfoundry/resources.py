"""Validate the external scientific-resource catalog shipped with final questions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import ContractError


class ResourceCatalogError(ContractError):
    """The final package resource catalog violates its public contract."""


RESOURCE_TYPES = frozenset({"paper", "tool", "dataset", "model"})
RESOURCE_FIELDS = frozenset({"id", "name", "type", "access", "notes"})
RUNTIME_ACCESS_FIELDS = frozenset(
    {
        "digest",
        "docker_image_uri",
        "environment_key",
        "image",
        "image_url",
        "package",
        "platform",
    }
)
GENERIC_RUNTIME_NAMES = frozenset(
    {"bash", "posix shell", "python", "python 3", "python3", "sh"}
)
EXTERNAL_IDENTITIES = {
    "paper": frozenset({"doi", "paper_id", "source_question_id", "url"}),
    "tool": frozenset({"repository", "tool_id", "unique_key", "url"}),
    "dataset": frozenset(
        {"dataset_id", "doi", "source_question_id", "unique_key", "url"}
    ),
    "model": frozenset({"doi", "model_id", "repository", "unique_key", "url"}),
}


def validate_resource_catalog(path: Path) -> list[dict[str, Any]]:
    """Return a strict catalog of external papers, software, datasets, and models."""
    value = _read_json(path)
    if not isinstance(value, list):
        raise ResourceCatalogError("resources.json 根节点必须是数组")
    if not value:
        raise ResourceCatalogError("resources.json 至少需要一个关联资源")
    resources: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for position, item in enumerate(value, start=1):
        resource = _validate_resource(item, position)
        identifier = resource["id"]
        if identifier in identifiers:
            raise ResourceCatalogError("resources.json 的 id 不得重复")
        identifiers.add(identifier)
        resources.append(resource)
    return resources


def _validate_resource(value: object, position: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RESOURCE_FIELDS:
        raise ResourceCatalogError(
            f"resources.json 第 {position} 项必须且只能包含 id/name/type/access/notes"
        )
    for field in ("id", "name", "type", "notes"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ResourceCatalogError(f"resources.json 第 {position} 项的 {field} 必须非空")
    resource_type = value["type"]
    if resource_type not in RESOURCE_TYPES:
        raise ResourceCatalogError("资源 type 只能是 paper/tool/dataset/model")
    access = value["access"]
    if not isinstance(access, dict) or not access:
        raise ResourceCatalogError(f"resources.json 第 {position} 项缺少 access")
    if RUNTIME_ACCESS_FIELDS.intersection(access):
        raise ResourceCatalogError("resources.json 不记录镜像或运行环境")
    if resource_type == "tool" and value["name"].strip().casefold() in GENERIC_RUNTIME_NAMES:
        raise ResourceCatalogError("resources.json 不记录 Python、shell 等通用运行时")
    identities = EXTERNAL_IDENTITIES[resource_type]
    if not any(_nonempty(access.get(field)) for field in identities):
        if resource_type == "dataset":
            raise ResourceCatalogError("dataset 必须是可追溯的公开外部数据集")
        raise ResourceCatalogError(f"{resource_type} 缺少可追溯的外部身份")
    if resource_type == "dataset" and access.get("public") is not True:
        raise ResourceCatalogError("dataset 必须是可追溯的公开外部数据集")
    return value


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read_json(path: Path) -> object:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Build one JSON object while rejecting duplicate member names."""
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ResourceCatalogError(f"无法读取 resources.json: {error}") from error
