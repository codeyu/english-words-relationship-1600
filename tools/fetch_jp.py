# -*- coding: utf-8 -*-
"""从本地 monokakido-python 服务（ウィズダム英和辞典）取日文释义，压成 1~2 条带词性的短释义。

选义标准（两重锚定）：
  1. 词性锚定 —— 牛津数据已经给出这个词的词性（noun/verb/...），
     只取对应词性组的释义，避免 record 取到「動」而牛津主要当「名」用。
  2. 义项锚定 —— ウィズダム 的语义按常用度排序，取每个词性组的第 1 义。
  最多输出 2 条（按牛津的词性顺序）；牛津只给一个词性时，取该词性的第 1、2 义。

用法：
  先启动词典服务：
    cd ../monokakido-python && python -m uvicorn api_server:app --port 8011
  再跑：
    python tools/fetch_jp.py --words data/oxford3000-words.txt --detail data/oxford3000-detail.json
    python tools/fetch_jp.py --limit 20 --print   # 先看看效果
"""

import argparse, io, json, os, re, sys, time, urllib.parse, urllib.request

from bs4 import BeautifulSoup

API = "http://127.0.0.1:8011"

# 牛津词性 -> ウィズダム 词性标记
POS_MAP = {
    "noun": "名", "verb": "動", "adjective": "形", "adverb": "副",
    "preposition": "前", "pronoun": "代", "conjunction": "接",
    "determiner": "限", "number": "数", "exclamation": "間",
    "modal verb": "助動", "auxiliary verb": "助動", "linking verb": "動",
    "indefinite article": "冠", "definite article": "冠",
}
POS_LABEL = {"名": "名", "動": "動", "形": "形", "副": "副", "前": "前",
             "代": "代", "接": "接", "限": "限", "数": "数", "間": "間",
             "助動": "助動", "冠": "冠"}

# 这些子标签是语法/语域标注，不是释义本身，压缩时丢掉
DROP_TAGS = ("用法", "注記", "選択制限", "共起", "専門", "内包", "参照",
             "名詞区分", "動詞区分", "複合語", "語法", "反意", "同意",
             "語源", "派生情報", "見出部", "変化", "発音", "用例G",
             "ref", "a", "補足", "rect", "ルビ")


def api_raw(word):
    u = "%s/api/raw/%s" % (API, urllib.parse.quote(word))
    with urllib.request.urlopen(u, timeout=30) as r:
        return json.load(r)


def clean_gloss(node):
    """把 <語釈> 压成一行日文：去掉句型标记、语域标记、异体写法和空括号。"""
    s = BeautifulSoup(str(node), "xml")
    for t in s.find_all(DROP_TAGS):
        t.decompose()
    txt = s.get_text(" ", strip=True)
    txt = re.sub(r"⦍[^⦎]*⦎", "", txt)                  # 句型：⦍book(A)B⦎
    txt = re.sub(r"⦅[^⦆]*⦆", "", txt)                  # 语域：⦅書⦆⦅ややかたく⦆
    txt = re.sub(r"⦑[^⦒]*⦒", "", txt)                  # 领域：⦑物理⦒
    txt = re.sub(r"[⦋⦌⚹⚷⚸･→]", "", txt)
    txt = re.sub(r"\[[^\]]*\]", "", txt)               # 异体：大学の[に関する]
    txt = re.sub(r"[（(][^）)]*[A-Za-z][^）)]*[）)]", "", txt)  # 含英文的括注：(reserve)
    txt = re.sub(r"\s+", "", txt)
    txt = re.sub(r"^[（(][^）)]*[）)]", "", txt)          # 开头的括注
    txt = re.sub(r"[（(]\s*[）)]", "", txt)              # 清完后剩下的空括号
    txt = txt.strip(" .，,、;；:：/／")
    # 只保留第一个分号前的部分，再取前两个并列项（去重）
    head = re.split(r"[;；]", txt)[0]
    parts, seen = [], set()
    for p0 in re.split(r"[,，、]", head):
        p0 = p0.strip(" ()（）")
        # 在括号内被切开的残片（"記録(書類"）：从左括号处截掉
        if p0.count("(") != p0.count(")"):
            p0 = re.split(r"[（(]", p0)[0].strip()
        if p0.count("（") != p0.count("）"):
            p0 = re.split(r"[（(]", p0)[0].strip()
        if p0 and p0 not in seen:
            seen.add(p0)
            parts.append(p0)
    return "、".join(parts[:2]).strip()


def parse_entry(xml):
    """raw XML -> [(词性, [该词性的义项...])]，保持词典里的顺序。"""
    soup = BeautifulSoup(xml, "xml")
    groups = []
    for sense in soup.find_all("sense"):
        pos_el = sense.find("品詞")
        pos = pos_el.get_text(strip=True) if pos_el else ""
        items = []
        for g in sense.find_all("語義"):
            num_el = g.find("番号")
            num = num_el.get_text(strip=True) if num_el else ""
            sk = g.find("語釈")
            if not sk:
                continue
            txt = clean_gloss(sk)
            if txt:
                items.append((num, txt))
        if items:
            groups.append((pos, items))
    return groups


def pick(groups, ox_pos, want=2):
    """按牛津词性顺序取每组第 1 义；牛津只给一个词性时取该组前两义。"""
    if not groups:
        return []
    wanted = [POS_MAP.get(p, "") for p in (ox_pos or [])]
    wanted = [p for p in wanted if p]
    out = []
    used = set()
    for wp in wanted:
        for i, (pos, items) in enumerate(groups):
            if i in used or not pos.startswith(wp):
                continue
            out.append((POS_LABEL.get(pos, pos), items[0][1]))
            used.add(i)
            break
        if len(out) >= want:
            break
    if not out:                                   # 牛津词性对不上，退回词典第一个有词性的组
        for pos, items in groups:
            if pos:
                out = [(POS_LABEL.get(pos, pos), items[0][1])]
                break
    if len(out) == 1:                             # 只有一个词性，补它的第 2 义（各只留一项）
        for pos, items in groups:
            if POS_LABEL.get(pos, pos) == out[0][0] and len(items) > 1:
                first = out[0][1].split("、")[0]
                second = items[1][1].split("、")[0]
                if second and second != first:
                    out[0] = (out[0][0], first)
                    out.append((out[0][0], second))
                break
    return out[:want]


def fmt(pairs):
    """[('名','記録'),('動','記録する')] -> '[名]記録 [動]記録する'"""
    if not pairs:
        return ""
    if len(pairs) == 2 and pairs[0][0] == pairs[1][0]:
        return "[%s]%s、%s" % (pairs[0][0], pairs[0][1], pairs[1][1])
    return " ".join("[%s]%s" % (p, g) for p, g in pairs)


# 词典里查不到、退回近似词会出错的，直接写死（只有这 15 个）
MANUAL = {
    "app": [("名", "アプリ")], "cannot": [("助動", "…できない")],
    "disc": [("名", "ディスク、円盤")], "enquiry": [("名", "問い合わせ、調査")],
    "everybody": [("代", "誰でも、皆")], "healthcare": [("名", "医療、健康管理")],
    "info": [("名", "情報")], "judgement": [("名", "判断、判決")],
    "located": [("形", "…に位置して")], "o'clock": [("副", "…時")],
    "practise": [("動", "を練習する")], "roughly": [("副", "およそ、だいたい")],
    "tonne": [("名", "トン")], "upon": [("前", "…の上に")],
    "yeah": [("副", "うん、そうだ")],
}

# 词典收词形与牛津词表不一致的，手工兜底
VARIANTS = {
    "practise": "practice", "judgement": "judgment", "enquiry": "inquiry",
    "tonne": "ton", "disc": "disk", "cannot": "can", "upon": "on",
    "everybody": "everyone", "healthcare": "health care", "o'clock": "oclock",
    "app": "application", "info": "information", "yeah": "yes",
    "roughly": "rough", "located": "locate",
}


def lookup(word, ox_pos):
    """查一个词；查不到时试几种常见变体。"""
    if word in MANUAL:
        return MANUAL[word], "(manual)"
    tries = [word]
    if word in VARIANTS:
        tries.append(VARIANTS[word])
    if word.endswith("s") and len(word) > 3:
        tries.append(word[:-1])
    if word.endswith("es") and len(word) > 4:
        tries.append(word[:-2])
    if "-" in word:
        tries.append(word.replace("-", ""))
        tries.append(word.replace("-", " "))
    wanted = {POS_MAP.get(p, "") for p in (ox_pos or [])} - {""}
    for w in tries:
        try:
            d = api_raw(w)
        except Exception:
            continue
        best = None
        # 一个词可能有多个词条（a1 字母 / a2 冠词），挑词性和牛津对得上的那条
        for r in d.get("results", []):
            groups = parse_entry(r.get("raw_content", ""))
            pairs = pick(groups, ox_pos)
            if not pairs:
                continue
            score = sum(1 for p, _ in pairs if p in wanted)
            if best is None or score > best[0]:
                best = (score, pairs)
            if score == len(pairs) and score:
                break
        if best:
            return best[1], w
    return [], ""


def main():
    ap = argparse.ArgumentParser(description="取日文释义并压成 1~2 条")
    ap.add_argument("--words", default="data/oxford3000-words.txt")
    ap.add_argument("--detail", default="data/oxford3000-detail.json")
    ap.add_argument("--out", default="data/oxford3000-jp.json")
    ap.add_argument("--api", default=None)
    ap.add_argument("--limit", type=int, help="只跑前 N 个词（试跑用）")
    ap.add_argument("--print", dest="show", action="store_true", help="把结果打印出来")
    ap.add_argument("--resume", action="store_true", help="接着上次的结果继续")
    args = ap.parse_args()

    global API
    if args.api:
        API = args.api

    words = [l.split("\t")[0].strip() for l in io.open(args.words, encoding="utf-8") if l.strip()]
    with io.open(args.detail, encoding="utf-8") as fh:
        det = json.load(fh)
    done = {}
    if args.resume and os.path.exists(args.out):
        with io.open(args.out, encoding="utf-8") as fh:
            done = json.load(fh)
    todo = [w for w in words if w not in done]
    if args.limit:
        todo = todo[:args.limit]

    t0, miss = time.time(), []
    for i, w in enumerate(todo, 1):
        pairs, via = lookup(w, (det.get(w) or {}).get("pos") or [])
        if pairs:
            done[w] = {"jp": fmt(pairs), "pairs": pairs, "via": via}
            if args.show:
                print("%-16s %s" % (w, done[w]["jp"]))
        else:
            miss.append(w)
        if i % 200 == 0:
            print("  %d/%d  %.0fs" % (i, len(todo), time.time() - t0), file=sys.stderr)
            with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(done, fh, ensure_ascii=False, indent=0)

    with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(done, fh, ensure_ascii=False, indent=0)
    print("\n完成 %d 词，未命中 %d：%s" % (len(done), len(miss), ", ".join(miss[:20])))
    print("耗时 %.0f 秒 -> %s" % (time.time() - t0, args.out))


if __name__ == "__main__":
    main()
