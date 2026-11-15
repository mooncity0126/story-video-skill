"""Plan causal choreography from the accepted actual frame before local rewriting."""
from pydantic import BaseModel, ConfigDict, Field

from .quality import VisualReviewer
from .util import HarnessError, write_json


GEOMETRY_INSTRUCTIONS = """Observe the actual first-frame image, without inventing a future action. Return JSON only. Identify at most six foreground objects involved in visible hand/contact relationships, their current orientation, openings, solid boundaries, contents and supports. State up/down relative to gravity, not just the screen. A visible top surface of contents inside a surrounding rim is evidence of an upward-facing opening; do not assume a hidden removable base or downward opening. Distinguish observed features from uncertain hidden surfaces. Report contacts and occupied hands. Do not add an action plan, storyline or aesthetic descriptions. Detail crops are portions of the same image, not extra objects.
Schema: {"observations":[{"object":"visible object","orientation":"orientation relative to gravity","openings_and_boundaries":"observed opening direction and solid barriers; uncertainty where hidden","contents":"current contents or not applicable","support_and_contact":"what supports/touches it"}],"hands":"visible hand contacts and occupancy","uncertainties":["concrete unknowns, if any"]}
"""


MOTION_INSTRUCTIONS = """Plan the physical execution of ONE existing shot using its actual accepted first-frame image, canonical intent and required final state. Do not change the story, cast, clothes, room, lighting, props, dialogue, camera intention or duration. Return JSON only.
The image and independently observed initial_geometry determine current geometry: positions, hand grips, openings, solid surfaces, supports and orientation. Verify those observations against the pixels; a narrative assumption cannot override them. Before describing any transfer, explicitly state the starting opening direction and required rotation, if any. Being above a destination does not mean a container opens toward it. The original action determines what must happen, not an excuse for impossible motion. Expand only necessary causal phases of that same action. Describe how the actor establishes contact, moves/rotates/releases the existing object through a real opening, and leaves it supported in the required final state. Account for each hand/tool that changes contact. A container's solid base/wall cannot disappear or be passed through. Do not invent removable components, tools, pre-cut pieces, extra objects or an offscreen intervention to explain an impossible transfer. Preserve materials: accidental breakage is not neat pre-cut slicing or a liquid splash.
For every change of hand use, name the supporting hand and the acting hand. A hand cannot keep gripping one place while tapping or reaching elsewhere. Explicitly release/reposition/regrip when needed, with the object continuously supported by the other hand or a visible surface. State contact transitions as actions, not as generic cautions.
Before a tap, press or release, orient the object so the intended contact surface is actually accessible. Prefer exposing a container's exterior base and establishing its exit direction BEFORE tapping that base, instead of striking toward its contents. Name the contact surface and direction of the acting hand relative to gravity. Preparation may be inserted before the terse authored action; preserve its story purpose and final state, not an impossible literal sentence order. Describe accidental material fracture as rough irregular tears with unequal fragments, not neat radial cutting.
Keep one continuous shot and a plausible pace within duration_ms. Normal preparation and returning the same tool are phases of a coherent action, not mandatory separate shots. If the actual image makes the intended action or final state impossible within the requested duration, set feasible=false and explain the concrete conflict. Do not quietly change either state. Keep the description concise in Chinese, suitable as input to the existing video-prompt rewriter. No lengthy negative-prompt list or background inventory.
Schema: {"feasible":true,"duration_ms":5000,"motion_description":"concise causal execution, ending at the required final state","steps":["chronological observable phase with contact and state change"],"reason":"how this uses actual visible geometry","risks":["remaining concrete uncertainty, if any"]}
"""


class MotionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feasible: bool
    duration_ms: int = Field(gt=0, le=15000)
    motion_description: str = Field(min_length=1, max_length=2400)
    steps: list[str] = Field(min_length=1, max_length=8)
    reason: str
    risks: list[str] = Field(default_factory=list, max_length=8)


class GeometryObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object: str
    orientation: str
    openings_and_boundaries: str
    contents: str
    support_and_contact: str


class FrameGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observations: list[GeometryObject] = Field(min_length=1, max_length=6)
    hands: str
    uncertainties: list[str] = Field(default_factory=list, max_length=8)


def rewrite_motion_context(plan):
    text = "\nExecutable causal motion from the actual first-frame geometry. Preserve the required phases and final placement:\n" + plan["motion_description"]
    text += "\nChronological phases:\n" + "\n".join(plan["steps"])
    if plan["risks"]:
        text += "\nConcrete physical risks to resolve within that same action:\n" + "\n".join(plan["risks"])
    return text


class VisualMotionPlanner(VisualReviewer):
    def plan(self, data, images, **limits):
        geometry_raw, geometry_root, geometry_key = self.generate_visual(
            {"stage": "initial_geometry", "scope": "Only the visible current state; no intended action supplied."},
            images, instructions=GEOMETRY_INSTRUCTIONS, **limits,
        )
        write_json(geometry_root / "geometry.raw.json", geometry_raw)
        try:
            geometry = FrameGeometry.model_validate(geometry_raw)
        except ValueError:
            raise HarnessError("首帧几何观察格式无效；未规划动作或送入 Rewriter") from None
        raw, root, key = self.generate_visual({**data, "initial_geometry": geometry.model_dump()}, images, instructions=MOTION_INSTRUCTIONS, **limits)
        write_json(root / "motion-plan.raw.json", raw)
        try:
            plan = MotionPlan.model_validate(raw)
        except ValueError:
            raise HarnessError("首帧动作规划格式无效；未送入 Rewriter") from None
        if plan.duration_ms != data["contract"]["duration_ms"]:
            raise HarnessError("动作规划不能悄悄改变分镜时长")
        record = {"plan": plan.model_dump(), "initial_geometry": geometry.model_dump(), "geometry_request_hash": geometry_key,
                  "request_hash": key, "record_path": str(root / "motion-plan.json")}
        write_json(root / "motion-plan.json", record)
        if not plan.feasible:
            raise HarnessError("实际首帧无法按当前动作/时长完成分镜：" + plan.reason)
        return record
