import json
import tempfile
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from unittest.mock import patch

from agent_brain import plan
from handoff import Snapshot, messages_for, parse_decision
from model_provider import DEEPSEEK_BASE, DEEPSEEK_MODEL, ModelProviderError, _destination, ask_model, verify_connection
from web_session import DesktopSession


class ChatStub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, self.headers.get("Authorization"), body))
        answer = self.server.answer
        data = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
        self.send_response(self.server.status_code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ModelConnectionTests(unittest.TestCase):
    def test_base_and_full_paths(self):
        for supplied, expected in [
            (DEEPSEEK_BASE, "/chat/completions"),
            (DEEPSEEK_BASE + "/", "/chat/completions"),
            (DEEPSEEK_BASE + "/v1", "/v1/chat/completions"),
            (DEEPSEEK_BASE + "/chat/completions/", "/chat/completions"),
            ("http://localhost:1234/v1/chat/completions", "/v1/chat/completions"),
            ("https://example.test/gateway/v1/", "/gateway/v1/chat/completions"),
        ]:
            with self.subTest(supplied=supplied):
                self.assertEqual(_destination(supplied)[3], expected)
        for url in ["http://example.test/v1", DEEPSEEK_BASE + "/anthropic", "https://platform.deepseek.com",
                    DEEPSEEK_BASE + "?secret=x", "https://user:pass@example.com/v1"]:
            with self.subTest(url=url), self.assertRaises(ModelProviderError):
                _destination(url)

    def test_deepseek_model_default_and_key(self):
        with patch("model_provider.ask_model", return_value='{"tool":"answer","text":"OK","args":{}}') as ask:
            self.assertEqual(verify_connection(DEEPSEEK_BASE, "", "demo"), DEEPSEEK_MODEL)
            self.assertEqual(ask.call_args.args[1], DEEPSEEK_MODEL)
            with self.assertRaisesRegex(ModelProviderError, "API Key"):
                verify_connection(DEEPSEEK_BASE, "", "")
            ask.assert_called_once()

    def start_provider(self):
        service = ThreadingHTTPServer(("127.0.0.1", 0), ChatStub)
        service.calls = []
        service.status_code = 200
        service.answer = '{"tool":"answer","text":"已收到测试问题","args":{}}'
        Thread(target=service.serve_forever, daemon=True).start()
        self.addCleanup(service.server_close)
        self.addCleanup(service.shutdown)
        return service

    def test_official_wire_format_for_agent_handoff_and_plain_chat(self):
        service = self.start_provider()
        transport = lambda *a, **kw: HTTPConnection("127.0.0.1", service.server_port, timeout=2)
        with tempfile.TemporaryDirectory() as folder, patch("model_provider.http.client.HTTPSConnection", side_effect=transport):
            session = DesktopSession(folder)
            session.dispatch("model", {"endpoint": DEEPSEEK_BASE, "key": "demo"})
            decision = plan("怎样提高进口商的等级？", [], {}, session.ask)
            self.assertEqual(decision["text"], "已收到测试问题")
            self.assertNotIn("notice", decision)
            chat_prompt = service.calls[-1][2]["messages"][0]["content"]
            self.assertIn('{"tool":"answer","args":{},"text":', chat_prompt)
            state = Snapshot("A", "importer", "JOB-1", "FOB", "T/T", "payment_result_unknown",
                             ("receipt_pending",), ("read_payment_ledger",), 1)
            service.answer = '{"decision":"choose","action":"read_payment_ledger","why":"核对付款"}'
            self.assertEqual(parse_decision(session.ask(messages_for(state)), state)[1], "read_payment_ledger")
            self.assertIn('{"decision":"choose","action":', service.calls[-1][2]["messages"][0]["content"])
            for _, _, body in service.calls:
                self.assertEqual(body["response_format"], {"type": "json_object"})
            service.answer = "plain text"
            reply = ask_model(DEEPSEEK_BASE, DEEPSEEK_MODEL, "demo", [{"role": "user", "content": "hi"}])
            self.assertEqual(reply, "plain text")
            self.assertNotIn("response_format", service.calls[-1][2])

    def test_session_probe_chat_errors_and_failed_replacement(self):
        service = self.start_provider()
        with tempfile.TemporaryDirectory() as folder:
            session = DesktopSession(folder)
            settings = {"endpoint": f"http://127.0.0.1:{service.server_port}/v1", "model": "example", "key": "test-private"}
            session.dispatch("model", settings)
            self.assertTrue(session.state()["model_enabled"])
            self.assertEqual(session.state()["model_name"], "example")
            _, auth, probe = service.calls[0]
            self.assertEqual(auth, "Bearer test-private")
            self.assertNotIn("test-private", json.dumps(probe))
            self.assertNotIn("response_format", probe)
            self.assertNotIn("已核实的工作台状态", str(probe))

            def chat(request_id):
                session.dispatch("chat", {"request_id":request_id, "message":"商品多样性如何影响成交？"})
                deadline = time.monotonic() + 3
                while session.state()["agent"]["busy"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(session.state()["agent"]["busy"])
                return [m["text"] for m in session.state()["agent"]["messages"]]

            self.assertIn("已收到测试问题", chat("turn-000001"))
            self.assertEqual([call[0] for call in service.calls], ["/v1/chat/completions"] * 2)
            self.assertEqual(service.calls[1][2]["messages"][-1]["content"], "商品多样性如何影响成交？")
            self.assertNotIn("test-private", json.dumps(session.state()))
            for code, hint in ((401, "API Key"), (402, "额度"), (429, "限制")):
                service.status_code = code
                with self.subTest(code=code), self.assertRaisesRegex(ValueError, hint):
                    session.dispatch("model", {**settings, "model": "replacement"})
                self.assertEqual(session.state()["model_name"], "example")
            self.assertTrue(any("HTTP 429" in msg for msg in chat("turn-000002")))
            service.status_code = 200
            service.answer = "unstructured text"
            with self.assertRaisesRegex(ValueError, "动作格式"):
                session.dispatch("model", settings)
            session.dispatch("model", {})
            self.assertFalse(session.state()["model_enabled"])

    def test_in_flight_probe_cannot_reenable_disabled_model(self):
        started, finish = Event(), Event()
        errors = []
        def probe(*args):
            started.set()
            finish.wait(3)
            return DEEPSEEK_MODEL
        with tempfile.TemporaryDirectory() as folder, patch("web_session.verify_connection", side_effect=probe):
            session = DesktopSession(folder)
            def configure():
                try:
                    session.dispatch("model", {"endpoint": DEEPSEEK_BASE, "key": "demo"})
                except ValueError as exc:
                    errors.append(str(exc))
            worker = Thread(target=configure)
            worker.start()
            self.assertTrue(started.wait(2))
            session.dispatch("model", {})
            finish.set()
            worker.join(3)
            self.assertFalse(session.state()["model_enabled"])
            self.assertTrue(any("过期" in message for message in errors))
