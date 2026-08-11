#!/usr/bin/env python3
"""ECO Bee backend: SQLite persistence, GIS assessment and traceability API."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # Local SQLite development does not require PostgreSQL extras.
    psycopg = None
    dict_row = None

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ECOBEE_DB", ROOT / "ecobee.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PAGE_FILES = {
    "/": ROOT / "farmer" / "index.html",
    "/farmer": ROOT / "farmer" / "index.html",
    "/organization": ROOT / "organization" / "index.html",
    "/trace": ROOT / "trace" / "index.html",
}
SPECIES_RADIUS = {"meliponini": 0.3, "cerana": 1.5, "mellifera": 3.0, "dorsata": 5.0}
# Farmer devices open the app through a short-lived LIFF webview. Keep the
# revocable server-side session long enough that closing LINE doesn't require
# another password login. Explicit logout and password resets still revoke it.
SESSION_MS = 30 * 24 * 60 * 60 * 1000
LOGIN_ATTEMPTS = {}
FARMER_PRIVATE_PREFIXES = ("profile_", "seenAlerts_", "uiScale")
ORG_PRIVATE_PREFIXES = ("accounts", "admins", "profile_", "orgSettings")
FARMER_SHARED_WRITES = {"feedback"}
ORG_SHARED_WRITES = {"feedback", "agencyEdits", "plantCatalog"}


class PostgresConnection:
    """Small adapter that keeps the existing qmark SQL portable to psycopg."""
    def __init__(self, url):
        self.raw = psycopg.connect(url, row_factory=dict_row)
        self.raw.execute("SET search_path TO ecobee, public")
    def __enter__(self):
        self.raw.__enter__()
        return self
    def __exit__(self, kind, value, traceback):return self.raw.__exit__(kind,value,traceback)
    def execute(self, sql, params=()):
        # psycopg treats every percent sign in the query text as part of its
        # placeholder syntax. Escape literal SQL wildcards before translating
        # the portable qmark placeholders used throughout this application.
        return self.raw.execute(sql.replace("%", "%%").replace("?", "%s"), params)
    def executescript(self, sql):return self.raw.execute(sql)


def connect():
    if DATABASE_URL:
        if psycopg is None:raise RuntimeError("psycopg is required when DATABASE_URL is set")
        return PostgresConnection(DATABASE_URL)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    if os.environ.get("ECOBEE_ENV") == "production" and not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required in production")
    with connect() as db:
        if not DATABASE_URL:db.executescript("""
        CREATE TABLE IF NOT EXISTS kv_store (
          scope TEXT NOT NULL CHECK(scope IN ('private','shared')),
          key TEXT NOT NULL, value TEXT NOT NULL, updated_at INTEGER NOT NULL,
          PRIMARY KEY(scope,key)
        );
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, phone TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          farm TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hives (
          id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          admin_id INTEGER REFERENCES org_admins(id) ON DELETE SET NULL,
          name TEXT NOT NULL, species TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL,
          radius_km REAL NOT NULL, note TEXT NOT NULL DEFAULT '', is_public INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plants (
          id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          admin_id INTEGER REFERENCES org_admins(id) ON DELETE SET NULL,
          plant_type TEXT NOT NULL, variety TEXT NOT NULL DEFAULT '', months TEXT NOT NULL,
          geometry TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS risk_zones (
          id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
          admin_id INTEGER REFERENCES org_admins(id) ON DELETE SET NULL,
          name TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('safe','danger')),
          geometry TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS movements (
          id TEXT PRIMARY KEY, hive_id TEXT NOT NULL REFERENCES hives(id) ON DELETE CASCADE,
          from_lat REAL, from_lng REAL, to_lat REAL NOT NULL, to_lng REAL NOT NULL,
          reason TEXT NOT NULL DEFAULT '', checked_in_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS harvest_batches (
          id TEXT PRIMARY KEY, hive_id TEXT NOT NULL REFERENCES hives(id) ON DELETE CASCADE,
          batch_code TEXT UNIQUE NOT NULL, product TEXT NOT NULL, harvested_at INTEGER NOT NULL,
          quantity_kg REAL NOT NULL, metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS org_admins (
          id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          password_hash TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS org_sessions (
          token TEXT PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES org_admins(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_verifications (
          user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          safety INTEGER NOT NULL DEFAULT 0, standard INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS org_audit_logs (
          id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL,
          action TEXT NOT NULL, target TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}',
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS password_reset_requests (
          id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
          created_at INTEGER NOT NULL, resolved_at INTEGER, admin_id INTEGER REFERENCES org_admins(id) ON DELETE SET NULL
        );
        """)
        if DATABASE_URL:
            db.executescript("""
            ALTER TABLE hives ADD COLUMN IF NOT EXISTS admin_id bigint REFERENCES org_admins(id) ON DELETE SET NULL;
            ALTER TABLE hives ADD COLUMN IF NOT EXISTS is_public integer NOT NULL DEFAULT 0;
            ALTER TABLE hives ALTER COLUMN user_id DROP NOT NULL;
            ALTER TABLE plants ADD COLUMN IF NOT EXISTS admin_id bigint REFERENCES org_admins(id) ON DELETE SET NULL;
            ALTER TABLE plants ALTER COLUMN user_id DROP NOT NULL;
            ALTER TABLE risk_zones ADD COLUMN IF NOT EXISTS admin_id bigint REFERENCES org_admins(id) ON DELETE SET NULL;
            ALTER TABLE risk_zones ALTER COLUMN user_id DROP NOT NULL;
            CREATE TABLE IF NOT EXISTS password_reset_requests (
              id text primary key, user_id bigint not null references users(id) on delete cascade,
              status text not null default 'pending' check(status in ('pending','approved','rejected')),
              created_at bigint not null, resolved_at bigint, admin_id bigint references org_admins(id) on delete set null
            );
            """)
        admin_password = os.environ.get("ECOBEE_ADMIN_PASSWORD", "")
        if os.environ.get("ECOBEE_ENV") == "production" and len(admin_password) < 12:
            raise RuntimeError("ECOBEE_ADMIN_PASSWORD must contain at least 12 characters in production")
        if not db.execute("SELECT 1 FROM org_admins LIMIT 1").fetchone():
            password = admin_password or secrets.token_urlsafe(18)
            db.execute("INSERT INTO org_admins(email,name,password_hash,created_at) VALUES(?,?,?,?)",
                       (os.environ.get("ECOBEE_ADMIN_EMAIL", "admin@ecobee.go.th"), "ผู้ดูแลระบบ", hash_password(password), now_ms()))
            if not admin_password:
                print(f"ECO Bee one-time admin password: {password}")
        elif admin_password:
            email=os.environ.get("ECOBEE_ADMIN_EMAIL", "admin@ecobee.go.th")
            current=db.execute("SELECT password_hash FROM org_admins WHERE email=?",(email,)).fetchone()
            if current and not verify_password(admin_password,current["password_hash"]):
                db.execute("UPDATE org_admins SET password_hash=? WHERE email=?",(hash_password(admin_password),email))
                db.execute("DELETE FROM org_sessions")
        else:
            weak=db.execute("SELECT id,password_hash FROM org_admins WHERE email='admin@ecobee.go.th'").fetchone()
            if weak and verify_password("admin",weak["password_hash"]):
                password=secrets.token_urlsafe(18)
                db.execute("UPDATE org_admins SET password_hash=? WHERE id=?",(hash_password(password),weak["id"]))
                db.execute("DELETE FROM org_sessions")
                print(f"ECO Bee rotated insecure admin password. New password: {password}")
        migrate_legacy_map_data(db)
        # Remove credentials and obsolete sessions previously stored by the demo UI.
        for key in ("accounts", "admins"):
            row=db.execute("SELECT value FROM kv_store WHERE scope='private' AND key=?",(key,)).fetchone()
            if row:
                try:
                    cleaned=json.loads(row["value"])
                    for item in cleaned:
                        if isinstance(item,dict): item.pop("pass",None); item.pop("password",None)
                    db.execute("UPDATE kv_store SET value=?,updated_at=? WHERE scope='private' AND key=?",(json.dumps(cleaned,ensure_ascii=False),now_ms(),key))
                except (ValueError,TypeError):
                    db.execute("DELETE FROM kv_store WHERE scope='private' AND key=?",(key,))
        db.execute("DELETE FROM kv_store WHERE scope='private' AND key IN ('session','orgSession')")
        db.execute("DELETE FROM sessions WHERE expires_at<=?",(now_ms(),))
        db.execute("DELETE FROM org_sessions WHERE expires_at<=?",(now_ms(),))


def migrate_legacy_map_data(db):
    """One-way import from the former KV map store into the relational source of truth."""
    admin=db.execute("SELECT id FROM org_admins ORDER BY id LIMIT 1").fetchone()
    admin_id=admin["id"] if admin else None
    rows=db.execute("SELECT key,value FROM kv_store WHERE scope='private' AND key LIKE 'myHives_%'").fetchall()
    for row in rows:
        phone=row["key"][len("myHives_"):];user=db.execute("SELECT id FROM users WHERE phone=?",(phone,)).fetchone()
        if not user:continue
        try:items=json.loads(row["value"])
        except (ValueError,TypeError):continue
        for item in items if isinstance(items,list) else []:
            if not isinstance(item,dict):continue
            item_id=str(item.get("backendId") or item.get("id") or new_id("hive"))
            if db.execute("SELECT 1 FROM hives WHERE id=?",(item_id,)).fetchone():continue
            species=item.get("species","meliponini");radius=float(item.get("radiusKm",SPECIES_RADIUS.get(species,.3)))
            try:db.execute("INSERT INTO hives(id,user_id,admin_id,name,species,lat,lng,radius_km,note,is_public,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(item_id,user["id"],None,str(item.get("name","รังผึ้ง"))[:150],species,float(item["lat"]),float(item["lng"]),radius,str(item.get("note",""))[:500],0,int(item.get("createdAt",now_ms()))))
            except (KeyError,ValueError,TypeError):continue
    for key,table in (("parkHives","hives"),("plants","plants"),("safetyZones","risk_zones")):
        row=db.execute("SELECT value FROM kv_store WHERE scope='shared' AND key=?",(key,)).fetchone()
        if not row:continue
        try:items=json.loads(row["value"])
        except (ValueError,TypeError):continue
        for item in items if isinstance(items,list) else []:
            if not isinstance(item,dict):continue
            item_id=str(item.get("backendId") or item.get("id") or new_id(table.rstrip("s")))
            if db.execute(f"SELECT 1 FROM {table} WHERE id=?",(item_id,)).fetchone():continue
            owner=db.execute("SELECT id FROM users WHERE phone=?",(str(item.get("ownerPhone","")),)).fetchone()
            try:
                if table=="hives":
                    species=item.get("species","meliponini")
                    db.execute("INSERT INTO hives(id,user_id,admin_id,name,species,lat,lng,radius_km,note,is_public,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(item_id,owner["id"] if owner else None,None if owner else admin_id,str(item.get("name","รังผึ้ง"))[:150],species,float(item["lat"]),float(item["lng"]),float(item.get("radiusKm",SPECIES_RADIUS.get(species,.3))),str(item.get("note",""))[:500],int(bool(item.get("public",False))),int(item.get("createdAt",now_ms()))))
                elif table=="plants":
                    db.execute("INSERT INTO plants(id,user_id,admin_id,plant_type,variety,months,geometry,created_at) VALUES(?,?,?,?,?,?,?,?)",(item_id,owner["id"] if owner else None,None if owner else admin_id,item["type"],str(item.get("variety","")),json.dumps(item.get("months",[])),json.dumps(item["geom"]),int(item.get("createdAt",now_ms()))))
                else:
                    db.execute("INSERT INTO risk_zones(id,user_id,admin_id,name,status,geometry,note,created_at) VALUES(?,?,?,?,?,?,?,?)",(item_id,owner["id"] if owner else None,None if owner else admin_id,str(item.get("name","พื้นที่")),item.get("status","danger"),json.dumps(item["geom"]),str(item.get("note","")),int(item.get("createdAt",now_ms()))))
            except (KeyError,ValueError,TypeError):continue


def now_ms(): return int(time.time() * 1000)
def new_id(prefix): return f"{prefix}_{secrets.token_hex(8)}"
def token_digest(token): return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    iterations=600_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    try:
        if encoded.startswith("pbkdf2_sha256$"):
            _,iterations,salt_hex,digest_hex=encoded.split("$",3)
            digest=hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(salt_hex),int(iterations))
            return hmac.compare_digest(digest.hex(),digest_hex)
        salt_hex,digest_hex=encoded.split(":",1)
        digest=hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(salt_hex),200_000)
        return hmac.compare_digest(digest.hex(),digest_hex)
    except (ValueError,TypeError):
        return False


def valid_phone(value):
    value=str(value).strip()
    return value.isdigit() and 9 <= len(value) <= 10


def valid_text(value, minimum=1, maximum=200):
    return isinstance(value,str) and minimum <= len(value.strip()) <= maximum


def rate_limited(address):
    now=time.monotonic(); attempts=[x for x in LOGIN_ATTEMPTS.get(address,[]) if now-x<300]
    LOGIN_ATTEMPTS[address]=attempts
    return len(attempts)>=10


def record_login_failure(address):
    LOGIN_ATTEMPTS.setdefault(address,[]).append(time.monotonic())


def record_login_success(address):
    LOGIN_ATTEMPTS.pop(address,None)


def haversine(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lng2-lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))


def geometry_center(geometry):
    coords = geometry.get("coords", [])
    if geometry.get("type") == "point": return float(coords[0]), float(coords[1])
    if not coords: raise ValueError("geometry has no coordinates")
    return sum(x[0] for x in coords)/len(coords), sum(x[1] for x in coords)/len(coords)


class API(SimpleHTTPRequestHandler):
    server_version = "ECOBee/1.0"
    sys_version = ""

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(self)")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://unpkg.com https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com https://cdnjs.cloudflare.com; font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; img-src 'self' data: blob: https:; connect-src 'self' https://nominatim.openstreetmap.org")
        super().end_headers()

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def json_response(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(data)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 2_000_000: raise ValueError("request too large")
        return json.loads(self.rfile.read(size) or b"{}")

    def user(self, db):
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        return db.execute("SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=? AND expires_at>?", (token_digest(token), now_ms())).fetchone()

    def require_user(self, db):
        user = self.user(db)
        if not user: self.json_response({"error":"unauthorized"}, 401)
        return user

    def org_user(self, db):
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        return db.execute("SELECT a.* FROM org_sessions s JOIN org_admins a ON a.id=s.admin_id WHERE s.token=? AND s.expires_at>?", (token_digest(token), now_ms())).fetchone()

    def token(self):
        auth=self.headers.get("Authorization","")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def actor(self, db):
        user=self.user(db)
        if user:return "farmer",user
        admin=self.org_user(db)
        if admin:return "org",admin
        return None,None

    def storage_allowed(self, role, actor, scope, key, write=False):
        if scope not in ("private","shared"):return False
        if role=="org":
            return (not write) or (key in ORG_SHARED_WRITES if scope=="shared" else key.startswith(ORG_PRIVATE_PREFIXES))
        if role!="farmer":return False
        if scope=="shared":return (not write) or key in FARMER_SHARED_WRITES
        phone=actor["phone"]
        if key=="uiScale":return True
        return key.startswith((f"profile_{phone}",f"myHives_{phone}",f"seenAlerts_{phone}"))

    @staticmethod
    def map_payload(db, farmer_id=None):
        hives=db.execute("""SELECT h.*,u.phone owner_phone,u.name owner_name,u.farm owner_farm,
          a.name admin_name FROM hives h LEFT JOIN users u ON u.id=h.user_id
          LEFT JOIN org_admins a ON a.id=h.admin_id ORDER BY h.created_at DESC""").fetchall()
        plants=db.execute("""SELECT p.*,u.phone owner_phone,u.name owner_name,u.farm owner_farm,
          a.name admin_name FROM plants p LEFT JOIN users u ON u.id=p.user_id
          LEFT JOIN org_admins a ON a.id=p.admin_id ORDER BY p.created_at DESC""").fetchall()
        zones=db.execute("""SELECT z.*,u.phone owner_phone,u.name owner_name,u.farm owner_farm,
          a.name admin_name FROM risk_zones z LEFT JOIN users u ON u.id=z.user_id
          LEFT JOIN org_admins a ON a.id=z.admin_id ORDER BY z.created_at DESC""").fetchall()
        visible_hives=[x for x in hives if farmer_id is None or x["user_id"]==farmer_id or bool(x["is_public"])]
        return {
          "hives":[{**dict(x),"mine":farmer_id is not None and x["user_id"]==farmer_id} for x in visible_hives],
          "plants":[{**dict(x),"months":json.loads(x["months"]),"geometry":json.loads(x["geometry"]),"mine":farmer_id is not None and x["user_id"]==farmer_id} for x in plants],
          "risk_zones":[{**dict(x),"geometry":json.loads(x["geometry"]),"mine":farmer_id is not None and x["user_id"]==farmer_id} for x in zones]
        }

    def validate_shared_update(self, db, role, actor, key, value):
        if role=="org":return
        try:new=json.loads(value)
        except ValueError:raise ValueError("invalid_storage_json")
        if not isinstance(new,list):raise ValueError("shared_value_must_be_array")
        row=db.execute("SELECT value FROM kv_store WHERE scope='shared' AND key=?",(key,)).fetchone()
        try:old=json.loads(row["value"]) if row else []
        except ValueError:old=[]
        owner_field="fromPhone" if key=="feedback" else "ownerPhone"
        phone=actor["phone"]
        old_other={str(x.get("id")):x for x in old if isinstance(x,dict) and x.get(owner_field)!=phone}
        new_other={str(x.get("id")):x for x in new if isinstance(x,dict) and x.get(owner_field)!=phone}
        if old_other!=new_other:raise PermissionError("cannot_modify_another_users_data")
        for item in new:
            if not isinstance(item,dict):raise PermissionError("invalid_data_owner")
            if str(item.get("id")) not in old_other and item.get(owner_field)!=phone:raise PermissionError("invalid_data_owner")

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            page_path = path.rstrip("/") or "/"
            if page_path in PAGE_FILES:
                data = PAGE_FILES[page_path].read_bytes(); self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data); return
            if path == "/api/health":
                with connect() as db:db.execute("SELECT 1").fetchone()
                return self.json_response({"status":"ok","service":"eco-bee","database":"postgresql" if DATABASE_URL else "sqlite","time":now_ms()})
            if path.startswith("/api/storage/"):
                _, _, _, scope, key = path.split("/", 4); key = unquote(key)
                with connect() as db:
                    role,actor=self.actor(db)
                    if not self.storage_allowed(role,actor,scope,key):return self.json_response({"error":"forbidden"},403)
                    row = db.execute("SELECT value FROM kv_store WHERE scope=? AND key=?", (scope,key)).fetchone()
                return self.json_response({"value":row["value"]}, 200) if row else self.json_response({"error":"not_found"},404)
            if path == "/api/auth/me":
                with connect() as db:
                    u=self.require_user(db)
                    if not u:return
                    return self.json_response({"user":{"id":u["id"],"phone":u["phone"],"name":u["name"],"farm":u["farm"]}})
            if path == "/api/org/me":
                with connect() as db:
                    a=self.org_user(db)
                    if not a:return self.json_response({"error":"unauthorized"},401)
                    return self.json_response({"admin":{"id":a["id"],"email":a["email"],"name":a["name"]}})
            if path == "/api/hives":
                with connect() as db:
                    u=self.require_user(db)
                    if not u:return
                    rows=db.execute("SELECT * FROM hives WHERE user_id=? ORDER BY created_at DESC",(u["id"],)).fetchall()
                return self.json_response([dict(x) for x in rows])
            if path == "/api/map-data":
                with connect() as db:
                    role,actor=self.actor(db)
                    if not actor:return self.json_response({"error":"unauthorized"},401)
                    return self.json_response(self.map_payload(db,actor["id"] if role=="farmer" else None))
            if path == "/api/org/farmers":
                with connect() as db:
                    if not self.org_user(db): return self.json_response({"error":"unauthorized"},401)
                    rows=db.execute("SELECT u.id,u.phone,u.name,u.farm,u.created_at,coalesce(v.safety,0) safety,coalesce(v.standard,0) standard FROM users u LEFT JOIN user_verifications v ON v.user_id=u.id ORDER BY u.created_at DESC").fetchall()
                return self.json_response([{**dict(x),"verify":{"safety":bool(x["safety"]),"standard":bool(x["standard"])}} for x in rows])
            if path == "/api/org/admins":
                with connect() as db:
                    if not self.org_user(db):return self.json_response({"error":"unauthorized"},401)
                    rows=db.execute("SELECT id,email,name,created_at FROM org_admins ORDER BY created_at").fetchall()
                return self.json_response([dict(x) for x in rows])
            if path == "/api/org/password-reset-requests":
                with connect() as db:
                    if not self.org_user(db):return self.json_response({"error":"unauthorized"},401)
                    rows=db.execute("""SELECT r.id,r.status,r.created_at,r.resolved_at,u.phone,u.name,u.farm
                      FROM password_reset_requests r JOIN users u ON u.id=r.user_id
                      ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END,r.created_at DESC""").fetchall()
                return self.json_response([dict(x) for x in rows])
            if path.startswith("/api/public/farmers/"):
                phone=unquote(path[len("/api/public/farmers/"):])
                with connect() as db:
                    row=db.execute("""SELECT u.phone,u.name,u.farm,u.created_at,coalesce(v.safety,0) safety,
                      coalesce(v.standard,0) standard,(SELECT count(*) FROM hives h WHERE h.user_id=u.id) hive_count,
                      (SELECT count(*) FROM harvest_batches b JOIN hives h ON h.id=b.hive_id WHERE h.user_id=u.id) lot_count
                      FROM users u LEFT JOIN user_verifications v ON v.user_id=u.id WHERE u.phone=?""",(phone,)).fetchone()
                if not row:return self.json_response({"error":"not_found"},404)
                return self.json_response({**dict(row),"phone":phone[:3]+"****"+phone[-3:],"verify":{"safety":bool(row["safety"]),"standard":bool(row["standard"])}})
            if path.startswith("/api/trace/"):
                code=unquote(path.rsplit("/",1)[1])
                with connect() as db:
                    batch=db.execute("""SELECT b.*,h.name hive_name,h.species,h.lat,h.lng,h.radius_km,
                      u.farm,u.name owner,coalesce(v.safety,0) safety,coalesce(v.standard,0) standard
                      FROM harvest_batches b JOIN hives h ON h.id=b.hive_id JOIN users u ON u.id=h.user_id
                      LEFT JOIN user_verifications v ON v.user_id=u.id WHERE batch_code=?""",(code,)).fetchone()
                    if not batch:return self.json_response({"error":"not_found"},404)
                    moves=db.execute("SELECT reason,checked_in_at FROM movements WHERE hive_id=? ORDER BY checked_in_at",(batch["hive_id"],)).fetchall()
                    plant_rows=db.execute("SELECT plant_type,variety,months,geometry FROM plants").fetchall()
                    zone_rows=db.execute("SELECT status,geometry FROM risk_zones").fetchall()
                radius=float(batch["radius_km"]);plant_groups={};food_months=set()
                for plant in plant_rows:
                    plat,plng=geometry_center(json.loads(plant["geometry"]));distance=haversine(batch["lat"],batch["lng"],plat,plng)
                    if distance<=radius:
                        key=(plant["plant_type"],plant["variety"]);plant_groups[key]=plant_groups.get(key,0)+1
                        food_months.update(json.loads(plant["months"]))
                zone_counts={"safe":0,"danger":0}
                for zone in zone_rows:
                    zlat,zlng=geometry_center(json.loads(zone["geometry"]))
                    if haversine(batch["lat"],batch["lng"],zlat,zlng)<=radius:zone_counts[zone["status"]]+=1
                out={
                  "batch_code":batch["batch_code"],"product":batch["product"],"harvested_at":batch["harvested_at"],
                  "quantity_kg":batch["quantity_kg"],"hive_name":batch["hive_name"],"species":batch["species"],
                  "farm":batch["farm"],"owner":batch["owner"],"radius_km":radius,
                  "verification":{"safety":bool(batch["safety"]),"standard":bool(batch["standard"])},
                  "movements":[dict(x) for x in moves],
                  "environment":{"plants":[{"type":k[0],"variety":k[1],"count":count} for k,count in sorted(plant_groups.items())],
                    "plant_count":sum(plant_groups.values()),"safe_zone_count":zone_counts["safe"],
                    "danger_zone_count":zone_counts["danger"],"food_months":sorted(food_months),
                    "coverage_month_count":len(food_months)}
                }
                return self.json_response(out)
            return self.json_response({"error":"not_found"},404)
        except (ValueError, json.JSONDecodeError) as e: self.json_response({"error":str(e)},400)
        except Exception:self.json_response({"error":"internal_error"},500)

    def do_HEAD(self):
        path=urlparse(self.path).path.rstrip("/") or "/"
        if path in PAGE_FILES:
            data=PAGE_FILES[path].read_bytes();self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.end_headers();return
        self.send_response(404);self.send_header("Content-Length","0");self.end_headers()

    def do_PUT(self):
        try:
            path=urlparse(self.path).path
            if path.startswith("/api/storage/"):
                _,_,_,scope,key=path.split("/",4); key=unquote(key)
                if scope not in ("private","shared"):return self.json_response({"error":"invalid_scope"},400)
                value=self.body().get("value")
                if not isinstance(value,str):return self.json_response({"error":"value_must_be_string"},400)
                with connect() as db:
                    role,actor=self.actor(db)
                    if not self.storage_allowed(role,actor,scope,key,True):return self.json_response({"error":"forbidden"},403)
                    if scope=="shared":self.validate_shared_update(db,role,actor,key,value)
                    db.execute("INSERT INTO kv_store(scope,key,value,updated_at) VALUES(?,?,?,?) ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(scope,key,value,now_ms()))
                return self.json_response({"ok":True})
            if path.startswith("/api/org/hives/"):
                hive_id=unquote(path[len("/api/org/hives/"):]);data=self.body()
                with connect() as db:
                    admin=self.org_user(db)
                    if not admin:return self.json_response({"error":"unauthorized"},401)
                    cur=db.execute("UPDATE hives SET is_public=? WHERE id=? AND admin_id=?",(int(bool(data.get("is_public"))),hive_id,admin["id"]))
                return self.json_response({"ok":True}) if cur.rowcount else self.json_response({"error":"not_found"},404)
            return self.json_response({"error":"not_found"},404)
        except PermissionError as e:self.json_response({"error":str(e)},403)
        except (ValueError,TypeError,json.JSONDecodeError) as e:self.json_response({"error":str(e)},400)
        except Exception:self.json_response({"error":"internal_error"},500)

    def do_DELETE(self):
        try:
            path=urlparse(self.path).path
            if path.startswith("/api/org/farmers/"):
                phone=unquote(path[len("/api/org/farmers/"):]).strip()
                if not valid_phone(phone):return self.json_response({"error":"invalid_phone"},400)
                with connect() as db:
                    actor=self.org_user(db)
                    if not actor:return self.json_response({"error":"unauthorized"},401)
                    farmer=db.execute("SELECT id,name,farm,phone FROM users WHERE phone=?",(phone,)).fetchone()
                    if not farmer:return self.json_response({"error":"not_found"},404)
                    # Remove legacy/private UI state as well as relational data. Foreign keys
                    # cascade sessions, hives, harvest lots, movements and verifications.
                    db.execute("DELETE FROM kv_store WHERE scope='private' AND (key=? OR key=? OR key=?)",
                               (f"profile_{phone}",f"myHives_{phone}",f"seenAlerts_{phone}"))
                    for key in ("plants","safetyZones","feedback","agencyEdits"):
                        row=db.execute("SELECT value FROM kv_store WHERE scope='shared' AND key=?",(key,)).fetchone()
                        if not row:continue
                        try:items=json.loads(row["value"])
                        except (ValueError,TypeError):continue
                        if not isinstance(items,list):continue
                        kept=[item for item in items if not (isinstance(item,dict) and phone in {
                            str(item.get("ownerPhone","")),str(item.get("targetPhone","")),str(item.get("fromPhone",""))})]
                        if len(kept)!=len(items):
                            db.execute("UPDATE kv_store SET value=?,updated_at=? WHERE scope='shared' AND key=?",
                                       (json.dumps(kept,ensure_ascii=False),now_ms(),key))
                    db.execute("DELETE FROM users WHERE id=?",(farmer["id"],))
                    db.execute("INSERT INTO org_audit_logs(admin_id,action,target,details,created_at) VALUES(?,?,?,?,?)",
                               (actor["id"],"delete_farmer",phone,json.dumps({"name":farmer["name"],"farm":farmer["farm"]},ensure_ascii=False),now_ms()))
                return self.json_response({"ok":True})
            if path.startswith("/api/org/admins/"):
                admin_id=int(path.rsplit("/",1)[1])
                with connect() as db:
                    actor=self.org_user(db)
                    if not actor:return self.json_response({"error":"unauthorized"},401)
                    if actor["id"]==admin_id:return self.json_response({"error":"cannot_delete_self"},409)
                    if db.execute("SELECT count(*) n FROM org_admins").fetchone()["n"]<=1:return self.json_response({"error":"last_admin"},409)
                    cur=db.execute("DELETE FROM org_admins WHERE id=?",(admin_id,))
                return self.json_response({"ok":True}) if cur.rowcount else self.json_response({"error":"not_found"},404)
            for prefix,table in (("/api/org/hives/","hives"),("/api/org/plants/","plants"),("/api/org/risk-zones/","risk_zones")):
                if path.startswith(prefix):
                    item_id=unquote(path[len(prefix):])
                    with connect() as db:
                        admin=self.org_user(db)
                        if not admin:return self.json_response({"error":"unauthorized"},401)
                        cur=db.execute(f"DELETE FROM {table} WHERE id=?",(item_id,))
                    return self.json_response({"ok":True}) if cur.rowcount else self.json_response({"error":"not_found"},404)
            for prefix,table in (("/api/hives/","hives"),("/api/plants/","plants"),("/api/risk-zones/","risk_zones")):
                if path.startswith(prefix):
                    item_id=unquote(path[len(prefix):])
                    with connect() as db:
                        u=self.require_user(db)
                        if not u:return
                        cur=db.execute(f"DELETE FROM {table} WHERE id=? AND user_id=?",(item_id,u["id"]))
                    return self.json_response({"ok":True}) if cur.rowcount else self.json_response({"error":"not_found"},404)
            return self.json_response({"error":"not_found"},404)
        except (ValueError,TypeError):self.json_response({"error":"invalid_request"},400)
        except Exception:self.json_response({"error":"internal_error"},500)

    def do_POST(self):
        try:
            path=urlparse(self.path).path; data=self.body()
            if path == "/api/auth/register":
                required=("phone","password","name","farm")
                if any(not str(data.get(k,"")).strip() for k in required):return self.json_response({"error":"missing_fields"},400)
                if not valid_phone(data.get("phone")):return self.json_response({"error":"invalid_phone"},400)
                if not valid_text(data.get("password"),8,128):return self.json_response({"error":"weak_password"},400)
                if not valid_text(data.get("name"),2,100) or not valid_text(data.get("farm"),2,150):return self.json_response({"error":"invalid_profile"},400)
                try:
                    with connect() as db:
                        cur=db.execute("INSERT INTO users(phone,name,farm,password_hash,created_at) VALUES(?,?,?,?,?) RETURNING id",(data["phone"],data["name"],data["farm"],hash_password(data["password"]),now_ms()))
                        user_id=cur.fetchone()["id"]
                        token=secrets.token_urlsafe(32); db.execute("INSERT INTO sessions VALUES(?,?,?)",(token_digest(token),user_id,now_ms()+SESSION_MS))
                except (sqlite3.IntegrityError, psycopg.IntegrityError if psycopg else sqlite3.IntegrityError):return self.json_response({"error":"phone_exists"},409)
                return self.json_response({"token":token,"user":{"phone":data["phone"],"name":data["name"],"farm":data["farm"]}},201)
            if path == "/api/auth/login":
                address=self.client_address[0]
                if rate_limited(address):return self.json_response({"error":"too_many_attempts"},429)
                with connect() as db:
                    u=db.execute("SELECT * FROM users WHERE phone=?",(data.get("phone"),)).fetchone()
                    if not u or not verify_password(str(data.get("password","")),u["password_hash"]):
                        record_login_failure(address);return self.json_response({"error":"invalid_credentials"},401)
                    if not u["password_hash"].startswith("pbkdf2_sha256$"):db.execute("UPDATE users SET password_hash=? WHERE id=?",(hash_password(str(data.get("password",""))),u["id"]))
                    record_login_success(address);token=secrets.token_urlsafe(32); db.execute("INSERT INTO sessions VALUES(?,?,?)",(token_digest(token),u["id"],now_ms()+SESSION_MS))
                return self.json_response({"token":token,"user":{"phone":u["phone"],"name":u["name"],"farm":u["farm"]}})
            if path == "/api/auth/password-reset-requests":
                phone=str(data.get("phone","")).strip()
                if not valid_phone(phone):return self.json_response({"error":"invalid_phone"},400)
                with connect() as db:
                    u=db.execute("SELECT id FROM users WHERE phone=?",(phone,)).fetchone()
                    if u and not db.execute("SELECT 1 FROM password_reset_requests WHERE user_id=? AND status='pending'",(u["id"],)).fetchone():
                        db.execute("INSERT INTO password_reset_requests(id,user_id,status,created_at) VALUES(?,?,?,?)",(new_id("reset"),u["id"],"pending",now_ms()))
                # Same response for existing and unknown phone numbers to prevent account enumeration.
                return self.json_response({"ok":True,"message":"request_received"},202)
            if path == "/api/org/auth/login":
                address=self.client_address[0]
                if rate_limited(address):return self.json_response({"error":"too_many_attempts"},429)
                with connect() as db:
                    a=db.execute("SELECT * FROM org_admins WHERE lower(email)=lower(?)",(str(data.get("email","")),)).fetchone()
                    if not a or not verify_password(str(data.get("password","")),a["password_hash"]):
                        record_login_failure(address);return self.json_response({"error":"invalid_credentials"},401)
                    if not a["password_hash"].startswith("pbkdf2_sha256$"):db.execute("UPDATE org_admins SET password_hash=? WHERE id=?",(hash_password(str(data.get("password",""))),a["id"]))
                    record_login_success(address);token=secrets.token_urlsafe(32); db.execute("INSERT INTO org_sessions VALUES(?,?,?)",(token_digest(token),a["id"],now_ms()+SESSION_MS))
                return self.json_response({"token":token,"admin":{"email":a["email"],"name":a["name"]}})
            if path == "/api/auth/logout":
                with connect() as db: db.execute("DELETE FROM sessions WHERE token=?",(token_digest(self.token()),))
                return self.json_response({"ok":True})
            if path == "/api/org/logout":
                with connect() as db: db.execute("DELETE FROM org_sessions WHERE token=?",(token_digest(self.token()),))
                return self.json_response({"ok":True})
            if path == "/api/auth/profile":
                with connect() as db:
                    u=self.require_user(db)
                    if not u:return
                    name=data.get("name",u["name"]);farm=data.get("farm",u["farm"])
                    if not valid_text(name,2,100) or not valid_text(farm,2,150):return self.json_response({"error":"invalid_profile"},400)
                    db.execute("UPDATE users SET name=?,farm=? WHERE id=?",(name.strip(),farm.strip(),u["id"]))
                return self.json_response({"ok":True,"user":{"phone":u["phone"],"name":name.strip(),"farm":farm.strip()}})
            if path.startswith("/api/org/farmers/") and path.endswith("/reset-password"):
                phone=unquote(path.split("/")[4]);password=str(data.get("password",""))
                if not valid_text(password,8,128):return self.json_response({"error":"weak_password"},400)
                with connect() as db:
                    if not self.org_user(db):return self.json_response({"error":"unauthorized"},401)
                    cur=db.execute("UPDATE users SET password_hash=? WHERE phone=?",(hash_password(password),phone))
                    db.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE phone=?)",(phone,))
                return self.json_response({"ok":True}) if cur.rowcount else self.json_response({"error":"not_found"},404)
            if path.startswith("/api/org/password-reset-requests/"):
                request_id=unquote(path.rsplit("/",1)[1]);action=data.get("action")
                if action not in ("approve","reject"):return self.json_response({"error":"invalid_action"},400)
                password=str(data.get("password",""))
                if action=="approve" and not valid_text(password,8,128):return self.json_response({"error":"weak_password"},400)
                with connect() as db:
                    admin=self.org_user(db)
                    if not admin:return self.json_response({"error":"unauthorized"},401)
                    req=db.execute("SELECT * FROM password_reset_requests WHERE id=? AND status='pending'",(request_id,)).fetchone()
                    if not req:return self.json_response({"error":"not_found"},404)
                    if action=="approve":
                        db.execute("UPDATE users SET password_hash=? WHERE id=?",(hash_password(password),req["user_id"]))
                        db.execute("DELETE FROM sessions WHERE user_id=?",(req["user_id"],))
                    db.execute("UPDATE password_reset_requests SET status=?,resolved_at=?,admin_id=? WHERE id=?",("approved" if action=="approve" else "rejected",now_ms(),admin["id"],request_id))
                return self.json_response({"ok":True})
            if path.startswith("/api/org/farmers/") and path.endswith("/profile"):
                phone=unquote(path.split("/")[4]);name=data.get("name");farm=data.get("farm")
                if not valid_text(name,2,100) or not valid_text(farm,2,150):return self.json_response({"error":"invalid_profile"},400)
                with connect() as db:
                    if not self.org_user(db):return self.json_response({"error":"unauthorized"},401)
                    cur=db.execute("UPDATE users SET name=?,farm=? WHERE phone=?",(name.strip(),farm.strip(),phone))
                return self.json_response({"ok":True}) if cur.rowcount else self.json_response({"error":"not_found"},404)
            if path.startswith("/api/org/farmers/") and path.endswith("/verification"):
                phone=unquote(path.split("/")[4]);safety=bool(data.get("safety"));standard=bool(data.get("standard"))
                with connect() as db:
                    if not self.org_user(db):return self.json_response({"error":"unauthorized"},401)
                    u=db.execute("SELECT id FROM users WHERE phone=?",(phone,)).fetchone()
                    if not u:return self.json_response({"error":"not_found"},404)
                    db.execute("INSERT INTO user_verifications(user_id,safety,standard,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET safety=excluded.safety,standard=excluded.standard,updated_at=excluded.updated_at",(u["id"],int(safety),int(standard),now_ms()))
                return self.json_response({"ok":True})
            if path == "/api/org/admins":
                email=str(data.get("email","")).strip().lower();name=data.get("name");password=str(data.get("password",""))
                if "@" not in email or not valid_text(name,2,100) or not valid_text(password,12,128):return self.json_response({"error":"invalid_admin"},400)
                with connect() as db:
                    if not self.org_user(db):return self.json_response({"error":"unauthorized"},401)
                    cur=db.execute("INSERT INTO org_admins(email,name,password_hash,created_at) VALUES(?,?,?,?) RETURNING id",(email,name.strip(),hash_password(password),now_ms()))
                    admin_id=cur.fetchone()["id"]
                return self.json_response({"id":admin_id,"email":email,"name":name.strip()},201)
            if path == "/api/org/profile":
                name=data.get("name")
                if not valid_text(name,2,100):return self.json_response({"error":"invalid_profile"},400)
                with connect() as db:
                    a=self.org_user(db)
                    if not a:return self.json_response({"error":"unauthorized"},401)
                    db.execute("UPDATE org_admins SET name=? WHERE id=?",(name.strip(),a["id"]))
                return self.json_response({"ok":True,"name":name.strip()})
            if path.startswith("/api/org/hives/"):
                hive_id=unquote(path[len("/api/org/hives/"):]);name=data.get("name");radius=float(data.get("radius_km",0))
                if not valid_text(name,1,150) or radius<=0 or radius>20:return self.json_response({"error":"invalid_hive"},400)
                with connect() as db:
                    if not self.org_user(db):return self.json_response({"error":"unauthorized"},401)
                    cur=db.execute("UPDATE hives SET name=?,radius_km=? WHERE id=?",(name.strip(),radius,hive_id))
                return self.json_response({"ok":True}) if cur.rowcount else self.json_response({"error":"not_found"},404)
            if path.startswith("/api/org/") and path in ("/api/org/hives","/api/org/plants","/api/org/risk-zones"):
                with connect() as db:
                    admin=self.org_user(db)
                    if not admin:return self.json_response({"error":"unauthorized"},401)
                    if path=="/api/org/hives":
                        species=data.get("species")
                        if species not in SPECIES_RADIUS:return self.json_response({"error":"invalid_species"},400)
                        hid=new_id("hive");lat,lng=float(data["lat"]),float(data["lng"])
                        db.execute("INSERT INTO hives(id,user_id,admin_id,name,species,lat,lng,radius_km,note,is_public,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(hid,None,admin["id"],str(data.get("name","รังสาธิต"))[:150],species,lat,lng,SPECIES_RADIUS[species],str(data.get("note",""))[:500],int(bool(data.get("is_public",True))),now_ms()))
                        return self.json_response({"id":hid,"radius_km":SPECIES_RADIUS[species]},201)
                    if path=="/api/org/plants":
                        geometry=data["geometry"];geometry_center(geometry);pid=new_id("plant")
                        months=sorted(set(int(m) for m in data.get("months",[]) if 1<=int(m)<=12))
                        db.execute("INSERT INTO plants(id,user_id,admin_id,plant_type,variety,months,geometry,created_at) VALUES(?,?,?,?,?,?,?,?)",(pid,None,admin["id"],data["plant_type"],data.get("variety",""),json.dumps(months),json.dumps(geometry),now_ms()))
                        return self.json_response({"id":pid},201)
                    geometry=data["geometry"];geometry_center(geometry);status=data.get("status")
                    if status not in ("safe","danger"):return self.json_response({"error":"invalid_status"},400)
                    zid=new_id("zone")
                    db.execute("INSERT INTO risk_zones(id,user_id,admin_id,name,status,geometry,note,created_at) VALUES(?,?,?,?,?,?,?,?)",(zid,None,admin["id"],data["name"],status,json.dumps(geometry),data.get("note",""),now_ms()))
                    return self.json_response({"id":zid},201)
            with connect() as db:
                u=self.require_user(db)
                if not u:return
                if path == "/api/hives":
                    species=data.get("species")
                    if species not in SPECIES_RADIUS:return self.json_response({"error":"invalid_species"},400)
                    hid=new_id("hive"); radius=SPECIES_RADIUS[species]
                    lat,lng=float(data["lat"]),float(data["lng"])
                    if not -90<=lat<=90 or not -180<=lng<=180:raise ValueError("invalid_coordinates")
                    if not valid_text(data.get("name","รังผึ้ง"),1,150):raise ValueError("invalid_name")
                    db.execute("INSERT INTO hives(id,user_id,admin_id,name,species,lat,lng,radius_km,note,is_public,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(hid,u["id"],None,data.get("name","รังผึ้ง").strip(),species,lat,lng,radius,str(data.get("note",""))[:500],0,now_ms()))
                    return self.json_response({"id":hid,"radius_km":radius},201)
                if path == "/api/plants":
                    months=sorted(set(int(m) for m in data.get("months",[]) if 1<=int(m)<=12))
                    geometry=data["geometry"]; geometry_center(geometry); pid=new_id("plant")
                    db.execute("INSERT INTO plants(id,user_id,admin_id,plant_type,variety,months,geometry,created_at) VALUES(?,?,?,?,?,?,?,?)",(pid,u["id"],None,data["plant_type"],data.get("variety",""),json.dumps(months),json.dumps(geometry),now_ms()))
                    return self.json_response({"id":pid},201)
                if path == "/api/risk-zones":
                    geometry=data["geometry"]; geometry_center(geometry); status=data.get("status")
                    if status not in ("safe","danger"):return self.json_response({"error":"invalid_status"},400)
                    zid=new_id("zone"); db.execute("INSERT INTO risk_zones(id,user_id,admin_id,name,status,geometry,note,created_at) VALUES(?,?,?,?,?,?,?,?)",(zid,u["id"],None,data["name"],status,json.dumps(geometry),data.get("note",""),now_ms()))
                    return self.json_response({"id":zid},201)
                if path == "/api/assess":
                    lat,lng=float(data["lat"]),float(data["lng"]); species=data.get("species","meliponini"); radius=SPECIES_RADIUS.get(species,float(data.get("radius_km",.3)))
                    plants=db.execute("SELECT * FROM plants").fetchall(); zones=db.execute("SELECT * FROM risk_zones WHERE status='danger'").fetchall()
                    covered=set(); nearby=[]
                    for p in plants:
                        plat,plng=geometry_center(json.loads(p["geometry"])); d=haversine(lat,lng,plat,plng)
                        if d<=radius: covered.update(json.loads(p["months"])); nearby.append({"type":p["plant_type"],"distance_km":round(d,3)})
                    risks=[]
                    for z in zones:
                        zlat,zlng=geometry_center(json.loads(z["geometry"])); d=haversine(lat,lng,zlat,zlng)
                        if d<=radius: risks.append({"id":z["id"],"name":z["name"],"distance_km":round(d,3),"note":z["note"]})
                    missing=[m for m in range(1,13) if m not in covered]
                    score=max(0,round(100-len(missing)*5-len(risks)*20))
                    return self.json_response({"radius_km":radius,"food_months":sorted(covered),"missing_months":missing,"nearby_plants":nearby,"risks":risks,"score":score,"safe":not risks})
                if path == "/api/movements":
                    hive=db.execute("SELECT * FROM hives WHERE id=? AND user_id=?",(data.get("hive_id"),u["id"])).fetchone()
                    if not hive:return self.json_response({"error":"hive_not_found"},404)
                    mid=new_id("move"); lat,lng=float(data["lat"]),float(data["lng"])
                    db.execute("INSERT INTO movements VALUES(?,?,?,?,?,?,?,?)",(mid,hive["id"],hive["lat"],hive["lng"],lat,lng,data.get("reason",""),now_ms()))
                    db.execute("UPDATE hives SET lat=?,lng=? WHERE id=?",(lat,lng,hive["id"]))
                    return self.json_response({"id":mid,"checked_in":True},201)
                if path == "/api/harvests":
                    hive=db.execute("SELECT id FROM hives WHERE id=? AND user_id=?",(data.get("hive_id"),u["id"])).fetchone()
                    if not hive:return self.json_response({"error":"hive_not_found"},404)
                    quantity=float(data["quantity_kg"])
                    if quantity<=0 or quantity>100000:raise ValueError("invalid_quantity")
                    product=str(data.get("product","น้ำผึ้ง")).strip()
                    if not valid_text(product,1,100):raise ValueError("invalid_product")
                    harvested=int(data.get("harvested_at",now_ms()))
                    if harvested>now_ms()+86400000:raise ValueError("invalid_harvest_date")
                    metadata=data.get("metadata",{})
                    if not isinstance(metadata,dict):raise ValueError("invalid_metadata")
                    bid=new_id("batch"); code=data.get("batch_code") or f"ECO-{time.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
                    db.execute("INSERT INTO harvest_batches VALUES(?,?,?,?,?,?,?)",(bid,hive["id"],code,product,harvested,quantity,json.dumps(metadata,ensure_ascii=False)))
                    return self.json_response({"id":bid,"batch_code":code,"trace_url":f"/trace?code={code}"},201)
            return self.json_response({"error":"not_found"},404)
        except (KeyError,ValueError,TypeError,json.JSONDecodeError) as e:self.json_response({"error":str(e)},400)
        except (sqlite3.IntegrityError, psycopg.IntegrityError if psycopg else sqlite3.IntegrityError):self.json_response({"error":"conflict"},409)
        except Exception:self.json_response({"error":"internal_error"},500)


if __name__ == "__main__":
    init_db()
    host=os.environ.get("ECOBEE_HOST","127.0.0.1"); port=int(os.environ.get("PORT",os.environ.get("ECOBEE_PORT","8000")))
    print(f"ECO Bee running at http://{host}:{port}")
    ThreadingHTTPServer((host,port),API).serve_forever()
