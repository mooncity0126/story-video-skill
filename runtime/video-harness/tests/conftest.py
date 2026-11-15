from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from video_harness.client import X2VClient
from video_harness.engine import Runner, create_run
from video_harness.models import Config, ModelChoice
from video_harness.util import read_json

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE = Path(__file__).parents[1] / "examples" / "two-cups"


class MockCloud:
    def __init__(self):
        self.lock = threading.Lock()
        self.submissions, self.queries, self.quote_bodies, self.download_headers = [], [], [], []
        self.tasks = {}
        self.unknown_once = False
        self.expired_once = False
        self.query_failure_once = False
        self.running = False
        self.delay_first_image = False
        self.completed = []
        self.result_requests = 0
        image = io.BytesIO()
        Image.new("RGB", (108, 192), "teal").save(image, format="PNG")
        self.image = image.getvalue()

    def image_for_task(self, task_id):
        number = int(task_id.rsplit("-", 1)[-1]) if task_id.rsplit("-", 1)[-1].isdigit() else 0
        data = io.BytesIO()
        Image.new("RGB", (108, 192), (number % 256, (number * 31) % 256, 120)).save(data, format="PNG")
        return data.getvalue()

    def handle(self, request):
        path = request.url.path
        if request.url.host == "media.test":
            self.download_headers.append(dict(request.headers))
            if self.expired_once:
                self.expired_once = False
                return httpx.Response(403)
            task = self.tasks[path.strip("/")]
            self.completed.append(path.strip("/"))
            return httpx.Response(200, content=b"MOCK_VIDEO_ONLY_FOR_TEST" if task["task"] in {"i2v", "i2av"} else self.image_for_task(path.strip("/")))
        if path == "/openapi.json":
            return httpx.Response(200, json=read_json(FIXTURES / "openapi.json"))
        if path.endswith("/model/list"):
            return httpx.Response(200, json=read_json(FIXTURES / "models.json"))
        if path.endswith("/model/specs"):
            data = read_json(FIXTURES / (request.url.params["task"] + ".json"))
            return httpx.Response(200, json={"specs": [s for s in data["specs"] if s["model_cls"] == request.url.params["model_cls"]]})
        if path.endswith("/billing/quote"):
            body = json.loads(request.content)
            assert not any(k.startswith("input_") and k != "input_meta" for k in body)
            self.quote_bodies.append(body)
            return httpx.Response(200, json={"quoted_credits": 2})
        if path.endswith("/task/submit"):
            body = json.loads(request.content)
            with self.lock:
                self.submissions.append(body)
                tid = f"task-{len(self.submissions)}"
                self.tasks[tid] = body
            if self.unknown_once:
                self.unknown_once = False
                raise httpx.ReadTimeout("simulated lost submit response", request=request)
            return httpx.Response(200, json={"task_id": tid, "quoted_credits": 2})
        if path.endswith("/task/query"):
            tid = request.url.params["task_id"]
            self.queries.append(tid)
            if self.query_failure_once:
                self.query_failure_once = False
                return httpx.Response(503)
            if self.delay_first_image and tid == "task-1":
                time.sleep(0.04)
            return httpx.Response(200, json={"task_id": tid, "model_cls": self.tasks[tid]["model_cls"], "status": "RUNNING" if self.running else "SUCCEED"})
        if path.endswith("/task/result_url"):
            self.result_requests += 1
            return httpx.Response(200, json={"url": "https://media.test/" + request.url.params["task_id"] + "?signature=temporary-secret"})
        raise AssertionError(f"unexpected API route {path}")


@pytest.fixture
def cloud():
    return MockCloud()


@pytest.fixture
def config():
    # Regression suite for existing independent runs. Serial behaviour has its own
    # tests with real tail extraction, rather than mock bytes posing as video.
    return Config(
        execute_enabled=True,
        budget_credits=1000,
        concurrency=4,
        request_interval_seconds=0,
        poll_interval_seconds=0.001,
        wait_timeout_seconds=0.01,
        retries=0,
        sequential_shots=False,
        require_object_states=False,
        image_fallback=None,
        edit_fallback=None,
        image=ModelChoice(task="t2i", model_cls="gpt-image-2.5-flare", parameters={"resolution_level": "1080p"}),
        edit=ModelChoice(task="i2i", model_cls="gpt-image-2.5-flare", parameters={"resolution_level": "1080p"}),
        video=ModelChoice(task="i2av", model_cls="LTX-2.3", parameters={"resolution_level": "720p"}),
    )


@pytest.fixture
def client(config, cloud):
    result = X2VClient(config, key="test-only-credential", transport=httpx.MockTransport(cloud.handle), sleep=lambda _: None)
    yield result
    result.close()


@pytest.fixture
def run_dir(tmp_path, config):
    return create_run(EXAMPLE / "script.md", EXAMPLE / "characters.json", EXAMPLE / "storyboard.json", config, tmp_path)


@pytest.fixture(autouse=True)
def mocked_video_metadata(monkeypatch):
    import video_harness.media as media

    original = media.probe

    def test_probe(path, cfg, kind):
        if kind == "video" and Path(path).read_bytes() == b"MOCK_VIDEO_ONLY_FOR_TEST":
            return {"width": 108, "height": 192, "duration_ms": 5000, "fps_num": 24, "fps_den": 1, "has_audio": True, "verification": "mocked_for_test"}
        return original(path, cfg, kind)

    monkeypatch.setattr(media, "probe", test_probe)


@pytest.fixture
def runner(run_dir, client):
    result = Runner(run_dir, client)
    yield result
    result.close()
