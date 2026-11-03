# Boot chain

The calculator has no bootloader in the usual sense. The mask-ROM boot ROM either
receives a firmware stream over USB, or reads one from NAND. Everything else — the menu,
NuttX, the recovery firmware — is a payload that the boot ROM puts somewhere and jumps
to.

## Three entry paths

```
                       ┌──────────────────────────┐
  USB 066f:3770  ─────►│  boot ROM (mask ROM)     │
                       │  SDP / HID, sb1 stream   │
                       └────────────┬─────────────┘
                                    │ load @ 0x0, jump 0x0
                                    ▼
                          payload runs from 0x0

                       ┌──────────────────────────┐
  power-on       ─────►│  boot ROM                │
                       │  reads LDLB from NAND    │
                       │  block 8 page 0          │
                       │  (block 12 is a mirror)  │
                       └────────────┬─────────────┘
                                    │ follows fw_start_sector
                                    ▼
                    ┌───────────────┴───────────────┐
                    │                               │
        stock: page 1408 (block 22)      ours: block 240
        OSLoader.sb → 0x0                menu sb → 0x30000
                    │                               │
                    ▼                               ▼
        OSLoader builds the VM,          boot menu, reads NAND,
        opens MMU/caches, jumps to       moves an image to 0x0
        0x0010_0010 (stock System)       and jumps
```

### SDP: the USB path

`elftosb` packs a raw binary into an **sb1 BootStream**: a container of `LOAD`/`FILL`/
`CALL`/`JUMP` commands with a `STMP` signature. The boot ROM executes the stream, so the
stream itself decides where the payload lands and where execution continues.

Two `.bd` scripts are used:

| File | Commands | Used for |
|---|---|---|
| `scripts/bootstream/nuttx.bd` | `load inputFile > 0x0000; jump 0x0000;` | NuttX, recovery firmware |
| ExistOS `Script/build_menu.bd` | `load inputFile > 0x00030000; jump 0x00030000;` | boot menu |

The boot ROM accepts a stream over USB HID at `066f:3770`. On macOS the interface is
claimed by the HID driver, so `libusb` cannot open it; `host-tools/sbloader-hid.c` talks
to `IOHIDManager` with `kIOHIDOptionsTypeSeizeDevice` instead. On Linux the stock
`sbloader` from `sbtools` works.

To get into the boot ROM: remove the battery, hold **ON/C**, plug in USB. The device
enumerates as `066f:3770` and waits. Nothing is written to flash in this mode — the
firmware runs from SRAM.

### LDLB: the NAND path

At power-on the boot ROM looks at **block 8 page 0** and **block 12 page 0** for a page
whose bytes 44..48 are `LDLB`. That page is a *Logical Drive Load Block* record; it
tells the ROM where the real firmware starts:

| Offset | Field | Meaning |
|---|---|---|
| `44` | magic | `LDLB` |
| `48` | `fw_start_nand` | start block |
| `52` | `start_sector` | first sector of the firmware (**2048-byte units**) |
| `56` | `stride` | |
| `60` | `sectors` | length |
| `68` | `fw2_start` | second image, same units |
| `76` | `sectors2` | |

Stock: `start_sector = 1408` (in pages) = block 22, holding `OSLoader.sb`. Ours:
`start_sector = 240 · 64 = 15360`, holding the menu stream.

Both copies (block 8 and block 12) are rewritten, and page 1 of each block must be
preserved — it is `LDLB2`, and the ROM reads it too. `install_menu.py` reads the existing
page 1 and writes it back after the erase. Losing it is one of the ways to end up with a
device that only boots over USB.

Because the LDLB records are the only pointer to the menu, restoring the stock system is
purely a matter of pointing them back — the stock blocks are never erased.

## Handing over to NuttX

The menu does the following, in this order (the order matters, see below):

1. Read the 16-byte header at block 243. It must be `NXB1` + a 32-bit length, written
   by `install_menu.py --nx-header`.
2. Copy everything **except the first 64 bytes** to `0x0`.
3. Draw `jumping to nuttx`.
4. `irq_off()`, copy the 64-byte vector block, then `park_peripherals()`, then jump.

### Why the first 64 bytes are copied last

The menu's own exception vectors live at `0x0` (its `_startup` copies 16 words from
`0x30000`: eight `ldr pc` plus eight literals). GPMI reads are DMA and depend on
interrupts. So while the 186 KB image is being moved into place, the tick interrupt at
1 ms keeps vectoring through `0x0`. As soon as the low part of `0x0` contains NuttX code
and `g_irqvector` is still garbage, the machine takes an exception into an
uninitialised table and resets back into the menu. Copying the vector block last, with
interrupts already off, closes that window.

### Why peripherals are parked

`park_peripherals()` resets **TIMROT, LCDIF, APBH, GPMI, ECC8 and ICOLL** to their
power-on state (`SFTRST` asserted; the hardware raises `CLKGATE` with it).

The menu leaves pending interrupt sources behind: TIMER0 (its own FreeRTOS tick) and
LCDIF DMA (from the last full-screen redraw). NuttX enables interrupts in
`up_irqinitialize()` *after* it has initialised the panel, so the first `cpsie` takes
those stale interrupts and lands in `irq_unexpected_isr` → panic. Resetting the blocks
clears the pending bits and, as a bonus, matches what every NuttX driver expects on
entry: each one starts by clearing `SFTRST`/`CLKGATE` itself.

Note that parking also stops the tick, so `vTaskDelay()` no longer works afterwards.
This must be the last thing the menu does.

## Why "HP OS" is disabled

The menu shows the stock system as `1. HP OS (unavail)`. Selecting it prints
`HP OS: not supported yet`. This is deliberate.

What the stock boot section actually does (disassembled from the word stream the stock
loader `LOAD`s):

* The first `LOAD 5492@0x0` is **Thumb code**, not a vector table; it includes a
  `bl 0x0` helper, so it has to be at 0 when it runs.
* `CALL 0x175` configures RTC, POWER and ICOLL early.
* Then `LOAD 1024@0x0` places the real ARM image header: `EA000006` (`b +0x20`) →
  `ldr sp,[pc]=0x23CE0` → `ldr pc,=0xE89FC`.
* `0xE89FC` switches to Thumb and enters the C runtime at `0xE8AD8`; `0xE88xx` sets up
  the mode stacks (FIQ `0x7FC`, IRQ `0x368`, ABT `0x6FC`, UND `0x3C8`).

On hardware, skipping the `CALL` leaves the screen showing the menu; executing the
`CALL` turns the screen black (the stock code touches power and display) and then the
device hangs. Restoring the ROM clock snapshot and the power registers did not help.

Under EmuGII the stock code gets further and then dies on its own MMU setup: it enables
the MMU, accesses `0xFFFF_0030` (the mask-ROM / high-vector area), takes
`DFSR=0x7` (translation fault), and loops in a prefetch abort on `0xFFFF_0010`. It
expects a 64 KB **mask OCROM at `0xFFFF_0000`** and high vectors. EmuGII has no OCROM
model, so the stock flow cannot be reproduced faithfully in the emulator either.

Two options remain, both out of scope for now:

1. Instead of running the stock image, rewrite the LDLB records back to the stock values
   and trigger a full reset through the RTC watchdog, letting the boot ROM start the
   stock firmware itself. The cost: the menu does not come back, and returning to it
   requires another SDP session.
2. Dump the 64 KB mask ROM from a real device (a `nandrestore`-style firmware can do it
   in a recovery session), add an OCROM model to EmuGII, and continue debugging there.

The experimental code path is kept behind `BOOTMENU_HP=1` in the menu source
(`OSLoader/bootmenu.c`).

## Register state at the menu → payload boundary

Useful if you ever have to debug the handover. Measured on hardware, printed by the
menu's diagnostic line, with the stock boot ROM having just handed control over:

| Register | Value | Meaning |
|---|---|---|
| CP15 `c1` | `0x0005_007A` | MMU off, caches off, `V=0` (low vectors), `A=1` |
| `CLKSEQ` | `0x0000_00FB` | |
| `CLKCTRL_CPU` | `0x0001_0001` | crystal bypass, divider 1 |
| `VDDDCTRL` | `0x0031_0710` | `TRG=0x10` ≈ 1.2 V |
| `VDDIOCTRL` | `0x0000_170C` | `TRG=0x0C` ≈ 3.1 V |

### Does the menu have to set up the MMU before the handover?

No. `OSLoader/bootmenu.c` contains no CP15 writes at all -- the menu firmware does not
run the VM (see the note in `OSLoader/VmMgr/vmMgr.c`: the cache/ZRAM arrays are there to
satisfy the linker, not to be used). The `c1` value above is what the stock boot ROM
left behind, and it has the MMU off.

That is the state NuttX expects: `arm_nommuhead.S` disables the MMU and the D-cache and
clears `V` in its first instructions regardless of what it inherits, so the handover is
safe whether the menu ever turns the MMU on or not.

It also means the MMU can be turned back on *inside* NuttX without any coherency debt:
the CPU arrives with both caches off, so there are no dirty lines to clean and no stale
TLB entries to invalidate. Patch 0024 does exactly that at the end of `arm_boot()`, once
the vectors are copied to 0 and the page table is in place.

The menu inherits the ROM's conservative 1.2 V setting and does not raise it. NuttX does:
`stmp3770_powerconfig()` sets `VDDD` 1.45 V, `VDDA` 1.8 V, `VDDIO` 3.3 V, and
`stmp3770_clockconfig()` reconfigures the PLL, CPU divider 5 and `CPUFRAC` 22 — the same
sequence ExistOS uses to run the core at 392 MHz. Without the power part the LCD panel
stays unpowered on hardware and the screen stays dark, which looks exactly like a hang.
