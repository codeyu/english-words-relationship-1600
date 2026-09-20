# -*- coding: utf-8 -*-
"""从 oxford-3000-and-5000 仓库抽取牛津 3000 词表，产出建簇用的词表文件和词条详情。

产出：
  data/oxford3000-words.txt    给 build_clusters.py 用的词表（word<TAB>短释义）
  data/oxford3000-freq.txt     CEFR 等级当词频代理
  data/oxford3000-detail.json  音标 / 释义 / 例句 / 等级 / 音频 / 词源词根
  data/oxford3000-roots.json   词根 -> 词，用来生成"同词源"连线

用法：
  python tools/build_oxford.py
  python tools/build_oxford.py --levels a1,a2 --out data
  python tools/build_oxford.py --copy-audio app-oxford/audio   # 把用到的 mp3 拷贝成自包含
"""

import argparse, collections, io, json, os, re, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OX = r"C:\github\oxford-3000-and-5000"

LEVEL_ORDER = {"a1": 0, "a2": 1, "b1": 2, "b2": 3, "c1": 4}
LEVEL_FREQ = {"a1": 16, "a2": 8, "b1": 4, "b2": 2, "c1": 1}
WORD_RE = re.compile(r"^[a-z][a-z\-']*$")

# --------------------------------------------------------------------------
# 词源解析
# --------------------------------------------------------------------------
# word_origin 形如：
#   "late Middle English: from Latin constitut- ‘established, appointed’,
#    from the verb constituere , from con- ‘together’ + statuere ‘set up’."
# 只认「语言名 + 外来词形」和「X ‘释义’」两种，且要求长度>=4，
# 否则会捞到 ad / a / de 这类前缀碎片。
LANGS = ("latin", "greek", "late latin", "medieval latin", "modern latin",
         "ecclesiastical latin", "old french", "french", "old english",
         "middle english", "old norse", "dutch", "middle dutch", "german",
         "italian", "spanish", "sanskrit", "arabic", "portuguese", "norse",
         "provençal", "provencal", "anglo-norman", "anglo-norman french",
         "old high german", "low german", "frankish", "gothic", "celtic",
         "hebrew", "persian", "turkish", "russian", "hindi", "malay",
         "norwegian", "swedish", "danish", "icelandic", "scandinavian")
# 标记词之后才是真正的外来词形；不这么限制的话，
# 叙述里的 "meaning ‘to bother’" 会被当成词根 meant，把无关词拉到一起。
MARKERS = ("from", "via", "+", "based on", "related to", "cognate with")
SKIP = {"the", "a", "an", "verb", "noun", "adjective", "adverb", "stem", "past",
        "participle", "present", "diminutive", "plural", "genitive", "literally",
        "of", "same", "word", "root", "form", "variant", "sense", "use", "used",
        "meaning", "dialect", "spelling", "origin", "unknown", "obscure",
        "probably", "perhaps", "imitative", "expressing", "denoting",
        "originally", "influenced", "alteration", "abbreviation", "compare",
        "ultimately", "later", "earlier", "this", "that", "which", "whose",
        "base", "element", "combining", "comparative", "superlative",
        "feminine", "masculine", "neuter", "singular", "infinitive",
        "second", "third", "another", "shared", "number", "indoeuropean",
        "indo", "european", "germanic", "scandinavian", "unrelated",
        "literal", "figurative", "reduplication", "blend", "shortening"}
# 纯前缀不算词根：contra 会把 control / country / contrast 混成一簇
STOP_ROOT = {"inter", "contra", "super", "trans", "ultra", "intra", "extra",
             "proto", "pseudo", "anti", "auto", "semi", "multi", "micro", "macro"}
RX_TOKEN = re.compile(r"[a-zāēīōūæœ][a-zāēīōūæœ\-]*", re.I)


def norm_root(s):
    s = s.strip().strip("-").lower()
    s = re.sub(r"[^a-zāēīōūæœ]", "", s)
    # 拉丁动词的屈折尾，砍掉才能让 constituere / constitut- 对上
    for suf in ("ere", "are", "ire", "us", "um", "is", "em", "re", "e"):
        if len(s) > 5 and s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s


def roots_of(origin):
    """只取「from / via / + 」这些标记之后的外来词形，跳过语言名和语法术语。"""
    if not origin:
        return []
    text = origin.replace("‘", "'").replace("’", "'")
    # 引号里是英文注解（'below' / 'together'），不是词形，先整段剥掉
    text = re.sub(r"'[^']{1,60}'", " ", text)
    low = text.lower()
    out, i = [], 0
    while i < len(low):
        hit = None
        for m in MARKERS:
            j = low.find(m, i)
            if j >= 0 and (hit is None or j < hit[0]):
                hit = (j, m)
        if not hit:
            break
        pos = hit[0] + len(hit[1])
        rest = low[pos:pos + 90]
        for lang in sorted(LANGS, key=len, reverse=True):   # 先吃掉语言名
            if rest.lstrip().startswith(lang):
                rest = rest.lstrip()[len(lang):]
                break
        for tok in RX_TOKEN.findall(rest):                  # 再跳过语法术语
            if tok in SKIP or tok in LANGS:
                continue
            out.append(norm_root(tok))
            break
        i = pos
    return [r for r in out if len(r) >= 4 and r not in STOP_ROOT and r not in SKIP]


def short_def(text, limit=34):
    """把一句英文释义压成节点标签能放下的短语。"""
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"^(the fact that|the state of|the act of|a person who|"
               r"a thing that|used to say that|used to refer to|"
               r"the process of|an act of)\s+", "", t, flags=re.I)
    t = re.split(r"[;:]|\s+\(", t)[0].strip().rstrip(".")
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0]
    return cut + "…"


# --------------------------------------------------------------------------
def load_index(levels):
    """words-5000.json -> {word: level}，取该词出现过的最低等级。"""
    p = os.path.join(OX, "data", "json", "words-5000.json")
    with io.open(p, encoding="utf-8") as fh:
        rows = json.load(fh)
    lv = {}
    for x in rows:
        w = (x.get("word") or "").strip().lower()
        l = (x.get("level") or "").strip()
        if not WORD_RE.match(w) or l not in LEVEL_ORDER:
            continue
        if w not in lv or LEVEL_ORDER[l] < LEVEL_ORDER[lv[w]]:
            lv[w] = l
    return {w: l for w, l in lv.items() if l in levels}


def load_details(words):
    """details/*.json 按词聚合：同一个词可能有 word_1 / word_2 多个词性文件。"""
    d_dir = os.path.join(OX, "data", "json", "details")
    by_word = collections.defaultdict(list)
    for fn in os.listdir(d_dir):
        if not fn.endswith(".json"):
            continue
        base = re.sub(r"_\d+$", "", fn[:-5]).lower()
        if base in words:
            by_word[base].append(os.path.join(d_dir, fn))

    out = {}
    for w, files in by_word.items():
        senses, pos, ipa_uk, ipa_us, au_uk, au_us, origins, forms = [], [], "", "", "", "", [], []
        for f in sorted(files):
            with io.open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            if d.get("pos"):
                pos.append(d["pos"])
            ph = d.get("phonetics") or {}
            ipa_uk = ipa_uk or (ph.get("uk") or "")
            ipa_us = ipa_us or (ph.get("us") or "")
            au_uk = au_uk or (d.get("uk") or "")
            au_us = au_us or (d.get("us") or "")
            if d.get("word_origin"):
                origins.append(d["word_origin"])
            for fm in (d.get("forms") or []):
                if fm.get("word"):
                    forms.append({"label": fm.get("label") or fm.get("type") or "", "word": fm["word"]})
            for s in (d.get("senses") or [])[:3]:
                if not s.get("def"):
                    continue
                ex = [e.get("text") for e in (s.get("examples") or []) if e.get("text")]
                senses.append({"pos": d.get("pos") or "", "level": s.get("level") or d.get("level") or "",
                               "def": s["def"], "ex": ex[:2]})
        rs = []
        for o in origins:
            rs += roots_of(o)
        out[w] = {
            "pos": sorted(set(pos)), "ipa": {"uk": ipa_uk, "us": ipa_us},
            "audio": {"uk": au_uk, "us": au_us}, "senses": senses[:4],
            "forms": forms[:6], "origin": origins[0] if origins else "",
            "roots": sorted(set(rs)),
        }
    return out


def main():
    ap = argparse.ArgumentParser(description="抽取牛津词表 + 词条详情")
    ap.add_argument("--levels", default="a1,a2,b1,b2", help="要哪些 CEFR 等级")
    ap.add_argument("--out", default=os.path.join(ROOT, "data"), help="输出目录")
    ap.add_argument("--oxford", default=None, help="oxford-3000-and-5000 仓库路径")
    ap.add_argument("--copy-audio", help="把用到的 mp3 拷到这个目录（自包含，约 80MB）")
    ap.add_argument("--js", help="同时写一份 detail.js 到这个目录，给 app 用 <script> 直接读")
    ap.add_argument("--jp", default=os.path.join(ROOT, "data", "oxford3000-jp.json"),
                    help="日文释义（tools/fetch_jp.py 产出）；有就用它当节点标签")
    ap.add_argument("--min-root-words", type=int, default=2, help="一个词根至少连几个词才保留")
    ap.add_argument("--max-root-words", type=int, default=10, help="超过这个数的词根太泛，丢掉")
    args = ap.parse_args()

    global OX
    if args.oxford:
        OX = args.oxford
    if not os.path.isdir(OX):
        sys.exit("找不到牛津仓库：%s" % OX)
    levels = set(x.strip() for x in args.levels.split(","))
    os.makedirs(args.out, exist_ok=True)

    lv = load_index(levels)
    print("等级 %s -> %d 个词" % (",".join(sorted(levels)), len(lv)))
    det = load_details(set(lv))
    print("详情命中 %d 个词 (%.1f%%)" % (len(det), 100 * len(det) / max(len(lv), 1)))

    # 词根表：只留连接 2~10 个词的根，太泛或太孤立的都没用
    root2w = collections.defaultdict(set)
    for w, d in det.items():
        for r in d["roots"]:
            root2w[r].add(w)
    roots = {r: sorted(ws) for r, ws in root2w.items()
             if args.min_root_words <= len(ws) <= args.max_root_words}
    print("词根 %d 个，覆盖 %d 个词" % (len(roots), len({w for ws in roots.values() for w in ws})))

    jp = {}
    if args.jp and os.path.exists(args.jp):
        with io.open(args.jp, encoding="utf-8") as fh:
            jp = json.load(fh)
        print("日文释义 %d 条" % len(jp))

    words_txt = os.path.join(args.out, "oxford3000-words.txt")
    freq_txt = os.path.join(args.out, "oxford3000-freq.txt")
    detail_js = os.path.join(args.out, "oxford3000-detail.json")
    roots_js = os.path.join(args.out, "oxford3000-roots.json")

    with io.open(words_txt, "w", encoding="utf-8", newline="\n") as fw, \
         io.open(freq_txt, "w", encoding="utf-8", newline="\n") as ff:
        for w in sorted(lv):
            d = det.get(w) or {}
            sd = short_def(d["senses"][0]["def"]) if d.get("senses") else ""
            label = (jp.get(w) or {}).get("jp") or sd      # 节点标签：日文优先
            fw.write("%s\t%s\t%s\n" % (w, label, sd))
            ff.write("%s\t%d\n" % (w, LEVEL_FREQ.get(lv[w], 1)))

    payload = {}
    for w in sorted(lv):
        rec = dict(det.get(w, {}), level=lv[w])
        j = jp.get(w)
        if j:
            rec["jp"] = j["jp"]
            rec["jp_pairs"] = j["pairs"]
        payload[w] = rec
    with io.open(detail_js, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    with io.open(roots_js, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(roots, fh, ensure_ascii=False, indent=1)

    print("\n词表   -> %s" % words_txt)
    print("词频   -> %s" % freq_txt)
    print("详情   -> %s (%.0f KB)" % (detail_js, os.path.getsize(detail_js) / 1024))
    print("词根   -> %s" % roots_js)

    if args.js:
        os.makedirs(args.js, exist_ok=True)
        jsp = os.path.join(args.js, "detail.js")
        with io.open(jsp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("// 由 tools/build_oxford.py 生成，请勿手改\n")
            fh.write("window.WC_DETAIL = %s;\n"
                     % json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        print("详情JS -> %s (%.0f KB)" % (jsp, os.path.getsize(jsp) / 1024))

    if args.copy_audio:
        os.makedirs(args.copy_audio, exist_ok=True)
        src = os.path.join(OX, "data", "mp3")
        n = 0
        for w, d in det.items():
            for k in ("uk", "us"):
                fn = (d.get("audio") or {}).get(k)
                if fn and os.path.exists(os.path.join(src, fn)):
                    shutil.copy2(os.path.join(src, fn), os.path.join(args.copy_audio, fn))
                    n += 1
        print("音频   -> %s（%d 个文件）" % (args.copy_audio, n))


if __name__ == "__main__":
    main()
