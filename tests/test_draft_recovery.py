import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from docx import Document

from app.agent import Candidate, Passage, Section, event_window, run_task, write_result
from app.delivery import prepare_delivery
from app.revision.document import RevisionIssue


AMBIGUOUS_APEC_REQUEST = (
    "帮我写一下 apec居家办公的通知书\n"
    "发给深圳的上班族，apec正式会议开始前一天和结束后一天，发文单位是深圳市政府。"
)
APEC_REQUEST = AMBIGUOUS_APEC_REQUEST + "\n只安排前后各一天。"
OFFICIAL_SCHEDULE = {
    "url": "https://www.mfa.gov.cn/wjdt_674879/wsrc_674883/202611/t20261113_12058548.shtml",
    "quote": "亚太经合组织第三十三次领导人非正式会议将于11月18日至19日在深圳举行",
}
APEC_ARRANGEMENT = "拟于11月17日和20日安排居家办公，具体适用范围以审定稿为准。"


def notice(text=APEC_ARRANGEMENT, public=True, **changes):
    uses = [{"source_id": "U", "role": "mixed", "usable_anchors": ["U.1"]}]
    if public:
        uses.append({"source_id": "P", "role": "facts", "usable_anchors": ["P.1"]})
    return Candidate.model_validate({
        "profile": "notice-draft", "title": "关于 APEC 会议相关居家办公安排的通知（审阅稿）",
        "sections": [{"title": "", "paragraphs": [
            {"text": text, "kind": "fact", "anchors": ["U.1", "P.1"] if public else ["U.1"]},
            {"text": "深圳市政府", "kind": "fact", "anchors": ["U.1"]},
            {"text": "", "kind": "fact", "anchors": ["U.1"]},
        ]}], "tasks": [], "review_notes": [], "questions": [], "material_uses": uses, **changes,
    })


class DraftRecoveryTests(unittest.TestCase):
    def test_optional_empty_field_does_not_block_notice_with_a_body(self):
        request = ("请给一份消防培训通知的 Word 审阅稿。全体网格员到社区服务中心培训，"
                   "发文单位是南园社区服务中心。")
        body = "请全体网格员到社区服务中心参加消防培训。"
        for name, text, status in (("联系方式", body, "draft"),
                                   ("时间地点", body, "needs_review"),
                                   ("联系方式", None, "needs_review")):
            with self.subTest(name=name, has_body=bool(text)):
                def propose(*_):
                    paragraphs = [Passage(text="南园社区服务中心", kind="fact", anchors=["U.1"])]
                    if text:
                        paragraphs.insert(0, Passage(text=text, kind="fact", anchors=["U.1"]))
                    sections = [Section(title="", paragraphs=paragraphs), Section(title=name, paragraphs=[])]
                    return notice(public=False, title="消防培训通知", sections=sections), {}

                run = run_task("fixture", request, generate=propose)
                self.assertEqual(run.status, status, run.note)
                if status == "draft":
                    with TemporaryDirectory() as temp:
                        output = Path(temp) / "out"
                        write_result(run, output)
                        receipt = json.loads((output / "运行回执.json").read_text())
                        file, = prepare_delivery(output, receipt)["files"]
                        self.assertIn(body, [p.text for p in Document(file).paragraphs])

    def test_known_meeting_dates_do_not_resolve_the_user_s_work_arrangement(self):
        for schedule in (None, OFFICIAL_SCHEDULE):
            with self.subTest(schedule=schedule):
                writer = Mock(side_effect=AssertionError("a business choice is still missing"))
                run = run_task("fixture", AMBIGUOUS_APEC_REQUEST, generate=writer, public_schedule=schedule)
                self.assertEqual(run.status, "needs_input")
                question, = run.candidate.questions
                self.assertIn("各一天", question)
                self.assertIn("连续", question)
                self.assertEqual("请告知会期" in question, schedule is None)
                writer.assert_not_called()

    def test_lookup_evidence_follows_same_request_to_editable_word_without_expert_review(self):
        writer = Mock(return_value=(notice(), {}))
        regular_writer = Mock(side_effect=AssertionError("should use the quick notice writer"))
        reviewer = Mock(side_effect=AssertionError("should not start independent review"))
        run = run_task("fixture", APEC_REQUEST, generate=regular_writer, quick_notice_generate=writer,
                       review_generate=reviewer, expert_team=True, public_schedule=OFFICIAL_SCHEDULE)
        self.assertEqual(run.status, "draft", run.note)
        writer.assert_called_once()
        regular_writer.assert_not_called()
        reviewer.assert_not_called()
        self.assertIn("会前一天为11月17日，会后一天为11月20日", writer.call_args.args[1])
        with TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            write_result(run, output)
            receipt = json.loads((output / "运行回执.json").read_text())
            self.assertEqual(receipt["original_request"], APEC_REQUEST)
            self.assertEqual(receipt["cited_excerpts"]["P.1"], OFFICIAL_SCHEDULE["quote"])
            self.assertEqual(receipt["materials"][1]["source_url"], OFFICIAL_SCHEDULE["url"])
            delivery = prepare_delivery(output, receipt)
            self.assertEqual(len(delivery["files"]), 1)
            doc = Document(delivery["files"][0])
            self.assertIn(APEC_ARRANGEMENT, [paragraph.text for paragraph in doc.paragraphs])
            doc.add_paragraph("编辑复验")
            edited = Path(temp) / "edited.docx"
            doc.save(edited)
            self.assertEqual(Document(edited).paragraphs[-1].text, "编辑复验")

    def test_user_defined_schedule_overrides_external_schedule(self):
        request = APEC_REQUEST + "\n会期为11月16日至21日。"
        writer = Mock(return_value=(notice("拟于11月15日、11月22日居家办公。", public=False), {}))
        run = run_task("fixture", request, generate=writer, public_schedule=OFFICIAL_SCHEDULE)
        self.assertEqual(run.status, "draft", run.note)
        self.assertIn("会前一天为11月15日，会后一天为11月22日", writer.call_args.args[1])
        self.assertNotIn("P.1", run.excerpts)

    def test_same_conversation_date_reply_produces_word(self):
        request = (
            "帮我写一下 apec居家办公的通知书，发给深圳的上班族，"
            "apec正式会议开始前一天和结束后一天，发文单位是深圳市政府。\n"
            "是2026年11月16日至21日。按这个会期写一份复盘用的审阅稿，只安排会议开始前一天和结束后一天居家办公，"
            "两天分别执行。请给我 Word。"
        )
        text = "请深圳市上班族于APEC正式会议开始前一天和结束后一天，即2026年11月15日和11月22日，分别执行居家办公。"
        spaced_text = text.replace("2026年11月15日", "2026 年 11 月 15 日").replace("11月22日", "11 月 22 日")
        spaced_text += "办公共 2 天。"
        for original, drafted, source_id in ((request, text, "U"),
                                             (request.replace("\n", ""), spaced_text, "U"),
                                             (request, text, "U.1")):
            with self.subTest(original=original), TemporaryDirectory() as temp:
                title = "关于 APEC 会议相关居家办公安排的通知"
                candidate = notice(drafted, public=False, title=title)
                candidate.material_uses[0].source_id = source_id
                writer = Mock(return_value=(candidate, {}))
                run = run_task("fixture", original, generate=writer)
                self.assertEqual(run.status, "draft", run.note)
                writer.assert_called_once()
                output = Path(temp) / "out"
                write_result(run, output)
                receipt = json.loads((output / "运行回执.json").read_text())
                self.assertEqual(receipt["original_request"], original)
                self.assertEqual(receipt["candidate"]["material_uses"][0]["source_id"], "U")
                word, = prepare_delivery(output, receipt)["files"]
                paragraphs = [paragraph.text for paragraph in Document(word).paragraphs]
                self.assertEqual(paragraphs[0].replace("\n", ""), title + "（审阅稿）")
                self.assertIn(drafted, paragraphs)

    def test_other_date_range_does_not_override_the_public_meeting_window(self):
        for reply in ("内部登记时间为11月2日至4日。", "11月2日至4日为内部登记时间。"):
            with self.subTest(reply=reply):
                writer = Mock(return_value=(notice(), {}))
                run = run_task("fixture", APEC_REQUEST + "\n" + reply, generate=writer,
                               public_schedule=OFFICIAL_SCHEDULE)
                self.assertEqual(run.status, "draft", run.note)
                self.assertIn("会前一天为11月17日，会后一天为11月20日", writer.call_args.args[1])

    def test_invalid_source_prompts_once_and_accepted_relative_dates_proceed(self):
        writer = Mock(return_value=(notice("会议开始前一天和结束后一天拟安排居家办公，会期待确认。",
                                             public=False), {}))
        run = run_task("fixture", APEC_REQUEST, generate=writer,
                       public_schedule={**OFFICIAL_SCHEDULE, "quote": "会期将在确认后公布。"})
        self.assertEqual(run.status, "needs_input")
        self.assertEqual(len(run.candidate.questions), 1)
        writer.assert_not_called()
        run = run_task("fixture", APEC_REQUEST + "\n日期没定，先按相对日期给我审阅稿。", generate=writer)
        self.assertEqual(run.status, "draft", run.note)
        writer.assert_called_once()

    def test_full_date_offsets_respect_leap_days_and_year_boundaries(self):
        self.assertEqual(event_window("2028年3月1日至2日")[-2:], ((2028, 2, 29), (2028, 3, 3)))
        self.assertEqual(event_window("2026年12月31日至2027年1月2日")[-2:],
                         ((2026, 12, 30), (2027, 1, 3)))
        self.assertIsNone(event_window("3月1日至2日"))
        self.assertEqual(event_window("会议将于6月1日举行")[-2:], ((None, 5, 31), (None, 6, 2)))

    def test_wrong_date_invented_numbers_and_unspecified_continuous_dates_still_block(self):
        for text in ("拟于11月16日和11月20日安排居家办公。", "拟于2025年11月17日和11月20日居家办公。",
                     "拟于11月17日、11月20日安排17人居家办公。", "拟于11月17日至20日安排居家办公。",
                     "拟于11月18日至19日安排居家办公。"):
            with self.subTest(text=text), TemporaryDirectory() as temp:
                writer = Mock(side_effect=[(notice(text), {}), (notice(text), {})])
                run = run_task("fixture", APEC_REQUEST, generate=writer, public_schedule=OFFICIAL_SCHEDULE)
                self.assertEqual(run.status, "needs_review")
                self.assertNotIn("draft_repair", run.expert_workflow)
                writer.assert_called_once()
                output = Path(temp) / "out"
                write_result(run, output)
                self.assertFalse((output / "正文.docx").exists())

    def test_incomplete_draft_is_preserved_and_specific_questions_return_without_redrafting(self):
        for first, status in (
            (notice(title=""), "needs_review"),
            (notice(title="", sections=[], questions=["适用哪些企业？", "适用哪些企业？"]), "needs_input"),
        ):
            with self.subTest(status=status), TemporaryDirectory() as temp:
                writer = Mock(side_effect=[(first, {}), AssertionError("must not redraft")])
                run = run_task("fixture", APEC_REQUEST, generate=writer, public_schedule=OFFICIAL_SCHEDULE)
                self.assertEqual(run.status, status)
                writer.assert_called_once()
                self.assertNotIn("draft_repair", run.expert_workflow)
                output = Path(temp) / "out"
                write_result(run, output)
                receipt = json.loads((output / "运行回执.json").read_text())
                self.assertEqual(prepare_delivery(output, receipt)["files"], [])
                if status == "needs_input":
                    self.assertEqual(receipt["candidate"]["questions"], ["适用哪些企业？"])
                else:
                    self.assertEqual(receipt["rejected_candidate"], first.model_dump())

    def test_structured_addressee_is_normalized_without_a_second_model_call(self):
        candidate = notice()
        candidate.sections.insert(0, Section(title="各有关单位：", paragraphs=[
            Passage(text="", kind="fact", anchors=["U.1"])]))
        writer = Mock(return_value=(candidate, {}))
        run = run_task("fixture", APEC_REQUEST, generate=writer, public_schedule=OFFICIAL_SCHEDULE)
        self.assertEqual(run.status, "draft", run.note)
        self.assertEqual(run.candidate.sections[0].paragraphs[0].text, "深圳的上班族：")
        self.assertFalse(any("各有关单位" in p.text for s in run.candidate.sections for p in s.paragraphs))
        writer.assert_called_once()

    def test_unknown_writer_timeout_does_not_retry(self):
        writer = Mock(side_effect=RevisionIssue("MODEL_TIMEOUT", "等待上游响应超时"))
        with self.assertRaises(RevisionIssue):
            run_task("fixture", APEC_REQUEST, generate=writer, public_schedule=OFFICIAL_SCHEDULE)
        writer.assert_called_once()

    def test_conflicting_notice_date_and_weekday_is_a_question_before_writing(self):
        writer = Mock(side_effect=AssertionError("the user must resolve the conflict"))
        run = run_task("fixture", "帮我写一份培训通知。培训定于2026年11月20日（周四）下午，发给全体同事。",
                       generate=writer)
        self.assertEqual(run.status, "needs_input")
        self.assertIn("2026年11月20日是周五", run.candidate.questions[0])
        self.assertIn("周四", run.candidate.questions[0])
        writer.assert_not_called()

    def test_explicit_rehearsal_label_survives_into_the_delivered_word(self):
        title = "关于APEC会议期间相关居家办公安排的通知"
        request = (AMBIGUOUS_APEC_REQUEST + "\n就会前和会后这两天，分别是2026年11月17日和11月20日。"
                   "先做一份演练用的可编辑 Word 审阅稿，落款深圳市政府。")
        candidate = notice("2026年11月17日和11月20日分别安排1天居家办公。", public=False, title=title)
        candidate.sections[0].title = title
        run = run_task("fixture", request, generate=Mock(return_value=(candidate, {})))
        self.assertEqual(run.status, "draft", run.note)
        with TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            write_result(run, output)
            receipt = json.loads((output / "运行回执.json").read_text())
            path, = prepare_delivery(output, receipt)["files"]
            paragraphs = [p.text.replace("\n", "") for p in Document(path).paragraphs]
            marked_title = "关于APEC会议相关居家办公安排的通知（演练审阅稿）"
            self.assertIn(marked_title, path.name)
            self.assertEqual(paragraphs.count(marked_title), 1)
            self.assertNotIn(title, paragraphs)

    def test_respects_explicit_relative_dates_and_blank_signing_date(self):
        request = (AMBIGUOUS_APEC_REQUEST + "\n2026年11月16日至21日。\n"
                   "先按审阅稿给我可编辑的 Word，发给深圳的上班族。月份不填，正文保留 APEC 正式会议开始前一天"
                   "和结束后一天这两天，别换算成具体日期。发文单位保持深圳市政府，成文日期尚未确定，留空。")
        first = notice("2026年11月15日和11月22日安排居家办公。", public=False)
        writer = Mock(side_effect=[(first, {}), AssertionError("must not redraft")])
        run = run_task("fixture", request, generate=writer, public_schedule=OFFICIAL_SCHEDULE)
        self.assertEqual(run.status, "draft", run.note)
        writer.assert_called_once()
        self.assertIn("会议开始前一天和结束后一天安排居家办公。", [p.text for s in run.candidate.sections for p in s.paragraphs])
        self.assertNotIn("P.1", run.excerpts)
        self.assertIn("保留相对日期，不换算", writer.call_args_list[0].args[1])
        self.assertEqual(run.candidate.sections[-1].paragraphs[-1].text, "成文日期：")

    def test_word_review_draft_and_colloquial_relative_dates_survive_export(self):
        request = (
            "另写一份给深圳上班族的 APEC 居家办公通知。深圳市政府落款，只安排正式会议开始前一天和结束后一天，"
            "两天分别执行。具体日期还没定，正文就用相对日期，成文日期留空。先给一份可编辑 Word 审阅稿。"
        )
        title = "会议相关居家办公安排"
        writer = Mock(return_value=(notice("会议开始前一天和结束后一天分别安排居家办公。",
                                          public=False, title=title), {}))
        run = run_task("fixture", request, generate=writer, public_schedule=OFFICIAL_SCHEDULE)
        self.assertEqual(run.status, "draft", run.note)
        self.assertNotIn("P.1", run.excerpts)
        with TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            write_result(run, output)
            receipt = json.loads((output / "运行回执.json").read_text())
            path, = prepare_delivery(output, receipt)["files"]
            self.assertEqual(path.name, title + "（审阅稿）.docx")
            self.assertEqual(Document(path).paragraphs[0].text.replace("\n", ""), title + "（审阅稿）")

    def test_relative_notice_drops_unsourced_media_assignment_and_keeps_audience(self):
        request = (AMBIGUOUS_APEC_REQUEST + "\n两天分别安排，就会议开始前一天和结束后一天，各一天。"
                   "具体公历日期先用相对表述，成文日期留空。深圳市政府落款，先出一份 Word 审阅稿。")
        media = "请深圳广播电影电视集团《都市路路通》栏目记者宣传推广。"
        title = "会议相关居家办公安排（审阅稿）"
        for authorized in (False, True):
            with self.subTest(authorized=authorized), TemporaryDirectory() as temp:
                original = request + ("\n" + media if authorized else "")
                candidate = notice(public=False, title=title, sections=[
                    Section(title="", paragraphs=[Passage(text=media, kind="fact", anchors=["U.1"])]),
                    Section(title="会议相关居家办公安排", paragraphs=[
                        Passage(text="一. 会议开始前一天，实行居家办公。", kind="fact", anchors=["U.1"]),
                        Passage(text="二. 会议结束后一天，实行居家办公。", kind="fact", anchors=["U.1"]),
                    ]),
                    Section(title="", paragraphs=[Passage(text="深圳市政府", kind="fact", anchors=["U.1"])]),
                ])
                writer = Mock(return_value=(candidate, {}))
                run = run_task("fixture", original, generate=writer)
                self.assertEqual(run.status, "draft", run.note)
                writer.assert_called_once()
                output = Path(temp) / "out"
                write_result(run, output)
                receipt = json.loads((output / "运行回执.json").read_text())
                word, = prepare_delivery(output, receipt)["files"]
                paragraphs = [p.text.replace("\n", "") for p in Document(word).paragraphs]
                self.assertEqual(media in paragraphs, authorized)
                self.assertIn("深圳的上班族：", paragraphs)
                self.assertNotIn("会议相关居家办公安排", paragraphs)
                self.assertTrue(any("前一天" in p and "APEC" in p for p in paragraphs))
                self.assertIn("二. 会议结束后一天，实行居家办公。", paragraphs)
                self.assertEqual(paragraphs[-2:], ["深圳市政府", "成文日期："])

        writer = Mock(side_effect=lambda *_: (notice(media, public=False), {}))
        run = run_task("fixture", request, generate=writer)
        self.assertEqual(run.status, "needs_review")
        with TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            write_result(run, output)
            self.assertFalse((output / "正文.docx").exists())

    def test_notice_recovers_explicit_issuer_omitted_by_model(self):
        request = (
            AMBIGUOUS_APEC_REQUEST + "\n两天分别安排，就会议开始前一天和结束后一天，各一天。"
            "具体公历日期先用相对表述，成文日期留空。深圳市政府落款，先出一份 Word 审阅稿。"
        )
        for tail in ([], ["成文日期："]):
            with self.subTest(tail=tail), TemporaryDirectory() as temp:
                body = "会议开始前一天和结束后一天分别安排一天居家办公。"
                candidate = notice(body, public=False)
                candidate.sections[0].paragraphs = [
                    Passage(text=line, kind="fact", anchors=["U.1"]) for line in [body, *tail]
                ]
                writer = Mock(return_value=(candidate, {}))
                run = run_task("fixture", request, generate=writer, public_schedule=OFFICIAL_SCHEDULE)
                self.assertEqual(run.status, "draft", run.note)
                writer.assert_called_once()
                self.assertNotIn("P.1", run.excerpts)
                self.assertIn("保留相对日期，不换算", writer.call_args.args[1])
                output = Path(temp) / "out"
                write_result(run, output)
                receipt = json.loads((output / "运行回执.json").read_text())
                path, = prepare_delivery(output, receipt)["files"]
                paragraphs = [paragraph.text for paragraph in Document(path).paragraphs]
                self.assertEqual(paragraphs.count("深圳市政府"), 1)
                self.assertEqual(paragraphs.index("深圳市政府"), paragraphs.index(body) + 1)

    def test_office_scope_and_unconfirmed_approval_are_guarded_with_no_word(self):
        for text in ("深圳上班族在APEC会议期间全程居家办公。",
                     "经研究决定，会议开始前一天和结束后一天安排居家办公。"):
            with self.subTest(text=text), TemporaryDirectory() as temp:
                writer = Mock(side_effect=lambda *_: (notice(text, public=False), {}))
                run = run_task("fixture", APEC_REQUEST + "\n保留相对日期，先给我审阅稿。", generate=writer)
                self.assertEqual(run.status, "needs_review", run.note)
                output = Path(temp) / "out"
                write_result(run, output)
                self.assertFalse((output / "正文.docx").exists())

    def test_issuing_unit_is_not_evidence_of_an_existing_notice(self):
        statement = "根据深圳市政府通知安排，会议开始前一天和结束后一天分别安排居家办公。"
        for extra, expected in (
            ("", "会议开始前一天和结束后一天分别安排居家办公。"),
            ("\n已有原通知，原文是：" + statement, statement),
        ):
            with self.subTest(prior_notice=bool(extra)):
                request = APEC_REQUEST + "\n成文日期留空，先按相对日期写审阅稿。" + extra
                writer = Mock(return_value=(notice(statement, public=False), {}))
                run = run_task("fixture", request, generate=writer)
                self.assertEqual(run.status, "draft", run.note)
                writer.assert_called_once()
                self.assertEqual([s.paragraphs[0].text for s in run.candidate.sections[:2]],
                                 ["深圳的上班族：", expected])

    def test_public_notice_strips_unsupplied_title_and_holds_incomplete_independent_review(self):
        request = "帮我拟一份交通管制公告。根据深府〔2026〕63号，11月18日临时封闭幸福路。"
        candidate = notice("根据《不准确的文件名称》（深府〔2026〕63号），11月18日临时封闭幸福路。",
                           public=False, title="交通管制公告")
        checker = Mock(side_effect=RevisionIssue("MODEL_TIMEOUT", "测试核稿未完成"))
        regular_checker = Mock(side_effect=AssertionError("should use the short control notice reviewer"))
        run = run_task("fixture", request, generate=Mock(return_value=(candidate, {})), expert_team=True,
                       quick_control_review_generate=checker, review_generate=regular_checker)
        checker.assert_called_once()
        self.assertIn("根据深府〔2026〕63号", checker.call_args.args[1])
        self.assertNotIn("不准确的文件名称", checker.call_args.args[1])
        self.assertEqual(run.status, "needs_review")
