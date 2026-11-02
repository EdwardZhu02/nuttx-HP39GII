#!/usr/bin/env python3
"""
Read a raw NAND dump off the calculator. No third-party dependencies; drives the
CDC port through termios.

The other end is the nandump firmware (built with -DDUMP_ONLY) running from SRAM,
which streams:

  text banner: "NANDUMP v1 pagesize=.. meta=.. pagesperblock=.. blocks=..\\n"
  binary frame: "PND1" | page(u32) | datalen(u32) | metalen(u32) | data | meta | crc32(u32)
  terminator:   "NANDUMP END\\n"

Output is one interleaved file, pagesize + meta bytes per page, so page N starts
at N*(pagesize+meta). Use split-nand.py to get the data and OOB arrays apart;
that is the form nandrestore.py and install_menu.py expect.

Usage: python3 nanddump.py [-p PORT] [-o OUT] [-n PAGES]
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

MAGIC = b"PND1"


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
        os.write(self.fd, data)

    def close(self):
        os.close(self.fd)


def find_port(explicit=None):
    if explicit:
        return explicit
    # macOS exposes the CDC interface as /dev/cu.usbmodem*, Linux as
    # /dev/ttyACM*. Same protocol either way.
    cands = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    return cands[0] if cands else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port")
    ap.add_argument("-o", "--out", default="nand-backup.bin")
    ap.add_argument("-n", "--pages", type=int, default=4096)
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    port_path = find_port(args.port)
    if not port_path:
        print("no CDC port found (the device only appears once the nandump "
              "firmware is running)", file=sys.stderr)
        return 2

    print("port: %s" % port_path)
    port = Port(port_path)

    # ---- banner ----
    print("waiting for banner (opening the port asserts DTR, which starts the dump)...")
    banner = b""
    t0 = time.time()
    while b"\n" not in banner:
        c = port.read(512, 0.5)
        if c:
            banner += c
        if time.time() - t0 > args.timeout:
            print("timeout; got %r" % banner[:120], file=sys.stderr)
            return 3

    line, rest = banner.split(b"\n", 1)
    print("banner: %s" % line.decode("ascii", "replace").strip())

    geo = {}
    for kv in line.decode("ascii", "replace").split():
        if "=" in kv:
            k, v = kv.split("=", 1)
            try:
                geo[k] = int(v)
            except ValueError:
                pass

    pagesize = geo.get("pagesize", 2048)
    metasize = geo.get("meta", 64)
    print("geometry: page=%d meta=%d total_blocks=%s" %
          (pagesize, metasize, geo.get("blocks", "?")))

    frame_len = 16 + pagesize + metasize + 4
    buf = bytearray(rest)

    out = open(args.out, "wb")
    seen = bad = 0
    wrote = set()
    ended = False
    t0 = time.time()
    idle = 0.0

    def need(n):
        nonlocal idle
        while len(buf) < n:
            c = port.read(max(1, n - len(buf)), 0.5)
            if c:
                buf.extend(c)
                idle = 0.0
            else:
                idle += 0.5
                if idle > 8.0:
                    return False
        return True

    while not ended:
        if not need(4):
            print("\nstream stalled (idle 8s)", file=sys.stderr)
            break
        if buf[:4] != MAGIC:
            if b"NANDUMP END" in bytes(buf[:64]):
                ended = True
                break
            del buf[0]
            continue

        if not need(frame_len):
            break

        page, dlen, mlen = struct.unpack("<III", bytes(buf[4:16]))
        body = bytes(buf[16:16 + dlen + mlen])
        crc_expect = struct.unpack("<I", bytes(buf[16 + dlen + mlen:frame_len]))[0]
        crc_calc = zlib.crc32(struct.pack("<III", page, dlen, mlen) + body) & 0xFFFFFFFF
        if crc_calc != crc_expect:
            bad += 1
            if bad <= 8:
                print("\npage %d CRC mismatch (%08x vs %08x)" %
                      (page, crc_calc, crc_expect), file=sys.stderr)

        out.seek(page * (pagesize + metasize))
        out.write(body)
        wrote.add(page)
        seen += 1
        del buf[:frame_len]

        if seen % 128 == 0:
            dt = time.time() - t0
            rate = seen / dt if dt > 0 else 0
            print("\r  %d/%d pages  %.0f p/s  %.0f KB/s" %
                  (seen, args.pages, rate, rate * pagesize / 1024), end="")
            sys.stdout.flush()

    out.close()
    dt = time.time() - t0
    size = os.path.getsize(args.out)
    print("\ndone: %d pages, %d bytes, %d crc errors, %.1fs" % (seen, size, bad, dt))

    missing = [p for p in range(min(args.pages, max(wrote) + 1 if wrote else 0))
               if p not in wrote]
    if missing and seen:
        print("MISSING %d pages (first: %s)" % (len(missing), missing[:8]),
              file=sys.stderr)
        return 4
    return 0 if (bad == 0 and seen > 0) else 5


if __name__ == "__main__":
    sys.exit(main())
