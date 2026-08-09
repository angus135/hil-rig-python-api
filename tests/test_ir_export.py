import json
import zipfile
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from hilrig import (
    DigitalState,
    FrequencyMode,
    LogicVoltage,
    StartMode,
    UARTLengthBits,
    UARTMode,
    UARTParity,
    UARTStopBits,
)
from hilrig import Test as HilRigTest
from hilrig.models.execution import CompiledTestIR


def _compiled_example() -> CompiledTestIR:
    test = HilRigTest(name="IR export example")
    test.configure(
        frequency_mode=FrequencyMode.HZ_10K,
        start_mode=StartMode.HOST_COMMAND,
    )
    digital_input = test.digital_input(channel=0)
    digital_input.configure(voltage=LogicVoltage.V3_3)
    output = test.digital_output(channel=1)
    output.configure(voltage=LogicVoltage.V5, initial_state=DigitalState.LOW)
    uart = test.uart(channel=0)
    uart.configure(
        mode=UARTMode.TTL_3V3,
        baud_hz=115_200,
        parity=UARTParity.NONE,
        length=UARTLengthBits.EIGHT,
        stop=UARTStopBits.ONE,
    )
    output.high(at_tick=20)
    uart.write(data=b"OK\r\n", at_tick=10)
    test.expect(digital_input).high(at_tick=15)
    return test.compile()


def test_compile_returns_an_immutable_chronological_snapshot() -> None:
    compiled = _compiled_example()

    assert isinstance(compiled, CompiledTestIR)
    assert [item.tick for item in compiled.instructions] == [10, 20]
    assert [item.instruction_id for item in compiled.instructions] == [1, 0]
    assert len(compiled.test_id_hex) == 32
    assert int(compiled.test_id_hex, 16) == compiled.test_id

    with pytest.raises(TypeError):
        compiled.instructions[0].arguments["data"] = "changed"
    with pytest.raises(FrozenInstanceError):
        compiled.name = "changed"


def test_machine_ir_uses_stable_values_and_excludes_assertions() -> None:
    machine_ir = _compiled_example().to_dict()

    assert machine_ir["ir_version"] == "1.0"
    assert machine_ir["test"]["frequency_mode"] == "HZ_10K"
    assert machine_ir["test"]["start_mode"] == "HOST_COMMAND"
    assert "assertions" not in machine_ir
    assert machine_ir["instructions"][0] == {
        "instruction_id": 1,
        "tick": 10,
        "peripheral": "uart",
        "channel": 0,
        "operation": "write",
        "arguments": {"data": "0x4f4b0d0a"},
    }


def test_json_can_be_returned_or_written(tmp_path: Path) -> None:
    compiled = _compiled_example()

    json_text = compiled.to_json()
    output_path = compiled.write_json(tmp_path / "compiled-test.json")

    assert json.loads(json_text) == json.loads(output_path.read_text(encoding="utf-8"))
    assert output_path.is_absolute()
    with pytest.raises(ValueError, match="end in .json"):
        compiled.write_json(tmp_path / "compiled-test.txt")


def test_excel_contains_the_four_human_sheets_and_host_assertions(tmp_path: Path) -> None:
    compiled = _compiled_example()
    output_path = compiled.write_excel(tmp_path / "compiled-test.xlsx")

    assert output_path.is_absolute()
    with zipfile.ZipFile(output_path) as workbook:
        workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
        assertions_xml = workbook.read("xl/worksheets/sheet4.xml").decode("utf-8")

    for name in ("Test Summary", "Configurations", "Instructions", "Assertions"):
        assert f'name="{name}"' in workbook_xml
    assert "state_at_tick" in assertions_xml
    assert 'expected_state="HIGH"' in assertions_xml

    with pytest.raises(ValueError, match="end in .xlsx"):
        compiled.write_excel(tmp_path / "compiled-test.xls")
