import struct
from pathlib import Path
from typing import Any

import pytest
from protocol_fakes import (
    AnalogInputValue,
    BusRole,
    CapturedRecord,
    ControlCommand,
    DigitalInputValue,
    FakeProtocol,
    FLAG_COMPLETE_TICK,
    FLAG_HAS_MORE_CHUNKS,
    LogicalOperation,
    PeripheralType,
    PWMInputValue,
    ResultCondition,
    SPIBitOrder,
    SPIClockPhase,
    SPIClockPolarity,
    SPIDataWidth,
    UARTElectricalMode,
    UARTWordLength,
    UpdateInstruction,
    VariableTestResult,
)
from protocol_fakes import (
    TestResult as LegacyTestResult,
)
from protocol_fakes import (
    UARTParity as ProtocolUARTParity,
)
from protocol_fakes import (
    UARTStopBits as ProtocolUARTStopBits,
)

from hilrig import (
    AggregatedTickResult,
    CapturedRunBuilder,
    CaptureStatus,
    CommunicationPeripheral,
    DigitalState,
    FrequencyMode,
    IncomingResultAdapter,
    LogicVoltage,
    SPIBaud,
    SPIFirst,
    SPIMode,
    SPIRole,
    SPISize,
    StartMode,
    TickCondition,
    UARTLengthBits,
    UARTMode,
    UARTParity,
    UARTStopBits,
    UploadAttempt,
    UploadOperationKind,
    VariableIOProtocolAdapter,
    aggregate_variable_results,
    chunk_update_instruction,
)
from hilrig import Test as HilRigTest
from hilrig.exceptions import ProtocolIntegrationError, ProtocolSessionError
from hilrig.protocol import FixedIOProtocolAdapter, ProtocolFamily


def _compiled_variable_io_test():
    test = HilRigTest(name="Variable protocol conversion")
    test.configure(frequency_mode=FrequencyMode.HZ_1K, start_mode=StartMode.IMMEDIATE)

    # Peripheral configs
    test.digital_input(channel=3).configure(voltage=LogicVoltage.V5)
    digital = test.digital_output(channel=1).configure(
        voltage=LogicVoltage.V3_3,
        initial_state=DigitalState.LOW,
    )
    digital_8 = test.digital_output(channel=8).configure(
        voltage=LogicVoltage.V3_3,
        initial_state=DigitalState.LOW,
    )
    test.analogue_input(channel=0).configure()
    analogue = test.analogue_output(channel=2).configure(initial_voltage=1.25)
    pwm = test.pwm_output(channel=0).configure(
        voltage=LogicVoltage.V3_3,
        initial_frequency_hz=1_000,
        initial_duty_cycle=0.25,
        initially_enabled=False,
    )
    uart = test.uart(channel=0).configure(
        mode=UARTMode.TTL_3V3,
        baud_hz=115200,
        parity=UARTParity.NONE,
        length=UARTLengthBits.EIGHT,
        stop=UARTStopBits.ONE,
    )
    spi = test.spi(channel=0).configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_1M406BIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_0,
        first_bit=SPIFirst.MSB,
    )

    # Stimuli on various sparse ticks
    digital.high(at_tick=5)
    digital_8.high(at_tick=5)
    pwm.enable(at_tick=5)

    analogue.set_voltage(2.5, at_tick=7)

    uart.write(data=b"hello", at_tick=10)
    spi.transfer(tx_data=b"\xaa\xbb\xcc", rx_length=3, at_tick=15)

    pwm.set_duty_cycle(duty_cycle=0.75, at_tick=20)
    digital.low(at_tick=25)
    pwm.disable(at_tick=30)

    return test.compile()


def test_variable_adapter_builds_sparse_update_instructions() -> None:
    compiled = _compiled_variable_io_test()
    attempt = UploadAttempt(
        definition_test_id=compiled.test_id,
        application_test_id=0x0102030405060708090A0B0C0D0E0F10,
    )
    adapter = VariableIOProtocolAdapter(protocol_module=FakeProtocol)

    upload = adapter.build_upload(compiled, upload_attempt=attempt)
    configuration = upload.configuration
    instructions = upload.instructions

    assert configuration.spi[0].enabled
    assert configuration.spi[0].bit_rate == 1_406_000
    assert configuration.spi[0].role is BusRole.MASTER
    assert configuration.spi[0].data_width is SPIDataWidth.BITS_8
    assert configuration.spi[0].bit_order is SPIBitOrder.MSB_FIRST
    assert configuration.spi[0].clock_polarity is SPIClockPolarity.IDLE_LOW
    assert configuration.spi[0].clock_phase is SPIClockPhase.FIRST_EDGE
    assert configuration.uart[0].enabled
    assert configuration.uart[0].baud_rate == 115_200
    assert configuration.uart[0].electrical_mode is UARTElectricalMode.TTL_3V3
    assert configuration.uart[0].word_length is UARTWordLength.BITS_8
    assert configuration.uart[0].parity is ProtocolUARTParity.NONE
    assert configuration.uart[0].stop_bits is ProtocolUARTStopBits.BITS_1
    assert configuration.uart[0].rx_enabled
    assert configuration.uart[0].tx_enabled

    # Tick 0 has initial analogue output (1.25V -> 1,250,000 uV)
    # Followed by ticks 5, 7, 10, 15, 20, 25, 30
    assert [inst.tick_number for inst in instructions] == [0, 5, 7, 10, 15, 20, 25, 30]

    # Verify Tick 0: Analogue output operation
    tick_0 = instructions[0]
    assert isinstance(tick_0, UpdateInstruction)
    assert tick_0.flags == 0
    assert len(tick_0.operations) == 1
    op_0 = tick_0.operations[0]
    assert op_0.peripheral_type == PeripheralType.ANALOG_OUTPUT
    assert op_0.channel == 2
    assert op_0.payload == (1_250_000).to_bytes(4, "little")

    # Verify Tick 5: Digital output bitmask + PWM output
    tick_5 = instructions[1]
    assert tick_5.tick_number == 5
    # digital channel 1 and channel 8 are high: (1 << 1) | (1 << 8) = 2 | 256 = 258 = 0x0102
    # pwm output enabled with 1000 Hz (1,000,000 ns) and 0.25 duty (2500 permyriad)
    periphs_5 = {op.peripheral_type: op for op in tick_5.operations}
    assert PeripheralType.DIGITAL_OUTPUT in periphs_5
    assert periphs_5[PeripheralType.DIGITAL_OUTPUT].channel == 0
    assert periphs_5[PeripheralType.DIGITAL_OUTPUT].payload == (258).to_bytes(2, "little")

    assert PeripheralType.PWM_OUTPUT in periphs_5
    pwm_op = periphs_5[PeripheralType.PWM_OUTPUT]
    assert pwm_op.channel == 0
    expected_pwm_5 = (1_000_000).to_bytes(4, "little") + (2500).to_bytes(2, "little")
    assert pwm_op.payload == expected_pwm_5

    # Verify Tick 7: Analogue output set to 2.5 V (2,500,000 uV)
    tick_7 = instructions[2]
    assert tick_7.tick_number == 7
    assert len(tick_7.operations) == 1
    assert tick_7.operations[0].peripheral_type == PeripheralType.ANALOG_OUTPUT
    assert tick_7.operations[0].channel == 2
    assert tick_7.operations[0].payload == (2_500_000).to_bytes(4, "little")

    # Verify Tick 10: UART raw bytes b"hello"
    tick_10 = instructions[3]
    assert tick_10.tick_number == 10
    assert len(tick_10.operations) == 1
    assert tick_10.operations[0].peripheral_type == PeripheralType.UART
    assert tick_10.operations[0].channel == 0
    assert tick_10.operations[0].payload == b"hello"

    # Verify Tick 15: SPI transfer [1B count=1][1B len=3][3B data=b"\xaa\xbb\xcc"]
    tick_15 = instructions[4]
    assert tick_15.tick_number == 15
    assert len(tick_15.operations) == 1
    assert tick_15.operations[0].peripheral_type == PeripheralType.SPI
    assert tick_15.operations[0].channel == 0
    assert tick_15.operations[0].payload == bytes([1, 3]) + b"\xaa\xbb\xcc"

    # Verify Tick 30: PWM disabled (period=0, duty=0)
    tick_30 = instructions[7]
    assert tick_30.tick_number == 30
    assert len(tick_30.operations) == 1
    assert tick_30.operations[0].peripheral_type == PeripheralType.PWM_OUTPUT
    assert tick_30.operations[0].payload == (0).to_bytes(4, "little") + (0).to_bytes(2, "little")


def test_variable_adapter_build_upload_plan() -> None:
    compiled = _compiled_variable_io_test()
    adapter = VariableIOProtocolAdapter(protocol_module=FakeProtocol)
    plan = adapter.build_upload_plan(compiled)

    assert plan.operations[0].kind is UploadOperationKind.CONFIGURATION
    assert plan.operations[0].response.scope is FakeProtocol.ResponseScope.TEST_CONFIGURATION

    tick_ops = [op for op in plan.operations if op.kind is UploadOperationKind.TICK]
    assert len(tick_ops) == len(plan.upload.instructions)
    assert all(op.response is None for op in tick_ops)  # Zero per-tick ACK
    assert [op.tick for op in tick_ops] == [inst.tick_number for inst in plan.upload.instructions]

    finalize_op = plan.operations[-2]
    assert finalize_op.kind is UploadOperationKind.FINALIZE
    assert finalize_op.response.scope is FakeProtocol.ResponseScope.COMPLETE_TEST

    start_op = plan.operations[-1]
    assert start_op.kind is UploadOperationKind.START
    assert start_op.response.control_command is ControlCommand.START


def test_incoming_result_adapter_decodes_variable_test_result(tmp_path: Path) -> None:
    compiled = _compiled_variable_io_test()
    attempt = compiled.new_upload_attempt()
    builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "variable_run.sqlite3",
        compiled,
        upload_attempt=attempt,
    )
    adapter = IncomingResultAdapter(builder, protocol_module=FakeProtocol)

    test_id = FakeProtocol.TestId(attempt.application_test_id.to_bytes(16, "big"))

    # Tick 5: digital input pins 1 and 3 high, analogue input 0 at 1.5V (1,500,000 uV), pwm input 0
    # UART RX b"ack"
    dig_mask = (1 << 1) | (1 << 3)  # 2 | 8 = 10
    pwm_bytes = (500_000).to_bytes(4, "little") + (5000).to_bytes(2, "little")

    result_msg = VariableTestResult(
        test_id=test_id,
        tick_number=5,
        condition=ResultCondition.OK,
        flags=0,
        records=(
            CapturedRecord(
                peripheral_type=PeripheralType.DIGITAL_INPUT,
                channel=0,
                data=dig_mask.to_bytes(2, "little"),
            ),
            CapturedRecord(
                peripheral_type=PeripheralType.ANALOG_INPUT,
                channel=0,
                data=(1_500_000).to_bytes(4, "little"),
            ),
            CapturedRecord(
                peripheral_type=PeripheralType.PWM_INPUT,
                channel=0,
                data=pwm_bytes,
            ),
            CapturedRecord(
                peripheral_type=PeripheralType.UART,
                channel=0,
                data=b"ack",
            ),
        ),
    )

    tick_res = adapter.ingest_application_message(result_msg)

    assert tick_res.tick == 5
    assert tick_res.condition is TickCondition.OK
    assert tick_res.digital_inputs[1] is True
    assert tick_res.digital_inputs[3] is True
    assert tick_res.digital_inputs[0] is False
    assert tick_res.analogue_inputs_uv[0] == 1_500_000
    assert tick_res.pwm_inputs[0].period_ns == 500_000
    assert tick_res.pwm_inputs[0].duty_permyriad == 5000

    # Tick 10: Only UART reception; digital/analogue/pwm forward-latch
    result_msg_10 = VariableTestResult(
        test_id=test_id,
        tick_number=10,
        condition=ResultCondition.OK,
        flags=0,
        records=(
            CapturedRecord(
                peripheral_type=PeripheralType.UART,
                channel=0,
                data=b"data_chunk",
            ),
            CapturedRecord(
                peripheral_type=PeripheralType.SPI,
                channel=0,
                data=b"\xde\xad\xbe\xef",
            ),
        ),
    )
    tick_res_10 = adapter.ingest_application_message(result_msg_10)
    assert tick_res_10.tick == 10
    # Latched states
    assert tick_res_10.digital_inputs[1] is True
    assert tick_res_10.analogue_inputs_uv[0] == 1_500_000
    assert tick_res_10.pwm_inputs[0].period_ns == 500_000

    # Finalize builder and query sqlite
    run = builder.finalize(status=CaptureStatus.INCOMPLETE)
    comm_captures = list(run.iter_communications())
    assert len(comm_captures) == 3

    assert comm_captures[0].tick == 5
    assert comm_captures[0].peripheral is CommunicationPeripheral.UART
    assert comm_captures[0].payload == b"ack"

    assert comm_captures[1].tick == 10
    assert comm_captures[1].peripheral is CommunicationPeripheral.UART
    assert comm_captures[1].payload == b"data_chunk"

    assert comm_captures[2].tick == 10
    assert comm_captures[2].peripheral is CommunicationPeripheral.SPI
    assert comm_captures[2].payload == b"\xde\xad\xbe\xef"


def test_incoming_result_adapter_handles_can_and_execution_problem(tmp_path: Path) -> None:
    compiled = _compiled_variable_io_test()
    attempt = compiled.new_upload_attempt()
    builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "can_run.sqlite3",
        compiled,
        upload_attempt=attempt,
    )
    adapter = IncomingResultAdapter(builder, protocol_module=FakeProtocol)
    test_id = FakeProtocol.TestId(attempt.application_test_id.to_bytes(16, "big"))

    # CAN 12-byte canonical packet: id=0x123 (2B LE), dlc=4 (1B),
    # data=b"1234" (8B padded), reserved=0 (1B).
    can_frame = struct.pack("<HB8sB", 0x123, 4, b"1234\x00\x00\x00\x00", 0)
    can_msg = VariableTestResult(
        test_id=test_id,
        tick_number=2,
        condition=ResultCondition.PARTIAL,
        records=(
            CapturedRecord(
                peripheral_type=PeripheralType.CAN,
                channel=0,
                data=can_frame,
            ),
        ),
    )
    res = adapter.ingest_application_message(can_msg)
    assert res.tick == 2
    assert res.condition is TickCondition.PARTIAL

    # Execution problem on tick 3
    err_msg = VariableTestResult(
        test_id=test_id,
        tick_number=3,
        condition=ResultCondition.EXECUTION_PROBLEM,
        problem_detail=42,
    )
    res_err = adapter.ingest_application_message(err_msg)
    assert res_err.tick == 3
    assert res_err.condition is TickCondition.EXECUTION_PROBLEM
    assert res_err.problem_detail == 42
    assert all(val is None for val in res_err.digital_inputs)
    assert all(val is None for val in res_err.analogue_inputs_uv)
    assert all(val is None for val in res_err.pwm_inputs)

    run = builder.finalize(status=CaptureStatus.INCOMPLETE)
    comm = list(run.iter_communications(peripheral=CommunicationPeripheral.CAN))
    assert len(comm) == 1
    assert comm[0].payload == can_frame


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
        pwm_inputs=tuple(
            PWMInputValue(period_nanoseconds=0, duty_cycle_permyriad=0) for _ in range(2)
        ),
        condition=ResultCondition.OK,
        problem_detail=0,
    )

    # Legacy message sent to Variable adapter must fail with explicit fault
    with pytest.raises(
        ProtocolSessionError,
        match="Received legacy TestResult.*variable message family was expected",
    ):
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
    with pytest.raises(
        ProtocolSessionError,
        match="Received VariableTestResult.*legacy message family was expected",
    ):
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
    spi.transfer(tx_data=b"\x01\x02", rx_length=2, at_ms=10)
    compiled = test.compile()

    legacy_adapter = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    with pytest.raises(
        ProtocolIntegrationError, match="not supported in the legacy fixed-I/O message family"
    ):
        legacy_adapter.build_upload(compiled)


def test_variable_adapter_encodes_multi_frame_can_in_single_instruction() -> None:
    test = HilRigTest(name="Multi-frame CAN test")
    test.configure(frequency_mode=FrequencyMode.HZ_1K, start_mode=StartMode.IMMEDIATE)
    can = test.can(channel=0).configure(bitrate=500_000)
    # 16 bytes = 2 full 8-byte frames
    can.transmit(frame_id=0x123, data=b"12345678abcdefgh", at_tick=5)
    compiled = test.compile()

    adapter = VariableIOProtocolAdapter(protocol_module=FakeProtocol)
    upload = adapter.build_upload(compiled)

    assert len(upload.instructions) == 1
    inst = upload.instructions[0]
    assert inst.tick_number == 5
    assert len(inst.operations) == 1
    op = inst.operations[0]
    assert op.peripheral_type == PeripheralType.CAN
    assert op.channel == 0
    assert len(op.payload) == 24
    frame1 = op.payload[:12]
    frame2 = op.payload[12:]
    assert frame1 == struct.pack("<HB8sB", 0x123, 8, b"12345678", 0)
    assert frame2 == struct.pack("<HB8sB", 0x123, 8, b"abcdefgh", 0)


def test_loopback_workload_points_encode_cleanly_at_high_utilisations() -> None:
    from examples.loopback_utilization_point import build_test_for_point
    from hilrig import LoopbackWorkloadPoint

    adapter = VariableIOProtocolAdapter(protocol_module=FakeProtocol)
    for pct in [20.0, 25.0, 27.0, 28.0, 29.0, 30.0, 35.0]:
        point = LoopbackWorkloadPoint(
            frequency_hz=1000,
            duration_s=1,
            target_utilization_percent=pct,
            burst_interval_ticks=1,
            seed=1,
        )
        test = build_test_for_point(point)
        compiled = test.compile()
        plan = adapter.build_upload_plan(compiled)
        assert len(plan.operations) > 0


def test_chunk_update_instruction_empty_and_splits() -> None:
    test_id = FakeProtocol.TestId(b"\x01" * 16)

    # Empty operations -> 1 chunk with flags=FLAG_COMPLETE_TICK (0) and empty ops
    empty_chunks = list(
        chunk_update_instruction(
            test_id=test_id,
            tick_number=0,
            operations=(),
            max_ops_per_chunk=4,
            instruction_factory=FakeProtocol.UpdateInstruction,
        )
    )
    assert len(empty_chunks) == 1
    assert empty_chunks[0].tick_number == 0
    assert empty_chunks[0].flags == FLAG_COMPLETE_TICK
    assert empty_chunks[0].operations == ()

    # 3 operations with max_ops_per_chunk=4 -> 1 chunk with flags=0
    ops_3 = tuple(
        LogicalOperation(
            peripheral_type=PeripheralType.ANALOG_OUTPUT,
            channel=i,
            payload=i.to_bytes(4, "little"),
        )
        for i in range(3)
    )
    chunks_3 = list(
        chunk_update_instruction(
            test_id=test_id,
            tick_number=5,
            operations=ops_3,
            max_ops_per_chunk=4,
            instruction_factory=FakeProtocol.UpdateInstruction,
        )
    )
    assert len(chunks_3) == 1
    assert chunks_3[0].tick_number == 5
    assert chunks_3[0].flags == FLAG_COMPLETE_TICK
    assert len(chunks_3[0].operations) == 3

    # 9 operations with max_ops_per_chunk=4 -> 3 chunks: [4 ops flags=1], [4 ops flags=1], [1 op flags=0]
    ops_9 = tuple(
        LogicalOperation(
            peripheral_type=PeripheralType.ANALOG_OUTPUT,
            channel=i,
            payload=i.to_bytes(4, "little"),
        )
        for i in range(9)
    )
    chunks_9 = list(
        chunk_update_instruction(
            test_id=test_id,
            tick_number=10,
            operations=ops_9,
            max_ops_per_chunk=4,
            instruction_factory=FakeProtocol.UpdateInstruction,
        )
    )
    assert len(chunks_9) == 3
    assert [c.tick_number for c in chunks_9] == [10, 10, 10]
    assert [c.flags for c in chunks_9] == [
        FLAG_HAS_MORE_CHUNKS,
        FLAG_HAS_MORE_CHUNKS,
        FLAG_COMPLETE_TICK,
    ]
    assert [len(c.operations) for c in chunks_9] == [4, 4, 1]
    assert chunks_9[0].operations == ops_9[0:4]
    assert chunks_9[1].operations == ops_9[4:8]
    assert chunks_9[2].operations == ops_9[8:9]

    # Validation of max_ops_per_chunk
    with pytest.raises(ValueError, match="max_ops_per_chunk must be a positive integer"):
        list(
            chunk_update_instruction(
                test_id=test_id,
                tick_number=0,
                operations=(),
                max_ops_per_chunk=0,
                instruction_factory=FakeProtocol.UpdateInstruction,
            )
        )


def test_variable_adapter_with_custom_chunking() -> None:
    test = HilRigTest(name="Multi-chunk instruction test")
    test.configure(frequency_mode=FrequencyMode.HZ_1K, start_mode=StartMode.IMMEDIATE)
    ao0 = test.analogue_output(channel=0).configure(initial_voltage=0.0)
    ao1 = test.analogue_output(channel=1).configure(initial_voltage=0.0)
    ao2 = test.analogue_output(channel=2).configure(initial_voltage=0.0)
    ao3 = test.analogue_output(channel=3).configure(initial_voltage=0.0)
    ao4 = test.analogue_output(channel=4).configure(initial_voltage=0.0)

    # Schedule 5 operations on tick 10
    ao0.set_voltage(1.0, at_tick=10)
    ao1.set_voltage(1.5, at_tick=10)
    ao2.set_voltage(2.0, at_tick=10)
    ao3.set_voltage(2.5, at_tick=10)
    ao4.set_voltage(3.0, at_tick=10)

    compiled = test.compile()
    attempt = UploadAttempt(
        definition_test_id=compiled.test_id,
        application_test_id=0x0102030405060708090A0B0C0D0E0F10,
    )

    # Adapter with max_ops_per_chunk=2 -> 5 operations on tick 10 should produce 3 chunks (2, 2, 1)
    adapter = VariableIOProtocolAdapter(protocol_module=FakeProtocol, max_ops_per_chunk=2)
    upload = adapter.build_upload(compiled, upload_attempt=attempt)

    instructions = upload.instructions
    assert len(instructions) == 3
    assert all(inst.tick_number == 10 for inst in instructions)
    assert instructions[0].flags == FLAG_HAS_MORE_CHUNKS
    assert len(instructions[0].operations) == 2
    assert instructions[1].flags == FLAG_HAS_MORE_CHUNKS
    assert len(instructions[1].operations) == 2
    assert instructions[2].flags == FLAG_COMPLETE_TICK
    assert len(instructions[2].operations) == 1

    plan = adapter.build_upload_plan(compiled, upload_attempt=attempt)
    tick_ops = [op for op in plan.operations if op.kind is UploadOperationKind.TICK]
    assert len(tick_ops) == 3
    assert all(op.tick == 10 for op in tick_ops)


def test_aggregate_variable_results_multi_chunk() -> None:
    test_id = FakeProtocol.TestId(b"\x02" * 16)
    r1 = CapturedRecord(
        peripheral_type=PeripheralType.UART,
        channel=0,
        data=b"chunk1_",
    )
    r2 = CapturedRecord(
        peripheral_type=PeripheralType.UART,
        channel=0,
        data=b"chunk2_",
    )
    r3 = CapturedRecord(
        peripheral_type=PeripheralType.UART,
        channel=0,
        data=b"chunk3",
    )
    r_tick1 = CapturedRecord(
        peripheral_type=PeripheralType.SPI,
        channel=0,
        data=b"spi_data",
    )

    incoming = [
        # Tick 0 in 3 chunks
        VariableTestResult(
            test_id=test_id,
            tick_number=0,
            condition=ResultCondition.OK,
            flags=FLAG_HAS_MORE_CHUNKS,
            records=(r1,),
        ),
        VariableTestResult(
            test_id=test_id,
            tick_number=0,
            condition=ResultCondition.OK,
            flags=FLAG_HAS_MORE_CHUNKS,
            records=(r2,),
        ),
        VariableTestResult(
            test_id=test_id,
            tick_number=0,
            condition=ResultCondition.OK,
            flags=FLAG_COMPLETE_TICK,
            records=(r3,),
        ),
        # Tick 1 in 1 chunk
        VariableTestResult(
            test_id=test_id,
            tick_number=1,
            condition=ResultCondition.OK,
            flags=FLAG_COMPLETE_TICK,
            records=(r_tick1,),
        ),
    ]

    aggregated = list(aggregate_variable_results(iter(incoming)))
    assert len(aggregated) == 2

    assert aggregated[0].tick_number == 0
    assert aggregated[0].condition is ResultCondition.OK
    assert aggregated[0].problem_detail == 0
    assert aggregated[0].records == [r1, r2, r3]

    assert aggregated[1].tick_number == 1
    assert aggregated[1].condition is ResultCondition.OK
    assert aggregated[1].records == [r_tick1]


def test_aggregate_variable_results_captures_condition_fault() -> None:
    test_id = FakeProtocol.TestId(b"\x03" * 16)
    r1 = CapturedRecord(
        peripheral_type=PeripheralType.UART,
        channel=0,
        data=b"part1",
    )
    r2 = CapturedRecord(
        peripheral_type=PeripheralType.UART,
        channel=0,
        data=b"part2",
    )

    incoming = [
        # Intermediate chunk has EXECUTION_PROBLEM
        VariableTestResult(
            test_id=test_id,
            tick_number=5,
            condition=ResultCondition.EXECUTION_PROBLEM,
            problem_detail=99,
            flags=FLAG_HAS_MORE_CHUNKS,
            records=(r1,),
        ),
        # Final chunk has OK
        VariableTestResult(
            test_id=test_id,
            tick_number=5,
            condition=ResultCondition.OK,
            problem_detail=0,
            flags=FLAG_COMPLETE_TICK,
            records=(r2,),
        ),
    ]

    aggregated = list(aggregate_variable_results(iter(incoming)))
    assert len(aggregated) == 1
    assert aggregated[0].tick_number == 5
    assert aggregated[0].condition is ResultCondition.EXECUTION_PROBLEM
    assert aggregated[0].problem_detail == 99
    assert aggregated[0].records == [r1, r2]


def test_incoming_result_adapter_multi_chunk_variable_test_result(tmp_path: Path) -> None:
    compiled = _compiled_variable_io_test()
    attempt = compiled.new_upload_attempt()
    builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "multi_chunk_run.sqlite3",
        compiled,
        upload_attempt=attempt,
    )
    adapter = IncomingResultAdapter(builder, protocol_module=FakeProtocol)
    test_id = FakeProtocol.TestId(attempt.application_test_id.to_bytes(16, "big"))

    # Tick 5 sent in 2 chunks:
    # Chunk 1 (flags=1): digital input + uart packet 1
    chunk_1 = VariableTestResult(
        test_id=test_id,
        tick_number=5,
        condition=ResultCondition.OK,
        flags=FLAG_HAS_MORE_CHUNKS,
        records=(
            CapturedRecord(
                peripheral_type=PeripheralType.DIGITAL_INPUT,
                channel=0,
                data=(2).to_bytes(2, "little"),  # pin 1 high
            ),
            CapturedRecord(
                peripheral_type=PeripheralType.UART,
                channel=0,
                data=b"first_part_",
            ),
        ),
    )
    # Intermediate chunk returns None
    res1 = adapter.ingest_application_message(chunk_1)
    assert res1 is None

    # Chunk 2 (flags=0): analogue input + uart packet 2
    chunk_2 = VariableTestResult(
        test_id=test_id,
        tick_number=5,
        condition=ResultCondition.OK,
        flags=FLAG_COMPLETE_TICK,
        records=(
            CapturedRecord(
                peripheral_type=PeripheralType.ANALOG_INPUT,
                channel=0,
                data=(2_000_000).to_bytes(4, "little"),
            ),
            CapturedRecord(
                peripheral_type=PeripheralType.UART,
                channel=0,
                data=b"second_part",
            ),
        ),
    )
    res2 = adapter.ingest_application_message(chunk_2)
    assert res2 is not None
    assert res2.tick == 5
    assert res2.digital_inputs[1] is True
    assert res2.analogue_inputs_uv[0] == 2_000_000

    run = builder.finalize(status=CaptureStatus.INCOMPLETE)
    comms = list(run.iter_communications(peripheral=CommunicationPeripheral.UART))
    assert len(comms) == 2
    assert comms[0].payload == b"first_part_"
    assert comms[1].payload == b"second_part"


def test_incoming_result_adapter_multi_chunk_fault_condition(tmp_path: Path) -> None:
    compiled = _compiled_variable_io_test()
    attempt = compiled.new_upload_attempt()
    builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "multi_chunk_fault.sqlite3",
        compiled,
        upload_attempt=attempt,
    )
    adapter = IncomingResultAdapter(builder, protocol_module=FakeProtocol)
    test_id = FakeProtocol.TestId(attempt.application_test_id.to_bytes(16, "big"))

    # Chunk 1 (flags=1) has EXECUTION_PROBLEM
    chunk_1 = VariableTestResult(
        test_id=test_id,
        tick_number=2,
        condition=ResultCondition.EXECUTION_PROBLEM,
        problem_detail=123,
        flags=FLAG_HAS_MORE_CHUNKS,
        records=(),
    )
    res1 = adapter.ingest_application_message(chunk_1)
    assert res1 is None

    # Chunk 2 (flags=0) finishes the tick
    chunk_2 = VariableTestResult(
        test_id=test_id,
        tick_number=2,
        condition=ResultCondition.OK,
        problem_detail=0,
        flags=FLAG_COMPLETE_TICK,
        records=(),
    )
    res2 = adapter.ingest_application_message(chunk_2)
    assert res2 is not None
    assert res2.tick == 2
    assert res2.condition is TickCondition.EXECUTION_PROBLEM
    assert res2.problem_detail == 123


def _drive_fake_variable_rig_to_state(
    connection: Any,
    application: VariableIOProtocolAdapter,
    target: Any = None,
) -> None:
    from protocol_fakes import (
        ApplicationResponse,
        FinalizeTestUpload,
        ProtocolVersion,
        ResponseOutcome,
        ResponseScope,
        SystemInfoRequest,
        SystemInfoResponse,
        TestConfiguration,
    )
    from hilrig.protocol import ProtocolWorkflowState
    from test_protocol_connection import _written_messages, _queue_incoming

    if target is None:
        target = ProtocolWorkflowState.RUNNING

    serial_port = connection.serial_port
    responded = 0
    for _ in range(500):
        connection.service()
        requests = _written_messages(serial_port, application)
        while responded < len(requests):
            request = requests[responded]
            responded += 1
            if type(request) is SystemInfoRequest:
                responses = [
                    SystemInfoResponse(
                        protocol_version=FakeProtocol.PROTOCOL_VERSION,
                        firmware_version=ProtocolVersion(1, 2, 3),
                        firmware_git_hash=b"abc123",
                    )
                ]
            elif type(request) is TestConfiguration:
                responses = [
                    ApplicationResponse(
                        request.test_id,
                        ResponseScope.TEST_CONFIGURATION,
                        ResponseOutcome.ACCEPTED,
                    )
                ]
            elif type(request) is UpdateInstruction:
                responses = []
            elif type(request) is FinalizeTestUpload:
                responses = [
                    ApplicationResponse(
                        request.test_id,
                        ResponseScope.COMPLETE_TEST,
                        ResponseOutcome.ACCEPTED,
                    )
                ]
            else:
                responses = [
                    ApplicationResponse(
                        request.test_id,
                        ResponseScope.EXECUTION_CONTROL,
                        ResponseOutcome.COMPLETED,
                        control_command=ControlCommand.START,
                    )
                ]
            _queue_incoming(serial_port, application, *responses)
        if connection.workflow_state is target:
            return
    raise AssertionError(f"fake protocol workflow did not reach {target.name}")


def test_connection_service_multi_chunk_variable_test_result(tmp_path: Path) -> None:
    from protocol_fakes import FakeSerial, FakeTransport
    from hilrig.protocol import FixedIOProtocolConnection

    compiled = _compiled_variable_io_test()
    attempt = compiled.new_upload_attempt()
    builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "conn_multi_chunk.sqlite3",
        compiled,
        upload_attempt=attempt,
    )
    application = VariableIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    result_adapter = IncomingResultAdapter(builder, protocol_module=FakeProtocol)
    serial_port = FakeSerial()
    connection = FixedIOProtocolConnection(
        serial_port=serial_port,
        application=application,
        transport=transport,
        result_adapter=result_adapter,
    )

    # Establish session & queue upload
    connection.queue_upload(compiled, upload_attempt=attempt)

    # Drive to RUNNING
    from test_protocol_connection import _queue_incoming
    _drive_fake_variable_rig_to_state(connection, application)

    test_id = FakeProtocol.TestId(attempt.application_test_id.to_bytes(16, "big"))

    # Send tick 0 in 2 chunks
    chunk_0_1 = VariableTestResult(
        test_id=test_id,
        tick_number=0,
        condition=ResultCondition.OK,
        flags=FLAG_HAS_MORE_CHUNKS,
        records=(
            CapturedRecord(
                peripheral_type=PeripheralType.DIGITAL_INPUT,
                channel=0,
                data=(1).to_bytes(2, "little"),
            ),
        ),
    )
    chunk_0_2 = VariableTestResult(
        test_id=test_id,
        tick_number=0,
        condition=ResultCondition.OK,
        flags=FLAG_COMPLETE_TICK,
        records=(
            CapturedRecord(
                peripheral_type=PeripheralType.UART,
                channel=0,
                data=b"tick0_data",
            ),
        ),
    )

    _queue_incoming(serial_port, application, chunk_0_1)
    report1 = connection.service()
    assert len(report1.stored_tick_results) == 0  # Intermediate chunk doesn't finalize tick

    _queue_incoming(serial_port, application, chunk_0_2)
    report2 = connection.service()
    assert len(report2.stored_tick_results) == 1
    assert report2.stored_tick_results[0].tick == 0
    assert report2.stored_tick_results[0].digital_inputs[0] is True
