# nuttx-HP39GII

NuttX on the **HP 39gII** graphing calculator (SigmaTel/STEC **STMP3770**, ARM926EJ-S),
plus the out-of-tree pieces needed to build it, install it and get the calculator back
to a factory state if something goes wrong.

The calculator has 512 KB of on-chip SRAM, **no external DRAM**, 128 MB of NAND and no
exposed serial port. Its console is the 256×127 monochrome panel, its keyboard is an
11×5 matrix, and firmware is loaded over the STMP37xx USB boot ROM. Everything in this
repository exists because of those four constraints.

Three things are built here:

| Component | What it is | Where it runs |
|---|---|---|
| **NuttX port** | `arch/arm/src/stmp3770` + `boards/arm/stmp3770/hp39gii` — 22 patch commits on top of Apache NuttX | calculator (after the menu hands over) |
| **Boot menu** | an OSLoader variant of [ExistOS](https://github.com/ExistOS-Team/ExistOS-For-HP39GII) that loads NuttX from NAND and offers the stock system as a (disabled) second entry | calculator, loaded by the boot ROM |
| **Recovery firmware** | `nandrestore` / `nanddump` / `probe`: RAM-only firmware that talks to a host over USB CDC | calculator, loaded by the boot ROM over USB |

Plus the host tools (`install_menu.py`, `nanddump.py`, `nandrestore.py`,
`split-nand.py`, `sbloader-hid`) and scripts that produce every image from a clean
checkout.

## Status

Verified on real hardware unless marked otherwise.

| Feature | State |
|---|---|
| Boot from NAND through the menu into NSH | ✅ works |
| LCD text console as NSH stdin/stdout/stderr | ✅ works |
| Keyboard (11×5 matrix, ALPHA/SHIFT, F1=space, F2=minus) | ✅ works |
| Interrupts (ICOLL), 1 ms tick (TIMROT), sleep/timeouts | ✅ works |
| GPMI + ECC8 NAND driver, read/write/erase | ✅ works |
| NAND → MTD → SmartFS (`/dev/smart0` mounted on `/mnt`) | ✅ works |
| Stock NCB (block 4) self-repair | ✅ works |
| Debug UART (PL011 register face, `0x8007_0000`) | ✅ works (no external pins) |
| Boot menu UI, NuttX entry | ✅ works |
| Boot menu, "HP OS" entry | ❌ disabled — the stock boot flow needs the mask ROM and high vectors; executing it from the menu ends in a black screen (see [docs/boot-chain.md](docs/boot-chain.md#why-hp-os-is-disabled)) |
| USB device (CDC-ACM) under NuttX | ❌ not implemented |
| LCD framebuffer / graphics beyond the 32×16 text console | ❌ not implemented |
| Linux on this device | ❌ impossible — 512 KB of RAM, see [docs/hardware.md](docs/hardware.md#there-is-no-dram) |

NuttX uses about 186 KB of the 512 KB SRAM (36 %), so roughly 320 KB is left for the
heap, the SmartFS cache and applications.

## Quick start

Prerequisites: Linux or macOS, `git`, `cmake`, `make`, `python3`, an `arm-none-eabi-`
toolchain (10.3 or newer) and the host libraries `libcrypto++`, `libusb-1.0` and
`libudev` for `elftosb`. CI uses Ubuntu 22.04 with GCC 10.3-2021.10.

```bash
git clone https://github.com/EdwardZhu02/nuttx-HP39GII
cd nuttx-HP39GII

./scripts/fetch-deps.sh      # clone pinned nuttx / nuttx-apps / ExistOS, apply patches
./scripts/build-all.sh       # everything below, into dist/
```

`build-all.sh` runs, in order:

```bash
./scripts/build-sbtools.sh                 # elftosb, from the ExistOS tree
./scripts/build-nuttx.sh                   # dist/nuttx.bin, dist/nuttx.sb
./scripts/build-firmware.sh bootmenu       # dist/bootmenu.sb
./scripts/build-firmware.sh nandrestore    # dist/nandrestore.sb
./scripts/build-firmware.sh nanddump       # dist/nanddump-8m.sb
./scripts/build-firmware.sh nanddump-full  # dist/nanddump-full.sb
./scripts/build-firmware.sh probe          # dist/probe.sb
```

## Flashing, in one screen

Full detail, including the recovery ladder, is in [docs/flashing.md](docs/flashing.md).

```bash
# 1. calculator into the boot ROM: pull the battery, hold ON/C, plug in USB
#    (USB ID 066f:3770)
./host-tools/sbloader-hid dist/nanddump-full.sb      # macOS
#   Linux: .deps/existos/tools/sbtools/sbloader -d dist/nanddump-full.sb

# 2. copy the stock NAND out, before anything writes to it (~8 minutes)
python3 host-tools/nanddump.py -o nand-full.raw -n 65536
python3 host-tools/split-nand.py --split nand-full.raw --out nand-full

# 3. power-cycle, back into the boot ROM, load the install firmware
./host-tools/sbloader-hid dist/nandrestore.sb

# 4. write the menu (block 240) and NuttX (block 243), then point the boot ROM
#    at the menu -- two separate commands, on purpose
python3 host-tools/install_menu.py --images dist/bootmenu.sb - dist/nuttx.bin \
        --nx-header --delay 0.003
python3 host-tools/install_menu.py --ldlb --backup-data nand-full.data \
        --backup-oob nand-full.oob
```

Power-cycle: the menu appears, press **DOWN** then **ENTER** (entry 1, "HP OS", is
selected by default and is not available).

To go back to the stock firmware, restore the LDLB records:

```bash
python3 host-tools/install_menu.py --restore-ldlb --backup-data nand-full.data \
        --backup-oob nand-full.oob
```

## Repository layout

```
patches/nuttx/     22 patches: the SoC port + board support, against a pinned NuttX
patches/apps/       1 patch:   the nshlib "clear" command
patches/existos/    2 patches: the NAND dump/restore firmware and the boot menu
scripts/            fetch, configure, build, package (see docs/building.md)
host-tools/         host side of the USB protocols, and the macOS SDP loader
docs/               hardware, boot chain, NAND layout, building, flashing, pitfalls
```

Nothing in this repository is a copy of an upstream tree. `fetch-deps.sh` clones
Apache NuttX, NuttX apps and ExistOS at pinned commit IDs into `.deps/` and applies
the patch series, so there are no nested repositories and no vendored sources.

## Three things that will bite you

1. **The boot ROM's SDP loader is the only way in.** There is no serial port and no
   JTAG. If you erase the boot area, the device still boots the boot ROM, so recovery
   is always possible — but only over USB, and only before the NAND is touched.
2. **Never issue `ERASEALL`.** The stock flash contents are not published anywhere;
   a full backup (`nanddump`) taken *before* installing anything is your only copy.
3. **The address bus decodes 19 bits.** `0x0080_0000` and `0x0010_0000` alias to
   physical `0`. Any pointer arithmetic that assumes otherwise silently corrupts the
   image at `0`. This is why NuttX's page table sits at `0x7C000`.

## Credits

* [Apache NuttX](https://nuttx.apache.org/) — Apache-2.0.
* [ExistOS-For-HP39GII](https://github.com/ExistOS-Team/ExistOS-For-HP39GII) — GPL-3.0.
  The OSLoader in it is the base for the boot menu and for the recovery firmware;
  its LCD, keyboard, GPMI and clock code was the reference for the NuttX drivers.
* [sbtools](https://github.com/ExistOS-Team/ExistOS-For-HP39GII/tree/main/tools/sbtools)
  (`elftosb`, `sbloader`) — GPL-2.0+, from Rockbox.
* Register layouts were reconstructed from the above sources and then checked
  against hardware; see [docs/hardware.md](docs/hardware.md) for which numbers were
  measured and which were inferred.

Licensing: `scripts/` and `host-tools/` are MIT (see [LICENSE](LICENSE));
`patches/nuttx/` and `patches/apps/` are Apache-2.0 and `patches/existos/` is GPL-3.0,
matching the trees they apply to.
