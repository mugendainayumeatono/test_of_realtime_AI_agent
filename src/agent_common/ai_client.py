import asyncio
import os
import logging
import time
from typing import Any, Optional
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("ai_client")

# Configure API
AI_CLIENT_TYPE = os.environ.get("AI_CLIENT_TYPE", "gemini").lower()

if AI_CLIENT_TYPE == "openai":
    API_KEY = os.environ.get("OPENAI_API_KEY")
    API_BASE = os.environ.get("OPENAI_API_BASE")
else:
    API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    API_BASE = os.environ.get("GEMINI_API_BASE")

_model = None
_openai_client = None

def _init_model():
    global _model, _openai_client
    if AI_CLIENT_TYPE == "openai":
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
                timeout=30.0,
                max_retries=1,
            )
            logger.info("OpenAI API client initialized successfully.")
        except ImportError:
            logger.error("openai package not found. Please install it.")
    else:
        if _model is not None:
            return
            
        if API_KEY:
            client_options = {}
            if API_BASE:
                client_options["api_endpoint"] = API_BASE
                
            genai.configure(api_key=API_KEY, client_options=client_options)
            _model = genai.GenerativeModel('gemini-2.5-flash')
            logger.info("Gemini API initialized successfully.")
        else:
            logger.error("Gemini API key not found. Model will not be initialized.")

def get_ai_model():
    """Retrieve the initialized generative model or client."""
    if AI_CLIENT_TYPE == "openai":
        if _openai_client is None:
            _init_model()
        return _openai_client
    else:
        if _model is None:
            _init_model()
        return _model

def generate_ai_response(prompt: str, response_schema: Any = None) -> str:
    """Generate a text response for a given prompt using the configured AI model."""
    client_or_model = get_ai_model()
    if not client_or_model:
        raise RuntimeError(f"{AI_CLIENT_TYPE} API client/model not configured")
        
    if AI_CLIENT_TYPE == "openai":
        kwargs = {
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": prompt}]
        }
        if response_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        response = client_or_model.chat.completions.create(**kwargs)
        return response.choices[0].message.content.strip()
    else:
        if response_schema is not None:
            generation_config = genai.GenerationConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
            )
            response = client_or_model.generate_content(prompt, generation_config=generation_config)
        else:
            response = client_or_model.generate_content(prompt)
        return response.text.strip()

def check_ai_connectivity() -> dict:
    """Test if the AI API is available and working, returning detailed status dict."""
    logger.info(f"Checking {AI_CLIENT_TYPE} AI API connectivity...")
    start_time = time.time()
    try:
        response = generate_ai_response("Hello, this is a test. Reply with 'OK'.")
        elapsed_time = round(time.time() - start_time, 2)
        logger.info(f"AI API connectivity test succeeded in {elapsed_time:.2f}s. Response: {response}")
        return {
            "status": "success",
            "available": True,
            "client_type": AI_CLIENT_TYPE,
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
            "latency": elapsed_time,
            "error": str(e),
            "timestamp": time.time(),
        }

def test_ai_api() -> bool:
    """Test if the AI API is available and working."""
    result = check_ai_connectivity()
    return result["available"]

async def generate_ai_response_async(prompt: str, response_schema: Any = None) -> str:
    """Asynchronously generate a text response for a given prompt without blocking the event loop."""
    return await asyncio.to_thread(generate_ai_response, prompt, response_schema)

async def check_ai_connectivity_async() -> dict:
    """Asynchronously test if the AI API is available and working without blocking the event loop."""
    return await asyncio.to_thread(check_ai_connectivity)

