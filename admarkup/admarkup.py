"""Identifies and extracts advertising markup from supported OpenRTB bid responses."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from assemblyline.common import forge
from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.request import ServiceRequest
from assemblyline_v4_service.common.result import Result, ResultKeyValueSection, ResultSection
from assemblyline_v4_service.common.task import MaxExtractedExceeded

from admarkup.limits import ResourceLimits
from admarkup.openrtb import OpenRTBRecognition, PreparedPayload, RecognitionIssue, recognize_openrtb_response


@dataclass
class _Extraction:
    """Counters and report lines accumulated while submitting payloads."""

    skip_lines: list[str]
    payload_lines: list[str] = field(default_factory=list)
    truncation_lines: list[str] = field(default_factory=list)
    extracted: int = 0
    rejected: int = 0
    policy_skipped: int = 0
    submitted: int = 0
    submitted_bytes: int = 0

    def skip_by_policy(self, payload: PreparedPayload, reason: str) -> None:
        self.policy_skipped += 1
        self.skip_lines.append(f"{_paths(payload)}: skipped {payload.filename} (SHA-256 {payload.sha256}; {reason}).")

    def record_submission(self, payload: PreparedPayload, accepted: bool) -> None:
        self.submitted += 1
        self.submitted_bytes += payload.byte_length
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


def _rejected_input(message: str) -> OpenRTBRecognition:
    return OpenRTBRecognition(
        recognized=False,
        markups=(),
        payloads=(),
        issues=(RecognitionIssue("$", message),),
    )


def _policy_skip_reason(payload: PreparedPayload, limits: ResourceLimits, platform_max_file_size: int) -> str | None:
    """Return why a single payload must be skipped, leaving its siblings eligible."""
    if payload.byte_length > limits.max_payload_bytes:
        return f"{payload.byte_length} UTF-8 bytes exceeds max_payload_bytes {limits.max_payload_bytes}"
    if platform_max_file_size > 0 and payload.byte_length > platform_max_file_size:
        return f"{payload.byte_length} bytes exceeds platform max_file_size {platform_max_file_size}"
    return None


def _budget_exhausted_reason(
    payload: PreparedPayload, extraction: _Extraction, limits: ResourceLimits, request: ServiceRequest
) -> str | None:
    """Return why submission must stop before this payload, truncating all remaining ones."""
    if extraction.submitted >= limits.max_extracted_children:
        return f"Configured max_extracted_children {limits.max_extracted_children} reached."
    if extraction.submitted_bytes + payload.byte_length > limits.max_total_child_bytes:
        return f"Configured max_total_child_bytes {limits.max_total_child_bytes} would be exceeded."
    framework_max = request.max_extracted
    if framework_max and len(request.extracted) >= framework_max:
        return f"Framework max_extracted {framework_max} reached."
    return None


def _framework_exceeded_reason(framework_max: int) -> str:
    if framework_max:
        return f"Framework max_extracted {framework_max} was reached during submission."
    return "Framework raised MaxExtractedExceeded while submitting a child."


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
    return [summary, *(ResultSection(title, "\n".join(lines)) for title, lines in detail_sections if lines)]


class AdMarkup(ServiceBase):
    """Identifies and extracts advertising markup from supported OpenRTB bid responses."""

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.limits = ResourceLimits.from_config(self.config)

    def execute(self, request: ServiceRequest) -> None:
        """Run the service."""
        result = Result()
        request.result = result

        input_size, recognition = self._recognize(request)
        extraction = self._extract_payloads(request, recognition)
        for section in _report_sections(input_size, recognition, extraction):
            result.add_section(section)

    def _recognize(self, request: ServiceRequest) -> tuple[int, OpenRTBRecognition]:
        """Return the input size and OpenRTB recognition, reading the input within the size limit."""
        max_input_bytes = self.limits.max_input_bytes
        input_size = request.file_size
        if input_size > max_input_bytes:
            return input_size, _rejected_input(
                f"Input size {input_size} bytes exceeds max_input_bytes {max_input_bytes}; no content was read."
            )

        with open(request.file_path, "rb") as input_file:
            raw_document = input_file.read(max_input_bytes + 1)

        input_size = len(raw_document)
        if input_size > max_input_bytes:
            return input_size, _rejected_input(
                f"Input exceeds max_input_bytes {max_input_bytes}; "
                f"read at most {input_size} bytes and did not decode it."
            )

        try:
            document = raw_document.decode("utf-8")
        except UnicodeDecodeError:
            return input_size, _rejected_input("Invalid JSON: document must be UTF-8 encoded.")
        return input_size, recognize_openrtb_response(document, self.limits)

    def _extract_payloads(self, request: ServiceRequest, recognition: OpenRTBRecognition) -> _Extraction:
        """Return the extraction outcome after submitting unique payloads until a policy or budget stops it."""
        extraction = _Extraction(skip_lines=[f"{issue.path}: {issue.message}" for issue in recognition.issues])
        payloads = recognition.payloads
        platform_submission = forge.get_config().submission
        parent_depth = request.task.depth

        if payloads and parent_depth >= platform_submission.max_extraction_depth:
            extraction.truncate(
                f"Platform max_extraction_depth {platform_submission.max_extraction_depth} "
                f"reached at parent depth {parent_depth}.",
                payloads,
            )
            return extraction

        for index, payload in enumerate(payloads):
            skip_reason = _policy_skip_reason(payload, self.limits, platform_submission.max_file_size)
            if skip_reason:
                extraction.skip_by_policy(payload, skip_reason)
                continue

            stop_reason = _budget_exhausted_reason(payload, extraction, self.limits, request)
            if stop_reason is None:
                try:
                    extraction.record_submission(payload, self._submit_child(request, payload))
                except MaxExtractedExceeded:
                    stop_reason = _framework_exceeded_reason(request.max_extracted)

            if stop_reason:
                extraction.truncate(stop_reason, payloads[index:])
                break

        return extraction

    def _submit_child(self, request: ServiceRequest, payload: PreparedPayload) -> bool:
        """Return whether Assemblyline accepted the payload after writing it and adding it as an extracted file."""
        path = Path(self.working_directory) / payload.filename
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
