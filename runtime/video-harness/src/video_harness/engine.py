from __future__ import annotations

import shutil
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import prompts
from .client import APIError, SubmissionUnknown, media_input
from .camera import is_locked_camera, review_fixed_camera
from .contracts import discover
from .continuity import review_objects
from .media import asset_record, check_clip, executable, extract_timeline_tail, identity_board, probe
from .models import Config, ImageRefinement, load_plan
from .planning import check_runtime_duration, prepare_board, shot_dependencies
from .rewriter import LocalPromptRewriter
from .quality import REVIEW_INSTRUCTIONS, VisualReviewer, aggregate_verdicts, initial_state_contract, reference_details, sample_video, shot_contract, video_segment_data
from .motion import MOTION_INSTRUCTIONS, VisualMotionPlanner, rewrite_motion_context
from .operator_review import decision_pointer, load_decision, save_decision
from .store import Store
from .util import BudgetError, HarnessError, PauseRequested, canonical, digest, file_hash, inside, now, read_json, safe_error, write_json


def grouped_media(node, choice, entries):
    fields = node["data"].get("media_fields")
    if fields:
        if len(fields) != len(entries):
            raise HarnessError("首尾帧媒体绑定不完整，不能丢弃或错置参考图")
        return [(field, [entry]) for field, entry in zip(fields, entries, strict=True)]
    return [(choice.media_field, entries)] if entries else []


def quote_media(contract, body, node, choice, metas):
    groups = grouped_media(node, choice, metas)
    last = next((group[0] for field, group in groups if field == "input_last_frame"), None)
    primary = [entry for field, group in groups if field != "input_last_frame" for entry in group]
    return contract.quote_body(body, primary, last_frame_meta=last)


def create_run(script, characters, storyboard, config, output_root, client=None):
    chars, board = load_plan(characters, storyboard)
    board, planning_report = prepare_board(board, config)
    config.check_board(board)
    review_objects(board, config.require_object_states)
    script = Path(script)
    if not script.read_text(encoding="utf-8-sig").strip():
        raise HarnessError("剧本为空")
    run_id = "RUN_" + uuid.uuid4().hex
    root = Path(output_root).resolve() / run_id
    root.mkdir(parents=True, exist_ok=False)
    for folder in ["characters", "scenes", "first_frames", "end_frames", "tail_frames", "reference_boards", "videos", "bindings", "logs", "exports"]:
        (root / folder).mkdir()
    shutil.copyfile(script, root / "script.md")
    write_json(root / "characters.json", chars.model_dump())
    write_json(root / "storyboard.json", board.model_dump())
    write_json(root / "planning-report.json", planning_report)
    write_json(root / "run.json", {"run_id": run_id, "created_at": now(), "script_sha256": file_hash(script), "planning_source": "imported_external_json", "prompt_version": prompts.prompt_version()})
    cfg = config.model_copy(deep=True)
    allowed = {"scene:" + s.scene_id for s in board.scenes}
    allowed.update("frame:" + s.shot_id for s in board.shots)
    allowed.update("character:" + c.character_id + ":base" for c in chars.characters)
    allowed.update("character:" + c.character_id + ":" + v.variant_id for c in chars.characters for v in c.variants)
    if set(cfg.bindings) - allowed:
        raise HarnessError("bindings 中包含未知角色、变体、场景或首帧 ID")
    for node_id, source in list(cfg.bindings.items()):
        target = root / "bindings" / (node_id.replace(":", "_") + ".image")
        if source.startswith(("http://", "https://")):
            if client is None:
                raise HarnessError("URL 素材绑定需要下载客户端")
            client.download_url(source, target)
        else:
            shutil.copyfile(source, target)
        probe(target, cfg, "image")
        cfg.bindings[node_id] = target.relative_to(root).as_posix()
    write_json(root / "config.json", cfg.model_dump())
    return root


def build_graph(chars, board, config):
    review_objects(board, config.require_object_states)
    nodes = {}
    char_map = {c.character_id: c for c in chars.characters}
    names = {c.character_id: c.name for c in chars.characters}
    adaptive = config.shot_scheduling == "dependency"
    decisions = shot_dependencies(board) if adaptive else {}

    def add(node_id, kind, role, prompt, deps=None, **data):
        provided = config.bindings.get(node_id)
        nodes[node_id] = {
            "kind": "provided" if provided else kind,
            "role": role,
            "prompt": prompt,
            "deps": [] if provided else (deps or []),
            "binding": provided,
            "data": data,
            "prompt_version": prompts.prompt_version(),
        }

    used = {(v.character_id, v.variant_id) for s in board.shots for v in s.visible_characters}
    for cid in sorted({cid for cid, _ in used}):
        c = char_map[cid]
        need_base = (cid, None) in used or any("character:" + cid + ":" + vid not in config.bindings for cc, vid in used if cc == cid and vid)
        base_id = "character:" + cid + ":base"
        if need_base:
            add(base_id, "character", "image", prompts.character_prompt(c, board.visual_style), character=c.model_dump())
        for v in c.variants:
            if (cid, v.variant_id) in used:
                add("character:" + cid + ":" + v.variant_id, "character", "edit", prompts.variant_prompt(c, v, board.visual_style), [base_id], character_id=cid, variant=v.model_dump())
    for scene in board.scenes:
        shots = sorted([s for s in board.shots if s.scene_id == scene.scene_id], key=lambda s: s.timeline_in_ms)
        if not shots:
            continue
        relevant = {p for s in shots for p in s.focus_prop_ids} | {o.prop_id for s in shots for o in s.objects}
        props = [p for p in board.props if p.prop_id in relevant]
        add(
            "scene:" + scene.scene_id,
            "scene",
            "image",
            prompts.scene_prompt(scene, shots[0], props, board.visual_style, board.aspect_ratio, [name for c in chars.characters for name in [c.name, *c.aliases]], layout_only=config.quality.enabled),
            scene=scene.model_dump(),
            initial_state=None if config.quality.enabled else shots[0].start_state,
            props=[] if config.quality.enabled else [p.model_dump() for p in props],
        )
    previous = None
    for shot in sorted(board.shots, key=lambda s: s.timeline_in_ms):
        deps = ["scene:" + shot.scene_id]
        labels = [f"环境 {shot.scene_id}"]
        cast_deps, cast_labels = [], []
        for v in shot.visible_characters:
            cast_deps.append("character:" + v.character_id + ":" + (v.variant_id or "base"))
            cast_labels.append(f"角色 {v.character_id}（{char_map[v.character_id].name}），版本 {v.variant_id or 'base'}")
        if (config.sequential_shots or adaptive) and config.character_reference_board and len(cast_deps) > 1:
            cast_id = "cast:" + shot.shot_id
            nodes[cast_id] = {
                "kind": "reference_board",
                "role": None,
                "deps": cast_deps,
                "prompt": "labelled identity reference board",
                "data": {"labels": cast_deps},
                "binding": None,
                "prompt_version": "identity-board-v1",
            }
            deps.append(cast_id)
            labels.append("人物参考板，按从左到右、从上到下顺序：" + "；".join(cast_labels) + "。参考板用于分别识别人脸和服装，输出仍为单幅实景")
        else:
            deps.extend(cast_deps)
            labels.extend(cast_labels)
        ref = shot.reference_plan
        if adaptive:
            visual_labels, visual_deps = [], []
            for reference in decisions[shot.shot_id]["references"]:
                source = next(s for s in board.shots if s.shot_id == reference["shot_id"])
                source_id = reference["kind"] + ":" + source.shot_id
                if reference["kind"] == "tail":
                    nodes[source_id] = {
                        "kind": "tail",
                        "role": None,
                        "deps": ["video:" + source.shot_id],
                        "prompt": "extract last retained timeline frame",
                        "data": {"duration_ms": source.duration_ms, "source_in_ms": 0},
                        "binding": None,
                        "prompt_version": "timeline-tail-v2",
                    }
                    visual_labels.append(f"分镜 {source.shot_id} 实际采用片段的尾帧：{reference['reason']}")
                else:
                    visual_labels.append(f"分镜 {source.shot_id} 首帧构图参考：{reference['reason']}；当前状态以本镜为准")
                visual_deps.append(source_id)
            deps = visual_deps + deps
            labels = visual_labels + labels
        elif config.sequential_shots and previous:
            tail_id = "tail:" + previous.shot_id
            nodes[tail_id] = {
                "kind": "tail",
                "role": None,
                "deps": ["video:" + previous.shot_id],
                "prompt": "extract last retained timeline frame",
                "data": {"duration_ms": previous.duration_ms, "source_in_ms": 0},
                "binding": None,
                "prompt_version": "timeline-tail-v2",
            }
            deps.insert(0, tail_id)
            labels.insert(0, f"前一分镜 {previous.shot_id} 实际尾帧，最新状态依据")
        elif not config.sequential_shots and ref.strategy == "reuse_composition":
            deps.append("frame:" + ref.based_on_shot_id)
            labels.append("此前镜头构图，只参考构图，当前状态以本镜为准")
        elif not config.sequential_shots and ref.strategy == "continue_from_previous":
            tail_id = "tail:" + ref.based_on_shot_id
            nodes[tail_id] = {"kind": "tail", "role": None, "deps": ["video:" + ref.based_on_shot_id], "prompt": "extract final frame", "data": {}, "binding": None, "prompt_version": "ffmpeg-tail-v1"}
            deps.append(tail_id)
            labels.append("前镜实际尾帧，衔接状态")
        add(
            "frame:" + shot.shot_id,
            "frame",
            "edit",
            prompts.first_frame_prompt(
                shot,
                [char_map[v.character_id] for v in shot.visible_characters],
                labels,
                board.visual_style,
                board.aspect_ratio,
                [p for p in board.props if p.prop_id in (set(shot.focus_prop_ids) | {o.prop_id for o in shot.objects})],
                previous if config.sequential_shots and not adaptive else None,
                adaptive=adaptive,
                state_only=config.quality.enabled,
            ),
            deps,
            shot_id=shot.shot_id,
            description=shot.first_frame_description,
            start_state=shot.start_state,
            camera=shot.camera.model_dump(),
            labels=labels,
        )
        if shot.focus_prop_ids:
            nodes["frame:" + shot.shot_id]["prompt_version"] += ":prop-anchors-v2"
        add(
            "video:" + shot.shot_id,
            "video",
            "video",
            prompts.motion_prompt(shot, names, config.video.task == "i2av", board.props, timing_settings=config.duration_planning, natural=adaptive),
            ["frame:" + shot.shot_id],
            shot_id=shot.shot_id,
            duration_ms=shot.duration_ms,
            timeline_in_ms=shot.timeline_in_ms,
            audio=shot.audio.model_dump(),
        )
        if adaptive:
            nodes["frame:" + shot.shot_id]["data"]["generation_decision"] = decisions[shot.shot_id]
        if config.prompt_rewriter.enabled and config.video.model_cls == "MiniMax-H3" and config.prompt_rewriter.input_mode in {"first_frame", "first_last_frame"}:
            nodes["video:" + shot.shot_id]["prompt"] = prompts.grounded_motion_prompt(
                shot, chars.characters, next(s for s in board.scenes if s.scene_id == shot.scene_id), board.props, audio_supported=config.video.task == "i2av"
            )
        if (config.rewrite_ahead or config.quality.enabled) and config.prompt_rewriter.enabled and config.video.model_cls == "MiniMax-H3":
            rewrite_id = "rewrite:" + shot.shot_id
            rewrite_deps = ["frame:" + shot.shot_id] if config.prompt_rewriter.input_mode in {"first_frame", "first_last_frame"} else []
            add(rewrite_id, "prompt_rewrite", None, nodes["video:" + shot.shot_id]["prompt"], rewrite_deps, shot_id=shot.shot_id, duration_ms=shot.duration_ms)
            nodes["video:" + shot.shot_id]["deps"].append(rewrite_id)
        if shot.objects:
            nodes["frame:" + shot.shot_id]["prompt_version"] += ":objects-v3"
            if shot.video_prompt is None:
                nodes["video:" + shot.shot_id]["prompt_version"] += ":objects-v3"
        if shot.video_prompt is not None:
            nodes["video:" + shot.shot_id]["prompt_version"] += ":" + shot.video_prompt_version
        previous = shot
    if config.quality.enabled:
        # Review outputs are control dependencies, never extra images sent to a generator.
        for shot in board.shots:
            sid = shot.shot_id
            frame_id, video_id, rewrite_id = "frame:" + sid, "video:" + sid, "rewrite:" + sid
            frame_check, prompt_check, video_check = (f"check:{stage}:{sid}" for stage in ("frame", "prompt", "video"))
            canonical_refs = ["scene:" + shot.scene_id] + ["character:" + v.character_id + ":" + (v.variant_id or "base") for v in shot.visible_characters]
            context_refs = [d for d in nodes[frame_id]["deps"] if d.startswith(("tail:", "frame:"))]
            common = {"shot_id": sid, "contract": shot_contract(shot, chars.characters, next(s for s in board.scenes if s.scene_id == shot.scene_id), board.props)}
            add(frame_check, "quality_review", None, REVIEW_INSTRUCTIONS, list(dict.fromkeys([frame_id, *canonical_refs, *context_refs])), stage="frame", **common)
            motion_id = "motion:" + sid
            if config.quality.causal_motion_planning:
                add(motion_id, "motion_plan", None, MOTION_INSTRUCTIONS, [frame_id, frame_check], **common)
            end_id, end_check = "end_frame:" + sid, "check:end_frame:" + sid
            end_deps = []
            if config.quality.end_frame_conditioning:
                add(end_id, "end_frame", "edit", prompts.end_frame_prompt(shot, board.visual_style, board.aspect_ratio),
                    [frame_id, frame_check] + ([motion_id] if config.quality.causal_motion_planning else []), **common)
                add(end_check, "quality_review", None, REVIEW_INSTRUCTIONS, [frame_id, end_id, frame_check], stage="end_frame", **common)
                end_deps = [end_id, end_check]
                nodes[video_id]["deps"].append(end_id)
                nodes[video_id]["data"]["media_fields"] = ["input_image", "input_last_frame"]
            if rewrite_id in nodes:
                nodes[rewrite_id]["deps"].append(frame_check)
                if config.quality.causal_motion_planning:
                    nodes[rewrite_id]["deps"].append(motion_id)
                nodes[rewrite_id]["deps"].extend(end_deps)
            add(prompt_check, "quality_review", None, REVIEW_INSTRUCTIONS, [frame_id, frame_check] + ([rewrite_id] if rewrite_id in nodes else []) + ([motion_id] if config.quality.causal_motion_planning else []) + end_deps,
                stage="prompt", candidate_prompt=nodes[video_id]["prompt"], **common)
            nodes[video_id]["deps"].append(prompt_check)
            add(video_check, "quality_review", None, REVIEW_INSTRUCTIONS, [video_id, frame_id, frame_check, prompt_check] + end_deps, stage="video", **common)
            if config.quality.require_operator_review:
                add("check:operator:" + sid, "operator_review", None, "Explicit review of this immutable video candidate", [video_id, video_check], **common)
        for node in nodes.values():
            if node["kind"] == "tail":
                source_sid = node["deps"][0].split(":", 1)[1]
                node["deps"].append("check:video:" + source_sid)
                if config.quality.require_operator_review:
                    node["deps"].append("check:operator:" + source_sid)
            elif node["kind"] == "frame":
                for dep in list(node["deps"]):
                    if dep.startswith("frame:"):
                        node["deps"].append("check:frame:" + dep.split(":", 1)[1])
    # Scoped repair feedback changes only the selected image recipe and its check.
    # Previously accepted, unrelated nodes retain their exact input identities.
    for selector, refinement in config.image_refinements.items():
        feedback = refinement if isinstance(refinement, str) else refinement.instructions
        review_feedback = feedback if isinstance(refinement, str) or refinement.review_instructions is None else refinement.review_instructions
        camera_check = not isinstance(refinement, str) and refinement.check_fixed_camera
        matched = [key for key in nodes if key.startswith(selector[:-1])] if selector in {"frame:*", "end_frame:*"} else [selector] if selector in nodes else []
        if not matched or any(nodes[key]["kind"] not in {"frame", "end_frame"} for key in matched):
            raise HarnessError("image_refinements 只能指向已存在且未绑定的 frame/end_frame 节点")
        if camera_check and (not config.quality.enabled or any(nodes[key]["kind"] != "end_frame" for key in matched)):
            raise HarnessError("固定机位首尾检查只适用于启用质量检查的 end_frame 节点")
        for key in matched:
            node = nodes[key]
            node["prompt"] += "\n图像生成修正要求：" + feedback
            node["data"].setdefault("image_refinements", []).append(feedback)
            if config.quality.enabled:
                if review_feedback:
                    nodes["check:" + key]["data"].setdefault("image_refinements", []).append(review_feedback)
                if camera_check and is_locked_camera(node["data"]["contract"]["camera"]["movement"]):
                    nodes["check:" + key]["data"]["fixed_camera_check"] = {"implementation_sha256": file_hash(Path(__file__).with_name("camera.py"))}
    for selector, feedback in config.prompt_refinements.items():
        if selector not in nodes or nodes[selector]["kind"] != "prompt_rewrite":
            raise HarnessError("prompt_refinements 只能指向已启用的 rewrite 节点")
        # Historical findings are input to a NEW model inference, never evidence
        # about that new candidate in the independent acceptance review.
        nodes[selector]["data"]["prompt_refinements"] = feedback
    return nodes


class Runner:
    def __init__(self, root, client, *, config=None, contracts=None):
        self.root = Path(root).resolve()
        self.config = config or Config.model_validate(read_json(self.root / "config.json"))
        self.chars, self.board = load_plan(self.root / "characters.json", self.root / "storyboard.json")
        self.config.check_board(self.board)
        self.object_review = review_objects(self.board, self.config.require_object_states)
        write_json(self.root / "continuity-review.json", self.object_review)
        self.client = client
        self.contracts = contracts
        self.store = Store(self.root)
        self.graph = build_graph(self.chars, self.board, self.config)
        self._rewriter = None
        self._scoped_rewriters = {}
        self.store.sync_nodes(self.graph)

    def close(self):
        self.store.close()

    def check_pause(self):
        if (self.root / "PAUSE_REQUESTED").exists():
            raise PauseRequested("pause_requested: 已保存任务状态，移除 PAUSE_REQUESTED 后可 resume；不会重提已知 task_id")

    def capabilities(self):
        if self.contracts is None:
            self.contracts = discover(self.client, self.config)
            write_json(self.root / "model-contracts.json", {role: c.spec for role, c in self.contracts.items()})
            for contract in self.contracts.values():
                write_json(self.root / "contracts" / (digest(contract.spec) + ".json"), contract.spec)
            write_json(self.root / "openapi.snapshot.json", self.contracts["image"].openapi)
        if self.config.quality.end_frame_conditioning:
            for field in ("input_image", "input_last_frame"):
                slot = self.contracts["video"].inputs.get(field, {})
                if slot.get("type") != "image" or slot.get("maximum", 1) < 1:
                    raise HarnessError("当前视频模型契约不支持所需首尾帧字段 " + field)
        return self.contracts

    def _valid_output(self, attempt):
        if not attempt or attempt["status"] != "succeeded" or not attempt["output"]:
            return False
        path = inside(self.root, attempt["output"]["path"])
        valid = path.is_file() and file_hash(path) == attempt["output"]["sha256"]
        if valid and attempt["output"]["kind"] == "text":
            record = read_json(path)
            for name, key in [("model-output.json", "raw_output_sha256"), ("minimax-prompt.txt", "prompt_file_sha256")]:
                artifact = path.parent / name
                valid = valid and artifact.is_file() and file_hash(artifact) == record[key]
        return valid

    def _dependency_outputs(self, node):
        outputs = []
        for dep in node["deps"]:
            a = self.store.current(dep)
            if not self._valid_output(a):
                raise HarnessError(f"依赖尚未就绪: {dep}")
            outputs.append(a["output"])
        return outputs

    def current_inputs(self):
        """Check transitive provenance without generating or mutating candidates."""
        valid = {}
        pending = set(self.graph)
        while pending:
            ready = [n for n in pending if all(d in valid for d in self.graph[n]["deps"])]
            if not ready:
                raise HarnessError("节点依赖有环或缺失")
            for node_id in ready:
                a = self.store.current(node_id)
                deps = self.graph[node_id]["deps"]
                valid[node_id] = False
                if self._valid_output(a) and all(valid[d] for d in deps):
                    try:
                        outputs = [self.store.current(d)["output"] for d in deps]
                        valid[node_id] = a["input_hash"] == self._input_hash(node_id, outputs)
                    except OSError:
                        pass
                pending.remove(node_id)
        return valid

    def check_assembly_inputs(self):
        valid = self.current_inputs()
        if any(not valid["video:" + s.shot_id] for s in self.board.shots):
            raise HarnessError("视频或其上游输入已变化/未就绪，请先 resume；assemble 不会调用生成模型")
        if self.config.quality.enabled and any(not valid["check:video:" + s.shot_id] for s in self.board.shots):
            raise HarnessError("视频内容尚未通过验收，不能拼接或交付为合格成片")
        if self.config.quality.require_operator_review and any(not valid["check:operator:" + s.shot_id] for s in self.board.shots):
            raise HarnessError("视频尚未完成人工/代理复核，不能拼接")

    def record_operator_decision(self, shot_id, *, decision, notes, reviewer):
        if shot_id not in {s.shot_id for s in self.board.shots}:
            raise HarnessError("复核镜头不存在")
        attempt = self.store.current("video:" + shot_id)
        if not self._valid_output(attempt):
            raise HarnessError("当前镜头没有完整可验证的视频候选")
        if decision == "approve":
            valid = self.current_inputs()
            if not valid["video:" + shot_id] or (self.config.quality.enabled and not valid["check:video:" + shot_id]):
                raise HarnessError("不能以人工批准绕过失败或过期的自动内容检查")
        record = save_decision(self.root, shot_id, attempt, decision=decision, notes=notes, reviewer=reviewer)
        self.store.event("video:" + shot_id, "operator_review_recorded", decision)
        self.export()
        return record

    def _input_hash(self, node_id, deps):
        node = self.graph[node_id]
        role = node["role"]
        choice = getattr(self.config, role).model_dump() if role else None
        binding = node.get("binding")
        data = {
            "recipe": node,
            "choice": choice,
            "fallback_choice": getattr(self.config, role + "_fallback").model_dump() if role in {"image", "edit"} and getattr(self.config, role + "_fallback") else None,
            "aspect_ratio": self.board.aspect_ratio,
            "revision": self.store.revision(node_id),
            "deps": [{"sha256": d["sha256"], "task_id": d["task_id"], "asset_id": d["asset_id"]} for d in deps],
        }
        if binding:
            data["provided_hash"] = file_hash(inside(self.root, binding))
        if node["kind"] == "operator_review":
            pointer = decision_pointer(self.root, node["data"]["shot_id"])
            data["operator_decision_pointer"] = file_hash(pointer) if pointer.is_file() else None
            if pointer.is_file():
                path = inside(self.root, read_json(pointer)["record_path"])
                data["operator_decision_record"] = file_hash(path) if path.is_file() else None
        rewriter = self.video_rewriter(node)
        if rewriter:
            data["prompt_rewriter"] = rewriter.identity
        if node["kind"] in {"quality_review", "motion_plan"}:
            data["quality"] = self.config.quality.model_dump()
            data["review_service"] = self.review_config().model_dump()
            data["review_implementation"] = file_hash(Path(__file__).with_name("quality.py"))
            if node["kind"] == "motion_plan":
                data["motion_implementation"] = file_hash(Path(__file__).with_name("motion.py"))
        return digest(data)

    def review_config(self):
        return self.config.text.model_copy(update={"model": self.config.quality.model, "temperature": 0,
                                                  "max_tokens": self.config.quality.max_output_tokens, "enable_thinking": False, "max_calls": 10})

    def video_rewriter(self, node):
        if node["kind"] not in {"video", "prompt_rewrite"} or self.config.video.model_cls != "MiniMax-H3" or not self.config.prompt_rewriter.enabled:
            return None
        layers = self.config.prompt_rewriter_cpu_layers.get(node["data"]["shot_id"], self.config.prompt_rewriter.cpu_layers)
        if layers != self.config.prompt_rewriter.cpu_layers:
            # A longer repair input may need more CPU placement. Scope this
            # inference identity to the affected shot, preserving accepted ones.
            if layers not in self._scoped_rewriters:
                config = self.config.prompt_rewriter.model_copy(update={"cpu_layers": layers})
                self._scoped_rewriters[layers] = LocalPromptRewriter(config, proper_names=[c.name for c in self.chars.characters])
            return self._scoped_rewriters[layers]
        if self._rewriter is None:
            self._rewriter = LocalPromptRewriter(self.config.prompt_rewriter, proper_names=[c.name for c in self.chars.characters])
        return self._rewriter

    def dry_run(self, quote=True):
        self.capabilities()
        reusable = self.current_inputs()
        report, predictions = [], {}
        pending = set(self.graph)
        while pending:
            ready = sorted(n for n in pending if all(d in predictions for d in self.graph[n]["deps"]))
            if not ready:
                raise HarnessError("节点依赖有环或缺失")
            for node_id in ready:
                node = self.graph[node_id]
                current = self.store.current(node_id)
                metas = [predictions[d] for d in node["deps"] if self.graph[d]["kind"] not in {"prompt_rewrite", "quality_review", "motion_plan", "operator_review"}]
                item = {
                    "node_id": node_id,
                    "depends_on": node["deps"],
                    "reference_bindings": node.get("data", {}).get("labels", node["deps"]),
                    "kind": node["kind"],
                    "current_task_id": current["task_id"] if current else None,
                    "quoted_credits": None,
                    "quote_basis": "actual_media" if all(m and not m.get("estimated") for m in metas) else "estimated_dependency_dimensions",
                }
                item["reusable"] = reusable[node_id]
                if node["kind"] == "provided":
                    predictions[node_id] = probe(inside(self.root, node["binding"]), self.config, "image")
                    item["binding"] = node["binding"]
                elif node["kind"] == "reference_board":
                    count = len(metas)
                    predictions[node_id] = {"width": min(3, count) * 512, "height": ((count + 2) // 3) * 808, "estimated": True}
                elif node["kind"] == "tail":
                    predictions[node_id] = metas[0]
                elif node["kind"] == "prompt_rewrite":
                    contract = self.contracts["video"]
                    body = contract.parameters(self.config.video, node["prompt"], self.board.aspect_ratio, node["data"]["duration_ms"])
                    check_runtime_duration(node, body, contract, self.config)
                    predictions[node_id] = {"kind": "text"}
                    item["prompt_status"] = "local_rewrite_pending; no inference in dry-run"
                elif node["kind"] in {"quality_review", "motion_plan", "operator_review"}:
                    predictions[node_id] = {"kind": "review"}
                    item["review_status"] = node["kind"] + "_pending; no inference in dry-run"
                else:
                    contract = self.contracts[node["role"]]
                    choice = getattr(self.config, node["role"])
                    body = contract.parameters(choice, node["prompt"], self.board.aspect_ratio, node["data"].get("duration_ms"))
                    check_runtime_duration(node, body, contract, self.config)
                    item["api_parameters"] = body
                    if self.video_rewriter(node):
                        item["prompt_status"] = "source_preview_only; local rewriting and final validation happen before submission"
                        item["prompt_rewriter"] = self._rewriter.identity
                    item["media_field"] = node["data"].get("media_fields", choice.media_field) if metas else None
                    fallback = getattr(self.config, node["role"] + "_fallback", None)
                    if fallback:
                        fc = self.contracts[node["role"] + "_fallback"]
                        fb = fc.parameters(fallback, node["prompt"], self.board.aspect_ratio, node["data"].get("duration_ms"))
                        maximum = fc.inputs.get(fallback.media_field, {}).get("maximum", 999)
                        if len(metas) > maximum:
                            raise HarnessError(f"{node_id}: 回退模型需要保留全部参考图，当前 {len(metas)} 张超过上限 {maximum}")
                        if all(m is not None for m in metas):
                            if metas:
                                fc.check_references(fallback.media_field, metas)
                            fc.quote_body(fb, metas)
                        item["fallback_api_parameters"] = fb
                    for field, group in grouped_media(node, choice, metas):
                        slot = contract.inputs.get(field, {})
                        maximum = slot.get("maximum", 1 if not slot.get("multiple") else 999)
                        if len(group) > maximum:
                            raise HarnessError(f"{node_id} 的 {field} 需要 {len(group)} 张参考图，模型上限 {maximum}；不能丢弃参考图")
                        if field not in contract.inputs:
                            raise HarnessError(f"模型不支持所需图像字段 {field}")
                    if all(m is not None for m in metas):
                        for field, group in grouped_media(node, choice, metas):
                            contract.check_references(field, group)
                        quote_body = quote_media(contract, body, node, choice, metas)
                        item["quote_input_meta"] = quote_body.get("input_meta")
                        if quote:
                            try:
                                item["quoted_credits"] = self.client.quote(quote_body)
                            except HarnessError as exc:
                                item["quote_error"] = safe_error(exc)
                    else:
                        item["quote_unavailable"] = "依赖媒体尚未生成且规格没有确定尺寸，执行时以实际属性报价"
                    predictions[node_id] = contract.expected_image_meta(body)
                    if node["kind"] == "video" and predictions[node_id] is None and metas and metas[0]:
                        predictions[node_id] = {"width": metas[0]["width"], "height": metas[0]["height"], "estimated": True}
                if reusable[node_id]:
                    predictions[node_id] = current["output"]["technical"]
                report.append(item)
                pending.remove(node_id)
        value = {
            "run_id": self.root.name,
            "submitted": False,
            "nodes": report,
            "available_quotes_total": sum(i["quoted_credits"] or 0 for i in report),
            "budget_reserved_credits": self.store.spent(),
            "sequential_shots": self.config.sequential_shots,
            "shot_scheduling": self.config.shot_scheduling,
            "shot_dependencies": shot_dependencies(self.board) if self.config.shot_scheduling == "dependency" else None,
            "object_continuity": getattr(self, "object_review", None),
            "note": "缺失报价和估算尺寸不能作为最终总价；每次提交前按真实媒体重新报价并检查累计预算",
        }
        write_json(self.root / "dry-run.json", value)
        return value

    def _local_node(self, node_id, node, attempt, deps):
        aid = attempt["attempt_id"]
        if node["kind"] == "quality_review":
            return self._review_node(node_id, node, attempt, deps)
        if node["kind"] == "operator_review":
            sid = node["data"]["shot_id"]
            try:
                decision, decision_hash = load_decision(self.root, sid, self.store.current("video:" + sid))
            except HarnessError as exc:
                self.store.update(aid, status="awaiting_review", error=safe_error(exc))
                raise
            passed = decision["decision"] == "approve"
            path = self.root / "quality" / aid / "report.json"
            write_json(path, {"verdict": {"outcome": "pass" if passed else "fail", "summary": decision["notes"]},
                              "operator_decision": decision, "operator_decision_sha256": decision_hash,
                              "source_hashes": {k: v["sha256"] for k, v in zip(node["deps"], deps, strict=True)}})
            record = {"asset_id": aid, "kind": "review", "path": path.relative_to(self.root).as_posix(), "sha256": file_hash(path),
                      "bytes": path.stat().st_size, "task_id": None, "parent_asset_ids": [d["asset_id"] for d in deps],
                      "source_type": "provided", "technical": {"format": "json", "stage": "operator", "outcome": "pass" if passed else "fail"}}
            self.store.update(aid, status="succeeded" if passed else "quality_failed", output=record,
                              error=None if passed else "原视频复核未通过，禁止尾帧传播和拼接")
            if not passed:
                raise HarnessError("原视频复核未通过，禁止尾帧传播和拼接")
            return
        if node["kind"] == "motion_plan":
            self.store.update(aid, status="planning_motion")
            mapped = dict(zip(node["deps"], deps, strict=True))
            sid = node["data"]["shot_id"]
            frame = inside(self.root, mapped["frame:" + sid]["path"])
            images = [("Actual accepted first frame", frame)]
            images += reference_details(frame, self.root / "motion_plans" / (aid + "-details"))
            result = VisualMotionPlanner(self.review_config()).plan(
                {"contract": node["data"]["contract"]},
                images, cache_dir=self.root / "quality_requests",
                total_call_limit=self.config.quality.max_calls, total_token_limit=self.config.quality.max_total_tokens,
            )
            path = self.root / "motion_plans" / (aid + ".json")
            write_json(path, {**result, "source_hashes": {k: v["sha256"] for k, v in mapped.items()}})
            record = {"asset_id": aid, "kind": "plan", "path": path.relative_to(self.root).as_posix(), "sha256": file_hash(path),
                      "bytes": path.stat().st_size, "task_id": None, "parent_asset_ids": [d["asset_id"] for d in deps],
                      "source_type": "generated", "technical": {"format": "json", "feasible": True}}
            self.store.update(aid, status="succeeded", output=record, error=None)
            return
        if node["kind"] == "prompt_rewrite":
            contract = self.capabilities()["video"]
            body = contract.parameters(self.config.video, node["prompt"], self.board.aspect_ratio, node["data"]["duration_ms"])
            check_runtime_duration(node, body, contract, self.config)
            field = contract.spec["output"]["duration"]["parameter"]
            self.store.update(aid, status="rewriting")
            mapped = dict(zip(node["deps"], deps, strict=True))
            sid = node["data"]["shot_id"]
            frame = {"first_frame": inside(self.root, mapped["frame:" + sid]["path"])} if self.config.prompt_rewriter.input_mode in {"first_frame", "first_last_frame"} else {}
            if self.config.prompt_rewriter.input_mode == "first_last_frame":
                frame["last_frame"] = inside(self.root, mapped["end_frame:" + sid]["path"])
            source = node["prompt"]
            if self.config.quality.enabled:
                review_asset = next(d for d in deps if d["kind"] == "review")
                anchors = read_json(inside(self.root, review_asset["path"]))["verdict"].get("visual_anchors", [])
                if anchors:
                    source += "\nVerified facts from the actual first frame (keep these states until the requested action changes them):\n" + "\n".join(anchors)
            motion = next((d for d in deps if d["kind"] == "plan"), None)
            if motion:
                plan = read_json(inside(self.root, motion["path"]))["plan"]
                source += rewrite_motion_context(plan)
            if node["data"].get("prompt_refinements"):
                source += "\n\n" + node["data"]["prompt_refinements"]
            rewritten = self.video_rewriter(node).rewrite(source, body[field], self.board.aspect_ratio, self.config.video.task, self.root / "prompt_rewrites", self.check_pause, **frame)
            path = Path(rewritten["record_path"])
            record = {
                "asset_id": aid,
                "kind": "text",
                "path": path.relative_to(self.root).as_posix(),
                "sha256": file_hash(path),
                "bytes": path.stat().st_size,
                "task_id": None,
                "parent_asset_ids": [d["asset_id"] for d in deps],
                "source_type": "generated",
                "technical": {"format": "json", "input_mode": self.config.prompt_rewriter.input_mode},
            }
            self.store.update(aid, status="succeeded", output=record, error=None)
            return
        if node["kind"] == "provided":
            path = inside(self.root, node["binding"])
        elif node["kind"] == "reference_board":
            path = self.root / "reference_boards" / (aid + ".png")
            identity_board([inside(self.root, d["path"]) for d in deps], node["data"]["labels"], path)
        else:
            path = self.root / "tail_frames" / (aid + ".png")
            duration = node["data"].get("duration_ms") or deps[0]["clip"]["duration_ms"]
            extract_timeline_tail(inside(self.root, deps[0]["path"]), path, self.config, duration, node["data"].get("source_in_ms", 0))
        record = asset_record(self.root, path, "image", self.config, parents=[d["asset_id"] for d in deps])
        record["asset_id"] = aid
        record["source_type"] = "provided" if node["kind"] == "provided" else "edited"
        self.store.update(aid, status="succeeded", output=record, error=None)

    def _review_node(self, node_id, node, attempt, deps):
        self.check_pause()
        aid = attempt["attempt_id"]
        cfg = self.config.quality
        mapped = dict(zip(node["deps"], deps, strict=True))
        sid, stage = node["data"]["shot_id"], node["data"]["stage"]
        first_frame = inside(self.root, mapped["frame:" + sid]["path"])
        images = [("Actual first frame", first_frame)]
        data = {"stage": stage, "contract": node["data"]["contract"]}
        if node["data"].get("image_refinements"):
            data["image_refinements"] = node["data"]["image_refinements"]
            data["refinement_review_scope"] = "Verify these pipeline repair requirements against the actual candidate and reference pixels. They cannot override the canonical action or justify new events. Report visible contradictions; do not assume the generator followed the requirements."
        target_end = mapped.get("end_frame:" + sid)
        if target_end:
            images.append(("Target ending frame (same shot, intended final state)", inside(self.root, target_end["path"])))
        if stage in {"prompt", "video"}:
            frame_review = mapped["check:frame:" + sid]
            data["verified_first_frame_facts"] = read_json(inside(self.root, frame_review["path"]))["verdict"].get("visual_anchors", [])
        folder = self.root / "quality" / aid
        folder.mkdir(parents=True, exist_ok=True)
        if stage in {"frame", "prompt", "end_frame"}:
            images += reference_details(first_frame, folder / "reference_details")
        if target_end and stage in {"prompt", "end_frame"}:
            images += [(label.replace("first frame", "target ending frame"), path) for label, path in reference_details(inside(self.root, target_end["path"]), folder / "end_details")]
        batches = []
        if stage == "frame":
            data["contract"] = initial_state_contract(data["contract"])
            images += [(label, inside(self.root, asset["path"])) for label, asset in mapped.items() if asset["kind"] == "image" and label != "frame:" + sid]
            data["reference_roles"] = "Character and scene images define identity and setting, not current object state. Tail/frame context belongs to earlier shots. Evaluate ONLY the candidate's exact declared START state. Check object counts, container contents and contact/support before identity/style. An empty container must have no contents; do not excuse an already changed state as an action in progress or infer any future action to justify it. The initial state is the acceptance contract."
            batches = [(data, images)]
        elif stage == "end_frame":
            data["review_scope"] = "Only the static target ending frame: required end state, allowed changes from the actual first frame, unchanged identities and room. No claim that the intervening motion has happened."
            batches = [(data, images)]
        elif stage == "prompt":
            rewrite = mapped.get("rewrite:" + sid)
            data["candidate_prompt"] = read_json(inside(self.root, rewrite["path"]))["prompt"] if rewrite else node["data"]["candidate_prompt"]
            if "motion:" + sid in mapped:
                motion_record = read_json(inside(self.root, mapped["motion:" + sid]["path"]))
                data["causal_motion_plan"] = motion_record["plan"]
                data["initial_geometry"] = motion_record.get("initial_geometry")
            batches = [(data, images)]
        else:
            source = inside(self.root, mapped["video:" + sid]["path"])
            samples = sample_video(source, folder / "samples", self.config, node["data"]["contract"]["duration_ms"])
            # Include adjacent overlap so a batch boundary cannot hide a discontinuity.
            stride = cfg.frames_per_review - 1
            for start in range(0, len(samples), stride):
                selected = samples[start:start + cfg.frames_per_review]
                if start and len(selected) == 1:
                    break
                if cfg.independent_physics_review:
                    batch_data = {"stage": "physics", "interval_seconds": [selected[0]["time_seconds"], selected[-1]["time_seconds"]],
                                  "sampling_fps": cfg.samples_per_second, "reference_role": "Only this contiguous sampled interval. No earlier image, target endpoint or desired narrative is supplied; inspect what actually happens."}
                else:
                    batch_data = video_segment_data(data, selected, contains_start=start == 0,
                                                    contains_end=start + cfg.frames_per_review >= len(samples),
                                                    total_samples=len(samples), rate=cfg.samples_per_second)
                batches.append((batch_data, ([] if cfg.independent_physics_review else images) + [(f"Video t={s['time_seconds']:.3f}s (decoded frame {s['frame_index']})", s["path"]) for s in selected]))
            if cfg.independent_physics_review:
                count = min(cfg.frames_per_review, len(samples))
                selected = samples if count == 1 else [samples[round(i * (len(samples) - 1) / (count - 1))] for i in range(count)]
                narrative = {**data, "review_scope": {"require_completion": True, "interval_seconds": [0, samples[-1]["time_seconds"]]},
                             "instructions": "These overview samples cover the entire retained clip. Check story action, identity, setting and the final state against the contract. Detailed physical transitions are reviewed separately without narrative priming. Do not infer unobserved action phases."}
                batches.append((narrative, images + [(f"Video t={s['time_seconds']:.3f}s", s["path"]) for s in selected]))
        results = []
        self.store.update(aid, status="reviewing")
        if stage == "end_frame" and node["data"].get("fixed_camera_check"):
            self.check_pause()
            result = review_fixed_camera(VisualReviewer(self.review_config()), first_frame, inside(self.root, target_end["path"]),
                                         cache_dir=self.root / "quality_requests", total_call_limit=cfg.max_calls, total_token_limit=cfg.max_total_tokens)
            results.append(result)
            if result["verdict"]["outcome"] != "pass":
                batches = []  # A broad narrative pass cannot override a failed invariant.
        for batch_data, batch_images in batches:
            self.check_pause()
            # The run-wide persistent budget governs batches; each request has
            # its own adapter so long clips cannot hit a text-planning call cap.
            results.append(VisualReviewer(self.review_config()).review(batch_data, batch_images, cache_dir=self.root / "quality_requests",
                                           total_call_limit=cfg.max_calls, total_token_limit=cfg.max_total_tokens))
        result = {"node_id": node_id, "verdict": aggregate_verdicts(results), "reviews": results,
                  "source_hashes": {label: asset["sha256"] for label, asset in mapped.items()}, "model": cfg.model}
        path = folder / "report.json"
        write_json(path, result)
        record = {"asset_id": aid, "kind": "review", "path": path.relative_to(self.root).as_posix(), "sha256": file_hash(path),
                  "bytes": path.stat().st_size, "task_id": None, "parent_asset_ids": [d["asset_id"] for d in deps],
                  "source_type": "generated", "technical": {"format": "json", "stage": stage, "outcome": result["verdict"]["outcome"]}}
        passed = result["verdict"]["outcome"] == "pass"
        error = None if passed else f"内容检查 {result['verdict']['outcome']}：{path.relative_to(self.root).as_posix()}；候选已保留，不能用于下游"
        self.store.update(aid, status="succeeded" if passed else "quality_failed", output=record, error=error)
        if not passed:
            raise HarnessError(error)

    def _run_node(self, node_id):
        try:
            self._run_node_attempt(node_id)
        except Exception as exc:
            if isinstance(exc, PauseRequested):
                raise
            node = self.graph[node_id]
            a = self.store.current(node_id)
            fallback = getattr(self.config, str(node["role"]) + "_fallback", None)
            req = (a or {}).get("request") or {}
            eligible = a and (a["status"] == "remote_failed" or (a["status"] == "rejected" and req.get("fallback_eligible")))
            if not fallback or req.get("fallback") or not eligible:
                raise
            # A known terminal failure is retained as the parent candidate. Unknown,
            # timeout, authentication, quota, safety and download failures never switch.
            self.store.attempt(node_id, a["input_hash"], {**req, "fallback": True}, force=True)
            self.store.event(node_id, "image_model_fallback", fallback.model_cls)
            self._run_node_attempt(node_id)

    def _run_node_attempt(self, node_id):
        self.check_pause()
        node = self.graph[node_id]
        deps = self._dependency_outputs(node)
        ihash = self._input_hash(node_id, deps)
        previous = self.store.current(node_id)
        if previous and previous["status"] in {"submitting", "submission_unknown"} and not previous["task_id"]:
            if previous["status"] == "submitting":
                self.store.update(previous["attempt_id"], status="submission_unknown")
            raise HarnessError(f"{node_id}: submission_unknown，输入修改也不能绕过核实；使用 resolve-submission")
        request_record = {"prompt": node["prompt"], "prompt_version": node["prompt_version"], "dependencies": [{"asset_id": d["asset_id"], "path": d["path"], "sha256": d["sha256"]} for d in deps]}
        a = self.store.attempt(node_id, ihash, request_record)
        aid = a["attempt_id"]
        request_record["fallback"] = bool((a["request"] or {}).get("fallback"))
        if self._valid_output(a):
            return
        if node["kind"] in {"provided", "tail", "reference_board", "prompt_rewrite", "quality_review", "motion_plan", "operator_review"}:
            self._local_node(node_id, node, a, deps)
            return
        if a["status"] in {"submission_unknown", "submitting"} and not a["task_id"]:
            raise HarnessError(f"{node_id}: submission_unknown，使用 resolve-submission 核实")
        if a["status"] in {"rejected", "remote_failed", "abandoned"}:
            raise HarnessError(f"{node_id}: 已记录失败，修正后使用 retry-node 或 rerun-shot 创建新候选")
        if not a["task_id"]:
            self.capabilities()
            role = node["role"] + ("_fallback" if request_record["fallback"] else "")
            contract = self.contracts[role]
            choice = getattr(self.config, role)
            media_deps = [d for d in deps if d["kind"] in {"image", "video"}]
            metas = [d["technical"] for d in media_deps]
            body = contract.parameters(choice, node["prompt"], self.board.aspect_ratio, node["data"].get("duration_ms"))
            check_runtime_duration(node, body, contract, self.config)
            rewriter = self.video_rewriter(node)
            if rewriter:
                rewritten = (
                    read_json(inside(self.root, dict(zip(node["deps"], deps, strict=True))["rewrite:" + node["data"]["shot_id"]]["path"]))
                    if self.config.rewrite_ahead or self.config.quality.enabled
                    else rewriter.rewrite(node["prompt"], body["video_duration_seconds"], self.board.aspect_ratio, choice.task, self.root / "prompt_rewrites", self.check_pause,
                                          **({"first_frame": inside(self.root, media_deps[0]["path"])} if self.config.prompt_rewriter.input_mode == "first_frame" else {}))
                )
                body["prompt"] = rewritten["prompt"]
                request_record.update(
                    original_prompt=node["prompt"],
                    prompt=rewritten["prompt"],
                    prompt_rewrite={
                        "input_hash": rewritten["input_hash"],
                        "record_path": str(Path(rewritten["record_path"]).relative_to(self.root)),
                        "raw_output_sha256": rewritten["raw_output_sha256"],
                        "prompt_file_sha256": rewritten["prompt_file_sha256"],
                    },
                )
                self.store.update(aid, request=request_record)
                self.store.event(node_id, "prompt_rewritten", request_record["prompt_rewrite"]["record_path"])
                self.check_pause()
            for field, group in grouped_media(node, choice, metas):
                contract.check_references(field, group)
            quote_body = quote_media(contract, body, node, choice, metas)
            # Validate and load media before reserving, so local errors cannot be ambiguous submissions.
            submission = body
            for field, group in grouped_media(node, choice, media_deps):
                submission = contract.with_media(submission, field, [media_input(inside(self.root, d["path"]), self.config.max_media_bytes) for d in group], [d["technical"] for d in group])
            contract.validate(submission)
            quote = self.client.quote(quote_body)
            self.store.update(
                aid,
                quote=quote,
                request={
                    **request_record,
                    "api_parameters": body,
                    "media_field": node["data"].get("media_fields", choice.media_field) if media_deps else None,
                    "input_meta": quote_body.get("input_meta"),
                    "model_spec_hash": digest(contract.spec),
                    "model_spec_path": "contracts/" + digest(contract.spec) + ".json",
                },
            )
            self.check_pause()
            self.store.reserve(aid, quote, self.config.budget_credits)
            try:
                response = self.client.submit(submission)
            except SubmissionUnknown as exc:
                self.store.update(aid, status="submission_unknown", error=safe_error(exc))
                raise
            except APIError as exc:
                saved = self.store.current(node_id)["request"]
                self.store.update(aid, status="rejected", reserved=0, error=safe_error(exc), request={**saved, "fallback_eligible": exc.category in {"parameters", "not_found"}})
                raise
            # First durable operation after submission is saving task_id. Nothing else is done first.
            self.store.update(aid, task_id=response["task_id"], status="submitted")
            self.store.event(node_id, "task_submitted", response["task_id"])
            a = self.store.current(node_id)
            billed_quote = response.get("quoted_credits")
            if isinstance(billed_quote, (float, int)) and billed_quote > quote:
                self.store.update(aid, reserved=billed_quote)
                self.store.event(node_id, "quote_changed", "server accepted at a higher quote; no further submissions if budget exhausted")
        deadline = time.monotonic() + self.config.wait_timeout_seconds
        while True:
            self.check_pause()
            result = self.client.query(a["task_id"])
            remote = result["status"]
            self.store.update(aid, remote_status=remote, status="waiting_remote")
            if remote == "SUCCEED":
                break
            if remote in {"FAILED", "CANCEL"}:
                self.store.update(aid, status="remote_failed", error="远端任务终态 " + remote)
                raise HarnessError(f"{node_id}: 远端 {remote}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.store.update(aid, status="wait_timeout", error="本地等待超时，远端状态不变；可 resume")
                raise HarnessError(f"{node_id}: 本地等待超时，可 resume 查询原任务")
            self.client.sleep(min(self.config.poll_interval_seconds, remaining))
        kind = "video" if node["kind"] == "video" else "image"
        folder = {"character": "characters", "scene": "scenes", "frame": "first_frames", "end_frame": "end_frames", "video": "videos"}[node["kind"]]
        target = self.root / folder / (aid + (".mp4" if kind == "video" else ".image"))
        self.store.update(aid, status="downloading")
        record = None
        try:
            self.client.download_result(a["task_id"], "output_video" if kind == "video" else "output_image", target)
            record = asset_record(self.root, target, kind, self.config, task_id=a["task_id"], parents=[d["asset_id"] for d in deps])
            record.update(asset_id=aid, source_type="generated", clip={"source_in_ms": 0, "duration_ms": node["data"].get("duration_ms")})
            if kind == "video":
                record["verification_note"] = check_clip(record["technical"], node["data"]["duration_ms"])
        except Exception as exc:
            fields = {"status": "download_or_validation_failed", "error": safe_error(exc)}
            if record is not None:
                fields["output"] = record
            self.store.update(aid, **fields)
            raise
        self.store.update(aid, output=record, status="succeeded", error=None)
        self.store.event(node_id, "asset_saved", record["path"])

    def execute(self, explicit=False):
        if not explicit or not self.config.execute_enabled or self.config.budget_credits is None:
            raise HarnessError("执行需要 --execute、execute_enabled=true 和 budget_credits")
        if not self.client.key:
            raise HarnessError("缺少 X2V_API_KEY 环境变量")
        if self.config.quality.enabled:
            VisualReviewer(self.review_config())  # Fail before any paid image generation if credentials are missing.
        if (self.config.quality.enabled or any(n["kind"] == "tail" for n in self.graph.values()) or (self.config.sequential_shots and len(self.board.shots) > 1)) and (
            not executable(self.config.ffmpeg) or not executable(self.config.ffprobe)
        ):
            raise HarnessError("串行分镜需要 FFmpeg/ffprobe 提取真实尾帧，请配置工具后执行；未提交任务")
        self.capabilities()
        # Preflight every node, including reference capacity, before the first paid task.
        self.dry_run(quote=False)
        pending = set(self.graph)
        completed, errors, running = set(), {}, {}
        halted = False
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as pool, ThreadPoolExecutor(max_workers=1) as rewrite_pool:
            while pending or running:
                if (self.root / "PAUSE_REQUESTED").exists():
                    halted = True
                    for node_id in pending:
                        errors[node_id] = "pause_requested: 用户要求暂停，后续任务未提交"
                    pending.clear()
                # Feed a ready first frame into video generation promptly while other images run.
                for node_id in sorted(list(pending), key=lambda n: (0 if self.graph[n]["kind"] == "video" else 1, n)):
                    deps = self.graph[node_id]["deps"]
                    if any(d in errors for d in deps):
                        errors[node_id] = "依赖节点失败或待核实"
                        pending.remove(node_id)
                    elif all(d in completed for d in deps):
                        is_rewrite = self.graph[node_id]["kind"] == "prompt_rewrite"
                        active = sum((self.graph[n]["kind"] == "prompt_rewrite") == is_rewrite for n in running.values())
                        if active < (1 if is_rewrite else self.config.concurrency):
                            executor = rewrite_pool if is_rewrite else pool
                            running[executor.submit(self._run_node, node_id)] = node_id
                            pending.remove(node_id)
                if not running:
                    if pending:
                        if any(any(d in errors for d in self.graph[n]["deps"]) for n in pending):
                            continue  # Propagate failures across any number of dependency levels.
                        raise HarnessError("无法推进依赖图")
                    break
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    node_id = running.pop(future)
                    try:
                        future.result()
                        completed.add(node_id)
                    except Exception as exc:
                        errors[node_id] = safe_error(exc)
                        current = self.store.current(node_id)
                        if (
                            isinstance(exc, (BudgetError, PauseRequested, SubmissionUnknown))
                            or (current and current["status"] in {"submitting", "submission_unknown"} and not current["task_id"])
                            or (isinstance(exc, APIError) and exc.category in {"authentication", "permission", "balance", "server", "network", "rate_limit", "rate_limit_retry_later"})
                        ):
                            halted = True
                        if current:
                            fields = {"error": errors[node_id]}
                            if current["status"] == "planned":
                                fields["status"] = "blocked"
                            self.store.update(current["attempt_id"], **fields)
                        self.store.event(node_id, "node_issue", errors[node_id])
                if halted:
                    for node_id in pending:
                        errors[node_id] = "服务或执行条件异常，后续节点未提交；修复后 resume"
                    pending.clear()
                self.export()
        history = self.collect_history() if not halted else {"deferred": "执行条件或服务异常，稍后 collect-history 回收旧任务"}
        self.export()
        return {"run_id": self.root.name, "completed": sorted(completed), "issues": errors, "historical_candidates": history, "budget_reserved_credits": self.store.spent()}

    def collect_history(self):
        """Download completed retired tasks; never submit or discard a paid candidate."""
        current_ids = {n["current_id"] for n in self.store.nodes().values()}
        result = {"collected": [], "pending": [], "issues": {}}
        for a in self.store.snapshot()["attempts"]:
            if a["attempt_id"] in current_ids or not a["task_id"] or a["output"] or a["status"] == "remote_failed":
                continue
            try:
                remote = self.client.query(a["task_id"])["status"]
                self.store.update(a["attempt_id"], remote_status=remote)
                if remote in {"FAILED", "CANCEL"}:
                    self.store.update(a["attempt_id"], status="remote_failed")
                elif remote == "SUCCEED":
                    kind = "video" if a["node_id"].startswith("video:") else "image"
                    path = self.root / "history" / (a["attempt_id"] + (".mp4" if kind == "video" else ".image"))
                    self.client.download_result(a["task_id"], "output_video" if kind == "video" else "output_image", path)
                    parents = [d["asset_id"] for d in (a["request"] or {}).get("dependencies", [])]
                    record = asset_record(self.root, path, kind, self.config, task_id=a["task_id"], parents=parents)
                    record.update(asset_id=a["attempt_id"], source_type="generated")
                    self.store.update(a["attempt_id"], status="succeeded", output=record)
                    result["collected"].append(a["attempt_id"])
                else:
                    result["pending"].append(a["task_id"])
            except Exception as exc:
                result["issues"][a["attempt_id"]] = safe_error(exc)
        return result

    def repair_prompt_from_review(self, node_id):
        """Regenerate a failed rewrite from traceable feedback, retaining both images."""
        node = self.graph.get(node_id)
        if not node or node["kind"] != "prompt_rewrite" or not self.config.quality.enabled:
            raise HarnessError("repair-prompt 需要已启用检查的 rewrite 节点")
        repairs = self.root / "prompt_repairs"
        if sum(read_json(path)["node_id"] == node_id for path in repairs.glob("*.json")) >= 2:
            raise HarnessError("该提示词已达到 2 次反馈修复上限；需检查流程原因")
        check_id = "check:prompt:" + node["data"]["shot_id"]
        check, candidate = self.store.current(check_id), self.store.current(node_id)
        if not check or check["status"] != "quality_failed" or not check["output"] or not self._valid_output(candidate):
            raise HarnessError("没有针对当前提示词的失败或不确定检查")
        report_path = inside(self.root, check["output"]["path"])
        if (file_hash(report_path) != check["output"]["sha256"]
                or check["input_hash"] != self._input_hash(check_id, self._dependency_outputs(self.graph[check_id]))
                or candidate["input_hash"] != self._input_hash(node_id, self._dependency_outputs(node))):
            raise HarnessError("提示词或检查已改变，不能复用旧反馈")
        verdict = read_json(report_path)["verdict"]
        if verdict["outcome"] not in {"fail", "uncertain"}:
            raise HarnessError("提示词检查未失败，不能构造失败修复")
        feedback = (
            "A prior rewrite did not pass review. Generate a fresh complete H3 prompt that resolves the following findings against the original action, actual first image and target ending image. "
            "The review is fallible feedback, not new scene evidence or permission to change the story. "
            "For directional language, distinguish the actor's own left/right from image/screen left/right; state the coordinate frame explicitly when needed and match the visible ending pose. "
            "Keep the required actions, timing, characters and objects. Do not add events to explain a prior error.\n"
            + canonical({"outcome": verdict["outcome"], "issues": verdict["issues"], "summary": verdict["summary"]})
        )
        config_data = self.config.model_dump()
        config_data["prompt_refinements"][node_id] = feedback
        config = Config.model_validate(config_data)
        graph = build_graph(self.chars, self.board, config)
        affected = {node_id}
        while True:
            expanded = affected | {key for key, recipe in graph.items() if affected.intersection(recipe["deps"])}
            if expanded == affected:
                break
            affected = expanded
        for key in affected:
            current = self.store.current(key)
            if current and current["status"] in {"submitting", "submission_unknown"} and not current["task_id"]:
                raise HarnessError("后续任务提交结果未知，核实前不能修复提示词")
        record_path = repairs / (uuid.uuid4().hex + ".json")
        record = {"node_id": node_id, "created_at": now(), "status": "prepared", "candidate_attempt_id": candidate["attempt_id"],
                  "candidate_sha256": candidate["output"]["sha256"], "review_attempt_id": check["attempt_id"],
                  "review_path": check["output"]["path"], "review_sha256": check["output"]["sha256"],
                  "previous_refinement": self.config.prompt_refinements.get(node_id), "updated_refinement": feedback}
        write_json(record_path, record)
        write_json(self.root / "config.json", config.model_dump())
        self.config, self.graph = config, graph
        invalidated = self.invalidate([node_id])
        record["status"] = "completed"
        write_json(record_path, record)
        self.store.event(node_id, "prompt_feedback_repair_prepared", record_path.relative_to(self.root).as_posix())
        return {"node_id": node_id, "repair_record": str(record_path), "invalidated": invalidated, "submitted": False, "next": "resume --execute"}

    def repair_image_from_review(self, node_id):
        """Prepare one bounded, model-feedback repair; submission remains explicit."""
        node = self.graph.get(node_id)
        if not node or node["kind"] not in {"frame", "end_frame"} or not self.config.quality.enabled:
            raise HarnessError("repair-image 需要已生成且启用检查的 frame/end_frame 节点")
        repairs = self.root / "image_repairs"
        prior = [read_json(path) for path in repairs.glob("*.json")]
        if sum(record["node_id"] == node_id for record in prior) >= 2:
            raise HarnessError("该图像已达到 2 次反馈修复上限；保留失败候选，需检查流程原因")
        check_id = "check:" + node_id
        check = self.store.current(check_id)
        candidate = self.store.current(node_id)
        if not check or check["status"] != "quality_failed" or not check["output"] or not self._valid_output(candidate):
            raise HarnessError("没有针对当前图像的失败或不确定检查，不能自动构造修复")
        report_path = inside(self.root, check["output"]["path"])
        if file_hash(report_path) != check["output"]["sha256"] or check["input_hash"] != self._input_hash(check_id, self._dependency_outputs(self.graph[check_id])):
            raise HarnessError("图像检查已改变或不再对应当前输入，不能复用旧反馈")
        verdict = read_json(report_path)["verdict"]
        if verdict["outcome"] not in {"fail", "uncertain"}:
            raise HarnessError("图像检查未失败，不能构造失败修复")
        feedback = "先前候选未通过检查。下面是原始检查反馈，只修复这些差异；原始首帧、分镜、物品保持和摄影规则仍为依据，不增加动作或物体来解释旧图。\n" + canonical({"outcome": verdict["outcome"], "issues": verdict["issues"], "summary": verdict["summary"]})
        config_data = self.config.model_dump()
        previous = self.config.image_refinements.get(node_id)
        # Previous observed defects are generation feedback, not evidence about a
        # new candidate. Keep the original acceptance rules in the fresh review.
        prior_instructions = previous.instructions if isinstance(previous, ImageRefinement) else previous or ""
        review_instructions = (previous.review_instructions if isinstance(previous, ImageRefinement) and previous.review_instructions is not None
                               else prior_instructions.split("先前候选未通过检查。", 1)[0].rstrip())
        if isinstance(previous, ImageRefinement):
            updated = previous.model_copy(update={"instructions": previous.instructions + "\n" + feedback, "review_instructions": review_instructions}).model_dump()
        else:
            updated = ImageRefinement(instructions=(prior_instructions + "\n" + feedback).strip(), review_instructions=review_instructions).model_dump()
        config_data["image_refinements"][node_id] = updated
        config = Config.model_validate(config_data)
        graph = build_graph(self.chars, self.board, config)
        affected = {node_id}
        while True:
            expanded = affected | {key for key, recipe in graph.items() if affected.intersection(recipe["deps"])}
            if expanded == affected:
                break
            affected = expanded
        for key in affected:
            current = self.store.current(key)
            if current and current["status"] in {"submitting", "submission_unknown"} and not current["task_id"]:
                raise HarnessError("后续任务提交结果未知，核实前不能修复图像")
        record_path = repairs / (uuid.uuid4().hex + ".json")
        record = {"node_id": node_id, "created_at": now(), "status": "prepared", "candidate_attempt_id": candidate["attempt_id"],
                  "candidate_sha256": candidate["output"]["sha256"], "review_attempt_id": check["attempt_id"],
                  "review_path": check["output"]["path"], "review_sha256": check["output"]["sha256"],
                  "feedback": feedback, "previous_refinement": self.config.model_dump()["image_refinements"].get(node_id),
                  "updated_refinement": updated}
        write_json(record_path, record)
        # A config-first interruption is safe: changed recipe hashes prevent old reuse.
        write_json(self.root / "config.json", config.model_dump())
        self.config, self.graph = config, graph
        invalidated = self.invalidate([node_id])
        record["status"] = "completed"
        write_json(record_path, record)
        self.store.event(node_id, "image_feedback_repair_prepared", record_path.relative_to(self.root).as_posix())
        return {"node_id": node_id, "repair_record": str(record_path), "invalidated": invalidated, "submitted": False, "next": "resume --execute"}

    def invalidate(self, targets):
        if set(targets) - set(self.graph):
            raise HarnessError("未知节点")
        affected = set(targets)
        while True:
            expanded = affected | {n for n, v in self.graph.items() if affected.intersection(v["deps"])}
            if expanded == affected:
                break
            affected = expanded
        for node_id in affected:
            current = self.store.current(node_id)
            if current and current["status"] in {"submitting", "submission_unknown"} and not current["task_id"]:
                raise HarnessError("提交结果未知，必须先 resolve-submission，不能直接创建重复候选")
        # Retire final delivery whenever a dependency is superseded; retain its file as history.
        assembly = self.root / "exports" / "assembly.json"
        if assembly.exists():
            data = read_json(assembly)
            data["status"] = "stale"
            write_json(assembly, data)
        self.store.invalidate(affected)
        self.export()
        return sorted(affected)

    def export(self):
        snapshot = self.store.snapshot()
        current_inputs = self.current_inputs()
        write_json(self.root / "state.json", snapshot)
        assets, units, clips, steps, checks = [], [], [], [], []
        for node_id, node in self.graph.items():
            a = self.store.current(node_id)
            local = a["status"] if a else "pending"
            public = (
                "succeeded"
                if local == "succeeded"
                else "failed"
                if local in {"remote_failed", "rejected", "download_or_validation_failed", "blocked", "abandoned", "quality_failed"}
                else "pending"
                if local in {"pending", "planned"}
                else "running"
            )
            steps.append(
                {
                    "step_id": node_id,
                    "title": node_id,
                    "depends_on": node["deps"],
                    "status": public,
                    "local_status": local,
                    "execution_mode": "automated",
                    "input_asset_ids": [self.store.current(d)["output"]["asset_id"] for d in node["deps"] if self.store.current(d) and self.store.current(d)["output"]],
                    "output_asset_ids": [a["output"]["asset_id"]] if a and a["output"] else [],
                    "task_id": a["task_id"] if a else None,
                    "operator": "video-harness",
                    "notes": a["error"] if a else None,
                }
            )
        for a in snapshot["attempts"]:
            if a["output"]:
                asset = a["output"]
                assets.append({**asset, "state": "available" if inside(self.root, asset["path"]).is_file() else "missing", "created_by_attempt_id": a["attempt_id"]})
        for s in sorted(self.board.shots, key=lambda s: s.timeline_in_ms):
            a = self.store.current("video:" + s.shot_id)
            accepted = current_inputs["video:" + s.shot_id] and (not self.config.quality.enabled or current_inputs["check:video:" + s.shot_id])
            if self.config.quality.require_operator_review:
                accepted = accepted and current_inputs["check:operator:" + s.shot_id]
            asset_id = a["output"]["asset_id"] if accepted else None
            units.append(
                {
                    "unit_id": s.shot_id,
                    "kind": "shot",
                    "description": s.action_description,
                    "depends_on": list(dict.fromkeys(d.split(":", 1)[1] for d in self.graph["frame:" + s.shot_id]["deps"] if d.startswith(("tail:", "frame:")))),
                    "planned_duration_ms": s.duration_ms,
                    "selected_asset_id": asset_id,
                    "status": "ready" if asset_id else "planned",
                    "reference_asset_ids": [self.store.current(d)["output"]["asset_id"] for d in self.graph["frame:" + s.shot_id]["deps"] if self.store.current(d) and self.store.current(d)["output"]],
                    "extensions": {"scene_id": s.scene_id},
                }
            )
            clips.append(
                {
                    "clip_id": "CLIP_" + s.shot_id,
                    "unit_id": s.shot_id,
                    "asset_id": asset_id,
                    "timeline_in_ms": s.timeline_in_ms,
                    "source_in_ms": 0,
                    "duration_ms": s.duration_ms,
                    "playback_rate": 1,
                    "mode": "source_video",
                }
            )
            operator = self.store.current("check:operator:" + s.shot_id) if self.config.quality.require_operator_review else None
            checks.append({"check_id": "CHK_" + s.shot_id, "scope": "unit", "target_ids": [s.shot_id], "criterion": "原视频人工/代理复核，具体覆盖范围以记录为准",
                           "verdict": operator["output"]["technical"]["outcome"] if operator and operator["output"] else "not_checked",
                           "report_path": operator["output"]["path"] if operator and operator["output"] else None})
            if self.config.quality.enabled:
                for stage in (("frame", "end_frame", "prompt", "video") if self.config.quality.end_frame_conditioning else ("frame", "prompt", "video")):
                    review = self.store.current(f"check:{stage}:" + s.shot_id)
                    checks.append({"check_id": f"AUTO_{stage}_" + s.shot_id, "scope": "unit", "target_ids": [s.shot_id],
                                   "criterion": "人物、场景、物品状态与物理合理性（抽帧检查）", "verdict": review["output"]["technical"]["outcome"] if review and review["output"] else "not_checked",
                                   "report_path": review["output"]["path"] if review and review["output"] else None})
        assembly = read_json(self.root / "exports" / "assembly.json") if (self.root / "exports" / "assembly.json").exists() else None
        if assembly and assembly["status"] != "stale":
            expected = [self.store.current("video:" + s.shot_id)["attempt_id"] if self.store.current("video:" + s.shot_id) else None for s in self.board.shots]
            expected_settings = {
                "width": self.config.export_width,
                "height": self.config.export_height,
                "fps": self.config.fps,
                "fit": self.config.fit,
            }
            final_path = inside(self.root, assembly["asset"]["path"])
            final_valid = final_path.is_file() and file_hash(final_path) == assembly["asset"]["sha256"]
            if expected != assembly["asset"].get("parent_asset_ids") or not all(current_inputs.values()) or assembly.get("settings") != expected_settings or not final_valid:
                assembly["status"] = "stale"
                write_json(self.root / "exports" / "assembly.json", assembly)
        assembled = assembly and assembly["status"] != "stale"
        if assembled:
            assets.append({**assembly["asset"], "asset_id": "FINAL", "state": "available", "source_type": "edited", "created_by_attempt_id": None})
        attempts = []
        for a in snapshot["attempts"]:
            req = a["request"] or {}
            api = req.get("api_parameters", {})
            terminal = a["status"] in {"succeeded", "remote_failed", "rejected", "abandoned", "download_or_validation_failed", "blocked", "quality_failed"}
            attempts.append(
                {
                    **a,
                    "local_status": a["status"],
                    "status": "succeeded" if a["status"] == "succeeded" else "failed" if terminal else "running",
                    "step_id": a["node_id"],
                    "unit_id": a["node_id"].split(":", 1)[1] if a["node_id"].split(":", 1)[0] in {"frame", "end_frame", "video", "tail"} else None,
                    "operator": "video-harness",
                    "execution_mode": "automated",
                    "provider": "x2v" if a["task_id"] else "local_or_unsubmitted",
                    "model_name": api.get("model_cls"),
                    "model_version": None,
                    "input_asset_ids": [d["asset_id"] for d in req.get("dependencies", [])],
                    "output_asset_ids": [a["output"]["asset_id"]] if a["output"] else [],
                    "prompt": req.get("original_prompt", req.get("prompt")),
                    "negative_prompt": api.get("negative_prompt"),
                    "effective_prompt": req.get("prompt") if req.get("prompt_rewrite") else None,
                    "settings": api,
                    "failure_reason": a["error"],
                    "started_at": a["created_at"],
                    "ended_at": a["updated_at"] if terminal else None,
                    "human_minutes": None,
                    "elapsed_seconds": None,
                    "cost_amount": None,
                    "cost_unit": "credits",
                }
            )
        manifest = {
            "schema_version": "0.1.0",
            "project_id": self.root.name,
            "revision": "r001",
            "status": "in_review" if assembled else "in_progress",
            "task": {
                "title": (self.root / "script.md").read_text(encoding="utf-8-sig").splitlines()[0],
                "brief": getattr(self.board, "story_summary", None),
                "audience": None,
                "language": None,
                "primary_workflow": "script",
                "tags": [],
                "owner": None,
                "input_asset_ids": [a["asset_id"] for a in assets if a.get("source_type") == "provided"],
                "constraints": ["单次生成不超过15秒", "禁止拉伸人物或用不足源片段凑时长"],
                "acceptance_criteria": ["全部规划镜头有真实素材", "技术检查通过", "剧情和声音经审看"],
                "output_spec": {
                    "duration_ms": self.board.total_duration_ms,
                    "width": self.config.export_width,
                    "height": self.config.export_height,
                    "fps_num": self.config.fps,
                    "fps_den": 1,
                    "container": "mp4",
                    "audio_requirement": "保留源音轨，缺失补静音；内容待审看",
                },
            },
            "steps": steps,
            "units": units,
            "assets": assets,
            "timeline": {"status": "ready" if all(c["asset_id"] for c in clips) else "planned", "tracks": [{"track_id": "V1", "kind": "video", "clips": clips}]},
            "attempts": attempts,
            "checks": checks,
            "delivery": {
                "status": "review" if assembled else "not_ready",
                "final_asset_id": "FINAL" if assembled else None,
                "editor_project_asset_id": None,
                "known_issues": ["尚未完整人工审看；音轨存在不表示对白、音效或字幕已完成"],
                "handoff_notes": "真实媒体属性与来源见 assets；执行状态以 state.sqlite3 为准",
            },
            "extensions": {"script": {"characters": "characters.json", "storyboard": "storyboard.json"}, "budget_reserved_credits": self.store.spent()},
        }
        write_json(self.root / "manifest.json", manifest)
        self.store.export_log()
        return manifest
