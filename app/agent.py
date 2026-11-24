"""Natural-language entry for material intake and source-grounded first drafts."""

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, create_model

from app import minute_actions, notice_scope, number_basis, policy_actions, submission
from app.draft_layout import (
    NOTICE_COLUMNS, NOTICE_ISSUER, NOTICE_RECEIPT, OPINION_FEEDBACK, PLAN_AGENDA, PLAN_COMPARISON, PLAN_TABLE_COLUMNS, TASK_FIELD_LABELS,
    _notice_sections, _notice_tail_role, _task_table, plan_table, render, table_row,
)
from app.revision import deadline, native_review
from app.materials import collect
from app.revision.document import RevisionIssue, require
from app.revision.letterhead import known_source, revise_letterhead
from app.revision.natural import revise
from app.revision.opinions import review_sources, revise_grouped, revise_opinions, structured_opinions
from app.runtime.codex import ask_codex

ROOT = Path(__file__).resolve().parents[1]
# 主笔要一次完成材料梳理、成稿和表格；模型提前完成时立即返回。
generate_intake = partial(ask_codex, disable_plugins=True)
generate_policy_discussion = partial(generate_intake, reasoning_effort="low")
generate_review = partial(ask_codex, reasoning_effort="medium", disable_plugins=True)
EXPERT = "skills/gongwen-expert/SKILL.md"
TEAM = ROOT / "skills/gongwen-expert-team"
DRAFT_SKILLS = {
    "summary-draft": "gongwen-expert-experience",
    "exchange-draft": "gongwen-expert-experience",
    "plan-draft": "gongwen-expert-plan",
    "experience-draft": "gongwen-expert-experience",
    "notice-draft": "gongwen-expert-notice",
    "brief-draft": "gongwen-expert-review-drafts",
    "speech-draft": "gongwen-expert-review-drafts",
    "request-draft": "gongwen-expert-review-drafts",
    "minutes-draft": "gongwen-expert-review-drafts",
    "opinion-draft": "gongwen-expert-review-drafts",
}
DRAFT_GENRES = {
    "工作方案": "plan-draft", "实施方案": "plan-draft", "会商方案": "plan-draft", "会商稿": "plan-draft",
    "会前讨论稿": "plan-draft", "会前沟通稿": "plan-draft",
    "经验材料": "experience-draft", "阶段总结": "summary-draft", "交流材料": "exchange-draft",
    "通知": "notice-draft", "公告": "notice-draft", "通告": "notice-draft",
    "简报": "brief-draft", "讲话稿": "speech-draft", "部署讲话": "speech-draft",
    "请示": "request-draft", "会议纪要": "minutes-draft", "征求意见稿": "opinion-draft",
}
DRAFT_GENRE = re.compile("|".join(DRAFT_GENRES))
DRAFT_DIRECTIVE = re.compile(
    rf"起草|拟写|撰写|写成|写一下|整理成|合成|文种\s*(?:是|为|[：:])|[写拟](?=\s*(?:一?[份个篇则]|[《“]|{DRAFT_GENRE.pattern}))"
)
SHORT_DRAFT_START = r"(?:麻烦|请)?\s*(?:帮我|给我)?\s*(?:起草|拟写|撰写|写|拟)\s*(?:一?[份个篇则]\s*)?"
BARE_DRAFT = re.compile(rf"{SHORT_DRAFT_START}(?P<genre>公文|材料|方案|公告|{DRAFT_GENRE.pattern})")
BARE_TRAFFIC_ANNOUNCEMENT = re.compile(
    rf"{SHORT_DRAFT_START}(?:关于)?(?:[\u4e00-\u9fffA-Za-z· ]{{1,30}}期间(?:的)?)?"
    r"(?:交通管制|交通限行|道路封闭|临时封路)(?:的)?(?:公告|通告|通知)"
)
REMOTE_WORK_NOTICE = re.compile(r"(?:居家|远程|在家)办公[^。！？\n]{0,12}(?:通知|通告|公告)")
RELATIVE_WORK_DATES = re.compile(
    r"(?:开始|开幕|会前|会议前|活动前)[^。\n]{0,8}前?一[日天].{0,10}"
    r"(?:结束|闭幕|会后|会议后|活动后)[^。\n]{0,8}后?一[日天]"
)
CALENDAR_DATE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{4}[-/]\d{1,2}[-/]\d{1,2}")
PROVISIONAL_NOTICE = re.compile(r"模板|虚构|演练|先出.{0,10}(?:初稿|草稿)|审阅稿|日期(?:先|暂)?(?:留空|待确认|未定|没定)|按相对日期")
KEEP_RELATIVE_DATES = re.compile(
    r"(?:按|(?:就|只|仅|先|暂)?用|保留|沿用|使用)相对(?:日期|表述)|"
    r"(?:别|不要|不必|无需|不)换算(?:成|为)?(?:具体)?日期|月份(?:先|暂)?(?:不填|留空)"
)
EXPLICIT_REHEARSAL_DOCUMENT = re.compile(r"(?:演练|模拟)(?:用(?:的)?|测试)[^。！？；\n]{0,30}(?:稿|通知|Word)", re.I)
EXPLICIT_RETROSPECTIVE_DRAFT = re.compile(r"复盘用(?:的)?[^。！？；;\n]{0,20}审阅稿")
EXPLICIT_WORD_REVIEW_DRAFT = re.compile(r"(?<![a-z])Word\s*审阅稿", re.I)
EVENT_DATE = re.compile(r"(?:(?P<year>\d{4})[ \t]*年[ \t]*)?(?P<month>\d{1,2})[ \t]*月[ \t]*(?P<day>\d{1,2})[ \t]*[日号]")
DATE_WITH_WEEKDAY = re.compile(
    r"(?P<year>\d{4})年\s*(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})[日号]\s*[（(]?\s*"
    r"(?:星期|周|礼拜)(?P<weekday>[一二三四五六日天])[）)]?"
)
EVENT_RANGE = re.compile(
    r"(?<!\d)(?:(?P<year>\d{4})[ \t]*年[ \t]*)?(?P<month>\d{1,2})[ \t]*月[ \t]*"
    r"(?P<day>\d{1,2})[ \t]*(?:[日号])?\s*(?:至|到|[-－–—~～]+)\s*"
    r"(?:(?P<end_year>\d{4})[ \t]*年[ \t]*)?(?:(?P<end_month>\d{1,2})[ \t]*月[ \t]*)?"
    r"(?P<end_day>\d{1,2})[ \t]*[日号]"
)
EVENT_DAY_PAIR = re.compile(
    EVENT_DATE.pattern + r"\s*(?:、|和|及|与)\s*(?P<second_day>\d{1,2})[日号]"
)
EVENT_SINGLE = re.compile(
    r"(?:会期为|会期是|(?:会议|活动)(?:定|将)?于)\s*" + EVENT_DATE.pattern + r"(?!\s*(?:至|到|[-－–—~～]))"
)
EVENT_RANGE_LABEL = re.compile(r"(?:会期|会议|活动|峰会|论坛)(?:时间|日期|会期)?\s*(?:是|为|定于|将于|于|[：:])?\s*$")
EVENT_REPLY_PREFIX = re.compile(r"[。！？!?；;\n][ \t]*(?:(?:是|就是|应该是|为|定在|按|就按)[ \t]*)?$")
EVENT_REPLY_SUFFIX = re.compile(r"[ \t]*(?:[。！？!?；;\n]|$)")
CONTINUOUS_WORK_DATES = re.compile(
    r"(?:从|自)[^。！？\n]{0,16}(?:会前|开始前|开幕前)[^。！？\n]{0,24}(?:持续|至|到)"
    r"|连续(?:居家|远程|在家|执行|实施|[两三四五六七八九十2-9]天)"
)
SEPARATE_WORK_DATES = re.compile(
    r"(?:各|分别)[一1][日天]|这(?:两个日期|[两二2]天)|(?:两|二|2)天分别(?:执行|安排|实施)"
    r"|(?:这|只(?:在|安排|执行)?[^。！？\n]{0,35})(?:两|二|2)[个]?[日天]"
)
DAY_RANGE_REPLY = re.compile(
    r"(?:^|\n)[ \t]*(?:[1-9]|[12]\d|3[01])[ \t]*[-－–—~～至][ \t]*"
    r"(?:[1-9]|[12]\d|3[01])(?:日|号)?[。！.!]?[ \t]*$"
)
QUICK_NOTICE_EXCEPTIONS = re.compile(
    r"分工表|回执|(?:多|[两二三四五六七八九十2-9])个?场次|逐条|红头|套版|联审|核稿|审校|比较|改稿|修改|重写"
)
PUBLIC_CONTROL_NOTICE = re.compile(r"交通管制|交通限行|道路封闭|临时封路|临时限行|封闭道路")
NOTICE_APPROVAL = re.compile(r"经(?:研究(?:决定|同意)?|批准|审议(?:通过)?|同意|决定|市政府(?:研究|批准|同意))")
NOTICE_ISSUED_BASIS = re.compile(r"(?:根据|按照)[\u4e00-\u9fff]{1,28}?政府通知安排(?=[，,。；;]|$)")
NOTICE_MEDIA_ASSIGNMENT = re.compile(
    r"请[^。；！？\n]{0,80}《(?P<outlet>[^《》\n]{2,40})》"
    r"[^。；！？\n]{0,15}(?:栏目|记者|编辑部|节目)[^。；！？\n]{0,12}(?P<action>宣传推广|宣传|报道|播出)"
)
REMOTE_WORK = notice_scope.REMOTE_WORK
WHOLE_EVENT = notice_scope.WHOLE_EVENT
DOCUMENT_CITATION = re.compile(
    r"《(?P<title>[^《》\n]{2,100})》\s*[（(]?(?P<number>[\u4e00-\u9fa5]{1,24}[〔\[［]\d{4}[〕\]］]\s*\d+\s*号)[）)]?"
)
DRAFT_REFERENCES = {
    "gongwen-expert-experience": "skills/gongwen-materials/references/experience.md",
    "gongwen-expert-plan": "skills/gongwen-materials/references/work-plan.md",
}
TEAM_RECIPES = {
    "summary-draft": "经验材料提炼复核",
    "exchange-draft": "经验材料提炼复核",
    "plan-draft": "方案会商编审",
    "experience-draft": "经验材料提炼复核",
    "notice-draft": "通知场次与要素校验",
    "brief-draft": "审阅稿编审",
    "speech-draft": "审阅稿编审",
    "request-draft": "审阅稿编审",
    "minutes-draft": "审阅稿编审",
    "opinion-draft": "审阅稿编审",
}
MEETING = re.compile(r"会商|会前|会上|碰头|碰一下|碰一碰|讨论稿|议题")
UNCERTAIN = re.compile(r"待(?:明确|定|核|确认|会商)|未(?:明确|定|指定|确定)|没(?:有)?(?:定|明确|指定|说)|尚未")
NUMBERS = re.compile(r"\d+(?:[.,]\d+)*(?:%|‰|万|亿)?")
PARAGRAPH_LIST_MARKER = re.compile(r"^\s*[1-9]\d*[.．]\s+(?=[\u4e00-\u9fff])")
HEADING_LIST_MARKER = re.compile(r"^\s*(?:[一二三四五六七八九十]+、|[1-9]\d*[.．、])\s*")
PENDING_LIST = re.compile(
    r"(?:没定|未定|未明确)的(?P<labels>[\u4e00-\u9fff、]{2,90})[，,]\s*(?:逐项|分别|单列)"
)
PENDING_AT_MEETING = re.compile(
    r"(?:^|[。；，,])\s*(?P<labels>[\u4e00-\u9fff]{2,20}(?:、[\u4e00-\u9fff]{2,20}?)+?)"
    r"(?:都)?(?:还)?(?:没定|未定|未明确)[，,][^。；]{0,20}(?:逐项|分别|单列)"
)
NAMED_ONLINE_TRAINING = re.compile(r"开展(?P<outcome>[\u4e00-\u9fff]{2,24}在线培训)(?=[，。；、]|$)")
JOINT_CENTER = re.compile(
    r"(?:^|[：:。；;，,])\s*(?P<center>[\u4e00-\u9fff]+?中心)[及与和、](?P<branch>各?分中心)"
    r"(?=持续推进|(?:共同|一同|联合)?(?:负责|牵头)|为[^，。；]+管理机构)"
)
ANCHOR = re.compile(r"(?<![A-Za-z0-9_.])(?:U|P|S[0-9]+)\.(?:[TW][0-9]+\.R[0-9]+|G[0-9]+\.L[0-9]+|[LPR]?[0-9]+)(?![A-Za-z0-9_.])")
PROFILES = {
    "experience-draft": "经验材料起草", "plan-draft": "工作方案起草", "notice-draft": "通知起草",
    "summary-draft": "阶段总结起草", "exchange-draft": "交流材料起草",
    "brief-draft": "调研简报", "speech-draft": "部署讲话",
    "request-draft": "请示审阅稿", "minutes-draft": "会议纪要审阅稿",
    "opinion-draft": "征求意见审阅稿",
    "experience-revise": "经验材料改稿", "plan-revise": "工作方案改稿", "unclear": "待确认",
}


ROLE_NAMES = {
    "facts": "本次事实", "reference": "仅作参考", "requirements": "本轮要求",
    "draft": "待改原稿", "mixed": "混合材料", "unused": "未使用",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MaterialPurpose(StrictModel):
    role: Literal["facts", "reference", "requirements", "draft", "mixed", "unused"]
    usable_anchors: list[str] = Field(description="只填本轮材料 blocks 对象的原始键，如 U.1、S1.L1；不得附加冒号、原文、摘要或说明。无可用依据时填空数组。")


class MaterialUse(MaterialPurpose):
    source_id: str


class Passage(StrictModel):
    text: str
    kind: Literal["fact", "proposal"]
    anchors: list[str] = Field(description="依据编号数组，每项只能是本轮材料 blocks 的一个原始键，如 U.1、S1.L1；不含冒号、原文或说明。")


class Section(StrictModel):
    title: str
    paragraphs: list[Passage]


def _task_exchange_schema(schema):
    # Old saved drafts may omit the new cells; strict model requests must still list every field.
    schema["required"] = list(schema["properties"])
    for value in schema["properties"].values():
        value.pop("default", None)


class TaskItem(StrictModel):
    model_config = ConfigDict(json_schema_extra=_task_exchange_schema)
    action: str
    lead: str
    support: str
    timing: str
    deliverable: str
    acceptance: str
    contact: str = Field(default="", description="本行联系人，仅用材料明确的人选；未定或用户要求留空时填空字符串。")
    phone: str = Field(default="", description="本行联系电话，仅用材料明确的号码；未定或用户要求留空时填空字符串。")
    status: Literal["confirmed", "proposed"]
    anchors: list[str] = Field(description="依据编号数组，每项只能是本轮材料 blocks 的一个原始键，如 U.1、S1.L1；不含冒号、原文或说明。")


FOLLOW_UP_SECTIONS = {"summary-draft": "后续复核安排", "exchange-draft": "交流建议"}


class Candidate(StrictModel):
    profile: Literal[
        "experience-draft", "summary-draft", "exchange-draft", "plan-draft", "notice-draft", "brief-draft", "speech-draft",
        "request-draft", "minutes-draft", "opinion-draft", "experience-revise", "plan-revise", "unclear",
    ]
    material_uses: list[MaterialUse]
    questions: list[str]
    title: str
    sections: list[Section]
    tasks: list[TaskItem]
    review_notes: list[str]


class OperationalMinutesDraft(StrictModel):
    title: str
    purpose: str
    sections: list[Section]
    questions: list[str]
    review_notes: list[str]


class PolicyAgendaItem(StrictModel):
    topic: str
    question: str
    anchors: list[str] = Field(description="只填材料 blocks 中的原始键，如 U.1、S1.L1。")


class PolicyDiscussionDraft(StrictModel):
    title: str
    purpose: str
    agenda: list[PolicyAgendaItem]


class ReviewFinding(StrictModel):
    target_ids: list[str]
    basis_ids: list[str]
    level: Literal["blocking", "pending", "advisory"]
    defect: str
    minimal_fix: str


class ConferenceReview(StrictModel):
    basis_key: str
    checked_ids: list[str]
    findings: list[ReviewFinding]


class FrozenFinding(StrictModel):
    target: str
    level: Literal["blocking", "pending", "advisory"]
    defect: str
    anchor: str
    quote: str
    before: str
    after: str


class FrozenReview(StrictModel):
    draft_sha256: str
    findings: list[FrozenFinding]


class ParagraphEdit(StrictModel):
    target_id: str
    replacements: list[Passage]


class TaskEdit(StrictModel):
    target_id: str
    replacements: list[TaskItem]


class ConferenceRepair(StrictModel):
    basis_key: str
    paragraphs: list[ParagraphEdit]
    tasks: list[TaskEdit]
    new_tasks: list[TaskItem]


class FindingVerdict(StrictModel):
    finding_id: str
    resolved: bool
    basis_id: str
    quote: str
    reason: str


class VerifiedConferenceDraft(StrictModel):
    basis_key: str
    findings: list[FindingVerdict]


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _can_hold_for_meeting(instruction):
    meeting_choice = re.search(r"会上.{0,6}(?:拍板|议定|决定|确定)|列.{0,6}(?:会商议题|待议事项)", instruction)
    review_with_blanks = (re.search(r"先(?:出|写|交|给).{0,8}审阅稿", instruction)
                          and re.search(r"(?:未定|争议|待确认).{0,8}(?:单列|另列)|单列待确认", instruction))
    return bool(meeting_choice or review_with_blanks)


def _meeting_slots(plan, instruction):
    if not plan or not plan["agenda"]:
        return {}
    allow_open_owners = _can_hold_for_meeting(instruction)
    lists_agenda = re.search(r"会上待定.{0,16}议题表|争议.{0,16}会商议题表", instruction)
    if not allow_open_owners and not lists_agenda:
        return {}
    # Each optional slot names a single field of the source-derived plan.
    slots = {}
    if allow_open_owners:
        for row in plan["actions"]:
            if not row["lead"]:
                key = row["anchors"][0] + "/lead"
                slots.setdefault(key, []).append(row["action"])
    for topic, (declaration, evidence, _) in plan["agenda"].items():
        slots[evidence + "/" + declaration + "/agenda"] = [topic]
    for anchor in _quoted_opinions(plan):
        slots[anchor + "/proposal"] = ["该段试点原意见"]
    return slots


def _bound_meeting_questions(candidate, plan, slots, bindings):
    if candidate.profile != "plan-draft" or not candidate.sections:
        return []
    source = plan["decisions"] | plan["feedback"]
    deferred = []
    for item in bindings:
        question, slot = item["question"].strip(), item["slot"]
        if slot not in slots or question not in candidate.questions:
            continue
        basis = " ".join(source[anchor] for anchor in slot.split("/")[:-1])
        if numeric_tokens(question) <= numeric_tokens(basis):
            candidate.questions.remove(question)
            deferred.append({"question": question, "slot": slot})
    return deferred


def _intake_for_review(candidate, slots, metadata):
    if not slots or not candidate.tasks or candidate.profile != "plan-draft":
        return None
    # An old writer could supply unreferenced editorial notes instead of the agenda.
    # Keep each one as an individually checked issue. Real sourced agenda rows, trial
    # opinions and operational paragraphs remain subject to the ordinary draft checks.
    notes = [s for s in candidate.sections if s.paragraphs
             and all(p.kind == "proposal" and not p.anchors for p in s.paragraphs)]
    bindings = metadata.get("intake_bindings")
    if not notes and (not bindings or not candidate.questions):
        return None
    if not any(p.kind == "fact" and p.anchors for s in candidate.sections for p in s.paragraphs):
        return None
    bound = {item["question"]: item["slot"] for item in bindings or []}
    items = {f"Q{i}": {"text": q, "writer_slot": bound.get(q)} for i, q in enumerate(candidate.questions, 1)}
    for section in notes:
        for p in section.paragraphs:
            items[f"N{len(items)+1}"] = {"text": p.text, "writer_slot": None}
        candidate.sections.remove(section)
    candidate.questions = []
    return {"slots": slots, "items": items}


def _defer_agenda_questions(candidate, instruction, ledger, materials):
    """Keep a legacy question on the agenda only when the user, source and drafted row agree."""
    if not candidate.questions or candidate.profile != "plan-draft" or not candidate.tasks or not _can_hold_for_meeting(instruction):
        return []
    passages = [p for section in candidate.sections for p in section.paragraphs if p.kind == "proposal"]
    blocks = {key: text for item in materials if ledger.get(item["id"], {}).get("用途") in {"试点材料", "会议纪要"}
              for key, text in item["blocks"].items()}
    minutes = [text for key, text in blocks.items() if ledger[key.split(".")[0]]["用途"] == "会议纪要"]
    general = {"是否", "时间", "安排", "需要", "具体", "确定", "事项", "会上", "提前", "相关"}
    topics = []
    proposals = []
    for row in passages:
        label = re.split(r"[｜：:]", row.text, maxsplit=1)[0]
        sources = [blocks[a] for a in row.anchors if a in blocks]
        if not sources or len(sources) != len(row.anchors):
            continue
        for anchor in row.anchors:
            if (ledger.get(anchor.split(".")[0], {}).get("用途") == "试点材料"
                    and "建议" in blocks[anchor] and "建议" in row.text and label != row.text):
                proposals.append((label, row.text, blocks[anchor]))
            for match in re.finditer(r"([^，。；：]{2,50}?)(?:都|均)?(?:拿到会上定|留待会上(?:议定|确定)|由会议决定)", blocks.get(anchor, "")):
                for subject in re.split(r"以及|和|及|与|、", match[1]):
                    pairs = {subject[n:n+2] for n in range(len(subject)-1)
                             if subject[n:n+2] in label} - general
                    if pairs:
                        topics.append((pairs, label, row.text, sources))
    deferred = []
    for question in candidate.questions[:]:
        clauses = [c.strip("。 ，,") for c in re.split(r"[？?；;]|以及|另外|此外", question) if c.strip("。 ，,")]
        declared = clauses and any(all(any(term in clause for term in topic) for clause in clauses)
                                   and numeric_tokens(question) <= numeric_tokens("".join(sources))
                                   and _agenda_wording_supported(question, label, row, sources)
                                   for topic, label, row, sources in topics)
        if declared or _sourced_proposed_responsibility(question, proposals, minutes):
            deferred.append(question)
            candidate.questions.remove(question)
    return deferred


def _source_words_cover(phrase, text):
    if phrase in text:
        return True
    covered = set()
    for i in range(len(phrase) - 1):
        if phrase[i:i + 2] in text:
            covered.update((i, i + 1))
    return len(covered) == len(phrase)


def _agenda_wording_supported(question, label, row, sources):
    # A shared topic such as “公告” cannot excuse a new action or a different date.
    basis = "\n".join([row, *sources]).replace("人员", "人")
    if "人选" in label:
        question = question.replace("联系方式", "")
    phrasing = r"[^\w]+|是否|如何|具体|确定|明确|议定|会上|未定|还是|或者|以及|另外|此外|[若需或和按请]"
    parts = filter(None, re.split(phrasing, question.replace("人员", "人")))
    return all(_source_words_cover(part, basis) for part in parts)


def _sourced_proposed_responsibility(question, proposals, minutes):
    # The role may be pending even when the distinct operational assignment is settled.
    # The candidate supplies a topic, the trial source must corroborate the proposed actor.
    match = re.fullmatch(r"([^，,。；;？?（）()]{2,24})是否由([^，,。；;？?（）()]{3,30})"
                         r"(?:[（(](?:当前)?纪要(?:仅)?明确([^，,。；;（）()]+)[）)])?"
                         r"(?:[，,]\s*(?:需|请|待)会上(?:确认|议定|明确|决定))?[。？?]?", question.strip())
    if not match or (match[3] and not any(match[3] in text for text in minutes)):
        return False
    subject, action = match[1], match[2]
    for label, row, quote in proposals:
        if subject not in label or not _source_words_cover(subject, quote):
            continue
        for i in range(2, len(action) - 1):
            actor, verb = action[:i], action[i:]
            recording = (subject.endswith(("记录", "登记")) and verb in {"填写", "登记", "记录"}
                         and any("由" + actor + v in quote for v in ("填写", "登记", "记录")))
            if ("由" + actor in row and verb in label
                    and ("由" + actor + verb in quote or recording)):
                return True
    return False


def _confirmed_checks(blocks):
    """Parse only explicit assignments to checks. Time and subject belong to the same action."""
    facts = []
    checks = r"抽核|抽查|核查|巡检|复查"
    for anchor, quote in blocks.items():
        for sentence in re.split(r"[。；;\n]", quote):
            if re.search(r"建议|拟|未定|待议|有争议|已作废", sentence):
                continue
            for clause in re.split(r"[，,]", sentence):
                duty = re.fullmatch(r"([^，。；]{2,24}?)负责(.+)", clause.strip())
                if not duty:
                    continue
                for match in re.finditer(r"(上线后|投用后|投产后|恢复后|运行后)(?:的)?(" + checks + ")", duty[2]):
                    facts.append(dict(主体=duty[1], 阶段=match[1], 动作=match[2], 依据=anchor))
            joint = re.match(r"([^，,]{1,12}后)[，,](?:由)?([^，,]{2,20}(?:和|与|及|、)[^，,]{2,20}?)"
                             r"(?:共同|联合)(" + checks + r")([^，,]+)", sentence.strip())
            if joint:
                facts.append(dict(主体=joint[2], 阶段=joint[1], 动作=joint[3], 对象=joint[4], 依据=anchor))
    return facts


def _ground_confirmed_checks(candidate, checks):
    """Supply source-bound single-owner tasks and distinguish them from a separately timed joint check."""
    for fact in checks:
        if "对象" in fact:
            continue
        actor, stage, action, anchor = (fact[k] for k in ("主体", "阶段", "动作", "依据"))
        if any(o["阶段"] == stage and o["动作"] == action and o["主体"] != actor for o in checks):
            continue
        others = [other for other in checks if other is not fact and other["阶段"] != stage
                  and other["主体"] != actor and other["动作"] == action and "对象" in other]
        for section in candidate.sections:
            for passage in section.paragraphs:
                # Resolve only an express, spurious conflict between these two separately assigned checks.
                if (others and actor in passage.text and action in passage.text
                        and re.search(r"两种口径|口径不一|归属有争议|存在冲突", passage.text)
                        and any(all(name in passage.text for name in re.split(r"和|与|及|、", o["主体"])) for o in others)):
                    independent = next(o for o in others if all(
                        name in passage.text for name in re.split(r"和|与|及|、", o["主体"])))
                    if len([chunk for chunk in re.split(r"[。！？]", passage.text) if chunk.strip()]) == 1:
                        passage.text = (f"{independent['阶段']}，{independent['主体']}共同{action}{independent['对象']}。"
                                        f"{actor}负责{stage}的{action}。")
                        passage.kind = "fact"
                        passage.anchors = list(dict.fromkeys([independent["依据"], anchor]))
                        # A settled item belongs with implementation, outside the meeting's pending topics.
                        target = next((s for s in candidate.sections if s is not section and stage in s.title), None)
                        if target and re.search(r"待议|待定|会上|拍板", section.title):
                            target.paragraphs.append(passage)
        for section in candidate.sections:
            if re.search(r"待议|待定|会上|拍板", section.title):
                section.paragraphs = [p for p in section.paragraphs if not any(
                    p is moved for other in candidate.sections if other is not section for moved in other.paragraphs)]
        candidate.sections = [s for s in candidate.sections if s.paragraphs]
        rows = [row for row in candidate.tasks if action in row.action and stage in (row.action + row.timing)
                and not any(other["对象"] in row.action for other in others)]
        if len(rows) == 1:
            row = rows[0]
            row.lead = actor
            row.status = "confirmed"
            row.anchors = list(dict.fromkeys([*row.anchors, anchor]))
        elif not rows:
            candidate.tasks.append(TaskItem(action=f"{stage}{action}", lead=actor, support="", timing=stage,
                                             deliverable="", acceptance="", status="confirmed", anchors=[anchor]))
        if not any(p.kind == "fact" and actor in p.text and stage in p.text and action in p.text
                   for s in candidate.sections for p in s.paragraphs):
            target = next((s for s in candidate.sections if stage in s.title), None)
            if target is None:
                target = Section(title=stage, paragraphs=[])
                index = next((i for i,s in enumerate(candidate.sections) if re.search(r"待议|待定|会上|拍板", s.title)),
                             len(candidate.sections))
                candidate.sections.insert(index, target)
            target.paragraphs.append(Passage(text=f"{actor}负责{stage}的{action}。", kind="fact", anchors=[anchor]))


def _source_plan(ledger, blocks, instruction=""):
    """Use a source-only table when the operational minutes can be consumed in full."""
    if sum(m["用途"] == "会议纪要" for m in ledger.values()) != 1:
        return None
    actions, decisions, feedback = [], {}, {}
    for key, record in ledger.items():
        if record["用途"] == "试点材料":
            feedback.update({a: blocks[key][a] for a, use in record["片段"].items() if use != "失效内容"})
        elif record["用途"] == "会议纪要":
            clean = {a: blocks[key][a] for a, use in record["片段"].items() if use == "纪要决定"}
            if set(clean) != set(record["片段"]):
                return None
            grouped = minute_actions.action_rows(clean)
            if not grouped or grouped.keys() != clean.keys():
                return None
            for rows in grouped.values():
                actions.extend(rows)
            decisions.update(clean)
        elif key != "U" and record["用途"] != "只参考结构，禁作事实依据":
            return None
    if not actions:
        return None
    agenda = {}
    for anchor, line in feedback.items():
        declaration = re.fullmatch(r"([^。，；：]+?)(?:都|均)?(?:拿到会上定|留待会上(?:议定|确定)|由会议决定)。?", line)
        if not declaration:
            continue
        for topic in re.split(r"以及|和|及|与|、", declaration[1]):
            terms = {topic[n:n+2] for n in range(len(topic)-1)} - {"时间", "安排", "确定", "事项"}
            matching = {a: quote for a, quote in feedback.items()
                        if a != anchor and any(term in quote for term in terms)}
            if len(matching) == 1:
                a, quote = next(iter(matching.items()))
                agenda[topic] = (anchor, a, quote)
    stages = [(r["lead"], r["timing"], r["action"], r["anchors"][0]) for r in actions
              if r["lead"] and r["timing"].endswith("后") and
              any(r["lead"] == f["主体"] and r["timing"] == f["阶段"] and r["action"] == f["动作"]
                  for record in ledger.values() for f in record.get("已确认核查分工", []))]
    return {"actions": actions, "decisions": decisions, "feedback": feedback, "agenda": agenda, "stages": stages,
            "phases": minute_actions.execution_phases(instruction, decisions, actions)}


def _apply_policy_plan(candidate, plan):
    candidate.tasks = [TaskItem(**row) for row in plan["actions"]]
    if handoff := plan["open_record_handoff"]:
        agenda = next(section for section in candidate.sections if section.title == policy_actions.AGENDA)
        span = Passage(kind="proposal", **handoff)
        existing = next((i for i, item in enumerate(agenda.paragraphs)
                         if policy_actions.VOTE_RECORD.search(item.text.partition("：")[0])), len(agenda.paragraphs))
        agenda.paragraphs[existing:existing + 1] = [span]
    passages = [Passage(text=line, kind="fact", anchors=[anchor])
                for anchor, line in plan["articles"].items()]
    candidate.sections.append(Section(title=policy_actions.SECTION, paragraphs=passages))


def _verify_policy_plan(candidate, plan, instruction):
    require(candidate.profile == "plan-draft" and
            [item.model_dump() for item in candidate.tasks] == plan["actions"],
            "条款分工与原文不一致，核对责任、条件及限定用语后再交件。", "POLICY_ROWS_CHANGED")
    policy = [section for section in candidate.sections if section.title == policy_actions.SECTION]
    require(len(policy) == 1 and
            [(item.anchors, item.text, item.kind) for item in policy[0].paragraphs] ==
            [([anchor], line, "fact") for anchor, line in plan["articles"].items()],
            "稿内条款与原文不一致。", "POLICY_CLAUSES_CHANGED")
    purpose, *rest = candidate.sections
    agendas = [section for section in rest if section.title == policy_actions.AGENDA]
    require(purpose.title == "会商目的" and len(agendas) == 1 and
            all(item.kind == "proposal" for section in candidate.sections if section is not policy[0]
                for item in section.paragraphs),
            "请补齐会商目的、待定事项，并把会商建议与既定条款分开。", "POLICY_AGENDA_MISSING")
    items = agendas[0].paragraphs
    labels = [item.text.partition("：")[0] for item in items]
    minimum = 2 if re.search(r"分别列|分开列|逐项列|单列", instruction) else 1
    require(len(labels) >= minimum and len(labels) == len(set(labels)) and all(
        item.anchors and re.fullmatch(r"[^，。；？！：:]{2,22}：[^。；！？;!?]+[？?]", item.text) for item in items
    ), "请把待定事项逐项列为会商问题，既定事实沿用原条款。", "POLICY_AGENDA_MISSING")
    if handoff := plan["open_record_handoff"]:
        matching = [item for item in items if policy_actions.VOTE_RECORD.search(item.text.partition("：")[0])]
        require(matching == [Passage(kind="proposal", **handoff)],
                "原文未指派投票记录归集和移交责任，请留待会商确定。", "POLICY_HANDOFF_NOT_OPEN")


TRIAL_SECTION = "试点反馈与建议（供会商）"


def _quoted_opinions(plan):
    declarations = {basis for basis, _, _ in plan["agenda"].values()}
    return {a: "试点反馈：“" + quote + "”" for a, quote in plan["feedback"].items() if a not in declarations}


def _open_owner_agenda(plan, anchors):
    if len(anchors) != 1:
        return None
    rows = [row for row in plan["actions"] if not row["lead"] and row["anchors"] == anchors]
    if not rows:
        return None
    timing = "；".join(dict.fromkeys(row["timing"] for row in rows if row["timing"]))
    wording = (timing + "。") if timing else ""
    return Passage(text="｜".join(("、".join(row["action"] for row in rows) + "的执行人", wording + "纪要未指名执行人。",
                                  "会上确认上述动作的执行人")), kind="proposal", anchors=anchors)


def _apply_execution_phases(candidate, plan):
    phases = plan["phases"]
    if not phases:
        return
    anchors = set().union(*phases.values())
    headings = set()
    ordered = []
    for title, pattern in minute_actions.EXECUTION_PHASES:
        current = next((s for s in candidate.sections if s.title not in headings
                        and s.title not in (TRIAL_SECTION, PLAN_AGENDA) and re.search(pattern, s.title)
                        and sum(bool(re.search(p, s.title)) for _, p in minute_actions.EXECUTION_PHASES) == 1), None)
        if current:
            headings.add(current.title)
        paragraphs = [Passage(text=quote, kind="fact", anchors=[a]) for a, quote in phases[title].items()]
        if title == "上线后跟踪":
            paragraphs += [Passage(text=f"{owner}负责{stage}的{action}。", kind="fact", anchors=[a])
                           for owner, stage, action, a in plan["stages"] if stage == "上线后"]
        ordered.append(Section(title=current.title if current else title, paragraphs=paragraphs))
    before, after = [], []
    for section in candidate.sections:
        if section.title in (TRIAL_SECTION, PLAN_AGENDA):
            after.append(section)
            continue
        remaining = [p for p in section.paragraphs if not (p.anchors and set(p.anchors) <= anchors)]
        if section.title in headings:
            target = next(s for s in ordered if s.title == section.title)
            target.paragraphs.extend(p for p in remaining if p not in target.paragraphs)
        elif remaining:
            if "分工表" in section.title and all(p.kind == "fact" and set(p.anchors) <= plan["decisions"].keys()
                                                for p in remaining):
                continue
            before.append(section.model_copy(update={"paragraphs": remaining}))
    candidate.sections = before + ordered + after


def _apply_source_plan(candidate, plan, owner_anchors=()):
    """Populate the first review copy from the same decisions used to derive its action rows."""
    decisions, feedback = plan["decisions"], plan["feedback"]
    candidate.tasks = [TaskItem(**row) for row in plan["actions"]]
    source = {**decisions, **feedback}
    # Only normalize agenda cells when the supplied numbers already have a source. A numerical
    # mismatch is reported to independent review at the original row position.
    agenda = next((s for s in candidate.sections if s.title == PLAN_AGENDA), None)
    if plan["agenda"] and not agenda:
        agenda = Section(title=PLAN_AGENDA, paragraphs=[])
        candidate.sections.append(agenda)
    if agenda and plan["agenda"]:
        rows = []
        for topic, (basis, evidence, quote) in plan["agenda"].items():
            matches = [p for p in agenda.paragraphs if {basis, evidence}.intersection(p.anchors) and
                       (len(plan["agenda"]) == 1 or any(topic[i:i+2] in table_row(p.text)[0]
                        for i in range(len(topic)-1)))]
            if len(matches) == 1 and numeric_tokens(matches[0].text) - numeric_tokens(quote + feedback[basis]):
                matches[0].kind = "proposal"
                rows.append(matches[0])
            else:
                rows.append(Passage(text="｜".join((topic, quote, "会上确定" + topic)), kind="proposal",
                                    anchors=[evidence, basis]))
        extras = []
        for p in agenda.paragraphs:
            if not set(p.anchors) <= set(feedback):
                entry = _open_owner_agenda(plan, p.anchors) if len(p.anchors) == 1 and p.anchors[0] in owner_anchors else None
                entry = entry or p
                if entry not in extras:
                    extras.append(entry)
        for anchor in sorted(owner_anchors):
            entry = _open_owner_agenda(plan, [anchor])
            if entry and entry not in extras:
                extras.append(entry)
        agenda.paragraphs = rows + extras
    body_anchors = set()
    quotations = _quoted_opinions(plan)
    declarations = feedback.keys() - quotations.keys()
    pending_notes = []
    fact_anchors = {a for section in candidate.sections if section.title != PLAN_AGENDA
                    for p in section.paragraphs if p.kind == "fact" for a in p.anchors}
    # Prefer the later execution phase when an overview repeats the same decision.
    for section in reversed(candidate.sections[:]):
        if section.title == PLAN_AGENDA:
            continue
        kept = []
        repeated_decision = None
        for p in section.paragraphs:
            basis = list(dict.fromkeys(p.anchors))
            if set(basis) <= source.keys() and decisions.keys() & set(basis) and feedback.keys() & set(basis):
                # This operational section may retain its decision. The separately attributed
                # trial quotation below remains the only source for the pilot's suggested action.
                basis = [a for a in basis if a in decisions]
                p.anchors = basis
            if basis and set(basis) <= feedback.keys():
                if (declarations.intersection(basis) and
                    all(item.anchors and set(item.anchors) <= feedback.keys() for item in section.paragraphs)):
                    note = Passage(text=" ".join(feedback[a] for a in basis if a in declarations),
                                   anchors=basis, kind="proposal")
                    if note not in pending_notes:
                        pending_notes.append(note)
                continue
            if basis and set(basis) <= decisions.keys():
                if p.kind == "proposal" and set(basis) <= fact_anchors:
                    continue
                p.kind = "fact"
                p.text = " ".join(decisions[a] for a in basis)
                if all(a in body_anchors for a in basis) and len(section.paragraphs) > 1:
                    repeated_decision = repeated_decision or p
                    continue
                body_anchors.update(basis)
            kept.append(p)
        if kept:
            section.paragraphs = kept
        elif repeated_decision is not None:
            section.paragraphs = [repeated_decision]
        elif section.paragraphs:
            candidate.sections.remove(section)
    if quotations or pending_notes:
        section = Section(title=TRIAL_SECTION, paragraphs=[
            Passage(text=text, kind="proposal", anchors=[a]) for a, text in quotations.items()] + pending_notes[::-1])
        index = next((i for i, s in enumerate(candidate.sections) if s.title == PLAN_AGENDA), len(candidate.sections))
        candidate.sections.insert(index, section)
        body_anchors.update(a for para in section.paragraphs for a in para.anchors)
    for owner, stage, action, anchor in plan["stages"]:
        phase = next((section for section in candidate.sections if stage in section.title), None)
        if phase and not any(owner in p.text and stage in p.text and action in p.text for p in phase.paragraphs):
            phase.paragraphs.append(Passage(text=owner + "负责" + stage + "的" + action + "。",
                                            kind="fact", anchors=[anchor]))
    missing = {a: line for a, line in source.items() if a not in body_anchors
               and not any(a in p.anchors and section.title == PLAN_AGENDA
                           for section in candidate.sections for p in section.paragraphs)}
    for a, line in missing.items():
        # Preserve decisions omitted by the writer and label trial observations explicitly.
        title = "会议已定安排" if a in decisions else "试点反馈与建议"
        section = next((s for s in candidate.sections if s.title == title), None)
        if section is None:
            section = Section(title=title, paragraphs=[])
            agenda = next((i for i, s in enumerate(candidate.sections) if s.title == PLAN_AGENDA),
                          len(candidate.sections))
            candidate.sections.insert(agenda, section)
        section.paragraphs.append(Passage(text=line if a in decisions else "试点反馈：" + line,
                                          anchors=[a], kind="fact" if a in decisions else "proposal"))
    _apply_execution_phases(candidate, plan)


def _verify_source_plan(candidate, plan):
    expected = [TaskItem(**r) for r in plan["actions"]]
    require(candidate.tasks == expected, "分工与纪要逐项确定的条件、动作、责任、时限或空栏不一致。",
            "REVIEW_UNRESOLVED")
    if phases := plan["phases"]:
        last = -1
        for phase, pattern in minute_actions.EXECUTION_PHASES:
            found = next((i for i, section in enumerate(candidate.sections)
                if i > last and re.search(pattern, section.title) and all(
                any(p.kind == "fact" and a in p.anchors and quote in p.text for p in section.paragraphs)
                for a, quote in phases[phase].items())), None)
            require(found is not None, f"正文缺少「{phase}」段或对应的纪要依据。", "REVIEW_UNRESOLVED")
            last = found
    for anchor, quote in plan["decisions"].items():
        body = [p for section in candidate.sections for p in section.paragraphs
                if anchor in p.anchors and p.kind == "fact"]
        require(any(quote in p.text for p in body), "正文缺少纪要已确认的原句或执行条件。", "REVIEW_UNRESOLVED")
    for anchor, text in _quoted_opinions(plan).items():
        require(any(s.title == TRIAL_SECTION and any(p.kind == "proposal" and p.anchors == [anchor] and
                    p.text == text for p in s.paragraphs) for s in candidate.sections),
                "独立试点意见节缺少有出处的完整引文或建议归属。", "REVIEW_UNRESOLVED")
    for anchor, quote in plan["feedback"].items():
        body = [p for section in candidate.sections for p in section.paragraphs if anchor in p.anchors]
        require(body and all(p.kind == "proposal" for p in body), "试点意见丢失或被写成会议决定。", "REVIEW_UNRESOLVED")
        require(not any(anchor in p.anchors for s in candidate.sections if s.title not in (TRIAL_SECTION, PLAN_AGENDA)
                        for p in s.paragraphs), "试点建议误入已定执行阶段。", "REVIEW_UNRESOLVED")
        if not any(anchor == declaration for declaration, _, _ in plan["agenda"].values()):
            require(any(quote in p.text for p in body), "正文或会商议题缺少试点原意见。", "REVIEW_UNRESOLVED")
    for topic, (basis, evidence, quote) in plan["agenda"].items():
        agenda = [p for s in candidate.sections if s.title == PLAN_AGENDA for p in s.paragraphs]
        require(any(table_row(p.text) == [topic, quote, "会上确定" + topic]
                    and {basis, evidence} <= set(p.anchors) for p in agenda),
                "会商议题、试点意见与待定边界不一致。", "REVIEW_UNRESOLVED")
    for section in candidate.sections:
        if section.title == PLAN_AGENDA:
            for row in section.paragraphs:
                expected_owner = _open_owner_agenda(plan, row.anchors)
                if expected_owner:
                    require(row == expected_owner, "执行人待议行越出纪要所列动作。", "REVIEW_UNRESOLVED")
    for owner, stage, action, _ in plan["stages"]:
        phases = [s for s in candidate.sections if stage in s.title]
        require(not phases or any(p.kind == "fact" and owner in p.text and stage in p.text and action in p.text
                                  for s in phases for p in s.paragraphs),
                "对应执行阶段未保留纪要明确的核查分工。", "REVIEW_UNRESOLVED")
    for section in candidate.sections:
        for p in section.paragraphs:
            if p.kind != "fact":
                continue
            allowed = [plan["decisions"][a] for a in p.anchors if a in plan["decisions"]]
            if not allowed:
                continue
            allowed += [owner + "负责" + stage + "的" + action + "。"
                        for owner, stage, action, a in plan["stages"] if a in p.anchors]
            require(all(clause.strip() in " ".join(allowed) for clause in re.split(r"[，,。；;]", p.text) if clause.strip()),
                    "正文的责任或条件越出本段所引纪要。", "REVIEW_UNRESOLVED")


def _restore_proposal_anchors(candidate, ledger, usable):
    """Attach a uniquely identifiable trial sentence to a quantitative proposal, retaining prior citations."""
    sources = {anchor: usable[anchor] for entry in ledger.values() if entry["用途"] == "试点材料"
               for anchor in entry["片段"] if anchor in usable}
    normalized_sources = {a: number_basis.QUANTITY.sub(number_basis.quantity_token, text)
                          for a, text in sources.items()}
    proposals = [(p, p.text) for s in candidate.sections for p in s.paragraphs if p.kind == "proposal"]
    proposals.extend((t, t.action) for t in candidate.tasks if t.status == "proposed")
    for item, text in proposals:
        known = {token for a in item.anchors for token in numeric_tokens(usable.get(a, ""))}
        missing = numeric_tokens(text) - known
        if not missing:
            continue
        claim = number_basis.QUANTITY.sub(number_basis.quantity_token, text)
        for quantity in number_basis.QUANTITY.finditer(claim):
            token = quantity[0]
            if token not in missing:
                continue
            # A quantity with the same unit is insufficient. Require surrounding wording on either side.
            left = re.search(r"[\u4e00-\u9fff]{2}$", claim[:quantity.start()])
            right = re.match(r"[\u4e00-\u9fff]{2}", claim[quantity.end():])
            fragments = ([left[0] + token] if left else []) + ([token + right[0]] if right else [])
            possible = [key for key, source in normalized_sources.items()
                        if token in numeric_tokens(sources[key]) and any(part in source for part in fragments)]
            if len(possible) == 1 and possible[0] not in item.anchors:
                item.anchors.append(possible[0])


def _ground_contingencies(candidate, ledger, usable):
    """Separate a triggered suspension from the responder who carries out the following rollback."""
    for record in ledger.values():
        for anchor in record.get("条件与责任原句", []):
            sentence = usable.get(anchor, "").strip().rstrip("。")
            rule = re.fullmatch(r"(.+?)[，,](?:就|则|应)(暂停[^，,；;]{1,16})[，,]由"
                                r"([^，,；;]{2,12}?)(回退到[^，,；;]+)[，,](.+)", sentence)
            if not rule:
                continue
            condition, suspend, responder, rollback, _ = rule.groups()
            rows = [row for row in candidate.tasks if anchor in row.anchors and row.status == "confirmed"]
            # A compound row that also describes a different action is left to the independent reviewer.
            existing = [row for row in rows if suspend in row.action and "回退" not in row.action]
            if len(existing) == 1:
                row = existing[0]
                row.timing = condition
                if row.lead == responder:
                    row.lead = ""
            elif not existing and not any(suspend in row.action for row in rows):
                index = next((i for i, row in enumerate(candidate.tasks) if row in rows), len(candidate.tasks))
                candidate.tasks.insert(index, TaskItem(action=suspend, lead="", support="", timing=condition,
                                                       deliverable="", acceptance="", status="confirmed", anchors=[anchor]))
            for row in rows:
                if row.action == rollback:
                    row.lead, row.timing = responder, condition


def _conference_sources(materials):
    """Freeze source wording and purpose before the writer can assign a role or omit a decision."""
    ledger = {}
    for material in materials:
        name, blocks = material["name"], material["blocks"]
        if material.get("reference_only"):
            ledger[material["id"]] = {"用途": "只参考结构，禁作事实依据", "片段": list(blocks)}
            continue
        if material["id"] == "U":
            ledger["U"] = {"用途": "用户要求", "片段": list(blocks)}
            continue
        minutes = bool(re.search(r"纪要|决定|决议", name))
        feedback = bool(re.search(r"反馈|建议|意见征集", name))
        # Preserve stable file anchors. A pasted material originally has one block
        # including its heading; give its individual source sentences distinct anchors.
        if len(blocks) == 1:
            raw = next(iter(blocks.values()))
            if raw.startswith("【") and "】" in raw:
                body = raw.split("】", 1)[1].strip()
                sentences = [v.strip() for v in re.split(r"(?<=[。！？])|\n+", body) if v.strip()]
                if sentences:
                    blocks = material["blocks"] = {
                        f"{material['id']}.L{i}": v for i, v in enumerate(sentences, 1)}
        roles = {}
        for key, quote in blocks.items():
            if re.search(r"已作废|已撤销|不再执行", quote):
                role = "失效内容"
            elif re.search(r"建议|拟安排|未定|会上定|会上确|不同意见", quote):
                role = "逐句区分观察、提议和待确认项"
            elif minutes:
                role = "纪要决定"
            elif feedback:
                role = "试点情况，仅供会商"
            else:
                role = "依原文判断"
            roles[key] = role
        ledger[material["id"]] = {"用途": "会议纪要" if minutes else "试点材料" if feedback else name,
                                   "片段": roles}
        if minutes:
            # Carry complete, verbatim conditional chains across all editing stages.
            linked = {k: v for k, v in blocks.items() if "；" in v or re.search(r"或者.+就.+由", v)}
            if linked:
                ledger[material["id"]]["条件与责任原句"] = list(linked)
        if minutes and (checks := _confirmed_checks(blocks)):
            ledger[material["id"]]["已确认核查分工"] = checks
    return ledger


class SourcedBriefItem(StrictModel):
    text: str
    status: Literal["known", "pending"]
    anchors: list[str] = Field(description="依据编号数组，每项只能是本轮材料 blocks 的一个原始键，如 U.1、S1.L1；不含冒号、原文或说明。条目说明只放 text。")


class ExpertBrief(StrictModel):
    input_key: str
    items: list[SourcedBriefItem]
    outline: list[str]


@dataclass
class Run:
    status: str
    candidate: Candidate
    materials: list
    model: dict
    excerpts: dict
    note: str = ""
    submission: dict | None = None
    conference_review: dict | None = None
    expert_workflow: dict | None = None
    original_request: str | None = None
    draft_check_error: str | None = None


CLOCK_TIME = re.compile(
    r"(?<![\d.零〇一二两三四五六七八九十百千万])(?!(?<=[这那哪另某有差])一点)(?P<period>上午|下午)?[ \t]*"
    # In words such as 统一时 and 同一时, 一 does not start a clock expression.
    r"(?!(?<=[统同唯惟单合归])一时)"
    r"(?P<hour>[01]?\d|2[0-3]|二十[一二三]?|十[一二三四五六七八九]?|[零〇一二两三四五六七八九])"
    r"[ \t]*(?::[ \t]*(?P<minute>[0-5]\d)(?!\d)|(?:点|时(?![段间候点限序]))(?:(?P<cn_minute>[0-5]?\d)分?)?(?![\d半刻小秒]))"
)


def event_window(quote, *, require_event_context=False):
    """Read an unambiguous event window, preserving whether the year was supplied."""
    spans = set()
    relative_request = RELATIVE_WORK_DATES.search(quote) if require_event_context else None
    ranges = []
    for match in EVENT_RANGE.finditer(quote):
        if require_event_context and not EVENT_RANGE_LABEL.search(quote, max(0, match.start() - 20), match.start()):
            # A follow-up may reach this entry on a new line or after sentence punctuation.
            if (not relative_request
                    or not EVENT_REPLY_PREFIX.search(quote, relative_request.end(), match.start())
                    or not EVENT_REPLY_SUFFIX.match(quote, match.end())):
                continue
        ranges.append(match)
    matches = ranges or EVENT_SINGLE.finditer(quote)
    for match in matches:
        fields = match.groupdict()
        year = int(match["year"]) if match["year"] else None
        end_year = int(fields["end_year"]) if fields.get("end_year") else year
        if year is None and end_year is not None:
            return None
        month, day = int(match["month"]), int(match["day"])
        end_month = int(fields.get("end_month") or month)
        end_day = int(fields.get("end_day") or day)
        if year is None and ((month, day) > (end_month, end_day) or
                             (month, day) in {(2, 29), (3, 1)} or (end_month, end_day) in {(2, 28), (2, 29)}):
            return None
        try:
            start = date(year or 2001, month, day)
            end = date(end_year or 2001, end_month, end_day)
            if start > end:
                return None
            before, after = start - timedelta(days=1), end + timedelta(days=1)
        except (ValueError, OverflowError):
            return None
        spans.add(tuple((value.year if year is not None else None, value.month, value.day)
                        for value in (start, end, before, after)))
    return next(iter(spans)) if len(spans) == 1 else None


def _schedule_material(source):
    if not isinstance(source, dict):
        return None
    url, quote = (source.get(key) for key in ("url", "quote"))
    if not isinstance(url, str) or not isinstance(quote, str) or len(url) > 2048 or len(quote) > 1200:
        return None
    try:
        address = urlsplit(url)
    except ValueError:
        return None
    if (address.scheme != "https" or not address.hostname or address.username is not None or
            not event_window(quote)):
        return None
    return {"id": "P", "name": "公开会期", "source_url": url, "blocks": {"P.1": quote},
            "sha256": hashlib.sha256(quote.encode()).hexdigest()}


def numeric_tokens(text):
    text = number_basis.normalized_text(text)
    clocks = set()

    def clock(match):
        minute = match["minute"] or match["cn_minute"] or "0"
        value = match["hour"]
        digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
        if value.isdigit():
            hour = int(value)
        elif "十" in value:
            tens, ones = value.split("十")
            hour = (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)
        else:
            hour = digits[value]
        if match["period"] == "下午" and 1 <= hour < 12:
            hour += 12
        clocks.add(f"时间{hour:02d}:{int(minute):02d}")
        return " "

    # Clock format changes retain the complete time; an added minute still needs a source.
    remainder = CLOCK_TIME.sub(clock, text)
    quantities = set()

    def quantity(match):
        quantities.add(number_basis.quantity_token(match))
        return " "

    remainder = number_basis.QUANTITY.sub(quantity, remainder)
    return set(NUMBERS.findall(remainder)) | clocks | quantities


def validate(candidate, materials):
    sources = {m["id"]: m["blocks"] for m in materials}
    ids = [item.source_id for item in candidate.material_uses]
    require(set(ids) == set(sources) and len(ids) == len(sources),
            "材料用途清单不完整，未生成 Word。", "BASIS_INVALID")
    usable = {}
    experience = candidate.profile == "experience-draft" or candidate.profile in FOLLOW_UP_SECTIONS
    references = {material["id"] for material in materials if material.get("reference_only")}
    for use in candidate.material_uses:
        require(use.source_id not in references or use.role == "reference",
                "用户指定仅作参考的材料不能用作事实。", "BASIS_INVALID")
        allowed = use.role in ({"facts", "mixed"} if experience else {"facts", "requirements", "mixed"})
        require(not use.usable_anchors or allowed, "参考、待改原稿或未用材料不能作为成稿依据。", "BASIS_INVALID")
        for anchor in use.usable_anchors:
            require(anchor in sources[use.source_id], f"无法找到材料片段 {anchor}。", "BASIS_INVALID")
            usable[anchor] = sources[use.source_id][anchor]
    return usable


def _generate_draft(model, prompt, materials, generate, *, split_inline, meeting_slots=None, policy_plan=None,
                    minutes_uses=None):
    if policy_plan:
        draft, metadata = generate(model, prompt, PolicyDiscussionDraft)
        sources = [MaterialUse(
            source_id=m["id"],
            role="reference" if m.get("reference_only") else "requirements" if m["id"] == "U" else "facts",
            usable_anchors=[] if m.get("reference_only") else list(m["blocks"]),
        ) for m in materials]
        sections = [
            Section(title="会商目的", paragraphs=[Passage(text=draft.purpose, kind="proposal", anchors=["U.1"])]),
            Section(title=policy_actions.AGENDA, paragraphs=[
                Passage(text=f"{item.topic.strip()}：{item.question.strip()}", kind="proposal", anchors=item.anchors)
                for item in draft.agenda
            ]),
        ]
        return Candidate(profile="plan-draft", title=draft.title, material_uses=sources, questions=[],
                         sections=sections, tasks=[], review_notes=[]), metadata
    fields = {}
    if meeting_slots:
        issue = create_model("MeetingIntakeIssue", __base__=StrictModel, question=(str, ...),
                             slot=(Literal[(*meeting_slots, "unresolved")], ...))
        fields["questions"] = (list[issue], ...)
    if split_inline and minutes_uses is None:
        # The host owns source identity. The model supplies only the purpose of each known source.
        purposes = create_model("BoundMaterialPurposes", __base__=StrictModel, **{
            m["id"]: (MaterialPurpose, ...) for m in materials if not m.get("reference_only")
        })
        fields["material_uses"] = (purposes, ...)
    base = OperationalMinutesDraft if minutes_uses is not None else Candidate
    exchange = create_model(base.__name__, __base__=base, **fields) if fields else base
    draft, metadata = generate(model, prompt, exchange)
    data = draft.model_dump() if fields or minutes_uses is not None else None
    if meeting_slots:
        metadata["intake_bindings"] = data["questions"]
        data["questions"] = [item["question"] for item in data["questions"]]
    if minutes_uses is not None:
        purpose = data.pop("purpose").strip()
        metadata["structured_meeting_purpose"] = True
        if purpose:
            opening = Section(title="会商目的", paragraphs=[Passage(text=purpose, kind="fact", anchors=["U.1"])])
            data["sections"].insert(0, opening)
        return Candidate(**data, profile="plan-draft", material_uses=minutes_uses, tasks=[]), metadata
    if not split_inline:
        if meeting_slots:
            draft = Candidate.model_validate(data)
        # Resolve a material ID reported as one of its own cited anchors.
        source_ids = {m["id"] for m in materials}
        anchor_sources = {anchor: m["id"] for m in materials for anchor in m["blocks"]}
        for use in draft.material_uses:
            if (use.source_id not in source_ids and use.source_id in use.usable_anchors
                    and (owner := anchor_sources.get(use.source_id)) is not None):
                use.source_id = owner
        return draft, metadata
    classified = data.pop("material_uses")
    uses = [
        {"source_id": m["id"], "role": "reference", "usable_anchors": []}
        if m.get("reference_only") else {"source_id": m["id"], **classified[m["id"]]}
        for m in materials
    ]
    return Candidate.model_validate({**data, "material_uses": uses}), metadata


def _verify_meeting_purpose(candidate):
    openings = [s for s in candidate.sections if s.title == "会商目的"]
    require(len(openings) == 1 and len(openings[0].paragraphs) == 1
            and re.fullmatch(r"[^。！？!?\r\n]{1,44}。", openings[0].paragraphs[0].text),
            "会商目的须用一句话写清，且不超过45字。", "DRAFT_INCOMPLETE")


def notice_receipt_fields(request):
    # Only complete an explicit list of supported field labels; other requests keep model intake.
    match = re.search(
        r"回执[，,\s]*(?:(?:请)?(?:留|填写|包含|列出))?([^。；\n]+?)"
        r"(?:这[一二三四五六\d]+栏[^。；\n]*)?(?=[。；\n]|$)", request)
    fields = re.split(r"、|和|及|与|，|,", match[1]) if match else []
    allowed = {"姓名", "岗位", "单位", "联系电话", "电话", "场次"}
    return list(dict.fromkeys(fields)) if fields and all(f in allowed for f in fields) else []


def _complete_notice_receipt(candidate, usable, request):
    if candidate.profile != "notice-draft" or not (fields := notice_receipt_fields(request)):
        return
    require("U.1" in usable, "报名回执字段缺少本轮原话依据。", "BASIS_INVALID")
    sections = []
    for section in candidate.sections:
        if section.title != NOTICE_RECEIPT:
            sections.append(section)
            continue
        # The explicit user list owns the blank table; retain any model-written instructions.
        content = [p for p in section.paragraphs if not re.fullmatch(
            r"(?:(?:联系电话|姓名|岗位|单位|电话|场次)|[\s｜|、,，；;：:＿_—\-（）()□])+", p.text)]
        if content:
            sections.append(section.model_copy(update={"title": "报名回执说明", "paragraphs": content}))
    candidate.sections = [*sections, Section(title=NOTICE_RECEIPT, paragraphs=[
        Passage(text="｜".join(fields), kind="fact", anchors=["U.1"])])]


def check_notice_receipt(candidate, request):
    if candidate.profile != "notice-draft" or not (fields := notice_receipt_fields(request)):
        return
    receipts = [s for s in candidate.sections if s.title == NOTICE_RECEIPT]
    require(len(receipts) == 1 and len(receipts[0].paragraphs) == 1
            and table_row(receipts[0].paragraphs[0].text) == fields,
            "报名回执缺少本轮明确要求的可填写字段，未生成 Word。", "DRAFT_INCOMPLETE")


def _normalize_notice_addressee(candidate):
    if len(candidate.sections) < 2:
        return
    first, following = candidate.sections[:2]
    title = first.title.strip()
    if len(first.paragraphs) != 1 or not re.fullmatch(r"[^：:\r\n]+[：:]", title):
        return
    placeholder = first.paragraphs[0]
    if placeholder.text.strip() or placeholder.kind != "fact" or placeholder.anchors != ["U.1"]:
        return
    if not any(p.text.strip() and not NOTICE_ISSUER.fullmatch(p.text.strip())
               and not p.text.strip().startswith("成文日期") for p in following.paragraphs):
        return
    first.title = ""
    placeholder.text = title[:-1] + "："


def _complete_notice_blank_date(candidate, request):
    if candidate.profile != "notice-draft":
        return
    date_field = r"(?:落款|成文|发文|印发)(?:日期|时间)"
    leave_blank = re.search(
        rf"(?:{date_field}|日期)\s*(?:(?:尚未确定|暂未确定|待定|尚未定|没定)[，,、\s]*)?"
        r"(?:先|暂时|暂)?(?:留空|空着|不写|不填)", request)
    date_pending = not re.search(date_field, request)
    if not (leave_blank or date_pending):
        return
    paragraphs = [p for section in candidate.sections if section.title not in {NOTICE_RECEIPT, NOTICE_RECEIPT + "说明"}
                  for p in section.paragraphs]
    if leave_blank and paragraphs and NOTICE_ISSUER.fullmatch(paragraphs[-1].text.strip()):
        tail = next(s for s in reversed(candidate.sections) if any(p is paragraphs[-1] for p in s.paragraphs))
        blank = Passage(text="成文日期：", kind="fact", anchors=["U.1"])
        tail.paragraphs.append(blank)
        paragraphs.append(blank)
    if len(paragraphs) < 2:
        return
    blank, issuer = paragraphs[-1], paragraphs[-2]
    if (blank.kind == "fact" and blank.anchors in ([], ["U.1"])
            and (re.fullmatch(r"成文日期\s*[:：]", blank.text.strip())
                 or (not blank.text.strip() and NOTICE_ISSUER.fullmatch(issuer.text.strip())))):
        # An empty field is formatting. The task's explicit instruction owns the blank.
        blank.text = "成文日期："
        blank.anchors = ["U.1"]


def _complete_notice_issuer(candidate, request, usable):
    """Recover a single organization named by the user if the model omitted the signature."""
    issuers = set()
    for clause in re.split(r"[，,。；;！？\n]", request):
        prefix = re.fullmatch(
            r"(?:发文单位|发文机关|落款(?:单位)?)\s*(?:写|用|为|是|[:：])\s*(.+)", clause.strip())
        suffix = re.fullmatch(r"(.+?)\s*落款", clause.strip())
        match = prefix or suffix
        issuer = match[1].strip(' “"「」”') if match else ""
        if NOTICE_ISSUER.fullmatch(issuer):
            issuers.add(issuer)
    if len(issuers) != 1 or not candidate.sections:
        return
    require("U.1" in usable, "落款缺少本轮原话依据。", "BASIS_INVALID")
    named_issuer = issuers.pop()
    for _, signature in _notice_sections(candidate.sections):
        for _, line in signature:
            if NOTICE_ISSUER.fullmatch(line):
                for section in reversed(candidate.sections):
                    issuer_line = next((p for p in reversed(section.paragraphs) if p.text.strip() == line), None)
                    if issuer_line:
                        issuer_line.text, issuer_line.anchors = named_issuer, ["U.1"]
                        return
                if line == named_issuer:
                    return
    issuer = Passage(text=named_issuer, kind="fact", anchors=["U.1"])
    end = len(candidate.sections)
    while end and candidate.sections[end - 1].title in {NOTICE_RECEIPT, NOTICE_RECEIPT + "说明"}:
        end -= 1
    if end and (paragraphs := candidate.sections[end - 1].paragraphs):
        date = _notice_tail_role(paragraphs[-1].text)
        if date and date[0] == "date":
            paragraphs.insert(len(paragraphs) - 1, issuer)
            return
    candidate.sections.insert(end, Section(title="", paragraphs=[issuer]))


def _has_notice_media_basis(match, sources):
    return any(f"《{match['outlet']}》" in clause and match["action"] in clause
               and not re.search(r"禁止|不得|不要|无需|不必|不作", clause)
               for source in sources for clause in re.split(r"[。；！？\n]", source))


def _complete_relative_notice(candidate, usable, request):
    # A complete, user-specified schedule can be recovered even when the writer leaves every
    # citation blank. Only consume fully recognised clauses; additional duties still need review.
    if (set(usable) == {"U.1"} and KEEP_RELATIVE_DATES.search(request)
            and SEPARATE_WORK_DATES.search(request) and not CONTINUOUS_WORK_DATES.search(request)
            and all(not p.anchors for s in candidate.sections for p in s.paragraphs if p.text.strip()
                    and not _notice_tail_role(p.text.strip()))):
        clauses = [s.strip() for s in re.split(r"[，,。；;！？\n]", request) if s.strip()]
        recipients, meetings, issuers = set(), set(), set()
        for clause in clauses:
            if m := re.fullmatch(r"发给([^，。]{2,35})", clause):
                recipients.add(m[1])
            elif m := re.fullmatch(r"([A-Za-z0-9\u4e00-\u9fff]{2,22}?(?:正式)?会议)开始前一天和结束后一天", clause):
                meetings.add(m[1].upper())
            elif m := re.fullmatch(r"(?:发文单位是(.{2,30})|(.{2,30})落款)", clause):
                issuers.add(m[1] or m[2])
            elif not re.fullmatch(r"(?:帮我|请)?写(?:一下|一份)?\s*[A-Za-z0-9\u4e00-\u9fff]{0,20}居家办公的通知书?"
                                  r"|两天分别安排|就会议开始前一天和结束后一天|各一天"
                                  r"|(?:具体公历)?日期(?:先用|保留)相对(?:表述|日期)|成文日期留空"
                                  r"|(?:先)?出一份\s*Word\s*审阅稿", clause, re.I):
                break
        else:
            if len(recipients) == len(meetings) == len(issuers) == 1:
                meeting, recipient, issuer = meetings.pop(), recipients.pop(), issuers.pop()
                candidate.title = f"关于{meeting}相关居家办公安排的通知（审阅稿）"
                lines = [recipient + "：", f"请于{meeting}开始前一天和结束后一天分别居家办公，各一天。",
                         issuer, "成文日期："]
                candidate.sections = [Section(title="", paragraphs=[
                    Passage(text=line, kind="fact", anchors=["U.1"]) for line in lines])]
    # Drop an isolated, unsupported media assignment without rerunning the writer.
    removed_assignment = False
    for section in candidate.sections[:]:
        if section.title or not section.paragraphs:
            continue
        for paragraph in section.paragraphs[:]:
            match = NOTICE_MEDIA_ASSIGNMENT.fullmatch(paragraph.text.strip().rstrip("。"))
            if match and not _has_notice_media_basis(match, [usable[a] for a in paragraph.anchors]):
                section.paragraphs.remove(paragraph)
                removed_assignment = True
        if not section.paragraphs:
            candidate.sections.remove(section)
    paragraphs = [p for section in candidate.sections for p in section.paragraphs]
    if removed_assignment:
        require(any(REMOTE_WORK.search(p.text) and p.text.strip() for p in paragraphs),
                "正文还缺少有依据的居家办公安排。", "DRAFT_INCOMPLETE")
    if KEEP_RELATIVE_DATES.search(request) and (window := event_window(request, require_event_context=True)):
        # A known pair of offsets can be written back in the exact form requested.
        for paragraph in paragraphs:
            if not REMOTE_WORK.search(paragraph.text):
                continue
            dates = list(EVENT_DATE.finditer(paragraph.text))
            if len(dates) != 2 or not re.fullmatch(r"\s*(?:和|与|及|、)\s*", paragraph.text[dates[0].end():dates[1].start()]):
                continue
            if all((int(match["month"]), int(match["day"])) == (month, day)
                   and (not match["year"] or int(match["year"]) == year)
                   for match, (year, month, day) in zip(dates, window[2:])):
                paragraph.text = (paragraph.text[:dates[0].start()] + "会议开始前一天和结束后一天"
                                  + paragraph.text[dates[1].end():])
    title = candidate.title.strip()
    plain_title = re.sub(r"[（(](?:演练|复盘)?审阅稿[）)]$", "", title).strip()
    for section in candidate.sections:
        if section.title.strip() in (title, plain_title):
            section.title = ""
    if event := re.search(r"\b([A-Za-z][A-Za-z0-9]{1,15})\s*(?:正式)?会议(?:开始|开幕)", request, re.ASCII):
        name = event[1].upper()
        if KEEP_RELATIVE_DATES.search(request) and len(paragraphs) > 1 and candidate.sections:
            opening = candidate.sections[0].paragraphs
            if len(opening) > 1 and notice_scope.is_salutation(opening[0].text) and not opening[1].anchors:
                framing = rf"现就{re.escape(name)}\s*(?:正式)?会议(?:期间)?相关办公安排通知如下[。：:]?"
                if re.fullmatch(framing, opening[1].text.strip(), re.I):
                    # Omit stock framing without inferring an event-wide obligation or authorizing new claims.
                    paragraphs.remove(opening.pop(1))
        if not any(name in text.upper() for text in [candidate.title, *(p.text for p in paragraphs)]):
            for paragraph in paragraphs:
                if REMOTE_WORK.search(paragraph.text) and (meeting := re.search(r"会议(?:开始|开幕)前", paragraph.text)):
                    paragraph.text = paragraph.text[:meeting.start()] + f"{name} " + paragraph.text[meeting.start():]
                    break
    recipients = set(re.findall(r"(?:^|[。！？；\n])\s*(?:请)?发给([^，,。；！？\n]{2,40})", request))
    if len(recipients) == 1 and paragraphs:
        recipient = recipients.pop().strip()
        # Replace the addressee in place, including a list of entities invented by the writer.
        first = paragraphs[0]
        if first.text.strip() == recipient or (notice_scope.is_salutation(first.text) and not _notice_tail_role(first.text)):
            first.text, first.anchors = recipient + "：", ["U.1"]
        if KEEP_RELATIVE_DATES.search(request) and set(usable) == {"U.1"}:
            # A notice to individuals cannot silently assign new duties to their employers.
            for section in candidate.sections:
                section.paragraphs[:] = [p for p in section.paragraphs if not any(
                    group in p.text and group not in request for group in ("用人单位", "企事业单位", "各级机关"))]
                for p in section.paragraphs:
                    if "�" in p.text:
                        for i in range(1, len(recipient)):
                            broken = recipient[:i] + "�" + recipient[i:]
                            p.text = p.text.replace(broken, recipient)
                        p.text = re.sub(r"([一-龥])�([一-龥])", lambda m: m[1]+m[2]
                                        if m[1]+m[2] in recipient else m[0], p.text)
                        require("�" not in p.text, "正文中有无法核实的乱码。", "DRAFT_INCOMPLETE")
            candidate.sections[:] = [s for s in candidate.sections if s.paragraphs]
            paragraphs = [p for s in candidate.sections for p in s.paragraphs]
        if not any(re.fullmatch(re.escape(recipient) + r"[：:]", p.text.strip()) for p in paragraphs):
            candidate.sections.insert(0, Section(title="", paragraphs=[
                Passage(text=recipient + "：", kind="fact", anchors=["U.1"])]))


def _restore_single_event_notice(candidate, request):
    """Draft a short event notice from the facts in the user's own words."""
    redundant = bool(candidate.questions) and all(
        re.fullmatch(r"(?:是否(?:按|以)原文为准|(?:还)?需要发(?:这份)?通知吗)[？?。]?", item.strip())
        for item in candidate.questions)
    if ((candidate.questions and not redundant) or candidate.tasks or not candidate.title.strip()):
        return False
    sentences = [part.strip() for part in re.split(r"[。！？；\n]", request) if part.strip()]
    bodies = []
    for part in sentences:
        if DRAFT_DIRECTIVE.search(part) and DRAFT_GENRE.search(part):
            # Embedded facts in the drafting directive would require editing by meaning.
            if CALENDAR_DATE.search(part) or CLOCK_TIME.search(part):
                return False
            continue
        if re.fullmatch(r"(?:发文单位|发文机关|落款(?:单位)?)\s*(?:是|为|写|用|[:：])[^，,。]{2,35}"
                        r"(?:[，,]\s*(?:成文|落款|发文)日期(?:先|暂)?(?:留空|待定|不填))?", part):
            continue
        if re.fullmatch(r"(?:成文|落款|发文)日期(?:先|暂)?(?:留空|待定|不填)", part):
            continue
        if (not CALENDAR_DATE.search(part) or not CLOCK_TIME.search(part)
                or not re.search(r"(?:在|到|地点(?:为|是|[:：]))[^，,。]{2,40}", part)
                or not re.search(r"[^，,。]{2,30}(?:参加|出席|到场|报到|集合)", part)
                or re.search(r"未定|待定|待确认|不确定|可能|冲突|请问|？|审阅稿|Word|正文|成文日期", part, re.I)):
            return False
        bodies.append(part + "。")
    if len(bodies) != 1:
        return False
    lines = bodies
    training = re.fullmatch(
        r"(?P<when>[^，,。]+)[，,]在(?P<place>[^，,。]+)[，,]"
        r"(?P<attendees>(?:全体|各)[\u4e00-\u9fff]{1,20})参加[，,]"
        r"(?P<carry>(?:带好|携带|带上)[\u4e00-\u9fff]{1,16})。",
        bodies[0],
    )
    if (training and "培训" in candidate.title and "培训" in request
            and re.search(r"正式通知|Word|审阅稿", request, re.I)
            and not re.search(r"原样|原文不动|不得改|只要正文|只给文字|发给|主送", request)):
        lines = [
            f"{training['attendees']}：",
            "现将培训安排通知如下：",
            f"一、培训时间。{training['when']}。",
            f"二、培训地点。{training['place']}。",
            f"三、参训要求。请{training['carry']}参加培训。",
        ]
    candidate.sections = [Section(title="", paragraphs=[
        Passage(text=line, kind="fact", anchors=["U.1"]) for line in lines])]
    candidate.questions = []
    candidate.review_notes = []
    return True


def _recover_scoped_notice(candidate, request):
    """Complete a quantified or explicitly partial one-event notice from its own facts."""
    clauses = [s.strip() for s in re.split(r"[。；，,！？\n]", request) if s.strip()]
    if not clauses:
        return
    addressee = (re.search(r"发给([^，。]{2,25}?)的(.+?)通知", clauses[0])
                 or re.match(r"给([^，。]{2,25}?)写(?:一份|一则|个|份)?(.+?)的通知", clauses[0]))
    if not addressee:
        return
    recipient, topic = addressee.groups()
    events = [c for c in clauses[1:] if CLOCK_TIME.search(c) and CALENDAR_DATE.search(c)
              and re.search(r"(?:在|到)[^，。]{2,32}(?:集合|开课|报到|开会|参加|开展)", c)]
    if len(events) != 1 or re.search(r"冲突|矛盾|有争议|暂不确定|未核实", request):
        return
    pending = re.search(r"(人数|份数|件数|套数|数量)(?:请|应)?单列(?:为|在)?待确认", request)
    rates = [(c, m) for c in clauses if (m := number_basis.BATCH.search(c))]
    calculate = re.search(r"(?:算出|算一下|计算|写清楚)[^，。]{0,10}(?:总共|合计|总数|总量)", request)
    if not (pending or (calculate and len(rates) == 1)):
        return
    # Rebuild an empty body only when every remaining clause is accounted for. An unrecognized
    # qualification, conditional action or additional fact stays with the normal review path.
    facts, rate_line = [], rates[0][0] if calculate and len(rates) == 1 else None
    for clause in clauses[1:]:
        if clause == events[0] or re.fullmatch(r"(?:请)?(?:携带|带好|带)[\u4e00-\u9fff]{1,16}", clause):
            facts.append(clause + "。")
        elif clause == rate_line:
            continue
        elif (re.fullmatch(r"(?:请)?(?:算出|算一下|计算)(?:总共|合计|总量|总数)(?:多少)?"
                           r"(?:人|件|套|双|份|箱)?(?:并写在通知里)?", clause)
              or re.fullmatch(r"(?:先)?(?:给我?|发我?)\s*(?:正文|Word)", clause, re.I)
              or re.fullmatch(r"先把已确定的安排写好", clause) or (pending and pending[0] == clause)
              or (pending and re.search(r"几个人|多少人|" + pending[1], clause)
                  and re.search(r"还没定|尚未确定|待定", clause))):
            continue
        else:
            return
    if not candidate.sections or candidate.questions or candidate.tasks:
        # Every clause in this simple request has a place in the recoverable body. Build it from
        # those facts so optional questions cannot prevent delivery or introduce new requirements.
        candidate.title = candidate.title.strip() or topic + "通知"
        candidate.sections = [Section(title="", paragraphs=[
            Passage(text=text, kind="fact", anchors=["U.1"]) for text in [recipient + "：", *facts]])]
        candidate.questions = []
        candidate.tasks = []
    elif candidate.sections[0].paragraphs:
        addressed = candidate.sections[0].paragraphs[0]
        if re.fullmatch(r"(?:全体|各)[^，。；：:]{1,18}[：:]", addressed.text.strip()):
            addressed.text, addressed.anchors = recipient + "：", ["U.1"]
    if rate_line:
        products = number_basis.derived_totals(rate_line)
        if len(products) == 1:
            amount = products.pop()
            body = "。".join(p.text for s in candidate.sections for p in s.paragraphs)
            requested_rate = number_basis.measure_token(number_basis.amount(rates[0][1]["m"]), rates[0][1]["unit"])
            if not {amount, requested_rate} <= numeric_tokens(body):
                candidate.sections[0].paragraphs.append(Passage(
                    text=rate_line + "，合计" + amount + "。", kind="fact", anchors=["U.1"]))


def _recover_group_notice(candidate, request):
    """Render a complete event and an explicitly requested group size from the user's own wording."""
    sentences = [line.strip() for line in re.split(r"[。！？\n]", request) if line.strip()]
    if len(sentences) != 3:
        return
    event = re.fullmatch(r"(?:请)?给(?P<recipient>[^，,；：。]{2,25}?)写(?:一份|一则|个|份)?\s*"
                         r"(?P<event>[^，,；：。]{5,80})的通知", sentences[0])
    quoted = re.fullmatch(r"(?:经办)?(?:特别)?(?:要求|请)(?:正文)?保留[“「]([^”」]{2,35})[”」]"
                          r"(?:的写法|的表述|原样)[，,]\s*(?:我)?只要正文", sentences[2])
    if not event or not quoted or not CALENDAR_DATE.match(event["event"]) or not CLOCK_TIME.search(event["event"]):
        return
    action = re.search(r"在[^，,；：。]{1,32}?(?P<action>(?:领用|领取|集合|报到|参加|培训|开会).*)$", event["event"])
    if not action:
        return
    if re.search(r"如果|可能|尚未|未定|待确认|冲突|还是|或者", event["event"]):
        return
    stated = number_basis.BATCH.fullmatch(sentences[1].removesuffix("参加").strip())
    requested = number_basis.BATCH.fullmatch(quoted[1])
    if not stated or not requested or stated.groupdict() != requested.groupdict():
        return
    # Every sentence is accounted for. Preserve the wording requested and leave unspecified
    # stationery and contact details empty. The ordinary number and source checks still apply.
    candidate.title = "关于" + action["action"] + "的通知"
    participation = "参加" if sentences[1].endswith("参加") else ""
    lines = [event["recipient"] + "：", "请于" + event["event"] + "。", quoted[1] + participation + "。"]
    candidate.sections = [Section(title="", paragraphs=[
        Passage(text=line, kind="fact", anchors=["U.1"]) for line in lines])]
    candidate.tasks, candidate.questions = [], []


def _prepare_direct_notice(candidate, request, workflow):
    # This path receives one user-authored instruction and no attachments or public sources.
    # The model cannot reclassify that instruction as seven sources or as a reference document.
    if candidate.material_uses and all(use.source_id == "U" and set(use.usable_anchors) <= {"U.1"}
                                       for use in candidate.material_uses):
        candidate.material_uses = [MaterialUse(source_id="U", role="requirements", usable_anchors=["U.1"])]
    if candidate.sections and candidate.sections[0].paragraphs:
        greeting = candidate.sections[0].paragraphs[0]
        addressee = re.fullmatch(r"主送(?:对象|单位)?[：:]\s*([^。；，、：:\n]{2,30})", greeting.text.strip())
        if (addressee and greeting.anchors in ([], ["U.1"]) and
                re.search(r"(?:^|[。；，,\n])(?:请)?(?:发给|给)" + re.escape(addressee[1])
                          + r"(?=写|发个|[，,。；\s]|$)", request)):
            greeting.text, greeting.kind, greeting.anchors = addressee[1] + "：", "fact", ["U.1"]
    if _restore_single_event_notice(candidate, request):
        workflow["notice_body_from_input"] = "U.1"
    _recover_scoped_notice(candidate, request)
    _recover_group_notice(candidate, request)
    if candidate.questions and any(p.text.strip() for s in candidate.sections for p in s.paragraphs):
        # Strip only optional field names and question phrasing. Any other content, such as a
        # second question about the time or the venue, keeps the full question active.
        optional = r"联系人|联系电话|报名|回执"
        framing = r"本轮表述为简短提醒|暂未写入|在通知中|是否|需要|缺少|留空|补充|写明|填写|要求|[和或及、，,？?。\s]"
        def unresolved(question):
            fields = re.findall(optional, question)
            if not fields or any(field in request for field in fields):
                return True
            return bool(re.sub(f"(?:{optional}|{framing})", "", question))
        candidate.questions[:] = [q for q in candidate.questions if unresolved(q)]
    recipient = re.search(r"发给([^，,。；\n]{2,25}?)的[^，,。；\n]*通知", request)
    if recipient and candidate.sections and candidate.sections[0].paragraphs:
        greeting = candidate.sections[0].paragraphs[0]
        if not greeting.anchors and greeting.text.strip() in {recipient[1] + "：", recipient[1] + ":"}:
            greeting.anchors = ["U.1"]
        if "工作人员" not in request and (match := re.fullmatch(r"各([\u4e00-\u9fff]{1,12})", recipient[1])) and re.search(
                r"每" + re.escape(match[1][-1]) + r"派(?:几个|多少)人", request):
            broad = "参加人员：" + recipient[1] + "工作人员"
            for section in candidate.sections:
                for p in section.paragraphs:
                    line = p.text.strip()
                    if p.anchors == ["U.1"] and re.match(re.escape(broad) + r"(?=[。，,]|$)", line):
                        p.text = line.replace(broad, "参加人员由" + recipient[1] + "派出", 1)
    if not (candidate.questions and candidate.sections and "审阅稿" in request and "单列待确认" in request):
        return
    # Discard a question only if every part concerns optional drafting choices or a named addressee.
    def already_answered(part):
        if "年份" in part and CALENDAR_DATE.search(request):
            return bool(re.fullmatch(r"(?:请确认\s*)?(?:\d+\s*月\s*\d+\s*日)?[\s\u4e00-\u9fff]{0,8}年份", part))
        if re.fullmatch(r"(?:以及|和|及)?(?:通知)?标题是否(?:需要)?体现(?:具体)?[^，、！？]{1,8}内容", part):
            return True
        return bool(recipient and re.fullmatch(r"(?:以及|和|及)?主送(?:各单位)?名称是否为[“「]?"
                                               + re.escape(recipient[1]) + r"[”」]?", part))
    candidate.questions = [q for q in candidate.questions
                           if not all(already_answered(p.strip()) for p in re.split(r"[、，,]", q.rstrip("。？?")))]


def _prepare_notice_numbers(candidate, usable, instruction, workflow):
    """Correct directly identifiable values and withhold isolated unverified quantities."""
    requested = re.search(r"(?:要求|请)?(?:正文)?保留[“「]([^”」]{2,36})[”」](?:的写法|的表述|原样)", instruction)
    if requested:
        # Keep the user's quoted grouping expression intact when the writer split it into two clauses.
        parts = requested[1].split("、")
        if (len(parts) == 2 and number_basis.QUANTITY.fullmatch(parts[0])
                and re.fullmatch(r"每[组队站箱份批]" + number_basis.QUANTITY.pattern, parts[1])):
            adjacent = re.compile(re.escape(parts[0]) + r"(?P<action>人员参加|组织|参加)?[，,、]\s*" + re.escape(parts[1]))
            for section in candidate.sections:
                for paragraph in section.paragraphs:
                    if paragraph.anchors == ["U.1"]:
                        paragraph.text = adjacent.sub(
                            lambda m: requested[1] + (m["action"] or "").removeprefix("人员"), paragraph.text)
    signed = bool(re.search(r"落款(?!日期)|发文(?:单位|机关)", instruction))
    # A venue is not automatically the issuer. A date attached to an event is not
    # the document's signing date, and an instruction to sign needs its own basis.
    for section in candidate.sections:
        kept = []
        for passage in section.paragraphs:
            line = passage.text.strip()
            if not line:
                # A spacer beside the explicit addressee is layout when real body text follows.
                if section is candidate.sections[0] and passage is section.paragraphs[0]:
                    following = section.paragraphs[1:]
                    heading = section.title.strip()
                    if not heading and len(following) >= 2:
                        heading, following = following[0].text.strip(), following[1:]
                    if (re.fullmatch(r"[^：:\n]{2,30}[：:]", heading) and heading[:-1] in instruction
                            and any(p.text.strip() and p.anchors and not _notice_tail_role(p.text.strip())
                                    for p in following)):
                        continue
                require(False, "生成结果含有未写完的正文段落。", "DRAFT_INCOMPLETE")
            sources = [usable[a] for a in passage.anchors if a in usable]
            role = _notice_tail_role(line)
            if role and role[0] == "issuer" and not signed:
                if any(re.search(r"(?:在|到|地点(?:为|是|在|[:：]))" + re.escape(role[1]), source) for source in sources):
                    continue
            if role and role[0] == "date" and "年" in line and not any(
                    re.search(r"(?:成文|落款|发文)日期[^，。；]{0,8}" + re.escape(line), source)
                    or re.search(r"[\n，,。]" + re.escape(line) + r"[。\n]?$", source) for source in sources):
                continue
            if re.search(r"(?:签字|签名)(?:领取|确认|登记|签收)", line) and not any(
                    re.search(r"(?:签字|签名)(?:领取|确认|登记|签收)", source) for source in sources):
                parts = re.split(r"(?<=[。；！？])", line)
                passage.text = "".join(part for part in parts if not re.search(r"(?:签字|签名)(?:领取|确认|登记|签收)", part))
                if not passage.text.strip():
                    continue
            kept.append(passage)
        section.paragraphs = kept
    candidate.sections = [section for section in candidate.sections if section.paragraphs or section.title]
    conflicts = number_basis.conflicting_rates(instruction)
    pending = ["请确认" + scope + verb + "的数量，以" + "还是".join(values.values()) + "为准？"
               for (scope, verb, unit), values in conflicts.items()]
    separate = re.search(r"(人数|件数|数量|份数|套数)(?:请|应)?单列(?:为|在)?待确认", instruction)
    if separate:
        original_pending = len(pending)
        for section in candidate.sections:
            for passage in section.paragraphs:
                parts = re.split(r"(?<=[。；！？])|(?<=[，,])(?=[^，。；！？]{0,15}" + separate[1] +
                                 r"(?:尚|仍|暂)?(?:待定|未定|没定|待确认))", passage.text)
                kept = []
                for claim in parts:
                    if re.fullmatch(r"[^，。；！？]{0,15}" + separate[1] + r"(?:尚|仍|暂)?(?:待定|未定|没定|待确认)"
                                    r"(?:[，,](?:另行|稍后|会上)?(?:确认|确定))?[。；！？]?", claim.strip()):
                        pending.append(claim.strip())
                    else:
                        kept.append(claim)
                passage.text = re.sub(r"[，,]$", "。", "".join(kept))
        if len(pending) == original_pending:
            field = separate[1]
            units = {"人数": "人", "件数": "件", "数量": "个件套份", "份数": "份", "套数": "套"}
            subject = re.compile(field + r"|(?:几|多少)个?[" + units[field] + r"]")
            for clause in re.split(r"[，,。；！？\n]", instruction):
                clause = clause.strip()
                if (subject.search(clause) and len(clause) <= 40
                        and re.search(r"(?:尚|还|暂|仍)?(?:没定|未定|没确定|未确定|未确认|待定)", clause)):
                    pending.append(clause + "。")
                    break
            else:
                pending.append(field + "待确认。")
    changes = []
    preserve_units = bool(re.search(r"保留[^，。；]{0,12}(?:原文|原有|原来的|单位|写法|表述)|按原文(?:写|表述)|不要换算", instruction))
    for section in candidate.sections:
        for passage in section.paragraphs:
            if not passage.anchors or not set(passage.anchors) <= usable.keys() or "｜" in passage.text:
                continue
            sources = [usable[a] for a in passage.anchors]
            known = {token for source in sources for token in numeric_tokens(source)}
            totals = {token for source in sources for token in number_basis.derived_totals(source)}
            def weekday(match):
                date_part = re.split(r"\s*[（(]?\s*(?:星期|周|礼拜)", match[0])[0]
                if not any(date_part in source for source in sources):
                    return match[0]
                try:
                    actual = "一二三四五六日"[date(*(int(match[k]) for k in ("year", "month", "day"))).weekday()]
                except ValueError:
                    return match[0]
                return match[0][:match.start("weekday") - match.start()] + actual + match[0][match.end("weekday") - match.start():]
            before = passage.text
            passage.text = DATE_WITH_WEEKDAY.sub(weekday, passage.text)
            if re.search(r"(?:星期|周[一二三四五六日天]).{0,15}改(?:对|正)", instruction):
                dates = re.findall(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", instruction)
                if len(set(dates)) == 1:
                    y, m, d = dates[0]
                    try:
                        label = "周" + "一二三四五六日"[date(int(y), int(m), int(d)).weekday()]
                    except ValueError:
                        label = None
                    if label:
                        passage.text = re.sub(r"(" + y + r"\s*年\s*0?" + str(int(m)) + r"\s*月\s*0?" +
                                              str(int(d)) + r"\s*[日号])(?!\s*[（(]?\s*(?:星期|周|礼拜))",
                                              r"\1（" + label + "）", passage.text)
            # A month/day without an explicit year cannot establish its weekday.
            if not any(re.search(r"\d{4}\s*年|星期|周[一二三四五六日天]", source) for source in sources):
                passage.text = re.sub(r"(\d{1,2}\s*月\s*\d{1,2}\s*[日号])\s*[（(]?(?:星期|周)[一二三四五六日天][）)]?",
                                      r"\1", passage.text)
            parts = re.split(r"(?<=[。；！？])", passage.text)
            for i, claim in enumerate(parts):
                unknown = []
                previous_claim = parts[i - 1] if i else ""
                def corrected(match):
                    token = number_basis.quantity_token(match)
                    if any(token in values for values in conflicts.values()):
                        unknown.append(match["unit"])
                        return match[0]
                    if re.search(number_basis.TOTAL_PREFIX + "$", claim[:match.start()]):
                        batch = number_basis.same_batch_total(previous_claim + claim[:match.start()], sources, match["unit"])
                        if batch is not None:
                            return match[0] if number_basis.amount(match["count"]) == batch else format(batch, "f") + match["unit"]
                        if token in totals:
                            return match[0]
                    source_quantity = number_basis.local_quantity(match, claim, sources)
                    if source_quantity:
                        original_spelling, original_token = source_quantity
                        if preserve_units or original_token != token:
                            return original_spelling
                    if token in known:
                        return match[0]
                    unknown.append(match["unit"])
                    return match[0]
                fixed = number_basis.QUANTITY.sub(corrected, claim)
                if not unknown:
                    parts[i] = fixed
                    continue
                # Retain a concrete question without publishing the invented number.
                subject = number_basis.QUANTITY.sub(lambda m: "多少" + m["unit"] if m["unit"] in unknown else m[0], claim)
                subject = subject.strip("。！？； ").strip()
                if not any(unit == scope[2] for unit in unknown for scope in conflicts):
                    pending.append("请确认“" + subject + "”中的数量。")
                parts[i] = ""
            passage.text = "".join(parts)
            if before != passage.text:
                changes.append({"from": before, "to": passage.text})
    if pending:
        for section in candidate.sections:
            section.paragraphs[:] = [p for p in section.paragraphs if p.text.strip()]
        candidate.sections[:] = [s for s in candidate.sections if s.paragraphs]
        substantive = any(p.text.strip() and not p.text.strip().endswith(("：", ":"))
                          and not _notice_tail_role(p.text)
                          for s in candidate.sections if s.title != NOTICE_RECEIPT for p in s.paragraphs)
        questions = list(dict.fromkeys(pending))
        if not substantive:
            raise RevisionIssue("NOTICE_QUANTITY_NEEDED", " ".join(questions[:2]))
        # Keep the signature at the end of the notice and distinguish the review copy.
        candidate.title = re.sub(r"[（(]审阅稿[）)]$", "", candidate.title).rstrip() + "（审阅稿）"
        questions_section = Section(title="待确认事项", paragraphs=[
            Passage(text=q, kind="fact", anchors=["U.1"]) for q in questions])
        index = len(candidate.sections)
        while index and (candidate.sections[index - 1].title in {NOTICE_RECEIPT, NOTICE_RECEIPT + "说明"}
                         or all(_notice_tail_role(p.text) for p in candidate.sections[index - 1].paragraphs)):
            index -= 1
        if index:
            last = candidate.sections[index - 1]
            tail = []
            while last.paragraphs and _notice_tail_role(last.paragraphs[-1].text):
                tail.insert(0, last.paragraphs.pop())
            if tail:
                candidate.sections.insert(index, Section(title="", paragraphs=tail))
        candidate.sections.insert(index, questions_section)
        workflow["pending_quantities"] = questions
    if changes:
        workflow["notice_number_edits"] = changes


def explicit_plan_issuer(candidate, request):
    if candidate.profile != "plan-draft":
        return None
    # Only consume an unambiguous named organization, never guess an omitted issuer.
    match = re.search(r'落款(?:单位)?\s*(?:写|用|为|是|[:：])\s*[“"「]?([^，,。；;\n”"」]+)', request)
    issuer = match[1].strip() if match else ""
    return issuer if NOTICE_ISSUER.fullmatch(issuer) else None


def has_plan_issuer(candidate, issuer):
    return any("落款" in s.title and any(line.strip().rstrip("。.") == issuer
               for p in s.paragraphs for line in p.text.splitlines()) for s in candidate.sections)


def _complete_plan_issuer(candidate, usable, request):
    if (issuer := explicit_plan_issuer(candidate, request)) and not has_plan_issuer(candidate, issuer):
        require("U.1" in usable, "落款缺少本轮原话依据。", "BASIS_INVALID")
        candidate.sections.append(Section(title="落款", paragraphs=[
            Passage(text=issuer, kind="fact", anchors=["U.1"])]))


def check_draft(candidate, usable):
    require(candidate.title.strip() and candidate.sections and all(
        s.paragraphs and all(p.text.strip() for p in s.paragraphs) for s in candidate.sections
    ), "生成结果缺少标题或正文。", "DRAFT_INCOMPLETE")
    request = usable.get("U.1", "")
    relative_notice = candidate.profile == "notice-draft" and RELATIVE_WORK_DATES.search(request)
    keep_relative = relative_notice and KEEP_RELATIVE_DATES.search(request)
    separate_days = relative_notice and SEPARATE_WORK_DATES.search(request) and not CONTINUOUS_WORK_DATES.search(request)
    check_notice_receipt(candidate, usable.get("U.1", ""))
    if issuer := explicit_plan_issuer(candidate, usable.get("U.1", "")):
        require(has_plan_issuer(candidate, issuer),
                "成稿缺少用户明确指定的落款单位，未生成 Word。", "DRAFT_INCOMPLETE")
    facts_only = candidate.profile in {"experience-draft", "notice-draft"}
    follow_up = FOLLOW_UP_SECTIONS.get(candidate.profile)
    if follow_up:
        require(len(candidate.sections) > 1 and candidate.sections[-1].title == follow_up
                and all(s.title != follow_up for s in candidate.sections[:-1]),
                f"已知情况和「{follow_up}」须分开，建议单列在文末。", "DRAFT_INCOMPLETE")
    require(bool(candidate.tasks) == (candidate.profile == "plan-draft"),
            "成稿类型与任务表不符。", "DRAFT_INCOMPLETE")
    if candidate.profile == "opinion-draft":
        require(sum(s.title == OPINION_FEEDBACK for s in candidate.sections) == 1,
                "征求意见稿须有一份逐项反馈表。", "DRAFT_INCOMPLETE")

    def check_basis(text, anchors, fact, location):
        require(set(anchors) <= usable.keys() and (not fact or anchors),
                f"{location}缺少有效的本次材料依据，未生成 Word。", "BASIS_INVALID")
        if candidate.profile == "notice-draft":
            for match in DOCUMENT_CITATION.finditer(text):
                require(any(match["title"] in usable[a] and match["number"] in usable[a] for a in anchors),
                        f"{location}的文件全称缺少原文依据，须仅保留已给出的文号。", "BASIS_INVALID")
            for match in NOTICE_MEDIA_ASSIGNMENT.finditer(text):
                require(_has_notice_media_basis(match, [usable[a] for a in anchors]),
                        f"{location}新增了无依据的媒体宣传安排。", "BASIS_INVALID")
        if relative_notice:
            for claim in (NOTICE_APPROVAL, NOTICE_ISSUED_BASIS):
                for match in claim.finditer(text):
                    require(any(match[0] in usable[a] for a in anchors),
                            f"{location}的审批或已发通知表述未得到确认。", "BASIS_INVALID")
            for clause in re.split(r"[。；;！？\n]", text):
                if not REMOTE_WORK.search(clause):
                    continue
                if separate_days:
                    require(not notice_scope.claims_whole_event(clause),
                            f"{location}把办公安排扩大到会议期间。", "BASIS_INVALID")
                if keep_relative:
                    require(not CALENDAR_DATE.search(clause),
                            f"{location}把要求保留的相对办公日期换算成了具体日期。", "BASIS_INVALID")
        known = {n for a in anchors for n in numeric_tokens(usable[a])}
        # A derived total supports a stated total, never a per-person or per-group limit.
        for claim in re.finditer(number_basis.TOTAL_PREFIX + number_basis.QUANTITY.pattern, text):
            token = number_basis.quantity_token(claim)
            if any(token in number_basis.derived_totals(usable[a]) for a in anchors):
                text = text.replace(claim[0], " ", 1)
        for number in number_basis.DOC_NO.finditer(text):
            cited_id = re.split(r"根据|按照|依据|参照|执行|文号(?:为|是)?", number[0])[-1]
            require(any(cited_id in usable[a] for a in anchors), f"{location}的文号缺少对应出处。", "BASIS_INVALID")

        if relative_notice and "U.1" in anchors and not keep_relative:
            user_window = event_window(usable["U.1"], require_event_context=True)
            public_window = event_window(usable["P.1"]) if "P.1" in anchors else None
            if window := user_window or public_window:
                # Exempt only the calendar expressions, never their component numbers elsewhere.
                def backed_date(match):
                    year = int(match["year"]) if match["year"] else None
                    month_day = (int(match["month"]), int(match["day"]))
                    if any(month_day == (month, day) and (year is None or year == allowed_year)
                           for allowed_year, month, day in window):
                        return " "
                    if any(match[0] in usable[anchor] for anchor in anchors):
                        return match[0]
                    require(False, f"{location}的日期「{match[0]}」与会期及相对日期要求不符。", "BASIS_INVALID")

                def backed_range(match):
                    span = event_window(match[0])
                    if not span:
                        return match[0]
                    dates = tuple((m, d) for _, m, d in span[:2])
                    expected = tuple((m, d) for _, m, d in window[2:])
                    schedule_text = re.match(r"\s*(?:为|拟|统一|安排|实行|执行)*(?:居家|在家|远程)办公",
                                             match.string[match.end():])
                    if schedule_text:
                        require(dates == expected,
                                f"{location}的办公日期区间与用户要求不符。", "BASIS_INVALID")
                    if dates != expected:
                        return match[0]
                    require(CONTINUOUS_WORK_DATES.search(usable["U.1"]),
                            f"{location}把会前、会后两个日期写成了连续区间。", "BASIS_INVALID")
                    for (actual_year, _, _), (year, _, _) in zip(span[:2], window[2:]):
                        require(actual_year is None or actual_year == year,
                                f"{location}的日期年份与会期依据不符。", "BASIS_INVALID")
                    return " "

                def backed_day_pair(match):
                    year = int(match["year"]) if match["year"] else None
                    dates = {(int(match["month"]), int(match[key])) for key in ("day", "second_day")}
                    expected = {(month, day) for _, month, day in window[2:]}
                    if dates == expected and all(year is None or year == value[0] for value in window[2:]):
                        return " "
                    return match[0]

                text = EVENT_RANGE.sub(backed_range, text)
                text = EVENT_DATE.sub(backed_date, EVENT_DAY_PAIR.sub(backed_day_pair, text))
        missing = sorted(numeric_tokens(text) - known)
        require(not missing, f"{location}的数字「{'、'.join(missing)}」缺少对应出处，未生成 Word。", "BASIS_INVALID")

    if separate_days:
        require(not (REMOTE_WORK.search(candidate.title) and WHOLE_EVENT.search(candidate.title)
                     and not RELATIVE_WORK_DATES.search(candidate.title)),
                "标题把办公安排扩大到会议期间。", "BASIS_INVALID")
    for s_idx, section in enumerate(candidate.sections, 1):
        if candidate.profile == "notice-draft":
            if section.title == NOTICE_RECEIPT:
                fields = table_row(section.paragraphs[0].text)
                require(s_idx == len(candidate.sections) and len(section.paragraphs) == 1
                        and all(fields) and len(set(fields)) == len(fields),
                        "报名回执须为末节，且只列一行不重复的填报字段。", "DRAFT_INCOMPLETE")
            elif any("｜" in p.text for p in section.paragraphs):
                rows = [table_row(p.text) for p in section.paragraphs]
                require(all(len(row) == len(NOTICE_COLUMNS) and all(row) for row in rows),
                        f"通知第 {s_idx} 节的场次表不完整。", "DRAFT_INCOMPLETE")
        if candidate.profile in {"plan-draft", "opinion-draft"} and section.title in PLAN_TABLE_COLUMNS:
            columns = PLAN_TABLE_COLUMNS[section.title]
            rows = [table_row(p.text) for p in section.paragraphs]
            require(all(len(row) == len(columns) and all(row) for row in rows),
                    f"「{section.title}」的表格行不完整。", "DRAFT_INCOMPLETE")
        for p_idx, p in enumerate(section.paragraphs, 1):
            require(not facts_only or p.kind == "fact", "经验材料与通知只能写入有依据的事实和安排。", "BASIS_INVALID")
            if follow_up:
                expected_kind = "proposal" if section.title == follow_up else "fact"
                require(p.kind == expected_kind, "已知情况和文末建议必须分别成段。", "BASIS_INVALID")
            text = PARAGRAPH_LIST_MARKER.sub("", p.text, count=1)
            if candidate.profile == "plan-draft" and section.title == policy_actions.AGENDA and p.kind == "proposal":
                # 哪一天 and 哪一场 ask the meeting to choose; they state no date or quantity.
                text = re.sub(r"哪一(?=[天日场组份次名位条件])", "哪", text)
            check_basis(text, p.anchors, p.kind == "fact" or bool(follow_up), f"第 {s_idx} 节第 {p_idx} 段")
        check_basis(section.title, [a for p in section.paragraphs for a in p.anchors], False, f"第 {s_idx} 节标题")
    for t_idx, task in enumerate(candidate.tasks, 1):
        fields = [task.action, task.lead, task.support, task.timing, task.deliverable, task.acceptance,
                  task.contact, task.phone]
        require(task.action.strip(), f"任务 {t_idx} 缺少工作事项，请核对。", "DRAFT_INCOMPLETE")
        require(not (re.search(r"分工表|任务表", task.action)
                     and re.search(r"(?:增加|新增|添加|增设|补上|补充|设置).*(?:字段|栏目|[栏列])", task.action)),
                f"任务 {t_idx} 把表格字段或排版修改写成了工作任务，未生成 Word。", "DRAFT_INCOMPLETE")
        check_basis(" ".join(fields), task.anchors, task.status == "confirmed", f"任务 {t_idx}")
    if candidate.profile == "plan-draft":
        leads_by_anchor = {}
        for task in candidate.tasks:
            if task.status == "confirmed":
                for anchor in task.anchors:
                    leads_by_anchor.setdefault(anchor, []).append(task.lead)
        for anchor, leads in leads_by_anchor.items():
            for group in JOINT_CENTER.finditer(usable[anchor]):
                center, branch = group.group("center", "branch")
                joint = re.compile(rf"{re.escape(center)}\s*(?:以及|及|与|和|、|，|,)\s*{branch}")
                require(any(joint.search(lead) for lead in leads),
                        f"分工表未保留 {anchor} 中「{center}及{branch}」的共同责任，请核稿。", "BASIS_INVALID")
    anchors = [a for s in candidate.sections for p in s.paragraphs for a in p.anchors]
    anchors += [a for t in candidate.tasks for a in t.anchors]
    check_basis(candidate.title, anchors, False, "总标题")


def case_sections(candidate):
    return [(s.title, [p.text for p in s.paragraphs]) for s in candidate.sections]


def _clear_optional_placeholders(task, source_lines):
    for name in ("contact", "phone"):
        value = getattr(task, name).strip()
        if value.lower() in {"string", "tbd", "todo", "xxx"} and not any(value in q for q in source_lines):
            setattr(task, name, "")


def _complete_plan_fields(candidate, usable, pending, request):
    for section in candidate.sections:
        # The layout already prints the section title. A repeated heading is not a table row.
        if (len(section.paragraphs) > 1
                and HEADING_LIST_MARKER.sub("", section.paragraphs[0].text.strip()) == section.title):
            section.paragraphs.pop(0)
    tables = {title: [s for s in candidate.sections if s.title == title] for title in PLAN_TABLE_COLUMNS}
    require(all(len(sections) <= 1 for sections in tables.values()), "方案表格出现重复章节。", "DRAFT_INCOMPLETE")
    if "季度" in request and "分工表" in request:
        require(all(len(table_row(task.timing)) == 4 and all(table_row(task.timing)) for task in candidate.tasks),
                "季度分工表须逐行列全四季，原文未明确的季度可填待明确。", "DRAFT_INCOMPLETE")
    if all(word in request for word in ("帮办", "代办")):
        require(tables[PLAN_COMPARISON], "方案缺少帮办与代办情形对照。", "DRAFT_INCOMPLETE")
        if pending and MEETING.search(request):
            require(tables[PLAN_AGENDA], "会商稿缺少会商议题表。", "DRAFT_INCOMPLETE")
    if pending and tables[PLAN_AGENDA]:
        labels = [table_row(p.text)[0] for p in tables[PLAN_AGENDA][0].paragraphs]
        parent_labels = [re.sub(r"（[^（）]+）$", "", label) for label in labels]
        require(
            [label for label, _ in groupby(parent_labels)] == pending
            and len(set(labels)) == len(labels)
            and all(label not in labels or parent_labels.count(label) == 1 for label in pending),
            "会商议题表须按用户列出的待定事项逐项排列，分项须紧邻且不得重复。", "DRAFT_INCOMPLETE",
        )
    paragraphs = [p.text for section in candidate.sections for p in section.paragraphs]
    for label in pending:
        require(any(re.match(rf"^\s*(?:建议[：:]\s*)?(?:[（(]?[一二三四五六七八九\d]+[）)、.．]\s*)?"
                             rf"{re.escape(label)}\s*[：:。]", paragraph)
                    and all(other not in paragraph for other in pending if other != label)
                    for paragraph in paragraphs),
                f"正文未单列待核事项「{label}」，请核稿。", "DRAFT_INCOMPLETE")
    for task in candidate.tasks:
        source_lines = [usable[a] for a in task.anchors if a in usable and not a.startswith("U.")]
        _clear_optional_placeholders(task, source_lines)
        if task.status == "confirmed" and PAPER_INTAKE.search(task.action):
            for name in ("lead", "support"):
                value = getattr(task, name)
                if value and not any(_responsibility_covers_action(task, value, q) for q in source_lines):
                    setattr(task, name, "")
        for name, (value, _) in _explicit_submissions(task, source_lines).items():
            setattr(task, name, value)
        joint, _ = _explicit_joint_lead(task, source_lines)
        if joint:
            task.lead = joint
        if (task.status != "confirmed" or "在线培训" not in task.action
                or task.deliverable.strip() not in {"", "待明确", "待核", "待确认"}):
            continue
        outcomes = {match.group("outcome") for anchor in task.anchors if not anchor.startswith("U.")
                    for match in NAMED_ONLINE_TRAINING.finditer(usable.get(anchor, ""))}
        if len(outcomes) == 1:
            outcome = outcomes.pop()
            if outcome in task.action or "开展在线培训" in task.action:
                task.deliverable = outcome


def _withhold_unsupported_proposed_timings(candidate, usable):
    withheld = []
    for index, task in enumerate(candidate.tasks, 1):
        if (task.status != "proposed" or len(table_row(task.timing)) != 1
                or not task.anchors or not set(task.anchors) <= usable.keys()):
            continue
        known = {token for anchor in task.anchors for token in numeric_tokens(usable[anchor])}
        if numeric_tokens(task.timing) - known:
            # Keep the proposal available for review without borrowing another task's deadline.
            task.timing = ""
            withheld.append(f"T{index}")
    return withheld


def _expert_brief(model, material_text, materials, generate):
    key = hashlib.sha256(material_text.encode()).hexdigest()
    started = monotonic()
    record = {"input_key": key, "status": "incomplete"}
    try:
        rules = (TEAM / "references/素材与框架.md").read_text(encoding="utf-8")
        anchors = {anchor for material in materials for anchor in material["blocks"]}
        anchor_type = Literal[tuple(sorted(anchors))]
        nonblank = Annotated[str, Field(pattern=r"\S")]
        item_type = create_model("BoundSourcedBriefItem", __base__=SourcedBriefItem,
                                 text=(nonblank, ...), anchors=(list[anchor_type], Field(min_length=1)))
        brief_type = create_model("BoundExpertBrief", __base__=ExpertBrief,
                                  input_key=(Literal[key], ...),
                                  items=(list[item_type], Field(min_length=1, max_length=16)),
                                  outline=(list[nonblank], Field(min_length=2, max_length=6)))
        brief, metadata = generate(model, rules + "\ninput_key: " + key
                                   + "\n可用依据编号：" + json.dumps(sorted(anchors), ensure_ascii=False)
                                   + "\n" + material_text, brief_type)
        record["model"] = metadata
        record["brief"] = brief.model_dump()
        invalid = []
        if brief.input_key != key:
            invalid.append("input_key")
        if not 0 < len(brief.items) <= 16:
            invalid.append("items_count")
        if not 2 <= len(brief.outline) <= 6:
            invalid.append("outline_count")
        for i, entry in enumerate(brief.items):
            if not entry.text.strip():
                invalid.append(f"items[{i}].text")
            if not entry.anchors or not set(entry.anchors) <= anchors:
                invalid.append(f"items[{i}].anchors")
        invalid += [f"outline[{i}]" for i, line in enumerate(brief.outline) if not line.strip()]
        if invalid:
            record["invalid_fields"] = invalid
        require(not invalid, "素材单缺少本轮有效依据或框架。", "BRIEF_BINDING_INVALID")
        record["status"] = "completed"
        # The binding hash is host metadata, not another material for the writer to classify.
        return json.dumps(brief.model_dump(exclude={"input_key"}), ensure_ascii=False), record
    except (RevisionIssue, OSError, ValueError) as error:
        if hasattr(error, "rejected_exchange"):
            record["rejected_exchange"] = error.rejected_exchange
        record["error"] = {"code": getattr(error, "code", "BRIEF_ERROR"), "message": str(error),
                           "elapsed_seconds": round(monotonic() - started, 1)}
        if hasattr(error, "model_failure"):
            record["error"]["model_failure"] = error.model_failure
        return "", record
    finally:
        record["elapsed_seconds"] = round(monotonic() - started, 1)


def _conference_review(model, request, candidate, usable, pending, generate, *, team=False, case=None, references=None, ledger=None, draft_issue=None, source_plan=None, intake=None):
    paragraphs = {f"P{i}.{j}": p for i, s in enumerate(candidate.sections, 1)
                  for j, p in enumerate(s.paragraphs, 1)}
    tasks = {f"T{i}": t for i, t in enumerate(candidate.tasks, 1)}
    plan = candidate.profile == "plan-draft"
    targets = paragraphs.keys() | tasks.keys()
    if plan:
        targets |= {"TASKS"}
    context = {
        "交件类型": PROFILES[candidate.profile], "可用依据": {"U.1": request, **usable},
        "正文与章节": {f"P{i}.{j}": {"section": section.title, **p.model_dump()}
                     for i, section in enumerate(candidate.sections, 1) for j, p in enumerate(section.paragraphs, 1)},
        "标题": candidate.title, "分工表": {key: task.model_dump(exclude_defaults=True) for key, task in tasks.items()},
        "可用检查位置": [*paragraphs, *tasks] + (["TASKS"] if plan else []),
    }
    if intake:
        context["需核实影响的问题"] = {"slots": intake["slots"],
                                  "items": {key: item["text"] for key, item in intake["items"].items()}}
    if draft_issue:
        context["首稿程序查出的待修问题"] = draft_issue
    if ledger:
        context["材料用途"] = ledger
    if references and not ledger:
        context["外部参考，仅核对建议覆盖，不得写作既定事实"] = references
    payload = json.dumps(context, ensure_ascii=False, sort_keys=True)
    basis = hashlib.sha256(payload.encode()).hexdigest()
    report = {"basis_key": basis, "status": "incomplete", "stages": {}}
    inputs = "\n\n本轮数据：\n" + json.dumps({"basis_key": basis, **context}, ensure_ascii=False)
    stage = "review"
    started = monotonic()
    try:
        rules = [(ROOT / "prompts/review.md").read_text(encoding="utf-8")]
        if plan:
            rules.append((ROOT / "prompts/会商稿核稿.md").read_text(encoding="utf-8"))
            rules.append("查看实际分工表中的责任分工，配合有据时与牵头同格分别标注。"
                         "TASKS 只定位缺失的实际工作任务。已有工作行的验收方式定位到该 T 行，"
                         "文末需要补充的待议事项定位对应 P 段。纯格式优化建议用 advisory。")
        if team and not plan:
            rules.append((TEAM / "references" / (TEAM_RECIPES[candidate.profile] + ".md")).read_text(encoding="utf-8"))
        rules.append("只列会改变执行或事实含义的问题。逐条检查决定是否同时进入正文和对应分工，"
                     "主语与动作是否对应，分别核条件、时限、缺失的执行项、试点事实与提议。"
                     "不同执行时点的核查是不同任务，以已确认核查分工逐项比对，不要据此提出责任冲突。"
                     "纸质受理没有指名负责人时允许留空，恢复后的补录责任不能用于推定此前的接件人。"
                     "会议未采纳的试点意见只能作为建议，不能仅因未写进已定要求而指为错误。"
                     "没有规定的留存成果无需补造。checked_ids 列出实际读过的所有段和任务。")
        # Bind the model contract as well as the host checks to this exact draft.
        position = Literal[tuple(sorted(targets))]
        finding_type = create_model("BoundReviewFinding", __base__=ReviewFinding,
                                    target_ids=(list[position], ...))
        extra_fields = {}
        if intake:
            rules.append("逐项独立核实 Q 问题和 N 未引用的待议说明。每项必须整体对应 slots 中同一来源同一字段，"
                         "且该事项在当前正文、分工空格、议题或试点建议中已保留，才能选该 slot。"
                         "若问题含额外事项或材料冲突、无法核实其全部含义，选 unresolved 并写一句需要用户确认的具体问题。"
                         "不能因主题相近把新增日期、活动、费用或不同责任塞进已授权议题。")
            impact = create_model("MeetingIssueImpact", __base__=StrictModel,
                                  slot=(Literal[(*intake["slots"], "unresolved")], ...),
                                  clarification=(str, ...))
            verdicts = create_model("MeetingIssueDecisions", __base__=StrictModel,
                                    **{key: (impact, ...) for key in intake["items"]})
            extra_fields["issue_decisions"] = (verdicts, ...)
        review_type = create_model("BoundConferenceReview", __base__=ConferenceReview,
                                   basis_key=(Literal[basis], ...), checked_ids=(list[position], ...),
                                   findings=(list[finding_type], ...), **extra_fields)
        review, metadata = generate(model, "\n\n".join(rules) + inputs, review_type)
        report["stages"][stage] = metadata
        report["review"] = review.model_dump()
        require(review.basis_key == basis, "核稿依据键与本轮稿件不一致。", "REVIEW_BINDING_INVALID")
        require(review.checked_ids, "核稿没有报告任何实际检查位置。", "REVIEW_BINDING_INVALID")
        checked = set(review.checked_ids)
        require(checked <= targets, "核稿位置不在本轮可检查清单内。", "REVIEW_BINDING_INVALID")
        if ledger:
            require(paragraphs.keys() | tasks.keys() <= checked, "独立核稿未检查全部正文与分工。", "REVIEW_BINDING_INVALID")
        if plan and tasks and tasks.keys() <= checked:
            # TASKS is a synthetic location for missing rows, not a separate source.
            checked.add("TASKS")
        for finding in review.findings:
            require(finding.target_ids and set(finding.target_ids) <= checked
                    and set(finding.basis_ids) <= context["可用依据"].keys() | (references or {}).keys()
                    and (finding.level != "blocking" or finding.basis_ids),
                    "核稿发现缺少有效定位或依据。", "REVIEW_BINDING_INVALID")
        if intake:
            blocked = []
            for key, verdict in review.issue_decisions.model_dump().items():
                item = intake["items"][key]
                slot = verdict["slot"]
                cited = [usable.get(a, "") for a in slot.split("/")[:-1]]
                new_numbers = numeric_tokens(ANCHOR.sub("", item["text"])) - numeric_tokens(" ".join(cited))
                if slot == "unresolved" or item["writer_slot"] not in {None, slot} or new_numbers:
                    question = (verdict["clarification"].strip() or item["text"]) if key.startswith("N") else item["text"]
                    blocked.append(ANCHOR.sub("", question).strip())
            if blocked:
                candidate.questions = list(dict.fromkeys(blocked))[:2]
                report.update(status="needs_input", remaining_questions=list(dict.fromkeys(blocked)))
                return candidate, report, "方案首稿已保留。请确认下列影响执行的事项。"
        fixes = [f.model_dump() for f in review.findings if f.level == "blocking"]
        if draft_issue:
            require(any(draft_issue["target"] in f["target_ids"] for f in fixes),
                    "独立核稿未处理首稿已定位的问题。", "REVIEW_UNRESOLVED")
        if fixes and source_plan:
            quotes = _quoted_opinions(source_plan)
            cited = {key: para for key, para in paragraphs.items() if context["正文与章节"][key]["section"] == TRIAL_SECTION
                     and para.kind == "proposal" and len(para.anchors) == 1 and quotes.get(para.anchors[0]) == para.text}
            named = {key for fix in fixes for key in fix["target_ids"]}
            protected_quotes = {key: para.model_dump() for key, para in cited.items() if key in named}
            if protected_quotes:
                report["retained_source_quotes"] = protected_quotes
            # All cells of an exactly projected decision, including empty cells, are frozen.
            # The verifier receives the original finding even when the editor has no authority to change it.
            projected = {f"T{i}": TaskItem(**row) for i, row in enumerate(source_plan["actions"], 1)}
            protected_rows = {key: row.model_dump() for key, row in projected.items()
                              if key in named and tasks.get(key) == row}
            if protected_rows:
                report["retained_source_rows"] = protected_rows
            protected_facts = {key: p.model_dump() for key, p in paragraphs.items()
                               if key in named and p.kind == "fact" and p.anchors
                               and all(a in source_plan["decisions"] for a in p.anchors)
                               and p.text == " ".join(source_plan["decisions"][a] for a in p.anchors)}
            if protected_facts:
                report["retained_source_facts"] = protected_facts
            protected = protected_quotes.keys() | protected_rows.keys() | protected_facts.keys()
            if protected:
                fixes = [{**fix, "target_ids": editable} for fix in fixes
                         if (editable := [key for key in fix["target_ids"] if key not in protected])]
        if not fixes:
            report["status"] = "checked_without_repair"
            return candidate, report, "已独立核稿，无需定点返修；初稿待人工核定。"

        stage = "repair"
        started = monotonic()
        allowed = {target for fix in fixes for target in fix["target_ids"]}
        repair_type = ConferenceRepair
        repair_format = "只回传实际改变的段或行；可用多个替换项拆开原段。"
        table_widths = {}
        if plan:
            table_widths = {
                f"P{i}.{j}": len(PLAN_TABLE_COLUMNS[section.title])
                for i, section in enumerate(candidate.sections, 1) if section.title in PLAN_TABLE_COLUMNS
                for j, _ in enumerate(section.paragraphs, 1)
            }
            edit_types = {}
            for key in sorted(allowed - {"TASKS"}):
                if key in table_widths:
                    width = table_widths[key]
                    item_type = create_model(f"RepairTableRow_{width}", __base__=StrictModel,
                                             cells=(list[str], Field(min_length=width, max_length=width)),
                                             anchors=(list[str], ...))
                    edit_types[key] = (list[item_type], Field(max_length=1))
                else:
                    edit_types[key] = (list[TaskItem if key in tasks else Passage], ...)
            fields = {"basis_key": (Literal[basis], ...),
                      "new_tasks": (list[TaskItem], ... if "TASKS" in allowed else Field(max_length=0))}
            if edit_types:
                fields["edits"] = (create_model("ConferenceRepairPositions", __base__=StrictModel, **edit_types), ...)
            repair_type = create_model("BoundConferenceRepair", __base__=StrictModel, **fields)
            repair_format = (
                "edits 中每个键只出现一次，只能使用 Schema 已列的键，未改填空数组。"
                "普通段落或任务可用多项拆开。正文表格行最多一个替换项，按原列顺序填写 cells；anchors 只能取该行原有依据或本项发现指明的可用依据。"
            )
        # Send the original text once per relevant anchor; unrelated draft sections stay private to review.
        source_ids = {a for fix in fixes for a in fix["basis_ids"]}
        target_fragments = {key: (paragraphs.get(key) or tasks[key]).model_dump()
                            for key in sorted(allowed - {"TASKS"})}
        source_ids.update(a for fragment in target_fragments.values() for a in fragment["anchors"])
        all_sources = {**(references or {}), **context["可用依据"]}
        repair_input = {"basis_key": basis, "核稿发现": fixes, "待修片段": target_fragments,
                        "可用依据": {key: all_sources[key] for key in sorted(source_ids)}}
        if ledger:
            repair_input["本次引用的用途和已定分工"] = {
                key: {"用途": record["用途"], "已定分工": [
                    fact for fact in record.get("已确认核查分工", []) if fact["依据"] in source_ids]}
                for key, record in ledger.items() if any(a.startswith(key + ".") for a in source_ids)}
        if "TASKS" in allowed:
            repair_input["已有任务名称（仅查重）"] = [t.action for t in tasks.values()]
        report["repair_input"] = {"targets": sorted(allowed), "anchors": sorted(source_ids)}
        repair, metadata = generate(
            model, "你是本轮主笔。以下是独立核稿已定位的问题；只修 blocking 指向的目标，"
            "保留该目标中其余正确内容与原顺序。" + repair_format +
            "TASKS 只授权补充材料已明确但分工表没有的实际工作任务；没有此类动作时 new_tasks 填空。"
            "不得自行定组织单位、人选、日期、成果形式或验收标准。"
            "联系人和电话列由导出程序提供，逐行数据写入 contact 和 phone；未定时留空。"
            "增加表格字段、栏目或调整排版属于格式修改，不得写成 new_tasks 的工作任务。"
            "未定的任务字段用空字符串，已确定动作即使部分字段未定也用 confirmed。"
            "仅将依据明确分配给当前行具体动作的单位写入 lead 或 support。原行合写不同时间或责任的动作时用多个 replacements 拆行；"
            "删除已由独立任务行覆盖的汇总式重复动作，尤其不能把首次登记合入恢复后补录或准备清单。"
            "核稿建议与材料原文出入时以原文为准。材料未确认的责任不要填入正文；不要把待定分工从表格转入正文。"
            "总体责任单位单列总体落实，不得下放给培训、技术服务等子项。"
            "当前行组织方未定时留空；牵头不明时仍保留明确负责当前动作的列名单位，"
            "不能只在 action 中写单位。来源明确的交付成果写入 deliverable，不只留在 action。"
            "正文已确定的事实用 fact，工作方案未定安排与阶段总结或交流稿文末建议用 proposal。外部参考只能用于完善拟议步骤，"
            "参考编号仅可出现在核稿发现中，不可加入改写段落或任务的 anchors。"
            "经验材料和通知只可修改已定位正文段落。通知的对象、日期、渠道、内容须按场次配对。"
            "所有改写沿用可核的来源编号，U.1 只有在原候选允许引用时才可用作成稿锚点。"
            "如有发现无法安全修复，不要凑数。原样返回 basis_key。\n核稿发现："
            + "\n本轮数据：\n" + json.dumps(repair_input, ensure_ascii=False), repair_type)
        report["stages"][stage] = metadata
        report["rejected_repair"] = repair.model_dump()
        require(repair.basis_key == basis, "定点返修与核稿稿件不一致。", "REVIEW_BINDING_INVALID")
        changes = []

        def replacements(edits, originals):
            patched = {}
            for edit in edits:
                key = edit.target_id
                require(key in originals and key in allowed and key not in patched and edit.replacements,
                        "定点返修超出核稿位置或会删除原有内容。", "REVIEW_SCOPE_INVALID")
                if edit.replacements != [originals[key]]:
                    changes.append({"target_id": key, "before": originals[key].model_dump(),
                                    "after": [item.model_dump() for item in edit.replacements]})
                patched[key] = edit.replacements
            return patched

        if plan:
            p_changes, t_changes = [], []
            for key, parts in (getattr(repair, "edits", None) or ()):
                if not parts:
                    continue
                if key in tasks:
                    t_changes.append(TaskEdit(target_id=key, replacements=parts))
                    continue
                if key in table_widths:
                    row = parts[0]
                    original = paragraphs[key]
                    permitted = set(original.anchors) | {a for f in fixes if key in f["target_ids"]
                                                        for a in f["basis_ids"] if a in usable}
                    require(row.anchors and set(row.anchors) <= permitted,
                            f"定点返修越出表格行 {key} 的引用范围。", "REVIEW_SCOPE_INVALID")
                    parts = [_replace_table_cells(original, row.cells, row.anchors, table_widths[key], key)]
                p_changes.append(ParagraphEdit(target_id=key, replacements=parts))
        else:
            p_changes, t_changes = repair.paragraphs, repair.tasks
        for task in [*repair.new_tasks, *(row for edit in t_changes for row in edit.replacements)]:
            _clear_optional_placeholders(task, [usable[a] for a in task.anchors if a in usable])
        p_edits = replacements(p_changes, paragraphs)
        t_edits = replacements(t_changes, tasks)
        if repair.new_tasks:
            require("TASKS" in allowed, "核稿未授权新增表格任务。", "REVIEW_SCOPE_INVALID")
            require(all(t not in candidate.tasks for t in repair.new_tasks),
                    "返修新增了重复任务。", "REVIEW_SCOPE_INVALID")
            changes.append({"target_id": "TASKS", "before": None,
                            "after": [t.model_dump() for t in repair.new_tasks]})
        require(changes, "主笔未返回可写入的修改。", "REVIEW_NO_CHANGES")
        revised = candidate.model_copy(deep=True)
        for i, section in enumerate(revised.sections, 1):
            section.paragraphs = [item for j, p in enumerate(section.paragraphs, 1)
                                  for item in p_edits.get(f"P{i}.{j}", [p])]
        revised.tasks = [item for i, t in enumerate(revised.tasks, 1)
                         for item in t_edits.get(f"T{i}", [t])] + repair.new_tasks
        check_draft(revised, usable)
        if case is not None:
            case["counts"] = submission.check_sections(case_sections(revised))
        # Record actual mutations after the same program checks as the original draft.
        for change in changes:
            if change["target_id"].startswith("T"):
                rows = repair.new_tasks if change["target_id"] == "TASKS" else t_edits[change["target_id"]]
                change["after"] = [row.model_dump() for row in rows]
        report.update(status="changes_applied", changes=changes)
        report.pop("rejected_repair")
        changed = {entry["target_id"] for entry in changes}
        report["unaddressed_targets"] = sorted(allowed - changed)
        return revised, report, "已独立核稿并写回定点修改；核稿发现及待复核项见《依据与待核》。"
    except (RevisionIssue, OSError, ValueError) as error:
        report["failed_stage"] = stage
        if hasattr(error, "rejected_exchange"):
            report["rejected_exchange"] = error.rejected_exchange
        report["error"] = {"code": getattr(error, "code", "REVIEW_ERROR"), "message": str(error),
                           "elapsed_seconds": round(monotonic() - started, 1)}
        if hasattr(error, "model_failure"):
            report["error"]["model_failure"] = error.model_failure
        return candidate, report, "独立核稿或返修未完成，保留原初稿，待人工复核。"


def _named_responsibility(value, quote):
    value = re.sub(r"^(?:共同牵头|共同负责|总体落实|责任单位|牵头|配合|组织)[：:]\s*", "", value.strip())
    value = re.sub(r"(?:共同负责|共同牵头|负责|牵头)$", "", value)
    if not value or UNCERTAIN.search(value):
        return False
    if value in quote:
        return True
    return re.sub(r"及|与|和|、", "、", value) in re.sub(r"及|与|和|、", "、", quote)


PAPER_INTAKE = re.compile(
    r"(?:改用|使用|采用|安排|组织|开展|办理|进行|实施|先行|首次)\s*纸质登记(?!表|字段|台账|记录|补录|回填|复核)"
)


def _responsibility_covers_action(task, value, quote):
    if not _named_responsibility(value, quote):
        return False
    if task.status != "confirmed" or (not PAPER_INTAKE.search(task.action) and task.action.strip() != "纸质登记"):
        return True
    # Paper intake and subsequent re-entry are separate actions even inside a summary row.
    # A responsibility in a neighboring clause cannot authorize intake.
    for clause in re.split(r"[，,。；;\n]", quote):
        if _named_responsibility(value, clause) and (
            PAPER_INTAKE.search(clause) or re.search(
                r"(?:负责|承担)\s*纸质登记(?:工作|事宜)?$|^\s*纸质登记(?:工作|事宜)?由[^，。；]+(?:负责|办理|承担)$",
                clause.strip(),
            )
        ) and not re.search(r"建议|拟由|待定|未定|未明确|待(?:会商|核实|明确|确定)", clause):
            return True
    return False


def _confirmed_source_sentences(excerpts):
    for excerpt in excerpts:
        for block in re.split(r"(?m)(?=^[ \t]*【(?:材料|附件|参考))", excerpt):
            heading = block.strip().split("\n", 1)[0]
            if heading.startswith("【") and re.search(r"参考|旧[稿版]|作废|草案|反馈|征求意见", heading):
                continue
            yield from (sentence.strip() for sentence in re.split(r"[。；\n]", block))


def _explicit_submissions(task, excerpts):
    # A dated list of named handovers establishes only those named deliverables.
    deliverable = task.deliverable.strip()
    if (task.status != "confirmed" or not deliverable or deliverable not in task.action
            or not re.search(r"提交|报送|移交|交付", task.action)):
        return {}
    claims = {}
    for sentence in _confirmed_source_sentences(excerpts):
        dated = re.fullmatch(r"(\d{1,2}月\d{1,2}日\s*(?:\d{1,2}:\d{2})?前)[，,](.+)", sentence)
        if not dated:
            continue
        for assignment in re.split(r"[，,]", dated[2]):
            match = re.fullmatch(r"(?:由)?([\u4e00-\u9fff]{2,30}?)(?:提交|报送|移交|交付|交)" +
                                 re.escape(deliverable), assignment.strip())
            if match and not re.search(r"建议|拟|不|未|应否|是否|或者|可能", match[1]):
                claims[(match[1], dated[1])] = sentence
    if len(claims) != 1:
        return {}
    (lead, timing), quote = next(iter(claims.items()))
    return {"lead": (lead, quote), "timing": (timing, quote)}


def _explicit_joint_lead(task, excerpts):
    # Recover a lost joint assignment only when the source names the same units and the entire action.
    units = re.fullmatch(r"(?:共同负责[：:]\s*)?([\u4e00-\u9fff]+(?:[、和与及][\u4e00-\u9fff]+)+)"
                         r"\s*(?:[（(]共同(?:负责)?[）)])?", task.lead.strip())
    if task.status != "confirmed" or not units or not task.action.strip():
        return "", ""
    names = re.split(r"[、和与及]", units[1])
    joint_subject = r"[、和与及]".join(map(re.escape, names)) + r"共同"
    action = re.sub(r"^(?:由)?" + joint_subject, "", task.action.strip(), count=1)
    if not action:
        return "", ""
    assignment = re.compile(r"(?:^|[，,])\s*(?:由)?" + joint_subject + re.escape(action) + r"(?=[，,]|$)")
    for sentence in _confirmed_source_sentences(excerpts):
        if (assignment.search(sentence)
                and not re.search(r"建议|拟|可能|是否|未明确|未确定|待(?:会商|核实|明确|确定)", sentence)):
            return "共同负责：" + "、".join(names), sentence
    return "", ""


def _replace_table_cells(original, cells, anchors, width, key):
    cells = [cell.strip() for cell in cells]
    require(len(cells) == width and all(cell and not any(sep in cell for sep in ("｜", "\n", "\r"))
                                        for cell in cells),
            f"表格行 {key} 未保留原列结构。", "REVIEW_TABLE_FORMAT")
    return original.model_copy(update={"text": "｜".join(cells), "anchors": anchors})


def _verify_conference_draft(model, candidate, usable, generate, report, *, fixed_checks=(), source_plan=None):
    """Read-only. Reuse the independent review for unchanged claims and verify each repaired finding."""
    frozen = candidate.model_dump()
    digest = _digest(frozen)
    audit = report["task_evidence"] = {"before_sha256": digest, "findings": []}
    check_draft(candidate, usable)
    check_placeholders(candidate)
    if source_plan:
        _verify_source_plan(candidate, source_plan)
    for fact in fixed_checks:
        # These obligations were fixed from the source before drafting. Check both projections after repair.
        owners = re.split(r"和|与|及|、", fact["主体"])
        def has_owners(text):
            text = text.replace("两家", "两个").replace("两处", "两个").replace("两站", "两个试点站")
            norm = lambda x: re.sub(r"(?:两个|两)(?:试点)?(?:服务)?站", "两站", x)
            return all(norm(name) in norm(text) for name in owners)
        def has_action(text):
            return fact["动作"] in text or (fact["动作"] == "抽核" and re.search(r"抽查|抽样核查", text))
        def covers_scope(text):
            normalized = re.sub(r"(切换|上线)(?:工作)?(?:完成|成功|运行)(?:之)?后", r"\1后", text)
            if fact["阶段"] not in normalized:
                return False
            if not (target := fact.get("对象")):
                return True
            def subject(value):
                value = number_basis.QUANTITY.sub(number_basis.quantity_token, value)
                return re.sub(r"\s+|记录", "", value)
            return subject(target) in subject(normalized)
        rows = [t for t in candidate.tasks if has_action(t.action) and t.status == "confirmed"
                and has_owners(t.lead) and covers_scope(t.timing + t.action + t.acceptance)]
        body = [p for sec in candidate.sections for p in sec.paragraphs if p.kind == "fact"
                and has_action(p.text) and has_owners(p.text) and covers_scope(sec.title + p.text)]
        require(rows and body, f"正文或分工仍缺少有明确主体的{fact['阶段']}{fact['动作']}。",
                "REVIEW_UNRESOLVED")
    require(not report.get("unaddressed_targets"), "有核稿指出的内容尚未更正。", "REVIEW_UNRESOLVED")
    findings = [f for f in report["review"]["findings"] if f["level"] == "blocking"]
    changes = {entry["target_id"]: entry["after"] for entry in report.get("changes", [])}
    if findings:
        tickets = {f"F{i}": finding for i, finding in enumerate(findings, 1)}
        claims = {}
        for key, finding in tickets.items():
            fragments = {target: changes[target] for target in finding["target_ids"] if target in changes}
            retained = {}
            for target in finding["target_ids"]:
                if original := report.get("retained_source_quotes", {}).get(target):
                    require(source_plan and any(s.title == TRIAL_SECTION and
                            any(p.model_dump() == original and p.kind == "proposal" and len(p.anchors) == 1
                                and _quoted_opinions(source_plan).get(p.anchors[0]) == p.text for p in s.paragraphs)
                            for s in candidate.sections), "待核引文与本轮试点原文不一致。", "REVIEW_UNRESOLVED")
                    retained[target] = [original]
            fragments.update(retained)
            retained_rows = {}
            for target in finding["target_ids"]:
                if original := report.get("retained_source_rows", {}).get(target):
                    require(source_plan and any(TaskItem(**r).model_dump() == original for r in source_plan["actions"])
                            and any(t.model_dump() == original for t in candidate.tasks),
                            "冻结分工与本次纪要或待交付行不符。", "REVIEW_UNRESOLVED")
                    retained_rows[target] = [original]
            fragments.update(retained_rows)
            for target in finding["target_ids"]:
                if original := report.get("retained_source_facts", {}).get(target):
                    require(source_plan and original["text"] == " ".join(source_plan["decisions"][a] for a in original["anchors"])
                            and any(p.model_dump() == original for s in candidate.sections for p in s.paragraphs),
                            "冻结的正文决定与纪要原句不符。", "REVIEW_UNRESOLVED")
                    fragments[target] = [original]
            require(set(fragments) == set(finding["target_ids"]), "存在未落实的核稿意见。", "REVIEW_UNRESOLVED")
            claims[key] = {**finding, "修改后": fragments}
            if retained:
                claims[key]["保留原引的说明"] = "这些段落仅在试点意见节逐字引用试点反馈，并非会议决定。核原引与纪要是否确有矛盾。"
                claims[key]["保持逐字引文的位置"] = list(retained)
            if retained_rows:
                claims[key]["保留纪要分工的位置"] = list(retained_rows)
                claims[key]["分工依据"] = "逐格对照本项材料与原问题，区分按时收件与内容验收。纪要确有遗漏或错配时必须判未解决。"
        source_ids = {a for finding in findings for a in finding["basis_ids"]}
        source_ids.update(a for claim in claims.values() for rows in claim["修改后"].values() for row in rows for a in row["anchors"])
        source = {key: usable[key] for key in sorted(source_ids) if key in usable}
        context = {"basis_key": digest, "待核问题": claims, "可用依据": source}
        # Limit citations to source text. A paraphrase from the repaired draft cannot be selected.
        quotes = set(source.values())
        for line in source.values():
            quotes.update(part.strip() for part in re.split(r"[，,。；;\n]", line) if part.strip())
        verdict = create_model("SourceFindingVerdict", __base__=FindingVerdict,
                               quote=(Literal[tuple(sorted(quotes))], ...))
        proof_type = create_model("VerifiedConferenceDraft", __base__=VerifiedConferenceDraft,
                                  basis_key=(Literal[digest], ...), findings=(list[verdict], ...))
        proof, metadata = generate(model, "只读核验。对照各项原缺陷、修改后的片段和来源，分别判定是否实际解决、"
                                   "是否破坏相邻已确认事实或新增无依据要求。仅改字或改了位置不足以算解决。"
                                   "标为保持逐字引文的位置，分别核试点原话和会议决定所属的主体、动作及其效力；"
                                   "有依据的意见与已定工作可以并存，发现真正矛盾或来源不符仍须标 false。"
                                   "每项仅给结论，quote 从 Schema 提供的原句中选取，必须出自同项所选 basis_id。无法确认就标 false。"
                                   "不得生成正文、分工、改写指令或调用工具。\n本轮数据：\n" +
                                   json.dumps(context, ensure_ascii=False), proof_type)
        report["stages"]["task_evidence"] = metadata
        audit["findings"] = [row.model_dump() for row in proof.findings]
        require(proof.basis_key == digest and len(proof.findings) == len(tickets)
                and {row.finding_id for row in proof.findings} == tickets.keys(),
                "核验结论没有覆盖本轮逐条问题。", "REVIEW_BINDING_INVALID")
        for row in proof.findings:
            allowed_basis = set(tickets[row.finding_id]["basis_ids"])
            allowed_basis.update(a for parts in claims[row.finding_id]["修改后"].values()
                                 for part in parts for a in part["anchors"])
            require(row.resolved and row.basis_id in allowed_basis and row.quote.strip()
                    and row.quote in source.get(row.basis_id, ""),
                    "有核稿指出的内容仍需确认。", "REVIEW_UNRESOLVED")
    else:
        audit["reused_checked_ids"] = report["review"]["checked_ids"]
    audit["after_sha256"] = _digest(candidate.model_dump())
    require(digest == audit["after_sha256"], "最终核验前后稿件不一致。", "REVIEW_BINDING_INVALID")
    audit["status"] = "verified"
    return candidate


def check_placeholders(candidate):
    placeholders = re.compile(r"(?:^|[\s｜（(：:])(?:string|lorem ipsum|TODO|TBD|XXX+)(?:$|[\s｜）)，。])", re.I)
    fields = [candidate.title] + [section.title for section in candidate.sections]
    fields += [p.text for section in candidate.sections for p in section.paragraphs]
    fields += [value for task in candidate.tasks for value in task.model_dump().values() if isinstance(value, str)]
    require(not any(placeholders.search(text.strip()) for text in fields),
            "稿件仍含未填写的占位文字。", "DRAFT_INCOMPLETE")


def _explicit_draft_genre(request):
    # Look for a named deliverable in the instruction, before any pasted source headings.
    instruction = re.split(r"(?m)^\s*(?:【(?:材料|附件|参考)|(?:材料|附件)[一二三四五六七八九十\d]+[：:])",
                           request, maxsplit=1)[0]
    profiles = set()
    for clause in re.split(r"[，,。；;！？\n]", instruction):
        directive = (DRAFT_DIRECTIVE.search(clause) or
                     re.match(r"\s*(?:请帮我|帮我|请|给[^，,。；\n]{1,22})写", clause))
        if directive is None:
            continue
        if re.search(r"(?<!分)别|不(?:要|用|必|想|确定)|无需|怎么|如何|是否|还是|哪种", clause):
            return None
        target = clause[directive.end():].strip()
        matches = list(DRAFT_GENRE.finditer(target))
        if not matches:
            continue
        tail = target[matches[-1].end():]
        if matches[-1].group() == "通知":
            tail = tail.removeprefix("书")
        if re.fullmatch(r"(?:[（(][^）)]*[）)])?[）)》”]*(?:初稿|草稿|审阅稿)?", tail):
            profiles.update(DRAFT_GENRES[match.group()] for match in matches)
    if not profiles and (re.search(r"(?:帮我|给[^，。；\n]{1,20})(?:写|发个)[^，。；\n]{1,25}(?:提醒|温馨提示)(?=[，。；\n]|$)", instruction)
                         or ("通知" in instruction and re.search(r"(?:星期|周[一二三四五六日天]).{0,15}改(?:对|正)", instruction)
                             and re.search(r"(?:只|先)(?:发|给)(?:我)?正文", instruction))):
        return "notice-draft"
    return next(iter(profiles)) if len(profiles) == 1 else None


def standalone_draft_question(instruction):
    short_request = instruction.strip().rstrip("。！!？?吧").rstrip()
    if BARE_TRAFFIC_ANNOUNCEMENT.fullmatch(short_request):
        return "请发我已确认的管制日期和时段、涉及路段及具体限制（如封闭或限行）。"
    if re.fullmatch(rf"{SHORT_DRAFT_START}(?:一下\s*)?(?:关于)?[\w· ]{{0,30}}"
                    r"(?:居家|远程|在家)办公(?:的)?(?:通知书?|通告|公告)", short_request):
        return ("这份通知发给谁、由哪个单位发？请告诉我居家办公的适用范围和安排在哪几天。日期还没定的话，可以先给相对安排。"
                "确认后我会给你一份可编辑的 Word 审阅稿。")
    if bare := BARE_DRAFT.fullmatch(short_request):
        if bare["genre"] in {"通知", "公告"}:
            return "这份通知要告诉大家什么事、通知谁？有已定的安排可以直接发我。"
        return "这份材料希望解决什么问题、给谁看？有现成的事实或原材料也可以一起发我。"
    return None


def ambiguous_remote_work_dates(instruction):
    return bool(RELATIVE_WORK_DATES.search(instruction) and not (
        SEPARATE_WORK_DATES.search(instruction) or CONTINUOUS_WORK_DATES.search(instruction)
        or DAY_RANGE_REPLY.search(instruction)
    ))


def _obvious_intake_question(instruction, *, has_event_window=False):
    if _explicit_draft_genre(instruction) == "notice-draft":
        for match in DATE_WITH_WEEKDAY.finditer(number_basis.normalized_text(instruction)):
            year, month, day = (int(match[key]) for key in ("year", "month", "day"))
            given_date = f"{year}年{month}月{day}日"
            try:
                actual_weekday = "一二三四五六日"[date(year, month, day).weekday()]
            except ValueError:
                return f"日历上没有{given_date}。请确认一下正确的日期，我再继续写。"
            given_weekday = match["weekday"].replace("天", "日")
            if actual_weekday != given_weekday:
                return f"{given_date}是周{actual_weekday}。请确认按这一天安排，还是按你说的周{given_weekday}？"
    if REMOTE_WORK_NOTICE.search(instruction) and DAY_RANGE_REPLY.search(instruction):
        return "这段会期是哪年哪月？请给出完整会期，或告诉我先保留会前、会后的相对日期。"
    if REMOTE_WORK_NOTICE.search(instruction) and ambiguous_remote_work_dates(instruction):
        question = "居家办公是会前、会后各一天，还是从会前一天连续安排到会后一天？"
        if not (has_event_window or CALENDAR_DATE.search(instruction) or PROVISIONAL_NOTICE.search(instruction)
                or KEEP_RELATIVE_DATES.search(instruction)):
            question += "如果会期已经确定，也请告知会期。日期还没定的话，可以先按相对日期出审阅稿。"
        return question
    if (REMOTE_WORK_NOTICE.search(instruction) and not (PROVISIONAL_NOTICE.search(instruction)
                                                       or KEEP_RELATIVE_DATES.search(instruction))):
        if (RELATIVE_WORK_DATES.search(instruction) and not has_event_window and not CALENDAR_DATE.search(instruction)
                and not DAY_RANGE_REPLY.search(instruction)):
            return "请告诉我是哪一届会议，或者发一下已确认的会期。我就能按你给的会前、会后安排继续出稿。"
    return standalone_draft_question(instruction)


def run_task(model, request, texts=(), files=(), *, generate=generate_intake, style_library=None,
             review_conference=None, review_generate=generate_review, expert_team=False,
             quick_notice_generate=None, quick_control_review_generate=None, public_schedule=None, checkpoint=None):
    # Explicit historical evaluation callers still select their original comparison arm.
    one_pass = review_conference is None and not expert_team
    materials = collect(request, texts, files, split_inline=True)
    instruction = materials[0]["blocks"]["U.1"]
    relative_dates = RELATIVE_WORK_DATES.search(instruction)
    keep_relative = relative_dates and KEEP_RELATIVE_DATES.search(instruction)
    user_window = event_window(instruction, require_event_context=True) if relative_dates and not keep_relative else None
    schedule = _schedule_material(public_schedule) if relative_dates and not (user_window or keep_relative) else None
    if schedule:
        materials.append(schedule)
    # Intercept only clear intake gaps before starting any model.
    if len(materials) == (2 if schedule else 1) and style_library is None and (
        question := _obvious_intake_question(instruction, has_event_window=bool(user_window or schedule))
    ):
        candidate = Candidate(profile="unclear", title="", sections=[], tasks=[], review_notes=[],
                              questions=[question], material_uses=[
                                  MaterialUse(source_id="U", role="requirements", usable_anchors=[]),
                              ])
        return Run("needs_input", candidate, materials, {}, {}, "请补充以下信息。", original_request=request)
    split_inline = instruction != request
    collection_case = style_library and submission.matches(request, materials)
    genre = "experience-draft" if collection_case else _explicit_draft_genre(request)
    specialists = (DRAFT_SKILLS[genre],) if genre else dict.fromkeys(DRAFT_SKILLS.values())
    paths = ["prompts/自然收件.md", EXPERT]
    paths.extend(reference for skill, reference in DRAFT_REFERENCES.items() if skill in specialists)
    paths.append("skills/gongwen-expert/references/交件自检.md")
    paths.extend(f"skills/{skill}/SKILL.md" for skill in specialists)
    rules = [(ROOT / path).read_text(encoding="utf-8") for path in paths]
    if window := user_window or (event_window(schedule["blocks"]["P.1"]) if schedule else None):
        before, after = (f"{y}年{m}月{d}日" if y else f"{m}月{d}日" for y, m, d in window[2:])
        basis = "U.1、P.1" if schedule else "U.1"
        rules.append(f"已按可用会期计算：会前一天为{before}，会后一天为{after}。用 {basis} 作为日期依据。"
                     "用户说两日时按两个独立日期；明确要求连续时才用日期区间。原文已说清的内容不重复追问。"
                     "公开会期仅证明活动日期，不能据此认定办公措施已经获批。")
    elif relative_dates and (keep_relative or PROVISIONAL_NOTICE.search(instruction)):
        rules.append("直接起草审阅稿。正文写清会前一天和会后一天的办公安排，保留相对日期，不换算、不追问月份。"
                     "两天分别安排时不要写成会议期间连休。落款日期留空时无需追问。不得补写审批、人员范围或其他规定。")
    if relative_dates and SEPARATE_WORK_DATES.search(instruction) and not CONTINUOUS_WORK_DATES.search(instruction):
        rules.append("办公安排只有会前和会后各一天。标题用‘会议相关居家办公安排’，正文不可写成会议期间全程居家办公。")
    if genre == "notice-draft":
        rules.append("依据仅有文号时原样保留文号，文件全称必须有材料原文。发文单位是本稿落款；"
                     "未提供审批或已发布通知的依据时，不写‘经研究决定’或‘根据某政府通知安排’。")
    if genre == "notice-draft" and EXPLICIT_RETROSPECTIVE_DRAFT.search(instruction):
        rules.append("这是按用户指定的会期和办公安排起草的复盘审阅稿。用拟安排的口吻，不增写审批或决策经过。")
    case = None
    if collection_case:
        reference, upstream = submission.load_style(style_library)
        rules.extend([(ROOT / "prompts/江苏基层案例.md").read_text(encoding="utf-8"), reference])
        case = {"profile": submission.PROFILE, "upstream": upstream}
    pending = []
    if (explicit := PENDING_LIST.search(instruction) or PENDING_AT_MEETING.search(instruction)) and "、" in explicit["labels"]:
        *first, last = explicit["labels"].split("、")
        pending = list(dict.fromkeys(label for label in [*first, *re.split(r"以及|和|及", last)] if label))
        if len(pending) > 1:
            rules.append("如果起草会商稿，以下是用户已明确要求单列的待核事项。每项各占一个正文段落，"
                         "以该事项原名和冒号开头，写清要会上确认什么。同一段只写一项。"
                         "若输出会商议题表，按下列原名和顺序逐项列出。材料要求多个单位分别议定同一事项时，"
                         "该事项用连续分项行替代总行，每行标题为「事项原名（单位原名）」且各自留空结果。事项："
                         + "、".join(pending) + "。")
        else:
            pending = []
    meeting_request = bool(MEETING.search(request))
    ledger = _conference_sources(materials) if genre == "plan-draft" and meeting_request else {}
    source_plan = _source_plan(ledger, {m["id"]: m["blocks"] for m in materials}, instruction if one_pass else "")
    compact_minutes = bool(one_pass and source_plan and source_plan["phases"] and not style_library)
    policy_plan = (policy_actions.source_plan(instruction, materials)
                   if one_pass and genre == "plan-draft" and not style_library and not source_plan else None)
    if ledger and not compact_minutes:
        rules.append("正文与分工表共用下列已冻结的依据索引，引用 blocks 中的原文。逐句落实纪要的"
                     "主体、动作、时点、条件及逻辑关系。相同动作在正文和表内的责任与时限应一致；"
                     "一个主体两个动作均须落到对应阶段，一个动作几个明确主体均在责任格列明。"
                     "先暂停、谁回退、谁通知逐项辨明。无指定主体不可自行派工。"
                     "无明文的成果、验收、人选留空。保持各试点观察、建议、异议及决定的边界。"
                     "只作结构参考的材料不得成为事实。逐项依据索引：" + json.dumps(ledger, ensure_ascii=False))
    material_text = json.dumps(
        [{k: v for k, v in m.items() if k != "sha256"} for m in materials], ensure_ascii=False
    )
    workflow = {"mode": "one_pass" if one_pass else "team" if expert_team else "single",
                "draft_rule_profile": genre}
    if ledger and genre == "plan-draft" and not compact_minutes:
        # With an explicit genre/ledger, unrelated intake and generic delivery references duplicate the plan specialist.
        rules[:len(paths)] = [rules[0].split("\n\n", 3)[0] + "\n" +
                              "\n".join(line for line in rules[0].splitlines() if "会前说明不能写成会议纪要" in line),
                              rules[len(paths) - 1]]
        rules.append("按材料中的实际动词分配责任。触发暂停并由某方回退，不代表该方决定暂停；将两项分行，"
                     "同时写触发条件和原文授权的通知。相邻时限不得移给另一任务。试点意见按建议单列。"
                     "未定责任、联系人和交付物用空字符串。来源未定的时间不填。")
    if source_plan:
        workflow["source_action_rows"] = source_plan["actions"]
        if source_plan["phases"]:
            workflow["source_execution_phases"] = list(source_plan["phases"])
    if ledger:
        workflow["frozen_sources"] = {"index": ledger, "blocks": {m["id"]: m["blocks"] for m in materials}}
    if (genre == "notice-draft" and len(materials) == (2 if schedule else 1) and style_library is None
            and len(request) <= 1600 and not QUICK_NOTICE_EXCEPTIONS.search(instruction)):
        # Keep the specialist asset and this notice's contextual rules. Cross-genre
        # requirements (case studies, multi-session forms, task tables) do not apply here.
        rules[:len(paths)] = [rules[len(paths) - 1]]
        if number_basis.conflicting_rates(instruction):
            rules.append("同一批材料对每组等执行数量有不同记载。请将共同确认的日期、地点、对象先写成审阅稿，"
                         "只在待确认事项中询问采用哪一份依据；不得选定一个数作为已批准的安排。")
        rules.append("返回 notice-draft。material_uses 按输入编号列出用途；用户原话引用 U.1。"
                     "sections 必须含正文，每段有 text、kind=fact、anchors。单场事项不用分工表或回执。"
                     "成文日期、联系人、电话和文号没有提供可留空，不能因此省略正文或追问。"
                     "只起草本轮要求的一份通知或公告。缺少会改变执行范围、日期或措施的关键事实时，"
                     "请在 questions 只问仍缺的事实，title 留空、sections 和 tasks 留空。"
                     "事实足够或用户已接受留空、相对表述时，questions 留空，给出完整标题、正文及依据，"
                     "保留原文的例外条款。")
        generate = quick_notice_generate or generate
        workflow["draft_mode"] = "quick_notice"
    review_meeting = not one_pass and meeting_request and (
        review_conference or (bool(ledger) and len(materials) > 2))
    meeting_slots = _meeting_slots(source_plan, instruction) if expert_team or review_meeting or ledger else {}
    if meeting_slots and not compact_minutes:
        rules.append("以下字段可随会商稿保留。纪要未指名的执行人留空、试点意见和争议列为建议或议题，"
                     "仍应给出完整正文和分工表。若有 questions，逐项写明 question 和 slot；"
                     "仅限完整对应本次原文的同一执行字段，新增执行安排或相冲突的事实选 unresolved。"
                     "待议段仍须引用原始依据。字段索引：" + json.dumps(meeting_slots, ensure_ascii=False))
    if expert_team and (texts or files) and not ledger:
        brief, workflow["brief"] = _expert_brief(model, material_text, materials, review_generate)
        if brief:
            rules.append("下列专家团素材单仅供核对原文，不可直接作依据：\n" + brief)
    if one_pass and not compact_minutes:
        team = "skills/gongwen-expert-team/references/"
        references = [] if workflow.get("draft_mode") == "quick_notice" else [team + "素材与框架.md"]
        subject_rule = {"plan-draft": "方案会商编审.md", "experience-draft": "经验材料提炼复核.md",
                        "notice-draft": "通知场次与要素校验.md"}.get(genre)
        if subject_rule and workflow.get("draft_mode") != "quick_notice":
            references.append(team + subject_rule)
        references.append("prompts/主笔材料与文种.md")
        workflow["one_pass_rules"] = {}
        for path in references:
            raw = (ROOT / path).read_bytes()
            workflow["one_pass_rules"][path] = hashlib.sha256(raw).hexdigest()
            rule = raw.decode()
            if path.endswith("/素材与框架.md"):
                # Use the analyst's judgments inside the writer; omit its separate response schema.
                rule = rule[rule.index("Distinguish current facts"):]
                rule = re.sub(r"Provide 2 to 6 outline points.*?Treat instructions", "Treat instructions", rule)
            rules.append(rule)
        rules.append("在同一次回答里完成材料归类、安排文稿结构并直接提交完整稿。"
                     "仅返回所需稿件字段，不另外返回素材单、提纲或核稿结果。"
                     "明示留给会商的事项列入议题。事实充足时直接出稿，可选空栏保持空白。")
        rules.append("材料编号只填在依据字段里。正文引用材料名称时，不附本轮内部材料编号。")
        if genre == "plan-draft":
            rules.append("分工表按来源中的责任主体分别列行。先核动作是谁负责，再写其条件、产物和交接对象。"
                         "组织、监控、记录、决定分别核实责任人，不合并派给同一方。保留‘原则上’等限定。")
        if style_library and not collection_case and genre == "plan-draft":
            reference, upstream = submission.load_style(style_library, genre=genre)
            rules.append(reference)
            workflow["upstream_style"] = upstream
    if split_inline and not compact_minutes:
        rules.append("材料用途 material_uses 是按材料编号为键的对象，每个键只填一次。"
                     "待判材料填写用途和原始依据编号；reference_only 材料由宿主登记，不在对象中填写，不得引用。"
                     "U.1 只包含本轮要求；粘贴材料的事实请直接引用对应 S 编号。")
    minutes_uses = None
    if compact_minutes:
        workflow["writer_profile"] = "operational_minutes_compact"
        source_roles = {"用户要求": "requirements", "会议纪要": "facts", "试点材料": "mixed",
                        "只参考结构，禁作事实依据": "reference"}
        minutes_uses = []
        writer_materials = []
        for material in materials:
            purpose = ledger[material["id"]]["用途"]
            role = source_roles[purpose]
            blocks = material["blocks"]
            if role == "mixed":
                blocks = {a: blocks[a] for a in blocks if a in source_plan["feedback"]}
            minutes_uses.append(MaterialUse(source_id=material["id"], role=role,
                                           usable_anchors=[] if role == "reference" else list(blocks)))
            writer_materials.append({"用途": purpose, "片段": blocks})
        material_text = json.dumps(writer_materials, ensure_ascii=False, separators=(",", ":"))
        rules = [
            "起草本轮会商稿。先读 U.1，再用下面的原文核对。purpose 只用 U.1 写一句会商目的，"
            "不超过 45 字。直接写请参会方核对或议定什么，不重复标题、章节目录、交稿要求或模拟数据说明。"
            "sections 只写程序尚未覆盖的用户明确要求，无则填空数组；导语只填 purpose。"
            "试点观察、建议和已明确留待会商的事项由程序按原文入稿，"
            "无须向用户确认，也不新增启用条件。"
            "程序会根据纪要写出执行段落、分工表以及已识别的留空责任，并根据试点反馈写出建议和待议事项。"
            "不用复写这些段落、表格或纪要数字。旧材料只供参考章节结构，其内容不得作为事实或依据。"
            "sections 每段必须标注 kind 和本轮原始 anchors，待议事项用 proposal，依据编号不写进正文。"
            "如有程序尚未覆盖且有原文依据的待议事项，以「会商议题表」为节，每段按"
            "「议题｜待定内容｜议定要点」书写。review_notes 仅留需要说明的依据限制。"
        ]
        if meeting_slots:
            rules.append("可留到会上确认的事项随稿处理。纪要足以出稿时，questions 填空数组。"
                         "只在缺失或冲突导致无法起草时补问，"
                         "每项含 question 与 slot；没有对应来源字段填 unresolved。可用字段编号和含义："
                         + json.dumps(meeting_slots, ensure_ascii=False, separators=(",", ":")))
        if generate is generate_intake:
            generate = partial(generate_intake, reasoning_effort="low")
    elif policy_plan:
        workflow["writer_profile"] = "numbered_policy_discussion"
        policy_rules = (ROOT / "prompts/条款会商主笔.md").read_text(encoding="utf-8")
        rules = [policy_rules]
        if generate is generate_intake:
            generate = generate_policy_discussion
    elif one_pass and source_plan:
        workflow["writer_profile"] = "operational_minutes"
        rules.append("纪要分工表由程序从原文生成并核对，tasks 返回空数组。"
                     "正文仍须写全用户要求的专门章节、关键执行顺序、试点意见及会商议题，保留原始依据。")
    material_label = "本轮输入（已判定用途）" if compact_minutes else "本轮输入（U 为用户直接输入，其余为待判材料）"
    prompt = "\n\n".join(rules) + f"\n\n{material_label}：\n" + material_text
    if one_pass:
        workflow["writer_prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
        workflow["writer_prompt_bytes"] = len(prompt.encode())
    try:
        candidate, metadata = _generate_draft(model, prompt, materials, generate, split_inline=split_inline,
                                              meeting_slots=meeting_slots, policy_plan=policy_plan,
                                              minutes_uses=minutes_uses)
    except RevisionIssue as error:
        if failure := getattr(error, "model_failure", None):
            failure["stage"] = "draft"
            if brief_record := workflow.get("brief"):
                brief_call = brief_record.get("model", brief_record.get("error", {}).get("model_failure", {}))
                failure["brief"] = {"status": brief_record["status"],
                                    "elapsed_seconds": brief_record["elapsed_seconds"],
                                    "upstream_call_id": brief_call.get("upstream_call_id")}
        raise

    def freeze_first_draft(snapshot=None):
        workflow["first_draft"] = candidate.model_dump() if snapshot is None else snapshot
        workflow["first_draft_sha256"] = _digest(workflow["first_draft"])
        if checkpoint:
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            with checkpoint.open("x", encoding="utf-8") as saved:
                json.dump(workflow, saved, ensure_ascii=False)

    project_before_freeze = candidate.profile == "plan-draft" and (one_pass or bool(source_plan))
    if project_before_freeze:
        workflow["model_draft"] = candidate.model_dump()
        workflow["model_draft_sha256"] = _digest(workflow["model_draft"])
    else:
        freeze_first_draft()

    def prepare(candidate):
        if policy_plan:
            require(candidate.profile == "plan-draft", "条款会商稿的文种不符。", "DRAFT_INCOMPLETE")
        if candidate.profile == "notice-draft":
            # Models sometimes emit a separate empty section for an optional field.
            # Keep empty business sections under the usual missing-body check.
            optional = {"联系人", "联系电话", "联系方式", "发文字号", "成文日期"}
            if any(section and section.title not in {NOTICE_RECEIPT, NOTICE_RECEIPT + "说明"}
                   and any(p.text.strip() and not p.text.strip().endswith(("：", ":"))
                           for p in section.paragraphs)
                   for section, _ in _notice_sections(candidate.sections)):
                candidate.sections = [
                    section for section in candidate.sections
                    if section.title.strip().rstrip("：:").strip() not in optional
                    or any(p.text.strip() for p in section.paragraphs)
                ]
            # Empty model placeholders carry no business content.
            tasks = [task for task in candidate.tasks if task.anchors or any(
                str(value).strip() for key, value in task.model_dump().items()
                if key not in {"status", "anchors"}
            )]
            if len(tasks) != len(candidate.tasks):
                metadata["discarded_empty_notice_tasks"] = len(candidate.tasks) - len(tasks)
                candidate.tasks = tasks
            _normalize_notice_addressee(candidate)
            _complete_notice_blank_date(candidate, instruction)
            if relative_dates and SEPARATE_WORK_DATES.search(instruction) and not CONTINUOUS_WORK_DATES.search(instruction):
                title = candidate.title
                if REMOTE_WORK.search(title) and not RELATIVE_WORK_DATES.search(title):
                    candidate.title = WHOLE_EVENT.sub(
                        lambda match: match[0].replace("期间", "相关"), title
                    ).replace("相关相关", "相关")
                    if candidate.sections and candidate.sections[0].title == title:
                        candidate.sections[0].title = candidate.title
        if ledger:
            # Classified source sentences stay available to the editor even if the writer omits them.
            for use in candidate.material_uses:
                record = ledger.get(use.source_id, {})
                if record.get("用途") in {"会议纪要", "试点材料"}:
                    use.role = "facts" if record["用途"] == "会议纪要" else "mixed"
                    use.usable_anchors = [key for key, purpose in record["片段"].items() if purpose != "失效内容"]
        usable = validate(candidate, materials)
        if candidate.profile.endswith("-revise"):
            return usable
        if candidate.profile == "plan-draft":
            if one_pass:
                # Internal material labels in a source attribution are not document content.
                # Keep real identifiers present in the source and then run the usual numeric check.
                materials_ids = {m["id"] for m in materials if not m.get("reference_only")}
                label = re.compile(r"(纪要|反馈|材料|通知|报告)[（(](S[1-9]\d*)[）)]")
                attribution = re.compile(r"^(?:本(?:方案|稿件|稿|文))?(?:依据|根据|参照)[^，。；\n]+")

                def remove_label(match):
                    name, source_id = match.groups()
                    marker = rf"(?<![A-Za-z0-9]){re.escape(source_id)}(?![A-Za-z0-9])"
                    if source_id in materials_ids and not any(re.search(marker, value) for value in usable.values()):
                        return name
                    return match[0]

                for section in candidate.sections:
                    for paragraph in section.paragraphs:
                        text = attribution.sub(lambda match: label.sub(remove_label, match[0]), paragraph.text)
                        if text == paragraph.text:
                            continue
                        paragraph.text = text
                        if source_plan and paragraph.kind == "fact" and "U.1" in paragraph.anchors:
                            decisions = [a for a in paragraph.anchors if a in source_plan["decisions"]]
                            known = {token for a in paragraph.anchors for token in numeric_tokens(usable[a])}
                            if decisions and set(paragraph.anchors) == {"U.1", *decisions} and numeric_tokens(text) <= known:
                                # Project a mixed source preamble and decision paragraph from the minutes.
                                # Leave unsupported quantities in place for the normal draft check.
                                paragraph.anchors = decisions
            _restore_proposal_anchors(candidate, ledger, usable)
            _ground_contingencies(candidate, ledger, usable)
            _ground_confirmed_checks(candidate, [f for source in ledger.values() for f in source.get("已确认核查分工", [])])
            _complete_plan_fields(candidate, usable, pending, instruction)
            if source_plan:
                bindings = workflow.get("bound_meeting_questions", []) if one_pass else metadata.get("intake_bindings", [])
                owner_anchors = {q["slot"].split("/")[0] for q in bindings
                                 if q["slot"].endswith("/lead")}
                if compact_minutes:
                    # The writer no longer needs to ask for optional owners to put them on the agenda.
                    owner_anchors.update(slot.split("/")[0] for slot in meeting_slots if slot.endswith("/lead"))
                _apply_source_plan(candidate, source_plan, owner_anchors)
            if policy_plan:
                _apply_policy_plan(candidate, policy_plan)
            _complete_plan_issuer(candidate, usable, instruction)
            if (one_pass or expert_team or review_meeting) and (meeting_request or MEETING.search(candidate.title)):
                if withheld := _withhold_unsupported_proposed_timings(candidate, usable):
                    workflow["proposed_timing_withheld"] = withheld
        if candidate.profile == "notice-draft":
            _complete_notice_issuer(candidate, instruction, usable)
            _complete_notice_blank_date(candidate, instruction)
            if relative_dates:
                _complete_relative_notice(candidate, usable, instruction)
        _complete_notice_receipt(candidate, usable, instruction)
        if candidate.profile == "notice-draft":
            for section in candidate.sections:
                for paragraph in section.paragraphs:
                    def known_document(match):
                        sources = [usable[a] for a in paragraph.anchors]
                        if (any(match["number"] in source for source in sources)
                                and not any(match["title"] in source and match["number"] in source for source in sources)):
                            return match["number"]
                        return match[0]

                    paragraph.text = DOCUMENT_CITATION.sub(known_document, paragraph.text)
                    if relative_dates and (match := NOTICE_ISSUED_BASIS.match(paragraph.text)):
                        tail = paragraph.text[match.end():]
                        if (tail.startswith(("，", ",")) and tail[1:].strip()
                                and not any(match[0] in usable[a] for a in paragraph.anchors)):
                            paragraph.text = tail[1:].lstrip()
        if candidate.profile == "notice-draft" and not relative_dates:
            _prepare_notice_numbers(candidate, usable, instruction, workflow)
        check_started = monotonic()
        try:
            if metadata.get("structured_meeting_purpose"):
                _verify_meeting_purpose(candidate)
            check_draft(candidate, usable)
            if one_pass:
                check_placeholders(candidate)
            if one_pass and source_plan and candidate.profile == "plan-draft":
                _verify_source_plan(candidate, source_plan)
            if policy_plan:
                _verify_policy_plan(candidate, policy_plan, instruction)
        except RevisionIssue as error:
            # A defective, sourced draft is the input to independent review. It cannot be delivered
            # until the target is repaired and the same full draft check passes in the read-only verifier.
            position = re.search(r"第 (\d+) 节第 (\d+) 段|任务 (\d+)", str(error))
            if not (review_meeting and len(materials) > 2 and ledger and candidate.profile == "plan-draft"
                    and error.code in {"BASIS_INVALID", "DRAFT_INCOMPLETE"} and position):
                raise
            target = f"T{position[3]}" if position[3] else f"P{position[1]}.{position[2]}"
            workflow["draft_issue"] = {"target": target, "message": str(error)}
        finally:
            if one_pass:
                workflow["final_check_seconds"] = round(monotonic() - check_started, 4)
        if case:
            require(candidate.profile == "experience-draft", "征集案例须按经验材料成稿。")
            case["counts"] = submission.check_sections(case_sections(candidate))
        return usable

    candidate.questions = list(dict.fromkeys(q.strip() for q in candidate.questions if q.strip()))
    if (workflow.get("draft_mode") == "quick_notice" and candidate.profile == "notice-draft"
            and len(materials) == 1 and materials[0]["id"] == "U"):
        _prepare_direct_notice(candidate, instruction, workflow)
    if ledger and candidate.profile == "plan-draft":
        for s in candidate.sections:
            title = HEADING_LIST_MARKER.sub("", s.title.strip(), count=1)
            if title in PLAN_TABLE_COLUMNS:
                s.title = title
    intake = None if one_pass else _intake_for_review(candidate, meeting_slots, metadata)
    if intake:
        workflow["intake_for_review"] = intake
    elif one_pass and meeting_slots and "intake_bindings" in metadata:
        deferred = _bound_meeting_questions(candidate, source_plan, meeting_slots, metadata["intake_bindings"])
        if deferred:
            workflow["deferred_to_meeting"] = [item["question"] for item in deferred]
            workflow["bound_meeting_questions"] = deferred
    elif ledger and (one_pass or "intake_bindings" not in metadata) and (
        deferred := _defer_agenda_questions(candidate, instruction, ledger, materials)
    ):
        workflow["deferred_to_meeting"] = deferred
    if candidate.questions or candidate.profile == "unclear":
        if project_before_freeze:
            # No review copy exists until intake finishes. Preserve the complete model draft
            # including the questions since deferred to the meeting.
            freeze_first_draft(workflow["model_draft"])
        return Run("needs_input", candidate, materials, metadata, {},
                   "请补充下列信息后再写稿。" if candidate.questions else "这次希望起草什么，或修改哪份稿件？",
                   expert_workflow=workflow, original_request=request)
    try:
        usable = prepare(candidate)
    except RevisionIssue as error:
        if project_before_freeze:
            freeze_first_draft()
        if error.code == "NOTICE_QUANTITY_NEEDED":
            candidate.questions = [str(error)]
            return Run("needs_input", candidate, materials, metadata, {}, "请确认剩余的数量后继续出稿。",
                       expert_workflow=workflow, original_request=request)
        return Run("needs_review", candidate, materials, metadata, {}, str(error), expert_workflow=workflow,
                   original_request=request, draft_check_error=error.code)
    if project_before_freeze:
        if one_pass and candidate.profile == "plan-draft":
            workflow["review_basis_sha256"] = _digest([request, model, materials, candidate.model_dump()])
            workflow["review_materials"] = materials
            workflow["writer_model"] = model
        freeze_first_draft()
    if candidate.profile.endswith("-revise"):
        return Run("revision_identified", candidate, materials, metadata, {},
                   "已识别到改稿。请提供可编辑原件和明确的修改范围。", expert_workflow=workflow,
                   original_request=request)
    if ledger and _digest(candidate.model_dump()) != workflow["first_draft_sha256"]:
        workflow["prepared_draft"] = candidate.model_dump()
        workflow["prepared_draft_sha256"] = _digest(workflow["prepared_draft"])
    review, note = None, ""
    workflow["draft_skill"] = DRAFT_SKILLS[candidate.profile]
    control_notice = candidate.profile == "notice-draft" and PUBLIC_CONTROL_NOTICE.search(instruction)
    quick_notice = workflow.get("draft_mode") == "quick_notice"
    quick_draft_only = quick_notice and not control_notice
    if expert_team and not quick_draft_only:
        reference_ids = {item.source_id for item in candidate.material_uses if item.role == "reference"}
        references = {key: value for item in materials if item["id"] in reference_ids
                      for key, value in item["blocks"].items()}
        reviewer = (quick_control_review_generate or review_generate) if quick_notice and control_notice else review_generate
        candidate, review, note = _conference_review(model, instruction, candidate, usable, pending, reviewer,
                                                     team=True, case=case, references=references, ledger=ledger,
                                                     draft_issue=workflow.get("draft_issue"), source_plan=source_plan, intake=intake)
    elif review_meeting and candidate.profile == "plan-draft":
        if candidate.tasks or pending or any(UNCERTAIN.search(value) for value in (request, *usable.values())):
            candidate, review, note = _conference_review(model, instruction, candidate, usable, pending, review_generate, ledger=ledger,
                                                         draft_issue=workflow.get("draft_issue"), source_plan=source_plan, intake=intake)
    if control_notice and review and (review["status"] == "incomplete" or review.get("unaddressed_targets")):
        return Run("needs_review", candidate, materials, metadata, {}, note,
                   conference_review=review, expert_workflow=workflow, original_request=request)
    if review is not None:
        if review["status"] == "needs_input":
            return Run("needs_input", candidate, materials, metadata, {}, note,
                       conference_review=review, expert_workflow=workflow, original_request=request)
        if review["status"] == "incomplete":
            return Run("needs_review", candidate, materials, metadata, {}, note,
                       conference_review=review, expert_workflow=workflow, original_request=request)
        started = monotonic()
        try:
            verifier = (quick_control_review_generate or review_generate) if quick_notice else review_generate
            fixed_checks = [f for source in ledger.values() for f in source.get("已确认核查分工", [])]
            candidate = _verify_conference_draft(model, candidate, {"U.1": instruction, **usable},
                                                verifier, review, fixed_checks=fixed_checks, source_plan=source_plan)
            note += "已核对本轮待交付稿及逐条修订。"
        except (RevisionIssue, OSError, ValueError) as error:
            if audit := review.get("task_evidence"):
                audit["after_sha256"] = _digest(candidate.model_dump())
                audit["status"] = "failed"
            review.update(status="incomplete", failed_stage="task_evidence", error={
                "code": getattr(error, "code", "REVIEW_ERROR"), "message": str(error),
                "elapsed_seconds": round(monotonic() - started, 1),
            })
            if hasattr(error, "model_failure"):
                review["error"]["model_failure"] = error.model_failure
            return Run("needs_review", candidate, materials, metadata, {},
                       "稿件已保留，仍有内容需要核实后才能交付。",
                       conference_review=review, expert_workflow=workflow, original_request=request)
    if expert_team and review:
        brief_incomplete = workflow.get("brief", {}).get("status") == "incomplete"
        workflow["status"] = ("completed" if not brief_incomplete and review["status"] != "incomplete"
                              and not review.get("unaddressed_targets")
                              else "partial")
        if brief_incomplete:
            note = "素材预审未完成，已由主笔直接读取原始材料。" + note
        elif review.get("unaddressed_targets"):
            note += "仍有问题未写回，请按回执核对。"
    elif review:
        workflow["mode"] = "auto_review"
    cited = {a for s in candidate.sections for p in s.paragraphs for a in p.anchors}
    cited.update(a for task in candidate.tasks for a in task.anchors)
    return Run("draft", candidate, materials, metadata, {a: usable[a] for a in sorted(cited)}, note,
               submission=case, conference_review=review, expert_workflow=workflow, original_request=request)


def review_frozen_task(model, parent, *, generate=generate_review):
    """Review only the first draft and original materials sealed by the drafting entry."""
    receipt = json.loads((parent / "运行回执.json").read_text(encoding="utf-8"))
    workflow = receipt.get("expert_workflow") or {}
    frozen = workflow.get("first_draft")
    material = workflow.get("review_materials")
    request = receipt["original_request"]
    draft_sha = _digest(frozen)
    require(receipt["status"] == "draft" and workflow.get("mode") == "one_pass"
            and workflow.get("writer_model") == model and frozen == receipt.get("candidate")
            and isinstance(material, list) and material
            and workflow.get("first_draft_sha256") == draft_sha
            and workflow.get("review_basis_sha256") == _digest([request, model, material, frozen]),
            "找不到同一主笔、材料及内容绑定的可核稿首稿。", "REVIEW_BINDING_INVALID")
    for name, expected in receipt["artifacts"].items():
        path = parent / name
        require(path.resolve().parent == parent.resolve() and not path.is_symlink()
                and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                "父稿交付物已变化。", "PARENT_MISMATCH")
    candidate = Candidate.model_validate(frozen)
    usable = validate(candidate, material)
    instruction = material[0]["blocks"]["U.1"]
    plan = (_source_plan(_conference_sources(material), {m["id"]: m["blocks"] for m in material},
                         instruction if workflow.get("source_execution_phases") else "")
            if MEETING.search(request) else None)
    policy_plan = (policy_actions.source_plan(instruction, material)
                   if not plan and not workflow.get("upstream_style") else None)
    if policy_plan:
        _verify_policy_plan(candidate, policy_plan, instruction)
    targets = {}
    for section_index, section in enumerate(candidate.sections, 1):
        for index, paragraph in enumerate(section.paragraphs, 1):
            targets[f"P{section_index}.{index}"] = (paragraph, "text")
    for index, item in enumerate(candidate.tasks, 1):
        for key in ("action", "lead", "support", "timing", "deliverable", "acceptance", "contact", "phone"):
            targets[f"T{index}.{key}"] = (item, key)
    review_rules = "skills/gongwen-expert-review/SKILL.md"
    raw_rules = (ROOT / review_rules).read_bytes()
    prompt = (raw_rules.decode() + "\n\n补充要求：只核对这份冻结稿。报告有具体位置和原句的问题。"
              "draft_sha256 原样回填。段落或单元格问题使用下列位置键。"
              "用户明确要求的章节缺失时，位置填 sections，引用 U.1，before 和 after 均留空。"
              "anchor 指这条原材料编号，quote 从对应原句逐字摘录。"
              "只建议一次短语替换，before 为原位置唯一出现的片段，after 必须逐字见于 quote。"
              "填原为空的单元格时 before 为空字符串。需要判断、重写或改动多个字段时两者均留空。"
              "blocking 表示影响执行的错误，pending 表示需人工确认，advisory 表示行文建议。"
              "仅 blocking 可以建议写回。"
              "不得把试点建议写成会议决定，不填材料没有的人、时点、验收或联系方式。\n" +
              json.dumps({"draft_sha256": draft_sha, "request": instruction,
                          "locations": {key: {"field": getattr(item, field), "anchors": item.anchors}
                                        for key, (item, field) in targets.items()},
                          "sections": [s.title for s in candidate.sections], "sources": usable}, ensure_ascii=False))
    started = monotonic()
    audit = {"status": "incomplete", "basis_sha256": workflow["review_basis_sha256"],
             "parent_word_sha256": receipt["artifacts"].get("正文.docx"),
             "before_sha256": draft_sha, "after_sha256": draft_sha,
             "review_rules": {review_rules: hashlib.sha256(raw_rules).hexdigest()}, "changes": []}
    review_workflow = {"mode": "frozen_review", "first_draft": frozen, "first_draft_sha256": draft_sha,
                       "source_action_rows": workflow.get("source_action_rows"),
                       "source_execution_phases": workflow.get("source_execution_phases"), "frozen_review": audit}
    if policy_plan:
        review_workflow["writer_profile"] = "numbered_policy_discussion"
    try:
        findings, metadata = generate(model, prompt, FrozenReview)
        audit["model"] = metadata
        audit["findings"] = findings.model_dump()["findings"]
        require(findings.draft_sha256 == draft_sha, "独立核稿返回的首稿校验值不符。", "REVIEW_BINDING_INVALID")
        duplicates = Counter(f.target for f in findings.findings)
        for finding in findings.findings:
            before, after = finding.before, finding.after
            target = targets.get(finding.target)
            source = usable.get(finding.anchor, "")
            cited = bool(finding.quote.strip() and finding.quote in source)
            supported = bool(cited and target and finding.anchor in target[0].anchors)
            missing_section = cited and finding.target == "sections" and finding.anchor == "U.1"
            changed = False
            if (supported and finding.level == "blocking" and duplicates[finding.target] == 1
                    and after in finding.quote and 0 < len(after) <= 80):
                item, field = target
                value = getattr(item, field)
                if ((before.strip() and len(before) <= 80 and value.count(before) == 1)
                        or (isinstance(item, TaskItem) and not (before or value))):
                    edited = value.replace(before, after, 1)
                    if edited != value and (not (plan or policy_plan) or not isinstance(item, TaskItem)):
                        setattr(item, field, edited)
                        try:
                            check_draft(candidate, usable)
                            check_placeholders(candidate)
                            if plan:
                                _verify_source_plan(candidate, plan)
                            if policy_plan:
                                _verify_policy_plan(candidate, policy_plan, instruction)
                            changed = True
                            audit["changes"].append({"target": finding.target, "before": value, "after": edited,
                                                     "anchor": finding.anchor, "quote": finding.quote})
                        except RevisionIssue:
                            setattr(item, field, value)
            if not changed and (supported or missing_section) and finding.level == "blocking":
                audit.setdefault("blocking", []).append(finding.target)
            elif not changed:
                audit.setdefault("manual_review", []).append(finding.target)
        check_draft(candidate, usable)
        check_placeholders(candidate)
        if plan:
            _verify_source_plan(candidate, plan)
        if policy_plan:
            _verify_policy_plan(candidate, policy_plan, instruction)
        require(not audit.get("blocking"), "独立核稿发现影响执行的内容仍需确认，已保留首稿。", "REVIEW_UNRESOLVED")
        audit.update(status="verified", after_sha256=_digest(candidate.model_dump()))
        draft = Run("draft", candidate, material, metadata, usable,
                    expert_workflow=review_workflow, original_request=request)
    except (RevisionIssue, ValueError, OSError) as error:
        audit["error"] = {"code": getattr(error, "code", "REVIEW_ERROR"), "message": str(error)}
        if failure := getattr(error, "model_failure", None):
            audit["error"]["model_failure"] = failure
        draft = Run("needs_review", Candidate.model_validate(frozen), material, {}, {}, str(error),
                    expert_workflow=review_workflow, original_request=request)
    audit["elapsed_seconds"] = round(monotonic() - started, 3)
    return draft


def revise_case(model, request, files, parent, receipt, *, generate=ask_codex):
    """Apply a single new count to the brief and effects on the verified parent."""
    require(receipt["status"] == "draft", "续改需要一份已交付的案例稿。")
    for name in ("正文.docx", "正文.txt"):
        require(hashlib.sha256((parent / name).read_bytes()).hexdigest() == receipt["artifacts"].get(name),
                f"父版{name}已变化，请提供原交付目录。", "PARENT_MISMATCH")
    materials = collect(request, (), files)
    prior = receipt["materials"][1:]
    require(len(materials) == len(prior) + 2, "请按原顺序附上全部原材料，末尾另附一份补数 CSV。")
    require(all(a["sha256"] == b["sha256"] for a, b in zip(materials[1:-1], prior, strict=True)),
            "本轮原材料与父版不同，请核对文件与顺序。", "PARENT_MISMATCH")
    supplement = materials[-1]
    require(Path(supplement["name"]).suffix.lower() == ".csv" and len(supplement["blocks"]) == 2,
            "补数 CSV 应含表头和一条数据。")
    with Path(files[-1]).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, strict=True))
    require(len(rows) == 1 and all(rows[0].get(k) for k in ("事项", "指标", "已核实数值", "范围与来源")),
            "补数 CSV 需要事项、指标、已核实数值和范围与来源。")
    datum = rows[0]
    value = datum["已核实数值"]
    token = re.fullmatch(r"(\d+)条", value)
    require(token is not None, "此案例续改当前接收一项以「条」计的数量。")
    anchor = next(reversed(supplement["blocks"]))
    candidate = Candidate.model_validate(receipt["candidate"])
    parent_text, _ = submission.render_word(candidate.title, case_sections(candidate))
    require(parent_text == (parent / "正文.txt").read_text(encoding="utf-8"),
            "父版结构化内容与交付稿不同，请提供原交付目录。", "PARENT_MISMATCH")
    indexed = {s.title: s for s in candidate.sections if s.title in submission.EDITABLE}
    require(tuple(indexed) == submission.EDITABLE, "父版缺少可续改的案例小节。")
    prompt = (
        "以父稿续改，执行本轮用户意见，只能用下列补数口径。分别在内容提要和成效与影响各改一段，"
        "按原文最小改动保留其余内容及原段所有数字。不要新增日期等其他数字。"
        "两处都明确事项、谁提出、纳入何方案和准确数量，不写为全村累计或办结量。"
        "返回每处节名、从1起的原段号和整段替换文本，无换行。\n"
        + json.dumps({"用户": request, "补数": datum, "父稿": {
            name: [p.text for p in s.paragraphs] for name, s in indexed.items()
        }}, ensure_ascii=False)
    )
    edits, metadata = generate(model, prompt, submission.Edits)
    require(len(edits.changes) == 2 and {e.section for e in edits.changes} == set(indexed),
            "续改必须分别定位到提要和成效各一段。", "EDIT_SCOPE")
    changes = []
    for edit in edits.changes:
        paragraphs = indexed[edit.section].paragraphs
        require(1 <= edit.paragraph <= len(paragraphs), "续改定位超出父版。", "EDIT_SCOPE")
        passage = paragraphs[edit.paragraph - 1]
        before, after = passage.text, edit.text
        expected = Counter(NUMBERS.findall(before)) + Counter([token[1]])
        require(Counter(NUMBERS.findall(after)) == expected and value in after and datum["事项"] in after,
                "续改的事项或数字与补数不符，或改动了原数字。", "EDIT_SCOPE")
        passage.text = after
        passage.anchors = list(dict.fromkeys([*passage.anchors, anchor]))
        changes.append({"section": edit.section, "paragraph": edit.paragraph, "before": before,
                        "after": after, "anchor": anchor})
    candidate.material_uses.append(MaterialUse(source_id=supplement["id"], role="facts", usable_anchors=[anchor]))
    usable = validate(candidate, materials)
    check_draft(candidate, usable)
    case = {**receipt["submission"], "counts": submission.check_sections(case_sections(candidate)),
            "parent_word_sha256": receipt["artifacts"]["正文.docx"], "changes": changes}
    cited = {a for s in candidate.sections for p in s.paragraphs for a in p.anchors}
    return Run("draft", candidate, materials, metadata, {a: usable[a] for a in sorted(cited)}, submission=case)


def write_result(run, output):
    started = monotonic()
    candidate = run.candidate
    instruction = run.materials[0]["blocks"]["U.1"]
    original_request = run.original_request or instruction
    policy_plan = None
    if run.status == "draft":
        check_placeholders(candidate)
        if run.model.get("structured_meeting_purpose"):
            _verify_meeting_purpose(candidate)
        workflow = run.expert_workflow or {}
        if workflow.get("mode") in {"one_pass", "frozen_review"} and workflow.get("source_action_rows"):
            phases = workflow.get("mode") == "one_pass" or workflow.get("source_execution_phases")
            plan = _source_plan(_conference_sources(run.materials), {m["id"]: m["blocks"] for m in run.materials},
                                instruction if phases else "")
            require(plan is not None, "交件时未找到冻结的纪要分工依据。", "REVIEW_BINDING_INVALID")
            _verify_source_plan(candidate, plan)
        elif workflow.get("mode") in {"one_pass", "frozen_review"} and not workflow.get("upstream_style"):
            policy_plan = policy_actions.source_plan(instruction, run.materials)
            if policy_plan:
                _verify_policy_plan(candidate, policy_plan, instruction)
        if workflow.get("mode") == "frozen_review":
            audit = workflow["frozen_review"]
            require(audit.get("status") == "verified" and audit.get("after_sha256") == _digest(candidate.model_dump()),
                    "交件稿与本轮冻结稿核验结果不符。", "REVIEW_BINDING_INVALID")
        if review := run.conference_review:
            audit = review.get("task_evidence", {})
            require(audit.get("status") == "verified" and audit.get("after_sha256") == _digest(candidate.model_dump())
                        and not review.get("unaddressed_targets"), "交件稿与本轮核验结果不符。", "REVIEW_BINDING_INVALID")
        check_draft(candidate, run.excerpts)
        check_notice_receipt(candidate, instruction)
        if candidate.profile == "notice-draft" and (
            EXPLICIT_REHEARSAL_DOCUMENT.search(instruction) or EXPLICIT_RETROSPECTIVE_DRAFT.search(instruction)
            or EXPLICIT_WORD_REVIEW_DRAFT.search(instruction)
            or RELATIVE_WORK_DATES.search(instruction) and KEEP_RELATIVE_DATES.search(instruction)
        ):
            original_title = candidate.title.strip()
            label = "演练审阅稿" if EXPLICIT_REHEARSAL_DOCUMENT.search(instruction) else "审阅稿"
            title = re.sub(r"[（(](?:演练|复盘)?审阅稿[）)]$", "", original_title).rstrip() + f"（{label}）"
            candidate = candidate.model_copy(deep=True)
            candidate.title = title
            if candidate.sections[0].title.strip() == original_title:
                candidate.sections[0].title = title
        if issuer := explicit_plan_issuer(candidate, instruction):
            require(has_plan_issuer(candidate, issuer),
                    "成稿缺少用户明确指定的落款单位，未生成 Word。", "DRAFT_INCOMPLETE")
    receipt = {
        "status": run.status, "message": run.note, "model": run.model,
        "original_request": original_request,
        "materials": [{k: v for k, v in m.items() if k != "blocks"} for m in run.materials],
        "candidate": candidate.model_dump() if run.status == "draft" else {
            "profile": candidate.profile, "material_uses": [u.model_dump() for u in candidate.material_uses],
            "questions": candidate.questions,
        },
        "cited_excerpts": run.excerpts,
        "artifacts": {},
    }
    if run.expert_workflow:
        receipt["expert_workflow"] = run.expert_workflow
    if run.conference_review:
        receipt["conference_review"] = run.conference_review
    if run.status == "needs_review":
        receipt["rejected_candidate"] = candidate.model_dump()
        if run.draft_check_error:
            receipt["draft_check_error"] = run.draft_check_error
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".gongwen-", dir=output.parent) as temp:
        folder = Path(temp) / "result"
        folder.mkdir()
        lines = [f"识别结果：{PROFILES[candidate.profile]}", f"状态：{run.note or '初稿，待人工核稿。'}"]
        names = {m["id"]: m["name"] for m in run.materials}
        lines += ["", "材料用途："] + [f"- {names.get(u.source_id, u.source_id)}：{ROLE_NAMES[u.role]}" for u in candidate.material_uses]
        if any(m.get("intake_notes") for m in run.materials):
            lines += ["", "附件读取说明："] + [f"- {m['name']}：{note}" for m in run.materials
                                                  for note in m.get("intake_notes", ())]
        if run.expert_workflow and (brief := run.expert_workflow.get("brief")):
            lines += ["", "专家团素材预审：" + ("已完成" if brief["status"] == "completed" else "未完成，已保留原材料")]
            if "error" in brief:
                lines.append(f"- {brief['error']['message']}")
        if run.status == "draft":
            if run.conference_review:
                review = run.conference_review
                lines += ["", "独立核稿：", f"- {run.note}"]
                if review.get("error"):
                    lines.append(f"- {review['error']['message']}")
                levels = {"blocking": "需核对", "pending": "待确认", "advisory": "建议"}
                for finding in review.get("review", {}).get("findings", ()):
                    targets = "、".join(finding["target_ids"])
                    evidence = "、".join(finding["basis_ids"])
                    lines.append(f"- {levels[finding['level']]} · {targets}（{evidence}）：{finding['defect']}")
                if review.get("changes"):
                    positions = "、".join(c["target_id"] for c in review["changes"])
                    lines.append(f"- 已写回位置：{positions}。修改前后见运行回执；请核定实际效果。")
                if review.get("unaddressed_targets"):
                    lines.append("- 未写回位置：" + "、".join(review["unaddressed_targets"]) + "，请人工核对。")
            if run.submission:
                receipt["submission"] = run.submission
                plain, word = submission.render_word(candidate.title, case_sections(candidate))
                if "changes" in run.submission:
                    comparison = ["# 本轮变化对照", "", f"父版 Word SHA-256：`{run.submission['parent_word_sha256']}`"]
                    for change in run.submission["changes"]:
                        a = change["anchor"]
                        comparison += ["", f"## {change['section']} · 第 {change['paragraph']} 段", "",
                                       f"- 改前：{change['before']}", f"- 改后：{change['after']}",
                                       f"- 依据 {a}：{run.excerpts[a]}"]
                    (folder / "变化对照.md").write_text("\n".join(comparison) + "\n", encoding="utf-8")
            else:
                plain, word = (render(candidate, source_rows=True) if policy_plan or (run.expert_workflow or {}).get("source_action_rows")
                               else render(candidate))
            (folder / "正文.txt").write_text(plain, encoding="utf-8")
            (folder / "正文.docx").write_bytes(word)
            lines += ["", "待核："] + [f"- {note}" for note in candidate.review_notes]
            for i, task in enumerate(candidate.tasks, 1):
                pending = [label for label, value in zip(TASK_FIELD_LABELS, (
                    task.lead, task.support, task.timing, task.deliverable, task.acceptance
                ), strict=True) if not value.strip()]
                if pending:
                    lines.append(f"- 任务 {i}（{task.action}）：{'、'.join(pending)}待确认。")
            review_anchors = {anchor for note in candidate.review_notes for anchor in ANCHOR.findall(note)}
            source_blocks = {a: value for material in run.materials for a, value in material["blocks"].items()}
            if review_anchors:
                lines += ["", "待核事项涉及的原文（含参考件，不作为本轮既定安排）："]
                for a in sorted(review_anchors):
                    lines.append(f"- {a}：{source_blocks.get(a, '未找到原文，请人工核对。')}")
                receipt["review_excerpts"] = {a: source_blocks.get(a) for a in sorted(review_anchors)}
            lines += ["", "正文和任务对应依据见回执；以下是引用片段："] + [
                f"- {a}（{names[a.split('.')[0]]}）：{value}" for a, value in run.excerpts.items()
            ]
            if run.submission:
                lines += ["", "逐段出处："]
                for section in candidate.sections:
                    for i, passage in enumerate(section.paragraphs, 1):
                        lines.append(f"- {section.title} 第 {i} 段　{', '.join(passage.anchors)}")
            receipt["artifacts"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
        elif candidate.questions:
            lines += ["", "需要补充："] + [f"- {q}" for q in candidate.questions]
        (folder / "依据与待核.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        receipt["artifact_build_seconds"] = round(monotonic() - started, 3)
        (folder / "运行回执.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        require(not output.exists(), "输出目录已存在，请指定一个新目录。")
        folder.rename(output)


def main(argv=None, *, quick_notice_generate=None, quick_control_review_generate=None):
    parser = argparse.ArgumentParser(description="直接描述需求并附材料，起草或改稿。")
    parser.add_argument("request", help="自然语言需求，可含本轮事实")
    parser.add_argument("--text", action="append", default=[], help="另贴一份材料，可重复")
    parser.add_argument("--public-schedule", type=json.loads, help=argparse.SUPPRESS)
    parser.add_argument("--text-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--file", action="append", default=[], type=Path, help="材料文件，可重复")
    parser.add_argument("--continue-from", type=Path, help="上一轮交付目录；可另附一份手改 Word 副本")
    parser.add_argument("--style-library", type=Path, help="已获准使用的固定版本 lieflat-gongwen 目录")
    parser.add_argument("--opinion-template", type=Path, help="意见汇总空白 Word（辽宁六列版式）")
    parser.add_argument("--output", required=True, type=Path, help="本次结果的新目录")
    parser.add_argument("--codex-model", required=True, help="明确同意将本轮需求和可读材料交给本机配置的 Codex 模型")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--single-writer", action="store_true", help="公文专家独立起草")
    mode.add_argument("--expert-team", action="store_true", help="公文专家团：素材预审、起草、独立核稿")
    mode.add_argument("--review-frozen-from", type=Path, help="仅对原任务已冻结的首稿独立核稿")
    args = parser.parse_args(argv)
    try:
        require(not args.output.exists(), "输出目录已存在，请指定一个新目录。")
        if args.review_frozen_from:
            require(not (args.text or args.file or args.continue_from or args.text_only or args.style_library
                         or args.opinion_template or args.public_schedule), "冻结稿核验只消费原稿和原材料。")
            # One claim per parent, including an interrupted or failed model call.
            claim = args.review_frozen_from.with_suffix(".review-attempt.json")
            try:
                with claim.open("x", encoding="utf-8") as handle:
                    json.dump({"output": str(args.output.resolve())}, handle)
            except FileExistsError:
                parser.exit(2, "原首稿已有独立核稿调用，请查看原回执。\n")
            run = review_frozen_task(args.codex_model, args.review_frozen_from)
            write_result(run, args.output)
            return 0 if run.status == "draft" else 2
        if args.text_only:
            require(not (args.continue_from or args.opinion_template),
                    "请说明这次希望在聊天里查看哪份稿子及哪部分内容。")
            run = run_task(args.codex_model, args.request, args.text, args.file, expert_team=args.expert_team,
                           quick_notice_generate=quick_notice_generate,
                           quick_control_review_generate=quick_control_review_generate,
                           public_schedule=args.public_schedule,
                           checkpoint=args.output.with_suffix(".first-draft.json"))
            if run.status == "draft":
                check_draft(run.candidate, run.excerpts)
                paragraphs = [run.candidate.title]
                for section in run.candidate.sections:
                    if section.title and section.title != run.candidate.title:
                        paragraphs.append(section.title)
                    paragraphs.extend(p.text for p in section.paragraphs)
                if run.candidate.tasks:
                    rows, _ = _task_table(run.candidate.tasks, source_rows=bool(policy_plan or (run.expert_workflow or {}).get("source_action_rows")))
                    paragraphs.append("分工表")
                    paragraphs.extend("｜".join(row) for row in rows)
                run.note = "\n".join(paragraphs)
                run.status = "text_only"
            write_result(run, args.output)
            return 0 if run.status == "text_only" else 2
        if args.opinion_template is not None:
            require(not (args.text or args.continue_from or args.style_library),
                    "套版请用 --file 提交本轮待改 Word 和逐条意见记录。")
        if args.continue_from:
            receipt_path = args.continue_from / "运行回执.json"
            receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
            # A repeated complete drafting request is not a revision instruction.
            if not (args.file or args.text) and receipt.get("original_request") == args.request:
                args.continue_from = None
        if args.continue_from:
            require(not args.text, "请把本轮修改意见放在自然语言需求中，再附原文件。")
            if (receipt.get("submission") or {}).get("profile") == submission.PROFILE:
                run = revise_case(args.codex_model, args.request, args.file, args.continue_from, receipt,
                                  generate=submission.generate_case)
                write_result(run, args.output)
                print(f"案例续改已生成，待核稿。\n{args.output.resolve()}")
                return 0
            local_revision = receipt.get("profile") in deadline.CONTINUATION_PROFILES
            if local_revision or receipt.get("status") == "draft":
                prior = args.continue_from / "正文.docx"
                expected = (receipt.get("artifacts") or {}).get(prior.name)
                if local_revision:
                    require(receipt.get("status") == "revision" and expected == receipt.get("revision_id"),
                            "上一轮修改还没有生成可用的 Word。")
                require(expected and not prior.is_symlink() and prior.is_file() and
                        hashlib.sha256(prior.read_bytes()).hexdigest() == expected,
                        "找不到可核对的上一版 Word。")
                require(not args.file, "请确认以对话里的上一版还是新上传的 Word 为准。")
                edit = deadline.revise_deadline if deadline.pair(args.request) else deadline.revise_text
                revised = edit(args.codex_model, args.request, prior, args.output, previous=expected)
                print(f"{'Word 已生成，待核稿。' if revised else '未修改原稿，请查看核稿单。'}\n{args.output.resolve()}")
                return 0 if revised else 2
            require(receipt.get("profile") == "plan-revise",
                    "此类稿件的自动续改尚未支持；原稿未改动。请提供当前 Word 和明确意见，按已有核稿能力处理。")
            revise(ROOT, args.codex_model, args.request, args.file, args.output,
                   continuation=args.continue_from)
            print(f"工作方案改稿已生成，待核稿。\n{args.output.resolve()}")
            return 0
        ordinary_word = not args.text and len(args.file) == 1 and args.file[0].suffix.lower() == ".docx"
        if ordinary_word and not (args.style_library or args.opinion_template) and native_review.matches_request(args.request):
            receipt = native_review.review(args.request, args.file[0], args.output)
            print(f"{receipt['message']}详见逐条核稿单。\n{args.output.resolve()}")
            return 0
        letterhead = known_source(args.file, args.text)
        if letterhead is not None and args.opinion_template is None:
            revise_letterhead(letterhead, args.request, args.codex_model, args.output)
            print(f"红头通知模拟改稿已生成，待核稿。\n{args.output.resolve()}")
            return 0
        opinions = structured_opinions(args.file) if not args.text else None
        if args.opinion_template is not None:
            require(opinions is not None, "套版需要待改 Word 和含逐条采纳结论的文本记录。")
        if opinions is not None:
            template = {"template": args.opinion_template} if args.opinion_template is not None else {}
            revise_opinions(args.codex_model, args.request, args.file, args.output, opinions, **template)
            print(f"逐条核稿 Word 已生成，待业务核稿。\n{args.output.resolve()}")
            return 0
        grouped = review_sources(args.request, args.file) if not args.text else None
        if grouped is not None:
            revise_grouped(args.codex_model, args.request, args.file, args.output, grouped, include_summary=True)
            print(f"意见核稿结果已生成，待业务核稿。\n{args.output.resolve()}")
            if (args.output / "改稿短表.docx").is_file():
                print("改稿短表.docx")
            return 0
        if ordinary_word and deadline.pair(args.request):
            revised = deadline.revise_deadline(args.codex_model, args.request, args.file[0], args.output)
            print(f"{'Word 已生成，待核稿。' if revised else '未修改原稿，请查看核稿单。'}\n{args.output.resolve()}")
            return 0 if revised else 2
        run = run_task(args.codex_model, args.request, args.text, args.file, style_library=args.style_library,
                       generate=submission.generate_case if args.style_library else generate_intake,
                       expert_team=args.expert_team,
                       quick_notice_generate=quick_notice_generate,
                       quick_control_review_generate=quick_control_review_generate, public_schedule=args.public_schedule,
                       checkpoint=args.output.with_suffix(".first-draft.json"))
        if ordinary_word and run.status == "revision_identified":
            deadline.revise_deadline(args.codex_model, args.request, args.file[0], args.output)
            print(f"未修改原稿，请查看核稿单。\n{args.output.resolve()}")
            return 2
        if run.status == "revision_identified" and run.candidate.profile == "plan-revise":
            require(not args.text, "请把本轮修改意见放在自然语言需求中，再附原文件。")
            revise(ROOT, args.codex_model, args.request, args.file, args.output, intake=run.model)
            print(f"工作方案改稿已生成，待核稿。\n{args.output.resolve()}")
            return 0
        write_result(run, args.output)
    except (RevisionIssue, OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"{error}\n")
    print(f"{PROFILES[run.candidate.profile]}：{run.note or '初稿已生成，待核稿。'}")
    print(args.output.resolve())
    return 0 if run.status == "draft" else 2


if __name__ == "__main__":
    raise SystemExit(main())
