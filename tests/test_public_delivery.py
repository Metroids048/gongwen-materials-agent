"""Consumer delivery must not export private audit and continuation artifacts."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from docx import Document

ENTRY = Path(__file__).resolve().parents[1] / 'deploy/openagent/20261112/overlay/openagent_entry.py'
if not ENTRY.is_file():
    ENTRY = Path(__file__).resolve().parents[1] / 'openagent_entry.py'
spec = importlib.util.spec_from_file_location('public_entry', ENTRY)
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class PublicDelivery(unittest.TestCase):
    def deliver(self, folder, **extra):
        receipt = {'status': 'draft', 'candidate': {'title': '社区消防排查工作安排'},
                   'artifacts': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in folder.iterdir() if p.is_file()}}
        receipt.update(extra)
        (folder / '运行回执.json').write_text(json.dumps(receipt, ensure_ascii=False))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            entry.deliver(folder)
        lines = out.getvalue().splitlines()
        return json.loads(lines[0]), [Path(x[6:]) for x in lines if x.startswith('MEDIA:')]

    def test_one_named_word_with_private_files_retained_and_cache_redelivery(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            word = folder / '正文.docx'
            doc = Document(); doc.add_paragraph('社区消防排查工作安排'); doc.add_table(rows=2, cols=6); doc.save(word)
            for name in ('正文.txt', '依据与待核.md', '变化对照.md', '逐条核稿表.csv', '核稿单.html'):
                (folder / name).write_text('内部记录')
            Document().save(folder / '本轮父版.docx')
            before = {p.name: p.read_bytes() for p in folder.iterdir()}
            header, files = self.deliver(folder)
            self.assertEqual([p.name for p in files], ['社区消防排查工作安排.docx'])
            self.assertEqual(files[0].read_bytes(), before['正文.docx'])
            self.assertEqual(header['delivery_message'], 'Word写好了，可以直接修改。')
            self.assertEqual(header['word_contents']['table_count'], 1)
            for name, raw in before.items():
                self.assertEqual((folder / name).read_bytes(), raw)
            out = io.StringIO()
            with contextlib.redirect_stdout(out): entry.deliver(folder)
            self.assertEqual(out.getvalue().count('MEDIA:'), 1)

    def test_concrete_pending_note_in_chat_without_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); Document().save(folder / '正文.docx')
            header, files = self.deliver(folder, conference_review={'review': {'findings': [
                {'level': 'pending', 'defect': '会议地点尚未确定。', 'target_ids': ['P1']}]}})
            self.assertIn('会议地点尚未确定', header['delivery_message'])
            self.assertNotIn('依据与待核', header['delivery_message'])
            self.assertEqual(len(files), 1)

    def test_failed_result_does_not_export_stray_word_or_internal_files(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); Document().save(folder / '正文.docx')
            header, files = self.deliver(folder, status='needs_review', message='内部校验未通过。')
            self.assertEqual(files, [])
            self.assertNotIn('写好了', header['delivery_message'])

    def test_revision_without_primary_word_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            Document().save(folder / '改稿短表.docx')
            header, files = self.deliver(folder, status='revision', message='Word改好了，可以直接修改。',
                                         original_request='按意见改正文，并给我改稿短表')
            self.assertEqual(files, [folder / '改稿短表.docx'])
            self.assertIsNone(header['word_contents'])
            self.assertNotIn('改好了', header['delivery_message'])

    def test_title_cannot_escape_output_or_change_extension(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); Document().save(folder / '正文.docx')
            _, files = self.deliver(folder, candidate={'title': '../../安排:表格.txt'})
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].suffix, '.docx')
            self.assertTrue(files[0].resolve().is_relative_to(folder.resolve()))

    def test_review_only_keeps_requested_findings_as_word_without_sending_original(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); Document().save(folder / '正文.docx')
            (folder / '逐条核稿表.csv').write_text('编号,位置,核稿说明\n1,第2段,会议地点尚未确定\n')
            header, files = self.deliver(folder, status='review_only')
            self.assertEqual([p.name for p in files], ['核稿意见.docx'])
            self.assertIn('原稿没有改动', header['delivery_message'])
            self.assertIn('会议地点尚未确定', '\n'.join(p.text for p in Document(files[0]).paragraphs))

    def test_repaired_findings_are_not_repeated_as_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); Document().save(folder / '正文.docx')
            header, _ = self.deliver(folder, conference_review={'status': 'changes_applied',
                'unaddressed_targets': [], 'review': {'findings': [
                    {'level': 'blocking', 'defect': '旧稿缺落款', 'target_ids': ['P1']}]}})
            self.assertEqual(header['delivery_message'], 'Word写好了，可以直接修改。')

    def test_changed_public_copy_is_rejected_on_cached_redelivery(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); Document().save(folder / '正文.docx')
            _, files = self.deliver(folder)
            files[0].write_bytes(b'changed')
            with self.assertRaises(ValueError), contextlib.redirect_stdout(io.StringIO()):
                entry.deliver(folder)
