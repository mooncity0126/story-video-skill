from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from .client import X2VClient
from .contracts import discover
from .engine import Runner, create_run
from .media import assemble, executable
from .models import Config, load_config, load_plan
from .planning import prepare_board
from .continuity import review_objects
from .rewriter import LocalPromptRewriter
from .text import plan
from .util import HarnessError, read_json, run_lock, safe_error, write_json


def parser():
    p = argparse.ArgumentParser(description="剧本驱动的 LightX2V 官方云 API 制作流水线")
    sub = p.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="环境与实时模型能力检查，不显示密钥")
    doctor.add_argument("--config")
    doctor.add_argument("--offline", action="store_true")
    planning = sub.add_parser("plan", help="独立文本服务规划；未配置时导出提示词")
    planning.add_argument("--script", required=True)
    planning.add_argument("--config")
    planning.add_argument("--output", required=True)
    planning.add_argument("--characters", help="复用已有角色 JSON，减少一次文本模型调用")
    planning.add_argument("--reference-storyboard", help="保留已有场景和道具设定，由文本模型重新组织分镜；需同时提供 --characters")
    planning.add_argument("--keep-prefix", type=int, default=0, help="重新规划时保留前几个镜头原文和时长，其余由文本模型生成")
    optimizing = sub.add_parser("optimize-plan", help="编译已有分镜的依赖和动作时长，输出到新目录")
    for name in ("script", "characters", "storyboard", "output"):
        optimizing.add_argument("--" + name, required=True)
    optimizing.add_argument("--config")
    optimizing.add_argument("--live-specs", action="store_true", help="只读查询 X2V 时长规格；不提交生成任务")
    rewriting = sub.add_parser("rewrite-prompt", help="仅本地改写一条分镜提示词，不创建 run 或访问视频 API")
    rewriting.add_argument("--prompt-file", required=True)
    rewriting.add_argument("--first-frame", help="图像条件改写使用的实际首帧")
    rewriting.add_argument("--last-frame", help="首尾帧模式使用的实际目标尾帧")
    rewriting.add_argument("--duration", type=int, choices=range(4, 16), default=5)
    rewriting.add_argument("--aspect-ratio", default="9:16")
    rewriting.add_argument("--config")
    rewriting.add_argument("--output", required=True)
    imp = sub.add_parser("import-plan", help="校验已有角色/分镜 JSON 并创建独立 run")
    for name in ("script", "characters", "storyboard"):
        imp.add_argument("--" + name, required=True)
    imp.add_argument("--config")
    imp.add_argument("--output-root", default="runs")
    for name in ("dry-run", "execute", "resume", "rerun-shot", "retry-node", "repair-image", "repair-prompt", "assemble", "status", "resolve-submission", "collect-history", "review-video"):
        cmd = sub.add_parser(name)
        cmd.add_argument("run", help="run 目录路径（其末级目录名即 run_id）")
        if name in {"execute", "resume", "rerun-shot", "retry-node", "repair-image", "repair-prompt"}:
            cmd.add_argument("--execute", action="store_true", help="显式允许云端付费任务")
        if name == "dry-run":
            cmd.add_argument("--no-quote", action="store_true")
        if name == "rerun-shot":
            cmd.add_argument("--shot-id", required=True)
            cmd.add_argument("--from", dest="from_stage", choices=["frame", "video"], default="frame")
        if name == "review-video":
            cmd.add_argument("--shot-id", required=True)
            cmd.add_argument("--decision", required=True, choices=["approve", "reject"])
            cmd.add_argument("--notes", required=True, help="实际审看的范围、依据和发现")
            cmd.add_argument("--reviewer", default="operator")
        if name in {"retry-node", "repair-image", "repair-prompt", "resolve-submission"}:
            cmd.add_argument("--node-id", required=True)
        if name == "resolve-submission":
            group = cmd.add_mutually_exclusive_group(required=True)
            group.add_argument("--task-id")
            group.add_argument("--confirmed-not-submitted", action="store_true", help="已人工核实云端没有创建该任务")
    return p


def perform(args):
    if args.command == "plan":
        return plan(args.script, load_config(args.config), args.output, characters=args.characters, reference_storyboard=args.reference_storyboard, keep_prefix=args.keep_prefix)
    if args.command == "optimize-plan":
        import shutil

        config = load_config(args.config)
        chars, board = load_plan(args.characters, args.storyboard)
        contract = None
        if args.live_specs:
            client = X2VClient(config)
            try:
                contract = discover(client, config)["video"]
            finally:
                client.close()
        board, report = prepare_board(board, config, contract)
        review = review_objects(board, config.require_object_states)
        root = Path(args.output).resolve()
        root.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(args.script, root / "script.md")
        write_json(root / "characters.json", chars.model_dump())
        write_json(root / "storyboard.json", board.model_dump())
        write_json(root / "planning-report.json", report)
        write_json(root / "continuity-review.json", review)
        return {"status": "optimized", "output": str(root), "report": report}
    if args.command == "rewrite-prompt":
        config = load_config(args.config)
        if not config.prompt_rewriter.enabled:
            raise HarnessError("当前配置未启用本地 Prompt Rewriter")
        source = Path(args.prompt_file).read_text(encoding="utf-8-sig").strip()
        # Deliberately before construction of any cloud client.
        return LocalPromptRewriter(config.prompt_rewriter).rewrite(source, args.duration, args.aspect_ratio, "i2av", args.output, first_frame=args.first_frame, last_frame=args.last_frame)
    if args.command in {"doctor", "plan", "import-plan"}:
        config = load_config(args.config)
    else:
        config = Config.model_validate(read_json(Path(args.run) / "config.json"))
    client = X2VClient(config)
    try:
        if args.command == "doctor":
            result = {
                "python": sys.version.split()[0],
                "ffmpeg": executable(config.ffmpeg),
                "ffprobe": executable(config.ffprobe),
                "x2v_key_present": bool(client.key),
                "text_provider_configured": bool(config.text.endpoint and config.text.model),
                "default_aspect_ratio": config.aspect_ratio,
            }
            if not args.offline:
                result["models"] = {role: c.spec for role, c in discover(client, config).items()}
            return result
        if args.command == "import-plan":
            root = create_run(args.script, args.characters, args.storyboard, config, args.output_root, client)
            runner = Runner(root, client)
            try:
                runner.export()
            finally:
                runner.close()
            return {"run_id": root.name, "run_directory": str(root), "status": "imported_validated"}
        if args.command == "status":
            root = Path(args.run).resolve()
            with sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = [
                    dict(r) for r in db.execute("SELECT n.node_id,a.status,a.remote_status,a.task_id,a.quote,a.error FROM nodes n LEFT JOIN attempts a ON n.current_id=a.attempt_id ORDER BY n.node_id")
                ]
                reserved = db.execute("SELECT COALESCE(SUM(reserved),0) FROM attempts").fetchone()[0]
            return {"run_id": root.name, "steps": rows, "budget_reserved_credits": reserved, "delivery": read_json(root / "manifest.json")["delivery"]}
        with run_lock(args.run):
            runner = Runner(args.run, client, config=config)
            try:
                if args.command == "dry-run":
                    return runner.dry_run(quote=not args.no_quote)
                if args.command == "collect-history":
                    result = runner.collect_history()
                    runner.export()
                    return result
                if args.command == "assemble":
                    runner.check_assembly_inputs()
                    report = assemble(runner.root, runner.board, runner.store, config)
                    runner.export()
                    return report
                if args.command == "review-video":
                    return runner.record_operator_decision(args.shot_id, decision=args.decision, notes=args.notes, reviewer=args.reviewer)
                if args.command == "resolve-submission":
                    a = runner.store.current(args.node_id)
                    if not a or a["task_id"] or a["status"] not in {"submission_unknown", "submitting"}:
                        raise HarnessError("只有无 task_id 的提交未知记录可以核实")
                    if args.task_id:
                        found = client.query(args.task_id)
                        choice = getattr(config, runner.graph[args.node_id]["role"])
                        expected_model = (a.get("request") or {}).get("api_parameters", {}).get("model_cls", choice.model_cls)
                        if found.get("model_cls") and found["model_cls"] != expected_model:
                            raise HarnessError("核实的 task_id 模型与该节点不匹配")
                        expected_prompt = (a.get("request") or {}).get("api_parameters", {}).get("prompt")
                        actual_prompt = found.get("params", {}).get("prompt")
                        if expected_prompt and actual_prompt and expected_prompt != actual_prompt:
                            raise HarnessError("核实的 task_id 提示词与原请求不匹配")
                        runner.store.update(a["attempt_id"], task_id=args.task_id, status="submitted", remote_status=found["status"], error=None)
                    else:
                        runner.store.update(a["attempt_id"], status="abandoned", reserved=0, error="operator confirmed no remote task")
                        runner.invalidate([args.node_id])
                    runner.store.event(args.node_id, "submission_resolved", args.task_id or "operator confirmed not submitted")
                    runner.export()
                    return {"status": "resolved", "next": "resume --execute"}
                if args.command == "repair-image":
                    repair = runner.repair_image_from_review(args.node_id)
                    if not args.execute:
                        return repair
                if args.command == "repair-prompt":
                    repair = runner.repair_prompt_from_review(args.node_id)
                    if not args.execute:
                        return repair
                if args.command in {"rerun-shot", "retry-node"}:
                    target = args.node_id if args.command == "retry-node" else args.from_stage + ":" + args.shot_id
                    affected = runner.invalidate([target])
                    if not args.execute:
                        return {"invalidated": affected, "submitted": False, "next": "resume --execute"}
                result = runner.execute(explicit=args.execute)
                if not result["issues"]:
                    try:
                        result["assembly"] = assemble(runner.root, runner.board, runner.store, runner.config)
                    except HarnessError as exc:
                        result["assembly_issue"] = safe_error(exc)
                runner.export()
                return result
            finally:
                runner.close()
    finally:
        client.close()


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        result = perform(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("issues") or result.get("assembly_issue") else 0
    except ValidationError as exc:
        fields = [".".join(map(str, e["loc"])) for e in exc.errors(include_input=False, include_url=False)]
        print(json.dumps({"status": "needs_review", "error": "数据校验失败", "fields": fields}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"error": safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
