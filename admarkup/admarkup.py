"""Extract advertising markup from supported OpenRTB and Prebid.js documents."""

from pathlib import Path
from uuid import uuid4

from assemblyline.common import forge
from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.request import ServiceRequest
from assemblyline_v4_service.common.result import Result, ResultKeyValueSection, ResultSection
from assemblyline_v4_service.common.task import MaxExtractedExceeded

from admarkup.openrtb import OpenRTBRecognition, PreparedPayload, recognize_openrtb_response, rejected_input

def _report_section(recognition: OpenRTBRecognition) -> ResultSection:
    summary = ResultKeyValueSection("Markup extraction summary")
    
    source_format = recognition.source_format
    recognized_label = "OpenRTB response" if source_format == "OpenRTB" else "Prebid.js auction event"
    summary.set_item("Recognized Type", recognized_label)
    summary.set_item("Markup occurrences", len(recognition.markups))
    
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

        self._extract_payloads(request, recognition)

        summary_section = _report_section(recognition)
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

    def _extract_payloads(self, request: ServiceRequest, recognition: OpenRTBRecognition):
        """Return the extraction outcome after submitting each unique payload as an extracted child."""
        payloads = recognition.payloads
        max_file_size = forge.get_config().submission.max_file_size

        for payload in payloads:
            if max_file_size > 0 and payload.byte_length > max_file_size:
                request.log.warning(
                    "Skipping oversize payload %s (SHA-256 %s; %d bytes exceeds max_file_size %d)",
                    payload.filename,
                    payload.sha256,
                    payload.byte_length,
                    max_file_size,
                )
                continue

            try:
                self._submit_child(request, payload, recognition.source_format)
            except MaxExtractedExceeded:
                request.log.warning("Reached max_extracted limit; remaining AdMarkup payloads were not submitted")
                break

    def _submit_child(self, request: ServiceRequest, payload: PreparedPayload, source_format: str):
        """Return whether Assemblyline accepted the payload after writing it and adding it as an extracted file."""
        path = Path(self.working_directory) / f"{uuid4().hex}-{payload.filename}"
        path.write_bytes(payload.content)
        description = (
            f"Extracted {source_format} adm payload; SHA-256: {payload.sha256}"
        )
        accepted = request.add_extracted(str(path), payload.filename, description)
        if not accepted:
            request.log.warning(
                "Assemblyline rejected extracted AdMarkup payload %s (%s)",
                payload.filename,
                payload.sha256,
            )
