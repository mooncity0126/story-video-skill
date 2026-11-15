from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

from video_harness import cli
from video_harness.client import X2VClient
from video_harness.engine import Runner, build_graph, create_run
from video_harness.models import Config, DurationPlanningConfig, GenerationPlan, ModelChoice, Storyboard, TextConfig, TimingPlan, load_config, load_plan
from video_harness.planning import check_runtime_duration, prepare_board, shot_dependencies, shot_timing
from video_harness.text import TextAdapter, plan
from video_harness.util import HarnessError, digest, file_hash, read_json, write_json

from conftest import EXAMPLE


def small_board():
    chars, source = load_plan(EXAMPLE / "characters.json", EXAMPLE / "storyboard.json")
    data = source.model_dump()
    data["shots"] = data["shots"][:3]
    data["total_duration_ms"] = 15000
    for index, shot in enumerate(data["shots"]):
        shot.update(duration_ms=5000, timeline_in_ms=index * 5000, video_prompt=None, objects=[])
        shot["reference_plan"] = {"strategy": "independent_keyframe"}
        shot["generation_plan"] = {"relation": "independent", "confidence": "high", "reason": "起始状态可由公共参考确定", "references": []}
        shot["timing"] = {"beats": [{"beat_id": "A", "description": "动作", "duration_ms": 4100, "after": [], "kind": "action"}], "reason": "按动作估计"}
        shot["audio"]["dialogue"] = []
    data["shots"][1]["generation_plan"] = {"relation": "continuous", "confidence": "high", "reason": "延续动作", "references": [{"shot_id": "U001", "kind": "tail", "reason": "承接动作"}]}
    data["shots"][2]["scene_id"] = data["scenes"][1]["scene_id"]
    data["shots"][2]["transition_from_previous"] = "scene_change"
    return chars, Storyboard.model_validate(data)


def adaptive_config(**kwargs):
    return Config(shot_scheduling="dependency", require_object_states=False, duration_planning=DurationPlanningConfig(enabled=True), **kwargs)


def test_dependencies_branch_and_nonadjacent_return():
    chars, board = small_board()
    cfg = adaptive_config()
    graph = build_graph(chars, board, cfg)
    assert "tail:U001" in graph["frame:U002"]["deps"]
    assert not any(x.startswith("tail:") for x in graph["frame:U003"]["deps"])
    # A later scene return can use U001 even with an intervening independent shot.
    board.shots[2].generation_plan = GenerationPlan(relation="continuous", confidence="high", reason="回到较早空间", references=[{"shot_id": "U001", "kind": "tail", "reason": "空间状态"}])
    graph = build_graph(chars, board, cfg)
    assert "tail:U001" in graph["frame:U003"]["deps"]
    assert "tail:U002" not in graph["frame:U003"]["deps"]
    assert "U001 实际采用片段的尾帧" in graph["frame:U003"]["prompt"]
    assert "相邻上一镜" not in graph["frame:U003"]["prompt"]


def test_uncertain_is_conservative_and_reference_cycle_rejected():
    _, board = small_board()
    board.shots[2].generation_plan.confidence = "low"
    assert shot_dependencies(board)["U003"]["references"][0]["shot_id"] == "U002"
    data = board.model_dump()
    data["shots"][1]["generation_plan"]["references"][0]["shot_id"] = "U003"
    with pytest.raises(ValueError, match="更早"):
        Storyboard.model_validate(data)


def test_state_guard_overrides_independence_for_changed_object():
    chars, board = load_plan(EXAMPLE.parent / "two-cups-minimax-v2/characters.json", EXAMPLE.parent / "two-cups-minimax-v2/storyboard.json")
    for shot in board.shots:
        shot.generation_plan = GenerationPlan(relation="independent", confidence="high", reason="test declaration")
    decisions = shot_dependencies(board)
    assert any(r["shot_id"] == "U006" for r in decisions["U007"]["references"])
    assert any(r["shot_id"] == "U011" for r in decisions["U013"]["references"])
    assert not decisions["U012"]["references"]


def test_timing_critical_path_and_rebuilt_timeline():
    _, board = small_board()
    board.shots[0].timing = TimingPlan.model_validate(
        {
            "beats": [
                {"beat_id": "A", "description": "取物", "duration_ms": 2200},
                {"beat_id": "B", "description": "移动并放置", "duration_ms": 3200, "after": ["A"]},
                {"beat_id": "C", "description": "同步运镜", "duration_ms": 4000, "kind": "camera"},
            ],
            "end_hold_ms": 600,
            "reason": "动作串行，运镜并行",
        }
    )
    result, report = prepare_board(board, adaptive_config())
    assert [s.duration_ms for s in result.shots] == [6000, 5000, 5000]
    assert [s.timeline_in_ms for s in result.shots] == [0, 6000, 11000]
    assert result.total_duration_ms == 16000
    assert report["shots"][0]["timing"]["beats"]["B"] == {"start_ms": 2200, "end_ms": 5400, "description": "移动并放置"}


@pytest.mark.parametrize("problem", ["cycle", "missing", "overlong", "dialogue"])
def test_invalid_timing_stops_before_media(problem):
    _, board = small_board()
    shot = board.shots[0]
    if problem in {"cycle", "missing"}:
        shot.timing.beats[0].after = ["A" if problem == "cycle" else "B"]
    elif problem == "overlong":
        shot.timing.beats[0].duration_ms = 15000
    else:
        shot.audio.dialogue = [{"text": "hello"}]  # Missing matching dialogue beat.
    with pytest.raises(HarnessError):
        prepare_board(board, adaptive_config())


def test_dialogue_floor_and_target_hold():
    _, board = small_board()
    shot = board.shots[0]
    from video_harness.models import Dialogue

    shot.audio.dialogue = [Dialogue(speaker_character_id=None, text="这是一段需要自然语速说完的话。", delivery="voice_over")]
    shot.timing.beats[0].kind = "dialogue"
    shot.timing.beats[0].duration_ms = 100
    shot.timing.beats[0].dialogue_index = 0
    required, _ = shot_timing(shot, DurationPlanningConfig())
    assert required > 4000
    result, _ = prepare_board(board, adaptive_config(target_duration_ms=18000))
    assert result.total_duration_ms == 18000
    with pytest.raises(HarnessError, match="目标片长"):
        prepare_board(board, adaptive_config(target_duration_ms=10000))


@pytest.mark.parametrize("tolerance,allowed", [(0, False), (500, False), (1000, True)])
def test_total_duration_tolerance_never_accelerates_actions(tolerance, allowed):
    _, board = small_board()
    board.shots[0].timing.beats[0].duration_ms = 5100
    cfg = adaptive_config(target_duration_ms=15000)
    cfg.duration_planning.target_tolerance_ms = tolerance
    if not allowed:
        with pytest.raises(HarnessError, match="不自动加速"):
            prepare_board(board, cfg)
    else:
        result, report = prepare_board(board, cfg)
        assert result.total_duration_ms == 16000
        assert result.shots[0].timing.beats[0].duration_ms == 5100
        assert report["duration_budget"] == {"target_ms": 15000, "tolerance_ms": 1000, "actual_ms": 16000, "difference_ms": 1000}


def test_actual_duration_mismatch_blocks_before_submission(runner):
    contract = runner.capabilities()["video"]
    node = {"kind": "video", "data": {"duration_ms": 4100}}
    body = contract.parameters(runner.config.video, "prompt", "9:16", 4100)
    with pytest.raises(HarnessError, match="实时接口时长"):
        check_runtime_duration(node, body, contract, adaptive_config())


def test_new_defaults_and_legacy_compatibility():
    new = load_config()
    assert new.shot_scheduling == "dependency" and new.rewrite_ahead and not new.require_object_states
    assert new.text.model == "qwen3.7-plus"
    old = Config.model_validate({})
    assert old.shot_scheduling == "legacy" and not old.rewrite_ahead


def test_text_cache_finish_reason_and_call_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXT_LLM_API_KEY", "text-secret")
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "qwen3.7-plus", "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}], "usage": {"total_tokens": 20}})

    cfg = TextConfig(endpoint="https://text.test/v1/chat/completions", model="qwen3.7-plus", enable_thinking=False, max_calls=1)
    adapter = TextAdapter(cfg, httpx.MockTransport(handle))
    assert adapter.generate("JSON", "input", cache_dir=tmp_path) == {"ok": True}
    assert adapter.generate("JSON", "input", cache_dir=tmp_path) == {"ok": True}
    assert len(calls) == 1 and calls[0]["enable_thinking"] is False
    assert calls[0]["max_tokens"] == cfg.max_tokens
    with pytest.raises(HarnessError, match="次数上限"):
        adapter.generate("JSON", "another input", cache_dir=tmp_path)
    assert all("text-secret" not in p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json"))
    truncated = TextAdapter(cfg, httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})))
    with pytest.raises(HarnessError, match="截断"):
        truncated.generate("JSON", "input", cache_dir=tmp_path / "truncated")


def test_text_planning_reuses_characters_and_normalizes_arithmetic(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXT_LLM_API_KEY", "text-secret")
    chars, board = small_board()
    raw = board.model_dump()
    raw["total_duration_ms"] = 1
    raw["shots"][1]["timeline_in_ms"] = 5555
    raw["shots"][1]["first_frame_reference_policy"] = "continuous"
    received = []

    def handle(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw)}}]})

    cfg = adaptive_config(text=TextConfig(endpoint="https://text.test/v1/chat/completions", model="qwen3.7-plus"))
    charfile = tmp_path / "characters.json"
    write_json(charfile, chars.model_dump())
    root = tmp_path / "plan"
    result = plan(EXAMPLE / "script.md", cfg, root, httpx.MockTransport(handle), characters=charfile)
    assert result["shots"] == 3 and len(received) == 1
    assert read_json(root / "storyboard.raw.json")["total_duration_ms"] == 1
    assert read_json(root / "storyboard.json")["total_duration_ms"] == 15000
    assert read_json(root / "storyboard.json")["shots"][1]["first_frame_reference_policy"] == "planned_frame"
    assert "普通布景无需逐物品" in received[0]["messages"][0]["content"]
    assert "把场景固定结构和人物以外的每件可见物品登记" not in received[0]["messages"][0]["content"]
    assert plan(EXAMPLE / "script.md", cfg, root, httpx.MockTransport(handle), characters=charfile)["requests_this_invocation"] == 0


def test_plan_command_does_not_construct_video_client(tmp_path, monkeypatch):
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.setattr(cli, "X2VClient", lambda *a: pytest.fail("text planning must not construct video client"))
    result = cli.perform(cli.parser().parse_args(["plan", "--script", str(EXAMPLE / "script.md"), "--output", str(tmp_path)]))
    assert result["status"] == "needs_text_provider_or_import"


def test_shot_cap_blocks_model_output_and_import_before_media(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXT_LLM_API_KEY", "fixture")
    chars, board = small_board()
    cfg = adaptive_config(max_shots=2, text=TextConfig(endpoint="https://text.test/chat/completions", model="qwen-test"))
    write_json(tmp_path / "characters.json", chars.model_dump())
    write_json(tmp_path / "storyboard.json", board.model_dump())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(board.model_dump())}}]}))
    with pytest.raises(HarnessError, match="超过配置上限"):
        plan(EXAMPLE / "script.md", cfg, tmp_path / "plan", transport, characters=tmp_path / "characters.json")
    assert (tmp_path / "plan/storyboard.raw.json").exists()
    assert not (tmp_path / "plan/storyboard.json").exists()
    with pytest.raises(HarnessError, match="超过配置上限"):
        create_run(EXAMPLE / "script.md", tmp_path / "characters.json", tmp_path / "storyboard.json", cfg, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


def test_replan_retains_prefix_and_setting_without_editing_model_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXT_LLM_API_KEY", "fixture")
    chars, board = small_board()
    cfg = adaptive_config(max_shots=3, text=TextConfig(endpoint="https://text.test/chat/completions", model="qwen-test"))
    board, _ = prepare_board(board, cfg)
    board.shots[0].first_frame_reference_policy = "planned_frame"
    raw = {"shots": [s.model_dump() for s in board.shots[1:]]}
    raw["shots"][0]["action_description"] = "Model-authored complete causal action"
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw)}}]})

    write_json(tmp_path / "characters.json", chars.model_dump())
    write_json(tmp_path / "reference.json", board.model_dump())
    root = tmp_path / "plan"
    kwargs = dict(characters=tmp_path / "characters.json", reference_storyboard=tmp_path / "reference.json", keep_prefix=1)
    result = plan(EXAMPLE / "script.md", cfg, root, httpx.MockTransport(handle), **kwargs)
    output = read_json(root / "storyboard.json")
    assert result["shots"] == 3 and output["shots"][0] == board.shots[0].model_dump()
    assert output["scenes"] == [s.model_dump() for s in board.scenes]
    assert output["props"] == [p.model_dump() for p in board.props]
    assert output["shots"][1]["action_description"] == raw["shots"][0]["action_description"]
    assert read_json(root / "storyboard.raw.json") == raw
    assert plan(EXAMPLE / "script.md", cfg, root, httpx.MockTransport(handle), **kwargs)["requests_this_invocation"] == 0
    assert len(calls) == 1 and "fixed_prefix" in calls[0]["messages"][1]["content"]


@pytest.mark.parametrize("fits", [True, False])
def test_budget_replanning_is_bounded_and_preserves_raw_model_outputs(tmp_path, monkeypatch, fits):
    monkeypatch.setenv("TEXT_LLM_API_KEY", "fixture")
    chars, board = small_board()
    initial = board.model_dump()
    initial["shots"][0]["timing"]["beats"][0]["duration_ms"] = 9000
    proposed = board.model_dump() if fits else initial
    responses = iter([initial, proposed])
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(next(responses))}}]})

    cfg = adaptive_config(target_duration_ms=15000, text=TextConfig(endpoint="https://text.test/chat/completions", model="qwen-test", max_calls=2))
    write_json(tmp_path / "characters.json", chars.model_dump())
    root = tmp_path / "plan"
    args = (EXAMPLE / "script.md", cfg, root, httpx.MockTransport(handle))
    if fits:
        result = plan(*args, characters=tmp_path / "characters.json")
        assert result["total_duration_ms"] == 15000
        assert read_json(root / "storyboard.json")["shots"][0]["timing"] == proposed["shots"][0]["timing"]
    else:
        with pytest.raises(HarnessError, match="不自动加速"):
            plan(*args, characters=tmp_path / "characters.json")
        assert not (root / "storyboard.json").exists()
    assert len(calls) == 2
    assert read_json(root / "storyboard.raw.json") == initial
    assert read_json(root / "storyboard.duration-repair.raw.json") == proposed
    assert "duration_budget" in calls[1]["messages"][1]["content"]


def test_rewrite_and_cloud_overlap_resume_and_media_filter(tmp_path, cloud, monkeypatch):
    from test_sequential import local_tools
    from video_harness.media import assemble, run_process
    from operations.deliver_sequential import deliver

    ffmpeg, ffprobe = local_tools()
    source = tmp_path / "rewritten-clip.mp4"
    run_process([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=blue:s=108x192:r=24:d=5", "-c:v", "libx264", str(source)], 60)
    payload = source.read_bytes()
    chars, board = small_board()
    # Only independent shots here; serial tail byte correctness has its own FFmpeg tests.
    for shot in board.shots:
        shot.generation_plan = GenerationPlan(relation="independent", confidence="high", reason="独立场景")
    cfg = adaptive_config(execute_enabled=True, budget_credits=1000, concurrency=1, rewrite_ahead=True, request_interval_seconds=0, poll_interval_seconds=0.001, retries=0, sequential_shots=False)
    cfg.prompt_rewriter.enabled = True
    cfg.ffmpeg, cfg.ffprobe = ffmpeg, ffprobe
    cfg.export_width, cfg.export_height = 108, 192
    cfg.image = ModelChoice(task="t2i", model_cls="gpt-image-2.5-flare", parameters={"resolution_level": "1080p"})
    cfg.edit = ModelChoice(task="i2i", model_cls="gpt-image-2.5-flare", parameters={"resolution_level": "1080p"})
    cfg.image_fallback = cfg.edit_fallback = None
    write_json(tmp_path / "chars.json", chars.model_dump())
    write_json(tmp_path / "board.json", board.model_dump())
    rewrite_started, image_started = threading.Event(), threading.Event()
    calls = []

    class FakeRewriter:
        def __init__(self, config, proper_names=()):
            self.identity = {"test": "fake-local-rewriter"}

        def rewrite(self, source, duration, aspect, task, directory, check_pause):
            calls.append(source)
            rewrite_started.set()
            assert image_started.wait(4), "cloud queue stalled behind local rewrite"
            check_pause()
            root = Path(directory) / digest(source)
            root.mkdir(parents=True, exist_ok=True)
            write_json(root / "model-output.json", {"raw": "test output"})
            prompt = "ENGLISH TEST " + str(len(calls))
            (root / "minimax-prompt.txt").write_text(prompt, encoding="utf-8")
            record = {
                "input_hash": digest(source),
                "source_prompt": source,
                "prompt": prompt,
                "record_path": str(root / "record.json"),
                "raw_output_sha256": file_hash(root / "model-output.json"),
                "prompt_file_sha256": file_hash(root / "minimax-prompt.txt"),
            }
            write_json(root / "record.json", record)
            return record

    monkeypatch.setattr("video_harness.engine.LocalPromptRewriter", FakeRewriter)
    original = cloud.handle

    def handle(request):
        if request.url.host == "media.test" and cloud.tasks[request.url.path.strip("/")]["task"] == "i2av":
            return httpx.Response(200, content=payload)
        if request.url.path.endswith("/task/submit"):
            body = json.loads(request.content)
            if body["task"] == "t2i" and not image_started.is_set():
                assert rewrite_started.wait(4), "local rewrite could not run during cloud work"
                image_started.set()
            if body["task"] == "i2av":
                assert body["prompt"].startswith("ENGLISH TEST")
                assert isinstance(body["input_image"], dict)  # Exactly one frame, no text artifact as image.
        return original(request)

    client = X2VClient(cfg, key="mock", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    root = create_run(EXAMPLE / "script.md", tmp_path / "chars.json", tmp_path / "board.json", cfg, tmp_path / "runs")
    runner = Runner(root, client)
    try:
        assert runner.graph["rewrite:U002"]["deps"] == []
        assert "rewrite:U002" in runner.graph["video:U002"]["deps"]
        assert not runner.execute(explicit=True)["issues"]
        count = len(cloud.submissions)
        assert len(calls) == 3
        assert not runner.execute(explicit=True)["issues"]
        assert len(calls) == 3 and len(cloud.submissions) == count
        assemble(runner.root, runner.board, runner.store, cfg)
        runner.export()
        delivery = deliver(runner.root, tmp_path / "delivery")
        assert delivery["status"] == "technical_validation_passed"
        assert delivery["previous_tail_count"] == 0 and delivery["shot_scheduling"] == "dependency"
        affected = runner.invalidate(["frame:U001"])
        assert "rewrite:U001" not in affected and "frame:U002" not in affected
        assert "video:U001" in affected
    finally:
        runner.close()
        client.close()


def test_independent_branch_completes_while_continuous_branch_waits(tmp_path, cloud):
    from test_sequential import local_tools
    from video_harness.media import run_process

    ffmpeg, ffprobe = local_tools()
    chars, board = small_board()
    cfg = adaptive_config(ffmpeg=ffmpeg, ffprobe=ffprobe, execute_enabled=True, budget_credits=1000, concurrency=2, request_interval_seconds=0, poll_interval_seconds=0.001, retries=0)
    write_json(tmp_path / "chars.json", chars.model_dump())
    write_json(tmp_path / "board.json", board.model_dump())
    source = tmp_path / "clip.mp4"
    run_process([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=yellow:s=108x192:r=24:d=5", "-c:v", "libx264", str(source)], 60)
    payload = source.read_bytes()
    independent_submitted = threading.Event()
    original = cloud.handle
    root = create_run(EXAMPLE / "script.md", tmp_path / "chars.json", tmp_path / "board.json", cfg, tmp_path / "runs")
    runner = None

    def handle(request):
        if request.url.path.endswith("/task/submit"):
            body = json.loads(request.content)
            if body["task"] == "i2av":
                if body["prompt"] == runner.graph["video:U003"]["prompt"]:
                    independent_submitted.set()
                if body["prompt"] == runner.graph["video:U002"]["prompt"]:
                    assert runner.store.current("video:U001")["status"] == "succeeded"
        if request.url.host == "media.test":
            task = cloud.tasks[request.url.path.strip("/")]
            if task["task"] == "i2av":
                if task["prompt"] == runner.graph["video:U001"]["prompt"]:
                    assert independent_submitted.wait(5), "independent shot was unnecessarily serialized"
                return httpx.Response(200, content=payload)
        return original(request)

    client = X2VClient(cfg, key="mock", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    runner = Runner(root, client)
    try:
        assert not runner.execute(explicit=True)["issues"]
        affected = runner.invalidate(["video:U001"])
        assert {"tail:U001", "frame:U002", "video:U002"} <= set(affected)
        assert not ({"frame:U003", "video:U003"} & set(affected))
        assert runner.export()["units"][2]["depends_on"] == []
    finally:
        runner.close()
        client.close()
