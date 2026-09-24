from pathlib import Path

path = Path("tests/test_protocol_variable.py")
txt = path.read_text(encoding="utf-8")

extra_tests = """
import pytest
from hilrig.exceptions import ProtocolIntegrationError, ProtocolSessionError
from hilrig.protocol import FixedIOProtocolAdapter, ProtocolFamily
from protocol_fakes import (
    AnalogInputValue,
    DigitalInputValue,
    PWMInputValue,
    TestResult as LegacyTestResult,
)


def test_incoming_result_adapter_enforces_expected_protocol_family(tmp_path: Path) -> None:
    test = HilRigTest(name="Family Check")
    test.configure(frequency_mode=FrequencyMode.HZ_1K, start_mode=StartMode.IMMEDIATE)
    compiled = test.compile()
    attempt = compiled.new_upload_attempt()
    builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "family_check.sqlite3",
        compiled,
        upload_attempt=attempt,
    )
    test_id = FakeProtocol.TestId(attempt.application_test_id.to_bytes(16, "big"))

    # Adapter expecting VARIABLE (default)
    var_adapter = IncomingResultAdapter(
        builder,
        protocol_module=FakeProtocol,
        expected_family=ProtocolFamily.VARIABLE,
    )

    legacy_msg = LegacyTestResult(
        test_id=test_id,
        tick_number=0,
        digital_inputs=tuple(DigitalInputValue(high=False) for _ in range(10)),
        analog_inputs=tuple(AnalogInputValue(microvolts=0) for _ in range(6)),
        pwm_inputs=tuple(PWMInputValue(period_nanoseconds=0, duty_cycle_permyriad=0) for _ in range(2)),
        condition=ResultCondition.OK,
        problem_detail=0,
    )

    # Legacy message sent to Variable adapter must fail with explicit fault
    with pytest.raises(ProtocolSessionError, match="Received legacy TestResult.*variable message family was expected"):
        var_adapter.ingest_application_message(legacy_msg)

    # Adapter expecting LEGACY
    builder_legacy = CapturedRunBuilder.from_compiled_test(
        tmp_path / "family_check_legacy.sqlite3",
        compiled,
        upload_attempt=attempt,
    )
    leg_adapter = IncomingResultAdapter(
        builder_legacy,
        protocol_module=FakeProtocol,
        expected_family=ProtocolFamily.LEGACY,
    )

    var_msg = VariableTestResult(
        test_id=test_id,
        tick_number=0,
        condition=ResultCondition.OK,
        records=(),
    )

    # Variable message sent to Legacy adapter must fail with explicit fault
    with pytest.raises(ProtocolSessionError, match="Received VariableTestResult.*legacy message family was expected"):
        leg_adapter.ingest_application_message(var_msg)


def test_legacy_fixed_adapter_rejects_communication_peripherals() -> None:
    test = HilRigTest(name="SPI in legacy mode")
    test.configure(frequency_mode=FrequencyMode.HZ_1K, start_mode=StartMode.IMMEDIATE)
    spi = test.spi(channel=0).configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_5M625BIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_0,
        first_bit=SPIFirst.MSB,
    )
    spi.transfer(tx_data=b"\\x01\\x02", rx_length=2, at_ms=10)
    compiled = test.compile()

    legacy_adapter = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    with pytest.raises(ProtocolIntegrationError, match="not supported in the legacy fixed-I/O message family"):
        legacy_adapter.build_upload(compiled)
"""

if "test_incoming_result_adapter_enforces_expected_protocol_family" not in txt:
    txt = txt + extra_tests
    path.write_text(txt, encoding="utf-8")
    print("Added family enforcement tests")
