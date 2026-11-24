"""Identifies and extracts advertising markup from supported OpenRTB bid responses."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from assemblyline.common import forge
from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.request import ServiceRequest
from assemblyline_v4_service.common.result import Result, ResultKeyValueSection, ResultSection
from assemblyline_v4_service.common.task import MaxExtractedExceeded

from admarkup.openrtb import OpenRTBRecognition, PreparedPayload, recognize_openrtb_response, rejected_input


@dataclass
class _Extraction:
    """Counters and report lines accumulated while submitting payloads."""

    skip_lines: list[str]
    payload_lines: list[str] = field(default_factory=list)
    truncation_lines: list[str] = field(default_factory=list)
    extracted: int = 0
    rejected: int = 0
    policy_skipped: int = 0

    def skip_oversize(self, payload: PreparedPayload, max_file_size: int) -> None:
        self.policy_skipped += 1
        self.skip_lines.append(
            f"{_paths(payload)}: skipped {payload.filename} (SHA-256 {payload.sha256}; "
            f"{payload.byte_length} bytes exceeds platform max_file_size {max_file_size})."
        )

    def record_submission(self, payload: PreparedPayload, accepted: bool) -> None:
        if accepted:
            self.extracted += 1
            outcome = "extracted"
        else:
            self.rejected += 1
            outcome = "rejected by Assemblyline"
            self.skip_lines.append(
                f"{_paths(payload)}: Assemblyline rejected {payload.filename} "
                f"(SHA-256 {payload.sha256}); it was not extracted."
            )
        self.payload_lines.append(
            f"{payload.filename} (SHA-256: {payload.sha256}; {payload.byte_length} bytes; "
            f"{outcome}; source paths: {_paths(payload)})"
        )

    def truncate(self, reason: str, remaining: Sequence[PreparedPayload]) -> None:
        self.truncation_lines.extend(
            (
                reason,
                f"Unique payloads not submitted: {len(remaining)}",
                f"UTF-8 bytes not submitted: {sum(item.byte_length for item in remaining)}",
            )
        )


def _paths(payload: PreparedPayload) -> str:
    return ", ".join(payload.source_paths)


def _report_sections(input_size: int, recognition: OpenRTBRecognition, extraction: _Extraction) -> list[ResultSection]:
    summary = ResultKeyValueSection("OpenRTB markup extraction summary")
    summary.set_item("Input bytes", input_size)
    summary.set_item("Recognized OpenRTB response", recognition.recognized)
    summary.set_item("Markup occurrences", len(recognition.markups))
    summary.set_item("Unique payloads", len(recognition.payloads))
    summary.set_item("Extracted payloads", extraction.extracted)
    summary.set_item("Rejected payloads", extraction.rejected)
    summary.set_item("Policy-skipped payloads", extraction.policy_skipped)
    summary.set_item("Skipped items", len(extraction.skip_lines))
    summary.set_item("Truncated", bool(extraction.truncation_lines))

    detail_sections = (
        ("Extracted payload provenance", extraction.payload_lines),
        ("OpenRTB extraction skips", extraction.skip_lines),
        ("Extraction limit reached", extraction.truncation_lines),
    )
    
    return [
        summary,
        *(
            ResultSection(title, "\n".join(lines))
            for title, lines in detail_sections if lines
        )
    ]


class AdMarkup(ServiceBase):
    """Identifies and extracts advertising markup from supported OpenRTB bid responses."""

    def execute(self, request: ServiceRequest) -> None:
        """Run the service."""
        result = Result()
        request.result = result

        input_size, recognition = self._recognize(request)
        if not recognition.recognized:
            return

        extraction = self._extract_payloads(request, recognition)

        for section in _report_sections(input_size, recognition, extraction):
            result.add_section(section)

        if recognition.network_indicators:
            section = ResultSection("OpenRTB network indicators")
            for indicator in recognition.network_indicators:
                section.add_line(f"{indicator.source_path}: {indicator.value} ({indicator.tag_type})")
                section.add_tag(indicator.tag_type, indicator.value)
            result.add_section(section)

    def _recognize(self, request: ServiceRequest) -> tuple[int, OpenRTBRecognition]:
        """Return the input size and OpenRTB recognition of the input file."""
        with open(request.file_path, "rb") as input_file:
            raw_document = input_file.read()

        try:
            document = raw_document.decode("utf-8")
        except UnicodeDecodeError:
            return len(raw_document), rejected_input("Invalid JSON: document must be UTF-8 encoded.")
        return len(raw_document), recognize_openrtb_response(document)

    def _extract_payloads(self, request: ServiceRequest, recognition: OpenRTBRecognition) -> _Extraction:
        """Return the extraction outcome after submitting each unique payload as an extracted child."""
        extraction = _Extraction(skip_lines=[f"{issue.path}: {issue.message}" for issue in recognition.issues])
        payloads = recognition.payloads
        max_file_size = forge.get_config().submission.max_file_size

        for index, payload in enumerate(payloads):
            if max_file_size > 0 and payload.byte_length > max_file_size:
                extraction.skip_oversize(payload, max_file_size)
                continue

            try:
                extraction.record_submission(payload, self._submit_child(request, payload))
            except MaxExtractedExceeded:
                extraction.truncate("Framework max_extracted was reached during submission.", payloads[index:])
                break

        return extraction

    def _submit_child(self, request: ServiceRequest, payload: PreparedPayload) -> bool:
        """Return whether Assemblyline accepted the payload after writing it and adding it as an extracted file."""
        path = Path(self.working_directory) / f"{uuid4().hex}-{payload.filename}"
        path.write_bytes(payload.content)
        description = f"Extracted OpenRTB adm payload; SHA-256: {payload.sha256}; source paths: {_paths(payload)}"
        accepted = request.add_extracted(str(path), payload.filename, description)
        if not accepted:
            request.log.warning(
                "Assemblyline rejected extracted AdMarkup payload %s (%s)",
                payload.filename,
                payload.sha256,
            )
        return accepted
