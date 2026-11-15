from contextlib import closing
from pathlib import Path

import httpx
import pytest

from conftest import EXAMPLE
from test_sequential import ENHANCED, local_tools
from video_harness.client import X2VClient
from video_harness.engine import Runner, build_graph, create_run
from video_harness.models import Config, PromptRewriterConfig, QualityConfig, TextConfig, load_plan
from video_harness.quality import VisualReviewer
from video_harness.util import HarnessError, digest, file_hash, read_json, write_json


def test_memory_override_is_scoped_to_shot_and_shared_with_video(monkeypatch):
    chars, board = load_plan(ENHANCED / "characters.json", ENHANCED / "storyboard.json")
    created = []

    class FakeRewriter:
        def __init__(self, config, proper_names):
            self.identity = config.model_dump()
            created.append(config.cpu_layers)

    monkeypatch.setattr("video_harness.engine.LocalPromptRewriter", FakeRewriter)
    runner = Runner.__new__(Runner)
    runner.config = Config(prompt_rewriter=PromptRewriterConfig(enabled=True, cpu_layers=2))
    runner.chars, runner._rewriter, runner._scoped_rewriters = chars, None, {}
    first = {"kind": "prompt_rewrite", "data": {"shot_id": "U001"}}
    second = {"kind": "prompt_rewrite", "data": {"shot_id": "U002"}}
    original = runner.video_rewriter(first)
    runner.config.prompt_rewriter_cpu_layers = {"U002": 6}
    runner.config.check_board(board)
    assert runner.video_rewriter(first) is original
    changed = runner.video_rewriter(second)
    assert changed.identity["cpu_layers"] == 6
    assert runner.video_rewriter({**second, "kind": "video"}) is changed
    assert created == [2, 6] and original.identity["cpu_layers"] == 2
    runner.config.prompt_rewriter_cpu_layers["MISSING"] = 6
    with pytest.raises(HarnessError, match="未知分镜"):
        runner.config.check_board(board)


@pytest.mark.parametrize("selector", ["rewrite:MISSING", "video:U001", "frame:U001"])
def test_prompt_refinement_requires_an_existing_rewriter(selector):
    chars, board = load_plan(ENHANCED / "characters.json", ENHANCED / "storyboard.json")
    with pytest.raises(HarnessError, match="prompt_refinements"):
        build_graph(chars, board, Config(prompt_refinements={selector: "fixture"}))


def test_repair_regenerates_only_text_preserves_images_and_keeps_review_independent(tmp_path, cloud, monkeypatch):
    ffmpeg, ffprobe = local_tools()
    sources = []

    class FixtureRewriter:
        identity = {"fixture": True}

        def rewrite(self, source, duration, aspect, task, directory, check_pause, **frames):
            sources.append(source)
            assert all(Path(frames[key]).is_file() for key in ("first_frame", "last_frame"))
            folder = Path(directory) / digest(source)
            folder.mkdir(parents=True, exist_ok=True)
            raw, prompt, record_path = folder / "model-output.json", folder / "minimax-prompt.txt", folder / "record.json"
            write_json(raw, {"fixture": True, "source": source})
            prompt.write_text("fixture model output", encoding="utf-8")
            record = {"record_path": str(record_path), "prompt": "fixture model output", "input_hash": digest(source),
                      "raw_output_sha256": file_hash(raw), "prompt_file_sha256": file_hash(prompt)}
            write_json(record_path, record)
            return record

    def review(self, data, images, **kwargs):
        assert "PRIOR_DIRECTION_DEFECT" not in str(data)
        fail = data["stage"] == "prompt"
        return {"verdict": {"outcome": "fail" if fail else "pass", "summary": "Fixture only", "coverage": "Fixture only", "visual_anchors": [],
                            "issues": [{"category": "action", "severity": "blocker", "expected": "declared turn", "observed": "PRIOR_DIRECTION_DEFECT", "evidence": "fixture"}] if fail else []},
                "request_hash": "fixture", "record_path": "fixture"}

    monkeypatch.setattr(Runner, "video_rewriter", lambda self, node: FixtureRewriter() if node["kind"] in {"video", "prompt_rewrite"} else None)
    monkeypatch.setattr(VisualReviewer, "review", review)
    monkeypatch.setenv("QUALITY_TEST_KEY", "fixture-not-real")
    cfg = Config(ffmpeg=ffmpeg, ffprobe=ffprobe, execute_enabled=True, budget_credits=1000,
                 request_interval_seconds=0, poll_interval_seconds=.001, retries=0,
                 quality=QualityConfig(enabled=True, end_frame_conditioning=True),
                 prompt_rewriter=PromptRewriterConfig(enabled=True, input_mode="first_last_frame"),
                 text=TextConfig(endpoint="https://test.invalid/chat/completions", key_env="QUALITY_TEST_KEY"))
    board = read_json(ENHANCED / "storyboard.json")
    board["shots"], board["total_duration_ms"] = board["shots"][:2], 10000
    write_json(tmp_path / "board.json", board)
    root = create_run(EXAMPLE / "script.md", ENHANCED / "characters.json", tmp_path / "board.json", cfg, tmp_path)
    with closing(X2VClient(cfg, key="fixture", transport=httpx.MockTransport(cloud.handle), sleep=lambda _: None)) as client:
        runner = Runner(root, client)
        runner._rewriter = FixtureRewriter()
        try:
            assert "check:prompt:U001" in runner.execute(explicit=True)["issues"]
            count = len(cloud.submissions)
            kept = {key: runner.store.current(key) for key in ("frame:U001", "end_frame:U001", "check:frame:U001", "check:end_frame:U001")}
            original_prompt = runner.graph["rewrite:U001"]["prompt"]
            runner.graph["rewrite:U001"]["prompt"] += "changed source"
            with pytest.raises(HarnessError, match="已改变"):
                runner.repair_prompt_from_review("rewrite:U001")
            runner.graph["rewrite:U001"]["prompt"] = original_prompt
            check = runner.store.current("check:prompt:U001")
            report = root / check["output"]["path"]
            original_bytes = report.read_bytes()
            report.write_bytes(original_bytes + b" ")
            with pytest.raises(HarnessError, match="已改变"):
                runner.repair_prompt_from_review("rewrite:U001")
            report.write_bytes(original_bytes)
            for _ in range(2):
                old_rewrite = root / runner.store.current("rewrite:U001")["output"]["path"]
                repair = runner.repair_prompt_from_review("rewrite:U001")
                assert not repair["submitted"] and len(cloud.submissions) == count
                assert all(runner.store.current(key) == value for key, value in kept.items())
                assert old_rewrite.is_file() and "frame:U001" not in repair["invalidated"] and "end_frame:U001" not in repair["invalidated"]
                assert "check:prompt:U001" in runner.execute(explicit=True)["issues"]
                assert "PRIOR_DIRECTION_DEFECT" in sources[-1]
                assert len(cloud.submissions) == count
            with pytest.raises(HarnessError, match="2 次"):
                runner.repair_prompt_from_review("rewrite:U001")
            assert len(sources) == 3 and runner.store.current("video:U001") is None
            assert runner.store.current("frame:U002") is None
        finally:
            runner.close()
