import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from assemblyline_v4_service.common.task import MaxExtractedExceeded

from admarkup.admarkup import AdMarkup
from admarkup.openrtb import recognize_openrtb_response


def _service(tmp_path, monkeypatch):
    service = object.__new__(AdMarkup)
    monkeypatch.setattr(AdMarkup, "working_directory", property(lambda _self: str(tmp_path)), raising=False)
    return service


def _request(tmp_path, raw):
    input_path = tmp_path / "input.json"
    input_path.write_bytes(raw)
    request = Mock()
    request.file_path = str(input_path)
    request.file_size = len(raw)
    request.extracted = []

    def add_extracted(path, name, description):
        request.extracted.append({"path": path, "name": name, "description": description})
        return True

    request.add_extracted.side_effect = add_extracted
    return request


def _document(*markups):
    bids = [
        {"id": f"bid-{index}", "impid": f"imp-{index}", "price": index + 1, "adm": markup}
        for index, markup in enumerate(markups)
    ]
    return json.dumps({"id": "response-1", "seatbid": [{"bid": bids}]}).encode("utf-8")


def test_decoder_recursion_limit_becomes_a_controlled_input_issue():
    nested = "[" * 1200 + "0" + "]" * 1200
    document = f'{{"id":"response-1","seatbid":[],"ext":{nested}}}'

    result = recognize_openrtb_response(document)

    assert not result.recognized
    assert result.payloads == ()
    assert "nesting" in result.issues[0].message


def test_json_numeric_parser_limit_is_reported_as_invalid_input():
    oversized_number = "9" * 5000
    document = (
        '{"id":"response-1","seatbid":[{"bid":[{"id":"bid-1","impid":"imp-1","price":'
        + oversized_number
        + ',"adm":"synthetic"}]}]}'
    )

    result = recognize_openrtb_response(document)

    assert not result.recognized
    assert result.payloads == ()
    assert "numeric value exceeds parser limits" in result.issues[0].message


def test_framework_extraction_exception_retains_prior_children_and_logs_truncation(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    request = _request(tmp_path, _document("one", "two", "three"))
    attempts = 0

    def add_extracted(path, name, description):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            request.extracted.append({"path": path, "name": name, "description": description})
            return True
        raise MaxExtractedExceeded

    request.add_extracted.side_effect = add_extracted

    service.execute(request)

    assert len(request.extracted) == 1
    assert attempts == 2
    request.log.warning.assert_called_once()
    assert "max_extracted" in request.log.warning.call_args.args[0]
    assert request.log.warning.call_args.args[1] == 2
    assert len(request.result.sections) == 1
    summary_section = request.result.sections[0]
    summary = json.loads(summary_section.body)
    assert summary["Extracted payloads"] == 1
    assert "Unique payloads not submitted" not in summary
    assert "Truncated" not in summary
    assert summary_section.subsections == []


def test_unexpected_child_file_io_error_surfaces(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    request = _request(tmp_path, _document("one"))
    monkeypatch.setattr(Path, "write_bytes", Mock(side_effect=OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        service.execute(request)


def test_unexpected_framework_error_surfaces(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    request = _request(tmp_path, _document("one"))
    request.add_extracted.side_effect = RuntimeError("framework unavailable")

    with pytest.raises(RuntimeError, match="framework unavailable"):
        service.execute(request)
