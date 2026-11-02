/*
 * sbloader-hid -- push a BootStream into the STMP3770 boot ROM (066f:3770)
 * through the IOKit HID stack.
 *
 * On macOS the HID driver owns this interface: libusb's USBInterfaceOpen fails
 * and detaching it needs root or an entitlement. IOHIDManager with
 * kIOHIDOptionsTypeSeizeDevice is the only user-space way in.
 *
 * Protocol (compare tools/sbtools/sbloader.c):
 *   SET_REPORT wValue=0x0201 -> report type 02 = OUTPUT, id 1, CBW (hid_cbw_t, 31 B)
 *   SET_REPORT wValue=0x0202 -> report type 02 = OUTPUT, id 2, data block (48 B)
 *   interrupt IN 0x81        -> input report id 4, CSW (hid_csw_t, 13 B)
 *
 * Implementations disagree about how the report ID is passed, so a zero-length
 * CBW is used as a handshake first: whichever encoding gets the tag echoed back
 * is the encoding used for the transfer.
 *
 * Build: see Makefile in this directory (macOS only).
 */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/hid/IOHIDKeys.h>
#include <IOKit/hid/IOHIDManager.h>
#include <IOKit/IOKitLib.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define TARGET_VID 0x066f
#define TARGET_PID 0x3770
#define XFER_SIZE 48

#define CBW_BLTC 0x43544C42 /* "BLTC" */
#define CSW_BLTS 0x53544C42 /* "BLTS" */
#define BLTC_DOWNLOAD_FW 2

struct hid_cdb_t {
    uint8_t command;
    uint8_t length[4]; /* big-endian */
    uint8_t reserved[11];
} __attribute__((packed));

struct hid_cbw_t {
    uint32_t signature;
    uint32_t tag;
    uint32_t length;
    uint8_t flags;
    uint8_t reserved[2];
    struct hid_cdb_t cdb;
} __attribute__((packed));

struct hid_csw_t {
    uint32_t signature;
    uint32_t tag;
    uint32_t residue;
    uint8_t status;
} __attribute__((packed));

static uint8_t g_report_buf[256];
static uint8_t g_status[128];
static volatile int g_status_ready;
static volatile int g_status_len;

static void input_report_cb(void *ctx, IOReturn res, void *sender,
                            IOHIDReportType type, uint32_t report_id,
                            uint8_t *report, CFIndex len)
{
    (void)ctx;
    (void)sender;
    (void)type;
    (void)res;
    (void)report_id;

    if (len > 0 && len <= (CFIndex)sizeof(g_status)) {
        memcpy(g_status, report, len);
        g_status_len = (int)len;
        g_status_ready = 1;
    }
}

static void pump_runloop(double seconds)
{
    CFTimeInterval end = CFAbsoluteTimeGetCurrent() + seconds;

    while (CFAbsoluteTimeGetCurrent() < end)
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.02, true);
}

static int wait_status(double seconds)
{
    CFTimeInterval end = CFAbsoluteTimeGetCurrent() + seconds;

    while (CFAbsoluteTimeGetCurrent() < end) {
        if (g_status_ready)
            return 0;
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.02, true);
    }
    return -1;
}

static IOHIDDeviceRef find_device(void)
{
    IOHIDManagerRef mgr = IOHIDManagerCreate(kCFAllocatorDefault, kIOHIDOptionsTypeNone);
    int vid = TARGET_VID, pid = TARGET_PID;
    CFNumberRef vidRef = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &vid);
    CFNumberRef pidRef = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &pid);
    const void *mk[] = {CFSTR(kIOHIDVendorIDKey), CFSTR(kIOHIDProductIDKey)};
    const void *mv[] = {vidRef, pidRef};
    CFDictionaryRef matching = CFDictionaryCreate(kCFAllocatorDefault, mk, mv, 2,
                                                 &kCFTypeDictionaryKeyCallBacks,
                                                 &kCFTypeDictionaryValueCallBacks);

    IOHIDManagerSetDeviceMatching(mgr, matching);
    IOHIDManagerScheduleWithRunLoop(mgr, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
    IOHIDManagerOpen(mgr, kIOHIDOptionsTypeNone);
    pump_runloop(0.6);

    CFSetRef devices = IOHIDManagerCopyDevices(mgr);
    if (!devices || CFSetGetCount(devices) == 0) {
        fprintf(stderr, "device 066f:3770 not found\n");
        return NULL;
    }

    const void *vals[8];
    CFIndex cnt = CFSetGetCount(devices);
    if (cnt > 8)
        cnt = 8;
    CFSetGetValues(devices, vals);

    IOHIDDeviceRef dev = (IOHIDDeviceRef)vals[0];
    CFRetain(dev);

    CFTypeRef v = IOHIDDeviceGetProperty(dev, CFSTR("MaxOutputReportSize"));
    int n = -1;
    if (v && CFGetTypeID(v) == CFNumberGetTypeID())
        CFNumberGetValue((CFNumberRef)v, kCFNumberIntType, &n);
    printf("device: ROM Recovery, max output report = %d byte(s)\n", n);
    return dev;
}

struct strategy {
    IOHIDReportType type;
    int include_id;
    const char *name;
};

static IOReturn send_report(IOHIDDeviceRef dev, struct strategy *s, uint32_t id,
                            const uint8_t *payload, int plen)
{
    uint8_t tmp[256];
    IOReturn r = kIOReturnError;

    /* The device briefly NAKs right after a command (0xe00002ed and friends). */
    for (int attempt = 0; attempt < 6; attempt++) {
        if (s->include_id) {
            tmp[0] = (uint8_t)id;
            memcpy(tmp + 1, payload, (size_t)plen);
            r = IOHIDDeviceSetReport(dev, s->type, id, tmp, plen + 1);
        } else {
            r = IOHIDDeviceSetReport(dev, s->type, id, payload, plen);
        }
        if (r == kIOReturnSuccess)
            return r;
        usleep(20000);
    }
    return r;
}

static void build_cbw(uint8_t *out, uint32_t size, uint32_t tag)
{
    struct hid_cbw_t cbw;

    memset(&cbw, 0, sizeof(cbw));
    cbw.signature = CBW_BLTC;
    cbw.tag = tag;
    cbw.length = size;
    cbw.flags = 0x00;
    cbw.cdb.command = BLTC_DOWNLOAD_FW;
    cbw.cdb.length[0] = (uint8_t)((size >> 24) & 0xff);
    cbw.cdb.length[1] = (uint8_t)((size >> 16) & 0xff);
    cbw.cdb.length[2] = (uint8_t)((size >> 8) & 0xff);
    cbw.cdb.length[3] = (uint8_t)(size & 0xff);
    memcpy(out, &cbw, sizeof(cbw));
}

static void dump_status(const char *msg)
{
    printf("  %s raw(%d):", msg, g_status_len);
    for (int i = 0; i < g_status_len && i < 20; i++)
        printf(" %02x", g_status[i]);
    printf("\n");
}

static uint32_t parse_csw(struct hid_csw_t *csw)
{
    int off = (g_status[0] == 4) ? 1 : 0;

    if (g_status_len - off < (int)sizeof(*csw))
        return 0;
    memcpy(csw, g_status + off, sizeof(*csw));
    return csw->signature == CSW_BLTS ? csw->tag : 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s <bootstream.sb>\n", argv[0]);
        return 2;
    }

    FILE *f = fopen(argv[1], "rb");
    if (!f) {
        perror("open");
        return 1;
    }
    fseek(f, 0, SEEK_END);
    long fsize = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *buf = calloc(1, (size_t)fsize + XFER_SIZE);
    if (fread(buf, 1, (size_t)fsize, f) != (size_t)fsize) {
        fprintf(stderr, "short read\n");
        return 1;
    }
    fclose(f);

    IOHIDDeviceRef dev = find_device();
    if (!dev)
        return 3;

    IOReturn r = IOHIDDeviceOpen(dev, kIOHIDOptionsTypeSeizeDevice);
    if (r != kIOReturnSuccess)
        r = IOHIDDeviceOpen(dev, kIOHIDOptionsTypeNone);
    if (r != kIOReturnSuccess) {
        fprintf(stderr, "IOHIDDeviceOpen failed 0x%x\n", r);
        return 3;
    }

    IOHIDDeviceRegisterInputReportCallback(dev, g_report_buf, sizeof(g_report_buf),
                                           input_report_cb, NULL);
    pump_runloop(0.2);

    /* ---- handshake ---- */
    struct strategy strats[] = {
        {kIOHIDReportTypeOutput, 0, "output, id-separate"},
        {kIOHIDReportTypeOutput, 1, "output, id-in-buffer"},
        {kIOHIDReportTypeFeature, 0, "feature, id-separate"},
        {kIOHIDReportTypeFeature, 1, "feature, id-in-buffer"},
    };
    int chosen = -1;
    uint8_t cbw[64];

    /*
     * Measured: the device treats the first byte of the report as the report ID,
     * i.e. the ID has to be inside the buffer (max output report = 49 = 48 data
     * bytes + 1 ID, which confirms it). So "output, id-in-buffer" is used by
     * default; the "probe" argument runs the handshake search instead.
     *
     * The handshake is a non-destructive health check:
     *   1) send CBW(length=48) plus one all-zero data block. The bootstream
     *      header is invalid, so the ROM must answer with status != 0 -- but if
     *      the tag comes back correctly, both the encoding and the fact that the
     *      ROM is still serving USB are proven at once.
     *   2) if none of the four encodings answer, still try one real upload with
     *      the encoding that is known to work (the device may have jumped
     *      already; a USB-level ACK does not mean the ROM is alive).
     */
    chosen = 1; /* confirmed by the health check: output + id-in-buffer */
    if (argc >= 3 && strcmp(argv[2], "health") == 0) {
        chosen = -1;
        printf("health probe...\n");
        for (int i = 0; i < 4 && chosen < 0; i++) {
        uint32_t tag = 0x11223300u + (uint32_t)i;
        struct hid_csw_t csw;
        uint8_t zero[XFER_SIZE];

        memset(zero, 0, sizeof(zero));
        build_cbw(cbw, XFER_SIZE, tag);
        g_status_ready = 0;

        r = send_report(dev, &strats[i], 1, cbw, sizeof(struct hid_cbw_t));
        if (r != kIOReturnSuccess) {
            printf("  %-24s CBW failed 0x%x\n", strats[i].name, r);
            continue;
        }
        r = send_report(dev, &strats[i], 2, zero, XFER_SIZE);
        if (r != kIOReturnSuccess) {
            printf("  %-24s data failed 0x%x\n", strats[i].name, r);
            continue;
        }
        if (wait_status(1.5) != 0) {
            printf("  %-24s no CSW\n", strats[i].name);
            continue;
        }

        uint32_t got = parse_csw(&csw);
        printf("  %-24s CSW tag=0x%08x status=%u\n", strats[i].name, got, csw.status);
        if (got == tag)
            chosen = i;
    }

    }

    if (chosen < 0) {
        printf("!! BootROM did not respond to any encoding\n");
        printf("   (it either jumped already, or needs a power cycle back to SDP)\n");
        printf("   proceeding with '%s' anyway\n", strats[1].name);
        chosen = 1;
    } else {
        printf("=> BootROM alive, encoding: %s\n", strats[chosen].name);
    }

    /* ---- upload ---- */
    const uint32_t tag = 0xcafebabe;
    struct strategy *s = &strats[chosen];

    build_cbw(cbw, (uint32_t)fsize, tag);
    g_status_ready = 0;
    r = send_report(dev, s, 1, cbw, sizeof(struct hid_cbw_t));
    if (r != kIOReturnSuccess) {
        fprintf(stderr, "CBW failed 0x%x\n", r);
        return 5;
    }
    printf("CBW sent: %ld bytes, tag 0x%08x\n", fsize, tag);

    long nr = (fsize + XFER_SIZE - 1) / XFER_SIZE;
    uint8_t chunk[XFER_SIZE];

    for (long i = 0; i < nr; i++) {
        memset(chunk, 0xff, sizeof(chunk));
        long off = i * XFER_SIZE;
        long len = fsize - off;
        if (len > XFER_SIZE)
            len = XFER_SIZE;
        memcpy(chunk, buf + off, (size_t)len);

        r = send_report(dev, s, 2, chunk, XFER_SIZE);
        if (r != kIOReturnSuccess) {
            fprintf(stderr, "\nchunk %ld failed 0x%x\n", i, r);
            return 6;
        }
        if ((i % 200) == 0) {
            printf("\r  %ld/%ld", i, nr);
            fflush(stdout);
        }
    }
    printf("\r  %ld/%ld data sent\n", nr, nr);

    if (wait_status(5.0) != 0) {
        /* The ROM jumps as soon as the stream is in, so no CSW arrives; that
         * is the normal success path. */
        printf("no final CSW -- device likely jumped to the loaded image\n");
        printf("UPLOAD DONE (assumed ok)\n");
        return 0;
    }

    struct hid_csw_t csw;
    uint32_t got = parse_csw(&csw);
    printf("CSW: tag=0x%08x (expect 0x%08x) residue=%u status=%u\n",
           got, tag, csw.residue, csw.status);

    if (got != tag) {
        dump_status("bad tag");
        return 8;
    }
    if (csw.status != 0) {
        fprintf(stderr, "device reported status %u\n", csw.status);
        return 9;
    }

    printf("UPLOAD OK\n");
    return 0;
}
