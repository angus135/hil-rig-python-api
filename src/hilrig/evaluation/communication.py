"""Evaluation of stored communication peripheral assertions."""

from __future__ import annotations

from hilrig.evaluation.common import integer_argument, make_result
from hilrig.evaluation.context import EvaluationContext
from hilrig.evaluation.models import AssertionResult, EvaluationVerdict
from hilrig.models.execution import CompiledAssertion
from hilrig.results.models import CommunicationPeripheral

_PERIPHERAL_MAP = {
    "uart": CommunicationPeripheral.UART,
    "spi": CommunicationPeripheral.SPI,
    "i2c": CommunicationPeripheral.I2C,
    "can": CommunicationPeripheral.CAN,
}


def evaluate_communication_receive(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    """Evaluate whether a payload was received within the half-open tick range."""
    from_tick = integer_argument(assertion, "from_tick")
    until_tick = integer_argument(assertion, "until_tick")
    expected_hex = str(assertion.arguments.get("expected_payload", ""))
    expected_bytes = bytes.fromhex(
        expected_hex[2:] if expected_hex.startswith("0x") else expected_hex
    )

    peripheral_enum = _PERIPHERAL_MAP.get(assertion.peripheral)
    channel = assertion.channel

    captures = list(
        context.captured_run.iter_communications(
            peripheral=peripheral_enum,
            channel=channel,
            from_tick=from_tick,
            until_tick=until_tick,
        )
    )

    matched = False
    matching_tick: int | None = None
    observed_payloads = []

    for capture in captures:
        observed_payloads.append(f"0x{capture.payload.hex()}")
        if capture.payload == expected_bytes:
            matched = True
            matching_tick = capture.tick
            break

    if matched:
        return make_result(
            assertion,
            verdict=EvaluationVerdict.PASS,
            message=(
                f"{assertion.subject_name} received the expected payload at tick {matching_tick}."
            ),
            observed={
                "matched": True,
                "matching_tick": matching_tick,
                "captured_count": len(captures),
                "payloads": ", ".join(observed_payloads[:5]),
            },
            valid_sample_count=len(captures),
            violation_count=0,
        )
    else:
        return make_result(
            assertion,
            verdict=EvaluationVerdict.FAIL,
            message=(
                f"{assertion.subject_name} did not receive the expected payload "
                f"0x{expected_bytes.hex()} between tick {from_tick} and {until_tick}. "
                f"Observed {len(captures)} captures: [{', '.join(observed_payloads[:5])}]."
            ),
            observed={
                "matched": False,
                "matching_tick": None,
                "captured_count": len(captures),
                "payloads": ", ".join(observed_payloads[:5]),
            },
            valid_sample_count=len(captures),
            violation_count=1,
            first_failure_tick=from_tick,
        )
