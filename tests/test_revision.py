"""Regression coverage for shared date rendering, lock enforcement and stale commits."""

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from app.revision.document import (
    TEMPLATE_VERSION, RevisionIssue, build_word, check_word_parts, open_docx, prepare_revision,
    table_rows, word_bytes,
)
from app.revision.layout import describe_layout
from app.revision.natural import import_manual
from app.runtime.store import RequestConflict, RevisionConflict, Store


class DateRevision(unittest.TestCase):
    def setUp(self):
        table_doc = Document()
        table_doc.add_paragraph("任务分工表")
        table = table_doc.add_table(rows=2, cols=3)
        for row, values in zip(table.rows, [
            ["序号", "具体措施", "完成时限"],
            ["1", "2025年9月底前力争完成试点。", "2025年底"],
        ]):
            for cell, value in zip(row.cells, values):
                cell.text = value
        self.rows = table_rows(table)
        self.body = ["工作方案", "一、重点任务", "到2025年9月底力争完成试点。",
                     "二、保障措施", "（一）组织领导", "已明确组织安排。", "（二）落实督查", "开展督查。"]
        self.parent = word_bytes(build_word(self.body, word_bytes(table_doc)))
        self.request = "把2025年9月底改到2025年10月底。锁定（二、保障措施）（一）组织领导。"
        self.selection = {
            "question": "", "old_date": "2025年9月底", "new_date": "2025年10月底",
            "lock_heading": "（一）组织领导", "body_paragraph": 3, "table_row": 2, "table_column": 2,
        }

    def test_shared_date_and_locked_word_render(self):
        word, view = prepare_revision(self.parent, self.body, self.rows, self.request, self.selection)
        doc = open_docx(word)
        self.assertEqual(doc.paragraphs[2].text, "到2025年10月底力争完成试点。")
        self.assertEqual([p.text for p in doc.paragraphs[4:6]], self.body[4:6])
        self.assertEqual(table_rows(doc.tables[0])[1], ["1", "2025年10月底前力争完成试点。", "2025年底"])
        self.assertEqual(len(view["changes"]), 2)

    def test_conflicting_locked_shared_date_blocks_word(self):
        self.body[5] += "2025年9月底。"
        with self.assertRaises(RevisionIssue) as raised:
            prepare_revision(self.parent, self.body, self.rows, self.request, self.selection)
        self.assertEqual(raised.exception.code, "LOCKED_SCOPE_CONFLICT")

    def test_date_revision_preserves_letterhead_styles_and_page_fields(self):
        doc = open_docx(self.parent)
        title = doc.paragraphs[0]
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        borders, bottom = OxmlElement("w:pBdr"), OxmlElement("w:bottom")
        bottom.set(qn("w:color"), "C00000")
        bottom.set(qn("w:val"), "single")
        borders.append(bottom)
        title._p.get_or_add_pPr().append(borders)
        doc.sections[0].header.paragraphs[0].add_run("某单位文件")
        field = OxmlElement("w:fldSimple")
        field.set(qn("w:instr"), "PAGE")
        doc.sections[0].footer.paragraphs[0]._p.append(field)
        parent = word_bytes(doc)
        word, _ = prepare_revision(parent, self.body, self.rows, self.request, self.selection)
        reread = open_docx(word)
        check_word_parts(parent, word)
        self.assertEqual(reread.paragraphs[0]._p.xml, title._p.xml)
        layout = describe_layout(reread, word)
        self.assertEqual(layout, describe_layout(open_docx(parent), parent))
        self.assertEqual(layout["explicit_red_line_parts"], ["word/document.xml"])
        self.assertEqual(layout["centered_nonempty_paragraphs"], [1])
        self.assertEqual((layout["header_parts"], layout["footer_parts"]), (1, 1))

    def test_continuation_rejects_hand_edits_to_the_locked_text(self):
        word, view = prepare_revision(self.parent, self.body, self.rows, self.request, self.selection)
        doc = open_docx(word)
        doc.paragraphs[5].runs[0].text = "更改已锁定的正文。"
        previous = {"original_request": self.request, "view": view}
        with self.assertRaises(RevisionIssue) as raised:
            import_manual(word, word_bytes(doc), self.body, previous)
        self.assertEqual(raised.exception.code, "LOCKED_SCOPE_CONFLICT")

    def test_continuation_imports_one_text_edit_with_original_layout(self):
        word, view = prepare_revision(self.parent, self.body, self.rows, self.request, self.selection)
        previous = {"original_request": self.request, "view": view}
        supplied = open_docx(word)
        paragraph = supplied.paragraphs[7]
        paragraph.runs[0].text = "每月开展"
        paragraph.add_run("督查。").bold = True
        supplied.styles["Normal"].font.name = "Arial"
        parent, changes = import_manual(word, word_bytes(supplied), self.body, previous)
        check_word_parts(word, parent)
        self.assertEqual(changes[0]["after"], "每月开展督查。")
        self.assertEqual(len(open_docx(parent).paragraphs[7].runs), 1)

        supplied.tables[0].cell(1, 2).text = "2026年底"
        with self.assertRaises(RevisionIssue) as raised:
            import_manual(word, word_bytes(supplied), self.body, previous)
        self.assertEqual(raised.exception.code, "EDIT_SCOPE_CONFLICT")

    def test_manual_revision_blocks_stale_commit_and_exports_exact_current_word(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "tasks.sqlite")
            store.initialize()
            owner = store.owner(store.create_session())
            other = store.owner(store.create_session())
            profile = {"id": "plan-revise", "document_type": "work_plan", "mode": "revise"}
            metadata = [{"role": "body", "name": "body.txt", "content_hash": "synthetic"}]
            registered, _ = store.register_task(owner, "register", self.request, metadata, profile,
                                                sources=["\n\n".join(self.body).encode()], word=self.parent)
            task_id = registered["task_id"]

            def task():
                return store.query_status(owner, task_id, "lookup")["data"]["task"]

            parent = task()["current_revision"]
            store.begin_revision(owner, task_id, "generate", parent, 1, "rules")
            self.assertFalse(store.begin_revision(owner, task_id, "generate", parent, 1, "rules"))
            with self.assertRaises(RequestConflict):
                store.begin_revision(owner, task_id, "generate", "different-parent", 1, "rules")
            with self.assertRaises(RequestConflict):
                store.begin_revision(owner, task_id, "second", parent, 1, "rules")
            candidate_word, view = prepare_revision(self.parent, self.body, self.rows, self.request, self.selection)
            store.finish_revision(task_id, "generate", word=candidate_word, view=view)
            candidate = task()["revision_work"]["candidate"]
            revision, review, basis = candidate["revision_id"], candidate["review_id"], task()["basis_key"]
            with self.assertRaises(RevisionConflict):
                store.export_revision(owner, task_id, revision, TEMPLATE_VERSION, "rules")
            with self.assertRaises(RevisionConflict):
                store.commit_candidate(other, task_id, revision, parent, review, 1, basis, "rules")
            with self.assertRaises(RevisionIssue) as locked:
                store.apply_user_edit(owner, task_id, "locked", parent, [{"paragraph": 6, "text": "越权修改。"}])
            self.assertEqual(locked.exception.code, "LOCKED_SCOPE_CONFLICT")
            self.assertEqual(task()["current_revision"], parent)
            edits = [{"paragraph": 8, "text": "每月开展督查。"}]
            with self.assertRaises(RevisionConflict):
                store.apply_user_edit(other, task_id, "edit", parent, edits)
            edit = store.apply_user_edit(owner, task_id, "edit", parent, edits)
            manual = edit["revision_id"]
            self.assertEqual(edit["invalidated_reviews"], [review])
            self.assertEqual(store.apply_user_edit(owner, task_id, "edit", parent, edits), edit)
            with self.assertRaises(RequestConflict):
                store.apply_user_edit(owner, task_id, "edit", parent, [{"paragraph": 8, "text": "另一次修改。"}])
            with self.assertRaises(RevisionConflict):
                store.apply_user_edit(owner, task_id, "stale-edit", parent, edits)
            with self.assertRaises(RevisionConflict):
                store.commit_candidate(owner, task_id, revision, parent, review, 1, basis, "rules")
            self.assertEqual(task()["current_revision"], manual)
            self.assertEqual(task()["quality_state"], "unreviewed")
            self.assertEqual(task()["revision_work"]["candidate"]["revision_id"], revision)

            with self.assertRaises(RevisionConflict):
                store.export_revision(owner, task_id, revision, TEMPLATE_VERSION, "rules", allow_draft=True)
            with self.assertRaises(RevisionConflict):
                store.export_revision(other, task_id, manual, TEMPLATE_VERSION, "rules", allow_draft=True)
            with self.assertRaises(RevisionConflict):
                store.export_revision(owner, task_id, manual, TEMPLATE_VERSION, "rules")
            word, artifact = store.export_revision(owner, task_id, manual, TEMPLATE_VERSION, "rules", allow_draft=True)
            doc = open_docx(word)
            self.assertEqual(doc.paragraphs[7].text, edits[0]["text"])
            self.assertEqual(doc.paragraphs[5]._p.xml, open_docx(self.parent).paragraphs[5]._p.xml)
            self.assertEqual(table_rows(doc.tables[0]), self.rows)
            self.assertEqual(artifact["revision_id"], task()["current_revision"])
            self.assertEqual(store.export_revision(owner, task_id, manual, TEMPLATE_VERSION, "rules", allow_draft=True),
                             (word, artifact))

            claim = store.begin_revision(owner, task_id, "regenerate", manual, 1, "rules")
            self.assertTrue(claim)
            self.assertFalse(store.begin_revision(owner, task_id, "generate", parent, 1, "rules"))
            original, _, base_word = claim
            self.assertEqual(base_word, word)
            new_word, new_view = prepare_revision(
                base_word, [p.text for p in doc.paragraphs[:len(self.body)]], table_rows(doc.tables[0]),
                original, self.selection,
            )
            store.finish_revision(task_id, "regenerate", word=new_word, view=new_view)
            self.assertEqual(task()["current_revision"], manual)
            new_candidate = task()["revision_work"]["candidate"]
            self.assertEqual(new_candidate["parent_revision"], manual)
            with self.assertRaises(RevisionConflict):
                store.commit_candidate(owner, task_id, revision, parent, review, 1, basis, "rules")
            adopted = new_candidate["revision_id"]
            store.commit_candidate(owner, task_id, adopted, manual, new_candidate["review_id"], 1,
                                   task()["basis_key"], "rules")
            downloaded, export = store.export_revision(owner, task_id, adopted, TEMPLATE_VERSION, "rules")
            self.assertEqual(downloaded, new_word)
            self.assertEqual(export["revision_id"], task()["current_revision"])
            adopted_doc = open_docx(downloaded)
            self.assertEqual(adopted_doc.paragraphs[7].text, edits[0]["text"])
            self.assertIn("2025年10月底", adopted_doc.paragraphs[2].text)
            self.assertIn("2025年10月底", table_rows(adopted_doc.tables[0])[1][1])
            self.assertEqual(adopted_doc.paragraphs[5]._p.xml, doc.paragraphs[5]._p.xml)
            next_edit = store.apply_user_edit(owner, task_id, "after-adoption", adopted,
                                              [{"paragraph": 8, "text": "每周开展督查。"}])
            self.assertFalse(task()["revision_work"]["can_generate"])
            with self.assertRaises(RequestConflict):
                store.begin_revision(owner, task_id, "already-done", next_edit["revision_id"], 1, "rules")
