"""Exercise recovery from the database rather than the lifetime of an app object."""

import tempfile
import unittest
from pathlib import Path

from app.runtime.store import Store
from test_contracts import read_json


class Persistence(unittest.TestCase):
    def test_reopen_persistent_task_and_receipt(self):
        workflow = read_json("contracts/workflow.json")
        profile = next(p for p in workflow["profiles"] if p["id"] == "plan-revise")
        original = "  把截止日期往后挪。\n第二段原样保留。  "
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite3"
            store = Store(path)
            store.initialize()
            token = store.create_session()
            owner = store.owner(token)
            registration, created = store.register_task(owner, "first", original, [], profile)
            self.assertTrue(created)
            task_id = registration["task_id"]

            reopened = Store(path)
            reopened.initialize()
            self.assertEqual(reopened.owner(token), owner)
            result = reopened.query_status(owner, task_id, "check")["data"]
            self.assertEqual(result["existing_result"], registration)
            self.assertEqual(result["task"]["original_request"], original)
            self.assertEqual(
                reopened.register_task(owner, "first", original, [], profile), (registration, False)
            )
