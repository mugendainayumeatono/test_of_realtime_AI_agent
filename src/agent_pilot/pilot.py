import os
import json
import httpx
import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from pathlib import Path
import logging

log_dir = Path("/app/logs")
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/app/logs/pilot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("pilot")

load_dotenv()  # Load environment variables from .env

app = FastAPI()

# Configure Gemini API
API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
API_BASE = os.environ.get("GEMINI_API_BASE")

if API_KEY:
    client_options = {}
    if API_BASE:
        client_options["api_endpoint"] = API_BASE
        
    genai.configure(api_key=API_KEY, client_options=client_options)
    model = genai.GenerativeModel('gemini-2.5-flash')
    logger.info("Gemini API initialized successfully.")
else:
    model = None
    logger.error("Gemini API key not found. Model will not be initialized.")

# Webview API Base URL
WEBVIEW_URL = os.environ.get("WEBVIEW_URL", "http://localhost:8010")

class RadioCommand(BaseModel):
    text: str              # The raw voice/text command (now includes ID/Name)

@app.post("/radio_RX")
async def radio_rx(command: RadioCommand):
    logger.info(f"Received radio command: {command.text}")
    if not model:
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
        response = model.generate_content(prompt)
        content = response.text.strip()
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
            resp = await client.get(f"{WEBVIEW_URL}/api/objects")
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

        # Call Webview API to update each matched object
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

                resp = await client.get(f"{WEBVIEW_URL}/api/update", params=query_params)
                results.append({"id": uuid, "response": resp.json()})
            
            # Post the extracted parameters to the webview radio log
            try:
                log_data = {
                    "source": "agent_pilot",
                    "type": "control_params",
                    "data": params
                }
                await client.post(f"{WEBVIEW_URL}/api/radio_log", json=log_data)
            except Exception as e:
                print(f"Failed to post radio log: {e}")

        return {
            "status": "success",
            "resolved_count": len(target_uuids),
            "resolved_ids": target_uuids,
            "extracted": params,
            "webview_responses": results
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8020)
