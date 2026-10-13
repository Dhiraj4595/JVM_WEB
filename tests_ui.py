"""Browser tests (needs: pip install playwright && playwright install chromium). Run the server on a COPY of the DB first:
   JVM_CHAT_LIMIT=500 JVM_DB_PATH=/tmp/t.sqlite3 python app_server.py --port 8123  then  python tests_ui.py [port]"""
import sys, os
from playwright.sync_api import sync_playwright
B = os.environ.get("JVM_TEST_BASE", "http://localhost:%s" % (sys.argv[1] if len(sys.argv) > 1 else "8123"))
FAILS = []
def check(n, c, x=""):
    print(("PASS " if c else "FAIL ") + n, "" if c else x)
    if not c: FAILS.append(n)

def login(page, tab, uid, pw):
    page.goto(B + "/pages/portal.html", wait_until="domcontentloaded"); page.click("#tab_" + tab)
    page.wait_for_selector("#portalAuthBox", state="visible")
    page.fill("#authPortalId", uid); page.fill("#authPortalPwd", pw); page.click("#portalAuthForm button[type=submit]")
    page.wait_for_selector("#portalDashboardArea", state="visible", timeout=8000)

def chat_geometry(page):
    return page.evaluate("""()=>{const r=document.getElementById('chatWindow').getBoundingClientRect();
      const q=s=>{const e=document.querySelector(s);if(!e)return null;const b=e.getBoundingClientRect();return {t:b.top,b:b.bottom,l:b.left,r:b.right,h:b.height}};
      return {win:{t:r.top,b:r.bottom,l:r.left,r:r.right,w:r.width,h:r.height},head:q('#chatWindow .chat-head'),input:q('#chatWindow #chatIn'),msgs:q('#chatMsgs'),vw:innerWidth,vh:innerHeight,
      nav:(document.getElementById('topNav')||{getBoundingClientRect:()=>({bottom:0})}).getBoundingClientRect().bottom}}""")

def clicks(pg, sel, n):
    for _ in range(n):
        if pg.is_disabled(sel): break
        pg.click(sel)

def inside(g, name=""):
    w = g["win"]; ok = w["t"] >= g["nav"] - 1 and w["l"] >= 0 and w["r"] <= g["vw"] + 0.5 and w["b"] <= g["vh"] + 0.5
    ok = ok and g["head"]["t"] >= g["nav"] - 1 and g["input"]["b"] <= g["vh"] + 0.5 and g["input"]["t"] >= 0
    return ok

with sync_playwright() as pw:
    executable = os.environ.get("JVM_CHROMIUM_EXECUTABLE")
    br = pw.chromium.launch(executable_path=executable, args=["--no-sandbox"]) if executable else pw.chromium.launch()
    errs = []
    def newpage(viewport):
        ctx = br.new_context(viewport=viewport)
        ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(B) else r.abort())  # sandbox/offline: skip external images & fonts
        return ctx.new_page()
    # ---------- chatbot window at several viewports and zoom levels
    for (vw, vh, label) in [(1366, 768, "laptop"), (1920, 1080, "desktop"), (390, 740, "phone"), (800, 500, "short")]:
        pg = newpage(viewport={"width": vw, "height": vh}); pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(B + "/pages/portal.html", wait_until="domcontentloaded"); pg.click("#chatBtn", force=True); pg.wait_for_timeout(700)
        g = chat_geometry(pg)
        check(f"[{label}] compact default size", g["win"]["w"] <= 360 + 1 and g["win"]["h"] <= 470, g["win"])
        check(f"[{label}] default fully on-screen", inside(g), g)
        pg.evaluate("window.scrollTo(0, 600)"); pg.wait_for_timeout(200); check(f"[{label}] still on-screen after page scroll", inside(chat_geometry(pg)))
        clicks(pg, "#chatZoomIn", 8)
        g = chat_geometry(pg); check(f"[{label}] max zoom fits screen", inside(g), g); check(f"[{label}] zoom label", pg.inner_text("#chatZoomLvl") == "160%")
        check(f"[{label}] zoom-in disabled at max", pg.is_disabled("#chatZoomIn"))
        check(f"[{label}] controls visible at max zoom", g["head"]["h"] > 20 and g["input"]["b"] <= g["vh"])
        clicks(pg, "#chatZoomOut", 12)
        g = chat_geometry(pg); check(f"[{label}] min zoom fits", inside(g), g); check(f"[{label}] zoom label min", pg.inner_text("#chatZoomLvl") == "70%")
        pg.click("#chatZoomLvl"); g = chat_geometry(pg); check(f"[{label}] reset restores 100%", pg.inner_text("#chatZoomLvl") == "100%" and g["win"]["w"] <= 361 and inside(g))
        # drag to far top-left and bottom-right: must stay clamped
        hb = pg.locator("#chatWindow .chat-head").bounding_box()
        pg.mouse.move(hb["x"] + 40, hb["y"] + 10); pg.mouse.down(); pg.mouse.move(-500, -500, steps=5); pg.mouse.up()
        g = chat_geometry(pg); check(f"[{label}] drag top-left clamped (not behind title bar)", inside(g), g)
        hb = pg.locator("#chatWindow .chat-head").bounding_box()
        pg.mouse.move(hb["x"] + 40, hb["y"] + 10); pg.mouse.down(); pg.mouse.move(5000, 5000, steps=5); pg.mouse.up()
        g = chat_geometry(pg); check(f"[{label}] drag bottom-right clamped", inside(g), g)
        pg.click("#chatResetBtn"); check(f"[{label}] reset restores position", inside(chat_geometry(pg)))
        pg.click("#chatSizeBtn"); pg.wait_for_timeout(400); check(f"[{label}] maximize fits", inside(chat_geometry(pg)), chat_geometry(pg)); pg.click("#chatSizeBtn")
        clicks(pg, "#chatZoomIn", 8)
        pg.set_viewport_size({"width": max(vw // 2, 360), "height": max(vh // 2, 380)}); pg.wait_for_timeout(300)
        g = chat_geometry(pg); check(f"[{label}] refit after window shrink (browser-zoom-like)", inside(g), g)
        pg.close()
    # ---------- chatbot conversation
    pg = newpage(viewport={"width": 1280, "height": 800}); pg.goto(B + "/pages/portal.html", wait_until="domcontentloaded"); pg.click("#chatBtn", force=True)
    pg.fill("#chatIn", "When is the next holiday?"); pg.press("#chatIn", "Enter"); pg.wait_for_function("document.querySelectorAll('#chatMsgs .msg').length>=3", timeout=6000); pg.wait_for_timeout(500)
    check("chat UI replies with calendar answer", "holiday" in pg.inner_text("#chatMsgs").lower())
    pg.fill("#chatIn", "I forgot my password"); pg.press("#chatIn", "Enter"); pg.wait_for_timeout(1200)
    check("chat UI login-help", "cannot reveal" in pg.inner_text("#chatMsgs").lower())
    pg.close()
    # ---------- v10: fee estimator + working days through the real chat UI
    pg = newpage(viewport={"width": 1280, "height": 800}); pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(B + "/pages/admissions.html", wait_until="domcontentloaded"); pg.click("#chatBtn", force=True); pg.wait_for_timeout(600)
    def ask(text):
        n = pg.evaluate("document.querySelectorAll('#chatMsgs .msg.bot').length")
        pg.fill("#chatIn", text); pg.press("#chatIn", "Enter")
        pg.wait_for_function("(n)=>{const b=document.querySelectorAll('#chatMsgs .msg.bot');return b.length>n && !b[b.length-1].querySelector('.typing')}", arg=n, timeout=8000)
        return pg.evaluate("document.querySelector('#chatMsgs .msg.bot:last-child').innerText")
    def tap(label):
        n = pg.evaluate("document.querySelectorAll('#chatMsgs .msg.bot').length")
        pg.locator("#chatMsgs .msg.bot:last-child .chat-suggest", has_text=label).first.click()
        pg.wait_for_function("(n)=>{const b=document.querySelectorAll('#chatMsgs .msg.bot');return b.length>n && !b[b.length-1].querySelector('.typing')}", arg=n, timeout=8000)
        return pg.evaluate("document.querySelector('#chatMsgs .msg.bot:last-child').innerText")
    t = ask("estimate fees for class 11"); check("UI fee estimate asks for the stream with tap buttons", "stream" in t.lower() and pg.locator("#chatMsgs .msg.bot:last-child .chat-suggest").count() >= 3, t[:200])
    tap("Science"); tap("New admission"); tap("Zone 1"); t = tap("Annual")
    check("UI fee estimate final answer (Class XI Science, new, zone 1, annual)", "1,19,698" in t and "Estimated total" in t, t[:300])
    t = ask("Class 5 fees"); check("class-level fee question is answered by the estimator, not the admissions shortcut", "existing jvm student" in t.lower(), t[:200])
    t = tap("Skip"); check("UI skip-to-defaults gives Class 5 estimate", "13,755" in t, t[:200])
    t = ask("how many working days are in this month?"); check("UI working days answer", "working days" in t.lower() and "holiday" in t.lower() and "Sundays" in t, t[:200])
    t = ask("how many holidays in november"); check("UI holidays in a named month", "10 holidays" in t and "Deepawali" in t, t[:250])
    t = ask("make me a sandwich"); check("UI unknown prompt gives helpful reply", "did not understand" in t.lower() and pg.locator("#chatMsgs .msg.bot:last-child .chat-suggest").count() >= 3, t[:200])
    t = ask("Class 11 bus fee"); check("existing bus-fee answer is unchanged (admissions shortcut)", "approximate batches" in t.lower() and "route estimates" in t.lower() or "Zone 1" in t, t[:200])
    pg.close()
    # ---------- student portal + payment through the real UI
    pg = newpage(viewport={"width": 1280, "height": 900}); pg.on("pageerror", lambda e: errs.append(str(e)))
    login(pg, "student", "JVM/2026/1002", "pass@1002")
    pg.wait_for_selector("#feePanelInner", timeout=6000)
    txt = pg.inner_text("#studentFeePanel"); check("fee panel shows totals/due dates/history", "Total fees" in txt and "15-Oct-2026" in txt and "Payment history" in txt, txt[:200])
    due0 = pg.locator("#studentFeePanel button.btn-gold").count() > 0
    if due0:
        pg.click("#studentFeePanel button.btn-gold"); pg.wait_for_selector("#payOkBtn", state="visible"); check("demo banner visible", "DEMO" in pg.inner_text("#payModeBanner"))
        pg.click("#payOkBtn"); pg.wait_for_selector("#paymentReceiptArea a", timeout=6000)
        check("confirmed receipt shown", "Payment confirmed" in pg.inner_text("#paymentReceiptArea"))
        pg.click("text=Done"); pg.wait_for_timeout(600)
        t2 = pg.inner_text("#studentFeePanel"); check("panel updated after payment", "(DEMO)" in t2 and pg.locator("#studentFeePanel button.btn-gold").count() == 0, t2[:300])
    else:
        print("note: student 1004 had no dues in this DB copy")
    pg.click("text=Logout"); pg.wait_for_selector("#portalAuthBox", state="visible"); check("logout returns to sign-in", True)
    pg.close()
    # ---------- faculty / admin portals render
    for tab, uid, pw, expect in [("teacher", "TCH/101", "teacher@101", "Dr. B. K. Upadhyay"), ("master", "DHIRAJ4595", "DHIRAJ4595", "Master Administration")]:
        pg = newpage(viewport={"width": 1280, "height": 900}); pg.on("pageerror", lambda e: errs.append(str(e)))
        login(pg, tab, uid, pw); pg.wait_for_timeout(800); body = pg.inner_text("#portalDashboardArea")
        check(f"{tab} dashboard loads", expect in body, body[:180])
        check(f"{tab} sees no fee-payment panel", "Pay " not in body or tab == "master")
        if tab == "teacher":
            # The chatbot must use the session's real faculty name and show only assigned student roster IDs.
            pg.click("#chatBtn", force=True)
            pg.fill("#chatIn", "View My Class"); pg.press("#chatIn", "Enter")
            pg.wait_for_function("document.querySelector('#chatMsgs')?.innerText.includes('Your assigned class roster')", timeout=8000)
            roster_text = pg.inner_text("#chatMsgs")
            check("teacher chatbot shows assigned class roster", "Student ID:" in roster_text and "authorized student record" in roster_text.lower(), roster_text[:200])
            check("teacher chatbot does not replace faculty name with a number placeholder", "Faculty account 101" not in roster_text and "Dr. B. K. Upadhyay" in body)
        pg.close()
    # ---------- navigation across pages
    pg = newpage(viewport={"width": 390, "height": 800}); pg.on("pageerror", lambda e: errs.append(str(e)))
    for u in ["index", "news", "academics", "campus", "admissions", "portal", "contact"]:
        r = pg.goto(B + f"/pages/{u}.html", wait_until="domcontentloaded"); check("page loads " + u, r.status == 200)
        check("no horizontal overflow on phone " + u, pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 2"))
    check("no JS page errors", not errs, errs[:3])
    br.close()
print("\n%d failures" % len(FAILS), FAILS); sys.exit(1 if FAILS else 0)
