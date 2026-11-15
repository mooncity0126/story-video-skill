from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .util import HarnessError, read_json

ID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")]
Nonempty = Annotated[str, StringConstraints(min_length=1)]
Millis = Annotated[int, Field(strict=True, ge=0)]


class PlanModel(BaseModel):
    # Existing workflow extensions (facts, evidence, design suggestions) survive round trips.
    model_config = ConfigDict(extra="allow", strict=True)


class Variant(PlanModel):
    variant_id: ID
    trigger: str
    changes: dict[str, Any]
    basis: str


class Character(PlanModel):
    character_id: ID
    name: Nonempty
    aliases: list[str]
    character_type: Literal["human", "animal", "non_human", "group"] | None = None
    presence: Literal["on_screen", "off_screen_voice", "mentioned_only"]
    requires_visual_design: bool
    appearance: dict[str, Any] | None
    visual_description: str | None
    consistency_anchors: list[str]
    variants: list[Variant]

    @model_validator(mode="after")
    def visual(self):
        unique(self.variants, "variant_id")
        if self.requires_visual_design and (not self.appearance or not self.visual_description):
            raise ValueError("需要视觉设计的角色必须有 appearance 和 visual_description")
        if self.presence != "on_screen" and self.requires_visual_design:
            raise ValueError("仅声音或提及角色不应要求视觉设计")
        return self


class Characters(PlanModel):
    schema_version: Literal["character_bible.v1"] = "character_bible.v1"
    status: Literal["ok", "needs_input", "needs_review"]
    characters: list[Character]
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ids(self):
        unique(self.characters, "character_id")
        ids = {c.character_id for c in self.characters}
        for c in self.characters:
            for rel in getattr(c, "relationships", []):
                if rel.get("target_character_id") not in ids:
                    raise ValueError("人物关系引用了不存在的角色")
        return self


class Scene(PlanModel):
    scene_id: ID
    location: str
    environment: str
    layout_anchors: list[str]
    lighting: str
    time_of_day: str | None = None


class Prop(PlanModel):
    prop_id: ID
    name: str
    appearance: str
    initial_state: str
    basis: str = ""


class ShotObject(PlanModel):
    prop_id: ID
    start_visible: bool = True
    end_visible: bool = True
    start_state_id: ID
    end_state_id: ID
    start_description: Nonempty
    end_description: Nonempty
    entry_reason: str = ""
    exit_reason: str = ""
    transition_reason: str = ""


class FramingExclusion(PlanModel):
    prop_id: ID
    reason: Nonempty


class VisibleCharacter(PlanModel):
    character_id: ID
    variant_id: ID | None


class Camera(PlanModel):
    shot_size: str
    angle: str
    movement: str
    composition: str


class Dialogue(PlanModel):
    speaker_character_id: ID | None
    text: Nonempty
    delivery: Literal["on_screen", "off_screen", "voice_over"]


class Audio(PlanModel):
    dialogue: list[Dialogue]
    sound_effects: list[str]
    music: str | None


class ReferencePlan(PlanModel):
    strategy: Literal["independent_keyframe", "reuse_composition", "continue_from_previous"]
    based_on_shot_id: ID | None = None
    notes: str = ""


class VisualReference(PlanModel):
    shot_id: ID
    kind: Literal["tail", "frame"] = "tail"
    reason: Nonempty


class GenerationPlan(PlanModel):
    relation: Literal["continuous", "shared_reference", "independent", "uncertain"]
    confidence: Literal["high", "low"] = "low"
    reason: Nonempty
    references: list[VisualReference] = Field(default_factory=list)


class TimingBeat(PlanModel):
    beat_id: ID
    description: Nonempty
    duration_ms: Annotated[int, Field(strict=True, gt=0, le=15000)]
    after: list[ID] = Field(default_factory=list)
    kind: Literal["action", "dialogue", "camera", "hold"] = "action"
    dialogue_index: Annotated[int, Field(strict=True, ge=0)] | None = None


class TimingPlan(PlanModel):
    beats: list[TimingBeat] = Field(min_length=1)
    start_hold_ms: Millis = 0
    end_hold_ms: Millis = 500
    reason: Nonempty


class Shot(PlanModel):
    shot_id: ID
    scene_id: ID
    visible_characters: list[VisibleCharacter]
    duration_ms: Annotated[int, Field(strict=True, gt=0, le=15000)]
    timeline_in_ms: Millis
    camera: Camera
    first_frame_description: Nonempty
    start_state: Nonempty
    action_description: Nonempty
    end_state: Nonempty
    audio: Audio
    continuity_notes: list[str]
    focus_prop_ids: list[ID] = Field(default_factory=list)
    objects: list[ShotObject] = Field(default_factory=list)
    no_objects_reason: str = ""
    framing_exclusions: list[FramingExclusion] = Field(default_factory=list)
    first_frame_reference_policy: Literal["legacy", "planned_frame"] = "legacy"
    video_prompt: Nonempty | None = None
    video_prompt_version: Nonempty = "authored-v1"
    transition_from_previous: Literal["first_shot", "same_scene", "scene_change", "time_jump"] = "same_scene"
    reference_plan: ReferencePlan = Field(default_factory=lambda: ReferencePlan(strategy="independent_keyframe"))
    generation_plan: GenerationPlan | None = None
    timing: TimingPlan | None = None


class Storyboard(PlanModel):
    schema_version: Literal["storyboard.v1"] = "storyboard.v1"
    status: Literal["ok", "needs_input", "needs_review"]
    aspect_ratio: str = "9:16"
    visual_style: Nonempty = "写实"
    total_duration_ms: Millis
    scenes: list[Scene]
    props: list[Prop]
    shots: list[Shot]
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def timeline(self):
        if self.aspect_ratio not in {"9:16", "16:9", "1:1", "4:3", "3:4", "3:2", "2:3"}:
            raise ValueError("无效画幅")
        for items, attr in [(self.scenes, "scene_id"), (self.props, "prop_id"), (self.shots, "shot_id")]:
            unique(items, attr)
        end = 0
        previous = set()
        for shot in sorted(self.shots, key=lambda s: s.timeline_in_ms):
            if shot.timeline_in_ms != end:
                raise ValueError("时间线必须从0连续，无重叠或空隙")
            if shot.scene_id not in {s.scene_id for s in self.scenes}:
                raise ValueError(f"{shot.shot_id}: 场景不存在")
            if set(shot.focus_prop_ids) - {p.prop_id for p in self.props}:
                raise ValueError(f"{shot.shot_id}: 道具不存在")
            ref = shot.reference_plan
            if ref.strategy != "independent_keyframe" and ref.based_on_shot_id not in previous:
                raise ValueError("构图参考必须指向更早的有效镜头")
            if shot.generation_plan:
                refs = shot.generation_plan.references
                if any(r.shot_id not in previous for r in refs):
                    raise ValueError("生成依赖必须指向更早的有效镜头")
                if len({(r.shot_id, r.kind) for r in refs}) != len(refs):
                    raise ValueError("生成依赖重复")
            previous.add(shot.shot_id)
            end += shot.duration_ms
        if end != self.total_duration_ms:
            raise ValueError("总时长与时间线不一致")
        return self


def unique(items, attr):
    values = [getattr(x, attr) for x in items]
    if len(set(values)) != len(values):
        raise ValueError(f"重复 ID: {attr}")


def validate_plan(characters: Characters, board: Storyboard):
    if characters.status != "ok" or board.status != "ok" or not board.shots:
        raise HarnessError("needs_review: 规划未就绪，请修正角色/分镜 JSON")
    chars = {c.character_id: c for c in characters.characters}
    for shot in board.shots:
        seen = set()
        for v in shot.visible_characters:
            c = chars.get(v.character_id)
            if c is None or not c.requires_visual_design or c.presence != "on_screen":
                raise HarnessError(f"needs_review: {shot.shot_id} 缺少可出镜角色 {v.character_id}")
            if v.character_id in seen:
                raise HarnessError(f"needs_review: {shot.shot_id} 重复角色")
            seen.add(v.character_id)
            if v.variant_id and v.variant_id not in {x.variant_id for x in c.variants}:
                raise HarnessError(f"needs_review: {shot.shot_id} 缺少变体 {v.variant_id}")
        for d in shot.audio.dialogue:
            if d.speaker_character_id is not None and d.speaker_character_id not in chars:
                raise HarnessError("needs_review: 对白角色不存在")
            if d.delivery == "on_screen" and d.speaker_character_id not in seen:
                raise HarnessError("needs_review: 画内对白说话人未出镜")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelChoice(StrictModel):
    task: str
    model_cls: str
    stage: str = "single_stage"
    parameters: dict[str, Any] = Field(default_factory=dict)
    media_field: str = "input_image"


class TextConfig(StrictModel):
    endpoint: str | None = None
    model: str | None = None
    key_env: str = "TEXT_LLM_API_KEY"
    timeout: float = Field(default=120, gt=0)
    max_tokens: int = Field(default=24576, ge=256, le=65536)
    enable_thinking: bool | None = None
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_calls: int = Field(default=2, ge=1, le=10)


class DurationPlanningConfig(StrictModel):
    enabled: bool = False
    target_tolerance_ms: int = Field(default=0, ge=0, le=60000)
    minimum_ms: int = Field(default=5000, gt=0, le=15000)
    maximum_ms: int = Field(default=15000, gt=0, le=15000)
    step_ms: int = Field(default=1000, gt=0, le=15000)
    chinese_chars_per_second: float = Field(default=3.5, gt=0)
    words_per_second: float = Field(default=2.5, gt=0)

    @model_validator(mode="after")
    def bounds(self):
        if self.minimum_ms > self.maximum_ms or self.minimum_ms % self.step_ms or self.maximum_ms % self.step_ms:
            raise ValueError("时长上下限必须有序并符合时长步长")
        return self


class PromptRewriterConfig(StrictModel):
    enabled: bool = False  # Missing field in historical runs retains their original requests.
    python: str = str(Path(__file__).resolve().parents[3] / "prompt-rewriter/.venv/Scripts/python.exe")
    script: str = str(Path(__file__).resolve().parents[3] / "prompt-rewriter/rewrite.py")
    model_root: str = str(Path(__file__).resolve().parents[4] / "models/Prompt-Rewriter")
    max_new_tokens: int = Field(default=768, ge=1, le=1024)
    max_input_tokens: int = Field(default=4096, ge=1, le=8192)
    cpu_layers: int = Field(default=4, ge=0, le=12)
    timeout_seconds: float = Field(default=1200, gt=0)
    input_mode: Literal["text_only", "first_frame", "first_last_frame"] = "text_only"
    image_max_pixels: int = Field(default=262144, ge=65536, le=1048576)
    content_policy: Literal["warn", "block"] = "warn"


class QualityConfig(StrictModel):
    end_frame_conditioning: bool = False
    # Historical runs retain their original graph. New CLI projects opt in.
    enabled: bool = False
    model: str = "qwen3-vl-plus"
    samples_per_second: int = Field(default=4, ge=1, le=8)
    sample_max_edge: int = Field(default=512, ge=256, le=1024)
    frames_per_review: int = Field(default=12, ge=4, le=24)
    max_calls: int = Field(default=80, ge=1, le=500)
    max_total_tokens: int = Field(default=300000, ge=1000)
    max_output_tokens: int = Field(default=2048, ge=512, le=4096)
    causal_motion_planning: bool = False
    independent_physics_review: bool = False
    require_operator_review: bool = False


class ImageRefinement(StrictModel):
    instructions: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
    check_fixed_camera: bool = False
    review_instructions: Annotated[str, StringConstraints(strip_whitespace=True, max_length=6000)] | None = None


class Config(StrictModel):
    aspect_ratio: str = "9:16"
    visual_style: str = "写实"
    target_duration_ms: int | None = Field(default=None, gt=0)
    max_shots: int | None = Field(default=None, ge=1)
    image: ModelChoice = Field(default_factory=lambda: ModelChoice(task="t2i", model_cls="gpt-image-2", parameters={"resolution_level": "1080p", "quality": "high"}))
    edit: ModelChoice = Field(default_factory=lambda: ModelChoice(task="i2i", model_cls="gpt-image-2", parameters={"resolution_level": "1080p", "quality": "high"}))
    video: ModelChoice = Field(default_factory=lambda: ModelChoice(task="i2av", model_cls="MiniMax-H3", parameters={"resolution_level": "768p", "enhance_prompt": False}))
    image_fallback: ModelChoice | None = Field(default_factory=lambda: ModelChoice(task="t2i", model_cls="Qwen-Image-2512", parameters={"resolution_level": "720p"}))
    edit_fallback: ModelChoice | None = Field(default_factory=lambda: ModelChoice(task="i2i", model_cls="Qwen-Image-Edit-2511"))
    sequential_shots: bool = True
    shot_scheduling: Literal["legacy", "dependency"] = "legacy"
    duration_planning: DurationPlanningConfig = Field(default_factory=DurationPlanningConfig)
    rewrite_ahead: bool = False
    require_object_states: bool = True
    character_reference_board: bool = True
    concurrency: int = Field(default=2, ge=1, le=32)
    request_interval_seconds: float = Field(default=0.2, ge=0)
    poll_interval_seconds: float = Field(default=5, gt=0)
    wait_timeout_seconds: float = Field(default=1800, gt=0)
    request_timeout_seconds: float = Field(default=60, gt=0)
    retries: int = Field(default=3, ge=0, le=8)
    budget_credits: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    execute_enabled: bool = False
    bindings: dict[str, str] = Field(default_factory=dict)
    image_refinements: dict[str, Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)] | ImageRefinement] = Field(default_factory=dict)
    prompt_refinements: dict[str, Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]] = Field(default_factory=dict)
    fps: int = Field(default=24, ge=1, le=120)
    export_width: int = Field(default=720, ge=16)
    export_height: int = Field(default=1280, ge=16)
    fit: Literal["pad", "crop"] = "pad"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    media_timeout_seconds: float = Field(default=600, gt=0)
    max_media_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    text: TextConfig = Field(default_factory=TextConfig)
    prompt_rewriter: PromptRewriterConfig = Field(default_factory=PromptRewriterConfig)
    prompt_rewriter_cpu_layers: dict[str, Annotated[int, Field(ge=0, le=12)]] = Field(default_factory=dict)
    quality: QualityConfig = Field(default_factory=QualityConfig)

    @model_validator(mode="after")
    def constraints(self):
        if self.prompt_rewriter_cpu_layers and not self.prompt_rewriter.enabled:
            raise ValueError("分镜内存配置需要启用本地 Rewriter")
        if self.quality.causal_motion_planning and not (self.quality.enabled and self.prompt_rewriter.enabled and self.prompt_rewriter.input_mode in {"first_frame", "first_last_frame"}):
            raise ValueError("实际首帧动作规划需要启用内容检查和图像条件 Rewriter")
        if self.quality.end_frame_conditioning and not (self.quality.enabled and self.video.model_cls == "MiniMax-H3" and self.prompt_rewriter.enabled and self.prompt_rewriter.input_mode == "first_last_frame"):
            raise ValueError("目标尾帧约束需要 MiniMax、内容检查和首尾帧 Rewriter")
        if self.prompt_rewriter.input_mode == "first_last_frame" and not self.quality.end_frame_conditioning:
            raise ValueError("首尾帧 Rewriter 需要启用目标尾帧生成与检查")
        if self.quality.require_operator_review and not self.quality.enabled:
            raise ValueError("人工/代理视频复核需要启用内容检查")
        if (self.image.task, self.edit.task) != ("t2i", "i2i") or self.video.task not in {"i2v", "i2av"}:
            raise ValueError("image/edit/video 任务必须为 t2i/i2i/i2v或i2av")
        if self.export_width % 2 or self.export_height % 2:
            raise ValueError("MP4 导出尺寸必须为偶数")
        if self.text.key_env.upper() == "X2V_API_KEY":
            raise ValueError("文本服务必须使用独立的密钥环境变量")
        if self.prompt_rewriter.enabled and self.video.model_cls == "MiniMax-H3" and self.video.parameters.get("enhance_prompt") is not False:
            raise ValueError("本地改写已启用时 MiniMax enhance_prompt 必须为 false，避免二次改写")
        for fallback, task in [(self.image_fallback, "t2i"), (self.edit_fallback, "i2i")]:
            if fallback and fallback.task != task:
                raise ValueError("图片回退模型任务必须与原任务一致")
        for role in [self.image, self.edit, self.video, self.image_fallback, self.edit_fallback]:
            if role is None:
                continue
            if any(k.lower() in {"api_key", "authorization", "token"} for k in role.parameters):
                raise ValueError("模型参数不得包含密钥")
        return self

    def check_board(self, board):
        if set(self.prompt_rewriter_cpu_layers) - {shot.shot_id for shot in board.shots}:
            raise HarnessError("Rewriter 内存配置指向未知分镜")
        if self.max_shots is not None and len(board.shots) > self.max_shots:
            raise HarnessError(f"分镜共 {len(board.shots)} 镜，超过配置上限 {self.max_shots}；请保留完整动作单元重新规划，未进入媒体生成")
        if self.aspect_ratio != board.aspect_ratio or self.visual_style != board.visual_style:
            raise HarnessError("配置的画幅/风格与分镜不一致，请显式对齐")
        if self.target_duration_ms and abs(self.target_duration_ms - board.total_duration_ms) > self.duration_planning.target_tolerance_ms:
            raise HarnessError("分镜片长与目标片长不一致")
        a, b = map(int, self.aspect_ratio.split(":"))
        if not math.isclose(self.export_width / self.export_height, a / b, rel_tol=0.02):
            raise HarnessError("导出宽高比与画幅不一致")


def load_plan(char_path, board_path):
    chars = Characters.model_validate(read_json(char_path))
    board = Storyboard.model_validate(read_json(board_path))
    validate_plan(chars, board)
    return chars, board


def load_config(path=None):
    data = read_json(path) if path else {}
    # CLI configuration is for new projects; persisted runs use Config directly.
    rewriting = data.setdefault("prompt_rewriter", {"enabled": True})
    rewriting.setdefault("input_mode", "first_frame")
    rewriting.setdefault("content_policy", "block")
    rewriting.setdefault("max_new_tokens", 1024)
    data.setdefault("shot_scheduling", "dependency")
    data.setdefault("duration_planning", {"enabled": True})
    data.setdefault("rewrite_ahead", True)
    data.setdefault("require_object_states", False)
    quality = data.setdefault("quality", {"enabled": True})
    quality.setdefault("model", "qwen3.8-max")
    if quality.get("enabled"):
        quality.setdefault("independent_physics_review", True)
        quality.setdefault("frames_per_review", 24)
        if rewriting.get("enabled") and rewriting["input_mode"] in {"first_frame", "first_last_frame"}:
            quality.setdefault("causal_motion_planning", True)
        if quality.get("end_frame_conditioning"):
            from .prompts import template
            data.setdefault("image_refinements", {"end_frame:*": {"instructions": template("endpoint_conservation"), "check_fixed_camera": True}})
    data.setdefault(
        "text", {"endpoint": "https://maas.qianwenaiapi.com/compatible-mode/v1/chat/completions", "model": "qwen3.7-plus", "key_env": "QWEN_API_KEY", "enable_thinking": False, "timeout": 300, "max_calls": 5}
    )
    cfg = Config.model_validate(data)
    if path:
        base = Path(path).resolve().parent
        cfg.bindings = {k: str((base / v).resolve()) if not v.startswith(("https://", "http://")) else v for k, v in cfg.bindings.items()}
    return cfg
