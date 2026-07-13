from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.parser import (
    SourceDataError,
    SourceSchemaError,
    parse_disstr_date,
    parse_response,
)

SEOUL_TZ = ZoneInfo("Asia/Seoul")
SOURCE_URL = "https://safecity.seoul.go.kr/news/dist/dust/newsDistDustList.page"


def test_full_text_extraction_from_api_fixture(sample_response_payload):
    detected_at = datetime(2026, 7, 13, 9, 30, 0, tzinfo=SEOUL_TZ)
    records = parse_response(sample_response_payload, SOURCE_URL, detected_at)

    assert len(records) == 2
    first = records[0]
    assert first.source_id == "DS00099001"
    assert first.sender_or_region == "서울특별시 테스트구 테스트동"
    # Full body preserved verbatim, including the ▲ symbol and embedded URL —
    # not summarized, not truncated, not rewritten.
    assert first.original_body == (
        "테스트 안내입니다. ▲긴급상황 발생 시 대피 요령을 따르세요 "
        "▲자세한 내용은 https://example.test/notice 참고 [테스트구]"
    )
    # \r\n preserved exactly for the second record.
    assert "\r\n" in records[1].original_body


def test_timestamp_parsed_as_asia_seoul(sample_response_payload):
    detected_at = datetime(2026, 7, 13, 9, 30, 0, tzinfo=SEOUL_TZ)
    records = parse_response(sample_response_payload, SOURCE_URL, detected_at)

    sent_at = records[0].sent_at
    assert sent_at.tzinfo is not None
    assert sent_at.utcoffset().total_seconds() == 9 * 3600
    assert sent_at.year == 2026 and sent_at.month == 7 and sent_at.day == 13
    assert sent_at.hour == 9 and sent_at.minute == 15


def test_parse_disstr_date_direct():
    dt = parse_disstr_date("2026/07/13 00:45:39")
    assert dt == datetime(2026, 7, 13, 0, 45, 39, tzinfo=SEOUL_TZ)


def test_parse_disstr_date_rejects_garbage():
    with pytest.raises(SourceDataError):
        parse_disstr_date("not-a-date")


def test_sender_district_parsed_verbatim():
    payload = {
        "sms": [
            {
                "disstrSmsSn": "DS1",
                "orgnlSn": "1",
                "disstrDate": "2026/07/13 09:00:00",
                "lctnNm": "서울특별시 강남구 ,서울특별시 강동구 ",
                "smsMsg": "본문",
            }
        ]
    }
    detected_at = datetime(2026, 7, 13, 9, 30, 0, tzinfo=SEOUL_TZ)
    records = parse_response(payload, SOURCE_URL, detected_at)
    assert records[0].sender_or_region == "서울특별시 강남구 ,서울특별시 강동구"


def test_rejects_response_missing_full_message_field():
    """The API's 'smsMsg' is the only documented full-text source; a record
    carrying only a hypothetical truncated preview field must be rejected,
    not silently accepted as if it were the complete body."""
    payload = {
        "sms": [
            {
                "disstrSmsSn": "DS2",
                "orgnlSn": "2",
                "disstrDate": "2026/07/13 09:00:00",
                "lctnNm": "서울특별시 테스트구",
                "smsMsgPreview": "짧은 미리보기...",
            }
        ]
    }
    detected_at = datetime(2026, 7, 13, 9, 30, 0, tzinfo=SEOUL_TZ)
    with pytest.raises(SourceDataError):
        parse_response(payload, SOURCE_URL, detected_at)


def test_rejects_empty_full_message():
    payload = {
        "sms": [
            {
                "disstrSmsSn": "DS3",
                "orgnlSn": "3",
                "disstrDate": "2026/07/13 09:00:00",
                "lctnNm": "서울특별시 테스트구",
                "smsMsg": "   ",
            }
        ]
    }
    detected_at = datetime(2026, 7, 13, 9, 30, 0, tzinfo=SEOUL_TZ)
    with pytest.raises(SourceDataError):
        parse_response(payload, SOURCE_URL, detected_at)


def test_rejects_missing_timestamp():
    payload = {
        "sms": [
            {
                "disstrSmsSn": "DS4",
                "orgnlSn": "4",
                "lctnNm": "서울특별시 테스트구",
                "smsMsg": "본문",
            }
        ]
    }
    detected_at = datetime(2026, 7, 13, 9, 30, 0, tzinfo=SEOUL_TZ)
    with pytest.raises(SourceDataError):
        parse_response(payload, SOURCE_URL, detected_at)


def test_rejects_missing_sender():
    payload = {
        "sms": [
            {
                "disstrSmsSn": "DS5",
                "orgnlSn": "5",
                "disstrDate": "2026/07/13 09:00:00",
                "smsMsg": "본문",
            }
        ]
    }
    detected_at = datetime(2026, 7, 13, 9, 30, 0, tzinfo=SEOUL_TZ)
    with pytest.raises(SourceDataError):
        parse_response(payload, SOURCE_URL, detected_at)


def test_rejects_non_dict_top_level():
    with pytest.raises(SourceSchemaError):
        parse_response([1, 2, 3], SOURCE_URL, datetime.now(tz=SEOUL_TZ))


def test_rejects_missing_sms_key():
    with pytest.raises(SourceSchemaError):
        parse_response({"foo": "bar"}, SOURCE_URL, datetime.now(tz=SEOUL_TZ))


def test_rejects_sms_not_a_list():
    with pytest.raises(SourceSchemaError):
        parse_response({"sms": "oops"}, SOURCE_URL, datetime.now(tz=SEOUL_TZ))
