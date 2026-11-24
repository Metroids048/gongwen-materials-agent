import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from docx import Document
from openpyxl import Workbook
from pydantic import ValidationError
from pypdf import PdfWriter

from app.agent import main, run_task
from app.materials import collect
from app.revision.deadline import revise_deadline
from app.revision.document import RevisionIssue, check_word_parts


class ExpandedInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_xlsx_anchors_hide_formula_values_and_hidden_data(self):
        path = self.root / "公开统计.xlsx"
        book = Workbook()
        visible = book.active
        visible.title = "可见数据"
        visible.append(["项目", "办件量（件）", "办结率", "隐藏栏"])
        visible.append(["现场", 20, 0.825, 733])
        visible["C2"].number_format = "0.0%"
        visible["B3"] = "=20+7000"
        visible["A4"] = "隐藏行"
        visible.row_dimensions[4].hidden = True
        visible.column_dimensions["D"].hidden = True
        book.create_sheet("仅内部").sheet_state = "hidden"
        book["仅内部"]["A1"] = 999
        book.save(path)
        source = collect("写交流材料", [], [path])[1]
        self.assertEqual(source["blocks"], {
            "S1.W1.R1": "A: 项目 | B: 办件量（件） | C: 办结率",
            "S1.W1.R2": "A: 现场 | B: 20 | C: 82.5%",
        })
        self.assertTrue(any("W1.B3" in note for note in source["intake_notes"]))

    def test_pdf_page_without_text_does_not_reach_model(self):
        path = self.root / "无文字层.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.write(path)
        with self.assertRaisesRegex(RevisionIssue, "第 1 页无可用文字层"):
            run_task("fixture", "写交流材料", files=[path],
                     generate=lambda *_: self.fail("不能把不完整的材料当作已读取"))

    def test_unknown_word_scope_is_exhaustive_and_patches_only_approved_targets(self):
        path = self.root / "未见过的原件.docx"
        document = Document()
        paragraph = document.add_paragraph("全市落实：2026年")
        paragraph.add_run("年底").bold = True
        paragraph.add_run("前")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "省级目标：2026年年底前"
        table.cell(0, 1).text = "市级时限：2026年12月底前"
        document.sections[0].header.paragraphs[0].text = "原有版头"
        document.save(path)
        request = "把全市落实时间从2026年年底提前到2026年9月底，市级时限一起改，省级目标不动。"
        def select(_, prompt, scope_type):
            candidates = json.loads(prompt.split("\n", 1)[1])["全部日期候选位置"]
            self.assertEqual(candidates, {
                "position_1": {"P1": "全市落实：2026年年底前"},
                "position_2": {"T1.R1.C1": "省级目标：2026年年底前"},
                "position_3": {"T1.R1.C2": "市级时限：2026年12月底前"},
            })
            decisions = {field: {"action": action, "reason": "fixture"} for field, action in
                         zip(candidates, ["replace", "preserve", "replace"])}
            with self.assertRaises(ValidationError):
                scope_type.model_validate({"decisions": {"position_1": decisions["position_1"]}, "questions": []})
            return scope_type.model_validate({"decisions": decisions, "questions": []}), {}

        def unclear(*args):
            scope, metadata = select(*args)
            scope.decisions.position_3.action = "unclear"
            return scope, metadata

        held = self.root / "held"
        self.assertFalse(revise_deadline("fixture", request, path, held, generate=unclear))
        self.assertFalse((held / "正文.docx").exists())
        changed = self.root / "changed"
        self.assertTrue(revise_deadline("fixture", request, path, changed, generate=select))
        word = changed / "正文.docx"
        after = Document(word)
        self.assertEqual(after.paragraphs[0].text, "全市落实：2026年9月底前")
        self.assertEqual(after.tables[0].cell(0, 0).text, "省级目标：2026年年底前")
        self.assertEqual(after.tables[0].cell(0, 1).text, "市级时限：2026年9月底前")
        check_word_parts(path.read_bytes(), word.read_bytes())
        receipt = json.loads((changed / "运行回执.json").read_text())
        self.assertEqual(receipt["parent_revision_id"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(receipt["changed_cells"], ["T1.R1.C2"])

    def test_same_draft_changes_only_confirmed_reporting_time_in_body_and_table(self):
        parent = self.root / "first"
        parent.mkdir()
        path = parent / "正文.docx"
        document = Document()
        document.add_paragraph("切换工作方案（会商稿）")
        document.add_paragraph("次日10:00前，服务组汇总遗留问题给办公室；办公室连续跟踪三个工作日。")
        document.add_paragraph("次日10:00前，技术组交运行情况。")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "工作事项", "时限"
        table.cell(1, 0).text, table.cell(1, 1).text = "服务组报送遗留问题", "次日10:00前"
        document.save(path)
        parent_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        (parent / "运行回执.json").write_text(json.dumps({
            "status": "draft", "artifacts": {path.name: parent_hash},
        }))
        request = ("会商后确认，服务组把遗留问题报给办公室的时间从次日10:00前改为次日11:00前。"
                   "请只改上一版正文和任务分工表里的这两处，其他文字和表格都照旧。请给我改好的完整 Word。")
        def select(_, prompt, scope_type):
            candidates = json.loads(prompt.split("\n", 1)[1])["全部日期候选位置"]
            decisions = {field: {"action": "preserve" if "P3" in item else "replace", "reason": "对应本轮要求"}
                         for field, item in candidates.items()}
            return scope_type.model_validate({"decisions": decisions, "questions": []}), {}

        output = self.root / "revised"
        with patch("app.revision.deadline.ask_codex", side_effect=select):
            self.assertEqual(main([request, "--codex-model", "fixture", "--continue-from", str(parent),
                                   "--output", str(output)]), 0)
        after_path = output / path.name
        after = Document(after_path)
        self.assertIn("次日11:00前", after.paragraphs[1].text)
        self.assertEqual(after.paragraphs[2].text, "次日10:00前，技术组交运行情况。")
        self.assertEqual(after.tables[0].cell(1, 1).text, "次日11:00前")
        check_word_parts(path.read_bytes(), after_path.read_bytes())
        self.assertEqual(json.loads((output / "运行回执.json").read_text())["previous_revision_id"], parent_hash)

    def test_non_date_revision_uses_the_word_from_the_previous_deadline_revision(self):
        parent = self.root / "revised"
        parent.mkdir()
        path = parent / "正文.docx"
        word = Document()
        word.add_paragraph("服务组次日11:00前报送遗留问题。")
        word.add_paragraph("试点原话：“拟安排两名夜间值守，但纪要没有定人。”")
        table = word.add_table(rows=2, cols=3)
        table.rows[0].cells[0].text = "议题"
        table.rows[0].cells[1].text = "材料依据"
        table.rows[0].cells[2].text = "议定结果"
        table.rows[1].cells[0].text = "夜间值守人选"
        table.rows[1].cells[1].text = "拟安排两名夜间值守，但纪要没有定人。"
        table.rows[1].cells[2].text = "会上确定夜间值守人选"
        word.save(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        (parent / "运行回执.json").write_text(json.dumps({
            "status": "revision", "profile": "ordinary-word-deadline",
            "revision_id": digest, "artifacts": {path.name: digest},
        }))
        request = ("夜间值守继续放在待议事项里，改成由办公室在会上提出备选安排，会上再定，不提前定人数。"
                   "上一轮改好的报送时间次日11:00前和其他内容照旧。请给我改好的完整 Word。")
        updated = "由办公室在会上提出备选安排，会上再定，不提前定人数"

        def select(_, prompt, response_type):
            return response_type.model_validate({"edits": [{
                "location": "T1.R2.C3", "before": "会上确定夜间值守人选", "after": updated,
            }], "question": ""}), {}

        output = self.root / "revised-again"
        with patch("app.revision.deadline.ask_codex", side_effect=select):
            self.assertEqual(main([request, "--codex-model", "fixture", "--continue-from", str(parent),
                                   "--output", str(output)]), 0)
        after_path = output / path.name
        amended = Document(after_path)
        self.assertEqual(amended.paragraphs[0].text, word.paragraphs[0].text)
        self.assertEqual(amended.paragraphs[1].text, word.paragraphs[1].text)
        self.assertEqual(amended.tables[0].cell(1, 1).text, table.cell(1, 1).text)
        self.assertEqual(amended.tables[0].cell(1, 2).text, updated)
        check_word_parts(path.read_bytes(), after_path.read_bytes())
        self.assertEqual(json.loads((output / "运行回执.json").read_text())["previous_revision_id"], digest)
