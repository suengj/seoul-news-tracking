"""Service version, read once from `pyproject.toml` (the single source of
truth — see docs/versioning.md). Never hardcode the version string elsewhere."""

from __future__ import annotations

import tomllib
from functools import lru_cache

from app.config import PROJECT_ROOT


@lru_cache(maxsize=1)
def get_version() -> str:
    pyproject = PROJECT_ROOT / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]
