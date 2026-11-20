"""Run the installed story-video skill with this project's environment/settings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def skill_directory(root: Path) -> Path:
    location_path = root / "config/skill-location.json"
    location = json.loads(location_path.read_text(encoding="utf-8-sig")) if location_path.is_file() else {}
    chosen = os.environ.get("STORY_VIDEO_SKILL_DIR") or location.get("skill_root") or ".agents/skills/produce-story-video"
    return (root / chosen).resolve()


def main() -> int:
    settings_path = ROOT / "config/local-settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    tool = skill_directory(ROOT) / "scripts/media_tools.py"
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
