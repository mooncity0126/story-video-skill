"""Explicit visual review decisions bound to an immutable video candidate."""
from pathlib import Path
import uuid

from .util import HarnessError, digest, file_hash, inside, now, read_json, write_json


def decision_pointer(root, shot_id):
    return Path(root) / "operator_reviews" / digest(shot_id) / "latest.json"


def save_decision(root, shot_id, attempt, *, decision, notes, reviewer):
    if decision not in {"approve", "reject"} or not notes.strip() or not reviewer.strip():
        raise HarnessError("视频复核必须明确批准/拒绝，并记录检查人和实际检查依据")
    asset = attempt["output"]
    if asset["kind"] != "video" or file_hash(inside(root, asset["path"])) != asset["sha256"]:
        raise HarnessError("复核视频文件缺失或已改变")
    pointer = decision_pointer(root, shot_id)
    path = pointer.parent / (uuid.uuid4().hex + ".json")
    record = {"shot_id": shot_id, "video_attempt_id": attempt["attempt_id"], "video_sha256": asset["sha256"],
              "decision": decision, "notes": notes, "reviewer": reviewer, "created_at": now()}
    write_json(path, record)
    write_json(pointer, {"record_path": path.relative_to(root).as_posix(), "sha256": file_hash(path)})
    return record


def load_decision(root, shot_id, attempt):
    pointer = decision_pointer(root, shot_id)
    if not pointer.is_file():
        raise HarnessError(f"{shot_id}: 等待人工/代理复核原视频；使用 review-video 记录检查依据后 resume")
    reference = read_json(pointer)
    path = inside(root, reference["record_path"])
    if not path.is_file() or file_hash(path) != reference["sha256"]:
        raise HarnessError("视频复核记录已改变")
    record = read_json(path)
    if record["shot_id"] != shot_id or record["video_attempt_id"] != attempt["attempt_id"] or record["video_sha256"] != attempt["output"]["sha256"]:
        raise HarnessError(f"{shot_id}: 原复核针对旧候选，当前视频需要重新检查")
    if record["decision"] not in {"approve", "reject"} or not record["notes"].strip() or not record["reviewer"].strip():
        raise HarnessError("视频复核结论不完整")
    return record, reference["sha256"]
