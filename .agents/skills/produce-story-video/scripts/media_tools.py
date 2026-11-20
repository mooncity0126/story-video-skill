"""Atomic media operations. The agent, not this module, chooses the workflow."""
from __future__ import annotations

import argparse
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
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


def dependency_kind(item):
    kind = item.get("kind", item.get("type"))
    if kind in {"actual_tail", "accepted_actual_tail", "edited_actual_tail", "tail"}:
        return "tail"
    return "frame" if kind in {"shot_frame", "frame"} else "reference"


def shot_dependencies(root, board=None):
    """Compile the skill's run records with the original harness continuity planner."""
    from video_harness.models import GenerationPlan, ShotObject
    from video_harness.planning import shot_dependencies as compile_graph
    from video_harness.util import HarnessError
    root = Path(root)
    if board is None:
        board = read(root / "storyboard.json") if (root / "storyboard.json").is_file() else {}
    shots = board.get("shots", [])
    if not shots:
        return {}
    items = {identifier(s["shot_id"]): s for s in shots}
    order = board.get("playback_order") or [s["shot_id"] for s in sorted(
        shots, key=lambda s: s.get("timeline_start_ms", s.get("timeline_in_ms", 0)))]
    if len(order) != len(shots) or len(set(order)) != len(order) or set(order) != set(items):
        raise ValueError("分镜播放顺序含缺失或重复的镜头。")
    path = root / "dependencies.json"
    saved = read(path) if path.is_file() else []
    rows = saved.get("shots", []) if isinstance(saved, dict) else saved
    # Older re-edits keep rows for removed shots. Only the current playback timeline is scheduled.
    rows = [row for row in rows if row.get("shot_id") in items]
    declared = {identifier(row["shot_id"]): row for row in rows}
    if len(rows) != len(declared):
        raise ValueError("镜间依赖含有重复的在用分镜。")
    planned, storyboard_plans = [], []
    for i, sid in enumerate(order):
        shot, row = items[sid], declared.get(sid, {})
        previous = row.get("requires", [])
        refs = [{"shot_id": v["source_id"], "kind": dependency_kind(v), "reason": v.get("reason") or "已记录的镜间依赖"}
                for v in previous if dependency_kind(v) != "reference"]
        if any(ref["shot_id"] not in order[:i] for ref in refs):
            raise ValueError(f"{sid} 的依赖必须指向播放顺序中更早的镜头。")
        authored = shot.get("generation_plan")
        if row:
            plan = {"relation": row.get("relation", "uncertain"),
                    "confidence": row.get("confidence", "high" if row.get("reason") or previous else "low"),
                    "reason": row.get("reason") or "沿用项目已记录的镜间关系", "references": refs}
        else:
            plan = authored
        if any(ref.get("shot_id") not in order[:i] for ref in (authored or {}).get("references", [])):
            raise ValueError(f"{sid} 的分镜引用必须指向播放顺序中更早的镜头。")
        objects = [ShotObject.model_validate(v) for v in shot.get("objects", []) if isinstance(v, dict) and "start_state_id" in v]
        attributes = dict(shot_id=sid, timeline_in_ms=i, scene_id=shot.get("scene_id") or shot.get("scene_unit_id"),
                          transition_from_previous=shot.get("transition_from_previous"), objects=objects)
        parsed = GenerationPlan.model_validate(plan) if plan else None
        planned.append(SimpleNamespace(**attributes, generation_plan=parsed))
        storyboard_plans.append(SimpleNamespace(**attributes, generation_plan=
            GenerationPlan.model_validate(authored) if row and authored else parsed))
    try:
        compiled = compile_graph(SimpleNamespace(shots=planned))
        authored_graph = compile_graph(SimpleNamespace(shots=storyboard_plans)) if planned != storyboard_plans else compiled
    except HarnessError as exc:
        raise ValueError(str(exc)) from exc
    snapshot = root / "settings.json"
    serial = snapshot.is_file() and read(snapshot).get("production", {}).get("scheduling") == "serial"
    result = {}
    for index, sid in enumerate(order):
        decision = compiled[sid]
        requires = list(declared.get(sid, {}).get("requires", []))
        other = authored_graph[sid]
        authored_refs = (items[sid].get("generation_plan") or {}).get("references", [])
        for ref in [*decision["references"], *other["references"], *authored_refs]:
            if not any(dependency_kind(v) == ref["kind"] and v.get("source_id") == ref["shot_id"] for v in requires):
                requires.append({"kind": "actual_tail" if ref["kind"] == "tail" else "shot_frame",
                                 "source_id": ref["shot_id"], "reason": ref["reason"]})
        if serial and index:
            predecessor = order[index - 1]
            if not any(dependency_kind(v) == "tail" and v.get("source_id") == predecessor for v in requires):
                requires.append({"kind": "actual_tail", "source_id": predecessor, "reason": "逐镜串行，承接播放顺序前镜的采用视频实际尾帧"})
        relation = "continuous" if any(dependency_kind(v) == "tail" for v in requires) else decision["relation"]
        if relation in {"uncertain", "continuous"} and not requires:
            relation = "independent"
        reason = decision["reason"]
        if decision["conservative_fallback"] or other["conservative_fallback"]:
            reason = "依赖未确定，保守承接前镜" if requires else "首镜从设定参考起步"
        elif other["reason"] != reason and other["references"] != decision["references"]:
            reason += "；分镜另有承接：" + other["reason"]
        if serial and index:
            reason = f"逐镜串行，等待 {order[index - 1]} 的采用视频与实际尾帧"
        result[sid] = {"shot_id": sid, "relation": relation, "requires": requires, "reason": reason}
    return result


def selected_video(request_id, accepted_id, jobs):
    if not request_id:
        return accepted_id
    edit = jobs.get(accepted_id, {}).get("local_edit", {})
    source = jobs.get(request_id, {}).get("asset", {})
    if source and edit.get("source_job_id") == request_id and edit.get("source_sha256") == source.get("sha256"):
        return accepted_id
    return request_id


def current_videos(root, jobs=None, requests=None):
    root = Path(root)
    if jobs is None:
        jobs = [read(p) for p in (root / "jobs").glob("*.json")]
    by_id = {j["recipe"]["job_id"]: j for j in jobs}
    latest = {j["recipe"]["shot_id"]: j["recipe"]["job_id"] for j in sorted(jobs, key=lambda j: j.get("submitted_at") or j.get("created_at") or 0)
              if j["recipe"]["role"] == "video" and not j.get("local_edit")}
    if requests is None:
        requests = {}
        for p in sorted((root / "requests").glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name)):
            req = read(p)
            if req.get("role") == "video":
                requests[req["shot_id"]] = (p, req)
    path = root / "queue.json"
    accepted = read(path).get("accepted", {}) if path.is_file() else {}
    selected = {sid: selected_video(requests[sid][1]["job_id"] if sid in requests else None,
                                    accepted.get(sid) or latest.get(sid), by_id)
                for sid in latest.keys() | requests.keys() | accepted.keys()}
    return by_id, selected


def dependency_sources(config, root, row, by_id, selected, *, extract=False, graph=None, verified=None):
    """Resolve the actual visual sources. Read-only callers never create a tail."""
    root = Path(root)
    sources = []
    verified = set() if verified is None else verified
    for req in row["requires"]:
        kind, sid = dependency_kind(req), req.get("source_id")
        binding = req
        if kind == "tail":
            jid = selected.get(sid)
            job = by_id.get(jid, {})
            if job.get("status") != "succeeded" or job["recipe"].get("shot_id") != sid or not job.get("asset", {}).get("sha256"):
                raise ValueError(f"等待 {sid} 的采用视频和实际尾帧")
            asset = job["asset"]
            review = root / "reviews" / (jid + ".json")
            report = read(review) if review.is_file() else {}
            if report.get("outcome") not in {"pass", "uncertain"} or report.get("source_sha256") != asset["sha256"]:
                raise ValueError(f"等待 {sid} 当前候选完成内容检查")
            if jid not in verified:
                video = root / asset.get("path", "")
                if not video.is_file() or sha(video) != asset["sha256"]:
                    raise ValueError(f"{sid} 的来源视频缺失或内容已变化")
                if graph and graph[sid]["requires"]:
                    require_continuity(config, root, job["recipe"], graph=graph, ledger=(by_id, selected), verified=verified)
                verified.add(jid)
            pin = dict(req.get("derived_from") or req)
            if "source_video_job_id" in pin:
                pin.setdefault("source_job_id", pin["source_video_job_id"])
            if "source_video_sha256" in pin:
                pin.setdefault("source_sha256", pin["source_video_sha256"])
            if any(pin.get(key) not in {None, expected} for key, expected in (
                    ("source_job_id", jid), ("source_sha256", asset["sha256"]))):
                raise ValueError(f"{sid} 的采用候选已变更，请更新下游素材")
            # Keep recorded full-length legacy tails readable. New runs create this sidecar when extracting.
            cached = root / "assets" / (jid + "-tail-full.json")
            if cached.is_file():
                binding = read(cached)
                if (binding.get("source_sha256") != asset["sha256"] or binding.get("source_job_id") != jid
                        or (root / binding.get("path", "")).resolve() != cached.with_suffix(".png").resolve()):
                    raise ValueError(f"{sid} 的实际尾帧来源已变更")
            elif pin.get("path") and (root / pin["path"]).resolve() in {
                    cached.with_suffix(".png").resolve(), (root / "assets" / (jid + "-tail.png")).resolve()
            } and pin.get("source_sha256") == asset["sha256"] and (
                    pin.get("retention_policy") == "full_source" or
                    pin.get("retained_duration_ms") == asset.get("technical", {}).get("source_duration_ms", -1)):
                binding = pin
            elif extract:
                binding = tail(config, root, jid)
            else:
                raise ValueError(f"等待提取 {sid} 经检查的完整视频末帧")
            if pin.get("sha256") and pin["sha256"] != binding.get("sha256"):
                raise ValueError(f"{sid} 的实际尾帧已变化，请更新下游素材")
            binding = {**binding, "kind": "actual_tail", "source_id": sid,
                       "source_job_id": jid, "source_sha256": asset["sha256"]}
        elif kind == "frame":
            frame = root / "frames" / (identifier(sid) + ".json")
            first = read(frame).get("first") if frame.is_file() else None
            if not first or (req.get("sha256") and req["sha256"] != first.get("sha256")):
                raise ValueError(f"等待 {sid} 的当前首帧")
            binding = {**first, "kind": "shot_frame", "source_id": sid}
            if graph and graph[sid]["requires"]:
                require_continuity(config, root, {"role": "edit", "shot_id": sid, "input_hashes": {
                    config["models"]["edit"]["media_field"]: [first["sha256"]]}},
                    graph=graph, ledger=(by_id, selected), verified=verified)
        elif not req.get("path"):
            index = root / "reference-index.json"
            ref = read(index).get(sid) if index.is_file() else None
            if not ref or (req.get("sha256") and req["sha256"] != ref.get("sha256")):
                raise ValueError(f"等待设定参考 {sid}")
            binding = {**ref, "kind": "reference_image", "source_id": sid}
        path = root / binding.get("path", "")
        if not path.is_file() or not binding.get("sha256") or sha(path) != binding["sha256"]:
            raise ValueError(f"{sid} 的参考文件缺失或内容已变更")
        sources.append({**binding, "path": str(path.resolve())})
    return sources


def require_continuity(config, root, recipe, *, graph=None, ledger=None, verified=None):
    """The CLI and workbench share the same gate on new shot-level requests."""
    sid = recipe.get("shot_id")
    serial = config["production"].get("scheduling") == "serial"
    if not sid or (recipe["role"] not in {"edit", "video"} and not (serial and recipe["role"] == "image")):
        return
    root = Path(root)
    graph = shot_dependencies(root) if graph is None else graph
    if not graph and not (root / "storyboard.json").is_file():
        if serial:
            raise ValueError("逐镜串行需要先保存分镜和播放顺序。")
        return  # Standalone media operations have no story timeline.
    if sid not in graph:
        raise ValueError(f"{sid} 不在已规划的分镜中")
    row = graph[sid]
    if serial and recipe["role"] == "image" and any(dependency_kind(item) == "tail" for item in row["requires"]):
        raise ValueError(f"{sid} 的串行首帧必须使用采用视频的实际尾帧，不能独立文生图。")
    field = config["models"][recipe["role"]].get("media_field") or "input_image"
    inputs = set(recipe.get("input_hashes", {}).get(field, []))
    first_file = root / "frames" / (identifier(sid) + ".json")
    frames = read(first_file) if first_file.is_file() else {}
    first = frames.get("first") or {}
    managed = root / "workbench-assets.json"
    managed_video = recipe["role"] == "video" and managed.is_file() and read(managed).get("version") == 2
    if managed_video:
        if not first.get("sha256") or inputs != {first["sha256"]}:
            raise ValueError(f"{sid} 的视频必须使用本镜已准备的实际首帧")
        target = frames.get("target_end") or {}
        if recipe["input_hashes"].get("input_last_frame", []) != ([target["sha256"]] if target else []):
            raise ValueError(f"{sid} 的目标尾帧与选定素材不一致")
    base_refs = [v for v in first.get("sources", []) if v.get("kind") in {"scene_reference", "cast_reference"}]
    if not row["requires"] and not (base_refs and first.get("sha256") in inputs):
        return
    by_id, selected = ledger if ledger is not None else current_videos(root)
    sources = dependency_sources(config, root, row, by_id, selected, graph=graph, verified=verified)
    required = {s["sha256"] for s in sources}
    if first.get("sha256") in inputs:
        for ref in base_refs:
            path = root / ref["path"]
            if not path.is_file() or sha(path) != ref["sha256"]:
                raise ValueError(f"{sid} 的人物或场景设定图有变，请重新准备首帧")
            required.add(ref["sha256"])
        lineage = [first, first.get("derived_from") or {}, *first.get("sources", [])]
        for parent in (s for s in sources if dependency_kind(s) == "tail"):
            if any(dependency_kind(v) == "tail" and v.get("source_id", parent["source_id"]) == parent["source_id"]
                   and v.get("source_job_id") and v["source_job_id"] != parent["source_job_id"]
                   for v in lineage if v.get("kind") != "edited_actual_tail"):
                raise ValueError(f"{sid} 使用了已被替换的前镜候选")
    # Some historical requests bind the approved reference/actual tail directly.
    if inputs and required <= inputs:
        return
    if first.get("sha256") not in inputs:
        raise ValueError(f"{sid} 的首帧尚未绑定所需的来源图片")
    edit = by_id.get(first.get("source_job_id"), {})
    if (edit.get("status") != "succeeded" or edit.get("recipe", {}).get("role") != "edit"
            or edit.get("asset", {}).get("sha256") != first["sha256"]):
        raise ValueError(f"{sid} 缺少由所需参考生成首帧的编辑任务")
    hashes = edit["recipe"].get("input_hashes", {}).get(config["models"]["edit"]["media_field"], [])
    if not required <= set(hashes):
        raise ValueError(f"{sid} 的首帧未使用当前依赖图片，请重新准备该镜")


class SetupRequired(ValueError):
    pass


def prompt_mode(config):
    return config["rewriter"].get("mode", "local" if config["rewriter"]["enabled"] else "direct")


def setup_options(config):
    video = config["models"]["video"]
    provider = config["providers"][video["provider"]]
    fields = read(DEFAULT_CONFIG.with_name("setup-options.json"))
    api_fields = [
        ("api_url", "视频 API 地址", provider["base_url"], "首帧、所选目标尾帧和正文将发往此地址。支持 LightX2V Cloud 协议和原生音画图生视频。"),
        ("model", "视频模型", video["model_cls"], "填写网关规格中的模型标识。模型会影响画质、声音、可用时长与积分报价。"),
        ("stage", "模型阶段", video["stage"], "与网关公布的阶段标识一致。工作流会在提交前核对规格。"),
        ("key_name", "本地密钥名称", provider["key_env"], "填写配置文件里的名称，例如 X2V_API_KEY。密钥值留在本机密钥文件中。"),
    ]
    fields[1:1] = [dict(id=key, label=label, impact=impact, group="api") for key, label, _, impact in api_fields]
    review = config["review"]
    defaults = {key: value for key, _, value, _ in api_fields}
    defaults.update(scheduling="serial", prompt_mode=prompt_mode(config), writer="agent",
                    frame_check=review.get("frame_check", "on_demand"), prompt_check=review.get("prompt_check", "on_demand"),
                    review_mode=review["mode"], repairs=str(review["max_content_repairs_per_shot"]))
    return fields, defaults


def profile_settings(template, values):
    fields, defaults = setup_options(template)
    if not isinstance(values, dict) or set(values) != set(defaults):
        raise ValueError("请完整填写工作流设置，并仅提交表单中列出的选项。")
    for field in fields:
        value = values[field["id"]]
        if not isinstance(value, str) or not value.strip() or len(value) > 512 or value != value.strip():
            raise ValueError("请检查" + field["label"] + "的填写内容。")
        if "choices" in field and value not in {item["value"] for item in field["choices"]}:
            raise ValueError("请选择有效的" + field["label"] + "。")
    try:
        endpoint = urlparse(values["api_url"])
        endpoint.port  # Validate the port now, before saving an unusable address.
    except ValueError:
        raise ValueError("视频 API 地址或端口格式无效。") from None
    if (any(c.isspace() for c in values["api_url"]) or endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None or
            endpoint.password is not None or endpoint.query or endpoint.fragment or any(c.isspace() or c == chr(92) for c in endpoint.netloc)):
        raise ValueError("视频 API 需要有效的 HTTPS 地址，地址中不能包含凭据、查询或片段。")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", values["key_name"]):
        raise ValueError("请填写密钥名称，例如 X2V_API_KEY。")
    for name in ("model", "stage"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", values[name]):
            raise ValueError("模型或阶段标识格式有误。")
    cfg = deepcopy(template)
    video = cfg["models"]["video"]
    original_model = video["model_cls"]
    api = cfg["providers"][video["provider"]]
    key = values["key_name"]
    for other in cfg["providers"].values():
        origin = urlparse(other.get("base_url") or other["endpoint"])
        if key == other["key_env"] and endpoint.netloc.lower() != origin.netloc.lower():
            raise ValueError("新服务地址请使用专属密钥名称，以便凭据只发往对应的服务。")
    url = values["api_url"].rstrip("/")
    if (url, key) != (api["base_url"].rstrip("/"), api["key_env"]):
        known_keys = {v["key_env"] for v in cfg["providers"].values()}
        video["provider"] = "workflow_video"
        custom = {"protocol": "lightx2v_cloud", "base_url": url, "key_env": key}
        if key not in known_keys:
            directory = Path(cfg.get("credentials_file") or cfg["workflow_preferences_file"]).parent
            custom["credentials_file"] = str(directory / (key + ".local.json"))
        cfg["providers"][video["provider"]] = custom
    video.update(model_cls=values["model"], stage=values["stage"], task="i2av")
    if original_model != values["model"]:
        video["parameters"] = {}
    mode = values["prompt_mode"]
    cfg["rewriter"].update(mode=mode, enabled=mode == "local")
    if mode == "local":
        if values["model"] not in cfg["rewriter"].get("compatible_models", []):
            raise ValueError("此视频模型没有本地 Prompt Rewriter 适配器。")
        for field in ("python", "script", "model_root"):
            value = cfg["rewriter"].get("local", {}).get(field)
            if not value or not (Path(value).is_dir() if field == "model_root" else Path(value).is_file()):
                raise ValueError("本地改写缺少 " + field + "。请先在运行配置中填写已安装的路径。")
    # H3 defaults to enhancement at the gateway. Always send the explicit selection there.
    if mode == "online" or values["model"] == "MiniMax-H3" or "enhance_prompt" in video["parameters"]:
        video["parameters"]["enhance_prompt"] = mode == "online"
    cfg["production"].update(scheduling=values["scheduling"], writer=values["writer"])
    cfg["review"].update(require_material_approval=True, mode=values["review_mode"],
                         frame_check=values["frame_check"], prompt_check=values["prompt_check"],
                         max_content_repairs_per_shot=int(values["repairs"]))
    for selected, provider_name in [(values["writer"] == "text_api", "text"),
                                    (values["review_mode"] == "vision_api", cfg["review"].get("vision_provider"))]:
        if selected and cfg["providers"].get(provider_name, {}).get("protocol") != "chat_completions":
            raise ValueError("请先在运行配置中配置对应的外部文字或视觉 API。")
    cfg["execution"]["budget_credits"] = None
    cfg.pop("workflow_preferences_file", None)
    return cfg


def setup_status(config):
    fields, defaults = setup_options(config)
    path = Path(config["workflow_preferences_file"])
    status = {"configured": False, "values": defaults, "fields": fields}
    if path.exists():
        try:
            item = read(path)
            if not isinstance(item, dict) or item.get("version") != 1:
                raise ValueError("工作流设置版本无法识别。")
            profile_settings(config, item.get("values"))
            status.update(configured=True, values=item["values"])
        except (OSError, ValueError) as exc:
            status["error"] = "请重新确认已保存的工作流设置。" + str(exc)
    return status


def save_setup(config, values):
    profile_settings(config, values)
    path = Path(config["workflow_preferences_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump({"version": 1, "values": values}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"configured": True, "values": values}


def load_settings(path):
    path = Path(path).resolve()
    data = read(path)
    if "workflow_preferences_file" in data:
        data["workflow_preferences_file"] = resolve_path(data["workflow_preferences_file"], path.parent)
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
    rewriter = data["rewriter"]
    if type(rewriter["enabled"]) is not bool:
        raise ValueError("rewriter.enabled must be a boolean")
    for key in ("python", "script", "model_root"):
        value = rewriter.get("local", {}).get(key)
        if value:
            rewriter["local"][key] = resolve_path(value, path.parent)
        elif rewriter["enabled"]:
            raise ValueError("Local rewriting requires rewriter.local." + key)
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
    if type(data["review"].get("require_material_approval", True)) is not bool:
        raise ValueError("review.require_material_approval must be a boolean")
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
    video = data["models"]["video"]
    mode = prompt_mode(data)
    if mode not in {"direct", "local", "online"} or (mode == "local") != rewriter["enabled"]:
        raise ValueError("Inconsistent prompt mode and local rewriter settings")
    if data["production"].get("scheduling", "dependency_parallel") not in {"serial", "dependency_parallel"}:
        raise ValueError("Invalid scheduling mode")
    if mode == "online" and video["parameters"].get("enhance_prompt") is not True:
        raise ValueError("Online prompt optimization requires enhance_prompt=true")
    if mode != "online" and (video["model_cls"] == "MiniMax-H3" or rewriter["enabled"] or "mode" in rewriter and "enhance_prompt" in video["parameters"]):
        if video["parameters"].get("enhance_prompt") is not False:
            raise ValueError("Authored H3 or locally rewritten prompts require enhance_prompt=false")
    if data["rewriter"]["enabled"]:
        if video["model_cls"] not in data["rewriter"]["compatible_models"]:
            raise ValueError("The selected video model has no configured local rewriter adapter")
    return data


@contextmanager
def credential_environment(config):
    """Expose local keys only during this command; never change Windows settings."""
    path = config.get("credentials_file")
    def read_keys(file, allowed):
        try:
            entries = read(file)
        except (OSError, ValueError):
            raise ValueError("Cannot read credentials_file; check that it exists and contains valid UTF-8 JSON") from None
        if not isinstance(entries, dict) or set(entries) - allowed:
            raise ValueError("credentials_file must be an object containing only the configured credential names")
        return entries

    providers = config["providers"].values()
    values = read_keys(path, {p["key_env"] for p in providers if not p.get("credentials_file")}) if path else {}
    for provider in providers:
        separate = provider.get("credentials_file")
        if separate and Path(separate).is_file():
            values.update(read_keys(separate, {provider["key_env"]}))
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
        "video": 1 if production.get("scheduling") == "serial" else min(total, production.get("maximum_parallel_video_jobs", total)),
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
    if config.get("workflow_preferences_file"):
        status = setup_status(config)
        if not status["configured"]:
            raise SetupRequired(status.get("error") or "创建新项目之前，请先在工作台「工作流设置」或通过 setup 确认首次设置。")
        config = profile_settings(config, status["values"])
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
    contract = Contract(cached["spec"], cached["openapi"])
    if role == "video" and config["rewriter"].get("mode"):
        mode = prompt_mode(config)
        enhancement = contract.params.get("enhance_prompt")
        if mode == "online" and (enhancement or {}).get("type") != "boolean":
            raise ValueError("当前模型接口没有可开启的在线提示词优化。请更换模型或提示词方式。")
        if choice.task != "i2av" or not contract.inputs.get("input_image"):
            raise ValueError("本片的视频模型需要支持首帧图生视频和原生声音。")
        if enhancement and "enhance_prompt" not in choice.parameters:
            choice = choice.model_copy(update={"parameters": {**choice.parameters, "enhance_prompt": mode == "online"}})
    return choice, contract


def request_data(config, request, base):
    allowed = {"job_id", "role", "shot_id", "prompt", "prompt_file", "duration_ms", "inputs", "rewriter_record", "rewriter_source_file", "repair_of", "retry_of"}
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
        if not config["rewriter"]["enabled"] and (request.get("rewriter_record") or request.get("rewriter_source_file")):
            raise ValueError("Direct prompt mode uses prompt or prompt_file, not rewriter_record")
        if config["rewriter"]["enabled"]:
            if not request.get("rewriter_record"):
                raise ValueError("本镜需要绑定本地改写记录。")
            record_path = resolve_path(request["rewriter_record"], base)
            record = read(record_path)
            original = read(Path(record_path).parent / "input.json")
            if request.get("rewriter_source_file"):
                source = Path(resolve_path(request["rewriter_source_file"], base)).read_text(encoding="utf-8-sig")
                if source != original.get("source_prompt"):
                    raise ValueError("本地改写的源提示词已变化，请按当前素材重新改写。")
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
        if execute and recipe["role"] == "video":
            require_material_approval(config, root, recipe)
        if execute:
            require_continuity(config, root, recipe)
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
            repaired = {j["recipe"]["repair_of"] for j in same_shot if j["recipe"].get("repair_of")}
            if parent_id in repaired:
                raise ValueError("Shot content repair limit reached for this candidate; review its replacement")
            if len(repaired) >= config["review"]["max_content_repairs_per_shot"]:
                auth_file = root / "budget-authorization.json"
                auth = read(auth_file) if auth_file.is_file() else {}
                expanded = (auth.get("status") == "authorized" and auth.get("run_id") == root.name
                            and auth.get("allow_additional_content_repairs") is True
                            and auth.get("budget_credits") is not None
                            and auth["budget_credits"] == config["execution"]["budget_credits"])
                if not expanded:
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


def material_fingerprint(config, recipe):
    """Bind the user's decision to the actual video payload, not a checkbox or filename."""
    return fingerprint({"version": 1, "shot_id": recipe["shot_id"], "prompt": recipe["prompt"],
                        "input_hashes": recipe["input_hashes"], "duration_ms": recipe["duration_ms"],
                        "aspect_ratio": config["production"]["aspect_ratio"], "model": recipe["model"],
                        "provider": {key: recipe["provider"].get(key) for key in ("protocol", "base_url")}})


def material_review_status(config, root, recipe):
    current = material_fingerprint(config, recipe)
    path = Path(root) / "material-approvals.json"
    records = read(path) if path.is_file() else {"shots": {}}
    record = records.get("shots", {}).get(recipe["shot_id"])
    state = "pending"
    if record:
        if record.get("fingerprint") != current:
            state = "outdated"
        elif record.get("actor") == "user" and record.get("decision") in {"approved", "changes_requested"}:
            state = record["decision"]
    return {"state": state, "fingerprint": current, "note": (record or {}).get("note", ""),
            "reviewed_at": (record or {}).get("reviewed_at")}


def planned_prechecks(config, root, recipe, stages=("frame", "prompt")):
    if recipe["role"] != "video" or "scheduling" not in config["production"]:
        return {}  # Legacy productions keep their recorded check protocol.
    board = Path(root) / "storyboard.json"
    shots = read(board).get("shots", []) if board.is_file() else []
    shot = next((item for item in shots if item["shot_id"] == recipe["shot_id"]), {})
    planned = shot.get("checks") or {}
    evidence = {}
    images = recipe["input_hashes"]
    for stage in stages:
        field = stage + "_check"
        mode = config["review"].get(field, "on_demand")
        selection = planned.get(stage, planned.get(field, {}))
        enabled = selection.get("enabled") if isinstance(selection, dict) else selection
        if mode != "always" and not (mode == "on_demand" and enabled is True):
            continue
        hashes = {"first": next(iter(images.get("input_image", [])), None),
                  "target_end": next(iter(images.get("input_last_frame", [])), None)}
        if stage == "prompt":
            hashes["prompt"] = hashlib.sha256(recipe["prompt"].encode("utf-8")).hexdigest()
            hashes["material"] = material_fingerprint(config, recipe)
        evidence[stage] = hashes
    return evidence


def frame_check_recipe(root, sid):
    root = Path(root)
    info = read(root / "frames" / (identifier(sid) + ".json"))
    hashes = {}
    for name, field in (("first", "input_image"), ("target_end", "input_last_frame")):
        frame = info.get(name)
        if not frame:
            if name == "first":
                raise ValueError(f"{sid} 等待实际首帧。")
            continue
        path = root / frame["path"]
        if not path.is_file() or sha(path) != frame.get("sha256"):
            raise ValueError(f"{sid} 的检查用图像缺失或已变更。")
        hashes[field] = [frame["sha256"]]
    return {"shot_id": sid, "role": "video", "input_hashes": hashes}


def require_prechecks(config, root, recipe, stages=("frame", "prompt")):
    labels = {"frame": "首尾帧", "prompt": "提示词一致性"}
    for stage, hashes in planned_prechecks(config, root, recipe, stages).items():
        path = Path(root) / "checks" / (identifier(recipe["shot_id"]) + "-" + stage + ".json")
        record = read(path) if path.is_file() else {}
        if (record.get("enabled") is not True or record.get("round") != 1 or
                record.get("outcome") not in {"pass", "uncertain"} or record.get("source_hashes") != hashes):
            raise ValueError(f'{recipe["shot_id"]} 等待当前素材的{labels[stage]}检查结论。')


def require_material_approval(config, root, recipe):
    require_prechecks(config, root, recipe)
    inbox = Path(root) / "workflow-inbox.json"
    if inbox.is_file() and read(inbox).get("status") == "pending_planning":
        raise ValueError("Saved production inputs await planning; apply the user's current brief before video submission")
    if not config["review"].get("require_material_approval", True):
        return
    status = material_review_status(config, root, recipe)
    if status["state"] != "approved":
        raise ValueError("Material review required before video submission: " + recipe["shot_id"]
                         + " (" + status["state"] + "). Ask the user to review this version in the local workbench.")


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
    sidecar = target.with_suffix(".json")
    if sidecar.is_file() and target.is_file():
        cached = read(sidecar)
        if (cached.get("source_job_id") == job_id and cached.get("source_sha256") == job["asset"]["sha256"]
                and (Path(root) / cached.get("path", "")).resolve() == target.resolve()
                and cached.get("sha256") == sha(target)):
            return cached
    source = Path(root) / job["asset"]["path"]
    cfg = technical(config)
    meta = probe(source, cfg, "video")
    with TemporaryDirectory(dir=target.parent) as temp_dir:
        decoded = Path(temp_dir) / "tail.png"
        extract_timeline_tail(source, decoded, cfg, None)
        binding = {"kind": "actual_tail", "source_id": job["recipe"]["shot_id"], "source_job_id": job_id,
                   "path": str(target.resolve()), "sha256": sha(decoded), "source_sha256": job["asset"]["sha256"],
                   "requested_duration_ms": job["recipe"]["duration_ms"], "retained_duration_ms": meta["source_duration_ms"],
                   "retained_video_duration_ms": meta["duration_ms"], "retention_policy": "full_source"}
        metadata = decoded.with_suffix(".json")
        save(metadata, binding)
        os.replace(decoded, target)
        os.replace(metadata, sidecar)
    return binding


def black_title_source(root, title, cfg):
    """Render the storyboard's local end card as its own silent source."""
    from PIL import Image, ImageDraw, ImageFont
    from video_harness.media import asset_record, run_process
    text = title.get("text", "").strip()
    duration = title.get("duration_ms")
    if not text or type(duration) is not int or not 0 < duration <= 15000:
        raise ValueError("The end card needs text and a positive duration of at most 15 seconds")
    root = Path(root)
    out = root / "exports"
    out.mkdir(exist_ok=True)
    width, height = cfg.export_width, cfg.export_height
    surface = Image.new("RGB", (width, height), "black")
    draw = ImageDraw.Draw(surface)
    names = ("msyh.ttc", "PingFang.ttc", "NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    size = max(12, round(width * .067))
    for name in names:
        try:
            font = ImageFont.truetype(name, size=size)
            break
        except OSError:
            continue
    else:
        raise ValueError("A Chinese font is required to render the storyboard's end card")
    while draw.textlength(text, font=font) > width * .86 and size > 8:
        size -= 1
        font = font.font_variant(size=size)
    draw.text((width / 2, height / 2), text, font=font, fill="white", anchor="mm")
    picture = out / "outro.png"
    surface.save(picture)
    clip = out / "outro.mp4"
    run_process([cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate",
                 str(cfg.fps), "-i", str(picture), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                 "-t", f"{duration / 1000:.6f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-movflags", "+faststart", str(clip)], cfg.media_timeout_seconds)
    return {"job_id": "local-black-title", "asset": asset_record(root, clip, "video", cfg),
            "requested_duration_ms": duration}


def assemble(config, root, timeline):
    from video_harness.media import assemble_full_sources
    if not timeline or len({item["job_id"] for item in timeline}) != len(timeline):
        raise ValueError("Timeline must contain distinct, explicitly ordered candidates")
    sources, reviews, elapsed = [], [], 0
    graph = shot_dependencies(root)
    if graph:
        by_id, chosen = current_videos(root)
        archive = Path(root) / "exports" / "assembly.json"
        archived = read(archive).get("shot_reviews", []) if archive.is_file() else []
        # Rejoining an identical delivered cut must also work with archived, shorter tail files.
        same_delivery = [(v.get("job_id"), v.get("source_sha256")) for v in archived] == [
            (entry["job_id"], by_id.get(entry["job_id"], {}).get("asset", {}).get("sha256")) for entry in timeline]
        for entry in timeline:
            candidate = by_id.get(entry["job_id"])
            if candidate:
                chosen[candidate["recipe"]["shot_id"]] = entry["job_id"]
    verified = set()
    for entry in timeline:
        job_id = identifier(entry["job_id"])
        job, review = usable_review(root, job_id)
        if graph and not same_delivery:
            require_continuity(config, root, job["recipe"], graph=graph, ledger=(by_id, chosen), verified=verified)
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
    board_path = Path(root) / "storyboard.json"
    outro = read(board_path).get("editorial_outro") if board_path.is_file() else None
    cfg = technical(config)
    if outro and outro.get("type") == "black_title" and outro.get("source") == "local_edit":
        sources.append(black_title_source(root, outro, cfg))
    result = assemble_full_sources(Path(root), sources, cfg,
                                   max_duration_ms=config["production"].get("max_duration_ms"))
    result["planned_duration_ms"] = elapsed
    if len(sources) > len(timeline):
        result["planned_video_duration_ms"] = elapsed
        result["planned_duration_ms"] += outro["duration_ms"]
        result["editorial_outro"] = {"text": outro["text"], "duration_ms": outro["duration_ms"],
                                    "timeline_in_ms": result["clips"][-1]["timeline_in_ms"]}
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
    if "writer" in config["production"]:
        if provider_name == "text" and config["production"]["writer"] != "text_api":
            raise ValueError("本片选择由当前 AI 执笔，外部文字 API 已关闭。")
        if provider_name == config["review"].get("vision_provider") and config["review"]["mode"] != "vision_api":
            raise ValueError("本片选择由当前 AI 检查，外部视觉 API 已关闭。")
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
    setup = sub.add_parser("setup", help="查看首次工作流设置和每个选项的影响，或保存用户的选择")
    setup.add_argument("--answers", type=Path, help="用户已确认的完整设置值 JSON")
    create = sub.add_parser("init")
    create.add_argument("--output", type=Path, required=True)
    specs = sub.add_parser("specs")
    specs.add_argument("--role", choices=["image", "edit", "video"], required=True)
    checks = sub.add_parser("check-inputs", help="列出本候选已启用的检查及其素材指纹")
    source = checks.add_mutually_exclusive_group(required=True)
    source.add_argument("--request", type=Path)
    source.add_argument("--shot", help="在准备最终提示词之前查看实际首尾帧的检查输入")
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
    if args.command == "setup" and args.run:
        parser.error("setup applies to the installation; omit --run")
    if args.command not in {"doctor", "init", "setup"} and args.run is None:
        parser.error("--run is required for project operations")
    snapshot = args.run / "settings.json" if args.run else None
    if snapshot and args.config and args.command != "init" and snapshot.is_file() and args.config.resolve() != snapshot.resolve():
        if read(snapshot).get("production", {}).get("scheduling"):
            raise ValueError("请使用 --run 读取本片已经确认的配置快照。")
    settings_path = args.config or snapshot or DEFAULT_CONFIG
    config = load_settings(settings_path)
    if args.command in {"setup", "init"}:
        # Bring pre-setup installation templates into onboarding without changing saved productions.
        profile_dir = Path(config.get("credentials_file") or settings_path).resolve().parent
        config.setdefault("workflow_preferences_file", str(profile_dir / "workflow-preferences.local.json"))
    if args.command == "setup":
        return save_setup(config, read(args.answers)) if args.answers else setup_status(config)
    if args.command == "init":
        return init_run(config, args.output)
    if args.command == "doctor" and config.get("workflow_preferences_file"):
        prefs = setup_status(config)
        if prefs["configured"]:
            config = profile_settings(config, prefs["values"])
    with credential_environment(config):
        return dispatch(config, args)


def dispatch(config, args):
    if args.command == "doctor":
        import shutil
        return {"runtime": config["runtime"], "ffmpeg": shutil.which(config["media"]["ffmpeg"]),
                "ffprobe": shutil.which(config["media"]["ffprobe"]),
                "credentials_file": config.get("credentials_file"),
                "credential_present": {name: bool(os.environ.get(p["key_env"])) for name, p in config["providers"].items()},
                "prompt_mode": {"local": "local_rewriter"}.get(prompt_mode(config), prompt_mode(config)),
                "local_rewriter_files_present": (
                    {k: Path(config["rewriter"]["local"][k]).exists() for k in ("python", "script", "model_root")}
                    if config["rewriter"]["enabled"] else None)}
    root = args.run.resolve()
    if args.command == "specs":
        provider = config["providers"][config["models"][args.role]["provider"]]
        client = make_client(config, provider)
        try:
            _, contract = get_contract(config, root, args.role, client)
            return contract.spec
        finally:
            client.close()
    if args.command == "check-inputs":
        if args.shot:
            return planned_prechecks(config, root, frame_check_recipe(root, args.shot), ("frame",))
        recipe = request_data(config, read(args.request), args.request.resolve().parent)
        return planned_prechecks(config, root, recipe)
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
        message = str(exc) if type(exc) in {ValueError, SetupRequired} else type(exc).__name__
        try:
            from video_harness.util import HarnessError, safe_error
            message = safe_error(exc) if isinstance(exc, HarnessError) else safe_error(HarnessError(message))
        except ImportError:
            pass
        print(json.dumps({"status": "needs_attention", "error": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
