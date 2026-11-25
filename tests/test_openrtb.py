import hashlib
import json
from pathlib import Path

import pytest

from admarkup.openrtb import recognize_openrtb_response


def bid(**overrides):
    """Create a small synthetic valid OpenRTB bid.

    Returns:
        A bid dictionary.
    """
    value = {"id": "bid-1", "impid": "imp-1", "price": 1.25, "adm": "<p>synthetic</p>"}
    value.update(overrides)
    return value


def bid_without_adm(**overrides):
    """Create a small synthetic valid bid without inline markup.

    Returns:
        A bid dictionary without an adm field.
    """
    value = bid()
    value.pop("adm")
    value.update(overrides)
    return value


def response(*seats, **overrides):
    """Create a small synthetic root response.

    Returns:
        The encoded JSON document.
    """
    value = {"id": "response-1", "seatbid": list(seats)}
    value.update(overrides)
    return json.dumps(value)


@pytest.mark.parametrize(
    "markup",
    [
        "<VAST version='4.0'></VAST>",
        "<div>synthetic HTML</div>",
        "# Synthetic Markdown",
        "arbitrary synthetic payload",
    ],
)
def test_extracts_string_adm_from_valid_bid(markup):
    result = recognize_openrtb_response(response({"bid": [bid(adm=markup)]}))

    assert result.recognized
    assert [item.markup for item in result.markups] == [markup]
    assert result.issues == ()


@pytest.mark.parametrize(
    "markup",
    [
        'quoted "value" and backslash \\ path',
        "Unicode: café ☃",
        "line one\r\nline two\n",
        "# Markdown\n\ncontent",
        "<div>synthetic HTML</div>",
        "<VAST version='4.0'></VAST>",
        "${AUCTION_PRICE} ${AUCTION_ID}",
        '{"adm":"serialized JSON is still text"}',
    ],
)
def test_prepares_exact_utf8_payload_and_digest(markup):
    result = recognize_openrtb_response(response({"bid": [bid(adm=markup)]}))
    expected_content = markup.encode("utf-8")
    expected_digest = hashlib.sha256(expected_content).hexdigest()

    assert len(result.payloads) == 1
    payload = result.payloads[0]
    assert payload.content == expected_content
    assert payload.sha256 == expected_digest
    assert payload.filename == f"adm-{expected_digest}.txt"
    assert payload.byte_length == len(expected_content)
    assert payload.source_paths == ("$.seatbid[0].bid[0].adm",)
    assert payload.submission_outcome == "not_attempted"


def test_deduplicates_payloads_and_tracks_all_source_paths_in_encounter_order():
    result = recognize_openrtb_response(
        response(
            {"bid": [bid(id="../unsafe", adm="same"), bid(id="second", adm="same")]},
            {"bid": [bid(id="third", adm="different"), bid(id="fourth", adm="same")]},
        )
    )

    assert [payload.content for payload in result.payloads] == [b"same", b"different"]
    assert result.payloads[0].source_paths == (
        "$.seatbid[0].bid[0].adm",
        "$.seatbid[0].bid[1].adm",
        "$.seatbid[1].bid[1].adm",
    )
    assert result.payloads[1].source_paths == ("$.seatbid[1].bid[0].adm",)
    assert all("/" not in payload.filename and ".." not in payload.filename for payload in result.payloads)


def test_whitespace_and_newline_differences_remain_distinct_payloads():
    result = recognize_openrtb_response(
        response({"bid": [bid(adm="payload"), bid(adm=" payload"), bid(adm="payload\n")]})
    )

    assert [payload.content for payload in result.payloads] == [b"payload", b" payload", b"payload\n"]


@pytest.mark.parametrize("adm", [None, [], {}, True, 7, ""])
def test_empty_and_non_string_adm_values_are_diagnosed(adm):
    result = recognize_openrtb_response(response({"bid": [bid(adm=adm), bid(adm="valid sibling")]}))

    assert [item.markup for item in result.markups] == ["valid sibling"]
    assert [payload.content for payload in result.payloads] == [b"valid sibling"]
    issue = next(issue for issue in result.issues if issue.path == "$.seatbid[0].bid[0].adm")
    assert issue.message in {"Bid adm must not be empty.", "Bid adm must be a string when present."}
    assert repr(adm) not in issue.message


def test_whitespace_only_adm_is_valid_data():
    result = recognize_openrtb_response(response({"bid": [bid(adm=" \t\n")]}))

    assert result.payloads[0].content == b" \t\n"
    assert result.issues == ()


def test_unpaired_surrogate_is_diagnosed_without_affecting_valid_sibling():
    result = recognize_openrtb_response(response({"bid": [bid(adm="\ud800"), bid(adm="valid")]}))

    assert [payload.content for payload in result.payloads] == [b"valid"]
    issue = next(issue for issue in result.issues if issue.path == "$.seatbid[0].bid[0].adm")
    assert issue.message == "Bid adm cannot be encoded as UTF-8."
    assert "\ud800" not in issue.message


@pytest.mark.parametrize(
    "document",
    [
        '{"id":"response-1","id":"response-2","seatbid":[]}',
        '{"id":"response-1","seatbid":[{"bid":[{"id":"bid-1","impid":"imp-1","price":1,"adm":"first","adm":"second"}]}]}',
    ],
)
def test_duplicate_json_object_keys_reject_the_document(document):
    result = recognize_openrtb_response(document)

    assert not result.recognized
    assert result.markups == ()
    assert result.payloads == ()
    assert len(result.issues) == 1
    assert result.issues[0].path == "$"
    assert result.issues[0].message == "Invalid JSON: duplicate object keys are not allowed."


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_json_numeric_constants_reject_the_document(constant):
    document = (
        '{"id":"response-1","seatbid":[{"bid":[{"id":"bid-1","impid":"imp-1",'
        f'"price":1,"adm":"valid","ext":{constant}' + "}]}]}"
    )

    result = recognize_openrtb_response(document)

    assert not result.recognized
    assert result.markups == ()
    assert result.payloads == ()
    assert result.issues[0].path == "$"
    assert result.issues[0].message == "Invalid JSON: non-standard numeric constants are not allowed."


def test_extracts_valid_bids_across_seats_and_ignores_optional_fields():
    result = recognize_openrtb_response(
        response(
            {"seat": "one", "bid": [bid(id="bid-1"), bid(id="bid-2", adm="two")]},
            {"seat": "two", "bid": [bid(id="bid-3", adm="three")]},
            ext={"adm": "not a bid payload"},
            cur=["USD"],
        )
    )

    assert [item.markup for item in result.markups] == ["<p>synthetic</p>", "two", "three"]
    assert [(item.seat_index, item.bid_index) for item in result.markups] == [(0, 0), (0, 1), (1, 0)]
    assert result.issues == ()


@pytest.mark.parametrize(
    ("document", "recognized"),
    [
        (json.dumps({"adm": "not a response"}), False),
        (json.dumps([{"id": "response-1", "seatbid": []}]), False),
        (json.dumps({"log": {"id": "response-1", "seatbid": [{"bid": [bid()]}]}}), False),
        (json.dumps({"id": "request-1", "imp": [{"id": "imp-1", "banner": {"adm": "request data"}}]}), False),
        (response({"bid": [bid_without_adm(ext={"adm": "decoy"})]}), True),
        (response({"bid": [bid_without_adm(request={"adm": "decoy"})]}), True),
        (response({"bid": [bid_without_adm(request=json.dumps({"adm": "nested string"}))]}), True),
        (response({"bid": [bid_without_adm()]}, ext={"adm": "decoy"}), True),
    ],
)
def test_rejects_non_response_and_nested_adm_sources(document, recognized):
    result = recognize_openrtb_response(document)

    assert result.recognized is recognized
    assert result.markups == ()


@pytest.mark.parametrize(
    ("overrides", "issue_path"),
    [
        ({"id": None}, "$.id"),
        ({"id": 2}, "$.id"),
        ({"seatbid": None}, "$.seatbid"),
        ({"seatbid": {}}, "$.seatbid"),
    ],
)
def test_invalid_response_envelope_cannot_yield_payloads(overrides, issue_path):
    result = recognize_openrtb_response(response({"bid": [bid()]}, **overrides))

    assert not result.recognized
    assert result.markups == ()
    assert issue_path in {issue.path for issue in result.issues}


@pytest.mark.parametrize(
    ("document", "issue_path"),
    [
        (json.dumps({"seatbid": []}), "$.id"),
        (json.dumps({"id": "response-1"}), "$.seatbid"),
    ],
)
def test_missing_response_fields_cannot_yield_payloads(document, issue_path):
    result = recognize_openrtb_response(document)

    assert not result.recognized
    assert result.markups == ()
    assert issue_path in {issue.path for issue in result.issues}


@pytest.mark.parametrize(
    ("overrides", "issue_path"),
    [
        ({"id": None}, "$.seatbid[0].bid[0].id"),
        ({"id": 1}, "$.seatbid[0].bid[0].id"),
        ({"impid": None}, "$.seatbid[0].bid[0].impid"),
        ({"impid": 1}, "$.seatbid[0].bid[0].impid"),
        ({"price": "1.25"}, "$.seatbid[0].bid[0].price"),
        ({"price": True}, "$.seatbid[0].bid[0].price"),
        ({"adm": 7}, "$.seatbid[0].bid[0].adm"),
    ],
)
def test_invalid_bid_fields_are_reported_without_payload(overrides, issue_path):
    result = recognize_openrtb_response(response({"bid": [bid(**overrides)]}))

    assert result.recognized
    assert result.markups == ()
    assert issue_path in {issue.path for issue in result.issues}


@pytest.mark.parametrize(
    ("field", "issue_path"),
    [
        ("id", "$.seatbid[0].bid[0].id"),
        ("impid", "$.seatbid[0].bid[0].impid"),
        ("price", "$.seatbid[0].bid[0].price"),
    ],
)
def test_missing_bid_fields_are_reported_without_payload(field, issue_path):
    invalid_bid = bid()
    invalid_bid.pop(field)
    result = recognize_openrtb_response(response({"bid": [invalid_bid]}))

    assert result.recognized
    assert result.markups == ()
    assert issue_path in {issue.path for issue in result.issues}


def test_json_number_overflow_cannot_yield_payload():
    document = (
        '{"id":"response-1","seatbid":[{"bid":[{"id":"bid-1","impid":"imp-1","price":1e999,"adm":"not extracted"}]}]}'
    )

    result = recognize_openrtb_response(document)

    assert result.recognized
    assert result.markups == ()
    assert "$.seatbid[0].bid[0].price" in {issue.path for issue in result.issues}


def test_valid_sibling_survives_malformed_bid():
    result = recognize_openrtb_response(response({"bid": [bid(adm="valid"), bid(id=None, adm="invalid sibling")]}))

    assert result.recognized
    assert [item.markup for item in result.markups] == ["valid"]
    assert "$.seatbid[0].bid[1].id" in {issue.path for issue in result.issues}


@pytest.mark.parametrize(
    "document",
    [
        response(),
        response({"bid": []}),
        response({"bid": [bid_without_adm()]}),
    ],
)
def test_no_bid_or_missing_adm_is_a_normal_empty_extraction(document):
    result = recognize_openrtb_response(document)

    assert result.recognized
    assert result.markups == ()
    assert result.issues == ()


def test_malformed_json_is_reported():
    result = recognize_openrtb_response("{")

    assert not result.recognized
    assert result.markups == ()
    assert result.issues[0].path == "$"


@pytest.mark.parametrize(
    "document",
    [
        {"source": "other", "event": "auctionEnd", "payload": {"bidsReceived": [bid()]}},
        {"source": "pbjs", "event": "bidResponse", "payload": {"bidsReceived": [bid()]}},
        {"source": "pbjs", "event": "auctionEnd", "payload": {"bidsReceived": {}}},
        {"source": "pbjs", "event": "auctionEnd", "payload": None},
        {"source": "pbjs", "event": "bidWon", "payload": [bid()]},
    ],
)
def test_rejects_nonmatching_prebid_envelopes(document):
    result = recognize_openrtb_response(json.dumps(document))
    assert not result.recognized
    assert result.payloads == ()


def test_prebid_handles_malformed_bids_and_empty_markup_without_losing_valid_siblings():
    document = {
        "source": "pbjs",
        "event": "auctionEnd",
        "payload": {"bidsReceived": [None, {"adm": ""}, {"adm": "\ud800"}, {"adm": "valid"}]},
    }

    result = recognize_openrtb_response(json.dumps(document))

    assert result.recognized
    assert result.source_format == "Prebid.js"
    assert [payload.content for payload in result.payloads] == [b"valid"]
    assert result.payloads[0].source_paths == ("$.payload.bidsReceived[3].adm",)
    assert [issue.path for issue in result.issues] == [
        "$.payload.bidsReceived[0]",
        "$.payload.bidsReceived[1].adm",
        "$.payload.bidsReceived[2].adm",
    ]


def test_openrtb_response_takes_precedence_over_embedded_prebid_envelope():
    document = response(
        {"bid": [bid(adm="openrtb")]},
        source="pbjs",
        event="auctionEnd",
        payload={"bidsReceived": [{"adm": "prebid"}]},
    )
    result = recognize_openrtb_response(document)

    assert result.source_format == "OpenRTB"
    assert [payload.content for payload in result.payloads] == [b"openrtb"]


def test_prebid_bid_won_extracts_payload_markup_and_url_indicators():
    fixture = Path(__file__).parent / "fixtures" / "prebid_synthetic_bid_won.json"

    result = recognize_openrtb_response(fixture.read_text(encoding="utf-8"))

    assert result.recognized
    assert result.source_format == "Prebid.js"
    assert [markup.source_path for markup in result.markups] == ["$.payload.adm"]
    assert result.payloads[0].content == b'<div class="campaign">Synthetic winning display</div>'
    assert {(i.source_path, i.tag_type, i.value) for i in result.network_indicators} == {
        ("$.payload.adomain[0]", "network.static.uri", "https://www.advertiser.example.com/"),
        ("$.payload.adomain[0]", "network.static.domain", "www.advertiser.example.com"),
        ("$.payload.adomain[1]", "network.static.domain", "brand.example.com"),
        ("$.payload.iurl", "network.static.uri", "https://ads.example.com/synthetic-win.png"),
        ("$.payload.iurl", "network.static.domain", "ads.example.com"),
        (
            "$.payload.eventtrackers[0].url",
            "network.static.uri",
            "https://tracker.example.com/event?t=win&b=synthetic-bid-1",
        ),
        ("$.payload.eventtrackers[0].url", "network.static.domain", "tracker.example.com"),
    }
    assert {issue.path for issue in result.issues} == {
        "$.payload.adomain[2]",
        "$.payload.eventtrackers[1].url",
    }
