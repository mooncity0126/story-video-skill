from __future__ import annotations

import base64
import shutil
from pathlib import Path

import httpx
import pytest
from PIL import Image

from video_harness.client import X2VClient
from video_harness.continuity import review_objects
from video_harness.engine import Runner, build_graph, create_run
from video_harness.media import extract_timeline_tail, run_process
from video_harness.models import Config, ModelChoice, Storyboard, load_plan
from video_harness.util import HarnessError, read_json, write_json

from conftest import EXAMPLE

ENHANCED = EXAMPLE.parent / "two-cups-sequential"


def local_tools():
    local = Path(__file__).parents[1] / ".tools"
    tools = [shutil.which(n) or str(local / (n + ".exe")) for n in ["ffmpeg", "ffprobe"]]
    if not all(Path(t).is_file() for t in tools):
        pytest.skip("Real FFmpeg required for serial continuity checks")
    return tools


def test_new_defaults_and_adjacent_input():
    cfg = Config()
    assert cfg.video.model_cls == "MiniMax-H3" and cfg.video.parameters["enhance_prompt"] is False
    assert cfg.image.model_cls == cfg.edit.model_cls == "gpt-image-2"
    chars, board = load_plan(ENHANCED / "characters.json", ENHANCED / "storyboard.json")
    review = review_objects(board)
    assert len(review["pairs"]) == 19
    assert len(board.props) == 11
    graph = build_graph(chars, board, cfg)
    assert graph["frame:U002"]["deps"][0] == "tail:U001"
    assert graph["tail:U001"]["deps"] == ["video:U001"]
    assert len(graph["frame:U013"]["deps"]) == 3
    assert graph["cast:U013"]["deps"] == ["character:CHAR_001:base", "character:CHAR_002:base"]
    assert "PROP_CUP_A" in graph["frame:U008"]["prompt"]
    assert "PROP_CUP_B" in graph["video:U008"]["prompt"]
    assert "金色蛋糕" in graph["video:U008"]["prompt"]


def test_serial_missing_ffmpeg_blocks_before_any_paid_task(runner, cloud):
    runner.config.sequential_shots = True
    runner.config.ffmpeg = "missing-ffmpeg-for-serial-test"
    with pytest.raises(HarnessError, match="FFmpeg"):
        runner.execute(explicit=True)
    assert not cloud.submissions


@pytest.mark.parametrize("change", ["missing", "state_jump", "new_unknown", "no_source", "no_basis"])
def test_object_errors_rejected_before_any_generation(change):
    b = read_json(ENHANCED / "storyboard.json")
    if change == "missing":
        b["shots"][1]["objects"].pop(0)
    elif change == "state_jump":
        b["shots"][1]["objects"][0]["start_description"] = "A different quantity or container"
    elif change == "new_unknown":
        b["shots"][1]["objects"][0]["prop_id"] = "PROP_UNREGISTERED"
    elif change == "no_source":
        b["shots"][0]["objects"][0]["entry_reason"] = ""
    else:
        b["props"][1]["basis"] = ""
    with pytest.raises(HarnessError):
        review_objects(Storyboard.model_validate(b))


def test_left_object_can_be_omitted_and_check_is_only_adjacent():
    b = read_json(ENHANCED / "storyboard.json")
    pid = b["shots"][0]["objects"][0]["prop_id"]
    first = b["shots"][0]["objects"][0]
    first.update(end_visible=False, exit_reason="Camera reframes; this original object leaves the image")
    second = b["shots"][1]
    second["objects"] = [o for o in second["objects"] if o["prop_id"] != pid]
    second["focus_prop_ids"].remove(pid)
    # It can return in the third image with an explicit camera-reveal source;
    # no comparison to the first shot is imposed on its returning state.
    third = next(o for o in b["shots"][2]["objects"] if o["prop_id"] == pid)
    third["entry_reason"] = "Camera reveals the existing object again"
    assert review_objects(Storyboard.model_validate(b))["status"] == "passed"


def test_tail_is_last_retained_frame_not_full_generated_clip(tmp_path):
    ffmpeg, ffprobe = local_tools()
    cfg = Config(ffmpeg=ffmpeg, ffprobe=ffprobe)
    source = tmp_path / "red_then_blue.mp4"
    run_process(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=108x192:r=24:d=3",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=108x192:r=24:d=2",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-c:v",
            "libx264",
            str(source),
        ],
        60,
    )
    retained, full = tmp_path / "retained.png", tmp_path / "full.png"
    extract_timeline_tail(source, retained, cfg, 3000)
    extract_timeline_tail(source, full, cfg, 5000)
    with Image.open(retained) as im:
        assert im.getpixel((50, 50))[0] > 200 and im.getpixel((50, 50))[2] < 20
    with Image.open(full) as im:
        assert im.getpixel((50, 50))[2] > 200


def test_serial_execution_actual_tail_bytes_resume_and_invalidation(tmp_path, cloud):
    ffmpeg, ffprobe = local_tools()
    cfg = Config(ffmpeg=ffmpeg, ffprobe=ffprobe, execute_enabled=True, budget_credits=1000, concurrency=4, request_interval_seconds=0, poll_interval_seconds=0.001, wait_timeout_seconds=1, retries=0)
    b = read_json(ENHANCED / "storyboard.json")
    b["shots"] = b["shots"][:3]
    b["total_duration_ms"] = 15000
    for shot in b["shots"][1:]:
        shot["visible_characters"].append({"character_id": "CHAR_002", "variant_id": None})
    boardfile = tmp_path / "input.json"
    write_json(boardfile, b)
    source = tmp_path / "actual-video.mp4"
    run_process([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=yellow:s=108x192:r=24:d=5", "-c:v", "libx264", str(source)], 60)
    payload = source.read_bytes()
    original_handle = cloud.handle
    completed_video_nodes = []
    runner = None

    def handle(request):
        if request.url.host == "media.test":
            task = cloud.tasks[request.url.path.strip("/")]
            if task["task"] == "i2av":
                completed_video_nodes.append(task["prompt"])
                return httpx.Response(200, content=payload)
        if request.url.path.endswith("/task/submit"):
            import json

            body = json.loads(request.content)
            if body["task"] == "i2i":
                prior = "video:U001" if "U001 实际尾帧" in body["prompt"] else "video:U002" if "U002 实际尾帧" in body["prompt"] else None
                if prior:
                    assert runner.store.current(prior)["status"] == "succeeded"
        return original_handle(request)

    client = X2VClient(cfg, key="test-only-credential", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    root = create_run(ENHANCED / "script.md", ENHANCED / "characters.json", boardfile, cfg, tmp_path / "runs")
    runner = Runner(root, client)
    try:
        result = runner.execute(explicit=True)
        assert not result["issues"]
        assert len(completed_video_nodes) == 3
        frame = runner.store.current("frame:U002")
        submitted = cloud.tasks[frame["task_id"]]
        tail = runner.store.current("tail:U001")
        assert base64.b64decode(submitted["input_image"][0]["data"]) == (root / tail["output"]["path"]).read_bytes()
        assert tail["output"]["parent_asset_ids"] == [runner.store.current("video:U001")["attempt_id"]]
        assert len(submitted["input_image"]) == 3
        cast = runner.store.current("cast:U002")
        assert len(cast["output"]["parent_asset_ids"]) == 2
        video = runner.store.current("video:U002")
        assert base64.b64decode(cloud.tasks[video["task_id"]]["input_image"]["data"]) == (root / frame["output"]["path"]).read_bytes()
        assert cloud.tasks[video["task_id"]]["model_cls"] == "MiniMax-H3"
        count = len(cloud.submissions)
        assert not runner.execute(explicit=True)["issues"]
        assert len(cloud.submissions) == count
        affected = runner.invalidate(["frame:U002"])
        assert {"frame:U002", "video:U002", "tail:U002", "frame:U003", "video:U003"} <= set(affected)
        assert "video:U001" not in affected
    finally:
        runner.close()
        client.close()


def test_primary_image_known_failure_falls_back_and_resumes(runner, cloud, monkeypatch):
    runner.config.concurrency = 1
    runner.config.image_fallback = ModelChoice(task="t2i", model_cls="Qwen-Image-2512", parameters={"resolution_level": "720p"})
    query = runner.client.query
    failed = []

    def fail_first(task_id):
        result = query(task_id)
        if not failed:
            failed.append(task_id)
        if task_id == failed[0]:
            result["status"] = "FAILED"
        return result

    monkeypatch.setattr(runner.client, "query", fail_first)
    assert not runner.execute(explicit=True)["issues"]
    assert len(cloud.submissions) == 45
    a = runner.store.current("character:CHAR_001:base")
    assert a["request"]["fallback"] and a["parent_id"]
    assert a["request"]["api_parameters"]["model_cls"] == "Qwen-Image-2512"
    before = len(cloud.submissions)
    assert not runner.execute(explicit=True)["issues"]
    assert len(cloud.submissions) == before
    assert runner.store.spent() == 90  # Failed primary reservation remains accounted for.


def test_primary_image_unknown_never_falls_back(runner, cloud):
    runner.config.concurrency = 1
    runner.config.image_fallback = ModelChoice(task="t2i", model_cls="Qwen-Image-2512", parameters={"resolution_level": "720p"})
    cloud.unknown_once = True
    assert runner.execute(explicit=True)["issues"]
    assert runner.execute(explicit=True)["issues"]
    assert len(cloud.submissions) == 1
    assert not any(s["model_cls"].startswith("Qwen") for s in cloud.submissions)


def test_failed_fallback_is_not_submitted_again(runner, cloud, monkeypatch):
    runner.config.concurrency = 1
    runner.config.image_fallback = ModelChoice(task="t2i", model_cls="Qwen-Image-2512", parameters={"resolution_level": "720p"})
    monkeypatch.setattr(runner.client, "query", lambda tid: {"status": "FAILED", "task_id": tid})
    with pytest.raises(HarnessError):
        runner._run_node("character:CHAR_001:base")
    assert len(cloud.submissions) == 2
    with pytest.raises(HarnessError):
        runner._run_node("character:CHAR_001:base")
    assert len(cloud.submissions) == 2


def test_pause_preserves_known_task_and_resume_uses_it(runner, cloud, monkeypatch):
    runner.config.concurrency = 1
    query = runner.client.query

    def pause_after_query(task_id):
        (runner.root / "PAUSE_REQUESTED").write_text("user quota threshold", encoding="utf-8")
        result = query(task_id)
        result["status"] = "RUNNING"
        return result

    monkeypatch.setattr(runner.client, "query", pause_after_query)
    assert runner.execute(explicit=True)["issues"]
    assert len(cloud.submissions) == 1
    task_id = runner.store.current("character:CHAR_001:base")["task_id"]
    (runner.root / "PAUSE_REQUESTED").unlink()
    monkeypatch.setattr(runner.client, "query", query)
    assert not runner.execute(explicit=True)["issues"]
    assert runner.store.current("character:CHAR_001:base")["task_id"] == task_id
    assert len(cloud.submissions) == 44
