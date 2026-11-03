#!/usr/bin/env bash
#
# Build libkhicas.a: the resident half of the KhiCAS application (the HAL,
# the REPL and the C++ constructor runner that lives in the board directory).
#
#   ./scripts/build-khicas.sh
#
# build-nuttx.sh calls this before ``make`` when CONFIG_STMP3770_KHICAS is
# enabled.  The giac engine itself is not built here: its prebuilt archives
# come from the ExistOS tree (Libs/libcas.libcpp, Libs/libtommath.libcpp).
#
# Why a separate script instead of a normal NuttX rule: this code has to be
# compiled against the toolchain's newlib headers, not the NuttX ones.  It is
# linked against prebuilt libstdc++ objects that expect the newlib ABI, and
# the two header sets collide (ldiv_t, the ctype bit masks, ...).  The rule
# keeps those flags out of the kernel build.

set -euo pipefail

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.sh"

NUTTX_DIR=${NUTTX_DIR:-$DEPS_DIR/nuttx}
EXISTOS_DIR=${EXISTOS_DIR:-$DEPS_DIR/existos}

KHI=$EXISTOS_DIR/Libs/src/khicas
OUT=$NUTTX_DIR/boards/arm/stmp3770/hp39gii/src/khicas

[ -d "$KHI" ] || die "$KHI missing -- run scripts/fetch-deps.sh first"
[ -d "$OUT" ] || die "$OUT missing -- is patches/nuttx applied?"

CXX=${CROSS_COMPILE}g++
CC=${CROSS_COMPILE}gcc

# Thumb first (-marm is deliberate: the engine archives are ARM/Thumb mixed and
# the linker resolves interworking through the symbols), soft float because the
# ARM926EJ-S has no VFP, and -fno-exceptions/-fno-rtti because libsupc++'s
# exception runtime is not linked into a NuttX image.
CXXFLAGS="-Os -mcpu=arm926ej-s -mtune=arm926ej-s -marm -mfloat-abi=soft \
 -std=c++11 -fpermissive -fno-use-cxa-atexit -fno-strict-aliasing \
 -fno-rtti -fno-exceptions -ffunction-sections -fdata-sections \
 -DHP39 -DKHICAS -DHAVE_CONFIG_H \
 -include $KHI/config.h -I$KHI -I$KHI/../libtommath-0.39"
CFLAGS="-Os -mcpu=arm926ej-s -marm -mfloat-abi=soft -ffunction-sections -fdata-sections"

step "building libkhicas.a"

cd "$OUT"
rm -f khicas_app.o khicas_stubs.o khicas_compat.o libkhicas.a
$CXX $CXXFLAGS -c khicas_app.cpp  -o khicas_app.o
$CXX $CXXFLAGS -c khicas_stubs.cpp -o khicas_stubs.o
$CC  $CFLAGS   -c khicas_compat.c -o khicas_compat.o
${CROSS_COMPILE}ar rcs libkhicas.a khicas_app.o khicas_stubs.o khicas_compat.o

note "libkhicas.a: $(wc -c <libkhicas.a) bytes"
