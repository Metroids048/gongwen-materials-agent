"""Continue a public work-plan revision from an immutable local delivery."""

import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from docx.text.paragraph import Paragraph

from app.revision.document import (
    apply_block_edits, build_word, open_docx, prepare_revision,
    read_sources, require, table_rows, word_bytes,
)
from app.revision.model import pick_date, read_rules
from app.revision.public_inputs import approved_sources

WORD = "正文.docx"
RECEIPT = "运行回执.json"


def digest(content):
    return sha256(content).hexdigest()


def public_files(root, paths):
    roles = {value: role for role, value in approved_sources(root).items()}
    sources, names, extra = {}, {}, []
    for path in paths:
        require(path.stat().st_size <= 2_000_000, f"文件超出试跑上限：{path.name}")
        content = path.read_bytes()
        role = roles.get(digest(content))
        if role and role not in sources:
            sources[role], names[role] = content, path.name
        else:
            extra.append((path, content))
    require(sources.keys() == {"body", "table", "reference"},
            "当前只支持南阳公开样本。请附上官网正文 TXT、任务分工表和项目清单原件。")
    return sources, names, extra


def import_manual(parent, supplied, body, previous):
    base, edited = open_docx(parent), open_docx(supplied)
    blocks = ".//w:p | .//w:tbl | .//w:tr | .//w:tc | .//w:sectPr"
    unsupported = (
        ".//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo | .//w:fldSimple | .//w:fldChar | "
        ".//w:sdt | .//w:drawing | .//w:pict | .//w:object | .//w:altChunk | .//w:commentRangeStart | "
        ".//w:footnoteReference | .//w:endnoteReference | .//w:headerReference | .//w:footerReference"
    )
    require(not edited._element.xpath(unsupported),
            "手改副本含修订、批注或其他复杂内容，请提交只改正文文字的副本。", "EDIT_SCOPE_CONFLICT")
    require([(node.tag, node.getparent().tag) for node in base._element.xpath(blocks)]
            == [(node.tag, node.getparent().tag) for node in edited._element.xpath(blocks)],
            "手改副本的段落、分节或表格结构有变化。", "EDIT_SCOPE_CONFLICT")
    edits = [{"paragraph": i, "text": right.text}
             for i, (left, right) in enumerate(zip(base.paragraphs, edited.paragraphs, strict=True), 1)
             if left.text != right.text]
    require(len(edits) <= 1, "手改副本一次只接收一处普通正文段内文字修改。", "EDIT_SCOPE_CONFLICT")
    if edits:
        parent, view = apply_block_edits(parent, body, previous["original_request"],
                                         previous["view"], edits)
    else:
        view = {"changes": []}
    # WPS can rewrite styles and split runs on save. The previous delivery remains the layout master.
    canonical = open_docx(parent)
    require([Paragraph(p, canonical).text for p in canonical._element.xpath(".//w:p")]
            == [Paragraph(p, edited).text for p in edited._element.xpath(".//w:p")],
            "手改副本还有表格或其他范围外的文字修改。", "EDIT_SCOPE_CONFLICT")
    return parent, view["changes"]


def revise(root, model, request, files, output, *, continuation=None, intake=None, select=pick_date):
    sources, names, extra = public_files(root, files)
    original_body, _, reference = read_sources(sources)
    hashes = {role: digest(content) for role, content in sources.items()}
    previous_id, hand_edits = None, []
    if continuation is None:
        require(not extra, "首次改稿请只附三份公开原件。")
        parent = word_bytes(build_word(original_body, sources["table"]))
    else:
        previous = json.loads((continuation / RECEIPT).read_text(encoding="utf-8"))
        parent = (continuation / WORD).read_bytes()
        previous_id = digest(parent)
        require(previous.get("status") == "revision" and previous.get("revision_id") == previous_id
                and previous.get("source_hashes") == hashes,
                "上一轮交付与原件或回执不匹配。请保留交付原件，另附手改副本。", "STALE_PARENT")
        require(len(extra) <= 1 and all(p.suffix.lower() == ".docx" for p, _ in extra),
                "续改只接收一份可选的手改 Word 副本。")
        if extra:
            parent, hand_edits = import_manual(parent, extra[0][1], original_body, previous)

    current = open_docx(parent)
    body = [p.text for p in current.paragraphs[:len(original_body)]]
    rows = table_rows(current.tables[0])
    # A continuing instruction may omit the existing lock. Retain it as an inherited constraint.
    instruction = request
    if continuation is not None:
        lock_title = body[previous["view"]["lock_indices"][0]]
        if lock_title not in instruction:
            instruction += f"\n上一版沿用的保护约束：锁定「{lock_title}」。"
    selection, metadata = select(model, read_rules(root), instruction, body, rows, reference)
    word, view = prepare_revision(parent, body, rows, instruction, selection)
    if continuation is not None:
        require(view["lock_indices"] == previous["view"]["lock_indices"],
                "新修改意见与上一版锁定范围不一致，未发布新稿。", "LOCKED_SCOPE_CONFLICT")
    result = {
        "status": "revision", "profile": "plan-revise", "original_request": request,
        "effective_request": instruction, "source_names": names, "source_hashes": hashes,
        "previous_delivery_revision_id": previous_id, "parent_revision_id": digest(parent),
        "revision_id": digest(word), "hand_edits": hand_edits, "view": view,
        "selection": selection, "model": metadata, "intake_model": intake,
        "format": {"body_source": "官网 TXT 转为可编辑正文，无原发文版面", "attachment": "保留任务表版式",
                   "verification": "Word 复读，撤销两处日期改动后正文 XML 与其他包部件一致"},
    }
    write_revision(output, word, parent, result, sources["reference"], names["reference"])


def write_revision(output, word, parent, receipt, reference, reference_name):
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".gongwen-", dir=output.parent) as temp:
        folder = Path(temp) / "result"
        folder.mkdir()
        (folder / WORD).write_bytes(word)
        (folder / "本轮父版.docx").write_bytes(parent)
        (folder / f"原附件-{reference_name}").write_bytes(reference)
        view = receipt["view"]
        paragraphs = "\n\n".join(view["body"])
        doc = open_docx(word)
        table = "\n".join("｜".join(row) for row in table_rows(doc.tables[0]))
        (folder / "正文.txt").write_text(paragraphs + "\n\n任务分工表\n" + table + "\n", encoding="utf-8")
        lines = ["# 变化对照", "", "当前稿可编辑，待业务核稿。"]
        for title, changes in (("已保留的手改", receipt["hand_edits"]), ("本轮日期改动", view["changes"])):
            if changes:
                lines += ["", f"## {title}"]
                for change in changes:
                    lines += ["", f"**{change['location']}**", "", f"- 改前　{change['before']}",
                              f"- 改后　{change['after']}"]
        lock = "、".join(str(i + 1) for i in view["lock_indices"])
        lines += ["", f"锁定第 {lock} 段。独立完成时限和原附件 2 均保留。",
                  "", f"当前 Word SHA-256　`{receipt['revision_id']}`"]
        (folder / "变化对照.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        receipt["artifacts"] = {path.name: digest(path.read_bytes()) for path in folder.iterdir()}
        (folder / RECEIPT).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        require(not output.exists(), "输出目录已存在，请指定一个新目录。")
        folder.rename(output)
