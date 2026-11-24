"""Locate Word review marks and accept explicitly authorized inline text changes."""
import re
from collections import Counter, defaultdict
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

from docx.oxml.ns import qn
from lxml import etree

from app.materials import MAX_BYTES
from app.revision import opinion_template
from app.revision.document import RevisionIssue, check_word_parts, open_docx, require, write_word_patch
from app.revision.opinions import publish

PROFILE = "native-word-review-v1"
WORD_SCOPE = r"(?:(?:这份|该份|这|该)?(?:Word(?:文档|文件)?|文档|文件)(?:里|中|内)(?:已有的|原有的)?)?"
ALL_BODY_TEXT = r"(?:全部|所有)(?:的)?正文文字修订"
ACCEPT = re.compile(
    rf"(?:请|麻烦)?(?:帮我)?(?:接受{WORD_SCOPE}{ALL_BODY_TEXT}|把{WORD_SCOPE}{ALL_BODY_TEXT}(?:都)?接受)",
    re.IGNORECASE,
)
REVIEW_REQUEST = re.compile(
    rf"批注|修订痕迹|修订标记|接受{WORD_SCOPE}(?:全部|所有)?正文(?:文字)?修订|"
    rf"{ALL_BODY_TEXT}(?:都)?接受|(?:接受|拒绝)(?:全部|所有)修订", re.IGNORECASE,
)
PRESERVED = r"(?:批注(?:和|及|、)格式修订|批注|格式修订)"
SAFE_FOLLOWUP = re.compile(
    rf"(?:保留{PRESERVED}|{PRESERVED}(?:都)?(?:保留|不动)|"
    r"(?:请)?(?:给我|提供|输出|返回)(?:改后|改完|修订后)的Word(?:文档|文件)?"
    r"(?:和|及|、)(?:逐条)?核稿单)",
    re.IGNORECASE,
)
ID = qn("w:id")
P = qn("w:p")
TEXT = {qn("w:t"), qn("w:delText"), qn("w:tab"), qn("w:br"), qn("w:cr")}
ANCHORS = {qn("w:" + name): name for name in ("commentRangeStart", "commentRangeEnd", "commentReference")}
REV = {qn("w:" + name): name for name in (
    "ins", "del", "moveFrom", "moveTo", "moveFromRangeStart", "moveFromRangeEnd",
    "moveToRangeStart", "moveToRangeEnd", "pPrChange", "rPrChange", "sectPrChange",
    "tblPrChange", "tblGridChange", "tblPrExChange", "trPrChange", "tcPrChange", "numberingChange",
    "cellIns", "cellDel", "cellMerge", "customXmlInsRangeStart", "customXmlInsRangeEnd",
    "customXmlDelRangeStart", "customXmlDelRangeEnd", "customXmlMoveFromRangeStart",
    "customXmlMoveFromRangeEnd", "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd",
    "conflictIns", "conflictDel",
)}
INSERT, DELETE = qn("w:ins"), qn("w:del")


def matches_request(request):
    return bool(REVIEW_REQUEST.search(re.sub(r"[^\S\r\n]+", "", request)))


def authorizes_acceptance(request):
    # Require an affirmative command and recognize every later clause before changing the Word.
    parts = re.split(r"[，,；;。！.!\r\n]+", request.strip())
    clauses = [re.sub(r"\s+", "", part) for part in parts if part.strip()]
    return bool(clauses and ACCEPT.fullmatch(clauses[0])
                and all(SAFE_FOLLOWUP.fullmatch(clause) for clause in clauses[1:]))


def text_of(node):
    if node.tag == qn("w:tab"):
        return "\t"
    if node.tag in (qn("w:br"), qn("w:cr")):
        return "\n"
    return node.text or ""


def paragraph_of(node):
    return next(node.iterancestors(P), None)


def table_locations(root):
    cells = {}
    table, row, cell = (qn("w:" + name) for name in ("tbl", "tr", "tc"))
    for t, tbl in enumerate(root.iter(table), 1):
        for r, tr in enumerate(tbl.iterchildren(row), 1):
            for c, tc in enumerate(tr.iterchildren(cell), 1):
                cells[tc] = f"第 {t} 张表，第 {r} 行第 {c} 格"
    return {i: cells[tc] for i, p in enumerate(root.iter(P), 1)
            if (tc := next(p.iterancestors(cell), None)) in cells}


def location(first, last=None, cells=None):
    if not first:
        return "正文结构"
    if last and first != last:
        return f"正文第 {first} 至 {last} 段"
    detail = cells.get(first) if cells else None
    return f"正文第 {first} 段" + (f"（{detail}）" if detail else "")


def review_marks(root):
    """Word IDs, including overlapping ranges, are independent of visible numbering."""
    paragraphs = {p: i for i, p in enumerate(root.iter(P), 1)}
    offsets = defaultdict(lambda: defaultdict(list))
    pieces = []
    paragraph = None
    for order, node in enumerate(root.iter()):
        if node.tag not in ANCHORS and node.tag not in TEXT:
            continue
        number = paragraphs.get(paragraph_of(node))
        if number != paragraph:
            if pieces:
                pieces.append("\n")
            paragraph = number
        if node.tag in ANCHORS:
            offsets[node.get(ID)][ANCHORS[node.tag]].append((len(pieces), number, order))
        else:
            pieces.append(text_of(node))
    return offsets, pieces, paragraphs


def anchor_of(events, pieces, cells):
    starts, ends, refs = (events.get(key, ()) for key in ("commentRangeStart", "commentRangeEnd", "commentReference"))
    if len(refs) == 1 and not starts and not ends:
        return "点批注", location(refs[0][1], cells=cells), ""
    if len(starts) == len(ends) == len(refs) == 1 and starts[0][2] < ends[0][2] < refs[0][2]:
        a, first, _ = starts[0]
        b, last, _ = ends[0]
        excerpt = "".join(pieces[a:b])
        return ("已定位" if excerpt.strip() else "范围无可读文字"), location(first, last, cells), excerpt
    return "锚点缺失或不唯一", "请在 Word 审阅面板定位", ""


def is_inline(node):
    kind = node.tag
    allowed_text = qn("w:t") if kind == INSERT else qn("w:delText")
    return (kind in (INSERT, DELETE) and node.getparent().tag == P and len(node) > 0
            and not any(parent.tag in REV or parent.tag == qn("w:txbxContent") for parent in node.iterancestors())
            and all(run.tag == qn("w:r") and all(
                item.tag in {qn("w:rPr"), allowed_text, qn("w:tab")}
                and not any(child.tag in REV for child in item.iterdescendants())
                for item in run
            ) for run in node))


def review(request, path, output, *, template=None):
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, f"{path.name} 超过 4 MB，请提供相关部分。")
    doc = open_docx(raw, allow_local_template=True)
    body = doc._element.body
    marks, pieces, paragraphs = review_marks(body)
    cells = table_locations(body)
    comments = defaultdict(list)
    outside = Counter()
    with ZipFile(BytesIO(raw)) as archive:
        for name in archive.namelist():
            if not (name.startswith("word/") and name.endswith(".xml")) or name == "word/document.xml":
                continue
            root = etree.fromstring(archive.read(name), parser=etree.XMLParser(resolve_entities=False, no_network=True))
            if root.tag == qn("w:comments"):
                for comment in root.iterchildren(qn("w:comment")):
                    comments[comment.get(ID)].append("\n".join(
                        "".join(text_of(n) for n in p.iter() if n.tag in TEXT) for p in comment.iter(P)
                    ))
            if root.tag in {qn("w:" + s) for s in ("hdr", "ftr", "footnotes", "endnotes", "comments", "glossaryDocument")}:
                for node in root.iter():
                    if node.tag in REV:
                        outside[name] += 1
    changes = [node for node in body.iter() if node.tag in REV]
    require(comments or marks or changes or outside, "原稿没有可读的 Word 原生批注或修订标记；颜色、下划线或‘修订稿’文件名不算。")
    accept = authorizes_acceptance(request)
    rows = []
    applied = []
    for node in changes:
        inline = is_inline(node)
        phrase = "".join(text_of(n) for n in node.iter() if n.tag in TEXT) if node.tag in (INSERT, DELETE) else ""
        place = location(paragraphs.get(paragraph_of(node)), cells=cells)
        if inline and accept:
            status, note = "已接受", "按用户指令接受原有文字修订。"
            applied.append(node)
        elif inline:
            status, note = "未改", "需明确授权；可单独提交「接受全部正文文字修订」。"
        else:
            status, note = "保留原修订", "此修订涉及结构、格式、移动或复杂范围，请在 Word 审阅。"
        rows.append((f"{REV[node.tag]}:{node.get(ID, '?')}", status, place,
                     phrase if node.tag == DELETE else "", phrase if node.tag == INSERT else "",
                     "", "原稿原生修订", note))
    applied_paragraphs = {}
    if template is not None:
        accepted_nodes = set(applied)
        for p in dict.fromkeys(paragraph_of(node) for node in applied):
            applied_paragraphs[p] = "".join(
                text_of(part) for part in p.iter() if part.tag in TEXT
                and not any((a.tag == INSERT and a in accepted_nodes) or
                            (a.tag == DELETE and a not in accepted_nodes) for a in part.iterancestors())
            )
    for node in applied:
        parent = node.getparent()
        if node.tag == INSERT:
            for child in list(node):
                node.addprevious(child)
        parent.remove(node)
    revised = write_word_patch(raw, doc) if applied else raw
    if applied:
        check_word_parts(raw, revised)
        fresh = open_docx(revised, allow_local_template=True)
        require(etree.tostring(fresh._element.body) == etree.tostring(body),
                "改后 Word 正文与写入结果不一致。", "RENDER_MISMATCH")
        after_marks, after_pieces, _ = review_marks(fresh._element.body)
    else:
        after_marks, after_pieces = marks, pieces
    comment_rows = []
    located = removed = 0
    for key in dict.fromkeys((*comments, *marks)):
        state, place, before = anchor_of(marks[key], pieces, cells)
        after_state, _, after = anchor_of(after_marks[key], after_pieces, cells)
        texts = comments.get(key, [])
        if key is None or len(texts) != 1:
            state = "批注正文缺失或编号重复"
        located += state == "已定位"
        if state == "已定位" and after_state == "范围无可读文字":
            removed += 1
            state = "已定位，所指文字已按原修订删除"
            note = "批注仍在原位置。经办人确认是否解决批注；原句见改前。"
        else:
            note = f"批注保留，经办人确认处理。改后锚点：{after_state}。"
        comment_rows.append((f"批注:{key if key is not None else '?'}", state, place, before, after, "",
                             "\n".join(texts), note))
    for name, count in outside.items():
        rows.append((name, "保留原修订", name, "", "", "", f"{count} 个原生标记",
                     "正文之外的修订保留，由经办人在 Word 审阅。"))
    if applied:
        message = f"正文已改，已接受 {len(applied)} 处正文文字修订。"
    elif accept or not any(is_inline(node) for node in changes):
        message = "正文未改，原稿没有可安全接受的正文文字修订。"
    else:
        message = "正文未改，未识别到明确且无冲突的正文文字修订接受指令。"
    receipt = {
        "profile": PROFILE, "status": "revision" if applied else "review_only", "accepted": False,
        "message": message,
        "parent_name": path.name, "parent_sha256": sha256(raw).hexdigest(),
        "revision_id": sha256(revised).hexdigest(), "body_revision_marks": len(changes),
        "applied": len(applied), "body_pending_marks": len(changes) - len(applied),
        "outside_body_marks": sum(outside.values()), "comments": len(comments), "comments_located": located,
        "comments_target_removed": removed,
    }
    filled = None
    if template is not None:
        try:
            require(comment_rows or applied_paragraphs, "没有可填入汇总表的批注或已接受的文字修订。")
            template_rows = [[f"{row[0]}｜{row[2]}", f"原文：{row[3]}\n原批注：{row[6]}", row[4], "",
                              "待核，未判定采纳", ""]
                             for row in comment_rows]
            for p, previous in applied_paragraphs.items():
                current = "".join(text_of(part) for part in p.iter()
                                  if part.tag in TEXT and not any(a.tag == DELETE for a in part.iterancestors()))
                template_rows.append([location(paragraphs[p], cells=cells), previous, current, "",
                                      "已接受文字修订", ""])
            template_raw, template_doc = opinion_template.load_template(template, len(template_rows))
            filled = opinion_template.fill_template(template_raw, template_doc, template_rows, path.name)
        except RevisionIssue as error:
            receipt["template_note"] = f"未套版：{error}"
        else:
            receipt["template_note"] = f"意见汇总表已填 {len(template_rows)} 条；正文沿用原稿版式。"
            receipt["template_sha256"] = sha256(template_raw).hexdigest()
    publish(output, raw, revised, comment_rows + rows, receipt,
            basis_note="正文按物理段落编号，包含表格单元格。批注保留待确认；仅在明确授权后接受可安全处理的正文文字修订。"
                       + (receipt.get("template_note") or ""),
            filled_template=filled,
            review_intro=message + "改前及改后来自同一份 Word 的原生标记和批注范围，待业务核稿。")
    return receipt
