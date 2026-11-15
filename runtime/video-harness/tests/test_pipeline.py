from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from video_harness.client import media_input
from video_harness.engine import Runner, create_run
from video_harness.models import Characters, Storyboard, validate_plan
from video_harness.util import HarnessError, read_json, run_lock, write_json

from conftest import EXAMPLE


def test_multi_character_scene_media_routing_and_order(runner, cloud):
    cloud.delay_first_image = True
    result = runner.execute(explicit=True)
    assert not result["issues"]
    assert len(cloud.submissions) == 44
    assert cloud.completed[0] != "task-1"  # Completion order differs from submission order.
    manifest = runner.export()
    assert all(s["status"] in {"pending", "running", "succeeded", "failed", "skipped"} for s in manifest["steps"])
    assert all(a["status"] in {"running", "succeeded", "failed"} and "local_status" in a for a in manifest["attempts"])
    clips = manifest["timeline"]["tracks"][0]["clips"]
    assert [c["unit_id"] for c in clips] == [f"U{i:03}" for i in range(1, 21)]
    for sid, scene, count in [("U001", "SCENE_001", 2), ("U012", "SCENE_002", 2), ("U013", "SCENE_001", 3)]:
        node = runner.graph["frame:" + sid]
        assert node["deps"][0] == "scene:" + scene
        a = runner.store.current("frame:" + sid)
        assert len(a["request"]["dependencies"]) == count
        submit = cloud.tasks[a["task_id"]]
        assert len(submit["input_image"]) == count
        assets = {a["output"]["asset_id"]: a["output"] for a in runner.store.snapshot()["attempts"] if a["output"]}
        for inp, dep in zip(submit["input_image"], a["request"]["dependencies"]):
            assert inp["type"] == "base64"
            assert base64.b64decode(inp["data"]) == cloud.image_for_task(assets[dep["asset_id"]]["task_id"])
        v = runner.store.current("video:" + sid)
        assert v["output"]["parent_asset_ids"] == [a["attempt_id"]]
        assert cloud.tasks[v["task_id"]]["video_duration_seconds"] >= runner.graph["video:" + sid]["data"]["duration_ms"] / 1000
    assert all("authorization" not in h for h in cloud.download_headers)
    assert "temporary-secret" not in (runner.root / "manifest.json").read_text(encoding="utf8")
    assert "base64" not in (runner.root / "state.json").read_text(encoding="utf8")
    assert "test-only-credential" not in (runner.root / "state.json").read_text(encoding="utf8")


def test_dry_run_never_submits(runner, cloud):
    report = runner.dry_run()
    assert len(report["nodes"]) == 44
    assert report["available_quotes_total"] == 88
    assert not cloud.submissions
    assert all("input_meta" in b for b in cloud.quote_bodies if b["task"] != "t2i")


def test_service_outage_stops_new_nodes_and_resume_reuses_task(runner, cloud):
    runner.config.concurrency = 1
    cloud.query_failure_once = True
    failed = runner.execute(explicit=True)
    assert failed["issues"] and len(cloud.submissions) == 1
    task_id = runner.store.current("character:CHAR_001:base")["task_id"]
    restored = runner.execute(explicit=True)
    assert not restored["issues"] and len(cloud.submissions) == 44
    assert runner.store.current("character:CHAR_001:base")["task_id"] == task_id


def test_resume_success_reuses_all_candidates(runner, cloud):
    runner.execute(explicit=True)
    before = len(cloud.submissions)
    result = runner.execute(explicit=True)
    assert not result["issues"]
    assert len(cloud.submissions) == before
    assert len(runner.store.snapshot()["attempts"]) == 44


def test_known_task_is_queried_after_wait_timeout(runner, cloud):
    cloud.running = True
    first = runner.execute(explicit=True)
    assert first["issues"]
    assert len(cloud.submissions) == 4
    a = runner.store.current("character:CHAR_001:base")
    assert a["status"] == "wait_timeout" and a["remote_status"] == "RUNNING"
    tid = a["task_id"]
    cloud.running = False
    second = runner.execute(explicit=True)
    assert not second["issues"]
    assert cloud.queries.count(tid) > 1
    assert len(cloud.submissions) == 44


def test_unknown_submission_not_retried(runner, cloud):
    runner.config.concurrency = 1
    cloud.unknown_once = True
    first = runner.execute(explicit=True)
    assert first["issues"]
    unknown = [a for a in runner.store.snapshot()["attempts"] if a["status"] == "submission_unknown"]
    assert len(unknown) == 1 and not unknown[0]["task_id"]
    before = len(cloud.submissions)
    second = runner.execute(explicit=True)
    assert second["issues"]
    assert len(cloud.submissions) == before
    assert runner.store.spent() >= 2


def test_crash_between_submit_and_persist_is_unknown(runner):
    node_id = "character:CHAR_001:base"
    a = runner.store.attempt(node_id, runner._input_hash(node_id, []), {})
    runner.store.reserve(a["attempt_id"], 2, 100)
    recovered = runner.store.attempt(node_id, a["input_hash"], {})
    assert recovered["status"] == "submission_unknown"


def test_expired_url_refreshes_without_resubmission(runner, cloud):
    runner.client.config.retries = 1
    cloud.expired_once = True
    result = runner.execute(explicit=True)
    assert not result["issues"]
    assert len(cloud.submissions) == 44
    assert cloud.result_requests == 45


def test_missing_file_redownloads_original_task(runner, cloud):
    runner.execute(explicit=True)
    old = runner.store.current("video:U001")
    (runner.root / old["output"]["path"]).unlink()
    result = runner.execute(explicit=True)
    assert not result["issues"]
    assert len(cloud.submissions) == 44
    assert runner.store.current("video:U001")["task_id"] == old["task_id"]


def test_short_video_preserves_actual_asset_and_resumes_without_new_task(runner, cloud, monkeypatch):
    import video_harness.media as media

    original = media.probe

    def short_probe(path, config, kind):
        result = original(path, config, kind)
        if kind == "video":
            result["duration_ms"] = 3000
        return result

    monkeypatch.setattr(media, "probe", short_probe)
    first = runner.execute(explicit=True)
    assert first["issues"]
    a = runner.store.current("video:U001")
    assert a["status"] == "download_or_validation_failed"
    assert a["output"]["technical"]["duration_ms"] == 3000
    assert (runner.root / a["output"]["path"]).is_file()
    count = len(cloud.submissions)
    monkeypatch.setattr(media, "probe", original)
    assert not runner.execute(explicit=True)["issues"]
    assert len(cloud.submissions) == count


def test_budget_atomic_across_concurrent_nodes(runner, cloud):
    runner.config.budget_credits = 3
    result = runner.execute(explicit=True)
    assert result["issues"]
    assert len(cloud.submissions) == 1
    assert runner.store.spent() == 2


def test_execution_gates(runner, cloud):
    with pytest.raises(HarnessError):
        runner.execute()
    runner.config.budget_credits = None
    with pytest.raises(HarnessError):
        runner.execute(explicit=True)
    assert not cloud.submissions


@pytest.mark.parametrize("role,param", [("image", "duration"), ("edit", "negative_prompt"), ("video", "unsupported")])
def test_unsupported_parameters_block_before_paid_tasks(runner, cloud, role, param):
    getattr(runner.config, role).parameters[param] = 5
    with pytest.raises(HarnessError):
        runner.execute(explicit=True)
    assert not cloud.submissions


def test_reference_overflow_never_drops_characters(runner, cloud):
    runner.capabilities()
    runner.contracts["edit"].inputs["input_image"]["maximum"] = 2
    with pytest.raises(HarnessError, match="需要 3 张参考图"):
        runner.execute(explicit=True)
    assert not cloud.submissions


def test_rerun_only_one_shot_and_preserves_old_candidate(runner, cloud):
    runner.execute(explicit=True)
    old = runner.store.current("video:U013")
    assert runner.invalidate(["frame:U013"]) == ["frame:U013", "video:U013"]
    runner.execute(explicit=True)
    assert len(cloud.submissions) == 46
    new = runner.store.current("video:U013")
    assert new["parent_id"] == old["attempt_id"]
    assert (runner.root / old["output"]["path"]).exists()


def test_upstream_input_change_invalidates_affected_descendants(runner, cloud):
    runner.execute(explicit=True)
    data = read_json(runner.root / "characters.json")
    data["characters"][1]["visual_description"] += "，保持小巧的蓝色纽扣"
    write_json(runner.root / "characters.json", data)
    other = Runner(runner.root, runner.client, config=runner.config, contracts=runner.contracts)
    try:
        with pytest.raises(HarnessError, match="上游输入已变化"):
            other.check_assembly_inputs()
        result = other.execute(explicit=True)
        assert not result["issues"]
        # Mother base + her 9 visible shots (U012..U020), each frame and video.
        assert len(cloud.submissions) == 44 + 1 + 9 * 2
        assert len([a for a in other.store.snapshot()["attempts"] if a["node_id"] == "video:U001"]) == 1
    finally:
        other.close()


def test_no_cross_run_reference_reuse(tmp_path, config, client, cloud):
    roots = [create_run(EXAMPLE / "script.md", EXAMPLE / "characters.json", EXAMPLE / "storyboard.json", config, tmp_path) for _ in range(2)]
    assert roots[0] != roots[1]
    task_ids = []
    for root in roots:
        r = Runner(root, client)
        try:
            r.execute(explicit=True)
            task_ids.append(r.store.current("character:CHAR_001:base")["task_id"])
        finally:
            r.close()
    assert task_ids[0] != task_ids[1]
    assert len(cloud.submissions) == 88


def test_scene_prompt_contains_only_environment_state(runner):
    prompt = runner.graph["scene:SCENE_001"]["prompt"]
    assert "小林" not in prompt and "妈妈" not in prompt
    assert "完全无人" in prompt


def test_keyframe_receives_only_referenced_prop_appearance(runner):
    from video_harness.engine import build_graph

    board = runner.board.model_copy(deep=True)
    board.props[1].appearance = "UNIQUE_TRANSPARENT_CONTAINER_AND_COUNT"
    board.props[1].initial_state = "DO_NOT_COPY_INITIAL_STATE_INTO_LATER_SHOTS"
    graph = build_graph(runner.chars, board, runner.config)
    assert "UNIQUE_TRANSPARENT_CONTAINER_AND_COUNT" in graph["frame:U008"]["prompt"]
    assert "DO_NOT_COPY_INITIAL_STATE_INTO_LATER_SHOTS" not in graph["frame:U008"]["prompt"]
    assert "UNIQUE_TRANSPARENT_CONTAINER_AND_COUNT" not in graph["frame:U012"]["prompt"]
    assert graph["frame:U008"]["prompt_version"].endswith(":prop-anchors-v2")


def test_retired_inflight_candidates_are_collected(runner, cloud):
    node_id = "scene:SCENE_001"
    first = runner.store.attempt(node_id, "old-input", {"dependencies": []})
    cloud.tasks["retired-task"] = {"task": "t2i", "model_cls": "gpt-image-2.5-flare"}
    runner.store.update(first["attempt_id"], task_id="retired-task", status="waiting_remote")
    runner.store.invalidate([node_id])
    runner.store.attempt(node_id, "new-input", {})
    report = runner.collect_history()
    assert report["collected"] == [first["attempt_id"]]
    old = next(a for a in runner.store.snapshot()["attempts"] if a["attempt_id"] == first["attempt_id"])
    assert (runner.root / old["output"]["path"]).is_file()


def test_variant_edits_saved_base_and_unused_variants_skipped(runner, cloud):
    data = read_json(runner.root / "characters.json")
    data["characters"][0]["variants"] = [
        {"variant_id": "apron_dirty", "trigger": "制作后", "changes": {"apron": "少量面粉"}, "basis": "设计建议"},
        {"variant_id": "unused", "trigger": "无", "changes": {}, "basis": "未使用"},
    ]
    write_json(runner.root / "characters.json", data)
    board = read_json(runner.root / "storyboard.json")
    board["shots"][2]["visible_characters"][0]["variant_id"] = "apron_dirty"
    write_json(runner.root / "storyboard.json", board)
    r = Runner(runner.root, runner.client)
    try:
        assert "character:CHAR_001:unused" not in r.graph
        result = r.execute(explicit=True)
        assert not result["issues"]
        variant = r.store.current("character:CHAR_001:apron_dirty")
        base = r.store.current("character:CHAR_001:base")
        assert variant["output"]["parent_asset_ids"] == [base["attempt_id"]]
        assert cloud.tasks[variant["task_id"]]["task"] == "i2i"
        assert "character:CHAR_001:apron_dirty" in r.graph["frame:U003"]["deps"]
    finally:
        r.close()


def test_existing_image_binding_preferred(tmp_path, config, client, cloud):
    path = tmp_path / "mother.png"
    path.write_bytes(cloud.image)
    config.bindings["character:CHAR_002:base"] = str(path)
    root = create_run(EXAMPLE / "script.md", EXAMPLE / "characters.json", EXAMPLE / "storyboard.json", config, tmp_path)
    r = Runner(root, client)
    try:
        r.execute(explicit=True)
        assert len(cloud.submissions) == 43
        a = r.store.current("character:CHAR_002:base")
        assert a["task_id"] is None and a["output"]["source_type"] == "provided"
    finally:
        r.close()


@pytest.mark.parametrize("change", ["duplicate", "missing_character", "missing_variant", "timeline", "duration", "enum", "character_type"])
def test_invalid_plan_reports_needs_review(change):
    c, b = read_json(EXAMPLE / "characters.json"), read_json(EXAMPLE / "storyboard.json")
    if change == "duplicate":
        c["characters"].append(c["characters"][0])
    if change == "missing_character":
        b["shots"][0]["visible_characters"][0]["character_id"] = "missing"
    if change == "missing_variant":
        b["shots"][0]["visible_characters"][0]["variant_id"] = "missing"
    if change == "timeline":
        b["shots"][1]["timeline_in_ms"] = 0
    if change == "duration":
        b["shots"][0]["duration_ms"] = 16000
    if change == "enum":
        c["characters"][0]["presence"] = "unknown"
    if change == "character_type":
        c["characters"][0]["character_type"] = "undefined_kind"
    with pytest.raises((HarnessError, ValidationError)):
        validate_plan(Characters.model_validate(c), Storyboard.model_validate(b))


def test_url_media_input_and_run_lock(tmp_path):
    assert media_input("https://example.org/image.png", 100) == {"type": "url", "data": "https://example.org/image.png"}
    with run_lock(tmp_path):
        with pytest.raises(HarnessError):
            with run_lock(tmp_path):
                pass


@pytest.mark.parametrize("change", ["missing_file", "local_settings"])
def test_final_delivery_is_stale_when_file_or_export_settings_change(runner, cloud, change):
    from video_harness.util import file_hash

    assert not runner.execute(explicit=True)["issues"]
    final = runner.root / "exports/final.mp4"
    final.write_bytes(b"LOCAL_MOCK_ASSEMBLY_FOR_STATE_TEST_ONLY")
    settings = {"width": runner.config.export_width, "height": runner.config.export_height, "fps": runner.config.fps, "fit": runner.config.fit}
    write_json(
        runner.root / "exports/assembly.json",
        {
            "status": "assembled_needs_review",
            "settings": settings,
            "asset": {
                "path": "exports/final.mp4",
                "sha256": file_hash(final),
                "kind": "video",
                "technical": {"verification": "mocked_for_test"},
                "parent_asset_ids": [runner.store.current("video:" + s.shot_id)["attempt_id"] for s in runner.board.shots],
            },
        },
    )
    assert runner.export()["delivery"]["status"] == "review"
    if change == "missing_file":
        final.unlink()
    else:
        runner.config.fit = "crop"
    assert runner.export()["delivery"]["status"] == "not_ready"
    assert len(cloud.submissions) == 44
