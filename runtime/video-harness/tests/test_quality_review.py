import json

import httpx
from PIL import Image
import pytest

from video_harness.models import TextConfig
from video_harness.quality import VisualReviewer
from video_harness.util import HarnessError, read_json


def reviewer(monkeypatch, responses):
    monkeypatch.setenv("QWEN_API_KEY", "test-review-key")
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        value = responses[min(len(calls) - 1, len(responses) - 1)]
        return httpx.Response(200, json={"model": "qwen3-vl-plus", "usage": {"total_tokens": 100}, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]})

    config = TextConfig(endpoint="https://review.example/chat/completions", model="qwen3-vl-plus", key_env="QWEN_API_KEY", max_calls=10)
    return VisualReviewer(config, httpx.MockTransport(handler)), calls


PASS = {"outcome": "pass", "issues": [], "summary": "No observed drift in supplied image.", "coverage": "One supplied image only."}


def test_visual_inputs_are_sent_but_never_logged_as_base64(monkeypatch, tmp_path):
    adapter, calls = reviewer(monkeypatch, [PASS])
    frame = tmp_path / "frame.png"
    Image.new("RGB", (32, 64), "red").save(frame)
    first = adapter.review({"stage": "frame"}, [("actual frame", frame)], cache_dir=tmp_path / "cache", total_call_limit=1)
    again = adapter.review({"stage": "frame"}, [("actual frame", frame)], cache_dir=tmp_path / "cache", total_call_limit=1)
    assert first == again and len(calls) == 1
    parts = calls[0]["messages"][1]["content"]
    assert parts[-1]["image_url"]["url"].startswith("data:image/png;base64,")
    logged = next((tmp_path / "cache").glob("*/request.json")).read_text(encoding="utf-8")
    assert "base64," not in logged and "test-review-key" not in logged
    assert "sha256" in logged and "actual frame" in logged


def test_persistent_call_limit_survives_new_adapter(monkeypatch, tmp_path):
    adapter, calls = reviewer(monkeypatch, [PASS])
    adapter.review({"stage": "text"}, [], cache_dir=tmp_path, total_call_limit=1)
    restarted, calls_after_restart = reviewer(monkeypatch, [PASS])
    with pytest.raises(HarnessError, match="预算上限"):
        restarted.review({"stage": "different"}, [], cache_dir=tmp_path, total_call_limit=1)
    assert len(calls) == 1 and not calls_after_restart


def test_image_change_cannot_reuse_prior_review(monkeypatch, tmp_path):
    adapter, calls = reviewer(monkeypatch, [PASS])
    frame = tmp_path / "frame.png"
    Image.new("RGB", (32, 64), "red").save(frame)
    first = adapter.review({}, [("frame", frame)], cache_dir=tmp_path / "cache")
    Image.new("RGB", (32, 64), "blue").save(frame)
    second = adapter.review({}, [("frame", frame)], cache_dir=tmp_path / "cache")
    assert first["request_hash"] != second["request_hash"] and len(calls) == 2


def test_inconsistent_pass_is_rejected_and_raw_response_preserved(monkeypatch, tmp_path):
    bad = {**PASS, "issues": [{"category": "identity", "severity": "blocker", "expected": "same person", "observed": "different person", "evidence": "frame 2"}]}
    adapter, _ = reviewer(monkeypatch, [bad])
    with pytest.raises(HarnessError, match="不能当作验收通过"):
        adapter.review({}, [], cache_dir=tmp_path)
    assert len(list(tmp_path.glob("*/response.json"))) == 1
    assert not list(tmp_path.glob("*/verdict.json"))


def test_uncertain_is_preserved_as_uncertain(monkeypatch, tmp_path):
    adapter, _ = reviewer(monkeypatch, [{**PASS, "outcome": "uncertain", "coverage": "Hands obscured, contact cannot be checked."}])
    result = adapter.review({}, [], cache_dir=tmp_path)
    assert result["verdict"]["outcome"] == "uncertain"
    assert read_json(result["record_path"])["outcome"] == "uncertain"
