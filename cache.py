
import sqlite3, json, time
from pathlib import Path

class ProviderCache:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        c=sqlite3.connect(self.db_path)
        c.execute("""CREATE TABLE IF NOT EXISTS provider_cache(
            cache_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )""")
        c.commit(); c.close()

    def get(self, key: str):
        c=sqlite3.connect(self.db_path)
        r=c.execute("SELECT payload_json,expires_at FROM provider_cache WHERE cache_key=?",(key,)).fetchone()
        c.close()
        if not r or r[1] < int(time.time()):
            return None
        return json.loads(r[0])

    def set(self, key: str, provider: str, payload, ttl: int = 86400):
        now=int(time.time())
        c=sqlite3.connect(self.db_path)
        c.execute("""INSERT OR REPLACE INTO provider_cache(cache_key,provider,payload_json,expires_at,created_at)
                     VALUES (?,?,?,?,?)""",
                  (key,provider,json.dumps(payload,ensure_ascii=False),now+ttl,now))
        c.commit(); c.close()
