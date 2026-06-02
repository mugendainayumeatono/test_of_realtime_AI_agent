import os
import time
import asyncio
import httpx
import google.generativeai as genai
from dotenv import load_dotenv
from pathlib import Path
import logging

log_dir = Path("/app/logs")
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/app/logs/controller.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("controller")

load_dotenv()

API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
API_BASE = os.environ.get("GEMINI_API_BASE")

if API_KEY:
    client_options = {}
    if API_BASE:
        client_options["api_endpoint"] = API_BASE
        
    genai.configure(api_key=API_KEY, client_options=client_options)
    model = genai.GenerativeModel('gemini-2.5-flash')
else:
    model = None

WEBVIEW_URL = os.environ.get("WEBVIEW_URL", "http://localhost:8010")
PILOT_URL = os.environ.get("PILOT_URL", "http://localhost:8020")

async def get_airport():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{WEBVIEW_URL}/api/airport")
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch airport: {e}")
            return {"lat": None, "lon": None}

async def get_objects():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{WEBVIEW_URL}/api/objects")
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch objects: {e}")
            return []

async def get_controller_state():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{WEBVIEW_URL}/api/controller_state")
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch controller state: {e}")
            return {"enabled": True}


async def send_radio_message(text: str):
    logger.info(f"Controller issuing command: {text}")
    async with httpx.AsyncClient() as client:
        try:
            # Send to webview for UI
            log_data = {
                "source": "agent_controller",
                "type": "voice",
                "text": text
            }
            await client.post(f"{WEBVIEW_URL}/api/radio_log", json=log_data)
            
            # Send to pilot for execution
            await client.post(f"{PILOT_URL}/radio_RX", json={"text": text})
        except Exception as e:
            logger.error(f"Failed to send radio message: {e}")

async def run_loop():
    if not model:
        logger.error("Gemini API key not found. Exiting.")
        return
        
    logger.info("Agent Controller started. Monitoring airspace...")
    while True:
        try:
            c_state = await get_controller_state()
            if not c_state.get("enabled", True):
                await asyncio.sleep(10)
                continue

            airport = await get_airport()
            objects = await get_objects()
            
            if not objects:
                await asyncio.sleep(10)
                continue
                
            airport_lat = airport.get("lat")
            airport_lon = airport.get("lon")
            runway_heading = airport.get("runway_heading")
            
            if airport_lat is None or airport_lon is None:
                logger.warning("No airport location set. Waiting for UI configuration.")
                await asyncio.sleep(10)
                continue

            prompt_file = Path(__file__).parent / "prompt_template.txt"
            try:
                with open(prompt_file, "r", encoding="utf-8") as f:
                    template = f.read()
            except Exception as e:
                logger.error(f"Could not read prompt template: {e}")
                await asyncio.sleep(10)
                continue
                
            prompt = template.format(
                airport_lat=airport_lat,
                airport_lon=airport_lon,
                runway_heading=runway_heading if runway_heading is not None else "Not set",
                objects=objects
            )
            response = model.generate_content(prompt)
            text = response.text.strip()
            
            object_names = [obj.get("name") for obj in objects if obj.get("name")]
            
            if not text or text == "NO_COMMAND":
                pass
            elif text in object_names:
                logger.info(f"AI determined {text} does not need flight state changes.")
                # Send log to UI but not to the pilot
                async with httpx.AsyncClient() as client:
                    try:
                        log_data = {
                            "source": "agent_controller",
                            "type": "info",
                            "text": f"Monitoring {text}, no instruction needed."
                        }
                        await client.post(f"{WEBVIEW_URL}/api/radio_log", json=log_data)
                    except Exception as e:
                        pass
            else:
                await send_radio_message(text)
                
        except Exception as e:
            logger.error(f"Error in controller loop: {e}")
            
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(run_loop())
