"""Regression from the real 1.0.9 draft that omitted its requested issuer."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH

from app.agent import Candidate, Run, run_task, write_result
from app.materials import collect
from app.revision.document import RevisionIssue


class ExplicitPlanIssuerTests(unittest.TestCase):
    def fixture(self):
        data = json.loads((Path(__file__).parent / 'fixtures/plan_missing_explicit_issuer.json').read_text())
        return data['original_request'], Candidate.model_validate(data['candidate'])

    def test_actual_omission_is_completed_from_the_original_request(self):
        request, candidate = self.fixture()
        for instruction in ('落款写幸福社区', '落款用“幸福社区”', '落款单位：幸福社区'):
            with self.subTest(instruction=instruction):
                current = request.replace('落款写幸福社区', instruction)
                run = run_task('fixture', current, generate=lambda *_: (candidate.model_copy(deep=True), {}))
                self.assertEqual(run.status, 'draft', run.note)
                with TemporaryDirectory() as folder:
                    output = Path(folder) / 'word'
                    write_result(run, output)
                    word = Document(output / '正文.docx')
                    issuer = next(p for p in word.paragraphs if p.text == '幸福社区')
                    self.assertEqual(issuer.alignment, WD_ALIGN_PARAGRAPH.RIGHT)
                    self.assertEqual(sum(p.text == '幸福社区' for p in word.paragraphs), 1)
                    self.assertNotIn('落款', [p.text for p in word.paragraphs])
                    self.assertEqual(len(word.tables[-1].rows), 4)
                    self.assertNotRegex((output / '正文.txt').read_text(), r'\d{4}年\d+月\d+日')

    def test_direct_export_cannot_bypass_the_requested_issuer(self):
        request, candidate = self.fixture()
        with TemporaryDirectory() as folder:
            with self.assertRaises(RevisionIssue):
                write_result(Run('draft', candidate, collect(request, (), ()), {}, {'U.1': request}),
                             Path(folder) / 'word')
