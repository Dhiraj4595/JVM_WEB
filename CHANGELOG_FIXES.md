# JVM SMARTVERSE — fixes in this build

## Teacher chatbot student selection and marks editing
- Fixed the natural-language edit parser so subject-only mark updates after selecting a student from **View My Class / Assigned Students** use the currently selected student instead of trying to search for a student using subject words.
- Added handling for filler phrasing such as “subject maths” and “update marks” when resolving the target student.
- Existing server-side authorization and the save-confirmation step remain in place. Teachers can still edit only records assigned to their account and only the fields allowed for their role.

## Chatbot knowledge improvements
- Added targeted answers for report cards, attendance, locating a Student ID/Registration Number, transport guidance, timetables, and campus facilities.
- Prevented generic “report card” questions from being mistaken for academic-calendar questions.
- Prevented a plain “registration number” help request from being misrouted into admissions guidance.
- Updated the fallback suggestions to mention report cards, attendance, transport, and facilities.

## Validation
- `python -m py_compile app_server.py` passes.
- `tests_api.py` passed all existing API/permission/payment/chatbot checks against a copied database after the final routing refinements (0 failures).
- Browser UI tests could not be run here because the Playwright Chromium executable is not installed in the environment.
- Regenerate page copies with `python build_pages.py` after changing `index.html`; this was done for this build.

No student records, teacher assignments, passwords, or database contents were intentionally changed.

## October 2026 — admission response refinement
- Unsupported admission classes (for example, Classes 9 and 10) now receive a direct, link-free message that admissions are not listed for that class, rather than a provisional-test or missing-data fallback.
- Class XI optional-subject questions return a readable stream-specific list from the supplied 2026–27 notice, including separate PCM and PCB options; they no longer display the full admissions summary or redirect links.
- Class XI overview responses use clear headings and separate sections for status, eligibility, streams, fees and dates. The responses explicitly label the 2026–27 notice as a past cycle.
- Added API regression checks for unsupported classes, stream optional subjects and reduced redirect use.
