import hashlib
import json
import unittest

from pydantic import ValidationError

from app.agent import ExpertBrief, _expert_brief


class BriefBindingTests(unittest.TestCase):
    def setUp(self):
        self.materials = [{"id": "U", "blocks": {"U.1": "培训通知"}},
                          {"id": "S1", "blocks": {"S1.L1": "11月20日上午培训"}}]
        self.text = json.dumps(self.materials, ensure_ascii=False)
        self.key = hashlib.sha256(self.text.encode()).hexdigest()
        self.valid = {"input_key": self.key, "items": [
            {"text": "11月20日上午培训", "status": "known", "anchors": ["S1.L1"]}],
            "outline": ["培训安排", "报名事项"]}

    def test_model_contract_binds_current_key_anchors_and_existing_limits(self):
        def generate(_model, prompt, schema):
            self.assertTrue(issubclass(schema, ExpertBrief))
            self.assertIn("U.1", prompt)
            self.assertIn("S1.L1", prompt)
            for update in ({"input_key": "previous-input"}, {"items": []},
                           {"items": self.valid["items"] * 17}, {"outline": ["仅一章"]},
                           {"outline": ["章节"] * 7}, {"outline": ["培训", "  "]},
                           {"items": [{**self.valid["items"][0], "anchors": ["S9.L1"]}]},
                           {"items": [{**self.valid["items"][0], "anchors": []}]},
                           {"items": [{**self.valid["items"][0], "text": "  "}]}):
                with self.subTest(update=update), self.assertRaises(ValidationError):
                    schema.model_validate({**self.valid, **update})
            return schema.model_validate(self.valid), {"upstream_call_id": "fixture-brief"}

        writer_view, record = _expert_brief("fixture", self.text, self.materials, generate)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["brief"], self.valid)
        self.assertNotIn("input_key", json.loads(writer_view))

    def test_host_still_rejects_and_retains_invalid_model_result(self):
        invalid = {**self.valid, "input_key": "previous-input", "outline": ["仅一章"],
                   "items": [{**self.valid["items"][0], "anchors": ["S9.L1"]}]}

        def generate(*_args):
            # Simulate an untrusted bridge returning a base model despite the bound schema.
            return ExpertBrief.model_validate(invalid), {"upstream_call_id": "fixture-rejected"}

        writer_view, record = _expert_brief("fixture", self.text, self.materials, generate)
        self.assertEqual(writer_view, "")
        self.assertEqual(record["status"], "incomplete")
        self.assertEqual(record["brief"], invalid)
        self.assertEqual(record["error"]["code"], "BRIEF_BINDING_INVALID")
        self.assertEqual(record["invalid_fields"], ["input_key", "outline_count", "items[0].anchors"])

    def test_transport_rejection_is_kept_in_private_stage_receipt(self):
        from app.revision.document import RevisionIssue
        rejected = {"content": "{}", "model": {"upstream_call_id": "fixture-wire"},
                    "validation_errors": [{"loc": ["items"], "type": "missing"}]}

        def generate(*_args):
            error = RevisionIssue("MODEL_INVALID", "交换格式无效")
            error.rejected_exchange = rejected
            raise error

        writer_view, record = _expert_brief("fixture", self.text, self.materials, generate)
        self.assertEqual(writer_view, "")
        self.assertEqual(record["status"], "incomplete")
        self.assertEqual(record["rejected_exchange"], rejected)
        self.assertEqual(record["error"]["code"], "MODEL_INVALID")
