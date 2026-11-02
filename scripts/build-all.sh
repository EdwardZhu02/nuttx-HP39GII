#!/usr/bin/env bash
#
# Build every artifact this repository produces, in dependency order.
#
#   ./scripts/build-all.sh
#
# Result in dist/:
#   nuttx.bin  nuttx.sb                     the NuttX port
#   bootmenu.sb                             the dual-boot menu
#   nandrestore.sb  nanddump-8m.sb
#   nanddump-full.sb  probe.sb              recovery / backup firmware
#   osloader.sb                             stock OSLoader (reference)
#   SHA256SUMS

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/config.sh"

"$HERE/fetch-deps.sh"
"$HERE/build-sbtools.sh"
"$HERE/build-nuttx.sh"

for variant in bootmenu nandrestore nanddump nanddump-full probe loader; do
  "$HERE/build-firmware.sh" "$variant"
done

step "summing up"
(
  cd "$DIST_DIR"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum ./*.sb ./*.bin >SHA256SUMS
  else
    shasum -a 256 ./*.sb ./*.bin >SHA256SUMS
  fi
  ls -l
  cat SHA256SUMS
) >&2

step "done: $DIST_DIR"
