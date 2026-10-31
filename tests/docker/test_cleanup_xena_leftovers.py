"""Root leftover cleanup removes only an exact knowledge/README.md tree.

The production script targets /opt/data and runs as root from stage2. These
tests pass a temp root and never touch a real volume.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "cleanup-xena-leftovers.sh"


def _run(root: Path) -> str:
    env = os.environ.copy()
    env["XENA_LEFTOVER_ALLOW_NONROOT"] = "1"
    result = subprocess.run(
        ["/bin/sh", str(_SCRIPT), str(root)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def test_removes_only_an_exact_readme_tree(tmp_path: Path) -> None:
    root = tmp_path / "data"
    clean = root / "profiles" / "xena_management" / "knowledge"
    clean.mkdir(parents=True)
    (clean / "README.md").write_text("left", encoding="utf-8")
    extra = root / "profiles" / "xena_clientes"
    (extra / "knowledge").mkdir(parents=True)
    (extra / "knowledge" / "README.md").write_text("left", encoding="utf-8")
    (extra / "SOUL.md").write_text("keep", encoding="utf-8")
    tomb = root / "profiles" / ".deleted" / "xena_management" / "knowledge"
    tomb.mkdir(parents=True)
    (tomb / "README.md").write_text("tomb", encoding="utf-8")

    first = _run(root)
    assert "removed" in first and "xena_management" in first
    assert not (root / "profiles" / "xena_management").exists()
    assert (extra / "SOUL.md").read_text(encoding="utf-8") == "keep"
    assert (tomb / "README.md").read_text(encoding="utf-8") == "tomb"

    (extra / "SOUL.md").unlink()
    second = _run(root)
    assert "removed" in second and "xena_clientes" in second
    assert not extra.exists()
    assert (tomb / "README.md").read_text(encoding="utf-8") == "tomb"

    third = _run(root)
    assert "absent" in third
    assert (tomb / "README.md").exists()


def test_skips_a_symlinked_knowledge_dir(tmp_path: Path) -> None:
    root = tmp_path / "data"
    profile = root / "profiles" / "xena_management"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "README.md").write_text("nope", encoding="utf-8")
    profile.mkdir(parents=True)
    (profile / "knowledge").symlink_to(outside)

    output = _run(root)
    assert "skip" in output
    assert profile.is_dir()
    assert (outside / "README.md").read_text(encoding="utf-8") == "nope"
