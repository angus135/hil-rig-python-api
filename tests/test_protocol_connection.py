from pathlib import Path

import pytest
from protocol_fakes import (
    ApplicationErrorMessage,
    ApplicationResponse,
    ControlCommand,
    ErrorCategory,
    EventType,
    FakeProtocol,
    FakeSerial,
    FakeTransport,
    GlobalControlCommand,
    ProtocolVersion,
    ResponseOutcome,
    ResponseReason,
    ResponseScope,
    ResultCondition,
    SystemInfoRequest,
    SystemInfoResponse,
    TransportEvent,
)
from protocol_fakes import (
    TestConfiguration as ProtocolTestConfiguration,
)
from protocol_fakes import (
    TestId as ProtocolTestId,
)
from protocol_fakes import (
    TestInstruction as ProtocolTestInstruction,
)
from protocol_fakes import (
    TestResult as ProtocolTestResult,
)

from hilrig import (
    CapturedRunBuilder,
    DigitalState,
    FixedIOProtocolAdapter,
    FixedIOProtocolConnection,
    FrequencyMode,
    IncomingResultAdapter,
    LogicVoltage,
    ProtocolSessionError,
    ProtocolWorkflowState,
    StartMode,
    UploadAdvanceMode,
    UploadOperationKind,
)
from hilrig import Test as HilRigTest


def _compiled_digital_test(*, start_mode: StartMode = StartMode.IMMEDIATE):
    test = HilRigTest(name="Connection upload")
    test.configure(frequency_mode=FrequencyMode.HZ_1K, start_mode=start_mode)
    output = test.digital_output(channel=0).configure(
        voltage=LogicVoltage.V3_3,
        initial_state=DigitalState.LOW,
    )
    output.high(at_tick=5)
    return test.compile()


def _drive_fake_rig_to_state(
    connection: FixedIOProtocolConnection,
    application: FixedIOProtocolAdapter,
    transport: FakeTransport,
    target: ProtocolWorkflowState = ProtocolWorkflowState.RUNNING,
    *,
    responded: int = 0,
) -> int:
    instructions = connection.active_upload.instructions
    last_sparse_tick = instructions[-1].tick_number if instructions else None
    for _ in range(500):
        connection.service()
        while responded < transport.committed:
            request = application.codec.decode(transport.submitted[responded])
            responded += 1
            responses: list[object]
            if type(request) is SystemInfoRequest:
                responses = [
                    SystemInfoResponse(
                        protocol_version=FakeProtocol.PROTOCOL_VERSION,
                        firmware_version=ProtocolVersion(1, 2, 3),
                        firmware_git_hash=b"abc123",
                    )
                ]
            elif type(request) is ProtocolTestConfiguration:
                responses = [
                    ApplicationResponse(
                        request.test_id,
                        ResponseScope.TEST_CONFIGURATION,
                        ResponseOutcome.ACCEPTED,
                    )
                ]
                if last_sparse_tick is None:
                    responses.append(
                        ApplicationResponse(
                            request.test_id,
                            ResponseScope.COMPLETE_TEST,
                            ResponseOutcome.ACCEPTED,
                        )
                    )
            elif type(request) is ProtocolTestInstruction:
                responses = [
                    ApplicationResponse(
                        request.test_id,
                        ResponseScope.TICK,
                        ResponseOutcome.ACCEPTED,
                        tick_number=request.tick_number,
                    )
                ]
                if request.tick_number == last_sparse_tick:
                    responses.append(
                        ApplicationResponse(
                            request.test_id,
                            ResponseScope.COMPLETE_TEST,
                            ResponseOutcome.ACCEPTED,
                        )
                    )
            else:
                responses = [
                    ApplicationResponse(
                        request.test_id,
                        ResponseScope.EXECUTION_CONTROL,
                        ResponseOutcome.COMPLETED,
                        control_command=ControlCommand.START,
                    )
                ]
            transport.application_data.extend(application.codec.encode(item) for item in responses)
        if connection.workflow_state is target:
            return responded
    raise AssertionError(f"fake protocol workflow did not reach {target.name}")


def test_operator_gate_steps_configuration_tick_and_start_as_semantic_operations() -> None:
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
    )
    connection.queue_upload(
        _compiled_digital_test(),
        advance_mode=UploadAdvanceMode.OPERATOR_GATED,
    )

    responded = _drive_fake_rig_to_state(
        connection,
        application,
        transport,
        ProtocolWorkflowState.WAITING_FOR_OPERATOR,
    )
    assert connection.next_upload_operation.kind is UploadOperationKind.CONFIGURATION
    assert [type(application.codec.decode(item)).__name__ for item in transport.submitted] == [
        "SystemInfoRequest"
    ]

    released = connection.release_next_operation()
    assert released.kind is UploadOperationKind.CONFIGURATION
    responded = _drive_fake_rig_to_state(
        connection,
        application,
        transport,
        ProtocolWorkflowState.WAITING_FOR_OPERATOR,
        responded=responded,
    )
    assert connection.next_upload_operation.kind is UploadOperationKind.TICK
    assert connection.next_upload_operation.tick == 5

    connection.release_next_operation()
    responded = _drive_fake_rig_to_state(
        connection,
        application,
        transport,
        ProtocolWorkflowState.WAITING_FOR_OPERATOR,
        responded=responded,
    )
    assert connection.upload_accepted
    assert connection.next_upload_operation.kind is UploadOperationKind.START
    assert not connection.execution_started

    connection.release_next_operation()
    _drive_fake_rig_to_state(
        connection,
        application,
        transport,
        ProtocolWorkflowState.RUNNING,
        responded=responded,
    )
    assert connection.execution_started


def test_continue_releases_gate_and_runs_remaining_operations_automatically() -> None:
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
    )
    connection.queue_upload(
        _compiled_digital_test(),
        advance_mode=UploadAdvanceMode.OPERATOR_GATED,
    )
    responded = _drive_fake_rig_to_state(
        connection,
        application,
        transport,
        ProtocolWorkflowState.WAITING_FOR_OPERATOR,
    )

    released = connection.continue_upload()
    assert released.kind is UploadOperationKind.CONFIGURATION
    assert connection.advance_mode is UploadAdvanceMode.AUTOMATIC
    _drive_fake_rig_to_state(
        connection,
        application,
        transport,
        ProtocolWorkflowState.RUNNING,
        responded=responded,
    )

    assert connection.execution_started
    assert not connection.waiting_for_operator


def test_connection_preserves_partial_writes_and_sends_one_message_at_a_time() -> None:
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    serial_port = FakeSerial(max_write_size=4)
    connection = FixedIOProtocolConnection(
        serial_port=serial_port,
        application=application,
        transport=transport,
    )

    connection.queue_upload(_compiled_digital_test())
    first_report = connection.service()

    assert first_report.application_message_submitted
    assert len(transport.submitted) == 1
    assert transport.committed == 0

    _drive_fake_rig_to_state(connection, application, transport)

    assert connection.upload_delivery_complete
    assert connection.upload_accepted
    assert connection.execution_started
    assert connection.workflow_state is ProtocolWorkflowState.RUNNING
    assert connection.session_info.firmware_version == "1.2.3"
    assert len(transport.submitted) == 4
    assert transport.committed == 4
    assert bytes(serial_port.written) == b"".join(
        b"frame:" + submitted for submitted in transport.submitted
    )


def test_observation_only_upload_waits_for_complete_test_after_configuration() -> None:
    test = HilRigTest(name="Observation only")
    test.digital_input(channel=0).configure(voltage=LogicVoltage.V3_3)
    compiled = test.compile()
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
    )

    connection.queue_upload(compiled)
    _drive_fake_rig_to_state(connection, application, transport)

    assert [type(application.codec.decode(item)).__name__ for item in transport.submitted] == [
        "SystemInfoRequest",
        "TestConfiguration",
        "ExecutionControl",
    ]


def test_connection_decodes_and_stores_received_fixed_results(tmp_path: Path) -> None:
    compiled = _compiled_digital_test()
    attempt = compiled.new_upload_attempt()
    builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "run.sqlite3",
        compiled,
        upload_attempt=attempt,
    )
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    result_adapter = IncomingResultAdapter(builder, protocol_module=FakeProtocol)
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
        result_adapter=result_adapter,
    )
    connection.queue_upload(compiled, upload_attempt=attempt)
    _drive_fake_rig_to_state(connection, application, transport)
    result_message = ProtocolTestResult(
        test_id=ProtocolTestId(attempt.application_test_id.to_bytes(16, "big")),
        condition=ResultCondition.OK,
    )
    encoded = application.codec.encode(result_message)
    transport.application_data.append(encoded)

    report = connection.service()
    run = builder.finalize()

    assert report.application_messages == (result_message,)
    assert len(report.stored_tick_results) == 1
    assert run.metadata.received_tick_count == 1


def test_application_error_is_mapped_into_capture_storage(tmp_path: Path) -> None:
    compiled = _compiled_digital_test()
    attempt = compiled.new_upload_attempt()
    builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "run.sqlite3",
        compiled,
        upload_attempt=attempt,
    )
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
        result_adapter=IncomingResultAdapter(builder, protocol_module=FakeProtocol),
    )
    connection.queue_upload(compiled, upload_attempt=attempt)
    _drive_fake_rig_to_state(connection, application, transport)
    message = ApplicationErrorMessage(
        ProtocolTestId(attempt.application_test_id.to_bytes(16, "big")),
        ErrorCategory.EXECUTION,
        True,
        tick_number=0,
        detail=27,
        diagnostic_data=b"timing slip",
    )
    transport.application_data.append(application.codec.encode(message))

    report = connection.service()
    run = builder.finalize()
    stored = tuple(run.iter_application_errors())

    assert report.stored_application_errors[0].detail == "27"
    assert stored[0].category == "execution"
    assert stored[0].diagnostic_data == b"timing slip"


def test_rejected_tick_stops_sparse_upload_and_surfaces_reason() -> None:
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
    )
    compiled = _compiled_digital_test()
    attempt = connection.queue_upload(compiled)

    connection.service()
    connection.service()
    request = application.codec.decode(transport.submitted[0])
    assert type(request) is SystemInfoRequest
    transport.application_data.append(
        application.codec.encode(
            SystemInfoResponse(FakeProtocol.PROTOCOL_VERSION, ProtocolVersion(1, 0, 0))
        )
    )
    connection.service()
    connection.service()
    configuration = application.codec.decode(transport.submitted[1])
    transport.application_data.append(
        application.codec.encode(
            ApplicationResponse(
                configuration.test_id,
                ResponseScope.TEST_CONFIGURATION,
                ResponseOutcome.ACCEPTED,
            )
        )
    )
    connection.service()
    connection.service()
    instruction = application.codec.decode(transport.submitted[2])
    transport.application_data.append(
        application.codec.encode(
            ApplicationResponse(
                instruction.test_id,
                ResponseScope.TICK,
                ResponseOutcome.REJECTED,
                reason=ResponseReason.INVALID_TICK,
                tick_number=instruction.tick_number,
                detail=4,
            )
        )
    )

    with pytest.raises(ProtocolSessionError, match="INVALID_TICK"):
        connection.service()
    assert connection.workflow_state is ProtocolWorkflowState.FAILED
    assert connection.active_upload is None

    with pytest.raises(ValueError, match="abandoned Application Test ID"):
        connection.queue_upload(compiled, upload_attempt=attempt)

    restarted = attempt.restart()
    assert connection.queue_upload(compiled, upload_attempt=restarted) is restarted


def test_rejected_start_remains_ready_for_an_explicit_retry() -> None:
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
    )
    connection.queue_upload(_compiled_digital_test(start_mode=StartMode.HOST_COMMAND))
    _drive_fake_rig_to_state(
        connection,
        application,
        transport,
        ProtocolWorkflowState.READY_TO_START,
    )
    connection.start()
    connection.service()
    request = application.codec.decode(transport.submitted[-1])
    transport.application_data.append(
        application.codec.encode(
            ApplicationResponse(
                request.test_id,
                ResponseScope.EXECUTION_CONTROL,
                ResponseOutcome.REJECTED,
                reason=ResponseReason.OPERATION_NOT_ALLOWED,
                control_command=ControlCommand.START,
            )
        )
    )

    with pytest.raises(ProtocolSessionError, match="OPERATION_NOT_ALLOWED"):
        connection.service()

    assert connection.upload_accepted
    assert not connection.execution_started
    assert connection.workflow_state is ProtocolWorkflowState.READY_TO_START
    connection.start()
    assert connection.workflow_state is ProtocolWorkflowState.STARTING


def test_host_command_waits_for_explicit_start_and_correlates_completion() -> None:
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
    )
    connection.queue_upload(_compiled_digital_test(start_mode=StartMode.HOST_COMMAND))
    _drive_fake_rig_to_state(
        connection,
        application,
        transport,
        ProtocolWorkflowState.READY_TO_START,
    )

    assert not connection.execution_started
    connection.start()
    connection.service()
    request = application.codec.decode(transport.submitted[-1])
    assert request.command is ControlCommand.START
    transport.application_data.append(
        application.codec.encode(
            ApplicationResponse(
                request.test_id,
                ResponseScope.EXECUTION_CONTROL,
                ResponseOutcome.COMPLETED,
                control_command=ControlCommand.START,
            )
        )
    )
    connection.service()

    assert connection.execution_started
    assert connection.workflow_state is ProtocolWorkflowState.RUNNING


def test_external_trigger_stays_ready_without_protocol_start() -> None:
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
    )
    connection.queue_upload(_compiled_digital_test(start_mode=StartMode.EXTERNAL_TRIGGER))
    _drive_fake_rig_to_state(
        connection,
        application,
        transport,
        ProtocolWorkflowState.READY_TO_START,
    )

    with pytest.raises(ProtocolSessionError, match="no protocol implementation"):
        connection.start()
    assert connection.workflow_state is ProtocolWorkflowState.READY_TO_START


def test_abort_and_global_reset_use_correlated_control_responses() -> None:
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
    )
    connection.queue_upload(_compiled_digital_test())
    _drive_fake_rig_to_state(connection, application, transport)

    connection.abort()
    connection.service()
    abort = application.codec.decode(transport.submitted[-1])
    transport.application_data.append(
        application.codec.encode(
            ApplicationResponse(
                abort.test_id,
                ResponseScope.EXECUTION_CONTROL,
                ResponseOutcome.COMPLETED,
                control_command=ControlCommand.ABORT,
            )
        )
    )
    connection.service()
    assert connection.workflow_state is ProtocolWorkflowState.ABORTED

    connection.reset_application()
    connection.service()
    reset = application.codec.decode(transport.submitted[-1])
    assert reset.command is GlobalControlCommand.RESET_APPLICATION
    transport.application_data.append(
        application.codec.encode(
            ApplicationResponse(
                None,
                ResponseScope.GLOBAL_CONTROL,
                ResponseOutcome.COMPLETED,
                global_control_command=GlobalControlCommand.RESET_APPLICATION,
            )
        )
    )
    connection.service()

    assert connection.workflow_state is ProtocolWorkflowState.READY
    assert connection.active_upload is None


def test_discovery_response_does_not_finish_before_transport_confirmation() -> None:
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(max_write_size=1),
        application=application,
        transport=transport,
    )

    connection.service()
    transport.application_data.append(
        application.codec.encode(
            SystemInfoResponse(FakeProtocol.PROTOCOL_VERSION, ProtocolVersion(1, 0, 0))
        )
    )
    connection.service()
    assert not connection.session_confirmed

    for _ in range(100):
        connection.service()
        if connection.session_confirmed:
            break
    assert connection.session_confirmed


def test_incompatible_discovery_and_session_reset_abandon_workflow() -> None:
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    transport = FakeTransport()
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=transport,
    )
    connection.service()
    transport.application_data.append(
        application.codec.encode(
            SystemInfoResponse(ProtocolVersion(9, 9, 9), ProtocolVersion(1, 0, 0))
        )
    )
    with pytest.raises(ProtocolSessionError, match="incompatible"):
        connection.service()
    assert connection.workflow_state is ProtocolWorkflowState.FAILED

    second_transport = FakeTransport()
    second = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=FixedIOProtocolAdapter(protocol_module=FakeProtocol),
        transport=second_transport,
    )
    second_transport.events.append(TransportEvent(EventType.SESSION_RESET))
    with pytest.raises(ProtocolSessionError, match="session reset"):
        second.service()
    assert not second.session_confirmed
    assert second.workflow_state is ProtocolWorkflowState.FAILED


def test_queue_upload_reuses_the_capture_builders_wire_id(tmp_path: Path) -> None:
    compiled = _compiled_digital_test()
    builder = CapturedRunBuilder.from_compiled_test(tmp_path / "run.sqlite3", compiled)
    application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=application,
        transport=FakeTransport(),
        result_adapter=IncomingResultAdapter(builder, protocol_module=FakeProtocol),
    )

    attempt = connection.queue_upload(compiled)

    assert attempt.application_test_id == builder.application_test_id
    builder.abort()


def test_abandoned_attempt_can_replace_its_capture_builder(tmp_path: Path) -> None:
    compiled = _compiled_digital_test()
    first_attempt = compiled.new_upload_attempt()
    first_builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "first.sqlite3",
        compiled,
        upload_attempt=first_attempt,
    )
    connection = FixedIOProtocolConnection(
        serial_port=FakeSerial(),
        application=FixedIOProtocolAdapter(protocol_module=FakeProtocol),
        transport=FakeTransport(),
        result_adapter=IncomingResultAdapter(first_builder, protocol_module=FakeProtocol),
    )
    restarted = first_attempt.restart()
    second_builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "second.sqlite3",
        compiled,
        upload_attempt=restarted,
    )

    connection.bind_result_builder(second_builder, replace=True)
    queued = connection.queue_upload(compiled, upload_attempt=restarted)

    assert queued is restarted
    assert connection.result_adapter.builder is second_builder
    first_builder.abort()
    second_builder.abort()
