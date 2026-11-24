"""Run against a live local service. Creates only synthetic test records."""

import json
import sys
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from uuid import uuid4


def session():
    return build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))


def run(base):
    def call(client, path, expected, body=None):
        request = Request(base + path)
        if body is not None:
            request.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        try:
            response = client.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            assert response.code == expected, (path, response.code, expected, raw)
            return json.loads(raw) if response.headers.get_content_type() == "application/json" else raw

    def action(client, path, expected, body=None, code=None):
        result = call(client, path, expected, body)
        assert {"status", "summary", "task_id", "request_id", "next_actions", "artifacts"} <= result.keys()
        assert result["next_actions"] == result["artifacts"] == []
        if code:
            assert result["status"] == "error" and result["error"]["code"] == code, result
            assert result["error"]["impact"] and result["error"]["recovery_action"]
        else:
            assert result["status"] == "success", result
        return result

    client, other, anonymous = session(), session(), session()
    assert call(client, "/healthz", 200) == call(client, "/api/health", 200) == {
        "status": "ok", "storage": "available",
    }
    caps = call(client, "/api/capabilities", 200)
    assert caps["contract_version"] == "0.3"
    actions = {"register_task", "query_status"}
    public_revision = caps["public_revision_profile"]
    if public_revision:
        assert public_revision == "plan-revise"
        actions |= {"generate_candidate", "commit_candidate", "apply_user_edit", "export_revision"}
    assert set(caps["actions"]) == actions
    assert all(caps[k] is bool(public_revision) for k in ("attachments", "model", "export"))
    profiles = ["experience-draft", "experience-revise", "plan-draft", "plan-revise"]
    assert sorted(p["id"] for p in caps["profiles"]) == profiles
    call(client, "/", 200)
    call(other, "/", 200)

    tasks = []
    for profile in profiles:
        body = {
            "request_id": uuid4().hex, "profile_id": profile, "inputs": [],
            "original_request": "  合成接口验证：补一条 <事项>。\n原来的责任分工不动。  ",
        }
        registered = action(client, "/api/tasks", 201, body)
        task_id = registered["task_id"]
        assert registered["request_id"] == body["request_id"]
        assert registered["data"]["source_ids"] == []
        assert registered["data"]["task_state"] == "received"
        path = f"/api/tasks/{task_id}"
        query = action(client, path, 200)["data"]
        assert query["existing_result"] == registered
        assert query["job_state"] is None
        assert query["target_request_or_job_id"] == body["request_id"]
        assert query["task"] == {
            "task_id": task_id, "profile_id": profile, "original_request": body["original_request"],
            "task_state": "received", "quality_state": "unreviewed", "requirements": [],
            "current_revision": None, "source_set_version": 0, "basis_key": None,
        }, query
        assert action(client, "/api/tasks", 200, body) == registered
        action(client, "/api/tasks", 409, {**body, "original_request": "另一个需求"}, "REQUEST_CONFLICT")
        assert action(client, path, 200)["data"] == query
        action(other, path, 404, code="TASK_NOT_FOUND")
        tasks.append(task_id)

    for change, code in [
        ({"profile_id": "invalid"}, "INVALID_PROFILE"),
        ({"original_request": "  \n\t"}, "EMPTY_REQUEST"),
        ({"inputs": [{"name": "原稿.docx"}]}, "INPUTS_NOT_SUPPORTED"),
        ({"inputs": None}, "INVALID_REQUEST"),
        ({"owner_id": "caller-supplied"}, "INVALID_REQUEST"),
    ]:
        error = action(client, "/api/tasks", 422, {**body, **change}, code)
        assert error["task_id"] is None
    action(client, "/api/tasks", 422, {k: v for k, v in body.items() if k != "inputs"}, "INVALID_REQUEST")
    action(anonymous, "/api/tasks", 401, body, "SESSION_REQUIRED")
    action(anonymous, f"/api/tasks/{tasks[0]}", 404, code="TASK_NOT_FOUND")
    action(client, f"/api/tasks/{uuid4().hex}", 404, code="TASK_NOT_FOUND")
    call(client, "/data/gongwen.sqlite3", 404)
    print(f"PASS live HTTP {base}: health, capabilities, four profiles, exact text, idempotency/conflict, "
          "validation, read-only recovery, session isolation and no public database path.")
    for profile, task_id in zip(profiles, tasks):
        print(f"{profile}: {task_id}")


if __name__ == "__main__":
    run(sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8765")
