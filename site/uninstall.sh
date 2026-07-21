#!/bin/sh
#
# yangble5 client uninstaller — macOS and Linux (POSIX sh).
#
#   curl -fsSL https://yangble5.com/uninstall.sh | sh -s -- --yes
#   sh uninstall.sh --dry-run     # print what would be deleted, delete nothing
#
# It prints every path it is about to delete BEFORE deleting anything, and it
# deletes nothing else. Specifically it will NOT touch:
#   * ~/.claude or ~/.codex  — your normal logins were never involved
#   * your shell rc files    — the installer never edited them
#   * anything outside ~/.yangble5 and symlinks that point into it
#
# Symlinks in ~/.local/bin are removed ONLY if they are symlinks AND they
# resolve into ~/.yangble5. A real file with the same name is left alone, so
# this cannot eat something you wrote yourself.
#
# Deleting the key here removes it from THIS MACHINE only. The server still
# has its hash and the key still works if someone else has a copy. If you
# think it leaked, ask the operator to revoke it server-side as well.
#
# Refuses to run as root, for the same reason the installer does.
#
# EXIT CODES
#   0  removed (or nothing was there to remove)
#   1  bad arguments, or refused because no confirmation was given
#   2  refused: running as root / under sudo
#   3  \$HOME is not usable
#
# SPDX-License-Identifier: MIT
#

set -eu

YB5_HOME="${HOME:-}/.yangble5"
LINK_DIR="${HOME:-}/.local/bin"
LAUNCHERS="yangble5-claude yangble5-codex yangble5-env yangble5-uninstall"

ASSUME_YES=0
DRY_RUN=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$(printf '\033[31m'); C_GRN=$(printf '\033[32m')
    C_YLW=$(printf '\033[33m'); C_BLD=$(printf '\033[1m')
    C_OFF=$(printf '\033[0m')
else
    C_RED=""; C_GRN=""; C_YLW=""; C_BLD=""; C_OFF=""
fi

usage() {
    cat <<'USAGE'
usage: sh uninstall.sh [options]

  --yes, -y     do not prompt (required when stdin is not a terminal)
  --dry-run     print exactly what would be deleted, then stop
  -h, --help    this text
USAGE
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)  ASSUME_YES=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage ;;
        *)         printf 'unknown option: %s\n' "$1" >&2; usage ;;
    esac
done

# ── refuse to run privileged ───────────────────────────────────────────────
uid="$(id -u 2>/dev/null || echo 0)"
if [ "$uid" = "0" ] || [ -n "${SUDO_USER:-}" ]; then
    cat >&2 <<'ROOT'

REFUSING TO RUN AS ROOT.

Everything this script deletes lives in a normal user's home directory. As
root it would resolve $HOME to /root and either delete the wrong thing or
nothing at all. Run it as the user who installed yangble5, without sudo.

ROOT
    exit 2
fi

if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
    printf '\$HOME is not set or not a directory; refusing to guess.\n' >&2
    exit 3
fi

# ── enumerate, then print, then (maybe) delete ─────────────────────────────
TARGETS=""
FOUND=0

if [ -d "$YB5_HOME" ]; then
    FOUND=1
fi

for name in $LAUNCHERS; do
    link="${LINK_DIR}/${name}"
    if [ -L "$link" ]; then
        target="$(readlink "$link" 2>/dev/null || echo '')"
        case "$target" in
            "${YB5_HOME}"/*)
                TARGETS="${TARGETS}${link}
"
                FOUND=1
                ;;
        esac
    fi
done

printf '\n%s%syangble5 uninstaller%s\n\n' "$C_BLD" "$C_RED" "$C_OFF"

if [ "$FOUND" -eq 0 ]; then
    printf '  Nothing to remove — no %s and no launcher symlinks pointing into it.\n\n' "$YB5_HOME"
    exit 0
fi

printf '  It will delete EXACTLY these paths and nothing else:\n\n'

if [ -d "$YB5_HOME" ]; then
    size="$(du -sh "$YB5_HOME" 2>/dev/null | cut -f1 || echo '?')"
    printf '    %s   (entire directory, %s)\n' "$YB5_HOME" "$size"
    for f in credentials machine-id env.sh INSTALL_INFO uninstall.sh; do
        if [ -e "${YB5_HOME}/${f}" ]; then
            printf '      - %s\n' "${YB5_HOME}/${f}"
        fi
    done
    for d in bin claude codex; do
        if [ -d "${YB5_HOME}/${d}" ]; then
            printf '      - %s/\n' "${YB5_HOME}/${d}"
        fi
    done
fi

if [ -n "$TARGETS" ]; then
    printf '\n'
    printf '%s' "$TARGETS" | while IFS= read -r line; do
        [ -n "$line" ] || continue
        printf '    %s   (symlink into %s)\n' "$line" "$YB5_HOME"
    done
fi

cat <<'KEEP'

  It will NOT touch:
    ~/.claude            your real Claude Code login
    ~/.codex             your real Codex config
    ~/.bashrc ~/.zshrc ~/.profile    never edited in the first place
    any file in ~/.local/bin that is not a symlink into ~/.yangble5

  Note: this removes your key from THIS MACHINE. The server keeps its hash and
  the key keeps working for anyone else holding a copy. If it may have leaked,
  ask the operator to revoke it server-side too.

KEEP

if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %sdry run — nothing was deleted.%s\n\n' "$C_YLW" "$C_OFF"
    exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
    if [ -t 0 ]; then
        printf '  Type %sYES%s to confirm: ' "$C_BLD" "$C_OFF"
        read -r answer
        if [ "$answer" != "YES" ]; then
            printf '  aborted; nothing was deleted.\n\n'
            exit 1
        fi
    else
        printf '  %sRefusing to delete without confirmation.%s\n' "$C_RED" "$C_OFF"
        printf '  Re-run with --yes if this is really what you want.\n\n'
        exit 1
    fi
fi

for name in $LAUNCHERS; do
    link="${LINK_DIR}/${name}"
    if [ -L "$link" ]; then
        target="$(readlink "$link" 2>/dev/null || echo '')"
        case "$target" in
            "${YB5_HOME}"/*)
                rm -f "$link"
                printf '  %sremoved%s %s\n' "$C_GRN" "$C_OFF" "$link"
                ;;
        esac
    fi
done

if [ -d "$YB5_HOME" ]; then
    rm -rf "$YB5_HOME"
    printf '  %sremoved%s %s\n' "$C_GRN" "$C_OFF" "$YB5_HOME"
fi

printf '\n  yangble5 is gone. Your normal Claude Code login was never touched —\n'
printf '  run `claude` and everything is exactly as it was.\n\n'
exit 0
