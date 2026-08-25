#!/usr/bin/env python3
"""
Phase 1 of the "see BIOS over the network" work: pulls the standard x86 BIOS
ROM shadow region (physical 0xF0000-0xFFFFF, 64KiB) over the network from
the ALREADY-RUNNING remote_agent v14 firmware, using its existing READBLOCK
opcode (0x03) -- no firmware changes, no rebuild. That region is where
system BIOS always lives (real BIOS itself executes out of it during POST)
and, per the SMBIOS spec, is also where DMI table entry points get scanned
for -- so one dump gets us both the raw ROM bytes and, if present, decoded
BIOS/system identity (vendor, version, release date, board model) via a
pure-Python SMBIOS parser below. No GPU MMIO involved anywhere in this path.

Usage:
    python dump_and_decode_bios.py
Writes bios_rom_dump.bin alongside this script and prints decoded SMBIOS
info to stdout.
"""

import struct
import sys

import remote_control as rc

BIOS_ROM_BASE = 0xF0000
BIOS_ROM_SIZE = 0x10000  # 64 KiB
OUT_PATH = "bios_rom_dump.bin"


def dump_bios_rom() -> bytes:
    chunk_dwords = rc.MAX_BLOCK_COUNT
    chunk_bytes = chunk_dwords * 4
    out = bytearray()
    offset = 0
    total_calls = (BIOS_ROM_SIZE + chunk_bytes - 1) // chunk_bytes
    call_no = 0
    while offset < BIOS_ROM_SIZE:
        remaining_bytes = BIOS_ROM_SIZE - offset
        count = min(chunk_dwords, (remaining_bytes + 3) // 4)
        call_no += 1
        addr = BIOS_ROM_BASE + offset
        dwords = rc.read_block(addr, count)
        chunk = b"".join(v.to_bytes(4, "little") for v in dwords)
        out.extend(chunk)
        print(f"[{call_no}/{total_calls}] read_block(0x{addr:06x}, {count}) OK", file=sys.stderr)
        offset += count * 4
    return bytes(out[:BIOS_ROM_SIZE])


def fetch_physical(addr: int, length: int) -> bytes:
    """Fetches `length` bytes starting at physical `addr` via read_block,
    for structure tables that land outside the initial BIOS ROM dump."""
    chunk_dwords = rc.MAX_BLOCK_COUNT
    chunk_bytes = chunk_dwords * 4
    out = bytearray()
    offset = 0
    while offset < length:
        remaining_bytes = length - offset
        count = min(chunk_dwords, (remaining_bytes + 3) // 4)
        dwords = rc.read_block(addr + offset, count)
        out.extend(b"".join(v.to_bytes(4, "little") for v in dwords))
        offset += count * 4
    return bytes(out[:length])


def find_smbios_entry(rom: bytes):
    """Scans for the SMBIOS 3.x (_SM3_) or 2.x (_SM_) entry point, both of
    which are spec-mandated to live paragraph-aligned in 0xF0000-0xFFFFF."""
    idx = rom.find(b"_SM3_")
    if idx != -1:
        ep = rom[idx:idx + 24]
        if len(ep) >= 24:
            length = ep[6]
            table_max_size = struct.unpack_from("<I", ep, 12)[0]
            table_addr = struct.unpack_from("<Q", ep, 16)[0]
            return {"version": "3.x", "table_addr": table_addr, "table_len": table_max_size}

    idx = rom.find(b"_SM_")
    if idx != -1:
        ep = rom[idx:idx + 0x1F]
        if len(ep) >= 0x1F:
            major = ep[6]
            minor = ep[7]
            table_len = struct.unpack_from("<H", ep, 0x16)[0]
            table_addr = struct.unpack_from("<I", ep, 0x18)[0]
            return {"version": f"{major}.{minor}", "table_addr": table_addr, "table_len": table_len}

    return None


def parse_smbios_structures(table: bytes):
    """Walks the SMBIOS structure table: each entry is [type u8][len u8]
    [handle u16][formatted area (len-4 bytes)][string set: NUL-terminated
    strings, double-NUL terminates the structure]."""
    structures = []
    pos = 0
    while pos + 4 <= len(table):
        stype = table[pos]
        slen = table[pos + 1]
        if slen < 4:
            break
        handle = struct.unpack_from("<H", table, pos + 2)[0]
        formatted = table[pos:pos + slen]
        str_start = pos + slen
        strings = []
        p = str_start
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
        if stype == 127:  # end-of-table marker
            break
    return structures


def sval(strings, idx):
    if idx == 0 or idx > len(strings):
        return "(none)"
    return strings[idx - 1]


def decode_and_print(structures):
    for s in structures:
        f = s["formatted"]
        if s["type"] == 0 and len(f) >= 3:
            vendor_i, ver_i = f[4] if len(f) > 4 else 0, f[5] if len(f) > 5 else 0
            print("=== BIOS Information (Type 0) ===")
            print("  Vendor:      ", sval(s["strings"], f[4]) if len(f) > 4 else "?")
            print("  Version:     ", sval(s["strings"], f[5]) if len(f) > 5 else "?")
            print("  Release Date:", sval(s["strings"], f[8]) if len(f) > 8 else "?")
        elif s["type"] == 1 and len(f) >= 8:
            print("=== System Information (Type 1) ===")
            print("  Manufacturer:", sval(s["strings"], f[4]))
            print("  Product:     ", sval(s["strings"], f[5]))
            print("  Version:     ", sval(s["strings"], f[6]))
            print("  Serial:      ", sval(s["strings"], f[7]))
        elif s["type"] == 2 and len(f) >= 8:
            print("=== Baseboard Information (Type 2) ===")
            print("  Manufacturer:", sval(s["strings"], f[4]))
            print("  Product:     ", sval(s["strings"], f[5]))
            print("  Version:     ", sval(s["strings"], f[6]))
            print("  Serial:      ", sval(s["strings"], f[7]))


def main():
    print(f"Dumping physical 0x{BIOS_ROM_BASE:06x}-0x{BIOS_ROM_BASE+BIOS_ROM_SIZE-1:06x} "
          f"from the live v14 agent...", file=sys.stderr)
    rom = dump_bios_rom()
    with open(OUT_PATH, "wb") as fh:
        fh.write(rom)
    print(f"Wrote {len(rom)} bytes to {OUT_PATH}", file=sys.stderr)

    entry = find_smbios_entry(rom)
    if entry is None:
        print("No SMBIOS entry point (_SM_/_SM3_) found in 0xF0000-0xFFFFF.")
        return

    print(f"Found SMBIOS entry point, version {entry['version']}, "
          f"table at 0x{entry['table_addr']:x}, length {entry['table_len']}")

    table_addr = entry["table_addr"]
    table_len = entry["table_len"]
    if BIOS_ROM_BASE <= table_addr and table_addr + table_len <= BIOS_ROM_BASE + BIOS_ROM_SIZE:
        table = rom[table_addr - BIOS_ROM_BASE: table_addr - BIOS_ROM_BASE + table_len]
        print("(table already covered by the 0xF0000-0xFFFFF dump)")
    else:
        print(f"Table is outside the initial dump -- fetching 0x{table_addr:x} "
              f"({table_len} bytes) separately...", file=sys.stderr)
        table = fetch_physical(table_addr, table_len)

    structures = parse_smbios_structures(table)
    decode_and_print(structures)


if __name__ == "__main__":
    main()
