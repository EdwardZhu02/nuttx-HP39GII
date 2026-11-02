# Host tools

Everything here talks to firmware that is running from SRAM, loaded over the boot
ROM. Nothing in this directory can brick the device by itself — the only tool
that writes NAND is `install_menu.py`, and it writes only the blocks the menu
owns.

| Tool | Talks to | Writes | Purpose |
|---|---|---|---|
| `sbloader-hid` | boot ROM, USB HID `066f:3770` | nothing | load an `.sb` BootStream into SRAM |
| `hiddump` | boot ROM | nothing | print the HID report descriptor, for debugging the loader |
| `nanddump.py` | nandump firmware | nothing | read the whole NAND out |
| `split-nand.py` | — | — | convert between interleaved dumps and data/OOB pairs |
| `nandrestore.py` | nandrestore firmware | NAND | write an image back, or verify against it |
| `install_menu.py` | nandrestore firmware | NAND | install menu / NuttX / HP section, repoint LDLB |

## Getting into the boot ROM

Remove the battery, hold **ON/C**, plug in USB. The device enumerates as
`066f:3770`. Release the key once it is there. This is the only recovery path
the hardware has, and it always works: the boot ROM is mask ROM.

## `sbloader-hid` (macOS)

```bash
make sbloader-hid
./sbloader-hid ../dist/nandrestore.sb
```

libusb cannot open this interface on macOS — the HID driver owns it, and
detaching needs root. `sbloader-hid` goes through `IOHIDManager` with
`kIOHIDOptionsTypeSeizeDevice` instead.

The report ID encoding differs between implementations, so the tool first runs a
non-destructive health check: a `CBW(length=48)` plus an all-zero data block.
The stream header is invalid, so the ROM must answer with a non-zero status —
if the tag comes back correctly, both the encoding and the fact that the ROM is
still alive are confirmed. Then the real upload starts.

A final CSW usually never arrives: the ROM jumps into the loaded image as soon
as the last byte lands. `no final CSW -- device likely jumped` is the success
path.

`./sbloader-hid <file> probe` forces the full four-encoding search, useful if a
device revision behaves differently. `hiddump` prints the report descriptor if
you need to work out why.

## `sbloader` (Linux)

The libusb loader from `sbtools` does the same thing:

```bash
../scripts/build-sbtools.sh
../.deps/existos/tools/sbtools/sbloader -d ../dist/nandrestore.sb
```

## The CDC protocol

Both recovery firmwares expose a USB CDC-ACM port and speak a framed protocol
with a CRC32 over every frame. The banner line carries the geometry, so the host
never has to assume it.

```
nandump      -> host : "NANDUMP v1 pagesize=2048 meta=19 pagesperblock=64 blocks=1024"
                      frame: "PND1" | page | dlen | mlen | data | meta | crc32
                      end:   "NANDUMP END"
host -> nandrestore : frame: magic | page | dlen | mlen | data | meta | crc32
                      magic: PNR1 write, PNR2 verify, PNR3 ping, PNR4 erase block,
                             PNR5 read, PNR0 end session
                      reply: "W ok <page>" / "V ok <page>" / "B ok <blk>"
```

The device-side RX FIFO is 512 bytes, so frames are sent in 256-byte chunks with
a pause. `--delay 0.003` is the slowest value known to be reliable; going faster
shows up as intermittent CRC errors (`E crc`), not as corruption.

## `nanddump.py`

```bash
python3 nanddump.py -o nand-full.raw -n 65536     # whole device, ~8 minutes
python3 nanddump.py -o nand-head.raw -n 4096      # first 8 MB: boot chain + loader + system
```

Output is **interleaved**: `pagesize + meta` bytes per page, so page N starts at
`N * 2067`. Split it before feeding it to the other tools:

```bash
python3 split-nand.py --split nand-full.raw --out nand-full
#   -> nand-full.data (134217728 B), nand-full.oob (1245184 B)
```

Do this before you install anything. The stock images are not published; the
dump is the only copy.

## `nandrestore.py`

Defaults to `--verify` (read-only): the backup is sent to the device, the device
compares it against NAND and answers `V ok` or a mismatch. Use it to check a dump
without risking anything.

```bash
python3 nandrestore.py --verify --all                        # read-only full check
python3 nandrestore.py --write --start 0 --count 64          # write the first 64 pages
python3 nandrestore.py --write --all                         # last resort, full restore
```

`--write` erases each block before programming and reads every page back. Blank
(0xFF) pages are skipped so that the ECC area in OOB keeps its erased state.
`--no-skip-blank` overrides that, which you almost never want.

## `install_menu.py`

Writes the menu and its payloads, then repoints the LDLB records. Run the two
steps separately — the image write and the LDLB update are deliberately not one
command:

```bash
python3 install_menu.py --images ../dist/bootmenu.sb - ../dist/nuttx.bin \
        --nx-header --delay 0.003
python3 install_menu.py --ldlb
python3 install_menu.py --show            # fw_start_nand / start_sector / sectors
```

Pass `-` to skip a slot. The second slot is the stock boot section, which is only
used by the disabled "HP OS" entry; installing without it is the normal case. If
you do have it, pass the file and drop the `--nx-header`-compatible slot order:

```bash
python3 install_menu.py --images ../dist/bootmenu.sb hp-boot-section.bin ../dist/nuttx.bin --nx-header
```

`--ldlb` needs the backup (default `nand-dump-new.bin` / `nand-oob-new.bin`,
override with `--backup-data` / `--backup-oob`) because it has to preserve page 1
of blocks 8 and 12. `--restore-ldlb` writes those records back from the same
backup, which is how you return to the stock firmware.

## Ports

`/dev/cu.usbmodem*` on macOS, `/dev/ttyACM*` on Linux; the first match is used
unless `-p` is given. The port only exists while the firmware is running.

## Requirements

Python 3.5 or newer, standard library only. `sbloader-hid` and `hiddump` need
macOS and Xcode command line tools; `make` with no arguments does the right thing
on either platform.
