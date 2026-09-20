from dataclasses import fields

import pytest

from hilrig.protocol_test.harness_codec import (
    HEADER_SIZE,
    MAGIC,
    HarnessCodecError,
    Opcode,
    RequestIdAllocator,
    StatusPayloadV3,
    decode_message,
    decode_status_payload,
    encode_echo_request,
    encode_message,
    encode_status_request,
    maximum_payload_size,
)

LIMIT = 512


def test_status_v3_field_order_is_exact() -> None:
    assert [field.name for field in fields(StatusPayloadV3)] == [
        "schema_version",
        "link_state",
        "link_generation",
        "transport_event_count",
        "usb_rx_bytes",
        "usb_tx_bytes",
        "application_messages_received",
        "responses_results_submitted",
        "usb_tx_busy_retries",
        "invalid_hrtp_messages",
        "maximum_service_gap_ms",
        "transport_session_state",
        "compatibility_profile_id",
        "protocol_version_major",
        "protocol_version_minor",
        "protocol_version_patch",
        "application_codec_initialized",
        "application_initialization_status",
        "non_hrtp_application_messages_received",
        "application_decode_failures",
        "application_semantic_rejections",
        "application_encode_failures",
        "configurations_accepted",
        "instructions_accepted",
        "results_encoded",
        "application_harness_state",
        "next_expected_tick",
        "active_expected_tick_count",
        "last_application_status",
        "last_decoded_application_message_type",
        "configuration_digest",
        "last_instruction_digest",
        "selected_instruction_family",
        "selected_result_family",
        "completed_instruction_ticks",
        "current_chunk_count",
        "maximum_chunk_count",
        "finalization_requests",
        "accepted_finalizations",
        "variable_operations_accepted",
        "result_records_emitted",
        "capture_overflow_events",
        "i2c_not_implemented_rejections",
        "maximum_decode_storage_required",
        "decode_storage_used",
        "selected_test_profile",
        "selected_fault_mode",
        "spontaneous_output_pending",
    ]


def test_fixed_little_endian_header_vector() -> None:
    encoded = encode_echo_request(0x12345678, b"abc", max_application_message_size=LIMIT)
    assert encoded == b"HRTP\x01\x01\x00\x00\x78\x56\x34\x12\x03\x00\x00\x00abc"
    assert HEADER_SIZE == 16


def test_empty_echo_round_trip() -> None:
    encoded = encode_echo_request(1, b"", max_application_message_size=LIMIT)
    message = decode_message(encoded, max_application_message_size=LIMIT)
    assert message.opcode is Opcode.ECHO_REQUEST
    assert message.request_id == 1
    assert message.payload == b""


def test_binary_echo_round_trip_and_request_id_preservation() -> None:
    payload = b"\x00\xff\x00\x01"
    message = decode_message(
        encode_echo_request(0xDEADBEEF, payload, max_application_message_size=LIMIT),
        max_application_message_size=LIMIT,
    )
    assert message.request_id == 0xDEADBEEF
    assert message.payload == payload


def test_status_request_has_empty_payload() -> None:
    message = decode_message(
        encode_status_request(7, max_application_message_size=LIMIT),
        max_application_message_size=LIMIT,
    )
    assert message.opcode is Opcode.STATUS_REQUEST
    assert message.payload == b""


def test_status_response_decoding() -> None:
    import struct

    payload = struct.pack("<48I", 3, *range(1, 48))
    status = decode_status_payload(payload)
    assert status == StatusPayloadV3(3, *range(1, 48))
    assert len(payload) == 192

    encoded = encode_message(
        Opcode.STATUS_RESPONSE,
        9,
        payload,
        max_application_message_size=LIMIT,
    )
    assert len(encoded) == 208


@pytest.mark.parametrize("size", [188, 191, 193])
def test_incorrect_status_payload_size_rejected(size: int) -> None:
    with pytest.raises(HarnessCodecError, match="192 bytes"):
        decode_status_payload(bytes(size))


def test_unsupported_status_schema_version_rejected() -> None:
    import struct

    payload = struct.pack("<48I", 1, *range(1, 48))
    with pytest.raises(HarnessCodecError, match="schema version 1"):
        decode_status_payload(payload)


def test_invalid_magic_rejected() -> None:
    data = bytearray(encode_echo_request(1, b"x", max_application_message_size=LIMIT))
    data[:4] = b"NOPE"
    with pytest.raises(HarnessCodecError, match="magic"):
        decode_message(bytes(data), max_application_message_size=LIMIT)


def test_unsupported_version_rejected() -> None:
    data = bytearray(encode_echo_request(1, b"x", max_application_message_size=LIMIT))
    data[4] = 2
    with pytest.raises(HarnessCodecError, match="version"):
        decode_message(bytes(data), max_application_message_size=LIMIT)


def test_unsupported_flags_rejected() -> None:
    data = bytearray(encode_echo_request(1, b"x", max_application_message_size=LIMIT))
    data[6:8] = b"\x01\x00"
    with pytest.raises(HarnessCodecError, match="flags"):
        decode_message(bytes(data), max_application_message_size=LIMIT)


def test_unknown_opcode_rejected() -> None:
    data = bytearray(encode_echo_request(1, b"x", max_application_message_size=LIMIT))
    data[5] = 0x7F
    with pytest.raises(HarnessCodecError, match="opcode"):
        decode_message(bytes(data), max_application_message_size=LIMIT)


def test_truncated_header_rejected() -> None:
    with pytest.raises(HarnessCodecError, match="truncated"):
        decode_message(MAGIC, max_application_message_size=LIMIT)


def test_declared_length_longer_than_available_rejected() -> None:
    data = bytearray(encode_echo_request(1, b"abc", max_application_message_size=LIMIT))
    data[12:16] = (4).to_bytes(4, "little")
    with pytest.raises(HarnessCodecError, match="exceeds"):
        decode_message(bytes(data), max_application_message_size=LIMIT)


def test_declared_length_shorter_than_actual_rejected() -> None:
    data = bytearray(encode_echo_request(1, b"abc", max_application_message_size=LIMIT))
    data[12:16] = (2).to_bytes(4, "little")
    with pytest.raises(HarnessCodecError, match="shorter"):
        decode_message(bytes(data), max_application_message_size=LIMIT)


def test_oversized_message_rejected() -> None:
    payload = bytes(maximum_payload_size(LIMIT) + 1)
    with pytest.raises(HarnessCodecError, match="exceeds"):
        encode_echo_request(1, payload, max_application_message_size=LIMIT)


def test_encode_rejects_unknown_flags() -> None:
    with pytest.raises(HarnessCodecError, match="flags"):
        encode_message(
            Opcode.ECHO_REQUEST,
            1,
            max_application_message_size=LIMIT,
            flags=1,
        )


def test_request_id_wrap_skips_zero_and_active_id() -> None:
    allocator = RequestIdAllocator(0xFFFFFFFF)
    assert allocator.allocate() == 0xFFFFFFFF
    assert allocator.allocate() == 1
    allocator.release(0xFFFFFFFF)
    allocator.release(1)
    assert allocator.allocate() == 2
