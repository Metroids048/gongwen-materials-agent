import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from docx import Document

from app import agent


REQUEST = "照附件写一份会前讨论稿。把召集单位和培训日期分别列，未定的会上再议，分工表能改，给我 Word。"
SOURCE = (
    "市档案局《业务协同办理指引》。\n"
    "第一条：实施主体应在办理前一周向服务中心提交并预约。服务中心负责提供操作培训。\n"
    "第二条：仍不能统一的，通过电子系统投票，主场和客场都负责监控和记录。成员原则上采用电子签名。\n"
    "第三条：超过2小时仍不能解除，由实施主体决定是否继续办理。\n"
)


def writer(_model, prompt, exchange):
    assert set(exchange.model_fields) == {"title", "purpose", "agenda"}
    return exchange(
        title="业务协同办理会前讨论稿",
        purpose="商议业务协同安排。",
        agenda=[
            dict(topic="召集单位", question="本次会议由哪个单位召集？", anchors=["U.1"]),
            dict(topic="培训日期", question="操作培训安排在哪一天？", anchors=["U.1"]),
        ],
    ), {}


class PolicyDiscussionTests(unittest.TestCase):
    def test_policy_writer_uses_explicit_low_effort_on_default_entry(self):
        with TemporaryDirectory() as temp:
            source = Path(temp) / "指引摘记.txt"
            source.write_text(SOURCE, encoding="utf-8")
            with patch.object(agent, "generate_policy_discussion", side_effect=writer) as selected:
                run = agent.run_task("fixture", REQUEST, files=[source])
        self.assertEqual(run.status, "draft", run.note)
        selected.assert_called_once()
        self.assertEqual(agent.generate_policy_discussion.keywords["reasoning_effort"], "low")

    def test_default_once_resolves_roles_and_preserves_conditions_in_word(self):
        generate = Mock(side_effect=writer)
        never_review = Mock(side_effect=AssertionError("default workflow must not call a review model"))
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "指引摘记.txt"
            source.write_text(SOURCE, encoding="utf-8")
            run = agent.run_task("fixture", REQUEST, files=[source], generate=generate, review_generate=never_review)
            self.assertEqual(run.status, "draft", run.note)
            generate.assert_called_once()
            never_review.assert_not_called()
            self.assertEqual([use.source_id for use in run.candidate.material_uses], ["U", "S1"])
            self.assertEqual(run.candidate.material_uses[1].usable_anchors,
                             ["S1.L1", "S1.L2", "S1.L3", "S1.L4"])
            agent.write_result(run, folder / "good")
            document = Document(io.BytesIO((folder / "good" / "正文.docx").read_bytes()))
            header, *values = [[cell.text for cell in row.cells] for row in document.tables[-1].rows]
            rows = [dict(zip(header, row, strict=True)) for row in values]
            for action, lead, when in (("预约", "实施主体", "办理前一周"), ("投票", "", "仍不能统一的"),
                                       ("监控和记录", "主场和客场", "仍不能统一的"), ("原则上", "成员", ""),
                                       ("决定是否继续", "实施主体", "超过2小时仍不能解除")):
                self.assertTrue(any(action in row["工作任务"] and row["牵头或共同负责"] == lead and
                                    row["时间或依赖"] == when for row in rows), action)

    def test_export_rechecks_rows_even_without_prompt_profile_metadata(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "指引摘记.txt"
            source.write_text(SOURCE, encoding="utf-8")
            run = agent.run_task("fixture", REQUEST, files=[source], generate=writer)
            self.assertEqual(run.status, "draft", run.note)
            del run.expert_workflow["writer_profile"]
            mutations = [
                ("预约", "lead", "实施主体或代理机构"),
                ("操作培训", "acceptance", "培训签到表"),
                ("投票", "lead", "主场和客场"),
                ("原则上", "action", "采用电子签名"),
                ("继续办理", "timing", "超过2小时"),
            ]
            for word, field, change in mutations:
                with self.subTest(field=field, word=word):
                    row = next(row for row in run.candidate.tasks if word in row.action)
                    before = getattr(row, field)
                    setattr(row, field, change)
                    with self.assertRaisesRegex(agent.RevisionIssue, "条款分工与原文不一致"):
                        agent.write_result(run, folder / "bad")
                    self.assertFalse((folder / "bad").exists())
                    setattr(row, field, before)
            item = run.candidate.sections[1].paragraphs[1]
            before = item.text
            item.text = "培训日期：已经安排。届时如何登记？"
            with self.assertRaisesRegex(agent.RevisionIssue, "请把待定事项逐项列为会商问题"):
                agent.write_result(run, folder / "bad")
            item.text = before
            run.candidate.sections[-1].paragraphs.pop()
            with self.assertRaisesRegex(agent.RevisionIssue, "稿内条款与原文不一致"):
                agent.write_result(run, folder / "bad")

    def test_vote_record_handoff_keeps_the_assignee_open(self):
        request = REQUEST + " 投票记录怎么交也带上。"

        def assign_handoff(model, prompt, exchange):
            draft, metadata = writer(model, prompt, exchange)
            draft.agenda.append(agent.PolicyAgendaItem(
                topic="投票记录交接", question="以何种方式由主场与客场归集、传递？", anchors=["U.1"]))
            return draft, metadata

        with TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "指引摘记.txt"
            source.write_text(SOURCE, encoding="utf-8")
            run = agent.run_task("fixture", request, files=[source], generate=assign_handoff)
            self.assertEqual(run.status, "draft", run.note)
            agent.write_result(run, folder / "good")
            document = Document(folder / "good" / "正文.docx")
            self.assertTrue(any("投票记录移交：由谁归集、向谁移交，采用什么形式？" in p.text
                                for p in document.paragraphs))
            run.candidate.sections[1].paragraphs[-1].text = "投票记录交接：以何种方式由主场与客场归集、传递？"
            with self.assertRaisesRegex(agent.RevisionIssue, "请留待会商确定"):
                agent.write_result(run, folder / "bad")
            self.assertFalse((folder / "bad").exists())

            source.write_text(SOURCE + "第四条：投票记录由主场负责移交。", encoding="utf-8")
            materials = agent.collect(request, [], [source], split_inline=True)
            self.assertIsNone(agent.policy_actions.source_plan(request, materials)["open_record_handoff"])

    def test_reference_and_multi_source_inputs_keep_normal_writer(self):
        sources = agent.collect(REQUEST, [SOURCE], (), split_inline=True)
        self.assertIsNotNone(agent.policy_actions.source_plan(REQUEST, sources))
        self.assertIsNone(agent.policy_actions.source_plan(REQUEST + "（附件仅供参考）", sources))
        sources.append(dict(id="S2", blocks={"S2.L1": "另一份材料。"}))
        self.assertIsNone(agent.policy_actions.source_plan(REQUEST, sources))
