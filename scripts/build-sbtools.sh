#!/usr/bin/env bash
#
# Build elftosb (and sbloader) from the ExistOS tree. elftosb turns a raw binary
# into the sb1 BootStream the STMP37xx boot ROM accepts.
#
#   ./scripts/build-sbtools.sh [--force]

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force|-f) FORCE=1 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

[ -d "$SBTOOLS_DIR" ] || die "$SBTOOLS_DIR missing -- run scripts/fetch-deps.sh first"

if [ "$FORCE" -eq 0 ] && [ -x "$ELFTOSB" ]; then
  note "elftosb already built: $ELFTOSB"
  exit 0
fi

need make
need pkg-config "libusb-1.0 and libcrypto++ are needed by sbtools"
need g++

pkg-config --exists libusb-1.0 \
  || die "libusb-1.0 development files not found
    Debian/Ubuntu: sudo apt-get install libusb-1.0-0-dev
    macOS:         brew install libusb"
pkg-config --exists libcrypto++ \
  || die "libcrypto++ development files not found
    Debian/Ubuntu: sudo apt-get install libcrypto++-dev
    macOS:         brew install crypto++"

step "building sbtools"
# sbtools derives its own LDFLAGS from pkg-config, but pkg-config does not list
# libudev as a dependency of libusb-1.0 even though a static libusb needs it, and
# some distributions ship exactly that. Build the link line here instead of
# fighting it later: elftosb is the tool we actually need, sbloader is a bonus.
LD_LIBS=$(pkg-config --libs libusb-1.0 libcrypto++)
if pkg-config --exists libudev 2>/dev/null; then
  LD_LIBS="$LD_LIBS $(pkg-config --libs libudev) -lpthread"
fi

make -C "$SBTOOLS_DIR" -j"$JOBS" LDFLAGS="$LD_LIBS"

[ -x "$ELFTOSB" ] || die "elftosb was not produced"
describe "$ELFTOSB"
