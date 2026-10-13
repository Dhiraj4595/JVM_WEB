"""Regenerate pages/*.html from index.html. Run after editing index.html:  python build_pages.py"""
import pathlib
root = pathlib.Path(__file__).parent
src = (root / "index.html").read_text(encoding="utf-8")
pages = {"index": "home", "news": "news", "academics": "academics", "campus": "campus",
         "admissions": "admissions", "portal": "portal", "contact": "contact"}
(root / "pages").mkdir(exist_ok=True)
for name, view in pages.items():
    out = src.replace("window.JVM_VIEW='home'", "window.JVM_VIEW='%s'" % view, 1)
    (root / "pages" / (name + ".html")).write_text(out, encoding="utf-8")
    print("wrote pages/%s.html (%s)" % (name, view))
