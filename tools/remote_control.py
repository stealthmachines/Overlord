#!/usr/bin/env python3
"""
Client for the bare-metal REMOTE_AGENT command loop running on the real
RTL8168 (B450M DS3H box). Same EtherType (0x88B5) and interface as every
other probe in this project.

Wire protocol (all multi-byte fields little-endian, matching remote_agent's
own asm -- no byte-swapping needed on either end):
  byte 0 = opcode
    0x00 PING:    remainder = arbitrary bytes; reply echoes "ECHO:"+bytes
                  with opcode 0x80
    0x01 READ32:  bytes 1-4 = target physical address
                  reply: opcode 0x81, bytes1-4=address, bytes5-8=value read
    0x02 WRITE32: bytes 1-4 = address, bytes 5-8 = value to write
                  reply: opcode 0x82, bytes1-4=address, bytes5-8=value
                         read back AFTER the write (what's actually there)

Usage:
    from remote_control import read32, write32, ping
    val = read32(0x02002500)             # NVS295 PFIFO.CACHE1_PUSH0, e.g.
    write32(0x02002500, 1)                # PFIFO safe-list address only!
    ping("hello")

IMPORTANT: WRITE32 is not restricted in firmware to any address range --
this script is the only thing standing between "convenient" and "writing
to something we haven't vetted." Only ever target addresses already on
this project's established safe-list (NVS295 PFIFO 0x2000-0x2800,
PDISPLAY0 0x610000-0x620000, all offset from the GPU's real BAR0 base
0xFA000000 -- so e.g. PFIFO.CACHE1_PUSH0 is 0xFA000000+0x2500) unless a
new address has been separately, deliberately reviewed first.
"""

import struct
import time

from scapy.all import Ether, Raw, sendp, sniff

IFACE = "Ethernet 8"
ETHERTYPE = 0x88B5
TIMEOUT = 1.5          # per-attempt wait; short, because most replies land in ms
RETRIES = 4            # total attempts before giving up -- covers transient
                       # dropped frames during bulk/high-volume operations
                       # (e.g. a full-aperture sweep) without masking a
                       # genuinely dead agent: RETRIES failures in a row
                       # still raises, it just doesn't crash on one blip

# The GPU's confirmed real BAR0 base (from this project's earlier probes).
NVS295_BAR0 = 0xFA000000

MAX_BLOCK_COUNT = 368  # must match remote_agent's MAX_BLOCK_COUNT (stage2_10.asm)

# 2026-08-23 incident: a plain READ around BAR0 offset 0x60B000 hung the
# WHOLE machine (ping() itself stopped answering, not just the GPU
# register interface) -- recovered only by a physical power-cycle. This is
# the canonical definition of that hazard, in BAR0-relative offsets;
# fast_sweep.py imports it from here rather than keeping its own copy, so
# the two can't drift out of sync. read_block() below refuses any request
# whose span overlaps it AT ALL, not just requests starting inside it --
# a bulk read has all-or-nothing blast radius across its whole span, so a
# request that starts just before the hazard and extends into it is just
# as dangerous as one starting inside it.
#
# SECOND incident, same session, same day: full_dense_scan.py's
# READ_BLOCK sweep of previously-never-touched territory (0x0-0x600000 had
# only ever seen sparse single-dword probing in much earlier rounds, never
# a dense sweep) hung the whole machine again -- ping() dead again, same
# recovery (physical power-cycle) required. Last confirmed-good read was
# 0x1C6BFC=0; the failing request covered the WHOLE 256-dword chunk
# 0x1C6C00-0x1C6FFC (1KB), and unlike the single-dword READ32 case, there
# is no finer localization than that -- a bulk read's failure doesn't tell
# you which of its 256 addresses was the actual trigger. Confirms this is
# a real, not one-off, class of risk: plain reads into never-swept GPU
# MMIO territory on this board can hang the CPU, not just writes -- true
# whether reading one dword at a time or 256 at once.
#
# THIRD incident, immediately after: resumed the scan at exactly 0x1C7000
# (the boundary right after the above range) after a fresh power-cycle +
# reflash + confirmed-alive ping(). The VERY FIRST request at that
# boundary hung the machine again (ping() dead again). That means the
# narrow 1KB exclusion above was NOT wide enough -- this whole
# neighborhood is more sensitive than one chunk, not just one unlucky
# address next to safe territory. Rather than keep finding this region's
# true edge by repeatedly crashing the machine, marking a much wider
# buffer around it and deferring finer localization to a deliberate,
# single-dword, human-supervised approach if it's ever worth pinning down
# further -- NOT another automated bulk sweep.
# 2026-08-23, later same session: remote_agent_4.img added a full IDT +
# exception handlers for all 32 CPU vectors + CR4.MCE (see stage2_4.asm).
# Deliberately re-read EVERY ONE of the four ranges below afterward (0x400
# stride across each full range) specifically to test whether this fixes
# the hangs -- ALL FOUR came back completely clean, machine stayed fully
# responsive (ping confirmed) throughout and after. This strongly
# validates the fix: catching whatever fault a bad read raises instead of
# letting the CPU triple-fault. HAZARD_RANGES is kept below purely as a
# historical record of what was found and how -- nothing in this file
# skips these anymore (see RESOLVED, cleared below). If a genuinely NEW
# hang ever occurs again with remote_agent_4.img or later running, that
# would mean the fix doesn't cover every failure mode, and hazard-skipping
# should be reinstated for whatever new range is found.
_RESOLVED_HAZARD_HISTORY = [
    (0x60B000, 0x610000),   # incident #1
    (0x1C0000, 0x200000),   # widened after a second hang right at the
                            # narrow range's boundary -- treat this whole
                            # 256KB neighborhood as unsafe until deliberately
                            # investigated, not just the one failing chunk
    (0xED9800, 0xEDA000),   # FOURTH incident (2026-08-23, descending scan
                            # from the top of the aperture): last good read
                            # 0xEDA3FC, failing chunk 0xED9C00-0xEDA000.
                            # Widened by one extra chunk below the failure
                            # (same lesson as incident #3 -- a narrow
                            # exclusion undersold the true edge last time).
                            # Different neighborhood entirely from the
                            # other two hazards -- this is looking like a
                            # recurring property of unswept territory on
                            # this board, not one or two unlucky spots.
    (0xE60000, 0xE61000),   # FIFTH incident (2026-08-23, same descending
                            # scan, resumed past incident #4): last good
                            # read ended 0xE60C00, failing chunk
                            # 0xE60800-0xE60C00. ~1.5MB from incident #4,
                            # a completely distinct location again. Five
                            # incidents now, scattered across the aperture
                            # -- confirms this is a distributed property of
                            # unswept territory on this board, not a
                            # handful of unlucky addresses.
]

# SIXTH incident, remote_agent_4.img (2026-08-23): a full fresh-aperture
# scan swept cleanly through 0x0-0xa40000 (64.5%, including ALL FOUR of
# the ranges above with zero issues -- the exception-handling fix clearly
# works for whatever failure mode caused those). Then hung again in NEW
# territory: last good read 0xa5c3fc, failing chunk 0xa5c400-0xa5c800.
# ping() confirmed a genuine hang (dead), same as before the fix. This
# means there are at least TWO distinct failure modes on this board: one
# that raises a catchable CPU exception (now handled), and at least one
# that's a true hardware-level bus stall no software exception handler
# can trap (the CPU is stuck inside the faulting instruction itself,
# never reaching a point where it could raise anything) -- likely a PCIe
# Completion Timeout misconfiguration (disabled/unbounded), a different,
# deeper fix than an IDT.
# NOTE: this single list is shared by full_dense_scan.py's `base` parameter
# regardless of which aperture is being scanned (BAR0 vs BAR3) -- offsets
# below are NOT necessarily comparable between apertures, this is a
# simplification made because BAR0 and BAR3 scanning haven't overlapped in
# time so far. If both are ever resumed concurrently, split this into
# per-base lists to avoid a false skip or a missed one.
HAZARD_RANGES: list[tuple[int, int]] = [
    (0xA50000, 0xA60000),   # BAR0 -- widened after resuming right at the
                            # narrow range's boundary (0xA5C800) hung the
                            # machine again on the very first request --
                            # same lesson as incident #3: a narrow
                            # exclusion undersold the true edge. Second
                            # hit of the "true hardware bus stall" class.
    (0xBA0000, 0xC20000),   # BAR3 (0xF8000000) -- EIGHTH incident (first on
                            # BAR3): last good read 0xBB9FFC, failing chunk
                            # 0xBBA000-0xBBA400. NINTH incident: resuming
                            # right at that widened boundary (0xBC0000)
                            # hung again at 0xC13400-0xC13800 -- same
                            # lesson as incidents #3/#7, a narrow exclusion
                            # undersold the true edge again. Widened to
                            # cover the whole apparent danger neighborhood
                            # (0xBA0000-0xC20000, ~360KB) rather than
                            # inching forward and re-crashing repeatedly.
    (0x101f000, 0x1021000),  # TWELFTH incident: resuming descending from
                             # 0x1306000 got 43.2% through (down to
                             # 0x1020000) before hanging again just below.
    (0x1306000, 0x1307000),  # ELEVENTH incident: descending scan from top
                             # of BAR3 got 65.9% through cleanly (0x2000000
                             # down to 0x1310000) before hitting this --
                             # last good read 0x1306BFC, failing chunk
                             # 0x1306800-0x1306C00, widened by a margin.
    (0xC50000, 0xC60000),   # TENTH incident: resuming past the above (at
                            # 0xC20000) ran cleanly through 0xC50000, THEN
                            # hit a separate, closely-spaced landmine --
                            # last good read 0xC53BFC, failing chunk
                            # 0xC53C00-0xC54000. NOT a resume-boundary
                            # issue this time (30KB of clean reads happened
                            # first) -- this whole BAR3 neighborhood
                            # (0xBA0000-0xC60000, ~1.4MB) looks genuinely
                            # landmine-dense rather than a few isolated
                            # spots. Trying a descending approach (top of
                            # BAR3 downward) for the rest of BAR3 next,
                            # same strategy that worked for the remainder
                            # of BAR0 earlier this session.
]


def in_hazard(offset: int) -> bool:
    return any(lo <= offset < hi for lo, hi in HAZARD_RANGES)


def range_in_hazard(offset: int, count: int) -> bool:
    span_end = offset + count * 4  # exclusive
    return any(lo < span_end and offset < hi for lo, hi in HAZARD_RANGES)


def _send_and_wait(payload: bytes, expect_opcode: int) -> bytes:
    frame = Ether(dst="ff:ff:ff:ff:ff:ff", type=ETHERTYPE) / Raw(payload)

    last_error = None
    for attempt in range(RETRIES):
        result = {}

        def _handler(pkt):
            if Raw not in pkt:
                return
            data = bytes(pkt[Raw].load)
            if data and data[0] == expect_opcode:
                result["data"] = data

        sendp(frame, iface=IFACE, verbose=False)
        sniff(iface=IFACE, filter="ether proto 0x88b5", timeout=TIMEOUT,
              stop_filter=lambda p: "data" in result, prn=_handler)

        if "data" in result:
            return result["data"]
        last_error = f"no reply (opcode 0x{expect_opcode:02x}) on attempt {attempt+1}/{RETRIES}"

    raise TimeoutError(
        f"No reply (opcode 0x{expect_opcode:02x}) after {RETRIES} attempts "
        f"({RETRIES*TIMEOUT:.1f}s total) -- last: {last_error}"
    )


def read32(addr: int) -> int:
    """Reads one 32-bit value from the given physical address on the
    bare-metal box. Always safe -- read-only."""
    payload = bytes([0x01]) + struct.pack("<I", addr)
    reply = _send_and_wait(payload, expect_opcode=0x81)
    got_addr, value = struct.unpack("<II", reply[1:9])
    if got_addr != addr:
        raise ValueError(f"address mismatch: sent 0x{addr:08x}, got 0x{got_addr:08x}")
    return value


def read_block(addr: int, count: int) -> list[int]:
    """Reads `count` consecutive dwords starting at `addr` in ONE network
    round-trip (opcode 0x03, added 2026-08-23 specifically because one-
    dword-per-round-trip was the actual throughput bottleneck for any
    dense/graphical read of the GPU's address space -- not the analysis
    method). count is clamped server-side to MAX_BLOCK_COUNT; this raises
    if the reply's echoed count doesn't match what was requested, so a
    silent clamp can never masquerade as a full read.

    ALL-OR-NOTHING, unlike read32() retried one dword at a time: if any
    address in [addr, addr+count*4) wedges the bus, nothing comes back at
    all. Refuses outright if the requested span overlaps HAZARD_RANGES
    anywhere, not just at its start address."""
    if count > MAX_BLOCK_COUNT:
        raise ValueError(f"count={count} exceeds MAX_BLOCK_COUNT={MAX_BLOCK_COUNT}")
    offset = addr - NVS295_BAR0
    if range_in_hazard(offset, count):
        raise RuntimeError(
            f"refusing read_block(0x{addr:08x}, {count}) -- span overlaps a "
            f"known hazard range {HAZARD_RANGES} (BAR0-relative). This range "
            f"hung the whole machine once already; needs deliberate, "
            f"supervised single-stepping, not a bulk read."
        )
    payload = bytes([0x03]) + struct.pack("<IH", addr, count)
    reply = _send_and_wait(payload, expect_opcode=0x83)
    got_addr, got_count = struct.unpack("<IH", reply[1:7])
    if got_addr != addr:
        raise ValueError(f"address mismatch: sent 0x{addr:08x}, got 0x{got_addr:08x}")
    if got_count != count:
        raise ValueError(f"count mismatch: sent {count}, got {got_count} (server-side clamp?)")
    data = reply[7:7 + count * 4]
    if len(data) < count * 4:
        raise ValueError(f"short reply: expected {count*4} data bytes, got {len(data)}")
    return list(struct.unpack(f"<{count}I", data))


def write32(addr: int, value: int) -> int:
    """Writes one 32-bit value to the given physical address on the
    bare-metal box, and returns what was actually read back afterward.
    NOT restricted in firmware -- see the module docstring. Only target
    addresses already vetted safe."""
    payload = bytes([0x02]) + struct.pack("<II", addr, value)
    reply = _send_and_wait(payload, expect_opcode=0x82)
    got_addr, readback = struct.unpack("<II", reply[1:9])
    if got_addr != addr:
        raise ValueError(f"address mismatch: sent 0x{addr:08x}, got 0x{got_addr:08x}")
    return readback


def pci_config_read(bus: int, device: int, function: int, register: int) -> int:
    """Reads one 32-bit PCI config-space dword via remote_agent_3.img's
    PCI_CONFIG_READ opcode (0x04) -- reuses the same pci_read32 the
    firmware's own NIC scan already relies on. Added 2026-08-23 to find
    the GPU's real VRAM aperture base address (BAR2/BAR3 config offsets
    0x18/0x1C) before ever writing image content into VRAM -- always
    safe, config-space reads have no write side effects."""
    payload = bytes([0x04, bus & 0xFF, device & 0xFF, function & 0xFF, register & 0xFF])
    reply = _send_and_wait(payload, expect_opcode=0x84)
    got_bus, got_dev, got_func, got_reg = reply[1], reply[2], reply[3], reply[4]
    if (got_bus, got_dev, got_func, got_reg) != (bus & 0xFF, device & 0xFF, function & 0xFF, register & 0xFF):
        raise ValueError("PCI config read echo mismatch")
    return struct.unpack("<I", reply[5:9])[0]


def disk_get_params(drive: int) -> dict | None:
    """Queries BIOS drive parameters (INT13h AH=0x48) for a given drive
    number (0x80, 0x81, ...) via remote_agent_5.img's DISK_GET_PARAMS
    opcode (0x05). Read-only, always safe -- BIOS function 0x48 doesn't
    write anything. Returns None if the drive doesn't exist (BIOS
    reported an error), else a dict with total_sectors and
    bytes_per_sector. Verified in QEMU against a known 1MB test image
    before ever running against real hardware: total_sectors=0x800 (2048),
    bytes_per_sector=0x200 (512) -- exactly matched the real file size."""
    payload = bytes([0x05, drive & 0xFF])
    reply = _send_and_wait(payload, expect_opcode=0x85)
    got_drive, carry = reply[1], reply[2]
    ax, total_sectors, bytes_per_sector = struct.unpack("<HQH", reply[3:15])
    if got_drive != (drive & 0xFF):
        raise ValueError("drive echo mismatch")
    if carry:
        return None
    return {
        "drive": drive,
        "ax": ax,
        "total_sectors": total_sectors,
        "bytes_per_sector": bytes_per_sector,
        "total_bytes": total_sectors * bytes_per_sector,
    }


STAGE_BUF_SIZE = 512 * 512     # 256KB -- must match stage2_10.asm's STAGE_BUF_SIZE
MAX_STAGE_DWORDS = 368          # per STAGE_WRITE_BLOCK call (1472 bytes)
MAX_WRITE_SECTORS = 512         # per DISK_WRITE_SECTORS call = 256KB


def stage_write_block(offset: int, data: bytes) -> None:
    """Uploads `data` (must be a multiple of 4 bytes, <=1024) into the
    agent's 64KB staging buffer at `offset`, via opcode 0x06. Pure RAM
    copy -- does not touch disk. Call this (possibly many times) to
    assemble a full chunk before calling disk_write_sectors()."""
    if len(data) % 4 != 0:
        raise ValueError("data length must be a multiple of 4 bytes")
    count = len(data) // 4
    if count > MAX_STAGE_DWORDS:
        raise ValueError(f"count={count} exceeds MAX_STAGE_DWORDS={MAX_STAGE_DWORDS}")
    payload = bytes([0x06]) + struct.pack("<IH", offset, count) + data
    reply = _send_and_wait(payload, expect_opcode=0x86)
    got_offset, got_count = struct.unpack("<IH", reply[1:7])
    if got_offset != offset or got_count != count:
        raise ValueError(f"stage echo mismatch: sent ({offset},{count}), got ({got_offset},{got_count})")


def stage_write_bulk(data: bytes) -> None:
    """Convenience: splits `data` (<=STAGE_BUF_SIZE) into MAX_STAGE_DWORDS-
    sized chunks and stages all of them, filling the staging buffer from
    offset 0. Pads the final chunk to a 4-byte boundary with zeros if
    needed (harmless -- only matters if the caller writes fewer than a
    whole number of sectors, in which case they should size `data` to a
    512-byte multiple anyway before calling disk_write_sectors)."""
    if len(data) > STAGE_BUF_SIZE:
        raise ValueError(f"data length {len(data)} exceeds STAGE_BUF_SIZE={STAGE_BUF_SIZE}")
    chunk_bytes = MAX_STAGE_DWORDS * 4
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + chunk_bytes]
        if len(chunk) % 4 != 0:
            chunk = chunk + b"\x00" * (4 - len(chunk) % 4)
        stage_write_block(offset, chunk)
        offset += chunk_bytes


def disk_write_sectors(drive: int, lba: int, sector_count: int) -> dict:
    """Commits whatever is currently in the agent's staging buffer (from
    its start) to real disk via BIOS INT13h AH=0x43, at (drive, lba), for
    sector_count sectors (<=MAX_WRITE_SECTORS=128=64KB). THIS IS THE ONLY
    OPERATION IN THIS PROJECT THAT WRITES TO PERSISTENT STORAGE.

    Verified in QEMU before ever being used against real hardware: staged
    a known pattern, committed it to LBA 100, read it back via a SEPARATE
    BIOS call into a different buffer, and confirmed byte-for-byte match
    -- independently re-verified by reading the raw disk image file at
    the expected byte offset. Real-mode round-trip bugs (missing `sti`,
    and IDTR not reverting when dropping from protected to real mode)
    were caught and fixed in that same testing, not discovered here.

    Caller MUST stage the right data first via stage_write_block() /
    stage_write_bulk() -- this only commits whatever's already staged."""
    if sector_count > MAX_WRITE_SECTORS:
        raise ValueError(f"sector_count={sector_count} exceeds MAX_WRITE_SECTORS={MAX_WRITE_SECTORS}")
    payload = bytes([0x07, drive & 0xFF]) + struct.pack("<QH", lba, sector_count)
    reply = _send_and_wait(payload, expect_opcode=0x87)
    got_drive = reply[1]
    got_lba, got_count = struct.unpack("<QH", reply[2:12])
    carry, ax = struct.unpack("<BH", reply[12:15])
    if got_drive != (drive & 0xFF) or got_lba != lba or got_count != sector_count:
        raise ValueError("disk_write_sectors echo mismatch")
    return {"drive": drive, "lba": lba, "sector_count": sector_count, "carry": carry, "ax": ax, "ok": carry == 0}


def disk_read_sectors(drive: int, lba: int, sector_count: int) -> bytes:
    """Reads sector_count sectors (<=MAX_WRITE_SECTORS=128) from real disk
    at (drive, lba) into the agent's staging buffer via opcode 0x08 (BIOS
    INT13h AH=0x42, extended read), then pulls that buffer back over the
    network via read_block() against its physical address -- reuses the
    existing bulk-read mechanism rather than duplicating it. Read-only
    w.r.t. the disk. Verified in QEMU: wrote a known pattern, cleared the
    staging buffer, read it back through this exact opcode path, and
    confirmed an exact match."""
    if sector_count > MAX_WRITE_SECTORS:
        raise ValueError(f"sector_count={sector_count} exceeds MAX_WRITE_SECTORS={MAX_WRITE_SECTORS}")
    payload = bytes([0x08, drive & 0xFF]) + struct.pack("<QH", lba, sector_count)
    reply = _send_and_wait(payload, expect_opcode=0x87)
    got_drive = reply[1]
    got_lba, got_count = struct.unpack("<QH", reply[2:12])
    carry, ax = struct.unpack("<BH", reply[12:15])
    if got_drive != (drive & 0xFF) or got_lba != lba or got_count != sector_count:
        raise ValueError("disk_read_sectors echo mismatch")
    if carry:
        raise RuntimeError(f"BIOS reported an error reading drive 0x{drive:02x} LBA {lba}: ax=0x{ax:04x}")

    byte_count = sector_count * 512
    dwords = []
    remaining = byte_count // 4
    offset = 0
    while remaining > 0:
        n = min(MAX_BLOCK_COUNT, remaining)
        dwords.extend(read_block(STAGE_BUF_ADDR + offset, n))
        offset += n * 4
        remaining -= n
    return b"".join(v.to_bytes(4, "little") for v in dwords)


STAGE_BUF_ADDR = 0x40000  # must match stage2_7.asm's STAGE_BUF_ADDR


# -----------------------------------------------------------------------------
# High-throughput bulk install: stage_write_block's per-call ack round-trip
# makes a real multi-megabyte transfer impractical (a 56MB image is ~55,000
# 1KB chunks -- even a fast ack round-trip adds up, and before remote_agent_9
# the ack didn't arrive at all, meaning every single call burned a full
# RETRIES*TIMEOUT=6s timeout -- ~90 hours for one image). Fix: fire chunks
# without waiting for any ack, then verify the whole 64KB staging buffer in
# bulk via read_block() (proven 100% reliable all session) before committing
# to disk. Only mismatches get resent, acked, individually.
# -----------------------------------------------------------------------------

def _stage_write_block_noack(offset: int, data: bytes) -> None:
    """Fire-and-forget variant of stage_write_block(): sends the frame but
    does not wait for or parse a reply. Correctness is established afterward
    by bulk-verifying the staging buffer with read_block(), not by trusting
    this call's own ack."""
    if len(data) % 4 != 0:
        raise ValueError("data length must be a multiple of 4 bytes")
    count = len(data) // 4
    if count > MAX_STAGE_DWORDS:
        raise ValueError(f"count={count} exceeds MAX_STAGE_DWORDS={MAX_STAGE_DWORDS}")
    payload = bytes([0x06]) + struct.pack("<IH", offset, count) + data
    frame = Ether(dst="ff:ff:ff:ff:ff:ff", type=ETHERTYPE) / Raw(payload)
    sendp(frame, iface=IFACE, verbose=False)


def _read_block_noack(addr: int, count: int) -> None:
    """Fire-and-forget variant of read_block(): sends the READ_BLOCK
    request but does not wait for a reply. Pipelined verification fires many
    of these, then collects every reply in one sniff() window."""
    payload = bytes([0x03]) + struct.pack("<IH", addr, count)
    frame = Ether(dst="ff:ff:ff:ff:ff:ff", type=ETHERTYPE) / Raw(payload)
    sendp(frame, iface=IFACE, verbose=False)


def _read_block_pipelined(requests: list, collect_timeout: float = 0.6) -> dict:
    """Fires every (addr, count) in `requests` with no wait between them,
    then opens ONE sniff() window collecting every 0x83 reply, matched back
    to its request by the address it echoes (not by arrival order). Returns
    {addr: [dwords]} for whichever requests got a reply; addrs missing from
    the result are drops that need a retry pass, not an error -- this is the
    actual frame-usage fix: one wait per BATCH of requests, not one wait per
    request."""
    results = {}

    def _handler(pkt):
        if Raw not in pkt:
            return
        data = bytes(pkt[Raw].load)
        if not data or data[0] != 0x83 or len(data) < 7:
            return
        got_addr, got_count = struct.unpack("<IH", data[1:7])
        chunk = data[7:7 + got_count * 4]
        if len(chunk) < got_count * 4:
            return
        results[got_addr] = list(struct.unpack(f"<{got_count}I", chunk))

    for addr, count in requests:
        _read_block_noack(addr, count)
        time.sleep(0.0005)

    want = len(requests)
    sniff(iface=IFACE, filter="ether proto 0x88b5", timeout=collect_timeout,
          prn=_handler, stop_filter=lambda p: len(results) >= want)

    return results


def _read_staged_fast(nbytes: int, max_retry_passes: int = 6) -> bytes:
    """Bulk-reads the first `nbytes` of the staging buffer via pipelined
    READ_BLOCK requests -- one wait per batch, not one wait per 1KB chunk
    (that per-chunk wait was the actual frame-usage bottleneck, not LAN
    bandwidth). Retries only whatever chunk(s) didn't come back."""
    chunk_dwords = MAX_BLOCK_COUNT
    total_dwords = (nbytes + 3) // 4

    addrs = []
    offset = 0
    remaining = total_dwords
    while remaining > 0:
        n = min(chunk_dwords, remaining)
        addrs.append((STAGE_BUF_ADDR + offset, n))
        offset += n * 4
        remaining -= n

    collected = {}
    pending = list(addrs)
    for _ in range(max_retry_passes):
        if not pending:
            break
        got = _read_block_pipelined(pending)
        collected.update(got)
        pending = [(a, n) for (a, n) in pending if a not in collected]

    if pending:
        raise RuntimeError(
            f"_read_staged_fast: {len(pending)} chunk(s) never replied after "
            f"{max_retry_passes} passes: {[hex(a) for a, _ in pending]}")

    dwords = []
    for addr, n in addrs:
        dwords.extend(collected[addr])
    return b"".join(v.to_bytes(4, "little") for v in dwords)[:nbytes]


def stage_write_bulk_fast(data: bytes, inter_frame_delay: float = 0.0015,
                           max_repair_passes: int = 5) -> None:
    """High-throughput staging for data up to STAGE_BUF_SIZE (64KB): fires all
    sub-chunks without waiting for per-chunk acks (a small inter_frame_delay
    gives remote_agent's single-descriptor RX polling loop time to re-arm
    between frames), then bulk-verifies the whole buffer with read_block().
    Any chunk that didn't land (dropped frame) gets individually resent
    (acked, via stage_write_block) and the buffer re-verified, up to
    max_repair_passes times. Raises if it can't converge."""
    if len(data) > STAGE_BUF_SIZE:
        raise ValueError(f"data length {len(data)} exceeds STAGE_BUF_SIZE={STAGE_BUF_SIZE}")
    chunk_bytes = MAX_STAGE_DWORDS * 4
    padded = data if len(data) % 4 == 0 else data + b"\x00" * (4 - len(data) % 4)

    offset = 0
    while offset < len(padded):
        chunk = padded[offset:offset + chunk_bytes]
        _stage_write_block_noack(offset, chunk)
        offset += chunk_bytes
        time.sleep(inter_frame_delay)

    for _ in range(max_repair_passes):
        actual = _read_staged_fast(len(padded))
        if actual == padded:
            return
        offset = 0
        while offset < len(padded):
            chunk = padded[offset:offset + chunk_bytes]
            if actual[offset:offset + len(chunk)] != chunk:
                stage_write_block(offset, chunk)  # acked, reliable, rare path
            offset += chunk_bytes

    raise RuntimeError("stage_write_bulk_fast: staging buffer would not "
                        "converge after repair passes")


def install_image_to_disk(drive: int, data: bytes, start_lba: int = 0,
                           progress: bool = True) -> None:
    """Writes `data` to real disk at (drive, start_lba) onward, using the
    fast pipelined staging path per 64KB block, verified before every
    disk_write_sectors() commit. This is the actual bulk-install mechanism --
    e.g. writing a whole prebuilt disk image to drive 0x80 over the network,
    no physical reflash."""
    block_bytes = STAGE_BUF_SIZE  # 64KB = 128 sectors
    sectors_per_block = block_bytes // 512
    total_blocks = (len(data) + block_bytes - 1) // block_bytes
    lba = start_lba
    t0 = time.time()
    for i in range(total_blocks):
        chunk = data[i * block_bytes:(i + 1) * block_bytes]
        sector_count = (len(chunk) + 511) // 512
        if len(chunk) % 512 != 0:
            chunk = chunk + b"\x00" * (sector_count * 512 - len(chunk))

        stage_write_bulk_fast(chunk)
        result = disk_write_sectors(drive, lba, sector_count)
        if not result["ok"]:
            raise RuntimeError(f"disk_write_sectors failed at lba={lba}: "
                                f"ax=0x{result['ax']:04x}")

        lba += sector_count
        if progress and (i % 10 == 0 or i == total_blocks - 1):
            done = min((i + 1) * block_bytes, len(data))
            elapsed = time.time() - t0
            rate = done / elapsed / 1024 if elapsed > 0 else 0
            print(f"  [{i+1}/{total_blocks}] {done}/{len(data)} bytes "
                  f"({rate:.1f} KB/s, {elapsed:.1f}s elapsed)")


def verify_disk_image(drive: int, data: bytes, start_lba: int = 0,
                       sample_stride_blocks: int = 1) -> bool:
    """Independently reads back what was written by install_image_to_disk()
    via disk_read_sectors() and compares. sample_stride_blocks=1 verifies
    every 64KB block; >1 spot-checks every Nth block instead (faster, weaker
    guarantee)."""
    block_bytes = STAGE_BUF_SIZE
    sectors_per_block = block_bytes // 512
    total_blocks = (len(data) + block_bytes - 1) // block_bytes
    lba = start_lba
    for i in range(total_blocks):
        chunk = data[i * block_bytes:(i + 1) * block_bytes]
        sector_count = (len(chunk) + 511) // 512
        if i % sample_stride_blocks == 0:
            readback = disk_read_sectors(drive, lba, sector_count)
            expected = chunk if len(chunk) % 512 == 0 else \
                chunk + b"\x00" * (sector_count * 512 - len(chunk))
            if readback != expected:
                print(f"MISMATCH at block {i} (lba={lba})")
                return False
        lba += sector_count
    return True


def ping(text: str = "hello") -> str:
    """Sends a PING and returns the echoed reply text (sanity check that
    the agent is alive and answering)."""
    payload = bytes([0x00]) + text.encode("ascii")
    reply = _send_and_wait(payload, expect_opcode=0x80)
    return reply[1:].split(b"\x00", 1)[0].decode("ascii", errors="replace")


# -----------------------------------------------------------------------------
# Keepalive: the link has been observed to go dead after a period of no
# traffic -- works fine right after boot, degrades the longer it sits idle.
# Root cause is suspected to be a PHY-level power-saving feature (RTL8168
# ALDPS/EEE live in the PHY's own MDIO registers, which this firmware's
# do_reset never touches -- it only resets the MAC and clears one MAC-level
# gate bit) rather than anything host-side: disabling every Windows-side
# power/EEE/green-ethernet setting on this machine's NIC made no difference.
# Cheapest mitigation that doesn't require touching PHY registers at all:
# never let the link go idle in the first place. Run this in a background
# thread for the duration of a work session; it's silent on success and
# just logs (never raises) on a missed beat, so it won't crash whatever
# else is using the agent at the same time.
# -----------------------------------------------------------------------------

import threading


class Keepalive:
    """Background thread that pings the agent every `interval` seconds so
    the link never sits idle long enough to trigger the power-state issue.
    Usage:
        ka = Keepalive(interval=2.0)
        ka.start()
        ... do real work with ping()/read32()/etc ...
        ka.stop()
    Or as a context manager: `with Keepalive(): ...`
    """

    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.misses = 0
        self.beats = 0

    def _run(self):
        while not self._stop.is_set():
            try:
                ping("keepalive")
                self.beats += 1
            except TimeoutError:
                self.misses += 1
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + TIMEOUT * RETRIES + 1)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()


# -----------------------------------------------------------------------------
# Remote self-flash (remote_agent_13+): the actual "flash on-the-fly" path --
# push a new boot image over the network and have the box reboot into it
# itself, no physical re-flash or manual power cycle. Deliberately built on
# the plain, non-pipelined stage_write_block()/disk_write_sectors() calls
# (proven reliable one-at-a-time) -- NOT stage_write_bulk_fast()/
# install_image_to_disk(), whose fire-and-forget bursting has hung the
# machine twice this session even after a firmware ring rework. Self-flash
# is exactly the operation that must not be built on an unproven fast path.
# -----------------------------------------------------------------------------

def get_boot_drive() -> int:
    """Returns the BIOS drive number this remote_agent image itself booted
    from (opcode 0x09) -- captured by boot.asm into a fixed low-memory
    address at boot time. Lets self_flash() target the right drive without
    the caller having to know or guess it."""
    payload = bytes([0x09])
    reply = _send_and_wait(payload, expect_opcode=0x89)
    return reply[1]


def reboot() -> None:
    """Sends the REBOOT command (opcode 0x0A, guarded by a magic value so a
    stray frame can't trigger it by accident). The agent acks first (so this
    call returns normally) and only then resets via the keyboard controller
    -- by the time this function returns, the box is on its way down and
    will re-enter BIOS POST -> boot from whatever is now at LBA 0 of its
    boot drive."""
    payload = bytes([0x0A]) + struct.pack("<I", 0xDEADC0DE)
    _send_and_wait(payload, expect_opcode=0x8A)


def self_flash(boot_bin: bytes, stage2_bin: bytes, confirm: bool = False) -> None:
    """Writes a new (boot.bin + stage2.bin) image to LBA 0 of this agent's
    own boot drive over the network, then reboots into it. THE ONLY WAY
    THIS PROJECT FLASHES ITSELF WITHOUT PHYSICAL ACCESS -- get this input
    right, since a bad image here means the box won't come back up without
    someone physically re-flashing it. Caller must pass confirm=True."""
    if not confirm:
        raise ValueError("self_flash requires confirm=True -- this overwrites "
                          "this agent's own boot sectors and reboots into whatever "
                          "was written; a bad image means physical recovery")
    image = boot_bin + stage2_bin
    if len(image) % 512 != 0:
        raise ValueError(f"image length {len(image)} is not sector-aligned (512)")
    if len(image) > STAGE_BUF_SIZE:
        raise ValueError(f"image length {len(image)} exceeds STAGE_BUF_SIZE={STAGE_BUF_SIZE}")

    drive = get_boot_drive()
    stage_write_bulk(image)  # plain, non-pipelined, proven reliable
    result = disk_write_sectors(drive, 0, len(image) // 512)
    if not result["ok"]:
        raise RuntimeError(f"self_flash: disk_write_sectors failed, ax=0x{result['ax']:04x}")
    reboot()


if __name__ == "__main__":
    print("ping:", ping("hello-remote-agent"))
