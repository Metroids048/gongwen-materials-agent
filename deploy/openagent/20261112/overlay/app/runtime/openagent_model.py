"""Structured transport through the current OpenAgent managed model route."""

import json
import os
import re
import socket
from contextvars import ContextVar
from time import monotonic, time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import ValidationError

from app.revision.document import RevisionIssue


stage_log = ContextVar("managed_model_stage_log", default=None)
_LOW_EFFORT_EXCHANGES = {"Candidate", "ExpertBrief", "BoundConferenceReview", "BoundConferenceRepair", "VerifiedConferenceDraft"}


def _record_stage(event, metadata):
    path = stage_log.get()
    if path is None:
        return
    keys = ("model", "interface", "response_schema", "upstream_call_id",
            "reasoning_effort", "elapsed_seconds", "http_status", "prompt_bytes", "schema_bytes", "response_headers_seconds")
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps({"event": event, "unix_seconds": time(), **{k: metadata[k] for k in keys if k in metadata}}) + "\n")


def _safe_identifier(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        return value
    return None


def ask_platform(prompt, response_type, *, timeout, reasoning_effort):
    names = ("OPENAGENT_CHAT_BASE_URL", "OPENAGENT_CHAT_MODEL",
             "OPENAGENT_CHAT_INTERFACE_TYPE", "OPENAGENT_MODEL_API_KEY")
    if not all(os.environ.get(name) for name in names):
        raise RevisionIssue("MODEL_UNAVAILABLE", "平台托管模型环境不完整。")
    base, model, interface, credential = (os.environ[name] for name in names)
    # The first draft shares the managed synchronous request window. Keep the
    # original input, output schema and independent review stages intact.
    if model == "deepseek-v4-pro" and reasoning_effort == "none" and response_type.__name__ == "Candidate":
        max_tokens = 4096
    else:
        max_tokens = 16384
    if (model == "deepseek-v4-pro" and response_type.__name__ in _LOW_EFFORT_EXCHANGES
            and reasoning_effort != "none"):
        # Independent review and the issue-specific exchanges retain reasoning.
        reasoning_effort = "low"
    elif response_type.__name__ == "Candidate" and reasoning_effort == "high":
        reasoning_effort = "medium"
    schema = response_type.model_json_schema()
    instruction = ("请按请求指定的 JSON Schema 返回一个 JSON 对象，不使用 Markdown，不添加解释。"
                   "保留所有材料出处、待核和修改范围限制。")
    if interface in {"openai-completions", "chat-completions"}:
        endpoint = base.rstrip("/") + "/v1/chat/completions"
        body = {"model": model, "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": response_type.__name__, "strict": True, "schema": schema}},
            "reasoning_effort": reasoning_effort, "max_tokens": max_tokens}
    elif interface in {"openai-responses", "responses"}:
        endpoint = base.rstrip("/") + "/v1/responses"
        body = {"model": model, "instructions": instruction, "input": prompt,
                "text": {"format": {"type": "json_schema", "name": response_type.__name__,
                                    "strict": True, "schema": schema}},
                "reasoning": {"effort": reasoning_effort}, "max_output_tokens": max_tokens}
    else:
        raise RevisionIssue("MODEL_INTERFACE_GUIDE_UNAVAILABLE", "平台未提供此模型接口的调用规范。")
    call_id = "gongwen-" + uuid4().hex
    request = Request(endpoint, data=json.dumps(body, ensure_ascii=False).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + credential,
        "X-OASN-Upstream-Call-Id": call_id}, method="POST")
    metadata = {"model": model, "transport": "openagent-managed", "interface": interface,
                "response_schema": response_type.__name__, "upstream_call_id": call_id,
                "reasoning_effort": reasoning_effort, "timeout_seconds": timeout,
                "prompt_bytes": len(prompt.encode()), "schema_bytes": len(json.dumps(schema, ensure_ascii=False).encode())}
    _record_stage("started", metadata)
    started = monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            metadata["response_headers_seconds"] = round(monotonic() - started, 1)
            payload = json.load(response)
    except HTTPError as error:
        metadata["response_headers_seconds"] = round(monotonic() - started, 1)
        # Keep the gateway identifiers separately from the project retry decision.
        gateway_code = None
        try:
            with error:
                value = json.loads(error.read(16384))
            upstream = value.get("error", value) if isinstance(value, dict) else None
            if isinstance(upstream, dict):
                gateway_code = _safe_identifier(upstream.get("code"))
        except (OSError, ValueError):
            pass
        gateway_id = _safe_identifier(error.headers.get("X-Request-Id")) if error.headers else None
        code = "JOB_OUTCOME_UNKNOWN" if error.code >= 500 else gateway_code or "MODEL_FAILED"
        issue = RevisionIssue(code, f"托管模型 HTTP {error.code}；调用编号 {call_id}，请先核对原调用。")
        metadata.update(http_status=error.code, gateway_error_code=gateway_code, gateway_request_id=gateway_id)
        issue.model_failure = metadata
        raise issue from None
    except (TimeoutError, socket.timeout, URLError):
        reason = "托管模型连接中断" if timeout is None else f"托管模型连接中断或超过 {timeout} 秒"
        issue = RevisionIssue("JOB_OUTCOME_UNKNOWN",
                              f"{reason}；调用编号 {call_id}，未自动重试。")
        issue.model_failure = metadata
        raise issue from None
    finally:
        metadata["elapsed_seconds"] = round(monotonic() - started, 1)
        _record_stage("request_ended", metadata)
    metadata["usage"] = payload.get("usage", {})
    content = None
    try:
        if interface in {"openai-completions", "chat-completions"}:
            choice = payload["choices"][0]
            if choice.get("finish_reason") == "length":
                raise RevisionIssue("MODEL_INVALID", f"托管模型结果达到长度上限；调用编号 {call_id}。")
            content = choice["message"]["content"]
        else:
            content = payload.get("output_text") or "".join(
                part["text"] for item in payload.get("output", [])
                for part in item.get("content", []) if part.get("type") == "output_text")
        result = response_type.model_validate_json(content)
    except (KeyError, IndexError, TypeError, ValueError, ValidationError) as error:
        issue = RevisionIssue("MODEL_INVALID", f"托管模型结果不符合原交换格式；调用编号 {call_id}。")
        # Private stage receipts retain rejected output, never request headers or credentials.
        issue.rejected_exchange = {"content": content, "model": metadata,
                                   "validation_errors": [{"loc": list(e["loc"]), "type": e["type"]}
                                                         for e in error.errors()] if isinstance(error, ValidationError) else []}
        raise issue from None
    return result, metadata
