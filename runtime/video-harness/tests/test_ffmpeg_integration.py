"""Real local codecs/probing; no cloud requests or mocked metadata in these tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from video_harness.media import assemble, asset_record, run_process
from video_harness.models import Config, Storyboard
from video_harness.store import Store
from video_harness.util import HarnessError, read_json

from conftest import EXAMPLE


@pytest.fixture
def tools():
    local = Path(__file__).parents[1] / ".tools"
    ffmpeg = shutil.which("ffmpeg") or str(local / "ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or str(local / "ffprobe.exe")
    if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
        pytest.skip("FFmpeg/ffprobe not installed; install them for real codec integration checks")
    return ffmpeg, ffprobe


def make_source(path, cfg, duration, audio, color):
    args = [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=30:d={duration}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}"]
    args += ["-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac"]
    run_process(args + [str(path)], 60)


def setup_local_run(tmp_path, tools):
    cfg = Config(ffmpeg=tools[0], ffprobe=tools[1], export_width=180, export_height=320)
    board = read_json(EXAMPLE / "storyboard.json")
    board["shots"] = board["shots"][:2]
    for i, shot in enumerate(board["shots"]):
        shot.update(duration_ms=1000, timeline_in_ms=i * 1000)
    board["total_duration_ms"] = 2000
    board = Storyboard.model_validate(board)
    (tmp_path / "videos").mkdir()
    (tmp_path / "logs").mkdir()
    store = Store(tmp_path)
    store.sync_nodes({"video:U001": {}, "video:U002": {}})
    for i, audio in enumerate([True, False], 1):
        path = tmp_path / "videos" / f"U{i:03}.mp4"
        make_source(path, cfg, 1.5, audio, "red" if i == 1 else "blue")
        aid = store.attempt(f"video:U{i:03}", "test-hash", {})["attempt_id"]
        asset = asset_record(tmp_path, path, "video", cfg, task_id=f"local-fixture-{i}")
        asset["asset_id"] = aid
        store.update(aid, status="succeeded", output=asset)
    return cfg, board, store


def test_real_ffmpeg_preserves_aspect_audio_and_timeline(tmp_path, tools):
    cfg, board, store = setup_local_run(tmp_path, tools)
    try:
        result = assemble(tmp_path, board, store, cfg)
        meta = result["asset"]["technical"]
        assert meta["width"] == 180 and meta["height"] == 320
        assert abs(meta["duration_ms"] - 2000) <= 42
        assert meta["has_audio"] and meta["fps_num"] == 24
        assert [c["silence_added"] for c in result["clips"]] == [False, True]
        # Decode actual pixels: wide input stays wide, black padding occupies top of portrait output.
        from PIL import Image

        preview = tmp_path / "preview.png"
        run_process([cfg.ffmpeg, "-v", "error", "-y", "-i", str(tmp_path / "exports/final.mp4"), "-frames:v", "1", str(preview)], 60)
        with Image.open(preview) as im:
            assert max(im.getpixel((90, 10))[:3]) < 10
            assert im.getpixel((90, 160))[0] > 200
        # The source's sine wave must survive, not be replaced with silence.
        pcm = tmp_path / "first-second.pcm"
        run_process([cfg.ffmpeg, "-v", "error", "-y", "-i", str(tmp_path / "exports/final.mp4"), "-t", "0.8", "-vn", "-f", "s16le", str(pcm)], 60)
        assert any(b != 0 for b in pcm.read_bytes())
    finally:
        store.close()


def test_real_short_source_is_reported(tmp_path, tools):
    cfg, board, store = setup_local_run(tmp_path, tools)
    try:
        a = store.current("video:U001")
        path = tmp_path / a["output"]["path"]
        make_source(path, cfg, 0.3, True, "red")
        record = asset_record(tmp_path, path, "video", cfg)
        record["asset_id"] = a["attempt_id"]
        store.update(a["attempt_id"], output=record)
        with pytest.raises(HarnessError, match="源视频不足"):
            assemble(tmp_path, board, store, cfg)
        assert not (tmp_path / "exports/final.mp4").exists()
    finally:
        store.close()
