"""Read-only, channel-oriented facade over a captured-run SQLite database."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path

from hilrig.results.models import (
    ANALOGUE_INPUT_CHANNEL_COUNT,
    DIGITAL_INPUT_CHANNEL_COUNT,
    PWM_INPUT_CHANNEL_COUNT,
    AnalogueInputSample,
    CapturedApplicationError,
    CapturedRunMetadata,
    CapturedTickResult,
    CommunicationCapture,
    CommunicationPeripheral,
    DigitalInputSample,
    PWMInputSample,
)
from hilrig.results.sqlite_store import (
    iter_application_errors,
    iter_communications,
    iter_ticks,
    read_metadata,
    read_tick,
    validate_capture_database,
)


class CapturedRunIR:
    """Logical captured-run IR backed by an authoritative SQLite file.

    Callers and future assertion evaluators use this object rather than writing SQL.
    Each iterator opens a read-only/query-only connection and streams rows, so large
    ranges do not need to be loaded into memory at once.
    """

    __slots__ = ("_database_path",)

    def __init__(self, database_path: str | Path) -> None:
        resolved = Path(database_path).expanduser().resolve()
        validate_capture_database(resolved)
        self._database_path = resolved

    @classmethod
    def open(cls, database_path: str | Path) -> CapturedRunIR:
        """Open and validate an existing capture database."""
        return cls(database_path)

    @property
    def database_path(self) -> Path:
        """Return the authoritative SQLite file path."""
        return self._database_path

    @property
    def metadata(self) -> CapturedRunMetadata:
        """Read the latest immutable run summary."""
        return read_metadata(self._database_path)

    def tick_at(self, tick: int) -> CapturedTickResult | None:
        """Return one fixed result, or ``None`` when that tick was not received."""
        _non_negative_tick(tick, name="tick")
        return read_tick(self._database_path, tick)

    def iter_ticks(
        self,
        *,
        from_tick: int = 0,
        until_tick: int | None = None,
    ) -> Iterator[CapturedTickResult]:
        """Stream fixed results over an inclusive chronological range."""
        _validate_range(from_tick, until_tick)
        return iter_ticks(
            self._database_path,
            from_tick=from_tick,
            until_tick=until_tick,
        )

    def digital_input(self, *, channel: int) -> DigitalInputSeries:
        """Return a query handle for one digital input channel."""
        _channel(channel, DIGITAL_INPUT_CHANNEL_COUNT, peripheral="digital input")
        return DigitalInputSeries(self, channel)

    def analogue_input(self, *, channel: int) -> AnalogueInputSeries:
        """Return a query handle for one analogue input channel."""
        _channel(channel, ANALOGUE_INPUT_CHANNEL_COUNT, peripheral="analogue input")
        return AnalogueInputSeries(self, channel)

    def pwm_input(self, *, channel: int) -> PWMInputSeries:
        """Return a query handle for one PWM input channel."""
        _channel(channel, PWM_INPUT_CHANNEL_COUNT, peripheral="PWM input")
        return PWMInputSeries(self, channel)

    def iter_communications(
        self,
        *,
        peripheral: CommunicationPeripheral | None = None,
        channel: int | None = None,
        from_tick: int = 0,
        until_tick: int | None = None,
    ) -> Iterator[CommunicationCapture]:
        """Stream raw communication captures matching the supplied filters."""
        if peripheral is not None and not isinstance(peripheral, CommunicationPeripheral):
            raise TypeError("peripheral must be a CommunicationPeripheral value or None")
        if channel is not None:
            _non_negative_tick(channel, name="channel")
        _validate_range(from_tick, until_tick)
        return iter_communications(
            self._database_path,
            tick_period_ns=self.metadata.tick_period_ns,
            peripheral=peripheral,
            channel=channel,
            from_tick=from_tick,
            until_tick=until_tick,
        )

    def iter_application_errors(self) -> Iterator[CapturedApplicationError]:
        """Stream retained application diagnostics in arrival order."""
        return iter_application_errors(self._database_path)

    def to_manifest_dict(self) -> dict[str, object]:
        """Return a small JSON-compatible summary; time-series data stays in SQLite."""
        metadata = self.metadata
        return {
            "result_ir_version": metadata.schema_version,
            "test_id": metadata.test_id_hex,
            "run_id": metadata.run_id_hex,
            "test_name": metadata.test_name,
            "timing": {
                "tick_period_ns": metadata.tick_period_ns,
                "expected_tick_count": metadata.expected_tick_count,
                "received_tick_count": metadata.received_tick_count,
                "first_tick": metadata.first_tick,
                "last_tick": metadata.last_tick,
            },
            "capture": {
                "status": metadata.status.value,
                "created_at": metadata.created_at,
                "finalized_at": metadata.finalized_at,
            },
            "rig": {
                "application_protocol_version": metadata.application_protocol_version,
                "firmware_version": metadata.firmware_version,
            },
            "authoritative_data": self._database_path.name,
        }

    def write_manifest_json(self, path: str | Path, *, indent: int | None = 2) -> Path:
        """Write the small run manifest, excluding bulk time-series values."""
        output = _output_path(path, suffix=".json")
        output.write_text(
            json.dumps(self.to_manifest_dict(), indent=indent) + "\n",
            encoding="utf-8",
        )
        return output

    def write_fixed_results_csv(self, path: str | Path) -> Path:
        """Export the wide fixed-result table as a human-readable CSV log."""
        output = _output_path(path, suffix=".csv")
        header = ["tick", "time_ns", "condition", "problem_detail"]
        header.extend(f"digital_input_{index}" for index in range(DIGITAL_INPUT_CHANNEL_COUNT))
        header.extend(f"analogue_input_{index}_uv" for index in range(ANALOGUE_INPUT_CHANNEL_COUNT))
        for index in range(PWM_INPUT_CHANNEL_COUNT):
            header.extend((f"pwm_input_{index}_period_ns", f"pwm_input_{index}_duty_permyriad"))
        period = self.metadata.tick_period_ns
        with output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            for result in self.iter_ticks():
                row: list[object] = [
                    result.tick,
                    result.tick * period,
                    result.condition.value,
                    "" if result.problem_detail is None else result.problem_detail,
                ]
                row.extend("" if value is None else int(value) for value in result.digital_inputs)
                row.extend("" if value is None else value for value in result.analogue_inputs_uv)
                for measurement in result.pwm_inputs:
                    if measurement is None:
                        row.extend(("", ""))
                    else:
                        row.extend((measurement.period_ns, measurement.duty_permyriad))
                writer.writerow(row)
        return output

    def write_communication_results_csv(self, path: str | Path) -> Path:
        """Export raw communication payloads as hexadecimal CSV fields."""
        output = _output_path(path, suffix=".csv")
        with output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                (
                    "capture_id",
                    "tick",
                    "time_ns",
                    "ordinal",
                    "peripheral",
                    "channel",
                    "payload_size",
                    "payload_hex",
                )
            )
            for result in self.iter_communications():
                writer.writerow(
                    (
                        result.capture_id,
                        result.tick,
                        result.time_ns,
                        result.ordinal,
                        result.peripheral.value,
                        result.channel,
                        result.payload_size,
                        f"0x{result.payload.hex()}",
                    )
                )
        return output

    def write_application_errors_csv(self, path: str | Path) -> Path:
        """Export application diagnostics without interpreting their payloads."""
        output = _output_path(path, suffix=".csv")
        with output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                (
                    "error_id",
                    "tick",
                    "time_ns",
                    "category",
                    "recoverable",
                    "detail",
                    "diagnostic_data_hex",
                )
            )
            period = self.metadata.tick_period_ns
            for error in self.iter_application_errors():
                writer.writerow(
                    (
                        error.error_id,
                        "" if error.tick is None else error.tick,
                        "" if error.tick is None else error.tick * period,
                        error.category,
                        int(error.recoverable),
                        error.detail,
                        f"0x{error.diagnostic_data.hex()}",
                    )
                )
        return output


class DigitalInputSeries:
    """Read-only query view of one digital input time series."""

    __slots__ = ("_run", "_tick_period_ns", "channel")

    def __init__(self, run: CapturedRunIR, channel: int) -> None:
        self._run = run
        self._tick_period_ns = run.metadata.tick_period_ns
        self.channel = channel

    def sample_at(self, tick: int) -> DigitalInputSample | None:
        result = self._run.tick_at(tick)
        return None if result is None else self._sample(result)

    def iter_samples(
        self,
        *,
        from_tick: int = 0,
        until_tick: int | None = None,
    ) -> Iterator[DigitalInputSample]:
        return (
            self._sample(result)
            for result in self._run.iter_ticks(from_tick=from_tick, until_tick=until_tick)
        )

    def _sample(self, result: CapturedTickResult) -> DigitalInputSample:
        return DigitalInputSample(
            tick=result.tick,
            time_ns=result.tick * self._tick_period_ns,
            value=result.digital_inputs[self.channel],
            condition=result.condition,
            problem_detail=result.problem_detail,
        )


class AnalogueInputSeries:
    """Read-only query view of one analogue input time series."""

    __slots__ = ("_run", "_tick_period_ns", "channel")

    def __init__(self, run: CapturedRunIR, channel: int) -> None:
        self._run = run
        self._tick_period_ns = run.metadata.tick_period_ns
        self.channel = channel

    def sample_at(self, tick: int) -> AnalogueInputSample | None:
        result = self._run.tick_at(tick)
        return None if result is None else self._sample(result)

    def iter_samples(
        self,
        *,
        from_tick: int = 0,
        until_tick: int | None = None,
    ) -> Iterator[AnalogueInputSample]:
        return (
            self._sample(result)
            for result in self._run.iter_ticks(from_tick=from_tick, until_tick=until_tick)
        )

    def _sample(self, result: CapturedTickResult) -> AnalogueInputSample:
        return AnalogueInputSample(
            tick=result.tick,
            time_ns=result.tick * self._tick_period_ns,
            microvolts=result.analogue_inputs_uv[self.channel],
            condition=result.condition,
            problem_detail=result.problem_detail,
        )


class PWMInputSeries:
    """Read-only query view of one PWM input time series."""

    __slots__ = ("_run", "_tick_period_ns", "channel")

    def __init__(self, run: CapturedRunIR, channel: int) -> None:
        self._run = run
        self._tick_period_ns = run.metadata.tick_period_ns
        self.channel = channel

    def sample_at(self, tick: int) -> PWMInputSample | None:
        result = self._run.tick_at(tick)
        return None if result is None else self._sample(result)

    def iter_samples(
        self,
        *,
        from_tick: int = 0,
        until_tick: int | None = None,
    ) -> Iterator[PWMInputSample]:
        return (
            self._sample(result)
            for result in self._run.iter_ticks(from_tick=from_tick, until_tick=until_tick)
        )

    def _sample(self, result: CapturedTickResult) -> PWMInputSample:
        return PWMInputSample(
            tick=result.tick,
            time_ns=result.tick * self._tick_period_ns,
            measurement=result.pwm_inputs[self.channel],
            condition=result.condition,
            problem_detail=result.problem_detail,
        )


def _validate_range(from_tick: int, until_tick: int | None) -> None:
    _non_negative_tick(from_tick, name="from_tick")
    if until_tick is not None:
        _non_negative_tick(until_tick, name="until_tick")
        if from_tick > until_tick:
            raise ValueError("from_tick must not be after until_tick")


def _non_negative_tick(value: object, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _channel(value: object, count: int, *, peripheral: str) -> int:
    channel = _non_negative_tick(value, name="channel")
    if channel >= count:
        raise ValueError(f"{peripheral} channel must be between 0 and {count - 1}")
    return channel


def _output_path(path: str | Path, *, suffix: str) -> Path:
    output = Path(path).expanduser().resolve()
    if output.suffix.lower() != suffix:
        raise ValueError(f"Output path must end in {suffix}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output
