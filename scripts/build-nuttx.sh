#!/usr/bin/env bash
#
# Configure and build the NuttX port, then wrap the raw binary in an sb1
# BootStream so the boot ROM can take it over USB.
#
#   ./scripts/build-nuttx.sh                 # configure on first run, then build
#   ./scripts/build-nuttx.sh --reconfigure   # start from .config again
#   ./scripts/build-nuttx.sh --clean         # make distclean first
#
# Outputs: dist/nuttx.bin, dist/nuttx.sb

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

RECONFIGURE=0
CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --reconfigure) RECONFIGURE=1 ;;
    --clean) CLEAN=1; RECONFIGURE=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

[ -d "$NUTTX_DIR" ] || die "$NUTTX_DIR missing -- run scripts/fetch-deps.sh first"

need "${CROSS_COMPILE}gcc" "install an arm-none-eabi toolchain, or set CROSS_COMPILE"

# NuttX records the build host in .config; pick the right one.
case "$(uname -s)" in
  Darwin) HOST_OPT=-m ;;
  Linux)  HOST_OPT=-l ;;
  *)      HOST_OPT= ; warn "unknown host $(uname -s), letting configure.sh decide" ;;
esac

cd "$NUTTX_DIR"

# ---------------------------------------------------------------------------
# kconfig tooling.
#
# NuttX needs a kconfig implementation for two things: tools/sethost.sh rewrites
# the host selection with kconfig-tweak (kconfig-frontends only), and ``make``
# silently re-runs ``olddefconfig`` when a Kconfig file is newer than .config --
# and apps_preconfig rewrites the apps' Kconfig files on every build, so that
# happens every time.
#
# kconfiglib (a pip package) provides everything except kconfig-tweak, and
# NuttX prefers it when it is on PATH. So: use whatever is installed, otherwise
# build a throwaway venv in .deps/.venv, and fall back to the bundled
# kconfig-tweak shim for the one tool kconfiglib does not ship.
# ---------------------------------------------------------------------------

if command -v menuconfig >/dev/null 2>&1 || command -v kconfig-conf >/dev/null 2>&1; then
  note "kconfig tool: $(command -v menuconfig || command -v kconfig-conf)"
else
  # Install kconfiglib into a private directory: no root, no virtualenv (the
  # python3-venv package is missing on plenty of systems), no change to the
  # user's environment.
  KC_DIR=$DEPS_DIR/pylib
  if [ ! -x "$KC_DIR/bin/olddefconfig" ]; then
    step "installing kconfiglib into $KC_DIR"
    python3 -m pip --version >/dev/null 2>&1 \
      || die "python3 pip is not available; install kconfiglib another way:
    pip3 install --user kconfiglib
  or install kconfig-frontends, or python3-venv"
    python3 -m pip install --quiet --target "$KC_DIR" kconfiglib \
      || die "pip could not install kconfiglib (no network?)"
  fi
  export PATH="$KC_DIR/bin:$PATH"
  export PYTHONPATH="$KC_DIR${PYTHONPATH:+:$PYTHONPATH}"
  note "kconfig tool: kconfiglib in $KC_DIR"
fi

if ! command -v kconfig-tweak >/dev/null 2>&1; then
  export PATH="$ROOT/scripts/tools:$PATH"
  note "kconfig-tweak not installed; using the bundled shim"
fi

if [ "$CLEAN" -eq 1 ]; then
  step "make distclean"
  make distclean >/dev/null 2>&1 || true
fi

if [ ! -f .config ] || [ "$RECONFIGURE" -eq 1 ]; then
  step "configure hp39gii:nsh"
  # configure.sh copies the board defconfig to .config, links Make.defs and
  # creates include/arch/board. It does not need kconfig-frontends.
  ./tools/configure.sh ${HOST_OPT:+"$HOST_OPT"} hp39gii:nsh
else
  note "reusing existing .config"
fi

# The KhiCAS application has a resident half that is not part of the kernel
# build: it is compiled against the toolchain headers (newlib ABI) because
# that is what the prebuilt giac archives use.  See scripts/build-khicas.sh.
if grep -q '^CONFIG_STMP3770_KHICAS=y' .config; then
  "$ROOT/scripts/build-khicas.sh"
fi

step "make -j$JOBS"
make -j"$JOBS"

[ -f nuttx ] || die "nuttx was not produced"

mkdir -p "$DIST_DIR"

# With KhiCAS the ELF carries the engine in .pgtext, linked into the paged
# window at 0x400000.  That section is not part of the resident image: the
# loader maps one 4 KB page of it at a time out of dist/khicas.bin, which
# install_menu.py writes to NAND.  The resident image is everything else.
if grep -q '^CONFIG_STMP3770_KHICAS=y' .config; then
  "${CROSS_COMPILE}objcopy" -O binary -R .pgtext nuttx "$DIST_DIR/nuttx.bin"
  "${CROSS_COMPILE}objcopy" -O binary -j .pgtext nuttx "$DIST_DIR/khicas.bin"
else
  "${CROSS_COMPILE}objcopy" -O binary nuttx "$DIST_DIR/nuttx.bin"
fi

[ -x "$ELFTOSB" ] || die "elftosb not built -- run scripts/build-sbtools.sh first"

step "packing BootStream"
"$ELFTOSB" -z -c "$ROOT/scripts/bootstream/nuttx.bd" \
           -o "$DIST_DIR/nuttx.sb" "$DIST_DIR/nuttx.bin"

python3 "$ROOT/scripts/verify-sb.py" "$DIST_DIR/nuttx.sb" \
  || die "dist/nuttx.sb is not a valid sb1 BootStream"

describe "$DIST_DIR/nuttx.bin"
if [ -f "$DIST_DIR/khicas.bin" ]; then
  describe "$DIST_DIR/khicas.bin"
fi
describe "$DIST_DIR/nuttx.sb"

# The menu copies this image to 0x0 while it runs from 0x50000, so the image
# has to end below the menu (the menu refuses anything over 0x4F000 itself).
SIZE=$(wc -c <"$DIST_DIR/nuttx.bin" | tr -d ' ')
if [ "$SIZE" -gt $((0x4f000)) ]; then
  die "nuttx.bin is $SIZE bytes; the boot menu at 0x50000 can only load 0x4f000"
fi
note "nuttx.bin uses $((SIZE * 100 / (512 * 1024)))% of the 512 KB SRAM"
