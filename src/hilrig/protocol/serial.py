"""Windows USB CDC discovery and pySerial connection setup."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from hilrig.exceptions import ProtocolDependencyError, ProtocolSessionError, SerialDiscoveryError


@dataclass(frozen=True, slots=True)
class SerialConnectionSettings:
    """Explicit line coding and flow-control settings for the RIG's CDC port."""

    device_description: str = "USB Serial Device"
    baud_rate: int = 115_200
    byte_size: int = 8
    parity: str = "N"
    stop_bits: int = 1
    read_timeout_s: float = 0.0
    write_timeout_s: float = 1.0
    dtr: bool = False
    rts: bool = False
    device: str | None = None

    def __post_init__(self) -> None:
        if self.device is not None and (
            not isinstance(self.device, str) or not self.device.strip()
        ):
            raise ValueError("device must be a non-empty string or None")
        if not isinstance(self.device_description, str) or not self.device_description:
            raise ValueError("device_description must be a non-empty string")
        if self.baud_rate != 115_200:
            raise ValueError("the RIG USB serial baud rate must be 115200")
        if self.byte_size != 8:
            raise ValueError("the RIG USB serial byte size must be 8")
        if self.parity != "N":
            raise ValueError("the RIG USB serial parity must be none ('N')")
        if self.stop_bits != 1:
            raise ValueError("the RIG USB serial stop bits must be 1")
        if self.dtr or self.rts:
            raise ValueError("DTR and RTS must remain disabled for the current RIG")


def discover_serial_port(
    *,
    description: str = "USB Serial Device",
    comports: Callable[[], Iterable[object]] | None = None,
) -> str:
    """Return the first port whose reported description is an exact match."""
    if not isinstance(description, str) or not description:
        raise ValueError("description must be a non-empty string")
    if comports is None:
        try:
            list_ports = import_module("serial.tools.list_ports")
        except ImportError as error:
            raise ProtocolDependencyError(
                "Serial discovery requires the pyserial package"
            ) from error
        comports = list_ports.comports

    for port in comports():
        if getattr(port, "description", None) == description:
            device = getattr(port, "device", None)
            if not isinstance(device, str) or not device:
                raise SerialDiscoveryError(
                    f"Serial device {description!r} did not report a usable COM port"
                )
            return device
    raise SerialDiscoveryError(f"No serial device named exactly {description!r} was found")


def open_serial_port(
    settings: SerialConnectionSettings | None = None,
    *,
    serial_factory: Callable[..., Any] | None = None,
    comports: Callable[[], Iterable[object]] | None = None,
) -> Any:
    """Discover and open the RIG using explicit non-blocking pySerial settings."""
    if settings is None:
        settings = SerialConnectionSettings()
    elif not isinstance(settings, SerialConnectionSettings):
        raise TypeError("settings must be SerialConnectionSettings or None")

    device = settings.device or discover_serial_port(
        description=settings.device_description,
        comports=comports,
    )
    if serial_factory is None:
        try:
            serial_module = import_module("serial")
        except ImportError as error:
            raise ProtocolDependencyError(
                "Opening the RIG serial port requires the pyserial package"
            ) from error
        serial_factory = serial_module.Serial

    serial_port = serial_factory(
        port=None,
        baudrate=settings.baud_rate,
        bytesize=settings.byte_size,
        parity=settings.parity,
        stopbits=settings.stop_bits,
        timeout=settings.read_timeout_s,
        write_timeout=settings.write_timeout_s,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    try:
        # Set the requested inactive signal state before opening so pySerial applies
        # it as the port is established rather than deliberately toggling it later.
        serial_port.dtr = settings.dtr
        serial_port.rts = settings.rts
        serial_port.port = device
        serial_port.open()
    except Exception as error:
        with suppress(Exception):
            serial_port.close()
        raise ProtocolSessionError(f"Could not open HIL-RIG serial port {device}") from error
    return serial_port


__all__ = ["SerialConnectionSettings", "discover_serial_port", "open_serial_port"]
