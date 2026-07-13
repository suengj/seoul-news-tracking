from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.models import DisasterMessageRecord

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SEOUL_TZ = ZoneInfo("Asia/Seoul")


@pytest.fixture
def sample_response_payload() -> dict:
    return json.loads((FIXTURES_DIR / "sample_response.json").read_text(encoding="utf-8"))


@pytest.fixture
def make_record():
    def _make(
        source_id: str = "DS00099001",
        sender: str = "서울특별시 테스트구",
        sent_at: datetime | None = None,
        body: str = "테스트 본문입니다. [테스트구]",
        source_url: str = "https://safecity.seoul.go.kr/news/dist/dust/newsDistDustList.page",
        detected_at: datetime | None = None,
    ) -> DisasterMessageRecord:
        sent_at = sent_at or datetime(2026, 7, 13, 9, 0, 0, tzinfo=SEOUL_TZ)
        detected_at = detected_at or datetime(2026, 7, 13, 9, 1, 0, tzinfo=SEOUL_TZ)
        return DisasterMessageRecord(
            source_id=source_id,
            sender_or_region=sender,
            sent_at=sent_at,
            original_body=body,
            source_url=source_url,
            detected_at=detected_at,
            raw_payload={"disstrSmsSn": source_id},
        )

    return _make
