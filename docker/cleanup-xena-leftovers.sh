#!/bin/sh
# Remove two root-owned profile leftovers the hermes user cannot unlink.
# Each directory is removed only when it is a real directory whose only
# content is knowledge/README.md (regular file, no symlinks). Anything else
# is left alone. Missing directories are a no-op. Idempotent.
# shellcheck shell=sh
set -eu

HERMES_HOME="${1:-${HERMES_HOME:-/opt/data}}"

if [ "$(id -u)" != 0 ] && [ "${XENA_LEFTOVER_ALLOW_NONROOT:-}" != 1 ]; then
    echo "[xena-leftover] not root; skipping"
    exit 0
fi

profile_is_readme_only() {
    dir=$1
    [ -d "$dir" ] || return 1
    [ -L "$dir" ] && return 1
    names=$(find -P "$dir" -mindepth 1 -maxdepth 1 -printf '%P\n' | sort)
    [ "$names" = "knowledge" ] || return 1
    [ -d "$dir/knowledge" ] || return 1
    [ -L "$dir/knowledge" ] && return 1
    inner=$(find -P "$dir/knowledge" -mindepth 1 -maxdepth 1 -printf '%P\n' | sort)
    [ "$inner" = "README.md" ] || return 1
    [ -f "$dir/knowledge/README.md" ] || return 1
    [ -L "$dir/knowledge/README.md" ] && return 1
    deeper=$(find -P "$dir" -mindepth 3 -print -quit)
    [ -z "$deeper" ] || return 1
    return 0
}

for name in xena_management xena_clientes; do
    dir="$HERMES_HOME/profiles/$name"
    if [ ! -e "$dir" ]; then
        echo "[xena-leftover] absent $dir"
        continue
    fi
    if profile_is_readme_only "$dir"; then
        rm -rf -- "$dir"
        echo "[xena-leftover] removed $dir"
    else
        echo "[xena-leftover] skip $dir (not exactly knowledge/README.md)"
    fi
done
