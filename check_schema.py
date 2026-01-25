import sqlite3

conn = sqlite3.connect("jobs.db")
cur = conn.cursor()

cur.execute("PRAGMA table_info(jobs)")
rows = cur.fetchall()

print("\n=== jobs table schema ===")
for r in rows:
    print(r)

conn.close()