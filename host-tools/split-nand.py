#!/usr/bin/env python3
"""
Convert between the interleaved NAND dump that nanddump.py produces and the
split data / OOB pair that nandrestore.py and install_menu.py expect.

nanddump.py writes one file, pagesize + meta bytes per page:

    [ data(2048) | oob(19) ][ data(2048) | oob(19) ] ...

nandrestore.py and install_menu.py want two flat arrays instead:

    nand-data.bin   65536 * 2048 = 134217728 bytes
    nand-oob.bin    65536 *   19 =   1245184 bytes

Usage:
  split-nand.py --split nand-full.raw                 # -> nand-full.data, nand-full.oob
  split-nand.py --split nand-full.raw --out prefix    # -> prefix.data, prefix.oob
  split-nand.py --join nand-data.bin nand-oob.bin -o nand-full.raw
  split-nand.py --info nand-full.raw                  # page count and geometry guess
"""

import argparse
import os
import sys

PAGE = 2048
META = 19


def do_info(path, page, meta):
    size = os.path.getsize(path)
    stride = page + meta
    if size % stride:
        print("%s: %d bytes is not a multiple of %d (page+meta)" % (path, size, stride))
        return 1
    pages = size // stride
    print("%s: %d pages (%d data bytes + %d oob bytes)" % (path, pages, pages * page, pages * meta))
    if pages == 65536:
        print("        = a complete 128 MB dump")
    return 0


def do_split(path, out_prefix, page, meta):
    stride = page + meta
    size = os.path.getsize(path)
    if size % stride:
        print("%s: %d bytes is not a multiple of %d; is --page/--meta right?"
              % (path, size, stride), file=sys.stderr)
        return 2
    pages = size // stride

    data_path = out_prefix + ".data"
    oob_path = out_prefix + ".oob"
    with open(path, "rb") as src, open(data_path, "wb") as dout, open(oob_path, "wb") as mout:
        for _ in range(pages):
            dout.write(src.read(page))
            mout.write(src.read(meta))

    print("%d pages -> %s (%d B), %s (%d B)"
          % (pages, data_path, pages * page, oob_path, pages * meta))
    return 0


def do_join(data_path, oob_path, out_path, page, meta):
    dsize = os.path.getsize(data_path)
    msize = os.path.getsize(oob_path) if oob_path and os.path.exists(oob_path) else 0
    if dsize % page:
        print("%s: not a multiple of the page size" % data_path, file=sys.stderr)
        return 2
    pages = dsize // page
    if msize and msize != pages * meta:
        print("%s: expected %d bytes of OOB, found %d"
              % (oob_path, pages * meta, msize), file=sys.stderr)
        return 2

    blank = b"\xff" * meta
    with open(data_path, "rb") as din, open(out_path, "wb") as out:
        min_ = open(oob_path, "rb") if msize else None
        try:
            for _ in range(pages):
                out.write(din.read(page))
                out.write(min_.read(meta) if min_ else blank)
        finally:
            if min_:
                min_.close()

    print("%d pages -> %s (%d B)" % (pages, out_path, pages * (page + meta)))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--page", type=int, default=PAGE, help="page size (default 2048)")
    ap.add_argument("--meta", type=int, default=META, help="OOB bytes per page (default 19)")
    ap.add_argument("--split", metavar="RAW", help="interleaved file to split")
    ap.add_argument("--join", nargs=2, metavar=("DATA", "OOB"), help="two flat files to interleave")
    ap.add_argument("--info", metavar="RAW", help="report geometry of an interleaved file")
    ap.add_argument("--out", metavar="PATH", help="output prefix (--split) or file (--join)")
    args = ap.parse_args()

    if args.info:
        return do_info(args.info, args.page, args.meta)

    if args.split:
        prefix = args.out or (os.path.splitext(args.split)[0] or "nand")
        return do_split(args.split, prefix, args.page, args.meta)

    if args.join:
        if not args.out:
            print("--join needs --out", file=sys.stderr)
            return 2
        return do_join(args.join[0], args.join[1], args.out, args.page, args.meta)

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
