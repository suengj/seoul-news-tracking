from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.future import approval_workflow, message_formatter, scheduler, trigger_rules, x_publisher

APP_DIR = Path(__file__).parent.parent / "app"
RUNTIME_MODULES = [
    APP_DIR / "collector.py",
    APP_DIR / "parser.py",
    APP_DIR / "database.py",
    APP_DIR / "telegram_sender.py",
    APP_DIR / "config.py",
    APP_DIR / "models.py",
    *sorted((APP_DIR / "commands").glob("*.py")),
]


def test_message_formatter_not_implemented(make_record):
    with pytest.raises(NotImplementedError):
        message_formatter.format_for_future_channel(make_record())


def test_trigger_rules_not_implemented(make_record):
    with pytest.raises(NotImplementedError):
        trigger_rules.evaluate_triggers(make_record())


def test_approval_workflow_not_implemented(make_record):
    with pytest.raises(NotImplementedError):
        approval_workflow.request_approval(make_record(), ())


def test_x_publisher_not_implemented(make_record):
    with pytest.raises(NotImplementedError):
        x_publisher.publish_to_x(make_record(), "formatted text")


def test_scheduler_not_implemented():
    with pytest.raises(NotImplementedError):
        scheduler.run_scheduled_polling()


def test_future_modules_not_imported_by_part1_runtime():
    """Static check: no Part 1 runtime module imports app.future.*.

    Part 1 must not call any future placeholder at runtime; this guards
    against an accidental import creeping in later.
    """
    for module_path in RUNTIME_MODULES:
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.future"):
                pytest.fail(f"{module_path} imports {node.module}, but Part 1 runtime must not use app.future")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.future"):
                        pytest.fail(f"{module_path} imports {alias.name}, but Part 1 runtime must not use app.future")
