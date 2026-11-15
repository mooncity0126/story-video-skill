"""Run the installed story-video skill with this project's environment/settings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    settings_path = ROOT / "config/local-settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    location_path = ROOT / "config/skill-location.json"
    location = json.loads(location_path.read_text(encoding="utf-8-sig")) if location_path.is_file() else {}
    skill = Path(os.environ.get("STORY_VIDEO_SKILL_DIR", location.get("skill_root", str(ROOT / ".agents/skills/produce-story-video"))))
    if not skill.is_absolute():
        skill = ROOT / skill
    tool = skill / "scripts/media_tools.py"
    python = Path(settings["runtime"]["python"])
    for path in (python, tool):
        if not path.is_file():
            raise FileNotFoundError(f"Required local file is missing: {path}")
    args = sys.argv[1:] or ["doctor"]
    if not any(arg in {"--config", "--run"} or arg.startswith(("--config=", "--run=")) for arg in args):
        args = ["--config", str(settings_path), *args]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.call([str(python), str(tool), *args], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
