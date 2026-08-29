from collections import deque
from types import SimpleNamespace

import pytest

from hilrig.protocol_test.models import SerialDevice, SerialSelector
from hilrig.protocol_test.serial_port import FaultInjectingSerial, FaultPlan


class FakeSerial:
    def __init__(self) -> None:
        self.identity = SerialDevice("fake")
        self.is_open = True
        self.incoming = bytearray(b"abcdefgh")
        self.writes: list[bytes] = []
        self.accepted = deque()

    @property
    def in_waiting(self) -> int:
        return len(self.incoming)

    def read(self, size: int) -> bytes:
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def write(self, data: bytes) -> int:
        accepted = self.accepted.popleft() if self.accepted else len(data)
        self.writes.append(data[:accepted])
        return accepted

    def close(self) -> None:
        self.is_open = False

    def reset_input_buffer(self) -> None:
        self.incoming.clear()

    def reset_output_buffer(self) -> None:
        pass


def test_serial_selector_requires_identity() -> None:
    with pytest.raises(ValueError, match="specify"):
        SerialSelector()


def test_fault_wrapper_limits_read_and_write_chunks() -> None:
    serial = FakeSerial()
    actions: list[dict[str, object]] = []
    wrapper = FaultInjectingSerial(
        serial,
        FaultPlan(max_read_chunk=2, max_write_accept=3),
        actions.append,
    )
    assert wrapper.read(8) == b"ab"
    assert wrapper.write(b"123456") == 3
    assert serial.writes == [b"123"]
    assert [action["action"] for action in actions] == [
        "limit_read_chunk",
        "limit_write_chunk",
    ]


def test_fault_wrapper_zero_write_is_deterministic() -> None:
    serial = FakeSerial()
    actions: list[dict[str, object]] = []
    wrapper = FaultInjectingSerial(
        serial,
        FaultPlan(zero_write_operations=frozenset({2})),
        actions.append,
    )
    assert wrapper.write(b"a") == 1
    assert wrapper.write(b"b") == 0
    assert wrapper.write(b"c") == 1
    assert actions[0]["action"] == "zero_write"
    assert actions[0]["operation"] == 2


def test_drop_reports_external_acceptance_without_forwarding_bytes() -> None:
    serial = FakeSerial()
    wrapper = FaultInjectingSerial(
        serial,
        FaultPlan(drop_write_operations=frozenset({1}), max_write_accept=2),
    )
    assert wrapper.write(b"abcd") == 2
    assert serial.writes == []


def test_duplicate_forwards_selected_accepted_chunk_twice() -> None:
    serial = FakeSerial()
    wrapper = FaultInjectingSerial(
        serial,
        FaultPlan(duplicate_write_operations=frozenset({1})),
    )
    assert wrapper.write(b"abc") == 3
    assert serial.writes == [b"abc", b"abc"]


def test_corrupt_changes_one_selected_byte_only_on_link() -> None:
    serial = FakeSerial()
    wrapper = FaultInjectingSerial(
        serial,
        FaultPlan(corrupt_write_operations=((1, 1),)),
    )
    assert wrapper.write(b"abc") == 3
    assert serial.writes == [b"acc"]


def test_pyserial_provider_rejects_ambiguous_usb_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from hilrig.protocol_test import serial_port

    ports = [
        SimpleNamespace(device="COM1", description="one", vid=1, pid=2, serial_number="A"),
        SimpleNamespace(device="COM2", description="two", vid=1, pid=2, serial_number="B"),
    ]
    monkeypatch.setattr(
        serial_port,
        "_load_pyserial",
        lambda: (object(), SimpleNamespace(comports=lambda: ports)),
    )
    with pytest.raises(serial_port.SerialSelectionError, match="ambiguous.*COM1.*COM2"):
        serial_port.PySerialProvider().resolve(SerialSelector(vid=1, pid=2))
