# remote_agent

**A machine you can reach when the OS is gone.**

`remote_agent` is a ~16KB x86 program that *is* the operating system, for
one purpose: give you a persistent, network-reachable command channel
into a real physical machine, running before, instead of, and completely
independent of Windows, Linux, or anything else that would normally sit
between you and the hardware. It boots off a USB stick or over legacy
PXE, brings up the NIC itself with hand-written driver code, and then
just... sits there, forever, answering a raw-Ethernet protocol: read any
physical address, write any physical address, read/write raw disk
sectors, dump and decode the BIOS, enumerate every PCI device and every
ACPI table, reboot the box, or push a whole new image to it and have it
reboot into that instead — all from a Python REPL on another machine, no
keyboard or monitor on the target required after the very first boot.

## Why this is different from "normal" remote management

Server hardware has IPMI/BMC chips for this. Consumer boards don't. This
project builds the equivalent — a true out-of-band control channel — out
of nothing but a boot sector, on hardware that was never designed to
have one. The agent isn't a program running *under* an OS that could
crash, get patched, get compromised, or simply not have booted yet — it
*is* the entire execution environment. There's no kernel to panic, no
driver stack to fail, no login screen to get past. If the CPU is
running, the agent is what's running on it.

That also means it sees things a normal remote-management story never
would: raw BIOS ROM content, the real SMBIOS/DMI tables, the actual ACPI
tables the platform hands the OS, arbitrary physical memory, at an
address you choose, with nothing between your request and the hardware.

## Use cases

- **Out-of-band management for boards without a BMC.** Reboot a
  headless/remote machine, inspect its state, or push a new disk image
  to it over the network — even if whatever OS was on it is unbootable,
  corrupted, or was never installed at all.
- **Hardware archaeology / reverse engineering.** Full read-only maps of
  SMBIOS, ACPI (RSDT/XSDT → every table, FADT/MADT/MCFG decoded), and
  PCI config space (including walking bridge chains to devices that
  aren't even on bus 0) — from a board's *actual* firmware, not a
  datasheet.
- **Bare-metal / low-level education.** A complete, working, from-scratch
  example of PCI enumeration, MMIO, a real NIC brought up by hand (RTL8168,
  MDIO/PHY access included), protected-mode transition, and a real
  request/reply network protocol — all in one relatively small, readable
  codebase, with the full round-by-round history kept so you can see
  *how* it was built, not just the final state.
- **Recovery tooling.** When a machine won't boot into its real OS, this
  still can — from a spare USB slot or PXE — and gives you memory and
  disk access to diagnose or fix it remotely.
- **Fleet provisioning with an actual back-channel.** PXE normally means
  "one-shot install and hope." This keeps a live control channel open
  after boot instead of handing off to an installer and disappearing.

## Layout

```
source/     boot.asm/boot_N.asm (stage1, MBR) and stage2.asm/stage2_N.asm
            (stage2: PCI scan, NIC bring-up, protected mode, command loop)
            for every round from the first working version through the
            current one. boot_pxe_N.asm is the PXE-delivery variant of
            stage1 (stage2 is identical either way).
tools/      Host-side Python (needs `scapy` — `pip install scapy`):
              remote_control.py       the client library
              dump_and_decode_bios.py BIOS ROM dump + SMBIOS/DMI decode
              bios_mapper.py          full SMBIOS + ACPI + PCI enumeration
              bench_block.py          disk write/read throughput bench
              verify_writes.py        disk-write correctness check
              poll_ping.py            wait-for-recovery poller
              graduated_burst_test.py stage-write burst/stress test
VERSIONS/   Unified diffs between every consecutive round (stage2_N_to_M
            .diff, boot_N_to_M.diff where stage1 actually changed) — use
            these to see exactly what each round changed, in order.
release/    Latest working build: remote_agent_19.img (disk) and
            remote_agent_pxe_19.nbp (PXE). Rebuild any round yourself
            with `nasm -f bin boot_N.asm -o boot_N.bin` etc. (see Building).
```

## Getting started

**What you need:** two machines connected by a plain Ethernet cable (a
switch works too — everything here is broadcast-based, no IP needed at
all) or a PXE-capable network; a USB stick if you're going the disk-boot
route; [NASM](https://www.nasm.us/) to assemble; Python 3.10+ with
`scapy` (`pip install scapy`) on the *controller* machine.

**1. Flash or serve the image.**
- Disk boot: write `release/remote_agent_19.img` raw to a USB stick (Rufus
  in "DD image" mode on Windows, or `dd if=remote_agent_19.img
  of=/dev/sdX` on Linux — get the device letter right, this overwrites
  the whole disk) and boot the target from it.
- PXE boot: serve `release/remote_agent_pxe_19.nbp` over TFTP and point
  your DHCP/PXE server's boot filename at it. No disk write on the
  target at all.

**2. Point `remote_control.py` at the right NIC.** Open
`tools/remote_control.py` and set `IFACE` to the *controller* machine's
network adapter name (on Windows, whatever `Get-NetAdapter` calls it —
e.g. `"Ethernet 8"`; on Linux, `eth0`/`enp3s0`/etc.).

**3. Run it elevated.** On Windows this has to be an Administrator
terminal — Npcap silently no-ops raw frame send/receive without it, and
the failure mode (every call times out, no error message) looks exactly
like a dead cable. This is the single most common "it's not working"
cause; check it first.

**4. Boot the target and talk to it:**

```python
import remote_control as rc

rc.ping()                        # sanity check -- should echo back instantly
rc.read32(0xF0000)               # one dword from physical memory
rc.read_block(0xF0000, 368)      # up to 368 dwords in one round trip
rc.reboot()                      # remote power-cycle, no hands on the box needed
```

For anything more than a quick one-off, wrap it in the keepalive (see
*Known issues* — the link has been observed to go idle-dead otherwise):

```python
with rc.Keepalive():
    rc.read_block(0xF0000, 368)
    # ... whatever else, however long it takes ...
```

**5. Try the higher-level tools** once basic `ping()` works:

```bash
python tools/dump_and_decode_bios.py        # BIOS ROM + SMBIOS decode
python tools/bios_mapper.py --json out.json # full SMBIOS + ACPI + PCI map
```

## Wire protocol

Raw Ethernet, EtherType `0x88B5` (IEEE-reserved for local experimental
use — not a real registered protocol, so it's inert on any real network
and trivially filterable: `eth.type == 0x88b5`). No IP, no ARP, no DHCP —
broadcast frames, opcode-addressed:

| Opcode | Name | Payload | Reply |
|---|---|---|---|
| `0x00` | PING | arbitrary bytes | `0x80` + `"ECHO:"` + bytes |
| `0x01` | READ32 | 4-byte address | `0x81` + address + 4-byte value |
| `0x02` | WRITE32 | 4-byte address + 4-byte value | `0x82` + address + readback value |
| `0x03` | READ_BLOCK | 4-byte address + 2-byte count | `0x83` + address + count + count×4 bytes |
| `0x04` | PCI_CONFIG_READ | bus/dev/func/register | `0x84` + echo + 4-byte value |
| `0x05` | DISK_GET_PARAMS | 1-byte drive | `0x85` + drive + carry + BIOS geometry |
| `0x06` | STAGE_WRITE_BLOCK | offset + count + data | `0x86` + offset + count (into a 64KB RAM staging buffer) |
| `0x07` | DISK_WRITE_SECTORS | drive + LBA + count | `0x87` + echo + carry + AX (commits the staging buffer to real disk) |
| `0x08` | DISK_READ_SECTORS | drive + LBA + count | same shape as 0x07 (reads into the staging buffer, pull it back with READ_BLOCK) |
| `0x09` | GET_BOOT_DRIVE | — | `0x89` + drive number |
| `0x0A` | REBOOT | 4-byte magic `0xDEADC0DE` | `0x8A` (acks, then resets via the keyboard controller) |

`WRITE32` is **not** range-restricted in firmware — anything vetting
which addresses are safe to write happens on the host side, in whatever
script calls `remote_control.py`.

## Building

```bash
nasm -f bin boot_19.asm -o boot_19.bin
nasm -f bin boot_pxe_19.asm -o boot_pxe_19.bin
nasm -f bin stage2_19.asm -o stage2_19.bin
```

Disk image = `boot.bin` (512B) + `stage2.bin` (16384B) + zero-padding to
1MiB. PXE image = `boot_pxe.bin` (512B) + 512B zero pad + the *same*
`stage2.bin` (the pad exists purely so stage2 lands at the same physical
address, `0x8000`, that the disk path's own INT13 read would have put it
at — stage2 itself needs zero changes either way).

QEMU testing uses `-D TARGET_DEVICE=0x8139` (QEMU emulates `rtl8139`, not
the real `8168`) and `-nic user,model=rtl8139`. In this setup QEMU
doesn't map BAR2 correctly (`MMIO base` reads as `0`), so QEMU testing
only confirms the code executes without crashing — it does not validate
real register behavior. Real hardware validation is required either way.

## Version history

Full diffs for every transition are in `VERSIONS/`. Rounds 1–13 were
early NIC/PCI/protected-mode bring-up (see the diffs for exact changes —
no narrative reconstruction attempted here beyond what the diffs show).
From round 14 on:

- **v14**: last round before this project's real-hardware RX path
  actually started accepting frames.
- **v15**: `boot_pxe_15.asm` added — legacy PXE delivery of stage1,
  landing stage2 at the same address the disk path's INT13 read would
  have used, so stage2 itself needed no changes.
- **v16**: fixed a receiver that was silently accepting nothing. RCR
  (RTL8168 receive-filter register, offset `0x44`) came back
  `0x00028F00` after reset on real hardware — bits 0–3 (the actual
  accept-broadcast/accept-unicast/accept-multicast filters) were all
  clear. A prior round's comment had assumed RCR "already looked sane"
  post-reset based on an earlier probe and deliberately left it alone;
  that assumption didn't hold on every reset. v16 explicitly ORs in
  APM|AM|AB once, before RX is armed, instead of trusting the reset
  value.
- **v17**: fixed silently-dropped replies. `remote_send_frame`/
  `remote_send_frame_var` (every reply's TX path) polled the TX
  descriptor's OWN bit with a bounded timeout, but on a timeout it fell
  through and returned normally anyway — a genuinely failed transmit
  vanished with no error and no retry. v17 wraps both in a bounded retry
  (re-arm + re-ring the doorbell from scratch) and only reports failure
  if every attempt is exhausted.
- **v18**: disables ALDPS (Advanced Link Down Power Saving), a
  PHY-level Realtek power-saving feature living in MDIO registers,
  independent of anything the MAC-level reset touches. Implemented
  proper MDIO read/write via `PHYAR` (offset `0x60`) and cleared bit 2
  of PHY page `0x0A43` register `0x10`. This turned out **not** to be
  the actual cause of the freezing this round was chasing (see below),
  but is a real fix worth keeping regardless.
- **v19**: fixed an unbounded busy-spin. `remote_loop`'s wait for the
  next incoming frame had no timeout at all — if the RX engine ever
  stopped delivering into the single RX descriptor, the CPU would spin
  there forever. Same bug class as v17, just never applied to the RX
  side. v19 bounds the wait and, on timeout, recycles `CmdRxEnb` (off
  then on) to force the RX engine to re-arm before trying again, so a
  stall now self-heals instead of hanging forever.

### The actual freeze root cause (not firmware)

After v16–v18, the box still froze intermittently — not just
network-quiet, the whole screen stopped updating. It turned out to be
**external to this firmware entirely**: a second PS/2 keyboard connected
to the box. Most BIOSes route PS/2 keyboard/mouse activity through SMM
(System Management Mode) for USB legacy-keyboard emulation. SMM is
invisible to and can preempt any ring-0 code with zero warning — nothing
in this firmware, and no register it reads, could ever have surfaced
this. Disconnecting the second keyboard resolved it immediately.

**If this box ever freezes again: check for a second PS/2 device before
touching any of this code.**

## Known issues

- **Idle-link dropout**, separate from the freeze above: the network
  link itself has been observed to stop responding after a period with
  no traffic, recovering only on a fresh boot — not a `reboot()`-cured
  hang, an actually-dead link. Working mitigation, not yet root-caused:
  never let it go idle. `remote_control.py`'s `Keepalive` class pings in
  a background thread; wrap any real work in `with rc.Keepalive(): ...`.
  Every Windows-side power/EEE/Green-Ethernet/Gigabit-Lite setting on
  the *host* NIC was ruled out as the cause (all disabled, including a
  full adapter restart to force the changes live, zero change to the
  symptom) — root cause is presumed to be host- or link-side but is not
  confirmed.
- **BIOS *modification* has not been attempted.** Reading the BIOS ROM
  shadow (`0xF0000`–`0xFFFFF`) is safe and working (`dump_and_decode_bios.py`).
  Actually changing persistent BIOS content is a different, much
  higher-stakes problem — likely requires the chipset's SPI controller
  rather than a plain memory write, may be write-protect locked
  (`BIOS_CNTL`/WPD), and a bad write risks bricking the board's firmware
  with no easy recovery. Not scoped, not attempted.
