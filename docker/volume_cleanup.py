#!/usr/bin/env python3
"""Delete an allowlist of stale paths under the data volume at container boot.

Logs every candidate and its size, then deletes only that candidate. Missing
paths are skipped. Nothing outside the data root is deleted. Safe to run
twice: the second run finds the paths gone and skips them.

One summary line (paths removed and bytes freed) is printed and written to
``<data>/logs/volume-cleanup.log``. The directory is created when missing.
The file is never written through a symlink or outside the data root.
Production data root is /opt/data, so the file is /opt/data/logs/volume-cleanup.log.

Production uses /opt/data and /opt/hermes/.venv. Tests pass other roots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path


STALE_INSTALL_PREFIX = "351f81cc"
ROTATED_LOG_GLOBS = ("agent.log.[0-9]*", "errors.log.[0-9]*")
PLUGIN_INSTALL_LEFTOVERS = (".install-rqzgcdjx", ".install-61yo688a")
SUMMARY_LOG_NAME = "volume-cleanup.log"
RECORD_NAMES = {
    "config.yaml",
    "config.yml",
    "profile.yaml",
    "facts.json",
    "publication.json",
    "selected.json",
    "active.json",
}
# A repair receipt or config larger than this is treated as a live reference.
# Reading it would be slow, and skipping the delete is the safe outcome.
MAX_RECORD_BYTES = 2_000_000


class CheckError(Exception):
    """A safety check could not be completed. The caller must not delete."""


def _log(message: str) -> None:
    print(f"[volume-cleanup] {message}", flush=True)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _within(path: Path, root: Path) -> bool:
    try:
        _absolute(path).relative_to(_absolute(root))
    except ValueError:
        return False
    return True


def _same_or_under(path: Path, root: Path) -> bool:
    path_abs = _absolute(path)
    root_abs = _absolute(root)
    return path_abs == root_abs or _within(path_abs, root_abs)


def _link_target(path: Path) -> Path:
    raw = os.readlink(path)
    target = Path(raw)
    if not target.is_absolute():
        target = path.parent / target
    return Path(os.path.normpath(str(target)))


def _resolved_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def byte_size(path: Path) -> int:
    """Bytes of this path, not following symlinks into other trees."""
    try:
        info = path.lstat()
    except OSError:
        return 0
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return info.st_size
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):  # size is best-effort
        try:
            total += Path(dirpath).lstat().st_size
        except OSError:
            pass
        for name in dirnames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                try:
                    total += entry.lstat().st_size
                except OSError:
                    pass
        for name in filenames:
            entry = Path(dirpath) / name
            try:
                total += entry.lstat().st_size
            except OSError:
                pass
    return total


def _mount_point(path: Path) -> str:
    target = path if path.exists() else path.parent
    try:
        resolved = str(target.resolve())
    except OSError:
        resolved = str(_absolute(target))
    best = resolved
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return best
    best_len = -1
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        mount = parts[1].replace("\\040", " ")
        if resolved == mount or resolved.startswith(mount.rstrip("/") + "/"):
            if len(mount) > best_len:
                best = mount
                best_len = len(mount)
    return best


def _log_free(label: str, data: Path) -> None:
    probe = data if data.exists() else data.parent
    if not probe.exists():
        _log(f"free bytes {label}: unknown ({data} is missing)")
        return
    try:
        usage = os.statvfs(probe)
    except OSError as exc:
        _log(f"free bytes {label}: unknown ({exc})")
        return
    free = usage.f_bavail * usage.f_frsize
    _log(
        f"free bytes {label}: {free} on {_mount_point(probe)} "
        f"(filesystem holding {data})"
    )


def _delete(path: Path, data: Path) -> tuple[bool, int]:
    """Log the path and its size, then delete it if it stays inside data.

    Returns whether the path was removed and how many bytes ``byte_size``
    reported before the delete. A skip returns ``(False, 0)``.
    """
    if not os.path.lexists(path):
        _log(f"skip missing {path}")
        return False, 0
    if _absolute(path) == _absolute(data):
        _log(f"skip {path} bytes={byte_size(path)} reason=refuses to delete the data root")
        return False, 0
    if not _within(path, data):
        _log(f"skip {path} bytes={byte_size(path)} reason=outside {data}")
        return False, 0
    size = byte_size(path)
    try:
        if path.is_symlink():
            # Unlink the symlink itself. Do not follow it.
            _log(f"dry-run {path} bytes={size}")
            path.unlink()
        elif not _resolved_within(path, data):
            _log(f"skip {path} bytes={size} reason=resolves outside {data}")
            return False, 0
        else:
            _log(f"dry-run {path} bytes={size}")
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    except OSError as exc:
        _log(f"skip {path} bytes={size} reason={exc}")
        return False, 0
    _log(f"deleted {path}")
    return True, size


def _delete_counted(path: Path, data: Path, removed: list[tuple[Path, int]]) -> None:
    deleted, size = _delete(path, data)
    if deleted:
        removed.append((_absolute(path), size))


def _fnmatch_rotated(name: str) -> bool:
    # fnmatch is fine, but keep the rule obvious: agent.log.<digit>... or
    # errors.log.<digit>..., and never the live agent.log / errors.log.
    import fnmatch

    return any(fnmatch.fnmatch(name, pattern) for pattern in ROTATED_LOG_GLOBS)


def _rotated_log_dirs(data: Path) -> list[Path]:
    dirs = [data / "logs"]
    profiles = data / "profiles"
    if not profiles.is_dir() or profiles.is_symlink():
        if profiles.is_symlink():
            _log(f"skip {profiles} reason=profiles path is a symlink")
        return dirs
    if not _resolved_within(profiles, data):
        _log(f"skip {profiles} reason=resolves outside {data}")
        return dirs
    try:
        children = sorted(profiles.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        _log(f"skip {profiles} reason={exc}")
        return dirs
    for child in children:
        if child.is_symlink() and not _resolved_within(child, data):
            _log(f"skip {child} reason=profile symlink resolves outside {data}")
            continue
        dirs.append(child / "logs")
    return dirs


def _clean_rotated_logs(data: Path, removed: list[tuple[Path, int]]) -> None:
    for logs in _rotated_log_dirs(data):
        if not os.path.lexists(logs):
            if logs == data / "logs":
                _log(f"skip missing {logs}")
            continue
        if logs.is_symlink() or not _resolved_within(logs, data):
            _log(f"skip {logs} reason=log dir resolves outside {data}")
            continue
        if not logs.is_dir():
            _log(f"skip {logs} reason=not a directory")
            continue
        try:
            names = sorted(entry.name for entry in logs.iterdir())
        except OSError as exc:
            _log(f"skip {logs} reason={exc}")
            continue
        matches = [logs / name for name in names if _fnmatch_rotated(name)]
        if not matches:
            _log(f"no rotated agent/errors logs in {logs}")
            continue
        for match in matches:
            _delete_counted(match, data, removed)


def install_key(project: Path) -> str:
    """Same key PM uses: sha256 of the resolved project path, first 16 hex."""
    canonical = str(project.resolve())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _selected_environment(install_dir: Path) -> Path | None:
    facts = install_dir / "facts.json"
    if not facts.is_file() or facts.is_symlink():
        return None
    try:
        payload = json.loads(facts.read_text(encoding="utf-8"))
        value = payload.get("packages", {}).get("venv", {}).get("environment")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(value, str) or not value:
        return None
    environment = Path(value)
    try:
        environment.resolve().relative_to(install_dir.resolve())
    except (OSError, ValueError):
        return None
    if (environment / "pyvenv.cfg").is_file():
        return environment
    return None


def _discover_active(data: Path, project: Path) -> tuple[Path | None, list[Path], str]:
    """Return (active dir or None, dirs that must be treated as live, reason)."""
    installs = data / "installs"
    if not installs.is_dir() or installs.is_symlink():
        return None, [], "no installs directory"
    try:
        children = [child for child in installs.iterdir() if child.is_dir() and not child.is_symlink()]
    except OSError as exc:
        raise CheckError(f"cannot list {installs}: {exc}") from exc
    live = []
    for child in children:
        if child.name.startswith(STALE_INSTALL_PREFIX):
            continue
        if _selected_environment(child) is not None:
            live.append(child)
    key = install_key(project)
    keyed = installs / key
    if keyed in live:
        return keyed, [keyed], f"install key {key} of {project}"
    if len(live) == 1:
        return live[0], live, f"selected environment in {live[0].name}"
    if len(live) > 1:
        names = ", ".join(child.name for child in live)
        return None, live, f"multiple active installs directories: {names}"
    return None, [], "no different active installs directory"


def _text_references(text: str, candidate: Path) -> bool:
    absolute = str(_absolute(candidate))
    if absolute in text:
        return True
    needle = f"installs/{candidate.name}"
    start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return False
        after = found + len(needle)
        if after >= len(text) or text[after] in "/\\\"' \t\r\n,]}":
            return True
        start = after


def _is_record(path: Path) -> bool:
    if path.name in RECORD_NAMES or "repair" in path.name.lower():
        return True
    return path.name == "latest.json" and path.parent.name == "update_receipts"


def _references(data: Path, candidate: Path) -> list[str]:
    """Profiles, symlinks, configs, and pm repair records that point at candidate."""
    found: list[str] = []
    candidate_abs = _absolute(candidate)
    for dirpath, dirnames, filenames in _walk(data):
        current = Path(dirpath)
        kept = []
        for name in dirnames:
            child = current / name
            if _absolute(child) == candidate_abs or _same_or_under(child, candidate):
                continue
            kept.append(name)
        dirnames[:] = kept
        entries = [(current / name) for name in dirnames + filenames]
        for entry in entries:
            if _absolute(entry) == candidate_abs or _same_or_under(entry, candidate):
                continue
            if entry.is_symlink():
                try:
                    target = _link_target(entry)
                except OSError as exc:
                    found.append(f"{entry} (unreadable symlink: {exc})")
                    continue
                if _same_or_under(target, candidate) or _absolute(target) == candidate_abs:
                    found.append(str(entry))
            if not entry.is_symlink() and entry.is_file() and _is_record(entry):
                found.extend(_record_reference(entry, candidate))
    return found


def _record_reference(path: Path, candidate: Path) -> list[str]:
    try:
        info = path.lstat()
    except OSError as exc:
        return [f"{path} (unreadable record: {exc})"]
    if info.st_size > MAX_RECORD_BYTES:
        return [f"{path} (record too large to prove it does not point at the candidate)"]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"{path} (unreadable record: {exc})"]
    if _text_references(text, candidate):
        return [str(path)]
    return []


def _device(path: Path) -> int | None:
    try:
        return path.stat().st_dev
    except OSError:
        return None


def _walk(root: Path):
    def onerror(exc: OSError) -> None:
        raise CheckError(f"cannot list {exc.filename}: {exc}") from exc

    return os.walk(root, followlinks=False, onerror=onerror)


def _files_with_extra_links(root: Path) -> dict[tuple[int, int], Path]:
    found: dict[tuple[int, int], Path] = {}
    if not root.exists() or root.is_symlink():
        return found

    def consider(entry: Path) -> None:
        try:
            info = entry.lstat()
        except OSError as exc:
            raise CheckError(f"cannot stat {entry}: {exc}") from exc
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            found.setdefault((info.st_dev, info.st_ino), entry)

    if root.is_file():
        consider(root)
        return found
    for dirpath, _dirnames, filenames in _walk(root):
        for name in filenames:
            consider(Path(dirpath) / name)
    return found


def _hardlink_between(left: Path, right: Path) -> str | None:
    if not left.exists() or not right.exists():
        return None
    left_dev = _device(left)
    right_dev = _device(right)
    if left_dev is None or right_dev is None:
        raise CheckError(f"cannot stat devices for {left} and {right}")
    if left_dev != right_dev:
        return None
    left_inodes = _files_with_extra_links(left)
    if not left_inodes:
        return None
    right_inodes = _files_with_extra_links(right)
    shared = set(left_inodes) & set(right_inodes)
    if not shared:
        return None
    device, inode = next(iter(shared))
    return f"{left_inodes[(device, inode)]} hardlinked to {right_inodes[(device, inode)]}"


def _symlink_into(root: Path, dest: Path) -> str | None:
    if not root.exists() or root.is_symlink():
        return None
    dest_abs = _absolute(dest)
    try:
        dest_resolved = dest.resolve()
    except OSError:
        dest_resolved = dest_abs
    for dirpath, dirnames, filenames in _walk(root):
        current = Path(dirpath)
        for name in list(dirnames) + list(filenames):
            entry = current / name
            if not entry.is_symlink():
                continue
            try:
                target = _link_target(entry)
            except OSError as exc:
                raise CheckError(f"cannot read symlink {entry}: {exc}") from exc
            if _same_or_under(target, dest) or _absolute(target) == dest_abs:
                return str(entry)
            try:
                if _same_or_under(target.resolve(), dest_resolved):
                    return str(entry)
            except OSError:
                continue
    return None


def _cache_block_reason(cache: Path, venv: Path, live_dirs: list[Path]) -> str | None:
    if cache.is_symlink():
        target = _link_target(cache)
        for root in [venv, *live_dirs]:
            if _same_or_under(target, root):
                return f"cache path is a symlink into {root}"
    roots = [venv, *live_dirs]
    for root in roots:
        if not root.exists():
            continue
        shared = _hardlink_between(cache, root)
        if shared:
            return f"hardlinked to {root}: {shared}"
        link = _symlink_into(root, cache)
        if link:
            return f"required: {link} points into the cache"
    return None


def _clean_cache(data: Path, venv: Path, live_dirs: list[Path], removed: list[tuple[Path, int]]) -> None:
    cache = data / "cache" / "uv"
    if not os.path.lexists(cache):
        _log(f"skip missing {cache}")
        return
    try:
        reason = _cache_block_reason(cache, venv, live_dirs)
    except CheckError as exc:
        _log(f"skip {cache} bytes={byte_size(cache)} reason={exc}")
        return
    if reason:
        _log(f"skip {cache} bytes={byte_size(cache)} reason={reason}")
        return
    _delete_counted(cache, data, removed)


def _clean_stale_installs(
    data: Path,
    active: Path | None,
    active_reason: str,
    project: Path,
    removed: list[tuple[Path, int]],
) -> None:
    pattern = data / "installs" / f"{STALE_INSTALL_PREFIX}*"
    installs = data / "installs"
    if not installs.is_dir() or installs.is_symlink():
        _log(f"skip missing {pattern}")
        return
    try:
        matches = sorted(
            (child for child in installs.iterdir() if child.name.startswith(STALE_INSTALL_PREFIX)),
            key=lambda item: item.name,
        )
    except OSError as exc:
        _log(f"skip {pattern} reason={exc}")
        return
    if not matches:
        _log(f"skip missing {pattern}")
        return
    project_key = install_key(project)
    for candidate in matches:
        if candidate.name == project_key:
            # 351f81cc06284494 is sha256("/opt/hermes")[:16]: the project's own
            # install key, i.e. the live toolchain. Never a stale directory,
            # even when another install has a selected environment.
            _log(f"skip {candidate} bytes={byte_size(candidate)} reason=is the install key of {project}")
            continue
        if active is None or _absolute(active) == _absolute(candidate) or _same_or_under(active, candidate):
            _log(
                f"skip {candidate} bytes={byte_size(candidate)} "
                f"reason=active toolchain is not a different installs directory ({active_reason})"
            )
            continue
        try:
            refs = _references(data, candidate)
        except (OSError, CheckError) as exc:
            _log(f"skip {candidate} bytes={byte_size(candidate)} reason=reference check failed: {exc}")
            continue
        if refs:
            for ref in refs:
                _log(f"skip {candidate} bytes={byte_size(candidate)} reason=referenced by {ref}")
            continue
        _log(f"active toolchain {active} ({active_reason})")
        _delete_counted(candidate, data, removed)


def _summary_line(removed: list[tuple[Path, int]]) -> str:
    ordered = sorted(removed, key=lambda item: str(item[0]))
    total = sum(size for _path, size in ordered)
    paths = ",".join(str(path) for path, _size in ordered)
    return f"[volume-cleanup] summary removed={len(ordered)} bytes_freed={total} paths={paths}"


def _write_summary_file(data: Path, line: str) -> None:
    """Write ``line`` to ``<data>/logs/volume-cleanup.log``, or skip.

    Creates ``logs`` when it is missing. Refuses a symlink and any path that
    resolves outside the data root, so a hostile ``logs`` link cannot publish
    the summary somewhere else.
    """
    if not data.is_dir() or data.is_symlink():
        return
    logs = data / "logs"
    if logs.is_symlink() or not _within(logs, data):
        _log(f"skip summary file reason=refuses to write outside {data}")
        return
    try:
        if logs.exists() and (not logs.is_dir() or not _resolved_within(logs, data)):
            _log(f"skip summary file reason=logs dir resolves outside {data}")
            return
        logs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log(f"skip summary file reason={exc}")
        return
    if logs.is_symlink() or not logs.is_dir() or not _resolved_within(logs, data):
        _log(f"skip summary file reason=logs dir resolves outside {data}")
        return
    target = logs / SUMMARY_LOG_NAME
    if target.is_symlink() or not _within(target, data):
        _log(f"skip summary file reason=refuses to write outside {data}")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(target, flags, 0o644)
    except OSError as exc:
        _log(f"skip summary file reason={exc}")
        return
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    except OSError as exc:
        _log(f"skip summary file reason={exc}")
    finally:
        os.close(fd)


def _emit_summary(data: Path, removed: list[tuple[Path, int]]) -> None:
    line = _summary_line(removed)
    print(line, flush=True)
    _write_summary_file(data, line)


def cleanup(data: Path, venv: Path, project: Path) -> None:
    """Run the allowlisted cleanup against one data root."""
    data = _absolute(data)
    venv = _absolute(venv)
    project = _absolute(project)
    removed: list[tuple[Path, int]] = []
    try:
        _log_free("before", data)
        if not data.is_dir():
            _log(f"skip: {data} is not a directory")
            _log_free("after", data)
            return
        _clean_rotated_logs(data, removed)
        plugins = data / "plugins"
        for name in PLUGIN_INSTALL_LEFTOVERS:
            _delete_counted(plugins / name, data, removed)
        _delete_counted(data / ".npm" / "_cacache", data, removed)
        _delete_counted(data / "skills" / ".hub" / "index-cache", data, removed)
        try:
            active, live_dirs, reason = _discover_active(data, project)
        except CheckError as exc:
            _log(f"skip {data / 'cache' / 'uv'} reason={exc}")
            _log(f"skip {data / 'installs' / (STALE_INSTALL_PREFIX + '*')} reason={exc}")
            _log_free("after", data)
            return
        _log(f"active installs: {active if active is not None else 'none'} ({reason})")
        _clean_cache(data, venv, live_dirs, removed)
        _clean_stale_installs(data, active, reason, project, removed)
        _log_free("after", data)
    finally:
        _emit_summary(data, removed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/opt/data"))
    parser.add_argument("--venv", type=Path, default=Path("/opt/hermes/.venv"))
    parser.add_argument("--project", type=Path, default=Path("/opt/hermes"))
    args = parser.parse_args(argv)
    cleanup(args.data, args.venv, args.project)
    return 0


if __name__ == "__main__":
    sys.exit(main())
