from contextlib import closing
import base64
from pathlib import Path

import httpx
import pytest

from conftest import EXAMPLE
from test_sequential import ENHANCED, local_tools
from video_harness.client import X2VClient
from video_harness.engine import Runner, build_graph, create_run
from video_harness.media import assemble, run_process
from video_harness.models import Config, ImageRefinement, PromptRewriterConfig, QualityConfig, TextConfig
from video_harness.motion import VisualMotionPlanner
from video_harness.operator_review import decision_pointer, load_decision, save_decision
from video_harness.quality import VisualReviewer
from video_harness.util import HarnessError, digest, file_hash, read_json, write_json


@pytest.mark.parametrize(("end_frames", "reject_end", "camera_fail"), [(False, False, False), (True, False, False), (True, True, False), (True, False, True)])
def test_motion_plan_physics_review_and_operator_gate_end_to_end(tmp_path, cloud, monkeypatch, end_frames, reject_end, camera_fail):
    ffmpeg, ffprobe = local_tools()
    video = tmp_path / "fixture.mp4"
    run_process([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=yellow:s=108x192:r=24:d=5", "-c:v", "libx264", str(video)], 60)
    payload = video.read_bytes()
    original_handle = cloud.handle

    def handle(request):
        if request.url.host == "media.test" and cloud.tasks[request.url.path.strip("/")]["task"] == "i2av":
            return httpx.Response(200, content=payload)
        return original_handle(request)

    observed, rewrite_sources = [], []

    def motion(self, data, images, **kwargs):
        observed.append("motion")
        assert Path(images[0][1]).is_file()
        return {"plan": {"feasible": True, "duration_ms": 5000, "motion_description": "CAUSAL_PHASE_FROM_ACTUAL_FRAME", "steps": ["Rotate before release"], "reason": "Fixture", "risks": []},
                "request_hash": "fixture", "record_path": "fixture"}

    def review(self, data, images, **kwargs):
        observed.append(data["stage"])
        if end_frames and data["stage"] == "end_frame" and data["contract"]["shot_id"] == "U002":
            assert data["image_refinements"] == ["Preserve existing large fragments during transport."]
            assert "actual candidate" in data["refinement_review_scope"]
        if data["stage"] == "end_frame" and reject_end:
            return {"verdict": {"outcome": "fail", "issues": [{"category": "object_state", "severity": "blocker", "expected": "required material state", "observed": "wrong target", "evidence": "fixture"}], "summary": "Rejected target fixture", "coverage": "Fixture only", "visual_anchors": []}, "request_hash": "fixture", "record_path": "fixture"}
        if data["stage"] == "physics":
            assert "contract" not in data and "candidate_prompt" not in data
            assert all(label.startswith("Video t=") for label, path in images)
        if data["stage"] == "prompt":
            assert data["causal_motion_plan"]["motion_description"] in data["candidate_prompt"]
        return {"verdict": {"outcome": "pass", "issues": [], "summary": "Fixture only", "coverage": "Not a real visual review", "visual_anchors": []}, "request_hash": "fixture", "record_path": "fixture"}

    class FixtureRewriter:
        identity = {"fixture": True}

        def rewrite(self, source, duration, aspect, task, directory, check_pause, **kwargs):
            rewrite_sources.append(source)
            assert "CAUSAL_PHASE_FROM_ACTUAL_FRAME" in source
            assert Path(kwargs["first_frame"]).is_file()
            if end_frames:
                assert Path(kwargs["last_frame"]).is_file()
            root = Path(directory) / digest(source)
            root.mkdir(parents=True, exist_ok=True)
            raw = root / "model-output.json"
            write_json(raw, {"fixture": True})
            prompt = root / "minimax-prompt.txt"
            prompt.write_text(source, encoding="utf-8")
            path = root / "record.json"
            record = {"record_path": str(path), "prompt": source, "input_hash": digest(source),
                      "raw_output_sha256": file_hash(raw), "prompt_file_sha256": file_hash(prompt)}
            write_json(path, record)
            return record

    monkeypatch.setattr(VisualMotionPlanner, "plan", motion)
    monkeypatch.setattr(VisualReviewer, "review", review)
    def fixed_camera(reviewer, first_frame, target_frame, **limits):
        observed.append("fixed_camera")
        assert Path(first_frame).is_file() and Path(target_frame).is_file()
        return {"verdict": {"outcome": "fail", "issues": [{"category": "scene", "severity": "blocker", "expected": "fixed table", "observed": "table deleted", "evidence": "fixture"}], "summary": "Camera invariant failure fixture", "coverage": "Fixture only", "visual_anchors": []}, "request_hash": "camera-fixture", "record_path": "fixture"}
    monkeypatch.setattr("video_harness.engine.review_fixed_camera", fixed_camera)
    monkeypatch.setattr(Runner, "video_rewriter", lambda self, node: FixtureRewriter() if node["kind"] in {"video", "prompt_rewrite"} else None)
    monkeypatch.setenv("QUALITY_TEST_KEY", "fixture-not-real")
    cfg = Config(ffmpeg=ffmpeg, ffprobe=ffprobe, execute_enabled=True, budget_credits=1000,
                 concurrency=2, request_interval_seconds=0, poll_interval_seconds=.001, retries=0,
                 quality=QualityConfig(enabled=True, causal_motion_planning=True, independent_physics_review=True, require_operator_review=True, end_frame_conditioning=end_frames),
                 prompt_rewriter=PromptRewriterConfig(enabled=True, input_mode="first_last_frame" if end_frames else "first_frame"),
                 text=TextConfig(endpoint="https://test.invalid/chat/completions", key_env="QUALITY_TEST_KEY"))
    board = read_json(ENHANCED / "storyboard.json")
    board["shots"] = board["shots"][:2]
    if camera_fail:
        board["shots"][0]["camera"]["movement"] = "固定"
        cfg.image_refinements = {"end_frame:U001": ImageRefinement(instructions="Preserve fixed-camera geometry.", check_fixed_camera=True)}
    board["total_duration_ms"] = 10000
    write_json(tmp_path / "board.json", board)
    root = create_run(EXAMPLE / "script.md", ENHANCED / "characters.json", tmp_path / "board.json", cfg, tmp_path)
    with closing(X2VClient(cfg, key="fixture", transport=httpx.MockTransport(handle), sleep=lambda _: None)) as client:
        runner = Runner(root, client)
        runner._rewriter = FixtureRewriter()
        try:
            runner.dry_run()
            assert not observed and not rewrite_sources
            if end_frames:
                video_quotes = [b for b in cloud.quote_bodies if b["task"] == "i2av"]
                assert video_quotes and all(len(b["input_meta"]["images"]) == 1 and b["input_meta"]["last_frame"]["width"] > 0 for b in video_quotes)
            result = runner.execute(explicit=True)
            if reject_end or camera_fail:
                assert "check:end_frame:U001" in result["issues"]
                assert runner.store.current("video:U001") is None
                assert runner.store.current("rewrite:U001") is None
                assert runner.store.current("frame:U002") is None
                assert not [r for r in cloud.submissions if r["task"] == "i2av"]
                if camera_fail:
                    assert "fixed_camera" in observed and "end_frame" not in observed
                    frame_id = runner.store.current("frame:U001")["attempt_id"]
                    rejected_image = runner.root / runner.store.current("end_frame:U001")["output"]["path"]
                    for index in range(2):
                        submissions = len(cloud.submissions)
                        repair = runner.repair_image_from_review("end_frame:U001")
                        assert not repair["submitted"] and len(cloud.submissions) == submissions
                        assert rejected_image.is_file() and "table deleted" in runner.graph["end_frame:U001"]["prompt"]
                        assert runner.graph["check:end_frame:U001"]["data"]["image_refinements"] == ["Preserve fixed-camera geometry."]
                        assert "table deleted" not in str(runner.graph["check:end_frame:U001"])
                        with pytest.raises(HarnessError):
                            runner.repair_image_from_review("end_frame:U001")
                        assert "check:end_frame:U001" in runner.execute(explicit=True)["issues"]
                        assert runner.store.current("frame:U001")["attempt_id"] == frame_id
                        assert not [r for r in cloud.submissions if r["task"] == "i2av"]
                    with pytest.raises(HarnessError, match="2 次"):
                        runner.repair_image_from_review("end_frame:U001")
                return
            assert "check:operator:U001" in result["issues"]
            assert runner.store.current("tail:U001") is None
            assert runner.store.current("frame:U002") is None
            assert observed.index("frame") < observed.index("motion") < observed.index("prompt") < observed.index("physics") < observed.index("video")
            with pytest.raises(HarnessError):
                assemble(root, runner.board, runner.store, cfg)
            runner.record_operator_decision("U001", decision="approve", notes="Fixture inspection only", reviewer="test")
            if end_frames:
                existing_inputs = {key: runner._input_hash(key, runner._dependency_outputs(node)) for key, node in runner.graph.items() if runner.store.current(key)}
                cfg.image_refinements = {"end_frame:U002": "Preserve existing large fragments during transport."}
                runner.graph = build_graph(runner.chars, runner.board, cfg)
                # A scoped repair cannot discard the already generated and approved prefix.
                assert all(runner._input_hash(key, runner._dependency_outputs(runner.graph[key])) == value for key, value in existing_inputs.items())
                assert "Preserve existing large fragments" in runner.graph["end_frame:U002"]["prompt"]
            result = runner.execute(explicit=True)
            assert "check:operator:U002" in result["issues"]
            runner.record_operator_decision("U002", decision="approve", notes="Fixture inspection only", reviewer="test")
            assert not runner.execute(explicit=True)["issues"]
            runner.check_assembly_inputs()
            assert len([r for r in cloud.submissions if r["task"] == "i2av"]) == 2
            for i, submission in enumerate([r for r in cloud.submissions if r["task"] == "i2av"], start=1):
                sid = f"U{i:03}"
                actual_first = root / runner.store.current("frame:" + sid)["output"]["path"]
                assert base64.b64decode(submission["input_image"]["data"]) == actual_first.read_bytes()
                if end_frames:
                    actual_last = root / runner.store.current("end_frame:" + sid)["output"]["path"]
                    assert base64.b64decode(submission["input_last_frame"]["data"]) == actual_last.read_bytes()
                    assert "check:end_frame:" + sid in runner.graph["rewrite:" + sid]["deps"]
                else:
                    assert "input_last_frame" not in submission
            assert len(rewrite_sources) == 2
            pointer = decision_pointer(root, "U001")
            decision_file = root / read_json(pointer)["record_path"]
            decision_file.write_text(decision_file.read_text(encoding="utf-8") + " ", encoding="utf-8")
            assert not runner.current_inputs()["check:operator:U001"]
            with pytest.raises(HarnessError):
                assemble(root, runner.board, runner.store, cfg)
            runner.record_operator_decision("U001", decision="reject", notes="Reopened fixture review", reviewer="test")
            with pytest.raises(HarnessError):
                assemble(root, runner.board, runner.store, cfg)
        finally:
            runner.close()


def test_operator_decision_cannot_approve_a_replacement_candidate(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"fixture, not playable media")
    attempt = {"attempt_id": "first", "output": {"kind": "video", "path": "video.mp4", "sha256": file_hash(path)}}
    save_decision(tmp_path, "U001", attempt, decision="approve", notes="Fixture only", reviewer="test")
    assert load_decision(tmp_path, "U001", attempt)[0]["decision"] == "approve"
    with pytest.raises(HarnessError, match="旧候选"):
        load_decision(tmp_path, "U001", {**attempt, "attempt_id": "replacement"})


@pytest.mark.parametrize("feasible,duration", [(False, 5000), (True, 6000)])
def test_motion_planning_cannot_hide_infeasibility_or_retime(tmp_path, monkeypatch, feasible, duration):
    monkeypatch.setenv("TEXT_LLM_API_KEY", "fixture-not-real")
    planner = VisualMotionPlanner(TextConfig(endpoint="https://test.invalid", model="fixture"))
    def generate(data, *args, **kwargs):
        if data.get("stage") == "initial_geometry":
            assert "contract" not in data
            return {"observations": [{"object": "Fixture", "orientation": "upward", "openings_and_boundaries": "top opening", "contents": "solid material", "support_and_contact": "held"}], "hands": "occupied", "uncertainties": []}, tmp_path, "geometry"
        assert data["initial_geometry"]["observations"][0]["orientation"] == "upward"
        return {"feasible": feasible, "duration_ms": duration, "motion_description": "Fixture", "steps": ["Fixture"], "reason": "Insufficient time or invalid geometry", "risks": []}, tmp_path, "fixture"
    monkeypatch.setattr(planner, "generate_visual", generate)
    with pytest.raises(HarnessError):
        planner.plan({"contract": {"duration_ms": 5000}}, [])
    assert (tmp_path / "motion-plan.raw.json").is_file()
