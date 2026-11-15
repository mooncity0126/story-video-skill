from __future__ import annotations

from importlib.resources import files
import re

from .util import digest

PROMPT_VERSION = "workflow-v0.1+harness-v1"


def template(name):
    return files("video_harness").joinpath("templates", name + ".md").read_text(encoding="utf-8-sig")


def prompt_version():
    return PROMPT_VERSION + ":" + digest({n: template(n) for n in ["characters", "storyboard", "character_image", "scene_image", "video", "object_continuity"]})[:16]


def character_prompt(c, style):
    return (
        f"视觉风格：{style}。单主体全身定妆照，正面平视、自然中性表情，头顶双手脚部完整，浅灰色简洁背景、柔和均匀光线。"
        f"角色基础外观：{c.visual_description}。一致性特征：{'；'.join(c.consistency_anchors)}。"
        "只画一个基础版本，不混入变体，不加入剧情、场景、额外饰品、文字、拼贴或水印。"
    )


def variant_prompt(c, variant, style):
    changes = "；".join(f"{key}：{value}" for key, value in variant.changes.items())
    return f"以实际传入的第一张基础定妆图为身份依据，保持同一人物、脸型五官发型和未声明变化的服装。风格：{style}。仅编辑以下变体变化：{changes}。保持单主体、简洁背景和中性表情，不添加剧情或文字。"


def scene_prompt(scene, earliest, props, style, aspect, character_names=(), *, layout_only=False):
    if layout_only:
        return (
            f"完全无人、没有人体或人物倒影的环境参考照片。{style}，{aspect}。平视广景。地点：{scene.location}；{scene.environment}。"
            f"空间布局：{'；'.join(scene.layout_anchors)}。时间：{scene.time_of_day or '遵循灯光设定'}；灯光：{scene.lighting}。"
            "只建立固定空间、家具、灯光和背景结构。桌面及操作面留空，剧情使用的可移动物品留给各镜首帧按其当前状态摆放。"
            "这张图不表现任何剧情时点、动作结果或人物，不添加文字、分格或水印。"
        )
    prop_text = "；".join(f"{p.name}，外观{p.appearance}，初始状态{p.initial_state}" for p in props)
    # Do not feed narrative character clauses to an environment-only generator.
    # A dedicated environment state can be supplied as a storyboard extension for complex prose.
    initial = getattr(scene, "environment_initial_state", None)
    if initial is None:
        names = tuple(character_names) + ("人物", "角色", "她", "他", "双手", "手持", "站在", "坐在")
        initial = "；".join(part.strip() for part in re.split(r"[，。；\n]", earliest.start_state) if part.strip() and not any(name in part for name in names if name))
    return (
        f"完全无人、没有任何人体或人物倒影的室内环境照片。{style}，{aspect}。平视广景。地点：{scene.location}；{scene.environment}。"
        f"空间布局：{'；'.join(scene.layout_anchors)}。时间：{scene.time_of_day or '遵循灯光设定'}；灯光：{scene.lighting}。"
        f"道具外观资料：{prop_text}。环境和道具初始状态：{initial}。"
        "保留空的活动空间。道具是否存在及其状态以该初始时点为准，后续才制作、破损或带入的物品不要提前出现。"
        "不出现剧情人物、手部、倒影或路人，不预画后续剧情结果，不加字幕、标签或水印。"
    )


def object_prompt(shot, props, *, motion=False):
    registry = {p.prop_id: p for p in props}
    descriptions = []
    for obj in shot.objects:
        prop = registry[obj.prop_id]
        if motion:
            state = f"起始{'在画' if obj.start_visible else '画外'}：{obj.start_description}；结束{'在画' if obj.end_visible else '画外'}：{obj.end_description}"
            changes = "；".join(x for x in [obj.entry_reason, obj.exit_reason, obj.transition_reason] if x)
            if changes:
                state += f"；进出画与衔接依据：{changes}"
        elif obj.start_visible:
            state = obj.start_description
        else:
            continue
        descriptions.append(f"{prop.prop_id}（{prop.name}）：固定外观{prop.appearance}；{state}")
    if not descriptions:
        return "本镜没有可见的独立物品。" if shot.no_objects_reason else ""
    return "本镜对象清单（逐项保持身份、数量、材质、容器、位置与状态）：" + "。".join(descriptions) + "。清单外不增加新的独立物品；被遮挡或离画的物品不复制、不瞬移。"


def first_frame_prompt(shot, chars, labels, style, aspect, props=(), previous=None, *, adaptive=False, state_only=False):
    camera = shot.camera
    identities = "；".join(f"{c.character_id}（{c.name}）：{'，'.join(c.consistency_anchors)}" for c in chars)
    prop_anchors = "；".join(f"{p.prop_id}（{p.name}）：{p.appearance}" for p in props)
    prop_text = (
        object_prompt(shot, props)
        if shot.objects or shot.no_objects_reason
        else (f"关键道具外观与数量：{prop_anchors}。容器、数量和破损状态严格服从本镜起始描述，不保留与当前时点冲突的旧道具状态。" if props else "")
    )
    continuity = ""
    if state_only:
        return (
            f"图像编辑：各输入图依次为{'；'.join(labels)}。生成{aspect}、{style}的单幅静态首帧。"
            f"人物身份：{identities or '无人'}。此刻唯一的物品与人物状态依据：{shot.start_state}。"
            f"景别：{camera.shot_size}；角度：{camera.angle}。相关物品固定外观：{prop_anchors or '按起始状态'}。"
            "画面冻结在本镜动作开始之前。物品的数量、容器内是否有内容、完整或破损状态严格采用上述起始状态，固定外观资料仅提供材质与风格。"
            "人物参考用于身份，空场景参考只用于空间布局和灯光。实际镜头参考提供已有状态；本镜明确声明的起始变化以本镜为准。"
            "不要预演或提前完成后续动作；未明确由人物拿着的物品应有正常支撑。只生成一幅画面，不加字幕或标签。"
        )
    if adaptive:
        return (
            f"图像编辑：各输入图依次为{'；'.join(labels)}。生成{aspect}、{style}的单幅镜头起始画面。"
            f"画面人物：{identities or '无人'}。"
            f"起始画面：{shot.first_frame_description}。起始状态：{shot.start_state}。"
            f"取景：{camera.shot_size}；角度：{camera.angle}；构图：{camera.composition}。"
            f"相关道具外观：{prop_anchors or '按场景自然呈现'}。"
            "人物参考用于身份，场景参考用于空间；附带镜头参考时按其标注用途衔接，当前状态以本镜描述为准。"
        )
    if shot.first_frame_reference_policy == "planned_frame":
        cast = "；".join(f"{c.name}：{'，'.join(c.consistency_anchors)}" for c in chars)
        cast_scope = f"仅使用本镜声明的{len(chars)}个人物身份：{cast}。人物仅按本镜取景露出身体部位，不把定妆照全身或参考板排版搬入画面。" if chars else "本镜无人。"
        continuity = "第一张输入是上一镜实际尾帧，用作本镜仍在画人物和物品的衔接参考。" if previous else ""
        return (
            f"图像编辑：各输入图依次为{'；'.join(labels)}。生成{aspect}、{style}的单幅镜头起始画面。"
            f"{cast_scope}本镜起始要求：{shot.first_frame_description}。当前物理状态：{shot.start_state}。"
            f"取景：{camera.shot_size}；角度：{camera.angle}；构图：{camera.composition}。"
            f"{prop_text}{continuity}"
            "本镜声明的取景、出镜人物和物品状态优先；前镜生成图中的额外人物、意外物品或错误状态不沿用。"
            "仅把当前清单中可见的独立物品画入取景，画外物品保持画外，不因前镜尾帧或场景参考曾出现而塞进近景。"
            "人物参考负责身份，场景参考只负责固定布局和光线；只参考相邻上一镜，不预画后续镜头。"
            "仅表现动作发生前的一个时点，不执行本镜动作，不画动作结束结果、分格、参考标签、字幕或水印。"
        )
    if previous:
        continuity = f"相邻镜头衔接：上一镜 {previous.shot_id} 结束状态为{previous.end_state}。第一张参考图是上一镜实际采用片段的尾帧。"
        if shot.transition_from_previous == "scene_change":
            continuity += "本镜明确切换场景，按本镜环境参考建立新空间；尾帧仅辅助人物与光线风格衔接，不将旧房间或已离画物品带入新场景。"
        else:
            continuity += "同场景以尾帧的最新状态为依据，只执行本镜明确声明的姿态、机位与物品变化；环境参考仅约束固定布局，禁止将物品恢复为场景图里的旧状态。"
    return (
        f"图像编辑：使用实际附带的全部参考图。按传入顺序绑定：{'；'.join(labels)}。{style}，{aspect}。"
        f"保持角色身份：{identities}。起始画面：{shot.first_frame_description}。起始状态：{shot.start_state}。"
        f"摄影：{camera.shot_size}，{camera.angle}；构图：{camera.composition}。"
        f"{prop_text}"
        f"{continuity}"
        "场景参考负责空间、灯光和环境布局，角色参考负责身份；按当前起始状态调整人物姿态与道具。"
        "仅生成一个独立起始时点，不描绘镜尾状态、连续动作、分格、字幕或标签。"
    )


def motion_prompt(shot, names, audio_supported, props=(), *, timing_settings=None, natural=False):
    if shot.video_prompt is not None:
        # Authored model-native prompts are complete payloads, not fragments to
        # be wrapped again with business objects, sound defaults or constraints.
        return shot.video_prompt
    if natural:
        text = (
            f"{shot.camera.shot_size}，{shot.camera.angle}，{shot.camera.movement}。{shot.start_state}。{shot.action_description}。结束时：{shot.end_state}。镜头时长 {shot.duration_ms / 1000:g} 秒。"
        )
        if audio_supported:
            spoken = "；".join(f"{names.get(d.speaker_character_id, '旁白')}：‘{d.text}’" for d in shot.audio.dialogue)
            text += f"对白：{spoken or '无'}。声音：{'；'.join(shot.audio.sound_effects) or '自然环境声'}。音乐：{shot.audio.music or '无'}。"
        return text
    text = (
        f"从输入的真实首帧开始，保持人物身份、服装、人数、空间、灯光和画风。"
        f"起始状态：{shot.start_state}。主要动作与表演：{shot.action_description}。"
        f"运镜：{shot.camera.movement}。结束状态：{shot.end_state}。"
        f"连续性：{'；'.join(shot.continuity_notes)}。单个连续镜头，动作自然、不加速、不增加转场。"
        f"{object_prompt(shot, props, motion=True) if shot.objects or shot.no_objects_reason else ''}"
    )
    if shot.timing is not None:
        from .models import DurationPlanningConfig
        from .planning import shot_timing

        _, timing = shot_timing(shot, timing_settings or DurationPlanningConfig())
        text += (
            f"镜头总时长 {shot.duration_ms / 1000:g} 秒。动作时间安排："
            + "；".join(f"{b['start_ms'] / 1000:g}—{b['end_ms'] / 1000:g}秒：{b['description']}" for b in timing.get("beats", {}).values())
            + "。未安排动作的剩余时间保持结束状态，不重复动作。"
        )
    if audio_supported:
        spoken = "；".join(f"{names.get(d.speaker_character_id, '旁白')}（{d.delivery}）：“{d.text}”" for d in shot.audio.dialogue)
        text += f"对白仅使用：{spoken or '无对白'}。声音：{'；'.join(shot.audio.sound_effects)}。音乐：{shot.audio.music or '无'}。"
    elif shot.audio.dialogue:
        text += "按分镜表现说话动作，声音另行制作。"
    return text + "不新增台词、字幕、文字或水印。"


def grounded_motion_prompt(shot, characters, scene, props, *, audio_supported=True):
    """Supply compact canonical facts to the image-conditioned rewriter."""
    cast = []
    for visible in shot.visible_characters:
        character = next(c for c in characters if c.character_id == visible.character_id)
        description = "、".join(character.consistency_anchors)
        if visible.variant_id:
            variant = next(v for v in character.variants if v.variant_id == visible.variant_id)
            description += "；本镜状态：" + "、".join(str(v) for v in variant.changes.values())
        cast.append(f"{character.name}：{description}")
    wanted = set(shot.focus_prop_ids) | {o.prop_id for o in shot.objects}
    anchors = "；".join(f"{p.name}：{p.appearance}" for p in props if p.prop_id in wanted)
    context = (
        "基于附带的实际首帧生成一个连续镜头。固定人物设定：" + ("；".join(cast) or "本镜无人") + "。"
        f"固定场景：{scene.location}；{scene.environment}；灯光：{scene.lighting}。"
        f"关键物品外观：{anchors or '按实际首帧'}。物品的当前状态以首帧和本镜起始状态为准。"
    )
    return context + motion_prompt(shot, {c.character_id: c.name for c in characters}, audio_supported, props, natural=True)


def end_frame_prompt(shot, style, aspect):
    """Generate an endpoint from the accepted actual start, never a replacement video tail."""
    return (
        f"图像编辑：输入图是这个连续镜头的真实首帧。生成{aspect}、{style}的单幅目标结束帧。"
        f"只完成这一动作后的状态：{shot.action_description}。最终状态：{shot.end_state}。"
        f"摄影运动：{shot.camera.movement}。人物身份、面部、发型、衣着、房间、灯光、固定家具和未参与动作的物品保持输入图一致。"
        "只改变动作要求改变的姿态、位置与材料状态。所有物品都要有合理支撑；保持原有物体数量，除非该动作明确发生分裂或合并。"
        "材料变化的外形必须符合原因：撞击或撕裂形成粗糙、参差、不对称的断面与碎屑，不能画成刀切的整齐切面或均等几何扇形。"
        "动作结束后已放下的物品必须放在真实表面上。仅绘制结束的一个时点，不画动作轨迹、拼贴、字幕或标签。"
    )
