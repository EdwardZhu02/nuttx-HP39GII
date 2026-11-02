#!/usr/bin/env python3
"""
Sanity-check an sb1 BootStream before it goes near the calculator.

An sb1 stream starts with an optional 16-byte key block and then the header:

    offset 0x00  STMP            signature
    offset 0x04  u32 version
    offset 0x08  u32 image_size  in 16-byte blocks, counted from file start

`elftosb -z` (the zero key, which every image here uses) always emits the 16-byte
key block, so the signature lands at 0x14. Looking for "STMP" at offset 0 -- the
obvious check -- fails on a perfectly good stream.

Usage: verify-sb.py FILE [FILE...]
Prints one line per file and exits non-zero on the first problem.
"""

import math
import struct
import sys


def check(path):
    with open(path, "rb") as f:
        data = f.read()

    size = len(data)
    if size < 32:
        return "%s: only %d bytes" % (path, size)

    off = -1
    for cand in (0x00, 0x14):
        if data[cand:cand + 4] == b"STMP":
            off = cand
            break
    if off < 0:
        return "%s: no STMP signature at 0x00 or 0x14" % path

    version, blocks = struct.unpack_from("<II", data, off + 4)
    expect = blocks * 16
    if expect != size:
        return ("%s: header says %d blocks = %d bytes, file is %d bytes"
                % (path, blocks, expect, size))

    print("%s: sb1 ok, key block %s, version 0x%08x, %d bytes, %d pages"
          % (path,
             "present" if off == 0x14 else "absent",
             version, size, math.ceil(size / 2048)))
    return None


def main(argv):
    if not argv:
        sys.stderr.write(__doc__.strip().splitlines()[-1] + "\n")
        return 2
    for path in argv:
        err = check(path)
        if err:
            sys.stderr.write("error: %s\n" % err)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
