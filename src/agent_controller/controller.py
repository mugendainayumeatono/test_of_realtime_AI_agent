import os
import time
import asyncio
import httpx
import google.generativeai as genai
from dotenv import load_dotenv
from pathlib import Path

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

            prompt_file = Path(__file__).parent / "prompt_template.txt"
            try:
                with open(prompt_file, "r", encoding="utf-8") as f:
                    template = f.read()
            except Exception as e:
                print(f"Could not read prompt template: {e}")
                await asyncio.sleep(10)
                continue
                
            prompt = template.format(
                airport_lat=airport_lat,
                airport_lon=airport_lon,
                objects=objects
            )
            response = model.generate_content(prompt)
            text = response.text.strip()
            
            if text and text != "NO_COMMAND":
                await send_radio_message(text)
                
        except Exception as e:
            print(f"Error in controller loop: {e}")
            
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(run_loop())
