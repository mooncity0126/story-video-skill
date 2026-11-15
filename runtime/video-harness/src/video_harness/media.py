from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from .util import HarnessError, file_hash, inside, read_json, write_json


def executable(name):
    return shutil.which(name)


def run_process(args, timeout):
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise HarnessError("媒体工具不可用或处理超时") from None
    if result.returncode:
        raise HarnessError("媒体工具处理失败；请检查输入媒体、编码器和路径")
    return result.stdout


def probe(path, config, kind):
    path = Path(path)
    if kind == "image":
        try:
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                return {"width": im.width, "height": im.height, "format": im.format, "duration_ms": None, "fps_num": None, "fps_den": None, "has_audio": False, "verification": "verified"}
        except (OSError, ValueError):
            raise HarnessError("下载结果不是有效图片") from None
    if not executable(config.ffprobe):
        return {"width": None, "height": None, "duration_ms": None, "fps_num": None, "fps_den": None, "has_audio": None, "verification": "ffprobe_missing"}
    data = json.loads(run_process([config.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], config.media_timeout_seconds))
    videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise HarnessError("文件中没有有效视频流")
    stream = videos[0]
    # Container duration may include a longer audio tail. Use video duration for source range checks.
    duration = stream.get("duration", data.get("format", {}).get("duration"))
    if duration is None:
        raise HarnessError("ffprobe 未返回可检查的视频时长")
    audios = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    audio_duration = audios[0].get("duration", data.get("format", {}).get("duration", duration)) if audios else 0
    try:
        fps = Fraction(stream.get("avg_frame_rate", "0/1"))
    except (ValueError, ZeroDivisionError):
        raise HarnessError("视频帧率无效") from None
    return {
        "width": stream["width"],
        "height": stream["height"],
        "duration_ms": round(float(duration) * 1000),
        "audio_duration_ms": round(float(audio_duration) * 1000),
        "source_duration_ms": round(max(float(duration), float(audio_duration)) * 1000),
        "fps_num": fps.numerator,
        "fps_den": fps.denominator,
        "has_audio": any(s.get("codec_type") == "audio" for s in data["streams"]),
        "verification": "verified",
    }


def asset_record(root, path, kind, config, *, task_id=None, parents=None):
    return {
        "path": Path(path).relative_to(root).as_posix(),
        "sha256": file_hash(path),
        "bytes": Path(path).stat().st_size,
        "kind": kind,
        "task_id": task_id,
        "parent_asset_ids": parents or [],
        "technical": probe(path, config, kind),
    }


def check_clip(meta, planned_ms):
    if meta.get("duration_ms") is None:
        return "ffprobe_missing: 已下载，需测量时长后才能拼接"
    if meta["duration_ms"] > 15000 + 1:
        raise HarnessError("实际源视频超过单次15秒上限，请选择符合要求的模型")
    if meta["duration_ms"] < planned_ms:
        raise HarnessError(f"源视频不足：实际 {meta['duration_ms']}ms，规划 {planned_ms}ms；请修正分镜或显式重做")
    return None


def extract_timeline_tail(source, target, config, duration_ms, source_in_ms=0):
    """Extract the retained last frame; duration_ms=None retains the complete source."""
    if not executable(config.ffmpeg) or not executable(config.ffprobe):
        raise HarnessError("连续分镜需要 FFmpeg/ffprobe，安装后可 resume")
    meta = probe(source, config, "video")
    check_clip(meta, meta["duration_ms"] if duration_ms is None else source_in_ms + duration_ms)
    # Trim then reverse makes the first decoded output exactly the last retained
    # frame, including variable-fps sources. Clips are bounded to 15 seconds.
    start = source_in_ms / 1000
    vf = f"trim=start={start:.9f},reverse" if duration_ms is None else f"trim=start={start:.9f}:end={(source_in_ms + duration_ms) / 1000:.9f},reverse"
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    run_process(
        [config.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vf", vf, "-frames:v", "1", str(target)],
        config.media_timeout_seconds,
    )


def identity_board(sources, labels, target):
    columns = min(3, len(sources))
    rows = (len(sources) + columns - 1) // columns
    w, h, header = 512, 768, 40
    result = Image.new("RGB", (columns * w, rows * (h + header)), "#eeeeee")
    draw = ImageDraw.Draw(result)
    for i, (source, label) in enumerate(zip(sources, labels, strict=True)):
        x, y = (i % columns) * w, (i // columns) * (h + header)
        with Image.open(source) as image:
            thumb = ImageOps.contain(image.convert("RGB"), (w, h))
        result.paste(thumb, (x + (w - thumb.width) // 2, y + header + (h - thumb.height) // 2))
        draw.text((x + 12, y + 12), label, fill="black")
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    result.save(target, format="PNG")


def assemble_full_sources(root, sources, config, *, max_duration_ms=None):
    """Join complete native A/V sources; never trim them to requested durations.

    Each source has job_id, asset and requested_duration_ms. Content decisions
    remain with the caller; this operation only validates and renders media.
    """
    root = Path(root)
    if not sources or not executable(config.ffmpeg) or not executable(config.ffprobe):
        raise HarnessError("完整原生音画合成需要素材和 FFmpeg/ffprobe")
    args = [config.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    filters, labels, clips = [], [], []
    source_total_ms = 0
    w, h = config.export_width, config.export_height
    fit = f"scale={w}:{h}:force_original_aspect_ratio={'decrease' if config.fit == 'pad' else 'increase'}"
    fit += f",pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black" if config.fit == "pad" else f",crop={w}:{h}"
    for i, item in enumerate(sources):
        asset = item["asset"]
        path = inside(root, asset["path"])
        if not path.is_file() or file_hash(path) != asset["sha256"]:
            raise HarnessError("源视频缺失或哈希变化")
        meta = probe(path, config, "video")
        check_clip(meta, item["requested_duration_ms"])
        if not meta["has_audio"]:
            raise HarnessError("原生音画素材缺少音轨；不会以配音或静音替代")
        full_ms = meta["source_duration_ms"]
        # Audio may outlast the picture. Hold the actual last frame instead of
        # cutting the voice, changing playback speed, or overlapping dialogue.
        hold_seconds = max(0, meta["audio_duration_ms"] - meta["duration_ms"]) / 1000
        args += ["-threads", "1", "-i", str(path)]
        filters.append(f"[{i}:v:0]setpts=PTS-STARTPTS,{fit},setsar=1,"
                       f"tpad=stop_mode=clone:stop_duration={hold_seconds:.9f},fps={config.fps},format=yuv420p[v{i}]")
        filters.append(f"[{i}:a:0]asetpts=PTS-STARTPTS,aresample=48000[a{i}]")
        labels.append(f"[v{i}][a{i}]")
        clips.append({"shot_id": item["job_id"], "source": asset["path"], "source_in_ms": 0,
                      "requested_duration_ms": item["requested_duration_ms"], "duration_ms": full_ms,
                      "timeline_in_ms": source_total_ms, "actual_source": meta,
                      "retention_policy": "full_source", "silence_added": False,
                      "video_tail_hold_ms": round(hold_seconds * 1000)})
        source_total_ms += full_ms
    if max_duration_ms is not None and source_total_ms > max_duration_ms:
        raise HarnessError("完整音画超过用户时长上限；请调整分镜或台词，不能硬切音轨")
    filters.append("".join(labels) + f"concat=n={len(sources)}:v=1:a=1[v][a]")
    target = root / "exports/final.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name("final.pending.mp4")
    # No trim/atrim, -t, -frames:v or -shortest: all decoded audio is retained.
    args += ["-filter_complex_threads", "2", "-filter_complex", ";".join(filters),
             "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-threads", "4",
             "-preset", "medium", "-crf", "18", "-c:a", "aac", "-ar", "48000", "-ac", "2",
             "-movflags", "+faststart", str(pending)]
    run_process(args, config.media_timeout_seconds)
    record = asset_record(root, pending, "video", config, parents=[s["job_id"] for s in sources])
    actual_ms = record["technical"]["source_duration_ms"]
    if abs(actual_ms - source_total_ms) > len(sources) * (1000 / config.fps + 25):
        raise HarnessError("完整原生音画合成长度与源素材不一致")
    if max_duration_ms is not None and actual_ms > max_duration_ms:
        raise HarnessError("导出音画超过用户时长上限；不能硬切音轨")
    pending.replace(target)
    record["path"] = target.relative_to(root).as_posix()
    return {"status": "assembled_with_recorded_review_limits", "retention_policy": "full_source",
            "settings": {"width": w, "height": h, "fps": config.fps, "fit": config.fit},
            "asset": record, "clips": clips, "source_total_duration_ms": source_total_ms,
            "actual_duration_ms": actual_ms,
            "known_issues": ["完整保留原生音轨不代表原始台词完整；不额外进行转写、音色或口型检查。"]}


def assemble(root, board, store, config):
    from .operator_review import load_decision
    root = Path(root)
    if not executable(config.ffmpeg) or not executable(config.ffprobe):
        raise HarnessError("FFmpeg/ffprobe 未安装；逐镜素材已保留，可安装后 assemble")
    workspace = root / "exports" / "normalized"
    workspace.mkdir(parents=True, exist_ok=True)
    clips, checks = [], []
    for shot in sorted(board.shots, key=lambda s: s.timeline_in_ms):
        attempt = store.current("video:" + shot.shot_id)
        if not attempt or attempt["status"] != "succeeded" or not attempt["output"]:
            raise HarnessError(f"镜头 {shot.shot_id} 尚无可用视频")
        asset = attempt["output"]
        if config.quality.enabled:
            review = store.current("check:video:" + shot.shot_id)
            if not review or review["status"] != "succeeded" or not review["output"]:
                raise HarnessError(f"镜头 {shot.shot_id} 内容检查尚未通过")
            report_path = inside(root, review["output"]["path"])
            if not report_path.is_file() or file_hash(report_path) != review["output"]["sha256"]:
                raise HarnessError(f"镜头 {shot.shot_id} 检查证据缺失或被修改")
            report = read_json(report_path)
            if report["verdict"]["outcome"] != "pass" or report["source_hashes"].get("video:" + shot.shot_id) != asset["sha256"]:
                raise HarnessError(f"镜头 {shot.shot_id} 当前视频与通过检查的视频不一致")
        if config.quality.require_operator_review:
            decision, decision_hash = load_decision(root, shot.shot_id, attempt)
            review = store.current("check:operator:" + shot.shot_id)
            if decision["decision"] != "approve" or not review or review["status"] != "succeeded" or not review["output"]:
                raise HarnessError(f"镜头 {shot.shot_id} 人工/代理复核尚未通过")
            report_path = inside(root, review["output"]["path"])
            if not report_path.is_file() or file_hash(report_path) != review["output"]["sha256"]:
                raise HarnessError("人工/代理复核证据缺失或已改变")
            report = read_json(report_path)
            if report["operator_decision_sha256"] != decision_hash or report["source_hashes"].get("video:" + shot.shot_id) != asset["sha256"]:
                raise HarnessError("人工/代理复核与当前候选不一致")
        source = inside(root, asset["path"])
        if not source.is_file() or file_hash(source) != asset["sha256"]:
            raise HarnessError(f"镜头 {shot.shot_id} 文件缺失或哈希变化，请 resume")
        meta = probe(source, config, "video")
        check_clip(meta, shot.duration_ms)
        start_frame = round(shot.timeline_in_ms * config.fps / 1000)
        end_frame = round((shot.timeline_in_ms + shot.duration_ms) * config.fps / 1000)
        frame_count = end_frame - start_frame
        if frame_count < 1:
            raise HarnessError("镜头短于一个导出帧")
        seconds = frame_count / config.fps
        w, h = config.export_width, config.export_height
        fit = f"scale={w}:{h}:force_original_aspect_ratio={'decrease' if config.fit == 'pad' else 'increase'}"
        fit += f",pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black" if config.fit == "pad" else f",crop={w}:{h}"
        vf = f"trim=start=0:duration={seconds:.9f},setpts=PTS-STARTPTS,{fit},setsar=1,fps={config.fps},format=yuv420p"
        out = workspace / (shot.shot_id + ".mp4")
        args = [config.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
        if meta["has_audio"]:
            af = f"[0:a:0]atrim=start=0:duration={seconds:.9f},asetpts=PTS-STARTPTS,aresample=48000,apad,atrim=duration={seconds:.9f}[a]"
        else:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            af = f"[1:a:0]atrim=duration={seconds:.9f},asetpts=PTS-STARTPTS[a]"
        args += [
            "-filter_complex",
            f"[0:v:0]{vf}[v];{af}",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-frames:v",
            str(frame_count),
            "-t",
            f"{seconds:.9f}",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out),
        ]
        run_process(args, config.media_timeout_seconds)
        normalized = probe(out, config, "video")
        if abs(normalized["duration_ms"] - seconds * 1000) > 1000 / config.fps + 1:
            raise HarnessError(f"{shot.shot_id} 归一化长度不符合时间线")
        clips.append(out)
        checks.append(
            {
                "shot_id": shot.shot_id,
                "source": asset["path"],
                "source_in_ms": 0,
                "duration_ms": shot.duration_ms,
                "actual_source": meta,
                "silence_added": not meta["has_audio"],
                "frames": frame_count,
            }
        )
    # Filenames are validated IDs; the concat file only references files in this directory.
    listing = workspace / "concat.txt"
    listing.write_text("\n".join("file '" + p.name + "'" for p in clips), encoding="utf-8")
    target = root / "exports" / "final.mp4"
    pending = target.with_name("final.pending.mp4")
    run_process(
        [config.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(pending)],
        config.media_timeout_seconds,
    )
    record = asset_record(root, pending, "video", config, parents=[store.current("video:" + s.shot_id)["attempt_id"] for s in board.shots])
    if abs(record["technical"]["duration_ms"] - board.total_duration_ms) > 1000 / config.fps + 2:
        raise HarnessError("拼接后真实片长与时间线不一致")
    pending.replace(target)
    record["path"] = target.relative_to(root).as_posix()
    report = {
        "status": "assembled_needs_review",
        "settings": {"width": config.export_width, "height": config.export_height, "fps": config.fps, "fit": config.fit},
        "asset": record,
        "clips": checks,
        "checks": ["叙事、身份一致性、对白、声音内容和字幕尚需人工审看"],
        "known_issues": ["缺失音轨以静音补齐；存在音轨不代表声音内容已验证"],
    }
    write_json(root / "exports" / "assembly.json", report)
    return report
