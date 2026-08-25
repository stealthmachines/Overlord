#!/usr/bin/env python3
"""
Universal BIOS mapper -- read-only enumeration of everything reachable
through the three primitives remote_agent already proves safe over the
network: read_block() (arbitrary physical memory), pci_config_read()
(PCI config space). No WRITE32, no I/O port writes, no SMI triggers --
"sniffs without scratching." Nothing here is board-specific in method,
only in the values it reports, so it should map any x86 BIOS this agent
can reach, not just this one.

Three independent maps:
  - SMBIOS/DMI: every structure type present, not just BIOS/System/Board
    (dump_and_decode_bios.py only covered types 0/1/2 -- this covers the
    full type table with decoders for the common ones and a raw hex dump
    for anything not specifically decoded).
  - ACPI: RSDP -> RSDT/XSDT -> every table pointed to, with header info
    for all of them and deeper decodes for FADT/MADT/MCFG. FADT surfaces
    SMI_CMD + ACPI_ENABLE/ACPI_DISABLE -- the standard ACPI SMI command
    interface, directly relevant to (but NOT itself exercising) any
    future SMI-based Setup-variable work.
  - PCI: full bus-0 device/function enumeration -- vendor:device, class,
    header type, BARs. Same pci_config_read() opcode already used for
    GPU BAR discovery.

Usage: python bios_mapper.py [--json out.json]
"""

import argparse
import json
import struct
import sys

import remote_control as rc

CHUNK_DWORDS = rc.MAX_BLOCK_COUNT
CHUNK_BYTES = CHUNK_DWORDS * 4


def fetch_physical(addr: int, length: int) -> bytes:
    """Reads `length` bytes starting at physical `addr`, chunked to fit
    MAX_BLOCK_COUNT per read_block() call. Pure read, no side effects."""
    out = bytearray()
    offset = 0
    while offset < length:
        remaining = length - offset
        count = min(CHUNK_DWORDS, (remaining + 3) // 4)
        dwords = rc.read_block(addr + offset, count)
        out.extend(b"".join(v.to_bytes(4, "little") for v in dwords))
        offset += count * 4
    return bytes(out[:length])


# =============================================================================
# SMBIOS -- full structure table, not just types 0/1/2
# =============================================================================

SMBIOS_TYPE_NAMES = {
    0: "BIOS Information", 1: "System Information", 2: "Baseboard Information",
    3: "Chassis Information", 4: "Processor Information", 5: "Memory Controller",
    6: "Memory Module", 7: "Cache Information", 8: "Port Connector",
    9: "System Slots", 10: "On Board Devices", 11: "OEM Strings",
    12: "System Configuration Options", 13: "BIOS Language",
    14: "Group Associations", 15: "System Event Log", 16: "Physical Memory Array",
    17: "Memory Device", 18: "32-bit Memory Error", 19: "Memory Array Mapped Address",
    20: "Memory Device Mapped Address", 21: "Built-in Pointing Device",
    22: "Portable Battery", 23: "System Reset", 24: "Hardware Security",
    25: "System Power Controls", 26: "Voltage Probe", 27: "Cooling Device",
    28: "Temperature Probe", 29: "Electrical Current Probe",
    30: "Out-of-Band Remote Access", 31: "Boot Integrity Services",
    32: "System Boot Information", 33: "64-bit Memory Error",
    34: "Management Device", 38: "IPMI Device", 39: "Power Supply",
    41: "Onboard Device Extended", 42: "Management Controller Host Interface",
    43: "TPM Device", 126: "Inactive", 127: "End-of-Table",
}


def find_smbios_entry(rom: bytes, rom_base: int):
    idx = rom.find(b"_SM3_")
    if idx != -1:
        ep = rom[idx:idx + 24]
        if len(ep) >= 24:
            table_max_size = struct.unpack_from("<I", ep, 12)[0]
            table_addr = struct.unpack_from("<Q", ep, 16)[0]
            return {"version": "3.x", "table_addr": table_addr, "table_len": table_max_size}
    idx = rom.find(b"_SM_")
    if idx != -1:
        ep = rom[idx:idx + 0x1F]
        if len(ep) >= 0x1F:
            major, minor = ep[6], ep[7]
            table_len = struct.unpack_from("<H", ep, 0x16)[0]
            table_addr = struct.unpack_from("<I", ep, 0x18)[0]
            return {"version": f"{major}.{minor}", "table_addr": table_addr, "table_len": table_len}
    return None


def parse_smbios_structures(table: bytes):
    structures = []
    pos = 0
    while pos + 4 <= len(table):
        stype, slen = table[pos], table[pos + 1]
        if slen < 4:
            break
        handle = struct.unpack_from("<H", table, pos + 2)[0]
        formatted = table[pos:pos + slen]
        p = pos + slen
        strings = []
        if p < len(table) and table[p] == 0 and (p + 1 >= len(table) or table[p + 1] == 0):
            end = p + 2 if p + 1 < len(table) else p + 1
        else:
            while p < len(table):
                z = table.find(b"\x00", p)
                if z == -1:
                    p = len(table)
                    break
                s = table[p:z]
                if not s:
                    p = z + 1
                    break
                strings.append(s.decode("latin-1", errors="replace"))
                p = z + 1
            end = p
        structures.append({"type": stype, "handle": handle, "formatted": formatted, "strings": strings})
        pos = end
        if stype == 127:
            break
    return structures


def sval(strings, idx):
    return "(none)" if idx == 0 or idx > len(strings) else strings[idx - 1]


def map_smbios():
    print("=" * 70)
    print("SMBIOS / DMI -- full structure table")
    print("=" * 70)
    rom_base = 0xF0000
    rom = fetch_physical(rom_base, 0x10000)
    entry = find_smbios_entry(rom, rom_base)
    if entry is None:
        print("No SMBIOS entry point found.")
        return {}

    table_addr, table_len = entry["table_addr"], entry["table_len"]
    print(f"Entry point version {entry['version']}, table at 0x{table_addr:x}, length {table_len}")
    if rom_base <= table_addr and table_addr + table_len <= rom_base + len(rom):
        table = rom[table_addr - rom_base: table_addr - rom_base + table_len]
    else:
        table = fetch_physical(table_addr, table_len)

    structures = parse_smbios_structures(table)
    result = {"entry": entry, "structures": []}
    for s in structures:
        name = SMBIOS_TYPE_NAMES.get(s["type"], f"OEM/Unknown type {s['type']}")
        print(f"\n--- Type {s['type']} ({name}), handle 0x{s['handle']:04x}, "
              f"{len(s['formatted'])} bytes formatted ---")
        f = s["formatted"]
        entry_out = {"type": s["type"], "name": name, "handle": s["handle"], "strings": s["strings"]}
        if s["type"] == 0 and len(f) > 8:
            print("  Vendor:      ", sval(s["strings"], f[4]))
            print("  Version:     ", sval(s["strings"], f[5]))
            print("  Release Date:", sval(s["strings"], f[8]))
        elif s["type"] in (1, 2, 3) and len(f) > 7:
            print("  Manufacturer:", sval(s["strings"], f[4]))
            print("  Product:     ", sval(s["strings"], f[5]))
            if s["type"] != 3:
                print("  Version:     ", sval(s["strings"], f[6]))
                print("  Serial:      ", sval(s["strings"], f[7]))
        elif s["type"] == 4 and len(f) > 20:
            print("  Socket:      ", sval(s["strings"], f[4]))
            print("  Manufacturer:", sval(s["strings"], f[7]))
            print("  Version:     ", sval(s["strings"], f[16]))
            print("  Max Speed:   ", struct.unpack_from("<H", f, 20)[0], "MHz")
        elif s["type"] == 17 and len(f) > 21:
            size = struct.unpack_from("<H", f, 12)[0]
            print("  Device Locator:", sval(s["strings"], f[16]))
            print("  Bank Locator:  ", sval(s["strings"], f[17]))
            print("  Size (raw):    ", f"0x{size:04x}")
            if len(f) > 23:
                print("  Speed (MT/s):  ", struct.unpack_from("<H", f, 21)[0])
        elif s["type"] == 11 and len(s["strings"]) > 0:
            for i, st in enumerate(s["strings"], 1):
                print(f"  OEM string {i}: {st}")
        else:
            if s["strings"]:
                print("  Strings:", s["strings"])
        result["structures"].append(entry_out)
    return result


# =============================================================================
# ACPI -- RSDP -> RSDT/XSDT -> every table
# =============================================================================

def find_rsdp():
    # Legacy-spec scan range: 0xE0000-0xFFFFF, 16-byte aligned.
    region = fetch_physical(0xE0000, 0x20000)
    idx = region.find(b"RSD PTR ")
    if idx == -1:
        return None, None
    return 0xE0000 + idx, region[idx:idx + 36]


def read_acpi_table_header(addr: int) -> bytes:
    return fetch_physical(addr, 36)


def decode_fadt(data: bytes):
    out = {}
    if len(data) < 116:
        return out
    out["dsdt"] = struct.unpack_from("<I", data, 40)[0]
    out["sci_int"] = struct.unpack_from("<H", data, 46)[0]
    out["smi_cmd"] = struct.unpack_from("<I", data, 48)[0]
    out["acpi_enable"] = data[52]
    out["acpi_disable"] = data[53]
    out["pm1a_evt_blk"] = struct.unpack_from("<I", data, 56)[0]
    out["pm1a_cnt_blk"] = struct.unpack_from("<I", data, 64)[0]
    out["pm_tmr_blk"] = struct.unpack_from("<I", data, 76)[0]
    return out


def decode_madt(data: bytes):
    entries = []
    pos = 44
    while pos + 2 <= len(data):
        etype, elen = data[pos], data[pos + 1]
        if elen == 0:
            break
        entries.append((etype, elen))
        pos += elen
    return entries


def decode_mcfg(data: bytes):
    segs = []
    pos = 44
    while pos + 16 <= len(data):
        base, seg, bus_start, bus_end = struct.unpack_from("<QHBB", data, pos)
        segs.append({"base": base, "segment": seg, "bus_start": bus_start, "bus_end": bus_end})
        pos += 16
    return segs


def map_acpi():
    print("\n" + "=" * 70)
    print("ACPI -- RSDP -> RSDT/XSDT -> tables")
    print("=" * 70)
    rsdp_addr, rsdp = find_rsdp()
    if rsdp is None:
        print("No RSDP found in 0xE0000-0xFFFFF.")
        return {}

    revision = rsdp[15] if len(rsdp) > 15 else 0
    oem_id = rsdp[9:15].decode("latin-1", errors="replace")
    print(f"RSDP at 0x{rsdp_addr:x}, revision {revision}, OEM '{oem_id}'")

    result = {"rsdp_addr": rsdp_addr, "revision": revision, "oem_id": oem_id, "tables": []}

    table_ptrs = []
    if revision >= 2 and len(rsdp) >= 36:
        xsdt_addr = struct.unpack_from("<Q", rsdp, 24)[0]
        hdr = read_acpi_table_header(xsdt_addr)
        length = struct.unpack_from("<I", hdr, 4)[0]
        full = fetch_physical(xsdt_addr, length)
        count = (length - 36) // 8
        table_ptrs = list(struct.unpack_from(f"<{count}Q", full, 36))
        print(f"XSDT at 0x{xsdt_addr:x}, {count} tables")
    else:
        rsdt_addr = struct.unpack_from("<I", rsdp, 16)[0]
        hdr = read_acpi_table_header(rsdt_addr)
        length = struct.unpack_from("<I", hdr, 4)[0]
        full = fetch_physical(rsdt_addr, length)
        count = (length - 36) // 4
        table_ptrs = list(struct.unpack_from(f"<{count}I", full, 36))
        print(f"RSDT at 0x{rsdt_addr:x}, {count} tables")

    for addr in table_ptrs:
        hdr = read_acpi_table_header(addr)
        if len(hdr) < 36:
            continue
        sig = hdr[0:4].decode("latin-1", errors="replace")
        length = struct.unpack_from("<I", hdr, 4)[0]
        oem_id = hdr[10:16].decode("latin-1", errors="replace")
        print(f"\n--- {sig} at 0x{addr:x}, length {length}, OEM '{oem_id}' ---")
        entry = {"signature": sig, "addr": addr, "length": length, "oem_id": oem_id}

        if sig == "FACP" and length <= 0x1000:
            full = fetch_physical(addr, length)
            fadt = decode_fadt(full)
            for k, v in fadt.items():
                print(f"  {k}: 0x{v:x}")
            entry["fadt"] = fadt
        elif sig == "APIC" and length <= 0x2000:
            full = fetch_physical(addr, length)
            madt = decode_madt(full)
            print(f"  {len(madt)} MADT entries (type,len): {madt[:10]}{'...' if len(madt) > 10 else ''}")
            entry["madt_entries"] = madt
        elif sig == "MCFG" and length <= 0x1000:
            full = fetch_physical(addr, length)
            mcfg = decode_mcfg(full)
            for seg in mcfg:
                print(f"  ECAM base=0x{seg['base']:x} seg={seg['segment']} "
                      f"bus {seg['bus_start']}-{seg['bus_end']}")
            entry["mcfg_segments"] = mcfg

        result["tables"].append(entry)

    return result


# =============================================================================
# PCI -- full bus-0 device/function enumeration via pci_config_read()
# =============================================================================

def map_pci(max_bus: int = 1):
    print("\n" + "=" * 70)
    print("PCI -- bus/device/function enumeration")
    print("=" * 70)
    result = []
    for bus in range(max_bus):
        for dev in range(32):
            for func in range(8):
                try:
                    dw0 = rc.pci_config_read(bus, dev, func, 0x00)
                except Exception as e:
                    print(f"  {bus:02x}:{dev:02x}.{func} read failed: {e}")
                    continue
                vendor = dw0 & 0xFFFF
                device = (dw0 >> 16) & 0xFFFF
                if vendor == 0xFFFF:
                    if func == 0:
                        break
                    continue
                class_dw = rc.pci_config_read(bus, dev, func, 0x08)
                class_code = (class_dw >> 24) & 0xFF
                subclass = (class_dw >> 16) & 0xFF
                hdr_dw = rc.pci_config_read(bus, dev, func, 0x0C)
                hdr_type = (hdr_dw >> 16) & 0x7F
                multi = bool((hdr_dw >> 16) & 0x80)

                bars = []
                if hdr_type == 0x00:
                    for bar_off in range(0x10, 0x28, 4):
                        bars.append(rc.pci_config_read(bus, dev, func, bar_off))

                print(f"  {bus:02x}:{dev:02x}.{func}  {vendor:04x}:{device:04x}  "
                      f"class={class_code:02x}{subclass:02x}  hdr={hdr_type:02x}")
                if bars:
                    bar_str = " ".join(f"0x{b:08x}" for b in bars if b)
                    if bar_str:
                        print(f"           BARs: {bar_str}")

                result.append({
                    "bus": bus, "dev": dev, "func": func,
                    "vendor": vendor, "device": device,
                    "class": class_code, "subclass": subclass,
                    "header_type": hdr_type, "bars": bars,
                })

                if func == 0 and not multi:
                    break
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write full structured results to this path")
    args = ap.parse_args()

    print("Sanity check...")
    print("ping:", rc.ping("bios-mapper-start"))

    smbios = map_smbios()
    acpi = map_acpi()
    pci = map_pci()

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"smbios": smbios, "acpi": acpi, "pci": pci}, f, indent=2)
        print(f"\nWrote {args.json}")


if __name__ == "__main__":
    main()
