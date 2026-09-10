from unittest.mock import patch

import pytest

from hilrig import Test as HilRigTest
from hilrig import UploadAttempt


def test_compiled_test_creates_attempt_for_its_immutable_definition() -> None:
    compiled = HilRigTest(name="Upload identity").compile()
    wire_id = compiled.test_id ^ 1

    with patch("hilrig.models.identifiers.secrets.randbits", return_value=wire_id):
        attempt = compiled.new_upload_attempt()

    assert attempt.definition_test_id == compiled.test_id
    assert attempt.application_test_id == wire_id


def test_upload_attempt_restart_keeps_definition_and_rekeys_wire_identity() -> None:
    with patch(
        "hilrig.models.identifiers.secrets.randbits",
        side_effect=[1, 2, 2, 3],
    ) as randbits:
        first = UploadAttempt.create(definition_test_id=1)
        restarted = first.restart()

    assert first.definition_test_id == restarted.definition_test_id == 1
    assert first.application_test_id == 2
    assert restarted.application_test_id == 3
    assert first.definition_test_id_hex == "00000000000000000000000000000001"
    assert restarted.application_test_id_hex == "00000000000000000000000000000003"
    assert randbits.call_count == 4


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("definition_test_id", True, TypeError),
        ("definition_test_id", -1, ValueError),
        ("application_test_id", 2**128, ValueError),
    ],
)
def test_upload_attempt_rejects_invalid_ids(
    field: str,
    value: object,
    error: type[Exception],
) -> None:
    values: dict[str, object] = {
        "definition_test_id": 1,
        "application_test_id": 2,
    }
    values[field] = value

    with pytest.raises(error):
        UploadAttempt(**values)  # type: ignore[arg-type]
