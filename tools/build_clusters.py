# -*- coding: utf-8 -*-
"""从一份单词表自动生成形近词簇，输出与 json/pageN.json 同构的文件。

两个子命令：
  build  词表  ->  词簇 JSON（app 可直接读）
  audit  给现有 json/*.json 的每条边打分，找出凑数的连接

核心思路见 README 或对话记录：
  1. 候选生成 —— 3-gram 倒排 + 长度分桶求编辑距离，召回率约 97%
  2. 关系定型 —— compound > derive > spelling
  3. 打分     —— 锚点 x 位置 + 易错点覆盖 - 干扰惩罚
  4. 成簇     —— 选中心词，再按边权长成一棵树（每词只有一个父节点）

用法：
  python tools/build_clusters.py build --words words.txt --out out/
  python tools/build_clusters.py build --words json/ --out out/ --report report.json
  python tools/build_clusters.py audit --src json/ --top 40
"""

import argparse, collections, csv, glob, io, itertools, json, math, os, re, sys

# --------------------------------------------------------------------------
# 词缀表：用于判定 derive，以及判断共享块是否算"有意义的块"
# --------------------------------------------------------------------------
SUFFIXES = ["s", "es", "ed", "ing", "er", "est", "ly", "y", "ful", "less", "ness",
            "ment", "tion", "ation", "sion", "ion", "al", "ial", "ive", "able",
            "ible", "ist", "ism", "ian", "ship", "hood", "dom", "en", "ize",
            "ise", "ity", "ty", "ous", "ance", "ence", "ant", "ent", "ward",
            "wards", "th", "teen", "fold", "like", "ish"]
PREFIXES = ["un", "dis", "re", "in", "im", "il", "ir", "non", "pre", "mis",
            "over", "under", "out", "up", "be", "en", "em", "sur", "trans",
            "inter", "super", "anti", "co", "de", "ex", "fore", "semi", "sub",
            "mid", "post", "pro", "tele", "auto", "bi", "tri", "multi", "a"]

# 常见易错模式：共享块盖住这些位置，才真正帮到拼写
ERROR_ZONES = [
    r"ie", r"ei",                 # friend / receive / believe
    r"ance$", r"ence$",           # experience / importance
    r"able$", r"ible$",           # comfortable / possible
    r"ary$", r"ery$", r"ory$",    # library / necessary
    r"([bcdfglmnprstz])\1",       # 双写字母 committee / necessary
    r"gh", r"kn", r"wr", r"mb$", r"ps",   # 不发音字母 night / knife / write / comb
    r"tion$", r"sion$", r"cian$",
    r"ough", r"augh",
]
ERROR_RE = [re.compile(p) for p in ERROR_ZONES]

WORD_RE = re.compile(r"^[a-z][a-z\-' ]*$")


# --------------------------------------------------------------------------
# 基础字符串工具
# --------------------------------------------------------------------------
def lev(a, b, cap=3):
    """带上限的编辑距离；超过 cap 直接返回 cap+1，省掉大量无谓计算。"""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def lcs(a, b):
    """最长公共子串（连续）。app 里高亮的就是这个。"""
    best = ""
    for i in range(len(a)):
        for j in range(i + len(best) + 1, len(a) + 1):
            if a[i:j] in b:
                best = a[i:j]
    return best


def covers_error_zone(word, chunk):
    """共享块是否盖住了这个词里一处常见易错位置。"""
    i = word.find(chunk)
    if i < 0:
        return False
    lo, hi = i, i + len(chunk)
    for rx in ERROR_RE:
        for m in rx.finditer(word):
            # 易错片段被共享块完整包住才算数
            if m.start() >= lo and m.end() <= hi:
                return True
    return False


# --------------------------------------------------------------------------
# 读词表
# --------------------------------------------------------------------------
def load_words(path):
    """支持：
       *.txt  每行 `word` 或 `word<TAB>日文<TAB>中文`
       *.csv  首行表头，需含 word 列，可选 jp / zh 列
       *.json 字符串数组，或 [{id,jp,zh}]，或 pageN.json 格式
       目录    读取其中全部 *.json（当作 pageN.json）
       返回 [(word, jp, zh)]，按出现顺序去重
    """
    out, seen = [], set()

    def add(w, jp="", zh=""):
        w = str(w).strip().lower()
        if not w or w in seen or not WORD_RE.match(w):
            return
        seen.add(w)
        out.append((w, jp or "", zh or ""))

    def eat_json(obj):
        if isinstance(obj, dict) and "words" in obj:       # pageN.json
            for w in obj["words"]:
                add(w.get("id", ""), w.get("jp", ""), w.get("zh", ""))
        elif isinstance(obj, list):
            for it in obj:
                if isinstance(it, str):
                    add(it)
                elif isinstance(it, dict):
                    add(it.get("id") or it.get("word", ""), it.get("jp", ""), it.get("zh", ""))

    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.json")),
                       key=lambda f: int(re.sub(r"\D", "", os.path.basename(f)) or 0))
        for f in files:
            with io.open(f, encoding="utf-8") as fh:
                eat_json(json.load(fh))
    elif path.lower().endswith(".json"):
        with io.open(path, encoding="utf-8") as fh:
            eat_json(json.load(fh))
    elif path.lower().endswith(".csv"):
        with io.open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                low = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
                add(low.get("word") or low.get("id", ""), low.get("jp", ""), low.get("zh", ""))
    else:
        with io.open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if parts and parts[0].strip():
                    add(parts[0], parts[1] if len(parts) > 1 else "",
                        parts[2] if len(parts) > 2 else "")
    return out


def load_freq(path, words):
    """词频表：每行 `word [空白] 数值`，数值越大越常见。
       没给文件时，用词表自身顺序当频率代理（很多词表本身就是按频率排的）。
       返回 word -> [0,1] 的分值。
    """
    if not path:
        n = max(len(words), 1)
        return {w: 1.0 - i / n for i, w in enumerate(words)}
    raw = {}
    with io.open(path, encoding="utf-8") as fh:
        for line in fh:
            p = line.split()
            if len(p) >= 2:
                try:
                    raw[p[0].strip().lower()] = float(p[1])
                except ValueError:
                    pass
    if not raw:
        return {w: 0.5 for w in words}
    hi = math.log(max(raw.values()) + 1)
    return {w: (math.log(raw.get(w, 0) + 1) / hi if hi else 0.5) for w in words}


# --------------------------------------------------------------------------
# 1. 候选生成
# --------------------------------------------------------------------------
def candidates(words):
    """返回候选词对集合。两条通道：共享 3-gram、编辑距离<=2。"""
    pairs = set()
    inv = collections.defaultdict(list)
    for w in words:
        for i in range(len(w) - 2):
            inv[w[i:i + 3]].append(w)
    for bucket in inv.values():
        if len(bucket) > 400:            # 极高频片段（-ing/-tion）先跳过，靠打分阶段也救不回来
            continue
        for a, b in itertools.combinations(sorted(set(bucket)), 2):
            pairs.add((a, b))
    bylen = collections.defaultdict(list)
    for w in words:
        bylen[len(w)].append(w)
    for L, ws in sorted(bylen.items()):
        pool = sorted(set(ws + bylen.get(L + 1, []) + bylen.get(L + 2, [])))
        for a, b in itertools.combinations(pool, 2):
            if (a, b) not in pairs and lev(a, b, 2) <= 2:
                pairs.add((a, b))
    return pairs


# --------------------------------------------------------------------------
# 2. 关系定型：compound > derive > spelling
# --------------------------------------------------------------------------
def split_affix(stem, other):
    """other 是不是 stem 加词缀来的？返回词缀类型或 None。"""
    if other.startswith(stem):
        tail = other[len(stem):]
        if tail in SUFFIXES:
            return "suffix"
    if other.endswith(stem):
        head = other[:-len(stem)]
        if head in PREFIXES:
            return "prefix"
    # 拼写变体：e 脱落 / y->i / 末字母双写
    variants = {stem}
    if stem.endswith("e"):
        variants.add(stem[:-1])
    if stem.endswith("y"):
        variants.add(stem[:-1] + "i")
    if len(stem) > 2:
        variants.add(stem + stem[-1])
    for v in variants:
        for s in SUFFIXES:
            if other == v + s:
                return "suffix"
    return None


def classify(a, b, vocab):
    """判定 a-b 的关系类型、父->子方向、共享块。无关系返回 None。"""
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)

    # 复合词：短词完整嵌在长词里，且剩下的部分也是个词
    if short in long_ and len(long_) - len(short) >= 2:
        rest = long_.replace(short, "", 1)
        if rest in vocab:
            return "compound", short, long_, short

    # 派生：加词缀
    kind = split_affix(short, long_)
    if kind:
        return "derive", short, long_, short

    # 复合词（宽松）：短词嵌在长词里但剩余部分不是独立词（restroom / textbook 这类）
    if short in long_ and len(long_) - len(short) >= 2:
        return "compound", short, long_, short

    # 形近
    chunk = lcs(a, b)
    if len(chunk) >= 3 or lev(a, b, 2) <= 2:
        # 方向：短的当父节点，一样长按字母序
        p, c = (short, long_) if len(a) != len(b) else tuple(sorted((a, b)))
        return "spelling", p, c, chunk
    return None


# --------------------------------------------------------------------------
# 3. 打分
# --------------------------------------------------------------------------
def chunk_df(words, maxlen=9):
    """统计每个子串出现在多少个词里，用来算块的区分度（IDF）。"""
    df = collections.Counter()
    for w in words:
        seen = set()
        for i in range(len(w)):
            for j in range(i + 2, min(len(w), i + maxlen) + 1):
                seen.add(w[i:j])
        df.update(seen)
    return df


def idf_weight(chunk, df, V):
    """tion / ing 这种到处都是的块，区分度低，得压下去；
       round / count 这种只出现在几个词里的块，才是真的记忆抓手。"""
    if not chunk or not df or V <= 1:
        return 1.0
    n = df.get(chunk, 1)
    idf = math.log(V / (1 + n)) / math.log(V)      # (0,1]
    return 0.3 + 0.7 * max(0.0, min(1.0, idf))


def anchor_score(chunk, vocab):
    if len(chunk) < 2:
        return 0.1
    if chunk in vocab:
        return 2.0                       # end / rest / arm —— 块本身就是会的词
    if chunk in SUFFIXES or chunk in PREFIXES:
        return 1.6                       # -ness / un- —— 有意义的构词单位
    if len(chunk) >= 4:
        return 0.8                       # 长碎片，勉强能当块
    return 0.2                           # rien / obb / ard —— 无锚碎片


def position_score(a, b, chunk):
    if not chunk:
        return 0.3
    ta, tb = a.endswith(chunk), b.endswith(chunk)
    ha, hb = a.startswith(chunk), b.startswith(chunk)
    if ta and tb:
        return 1.5                       # 同韵族 best/rest/test
    if ha and hb:
        return 1.2                       # 同词头 count/country
    if (ta and hb) or (ha and tb) or ta or tb or ha or hb:
        return 1.0                       # 一端靠边 farmland/land
    return 0.3                           # 卡中间 friend/experience 的 rien


def score_edge(parent, child, etype, chunk, vocab, freq, mis=None, df=None, V=0):
    """返回 (总分, 明细)。"""
    anc = anchor_score(chunk, vocab) * idf_weight(chunk, df, V or len(vocab))
    pos = position_score(parent, child, chunk)
    base = {"compound": 1.15, "derive": 1.1, "spelling": 1.0, "root": 1.3}[etype]

    cov = 0.0
    if covers_error_zone(child, chunk):
        cov += 0.6
    if covers_error_zone(parent, chunk):
        cov += 0.3
    if mis:                              # 有真人错拼语料时，直接看块是否盖住实际错位
        for wrong in mis.get(child, []):
            if chunk in child and chunk not in wrong:
                cov += 0.5
                break

    ratio = len(chunk) / max(len(parent), len(child)) if chunk else 0
    shape = 0.6 * ratio - 0.35 * abs(len(parent) - len(child)) / 10.0

    # 「同框换字母」：fix/fox、click/clock、inspect/insect。
    # 这类词共享的是一个不连续的框架，最长公共子串看不出来，得单独算。
    d = lev(parent, child, 3)
    longest = max(len(parent), len(child))
    frame = 1.9 * (1 - d / longest) if d <= 2 else 0.0

    # 只差一个字母、长度又相同的高频词：quite/quiet 这类，学的时候容易互相污染。
    # 标记出来单独做对比练习，这里只做轻度降权，不让它盖过框架分。
    conf = (d == 1 and len(parent) == len(child) and longest >= 4)
    penalty = 0.4 if conf else 0.0

    total = max(anc * pos * base, frame * base) + cov + shape + 0.25 * freq.get(child, 0) - penalty
    return round(total, 3), {
        "anchor": round(anc, 3), "position": pos, "coverage": round(cov, 2),
        "shape": round(shape, 3), "confusable": conf, "chunk": chunk, "type": etype,
    }


# --------------------------------------------------------------------------
# 4. 成簇：选中心词 + 长树
# --------------------------------------------------------------------------
def build_clusters(words, scored, freq, size_min, size_max, min_score):
    """scored: {(parent,child): (score, info)}；返回 [{center, edges:[(p,c,type)], words:[...]}]"""
    adj = collections.defaultdict(list)
    for (p, c), (sc, info) in scored.items():
        if sc < min_score:
            continue
        adj[p].append((sc, c, info))
        adj[c].append((sc, p, info))
    for k in adj:
        adj[k].sort(reverse=True, key=lambda x: x[0])

    # 中心词打分：要像个"词根"——短、常见、连接多，而且别的词是在它身上长出来的。
    # 少了最后这一项，fighting / attention 这种长派生词会因为 3-gram 多而被选成中心。
    def center_rank(w):
        nb = adj.get(w, ())
        deg = len(nb)
        strength = sum(s for s, _, _ in nb[:size_max])
        rooted = sum(1 for _, c, _ in nb if w in c and w != c)   # 有多少邻居把它整个包含在内
        return (deg * 0.35 + strength * 0.3 + rooted * 1.8
                + freq.get(w, 0) * 4 - len(w) * 1.6)

    used, clusters = set(), []
    for center in sorted(words, key=center_rank, reverse=True):
        if center in used or len(adj.get(center, ())) < size_min - 1:
            continue
        tree, edges = {center}, []
        # Prim：每次从树里向外挑分最高的一条边
        frontier = [(s, center, c, i) for s, c, i in adj[center]]
        while len(tree) < size_max and frontier:
            frontier.sort(reverse=True, key=lambda x: x[0])
            s, p, c, info = frontier.pop(0)
            if c in tree or c in used:
                continue
            tree.add(c)
            edges.append((p, c, info["type"]))
            for s2, c2, i2 in adj.get(c, ()):
                if c2 not in tree and c2 not in used:
                    frontier.append((s2 * 0.92, c, c2, i2))   # 越往外权重略降，树不至于长成长链
        if len(tree) < size_min:
            continue
        used |= tree
        clusters.append({"center": center, "edges": edges, "words": sorted(tree)})
    # 第二轮：把落单的词挂到已有簇上（有够分的边就挂），簇可以略微超出上限
    idx = {w: k for k, c in enumerate(clusters) for w in c["words"]}
    for w in [x for x in words if x not in used]:
        best = None
        for s, nb, info in adj.get(w, ()):
            k = idx.get(nb)
            if k is None or len(clusters[k]["words"]) >= size_max + 2:
                continue
            if best is None or s > best[0]:
                best = (s, k, nb, info)
        if best:
            _, k, nb, info = best
            clusters[k]["words"].append(w)
            clusters[k]["words"].sort()
            clusters[k]["edges"].append((nb, w, info["type"]))
            idx[w] = k
            used.add(w)

    leftovers = [w for w in words if w not in used]
    return clusters, leftovers


def paginate(clusters, per_unit):
    return [clusters[i:i + per_unit] for i in range(0, len(clusters), per_unit)]


def write_units(units, gloss, out_dir, title_fmt, source_fmt):
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for n, unit in enumerate(units, 1):
        words, cl_meta, edges = [], [], []
        for k, c in enumerate(unit, 1):
            cid = "c%d" % k
            cl_meta.append({"id": cid, "center": c["center"]})
            jp, zh = gloss.get(c["center"], ("", ""))
            words.append({"id": c["center"], "jp": jp, "zh": zh, "cluster": cid, "role": "center"})
            for w in c["words"]:
                if w == c["center"]:
                    continue
                jp, zh = gloss.get(w, ("", ""))
                words.append({"id": w, "jp": jp, "zh": zh, "cluster": cid, "role": "member"})
            for p, ch, t in c["edges"]:
                edges.append({"from": p, "to": ch, "type": t})
        doc = {"source": source_fmt % n, "clusters": cl_meta, "words": words, "edges": edges}
        path = os.path.join(out_dir, title_fmt % n)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        written.append(path)
    return written


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def score_all(words, freq, mis=None, roots=None):
    vocab = set(words)
    df, V = chunk_df(words), len(words)
    scored = {}
    for a, b in candidates(words):
        r = classify(a, b, vocab)
        if not r:
            continue
        etype, p, c, chunk = r
        scored[(p, c)] = score_edge(p, c, etype, chunk, vocab, freq, mis, df, V)

    # 同词源：由词源数据给出，不靠字符串猜。词根是真的语义关联，
    # 分数给得比形近高，且不要求字面相似（receive / deceive 这种也能连上）。
    for root, ws in (roots or {}).items():
        ws = [w for w in ws if w in vocab]
        if len(ws) < 2:
            continue
        hub = min(ws, key=lambda w: (len(w), w))          # 最短的当这一族的根
        for w in ws:
            if w == hub:
                continue
            key = (hub, w) if (w, hub) not in scored else (w, hub)
            base = 2.6 + 0.25 * freq.get(w, 0)
            prev = scored.get(key)
            if prev is None or prev[0] < base:
                scored[key] = (round(base, 3),
                               {"anchor": 2.0, "position": 1.0, "coverage": 0.0,
                                "shape": 0.0, "confusable": False,
                                "chunk": root, "type": "root"})
    return scored


def cmd_build(args):
    rows = load_words(args.words)
    words = [w for w, _, _ in rows]
    gloss = {w: (jp, zh) for w, jp, zh in rows}
    if not words:
        sys.exit("词表是空的：%s" % args.words)
    freq = load_freq(args.freq, words)
    mis = load_misspellings(args.misspellings) if args.misspellings else None

    roots = None
    if args.roots:
        with io.open(args.roots, encoding="utf-8") as fh:
            roots = json.load(fh)
        print("词根表 %d 组" % len(roots))
    print("词表 %d 个词，生成候选……" % len(words))
    scored = score_all(words, freq, mis, roots)
    print("候选边 %d 条，其中 %d 条达到阈值 %.2f"
          % (len(scored), sum(1 for s, _ in scored.values() if s >= args.min_score), args.min_score))

    clusters, leftovers = build_clusters(words, scored, freq,
                                         args.size_min, args.size_max, args.min_score)
    units = paginate(clusters, args.per_unit)
    files = write_units(units, gloss, args.out, args.name, args.source)

    covered = sum(len(c["words"]) for c in clusters)
    print("\n生成 %d 个词簇 / %d 个单元 -> %s" % (len(clusters), len(units), args.out))
    print("覆盖 %d/%d 个词 (%.1f%%)，落单 %d 个"
          % (covered, len(words), 100 * covered / len(words), len(leftovers)))
    if leftovers[:12]:
        print("落单示例:", ", ".join(leftovers[:12]))

    if args.report:
        rep = {
            "words": len(words), "clusters": len(clusters), "units": len(units),
            "covered": covered, "leftovers": leftovers,
            "confusable_pairs": sorted({tuple(sorted(k)) for k, (s, i) in scored.items()
                                        if i["confusable"]}),
            "clusters_detail": [{
                "center": c["center"], "size": len(c["words"]),
                "edges": [dict(zip(("from", "to", "type", "score", "chunk"),
                                   (p, ch, t) + edge_info(scored, p, ch)))
                          for p, ch, t in c["edges"]],
            } for c in clusters],
        }
        with io.open(args.report, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
        print("报告 ->", args.report)
        print("易混对 %d 组（建议单独对比学习）" % len(rep["confusable_pairs"]))


def edge_info(scored, p, c):
    """成簇时边的父子方向可能和打分时相反，两边都查一下。"""
    hit = scored.get((p, c)) or scored.get((c, p))
    return (hit[0], hit[1]["chunk"]) if hit else (None, None)


def load_misspellings(path):
    """每行 `正确拼写<TAB>错拼[,错拼...]`，例如维基百科常见错拼列表整理后的结果。"""
    mis = collections.defaultdict(list)
    with io.open(path, encoding="utf-8") as fh:
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2:
                mis[p[0].strip().lower()] += [x.strip().lower() for x in p[1].split(",") if x.strip()]
    return mis


def cmd_audit(args):
    files = sorted(glob.glob(os.path.join(args.src, "*.json")),
                   key=lambda f: int(re.sub(r"\D", "", os.path.basename(f)) or 0))
    vocab, edges = set(), []
    for f in files:
        with io.open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        vocab |= {w["id"].lower() for w in d["words"]}
        for e in d["edges"]:
            edges.append((os.path.basename(f), e["from"].lower(), e["to"].lower(), e["type"]))
    freq = load_freq(args.freq, sorted(vocab))
    mis = load_misspellings(args.misspellings) if args.misspellings else None

    df, V = chunk_df(sorted(vocab)), len(vocab)
    rows = []
    for fname, p, c, t in edges:
        r = classify(p, c, vocab)
        chunk = r[3] if r else lcs(p, c)
        etype = r[0] if r else t
        sc, info = score_edge(p, c, etype, chunk, vocab, freq, mis, df, V)
        rows.append({"file": fname, "from": p, "to": c, "labeled": t,
                     "detected": etype, "chunk": chunk, "score": sc,
                     "anchor": info["anchor"], "position": info["position"],
                     "coverage": info["coverage"], "confusable": info["confusable"]})
    rows.sort(key=lambda r: r["score"])

    mism = [r for r in rows if r["labeled"] != r["detected"]]
    print("边 %d 条，平均分 %.2f" % (len(rows), sum(r["score"] for r in rows) / len(rows)))
    print("类型标注与规则判定不一致：%d 条 (%.1f%%)" % (len(mism), 100 * len(mism) / len(rows)))
    print("易混对（只差一个字母、长度相同）：%d 条" % sum(1 for r in rows if r["confusable"]))
    print("\n分数最低的 %d 条（最可能是凑数的连接）：" % args.top)
    print("%-28s %-9s %-9s %-7s %s" % ("边", "标注", "判定", "共享块", "分数"))
    for r in rows[:args.top]:
        print("%-28s %-9s %-9s %-7s %.2f"
              % ("%s -> %s" % (r["from"], r["to"]), r["labeled"], r["detected"],
                 r["chunk"] or "-", r["score"]))
    if args.report:
        with io.open(args.report, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        print("\n完整报告 ->", args.report)


def main():
    ap = argparse.ArgumentParser(description="形近词簇自动生成 / 现有词簇质量审计")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="从词表生成词簇")
    b.add_argument("--words", required=True, help="词表：.txt/.csv/.json，或装着 pageN.json 的目录")
    b.add_argument("--out", default="out", help="输出目录")
    b.add_argument("--freq", help="词频表：每行 `word 数值`")
    b.add_argument("--misspellings", help="错拼语料：每行 `正确<TAB>错拼,错拼`")
    b.add_argument("--roots", help="词根表 JSON：{词根: [词,...]}，生成同词源连线")
    b.add_argument("--size-min", type=int, default=6, help="每簇最少词数")
    b.add_argument("--size-max", type=int, default=11, help="每簇最多词数")
    b.add_argument("--per-unit", type=int, default=8, help="每单元几个词簇")
    b.add_argument("--min-score", type=float, default=1.2, help="边的最低分，越高越严")
    b.add_argument("--name", default="page%d.json", help="输出文件名模板")
    b.add_argument("--source", default="自动生成 第%d单元", help="source 字段模板")
    b.add_argument("--report", help="把打分明细写到这个 JSON")
    b.set_defaults(func=cmd_build)

    a = sub.add_parser("audit", help="给现有词簇的每条边打分")
    a.add_argument("--src", default="json", help="装着 pageN.json 的目录")
    a.add_argument("--freq")
    a.add_argument("--misspellings")
    a.add_argument("--top", type=int, default=30, help="列出分数最低的几条")
    a.add_argument("--report", help="把完整打分写到这个 JSON")
    a.set_defaults(func=cmd_audit)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
