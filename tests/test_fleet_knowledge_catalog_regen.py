from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet_knowledge_catalog_regen.py"
SPEC = importlib.util.spec_from_file_location("fleet_knowledge_catalog_regen", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("catalog_root", ["Agents/Catalog", "Skills/Catalog", "System/Catalogs"])
def test_rejects_symlinked_catalog_root(tmp_path: Path, catalog_root: str) -> None:
    real_root = tmp_path / "outside" / Path(catalog_root)
    real_root.mkdir(parents=True)
    link = tmp_path / catalog_root
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        MODULE.assert_owned_catalog_tree(tmp_path)


@pytest.mark.parametrize("ancestor", ["Agents", "Skills", "System"])
def test_rejects_symlinked_catalog_ancestor(tmp_path: Path, ancestor: str) -> None:
    catalog_name = "Catalog" if ancestor != "System" else "Catalogs"
    real_root = tmp_path / "outside" / ancestor / catalog_name
    real_root.mkdir(parents=True)
    link = tmp_path / ancestor
    link.symlink_to(tmp_path / "outside" / ancestor, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        MODULE.assert_owned_catalog_tree(tmp_path)


def test_accepts_real_owned_catalog_tree(tmp_path: Path) -> None:
    for root_name in ("Agents", "Skills"):
        root = tmp_path / root_name / "Catalog"
        root.mkdir(parents=True)
        (root / "generated.md").write_text(
            "---\ngenerated: true\ngenerator: control-spine/scripts/generate_knowledge_catalogs.py\n---\n",
            encoding="utf-8",
        )

    MODULE.assert_owned_catalog_tree(tmp_path)
