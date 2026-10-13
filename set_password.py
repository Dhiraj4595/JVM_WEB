"""Change one account's password directly in the SQLite database (run this on the server, or against a copy).

    python set_password.py master DHIRAJ4595
    python set_password.py teacher TCH/101
    python set_password.py student JVM/2026/1001

Uses JVM_DB_PATH if set (e.g. /data/school.sqlite3 on your host), else private_data/school.sqlite3.
Restart is not required; existing sessions of that account stay valid until they expire (8 h) or the server restarts.
Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes - the same format app_server.py uses.
"""
import getpass, os, sqlite3, sys
from pathlib import Path
import app_server as app

if len(sys.argv) != 3 or sys.argv[1] not in ("master", "teacher", "student"):
    sys.exit(__doc__)
role, login = sys.argv[1], sys.argv[2].strip()
if role == "teacher": login = app.norm_teacher_key(login.upper())
else: login = login.upper()
con = sqlite3.connect(app.DB_PATH)
row = con.execute("SELECT 1 FROM users WHERE role=? AND login_id=?", (role, login)).fetchone()
if not row:
    sys.exit(f"No {role} account with id {login!r} in {app.DB_PATH}")
pw = os.environ.get("JVM_NEW_PASSWORD") or getpass.getpass("New password (min 10 chars): ")
if len(pw) < 10:
    sys.exit("Password too short (use at least 10 characters).")
if not os.environ.get("JVM_NEW_PASSWORD") and getpass.getpass("Repeat password: ") != pw:
    sys.exit("Passwords do not match.")
salt, hashed = app.password_hash(pw)
con.execute("UPDATE users SET salt=?, password_hash=? WHERE role=? AND login_id=?", (salt, hashed, role, login))
con.commit(); con.close()
print(f"Password updated for {role} {login} in {app.DB_PATH}")
