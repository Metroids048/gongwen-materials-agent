"""Bind the current native Invocation to the existing natural intake entry."""

import argparse
from contextlib import nullcontext, redirect_stdout
from functools import partial
import hashlib
import json
import os
import re
import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory, NamedTemporaryFile
from time import monotonic

from app.agent import (Candidate, _explicit_draft_genre, generate_intake,
                       generate_review, main, standalone_draft_question)
from app.delivery import prepare_delivery
from app.draft_layout import render
from app.revision.document import RevisionIssue

WORD_NOT_READY = "这轮还没有可交付的 Word。可以接着这份材料补充要求，或让我找回已留存的未核稿正文。"
TEXT_NOT_READY = "本轮正文暂未完成。你可以沿用刚才的材料继续核对，或补充需要调整的地方。"
TEXT_ONLY_REQUEST = re.compile(
    r"(?:^|[。！？；，,\n])\s*(?:这轮|本轮|现在)?\s*(?:请)?(?:先|暂时|暂)?\s*"
    r"(?:(?:别|不要|不用|不必|无需|不需要)\s*(?:给我|生成|导出|发|出|做|提供)?\s*(?:Word|附件|文档|文件|\.docx)"
    r"|先给(?:我)?(?:一份)?(?:可核对的|供审阅的)?(?:正文|纯文本|文字)|只(?:在聊天里|在这里)?(?:给我|发我|给|看|发|要)(?:看)?(?:正文|纯文本|文字|要点|提纲))"
    r"\s*(?=[。！？；，,、.!\n]|$)", re.IGNORECASE,
)
FROZEN_REVIEW_REQUEST = re.compile(
    r"^\s*(?:请|麻烦)?(?:帮我)?(?:对|给)\s*(?:刚才|上一轮|上轮|这份|该)(?:生成的|的|这份)?"
    r"(?:冻结)?(?:首稿|初稿|会商稿)\s*(?:做(?:一次)?)?独立核稿"
    r"(?:[，,、\s]*并?(?:返回|提供|给我|发我|交付)(?:可编辑的)?\s*Word)?[。！!\s]*$",
    re.IGNORECASE,
)


def load_input(native_stream=None, invocation_path=Path('/oasn/in/invocation.json')):
    """Formal projection or a private stdin envelope from the runtime tool factory."""
    if native_stream is None:
        invocation = json.loads(invocation_path.read_bytes())
        identity = invocation_identity(invocation)
        current = invocation['input']
        current = {**current, 'authorized_attachments': current.get('attachments', [])}
        binding = {'identity': identity, 'input': current}
    else:
        envelope = json.load(native_stream)
        identity = invocation_identity(envelope.get('identity', {}))
        current = envelope['input']
        # A retry can change the tool-call ID, while a new user turn has its own run ID.
        binding = {'identity': {k: identity[k] for k in ('session_id', 'consumer_id', 'run_id')}, 'input': current}
    prompt = current.get('user_prompt')
    files = current.get('attachments', [])
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(files, list) or not all(isinstance(x, str) for x in files):
        raise ValueError('本轮消息或附件结构无效；未调用模型。')
    return identity, current, json.dumps(binding, ensure_ascii=False, sort_keys=True).encode()


def require_attachment_binding(files, current):
    authorized = {Path(x).resolve() for x in current.get('authorized_attachments', [])}
    for file in files:
        if file.resolve() not in authorized or not file.is_file():
            raise ValueError('附件未绑定当前调用，未读取材料或调用模型。')


def cached_exit_code(output):
    attempt = json.loads(output.with_suffix('.attempt.json').read_text())
    if expected := attempt.get("receipt_sha256"):
        path = output / "运行回执.json"
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("本次回执与原调用不符，请重新核对交付文件。")
    code = attempt.get('exit_code')
    if attempt.get('status') != 'finished' or type(code) is not int:
        raise ValueError('原调用尚未确认完成，未重试或宣布成功。')
    return code


def invocation_identity(invocation):
    identity = {key: invocation.get(key) for key in
                ("session_id", "consumer_id", "invocation_id", "run_id")}
    if any(not isinstance(identity[key], str) or not identity[key].strip()
           for key in ("session_id", "invocation_id", "run_id")):
        raise ValueError("当前 Invocation 缺少会话或运行标识；未调用模型。")
    if identity["consumer_id"] is not None and not isinstance(identity["consumer_id"], str):
        raise ValueError("当前 Invocation 所有者标识无效；未调用模型。")
    return identity


def require_parent_binding(parent, identity):
    attempt = parent.with_suffix(".attempt.json")
    try:
        if attempt.is_symlink():
            raise ValueError("父版绑定不能是链接。")
        binding = json.loads(attempt.read_text())
        matches = all(binding.get(key) == identity[key] for key in ("session_id", "consumer_id"))
    except (OSError, ValueError, AttributeError, TypeError):
        matches = False
    if not matches:
        raise ValueError("无法确认父版属于当前会话；请重新提供本轮获准修改的 Word。")


def active_task_file(root, identity):
    owner = json.dumps([identity["session_id"], identity["consumer_id"]], ensure_ascii=False).encode()
    return root / ".conversations" / (hashlib.sha256(owner).hexdigest() + ".json")


def active_task(root, identity):
    state = active_task_file(root, identity)
    if not state.exists():
        return None
    name = json.loads(state.read_bytes()).get("output")
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{24}", name):
        raise ValueError("无法确认本会话当前稿件。")
    parent = root / name
    require_parent_binding(parent, identity)
    return parent


def select_active_task(root, identity, output):
    target = active_task_file(root, identity)
    target.parent.mkdir(exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, mode="w", delete=False) as stream:
        json.dump({"output": output.name}, stream)
    os.replace(stream.name, target)


def current_task_prompt(root, identity, prompt, prior_hint):
    # The chat can suggest a continuation. Only the last owned receipt supplies facts.
    if standalone_draft_question(prompt) or _explicit_draft_genre(prompt) or re.search(r"(?:另|再|重新)写一份", prompt):
        return prompt
    parent = active_task(root, identity)
    if parent is None:
        return prompt
    path = parent / "运行回执.json"
    if not path.is_file() or path.is_symlink():
        return prompt
    receipt = json.loads(path.read_bytes())
    previous = receipt.get("original_request") or ""
    explicit_update = bool(re.search(r"^\s*(?:补充|更正|改为|改成|另外加上|上份通知|沿用刚才|刚才那份)", prompt))
    if not previous or not (receipt.get("status") == "needs_input" or
                            explicit_update and receipt.get("status") in {"draft", "needs_review", "text_only"}):
        return prompt
    if not (prior_hint or explicit_update or re.search(r"^(?:发给|只安排|两天|分别|各一天|是|落款|按|不要|保留)", prompt)):
        return prompt
    if prompt.strip() in previous.splitlines():
        return previous
    return previous + "\n" + prompt


def print_delivery(output, status, delivery):
    header = {"status": status, "message": delivery['message'],
              "delivery_message": delivery['message'], "word_contents": delivery['word_contents'],
              "output_directory": str(output)}
    if "diagnostic" in delivery:
        header["diagnostic"] = delivery["diagnostic"]
    print('\n'.join([json.dumps(header, ensure_ascii=False), *(f'MEDIA:{path}' for path in delivery['files'])]))


def changed_decision_fields(receipt, review):
    """Name only fields whose edited values disagree with a source-bound task row."""
    rows = receipt.get("expert_workflow", {}).get("source_action_rows", [])
    fields = set()
    for change in review.get("changes", []):
        before = change.get("before", {})
        if not isinstance(before, dict) or not before.get("action") or not before.get("anchors"):
            continue
        same_action = [row for row in rows if row.get("action") == before["action"]
                       and row.get("anchors") == before["anchors"]]
        after = change.get("after", [])
        if len(same_action) != 1 or not isinstance(after, list) or len(after) != 1 or not isinstance(after[0], dict):
            continue
        source = same_action[0]
        for field in ("action", "lead", "support", "timing", "deliverable", "acceptance"):
            if field in source and before.get(field) == source[field] and after[0].get(field) != source[field]:
                fields.add(field)
    return sorted(fields)


def deliver(output, request=None, exit_code=0):
    receipt = json.loads((output / "运行回执.json").read_text())
    status = receipt.get("status")
    if exit_code and (exit_code != 2 or status in {"draft", "revision", "text_only"}):
        message = TEXT_NOT_READY if TEXT_ONLY_REQUEST.search(request or "") else WORD_NOT_READY
        print_delivery(output, "failed", {"message": message, "word_contents": None, "files": []})
        return False
    delivery = prepare_delivery(output, receipt, request)
    review = receipt.get("conference_review", {})
    if status == "needs_review" and not delivery["files"]:
        delivery["message"] = TEXT_NOT_READY if TEXT_ONLY_REQUEST.search(request or "") else WORD_NOT_READY
    if status == "needs_review" and not delivery["files"] and re.fullmatch(r"[a-f0-9]{24}", output.name):
        stage = review.get("failed_stage", "draft_check")
        if stage not in {"review", "repair", "task_evidence"}:
            stage = "draft_check"
        code = review.get("error", {}).get("code", receipt.get("draft_check_error", "DRAFT_CHECK_BLOCKED"))
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,47}", code):
            code = "REVIEW_ERROR"
        delivery["diagnostic"] = {"stage": stage, "code": code, "reference": output.name}
        if stage == "task_evidence" and (fields := changed_decision_fields(receipt, review)):
            delivery["diagnostic"]["disputed_fields"] = fields
    if status == "needs_input" and receipt.get("candidate", {}).get("questions"):
        delivery["message"] = "\n".join(receipt["candidate"]["questions"])
    print_delivery(output, status, delivery)
    return exit_code == 0 and status in {"draft", "revision"} and bool(delivery["word_contents"])


def recover_delivery(parent, identity, action):
    if parent is None:
        raise ValueError("找不到本会话对应的原稿记录。")
    parent = parent.resolve()
    if parent.parent != Path("/oasn/out/gongwen") or not re.fullmatch(r"[0-9a-f]{24}", parent.name):
        raise ValueError("找不到本会话对应的原稿记录。")
    require_parent_binding(parent, identity)
    exit_code = cached_exit_code(parent)
    receipt_path = parent / "运行回执.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError("原调用没有可用的运行回执。")
    receipt = json.loads(receipt_path.read_text())
    if action == "resend_word":
        if exit_code != 0 or receipt.get("status") not in {"draft", "revision"}:
            raise ValueError("这次原调用没有已完成的正文 Word 可补发。")
        delivery = prepare_delivery(parent, receipt)
        if not delivery["word_contents"]:
            raise ValueError("原 Word 已不可用。")
        print_delivery(parent, receipt["status"], delivery)
        return

    if exit_code != 2 or receipt.get("status") != "needs_review" or not receipt.get("rejected_candidate"):
        raise ValueError("这次原调用没有可找回的被拦原稿。")
    candidate = Candidate.model_validate(receipt["rejected_candidate"])
    lines = ["未通过核稿的原稿（仅供核对）", "这份原稿的事实或执行安排尚需核对。", "", candidate.title]
    for section in candidate.sections:
        lines.extend(["", section.title, *(p.text for p in section.paragraphs)])
    if candidate.tasks:
        lines.extend(["", "分工表", "工作事项｜牵头｜配合｜时间或依赖｜交付成果｜验收方式｜联系人｜电话"])
        for task in candidate.tasks:
            lines.append("｜".join((task.action, task.lead, task.support, task.timing,
                                   task.deliverable, task.acceptance, task.contact, task.phone)))
    public = parent / "交付"
    if public.is_symlink():
        raise ValueError("原稿的交付目录不可用。")
    public.mkdir(exist_ok=True)
    target = public / "未通过核稿的原稿.txt"
    if target.is_symlink():
        raise ValueError("原稿的交付位置不可用。")
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    with TemporaryDirectory(dir=public) as temp:
        private = Path(temp) / target.name
        private.write_bytes(raw)
        try:
            os.link(private, target)
        except FileExistsError:
            if target.is_symlink() or target.read_bytes() != raw:
                raise ValueError("留存的原稿与回执不一致。") from None
    print_delivery(parent, "recovered_draft", {
        "message": "找回了当时被拦的原稿，正文和分工表见附件。原稿尚未通过核稿。"
                   if candidate.tasks else "找回了当时被拦的原稿，正文见附件。原稿尚未通过核稿。",
        "word_contents": None, "files": [target],
    })


def run():
    from app.runtime.openagent_model import stage_log

    parser = argparse.ArgumentParser()
    parser.add_argument("--continue-from", type=Path)
    parser.add_argument("--opinion-template", type=Path)
    parser.add_argument("--file", action="append", type=Path, default=[])
    parser.add_argument("--single-writer", action="store_true")
    parser.add_argument("--delivery-action", choices=("resend_word", "recover_draft"))
    parser.add_argument("--review-frozen", action="store_true")
    parser.add_argument("--native-input", action="store_true", help="仅供 OpenClaw 业务工具通过 stdin 传入本轮消息和运行时身份")
    args = parser.parse_args()
    identity, current, raw = load_input(sys.stdin if args.native_input else None)
    prompt = current["user_prompt"]
    files = current.get("attachments", [])
    if not isinstance(prompt, str) or not isinstance(files, list) or not all(isinstance(x, str) for x in files):
        raise ValueError("当前 Invocation 输入结构不支持；未调用模型。")
    text_only = bool(TEXT_ONLY_REQUEST.search(prompt))
    root = Path("/oasn/out/gongwen")
    files = [Path(x) for x in files] + args.file
    review_request = bool(FROZEN_REVIEW_REQUEST.fullmatch(prompt))
    if args.review_frozen or review_request:
        if not review_request or files or args.opinion_template or args.delivery_action or args.continue_from:
            print_delivery(None, "failed", {"message": "请明确对当前会话的首稿独立核稿。",
                                           "word_contents": None, "files": []})
            return 2
        parent = active_task(root, identity)
        if parent is None or not (parent / "运行回执.json").is_file():
            print_delivery(None, "failed", {"message": "本会话没有可核对的首稿。",
                                           "word_contents": None, "files": []})
            return 2
        parent_code = cached_exit_code(parent)
        parent_receipt = json.loads((parent / "运行回执.json").read_bytes())
        parent_mode = (parent_receipt.get("expert_workflow") or {}).get("mode")
        if parent_mode == "frozen_review":
            deliver(parent, prompt, parent_code)
            return parent_code
        if parent_mode != "one_pass" or parent_code:
            print_delivery(None, "failed", {"message": "本会话没有可交给独立核稿的冻结首稿。",
                                           "word_contents": None, "files": []})
            return 2
        args.review_frozen = True
    else:
        parent = None
    if args.delivery_action:
        try:
            if files or args.opinion_template or args.single_writer:
                raise ValueError("找回已有文件只需要本会话的原稿记录。")
            if (text_only or re.search(r"(?:别|不要|不用|无需|不能|禁止)(?:再)?(?:补发|重发|重新发|找回|取回|发附件|发文件)", prompt)
                    or not re.search(r"补发|重发|重新发|找回|取回|再(?:发|给)[^。！？]{0,12}(?:上份|上次|那份|原件|原稿|附件|Word)|看.{0,6}原稿", prompt, re.I)):
                raise ValueError("当前消息没有要求找回此前的文件。")
            active = active_task(root, identity)
            if active and args.continue_from and active != args.continue_from.resolve():
                raise ValueError("补发的记录不是本会话正在处理的稿件。")
            recover_delivery(args.continue_from, identity, args.delivery_action)
        except (OSError, ValueError):
            print_delivery(None, "failed", {"message": "这次没能从本会话找回原文件。请核对要补发的是已生成的 Word，还是被核稿拦住的原稿。",
                                           "word_contents": None, "files": []})
            return 2
        return 0
    require_attachment_binding(files, current)
    roots = [Path('/oasn/in')]
    if args.native_input:
        roots += [Path('/oasn/.openclaw/media/inbound'), Path('/root/.openclaw/media/inbound')]
    for file in files:
        if not any(file.resolve().is_relative_to(root) for root in roots) or not file.is_file():
            raise ValueError("附件不在当前输入目录；未调用模型。")
    if args.opinion_template and args.opinion_template.resolve() not in {file.resolve() for file in files}:
        raise ValueError("意见模板必须是本次明确提供的附件；未调用模型。")
    if args.continue_from and not args.continue_from.resolve().is_relative_to(Path("/oasn/out/gongwen")):
        raise ValueError("父版必须是当前会话已交付的公文目录。")
    if args.continue_from:
        args.continue_from = args.continue_from.resolve()
        require_parent_binding(args.continue_from, identity)
        active = active_task(root, identity)
        if active and active != args.continue_from:
            print_delivery(None, "failed", {"message": "当前对话已有更新的稿件。请指出本轮要改的稿件，并提供对应的 Word。",
                                           "word_contents": None, "files": []})
            return 2
        cached_exit_code(args.continue_from)
    user_prompt = prompt
    fresh_intake = not files and not args.continue_from and standalone_draft_question(user_prompt) is not None
    if not (files or args.continue_from or fresh_intake or parent):
        prompt = current_task_prompt(root, identity, user_prompt, current.get("prior_request"))
    operation = hashlib.sha256(json.dumps(
        [prompt, str(args), str(parent) if parent else None, os.environ.get("OPENAGENT_CHAT_MODEL"),
         current.get("public_schedule")], ensure_ascii=False, sort_keys=True).encode())
    # Generation assets are part of the cached revision, including reviewed writing rules.
    for code in (Path(__file__), Path(main.__code__.co_filename),
                 Path(render.__code__.co_filename),
                 Path(__file__).parent / 'app/materials.py',
                 Path(__file__).parent / 'app/number_basis.py',
                 Path(__file__).parent / 'app/minute_actions.py',
                 Path(__file__).parent / 'app/notice_scope.py',
                 Path(__file__).parent / 'app/revision/deadline.py',
                 Path(prepare_delivery.__code__.co_filename),
                 Path(__file__).parent / 'app/runtime/openagent_model.py'):
        operation.update(hashlib.sha256(code.read_bytes()).digest())
    for folder in ('prompts', 'skills'):
        for asset in sorted((Path(__file__).parent / folder).rglob('*.md')):
            operation.update(str(asset.relative_to(Path(__file__).parent)).encode())
            operation.update(hashlib.sha256(asset.read_bytes()).digest())
    for file in files:
        operation.update(file.name.encode())
        operation.update(hashlib.sha256(file.read_bytes()).digest())
    operation_hash = operation.hexdigest()
    output = root / hashlib.sha256(raw + operation.digest()).hexdigest()[:24]
    active = active_task(root, identity)
    if active and active != output and not (args.continue_from or parent):
        prior_attempt = json.loads(active.with_suffix(".attempt.json").read_bytes())
        if prior_attempt.get("operation_sha256") == operation_hash:
            if prior_attempt.get("status") == "finished" and (active / "运行回执.json").is_file():
                result = cached_exit_code(active)
                deliver(active, prompt, result)
                return result
            print_delivery(None, "failed", {"message": "同一份材料已有处理记录，可以在会话中查看原结果。",
                                           "word_contents": None, "files": []})
            return 2
    if args.native_input:
        # Survives gateway/process restart and changes to the tool's optional arguments.
        turn = hashlib.sha256(json.dumps(
            [identity[k] for k in ("session_id", "consumer_id", "run_id")]).encode()).hexdigest()
        guard = root / ".native-turns" / turn
        guard.parent.mkdir(parents=True, exist_ok=True)
        try:
            with guard.open("x") as stream:
                stream.write(output.name)
        except FileExistsError:
            if guard.read_text() != output.name:
                print_delivery(None, "failed", {"message": "本轮已有公文调用，请查看第一次的结果。",
                                               "word_contents": None, "files": []})
                return 2
    if (output / "运行回执.json").is_file():
        require_parent_binding(output, identity)
        active = active_task(root, identity)
        result = cached_exit_code(output)
        independent_question = fresh_intake and result == 2
        revision_of_current = args.continue_from is not None and active == args.continue_from
        if active and active != output and not (independent_question or revision_of_current):
            print_delivery(None, "failed", {"message": "这份稿件已有后续任务，请在对应的最新回复中查看。",
                                           "word_contents": None, "files": []})
            return 2
        if deliver(output, prompt, result) and revision_of_current and active_task(root, identity) == active:
            select_active_task(root, identity, output)
        return result
    attempt = output.with_suffix(".attempt.json")
    start_record = {**identity, "status": "started", "output": str(output), "operation_sha256": operation_hash,
                    "input_sha256": hashlib.sha256(user_prompt.encode()).hexdigest()}
    if prompt != user_prompt:
        start_record["effective_input_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
    attempt.parent.mkdir(parents=True, exist_ok=True)
    try:
        with attempt.open("x") as handle:
            json.dump(start_record, handle)
    except FileExistsError:
        require_parent_binding(output, identity)
        try:
            previous = cached_exit_code(output)
        except ValueError:
            previous = None
        if previous:
            print_delivery(None, "failed", {"message": "原调用已失败，未生成可交付文件。可以在当前对话中继续这份材料。",
                                           "word_contents": None, "files": []})
            return previous
        print("该 Invocation 已执行过但没有完整回执。请先核对原调用，未重复调用模型。")
        return 2
    # An independent short question may run while another draft holds this conversation's active record.
    if not args.continue_from and (not fresh_intake or active is None or (active / "运行回执.json").is_file()):
        select_active_task(root, identity, output)
    argv = [prompt, "--codex-model", os.environ["OPENAGENT_CHAT_MODEL"], "--output", str(output)]
    if text_only:
        argv += ["--text-only"]
    if not (args.continue_from or fresh_intake or parent) and isinstance(current.get("public_schedule"), dict):
        argv += ["--public-schedule", json.dumps(current["public_schedule"], ensure_ascii=False)]
    if parent:
        argv += ["--review-frozen-from", str(parent)]
    for file in files:
        argv += ["--file", str(file)]
    if args.continue_from:
        argv += ["--continue-from", str(args.continue_from)]
    if args.opinion_template:
        argv += ["--opinion-template", str(args.opinion_template)]
    failure_code = None
    model_failure = None
    input_issue = None
    runtime_failure = None
    started = monotonic()
    trace_token = stage_log.set(output.with_suffix(".model.jsonl"))
    try:
        # Only the verified delivery belongs on the native tool's stdout.
        with redirect_stdout(sys.stderr) if args.native_input else nullcontext():
            if os.environ["OPENAGENT_CHAT_MODEL"] == "deepseek-v4-pro":
                result = main(argv, quick_notice_generate=partial(generate_intake, reasoning_effort="none"),
                              quick_control_review_generate=partial(generate_review, reasoning_effort="low"))
            else:
                result = main(argv)
    except SystemExit as error:
        # app.agent is also a CLI: parser.exit must not leave its native attempt running.
        result = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
        if isinstance(error.__context__, RevisionIssue):
            failure_code = error.__context__.code
            model_failure = getattr(error.__context__, "model_failure", None)
            if failure_code in {'NEEDS_INPUT', 'SOURCE_UNREADABLE'}:
                input_issue = str(error.__context__)
        if error.code is not None and not isinstance(error.code, int):
            print(error.code, file=sys.stderr)
    except Exception as error:
        result = 1
        failure_code = "PROGRAM_ERROR"
        runtime_failure = {"type": type(error).__name__, "frames": [
            {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
            for frame in traceback.extract_tb(error.__traceback__)[-6:]]}
    finally:
        stage_log.reset(trace_token)
    if input_issue:
        result = 2
    record = {**start_record, "status": "finished", "exit_code": result,
              "error_code": failure_code,
              "program_elapsed_seconds": round(monotonic() - started, 1)}
    receipt_path = output / "运行回执.json"
    if receipt_path.is_file() and not receipt_path.is_symlink():
        record["receipt_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    if model_failure:
        record["model_failure"] = model_failure
    if runtime_failure:
        record["runtime_failure"] = runtime_failure
    attempt.write_text(json.dumps(record))
    if not runtime_failure and (output / "运行回执.json").is_file():
        if deliver(output, prompt, result) and args.continue_from and active_task(root, identity) == active:
            select_active_task(root, identity, output)
    elif input_issue:
        print(json.dumps({"status": "needs_input", "word_contents": None,
                          "delivery_message": input_issue}, ensure_ascii=False))
    elif result:
        message = TEXT_NOT_READY if text_only else WORD_NOT_READY
        print_delivery(output, "failed", {"message": message, "word_contents": None, "files": []})
    return result


if __name__ == "__main__":
    raise SystemExit(run())
