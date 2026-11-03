# Patch series

Three series, all pinned to an exact upstream commit (see `scripts/config.sh`).
They are applied with `git am`, in filename order, by `scripts/fetch-deps.sh`.

## `nuttx/` — 25 patches

Apache NuttX, base `1080f140`. The result is a tree that builds NuttX for
`hp39gii:nsh` and nothing else.

| # | Subject | What it adds |
|---|---|---|
| 0001 | add NuttX SoC port — boots NSH in EmuGII QEMU | `arch/arm/src/stmp3770` + `boards/arm/stmp3770/hp39gii`, register headers, linker script, defconfig, PL011 console, ICOLL, TIMROT |
| 0002 | add GPMI + ECC8 NAND driver | APBH descriptor chains, page read/write/erase, `0x90` geometry probe |
| 0003 | add NuttX MTD adapter over the GPMI NAND | `stmp3770_mtd.c` |
| 0004 | MTD byte-read + NAND filesystem bring-up | `bread` semantics needed by SmartFS |
| 0005 | document NAND MTD status + SmartFS data-abort issue | `STATUS.md` |
| 0006 | complete NAND → SmartFS filesystem path | format + mount + write/read |
| 0007 | enable SmartFS in defconfig | |
| 0008 | add LCD text console device (`/dev/lcdcon`) | 32×16 text renderer |
| 0009 | make the LCD text console NSH's console | `NSH_ALTCONDEV` |
| 0010 | document the LCD console | `STATUS.md` |
| 0011 | document `nuttx.sb` (elftosb) generation for SDP | |
| 0012 | boot to NSH on real hardware, keyboard input works | keymap, injection, the `pl011_earlyserialinit()` fix |
| 0013 | LCD console scrollback, auto-scroll and clear | |
| 0014 | enable the NAND driver, read-only hardware smoke test | |
| 0015 | F1 emits a space, add a bottom hint bar | |
| 0016 | NAND bring-up diagnostics | |
| 0017 | F2 types a minus, NAND bus state dump | |
| 0018 | mux the GPMI pins and enable the GPMI clock | |
| 0019 | run the GPMI from HF (PLL/2) like ExistOS | |
| 0020 | pin the NAND geometry, NAND reads verified on hardware | |
| 0021 | SmartFS partition layout, NCB backup repair, boot counter | final board bring-up (hand-written patch header, see below) |
| 0022 | size the NCB metadata buffer to the real OOB width | `meta[16]` held 19 bytes; fixes a `-Warray-bounds` warning |
| 0023 | LCD text console in 1bpp | 32 KB shadow buffer -> 4 KB; `.bss` 56,992 -> 28,576 B, heap +28,768 B |
| 0024 | bring the MMU up from C | two-section map + CP15 from `arm_boot()`; needed before paging, `arm_head.S` cannot be used here |
| 0025 | synchronous text pager | 3.5 MB window over NAND, filled from the abort handlers; 888/888 paged calls pass. Also fixes the abort vectors, which read the saved context from sp=0x9F after switching back to ABT mode |

Patch 0021 is a plain `git diff` with a message and diffstat written by hand. It
applies with `git am` like the others, but if you rebase the series, regenerate it
from a real commit.

## `apps/` — 1 patch

Apache NuttX apps, base `b86baec`.

| # | Subject | What it adds |
|---|---|---|
| 0001 | nshlib: add the `clear` command | writes `ESC[H ESC[2J`, which the LCD console and a real terminal both understand |

NuttX has no `clear` in NSH at this revision. Without it the only way to clear the
panel is to let the text scroll off.

## `existos/` — 2 patches

ExistOS-For-HP39GII, base `af623357`. Both patches only touch `OSLoader/`, never
`System/`, so the `Libs/` submodules are not needed to build anything here.

| # | Subject | What it adds |
|---|---|---|
| 0001 | NAND dump and restore firmwares over USB CDC | `OSLoader/nanddump.c`, `nandrestore.c`, `probe.c` and the build switches |
| 0002 | dual-boot menu | `OSLoader/bootmenu.c`, `Script/build_menu.bd`, `ldr_ld_30000.script`, the NuttX hand-off fixes |

The variants are selected by environment variables at CMake configure time:
`BOOTMENU`, `RESTORE_ONLY`, `DUMP_ONLY`, `PROBE_ONLY`, plus `BOOTMENU_AUTO` and
`BOOTMENU_HP` for the two menu experiments.

## What would have to change to upstream this

The NuttX series is a working port, not a submittable patch set. Known items:

* **`sched/init/nx_start.c` is patched** with `stmp3770_stage(n)` calls — a
  bring-up trace hook that writes to a debug buffer at various points in the boot
  sequence. It is unconditional, so the patched tree only links for this board.
  It must become `#ifdef CONFIG_STMP3770_*` or, better, be removed.
* **`drivers/serial/uart_pl011.c` gains `pl011_console_device()`** so that
  non-UART input (the keyboard matrix) can be injected into the console's receive
  buffer. Reasonable, but needs an API review.
* **`scripts/ld.script` is a committed symlink** to the board linker script.
  `distclean` deletes it, and `configure.sh` does not recreate it, which is why it
  was committed. Upstream would want `configure.sh` to create it.
* **`boards/arm/stmp3770/hp39gii/tools/` is a copy of the LPC31xx `lpchdr`
  tooling** (`mkhp39gii.sh` still refers to lpc31xx paths and does not work). The
  real image packaging happens through `elftosb` in `scripts/`. The directory
  should be deleted.
* `arch/arm/src/stmp3770/hw/*.h` are large register headers reconstructed from
  ExistOS and NXP i.MX28 headers. They carry NXP copyright fragments and would
  need a licensing pass before submission.
* `STATUS.md` in `arch/arm/src/stmp3770/` is the original bring-up log, in
  Chinese. It is history, not documentation; it would not go upstream.

None of this affects the build or the behaviour described in `docs/`.
