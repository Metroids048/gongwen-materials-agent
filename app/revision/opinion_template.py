"""Fill the verified six-column opinion form without recreating its layout."""

from copy import deepcopy

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from docx.text.paragraph import Paragraph

from app.revision.document import check_word_parts, open_docx, require, table_rows, write_word_patch


NAME = "意见汇总处理表.docx"
MAX_ROWS = 200
HEADERS = [
    ["章条\n编号", "专家意见", "专家\n（姓名、职称、单位）", "修改情况\n(采纳/不采纳)", "不采纳理由"],
    ["", "原稿", "改为", "", "", ""],
]


def load_template(path, count):
    with path.open("rb") as handle:
        raw = handle.read(4 * 1024**2 + 1)
    require(len(raw) <= 4 * 1024**2, "空白模板超过 4 MiB。")
    doc = open_docx(raw)
    require(len(doc.sections) == len(doc.tables) == 1 and doc.sections[0].page_width > doc.sections[0].page_height,
            "当前套版需要单节横向、单表的意见汇总空白 Word。")
    table = doc.tables[0]
    rows = table_rows(table)
    require(rows[:2] == HEADERS and not table._tbl.xpath(
        ".//w:tbl | .//w:ins | .//w:del | .//w:sdt | .//w:fldChar | .//w:commentRangeStart"
    ), "当前套版仅支持已核对的辽宁六列空白模板。")
    slots = table.rows[2:]
    require(all(len(row._tr.tc_lst) == 6 and all(
        len(cell.p_lst) == 1 and not cell.p_lst[0].xpath("./*[not(self::w:pPr)]")
        and not cell.xpath("./w:tcPr/w:vMerge | ./w:tcPr/w:gridSpan")
        for cell in row._tr.tc_lst
    ) for row in slots), "模板数据区须为空白普通单元格。")
    require(slots and 0 < count <= MAX_ROWS,
            f"模板须有空白数据行，且每次最多填 {MAX_ROWS} 条；本轮有 {count} 条。")
    label = [p for p in doc.paragraphs if p.text.strip() == "附件2"]
    require(len(label) == 1 and doc.paragraphs[0]._p is label[0]._p and label[0].runs,
            "模板缺少可定位的附件标签。")
    return raw, doc


def fill_template(raw, doc, rows, parent_name):
    table = doc.tables[0]
    for header in table.rows[:2]:
        props = header._tr.get_or_add_trPr()
        if props.find(qn("w:tblHeader")) is None:
            props.append(OxmlElement("w:tblHeader"))
    slots = table.rows[2:]
    for _ in range(len(slots), len(rows)):
        extra = deepcopy(slots[-1]._tr)
        for element in extra.iter():
            for attr in (qn("w14:paraId"), qn("w14:textId")):
                element.attrib.pop(attr, None)
        table._tbl.append(extra)
    slots = table.rows[2:]
    for row, values in zip(slots, rows):
        for column, (cell, value) in enumerate(zip(row._tr.tc_lst, values, strict=True)):
            if value:
                paragraph = Paragraph(cell.p_lst[0], doc)
                props = paragraph._p.get_or_add_pPr()
                # WPS can ignore the empty paragraph's font when a run is added.
                run = paragraph.add_run(value)
                mark_format = props.find(qn("w:rPr"))
                if mark_format is not None:
                    run._r.insert(0, deepcopy(mark_format))
                paragraph.paragraph_format.line_spacing = Pt(14)
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(0)
                snap = props.find(qn("w:snapToGrid"))
                if snap is None:
                    snap = OxmlElement("w:snapToGrid")
                    props.insert_element_before(snap, "w:spacing")
                snap.set(qn("w:val"), "0")
                if column in (1, 2):
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for row in slots[len(rows):]:
        table._tbl.remove(row._tr)
    label = f"套版试填｜{parent_name.removesuffix('.docx')}"
    first = doc.paragraphs[0]
    first.runs[0].text = label
    for run in first.runs[1:]:
        run.text = ""
    filled = write_word_patch(raw, doc)
    check_word_parts(raw, filled)
    reread = open_docx(filled)
    require(reread.paragraphs[0].text == label and table_rows(reread.tables[0]) == HEADERS + rows,
            "套版 Word 复读与逐条核稿记录不一致。", "RENDER_MISMATCH")
    return filled
