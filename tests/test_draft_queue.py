import asyncio
import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from docx import Document

from fastapi import BackgroundTasks, Request

from app.main import COOKIE_NAME, DraftIntake, create_app
from app.runtime.store import Store


class DraftQueueTests(unittest.TestCase):
    def test_submit_returns_running_before_generation_and_replay_does_not_queue_twice(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"GONGWEN_PUBLIC_CODEX_MODEL": "fixture"}):
            root = Path(tmp)
            (root / "web").mkdir()
            store = Store(root / "test.sqlite3")
            store.initialize()
            token = store.create_session()
            request = Request({"type": "http", "headers": [(b"cookie", f"{COOKIE_NAME}={token}".encode())]})
            with patch("app.main.ROOT", root), patch("app.main.approved_sources", return_value={}), \
                    patch("app.main.read_rules", return_value="rules"):
                app = create_app(root / "test.sqlite3")
                endpoint = next(r.endpoint for r in app.routes if r.path == "/api/natural-drafts")
                body = DraftIntake(request_id="draft-queue", original_request="公开合成通知", public_trial=True)
                background = BackgroundTasks()
                def write_result(run, output):
                    output.mkdir(parents=True)
                    word = output / '正文.docx'
                    doc = Document(); doc.add_paragraph('社区业务培训通知'); doc.save(word)
                    (output / '依据与待核.md').write_text('内部依据')
                    (output / '正文.txt').write_text('内部续改底稿')
                    (output / '运行回执.json').write_text(json.dumps({
                        'status': 'draft', 'candidate': {'title': '社区业务培训通知'},
                        'artifacts': {'正文.docx': hashlib.sha256(word.read_bytes()).hexdigest()}}))
                with patch("app.main.agent.run_task", return_value=SimpleNamespace(status="draft", note="待核稿")) as run, \
                        patch("app.main.agent.write_result", side_effect=write_result):
                    result = endpoint(request, body, background)
                    self.assertEqual(result["state"], "running")
                    run.assert_not_called()
                    self.assertEqual(len(background.tasks), 1)
                    replay = BackgroundTasks()
                    self.assertEqual(endpoint(request, body, replay), result)
                    self.assertEqual(len(replay.tasks), 0)
                    asyncio.run(background())
                    run.assert_called_once()
                    completed = endpoint(request, body, BackgroundTasks())
                    self.assertEqual(completed["state"], "draft")
                    self.assertEqual(completed["task_id"], result["task_id"])
                    self.assertEqual(completed['files'], ['社区业务培训通知.docx'])
                    self.assertEqual(completed['message'], 'Word写好了，可以直接修改。')
                    download = next(r.endpoint for r in app.routes if r.path == '/api/tasks/{task_id}/draft/{name}')
                    self.assertEqual(download(request, result['task_id'], '依据与待核.md').status_code, 404)
                    self.assertEqual(download(request, result['task_id'], '运行回执.json').status_code, 404)
                    self.assertEqual(download(request, result['task_id'], '社区业务培训通知.docx').filename,
                                     '社区业务培训通知.docx')


if __name__ == "__main__":
    unittest.main()
