import os, sqlite3, json, smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
from typing import Optional
from contextlib import contextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()
DB_PATH = os.getenv("DATABASE_URL", "sqlite:///./faretracker.db").replace("sqlite:///", "")
TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
DUFFEL_VERSION = os.getenv("DUFFEL_VERSION", "v2")
app = FastAPI(title="FareTracker API", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=[os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

def now():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL, destination TEXT NOT NULL,
            depart_date TEXT NOT NULL, return_date TEXT, passengers INTEGER NOT NULL DEFAULT 1,
            cabin TEXT NOT NULL DEFAULT 'economy', target_price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'INR', enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id INTEGER NOT NULL, amount REAL NOT NULL,
            currency TEXT NOT NULL, airline TEXT, source TEXT NOT NULL, checked_at TEXT NOT NULL,
            FOREIGN KEY(watch_id) REFERENCES watches(id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id INTEGER NOT NULL, kind TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0)""")
init_db()

class WatchIn(BaseModel):
    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    depart_date: str
    return_date: Optional[str] = None
    passengers: int = Field(default=1, ge=1, le=9)
    cabin: str = "economy"
    target_price: float = Field(gt=0)
    currency: str = "INR"

def watch_dict(r):
    d = dict(r); d["enabled"] = bool(d["enabled"]); return d

def add_alert(c, wid, kind, message):
    c.execute("INSERT INTO alerts(watch_id,kind,message,created_at) VALUES(?,?,?,?)",
              (wid, kind, message, now()))

def send_email(subject, body):
    host=os.getenv("SMTP_HOST"); recipient=os.getenv("ALERT_EMAIL")
    if not all([host, recipient, os.getenv("SMTP_FROM")]): return
    msg=EmailMessage(); msg["Subject"]=subject; msg["From"]=os.getenv("SMTP_FROM"); msg["To"]=recipient; msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT","587")), timeout=10) as s:
            s.starttls()
            user=os.getenv("SMTP_USERNAME"); password=os.getenv("SMTP_PASSWORD")
            if user: s.login(user,password or "")
            s.send_message(msg)
    except Exception as e:
        print("Email alert failed:", e)

async def get_live_fare(w):
    if not TOKEN:
        return None
    slices=[{"origin":w["origin"],"destination":w["destination"],"departure_date":w["depart_date"]}]
    if w.get("return_date"):
        slices.append({"origin":w["destination"],"destination":w["origin"],"departure_date":w["return_date"]})
    payload={"data":{"slices":slices,"passengers":[{"type":"adult"} for _ in range(w["passengers"])],"cabin_class":w["cabin"]}}
    headers={"Authorization":f"Bearer {TOKEN}","Duffel-Version":DUFFEL_VERSION,"Accept":"application/json","Content-Type":"application/json"}
    async with httpx.AsyncClient(timeout=45) as client:
        response=await client.post("https://api.duffel.com/air/offer_requests?return_offers=true", headers=headers, json=payload)
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Flight provider error ({response.status_code}). Check API credentials and route/date support.")
        data=response.json().get("data",{})
        offers=data.get("offers") or []
        if not offers:
            return None
        offers.sort(key=lambda x: float(x.get("total_amount", "inf")))
        best=offers[0]
        amount=float(best["total_amount"])
        currency=best.get("total_currency","USD")
        # Duffel returns its organisation currency; only compare to target when currencies match.
        airline="Unknown airline"
        try:
            airline=best["slices"][0]["segments"][0]["operating_carrier"]["name"]
        except (KeyError, IndexError, TypeError): pass
        return {"amount":amount,"currency":currency,"airline":airline,"source":"Duffel"}

@app.get("/api/health")
def health():
    return {"ok":True,"live_provider_configured":bool(TOKEN),"provider":"Duffel" if TOKEN else "Not configured"}

@app.get("/api/watches")
def list_watches():
    with db() as c:
        rows=c.execute("SELECT * FROM watches ORDER BY id DESC").fetchall()
        result=[]
        for row in rows:
            w=watch_dict(row)
            p=c.execute("SELECT amount,currency,airline,source,checked_at FROM prices WHERE watch_id=? ORDER BY id DESC LIMIT 1",(w["id"],)).fetchone()
            w["latest_price"]=dict(p) if p else None
            result.append(w)
        return result

@app.post("/api/watches")
def create_watch(w: WatchIn):
    if w.origin.upper()==w.destination.upper(): raise HTTPException(400,"Origin and destination must differ.")
    with db() as c:
        cur=c.execute("""INSERT INTO watches(origin,destination,depart_date,return_date,passengers,cabin,target_price,currency,enabled,created_at)
            VALUES(?,?,?,?,?,?,?,?,1,?)""",
            (w.origin.upper(),w.destination.upper(),w.depart_date,w.return_date,w.passengers,w.cabin,w.target_price,w.currency.upper(),now()))
        return {"id":cur.lastrowid,"message":"Route watch created"}

@app.delete("/api/watches/{watch_id}")
def delete_watch(watch_id:int):
    with db() as c:
        c.execute("DELETE FROM prices WHERE watch_id=?",(watch_id,))
        c.execute("DELETE FROM alerts WHERE watch_id=?",(watch_id,))
        cur=c.execute("DELETE FROM watches WHERE id=?",(watch_id,))
        if not cur.rowcount: raise HTTPException(404,"Watch not found")
    return {"ok":True}

async def check_watch(w):
    try:
        fare=await get_live_fare(w)
    except HTTPException as e:
        return {"watch_id":w["id"],"ok":False,"error":e.detail}
    if fare is None:
        return {"watch_id":w["id"],"ok":False,"demo":not bool(TOKEN),
                "message":"No live fare available. Configure Duffel credentials and try again." if TOKEN else "Live fares are not configured. Add DUFFEL_ACCESS_TOKEN to backend/.env."}
    with db() as c:
        prev=c.execute("SELECT amount,currency FROM prices WHERE watch_id=? ORDER BY id DESC LIMIT 1",(w["id"],)).fetchone()
        c.execute("INSERT INTO prices(watch_id,amount,currency,airline,source,checked_at) VALUES(?,?,?,?,?,?)",
                  (w["id"],fare["amount"],fare["currency"],fare["airline"],fare["source"],now()))
        notes=[]
        if prev and prev["currency"]==fare["currency"]:
            old=float(prev["amount"]); new=fare["amount"]
            if abs(new-old)>=0.01:
                direction="dropped" if new<old else "increased"
                pct=abs((new-old)/old*100) if old else 0
                msg=f'{w["origin"]} → {w["destination"]}: fare {direction} from {old:.2f} to {new:.2f} {fare["currency"]} ({pct:.1f}%).'
                add_alert(c,w["id"],"price_change",msg); notes.append(msg)
                send_email("FareTracker price change",msg)
        if fare["currency"]==w["currency"] and fare["amount"]<=float(w["target_price"]):
            msg=f'{w["origin"]} → {w["destination"]}: target reached at {fare["amount"]:.2f} {fare["currency"]} (target {w["target_price"]:.2f}).'
            add_alert(c,w["id"],"target_reached",msg); notes.append(msg)
            send_email("FareTracker target fare reached",msg)
        return {"watch_id":w["id"],"ok":True,"fare":fare,"alerts":notes}

@app.post("/api/watches/{watch_id}/check")
async def check_one(watch_id:int):
    with db() as c:
        row=c.execute("SELECT * FROM watches WHERE id=?",(watch_id,)).fetchone()
    if not row: raise HTTPException(404,"Watch not found")
    return await check_watch(watch_dict(row))

@app.post("/api/check-all")
async def check_all():
    with db() as c:
        watches=[watch_dict(r) for r in c.execute("SELECT * FROM watches WHERE enabled=1").fetchall()]
    results=[]
    for w in watches: results.append(await check_watch(w))
    return {"checked":len(results),"results":results}

@app.get("/api/watches/{watch_id}/history")
def history(watch_id:int):
    with db() as c:
        rows=c.execute("SELECT amount,currency,airline,source,checked_at FROM prices WHERE watch_id=? ORDER BY id ASC",(watch_id,)).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/alerts")
def alerts():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 100").fetchall()]

@app.post("/api/alerts/read")
def read_alerts():
    with db() as c: c.execute("UPDATE alerts SET read=1")
    return {"ok":True}
