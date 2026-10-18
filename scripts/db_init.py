"""Apply db/schema.sql one statement at a time (continuous aggregates can't run in a transaction)."""
import os
import re

import psycopg
from dotenv import load_dotenv

load_dotenv()
sql = open("db/schema.sql", encoding="utf-8").read()
statements = []
for chunk in re.split(r";\s*\n", sql):
    body = "\n".join(line for line in chunk.splitlines() if not line.strip().startswith("--")).strip()
    if body:
        statements.append(body.rstrip(";"))

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
    for stmt in statements:
        conn.execute(stmt)
        print("ok:", stmt.splitlines()[0][:70])
print(f"Applied {len(statements)} statements.")
