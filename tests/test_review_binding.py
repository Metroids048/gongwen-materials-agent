import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.agent import Candidate, ConferenceReview, _conference_review


class ReviewBindingTests(unittest.TestCase):

    def test_rejected_review_is_retained_without_weakening_validation(self):
        from app.agent import Section, Passage
        candidate = Candidate(profile="notice-draft", material_uses=[], questions=[],
                              title="通知", sections=[Section(title="事项", paragraphs=[
                                  Passage(text="培训安排", kind="fact", anchors=["U.1"])])],
                              tasks=[], review_notes=[])

        def generate(_model, _prompt, schema):
            with self.assertRaises(ValidationError):
                schema(basis_key="wrong-parent", checked_ids=["P1.1"], findings=[])
            return ConferenceReview(basis_key="wrong-parent", checked_ids=["P1.1"], findings=[]), {}

        actual, report, _ = _conference_review("fixture", "培训安排", candidate, {}, {}, generate)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["error"]["code"], "REVIEW_BINDING_INVALID")
        self.assertEqual(report["review"]["basis_key"], "wrong-parent")
        self.assertIs(actual, candidate)
