from decimal import Decimal

import pytest

from hilrig import FrequencyMode
from hilrig.exceptions import TimingError
from hilrig.timing import resolve_time_range, resolve_timestamp


@pytest.mark.parametrize(
    ("mode", "arguments", "expected"),
    [
        (FrequencyMode.HZ_100, {"at_tick": 7}, 7),
        (FrequencyMode.HZ_100, {"at_ms": 20}, 2),
        (FrequencyMode.HZ_1K, {"at_ms": 1}, 1),
        (FrequencyMode.HZ_10K, {"at_ms": Decimal("0.1")}, 1),
        (FrequencyMode.HZ_100, {"at_s": 1.5}, 150),
    ],
)
def test_timestamp_conversion(mode, arguments, expected: int) -> None:
    assert resolve_timestamp(mode, **arguments) == expected


def test_exactly_one_timestamp_unit_is_required() -> None:
    with pytest.raises(TimingError, match="exactly one"):
        resolve_timestamp(FrequencyMode.HZ_1K)

    with pytest.raises(TimingError, match="exactly one"):
        resolve_timestamp(FrequencyMode.HZ_1K, at_tick=1, at_ms=1)


def test_non_aligned_time_is_rejected_instead_of_rounded() -> None:
    with pytest.raises(TimingError, match="whole tick"):
        resolve_timestamp(FrequencyMode.HZ_100, at_ms=5)


def test_time_range_is_converted_and_order_checked() -> None:
    assert resolve_time_range(FrequencyMode.HZ_1K, milliseconds=(10, 20)) == (10, 20)

    with pytest.raises(TimingError, match="must not be after"):
        resolve_time_range(FrequencyMode.HZ_1K, ticks=(20, 10))
