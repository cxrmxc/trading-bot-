import json
import os
import time
import random

STATE_FILE = 'State.json'

def load_state():
    """Loads state from JSON, or creates a default one if missing."""
    if not os.path.exists(STATE_FILE):
        print(f"{STATE_FILE} not found. Creating a new one...")
        default_state = {
            "last_report": time.strftime("%Y-%m-%d"),
            "agents": {
                "momentum": {"cash": 1000.0, "positions": {}, "closed": []},
                "dip_buyer": {"cash": 1000.0, "positions": {}, "closed": []},
                "volume_spike": {"cash": 1000.0, "positions": {}, "closed": []}
            }
        }
        save_state(default_state)
        return default_state
    
    with open(STATE_FILE, 'r') as f:
        return json.load(f)

def save_state(state):
    """Saves the updated state back to JSON."""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    print("State saved successfully.")

def run_bot():
    state = load_state()
    print(f"Bot started. Current Date: {state['last_report']}")
    
    # --- YOUR TRADING LOGIC GOES HERE ---
    # Example: Give the momentum agent $10 if it has less than $1000
    if state['agents']['momentum']['cash'] < 1000.0:
        state['agents']['momentum']['cash'] += 10.0
        print("Added $10 to momentum agent cash.")
    
    # Update the last report date
    state['last_report'] = time.strftime("%Y-%m-%d")
    # ------------------------------------

    save_state(state)

if __name__ == "__main__":
    run_bot()
