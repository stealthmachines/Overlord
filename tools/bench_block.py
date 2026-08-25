import time
import remote_control as rc

DRIVE = 0x80
LBA = 1_100_000  # fresh, unused, deep, distinct from earlier test LBAs

data = bytes([(i * 7 + 3) & 0xFF for i in range(rc.STAGE_BUF_SIZE)])

t0 = time.time()
rc.install_image_to_disk(DRIVE, data, start_lba=LBA, progress=True)
t1 = time.time()
print(f"write+verify+commit: {t1-t0:.2f}s for {len(data)} bytes "
      f"({len(data)/(t1-t0)/1024:.1f} KB/s)")

readback = rc.disk_read_sectors(DRIVE, LBA, len(data)//512)
ok = readback == data
print("readback match:", ok)
print("ping:", rc.ping("post-bench"))
