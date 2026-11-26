import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from assemblyline.common import forge
from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.result import Result, ResultSection
from assemblyline_v4_service.common.task import Task

from admarkup.admarkup import AdMarkup


def test_service_imports_from_service_path():
    assert AdMarkup.__module__ == "admarkup.admarkup"
    assert issubclass(AdMarkup, ServiceBase)


def test_execute_submits_each_unique_payload_and_logs_framework_rejections(tmp_path, monkeypatch):
    document = {
        "id": "response-1",
        "seatbid": [
            {
                "bid": [
                    {"id": "bid-1", "impid": "imp-1", "price": 1.0, "adm": "<p>synthetic</p>"},
                    {"id": "bid-2", "impid": "imp-2", "price": 2.0, "adm": "<p>synthetic</p>"},
                    {"id": "bid-3", "impid": "imp-3", "price": 3.0, "adm": "const marker = 3;"},
                    {"id": "bid-4", "impid": "imp-4", "price": 4.0, "adm": False},
                ]
            }
        ],
    }
    service = object.__new__(AdMarkup)
    monkeypatch.setattr(AdMarkup, "working_directory", property(lambda _self: str(tmp_path)), raising=False)
    request = Mock()
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(document), encoding="utf-8")
    request.file_path = str(input_path)
    request.file_size = input_path.stat().st_size
    request.extracted = []
    request.add_extracted.side_effect = [True, False]

    service.execute(request)

    first_content = b"<p>synthetic</p>"
    second_content = b"const marker = 3;"
    first_digest = hashlib.sha256(first_content).hexdigest()
    second_digest = hashlib.sha256(second_content).hexdigest()
    assert request.result is not None
    assert request.add_extracted.call_count == 2
    first_call, second_call = request.add_extracted.call_args_list
    assert Path(first_call.args[0]).parent == tmp_path
    assert Path(first_call.args[0]).read_bytes() == first_content
    assert first_call.args[1] == f"adm-{first_digest}.txt"
    assert first_call.args[2] == (
        f"Extracted OpenRTB adm payload; SHA-256: {first_digest}; "
        "source paths: $.seatbid[0].bid[0].adm, $.seatbid[0].bid[1].adm"
    )
    assert Path(second_call.args[0]).parent == tmp_path
    assert Path(second_call.args[0]).read_bytes() == second_content
    assert second_call.args[1] == f"adm-{second_digest}.txt"
    assert second_call.args[2] == (
        f"Extracted OpenRTB adm payload; SHA-256: {second_digest}; source paths: $.seatbid[0].bid[2].adm"
    )
    request.add_supplementary.assert_not_called()
    request.drop.assert_not_called()
    request.api.submit.assert_not_called()
    assert request.log.warning.call_count == 1
    assert "Assemblyline rejected" in request.log.warning.call_args.args[0]
    assert request.log.warning.call_args.args[1:] == (f"adm-{second_digest}.txt", second_digest)

    assert len(request.result.sections) == 1
    summary_section = request.result.sections[0]
    assert summary_section.title_text == "Markup extraction summary"
    assert json.loads(summary_section.body) == {
        "Recognized Type": "OpenRTB response",
        "Markup occurrences": 3,
        "Extracted payloads": 1,
    }
    assert len(summary_section.subsections) == 1
    issues = summary_section.subsections[0]
    assert issues.title_text == "Extraction issues"
    assert "$.seatbid[0].bid[3].adm: Bid adm must be a string when present." in issues.body
    assert "Assemblyline rejected" not in issues.body
    assert "<p>synthetic</p>" not in summary_section.body
    assert "const marker = 3;" not in summary_section.body


def test_synthetic_fixture_preserves_child_bytes_provenance_and_diagnostics(tmp_path, monkeypatch):
    service = object.__new__(AdMarkup)
    monkeypatch.setattr(AdMarkup, "working_directory", property(lambda _self: str(tmp_path)), raising=False)
    fixture_path = Path(__file__).parent / "fixtures" / "openrtb_synthetic_response.json"

    request = Mock()
    request.file_path = str(fixture_path)
    request.file_size = fixture_path.stat().st_size
    request.extracted = []
    request.add_extracted.return_value = True

    service.execute(request)

    html_content = b'<div class="campaign">Synthetic display</div>'
    javascript_content = 'const campaign = "synthetic";\nconst label = "café";'.encode("utf-8")
    expected_children = (
        (html_content, "$.seatbid[0].bid[0].adm, $.seatbid[0].bid[1].adm"),
        (javascript_content, "$.seatbid[0].bid[2].adm"),
    )
    assert request.add_extracted.call_count == len(expected_children)
    for call, (content, source_paths) in zip(request.add_extracted.call_args_list, expected_children):
        digest = hashlib.sha256(content).hexdigest()
        child_path, filename, description = call.args
        assert Path(child_path).parent == tmp_path
        assert Path(child_path).read_bytes() == content
        assert filename == f"adm-{digest}.txt"
        assert description == f"Extracted OpenRTB adm payload; SHA-256: {digest}; source paths: {source_paths}"

    assert len(request.result.sections) == 1
    summary_section = request.result.sections[0]
    summary = json.loads(summary_section.body)
    assert summary["Recognized Type"] == "OpenRTB response"
    assert summary["Markup occurrences"] == 3
    assert summary["Extracted payloads"] == 2
    assert summary_section.heuristic is None
    sections = {section.title_text: section for section in summary_section.subsections}
    assert "$.seatbid[0].bid[3].adm: Bid adm must be a string when present." in sections["Extraction issues"].body
    network_section = sections["Network Indicators"]
    assert network_section.title_text == "Network Indicators"
    assert network_section.heuristic is None
    assert "Synthetic root decoy" not in network_section.body
    assert "Synthetic nested decoy" not in network_section.body
    assert network_section.tags == {
        "network.static.uri": ["https://ads.example.com/synthetic-campaign.png"],
        "network.static.domain": ["ads.example.com", "advertiser.example.com"],
    }
    assert "$.seatbid[0].bid[0].adomain[0]: advertiser.example.com" in network_section.body


def test_standalone_bid_fixture_extracts_markup_and_reports_indicators(tmp_path, monkeypatch):
    service = object.__new__(AdMarkup)
    monkeypatch.setattr(AdMarkup, "working_directory", property(lambda _self: str(tmp_path)), raising=False)
    request = Mock()
    request.file_path = str(Path(__file__).parent / "fixtures" / "openrtb_synthetic_bid.json")
    request.add_extracted.return_value = True

    service.execute(request)

    request.add_extracted.assert_called_once()
    path, filename, description = request.add_extracted.call_args.args
    content = b"<div>Synthetic standalone ad</div>"
    digest = hashlib.sha256(content).hexdigest()
    assert Path(path).read_bytes() == content
    assert filename == f"adm-{digest}.txt"
    assert description == f"Extracted OpenRTB bid adm payload; SHA-256: {digest}; source paths: $.adm"
    summary = request.result.sections[0]
    assert json.loads(summary.body) == {
        "Recognized Type": "OpenRTB bid",
        "Markup occurrences": 1,
        "Extracted payloads": 1,
    }
    network = summary.subsections[0]
    assert network.tags == {
        "network.static.uri": ["https://ads.example.com/creative.png"],
        "network.static.domain": ["ads.example.com", "advertiser.example.com"],
    }
    assert "$.adomain[0]: advertiser.example.com" in network.body


def test_prebid_fixture_extracts_unique_markup_and_reports_source_paths(tmp_path, monkeypatch):
    service = object.__new__(AdMarkup)
    monkeypatch.setattr(AdMarkup, "working_directory", property(lambda _self: str(tmp_path)), raising=False)
    fixture_path = Path(__file__).parent / "fixtures" / "prebid_synthetic_auction_end.json"
    request = Mock()
    request.file_path = str(fixture_path)
    request.add_extracted.return_value = True

    service.execute(request)

    assert request.add_extracted.call_count == 2
    html_call, js_call = request.add_extracted.call_args_list
    assert Path(html_call.args[0]).read_bytes() == b'<div class="campaign">Synthetic display</div>'
    assert Path(js_call.args[0]).read_bytes() == 'const campaign = "synthetic";\nconst label = "café";'.encode()
    assert html_call.args[2].startswith("Extracted Prebid.js adm payload;")
    assert js_call.args[2].startswith("Extracted Prebid.js adm payload;")
    assert "source paths: $.payload.bidsReceived[0].adm, $.payload.bidsReceived[1].adm" in html_call.args[2]
    assert "source paths: $.payload.bidsReceived[2].adm" in js_call.args[2]

    summary_section = request.result.sections[0]
    assert summary_section.title_text == "Markup extraction summary"
    summary = json.loads(summary_section.body)
    assert summary["Recognized Type"] == "Prebid.js auction event"
    assert summary["Markup occurrences"] == 3
    assert summary["Extracted payloads"] == 2
    sections = {section.title_text: section for section in summary_section.subsections}
    assert "$.payload.bidsReceived[3].adm: Bid adm must be a string when present." in sections["Extraction issues"].body
    network_section = sections["Network Indicators"]
    assert "$.payload.bidsReceived[0].adomain[0]" in network_section.body
    assert network_section.tags["network.static.domain"] == ["advertiser.example.com"]


@pytest.mark.parametrize("raw", [b"\xff", b"not json", b'{"message": "not a bid response"}'])
def test_unrecognized_input_is_not_reported(tmp_path, monkeypatch, raw):
    service = object.__new__(AdMarkup)
    extract = Mock()
    monkeypatch.setattr(AdMarkup, "_extract_payloads", extract)
    request = Mock()
    input_path = tmp_path / "input.json"
    input_path.write_bytes(raw)
    request.file_path = str(input_path)

    service.execute(request)

    extract.assert_not_called()
    request.add_extracted.assert_not_called()
    assert request.result.sections == []


def test_service_manifest_scopes_json_and_disables_privilege_and_internet():
    manifest_path = Path(__file__).parents[1] / "service_manifest.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert manifest["accepts"] == "^text/json$"
    assert manifest["rejects"] == "empty"
    assert manifest["stage"] == "EXTRACT"
    assert manifest["category"] == "Extraction"
    assert manifest["file_required"] is True
    assert manifest["enabled"] is True
    assert manifest["privileged"] is False
    assert manifest["is_external"] is False
    assert manifest["update_channel"] == "stable"
    assert manifest["docker_config"]["allow_internet_access"] is False
    assert not manifest.get("dependencies")
    assert "config" not in manifest


def test_framework_preserves_minimum_classification_for_child_and_result(tmp_path):
    classification = forge.get_classification()
    minimum = classification.RESTRICTED
    task = Task.__new__(Task)
    task._classification = classification
    task.min_classification = minimum
    task.service_default_result_classification = classification.UNRESTRICTED
    task.result = Result(
        [
            ResultSection(
                "Neutral extraction summary",
                "Payload extraction completed.",
                classification=classification.UNRESTRICTED,
            )
        ]
    )
    task.extracted = []
    task.supplementary = []
    task._service_started = None
    task._service_completed = None
    task.service_version = None
    task.service_tool_version = None
    task.service_name = "AdMarkup"
    task.service_context = None
    task.service_debug_info = None
    task.fileinfo = SimpleNamespace(type="text/json", size=1, sha256="synthetic-sha256")
    task.drop_file = False
    task.partial_result = False
    task.temp_submission_data = {}

    child_path = tmp_path / "child.txt"
    child_path.write_bytes(b"synthetic")
    child = task._add_file(str(child_path), "child.txt", "Synthetic extracted payload")
    task.extracted.append(child)

    expected_classification = classification.max_classification(minimum, classification.UNRESTRICTED)
    service_result = task.get_service_result()
    assert child["classification"] == expected_classification
    assert service_result["classification"] == expected_classification


def test_identify_routes_txt_children_from_content_not_filename(tmp_path):
    from assemblyline.common.identify import Identify

    html = (
        b"<!doctype html><html><head><title>Synthetic</title></head><body>"
        + b"<p>Benign synthetic advertisement content.</p>" * 8
        + b"</body></html>"
    )
    javascript = (
        b"const syntheticMarker = 'benign';\n"
        b"function renderSyntheticAd() {\n"
        b"  return document.createElement('div');\n"
        b"}\n"
    ) * 8
    markdown = (b"# Synthetic Markdown\n\nThis is ordinary explanatory text, not a script or executable payload.\n") * 8

    with Identify(use_cache=False) as identify:
        for content, expected_type in (
            (html, "code/html"),
            (javascript, "code/javascript"),
            (markdown, "text/plain"),
        ):
            child_path = tmp_path / f"adm-{hashlib.sha256(content).hexdigest()}.txt"
            child_path.write_bytes(content)
            identified = identify.fileinfo(str(child_path), generate_hashes=False)
            assert identified["type"] == expected_type
