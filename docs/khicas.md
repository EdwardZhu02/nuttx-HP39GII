# KhiCAS on NuttX — what it takes

KhiCAS is a port of Giac/Xcas to calculators. There is already a working KhiCAS for
this exact calculator: [ExistOS](https://github.com/ExistOS-Team/ExistOS-For-HP39GII)
ships one. This document records what that port actually is, what it costs, and what
would have to be built for the same CAS to run under NuttX.

Short version: **the CAS is not the hard part, the memory is.** KhiCAS is ~2.4 MB of
ARM code on a machine with 512 KB of SRAM. ExistOS runs it behind a demand-paging VM
whose backing store is NAND. NuttX has an in-tree pager of the same shape
(`CONFIG_LEGACY_PAGING`), but this port does not use it yet, and enabling it is the
real work item.

Measurements below are from the build artifacts in this repository's `.deps/existos`
tree (`sys.elf`, `libcas.libcpp`) with `arm-none-eabi-nm/size`; the mechanism comes
from reading `OSLoader/VmMgr/`.

## 1. What ExistOS does

### 1.1 KhiCAS is linked into the system image, not loaded as an app

`System/CMakeLists.txt` links `Libs/libcas.libcpp` (10.5 MB archive) and
`Libs/libkcasgui.libcpp` (1.3 MB) straight into `sys.elf`, alongside `libgmp` and
`libtommath`. There is no per-app loader, no separate binary: KhiCAS is part of the
5.27 MB `ExistOS.sys` image that the loader maps into a 6 MB virtual ROM window.

| Artifact | Size |
|---|---|
| `Libs/libcas.libcpp` (49 objects, full giac) | 10,553,738 B |
| `.text` summed over those 52 objects | 3,927,013 B (3.83 MB) |
| `giac::` symbols in `sys.elf` (5,894 of them) | 2,498,860 B (**2.44 MB**) |
| `sys.elf` total text | ~3.99 MB |
| `ExistOS.sys` | 5,275,004 B |

The 2.44 MB is the floor for the full command set as ExistOS ships it; the 3.83 MB is
everything the archive contains before the linker drops unreferenced members.

### 1.2 The memory system is the port

`OSLoader/Config/SystemConfig.h` (ExistOS's KhiCAS configuration):

| Constant | Value | Meaning |
|---|---|---|
| `PAGE_SIZE` | 1024 | 1 KB pages (tiny pages, ARMv5 short descriptors) |
| `TOTAL_MEM_PAGE` | 292 | 292 KB of SRAM managed as pages |
| `NUM_CACHEPAGE_VROM` | 160 | 160 KB of frames for **code** pages |
| `NUM_CACHEPAGE_VRAM` | 40 | 40 KB of frames for **data** pages |
| `ZRAM_SIZE` | 92 KB | compressed swap cache (quicklz) |
| `VM_ROM_BASE/SIZE` | `0x0010_0000` / 6 MB | virtual code window, backed by NAND |
| `VM_RAM_BASE/SIZE` | `0x0200_0000` / 3 MB | virtual data window, backed by zram + NAND swap |
| `SIZE_SWAPAREA_MB` | 3 | swap area in NAND |

`OSLoader/VmMgr/vmMgr.c` (1074 lines) + `mmu.c` (556) implement: a page-fault queue
(`PageFaultQueue`, `pageFaultInfo_t`), a fault-handling task, LRU-ish frame
replacement, NAND-backed swap for `VM_RAM`, and compression of evicted pages into
`ZRAM`. `vmMgr.h` enumerates the fault reasons it cares about — `FSR_ZRAM_OOM`,
`FSR_SWAP_NOTENABLE` — i.e. running out of frames is an expected, handled event, not a
crash.

The KhiCAS-side README (`Libs/src/khicas/README`) states the budget directly:

> Total free memory available 288K = 32K non swappable and 256K swappable.
> mixed RAM config: 32K at 0x20000000 never swapped (in giac used for 1 bit per pixel
> display, and for fast allocations of 16/32/48-bit gen).

So on this calculator KhiCAS runs with **288 KB of usable RAM**, of which 32 KB is
pinned (the 1bpp framebuffer, `System/applications/user/khicas/kcasporing_gl.c:27`,
`screen_1bpp = (char *)0x02000000`) and 256 KB is pageable. Code is paged in from NAND
continuously during normal use.

### 1.3 The glue ExistOS wrote is small

| File | Lines | Role |
|---|---|---|
| `System/applications/user/khicas/kcasporing_gl.c` | 2301 | 1bpp 256×127 framebuffer, `vGL_*` drawing/console/key primitives |
| `System/applications/user/khicas/khicas_stub.cpp` | 1095 | app entry, `Bfile_*` file API on the ExistOS filesystem, keyboard map, timers |

That is the whole platform layer. Giac itself is unmodified apart from the `k*.cc`
build split (`-DXLIGHT`, `-DHP39`, soft float, `-Os`, `-marm`).

## 2. What that means for NuttX

This port is a flat build with a static MMU map: 185 KB of text, 0.9 KB of data,
57 KB of bss, image ends at `0x3B680`, page table at `0x7C000`. That leaves **~258 KB
of heap**. KhiCAS needs 2.44 MB of code and a heap for giac's expression trees.

| | Bytes | vs 512 KB SRAM |
|---|---|---|
| KhiCAS code (measured) | 2,498,860 | 4.8 × the whole machine |
| Whole archive | 3,927,013 | 7.5 × |
| Free RAM today | 264,576 | — |

So: **there is no configuration of compiler flags that makes a static link fit.** Even
the empty shell of giac plus the parser is in the hundreds of KB. Any real port needs
the code to execute from somewhere other than resident SRAM.

### 2.1 Measured: one command costs the whole CAS

`--gc-sections` does not rescue this, and it is worth knowing why. The archives *are*
built with `-ffunction-sections` (275 `.text.*` sections in `alg_ext.o` alone), so the
linker can drop individual functions — but giac's generated command tables
(`static_lexer.h`, `static_lexer_full.h`, `unary_function_ptr` objects) reference every
command, so almost nothing is unreachable. Linking the smallest possible program against
the archives and forcing a single entry point:

| Entry point forced with `-u` | Text |
|---|---|
| `giac::_derive(gen const&, context const*)` + libgmp | 3,562,197 B |
| `giac::_factor(gen const&, context const*)` + libgmp | 3,560,285 B |
| `giac::_derive` without libgmp (tommath only) | **2,835,057 B** |

Two consequences:

* libgmp costs ~720 KB of the image; `USE_GMP_REPLACEMENTS` + libtommath is the smaller
  configuration, and it is the one to build for.
* Trimming the command set is a **source-level** job — editing the generated lexer /
  command tables and rebuilding from `Libs/src/khicas` — not a linker flag. Whatever
  survives, the expression engine alone is still megabytes, not hundreds of KB, so the
  paging requirement holds either way.


## 3. NuttX already has the mechanism

`arch/Kconfig:1096` still carries the legacy on-demand paging framework, and
`sched/paging/pg_miss.c` / `pg_worker.c` implement exactly the model this device needs:

* only **read-only text** is paged (`arch/arm/src/arm/arm_dataabort.c:85-92` says so
  explicitly: "In the NuttX on-demand paging implementation, only the read-only,
  .text…"), which is fine — giac's `.text` is what is huge, its `.data`/`.bss` are 4 KB
  and 24 KB in the ExistOS image;
* the platform supplies one function, `up_fillpage()`, to bring a page in;
* the memory layout is a ramp defined by `include/nuttx/page.h:77-170`:
  `[locked text/data][physical frames for paged text][heap]` in physical memory and
  `[locked][paged text window]` in virtual memory, parameterised by
  `PAGING_PAGESIZE`, `PAGING_NLOCKED`, `PAGING_NPPAGED`, `PAGING_NVPAGED`,
  `PAGING_NDATA`, `PAGING_BLOCKINGFILL`.

The closest existing precedent is `boards/arm/lpc31xx/ea3131/src/lpc31_fillpage.c`
(526 lines): LPC3131 is the same ARM926EJ-S class, and its `up_fillpage()` reads text
pages out of **NXFFS on NAND**. That is the same shape as what this board needs, with
GPMI+ECC8 in place of the LPC31xx NAND controller.

### 3.1 Proposed configuration for the HP 39gII

4 KB pages (the ARM926 coarse-page size; ExistOS's 1 KB tiny pages buy finer
granularity at more faults per byte — worth trying later, not first):

| Parameter | Value | Bytes |
|---|---|---|
| `PAGING_PAGESIZE` | 4096 | — |
| `PAGING_NLOCKED` | 64 | 256 KB resident: NuttX + HAL + giac data/bss |
| `PAGING_NPPAGED` | 24 | 96 KB of frames for code pages |
| `PAGING_NDATA` | 32 | 128 KB heap for giac |
| `PAGING_NVPAGED` | 896 | 3.5 MB virtual code window at `0x0040_0000` |
| `PAGING_BLOCKINGFILL` | y | GPMI reads block |

Resident 256 + frames 96 + heap 128 = 480 KB, below the 496 KB usable before the page
table at `0x7C000`. The virtual window starts above physical RAM on purpose: with the
MMU on, `0x0040_0000` is a legal virtual address even though the address bus only
decodes 19 bits.

### 3.2 What has to be written

1. **`up_fillpage()`** in `boards/arm/stmp3770/hp39gii/src/`: `vpage` → image offset →
   read the 2 KB NAND pages holding it through the existing GPMI driver → copy into the
   frame → invalidate the I-cache for that page. No dirty-page handling, no writeback.
2. **Image split.** The paged text cannot be part of the blob the menu copies to `0x0`.
   Two images: the resident prefix (as today, `nuttx.bin` at block 243) and
   `khicas.paged` (the 3.5 MB text blob) written to its own block range; `up_fillpage()`
   reads it raw, the same way the boot menu reads raw pages.
3. **Linker script.** Place `libcas.a`'s `.text` in the `0x0040_0000` region,
   `-ffunction-sections -fdata-sections -Wl,--gc-sections` so unreferenced giac members
   are dropped, and keep `.data`/`.bss` on the locked side.
4. **The HAL** (see below), ~1000-1500 lines, modelled directly on
   `kcasporing_gl.c` + `khicas_stub.cpp`.
5. **Heap sizing and OOM behaviour.** giac allocates through `malloc`; when the 128 KB
   is gone it must fail cleanly, not corrupt state.

## 4. The port contract is small

Extracting `libcas.libcpp` + `libkcasgui.libcpp` and diffing undefined against defined
symbols leaves **307 external symbols**. Of those, ~120 are libstdc++/libm/libc that
NuttX and the `arm-none-eabi` toolchain already provide, 40 are libtommath (`mp_*`,
source is in ExistOS under `Libs/src`), and the actual platform surface is about 30
functions:

| Group | Symbols | NuttX equivalent |
|---|---|---|
| display | `vGL_SetPoint`, `vGL_GetPoint`, `vGL_putString`, `vGL_FlushVScreen`, `Bdisp_AllClr_VRAM`, `Bdisp_AreaFillVRAM`, `khicas_1bpp` | LCDIF driver + a 4 KB 1bpp backing store |
| keyboard | `GetKey`, `IsKeyDown` | the 11×5 matrix driver, key codes mapped as in `khicas_stub.cpp` |
| files | `Bfile_OpenFile_OS`, `Bfile_ReadFile`, `Bfile_WriteFile_OS`, `Bfile_CreateFile`, `Bfile_CloseFile_OS`, `Bfile_GetFileSize_OS`, `Bfile_DeleteEntry`, `Bfile_FindFirst/Next/Close`, `Bfile_StrToName_ncpy`, `Bfile_NameToStr_ncpy`, `file_exists` | SmartFS on `/mnt`, or a private raw region |
| system | `Sleep`, `RTC_GetTicks`, `RTC_SetDateTime`, `giac::rtc_get_tick_ms`, `SetQuitHandler`, `Setup_GetEntry`, `Setup_SetEntry` | `usleep`, `clock_systime_ticks`, RTC driver, a small settings blob in NAND |

That is the whole "port KhiCAS" surface. Everything else is Giac.

## 5. Phases

| Phase | Deliverable | Acceptance |
|---|---|---|
| **P0** | ✅ measured (§2.1): one-command link is 2.84 MB with tommath, 3.56 MB with libgmp. Next: rebuild from `Libs/src/khicas` with a trimmed command table to find the true floor | a number for "minimum viable giac", and a build script that produces it |
| **P1** | `CONFIG_LEGACY_PAGING` bring-up with a synthetic 3.5 MB blob in the paged window; `up_fillpage()` reads it from a NAND partition | a function living only in the paged window runs; `pg_miss`/`pg_worker` counters show faults and fills; verified in EmuGII first |
| **P2** | Resident/paged image split in the build, the boot menu and the install tool; page-fault rate measurable | KhiCAS' *entry point* executes from the paged window on hardware |
| **P3** | The HAL: display, keyboard, files, time | a KhiCAS splash and the input line appear on the panel |
| **P4** | Heap tuning, OOM behaviour, thrash measurement, `-Os` + `--gc-sections` pass over giac | `diff(x^2*sin(x),x)` and a numeric evaluation run from the keyboard |

Rough shape: P1 is the risky one (it decides whether the whole thing works), P3 is the
bulk of the typing, P0/P2 are mechanical.

## 6. Risks

1. **Page-fault cost.** Every 4 KB of code touched costs a NAND read (2 × 2 KB pages,
   ECC8, DMA). ExistOS lives with this on the same hardware, so it is tolerable — but
   it is why `PAGING_NPPAGED` should start at 24 pages and be tuned, not maximised.
2. **Heap starvation.** giac with 128 KB of heap will fail on large computations.
   ExistOS gives it 288 KB with compression; we cannot match that without paging data
   too. Accept `out of memory` for big inputs, or shrink the CAS feature set.
3. **Flat-build paging semantics.** `CONFIG_LEGACY_PAGING` was written for a single
   task's text, and this port runs NSH, the CAS and idle concurrently. All of them share
   one address space, so a fill must not be allowed to happen while the faulting task's
   context is switched — `pg_miss.c` handles this, but it needs testing under load, not
   just in a two-task smoke test.
4. **I-cache coherency.** ARM926 has a virtually-indexed I-cache: after a fill, the
   page must be invalidated by virtual address, and the D-cache cleaned if the copy went
   through the cache.
5. **The 19-bit alias.** Any pointer that computes an address in the paged window
   (`0x0040_0000`…) as a *physical* address wraps to `0`. All frame addresses must come
   from the frame allocator, never from pointer arithmetic on the virtual address.
6. **C++ static initialisers.** giac has constructors at namespace scope; they run from
   `.init_array` at startup, and `atexit`/`__cxa_atexit` behaviour under NuttX differs
   from `rdimon`. Expect a few of these to need stubs.

## 7. The MMU is on now (was: the blocker for paging)

`CONFIG_LEGACY_PAGING` builds, links and looks healthy, but none of it can ever
run on the current port, and this is worth writing down before anyone spends
another afternoon on it.

Patch 0012 (`boot to NSH on real hardware`) changed `arch/arm/src/arm/arm_nommuhead.S`
to **turn the MMU and the D-cache off and clear the CP15 V bit at reset**, because
the STMP37xx SDP loader leaves CP15 in an unknown state. That is the configuration
the calculator actually runs today.

The consequence for paging:

* `CONFIG_ARCH_USE_MMU=y` is set in the port's Kconfig, so Kconfig happily accepts
  `CONFIG_LEGACY_PAGING=y` (it `depends on ARCH_USE_MMU`).
* But `arch/arm/src/arm/Make.defs:27` selects `HEAD_ASRC = arm_nommuhead.S`
  unconditionally. `arm_head.S` — the only file that enables the MMU and builds the
  initial L1/L2 tables for the paged window — is never assembled, so the edit to its
  L2-clear path does not even reach the object file.
* `arch/arm/src/arm/arm_dataabort.c` and `arm_prefetchabort.c` still call `pg_miss()`
  when a fault address falls inside `PG_PAGED_*`, but with the MMU off there are no
  translation faults, so `up_fillpage()` is never entered.

In other words: with the MMU off, `PG_PAGED_VBASE` is just an address, and the
3.5 MB window is a hole above RAM that cannot be reached at all. A test that calls
into it would take a data/prefetch abort that the handler treats as a real fault
(`PANIC_WITH_REGS`), not a page miss.

### 7.1 What has to happen first

**Status: done.** Patch 0024 enables the MMU from C at the end of `arm_boot()`, after
`stmp3770_copyvectors()` has put the vectors at 0 and with a two-section map (SRAM at 0,
peripherals at `0x8000_0000`). Verified in EmuGII: NSH is pixel-identical to the MMU-off
build and the 100 Hz tick runs. `arm_head.S` is not usable here -- it unmaps the megabyte
section that holds both the code and the page table, and it expects the vector table to be
in the image at 0. Two ARM926 details cost a debug cycle each and are worth remembering:
CP15 `c3` (DACR) resets to 0 = all domains denied, and `CR_V` has to be *cleared*, not just
left alone, because the loader hands over with `V=1`.

The original plan for that milestone, kept for the record:

1. Select `arm_head.S` for this board when paging is enabled (`HEAD_ASRC`), keeping
   `arm_nommuhead.S` for the paging-off configuration so the working build is not
   disturbed.
2. Boot it in EmuGII with the D-cache still off and confirm NSH still comes up. The
   initial mappings come from `arm_head.S` plus the `stmp3770_setupmappings()` call
   that already exists for the `0x8000_0000` peripheral section — that call was
   written when the MMU was on, and is inert today.
3. Only then enable `CONFIG_LEGACY_PAGING` and the NAND-backed `up_fillpage()`.

Work already done for step 3, kept as `paging-wip.patch` (not committed, because it
is dead code until step 1 lands):

* `stmp3770_memorymap.h`: a paging layout — locked 0..`0x3C000`, 24 x 4 KB frames at
  `0x3C000`, 36 x 4 KB heap at `0x54000`, L1 at `0x78000` and L2 coarse tables at
  `0x7C000`; virtual window `0x3C000`..`0x3BC000`.
* `stmp3770_allocateheap.c`: heap is the data region, not `_ebss`..RAM top.
* `hp39gii_fillpage.c`: `up_fillpage()` maps `far - PG_PAGED_VBASE` onto a NAND blob
  and reads one 4 KB page through the GPMI MTD.
* `hp39gii_pagetest.c`: writes 896 four-KB records to NAND, each a three-instruction
  stub returning its own index, then calls every record through the window — 896
  faults against 24 frames, so replacement is exercised too.
* `arm_head.S`: clear the L2 coarse tables at reset; they sit above the 16 KB L1 and
  are not covered by the existing clear loop, so on a cold boot they would otherwise
  contain garbage that looks like valid mappings.

## 7. What is reusable from ExistOS, verbatim

* `Libs/libcas.libcpp`, `libkcasgui.libcpp`, `libgmp.libcpp`, `libtommath.libcpp` — ARM
  EABI5 static archives, soft float, `-marm`, same ABI as this port.
* `Libs/src/khicas/*.cc|h` — the source, if a rebuild with different flags is wanted.
* `kcasporing_gl.c` — 1bpp drawing and console primitives; the framebuffer is a plain
  byte array, so it ports by replacing the flush and the key read.
* `khicas_stub.cpp` — the `Bfile_*` semantics (open modes, `FindFirst/Next` layout,
  name conversion) are worth copying exactly; the ExistOS-specific parts are the
  `SystemUI` calls.

Licensing: giac is GPL-3.0. Anything built here that links it inherits that.
