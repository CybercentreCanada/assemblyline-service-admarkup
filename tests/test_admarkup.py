import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import yaml
from assemblyline.common import forge
from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.result import Result, ResultSection
from assemblyline_v4_service.common.task import Task

from admarkup.admarkup import AdMarkup


def test_service_imports_from_service_path():
    assert AdMarkup.__module__ == "admarkup.admarkup"
    assert issubclass(AdMarkup, ServiceBase)


def test_execute_submits_each_unique_payload_and_reports_framework_rejections(tmp_path, monkeypatch):
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
        "Extracted OpenRTB adm payload; SHA-256: "
        f"{first_digest}; source paths: $.seatbid[0].bid[0].adm, $.seatbid[0].bid[1].adm"
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

    summary = json.loads(request.result.sections[0].body)
    assert summary == {
        "Input bytes": input_path.stat().st_size,
        "Recognized OpenRTB response": True,
        "Markup occurrences": 3,
        "Unique payloads": 2,
        "Extracted payloads": 1,
        "Rejected payloads": 1,
        "Policy-skipped payloads": 0,
        "Skipped items": 2,
        "Truncated": False,
    }
    assert summary["Input bytes"] == input_path.stat().st_size
    section_text = "\n".join(str(section.body) for section in request.result.sections[1:])
    assert f"{first_digest}; 16 bytes; extracted" in section_text
    assert f"{second_digest}; 17 bytes; rejected by Assemblyline" in section_text
    assert "$.seatbid[0].bid[3].adm" in section_text
    assert "<p>synthetic</p>" not in section_text
    assert "const marker = 3;" not in section_text


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
        (
            html_content,
            ("$.seatbid[0].bid[0].adm", "$.seatbid[0].bid[1].adm"),
        ),
        (javascript_content, ("$.seatbid[0].bid[2].adm",)),
    )
    assert request.add_extracted.call_count == len(expected_children)
    for call, (content, source_paths) in zip(request.add_extracted.call_args_list, expected_children):
        digest = hashlib.sha256(content).hexdigest()
        child_path, filename, description = call.args
        assert Path(child_path).parent == tmp_path
        assert Path(child_path).read_bytes() == content
        assert filename == f"adm-{digest}.txt"
        assert description == (
            f"Extracted OpenRTB adm payload; SHA-256: {digest}; source paths: {', '.join(source_paths)}"
        )

    summary = json.loads(request.result.sections[0].body)
    assert summary == {
        "Input bytes": fixture_path.stat().st_size,
        "Recognized OpenRTB response": True,
        "Markup occurrences": 3,
        "Unique payloads": 2,
        "Extracted payloads": 2,
        "Rejected payloads": 0,
        "Policy-skipped payloads": 0,
        "Skipped items": 1,
        "Truncated": False,
    }
    assert all(section.heuristic is None for section in request.result.sections)
    skip_section = request.result.sections[2]
    assert skip_section.title_text == "OpenRTB extraction skips"
    assert skip_section.body == "$.seatbid[0].bid[3].adm: Bid adm must be a string when present."
    result_text = "\n".join(str(section.body) for section in request.result.sections[1:])
    assert "$.seatbid[0].bid[0].adm, $.seatbid[0].bid[1].adm" in result_text
    assert "$.seatbid[0].bid[2].adm" in result_text
    assert "Synthetic root decoy" not in result_text
    assert "Synthetic nested decoy" not in result_text
    network_section = next(
        section for section in request.result.sections if section.title_text == "OpenRTB network indicators"
    )
    assert network_section.tags == {
        "network.static.uri": ["https://ads.example.com/synthetic-campaign.png"],
        "network.static.domain": ["ads.example.com", "advertiser.example.com"],
    }
    assert "$.seatbid[0].bid[0].adomain[0]: advertiser.example.com" in network_section.body


def test_invalid_utf8_is_reported_as_a_skip(tmp_path, monkeypatch):
    service = object.__new__(AdMarkup)
    monkeypatch.setattr(AdMarkup, "working_directory", property(lambda _self: str(tmp_path)), raising=False)
    request = Mock()
    input_path = tmp_path / "input.json"
    input_path.write_bytes(b"\xff")
    request.file_path = str(input_path)
    request.file_size = 1
    request.extracted = []

    service.execute(request)

    assert request.add_extracted.call_count == 0
    summary = json.loads(request.result.sections[0].body)
    assert summary["Recognized OpenRTB response"] is False
    assert summary["Skipped items"] == 1
    assert "document must be UTF-8 encoded" in request.result.sections[1].body


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
