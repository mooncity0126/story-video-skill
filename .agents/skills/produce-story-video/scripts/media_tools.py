"""Atomic media operations. The agent, not this module, chooses the workflow."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlparse

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs/default.json"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def resolve_path(value, base):
    path = Path(os.path.expandvars(value)).expanduser()
    return str((base / path).resolve() if not path.is_absolute() else path.resolve())


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("job_id/shot_id must contain only letters, digits, underscores or hyphens")
    return value


def load_settings(path):
    path = Path(path).resolve()
    data = read(path)
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported settings schema_version")
    if data.get("credentials_file"):
        if not isinstance(data["credentials_file"], str):
            raise ValueError("credentials_file must be a file path")
        data["credentials_file"] = resolve_path(data["credentials_file"], path.parent)
    for group, fields in [("runtime", ["harness_root", "python"]), ("media", ["ffmpeg", "ffprobe"])]:
        for key in fields:
            value = data[group][key]
            if group == "runtime" or "/" in value or "\\" in value:
                data[group][key] = resolve_path(value, path.parent)
    for key in ("python", "script", "model_root"):
        data["rewriter"]["local"][key] = resolve_path(data["rewriter"]["local"][key], path.parent)
    for provider in data["providers"].values():
        endpoint = provider.get("base_url", provider.get("endpoint", ""))
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query:
            raise ValueError("Provider URLs must be HTTPS without embedded credentials or query strings")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", provider["key_env"]):
            raise ValueError("key_env must be an environment variable name")
    budget = data["execution"]["budget_credits"]
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (float, int)) or not math.isfinite(budget) or budget < 0):
        raise ValueError("budget_credits must be null or a nonnegative finite number")
    if not 2 <= data["review"]["sample_frames"] <= 12:
        raise ValueError("sample_frames must be between 2 and 12")
    if data["review"]["max_content_repairs_per_shot"] not in (0, 1):
        raise ValueError("This skill supports at most one content repair per shot")
    for key in ("maximum_parallel_media_jobs", "maximum_parallel_video_jobs", "maximum_parallel_image_jobs"):
        value = data["production"].get(key, data["production"]["maximum_parallel_media_jobs"])
        if type(value) is not int or not 1 <= value <= 32:
            raise ValueError("Invalid concurrency setting: " + key)
    for key in ("frame_check", "prompt_check"):
        if data["review"].get(key, "on_demand") not in {"on_demand", "always", "off"}:
            raise ValueError("Invalid review selection: " + key)
    for key in ("content_check_rounds", "frame_check_rounds", "prompt_check_rounds", "video_check_rounds"):
        value = data["review"].get(key, 1)
        if type(value) is not int or value != 1:
            raise ValueError("Review rounds are one per candidate: " + key)
    if data["review"].get("sampling", "uniform_count") != "uniform_count" or data["review"].get("include_endpoints", True) is not True:
        raise ValueError("Inspection uses uniform frame sampling including both endpoints")
    if data["review"]["mode"] not in ("agent", "vision_api"):
        raise ValueError("review.mode must be agent or vision_api")
    if data["production"]["target_end_frames"] != "adaptive" or data["production"]["audio"] != "native":
        raise ValueError("This version implements adaptive end frames and native audio")
    maximum = data["production"].get("max_duration_ms")
    if maximum is not None and (type(maximum) is not int or maximum <= 0):
        raise ValueError("max_duration_ms must be null or a positive integer")
    src = Path(data["runtime"]["harness_root"]) / "src"
    if not (src / "video_harness/client.py").is_file():
        raise ValueError("Configured video-harness source not found")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from video_harness.models import ModelChoice
    for role, expected in [("image", {"t2i"}), ("edit", {"i2i"}), ("video", {"i2v", "i2av"})]:
        item = data["models"][role]
        choice = ModelChoice.model_validate({k: v for k, v in item.items() if k != "provider"})
        if choice.task not in expected or data["providers"][item["provider"]]["protocol"] != "lightx2v_cloud":
            raise ValueError("Media roles require compatible LightX2V cloud tasks")
        if any(k.lower() in {"api_key", "authorization", "token"} for k in choice.parameters):
            raise ValueError("Store credential environment names, not credentials in model parameters")
    if data["rewriter"]["enabled"]:
        video = data["models"]["video"]
        if video["model_cls"] not in data["rewriter"]["compatible_models"]:
            raise ValueError("The selected video model has no configured local rewriter adapter")
        if video["parameters"].get("enhance_prompt") is not False:
            raise ValueError("Local rewriting requires enhance_prompt=false")
    return data


@contextmanager
def credential_environment(config):
    """Expose local keys only during this command; never change Windows settings."""
    path = config.get("credentials_file")
    if not path:
        yield
        return
    try:
        values = read(path)
    except (OSError, ValueError):
        raise ValueError("Cannot read credentials_file; check that it exists and contains valid UTF-8 JSON") from None
    allowed = {provider["key_env"] for provider in config["providers"].values()}
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValueError("credentials_file must be an object containing only the configured credential names")
    updates = {}
    for name, value in values.items():
        if not isinstance(value, str):
            raise ValueError("API key values in credentials_file must be strings")
        value = value.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("API key values must not contain control characters")
        # An explicit empty value means unconfigured, even if an inherited key exists.
        updates[name] = value
    previous = {name: os.environ.get(name) for name in updates}
    try:
        os.environ.update(updates)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def save(path, value):
    from video_harness.util import write_json
    write_json(path, value)


def technical(config):
    return SimpleNamespace(**config["media"], max_media_bytes=config["execution"]["max_media_bytes"],
                           quality=SimpleNamespace(enabled=False, require_operator_review=False))


def make_client(config, provider, transport=None):
    from video_harness.client import X2VClient
    client = X2VClient(SimpleNamespace(**config["execution"]), key=os.environ.get(provider["key_env"], ""), transport=transport)
    # No request is made by the parent constructor. Override its legacy constant.
    client.http.base_url = provider["base_url"].rstrip("/")
    return client


def pause_check(root):
    if (Path(root) / "PAUSE_REQUESTED").exists():
        raise ValueError("Project paused; do not clear PAUSE_REQUESTED without the user's resume request")


def concurrency_status(config, jobs):
    production = config["production"]
    total = production["maximum_parallel_media_jobs"]
    limits = {
        "total": total,
        "video": min(total, production.get("maximum_parallel_video_jobs", total)),
        "image": min(total, production.get("maximum_parallel_image_jobs", total)),
    }
    active = {"total": 0, "video": 0, "image": 0}
    for job in jobs:
        if job["status"] in {"submitting", "submission_unknown", "submitted", "waiting_remote"}:
            kind = "video" if job["recipe"]["role"] == "video" else "image"
            active[kind] += 1
            active["total"] += 1
    available = {kind: max(0, min(limits[kind] - active[kind], total - active["total"])) for kind in ("video", "image")}
    return {"active": active, "limits": limits, "available": available}


def init_run(config, root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    for directory in ("jobs", "assets", "contracts", "reviews", "inspection", "prompt_rewrites", "exports"):
        (root / directory).mkdir()
    save(root / "settings.json", config)
    save(root / "project.json", {"created_at": time.time(), "status": "planning", "orchestrator": "agent"})
    return {"run": str(root), "status": "created"}


def get_contract(config, root, role, client):
    from video_harness.contracts import Contract
    from video_harness.models import ModelChoice
    item = config["models"][role]
    choice = ModelChoice.model_validate({k: v for k, v in item.items() if k != "provider"})
    key = fingerprint({"provider": config["providers"][item["provider"]], "choice": item})
    path = Path(root) / "contracts" / (key + ".json")
    if path.exists():
        cached = read(path)
    else:
        specs = [s for s in client.specs(choice) if (s["task"], s["model_cls"], s["stage"]) == (choice.task, choice.model_cls, choice.stage)]
        if len(specs) != 1:
            raise ValueError("Cannot uniquely identify the selected model's live specification")
        cached = {"spec": specs[0], "openapi": client.openapi(), "fetched_at": time.time()}
        save(path, cached)
    return choice, Contract(cached["spec"], cached["openapi"])


def request_data(config, request, base):
    allowed = {"job_id", "role", "shot_id", "prompt", "prompt_file", "duration_ms", "inputs", "rewriter_record", "repair_of", "retry_of"}
    if set(request) - allowed:
        raise ValueError("Unknown request fields")
    identifier(request["job_id"])
    role = request["role"]
    if role not in config["models"]:
        raise ValueError("Unknown model role")
    prompt = request.get("prompt", "")
    if request.get("prompt_file"):
        if prompt:
            raise ValueError("Use prompt or prompt_file, not both")
        prompt = Path(resolve_path(request["prompt_file"], base)).read_text(encoding="utf-8-sig")
    if any(not isinstance(paths, list) or not paths for paths in request.get("inputs", {}).values()):
        raise ValueError("Each inputs entry must be a nonempty list of image paths")
    inputs = {field: [resolve_path(p, base) for p in paths] for field, paths in request.get("inputs", {}).items()}
    hashes = {field: [sha(p) for p in paths] for field, paths in inputs.items()}
    if role == "video":
        identifier(request["shot_id"])
        duration = request.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int) or not 0 < duration <= 15000:
            raise ValueError("Video duration_ms must be a positive integer, at most 15000")
        if config["rewriter"]["enabled"]:
            record_path = resolve_path(request["rewriter_record"], base)
            record = read(record_path)
            original = read(Path(record_path).parent / "input.json")
            first = hashes.get("input_image", [])
            last = hashes.get("input_last_frame", [])
            if first != [original.get("first_frame_sha256")] or last != ([original["last_frame_sha256"]] if original.get("last_frame_sha256") else []):
                raise ValueError("Video images differ from the actual rewriter inputs")
            if original["duration_seconds"] * 1000 != duration or original["aspect_ratio"] != config["production"]["aspect_ratio"]:
                raise ValueError("Video timing/aspect differs from rewritten prompt")
            if sha(Path(record_path).parent / "model-output.json") != record["raw_output_sha256"] or sha(Path(record_path).parent / "minimax-prompt.txt") != record["prompt_file_sha256"]:
                raise ValueError("Rewriter evidence changed")
            saved_prompt = (Path(record_path).parent / "minimax-prompt.txt").read_text(encoding="utf-8").strip()
            if saved_prompt != record["prompt"].strip() or (prompt and prompt.strip() != saved_prompt):
                raise ValueError("Submitted prompt differs from the retained rewriter output")
            prompt = record["prompt"]
    if not prompt.strip():
        raise ValueError("A nonempty prompt is required")
    provider = config["providers"][config["models"][role]["provider"]]
    recipe = {"job_id": request["job_id"], "role": role, "shot_id": request.get("shot_id"), "prompt": prompt,
              "duration_ms": request.get("duration_ms"), "inputs": inputs, "input_hashes": hashes,
              "model": config["models"][role], "provider": provider, "repair_of": request.get("repair_of"), "retry_of": request.get("retry_of")}
    return recipe


def submit_job(config, root, request, base, *, execute=False, client=None):
    from video_harness.client import APIError, SubmissionUnknown, media_input
    from video_harness.media import probe
    from video_harness.util import run_lock
    root = Path(root)
    pause_check(root)
    recipe = request_data(config, request, Path(base))
    path = root / "jobs" / (recipe["job_id"] + ".json")
    identity = fingerprint(recipe)
    with run_lock(root):
        if path.exists():
            old = read(path)
            if old["input_hash"] != identity:
                raise ValueError("job_id already has different inputs; retain it and create an explicit new candidate")
            return old  # Never resubmit an existing job, even after a known failure.
        jobs = [read(p) for p in (root / "jobs").glob("*.json")]
        if any(j["status"] in {"submitting", "submission_unknown"} for j in jobs):
            raise ValueError("Unresolved submission exists; resolve it before new submissions")
        same_shot = [j for j in jobs if recipe["role"] == "video" and j["recipe"].get("shot_id") == recipe["shot_id"] and j["recipe"]["role"] == "video"]
        if recipe.get("retry_of"):
            if recipe.get("repair_of"):
                raise ValueError("Use retry_of for technical failures or repair_of for content, not both")
            retry_id = identifier(recipe["retry_of"])
            parent = next((j for j in jobs if j["recipe"]["job_id"] == retry_id), None)
            if not parent or parent["status"] not in {"rejected", "remote_failed", "confirmed_not_submitted"}:
                raise ValueError("Technical retry requires an unambiguously failed or unsubmitted predecessor")
            if parent["recipe"]["role"] != recipe["role"] or parent["recipe"].get("shot_id") != recipe.get("shot_id"):
                raise ValueError("Technical retry must retain the predecessor role and shot")
            if parent["recipe"].get("retry_of") or any(j["recipe"].get("retry_of") == retry_id for j in jobs):
                raise ValueError("One technical retry already used; ask the user before more attempts")
        elif same_shot:
            parent_id = identifier(recipe.get("repair_of"))
            parent = next((j for j in same_shot if j["recipe"]["job_id"] == parent_id), None)
            review = read(root / "reviews" / (parent_id + ".json"))
            if not parent or review["outcome"] != "serious" or review["source_sha256"] != parent.get("asset", {}).get("sha256"):
                raise ValueError("A video repair requires a current serious finding on its predecessor")
            if len({j["recipe"]["repair_of"] for j in same_shot if j["recipe"].get("repair_of")}) >= config["review"]["max_content_repairs_per_shot"]:
                raise ValueError("Shot content repair limit reached; ask the user")
        elif recipe.get("repair_of"):
            raise ValueError("repair_of must identify an existing video of the same shot")
        capacity = concurrency_status(config, jobs)
        kind = "video" if recipe["role"] == "video" else "image"
        if capacity["available"][kind] == 0:
            raise ValueError(kind.capitalize() + " concurrency limit reached; poll existing jobs first")
        budget = config["execution"]["budget_credits"]
        if execute and budget is None:
            raise ValueError("Set an authorized budget_credits before submission")
        own_client = client is None
        client = client or make_client(config, recipe["provider"])
        try:
            choice, contract = get_contract(config, root, recipe["role"], client)
            body = contract.parameters(choice, recipe["prompt"], config["production"]["aspect_ratio"], recipe["duration_ms"])
            if recipe["role"] == "video":
                field = contract.spec.get("output", {}).get("duration", {}).get("parameter")
                if not field or round(body[field] * 1000) != recipe["duration_ms"]:
                    raise ValueError("Live model duration differs from planned duration; replan explicitly")
            metas = {field: [probe(p, technical(config), "image") for p in paths] for field, paths in recipe["inputs"].items()}
            for field, values in metas.items():
                contract.check_references(field, values)
            last = metas.get("input_last_frame", [])
            quote_body = contract.quote_body(body, [m for f, values in metas.items() if f != "input_last_frame" for m in values], last_frame_meta=last[0] if last else None)
            submission = dict(body)
            for field, paths in recipe["inputs"].items():
                values = [media_input(p, config["execution"]["max_media_bytes"]) for p in paths]
                submission[field] = values if contract.inputs[field]["multiple"] else values[0]
            contract.validate(submission)
            quote = client.quote(quote_body)
            reserved = sum(j.get("reserved_credits", 0) for j in jobs)
            if not execute:
                return {"status": "quoted", "quote": quote, "reserved_credits": reserved, "job_id": recipe["job_id"]}
            if reserved + quote > budget:
                raise ValueError("Media credit budget exceeded")
            pause_check(root)
            job = {"recipe": recipe, "input_hash": identity, "status": "submitting", "task_id": None, "reserved_credits": quote, "submitted_at": time.time()}
            save(path, job)  # Persist reservation before the non-idempotent request.
            try:
                result = client.submit(submission)
            except SubmissionUnknown:
                job["status"] = "submission_unknown"
                save(path, job)
                raise
            except APIError as exc:
                job.update(status="rejected", reserved_credits=0, error=str(exc))
                save(path, job)
                raise
            job.update(task_id=result["task_id"], status="submitted")
            if isinstance(result.get("quoted_credits"), (float, int)):
                job["reserved_credits"] = max(quote, result["quoted_credits"])
            save(path, job)
            return job
        finally:
            if own_client:
                client.close()


def poll_job(config, root, job_id, client=None):
    from video_harness.media import asset_record, check_clip
    from video_harness.util import run_lock
    root = Path(root)
    path = root / "jobs" / (identifier(job_id) + ".json")
    with run_lock(root):
        job = read(path)
        if job["status"] == "succeeded" and sha(root / job["asset"]["path"]) == job["asset"]["sha256"]:
            return job
        if not job["task_id"]:
            raise ValueError("No known task_id; do not resubmit an ambiguous request")
        own_client = client is None
        client = client or make_client(config, job["recipe"]["provider"])
        try:
            result = client.query(job["task_id"])
            job["remote_status"] = result["status"]
            if result["status"] in {"FAILED", "CANCEL"}:
                job["status"] = "remote_failed"
            elif result["status"] != "SUCCEED":
                job["status"] = "waiting_remote"
            else:
                kind = "video" if job["recipe"]["role"] == "video" else "image"
                target = root / "assets" / (job_id + (".mp4" if kind == "video" else ".image"))
                client.download_result(job["task_id"], "output_video" if kind == "video" else "output_image", target)
                asset = asset_record(root, target, kind, technical(config), task_id=job["task_id"])
                if kind == "video":
                    if check_clip(asset["technical"], job["recipe"]["duration_ms"]):
                        raise ValueError("Video duration could not be verified")
                job.update(status="succeeded", asset=asset, completed_at=time.time())
                job["elapsed_seconds"] = job["completed_at"] - job["submitted_at"]
            save(path, job)
            return job
        finally:
            if own_client:
                client.close()


def rewrite(config, root, prompt_file, duration_ms, first_frame, last_frame=None):
    from video_harness.models import PromptRewriterConfig
    from video_harness.rewriter import LocalPromptRewriter
    pause_check(root)
    if not config["rewriter"]["enabled"]:
        raise ValueError("Local rewriting is disabled in this project's configuration")
    settings = PromptRewriterConfig.model_validate({**config["rewriter"]["local"], "enabled": True,
                                                    "input_mode": "first_last_frame" if last_frame else "first_frame"})
    started = time.monotonic()
    result = LocalPromptRewriter(settings).rewrite(
        Path(prompt_file).read_text(encoding="utf-8-sig"), duration_ms / 1000,
        config["production"]["aspect_ratio"], config["models"]["video"]["task"],
        Path(root) / "prompt_rewrites", lambda: pause_check(root),
        first_frame=Path(first_frame).resolve(), last_frame=Path(last_frame).resolve() if last_frame else None,
    )
    save(Path(root) / "prompt_rewrites" / "last-operation.json", {"record_path": result["record_path"], "elapsed_seconds": time.monotonic() - started})
    return {"record_path": result["record_path"], "prompt_file": str(Path(result["record_path"]).parent / "minimax-prompt.txt")}


def video_asset(root, job_id):
    root = Path(root)
    job = read(root / "jobs" / (identifier(job_id) + ".json"))
    if job["status"] != "succeeded" or job["recipe"]["role"] != "video":
        raise ValueError("A downloaded video candidate is required")
    source = root / job["asset"]["path"]
    if sha(source) != job["asset"]["sha256"]:
        raise ValueError("Video changed since download")
    return job, source


def inspect_video(config, root, job_id):
    from PIL import Image, ImageDraw, ImageOps
    from video_harness.media import run_process
    root = Path(root)
    job, source = video_asset(root, job_id)
    out = root / "inspection" / job_id
    out.mkdir(parents=True, exist_ok=True)
    packet_file = out / "packet.json"
    if packet_file.exists():
        packet = read(packet_file)
        if packet["source_sha256"] == job["asset"]["sha256"] and all(Path(p["path"]).is_file() for p in packet["samples"]) and Path(packet["contact_sheet"]).is_file():
            return packet
    cfg = technical(config)
    decoded = json.loads(run_process([cfg.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                                      "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(source)], cfg.media_timeout_seconds))["frames"]
    timestamps = [float(frame["best_effort_timestamp_time"]) for frame in decoded]
    if not timestamps or timestamps != sorted(timestamps):
        raise ValueError("Decoded frame timestamps are missing or unordered")
    retained = [t - timestamps[0] for t in timestamps]
    count = min(config["review"]["sample_frames"], len(retained))
    indices = sorted({round(i * (len(retained) - 1) / (count - 1)) for i in range(count)}) if count > 1 else [0]
    select = "+".join(f"eq(n\\,{i})" for i in indices)
    edge = config["review"]["sample_max_edge"]
    run_process([cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vf",
                 f"select={select},scale=w='min({edge},iw)':h='min({edge},ih)':force_original_aspect_ratio=decrease",
                 "-fps_mode", "vfr", "-frames:v", str(len(indices)), str(out / "frame-%03d.jpg")], cfg.media_timeout_seconds)
    samples = [{"frame_index": idx, "time_seconds": retained[idx], "path": str((out / f"frame-{n + 1:03d}.jpg").resolve())} for n, idx in enumerate(indices)]
    sheet = Image.new("RGB", (edge * 3, (edge + 30) * math.ceil(len(samples) / 3)), "#eeeeee")
    draw = ImageDraw.Draw(sheet)
    for i, sample in enumerate(samples):
        x, y = (i % 3) * edge, (i // 3) * (edge + 30)
        with Image.open(sample["path"]) as frame:
            sheet.paste(ImageOps.contain(frame.convert("RGB"), (edge, edge)), (x, y + 30))
        draw.text((x + 8, y + 8), f"{job_id}  t={sample['time_seconds']:.3f}s", fill="black")
    contact = out / "contact-sheet.jpg"
    sheet.save(contact, quality=90)
    audio = None
    if job["asset"]["technical"]["has_audio"]:
        audio = out / "audio.wav"
        run_process([cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vn",
                     "-map", "0:a:0", "-ac", "1", "-ar", "16000", str(audio)], cfg.media_timeout_seconds)
    packet = {"job_id": job_id, "source_sha256": job["asset"]["sha256"], "video": str(source.resolve()),
              "contact_sheet": str(contact.resolve()), "audio": str(audio.resolve()) if audio else None, "samples": samples,
              "retention_policy": "full_source", "requested_duration_ms": job["recipe"]["duration_ms"],
              "retained_video_duration_ms": job["asset"]["technical"]["duration_ms"],
              "coverage": "Uniform samples across the complete source video, including its first and actual last frame; full native audio extracted without a planned-duration cut. Audio findings are recorded only if heard in this same round."}
    save(packet_file, packet)
    return packet


def record_review(root, job_id, report):
    job, _ = video_asset(root, job_id)
    if report.get("outcome") not in {"pass", "uncertain", "serious"} or not report.get("notes") or not report.get("coverage"):
        raise ValueError("Review requires outcome, concrete notes and actual coverage")
    if report.get("source_sha256") != job["asset"]["sha256"]:
        raise ValueError("Review is not bound to the current video")
    result = {**report, "job_id": job_id}
    path = Path(root) / "reviews" / (job_id + ".json")
    if path.exists() and read(path) != result:
        raise ValueError("This candidate already has a review; do not silently replace its finding")
    save(path, result)
    return result


def usable_review(root, job_id):
    job, _ = video_asset(root, job_id)
    review = read(Path(root) / "reviews" / (job_id + ".json"))
    if review["source_sha256"] != job["asset"]["sha256"] or review["outcome"] not in {"pass", "uncertain"}:
        raise ValueError("Candidate has an unresolved serious issue or stale/missing review")
    return job, review


def tail(config, root, job_id):
    from video_harness.media import extract_timeline_tail, probe
    job, _ = usable_review(root, job_id)
    # Keep legacy trimmed tail files intact: existing downstream inputs bind their hashes.
    target = Path(root) / "assets" / (job_id + "-tail-full.png")
    source = Path(root) / job["asset"]["path"]
    cfg = technical(config)
    meta = probe(source, cfg, "video")
    extract_timeline_tail(source, target, cfg, None)
    return {"path": str(target.resolve()), "source_sha256": job["asset"]["sha256"],
            "requested_duration_ms": job["recipe"]["duration_ms"], "retained_duration_ms": meta["source_duration_ms"],
            "retained_video_duration_ms": meta["duration_ms"], "retention_policy": "full_source"}


def assemble(config, root, timeline):
    from video_harness.media import assemble_full_sources
    if not timeline or len({item["job_id"] for item in timeline}) != len(timeline):
        raise ValueError("Timeline must contain distinct, explicitly ordered candidates")
    sources, reviews, elapsed = [], [], 0
    for entry in timeline:
        job_id = identifier(entry["job_id"])
        job, review = usable_review(root, job_id)
        duration = job["recipe"]["duration_ms"]
        if config["models"]["video"]["task"] == "i2av" and not job["asset"]["technical"]["has_audio"]:
            raise ValueError("Native audio is missing from an audio-video candidate")
        if entry.get("duration_ms", duration) != duration:
            raise ValueError("Changing retained duration would invalidate tail references; replan explicitly")
        sources.append({"job_id": job_id, "asset": job["asset"], "requested_duration_ms": duration})
        reviews.append(review)
        elapsed += duration
    target = config["production"]["target_duration_ms"]
    if target is not None and abs(elapsed - target) > config["production"]["duration_tolerance_ms"]:
        raise ValueError("Timeline exceeds the agreed whole-film duration tolerance")
    result = assemble_full_sources(Path(root), sources, technical(config),
                                   max_duration_ms=config["production"].get("max_duration_ms"))
    result["planned_duration_ms"] = elapsed
    result["shot_reviews"] = reviews
    result["status"] = "assembled_with_recorded_review_limits"
    for entry in timeline:
        packet = Path(root) / "inspection" / entry["job_id"] / "packet.json"
        if packet.is_file() and read(packet).get("retention_policy") != "full_source":
            result["known_issues"].append(entry["job_id"] + ": reused legacy inspection coverage; restored tail was not additionally reviewed. Existing downstream tail bindings were not changed.")
    save(Path(root) / "exports/assembly.json", result)
    return result


def text_call(config, root, provider_name, instructions, data, images=(), *, transport=None):
    from video_harness.models import TextConfig
    from video_harness.text import TextAdapter
    from video_harness.quality import VisualReviewer
    pause_check(root)
    provider = config["providers"][provider_name]
    if provider["protocol"] != "chat_completions":
        raise ValueError("Text helper requires a Chat Completions compatible provider")
    settings = TextConfig.model_validate({k: v for k, v in provider.items() if k != "protocol"})
    cache = Path(root) / "text_requests" / provider_name
    class RunBudget:
        def _request(self, body, request_root, request_hash, **kwargs):
            if not (request_root / "response.json").is_file():
                all_cache = Path(root) / "text_requests"
                calls = list(all_cache.glob("*/*/request.json"))
                tokens = sum((read(p).get("usage") or {}).get("total_tokens", 0) for p in all_cache.glob("*/*/usage.json"))
                if len(calls) >= config["execution"]["max_text_calls"] or tokens >= config["execution"]["max_text_tokens"]:
                    raise ValueError("Run-wide external text/vision call or token limit reached")
            return super()._request(body, request_root, request_hash, **kwargs)
    class RunText(RunBudget, TextAdapter):
        pass
    class RunVision(RunBudget, VisualReviewer):
        pass
    if images:
        result, _, _ = RunVision(settings, transport).generate_visual(data, [(Path(p).name, Path(p)) for p in images],
                                                               instructions=instructions, cache_dir=cache,
                                                               total_call_limit=config["execution"]["max_text_calls"],
                                                               total_token_limit=config["execution"]["max_text_tokens"])
        return result
    return RunText(settings, transport).generate(instructions, data, cache_dir=cache)


def resolve_submission(config, root, job_id, task_id, confirmed_not_submitted, evidence):
    from video_harness.util import run_lock
    with run_lock(root):
        path = Path(root) / "jobs" / (identifier(job_id) + ".json")
        job = read(path)
        if job["status"] not in {"submitting", "submission_unknown"} or job["task_id"] or not evidence.strip():
            raise ValueError("Only an ambiguous submission with concrete resolution evidence can be resolved")
        if confirmed_not_submitted:
            job.update(status="confirmed_not_submitted", reserved_credits=0)
        else:
            client = make_client(config, job["recipe"]["provider"])
            try:
                result = client.query(task_id)
            finally:
                client.close()
            if result.get("model_cls") and result["model_cls"] != job["recipe"]["model"]["model_cls"]:
                raise ValueError("Task model differs from the saved submission")
            remote_prompt = result.get("params", {}).get("prompt")
            if remote_prompt and remote_prompt != job["recipe"]["prompt"]:
                raise ValueError("Task prompt differs from the saved submission")
            job.update(task_id=task_id, status="submitted")
        job["resolution_evidence"] = evidence
        save(path, job)
        return job


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    create = sub.add_parser("init")
    create.add_argument("--output", type=Path, required=True)
    specs = sub.add_parser("specs")
    specs.add_argument("--role", choices=["image", "edit", "video"], required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--request", type=Path, required=True)
    submit.add_argument("--execute", action="store_true")
    for name in ("poll", "inspect", "tail", "review", "resolve"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--job", required=True)
        if name == "review":
            cmd.add_argument("--report", type=Path, required=True)
        if name == "resolve":
            choices = cmd.add_mutually_exclusive_group(required=True)
            choices.add_argument("--task-id")
            choices.add_argument("--confirmed-not-submitted", action="store_true")
            cmd.add_argument("--evidence", required=True)
    rewriting = sub.add_parser("rewrite")
    rewriting.add_argument("--prompt-file", type=Path, required=True)
    rewriting.add_argument("--duration-ms", type=int, required=True)
    rewriting.add_argument("--first-frame", type=Path, required=True)
    rewriting.add_argument("--last-frame", type=Path)
    assembling = sub.add_parser("assemble")
    assembling.add_argument("--timeline", type=Path, required=True)
    text = sub.add_parser("text")
    text.add_argument("--provider", default="text")
    text.add_argument("--instructions", type=Path, required=True)
    text.add_argument("--input", type=Path, required=True)
    text.add_argument("--images", nargs="*", default=[])
    text.add_argument("--output", type=Path, required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command not in {"doctor", "init"} and args.run is None:
        parser.error("--run is required for project operations")
    settings_path = args.config or (args.run / "settings.json" if args.run else DEFAULT_CONFIG)
    config = load_settings(settings_path)
    with credential_environment(config):
        return dispatch(config, args)


def dispatch(config, args):
    if args.command == "doctor":
        import shutil
        return {"runtime": config["runtime"], "ffmpeg": shutil.which(config["media"]["ffmpeg"]),
                "ffprobe": shutil.which(config["media"]["ffprobe"]),
                "credentials_file": config.get("credentials_file"),
                "credential_present": {name: bool(os.environ.get(p["key_env"])) for name, p in config["providers"].items()},
                "local_rewriter_files_present": {k: Path(config["rewriter"]["local"][k]).exists() for k in ("python", "script", "model_root")}}
    if args.command == "init":
        return init_run(config, args.output)
    root = args.run.resolve()
    if args.command == "specs":
        provider = config["providers"][config["models"][args.role]["provider"]]
        client = make_client(config, provider)
        try:
            _, contract = get_contract(config, root, args.role, client)
            return contract.spec
        finally:
            client.close()
    if args.command == "submit":
        return submit_job(config, root, read(args.request), args.request.resolve().parent, execute=args.execute)
    if args.command == "poll":
        return poll_job(config, root, args.job)
    if args.command == "rewrite":
        return rewrite(config, root, args.prompt_file, args.duration_ms, args.first_frame, args.last_frame)
    if args.command == "inspect":
        return inspect_video(config, root, args.job)
    if args.command == "review":
        return record_review(root, args.job, read(args.report))
    if args.command == "tail":
        return tail(config, root, args.job)
    if args.command == "assemble":
        return assemble(config, root, read(args.timeline))
    if args.command == "text":
        result = text_call(config, root, args.provider, args.instructions.read_text(encoding="utf-8-sig"), args.input.read_text(encoding="utf-8-sig"), args.images)
        save(args.output, result)
        return {"output": str(args.output.resolve())}
    if args.command == "resolve":
        return resolve_submission(config, root, args.job, args.task_id, args.confirmed_not_submitted, args.evidence)
    jobs = [read(p) for p in sorted((root / "jobs").glob("*.json"))]
    return {"jobs": [{"job_id": j["recipe"]["job_id"], "status": j["status"], "task_id": j["task_id"], "elapsed_seconds": j.get("elapsed_seconds")} for j in jobs],
            "concurrency": concurrency_status(config, jobs),
            "reserved_credits": sum(j.get("reserved_credits", 0) for j in jobs)}


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        started = time.time()
        result = main()
        result["operation_elapsed_seconds"] = round(time.time() - started, 3)
        metadata_parser = argparse.ArgumentParser(add_help=False)
        metadata_parser.add_argument("--run", type=Path)
        metadata_parser.add_argument("--config")
        metadata, rest = metadata_parser.parse_known_args()
        if metadata.run and (metadata.run / "settings.json").is_file():
            save(metadata.run / "metrics" / f"{time.time_ns()}-{os.getpid()}.json",
                 {"operation": rest[0] if rest else "unknown", "started_at": started,
                  "elapsed_seconds": result["operation_elapsed_seconds"], "status": result.get("status", "completed")})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        # Errors may contain service internals; never print HTTP request bodies or secrets.
        message = str(exc) if type(exc) is ValueError else type(exc).__name__
        try:
            from video_harness.util import HarnessError, safe_error
            message = safe_error(exc) if isinstance(exc, HarnessError) else safe_error(HarnessError(message))
        except ImportError:
            pass
        print(json.dumps({"status": "needs_attention", "error": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
