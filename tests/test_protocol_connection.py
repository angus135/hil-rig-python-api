from pathlib import Path

from protocol_fakes import (
    FakeProtocol,
    FakeSerial,
    FakeTransport,
    ResultCondition,
)
from protocol_fakes import (
    TestId as ProtocolTestId,
)
from protocol_fakes import (
    TestResult as ProtocolTestResult,
)

from hilrig import (
    CapturedRunBuilder,
    DigitalState,
    FixedIOProtocolAdapter,
    FixedIOProtocolConnection,
    IncomingResultAdapter,
    LogicVoltage,
)
from hilrig import Test as HilRigTest


def _compiled_digital_test():
    test = HilRigTest(name="Connection upload")
    output = test.digital_output(channel=0).configure(
        voltage=LogicVoltage.V3_3,
        initial_state=DigitalState.LOW,
    )
    output.high(at_tick=5)
    return test.compile()


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

    for _ in range(20):
        connection.service()
        if connection.upload_delivery_complete:
            break

    assert connection.upload_delivery_complete
    assert len(transport.submitted) == 2
    assert transport.committed == 2
    assert bytes(serial_port.written) == b"".join(
        b"frame:" + submitted for submitted in transport.submitted
    )


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
