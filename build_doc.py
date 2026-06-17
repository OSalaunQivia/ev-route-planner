"""Convertit la doc Markdown en HTML stylé (prêt PDF/Word). Sans dépendance lourde."""
from pathlib import Path

import markdown

SRC = Path("Qivia_EV_Documentation.md")
OUT = Path("Qivia_EV_Documentation.html")

CSS = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
  color: #1A1F2B; line-height: 1.55; font-size: 11pt;
  max-width: 820px; margin: 0 auto; padding: 24px;
}
h1 { font-size: 24pt; color: #0B111C; border-bottom: 3px solid #5FFFA7;
     padding-bottom: 8px; margin-top: 0; }
h2 { font-size: 15pt; color: #0B111C; margin-top: 26px;
     border-bottom: 1px solid #D6DBE3; padding-bottom: 4px; }
h3 { font-size: 12.5pt; color: #18324A; margin-top: 18px; }
h2, h3 { page-break-after: avoid; }
table, figure, pre { page-break-inside: avoid; }
a { color: #0B7C53; text-decoration: none; }
code { font-family: "SF Mono", Menlo, monospace; font-size: 9.5pt;
       background: #F1F4F8; padding: 1px 4px; border-radius: 3px; }
pre { background: #0B111C; color: #E8EDF4; padding: 12px 14px; border-radius: 6px;
      overflow-x: auto; font-size: 9pt; line-height: 1.45; }
pre code { background: none; color: inherit; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 9.5pt; }
th, td { border: 1px solid #D6DBE3; padding: 6px 9px; text-align: left;
         vertical-align: top; }
th { background: #0B111C; color: #fff; font-weight: 600; }
tr:nth-child(even) td { background: #F7F9FB; }
blockquote { border-left: 4px solid #FBBF24; background: #FFFBEB; margin: 12px 0;
             padding: 8px 14px; color: #5A4A00; }
hr { border: none; border-top: 1px solid #D6DBE3; margin: 22px 0; }
strong { color: #0B111C; }
"""

html_body = markdown.markdown(
    SRC.read_text(encoding="utf-8"),
    extensions=["tables", "fenced_code", "sane_lists", "toc", "attr_list"],
)
doc = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Qivia EV — Documentation technique</title>
<style>{CSS}</style></head>
<body>{html_body}</body></html>"""
OUT.write_text(doc, encoding="utf-8")
print(f"HTML écrit : {OUT}  ({len(doc)} octets)")
