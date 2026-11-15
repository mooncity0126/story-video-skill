from __future__ import annotations

import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .models import Characters, Storyboard, validate_plan
from .continuity import review_objects
from .prompts import template
from .planning import prepare_board
from .preview import write_plan_preview
from .util import HarnessError, canonical, digest, now, read_json, run_lock, safe_error, write_json


class TextAdapter:
    """Small replaceable text-only adapter for an explicitly configured Chat Completions API."""

    def __init__(self, config, transport=None):
        self.config = config
        self.key = os.environ.get(config.key_env, "")
        x2v = os.environ.get("X2V_API_KEY", "")
        if config.key_env.upper() == "X2V_API_KEY" or (self.key and x2v and hmac.compare_digest(self.key, x2v)):
            raise HarnessError("文本服务不得使用 X2V_API_KEY")
        if not config.endpoint or not config.model or not self.key:
            raise HarnessError("未配置独立文本 API；请使用导出的提示词生成并导入 JSON")
        if urlparse(config.endpoint).scheme != "https":
            raise HarnessError("文本 endpoint 必须使用 HTTPS")
        self.transport = transport
        self.calls = 0

    def generate(self, instructions, data, *, cache_dir=None, max_tokens=None):
        # A separate client, exact configured endpoint, no automatic paid text retries.
        body = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": data}],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if self.config.enable_thinking is not None:
            body["enable_thinking"] = self.config.enable_thinking
        request_hash = digest({"endpoint": self.config.endpoint, "body": body})
        root = Path(cache_dir) / request_hash if cache_dir else None
        if root:
            root.mkdir(parents=True, exist_ok=True)
            with run_lock(root):
                return self._request(body, root, request_hash)
        return self._request(body, None, request_hash)

    def _request(self, body, root, request_hash, *, logged_body=None):
        if root and (root / "response.json").is_file():
            response = read_json(root / "response.json")
            return self._content(response)
        if root and (root / "request.json").is_file():
            raise HarnessError("已有文本请求未得到可复用结果；为避免重复扣费，未自动重试。请检查 text_requests 记录")
        if self.calls >= self.config.max_calls:
            raise HarnessError("已达到本次文本请求次数上限")
        self.calls += 1
        if root:
            write_json(root / "request.json", {"request_hash": request_hash, "endpoint": self.config.endpoint, "body": logged_body if logged_body is not None else body, "created_at": now()})
            write_json(root / "status.json", {"status": "requesting", "request_hash": request_hash})
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.config.timeout, transport=self.transport, follow_redirects=False) as client:
                response = client.post(
                    self.config.endpoint,
                    headers={"Authorization": "Bearer " + self.key},
                    json=body,
                )
            if not response.is_success:
                raise HarnessError(f"文本 API 请求失败 (HTTP {response.status_code})")
            payload = response.json()
            if root:
                write_json(root / "response.json", payload)
                write_json(
                    root / "usage.json",
                    {"request_hash": request_hash, "model": payload.get("model", self.config.model), "usage": payload.get("usage"), "elapsed_seconds": round(time.monotonic() - started, 3)},
                )
            result = self._content(payload)
            if root:
                write_json(root / "status.json", {"status": "completed", "request_hash": request_hash})
            return result
        except (httpx.TransportError, ValueError, KeyError, IndexError, TypeError, HarnessError) as exc:
            error = exc if isinstance(exc, HarnessError) else HarnessError("文本服务超时或响应格式无效，已保留记录且未自动重试")
            if root:
                write_json(root / "status.json", {"status": "failed_or_unknown", "error": safe_error(error), "request_hash": request_hash})
            raise error from None

    @staticmethod
    def _content(payload):
        try:
            choice = payload["choices"][0]
            if choice.get("finish_reason") not in {None, "stop"}:
                raise HarnessError("文本输出被截断或未正常完成，未导入分镜，也未自动重试")
            result = json.loads(choice["message"]["content"])
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, KeyError, IndexError, TypeError):
            raise HarnessError("文本模型未返回合法 JSON 对象，原响应已保留") from None


def plan(script, config, output, transport=None, characters=None, reference_storyboard=None, keep_prefix=0):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    with run_lock(root):
        try:
            return _plan(script, config, root, transport, characters, reference_storyboard, keep_prefix)
        finally:
            usage = [read_json(p) for p in sorted((root / "text_requests").glob("*/usage.json"))]
            write_json(root / "text-usage.json", usage)


def _plan(script, config, root, transport, characters, reference_storyboard, keep_prefix):
    if keep_prefix < 0 or (keep_prefix and not reference_storyboard) or (reference_storyboard and not characters):
        raise HarnessError("参考分镜重新规划需要已有角色；保留前缀数量必须非负，且只能与参考分镜一起使用")
    story = Path(script).read_text(encoding="utf-8-sig")
    if not story.strip():
        raise HarnessError("剧本为空")
    constraint = f"\n制作要求：画幅 {config.aspect_ratio}；统一风格 {config.visual_style}；目标片长 {config.target_duration_ms or '按剧情规划'} 毫秒。"
    if config.duration_planning.enabled:
        d = config.duration_planning
        constraint += f"\n时长约束：每镜 {d.minimum_ms}—{d.maximum_ms} 毫秒，步长 {d.step_ms} 毫秒。"
        if config.target_duration_ms:
            constraint += f"\n总时长预算优先：目标 {config.target_duration_ms} 毫秒，允许偏差 {d.target_tolerance_ms} 毫秒。动作确实过密时可以增加镜头，但新增镜头的时间应从总预算重新分配，而非逐镜追加。先减少重复展示和非必要停留，不加速必要动作、不删除剧情。每个新增镜头仍需满足接口最小时长。"
    if config.max_shots is not None:
        constraint += f"\n镜头数量上限：{config.max_shots} 镜。按完整叙事动作分镜，不能为了减少单次生成难度把同一动作的各个阶段分成多个镜头，也不能删去剧情来满足上限。优先按动作节拍分配足够时长。"
    adaptive = config.shot_scheduling == "dependency" or config.duration_planning.enabled
    instructions = {}
    for name in ("characters", "storyboard"):
        extra = "\n\n" + template("object_continuity") if name == "storyboard" and config.require_object_states else ""
        base = template("adaptive_planning") if name == "storyboard" and adaptive else template(name)
        instructions[name] = base + extra + constraint
    review_instructions = instructions["storyboard"]
    supplied_characters = read_json(characters) if characters else None
    reference = Storyboard.model_validate(read_json(reference_storyboard)) if reference_storyboard else None
    if reference:
        validate_plan(Characters.model_validate(supplied_characters), reference)
        if keep_prefix >= len(reference.shots) or (config.max_shots is not None and keep_prefix >= config.max_shots):
            raise HarnessError("保留的前缀必须为重新规划的后续镜头留下空间")
        instructions["storyboard"] += "\n\n重新规划已有分镜：原剧本是剧情依据，参考分镜不是必须保留的剪辑方案。保留原有角色、场景及道具设定，把后续分镜按完整叙事动作重新组织；不要复制冗余段落，也不要删除原剧本事件。镜头数量上限包含已保留的前缀。只返回 {\"shots\":[完整的后续 shot 对象]}，不返回其他字段、不重复已保留的前缀。新后续镜头必须接上前缀结束状态，使用唯一 ID，所有依赖指向前缀或更早的新镜头，不能引用已删去的旧镜头。"
    identity = digest(
        {
            "script": story,
            "characters": supplied_characters,
            "instructions": instructions,
            "text": config.text.model_dump(),
            "duration": config.duration_planning.model_dump(),
            "scheduling": config.shot_scheduling,
            "quality_review_enabled": config.quality.enabled,
            "reference_storyboard": reference.model_dump() if reference else None,
            "keep_prefix": keep_prefix,
        }
    )
    marker = root / "planning-input.json"
    if marker.exists() and read_json(marker)["input_hash"] != identity:
        raise HarnessError("输出目录已有不同的规划输入，请使用新目录")
    if not marker.exists() and ((root / "characters.json").exists() or (root / "storyboard.json").exists()):
        raise HarnessError("输出目录已有外部规划，请使用新目录")
    write_json(marker, {"input_hash": identity})
    for name, instruction in instructions.items():
        (root / (name + "-prompt.md")).write_text(instruction, encoding="utf-8")
    (root / "script.md").write_text(story, encoding="utf-8")
    if not config.text.endpoint or not config.text.model or not os.environ.get(config.text.key_env):
        return {
            "status": "needs_text_provider_or_import",
            "prompts": [str(root / "characters-prompt.md"), str(root / "storyboard-prompt.md")],
            "note": "未生成角色或分镜；先用角色提示词，再把角色JSON与剧本交给分镜提示词",
        }
    adapter = TextAdapter(config.text, transport)
    chars_raw = (
        supplied_characters if supplied_characters is not None else adapter.generate(instructions["characters"], story, cache_dir=root / "text_requests", max_tokens=min(6144, config.text.max_tokens))
    )
    if characters:
        write_json(root / "characters-source.json", {"path": str(Path(characters).resolve()), "content_hash": digest(chars_raw), "reused": True})
    write_json(root / "characters.raw.json", chars_raw)
    chars = Characters.model_validate(chars_raw)
    write_json(root / "characters.json", chars.model_dump())
    if chars.status != "ok":
        raise HarnessError("角色规划需修正，结果已保存")
    if reference:
        raw = adapter.generate(instructions["storyboard"], canonical({"script": story, "characters": chars.model_dump(),
                                                              "reference_storyboard": reference.model_dump(),
                                                              "fixed_prefix": [s.model_dump() for s in reference.shots[:keep_prefix]]}), cache_dir=root / "text_requests")
    else:
        raw = adapter.generate(instructions["storyboard"], "剧本：\n" + story + "\n角色JSON：\n" + canonical(chars.model_dump()), cache_dir=root / "text_requests")
    write_json(root / "storyboard.raw.json", raw)
    assembled = raw
    fixed_prefix = reference.shots[:keep_prefix] if reference else []
    if reference:
        if set(raw) != {"shots"} or not isinstance(raw["shots"], list) or not raw["shots"]:
            raise HarnessError("重新规划必须只返回非空的后续 shots 数组；原响应已保存")
        assembled = {**reference.model_dump(), "shots": [s.model_dump() for s in fixed_prefix] + raw["shots"]}
        write_json(root / "storyboard-source.json", {"path": str(Path(reference_storyboard).resolve()), "content_hash": digest(reference.model_dump()),
                                                    "kept_prefix_shot_ids": [s.shot_id for s in fixed_prefix],
                                                    "preserved_fields": "all non-shot fields; original model output contains only the new suffix"})
    # Compile natural action/speech timing before comparing the whole-film budget.
    # One model-authored rebalance may merge/split the editable suffix; the caller
    # never shortens action beats or edits generated prose to make the sum fit.
    draft_config = config.model_copy(deep=True)
    draft_config.target_duration_ms = None
    board, draft_report = normalize_board(assembled, chars, draft_config)
    if config.target_duration_ms and board.total_duration_ms > config.target_duration_ms + config.duration_planning.target_tolerance_ms:
        budget = {"target_ms": config.target_duration_ms, "tolerance_ms": config.duration_planning.target_tolerance_ms,
                  "draft_ms": board.total_duration_ms, "fixed_prefix_ms": sum(s.duration_ms for s in fixed_prefix),
                  "report": draft_report}
        write_json(root / "duration-budget-repair.json", {"status": "required", **budget})
        rebalance_instruction = instructions["storyboard"] + "\n\n上一稿的实际节拍总时长超过预算，请重新分配整片时间。依据原剧本保留全部剧情事件、因果和情感转折；可以合并连续动作、在过密处重新分镜，删去参考稿自行添加且非必要的装饰动作，省略已经交代的重复制作并明确跳切。不要逐镜沿用旧时长，也不要通过加速动作、删掉必要因果或低于模型最小时长凑数。提供的编译报告已经计入对白自然语速和接口取整，必须据此计算总和。固定前缀保持原文和时长，其他镜头依赖重新连接。输出格式与本次初稿相同。"
        rebalanced = adapter.generate(rebalance_instruction, canonical({"script": story, "characters": chars.model_dump(),
                                                                         "candidate_storyboard": board.model_dump(), "duration_budget": budget,
                                                                         "fixed_prefix": [s.model_dump() for s in fixed_prefix]}), cache_dir=root / "text_requests")
        write_json(root / "storyboard.duration-repair.raw.json", rebalanced)
        if reference:
            if set(rebalanced) != {"shots"} or not isinstance(rebalanced["shots"], list) or not rebalanced["shots"]:
                raise HarnessError("时间预算重规划必须只返回非空的后续 shots 数组，原响应已保存")
            rebalanced = {**reference.model_dump(), "shots": [s.model_dump() for s in fixed_prefix] + rebalanced["shots"]}
        try:
            board, report = normalize_board(rebalanced, chars, config)
        except (ValueError, HarnessError):
            write_json(root / "duration-budget-repair.json", {"status": "failed_validation", **budget})
            raise
        write_json(root / "storyboard.duration-repair.json", board.model_dump())
        write_json(root / "duration-budget-repair.json", {"status": "rebalanced", **budget, "result_ms": board.total_duration_ms, "result_shots": len(board.shots)})
    else:
        board, report = normalize_board(assembled, chars, config)
    if board.shots[:keep_prefix] != fixed_prefix:
        raise HarnessError("重新规划改变了保留前缀的时长或状态，未进入媒体生成")
    if config.quality.enabled:
        from .plan_quality import review_and_repair

        board, repair_report, history = review_and_repair(adapter, story, chars, board, config, root, review_instructions, normalize_board, fixed_prefix=fixed_prefix)
        if repair_report:
            report = repair_report
        report["quality_review"] = history
    write_json(root / "storyboard.json", board.model_dump())
    write_json(root / "continuity-review.json", review_objects(board, config.require_object_states))
    write_json(root / "planning-report.json", report)
    write_plan_preview(board, report, root / "分镜预览.html", config.text.model)
    usage = [read_json(p) for p in sorted((root / "text_requests").glob("*/usage.json"))]
    write_json(root / "text-usage.json", usage)
    return {
        "status": "planned",
        "characters": str(root / "characters.json"),
        "storyboard": str(root / "storyboard.json"),
        "report": str(root / "planning-report.json"),
        "preview": str(root / "分镜预览.html"),
        "model": config.text.model,
        "requests_this_invocation": adapter.calls,
        "total_duration_ms": board.total_duration_ms,
        "shots": len(board.shots),
        "usage": usage,
    }


def normalize_board(raw, chars, config):
    adaptive = config.shot_scheduling == "dependency" or config.duration_planning.enabled
    # Preserve raw output; only timeline arithmetic is normalized, not story content.
    data = json.loads(json.dumps(raw))
    normalizations = []
    if adaptive:
        cursor = 0
        for shot in data.get("shots", []):
            if shot.get("timeline_in_ms") != cursor:
                normalizations.append({"shot_id": shot.get("shot_id"), "field": "timeline_in_ms", "from": shot.get("timeline_in_ms"), "to": cursor})
            shot["timeline_in_ms"] = cursor
            cursor += shot.get("duration_ms", 0)
            # The dependency compiler owns this legacy rendering policy. The
            # model's semantic decision lives solely in generation_plan.
            if shot.get("first_frame_reference_policy") != "planned_frame":
                normalizations.append({"shot_id": shot.get("shot_id"), "field": "first_frame_reference_policy", "from": shot.get("first_frame_reference_policy"), "to": "planned_frame"})
            shot["first_frame_reference_policy"] = "planned_frame"
        data["total_duration_ms"] = cursor
    board = Storyboard.model_validate(data)
    if adaptive and any(s.generation_plan is None or s.timing is None for s in board.shots):
        raise HarnessError("千问分镜缺少依赖判断或动作节拍，已保存原响应，尚未导入")
    board, report = prepare_board(board, config)
    report["metadata_normalizations"] = normalizations
    validate_plan(chars, board)
    config.check_board(board)
    review_objects(board, config.require_object_states)
    return board, report
