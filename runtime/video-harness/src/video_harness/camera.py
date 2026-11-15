"""Check a declared locked-camera invariant without narrative justification."""
from .quality import QualityVerdict
from .util import HarnessError, write_json


CAMERA_INSTRUCTIONS = """Inspect two actual images as endpoints of ONE LOCKED CAMERA shot. The camera position, rotation, height, zoom and field of view must remain fixed. No plot or desired action is supplied. Return a QualityVerdict JSON only.
Compare only static scene geometry: foreground table, chairs/fixed furniture, wall/counter/window layout, projected positions and scale. Changes in a person's pose and hand-held movable objects are allowed. Small rendering/texture differences are not geometry violations. A large foreground table cannot disappear merely because a person stands up. A medium view cannot become a full-body view by zooming out, tilting, moving the camera, a dissolve or a cut. Do not excuse a changed camera/framing as necessary to reveal a person or destination; camera motion is expressly forbidden for this check. Distinguish legitimate occlusion by a moving foreground person from deletion or relocation of fixed furniture. If scene geometry or field of view changes, outcome=fail with specific image evidence. Use uncertain if the evidence cannot resolve a material geometric question. Otherwise pass. Do not evaluate food fragmentation, acting, dialogue, or whether the action is complete.
Schema: {"outcome":"pass|fail|uncertain","issues":[{"category":"scene|physics|unverifiable","severity":"blocker|warning","expected":"camera/scene invariant","observed":"specific difference","evidence":"image label and region","suggested_change":"minimal framing correction"}],"summary":"brief observation","coverage":"scope and limitations","visual_anchors":[]}
"""


def is_locked_camera(movement):
    # Exact declarations only: a sentence that starts static then moves is not locked.
    return movement.strip().lower().rstrip("。.") in {
        "固定", "固定机位", "固定镜头", "静止", "静态", "static", "static camera",
        "locked", "locked camera", "locked-off", "locked-off camera", "locked off", "locked off camera",
    }


def review_fixed_camera(reviewer, first_frame, target_frame, **limits):
    raw, root, key = reviewer.generate_visual(
        {"stage": "locked_camera_endpoints", "camera_motion_allowed": False},
        [("FIRST FRAME", first_frame), ("TARGET ENDING FRAME", target_frame)],
        instructions=CAMERA_INSTRUCTIONS, **limits,
    )
    write_json(root / "camera-verdict.raw.json", raw)
    try:
        verdict = QualityVerdict.model_validate(raw)
    except ValueError:
        raise HarnessError("固定机位检查格式无效，不能通过结束图检查") from None
    path = root / "camera-verdict.json"
    write_json(path, verdict.model_dump())
    return {"verdict": verdict.model_dump(), "record_path": str(path), "request_hash": key}
