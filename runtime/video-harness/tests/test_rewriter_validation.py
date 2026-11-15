import pytest

from video_harness.rewriter import content_warnings, frame_alignment, validate_output
from video_harness.util import HarnessError


def payload(description):
    return {
        "enhanced_prompt": "integrated_multimodal_description: [Shot 1] " + description + "\noverall_soundscape: Room tone.\nnon_diegetic_music: N/A",
        "format_valid": True,
        "metrics": {"finished_with_eos": True},
    }


def test_declared_chinese_name_is_preserved_verbatim():
    raw = payload('A character named "小林" taps the mold. 小林\'s hand stops.')
    original = raw["enhanced_prompt"]
    assert validate_output(raw, "小林拍打模具，手停住。", ["小林"]) == original
    assert raw["enhanced_prompt"] == original
    assert content_warnings(raw, "小林拍打模具，手停住。", ["小林"]) == []


def test_first_last_alignment_does_not_change_model_body():
    raw = payload("The same person reaches the pictured final state.")
    body = raw["enhanced_prompt"]
    alignment = frame_alignment(7, both=True)
    raw["enhanced_prompt"] = alignment + "\n\n" + body
    assert validate_output(raw, "Reach the end", alignment=alignment) == body
    assert not content_warnings(raw, "Reach the end", alignment=alignment)
    assert validate_output(raw, "Reach the end", alignment=frame_alignment(6, both=True)) == body
    assert content_warnings(raw, "Reach the end", alignment=frame_alignment(6, both=True))[0]["code"] == "alignment_time_normalized"
    raw["enhanced_prompt"] = raw["enhanced_prompt"].replace("Picture 2", "Picture 3")
    with pytest.raises(HarnessError):
        validate_output(raw, "Reach the end", alignment=alignment)


@pytest.mark.parametrize(
    ("description", "source", "names"),
    [
        ("小林站在桌前。", "小林站在桌前。", ["小林"]),
        ("小夏 lifts a plate.", "小夏举起盘子。", ["小林"]),
        ("小林 lifts a plate.", "手举起盘子。", ["小林"]),
    ],
)
def test_names_do_not_hide_language_warnings(description, source, names):
    raw = payload(description)
    assert validate_output(raw, source, names) == raw["enhanced_prompt"]
    assert "chinese_narrative" in {w["code"] for w in content_warnings(raw, source, names)}


def test_multiple_shots_and_added_dialogue_are_preserved_with_warnings():
    raw = payload('A woman looks at cups. [Shot 2] She says <d>[Chinese] 喝完吧。</d>')
    original = raw["enhanced_prompt"]
    assert validate_output(raw, "看向杯子。对白：无。") == original
    warnings = content_warnings(raw, "看向杯子。对白：无。")
    assert {w["code"] for w in warnings} == {"shot_structure", "added_or_changed_dialogue"}
    assert raw["enhanced_prompt"] == original


@pytest.mark.parametrize("failure", ["truncated", "malformed", "missing_field"])
def test_incomplete_or_malformed_outputs_still_block(failure):
    raw = payload("A hand holds a cup.")
    if failure == "truncated":
        raw["metrics"]["finished_with_eos"] = False
    elif failure == "malformed":
        raw["format_valid"] = False
    else:
        raw["enhanced_prompt"] = raw["enhanced_prompt"].replace("overall_soundscape:", "other:")
    with pytest.raises(HarnessError):
        validate_output(raw, "拿着杯子。")
