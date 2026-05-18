import os
import json
import httpx
import google.generativeai as genai
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env

app = FastAPI()

# Configure Gemini API
API_KEY = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
else:
    model = None

# Webview API Base URL
WEBVIEW_URL = "http://localhost:8010"

class RadioCommand(BaseModel):
    text: str              # The raw voice/text command (now includes ID/Name)

@app.post("/radio_RX")
async def radio_rx(command: RadioCommand):
    if not model:
        raise HTTPException(status_code=500, detail="Gemini API key not configured")
    
    # Prompt for parameter extraction including the object identifier
    prompt = f"""
    You are a professional flight controller. Your task is to extract flight parameters and the target object identifier (ID or Name) from the following radio transmission.
    
    Extract:
    - Target Object (the name or ID of the aircraft being addressed. Extract ONLY the core identifier, excluding descriptive words like "object", "ID", "aircraft", "callsign", "物体", "编号" etc.)
    - Target Speed (in knots)
    - Target Heading (in degrees, 0-360)
    - Target Altitude (in feet)
    
    Input text: "{command.text}"
    
    Return ONLY a valid JSON object with the following keys:
    - "target": string or null (The clean name/ID of the object, e.g., "7a2b" instead of "object 7a2b")
    - "speed": number or null
    - "heading": number or null
    - "altitude": number or null
    
    Example 1:
    Input: "Object-7a2b, turn left to 180 and climb to 5000 feet"
    Output: {{"target": "Object-7a2b", "speed": null, "heading": 180, "altitude": 5000}}

    Example 2:
    Input: "Drone-Alpha, speed up to 300"
    Output: {{"target": "Drone-Alpha", "speed": 300, "heading": null, "altitude": null}}
    """
    
    try:
        response = model.generate_content(prompt)
        content = response.text.strip()
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()
        
        params = json.loads(content)
        extracted_target = params.get("target")

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
            return {
                "status": "error",
                "message": f"Could not find any active objects matching '{extracted_target}'",
                "extracted": params
            }

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
