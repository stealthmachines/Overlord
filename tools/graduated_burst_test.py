import time
import remote_control as rc
from scapy.all import Ether, Raw, sendp

def fire_burst(n, delay):
    for i in range(n):
        offset = (i * 1472) % (rc.STAGE_BUF_SIZE - 1472)
        data = bytes([(i * 7 + 5) & 0xFF] * 1472)
        payload = bytes([0x06]) + __import__("struct").pack("<IH", offset, 368) + data
        frame = Ether(dst="ff:ff:ff:ff:ff:ff", type=rc.ETHERTYPE) / Raw(payload)
        sendp(frame, iface=rc.IFACE, verbose=False)
        time.sleep(delay)

for n in [5, 10, 20, 40, 80]:
    print(f"=== burst of {n} frames, 1.5ms pacing ===")
    fire_burst(n, 0.0015)
    time.sleep(0.5)
    try:
        print("ping:", rc.ping(f"after-burst-{n}"))
    except TimeoutError:
        print(f"HUNG after burst of {n}")
        break
