import asyncio
import json
import logging
import sys
from pathlib import Path
from helpers.db import DBClient
from helpers.utils import setup_logging, load_config, ContextLogger

DEFAULT_ANSWERS = [
    ("work_authorization_us", "Yes", ["authorized to work", "legally authorized", "work authorization", "work in the united states", "authorized to work in the united states"]),
    ("visa_sponsorship", "No", ["require sponsorship", "visa sponsorship", "h1b", "sponsorship"]),
    ("phone", "5015551234", ["phone", "mobile number", "phone number"]),
    ("country", "United States", ["country"]),
    ("gender", "Male", ["gender", "sex"]),
    ("race", "White", ["race", "ethnicity"]),
    ("certifications", "CPA", ["certification", "license", "cpa"]),
    ("experience_years", "10", ["years of experience", "how many years"]),
    ("relocation", "Yes", ["relocate", "relocation"]),
    ("drug_screen", "Yes", ["drug screen", "drug test"]),
]

async def main():
    logger = setup_logging()
    ctx_logger = ContextLogger(logger, {"step": "init_db"})
    config = load_config("config.json")
    db = DBClient(str(config.db_path), ctx_logger)
    
    # Initialize schema (already handled in DBClient._execute_sync if needed, 
    # but let's ensure the answers are there)
    print(f"Initializing {config.db_path}...")
    
    # For simplicity in this redo, we use direct execution via DBClient log_event or similar
    # But since init_db is for 'answers' mainly now:
    for key, val, aliases in DEFAULT_ANSWERS:
        # We need an 'upsert_answer' in DBClient or just execute here
        query = "INSERT OR REPLACE INTO answers (key, default_value, aliases_json, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)"
        await asyncio.to_thread(db._execute_sync, query, (key, val, json.dumps(aliases)))
        
    print("OK: Database initialized with default answers.")

if __name__ == "__main__":
    asyncio.run(main())
