# -*- coding: utf-8 -*-
"""把 json/*.json 打包成 app/data.js（浏览器用 <script> 直接读，file:// 下也能打开）。
用法：python tools/build_data.py
     python tools/build_data.py --src out --out app/data.js   # 预览自动生成的词簇
"""
import argparse, io, json, os, re, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC  = os.path.join(ROOT, "json")
OUT  = os.path.join(ROOT, "app", "data.js")

def page_no(path):
    m = re.search(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else 9999

def main():
    ap = argparse.ArgumentParser(description="把词簇 JSON 打包成 app/data.js")
    ap.add_argument("--src", default=SRC, help="装着 pageN.json 的目录")
    ap.add_argument("--out", default=OUT, help="输出的 data.js")
    ap.add_argument("--unit", default="第 %d 单元", help="单元标题模板")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.src, "*.json")), key=page_no)
    pages, seen = [], {}
    for f in files:
        with io.open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict) or not isinstance(d.get("words"), list):
            continue                     # 跳过 report.json 之类的非词表文件
        name = os.path.splitext(os.path.basename(f))[0]
        no = page_no(f)
        for w in d.get("words", []):
            seen.setdefault(w["id"].lower(), 0)
            seen[w["id"].lower()] += 1
        pages.append({
            "file": os.path.basename(f),
            "no": no,
            "title": args.unit % no,
            "source": d.get("source", name),
            "clusters": d.get("clusters", []),
            "words": d.get("words", []),
            "edges": d.get("edges", []),
        })
    body = json.dumps(pages, ensure_ascii=False, separators=(",", ":"))
    with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("// 由 tools/build_data.py 从 %s 生成" % args.src + "，请勿手改\n")
        fh.write("window.WC_PAGES = %s;\n" % body)
    print("%d units, %d unique words -> %s (%.0f KB)"
          % (len(pages), len(seen), args.out, os.path.getsize(args.out) / 1024))

if __name__ == "__main__":
    main()
