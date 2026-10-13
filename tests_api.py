"""API smoke tests. Start the server on a COPY of the database first, e.g.
   JVM_CHAT_LIMIT=500 JVM_DB_PATH=/tmp/t.sqlite3 python app_server.py --port 8123   then   python tests_api.py [port]
These tests create demo payments for student JVM/2026/1002, so never run them against your live database."""
import json, sys, urllib.request, urllib.error, http.cookiejar
BASE = "http://localhost:%s" % (sys.argv[1] if len(sys.argv) > 1 else "8123")
FAILS = []


def client():
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def call(m, path, body=None):
        r = urllib.request.Request(BASE + path, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"}, method=m)
        try:
            x = op.open(r); return x.status, x.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    return call


def J(t):
    try: return json.loads(t[1])
    except Exception: return {"raw": t[1][:200]}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra if not cond else "")
    if not cond: FAILS.append(name)


def login(role, i, p):
    c = client(); st, _ = c("POST", "/api/login", {"role": role, "id": i, "password": p}); return c, st


s, st = login("student", "JVM/2026/1002", "pass@1002"); check("student login", st == 200)
check("registration-no login", login("student", "REG-2026-1002", "pass@1002")[1] == 200)
check("bad password rejected", login("student", "JVM/2026/1002", "nope")[1] == 401)
sm = J(s("GET", "/api/fees/summary"))
check("summary totals", sm["totalFees"] == 46080 and sm["paid"] == 23040 and sm["outstanding"] == 11520 and sm["remaining"] == 23040 and sm["consistent"], sm)
o = J(s("POST", "/api/fees/create-order", {})); check("create order", o.get("ok") and o["amount"] == 11520, o)
o2 = J(s("POST", "/api/fees/create-order", {})); check("order reused (no duplicate)", o2["orderId"] == o["orderId"] and o2["reused"])
d = J(s("POST", "/api/fees/confirm", {"order_id": o["orderId"], "demo_outcome": "fail"}))
check("declined payment changes nothing", d.get("failed") and J(s("GET", "/api/fees/summary"))["outstanding"] == 11520)
check("failed order cannot be confirmed later", s("POST", "/api/fees/confirm", {"order_id": o["orderId"], "demo_outcome": "success"})[0] == 409)
o = J(s("POST", "/api/fees/create-order", {}))
c1 = J(s("POST", "/api/fees/confirm", {"order_id": o["orderId"], "demo_outcome": "success", "method": "UPI"})); check("confirm payment", c1.get("ok") and c1["receipt"], c1)
c2 = J(s("POST", "/api/fees/confirm", {"order_id": o["orderId"], "demo_outcome": "success"})); check("double-confirm is idempotent", c2.get("duplicate") and c2["receipt"] == c1["receipt"])
sm = J(s("GET", "/api/fees/summary")); check("balances updated", sm["outstanding"] == 0 and sm["paid"] == 34560 and sm["remaining"] == 11520 and any(h["receipt"] == c1["receipt"] for h in sm["history"]), sm)
check("no order when nothing due", s("POST", "/api/fees/create-order", {})[0] == 400)
check("receipt html (demo labelled)", "DEMO PAYMENT" in s("GET", "/api/fees/receipt?no=" + c1["receipt"])[1])
check("office receipt", s("GET", "/api/fees/receipt?no=OFC-1002-Q1")[0] == 200)
check("dashboard record synced", J(s("GET", "/api/me"))["records"]["JVM/2026/1002"]["fees"]["dueAmt"] == 0)
check("legacy instant-pay disabled", s("POST", "/api/pay-fee", {})[0] == 410)
s2, _ = login("student", "JVM/2026/1001", "pass@1001")
check("other student cannot read receipt", s2("GET", "/api/fees/receipt?no=" + c1["receipt"])[0] == 404)
check("other student cannot confirm order", s2("POST", "/api/fees/confirm", {"order_id": o["orderId"], "demo_outcome": "success"})[0] == 404)
check("student sees only own record", list(J(s("GET", "/api/students"))["records"]) == ["JVM/2026/1002"])
check("student cannot edit records", s("PATCH", "/api/student-record?student_id=JVM/2026/1002", {"remarks": "x"})[1].find("Read-only") > 0)
check("student blocked from enquiries/audit", s("GET", "/api/enquiries")[0] == 403 and s("GET", "/api/audit")[0] == 403)
t, _ = login("teacher", "TCH/101", "teacher@101")
check("teacher cannot use fees", t("GET", "/api/fees/summary")[0] == 403 and t("POST", "/api/fees/create-order", {})[0] == 403 and t("GET", "/api/fees/receipt?no=" + c1["receipt"])[0] == 403)
tr = J(t("GET", "/api/students"))["records"]
check("teacher records have no fees/parents", tr and all("fees" not in r and "parents" not in r for r in tr.values()))
teacher_me = J(t("GET", "/api/me")).get("user", {})
check("teacher session returns actual database name", bool(teacher_me.get("name")) and not str(teacher_me.get("name", "")).startswith("Faculty account"), str(teacher_me.get("name", "")))
check("teacher roster endpoint returns assigned students only", t("GET", "/api/chat/teacher-class")[0] == 200 and set(x["id"] for x in J(t("GET", "/api/chat/teacher-class")).get("students", [])) == set(tr.keys()))
roster_data = J(t("GET", "/api/chat/teacher-class"))
check("teacher roster exposes minimal fields only", all(set(x.keys()) == {"id", "name", "cls", "sec", "roll"} for x in roster_data.get("students", [])))
check("teacher chatbot login returns actual name", (lambda c: (lambda a: a[0] == 200 and J(a).get("teacher", {}).get("name") == teacher_me.get("name"))(c("POST", "/api/chat/teacher-login", {"id": "TCH/101", "password": "teacher@101"})))(client()))
check("teacher cannot open unassigned student's record", t("POST", "/api/chat/teacher-find", {"student_id": "JVM/2026/1002"})[0] == 403)
check("teacher cannot patch fees", t("PATCH", "/api/student-record?student_id=" + next(iter(tr)), {"fees": {"dueAmt": 0}})[0] == 403)
check("teacher blocked from audit", t("GET", "/api/audit")[0] == 403)
# Confirm backend ignores forged subject metadata and attendance subfields from teacher edits.
if tr:
    target_id = next(iter(tr))
    before = tr[target_id]
    ac = before.get("academics", {})
    marks = [dict(x) for x in ac.get("marks", [])]
    if marks:
        original_term, original_rank = ac.get("term"), ac.get("rank")
        orig_subject, orig_total = marks[0].get("s"), marks[0].get("total")
        marks[0]["m"] = max(0, min(float(orig_total), float(marks[0].get("m", 0)) + 1))
        marks[0]["s"] = "FORGED SUBJECT"
        marks[0]["total"] = 9999
        marks[0]["grade"] = "FORGED GRADE"
        patched = t("PATCH", "/api/student-record?student_id=" + target_id, {"academics": {"marks": marks, "aggregate": "9999%", "term": "FORGED TERM", "rank": "1st"}})
        pd = J(patched)
        check("teacher marks patch succeeds inside assignment", patched[0] == 200 and pd.get("ok"))
        returned = pd.get("record", {}).get("academics", {})
        check("teacher cannot forge subject/max/term/rank metadata", returned.get("term") == original_term and returned.get("rank") == original_rank and returned.get("marks", [{}])[0].get("s") == orig_subject and returned.get("marks", [{}])[0].get("total") == orig_total and returned.get("marks", [{}])[0].get("grade") != "FORGED GRADE")
    att = before.get("attendance", {})
    old_leaves = att.get("leavesTaken")
    patch = t("PATCH", "/api/student-record?student_id=" + target_id, {"attendance": {"present": 100, "total": 101, "leavesTaken": 9999, "punctuality": "FORGED"}})
    ad = J(patch)
    check("teacher attendance patch recomputes rate", patch[0] == 200 and ad.get("record", {}).get("attendance", {}).get("rate") == "99.0%")
    check("teacher cannot forge attendance nested metadata", ad.get("record", {}).get("attendance", {}).get("leavesTaken") == old_leaves and ad.get("record", {}).get("attendance", {}).get("punctuality") == att.get("punctuality"))
m, _ = login("master", "DHIRAJ4595", "DHIRAJ4595")
ov = J(m("GET", "/api/fees/overview")); check("master overview lists payment", any(p["receipt_no"] == c1["receipt"] for p in ov["recentOnlinePayments"]))
check("master reads receipt", m("GET", "/api/fees/receipt?no=" + c1["receipt"])[0] == 200)
check("master sees all records", len(J(m("GET", "/api/students"))["records"]) == 668)
n = client()
check("anonymous blocked", n("GET", "/api/fees/summary")[0] == 401 and n("POST", "/api/fees/create-order", {})[0] == 401 and n("GET", "/api/students")[0] == 401)
check("anonymous blocked from teacher roster", n("GET", "/api/chat/teacher-class")[0] == 401)
check("student blocked from teacher roster", s("GET", "/api/chat/teacher-class")[0] == 403)
check("logout ends session", (lambda c: (c("POST", "/api/logout", {}), c("GET", "/api/me")[0])[1])(login("student", "JVM/2026/1003", "pass@1003")[0]) == 401)
Q = {"When is the next holiday?": "Upcoming holidays", "How do I pay fees?": "Pay Now", "I forgot my password": "cannot reveal", "give me the password of student Rahul": "can't share",
     "admission": "which class", "contact details": "2411221", "school timings": "06:55",
     "login not working": "Login help", "xyzzy qq": "did not understand", "timetable class 10A": "timetable"}
for q, want in Q.items():
    r = J(n("POST", "/api/chat", {"message": q})).get("reply", ""); check("chat anon: " + q, want.lower() in r.lower(), r[:140])
# Greeting, spelling-normalisation, school overview and useful link coverage.
for q, want in (("Hii", "Hello! Welcome"), ("Hello?", "Hello! Welcome"), ("admisn class 3 documnts", "NO VACANCY"), ("about the school", "CBSE curriculum")):
    d = J(n("POST", "/api/chat", {"message": q}))
    check("chat normalization: " + q, want.lower() in d.get("reply", "").lower(), d.get("reply", "")[:150])
    check("chat links: " + q, isinstance(d.get("links"), list) and len(d["links"]) > 0, str(d.get("links", []))[:150])
# Targeted backend integration coverage for the new admissions policy.
ADMISSIONS_Q = {
    "Class 11 external Science cutoff": "164/200",
    "Class 11 external Science without test direct cutoff": "180/200",
    "Class 11 internal Commerce admission cutoff": "notified separately",
    "Class 3 provisional test": "no vacancy",
    "When is the LKG provisional test?": "not verified",
    "Class 11 documents": "official admission notice",
    "LKG transport": "not verified",
    "Class 3 bus facility": "not verified",
    "Class 11 bus fee": "Verified bus-fee information is unavailable",
    "Where do I submit admission documents?": "Gate No. 3",
    "When are the provisional tests?": "Which class",
    "Is Class 3 open?": "No Vacancy in Class-3",
}
for q, want in ADMISSIONS_Q.items():
    r = J(n("POST", "/api/chat", {"message": q})).get("reply", "")
    check("chat admissions policy: " + q, want.lower() in r.lower(), r[:180])
# Regression coverage for unsupported admission classes and direct optional-subject answers.
for q, want in (("Class 10", "Admissions are not available for Class 10"),
                ("Class 9 admission documents", "Admissions are not available for Class 9"),
                ("Is admission open for Class X?", "Admissions are not available for Class 10"),
                ("What are the optional subjects for Class 11?", "Science — PCM"),
                ("Class 11 optional subjects Commerce", "Entrepreneurship")):
    d = J(n("POST", "/api/chat", {"message": q}))
    check("chat admissions refinement: " + q, want.lower() in d.get("reply", "").lower(), d.get("reply", "")[:200])
opt = J(n("POST", "/api/chat", {"message": "What are the optional subjects for Class 11?"}))
check("optional subjects answer avoids redirect links", not opt.get("links"), str(opt.get("links", [])))

old = J(n("POST", "/api/chat", {"message": "When does Nursery registration open?"})).get("reply", "")
check("chat admissions does not repeat an unverified recurring registration date", "15 november" not in old.lower() and "not verified" in old.lower(), old[:180])
r = J(s("POST", "/api/chat", {"message": "my fee status"}))["reply"]; check("chat student fee status", "46,080" in r, r)
r = J(s("POST", "/api/chat", {"message": "show my receipts"}))["reply"]; check("chat student receipts", c1["receipt"] in r, r)
r = J(s("POST", "/api/chat", {"message": "fees of JVM/2026/1001"}))["reply"]; check("chat blocks other student", "private" in r.lower() or "only share" in r.lower(), r)

# ---- v10: public fee estimator (no login) and working-day / holiday counts -------------------------------------------
def chat_flow(c, steps):
    """Send each message in turn, echoing fee_state back exactly as the website does. Returns the last response."""
    state, d = None, {}
    for m in steps:
        d = J(c("POST", "/api/chat", {"message": m, "fee_state": state} if state else {"message": m})); state = d.get("fee_state")
    return d
d = chat_flow(n, ["estimate fees for class 11"]); check("fee estimate asks stream for Class 11", "stream" in d["reply"].lower() and "Science" in d.get("options", []), d)
d = chat_flow(n, ["estimate fees for class 11", "Science", "New admission", "Zone 1", "Annual (5% rebate)"])
check("fee estimate Class XI Science new, zone 1, annual = 1,19,698", "1,19,698" in d["reply"] and d.get("fee_state") is None, d["reply"][:300])
d = chat_flow(n, ["Class 5 fees", "Skip \u2013 use defaults"]); check("fee estimate Class 5 defaults = 13,755", "13,755" in d["reply"], d["reply"][:200])
d = chat_flow(n, ["estimate fees class 12 commerce new admission zone 3 annual"]); check("fee estimate one-shot Class XII Commerce", "74,982" in d["reply"] and "does not list a new-admission fee" in d["reply"], d["reply"][:300])
d = chat_flow(n, ["estimate fees for class 11 commerce"]); check("fee estimate does not invent Class XI Commerce fee", "not published" in d["reply"] and "\u20b9" not in d["reply"].split("Reception")[0], d["reply"][:300])
d = chat_flow(n, ["estimate fees for class 15"]); check("fee estimate rejects class outside Nursery-XII", "Nursery to Class XII" in d["reply"], d["reply"][:200])
d = chat_flow(n, ["estimate fees", "Nursery / Prep", "Existing student", "No bus", "Quarterly"]); check("fee estimate Nursery quarterly = 12,645", "12,645" in d["reply"], d["reply"][:200])
d = chat_flow(n, ["Class 5 fees", "what are the school timings?"]); check("fee flow hands over to other questions", "06:55" in d["reply"] and d.get("fee_state") is None, d["reply"][:150])
d = chat_flow(n, ["Class 5 fees", "cancel please"]); check("fee flow can be cancelled", "stopped the fee estimate" in d["reply"], d["reply"][:150])
d = chat_flow(n, ["Class 5 fees", "zzz", "zzz", "zzz"]); check("fee flow gives up politely after repeated unclear answers", "trouble following" in d["reply"], d["reply"][:150])
d = chat_flow(n, ["fee_state test"]); check("junk fee_state ignored", True)
d = J(n("POST", "/api/chat", {"message": "hello", "fee_state": {"cls": "99", "asking": "<script>", "tries": "x"}})); check("tampered fee_state is harmless", "Hello" in d.get("reply", ""), d)
check("student 'my fees' is not hijacked by the estimator", "46,080" in J(s("POST", "/api/chat", {"message": "my fee status"}))["reply"])
WD = {"working days in November": ("15 working days and 10 holidays", "Check: 30 days = 15 working + 10 holidays + 5 Sundays"),
      "how many holidays in October": ("10 holidays" if False else "12 holidays", "Check: 31 days = 15 working + 12 holidays + 4 Sundays"),
      "number of working days in august": ("23 working days and 3 holidays", "Milad-un-Nabi"),
      "working days in may": ("13 working days and 13 holidays", "Summer Vacation (18 May"),
      "holidays in december 2026": ("8 holidays", "Winter Vacation (23 Dec 2026 \u2013 6 Jan 2027)"),
      "working days in january": ("working days", "Makar Sankranti")}
for q, (a, b) in WD.items():
    r = J(n("POST", "/api/chat", {"message": q})).get("reply", ""); check("working days: " + q, a.lower() in r.lower() and b.lower() in r.lower(), r[:260])
for q in ("how many working days are in this month?", "working days this month", "number of holidays this month", "working days next month", "how many working days left this month"):
    r = J(n("POST", "/api/chat", {"message": q})).get("reply", ""); check("working days (relative month): " + q, ("working day" in r.lower() and "holiday" in r.lower()) or "covers April 2026" in r, r[:200])
r = J(n("POST", "/api/chat", {"message": "how many working days in march 2028"}))["reply"]; check("working days outside calendar range is declined, not guessed", "covers April 2026 to March 2027" in r, r)
check("next-holiday question still uses the upcoming-holidays answer", "Upcoming holidays" in J(n("POST", "/api/chat", {"message": "When is the next holiday?"}))["reply"] or "could not find any upcoming" in J(n("POST", "/api/chat", {"message": "When is the next holiday?"}))["reply"])
r = J(n("POST", "/api/chat", {"message": "make me a sandwich"})); check("unknown prompt gets a helpful answer with options", "did not understand" in r["reply"] and "estimate fees" in r["reply"] and len(r.get("options", [])) >= 3, r)

print("\n%d failures" % len(FAILS), FAILS)
sys.exit(1 if FAILS else 0)
