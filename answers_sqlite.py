import json
from typing import Optional, Tuple, List
from db import conn, utcnow

def load_answers() -> list[dict]:
    """
    Returns list of dicts: {key, default_value, aliases(list[str])}
    """
    with conn() as c:
        rows = c.execute("SELECT key, default_value, aliases_json FROM answers").fetchall()
        out = []
        for r in rows:
            out.append({
                "key": r["key"],
                "default_value": r["default_value"],
                "aliases": json.loads(r["aliases_json"])
            })
        return out

def find_answer(question_text: str) -> Optional[Tuple[str, str]]:
    """
    Returns (key, value) or None
    """
    q = (question_text or "").lower()
    for a in load_answers():
        for alias in a["aliases"]:
            if alias.lower() in q:
                return a["key"], a["default_value"]
    return None

def train_answer(new_key: str, default_value: str, aliases: List[str]):
    aliases = [a.strip().lower() for a in aliases if a.strip()]
    with conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO answers(key, default_value, aliases_json, updated_at)
            VALUES (?, ?, ?, ?)
        """, (new_key, default_value, json.dumps(aliases), utcnow()))
