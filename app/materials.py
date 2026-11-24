"""Read ordinary source materials and assign stable, human-readable anchors."""

import csv
import hashlib
import io
import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter
from pypdf import PdfReader

from app.revision.document import RevisionIssue, open_docx, require, table_rows
from app.revision.layout import describe_layout

MAX_BYTES = 4 * 1024**2
MAX_CHARS = 40_000
INLINE_HEADING = re.compile(r"(?m)^[ \t]*【(?P<label>(?:材料|附件)[^】\r\n]+|参考[^】\r\n]*)】[^\S\n]*$")
REFERENCE_HEADING = re.compile(r"仅(?:供|作|用于).{0,12}参考|只(?:供|作|用于).{0,12}参考|^参考(?:材料)?[一二三四五六七八九十\d]*$")


def _read_xlsx(raw, name):
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            parts = archive.infolist()
            require(len(parts) <= 256 and sum(part.file_size for part in parts) <= 8 * 1024**2,
                    f"{name} 展开后过大，请提供相关工作表。")
            require(len({part.filename for part in parts}) == len(parts), f"{name} 含重复部件。")
            require(not any(b"<!DOCTYPE" in archive.read(part).replace(b"\x00", b"") for part in parts
                            if part.filename.endswith((".xml", ".rels"))), f"{name} 含不支持的 XML 声明。")
        book = load_workbook(io.BytesIO(raw), data_only=False, keep_links=False)
        try:
            blocks, notes = [], []
            visible_count = sum(sheet.sheet_state == "visible" for sheet in book.worksheets)
            require(0 < visible_count <= 12, f"{name} 可见工作表过多或没有可见表，请提供相关工作表。")
            if visible_count < len(book.worksheets):
                notes.append(f"有 {len(book.worksheets) - visible_count} 张隐藏工作表，未纳入依据。")
            for index, sheet in enumerate(book.worksheets, 1):
                if sheet.sheet_state != "visible":
                    continue
                require(sheet.max_row <= 1000 and sheet.max_column <= 40,
                        f"{name} 的工作表 W{index} 超过 1000 行或 40 列，请提供相关区域。")
                notes.append(f"W{index} 的工作表名称：{sheet.title}。")
                hidden = [(dim.min or column_index_from_string(label), dim.max or column_index_from_string(label))
                          for label, dim in sheet.column_dimensions.items() if dim.hidden]
                if hidden or any(dim.hidden for dim in sheet.row_dimensions.values()):
                    notes.append(f"W{index} 隐藏行列未纳入依据。")
                if sheet.merged_cells.ranges:
                    notes.append(f"W{index} 有合并单元格，仅保留起点值，未推断空格含义。")
                for number, row in enumerate(sheet.iter_rows(), 1):
                    if (dim := sheet.row_dimensions.get(number)) is not None and dim.hidden:
                        continue
                    values = []
                    for col, cell in enumerate(row, 1):
                        if cell.value is None or any(start <= col <= end for start, end in hidden):
                            continue
                        if cell.data_type in {"f", "e"}:
                            notes.append(f"W{index}.{cell.coordinate} 是公式或错误值，未纳入依据。")
                            continue
                        # Non-percent values keep their raw precision; scale percentages explicitly.
                        value = cell.value
                        if type(value) in (float, int) and cell.number_format in {"0%", "0.0%", "0.00%"}:
                            digits = {"0%": 0, "0.0%": 1, "0.00%": 2}[cell.number_format]
                            value = format(value, f".{digits}%")
                        values.append(f"{get_column_letter(col)}: {value}")
                    if values:
                        blocks.append((f"W{index}.R{number}", " | ".join(values)))
            return blocks, notes
        finally:
            book.close()
    except RevisionIssue:
        raise
    except (BadZipFile, ValueError, TypeError, KeyError, OSError, SyntaxError, IndexError) as error:
        raise RevisionIssue("SOURCE_UNREADABLE", f"无法读取 {name}，请提供正常保存的 XLSX。") from error


def _read_pdf(raw, name):
    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        require(not reader.is_encrypted, f"{name} 已加密，请提供获准读取的副本。")
        require(0 < len(reader.pages) <= 30, f"{name} 超过 30 页或没有页面，请提供相关页。")
        blocks = []
        for index, page in enumerate(reader.pages, 1):
            lines = page.extract_text(extraction_mode="layout").splitlines()
            require(any(any(char.isalnum() for char in line) for line in lines),
                    f"{name} 第 {index} 页无可用文字层，请提供文字版；扫描识别尚未接入。")
            blocks.extend((f"G{index}.L{number}", line.strip()) for number, line in enumerate(lines, 1))
            require(sum(len(value) for _, value in blocks) <= MAX_CHARS, f"{name} 可读文字超过 4 万字。")
        return blocks, ["PDF 依据为文字层；核稿时请按页复核视觉表格的表头、空格和跨行关系。"]
    except RevisionIssue:
        raise
    except Exception as error:  # PDF parser errors vary with the damaged part.
        raise RevisionIssue("SOURCE_UNREADABLE", f"无法读取 {name} 的文字层，请提供可选中文字的 PDF。") from error


def _read_text(raw, name):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"{name} 不是 UTF-8 文本，请另存为 UTF-8 后重试。") from error


def _file_blocks(path, raw, document):
    suffix = path.suffix.lower()
    if suffix == ".docx":
        p_index = t_index = 0
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                p_index += 1
                yield f"P{p_index}", block.text
            elif isinstance(block, Table):
                t_index += 1
                require(not block._tbl.xpath(".//w:tbl"), f"{path.name} 含嵌套表格，请提供普通表格。")
                for index, row in enumerate(table_rows(block), 1):
                    yield f"T{t_index}.R{index}", " | ".join(row)
        return
    require(suffix in (".txt", ".md", ".csv"), f"暂不能读取 {path.name}，请转成 TXT、Markdown、CSV、XLSX、可选中文字的 PDF 或普通 DOCX。")
    text = _read_text(raw, path.name)
    if suffix == ".csv":
        for index, row in enumerate(csv.reader(io.StringIO(text, newline=""), strict=True), 1):
            yield f"R{index}", " | ".join(row)
    else:
        yield from ((f"L{i}", line) for i, line in enumerate(text.splitlines(), 1))


def _material(source_id, name, raw, blocks):
    blocks = {f"{source_id}.{anchor}": value for anchor, value in blocks if value.strip()}
    require(blocks, f"{name} 没有可读取的正文或表格内容。")
    return {"id": source_id, "name": name, "sha256": hashlib.sha256(raw).hexdigest(), "blocks": blocks}


def collect(request, texts, files, *, split_inline=False):
    require(request.strip(), "请说一下这次要解决什么，或者希望拿到什么样的稿件。")
    require(len(texts) + len(files) <= 12, "一次最多附 12 份材料，请先保留本轮相关材料。")
    materials = [_material("U", "本轮用户原话", request.encode(), [("1", request)])]
    for text in texts:
        materials.append(_material(f"S{len(materials)}", "用户粘贴材料", text.encode(),
                                   ((f"L{i}", line) for i, line in enumerate(text.splitlines(), 1))))
    for path in map(Path, files):
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, f"{path.name} 超过 4 MB，请提供相关部分。")
        document = open_docx(raw) if path.suffix.lower() == ".docx" else None
        parser = {".xlsx": _read_xlsx, ".pdf": _read_pdf}.get(path.suffix.lower())
        blocks, notes = parser(raw, path.name) if parser else (_file_blocks(path, raw, document), [])
        material = _material(f"S{len(materials)}", path.name, raw, blocks)
        if notes:
            material["intake_notes"] = notes
        if document is not None:
            material["layout"] = describe_layout(document, raw)
        materials.append(material)
    require(sum(len(value) for m in materials for value in (*m["blocks"].values(), *m.get("intake_notes", ()))) <= MAX_CHARS,
            "本轮可读文字超过 4 万字。请提供相关部分；系统没有截断资料。")
    if split_inline:
        headings = list(INLINE_HEADING.finditer(request))
        if headings and (instruction := request[:headings[0].start()].strip()):
            require(len(texts) + len(files) + len(headings) <= 12,
                    "一次最多附 12 份材料，请先保留本轮相关材料。")
            materials[0]["blocks"] = {"U.1": instruction}
            ends = [heading.start() for heading in headings[1:]] + [len(request)]
            for heading, end in zip(headings, ends, strict=True):
                raw = request[heading.start():end]
                material = _material(f"S{len(materials)}", heading["label"], raw.encode(),
                                     [("L1", raw.strip())])
                if REFERENCE_HEADING.search(heading["label"]):
                    material["reference_only"] = True
                materials.append(material)
    return materials
