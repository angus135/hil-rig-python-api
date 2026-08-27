import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from hilrig import FrequencyMode, StartMode
from hilrig import Test as HilRigTest
from hilrig.models.assertions import (
    AnalogueInputNearAssertion,
    AnalogueInputRemainAboveAssertion,
    AnalogueInputRemainBelowAssertion,
    AnalogueInputRemainWithinAssertion,
    AnalogueInputWithinAssertion,
    DigitalInputRemainLowAssertion,
    PwmInputDutyCycleNearAssertion,
    PwmInputDutyCycleRemainWithinAssertion,
    PwmInputFrequencyNearAssertion,
    PwmInputFrequencyRemainWithinAssertion,
    PwmInputPeriodNearAssertion,
    PwmInputWaveformNearAssertion,
)


def test_digital_remain_low_supports_all_time_units() -> None:
    test = HilRigTest(name="Remain low")
    digital_input = test.digital_input(channel=0)

    test.expect(digital_input).remain_low(from_tick=10, until_tick=20)
    test.expect(digital_input).remain_low(from_ms=30, until_ms=40)
    test.expect(digital_input).remain_low(from_s=0.05, until_s=0.06)

    assertions = tuple(test.assertions)
    assert all(isinstance(item, DigitalInputRemainLowAssertion) for item in assertions)
    assert [(item.from_tick, item.until_tick) for item in assertions] == [
        (10, 20),
        (30, 40),
        (50, 60),
    ]


def test_pwm_point_assertions_store_validated_values_and_converted_ticks() -> None:
    test = HilRigTest(name="PWM point assertions")
    test.configure(frequency_mode=FrequencyMode.HZ_10K, start_mode=StartMode.IMMEDIATE)
    pwm_input = test.pwm_input(channel=0)

    test.expect(pwm_input).period_near(period_ns=20_000, tolerance_ns=200, at_tick=100)
    test.expect(pwm_input).frequency_near(
        frequency_hz=50_000,
        tolerance_hz=500,
        at_ms=20,
    )
    test.expect(pwm_input).duty_cycle_near(
        duty_cycle=0.5,
        duty_cycle_tolerance=0.01,
        at_s=0.03,
    )
    test.expect(pwm_input).waveform_near(
        frequency_hz=50_000,
        frequency_tolerance_hz=500,
        duty_cycle=0.5,
        duty_cycle_tolerance=0.01,
        at_tick=400,
    )

    period, frequency, duty, waveform = tuple(test.assertions)
    assert isinstance(period, PwmInputPeriodNearAssertion)
    assert (period.period_ns, period.tolerance_ns, period.timestamp) == (20_000, 200, 100)
    assert isinstance(frequency, PwmInputFrequencyNearAssertion)
    assert (frequency.frequency_hz, frequency.tolerance_hz, frequency.timestamp) == (
        50_000,
        500,
        200,
    )
    assert isinstance(duty, PwmInputDutyCycleNearAssertion)
    assert (duty.duty_cycle, duty.duty_cycle_tolerance, duty.timestamp) == (0.5, 0.01, 300)
    assert isinstance(waveform, PwmInputWaveformNearAssertion)
    assert waveform.timestamp == 400
    assert [item.assertion_id for item in test.assertions] == [0, 1, 2, 3]


def test_pwm_range_assertions_support_converted_ranges() -> None:
    test = HilRigTest(name="PWM range assertions")
    test.configure(frequency_mode=FrequencyMode.HZ_10K, start_mode=StartMode.IMMEDIATE)
    pwm_input = test.pwm_input(channel=0)

    test.expect(pwm_input).frequency_remain_within(
        minimum_hz=49_500,
        maximum_hz=50_500,
        from_ms=10,
        until_ms=50,
    )
    test.expect(pwm_input).duty_cycle_remain_within(
        minimum_duty_cycle=0.49,
        maximum_duty_cycle=0.51,
        from_s=0.06,
        until_s=0.08,
    )

    frequency, duty = tuple(test.assertions)
    assert isinstance(frequency, PwmInputFrequencyRemainWithinAssertion)
    assert (frequency.minimum_hz, frequency.maximum_hz) == (49_500, 50_500)
    assert (frequency.from_tick, frequency.until_tick) == (100, 500)
    assert isinstance(duty, PwmInputDutyCycleRemainWithinAssertion)
    assert (duty.minimum_duty_cycle, duty.maximum_duty_cycle) == (0.49, 0.51)
    assert (duty.from_tick, duty.until_tick) == (600, 800)


def test_analogue_assertions_convert_public_volts_to_signed_microvolts() -> None:
    test = HilRigTest(name="Analogue assertions")
    analogue_input = test.analogue_input(channel=0).configure()

    test.expect(analogue_input).near(target_v=5, tolerance_v=0.005, at_tick=100)
    test.expect(analogue_input).within(minimum_v=-1.2, maximum_v=1.2, at_ms=200)
    test.expect(analogue_input).remain_within(
        minimum_v=Decimal("4.9"),
        maximum_v=Decimal("5.1"),
        from_tick=300,
        until_tick=500,
    )
    test.expect(analogue_input).remain_above(
        threshold_v=-0.25,
        from_ms=600,
        until_ms=700,
    )
    test.expect(analogue_input).remain_below(
        threshold_v=1,
        from_s=0.8,
        until_s=0.9,
    )

    near, within, remain_within, remain_above, remain_below = tuple(test.assertions)
    assert isinstance(near, AnalogueInputNearAssertion)
    assert (near.target_uv, near.tolerance_uv) == (5_000_000, 5_000)
    assert isinstance(within, AnalogueInputWithinAssertion)
    assert (within.minimum_uv, within.maximum_uv) == (-1_200_000, 1_200_000)
    assert isinstance(remain_within, AnalogueInputRemainWithinAssertion)
    assert (remain_within.minimum_uv, remain_within.maximum_uv) == (4_900_000, 5_100_000)
    assert isinstance(remain_above, AnalogueInputRemainAboveAssertion)
    assert remain_above.threshold_uv == -250_000
    assert isinstance(remain_below, AnalogueInputRemainBelowAssertion)
    assert remain_below.threshold_uv == 1_000_000
    assert [item.assertion_id for item in test.assertions] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("channel", [2, 3])
def test_analogue_input_only_accepts_the_two_physical_channels(channel: int) -> None:
    test = HilRigTest(name="Analogue channel limits")

    with pytest.raises(ValueError, match="must be 0 or 1"):
        test.analogue_input(channel=channel)


def test_analogue_input_channel_rejects_bool() -> None:
    test = HilRigTest(name="Analogue channel type")

    with pytest.raises(TypeError, match="integer"):
        test.analogue_input(channel=True)


@pytest.mark.parametrize(
    "command",
    [
        lambda expectation: expectation.period_near(
            period_ns=0,
            tolerance_ns=1,
            at_tick=0,
        ),
        lambda expectation: expectation.frequency_near(
            frequency_hz=float("nan"),
            tolerance_hz=1,
            at_tick=0,
        ),
        lambda expectation: expectation.duty_cycle_near(
            duty_cycle=1.1,
            duty_cycle_tolerance=0.1,
            at_tick=0,
        ),
        lambda expectation: expectation.frequency_remain_within(
            minimum_hz=100,
            maximum_hz=99,
            from_tick=0,
            until_tick=1,
        ),
        lambda expectation: expectation.duty_cycle_remain_within(
            minimum_duty_cycle=0.7,
            maximum_duty_cycle=0.6,
            from_tick=0,
            until_tick=1,
        ),
    ],
)
def test_pwm_assertions_reject_invalid_measurement_values(command) -> None:
    test = HilRigTest(name="Invalid PWM assertion")
    expectation = test.expect(test.pwm_input(channel=0))

    with pytest.raises(ValueError):
        command(expectation)


def test_analogue_assertions_reject_invalid_voltage_values() -> None:
    test = HilRigTest(name="Invalid analogue assertion")
    expectation = test.expect(test.analogue_input(channel=0))

    with pytest.raises(ValueError, match="non-negative"):
        expectation.near(target_v=1, tolerance_v=-0.1, at_tick=0)
    with pytest.raises(ValueError, match="whole microvolt"):
        expectation.near(target_v=0.0000001, tolerance_v=0, at_tick=0)
    with pytest.raises(ValueError, match="ascending order"):
        expectation.within(minimum_v=5, maximum_v=4, at_tick=0)


def test_compiler_represents_new_assertions_and_uses_latest_range_end() -> None:
    test = HilRigTest(name="Compiled extended assertions")
    analogue_input = test.analogue_input(channel=1)
    pwm_input = test.pwm_input(channel=0)
    test.expect(pwm_input).period_near(period_ns=20_000, tolerance_ns=200, at_tick=100)
    test.expect(analogue_input).remain_within(
        minimum_v=4.9,
        maximum_v=5.1,
        from_tick=200,
        until_tick=500,
    )

    compiled = test.compile()

    assert [item.assertion for item in compiled.assertions] == ["period_near", "remain_within"]
    assert dict(compiled.assertions[0].arguments) == {
        "tick": 100,
        "period_ns": 20_000,
        "tolerance_ns": 200,
    }
    assert dict(compiled.assertions[1].arguments) == {
        "from_tick": 200,
        "until_tick": 500,
        "minimum_uv": 4_900_000,
        "maximum_uv": 5_100_000,
    }
    assert compiled.expected_tick_count == 1_501


def test_new_assertions_are_written_to_the_human_excel_view(tmp_path: Path) -> None:
    test = HilRigTest(name="Extended assertion workbook")
    analogue_input = test.analogue_input(channel=0)
    test.expect(analogue_input).near(target_v=5, tolerance_v=0.005, at_tick=20)

    workbook_path = test.compile().write_excel(tmp_path / "assertions.xlsx")

    with zipfile.ZipFile(workbook_path) as workbook:
        assertions_xml = workbook.read("xl/worksheets/sheet4.xml").decode("utf-8")
    assert "near" in assertions_xml
    assert "target_uv=5000000" in assertions_xml
    assert "tolerance_uv=5000" in assertions_xml
