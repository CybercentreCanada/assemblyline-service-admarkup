"""Recognize supported ad-auction documents and extract direct bid markup."""

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal
from urllib.parse import urlsplit

from assemblyline.common.net import is_valid_domain
from assemblyline.odm import URI

_URI_VALIDATOR = URI()


class SourceFormat(Enum):
    """Source format and its labels for child descriptions and result summaries."""

    OPENRTB_RESPONSE = ("OpenRTB", "OpenRTB response")
    OPENRTB_BID = ("OpenRTB bid", "OpenRTB bid")
    PREBID = ("Prebid.js", "Prebid.js auction event")

    def __init__(self, description: str, recognized_label: str):
        self.description = description
        self.recognized_label = recognized_label


class _DuplicateJSONKeyError(ValueError):
    """Raised when a JSON object contains an ambiguous duplicate key."""


class _NonJSONConstantError(ValueError):
    """Raised when JSON contains a non-standard numeric constant."""


@dataclass(frozen=True)
class ExtractedMarkup:
    """Markup extracted from one structurally valid bid."""

    seat_index: int | None
    bid_index: int
    bid_id: str | None
    impression_id: str | None
    markup: str
    source_path: str


@dataclass(frozen=True)
class PreparedPayload:
    """A unique UTF-8 payload and the paths of bids that supplied it."""

    sha256: str
    filename: str
    content: bytes
    byte_length: int
    source_paths: tuple[str, ...]
    submission_outcome: str


@dataclass(frozen=True)
class RecognitionIssue:
    """A structural problem found at a JSON document path."""

    path: str
    message: str


@dataclass(frozen=True)
class NetworkIndicator:
    """A network indicator and the bid field that supplied it."""

    source_path: str
    tag_type: Literal["network.static.uri", "network.static.domain"]
    value: str


@dataclass(frozen=True)
class OpenRTBRecognition:
    """Recognition result for a supported ad-auction document."""

    recognized: bool
    markups: tuple[ExtractedMarkup, ...]
    payloads: tuple[PreparedPayload, ...]
    issues: tuple[RecognitionIssue, ...]
    network_indicators: tuple[NetworkIndicator, ...] = ()
    source_format: SourceFormat = SourceFormat.OPENRTB_RESPONSE


def rejected_input(message: str) -> OpenRTBRecognition:
    """Return a rejected document with a root-level diagnostic."""
    return OpenRTBRecognition(
        recognized=False,
        markups=(),
        payloads=(),
        issues=(RecognitionIssue("$", message),),
    )


def _url_indicators(path: str, value: str) -> list[NetworkIndicator] | None:
    """Return URI and host-domain indicators for a valid URL, or None when the URL is invalid."""
    try:
        if not _URI_VALIDATOR.validation_regex.fullmatch(value):
            raise ValueError("Invalid URI syntax")
        _URI_VALIDATOR.check(value)
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing port validates its syntax and range.
        _ = parsed.port
    except ValueError:
        return None
    indicators = [NetworkIndicator(path, "network.static.uri", value)]
    if hostname and is_valid_domain(hostname):
        indicators.append(NetworkIndicator(path, "network.static.domain", hostname))
    return indicators


def _bid_network_indicators(
    bid: dict[str, object], bid_path: str, issues: list[RecognitionIssue]
) -> list[NetworkIndicator]:
    indicators = []
    for field in ("iurl", "burl", "nurl", "lurl"):
        if field not in bid:
            continue
        path = f"{bid_path}.{field}"
        value = bid[field]
        if not isinstance(value, str) or not value:
            issues.append(RecognitionIssue(path, f"Bid {field} must be a non-empty URL string."))
            continue
        url_indicators = _url_indicators(path, value)
        if url_indicators is None:
            issues.append(RecognitionIssue(path, f"Bid {field} must be a valid URL."))
            continue
        indicators.extend(url_indicators)

    if "adomain" in bid:
        domains = bid["adomain"]
        path = f"{bid_path}.adomain"
        if not isinstance(domains, list):
            issues.append(RecognitionIssue(path, "Bid adomain must be an array of domains."))
        else:
            for index, domain in enumerate(domains):
                source_path = f"{path}[{index}]"
                if isinstance(domain, str) and is_valid_domain(domain):
                    indicators.append(NetworkIndicator(source_path, "network.static.domain", domain.lower()))
                    continue
                # Some bidders send an advertiser URL instead of a bare domain.
                url_indicators = (
                    _url_indicators(source_path, domain)
                    if isinstance(domain, str) and domain.lower().startswith(("http://", "https://"))
                    else None
                )
                if url_indicators is None:
                    issues.append(RecognitionIssue(source_path, "Bid adomain entry must be a valid domain."))
                    continue
                indicators.extend(url_indicators)

    if "eventtrackers" in bid:
        trackers = bid["eventtrackers"]
        path = f"{bid_path}.eventtrackers"
        if not isinstance(trackers, list):
            issues.append(RecognitionIssue(path, "Bid eventtrackers must be an array."))
        else:
            for index, tracker in enumerate(trackers):
                source_path = f"{path}[{index}].url"
                url = tracker.get("url") if isinstance(tracker, dict) else None
                url_indicators = _url_indicators(source_path, url) if isinstance(url, str) and url else None
                if url_indicators is None:
                    issues.append(RecognitionIssue(source_path, "Bid event tracker url must be a valid URL."))
                    continue
                indicators.extend(url_indicators)
    return indicators


def _unique_object(pairs):
    """Build a JSON object while rejecting duplicate keys.

    Returns:
        The object represented by the key-value pairs.

    Raises:
        _DuplicateJSONKeyError: If an object contains the same key more than once.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError
        result[key] = value
    return result


def _reject_json_constant(_constant):
    """Reject JavaScript-style numeric constants that are not valid JSON.

    Raises:
        _NonJSONConstantError: Always, because the supplied token is not JSON.
    """
    raise _NonJSONConstantError


def _bid_required_issues(bid: dict, bid_path: str) -> list[RecognitionIssue]:
    issues = []
    if not isinstance(bid.get("id"), str):
        issues.append(RecognitionIssue(f"{bid_path}.id", "Bid id must be a string."))
    if not isinstance(bid.get("impid"), str):
        issues.append(RecognitionIssue(f"{bid_path}.impid", "Bid impid must be a string."))
    price = bid.get("price")
    valid_price = (isinstance(price, int) and not isinstance(price, bool)) or (
        isinstance(price, float) and math.isfinite(price)
    )
    if not valid_price:
        issues.append(RecognitionIssue(f"{bid_path}.price", "Bid price must be a finite JSON number."))
    return issues


def recognize_openrtb_response(document: str) -> OpenRTBRecognition:
    """Recognize OpenRTB responses, standalone bids, or supported Prebid.js events.

    Returns:
        The recognition status, per-bid markup, unique prepared payloads, and issues.
    """
    try:
        response = json.loads(
            document,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJSONKeyError:
        return rejected_input("Invalid JSON: duplicate object keys are not allowed.")
    except _NonJSONConstantError:
        return rejected_input("Invalid JSON: non-standard numeric constants are not allowed.")
    except json.JSONDecodeError as error:
        return rejected_input(f"Invalid JSON: {error.msg}")
    except ValueError:
        return rejected_input("Invalid JSON: a numeric value exceeds parser limits.")
    except RecursionError:
        return rejected_input("JSON nesting exceeds the parser's safe recursion depth; no bids were processed.")

    if not isinstance(response, dict):
        return rejected_input("The document root must be an object.")

    markups = []
    issues = []
    network_indicators = []
    payloads_by_content = {}

    event_payload = response.get("payload")
    prebid_bids = event_payload.get("bidsReceived") if isinstance(event_payload, dict) else None
    is_prebid = response.get("source") == "pbjs"
    if isinstance(response.get("id"), str) and isinstance(response.get("seatbid"), list):
        source_format = SourceFormat.OPENRTB_RESPONSE
    elif is_prebid and response.get("event") == "auctionEnd" and isinstance(prebid_bids, list):
        source_format = SourceFormat.PREBID
    elif is_prebid and response.get("event") == "bidWon" and isinstance(event_payload, dict):
        source_format = SourceFormat.PREBID
        # A bidWon event carries the winning bid object itself as its payload.
        prebid_bids = None
    elif "seatbid" not in response and not _bid_required_issues(response, "$"):
        source_format = SourceFormat.OPENRTB_BID
    else:
        root_issues = []
        if not isinstance(response.get("id"), str):
            root_issues.append(RecognitionIssue("$.id", "Response id must be a string."))
        if not isinstance(response.get("seatbid"), list):
            root_issues.append(RecognitionIssue("$.seatbid", "Response seatbid must be an array."))
        if root_issues:
            return OpenRTBRecognition(recognized=False, markups=(), payloads=(), issues=tuple(root_issues))

    def bid_groups():
        """Yield (seat_index, [(bid_index, bid_path, bid), ...]) for each group of bids."""
        if source_format == SourceFormat.PREBID:
            if isinstance(prebid_bids, list):
                yield None, [(i, f"$.payload.bidsReceived[{i}]", bid) for i, bid in enumerate(prebid_bids)]
            else:
                yield None, [(0, "$.payload", event_payload)]
        elif source_format == SourceFormat.OPENRTB_BID:
            yield None, [(0, "$", response)]
        else:
            for seat_index, seat in enumerate(response["seatbid"]):
                seat_path = f"$.seatbid[{seat_index}]"
                if not isinstance(seat, dict):
                    issues.append(RecognitionIssue(seat_path, "SeatBid entry must be an object."))
                    continue
                bids = seat.get("bid")
                if not isinstance(bids, list):
                    issues.append(RecognitionIssue(f"{seat_path}.bid", "SeatBid bid must be an array."))
                    continue
                yield seat_index, [(i, f"{seat_path}.bid[{i}]", bid) for i, bid in enumerate(bids)]

    for seat_index, bid_entries in bid_groups():
        for bid_index, bid_path, bid in bid_entries:
            if not isinstance(bid, dict):
                issues.append(RecognitionIssue(bid_path, "Bid entry must be an object."))
                continue

            if source_format != SourceFormat.PREBID:
                bid_issues = _bid_required_issues(bid, bid_path)
                if bid_issues:
                    issues.extend(bid_issues)
                    continue

            network_indicators.extend(_bid_network_indicators(bid, bid_path, issues))

            if "adm" not in bid:
                continue
            adm = bid["adm"]
            if not isinstance(adm, str):
                issues.append(RecognitionIssue(f"{bid_path}.adm", "Bid adm must be a string when present."))
                continue
            if not adm:
                issues.append(RecognitionIssue(f"{bid_path}.adm", "Bid adm must not be empty."))
                continue

            try:
                content = adm.encode("utf-8")
            except UnicodeEncodeError:
                issues.append(RecognitionIssue(f"{bid_path}.adm", "Bid adm cannot be encoded as UTF-8."))
                continue

            source_path = f"{bid_path}.adm"
            markups.append(
                ExtractedMarkup(
                    seat_index=seat_index,
                    bid_index=bid_index,
                    bid_id=bid.get("id") if isinstance(bid.get("id"), str) else None,
                    impression_id=bid.get("impid") if isinstance(bid.get("impid"), str) else None,
                    markup=adm,
                    source_path=source_path,
                )
            )

            payload = payloads_by_content.get(content)
            if payload is None:
                digest = hashlib.sha256(content).hexdigest()
                payload = {
                    "sha256": digest,
                    "filename": f"adm-{digest}.txt",
                    "content": content,
                    "byte_length": len(content),
                    "source_paths": [],
                }
                payloads_by_content[content] = payload
            payload["source_paths"].append(source_path)

    payloads = tuple(
        PreparedPayload(
            sha256=payload["sha256"],
            filename=payload["filename"],
            content=payload["content"],
            byte_length=payload["byte_length"],
            source_paths=tuple(payload["source_paths"]),
            submission_outcome="not_attempted",
        )
        for payload in payloads_by_content.values()
    )
    return OpenRTBRecognition(
        recognized=True,
        markups=tuple(markups),
        payloads=payloads,
        issues=tuple(issues),
        network_indicators=tuple(network_indicators),
        source_format=source_format,
    )
