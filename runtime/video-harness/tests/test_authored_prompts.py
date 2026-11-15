from __future__ import annotations

import pytest

from video_harness.continuity import review_objects
from video_harness.engine import Runner, build_graph, create_run
from video_harness.models import ModelChoice, Storyboard, load_plan
from video_harness.util import HarnessError, read_json, write_json

from conftest import EXAMPLE

V2 = EXAMPLE.parent / "two-cups-minimax-v2"


def h3_config(config):
    return config.model_copy(
        update={"video": ModelChoice(task="i2av", model_cls="MiniMax-H3", parameters={"resolution_level": "768p", "enhance_prompt": False}), "sequential_shots": True, "require_object_states": True}
    )


def test_authored_preview_exact_in_graph_and_adapter(config, client, tmp_path):
    cfg = h3_config(config)
    root = create_run(V2 / "script.md", V2 / "characters.json", V2 / "storyboard.json", cfg, tmp_path)
    runner = Runner(root, client)
    try:
        report = runner.dry_run(quote=False)
        videos = {r["node_id"]: r for r in report["nodes"] if r["kind"] == "video"}
        assert len(videos) == 20
        for shot in runner.board.shots:
            request = videos["video:" + shot.shot_id]["api_parameters"]
            assert request["prompt"] == shot.video_prompt
            assert request["video_duration_seconds"] == shot.duration_ms / 1000
            assert "本镜对象清单" not in request["prompt"]
        assert videos["video:U008"]["api_parameters"]["video_duration_seconds"] == 8
        assert videos["video:U018"]["api_parameters"]["video_duration_seconds"] == 6
        assert runner.board.total_duration_ms == 104000
    finally:
        runner.close()


def test_authored_prompt_submitted_verbatim(config, client, cloud, tmp_path):
    cfg = h3_config(config)
    b = read_json(V2 / "storyboard.json")
    b["shots"] = b["shots"][:1]
    b["total_duration_ms"] = 5000
    path = tmp_path / "one.json"
    write_json(path, b)
    root = create_run(V2 / "script.md", V2 / "characters.json", path, cfg, tmp_path / "runs")
    runner = Runner(root, client)
    try:
        assert not runner.execute(explicit=True)["issues"]
        submitted = [r for r in cloud.submissions if r["task"] == "i2av"]
        assert len(submitted) == 1
        assert submitted[0]["prompt"] == b["shots"][0]["video_prompt"]
        assert submitted[0]["enhance_prompt"] is False
        assert runner.store.current("video:U001")["request"]["api_parameters"]["prompt"] == submitted[0]["prompt"]
    finally:
        runner.close()


def test_reframing_is_declared_not_global_carryover(config):
    chars, board = load_plan(V2 / "characters.json", V2 / "storyboard.json")
    review = review_objects(board)
    assert review["pairs"][0]["out_of_frame_by_reframing"]["PROP_CUP_A"]
    graph = build_graph(chars, board, h3_config(config))
    prompt = graph["frame:U002"]["prompt"]
    assert "PROP_CUP_A" not in prompt and "PROP_CREAM" not in prompt
    assert board.shots[0].end_state not in prompt
    assert "额外人物" in prompt and "仅使用本镜声明的1个人物" in prompt
    assert graph["frame:U002"]["deps"][0] == "tail:U001"


@pytest.mark.parametrize("change", ["unknown", "overlap", "missing_reason", "unexplained"])
def test_invalid_reframing_rejected(change):
    b = read_json(V2 / "storyboard.json")
    exclusions = b["shots"][1]["framing_exclusions"]
    if change == "unknown":
        exclusions[0]["prop_id"] = "PROP_UNKNOWN"
    elif change == "overlap":
        exclusions[0]["prop_id"] = "PROP_PLATE"
    elif change == "missing_reason":
        exclusions[0]["reason"] = " "
    else:
        exclusions.clear()
    with pytest.raises(HarnessError):
        review_objects(Storyboard.model_validate(b))
