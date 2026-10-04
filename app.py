import os
import time
import threading
import requests
import base64
import math
from io import BytesIO
from PIL import Image
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv(override=True)

from worker import load_state, save_state
from pipeline import geocode_address, sanitize_error
from worker import background_worker

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# ─── FRONTEND ROUTES ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify(load_state())

@app.route("/api/configure", methods=["POST"])
def api_configure():
    req = request.get_json()
    coverage = req.get("coverage")
    business_types = req.get("business_types")
    target_per_day = int(req.get("target_per_day"))
    planter_id = req.get("planter_id", "1")

    try:
        lat, lng = geocode_address(coverage)
    except Exception as e:
        err_msg = sanitize_error(str(e))
        print(f"[Configure] Geocoding failed: {err_msg}", flush=True)
        return jsonify({"error": f"Geocoding failed: {err_msg}"}), 400

    state = load_state()
    old_config = state.get("config", {})
    if old_config.get("coverage") != coverage or old_config.get("business_types") != business_types:
        print("[Configure] Area or types changed. Resetting queue to discover new venues (keeping past results).", flush=True)
        state["queue"] = []
        # Keep past results, rejected, and processed_ids so they don't disappear from the UI
    
    state["config"] = {
        "coverage": coverage,
        "business_types": business_types,
        "target_per_day": target_per_day,
        "planter_id": planter_id,
        "lat": lat,
        "lng": lng
    }
    save_state(state)

    return jsonify(state)

@app.route("/api/start", methods=["POST"])
def api_start():
    state = load_state()
    state["status"] = "running"
    state["next_run_time"] = time.time()
    state["session_start_time"] = time.time()
    save_state(state)
    return jsonify(state)

@app.route("/api/pause", methods=["POST"])
def api_pause():
    state = load_state()
    state["status"] = "paused"
    save_state(state)
    return jsonify(state)

# ─── DOORWAY INTEGRATION ──────────────────────────────────────────────────────
API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

def calculate_heading(lat1, lng1, lat2, lng2):
    """Calculate the bearing from camera (lat1, lng1) to target (lat2, lng2)"""
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    diff_lng = math.radians(lng2 - lng1)

    x = math.sin(diff_lng) * math.cos(lat2_rad)
    y = math.cos(lat1_rad) * math.sin(lat2_rad) - (math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(diff_lng))

    initial_bearing = math.atan2(x, y)
    initial_bearing = math.degrees(initial_bearing)
    return (initial_bearing + 360) % 360

def ask_openai_for_rotation(image_bytes, business_name, history_text=""):
    if not OPENAI_API_KEY:
        print("\n[!] ERROR: OPENAI_API_KEY is not set in .env")
        return "OK"
        
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}"
    }
    payload = {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Look at this Street View image.\n\n{history_text}1. If the storefront/doorway for '{business_name}' is visible ANYWHERE in the image (even on the far edges), you MUST reply with the number of degrees to pan the camera to center it (positive integer to pan right, negative integer to pan left).\n2. If the main entrance is perfectly in the absolute horizontal center of the image, reply with exactly 'OK'.\n3. If the storefront/doorway is COMPLETELY invisible in the entire image, reply with exactly 'SWITCH'. Do not say SWITCH just because it is off-center.\n4. If the history shows you are bouncing back and forth or not improving the centering, reply with exactly 'BEST_POSSIBLE' to stop trying.\nONLY reply with 'SWITCH', 'OK', 'BEST_POSSIBLE', or an integer."
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        }
                    }
                ]
            }
        ],
        "max_tokens": 100,
        "temperature": 0.0
    }
    try:
        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        return result['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"OpenAI API Error: {e}")
        if 'response' in locals() and hasattr(response, 'text'):
            print(f"Details: {response.text}")
        return "OK"

@app.route("/api/doorway", methods=["POST"])
def api_doorway():
    req = request.get_json() or {}
    business_name = req.get("business_name")
    address = req.get("address", "")
    
    if not business_name:
        return jsonify({"error": "Missing business_name parameter"}), 400
        
    search_query = f"{business_name} {address}".strip()
        
    if not API_KEY:
        return jsonify({"error": "Google Maps API key not set in environment."}), 500

    # 1. Find the business location using Google Places API (New)
    print(f"Locating business: {search_query}...")
    places_url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places.location"
    }
    payload = {
        "textQuery": search_query
    }
    
    try:
        response = requests.post(places_url, headers=headers, json=payload)
        data = response.json()
        
        if 'error' in data:
            return jsonify({"error": f"API Error ({data['error'].get('code')}): {data['error'].get('message')}"}), 500
            
        if not data.get('places'):
            return jsonify({"error": f"Could not find a location for '{business_name}'. No results found."}), 404
            
        # Get the coordinates of the first result
        location = data['places'][0]['location']
        lat, lng = location['latitude'], location['longitude']
        
        # 2. Collect all nearby outdoor panoramas by sampling a small grid
        metadata_url = "https://maps.googleapis.com/maps/api/streetview/metadata"
        
        # 1 degree of latitude is ~111km. 0.00015 is roughly 15-20 meters.
        step = 0.00015 
        offsets = [
            (0, 0),
            (step, 0), (-step, 0), (0, step), (0, -step),
            (step, step), (-step, -step), (step, -step), (-step, step)
        ]
        
        panos = {}
        
        print("Searching nearby for outdoor imagery...")
        for d_lat, d_lng in offsets:
            sample_lat = lat + d_lat
            sample_lng = lng + d_lng
            
            meta_params = {
                "location": f"{sample_lat},{sample_lng}",
                "key": API_KEY,
                "source": "outdoor",
                "radius": 20
            }
            meta_response = requests.get(metadata_url, params=meta_params)
            meta_data = meta_response.json()
            
            if meta_data.get('status') == 'OK':
                pano_id = meta_data.get('pano_id')
                if pano_id not in panos:
                    panos[pano_id] = {
                        'pano_id': pano_id,
                        'date': meta_data.get('date', ''),
                        'lat': meta_data['location']['lat'],
                        'lng': meta_data['location']['lng']
                    }

        if not panos:
            return jsonify({"error": "Could not find any outdoor Street View imagery near this location."}), 404

        # Filter out images older than 5 years
        cutoff_date = (datetime.now() - timedelta(days=5*365)).strftime("%Y-%m")
        recent_panos = {}
        for pid, info in panos.items():
            date_str = info.get('date', '')
            if not date_str or date_str >= cutoff_date:
                recent_panos[pid] = info
            else:
                print(f"Ignoring panorama from {date_str} (older than 5 years).")
                
        if not recent_panos:
            return jsonify({"error": "Found panoramas, but all were older than 5 years."}), 404

        # Sort panoramas by date (newest first)
        sorted_panos = sorted(recent_panos.values(), key=lambda x: x['date'], reverse=True)
        
        print(f"Found {len(sorted_panos)} unique panoramas. Sorting newest to oldest...")
            
        # 3. Get the Street View image and adjust based on OpenAI
        street_view_url = "https://maps.googleapis.com/maps/api/streetview"
        max_attempts = 15
        final_image_bytes = None
        
        for pano_info in sorted_panos:
            current_pano_id = pano_info['pano_id']
            date = pano_info['date']
            cam_lat = pano_info['lat']
            cam_lng = pano_info['lng']
            
            heading = calculate_heading(cam_lat, cam_lng, lat, lng)
            
            print(f"\n--- Trying panorama from {date if date else 'Unknown Date'} ---")
            
            pano_success = False
            saw_business = False
            last_valid_image = None
            visited_headings = set()
            
            history = []
            
            for attempt in range(max_attempts):
                int_heading = int(heading)
                
                if int_heading in visited_headings:
                    if saw_business:
                        print("Camera is bouncing back and forth. Accepting current centering as best possible.")
                        final_image_bytes = last_valid_image
                        pano_success = True
                    else:
                        print("Completed a full 360-degree search without finding the business. Skipping to next panorama.")
                    break
                    
                visited_headings.add(int_heading)
                
                print(f"Fetching doorway picture (Attempt {attempt+1}, Heading: {int_heading})...")
                sv_params = {
                    "size": "800x600",
                    "pano": current_pano_id,
                    "fov": 80, 
                    "pitch": 0,
                    "heading": heading,
                    "key": API_KEY
                }
                
                sv_response = requests.get(street_view_url, params=sv_params)
                sv_response.raise_for_status()
                
                image_bytes = sv_response.content
                
                # Check with OpenAI if it's centered well
                history_text = ""
                if history:
                    history_text = "Previous Attempts History:\n" + "\n".join(history) + "\n\n"
                    
                print("Checking image centering with OpenAI...")
                rotation_response = ask_openai_for_rotation(image_bytes, search_query, history_text)
                response_upper = rotation_response.upper()
                
                if response_upper == "OK":
                    print("OpenAI says the image is perfectly centered!")
                    final_image_bytes = image_bytes
                    pano_success = True
                    break
                elif response_upper == "BEST_POSSIBLE":
                    print("OpenAI decided this is the best possible centering.")
                    final_image_bytes = image_bytes
                    pano_success = True
                    break
                elif response_upper == "SWITCH":
                    print("OpenAI doesn't see the business yet. Panning 90 degrees to search this panorama...")
                    heading = (heading + 90) % 360
                    history.append(f"Attempt {attempt+1}: We panned 90 degrees because you replied 'SWITCH'.")
                else:
                    try:
                        degrees = int(rotation_response)
                        print(f"OpenAI says it needs rotating by {degrees} degrees. Adjusting camera...")
                        heading = (heading + degrees) % 360
                        saw_business = True
                        last_valid_image = image_bytes
                        history.append(f"Attempt {attempt+1}: We panned {degrees} degrees right because you replied '{degrees}'.")
                    except ValueError:
                        print(f"Unexpected response from OpenAI: '{rotation_response}'. Panning 90 degrees...")
                        heading = (heading + 90) % 360
                        history.append(f"Attempt {attempt+1}: We panned 90 degrees because you gave an invalid response '{rotation_response}'.")
                        
            if pano_success:
                break
            elif saw_business and last_valid_image:
                print("Exhausted camera adjustments, but business was found. Keeping the closest centered image!")
                final_image_bytes = last_valid_image
                break
                
        if not final_image_bytes:
            return jsonify({"error": "Failed to find the business in any nearby panoramas."}), 404
            
        image_bytes = final_image_bytes
                    
        # 4. Save the final image
        img = Image.open(BytesIO(image_bytes))
        
        output_dir = "images"
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            
        # Clean business name for filename
        safe_name = "".join([c for c in search_query if c.isalnum() or c == ' ']).rstrip().replace(" ", "_")
        if not safe_name:
            safe_name = "business"
            
        file_path = os.path.join(output_dir, f"{safe_name}.jpg")
        img.save(file_path)
        print(f"Image saved to {file_path}")
        
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        return jsonify({"success": True, "file_path": file_path, "image_b64": b64_img})
        
    except Exception as e:
        print(f"An error occurred: {e}")
        return jsonify({"error": f"An internal error occurred: {str(e)}"}), 500

# Start background worker thread once
if not os.environ.get("WORKER_STARTED"):
    threading.Thread(target=background_worker, daemon=True).start()
    os.environ["WORKER_STARTED"] = "true"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
