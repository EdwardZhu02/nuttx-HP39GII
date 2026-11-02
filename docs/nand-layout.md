# NAND layout

128 MB, 1024 blocks, 64 pages per block, 2048-byte pages plus 64 bytes of spare, of
which the first **19 bytes** are the metadata the drivers read and write. A full dump is
therefore two files: 134 217 728 bytes of data and 1 245 184 bytes of OOB (65 536 pages ×
19 bytes).

Page numbering used everywhere in this repository is `page = block * 64 + index`. Block
240 is page 15 360.

## Stock layout

Derived from ExistOS's `SystemConfig.h`; the parts that matter are the ones this project
writes near.

| Block | Page | Contents |
|---|---|---|
| 0 | 0 | **NCB** — NAND control block, starts with `STMP` |
| 4 | 256 | second copy of the NCB, byte-identical |
| 8 and 12 | 512 / 768 | `LDLB` in page 0, `LDLB2` in page 1 |
| 22 | 1408 | `OSLoader.sb` (stock loader, referenced by the stock LDLB) |
| 23 | 1472 | configuration block |
| 31 | 1984 | `ExistOS.sys` (the stock application image) |
| 160 | 10240 | `DATA_START_BLOCK` — start of the HP file system |
| 4096 | — | FTL data area (8 MB offset) |

Nothing above block 160 is touched by this project, and neither is anything below
block 238.

## Layout with the boot menu installed

| Block | Page | Size | Contents | Written by |
|---|---|---|---|---|
| 0 | 0 | 1 page | NCB (`STMP`) | stock, repaired if damaged |
| 4 | 256 | 1 page | NCB backup | stock, **self-repaired at every boot** |
| 8 | 512 | 2 pages | `LDLB` + `LDLB2` | `install_menu.py --ldlb` |
| 12 | 768 | 2 pages | mirror of block 8 | `install_menu.py --ldlb` |
| 238–239 | | | isolation gap, never written | — |
| 240 | 15360 | 42 pages | **boot menu** sb1 stream (`load 0x30000; jump 0x30000`) | `--images` |
| 241–242 | 15424 | 70 pages | stock boot section: `HPB1` + length + 143 136 bytes | optional, `--images` |
| 243–244 | 15552 | 92 pages | NuttX: `NXB1` + length + `nuttx.bin` | `--images --nx-header` |
| 245–1023 | | 779 blocks | **SmartFS** volume, `/dev/smart0` → `/mnt` | `mksmartfs` from NuttX |

779 blocks is 97.4 MB of the 128 MB. Blocks 238 onwards were all `0xFF` in the stock
image — no factory data, no bad-block table — which is why this area is safe to claim.

## Rules

**Copy the whole thing before you write anything.** `nanddump` reads data and OOB, never
writes. A backup taken before the first install is the only way back to a byte-exact
stock device; the stock images are not distributed anywhere.

**`install_menu.py` writes whole blocks and preserves page 1.** Erasing block 8 without
restoring `LDLB2` bricks the NAND boot path (the device still boots over USB, so it is
recoverable, but you will need a full restore).

**The menu never writes NAND.** It only reads blocks 240/241/243 to load images. A
corrupt menu means you re-enter the boot ROM and reinstall it; it cannot spread.

**NuttX writes only blocks 245..1023.** The volume is created with `mtd_partition()` over
that range, so a SmartFS bug can lose files but cannot reach the stock area.

**Block 4 is repaired, not ignored.** The first version of the partition code passed
erase-block units to `mtd_partition()`, which takes *read/write* blocks (pages here), so
the SmartFS volume was placed on top of block 4 and formatting it erased the NCB backup.
`hp39gii_boot.c` now copies block 0 back over block 4 at boot and verifies it. If you see
`ncb backup RESTORED` on the panel, that path just ran.

**OOB matters.** The stock data uses 19 metadata bytes per page (including `BCB ` bad
block markers). A restore that writes data but not OOB produces a device that boots and
then loses files. `nandrestore.py` always sends both.

## What the panel tells you at boot

NuttX prints its NAND state on the LCD console as it comes up. Expected sequence on a
healthy device:

```
NAND init...
NAND init OK
stock p0 OK
ncb backup OK            (or "ncb backup RESTORED" once)
smart0 ready
mounted /mnt
boot# N
```

`mount FAILED (unformatted?)` means the SmartFS volume was never formatted. Run
`mksmartfs /dev/smart0` in NSH once; the boot counter then appears on the next boot.
