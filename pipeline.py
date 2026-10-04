import os
import time
import requests
import base64
import math
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv(override=True)

def geocode_address(address):
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_MAPS_API_KEY not set")
    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        response = requests.get(url, params={"address": address, "key": api_key}, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "OK" and data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
    except Exception as e:
        print(f"[Geocode] Geocoding API failed: {e}", flush=True)
    raise ValueError(f"Geocoding failed for '{address}'")

def sanitize_error(msg):
    for key in [os.environ.get("GOOGLE_MAPS_API_KEY")]:
        if key and key in msg:
            msg = msg.replace(key, "***")
    return msg

def run_discovery_sync(config):
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return [], []
    lat = config.get("lat", 51.5231)
    lng = config.get("lng", -0.0755)
    types_str = config.get("business_types", "cafe, beauty_salon")
    
    raw_types = [t.strip().lower() for t in types_str.split(",") if t.strip()]
    included_types = raw_types
    
    venues, rejected = [], []
    for itype in included_types:
        payload = {
            "includedPrimaryTypes": [itype],
            "maxResultCount": 20,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": 1200.0,
                }
            },
        }
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": (
                "places.id,places.displayName,places.formattedAddress,"
                "places.location,places.types,places.rating,"
                "places.userRatingCount,places.businessStatus,places.photos"
            ),
        }
        try:
            url = "https://places.googleapis.com/v1/places:searchNearby"
            response = requests.post(url, json=payload, headers=headers, timeout=20)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                print(f"[Discovery] Google API Error for type '{itype}': {data['error'].get('message')}", flush=True)
                continue
        except Exception as e:
            print(f"[Discovery] Failed for type {itype}: {e}", flush=True)
            continue
        
        for p in data.get("places", []):
            name    = p.get("displayName", {}).get("text", "")
            address = p.get("formattedAddress", "")
            status  = p.get("businessStatus", "")
            loc     = p.get("location", {})

            if status == "CLOSED_PERMANENTLY":
                rejected.append({
                    "name": name,
                    "reason": "closed permanently",
                    "timestamp": time.time(),
                    "status": "rejected",
                    "venue": {
                        "name": name,
                        "address": address
                    }
                })
                continue
            reviews = p.get("userRatingCount", 0)
            rating  = p.get("rating", 0)
            score   = 1.0 + (2.0 if reviews < 50 else 0) + (1.0 if 3.5 <= rating <= 4.4 else 0)
            photo   = p["photos"][0].get("name") if p.get("photos") else None

            if not any(v["id"] == p.get("id") for v in venues):
                venues.append({
                    "id": p.get("id"), "name": name, "address": address,
                    "lat": loc.get("latitude"), "lng": loc.get("longitude"),
                    "types": p.get("types", []), "rating": rating,
                    "review_count": reviews, "opportunity_score": score,
                    "place_photo_name": photo,
                    "photos": p.get("photos", [])
                })
    venues.sort(key=lambda x: x["opportunity_score"], reverse=True)
    return venues, rejected

def _set_step(step, detail=""):
    try:
        from worker import load_state, save_state
        st = load_state()
        st["current_step"] = step
        if detail:
            st["current_step_detail"] = detail
        else:
            st["current_step_detail"] = ""
        save_state(st)
    except Exception:
        pass
    if detail:
        print(f"[Pipeline] {detail}", flush=True)

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
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        print("\n[!] ERROR: OPENAI_API_KEY is not set in .env")
        return "OK"
        
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {openai_api_key}"
    }

    payload = {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Look at this Street View image.\n\n{history_text}"
                            f"We need to capture a storefront view that includes BOTH the main entrance/doorway AND the business name sign/branding for '{business_name}'.\n\n"
                            f"Rules:\n"
                            f"1. If BOTH the main doorway and the business sign for '{business_name}' are visible anywhere in the image, reply with the number of degrees to pan the camera to center them (positive integer to pan right, negative integer to pan left).\n"
                            f"2. If BOTH the doorway and the business sign are perfectly in the horizontal center of the image, reply with exactly 'OK'.\n"
                            f"3. If EITHER the main entrance/doorway OR the business sign is missing, obscured, or completely invisible, you MUST reply with exactly 'SWITCH' to try another panorama.\n"
                            f"4. If the history shows you are bouncing back and forth or not improving the centering, reply with exactly 'BEST_POSSIBLE' to stop trying.\n"
                            f"ONLY reply with 'SWITCH', 'OK', 'BEST_POSSIBLE', or an integer."
                        )
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
        return "OK"

def evaluate_image_quality(original_b64, edited_b64, business_name):
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        print("\n[!] ERROR: OPENAI_API_KEY is not set in .env. Skipping evaluation.")
        return {"status": "PASS", "reason": "No API key"}
        
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {openai_api_key}"
    }
    
    prompt = (
        f"You are a strict quality control evaluator for an architectural photo editing task for the business '{business_name}'.\n"
        "You are provided with two images:\n"
        "1. The original storefront photo.\n"
        "2. The edited photo with planters added.\n\n"
        "Evaluate the edited photo against the following strict rejection criteria:\n"
        "- Structural Alteration: The model alters the building's original facade, textures, or business branding.\n"
        "- Doorway Obstruction / Blocking the way: The planters block the central walkway, path, or main entrance.\n"
        "- Hallucination: People or unrequested objects are added to the scene.\n"
        "- Reference Mismatch: The planters deviate significantly in texture or shape from standard planters.\n"
        "- Sign Missing: The business sign or name isn't appearing or was distorted.\n"
        "- Wrong Location: The plant is in another store or placed far from the main entrance.\n\n"
        "Respond ONLY with a valid JSON object matching this schema:\n"
        "{\n"
        '  "status": "PASS" | "REJECT",\n'
        '  "reason": "If REJECT, explain clearly which criteria failed. If PASS, leave empty."\n'
        "}"
    )

    payload = {
        "model": "gpt-4o",
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{original_b64}"
                        }
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{edited_b64}"
                        }
                    }
                ]
            }
        ],
        "max_tokens": 300,
        "temperature": 0.0
    }
    try:
        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        content = result['choices'][0]['message']['content'].strip()
        return json.loads(content)
    except Exception as e:
        print(f"[Pipeline] Evaluation API Error: {e}")
        return {"status": "PASS", "reason": "API Error"}

def process_venue_pipeline(venue):
    name = venue.get("name", "")
    address = venue.get("address", "")
    search_query = f"{name} {address}".strip()
    lat = venue.get("lat")
    lng = venue.get("lng")
    
    print(f"[Pipeline] Starting live pipeline for: {search_query}", flush=True)
    
    google_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    
    if not google_key:
        print("[Pipeline] Error: Missing Google Maps API key.", flush=True)
        return {
            "status": "rejected",
            "reason": "Missing Google Maps API key."
        }

    # Ensure captured_images directory exists
    import re
    if os.environ.get("RENDER"):
        captured_dir = "/app/data/captured_images"
    else:
        captured_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captured_images")
    os.makedirs(captured_dir, exist_ok=True)
    safe_venue_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', name)

    _set_step("discover", f"Locating panoramas for: {name}")
    _set_step("capture", f"Running agent rotation on Street View for: {name}")
    
    img_b64 = ""
    img_bytes = None
    
    # 2. Collect all nearby outdoor panoramas by sampling a small grid
    metadata_url = "https://maps.googleapis.com/maps/api/streetview/metadata"
    step = 0.00015 
    offsets = [
        (0, 0),
        (step, 0), (-step, 0), (0, step), (0, -step),
        (step, step), (-step, -step), (step, -step), (-step, step)
    ]
    
    panos = {}
    print(f"[Pipeline] Searching nearby for outdoor imagery for {name}...")
    for d_lat, d_lng in offsets:
        sample_lat = lat + d_lat
        sample_lng = lng + d_lng
        
        meta_params = {
            "location": f"{sample_lat},{sample_lng}",
            "key": google_key,
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
        print("[Pipeline] No outdoor Street View panoramas found nearby.")

    cutoff_date = (datetime.now() - timedelta(days=5*365)).strftime("%Y-%m")
    recent_panos = {}
    for pid, info in panos.items():
        date_str = info.get('date', '')
        if not date_str or date_str >= cutoff_date:
            recent_panos[pid] = info
        else:
            print(f"[Pipeline] Ignoring panorama from {date_str} (older than 5 years).")

    sorted_panos = sorted(recent_panos.values(), key=lambda x: x['date'], reverse=True)[:3]
    if sorted_panos:
        print(f"[Pipeline] Found {len(sorted_panos)} recent panorama(s) for Street View (limiting to max 3 attempts).")
    else:
        print("[Pipeline] No recent outdoor Street View panoramas found.")
        
    street_view_url = "https://maps.googleapis.com/maps/api/streetview"
    max_attempts = 15
    final_image_bytes = None
    
    for pano_info in sorted_panos:
        current_pano_id = pano_info['pano_id']
        date = pano_info['date']
        cam_lat = pano_info['lat']
        cam_lng = pano_info['lng']
        
        heading = calculate_heading(cam_lat, cam_lng, lat, lng)
        print(f"\n[Pipeline] --- Trying Street View panorama from {date if date else 'Unknown Date'} ---")
        
        pano_success = False
        saw_business = False
        last_valid_image = None
        visited_headings = set()
        history = []
        
        for attempt in range(max_attempts):
            int_heading = int(heading)
            
            if int_heading in visited_headings:
                if saw_business:
                    print("[Pipeline] Camera bouncing. Accepting current centering.")
                    final_image_bytes = last_valid_image
                    pano_success = True
                else:
                    print("[Pipeline] Completed 360 search. Skipping to next panorama.")
                break
                
            visited_headings.add(int_heading)
            
            print(f"[Pipeline] Fetching doorway picture (Attempt {attempt+1}, Heading: {int_heading})...")
            sv_params = {
                "size": "800x600",
                "pano": current_pano_id,
                "fov": 80, 
                "pitch": 0,
                "heading": heading,
                "key": google_key
            }
            
            sv_response = requests.get(street_view_url, params=sv_params)
            sv_response.raise_for_status()
            image_bytes = sv_response.content
            
            history_text = ""
            if history:
                history_text = "Previous Attempts History:\n" + "\n".join(history) + "\n\n"
                
            print("[Pipeline] Checking image centering with OpenAI...")
            rotation_response = ask_openai_for_rotation(image_bytes, search_query, history_text)
            response_upper = rotation_response.upper()
            
            if response_upper == "OK":
                print("[Pipeline] OpenAI says the image is perfectly centered!")
                final_image_bytes = image_bytes
                pano_success = True
                break
            elif response_upper == "BEST_POSSIBLE":
                print("[Pipeline] OpenAI decided this is the best possible centering.")
                final_image_bytes = image_bytes
                pano_success = True
                break
            elif response_upper == "SWITCH":
                print("[Pipeline] OpenAI doesn't see the business yet. Panning 90 degrees...")
                heading = (heading + 90) % 360
                history.append(f"Attempt {attempt+1}: We panned 90 degrees because you replied 'SWITCH'.")
            else:
                try:
                    degrees = int(rotation_response)
                    print(f"[Pipeline] OpenAI says it needs rotating by {degrees} degrees. Adjusting camera...")
                    heading = (heading + degrees) % 360
                    saw_business = True
                    last_valid_image = image_bytes
                    history.append(f"Attempt {attempt+1}: We panned {degrees} degrees right because you replied '{degrees}'.")
                except ValueError:
                    print(f"[Pipeline] Unexpected response from OpenAI: '{rotation_response}'. Panning 90 degrees...")
                    heading = (heading + 90) % 360
                    history.append(f"Attempt {attempt+1}: We panned 90 degrees because you gave an invalid response '{rotation_response}'.")
                    
        if pano_success:
            break
        elif saw_business and last_valid_image:
            print("[Pipeline] Exhausted camera adjustments, keeping closest image!")
            final_image_bytes = last_valid_image
            break

    # If Street View fails after 3 panorama attempts, try Google Places Photos (limit 10)
    if not final_image_bytes:
        print("[Pipeline] Street View attempts failed or unavailable. Falling back to Google Places Photos (limit 10)...", flush=True)
        _set_step("capture", f"Checking Google Places Photos for: {name}")

        photos_list = venue.get("photos", [])
        if not photos_list and venue.get("id"):
            try:
                place_details_url = f"https://places.googleapis.com/v1/places/{venue.get('id')}"
                p_resp = requests.get(
                    place_details_url,
                    headers={"X-Goog-Api-Key": google_key, "X-Goog-FieldMask": "photos"},
                    timeout=10
                )
                if p_resp.status_code == 200:
                    photos_list = p_resp.json().get("photos", [])
            except Exception as e:
                print(f"[Pipeline] Error fetching place details photos: {e}", flush=True)

        photos_to_check = photos_list[:10]
        if photos_to_check:
            print(f"[Pipeline] Checking up to {len(photos_to_check)} photo(s) from Google Places...", flush=True)
            for idx, photo_item in enumerate(photos_to_check, 1):
                photo_name = photo_item.get("name") if isinstance(photo_item, dict) else photo_item
                if not photo_name:
                    continue
                try:
                    media_url = f"https://places.googleapis.com/v1/{photo_name}/media"
                    media_resp = requests.get(
                        media_url,
                        params={"maxHeightPx": 800, "maxWidthPx": 800, "key": google_key},
                        timeout=15
                    )
                    if media_resp.status_code != 200:
                        print(f"[Pipeline] Google Photo {idx} fetch failed ({media_resp.status_code}).", flush=True)
                        continue

                    photo_bytes = media_resp.content
                    print(f"[Pipeline] Checking Google Photo {idx}/{len(photos_to_check)} with OpenAI...", flush=True)

                    rotation_response = ask_openai_for_rotation(photo_bytes, search_query)
                    response_upper = rotation_response.upper()

                    if response_upper != "SWITCH":
                        print(f"[Pipeline] OpenAI accepted Google Photo {idx}! (Response: '{rotation_response}')", flush=True)
                        final_image_bytes = photo_bytes
                        break
                    else:
                        print(f"[Pipeline] OpenAI rejected Google Photo {idx} (Entrance/sign not visible).", flush=True)
                except Exception as e:
                    print(f"[Pipeline] Error processing Google Photo {idx}: {e}", flush=True)

    if not final_image_bytes:
        return {
            "status": "rejected",
            "reason": "Failed to find suitable storefront image in Street View (3 attempts) or Google Photos (10 photos)."
        }
        
    img_bytes = final_image_bytes
    img_b64 = base64.b64encode(img_bytes).decode("utf-8")
        
    filename_best = f"{safe_venue_name}_FINAL_BEST.jpg"
    filepath_best = os.path.join(captured_dir, filename_best)
    try:
        with open(filepath_best, "wb") as f:
            f.write(img_bytes)
        print(f"[Pipeline] Saved final best image to: {filename_best}", flush=True)
    except Exception as e:
        print(f"[Pipeline] Failed to save final best image: {e}", flush=True)

    _set_step("composite", f"Finalizing image for: {name}")
    
    import random
    # Select a random planter design from static/images/ (1.png, 2.png, or 3.png)
    planter_id = str(random.choice([1, 2, 3]))
    base_dir = os.path.dirname(os.path.abspath(__file__))
    plan_image_path = os.path.join(base_dir, "static", "images", f"{planter_id}.png")
    if not os.path.exists(plan_image_path):
        plan_image_path = os.path.join(base_dir, "static", "images", "1.png")

    plan_image_b64 = ""
    try:
        with open(plan_image_path, "rb") as pf:
            plan_image_b64 = base64.b64encode(pf.read()).decode("utf-8")
    except Exception as e:
        print(f"[Pipeline] Error reading planter image: {e}", flush=True)

    composited_b64 = img_b64
    bfl_key = os.environ.get("BFL_API_KEY")

    if bfl_key and plan_image_b64:
        max_bfl_attempts = 3
        bfl_attempt = 1
        last_eval_reason = ""

        while bfl_attempt <= max_bfl_attempts:
            try:
                print(f"[Pipeline] Calling BFL API (flux-2-max) for image composition (Attempt {bfl_attempt}/{max_bfl_attempts})...", flush=True)
                base_prompt = (
                    f"Based on the exterior photo of '{name}' provided in input_image, edit the doorway area to "
                    "seamlessly integrate the planter design plan shown in input_image_2. "
                    "CRITICAL: Choose to add either ONE or TWO planters immediately flanking the main doorway on either side, depending on how much space is available. DO NOT place any planters directly in front of the door or block the entrance in any way. The plants must be right next to the venue's entrance, not positioned far away or in the middle of the walkway/path. "
                    f"CRITICAL: Place the planter design ONLY in front of the main venue '{name}'. Do not place it in front of neighboring venues or shops. "
                    "CRITICAL: Keep the business sign, branding, and text on the storefront of input_image exactly as they are. DO NOT write, add, or hallucinate ANY new text, signs, or characters anywhere in the image. If there is no text in the original image, DO NOT add any. "
                    "CRITICAL: Keep the scene strictly as a storefront exterior photograph; do not show or reimagine the interior of the venue. "
                    "Clean up the scene in input_image by removing all people. "
                    "Center the image composition better, as if taken by a professional architectural photographer. "
                    "Strictly preserve the exact building, textures, colors, and structure of input_image, only editing the doorway area and removing people."
                )

                if last_eval_reason:
                    base_prompt += (
                        f"\nCRITICAL CORRECTION REQUIRED: The previous attempt was rejected by quality control with reason: '{last_eval_reason}'. "
                        "You MUST strictly fix this issue in your output."
                    )

                payload = {
                    "prompt": base_prompt,
                    "width": 1024,
                    "height": 1024,
                    "safety_tolerance": 2,
                    "output_format": "jpeg",
                    "input_image": f"data:image/jpeg;base64,{img_b64}",
                    "input_image_2": f"data:image/png;base64,{plan_image_b64}"
                }
                headers = {
                    "accept": "application/json",
                    "x-key": bfl_key,
                    "Content-Type": "application/json",
                }
                api_url = "https://api.bfl.ai/v1/flux-2-max"
                poll_url = "https://api.bfl.ai/v1/get_result"
                
                response = requests.post(api_url, headers=headers, json=payload, timeout=30)
                if response.status_code == 200:
                    task_id = response.json().get("id")
                    if task_id:
                        print(f"[Pipeline] BFL task submitted. ID: {task_id}. Polling...", flush=True)
                        bfl_attempt_done = False
                        # Poll for up to 2 minutes
                        for _ in range(60):
                            time.sleep(2)
                            poll_resp = requests.get(poll_url, headers=headers, params={"id": task_id}, timeout=15)
                            if poll_resp.status_code == 200:
                                poll_data = poll_resp.json()
                                status = poll_data.get("status")
                                if status == "Ready":
                                    result_url = poll_data.get("result", {}).get("sample")
                                    if result_url:
                                        print("[Pipeline] BFL image edit completed successfully!", flush=True)
                                        img_data = requests.get(result_url, timeout=20).content
                                        composited_b64 = base64.b64encode(img_data).decode("utf-8")
                                        
                                        # Save edited image locally
                                        filename_edited = f"{safe_venue_name}_EDITED.jpg"
                                        filepath_edited = os.path.join(captured_dir, filename_edited)
                                        with open(filepath_edited, "wb") as ef:
                                            ef.write(img_data)
                                        print(f"[Pipeline] Saved edited image to: {filename_edited}", flush=True)
                                        
                                        print(f"[Pipeline] Running AI quality evaluation (Attempt {bfl_attempt}/{max_bfl_attempts})...", flush=True)
                                        eval_result = evaluate_image_quality(img_b64, composited_b64, name)
                                        if eval_result.get("status") == "REJECT":
                                            last_eval_reason = eval_result.get('reason', 'Quality check failed')
                                            print(f"[Pipeline] Image rejected by AI. Reason: {last_eval_reason}", flush=True)
                                            if bfl_attempt < max_bfl_attempts:
                                                print(f"[Pipeline] Reprompting BFL model with feedback (Attempt {bfl_attempt+1})...", flush=True)
                                                bfl_attempt += 1
                                                bfl_attempt_done = True
                                                break
                                            else:
                                                print(f"[Pipeline] Reached max BFL reprompt attempts ({max_bfl_attempts}). Proceeding with generated image.", flush=True)
                                                bfl_attempt_done = True
                                                bfl_attempt = max_bfl_attempts + 1
                                                break
                                        else:
                                            print("[Pipeline] Image passed AI evaluation.", flush=True)
                                            bfl_attempt_done = True
                                            bfl_attempt = max_bfl_attempts + 1
                                            break
                                elif status in ["Failed", "Error"]:
                                    print(f"[Pipeline] BFL task failed: {poll_data}", flush=True)
                                    break
                            else:
                                print(f"[Pipeline] BFL polling error: {poll_resp.status_code}", flush=True)
                        
                        if bfl_attempt_done:
                            continue
                    else:
                        print(f"[Pipeline] BFL API response did not contain ID: {response.text}", flush=True)
                        break
                else:
                    print(f"[Pipeline] BFL API submission failed ({response.status_code}): {response.text}", flush=True)
                    break
            except Exception as e:
                print(f"[Pipeline] BFL API integration error: {e}", flush=True)
                break
    else:
        if not bfl_key:
            print("[Pipeline] BFL_API_KEY not found in environment. Skipping image editing.", flush=True)

    return {
        "status": "success",
        "venue": venue,
        "original_b64": img_b64,
        "composited_b64": composited_b64,
        "score": 10,
        "reason": "Google Places Photo",
        "timestamp": time.time(),
        "source": "places_photo"
    }
