#!/usr/bin/env python3
"""
HP 39gII 双启动安装工具（配合 RAM 里的 nandrestore 固件，走 USB CDC）。

把三样东西写进 NAND 的空闲区，再把块 8/12 的 LDLB 指向菜单固件：

  块 240        菜单固件 SB（由 BootROM 载入到 0x50000 执行）
  块 241..242   原厂引导段（16 字节头 "HPB1" + 离线解密出的 SB 指令流）
  块 243..246   NuttX 镜像（16 字节头 "NXB1" + nuttx.bin，最多 256 页）
  块 800..1023  KhiCAS 分页引擎（khicas.bin，裸数据，在文件系统之外）
  块 8/12 page0 LDLB：firmware_start_sector -> 240*64，sectors -> 菜单页数

块 0..239 里除了 8/12 的 page0（和它们被擦后必须写回的 page1）之外一律不动。

用法:
  install_menu.py --images  menu.sb hp-boot-section.bin nuttx.bin --nx-header \
                 --khicas khicas.bin
  install_menu.py --ldlb                    # 只改 LDLB（先 --images 验证过再跑）
  install_menu.py --show                    # 读出 LDLB 两页看看
  install_menu.py --restore-ldlb            # 用备份把 LDLB 还原成原厂值
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

# KhiCAS 分页代码区（块 800..1023，裸数据）
KHICAS_FIRST_BLOCK = 800
KHICAS_BLOCKS = 224

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
    c = sorted(glob.glob("/dev/cu.usbmodem*"))
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
    """16 字节头 + 数据，按页对齐"""
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
    ap.add_argument("--khicas", metavar="BLOB",
                    help="把 KhiCAS 分页代码镜像 (khicas.bin) 写到块 800（裸数据，无头）")
    ap.add_argument("--nx-header", action="store_true",
                    help="NuttX 段加上 NXB1 头（菜单自己加载时用）")
    ap.add_argument("--only", choices=("menu", "hp", "nuttx"), default=None,
                    help="只写其中一项（配合 --images）")
    ap.add_argument("--delay", type=float, default=0.0015, help="CDC 分块间隔秒")
    ap.add_argument("--ldlb", action="store_true")
    ap.add_argument("--restore-ldlb", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--backup-data", default="nand-dump-new.bin")
    ap.add_argument("--backup-oob", default="nand-oob-new.bin")
    args = ap.parse_args()

    path = find_port(args.port)
    if not path:
        raise SystemExit("no /dev/cu.usbmodem* — 先载入 nandrestore 固件")

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

    def write_khicas():
        blob = open(args.khicas, "rb").read()
        maxlen = KHICAS_BLOCKS * PPB * PAGE
        if len(blob) > maxlen:
            raise SystemExit("khicas.bin is %d bytes, region holds %d"
                             % (len(blob), maxlen))
        if len(blob) % PAGE:
            blob += b"\xff" * (PAGE - len(blob) % PAGE)
        pages = [blob[i:i + PAGE] for i in range(0, len(blob), PAGE)]
        write_area(p, KHICAS_FIRST_BLOCK, pages, "khicas", args.delay)

    # --khicas on its own
    if args.khicas and not (args.images or args.ldlb or args.restore_ldlb):
        write_khicas()
        p.write(MAGIC_E)
        p.close()
        return

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
        menu_sb, hp_bin, nx_bin = (open(x, "rb").read() for x in args.images)
        menu_pages = payload_pages(b"", menu_sb)          # 菜单本身就是 SB 流，不加头
        hp_pages = payload_pages(b"HPB1", hp_bin)
        # NuttX 这一段是给 ROM 直接执行的 SB 流，不能加头（加头会让
        # ROM 把 "NXB1" 当 SB 签名而放弃启动）
        if args.nx_header:
            nx_pages = payload_pages(b"NXB1", nx_bin)
        else:
            nx_pages = [nx_bin[i:i + PAGE].ljust(PAGE, b"\xff")
                        for i in range(0, len(nx_bin), PAGE)]

        # 菜单 SB 的第一页就是流头，原样写
        if args.only in (None, "menu"):
            write_area(p, MENU_BLOCK, [menu_sb[i:i + PAGE].ljust(PAGE, b"\xff")
                                       for i in range(0, len(menu_sb), PAGE)],
                       "menu", args.delay)
        if args.only in (None, "hp"):
            write_area(p, HP_BLOCK, hp_pages, "hp script", args.delay)
        if args.only in (None, "nuttx"):
            write_area(p, NX_BLOCK, nx_pages, "nuttx image", args.delay)
        # Kernel and engine are one image pair; write both from one command
        # so they cannot come from different builds.
        if args.khicas:
            write_khicas()
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
        # 菜单页数直接从 NAND 上菜单 SB 的头部算（image_size 以 16 字节块计）
        hdr, _ = read_page(p, MENU_BLOCK * PPB)
        if hdr[20:24] != b"STMP":
            raise SystemExit("menu image not found at page %d" % (MENU_BLOCK * PPB))
        img_blocks = struct.unpack_from("<I", hdr, 28)[0]
        menu_pages = (img_blocks * 16 + PAGE - 1) // PAGE
        print("menu SB: %d bytes -> %d pages"
              % (img_blocks * 16, menu_pages))

        for b in LDLB_BLOCKS:
            d0, m0 = read_page(p, b * PPB)          # 当前 LDLB（可能已经是菜单版）
            base = d0
            if have_backup and not (base[44:48] == b"LDLB"):
                base = bk_page(b * PPB)[0]
            new = bytearray(base)
            struct.pack_into("<I", new, 52, MENU_BLOCK * PPB)
            struct.pack_into("<I", new, 60, menu_pages)
            struct.pack_into("<I", new, 68, MENU_BLOCK * PPB)
            struct.pack_into("<I", new, 76, menu_pages)
            # 擦写整块：page0 = 新 LDLB，page1 = 原 LDLB2，其余页原本是全 ff
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
