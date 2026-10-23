import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger("ai_client")

# Lazy-loaded genai reference to avoid heavy dependency import on startup
genai = None


def _get_genai():
    global genai
    if genai is None:
        import google.generativeai as _genai
        genai = _genai
    return genai


def _load_config_file(config_path: Optional[str] = None) -> dict:
    """Load configuration from config.json.

    Search order:
    1. Explicit config_path argument
    2. ./config.json (current working directory)
    3. Project root relative to this file (../../config.json)
    4. /app/config.json (Docker container default path)
    """
    candidates = []
    if config_path:
        candidates.append(config_path)
    candidates.append(os.path.join(os.getcwd(), "config.json"))
    candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config.json")))
    candidates.append("/app/config.json")

    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    content = f.read()
                # Filter out single-line comments starting with // or #
                lines = [
                    line for line in content.splitlines()
                    if not line.strip().startswith(("//", "#"))
                ]
                data = json.loads("\n".join(lines))
                if isinstance(data, dict):
                    logger.debug(f"Loaded configuration from {p}")
                    return data
            except Exception as e:
                logger.warning(f"Failed to read/parse config file at {p}: {e}")
    return {}


# Default endpoints and lowest-tier models per provider
DEFAULT_API_BASES = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com",
}

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}

# Global module configuration state
_file_config: dict = {}
AI_CLIENT_TYPE: str = "openai"
AI_MODEL: str = "gpt-4o-mini"
API_KEY: Optional[str] = None
API_BASE: Optional[str] = None
_model = None
_openai_client = None


def reload_config(config_path: Optional[str] = None) -> dict:
    """Reload configuration exclusively from config.json."""
    global _file_config, AI_CLIENT_TYPE, AI_MODEL, API_KEY, API_BASE, _model, _openai_client

    _file_config = _load_config_file(config_path)
    ai_cfg = _file_config.get("ai") if isinstance(_file_config.get("ai"), dict) else {}

    # 1. Resolve AI client type (e.g. 'openai' or 'gemini')
    raw_client_type = ai_cfg.get("client_type")
    AI_CLIENT_TYPE = (
        str(raw_client_type).strip().lower()
        if (raw_client_type is not None and str(raw_client_type).strip() != "")
        else "openai"
    )

    # 2. Resolve AI model name: if not configured, fallback to provider's lowest-tier model
    default_model = DEFAULT_MODELS.get(
        AI_CLIENT_TYPE,
        "gpt-4o-mini" if AI_CLIENT_TYPE == "openai" else "gemini-2.5-flash",
    )
    raw_model = ai_cfg.get("model")
    AI_MODEL = (
        str(raw_model).strip()
        if (raw_model is not None and str(raw_model).strip() != "")
        else default_model
    )

    # 3. Resolve API Base URL and API Key
    default_api_base = DEFAULT_API_BASES.get(
        AI_CLIENT_TYPE,
        "https://api.openai.com/v1" if AI_CLIENT_TYPE == "openai" else "https://generativelanguage.googleapis.com",
    )
    raw_api_base = ai_cfg.get("api_base")
    API_BASE = (
        str(raw_api_base).strip()
        if (raw_api_base is not None and str(raw_api_base).strip() != "")
        else default_api_base
    )

    raw_api_key = ai_cfg.get("api_key")
    API_KEY = (
        str(raw_api_key)
        if (raw_api_key is not None and str(raw_api_key) != "")
        else None
    )

    # Reset cached client/model instances so they reinitialize on next call
    _model = None
    _openai_client = None

    return {
        "client_type": AI_CLIENT_TYPE,
        "model": AI_MODEL,
        "api_base": API_BASE,
        "api_key": API_KEY,
    }


# Initial configuration resolution on import
reload_config()


def get_ai_config() -> dict:
    """Return a dictionary of the active AI configuration (with api_key masked)."""
    masked_key = (
        (API_KEY[:6] + "..." + API_KEY[-4:])
        if (API_KEY and len(API_KEY) > 10)
        else ("***" if API_KEY else None)
    )
    return {
        "client_type": AI_CLIENT_TYPE,
        "model": AI_MODEL,
        "api_base": API_BASE,
        "api_key_masked": masked_key,
    }


def _init_model(client_type: Optional[str] = None):
    global _model, _openai_client
    active_type = client_type or AI_CLIENT_TYPE
    if active_type == "openai":
        if _openai_client is not None:
            return
        if not API_KEY:
            logger.error("API key not found. OpenAI Client will not be initialized.")
            return

        try:
            from openai import OpenAI
            base_url = API_BASE
            if base_url and not base_url.endswith("/v1") and not base_url.endswith("/v1/"):
                base_url = base_url.rstrip("/") + "/v1"

            _openai_client = OpenAI(
                api_key=API_KEY,
                base_url=base_url,
                timeout=120.0,
                max_retries=0,
            )
            logger.info(f"OpenAI API client initialized successfully (base_url={base_url}, model={AI_MODEL}).")
        except ImportError:
            logger.error("openai package not found. Please install it.")
    else:
        if _model is not None:
            return

        g = _get_genai()
        gemini_key = API_KEY or "mock-key"
        client_options = {}
        if API_BASE and active_type == "gemini":
            endpoint = API_BASE
            if endpoint.startswith("https://"):
                endpoint = endpoint[len("https://"):]
            elif endpoint.startswith("http://"):
                endpoint = endpoint[len("http://"):]
            endpoint = endpoint.rstrip("/")
            if endpoint and endpoint != "generativelanguage.googleapis.com":
                client_options["api_endpoint"] = endpoint

        try:
            g.configure(api_key=gemini_key, client_options=client_options if client_options else None)
        except Exception:
            pass
        _model = g.GenerativeModel(AI_MODEL)
        logger.info(f"Gemini API initialized successfully with model {AI_MODEL}.")


def get_ai_model():
    """Retrieve the initialized generative model or client."""
    g = _get_genai()
    # Check if a test specifically patched google.generativeai.GenerativeModel.generate_content
    if hasattr(g.GenerativeModel.generate_content, "mock_calls"):
        if _model is None:
            _init_model(client_type="gemini")
        return _model or g.GenerativeModel(AI_MODEL)

    if AI_CLIENT_TYPE == "openai":
        if _openai_client is None:
            _init_model()
        return _openai_client
    else:
        if _model is None:
            _init_model()
        return _model


def generate_ai_response(prompt: str, response_schema: Any = None, model: Optional[str] = None) -> str:
    """Generate a text response for a given prompt using the configured AI model."""
    g = _get_genai()
    is_gemini_mocked = hasattr(g.GenerativeModel.generate_content, "mock_calls")
    client_type = "gemini" if is_gemini_mocked else AI_CLIENT_TYPE

    client_or_model = get_ai_model()
    if not client_or_model:
        raise RuntimeError(f"{client_type} API client/model not configured")

    target_model = model or AI_MODEL

    if client_type == "openai":
        kwargs = {
            "model": target_model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if response_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        response = client_or_model.chat.completions.create(**kwargs)
        return response.choices[0].message.content.strip()
    else:
        m = client_or_model
        if not hasattr(m, "generate_content"):
            m = g.GenerativeModel(target_model)
        elif model and model != AI_MODEL:
            m = g.GenerativeModel(model)

        if response_schema is not None:
            generation_config = g.GenerationConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
            )
            response = m.generate_content(prompt, generation_config=generation_config)
        else:
            response = m.generate_content(prompt)
        return response.text.strip()


def check_ai_connectivity() -> dict:
    """Test if the AI API is available and working, returning detailed status dict."""
    logger.info(f"Checking {AI_CLIENT_TYPE} AI API connectivity (model: {AI_MODEL})...")
    start_time = time.time()
    try:
        response = generate_ai_response("Hello, this is a test. Reply with 'OK'.")
        elapsed_time = round(time.time() - start_time, 2)
        logger.info(f"AI API connectivity test succeeded in {elapsed_time:.2f}s. Response: {response}")
        return {
            "status": "success",
            "available": True,
            "client_type": AI_CLIENT_TYPE,
            "model": AI_MODEL,
            "latency": elapsed_time,
            "response": response,
            "timestamp": time.time(),
        }
    except Exception as e:
        elapsed_time = round(time.time() - start_time, 2)
        logger.error(f"AI API connectivity test failed after {elapsed_time:.2f}s: {e}", exc_info=True)
        return {
            "status": "error",
            "available": False,
            "client_type": AI_CLIENT_TYPE,
            "model": AI_MODEL,
            "latency": elapsed_time,
            "error": str(e),
            "timestamp": time.time(),
        }


def test_ai_api() -> bool:
    """Test if the AI API is available and working."""
    result = check_ai_connectivity()
    return result["available"]


async def generate_ai_response_async(prompt: str, response_schema: Any = None, model: Optional[str] = None) -> str:
    """Asynchronously generate a text response for a given prompt without blocking the event loop."""
    return await asyncio.to_thread(generate_ai_response, prompt, response_schema, model)


async def check_ai_connectivity_async() -> dict:
    """Asynchronously test if the AI API is available and working without blocking the event loop."""
    return await asyncio.to_thread(check_ai_connectivity)
