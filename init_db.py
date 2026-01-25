from db import init_schema
from db import conn, utcnow
import json

DEFAULT_ANSWERS = [
    # key, default_value, aliases
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

def main():
    init_schema()
    with conn() as c:
        for key, default, aliases in DEFAULT_ANSWERS:
            c.execute("""
                INSERT OR REPLACE INTO answers(key, default_value, aliases_json, updated_at)
                VALUES (?, ?, ?, ?)
            """, (key, default, json.dumps(aliases), utcnow()))
    print("OK: jobs.db initialized and default answers loaded.")

if __name__ == "__main__":
    main()
