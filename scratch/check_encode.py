import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.loopback_utilization_point import DEFAULT_POINT, build_test_for_point
from hilrig.protocol.application import VariableIOProtocolAdapter

test = build_test_for_point(DEFAULT_POINT).compile()
adapter = VariableIOProtocolAdapter()
upload = adapter.build_upload(test)

print(f"Total messages to upload: {len(upload.messages)}")
for i, msg in enumerate(upload.messages):
    try:
        encoded = adapter.codec.encode(msg)
    except Exception as e:
        print(
            f"\nFailed at message index {i}: type={type(msg).__name__}, "
            f"tick={getattr(msg, 'tick_number', None)}"
        )
        print(f"Error: {e}")
        if hasattr(msg, "operations"):
            print(f"Total operations in message: {len(msg.operations)}")
            for j, op in enumerate(msg.operations):
                print(
                    f"  Op {j}: periph={op.peripheral_type.name}, chan={op.channel}, "
                    f"payload_len={len(op.payload)}"
                )
        break
else:
    print("All messages encoded successfully!")
