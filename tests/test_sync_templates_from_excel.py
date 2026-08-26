from __future__ import annotations

import pytest

from app.commands.sync_templates_from_excel import (
    build_from_workbook,
    build_yaml_document,
    dump_yaml,
)
from app.config import PROJECT_ROOT
from app.template_ids import (
    EXPECTED_AUTOMATION_COUNT,
    EXPECTED_REFERENCE_COUNT,
    EXPECTED_WORKBOOK_TOTAL,
)
from app.template_renderer import clear_template_cache, load_templates, resolve_template_id

WORKBOOK = PROJECT_ROOT / "templates" / "서울시_재난특보_X템플릿.xlsx"


def test_workbook_sync_counts_and_loader_accept_generated_yaml(tmp_path):
    if not WORKBOOK.is_file():
        pytest.skip(
            "Local-only workbook missing (templates/서울시_재난특보_X템플릿.xlsx is gitignored)"
        )
    sync = build_from_workbook(WORKBOOK)
    assert sync.ok, [i.format() for i in sync.issues]
    assert sync.total == EXPECTED_WORKBOOK_TOTAL
    assert sync.automation_count == EXPECTED_AUTOMATION_COUNT
    assert sync.reference_count == EXPECTED_REFERENCE_COUNT

    yaml_text = dump_yaml(build_yaml_document(sync))
    path = tmp_path / "message_templates.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    clear_template_cache()
    templates = load_templates(path)
    assert templates["HW-01"].automation is True
    assert templates["REF-01"].enabled is False
    assert templates["ORIGINAL_ONLY"].system is True
    assert templates["HEAVY_RAIN_MULTI_LEVEL_ISSUED"].legacy is True
    assert resolve_template_id("HEAVY_RAIN_CLEARED", path) == "HW-05"
    clear_template_cache()


def test_resolve_template_id_pass_through():
    assert resolve_template_id("HW-05") == "HW-05"
    assert resolve_template_id("FL-01") == "FL-01"
