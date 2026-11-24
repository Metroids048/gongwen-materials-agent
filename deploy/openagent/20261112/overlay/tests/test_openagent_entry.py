import contextlib
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from docx import Document

import openagent_entry as entry
from app.revision.document import RevisionIssue

from openagent_entry import deliver, invocation_identity, require_parent_binding, load_input, cached_exit_code, require_attachment_binding


class NativeDelivery(unittest.TestCase):
    def test_source_bound_edit_identifies_only_changed_decision_fields(self):
        row = {"action": "交演练记录", "lead": "试点站", "acceptance": "", "anchors": ["S1.L3"]}
        receipt = {"expert_workflow": {"source_action_rows": [row]}}
        change = {"before":row, "after":[{**row, "acceptance":"建议留待会商"}]}
        self.assertEqual(entry.changed_decision_fields(receipt, {"changes":[change]}), ["acceptance"])
        other = {**row, "anchors":["S2.L3"]}
        change["before"] = other
        self.assertEqual(entry.changed_decision_fields(receipt, {"changes":[change]}), [])

    def test_same_native_conversation_keeps_user_prompt_and_delivers_word_after_schedule_lookup(self):
        initial = "帮我写一下 apec居家办公的通知书"
        follow_up = "发给深圳的上班族，apec正式会议开始前一天和结束后一天，发文单位是深圳市政府。"
        schedule = {"url": "https://www.mfa.gov.cn/wjdt_674879/wsrc_674883/202611/t20261113_12058548.shtml",
                    "quote": "第三十三次领导人非正式会议将于11月18日至19日在深圳举行"}
        body = "拟于11月17日和20日安排居家办公。"
        writes = []

        def writer(_model, prompt, response_type, **_kwargs):
            self.assertIn("会前一天为11月17日，会后一天为11月20日", prompt)
            writes.append(prompt)
            return response_type(profile="notice-draft", title="居家办公通知（审阅稿）", sections=[
                {"title": "", "paragraphs": [{"text": body, "kind": "fact", "anchors": ["U.1", "P.1"]}]},
                {"title": "", "paragraphs": [{"text": "深圳市政府", "kind": "fact", "anchors": ["U.1"]}]},
            ], tasks=[], questions=[], review_notes=[], material_uses=[
                {"source_id": "U", "role": "mixed", "usable_anchors": ["U.1"]},
                {"source_id": "P", "role": "facts", "usable_anchors": ["P.1"]},
            ]), {}

        with TemporaryDirectory() as temp:
            root = Path(temp) / "out"

            def native_path(value):
                return root if str(value) == "/oasn/out/gongwen" else Path(value)

            def invoke(turn, **current):
                identity = dict(session_id="session", consumer_id="owner", invocation_id=turn, run_id=turn)
                envelope = io.StringIO(json.dumps({"identity": identity, "input": current}))
                output = io.StringIO()
                with patch("sys.stdin", envelope), contextlib.redirect_stdout(output):
                    status = entry.run()
                header, *files = output.getvalue().splitlines()
                return status, json.loads(header), files

            with patch.object(entry, "Path", side_effect=native_path), \
                    patch.object(entry, "generate_intake", writer), \
                    patch.dict("os.environ", {"OPENAGENT_CHAT_MODEL": "deepseek-v4-pro"}), \
                    patch("sys.argv", ["openagent_entry.py", "--native-input"]), \
                    contextlib.redirect_stderr(io.StringIO()):
                status, header, files = invoke("question", user_prompt=initial)
                self.assertEqual((status, header["status"], files), (2, "needs_input", []))
                self.assertIn("发给谁", header["delivery_message"])
                self.assertFalse(writes)
                stale = f"{follow_up}\n另写一份给深圳上班族的 APEC 居家办公通知。深圳市政府落款，只安排正式会议开始前一天和结束后一天，两天分别执行。具体日期还没定，正文就用相对日期，成文日期留空。先给一份可编辑 Word 审阅稿。"
                status, header, files = invoke("fresh-question", user_prompt=initial, prior_request=stale)
                self.assertEqual((status, header["status"], files), (2, "needs_input", []))
                self.assertIn("发给谁", header["delivery_message"])
                self.assertIn("可编辑的 Word 审阅稿", header["delivery_message"])
                self.assertFalse(writes)
                status, header, files = invoke("scope-question", user_prompt=follow_up, prior_request=initial,
                                               public_schedule=schedule)
                self.assertEqual((status, header["status"], files), (2, "needs_input", []))
                self.assertIn("各一天", header["delivery_message"])
                self.assertFalse(writes)
                status, header, files = invoke("scope-question-with-stale-facts", user_prompt=follow_up,
                                               prior_request=f"{initial}\n{stale.splitlines()[1]}")
                self.assertEqual((status, header["status"], files), (2, "needs_input", []))
                self.assertIn("各一天", header["delivery_message"])
                self.assertFalse(writes)
                arrangement = "只安排前后各一天。"
                status, header, files = invoke("draft", user_prompt=arrangement,
                                               prior_request=f"{initial}\n{follow_up}", public_schedule=schedule)
                self.assertEqual((status, header["status"]), (0, "draft"))
                self.assertEqual(len(files), 1)
                self.assertIn(body, [p.text for p in Document(files[0].removeprefix("MEDIA:")).paragraphs])
                receipt = json.loads((Path(header["output_directory"]) / "运行回执.json").read_text())
                self.assertEqual(receipt["original_request"], f"{initial}\n{follow_up}\n{arrangement}")
                self.assertEqual(receipt["expert_workflow"]["draft_mode"], "quick_notice")
                self.assertNotIn("conference_review", receipt)
                self.assertEqual(len(writes), 1)

    def test_natural_text_requests_select_no_export(self):
        for prompt in ("领物资，请算出总共多少套并写在通知里，先给正文。",
                       "数量需核对，先给一份可核对的文字。", "请只给我正文。"):
            with self.subTest(prompt=prompt):
                self.assertIsNotNone(entry.TEXT_ONLY_REQUEST.search(prompt))
        self.assertIsNone(entry.TEXT_ONLY_REQUEST.search("请给我 Word 审阅稿。"))

    def test_blocked_delivery_keeps_stage_code_and_reference_without_private_error(self):
        with TemporaryDirectory() as temp:
            output = Path(temp) / ("a" * 24)
            output.mkdir()
            (output / "运行回执.json").write_text(json.dumps({
                "status": "needs_review", "message": "最终核验未完成。",
                "conference_review": {"status": "incomplete", "failed_stage": "task_evidence",
                                      "error": {"code": "REVIEW_BINDING_INVALID", "message": "private detail"}},
            }))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                deliver(output)
            line, = stdout.getvalue().splitlines()
            receipt = json.loads(line)
            self.assertEqual(receipt["diagnostic"], {
                "stage": "task_evidence", "code": "REVIEW_BINDING_INVALID", "reference": output.name,
            })
            self.assertEqual(receipt["delivery_message"], entry.WORD_NOT_READY)
            self.assertNotIn(output.name, receipt["delivery_message"])
            self.assertNotIn("private detail", line)

    def test_recovery_uses_only_the_original_owned_receipt_and_does_not_generate(self):
        owner = dict(session_id="telegram-thread", consumer_id="telegram-owner", invocation_id="new", run_id="new")
        candidate = {
            "profile": "plan-draft", "title": "服务切换会商稿", "material_uses": [], "questions": [],
            "sections": [{"title": "切换安排", "paragraphs": [
                {"text": "切换在01:00进行。", "kind": "fact", "anchors": ["U.1"]}]}],
            "tasks": [{"action": "核对纸质登记", "lead": "服务窗口", "support": "", "timing": "第1时段",
                       "deliverable": "登记清单", "acceptance": "", "status": "confirmed", "anchors": ["U.1"]}],
            "review_notes": [],
        }
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            completed = root / ("a" * 24)
            rejected = root / ("b" * 24)
            for parent, code in ((completed, 0), (rejected, 2)):
                parent.mkdir()
                parent.with_suffix(".attempt.json").write_text(json.dumps({
                    **owner, "status": "finished", "exit_code": code,
                }))
            doc = Document()
            doc.add_paragraph(candidate["title"])
            doc.add_table(rows=2, cols=3)
            doc.add_table(rows=2, cols=2)
            original = completed / "正文.docx"
            doc.save(original)
            (completed / "运行回执.json").write_text(json.dumps({
                "status": "draft", "candidate": {"title": candidate["title"]},
                "artifacts": {original.name: hashlib.sha256(original.read_bytes()).hexdigest()},
            }))
            (rejected / "运行回执.json").write_text(json.dumps({
                "status": "needs_review", "message": "时间01:00缺少出处。", "rejected_candidate": candidate,
            }))

            def invoke(parent, action, identity=owner, prompt="找回原件"):
                argv = ["openagent_entry.py", "--native-input", "--continue-from", str(parent),
                        "--delivery-action", action]
                stdin = io.StringIO(json.dumps({"identity": identity, "input": {"user_prompt": prompt}}))
                stdout = io.StringIO()
                with patch.object(entry, "Path", side_effect=lambda p: root if p == "/oasn/out/gongwen" else Path(p)), \
                        patch.object(entry, "main", side_effect=AssertionError("must not call the model")), \
                        patch("sys.argv", argv), patch("sys.stdin", stdin), contextlib.redirect_stdout(stdout):
                    code = entry.run()
                header, *media = stdout.getvalue().splitlines()
                return code, json.loads(header), media

            for denied in ("只要正文。", "不要补发原件。", "再给我一份全新的消防培训通知。"):
                code, header, media = invoke(completed, "resend_word", prompt=denied)
                self.assertEqual((code, header["status"], media), (2, "failed", []))
            code, header, media = invoke(completed, "resend_word")
            self.assertEqual(code, 0)
            self.assertEqual(header["word_contents"]["table_count"], 2)
            self.assertEqual(len(media), 1)
            self.assertEqual(Path(media[0].removeprefix("MEDIA:")).read_bytes(), original.read_bytes())
            for wrong_owner in ({**owner, "consumer_id": "other"}, {**owner, "session_id": "other"}):
                code, header, media = invoke(rejected, "recover_draft", wrong_owner)
                self.assertEqual((code, header["status"], media), (2, "failed", []))
            self.assertFalse((rejected / "交付").exists())
            code, header, media = invoke(rejected, "resend_word")
            self.assertEqual((code, header["status"], media), (2, "failed", []))
            code, header, media = invoke(rejected, "recover_draft")
            self.assertEqual((code, header["status"], header["word_contents"]), (0, "recovered_draft", None))
            path = Path(media[0].removeprefix("MEDIA:"))
            self.assertEqual(path.name, "未通过核稿的原稿.txt")
            text = path.read_text()
            self.assertNotIn("时间01:00缺少出处", text)
            for expected in ("仅供核对", "切换在01:00进行。", "服务窗口", "第1时段"):
                self.assertIn(expected, text)
            self.assertEqual(invoke(rejected, "recover_draft")[2], media)
            self.assertFalse((rejected / "正文.docx").exists())
            entry.select_active_task(root, owner, rejected)
            code, header, media = invoke(completed, "resend_word")
            self.assertEqual((code, header["status"], media), (2, "failed", []))
            attempt = rejected.with_suffix(".attempt.json")
            binding = json.loads(attempt.read_bytes())
            binding["receipt_sha256"] = "0" * 64
            attempt.write_text(json.dumps(binding))
            self.assertEqual(invoke(rejected, "recover_draft")[0], 2)

    def test_failed_revision_keeps_the_current_word_available_for_the_next_turn(self):
        owner = dict(session_id="dashboard", consumer_id="owner", invocation_id="initial", run_id="initial")
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            parent = root / ("a" * 24)
            parent.mkdir()
            parent.with_suffix(".attempt.json").write_text(json.dumps({
                **owner, "status": "finished", "exit_code": 0,
            }))
            entry.select_active_task(root, owner, parent)

            def writer(args):
                output = Path(args[args.index("--output") + 1])
                if args[0] == "改了再说":
                    raise SystemExit(2)
                self.assertEqual(Path(args[args.index("--continue-from") + 1]), parent)
                output.mkdir()
                doc = Document()
                doc.add_paragraph("服务组次日11:00前汇总。")
                word = output / "正文.docx"
                doc.save(word)
                (output / "运行回执.json").write_text(json.dumps({
                    "status": "revision", "parent_name": "原会商稿.docx",
                    "artifacts": {"正文.docx": hashlib.sha256(word.read_bytes()).hexdigest()},
                }))
                return 0

            def invoke(turn, prompt):
                identity = {**owner, "invocation_id": turn, "run_id": turn}
                data = io.StringIO(json.dumps({"identity": identity, "input": {"user_prompt": prompt}}))
                with patch("sys.stdin", data), contextlib.redirect_stdout(io.StringIO()):
                    return entry.run()

            with patch.object(entry, "Path", side_effect=lambda p: root if p == "/oasn/out/gongwen" else Path(p)), \
                    patch.object(entry, "main", writer), \
                    patch.dict("os.environ", {"OPENAGENT_CHAT_MODEL": "test"}), \
                    patch("sys.argv", ["openagent_entry.py", "--native-input", "--continue-from", str(parent)]):
                self.assertEqual(invoke("first", "改了再说"), 2)
                self.assertEqual(entry.active_task(root, owner), parent)
                self.assertEqual(invoke("second", "汇总时点从次日10:00前改到次日11:00前。"), 0)
                revised = entry.active_task(root, owner)
                self.assertNotEqual(revised, parent)
                self.assertEqual(Document(revised / "正文.docx").paragraphs[0].text, "服务组次日11:00前汇总。")

    def test_delivery_description_follows_actual_word_not_requested_table(self):
        for table_count in (0, 1, 2):
            with self.subTest(table_count=table_count), TemporaryDirectory() as temp:
                folder = Path(temp)
                document = Document()
                document.add_paragraph('关于召开居民议事会的通知')
                for _ in range(table_count):
                    document.add_table(rows=2, cols=3)
                word = folder / '正文.docx'
                document.save(word)
                (folder / '运行回执.json').write_text(json.dumps({
                    'status': 'draft', 'message': '已独立核稿',
                    'original_request': '附一张议题表',
                    'candidate': {'tasks': [{'action': '议题表'}]},
                    'artifacts': {'正文.docx': hashlib.sha256(word.read_bytes()).hexdigest()}}))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    deliver(folder)
                header = json.loads(output.getvalue().splitlines()[0])
                self.assertEqual(header.get('word_contents'), {
                    'file_name': '关于召开居民议事会的通知.docx', 'table_count': table_count})
                self.assertEqual(header.get('delivery_message'), 'Word写好了，可以直接修改。')
                self.assertIn('MEDIA:' + str(folder / '交付' / '关于召开居民议事会的通知.docx'), output.getvalue().splitlines())

    def test_delivery_description_does_not_announce_word_for_incomplete_result(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / '运行回执.json').write_text(json.dumps({
                'status': 'needs_review', 'message': '内部校验未通过，请维护方核对。',
                'artifacts': {}}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                deliver(folder)
            header = json.loads(output.getvalue().splitlines()[0])
            self.assertEqual(header.get('delivery_message'), entry.WORD_NOT_READY)
            self.assertIsNone(header.get('word_contents'))

    def test_delivery_description_keeps_review_warning(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            word = folder / '正文.docx'
            Document().save(word)
            (folder / '运行回执.json').write_text(json.dumps({
                'status': 'draft', 'message': '初稿待核',
                'conference_review': {'review': {'findings': [{'level': 'pending', 'defect': '会议地点尚未确定。'}]}},
                'artifacts': {'正文.docx': hashlib.sha256(word.read_bytes()).hexdigest()}}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                deliver(folder)
            header = json.loads(output.getvalue().splitlines()[0])
            self.assertIn('会议地点尚未确定', header.get('delivery_message', ''))
            self.assertNotIn('依据与待核', header['delivery_message'])

    def test_exporter_change_does_not_reuse_previous_native_artifact(self):
        identity = dict(session_id='same-session', consumer_id='owner',
                        invocation_id='call', run_id='run')
        current = {'user_prompt': '写培训通知', 'attachments': []}
        calls = []

        def failing_main(argv):
            calls.append(argv)
            raise SystemExit(1)

        with TemporaryDirectory() as temp:
            root = Path(temp)
            exporter = root / 'draft_layout.py'
            exporter.write_text('first layout')
            covered_assets = {name: root / name for name in ('minute_actions.py', 'notice_scope.py')}
            for asset in covered_assets.values():
                asset.write_text('first rules')

            def native_path(value):
                return root / 'out' if str(value) == '/oasn/out/gongwen' else Path(value)

            original_read = Path.read_bytes

            def read_bytes(path):
                if str(path).endswith('/app/draft_layout.py'):
                    return original_read(exporter)
                if path.parent.name == 'app' and path.name in covered_assets:
                    return original_read(covered_assets[path.name])
                return original_read(path)

            with patch.object(entry, 'Path', side_effect=native_path), \
                    patch.object(entry, 'load_input', return_value=(identity, current, b'current-input')), \
                    patch.object(entry, 'main', failing_main), \
                    patch.object(Path, 'read_bytes', read_bytes), \
                    patch.dict('os.environ', {'OPENAGENT_CHAT_MODEL': 'unit-test-model'}), \
                    patch('sys.argv', ['openagent_entry.py']), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(entry.run(), 1)
                exporter.write_text('repaired layout')
                self.assertEqual(entry.run(), 1)
                for asset in covered_assets.values():
                    asset.write_text('repaired rules')
                    self.assertEqual(entry.run(), 1)
            self.assertEqual(len(calls), 4)
            self.assertEqual(len(list((root / 'out').glob('*.attempt.json'))), 4)

    def test_cli_system_exit_finishes_failed_attempt_without_replaying(self):
        identity = dict(session_id='same-session', consumer_id='owner',
                        invocation_id='call', run_id='run')
        current = {'user_prompt': '写培训通知', 'attachments': []}
        input_hash = hashlib.sha256(current['user_prompt'].encode()).hexdigest()
        calls = []

        def failing_main(argv):
            attempt, = output_root.glob('*.attempt.json')
            self.assertEqual(json.loads(attempt.read_text())['input_sha256'], input_hash)
            calls.append(argv)
            try:
                raise RevisionIssue('MODEL_FAILED', '托管模型 HTTP 403；调用编号 test-call。')
            except RevisionIssue:
                raise SystemExit(1)

        with TemporaryDirectory() as temp:
            output_root = Path(temp) / 'out'

            def native_path(value):
                return output_root if str(value) == '/oasn/out/gongwen' else Path(value)

            output = io.StringIO()
            with patch.object(entry, 'Path', side_effect=native_path), \
                    patch.object(entry, 'load_input', return_value=(identity, current, b'current-input')), \
                    patch.object(entry, 'main', failing_main), \
                    patch.dict('os.environ', {'OPENAGENT_CHAT_MODEL': 'unit-test-model'}), \
                    patch('sys.argv', ['openagent_entry.py']), contextlib.redirect_stdout(output):
                self.assertEqual(entry.run(), 1)
                attempts = list(output_root.glob('*.attempt.json'))
                self.assertEqual(len(attempts), 1)
                saved = json.loads(attempts[0].read_text())
                self.assertEqual(saved['input_sha256'], input_hash)
                self.assertEqual(saved['status'], 'finished')
                self.assertEqual(saved['exit_code'], 1)
                self.assertEqual(saved['error_code'], 'MODEL_FAILED')
                self.assertEqual(entry.run(), 1)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('MEDIA:', output.getvalue())
            self.assertIn('原调用已失败', output.getvalue())

    def test_text_only_runtime_exception_is_private_and_attempt_is_closed(self):
        identity = dict(session_id='same-session', consumer_id='owner', invocation_id='call', run_id='run')
        current = {'user_prompt': '请先给正文，不要文件。', 'attachments': []}
        calls = []

        def failing_main(argv):
            calls.append(argv)
            destination = Path(argv[argv.index('--output') + 1])
            destination.mkdir(parents=True)
            word = destination / '正文.docx'
            Document().save(word)
            receipt = {'status': 'draft', 'candidate': {'title': '不完整的中间文件'},
                       'artifacts': {'正文.docx': hashlib.sha256(word.read_bytes()).hexdigest()}}
            (destination / '运行回执.json').write_text(json.dumps(receipt))
            raise AttributeError('private-error-with-full-input-and-id')

        with TemporaryDirectory() as temp:
            root = Path(temp) / 'out'
            def native_path(value):
                return root if str(value) == '/oasn/out/gongwen' else Path(value)
            output = io.StringIO()
            with patch.object(entry, 'Path', side_effect=native_path), \
                    patch.object(entry, 'load_input', return_value=(identity, current, b'text-input')), \
                    patch.object(entry, 'main', failing_main), \
                    patch.dict('os.environ', {'OPENAGENT_CHAT_MODEL': 'unit-test-model'}), \
                    patch('sys.argv', ['openagent_entry.py']), contextlib.redirect_stdout(output):
                self.assertEqual(entry.run(), 1)
                self.assertEqual(entry.run(), 1)
            attempt, = root.glob('*.attempt.json')
            saved = json.loads(attempt.read_bytes())
            self.assertEqual(saved['status'], 'finished')
            self.assertEqual(saved['error_code'], 'PROGRAM_ERROR')
            self.assertEqual(saved['runtime_failure']['type'], 'AttributeError')
            self.assertTrue(saved['runtime_failure']['frames'])
            self.assertEqual(len(calls), 1)
            self.assertIn('正文暂未完成', output.getvalue())
            for internal in ('PROGRAM_ERROR', 'AttributeError', 'private-error', 'MEDIA:', 'Word'):
                self.assertNotIn(internal, output.getvalue())
            self.assertFalse(list(root.rglob('交付/*.docx')))

    def test_attachment_requires_explicit_trusted_current_binding(self):
        with TemporaryDirectory() as temp:
            p = Path(temp) / 'other-session.docx'
            p.write_bytes(b'private')
            with self.assertRaises(ValueError):
                require_attachment_binding([p], {'attachments': [str(p)]})
            require_attachment_binding([p], {'authorized_attachments': [str(p)]})

    def test_cached_incomplete_status_is_preserved(self):
        with TemporaryDirectory() as temp:
            p = Path(temp) / 'run'
            p.with_suffix('.attempt.json').write_text(json.dumps({'status': 'finished', 'exit_code': 2}))
            self.assertEqual(cached_exit_code(p), 2)
            p.with_suffix('.attempt.json').write_text(json.dumps({'status': 'started'}))
            with self.assertRaises(ValueError):
                cached_exit_code(p)

    def test_missing_receipt_word_is_not_announced(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / '运行回执.json').write_text(json.dumps({'status': 'draft', 'artifacts': {'正文.docx': 'hash'}}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(ValueError):
                deliver(folder)
            self.assertEqual(output.getvalue(), '')

    def test_webchat_without_formal_projection_uses_trusted_tool_input(self):
        envelope = {'identity': {'session_id': 'runtime-session', 'consumer_id': 'sender',
                                'invocation_id': 'tool-call', 'run_id': 'tool-call'},
                    'input': {'user_prompt': '写培训通知', 'attachments': []}}
        with TemporaryDirectory() as temp:
            identity, current, binding = load_input(io.StringIO(json.dumps(envelope)), Path(temp) / 'absent.json')
        self.assertEqual(identity['session_id'], 'runtime-session')
        self.assertEqual(current['user_prompt'], '写培训通知')
        self.assertNotIn(b'identity_secret', binding)

    def test_webchat_requires_runtime_identity(self):
        with self.assertRaises(ValueError):
            load_input(io.StringIO(json.dumps({'input': {'user_prompt': '写通知'}})))

    def test_same_prompt_reuses_only_the_same_runtime_run(self):
        envelope = {'identity': {'session_id': 'telegram-thread', 'consumer_id': 'sender',
                                 'invocation_id': 'tool-call', 'run_id': 'first-run'},
                    'input': {'user_prompt': '写同一份会商稿', 'attachments': []}}
        _, _, first = load_input(io.StringIO(json.dumps(envelope)))
        envelope['identity']['invocation_id'] = 'retried-tool-call'
        _, _, retry = load_input(io.StringIO(json.dumps(envelope)))
        self.assertEqual(first, retry)
        envelope['identity']['run_id'] = 'next-user-run'
        _, _, next_run = load_input(io.StringIO(json.dumps(envelope)))
        self.assertNotEqual(first, next_run)

    def test_formal_input_remains_authoritative(self):
        with TemporaryDirectory() as temp:
            p = Path(temp) / 'invocation.json'
            p.write_text(json.dumps({'session_id': 'formal-session', 'invocation_id': 'inv',
                                    'run_id': 'run', 'input': {'user_prompt': '正式原话', 'attachments': []},
                                    'identity_secret': 'must-not-be-copied'}))
            identity, current, binding = load_input(None, p)
        self.assertEqual(current['user_prompt'], '正式原话')
        self.assertNotIn(b'must-not-be-copied', binding)

    def test_requires_actual_invocation_identity(self):
        for missing in ('session_id', 'invocation_id', 'run_id'):
            current = dict(session_id='same-session', invocation_id='current-call', run_id='current-run')
            current.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                invocation_identity(current)

    def test_parent_accepts_same_session_continuation(self):
        with TemporaryDirectory() as temp:
            parent = Path(temp) / 'parent'
            parent.with_suffix('.attempt.json').write_text(json.dumps({
                'session_id': 'same-session', 'consumer_id': 'owner',
                'invocation_id': 'old-call', 'run_id': 'old-run'}))
            require_parent_binding(parent, {'session_id': 'same-session', 'consumer_id': 'owner',
                                           'invocation_id': 'new-call', 'run_id': 'new-run'})

    def test_parent_rejects_other_session_or_consumer(self):
        with TemporaryDirectory() as temp:
            parent = Path(temp) / 'parent'
            parent.with_suffix('.attempt.json').write_text(json.dumps({
                'session_id': 'same-session', 'consumer_id': 'owner'}))
            for identity in ({'session_id': 'other-session', 'consumer_id': 'owner'},
                             {'session_id': 'same-session', 'consumer_id': 'other-owner'}):
                with self.subTest(identity=identity), self.assertRaises(ValueError):
                    require_parent_binding(parent, identity)

    def test_parent_without_binding_is_not_guessed(self):
        with TemporaryDirectory() as temp:
            parent = Path(temp) / 'parent'
            with self.assertRaises(ValueError):
                require_parent_binding(parent, {'session_id': 'same-session', 'consumer_id': None})

    def test_revision_sends_only_current_word_not_parent_or_sidecars(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            names = ('正文.docx', '本轮父版.docx', '改稿短表.docx', '原附件2.docx', '变化对照.md')
            for name in names:
                if name.endswith('.docx'):
                    Document().save(folder / name)
                else:
                    (folder / name).write_bytes(name.encode())
            (folder / '运行回执.json').write_text(json.dumps({'status': 'revision', 'artifacts': {
                name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in names}}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                deliver(folder)
            media = [x for x in output.getvalue().splitlines() if x.startswith('MEDIA:')]
            self.assertEqual(media, ['MEDIA:' + str(folder / '交付' / '公文稿.docx')])
            for name in names:
                self.assertTrue((folder / name).is_file())

    def test_tampered_word_is_not_announced(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / '正文.docx').write_bytes(b'changed')
            (folder / '运行回执.json').write_text(json.dumps({'artifacts': {
                '正文.docx': hashlib.sha256(b'original').hexdigest()}}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(ValueError):
                deliver(folder)
            self.assertNotIn('MEDIA:', output.getvalue())

    def test_surfaces_existing_review_findings(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / '运行回执.json').write_text(json.dumps({
                'status': 'draft', 'message': '无需定点返修',
                'conference_review': {'review': {'findings': [{'level': 'pending', 'defect': '会议地点尚未确定。'}]}}}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                deliver(folder)
            self.assertIn('会议地点尚未确定', output.getvalue())
            self.assertNotIn('无需定点返修', output.getvalue())
