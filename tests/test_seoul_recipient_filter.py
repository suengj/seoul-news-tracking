from __future__ import annotations

import pytest

from app.models import SEOUL_DISTRICTS, is_seoul_recipient


def test_bare_seoul_prefix_is_seoul():
    assert is_seoul_recipient("서울특별시") is True


@pytest.mark.parametrize("district", SEOUL_DISTRICTS)
def test_each_seoul_district_with_prefix_is_seoul(district):
    assert is_seoul_recipient(f"서울특별시 {district}") is True


def test_multi_region_containing_seoul_is_seoul():
    assert is_seoul_recipient("경기도 광명시,경기도 시흥시,서울특별시 구로구") is True


def test_multi_region_without_seoul_is_not_seoul():
    assert is_seoul_recipient("경기도 광명시,경기도 시흥시,인천광역시 부평구") is False


def test_body_mentioning_seoul_but_region_excluding_it_is_not_seoul():
    # is_seoul_recipient only ever receives the region field, never the body —
    # this documents that a region value with no Seoul token is correctly
    # excluded even if it superficially resembles other Seoul-adjacent text.
    assert is_seoul_recipient("경기도 과천시") is False


def test_missing_region_is_schema_invalid_not_seoul():
    assert is_seoul_recipient(None) is False


def test_empty_string_region_is_not_seoul():
    assert is_seoul_recipient("") is False


def test_malformed_region_whitespace_only_is_not_seoul():
    assert is_seoul_recipient("   ") is False


def test_list_region_type_is_supported():
    assert is_seoul_recipient(["경기도", "서울특별시 구로구"]) is True


def test_list_region_without_seoul_is_not_seoul():
    assert is_seoul_recipient(["경기도", "인천광역시"]) is False


def test_trailing_space_in_region_token_is_handled():
    # Confirmed live format: MOIS/SafeKorea region tokens carry a trailing
    # space (e.g. "서울특별시 노원구 ").
    assert is_seoul_recipient("서울특별시 노원구 ") is True


def test_seoul_substring_without_official_token_is_not_seoul():
    # "서울" alone (not the full "서울특별시" administrative token) must not match.
    assert is_seoul_recipient("서울 근처") is False


def test_generic_city_named_seoul_something_is_not_matched_loosely():
    assert is_seoul_recipient("서울특별시청 인근") is False
