"""Serial discovery, pyserial adapter, and deterministic link-fault seam."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .models import SerialDevice, SerialSelector


class SerialDependencyError(RuntimeError):
    pass


class SerialSelectionError(RuntimeError):
    pass


class SerialIOError(RuntimeError):
    pass


class SerialWriteTimeout(SerialIOError):
    pass


class SerialPort(Protocol):
    @property
    def is_open(self) -> bool: ...

    @property
    def identity(self) -> SerialDevice: ...

    def close(self) -> None: ...

    def read(self, size: int) -> bytes: ...

    def write(self, data: bytes) -> int | None: ...

    @property
    def in_waiting(self) -> int: ...

    def reset_input_buffer(self) -> None: ...

    def reset_output_buffer(self) -> None: ...


class SerialProvider(Protocol):
    def resolve(self, selector: SerialSelector) -> SerialDevice: ...

    def open(self, device: SerialDevice, baud: int) -> SerialPort: ...


def _load_pyserial() -> tuple[object, object]:
    try:
        import serial  # type: ignore[import-not-found]
        from serial.tools import list_ports  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SerialDependencyError(
            "pyserial is required for hardware tests; install with "
            'python -m pip install -e ".[hardware-test]"'
        ) from exc
    return serial, list_ports


class PySerialPort:
    """Narrow normalization layer over one open pyserial handle."""

    def __init__(self, handle: object, identity: SerialDevice, serial_module: object) -> None:
        self._handle = handle
        self._identity = identity
        self._serial_module = serial_module

    @property
    def is_open(self) -> bool:
        return bool(getattr(self._handle, "is_open", False))

    @property
    def identity(self) -> SerialDevice:
        return self._identity

    def _translate(self, operation: str, exc: Exception) -> SerialIOError:
        timeout_type = getattr(self._serial_module, "SerialTimeoutException")
        if isinstance(exc, timeout_type):
            return SerialWriteTimeout(f"serial {operation} timed out: {exc}")
        return SerialIOError(f"serial {operation} failed: {exc}")

    def close(self) -> None:
        try:
            self._handle.close()
        except Exception as exc:
            serial_exception = getattr(self._serial_module, "SerialException")
            if isinstance(exc, serial_exception):
                raise self._translate("close", exc) from exc
            raise

    def read(self, size: int) -> bytes:
        try:
            return bytes(self._handle.read(size))
        except Exception as exc:
            serial_exception = getattr(self._serial_module, "SerialException")
            if isinstance(exc, serial_exception):
                raise self._translate("read", exc) from exc
            raise

    def write(self, data: bytes) -> int | None:
        try:
            return self._handle.write(data)
        except Exception as exc:
            serial_exception = getattr(self._serial_module, "SerialException")
            timeout_exception = getattr(self._serial_module, "SerialTimeoutException")
            if isinstance(exc, (serial_exception, timeout_exception)):
                raise self._translate("write", exc) from exc
            raise

    @property
    def in_waiting(self) -> int:
        try:
            return int(self._handle.in_waiting)
        except Exception as exc:
            serial_exception = getattr(self._serial_module, "SerialException")
            if isinstance(exc, serial_exception):
                raise self._translate("read readiness", exc) from exc
            raise

    def reset_input_buffer(self) -> None:
        try:
            self._handle.reset_input_buffer()
        except Exception as exc:
            serial_exception = getattr(self._serial_module, "SerialException")
            if isinstance(exc, serial_exception):
                raise self._translate("input reset", exc) from exc
            raise

    def reset_output_buffer(self) -> None:
        try:
            self._handle.reset_output_buffer()
        except Exception as exc:
            serial_exception = getattr(self._serial_module, "SerialException")
            if isinstance(exc, serial_exception):
                raise self._translate("output reset", exc) from exc
            raise


class PySerialProvider:
    """Resolve an explicit path or unambiguous USB identity and open it nonblocking."""

    def resolve(self, selector: SerialSelector) -> SerialDevice:
        _, list_ports = _load_pyserial()
        ports = list(list_ports.comports())
        if selector.port is not None:
            for port in ports:
                if port.device == selector.port:
                    return _port_info(port)
            # Explicit paths can be valid even when a backend omits them from discovery.
            return SerialDevice(port=selector.port)

        matches: list[SerialDevice] = []
        for port in ports:
            if selector.vid is not None and port.vid != selector.vid:
                continue
            if selector.pid is not None and port.pid != selector.pid:
                continue
            if selector.serial_number is not None and port.serial_number != selector.serial_number:
                continue
            matches.append(_port_info(port))
        if not matches:
            raise SerialSelectionError("no serial device matches the requested USB identity")
        if len(matches) != 1:
            candidates = ", ".join(
                (
                    f"{item.port} (VID={_hex(item.vid)}, PID={_hex(item.pid)}, "
                    f"serial={item.serial_number!r})"
                )
                for item in matches
            )
            raise SerialSelectionError(f"serial selector is ambiguous; candidates: {candidates}")
        return matches[0]

    def open(self, device: SerialDevice, baud: int) -> SerialPort:
        if type(baud) is not int or baud <= 0:
            raise ValueError("baud must be a positive integer")
        serial, _ = _load_pyserial()
        try:
            handle = serial.Serial(
                port=device.port,
                baudrate=baud,
                timeout=0,
                write_timeout=0,
            )
        except Exception as exc:
            serial_exception = getattr(serial, "SerialException")
            if isinstance(exc, serial_exception):
                raise SerialIOError(f"failed to open {device.port}: {exc}") from exc
            raise
        return PySerialPort(handle, device, serial)


def _port_info(port: object) -> SerialDevice:
    return SerialDevice(
        port=str(port.device),
        description=getattr(port, "description", None),
        vid=getattr(port, "vid", None),
        pid=getattr(port, "pid", None),
        serial_number=getattr(port, "serial_number", None),
    )


def _hex(value: int | None) -> str:
    return "?" if value is None else f"0x{value:04X}"


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """Explicit deterministic serial fault operations. All features default off."""

    max_read_chunk: int | None = None
    max_write_accept: int | None = None
    zero_write_operations: frozenset[int] = frozenset()
    delay_read_operations: frozenset[int] = frozenset()
    delay_write_operations: frozenset[int] = frozenset()
    drop_write_operations: frozenset[int] = frozenset()
    duplicate_write_operations: frozenset[int] = frozenset()
    corrupt_write_operations: tuple[tuple[int, int], ...] = ()
    delay_ms: int = 0
    seed: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_read_chunk", "max_write_accept"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be positive when supplied")
        if type(self.delay_ms) is not int or self.delay_ms < 0:
            raise ValueError("delay_ms must be non-negative")
        for operation, byte_index in self.corrupt_write_operations:
            if operation < 1 or byte_index < 0:
                raise ValueError(
                    "corrupt write operations use 1-based operations and non-negative offsets"
                )

    @property
    def enabled(self) -> bool:
        return any(
            (
                self.max_read_chunk is not None,
                self.max_write_accept is not None,
                self.zero_write_operations,
                self.delay_read_operations,
                self.delay_write_operations,
                self.drop_write_operations,
                self.duplicate_write_operations,
                self.corrupt_write_operations,
            )
        )


@dataclass(slots=True)
class FaultInjectingSerial:
    """Wrap serial I/O with deterministic shaping while preserving accepted-count semantics."""

    inner: SerialPort
    plan: FaultPlan
    log_action: Callable[[dict[str, object]], None] | None = None
    _read_operation: int = field(init=False, default=0)
    _write_operation: int = field(init=False, default=0)

    @property
    def is_open(self) -> bool:
        return self.inner.is_open

    @property
    def identity(self) -> SerialDevice:
        return self.inner.identity

    @property
    def in_waiting(self) -> int:
        return self.inner.in_waiting

    def close(self) -> None:
        self.inner.close()

    def reset_input_buffer(self) -> None:
        self.inner.reset_input_buffer()

    def reset_output_buffer(self) -> None:
        self.inner.reset_output_buffer()

    def _log(self, action: str, operation: int, **details: object) -> None:
        if self.log_action is not None:
            self.log_action({"action": action, "operation": operation, **details})

    def read(self, size: int) -> bytes:
        self._read_operation += 1
        operation = self._read_operation
        if operation in self.plan.delay_read_operations:
            self._log("delay_read", operation, delay_ms=self.plan.delay_ms)
            time.sleep(self.plan.delay_ms / 1000)
        if self.plan.max_read_chunk is not None and size > self.plan.max_read_chunk:
            self._log(
                "limit_read_chunk",
                operation,
                requested=size,
                limited=self.plan.max_read_chunk,
            )
            size = self.plan.max_read_chunk
        return self.inner.read(size)

    def write(self, data: bytes) -> int | None:
        self._write_operation += 1
        operation = self._write_operation
        if operation in self.plan.delay_write_operations:
            self._log("delay_write", operation, delay_ms=self.plan.delay_ms)
            time.sleep(self.plan.delay_ms / 1000)
        offered = data
        if self.plan.max_write_accept is not None and len(offered) > self.plan.max_write_accept:
            self._log(
                "limit_write_chunk",
                operation,
                offered=len(offered),
                limited=self.plan.max_write_accept,
            )
            offered = offered[: self.plan.max_write_accept]
        if operation in self.plan.zero_write_operations:
            self._log("zero_write", operation, offered=len(offered))
            return 0
        corrupt_lookup = dict(self.plan.corrupt_write_operations)
        if operation in corrupt_lookup and offered:
            index = min(corrupt_lookup[operation], len(offered) - 1)
            mutated = bytearray(offered)
            mutated[index] ^= 0x01
            offered = bytes(mutated)
            self._log("corrupt_write", operation, byte_index=index)
        if operation in self.plan.drop_write_operations:
            accepted = len(offered)
            self._log("drop_accepted_write", operation, accepted=accepted)
            return accepted
        accepted = self.inner.write(offered)
        accepted_count = 0 if accepted is None else accepted
        if operation in self.plan.duplicate_write_operations and accepted_count:
            duplicate = offered[:accepted_count]
            self.inner.write(duplicate)
            self._log("duplicate_write", operation, duplicated=accepted_count)
        return accepted


@dataclass(slots=True)
class FaultInjectingProvider:
    """Provider decorator that applies one deterministic fault plan per opened link."""

    inner: SerialProvider
    plan: FaultPlan
    log_action: Callable[[dict[str, object]], None] | None = None

    def resolve(self, selector: SerialSelector) -> SerialDevice:
        return self.inner.resolve(selector)

    def open(self, device: SerialDevice, baud: int) -> SerialPort:
        return FaultInjectingSerial(self.inner.open(device, baud), self.plan, self.log_action)
