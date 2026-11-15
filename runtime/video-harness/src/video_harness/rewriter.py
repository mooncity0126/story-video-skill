"""Local H3 rewriting with auditable image conditions and cached raw outputs."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .util import HarnessError, digest, file_hash, now, read_json, run_lock, write_json

ALIGNMENT = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
FIELDS = ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:")
CONSTRAINTS = Path(__file__).parent / "templates/minimax_rewriter_constraints.md"
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def frame_alignment(duration_seconds, both=False):
    if both:
        return f"How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the {duration_seconds:.2f}-second mark of the target video."
    return ALIGNMENT


def validate_output(payload, source, proper_names=(), *, alignment=ALIGNMENT):
    text = payload.get("enhanced_prompt", "").strip()
    if not payload.get("format_valid") or not payload.get("metrics", {}).get("finished_with_eos"):
        raise HarnessError("Rewriter 输出未完整结束或缺少 H3 字段；已保存原始输出，视频未提交")
    if text.startswith(alignment):
        text = text[len(alignment) :].lstrip()
    elif alignment.startswith("How the reference pictures align"):
        header, separator, body = text.partition("\n")
        # Frame roles and clip duration are request metadata, not model-authored
        # choreography. Only normalize the timestamp in this exact role mapping.
        pattern = r"How the reference pictures align with the target video — Picture 1 \(from Shot 1\) aligns with the 0\.00-second mark of the target video; Picture 2 \(from Shot 1\) aligns with the \d+(?:\.\d+)?-second mark of the target video\."
        if separator and re.fullmatch(pattern, header):
            text = body.lstrip()
    positions = [text.find(f) for f in FIELDS]
    if positions[0] != 0 or any(text.count(f) != 1 for f in FIELDS) or positions != sorted(positions):
        raise HarnessError("Rewriter 字段结构无效，视频未提交")
    return text


def content_warnings(payload, source, proper_names=(), *, alignment=ALIGNMENT):
    """Report semantic deviations without changing or rejecting model prose."""
    text = validate_output(payload, source, proper_names, alignment=alignment)
    warnings = []
    header = payload["enhanced_prompt"].strip().splitlines()[0]
    if header.startswith("How the reference pictures align") and header != alignment:
        warnings.append({"code": "alignment_time_normalized", "message": "接口帧对齐标记采用已声明的分镜时长；原标记与模型正文均保留在原始输出", "original": header, "submitted": alignment})
    if re.findall(r"\[Shot\s+(\d+)\]", text) != ["1"]:
        warnings.append({"code": "shot_structure", "message": "Rewriter 输出与单个连续分镜的要求不一致；保留原文"})
    if "```" in text:
        warnings.append({"code": "markdown_fence", "message": "Rewriter 正文包含 Markdown 标记；保留原文"})
    # Original dialogue, visible text and declared character names may remain
    # Chinese. Removing them for validation never changes the submitted text.
    prose = text
    for match in re.finditer(r"<d>\s*\[[^\]]+\]\s*(.*?)</d>|\"([^\"]+)\"", text, re.S):
        literal = (match.group(1) or match.group(2)).strip()
        if match.group(1) is not None:
            if literal and literal not in source:
                warnings.append({"code": "added_or_changed_dialogue", "message": "Rewriter 对白未原样出现在输入中；保留原文", "literal": literal})
            prose = prose.replace(match.group(0), " ")
        elif literal and literal in source:
            prose = prose.replace(match.group(0), " ")
    for name in sorted(set(proper_names), key=len, reverse=True):
        if name and name in source:
            prose = prose.replace(name, " ")
    if CJK.search(prose):
        warnings.append({"code": "chinese_narrative", "message": "Rewriter 叙述中含有中文；保留原文"})
    if re.search(r"\b(?:PROP|STATE|CHAR|SCENE)_[A-Za-z0-9_]+", text):
        warnings.append({"code": "internal_identifiers", "message": "Rewriter 输出包含内部对象标识；保留原文"})
    return warnings


def inference_request(request):
    """Separate inference inputs from validation/serialization adapter revisions."""
    identity = request["rewriter"]
    return {**request, "rewriter": {**identity, "source_sha256": {k: v for k, v in identity["source_sha256"].items() if k != "adapter_module"}}}


class LocalPromptRewriter:
    def __init__(self, config, proper_names=()):
        self.config = config
        self.proper_names = tuple(sorted(set(proper_names)))
        self.constraints = CONSTRAINTS.with_name("minimax_rewriter_keyframes.md") if config.input_mode == "first_last_frame" else CONSTRAINTS.with_name("minimax_rewriter_grounded.md") if config.input_mode == "first_frame" else CONSTRAINTS
        self.script = Path(config.script).resolve()
        self.python = Path(config.python).resolve()
        self.models = Path(config.model_root).resolve()
        base = self.models / "Qwen3-VL-8B-Instruct"
        adapter = self.models / "MiniMax-H3-Prompt-Rewriter-LoRA-8B"
        small_files = [
            self.script,
            self.script.parent / "prompt_template.py",
            self.constraints,
            Path(__file__),
            base / "config.json",
            base / "model.safetensors.index.json",
            adapter / "adapter_config.json",
        ]
        for path in [self.python, *small_files, adapter / "adapter_model.safetensors"]:
            if not path.is_file():
                raise HarnessError(f"本地 Rewriter 文件不存在：{path}")
        weight_files = sorted(base.glob("*.safetensors")) + [adapter / "adapter_model.safetensors"]
        index = read_json(base / "model.safetensors.index.json")
        if any(not (base / name).is_file() for name in set(index["weight_map"].values())):
            raise HarnessError("本地 Rewriter 基座分片不完整")
        manifest = self.models / "download-manifest.json"
        self.identity = {
            "adapter_version": "local-text-h3-v1",
            "settings": config.model_dump(),
            # Code may be run from a frozen source snapshot. Location changes
            # alone must not invalidate a known cloud task or cached rewrite.
            "source_sha256": {
                name: file_hash(p) for name, p in zip(("local_runner", "official_template", "integration_constraints", "adapter_module", "base_config", "weights_index", "lora_config"), small_files)
            },
            "download_manifest_sha256": file_hash(manifest) if manifest.is_file() else None,
            "weight_files": [{"path": str(p), "bytes": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns} for p in weight_files],
            "rewriter_task": "fl2av" if config.input_mode == "first_last_frame" else "i2av" if config.input_mode == "first_frame" else "t2av",
            "input_mode": config.input_mode,
            "preserved_character_names": self.proper_names,
        }

    def rewrite(self, source, duration_seconds, aspect_ratio, target_task, directory, check_pause=lambda: None, *, first_frame=None, last_frame=None):
        if not source.strip():
            raise HarnessError("Rewriter 输入为空")
        if target_task not in {"i2av", "i2v"} or not 4 <= duration_seconds <= 15 or not float(duration_seconds).is_integer():
            raise HarnessError("本地 H3 改写仅支持 4–15 秒整数时长的图生视频")
        if aspect_ratio not in {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}:
            raise HarnessError("本地 Rewriter 不支持该画幅")
        request = {"source_prompt": source, "duration_seconds": int(duration_seconds), "aspect_ratio": aspect_ratio, "target_task": target_task, "rewriter": self.identity}
        both = self.config.input_mode == "first_last_frame"
        alignment = frame_alignment(duration_seconds, both)
        if self.config.input_mode in {"first_frame", "first_last_frame"}:
            if not first_frame or not Path(first_frame).is_file():
                raise HarnessError("图像条件 Rewriter 需要实际首帧；不能退回无图改写")
            first_frame = Path(first_frame).resolve()
            request["first_frame_sha256"] = file_hash(first_frame)
        elif first_frame is not None:
            raise HarnessError("text_only 模式不能忽略传入的首帧")
        if both:
            if not last_frame or not Path(last_frame).is_file():
                raise HarnessError("首尾帧 Rewriter 需要实际目标尾帧；不能退回仅首帧改写")
            last_frame = Path(last_frame).resolve()
            request["last_frame_sha256"] = file_hash(last_frame)
        elif last_frame is not None:
            raise HarnessError("当前 Rewriter 模式不能忽略传入的尾帧")

        def check_payload(payload):
            # The local runner strips boundary whitespace when reading --prompt-file.
            # Accept that serialization change, while preserving exact internal text,
            # the original source file, image bindings, duration and aspect checks.
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or prompt not in (source, source.strip()) or payload.get("duration") != int(duration_seconds) or payload.get("resolution") != aspect_ratio:
                raise HarnessError("Rewriter 输出与当前请求不匹配")
            if first_frame:
                refs = payload.get("reference_frames", [])
                if payload.get("task") != ("fl2av" if both else "i2av") or len(refs) != (2 if both else 1) or refs[0].get("role") != "first_frame" or refs[0].get("sha256") != request["first_frame_sha256"]:
                    raise HarnessError("Rewriter 未使用与视频请求一致的首帧")
                if both and (refs[1].get("role") != "last_frame" or refs[1].get("sha256") != request["last_frame_sha256"]):
                    raise HarnessError("Rewriter 未使用与视频请求一致的目标尾帧")
                if not payload.get("image_grid_thw") or (both and len(payload["image_grid_thw"]) != 2):
                    raise HarnessError("Rewriter 缺少实际视觉输入记录")
            body = validate_output(payload, source, self.proper_names, alignment=alignment)
            warnings = content_warnings(payload, source, self.proper_names, alignment=alignment)
            write_json(root / "validation.json", {"policy": self.config.content_policy, "warnings": warnings})
            critical = [w for w in warnings if w["code"] in {"shot_structure", "added_or_changed_dialogue", "internal_identifiers"}]
            if self.config.content_policy == "block" and critical:
                raise HarnessError("Rewriter 内容验收未通过：" + ", ".join(w["code"] for w in critical))
            return body, warnings
        key = digest(request)
        root = Path(directory).resolve() / key
        root.mkdir(parents=True, exist_ok=True)
        check_pause()
        # All pipeline callers share the one local GPU; never load two 8B workers.
        # Frozen helper snapshots still share the same physical model/GPU.
        lock_dir = self.models / ".pipeline-rewriter-lock"
        lock_dir.mkdir(exist_ok=True)
        with run_lock(lock_dir):
            cached = root / "record.json"
            if cached.exists():
                record = read_json(cached)
                if record["input_hash"] != key or file_hash(root / "model-output.json") != record["raw_output_sha256"] or file_hash(root / "minimax-prompt.txt") != record["prompt_file_sha256"]:
                    raise HarnessError("Rewriter 缓存校验失败，请检查改写记录")
                raw_body, _ = check_payload(read_json(root / "model-output.json"))
                if record["prompt"] != alignment + "\n\n" + raw_body:
                    raise HarnessError("Rewriter 缓存提示词不匹配")
                return record
            write_json(root / "input.json", request)
            (root / "source.txt").write_text(source, encoding="utf-8")
            # An unsuccessful/invalid result is retained for inspection, never
            # silently translated by another model or replaced with the source.
            output = root / "model-output.json"
            if not output.exists():
                core_hash = digest(inference_request(request))
                for prior_input in sorted(root.parent.glob("*/input.json")):
                    prior_output = prior_input.parent / "model-output.json"
                    if prior_input.parent == root or not prior_output.is_file():
                        continue
                    if digest(inference_request(read_json(prior_input))) != core_hash:
                        continue
                    prior_record = prior_input.parent / "record.json"
                    raw_hash = file_hash(prior_output)
                    if prior_record.exists() and read_json(prior_record)["raw_output_sha256"] != raw_hash:
                        raise HarnessError("可复用改写原文的哈希不一致，未覆盖或重新推理")
                    shutil.copyfile(prior_output, output)
                    write_json(root / "raw-reuse.json", {"source": str(prior_output), "sha256": raw_hash, "inference_input_hash": core_hash,
                                                         "reason": "Identical source, images, model weights, helper, template and settings; only integration adapter revision differs."})
                    break
            if not output.exists():
                args = [
                    str(self.python),
                    str(self.script),
                    "--prompt-file",
                    str(root / "source.txt"),
                    "--duration",
                    str(int(duration_seconds)),
                    "--resolution",
                    aspect_ratio,
                    "--max-new-tokens",
                    str(self.config.max_new_tokens),
                    "--max-input-tokens",
                    str(self.config.max_input_tokens),
                    "--cpu-layers",
                    str(self.config.cpu_layers),
                    "--placement",
                    "balanced",
                    "--no-stream",
                    "--model-root",
                    str(self.models),
                    "--system-suffix-file",
                    str(self.constraints),
                    "--output",
                    str(output),
                ]
                if first_frame:
                    args.extend(["--task", "fl2av" if both else "i2av", "--first-frame", str(first_frame), "--image-max-pixels", str(self.config.image_max_pixels)])
                if both:
                    args.extend(["--last-frame", str(last_frame)])
                env = os.environ.copy()
                for name in list(env):
                    if name.upper().endswith("API_KEY") or name in {"PYTHONPATH", "PYTHONHOME"}:
                        env.pop(name, None)
                env.update(PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
                with (root / "stdout.log").open("w", encoding="utf-8") as stdout, (root / "stderr.log").open("w", encoding="utf-8") as stderr:
                    process = subprocess.Popen(
                        args, cwd=self.script.parent, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    )
                    started = time.monotonic()
                    try:
                        while process.poll() is None:
                            check_pause()
                            if time.monotonic() - started > self.config.timeout_seconds:
                                raise HarnessError("本地 Rewriter 超时；日志已保存，视频未提交")
                            time.sleep(0.5)
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                    if process.returncode != 0 or not output.is_file():
                        raise HarnessError(f"本地 Rewriter 失败，视频未提交；诊断日志：{root / 'stderr.log'}")
            payload = read_json(output)
            body, warnings = check_payload(payload)
            effective = alignment + "\n\n" + body
            (root / "minimax-prompt.txt").write_text(effective + "\n", encoding="utf-8")
            record = {
                "status": "ready",
                "created_at": now(),
                "input_hash": key,
                "source_prompt": source,
                "prompt": effective,
                "raw_enhanced_prompt": payload["enhanced_prompt"],
                "adaptation": ("FL2AV" if both else "I2AV") + " alignment line only; generated body is not manually rewritten",
                "submitted_alignment": alignment,
                "validation_policy": self.config.content_policy,
                "validation_warnings": warnings,
                "first_frame_sha256": request.get("first_frame_sha256"),
                "last_frame_sha256": request.get("last_frame_sha256"),
                "rewriter": self.identity,
                "metrics": payload["metrics"],
                "record_path": str(cached),
                "raw_output_sha256": file_hash(output),
                "prompt_file_sha256": file_hash(root / "minimax-prompt.txt"),
            }
            write_json(cached, record)
            return record
