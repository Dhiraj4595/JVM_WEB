JVM SHYAMALI WEBSITE — AUDITED MULTI-PAGE BUNDLE
================================================

WHAT IS INCLUDED
- index.html: primary site with section-view routing.
- pages/: separate Home, News & Achievements, Academics, Campus, Admissions & Fees, Portal, and Contact pages.
- app_server.py: local same-origin website server, authentication API, role checks, persistent SQLite storage, contact-enquiry store, and audit log.
- private_data/: private seed data and the pre-initialized SQLite database. The seed copy is retained as a recovery source; do not publish it.
- Original jvm_shyamali_portal.html and the originally supplied JSON files are preserved in this archive. The main index was enhanced, while the original portal HTML stays unchanged.

START THE WEBSITE LOCALLY
1. Install Python 3.10 or newer.
2. Open a terminal in this folder (the folder containing app_server.py).
3. Run: python app_server.py --port 8000
4. Open http://127.0.0.1:8000 in the browser.
5. Keep the terminal/server running while using the portal. Use the Log Out control when finished.

IMPORTANT: Do NOT use `python -m http.server`, a static file host, or a `file://` URL for this bundle. They bypass the allowlisted file server and authentication API. The included credentials, original files, private seed records, and database must never be exposed through a public static web server.

SAVED CHANGES / PERMISSIONS
- A successful teacher/master “Save & Commit Changes” request is validated by app_server.py and committed to private_data/school.sqlite3 before the website reports success.
- Data persists across page reloads and server restarts. Back up the SQLite database regularly and keep backups offline/access controlled.
- Master account: all seeded student records, edit controls, enquiries and audit log.
- Academic teacher accounts: only their configured class/subject scope; edits are restricted to academic marks, attendance and teacher remarks.
- Sports, arts and music faculty: the supplied broad-wing assignments map to an activity-only workspace (per the source teacher records); edits can change extracurricular activity lists only, never academic marks, attendance, fees or parent contacts.
- Any unmatched teacher assignment fails closed rather than silently granting school-wide access.
- Student accounts (also used by parents; no separate parent login): read-only access to the one record linked to that account. The API enforces this server-side; it does not depend only on client-side hiding.
- The Contact form stores enquiries in the database. It does NOT send email; the master account can review enquiries in the portal.
- Fee payment: see README.md (v7). Payments run in clearly labelled DEMO mode unless a Razorpay account is configured; a payment is shown as paid only after the server records it.

OPTIONAL FREE / OPEN-SOURCE CHATBOT
- Install Ollama from its official source and pull a model, for example: `ollama pull llama3.2`.
- Run Ollama locally and allow the exact website origin in its browser CORS/origin configuration (e.g. http://127.0.0.1:8000). If you open the site via localhost instead, allow that exact origin as well.
- The chatbot attempts the local Ollama API on port 11434. If the model is stopped or unavailable, it falls back to its built-in school FAQ. The chatbot must not be treated as an official source for dates, fees or results.

SECURITY AND PRODUCTION CHECKLIST
- The server binds to 127.0.0.1 by default. Do not expose it directly to the Internet. Public deployment needs an institution-managed production server, HTTPS, secure reverse-proxy configuration, restricted administration, monitoring, encrypted/offsite backups, recovery drills and a school-approved privacy notice.
- Before real-world use, rotate all supplied/demo account passwords to unique strong secrets and distribute them securely. These supplied records are a development dataset; verify permission to process and publish all personal/student information.
- Change default/demo credentials before launch. Review teacher assignments with school administration: the server derives teacher scope from the supplied class/subject assignments, so those records should be checked for least privilege.
- The SQLite file lives under private_data and is not exposed by app_server.py's static file allowlist. File permissions on the host must still restrict it to the account running the server.
- The local session store is held in server memory and sessions expire after eight hours; restarting the service signs users out but does not discard committed records.
- No external payment gateway or email delivery is configured; the UI states this rather than pretending either action succeeded.

DATA PRESERVATION
- The original uploaded website and supplied JSON files are included unmodified for recovery. The new seeded database stores all 458 student records with credential fields removed from record payloads; account secrets are stored as salted PBKDF2-HMAC-SHA256 hashes.
- Previous browser-only edits are imported only after master sign-in. The old browser copy is removed only when the server confirms that the import had no skipped records/fields. If anything is skipped, the old browser copy remains for review.
- Keep a separate copy of the supplied original ZIP and a scheduled backup of private_data/school.sqlite3.

OFFICIAL SCHOOL REFERENCE LINKS USED FOR NEWS
- https://jvmshyamali.com/home
- https://www.jvmshyamali.com/achievement_list
Verify notices and results on the official school site before treating them as current or official.


SEE CHANGES_V3.txt for the latest changes (classes 1-12, per-class timetable, faculty, animated theme, AI chatbot with links).
Login IDs/passwords: private_data/seed_master.json, seed_teachers.json, seed_students.json.


PUBLISHING ONLINE (V5)
The website (HTML) and the backend (app_server.py) are ONE program: the pages call /api/... on their own address, so
they must be hosted together. A static-only host (GitHub Pages, Netlify, plain file hosting) will NOT work.
1. Use a host that runs Python or Docker: Render (render.yaml included), Railway, Fly.io, or a VPS.
   Start command: python app_server.py   (no pip install needed; see requirements.txt / Procfile / Dockerfile)
2. Set environment variables on the host:
   JVM_TRUST_PROXY=1                 (host provides HTTPS in front of the app)
   JVM_DB_PATH=/data/school.sqlite3  (a persistent disk mounted at /data - otherwise edits are lost on redeploy)
   ANTHROPIC_API_KEY=...             (optional, for free-form chatbot answers)
   The host supplies PORT itself.
3. The first start on an empty disk builds the database from private_data/seed_*.json and takes about 2 minutes.
4. Change every demo password (private_data/seed_*.json) BEFORE going public; keep the repository private.

HOSTING / CORS (V8)
See HOSTING.md. Same-address hosting (default) needs no CORS. For pages and backend on different addresses set
JVM_ALLOWED_ORIGINS on the backend and JVM_API_BASE in jvm-config.js. Use set_password.py to replace demo passwords.
