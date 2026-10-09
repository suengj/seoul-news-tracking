from __future__ import annotations

import re
import subprocess
from pathlib import Path


def test_documented_fresh_install_command_renders_expected_unit_paths(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    documentation = (repo_root / "docs/systemd_deployment.md").read_text()
    match = re.search(
        r"^sed -e .* > /tmp/seoulnews-runlocal\.service$",
        documentation,
        flags=re.MULTILINE,
    )
    assert match is not None

    rendered_path = tmp_path / "seoulnews-runlocal.service"
    command = match.group().replace(
        "/tmp/seoulnews-runlocal.service", str(rendered_path)
    )
    subprocess.run(
        ["bash", "-eu", "-c", command],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    rendered = rendered_path.read_text()
    assert "/path/to/" not in rendered
    assert "WorkingDirectory=/opt/seoulnews/app\n" in rendered
    assert "EnvironmentFile=/etc/seoulnews/run_local.env\n" in rendered
    assert (
        "ExecStart=/opt/seoulnews/app/.venv/bin/python -u "
        "-m app.commands.run_local\n"
    ) in rendered
