"""Boot cleanup deletes only the approved stale paths under a data root.

The production script targets /opt/data. These tests pass a temp root so the
suite never touches a real volume.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).resolve().parents[2] / "docker" / "volume_cleanup.py"
    spec = importlib.util.spec_from_file_location("volume_cleanup", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cleanup_mod = _load()


def _write(path: Path, payload: bytes | str = b"x" * 32) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_bytes(payload)


def _facts(env: Path) -> str:
    return json.dumps({"packages": {"venv": {"environment": str(env)}}})


def _live_install(data: Path, name: str = "abcdef0123456789") -> Path:
    env = data / "installs" / name / "environments" / "gen"
    _write(env / "pyvenv.cfg", "home = /usr/bin\n")
    _write(data / "installs" / name / "facts.json", _facts(env))
    return data / "installs" / name


def _stale(data: Path, name: str = "351f81ccdeadbeef") -> Path:
    env = data / "installs" / name / "environments" / "old"
    _write(env / "pyvenv.cfg", "home = /usr/bin\n")
    _write(data / "installs" / name / "marker.txt", b"stale-bytes")
    return data / "installs" / name


def _leave_alones(data: Path) -> list[Path]:
    paths = [
        data / ".env.bak-keep",
        data / "config.yaml.bak-keep",
        data / "lazy-packages" / "keep.txt",
        data / "tirith" / "duplicate.txt",
        data / "jbq" / "scratch.txt",
        data / "profiles" / "xena" / "notes.txt",
        data / "xaedron" / "keep.txt",
        data / "logs" / "agent.log",
        data / "logs" / "errors.log",
        data / "logs" / "gateway.log.1",
        data / "plugins" / "hindsight" / "plugin.yaml",
        data / "plugins" / ".install-other" / "keep.txt",
        data / ".npm" / "keep.txt",
        data / "skills" / ".hub" / "keep.txt",
        data / "skills" / "demo" / "SKILL.md",
        data / "cache" / "other" / "keep.txt",
        data / "installs" / "abcdef0123456789" / "facts.json",
    ]
    for path in paths:
        if path.suffix == ".json" and path.name == "facts.json":
            continue
        _write(path, b"keep")
    return paths


def _run(data: Path, venv: Path, project: Path, capsys: pytest.CaptureFixture[str]) -> str:
    cleanup_mod.cleanup(data, venv, project)
    captured = capsys.readouterr()
    return captured.out


def test_deletes_only_the_allowlist_and_is_safe_to_repeat(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    venv = tmp_path / "venv"
    project = tmp_path / "project"
    venv.mkdir()
    project.mkdir()
    _write(data / "logs" / "agent.log.1", b"rotated-agent")
    _write(data / "logs" / "errors.log.2", b"rotated-errors")
    _write(data / "logs" / "agent.log.10.gz", b"rotated-gz")
    _write(data / "profiles" / "coder" / "logs" / "agent.log.3", b"profile-rotated")
    _write(data / "profiles" / "coder" / "logs" / "errors.log", b"live-profile-log")
    _write(data / "plugins" / ".install-rqzgcdjx" / "partial.txt", b"partial")
    _write(data / "plugins" / ".install-61yo688a" / "partial.txt", b"partial")
    _write(data / ".npm" / "_cacache" / "index.txt", b"npm")
    _write(data / "skills" / ".hub" / "index-cache" / "idx.txt", b"hub")
    _write(data / "cache" / "uv" / "wheel.whl", b"wheel-bytes")
    live = _live_install(data)
    stale = _stale(data)
    kept = _leave_alones(data)

    first = _run(data, venv, project, capsys)

    assert "free bytes before:" in first
    assert "free bytes after:" in first
    assert f"dry-run {data / 'logs' / 'agent.log.1'}" in first
    assert not (data / "logs" / "agent.log.1").exists()
    assert not (data / "logs" / "errors.log.2").exists()
    assert not (data / "logs" / "agent.log.10.gz").exists()
    assert not (data / "profiles" / "coder" / "logs" / "agent.log.3").exists()
    assert (data / "profiles" / "coder" / "logs" / "errors.log").read_bytes() == b"live-profile-log"
    assert (data / "logs" / "agent.log").read_bytes() == b"keep"
    assert (data / "logs" / "errors.log").read_bytes() == b"keep"
    assert (data / "logs" / "gateway.log.1").read_bytes() == b"keep"
    assert not (data / "plugins" / ".install-rqzgcdjx").exists()
    assert not (data / "plugins" / ".install-61yo688a").exists()
    assert (data / "plugins" / "hindsight" / "plugin.yaml").is_file()
    assert (data / "plugins" / ".install-other" / "keep.txt").is_file()
    assert not (data / ".npm" / "_cacache").exists()
    assert (data / ".npm" / "keep.txt").is_file()
    assert not (data / "skills" / ".hub" / "index-cache").exists()
    assert (data / "skills" / ".hub" / "keep.txt").is_file()
    assert not (data / "cache" / "uv").exists()
    assert (data / "cache" / "other" / "keep.txt").is_file()
    assert not stale.exists()
    assert live.is_dir()
    for path in kept:
        if "351f81cc" in str(path) or path.name == "facts.json":
            continue
        assert path.exists(), path

    second = _run(data, venv, project, capsys)
    assert f"skip missing {data / 'plugins' / '.install-rqzgcdjx'}" in second
    assert f"skip missing {data / 'cache' / 'uv'}" in second
    assert f"skip missing {data / 'installs' / '351f81cc*'}" in second
    assert (data / "logs" / "agent.log").is_file()
    assert live.is_dir()


def test_skips_uv_cache_hardlinked_to_the_live_venv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    venv = tmp_path / "venv"
    cache_file = data / "cache" / "uv" / "wheel.whl"
    venv_file = venv / "wheel.whl"
    _write(cache_file, b"shared-wheel")
    venv.mkdir()
    os.link(cache_file, venv_file)
    _live_install(data)

    output = _run(data, venv, tmp_path / "project", capsys)

    assert cache_file.is_file()
    assert "hardlinked to" in output
    assert venv_file.is_file()


def test_skips_uv_cache_hardlinked_to_the_live_installs_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    venv = tmp_path / "venv"
    venv.mkdir()
    live = _live_install(data)
    cache_file = data / "cache" / "uv" / "wheel.whl"
    install_file = live / "environments" / "gen" / "wheel.whl"
    _write(cache_file, b"shared-wheel")
    os.link(cache_file, install_file)

    output = _run(data, venv, tmp_path / "project", capsys)

    assert cache_file.is_file()
    assert "hardlinked to" in output
    assert install_file.is_file()


def test_skips_uv_cache_when_the_venv_symlinks_into_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    venv = tmp_path / "venv"
    cache_file = data / "cache" / "uv" / "wheel.whl"
    _write(cache_file, b"wheel")
    venv.mkdir()
    (venv / "wheel.whl").symlink_to(cache_file)
    _live_install(data)

    output = _run(data, venv, tmp_path / "project", capsys)

    assert cache_file.is_file()
    assert "points into the cache" in output


def test_skips_stale_install_when_config_or_symlink_points_at_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    venv = tmp_path / "venv"
    venv.mkdir()
    _live_install(data)
    stale = _stale(data)
    _write(data / "config.yaml", f"toolchain: {stale}\n")

    output = _run(data, venv, tmp_path / "project", capsys)

    assert stale.is_dir()
    assert f"referenced by {data / 'config.yaml'}" in output

    stale2 = _stale(data, "351f81ccother")
    link = data / "profiles" / "coder" / "toolchain"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(stale2)
    # config.yaml still references the first stale dir; point it at the live one
    # so only the symlink blocks the second candidate.
    live_env = data / "installs" / "abcdef0123456789" / "environments" / "gen"
    _write(data / "config.yaml", f"toolchain: {live_env}\n")

    output = _run(data, venv, tmp_path / "project", capsys)

    assert not stale.exists()
    assert stale2.is_dir()
    assert f"referenced by {link}" in output


def test_skips_stale_install_when_a_repair_record_points_at_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    venv = tmp_path / "venv"
    venv.mkdir()
    live = _live_install(data)
    stale = _stale(data)
    _write(live / "repair.json", json.dumps({"environment": str(stale)}))
    _write(
        data / "logs" / "update_receipts" / "latest.json",
        json.dumps({"environment": str(live / "environments" / "gen")}),
    )

    output = _run(data, venv, tmp_path / "project", capsys)

    assert stale.is_dir()
    assert f"referenced by {live / 'repair.json'}" in output
    assert (data / "logs" / "update_receipts" / "latest.json").is_file()


def test_skips_stale_install_without_a_different_active_toolchain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    venv = tmp_path / "venv"
    venv.mkdir()
    stale = _stale(data)
    env = stale / "environments" / "old"
    _write(stale / "facts.json", _facts(env))

    output = _run(data, venv, tmp_path / "project", capsys)

    assert stale.is_dir()
    assert "no different active installs directory" in output


def test_prefers_the_project_install_key_as_the_active_toolchain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    venv = tmp_path / "venv"
    project = tmp_path / "project"
    venv.mkdir()
    project.mkdir()
    key = cleanup_mod.install_key(project)
    keyed = _live_install(data, key)
    _live_install(data, "bbbbbbbbbbbbbbbb")
    stale = _stale(data)

    output = _run(data, venv, project, capsys)

    assert not stale.exists()
    assert keyed.is_dir()
    assert (data / "installs" / "bbbbbbbbbbbbbbbb").is_dir()
    assert f"install key {key}" in output


def test_does_not_follow_a_symlink_outside_the_data_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    outside = tmp_path / "outside" / "secret.txt"
    _write(outside, b"do-not-touch")
    link = data / "plugins" / ".install-rqzgcdjx"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside.parent)
    venv = tmp_path / "venv"
    venv.mkdir()

    output = _run(data, venv, tmp_path / "project", capsys)

    assert outside.read_bytes() == b"do-not-touch"
    assert not link.exists()
    assert f"deleted {link}" in output or f"dry-run {link}" in output


def test_missing_data_root_logs_free_space_and_skips(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing-data"
    output = _run(missing, tmp_path / "venv", tmp_path / "project", capsys)
    assert "free bytes before:" in output
    assert "free bytes after:" in output
    assert "is not a directory" in output
