"""Describe directly observed layout signals without inferring document status."""

from io import BytesIO
from zipfile import ZipFile

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "v": "urn:schemas-microsoft-com:vml",
}


def is_red(value):
    if not value or len(value.lstrip("#")) != 6:
        return False
    try:
        red, green, blue = bytes.fromhex(value.lstrip("#"))
    except ValueError:
        return False
    return red >= 128 and max(green, blue) < red / 2


def describe_layout(doc, content):
    centered = []
    for index, paragraph in enumerate(doc.paragraphs, 1):
        alignment = paragraph.alignment
        if alignment is None:
            alignment = paragraph.style.paragraph_format.alignment
        if alignment == WD_ALIGN_PARAGRAPH.CENTER and paragraph.text.strip():
            centered.append(index)
    with ZipFile(BytesIO(content)) as archive:
        names = archive.namelist()
        headers = [n for n in names if n.startswith("word/header") and n.endswith(".xml")]
        footers = [n for n in names if n.startswith("word/footer") and n.endswith(".xml")]
        red_line_parts = []
        for name in ["word/document.xml", *headers, *footers]:
            root = etree.fromstring(archive.read(name), parser=etree.XMLParser(resolve_entities=False, no_network=True))
            colors = root.xpath(
                ".//w:pBdr/*/@w:color | .//v:line/@strokecolor | .//a:ln/a:solidFill/a:srgbClr/@val",
                namespaces=NS,
            )
            if any(is_red(color) for color in colors):
                red_line_parts.append(name)
    return {
        "carrier": "docx",
        "centered_nonempty_paragraphs": centered,
        "sections": [{"orientation": section.orientation.name,
                      "page_size_twips": [section._sectPr.pgSz.get(qn("w:w")), section._sectPr.pgSz.get(qn("w:h"))]
                      if section._sectPr.pgSz is not None else None}
                     for section in doc.sections],
        "top_level_tables": len(doc.tables),
        "header_parts": len(headers), "footer_parts": len(footers),
        "explicit_red_line_parts": red_line_parts,
    }
