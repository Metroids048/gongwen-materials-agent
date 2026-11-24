"""Reflow a confirmed notice into the published Ziyang office Word layout."""

import re
from copy import deepcopy
from datetime import date

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml.ns import qn
from docx.shared import Pt

from app.revision.document import open_docx, require, word_bytes

PROFILE = "formal-notice"
FILES = ("套版前.docx", "套版后.docx")
ISSUER = "紫阳县人民政府办公室"
NUMBER = re.compile(r"紫政办发〔[1-9][0-9]{3}〕[1-9][0-9]*号")
DATE = re.compile(r"([1-9][0-9]{3})年([1-9][0-9]?)月([1-9][0-9]?)日")
FIRST = re.compile(r"^[一二三四五六七八九十]+、")
SECOND = re.compile(r"^（[一二三四五六七八九十]+）[^。！？]{1,48}(?:[。！？]|$)")
TITLE_BREAK = re.compile(r"认定|实施|管理|工作|方案|办法|事项|有关")
TEMPLATE_HELP = "请选择紫阳县政府办官网《政府办(图三)》转换的 DOCX 通知样张。"


def clean_fields(fields):
    result = {key: fields[key].strip() for key in ("title", "number", "addressee", "issuer", "date")}
    for key in ("body", "attachments"):
        result[key] = [line.strip() for line in fields[key].splitlines() if line.strip()]
    require(all(result[key] and len(result[key].splitlines()) == 1
                for key in ("title", "number", "addressee", "issuer", "date")) and result["body"],
            "请填写标题、文号、主送、正文、署名和成文日期。")
    require(result["issuer"] == ISSUER and NUMBER.fullmatch(result["number"]),
            "文号和发文机关署名须与紫阳县政府办样张一致，文号示例为紫政办发〔2020〕78号。")
    require(result["title"].endswith("通知"), "此样张用于通知，标题须以“通知”结尾。")
    require(result["addressee"].endswith("："), "主送机关一行须以全角冒号结尾。")
    match = DATE.fullmatch(result["date"])
    valid = False
    if match:
        try:
            date(*map(int, match.groups()))
            valid = True
        except ValueError:
            pass
    require(valid, "请填写实际成文日期，格式如 2020年8月29日。")
    require(all(ord(ch) >= 32 for value in result.values()
                for line in ([value] if isinstance(value, str) else value) for ch in line),
            "字段中含有控制字符，请清理后再提交。")
    return result


def attachment_rows(names):
    if len(names) == 1:
        return [("附件：", names[0])]
    return [("附件：" if index == 1 else "", f"{index}. {name}")
            for index, name in enumerate(names, 1)]


def title_lines(title):
    lines = []
    if title.startswith(ISSUER):
        lines.append(ISSUER)
        title = title[len(ISSUER):]
    # The sample has room for twenty 22-point characters. Leave a little room
    # for substitutions and avoid pushing just “通知” onto its own line.
    count = (len(title) + 18) // 19
    for left in range(count, 1, -1):
        low = max(len(title) - 19 * (left - 1), 5)
        high = min(19, len(title) - 5 * (left - 1))
        center = round(len(title) / left)
        breaks = [m.start() for m in TITLE_BREAK.finditer(title)
                  if low <= m.start() <= high and abs(m.start() - center) <= 4]
        cut = min(breaks, key=lambda position: abs(position - center)) if breaks else center
        lines.append(title[:cut])
        title = title[cut:]
    return "\n".join([*lines, title])


def paragraph(doc, sample, text):
    new = doc.add_paragraph()
    new._p.append(deepcopy(sample._p.pPr))
    add_text(new, sample, text)
    return new


def add_text(paragraph, sample, text, font=None):
    run = paragraph.add_run(text)
    run._r.insert(0, deepcopy(next(r._r.rPr for r in sample.runs if r._r.rPr is not None)))
    if font:
        fonts = run._r.rPr.rFonts
        for key in ("ascii", "hAnsi", "eastAsia"):
            fonts.set(qn(f"w:{key}"), font)


def apply_indent(p, *, left=0, first=0):
    ind = p._p.pPr.get_or_add_ind()
    ind.attrib.clear()
    ind.set(qn("w:left"), str(left))
    ind.set(qn("w:hanging" if first < 0 else "w:firstLine"), str(abs(first)))


def template_parts(doc):
    require(len(doc.sections) == 1 and not doc.tables and len(doc.paragraphs) == 40,
            TEMPLATE_HELP, "FORMAL_TEMPLATE_UNSUPPORTED")
    parts = doc.paragraphs
    red_lines = parts[7]._p.xpath(
        './/*[local-name()="AlternateContent"][.//*[local-name()="srgbClr" and @val="FF0000"]'
        ' and .//*[local-name()="prstGeom" and @prst="line"]]'
    )
    section = doc.sections[0]
    footer = section.footer.paragraphs[0] if not section.footer.is_linked_to_previous else None
    require(parts[3].text == f"{ISSUER}文件" and parts[3]._p.xpath('.//w:color[@w:val="FF0000"]')
            and parts[6].text.startswith("紫政办发〔2012〕") and parts[9].text == ISSUER
            and parts[13].text.strip() == "×××：" and len(red_lines) == 1
            and footer is not None and [f.text.strip() for f in footer._p.xpath(".//w:instrText")] == ["PAGE"]
            and all(parts[i]._p.pPr is not None and any(r._r.rPr is not None for r in parts[i].runs)
                    for i in (6, 7, 9, 12, 13, 16)),
            TEMPLATE_HELP, "FORMAL_TEMPLATE_UNSUPPORTED")
    return parts, red_lines[0], footer


def add_page_numbers(doc, section, footer):
    doc.settings.odd_and_even_pages_header_footer = True
    section.different_first_page_header_footer = False
    number_type = section._sectPr.find(qn("w:pgNumType"))
    if number_type is not None:
        number_type.set(qn("w:start"), "1")
    even = section.even_page_footer.paragraphs[0]
    for child in list(even._p):
        even._p.remove(child)
    even._p.extend(deepcopy(child) for child in footer._p)
    for p, side, value in ((footer, WD_ALIGN_PARAGRAPH.RIGHT, "1"), (even, WD_ALIGN_PARAGRAPH.LEFT, "2")):
        p.alignment = side
        ind = p._p.pPr.get_or_add_ind()
        ind.attrib.clear()
        ind.set(qn("w:right" if value == "1" else "w:left"), "320")
        for t in p._p.xpath(".//w:t"):
            if (t.text or "").isdecimal():
                t.text = value
    if not section.header.is_linked_to_previous:
        section.header.is_linked_to_previous = True


def render(template, fields):
    doc = open_docx(template)
    parts, red_line, footer = template_parts(doc)
    section = doc.sections[0]
    doc._body.clear_content()

    # Reserve the original height above the red heading without the sample
    # serial number, classification, urgency or their instructional callouts.
    for _ in range(3):
        paragraph(doc, parts[5], "").paragraph_format.keep_with_next = True
    doc._body._body.insert_element_before(deepcopy(parts[3]._p), "w:sectPr")
    paragraph(doc, parts[5], "").paragraph_format.keep_with_next = True
    paragraph(doc, parts[6], fields["number"]).paragraph_format.keep_with_next = True
    rule = paragraph(doc, parts[7], "")
    rule.runs[0]._r.append(deepcopy(red_line))
    rule.paragraph_format.keep_with_next = True
    paragraph(doc, parts[8], "").paragraph_format.keep_with_next = True

    heading = paragraph(doc, parts[9], title_lines(fields["title"]))
    heading.paragraph_format.keep_with_next = True
    heading.paragraph_format.keep_together = True
    paragraph(doc, parts[12], "").paragraph_format.keep_with_next = True
    paragraph(doc, parts[13], fields["addressee"]).paragraph_format.keep_with_next = True
    for line in fields["body"]:
        p = paragraph(doc, parts[16], "")
        apply_indent(p, first=640)
        p.paragraph_format.widow_control = True
        if FIRST.match(line):
            add_text(p, parts[16], line, "黑体")
            p.paragraph_format.keep_with_next = True
        elif match := SECOND.match(line):
            add_text(p, parts[16], match[0], "楷体_GB2312")
            add_text(p, parts[16], line[match.end():])
        else:
            add_text(p, parts[16], line)

    attachments = attachment_rows(fields["attachments"])
    for index, (label, name) in enumerate(attachments):
        p = paragraph(doc, parts[16], label + name)
        apply_indent(p, left=1600, first=-960 if label else 0)
        if index == 0:
            p.paragraph_format.space_before = Pt(28)
        p.paragraph_format.keep_together = True
        p.paragraph_format.keep_with_next = True
    # Keep the signature and date together with the preceding line of content.
    if not attachments:
        doc.paragraphs[-1].paragraph_format.keep_with_next = True
    signature = paragraph(doc, parts[16], fields["issuer"])
    apply_indent(signature)
    signature.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    signature.paragraph_format.right_indent = Pt(64)
    signature.paragraph_format.space_before = Pt(28)
    signature.paragraph_format.keep_with_next = True
    # The approved issuer is set in 16-point full-width characters.
    date_paragraph = paragraph(doc, parts[16], "\t" + fields["date"])
    apply_indent(date_paragraph)
    date_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    center = section.page_width - section.left_margin - section.right_margin - Pt(64 + 8 * len(ISSUER))
    date_paragraph.paragraph_format.tab_stops.add_tab_stop(center, WD_TAB_ALIGNMENT.CENTER)
    add_page_numbers(doc, section, footer)

    before = Document()
    before.add_paragraph(fields["title"], style="Title")
    remaining = [fields["addressee"], *fields["body"],
                *(label + name for label, name in attachments), fields["issuer"], fields["date"]]
    for line in [fields["number"], *remaining]:
        before.add_paragraph(line)
    before_bytes, after_bytes = word_bytes(before), word_bytes(doc)
    orders = ([fields["title"], fields["number"]], [f"{ISSUER}文件", fields["number"], fields["title"]])
    for content, prefix in zip((before_bytes, after_bytes), orders):
        paragraphs = [p.text.replace("\n", "").removeprefix("\t")
                      for p in open_docx(content).paragraphs if p.text]
        require(paragraphs == [*prefix, *remaining],
                "套版复核未通过，正文或字段与提交内容不一致。", "FORMAL_CONTENT_CHANGED")
    return before_bytes, after_bytes
