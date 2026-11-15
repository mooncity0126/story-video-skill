from pathlib import Path
from contextlib import closing
import base64

import httpx
import pytest

from conftest import EXAMPLE
from test_sequential import ENHANCED, local_tools
from video_harness.client import X2VClient
from video_harness.engine import Runner, build_graph, create_run
from video_harness.media import assemble, run_process
from video_harness.models import Config, ImageRefinement, PromptRewriterConfig, QualityConfig, TextConfig, load_plan
from video_harness.quality import VisualReviewer, initial_state_contract, sample_video, shot_contract, video_segment_data
from video_harness.util import HarnessError, read_json, write_json


def test_partial_video_review_does_not_require_later_completion():
    contract = {"action": "Put the existing cake into the cup", "end_state": "Cup contains cake", "object_states": [{"prop_id": "cup", "start_state_id": "empty", "end_state_id": "filled"}]}
    data = {"stage": "video", "contract": contract}
    samples = [{"time_seconds": 0}, {"time_seconds": 1.375}]
    early = video_segment_data(data, samples, contains_start=True, contains_end=False, total_samples=49, rate=8)
    assert "end_state" not in early["contract"]
    assert "end_state_id" not in early["contract"]["object_states"][0]
    assert early["contract"]["allowed_action_in_progress"] == contract["action"]
    assert not early["review_scope"]["require_completion"]
    final = video_segment_data(data, samples, contains_start=False, contains_end=True, total_samples=49, rate=8)
    assert final["contract"] == contract and final["review_scope"]["require_completion"]
    assert data["contract"]["end_state"] == "Cup contains cake"


@pytest.mark.parametrize("selector", ["end_frame:MISSING", "video:U001", "frame:U001"])
def test_image_refinement_cannot_silently_target_missing_video_or_bound_image(selector):
    chars, board = load_plan(ENHANCED / "characters.json", ENHANCED / "storyboard.json")
    cfg = Config(quality=QualityConfig(enabled=True, end_frame_conditioning=True),
                 prompt_rewriter=PromptRewriterConfig(enabled=True, input_mode="first_last_frame"),
                 bindings={"frame:U001": "already-provided.image"},
                 image_refinements={selector: "Preserve the actual material structure."})
    with pytest.raises(HarnessError, match="image_refinements"):
        build_graph(chars, board, cfg)


def test_review_dag_guards_frame_rewrite_video_and_tail():
    chars, board = load_plan(ENHANCED / "characters.json", ENHANCED / "storyboard.json")
    cfg = Config(quality=QualityConfig(enabled=True), rewrite_ahead=False,
                 prompt_rewriter=PromptRewriterConfig(enabled=True, input_mode="first_frame"))
    board.shots[0].first_frame_description = "FUTURE_ACTION_MUST_NOT_ENTER_FIRST_FRAME"
    board.shots[0].camera.composition = "FUTURE_ACTION_IN_COMPOSITION"
    graph = build_graph(chars, board, cfg)
    assert graph["rewrite:U001"]["deps"] == ["frame:U001", "check:frame:U001"]
    assert "rewrite:U001" in graph["check:prompt:U001"]["deps"]
    assert "check:prompt:U001" in graph["video:U001"]["deps"]
    assert "check:video:U001" in graph["tail:U001"]["deps"]
    assert graph["frame:U002"]["deps"][0] == "tail:U001"
    assert "此刻唯一的物品与人物状态依据" in graph["frame:U001"]["prompt"]
    assert board.shots[0].first_frame_description not in graph["frame:U001"]["prompt"]
    assert board.shots[0].camera.composition not in graph["frame:U001"]["prompt"]
    # A shared room reference cannot carry finished/initial movable props into every shot.
    for scene in board.scenes:
        recipe = graph["scene:" + scene.scene_id]
        assert recipe["data"]["props"] == [] and recipe["data"]["initial_state"] is None
        assert "桌面及操作面留空" in recipe["prompt"]
        assert "道具外观资料" not in recipe["prompt"]
    # Topological completion proves none of the added gates makes a cycle.
    done = set()
    while len(done) < len(graph):
        ready = {n for n, recipe in graph.items() if n not in done and set(recipe["deps"]) <= done}
        assert ready
        done |= ready


@pytest.mark.parametrize("review_rules", ["Preserve material structure.", ""])
def test_isolating_review_feedback_preserves_paid_image_recipe(review_rules):
    chars, board = load_plan(ENHANCED / "characters.json", ENHANCED / "storyboard.json")
    instructions = "Preserve material structure. Previous candidate defect: TABLE_DELETED."
    cfg = Config(quality=QualityConfig(enabled=True, end_frame_conditioning=True),
                 prompt_rewriter=PromptRewriterConfig(enabled=True, input_mode="first_last_frame"),
                 image_refinements={"end_frame:U002": ImageRefinement(instructions=instructions)})
    old = build_graph(chars, board, cfg)
    cfg.image_refinements["end_frame:U002"].review_instructions = review_rules
    new = build_graph(chars, board, cfg)
    assert "TABLE_DELETED" in new["end_frame:U002"]["prompt"]
    assert "TABLE_DELETED" not in str(new["check:end_frame:U002"])
    assert new["check:end_frame:U002"]["data"]["contract"] == old["check:end_frame:U002"]["data"]["contract"]
    for key in old:
        if key != "check:end_frame:U002":
            assert new[key] == old[key]


@pytest.mark.parametrize("failed_stage", ["frame", "prompt", "video", None])
def test_content_gate_blocks_downstream_and_preserves_candidates(tmp_path, cloud, monkeypatch, failed_stage):
    ffmpeg, ffprobe = local_tools()
    source = tmp_path / "source.mp4"
    run_process([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=yellow:s=108x192:r=24:d=5", "-c:v", "libx264", str(source)], 60)
    payload = source.read_bytes()
    original_handle = cloud.handle

    def handle(request):
        if request.url.host == "media.test" and cloud.tasks[request.url.path.strip("/")]["task"] == "i2av":
            return httpx.Response(200, content=payload)
        return original_handle(request)

    calls = []

    def review(self, data, images, **kwargs):
        calls.append(data["stage"])
        fail = data["stage"] == failed_stage
        verdict = {"outcome": "fail" if fail else "pass", "summary": "Fixture review", "coverage": "Test fixtures only",
                   "issues": [{"category": "identity", "severity": "blocker", "expected": "same person", "observed": "changed costume", "evidence": "fixture"}] if fail else []}
        return {"verdict": verdict, "record_path": "fixture", "request_hash": "fixture"}

    monkeypatch.setattr(VisualReviewer, "review", review)
    monkeypatch.setenv("QUALITY_TEST_KEY", "test-only-not-a-real-key")
    cfg = Config(ffmpeg=ffmpeg, ffprobe=ffprobe, quality=QualityConfig(enabled=True), execute_enabled=True, budget_credits=1000,
                 concurrency=2, request_interval_seconds=0, poll_interval_seconds=0.001, retries=0,
                 text=TextConfig(endpoint="https://test.invalid/chat/completions", key_env="QUALITY_TEST_KEY"))
    board = read_json(ENHANCED / "storyboard.json")
    board["shots"] = board["shots"][:2]
    board["total_duration_ms"] = 10000
    write_json(tmp_path / "board.json", board)
    root = create_run(EXAMPLE / "script.md", ENHANCED / "characters.json", tmp_path / "board.json", cfg, tmp_path)
    with closing(X2VClient(cfg, key="test", transport=httpx.MockTransport(handle), sleep=lambda _: None)) as client:
        runner = Runner(root, client)
        try:
            result = runner.execute(explicit=True)
            videos = [r for r in cloud.submissions if r["task"] == "i2av"]
            assert len(videos) == (2 if failed_stage is None else 1 if failed_stage == "video" else 0)
            # Review JSON dependencies must not become generation reference images.
            for index, video in enumerate(videos, start=1):
                first_frame = runner.store.current(f"frame:U{index:03d}")["output"]
                assert video["input_image"]["type"] == "base64"
                assert base64.b64decode(video["input_image"]["data"]) == (root / first_frame["path"]).read_bytes()
            if failed_stage:
                current = runner.store.current(f"check:{failed_stage}:U001")
                assert current["status"] == "quality_failed"
                assert (root / current["output"]["path"]).is_file()
                assert runner.store.current("tail:U001") is None
                assert runner.store.current("frame:U002") is None
                assert result["issues"]
                with pytest.raises(HarnessError):
                    runner.check_assembly_inputs()
                if failed_stage == "video":
                    assert runner.store.current("video:U001")["status"] == "succeeded"
                    with pytest.raises(HarnessError, match="内容检查"):
                        assemble(root, runner.board, runner.store, cfg)
                count = len(cloud.submissions)
                runner.execute(explicit=True)
                assert len(cloud.submissions) == count
            else:
                assert not result["issues"]
                runner.check_assembly_inputs()
                manifest = read_json(root / "manifest.json")
                assert all(u["status"] == "ready" for u in manifest["units"])
                # Changed contract invalidates prior verdicts, including assembly eligibility.
                runner.board.shots[0].end_state = "A different final state"
                runner.graph = build_graph(runner.chars, runner.board, cfg)
                with pytest.raises(HarnessError):
                    runner.check_assembly_inputs()
        finally:
            runner.close()


def test_real_video_sampling_keeps_exact_retained_endpoints(tmp_path):
    ffmpeg, ffprobe = local_tools()
    cfg = Config(ffmpeg=ffmpeg, ffprobe=ffprobe, quality=QualityConfig(samples_per_second=4))
    source = tmp_path / "source.mp4"
    run_process([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=108x192:r=24:d=5", "-c:v", "libx264", str(source)], 60)
    samples = sample_video(source, tmp_path / "samples", cfg, 3000)
    assert samples[0]["frame_index"] == 0 and samples[0]["time_seconds"] == 0
    assert samples[-1]["frame_index"] == 71
    assert samples[-1]["time_seconds"] == pytest.approx(71 / 24, abs=0.000001)
    assert len(samples) == 13 and all(Path(s["path"]).is_file() for s in samples)


def test_review_key_missing_blocks_before_generation(runner, cloud):
    runner.config.quality.enabled = True
    with pytest.raises(HarnessError, match="未配置独立文本 API"):
        runner.execute(explicit=True)
    assert not cloud.submissions


def test_first_frame_review_cannot_see_future_action_as_an_excuse():
    chars, board = load_plan(ENHANCED / "characters.json", ENHANCED / "storyboard.json")
    shot = board.shots[0]
    contract = shot_contract(shot, chars.characters, board.scenes[0], board.props)
    initial = initial_state_contract(contract)
    assert initial["start_state"] == shot.start_state
    assert not {"action", "end_state", "first_frame_description", "audio"} & initial.keys()
    assert set(initial["camera"]) == {"shot_size", "angle"}
    assert all(not any(k.startswith("end_") for k in obj) for obj in initial["object_states"])
