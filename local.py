
import sqlite3, json
from pathlib import Path
from base import TechnicalDataProvider

class LocalProvider(TechnicalDataProvider):
    name = "local"

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def configured(self) -> bool:
        return True

    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    async def search_vehicle(self, **kwargs):
        c=self._conn()
        q="SELECT * FROM vehicles WHERE 1=1"; p=[]
        if kwargs.get("brand"):
            q+=" AND brand=?"; p.append(kwargs["brand"])
        if kwargs.get("model"):
            q+=" AND model=?"; p.append(kwargs["model"])
        rows=[dict(x) for x in c.execute(q+" ORDER BY brand,model",p)]
        c.close()
        for x in rows:
            x["engines"]=[e for e in (x.get("engines") or "").split(";") if e]
        return rows

    async def get_technical_data(self, vehicle_ref: str, section: str | None = None):
        c=self._conn()
        q="SELECT * FROM technical_specs WHERE vehicle_id=?"; p=[int(vehicle_ref)]
        if section:
            q+=" AND spec_key LIKE ?"; p.append(f"%{section}%")
        rows=[dict(x) for x in c.execute(q,p)]
        c.close()
        return rows

    async def get_repair_procedure(self, vehicle_ref: str, procedure_ref: str):
        c=self._conn()
        r=c.execute("SELECT * FROM repairs WHERE slug=?",(procedure_ref,)).fetchone()
        c.close()
        if not r:
            return None
        d=dict(r)
        for k in ("tools_json","parts_json","steps_json","warnings_json"):
            d[k.replace("_json","")] = json.loads(d.pop(k) or "[]")
        return d
