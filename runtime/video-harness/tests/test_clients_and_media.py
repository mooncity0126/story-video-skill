from __future__ import annotations

import json

import httpx
import pytest

from video_harness.client import APIError, SubmissionUnknown, X2VClient
from video_harness.contracts import Contract
from video_harness.media import assemble, check_clip
from video_harness.models import Config, ModelChoice, TextConfig
from video_harness.text import TextAdapter, plan
from video_harness.util import HarnessError, read_json, write_json

from conftest import EXAMPLE, FIXTURES


def test_read_retries_429_and_respects_retry_after(config):
    config.retries = 2
    config.wait_timeout_seconds = 20
    calls, sleeps = [], []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3"}, json={"code": "rate_limit"}) if len(calls) == 1 else httpx.Response(200, json={"status": "SUCCEED"})

    client = X2VClient(config, key="test", transport=httpx.MockTransport(handle), sleep=sleeps.append)
    try:
        assert client.query("abc")["status"] == "SUCCEED"
        assert len(calls) == 2 and sleeps[0] >= 3
    finally:
        client.close()


@pytest.mark.parametrize("status,category", [(400, "parameters"), (401, "authentication"), (402, "balance"), (403, "permission"), (422, "parameters")])
def test_non_retryable_errors(config, status, category):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "private response must not leak"})

    client = X2VClient(config, key="test", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    try:
        with pytest.raises(APIError) as error:
            client.quote({})
        assert error.value.category == category and len(calls) == 1
        assert "private response" not in str(error.value)
    finally:
        client.close()


@pytest.mark.parametrize("field", ["message", "error"])
def test_chinese_moderation_rejection_is_not_parameter_failure(config, field):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(400, json={field: "内容审核未通过: prompt不合规"})

    client = X2VClient(config, key="test", transport=httpx.MockTransport(handle), sleep=lambda _: None)
    try:
        with pytest.raises(APIError) as error:
            client.submit({})
        assert error.value.category == "content_policy" and len(calls) == 1
        assert "内容审核" not in str(error.value)
    finally:
        client.close()


def test_gateway_ring_balancer_is_not_account_balance_error(config):
    client = X2VClient(config, key="test", transport=httpx.MockTransport(lambda r: httpx.Response(503, json={"message": "failure to get a peer from the ring-balancer"})))
    try:
        with pytest.raises(APIError) as error:
            client.quote({})
        assert error.value.category == "server"
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["server_500", "malformed", "timeout"])
def test_submit_ambiguous_failure_never_retries(config, mode):
    config.retries = 3
    calls = []

    def handle(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(500, json={}) if mode == "server_500" else httpx.Response(200, content=b"not json")

    client = X2VClient(config, key="test", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(SubmissionUnknown):
            client.submit({})
        assert len(calls) == 1
    finally:
        client.close()


def test_fixed_duration_model_receives_no_duration_parameter():
    choice = ModelChoice(task="i2v", model_cls="Wan2.2_I2V_A14B_distilled")
    contract = Contract(read_json(FIXTURES / "i2v.json")["specs"][0], read_json(FIXTURES / "openapi.json"))
    body = contract.parameters(choice, "动作", "9:16", 4000)
    assert not any("duration" in key for key in body)
    with pytest.raises(HarnessError, match="源视频不足"):
        check_clip({"duration_ms": 3000}, 4000)
    with pytest.raises(HarnessError, match="超过"):
        check_clip({"duration_ms": 16000}, 5000)


def test_duration_rounding_and_model_minimum(runner):
    contract = runner.capabilities()["video"]
    assert contract.parameters(runner.config.video, "动作", "9:16", 4100)["video_duration_seconds"] == 5


def test_text_adapter_never_reuses_x2v_key(monkeypatch):
    monkeypatch.setenv("X2V_API_KEY", "shared-secret")
    monkeypatch.setenv("TEXT_LLM_API_KEY", "shared-secret")
    with pytest.raises(HarnessError, match="不得使用"):
        TextAdapter(TextConfig(endpoint="https://text.test/v1/chat/completions", model="text"))


def test_text_planning_without_key_exports_prompts_only(tmp_path, monkeypatch):
    monkeypatch.delenv("TEXT_LLM_API_KEY", raising=False)
    result = plan(EXAMPLE / "script.md", Config(), tmp_path)
    assert result["status"] == "needs_text_provider_or_import"
    assert (tmp_path / "characters-prompt.md").exists()
    assert not (tmp_path / "characters.json").exists()


def test_independent_text_provider_receives_script_and_character_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXT_LLM_API_KEY", "text-service-only")
    monkeypatch.setenv("X2V_API_KEY", "x2v-service-only")
    received = []

    def handle(request):
        received.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer text-service-only"
        data = read_json((EXAMPLE if len(received) == 1 else EXAMPLE.parent / "two-cups-sequential") / ("characters.json" if len(received) == 1 else "storyboard.json"))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(data, ensure_ascii=False)}}]})

    cfg = Config(text=TextConfig(endpoint="https://text.test/v1/chat/completions", model="text"))
    result = plan(EXAMPLE / "script.md", cfg, tmp_path, transport=httpx.MockTransport(handle))
    assert result["status"] == "planned"
    assert "CHAR_001" in received[1]["messages"][1]["content"]
    assert "x2v-service-only" not in json.dumps(received)


def test_unknown_submission_cannot_be_bypassed_by_rerun_or_edit(runner, cloud):
    cloud.unknown_once = True
    runner.config.concurrency = 1
    runner.execute(explicit=True)
    node = next(a["node_id"] for a in runner.store.snapshot()["attempts"] if a["status"] == "submission_unknown")
    with pytest.raises(HarnessError):
        runner.invalidate([node])
    runner.graph[node]["prompt"] += " changed"
    before = len(cloud.submissions)
    runner.execute(explicit=True)
    assert len(cloud.submissions) == before


def test_resolve_unknown_checks_original_model_after_config_change(runner, cloud, client, monkeypatch):
    from video_harness import cli

    cloud.unknown_once = True
    runner.config.concurrency = 1
    runner.execute(explicit=True)
    a = next(a for a in runner.store.snapshot()["attempts"] if a["status"] == "submission_unknown")
    cfg = read_json(runner.root / "config.json")
    cfg["image"] = {"task": "t2i", "model_cls": "Gemini-3.1-Flash-Image", "parameters": {"resolution_level": "2K"}}
    write_json(runner.root / "config.json", cfg)
    monkeypatch.setattr(cli, "X2VClient", lambda config: client)
    before = len(cloud.submissions)
    result = cli.perform(cli.parser().parse_args(["resolve-submission", str(runner.root), "--node-id", a["node_id"], "--task-id", "task-1"]))
    assert result["status"] == "resolved"
    assert runner.store.current(a["node_id"])["task_id"] == "task-1"
    assert len(cloud.submissions) == before


def test_openapi_validation_error_does_not_echo_base64(runner):
    contract = runner.capabilities()["image"]
    with pytest.raises(HarnessError) as error:
        contract.validate({"task": "t2i", "model_cls": "gpt-image-2.5-flare", "stage": "single_stage", "prompt": "safe", "input_image": {"type": "base64", "data": "PRIVATE_BASE64"}})
    assert "PRIVATE_BASE64" not in str(error.value)


def test_missing_ffmpeg_keeps_assets(runner, cloud):
    runner.execute(explicit=True)
    runner.config.ffmpeg = "missing_ffmpeg_for_test"
    with pytest.raises(HarnessError, match="已保留"):
        assemble(runner.root, runner.board, runner.store, runner.config)
    assert all((runner.root / runner.store.current("video:" + s.shot_id)["output"]["path"]).exists() for s in runner.board.shots)
