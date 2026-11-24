"""The Jiangsu rural innovation case profile and its Word format."""

import hashlib
import re
from functools import partial
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from pydantic import BaseModel, ConfigDict

from app.revision.document import open_docx, require, word_bytes
from app.runtime.codex import ask_codex

PROFILE = "jiangsu-rural-innovation-2026"
NOTICE = "第三届“深化党建引领，扎实推进乡村全面振兴”案例征集启事"
SECTIONS = ("内容提要", "案例背景", "主要做法", "成效与影响", "经验启示")
EDITABLE = ("内容提要", "成效与影响")
UPSTREAM = "c9cdbd432713ca7eb9367bea22e6d2ee5c1cdab2"
generate_case = partial(ask_codex, reasoning_effort="medium")
STYLE_FILES = {
    "公文语料/文章结构模板.md": "73084d3bfb3a90e2a0b528aecbe070be666b95f3a6b5df93d64fb2b773446fd8",
    "示例/经验材料_矛盾纠纷调解一站式化解.md": "403e968c9717d8dfd9b8c27f9294d623fbf13db8f85c9caa47f8f5099f9d6e7c",
}
PLAN_STYLE_FILES = {
    "公文语料/文章结构模板.md": STYLE_FILES["公文语料/文章结构模板.md"],
    "示例/工作方案_电动自行车充停设施补建和隐患整治攻坚方案.md":
        "98b331dd2839c197586fcaeaa6e322d13bf7656403883c7b7abadd806e474c58",
}


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: str
    paragraph: int
    text: str


class Edits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: list[Edit]


def matches(request, materials):
    return "案例" in request and any(
        NOTICE in block for m in materials[1:] for block in m["blocks"].values()
    )


def load_style(root: Path, *, genre="experience-draft"):
    template = {"experience-draft": ("## 三、经验材料", "## 四、"),
                "plan-draft": ("## 一、工作方案", "## 二、")}[genre]
    files = PLAN_STYLE_FILES if genre == "plan-draft" else STYLE_FILES
    snippets = []
    for name, expected in files.items():
        raw = (root / name).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == expected,
                f"上游写法文件版本不匹配：{name}。请使用 {UPSTREAM}。", "STYLE_MISMATCH")
        value = raw.decode()
        if name.startswith("公文语料/"):
            value = template[0] + value.split(template[0], 1)[1].split(template[1], 1)[0]
        snippets.append(f"{name}\n{value}")
    return "\n\n".join(snippets), {"revision": UPSTREAM, "genre": genre, "files": files}


def char_count(paragraphs):
    return sum(len(re.sub(r"\s", "", text)) for text in paragraphs)


def check_sections(sections):
    require(tuple(name for name, _ in sections) == SECTIONS,
            "请按征集通知组织内容提要、案例背景和正文三节。", "SUBMISSION_STRUCTURE")
    counts = [char_count(paragraphs) for _, paragraphs in sections]
    require(200 <= counts[0] <= 300 and 400 <= counts[1] <= 500 and sum(counts[2:]) <= 3000,
            f"本稿提要 {counts[0]} 字、背景 {counts[1]} 字、申报正文 {sum(counts[2:])} 字，未通过本轮篇幅检查。",
            "SUBMISSION_LENGTH")
    return dict(zip(SECTIONS, counts, strict=True))


def render_word(title, sections):
    doc = Document()
    page = doc.sections[0]
    page.page_width, page.page_height = Cm(21), Cm(29.7)
    page.left_margin = page.right_margin = Cm(2.7)
    page.top_margin = page.bottom_margin = Cm(2.5)
    for name, font, points, bold in (
        ("Title", "黑体", 16, True), ("Subtitle", "楷体", 14, False),
        ("Heading 1", "黑体", 14, True), ("Normal", "楷体", 14, False),
    ):
        style = doc.styles[name]
        style.font.name, style.font.size, style.font.bold = font, Pt(points), bold
        style.font.color.rgb = None
        fonts = style._element.get_or_add_rPr().rFonts
        for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            fonts.attrib.pop(qn(f"w:{attr}"), None)
        fonts.set(qn("w:eastAsia"), font)
        color = style._element.get_or_add_rPr().get_or_add_color()
        color.set(qn("w:val"), "000000")
        color.attrib.pop(qn("w:themeColor"), None)
        style.paragraph_format.space_before = Pt(0)
        style.paragraph_format.space_after = Pt(0)
        style.paragraph_format.line_spacing = 2
    normal = doc.styles["Normal"].paragraph_format
    normal.first_line_indent = Pt(28)
    normal.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    for name in ("Title", "Subtitle"):
        style = doc.styles[name]
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        props = style._element.get_or_add_pPr()
        border = props.find(qn("w:pBdr"))
        if border is not None:
            props.remove(border)
    title_style = doc.styles["Title"].paragraph_format
    title_style.space_after = Pt(6)
    title_style.keep_with_next = True
    subtitle = doc.styles["Subtitle"].paragraph_format
    subtitle.space_after = Pt(10)
    subtitle.keep_with_next = True
    heading = doc.styles["Heading 1"].paragraph_format
    heading.first_line_indent = Pt(0)
    heading.space_before = Pt(6)
    unit = "申报单位　待确认"
    doc.add_paragraph(title, "Title")
    doc.add_paragraph(unit, "Subtitle")
    expected = [title, unit]
    for name, paragraphs in sections:
        doc.add_heading(name, level=1)
        expected.append(name)
        for paragraph in paragraphs:
            require(paragraph.strip() and "\n" not in paragraph and "\r" not in paragraph,
                    "申报稿每段须有正文，段内不得夹带换行。", "SUBMISSION_STRUCTURE")
            doc.add_paragraph(paragraph)
            expected.append(paragraph)
    word = word_bytes(doc)
    require([p.text for p in open_docx(word).paragraphs] == expected,
            "Word 复读与当前稿不一致。", "RENDER_MISMATCH")
    return "\n\n".join(expected) + "\n", word
