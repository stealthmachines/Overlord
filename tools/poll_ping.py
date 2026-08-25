import time
import remote_control as rc

deadline = time.time() + 1800  # poll for up to 30 minutes
while time.time() < deadline:
    try:
        reply = rc.ping("recovery-poll")
        print("ALIVE:", reply)
        break
    except TimeoutError:
        print("still down...")
        time.sleep(5)
else:
    print("TIMED OUT waiting for recovery")
