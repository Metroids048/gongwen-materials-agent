import csv
import base64
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from fastapi import Request

from app.agent import main
from app.revision.document import RevisionIssue, open_docx, table_rows
from app.revision.native_review import review
from app.main import COOKIE_NAME, OpinionFile, OpinionIntake, create_app
from app.runtime.store import Store


class NativeReviewTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.source = self.folder / "审阅稿.docx"
        doc = Document()
        doc.add_paragraph("不要动这个职责。")
        doc.sections[0].header.paragraphs[0].text = "原页眉"
        cell = doc.add_table(1, 1).cell(0, 0).paragraphs[0]
        obsolete = cell.add_run("旧条款")
        new = cell.add_run("新条款")
        new.bold = True
        doc.add_comment(obsolete, "此条已在附件明确，删去。")
        for kind, number, run in (("del", "17", obsolete), ("ins", "18", new)):
            wrapper = OxmlElement("w:" + kind)
            wrapper.set(qn("w:id"), number)
            run._r.addprevious(wrapper)
            wrapper.append(run._r)
            if kind == "del":
                run._r.find(qn("w:t")).tag = qn("w:delText")
        props = OxmlElement("w:pPrChange")
        props.set(qn("w:id"), "19")
        props.append(OxmlElement("w:pPr"))
        cell._p.get_or_add_pPr().append(props)
        doc.save(self.source)

    def run_entry(self, request, name):
        out = self.folder / name
        stdout = StringIO()
        with patch("app.runtime.codex.subprocess.run", side_effect=AssertionError("审阅材料应留在本地")), redirect_stdout(stdout):
            self.assertEqual(main([request, "--file", str(self.source), "--output", str(out),
                                   "--codex-model", "fixture"]), 0)
        with (out / "逐条核稿表.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        receipt = json.loads((out / "运行回执.json").read_text())
        self.assertIn(receipt["message"], stdout.getvalue())
        self.assertIn(receipt["message"], (out / "核稿单.html").read_text())
        return out, rows, receipt

    def test_accept_does_not_lose_comment_or_table_format(self):
        out, rows, receipt = self.run_entry(
            "请接受这份 Word 里已有的全部正文文字修订，保留批注和格式修订，给我改后的 Word 和逐条核稿单。", "accept")
        changed = out / "正文.docx"
        doc = Document(changed)
        self.assertEqual(doc.paragraphs[0].text, "不要动这个职责。")
        p = doc.tables[0].cell(0, 0).paragraphs[0]
        self.assertEqual(p.text, "新条款")
        self.assertTrue(p.runs[-1].bold)
        self.assertEqual(len(p._p.xpath(".//w:commentReference")), 1)
        self.assertEqual(len(p._p.xpath(".//w:pPrChange")), 1)
        self.assertEqual(len(p._p.xpath(".//w:ins|.//w:del")), 0)
        self.assertEqual((out / "本轮父版.docx").read_bytes(), self.source.read_bytes())
        with ZipFile(self.source) as original, ZipFile(changed) as revised:
            self.assertEqual([name for name in original.namelist() if original.read(name) != revised.read(name)],
                             ["word/document.xml"])
        self.assertEqual(rows[0]["改前"], "旧条款")
        self.assertEqual(rows[0]["改后"], "")
        self.assertIn("第 1 张表，第 1 行第 1 格", rows[0]["位置"])
        self.assertIn("已按原修订删除", rows[0]["处理结果"])
        self.assertEqual((receipt["applied"], receipt["body_pending_marks"], receipt["comments_target_removed"]),
                         (2, 1, 1))
        self.assertIn("正文已改", receipt["message"])

    def test_natural_request_without_comment_keyword_still_uses_native_review(self):
        _, _, receipt = self.run_entry("麻烦把文档中所有正文文字修订都接受。", "natural")
        self.assertEqual(receipt["applied"], 2)

    def test_negative_accept_request_produces_only_a_report(self):
        out, rows, receipt = self.run_entry("不要接受全部正文文字修订，只列出批注。", "check")
        self.assertEqual((out / "正文.docx").read_bytes(), self.source.read_bytes())
        self.assertEqual((rows[0]["改前"], rows[0]["改后"]), ("旧条款", "旧条款"))
        self.assertEqual(receipt["status"], "review_only")
        self.assertIn("正文未改", receipt["message"])

    def test_web_review_delivers_word_findings_and_blocks_internal_downloads(self):
        db = self.folder / 'web.sqlite3'
        store = Store(db); store.initialize()
        token = store.create_session()
        request = Request({'type': 'http', 'headers': [(b'cookie', f'{COOKIE_NAME}={token}'.encode())]})
        app = create_app(db)
        submit = next(r.endpoint for r in app.routes if r.path == '/api/opinion-tasks')
        download = next(r.endpoint for r in app.routes if r.path == '/api/tasks/{task_id}/opinion/{name}')
        result = submit(request, OpinionIntake(request_id='word-only-review',
            original_request='只核对批注，不改正文。', public_trial=True, native_review=True,
            files=[OpinionFile(name=self.source.name,
                content_base64=base64.b64encode(self.source.read_bytes()).decode())]))
        self.assertEqual(result['state'], 'review_only')
        self.assertEqual(result['files'], ['核稿意见.docx'])
        self.assertEqual(result['message'], '核稿意见整理好了，原稿没有改动。')
        response = download(request, result['task_id'], '核稿意见.docx')
        self.assertIn('此条已在附件明确', '\n'.join(p.text for p in Document(response.path).paragraphs))
        for name in ('正文.docx', '本轮父版.docx', '运行回执.json', '逐条核稿表.csv', '核稿单.html'):
            self.assertEqual(download(request, result['task_id'], name).status_code, 404)

    def test_conflicting_or_broader_request_never_mutates_word(self):
        for index, request in enumerate((
            "请接受全部正文文字修订，先不要改 Word。",
            "请接受全部正文文字修订，同时接受格式修订。",
            "请接受全部修订，保留批注。",
            "「请接受全部正文文字修订」，只需要核稿单。",
        )):
            with self.subTest(request=request):
                out, _, receipt = self.run_entry(request, f"blocked-{index}")
                self.assertEqual((out / "正文.docx").read_bytes(), self.source.read_bytes())
                self.assertIn("正文未改", receipt["message"])

    def test_only_native_review_allows_a_stale_drive_local_template(self):
        targets = (
            (r"file:///C:\Work\old.dot", True),
            ("file://server/share/old.dot", False),
            ("https://example.org/old.dot", False),
        )
        for index, (target, local) in enumerate(targets):
            with self.subTest(target=target):
                doc = Document(self.source)
                settings = doc.part.part_related_by(RT.SETTINGS)
                link = OxmlElement("w:attachedTemplate")
                relationship = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate"
                link.set(qn("r:id"), settings.relate_to(target, relationship, is_external=True))
                doc.settings.element.append(link)
                source = self.folder / f"template-link-{index}.docx"
                doc.save(source)
                with self.assertRaisesRegex(RevisionIssue, "无外部链接"):
                    open_docx(source.read_bytes())
                out = self.folder / f"link-output-{index}"
                if local:
                    review("只核对批注，不改正文。", source, out)
                    self.assertEqual((out / "正文.docx").read_bytes(), source.read_bytes())
                else:
                    with self.assertRaisesRegex(RevisionIssue, "无外部链接"):
                        review("只核对批注，不改正文。", source, out)
                    self.assertFalse(out.exists())
