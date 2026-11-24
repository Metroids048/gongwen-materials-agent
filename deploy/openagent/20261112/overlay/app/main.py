"""Local web entry and HTTP adapters for the implemented workflow actions."""

import base64
import binascii
import errno
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app import agent
from app.revision.document import (
    TEMPLATE_VERSION, RevisionIssue, build_word, open_docx, prepare_revision, read_sources, table_rows, word_bytes,
)
from app.revision.model import pick_date, read_rules
from app.revision import formal, native_review, opinion_template
from app.revision.opinions import review_sources, revise_grouped, revise_opinions, structured_opinions
from app.revision.public_inputs import approved_sources, decode_sources
from app.runtime.store import RequestConflict, RevisionConflict, Store, receipt

ROOT = Path(__file__).resolve().parent.parent
COOKIE_NAME = os.environ.get("GONGWEN_COOKIE_NAME", "gongwen_browser")
WORKFLOW = json.loads((ROOT / "contracts/workflow.json").read_text(encoding="utf-8"))
PROFILES = {profile["id"]: profile for profile in WORKFLOW["profiles"]}
LABELS = {
    ("experience", "draft"): "经验材料起草",
    ("experience", "revise"): "经验材料改稿",
    ("work_plan", "draft"): "工作方案起草",
    ("work_plan", "revise"): "工作方案改稿",
}
ACTIONS = {"register_task", "query_status"}
REVISION_ACTIONS = {"generate_candidate", "commit_candidate", "apply_user_edit", "export_revision"}
WORD_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ID = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
OPINION_FILES = ("核稿单.html", "逐条核稿表.csv", "正文.docx", "本轮父版.docx", opinion_template.NAME, "运行回执.json")
DRAFT_PROFILE = "natural-draft"
DRAFT_FILES = ("正文.docx", "依据与待核.md", "运行回执.json")


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: ID
    profile_id: str
    original_request: str
    inputs: list
    public_trial: bool = False


class OpinionFile(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    content_base64: Annotated[str, Field(min_length=1, max_length=2_000_000)]


class OpinionIntake(BaseModel):
    request_id: ID
    original_request: Annotated[str, Field(min_length=1, max_length=4000)]
    public_trial: bool
    files: Annotated[list[OpinionFile], Field(min_length=1, max_length=8)]
    native_review: bool = False
    template: OpinionFile | None = None


class DraftIntake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: ID
    original_request: Annotated[str, Field(min_length=1, max_length=4000)]
    public_trial: bool
    attachment: OpinionFile | None = None
    expert_mode: Literal["single", "team"] = "single"


class FormalIntake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: ID
    public_trial: bool
    template: OpinionFile
    title: Annotated[str, Field(max_length=120)]
    number: Annotated[str, Field(max_length=40)]
    addressee: Annotated[str, Field(max_length=300)]
    body: Annotated[str, Field(max_length=24_000)]
    attachments: Annotated[str, Field(max_length=2_000)] = ""
    issuer: Annotated[str, Field(max_length=80)]
    date: Annotated[str, Field(max_length=30)]


class Generate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: ID
    expected_revision: ID
    source_set_version: int


class Commit(Generate):
    candidate_id: ID
    review_id: ID
    basis_key: ID


class BlockEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paragraph: Annotated[int, Field(gt=0, strict=True)]
    text: Annotated[str, Field(min_length=1, max_length=4000)]


class UserEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: ID
    expected_revision: ID
    block_edits: Annotated[list[BlockEdit], Field(min_length=1, max_length=20)]


def error_response(http_status, code, impact, recovery, *, request_id=None, task_id=None, retry_safe=False):
    return JSONResponse(
        receipt(
            "error", impact, request_id, task_id,
            error={
                "code": code, "impact": impact, "retry_safe": retry_safe,
                "recovery_action": recovery,
            },
        ),
        status_code=http_status,
    )


def create_app(db_path: Path = ROOT / "data" / "gongwen.sqlite3"):
    store = Store(db_path)
    model = os.environ.get("GONGWEN_PUBLIC_CODEX_MODEL")
    approved = approved_sources(ROOT) if model else {}
    rules = read_rules(ROOT)
    rule_version = sha256((TEMPLATE_VERSION + rules).encode()).hexdigest()

    @asynccontextmanager
    async def lifespan(_app):
        store.initialize()
        yield

    app = FastAPI(title="公文材料 Agent", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.environ.get("GONGWEN_ALLOWED_HOSTS", "127.0.0.1,localhost").split(","))
    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Validation errors can contain raw input; do not echo it.
        body = exc.body
        request_id = body.get("request_id") if isinstance(body, dict) else None
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
            request_id = None
        if request.url.path.endswith("/apply_user_edit"):
            return error_response(422, "INVALID_REQUEST", "手改格式无效，当前稿未变。",
                                  "检查段落及内容长度。", request_id=request_id)
        return error_response(422, "INVALID_REQUEST", "请求格式无效，未登记任务。", "检查登记表单。", request_id=request_id)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(_request, _exc):
        return error_response(
            503, "STORAGE_UNAVAILABLE", "存储暂不可用，无法确认任务状态。",
            "恢复本地存储后，使用原请求重试。", retry_safe=True,
        )

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        response = FileResponse(ROOT / "web/index.html", headers={"Cache-Control": "no-store"})
        if store.owner(request.cookies.get(COOKIE_NAME)) is None:
            response.set_cookie(COOKIE_NAME, store.create_session(), max_age=365 * 24 * 3600,
                                httponly=True, samesite="strict")
        return response

    @app.get("/healthz", include_in_schema=False)
    @app.get("/api/health")
    def health():
        store.check()
        return {"status": "ok", "storage": "available"}

    @app.get("/api/capabilities")
    def capabilities():
        actions = ACTIONS | REVISION_ACTIONS if approved else ACTIONS
        return {
            "contract_version": WORKFLOW["contract_version"],
            "profiles": [
                {"id": p["id"], "label": LABELS[p["document_type"], p["mode"]]}
                for p in PROFILES.values()
            ],
            "actions": [a["id"] for a in WORKFLOW["actions"] if a["id"] in actions],
            "attachments": bool(approved), "model": bool(approved), "export": bool(approved),
            "public_revision_profile": "plan-revise" if approved else None,
            "opinion_review": True, "opinion_external": bool(model), "formal_notice": True,
            "natural_draft": bool(model),
        }

    @app.post("/api/tasks", status_code=201)
    def register(request: Request, body: Registration):
        request_id = body.request_id
        owner_id = store.owner(request.cookies.get(COOKIE_NAME))
        if not owner_id:
            return error_response(401, "SESSION_REQUIRED", "当前浏览器尚未建立会话。",
                                  "先打开本地首页。", request_id=request_id)
        profile = PROFILES.get(body.profile_id)
        if not profile:
            return error_response(422, "INVALID_PROFILE", "所选路径无效，未登记任务。",
                                  "选择页面提供的四条路径之一。", request_id=request_id)
        if not body.original_request.strip():
            return error_response(422, "EMPTY_REQUEST", "需求为空，未登记任务。",
                                  "填写需求后再提交。", request_id=request_id)
        if body.inputs and not (approved and profile["id"] == "plan-revise" and body.public_trial):
            return error_response(422, "INPUTS_NOT_SUPPORTED", "本轮暂不支持附件，未登记任务。",
                                  "只提交需求，或在工作方案改稿中确认公开材料试跑。", request_id=request_id)
        metadata, sources, parent_word = [], {}, None
        try:
            if body.inputs:
                if len(body.original_request) > 4000:
                    return error_response(422, "INVALID_REQUEST", "试跑意见须在 4000 字以内。",
                                          "提供明确的日期修改与锁定范围。", request_id=request_id)
                metadata, sources = decode_sources(body.inputs, approved)
                paragraphs, _, _ = read_sources(sources)
                parent_word = word_bytes(build_word(paragraphs, sources["table"]))
            result, created = store.register_task(
                owner_id, request_id, body.original_request, metadata, profile,
                sources=[sources[source["role"]] for source in metadata], word=parent_word,
            )
        except RevisionIssue as error:
            return error_response(422, error.code, str(error), "检查试跑原件后重新登记。", request_id=request_id)
        except RequestConflict:
            return error_response(409, "REQUEST_CONFLICT", "该请求 ID 已用于另一份输入。",
                                  "查阅原任务；新需求使用新的请求 ID。", request_id=request_id)
        return JSONResponse(result, status_code=201 if created else 200)

    @app.get("/api/tasks/{task_id}")
    def query(request: Request, task_id: str):
        request_id = uuid4().hex
        owner_id = store.owner(request.cookies.get(COOKIE_NAME))
        result = store.query_status(owner_id, task_id, request_id) if owner_id else None
        if result is None:
            return error_response(404, "TASK_NOT_FOUND", "找不到此任务。",
                                  "检查任务 ID，并在登记时使用的浏览器中打开。",
                                  task_id=task_id, request_id=request_id)
        task = result["data"]["task"]
        if task["profile_id"] == "opinion-review":
            task["opinion_review"] = opinion_status(task_id)
        if task["profile_id"] == formal.PROFILE:
            task["formal_notice"] = {"files": list(formal.FILES) if formal_dir(task_id).is_dir() else []}
        if task["profile_id"] == DRAFT_PROFILE:
            task["natural_draft"] = draft_status(task_id)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    def owner_task(request, task_id, request_id):
        owner = store.owner(request.cookies.get(COOKIE_NAME))
        result = store.query_status(owner, task_id, request_id) if owner else None
        return owner, result

    def missing(task_id, request_id=None):
        return error_response(404, "TASK_NOT_FOUND", "找不到此任务。",
                              "在登记任务的浏览器中重开。", task_id=task_id, request_id=request_id)

    def conflict(task_id, request_id=None):
        return error_response(409, "REVISION_CONFLICT", "请求的版本或依据已变化，当前稿保持原样。",
                              "重开任务并核对当前稿。", task_id=task_id, request_id=request_id)

    def formal_dir(task_id):
        return db_path.parent / "formal" / task_id

    def draft_dir(task_id):
        return db_path.parent / "natural-draft" / task_id

    def draft_status(task_id):
        work = draft_dir(task_id)
        status_path = work / "status.json"
        if not status_path.is_file():
            return {"state": "running", "message": "任务已登记，等待起草结果。", "files": []}
        return json.loads(status_path.read_text(encoding="utf-8"))

    def generate_draft(body, work):
        started = perf_counter()
        output = work / "result"
        try:
            paths = []
            if file := body.attachment:
                inputs = work / "input"
                inputs.mkdir()
                path = inputs / file.name
                path.write_bytes(base64.b64decode(file.content_base64, validate=True))
                paths.append(path)
            run = agent.run_task(model, body.original_request, files=paths,
                                 checkpoint=work / "first-draft.json")
            agent.write_result(run, output)
            state, message = run.status, run.note or "初稿已生成，待核稿。"
        except (RevisionIssue, ValueError) as error:
            state, message = "failed", str(error)
        except OSError:
            state, message = "failed", "起草文件读写失败，请检查本地存储。"
        status = {"state": state, "message": message, "elapsed_seconds": round(perf_counter() - started, 2),
                  "files": [name for name in DRAFT_FILES if (output / name).is_file()]}
        pending = work / ".status.json"
        pending.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        pending.replace(work / "status.json")

    @app.post("/api/natural-drafts")
    def submit_draft(request: Request, body: DraftIntake, background: BackgroundTasks):
        owner = store.owner(request.cookies.get(COOKIE_NAME))
        if not owner or not model:
            return error_response(403, "DRAFT_UNAVAILABLE", "起草入口未开启或浏览器会话未建立。", "重开首页。")
        if not body.public_trial or not body.original_request.strip():
            return error_response(422, "DRAFT_INPUT_REQUIRED", "请填写需求并确认材料公开。", "核对提交范围。")
        metadata = []
        if file := body.attachment:
            try:
                if (file.name.startswith(".") or any(c in file.name for c in "/\\\x00\r\n")
                        or Path(file.name).suffix.lower() != ".txt"):
                    raise ValueError
                content = base64.b64decode(file.content_base64, validate=True)
                if not content or len(content) > 100_000 or not content.decode("utf-8-sig").strip():
                    raise ValueError
            except (ValueError, binascii.Error):
                return error_response(422, "INVALID_DRAFT_FILE", "请使用有效的 UTF-8 TXT 附件。", "附件上限 100 KB。")
            metadata.append({"name": file.name, "sha256": sha256(content).hexdigest()})
        try:
            registration, created = store.register_task(
                owner, body.request_id, body.original_request, metadata,
                {"id": DRAFT_PROFILE, "document_type": "work_plan", "mode": "draft"},
                request_options={"expert_mode": "team"} if body.expert_mode == "team" else None,
            )
        except RequestConflict:
            return error_response(409, "REQUEST_CONFLICT", "该请求 ID 已用于另一份输入。", "重开原任务。")
        task_id = registration["task_id"]
        if not created:
            return {"task_id": task_id, **draft_status(task_id)}
        work = draft_dir(task_id)
        work.mkdir(mode=0o700, parents=True)
        background.add_task(generate_draft, body, work)
        return {"task_id": task_id, **draft_status(task_id)}

    @app.get("/api/tasks/{task_id}/draft/{name}")
    def download_draft(request: Request, task_id: str, name: str):
        _, task = owner_task(request, task_id, None)
        if task is None or task["data"]["task"]["profile_id"] != DRAFT_PROFILE or name not in DRAFT_FILES:
            return missing(task_id)
        path = draft_dir(task_id) / "result" / name
        if not path.is_file():
            return missing(task_id)
        return FileResponse(path, filename=name, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/formal-tasks")
    def submit_formal(request: Request, body: FormalIntake):
        owner = store.owner(request.cookies.get(COOKIE_NAME))
        if not owner:
            return error_response(401, "SESSION_REQUIRED", "当前浏览器尚未建立会话。", "先打开本地首页。")
        if not body.public_trial:
            return error_response(422, "FORMAL_CONSENT", "请确认材料可在本机处理。", "核对模板与材料的使用权限。")
        try:
            template = base64.b64decode(body.template.content_base64, validate=True)
            if (not 0 < len(template) <= 1_500_000 or not body.template.name.lower().endswith(".docx")
                    or any(ch in body.template.name for ch in "/\\\x00\r\n")):
                raise ValueError
        except (ValueError, binascii.Error):
            return error_response(422, "INVALID_FORMAL_TEMPLATE", "请上传不超过 1.5 MB 的 DOCX 样张。", formal.TEMPLATE_HELP)
        try:
            fields = formal.clean_fields(body.model_dump(exclude={"request_id", "public_trial", "template"}))
            outputs = formal.render(template, fields)
        except RevisionIssue as error:
            return error_response(422, error.code, str(error), "检查模板和已核定的公文要素后重试。")
        serialized = json.dumps(fields, ensure_ascii=False, sort_keys=True)
        metadata = [
            {"name": body.template.name, "role": "template", "sha256": sha256(template).hexdigest()},
            {"name": "通知材料", "role": "notice", "sha256": sha256(serialized.encode()).hexdigest()},
        ]
        try:
            registration, _ = store.register_task(
                owner, body.request_id, fields["title"], metadata,
                {"id": formal.PROFILE, "document_type": "notice", "mode": "revise"},
            )
        except RequestConflict:
            return error_response(409, "REQUEST_CONFLICT", "该请求 ID 已用于另一份输入。", "新需求使用新的请求 ID。")
        task_id = registration["task_id"]
        work = formal_dir(task_id)
        try:
            work.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not work.is_dir():
                with TemporaryDirectory(dir=work.parent) as staging:
                    pending = Path(staging) / "result"
                    pending.mkdir(mode=0o700)
                    (pending / "输入模板.docx").write_bytes(template)
                    (pending / "通知材料.json").write_text(serialized, encoding="utf-8")
                    for name, content in zip(formal.FILES, outputs):
                        (pending / name).write_bytes(content)
                    try:
                        pending.rename(work)
                    except OSError as error:
                        if error.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not work.is_dir():
                            raise
        except OSError:
            return error_response(503, "FORMAL_STORAGE", "套版文件保存失败。", "检查本机存储后原样重试。",
                                  task_id=task_id, retry_safe=True)
        return {"task_id": task_id}

    @app.get("/api/tasks/{task_id}/formal/{name}")
    def download_formal(request: Request, task_id: str, name: str):
        _, task = owner_task(request, task_id, None)
        if task is None or task["data"]["task"]["profile_id"] != formal.PROFILE or name not in formal.FILES:
            return missing(task_id)
        path = formal_dir(task_id) / name
        if not path.is_file():
            return missing(task_id)
        return FileResponse(path, filename=name, media_type=WORD_TYPE,
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    def opinion_dir(task_id):
        return db_path.parent / "opinions" / task_id

    def opinion_status(task_id):
        work = opinion_dir(task_id)
        result = work / "result"
        failure = work / "error.json"
        if result.is_dir():
            run = json.loads((result / "运行回执.json").read_text(encoding="utf-8"))
            return {"state": run["status"], "children": run.get("child_count", 0),
                    "native": {key: run.get(key) for key in ("message", "comments", "comments_located", "applied", "body_pending_marks", "outside_body_marks", "template_note")}
                    if run.get("profile") == native_review.PROFILE else None,
                    "files": [name for name in OPINION_FILES if (result / name).is_file()]}
        if failure.is_file():
            return {"state": "failed", "error": json.loads(failure.read_bytes())}
        return {"state": "running"}

    @app.post("/api/opinion-tasks")
    def submit_opinion(request: Request, body: OpinionIntake):
        owner = store.owner(request.cookies.get(COOKIE_NAME))
        if not owner or (not model and not body.native_review):
            return error_response(403, "OPINION_UNAVAILABLE", "核稿入口未开启或浏览器会话未建立。", "重开首页。")
        if not body.public_trial or not body.original_request.strip():
            return error_response(422, "OPINION_CONSENT", "请填写改稿要求并确认材料可以在本机处理。", "核对提交范围。")
        if body.native_review and (len(body.files) != 1 or Path(body.files[0].name).suffix.lower() != ".docx"):
            return error_response(422, "NATIVE_PARENT_REQUIRED", "原生审阅需要一份 DOCX 原稿。", "把空白模板放在单独的模板栏。")
        files = {}
        try:
            for file in [*body.files, *([body.template] if body.template else [])]:
                name = file.name
                if (name.startswith(".") or any(c in name for c in "/\\\x00\r\n")
                        or Path(name).suffix.lower() not in {".docx", ".txt", ".md", ".csv"} or name in files):
                    raise ValueError
                files[name] = base64.b64decode(file.content_base64, validate=True)
                if not files[name] or len(files[name]) > 1_500_000:
                    raise ValueError
            if body.template and Path(body.template.name).suffix.lower() != ".docx":
                raise ValueError
        except (ValueError, binascii.Error):
            return error_response(422, "INVALID_OPINION_FILE", "附件名称、格式或大小不符合要求。", "文件须同名不重复且不超过 1.5 MB。")
        metadata = [{"name": name, "sha256": sha256(content).hexdigest()} for name, content in files.items()]
        if body.native_review or body.template:
            for item in metadata:
                item["role"] = ("template" if body.template and item["name"] == body.template.name else
                                "native_parent" if body.native_review else "material")
        try:
            registration, _ = store.register_task(
                owner, body.request_id, body.original_request, metadata,
                {"id": "opinion-review", "document_type": "work_plan", "mode": "revise"},
            )
        except RequestConflict:
            return error_response(409, "REQUEST_CONFLICT", "该请求 ID 已用于另一份输入。", "新需求使用新的请求 ID。")
        task_id = registration["task_id"]
        work = opinion_dir(task_id)
        try:
            work.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            return {"task_id": task_id, **opinion_status(task_id)}
        try:
            inputs = work / "input"
            inputs.mkdir()
            paths = []
            template = None
            for name, content in files.items():
                path = inputs / name
                path.write_bytes(content)
                if body.template and name == body.template.name:
                    template = path
                else:
                    paths.append(path)
            request_text = body.original_request
            if body.native_review:
                native_review.review(request_text, paths[0], work / "result", template=template)
            elif opinions := structured_opinions(paths):
                revise_opinions(model, request_text, paths, work / "result", opinions, template=template)
            elif template is not None:
                raise RevisionIssue("TEMPLATE_NOT_APPLICABLE", "套版需要原生审阅或结构化的意见及答复表。")
            elif intake := review_sources(request_text, paths):
                revise_grouped(model, request_text, paths, work / "result", intake)
            else:
                raise RevisionIssue("OPINION_RECORD_MISSING", "找不到可识别的意见处理表。请提交 Word 意见及答复表。")
        except (RevisionIssue, ValueError) as error:
            detail = {"code": getattr(error, "code", "INVALID_MATERIAL"), "message": str(error)}
        except OSError:
            detail = {"code": "OPINION_STORAGE", "message": "核稿文件读写失败，请核查本地存储。"}
        else:
            detail = None
        if detail:
            failure = work / ".error.json"
            failure.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")
            failure.replace(work / "error.json")
        return {"task_id": task_id, **opinion_status(task_id)}

    @app.get("/api/tasks/{task_id}/opinion/{name}")
    def download_opinion(request: Request, task_id: str, name: str):
        _, task = owner_task(request, task_id, None)
        if task is None or task["data"]["task"]["profile_id"] != "opinion-review" or name not in OPINION_FILES:
            return missing(task_id)
        path = opinion_dir(task_id) / "result" / name
        if not path.is_file():
            return missing(task_id)
        return FileResponse(path, filename=name, content_disposition_type="inline" if name.endswith(".html") else "attachment",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                     "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"})

    @app.post("/api/tasks/{task_id}/generate_candidate")
    def generate(request: Request, task_id: str, body: Generate):
        if not approved:
            return missing(task_id, body.request_id)
        owner, existing = owner_task(request, task_id, body.request_id)
        if existing is None:
            return missing(task_id, body.request_id)
        try:
            claim = store.begin_revision(owner, task_id, body.request_id, body.expected_revision,
                                         body.source_set_version, rule_version)
        except RequestConflict:
            return error_response(409, "RUN_ALREADY_REGISTERED", "当前无法开始新一轮生成。",
                                  "重开任务查看当前稿和已登记的生成记录。", task_id=task_id, request_id=body.request_id)
        except RevisionConflict:
            return conflict(task_id, body.request_id)
        if not claim:
            return store.query_status(owner, task_id, body.request_id)

        original, sources, parent_word = claim
        model_result = None
        try:
            if {role: sha256(content).hexdigest() for role, content in sources.items()} != approved:
                raise RevisionIssue("SOURCE_NOT_APPROVED", "该任务的来源与已批准的公开材料不一致。")
            paragraphs, rows, reference = read_sources(sources)
            selection, model_metadata = pick_date(model, rules, original, paragraphs, rows, reference)
            model_result = {"selection": selection, **model_metadata}
            parent = open_docx(parent_word)
            current_body = [p.text for p in parent.paragraphs[:len(paragraphs)]]
            word, view = prepare_revision(parent_word, current_body, table_rows(parent.tables[0]), original, selection)
            store.finish_revision(task_id, body.request_id, word=word, view=view, model_result=model_result)
        except RevisionIssue as error:
            detail = {"code": error.code, "message": str(error)}
            store.finish_revision(task_id, body.request_id, model_result=model_result, error=detail)
            status = 409 if error.code == "LOCKED_SCOPE_CONFLICT" else 422
            return error_response(status, error.code, str(error), "重开任务查看父版及本轮记录。",
                                  task_id=task_id, request_id=body.request_id)
        result = store.query_status(owner, task_id, body.request_id)
        if result["data"]["task"]["current_revision"] == body.expected_revision:
            result["summary"] = "候选已通过范围、同步和锁定检查，待采用。"
            result["next_actions"] = ["commit_candidate"]
        else:
            result["summary"] = "候选已保留。当前稿在生成期间发生变化，该候选已过期。"
        return result

    @app.post("/api/tasks/{task_id}/commit_candidate")
    def commit(request: Request, task_id: str, body: Commit):
        owner, existing = owner_task(request, task_id, body.request_id)
        if existing is None:
            return missing(task_id, body.request_id)
        try:
            store.commit_candidate(owner, task_id, body.candidate_id, body.expected_revision, body.review_id,
                                   body.source_set_version, body.basis_key, rule_version)
        except RevisionConflict:
            return conflict(task_id, body.request_id)
        result = store.query_status(owner, task_id, body.request_id)
        result["summary"] = "已采用候选为当前稿，待业务核稿。"
        result["next_actions"] = ["export_revision"]
        return result

    @app.post("/api/tasks/{task_id}/apply_user_edit")
    def edit(request: Request, task_id: str, body: UserEdit):
        owner = store.owner(request.cookies.get(COOKIE_NAME))
        if not owner or store.query_status(owner, task_id, body.request_id) is None:
            return missing(task_id, body.request_id)
        try:
            saved = store.apply_user_edit(owner, task_id, body.request_id, body.expected_revision,
                                          [edit.model_dump() for edit in body.block_edits])
        except RequestConflict:
            return error_response(409, "REQUEST_CONFLICT", "该请求 ID 已用于另一份手改。",
                                  "重开任务并核对当前稿。", task_id=task_id, request_id=body.request_id)
        except RevisionConflict:
            return conflict(task_id, body.request_id)
        except RevisionIssue as error:
            status = 409 if error.code.endswith("CONFLICT") else 422
            return error_response(status, error.code, str(error), "检查当前稿中的可编辑段落。",
                                  task_id=task_id, request_id=body.request_id)
        result = store.query_status(owner, task_id, body.request_id)
        result["data"].update(saved)
        result["revision_id"] = saved["revision_id"]
        result["summary"] = "已保存手改为新版本，待核稿。"
        result["next_actions"] = ["export_revision"]
        return result

    @app.get("/api/tasks/{task_id}/revisions/{revision_id}/word")
    def download(request: Request, task_id: str, revision_id: str, allow_draft: bool = False):
        owner, existing = owner_task(request, task_id, None)
        if existing is None:
            return missing(task_id)
        try:
            word, artifact = store.export_revision(owner, task_id, revision_id, TEMPLATE_VERSION,
                                                   rule_version, allow_draft=allow_draft)
        except RevisionConflict:
            return conflict(task_id)
        return Response(word, media_type=WORD_TYPE, headers={
            "Content-Disposition": f'attachment; filename="work-plan-{artifact["revision_id"]}.docx"',
            "Cache-Control": "no-store", "X-Task-Id": task_id,
            "X-Revision-Id": revision_id, "X-Artifact-Id": artifact["artifact_id"],
            "X-Quality-Label": artifact["quality_label"],
            "X-Content-Sha256": artifact["content_hash"], "X-Template-Version": TEMPLATE_VERSION,
        })

    @app.get("/api/tasks/{task_id}/reference")
    def download_reference(request: Request, task_id: str):
        owner = store.owner(request.cookies.get(COOKIE_NAME))
        source = store.original_reference(owner, task_id) if owner else None
        if not source:
            return missing(task_id)
        return Response(source["content"], media_type=WORD_TYPE, headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(source['name'])}",
            "Cache-Control": "no-store", "X-Content-Sha256": source["content_hash"],
        })

    return app


app = create_app()
