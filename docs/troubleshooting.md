# Pitfalls

Everything here cost at least one debug cycle on real hardware or in the emulator.
They are ordered by layer, not by severity. Each entry is *symptom → cause → fix*.

## Build

**`configure.sh` succeeds, `make` cannot find `board.h`.**
`ARCH_BOARD_HP39GII` was never registered in `boards/Kconfig`, so kconfig fell back
to `ARCH_BOARD_CUSTOM` with an empty name and the include path resolved to
`TOPDIR/src`. The port adds `config ARCH_BOARD_HP39GII`, its `default "hp39gii"`
line and a `source` block, plus `default "stmp3770" if ARCH_CHIP_STMP3770` in
`arch/arm/Kconfig`.

**`kconfig-conf --olddefconfig` recurses until it dies.**
`boards/dummy/Kconfig` includes itself in this tree. Don't run `olddefconfig`;
edit `.config` and let `make` regenerate.

**`clean_context` removes `include/nuttx/config.h` and `include/arch/board`.**
If `arch/arm/src/board` then points somewhere wrong, check `CONFIG_ARCH_BOARD`
and `CONFIG_ARCH_CHIP` strings in `.config`.

**Header changes do not take effect.**
The board `src/Makefile` hard-codes `CSRCS` and the dependency tracking does not
notice new headers. `touch` the `.c` files that include the header.

**`scripts/ld.script` disappears after `distclean`.**
The board makes `TOPDIR/scripts/ld.script` a symlink to
`boards/arm/stmp3770/hp39gii/scripts/ld.script`; `distclean` deletes it and
nothing recreates it. Re-run `./tools/configure.sh`, or recreate the symlink.

**CMake builds `System/` and fails on missing `Libs/*.libcpp`.**
The ExistOS top-level project contains both `System` and `OSLoader`. Only
`OSLoader.sb` is ever built here, so the KhiCAS / micropython prebuilt libraries
are not needed — configure succeeds, and `--target OSLoader.sb` never touches
them. Do not `cmake --build` without a target.

**`elftosb` fails to link: `libudev` not found.**
`pkg-config --libs libusb-1.0` omits `-ludev` on some systems. Install
`libudev-dev`, or pass it explicitly:
`make -C tools/sbtools LDFLAGS="$(pkg-config --libs libusb-1.0 libcrypto++) -ludev -lpthread"`.

## Boot

**Prefetch abort at `0x50` the moment the MMU is enabled.**
`PGTABLE_BASE_PADDR` was 0, which is where the image starts: the page table was
being written over the loaded image. It now lives at `0x7C000`, and the linker
script ends the heap there instead of at `0x80000`.

**The page table reads back all zeros (`xp` in the monitor).**
`CONFIG_ARCH_ROMPGTABLE=y` was inherited from the LPC31 board template, which
makes `arm_head.S` skip page table setup entirely. Turn it off.

**`DFAR=0x8004_0000`, data abort in the first driver.**
`arm_head.S` only maps `_stext.._ebss`. The peripheral window at `0x8000_0000`
has to be mapped by the board: one 1 MB section in `arm_boot()` plus a TLB flush.

**Exception taken at `0xFFFF_0008` / `0xFFFF_000C`, which is unmapped.**
The CPU comes out of reset with `V=1` (high vectors), but NuttX's vector table is
inside the image at `_vector_start` and nothing copies it. `arm_boot()` copies it
to 0 and clears `CR.V`. The linker script keeps `.vectors` with `KEEP()` so it
survives `--gc-sections`.

**`Assertion ... nx_start.c:684` before anything prints.**
`group_setupidlefiles()` failed because there is no `/dev/console`. Two things
were missing: `CONFIG_UART_PL011` (the generic driver, not just the chip one) and
a call to `pl011_serialinit()` from `arm_serialinit()`.

**The device resets back into the previous firmware when NuttX starts.**
This is the menu → NuttX hand-off, see "Menu" below.

## Interrupts and time

**No interrupts at all; `-d int` shows only SVC.**
`up_irqinitialize()` wrote `(1 << 1)` to `ICOLL_CTRL`. That is the **FIQ** enable.
The IRQ final-enable is bit 16 (FIQ 17, `ARM_RSE` 18). Worse, it used a plain
`putreg32()` on the whole register, clearing the reset-value `SFTRST`/`CLKGATE`
bits. Use the `CLR` alias for those, then read-modify-write the enable bits.

**First interrupt after fixing that: Undefined Instruction.**
`arm_decodeirq()` returned `NULL`. `arm_vectorirq`'s return path is `mov sp, r0`
followed by a pop, so `sp` became 0 and the return frame was read out of the
vector table. It must return the context to restore: the `regs` argument when no
switch happened, the new task's `xcp.regs` when one did.

**`clock_systime_ticks()` is always 0, so `sleep()` never returns.**
`TIMROT`'s `ROTCTRL` comes out of reset with `SFTRST|CLKGATE` set, and
`up_timer_initialize()` never cleared them — the counter never moved. Clear them
in the SoC order: assert `SFTRST`, poll `CLKGATE`=1, clear both, poll `CLKGATE`=0.
Also clear the rotary-decoder input source.

**`NR_IRQS = 65` corrupts the vector base.**
Source 64 does not exist; `PRIORITY(64)` computes `0x60 + 16*0x10 = 0x160`, which
is `VBASE`. Enabling or disabling that "interrupt" rewrites the vector table base.
`NR_IRQS` is 64.

**Stale interrupt on the first `cpsie` after the menu.**
See "Menu" below: park the peripherals.

## LCD

**Panel ID reads `da/00/00` instead of `00/80/00`, and the screen is white.**
APBH channel 0 never ran. `CTRL0` resets with `SFTRST|CLKGATE` and channel 0 held
by `CLKGATE_CHANNEL`/`RESET_CHANNEL`/`FREEZE_CHANNEL`, and `CHn_DEBUG2` reads 0 —
so `LCDIF_checkDMAFin()` concluded "done" immediately and every command was
dropped. `LCDIF_APBHPowerUp()` clears the reset/gate bits and releases the three
channel bits, and the init order is reset-then-enable, not enable-then-reset.

**NuttX hangs in the boot path when the panel is initialised early.**
Panel reset to display-on is about 820 ms of busy waiting. It moved to
`board_late_initialize()`, and when the LCD is NSH's console it must be
initialised **synchronously** there — a delayed kernel thread loses NSH's first
output. `CONFIG_STMP3770_LCD_INIT` (default n) still gates the "initialise the
panel during boot" behaviour for experiments.

**NuttX boots and NSH prints half a line, then stops.**
Usually the LCD init is racing the console, or the panel was initialised from
`arm_boot()` (task context not ready). See the previous entry.

**A kernel thread's `printf` goes nowhere.**
Kernel threads have no stdout. Use `up_lowputc()` (polled debug UART) while
bringing something up.

## NAND

**Every GPMI access hangs, no error.**
GPMI and ECC8 both come out of reset with `SFTRST|CLKGATE` set, and ECC8 has an
extra `AHBM_SFTRST`. `GPMI_PowerUp()` clears them; without it the DMA descriptor
chain never starts and the poll loop spins forever. The GPMI pins also have to be
muxed, and the GPMI clock enabled, before the first register write.

**The NAND works in the emulator and not on hardware (or the reverse).**
The GPMI has to run from HF (PLL/2), the way ExistOS runs it. At the boot ROM's
default 24 MHz the timing is wrong enough to return garbage.

**Data abort with `DFAR=0xFFFF_FFFF` in the keyboard thread after adding NAND.**
The page buffer was 32 bytes, so the GPMI DMA wrote past it and over the keyboard
driver's globals. Page buffers must be one page (2048 B) and 32-byte aligned.

**SmartFS `open(O_CREAT)` returns `EIO` after `mksmartfs`.**
`mksmartfs` does more than format: it has to mark the root directory
(`BIOC_LLFORMAT` + `BIOC_ALLOCSECT` + `BIOC_WRITESECT`). Use the NuttX
`mksmartfs` app rather than calling the ioctls by hand.

**MTD `bread` / `bwrite` return values.**
They return a **block count**, not a byte count. SmartFS compares against the
requested count and misbehaves in confusing ways if you return bytes.

**`smart_initialize()` produces `/dev/smart0` only with a NULL name.**
Passing `"smart0"` gives `/dev/smart0smart0`-ish paths. Pass `NULL`.

**`mtd_partition()` takes read/write blocks, not erase blocks.**
Its `firstblock`/`nblocks` are counted in the MTD's `blocksize` units, which here
are pages. The first version passed erase blocks, so the volume landed on block 4
— the stock NCB backup — and the first `mksmartfs` erased it. The board now
partitions in pages, and repairs block 4 from block 0 at boot: the panel prints
`ncb backup RESTORED` when that path runs.

## Keyboard

**Injected characters do not reach NSH: no echo, no command.**
`uart_register()` never set `isconsole`, so `ECHO`/`ICANON`/`ICRNL` stayed off and
`uart_readv` took the raw path. The board's `arm_serialinit()` only called
`pl011_serialinit()`; it must call `pl011_earlyserialinit()` first.

**`read()` on `/dev/lcdcon` blocks forever.**
The write path existed before the read path. The keyboard thread now injects into
a 64-byte ring buffer with a counting semaphore, and `read()` blocks on it. The
device is registered in `board_late_initialize()` before the keyboard can inject,
otherwise the semaphore is used uninitialised.

## Menu

**Selecting NuttX resets back into the menu.**
Two independent causes, both fixed:

1. the image was copied to 0 including the vector block, while interrupts
   were still on. Every 1 ms tick vectoring through `0x0` landed in NuttX's
   not-yet-initialised table. Copy everything except the first 64 bytes, disable
   interrupts, then copy the vectors.
2. the menu left pending interrupt sources behind: TIMER0 (its FreeRTOS tick) and
   LCDIF DMA (from the last full-screen redraw). `park_peripherals()` resets
   TIMROT/LCDIF/APBH/GPMI/ECC8/ICOLL to their power-on state before the jump, so
   NuttX starts from the state its drivers expect. Parking also stops the tick, so
   `vTaskDelay()` stops working — it has to be the last thing the menu does.

**`HP OS` goes black.**
Expected. The stock boot flow needs the mask OCROM at `0xFFFF_0000` and high
vectors; see [boot-chain.md](boot-chain.md#why-hp-os-is-disabled). The entry is
shown as `unavail` on purpose.

**QEMU: `sendkey` seems to be ignored.**
A full-screen redraw takes about 11 s in the emulator and key events that arrive
during a redraw are lost. Hold the key longer and send it between redraws; the
menu also redraws every 25 ticks (`25 * 40 ms`).

**QEMU: `screendump` returns a blank panel.**
`-display none` with no display listener never refreshes the surface, so you get
the first frame forever. Add `-vnc 127.0.0.1:9`.

**QEMU: the guest shell eats its own output when using `-serial file:log`.**
The `file` chardev is opened read-write; guest TX goes into the file and guest RX
reads it back as input. Use `-serial pty` (and `cat` the pty from another
process), or a socket.

**QEMU: `InitRD overlaps in-use memory region` and no initrd.**
The machine only has DRAM if you pass `-m`; the default is none. The kernel image
decompresses to ~21 MB. Use `-m 64`.

## Emulator model bugs worth knowing

These were fixed in EmuGII and are documented here because they explain
"it works on hardware but not in the emulator" (and vice versa):

* the UARTDBG model had a single-deep TX register and reported `FR.TXFF` as
  "shifter busy", so characters were dropped under load. Patched to a 16-deep FIFO
  with PL011 semantics.
* DIGCTL and TIMROT only handled 32-bit accesses; the stock code uses 16-bit and
  8-bit reads/writes (`CHIPID+2`, `TIMCOUNT` at `0x30/0x50/0x70/0x90`). Patched to
  merge sub-word accesses.
* `-kernel` / `-dtb` did not follow the ARM Linux boot ABI (no `r0/r1/r2`), so
  booting a kernel image directly could not work. Patched to go through
  `arm_load_kernel()`.
