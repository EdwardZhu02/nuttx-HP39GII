#!/usr/bin/env python3
"""
Install the dual-boot menu and its payloads into NAND.

Talks over USB CDC to the nandrestore firmware running from SRAM, and writes
only the blocks the menu owns:

  block 240        menu firmware, an sb1 stream the boot ROM loads at 0x30000
  block 241..242   stock boot section ("HPB1" + length + the decrypted stream)
  block 243..244   NuttX image ("NXB1" + length + nuttx.bin)
  block 8/12 page0 LDLB: start_sector -> 240*64, sectors -> menu page count

Nothing in blocks 0..239 is touched, except page 0 of blocks 8 and 12 (and page 1
of those blocks, which has to be written back after the erase).

Usage:
  install_menu.py --images bootmenu.sb - dist/nuttx.bin --nx-header
  install_menu.py --ldlb                 # repoint LDLB (run after --images)
  install_menu.py --show                 # dump both LDLB pages
  install_menu.py --restore-ldlb         # restore LDLB from the backup

Pass "-" in place of an image to skip that slot; the stock boot section is
optional and only used by the disabled "HP OS" menu entry.
"""
import argparse
import glob
import os
import select
import struct
import sys
import termios
import time
import zlib

PAGE = 2048
PPB = 64
MS = 19

MENU_BLOCK = 240
HP_BLOCK = 241
NX_BLOCK = 243
LDLB_BLOCKS = (8, 12)

MAGIC_W, MAGIC_B, MAGIC_P, MAGIC_R, MAGIC_E = b"PNR1", b"PNR4", b"PNR3", b"PNR5", b"PNR0"


class Port:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        iflag, oflag, cflag, lflag = a[0], a[1], a[2], a[3]
        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK |
                   termios.ISTRIP | termios.INLCR | termios.IGNCR |
                   termios.ICRNL | termios.IXON)
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON |
                   termios.ISIG | termios.IEXTEN)
        cflag &= ~(termios.CSIZE | termios.PARENB)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
        a[0], a[1], a[2], a[3] = iflag, oflag, cflag, lflag
        a[4] = a[5] = termios.B115200
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 1
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def read(self, n, timeout=0.5):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(self.fd, n)
        except BlockingIOError:
            return b""

    def write(self, data):
        off = 0
        while off < len(data):
            try:
                off += os.write(self.fd, data[off:])
            except BlockingIOError:
                time.sleep(0.002)

    def write_paced(self, data, chunk=256, delay=0.0015):
        for i in range(0, len(data), chunk):
            self.write(data[i:i + chunk])
            time.sleep(delay)

    def read_exact(self, n, timeout=10.0):
        buf = b""
        end = time.time() + timeout
        while len(buf) < n and time.time() < end:
            c = self.read(n - len(buf), 0.3)
            if c:
                buf += c
        return buf

    def readline(self, timeout=8.0):
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            c = self.read(1, 0.2)
            if not c:
                continue
            buf += c
            if c == b"\n":
                return buf.decode("ascii", "replace").strip()
        return None

    def close(self):
        os.close(self.fd)


def frame(magic, page, data=b"", meta=b""):
    hdr = struct.pack("<III", page, len(data), len(meta))
    crc = zlib.crc32(hdr + data + meta) & 0xFFFFFFFF
    return magic + hdr + data + meta + struct.pack("<I", crc)


def find_port(explicit=None):
    if explicit:
        return explicit
    # macOS exposes the CDC interface as /dev/cu.usbmodem*, Linux as
    # /dev/ttyACM*. Same protocol either way.
    c = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    return c[0] if c else None


def connect(path):
    p = Port(path)
    banner = p.readline(10.0)
    if not banner or not banner.startswith("NANDRESTORE"):
        p.close()
        raise SystemExit("no NANDRESTORE banner (got %r)" % banner)
    print(banner)
    for _ in range(8):
        p.write_paced(frame(MAGIC_P, 0))
        if p.readline(3.0) == "P ok":
            return p
    p.close()
    raise SystemExit("ping sync failed")


def read_page(p, page):
    for _ in range(3):
        p.write_paced(frame(MAGIC_R, page))
        hdr = p.read_exact(16)
        if len(hdr) == 16 and hdr[:4] == b"PND1":
            _, dlen, mlen = struct.unpack("<III", hdr[4:16])
            body = p.read_exact(dlen + mlen + 4)
            if len(body) == dlen + mlen + 4:
                return body[:dlen], body[dlen:dlen + mlen]
    raise SystemExit("read page %d failed" % page)


def erase_block(p, blk):
    for _ in range(3):
        p.write_paced(frame(MAGIC_B, blk))
        r = p.readline(10.0)
        if r and r.startswith("B ok"):
            return
    raise SystemExit("erase block %d failed: %r" % (blk, r))


def write_page(p, page, data, meta, delay=0.0015):
    assert len(data) == PAGE and len(meta) == MS
    for _ in range(4):
        p.write_paced(frame(MAGIC_W, page, data, meta), delay=delay)
        r = p.readline(10.0)
        if r and (r.startswith("W ok") or r.startswith("W meta")):
            return
    raise SystemExit("write page %d failed: %r" % (page, r))


def payload_pages(prefix, blob):
    """16-byte header + payload, padded to a page boundary."""
    head = prefix + struct.pack("<I", len(blob)) + b"\x00" * 8
    raw = head + blob
    if len(raw) % PAGE:
        raw += b"\xff" * (PAGE - len(raw) % PAGE)
    return [raw[i:i + PAGE] for i in range(0, len(raw), PAGE)]


def write_area(p, first_block, pages, label, delay=0.0015):
    nblocks = (len(pages) + PPB - 1) // PPB
    print("%s: %d pages -> blocks %d..%d" % (label, len(pages), first_block,
                                             first_block + nblocks - 1))
    for i in range(nblocks):
        erase_block(p, first_block + i)
    for i, pg in enumerate(pages):
        write_page(p, first_block * PPB + i, pg, b"\xff" * MS, delay)
    for i, pg in enumerate(pages):
        d, _ = read_page(p, first_block * PPB + i)
        if d != pg:
            raise SystemExit("verify failed at page %d (%s)"
                             % (first_block * PPB + i, label))
    print("  %s written + verified" % label)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port")
    ap.add_argument("--images", nargs=3, metavar=("MENU_SB", "HP_BIN", "NUTTX_BIN"))
    ap.add_argument("--nx-header", action="store_true",
                    help="prefix the NuttX image with the NXB1 header (needed "
                         "when the menu loads it; pass '-' to skip a slot)")
    ap.add_argument("--only", choices=("menu", "hp", "nuttx"), default=None,
                    help="write only one of the three areas")
    ap.add_argument("--delay", type=float, default=0.0015,
                    help="pause between CDC chunks, in seconds (0.003 is the "
                         "slowest value known to be reliable)")
    ap.add_argument("--ldlb", action="store_true")
    ap.add_argument("--restore-ldlb", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--backup-data", default="nand-dump-new.bin")
    ap.add_argument("--backup-oob", default="nand-oob-new.bin")
    args = ap.parse_args()

    path = find_port(args.port)
    if not path:
        raise SystemExit("no CDC port found -- load the nandrestore firmware first")

    have_backup = os.path.exists(args.backup_data) and os.path.exists(args.backup_oob)

    def bk_page(page):
        with open(args.backup_data, "rb") as f:
            f.seek(page * PAGE)
            d = f.read(PAGE)
        with open(args.backup_oob, "rb") as f:
            f.seek(page * MS)
            m = f.read(MS)
        return d, m

    p = connect(path)

    if args.show:
        for b in LDLB_BLOCKS:
            d, m = read_page(p, b * PPB)
            print("block %d page0: fw_start_nand=%d start_sector=%d stride=%d sectors=%d | "
                  "fw2_start=%d sectors2=%d"
                  % (b, struct.unpack_from("<I", d, 48)[0],
                     struct.unpack_from("<I", d, 52)[0],
                     struct.unpack_from("<I", d, 56)[0],
                     struct.unpack_from("<I", d, 60)[0],
                     struct.unpack_from("<I", d, 68)[0],
                     struct.unpack_from("<I", d, 76)[0]))
        p.write(MAGIC_E)
        p.close()
        return

    if args.images and not (args.ldlb or args.restore_ldlb):
        menu_sb, hp_bin, nx_bin = (None if x == "-" else open(x, "rb").read()
                                   for x in args.images)
        menu_pages = payload_pages(b"", menu_sb)          # the menu is the stream itself
        hp_pages = payload_pages(b"HPB1", hp_bin) if hp_bin is not None else None
        # The NuttX segment is read by the menu, not by the boot ROM, so the
        # NXB1 header goes in front of the raw binary. Without --nx-header the
        # image is written verbatim, which is what the boot ROM would want if
        # it were pointed at this block directly.
        if args.nx_header:
            nx_pages = payload_pages(b"NXB1", nx_bin)
        else:
            nx_pages = [nx_bin[i:i + PAGE].ljust(PAGE, b"\xff")
                        for i in range(0, len(nx_bin), PAGE)]

        # The first page of the menu stream is the sb1 header; write it verbatim.
        if args.only in (None, "menu"):
            write_area(p, MENU_BLOCK, [menu_sb[i:i + PAGE].ljust(PAGE, b"\xff")
                                       for i in range(0, len(menu_sb), PAGE)],
                       "menu", args.delay)
        if args.only in (None, "hp") and hp_pages is not None:
            write_area(p, HP_BLOCK, hp_pages, "hp script", args.delay)
        if args.only in (None, "nuttx"):
            write_area(p, NX_BLOCK, nx_pages, "nuttx image", args.delay)
        print("menu pages: %d" % len(menu_pages))
        p.write(MAGIC_E)
        p.close()
        return

    if args.restore_ldlb:
        if not have_backup:
            raise SystemExit("need %s / %s" % (args.backup_data, args.backup_oob))
        for b in LDLB_BLOCKS:
            d0, m0 = bk_page(b * PPB)
            d1, m1 = bk_page(b * PPB + 1)
            erase_block(p, b)
            write_page(p, b * PPB, d0, m0)
            write_page(p, b * PPB + 1, d1, m1)
            print("block %d restored from backup" % b)
        p.write(MAGIC_E)
        p.close()
        return

    if args.ldlb:
        if not have_backup:
            raise SystemExit("need backup to preserve LDLB2 page")
        # Take the menu size from the stream header already in NAND;
        # image_size there is counted in 16-byte blocks.
        hdr, _ = read_page(p, MENU_BLOCK * PPB)
        if hdr[20:24] != b"STMP":
            raise SystemExit("menu image not found at page %d" % (MENU_BLOCK * PPB))
        img_blocks = struct.unpack_from("<I", hdr, 28)[0]
        menu_pages = (img_blocks * 16 + PAGE - 1) // PAGE
        print("menu SB: %d bytes -> %d pages"
              % (img_blocks * 16, menu_pages))

        for b in LDLB_BLOCKS:
            d0, m0 = read_page(p, b * PPB)   # current LDLB, maybe already ours
            base = d0
            if have_backup and not (base[44:48] == b"LDLB"):
                base = bk_page(b * PPB)[0]
            new = bytearray(base)
            struct.pack_into("<I", new, 52, MENU_BLOCK * PPB)
            struct.pack_into("<I", new, 60, menu_pages)
            struct.pack_into("<I", new, 68, MENU_BLOCK * PPB)
            struct.pack_into("<I", new, 76, menu_pages)
            # Rewrite the whole block: page0 is the new LDLB, page1 the
            # preserved LDLB2, every other page was 0xff and stays 0xff.
            o1, o2 = bk_page(b * PPB + 1)
            if not have_backup:
                o1, o2 = read_page(p, b * PPB + 1)
            erase_block(p, b)
            write_page(p, b * PPB, bytes(new), m0 if m0 else b"\xff\xffBCB " + b"\xff" * 14)
            write_page(p, b * PPB + 1, o1, o2)
            chk, _ = read_page(p, b * PPB)
            if chk != bytes(new):
                raise SystemExit("LDLB verify failed on block %d" % b)
            print("block %d LDLB -> menu at page %d (%d pages)"
                  % (b, MENU_BLOCK * PPB, menu_pages))
        p.write(MAGIC_E)
        p.close()
        return

    print("nothing to do")


if __name__ == "__main__":
    main()
