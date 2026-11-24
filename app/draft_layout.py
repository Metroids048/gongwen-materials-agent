"""Editable review drafts; a unit's formal template belongs to a separate path."""

import re

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.revision.document import open_docx, require, table_rows, word_bytes

TASK_FIELD_LABELS = ("责任单位", "配合单位", "时间或依赖", "交付成果", "验收方式")
NOTICE_COLUMNS = ("场次", "参加对象", "时间", "地点或线上渠道", "主要内容")
NOTICE_RECEIPT = "报名回执"
NOTICE_DATE = re.compile(r"(?:\d{4}年\s*)?\d{1,2}月\s*\d{1,2}日")
NOTICE_CONTACT = re.compile(r"(?:联系人|联系电话|电话|联系方式)\s*[:：]")
NOTICE_DATE_FIELD = re.compile(r"(?:成文日期|落款日期|日期)\s*[:：]\s*(.*)")
NOTICE_ISSUER_FIELD = re.compile(r"(?:主办单位|发文单位|发文机关|落款单位|发布单位|单位)\s*[:：]\s*(.*)")
NOTICE_ISSUER = re.compile(
    r"(?:办公室|[^，。；：！？\n]{2,60}(?:中心|办公室|委员会|人民政府|[市区县镇乡]政府|社区|居委会|村委会|厅|局|处|院|校|学校|大学|公司|协会|联合会))")
PLAN_COMPARISON = "帮办与代办情形对照"
PLAN_AGENDA = "会商议题表"
OPINION_FEEDBACK = "逐项反馈表"
PLAN_TABLE_COLUMNS = {
    PLAN_COMPARISON: ("情形", "仅帮办", "需办委托", "判定与下一步"),
    PLAN_AGENDA: ("议题", "待定内容", "议定要点"),
    OPINION_FEEDBACK: ("已有方向", "请反馈的问题"),
}
TABLE_WIDTHS = {
    "notice-table": (1.35, 3.5, 3.15, 3.1, 4.5),
    PLAN_COMPARISON: (2.3, 3.9, 4.3, 5.1),
    PLAN_AGENDA: (1.05, 2.4, 3.7, 4.65, 3.8),
    OPINION_FEEDBACK: (3.1, 7.6, 4.9),
}


def _style(doc, name, size, *, bold=False):
    style = doc.styles[name]
    style.font.name = "Times New Roman"
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor(0, 0, 0)
    fonts = style._element.get_or_add_rPr().rFonts
    for attr in tuple(fonts.attrib):
        if "theme" in attr.lower():
            del fonts.attrib[attr]
    fonts.set(qn("w:eastAsia"), "Songti SC")
    props = style._element.get_or_add_pPr()
    border = props.find(qn("w:pBdr"))
    if border is not None:
        props.remove(border)
    return style.paragraph_format


def _task_table(tasks, *, source_rows=False):
    empty = "" if source_rows else "待明确"
    def responsibility(task, _):
        unit = task.lead.strip()
        if not unit.startswith(("牵头：", "共同牵头：", "责任单位：", "共同负责：", "组织：", "总体落实：")):
            unit = ("责任单位：" + (unit or empty)) if unit or empty else ""
        return unit + ("\n配合：" + task.support.strip() if task.support.strip() else "")

    columns = [("序号", 1.45, lambda task, i: str(i)),
               ("工作任务", 6, lambda task, _: task.action.strip() +
                ("（建议）" if task.status == "proposed" else ""))]
    if source_rows:
        columns.extend((("牵头或共同负责", 3.25, lambda task, _: task.lead.strip()),
                        ("配合单位", 1.95, lambda task, _: task.support.strip())))
    else:
        columns.append(("责任分工", 5.2, responsibility))
    quarterly = all(len(table_row(task.timing)) == 4 for task in tasks)
    if quarterly:
        for index, quarter in enumerate(("一季度", "二季度", "三季度", "四季度")):
            columns.append((quarter, 2.95, lambda task, _, index=index: table_row(task.timing)[index]))
    else:
        columns.append(("时间或依赖", 6.8, lambda task, _: task.timing.strip() or empty))
    for name, field, width in (("交付成果", "deliverable", 3.25), ("验收方式", "acceptance", 3.25)):
        if source_rows or any(getattr(task, field).strip() for task in tasks):
            columns.append((name, 2.3 if quarterly else width,
                            lambda task, _, field=field: getattr(task, field).strip() or empty))
    # These are real editable cells even when the user has not supplied their values.
    columns.extend((("联系人", 2, lambda task, _: task.contact.strip()),
                    ("电话", 2.7, lambda task, _: task.phone.strip())))
    data = [[name for name, _, _ in columns]]
    data.extend([value(task, i) for _, _, value in columns] for i, task in enumerate(tasks, 1))
    # Give unused optional-column space back to the task descriptions and stage details.
    remaining = 25.7 - sum(width for _, width, _ in columns)
    weights = {1: .4, 2: .2, 3: .1, 4: .1, 5: .1, 6: .1} if quarterly else {1: .35, 3: .65}
    if source_rows and not quarterly:
        weights = {1: .35, 4: .65}
    widths = [width + remaining * weights.get(i, 0)
              for i, (_, width, _) in enumerate(columns)]
    return data, widths


def table_row(text):
    return [cell.strip() for cell in text.split("｜")]


def plan_table(section):
    columns = list(PLAN_TABLE_COLUMNS[section.title])
    rows = [table_row(p.text) for p in section.paragraphs]
    if section.title == PLAN_AGENDA:
        return [["序号", *columns, "议定结果"],
                *[[str(i), *row, ""] for i, row in enumerate(rows, 1)]]
    if section.title == OPINION_FEEDBACK:
        return [[*columns, "街镇反馈"], *[[*row, ""] for row in rows]]
    return [columns, *rows]


def _word_table(doc, data, widths, font_size):
    style_name = f"Draft Table Cell {font_size}"
    cell_style = (doc.styles[style_name] if style_name in doc.styles
                  else doc.styles.add_style(style_name, doc.styles["Normal"].type))
    cell_style.base_style = doc.styles["Normal"]
    cells = _style(doc, cell_style.name, font_size)
    cells.alignment = WD_ALIGN_PARAGRAPH.LEFT
    cells.first_line_indent = Pt(0)
    cells.line_spacing = Pt(16)
    cells.space_before = cells.space_after = Pt(1)
    table = doc.add_table(rows=0, cols=len(widths))
    table.style = "Table Grid"
    table.autofit = False
    for column, width in zip(table.columns, widths, strict=True):
        column.width = Cm(width)
    for i, values in enumerate(data):
        row = table.add_row()
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        if i == 0:
            row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for j, (cell, value, width) in enumerate(zip(row.cells, values, widths, strict=True)):
            cell.width = Cm(width)
            p = cell.paragraphs[0]
            p.style = cell_style
            if i == 0 or j == 0:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.add_run(value).bold = i == 0


def _notice_tail_role(text):
    line = text.strip()
    date_field = NOTICE_DATE_FIELD.fullmatch(line)
    date = date_field[1].strip() if date_field else line
    if (NOTICE_DATE.fullmatch(date) or re.sub(r"\s", "", date) == "年月日"
            or (re.search(r"[_＿]", date) and re.fullmatch(
                r"(?:[\d_＿]+年)?[\d_＿]+月[\d_＿]+日", re.sub(r"\s", "", date)))
            or (date_field and not re.sub(r"[\s_＿]", "", date))):
        return "date", text
    if NOTICE_CONTACT.match(line):
        return "contact", text
    issuer_field = NOTICE_ISSUER_FIELD.fullmatch(line)
    issuer = (issuer_field[1] if issuer_field else line).rstrip("。.").strip()
    if issuer_field and not re.sub(r"[\s_＿]", "", issuer):
        return "issuer", line
    if NOTICE_ISSUER.fullmatch(issuer):
        return "issuer", issuer
    return None


def _notice_sections(sections, *, heading_required=False):
    """Normalize only a recognized metadata suffix; keep all business sections."""
    end = len(sections)
    while end and sections[end - 1].title in {NOTICE_RECEIPT, NOTICE_RECEIPT + "说明"}:
        end -= 1
    lines = [(si, pi, li, line) for si, section in enumerate(sections[:end])
             for pi, passage in enumerate(section.paragraphs)
             for li, line in enumerate(passage.text.splitlines()) if line.strip()]
    tail = []
    for item in reversed(lines):
        # Plans need an explicit signature heading; a business location is not an issuer.
        if heading_required and "落款" not in sections[item[0]].title:
            break
        role = _notice_tail_role(item[3])
        if not role and tail and tail[-1][4][0] == "date" and re.fullmatch(r"[_＿\s]+", item[3]):
            role = ("issuer", item[3])
        if not role or (tail and role[0] == "date"):
            break
        tail.append((*item, role))
    issuers = {item[4][1] for item in tail if item[4][0] == "issuer"}
    if len(issuers) != 1:
        return [(section, []) for section in sections]
    tail.reverse()
    removed = {item[:3] for item in tail}
    signature = [("notice-contact", item[4][1]) for item in tail if item[4][0] == "contact"]
    signature += [("notice-signature", next(iter(issuers)))]
    signature += [("notice-signature", item[4][1]) for item in tail if item[4][0] == "date"]
    result = []
    for si, section in enumerate(sections):
        passages = []
        for pi, passage in enumerate(section.paragraphs):
            remaining = [line for li, line in enumerate(passage.text.splitlines())
                         if (si, pi, li) not in removed]
            if any(line.strip() for line in remaining):
                passages.append(passage.model_copy(update={"text": "\n".join(remaining)}))
        if passages:
            result.append((section.model_copy(update={"paragraphs": passages}), []))
        if si == tail[0][0]:
            result.append((None, signature))
    return result


def render(candidate, *, source_rows=False):
    doc = Document()
    page = doc.sections[0]
    page.page_width, page.page_height = Cm(21), Cm(29.7)
    page.left_margin = page.right_margin = Cm(2.7)
    page.top_margin = page.bottom_margin = Cm(2.3)
    body = _style(doc, "Normal", 16)
    body.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    body.first_line_indent = Pt(32)
    body.line_spacing = Pt(21 if candidate.profile == "plan-draft" and candidate.tasks else 24)
    body.space_after = Pt(0)
    title = _style(doc, "Title", 22 if len(candidate.title) <= 19 else 18, bold=True)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.first_line_indent = Pt(0)
    title.line_spacing = Pt(28)
    title.space_after = Pt(12)
    title.keep_with_next = True
    heading = _style(doc, "Heading 1", 16, bold=True)
    heading.first_line_indent = Pt(0)
    heading.space_before = Pt(6)
    heading.space_after = Pt(0)
    heading.keep_with_next = True

    notice = candidate.profile == "notice-draft"
    signature_layout = notice or candidate.profile == "plan-draft"
    paragraphs = [("Title", candidate.title)]
    sections = (_notice_sections(candidate.sections, heading_required=not notice) if signature_layout
                else [(section, []) for section in candidate.sections])
    for index, (section, signature) in enumerate(sections):
        if signature:
            paragraphs.extend(signature)
            continue
        if signature_layout and index == 0 and section.title.strip() == candidate.title.strip():
            section = section.model_copy(update={"title": ""})
        first_lines = section.paragraphs[0].text.splitlines()
        address = (notice and index == 0
                   and bool(first_lines) and first_lines[0].rstrip().endswith("：")
                   and (not section.title.strip() or section.title.strip().startswith("主送")
                        or section.title.strip() == first_lines[0].rstrip()[:-1]))
        if section.title.strip() and not address:
            paragraphs.append(("Heading 1", section.title))
        if candidate.profile in {"plan-draft", "opinion-draft"} and section.title in PLAN_TABLE_COLUMNS:
            if section.title == PLAN_AGENDA:
                paragraphs.append(("Normal", "议定结果栏会前留空，供现场逐项填写。"))
            paragraphs.append((section.title, plan_table(section)))
            continue
        if notice and section.title == NOTICE_RECEIPT:
            fields = table_row(section.paragraphs[0].text)
            sessions = dict.fromkeys((row[0], row[2], row[3]) for style, table in paragraphs
                                     if style == "notice-table" for row in table[1:])
            if sessions and "场次" in fields:
                fields.extend(label for label in ("时间", "地点或线上渠道") if label not in fields)
            rows = []
            for session, time, place in sessions or [("", "", "")]:
                known = {"场次": session, "时间": time, "地点或线上渠道": place}
                rows.append([known.get(field, "") for field in fields])
            paragraphs.append((NOTICE_RECEIPT, [fields, *rows]))
            continue
        if notice and all("｜" in p.text for p in section.paragraphs):
            paragraphs.append(("notice-table", [list(NOTICE_COLUMNS), *[table_row(p.text) for p in section.paragraphs]]))
            continue
        if address:
            paragraphs.append(("notice-address", first_lines[0]))
            remaining = section.paragraphs[1:]
            if len(first_lines) > 1:
                remaining = [section.paragraphs[0].model_copy(update={
                    "text": "\n".join(first_lines[1:])}), *remaining]
            section = section.model_copy(update={"paragraphs": remaining})
        for passage in section.paragraphs:
            lines = passage.text.splitlines()
            style = ("notice-contact" if notice
                     and all(NOTICE_CONTACT.match(line) for line in lines) else "Normal")
            prefix = ("建议：" if candidate.profile not in {
                "speech-draft", "request-draft", "summary-draft", "exchange-draft",
            } and passage.kind == "proposal" and not passage.text.lstrip().startswith("建议")
                and not (source_rows and passage.text.startswith("试点反馈：")) else "")
            # Manual line breaks in a justified paragraph stretch short Chinese lines in Word/WPS.
            if notice:
                paragraphs.extend((style, (prefix if i == 0 else "") + line)
                                  for i, line in enumerate(lines) if line.strip())
            else:
                paragraphs.append((style, prefix + passage.text))
    if candidate.tasks:
        paragraphs.append(("Normal", "附件：任务分工表"))
    expected = []
    expected_tables = []
    table_styles = TABLE_WIDTHS.keys() | {NOTICE_RECEIPT}
    for style, value in paragraphs:
        if style in table_styles:
            widths = ((15.6 / len(value[0]),) * len(value[0]) if style == NOTICE_RECEIPT
                      else TABLE_WIDTHS[style])
            _word_table(doc, value, widths, 11)
            expected_tables.append(value)
            continue
        if style == "Title" and "\n" not in value:
            # Keep the document's status label together across Word renderers.
            suffix = re.search(r"（(?:会商稿|讨论稿|征求意见稿|(?:演练)?审阅稿|送审稿)）$", value)
            main = value[:suffix.start()] if suffix and len(value) >= 19 else value
            if 24 < len(main) <= 46:
                middle = len(main) // 2
                breaks = [i + 1 for i, char in enumerate(main) if char in "、，：）"
                          and abs(i + 1 - middle) <= 4]
                split = min(breaks, key=lambda i: abs(i - middle)) if breaks else middle
                main = main[:split] + "\n" + main[split:]
            value = main + "\n" + suffix.group() if suffix and len(value) >= 19 else main
        p = doc.add_paragraph(value, style=style if not style.startswith("notice-") else "Normal")
        if style in {"notice-address", "notice-signature", "notice-contact"}:
            p.paragraph_format.first_line_indent = Pt(0)
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT if style == "notice-signature" else WD_ALIGN_PARAGRAPH.LEFT
        expected.append(value)

    data = []
    if candidate.tasks:
        appendix = doc.add_section()
        section_break = doc.paragraphs[-1].paragraph_format
        section_break.line_spacing = Pt(1)
        section_break.space_before = section_break.space_after = Pt(0)
        appendix.orientation = WD_ORIENT.LANDSCAPE
        appendix.page_width, appendix.page_height = Cm(29.7), Cm(21)
        appendix.left_margin = appendix.right_margin = Cm(2)
        appendix.top_margin = appendix.bottom_margin = Cm(1.5)
        label = doc.add_paragraph("附件").paragraph_format
        label.first_line_indent = Pt(0)
        label.line_spacing = Pt(20)
        doc.add_paragraph("任务分工表", style="Title")
        expected += ["", "附件", "任务分工表"]
        paragraphs.extend([("Normal", "附件"), ("Title", "任务分工表")])

        data, widths = _task_table(candidate.tasks, source_rows=source_rows)
        _word_table(doc, data, widths, 12)
        expected_tables.append(data)

    word = word_bytes(doc)
    reread = open_docx(word)
    require([p.text for p in reread.paragraphs] == expected
            and [table_rows(t) for t in reread.tables] == expected_tables,
            "导出内容与本轮稿件不一致。", "RENDER_MISMATCH")
    plain = "\n\n".join(value if style not in table_styles else "\n".join("｜".join(row) for row in value)
                       for style, value in paragraphs) + "\n"
    if data:
        plain += "\n" + "\n".join("｜".join("；".join(line.rstrip("。；") for line in value.splitlines()) for value in row) for row in data) + "\n"
    return plain, word
