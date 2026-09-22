from dataclasses import dataclass

import pytest

from hilrig import SerialConnectionSettings, SerialDiscoveryError, discover_serial_port
from hilrig.protocol.serial import open_serial_port


@dataclass
class _PortInfo:
    device: str
    description: str


def test_discovery_matches_windows_com_suffix() -> None:
    ports = [
        _PortInfo("COM4", "USB Serial Device (COM4)"),
        _PortInfo("COM5", "USB Serial Device (COM5)"),
    ]

    assert discover_serial_port(comports=lambda: ports) == "COM4"


def test_discovery_requires_an_exact_base_description_match() -> None:
    ports = [
        _PortInfo("COM4", "USB Serial Device Pro (COM4)"),
        _PortInfo("COM5", "usb serial device (COM5)"),
    ]

    with pytest.raises(SerialDiscoveryError, match="matching"):
        discover_serial_port(comports=lambda: ports)


def test_discovery_returns_the_first_exact_match() -> None:
    ports = [
        _PortInfo("COM7", "USB Serial Device"),
        _PortInfo("COM3", "USB Serial Device"),
    ]

    assert discover_serial_port(comports=lambda: ports) == "COM7"


def test_open_declares_requested_line_settings_and_disables_dtr_rts() -> None:
    created: list[object] = []

    class SerialPort:
        def __init__(self, **kwargs: object) -> None:
            self.arguments = kwargs
            self.dtr = True
            self.rts = True
            self.port = None
            self.opened_with: tuple[str | None, bool, bool] | None = None
            created.append(self)

        def open(self) -> None:
            self.opened_with = (self.port, self.dtr, self.rts)

        def close(self) -> None:
            pass

    serial_port = open_serial_port(
        SerialConnectionSettings(),
        serial_factory=SerialPort,
        comports=lambda: [_PortInfo("COM12", "USB Serial Device (COM12)")],
    )

    assert serial_port.arguments == {
        "port": None,
        "baudrate": 115_200,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 0.0,
        "write_timeout": 1.0,
        "xonxoff": False,
        "rtscts": False,
        "dsrdtr": False,
    }
    assert serial_port.opened_with == ("COM12", False, False)


def test_open_uses_an_explicit_device_without_running_discovery() -> None:
    discovered = False

    class SerialPort:
        def __init__(self, **kwargs: object) -> None:
            self.dtr = True
            self.rts = True
            self.port = None
            self.opened_with: tuple[str | None, bool, bool] | None = None

        def open(self) -> None:
            self.opened_with = (self.port, self.dtr, self.rts)

        def close(self) -> None:
            pass

    def comports() -> list[object]:
        nonlocal discovered
        discovered = True
        return []

    serial_port = open_serial_port(
        SerialConnectionSettings(device="COM2"),
        serial_factory=SerialPort,
        comports=comports,
    )

    assert not discovered
    assert serial_port.opened_with == ("COM2", False, False)
