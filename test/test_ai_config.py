import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
from agent_common import ai_client


class TestAiConfigPrecedence(unittest.TestCase):
    def setUp(self):
        # Save original environ
        self.orig_env = os.environ.copy()

    def tearDown(self):
        # Restore original environ
        os.environ.clear()
        os.environ.update(self.orig_env)
        # Restore configuration from actual config.json
        ai_client.reload_config()

    def test_load_config_from_json(self):
        """Test reading AI configuration directly from config.json when env vars are unset."""
        test_data = {
            "ai": {
                "client_type": "openai",
                "model": "gpt-4o-custom",
                "api_base": "http://mock-proxy:9999",
                "api_key": "sk-test-key-1234567890",
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(test_data))
            tmp_path = f.name

        try:
            # Clear relevant env vars
            for k in ["AI_CLIENT_TYPE", "AI_MODEL", "MODEL_NAME", "OPENAI_API_KEY", "OPENAI_API_BASE", "GEMINI_API_KEY", "GEMINI_API_BASE"]:
                os.environ.pop(k, None)

            cfg = ai_client.reload_config(config_path=tmp_path)
            self.assertEqual(cfg["client_type"], "openai")
            self.assertEqual(cfg["model"], "gpt-4o-custom")
            self.assertEqual(cfg["api_base"], "http://mock-proxy:9999")
            self.assertEqual(cfg["api_key"], "sk-test-key-1234567890")
            self.assertEqual(ai_client.AI_CLIENT_TYPE, "openai")
            self.assertEqual(ai_client.AI_MODEL, "gpt-4o-custom")
            self.assertEqual(ai_client.API_BASE, "http://mock-proxy:9999")
            self.assertEqual(ai_client.API_KEY, "sk-test-key-1234567890")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_env_vars_ignored_in_favor_of_config_json(self):
        """Test that environment variables are ignored and configuration is read exclusively from config.json."""
        test_data = {
            "ai": {
                "client_type": "openai",
                "model": "gpt-4o-json",
                "api_base": "http://json-proxy:8000",
                "api_key": "sk-json-key-1234567890",
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(test_data))
            tmp_path = f.name

        try:
            # Set environment variables that must be ignored
            os.environ["AI_CLIENT_TYPE"] = "gemini"
            os.environ["AI_MODEL"] = "gemini-3.8-flash-env"
            os.environ["OPENAI_API_BASE"] = "http://env-proxy:8765"
            os.environ["OPENAI_API_KEY"] = "sk-env-key-9999999999"

            cfg = ai_client.reload_config(config_path=tmp_path)
            self.assertEqual(cfg["client_type"], "openai")
            self.assertEqual(cfg["model"], "gpt-4o-json")
            self.assertEqual(cfg["api_base"], "http://json-proxy:8000")
            self.assertEqual(cfg["api_key"], "sk-json-key-1234567890")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_gemini_client_configuration(self):
        """Test configuration resolution when client_type is gemini."""
        test_data = {
            "ai": {
                "client_type": "gemini",
                "model": "gemini-1.5-pro",
                "api_base": "https://custom.gemini.endpoint",
                "api_key": "AIzaSyCustomKey",
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(test_data))
            tmp_path = f.name

        try:
            for k in ["AI_CLIENT_TYPE", "AI_MODEL", "MODEL_NAME", "OPENAI_API_KEY", "OPENAI_API_BASE", "GEMINI_API_KEY", "GEMINI_API_BASE"]:
                os.environ.pop(k, None)

            cfg = ai_client.reload_config(config_path=tmp_path)
            self.assertEqual(cfg["client_type"], "gemini")
            self.assertEqual(cfg["model"], "gemini-1.5-pro")
            self.assertEqual(cfg["api_base"], "https://custom.gemini.endpoint")
            self.assertEqual(cfg["api_key"], "AIzaSyCustomKey")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_json_with_comments_resilience(self):
        """Test that single line comments starting with // or # do not break config loading."""
        content = """{
            // AI Configuration
            # Provider settings
            "ai": {
                "client_type": "openai",
                "model": "gemini-2.5-flash",
                "api_base": "http://127.0.0.1:8765",
                "api_key": "sk-proxy-test-comment"
            }
        }"""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(content)
            tmp_path = f.name

        try:
            for k in ["AI_CLIENT_TYPE", "AI_MODEL", "MODEL_NAME", "OPENAI_API_KEY", "OPENAI_API_BASE", "GEMINI_API_KEY", "GEMINI_API_BASE"]:
                os.environ.pop(k, None)

            cfg = ai_client.reload_config(config_path=tmp_path)
            self.assertEqual(cfg["model"], "gemini-2.5-flash")
            self.assertEqual(cfg["api_key"], "sk-proxy-test-comment")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_get_ai_config_masking(self):
        """Test get_ai_config() masks the api_key for security."""
        test_data = {
            "ai": {
                "client_type": "openai",
                "model": "gemini-2.5-flash",
                "api_base": "http://localhost:8765",
                "api_key": "sk-proxy-1234567890abcdef",
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(test_data))
            tmp_path = f.name

        try:
            for k in ["AI_CLIENT_TYPE", "AI_MODEL", "MODEL_NAME", "OPENAI_API_KEY", "OPENAI_API_BASE"]:
                os.environ.pop(k, None)
            ai_client.reload_config(config_path=tmp_path)
            info = ai_client.get_ai_config()
            self.assertEqual(info["client_type"], "openai")
            self.assertEqual(info["model"], "gemini-2.5-flash")
            self.assertTrue(info["api_key_masked"].startswith("sk-pro..."))
            self.assertTrue(info["api_key_masked"].endswith("cdef"))
            self.assertNotIn("1234567890", info["api_key_masked"])
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_default_fallback_for_openai_when_unconfigured(self):
        """Test default fallback to https://api.openai.com/v1 and gpt-4o-mini when unconfigured."""
        test_data = {
            "ai": {
                "client_type": "openai",
                "api_key": "sk-some-key",
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(test_data))
            tmp_path = f.name

        try:
            for k in ["AI_CLIENT_TYPE", "AI_MODEL", "MODEL_NAME", "OPENAI_API_KEY", "OPENAI_API_BASE", "GEMINI_API_KEY", "GEMINI_API_BASE"]:
                os.environ.pop(k, None)
            cfg = ai_client.reload_config(config_path=tmp_path)
            self.assertEqual(cfg["client_type"], "openai")
            self.assertEqual(cfg["api_base"], "https://api.openai.com/v1")
            self.assertEqual(cfg["model"], "gpt-4o-mini")
            self.assertFalse(cfg["use_history"])
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_default_fallback_for_gemini_when_unconfigured(self):
        """Test default fallback to https://generativelanguage.googleapis.com and gemini-1.5-flash when unconfigured."""
        test_data = {
            "ai": {
                "client_type": "gemini",
                "api_key": "AIzaSyTest",
            }
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(test_data))
            tmp_path = f.name

        try:
            for k in ["AI_CLIENT_TYPE", "AI_MODEL", "MODEL_NAME", "OPENAI_API_KEY", "OPENAI_API_BASE", "GEMINI_API_KEY", "GEMINI_API_BASE"]:
                os.environ.pop(k, None)
            cfg = ai_client.reload_config(config_path=tmp_path)
            self.assertEqual(cfg["client_type"], "gemini")
            self.assertEqual(cfg["api_base"], "https://generativelanguage.googleapis.com")
            self.assertEqual(cfg["model"], "gemini-2.5-flash")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_default_fallback_when_empty_config(self):
        """Test default fallback when config is completely empty."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{}")
            tmp_path = f.name

        try:
            for k in ["AI_CLIENT_TYPE", "AI_MODEL", "MODEL_NAME", "OPENAI_API_KEY", "OPENAI_API_BASE", "GEMINI_API_KEY", "GEMINI_API_BASE"]:
                os.environ.pop(k, None)
            cfg = ai_client.reload_config(config_path=tmp_path)
            self.assertEqual(cfg["client_type"], "openai")
            self.assertEqual(cfg["api_base"], "https://api.openai.com/v1")
            self.assertEqual(cfg["model"], "gpt-4o-mini")
            self.assertFalse(cfg["use_history"])
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_use_history_config_option(self):
        """Test use_history configuration option in config.json."""
        for expected in (True, False):
            test_data = {
                "ai": {
                    "client_type": "openai",
                    "model": "gpt-4o-mini",
                    "api_base": "http://127.0.0.1:8317/v1",
                    "api_key": "sk-test-key",
                    "use_history": expected,
                }
            }
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                f.write(json.dumps(test_data))
                tmp_path = f.name

            try:
                cfg = ai_client.reload_config(config_path=tmp_path)
                self.assertEqual(cfg["use_history"], expected)
                self.assertEqual(ai_client.get_use_history(), expected)
                self.assertEqual(ai_client.get_ai_config()["use_history"], expected)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    def test_generate_ai_response_with_history_session_reuse(self):
        """Test multi-turn AI session interface: system prompt on turn 1, flight state user message on turn 2+."""
        from unittest.mock import MagicMock

        mock_client = MagicMock()
        resp1 = MagicMock()
        resp1.choices = [MagicMock()]
        resp1.choices[0].message.content = '[{"id": "Flight-1", "is_schedule": true, "msg": "Flight-1 descend 3000"}]'

        resp2 = MagicMock()
        resp2.choices = [MagicMock()]
        resp2.choices[0].message.content = '[{"id": "Flight-1", "is_schedule": false, "msg": ""}]'

        mock_client.chat.completions.create.side_effect = [resp1, resp2]

        session = ai_client.AIChatSession()
        template = "ATC RULES AND TEMPLATE\nCurrent Flight Data:\n{flight_data}"
        flight_state_1 = '{"aircraft": [{"id": "Flight-1", "altitude": 5000}]}'
        flight_state_2 = '{"aircraft": [{"id": "Flight-1", "altitude": 3500}]}'

        with patch.object(ai_client, "AI_CLIENT_TYPE", "openai"), \
             patch.object(ai_client, "get_ai_model", return_value=mock_client):
            # Turn 1: system prompt with {flight_data} replaced
            out1 = ai_client.generate_ai_response_with_history(
                prompt=flight_state_1,
                response_schema=list,
                session=session,
                system_prompt=template,
            )
            self.assertIn("Flight-1 descend 3000", out1)
            self.assertEqual(len(session.history), 2)
            self.assertEqual(session.history[0]["role"], "system")
            self.assertIn("ATC RULES AND TEMPLATE", session.history[0]["content"])
            self.assertIn(flight_state_1, session.history[0]["content"])
            self.assertNotIn("{flight_data}", session.history[0]["content"])
            self.assertEqual(session.history[1]["role"], "assistant")

            # Verify Turn 1 API call payload
            call1_messages = mock_client.chat.completions.create.call_args_list[0].kwargs["messages"]
            self.assertEqual(len(call1_messages), 1)
            self.assertEqual(call1_messages[0]["role"], "system")

            # Turn 2: reuses session history, only sends updated flight state without template
            out2 = ai_client.generate_ai_response_with_history(
                prompt=flight_state_2,
                response_schema=list,
                session=session,
            )
            self.assertIn('"is_schedule": false', out2)
            self.assertEqual(len(session.history), 4)
            self.assertEqual(session.history[2]["role"], "user")
            self.assertEqual(session.history[2]["content"], flight_state_2)
            self.assertNotIn("ATC RULES AND TEMPLATE", session.history[2]["content"])
            self.assertEqual(session.history[3]["role"], "assistant")

            # Verify Turn 2 API call payload includes full history + new user message
            call2_messages = mock_client.chat.completions.create.call_args_list[1].kwargs["messages"]
            self.assertEqual(len(call2_messages), 3)
            self.assertEqual([m["role"] for m in call2_messages], ["system", "assistant", "user"])
            self.assertEqual(call2_messages[2]["content"], flight_state_2)


if __name__ == "__main__":
    unittest.main()
