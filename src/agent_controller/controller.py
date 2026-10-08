import os
import time
import asyncio
import httpx
import sys
from dotenv import load_dotenv
from pathlib import Path
import logging

# Add src/ to sys.path to resolve agent_common
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from contextlib import asynccontextmanager
from fastapi import FastAPI
from agent_common.ai_client import generate_ai_response, get_ai_model, test_ai_api, check_ai_connectivity



log_dir = Path("../../logs")
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_dir / "controller.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("controller")

load_dotenv()

PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://localhost:8010")
PILOT_URL = os.environ.get("PILOT_URL", "http://localhost:8020")

async def get_airport():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{PLATFORM_URL}/api/airport")
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch airport: {e}")
            return {"lat": None, "lon": None}

async def get_objects():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{PLATFORM_URL}/api/objects")
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch objects: {e}")
            return []

async def get_controller_state():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{PLATFORM_URL}/api/controller_state")
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch controller state: {e}")
            return {"enabled": False}


async def send_radio_message(text: str):
    logger.info(f"Controller issuing command: {text}")
    async with httpx.AsyncClient() as client:
        try:
            # Send to platform for UI
            log_data = {
                "source": "agent_controller",
                "type": "voice",
                "text": text
            }
            await client.post(f"{PLATFORM_URL}/api/radio_log", json=log_data)
            
            # Send to pilot for execution
            await client.post(f"{PILOT_URL}/radio_RX", json={"text": text})
        except Exception as e:
            logger.error(f"Failed to send radio message: {e}")

async def run_loop():
    if not get_ai_model():
        logger.error("Gemini API key not found. Exiting.")
        return
        
    test_ai_api()
        
    logger.info("Agent Controller started. Monitoring airspace...")
    while True:
        try:
            c_state = await get_controller_state()
            if not c_state.get("enabled", False):
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
            text = generate_ai_response(prompt)
            
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
                        await client.post(f"{PLATFORM_URL}/api/radio_log", json=log_data)
                    except Exception as e:
                        pass
            else:
                await send_radio_message(text)
                
        except Exception as e:
            logger.error(f"Error in controller loop: {e}")
            
        await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run the controller airspace monitoring loop in the background
    loop_task = asyncio.create_task(run_loop())
    yield
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="Agent Controller", lifespan=lifespan)

@app.get("/test_ai")
@app.post("/test_ai")
async def test_ai_endpoint():
    """Test AI model connectivity from the Controller process."""
    logger.info("Received request to test AI connectivity on Controller.")
    res = await asyncio.to_thread(check_ai_connectivity)
    res["source"] = "agent_controller"
    return res


if __name__ == "__main__":
    if os.environ.get("CONTROLLER_HTTP_DISABLED") == "1":
        asyncio.run(run_loop())
    else:
        import uvicorn
        host = os.environ.get("CONTROLLER_HOST", "0.0.0.0")
        port = int(os.environ.get("CONTROLLER_PORT", 8030))
        uvicorn.run(app, host=host, port=port)

