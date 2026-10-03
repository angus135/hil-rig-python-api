import json
from pathlib import Path

import pytest
from protocol_fakes import (
    ControlCommand,
    FakeProtocol,
    GlobalControlCommand,
    ResponseScope,
)
from protocol_fakes import (
    TestConfiguration as ProtocolTestConfiguration,
)
from protocol_fakes import (
    TestInstruction as ProtocolTestInstruction,
)

from hilrig import FixedIOProtocolAdapter, ManualMessageDefinitionError, load_manual_message

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "manual_messages"


def test_example_manual_messages_build_public_protocol_values() -> None:
    adapter = FixedIOProtocolAdapter(protocol_module=FakeProtocol)

    configuration = load_manual_message(_EXAMPLES / "configuration.json", adapter)
    instruction = load_manual_message(_EXAMPLES / "instruction.json", adapter)
    start = load_manual_message(_EXAMPLES / "start.json", adapter)
    reset = load_manual_message(_EXAMPLES / "reset-application.json", adapter)

    assert type(configuration.message) is ProtocolTestConfiguration
    assert configuration.message.digital_out[0].enabled
    assert configuration.response.scope is ResponseScope.TEST_CONFIGURATION
    assert type(instruction.message) is ProtocolTestInstruction
    assert instruction.message.tick_number == 10
    assert instruction.message.digital_outputs[0].high
    assert instruction.message.analog_outputs[0].microvolts == 2_500_000
    assert instruction.message.pwm_outputs[0].duty_cycle_permyriad == 5_000
    assert instruction.response.scope is ResponseScope.TICK
    assert start.message.command is ControlCommand.START
    assert reset.message.command is GlobalControlCommand.RESET_APPLICATION


def test_manual_message_loader_rejects_invalid_channel_and_test_id(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps(
            {
                "type": "test_instruction",
                "test_id": "not-a-test-id",
                "tick": 1,
                "digital_outputs": [{"channel": 12, "high": True}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ManualMessageDefinitionError, match="32 hexadecimal"):
        load_manual_message(path, FixedIOProtocolAdapter(protocol_module=FakeProtocol))
