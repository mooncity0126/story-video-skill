import json

import httpx
import pytest

from conftest import EXAMPLE
from test_adaptive import adaptive_config, small_board
from video_harness.models import Dialogue, QualityConfig, TextConfig
from video_harness.text import plan, normalize_board
from video_harness.plan_quality import clarify_plan_fields, split_overloaded_shots, planning_review_data, review_and_repair
from video_harness.util import HarnessError, read_json, write_json


FAIL = {"outcome": "fail", "summary": "Missing causal step", "issues": [{"shot_ids": ["U001"], "category": "missing_cause", "expected": "cause", "observed": "unexplained state change", "correction": "show cause"}]}
PASS = {"outcome": "pass", "summary": "No blocking issue in declared plan", "issues": []}


def test_plan_review_keeps_editorial_transition_separate_from_dependency():
    chars, board = small_board()
    board.shots[1].transition_from_previous = "time_jump"
    before = board.model_dump()
    reviewed = planning_review_data("story", chars, board)["candidate_storyboard"]["shots"][1]
    assert reviewed["editorial_cut_before_start"]["kind"] == "time_jump"
    assert "generation_plan" not in reviewed and "reference_plan" not in reviewed
    assert reviewed["visible_start_after_cut"] == board.shots[1].start_state
    assert reviewed["visible_end_before_next_cut"] == board.shots[1].end_state
    assert "first_frame_description" not in reviewed
    assert board.model_dump() == before


def test_review_sees_compiled_dialogue_duration_not_short_model_estimate():
    chars, board = small_board()
    shot = board.shots[0]
    shot.audio.dialogue = [Dialogue(speaker_character_id=None, text="妈妈我没能做好那个完整的蛋糕。", delivery="voice_over")]
    shot.timing.beats[0].kind = "dialogue"
    shot.timing.beats[0].dialogue_index = 0
    shot.timing.beats[0].duration_ms = 100
    reviewed = planning_review_data("Story", chars, board, adaptive_config().duration_planning)["candidate_storyboard"]["shots"][0]
    beat = reviewed["timing"]["effective_schedule"]["beats"]["A"]
    assert beat["end_ms"] - beat["start_ms"] >= 4000
    assert shot.timing.beats[0].duration_ms == 100


@pytest.mark.parametrize("change", ["valid", "extra", "missing"])
def test_prefix_repair_keeps_fixed_shot_and_rejects_suffix_structure_changes(tmp_path, change):
    chars, board = small_board()
    board, _ = normalize_board(board.model_dump(), chars, adaptive_config())
    prefix = [board.shots[0].model_copy(deep=True)]
    repaired = {"shots": [s.model_dump() for s in board.shots[1:]]}
    repaired["shots"][0]["action_description"] = "Corrected complete causal action"
    if change == "extra":
        repaired["shots"].append({**repaired["shots"][-1], "shot_id": "EXTRA"})
    elif change == "missing":
        repaired["shots"].pop(0)
    finding = {**FAIL, "issues": [{**FAIL["issues"][0], "shot_ids": ["U002"]}]}
    answers = iter([finding, repaired, PASS])

    class Reviewer:
        def generate(self, *args, **kwargs):
            return next(answers)

    args = (Reviewer(), "Story", chars, board, adaptive_config(max_shots=3), tmp_path, "schema", normalize_board)
    if change == "valid":
        result, _, _ = review_and_repair(*args, fixed_prefix=prefix)
        assert result.shots[0] == prefix[0]
        assert result.shots[1].action_description == repaired["shots"][0]["action_description"]
    else:
        with pytest.raises(HarnessError, match="增减镜头"):
            review_and_repair(*args, fixed_prefix=prefix)
    assert read_json(tmp_path / "storyboard.repair-1.raw.json") == repaired


def test_overload_review_can_split_without_increasing_total_duration(tmp_path):
    chars, board = small_board()
    board.shots[0].timing.beats[0].duration_ms = 9000
    board, _ = normalize_board(board.model_dump(), chars, adaptive_config())
    fail = {**FAIL, "issues": [{**FAIL["issues"][0], "category": "action_overload"}]}
    first, last = [board.shots[0].model_dump() for _ in range(2)]
    for shot in (first, last):
        shot["duration_ms"] = 5000
        shot["timing"]["beats"][0]["duration_ms"] = 4100
    first.update(shot_id="U001A", end_state="Intermediate state")
    last.update(shot_id="U001B", start_state="Intermediate state", transition_from_previous="same_scene",
                generation_plan={"relation": "continuous", "confidence": "high", "reason": "Continue", "references": [{"shot_id": "U001A", "kind": "tail", "reason": "State"}]})
    repaired = {"replacements": [{"source_shot_id": "U001", "shots": [first, last]}]}
    answers = iter([fail, repaired, PASS])

    class Reviewer:
        def generate(self, *args, **kwargs):
            return next(answers)

    result, report, history = review_and_repair(Reviewer(), "Story", chars, board, adaptive_config(), tmp_path, "schema", normalize_board)
    assert [s.shot_id for s in result.shots] == ["U001A", "U001B", "U002", "U003"]
    assert result.total_duration_ms == board.total_duration_ms == 20000
    assert report["duration_budget"]["difference_ms"] == 0
    assert len(history) == 2
    assert (tmp_path / "shot-splits.raw.json").exists()


@pytest.mark.parametrize("change", ["add", "remove", "reorder"])
def test_quality_repair_cannot_change_shot_structure(tmp_path, change):
    chars, board = small_board()
    repaired = board.model_dump()
    if change == "add":
        repaired["shots"].append({**repaired["shots"][0], "shot_id": "EXTRA"})
    elif change == "remove":
        repaired["shots"].pop()
    else:
        repaired["shots"].reverse()
    answers = iter([FAIL, repaired])

    class Reviewer:
        def generate(self, *args, **kwargs):
            return next(answers)

    with pytest.raises(HarnessError, match="增减镜头"):
        review_and_repair(Reviewer(), "Story", chars, board, adaptive_config(), tmp_path, "schema", normalize_board)
    assert read_json(tmp_path / "storyboard.repair-1.raw.json") == repaired
    assert not (tmp_path / "storyboard.repair-1.json").exists()


def test_explicit_split_cannot_exceed_shot_cap_or_spend_on_impossible_request(tmp_path):
    chars, board = small_board()

    class NoCalls:
        def generate(self, *args, **kwargs):
            pytest.fail("No model call when minimum split count already exceeds the cap")

    with pytest.raises(HarnessError, match="上限"):
        split_overloaded_shots(NoCalls(), "Story", chars, board, ["U001"], adaptive_config(max_shots=3), tmp_path, "schema", normalize_board)


@pytest.mark.parametrize("problem", [None, "no_hold_budget", "accelerated_action", "protected"])
def test_split_can_borrow_holds_but_cannot_rush_actions_or_grow_film(tmp_path, problem):
    chars, board = small_board()
    board.shots[0].timing.beats[0].duration_ms = 5100
    board.shots[1].timing.end_hold_ms = 4500
    board, _ = normalize_board(board.model_dump(), chars, adaptive_config())
    assert board.total_duration_ms == 20000
    first, last = [board.shots[0].model_dump() for _ in range(2)]
    for shot in (first, last):
        shot["duration_ms"] = 5000
        shot["timing"]["beats"][0]["duration_ms"] = 4100
    first.update(shot_id="U001A", end_state="Intermediate")
    last.update(shot_id="U001B", start_state="Intermediate", transition_from_previous="same_scene",
                generation_plan={"relation": "continuous", "confidence": "high", "reason": "Continue", "references": [{"shot_id": "U001A", "kind": "tail", "reason": "State"}]})
    timing = board.shots[1].timing.model_dump()
    timing["end_hold_ms"] = 500
    if problem == "accelerated_action":
        timing["beats"][0]["duration_ms"] = 100
    raw = {"replacements": [{"source_shot_id": "U001", "shots": [first, last]}],
           "timing_updates": [] if problem == "no_hold_budget" else [{"shot_id": "U002", "timing": timing}]}

    class Model:
        def generate(self, *args, **kwargs):
            return raw

    args = (Model(), "Story", chars, board, ["U001"], adaptive_config(), tmp_path, "schema", normalize_board)
    if problem:
        with pytest.raises(HarnessError):
            split_overloaded_shots(*args, protected_shot_ids=["U002"] if problem == "protected" else [])
        assert not (tmp_path / "storyboard.split.json").exists()
    else:
        result, report = split_overloaded_shots(*args)
        assert len(result.shots) == 4 and result.total_duration_ms == board.total_duration_ms
        assert result.shots[2].timing.beats == board.shots[1].timing.beats
        assert result.shots[2].action_description == board.shots[1].action_description
        assert result.shots[2].timing.end_hold_ms == 500
        assert report["duration_budget"]["difference_ms"] == 0
    assert read_json(tmp_path / "shot-splits.raw.json") == raw


def test_plausible_choreography_warning_does_not_trigger_endless_splitting(tmp_path):
    chars, board = small_board()
    calls = []

    class Reviewer:
        def generate(self, *args, **kwargs):
            calls.append(args)
            return {**PASS, "warnings": [{**FAIL["issues"][0], "category": "action_overload", "observed": "Feasible single transfer with several hand phases; inspect contact in generated video"}]}

    result, _, history = review_and_repair(Reviewer(), "Story", chars, board, adaptive_config(), tmp_path, "schema", normalize_board)
    assert len(calls) == 1 and result == board
    assert history[0]["verdict"]["warnings"]


@pytest.mark.parametrize("second_verdict", [PASS, FAIL])
def test_bounded_plan_repair_retains_original_and_requires_recheck(tmp_path, monkeypatch, second_verdict):
    monkeypatch.setenv("TEXT_LLM_API_KEY", "fixture-not-real")
    chars, board = small_board()
    original = board.model_dump()
    repaired = board.model_dump()
    repaired["shots"][0]["action_description"] = "Repaired single causal action"
    outputs = [original, FAIL, repaired, second_verdict]
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(outputs[len(calls) - 1])}}]})

    cfg = adaptive_config(quality=QualityConfig(enabled=True), text=TextConfig(endpoint="https://text.test/chat/completions", model="qwen-test", max_calls=4))
    charfile = tmp_path / "chars.json"
    write_json(charfile, chars.model_dump())
    root = tmp_path / "plan"
    if second_verdict["outcome"] == "pass":
        result = plan(EXAMPLE / "script.md", cfg, root, httpx.MockTransport(handle), characters=charfile)
        assert result["status"] == "planned"
        assert read_json(root / "storyboard.json")["shots"][0]["action_description"] == repaired["shots"][0]["action_description"]
        assert plan(EXAMPLE / "script.md", cfg, root, httpx.MockTransport(handle), characters=charfile)["requests_this_invocation"] == 0
    else:
        with pytest.raises(HarnessError, match="未通过"):
            plan(EXAMPLE / "script.md", cfg, root, httpx.MockTransport(handle), characters=charfile)
        assert not (root / "storyboard.json").exists()
    assert len(calls) == 4
    assert read_json(root / "storyboard.raw.json") == original
    assert read_json(root / "storyboard.repair-1.raw.json") == repaired
    assert len(read_json(root / "plan-quality.json")["history"]) == 2
    assert "candidate_storyboard" in calls[2]["messages"][1]["content"]


def test_contradictory_plan_pass_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXT_LLM_API_KEY", "fixture-not-real")
    chars, board = small_board()
    responses = iter([board.model_dump(), {**FAIL, "outcome": "pass"}])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(next(responses))}}]}))
    cfg = adaptive_config(quality=QualityConfig(enabled=True), text=TextConfig(endpoint="https://text.test/chat/completions", model="qwen-test"))
    write_json(tmp_path / "chars.json", chars.model_dump())
    with pytest.raises(HarnessError, match="格式无效"):
        plan(EXAMPLE / "script.md", cfg, tmp_path / "plan", transport, characters=tmp_path / "chars.json")
    assert not (tmp_path / "plan/storyboard.json").exists()
    assert (tmp_path / "plan/plan-review-0.raw.json").exists()


@pytest.mark.parametrize("corruption", [None, "end_state", "duplicate_id", "scene"])
def test_targeted_split_preserves_unrelated_content_and_reconnects_tail(tmp_path, corruption):
    chars, board = small_board()
    board.shots[0].timing.beats[0].duration_ms = 9000
    board, _ = normalize_board(board.model_dump(), chars, adaptive_config())
    before = board.model_dump()
    source = board.shots[0]
    first, last = source.model_dump(), source.model_dump()
    for shot in (first, last):
        shot["duration_ms"] = 5000
        shot["timing"]["beats"][0]["duration_ms"] = 4100
    first.update(shot_id="U001A", end_state="Intermediate physical state")
    last.update(shot_id="U001B", start_state="Intermediate physical state", transition_from_previous="same_scene",
                generation_plan={"relation": "continuous", "confidence": "high", "reason": "Continue operation", "references": [{"shot_id": "U001A", "kind": "tail", "reason": "Actual current state"}]})
    if corruption == "end_state":
        last["end_state"] = "Invented ending"
    elif corruption == "duplicate_id":
        last["shot_id"] = "U002"
    elif corruption == "scene":
        last["scene_id"] = "OTHER_ROOM"

    class FixtureAdapter:
        def generate(self, *args, **kwargs):
            return {"replacements": [{"source_shot_id": "U001", "shots": [first, last]}]}

    args = (FixtureAdapter(), "Original script", chars, board, ["U001"], adaptive_config(), tmp_path, "schema", normalize_board)
    if corruption:
        with pytest.raises(HarnessError):
            split_overloaded_shots(*args)
        assert not (tmp_path / "storyboard.split.json").exists()
    else:
        result, _ = split_overloaded_shots(*args)
        assert [s.shot_id for s in result.shots] == ["U001A", "U001B", "U002", "U003"]
        assert result.shots[2].action_description == board.shots[1].action_description
        assert result.shots[2].generation_plan.references[0].shot_id == "U001B"
        assert result.shots[2].timeline_in_ms == result.shots[0].duration_ms + result.shots[1].duration_ms
    assert board.model_dump() == before
    assert (tmp_path / "shot-splits.raw.json").exists()


@pytest.mark.parametrize("corruption", [None, "unrequested_state", "new_scene", "unjustified_prop"])
def test_scoped_clarification_does_not_rewrite_physical_action(tmp_path, corruption):
    chars, board = small_board()
    original = board.model_dump()
    fields = {"continuity_notes": ["An explicit omitted repeated operation during the cut"], "transition_from_previous": "time_jump", "focus_prop_ids": [*board.shots[1].focus_prop_ids, "PROP_TOOL"]}
    prop = {"prop_id": "PROP_TOOL", "name": "existing action tool", "appearance": "metal", "initial_state": "beside the bowl", "basis": "Already used by the existing action"}
    if corruption == "unrequested_state":
        fields["end_state"] = "Different plot ending"
    elif corruption == "new_scene":
        fields["transition_from_previous"] = "scene_change"
    elif corruption == "unjustified_prop":
        prop["basis"] = ""

    class FixtureAdapter:
        def generate(self, *args, **kwargs):
            return {"updates": [{"shot_id": "U002", "fields": fields}], "new_props": [prop]}

    args = (FixtureAdapter(), board, {"U002": ["continuity_notes", "transition_from_previous", "focus_prop_ids"]}, {}, adaptive_config(), tmp_path, normalize_board, chars)
    if corruption:
        with pytest.raises(HarnessError):
            clarify_plan_fields(*args)
        assert not (tmp_path / "storyboard.clarified.json").exists()
    else:
        result, _ = clarify_plan_fields(*args)
        assert result.shots[1].action_description == board.shots[1].action_description
        assert result.shots[1].start_state == board.shots[1].start_state
        assert result.shots[1].end_state == board.shots[1].end_state
        assert result.shots[0] == board.shots[0].model_copy(update={"first_frame_reference_policy": "planned_frame"})
        assert result.shots[2] == board.shots[2].model_copy(update={"first_frame_reference_policy": "planned_frame"})
        assert result.props[-1].prop_id == "PROP_TOOL"
    assert board.model_dump() == original


@pytest.mark.parametrize("problem", [None, "over_budget", "unrequested_camera"])
def test_scoped_timing_camera_correction_preserves_story_and_budget(tmp_path, problem):
    chars, board = small_board()
    original = board.model_dump()
    timing = board.shots[0].timing.model_dump()
    timing["beats"][0]["duration_ms"] = 9000 if problem == "over_budget" else 5100
    camera = board.shots[0].camera.model_dump()
    camera["movement"] = "固定"
    raw = {"updates": [{"shot_id": "U001", "fields": {"timing": timing, "camera": camera}}], "new_props": []}

    class Model:
        def generate(self, *args, **kwargs):
            return raw

    cfg = adaptive_config(target_duration_ms=15000)
    cfg.duration_planning.target_tolerance_ms = 1000
    allowed = {"U001": ["timing"] if problem == "unrequested_camera" else ["timing", "camera"]}
    args = (Model(), board, allowed, {}, cfg, tmp_path, normalize_board, chars)
    if problem:
        with pytest.raises(HarnessError):
            clarify_plan_fields(*args)
        assert not (tmp_path / "storyboard.clarified.json").exists()
    else:
        result, report = clarify_plan_fields(*args)
        assert result.total_duration_ms == 16000
        assert result.shots[0].action_description == board.shots[0].action_description
        assert result.shots[0].start_state == board.shots[0].start_state
        assert result.shots[0].end_state == board.shots[0].end_state
        assert result.shots[0].timing.model_dump() == timing
        assert result.shots[1].timeline_in_ms == 6000
        assert report["duration_budget"]["difference_ms"] == 1000
    assert board.model_dump() == original
    assert read_json(tmp_path / "plan-clarifications.raw.json") == raw


def test_scoped_format_repair_only_replaces_invalid_or_missing_model_entries(tmp_path):
    chars, board = small_board()
    allowed = {"U001": ["continuity_notes"], "U002": ["transition_from_previous", "continuity_notes"], "U003": ["continuity_notes"]}
    first = {"updates": [{"shot_id": "U001", "fields": {"continuity_notes": ["Model's valid original note"]}},
                         {"shot_id": "U002", "fields": {"transition_from_previous": "cut"}}]}
    correction = {"updates": [{"shot_id": "U002", "fields": {"transition_from_previous": "time_jump", "continuity_notes": ["Model explains omitted repetitive action before the frame"]}},
                              {"shot_id": "U003", "fields": {"continuity_notes": ["Model's previously missing note"]}}]}
    responses, calls = iter([first, correction]), []

    class Model:
        def generate(self, instructions, payload, **kwargs):
            calls.append(json.loads(payload))
            return next(responses)

    result, _ = clarify_plan_fields(Model(), board, allowed, {}, adaptive_config(), tmp_path, normalize_board, chars)
    assert len(calls) == 2 and set(calls[1]["allowed_updates"]) == {"U002", "U003"}
    assert result.shots[0].continuity_notes == first["updates"][0]["fields"]["continuity_notes"]
    assert result.shots[1].transition_from_previous == "time_jump"
    assert read_json(tmp_path / "plan-clarifications.raw.json") == first
    assert read_json(tmp_path / "plan-clarifications.format-repair.raw.json") == correction
    assert read_json(tmp_path / "clarification-format-repair.json")["model_text_edited"] is False


def test_scope_format_repair_does_not_apply_unrequested_neighbor_edits(tmp_path):
    chars, board = small_board()
    first = {"updates": [{"shot_id": "U002", "fields": {"continuity_notes": ["Requested model note"]}},
                         {"shot_id": "U001", "fields": {"action_description": "Unrequested harmful change"}}]}
    correction = {"updates": [{"shot_id": "U002", "fields": {"continuity_notes": ["Requested model note"]}}]}
    responses = iter([first, correction])

    class Model:
        def generate(self, *args, **kwargs):
            return next(responses)

    result, _ = clarify_plan_fields(Model(), board, {"U002": ["continuity_notes"]}, {}, adaptive_config(), tmp_path, normalize_board, chars)
    assert result.shots[0].action_description == board.shots[0].action_description
    assert result.shots[1].continuity_notes == ["Requested model note"]
    assert read_json(tmp_path / "plan-clarifications.raw.json") == first
    assert read_json(tmp_path / "plan-clarifications.effective.json")["updates"] == correction["updates"]


@pytest.mark.parametrize("corruption", [None, "reference_kind", "relation", "removed_plan"])
def test_auxiliary_reason_cleanup_cannot_change_generation_dependencies(tmp_path, corruption):
    chars, board = small_board()
    original = board.model_dump()
    plan = board.shots[1].generation_plan.model_dump()
    plan["reason"] = "Corrected explanation of the existing causal action"
    plan["references"][0]["reason"] = "Existing actual tail supplies unchanged physical state"
    if corruption == "reference_kind":
        plan["references"][0]["kind"] = "frame"
    elif corruption == "relation":
        plan["relation"] = "independent"
    elif corruption == "removed_plan":
        plan = None
    raw = {"updates": [{"shot_id": "U002", "fields": {"generation_plan": plan}}]}

    class Model:
        def generate(self, *args, **kwargs):
            return raw

    args = (Model(), board, {"U002": ["generation_plan"]}, {}, adaptive_config(), tmp_path, normalize_board, chars)
    if corruption:
        with pytest.raises(HarnessError):
            clarify_plan_fields(*args)
        assert not (tmp_path / "storyboard.clarified.json").exists()
    else:
        result, _ = clarify_plan_fields(*args)
        assert result.shots[1].generation_plan.model_dump() == plan
        assert result.shots[1].start_state == board.shots[1].start_state
        assert result.shots[1].action_description == board.shots[1].action_description
        assert result.shots[1].timing == board.shots[1].timing
    assert board.model_dump() == original
    assert read_json(tmp_path / "plan-clarifications.raw.json") == raw
