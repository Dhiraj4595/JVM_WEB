# JVM Shyamali website — v7 upgrade

## Run
```
python app_server.py --port 8000      # Python 3.10+, no extra packages
```
Open http://127.0.0.1:8000 (do NOT use a static file server or file://). Existing accounts, teacher assignments and student records are unchanged (`private_data/`). The SQLite DB gets two new tables (`fee_orders`, `fee_payments`) automatically on first start — back up `private_data/school.sqlite3` before upgrading.

## Fee payments (Portal → Student/Parent login → "Fee Payment")
Shows total fees, paid, outstanding, remaining, instalment due dates, payment history and receipts. The server computes every amount; the browser can't set it.
Flow: `create-order` → gateway → `confirm`. Balance, history, receipt and dashboards (student, master) update only after the server records the payment, in one database transaction. Re-clicking/retrying returns the same receipt (no double charge); one open order per student; orders expire after 30 min; failed/declined payments change nothing. The old instant-pay endpoint is disabled.

Modes (`JVM_PAYMENT_MODE`):
- `demo` (default) — **no gateway, no real money.** Clearly labelled in the UI; receipts are watermarked DEMO.
- `razorpay` — set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`. Server creates the order and verifies the Razorpay HMAC signature before recording anything. **Not tested against live Razorpay** (no credentials/network here) — test with Razorpay test keys first. If keys are missing it falls back to demo.
- `disabled` — hides payment; office payment only.

Payments recorded before this upgrade are shown as "school office record" (derived from the existing fee fields, no invented transaction IDs).

## Chatbot
Knowledge base (server-side, `chat_kb`) covers admissions, fees, due dates, payment status/receipts (own account only after sign-in), calendar/holidays/exams/PTM (read live from the site's own calendar list), timetables, timings, teachers, achievements, events, contact, login and technical help, with a fallback. It refuses passwords, other students' data and staff personal contacts. Optional AI: `ANTHROPIC_API_KEY` or `JVM_OLLAMA_MODEL`.
Window: compact 340×460; − / % / + zoom (70–160%), % or ↺ resets zoom and position, drag by the header (double-click header re-docks), ⤢ maximise. Always clamped inside the viewport below the site header.

## Tests (run on a COPY of the DB — they create demo payments)
```
JVM_DB_PATH=/tmp/t.sqlite3 python app_server.py --port 8123
python tests_api.py 8123     # 50+ API/permission/payment/chatbot checks
python tests_ui.py 8123      # needs: pip install playwright && playwright install chromium
```
Both pass in the delivery environment. After editing `index.html` run `python build_pages.py`.

## Known limitations
- Razorpay live mode untested (see above); no webhook — if a customer pays but closes the browser before the confirm call, the office must reconcile manually.
- Receipts are HTML (print/save as PDF from the browser).
- Sessions are in server memory (restart = sign in again). Use HTTPS and change all demo passwords before going public (see README_DEPLOYMENT.txt).
- Homework/live attendance feeds don't exist in the data, so the chatbot points to the diary/portal instead of inventing answers.
- Tested in headless Chromium only (desktop, 390px phone, short-screen viewports); not on real iOS/Android devices.

## Update v9 (content update)
- Calendar data (`JVM_ACADEMIC_EVENTS` in index.html) replaced with the 2026-27 Holiday List and School Activity Calendar; added a "Class group" selector. Run `python build_pages.py` after any edit to index.html.
- New Leadership section (Chairman, Principal) on the About page; logo/favicon; campus photos; three news cards with newspaper clippings in `images/`.
- "Latest Notices" ticker now shows text-only notices. No backend, API, database or deployment files were changed.

## Supplied photo and faculty update (October 2026)

The public faculty directory in `faculty.json` contains the names, roles and subjects supplied for this update. Only the clearly labelled portraits for Mr. L. N. Pattanaik and Mr. Anshu Kumar Pandey are assigned to those profiles; the other cards use initials until matched portraits are supplied. The Anshu portrait asset is cropped from the supplied profile screenshot so the surrounding phone/status interface is not published.

Supplied genuine school photos now populate the campus, library, lab, sports, cultural-event, gallery, and NEET achievements sections. News and result entries are based on the supplied October 2026 research PDF and link to the listed school/news sources. The original `principal.png` and `chairman.png` files are unchanged. Teacher/student account IDs, passwords, database records, and server-side access rules were not changed.

## Update v10 — chatbot: public fee estimate, working days / holidays, proper "not understood" reply
Changed files: `app_server.py` (chatbot only), `index.html` + regenerated `pages/*.html` (chat window only), `tests_api.py`, `tests_ui.py`. No database, login, payment, CORS or deployment files changed; no new dependencies.

**1. Fee estimate for any class (public, no login).** Type e.g. "estimate fees for class 11" or "Class 5 fees". The bot asks, one question at a time with tap-buttons: class → stream/specialty (Class XI/XII only) → existing student or new admission → bus zone → quarterly or annual, then shows the itemised estimate (tuition, bus, Dev & Lab Fund, admission fee, 5% annual rebate). You can also give everything in one sentence, or tap "Skip – use defaults". Figures and formula are the same as the Fee Structure table and Fee Estimator on the Admissions page (`FEE_TIERS`, `FEE_BUS` in `app_server.py` — edit them there when fees change). Nothing is invented: the table has no Class XI Commerce/Arts tuition and no Class XII admission fee, so the bot says so instead of guessing. "my fees", receipts, payments and due dates still go to the existing signed-in flow. The server keeps no per-visitor state (the page sends the partial answers back with each message).

**2. Working days and holidays in a month.** Ask "how many working days are in this month?", "holidays in November", "working days next month", "working days left this month", "working days in May". Counted from the site's own `JVM_ACADEMIC_EVENTS` holiday entries (multi-day breaks use their `end` date). School week = Monday–Saturday, Sundays off; every day is exactly one of working / holiday / Sunday and the answer shows that check. Months outside April 2026 – March 2027 are declined rather than guessed. Class-wise session start dates and exam days are not deducted (stated in the answer).

**3. Unknown prompts.** The old reply "We are working on it." is replaced by a short list of what the chatbot can do, example questions, tap-buttons and the school office number (`FALLBACK`, `FALLBACK_OPTIONS`).

Small related fixes: the holiday list answers now show multi-day breaks as a date range and keep a break that has already started but not finished. New optional setting `JVM_CHAT_LIMIT` (default 30 messages/min per IP, unchanged); the test suite sets 500.

Tests: `tests_api.py` 102 checks and `tests_ui.py` 93 checks pass on a fresh DB copy; `tests_cors.py` passes. Start the test server with `JVM_CHAT_LIMIT=500` (see the file headers).
