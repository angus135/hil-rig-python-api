"""Temporary HRTP ECHO/STATUS envelope used only by Transport hardware tests."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from .models import ENVELOPE_VERSION, UINT32_MASK

MAGIC = b"HRTP"
HEADER = struct.Struct("<4sBBHII")
HEADER_SIZE = HEADER.size
STATUS_V1 = struct.Struct("<12I")
SUPPORTED_FLAGS = 0


class HarnessCodecError(ValueError):
    """The temporary test envelope is malformed or unsupported."""


class Opcode(IntEnum):
    ECHO_REQUEST = 0x01
    STATUS_REQUEST = 0x02
    ECHO_RESPONSE = 0x81
    STATUS_RESPONSE = 0x82


@dataclass(frozen=True, slots=True)
class HarnessMessage:
    version: int
    opcode: Opcode
    flags: int
    request_id: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class StatusPayloadV1:
    schema_version: int
    link_state: int
    link_generation: int
    transport_event_count: int
    usb_rx_bytes: int
    usb_tx_bytes: int
    application_requests_received: int
    responses_submitted: int
    usb_tx_busy_retries: int
    invalid_harness_messages: int
    maximum_service_gap_ms: int
    transport_session_state: int


class RequestIdAllocator:
    """Allocate nonzero uint32 request IDs without reusing an active ID."""

    def __init__(self, next_id: int = 1) -> None:
        if type(next_id) is not int or not 1 <= next_id <= UINT32_MASK:
            raise ValueError("next_id must be in the range 1..0xFFFFFFFF")
        self._next_id = next_id
        self._active: set[int] = set()

    def allocate(self) -> int:
        for _ in range(UINT32_MASK):
            candidate = self._next_id
            self._next_id = 1 if candidate == UINT32_MASK else candidate + 1
            if candidate not in self._active:
                self._active.add(candidate)
                return candidate
        raise RuntimeError("all request IDs are active")

    def release(self, request_id: int) -> None:
        self._active.discard(request_id)


def _require_message_limit(max_application_message_size: int) -> int:
    if type(max_application_message_size) is not int or max_application_message_size < HEADER_SIZE:
        raise ValueError(f"maximum Application message size must be at least {HEADER_SIZE}")
    return max_application_message_size


def maximum_payload_size(max_application_message_size: int) -> int:
    return _require_message_limit(max_application_message_size) - HEADER_SIZE


def encode_message(
    opcode: Opcode,
    request_id: int,
    payload: bytes = b"",
    *,
    max_application_message_size: int,
    flags: int = SUPPORTED_FLAGS,
) -> bytes:
    if type(opcode) is not Opcode:
        raise TypeError("opcode must be an Opcode")
    if type(request_id) is not int or not 0 <= request_id <= UINT32_MASK:
        raise ValueError("request_id must be a uint32")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if flags != SUPPORTED_FLAGS:
        raise HarnessCodecError("unsupported harness flags")
    limit = _require_message_limit(max_application_message_size)
    total = HEADER_SIZE + len(payload)
    if total > limit:
        raise HarnessCodecError(
            f"test message size {total} exceeds effective Transport Application limit {limit}"
        )
    header = HEADER.pack(MAGIC, ENVELOPE_VERSION, int(opcode), flags, request_id, len(payload))
    return header + payload


def decode_message(data: bytes, *, max_application_message_size: int) -> HarnessMessage:
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    limit = _require_message_limit(max_application_message_size)
    if len(data) > limit:
        raise HarnessCodecError("message exceeds effective Transport Application limit")
    if len(data) < HEADER_SIZE:
        raise HarnessCodecError("truncated harness header")
    magic, version, raw_opcode, flags, request_id, payload_length = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise HarnessCodecError("invalid harness magic")
    if version != ENVELOPE_VERSION:
        raise HarnessCodecError(f"unsupported harness version {version}")
    if flags != SUPPORTED_FLAGS:
        raise HarnessCodecError(f"unsupported harness flags 0x{flags:04x}")
    try:
        opcode = Opcode(raw_opcode)
    except ValueError as exc:
        raise HarnessCodecError(f"unknown harness opcode 0x{raw_opcode:02x}") from exc
    expected_length = HEADER_SIZE + payload_length
    if len(data) < expected_length:
        raise HarnessCodecError("declared payload length exceeds available content")
    if len(data) > expected_length:
        raise HarnessCodecError("declared payload length is shorter than actual content")
    return HarnessMessage(version, opcode, flags, request_id, data[HEADER_SIZE:])


def encode_echo_request(
    request_id: int, payload: bytes, *, max_application_message_size: int
) -> bytes:
    return encode_message(
        Opcode.ECHO_REQUEST,
        request_id,
        payload,
        max_application_message_size=max_application_message_size,
    )


def encode_status_request(request_id: int, *, max_application_message_size: int) -> bytes:
    return encode_message(
        Opcode.STATUS_REQUEST,
        request_id,
        max_application_message_size=max_application_message_size,
    )


def decode_status_payload(payload: bytes) -> StatusPayloadV1:
    if len(payload) != STATUS_V1.size:
        raise HarnessCodecError(
            f"STATUS v1 payload must be {STATUS_V1.size} bytes, received {len(payload)}"
        )
    values = STATUS_V1.unpack(payload)
    version = values[0]
    if version != 1:
        raise HarnessCodecError(f"unsupported STATUS schema version {version}")
    return StatusPayloadV1(*values)
