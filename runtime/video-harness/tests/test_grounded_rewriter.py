import sys

from PIL import Image
import pytest

from conftest import EXAMPLE
from video_harness.engine import build_graph
from video_harness.models import Config, PromptRewriterConfig, load_plan
from video_harness.rewriter import LocalPromptRewriter
from video_harness.util import HarnessError, digest


WORKER = '''from pathlib import Path
import argparse, json, hashlib
p=argparse.ArgumentParser()
for name in ['prompt-file','duration','resolution','output','first-frame','last-frame','task']:
 p.add_argument('--'+name)
a,_=p.parse_known_args()
counter=Path(__file__).with_name('calls.txt')
counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')
source=Path(a.prompt_file).read_text(encoding='utf-8')
text='integrated_multimodal_description: [Shot 1] The same person turns her head.'
if 'MULTI_SHOT' in source:text+=' [Shot 2] A cut.'
text+='\\noverall_soundscape: Quiet room.\\nnon_diegetic_music: N/A'
ref={'role':'first_frame','sha256':hashlib.sha256(Path(a.first_frame).read_bytes()).hexdigest()}
if 'WRONG_IMAGE' in source:ref['sha256']='wrong'
data={'task':a.task,'prompt':source,'duration':int(a.duration),'resolution':a.resolution,'reference_frames':[ref],'image_grid_thw':[[1,16,16]],'enhanced_prompt':text,'format_valid':True,'metrics':{'finished_with_eos':True}}
if a.last_frame:
 last={'role':'last_frame','sha256':hashlib.sha256(Path(a.last_frame).read_bytes()).hexdigest()}
 if 'WRONG_LAST' in source:last['sha256']='wrong'
 data['reference_frames'].append(last)
 data['image_grid_thw'].append([1,16,16])
Path(a.output).write_text(json.dumps(data),encoding='utf-8')
'''


def local_rewriter(tmp_path, policy="block", mode="first_frame"):
    script = tmp_path / "rewrite.py"
    script.write_text(WORKER, encoding="utf-8")
    (tmp_path / "prompt_template.py").write_text("# fixture", encoding="utf-8")
    models = tmp_path / "models"
    base = models / "Qwen3-VL-8B-Instruct"
    adapter = models / "MiniMax-H3-Prompt-Rewriter-LoRA-8B"
    base.mkdir(parents=True)
    adapter.mkdir()
    (base / "config.json").write_text("{}")
    (base / "model.safetensors.index.json").write_text('{"weight_map": {}}')
    (adapter / "adapter_config.json").write_text("{}")
    (adapter / "adapter_model.safetensors").write_bytes(b"fixture")
    config = PromptRewriterConfig(enabled=True, python=sys.executable, script=str(script), model_root=str(models), input_mode=mode, content_policy=policy)
    return LocalPromptRewriter(config)


def test_graph_waits_for_actual_frame_and_supplies_canonical_context():
    chars, board = load_plan(EXAMPLE / "characters.json", EXAMPLE / "storyboard.json")
    config = Config(shot_scheduling="dependency", require_object_states=False, rewrite_ahead=True, prompt_rewriter=PromptRewriterConfig(enabled=True, input_mode="first_frame"))
    graph = build_graph(chars, board, config)
    for shot in board.shots:
        rewrite = graph["rewrite:" + shot.shot_id]
        assert rewrite["deps"] == ["frame:" + shot.shot_id]
        scene = next(s for s in board.scenes if s.scene_id == shot.scene_id)
        assert scene.location in rewrite["prompt"] and scene.lighting in rewrite["prompt"]
        for visible in shot.visible_characters:
            character = next(c for c in chars.characters if c.character_id == visible.character_id)
            assert all(anchor in rewrite["prompt"] for anchor in character.consistency_anchors)


def test_grounded_mode_requires_frame_and_never_silently_falls_back(tmp_path):
    rewriter = local_rewriter(tmp_path)
    with pytest.raises(HarnessError, match="需要实际首帧"):
        rewriter.rewrite("turn head", 5, "9:16", "i2av", tmp_path / "cache")
    assert not (tmp_path / "calls.txt").exists()


def test_same_image_reuses_output_but_different_image_invalidates_cache(tmp_path):
    rewriter = local_rewriter(tmp_path)
    frame = tmp_path / "frame.png"
    Image.new("RGB", (32, 64), "red").save(frame)
    first = rewriter.rewrite("turn head", 5, "9:16", "i2av", tmp_path / "cache", first_frame=frame)
    repeat = rewriter.rewrite("turn head", 5, "9:16", "i2av", tmp_path / "cache", first_frame=frame)
    assert digest(first) == digest(repeat)
    assert (tmp_path / "calls.txt").read_text() == "1"
    Image.new("RGB", (32, 64), "blue").save(frame)
    changed = rewriter.rewrite("turn head", 5, "9:16", "i2av", tmp_path / "cache", first_frame=frame)
    assert changed["input_hash"] != first["input_hash"]
    assert changed["first_frame_sha256"] != first["first_frame_sha256"]
    assert (tmp_path / "calls.txt").read_text() == "2"


@pytest.mark.parametrize(("source", "message"), [("WRONG_IMAGE", "一致的首帧"), ("MULTI_SHOT", "内容验收未通过")])
def test_wrong_reference_and_critical_content_keep_raw_output_but_block(tmp_path, source, message):
    rewriter = local_rewriter(tmp_path)
    frame = tmp_path / "frame.png"
    Image.new("RGB", (32, 64)).save(frame)
    with pytest.raises(HarnessError, match=message):
        rewriter.rewrite(source, 5, "9:16", "i2av", tmp_path / "cache", first_frame=frame)
    assert len(list((tmp_path / "cache").glob("*/model-output.json"))) == 1
    assert not list((tmp_path / "cache").glob("*/record.json"))


def test_two_frame_rewriter_routes_both_images_and_invalidates_changed_endpoint(tmp_path):
    rewriter = local_rewriter(tmp_path, mode="first_last_frame")
    first, last = tmp_path / "first.png", tmp_path / "last.png"
    Image.new("RGB", (32, 64), "red").save(first)
    Image.new("RGB", (32, 64), "blue").save(last)
    with pytest.raises(HarnessError, match="实际目标尾帧"):
        rewriter.rewrite("turn", 7, "9:16", "i2av", tmp_path / "cache", first_frame=first)
    one = rewriter.rewrite("turn", 7, "9:16", "i2av", tmp_path / "cache", first_frame=first, last_frame=last)
    assert "Picture 2 (from Shot 1) aligns with the 7.00-second mark" in one["prompt"]
    assert one["rewriter"]["rewriter_task"] == "fl2av"
    rewriter.identity["source_sha256"]["adapter_module"] = "new_validation_adapter_only"
    revalidated = rewriter.rewrite("turn", 7, "9:16", "i2av", tmp_path / "cache", first_frame=first, last_frame=last)
    assert revalidated["raw_output_sha256"] == one["raw_output_sha256"]
    assert revalidated["input_hash"] != one["input_hash"]
    assert (tmp_path / "calls.txt").read_text() == "1"
    assert list((tmp_path / "cache").glob("*/raw-reuse.json"))
    Image.new("RGB", (32, 64), "green").save(last)
    two = rewriter.rewrite("turn", 7, "9:16", "i2av", tmp_path / "cache", first_frame=first, last_frame=last)
    assert one["input_hash"] != two["input_hash"]
    assert one["last_frame_sha256"] != two["last_frame_sha256"]
    with pytest.raises(HarnessError, match="一致的目标尾帧"):
        rewriter.rewrite("WRONG_LAST", 7, "9:16", "i2av", tmp_path / "cache", first_frame=first, last_frame=last)


def test_runner_stripping_boundary_whitespace_keeps_original_and_reuses_cache(tmp_path):
    rewriter = local_rewriter(tmp_path)
    rewriter.script.write_text(WORKER.replace("read_text(encoding='utf-8')", "read_text(encoding='utf-8').strip()"), encoding="utf-8")
    rewriter = LocalPromptRewriter(rewriter.config)
    frame = tmp_path / "frame.png"
    Image.new("RGB", (32, 64)).save(frame)
    source = "\r\n  turn  head\n"
    first = rewriter.rewrite(source, 8, "9:16", "i2av", tmp_path / "cache", first_frame=frame)
    repeat = rewriter.rewrite(source, 8, "9:16", "i2av", tmp_path / "cache", first_frame=frame)
    assert first["source_prompt"] == source
    assert digest(first) == digest(repeat)
    assert (tmp_path / "calls.txt").read_text() == "1"


def test_internal_prompt_changes_still_block_submission(tmp_path):
    rewriter = local_rewriter(tmp_path)
    rewriter.script.write_text(WORKER.replace("'prompt':source", "'prompt':source.strip().replace('  ', ' ')"), encoding="utf-8")
    rewriter = LocalPromptRewriter(rewriter.config)
    frame = tmp_path / "frame.png"
    Image.new("RGB", (32, 64)).save(frame)
    with pytest.raises(HarnessError, match="输出与当前请求不匹配"):
        rewriter.rewrite(" turn  head\n", 8, "9:16", "i2av", tmp_path / "cache", first_frame=frame)
    assert list((tmp_path / "cache").glob("*/model-output.json"))
    assert not list((tmp_path / "cache").glob("*/record.json"))
