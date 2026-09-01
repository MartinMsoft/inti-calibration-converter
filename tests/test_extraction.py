import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extraction import parse_json_response


def test_parse_json_response_plain():
    raw = '{"rows": [{"pos": 0, "values": [1.0, 2.0]}]}'
    assert parse_json_response(raw) == [{"pos": 0, "values": [1.0, 2.0]}]


def test_parse_json_response_with_verification_text_before():
    # El prompt le pide al modelo escribir una verificacion en texto plano
    # antes del JSON -- el parser tiene que ubicar el JSON igual.
    raw = 'Verificacion: 23.240, 24.263, 25.286\n{"rows": [{"pos": 0, "values": [23.240]}]}'
    assert parse_json_response(raw) == [{"pos": 0, "values": [23.240]}]


def test_parse_json_response_with_trailing_text_after():
    raw = '{"rows": [{"pos": 0, "values": [1.0]}]}\nListo, eso es todo.'
    assert parse_json_response(raw) == [{"pos": 0, "values": [1.0]}]


def test_parse_json_response_with_markdown_fences():
    raw = '```json\n{"rows": []}\n```'
    assert parse_json_response(raw) == []


def test_parse_json_response_no_json_raises():
    with pytest.raises(ValueError):
        parse_json_response("no hay tabla en esta pagina")
