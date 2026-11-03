# Hardware

There is no public datasheet for the STMP3770. Everything below was either read out of
the ExistOS sources (which are themselves a partial reverse-engineering of the stock
firmware), or measured on a real calculator with a read-only probe firmware that is
loaded into SRAM over USB and never touches flash.

Two markers are used throughout:

* **[M]** — measured on hardware. These are the numbers to trust.
* **[S]** — read from sources (ExistOS, EmuGII, mainline Linux). Correct as far as we
  know, but not independently confirmed on the device.

## The SoC

| | |
|---|---|
| Chip | SigmaTel / STEC **STMP3770** (the STMP37xx family; not an i.MX23) |
| `CHIPID` @ `0x8001_C310` | `0x37B0_0002` → product `0x37B0`, revision `2` **[M]** |
| Core | ARM926EJ-S, ARMv5TEJ, MMU, Thumb-1, **no VFP**, soft float **[S]** |
| Compiler flags used by the stock firmware | `-mcpu=arm926ej-s -mtune=arm926ej-s -mfloat-abi=soft -marm` |
| USB | device only; boot ROM exposes SDP as HID `066f:3770` **[M]** |

The chip ID register is at `base + 0x310`, not at `base`. Reading `0x8001_C000` returns
something else entirely.

## There is no DRAM

This is the single most important hardware fact, and it rules out Linux on the device
(it is the reason this project is a NuttX port and not a kernel port).

| Probe | Result | Conclusion |
|---|---|---|
| `HW_EMI_STAT` @ `0x8002_0010` | `0x0000_0000` | `DRAM_PRESENT`(bit31)=0, `LARGE_DRAM_ENABLED`(29)=0, `DRAM_HALTED`(1)=0 **[M]** |
| `HW_EMI_CTRL` @ `0x8002_0000` | `0x0000_0000` | the EMI was never configured **[M]** |
| read `0x4000_0000` | synchronous external abort, `DFSR=0x8`, `DFAR=0x4000_0000` | the address is not mapped at all **[M]** |
| read `0x0080_0000` | `0xE59FF018` — identical to `0x0000_0000` | aliases to physical 0 **[M]** |
| read `0x0010_0000` | `0xE59FF018` | also aliases to physical 0 **[M]** |

`0xE59FF018` is `LDR PC,[PC,#0x18]`, the first word of the probe firmware's own vector
table. Both addresses therefore decode to physical 0, which means **the address bus only
decodes 19 bits (512 KB)**. Everything above 512 KB wraps.

Consequences that show up all over the code:

* The only RAM is the 512 KB of on-chip SRAM at `0x0000_0000`.
* `0x0010_0000` cannot be used as a second RAM window; it is the same memory.
* The MMU page table cannot live at 0 (it would overlap the image), so the board puts it
  at `0x7C000` and removes the top 16 KB of SRAM from the heap.
* `0xE8000` "works" only because it aliases to `0x68000`.

## Memory map

Runtime map with the boot menu installed **[M]**:

| Range | Contents |
|---|---|
| `0x00000`–`0x2F80D` | stock loader `LOAD` targets (only used by the disabled HP OS path) |
| `0x00000`–… | NuttX: `.vectors` must be the first thing at 0, then text/data/bss |
| `0x50000`–`~0x69000` | the boot menu itself (linked at `0x50000`, see `Script/ldr_ld_50000.script`) |
| `0x67F00` and below | safe stack for anything that runs the stock boot stream |
| `0x68000`–`0x78300` | second half of the stock boot stream (reached as `0xE8000`) |
| `0x7C000`–`0x80000` | NuttX first-level page table (`PGTABLE_SIZE`, 16 KB) |

The 512 KB ceiling is hard: NuttX plus its heap, the SmartFS cache and the LCD text
buffer all live inside it.

## Peripherals

Base addresses follow `chip_io_map.h` from ExistOS, all inside the `0x8000_0000` block:

| Block | Base | Notes |
|---|---|---|
| ICOLL | `0x8000_0000` | interrupt collector, 64 sources |
| APBH | `0x8000_4000` | DMA engine shared by LCDIF and GPMI |
| ECC8 | `0x8000_A000` | BCH8 ECC |
| GPMI | `0x8000_C000` | NAND controller |
| PINCTRL | `0x8001_8000` | pin mux |
| DIGCTL | `0x8001_C000` | chip ID (at `+0x310`), microsecond counter (at `+0xC0`) |
| EMI | `0x8002_0000` | memory interface (unused, see above) |
| LCDIF | `0x8003_0000` | panel interface |
| CLKCTRL | `0x8004_0000` | PLLs, clock gates, resets |
| RTC | `0x8005_C000` | |
| TIMROT | `0x8006_8000` | four 32-bit timers |
| UARTAPP | `0x8006_C000` | AUART register face |
| UARTDBG | `0x8007_0000` | PL011 register face |
| DRI | `0x8007_4000` | DRAM controller; `STAT` bit31 reads 1 (controller present, no memory behind it) **[M]** |
| USB | `0x8008_0000` | device controller |

### ICOLL — interrupts

The STMP37xx interrupt collector does **not** look like the i.MX23 one, and this is the
first thing that has to be right before anything else works.

| Item | Value | Evidence |
|---|---|---|
| `STAT` | `0x8000_0030`, idle value `0x3F` (`VECTOR_NUMBER` in bits[7:0]) | **[M]** |
| `STAT` at `+0x70` | reads 0, not implemented | **[M]** |
| Sources | 0..63, `NR_IRQS = 64`. Source 64 does not exist and aliases `VBASE` | **[S]** + trashed vector base observed **[M]** |
| `PRIORITYn` | `0x60 + 0x10*(n/4)`, one byte per source: `ENABLE`=bit2, `PRIORITY`=bits[1:0], `SOFTIRQ`=bit3 | **[S]**, consistent with EmuGII |
| `CTRL` | IRQ final enable = **bit16**, FIQ = bit17, `ARM_RSE` = bit18 | **[S]** |

Two traps, both of which cost a full debug cycle:

* Writing `(1 << 1)` to `CTRL` enables **FIQ**, not IRQ. You then get no interrupts at
  all and no error message.
* `CTRL` has `SFTRST`/`CLKGATE` bits set out of reset. A plain `putreg32()` in
  `up_irqinitialize()` destroys them, and the block stops working. Use the `CLR` alias
  first, then read-modify-write the enable bits.

Source numbers used by the port: `0` = debug UART (so `CONFIG_UART0_IRQ=0` is correct,
which looks wrong at first glance), `28` = TIMER0, `45` = LCDIF DMA, plus the GPMI /
GPMI-DMA / ECC8 group.

### TIMROT — the tick

| Item | Value | Evidence |
|---|---|---|
| `VERSION` @ `0x8006_80A0` | `0x0101_0000` (major 1, minor 1, step 0) → the mainline `mxs_timer` v1 path applies | **[M]** |
| Channel stride | `0x20` (not the i.MX23's `0x40`); `TIMCTRL3` at `0x80` | **[M]** |
| Reset state | `ROTCTRL` comes up with `SFTRST|CLKGATE` set | **[S]** |

The counter does not move until `SFTRST`/`CLKGATE` are cleared in the documented order:
set `SFTRST`, poll `CLKGATE`=1, clear both, poll `CLKGATE`=0. Skipping this gives a
`clock_systime_ticks()` that is permanently 0, so every `sleep()`, `usleep()` and timeout
in the system hangs instead of failing.

### UARTs

**Debug UART** (`0x8007_0000`) is a PL011 as far as the register file is concerned:

| Register | Read value | Meaning |
|---|---|---|
| `CR` @ `0x30` | `0x0000_0101` | `UARTEN|TXE` |
| `FR` @ `0x18` | `0x0000_0090` | `TXFE|RXFE` |
| `PeriphID0` @ `0xFE0` | `0x0000_0011` | PL011 |

But it is **not** an AMBA PrimeCell on a real bus, so `amba-pl011` (and NuttX's
`uart_pl011.c`) needs two clocks named `uartclk` and `apb_pclk`, and the driver must not
rely on the ID block. The port drives it through NuttX's generic `uart_pl011.c`, and the
board calls `pl011_earlyserialinit()` **before** `pl011_serialinit()` — otherwise
`uart_register()` never marks the device as a console, `ECHO`/`ICANON` stay off, and the
keyboard injection path silently does nothing.

Whether the real UARTDBG has a 16-deep TX FIFO is still unconfirmed; EmuGII's model did
not, and was patched to match PL011 semantics for development.

**Application UART** (`0x8006_C000`) is an AUART (`VERSION = 0x0200_0000`, `STAT` at
`0x70`, `DATA` at `0x60`), register-compatible with `fsl,imx23-auart`. It is not used by
the port yet.

### LCD

| Item | Value |
|---|---|
| Panel | 256 × **127** monochrome, 1 byte per pixel, no framebuffer, no vsync |
| Path | LCDIF → APBH DMA descriptors → 8-bit parallel (8080/DBI) panel |
| Status bar | the top 8 rows (`SCREEN_START_Y = 8`) are outside the text area |
| Polarity | `DISPLAY_INVERSE = 1` |

Two things that are easy to get wrong:

* `SCREEN_END_Y = 136` is a *coordinate*, not a height. The panel is 127 pixels tall.
* APBH `CTRL0` comes out of reset with `SFTRST|CLKGATE` set **and** channel 0 held by
  `CLKGATE_CHANNEL`/`RESET_CHANNEL`/`FREEZE_CHANNEL`. Until all of that is cleared, every
  descriptor "completes" instantly (`CHn_DEBUG2` reads 0, so the poll sees "done") and
  every command is silently dropped. The symptom is a panel ID of `da/00/00` instead of
  `00/80/00` and a white screen.

The NuttX driver runs the panel as a **32×16 text console** (`/dev/lcdcon`): 8×8 glyphs,
CR/LF/backspace, scrollback and auto-scroll.

### Keyboard

| Item | Value |
|---|---|
| Matrix | **11 rows × 5 columns**; row 10 has only `ON/C` |
| Key code | `(row << 3) | col` |
| Modifiers | `ALPHA` and `SHIFT` are one-shot (apply to the next key only); `SHIFT+ALPHA` = upper case |
| F1 | space (there is no space bar) |
| F2 | minus |
| Scanning | GPIO matrix through `PINCTRL` mux, ~20 ms poll, no hardware debounce |

### NAND

| Item | Value |
|---|---|
| Geometry | 2048 B page, 64 B spare, 64 pages/block, 1024 blocks = **128 MB** |
| ECC | **BCH8** (`OPT_MCU_STMP3770`), not the i.MX28's BCH4 |
| Controller | GPMI + ECC8, APBH channel 4, descriptor chains |
| Clocking | GPMI runs from HF (PLL/2), same as ExistOS |
| Vendor/ID read | `0x90` command path |

GPMI and ECC8 both come out of reset gated (`SFTRST|CLKGATE`), and ECC8 has an extra
`AHBM_SFTRST`. As with the LCD, nothing errors — the DMA just never completes.

Page 0 and page 4·64 both hold a copy of the stock **NCB** (NAND control block, starts
with `STMP`). Block 4 is repaired from block 0 at boot if it has been erased.

## What is still unmeasured

Tracked here so nobody re-derives them from a datasheet that does not exist:

* Real UARTDBG FIFO depth and whether the ID block is populated.
* AUART baud divisor behaviour (the mainline `imx.c` path assumes i.MX23 clocks).
* Panel command sequence and pixel clock (24 MHz nominal) — the port reuses ExistOS's
  sequence, which works, but the timing margins are unknown.
* Actual row/column GPIO assignment for all 11 keyboard rows (the port matches the
  EmuGII/ExistOS tables and works, but they were never traced with a probe).
* RTC and power-domain behaviour across a battery removal.
