#!/usr/bin/env bash
# Shared configuration for every script in scripts/. Source it, do not run it.
#
#   . "$(dirname "$0")/config.sh"
#
# Every path can be overridden from the environment: DEPS_DIR, DIST_DIR, JOBS,
# CROSS_COMPILE, CC_FOR_BUILD.

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# ---------------------------------------------------------------------------
# Pinned upstream revisions.
#
# These are full commit IDs, not branches: the patch series is only guaranteed
# to apply to exactly these trees, and "it built yesterday" should stay true.
# Bump them deliberately, together with the patches.
# ---------------------------------------------------------------------------

NUTTX_URL=https://github.com/apache/nuttx.git
NUTTX_BASE=1080f1400be1b13153a3893467abba76d6352662

APPS_URL=https://github.com/apache/nuttx-apps.git
APPS_BASE=b86baecee51f5878ad8dfb93d5203001e571f1fb

EXISTOS_URL=https://github.com/ExistOS-Team/ExistOS-For-HP39GII.git
EXISTOS_BASE=af6233575451e3a6420320223734e35674069d54

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DEPS_DIR=${DEPS_DIR:-$ROOT/.deps}
DIST_DIR=${DIST_DIR:-$ROOT/dist}

NUTTX_DIR=$DEPS_DIR/nuttx
APPS_DIR=$DEPS_DIR/apps
EXISTOS_DIR=$DEPS_DIR/existos

SBTOOLS_DIR=$EXISTOS_DIR/tools/sbtools
ELFTOSB=$SBTOOLS_DIR/elftosb

CROSS_COMPILE=${CROSS_COMPILE:-arm-none-eabi-}

if [ -z "${JOBS:-}" ]; then
  JOBS=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)
fi

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_STEP=$'\033[1;36m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_OFF=$'\033[0m'
else
  C_STEP=''; C_WARN=''; C_ERR=''; C_OFF=''
fi

step() { printf '%s==> %s%s\n' "$C_STEP" "$*" "$C_OFF" >&2; }
note() { printf '    %s\n' "$*" >&2; }
warn() { printf '%swarning:%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }

die() {
  printf '%serror:%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "required tool not found: $1${2:+ ($2)}"
}

# Byte size and SHA256 of a build product, for the CI log.
_sha256_short() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -c1-16
  else
    shasum -a 256 "$1" | cut -c1-16
  fi
}

describe() {
  [ -f "$1" ] || die "missing artifact: $1"
  printf '    %-28s %8d bytes  sha256 %s\n' \
    "$(basename "$1")" "$(wc -c <"$1" | tr -d ' ')" "$(_sha256_short "$1")" >&2
}
