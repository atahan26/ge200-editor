"""Provider layer tests against a local fake HTTP server (no network, no keys, synthetic presets)."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from ge200 import llm, tone
from tests.test_tone import good_answer, make_presets


class FakeService:
    """A tiny HTTP server that records requests and answers from a queue of (status, json) tuples."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": json.loads(self.rfile.read(n))})
                status, payload = outer.replies.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def chat(text, finish="stop", **msg):
    return 200, {"choices": [{"message": {"content": text, **msg}, "finish_reason": finish}]}


class OpenAIStyleTests(unittest.TestCase):
    def setUp(self):
        self.presets = make_presets()
        self.base = self.presets[1]

    def provider(self, svc, **kw):
        return llm.OpenAIStyleProvider("openai", "OpenAI", svc.url, ["TEST_KEY"], **kw)

    def test_request_shape_and_answer(self):
        svc = FakeService([chat(good_answer())])
        self.addCleanup(svc.close)
        with mock.patch.dict(os.environ, {"TEST_KEY": "sk-secret"}):
            res = tone.generate("warm clean", self.base, self.presets, provider=self.provider(svc, token_param="max_completion_tokens"),
                                model="some-model")
        req = svc.requests[0]
        self.assertEqual(req["path"], "/chat/completions")
        self.assertEqual(req["headers"]["authorization"], "Bearer sk-secret")
        b = req["body"]
        self.assertEqual(b["model"], "some-model")
        self.assertEqual([m["role"] for m in b["messages"]], ["system", "user"])
        self.assertEqual(b["response_format"]["type"], "json_schema")
        self.assertTrue(b["response_format"]["json_schema"]["strict"])
        self.assertIn("max_completion_tokens", b)
        self.assertNotIn("temperature", b)
        self.assertIn("warm clean", b["messages"][1]["content"])
        self.assertEqual(res["edit"]["name"], "DREAM CLEAN")

    def test_falls_back_to_plain_json_mode_when_schema_is_rejected(self):
        svc = FakeService([(400, {"error": {"message": "response_format json_schema is not supported"}}),
                           chat(good_answer())])
        self.addCleanup(svc.close)
        p = self.provider(svc, needs_key=False)
        res = tone.generate("x", self.base, self.presets, provider=p, model="m")
        self.assertEqual(svc.requests[0]["body"]["response_format"]["type"], "json_schema")
        second = svc.requests[1]["body"]
        self.assertEqual(second["response_format"], {"type": "json_object"})
        self.assertIn("JSON schema", second["messages"][0]["content"])      # schema now travels in the prompt
        self.assertEqual(res["edit"]["name"], "DREAM CLEAN")

    def test_errors_never_contain_the_key(self):
        svc = FakeService([(401, {"error": {"message": "bad key sk-secret"}}), (429, {}), (500, {"error": {"message": "boom"}})])
        self.addCleanup(svc.close)
        p = self.provider(svc)
        with mock.patch.dict(os.environ, {"TEST_KEY": "sk-secret"}):
            for needle in ("authentication failed", "rate limited", "API error 500"):
                with self.assertRaises(tone.ToneError) as cm:
                    tone.generate("x", self.base, self.presets, provider=p, model="m")
                self.assertIn(needle, str(cm.exception))
                self.assertNotIn("sk-secret", str(cm.exception))

    def test_missing_key_refusal_and_truncation(self):
        svc = FakeService([chat("", refusal="no"), chat(good_answer(), finish="length")] * 2)
        self.addCleanup(svc.close)
        p = self.provider(svc)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_KEY", None)
            with self.assertRaises(tone.ToneError) as cm:
                tone.generate("x", self.base, self.presets, provider=p, model="m")
            self.assertIn("API key", str(cm.exception))
        with mock.patch.dict(os.environ, {"TEST_KEY": "k"}):
            with self.assertRaises(tone.ToneError) as cm:
                tone.generate("x", self.base, self.presets, provider=p, model="m")
            self.assertIn("no usable answer", str(cm.exception))

    def test_bad_json_is_retried_once(self):
        svc = FakeService([chat("not json"), chat(good_answer())])
        self.addCleanup(svc.close)
        res = tone.generate("x", self.base, self.presets, provider=self.provider(svc, needs_key=False), model="m")
        self.assertEqual(len(svc.requests), 2)
        self.assertEqual(res["edit"]["name"], "DREAM CLEAN")

    def test_model_name_is_validated(self):
        with self.assertRaises(llm.LlmError):
            llm.check_model("../../etc")
        with self.assertRaises(llm.LlmError):
            llm.check_model("a b")
        self.assertEqual(llm.check_model("openai/gpt-x:free"), "openai/gpt-x:free")


class GeminiTests(unittest.TestCase):
    def test_request_shape(self):
        presets = make_presets()
        svc = FakeService([(200, {"candidates": [{"content": {"parts": [{"text": good_answer()}]}, "finishReason": "STOP"}]})])
        self.addCleanup(svc.close)
        p = llm.GeminiProvider()
        p.BASE = svc.url
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g-secret"}):
            res = tone.generate("lead", presets[1], presets, provider=p, model="some-model")
        req = svc.requests[0]
        self.assertEqual(req["path"], "/models/some-model:generateContent")
        self.assertEqual(req["headers"]["x-goog-api-key"], "g-secret")
        self.assertNotIn("g-secret", req["path"])                          # key goes in a header, never the URL
        self.assertEqual(req["body"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("JSON schema", req["body"]["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(res["edit"]["name"], "DREAM CLEAN")

    def test_blocked_and_truncated(self):
        presets = make_presets()
        svc = FakeService([(200, {"promptFeedback": {"blockReason": "SAFETY"}})] * 2)
        self.addCleanup(svc.close)
        p = llm.GeminiProvider()
        p.BASE = svc.url
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
            with self.assertRaises(tone.ToneError):
                tone.generate("x", presets[1], presets, provider=p, model="m")
        with self.assertRaises(llm.LlmError):
            llm.GeminiProvider().complete("s", "u", {}, "bad/model")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY",
                  "GE200_LLM_PROVIDER", "GE200_LLM_MODEL", "GE200_LLM_BASE_URL", "GE200_LLM_API_KEY"):
            os.environ.pop(k, None)
        os.environ["GE200_LLM_CONFIG"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "llm.json")
        llm._cache.clear()
        self.noenv = mock.patch("ge200.env.load_env", lambda *a, **k: None)
        self.noenv.start()

    def tearDown(self):
        self.noenv.stop()
        self.env.stop()
        llm._cache.clear()

    def info(self):
        return {i["id"]: i for i in llm.overview()["providers"]}

    def test_overview_lists_all_providers_with_reasons(self):
        info = self.info()
        self.assertEqual(set(info), {"anthropic", "openai", "gemini", "openrouter", "local"})
        self.assertIn("OPENAI_API_KEY", info["openai"]["reason"])
        self.assertFalse(info["local"]["available"])
        os.environ["GE200_LLM_BASE_URL"] = "http://localhost:11434/v1"
        os.environ["GE200_LLM_MODEL"] = "llama3"
        self.assertTrue(self.info()["local"]["available"])
        self.assertEqual(self.info()["local"]["reason"], "")

    def test_default_provider_follows_env_and_credentials(self):
        os.environ["OPENAI_API_KEY"] = "k"
        os.environ["GE200_LLM_MODEL"] = "m"
        self.assertEqual(llm.default_id(), "openai")
        os.environ["GE200_LLM_PROVIDER"] = "gemini"
        self.assertEqual(llm.default_id(), "gemini")
        with self.assertRaises(llm.LlmError):
            llm.get_provider("nonsense")

    def test_overview_reports_missing_setup(self):
        o = llm.overview()
        if not o["available"]:
            self.assertIn("no AI provider", o["reason"])


if __name__ == "__main__":
    unittest.main()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"GE200_LLM_CONFIG": os.path.join(self.tmp.name, "llm.json")})
        self.env.start()
        for k in llm.CONFIG_KEYS:
            os.environ.pop(k, None)
        llm._cache.clear()
        self.noenv = mock.patch("ge200.env.load_env", lambda *a, **k: None)
        self.noenv.start()

    def tearDown(self):
        self.noenv.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_saved_setup_makes_a_provider_available_and_secrets_never_come_back(self):
        self.assertFalse({i["id"]: i for i in llm.overview()["providers"]}["openai"]["available"])
        llm.save_config({"OPENAI_API_KEY": "sk-secret-123", "GE200_OPENAI_MODEL": "some-model"})
        info = {i["id"]: i for i in llm.overview()["providers"]}["openai"]
        self.assertTrue(info["available"])
        self.assertEqual(info["default_model"], "some-model")
        self.assertNotIn("sk-secret-123", json.dumps(llm.overview()))
        llm.save_config({"OPENAI_API_KEY": ""})                               # empty removes it
        self.assertFalse({i["id"]: i for i in llm.overview()["providers"]}["openai"]["available"])

    def test_real_environment_wins_and_input_is_validated(self):
        llm.save_config({"OPENAI_API_KEY": "from-file"})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "from-env"}):
            self.assertEqual(llm._env("OPENAI_API_KEY"), "from-env")
        self.assertEqual(llm._env("OPENAI_API_KEY"), "from-file")
        for bad in ({"NOT_ALLOWED": "x"}, {"OPENAI_API_KEY": "a\nb"}, {"GE200_LLM_BASE_URL": "file:///etc"},
                    {"GE200_OPENAI_MODEL": "a b"}, {"OPENAI_API_KEY": "x" * 400}):
            with self.assertRaises(llm.LlmError):
                llm.save_config(bad)

    def test_local_server_address_is_saved(self):
        llm.save_config({"GE200_LLM_BASE_URL": "http://localhost:11434/v1", "GE200_LOCAL_MODEL": "llama3"})
        info = {i["id"]: i for i in llm.overview()["providers"]}["local"]
        self.assertTrue(info["available"])
        self.assertEqual(info["default_model"], "llama3")


class AnthropicRestTests(unittest.TestCase):
    def test_works_without_the_sdk_using_plain_rest(self):
        presets = make_presets()
        svc = FakeService([(200, {"content": [{"type": "text", "text": good_answer()}], "stop_reason": "end_turn"})])
        self.addCleanup(svc.close)
        p = llm.AnthropicProvider()
        p.REST_URL = svc.url + "/v1/messages"
        with mock.patch.object(llm.AnthropicProvider, "_has_sdk", lambda self: False), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-secret"}):
            self.assertTrue(p.info()["available"])
            res = tone.generate("lead", presets[1], presets, provider=p)
        req = svc.requests[0]
        self.assertEqual(req["path"], "/v1/messages")
        self.assertEqual(req["headers"]["x-api-key"], "sk-ant-secret")
        self.assertEqual(req["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(req["body"]["model"], "claude-opus-5-5")
        self.assertIn("JSON schema", req["body"]["system"][0]["text"])
        self.assertEqual(res["edit"]["name"], "DREAM CLEAN")

    def test_without_sdk_and_key_it_is_not_available(self):
        with mock.patch.object(llm.AnthropicProvider, "_has_sdk", lambda self: False), \
                mock.patch.dict(os.environ, {"GE200_LLM_CONFIG": os.path.join(os.path.dirname(__file__), "none", "x.json")}):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.assertFalse(llm.AnthropicProvider().info()["available"])
