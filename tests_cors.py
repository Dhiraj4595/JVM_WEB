"""CORS / split-hosting checks.  Start the server with an allow-list first, e.g.:
   JVM_DB_PATH=/tmp/t.sqlite3 JVM_ALLOWED_ORIGINS=https://www.example-school.in python app_server.py --port 8124
   python tests_cors.py 8124 https://www.example-school.in
"""
import json, sys, urllib.request, urllib.error, pathlib
port = sys.argv[1] if len(sys.argv) > 1 else "8124"
ok_origin = sys.argv[2] if len(sys.argv) > 2 else "https://www.example-school.in"
bad_origin = "https://evil.example.com"
base = f"http://127.0.0.1:{port}"
master = json.loads((pathlib.Path(__file__).parent / "private_data" / "seed_master.json").read_text())
fails = []

def call(method, path, origin=None, body=None, extra=None):
    h = {"Content-Type": "application/json"}
    if origin: h["Origin"] = origin
    h.update(extra or {})
    req = urllib.request.Request(base + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        r = urllib.request.urlopen(req); return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

s, h, _ = call("OPTIONS", "/api/login", ok_origin, extra={"Access-Control-Request-Method": "POST"})
check("preflight from allowed origin -> 204", s == 204)
check("preflight echoes exact origin", h.get("Access-Control-Allow-Origin") == ok_origin)
check("preflight allows credentials", h.get("Access-Control-Allow-Credentials") == "true")
check("preflight allows POST+PATCH", "POST" in h.get("Access-Control-Allow-Methods", "") and "PATCH" in h.get("Access-Control-Allow-Methods", ""))
check("preflight allows Content-Type header", "Content-Type" in h.get("Access-Control-Allow-Headers", ""))
s, h, _ = call("OPTIONS", "/api/login", bad_origin)
check("preflight from unlisted origin refused", s == 405 and "Access-Control-Allow-Origin" not in h)
s, h, _ = call("POST", "/api/login", bad_origin, {"role": "master", "id": master["id"], "password": master["password"]})
check("login POST from unlisted origin rejected (403)", s == 403 and "Access-Control-Allow-Origin" not in h)
s, h, _ = call("POST", "/api/login", ok_origin, {"role": "master", "id": master["id"], "password": master["password"]})
ck = h.get("Set-Cookie", "")
check("login from allowed origin works", s == 200)
check("response carries CORS headers", h.get("Access-Control-Allow-Origin") == ok_origin and h.get("Vary") == "Origin")
check("cross-site cookie is HttpOnly; SameSite=None; Secure", "HttpOnly" in ck and "SameSite=None" in ck and "Secure" in ck)
token = ck.split(";")[0]
s, h, b = call("GET", "/api/me", ok_origin, extra={"Cookie": token})
check("cookie session accepted on /api/me", s == 200 and json.loads(b)["user"]["role"] == "master")
s, h, _ = call("POST", "/api/login", None, {"role": "master", "id": master["id"], "password": master["password"]})
ck2 = h.get("Set-Cookie", "")
check("same-origin login still uses SameSite=Strict", s == 200 and "SameSite=Strict" in ck2 and "SameSite=None" not in ck2)
s, h, _ = call("POST", "/api/login", None, {"role": "master", "id": master["id"], "password": "wrong-password"})
check("wrong password still rejected", s == 401)
s, h, _ = call("GET", "/healthz")
check("healthz ok", s == 200)
s, h, b = call("GET", "/jvm-config.js")
check("jvm-config.js is served", s == 200 and b"JVM_API_BASE" in b)
for p in ("/private_data/school.sqlite3", "/private_data/seed_master.json", "/app_server.py", "/section-roster.json", "/original-student-records.json"):
    s, _, _ = call("GET", p); check(f"{p} NOT served", s == 404)
print("\n%d failures %s" % (len(fails), fails)); sys.exit(1 if fails else 0)
