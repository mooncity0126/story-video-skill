"""A self-contained, escaped preview of the compiled text plan."""

from html import escape
from pathlib import Path


def write_plan_preview(board, report, path, model):
    def h(value):
        return escape(str(value), quote=True)

    decisions = report.get("dependencies", {})
    rows = []
    for shot in sorted(board.shots, key=lambda s: s.timeline_in_ms):
        decision = decisions.get(shot.shot_id, {})
        references = decision.get("references", [])
        refs = "；".join(f"{r['shot_id']} {'尾帧' if r['kind'] == 'tail' else '首帧'}" for r in references) or "公共人物 / 场景参考"
        voice = "；".join(d.text for d in shot.audio.dialogue) or "无"
        rows.append(
            f'<article><div class="meta">{h(shot.shot_id)} · {shot.timeline_in_ms / 1000:g}–{(shot.timeline_in_ms + shot.duration_ms) / 1000:g} 秒 · {h(shot.scene_id)}</div>'
            f"<h2>{shot.duration_ms / 1000:g} 秒 · {h(shot.camera.shot_size)} · {h(shot.camera.movement)}</h2>"
            f"<p><b>首帧：</b>{h(shot.first_frame_description)}</p><p><b>动作：</b>{h(shot.action_description)}</p>"
            f"<p><b>结束：</b>{h(shot.end_state)}</p><p><b>对白：</b>{h(voice)}</p>"
            f'<div class="dependency"><b>生成参考：</b>{h(refs)}<br>{h(decision.get("reason", "保留原规划"))}</div></article>'
        )
    markup = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>分镜与执行计划</title><style>"
        'body{margin:0;background:#f2f4f7;color:#182337;font:16px/1.75 "Microsoft YaHei",sans-serif}'
        "main{max-width:1000px;margin:auto;padding:36px 24px}header,article{background:white;border-radius:14px;padding:24px;margin-bottom:18px}"
        "h1{margin:0;font-size:28px}h2{margin:10px 0;font-size:21px}.meta{color:#53677c;font-size:14px}"
        ".dependency{background:#edf4f8;padding:12px 16px;border-radius:8px}p{margin:10px 0}"
        "</style><main><header><h1>分镜与执行计划</h1>"
        f"<p>{len(board.shots)} 个镜头 · {board.total_duration_ms / 1000:g} 秒 · {h(board.aspect_ratio)} · {h(model)}</p>"
        "<p>这是文字规划预览，未生成图片或视频。描述来自文本模型，时长由动作节拍计算；依赖就绪的镜头可同时生成，成片仍按下列顺序排列。</p>"
        "<p>视频提示词在实际生成阶段交由本地 Rewriter 处理。</p></header>" + "".join(rows) + "</main></html>"
    )
    Path(path).write_text(markup, encoding="utf-8")
