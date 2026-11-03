#!/usr/bin/env bash
#
# Clone the pinned upstream trees into .deps/ and apply our patch series.
#
#   ./scripts/fetch-deps.sh            # fetch + patch (no-op if already done)
#   ./scripts/fetch-deps.sh --force    # throw .deps/ away first
#
# Nothing here needs network access after the first run; .deps/ is a build
# directory, never committed.

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force|-f) FORCE=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

need git

GIT_ID=(-c "user.name=nuttx-HP39GII build" -c "user.email=build@localhost")

# fetch_repo <dir> <url> <sha> <label>
fetch_repo() {
  local dir=$1 url=$2 sha=$3 label=$4

  if [ -d "$dir/.git" ] && [ "$(cat "$dir/.git/hp39gii-base" 2>/dev/null)" = "$sha" ]; then
    note "$label: already at ${sha:0:12}"
    return 0
  fi

  step "fetching $label at ${sha:0:12}"
  rm -rf "$dir"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" remote add origin "$url"
  # A shallow fetch of one exact commit: no history, no branches, no surprises
  # when upstream moves on.
  git -C "$dir" fetch -q --depth 1 origin "$sha" \
    || die "$label: cannot fetch $sha from $url"
  git -C "$dir" -c advice.detachedHead=false checkout -q FETCH_HEAD
  printf '%s\n' "$sha" > "$dir/.git/hp39gii-base"
}

# apply_series <dir> <patchdir> <expected-count>
apply_series() {
  local dir=$1 pdir=$2 want=$3 p n=0

  [ -d "$pdir" ] || die "patch directory not found: $pdir"
  for p in "$pdir"/*.patch; do
    [ -e "$p" ] || die "no patches in $pdir"
    if ! git -C "$dir" "${GIT_ID[@]}" am -q --whitespace=nowarn --keep-non-patch "$p"; then
      git -C "$dir" am --abort >/dev/null 2>&1 || true
      die "failed to apply $(basename "$p")
    The series is pinned to the upstream revision in scripts/config.sh.
    A failure here means the pin and the patches disagree -- re-check both,
    or run with --force to start from a clean fetch."
    fi
    n=$((n + 1))
  done

  [ "$n" -eq "$want" ] || die "expected $want patches in $pdir, applied $n"
  note "$(basename "$dir"): $n patches applied"
}

if [ "$FORCE" -eq 1 ]; then
  step "removing $DEPS_DIR"
  rm -rf "$DEPS_DIR"
fi

mkdir -p "$DEPS_DIR"

# ---------------------------------------------------------------------------
# Apache NuttX + apps
# ---------------------------------------------------------------------------

fetch_repo "$NUTTX_DIR" "$NUTTX_URL" "$NUTTX_BASE" nuttx
fetch_repo "$APPS_DIR" "$APPS_URL" "$APPS_BASE" nuttx-apps

if [ ! -f "$APPS_DIR/.hp39gii-patched" ]; then
  step "applying patches/apps"
  apply_series "$APPS_DIR" "$ROOT/patches/apps" 1 "$APPS_BASE"
  : > "$APPS_DIR/.hp39gii-patched"
else
  note "nuttx-apps: patch series already applied"
fi

if [ ! -f "$NUTTX_DIR/.hp39gii-patched" ]; then
  step "applying patches/nuttx"
  apply_series "$NUTTX_DIR" "$ROOT/patches/nuttx" 23 "$NUTTX_BASE"
  : > "$NUTTX_DIR/.hp39gii-patched"
else
  note "nuttx: patch series already applied"
fi

# ---------------------------------------------------------------------------
# ExistOS (boot menu + recovery firmware)
#
# Only OSLoader is ever built. System/ is configured but never compiled, so the
# Libs/ submodules (micropython, KhiCAS) are not needed and are not fetched.
# ---------------------------------------------------------------------------

fetch_repo "$EXISTOS_DIR" "$EXISTOS_URL" "$EXISTOS_BASE" existos

if [ ! -f "$EXISTOS_DIR/.hp39gii-patched" ]; then
  step "applying patches/existos"
  apply_series "$EXISTOS_DIR" "$ROOT/patches/existos" 2 "$EXISTOS_BASE"
  : > "$EXISTOS_DIR/.hp39gii-patched"
else
  note "existos: patch series already applied"
fi

step "dependencies ready in $DEPS_DIR"
