"""Shared structured Codex CLI transport."""

import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from pydantic import ValidationError

from app.revision.document import RevisionIssue


def _timeout_stage(output, has_result):
    if has_result:
        return "结构化结果已写出，CLI 未正常退出"
    events = set()
    for line in (output or b"").splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            events.add(event["type"])
    if "turn.completed" in events:
        return "模型回合已完成，结构化结果尚未写出"
    if "turn.started" in events:
        return "模型回合已启动，尚无完成事件"
    if "thread.started" in events:
        return "CLI 会话已启动，尚无模型回合事件"
    return "尚无 CLI 会话或模型回合事件"


def ask_codex(model, prompt, response_type, *, timeout=None, reasoning_effort="high", disable_plugins=False):
    codex = shutil.which("codex")
    if not codex:
        raise RevisionIssue("MODEL_UNAVAILABLE", "服务无法找到 Codex CLI，请检查启动终端的 PATH。")
    with TemporaryDirectory(prefix="gongwen-codex-") as directory:
        work = Path(directory)
        schema = work / "selection.schema.json"
        schema.write_text(json.dumps(response_type.model_json_schema()), encoding="utf-8")
        response = work / "selection.json"
        command = [
            codex, "-a", "never", "-m", model, "-c", f'model_reasoning_effort="{reasoning_effort}"',
            "-c", "project_doc_max_bytes=0", "exec", "--ignore-user-config", "--ephemeral",
            "--skip-git-repo-check", "-s", "read-only", "-C", str(work), "--json",
            "--output-schema", str(schema), "-o", str(response), "-",
        ]
        if disable_plugins:
            command[1:1] = ["--disable", "plugins"]
        started = monotonic()
        try:
            completed = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            stage = _timeout_stage(error.output, response.is_file())
            raise RevisionIssue("JOB_OUTCOME_UNKNOWN", f"Codex 达到 {timeout} 秒上限（{stage}），请先核查本轮结果。") from error
        elapsed = round(monotonic() - started, 1)
        if completed.returncode or not response.is_file():
            raise RevisionIssue("MODEL_FAILED", "Codex 未返回可用结果。请检查本机登录、模型与推理线路。")
        try:
            result = response_type.model_validate_json(response.read_bytes())
            events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
        except (ValueError, ValidationError) as error:
            raise RevisionIssue("MODEL_INVALID", "Codex 返回的结果不符合交换格式。") from error
    metadata = {"model": model, "reasoning_effort": reasoning_effort,
                "elapsed_seconds": elapsed, "timeout_seconds": timeout}
    for event in events:
        if event.get("type") == "thread.started":
            metadata["thread_id"] = event.get("thread_id")
        if event.get("type") == "turn.completed":
            metadata["usage"] = event.get("usage")
    return result, metadata
