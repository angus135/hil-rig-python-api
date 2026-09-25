"""Two-channel CAN standard-frame loopback test.

Cross-wire CAN channel 0 (CAN 1) to CAN channel 1 (CAN 2), configure both
interfaces for 500 kbit/s, and run this definition through the normal
HIL-RIG variable-message workflow.
"""

from hilrig import FrequencyMode, StartMode, Test


CAN_FRAME_COUNT = 128
CAN_PAYLOAD_SIZE = 8


def build_test() -> Test:
    """Construct a 128-frame cross-channel standard CAN loopback test."""
    test = Test(name="CAN2-to-CAN1 standard-frame loopback")
    test.configure(
        frequency_mode=FrequencyMode.HZ_1K,
        start_mode=StartMode.IMMEDIATE,
    )

    can1 = test.can(channel=0).configure(bitrate=500_000)
    can2 = test.can(channel=1).configure(bitrate=500_000)

    frames = tuple(
        bytes((frame_index + offset) & 0xFF for offset in range(CAN_PAYLOAD_SIZE))
        for frame_index in range(CAN_FRAME_COUNT)
    )
    frame_ids = tuple(0x100 + frame_index for frame_index in range(CAN_FRAME_COUNT))
    first_frame, final_frame = frames[0], frames[-1]
    first_id, final_id = frame_ids[0], frame_ids[-1]

    for frame_index, (frame_id, payload) in enumerate(zip(frame_ids, frames, strict=True)):
        can2.transmit(frame_id=frame_id, data=payload, at_ms=100 + frame_index * 2)

    can2_frame_end_ms = 100 + (CAN_FRAME_COUNT - 1) * 2
    test.expect(can1).receive(
        frame_id=first_id,
        data=first_frame,
        from_ms=100,
        until_ms=150,
    )
    test.expect(can1).receive(
        frame_id=final_id,
        data=final_frame,
        from_ms=can2_frame_end_ms,
        until_ms=can2_frame_end_ms + 100,
    )
    return test


if __name__ == "__main__":
    compiled = build_test().compile()
    print(f"Test: {compiled.name}")
    print(f"Frequency: {compiled.frequency_hz} Hz")
    print(f"Expected ticks: {compiled.expected_tick_count}")
    print(f"Instructions: {len(compiled.instructions)}")
    print(f"Assertions: {len(compiled.assertions)}")
    print("CAN payload: TX=1024 bytes, RX=1024 bytes")
