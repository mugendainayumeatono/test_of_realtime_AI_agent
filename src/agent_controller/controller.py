import os
import time
import asyncio
import httpx
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)
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
            print(f"Failed to fetch airport: {e}")
            return {"lat": None, "lon": None}

async def get_objects():
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{WEBVIEW_URL}/api/objects")
            return resp.json()
        except Exception as e:
            print(f"Failed to fetch objects: {e}")
            return []

async def send_radio_message(text: str):
    print(f"Controller issuing command: {text}")
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
            print(f"Failed to send radio message: {e}")

async def run_loop():
    if not model:
        print("Gemini API key not found. Exiting.")
        return
        
    print("Agent Controller started. Monitoring airspace...")
    while True:
        try:
            airport = await get_airport()
            objects = await get_objects()
            
            if not objects:
                await asyncio.sleep(10)
                continue
                
            airport_lat = airport.get("lat")
            airport_lon = airport.get("lon")
            
            if airport_lat is None or airport_lon is None:
                print("No airport location set. Please set airport in the UI.")
                await asyncio.sleep(10)
                continue

            prompt = f"""
You are an Approach Controller (进近管制员). Your task is to guide the aircraft in the airspace to join the traffic pattern (5边) and land at the airport.

Current Airport Location:
Lat: {airport_lat}, Lon: {airport_lon}

Current Aircraft States:
{objects}

Generate the NEXT radio instruction for one of the aircraft. 
The instruction should be natural language (like real ATC, e.g., "Drone-1, turn left heading 180, descend to 2000 feet, cleared for approach").
Ensure you explicitly mention the aircraft's Name or ID.
If all aircraft are flying perfectly towards the airport and no new instruction is needed, reply with "NO_COMMAND".
Do NOT output anything else except the radio instruction text.
"""
            response = model.generate_content(prompt)
            text = response.text.strip()
            
            if text and text != "NO_COMMAND":
                await send_radio_message(text)
                
        except Exception as e:
            print(f"Error in controller loop: {e}")
            
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(run_loop())
