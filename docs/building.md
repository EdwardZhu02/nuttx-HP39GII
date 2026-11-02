# Building

## What is actually in this repository

No upstream sources. `scripts/fetch-deps.sh` clones three trees at pinned commit
IDs and applies the patch series:

| Tree | Pin | Patches | Used for |
|---|---|---|---|
| `apache/nuttx` | `1080f140` | 22 | the port itself |
| `apache/nuttx-apps` | `b86baec` | 1 | NSH, `mksmartfs`, the shell, `clear` |
| `ExistOS-For-HP39GII` | `af623357` | 2 | boot menu, recovery firmware, `elftosb` |

Pins live in [`scripts/config.sh`](../scripts/config.sh). They are full commit IDs
because the patch series only applies to exactly those trees.

`.deps/` and `dist/` are build output; neither is committed.

## Toolchain

**Cross compiler.** `arm-none-eabi-gcc`, 10.3 or newer (the port was developed and
verified with 10.3-2021.10, which is what ExistOS and its CI use).

```bash
# Debian / Ubuntu
sudo apt-get install gcc-arm-none-eabi libnewlib-arm-none-eabi
# macOS
brew install --cask gcc-arm-embedded
# or the Arm download, which is what CI uses
curl -LO https://developer.arm.com/-/media/Files/downloads/gnu-rm/10.3-2021.10/gcc-arm-none-eabi-10.3-2021.10-x86_64-linux.tar.bz2
sudo tar -xf gcc-arm-none-eabi-10.3-2021.10-x86_64-linux.tar.bz2 -C /opt
export PATH=/opt/gcc-arm-none-eabi-10.3-2021.10/bin:$PATH
```

Set `CROSS_COMPILE` if your binaries have a different prefix, e.g.
`CROSS_COMPILE=arm-none-eabi-` is the default.

**Host tooling.** `git`, `make`, `cmake` ≥ 3.16, `python3`, `pkg-config`, and the
two libraries `elftosb` links against:

```bash
sudo apt-get install build-essential cmake pkg-config \
                     libcrypto++-dev libusb-1.0-0-dev libudev-dev
# macOS
brew install cmake pkg-config crypto++ libusb
```

`kconfig-frontends` is **not** needed. NuttX wants a kconfig implementation for
two things, and both are covered without it:

* `tools/configure.sh` ends in `tools/sethost.sh`, which calls `kconfig-tweak` to
  rewrite the host selection. `scripts/tools/kconfig-tweak` is a 100-line stand-in
  that implements the four operations `sethost.sh` uses; `build-nuttx.sh` puts it
  on `PATH` when the real one is missing.
* `make` re-runs `olddefconfig` whenever a `Kconfig` is newer than `.config`, and
  `apps_preconfig` rewrites the apps' `Kconfig` files on every build, so that is
  every build. NuttX prefers **kconfiglib** (a pip package) when `menuconfig` is
  on `PATH`; kconfiglib ships `olddefconfig`, `menuconfig`, `savedefconfig` and
  friends. If nothing is found, `build-nuttx.sh` installs kconfiglib into
  `.deps/pylib` with `pip install --target` — no root, no virtualenv, no change
  to your environment.

If you do want the interactive configurator, either `pip3 install --user
kconfiglib` (then `make menuconfig`) or install `kconfig-frontends`.

## Build

```bash
./scripts/fetch-deps.sh     # ~400 MB of clones, once
./scripts/build-all.sh
```

or step by step:

```bash
./scripts/build-sbtools.sh                  # elftosb
./scripts/build-nuttx.sh                    # dist/nuttx.bin, dist/nuttx.sb
./scripts/build-firmware.sh bootmenu        # dist/bootmenu.sb
./scripts/build-firmware.sh nandrestore     # dist/nandrestore.sb
./scripts/build-firmware.sh nanddump        # dist/nanddump-8m.sb
./scripts/build-firmware.sh nanddump-full   # dist/nanddump-full.sb
./scripts/build-firmware.sh probe           # dist/probe.sb
make -C host-tools check                    # python tools compile
make -C host-tools                          # sbloader-hid, on macOS
```

Expected sizes, so you can tell at a glance whether a build went wrong:

| Artifact | Size |
|---|---|
| `nuttx.bin` | ≈ 186 KB (36 % of the 512 KB SRAM) |
| `nuttx.sb` | ≈ 186 KB + 228 B of stream overhead |
| `bootmenu.sb` | ≈ 84 KB (42 pages) |
| `nandrestore.sb`, `nanddump*.sb`, `probe.sb` | ≈ 84–90 KB (one page count each) |

The build also stops if `nuttx.bin` would overlap the page table at `0x7C000`.

The `.sb` files are **not** bit-reproducible: `elftosb -z` emits a 16-byte key
block whose contents vary per run, so two builds of the same sources produce
different SHA256 sums for the stream (and the same `nuttx.bin` inside it). Compare
`nuttx.bin`, or the extracted payload, if you need to know whether two builds are
the same.

### NuttX configuration

`.config` is generated once from `boards/arm/stmp3770/hp39gii/configs/nsh/defconfig`
and then reused. `--reconfigure` regenerates it, `--clean` runs `make distclean`
first. If you change the defconfig, do it in the board directory and copy it back:

```bash
cd .deps/nuttx
make menuconfig                 # needs kconfiglib or kconfig-frontends
make savedefconfig              # produces a minimal defconfig
cp defconfig boards/arm/stmp3770/hp39gii/configs/nsh/defconfig
```

Relevant knobs:

| Option | Meaning |
|---|---|
| `CONFIG_STMP3770_LCD` | LCDIF + panel driver |
| `CONFIG_STMP3770_KBD`, `_KBD_CONSOLE` | keyboard scanning, inject into NSH's stdin |
| `CONFIG_STMP3770_NAND` | GPMI + ECC8 |
| `CONFIG_NSH_ALTCONDEV` + `CONFIG_NSH_ALTSTD*` | point NSH at `/dev/lcdcon` |
| `CONFIG_STMP3770_LCD_INIT` | initialise the panel during boot (off by default; see troubleshooting) |

### Where the NuttX port lives

Inside the patched tree, so the usual NuttX conventions apply:

```
arch/arm/src/stmp3770/          SoC: boot, clock, power, ICOLL, TIMROT, PL011,
                                LCD, LCD console, GPMI, ECC8, MTD, keyboard
boards/arm/stmp3770/hp39gii/    board: linker script, defconfig, board init,
                                keymap, NAND partition + SmartFS bring-up
```

`arch/arm/src/stmp3770/STATUS.md` in the tree is the original bring-up log —
long, in Chinese, and the most detailed description of why each driver looks the
way it does. This repository's `docs/` is the English summary.

## Emulator

EmuGII (QEMU `-M stmp3770`) is how the port was developed before hardware was
available. It is not part of this repository; if you want it, build
[EmuGII](https://github.com/ExistOS-Team/EmuGII) and remember three things:

```bash
# the machine only gets DRAM if you ask for it
qemu-system-arm -M stmp3770 -m 64 -bios dist/nuttx.bin -serial pty

# never use -serial file:... as a log. The chardev is opened read-write, so the
# guest reads its own output back as keyboard input and the shell eats itself.
# Use -serial pty and cat the pty, or a socket.

# APBH/LCDIF work, but you need a display backend for screendump to refresh:
# -display none alone returns the first frame forever.
-vnc 127.0.0.1:9 -monitor unix:/tmp/mon.sock,server,nowait
```

## Bumping the pins

```bash
cd .deps/nuttx
git fetch origin master
git rebase FETCH_HEAD              # or re-apply by hand and fix conflicts
git format-patch -o ../../patches/nuttx <new-base>..HEAD
# update NUTTX_BASE in scripts/config.sh to <new-base>
```

`patches/nuttx/0021-*` is a plain diff rather than a formatted commit; it applies
with `git am` like the rest, but its message and diffstat were written by hand, so
re-generate it from a real commit after a rebase.

## CI

`.github/workflows/build.yml` runs on every push and pull request. It uses Ubuntu
22.04, caches the Arm toolchain, builds everything listed above, checks that the
Python tools compile, and uploads `dist/` as an artifact. On a tag it attaches the
same files to the release. A second job builds the macOS host tools, because that
is the only place `sbloader-hid` compiles.

The workflow is the reference for a known-good build; if the local build fails and
CI is green, compare the toolchain version first. `.deps/` is deliberately not
cached: the pinned clones are cheap, and a stale patched tree is an expensive bug
to chase.
