from __future__ import annotations

import base64
import email.utils
import os
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .util import HarnessError

BASE_URL = "https://x2v.light-ai.top"
REMOTE_STATES = {"CREATED", "PENDING", "RUNNING", "SUCCEED", "FAILED", "CANCEL"}


class APIError(HarnessError):
    def __init__(self, category, status=None, retry_after=None):
        self.category, self.status, self.retry_after = category, status, retry_after
        super().__init__(f"X2V {category}" + (f" (HTTP {status})" if status else ""))


class SubmissionUnknown(HarnessError):
    pass


def retry_after(value):
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            dt = email.utils.parsedate_to_datetime(value)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def classify(response):
    status = response.status_code
    category = {401: "authentication", 403: "permission", 402: "balance", 404: "not_found", 422: "parameters", 429: "rate_limit"}.get(status, "server" if status >= 500 else "parameters")
    try:
        body = response.json()
        text = str(body.get("code", "")) + " " + str(body.get("message", body.get("error", "")))
        if status < 500 and any(t in text.lower() for t in ["insufficient_credit", "insufficient credit", "insufficient balance", "not enough credit", "余额不足", "积分不足"]):
            category = "balance"
        elif any(t in text.lower() for t in ["content", "safety", "moderation", "内容审核", "敏感内容", "prompt不合规"]):
            category = "content_policy"
    except (ValueError, AttributeError):
        pass
    return APIError(category, status, retry_after(response.headers.get("Retry-After")))


class X2VClient:
    """Only the official host receives X2V credentials. Downloads use a separate client."""

    def __init__(self, config, *, key=None, transport=None, sleep=time.sleep):
        self.config = config
        self.key = key if key is not None else os.environ.get("X2V_API_KEY", "")
        self.http = httpx.Client(base_url=BASE_URL, timeout=config.request_timeout_seconds, transport=transport, follow_redirects=False)
        self.download_http = httpx.Client(timeout=config.request_timeout_seconds, transport=transport, follow_redirects=True)
        self.sleep = sleep
        self._rate_lock = threading.Lock()
        self._next_request = 0.0

    def close(self):
        self.http.close()
        self.download_http.close()

    def _pace(self):
        with self._rate_lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                self.sleep(delay)
            self._next_request = time.monotonic() + self.config.request_interval_seconds

    def _backoff(self, attempt, header_delay=0):
        delay = max(header_delay or 0, min(2**attempt, 30) + random.uniform(0, 0.4))
        # A long Retry-After is reported to the caller instead of blocking a process indefinitely.
        if (header_delay or 0) > self.config.wait_timeout_seconds:
            raise APIError("rate_limit_retry_later", 429, delay)
        self.sleep(delay)

    def request(self, method, path, *, auth=True, params=None, body=None, submit=False):
        if auth and not self.key:
            raise HarnessError("缺少 X2V_API_KEY 环境变量")
        headers = {"Authorization": f"Bearer {self.key}"} if auth else {}
        for attempt in range(self.config.retries + 1):
            self._pace()
            try:
                response = self.http.request(method, path, params=params, json=body, headers=headers)
            except httpx.TransportError:
                if submit:
                    raise SubmissionUnknown("submission_unknown: 提交未返回 task_id，禁止自动重提；请核实云端历史") from None
                if attempt == self.config.retries:
                    raise APIError("network") from None
                self._backoff(attempt)
                continue
            if response.is_error:
                error = classify(response)
                if submit and response.status_code >= 500:
                    raise SubmissionUnknown("submission_unknown: 服务端提交结果不明确，请核实云端历史") from None
                if (response.status_code == 429 or (not submit and response.status_code >= 500)) and attempt < self.config.retries:
                    self._backoff(attempt, error.retry_after)
                    continue
                raise error
            if not response.is_success:
                if submit:
                    raise SubmissionUnknown("submission_unknown: 非预期提交响应，请核实云端历史")
                raise APIError("unexpected_response", response.status_code)
            try:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError()
            except ValueError:
                if submit:
                    raise SubmissionUnknown("submission_unknown: 提交响应无法解析，请核实云端历史") from None
                raise APIError("malformed_response") from None
            return result
        raise APIError("retry_exhausted")

    def models(self):
        return self.request("GET", "/api/v1/model/list", auth=False)

    def openapi(self):
        return self.request("GET", "/openapi.json", auth=False)

    def specs(self, choice):
        return self.request("GET", "/api/v1/model/specs", auth=False, params={"task": choice.task, "model_cls": choice.model_cls, "stage": choice.stage})["specs"]

    def quote(self, body):
        result = self.request("POST", "/api/v1/billing/quote", body=body)
        value = result.get("quoted_credits")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise APIError("malformed_quote")
        return value

    def submit(self, body):
        result = self.request("POST", "/api/v1/task/submit", body=body, submit=True)
        if not isinstance(result.get("task_id"), str) or not result["task_id"]:
            raise SubmissionUnknown("submission_unknown: 未收到有效 task_id，请核实云端历史")
        return result

    def query(self, task_id):
        result = self.request("GET", "/api/v1/task/query", params={"task_id": task_id})
        if result.get("status") not in REMOTE_STATES:
            raise APIError("unknown_remote_state")
        return result

    def result_url(self, task_id, output_name):
        result = self.request("GET", "/api/v1/task/result_url", params={"task_id": task_id, "name": output_name})
        url = result.get("url", "")
        if urlparse(url).scheme not in {"http", "https"}:
            raise APIError("invalid_result_url")
        return url

    def download_url(self, url, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".part")
        try:
            with self.download_http.stream("GET", url) as response:
                if not response.is_success:
                    raise classify(response)
                count = 0
                with temp.open("wb") as f:
                    for chunk in response.iter_bytes(1024 * 1024):
                        count += len(chunk)
                        if count > self.config.max_media_bytes:
                            raise HarnessError("媒体超过 max_media_bytes 限制")
                        f.write(chunk)
                if count == 0:
                    raise HarnessError("下载到空文件")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def download_result(self, task_id, output_name, path):
        for attempt in range(self.config.retries + 1):
            try:
                # Always obtain a fresh URL; never persist a signed download address.
                self.download_url(self.result_url(task_id, output_name), path)
                return
            except (APIError, httpx.TransportError) as exc:
                if isinstance(exc, APIError) and exc.status not in {401, 403, 404, 410, 429, 500, 502, 503, 504}:
                    raise
                if attempt == self.config.retries:
                    raise HarnessError("结果下载失败，可 resume 刷新地址并继续下载") from None
                self._backoff(attempt, getattr(exc, "retry_after", 0))


def media_input(path, max_bytes):
    value = str(path)
    if value.startswith(("https://", "http://")):
        return {"type": "url", "data": value}
    file = Path(path)
    if file.stat().st_size > max_bytes:
        raise HarnessError("本地媒体超过 max_media_bytes")
    return {"type": "base64", "data": base64.b64encode(file.read_bytes()).decode("ascii")}
