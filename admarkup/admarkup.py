"""Extract advertising markup from supported OpenRTB and Prebid.js documents."""

from pathlib import Path
from uuid import uuid4

from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.request import ServiceRequest
from assemblyline_v4_service.common.result import Result, ResultKeyValueSection, ResultSection
from assemblyline_v4_service.common.task import MaxExtractedExceeded

from admarkup.openrtb import OpenRTBRecognition, PreparedPayload, SourceFormat, recognize_openrtb_response, rejected_input


def _report_section(recognition: OpenRTBRecognition, extracted: int) -> ResultSection:
    summary = ResultKeyValueSection("Markup extraction summary")

    summary.set_item("Recognized Type", recognition.source_format.recognized_label)
    summary.set_item("Markup occurrences", len(recognition.markups))
    summary.set_item("Extracted payloads", extracted)

    details = [f"{issue.path}: {issue.message}" for issue in recognition.issues]
    if details:
        summary.add_subsection(ResultSection("Extraction issues", "\n".join(details)))

    return summary


class AdMarkup(ServiceBase):
    """Extract advertising markup from supported OpenRTB and Prebid.js documents."""

    def execute(self, request: ServiceRequest) -> None:
        """Run the service."""
        result = Result()
        request.result = result

        recognition = self._recognize(request)

        if not recognition.recognized:
            return

        extracted = self._extract_payloads(request, recognition)

        summary_section = _report_section(recognition, extracted)
        result.add_section(summary_section)

        if recognition.network_indicators:
            section = ResultSection("Network Indicators")
            for indicator in recognition.network_indicators:
                section.add_line(f"{indicator.source_path}: {indicator.value} ({indicator.tag_type})")
                section.add_tag(indicator.tag_type, indicator.value)
            summary_section.add_subsection(section)

    def _recognize(self, request: ServiceRequest) -> OpenRTBRecognition:
        """Return the recognition of the input file."""
        with open(request.file_path, "rb") as input_file:
            raw_document = input_file.read()

            try:
                return recognize_openrtb_response(raw_document.decode("utf-8"))
            except UnicodeDecodeError:
                return rejected_input("Invalid JSON: document must be UTF-8 encoded.")

    def _extract_payloads(self, request: ServiceRequest, recognition: OpenRTBRecognition) -> int:
        """Return the number of unique payloads accepted as extracted children."""
        payloads = recognition.payloads
        extracted = 0

        for index, payload in enumerate(payloads):
            try:
                accepted = self._submit_child(request, payload, recognition.source_format)
            except MaxExtractedExceeded:
                request.log.warning(
                    "Reached max_extracted limit; %d unique AdMarkup payloads were not submitted",
                    len(payloads) - index,
                )
                break
            if accepted:
                extracted += 1
        return extracted

    def _submit_child(self, request: ServiceRequest, payload: PreparedPayload, source_format: SourceFormat) -> bool:
        """Return whether Assemblyline accepted the payload after writing it and adding it as an extracted file."""
        path = Path(self.working_directory) / f"{uuid4().hex}-{payload.filename}"
        path.write_bytes(payload.content)
        description = (
            f"Extracted {source_format.description} adm payload; SHA-256: {payload.sha256}; "
            f"source paths: {', '.join(payload.source_paths)}"
        )
        accepted = request.add_extracted(str(path), payload.filename, description)
        if not accepted:
            request.log.warning(
                "Assemblyline rejected extracted AdMarkup payload %s (%s)",
                payload.filename,
                payload.sha256,
            )
        return accepted
