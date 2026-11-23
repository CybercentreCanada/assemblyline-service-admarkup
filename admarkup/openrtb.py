"""Recognize supported OpenRTB bid responses and extract direct bid markup."""

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from assemblyline.common.net import is_valid_domain
from assemblyline.odm import URI

_URI_VALIDATOR = URI()


class _DuplicateJSONKeyError(ValueError):
    """Raised when a JSON object contains an ambiguous duplicate key."""


class _NonJSONConstantError(ValueError):
    """Raised when JSON contains a non-standard numeric constant."""


@dataclass(frozen=True)
class ExtractedMarkup:
    """Markup extracted from one structurally valid bid."""

    seat_index: int
    bid_index: int
    bid_id: str
    impression_id: str
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
    """Recognition result for a single OpenRTB response document."""

    recognized: bool
    markups: tuple[ExtractedMarkup, ...]
    payloads: tuple[PreparedPayload, ...]
    issues: tuple[RecognitionIssue, ...]
    network_indicators: tuple[NetworkIndicator, ...] = ()


def rejected_input(message: str) -> OpenRTBRecognition:
    """Return a rejected document with a root-level diagnostic."""
    return OpenRTBRecognition(
        recognized=False,
        markups=(),
        payloads=(),
        issues=(RecognitionIssue("$", message),),
    )


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
        try:
            if not _URI_VALIDATOR.validation_regex.fullmatch(value):
                raise ValueError("Invalid URI syntax")
            _URI_VALIDATOR.check(value)
            parsed = urlsplit(value)
            hostname = parsed.hostname
            # Accessing port validates its syntax and range.
            _ = parsed.port
        except ValueError:
            issues.append(RecognitionIssue(path, f"Bid {field} must be a valid URL."))
            continue
        indicators.append(NetworkIndicator(path, "network.static.uri", value))
        if hostname and is_valid_domain(hostname):
            indicators.append(NetworkIndicator(path, "network.static.domain", hostname))

    if "adomain" in bid:
        domains = bid["adomain"]
        path = f"{bid_path}.adomain"
        if not isinstance(domains, list):
            issues.append(RecognitionIssue(path, "Bid adomain must be an array of domains."))
        else:
            for index, domain in enumerate(domains):
                source_path = f"{path}[{index}]"
                if not isinstance(domain, str) or not is_valid_domain(domain):
                    issues.append(RecognitionIssue(source_path, "Bid adomain entry must be a valid domain."))
                    continue
                indicators.append(NetworkIndicator(source_path, "network.static.domain", domain.lower()))
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


def recognize_openrtb_response(document: str) -> OpenRTBRecognition:
    """Recognize the supported root response shape and extract direct bid markup.

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

    root_issues = []
    if not isinstance(response.get("id"), str):
        root_issues.append(RecognitionIssue("$.id", "Response id must be a string."))
    if not isinstance(response.get("seatbid"), list):
        root_issues.append(RecognitionIssue("$.seatbid", "Response seatbid must be an array."))
    if root_issues:
        return OpenRTBRecognition(recognized=False, markups=(), payloads=(), issues=tuple(root_issues))

    markups = []
    issues = []
    network_indicators = []
    payloads_by_content = {}
    for seat_index, seat in enumerate(response["seatbid"]):
        seat_path = f"$.seatbid[{seat_index}]"
        if not isinstance(seat, dict):
            issues.append(RecognitionIssue(seat_path, "SeatBid entry must be an object."))
            continue

        bids = seat.get("bid")
        if not isinstance(bids, list):
            issues.append(RecognitionIssue(f"{seat_path}.bid", "SeatBid bid must be an array."))
            continue

        for bid_index, bid in enumerate(bids):
            bid_path = f"{seat_path}.bid[{bid_index}]"
            if not isinstance(bid, dict):
                issues.append(RecognitionIssue(bid_path, "Bid entry must be an object."))
                continue

            bid_issues = []
            if not isinstance(bid.get("id"), str):
                bid_issues.append(RecognitionIssue(f"{bid_path}.id", "Bid id must be a string."))
            if not isinstance(bid.get("impid"), str):
                bid_issues.append(RecognitionIssue(f"{bid_path}.impid", "Bid impid must be a string."))
            price = bid.get("price")
            valid_price = (isinstance(price, int) and not isinstance(price, bool)) or (
                isinstance(price, float) and math.isfinite(price)
            )
            if not valid_price:
                bid_issues.append(RecognitionIssue(f"{bid_path}.price", "Bid price must be a finite JSON number."))

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
                    bid_id=bid["id"],
                    impression_id=bid["impid"],
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
    )
