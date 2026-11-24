"""Limit this Codex trial to the reviewed public source set."""

from base64 import b64decode
from binascii import Error
from hashlib import sha256
from pathlib import Path

from app.revision.document import RevisionIssue, require


SOURCE_PATHS = {
    "body": "inputs/工作方案正文-官网提取.txt",
    "table": "inputs/附件1-任务分工表.docx",
    "reference": "inputs/附件2-项目清单.docx",
}


def approved_sources(root):
    manifest = root / "data/s0-s1/nanyang-2024-24/SHA256SUMS"
    if not manifest.is_file():
        return {}
    hashes = {path: digest for digest, path in (
        line.split(maxsplit=1) for line in manifest.read_text().splitlines()
    )}
    return {role: hashes[name] for role, name in SOURCE_PATHS.items()}


def decode_sources(inputs, approved):
    require(len(inputs) == len(approved) and all(isinstance(item, dict) for item in inputs),
            "请上传正文 TXT、任务分工表 Word 和项目清单 Word。")
    metadata, contents = [], {}
    for item in inputs:
        require(set(item) == {"role", "name", "content_base64"} and all(isinstance(v, str) for v in item.values()),
                "附件格式无效，请重新选择三个文件。")
        role, name, encoded = (item[k] for k in ("role", "name", "content_base64"))
        require(role in approved and role not in contents and 1 <= len(name) <= 180
                and Path(name).name == name and len(encoded) <= 2_000_000,
                "附件角色、文件名或大小无效。")
        try:
            content = b64decode(encoded, validate=True)
        except (Error, ValueError) as error:
            raise RevisionIssue("SOURCE_UNREADABLE", "无法解码附件，请重新选择文件。") from error
        digest = sha256(content).hexdigest()
        require(digest == approved[role], "当前 Codex 试跑仅接收已校验的南阳公开原件。", "SOURCE_NOT_APPROVED")
        contents[role] = content
        metadata.append({"role": role, "name": name, "content_hash": digest})
    metadata.sort(key=lambda source: source["role"])
    return metadata, contents
