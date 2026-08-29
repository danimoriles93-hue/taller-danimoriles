
from fastapi import FastAPI, HTTPException, Header, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from pathlib import Path
import sqlite3, json, os, csv, io, hashlib, html

from autodata import AutodataProvider
from local import LocalProvider
from cache import ProviderCache
from guides import GUIDES, GUIDES_BY_SLUG, OBD_GUIDES

BASE = Path(__file__).resolve().parent
DB = Path(os.getenv("MOTORFIX_DB_PATH", str(BASE / "motorfix.db")))
ADMIN_KEY = os.getenv("MOTORFIX_ADMIN_KEY", "").strip()

app = FastAPI(title="MotorFix Pro API", version="2.0.0")

local_provider = LocalProvider(DB)
autodata_provider = AutodataProvider()
provider_cache = ProviderCache(DB)

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def require_admin(x_admin_key: str | None):
    if not ADMIN_KEY:
        raise HTTPException(503, "Administración desactivada hasta configurar MOTORFIX_ADMIN_KEY")
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
    c.execute("""CREATE TABLE IF NOT EXISTS vehicles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        brand TEXT NOT NULL,
        model TEXT NOT NULL,
        year_from INTEGER NOT NULL,
        year_to INTEGER NOT NULL,
        engines TEXT NOT NULL DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS repairs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'Otros',
        difficulty INTEGER NOT NULL DEFAULT 2,
        time_estimate TEXT NOT NULL DEFAULT '',
        cost_estimate TEXT NOT NULL DEFAULT '',
        applies TEXT NOT NULL DEFAULT '',
        tools_json TEXT NOT NULL DEFAULT '[]',
        parts_json TEXT NOT NULL DEFAULT '[]',
        steps_json TEXT NOT NULL DEFAULT '[]',
        warnings_json TEXT NOT NULL DEFAULT '[]',
        video_url TEXT NOT NULL DEFAULT '',
        source_name TEXT NOT NULL DEFAULT 'MotorFix Pro',
        source_url TEXT NOT NULL DEFAULT '',
        is_verified INTEGER NOT NULL DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS obd_codes(
        code TEXT NOT NULL,
        title TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'Media',
        system TEXT NOT NULL DEFAULT '',
        causes_json TEXT NOT NULL DEFAULT '[]',
        checks_json TEXT NOT NULL DEFAULT '[]',
        driving_advice TEXT NOT NULL DEFAULT '',
        manufacturer TEXT NOT NULL DEFAULT 'Generic',
        PRIMARY KEY(code, manufacturer)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS technical_specs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_id INTEGER NOT NULL,
        engine TEXT NOT NULL DEFAULT '',
        spec_key TEXT NOT NULL,
        spec_value TEXT NOT NULL,
        unit TEXT NOT NULL DEFAULT '',
        source_name TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        verified INTEGER NOT NULL DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS repair_vehicle_map(
        repair_id INTEGER NOT NULL,
        vehicle_id INTEGER NOT NULL,
        engine TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(repair_id, vehicle_id, engine)
    )""")
    difficulty_map = {"Muy fácil": 1, "Fácil": 2, "Media": 3, "Difícil": 4, "Profesional": 5}
    for item in GUIDES:
        c.execute("""INSERT OR IGNORE INTO repairs(
            slug,title,category,difficulty,time_estimate,cost_estimate,applies,
            tools_json,parts_json,steps_json,warnings_json,source_name,is_verified
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            item["slug"], item["title"], item["category"], difficulty_map.get(item["difficulty"], 3),
            item["time"], item["cost"], "Multimarca; confirmar compatibilidad y procedimiento por vehículo",
            json.dumps(item["tools"], ensure_ascii=False), json.dumps(item["parts"], ensure_ascii=False),
            json.dumps(item["steps"], ensure_ascii=False), json.dumps(item["warnings"], ensure_ascii=False),
            "MotorFix Pro", 0,
        ))
    for code, item in OBD_GUIDES.items():
        c.execute("""INSERT OR IGNORE INTO obd_codes(
            code,title,severity,system,causes_json,checks_json,driving_advice,manufacturer
        ) VALUES(?,?,?,?,?,?,?,?)""", (
            code, item["title"], "Media", "OBD-II",
            json.dumps(item["causes"], ensure_ascii=False), json.dumps(item["checks"], ensure_ascii=False),
            "Diagnostica la causa antes de sustituir componentes.", "Generic",
        ))
    c.commit(); c.close()


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; frame-src https://www.youtube-nocookie.com; "
        "connect-src 'self'; base-uri 'self'; form-action 'self'"
    )
    return response

@app.get("/")
def home():
    return FileResponse(BASE/"index.html")

@app.get("/admin")
def admin():
    admin_file = BASE / "admin.html"
    if not admin_file.exists():
        raise HTTPException(404, "Panel de administración no incluido")
    return FileResponse(admin_file)

@app.get("/legal")
def legal():
    return FileResponse(BASE/"legal.html")


@app.get("/styles.css")
def stylesheet():
    return FileResponse(BASE/"styles.css", media_type="text/css")


SITE_URL = "https://auto-reparacion.onrender.com"


def page_shell(title: str, description: str, canonical: str, body: str, structured_data: dict | list | None = None):
    data = ""
    if structured_data:
        data = f'<script type="application/ld+json">{html.escape(json.dumps(structured_data, ensure_ascii=False), quote=False)}</script>'
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><meta name="description" content="{html.escape(description, quote=True)}">
<meta name="robots" content="index,follow,max-snippet:-1,max-image-preview:large">
<link rel="canonical" href="{html.escape(canonical, quote=True)}">{data}
<style>
:root{{--bg:#081018;--panel:#111b25;--line:#2a3948;--text:#f7fafc;--muted:#a9b6c4;--red:#ff4a43}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(145deg,#071019,#101923);color:var(--text);font:16px/1.65 system-ui,-apple-system,sans-serif}}
a{{color:#fff}}.wrap{{width:min(980px,calc(100% - 32px));margin:auto}}header{{border-bottom:1px solid var(--line);background:#081018e8;position:sticky;top:0}}
header .wrap{{display:flex;justify-content:space-between;align-items:center;padding:14px 0}}.brand{{font-weight:900;text-decoration:none}}.brand b{{color:var(--red)}}
main{{padding:48px 0 72px}}.crumbs{{color:var(--muted);font-size:14px;margin-bottom:20px}}h1{{font-size:clamp(2rem,6vw,3.6rem);line-height:1.05;margin:.2em 0}}
h2{{margin-top:34px}}.lead{{color:#c7d1db;font-size:1.15rem;max-width:760px}}.meta,.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px}}.card span{{display:block;color:var(--muted);font-size:13px}}
ul,ol{{padding-left:24px}}li{{margin:9px 0}}.warning{{border-left:4px solid #ffb020;background:#241c0f;padding:16px 18px;border-radius:12px}}
.cta{{display:inline-block;margin-top:28px;background:var(--red);padding:12px 18px;border-radius:12px;text-decoration:none;font-weight:800}}
footer{{border-top:1px solid var(--line);padding:28px 0;color:var(--muted)}}@media(max-width:700px){{.meta,.grid{{grid-template-columns:1fr}}}}
</style></head><body><header><div class="wrap"><a class="brand" href="/">🔧 Motor<b>Fix</b> Pro</a><a href="/reparaciones">Todas las guías</a></div></header>
<main><div class="wrap">{body}</div></main><footer><div class="wrap">Contenido educativo y orientativo. Confirma los datos críticos en el manual del fabricante.</div></footer></body></html>"""


@app.get("/robots.txt", response_class=Response)
def robots():
    return Response(f"User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/admin/\nSitemap: {SITE_URL}/sitemap.xml\n", media_type="text/plain")


@app.get("/sitemap.xml", response_class=Response)
def sitemap():
    urls = [(f"{SITE_URL}/", "1.0", "weekly"), (f"{SITE_URL}/reparaciones", "0.9", "weekly")]
    urls += [(f"{SITE_URL}/reparaciones/{g['slug']}.html", "0.8", "monthly") for g in GUIDES]
    urls += [(f"{SITE_URL}/obd/{code.lower()}.html", "0.8", "monthly") for code in OBD_GUIDES]
    entries = "".join(
        f"<url><loc>{html.escape(url)}</loc><changefreq>{freq}</changefreq><priority>{priority}</priority></url>"
        for url, priority, freq in urls
    )
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>', media_type="application/xml")


@app.get("/reparaciones", response_class=HTMLResponse)
def guide_index():
    cards = "".join(
        f'<article class="card"><span>{html.escape(g["category"])}</span><h2><a href="/reparaciones/{g["slug"]}.html">{html.escape(g["title"])}</a></h2><p>{html.escape(g["description"])}</p></article>'
        for g in GUIDES
    )
    body = f'<div class="crumbs"><a href="/">Inicio</a> / Guías</div><h1>Guías de reparación y mantenimiento</h1><p class="lead">{len(GUIDES)} guías orientativas con herramientas, seguridad y pasos claros. Selecciona tu vehículo en la portada para afinar las búsquedas.</p><div class="grid">{cards}</div>'
    return HTMLResponse(page_shell("Guías de reparación del coche | MotorFix Pro", "Guías de mantenimiento, diagnóstico y reparaciones habituales del automóvil.", f"{SITE_URL}/reparaciones", body))


@app.get("/reparaciones/{slug}.html", response_class=HTMLResponse)
def guide_page(slug: str):
    g = GUIDES_BY_SLUG.get(slug)
    if not g:
        raise HTTPException(404, "Guía no encontrada")
    tools = "".join(f"<li>{html.escape(x)}</li>" for x in g["tools"]) or "<li>Sin herramientas especiales indicadas</li>"
    parts = "".join(f"<li>{html.escape(x)}</li>" for x in g["parts"]) or "<li>Sin consumibles específicos indicados</li>"
    warnings = "".join(f"<li>{html.escape(x)}</li>" for x in g["warnings"])
    steps = "".join(f"<li>{html.escape(x)}</li>" for x in g["steps"])
    canonical = f"{SITE_URL}/reparaciones/{g['slug']}.html"
    structured = {
        "@context": "https://schema.org", "@type": "HowTo", "name": g["title"],
        "description": g["description"], "dateModified": g["updated"],
        "tool": [{"@type": "HowToTool", "name": x} for x in g["tools"]],
        "supply": [{"@type": "HowToSupply", "name": x} for x in g["parts"]],
        "step": [{"@type": "HowToStep", "position": i + 1, "text": step} for i, step in enumerate(g["steps"])],
    }
    body = f"""<div class="crumbs"><a href="/">Inicio</a> / <a href="/reparaciones">Reparaciones</a> / {html.escape(g['title'])}</div>
<p>{html.escape(g['category'])}</p><h1>{html.escape(g['title'])}</h1><p class="lead">{html.escape(g['description'])}</p>
<div class="meta"><div class="card"><span>Dificultad</span><b>{html.escape(g['difficulty'])}</b></div><div class="card"><span>Tiempo orientativo</span><b>{html.escape(g['time'])}</b></div><div class="card"><span>Coste orientativo</span><b>{html.escape(g['cost'])}</b></div></div>
<p><small>Actualizado: {html.escape(g['updated'])}. Contenido editorial orientativo pendiente de validación para cada vehículo concreto.</small></p>
<h2>Antes de empezar</h2><div class="warning"><ul>{warnings}</ul></div><div class="grid"><div class="card"><h2>Herramientas</h2><ul>{tools}</ul></div><div class="card"><h2>Piezas y consumibles</h2><ul>{parts}</ul></div></div>
<h2>Procedimiento orientativo</h2><div class="card"><ol>{steps}</ol></div><h2>Comprobación final</h2><p>Revisa que no queden fugas, testigos, ruidos o fijaciones pendientes. Confirma siempre pares de apriete, capacidades y referencias en la documentación específica del vehículo.</p>
<a class="cta" href="/?reparacion={g['slug']}#buscador">Buscar para mi vehículo</a>"""
    return HTMLResponse(page_shell(f"{g['title']} — Guía | MotorFix Pro", g["description"], canonical, body, structured))


@app.get("/obd/{code}.html", response_class=HTMLResponse)
def obd_page(code: str):
    key = code.upper()
    item = OBD_GUIDES.get(key)
    if not item:
        raise HTTPException(404, "Código OBD no encontrado")
    causes = "".join(f"<li>{html.escape(x)}</li>" for x in item["causes"])
    checks = "".join(f"<li>{html.escape(x)}</li>" for x in item["checks"])
    canonical = f"{SITE_URL}/obd/{key.lower()}.html"
    body = f'<div class="crumbs"><a href="/">Inicio</a> / Código OBD {key}</div><p>Diagnóstico OBD-II</p><h1>{key}: {html.escape(item["title"])}</h1><p class="lead">{html.escape(item["description"])}</p><div class="warning">Un código identifica un circuito o condición; no demuestra por sí solo qué pieza debe cambiarse.</div><div class="grid"><div class="card"><h2>Posibles causas</h2><ul>{causes}</ul></div><div class="card"><h2>Comprobaciones iniciales</h2><ol>{checks}</ol></div></div><a class="cta" href="/#buscador">Consultar otro código</a>'
    structured = {"@context": "https://schema.org", "@type": "TechArticle", "headline": f"Código OBD {key}: {item['title']}", "description": item["description"], "dateModified": GUIDES[0]["updated"]}
    return HTMLResponse(page_shell(f"Código OBD {key}: {item['title']} | MotorFix Pro", item["description"], canonical, body, structured))


@app.get("/api/public/guides")
def public_guides():
    return GUIDES

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
