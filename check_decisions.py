import sqlite3

conn = sqlite3.connect("jobs.db")
cur = conn.cursor()

print("\n=== Decision counts ===")
for row in cur.execute("SELECT decision, COUNT(*) FROM jobs GROUP BY decision"):
    print(row)

conn.close()
