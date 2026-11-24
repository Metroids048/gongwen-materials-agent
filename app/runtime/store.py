"""SQLite persistence for browser owners, tasks and registration receipts."""

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from app.revision.document import apply_block_edits, current_view, open_docx, read_body


class RequestConflict(Exception):
    pass


class RevisionConflict(Exception):
    pass


def receipt(status, summary, request_id, task_id, *, data=None, error=None):
    result = {
        "status": status, "summary": summary, "request_id": request_id,
        "task_id": task_id, "next_actions": [], "artifacts": [],
    }
    if data is not None:
        result["data"] = data
    if error is not None:
        result["error"] = error
    return result


class Store:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.path.parent / "tasks").mkdir(exist_ok=True, mode=0o700)
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS owners (
                    owner_id TEXT PRIMARY KEY,
                    session_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
                    profile_id TEXT NOT NULL,
                    original_request TEXT NOT NULL,
                    requirements TEXT NOT NULL DEFAULT '[]',
                    task_state TEXT NOT NULL DEFAULT 'received',
                    quality_state TEXT NOT NULL DEFAULT 'unreviewed',
                    current_revision TEXT,
                    source_set_version INTEGER NOT NULL DEFAULT 0,
                    basis_key TEXT
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    owner_id TEXT NOT NULL REFERENCES owners(owner_id),
                    action TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    payload TEXT NOT NULL,
                    result TEXT NOT NULL,
                    PRIMARY KEY (owner_id, action, request_id)
                );
                CREATE INDEX IF NOT EXISTS receipts_task ON receipts(task_id, action);
                CREATE TABLE IF NOT EXISTS revision_sources (
                    source_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    role TEXT NOT NULL,
                    name TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    content BLOB NOT NULL,
                    UNIQUE (task_id, role)
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    revision_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    parent_revision TEXT REFERENCES revisions(revision_id),
                    basis_key TEXT,
                    review_id TEXT,
                    rule_version TEXT,
                    word BLOB NOT NULL,
                    view TEXT
                );
                CREATE TABLE IF NOT EXISTS word_exports (
                    revision_id TEXT PRIMARY KEY REFERENCES revisions(revision_id),
                    artifact_id TEXT NOT NULL,
                    template_version TEXT NOT NULL,
                    content_hash TEXT NOT NULL
                );
            """)
            with db:
                db.execute("BEGIN IMMEDIATE")
                legacy = any(column["name"] == "request_id" and not column["pk"]
                             for column in db.execute("PRAGMA table_info(revision_runs)"))
                if legacy:
                    db.execute("ALTER TABLE revision_runs RENAME TO revision_runs_legacy")
                db.execute("""
                  CREATE TABLE IF NOT EXISTS revision_runs (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    request_id TEXT NOT NULL,
                    base_revision TEXT NOT NULL REFERENCES revisions(revision_id),
                    source_set_version INTEGER NOT NULL,
                    basis_key TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    state TEXT NOT NULL,
                    model_result TEXT,
                    error TEXT,
                    PRIMARY KEY (task_id, request_id),
                    UNIQUE (task_id, base_revision)
                  )
                """)
                if legacy:
                    db.execute("INSERT INTO revision_runs SELECT * FROM revision_runs_legacy")
                    db.execute("DROP TABLE revision_runs_legacy")

    def check(self):
        with closing(self.connect()) as db:
            db.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()

    def create_session(self):
        token = secrets.token_urlsafe(32)
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO owners VALUES (?, ?)", (uuid4().hex, self.session_hash(token)))
        return token

    @staticmethod
    def session_hash(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def owner(self, token):
        if not token:
            return None
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT owner_id FROM owners WHERE session_hash = ?", (self.session_hash(token),)
            ).fetchone()
            return row["owner_id"] if row else None

    def register_task(self, owner_id, request_id, original_request, inputs, profile, *, sources=(), word=None,
                      request_options=None):
        profile_id = profile["id"]
        payload = json.dumps([profile_id, original_request, inputs, *([request_options] if request_options else [])],
                             ensure_ascii=False)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT payload, result FROM receipts "
                "WHERE owner_id = ? AND action = 'register_task' AND request_id = ?",
                (owner_id, request_id),
            ).fetchone()
            if old:
                if old["payload"] != payload:
                    raise RequestConflict
                return json.loads(old["result"]), False

            task_id = uuid4().hex
            db.execute(
                "INSERT INTO tasks (task_id, owner_id, profile_id, original_request) VALUES (?, ?, ?, ?)",
                (task_id, owner_id, profile_id, original_request),
            )
            source_ids = []
            if sources:
                parent = uuid4().hex
                for source, content in zip(inputs, sources, strict=True):
                    source_ids.append(uuid4().hex)
                    db.execute("INSERT INTO revision_sources VALUES (?, ?, ?, ?, ?, ?)", (
                        source_ids[-1], task_id, source["role"], source["name"], source["content_hash"], content,
                    ))
                db.execute("INSERT INTO revisions (revision_id, task_id, word) VALUES (?, ?, ?)",
                           (parent, task_id, word))
                db.execute("UPDATE tasks SET current_revision = ?, source_set_version = 1 WHERE task_id = ?",
                           (parent, task_id))
            result = receipt(
                "success", "原件已保存，可开始改稿。" if sources else "任务已登记，后续处理待接入。",
                request_id, task_id,
                data={
                    "profile_id": profile_id,
                    "source_ids": source_ids,
                    "task_state": "received",
                    "task_understanding": {
                        "document_type": profile["document_type"], "mode": profile["mode"],
                    },
                },
            )
            db.execute(
                "INSERT INTO receipts VALUES (?, 'register_task', ?, ?, ?, ?)",
                (owner_id, request_id, task_id, payload, json.dumps(result, ensure_ascii=False)),
            )
            return result, True

    def query_status(self, owner_id, task_id, request_id):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT tasks.*, receipts.result AS registration FROM tasks "
                "JOIN receipts ON receipts.task_id = tasks.task_id "
                "AND receipts.owner_id = tasks.owner_id AND receipts.action = 'register_task' "
                "WHERE tasks.task_id = ? AND tasks.owner_id = ?",
                (task_id, owner_id),
            ).fetchone()
            revision = None
            if row and row["source_set_version"]:
                sources = db.execute(
                    "SELECT source_id, role, name, content_hash FROM revision_sources WHERE task_id = ?",
                    (task_id,),
                ).fetchall()
                run = db.execute("SELECT * FROM revision_runs WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
                                 (task_id,)).fetchone()
                candidate = db.execute(
                    "SELECT revision_id, parent_revision, review_id, view FROM revisions "
                    "WHERE task_id = ? AND review_id IS NOT NULL ORDER BY rowid DESC LIMIT 1", (task_id,),
                ).fetchone()
                current = self.read_current(db, task_id, row["current_revision"])
                revision = {
                    "sources": [dict(source) for source in sources],
                    "run": dict(run) if run else None,
                    "candidate": dict(candidate) if candidate else None,
                    "current": None,
                    "can_generate": bool(current) and self.can_generate(db, task_id, row["current_revision"]),
                }
        if row is None:
            return None
        task = dict(row)
        registration = json.loads(task.pop("registration"))
        task.pop("owner_id")
        task["requirements"] = json.loads(task["requirements"])
        if revision:
            if current:
                saved_view = json.loads(current["view"]) if current["view"] else None
                revision["current"] = {
                    "parent_revision": current["parent_revision"],
                    "manual": saved_view is not None and current["review_id"] is None,
                    "view": current_view(open_docx(current["word"]), read_body(current["body_source"]),
                                         task["original_request"], saved_view),
                }
            if revision["run"]:
                for field in ("model_result", "error"):
                    value = revision["run"][field]
                    revision["run"][field] = json.loads(value) if value else None
            if revision["candidate"]:
                revision["candidate"]["view"] = json.loads(revision["candidate"]["view"])
            task["revision_work"] = revision
        return receipt(
            "success", "已读取任务记录。", request_id, task_id,
            data={
                "task": task,
                "task_state": task["task_state"],
                "job_state": revision["run"]["state"] if revision and revision["run"] else None,
                "existing_result": registration,
                "target_request_or_job_id": registration["request_id"],
                "recovery_action": "在原浏览器按任务 ID 重开。",
            },
        )

    @staticmethod
    def read_current(db, task_id, revision_id):
        return db.execute(
            "SELECT r.*, s.content AS body_source FROM revisions r "
            "JOIN revision_sources s ON s.task_id = r.task_id AND s.role = 'body' "
            "WHERE r.task_id = ? AND r.revision_id = ?", (task_id, revision_id),
        ).fetchone()

    def apply_user_edit(self, owner, task_id, request_id, expected, block_edits):
        payload = json.dumps([task_id, expected, block_edits], ensure_ascii=False)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT payload, result FROM receipts "
                "WHERE owner_id = ? AND action = 'apply_user_edit' AND request_id = ?", (owner, request_id),
            ).fetchone()
            if old:
                if old["payload"] != payload:
                    raise RequestConflict
                return json.loads(old["result"])
            task = db.execute("SELECT * FROM tasks WHERE task_id = ? AND owner_id = ? AND profile_id = 'plan-revise'",
                              (task_id, owner)).fetchone()
            if not task or task["current_revision"] != expected or task["source_set_version"] != 1:
                raise RevisionConflict
            current = self.read_current(db, task_id, expected)
            if not current:
                raise RevisionConflict
            saved_view = json.loads(current["view"]) if current["view"] else None
            word, view = apply_block_edits(current["word"], read_body(current["body_source"]),
                                           task["original_request"], saved_view, block_edits)
            reviews = db.execute("SELECT review_id FROM revisions WHERE task_id = ? AND basis_key = ? "
                                 "AND review_id IS NOT NULL", (task_id, task["basis_key"]))
            export = db.execute("SELECT artifact_id FROM word_exports WHERE revision_id = ?", (expected,)).fetchone()
            revision_id = uuid4().hex
            result = {"revision_id": revision_id, "invalidated_reviews": [r["review_id"] for r in reviews],
                      "invalidated_artifacts": [export["artifact_id"]] if export else []}
            db.execute("INSERT INTO revisions (revision_id, task_id, parent_revision, word, view) VALUES (?, ?, ?, ?, ?)",
                       (revision_id, task_id, expected, word, json.dumps(view, ensure_ascii=False)))
            db.execute("UPDATE tasks SET current_revision = ?, task_state = 'reviewing', "
                       "quality_state = 'unreviewed', basis_key = NULL WHERE task_id = ?", (revision_id, task_id))
            db.execute("INSERT INTO receipts VALUES (?, 'apply_user_edit', ?, ?, ?, ?)",
                       (owner, request_id, task_id, payload, json.dumps(result, ensure_ascii=False)))
            return result

    @staticmethod
    def can_generate(db, task_id, revision_id):
        attempted = db.execute(
            "SELECT 1 FROM revision_runs WHERE task_id = ? "
            "AND (base_revision = ? OR state IN ('running', 'unknown')) LIMIT 1", (task_id, revision_id),
        ).fetchone()
        if attempted:
            return False
        # A reviewed revision on the current branch has already addressed the original request.
        adopted = db.execute("""
            WITH RECURSIVE lineage(parent_revision, review_id) AS (
                SELECT parent_revision, review_id FROM revisions WHERE task_id = ? AND revision_id = ?
                UNION ALL
                SELECT r.parent_revision, r.review_id FROM revisions r
                JOIN lineage l ON r.revision_id = l.parent_revision WHERE r.task_id = ?
            )
            SELECT 1 FROM lineage WHERE review_id IS NOT NULL LIMIT 1
        """, (task_id, revision_id, task_id)).fetchone()
        return adopted is None

    def begin_revision(self, owner, task_id, request_id, expected, source_set_version, rule_version):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM tasks WHERE task_id = ? AND owner_id = ?",
                              (task_id, owner)).fetchone()
            if task is None:
                return None
            if task["profile_id"] != "plan-revise" or task["source_set_version"] != source_set_version or source_set_version != 1:
                raise RevisionConflict
            run = db.execute(
                "SELECT base_revision, rule_version FROM revision_runs WHERE task_id = ? AND request_id = ?",
                (task_id, request_id),
            ).fetchone()
            if run:
                if (run["base_revision"], run["rule_version"]) == (expected, rule_version):
                    return False
                raise RequestConflict
            if task["current_revision"] != expected:
                raise RevisionConflict
            if not self.can_generate(db, task_id, expected):
                raise RequestConflict
            sources = db.execute(
                "SELECT role, content_hash, content FROM revision_sources WHERE task_id = ? ORDER BY role",
                (task_id,),
            ).fetchall()
            basis = hashlib.sha256(json.dumps([
                task_id, expected, task["source_set_version"], task["original_request"],
                [source["content_hash"] for source in sources], rule_version,
            ], ensure_ascii=False).encode()).hexdigest()
            db.execute(
                "INSERT INTO revision_runs VALUES (?, ?, ?, 1, ?, ?, 'running', NULL, NULL)",
                (task_id, request_id, expected, basis, rule_version),
            )
            db.execute("UPDATE tasks SET task_state = 'drafting', basis_key = ? WHERE task_id = ?",
                       (basis, task_id))
            parent = db.execute("SELECT word FROM revisions WHERE revision_id = ? AND task_id = ?",
                                (expected, task_id)).fetchone()
            return (task["original_request"], {source["role"]: source["content"] for source in sources},
                    parent["word"])

    def finish_revision(self, task_id, request_id, *, word=None, view=None, model_result=None, error=None):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                "SELECT * FROM revision_runs WHERE task_id = ? AND request_id = ? AND state = 'running'",
                (task_id, request_id),
            ).fetchone()
            if run is None:
                raise RevisionConflict
            if error:
                code = error["code"]
                state = "needs_input" if code in {"NEEDS_INPUT", "LOCKED_SCOPE_CONFLICT", "SOURCE_UNREADABLE"} else "failed"
                job = "unknown" if code == "JOB_OUTCOME_UNKNOWN" else "failed"
            else:
                db.execute(
                    "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (uuid4().hex, task_id, run["base_revision"], run["basis_key"], uuid4().hex,
                     run["rule_version"], word, json.dumps(view, ensure_ascii=False)),
                )
                state, job = "reviewing", "succeeded"
            db.execute("UPDATE revision_runs SET state = ?, model_result = ?, error = ? "
                       "WHERE task_id = ? AND request_id = ?",
                       (job, json.dumps(model_result, ensure_ascii=False),
                        json.dumps(error, ensure_ascii=False), task_id, request_id))
            db.execute("UPDATE tasks SET task_state = ? WHERE task_id = ? AND current_revision = ? AND basis_key = ?",
                       (state, task_id, run["base_revision"], run["basis_key"]))

    def commit_candidate(self, owner, task_id, candidate_id, expected, review_id, source_set_version, basis_key, rule_version):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT t.current_revision, t.source_set_version, t.basis_key, "
                "r.parent_revision, r.review_id, r.rule_version, r.basis_key AS candidate_basis, "
                "g.source_set_version AS candidate_source "
                "FROM tasks t JOIN revisions r ON r.task_id = t.task_id "
                "JOIN revision_runs g ON g.task_id = t.task_id AND g.basis_key = r.basis_key "
                "WHERE t.task_id = ? AND t.owner_id = ? AND r.revision_id = ?",
                (task_id, owner, candidate_id),
            ).fetchone()
            if (not row or not row["review_id"] or row["review_id"] != review_id
                    or row["rule_version"] != rule_version or row["parent_revision"] != expected
                    or row["basis_key"] != basis_key or row["candidate_basis"] != basis_key
                    or row["source_set_version"] != source_set_version or row["candidate_source"] != source_set_version):
                raise RevisionConflict
            if row["current_revision"] == candidate_id:
                return
            if row["current_revision"] != expected:
                raise RevisionConflict
            db.execute("UPDATE tasks SET current_revision = ?, task_state = 'ready_for_review', "
                       "quality_state = 'reviewable' WHERE task_id = ?", (candidate_id, task_id))

    def export_revision(self, owner, task_id, revision_id, template_version, rule_version, *, allow_draft=False):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT r.word, r.review_id, r.view IS NOT NULL AS has_view, r.basis_key, r.rule_version, "
                "t.current_revision, t.quality_state, t.basis_key AS task_basis, "
                "g.source_set_version = t.source_set_version AS sources_match "
                "FROM tasks t JOIN revisions r ON r.task_id = t.task_id "
                "LEFT JOIN revision_runs g ON g.task_id = t.task_id AND g.basis_key = r.basis_key "
                "WHERE t.owner_id = ? AND t.task_id = ? AND r.revision_id = ?",
                (owner, task_id, revision_id),
            ).fetchone()
            if not row or row["current_revision"] != revision_id:
                raise RevisionConflict
            reviewed = (row["quality_state"] == "reviewable" and row["review_id"] and row["sources_match"]
                        and row["basis_key"] == row["task_basis"] and row["rule_version"] == rule_version)
            manual_draft = (allow_draft and row["quality_state"] == "unreviewed" and row["has_view"]
                            and not row["review_id"])
            if not (reviewed or manual_draft):
                raise RevisionConflict
            db.execute("INSERT OR IGNORE INTO word_exports VALUES (?, ?, ?, ?)", (
                revision_id, uuid4().hex, template_version, hashlib.sha256(row["word"]).hexdigest(),
            ))
            export = db.execute("SELECT * FROM word_exports WHERE revision_id = ?", (revision_id,)).fetchone()
            if export["template_version"] != template_version:
                raise RevisionConflict
            return row["word"], {**dict(export), "quality_label": row["quality_state"]}

    def original_reference(self, owner, task_id):
        with closing(self.connect()) as db:
            return db.execute(
                "SELECT s.name, s.content, s.content_hash FROM revision_sources s "
                "JOIN tasks t ON s.task_id = t.task_id "
                "WHERE t.owner_id = ? AND t.task_id = ? AND s.role = 'reference'",
                (owner, task_id),
            ).fetchone()
