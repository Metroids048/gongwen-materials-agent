import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from docx import Document
from pydantic import ValidationError

from app import submission
from app.agent import (Candidate, ConferenceReview, VerifiedConferenceDraft, ExpertBrief, validate,
                       Passage, Run, Section, TaskItem, _verify_conference_draft, _complete_plan_fields, _replace_table_cells, check_draft, revise_case,
                       run_task, write_result)
from app.materials import collect
from app.revision.document import RevisionIssue
from app.runtime.codex import ask_codex


class NaturalIntakeTests(unittest.TestCase):
    def test_clearly_incomplete_requests_stop_before_any_model_or_word(self):
        cases = (
            ("给我写一篇关于深圳apec期间交通管制的公告", "管制日期"),
            ("帮我写一下 apec居家办公的通知书", "适用范围"),
            ("帮我写一下 apec居家办公的通知书\n\n发给深圳的上班族，"
             "apec正式会议开始前一天和结束后一天，发文单位是深圳市政府。", "各一天"),
            ("帮我拟一份交通限行公告。", "管制日期"),
            ("麻烦帮我写个通知。", "通知谁"),
            ("写一份工作方案", "希望解决什么"),
        )
        for request, expected in cases:
            with self.subTest(request=request), TemporaryDirectory() as temp:
                def unexpected_model(*_):
                    self.fail("明确缺项时不应启动起草或核稿模型")

                result = run_task("fixture", request, generate=unexpected_model,
                                  review_generate=unexpected_model, expert_team=True)
                output = Path(temp) / "result"
                write_result(result, output)
                receipt = json.loads((output / "运行回执.json").read_text())
                self.assertEqual(receipt["original_request"], request)
                self.assertEqual(receipt["status"], "needs_input")
                self.assertIn(expected, receipt["candidate"]["questions"][0])
                self.assertEqual(receipt["model"], {})
                self.assertFalse((output / "正文.docx").exists())

    def test_intake_does_not_block_requests_with_facts_or_a_template_request(self):
        for request in (
            "给我写一篇关于深圳apec期间交通管制的公告，11月22日8点到12点封闭海滨路东段，市公安局交管部门发布。",
            "写一份深圳APEC期间交通管制公告模板，具体日期和路段留空。",
            "帮我写一下 apec居家办公的通知书，日期留空，先出作为内部演练的模板。",
            "写一份培训通知。周五下午三点在二楼会议室培训，各组派员参加，联系人留空。",
        ):
            with self.subTest(request=request):
                self.assertIn(request, self._capture_draft_prompt(request))

        request = "写一份通知"
        source = "各小组星期五下午三点在二楼会议室培训。"
        candidate = Candidate(profile="unclear", questions=["请确认"], title="", sections=[], tasks=[],
                              review_notes=[], material_uses=[
                                  {"source_id": "U", "role": "requirements", "usable_anchors": []},
                                  {"source_id": "S1", "role": "facts", "usable_anchors": ["S1.L1"]},
                              ])
        with patch("app.agent.generate_intake", return_value=(candidate, {})) as generate:
            run_task("fixture", request, texts=[source], generate=generate)
            self.assertIn(source, generate.call_args.args[1])

    def test_main_writer_loads_the_explicit_genre_without_dropping_materials(self):
        request = ("帮我把下面三份材料合成一份《系统切换工作方案（会商稿）》，给我可编辑 Word。\n\n"
                   "【材料一｜协调会纪要】\n已定停录时间。\n"
                   "【材料二｜试点反馈】\n通知时间会上再定。\n"
                   "【材料三｜旧方案，仅作结构参考】\n旧方案12月5日停办。")
        request_prompt, run = self._capture_draft_prompt(request, with_run=True)
        rules, materials = request_prompt.split("本轮输入（U 为用户直接输入，其余为待判材料）：\n")
        sources = json.loads(materials)
        self.assertEqual([m["id"] for m in sources], ["U", "S1", "S2", "S3"])
        self.assertEqual(sources[0]["blocks"]["U.1"], request.split("\n\n", 1)[0])
        self.assertIn("已定停录时间。", sources[1]["blocks"]["S1.L1"])
        self.assertIn("通知时间会上再定。", sources[2]["blocks"]["S2.L1"])
        self.assertTrue(sources[3]["reference_only"])
        self.assertNotIn("S3.L1", validate(run.candidate, run.materials))
        with TemporaryDirectory() as temp:
            target = Path(temp) / "result"
            write_result(run, target)
            self.assertEqual(json.loads((target / "运行回执.json").read_text())["original_request"], request)
        self.assertIn("# 自然收件与初稿", rules)
        self.assertIn("name: gongwen-expert-plan", rules)
        for specialist in ("experience", "notice", "review-drafts"):
            self.assertNotIn(f"name: gongwen-expert-{specialist}", rules)

    def test_main_writer_keeps_full_rules_for_ambiguous_drafts(self):
        requests = ("根据会议纪要帮我整理成一份会前材料。", "帮我写一份工作方案或通知，看看哪种合适。",
                    "帮我起草一份通知，再写一份工作方案。", "先别写一份工作方案，请帮我看看还缺什么。",
                    "帮我写一份上次通知的执行情况小结。")
        for request in requests:
            with self.subTest(request=request):
                prompt = self._capture_draft_prompt(request)
                for specialist in ("plan", "experience", "notice", "review-drafts"):
                    self.assertIn(f"name: gongwen-expert-{specialist}", prompt)

    def test_public_notice_followup_uses_notice_rules_and_can_ask_for_a_remaining_fact(self):
        request = ("给我写一篇关于深圳apec期间交通管制的公告\n\n"
                   "补充一下，给市民看的公告只写外地号牌载客汽车临时限行。")
        candidate = Candidate(profile="notice-draft", questions=["请补充已确认的限行时段。"], title="",
                              sections=[], tasks=[], review_notes=[],
                              material_uses=[{"source_id": "U", "role": "requirements", "usable_anchors": []}])
        generic = Mock(side_effect=AssertionError("used generic writer"))
        reviewer = Mock(side_effect=AssertionError("started review"))
        notice = Mock(return_value=(candidate, {}))
        result = run_task("fixture", request, generate=generic, quick_notice_generate=notice,
                          review_conference=True, review_generate=reviewer)
        rules, _ = notice.call_args.args[1].split("本轮输入（U 为用户直接输入，其余为待判材料）：\n")
        self.assertIn("name: gongwen-expert-notice", rules)
        self.assertNotIn("name: gongwen-expert-plan", rules)
        self.assertEqual(result.status, "needs_input")
        self.assertEqual(result.candidate.questions, ["请补充已确认的限行时段。"])
        generic.assert_not_called()
        reviewer.assert_not_called()

    def _capture_draft_prompt(self, request, *, with_run=False):
        candidate = Candidate(profile="unclear", questions=["请确认"], title="", sections=[], tasks=[],
                              review_notes=[], material_uses=[{"source_id": "U", "role": "requirements",
                                                              "usable_anchors": []}])
        def host_exchange(model, prompt, schema):
            if schema is Candidate:
                return candidate, {}
            data = candidate.model_dump()
            with self.assertRaises(ValueError):
                schema.model_validate({**data, "material_uses": data["material_uses"] * 4})
            data["material_uses"] = {
                key: {"role": "facts", "usable_anchors": [anchor]}
                for key, anchor in (("U", "U.1"), ("S1", "S1.L1"), ("S2", "S2.L1"))
            }
            return schema.model_validate(data), {}

        with patch("app.agent.generate_intake", side_effect=host_exchange) as generate:
            run = run_task("fixture", request, generate=generate, expert_team=True)
        prompt = generate.call_args.args[1]
        return (prompt, run) if with_run else prompt

    def test_identical_full_request_is_drafting_not_public_plan_revision(self):
        from app.agent import main
        parent = self.base / "parent"
        parent.mkdir()
        request = "写一份培训通知，时间地点按本轮安排。"
        (parent / "运行回执.json").write_text(json.dumps({"status": "draft", "original_request": request,
                                                        "candidate": {"profile": "notice-draft"}}))
        with patch("app.agent.run_task") as draft, patch("app.agent.write_result"), patch("app.agent.revise") as revise:
            draft.return_value.status = "draft"
            draft.return_value.candidate.profile = "notice-draft"
            self.assertEqual(main([request, "--continue-from", str(parent), "--codex-model", "fixture",
                                   "--output", str(self.base / "new")]), 0)
            draft.assert_called_once()
            revise.assert_not_called()

    def test_unsupported_notice_revision_does_not_enter_nanyang_branch(self):
        from app.agent import main
        parent = self.base / "parent"
        parent.mkdir()
        (parent / "运行回执.json").write_text(json.dumps({"status": "draft", "original_request": "写培训通知",
                                                        "candidate": {"profile": "notice-draft"}}))
        with patch("app.agent.revise") as revise, self.assertRaises(SystemExit):
            main(["把通知的培训内容换一下", "--continue-from", str(parent), "--codex-model", "fixture",
                  "--output", str(self.base / "new")])
        revise.assert_not_called()

    def test_native_anchor_with_description_is_not_silently_rebound(self):
        request = "文种为社区工作人员业务培训的会议通知。"
        candidate = Candidate(profile="notice-draft", material_uses=[
            {"source_id": "U", "role": "mixed", "usable_anchors": ["U.1：" + request]},
        ], questions=[], title="培训通知", sections=[], tasks=[], review_notes=[])
        result = run_task("fixture", request, generate=lambda *_: (candidate, {}))
        self.assertEqual(result.status, "needs_review")
        self.assertIn("无法找到材料片段 U.1：", result.note)
        output = self.base / "rejected-diagnostic"
        write_result(result, output)
        receipt = json.loads((output / "运行回执.json").read_text())
        self.assertEqual(receipt["rejected_candidate"], candidate.model_dump())
        self.assertFalse((output / "正文.docx").exists())

    def test_summary_and_exchange_separate_grounded_facts_from_follow_up(self):
        fact = Passage(text="线上未解决的问题由专员接办。", kind="fact", anchors=["S1.L1"])
        suggestion = Passage(text="核对专员接办记录并向原渠道反馈。", kind="proposal", anchors=["S1.L1"])
        usable = {"S1.L1": fact.text}
        for profile, heading in [("summary-draft", "后续复核安排"), ("exchange-draft", "交流建议")]:
            with self.subTest(profile=profile):
                past = Section(title="已有情况", paragraphs=[fact])
                follow_up = Section(title=heading, paragraphs=[suggestion])
                candidate = Candidate(profile=profile, title="工作材料", sections=[past, follow_up], tasks=[],
                                      questions=[], review_notes=[], material_uses=[])
                check_draft(candidate, usable)
                with self.assertRaisesRegex(RevisionIssue, "必须分别成段"):
                    check_draft(candidate.model_copy(update={"sections": [
                        past.model_copy(update={"paragraphs": [suggestion]}), follow_up,
                    ]}), usable)
                with self.assertRaisesRegex(RevisionIssue, "单列在文末"):
                    check_draft(candidate.model_copy(update={"sections": [follow_up, past]}), usable)
                with self.assertRaisesRegex(RevisionIssue, "缺少有效"):
                    check_draft(candidate.model_copy(update={"sections": [
                        past, follow_up.model_copy(update={"paragraphs": [
                            suggestion.model_copy(update={"anchors": []}),
                        ]}),
                    ]}), usable)

    def test_codex_timeout_preserves_last_stage_without_model_text(self):
        output = b'{"type":"thread.started"}\n{"type":"turn.started"}\n{"text":"private draft"}\n{"type":'
        with patch("app.runtime.codex.shutil.which", return_value="/bin/codex"), patch(
            "app.runtime.codex.subprocess.run",
            side_effect=subprocess.TimeoutExpired([], 300, output=output),
        ) as run:
            with self.assertRaises(RevisionIssue) as raised:
                ask_codex("fixture", "private request", Candidate, timeout=300, disable_plugins=True)
        self.assertEqual(run.call_args.args[0][1:3], ["--disable", "plugins"])
        self.assertEqual(raised.exception.code, "JOB_OUTCOME_UNKNOWN")
        self.assertIn("300 秒上限（模型回合已启动，尚无完成事件）", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def run_fixture(self, **options):
        def generate(_model, prompt, schema):
            self.assertIs(schema, Candidate)
            self.assertIn("S1.L1", prompt)
            self.assertIn("S2.R2", prompt)
            values = {
                "profile": "experience-draft",
                "material_uses": [
                    {"source_id": "U", "role": "requirements", "usable_anchors": []},
                    {"source_id": "S1", "role": "facts", "usable_anchors": ["S1.L1"]},
                    {"source_id": "S2", "role": "reference", "usable_anchors": []},
                ],
                "questions": [],
                "title": "延时服务交流",
                "sections": [Section(title="便民服务", paragraphs=[
                    Passage(text="本月延时办理20件。", kind="fact", anchors=["S1.L1"])
                ])],
                "tasks": [],
                "review_notes": [],
            } | options
            return schema.model_validate(values), {"model": "fixture"}

        file = self.base / "旧稿.csv"
        file.write_text("示例\n旧稿完成99件\n")
        return run_task("fixture", "会上介绍一下延时服务，旧稿只参考写法。",
                        ["本月延时办理20件。"], [file], generate=generate)

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def test_clock_format_equivalence_uses_same_source_times(self):
        source = "11月20日上午9点到11点，11月18日17点前报名。"
        for text in ["11月20日9:00至11:00，11月18日17:00前报名。",
                     "11月20日9时至11时，11月18日17时前报名。"]:
            with self.subTest(text=text):
                candidate = Candidate(profile="notice-draft", title="培训通知", sections=[
                    Section(title="培训安排", paragraphs=[Passage(text=text, kind="fact", anchors=["U.1"])])
                ], tasks=[], questions=[], review_notes=[], material_uses=[])
                check_draft(candidate, {"U.1": source})

    def test_clock_equivalence_does_not_authorize_new_times_or_other_numbers(self):
        source = "11月20日上午9点到11点，安排2场培训。"
        for text in ["11月20日9:30至11:00。", "11月20日8:00至11:00。",
                     "11月20日9:00至11:00，安排3场培训。", "新增00人。"]:
            with self.subTest(text=text):
                candidate = Candidate(profile="notice-draft", title="培训通知", sections=[
                    Section(title="培训安排", paragraphs=[Passage(text=text, kind="fact", anchors=["U.1"])])
                ], tasks=[], questions=[], review_notes=[], material_uses=[])
                with self.assertRaisesRegex(RevisionIssue, "缺少对应出处"):
                    check_draft(candidate, {"U.1": source})

    def test_colloquial_afternoon_time_keeps_same_hour_without_authorizing_morning(self):
        from app.agent import numeric_tokens
        source = "18号下午5点前报名。"
        self.assertEqual(numeric_tokens(source), numeric_tokens("18号17:00前报名。"))
        for text in ("18号5:00前报名。", "18号17:30前报名。", "19号17:00前报名。"):
            candidate = Candidate(profile="notice-draft", title="培训通知", sections=[
                Section(title="报名事项", paragraphs=[Passage(text=text, kind="fact", anchors=["U.1"])])
            ], tasks=[], questions=[], review_notes=[], material_uses=[])
            with self.subTest(text=text), self.assertRaisesRegex(RevisionIssue, "缺少对应出处"):
                check_draft(candidate, {"U.1": source})

    def test_clock_minutes_and_half_hour_are_not_rounded_to_whole_hour(self):
        from app.agent import numeric_tokens
        self.assertEqual(numeric_tokens("09:30"), numeric_tokens("9时30分"))
        self.assertNotEqual(numeric_tokens("9点半"), numeric_tokens("9:00"))
        self.assertNotEqual(numeric_tokens("9小时"), numeric_tokens("9:00"))
        self.assertNotIn("时间01:00", numeric_tokens("第1时段、第一时段"))
        self.assertEqual(numeric_tokens("17:00前交校验记录，服务组在同一时间前交清单。"), {"时间17:00"})
        self.assertIn("时间01:00", numeric_tokens("1时截止"))

    def test_plan_task_checks_clock_times_without_splitting_words(self):
        source = "意见不一致时由组长组织协商，仍不能统一的，通过系统投票。"
        task = TaskItem(action="仍不能统一时通过系统投票", lead="", support="", timing="",
                        deliverable="", acceptance="", status="confirmed", anchors=["S1.L1"])
        candidate = Candidate(profile="plan-draft", title="会前讨论稿", sections=[
            Section(title="评审安排", paragraphs=[Passage(text=source, kind="fact", anchors=["S1.L1"])])
        ], tasks=[task], questions=[], review_notes=[], material_uses=[])
        check_draft(candidate, {"S1.L1": source})
        task.timing = "一时开始"
        with self.assertRaisesRegex(RevisionIssue, "时间01:00.*缺少对应出处"):
            check_draft(candidate, {"S1.L1": source})

    def test_pre_meeting_discussion_loads_plan_specialist(self):
        prompt = self._capture_draft_prompt("照附件写一份会前讨论稿，给我一个 Word，分工表要能改。")
        self.assertIn("name: gongwen-expert-plan", prompt)
        self.assertNotIn("name: gongwen-expert-notice", prompt)

    def test_paragraph_list_marker_and_equivalent_quantities(self):
        source = "拟安排两名夜间值守。办公室连续跟踪三个工作日。建议提前两天公告，演练中两件附件打不开。"
        for text, valid in [
            ("1. 拟安排2名夜间值守，办公室跟踪3个工作日。", True),
            ("建议提前2天公告，演练中2件附件打不开。", True),
            ("建议提前3天公告，演练中3件附件打不开。", False),
            ("1. 拟安排3名夜间值守，办公室跟踪2个工作日。", False),
            ("1. 拟安排2名夜间值守，在3天内完成。", False),
            ("仅安排1名夜间值守。", False),
            ("1.5个工作日内完成。", False),
        ]:
            with self.subTest(text=text):
                candidate = Candidate(profile="notice-draft", title="值守通知", sections=[
                    Section(title="安排", paragraphs=[Passage(text=text, kind="fact", anchors=["U.1"])])
                ], tasks=[], questions=[], review_notes=[], material_uses=[])
                if valid:
                    check_draft(candidate, {"U.1": source})
                else:
                    with self.assertRaisesRegex(RevisionIssue, "缺少对应出处"):
                        check_draft(candidate, {"U.1": source})

    def test_wrong_number_or_reference_cannot_be_exported(self):
        for anchors in (["S1.L1"], ["S2.R2"]):
            with self.subTest(anchors=anchors):
                result = self.run_fixture(sections=[Section(title="成效", paragraphs=[
                    Passage(text="完成99件。", kind="fact", anchors=anchors)
                ])])
                self.assertEqual(result.status, "needs_review")
        out = self.base / "result"
        write_result(result, out)
        self.assertFalse((out / "正文.docx").exists())

    def test_draft_has_current_excerpt_and_editable_word(self):
        result = self.run_fixture()
        out = self.base / "result"
        write_result(result, out)
        self.assertEqual(result.excerpts, {"S1.L1": "本月延时办理20件。"})
        doc = Document(out / "正文.docx")
        self.assertIn("本月延时办理20件。", [p.text for p in doc.paragraphs])
        receipt = json.loads((out / "运行回执.json").read_text())
        self.assertEqual(receipt["status"], "draft")
        self.assertIn("正文.docx", receipt["artifacts"])

    def test_plan_has_readable_body_and_editable_task_appendix(self):
        result = self.run_fixture(profile="plan-draft", sections=[
            Section(title="目标", paragraphs=[Passage(text="核对办理台账。", kind="proposal", anchors=[])])
        ], tasks=[TaskItem(action="核对办理台账", lead="", support="", timing="准备阶段",
                            deliverable="差异清单", acceptance="差异可定位到登记条目",
                            status="proposed", anchors=[])])
        out = self.base / "plan"
        write_result(result, out)
        doc = Document(out / "正文.docx")
        row = [cell.text for cell in doc.tables[0].rows[1].cells]
        self.assertEqual(row[1:], ["核对办理台账（建议）", "责任单位：待明确", "准备阶段",
                                   "差异清单", "差异可定位到登记条目", "", ""])
        self.assertEqual([c.text for c in doc.tables[0].rows[0].cells][-2:], ["联系人", "电话"])
        body = [p.text for p in doc.paragraphs]
        self.assertIn("附件：任务分工表", body)
        self.assertNotIn("差异可定位到登记条目", "\n".join(body))
        self.assertAlmostEqual(doc.sections[0].page_width.cm, 21, places=2)
        self.assertAlmostEqual(doc.sections[1].page_width.cm, 29.7, places=2)

    def test_confirmed_action_can_have_pending_fields(self):
        result = self.run_fixture(profile="plan-draft", sections=[
            Section(title="会议用途", paragraphs=[Passage(text="会前核对办理台账。", kind="proposal", anchors=[])])
        ], tasks=[TaskItem(action="核对办理台账", lead="", support="", timing="本月",
                           deliverable="", acceptance="", status="confirmed", anchors=["S1.L1"])])
        self.assertEqual(result.status, "draft")
        out = self.base / "partial"
        write_result(result, out)
        doc = Document(out / "正文.docx")
        row = [cell.text for cell in doc.tables[0].rows[1].cells]
        self.assertEqual(row[1:], ["核对办理台账", "责任单位：待明确", "本月", "", ""])
        self.assertIn("验收方式", (out / "依据与待核.md").read_text())

    def test_help_and_delegation_plan_exports_editable_comparison_and_blank_agenda(self):
        def passage(text):
            return Passage(text=text, kind="proposal", anchors=[])

        request = "请出一份帮办代办会商方案。牵头科室、试点窗口和启动日期都还没定，放到会上逐项议。"
        labels = ("牵头科室", "试点窗口", "启动日期")
        candidate = self.run_fixture(profile="plan-draft", sections=[
            Section(title="会上要定的事项", paragraphs=[passage(f"{label}：会上确认。") for label in labels]),
            Section(title="会商议题表", paragraphs=[
                passage(f"{label}｜待议｜逐项确认") for label in labels
            ]),
            Section(title="帮办与代办情形对照", paragraphs=[
                passage("申请递交｜申请人自行提交｜受委托代为递交｜先核对意愿和事项范围")
            ]),
        ], tasks=[TaskItem(action="登记受理", lead="", support="", timing="",
                           deliverable="台账", acceptance="", status="proposed", anchors=[])]).candidate

        def generate(_model, _prompt, _schema):
            value = candidate.model_copy(deep=True)
            value.material_uses = [use for use in value.material_uses if use.source_id == "U"]
            value.sections[1].paragraphs.insert(0, passage("二、会商议题表"))
            return value, {"model": "fixture"}

        result = run_task("fixture", request, generate=generate)
        self.assertEqual(result.status, "draft")
        out = self.base / "meeting-plan"
        write_result(result, out)
        word = Document(out / "正文.docx")
        agenda, comparison, tasks = word.tables
        self.assertEqual([p.text for p in word.paragraphs if "会商议题表" in p.text], ["会商议题表"])
        self.assertEqual([[cell.text for cell in row.cells] for row in agenda.rows], [
            ["序号", "议题", "待定内容", "议定要点", "议定结果"],
            *[[str(i), label, "待议", "逐项确认", ""] for i, label in enumerate(labels, 1)],
        ])
        self.assertEqual([cell.text for cell in comparison.rows[0].cells],
                         ["情形", "仅帮办", "需办委托", "判定与下一步"])
        self.assertEqual(tasks.rows[1].cells[2].text, "责任单位：待明确")

        candidate.sections[1].paragraphs[0].text += "｜已定"
        malformed = run_task("fixture", request, generate=generate)
        self.assertEqual(malformed.status, "needs_review")
        self.assertIn("表格行不完整", malformed.note)

    def test_meeting_agenda_can_give_each_unit_its_own_blank_result(self):
        def passage(text):
            return Passage(text=text, kind="proposal", anchors=[])

        request = "请起草会商方案。各点联系人、启动日期都还没定，放到会上逐项议。"
        labels = ["各点联系人（甲点）", "各点联系人（乙点）", "启动日期"]
        agenda = Section(title="会商议题表", paragraphs=[passage(f"{label}｜待议｜逐项确认") for label in labels])
        candidate = self.run_fixture(profile="plan-draft", sections=[
            Section(title="会上要定的事项", paragraphs=[
                passage("各点联系人：甲点、乙点分别确认。"),
                passage("启动日期：会上确认。"),
            ]),
            agenda,
        ], tasks=[TaskItem(action="提交记录", lead="", support="", timing="",
                           deliverable="记录", acceptance="", status="proposed", anchors=[])]).candidate
        candidate.material_uses = [use for use in candidate.material_uses if use.source_id == "U"]

        def generate(_model, _prompt, _schema):
            return candidate.model_copy(deep=True), {"model": "fixture"}

        result = run_task("fixture", request, generate=generate)
        self.assertEqual(result.status, "draft", result.note)
        out = self.base / "agenda-subitems"
        write_result(result, out)
        rows = Document(out / "正文.docx").tables[0].rows
        self.assertEqual([(row.cells[1].text, row.cells[-1].text) for row in rows[1:]],
                         [(label, "") for label in labels])

        agenda = candidate.sections[1]
        agenda.paragraphs.insert(0, passage("各点联系人｜待议｜逐项确认"))
        self.assertEqual(run_task("fixture", request, generate=generate).status, "needs_review")

    def test_explicit_pending_items_and_sourced_training_outcome(self):
        request = "请做份会商稿。材料里没定的联络人、培训日期和验收标准，单列出来会上问。"
        source = "专家培训：服务中心负责开展业务在线培训。培训日期和验收标准未定。"
        candidate = self.run_fixture(profile="plan-draft", sections=[
            Section(title="待核事项", paragraphs=[
                Passage(text="联络人、培训日期和验收标准会上确认。", kind="proposal", anchors=[])
            ])
        ], tasks=[TaskItem(action="开展业务在线培训", lead="服务中心", support="", timing="",
                            deliverable="", acceptance="", status="confirmed", anchors=["S1.L1"])]).candidate

        def generate(_model, _prompt, schema):
            value = candidate.model_dump()
            value["material_uses"] = [use for use in value["material_uses"] if use["source_id"] != "S2"]
            return schema.model_validate(value), {"model": "fixture"}

        merged = run_task("fixture", request, [source], generate=generate)
        self.assertEqual(merged.status, "needs_review")
        self.assertIn("单列", merged.note)
        alternative = run_task("fixture", "请做会商稿。联络人、培训日期和验收标准都还没定，放到会上逐项议。",
                               [source], generate=generate)
        self.assertIn("单列", alternative.note)
        candidate.sections[0].paragraphs = [
            Passage(text=f"{label}：请会上确认。", kind="proposal", anchors=[])
            for label in ("联络人", "培训日期", "验收标准")
        ]
        alternative = run_task("fixture", "请做会商稿。联络人、培训日期和验收标准都还没定，放到会上逐项议。",
                               [source], generate=generate)
        self.assertEqual(alternative.status, "draft")
        result = run_task("fixture", request, [source], generate=generate)
        self.assertEqual(result.status, "draft")
        out = self.base / "consultation"
        write_result(result, out)
        document = Document(out / "正文.docx")
        self.assertEqual([p.text for p in document.paragraphs if p.text.startswith("建议：")], [
            "建议：联络人：请会上确认。", "建议：培训日期：请会上确认。", "建议：验收标准：请会上确认。"
        ])
        self.assertEqual(document.tables[0].rows[1].cells[3].text, "待明确")
        self.assertEqual(document.tables[0].rows[1].cells[4].text, "业务在线培训")
        self.assertEqual(result.candidate.tasks[0].acceptance, "")

    def test_reviewed_conference_holds_a_proposed_deadline_without_its_own_source(self):
        source = "切换窗口已定为12月12日18:30到21:00。\n拟安排两名夜间值守，时间会上再定。"
        candidate = Candidate(profile="plan-draft", title="系统切换工作方案（会商稿）",
                              material_uses=[
                                  {"source_id": "U", "role": "requirements", "usable_anchors": []},
                                  {"source_id": "S1", "role": "facts", "usable_anchors": ["S1.L1", "S1.L2"]},
                              ], sections=[Section(title="待议事项", paragraphs=[
                                  Passage(text="拟安排两名夜间值守。", kind="proposal", anchors=["S1.L2"]),
                              ])], tasks=[
                                  TaskItem(action="执行系统切换", lead="", support="", timing="12月12日18:30到21:00",
                                           deliverable="", acceptance="", status="confirmed", anchors=["S1.L1"]),
                                  TaskItem(action="安排两名夜间值守", lead="", support="", timing="12月12日18:30到21:00",
                                           deliverable="", acceptance="", status="proposed", anchors=["S1.L2"]),
                              ], questions=[], review_notes=[])
        stages = []

        def review(_model, prompt, schema):
            stages.append(schema.__name__)
            inputs = json.loads(prompt.split("本轮数据：\n")[-1])
            rows = inputs.get("分工表", inputs.get("最终分工表"))
            self.assertEqual(rows["T1"]["timing"], "12月12日18:30到21:00")
            self.assertEqual(rows["T2"]["timing"], "")
            self.assertEqual(rows["T2"]["status"], "proposed")
            if issubclass(schema, ConferenceReview):
                return schema(basis_key=inputs["basis_key"], checked_ids=["P1.1", "T1", "T2"], findings=[]), {}
            self.fail("无独立核稿缺陷时不重跑模型")

        def run(value, enabled):
            return run_task("fixture", "请写系统切换方案（会商稿）。", [source],
                            generate=lambda *_: (value, {}), review_conference=enabled,
                            review_generate=review)

        rejected = run(candidate.model_copy(deep=True), False)
        self.assertEqual(rejected.status, "needs_review")
        self.assertIn("任务 2的数字", rejected.note)
        draft = run(candidate.model_copy(deep=True), True)
        self.assertEqual(stages, ["BoundConferenceReview"])
        self.assertEqual(draft.status, "draft")
        self.assertEqual(draft.candidate.tasks[1].timing, "")
        self.assertEqual(draft.candidate.tasks[0].timing, "12月12日18:30到21:00")

        for change in ({"status": "confirmed"}, {"acceptance": "核查99条登记"}):
            with self.subTest(change=change):
                invalid = candidate.model_copy(deep=True)
                invalid.tasks[1] = invalid.tasks[1].model_copy(update=change)
                stages.clear()
                self.assertEqual(run(invalid, True).status, "needs_review")
                self.assertEqual(stages, [])

    def test_conference_review_writes_targeted_changes_and_preserves_other_content(self):
        request = "帮我写会商稿，培训排期还未定，分工表别漏县区。"
        source = ("市交易中心应开展培训。培训日期未定。现场登记负责人未定。"
                  "12月11日17:00前，市交易中心交培训提纲，服务组交未办件清单。\n各县区负责组织属地单位落实。")
        training = TaskItem(action="开展培训", lead="市交易中心", support="", timing="",
                            deliverable="", acceptance="", status="confirmed", anchors=["S1.L1"])
        county = training.model_copy(update={"action": "组织属地单位落实", "lead": "各县区", "anchors": ["S1.L2"]})
        registration = training.model_copy(update={"action": "现场登记"})
        backlog = training.model_copy(update={"action": "整理并提交未办件清单", "lead": "", "deliverable": "未办件清单"})
        initial = Candidate(profile="plan-draft", material_uses=[
            {"source_id": "U", "role": "requirements", "usable_anchors": []},
            {"source_id": "S1", "role": "facts", "usable_anchors": ["S1.L1", "S1.L2"]},
        ], questions=[], title="街区便民服务系统切换工作方案（会商稿）", sections=[Section(title="", paragraphs=[
            Passage(text="市交易中心开展培训，会上确定日期。", kind="proposal", anchors=["S1.L1"]),
            Passage(text="各县区负责组织属地单位落实。", kind="fact", anchors=["S1.L2"]),
        ])], tasks=[training, registration, backlog], review_notes=[])
        stages = []

        def review(_model, prompt, schema):
            stages.append(ConferenceReview.__name__ if issubclass(schema, ConferenceReview) else schema.__name__)
            inputs = json.loads(prompt.split("本轮数据：\n")[-1])
            self.assertEqual(inputs["可用依据"]["S1.L2"], "各县区负责组织属地单位落实。")
            key = inputs["basis_key"]
            if issubclass(schema, VerifiedConferenceDraft):
                self.assertEqual(set(inputs["待核问题"]), {"F1", "F2", "F3"})
                self.assertNotIn("最终分工表", inputs)
                return schema(basis_key=key, findings=[
                    {"finding_id": name, "resolved": True, "basis_id": basis, "quote": quote, "reason": "与原文相符"}
                    for name, basis, quote in [("F1", "S1.L1", "市交易中心应开展培训"),
                                               ("F2", "S1.L2", "各县区负责组织属地单位落实"),
                                               ("F3", "S1.L1", "现场登记负责人未定")]
                ]), {}
            if issubclass(schema, ConferenceReview):
                self.assertEqual(inputs["可用依据"]["U.1"], request)
                self.assertNotIn("Word分工表视图", inputs)
                return schema(basis_key=key, checked_ids=["P1.1", "P1.2", "T1", "T2", "T3"], findings=[
                    {"target_ids": ["P1.1"], "basis_ids": ["S1.L1"], "level": "blocking",
                     "defect": "既定培训写成建议。", "minimal_fix": "培训义务和日期拆开。"},
                    {"target_ids": ["TASKS"], "basis_ids": ["S1.L2"], "level": "blocking",
                     "defect": "县区责任缺行。", "minimal_fix": "新增县区落实行。"},
                    {"target_ids": ["T2"], "basis_ids": ["S1.L1"], "level": "blocking",
                     "defect": "登记单位未定。", "minimal_fix": "去掉无依据的登记单位。"},
                ]), {}
            self.assertEqual(schema.__name__, "BoundConferenceRepair")
            self.assertEqual(set(inputs["待修片段"]), {"P1.1", "T2"})
            self.assertNotIn("正文与章节", inputs)
            self.assertFalse(schema.model_json_schema()["$defs"]["ConferenceRepairPositions"]["additionalProperties"])
            return schema(basis_key=key, edits={"P1.1": [
                Passage(text="市交易中心应开展培训。", kind="fact", anchors=["S1.L1"]),
                Passage(text="培训日期：请会上明确。", kind="proposal", anchors=["S1.L1"]),
            ], "T2": [registration.model_copy(update={"lead": ""})]}, new_tasks=[county]), {}

        def run(review_generate, enabled=True, evidence=source):
            return run_task("fixture", request, [evidence], generate=lambda *_: (initial.model_copy(deep=True), {}),
                            review_conference=enabled, review_generate=review_generate)

        baseline = run(lambda *_: self.fail("单主笔不能调用核稿"), enabled=False)
        result = run(review)
        self.assertEqual(stages, ["ConferenceReview", "BoundConferenceRepair", "VerifiedConferenceDraft"])
        self.assertEqual(result.conference_review["status"], "changes_applied")
        self.assertEqual(result.candidate.sections[0].paragraphs[-1], baseline.candidate.sections[0].paragraphs[-1])
        self.assertEqual(result.candidate.tasks[0], baseline.candidate.tasks[0])
        self.assertEqual(len(baseline.candidate.tasks), 3)
        audit = result.conference_review["task_evidence"]
        self.assertEqual(audit["before_sha256"], audit["after_sha256"])
        self.assertEqual(len(audit["findings"]), 3)
        out = self.base / "reviewed"
        write_result(result, out)
        document = Document(out / "正文.docx")
        self.assertEqual(document.paragraphs[0].text, "街区便民服务系统切换工作方案\n（会商稿）")
        self.assertEqual(result.candidate.title, initial.title)
        self.assertIn("市交易中心应开展培训。", [p.text for p in document.paragraphs])
        self.assertNotIn("市交易中心负责现场登记", "\n".join(p.text for p in document.paragraphs))
        self.assertEqual(document.tables[0].rows[2].cells[2].text, "责任单位：待明确")
        self.assertEqual(document.tables[0].rows[3].cells[2].text, "责任单位：服务组")
        self.assertEqual(document.tables[0].rows[3].cells[3].text, "12月11日17:00前")
        self.assertEqual(document.tables[0].rows[-1].cells[2].text, "责任单位：各县区")

        for corrupt in ("bad_quote", "unresolved", "wrong_basis", "missing"):
            def incomplete(*args):
                value, meta = review(*args)
                if isinstance(value, VerifiedConferenceDraft):
                    if corrupt == "bad_quote":
                        value.findings[0].quote = "市交易中心负责现场登记"
                    elif corrupt == "unresolved":
                        value.findings[0].resolved = False
                    elif corrupt == "wrong_basis":
                        value.basis_key = "other"
                    else:
                        value.findings.pop()
                return value, meta

            with self.subTest(corrupt=corrupt):
                held = run(incomplete)
                self.assertEqual(held.status, "needs_review")
                self.assertEqual(held.conference_review["failed_stage"], "task_evidence")
                output = self.base / corrupt
                write_result(held, output)
                self.assertFalse((output / "正文.docx").exists())
                self.assertTrue((output / "运行回执.json").exists())
        for case, code in (("stale", "REVIEW_BINDING_INVALID"), ("out_of_scope", "REVIEW_SCOPE_INVALID")):
            with self.subTest(case=case):
                def invalid(*args):
                    value, metadata = review(*args)
                    if args[-1].__name__ == "BoundConferenceRepair":
                        if case == "stale":
                            value.basis_key = "different-draft"
                        else:
                            unauthorized = value.model_dump()
                            unauthorized["edits"]["P1.2"] = unauthorized["edits"]["P1.1"]
                            with self.assertRaises(ValidationError):
                                args[-1].model_validate(unauthorized)
                            # Keep checking the host guard independently of the model schema.
                            value.edits.__dict__["P1.2"] = getattr(value.edits, "P1.1")
                    return value, metadata

                fallback = run(invalid)
                self.assertEqual(fallback.conference_review["error"]["code"], code)
                self.assertIn("rejected_repair", fallback.conference_review)
                self.assertEqual(fallback.candidate, baseline.candidate)
                self.assertIn("保留原初稿", fallback.note)

        def missing_task_check(*args):
            value, metadata = review(*args)
            if isinstance(value, ConferenceReview):
                value.checked_ids.remove("T3")
            return value, metadata

        self.assertEqual(run(missing_task_check).conference_review["error"]["code"], "REVIEW_BINDING_INVALID")

    def test_final_table_does_not_extend_pending_intake_to_another_rows_owner(self):
        ledger = "服务组负责窗口接待话术和未办件台账"
        intake = "紧急业务改用纸质登记，保留回填标记"
        reentry = "纸质登记由服务组在恢复运行后补录并复核"
        assigned = "现场组负责纸质登记"
        available = {"S1.L1": f"{ledger}。{intake}。{reentry}。", "S1.L2": f"{assigned}。"}

        def row(action, anchors=("S1.L1",), lead="服务组"):
            return TaskItem(action=action, lead=lead, support="", timing="", deliverable="",
                            acceptance="", status="confirmed", anchors=list(anchors))

        draft = Candidate(profile="plan-draft", title="系统切换工作方案（会商稿）", material_uses=[],
                          questions=[], review_notes=[], sections=[Section(title="", paragraphs=[
                              Passage(text="紧急业务改用纸质登记。", kind="fact", anchors=["S1.L1"]),
                          ])], tasks=[
            row("准备未办件台账，安排纸质登记及恢复后的补录复核"),
            row("受理紧急业务，使用纸质登记并保留回填标记"),
            row("补录并复核恢复运行前形成的纸质登记"),
            row("首次纸质登记", ("S1.L2",), "现场组"),
        ])

        actions = [task.action for task in draft.tasks]
        _complete_plan_fields(draft, available, [], "")
        self.assertEqual([task.lead for task in draft.tasks], ["", "", "服务组", "现场组"])
        frozen = draft.model_dump()
        report = {"stages": {}, "review": {"checked_ids": ["P1.1", "T1", "T2", "T3", "T4"], "findings": []}}
        verified = _verify_conference_draft("fixture", draft, available, self.fail, report)
        self.assertEqual(verified.model_dump(), frozen)
        self.assertEqual([t.action for t in verified.tasks], actions)

    def test_final_conference_evidence_preserves_agenda_rows_as_typed_cells(self):
        source = "西站拟安排两名夜间值守，人员未定。夜间值守安排由会上确定。"
        agenda = Passage(text="夜间值守人选｜西站拟安排两名夜间值守，人员未定｜会上明确人选",
                         kind="proposal", anchors=["S1.L1"])
        draft = Candidate(profile="plan-draft", title="系统切换方案（会商稿）", material_uses=[],
                          questions=[], review_notes=[], sections=[
            Section(title="会上要定的事项", paragraphs=[
                Passage(text="夜间值守人员未定。", kind="proposal", anchors=["S1.L1"]),
            ]),
            Section(title="会商议题表", paragraphs=[agenda]),
        ], tasks=[TaskItem(action="会上讨论夜间值守安排", lead="", support="", timing="",
                          deliverable="", acceptance="", status="proposed", anchors=["S1.L1"])])

        data = {"S1.L1": source}
        frozen = draft.model_dump()
        report = {"stages": {}, "review": {"checked_ids": ["P1.1", "P2.1", "T1"], "findings": []}}
        final = _verify_conference_draft("fixture", draft, data, self.fail, report)
        self.assertEqual(final.model_dump(), frozen)
        for forbidden in ("paragraphs", "table_rows", "rows", "tasks"):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValidationError):
                VerifiedConferenceDraft.model_validate({"basis_key": "x", "findings": [], forbidden: []})
        with self.assertRaises(RevisionIssue):
            _replace_table_cells(agenda, ["值守", "人员未定", "会上确定", "多余列"], agenda.anchors, 3, "P2.1")
        invalid = draft.model_copy(deep=True)
        invalid.sections[1].paragraphs[0].text = agenda.text.replace("两名", "三名")
        with self.assertRaises(RevisionIssue):
            _verify_conference_draft("fixture", invalid, data, self.fail, report)

    def test_final_conference_keeps_joint_responsibility_confirmed_by_the_action(self):
        action = "抽核10条待办记录，逐条核对办理状态和附件能否打开"
        quote = f"切换后，信息组和服务组共同{action}，两项都通过才恢复窗口录入"
        task = TaskItem(action=action, lead="信息组、服务组（共同）", support="", timing="",
                        deliverable="抽核结果（10条记录）", acceptance="办理状态和附件两项均通过",
                        status="confirmed", anchors=["S1.L1"])
        draft = Candidate(profile="plan-draft", title="系统切换工作方案（会商稿）", material_uses=[],
                          questions=[], review_notes=[], sections=[Section(title="抽核", paragraphs=[
                              Passage(text=f"{quote}。", kind="fact", anchors=["S1.L1"]),
                          ])], tasks=[task])

        for named in (False, True):
            item = draft.model_copy(deep=True)
            if named:
                item.tasks[0].action = f"信息组和服务组共同{action}"
            _complete_plan_fields(item, {"S1.L1": quote}, [], "")
            self.assertEqual(item.tasks[0].lead, "共同负责：信息组、服务组")
            report = {"stages": {}, "review": {"findings": [], "checked_ids": ["T1", "P1.1"]}}
            final = _verify_conference_draft("fixture", item, {"S1.L1": quote}, self.fail, report)
            self.assertEqual(final.tasks[0].lead, "共同负责：信息组、服务组")
        out = self.base / "joint-proof"
        materials = collect("写系统切换工作方案会商稿", (quote,), ())
        write_result(Run("draft", final, materials, {}, {"S1.L1": quote}), out)
        self.assertEqual(Document(out / "正文.docx").tables[0].cell(1, 2).text, "共同负责：信息组、服务组")
        for basis in (quote.replace("共同抽核", "拟共同抽核"), quote.replace("抽核10条", "复核10条"),
                      f"【材料一｜作废旧稿】\n{quote}", f"{quote}，分工待明确"):
            from app.agent import _explicit_joint_lead
            self.assertEqual(_explicit_joint_lead(task, [basis]), ("", ""))


    def test_word_preserves_explicit_joint_lead_and_neutral_responsibility(self):
        tasks = [TaskItem(action="核对台账", lead=lead, support="", timing="", deliverable="",
                          acceptance="", status="proposed", anchors=[])
                 for lead in ("甲、乙", "共同牵头：甲、乙")]
        result = self.run_fixture(profile="plan-draft", tasks=tasks)
        out = self.base / "responsibility"
        write_result(result, out)
        table = Document(out / "正文.docx").tables[0]
        self.assertEqual([row.cells[2].text for row in table.rows[1:]],
                         ["责任单位：甲、乙", "共同牵头：甲、乙"])

    def test_expert_team_binds_material_handoff_and_repairs_actual_table_cells(self):
        import re

        request = "请写试点工作方案和任务分工表。"
        source = "试点责任处室为甲处和乙处。发现重大差异应在评标报告中说明理由。"
        task = TaskItem(action="推进试点、说明重大差异", lead="", support="", timing="",
                        deliverable="", acceptance="", status="confirmed", anchors=["S1.L1"])
        initial = Candidate(profile="plan-draft", material_uses=[
            {"source_id": "U", "role": "requirements", "usable_anchors": []},
            {"source_id": "S1", "role": "facts", "usable_anchors": ["S1.L1"]},
        ], questions=[], title="试点工作方案", sections=[Section(title="任务", paragraphs=[
            Passage(text=source, kind="fact", anchors=["S1.L1"]),
        ])], tasks=[task], review_notes=[])
        stages = []

        def run(stale=False):
            def model(_model, prompt, schema):
                stages.append("ExpertBrief" if issubclass(schema, ExpertBrief) else
                              "ConferenceReview" if issubclass(schema, ConferenceReview) else schema.__name__)
                if issubclass(schema, ExpertBrief):
                    key = re.search(r"input_key: ([a-f0-9]{64})", prompt).group(1)
                    return (ExpertBrief if stale else schema)(input_key=key, items=[
                        {"text": "责任处室：甲处和乙处", "status": "known",
                         "anchors": ["S9.L1" if stale else "S1.L1"]},
                    ], outline=["试点任务", "责任分工"]), {}
                if schema is Candidate:
                    self.assertEqual("专家团素材单仅供核对原文" in prompt, not stale)
                    self.assertIn(source, prompt)
                    # The host binding key was mistaken for an extra unused source in a native run.
                    self.assertNotIn('"input_key"', prompt)
                    self.assertNotRegex(prompt, r"[a-f0-9]{64}")
                    return initial, {}
                inputs = json.loads(prompt.split("本轮数据：\n")[-1])
                if issubclass(schema, VerifiedConferenceDraft):
                    return schema(basis_key=inputs["basis_key"], findings=[dict(
                        finding_id="F1", resolved=True, basis_id="S1.L1", quote="试点责任处室为甲处和乙处",
                        reason="两处及报告已分别进入对应栏")]), {}
                self.assertNotIn("Word分工表视图", inputs)
                self.assertEqual(inputs.get("分工表", inputs.get("待修片段"))["T1"]["lead"], "")
                if issubclass(schema, ConferenceReview):
                    return schema(basis_key=inputs["basis_key"], checked_ids=["T1"], findings=[
                        {"target_ids": ["T1"], "basis_ids": ["S1.L1"], "level": "blocking",
                         "defect": "责任、成果漏填", "minimal_fix": "把甲乙及报告写进各自列"},
                    ]), {}
                self.assertEqual(schema.__name__, "BoundConferenceRepair")
                row = task.model_copy(update={"lead": "甲处、乙处", "deliverable": "在评标报告中说明理由"})
                self.assertEqual(schema.model_json_schema()["properties"]["new_tasks"]["maxItems"], 0)
                return schema(basis_key=inputs["basis_key"], edits={"T1": [row]}, new_tasks=[]), {}

            return run_task("fixture", request, [source], generate=model, review_generate=model, expert_team=True)

        result = run()
        self.assertEqual(stages, ["ExpertBrief", "Candidate", "ConferenceReview", "BoundConferenceRepair", "VerifiedConferenceDraft"])
        self.assertEqual(result.expert_workflow["status"], "completed")
        self.assertEqual(result.expert_workflow["brief"]["input_key"],
                         result.expert_workflow["brief"]["brief"]["input_key"])
        out = self.base / "expert-team"
        write_result(result, out)
        receipt = json.loads((out / "运行回执.json").read_text())
        self.assertGreaterEqual(receipt["artifact_build_seconds"], 0)
        table = Document(out / "正文.docx").tables[0]
        row = {header.text: cell.text for header, cell in zip(table.rows[0].cells, table.rows[1].cells, strict=True)}
        self.assertEqual(row["责任分工"], "责任单位：甲处、乙处")
        self.assertEqual(row["交付成果"], "在评标报告中说明理由")
        self.assertEqual(row["时间或依赖"], "待明确")
        fallback = run(stale=True)
        self.assertEqual(fallback.expert_workflow["status"], "partial")
        self.assertEqual(fallback.expert_workflow["brief"]["error"]["code"], "BRIEF_BINDING_INVALID")
        self.assertEqual(fallback.status, "draft")

    def test_text_only_notice_team_reviews_without_prebrief_or_task_table(self):
        source = "各单位：甲组上午到一楼培训，乙组下午到二楼交流。办公室，2026年9月3日。"
        request = "请把两个场次整理成培训通知。" + source
        draft = Candidate(profile="notice-draft", material_uses=[
            {"source_id": "U", "role": "facts", "usable_anchors": ["U.1"]},
        ], questions=[], title="培训通知", sections=[
            Section(title="", paragraphs=[Passage(text="各单位：", kind="fact", anchors=["U.1"])]),
            Section(title="场次安排", paragraphs=[
                Passage(text="第一场｜甲组｜上午｜二楼｜培训", kind="fact", anchors=["U.1"]),
                Passage(text="第二场｜乙组｜下午｜二楼｜交流", kind="fact", anchors=["U.1"]),
            ]),
            Section(title="", paragraphs=[
                Passage(text="办公室", kind="fact", anchors=["U.1"]),
                Passage(text="2026年9月3日", kind="fact", anchors=["U.1"]),
            ]),
        ], tasks=[], review_notes=[])

        def model(_model, prompt, schema):
            self.assertFalse(issubclass(schema, ExpertBrief))
            if schema is Candidate:
                self.assertIn("Notices and Administrative Communications", prompt)
                self.assertIn(source, prompt)
                return draft, {}
            context = json.loads(prompt.split("本轮数据：\n")[-1])
            self.assertNotIn("TASKS", context.get("可用检查位置", []))
            self.assertNotIn("Word分工表视图", context)
            if issubclass(schema, ConferenceReview):
                self.assertIn("Reviewing Notice Sessions and Required Details", prompt)
                return schema(basis_key=context["basis_key"], checked_ids=["P2.1", "P2.2"], findings=[
                    {"target_ids": ["P2.1"], "basis_ids": ["U.1"], "level": "blocking",
                     "defect": "甲组的地点串场", "minimal_fix": "二楼改为一楼"},
                ]), {}
            if issubclass(schema, VerifiedConferenceDraft):
                quote = context["可用依据"]["U.1"]
                return schema(basis_key=context["basis_key"], findings=[dict(finding_id="F1",
                    resolved=True, basis_id="U.1", quote=quote, reason="场次地点已改为一楼")]), {}
            return schema(basis_key=context["basis_key"], tasks=[], new_tasks=[], paragraphs=[
                {"target_id": "P2.1", "replacements": [
                    {"text": "第一场｜甲组｜上午｜一楼｜培训", "kind": "fact", "anchors": ["U.1"]},
                ]},
            ]), {}

        result = run_task("fixture", request, generate=model, review_generate=model, expert_team=True)
        self.assertNotIn("brief", result.expert_workflow)
        self.assertEqual(result.expert_workflow["status"], "completed")
        self.assertEqual(result.expert_workflow["draft_skill"], "gongwen-expert-notice")
        self.assertEqual(result.conference_review["status"], "changes_applied")
        out = self.base / "notice"
        write_result(result, out)
        word = Document(out / "正文.docx")
        self.assertEqual([p.text for p in word.paragraphs], ["培训通知", "各单位：", "场次安排", "办公室", "2026年9月3日"])
        self.assertEqual(word.paragraphs[1].alignment, 0)
        self.assertEqual(word.paragraphs[-1].alignment, 2)
        self.assertEqual(len(word.tables), 1)
        self.assertEqual([[c.text for c in row.cells] for row in word.tables[0].rows[1:]], [
            ["第一场", "甲组", "上午", "一楼", "培训"], ["第二场", "乙组", "下午", "二楼", "交流"],
        ])

    def test_notice_receipt_uses_fillable_cells_for_each_session(self):
        source = "办公室9月3日通知：上午一楼培训，下午二楼交流。报名请报姓名、场次、单位。"
        anchor = ["S1.L1"]
        draft = Candidate(profile="notice-draft", material_uses=[
            {"source_id": "U", "role": "requirements", "usable_anchors": []},
            {"source_id": "S1", "role": "facts", "usable_anchors": anchor},
        ], questions=[], title="培训通知", sections=[
            Section(title="场次安排", paragraphs=[
                Passage(text="培训｜各单位｜上午｜一楼｜培训", kind="fact", anchors=anchor),
                Passage(text="交流｜各单位｜下午｜二楼｜交流", kind="fact", anchors=anchor),
            ]),
            Section(title="", paragraphs=[
                Passage(text="办公室", kind="fact", anchors=anchor),
                Passage(text="9月3日", kind="fact", anchors=anchor),
            ]),
            Section(title="报名回执", paragraphs=[
                Passage(text="姓名｜场次｜单位", kind="fact", anchors=anchor),
            ]),
        ], tasks=[], review_notes=[])
        result = run_task("fixture", "请拟培训通知和报名回执。", [source],
                          generate=lambda *_: (draft, {}))
        out = self.base / "notice-receipt"
        write_result(result, out)
        word = Document(out / "正文.docx")
        self.assertEqual(word.paragraphs[-2].alignment, 2)
        self.assertEqual([[c.text for c in row.cells] for row in word.tables[1].rows], [
            ["姓名", "场次", "单位", "时间", "地点或线上渠道"],
            ["", "培训", "", "上午", "一楼"], ["", "交流", "", "下午", "二楼"],
        ])
        word.tables[1].cell(1, 0).text = "试填姓名"
        filled = self.base / "filled.docx"
        word.save(filled)
        self.assertEqual(Document(filled).tables[1].cell(1, 0).text, "试填姓名")
        self.assertEqual(Document(filled).tables[1].cell(1, 4).text, "一楼")

    def test_joint_responsibility_must_be_preserved_on_its_own_source(self):
        source = {
            "S1.L1": "完善系统：市交易中心及分中心持续推进。市交易中心负责转发指南。",
            "S1.L2": "组织培训：市交易中心及分中心共同负责。",
            "S1.L3": "组织交流：市交易中心负责向分中心转发操作指南。",
        }
        task = TaskItem(action="完善系统", lead="市交易中心", support="分中心", timing="",
                        deliverable="", acceptance="", status="confirmed", anchors=["S1.L1"])
        training = task.model_copy(update={"action": "组织培训", "lead": "市交易中心及分中心",
                                           "support": "", "anchors": ["S1.L2"]})
        candidate = self.run_fixture(profile="plan-draft", tasks=[task, training], sections=[
            Section(title="系统", paragraphs=[Passage(text="持续推进系统完善。", kind="fact", anchors=["S1.L1"])])
        ]).candidate
        with self.assertRaisesRegex(RevisionIssue, "市交易中心及分中心"):
            check_draft(candidate, source)
        candidate.tasks = [
            task.model_copy(update={"lead": "共同负责：市交易中心、分中心", "support": ""}),
            task.model_copy(update={"action": "转发指南", "support": ""}),
            training,
            task.model_copy(update={"action": "组织交流", "support": "", "anchors": ["S1.L3"]}),
        ]
        check_draft(candidate, source)

    def test_materials_alone_do_not_promote_clarification_to_a_draft(self):
        result = self.run_fixture(questions=["请确认这是要汇报已经发生的做法，还是部署后续任务。"])
        out = self.base / "blocked"
        write_result(result, out)
        self.assertEqual(result.status, "needs_input")
        self.assertIn("请确认", (out / "依据与待核.md").read_text())
        self.assertFalse((out / "正文.docx").exists())

    def test_requested_review_document_types_use_the_natural_word_entry(self):
        for profile in ("brief-draft", "speech-draft", "request-draft", "minutes-draft"):
            with self.subTest(profile=profile):
                result = self.run_fixture(profile=profile)
                out = self.base / profile
                write_result(result, out)
                self.assertEqual(result.status, "draft")
                self.assertEqual(Document(out / "正文.docx").paragraphs[-1].text, "本月延时办理20件。")

    def test_unsupported_attachment_stops_before_model(self):
        file = self.base / "原稿.ofd"
        file.touch()
        with self.assertRaisesRegex(Exception, "暂不能读取"):
            run_task("fixture", "帮我看看", files=[file], generate=lambda *_: self.fail("模型不应收到不完整材料"))

    def test_case_supplement_preserves_parent_and_rejects_other_sections(self):
        work = self.base / "记录.txt"
        work.write_text("村民参与议事。")
        supplement = self.base / "补数.csv"
        supplement.write_text("事项,指标,已核实数值,范围与来源\n艾津稻香节,纳入建议数,6条,活动方案\n")
        materials = collect("起草案例", (), [work])
        anchor = "S1.L1"
        sections = [Section(title=title, paragraphs=[
            Passage(text="村民参与议事。" * n, kind="fact", anchors=[anchor])
        ]) for title, n in zip(submission.SECTIONS, (35, 68, 1, 1, 1), strict=True)]
        candidate = Candidate(profile="experience-draft", title="议事协商", sections=sections, tasks=[],
                              questions=[], review_notes=[], material_uses=[
                                  {"source_id": "U", "role": "requirements", "usable_anchors": []},
                                  {"source_id": "S1", "role": "facts", "usable_anchors": [anchor]},
                              ])
        parent = self.base / "parent"
        write_result(Run("draft", candidate, materials, {}, {anchor: "村民参与议事。"},
                         submission={"profile": submission.PROFILE}), parent)
        receipt = json.loads((parent / "运行回执.json").read_text())

        def revise(effects):
            changes = [submission.Edit(section=section, paragraph=1,
                                       text=next(s for s in sections if s.title == section).paragraphs[0].text
                                       + "艾津稻香节将村民代表提出的6条建议纳入活动方案。")
                       for section in ("内容提要", effects)]
            return revise_case("fixture", "补数", [work, supplement], parent, receipt,
                               generate=lambda *_: (submission.Edits(changes=changes), {}))

        with self.assertRaisesRegex(RevisionIssue, "续改必须"):
            revise("案例背景")
        result = revise("成效与影响")
        self.assertEqual(result.candidate.sections[1], sections[1])
        self.assertEqual(result.candidate.sections[2], sections[2])
        self.assertEqual(result.candidate.sections[4], sections[4])
        self.assertEqual([c["anchor"] for c in result.submission["changes"]], ["S2.R2"] * 2)
        (parent / "正文.txt").write_text("手改后版本")
        with self.assertRaisesRegex(RevisionIssue, "父版正文.txt已变化"):
            revise("成效与影响")
