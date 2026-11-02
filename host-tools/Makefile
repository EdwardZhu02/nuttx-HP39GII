# Host tools for the HP 39gII.
#
#   make            build whatever this platform supports
#   make check      byte-compile the Python tools
#   make clean
#
# sbloader-hid and hiddump only build on macOS: they go through IOKit to reach the
# boot ROM's HID interface. On Linux the libusb-based sbloader from sbtools does
# the same job (scripts/build-sbtools.sh builds it).

CC      ?= clang
CFLAGS  ?= -O2 -g -Wall -Wextra
LDFLAGS ?=
FRAMEWORKS = -framework IOKit -framework CoreFoundation

PY_TOOLS = install_menu.py nanddump.py nandrestore.py split-nand.py

UNAME := $(shell uname -s)

ifeq ($(UNAME),Darwin)
BINS = sbloader-hid hiddump
else
BINS =
endif

all: $(BINS)
ifeq ($(UNAME),Darwin)
	@echo "built: $(BINS)"
else
	@echo "this platform has no HID tools to build (macOS only)"
	@echo "on Linux, use .deps/existos/tools/sbtools/sbloader -d <image.sb>"
endif

sbloader-hid: sbloader-hid.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS) $(FRAMEWORKS)

hiddump: hiddump.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS) $(FRAMEWORKS)

check:
	@for f in $(PY_TOOLS); do \
	  python3 -m py_compile $$f || exit 1; \
	done
	@echo "python tools: syntax ok"

clean:
	rm -f $(BINS) *.o
	rm -rf __pycache__

.PHONY: all check clean
