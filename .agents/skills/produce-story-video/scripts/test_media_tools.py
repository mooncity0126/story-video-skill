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
    cfg.pop("credentials_file", None)  # Fixtures use isolated test keys, never the user's file.
    cfg["providers"]["media"].update(base_url="https://fixture.media.test", key_env="STORY_SKILL_TEST_KEY")
    monkeypatch.setenv("STORY_SKILL_TEST_KEY", "test-only-key")
    cfg["models"]["image"].update(model_cls="gpt-image-2.5-flare", parameters={"resolution_level": "1080p"})
    cfg["models"]["edit"].update(model_cls="gpt-image-2.5-flare", parameters={"resolution_level": "1080p"})
    cfg["models"]["video"].update(model_cls="LTX-2.3", parameters={"resolution_level": "720p"})
    cfg["rewriter"]["enabled"] = False
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


def test_default_config_and_offline_doctor(tmp_path):
    assert BASE["rewriter"]["enabled"] is True
    assert BASE["production"]["target_end_frames"] == "adaptive"
    assert BASE["review"]["max_content_repairs_per_shot"] == 1
    settings = copy.deepcopy(BASE)
    settings.pop("credentials_file", None)
    path = tmp_path / "offline-settings.json"
    ops.save(path, settings)
    result = ops.main(["--config", str(path), "doctor"])
    assert result["ffmpeg"] and result["ffprobe"]


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


def test_stale_review_and_changed_timeline_are_rejected(config, root, test_video):
    job = seed_video(config, root, test_video)
    bad = {**review(job), "source_sha256": "stale"}
    with pytest.raises(ValueError, match="current video"):
        ops.record_review(root, "video_1", bad)
    ops.record_review(root, "video_1", review(job))
    with pytest.raises(ValueError, match="retained duration"):
        ops.assemble(config, root, [{"job_id": "video_1", "duration_ms": 4000}])
