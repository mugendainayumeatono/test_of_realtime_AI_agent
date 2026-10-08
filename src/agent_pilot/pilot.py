import os
import json
import asyncio
import httpx
import sys
from fastapi import FastAPI, HTTPException

from pydantic import BaseModel

from pathlib import Path
# Add src/ to sys.path to resolve agent_common
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_common.ai_client import generate_ai_response, get_ai_model, test_ai_api, check_ai_connectivity
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from pathlib import Path
import logging

log_dir = Path("../../logs")
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_dir / "pilot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("pilot")

load_dotenv()  # Load environment variables from .env

@asynccontextmanager
async def lifespan(app: FastAPI):
    test_ai_api()
    yield

app = FastAPI(lifespan=lifespan)

# Platform API Base URL
PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://localhost:8010")

class RadioCommand(BaseModel):
    text: str              # The raw voice/text command (now includes ID/Name)

@app.get("/test_ai")
@app.post("/test_ai")
async def test_ai_endpoint():
    """Test AI model connectivity from the Pilot process."""
    logger.info("Received request to test AI connectivity on Pilot.")
    res = await asyncio.to_thread(check_ai_connectivity)
    res["source"] = "agent_pilot"
    return res



@app.post("/radio_RX")
async def radio_rx(command: RadioCommand):
    logger.info(f"Received radio command: {command.text}")
    if not get_ai_model():
        logger.error("API Key missing on radio_RX")
        raise HTTPException(status_code=500, detail="Gemini API key not configured")
    
    prompt_file = Path(__file__).parent / "prompt_template.txt"
    try:
        with open(prompt_file, "r", encoding="utf-8") as f:
            template = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read prompt template: {e}")
        
    prompt = template.format(input_text=command.text)
    
    try:
        content = generate_ai_response(prompt)
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()
        
        params = json.loads(content)
        extracted_target = params.get("target")
        logger.info(f"Extracted parameters from Gemini: {params}")

        if not extracted_target:
            return {
                "status": "error",
                "message": "AI could not identify which object you are talking to.",
                "extracted": params
            }

        # Resolve the extracted target name/ID to all matching object IDs
        target_uuids = []
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{PLATFORM_URL}/api/objects")
            all_objects = resp.json()
            
            # Match by name or ID (case-insensitive)
            # If multiple objects have the same name, we collect all their IDs
            for obj in all_objects:
                if (extracted_target.lower() == obj["id"].lower() or 
                    extracted_target.lower() == obj["name"].lower()):
                    target_uuids.append(obj["id"])
        
        if not target_uuids:
            logger.warning(f"Could not find target object matching: {extracted_target}")
            return {
                "status": "error",
                "message": f"Could not find any active objects matching '{extracted_target}'",
                "extracted": params
            }

        logger.info(f"Target '{extracted_target}' resolved to UUIDs: {target_uuids}")

        # Call Platform API to update each matched object
        results = []
        async with httpx.AsyncClient() as client:
            for uuid in target_uuids:
                query_params = {"id": uuid}
                if params.get("speed") is not None:
                    query_params["speed"] = params["speed"]
                if params.get("heading") is not None:
                    query_params["heading"] = params["heading"]
                if params.get("altitude") is not None:
                    query_params["altitude"] = params["altitude"]

                resp = await client.get(f"{PLATFORM_URL}/api/update", params=query_params)
                results.append({"id": uuid, "response": resp.json()})
            
            # Post the extracted parameters to the platform radio log
            try:
                log_data = {
                    "source": "agent_pilot",
                    "type": "control_params",
                    "data": params
                }
                await client.post(f"{PLATFORM_URL}/api/radio_log", json=log_data)
            except Exception as e:
                print(f"Failed to post radio log: {e}")

        return {
            "status": "success",
            "resolved_count": len(target_uuids),
            "resolved_ids": target_uuids,
            "extracted": params,
            "platform_responses": results
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8020)
