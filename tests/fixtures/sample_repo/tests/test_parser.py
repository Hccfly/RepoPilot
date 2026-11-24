from src.parser import parse_input


def test_parse_input() -> None:
    assert parse_input(" OK ") == "ok"
