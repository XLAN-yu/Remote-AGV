import pytest

from safety import (
    ClearEstopMessage,
    DriveMessage,
    EstopMessage,
    MessageValidationError,
    parse_client_message,
)


def test_valid_messages_are_parsed_without_string_coercion() -> None:
    assert parse_client_message(
        '{"type":"drive","linear":0.2,"angular":-0.45,"seq":126}'
    ) == DriveMessage(0.2, -0.45, 126)
    assert parse_client_message({"type": "estop", "seq": 127}) == EstopMessage(127)
    assert parse_client_message(
        {"type": "clear_estop", "seq": 128, "confirm": True}
    ) == ClearEstopMessage(128)


@pytest.mark.parametrize(
    "message,code",
    [
        ({"type": "drive", "linear": "0.2", "angular": 0, "seq": 1}, "invalid_drive"),
        ({"type": "drive", "linear": True, "angular": 0, "seq": 1}, "invalid_drive"),
        ({"type": "drive", "linear": 1.01, "angular": 0, "seq": 1}, "drive_out_of_range"),
        ({"type": "drive", "linear": 0, "angular": 3.01, "seq": 1}, "drive_out_of_range"),
        ({"type": "drive", "linear": 0, "angular": 0, "seq": True}, "invalid_seq"),
        ({"type": "drive", "linear": 0, "angular": 0, "seq": -1}, "invalid_seq"),
        (
            {"type": "drive", "linear": 0, "angular": 0, "seq": 1, "pwm": 99},
            "invalid_fields",
        ),
        ({"type": "clear_estop", "seq": 2, "confirm": False}, "confirmation_required"),
    ],
)
def test_invalid_messages_are_rejected(message: dict[str, object], code: str) -> None:
    with pytest.raises(MessageValidationError) as caught:
        parse_client_message(message)
    assert caught.value.code == code


def test_non_finite_json_numbers_are_rejected() -> None:
    with pytest.raises(MessageValidationError) as caught:
        parse_client_message('{"type":"drive","linear":NaN,"angular":0,"seq":1}')
    assert caught.value.code == "invalid_json_number"
