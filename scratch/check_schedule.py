import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.loopback_utilization_point import DEFAULT_POINT, PROFILE, compile_loopback_workload

workload = compile_loopback_workload(PROFILE, DEFAULT_POINT)
print("CAN transfers:")
for t in workload.can.transfers[:10]:
    print(f"  Tick {t.tick}: {t.payload_bytes} bytes ({t.payload_bytes // 8} frames)")

print("\nUART transfers:")
for t in workload.uart.transfers[:10]:
    print(f"  Tick {t.tick}: {t.payload_bytes} bytes")

print("\nSPI transfers:")
for t in workload.spi.transfers[:10]:
    print(f"  Tick {t.tick}: {t.payload_bytes} bytes")
