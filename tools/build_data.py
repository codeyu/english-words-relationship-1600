# -*- coding: utf-8 -*-
"""把 json/*.json 打包成 app/data.js（浏览器用 <script> 直接读，file:// 下也能打开）。
用法：python tools/build_data.py
"""
import io, json, os, re, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC  = os.path.join(ROOT, "json")
OUT  = os.path.join(ROOT, "app", "data.js")

def page_no(path):
    m = re.search(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else 9999

def main():
    files = sorted(glob.glob(os.path.join(SRC, "*.json")), key=page_no)
    pages, seen = [], {}
    for f in files:
        with io.open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        name = os.path.splitext(os.path.basename(f))[0]
        no = page_no(f)
        for w in d.get("words", []):
            seen.setdefault(w["id"].lower(), 0)
            seen[w["id"].lower()] += 1
        pages.append({
            "file": os.path.basename(f),
            "no": no,
            "title": "第 %d 单元" % no,
            "source": d.get("source", name),
            "clusters": d.get("clusters", []),
            "words": d.get("words", []),
            "edges": d.get("edges", []),
        })
    body = json.dumps(pages, ensure_ascii=False, separators=(",", ":"))
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("// 由 tools/build_data.py 从 json/*.json 生成，请勿手改\n")
        fh.write("window.WC_PAGES = %s;\n" % body)
    print("%d pages, %d unique words -> %s (%.0f KB)"
          % (len(pages), len(seen), OUT, os.path.getsize(OUT) / 1024))

if __name__ == "__main__":
    main()
