"""最终题包关联资源目录的严格契约。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskfoundry.resources import ResourceCatalogError, validate_resource_catalog


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _paper() -> dict[str, object]:
    return {
        "id": "r1",
        "name": "A load-bearing paper",
        "type": "paper",
        "access": {"paper_id": "paper-1"},
        "notes": "Provides the scientific method used by the question.",
    }


def test_resource_catalog_accepts_only_external_scientific_resources(tmp_path: Path) -> None:
    catalog = tmp_path / "resources.json"
    _write(
        catalog,
        [
            _paper(),
            {
                "id": "r2",
                "name": "OpenFOAM",
                "type": "tool",
                "access": {"url": "https://www.openfoam.com"},
                "notes": "Load-bearing public scientific software.",
            },
            {
                "id": "r3",
                "name": "Public observations",
                "type": "dataset",
                "access": {"public": True, "doi": "10.1234/example"},
                "notes": "Publisher-hosted public data.",
            },
            {
                "id": "r4",
                "name": "Published surrogate model",
                "type": "model",
                "access": {"model_id": "model-1"},
                "notes": "External model used by the task.",
            },
        ],
    )

    assert [item["type"] for item in validate_resource_catalog(catalog)] == [
        "paper",
        "tool",
        "dataset",
        "model",
    ]


@pytest.mark.parametrize("resource_type", ["environment", "package", "literature"])
def test_resource_catalog_rejects_noncanonical_types(
    tmp_path: Path, resource_type: str
) -> None:
    catalog = tmp_path / "resources.json"
    item = _paper() | {"type": resource_type}
    _write(catalog, [item])

    with pytest.raises(ResourceCatalogError, match="paper/tool/dataset/model"):
        validate_resource_catalog(catalog)


@pytest.mark.parametrize("name", ["Python", "Python 3", "POSIX shell", "bash"])
def test_resource_catalog_rejects_generic_runtime_as_tool(tmp_path: Path, name: str) -> None:
    catalog = tmp_path / "resources.json"
    _write(
        catalog,
        [
            {
                "id": "r1",
                "name": name,
                "type": "tool",
                "access": {"url": "https://example.invalid/runtime"},
                "notes": "runtime",
            }
        ],
    )

    with pytest.raises(ResourceCatalogError, match="通用运行时"):
        validate_resource_catalog(catalog)


def test_resource_catalog_rejects_image_metadata(tmp_path: Path) -> None:
    catalog = tmp_path / "resources.json"
    _write(
        catalog,
        [
            {
                "id": "r1",
                "name": "Runtime image",
                "type": "tool",
                "access": {"image": "registry/image@sha256:abc"},
                "notes": "runtime",
            }
        ],
    )

    with pytest.raises(ResourceCatalogError, match="镜像或运行环境"):
        validate_resource_catalog(catalog)


@pytest.mark.parametrize(
    "access",
    [
        {"public": False, "url": "https://example.invalid/private"},
        {"public": True},
    ],
)
def test_resource_catalog_rejects_nonpublic_or_unidentified_dataset(
    tmp_path: Path, access: dict[str, object]
) -> None:
    catalog = tmp_path / "resources.json"
    _write(
        catalog,
        [
            {
                "id": "r1",
                "name": "Dataset",
                "type": "dataset",
                "access": access,
                "notes": "data",
            }
        ],
    )

    with pytest.raises(ResourceCatalogError, match="公开外部数据集"):
        validate_resource_catalog(catalog)


def test_resource_catalog_rejects_duplicate_ids_and_nonlist_root(tmp_path: Path) -> None:
    catalog = tmp_path / "resources.json"
    _write(catalog, [_paper(), _paper()])
    with pytest.raises(ResourceCatalogError, match="id 不得重复"):
        validate_resource_catalog(catalog)

    _write(catalog, {"resources": [_paper()]})
    with pytest.raises(ResourceCatalogError, match="根节点必须是数组"):
        validate_resource_catalog(catalog)


def test_resource_catalog_rejects_empty_or_malformed_entries(tmp_path: Path) -> None:
    catalog = tmp_path / "resources.json"
    _write(catalog, [])
    with pytest.raises(ResourceCatalogError, match="至少需要一个"):
        validate_resource_catalog(catalog)

    _write(catalog, [{"id": "r1"}])
    with pytest.raises(ResourceCatalogError, match="必须且只能包含"):
        validate_resource_catalog(catalog)

    item = _paper() | {"name": ""}
    _write(catalog, [item])
    with pytest.raises(ResourceCatalogError, match="name 必须非空"):
        validate_resource_catalog(catalog)

    item = _paper() | {"access": {}}
    _write(catalog, [item])
    with pytest.raises(ResourceCatalogError, match="缺少 access"):
        validate_resource_catalog(catalog)


def test_resource_catalog_rejects_unidentified_resource_and_unsafe_json(tmp_path: Path) -> None:
    catalog = tmp_path / "resources.json"
    item = _paper() | {"access": {"unrelated": "value"}}
    _write(catalog, [item])
    with pytest.raises(ResourceCatalogError, match="缺少可追溯的外部身份"):
        validate_resource_catalog(catalog)

    catalog.write_text(
        '[{"id":"r1","id":"r2","name":"Paper","type":"paper",'
        '"access":{"paper_id":"p"},"notes":"source"}]',
        encoding="utf-8",
    )
    with pytest.raises(ResourceCatalogError, match="duplicate JSON key"):
        validate_resource_catalog(catalog)

    catalog.write_text("[NaN]", encoding="utf-8")
    with pytest.raises(ResourceCatalogError, match="无法读取"):
        validate_resource_catalog(catalog)
