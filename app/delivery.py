"""Select user-facing Word files without exposing working and review sidecars."""

import hashlib
import csv
import re

from docx import Document


def prepare_delivery(output, receipt, request=None):
    artifacts = receipt.get('artifacts', {})
    for name, expected in artifacts.items():
        path = output / name
        if (path.is_symlink() or path.resolve().parent != output.resolve()
                or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ValueError('回执交付物缺失或已改变，未宣布交付。')

    status = receipt.get('status')
    candidate = receipt.get('candidate', {})
    word = output / '正文.docx'
    files, contents = [], None
    message = receipt.get('message') or '这次还没有生成可交付的Word。'
    if status in {'draft', 'revision'} and word.name in artifacts:
        document = Document(word)
        title = candidate.get('title') or next((p.text.strip() for p in document.paragraphs if p.text.strip()), '')
        title = title or receipt.get('parent_name', '').removesuffix('.docx') or '公文稿'
        title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '', title).strip(' .')[:60] or '公文稿'
        public = output / '交付'
        if public.is_symlink():
            raise ValueError('交付目录不能是链接。')
        public.mkdir(exist_ok=True)
        target = public / (title + '.docx')
        raw = word.read_bytes()
        if target.is_symlink() or (target.exists() and target.read_bytes() != raw):
            raise ValueError('交付副本与本轮Word不一致，未宣布交付。')
        if not target.exists():
            with target.open('xb') as stream:
                stream.write(raw)
        files.append(target)
        contents = {'file_name': target.name, 'table_count': len(document.tables)}
        message = 'Word写好了，可以直接修改。' if status == 'draft' else 'Word改好了，可以直接修改。'

    review = receipt.get('conference_review', {})
    notes = list((receipt.get("expert_workflow") or {}).get("pending_quantities", []))
    if review.get('error') or review.get('status') == 'incomplete':
        notes.append('这份初稿的独立核稿没完成，请先核对后使用。')
    unaddressed = set(review.get('unaddressed_targets', []))
    for finding in review.get('review', {}).get('findings', []):
        pending = finding.get('level') == 'pending' or (
            finding.get('level') == 'blocking' and
            (review.get('status') != 'changes_applied' or unaddressed.intersection(finding.get('target_ids', []))))
        if pending and (defect := finding.get('defect', '').strip()):
            if defect not in notes:
                notes.append(defect)
    if unaddressed and not notes:
        notes.append('还有核稿发现未完成修改，请核对后使用。')
    if contents is None and status in {'draft', 'revision'}:
        message = '这次还没有生成可交付的Word。'
    # Only concrete unresolved matters enter chat; internal IDs, source ledgers and
    # successfully repaired findings stay in the private receipt for continuation.
    if status == 'text_only':
        notes = [note for note in notes if note not in message]
    if notes:
        message += '\n需要确认：' + '；'.join(note.rstrip('。；') for note in notes) + '。'
    questions = candidate.get('questions', [])
    if questions:
        message += '\n' + '\n'.join(questions)

    # A requested opinion template is a business document, not a debug sidecar.
    request = request if request is not None else receipt.get('original_request', '')
    for name, marker in (('意见汇总处理表.docx', '意见汇总'), ('改稿短表.docx', '改稿短表')):
        requested = marker in request or (name == '意见汇总处理表.docx' and receipt.get('template_sha256'))
        if requested and name in artifacts and status not in {'needs_input', 'needs_review', 'failed'}:
            files.append(output / name)
    report_only = status in {'review_only', 'triage', 'held'}
    if '逐条核稿表.csv' in artifacts and (report_only or '核稿单' in request or '核稿表' in request):
        # Existing review-only actions still deliver their requested findings, in Word.
        public = output / '交付'
        if public.is_symlink():
            raise ValueError('交付目录不能是链接。')
        public.mkdir(exist_ok=True)
        report = public / '核稿意见.docx'
        if report.is_symlink():
            raise ValueError('交付文件不能是链接。')
        doc = Document()
        doc.add_heading('核稿意见', 0)
        with (output / '逐条核稿表.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        if not rows:
            raise ValueError('核稿记录为空，未宣布交付。')
        fields, *items = rows
        for index, row in enumerate(items, 1):
            doc.add_heading(f'第{index}条', level=1)
            for label, value in zip(fields, row):
                if value.strip():
                    doc.add_paragraph(f'{label}：{value}')
        doc.save(report)
        files.append(report)
        if report_only:
            message = '核稿意见整理好了，原稿没有改动。'
            contents = {'file_name': report.name, 'table_count': 0}
    return {'message': message, 'word_contents': contents, 'files': files}
