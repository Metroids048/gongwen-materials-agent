"""Regression from the actual 1.0.10 draft notice failure."""
import copy
import tempfile
import unittest
from pathlib import Path
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from app.agent import Candidate, numeric_tokens, run_task, write_result

CANDIDATE = {'profile': 'notice-draft', 'material_uses': [{'source_id': 'U', 'role': 'mixed', 'usable_anchors': ['U.1']}], 'questions': [], 'title': '关于开展垃圾分类培训的通知', 'sections': [{'title': '', 'paragraphs': [{'text': '各网格员、物业负责人：', 'kind': 'fact', 'anchors': ['U.1']}, {'text': '定于11月25日14:00在社区会议室开展垃圾分类培训，请网格员和物业负责人按时参加。', 'kind': 'fact', 'anchors': ['U.1']}]}, {'title': '', 'paragraphs': [{'text': '幸福社区', 'kind': 'fact', 'anchors': ['U.1']}, {'text': '', 'kind': 'fact', 'anchors': ['U.1']}]}], 'tasks': [], 'review_notes': ['未提供报名渠道和回执接收方，正文未设置报名或回执要求；如需报名，需另行补充。', '成文日期按要求留空。']}
REQUEST = '帮我写个垃圾分类培训通知，11月25日下午两点，在社区会议室开，网格员和物业负责人来参加，落款写幸福社区，日期先空着。给我Word就行。'

class BlankNoticeDateTests(unittest.TestCase):
    def run_candidate(self, request=REQUEST, data=None):
        candidate=Candidate.model_validate(copy.deepcopy(data or CANDIDATE))
        return run_task('fixture', request, generate=lambda *_: (candidate, {}))

    def test_actual_blank_date_delivers_notice_with_aligned_empty_date(self):
        result=self.run_candidate()
        self.assertEqual(result.status, 'draft', result.note)
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'out';write_result(result,out)
            doc=Document(out/'正文.docx')
            for text in ('幸福社区','成文日期：'):
                p=next(p for p in doc.paragraphs if p.text==text)
                self.assertEqual(p.alignment, WD_ALIGN_PARAGRAPH.RIGHT)
            self.assertTrue(any('11月25日14:00' in p.text for p in doc.paragraphs))
            self.assertEqual(len(doc.tables),0)

    def test_unspecified_signing_date_stays_blank_without_blocking_word(self):
        data = copy.deepcopy(CANDIDATE)
        data['review_notes'] = []
        result = self.run_candidate(REQUEST.replace('日期先空着。', ''), data)
        self.assertEqual(result.status, 'draft', result.note)
        self.assertEqual(result.candidate.sections[-1].paragraphs[-1].text, '成文日期：')

    def test_empty_business_paragraph_still_fails(self):
        data=copy.deepcopy(CANDIDATE)
        data['sections'][0]['paragraphs'].append({'text':'','kind':'fact','anchors':['U.1']})
        self.assertEqual(self.run_candidate(data=data).status,'needs_review')

    def test_short_notice_followup_preserves_relative_dates_and_leaves_signing_date_open(self):
        request = ("帮我写个展会居家办公的通知书，发给青州的上班族，"
                   "会议开始前一天和结束后一天，发文单位是青州市政府。")
        calls = []
        result = run_task("fixture", request, generate=lambda *_: calls.append(True))
        self.assertEqual(result.status, "needs_input")
        self.assertFalse(calls)

        data = copy.deepcopy(CANDIDATE)
        data["title"] = "关于展会期间居家办公安排的通知"
        body = data["sections"][0]["paragraphs"][-1]
        body["text"] = "青州的上班族在会议开始前一天和结束后一天居家办公。"
        data["sections"][0]["paragraphs"][0]["text"] = "各有关单位："
        data["sections"][-1]["paragraphs"][0]["text"] = "青州市政府"
        data["review_notes"] = ["成文日期未提供，请补充。"]

        with tempfile.TemporaryDirectory() as temp:
            result = self.run_candidate(f"{request}\n20-23", data)
            self.assertEqual(result.status, "needs_input")
            self.assertIn("哪年哪月", result.candidate.questions[0])
            result = self.run_candidate(f"{request}\n20-23\n月份不填，先按审阅稿给 Word，"
                                        "正文保留会议开始前一天和结束后一天这两天。", data)
            self.assertEqual(result.status, "draft", result.note)
            out = Path(temp) / "out"
            write_result(result, out)
            document = Document(out / "正文.docx")
            self.assertIn(body["text"], [p.text for p in document.paragraphs])
            for text in ("青州市政府", "成文日期："):
                paragraph = next(p for p in document.paragraphs if p.text == text)
                self.assertEqual(paragraph.alignment, WD_ALIGN_PARAGRAPH.RIGHT)

        review_request = (f"{request}\n先按审阅稿给我 Word，月份不填，"
                          "正文保留会议开始前一天和结束后一天这两天，别换算成具体日期。")
        result = self.run_candidate(review_request, data)
        self.assertEqual(result.status, "draft", result.note)

        body["text"] = "青州的上班族在11月19日、11月24日居家办公。"
        self.assertEqual(self.run_candidate(review_request, data).status, "needs_review")

    def test_chinese_clock_preserves_hour_and_other_numbers(self):
        for source, target in (('下午两点', '14:00'), ('上午十点', '10:00'),
                               ('二十三时', '23:00'), ('零点', '0:00')):
            self.assertEqual(numeric_tokens(source), numeric_tokens(target))
        for different in ('2:00', '14:30', '14:00，30人'):
            self.assertNotEqual(numeric_tokens('下午两点'), numeric_tokens(different))
        self.assertNotEqual(numeric_tokens('两点半'), numeric_tokens('2:00'))
        self.assertEqual(numeric_tokens('上午一点开放'), {'时间01:00'})
        for phrase in ('同一时间', '同一时点', '同一时限', '同一时序', '这一点', '有一点', '差一点'):
            self.assertEqual(numeric_tokens(phrase), set(), phrase)
