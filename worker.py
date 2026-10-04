import os
import json
import sqlite3
import threading
import time
from pipeline import run_discovery_sync, process_venue_pipeline

state_lock = threading.Lock()
DIR_PATH = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR_PATH, "state.json")

if os.environ.get("RENDER"):
    DB_FILE = "/app/data/state.db"
else:
    DB_FILE = os.path.join(DIR_PATH, "state.db")

def _init_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn

def _migrate_json_if_needed():
    if os.path.exists(STATE_FILE) and not os.path.exists(DB_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            
            conn = _init_db()
            cursor = conn.cursor()
            for k, v in data.items():
                cursor.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (k, json.dumps(v)))
            conn.commit()
            conn.close()
            print("[State] Migrated state.json to state.db successfully.", flush=True)
            os.rename(STATE_FILE, STATE_FILE + ".migrated")
        except Exception as e:
            print(f"[State] Error migrating state.json to state.db: {e}", flush=True)

# Run migration on import
with state_lock:
    _migrate_json_if_needed()

def get_default_state():
    return {
        "status": "idle",
        "current_step": "idle",
        "current_venue": None,
        "config": {
            "coverage": "Shoreditch, London",
            "business_types": "cafes, salons",
            "target_per_day": 3,
            "planter_id": "1",
            "lat": 51.5231,
            "lng": -0.0755
        },
        "queue": [],
        "results": [],
        "rejected": [],
        "processed_ids": [],
        "last_run_time": 0.0,
        "next_run_time": 0.0
    }

def load_state():
    with state_lock:
        try:
            conn = _init_db()
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM state")
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                return get_default_state()
            
            state = get_default_state()
            for k, v in rows:
                state[k] = json.loads(v)
            return state
        except Exception as e:
            print(f"[State] Error loading from state.db: {e}", flush=True)
            return get_default_state()

def save_state(state):
    with state_lock:
        try:
            conn = _init_db()
            cursor = conn.cursor()
            for k, v in state.items():
                cursor.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (k, json.dumps(v)))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[State] Error saving state.db: {e}", flush=True)

def background_worker():
    print("[Worker] Started background worker thread", flush=True)
    while True:
        try:
            time.sleep(4)
            state = load_state()
            if state.get("status") != "running":
                continue
            
            if not state.get("queue"):
                print("[Worker] Queue is empty. Fetching venues...", flush=True)
                state["current_step"] = "discover"
                state["current_venue"] = None
                save_state(state)
                
                venues, rejected = run_discovery_sync(state["config"])
                state = load_state()
                if state.get("status") == "running":
                    processed = state.get("processed_ids", [])
                    filtered_venues = [v for v in venues if v["id"] not in processed]
                    
                    if not filtered_venues:
                        print("[Worker] All discovered venues in this area have already been processed.", flush=True)
                        state["status"] = "idle"
                        state["current_step"] = "idle"
                        state["queue"] = []
                        save_state(state)
                        continue
                    
                    state["queue"] = filtered_venues
                    state["rejected"].extend(rejected)
                    state["next_run_time"] = time.time()
                    state["current_step"] = "idle"
                    save_state(state)
                continue
            
            now = time.time()
            next_run = state.get("next_run_time", 0.0)
            if now >= next_run:
                venue = state["queue"].pop(0)
                print(f"[Worker] Processing next venue: {venue.get('name')}", flush=True)
                
                state["current_venue"] = venue
                state["current_step"] = "capture"
                save_state(state)
                
                result = process_venue_pipeline(venue)
                
                state = load_state()
                if result.get("status") == "success":
                    state["results"].append(result)
                else:
                    state["rejected"].append({
                        "name": venue.get("name"),
                        "venue": venue,
                        "reason": result.get("reason", "Pipeline failure"),
                        "timestamp": time.time(),
                        "status": "rejected"
                    })
                
                if "processed_ids" not in state:
                    state["processed_ids"] = []
                state["processed_ids"].append(venue["id"])
                
                target_per_day = state["config"].get("target_per_day", 3)
                finish_time = time.time()
                session_start = state.get("session_start_time", 0.0)
                
                session_successes = [
                    r for r in state.get("results", [])
                    if r.get("timestamp", 0.0) >= session_start
                ]
                
                if len(session_successes) >= target_per_day:
                    print(f"[Worker] Reached target of {target_per_day} for this session. Pausing.", flush=True)
                    state["status"] = "paused"
                else:
                    state["next_run_time"] = finish_time
                
                state["last_run_time"] = finish_time
                state["current_step"] = "idle"
                state["current_venue"] = None
                save_state(state)
                print(f"[Worker] Finished {venue.get('name')}. Status: {result.get('status')}", flush=True)
                
        except Exception as e:
            print(f"[Worker] Error in loop: {e}", flush=True)
            time.sleep(5)
