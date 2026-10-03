from protocol_fakes import (
    ControlCommand,
    ExecutionControl,
    FakeProtocol,
    FinalizeTestUpload,
    GlobalControl,
    GlobalControlCommand,
    PeripheralVoltage,
    SystemInfoRequest,
)

from hilrig import (
    DigitalState,
    FixedIOProtocolAdapter,
    FrequencyMode,
    LogicVoltage,
    StartMode,
    UploadAttempt,
    UploadOperationKind,
)
from hilrig import Test as HilRigTest


def _compiled_fixed_io_test():
    test = HilRigTest(name="Fixed protocol conversion")
    test.configure(frequency_mode=FrequencyMode.HZ_1K, start_mode=StartMode.IMMEDIATE)
    test.digital_input(channel=3).configure(voltage=LogicVoltage.V5)
    digital = test.digital_output(channel=1).configure(
        voltage=LogicVoltage.V3_3,
        initial_state=DigitalState.LOW,
    )
    test.analogue_input(channel=0).configure()
    analogue = test.analogue_output(channel=2).configure(initial_voltage=1.25)
    test.pwm_input(channel=1).configure(voltage=LogicVoltage.V24)
    pwm = test.pwm_output(channel=0).configure(
        voltage=LogicVoltage.V3_3,
        initial_frequency_hz=1_000,
        initial_duty_cycle=0.25,
        initially_enabled=False,
    )

    digital.high(at_tick=5)
    pwm.enable(at_tick=5)
    analogue.set_voltage(2.5, at_tick=7)
    pwm.set_duty_cycle(duty_cycle=0.75, at_tick=10)
    pwm.disable(at_tick=20)
    pwm.set_frequency(frequency_hz=2_000, at_tick=30)
    pwm.enable(at_tick=40)
    return test.compile()


def test_configuration_maps_enabled_fixed_channels_and_initial_values() -> None:
    compiled = _compiled_fixed_io_test()
    attempt = UploadAttempt(
        definition_test_id=compiled.test_id,
        application_test_id=0x0102030405060708090A0B0C0D0E0F10,
    )
    adapter = FixedIOProtocolAdapter(protocol_module=FakeProtocol)

    upload = adapter.build_upload(compiled, upload_attempt=attempt)
    configuration = upload.configuration

    assert configuration.test_id.bytes == bytes.fromhex("0102030405060708090a0b0c0d0e0f10")
    assert configuration.tick_duration_us.microseconds == 1_000
    assert configuration.expected_tick_count == compiled.expected_tick_count
    assert configuration.digital_in[3].enabled
    assert configuration.digital_in[3].voltage_level is PeripheralVoltage.V_5V
    assert configuration.digital_out[1].enabled
    assert not configuration.digital_out[1].initial_high
    assert configuration.analog_in[0].enabled
    assert configuration.analog_out[2].enabled
    assert configuration.pwm_in[1].voltage_level is PeripheralVoltage.V_24V
    assert configuration.pwm_out[0].enabled
    assert configuration.pwm_out[0].initial_period_nanoseconds == 0
    assert configuration.pwm_out[0].initial_duty_cycle_permyriad == 0


def test_sparse_state_expander_retains_state_and_encodes_pwm_disable_as_zero() -> None:
    compiled = _compiled_fixed_io_test()
    adapter = FixedIOProtocolAdapter(protocol_module=FakeProtocol)

    upload = adapter.build_upload(compiled)
    messages = upload.instructions

    assert [message.tick_number for message in messages] == [0, 5, 7, 10, 20, 30, 40]
    assert messages[0].analog_outputs[2].microvolts == 1_250_000
    assert not messages[0].digital_outputs[1].high
    assert messages[0].pwm_outputs[0].period_nanoseconds == 0
    assert messages[0].pwm_outputs[0].duty_cycle_permyriad == 0

    assert messages[1].digital_outputs[1].high
    assert messages[1].pwm_outputs[0].period_nanoseconds == 1_000_000
    assert messages[1].pwm_outputs[0].duty_cycle_permyriad == 2_500
    assert messages[2].analog_outputs[2].microvolts == 2_500_000
    assert messages[3].pwm_outputs[0].duty_cycle_permyriad == 7_500

    assert messages[4].pwm_outputs[0].period_nanoseconds == 0
    assert messages[4].pwm_outputs[0].duty_cycle_permyriad == 0
    assert messages[5].pwm_outputs[0].period_nanoseconds == 0
    assert messages[6].pwm_outputs[0].period_nanoseconds == 500_000
    assert messages[6].pwm_outputs[0].duty_cycle_permyriad == 7_500


def test_analogue_configuration_alone_synthesizes_tick_zero() -> None:
    test = HilRigTest(name="Analogue initial state")
    test.analogue_output(channel=5).configure(initial_voltage=20)

    upload = FixedIOProtocolAdapter(protocol_module=FakeProtocol).build_upload(test.compile())

    assert len(upload.instructions) == 1
    assert upload.instructions[0].tick_number == 0
    assert upload.instructions[0].analog_outputs[5].microvolts == 20_000_000


def test_tick_zero_stimulus_is_applied_to_the_single_tick_zero_state() -> None:
    test = HilRigTest(name="Analogue tick zero")
    output = test.analogue_output(channel=0).configure(initial_voltage=1)
    output.set_voltage(3, at_tick=0)

    upload = FixedIOProtocolAdapter(protocol_module=FakeProtocol).build_upload(test.compile())

    assert [message.tick_number for message in upload.instructions] == [0]
    assert upload.instructions[0].analog_outputs[0].microvolts == 3_000_000


def test_upload_encoding_keeps_configuration_first() -> None:
    adapter = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    upload = adapter.build_upload(_compiled_fixed_io_test())

    encoded = adapter.encode_upload(upload)

    assert len(encoded) == len(upload.instructions) + 1
    assert adapter.codec.encoded[encoded[0]] is upload.configuration
    assert adapter.codec.encoded[encoded[1]] is upload.instructions[0]


def test_upload_plan_groups_messages_by_semantic_operation_and_owns_correlation() -> None:
    adapter = FixedIOProtocolAdapter(protocol_module=FakeProtocol)

    plan = adapter.build_upload_plan(_compiled_fixed_io_test())

    assert [operation.kind for operation in plan.operations] == [
        UploadOperationKind.CONFIGURATION,
        *([UploadOperationKind.TICK] * len(plan.upload.instructions)),
        UploadOperationKind.FINALIZE,
        UploadOperationKind.START,
    ]
    assert all(len(operation.encoded_messages) == 1 for operation in plan.operations)
    assert (
        plan.transfer_operations[0].response.scope is FakeProtocol.ResponseScope.TEST_CONFIGURATION
    )
    tick_operations = tuple(
        operation
        for operation in plan.transfer_operations
        if operation.kind is UploadOperationKind.TICK
    )
    assert [operation.tick for operation in tick_operations] == [
        instruction.tick_number for instruction in plan.upload.instructions
    ]
    finalize = plan.transfer_operations[-1]
    assert finalize.kind is UploadOperationKind.FINALIZE
    assert finalize.response.scope is FakeProtocol.ResponseScope.COMPLETE_TEST
    finalize_message = adapter.codec.decode(finalize.encoded_messages[0])
    assert type(finalize_message) is FinalizeTestUpload
    assert finalize_message.flags == 0
    assert finalize_message.test_id == plan.upload.configuration.test_id
    assert plan.start_operation is not None
    assert plan.start_operation.response.control_command is ControlCommand.START


def test_control_flow_builders_use_the_upload_attempt_wire_id() -> None:
    compiled = _compiled_fixed_io_test()
    attempt = compiled.new_upload_attempt()
    adapter = FixedIOProtocolAdapter(protocol_module=FakeProtocol)

    discovery = adapter.build_system_info_request()
    finalize = adapter.build_finalize_test_upload(attempt.application_test_id)
    start = adapter.build_start(attempt)
    abort = adapter.build_abort(attempt)
    reset = adapter.build_reset_application()

    assert type(discovery) is SystemInfoRequest
    assert discovery.request_firmware_git_hash
    assert type(finalize) is FinalizeTestUpload
    assert finalize.flags == 0
    assert finalize.test_id.bytes == attempt.application_test_id.to_bytes(16, "big")
    assert type(start) is ExecutionControl
    assert start.command is ControlCommand.START
    assert start.test_id.bytes == attempt.application_test_id.to_bytes(16, "big")
    assert abort.command is ControlCommand.ABORT
    assert type(reset) is GlobalControl
    assert reset.command is GlobalControlCommand.RESET_APPLICATION
