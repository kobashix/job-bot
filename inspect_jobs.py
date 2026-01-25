import sqlite3

conn = sqlite3.connect("jobs.db")
rows = conn.execute("""
SELECT job_id, title, decision, applied
FROM jobs
WHERE decision = 'apply'
AND applied = 0
""")

for r in rows:
    print(r)
