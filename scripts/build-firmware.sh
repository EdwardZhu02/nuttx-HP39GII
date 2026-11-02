#!/usr/bin/env bash
#
# Build one OSLoader variant from the ExistOS tree and package it as an sb1
# BootStream.
#
#   ./scripts/build-firmware.sh bootmenu       -> dist/bootmenu.sb
#   ./scripts/build-firmware.sh nandrestore    -> dist/nandrestore.sb
#   ./scripts/build-firmware.sh nanddump       -> dist/nanddump-8m.sb
#   ./scripts/build-firmware.sh nanddump-full  -> dist/nanddump-full.sb
#   ./scripts/build-firmware.sh probe          -> dist/probe.sb
#   ./scripts/build-firmware.sh loader         -> dist/osloader.sb
#
# The variant is selected by environment variables read at CMake configure time
# (see OSLoader/CMakeLists.txt in the ExistOS tree), which is why each variant
# gets its own build directory.
#
# Only the OSLoader target is built. The System/ tree that the same CMake project
# configures is never compiled, so the micropython / KhiCAS submodules are not
# needed.

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

VARIANT=${1:-}
[ -n "$VARIANT" ] || {
  sed -n '2,20p' "$0" >&2
  exit 2
}

ENV_PREFIX=
OUT_NAME=

case "$VARIANT" in
  bootmenu)
    # Dual-boot menu, linked at 0x30000 and loaded there by the boot ROM.
    ENV_PREFIX="BOOTMENU=1"
    OUT_NAME=bootmenu.sb
    ;;
  nandrestore)
    # RAM-only firmware: writes a host-supplied NAND image back, page by page.
    ENV_PREFIX="RESTORE_ONLY=1"
    OUT_NAME=nandrestore.sb
    ;;
  nanddump)
    # RAM-only firmware: streams NAND data + OOB to the host. Default 8 MB,
    # enough to cover the boot chain, the loader and the stock system.
    ENV_PREFIX="DUMP_ONLY=1"
    OUT_NAME=nanddump-8m.sb
    ;;
  nanddump-full)
    # Same firmware with the whole 128 MB / 65536 pages in range.
    ENV_PREFIX="DUMP_ONLY=1 DUMP_PAGES=65536"
    OUT_NAME=nanddump-full.sb
    ;;
  probe)
    # Read-only hardware probe: chip ID, EMI, ICOLL, TIMROT, UART, address
    # aliasing. Writes nothing.
    ENV_PREFIX="PROBE_ONLY=1"
    OUT_NAME=probe.sb
    ;;
  loader)
    # The stock OSLoader, unmodified. Useful as a known-good SDP payload and as
    # the base image for the stock system.
    ENV_PREFIX=
    OUT_NAME=osloader.sb
    ;;
  *)
    die "unknown variant '$VARIANT' (bootmenu|nandrestore|nanddump|nanddump-full|probe|loader)"
    ;;
esac

[ -d "$EXISTOS_DIR" ] || die "$EXISTOS_DIR missing -- run scripts/fetch-deps.sh first"
[ -x "$ELFTOSB" ] || die "elftosb not built -- run scripts/build-sbtools.sh first"

need cmake
need "${CROSS_COMPILE}gcc" "install an arm-none-eabi toolchain, or set CROSS_COMPILE"

BUILD_DIR=$DEPS_DIR/.build/existos-$VARIANT

step "configuring $VARIANT"
# shellcheck disable=SC2086  # ENV_PREFIX is a space-separated list of assignments
env $ENV_PREFIX cmake -S "$EXISTOS_DIR" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=MinSizeRel >/dev/null

step "building OSLoader.sb ($VARIANT)"
# shellcheck disable=SC2086
env $ENV_PREFIX cmake --build "$BUILD_DIR" --target OSLoader.sb -j"$JOBS" >/dev/null

SRC=$BUILD_DIR/OSLoader/OSLoader.sb
[ -f "$SRC" ] || die "expected $SRC, not produced"

mkdir -p "$DIST_DIR"
cp "$SRC" "$DIST_DIR/$OUT_NAME"

python3 "$ROOT/scripts/verify-sb.py" "$DIST_DIR/$OUT_NAME" \
  || die "dist/$OUT_NAME is not a valid sb1 BootStream"

describe "$DIST_DIR/$OUT_NAME"
