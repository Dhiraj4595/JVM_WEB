# Putting the JVM Shyamali website live

## What your friend meant: "CORS"
CORS (Cross-Origin Resource Sharing) is the browser rule that decides whether a web page on address A
may talk to a backend on address B. It only matters if the pages and the backend are on **different**
addresses. In this project the backend (`app_server.py`) serves the pages **and** the `/api/...` calls
itself, so on a normal single-service deployment **no CORS is needed and nothing is missing**.

Support for the separate-addresses case has now been added anyway, switched off by default:

| Setup | What to do |
|---|---|
| **A. One service (recommended)** | Deploy the whole folder as one Python/Docker service. Leave `jvm-config.js` and `JVM_ALLOWED_ORIGINS` alone. |
| **B. Pages and backend on different addresses** | Set `JVM_ALLOWED_ORIGINS` on the backend and `JVM_API_BASE` in `jvm-config.js` (see below). |

A static-only host (GitHub Pages, Netlify, Firebase Hosting alone) cannot run `app_server.py`, so it can
only be used for the pages in setup B, with the backend running somewhere else.

## Setup A - one service (Render, Railway, Fly.io, VPS)
1. Put this folder in a **private** Git repository (it contains `private_data/` with logins and student data).
2. Render: *New > Blueprint*, pick the repo (`render.yaml` and `Dockerfile` are included). Any Docker host works the same way.
3. Environment variables (already in `render.yaml`):
   - `JVM_TRUST_PROXY=1` - the host puts HTTPS in front of the app
   - `JVM_DB_PATH=/data/school.sqlite3` - on a **persistent disk** mounted at `/data`, otherwise edits are lost on every redeploy
   - optional: `ANTHROPIC_API_KEY` (free-form chatbot answers), `JVM_PAYMENT_MODE` / `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` (see README.md)
   - the host supplies `PORT` itself
4. First start on an empty disk copies the pre-built database (about a second).
5. Open `https://<your-service-url>/healthz` - it should show `{"ok": true}`, then open the site and log in.
6. **Change the demo passwords before announcing the site** (see "Before going public").
7. Point your own domain at the service from the host's dashboard (HTTPS is provided automatically).

## Setup B - pages and backend on different addresses
Example: pages at `https://www.yourschool.in`, backend at `https://api.yourschool.in`.
1. Backend: set `JVM_ALLOWED_ORIGINS=https://www.yourschool.in` (exact origin, comma-separate several, no trailing slash, no `*`).
2. Pages: edit the first line of `jvm-config.js` to `window.JVM_API_BASE = "https://api.yourschool.in";`
3. Upload to the page host everything the site needs **except** `private_data/`, `*.py`, tests and Docker files:
   `index.html`, `pages/`, `jvm-config.js`, `images/`, and the JSON files `faculty.json`, `timetable.json`,
   `achievements.json`, and `official-sources.json`. Do **not** publish `section-roster.json` or `original-student-records.json`: they contain student-level details and are not required by the public pages.
4. Both addresses must be **HTTPS**. Login cookies become `SameSite=None; Secure` for the allow-listed origin.
5. Browsers are tightening third-party cookies (Safari already blocks them across unrelated domains). Use
   subdomains of the **same** registrable domain as above (`www.` + `api.`), not two unrelated domains,
   or logins may silently fail for some visitors. If in doubt, use Setup A.

## Before going public (mandatory)
- Demo logins are in `private_data/seed_*.json`. Change each real account's password on the server:
  `python set_password.py master DHIRAJ4595` (also `teacher TCH/101`, `student JVM/2026/1001`).
  On a host shell set `JVM_DB_PATH=/data/school.sqlite3` first. Then **delete the plaintext passwords from the seed
  files in your repo** (the database holds only salted hashes). Seeds are only used if the database is missing.
- Keep the repository private; verify you may publish/process the student and staff data.
- Back up `/data/school.sqlite3` regularly. Sessions are in memory - a restart signs everyone out but loses no records.
- Payments run in labelled DEMO mode unless Razorpay keys are set; the contact form stores enquiries (no email is sent).

## Checking it works
```
JVM_DB_PATH=/tmp/t.sqlite3 JVM_ALLOWED_ORIGINS=https://www.example-school.in python app_server.py --port 8124
python tests_api.py 8124
python tests_cors.py 8124 https://www.example-school.in
```
