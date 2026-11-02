# Flashing

Read [nand-layout.md](nand-layout.md) first. This page is the procedure; that page is
the map.

## Before anything

You need:

* the calculator and a USB cable that carries data,
* the images from `dist/` (see [building.md](building.md)),
* `dist/nandrestore.sb`, `dist/nanddump-full.sb`, `dist/bootmenu.sb`, `dist/nuttx.sb`.

Two facts make this safe to attempt:

* the mask-ROM boot ROM is always reachable over USB, no matter what is in NAND;
* this procedure never erases blocks 0..239 except page 0/1 of blocks 8 and 12.

Two facts make it unsafe to attempt carelessly:

* the stock images are not distributed anywhere, so a backup is your only copy;
* `ERASEALL` exists and will remove the boot records. Do not use it.

## Step 0 — back up the stock NAND

Do this before the first install, on an unmodified device.

```bash
# pull the battery, hold ON/C, plug in USB -> 066f:3770
./host-tools/sbloader-hid dist/nanddump-full.sb        # macOS
# .deps/existos/tools/sbtools/sbloader -d dist/nanddump-full.sb   # Linux

python3 host-tools/nanddump.py -o nand-full.raw -n 65536
python3 host-tools/split-nand.py --split nand-full.raw --out nand-full
```

That takes about eight minutes for 128 MB and produces:

```
nand-full.raw    135 462 912 B   interleaved, data+OOB per page
nand-full.data   134 217 728 B
nand-full.oob      1 245 184 B
```

Check the dump before you trust it: `nanddump.py` reports CRC errors and missing
pages, and exits non-zero if it saw any. A dump with `crc errors: 0` and no
`MISSING` line is good. Keep all three files somewhere that is not this working
directory.

The device stays in the dump firmware afterwards. Power-cycle it to get back to
the stock system.

## Step 1 — install the menu and NuttX

```bash
# pull the battery, hold ON/C, plug in USB again
./host-tools/sbloader-hid dist/nandrestore.sb          # macOS
# .deps/existos/tools/sbtools/sbloader -d dist/nandrestore.sb   # Linux
```

`nandrestore` writes nothing until it is told to. Then:

```bash
python3 host-tools/install_menu.py \
        --images dist/bootmenu.sb - dist/nuttx.bin \
        --nx-header --delay 0.003
```

What that does, in order: erase block 240 and write the 42-page menu stream,
skip the HP slot (the `-`), erase blocks 243/244 and write `NXB1` + `nuttx.bin`,
then read every page back and compare. Expected output ends with

```
menu: 42 pages -> blocks 240..240
  menu written + verified
nuttx image: 92 pages -> blocks 243..244
  nuttx image written + verified
```

If you get `E crc` instead, the CDC pacing was too fast: raise `--delay` to
`0.005` and retry. Nothing is left half-written — the verify step fails before
the block is trusted.

## Step 2 — point the boot ROM at the menu

Deliberately a separate command, because this is the step that changes what the
device boots:

```bash
python3 host-tools/install_menu.py --ldlb
python3 host-tools/install_menu.py --show
```

`--show` must print `start_sector=15360` and `sectors=42` for both block 8 and
block 12. `15360 = 240 * 64`. Then power-cycle.

`--ldlb` needs the stock backup to preserve page 1 of each block; it defaults to
`nand-dump-new.bin` / `nand-oob-new.bin`. Point it at yours:

```bash
python3 host-tools/install_menu.py --ldlb \
        --backup-data nand-full.data --backup-oob nand-full.oob
```

## Step 3 — first boot

The panel shows, after about two seconds:

```
HP 39gII  BOOT MENU
> 1. HP OS (unavail)
  2. NuttX
nand 1024 blk x 64 pg
UP/DN select
ENTER boot
```

Entry 1 is selected by default. **Press DOWN, then ENTER.** The screen shows
`loading nuttx...`, then `jumping to nuttx`, then NuttX comes up and prints:

```
NuttShell (NSH)
nsh>
NAND init...
NAND init OK
stock p0 OK
ncb backup OK
smart0 ready
mounted /mnt
boot# 1
```

`boot#` increments on every boot — that is a real NAND write, read back and
verified, not a cached value.

On the very first boot the SmartFS volume does not exist yet:

```
mount FAILED (unformatted?)
errno 19
run: mksmartfs /dev/smart0
```

Do exactly that at the `nsh>` prompt, once. The next boot mounts it.

Keyboard: `F1` is space, `F2` is minus, `ALPHA` and `SHIFT` are one-shot, `SHIFT`
+ `ALPHA` gives upper case. There is no space bar.

## Undo

Three levels, cheapest first.

**Back to the stock system, keep the menu installed.** Point the LDLB records at
the stock loader again:

```bash
# boot ROM -> nandrestore.sb, then
python3 host-tools/install_menu.py --restore-ldlb \
        --backup-data nand-full.data --backup-oob nand-full.oob
```

The stock blocks were never touched, so this is a complete undo of step 2. The
menu stays in block 240, inert.

**Reinstall the menu** (it got corrupted, or you built a new one): repeat step 1
with `--only menu`. No need to touch NuttX.

**Full NAND restore** (something wrote where it should not have): load
`nandrestore.sb` and write the backup back.

```bash
python3 host-tools/nandrestore.py --verify --all        # read-only, do this first
python3 host-tools/nandrestore.py --write  --all        # ~40 minutes
```

Verify reports `V ok` per page; a full write erases and reprograms every block
and reads it back. This restores the device byte for byte, including the OOB
metadata that carries the bad-block markers.

## Recovery ladder

| Symptom | Cause | Do this |
|---|---|---|
| black screen, no USB | nothing is running | pull the battery, hold ON/C, plug in USB — the boot ROM is mask ROM and always comes up |
| menu appears, `NuttX: image missing / bad magic` | block 243 was not written, or was written without `--nx-header` | repeat step 1 |
| menu appears, NuttX hangs on `jumping to nuttx` | a stale interrupt or a corrupt image | re-flash NuttX; if it persists, the image is the problem, not the menu |
| boots straight to the stock system | `LDLB` still points at page 1408 | step 2 |
| boot loop between ROM and nothing | `LDLB` points at a block that no longer holds a valid stream | step 1, then step 2 |
| `ncb backup RESTORED` every boot | block 4 keeps getting erased | something is formatting block 4; check the NAND partition constants |
| `mount FAILED (unformatted?)` | no SmartFS volume yet | `mksmartfs /dev/smart0` once |
| `no banner` from the host tools | firmware not running, or the port was taken by something else | power-cycle, re-enter SDP, reload the firmware |
| `E crc` while writing | CDC pacing too fast | `--delay 0.005` |

## Things that are easy to get wrong

**`--images` and `--ldlb` in one command.** They are mutually exclusive by design;
the `--images` branch is skipped when `--ldlb` is present. Two commands.

**Writing NuttX without `--nx-header`.** The menu reads a 16-byte `NXB1` header to
find the length. Without it the menu reports `bad magic` and refuses to boot. (The
header-less form is only correct if you want the boot ROM to load that block
directly, which this layout does not do.)

**Battery in while flashing.** Works, but the device can power down mid-write. Use
USB power with the battery out, which is also how you get into the boot ROM.

**Assuming the menu is the first thing at 0.** It is linked for `0x30000` and the
boot ROM is told to load it there. A `nuttx.bd`-style stream (load at 0) will
crash immediately.

**Pulling the cable during a write.** `nandrestore` fails the verify and stops;
the block it was writing is half-programmed. Re-run the same command — it erases
the block again first.
