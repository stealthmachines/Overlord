import time
import remote_control as rc

DRIVE = 0x80
LBAS = [1_000_000, 1_000_008, 1_000_016, 1_000_024]

for i, lba in enumerate(LBAS):
    expected = bytes([((0x30 + i) & 0xFF)] * 512)
    ok = False
    for attempt in range(3):
        try:
            data = rc.disk_read_sectors(DRIVE, lba, 1)
            ok = True
            break
        except TimeoutError as e:
            print(f"lba {lba}: read attempt {attempt+1} timed out, retrying...")
            time.sleep(1.5)
    if not ok:
        print(f"lba {lba}: FAILED to read after 3 attempts")
        continue
    if data == expected:
        print(f"lba {lba}: VERIFIED (512/512 bytes match, fill=0x{0x30+i:02x})")
    else:
        diffs = sum(1 for a, b in zip(data, expected) if a != b)
        print(f"lba {lba}: MISMATCH ({diffs}/512 bytes differ) -- got {data[:8].hex()}")
    time.sleep(1.0)

print("ping:", rc.ping("verify-writes-done"))
