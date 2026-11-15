import pytest

from video_harness.camera import is_locked_camera, review_fixed_camera
from video_harness.util import HarnessError


@pytest.mark.parametrize("movement,locked", [("固定", True), ("Locked-off camera.", True), ("固定后跟随人物", False), ("轻微手持晃动", False), ("缓慢右移", False)])
def test_locked_camera_does_not_override_declared_movement(movement, locked):
    assert is_locked_camera(movement) is locked


def test_camera_evidence_has_no_story_and_malformed_verdict_blocks(tmp_path):
    class Reviewer:
        def generate_visual(self, data, images, **kwargs):
            assert data == {"stage": "locked_camera_endpoints", "camera_motion_allowed": False}
            assert [label for label, path in images] == ["FIRST FRAME", "TARGET ENDING FRAME"]
            return {"outcome": "pass"}, tmp_path, "fixture"
    with pytest.raises(HarnessError, match="固定机位"):
        review_fixed_camera(Reviewer(), "first.png", "target.png")
    assert (tmp_path / "camera-verdict.raw.json").is_file()
