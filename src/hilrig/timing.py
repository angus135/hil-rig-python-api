"""Conversion of user-facing time units into execution ticks."""

from decimal import Decimal, InvalidOperation

from hilrig.exceptions import TimingError
from hilrig.models.configuration import FrequencyMode

TimeValue = int | float | Decimal
TimeRange = tuple[TimeValue, TimeValue]


def resolve_timestamp(
    frequency_mode: FrequencyMode,
    *,
    at_tick: int | None = None,
    at_ms: TimeValue | None = None,
    at_s: TimeValue | None = None,
) -> int:
    """Resolve exactly one user-facing timestamp to a non-negative tick."""
    supplied = sum(value is not None for value in (at_tick, at_ms, at_s))
    if supplied != 1:
        raise TimingError("Specify exactly one of at_tick, at_ms, or at_s")

    if at_tick is not None:
        if not isinstance(at_tick, int) or isinstance(at_tick, bool):
            raise TimingError("at_tick must be an integer")
        if at_tick < 0:
            raise TimingError("Timestamps must be non-negative")
        return at_tick

    if at_ms is not None:
        ticks = _decimal_time(at_ms, name="at_ms") * frequency_mode.hertz / 1_000
        return _exact_tick(ticks, name="at_ms", frequency_mode=frequency_mode)

    ticks = _decimal_time(at_s, name="at_s") * frequency_mode.hertz
    return _exact_tick(ticks, name="at_s", frequency_mode=frequency_mode)


def resolve_time_range(
    frequency_mode: FrequencyMode,
    *,
    ticks: TimeRange | None = None,
    milliseconds: TimeRange | None = None,
    seconds: TimeRange | None = None,
) -> tuple[int, int]:
    """Resolve exactly one two-value time range into inclusive tick bounds."""
    supplied = sum(value is not None for value in (ticks, milliseconds, seconds))
    if supplied != 1:
        raise TimingError("Specify exactly one tick, millisecond, or second range")

    if ticks is not None:
        start, end = _two_values(ticks)
        bounds = (
            resolve_timestamp(frequency_mode, at_tick=start),
            resolve_timestamp(frequency_mode, at_tick=end),
        )
    elif milliseconds is not None:
        start, end = _two_values(milliseconds)
        bounds = (
            resolve_timestamp(frequency_mode, at_ms=start),
            resolve_timestamp(frequency_mode, at_ms=end),
        )
    else:
        start, end = _two_values(seconds)
        bounds = (
            resolve_timestamp(frequency_mode, at_s=start),
            resolve_timestamp(frequency_mode, at_s=end),
        )

    if bounds[0] > bounds[1]:
        raise TimingError("The start of a time range must not be after its end")
    return bounds


def _decimal_time(value: TimeValue | None, *, name: str) -> Decimal:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TimingError(f"{name} must be a number")
    try:
        converted = Decimal(str(value))
    except InvalidOperation as error:
        raise TimingError(f"{name} must be a finite number") from error
    if not converted.is_finite():
        raise TimingError(f"{name} must be a finite number")
    if converted < 0:
        raise TimingError("Timestamps must be non-negative")
    return converted


def _exact_tick(ticks: Decimal, *, name: str, frequency_mode: FrequencyMode) -> int:
    integral = ticks.to_integral_value()
    if ticks != integral:
        raise TimingError(f"{name} does not align with a whole tick in {frequency_mode.name} mode")
    return int(integral)


def _two_values(values: TimeRange | None) -> TimeRange:
    if not isinstance(values, tuple) or len(values) != 2:
        raise TimingError("A time range must be a two-value tuple")
    return values
