"""Compile declared shot relations and action timing into an executable plan.

The text model authors semantic judgments. These checks enforce declarations;
they do not infer physical correctness from natural language or generated pixels.
"""

from __future__ import annotations

import math
import re

from .models import Storyboard
from .util import HarnessError


def shot_dependencies(board):
    decisions, seen, latest, changed = {}, {}, {}, set()
    previous = None
    for shot in sorted(board.shots, key=lambda s: s.timeline_in_ms):
        plan = shot.generation_plan
        refs, notes = [], []
        fallback = plan is None or plan.confidence != "high" or plan.relation == "uncertain"
        if plan and plan.relation == "independent" and plan.references:
            raise HarnessError(f"{shot.shot_id}: 独立镜头不能同时声明镜头引用")
        if plan and plan.relation == "continuous" and not plan.references:
            fallback = True
        if plan and plan.relation == "continuous" and plan.references and not any(r.kind == "tail" for r in plan.references):
            fallback = True
        if fallback:
            if previous:
                refs.append({"shot_id": previous.shot_id, "kind": "tail", "reason": "缺少高置信度依赖判断，保留串行"})
            notes.append("conservative_fallback")
        else:
            refs = [r.model_dump() for r in plan.references]
        for ref in refs:
            if ref["shot_id"] not in seen:
                raise HarnessError(f"{shot.shot_id}: 依赖必须指向更早的镜头")

        # State memory only selects required visual assets. Adjacent prose/object
        # continuity validation remains the separate review_objects pass.
        for obj in shot.objects:
            old = latest.get(obj.prop_id)
            if not obj.start_visible or not old or obj.prop_id not in changed:
                continue
            origin, state = old
            if shot.transition_from_previous == "time_jump" and obj.transition_reason.strip():
                continue  # Explicitly authored new temporal state.
            if origin.scene_id != shot.scene_id:
                continue  # Cross-location transfer needs an authored reference.
            covered = False
            for ref in refs:
                source = seen[ref["shot_id"]]
                for item in source.objects:
                    state_id = item.end_state_id if ref["kind"] == "tail" else item.start_state_id
                    visible = item.end_visible if ref["kind"] == "tail" else item.start_visible
                    if item.prop_id == obj.prop_id and visible and state_id == obj.start_state_id:
                        covered = True
            if not covered:
                if not any(r["shot_id"] == origin.shot_id and r["kind"] == "tail" for r in refs):
                    refs.append({"shot_id": origin.shot_id, "kind": "tail", "reason": f"继承已变化对象 {obj.prop_id} 的实际状态"})
                notes.append(f"state_guard:{obj.prop_id}:{origin.shot_id}")
        decisions[shot.shot_id] = {
            "relation": plan.relation if plan else "uncertain",
            "reason": plan.reason if plan else "未提供依赖分析",
            "references": refs,
            "conservative_fallback": fallback,
            "notes": notes,
        }
        for obj in shot.objects:
            old = latest.get(obj.prop_id)
            if obj.start_state_id != obj.end_state_id or (old and old[1].end_state_id != obj.start_state_id):
                changed.add(obj.prop_id)
            if obj.end_visible:
                latest[obj.prop_id] = (shot, obj)
        seen[shot.shot_id] = shot
        previous = shot
    return decisions


def speech_ms(text, settings):
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", text))
    pauses = len(re.findall(r"[，。！？；,.!?;]", text)) * 150
    return math.ceil((chinese / settings.chinese_chars_per_second + words / settings.words_per_second) * 1000) + pauses


def shot_timing(shot, settings):
    if shot.timing is None:
        return max(shot.duration_ms, sum(speech_ms(d.text, settings) for d in shot.audio.dialogue)), {"method": "existing_duration_and_speech_floor"}
    timing = shot.timing
    beats = {b.beat_id: b for b in timing.beats}
    if len(beats) != len(timing.beats):
        raise HarnessError(f"{shot.shot_id}: 动作节拍 ID 重复")
    finish, intervals, pending = {}, {}, set(beats)
    dialogue = []
    while pending:
        ready = sorted(k for k in pending if set(beats[k].after) <= finish.keys())
        if not ready:
            raise HarnessError(f"{shot.shot_id}: 动作节拍存在环或引用缺失")
        for key in ready:
            beat = beats[key]
            duration = beat.duration_ms
            if beat.dialogue_index is not None:
                if beat.kind != "dialogue" or beat.dialogue_index >= len(shot.audio.dialogue):
                    raise HarnessError(f"{shot.shot_id}: 对白节拍索引不正确")
                dialogue.append(beat.dialogue_index)
                duration = max(duration, speech_ms(shot.audio.dialogue[beat.dialogue_index].text, settings))
            start = max((finish[d] for d in beat.after), default=0)
            finish[key] = start + duration
            intervals[key] = {"start_ms": timing.start_hold_ms + start, "end_ms": timing.start_hold_ms + finish[key], "description": beat.description}
            pending.remove(key)
    if sorted(dialogue) != list(range(len(shot.audio.dialogue))):
        raise HarnessError(f"{shot.shot_id}: 每条对白必须恰好对应一个 dialogue 节拍")
    required = timing.start_hold_ms + max(finish.values()) + timing.end_hold_ms
    return required, {"method": "beat_critical_path", "beats": intervals, "start_hold_ms": timing.start_hold_ms, "end_hold_ms": timing.end_hold_ms, "reason": timing.reason}


def prepare_board(board, config, contract=None):
    """Only used before creation of a run. Never silently retime a running project."""
    result = board.model_copy(deep=True)
    settings = config.duration_planning
    report = {"duration_enabled": settings.enabled, "shots": []}
    for shot in sorted(result.shots, key=lambda s: s.timeline_in_ms):
        duration, detail = shot_timing(shot, settings) if settings.enabled else (shot.duration_ms, {"method": "unchanged"})
        if settings.enabled:
            duration = math.ceil(max(duration, settings.minimum_ms) / settings.step_ms) * settings.step_ms
            if duration > settings.maximum_ms:
                raise HarnessError(f"{shot.shot_id}: 动作/对白需要 {duration}ms，超过单镜限制；请在文本规划阶段拆镜")
            if contract:
                body = contract.parameters(config.video, "duration preflight", result.aspect_ratio, duration)
                field = contract.spec.get("output", {}).get("duration", {}).get("parameter")
                if not field:
                    raise HarnessError("动态时长需要支持时长参数的视频模型")
                duration = int(body[field] * 1000)
                if duration > settings.maximum_ms:
                    raise HarnessError(f"{shot.shot_id}: 接口时长超出规划上限")
        report["shots"].append({"shot_id": shot.shot_id, "source_duration_ms": shot.duration_ms, "duration_ms": duration, "timing": detail})
        shot.duration_ms = duration
    if settings.enabled and config.target_duration_ms:
        delta = config.target_duration_ms - sum(s.duration_ms for s in result.shots)
        if abs(delta) <= settings.target_tolerance_ms:
            delta = 0  # Keep a naturally timed result within the declared tolerance.
        if delta < 0 or delta % settings.step_ms:
            raise HarnessError(f"目标片长不足以完成动作，或不符合时长步长；当前需要 {sum(s.duration_ms for s in result.shots)}ms，目标 {config.target_duration_ms}ms，容差 {settings.target_tolerance_ms}ms；请重分配时间预算，不自动加速动作")
        for shot in reversed(result.shots):
            if not delta:
                break
            allowed = []
            for value in range(shot.duration_ms, settings.maximum_ms + 1, settings.step_ms):
                if contract:
                    body = contract.parameters(config.video, "duration preflight", result.aspect_ratio, value)
                    if int(body[contract.spec["output"]["duration"]["parameter"]] * 1000) != value:
                        continue
                allowed.append(value)
            value = max(v for v in allowed if v - shot.duration_ms <= delta)
            delta -= value - shot.duration_ms
            shot.duration_ms = value
        if delta:
            raise HarnessError("目标片长无法在各镜时长上限内分配，请调整分镜")
    end = 0
    originals = {s.shot_id: s for s in board.shots}
    for shot, item in zip(sorted(result.shots, key=lambda s: s.timeline_in_ms), report["shots"], strict=True):
        if shot.duration_ms != originals[shot.shot_id].duration_ms and shot.video_prompt:
            raise HarnessError(f"{shot.shot_id}: 时长改变但存在手写视频提示词，请同步重写或设为 null 后按分镜生成")
        item["duration_ms"] = shot.duration_ms
        shot.timeline_in_ms = end
        end += shot.duration_ms
    result.total_duration_ms = end
    result = Storyboard.model_validate(result.model_dump())
    report["total_duration_ms"] = end
    if config.target_duration_ms:
        report["duration_budget"] = {"target_ms": config.target_duration_ms, "tolerance_ms": settings.target_tolerance_ms,
                                     "actual_ms": end, "difference_ms": end - config.target_duration_ms}
    report["dependencies"] = shot_dependencies(result) if config.shot_scheduling == "dependency" else {"mode": "legacy"}
    config.check_board(result)
    return result, report


def check_runtime_duration(node, body, contract, config):
    if config.duration_planning.enabled and node["kind"] in {"video", "prompt_rewrite"}:
        field = contract.spec.get("output", {}).get("duration", {}).get("parameter")
        if not field or int(body[field] * 1000) != node["data"]["duration_ms"]:
            raise HarnessError("实时接口时长与分镜不一致；请重新 optimize-plan --live-specs 后创建新 run，尚未提交生成任务")
