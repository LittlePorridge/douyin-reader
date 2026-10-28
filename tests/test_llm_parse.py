import pytest

from src.llm_client import _parse_json_lenient


def test_strips_markdown_fence_and_prefix():
    assert _parse_json_lenient('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json_lenient('好的：{"a": 2}') == {"a": 2}


def test_accepts_raw_newline_inside_string():
    # LLM 常在字符串里输出未转义的换行，原先会报 Invalid control character
    raw = '{"summary": "第一行\n第二行", "key_points": []}'
    assert _parse_json_lenient(raw)["summary"] == "第一行\n第二行"


def test_no_json_raises():
    with pytest.raises(ValueError):
        _parse_json_lenient("no json here")
