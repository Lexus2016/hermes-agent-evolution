#!/usr/bin/env python3
"""Convert marketing analysis markdown to PDF with weasyprint."""
import markdown
from weasyprint import HTML
import re

# Read markdown
with open('/root/hermes-evolution-work/marketing-analysis-6-ideas.md', 'r', encoding='utf-8') as f:
    md_content = f.read()

# Convert markdown to HTML
html_body = markdown.markdown(md_content, extensions=['tables', 'fenced_code', 'toc'])

# Full HTML with dark professional styling
html_full = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<style>
@page {{
    size: A4;
    margin: 2cm 1.5cm;
    @bottom-center {{
        content: counter(page) " / " counter(pages);
        font-size: 9pt;
        color: #888;
    }}
}}
body {{
    font-family: 'DejaVu Sans', 'Helvetica', sans-serif;
    font-size: 10.5pt;
    line-height: 1.6;
    color: #1a1a2e;
    max-width: 100%;
}}
h1 {{
    color: #0f3460;
    font-size: 20pt;
    border-bottom: 3px solid #0f3460;
    padding-bottom: 10px;
    margin-top: 0;
}}
h2 {{
    color: #16213e;
    font-size: 14pt;
    border-bottom: 1px solid #ccc;
    padding-bottom: 5px;
    margin-top: 25px;
    page-break-after: avoid;
}}
h3 {{
    color: #0f3460;
    font-size: 11.5pt;
    margin-top: 18px;
    page-break-after: avoid;
}}
p {{
    margin: 8px 0;
    text-align: justify;
}}
strong {{
    color: #0f3460;
}}
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
    font-size: 9pt;
    page-break-inside: auto;
}}
th {{
    background-color: #0f3460;
    color: white;
    padding: 8px 10px;
    text-align: left;
    border: 1px solid #0f3460;
}}
td {{
    border: 1px solid #ddd;
    padding: 6px 10px;
    vertical-align: top;
}}
tr:nth-child(even) {{
    background-color: #f8f9fa;
}}
tr {{
    page-break-inside: avoid;
}}
a {{
    color: #1976d2;
    text-decoration: none;
}}
hr {{
    border: none;
    border-top: 1px solid #ccc;
    margin: 20px 0;
}}
ul, ol {{
    margin: 8px 0;
    padding-left: 20px;
}}
li {{
    margin: 4px 0;
}}
code {{
    background-color: #f0f0f0;
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 9.5pt;
}}
</style>
</head>
<body>
{html_body}
</body>
</html>"""

# Generate PDF
output_path = '/root/reports/marketing-analysis-6-ideas.pdf'
import os
os.makedirs('/root/reports', exist_ok=True)

HTML(string=html_full).write_pdf(output_path)
print(f"PDF saved: {output_path}")

# Check file size
size = os.path.getsize(output_path)
print(f"Size: {size} bytes ({size/1024:.1f} KB)")