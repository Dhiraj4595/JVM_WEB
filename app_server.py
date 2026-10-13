#!/usr/bin/env python3
"""JVM Shyamali local server with role-based auth and SQLite persistence.

Run with: python app_server.py
This is a secure-by-default local/demo server, not a substitute for HTTPS and
an institution-managed production deployment.
"""
from __future__ import annotations
import argparse
import calendar as pycal
from contextlib import contextmanager
import difflib
import hashlib
from concurrent.futures import ThreadPoolExecutor
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import date as _date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private_data"
DB_PATH = Path(os.environ.get("JVM_DB_PATH") or (PRIVATE / "school.sqlite3"))  # set JVM_DB_PATH to a persistent disk when hosting
TRUST_PROXY = os.environ.get("JVM_TRUST_PROXY", "").lower() in {"1", "true", "yes"}  # set behind a reverse proxy / PaaS
STUDENTS_SEED = PRIVATE / "seed_students.json"
TEACHERS_SEED = PRIVATE / "seed_teachers.json"
MASTER_SEED = PRIVATE / "seed_master.json"
# CORS: only needed if the website pages are hosted on a DIFFERENT address than this server
# (e.g. pages on https://www.school.in, API on https://api.school.in). Leave empty for the normal
# one-service setup. Comma-separated exact origins, e.g. JVM_ALLOWED_ORIGINS=https://www.school.in
ALLOWED_ORIGINS = {o.strip().rstrip("/").lower() for o in os.environ.get("JVM_ALLOWED_ORIGINS", "").split(",") if o.strip() and o.strip() != "*"}
BUNDLED_DB = PRIVATE / "school.sqlite3"
CHAT_LIMIT_PER_MIN = int(os.environ.get("JVM_CHAT_LIMIT", "30") or 30)   # chat messages per minute per IP (the test suite raises this)
SESSION_COOKIE = "jvm_session"
PBKDF2_ROUNDS = 260_000
SESSIONS: dict[str, dict] = {}
LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_conn():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con

@contextmanager
def connection():
    con = db_conn()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def password_hash(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt.hex(), hashed.hex()


def password_matches(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        _, candidate = password_hash(password, bytes.fromhex(salt_hex))
        return hmac.compare_digest(candidate, hash_hex)
    except (ValueError, TypeError):
        return False


def sanitize_student(student: dict) -> dict:
    return {k: v for k, v in student.items() if k not in {"password", "passwordHash", "parentId", "parentPassword", "parentPasswordHash"}}


def teacher_assignment(teacher: dict, students: dict[str, dict]) -> tuple[set[str], str]:
    """Resolve least-privilege academic or activity-only scope from supplied faculty assignments."""
    academic_scope = class_scope_for_teacher(teacher, students)
    if academic_scope:
        return academic_scope, "academic"
    department = str(teacher.get("department", "")).lower()
    subject_text = " ".join(str(s.get("classes", "")) for s in teacher.get("subjects", []) if isinstance(s, dict)).lower()
    all_ids = set(students)
    if "physical education" in department and "all sections" in subject_text:
        return all_ids, "extracurricular"
    if "fine arts" in department and ("primary, middle & secondary" in subject_text or "primary, middle and secondary" in subject_text):
        scope = {sid for sid, rec in students.items()
                 if not re.search(r"\bclass\s+(11|12)\b", str(rec.get("cls", "")), re.I)}
        return scope, "extracurricular"
    if "music" in department and "all wings" in subject_text:
        return all_ids, "extracurricular"
    return set(), "unassigned"


def student_view_for_role(student: dict, role: str, teacher_mode: str = "academic") -> dict:
    """Project each record to the minimum fields that role's UI needs."""
    clean = sanitize_student(student)
    if role == "teacher":
        if teacher_mode == "extracurricular":
            fields = {"id", "name", "avatar", "cls", "sec", "roll", "house", "extracurricular"}
        else:
            fields = {"id", "name", "avatar", "cls", "sec", "roll", "house", "attendance",
                      "academics", "discipline", "remarks", "classTeacher", "classTeacherId"}
        return {k: clean[k] for k in fields if k in clean}
    return clean


def seed_database_if_needed() -> None:
    PRIVATE.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # First start on an empty persistent disk: copy the pre-built database instead of re-hashing every
    # password (which takes ~2 minutes and can make a host's health check time out).
    if not DB_PATH.exists() and BUNDLED_DB.exists() and BUNDLED_DB.resolve() != DB_PATH.resolve():
        import shutil
        shutil.copy2(BUNDLED_DB, DB_PATH)
        print(f"Initialised {DB_PATH} from the bundled database.")
    if not all(p.exists() for p in (STUDENTS_SEED, TEACHERS_SEED, MASTER_SEED)):
        raise RuntimeError("Private seed files are missing; refusing to start with an empty or partial database.")
    students = json.loads(STUDENTS_SEED.read_text(encoding="utf-8"))
    teachers = json.loads(TEACHERS_SEED.read_text(encoding="utf-8"))
    master = json.loads(MASTER_SEED.read_text(encoding="utf-8"))
    con = db_conn()
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS records (
            student_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            role TEXT NOT NULL,
            login_id TEXT NOT NULL,
            salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            student_id TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            aliases TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (role, login_id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            student_id TEXT,
            changed_fields TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS enquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new'
        );
        CREATE INDEX IF NOT EXISTS idx_records_class ON records(json_extract(payload, '$.cls'));
        CREATE INDEX IF NOT EXISTS idx_audit_date ON audit_log(occurred_at);
        """)
        init_fee_tables(con)
        count = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        if count == 0:
            for sid, record in students.items():
                con.execute("INSERT INTO records(student_id,payload,updated_at) VALUES(?,?,?)",
                            (sid, json.dumps(sanitize_student(record), ensure_ascii=False), utc_now()))
        user_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if user_count == 0:
            # Prepare the seed accounts in memory, hash in worker threads, then write
            # everything in one transaction. This preserves modern password hashing
            # while making the initial setup much quicker and preventing partial seeds.
            pending = []
            def queue_user(role, login_id, password, student_id=None, metadata=None, aliases=None):
                salt = secrets.token_bytes(16)
                pending.append((role, login_id.strip().upper(), password, salt.hex(), student_id,
                                json.dumps(metadata or {}, ensure_ascii=False),
                                json.dumps([str(a).strip().upper() for a in (aliases or []) if a]), salt))
            queue_user("master", master["id"], master["password"], metadata={"displayName": master.get("displayName", "Master Administrator")})
            for teacher in teachers:
                queue_user("teacher", teacher["id"], teacher["password"], metadata={
                    "id": teacher["id"], "name": teacher.get("name", "Faculty"),
                    "department": teacher.get("department", ""),
                    "classTeacherOf": teacher.get("classTeacherOf", ""),
                    "teacher": sanitize_student(teacher),
                })
            for sid, record in students.items():
                queue_user("student", sid, record["password"], student_id=sid,
                           metadata={"name": record.get("name", "Student")}, aliases=[record.get("regNo", "")])
            def hash_pending(item):
                role, login_id, password, salt_hex, student_id, metadata, aliases, salt = item
                hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()
                return (role, login_id, salt_hex, hashed, student_id, metadata, aliases)
            worker_count = min(8, max(2, os.cpu_count() or 2))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                hashed_users = list(pool.map(hash_pending, pending))
            con.executemany("INSERT INTO users(role,login_id,salt,password_hash,student_id,metadata,aliases) VALUES(?,?,?,?,?,?,?)", hashed_users)
        # One login serves student and parent: drop any leftover parent accounts / parent login fields.
        con.execute("DELETE FROM users WHERE role='parent'")
        for sid_, pay_ in con.execute("SELECT student_id,payload FROM records WHERE payload LIKE '%parentId%' OR payload LIKE '%parentPassword%'").fetchall():
            rec_ = json.loads(pay_); rec_.pop("parentId", None); rec_.pop("parentPassword", None)
            con.execute("UPDATE records SET payload=? WHERE student_id=?", (json.dumps(rec_, ensure_ascii=False), sid_))
        con.commit()
        expected = len(students)
        actual = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        if actual != expected:
            raise RuntimeError(f"Student record count mismatch: seed={expected}, database={actual}; refusing to continue.")
        expected_users = 1 + len(teachers) + len(students)
        actual_users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if actual_users != expected_users:
            raise RuntimeError(f"Account count mismatch: expected {expected_users}, database={actual_users}; refusing to continue.")
    finally:
        con.close()


def class_scope_for_teacher(teacher: dict, students: dict[str, dict]) -> set[str]:
    """Return explicit student IDs assigned by class/section; broad 'all' scopes are not accepted."""
    phrases: list[str] = []
    class_teacher_of = str(teacher.get("classTeacherOf", ""))
    if class_teacher_of:
        phrases.append(class_teacher_of)
    for subject in teacher.get("subjects", []) or []:
        phrase = subject.get("classes", "") if isinstance(subject, dict) else ""
        if phrase:
            phrases.extend(x.strip() for x in str(phrase).split(",") if x.strip())
    # Teacher metadata may itself include a sanitized copy of the full teacher object.
    allowed: set[str] = set()
    class_names = sorted({str(s.get("cls", "")) for s in students.values()}, key=len, reverse=True)
    for phrase in phrases:
        lower = phrase.lower().strip()
        if not lower or "all section" in lower or "all wing" in lower or "primary, middle" in lower:
            continue
        for class_name in class_names:
            pattern = r"(?<![a-z0-9])" + re.escape(class_name.lower()) + r"(?![a-z0-9])"
            if not re.search(pattern, lower):
                continue
            # Match section only when the assignment specifies one.
            wanted_section = None
            sm = re.search(r"section\s+([a-z])\b", lower)
            if sm:
                wanted_section = sm.group(1).upper()
            else:
                # Common compact notation such as Class 10-A / Class 9-A.
                compact = re.search(re.escape(class_name.lower()) + r"\s*-\s*([a-z])\b", lower)
                if compact:
                    wanted_section = compact.group(1).upper()
            for sid, record in students.items():
                if record.get("cls") != class_name:
                    continue
                sec = str(record.get("sec", ""))
                if wanted_section and not re.search(r"\b" + re.escape(wanted_section) + r"\b", sec, re.I):
                    continue
                # If a teacher explicitly lists a special track (e.g. Rose Section), enforce it.
                special = re.search(r"(rose|lotus)\s+section", lower)
                if special and special.group(1).lower() not in sec.lower():
                    continue
                # If the class label includes a track suffix, match it exactly when identifiable.
                for track in ("pcm + computer science", "pcb + biotechnology", "accountancy & economics",
                              "accountancy & business studies", "liberal arts & economics"):
                    if track in lower and track not in sec.lower():
                        break
                else:
                    allowed.add(sid)
    return allowed


def get_record_map(con) -> dict[str, dict]:
    return {r["student_id"]: json.loads(r["payload"]) for r in con.execute("SELECT student_id,payload FROM records")}


def allowed_ids_for_session(con, sess: dict) -> set[str]:
    role = sess.get("role")
    if role == "master":
        return {r[0] for r in con.execute("SELECT student_id FROM records")}
    if role == "student":
        return {sess["student_id"]} if sess.get("student_id") else set()
    if role == "teacher":
        data = json.loads(sess.get("teacher_json", "{}"))
        scope, mode = teacher_assignment(data, get_record_map(con))
        if mode != sess.get("teacher_mode", "unassigned"):
            return set()  # Fail closed if an in-memory role assignment is inconsistent.
        return scope
    return set()


def records_for_session(con, sess: dict) -> dict[str, dict]:
    allowed = allowed_ids_for_session(con, sess)
    out = {}
    for sid in sorted(allowed):
        row = con.execute("SELECT payload FROM records WHERE student_id=?", (sid,)).fetchone()
        if row:
            out[sid] = student_view_for_role(json.loads(row[0]), sess.get("role", ""), sess.get("teacher_mode", "academic"))
    return out


def public_session(sess: dict) -> dict:
    out = {k: v for k, v in sess.items() if k not in {"teacher_json", "password", "salt", "password_hash"}}
    return out


def seed_record_map() -> dict[str, dict]:
    return json.loads(STUDENTS_SEED.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- fee payments (server-side ledger)
# PAYMENT MODE: JVM_PAYMENT_MODE = "demo" (default; no real money, clearly labelled), "razorpay" (needs
# RAZORPAY_KEY_ID + RAZORPAY_KEY_SECRET) or "disabled". Amounts are always computed on the server.
PAY_MODE = os.environ.get("JVM_PAYMENT_MODE", "demo").strip().lower()
RZP_KEY, RZP_SECRET = os.environ.get("RAZORPAY_KEY_ID", "").strip(), os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
if PAY_MODE == "razorpay" and not (RZP_KEY and RZP_SECRET):
    PAY_MODE = "demo"  # credentials missing: fall back to the clearly labelled demo mode
QUARTERS = [("Q1", "15-Apr-2026", (2026, 4, 15)), ("Q2", "15-Jul-2026", (2026, 7, 15)),
            ("Q3", "15-Oct-2026", (2026, 10, 15)), ("Q4", "15-Jan-2027", (2027, 1, 15))]
ORDER_TTL = 30 * 60


def init_fee_tables(con) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS fee_orders (
        order_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, quarters TEXT NOT NULL, amount INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', provider TEXT NOT NULL, provider_order_id TEXT,
        created_at TEXT NOT NULL, created_epoch REAL NOT NULL, paid_at TEXT, receipt_no TEXT);
    CREATE TABLE IF NOT EXISTS fee_payments (
        receipt_no TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE, student_id TEXT NOT NULL, quarters TEXT NOT NULL,
        amount INTEGER NOT NULL, method TEXT NOT NULL, provider TEXT NOT NULL, provider_payment_id TEXT UNIQUE,
        paid_at TEXT NOT NULL, demo INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS idx_feepay_student ON fee_payments(student_id);
    """)


def fee_summary(rec: dict, payments: list, order=None) -> dict:
    f = rec.get("fees") or {}
    annual = int(f.get("annualTotal") or 0)
    qamt = int(f.get("q3Amt") or (annual // 4))
    qs = f.get("quarterStatus") or {}
    paid_by_q = {}
    for p in payments:
        for q in json.loads(p["quarters"]):
            paid_by_q[q] = p
    inst, today = [], datetime.now(timezone.utc).date()
    for q, due_txt, (y, m, d) in QUARTERS:
        st = str(qs.get(q, "Upcoming"))
        kind = "paid" if st.lower().startswith("paid") else ("due" if re.match(r"(pending|due)", st, re.I) else "upcoming")
        p = paid_by_q.get(q)
        overdue = kind == "due" and datetime(y, m, d).date() < today
        inst.append({"quarter": q, "amount": qamt, "status": kind, "statusText": st, "dueDate": due_txt, "overdue": overdue,
                     "paidOn": p["paid_at"] if p else None, "receipt": p["receipt_no"] if p else (f"OFC-{rec['id'].split('/')[-1]}-{q}" if kind == "paid" else None),
                     "source": ("online" if p else "school office record") if kind == "paid" else None})
    paid = sum(i["amount"] for i in inst if i["status"] == "paid")
    out_now = sum(i["amount"] for i in inst if i["status"] == "due")
    hist = [{"receipt": p["receipt_no"], "txn": p["provider_payment_id"], "amount": p["amount"], "method": p["method"],
             "date": p["paid_at"], "quarters": json.loads(p["quarters"]), "demo": bool(p["demo"]), "source": "online"} for p in payments]
    for i in inst:
        if i["status"] == "paid" and i["source"] != "online":
            hist.append({"receipt": i["receipt"], "txn": None, "amount": i["amount"], "method": "Recorded by school office",
                         "date": i["statusText"].replace("Paid", "").strip(" ()"), "quarters": [i["quarter"]], "demo": False, "source": "office"})
    return {"studentId": rec["id"], "name": rec.get("name"), "cls": rec.get("cls"), "sec": rec.get("sec"), "currency": "INR",
            "totalFees": annual, "paid": paid, "outstanding": out_now, "remaining": max(annual - paid, 0),
            "installments": inst, "history": hist, "gatewayMode": PAY_MODE,
            "pendingOrder": ({"orderId": order["order_id"], "amount": order["amount"]} if order else None),
            "consistent": out_now == int(f.get("dueAmt") or 0)}


def student_payments(con, sid):
    return con.execute("SELECT * FROM fee_payments WHERE student_id=? ORDER BY paid_at", (sid,)).fetchall()


def open_order(con, sid):
    row = con.execute("SELECT * FROM fee_orders WHERE student_id=? AND status='pending' ORDER BY created_epoch DESC LIMIT 1", (sid,)).fetchone()
    if row and time.time() - row["created_epoch"] > ORDER_TTL:
        con.execute("UPDATE fee_orders SET status='expired' WHERE order_id=?", (row["order_id"],)); return None
    return row


def fee_api(handler, sess, path, body):
    """POST /api/fees/create-order and /api/fees/confirm. Returns (payload, status)."""
    if sess.get("role") != "student" or not sess.get("student_id"):
        return {"error": "Fees can only be paid from a Student/Parent login."}, 403
    if PAY_MODE == "disabled":
        return {"error": "Online payment is currently disabled. Please pay at the school office."}, 503
    sid = sess["student_id"]
    con = db_conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT payload FROM records WHERE student_id=?", (sid,)).fetchone()
        if not row: return {"error": "Student record not found."}, 404
        rec = json.loads(row[0]); fees = rec.setdefault("fees", {})
        qamt = int(fees.get("q3Amt") or int(fees.get("annualTotal") or 0) // 4)
        qs = fees.setdefault("quarterStatus", {})
        due_q = [q for q, _, _ in QUARTERS if re.match(r"(pending|due)", str(qs.get(q, "")), re.I)]
        if path == "/api/fees/create-order":
            if not due_q or qamt <= 0:
                return {"error": "No fee is due. Your account is already clear."}, 400
            amount = qamt * len(due_q)
            order = open_order(con, sid)
            if order and order["amount"] == amount and json.loads(order["quarters"]) == due_q:
                reused = True
            else:
                if order: con.execute("UPDATE fee_orders SET status='superseded' WHERE order_id=?", (order["order_id"],))
                oid = "ORD-" + datetime.now(timezone.utc).strftime("%y%m%d%H%M%S") + "-" + secrets.token_hex(3).upper()
                prov_oid = None
                if PAY_MODE == "razorpay":
                    try:
                        import base64
                        rq = urllib.request.Request("https://api.razorpay.com/v1/orders", data=json.dumps({"amount": amount * 100, "currency": "INR", "receipt": oid}).encode(),
                            headers={"content-type": "application/json", "authorization": "Basic " + base64.b64encode(f"{RZP_KEY}:{RZP_SECRET}".encode()).decode()})
                        with urllib.request.urlopen(rq, timeout=20) as r: prov_oid = json.load(r)["id"]
                    except Exception:
                        return {"error": "The payment gateway could not be reached. Nothing was charged. Please try again later."}, 502
                con.execute("INSERT INTO fee_orders(order_id,student_id,quarters,amount,status,provider,provider_order_id,created_at,created_epoch) VALUES(?,?,?,?,?,?,?,?,?)",
                            (oid, sid, json.dumps(due_q), amount, "pending", PAY_MODE, prov_oid, utc_now(), time.time()))
                order = con.execute("SELECT * FROM fee_orders WHERE order_id=?", (oid,)).fetchone(); reused = False
            con.commit()
            out = {"ok": True, "orderId": order["order_id"], "amount": order["amount"], "quarters": due_q, "mode": PAY_MODE, "reused": reused,
                   "demo": PAY_MODE != "razorpay", "student": {"name": rec.get("name"), "id": sid}}
            if PAY_MODE == "razorpay": out.update({"key": RZP_KEY, "providerOrderId": order["provider_order_id"]})
            return out, 200
        # ---- confirm
        oid = str(body.get("order_id", ""))[:60]
        order = con.execute("SELECT * FROM fee_orders WHERE order_id=? AND student_id=?", (oid, sid)).fetchone()
        if not order: return {"error": "Order not found for this account."}, 404
        if order["status"] == "paid":  # duplicate click / retry: return the existing receipt, never charge twice
            return {"ok": True, "duplicate": True, "receipt": order["receipt_no"], "amount": order["amount"], "record": student_view_for_role(rec, "student")}, 200
        if order["status"] != "pending":
            return {"error": f"This payment order is {order['status']}. Start a new payment."}, 409
        if time.time() - order["created_epoch"] > ORDER_TTL:
            con.execute("UPDATE fee_orders SET status='expired' WHERE order_id=?", (oid,)); con.commit()
            return {"error": "This payment session expired. Nothing was charged. Please start again."}, 410
        quarters = json.loads(order["quarters"])
        if order["provider"] == "razorpay":
            pid, sig = str(body.get("razorpay_payment_id", ""))[:60], str(body.get("razorpay_signature", ""))
            good = hmac.new(RZP_SECRET.encode(), f"{order['provider_order_id']}|{pid}".encode(), hashlib.sha256).hexdigest()
            if not pid or not hmac.compare_digest(good, sig):
                con.execute("UPDATE fee_orders SET status='failed' WHERE order_id=?", (oid,)); con.commit()
                return {"error": "Payment could not be verified. No payment has been recorded."}, 400
            method, demo = str(body.get("method", "Razorpay"))[:40], 0
        else:
            if str(body.get("demo_outcome")) != "success":
                con.execute("UPDATE fee_orders SET status='failed' WHERE order_id=?", (oid,)); con.commit()
                return {"ok": False, "failed": True, "error": "Demo payment was declined. No payment has been recorded and your balance is unchanged."}, 200
            pid, method, demo = "DEMO-" + secrets.token_hex(5).upper(), str(body.get("method", "Demo gateway"))[:40], 1
        if any(q not in due_q for q in quarters):
            con.execute("UPDATE fee_orders SET status='stale' WHERE order_id=?", (oid,)); con.commit()
            return {"error": "The fee record changed after this order was created. Nothing was recorded; please refresh."}, 409
        now_ = datetime.now(timezone.utc)
        rno = "RCT-" + now_.strftime("%y%m%d") + "-" + secrets.token_hex(3).upper()
        paid_at = now_.strftime("%d-%b-%Y %H:%M UTC")
        con.execute("INSERT INTO fee_payments VALUES(?,?,?,?,?,?,?,?,?,?)", (rno, oid, sid, order["quarters"], order["amount"], method, order["provider"], pid, paid_at, demo))
        con.execute("UPDATE fee_orders SET status='paid', paid_at=?, receipt_no=? WHERE order_id=?", (paid_at, rno, oid))
        for q in quarters: qs[q] = "Paid (" + now_.strftime("%b %Y") + ")"
        rem = [q for q, _, _ in QUARTERS if re.match(r"(pending|due)", str(qs.get(q, "")), re.I)]
        fees["dueAmt"] = qamt * len(rem)
        fees["status"] = ("Q1 & Q2 Cleared | " if True else "") + (f"{'&'.join(quarters)} Paid (\u20b9 {order['amount']:,})" if not rem else f"{'&'.join(rem)} Due")
        fees["lastPaidDate"], fees["lastTxnId"] = now_.strftime("%d-%b-%Y"), pid
        con.execute("UPDATE records SET payload=?, updated_at=? WHERE student_id=?", (json.dumps(rec, ensure_ascii=False), utc_now(), sid))
        handler._log(con, sess, "fee-payment-demo" if demo else "fee-payment", student_id=sid, fields=["fees"])
        con.commit()
        return {"ok": True, "receipt": rno, "txn": pid, "amount": order["amount"], "demo": bool(demo), "record": student_view_for_role(rec, "student")}, 200
    except Exception:
        con.rollback(); raise
    finally:
        con.close()


def receipt_html(con, rec: dict, no: str):
    pays = student_payments(con, rec["id"]); summ = fee_summary(rec, pays)
    item = next((h for h in summ["history"] if h["receipt"] == no), None)
    if not item: return None
    e = lambda x: str(x if x is not None else "\u2014").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    banner = '<p style="background:#fef3c7;border:2px dashed #b45309;padding:8px;text-align:center;font-weight:bold">DEMO PAYMENT \u2014 no real money was charged. Not a valid fee receipt.</p>' if item["demo"] else ""
    src = "Online payment" if item["source"] == "online" else "Payment recorded in school office records (not an online transaction)"
    return f"""<!doctype html><meta charset=utf-8><title>Receipt {e(no)}</title><body style="font-family:Arial,sans-serif;max-width:640px;margin:24px auto;padding:0 12px">
<h2 style="margin:0">Jawahar Vidya Mandir, Shyamali, Ranchi</h2><p style="margin:2px 0 14px;color:#555">Fee Receipt</p>{banner}
<table style="width:100%;border-collapse:collapse" border=1 cellpadding=8>
<tr><td>Receipt No.</td><td><b>{e(no)}</b></td></tr><tr><td>Date</td><td>{e(item['date'])}</td></tr>
<tr><td>Student</td><td>{e(rec.get('name'))} ({e(rec['id'])})</td></tr><tr><td>Class</td><td>{e(rec.get('cls'))} \u2014 {e(rec.get('sec'))}</td></tr>
<tr><td>Instalment(s)</td><td>{e(', '.join(item['quarters']))} (2026\u201327)</td></tr><tr><td>Amount paid</td><td><b>\u20b9 {item['amount']:,}</b></td></tr>
<tr><td>Method</td><td>{e(item['method'])}</td></tr><tr><td>Transaction ref.</td><td>{e(item['txn'])}</td></tr><tr><td>Source</td><td>{e(src)}</td></tr>
<tr><td>Balance outstanding now</td><td>\u20b9 {summ['outstanding']:,}</td></tr></table>
<p style="color:#666;font-size:12px">Computer-generated receipt.</p><button onclick="print()">Print</button></body>"""


# ---------------------------------------------------------------- chatbot
CHAT_HITS: dict = {}
SITE_LINKS = [
    (r"fee|tuition|payment|scholarship", "Fee structure & calculator", "/pages/admissions.html#fees", "Here is the fee structure and calculator for all classes."),
    (r"timetable|time table|schedule|period|routine", "Class timetables", "/pages/academics.html#timetable", "Pick a class and a day to see its timetable."),
    (r"calendar|holiday|event|vacation|ptm|exam date", "Academic calendar", "/pages/academics.html#calendar", "The academic calendar lists holidays, exams and events."),
    (r"admission|apply|enrol|registration|faq", "Admissions & FAQs", "/pages/admissions.html#faq", "Admission details and common questions are here."),
    (r"topper|result|rank|achievement|news|notice|circular", "News, toppers & notices", "/pages/news.html#news", "Latest notices, results and toppers are here."),
    (r"teacher|faculty|staff|principal", "Our faculty", "/pages/campus.html#faculty", "Meet our faculty here."),
    (r"gallery|photo|picture", "Photo gallery", "/pages/campus.html#gallery", "Take a look at campus life."),
    (r"campus|facilit|library|sports|about|history", "About & campus", "/pages/campus.html#campus", "Learn about the school and its campus."),
    (r"contact|phone|address|email|enquir|location|reach", "Contact the school", "/pages/contact.html", "You can send an enquiry from the contact page."),
    (r"login|portal|sign in|dashboard", "Portals (sign in)", "/pages/portal.html", "Sign in to your portal here."),
]
STOP = set("find search show open get give me the a an of for to go link student students dashboard profile record records please class section is are with and".split())
AI_SYSTEM = ("You are the website assistant for Jawahar Vidya Mandir, Shyamali, Ranchi. Reply in at most four short sentences. "
             "Never invent fees, dates, results, notices or contact details; if unsure, say so and point to the relevant page or the school office. "
             "You have no access to student records. Pages: fees /pages/admissions.html#fees, timetable /pages/academics.html#timetable, "
             "calendar /pages/academics.html#calendar, news /pages/news.html, faculty /pages/campus.html#faculty, contact /pages/contact.html, portals /pages/portal.html.")

def _ai_text(message: str):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        try:
            req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps({
                "model": os.environ.get("JVM_AI_MODEL", "claude-sonnet-5-5"), "max_tokens": 400, "system": AI_SYSTEM,
                "messages": [{"role": "user", "content": message}]}).encode(),
                headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
            if text: return text
        except Exception:
            pass
    model = os.environ.get("JVM_OLLAMA_MODEL", "").strip()
    if model:
        try:
            req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=json.dumps({"model": model, "stream": False,
                "messages": [{"role": "system", "content": AI_SYSTEM}, {"role": "user", "content": message}]}).encode(),
                headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                text = (json.load(r).get("message", {}).get("content") or "").strip()
            if text: return text
        except Exception:
            pass
    return None

def chat_timetable(low: str):
    """'Timetable of Class 10A' -> direct link to that class's timetable (None if not a timetable query)."""
    if not re.search(r"time\s*-?\s*table|schedule|routine|period", low):
        return None
    try:
        classes = json.loads((ROOT / "timetable.json").read_text(encoding="utf-8"))["classes"]
    except Exception:
        return None
    keys = {c["key"]: c for c in classes}
    key = None
    if re.search(r"\bnursery\b", low): key = "nursery"
    elif re.search(r"\bprep\b", low): key = "prep"
    else:
        m = (re.search(r"\b(?:class|std|grade|standard|cls)\s*-?\s*(\d{1,2})(?:st|nd|rd|th)?\s*-?\s*(?:sec(?:tion)?\s*-?\s*)?([ab])?\b", low)
             or re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*-?\s*([ab])\b", low)
             or re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\s*(?:class|std|grade)\b(?:\s*-?\s*(?:sec(?:tion)?\s*)?([ab])\b)?", low)
             or re.search(r"\b(?:class|std|grade|standard)\s*-?\s*(\d{1,2})\b", low))
        if m:
            n, sec = int(m.group(1)), (m.group(2) if m.lastindex and m.lastindex >= 2 else None)
            if n == 10: key = "class-10-" + (sec or "a")
            elif n in (11, 12):
                stream = next((v for k2, v in (("science", "science"), ("commerce", "commerce"), ("humanit", "humanities"), ("arts", "humanities")) if k2 in low), None)
                key = f"class-{n}-{stream}" if stream else None
                if not key:
                    return {"reply": f"Class {n} has Science, Commerce and Humanities timetables. Pick one:", "links": [
                        {"label": f"Class {n} {st.title()} timetable", "url": f"/pages/academics.html?class=class-{n}-{st}#timetable"} for st in ("science", "commerce", "humanities")]}
            elif 1 <= n <= 9: key = f"class-{n}"
    if key and key in keys:
        c = keys[key]
        return {"reply": f"Here is the timetable of {c['label']}. Class teacher: {c.get('classTeacher', '—')}.", "links": [
            {"label": f"{c['label']} — timetable", "url": f"/pages/academics.html?class={key}#timetable"}]}
    return None


def _own_record(sess):
    with connection() as con:
        row = con.execute("SELECT payload FROM records WHERE student_id=?", (sess.get("student_id"),)).fetchone()
        rec = json.loads(row[0]) if row else None
        tdata = None
        if rec and rec.get("classTeacherId"):
            t = con.execute("SELECT metadata FROM users WHERE role='teacher' AND login_id=?", (rec["classTeacherId"],)).fetchone()
            tdata = json.loads(t[0]).get("teacher", {}) if t else None
    return rec, tdata


def chat_my_student(sess, msg: str, low: str):
    """Answers about the signed-in student's own (verified) record only; never another student's."""
    asks_child = re.search(r"receipt|transaction|payment|fee|due\b|attendance|absent|marks?\b|result|grade|rank|remark|activit|house|contact.{0,12}teacher|teacher.{0,20}(phone|email|contact)|\\bmy (son|daughter|child|ward|kid)\\b|\b(my|our)\s+(son|daughter|child|ward|kid)\b|\b(he|she|his|her|him)\b|\babout (me|myself)\b|\bmy (class teacher|subjects?|teachers?|class|section|roll)\b|class teacher|subjects?|teach", low)
    if not asks_child:
        return None
    mentioned = chat_other_ids(msg)
    if mentioned:
        with connection() as con:
            me = {sess.get("student_id", "")}
            u = con.execute("SELECT student_id,aliases FROM users WHERE role='student' AND login_id=?", (sess.get("student_id"),)).fetchone()
            if u: me |= set(json.loads(u["aliases"] or "[]"))
        if any(m not in me for m in mentioned):
            return {"reply": "I can only share the record of the student you are signed in as. Other students' details are private.", "links": []}
    rec, tdata = _own_record(sess)
    if not rec:
        return {"reply": "I could not find your linked student record. Please contact the school office.", "links": []}
    name, first = rec.get("name", "your child"), rec.get("name", "your child").split()[0]
    ct, ctid = rec.get("classTeacher") or "To be assigned", rec.get("classTeacherId", "")
    marks = rec.get("academics", {}).get("marks", [])
    pairs = []
    for m in marks:
        if m.get("s") and (m.get("s"), m.get("teacher")) not in pairs: pairs.append((m.get("s"), m.get("teacher")))
    dash = [{"label": "Open full dashboard", "url": "/pages/portal.html"}]
    if re.search(r"class teacher|who is (his|her|the) teacher|teacher.{0,12}(name|who)", low) and not re.search(r"subject", low):
        room = rec.get("room", "")
        return {"reply": f"{first}'s class teacher is {ct}" + (f" ({ctid})" if ctid else "") + f" for {rec.get('cls')} — {rec.get('sec')}" + (f", {room}." if room else "."), "links": dash}
    if re.search(r"subject|teach", low) and not re.search(r"phone|contact|email|mail|number|room", low):
        out = []
        subs = (tdata or {}).get("subjects", []) if re.search(r"teach", low) and not re.search(r"study|learn|taught|take\b", low) else []
        if subs:
            out.append(f"Class teacher {ct} teaches: " + "; ".join(f"{x.get('subject')} ({x.get('classes')})" for x in subs) + ".")
        if pairs:
            out.append(f"{first}'s subjects and teachers: " + "; ".join(f"{s_} — {t_}" for s_, t_ in pairs) + ".")
        if not out:
            out.append(f"No subject list is recorded yet for {first}. Class teacher: {ct}.")
        return {"reply": "\n".join(out), "links": dash}
    att, acad = rec.get("attendance", {}), rec.get("academics", {})
    fees = rec.get("fees", {})
    def L(t): return {"reply": t, "links": dash}
    if re.search(r"fee|due|pending|paid|payment|pay\b|receipt|transaction|balance|outstanding", low):
        with connection() as _c:
            sm = fee_summary(rec, student_payments(_c, rec["id"]))
        fl = [{"label": "Open Fee Payment", "url": "/pages/portal.html#studentFeePanel"}]
        nxt = next((x for x in sm["installments"] if x["status"] == "due"), None)
        if re.search(r"receipt|history|transaction|paid (on|when)|last payment", low):
            h = sm["history"][-3:]
            return {"reply": f"Payments recorded for {first}: " + ("; ".join(f"{x['receipt']} \u20b9{x['amount']:,} on {x['date']}" + (" (DEMO)" if x['demo'] else "") for x in h) if h else "none yet") + ". Download any receipt from Portal \u2192 Fee Payment.", "links": fl}
        if sm["outstanding"] > 0:
            return {"reply": f"{first}: total fees \u20b9{sm['totalFees']:,}, paid \u20b9{sm['paid']:,}, outstanding now \u20b9{sm['outstanding']:,}" + (f" (due {nxt['dueDate']}" + (", overdue" if nxt['overdue'] else "") + ")" if nxt else "") + ". Pay from the Fee Payment section; the balance changes only after the payment is confirmed.", "links": fl}
        last_payment = fees.get('lastPaidDate') or '—'
        return {"reply": f"No fee is due for {first}. Total \u20b9{sm['totalFees']:,}, paid \u20b9{sm['paid']:,}, remaining (upcoming instalments) \u20b9{sm['remaining']:,}. Last payment: {last_payment}.", "links": fl}
    if re.search(r"attendance|absent|present|leave", low):
        return L(f"{first}'s attendance is {att.get('rate', '—')}: present {att.get('present', '—')} of {att.get('total', '—')} days, {att.get('leavesTaken', '—')} leaves taken, punctuality {att.get('punctuality', '—')}.")
    hit = None if re.search(r"phone|contact|email|mail", low) else next((m for m in marks if m.get("s") and re.search(r"\b" + re.escape(m["s"].split()[0].lower()[:5]), low)), None)
    if hit:
        return L(f"{first}'s {hit['s']}: {hit['m']}/{hit['total']} (grade {hit.get('grade', '—')}), taught by {hit.get('teacher', '—')}.")
    if re.search(r"\bmarks?\b|result|grade|score|rank|percent|aggregate|gpa|perform|topper", low):
        return L(f"{acad.get('term', 'Latest term')}: aggregate {acad.get('aggregate', '—')}, grade {acad.get('grade', '—')}, rank {acad.get('rank', '—')}, GPA {acad.get('gpa', '—')}. Marks: " + "; ".join(f"{m['s']} {m['m']}/{m['total']}" for m in marks) + ".")
    if re.search(r"teacher.{0,20}(phone|contact|email|number|mail|room)|contact.{0,12}teacher", low) and tdata:
        return L(f"Class teacher {ct}: email {tdata.get('email', '—')}, phone {tdata.get('phone', '—')}, room {tdata.get('room', '—')}.")
    if re.search(r"house|roll|section|room|blood|reg(istration)? ?(no|number)", low):
        return L(f"{first}: {rec.get('cls')} — {rec.get('sec')}, roll {rec.get('roll')}, {rec.get('house', '—')}, classroom {rec.get('room', '—')}, blood group {rec.get('blood', '—')}, reg no {rec.get('regNo')}.")
    if re.search(r"remark|behaviou?r|discipline|conduct|feedback", low):
        return L(f"Teacher remarks: {rec.get('remarks', '—')}" + (f"\nDiscipline: {rec['discipline']}" if isinstance(rec.get("discipline"), str) else ""))
    if re.search(r"activit|sport|club|extra|hobby|music|art\b", low):
        ex = rec.get("extracurricular")
        return L(f"{first}'s activities: " + (", ".join(map(str, ex)) if isinstance(ex, list) and ex else str(ex or "none recorded")) + ".")
    lines = [f"{name} studies in {rec.get('cls')} — {rec.get('sec')} (Roll {rec.get('roll')}, {rec.get('regNo')}).",
             f"Class teacher: {ct}" + (f" ({ctid})." if ctid else "."),
             f"Attendance: {att.get('rate', '—')} ({att.get('present', '—')} of {att.get('total', '—')} days).",
             f"{acad.get('term', 'Latest term')}: aggregate {acad.get('aggregate', '—')}, grade {acad.get('grade', '—')}, rank {acad.get('rank', '—')}."]
    if rec.get("remarks"): lines.append(f"Teacher remarks: {rec['remarks']}")
    return {"reply": "\n".join(lines), "links": dash}


def chat_other_ids(msg: str) -> list[str]:
    ids = []
    for m in re.finditer(r"(REG|JVM)\W*(\d{4})\W*(\d{1,8})", msg.upper()):
        n = m.group(3).zfill(4)
        ids.append(f"REG-{m.group(2)}-{n}" if m.group(1) == "REG" else f"JVM/{m.group(2)}/{n}")
    return ids


_CHAT_SPELLING = {
    "hii": "hi", "hiii": "hi", "hiiii": "hi", "helo": "hello", "helllo": "hello", "hlo": "hello",
    "admisson": "admission", "admisison": "admission", "admisions": "admissions", "admisssion": "admission",
    "admisn": "admission", "addmission": "admission", "admmision": "admission", "admsn": "admission",
    "feees": "fees", "feeese": "fees", "feez": "fees", "tution": "tuition", "tutionfee": "tuition",
    "facuty": "faculty", "faculity": "faculty", "techer": "teacher", "teachr": "teacher", "teahcer": "teacher",
    "documnt": "document", "documnts": "documents", "documentaion": "documents", "requrement": "requirement",
    "requrements": "requirements", "requirment": "requirement", "requirments": "requirements",
    "timetble": "timetable", "timetablee": "timetable", "calender": "calendar", "clendar": "calendar",
    "holidy": "holiday", "holidys": "holidays", "studnt": "student", "studnts": "students",
    "stundent": "student", "portel": "portal", "poratl": "portal", "passwrod": "password", "pasword": "password",
    "logn": "login", "loggin": "login", "attendence": "attendance", "acadamics": "academics",
    "acdemics": "academics", "syllbus": "syllabus", "principle": "principal", "transpot": "transport",
    "transprt": "transport", "bonafidee": "bonafide", "paymnet": "payment", "reciept": "receipt",
    "regstration": "registration", "registraton": "registration", "studet": "student", "studnets": "students",
}
_CHAT_DOMAIN_WORDS = tuple(sorted({
    "admission", "admissions", "apply", "application", "enrollment", "enrolment", "fee", "fees", "tuition",
    "faculty", "teacher", "teachers", "students", "student", "documents", "document", "requirements",
    "requirement", "timetable", "calendar", "holiday", "holidays", "academics", "academic", "transport",
    "attendance", "password", "portal", "login", "principal", "syllabus", "homework", "classroom", "campus",
    "library", "sports", "payment", "receipt", "contact", "classes", "class", "curriculum", "scholarship",
    "marksheet", "certificate", "transfer", "bonafide", "science", "commerce", "humanities", "nursery",
    "kindergarten", "school", "examination", "exam", "registration", "eligibility", "cutoff", "stream",
    "student", "studentid", "parent", "parents", "teacher", "section", "vacancy", "vacancies", "fees",
}, key=len))


def normalize_chat_text(message: str) -> str:
    """Normalize common spelling mistakes and school-related phrasing, not arbitrary IDs/names."""
    low = str(message or "").lower().replace("’", "'").strip()
    low = re.sub(r"\bclass\s+(?:three|third|3rd)\b", "class 3", low)
    low = re.sub(r"\b(?:std|standard|grade)\s+(?:three|third|3rd)\b", "class 3", low)
    low = re.sub(r"\b(?:std|standard|grade)\s+(?:eleven|xi|11th)\b", "class 11", low)
    low = re.sub(r"\b(paperwork|papers)\b", "documents", low)
    low = re.sub(r"\b(school fees|tuition fees|fee structure|fees structure)\b", "fee structure", low)
    low = re.sub(r"\b(log ?in|signin|sign-in)\b", "login", low)
    low = re.sub(r"\b(sign ?out|sign-out)\b", "logout", low)

    def correct(match):
        word = match.group(0)
        if word in _CHAT_SPELLING:
            return _CHAT_SPELLING[word]
        # Fuzzy-match only against school-domain keywords; never fuzzy-correct names or IDs.
        if len(word) >= 5 and word.isalpha() and word not in _CHAT_DOMAIN_WORDS:
            close = difflib.get_close_matches(word, _CHAT_DOMAIN_WORDS, n=1, cutoff=0.88)
            if close:
                return close[0]
        return word

    low = re.sub(r"[a-z]+", correct, low)
    low = re.sub(r"\b(?:hii+|helo|hlo)\b", "hello", low)
    return low


def chat_reply(sess, message: str, fee_state=None) -> dict:
    from urllib.parse import quote
    msg = message.strip()[:400]
    low = normalize_chat_text(msg)
    if not msg:
        return {"reply": "Ask me about admissions, fees, the school calendar, academics, faculty, or portals. You can also phrase your question in your own words.", "links": [{"label": "Admissions", "url": "/pages/admissions.html"}, {"label": "Portals", "url": "/pages/portal.html"}]}
    if re.fullmatch(r"(?:(?:hi+|hello+|hey+)(?: there| sir| ma'?am)?|good morning|good afternoon|good evening|namaste)[!?.,\s]*", low):
        return {"reply": "Hello! Welcome to Jawahar Vidya Mandir, Shyamali. I can help with admissions, Class 3 documents, fees, academics, faculty, school timings, the calendar, or student and teacher portals. What would you like to know?", "links": [{"label": "Admissions & fees", "url": "/pages/admissions.html"}, {"label": "Student / teacher portals", "url": "/pages/portal.html"}, {"label": "Contact the school", "url": "/pages/contact.html"}]}
    if re.fullmatch(r"(?:thanks|thank you|thankyou|thx|thank u|goodbye|bye|see you)[!.\s]*", low):
        return {"reply": "You're welcome. For confirmed school notices, application dates and vacancies, please check the official JVM Shyamali website or contact the school office.", "links": [{"label": "Official JVM Shyamali website", "url": "https://www.jvmshyamali.com/home"}, {"label": "Contact the school", "url": "/pages/contact.html"}]}
    role = (sess or {}).get("role", "")
    fee = chat_fee_estimate(low, fee_state)          # public guided fee estimate (no login needed)
    if fee: return fee
    wd = chat_workdays(low)                          # working days / holiday counts for a month
    if wd: return wd
    tt = chat_timetable(low)
    if tt: return tt
    if role == "student":
        mine = chat_my_student(sess, msg, low)
        if mine: return mine
    if not role and re.search(r"\bmy\s+(son|daughter|child|ward)\b|\b(his|her)\s+(class teacher|subjects?|teachers?)\b", low):
        return {"reply": "Your child's details are private. Please sign in first: type \"show my dashboard\", enter the registration number and password, then ask again.", "links": [{"label": "Portals (sign in)", "url": "/pages/portal.html"}]}
    if role != "student" or not re.search(r"\bmy\b|\bour\b", low):
        if not (role in ("teacher", "master") and re.search(r"find|search|student|show|open|dashboard", low) and not re.search(r"holiday|exam|calendar|fee|admission|contact|timing|login|password", low)):
            kb = chat_kb(low, role)
            if kb: return kb
    wants_student = bool(re.search(r"student|dashboard|marks|attendance|record|find|search|show|open", low))
    link_hits = [(label, url, text) for rx, label, url, text in SITE_LINKS if re.search(rx, low)]
    tokens = [t for t in re.findall(r"[a-z0-9/]+", low) if t not in STOP and len(t) > 1]
    if role in ("teacher", "master") and tokens and (wants_student or not link_hits):
        with connection() as con:
            records = records_for_session(con, sess)
        hits = [r for r in records.values() if all(t in (str(r.get("name", "")) + " " + str(r.get("id", "")) + " " + str(r.get("cls", "")) + " " + str(r.get("sec", ""))).lower() for t in tokens)]
        if hits:
            hits.sort(key=lambda r: (str(r.get("cls")), int(r.get("roll") or 0)))
            links = [{"label": f"{r['name']} \u2014 {r['cls']} ({r['id']})", "url": "/pages/portal.html?student=" + quote(str(r["id"]), safe="")} for r in hits[:6]]
            more = f" (showing the first 6 of {len(hits)})" if len(hits) > 6 else ""
            return {"reply": f"I found {len(hits)} matching student(s){more}. Tap a name to open the dashboard.", "links": links}
    if role == "student" and re.search(r"my|dashboard|marks|attendance|result|record", low):
        return {"reply": "Here is your dashboard.", "links": [{"label": "Open my dashboard", "url": "/pages/portal.html"}]}
    if not role and wants_student and re.search(r"student|dashboard|marks|attendance", low):
        return {"reply": "Student dashboards are private. Please sign in to the portal first (faculty can then search any student by name here).", "links": [{"label": "Portals (sign in)", "url": "/pages/portal.html"}]}
    links = [{"label": l, "url": u} for l, u, _ in link_hits[:3]]
    ai = _ai_text(msg)
    if ai: return {"reply": ai, "links": links, "ai": True}
    if link_hits: return {"reply": link_hits[0][2], "links": links}
    return {"reply": FALLBACK, "links": [{"label": "Fee structure", "url": "/pages/admissions.html#fees"}, {"label": "Timetables", "url": "/pages/academics.html#timetable"}, {"label": "Contact", "url": "/pages/contact.html"}], "options": FALLBACK_OPTIONS}

# ---------------------------------------------------------------- chatbot knowledge base (public school info only)
_CAL = {"mtime": None, "events": []}
def cal_events():
    """Read the academic calendar straight from the site's own JVM_ACADEMIC_EVENTS list (no duplicate static copy)."""
    try:
        p = ROOT / "index.html"; m = p.stat().st_mtime
        if _CAL["mtime"] != m:
            txt = p.read_text(encoding="utf-8")
            ev = [{"date": a, "title": b.replace("\\'", "'"), "type": c, "desc": d.replace("\\'", "'"), "end": e or ""} for a, b, c, d, e in
                  re.findall(r"\{\s*date:\s*'(\d{4}-\d\d-\d\d)',\s*title:\s*'((?:[^'\\]|\\.)*)',\s*type:\s*'(\w+)',\s*desc:\s*'((?:[^'\\]|\\.)*)'(?:[^}]*?\bend:\s*'(\d{4}-\d\d-\d\d)')?", txt)]
            _CAL.update(mtime=m, events=sorted(ev, key=lambda e: e["date"]))
    except Exception:
        pass
    return _CAL["events"]

def _fmt_ev(e):
    d0 = datetime.strptime(e["date"], "%Y-%m-%d").strftime("%d %b %Y")
    if e.get("end") and e["end"] != e["date"]:
        d0 += " \u2013 " + datetime.strptime(e["end"], "%Y-%m-%d").strftime("%d %b %Y")
    return d0 + " \u2014 " + e["title"]

def chat_calendar(low):
    evs, today = cal_events(), datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not evs: return None
    kind = "holiday" if re.search(r"holiday|vacation|closed|off\b", low) else None
    if re.search(r"\bexam|test|assessment|half.?yearly|unit test|board", low): kind = "exam"
    elif re.search(r"\bptm\b|parent.?teacher|report card", low): kind = "ptm"
    pool = [e for e in evs if (e.get("end") or e["date"]) >= today and (kind is None or (kind == "holiday" and e["type"] == "holiday") or
            (kind == "exam" and re.search(r"exam|test|assessment|term", e["title"], re.I)) or (kind == "ptm" and re.search(r"ptm|parent|report card", e["title"], re.I)))]
    label = {"holiday": "holidays", "exam": "exams/tests", "ptm": "parent\u2013teacher meetings"}.get(kind, "events")
    if not pool: return {"reply": f"I could not find any upcoming {label} in the calendar. Please check the full calendar or ask the school office.", "links": [{"label": "Academic calendar", "url": "/pages/academics.html#calendar"}]}
    return {"reply": f"Upcoming {label} (from the school calendar): " + "; ".join(_fmt_ev(e) for e in pool[:4]) + ". Dates can change \u2014 confirm with the school office.", "links": [{"label": "Full academic calendar", "url": "/pages/academics.html#calendar"}]}


# ---------------------------------------------------------------- v10: working days / holiday counts (chatbot)
IST = timezone(timedelta(hours=5, minutes=30))
_MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
           "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10,
           "november": 11, "nov": 11, "december": 12, "dec": 12}
_MONTH_RX = r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b"
_NAMED_BREAK_RX = r"\b(summer|winter|autumn|diwali|deepawali|chhath|holi|christmas|dussehra|durga|puja|eid|id[- ]ul|muharram|rath)\b"
CAL_LINK = [{"label": "Academic calendar", "url": "/pages/academics.html#calendar"}]


def _holiday_map(events):
    """date -> [event, ...] for every day covered by a holiday / break (multi-day breaks use 'end')."""
    out: dict = {}
    for e in events:
        if e.get("type") != "holiday":
            continue
        try:
            d0 = _date.fromisoformat(e["date"]); d1 = _date.fromisoformat(e.get("end") or e["date"])
        except ValueError:
            continue
        d = d0
        while d <= d1:
            out.setdefault(d, []).append(e)
            d += timedelta(days=1)
    return out


def _coverage(events):
    """First and last month covered by the holiday list, as (year, month) tuples."""
    days = sorted(_holiday_map(events))
    if not days:
        return None
    return (days[0].year, days[0].month), (days[-1].year, days[-1].month)


def _month_from_text(low, today, cov):
    """Returns (year, month) the visitor means; defaults to the current month."""
    y, m = today.year, today.month
    named = re.search(_MONTH_RX, low)
    if named:
        m = _MONTHS[named.group(1)]
        yr = re.search(r"\b(20\d\d)\b", low)
        if yr:
            y = int(yr.group(1))
        else:
            start_year = cov[0][0] if cov else today.year       # session runs April..March
            y = start_year if m >= 4 else start_year + 1
        return y, m
    if re.search(r"\bnext month\b", low):
        m += 1
    elif re.search(r"\b(last|previous|past) month\b", low):
        m -= 1
    if m == 13: y, m = y + 1, 1
    if m == 0: y, m = y - 1, 12
    return y, m


def _short_day(d):
    return d.strftime("%a ") + str(d.day) + d.strftime(" %b")


def _range_text(e):
    d0 = _date.fromisoformat(e["date"])
    if not e.get("end") or e["end"] == e["date"]:
        return _short_day(d0)
    d1 = _date.fromisoformat(e["end"])
    return f"{d0.day} {d0.strftime('%b')}" + (f" {d0.year}" if d0.year != d1.year else "") + f" \u2013 {d1.day} {d1.strftime('%b %Y')}"


def month_working_days(year, month, events, from_day=None):
    """Counts for one month. School week = Monday-Saturday (Sundays off). A day is a holiday when the school's
    Holiday List covers it. Returns a dict; every day of the range is exactly one of working / holiday / Sunday."""
    hol = _holiday_map(events)
    last = pycal.monthrange(year, month)[1]
    working = sundays = 0
    hol_days = []                # Mon-Sat days that are holidays
    sunday_hol = []              # holidays that fall on a Sunday (already a weekly off)
    first = from_day.day if (from_day and (from_day.year, from_day.month) == (year, month)) else 1
    for n in range(first, last + 1):
        d = _date(year, month, n)
        if d.weekday() == 6:
            sundays += 1
            if d in hol: sunday_hol.append(d)
        elif d in hol:
            hol_days.append(d)
        else:
            working += 1
    # attribute each holiday day to one event (longest break first) so a festival inside a vacation is not double counted
    evs = sorted({id(e): e for es in hol.values() for e in es}.values(), key=lambda e: -((_date.fromisoformat(e.get("end") or e["date"]) - _date.fromisoformat(e["date"])).days))
    claimed, items = set(), []
    for e in evs:
        e0 = _date.fromisoformat(e["date"]); e1 = _date.fromisoformat(e.get("end") or e["date"])
        mine = [d for d in hol_days if e0 <= d <= e1 and d not in claimed]
        inside = [d for d in sunday_hol if e0 <= d <= e1]
        if not mine and not (inside and e0 <= _date(year, month, last) and e1 >= _date(year, month, first)):
            continue
        claimed.update(mine)
        items.append({"event": e, "days": len(mine), "start": e0})
    items.sort(key=lambda i: i["start"])
    return {"year": year, "month": month, "first": first, "last": last, "working": working, "holidays": len(hol_days),
            "sundays": sundays, "items": items, "sunday_holidays": sunday_hol}


def _workday_reply(st, today, remaining, holiday_first=False):
    mname = _date(st["year"], st["month"], 1).strftime("%B %Y")
    W, H, S = st["working"], st["holidays"], st["sundays"]
    span = st["last"] - st["first"] + 1
    if remaining:
        head = (f"From today ({_short_day(today)}) to {st['last']} {_date(st['year'], st['month'], 1).strftime('%B')} {st['year']}: "
                f"{W} working day{'s' if W != 1 else ''} and {H} holiday{'s' if H != 1 else ''}.")
    else:
        wtxt, htxt = f"{W} working day{'s' if W != 1 else ''}", f"{H} holiday{'s' if H != 1 else ''}"
        head = f"{mname}: " + (f"{htxt} and {wtxt}" if holiday_first else f"{wtxt} and {htxt}") + " (school week is Monday\u2013Saturday)."
    lines = [head, f"\u2022 Working days: {W}", f"\u2022 Holidays on school days (Mon\u2013Sat): {H}"]
    for it in st["items"]:
        e = it["event"]; multi = e.get("end") and e["end"] != e["date"]
        if it["days"]:
            tail = f": {it['days']} school day{'s' if it['days'] != 1 else ''} in this month" if multi else ""
            lines.append(f"    - {e['title']} ({_range_text(e)}){tail}")
        else:
            why = "falls on a Sunday (already a weekly off)" if _date.fromisoformat(e["date"]).weekday() == 6 else "already counted in the break above"
            lines.append(f"    - {e['title']} ({_range_text(e)}) \u2013 {why}")
    lines.append(f"\u2022 Sundays (weekly off): {S}")
    lines.append(f"Check: {span} days = {W} working + {H} holidays + {S} Sundays.")
    notes = []
    if any(("*" in it["event"]["title"] or "#" in it["event"]["title"] or "subject to change" in it["event"]["desc"].lower()) for it in st["items"]):
        notes.append("Holidays marked * or # are subject to change (moon sighting / official announcement).")
    if (st["year"], st["month"]) == (2026, 4):
        notes.append("The new session starts on different dates by class (6 Apr for UKG\u2013V, 13 Apr for LKG), so April working days differ by class.")
    notes.append("Counted from the school's 2026\u201327 Holiday List only; exam days and class-wise events are not deducted. Confirm with the school office.")
    return "\n".join(lines) + "\n" + " ".join(notes)


def chat_workdays(low, today=None):
    """'How many working days / holidays this month (or in <month>)?' -> counts from the site's own calendar. None if not this topic."""
    work_q = re.search(r"\bwork(?:ing)?\s*-?\s*days?\b|\bschool\s*-?\s*days?\b|\bdays?\b.{0,30}\b(?:school (?:is )?open|school (?:is )?working)\b|\bhow many days\b.{0,40}\b(?:school|open|working)\b", low)
    hol_word = re.search(r"\b(holiday|holidays|vacation|vacations|break|breaks|closed|off days?)\b", low)
    count_q = re.search(r"\b(how many|number of|no\.? of|count|total|kitne)\b", low)
    month_named = re.search(_MONTH_RX, low) or re.search(r"\b(this|current|present|next|last|previous|past) month\b", low)
    if not (work_q or (hol_word and (month_named or (count_q and not re.search(_NAMED_BREAK_RX, low))))):
        return None
    if re.search(r"\b(timetable|time table|exam|test|ptm)\b", low) and not work_q:
        return None
    today = today or datetime.now(IST).date()
    events = cal_events()
    cov = _coverage(events)
    if not cov:
        return {"reply": "I could not read the school calendar just now. Please open the academic calendar or ask the school office.", "links": CAL_LINK}
    y, m = _month_from_text(low, today, cov)
    label = _date(y, m, 1).strftime("%B %Y")
    if (y, m) < cov[0] or (y, m) > cov[1]:
        a = _date(cov[0][0], cov[0][1], 1).strftime("%B %Y"); b = _date(cov[1][0], cov[1][1], 1).strftime("%B %Y")
        return {"reply": f"The school calendar on this website covers {a} to {b}, so I can't give a reliable working-day or holiday count for {label}. Please check the school office for that month.", "links": CAL_LINK}
    remaining = bool(re.search(r"\b(left|remaining|rest of|still|more)\b", low)) and (y, m) == (today.year, today.month)
    st = month_working_days(y, m, events, from_day=today if remaining else None)
    return {"reply": _workday_reply(st, today, remaining, holiday_first=bool(hol_word and not work_q)), "links": CAL_LINK,
            "options": ["Working days next month", "Holidays next month", "Next holiday"]}


# ---------------------------------------------------------------- v10: public fee estimator (chatbot, no login)
# Figures mirror the Fee Structure table and the Interactive Fee Estimator on the Admissions page.
FEE_TIERS = {
    "nursery": {"label": "Nursery & Prep", "qtr": 11520, "adm": 31300},
    "c1-3": {"label": "Classes I\u2013III", "qtr": 12240, "adm": 31300},
    "c4-5": {"label": "Classes IV\u2013V", "qtr": 12630, "adm": 31300},
    "c6-8": {"label": "Classes VI\u2013VIII", "qtr": 13470, "adm": 37350},
    "c9-10": {"label": "Classes IX\u2013X", "qtr": 14370, "adm": 37350},
    "c11-sci": {"label": "Class XI Science", "qtr": 16710, "adm": 44500},
    "c12-sci": {"label": "Class XII Science", "qtr": 16710, "adm": None},
    "c12-com": {"label": "Class XII Commerce / Arts", "qtr": 15390, "adm": None},
}
FEE_BUS = {"z1": ("Zone 1 \u2014 Shyamali / Doranda", 1800), "z2": ("Zone 2 \u2014 Lalpur / Kadru", 2400), "z3": ("Zone 3 \u2014 Bariatu / Morabadi", 3000)}
FEE_DEV_QTR, FEE_DEV_ANNUAL = 1125, 4500
FEE_LINKS = [{"label": "Fee structure & calculator", "url": "/pages/admissions.html#fees"}, {"label": "Contact the school", "url": "/pages/contact.html"}]
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
            "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
_NUM_TOKEN = r"(\d{1,2}|xii|xi|x|ix|viii|vii|vi|iv|v|iii|ii|i|" + "|".join(sorted(_WORDNUM, key=len, reverse=True)) + r")"


def _inr(n):
    s = str(int(n))
    if len(s) <= 3: return "\u20b9" + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:]); head = head[:-2]
    if head: parts.insert(0, head)
    return "\u20b9" + ",".join(parts + [tail])


def _num(tok):
    tok = tok.lower()
    return int(tok) if tok.isdigit() else _ROMAN.get(tok) or _WORDNUM.get(tok) or 0


def _fee_parse_class(low, bare=False):
    """-> 'nursery' | '1'..'12' | 'bad' (outside Nursery-XII) | None"""
    if re.search(r"\b(nursery|prep|preparatory|lkg|ukg|kg|kindergarten|pre.?primary|play ?group)\b", low):
        return "nursery"
    pre = r"(?:class(?:es)?|std|standard|grade|cls)\s*-?\s*"
    m = re.search(r"\b" + pre + _NUM_TOKEN + r"(?:st|nd|rd|th)?\s*(?:-|\u2013|\u2014|to)\s*" + _NUM_TOKEN + r"(?:st|nd|rd|th)?\b", low)
    if not m and bare:
        m = re.fullmatch(r"\s*(?:" + pre + r")?" + _NUM_TOKEN + r"(?:st|nd|rd|th)?\s*(?:-|\u2013|\u2014|to)\s*" + _NUM_TOKEN + r"(?:st|nd|rd|th)?\s*", low)
    if m:
        n = _num(m.group(1))
        return str(n) if 1 <= n <= 12 else "bad"
    m = (re.search(r"\b" + pre + _NUM_TOKEN + r"(?:st|nd|rd|th)?\b", low)
         or re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\s*(?:class|std|standard|grade)?\b", low)
         or re.search(r"\b(xii|xi|ix|viii|vii|vi|iv|iii|ii)\s*(?:class|std|standard|grade)\b", low))
    if not m and bare:
        m = re.fullmatch(r"\s*" + _NUM_TOKEN + r"(?:st|nd|rd|th)?\s*(?:class|std|standard|grade)?\s*", low)
    if m:
        n = _num(m.group(1))
        return str(n) if 1 <= n <= 12 else "bad"
    return None


def _fee_parse_stream(low):
    if re.search(r"\b(science|pcm|pcb|biotech\w*|computer|comp\.? sci\w*|cs|ai)\b", low): return "science"
    if re.search(r"\b(commerce|accountancy|business studies)\b", low): return "commerce"
    if re.search(r"\b(arts|humanities|humanity)\b", low): return "arts"
    return None


def _fee_parse_cat(low):
    if re.search(r"\b(existing|re.?enrol\w*|re.?admission|current (?:student|class)|already (?:studies|study|studying)|continuing|old student|old|internal)\b", low): return "existing"
    if re.search(r"\b(new|fresh|external|outside|first time|newly|new admission)\b", low): return "new"
    return None


def _fee_parse_bus(low, bare=False):
    if re.search(r"\b(no bus|without (?:a )?bus|no transport|walking|private|own transport|not (?:need|required))\b", low) or (bare and re.fullmatch(r"\s*(none|no|nope|nothing)\s*", low)): return "none"
    if re.search(r"\b(zone\s*-?\s*1|z1|shyamali|doranda)\b", low) or (bare and re.fullmatch(r"\s*1\s*", low)): return "z1"
    if re.search(r"\b(zone\s*-?\s*2|z2|lalpur|kadru)\b", low) or (bare and re.fullmatch(r"\s*2\s*", low)): return "z2"
    if re.search(r"\b(zone\s*-?\s*3|z3|bariatu|morabadi)\b", low) or (bare and re.fullmatch(r"\s*3\s*", low)): return "z3"
    return None


def _fee_parse_freq(low):
    if re.search(r"\b(annual\w*|yearly|full year|one.?time|lump ?sum|whole year|full payment)\b", low): return "annual"
    if re.search(r"\b(quarter\w*|monthly|3 months|three months|per month|instal?lments?)\b", low): return "quarterly"
    return None


def _fee_trigger(low):
    if not re.search(r"\b(fee|fees|tuition|fee structure)\b", low): return False
    if re.search(r"\b(my|our|mine)\b|receipt|due date|last date|late fee|\bpayments?\b|\bpaid\b|pay (?:online|now|the fee)|how (?:do|can|to|should)\b.{0,20}\bpay\b|status|balance|outstanding|sibling|concession|scholarship|discount|refund|jvm/|reg-", low):
        return False
    est = re.search(r"\b(estimate|estimator|estimation|calculate|calculator|calculation|calc|how much|quote|total|approx\w*|compute|break ?down)\b", low)
    transport = re.search(r"\b(bus|transport|van|route)\b", low)
    if transport and not est and not re.search(r"\btuition\b", low): return False
    return bool(est or _fee_parse_class(low))


def fee_compute(tier_key, cat, bus, freq):
    t = FEE_TIERS[tier_key]
    adm = 0 if cat == "existing" else (t["adm"] or 0)
    tuition = t["qtr"] * (4 if freq == "annual" else 1)
    bus_q = FEE_BUS[bus][1] if bus in FEE_BUS else 0
    bus_total = bus_q * (4 if freq == "annual" else 1)
    dev = FEE_DEV_ANNUAL if freq == "annual" else FEE_DEV_QTR
    discount = (tuition * 5 + 50) // 100 if freq == "annual" else 0       # 5% of tuition, rounded
    return {"adm": adm, "tuition": tuition, "bus": bus_total, "dev": dev, "discount": discount, "total": adm + tuition + bus_total + dev - discount}


def _fee_tier_key(cls, stream):
    if cls == "nursery": return "nursery"
    n = int(cls)
    if n <= 3: return "c1-3"
    if n <= 5: return "c4-5"
    if n <= 8: return "c6-8"
    if n <= 10: return "c9-10"
    if n == 11: return "c11-sci" if stream == "science" else None
    return "c12-sci" if stream == "science" else "c12-com"


def _fee_state_clean(raw):
    if not isinstance(raw, dict): return {}
    out = {}
    if raw.get("cls") in ["nursery"] + [str(i) for i in range(1, 13)]: out["cls"] = raw["cls"]
    if raw.get("stream") in ("science", "commerce", "arts"): out["stream"] = raw["stream"]
    if raw.get("cat") in ("existing", "new"): out["cat"] = raw["cat"]
    if raw.get("bus") in ("none", "z1", "z2", "z3"): out["bus"] = raw["bus"]
    if raw.get("freq") in ("quarterly", "annual"): out["freq"] = raw["freq"]
    if raw.get("asking") in ("cls", "stream", "cat", "bus", "freq"): out["asking"] = raw["asking"]
    try: out["tries"] = max(0, min(5, int(raw.get("tries", 0))))
    except (TypeError, ValueError): out["tries"] = 0
    return out


def _fee_class_label(cls):
    return "Nursery" if cls == "nursery" else "Class " + cls


def _fee_question(slot, st, prefix=""):
    cls_name = _fee_class_label(st.get("cls", "")) if st.get("cls") else "this class"
    if slot == "cls":
        return {"reply": prefix + "I can estimate the fees for any class from Nursery to Class XII. Which class do you want the estimate for?",
                "options": ["Nursery / Prep", "Class 1\u20133", "Class 4\u20135", "Class 6\u20138", "Class 9\u201310", "Class 11", "Class 12"]}
    if slot == "stream":
        return {"reply": prefix + f"{cls_name} fees depend on the stream. Which specialty / stream will the student take?",
                "options": ["Science", "Commerce", "Arts / Humanities"]}
    if slot == "cat":
        return {"reply": prefix + "Is this for an existing JVM student (re-enrolling) or a new admission? New admissions also pay a one-time admission fee.",
                "options": ["Existing student", "New admission", "Skip \u2013 use defaults"]}
    if slot == "bus":
        zones = "; ".join(f"{v[0]} {_inr(v[1])}/qtr" for v in FEE_BUS.values())
        return {"reply": prefix + "Do you need school bus transport? Route estimates: " + zones + ".",
                "options": ["No bus", "Zone 1", "Zone 2", "Zone 3", "Skip \u2013 use defaults"]}
    return {"reply": prefix + "How will the fees be paid: quarterly (every 3 months) or annually in full (5% rebate on tuition)?",
            "options": ["Quarterly", "Annual (5% rebate)", "Skip \u2013 use defaults"]}


def _fee_result(st):
    cls, stream, cat, bus, freq = st["cls"], st.get("stream"), st["cat"], st["bus"], st["freq"]
    key = _fee_tier_key(cls, stream)
    t = FEE_TIERS[key]
    r = fee_compute(key, cat, bus, freq)
    alt = fee_compute(key, cat, bus, "annual" if freq == "quarterly" else "quarterly")
    when = "per quarter" if freq == "quarterly" else "for the full year"
    head = f"Fee estimate \u2014 {t['label']} \u00b7 {'Existing JVM student' if cat == 'existing' else 'New admission'} \u00b7 " \
           f"{FEE_BUS[bus][0] if bus in FEE_BUS else 'No bus'} \u00b7 {'Quarterly' if freq == 'quarterly' else 'Annual (paid in full)'}"
    lines = [head,
             f"\u2022 Tuition ({when}): {_inr(r['tuition'])}   (monthly rate {_inr(t['qtr'] // 3)})",
             f"\u2022 Bus transport: {_inr(r['bus'])}",
             f"\u2022 Dev & Lab Fund: {_inr(r['dev'])}",
             f"\u2022 Admission fee: {_inr(r['adm'])}" + (" (existing student)" if cat == "existing" else "")]
    if r["discount"]:
        lines.append(f"\u2022 Annual 5% rebate on tuition: - {_inr(r['discount'])}")
    lines.append(f"Estimated total: {_inr(r['total'])}")
    if freq == "quarterly":
        lines.append(f"If paid annually in full the same items come to {_inr(alt['total'])} (4 quarters + 5% tuition rebate).")
    else:
        lines.append(f"Paying quarterly instead, the first instalment would be {_inr(alt['total'])}.")
    notes = []
    if cat == "new" and t["adm"] is None:
        notes.append("The fee table does not list a new-admission fee for Class XII, so none is included.")
    if cat == "new" and cls == "3":
        notes.append("The official website lists no vacancy in Class 3 for 2026\u201327; please confirm admission availability first.")
    if cls in ("11",) and stream == "science":
        notes.append("The fee table lists Class XI as Science (CS/AI); confirm the exact subject combination with the office.")
    if cls == "nursery":
        notes.append("The fee table lists one 'Nursery & Prep' row; confirm LKG/UKG fees with the office.")
    notes.append("This is an estimate using the fee table and estimator on the Admissions page (the latest official table found is for 2025\u201326, not a confirmed 2026\u201327 quotation). Concessions are decided by the school office. Confirm the final amount at Reception: (0651) 2411221.")
    return {"reply": "\n".join(lines) + "\n" + " ".join(notes), "links": FEE_LINKS,
            "options": ["Estimate fees for another class", "How do I pay fees?"]}


def chat_fee_estimate(low, raw_state=None):
    """Guided public fee estimate. Stateless: the client echoes back 'fee_state'. Returns a reply dict (with 'fee_state'
    while the conversation is still collecting answers) or None when the message is not about a fee estimate."""
    st = _fee_state_clean(raw_state)
    active = bool(st.get("asking"))
    if not active and not _fee_trigger(low):
        return None
    if active and re.match(r"\s*(cancel|stop|exit|quit|never ?mind|forget it|no thanks?)\b", low):
        return {"reply": "Okay, I've stopped the fee estimate. You can start again anytime with \"estimate fees for Class 5\".", "links": FEE_LINKS}
    if active and _fee_trigger(low):          # an explicit new fee request mid-way restarts the estimate
        st, active = {}, False
    asking = st.get("asking")
    before = {k: st.get(k) for k in ("cls", "stream", "cat", "bus", "freq")}
    c = _fee_parse_class(low, bare=(asking == "cls"))
    if c == "bad":
        return {"reply": "JVM Shyamali teaches Nursery to Class XII. Please tell me a class in that range, for example \"Class 5\" or \"Class 11\".",
                "links": FEE_LINKS, "options": ["Nursery / Prep", "Class 1\u20133", "Class 6\u20138", "Class 11", "Class 12"],
                "fee_state": {**{k: v for k, v in st.items() if k in ("stream", "cat", "bus", "freq")}, "asking": "cls", "tries": st.get("tries", 0)}}
    if c:
        if c != st.get("cls"): st.pop("stream", None)
        st["cls"] = c
    v = _fee_parse_stream(low)
    if v and (st.get("cls") in ("11", "12") or not st.get("cls")): st["stream"] = v
    v = _fee_parse_cat(low)
    if v: st["cat"] = v
    v = _fee_parse_bus(low, bare=(asking == "bus"))
    if v: st["bus"] = v
    v = _fee_parse_freq(low)
    if v: st["freq"] = v
    if st.get("cls") not in ("11", "12"): st.pop("stream", None)
    if re.search(r"\b(skip|default|defaults|go ahead|just show|show (?:me )?(?:the )?(?:estimate|total)|that'?s all|calculate now)\b", low) and st.get("cls"):
        st.setdefault("cat", "existing"); st.setdefault("bus", "none"); st.setdefault("freq", "quarterly")
    changed = any(st.get(k) != before[k] for k in before)
    if active and not changed:
        words = len(re.findall(r"[a-z0-9]+", low))
        if words >= 4 or "?" in low:
            return None            # looks like a different question: let the normal chatbot answer it
        tries = st.get("tries", 0) + 1
        if tries >= 3:
            return {"reply": "I'm having trouble following the answers, so I've stopped the fee estimate. Type \"estimate fees for Class 5\" (for example) to start again, or contact the school office: Reception (0651) 2411221.", "links": FEE_LINKS}
        st["tries"] = tries
        q = _fee_question(asking, st, "Sorry, I didn't catch that. ")
        q["fee_state"] = {**st, "asking": asking}; q["links"] = []
        return q
    st["tries"] = 0
    for slot in ("cls", "stream", "cat", "bus", "freq"):
        if slot == "stream" and st.get("cls") not in ("11", "12"): continue
        if not st.get(slot):
            q = _fee_question(slot, st)
            q["fee_state"] = {**st, "asking": slot}; q["links"] = []
            return q
        if slot == "stream" and st.get("cls") == "11" and st.get("stream") != "science":
            name = "Commerce" if st["stream"] == "commerce" else "Arts / Humanities"
            return {"reply": f"The fee table on the Admissions page lists Class XI only for Science (CS/AI) \u2014 a separate Class XI {name} tuition figure is not published there, so I can't estimate it without guessing. Please confirm it with the school office: Reception (0651) 2411221. Would you like the Science estimate instead, or another class?",
                    "links": FEE_LINKS, "options": ["Science", "Estimate fees for another class"],
                    "fee_state": {**st, "asking": "stream"}}
    return _fee_result(st)


CONTACT = "Reception: (0651) 2411221 \u00b7 WhatsApp: +91 79790 72845 \u00b7 Email: jvmshyamali@yahoo.com \u00b7 Mon\u2013Sat, 08:00 AM\u201302:00 PM \u00b7 Jawahar Vidya Mandir, Shyamali Colony, Doranda, Ranchi, Jharkhand \u2013 834002."
L_PORTAL = [{"label": "Portals (sign in)", "url": "/pages/portal.html"}]
KB = [
    (r"report ?card|marksheet|mark sheet|download.*(result|marks)|exam result|individual marks|subject marks",
     "To view a report card or subject-wise marks, open the Portal and sign in with the student's Student ID or Registration Number and school-issued password. Open the student dashboard/result section. For security, this chatbot will not disclose individual marks before sign-in. If the report card is missing or looks incorrect, contact the class teacher or school office.", L_PORTAL),
    (r"attendance|present days|absent days|attendance percentage|leave record",
     "Attendance is private student information. Sign in to the Portal with the student's Student ID or Registration Number to view the attendance shown for that account. If a day is recorded incorrectly, contact the class teacher; only authorized school staff can correct the record.", L_PORTAL),
    (r"registration number|student id|student identity|where.*(student id|registration)|forgot.*(registration|student id)",
     "Use the Student ID or Registration Number issued by the school on the Portal sign-in screen. If you have misplaced it, contact the school office to verify the student's identity. Do not share the student's password in chat.", L_PORTAL),
    (r"bus route|school bus|transport|bus service|transport fee|bus fee",
     "For school transport routes, availability and current charges, check the Admissions page and confirm route-specific details with the school office. Route availability can depend on the pickup location, so please do not rely on a general estimate as confirmation of a seat.", [{"label": "Admissions & transport", "url": "/pages/admissions.html#fees"}, {"label": "Contact the school", "url": "/pages/contact.html"}]),
    (r"timetable|time table|period schedule|class schedule|exam schedule|exam timetable",
     "Use the Academics page for the published timetable and academic calendar. Class-specific or recently changed exam schedules should be confirmed with the class teacher or the latest school notice.", [{"label": "Academics & timetable", "url": "/pages/academics.html#timetable"}, {"label": "News & notices", "url": "/pages/news.html#news"}]),
    (r"library|laboratory|science lab|computer lab|sports facilities|playground|campus facilities",
     "JVM Shyamali's campus information, learning spaces and student facilities are described on the Campus page. For availability or access rules for a particular facility, please confirm with the school office.", [{"label": "Campus & facilities", "url": "/pages/campus.html"}]),
    (r"about (the )?(school|jvm)|school overview|school profile|what kind of school|school type|classes offered|which classes|affiliat(?:ed|ion)|jvm shyamali|jvm shyamli|jawahar vidya mandir",
     "Jawahar Vidya Mandir (JVM), Shyamali is a co-educational English-medium school in Shyamali Colony, Doranda, Ranchi, and follows the CBSE curriculum. For the latest official notices, class-wise offerings and current admission status, use the school website; questions not covered by a current official notice should be confirmed with the office. " + CONTACT,
     [{"label": "Official JVM Shyamali website", "url": "https://www.jvmshyamali.com/home"}, {"label": "Academics", "url": "/pages/academics.html"}, {"label": "Contact the school", "url": "/pages/contact.html"}]),
    (r"login|log in|sign.?in|password|otp|(forgot|reset|lost|cannot|can't|unable|wrong|invalid|locked).{0,25}(account|\bid\b|user ?name)",
     "Login help: Students/parents sign in on the Portal with the Student ID (e.g. JVM/2026/1001) or Registration No. (e.g. REG-2026-1001) and the password issued by the school. Faculty use their Teacher ID (e.g. TCH/101). Check CAPS LOCK and the selected role tab; after 5 wrong attempts an account pauses for 10 minutes. I cannot reveal or reset passwords \u2014 never share yours; ask the school office for a reset. " + CONTACT, L_PORTAL),
    (r"receipt|invoice|payment (history|status|proof)|transaction|paid or not|fee status|payment status",
     "Payment status and receipts are private. Sign in with the Student ID in the Portal \u2192 Fee Payment section to see total fees, amount paid, balance, due dates, payment history and downloadable receipts. A payment shows as paid only after the server confirms it. Not signed in? Type \"show my dashboard\" here.", L_PORTAL),
    (r"how.*(pay|payment)|pay (the )?(fee|online)|online payment|fee payment|payment (method|gateway|option|failed|deducted|stuck)|money (deducted|debited)",
     "To pay: sign in to the Portal with the Student ID \u2192 Fee Payment \u2192 Pay Now. Your balance, receipt and history update only after the payment is confirmed. If money was deducted but no receipt appears, do NOT pay again \u2014 wait a few minutes, refresh, then contact the office with your order/transaction reference. Fees are quarterly (due 15 Apr, 15 Jul, 15 Oct, 15 Jan). If the school is running in demo mode, no real money is charged and receipts are marked DEMO.", L_PORTAL),
    (r"due date|last date|late fee|when.*(fee|pay)|fee.*(due|deadline)",
     "Quarterly fee due dates for 2026\u201327: Q1 15 Apr 2026, Q2 15 Jul 2026, Q3 15 Oct 2026, Q4 15 Jan 2027. Your own balance is shown after signing in. For late-fee or concession questions, please ask the school office.", [{"label": "Fee structure", "url": "/pages/admissions.html#fees"}]),
    (r"timing|school hours|school time|what time|bell|start.*school|school.*start|reopen|assembly",
     "School hours: Senior Wing (Classes VI\u2013XII) 06:55 AM\u201301:10 PM; Primary Wing (Classes I\u2013V) 08:45 AM\u201302:45 PM; Pre-Primary Wing 12:30 PM\u201302:45 PM. Office hours Mon\u2013Sat 08:00 AM\u201302:00 PM.", [{"label": "Timetables", "url": "/pages/academics.html#timetable"}]),
    (r"homework|assignment|home work|classwork|syllabus|study material|worksheet",
     "Homework and assignments are given by each subject teacher through the class channel / diary. They are not stored in this website portal, so I cannot show today's homework. Please check the student diary or ask the class teacher (signed-in parents can ask \"who is the class teacher\").", [{"label": "Timetables", "url": "/pages/academics.html#timetable"}]),
    (r"stream|elective|commerce|science stream|humanities|subjects offered|curriculum|cbse",
     "Senior Secondary streams: Science (PCM/PCB with CS, AI, Biotech or PE), Commerce (with Applied Mathematics or IP) and Humanities (History, Political Science, Economics, Psychology, Sociology). The school follows the CBSE curriculum.", [{"label": "Academics", "url": "/pages/academics.html"}]),
    (r"technical|bug|error|not loading|page.*(blank|broken)|website.*(slow|down|issue|problem)|button.*(not|doesn)|support|help desk|helpdesk|complain",
     "Technical help: refresh the page (Ctrl+F5), make sure you opened the site through the school server address, and try another browser. If it persists, send the page name, what you clicked and the time through the Contact page, or call the office. Please never send passwords in a message. " + CONTACT, [{"label": "Contact the school", "url": "/pages/contact.html"}]),
    (r"contact|phone number|call|address|email|whatsapp|where is|location|reach|visit",
     CONTACT, [{"label": "Contact the school", "url": "/pages/contact.html"}]),
    (r"achievement|topper|award|medal|result.*(class|board|x\b|xii)|board result|toppers",
     "Achievements, board toppers and notices are listed on the News & Achievements page. Individual student marks are private and shown only after sign-in.", [{"label": "News, toppers & notices", "url": "/pages/news.html#news"}]),
    (r"event|function|annual day|sports day|celebration|competition|fest|club",
     None, [{"label": "Academic calendar", "url": "/pages/academics.html#calendar"}]),
    (r"teacher|faculty|staff|principal|head ?master|hod|vice principal",
     "Faculty profiles (qualification, subjects, experience) are on the Faculty page. For privacy I do not share staff personal phone numbers or emails in chat \u2014 contact teachers through the school office. " + CONTACT, [{"label": "Our faculty", "url": "/pages/campus.html#faculty"}]),
    (r"fee|tuition|scholarship|concession|sibling|discount|annual charges|cost",
     "Fees are charged quarterly per class. The Admissions page has the full class-wise structure and a fee calculator. Concessions/scholarships are decided by the school office, so I cannot promise any. Signed-in parents can see their own balance by typing \"my fees\".", [{"label": "Fee structure & calculator", "url": "/pages/admissions.html#fees"}]),
]
PRIVACY_RX = r"password|passcode|\botp\b|aadhaar|aadhar|salary|bank (account|detail)|home address|personal (phone|number|email)|database|all students|every student|list of students|student list|dump|admin (password|credential)"
GUARD_REPLY = "Sorry, I can't share passwords, personal contact details, or any student's or staff member's private records. Parents can see their own child's information after signing in; for anything else, please contact the school office. " + CONTACT
FALLBACK = ("Sorry, I did not understand that. I can help with: admissions (Class 3, Class 11, documents, transport); a fee estimate for any class "
            "(try \"estimate fees for Class 5\"); working days and holidays in a month (try \"how many working days are in this month?\"); "
            "report cards, marks and attendance after sign-in; transport; campus facilities; the academic calendar, exams and PTMs; timetables and school timings; faculty; contact details; and portal login. "
            "Please rephrase using one of these topics, or contact the school office: Reception (0651) 2411221, Mon\u2013Sat, 08:00 AM\u201302:00 PM.")
FALLBACK_OPTIONS = ["Estimate fees for a class", "Working days this month", "Next holiday", "Admission help"]

# Admissions policy supplied for the website update. Keep this separate from student records,
# fee data and login configuration so policy copy can be updated without touching those systems.
_ADMISSION_LINKS = {
    "home": {"label": "Admissions guidance", "url": "/pages/admissions.html#admissionInfo"},
    "criteria": {"label": "Class 11 eligibility and cutoffs", "url": "/pages/admissions.html#admission-criteria"},
    "documents": {"label": "Default document lists", "url": "/pages/admissions.html#admission-documents"},
    "tests": {"label": "Provisional-test guidance", "url": "/pages/admissions.html#admission-tests"},
    "transport": {"label": "Transport guidance", "url": "/pages/admissions.html#admission-transport"},
    "office": {"label": "School-office directions", "url": "/pages/admissions.html#admission-office"},
    "process": {"label": "Admission procedure", "url": "/pages/admissions.html#admission-process"},
    "fees": {"label": "Existing fee structure", "url": "/pages/admissions.html#fees"},
    "contact": {"label": "Contact the school", "url": "/pages/contact.html"},
    "official": {"label": "Official JVM Shyamali notices", "url": "https://www.jvmshyamali.com/home"},
    "class3-history": {"label": "Historical Class 3 notice (2024–25)", "url": "https://www.jvmshyamali.com/admin_panel/uploads/news/N_ec8ce6abb3e952a85b8551ba726a1227.pdf"},
    "class11-notice": {"label": "Official Class 11 notice (2026–27; past cycle)", "url": "https://www.jvmshyamali.com/add_form/Revised_CLASS%20XI%202026-27%20ADMISSION%20NOTICE2.pdf"},
}
_ADMISSION_OFFICE_DIRECTIONS = "Please enter through Gate No. 3 and proceed straight ahead to reach the school office."
_ADMISSION_DOCUMENT_NOTE = ("Document lists can change by session. Confirm the current checklist, vacancies and dates using the official school notices or by contacting the school office.")
_ADMISSION_STREAMS = {"science": "Science", "commerce": "Commerce", "arts": "Arts/Humanities", "humanities": "Arts/Humanities"}
# Verified from the school's revised 2026–27 Class XI notice. This is a past cycle;
# do not extrapolate these rules to a later session without a new school notice.
_ADMISSION_CUTOFFS = {
    # These are taken from the school's revised Class XI 2026–27 notice, a past cycle.
    # "admission_test" means the student qualified through the admission test; "direct"
    # means direct admission without that test. Never infer later-cycle cutoffs.
    "external": {
        "science": {
            "admission_test": {"pair": "Mathematics and Science", "pair_min": "164/200", "aggregate": "English, Mathematics, Science and any two other Class X subjects", "aggregate_min": "410/500"},
            "direct": {"pair": "Mathematics and Science", "pair_min": "180/200 (90%)", "aggregate": "English, Mathematics, Science and any two other Class X subjects", "aggregate_min": "450/500 (90%)"},
        },
        "commerce": {
            "admission_test": {"pair": "Mathematics and Social Science", "pair_min": "150/200", "aggregate": "English, Mathematics, Social Science and any two other Class X subjects", "aggregate_min": "375/500"},
            "direct": {"pair": "Mathematics and Social Science", "pair_min": "170/200 (85%)", "aggregate": "English, Mathematics, Social Science and any two other Class X subjects", "aggregate_min": "425/500 (85%)"},
        },
        "arts": {
            "admission_test": {"pair": "English and Social Science", "pair_min": "150/200", "aggregate": "English, Social Science and any three other Class X subjects", "aggregate_min": "375/500"},
            "direct": {"pair": "English and Social Science", "pair_min": "170/200 (85%)", "aggregate": "English, Social Science and any three other Class X subjects", "aggregate_min": "425/500 (85%)"},
        },
    }
}
# No recurring admission-test month has been verified for the current session.
_ADMISSION_TEST_MONTHS = {}


def _admission_links(*keys):
    return [_ADMISSION_LINKS[k] for k in keys if k in _ADMISSION_LINKS]


def _admission_class(low: str):
    if re.search(r"\b(nursery|nurseries)\b", low):
        return "nursery"
    if re.search(r"\b(lkg|lower kindergarten)\b", low):
        return "lkg"
    if re.search(r"\b(ukg|upper kindergarten)\b", low):
        return "ukg"
    if re.search(r"\b(class|grade|std|standard)\s*(?:11|xi|eleven)\b|\b(?:11th|xi)\s+class\b|\bclass\s*11th\b", low):
        return "class11"
    if re.search(r"\b(class|grade|std|standard)\s*(?:3|iii|third)\b|\b(?:3rd|third)\s+class\b|\bclass\s*3rd\b", low):
        return "class3"
    # A generic standalone class number is accepted only when admission is clearly the topic.
    if re.search(r"\bclass\s*11\b", low):
        return "class11"
    if re.search(r"\bclass\s*3\b", low):
        return "class3"
    return None


def _unsupported_admission_class(low: str):
    """Return a class label mentioned in an admission query that is not supported here."""
    # A supported destination class (especially Class XI for a current Class X student)
    # takes priority over the applicant's current class.
    if _admission_class(low) is not None:
        return None
    match = re.search(
        r"\b(?:class|grade|std|standard)\s*(?:no\.?\s*)?(12|11|10|[1-9]|xii|xi|x|ix|viii|vii|vi|iv|v|iii|ii|i)(?:st|nd|rd|th)?\b",
        low, re.I,
    )
    if not match:
        return None
    token = match.group(1).lower()
    roman = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6,
             "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}
    number = int(token) if token.isdigit() else roman[token]
    if number in (3, 11):
        return None
    return f"Class {number}"


def _admission_is_document_policy_query(low: str) -> bool:
    """Allow policy questions about Aadhaar documents without weakening private-data guards."""
    return bool(re.search(r"\b(aadhaar|aadhar)\b", low)
                and re.search(r"\b(document|documents|checklist|required|requirement|need|submit|bring)\b", low)
                and not re.search(r"\b(show|reveal|fetch|find|search|lookup|give me|tell me my|number of|personal details)\b", low))


def chat_admissions(low: str):
    """Answer only from the supplied admissions policy; do not infer dates or missing criteria."""
    policy_signal = re.search(
        r"\b(admission|admissions|apply|application|enrol|enroll|registration|prospectus|new session|"
        r"provisional|eligibility|eligible|cut.?off|direct admission|internal applicant|external applicant|"
        r"documents?|paperwork|checklist|school bus|bus facility|bus fee|transport|pickup|pick-up|departure|"
        r"gate\s*(?:no\.?\s*)?3|office directions|admission test|optional subjects?|5th subject|fifth subject|6th subject|sixth subject|subject choices?|subject options?|subject combination|subjects available)\b", low)
    grade = _admission_class(low)
    unsupported_class = _unsupported_admission_class(low)
    bare_unsupported_class = bool(unsupported_class and re.fullmatch(
        r"\s*(?:class|grade|std|standard)\s*(?:no\.?\s*)?(12|11|10|[1-9]|xii|xi|x|ix|viii|vii|vi|iv|v|iii|ii|i)(?:st|nd|rd|th)?\s*", low, re.I))
    unsupported_class_status_query = bool(unsupported_class and re.search(r"\b(open|opening|available|vacanc(?:y|ies))\b", low))
    if (policy_signal or bare_unsupported_class or unsupported_class_status_query) and unsupported_class and grade is None:
        return {"reply": (f"Admissions are not available for {unsupported_class} in the admission information provided to this chatbot. "
                          "The classes covered here are Nursery, LKG, UKG, Class 3 and Class 11. "
                          "Class 3 currently has no vacancy listed for 2026–27; availability for the other listed classes depends on the current official notice."),
                "links": []}
    test_with_class = bool(grade and re.search(r"\b(test|month|date|syllabus|venue|provisional)\b", low))
    class11_criteria = bool(grade == "class11" and re.search(
        r"\b(cut.?off|eligib|criteria|minimum|percentage|percent|marks required|direct admission|without (a )?test)\b", low))
    class11_status_or_stream = bool(grade == "class11" and re.search(
        r"\b(internal|external|science|commerce|humanities|arts)\b", low))
    class3_status_query = bool(grade == "class3" and re.search(
        r"\b(open|available|vacanc(?:y|ies)|apply|admission|admissions|enrol|enroll|registration|accepting|taking)\b", low))
    if not (policy_signal or test_with_class or class11_criteria or class11_status_or_stream or class3_status_query):
        return None

    # The placeholder is informational only. Never turn it into a telephone link or claim it works.
    if re.search(r"\b(phone|telephone|call|contact number|admission number)\b", low):
        return {"reply": "No separate admissions-specific phone number is verified here. Please use the school's published Contact page for its official reception number and contact details, or visit the school office. " + _ADMISSION_OFFICE_DIRECTIONS,
                "links": _admission_links("contact", "office")}

    # Explicit location/submission questions should receive the required Gate No. 3 direction
    # even when the visitor mentions documents in the same sentence.
    if re.search(r"\b(gate|directions?|where.{0,40}(office|submit|hand in|drop off|give)|how.{0,30}(reach|get to).{0,20}(office|school)|reach the office|visit the office|office location|admission office|document submission|submit.{0,30}documents?|office enquir(?:y|ies))\b", low):
        return {"reply": _ADMISSION_OFFICE_DIRECTIONS, "links": _admission_links("office", "contact")}

    # Current public vacancy status takes priority unless the user specifically asks for documents.
    if grade == "class3" and re.search(r"\b(vacanc(?:y|ies)|available|open|opening|accepting|taking|apply|application|admission|enrol|enroll|registration)\b", low) and not re.search(r"\b(document|documents|paperwork|checklist|certificate|marksheet|mark sheet|transfer certificate|bonafide)\b", low):
        return {"reply": "The official JVM Shyamali website currently lists ‘No Vacancy in Class-3 (2026–27)’. I cannot confirm the next session's Class 3 opening, test requirement, month or dates without a new official notice. Please check the latest official notice or contact the school office before preparing an application.",
                "links": _admission_links("official", "office")}

    # Class XI optional-subject questions get a focused answer instead of the full admissions summary.
    if grade == "class11" and re.search(
        r"\b(optional subjects?|5th subject|fifth subject|6th subject|sixth subject|subject choices?|subject options?|subject combination|subjects available)\b", low
    ):
        if re.search(r"\bpcm\b", low):
            stream = "pcm"
        elif re.search(r"\bpcb\b", low):
            stream = "pcb"
        else:
            stream = next((k for k in ("science", "commerce", "humanities", "arts") if re.search(r"\b" + k + r"\b", low)), None)
        if stream in ("humanities", "arts"):
            stream = "arts"
        choices = {
            "pcm": ("Science — PCM\n• 5th subject (choose one): Computer Science, Informatics Practices, Physical Education or Economics\n"
                    "• 6th subject (choose one): Painting, Artificial Intelligence (AI), PHE or Physical Activity Trainer"),
            "pcb": ("Science — PCB\n• 5th subject (choose one): Mathematics, Physical Education or Biotechnology\n"
                    "• 6th subject (choose one): Painting, Artificial Intelligence (AI), PHE or Physical Activity Trainer"),
            "commerce": ("Commerce\n• 5th subject (choose one): Mathematics, Applied Mathematics or Entrepreneurship\n"
                        "• 6th subject (choose one): Painting, Artificial Intelligence (AI), Applied Maths, Informatics Practices, PHE or Physical Activity Trainer"),
            "arts": ("Humanities / Arts\n• 5th subject (choose one): Mathematics, Applied Mathematics or Sociology\n"
                    "• 6th subject (choose one): Informatics Practices, Physical Education, Painting, Artificial Intelligence (AI) or Physical Activity Trainer"),
        }
        if stream == "pcm":
            answer = choices["pcm"]
        elif stream == "pcb":
            answer = choices["pcb"]
        elif stream == "science":
            answer = choices["pcm"] + "\n\n" + choices["pcb"]
        elif stream == "commerce":
            answer = choices["commerce"]
        elif stream == "arts":
            answer = choices["arts"]
        else:
            answer = "Class XI optional subjects — official 2026–27 notice\n\n" + "\n\n".join(choices[k] for k in ("pcm", "pcb", "commerce", "arts"))
        answer += "\n\nThese options are from the official Class XI 2026–27 notice. That admission cycle has passed; check the latest notice for the current session."
        return {"reply": answer, "links": []}

    # Document questions take priority over the broad class-11 cutoff intent.
    if re.search(r"\b(document|documents|paperwork|checklist|certificate|marksheet|mark sheet|transfer certificate|bonafide)\b", low) or _admission_is_document_policy_query(low):
        if not grade:
            return {"reply": "Document requirements differ by class. Which class is the applicant seeking: LKG, Class 3, Class 11, or another class? The latest checklist should be confirmed with the school office.",
                    "links": _admission_links("documents", "office")}
        doc_lists = {
            "lkg": "LKG document requirements vary by admission notice; confirm the current checklist in the official school notice.",
            "class3": ("Class 3: the official school website currently lists NO VACANCY for 2026–27. The next session's Class 3 checklist is not verified. "
                       "For reference only, the historical 2024–25 notice asked applicants for the Class II first-term/annual report card and a self-attested birth certificate. For selected candidates, that notice listed the original Class II transfer certificate, original and self-attested birth certificate, child's and parents' Aadhaar copies, caste certificate where applicable, and original final Class II report card for verification. This old list must not be treated as the current session's requirement."),
            "class11": "Class 11 document requirements must be checked against the current session's official admission notice; an old default list is not confirmed for the next cycle.",
        }
        answer = doc_lists.get(grade)
        if not answer:
            answer = f"A class-specific default document list for {grade.upper() if grade in ('nursery', 'ukg') else grade} is not available in the current guidance. Please confirm the checklist with the school office."
            return {"reply": answer + " " + _ADMISSION_DOCUMENT_NOTE + " " + _ADMISSION_OFFICE_DIRECTIONS, "links": _admission_links("documents", "office")}
        extra_links = ("documents", "office", "official", "class3-history") if grade == "class3" else (("documents", "office", "class11-notice") if grade == "class11" else ("documents", "office", "official"))
        return {"reply": answer + " " + _ADMISSION_DOCUMENT_NOTE + " " + _ADMISSION_OFFICE_DIRECTIONS,
                "links": _admission_links(*extra_links)}

    if grade == "class3" and re.search(r"\b(test|provisional|month|date|deadline|eligib)\b", low):
        return {"reply": "The official JVM Shyamali website currently lists ‘No Vacancy in Class-3 (2026–27)’. No current official Class 3 test requirement, syllabus or future date is verified here. Please check the latest official notice or contact the school office.",
                "links": _admission_links("official", "tests", "office")}

    # Directions apply consistently to admission visits, document submission and office enquiries.
    if re.search(r"\b(gate|directions?|where.*office|office.*where|reach|visit|location|school office)\b", low):
        return {"reply": _ADMISSION_OFFICE_DIRECTIONS, "links": _admission_links("office", "contact")}

    # Transport policy is class-specific. Do not merge bus cost estimates with tuition fees.
    if re.search(r"\b(bus|transport|pickup|pick-up|departure|van|route|bus fee|transport fee)\b", low):
        fee_note = "Verified bus-fee information is unavailable in this guidance; please confirm the current bus fee with the school office."
        if grade == "class11":
            text = ("Class 11 approximate batches: morning 7:00 AM–10:35 AM; second batch 10:35 AM–2:15 PM. "
                    "Bus availability depends on the allotted batch, route and schedule. Do not assume both pickup and departure journeys are available; confirm the needed journey before finalising the batch. " + fee_note)
        elif grade == "class3":
            text = ("The official school website lists no Class 3 vacancy for 2026–27, and a current Class 3 transport arrangement is not verified here. Please confirm future admission status, route availability and service details with the school office. " + fee_note)
        elif grade == "lkg":
            text = ("Current LKG transport arrangements are not verified here. Please confirm route availability and any bus/private-van arrangements with the school office. " + fee_note)
        elif grade:
            text = (f"A school-bus facility is not confirmed in the current guidance for {grade}. Please confirm route and service availability with the school office. " + fee_note)
        else:
            text = "Bus arrangements depend on class, route and schedule. Which class is the applicant seeking? Verified bus-fee information is unavailable in this guidance; please confirm route, service and fee with the school office."
        transport_links = ("official", "transport", "office") if grade == "class3" else ("official", "transport", "office") if grade == "lkg" else ("transport", "office")
        return {"reply": text, "links": _admission_links(*transport_links)}

    # Answer cutoff questions only when the applicant type and stream are clear.
    wants_cutoff = bool(re.search(r"\b(cut.?off|eligib|criteria|minimum|percentage|percent|marks required|direct admission|without (a )?test)\b", low))
    if grade == "class11" and (wants_cutoff or class11_status_or_stream):
        applicant_type = "internal" if re.search(r"\b(internal|existing student|current student|already studies here)\b", low) else (
            "external" if re.search(r"\b(external|outside student|other school|different school)\b", low) else None)
        stream = next((k for k in ("science", "commerce", "humanities", "arts") if re.search(r"\b" + k + r"\b", low)), None)
        if stream in ("humanities", "arts"):
            stream = "arts"
        if not applicant_type:
            return {"reply": "For Class 11 eligibility, is the applicant an internal JVM student or an external applicant? I can then provide the correct stream-wise cutoff.",
                    "links": _admission_links("criteria")}
        if not stream:
            return {"reply": "Which Class 11 stream are you asking about: Science, Commerce or Arts/Humanities? The cutoff differs by stream.",
                    "links": _admission_links("criteria")}
        stream_name = _ADMISSION_STREAMS[stream]
        if applicant_type == "internal":
            reply = ("For internal JVM students, the school's 2026–27 Class XI notice says the minimum eligibility criteria for JVM students would be notified separately. "
                     "No internal stream-wise percentage can be reliably quoted from that notice, and that admission cycle has passed. Please check the newest official notice for current criteria.")
            links = _admission_links("class11-notice", "official", "office")
        else:
            rule = _ADMISSION_CUTOFFS["external"][stream]
            tested = rule["admission_test"]
            direct = rule["direct"]
            reply = (f"Class 11 — external applicant, {stream_name} (official 2026–27 notice; this cycle has passed). "
                     f"For candidates who qualified through the admission test, the notice required either {tested['pair_min']} in {tested['pair']} or "
                     f"{tested['aggregate_min']} in {tested['aggregate']}. For direct admission without the test, it required either "
                     f"{direct['pair_min']} in {direct['pair']} or {direct['aggregate_min']} in {direct['aggregate']}. "
                     "These are historical 2026–27 requirements only; confirm current-session criteria and dates in a new official notice. "
                     "Meeting the minimum does not guarantee admission.")
            links = _admission_links("class11-notice", "official", "office")
        return {"reply": reply, "links": links}

    # Do not let admission test questions get answered by the general exams calendar.
    if re.search(r"\b(test|provisional|syllabus|month|date|deadline|venue)\b", low) and (grade or policy_signal):
        if not grade:
            return {"reply": "The provisional-test requirement and default month depend on the applicant's class. Which class is the applicant seeking: Nursery, LKG, UKG, Class 3, Class 11, or another class? Exact dates, deadlines, syllabus and venue are not verified; please confirm them with the school office.",
                    "links": _admission_links("tests", "home", "office")}
        if grade == "nursery":
            text = "Nursery: no current official provisional-test month, exact date, deadline, syllabus or venue has been verified here. The school's current notice should determine whether any test is required; please check the official notice or contact the school office."
        elif grade == "ukg":
            text = "UKG: no current official provisional-test month, exact date, deadline, syllabus or venue has been verified here. Do not assume the test rule or dates for another class apply to UKG; please check the latest official notice or contact the school office."
        elif grade == "class3":
            text = "The official JVM Shyamali website lists no vacancy in Class 3 for 2026–27. No current official Class 3 test requirement, month, syllabus or future date is verified here; please check the latest official notice or contact the school office."
        elif grade == "class11":
            text = "The official Class 11 notice for 2026–27 listed admission-test dates in March and April 2026; those dates have passed. The next session's test requirement, dates, syllabus and venue are not verified. The old notice described both admission-test and direct-admission criteria for external applicants; check a new official notice before relying on them."
        elif grade == "lkg":
            text = "For LKG, a current official test requirement, eligibility category, month, date, syllabus and venue are not verified here. Do not assume a test is mandatory without the applicable current notice; please confirm with the school office."
        else:
            text = "A class-specific provisional-test requirement or test month is not verified in the available guidance. Please contact the school office; exact dates, deadlines, syllabus and venue must not be assumed."
        return {"reply": text, "links": _admission_links("tests", "criteria", "office")}

    if not grade:
        return {"reply": "I can help with admissions, eligibility, provisional tests, documents, transport and office directions. Which class is the applicant seeking: Nursery, LKG, UKG, Class 3, Class 11, or another class? Exact dates, deadlines and vacancies are not verified here; please confirm them with the school office.",
                "links": _admission_links("home", "criteria", "documents", "transport", "office")}

    if grade == "class11":
        return {"reply": (
                    "Class XI admissions — 2026–27 (external candidates)\n\n"
                    "Status: the supplied notice is for a past cycle. Registration and test dates have passed; it is not a current open-admission notice.\n\n"
                    "Eligibility: students appearing in Class X in 2026 from any Board could register. Provisional admission through the test depended on test performance; final admission depended on Class X Board results and the relevant stream cutoff.\n\n"
                    "Streams:\n"
                    "• Science PCM: English Core, Physics, Chemistry and Mathematics.\n"
                    "• Science PCB: English Core, Physics, Chemistry and Biology.\n"
                    "• Commerce: English Core, Accountancy, Business Studies and Economics.\n"
                    "• Humanities / Arts: English Core, History, Political Science and Economics.\n\n"
                    "Registration fee: ₹2,000, online and non-refundable (as stated in the 2026–27 notice).\n\n"
                    "Dates listed: registration began 25 January 2026; registration closed 28 February 2026; application-fee deadline 2 March 2026; CBSE test 16 March 2026; ICSE / State Boards and defaulters test 2 April 2026. All deadlines and test dates have passed.\n\n"
                    "Stream-wise optional subjects and cutoffs can be answered separately. The notice does not include a complete document checklist. Check a new official notice for the next session."),
                "links": _admission_links("class11-notice", "fees")}
    if grade == "class3":
        return {"reply": "The official JVM Shyamali website currently lists no vacancy in Class 3 for 2026–27. No current official requirement for a Class 3 admission test, month, checklist or future date is verified here. Please check the latest official notice or contact the school office before preparing an application. " + _ADMISSION_OFFICE_DIRECTIONS,
                "links": _admission_links("official", "documents", "tests", "office")}
    if grade == "lkg":
        return {"reply": "LKG eligibility, current documents, any test requirement and transport arrangements must be confirmed from the current official school notice. The available information here does not verify a complete current checklist or a recurring test month. " + _ADMISSION_OFFICE_DIRECTIONS,
                "links": _admission_links("official", "documents", "tests", "transport", "office")}
    if grade in ("nursery", "ukg"):
        reply = f"{grade.upper() if grade == 'ukg' else 'Nursery'}: class-specific eligibility, any test requirement, test month and a complete document checklist are not verified in the current guidance. Exact admission dates and deadlines must not be assumed. Please confirm the current procedure and checklist with the school office. " + _ADMISSION_OFFICE_DIRECTIONS
    else:
        reply = f"Class-specific admission eligibility and document requirements for {grade} are not verified in the available guidance. Please contact the school office for current procedure, eligibility, any required test, document checklist, availability and dates. " + _ADMISSION_OFFICE_DIRECTIONS
    return {"reply": reply, "links": _admission_links("process", "tests", "documents", "office")}


def chat_kb(low: str, role: str):
    # Keep privacy protection in force; only an explicit document-requirement question may
    # mention Aadhaar as an admission checklist item. This does not expose any personal ID.
    safe_aadhaar_policy = _admission_is_document_policy_query(low)
    if re.search(PRIVACY_RX, low) and not re.search(r"forgot|reset|lost|how (do|can)|change", low) and not safe_aadhaar_policy:
        return {"reply": GUARD_REPLY, "links": []}
    # A plain request for the student's identifier is portal help, not admissions
    # registration policy. Keep class/session/opening questions on the admissions path.
    identifier_help = bool(re.search(r"\b(registration number|student id)\b", low)
                           and not re.search(r"\b(admission|nursery|lkg|ukg|class|grade|open|opening|date|when)\b", low))
    admission = None if identifier_help else chat_admissions(low)
    if admission:
        return admission
    if re.search(r"holiday|vacation|exam|\btest\b|assessment|half.?yearly|\bptm\b|parent.?teacher|calendar|term (start|end)|next (event|holiday)|upcoming", low) and not re.search(r"timetable|time table", low):
        r = chat_calendar(low)
        if r: return r
    for rx, text, links in KB:
        if re.search(rx, low):
            if text is None:
                evs = [e for e in cal_events() if e["date"] >= datetime.now(timezone.utc).strftime("%Y-%m-%d") and e["type"] != "holiday"][:3]
                text = ("Upcoming school events: " + "; ".join(_fmt_ev(e) for e in evs) + ".") if evs else "Events are listed in the academic calendar."
            return {"reply": text, "links": links}
    return None


# ---------------------------------------------------------------- chatbot student dashboard
CHAT_LOOKUPS: dict = {}
REG_FAILS: dict = {}   # login_id -> recent failed password timestamps
REG_FAIL_LIMIT, REG_FAIL_WINDOW = 5, 600


def norm_student_key(supplied: str) -> list[str]:
    """Candidate keys for a typed registration number / student ID (case, spaces, dashes, slashes tolerated)."""
    raw = re.sub(r"[\u200b-\u200f\u00a0\ufeff]", "", str(supplied)).strip().upper()
    if not raw or len(raw) > 40:
        return []
    out = [raw]
    m = re.search(r"(REG|JVM)\W*(\d{4})\W*(\d{1,8})", raw)
    if m:
        n = m.group(3).zfill(4)
        out += [f"REG-{m.group(2)}-{n}", f"JVM/{m.group(2)}/{n}"]
    else:
        m = re.fullmatch(r"(\d{4})\W+(\d{1,8})", raw) or re.fullmatch(r"()(\d{1,8})", raw)
        if m:
            n = m.group(2).zfill(4)
            out += [f"REG-{m.group(1) or '2026'}-{n}", f"JVM/{m.group(1) or '2026'}/{n}"]
    return out


def norm_teacher_key(supplied: str) -> str:
    """TCH/101 is the one ID format; tch101, TCH-101 and 'TCH 101' are accepted and mapped to it."""
    raw = str(supplied).strip().upper()
    m = re.fullmatch(r"TCH\W*(\d{1,4})", raw)
    return f"TCH/{m.group(1)}" if m else raw


def find_student_user(con, supplied: str):
    """Match a registration number (or student ID) to a student account."""
    keys = norm_student_key(supplied)
    if not keys:
        return None
    for cand in con.execute("SELECT * FROM users WHERE role='student'"):
        if cand["login_id"] in keys or any(k in keys for k in json.loads(cand["aliases"] or "[]")):
            return cand
    return None


def chat_dashboard(record: dict) -> dict:
    """Only what the chat dashboard shows: fees, results, attendance."""
    fees, att, acad = record.get("fees", {}), record.get("attendance", {}), record.get("academics", {})
    return {
        "name": record.get("name", ""), "regNo": record.get("regNo", ""),
        "cls": record.get("cls", ""), "sec": record.get("sec", ""), "roll": record.get("roll", ""),
        "fees": {k: fees.get(k) for k in ("status", "annualTotal", "dueAmt", "lastPaidDate", "quarterStatus")},
        "attendance": {k: att.get(k) for k in ("present", "total", "rate", "leavesTaken")},
        "result": {"term": acad.get("term"), "aggregate": acad.get("aggregate"), "grade": acad.get("grade"),
                   "rank": acad.get("rank"), "marks": acad.get("marks", [])},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "JVMPortal/1.0"

    def log_message(self, fmt, *args):
        # Keep request logging, but never log request bodies, credentials, or cookies.
        super().log_message(fmt, *args)

    def _headers(self, status=200, content_type="application/json; charset=utf-8", length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        cors = self._cors_origin()
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, value, status=200, extra=None):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, length=len(data), extra=extra)
        self.wfile.write(data)

    def _body(self, limit=1_500_000):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if size < 0 or size > limit:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(size) if size else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Request must contain valid JSON.")
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object.")
        return data

    def _ip(self) -> str:
        if TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            if fwd: return fwd[:64]
        return self.client_address[0]

    def _cors_origin(self):
        """Return the request's Origin if it is on the JVM_ALLOWED_ORIGINS allowlist, else None."""
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        return origin if origin and origin.lower() in ALLOWED_ORIGINS else None

    def _is_cross_site_allowed(self) -> bool:
        origin = self._cors_origin()
        if not origin:
            return False
        return urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower()

    def _session_cookie(self, value: str, max_age: int = 28800) -> str:
        if self._is_cross_site_allowed():
            # Cross-origin frontend: the browser only sends the cookie if it is SameSite=None; Secure (HTTPS).
            return f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=None; Secure; Max-Age={max_age}"
        cookie = f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https": cookie += "; Secure"
        return cookie

    def _host_origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # same-origin navigations and command-line tests
        if self._cors_origin():
            return True  # explicitly allow-listed frontend origin (JVM_ALLOWED_ORIGINS)
        parsed = urlparse(origin)
        host = self.headers.get("Host", "")
        return parsed.netloc.lower() == host.lower() and parsed.scheme in ("http", "https")

    def _cookie_session(self):
        token = None
        for part in self.headers.get("Cookie", "").split(";"):
            k, sep, v = part.strip().partition("=")
            if sep and k == SESSION_COOKIE:
                token = v
                break
        sess = SESSIONS.get(token) if token else None
        if sess and time.time() - sess.get("created_epoch", 0) > 8 * 60 * 60:
            SESSIONS.pop(token, None)
            return None, token
        return sess, token

    def _require_session(self):
        sess, token = self._cookie_session()
        if not sess:
            self._json({"error": "Please sign in again."}, 401)
            return None
        return sess

    def _log(self, con, sess, action, student_id=None, fields=None):
        con.execute("INSERT INTO audit_log(occurred_at,actor_role,actor_id,action,student_id,changed_fields) VALUES(?,?,?,?,?,?)",
                    (utc_now(), sess.get("role", "system"), sess.get("id", "system"), action,
                     student_id, json.dumps(fields or [])))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/me":
            sess, _ = self._cookie_session()
            if not sess:
                self._json({"error": "Not signed in."}, 401)
                return
            with connection() as con:
                records = records_for_session(con, sess)
            self._json({"user": public_session(sess), "records": records})
            return
        if path == "/api/chat/teacher-class":
            sess = self._require_session()
            if not sess: return
            if sess.get("role") not in {"teacher", "master"}:
                self._json({"error": "Only teachers and administrators can view an assigned-class roster."}, 403)
                return
            with connection() as con:
                allowed = allowed_ids_for_session(con, sess)
                roster = []
                for sid in sorted(allowed):
                    row = con.execute("SELECT payload FROM records WHERE student_id=?", (sid,)).fetchone()
                    if not row:
                        continue
                    rec = json.loads(row[0])
                    # The roster response intentionally excludes marks, attendance, fees,
                    # parent contacts, home address and all other student record fields.
                    roster.append({"id": sid, "name": str(rec.get("name", "")),
                                   "cls": str(rec.get("cls", "")), "sec": str(rec.get("sec", "")),
                                   "roll": str(rec.get("roll", ""))})
                roster.sort(key=lambda r: (str(r["cls"]), str(r["sec"]), int(r["roll"] or 0), r["name"].casefold()))
                self._log(con, sess, "chat-class-roster-view")
                con.commit()
            self._json({"count": len(roster), "students": roster,
                        "teacher_name": str(sess.get("name", "")),
                        "teacher_mode": str(sess.get("teacher_mode", "master" if sess.get("role") == "master" else "unassigned"))})
            return
        if path == "/api/students":
            sess = self._require_session()
            if not sess: return
            with connection() as con:
                records = records_for_session(con, sess)
            self._json({"records": records})
            return
        if path == "/api/enquiries":
            sess = self._require_session()
            if not sess: return
            if sess.get("role") != "master":
                self._json({"error": "Master access required."}, 403)
                return
            with connection() as con:
                rows = con.execute("SELECT id,created_at,name,email,message,status FROM enquiries ORDER BY id DESC LIMIT 300").fetchall()
            self._json({"enquiries": [dict(r) for r in rows]})
            return
        if path == "/api/audit":
            sess = self._require_session()
            if not sess: return
            if sess.get("role") != "master":
                self._json({"error": "Master access required."}, 403)
                return
            with connection() as con:
                rows = con.execute("SELECT id,occurred_at,actor_role,actor_id,action,student_id,changed_fields FROM audit_log ORDER BY id DESC LIMIT 500").fetchall()
            events = []
            for row in rows:
                event = dict(row)
                try:
                    event["changed_fields"] = json.loads(event.get("changed_fields") or "[]")
                except (TypeError, json.JSONDecodeError):
                    event["changed_fields"] = []
                events.append(event)
            self._json({"events": events})
            return
        if path == "/api/fees/summary":
            sess = self._require_session()
            if not sess: return
            sid = sess.get("student_id") if sess.get("role") == "student" else (parse_qs(parsed.query).get("student_id", [""])[0] if sess.get("role") == "master" else None)
            if not sid:
                self._json({"error": "Fee details are available to Student/Parent and Administrator logins only."}, 403); return
            with connection() as con:
                row = con.execute("SELECT payload FROM records WHERE student_id=?", (sid,)).fetchone()
                if not row: self._json({"error": "Student record not found."}, 404); return
                rec = json.loads(row[0]); o = open_order(con, sid)
                self._json(fee_summary(rec, student_payments(con, sid), o))
            return
        if path == "/api/fees/overview":
            sess = self._require_session()
            if not sess: return
            if sess.get("role") != "master": self._json({"error": "Master access required."}, 403); return
            with connection() as con:
                recs = get_record_map(con)
                tot = sum(int(r.get("fees", {}).get("annualTotal") or 0) for r in recs.values())
                due = sum(int(r.get("fees", {}).get("dueAmt") or 0) for r in recs.values())
                pays = con.execute("SELECT receipt_no,student_id,amount,method,paid_at,demo FROM fee_payments ORDER BY paid_at DESC LIMIT 50").fetchall()
            self._json({"students": len(recs), "totalFees": tot, "outstandingNow": due, "gatewayMode": PAY_MODE, "recentOnlinePayments": [dict(p) for p in pays]})
            return
        if path == "/api/fees/receipt":
            sess = self._require_session()
            if not sess: return
            no = parse_qs(parsed.query).get("no", [""])[0][:60]
            with connection() as con:
                sid = sess.get("student_id") if sess.get("role") == "student" else None
                if sess.get("role") == "master":
                    r = con.execute("SELECT student_id FROM fee_payments WHERE receipt_no=?", (no,)).fetchone()
                    sid = r[0] if r else parse_qs(parsed.query).get("student_id", [""])[0]
                if not sid: self._json({"error": "Receipts are available to the student/parent account that owns them."}, 403); return
                row = con.execute("SELECT payload FROM records WHERE student_id=?", (sid,)).fetchone()
                html = receipt_html(con, json.loads(row[0]), no) if row else None
            if not html: self._json({"error": "Receipt not found for this account."}, 404); return
            data = html.encode("utf-8"); self._headers(200, "text/html; charset=utf-8", length=len(data)); self.wfile.write(data); return
        if path == "/healthz":
            self._json({"ok": True}); return
        if path.startswith("/images/"):
            img = (ROOT / "images" / path[len("/images/"):]).resolve()
            if (ROOT / "images").resolve() in img.parents and img.is_file() and img.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"):
                data = img.read_bytes(); self._headers(200, mimetypes.guess_type(img.name)[0] or "application/octet-stream", length=len(data)); self.wfile.write(data); return
            self._json({"error": "Not found."}, 404); return
        # Explicit public-file allowlist. Private JSON, the source backup HTML, SQLite DB,
        # seed data and any unknown paths are never served as static files.
        allowed_files = {
            "/", "/index.html", "/pages/index.html", "/pages/academics.html",
            "/pages/campus.html", "/pages/news.html", "/pages/portal.html",
            "/pages/contact.html", "/pages/admissions.html", "/achievements.json",
            "/faculty.json", "/timetable.json", "/jvm-config.js", "/official-sources.json",
        }
        if path not in allowed_files:
            self._json({"error": "Not found."}, 404)
            return
        actual = ROOT / ("index.html" if path == "/" else path.lstrip("/"))
        if not actual.is_file() or PRIVATE in actual.parents:
            self._json({"error": "Not found."}, 404)
            return
        try:
            data = actual.read_bytes()
        except OSError:
            self._json({"error": "Unable to read page."}, 500)
            return
        content_type = mimetypes.guess_type(actual.name)[0] or "application/octet-stream"
        if content_type == "text/html": content_type += "; charset=utf-8"
        if content_type == "application/json": content_type += "; charset=utf-8"
        self._headers(200, content_type, length=len(data))
        self.wfile.write(data)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if not self._host_origin_ok():
            self._json({"error": "Cross-origin request rejected."}, 403)
            return
        try:
            body = self._body()
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        if path == "/api/login":
            ip = self._ip()
            now = time.time()
            LOGIN_ATTEMPTS[ip] = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < 60]
            if len(LOGIN_ATTEMPTS[ip]) >= 12:
                self._json({"error": "Too many login attempts. Please wait a minute."}, 429)
                return
            role = str(body.get("role", "")).strip().lower()
            if role == "admin": role = "master"
            supplied_id = str(body.get("id", "")).strip().upper()
            password = str(body.get("password", ""))
            if role == "teacher": supplied_id = norm_teacher_key(supplied_id)
            if role not in {"student", "teacher", "master"} or not supplied_id or not password:
                self._json({"error": "Invalid role or credentials."}, 400)
                return
            with connection() as con:
                if role == "student":
                    user = find_student_user(con, supplied_id)
                else:
                    user = None
                    for candidate in con.execute("SELECT * FROM users WHERE role=?", (role,)).fetchall():
                        aliases = json.loads(candidate["aliases"] or "[]")
                        if candidate["login_id"] == supplied_id or supplied_id in aliases:
                            user = candidate
                            break
                if not user or not password_matches(password, user["salt"], user["password_hash"]):
                    LOGIN_ATTEMPTS[ip].append(now)
                    self._json({"error": "Invalid credentials for the selected role."}, 401)
                    return
                LOGIN_ATTEMPTS[ip] = []
                metadata = json.loads(user["metadata"] or "{}")
                session_id = secrets.token_urlsafe(32)
                sess = {"role": role, "id": user["login_id"], "created_epoch": now,
                        "student_id": user["student_id"]}
                if role == "master":
                    sess["displayName"] = metadata.get("displayName", "Master Administrator")
                elif role == "teacher":
                    sess["id"] = metadata.get("id", user["login_id"])
                    sess["name"] = metadata.get("name", "Faculty")
                    sess["department"] = metadata.get("department", "")
                    sess["classTeacherOf"] = metadata.get("classTeacherOf", "")
                    teacher_data = metadata.get("teacher", {})
                    sess["teacher_json"] = json.dumps(teacher_data, ensure_ascii=False)
                    _, sess["teacher_mode"] = teacher_assignment(teacher_data, get_record_map(con))
                elif role == "student":
                    sess["name"] = metadata.get("name", "Student")
                SESSIONS[session_id] = sess
                self._log(con, sess, "login")
                con.commit()
                records = records_for_session(con, sess)
            cookie = self._session_cookie(session_id)
            self._json({"user": public_session(sess), "records": records}, 200, {"Set-Cookie": cookie})
            return
        if path == "/api/logout":
            sess, token = self._cookie_session()
            if token: SESSIONS.pop(token, None)
            extra = {"Set-Cookie": self._session_cookie("", 0)}
            self._json({"ok": True}, 200, extra)
            return
        if path == "/api/chat":
            ip = self._ip(); now = time.time()
            CHAT_HITS[ip] = [t for t in CHAT_HITS.get(ip, []) if now - t < 60]
            if len(CHAT_HITS[ip]) >= CHAT_LIMIT_PER_MIN:
                self._json({"error": "Too many messages. Please wait a minute."}, 429); return
            CHAT_HITS[ip].append(now)
            sess, _token = self._cookie_session()
            self._json(chat_reply(sess, str(body.get("message", "")), body.get("fee_state")))
            return
        if path == "/api/chat/student-lookup":
            ip = self._ip(); now = time.time()
            CHAT_LOOKUPS[ip] = [t for t in CHAT_LOOKUPS.get(ip, []) if now - t < 60]
            if len(CHAT_LOOKUPS[ip]) >= 15:
                self._json({"error": "Too many attempts. Please wait a minute."}, 429); return
            CHAT_LOOKUPS[ip].append(now)
            with connection() as con:
                user = find_student_user(con, str(body.get("regNo", "")))
            self._json({"found": bool(user)})
            return
        if path == "/api/chat/student-login":
            ip = self._ip(); now = time.time()
            LOGIN_ATTEMPTS[ip] = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < 60]
            if len(LOGIN_ATTEMPTS[ip]) >= 12:
                self._json({"error": "Too many login attempts. Please wait a minute."}, 429); return
            password = str(body.get("password", ""))
            with connection() as con:
                user = find_student_user(con, str(body.get("regNo", "")))
                if not user or not password:
                    LOGIN_ATTEMPTS[ip].append(now)
                    self._json({"error": "Registration number or password is incorrect."}, 401); return
                key = user["login_id"]
                REG_FAILS[key] = [t for t in REG_FAILS.get(key, []) if now - t < REG_FAIL_WINDOW]
                if len(REG_FAILS[key]) >= REG_FAIL_LIMIT:
                    self._json({"error": "Too many wrong passwords for this registration number. Try again in 10 minutes or contact the school office."}, 429); return
                if not password_matches(password, user["salt"], user["password_hash"]):
                    REG_FAILS[key].append(now); LOGIN_ATTEMPTS[ip].append(now)
                    self._json({"error": "Registration number or password is incorrect."}, 401); return
                REG_FAILS.pop(key, None); LOGIN_ATTEMPTS[ip] = []
                metadata = json.loads(user["metadata"] or "{}")
                sid = user["student_id"]
                row = con.execute("SELECT payload FROM records WHERE student_id=?", (sid,)).fetchone()
                if not row:
                    self._json({"error": "No record is linked to this account. Please contact the school office."}, 404); return
                session_id = secrets.token_urlsafe(32)
                sess = {"role": "student", "id": user["login_id"], "created_epoch": now,
                        "student_id": sid, "name": metadata.get("name", "Student")}
                SESSIONS[session_id] = sess
                self._log(con, sess, "chat-login", student_id=sid)
                con.commit()
                dash = chat_dashboard(json.loads(row[0]))
            cookie = self._session_cookie(session_id)
            self._json({"dashboard": dash}, 200, {"Set-Cookie": cookie})
            return
        if path == "/api/chat/teacher-login":
            ip = self._ip(); now = time.time()
            LOGIN_ATTEMPTS[ip] = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < 60]
            if len(LOGIN_ATTEMPTS[ip]) >= 12:
                self._json({"error": "Too many login attempts. Please wait a minute."}, 429); return
            supplied = norm_teacher_key(str(body.get("id", "")))
            password = str(body.get("password", ""))
            bad = {"error": "Teacher/Master ID or password is incorrect."}
            if not supplied or not password or len(supplied) > 40 or len(password) > 200:
                LOGIN_ATTEMPTS[ip].append(now); self._json(bad, 401); return
            with connection() as con:
                user = None
                for cand in con.execute("SELECT * FROM users WHERE role IN ('teacher','master')"):
                    if cand["login_id"] == supplied or supplied in json.loads(cand["aliases"] or "[]"):
                        user = cand; break
                if not user:
                    LOGIN_ATTEMPTS[ip].append(now); self._json(bad, 401); return
                key = "T:" + user["login_id"]
                REG_FAILS[key] = [t for t in REG_FAILS.get(key, []) if now - t < REG_FAIL_WINDOW]
                if len(REG_FAILS[key]) >= REG_FAIL_LIMIT:
                    self._json({"error": "Too many wrong passwords for this teacher ID. Try again in 10 minutes or contact the administrator."}, 429); return
                if not password_matches(password, user["salt"], user["password_hash"]):
                    REG_FAILS[key].append(now); LOGIN_ATTEMPTS[ip].append(now); self._json(bad, 401); return
                REG_FAILS.pop(key, None); LOGIN_ATTEMPTS[ip] = []
                metadata = json.loads(user["metadata"] or "{}")
                session_id = secrets.token_urlsafe(32)
                if user["role"] == "master":
                    sess = {"role": "master", "id": user["login_id"], "created_epoch": now, "student_id": user["student_id"],
                            "displayName": metadata.get("displayName", "Master Administrator")}
                    mode = "master"; shown = {"name": sess["displayName"], "department": "Administration", "mode": "master", "role": "master"}
                else:
                    teacher_data = metadata.get("teacher", {})
                    _, mode = teacher_assignment(teacher_data, get_record_map(con))
                    sess = {"role": "teacher", "id": metadata.get("id", user["login_id"]), "created_epoch": now,
                            "student_id": user["student_id"], "name": metadata.get("name", "Faculty"),
                            "department": metadata.get("department", ""), "classTeacherOf": metadata.get("classTeacherOf", ""),
                            "teacher_json": json.dumps(teacher_data, ensure_ascii=False), "teacher_mode": mode}
                    shown = {"name": sess["name"], "department": sess["department"], "mode": mode, "role": "teacher"}
                SESSIONS[session_id] = sess
                self._log(con, sess, "chat-login")
                con.commit()
            cookie = self._session_cookie(session_id)
            self._json({"teacher": shown}, 200, {"Set-Cookie": cookie})
            return
        if path == "/api/chat/teacher-find":
            sess = self._require_session()
            if not sess: return
            if sess.get("role") not in {"teacher", "master"}:
                self._json({"error": "Only teachers and administrators can look up students."}, 403); return
            ip = self._ip(); now = time.time()
            CHAT_LOOKUPS[ip] = [t for t in CHAT_LOOKUPS.get(ip, []) if now - t < 60]
            if len(CHAT_LOOKUPS[ip]) >= 60:
                self._json({"error": "Too many searches. Please wait a minute."}, 429); return
            CHAT_LOOKUPS[ip].append(now)
            student_id = str(body.get("student_id", "")).strip()
            query = str(body.get("query", "")).strip()
            if student_id and len(student_id) > 120 or len(query) > 60 or not (student_id or len(query) >= 2):
                self._json({"error": "Please type at least 2 characters of a registration number or name."}, 400); return
            with connection() as con:
                allowed = allowed_ids_for_session(con, sess)
                role, mode = sess.get("role", ""), sess.get("teacher_mode", "academic")
                if student_id:
                    if student_id not in allowed:
                        self._json({"error": "This account is not assigned to that student."}, 403); return
                    row = con.execute("SELECT payload FROM records WHERE student_id=?", (student_id,)).fetchone()
                    if not row:
                        self._json({"error": "Student record not found."}, 404); return
                    self._log(con, sess, "chat-student-view", student_id); con.commit()
                    self._json({"record": student_view_for_role(json.loads(row[0]), role, mode)}); return
                q = query.lower()
                exact, matches = None, []
                tokens = [t for t in re.findall(r"[a-z0-9/\-]+", q) if len(t) > 0]
                for sid, payload in con.execute("SELECT student_id,payload FROM records"):
                    if sid not in allowed:
                        continue
                    rec = json.loads(payload)
                    if q in (str(rec.get("regNo", "")).lower(), sid.lower()):
                        exact = (sid, rec); break
                    hay = " ".join(str(rec.get(k, "")) for k in ("name", "id", "regNo", "cls", "sec")).lower()
                    if tokens and all(t in hay for t in tokens):
                        matches.append(rec)
                if not exact and len(matches) == 1:
                    exact = (matches[0]["id"], matches[0])
                if exact:
                    self._log(con, sess, "chat-student-view", exact[0]); con.commit()
                    self._json({"record": student_view_for_role(exact[1], role, mode)}); return
                matches.sort(key=lambda r: (str(r.get("cls")), int(r.get("roll") or 0)))
                self._json({"count": len(matches), "matches": [
                    {"id": r["id"], "name": r.get("name", ""), "cls": r.get("cls", ""), "sec": r.get("sec", ""), "roll": r.get("roll", "")}
                    for r in matches[:8]]})
            return
        if path in ("/api/fees/create-order", "/api/fees/confirm"):
            sess, _t = self._cookie_session()
            if not sess: self._json({"error": "Please sign in again."}, 401); return
            out, code = fee_api(self, sess, path, body)
            self._json(out, code); return
        if path == "/api/pay-fee":  # legacy instant-pay endpoint removed: it confirmed payments without a gateway
            self._json({"error": "Use the fee payment page (create order \u2192 confirm). Instant unverified payment is disabled."}, 410); return
        if path == "/api/contact":
            name = str(body.get("name", "")).strip()
            email = str(body.get("email", "")).strip()
            message = str(body.get("message", "")).strip()
            if not name or len(name) > 120 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254 or not message or len(message) > 5000:
                self._json({"error": "Please provide a valid name, email and message (maximum 5000 characters)."}, 400)
                return
            with connection() as con:
                cur = con.execute("INSERT INTO enquiries(created_at,name,email,message) VALUES(?,?,?,?)",
                                  (utc_now(), name, email, message))
                enquiry_id = cur.lastrowid
                con.commit()
            self._json({"ok": True, "id": enquiry_id, "message": "Inquiry saved on this server. This form does not email the school."}, 201)
            return
        if path == "/api/admin/import-legacy":
            sess = self._require_session()
            if not sess: return
            if sess.get("role") != "master":
                self._json({"error": "Master access required."}, 403)
                return
            legacy = body.get("records")
            if not isinstance(legacy, dict):
                self._json({"error": "No legacy records supplied."}, 400)
                return
            seed = seed_record_map()
            changes = 0
            skipped_records = 0
            skipped_fields = 0
            immutable = {"id", "regNo", "password", "passwordHash"}
            with connection() as con:
                for sid, old_record in legacy.items():
                    if sid not in seed or not isinstance(old_record, dict):
                        skipped_records += 1
                        continue
                    clean = sanitize_student(old_record)
                    baseline = sanitize_student(seed[sid])
                    changed = {}
                    for key, value in clean.items():
                        if key in immutable:
                            continue
                        if key not in baseline:
                            skipped_fields += 1
                            continue
                        if value != baseline[key]:
                            changed[key] = value
                    row = con.execute("SELECT payload FROM records WHERE student_id=?", (sid,)).fetchone()
                    if not row:
                        skipped_records += 1
                        continue
                    if changed:
                        current = json.loads(row[0])
                        current.update(changed)
                        con.execute("UPDATE records SET payload=?,updated_at=? WHERE student_id=?",
                                    (json.dumps(sanitize_student(current), ensure_ascii=False), utc_now(), sid))
                        self._log(con, sess, "legacy-localstorage-import", sid, list(changed.keys()))
                        changes += 1
                con.commit()
                records = records_for_session(con, sess)
            self._json({"ok": True, "changed_records": changes, "skipped_records": skipped_records,
                        "skipped_fields": skipped_fields, "records": records})
            return
        self._json({"error": "Not found."}, 404)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._host_origin_ok():
            self._json({"error": "Cross-origin request rejected."}, 403)
            return
        sess = self._require_session()
        if not sess: return
        if path != "/api/student-record":
            self._json({"error": "Not found."}, 404)
            return
        sid = parse_qs(parsed.query).get("student_id", [""])[0]
        if not sid or len(sid) > 120:
            self._json({"error": "Invalid student identifier."}, 400)
            return
        if sess.get("role") not in {"teacher", "master"}:
            self._json({"error": "Read-only role. Student accounts cannot modify records."}, 403)
            return
        try:
            patch = self._body(limit=200_000)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        if not patch or not isinstance(patch, dict):
            self._json({"error": "No changes supplied."}, 400)
            return
        if sess.get("role") == "teacher":
            if sess.get("teacher_mode") == "extracurricular":
                forbidden = set(patch) - {"extracurricular"}
                denial = "Activity faculty may edit extracurricular activities only."
            else:
                forbidden = set(patch) - {"academics", "attendance", "remarks"}
                denial = "Academic teachers may edit marks, attendance and remarks only."
            if forbidden:
                self._json({"error": denial}, 403)
                return
        allowed_fields = {"academics", "attendance", "remarks", "parents", "fees", "discipline", "extracurricular", "house", "room", "classTeacher", "sec", "cls", "roll"}
        if set(patch) - allowed_fields:
            self._json({"error": "One or more fields cannot be edited."}, 400)
            return
        with connection() as con:
            row = con.execute("SELECT payload FROM records WHERE student_id=?", (sid,)).fetchone()
            if not row:
                self._json({"error": "Student record not found."}, 404)
                return
            scope = allowed_ids_for_session(con, sess)
            if sid not in scope:
                self._json({"error": "This account is not assigned to that student."}, 403)
                return
            record = json.loads(row[0])
            if "academics" in patch:
                academics = patch["academics"]
                if not isinstance(academics, dict):
                    self._json({"error": "Academic data must be an object."}, 400); return
                current_academics = record.get("academics", {})
                marks = academics.get("marks", current_academics.get("marks", []))
                if not isinstance(marks, list) or len(marks) > 40:
                    self._json({"error": "Invalid marks list."}, 400); return
                if sess.get("role") == "teacher":
                    # Academic teachers can change marks only. Subject titles, maximum marks,
                    # rank, term and other server-owned academic metadata remain authoritative.
                    old_marks = current_academics.get("marks", [])
                    if len(marks) != len(old_marks):
                        self._json({"error": "The subject list cannot be changed by a teacher."}, 400); return
                    safe_marks = []
                    for old_mark, supplied_mark in zip(old_marks, marks):
                        if not isinstance(old_mark, dict) or not isinstance(supplied_mark, dict):
                            self._json({"error": "Invalid mark entry."}, 400); return
                        try:
                            obtained = float(supplied_mark.get("m", old_mark.get("m", 0)))
                            maximum = float(old_mark.get("total", 100))
                        except (ValueError, TypeError):
                            self._json({"error": "Marks must be numeric."}, 400); return
                        if maximum <= 0 or obtained < 0 or obtained > maximum:
                            self._json({"error": "Marks must stay between zero and the maximum marks."}, 400); return
                        percent = obtained / maximum * 100
                        grade = ("A1" if percent >= 91 else "A2" if percent >= 81 else "B1" if percent >= 71 else
                                 "B2" if percent >= 61 else "C1" if percent >= 51 else "C2" if percent >= 41 else "D" if percent >= 33 else "E")
                        safe_marks.append({**old_mark, "m": obtained, "total": maximum, "grade": grade})
                    aggregate = (f"{sum((float(m['m']) / float(m['total']) * 100) for m in safe_marks) / len(safe_marks):.1f}%"
                                 if safe_marks else str(current_academics.get("aggregate", "—")))
                    patch["academics"] = {"marks": safe_marks, "aggregate": aggregate}
                else:
                    for mark in marks:
                        if not isinstance(mark, dict):
                            self._json({"error": "Invalid mark entry."}, 400); return
                        try: obtained, maximum = float(mark.get("m", 0)), float(mark.get("total", 100))
                        except (ValueError, TypeError):
                            self._json({"error": "Marks must be numeric."}, 400); return
                        if maximum <= 0 or obtained < 0 or obtained > maximum:
                            self._json({"error": "Marks must stay between zero and the maximum marks."}, 400); return
                    patch["academics"] = academics
            if "attendance" in patch:
                attendance = patch["attendance"]
                if not isinstance(attendance, dict):
                    self._json({"error": "Attendance data must be an object."}, 400); return
                try:
                    present, total = int(attendance.get("present")), int(attendance.get("total"))
                    if str(attendance.get("present")) != str(present) or str(attendance.get("total")) != str(total):
                        raise ValueError()
                except (ValueError, TypeError):
                    self._json({"error": "Attendance must use whole numbers."}, 400); return
                if total <= 0 or present < 0 or present > total or total > 400:
                    self._json({"error": "Attendance requires 0 ≤ present ≤ total ≤ 400 and total above zero."}, 400); return
                rate = f"{present / total * 100:.1f}%"
                # Ignore forged nested fields when a teacher submits a full UI object.
                patch["attendance"] = ({"present": present, "total": total, "rate": rate}
                                        if sess.get("role") == "teacher" else {**attendance, "rate": rate})
            if "remarks" in patch and (not isinstance(patch["remarks"], str) or len(patch["remarks"]) > 3000):
                self._json({"error": "Remarks must be text of at most 3000 characters."}, 400); return
            if "extracurricular" in patch:
                activities = patch["extracurricular"]
                if not isinstance(activities, list) or len(activities) > 30 or any(not isinstance(x, str) or len(x) > 250 for x in activities):
                    self._json({"error": "Extracurricular activities must be a list of up to 30 text items (250 characters each)."}, 400); return
                patch["extracurricular"] = [x.strip() for x in activities if x.strip()]
            for key, value in patch.items():
                if isinstance(value, dict) and isinstance(record.get(key), dict):
                    record[key].update(value)
                else:
                    record[key] = value
            record = sanitize_student(record)
            con.execute("UPDATE records SET payload=?,updated_at=? WHERE student_id=?",
                        (json.dumps(record, ensure_ascii=False), utc_now(), sid))
            self._log(con, sess, "student-record-update", sid, list(patch.keys()))
            con.commit()
        # Return only this authorized record; caller refreshes its own scoped copy.
        self._json({"ok": True, "record": student_view_for_role(record, sess.get("role", ""), sess.get("teacher_mode", "academic")), "committed_at": utc_now()})

    def do_OPTIONS(self):
        if self._cors_origin():
            # CORS preflight from an allow-listed frontend origin.
            self._headers(204, length=0, extra={
                "Access-Control-Allow-Methods": "GET, POST, PATCH, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600"})
            return
        self._json({"error": "CORS is disabled for this origin. Use the same origin or add it to JVM_ALLOWED_ORIGINS."}, 405)


def main():
    parser = argparse.ArgumentParser(description="JVM Shyamali website and secure record API")
    parser.add_argument("--host", default=os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"), help="Bind address (default: 127.0.0.1 locally, 0.0.0.0 when the host sets PORT)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8000), help="Port (default: $PORT or 8000)")
    args = parser.parse_args()
    seed_database_if_needed()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"JVM Shyamali website running at http://{args.host}:{args.port}")
    print(f"SQLite records are saved at {DB_PATH}; static file access uses an explicit allowlist.")
    print("Stop with Ctrl+C. Use HTTPS and rotate all demo credentials before deployment.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped. Saved records remain in SQLite.")
    finally:
        httpd.server_close()

if __name__ == "__main__":
    main()
