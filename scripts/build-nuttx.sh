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

step "make -j$JOBS"
make -j"$JOBS"

[ -f nuttx.bin ] || die "nuttx.bin was not produced"

mkdir -p "$DIST_DIR"
cp nuttx.bin "$DIST_DIR/nuttx.bin"

[ -x "$ELFTOSB" ] || die "elftosb not built -- run scripts/build-sbtools.sh first"

step "packing BootStream"
"$ELFTOSB" -z -c "$ROOT/scripts/bootstream/nuttx.bd" \
           -o "$DIST_DIR/nuttx.sb" "$DIST_DIR/nuttx.bin"

python3 "$ROOT/scripts/verify-sb.py" "$DIST_DIR/nuttx.sb" \
  || die "dist/nuttx.sb is not a valid sb1 BootStream"

describe "$DIST_DIR/nuttx.bin"
describe "$DIST_DIR/nuttx.sb"

# The image must fit next to the page table and leave a heap. 512 KB SRAM, 16 KB
# of it reserved for the first-level page table at 0x7C000.
SIZE=$(wc -c <"$DIST_DIR/nuttx.bin" | tr -d ' ')
if [ "$SIZE" -gt $((512 * 1024 - 0x4000)) ]; then
  die "nuttx.bin is $SIZE bytes, which does not fit below the page table"
fi
note "nuttx.bin uses $((SIZE * 100 / (512 * 1024)))% of the 512 KB SRAM"
