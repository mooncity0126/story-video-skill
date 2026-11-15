"""Rebuild the Windows runtime from version locks, without loading model weights."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def python_path(component):
    return ROOT / "runtime" / component / ".venv" / "Scripts" / "python.exe"


def environment_commands(component):
    folder = ROOT / "runtime" / component
    python = python_path(component)
    commands = []
    if not python.is_file():
        commands.append([sys.executable, "-m", "venv", str(folder / ".venv")])
    commands.append([str(python), "-m", "pip", "install", "-r", str(folder / "requirements-lock.txt")])
    if component == "video-harness":
        commands.append([str(python), "-m", "pip", "install", "--no-deps", "-e", str(folder)])
    commands.append([str(python), "-m", "pip", "check"])
    return commands


def install_ffmpeg(archive, specification):
    if digest(archive) != specification["sha256"]:
        raise ValueError("FFmpeg archive SHA256 differs from runtime-manifest.json")
    output = ROOT / "runtime/video-harness/.tools"
    with zipfile.ZipFile(archive) as package:
        selected = {}
        for basename in ("ffmpeg.exe", "ffprobe.exe", "LICENSE", "README.txt"):
            matches = [name for name in package.namelist() if Path(name).name == basename]
            if len(matches) != 1:
                raise ValueError("Expected one archive member: " + basename)
            selected[basename] = matches[0]
        output.mkdir(parents=True, exist_ok=True)
        for basename, member in selected.items():
            with package.open(member) as source, (output / basename).open("wb") as target:
                shutil.copyfileobj(source, target)
    return output


def obtain_ffmpeg(archive, specification):
    if archive:
        return install_ffmpeg(archive.resolve(), specification)
    cache = ROOT / ".downloads"
    cache.mkdir(exist_ok=True)
    target = cache / specification["filename"]
    if not target.is_file() or digest(target) != specification["sha256"]:
        partial = target.with_suffix(target.suffix + ".part")
        request = urllib.request.Request(specification["url"], headers={"User-Agent": "story-video-skill-bootstrap"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        if digest(partial) != specification["sha256"]:
            raise ValueError("Downloaded FFmpeg archive has an unexpected SHA256")
        partial.replace(target)
    return install_ffmpeg(target, specification)


def write_local_settings(model_root):
    data = json.loads((ROOT / "config/skill-runtime.example.json").read_text(encoding="utf-8-sig"))
    data["runtime"] = {"harness_root": str(ROOT / "runtime/video-harness"), "python": str(python_path("video-harness"))}
    data["rewriter"]["local"].update(python=str(python_path("prompt-rewriter")),
                                    script=str(ROOT / "runtime/prompt-rewriter/rewrite_local.py"),
                                    model_root=str(model_root.resolve()))
    data["media"].update(ffmpeg=str(ROOT / "runtime/video-harness/.tools/ffmpeg.exe"),
                         ffprobe=str(ROOT / "runtime/video-harness/.tools/ffprobe.exe"))
    credentials = ROOT / "config/api-keys.local.json"
    if not credentials.exists():
        shutil.copyfile(ROOT / "config/api-keys.example.json", credentials)
    data["credentials_file"] = str(credentials)
    output = ROOT / "config/local-settings.json"
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=("all", "harness", "rewriter"), default="all")
    parser.add_argument("--model-root", type=Path, default=Path(os.environ.get("STORY_VIDEO_MODEL_ROOT", str(ROOT / "models/Prompt-Rewriter"))))
    parser.add_argument("--ffmpeg-archive", type=Path, help="Use a previously downloaded release ZIP")
    parser.add_argument("--skip-media-tools", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the setup plan without installing or downloading")
    args = parser.parse_args(argv)
    manifest = json.loads((ROOT / "runtime-manifest.json").read_text(encoding="utf-8"))
    selected = {"all": ["video-harness", "prompt-rewriter"], "harness": ["video-harness"], "rewriter": ["prompt-rewriter"]}[args.component]
    commands = [command for component in selected for command in environment_commands(component)]
    media = "video-harness" in selected and not args.skip_media_tools
    if args.dry_run:
        print(json.dumps({"components": selected, "commands": commands,
                          "ffmpeg": str(args.ffmpeg_archive) if args.ffmpeg_archive else manifest["ffmpeg"]["url"] if media else None,
                          "settings": str(ROOT / "config/local-settings.json"), "model_root": str(args.model_root.resolve()),
                          "loads_models": False}, ensure_ascii=False, indent=2))
        return 0
    if os.name != "nt" or sys.version_info[:2] != (3, 12):
        parser.error("This lock set targets Windows x64 and Python 3.12")
    for command in commands:
        print(subprocess.list2cmdline(command), flush=True)
        subprocess.run(command, cwd=ROOT, check=True)
    if media:
        obtain_ffmpeg(args.ffmpeg_archive, manifest["ffmpeg"])
    settings = write_local_settings(args.model_root)
    print("Runtime prepared. Settings: " + str(settings))
    print("Model weights remain at the configured external path; no inference was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
