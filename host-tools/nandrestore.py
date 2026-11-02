#!/usr/bin/env python3
"""
Write a NAND image back to the calculator, or verify one against the device.
No third-party dependencies; drives the CDC port through termios.

The other end is the nandrestore firmware (built with -DRESTORE_ONLY), which
speaks the mirror image of the nandump protocol:

  banner: "NANDRESTORE v1 pagesize=.. meta=.. pagesperblock=.. blocks=..\n"
  frame:  magic(4) | page(u32) | datalen(u32) | metalen(u32) | data | meta | crc32(u32)
  reply:  "W ok <page>" / "V ok <page>" / "W bad <page>" / "B ok <blk>" / "E ..."

Modes:
  --verify  read-only (default): send the backup, let the firmware compare
  --write   actually program, erasing each block first and reading back

Usage:
  python3 nandrestore.py --verify --start 0 --count 256
  python3 nandrestore.py --write  --start 0 --count 64
  python3 nandrestore.py --write  --all
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

MAGIC_W = b"PNR1"
MAGIC_V = b"PNR2"
MAGIC_P = b"PNR3"
MAGIC_B = b"PNR4"
MAGIC_E = b"PNR0"


class Port:
    def __init__(self, path, baud=termios.B115200):
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
        a[4] = a[5] = baud
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

    def write_paced(self, data, chunk=512, delay=0.0015):
        """The device-side CDC RX FIFO is only 512 bytes (TinyUSB high-speed
        config), so a whole frame at full speed overflows it. Pace the sends."""
        for i in range(0, len(data), chunk):
            self.write(data[i:i + chunk])
            time.sleep(delay)

    def read_exact(self, n, timeout=15.0):
        buf = b""
        end = time.time() + timeout
        while len(buf) < n and time.time() < end:
            c = self.read(n - len(buf), 0.3)
            if c:
                buf += c
        return buf

    def readline(self, timeout=5.0):
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


def find_port(explicit=None):
    if explicit:
        return explicit
    # macOS exposes the CDC interface as /dev/cu.usbmodem*, Linux as
    # /dev/ttyACM*. Same protocol either way.
    cands = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    return cands[0] if cands else None


def frame(magic, page, data, meta):
    hdr = struct.pack("<III", page, len(data), len(meta))
    crc = zlib.crc32(hdr + data + meta) & 0xFFFFFFFF
    return magic + hdr + data + meta + struct.pack("<I", crc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port")
    ap.add_argument("-d", "--data", default="nand-data-full.bin")
    ap.add_argument("-o", "--oob", default="nand-oob-full.bin")
    ap.add_argument("--write", action="store_true",
                    help="program the device (default is verify only)")
    ap.add_argument("--verify", action="store_true", help="read-only verify (this is the default)")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=64)
    ap.add_argument("--all", action="store_true", help="all 65536 pages (the whole 128 MB)")
    ap.add_argument("--ping", action="store_true", help="ping the firmware and exit")
    ap.add_argument("--eraseblk", type=int, default=None,
                    help="erase one block and exit")
    ap.add_argument("--pattern", action="store_true",
                    help="send a generated test pattern instead of the backup")
    ap.add_argument("--nometa", action="store_true",
                    help="do not send OOB metadata (data area only)")
    ap.add_argument("--read", type=int, default=None,
                    help="read back one page and print or save it")
    ap.add_argument("--dump", action="store_true",
                    help="read a range back and save it")
    ap.add_argument("--out", default=None, help="output file for --read / --dump")
    ap.add_argument("--fill", default=None, help="fill with a fixed byte, e.g. 00 or a5")
    ap.add_argument("--chunk", type=int, default=256, help="CDC chunk size in bytes")
    ap.add_argument("--no-erase-first", action="store_true",
                    help="do not erase each block before writing")
    ap.add_argument("--no-skip-blank", action="store_true",
                    help="also write all-0xff pages (rewrites the ECC bytes in OOB)")
    ap.add_argument("--delay", type=float, default=0.0015, help="delay between chunks, seconds")
    ap.add_argument("--retry", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    port_path = find_port(args.port)
    if not port_path:
        print("no CDC port found (load the nandrestore firmware first)",
              file=sys.stderr)
        return 2

    img = b""
    meta_img = b""
    if not args.dump:
        with open(args.data, "rb") as f:
            img = f.read()
        if os.path.exists(args.oob):
            with open(args.oob, "rb") as f:
                meta_img = f.read()

    port = Port(port_path)
    print("port:", port_path)

    banner = port.readline(10.0)
    if not banner or not banner.startswith("NANDRESTORE"):
        print("no banner (got %r) -- firmware not running, or CDC not connected?" % banner,
              file=sys.stderr)
        port.close()
        return 3
    print(banner)

    kv = dict(x.split("=", 1) for x in banner.split() if "=" in x)
    ps = int(kv.get("pagesize", "2048"))
    ms = int(kv.get("meta", "19"))
    ppb = int(kv.get("pagesperblock", "64"))
    blocks = int(kv.get("blocks", "1024"))
    total = blocks * ppb
    print("geometry: page=%d meta=%d pages/block=%d blocks=%d total=%d"
          % (ps, ms, ppb, blocks, total))

    def sync():
        # A new connection may still have bytes from the previous session
        # sitting in the device CDC; ping until it answers.
        for _ in range(8):
            port.write_paced(frame(MAGIC_P, 0, b"", b""))
            r = port.readline(3.0)
            if r == "P ok":
                return True
        return False

    if not sync():
        print("sync failed", file=sys.stderr)
        port.close()
        return 3
    if args.ping:
        print("ping -> P ok")
        port.write(MAGIC_E)
        port.close()
        return 0

    if args.read is not None:
        port.write_paced(frame(b"PNR5", args.read, b"", b""))
        hdr = port.read_exact(16)
        if len(hdr) != 16 or hdr[:4] != b"PND1":
            print("bad read frame: %r" % hdr[:16], file=sys.stderr)
            port.close()
            return 5
        page, dlen, mlen = struct.unpack("<III", hdr[4:16])
        body = port.read_exact(dlen + mlen + 4)
        data, meta = body[:dlen], body[dlen:dlen + mlen]
        crc_rx = struct.unpack("<I", body[dlen + mlen:dlen + mlen + 4])[0]
        crc_ok = (zlib.crc32(hdr[4:16] + data + meta) & 0xFFFFFFFF) == crc_rx
        print("page %d dlen=%d mlen=%d crc=%s" % (page, dlen, mlen,
                                                  "ok" if crc_ok else "BAD"))
        print("data[0:32] " + " ".join("%02x" % b for b in data[:32]))
        print("meta[0:19] " + " ".join("%02x" % b for b in meta[:19]))
        if args.out:
            with open(args.out, "wb") as f:
                f.write(data + meta)
            print("saved", args.out)
        port.write(MAGIC_E)
        port.close()
        return 0 if crc_ok else 6

    if args.eraseblk is not None:
        port.write_paced(frame(MAGIC_B, args.eraseblk, b"", b""))
        print("erase %d ->" % args.eraseblk, port.readline(args.timeout))
        port.write(MAGIC_E)
        port.close()
        return 0

    def page_data(page):
        if args.fill is not None:
            v = int(args.fill, 16) & 0xff
            return bytes([v]) * ps, (bytes([v]) * ms if ms else b"")
        if args.pattern:
            data = bytes((page + i) & 0xff for i in range(ps))
        else:
            data = img[page * ps:(page + 1) * ps]
        if args.nometa:
            return data, b""
        if args.pattern:
            meta = bytes(((page * 7 + i) & 0xff) for i in range(ms)) if ms else b""
        else:
            meta = meta_img[page * ms:(page + 1) * ms] if (ms and meta_img) else b""
        return data, meta

    if args.all:
        start, count = 0, total
    else:
        start, count = args.start, args.count

    if not args.pattern and not args.dump:
        if (start + count) * ps > len(img):
            print("backup only covers %d pages" % (len(img) // ps),
                  file=sys.stderr)
            return 4
        if ms and meta_img and (start + count) * ms > len(meta_img):
            print("oob backup too short", file=sys.stderr)
            return 4

    if args.dump:
        start, count = (0, total) if args.all else (args.start, args.count)
        out_data = bytearray()
        out_meta = bytearray()
        bad_pages = []
        print("DUMP pages %d..%d" % (start, start + count - 1))
        t0 = time.time()

        def open_synced():
            q = Port(port_path)
            b = q.readline(10.0)
            if not b or not b.startswith("NANDRESTORE"):
                q.close()
                return None
            for _ in range(8):
                q.write_paced(frame(MAGIC_P, 0, b"", b""), args.chunk, args.delay)
                r = q.readline(3.0)
                if r == "P ok":
                    return q
            q.close()
            return None

        for i in range(count):
            page = start + i
            body = None
            for _ in range(3):
                try:
                    port.write_paced(frame(b"PNR5", page, b"", b""),
                                     args.chunk, args.delay)
                    hdr = port.read_exact(16)
                    if len(hdr) == 16 and hdr[:4] == b"PND1":
                        _, dlen, mlen = struct.unpack("<III", hdr[4:16])
                        b = port.read_exact(dlen + mlen + 4)
                        if len(b) == dlen + mlen + 4:
                            body = b
                            break
                except Exception:
                    pass
                try:
                    port.close()
                except Exception:
                    pass
                port = open_synced()
                if port is None:
                    print("reconnect failed at page %d" % page, file=sys.stderr)
                    return 5
            if body is None:
                bad_pages.append(page)
                out_data += b"\xff" * ps
                out_meta += b"\xff" * ms
            else:
                out_data += body[:dlen]
                out_meta += body[dlen:dlen + mlen]
            if (i + 1) % 512 == 0 or i + 1 == count:
                el = time.time() - t0
                print("  %d/%d  %.0f pages/s  bad=%d"
                      % (i + 1, count, (i + 1) / el if el else 0,
                         len(bad_pages)), flush=True)

        dfile = args.data if args.data != "nand-data-full.bin" else "nand-dump-new.bin"
        ofile = args.oob if args.oob != "nand-oob-full.bin" else "nand-oob-new.bin"
        with open(dfile, "wb") as f:
            f.write(out_data)
        with open(ofile, "wb") as f:
            f.write(out_meta)
        print("saved dump pages=%d bad=%d -> %s %s"
              % (count, len(bad_pages), dfile, ofile))
        if bad_pages:
            print("bad pages:", bad_pages[:20])
        port.write(MAGIC_E)
        port.close()
        return 0

    magic = MAGIC_W if args.write else MAGIC_V
    mode = "WRITE" if args.write else "VERIFY"
    print("%s pages %d..%d" % (mode, start, start + count - 1))

    ok = bad = skipped = 0
    cur_blk = None
    erase_first = args.write and not args.no_erase_first
    skip_blank = args.write and not args.no_skip_blank
    t0 = time.time()
    for i in range(count):
        page = start + i

        # Write mode: erase the block explicitly first (the firmware remembers
        # the last erase and will not do it twice), so that an all-0xff block
        # also ends up in a genuine erased state.
        if erase_first:
            blk = page // ppb
            if blk != cur_blk:
                r = None
                for _ in range(args.retry):
                    port.write_paced(frame(MAGIC_B, blk, b"", b""),
                                     args.chunk, args.delay)
                    r = port.readline(args.timeout)
                    if r and not (r.startswith("E crc") or r.startswith("E fmt")):
                        break
                if not r or not r.startswith("B ok"):
                    print("erase block %d failed: %r" % (blk, r), file=sys.stderr)
                    port.close()
                    return 5
                cur_blk = blk

        data, meta = page_data(page)

        # Blank pages are not programmed: they stay erased, and the ECC area
        # in OOB stays 0xff.
        if skip_blank and data == b"\xff" * ps and (not meta or meta == b"\xff" * len(meta)):
            skipped += 1
            if (i + 1) % 512 == 0 or i + 1 == count:
                el = time.time() - t0
                print("  %d/%d  ok=%d bad=%d skip=%d  %.0f KB/s"
                      % (i + 1, count, ok, bad, skipped,
                         (i + 1) * ps / el / 1024.0 if el else 0), flush=True)
            continue

        payload = frame(magic, page, data, meta)
        resp = None
        for _ in range(args.retry):
            port.write_paced(payload, args.chunk, args.delay)
            resp = port.readline(args.timeout)
            if resp and not (resp.startswith("E crc") or resp.startswith("E fmt")):
                break
        if resp is None:
            print("\nno response for page %d" % page, file=sys.stderr)
            port.close()
            return 5

        if resp.startswith("W ok") or resp.startswith("V ok"):
            ok += 1
        else:
            bad += 1
            print("  page %d: %s" % (page, resp))

        if (i + 1) % 512 == 0 or i + 1 == count:
            el = time.time() - t0
            rate = (i + 1) * ps / el / 1024.0 if el > 0 else 0
            print("  %d/%d  ok=%d bad=%d skip=%d  %.0f KB/s"
                  % (i + 1, count, ok, bad, skipped, rate), flush=True)

    port.write(MAGIC_E)
    print(port.readline(3.0) or "")
    port.close()

    print("%s done: ok=%d bad=%d skip=%d" % (mode, ok, bad, skipped))
    return 0 if bad == 0 else 6


if __name__ == "__main__":
    sys.exit(main())
