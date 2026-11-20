"""Offline behavior tests. No model inference and no live network requests."""
from __future__ import annotations

import copy
from array import array
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import wave

import httpx
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import media_tools as ops

BASE = ops.load_settings(ops.DEFAULT_CONFIG)
from video_harness.client import SubmissionUnknown
from video_harness.media import asset_record

fixture_spec = importlib.util.spec_from_file_location("upstream_media_test_helpers", Path(BASE["runtime"]["harness_root"]) / "tests/conftest.py")
fixture_module = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixture_module)


@pytest.fixture(autouse=True)
def block_live_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live network is forbidden in skill tests")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    from video_harness.rewriter import LocalPromptRewriter
    monkeypatch.setattr(LocalPromptRewriter, "rewrite", blocked)


@pytest.fixture
def config(monkeypatch):
    cfg = copy.deepcopy(BASE)
    cfg.pop("workflow_preferences_file", None)  # Exercise pre-setup project snapshots here.
    cfg.pop("credentials_file", None)  # Fixtures use isolated test keys, never the user's file.
    cfg["providers"]["media"].update(base_url="https://fixture.media.test", key_env="STORY_SKILL_TEST_KEY")
    monkeypatch.setenv("STORY_SKILL_TEST_KEY", "test-only-key")
    cfg["models"]["image"].update(model_cls="gpt-image-2.5-flare", parameters={"resolution_level": "1080p"})
    cfg["models"]["edit"].update(model_cls="gpt-image-2.5-flare", parameters={"resolution_level": "1080p"})
    cfg["models"]["video"].update(model_cls="LTX-2.3", parameters={"resolution_level": "720p"})
    cfg["rewriter"]["enabled"] = False
    cfg["review"]["require_material_approval"] = False  # Existing tests isolate other workflow behavior.
    cfg["execution"].update(budget_credits=100, retries=0, request_interval_seconds=0)
    return cfg


@pytest.fixture
def root(config, tmp_path):
    path = tmp_path / "project"
    ops.init_run(config, path)
    return path


@pytest.fixture
def cloud():
    return fixture_module.MockCloud()


@pytest.fixture
def client(config, cloud):
    value = ops.make_client(config, config["providers"]["media"], httpx.MockTransport(cloud.handle))
    yield value
    value.close()


def image_request(job_id="image_1"):
    return {"job_id": job_id, "role": "image", "prompt": "A quiet empty room."}


def review_request(root):
    frame = root / "assets/review-first.png"
    Image.new("RGB", (108, 192), "blue").save(frame)
    return {"job_id": "video_review", "role": "video", "shot_id": "S001", "duration_ms": 5000,
            "prompt": "A woman says hello, then holds her gaze.", "inputs": {"input_image": [str(frame)]}}


def user_approval(config, root, request):
    recipe = ops.request_data(config, request, root)
    ops.save(root / "material-approvals.json", {"shots": {"S001": {
        "actor": "user", "decision": "approved", "fingerprint": ops.material_fingerprint(config, recipe)}}})


def test_paid_video_requires_user_material_approval_before_any_remote_call(config, root, monkeypatch):
    config["review"]["require_material_approval"] = True
    request = review_request(root)
    def forbidden(*args, **kwargs):
        raise AssertionError("Unapproved video must not instantiate a cloud client")
    monkeypatch.setattr(ops, "make_client", forbidden)
    with pytest.raises(ValueError, match="Material review required"):
        ops.submit_job(config, root, request, root, execute=True)
    assert not list((root / "jobs").iterdir())


def test_approved_video_and_quotes_preserve_existing_idempotency(config, root, cloud, client):
    config["review"]["require_material_approval"] = True
    request = review_request(root)
    assert ops.submit_job(config, root, request, root, client=client)["status"] == "quoted"
    assert not cloud.submissions
    user_approval(config, root, request)
    first = ops.submit_job(config, root, request, root, execute=True, client=client)
    (root / "material-approvals.json").unlink()
    assert ops.submit_job(config, root, request, root, execute=True, client=client) == first
    assert len(cloud.submissions) == 1


@pytest.mark.parametrize("changed", ["prompt", "image", "duration", "model", "aspect", "provider"])
def test_material_changes_invalidate_user_approval(config, root, changed):
    config["review"]["require_material_approval"] = True
    request = review_request(root)
    user_approval(config, root, request)
    if changed == "prompt":
        request["prompt"] += " She steps back."
    elif changed == "image":
        Image.new("RGB", (108, 192), "red").save(request["inputs"]["input_image"][0])
    elif changed == "duration":
        request["duration_ms"] = 6000
    elif changed == "model":
        config["models"]["video"]["parameters"]["resolution_level"] = "1080p"
    elif changed == "aspect":
        config["production"]["aspect_ratio"] = "16:9"
    else:
        config["providers"]["media"]["base_url"] = "https://changed.fixture.test"
    with pytest.raises(ValueError, match="outdated"):
        ops.submit_job(config, root, request, root, execute=True)


def test_approval_does_not_override_pause_or_pending_user_input(config, root):
    config["review"]["require_material_approval"] = True
    request = review_request(root)
    user_approval(config, root, request)
    (root / "PAUSE_REQUESTED").write_text("user pause", encoding="utf-8")
    with pytest.raises(ValueError, match="Project paused"):
        ops.submit_job(config, root, request, root, execute=True)
    (root / "PAUSE_REQUESTED").unlink()  # Isolated fixture only.
    ops.save(root / "workflow-inbox.json", {"status": "pending_planning"})
    with pytest.raises(ValueError, match="await planning"):
        ops.submit_job(config, root, request, root, execute=True)
    assert not list((root / "jobs").iterdir())


def test_machine_decision_cannot_stand_in_for_user_approval(config, root):
    config["review"]["require_material_approval"] = True
    request = review_request(root)
    user_approval(config, root, request)
    path = root / "material-approvals.json"
    record = ops.read(path)
    record["shots"]["S001"]["actor"] = "agent"
    ops.save(path, record)
    with pytest.raises(ValueError, match="Material review required"):
        ops.submit_job(config, root, request, root, execute=True)


def test_first_use_cli_and_per_film_snapshots(config, tmp_path):
    template = tmp_path / "template.json"
    ops.save(template, config)
    args = ["--config", str(template)]
    run = tmp_path / "first-movie"
    status = ops.main([*args, "setup"])
    assert not status["configured"] and status["values"]["scheduling"] == "serial"
    assert all(item.get("impact") or all(o["impact"] for o in item["choices"]) for item in status["fields"])
    with pytest.raises(ops.SetupRequired):
        ops.main([*args, "init", "--output", str(run)])
    assert not run.exists()
    blocked = subprocess.run([sys.executable, "-B", "-X", "utf8", str(Path(ops.__file__)), *args, "init", "--output", str(run)],
                             capture_output=True, text=True, encoding="utf-8")
    assert blocked.returncode == 2 and "工作流设置" in json.loads(blocked.stderr)["error"] and not run.exists()
    answers = tmp_path / "answers.json"
    ops.save(answers, status["values"])
    ops.main([*args, "setup", "--answers", str(answers)])
    ops.main([*args, "init", "--output", str(run)])
    snapshot = (run / "settings.json").read_bytes()
    first = ops.load_settings(run / "settings.json")
    assert first["execution"]["budget_credits"] is None
    assert first["production"]["scheduling"] == "serial" and "workflow_preferences_file" not in first
    ops.save(answers, {**status["values"], "scheduling": "dependency_parallel", "writer": "text_api", "repairs": "0"})
    ops.main([*args, "setup", "--answers", str(answers)])
    second = tmp_path / "second-movie"
    ops.main([*args, "init", "--output", str(second)])
    saved = ops.load_settings(second / "settings.json")
    assert saved["production"]["writer"] == "text_api" and saved["review"]["max_content_repairs_per_shot"] == 0
    assert snapshot == (run / "settings.json").read_bytes()
    with pytest.raises(ValueError, match="快照"):
        ops.main(["--config", str(template), "--run", str(run), "status"])


def test_separate_video_endpoint_and_key_do_not_change_images(config, tmp_path, monkeypatch):
    config["workflow_preferences_file"] = str(tmp_path / "workflow-preferences.local.json")
    values = ops.setup_options(config)[1]
    url = "https://video.example.test"
    with pytest.raises(ValueError, match="专属"):
        ops.profile_settings(config, {**values, "api_url": url})
    effective = ops.profile_settings(config, {**values, "api_url": url, "key_name": "ALT_VIDEO_KEY"})
    custom = effective["providers"][effective["models"]["video"]["provider"]]
    assert custom["base_url"] == url and effective["models"]["image"]["provider"] == "media"
    dedicated = Path(custom["credentials_file"])
    assert dedicated.parent == tmp_path
    ops.save(dedicated, {"ALT_VIDEO_KEY": "fixture-secret"})
    monkeypatch.delenv("ALT_VIDEO_KEY", raising=False)
    with ops.credential_environment(effective):
        assert os.environ["ALT_VIDEO_KEY"] == "fixture-secret"
    assert "ALT_VIDEO_KEY" not in os.environ and "fixture-secret" not in json.dumps(effective)


@pytest.mark.parametrize("mode,flag", [("direct", False), ("online", True)])
def test_first_use_selection_reaches_gateway(config, root, cloud, client, mode, flag):
    config["models"]["video"] = copy.deepcopy(BASE["models"]["video"])
    values = {**ops.setup_options(config)[1], "prompt_mode": mode, "scheduling": "dependency_parallel"}
    effective = ops.profile_settings(config, values)
    effective["execution"]["budget_credits"] = 50
    ops.save(root / "settings.json", effective)
    request = review_request(root)
    user_approval(effective, root, request)
    ops.submit_job(effective, root, request, root, execute=True, client=client)
    assert cloud.submissions[-1]["enhance_prompt"] is flag
    assert cloud.submissions[-1]["prompt"] == request["prompt"]
    assert not list((root / "prompt_rewrites").iterdir())


def test_online_requires_explicit_capability_and_no_double_rewrite(config, root, client, cloud):
    config["models"]["video"] = copy.deepcopy(BASE["models"]["video"])
    effective = ops.profile_settings(config, {**ops.setup_options(config)[1], "prompt_mode": "online"})
    effective["execution"]["budget_credits"] = 50
    _, contract = ops.get_contract(effective, root, "video", client)
    assert contract.params["enhance_prompt"]["type"] == "boolean"
    cached = next((root / "contracts").glob("*.json"))
    data = ops.read(cached)
    del data["spec"]["parameters"]["enhance_prompt"]
    ops.save(cached, data)
    request = review_request(root)
    with pytest.raises(ValueError, match="在线提示词优化"):
        ops.submit_job(effective, root, request, root, client=client)
    assert not cloud.quote_bodies and not cloud.submissions
    effective["rewriter"]["enabled"] = True
    f = root / "invalid.json"
    ops.save(f, effective)
    with pytest.raises(ValueError, match="Inconsistent"):
        ops.load_settings(f)


def test_serial_overrules_all_independent_and_retains_secondary_tail(config, root):
    def shot(sid):
        return {"shot_id": sid, "generation_plan": {"relation": "independent", "confidence": "high", "reason": "独立镜头"}}
    board = {"shots": [shot("A"), shot("B"), shot("C")], "playback_order": ["B", "A", "C"]}
    ops.save(root / "storyboard.json", board)
    ops.save(root / "dependencies.json", {"shots": [{"shot_id": "C", "relation": "continuous",
        "requires": [{"kind": "actual_tail", "source_id": "B", "reason": "更早的道具交接"}]}]})
    config["production"]["scheduling"] = "serial"
    ops.save(root / "settings.json", config)
    graph = ops.shot_dependencies(root)
    assert [i["source_id"] for i in graph["A"]["requires"]] == ["B"]
    assert {i["source_id"] for i in graph["C"]["requires"]} == {"A","B"}
    assert ops.concurrency_status(config, [])["limits"]["video"] == 1
    with pytest.raises(ValueError, match="实际尾帧"):
        ops.require_continuity(config, root, {"role": "image", "shot_id": "A"})
    with pytest.raises(ValueError, match="等待 B"):
        ops.require_continuity(config, root, {"role": "video", "shot_id": "A", "input_hashes": {}})
    config["production"]["scheduling"] = "dependency_parallel"
    ops.save(root / "settings.json", config)
    graph = ops.shot_dependencies(root)
    assert graph["A"]["requires"] == [] and [i["source_id"] for i in graph["C"]["requires"]] == ["B"]


def test_selected_checks_bound_to_current_inputs(config, root):
    config["production"].update(scheduling="dependency_parallel", writer="agent")
    config["review"].update(frame_check="off", prompt_check="always")
    req = review_request(root)
    recipe = ops.request_data(config, req, root)
    checked = ops.planned_prechecks(config, root, recipe)
    assert set(checked) == {"prompt"}
    with pytest.raises(ValueError, match="提示词"):
        ops.require_prechecks(config, root, recipe)
    ops.save(root / "checks/S001-prompt.json", {"enabled": True, "round": 1, "outcome": "pass", "source_hashes": checked["prompt"]})
    ops.require_prechecks(config, root, recipe)
    with pytest.raises(ValueError, match="提示词"):
        ops.require_prechecks(config, root, {**recipe, "prompt": "changed"})
    with pytest.raises(ValueError, match="外部文字 API 已关闭"):
        ops.text_call(config, root, "text", "example", "unsubmitted")


def test_default_config_and_offline_doctor():
    assert BASE["rewriter"]["enabled"] is False
    assert BASE["models"]["video"]["parameters"]["enhance_prompt"] is False
    assert BASE["review"]["require_material_approval"] is True
    assert BASE["production"]["target_end_frames"] == "adaptive"
    assert BASE["review"]["max_content_repairs_per_shot"] == 1
    result = ops.main(["doctor"])
    assert result["ffmpeg"] and result["ffprobe"]
    assert result["prompt_mode"] == "direct"
    assert result["local_rewriter_files_present"] is None


def test_direct_mode_needs_no_local_model_installation(config, tmp_path):
    config["rewriter"] = {"enabled": False}
    config["models"]["video"] = copy.deepcopy(BASE["models"]["video"])
    settings = tmp_path / "settings.json"
    ops.save(settings, config)
    result = ops.main(["--config", str(settings), "doctor"])
    assert result["prompt_mode"] == "direct"
    assert result["local_rewriter_files_present"] is None


@pytest.mark.parametrize("enhancement", [True, None])
def test_direct_h3_requires_explicit_server_enhancement_off(config, tmp_path, enhancement):
    config["models"]["video"] = copy.deepcopy(BASE["models"]["video"])
    params = config["models"]["video"]["parameters"]
    if enhancement is None:
        params.pop("enhance_prompt")
    else:
        params["enhance_prompt"] = enhancement
    settings = tmp_path / "settings.json"
    ops.save(settings, config)
    with pytest.raises(ValueError, match="enhance_prompt=false"):
        ops.load_settings(settings)


def test_direct_prompt_reaches_media_submission_verbatim(config, root, cloud, client):
    config["models"]["video"] = copy.deepcopy(BASE["models"]["video"])
    first = root / "first.png"
    Image.new("RGB", (108, 192), "blue").save(first)
    prompt = (
        "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n\n"
        "integrated_multimodal_description: [Shot 1] Live-action, a static medium close-up. "
        "The man with a low, slightly husky Mandarin voice (S1) meets her gaze. His jaw tightens; "
        "he speaks firmly from 0.30 seconds, with one brief pause: "
        "<d>[Chinese] 这笔钱是我拿的，和她没有关系。</d> "
        "His line ends by 3.80 seconds; his mouth then closes. Allow at least 0.90 seconds after the line.\n\n"
        "overall_soundscape: Quiet room tone and a brief sleeve rustle beneath the clear dialogue.\n\n"
        "non_diegetic_music: N/A\n"
    )
    prompt_file = root / "S001-h3.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    request = {"job_id": "video_direct", "role": "video", "shot_id": "S001", "duration_ms": 5000,
               "prompt_file": prompt_file.name, "inputs": {"input_image": [first.name]}}
    job = ops.submit_job(config, root, request, root, execute=True, client=client)
    assert job["recipe"]["prompt"] == prompt
    assert cloud.submissions[-1]["prompt"] == prompt
    assert cloud.submissions[-1]["model_cls"] == "MiniMax-H3"
    assert cloud.submissions[-1]["enhance_prompt"] is False
    assert cloud.submissions[-1]["video_duration_seconds"] == 5
    assert not list((root / "prompt_rewrites").iterdir())
    assert not (root / "text_requests").exists()
    # A stale rewrite record must not silently replace an authored prompt.
    with pytest.raises(ValueError, match="Direct prompt mode"):
        ops.request_data(config, {**request, "rewriter_record": "old/record.json"}, root)


def test_config_uses_custom_host_and_credential(config):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "RUNNING"})
    client = ops.make_client(config, config["providers"]["media"], httpx.MockTransport(handler))
    try:
        client.query("example-task")
    finally:
        client.close()
    assert seen[0].url.host == "fixture.media.test"
    assert seen[0].headers["Authorization"] == "Bearer test-only-key"


@pytest.fixture
def file_credentials(config, tmp_path):
    folder = tmp_path / "local-config"
    folder.mkdir()
    keys = folder / "keys.json"
    values = {config["providers"]["media"]["key_env"]: "file-media-fixture",
              config["providers"]["text"]["key_env"]: "file-text-fixture"}
    ops.save(keys, values)
    settings = folder / "settings.json"
    ops.save(settings, {**config, "credentials_file": "keys.json"})
    return settings, keys, values


def test_file_keys_reach_all_services_without_entering_records(file_credentials, tmp_path):
    settings, keys, values = file_credentials
    cfg = ops.load_settings(settings)
    previous = {name: os.environ.get(name) for name in values}
    root = tmp_path / "file-key-run"
    template = {**cfg, "workflow_preferences_file": str(settings.with_name("workflow-preferences.local.json"))}
    ops.save_setup(template, ops.setup_options(template)[1])
    ops.main(["--config", str(settings), "init", "--output", str(root)])
    assert ops.read(root / "settings.json")["credentials_file"] == str(keys.resolve())
    report = ops.main(["--run", str(root), "doctor"])
    assert all(report["credential_present"].values())
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "fixture.media.test":
            return httpx.Response(200, json={"status": "RUNNING"})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]})

    transport = httpx.MockTransport(handler)
    frame = root / "assets/frame.png"
    Image.new("RGB", (16, 16)).save(frame)
    with ops.credential_environment(cfg):
        client = ops.make_client(cfg, cfg["providers"]["media"], transport)
        try:
            client.query("fixture-task")
        finally:
            client.close()
        assert ops.text_call(cfg, root, "text", "Return JSON", "test", transport=transport) == {"ok": True}
        assert ops.text_call(cfg, root, "vision", "Return JSON", "test", [frame], transport=transport) == {"ok": True}
    assert [r.headers["Authorization"] for r in requests] == [
        "Bearer file-media-fixture", "Bearer file-text-fixture", "Bearer file-text-fixture"]
    assert {name: os.environ.get(name) for name in values} == previous
    saved = json.dumps(report) + "".join(p.read_text(encoding="utf-8") for p in root.rglob("*.json"))
    assert not any(secret in saved for secret in values.values())


def test_file_keys_reload_and_explicit_empty_overrides_inherited_values(file_credentials):
    settings, keys, values = file_credentials
    cfg = ops.load_settings(settings)
    media = cfg["providers"]["media"]["key_env"]
    inherited = os.environ[media]
    ops.save(keys, {**values, media: "replacement-fixture"})
    with ops.credential_environment(cfg):
        assert os.environ[media] == "replacement-fixture"
    assert os.environ[media] == inherited
    ops.save(keys, {name: "" for name in values})
    assert not any(ops.main(["--config", str(settings), "doctor"])["credential_present"].values())
    assert os.environ[media] == inherited


def test_file_key_environment_is_restored_on_command_failure(file_credentials):
    settings, _, values = file_credentials
    previous = {name: os.environ.get(name) for name in values}
    with pytest.raises(RuntimeError, match="fixture failure"):
        with ops.credential_environment(ops.load_settings(settings)):
            raise RuntimeError("fixture failure")
    assert {name: os.environ.get(name) for name in values} == previous


@pytest.mark.parametrize("bad", [
    '{"QWEN_API_KEY": "secret-fixture-not-for-logs"',
    '["secret-fixture-not-for-logs"]',
    '{"QWEN_API_KEY": ["secret-fixture-not-for-logs"]}',
    '{"PATH": "secret-fixture-not-for-logs"}',
    '{"QWEN_API_KEY": "secret-fixture-not-for-logs\\nextra"}',
])
def test_bad_credential_files_do_not_expose_values_or_change_environment(file_credentials, bad):
    settings, keys, values = file_credentials
    keys.write_text(bad, encoding="utf-8")
    previous = {name: os.environ.get(name) for name in values}
    with pytest.raises(ValueError) as error:
        ops.main(["--config", str(settings), "doctor"])
    assert "secret-fixture-not-for-logs" not in str(error.value)
    assert {name: os.environ.get(name) for name in values} == previous


def test_legacy_environment_configuration_still_works(config):
    config.pop("credentials_file", None)
    name = config["providers"]["media"]["key_env"]
    with ops.credential_environment(config):
        assert os.environ[name] == "test-only-key"


def test_quote_is_read_only_and_submit_is_idempotent(config, root, client, cloud):
    preview = ops.submit_job(config, root, image_request(), root, client=client)
    assert preview["status"] == "quoted"
    assert not list((root / "jobs").glob("*.json"))
    assert not cloud.submissions
    one = ops.submit_job(config, root, image_request(), root, execute=True, client=client)
    two = ops.submit_job(config, root, image_request(), root, execute=True, client=client)
    assert one["task_id"] == two["task_id"]
    assert len(cloud.submissions) == 1
    assert "test-only-key" not in (root / "jobs/image_1.json").read_text()


def test_unknown_submission_never_resubmits(config, root, client, cloud):
    cloud.unknown_once = True
    with pytest.raises(SubmissionUnknown):
        ops.submit_job(config, root, image_request(), root, execute=True, client=client)
    assert ops.submit_job(config, root, image_request(), root, execute=True, client=client)["status"] == "submission_unknown"
    with pytest.raises(ValueError, match="Unresolved"):
        ops.submit_job(config, root, image_request("image_2"), root, execute=True, client=client)
    assert len(cloud.submissions) == 1


def test_budget_and_pause_block_submission(config, root, client, cloud):
    config["execution"]["budget_credits"] = 1
    with pytest.raises(ValueError, match="budget"):
        ops.submit_job(config, root, image_request(), root, execute=True, client=client)
    (root / "PAUSE_REQUESTED").write_text("user paused")
    with pytest.raises(ValueError, match="paused"):
        ops.submit_job(config, root, image_request(), root, execute=True, client=client)
    assert not cloud.submissions


def test_changed_request_and_concurrency_are_explicit(config, root, client):
    config["production"]["maximum_parallel_media_jobs"] = 1
    ops.submit_job(config, root, image_request(), root, execute=True, client=client)
    changed = {**image_request(), "prompt": "Different room"}
    with pytest.raises(ValueError, match="different inputs"):
        ops.submit_job(config, root, changed, root, execute=True, client=client)
    with pytest.raises(ValueError, match="concurrency"):
        ops.submit_job(config, root, image_request("image_2"), root, execute=True, client=client)


def test_four_videos_and_two_images_run_together_and_release_slots(config, root, client, cloud):
    frame = root / "first.png"
    Image.new("RGB", (108, 192), "blue").save(frame)

    def video_request(number):
        return {"job_id": f"video_{number}", "role": "video", "shot_id": f"S{number:03d}",
                "duration_ms": 5000, "prompt": "One continuous action",
                "inputs": {"input_image": [str(frame)]}}

    videos = [ops.submit_job(config, root, video_request(n), root, execute=True, client=client) for n in range(1, 5)]
    with pytest.raises(ValueError, match="Video concurrency"):
        ops.submit_job(config, root, video_request(5), root, execute=True, client=client)
    ops.submit_job(config, root, image_request("image_1"), root, execute=True, client=client)
    edit = {**image_request("edit_1"), "role": "edit", "inputs": {"input_image": [str(frame)]}}
    ops.submit_job(config, root, edit, root, execute=True, client=client)
    with pytest.raises(ValueError, match="Image concurrency"):
        ops.submit_job(config, root, image_request("image_2"), root, execute=True, client=client)
    state = ops.main(["--run", str(root), "status"])["concurrency"]
    assert state["active"] == {"total": 6, "video": 4, "image": 2}
    assert state["available"] == {"video": 0, "image": 0}
    assert len(cloud.submissions) == 6

    # A completed image immediately permits another image while videos remain active.
    ops.poll_job(config, root, "image_1", client=client)
    ops.submit_job(config, root, image_request("image_2"), root, execute=True, client=client)
    with pytest.raises(ValueError, match="Video concurrency"):
        ops.submit_job(config, root, video_request(5), root, execute=True, client=client)

    # A confirmed remote video failure releases its video slot.
    def first_video_failed(request):
        if request.url.path.endswith("/task/query") and request.url.params["task_id"] == videos[0]["task_id"]:
            return httpx.Response(200, json={"status": "FAILED"})
        return cloud.handle(request)
    failed_client = ops.make_client(config, config["providers"]["media"], httpx.MockTransport(first_video_failed))
    try:
        assert ops.poll_job(config, root, "video_1", client=failed_client)["status"] == "remote_failed"
    finally:
        failed_client.close()
    ops.submit_job(config, root, video_request(5), root, execute=True, client=client)
    assert len(cloud.submissions) == 8
    assert ops.main(["--run", str(root), "status"])["concurrency"]["active"] == {"total": 6, "video": 4, "image": 2}


def test_legacy_config_uses_total_limit_for_each_media_role(config, root, client):
    config["production"]["maximum_parallel_media_jobs"] = 3
    del config["production"]["maximum_parallel_video_jobs"]
    del config["production"]["maximum_parallel_image_jobs"]
    ops.save(root / "settings.json", config)
    legacy = ops.load_settings(root / "settings.json")
    for number in range(3):
        ops.submit_job(legacy, root, image_request(f"image_{number}"), root, execute=True, client=client)
    with pytest.raises(ValueError, match="concurrency"):
        ops.submit_job(legacy, root, image_request("image_3"), root, execute=True, client=client)
    assert ops.main(["--run", str(root), "status"])["concurrency"]["limits"] == {"total": 3, "video": 3, "image": 3}


def test_poll_resumes_and_downloads_without_auth_on_media(config, root, client, cloud):
    ops.submit_job(config, root, image_request(), root, execute=True, client=client)
    job = ops.poll_job(config, root, "image_1", client=client)
    assert job["status"] == "succeeded"
    assert job["asset"]["technical"]["verification"] == "verified"
    queries = len(cloud.queries)
    ops.poll_job(config, root, "image_1", client=client)
    assert len(cloud.queries) == queries
    assert all("authorization" not in headers for headers in cloud.download_headers)


def test_known_failure_has_one_explicit_technical_retry(config, root, client):
    ops.submit_job(config, root, image_request(), root, execute=True, client=client)
    original = ops.read(root / "jobs/image_1.json")
    original.update(status="rejected", reserved_credits=0)
    ops.save(root / "jobs/image_1.json", original)
    retry = {**image_request("image_2"), "retry_of": "image_1"}
    ops.submit_job(config, root, retry, root, execute=True, client=client)
    with pytest.raises(ValueError, match="technical retry"):
        ops.submit_job(config, root, {**image_request("image_3"), "retry_of": "image_1"}, root, execute=True, client=client)


def test_local_rewriter_binding_validation_without_inference(tmp_path):
    config = copy.deepcopy(BASE)
    config["rewriter"]["enabled"] = True
    first, wrong = tmp_path / "first.png", tmp_path / "wrong.png"
    Image.new("RGB", (64, 96), "blue").save(first)
    Image.new("RGB", (64, 96), "red").save(wrong)
    cache = tmp_path / "rewrite"
    cache.mkdir()
    ops.save(cache / "input.json", {"first_frame_sha256": ops.sha(first), "duration_seconds": 5, "aspect_ratio": "9:16"})
    ops.save(cache / "model-output.json", {"test": "synthetic evidence, no inference"})
    (cache / "minimax-prompt.txt").write_text("Retained prompt", encoding="utf-8")
    ops.save(cache / "record.json", {"prompt": "Retained prompt", "raw_output_sha256": ops.sha(cache / "model-output.json"), "prompt_file_sha256": ops.sha(cache / "minimax-prompt.txt")})
    request = {"job_id": "video_1", "role": "video", "shot_id": "S001", "duration_ms": 5000,
               "rewriter_record": str(cache / "record.json"), "inputs": {"input_image": [str(first)]}}
    assert ops.request_data(config, request, tmp_path)["prompt"] == "Retained prompt"
    request["inputs"]["input_image"] = [str(wrong)]
    with pytest.raises(ValueError, match="differ"):
        ops.request_data(config, request, tmp_path)


def test_text_cache_reused_after_call_limit(config, root, monkeypatch):
    monkeypatch.setenv(config["providers"]["text"]["key_env"], "test-text-only-key")
    config["execution"]["max_text_calls"] = 1
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}], "usage": {"total_tokens": 10}})
    transport = httpx.MockTransport(handler)
    assert ops.text_call(config, root, "text", "Return JSON", "first", transport=transport) == {"ok": True}
    assert ops.text_call(config, root, "text", "Return JSON", "first", transport=transport) == {"ok": True}
    with pytest.raises(ValueError, match="limit"):
        ops.text_call(config, root, "text", "Return JSON", "second", transport=transport)
    assert len(calls) == 1


@pytest.fixture(scope="session")
def test_video(tmp_path_factory):
    path = tmp_path_factory.mktemp("skill-media") / "sample.mp4"
    subprocess.run([BASE["media"]["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=64x96:r=10:d=5", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=5",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True, timeout=30)
    return path


def seed_video(config, root, test_video, job_id="video_1"):
    import shutil
    target = root / "assets" / (job_id + ".mp4")
    shutil.copyfile(test_video, target)
    asset = asset_record(root, target, "video", ops.technical(config), task_id="test-task")
    job = {"status": "succeeded", "task_id": "test-task", "asset": asset, "reserved_credits": 2,
           "submitted_at": 0, "input_hash": "synthetic",
           "recipe": {"job_id": job_id, "role": "video", "shot_id": "S001", "duration_ms": 5000, "repair_of": None}}
    ops.save(root / "jobs" / (job_id + ".json"), job)
    return job


def review(job, outcome="pass"):
    return {"source_sha256": job["asset"]["sha256"], "outcome": outcome,
            "notes": "Synthetic local test fixture", "coverage": "Test-only; no model evaluation", "audio_checked": False}


def test_real_frame_sampling_tail_and_assembly(config, root, test_video):
    config["media"].update(export_width=64, export_height=96, fps=10)
    job = seed_video(config, root, test_video)
    packet = ops.inspect_video(config, root, "video_1")
    assert len(packet["samples"]) == 6
    assert packet["samples"][0]["time_seconds"] == 0
    assert packet["samples"][-1]["time_seconds"] == pytest.approx(4.9)
    assert Path(packet["audio"]).is_file()
    ops.record_review(root, "video_1", review(job, "uncertain"))
    result = ops.tail(config, root, "video_1")
    assert Path(result["path"]).is_file()
    assembled = ops.assemble(config, root, [{"job_id": "video_1"}])
    assert assembled["asset"]["technical"]["has_audio"]
    assert abs(assembled["asset"]["technical"]["duration_ms"] - 5000) < 101
    assert assembled["shot_reviews"][0]["outcome"] == "uncertain"


@pytest.mark.parametrize("video_seconds,audio_seconds", [(6.6, 6.6), (5.6, 6.0)])
def test_full_native_tails_survive_sampling_continuity_and_join(config, root, test_video, tmp_path,
                                                               video_seconds, audio_seconds):
    """The only sound and the final red picture are after the requested 5 seconds."""
    config["media"].update(export_width=64, export_height=96, fps=10)
    config["production"].update(target_duration_ms=10000, duration_tolerance_ms=0)
    source = tmp_path / "late-dialogue.mp4"
    ffmpeg = config["media"]["ffmpeg"]
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=blue:s=64x96:r=10:d={video_seconds},drawbox=color=red:t=fill:enable='gte(t,5)'",
                    "-f", "lavfi", "-i",
                    f"aevalsrc=0.3*sin(2*PI*880*t)*gte(t\\,{audio_seconds - 0.35}):s=48000:d={audio_seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(source)], check=True, timeout=30)
    first = seed_video(config, root, source)
    second = seed_video(config, root, test_video, "video_2")
    packet = ops.inspect_video(config, root, "video_1")
    assert len(packet["samples"]) == 6
    assert packet["samples"][-1]["time_seconds"] == pytest.approx(video_seconds - 0.1)
    assert packet["retention_policy"] == "full_source"
    assert ops.inspect_video(config, root, "video_1") == packet

    def rms_at(path, start, end):
        with wave.open(str(path), "rb") as stream:
            assert stream.getsampwidth() == 2 and stream.getnchannels() == 1
            rate = stream.getframerate()
            stream.setpos(round(start * rate))
            samples = array("h", stream.readframes(round((end - start) * rate)))
        assert len(samples) > rate * (end - start) * 0.95
        return math.sqrt(sum(sample * sample for sample in samples) / len(samples))

    # inspect extracts the existing complete audio; this is a codec regression,
    # not a new production listening/transcription/checking stage.
    assert rms_at(packet["audio"], 3.0, 3.1) < 10
    assert rms_at(packet["audio"], audio_seconds - 0.2, audio_seconds - 0.05) > 1000
    for job_id, job in [("video_1", first), ("video_2", second)]:
        ops.record_review(root, job_id, review(job, "uncertain"))
    review_before = (root / "reviews/video_1.json").read_bytes()
    legacy_tail = root / "assets/video_1-tail.png"
    Image.new("RGB", (64, 96), "blue").save(legacy_tail)
    legacy_hash = ops.sha(legacy_tail)
    full_tail = ops.tail(config, root, "video_1")
    assert ops.sha(legacy_tail) == legacy_hash
    assert Path(full_tail["path"]).name == "video_1-tail-full.png"
    with Image.open(full_tail["path"]) as frame:
        assert frame.getpixel((32, 48))[0] > 200

    timeline = [{"job_id": "video_1"}, {"job_id": "video_2"}]
    result = ops.assemble(config, root, timeline)
    target = root / result["asset"]["path"]
    assert result["planned_duration_ms"] == 10000
    assert result["actual_duration_ms"] == pytest.approx((audio_seconds + 5) * 1000, abs=101)
    assert result["clips"][0]["duration_ms"] == pytest.approx(audio_seconds * 1000, abs=1)
    assert result["clips"][0]["video_tail_hold_ms"] == pytest.approx((audio_seconds - video_seconds) * 1000, abs=1)
    assert len(result["shot_reviews"]) == 2
    assert (root / "reviews/video_1.json").read_bytes() == review_before
    pcm = tmp_path / "joined.wav"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(target), "-vn", "-ac", "1", "-ar", "16000",
                    str(pcm)], check=True, timeout=30)
    assert rms_at(pcm, 3.0, 3.1) < 10
    assert rms_at(pcm, audio_seconds - 0.2, audio_seconds - 0.05) > 1000
    for time, color_index in [(audio_seconds - 0.2, 0), (audio_seconds + 0.2, 2)]:
        frame_path = tmp_path / f"frame-{time}.png"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(target), "-ss", str(time),
                        "-frames:v", "1", str(frame_path)], check=True, timeout=30)
        with Image.open(frame_path) as frame:
            assert frame.getpixel((32, 48))[color_index] > 200

    # A hard user limit fails without truncating or replacing the usable export.
    from video_harness.util import HarnessError
    previous_hash = ops.sha(target)
    config["production"]["max_duration_ms"] = 10000
    with pytest.raises(HarnessError, match="不能硬切音轨"):
        ops.assemble(config, root, timeline)
    assert ops.sha(target) == previous_hash


def test_old_inspection_is_reused_without_an_additional_round(config, root, test_video, monkeypatch):
    config["media"].update(export_width=64, export_height=96, fps=10)
    job = seed_video(config, root, test_video)
    packet = ops.inspect_video(config, root, "video_1")
    packet.pop("retention_policy")
    ops.save(root / "inspection/video_1/packet.json", packet)
    ops.record_review(root, "video_1", review(job))
    with monkeypatch.context() as patch:
        def unexpected(*args, **kwargs):
            raise AssertionError("Existing inspection must be reused")
        patch.setattr("video_harness.media.run_process", unexpected)
        assert ops.inspect_video(config, root, "video_1") == packet
    result = ops.assemble(config, root, [{"job_id": "video_1"}])
    assert any("restored tail was not additionally reviewed" in note for note in result["known_issues"])


def test_serious_finding_blocks_propagation_and_limits_repair(config, root, test_video, client):
    job = seed_video(config, root, test_video)
    ops.record_review(root, "video_1", review(job, "serious"))
    with pytest.raises(ValueError, match="serious"):
        ops.tail(config, root, "video_1")
    with pytest.raises(ValueError, match="serious"):
        ops.assemble(config, root, [{"job_id": "video_1"}])
    image = root / "frame.png"
    Image.new("RGB", (108, 192), "blue").save(image)
    request = {"job_id": "video_2", "role": "video", "shot_id": "S001", "duration_ms": 5000,
               "prompt": "One continuous action", "inputs": {"input_image": [str(image)]}, "repair_of": "video_1"}
    ops.submit_job(config, root, request, root, execute=True, client=client)
    with pytest.raises(ValueError, match="repair limit"):
        ops.submit_job(config, root, {**request, "job_id": "video_3"}, root, execute=True, client=client)


def test_extra_repair_requires_explicit_matching_run_and_budget(config, root, test_video, client):
    first = seed_video(config, root, test_video)
    second = seed_video(config, root, test_video, "video_2")
    second["recipe"]["repair_of"] = "video_1"
    ops.save(root / "jobs/video_2.json", second)
    ops.record_review(root, "video_1", review(first, "serious"))
    ops.record_review(root, "video_2", review(second, "serious"))
    image = root / "frame.png"
    Image.new("RGB", (108, 192), "blue").save(image)
    request = {"job_id": "video_3", "role": "video", "shot_id": "S001", "duration_ms": 5000,
               "prompt": "One continuous action", "inputs": {"input_image": [str(image)]}, "repair_of": "video_2"}
    budget = config["execution"]["budget_credits"]
    auth = {"status": "authorized", "run_id": root.name, "budget_credits": budget,
            "allow_additional_content_repairs": True}
    for override in ({"run_id": "other"}, {"budget_credits": budget + 1}, {"allow_additional_content_repairs": False}):
        ops.save(root / "budget-authorization.json", {**auth, **override})
        with pytest.raises(ValueError, match="repair limit"):
            ops.submit_job(config, root, request, root, execute=True, client=client)
    ops.save(root / "budget-authorization.json", auth)
    assert ops.submit_job(config, root, request, root, execute=True, client=client)["status"] == "submitted"
    with pytest.raises(ValueError, match="repair limit"):
        ops.submit_job(config, root, {**request, "job_id": "video_4"}, root, execute=True, client=client)


def test_stale_review_and_changed_timeline_are_rejected(config, root, test_video):
    job = seed_video(config, root, test_video)
    bad = {**review(job), "source_sha256": "stale"}
    with pytest.raises(ValueError, match="current video"):
        ops.record_review(root, "video_1", bad)
    ops.record_review(root, "video_1", review(job))
    with pytest.raises(ValueError, match="retained duration"):
        ops.assemble(config, root, [{"job_id": "video_1", "duration_ms": 4000}])
