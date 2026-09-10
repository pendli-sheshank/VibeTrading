from __future__ import annotations

from vibetrading.llm.parsing import parse_json_response


def test_parse_plain_json():
    assert parse_json_response('{"a": 1}') == {"a": 1}


def test_parse_code_fenced_json():
    content = '```json\n{"a": 1, "b": "x"}\n```'
    assert parse_json_response(content) == {"a": 1, "b": "x"}


def test_parse_invalid_json_returns_none():
    assert parse_json_response("not json at all") is None


def test_parse_empty_string_returns_none():
    assert parse_json_response("") is None


def test_parse_json_array_returns_none():
    assert parse_json_response("[1, 2, 3]") is None
