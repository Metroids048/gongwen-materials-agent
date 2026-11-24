"""Check the real outbound exchange contract without making model calls."""
import contextlib
from email.message import Message
from functools import partial
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import openagent_entry as entry
from app import agent
from app.agent import Candidate, ExpertBrief, VerifiedConferenceDraft
from app.revision.document import RevisionIssue

module_path = Path(__file__).resolve().parents[1] / "app/runtime/openagent_model.py"
spec = importlib.util.spec_from_file_location("managed_transport", module_path)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)
codex_spec = importlib.util.spec_from_file_location("managed_codex", module_path.with_name("codex.py"))
managed_codex = importlib.util.module_from_spec(codex_spec)
codex_spec.loader.exec_module(managed_codex)


class ManagedExchangeTests(unittest.TestCase):
    def test_native_502_records_managed_failure_without_claiming_delivery_or_replaying(self):
        headers = Message()
        headers['X-Request-Id'] = 'gw-502-original'
        response = io.BytesIO(b'{"error":{"code":"UPSTREAM_CONNECT_ERROR","message":"private-payload"}}')
        error = HTTPError('https://model.invalid', 502, 'Bad Gateway', headers, response)
        env = {'OPENAGENT_CHAT_BASE_URL': 'https://model.invalid', 'OPENAGENT_CHAT_MODEL': 'gpt-test',
               'OPENAGENT_CHAT_INTERFACE_TYPE': 'openai-responses', 'OPENAGENT_MODEL_API_KEY': 'unit-test-only'}
        def respond(request, *, timeout):
            self.assertIsNone(timeout)
            body = json.loads(request.data)
            self.assertEqual(body['text']['format']['name'], 'Candidate')
            raise error

        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            output = root / 'output'
            output.mkdir()
            inputs = root / 'input'
            inputs.mkdir()
            attachment = inputs / '背景.txt'
            attachment.write_text('本轮材料写会商方案', encoding='utf-8')
            envelope = json.dumps({
                'identity': dict(session_id='session', consumer_id='owner', invocation_id='call', run_id='run'),
                'input': {'user_prompt': '请把附件整理成会商方案。', 'attachments': [str(attachment)],
                          'authorized_attachments': [str(attachment)]},
            })

            def native_path(value):
                return {'/oasn/out/gongwen': output, '/oasn/in': inputs}.get(str(value), Path(value))

            stdout = io.StringIO()
            with patch.dict(os.environ, env), patch.dict(sys.modules, {'app.runtime.openagent_model': transport}), \
                    patch.object(transport, 'urlopen', side_effect=respond) as send, \
                    patch.object(agent, 'run_task', partial(agent.run_task, review_generate=partial(
                        managed_codex.ask_codex, **agent.generate_review.keywords))), \
                    patch.object(agent, 'generate_intake', partial(managed_codex.ask_codex, **agent.generate_intake.keywords)), \
                    patch.object(entry, 'Path', side_effect=native_path), \
                    patch('sys.argv', ['openagent_entry.py', '--native-input']), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                with patch('sys.stdin', io.StringIO(envelope)):
                    self.assertEqual(entry.run(), 1)
                attempt = next(output.glob('*.attempt.json')).read_text()
                with patch('sys.stdin', io.StringIO(envelope)):
                    self.assertEqual(entry.run(), 1)
            send.assert_called_once()
            self.assertFalse(list(output.rglob('*.docx')))
        saved = json.loads(attempt)
        self.assertEqual(saved['status'], 'finished')
        self.assertEqual(saved['error_code'], 'JOB_OUTCOME_UNKNOWN')
        self.assertGreaterEqual(saved['program_elapsed_seconds'], 0)
        failure = saved['model_failure']
        self.assertEqual(failure['stage'], 'draft')
        self.assertNotIn('brief', failure)
        self.assertEqual(failure['response_schema'], 'Candidate')
        self.assertEqual(failure['reasoning_effort'], 'medium')
        candidate_body = json.loads(send.call_args.args[0].data)
        self.assertEqual(candidate_body['reasoning']['effort'], 'medium')
        self.assertEqual(candidate_body['text']['format']['schema'], Candidate.model_json_schema())
        self.assertIsNone(failure['timeout_seconds'])
        self.assertEqual(failure['interface'], 'openai-responses')
        self.assertEqual(failure['http_status'], 502)
        self.assertEqual(failure['gateway_request_id'], 'gw-502-original')
        self.assertEqual(failure['gateway_error_code'], 'UPSTREAM_CONNECT_ERROR')
        self.assertEqual(failure['upstream_call_id'], send.call_args.args[0].get_header('X-oasn-upstream-call-id'))
        self.assertGreaterEqual(failure['response_headers_seconds'], 0)
        self.assertGreaterEqual(failure['elapsed_seconds'], failure['response_headers_seconds'])
        message = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(message['status'], 'failed')
        self.assertEqual(message['delivery_message'], entry.WORD_NOT_READY)
        self.assertNotIn(failure['upstream_call_id'], message['delivery_message'])
        self.assertIsNone(message['word_contents'])
        for private in ('unit-test-only', 'private-payload', '请把附件整理成会商方案', '本轮材料写会商方案', 'MEDIA:'):
            self.assertNotIn(private, attempt + stdout.getvalue())

    def test_both_managed_interfaces_send_the_original_strict_schema(self):
        for interface in ("openai-completions", "openai-responses"):
            with self.subTest(interface=interface):
                reply = {"input_key": "bound", "items": [], "outline": []}
                content = json.dumps(reply)
                payload = ({"choices": [{"message": {"content": content}}]}
                           if interface == "openai-completions" else {"output_text": content})
                with patch.dict(os.environ, {
                    "OPENAGENT_CHAT_BASE_URL": "https://model.invalid",
                    "OPENAGENT_CHAT_MODEL": "gpt-test",
                    "OPENAGENT_CHAT_INTERFACE_TYPE": interface,
                    "OPENAGENT_MODEL_API_KEY": "unit-test-only",
                }), patch.object(transport, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as send:
                    result, _ = transport.ask_platform("素材", ExpertBrief, timeout=10, reasoning_effort="medium")
                body = json.loads(send.call_args.args[0].data)
                fmt = body.get("response_format", body.get("text", {}).get("format"))
                self.assertEqual(fmt["type"], "json_schema")
                constraint = fmt.get("json_schema", fmt)
                self.assertTrue(constraint["strict"])
                self.assertEqual(constraint["schema"], ExpertBrief.model_json_schema())
                self.assertIn("blocks", constraint["schema"]["$defs"]["SourcedBriefItem"]["properties"]["anchors"]["description"])
                if interface == "openai-completions":
                    instruction, material = (m['content'] for m in body['messages'])
                else:
                    instruction, material = body['instructions'], body['input']
                self.assertEqual(material, '素材')
                self.assertNotIn('"$defs"', instruction)
                self.assertIn('保留所有材料出处、待核和修改范围限制', instruction)
                self.assertEqual(result.input_key, "bound")
                self.assertEqual(body.get("reasoning_effort", body.get("reasoning", {}).get("effort")), "medium")

    def test_invalid_exchange_is_rejected_after_the_wire(self):
        with patch.dict(os.environ, {
            "OPENAGENT_CHAT_BASE_URL": "https://model.invalid", "OPENAGENT_CHAT_MODEL": "gpt-test",
            "OPENAGENT_CHAT_INTERFACE_TYPE": "openai-completions", "OPENAGENT_MODEL_API_KEY": "unit-test-only",
        }), patch.object(transport, "urlopen", return_value=io.BytesIO(
            json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()
        )):
            with self.assertRaises(RevisionIssue) as raised:
                transport.ask_platform("素材", Candidate, timeout=10, reasoning_effort="high")
        self.assertEqual(raised.exception.code, "MODEL_INVALID")
        self.assertEqual(raised.exception.rejected_exchange["content"], "{}")
        self.assertTrue(raised.exception.rejected_exchange["model"]["upstream_call_id"].startswith("gongwen-"))
        self.assertIn("validation_errors", raised.exception.rejected_exchange)
        self.assertNotIn("unit-test-only", json.dumps(raised.exception.rejected_exchange))

    def test_native_deepseek_uses_low_and_records_stage_without_request_content(self):
        reply = json.dumps({"input_key": "bound", "items": [], "outline": []})
        payload = {"choices": [{"message": {"content": reply}}]}
        with TemporaryDirectory() as temp, patch.dict(os.environ, {
            "OPENAGENT_CHAT_BASE_URL": "https://model.invalid",
            "OPENAGENT_CHAT_MODEL": "deepseek-v4-pro",
            "OPENAGENT_CHAT_INTERFACE_TYPE": "openai-completions",
            "OPENAGENT_MODEL_API_KEY": "unit-test-only",
        }), patch.object(transport, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as send:
            log = Path(temp) / "model.jsonl"
            token = transport.stage_log.set(log)
            try:
                _, metadata = transport.ask_platform("私有材料", ExpertBrief, timeout=None, reasoning_effort="medium")
            finally:
                transport.stage_log.reset(token)
            events = [json.loads(line) for line in log.read_text().splitlines()]
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(body["model"], "deepseek-v4-pro")
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["response_format"]["json_schema"]["schema"], ExpertBrief.model_json_schema())
        self.assertEqual([event["event"] for event in events], ["started", "request_ended"])
        self.assertTrue(all(event["upstream_call_id"] == metadata["upstream_call_id"] for event in events))
        self.assertNotIn("私有材料", str(events))
        self.assertNotIn("unit-test-only", str(events))

    def test_short_notice_keeps_the_candidate_schema_with_thinking_disabled(self):
        candidate = Candidate(profile="unclear", title="", sections=[], tasks=[], review_notes=[],
                              questions=["日期是什么？"], material_uses=[])
        payload = {"choices": [{"message": {"content": candidate.model_dump_json()}}]}
        with patch.dict(os.environ, {
            "OPENAGENT_CHAT_BASE_URL": "https://model.invalid",
            "OPENAGENT_CHAT_MODEL": "deepseek-v4-pro",
            "OPENAGENT_CHAT_INTERFACE_TYPE": "openai-completions",
            "OPENAGENT_MODEL_API_KEY": "unit-test-only",
        }), patch.object(transport, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as send:
            result, _ = transport.ask_platform("素材", Candidate, timeout=10, reasoning_effort="none")
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["response_format"]["json_schema"]["schema"], Candidate.model_json_schema())
        self.assertEqual(result.questions, candidate.questions)

    def test_final_conference_evidence_uses_low_without_changing_its_quote_schema(self):
        proof = VerifiedConferenceDraft(basis_key="bound", findings=[])
        payload = {"choices": [{"message": {"content": proof.model_dump_json()}}]}
        with patch.dict(os.environ, {
            "OPENAGENT_CHAT_BASE_URL": "https://model.invalid",
            "OPENAGENT_CHAT_MODEL": "deepseek-v4-pro",
            "OPENAGENT_CHAT_INTERFACE_TYPE": "openai-completions",
            "OPENAGENT_MODEL_API_KEY": "unit-test-only",
        }), patch.object(transport, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as send:
            result, _ = transport.ask_platform("返修稿和依据", VerifiedConferenceDraft, timeout=30,
                                               reasoning_effort="medium")
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["response_format"]["json_schema"]["schema"], VerifiedConferenceDraft.model_json_schema())
        self.assertEqual(result, proof)
