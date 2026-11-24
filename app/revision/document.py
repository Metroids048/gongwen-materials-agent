"""Read, render and verify the bounded work-plan date revision."""

import re
from copy import deepcopy
from io import BytesIO
from urllib.parse import unquote
from zipfile import BadZipFile, ZipFile

from docx import Document
from docx.enum.section import WD_ORIENTATION, WD_SECTION_START
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from lxml import etree


TEMPLATE_VERSION = "public-txt-original-table-v1"
DATE = r"[0-9]{4}年(?:1[0-2]|[1-9])月底"
HEADING = re.compile(r"^(?:[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|[0-9]+[.．、])")


class RevisionIssue(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, message, code="NEEDS_INPUT"):
    if not condition:
        raise RevisionIssue(code, message)


def open_docx(content, *, allow_local_template=False):
    try:
        with ZipFile(BytesIO(content)) as archive:
            parts = archive.infolist()
            require(len(parts) <= 512 and sum(p.file_size for p in parts) <= 15 * 1024**2,
                    "Word 展开大小超出试跑上限。")
            require(len({p.filename for p in parts}) == len(parts),
                    "Word 有重复文件部件，请提供正常保存的副本。", "SOURCE_UNREADABLE")
            require(not any("vbaproject" in p.filename.lower() or "/embeddings/" in p.filename
                            for p in parts), "试跑仅支持无宏、无嵌入对象的 Word。")
            for part in parts:
                if part.filename.endswith(".rels"):
                    relationships = etree.fromstring(
                        archive.read(part), parser=etree.XMLParser(resolve_entities=False, no_network=True),
                    )
                    # Original review can read a stale drive-local Word template pointer without following it.
                    require(all(r.get("TargetMode") != "External" or (
                        allow_local_template and part.filename == "word/_rels/settings.xml.rels"
                        and r.get("Type") ==
                        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate"
                        and re.fullmatch(r"file:///[A-Za-z]:[/\\][^/\\][^\x00-\x1f]*",
                                         unquote(r.get("Target", "")), flags=re.IGNORECASE)
                    ) for r in relationships),
                            "试跑仅支持无外部链接的 Word。")
        return Document(BytesIO(content))
    except (BadZipFile, KeyError, ValueError, etree.LxmlError) as error:
        raise RevisionIssue("SOURCE_UNREADABLE", "无法读取 Word 文件，请核对原件。") from error


def table_rows(table):
    # Use physical cells so a vertical merge does not turn one source cell into several edits.
    return [["\n".join(p.text for p in cell.p_lst) for cell in row._tr.tc_lst]
            for row in table.rows]


def read_body(content):
    try:
        body = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise RevisionIssue("SOURCE_UNREADABLE", "原稿需为 UTF-8 纯文本。") from error
    paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n", body.replace("\r\n", "\n")) if p.strip()]
    require(len(paragraphs) >= 2, "正文至少需要标题和一段内容，请用空行分隔。")
    return paragraphs


def read_sources(sources):
    paragraphs = read_body(sources["body"])
    division, attachment = (open_docx(sources[role]) for role in ("table", "reference"))
    require(len(division.tables) == 1 and len(division.sections) == 1 and attachment.tables,
            "请上传含一张任务分工表的 Word，以及含表格的原附件。")
    require(not division.tables[0]._tbl.xpath(".//w:tbl"), "试跑暂不支持嵌套任务表。")
    return paragraphs, table_rows(division.tables[0]), [table_rows(t) for t in attachment.tables]


def heading_level(text):
    match = HEADING.match(text)
    if not match:
        return 0
    if text.startswith("（"):
        return 2
    return 3 if text[0].isascii() else 1


def locked_indices(body, title, request):
    titles = [i for i, value in enumerate(body)
              if value == title and heading_level(value)]
    require(len(titles) == 1 and title in request, "请按原稿写明唯一需要锁定的标题。")
    # The narrow slice supports a single explicit named lock. A second named lock needs clarification.
    mentioned = {value for value in body if heading_level(value) and value in request}
    parents = {body[i] for i in range(titles[0]) if heading_level(body[i]) == 1}
    if heading_level(title) == 2:
        previous = next((v for v in reversed(body[:titles[0]]) if heading_level(v) == 1), None)
        parents.intersection_update({previous})
    else:
        parents.clear()
    require(mentioned <= parents | {title}, "试跑只处理一个明确锁定的小节，请确认保护范围。")
    start = titles[0]
    level = heading_level(title)
    end = next((i for i in range(start + 1, len(body)) if 0 < heading_level(body[i]) <= level), len(body))
    return list(range(start, end))


def build_word(body, table_file):
    doc = open_docx(table_file)
    original_layout = deepcopy(doc.sections[0]._sectPr)
    anchor = doc.paragraphs[0] if doc.paragraphs else None
    require(anchor is not None and anchor._p.getprevious() is None,
            "任务分工表前需要原附件标题。")
    for index, value in enumerate(body):
        paragraph = anchor.insert_paragraph_before()
        paragraph.paragraph_format.space_after = Pt(6)
        paragraph.paragraph_format.line_spacing = 1.45
        level = heading_level(value)
        run = paragraph.add_run(value)
        run.font.name = "Songti SC"
        run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "宋体")
        run.font.size = Pt(18 if index == 0 else 12)
        run.bold = index == 0 or level in (1, 2)
        if index == 0:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif level:
            paragraph.paragraph_format.keep_with_next = True
        else:
            paragraph.paragraph_format.first_line_indent = Pt(24)

    section_break = anchor.insert_paragraph_before()
    portrait = deepcopy(original_layout)
    section_break._p.set_sectPr(portrait)
    first = doc.sections[0]
    first.orientation = WD_ORIENTATION.PORTRAIT
    first.page_width, first.page_height = Cm(21), Cm(29.7)
    first.left_margin = first.right_margin = Cm(2.6)
    first.top_margin = first.bottom_margin = Cm(2.4)
    doc.sections[-1].start_type = WD_SECTION_START.NEW_PAGE
    return doc


def word_bytes(document):
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def write_word_patch(parent, document):
    """Replace only the main XML part; keep original attachments and layout resources."""
    require(str(document.part.partname) == "/word/document.xml", "暂不支持此 Word 的正文部件位置。")
    output = BytesIO()
    with ZipFile(BytesIO(parent)) as before, ZipFile(output, "w") as after:
        require(not any(name.lower().startswith("_xmlsignatures/") for name in before.namelist()),
                "已签名的 Word 需要另存为经授权的可编辑稿后再修改。")
        protection = document.settings._element.find(qn("w:documentProtection"))
        require(protection is None or protection.get(qn("w:enforcement")) in ("0", "false", "off"),
                "原稿启用了编辑保护，请提供获准修改的 Word 副本。")
        xml = etree.tostring(document._element, encoding="UTF-8", xml_declaration=True, standalone=True)
        for part in before.infolist():
            content = xml if part.filename == "word/document.xml" else before.read(part)
            after.writestr(part, content)
    return output.getvalue()


def check_word_parts(parent, revised):
    """Verify the untouched package, including styles, headers, footers and drawings."""
    ignored = {"word/document.xml"}
    with ZipFile(BytesIO(parent)) as before, ZipFile(BytesIO(revised)) as after:
        names = set(before.namelist()) - ignored
        require(names == set(after.namelist()) - ignored
                and all(before.read(name) == after.read(name) for name in names),
                "Word 样式、页眉页脚或其他部件有范围外变化，请保留原版式后重试。", "RENDER_MISMATCH")


def current_view(doc, original_body, request, saved_view):
    paragraphs = doc.paragraphs[:len(original_body)]
    body = [p.text for p in paragraphs]
    dates = set(re.findall(DATE, request))
    headings = [p for p in original_body if heading_level(p) and p in request]
    lock = None
    if saved_view:
        lock = saved_view["lock_indices"]
    elif headings:
        try:
            lock = locked_indices(original_body, max(headings, key=heading_level), request)
        except RevisionIssue:
            pass
    editable = [i for i, p in enumerate(paragraphs) if lock is not None and i > 0 and i not in lock
                and not heading_level(original_body[i]) and len(p.runs) == 1
                and not any(date in p.text for date in dates)]
    rows = table_rows(doc.tables[0])
    header = rows[0]
    measures = header.index("具体措施")
    matches = [row for row in rows[1:] if measures < len(row) and any(date in row[measures] for date in dates)]
    return {"body": body, "lock_indices": lock or [], "editable_indices": editable,
            "table_header": header, "table_row": matches[0] if len(matches) == 1 else [],
            "changes": saved_view["changes"] if saved_view else []}


def apply_block_edits(word, original_body, request, saved_view, block_edits):
    doc = open_docx(word)
    view = current_view(doc, original_body, request, saved_view)
    changes = []
    seen = set()
    paragraphs = doc.paragraphs
    dates = set(re.findall(DATE, request))
    for edit in block_edits:
        index, text = edit["paragraph"] - 1, edit["text"]
        require(index not in view["lock_indices"], "锁定段落不能手改。", "LOCKED_SCOPE_CONFLICT")
        require(index in view["editable_indices"] and index not in seen,
                "请选择不同的可编辑正文段落。", "EDIT_SCOPE_CONFLICT")
        require(text.strip() and not re.search(r"[\x00-\x1f\x7f\u2028\u2029\ufffe\uffff]", text)
                and not heading_level(text.lstrip()) and not any(date in text for date in dates),
                "请输入单段正文，标题和本轮联动日期须通过原有流程修改。", "EDIT_SCOPE_CONFLICT")
        seen.add(index)
        if text != view["body"][index]:
            changes.append({"location": f"正文第 {index + 1} 段", "before": view["body"][index], "after": text})
            paragraphs[index].runs[0].text = text
            view["body"][index] = text
    require(changes, "正文没有变化。", "NO_CHANGES")
    revised = write_word_patch(word, doc)
    rendered = open_docx(revised)
    base = open_docx(word)
    require(all(base.paragraphs[i]._p.xml == rendered.paragraphs[i]._p.xml for i in view["lock_indices"]),
            "锁定段落在 Word 中发生变化。", "LOCKED_SCOPE_CONFLICT")
    require([p.text for p in rendered.paragraphs[:len(original_body)]] == view["body"],
            "Word 内容与手改稿不一致。", "RENDER_MISMATCH")
    for index in seen:
        rendered.paragraphs[index].runs[0].text = base.paragraphs[index].runs[0].text
    require(rendered._element.xml == base._element.xml,
            "Word 出现范围外变化。", "RENDER_MISMATCH")
    check_word_parts(word, revised)
    view["changes"] = changes
    return revised, view


def prepare_revision(original_word, body, rows, original_request, selection):
    if selection["question"]:
        raise RevisionIssue("NEEDS_INPUT", selection["question"])
    old, new = selection["old_date"], selection["new_date"]
    require(old != new and all(re.fullmatch(DATE, v) and v in original_request for v in (old, new)),
            "请在修改意见中明确旧日期和新日期，试跑支持带年份的月底日期。")
    lock = locked_indices(body, selection["lock_heading"], original_request)
    p, r, c = (selection[key] - 1 for key in ("body_paragraph", "table_row", "table_column"))
    require(0 <= p < len(body) and 0 < r < len(rows) and 0 <= c < len(rows[r]),
            "候选定位无法与正文或任务表对应，请确认改动位置。")
    require(p not in lock, f"正文第 {p + 1} 段位于锁定范围内。", "LOCKED_SCOPE_CONFLICT")
    if any(old in body[i] for i in lock):
        raise RevisionIssue("LOCKED_SCOPE_CONFLICT",
                            f"锁定小节「{body[lock[0]]}」也含同一旧日期，请先确认是否允许同步。")
    require(body[p].count(old) == rows[r][c].count(old) == 1 and rows[0][c] == "具体措施",
            "旧日期须分别定位到正文和任务表的具体措施，独立完成时限保持原样。")

    base = open_docx(original_word)
    candidate = open_docx(original_word)
    body_targets = [(i, run) for i, run in enumerate(candidate.paragraphs[p].runs) if old in run.text]
    require(len(body_targets) == 1, "正文日期跨格式片段，无法安全改写。")
    body_run_index, body_run = body_targets[0]
    original_body_text = body_run.text
    body_run.text = original_body_text.replace(old, new)
    cell = candidate.tables[0].rows[r]._tr.tc_lst[c]
    # Only alter the matched run in the original attachment. Split-run edits need explicit support.
    targets = [(pi, ri) for pi, paragraph in enumerate(cell.p_lst)
               for ri, run in enumerate(paragraph.r_lst) if old in run.text]
    require(len(targets) == 1, "任务表日期跨格式片段，试跑无法安全改写。")
    pi, ri = targets[0]
    run = cell.p_lst[pi].r_lst[ri]
    original_text = run.text
    run.text = original_text.replace(old, new)
    revised_word = write_word_patch(original_word, candidate)
    rendered = open_docx(revised_word)
    rendered_body = [p.text for p in rendered.paragraphs[:len(body)]]
    rendered_rows = table_rows(rendered.tables[0])
    require(all(base.paragraphs[i]._p.xml == rendered.paragraphs[i]._p.xml for i in lock),
            "锁定段落在 Word 渲染后发生变化。", "LOCKED_SCOPE_CONFLICT")
    expected_body, expected_rows = body.copy(), deepcopy(rows)
    expected_body[p] = body[p].replace(old, new)
    expected_rows[r][c] = rows[r][c].replace(old, new)
    require(rendered_body == expected_body and rendered_rows == expected_rows,
            "复读 Word 时发现范围外变化，未提交候选。", "RENDER_MISMATCH")
    # Undo both date edits in the check copy to verify the rest of the parent is intact.
    rendered.paragraphs[p].runs[body_run_index].text = original_body_text
    rendered_cell = rendered.tables[0].rows[r]._tr.tc_lst[c]
    rendered_cell.p_lst[pi].r_lst[ri].text = original_text
    require(rendered._element.xml == base._element.xml,
            "正文或任务分工表的结构与样式发生额外变化。", "RENDER_MISMATCH")
    check_word_parts(original_word, revised_word)
    changes = [
        {"location": f"正文第 {p + 1} 段", "before": body[p], "after": rendered_body[p]},
        {"location": f"任务分工表第 {rows[r][0]} 项 · {rows[0][c]}",
         "before": rows[r][c], "after": rendered_rows[r][c]},
    ]
    view = {
        "body": rendered_body, "table_header": rows[0], "table_row": rendered_rows[r],
        "lock_indices": lock, "changes": changes,
        "date": {"before": old, "after": new, "decision_source": "原始修改意见"},
    }
    return revised_word, view
