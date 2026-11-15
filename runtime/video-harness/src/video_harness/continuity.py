"""Validate an explicitly authored object inventory, using adjacent shots only.

This checks structured declarations; it does not pretend to understand arbitrary prose.
The author/text planner must enumerate visible objects and review their descriptions.
"""

from __future__ import annotations

from .util import HarnessError


def review_objects(board, required=True):
    if not required:
        return {"status": "disabled", "pairs": []}
    registry = {p.prop_id: p for p in board.props}
    previous = None
    pairs = []
    for shot in sorted(board.shots, key=lambda s: s.timeline_in_ms):
        current = {o.prop_id: o for o in shot.objects}
        if len(current) != len(shot.objects):
            raise HarnessError(f"{shot.shot_id}: 对象重复")
        if not current and not shot.no_objects_reason.strip():
            raise HarnessError(f"{shot.shot_id}: 必须列举所有在画物品，或明确说明画面无独立物品")
        if set(current) - set(registry) or set(shot.focus_prop_ids) - set(current):
            raise HarnessError(f"{shot.shot_id}: 对象未登记或遗漏关键物品")
        prior = {o.prop_id: o for o in previous.objects} if previous else {}
        exclusions = {e.prop_id: e.reason for e in shot.framing_exclusions}
        if len(exclusions) != len(shot.framing_exclusions) or set(exclusions) - set(registry):
            raise HarnessError(f"{shot.shot_id}: 取景排除对象重复或未登记")
        if set(exclusions) & set(current) or set(exclusions) - {p for p, o in prior.items() if o.end_visible}:
            raise HarnessError(f"{shot.shot_id}: 取景排除只能解释前镜仍可见、本镜不入画的对象")
        if any(not reason.strip() for reason in exclusions.values()):
            raise HarnessError(f"{shot.shot_id}: 取景排除需要明确机位依据")
        for pid, obj in current.items():
            if not registry[pid].appearance.strip() or not registry[pid].basis.strip():
                raise HarnessError(f"{shot.shot_id}/{pid}: 对象必须有外观与剧本/制作设计依据")
            if not obj.start_visible and not obj.end_visible:
                raise HarnessError(f"{shot.shot_id}/{pid}: 全程画外的对象应从本镜对象表移除")
            if not obj.end_visible and not obj.exit_reason.strip():
                raise HarnessError(f"{shot.shot_id}/{pid}: 离画必须说明原因")
            old = prior.get(pid)
            if obj.start_visible and old and old.end_visible:
                if (old.end_state_id, old.end_description) != (obj.start_state_id, obj.start_description) and not obj.transition_reason.strip():
                    raise HarnessError(f"{previous.shot_id}→{shot.shot_id}/{pid}: 相邻状态跳变且无衔接依据")
            elif obj.start_visible and not obj.entry_reason.strip():
                raise HarnessError(f"{shot.shot_id}/{pid}: 新进入画面的对象必须说明来源，禁止无端增加")
            if not obj.start_visible and not obj.entry_reason.strip():
                raise HarnessError(f"{shot.shot_id}/{pid}: 镜内入画/生成必须说明来源")
        if previous:
            missing = [pid for pid, obj in prior.items() if obj.end_visible and pid not in current]
            # A scene cut itself explains the old setting leaving the image; no global carryover.
            unexplained = [p for p in missing if p not in exclusions]
            if unexplained and shot.transition_from_previous != "scene_change":
                raise HarnessError(f"{previous.shot_id}→{shot.shot_id}: 前镜仍在画的物品无故消失 {unexplained}")
            pairs.append(
                {
                    "previous_shot_id": previous.shot_id,
                    "shot_id": shot.shot_id,
                    "carried_objects": sorted(set(prior) & set(current)),
                    "entered_objects": sorted(set(current) - {p for p, o in prior.items() if o.end_visible}),
                    "left_objects": sorted(set(prior) - set(current)),
                    "out_of_frame_by_reframing": exclusions,
                    "status": "passed",
                }
            )
        previous = shot
    return {"status": "passed", "method": "adjacent structured object declarations; semantic prose reviewed by plan author", "pairs": pairs}
