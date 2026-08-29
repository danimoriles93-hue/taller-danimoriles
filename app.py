
from fastapi import FastAPI, HTTPException, Header, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path
import sqlite3, json, os, csv, io, hashlib

from autodata import AutodataProvider
from local import LocalProvider
from cache import ProviderCache

BASE = Path(__file__).resolve().parent
DB = Path(os.getenv("MOTORFIX_DB_PATH", str(BASE / "motorfix.db")))
ADMIN_KEY = os.getenv("MOTORFIX_ADMIN_KEY", "change-me-now")

app = FastAPI(title="MotorFix Pro API", version="2.0.0")
app.mount("/static", StaticFiles(directory=BASE), name="static")

local_provider = LocalProvider(DB)
autodata_provider = AutodataProvider()
provider_cache = ProviderCache(DB)

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def require_admin(x_admin_key: str | None):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(401, "Admin key incorrecta")

def repair_row(r):
    d = dict(r)
    for k in ("tools_json","parts_json","steps_json","warnings_json"):
        d[k.replace("_json","")] = json.loads(d.pop(k) or "[]")
    d["is_verified"] = bool(d.get("is_verified"))
    return d

def obd_row(r):
    d = dict(r)
    d["causes"] = json.loads(d.pop("causes_json") or "[]")
    d["checks"] = json.loads(d.pop("checks_json") or "[]")
    return d

def active_provider():
    requested = os.getenv("MOTORFIX_PROVIDER", "local").lower().strip()
    if requested == "autodata" and autodata_provider.configured():
        return autodata_provider
    return local_provider

def cache_key(prefix: str, **kwargs):
    raw = prefix + "|" + json.dumps(kwargs, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

@app.on_event("startup")
def ensure_schema():
    c = conn()
    c.execute("""CREATE TABLE IF NOT EXISTS repair_vehicle_map(
        repair_id INTEGER NOT NULL,
        vehicle_id INTEGER NOT NULL,
        engine TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(repair_id, vehicle_id, engine)
    )""")
    c.commit(); c.close()

@app.get("/")
def home():
    return FileResponse(BASE/"index.html")

@app.get("/admin")
def admin():
    return FileResponse(BASE/"admin.html")

@app.get("/legal")
def legal():
    return FileResponse(BASE/"legal.html")

@app.get("/api/health")
def health():
    return {"ok": True, "version":"2.0.0"}

@app.get("/api/stats")
def stats():
    c=conn()
    out={
        "vehicles": c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0],
        "repairs": c.execute("SELECT COUNT(*) FROM repairs").fetchone()[0],
        "obd_codes": c.execute("SELECT COUNT(*) FROM obd_codes").fetchone()[0],
        "technical_specs": c.execute("SELECT COUNT(*) FROM technical_specs").fetchone()[0],
    }
    c.close()
    return out

@app.get("/api/vehicles")
def vehicles(brand: str="", model: str=""):
    c=conn()
    q="SELECT * FROM vehicles WHERE 1=1"; p=[]
    if brand: q+=" AND brand=?"; p.append(brand)
    if model: q+=" AND model=?"; p.append(model)
    rows=[dict(x) for x in c.execute(q+" ORDER BY brand,model,year_from",p)]
    c.close()
    for x in rows:
        x["engines"]=[e for e in (x["engines"] or "").split(";") if e]
    return rows

@app.get("/api/repairs")
def repairs(q: str="", category: str="", difficulty: int|None=None,
            brand: str="", model: str="", year: int|None=None, engine: str=""):
    c=conn()
    sql="""SELECT DISTINCT r.* FROM repairs r
           LEFT JOIN repair_vehicle_map m ON m.repair_id=r.id
           LEFT JOIN vehicles v ON v.id=m.vehicle_id
           WHERE 1=1"""
    p=[]
    if q:
        s=f"%{q}%"
        sql+=" AND (r.title LIKE ? OR r.category LIKE ? OR r.applies LIKE ?)"
        p += [s,s,s]
    if category:
        sql+=" AND r.category=?"; p.append(category)
    if difficulty is not None:
        sql+=" AND r.difficulty=?"; p.append(difficulty)

    # Unmapped repairs are treated as generic/multimake.
    if brand:
        sql+=" AND (m.repair_id IS NULL OR v.brand=?)"; p.append(brand)
    if model:
        sql+=" AND (m.repair_id IS NULL OR v.model=?)"; p.append(model)
    if year is not None:
        sql+=" AND (m.repair_id IS NULL OR (? BETWEEN v.year_from AND v.year_to))"; p.append(year)
    if engine:
        sql+=" AND (m.repair_id IS NULL OR m.engine='' OR m.engine=?)"; p.append(engine)

    rows=[repair_row(x) for x in c.execute(sql+" ORDER BY r.category,r.title",p)]
    c.close()
    return rows

@app.get("/api/repairs/{slug}")
def repair(slug: str):
    c=conn()
    r=c.execute("SELECT * FROM repairs WHERE slug=?",(slug,)).fetchone()
    if not r:
        c.close(); raise HTTPException(404,"Reparación no encontrada")
    out=repair_row(r)
    maps=[dict(x) for x in c.execute("""SELECT m.vehicle_id,m.engine,m.notes,v.brand,v.model,v.year_from,v.year_to
                                       FROM repair_vehicle_map m JOIN vehicles v ON v.id=m.vehicle_id
                                       WHERE m.repair_id=? ORDER BY v.brand,v.model""",(r["id"],))]
    c.close()
    out["compatibility"]=maps
    return out

@app.get("/api/obd")
def obd_list(q: str="", manufacturer: str=""):
    c=conn()
    sql="SELECT * FROM obd_codes WHERE 1=1"; p=[]
    if q:
        s=f"%{q.upper()}%"
        sql+=" AND (UPPER(code) LIKE ? OR UPPER(title) LIKE ?)"; p += [s,s]
    if manufacturer:
        sql+=" AND manufacturer=?"; p.append(manufacturer)
    rows=[obd_row(x) for x in c.execute(sql+" ORDER BY code",p)]
    c.close(); return rows

@app.get("/api/obd/{code}")
def obd(code: str, manufacturer: str="Generic"):
    code=code.upper().strip()
    c=conn()
    r=c.execute("""SELECT * FROM obd_codes
                   WHERE code=? AND (manufacturer=? OR manufacturer='Generic')
                   ORDER BY CASE WHEN manufacturer=? THEN 0 ELSE 1 END LIMIT 1""",
                (code,manufacturer,manufacturer)).fetchone()
    c.close()
    if not r: raise HTTPException(404,"Código no encontrado")
    return obd_row(r)

@app.get("/api/specs")
def specs(vehicle_id: int, engine: str=""):
    c=conn()
    if engine:
        rows=[dict(x) for x in c.execute(
            "SELECT * FROM technical_specs WHERE vehicle_id=? AND (engine=? OR engine='') ORDER BY spec_key",
            (vehicle_id,engine))]
    else:
        rows=[dict(x) for x in c.execute(
            "SELECT * FROM technical_specs WHERE vehicle_id=? ORDER BY engine,spec_key",(vehicle_id,))]
    c.close()
    return rows

@app.get("/api/provider/status")
def provider_status():
    selected=os.getenv("MOTORFIX_PROVIDER","local").lower().strip()
    provider=active_provider()
    return {
        "requested":selected,
        "active":provider.name,
        "autodata_configured":autodata_provider.configured(),
        "fallback":provider.name != selected
    }

@app.get("/api/provider/vehicles")
async def provider_vehicles(brand: str="", model: str="", year: str="", engine: str=""):
    provider=active_provider()
    args={"brand":brand,"model":model,"year":year,"engine":engine}
    key=cache_key("vehicles",provider=provider.name,**args)
    cached=provider_cache.get(key)
    if cached is not None:
        return {"provider":provider.name,"cached":True,"data":cached}
    try:
        data=await provider.search_vehicle(**args)
    except Exception as e:
        if provider.name!="local":
            data=await local_provider.search_vehicle(**args)
            return {"provider":"local","fallback_from":provider.name,"cached":False,"data":data,"warning":str(e)}
        raise HTTPException(502,str(e))
    provider_cache.set(key,provider.name,data,ttl=86400)
    return {"provider":provider.name,"cached":False,"data":data}

@app.get("/api/provider/technical/{vehicle_ref}")
async def provider_technical(vehicle_ref: str, section: str=""):
    provider=active_provider()
    key=cache_key("technical",provider=provider.name,vehicle_ref=vehicle_ref,section=section)
    cached=provider_cache.get(key)
    if cached is not None:
        return {"provider":provider.name,"cached":True,"data":cached}
    try:
        data=await provider.get_technical_data(vehicle_ref,section or None)
    except Exception as e:
        if provider.name!="local":
            data=await local_provider.get_technical_data(vehicle_ref,section or None)
            return {"provider":"local","fallback_from":provider.name,"cached":False,"data":data,"warning":str(e)}
        raise HTTPException(502,str(e))
    provider_cache.set(key,provider.name,data,ttl=43200)
    return {"provider":provider.name,"cached":False,"data":data}

@app.get("/api/provider/procedure/{vehicle_ref}/{procedure_ref}")
async def provider_procedure(vehicle_ref: str, procedure_ref: str):
    provider=active_provider()
    key=cache_key("procedure",provider=provider.name,vehicle_ref=vehicle_ref,procedure_ref=procedure_ref)
    cached=provider_cache.get(key)
    if cached is not None:
        return {"provider":provider.name,"cached":True,"data":cached}
    try:
        data=await provider.get_repair_procedure(vehicle_ref,procedure_ref)
    except Exception as e:
        if provider.name!="local":
            data=await local_provider.get_repair_procedure(vehicle_ref,procedure_ref)
            return {"provider":"local","fallback_from":provider.name,"cached":False,"data":data,"warning":str(e)}
        raise HTTPException(502,str(e))
    provider_cache.set(key,provider.name,data,ttl=43200)
    return {"provider":provider.name,"cached":False,"data":data}

# ---------------- Admin models ----------------

class RepairIn(BaseModel):
    slug: str
    title: str
    category: str = "Otros"
    difficulty: int = Field(default=2, ge=1, le=5)
    time_estimate: str = ""
    cost_estimate: str = ""
    applies: str = ""
    tools: list[str] = Field(default_factory=list)
    parts: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    video_url: str = ""
    source_name: str = "MotorFix"
    source_url: str = ""
    is_verified: bool = False

class VehicleIn(BaseModel):
    brand: str
    model: str
    year_from: int
    year_to: int
    engines: list[str] = Field(default_factory=list)

class ObdIn(BaseModel):
    code: str
    title: str
    severity: str = "Media"
    system: str = ""
    causes: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    driving_advice: str = ""
    manufacturer: str = "Generic"

class CompatibilityIn(BaseModel):
    repair_slug: str
    vehicle_id: int
    engine: str = ""
    notes: str = ""

@app.get("/api/admin/dashboard")
def admin_dashboard(x_admin_key: str|None=Header(default=None)):
    require_admin(x_admin_key)
    return stats()

@app.post("/api/admin/vehicles")
def add_vehicle(body: VehicleIn, x_admin_key: str|None=Header(default=None)):
    require_admin(x_admin_key)
    if body.year_to < body.year_from:
        raise HTTPException(400,"year_to no puede ser menor que year_from")
    c=conn()
    cur=c.execute("""INSERT INTO vehicles(brand,model,year_from,year_to,engines)
                     VALUES(?,?,?,?,?)""",
                  (body.brand.strip(),body.model.strip(),body.year_from,body.year_to,
                   ";".join(x.strip() for x in body.engines if x.strip())))
    c.commit(); vid=cur.lastrowid; c.close()
    return {"ok":True,"id":vid}

@app.put("/api/admin/vehicles/{vehicle_id}")
def update_vehicle(vehicle_id:int, body:VehicleIn, x_admin_key:str|None=Header(default=None)):
    require_admin(x_admin_key)
    c=conn()
    cur=c.execute("""UPDATE vehicles SET brand=?,model=?,year_from=?,year_to=?,engines=? WHERE id=?""",
                  (body.brand.strip(),body.model.strip(),body.year_from,body.year_to,
                   ";".join(x.strip() for x in body.engines if x.strip()),vehicle_id))
    c.commit(); c.close()
    if cur.rowcount==0: raise HTTPException(404,"Vehículo no encontrado")
    return {"ok":True}

@app.delete("/api/admin/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id:int, x_admin_key:str|None=Header(default=None)):
    require_admin(x_admin_key)
    c=conn()
    c.execute("DELETE FROM repair_vehicle_map WHERE vehicle_id=?",(vehicle_id,))
    c.execute("DELETE FROM technical_specs WHERE vehicle_id=?",(vehicle_id,))
    cur=c.execute("DELETE FROM vehicles WHERE id=?",(vehicle_id,))
    c.commit(); c.close()
    if cur.rowcount==0: raise HTTPException(404,"Vehículo no encontrado")
    return {"ok":True}

@app.post("/api/admin/repairs")
def add_repair(body: RepairIn, x_admin_key: str|None=Header(default=None)):
    require_admin(x_admin_key)
    c=conn()
    try:
        c.execute("""INSERT INTO repairs(slug,title,category,difficulty,time_estimate,cost_estimate,applies,
                     tools_json,parts_json,steps_json,warnings_json,video_url,source_name,source_url,is_verified)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (body.slug,body.title,body.category,body.difficulty,body.time_estimate,body.cost_estimate,
                   body.applies,json.dumps(body.tools,ensure_ascii=False),json.dumps(body.parts,ensure_ascii=False),
                   json.dumps(body.steps,ensure_ascii=False),json.dumps(body.warnings,ensure_ascii=False),
                   body.video_url,body.source_name,body.source_url,int(body.is_verified)))
        c.commit()
    except sqlite3.IntegrityError:
        c.close(); raise HTTPException(409,"Slug ya existente")
    c.close(); return {"ok":True}

@app.put("/api/admin/repairs/{slug}")
def update_repair(slug:str, body:RepairIn, x_admin_key:str|None=Header(default=None)):
    require_admin(x_admin_key)
    c=conn()
    cur=c.execute("""UPDATE repairs SET slug=?,title=?,category=?,difficulty=?,time_estimate=?,cost_estimate=?,
                     applies=?,tools_json=?,parts_json=?,steps_json=?,warnings_json=?,video_url=?,source_name=?,
                     source_url=?,is_verified=? WHERE slug=?""",
                  (body.slug,body.title,body.category,body.difficulty,body.time_estimate,body.cost_estimate,
                   body.applies,json.dumps(body.tools,ensure_ascii=False),json.dumps(body.parts,ensure_ascii=False),
                   json.dumps(body.steps,ensure_ascii=False),json.dumps(body.warnings,ensure_ascii=False),
                   body.video_url,body.source_name,body.source_url,int(body.is_verified),slug))
    c.commit(); c.close()
    if cur.rowcount==0: raise HTTPException(404,"Reparación no encontrada")
    return {"ok":True}

@app.delete("/api/admin/repairs/{slug}")
def delete_repair(slug:str, x_admin_key:str|None=Header(default=None)):
    require_admin(x_admin_key)
    c=conn()
    row=c.execute("SELECT id FROM repairs WHERE slug=?",(slug,)).fetchone()
    if not row:
        c.close(); raise HTTPException(404,"Reparación no encontrada")
    c.execute("DELETE FROM repair_vehicle_map WHERE repair_id=?",(row["id"],))
    c.execute("DELETE FROM repairs WHERE id=?",(row["id"],))
    c.commit(); c.close(); return {"ok":True}

@app.post("/api/admin/obd")
def add_obd(body:ObdIn, x_admin_key:str|None=Header(default=None)):
    require_admin(x_admin_key)
    c=conn()
    c.execute("""INSERT OR REPLACE INTO obd_codes(code,title,severity,system,causes_json,checks_json,driving_advice,manufacturer)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (body.code.upper().strip(),body.title,body.severity,body.system,
               json.dumps(body.causes,ensure_ascii=False),json.dumps(body.checks,ensure_ascii=False),
               body.driving_advice,body.manufacturer))
    c.commit(); c.close(); return {"ok":True}

@app.delete("/api/admin/obd/{code}")
def delete_obd(code:str, manufacturer:str="Generic", x_admin_key:str|None=Header(default=None)):
    require_admin(x_admin_key)
    c=conn()
    cur=c.execute("DELETE FROM obd_codes WHERE code=? AND manufacturer=?",(code.upper(),manufacturer))
    c.commit(); c.close()
    if cur.rowcount==0: raise HTTPException(404,"Código no encontrado")
    return {"ok":True}

@app.post("/api/admin/compatibility")
def add_compatibility(body:CompatibilityIn, x_admin_key:str|None=Header(default=None)):
    require_admin(x_admin_key)
    c=conn()
    r=c.execute("SELECT id FROM repairs WHERE slug=?",(body.repair_slug,)).fetchone()
    if not r:
        c.close(); raise HTTPException(404,"Reparación no encontrada")
    v=c.execute("SELECT id FROM vehicles WHERE id=?",(body.vehicle_id,)).fetchone()
    if not v:
        c.close(); raise HTTPException(404,"Vehículo no encontrado")
    c.execute("""INSERT OR REPLACE INTO repair_vehicle_map(repair_id,vehicle_id,engine,notes)
                 VALUES(?,?,?,?)""",(r["id"],body.vehicle_id,body.engine,body.notes))
    c.commit(); c.close(); return {"ok":True}

@app.delete("/api/admin/compatibility")
def delete_compatibility(repair_slug:str, vehicle_id:int, engine:str="",
                         x_admin_key:str|None=Header(default=None)):
    require_admin(x_admin_key)
    c=conn()
    r=c.execute("SELECT id FROM repairs WHERE slug=?",(repair_slug,)).fetchone()
    if not r:
        c.close(); raise HTTPException(404,"Reparación no encontrada")
    c.execute("DELETE FROM repair_vehicle_map WHERE repair_id=? AND vehicle_id=? AND engine=?",
              (r["id"],vehicle_id,engine))
    c.commit(); c.close(); return {"ok":True}

@app.get("/api/admin/export")
def export_db(x_admin_key:str|None=Header(default=None)):
    require_admin(x_admin_key)
    c=conn()
    payload={
        "vehicles":[dict(x) for x in c.execute("SELECT * FROM vehicles")],
        "repairs":[repair_row(x) for x in c.execute("SELECT * FROM repairs")],
        "obd":[obd_row(x) for x in c.execute("SELECT * FROM obd_codes")],
        "technical_specs":[dict(x) for x in c.execute("SELECT * FROM technical_specs")],
        "compatibility":[dict(x) for x in c.execute("SELECT * FROM repair_vehicle_map")],
    }
    c.close()
    return JSONResponse(payload)

@app.post("/api/admin/import/specs-csv")
async def import_specs_csv(file:UploadFile=File(...), x_admin_key:str|None=Header(default=None)):
    require_admin(x_admin_key)
    content=(await file.read()).decode("utf-8-sig")
    reader=csv.DictReader(io.StringIO(content))
    required={"vehicle_id","engine","spec_key","spec_value","unit","source_name","source_url","verified"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise HTTPException(400,f"CSV debe incluir: {sorted(required)}")
    c=conn(); n=0
    for row in reader:
        c.execute("""INSERT INTO technical_specs(vehicle_id,engine,spec_key,spec_value,unit,source_name,source_url,verified)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (int(row["vehicle_id"]),row["engine"],row["spec_key"],row["spec_value"],row["unit"],
                   row["source_name"],row["source_url"],int(row["verified"] or 0)))
        n+=1
    c.commit(); c.close()
    return {"ok":True,"imported":n}
