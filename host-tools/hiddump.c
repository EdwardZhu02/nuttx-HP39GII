/*
 * hiddump -- read and parse the HID report descriptor of 066f:3770 and print the
 * input/output/feature report size of every report ID.
 *
 * Useful when sbloader-hid cannot decide how the report ID is transported: the
 * descriptor tells you the maximum output report length, and whether the ID is
 * counted in it or not.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/hid/IOHIDKeys.h>
#include <IOKit/hid/IOHIDManager.h>
#include <IOKit/IOKitLib.h>

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static IOHIDDeviceRef g_dev;

static void match_cb(void *ctx, IOReturn res, void *sender, IOHIDDeviceRef dev)
{
    (void)ctx;
    (void)res;
    (void)sender;
    if (!g_dev) {
        g_dev = dev;
        CFRetain(dev);
    }
}

int main(void)
{
    IOHIDManagerRef mgr = IOHIDManagerCreate(kCFAllocatorDefault, kIOHIDOptionsTypeNone);
    int vid = 0x066f, pid = 0x3770;
    CFNumberRef vidRef = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &vid);
    CFNumberRef pidRef = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &pid);
    const void *mk[] = {CFSTR(kIOHIDVendorIDKey), CFSTR(kIOHIDProductIDKey)};
    const void *mv[] = {vidRef, pidRef};
    CFDictionaryRef matching = CFDictionaryCreate(kCFAllocatorDefault, mk, mv, 2,
                                                 &kCFTypeDictionaryKeyCallBacks,
                                                 &kCFTypeDictionaryValueCallBacks);

    IOHIDManagerSetDeviceMatching(mgr, matching);
    IOHIDManagerRegisterDeviceMatchingCallback(mgr, match_cb, NULL);
    IOHIDManagerScheduleWithRunLoop(mgr, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
    IOHIDManagerOpen(mgr, kIOHIDOptionsTypeNone);

    for (int i = 0; i < 60 && !g_dev; i++)
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, true);

    if (!g_dev) {
        fprintf(stderr, "device 066f:3770 not found\n");
        return 1;
    }

    CFTypeRef d = IOHIDDeviceGetProperty(g_dev, CFSTR(kIOHIDReportDescriptorKey));
    if (!d || CFGetTypeID(d) != CFDataGetTypeID()) {
        fprintf(stderr, "no report descriptor property\n");
        return 2;
    }

    CFIndex n = CFDataGetLength((CFDataRef)d);
    const uint8_t *p = CFDataGetBytePtr((CFDataRef)d);

    printf("report descriptor: %ld bytes\n", (long)n);
    for (CFIndex i = 0; i < n; i++) {
        printf("%02x%s", p[i], ((i + 1) % 16 == 0) ? "\n" : " ");
    }
    printf("\n\n");

    /* parse */
    int report_id = 0;
    int report_size = 0, report_count = 0;
    printf("id  kind     bytes\n");
    printf("-------------------\n");

    CFIndex i = 0;
    while (i < n) {
        uint8_t prefix = p[i];
        if (prefix == 0xfe) { /* long item */
            if (i + 1 >= n)
                break;
            i += 3 + p[i + 1];
            continue;
        }
        int bsize = prefix & 3;
        int btype = (prefix >> 2) & 3;
        int btag = (prefix >> 4) & 0xf;
        uint32_t val = 0;

        for (int k = 0; k < bsize && i + 1 + k < n; k++)
            val |= ((uint32_t)p[i + 1 + k]) << (8 * k);

        if (btype == 1) { /* global */
            if (btag == 7)
                report_size = (int)val;
            else if (btag == 8)
                report_id = (int)val;
            else if (btag == 9)
                report_count = (int)val;
        } else if (btype == 0) { /* main */
            if (btag == 8 || btag == 9 || btag == 11) {
                const char *kind = btag == 8 ? "input" : (btag == 9 ? "output" : "feature");
                int bytes = (report_size * report_count) / 8;
                printf("%-3d %-8s %d%s\n", report_id, kind, bytes,
                       (val & 1) ? "" : "  (const/arr)");
            }
        }

        i += 1 + bsize;
    }

    return 0;
}
