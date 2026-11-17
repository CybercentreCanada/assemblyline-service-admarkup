import builtins
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from assemblyline_v4_service.common.task import MaxExtractedExceeded

import admarkup.admarkup as admarkup_module
from admarkup.admarkup import AdMarkup
from admarkup.limits import ResourceLimits
from admarkup.openrtb import recognize_openrtb_response


def _service(tmp_path, monkeypatch, **overrides):
    service = object.__new__(AdMarkup)
    service.limits = ResourceLimits(**overrides)
    monkeypatch.setattr(AdMarkup, "working_directory", property(lambda _self: str(tmp_path)), raising=False)
    submission = SimpleNamespace(max_extraction_depth=6, max_file_size=100 * 1_048_576)
    monkeypatch.setattr(admarkup_module.forge, "get_config", lambda: SimpleNamespace(submission=submission))
    return service


def _document(*markups):
    bids = [
        {"id": f"bid-{index}", "impid": f"imp-{index}", "price": index + 1, "adm": markup}
        for index, markup in enumerate(markups)
    ]
    return json.dumps({"id": "response-1", "seatbid": [{"bid": bids}]}).encode("utf-8")


def _request(tmp_path, raw, *, max_extracted=0, depth=0, reported_size=None):
    input_path = tmp_path / "input.json"
    input_path.write_bytes(raw)
    request = Mock()
    request.file_path = str(input_path)
    request.file_size = len(raw) if reported_size is None else reported_size
    request.max_extracted = max_extracted
    request.extracted = []
    request.task.depth = depth

    def add_extracted(path, name, description):
        request.extracted.append({"path": path, "name": name, "description": description})
        return True

    request.add_extracted.side_effect = add_extracted
    return request


def _summary(request):
    return json.loads(request.result.sections[0].body)


def _result_text(request):
    return "\n".join(str(section.body) for section in request.result.sections[1:])


def test_resource_limits_reject_invalid_settings():
    with pytest.raises(ValueError, match="max_json_depth"):
        ResourceLimits(max_json_depth=0)
    with pytest.raises(ValueError, match="max_json_nodes"):
        ResourceLimits(max_json_nodes=True)
    with pytest.raises(ValueError, match="Missing resource limit configuration"):
        ResourceLimits.from_config({})


def test_service_rejects_invalid_manifest_configuration():
    with pytest.raises(ValueError, match="max_json_nodes"):
        AdMarkup(config={"max_json_nodes": 0})


@pytest.mark.parametrize(
    ("max_depth", "recognized"),
    [(3, True), (2, False)],
)
def test_json_depth_counts_root_as_zero(max_depth, recognized):
    document = json.dumps({"id": "response-1", "seatbid": [], "ext": {"a": {"b": 1}}})

    result = recognize_openrtb_response(document, ResourceLimits(max_json_depth=max_depth))

    assert result.recognized is recognized
    if not recognized:
        assert "depth 3 exceeds" in result.issues[0].message


@pytest.mark.parametrize(
    ("max_nodes", "recognized"),
    [(3, True), (2, False)],
)
def test_json_node_count_includes_values_but_not_object_keys(max_nodes, recognized):
    result = recognize_openrtb_response(
        '{"id":"response-1","seatbid":[]}',
        ResourceLimits(max_json_nodes=max_nodes),
    )

    assert result.recognized is recognized
    if not recognized:
        assert "node count exceeds" in result.issues[0].message


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


def test_input_at_byte_limit_is_processed(tmp_path, monkeypatch):
    raw = _document("ok")
    service = _service(tmp_path, monkeypatch, max_input_bytes=len(raw))
    request = _request(tmp_path, raw)

    service.execute(request)

    assert _summary(request)["Extracted payloads"] == 1
    assert not _summary(request)["Truncated"]


def test_oversized_input_is_refused_without_opening_it(tmp_path, monkeypatch):
    raw = _document("ok")
    service = _service(tmp_path, monkeypatch, max_input_bytes=len(raw) - 1)
    request = _request(tmp_path, raw)
    open_spy = Mock(side_effect=AssertionError("oversized input must not be read"))
    monkeypatch.setattr(builtins, "open", open_spy)

    service.execute(request)

    assert not request.add_extracted.called
    assert _summary(request)["Recognized OpenRTB response"] is False
    assert "no content was read" in _result_text(request)
    open_spy.assert_not_called()


def test_input_read_is_bounded_when_size_metadata_is_stale(tmp_path, monkeypatch):
    raw = _document("payload that is too large")
    limit = 12
    service = _service(tmp_path, monkeypatch, max_input_bytes=limit)
    request = _request(tmp_path, raw, reported_size=0)
    original_open = builtins.open
    read_sizes = []

    class TrackingReader:
        def __init__(self, path, mode):
            self.stream = original_open(path, mode)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.stream.close()

        def read(self, size=-1):
            read_sizes.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(builtins, "open", TrackingReader)

    service.execute(request)

    assert read_sizes == [limit + 1]
    assert not request.add_extracted.called
    assert "did not decode it" in _result_text(request)


def test_payload_limit_skips_oversize_payload_and_keeps_valid_siblings(tmp_path, monkeypatch):
    service = _service(
        tmp_path,
        monkeypatch,
        max_payload_bytes=4,
        max_total_child_bytes=100,
    )
    request = _request(tmp_path, _document("1234", "12345", "ok"))

    service.execute(request)

    assert [item["name"] for item in request.extracted] == [
        f"adm-{hashlib.sha256(b'1234').hexdigest()}.txt",
        f"adm-{hashlib.sha256(b'ok').hexdigest()}.txt",
    ]
    summary = _summary(request)
    assert summary["Extracted payloads"] == 2
    assert summary["Policy-skipped payloads"] == 1
    assert not summary["Truncated"]
    assert "5 UTF-8 bytes exceeds max_payload_bytes 4" in _result_text(request)


def test_payload_limit_counts_utf8_bytes_not_characters(tmp_path, monkeypatch):
    service = _service(
        tmp_path,
        monkeypatch,
        max_payload_bytes=3,
        max_total_child_bytes=10,
    )
    request = _request(tmp_path, _document("éé", "abc"))

    service.execute(request)

    assert len(request.extracted) == 1
    assert request.extracted[0]["path"].endswith(f"adm-{hashlib.sha256(b'abc').hexdigest()}.txt")
    assert "4 UTF-8 bytes exceeds max_payload_bytes 3" in _result_text(request)


def test_total_child_byte_limit_allows_exact_limit_then_reports_remainder(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, max_total_child_bytes=5)
    request = _request(tmp_path, _document("abc", "de", "f"))

    service.execute(request)

    assert len(request.extracted) == 2
    summary = _summary(request)
    assert summary["Extracted payloads"] == 2
    assert summary["Truncated"] is True
    assert "max_total_child_bytes 5 would be exceeded" in _result_text(request)
    assert "Unique payloads not submitted: 1" in _result_text(request)
    assert "UTF-8 bytes not submitted: 1" in _result_text(request)
    assert all(section.heuristic is None for section in request.result.sections)


def test_duplicate_payload_bytes_consume_aggregate_budget_once(tmp_path, monkeypatch):
    service = _service(
        tmp_path,
        monkeypatch,
        max_extracted_children=2,
        max_total_child_bytes=5,
    )
    request = _request(tmp_path, _document("abc", "abc", "de"))

    service.execute(request)

    assert len(request.extracted) == 2
    assert _summary(request)["Unique payloads"] == 2
    assert _summary(request)["Extracted payloads"] == 2
    assert not _summary(request)["Truncated"]


def test_unique_child_limit_counts_distinct_payloads(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, max_extracted_children=2)
    request = _request(tmp_path, _document("same", "same", "other"))

    service.execute(request)

    assert len(request.extracted) == 2
    assert _summary(request)["Unique payloads"] == 2
    assert not _summary(request)["Truncated"]
    assert "$.seatbid[0].bid[0].adm, $.seatbid[0].bid[1].adm" in _result_text(request)


def test_unique_child_limit_reports_one_unsubmitted_payload(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, max_extracted_children=2)
    request = _request(tmp_path, _document("one", "two", "three"))

    service.execute(request)

    assert len(request.extracted) == 2
    assert _summary(request)["Truncated"] is True
    assert "max_extracted_children 2 reached" in _result_text(request)
    assert "Unique payloads not submitted: 1" in _result_text(request)


def test_framework_max_extracted_is_respected_and_zero_is_unlimited(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    request = _request(tmp_path, _document("one", "two"), max_extracted=1)

    service.execute(request)

    assert len(request.extracted) == 1
    assert "Framework max_extracted 1 reached" in _result_text(request)
    assert _summary(request)["Truncated"] is True


def test_platform_depth_and_file_size_limits_preserve_eligible_sibling(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        admarkup_module.forge,
        "get_config",
        lambda: SimpleNamespace(submission=SimpleNamespace(max_extraction_depth=1, max_file_size=3)),
    )
    request = _request(tmp_path, _document("four", "abc"), depth=0)

    service.execute(request)

    assert len(request.extracted) == 1
    assert request.extracted[0]["name"].endswith(".txt")
    assert _summary(request)["Policy-skipped payloads"] == 1
    assert "platform max_file_size 3" in _result_text(request)


def test_platform_depth_limit_prevents_child_submission(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    request = _request(tmp_path, _document("one"), depth=6)

    service.execute(request)

    assert not request.add_extracted.called
    assert _summary(request)["Truncated"] is True
    assert "Platform max_extraction_depth 6 reached at parent depth 6" in _result_text(request)


def test_framework_extraction_exception_retains_prior_children_and_reports_truncation(tmp_path, monkeypatch):
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
    assert _summary(request)["Extracted payloads"] == 1
    assert _summary(request)["Truncated"] is True
    assert "Unique payloads not submitted: 2" in _result_text(request)


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
