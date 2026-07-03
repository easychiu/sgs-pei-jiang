# -*- coding: utf-8 -*-
"""
lint_tactics.py — 機械式一致性 linter(批20 核心工具, 長期保留)

讀 docs/data/tactics_parsed.json(引擎實際吃的資料) + 對應原文(tactics_overrides.json 優先,
否則 data/tactics.json 的 effectText/effectTarget), 逐戰法跑 9 條確定性規則(R1-R9), 找出
「原文有明確語意但 parsed 資料未正確反映、且未揭露」的違規, 輸出清單(名稱/規則/證據)。

設計原則(低誤報優先於高召回):
- 每條規則只咬"確定性"的錯位(能從原文正則抽出的明確語意 vs parsed 欄位不符), 不對模糊/
  概數措辭('約''左右''其中一種'語意不明) 做判斷。
- 任何有 _est/_todo/_note/_approx 揭露(戰法頂層或效果層級) 且揭露文字涵蓋該規則問題的,
  一律豁免(視為"已知且已誠實標註", 非本 linter 要追殺的對象)。
- type=="none" 的戰法(內政/幽靈條目, 不參與戰鬥) 全規則跳過。

用法:
    python lint_tactics.py --summary     # 人看的摘要(各規則違規數 + 前幾筆範例)
    python lint_tactics.py --json out.json   # 完整違規清單(供程式處理), 同時可加 --summary 印摘要
    python lint_tactics.py                # 預設等同 --summary

輸出 violation dict: {"name", "rule", "message", "evidence"}
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW_PATH = os.path.join(ROOT, "data", "tactics.json")
PARSED_PATH = os.path.join(ROOT, "docs", "data", "tactics_parsed.json")
OVERRIDES_PATH = os.path.join(ROOT, "docs", "data", "tactics_overrides.json")

STAT_KW = ("武力", "智力", "統率", "統帥", "速度", "魅力")
CTRL_EFFECT_KINDS = {"stun", "silence", "disarm", "chaos", "ambush", "taunt"}
DAMAGE_HEAL_KINDS = {"amp", "mitig", "heal", "stat"}  # scale 可意義套用的效果種類(R1)

DMG_RANGE = re.compile(r"傷害率\s*(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)\s*%?")
DMG_SINGLE = re.compile(r"傷害率\s*(\d+(?:\.\d+)?)\s*%")
HEAL_RANGE = re.compile(r"治療率\s*(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)\s*%?")
DUR_RE = re.compile(r"持續\s*(\d+(?:\.\d+)?)\s*(?:[-~]\s*(\d+(?:\.\d+)?)\s*)?回合")
PROB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:→|~|-)?\s*(\d+(?:\.\d+)?)?\s*%?\s*(?:的)?\s*(?:機率|概率|幾率)")
RATE_NOT_TRIGGER = ("規避", "會心", "奇謀")

SINGLE_TARGET_RE = re.compile(r"單體(?:（\s*1\s*人\s*）)?")
GROUP_TARGET_RE = re.compile(r"群體\s*(?:（\s*(\d+)\s*人\s*）)?")
GROUP_RANGE_RE = re.compile(r"[（(]\s*(\d+)\s*[~～-]\s*(\d+)\s*人\s*[)）]")
ALL_TARGET_RE = re.compile(r"全體")

# 「擇一」需排除「選擇一名/選擇一個」(單純挑選單一目標的措辭, 非多效果分支擇一);
# 「之一」需排除「之一部」等非本義用法。經全庫核對(scratchpad choice_kw 排查), 「選擇一」
# 開頭的 4 筆(結盟/虎痴/鐵騎驅馳/閉月)皆是選目標措辭, 不是效果擇一分支, 故用負向前瞻
# 排除「選」字開頭的「擇一」。
CHOICE_KW_RE = re.compile(r"其中一種|三選一|二選一|之一(?!部)|(?<!選)擇一|狀態的一種|狀態中的一種")
DUR_ROUND_RE = re.compile(r"持續\s*(\d+)\s*回合")


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_raw_texts():
    """回傳 {name: {"effectText":.., "effectTarget":.., "kind":.., "activationRate":..}},
    overrides 的 effectText/type 已套用(與 reparse_effects.py 的 apply_overrides_to_raw 同邏輯,
    確保 linter 核對的原文與 reparse 產生 parsed 檔時所用的原文一致, 避免對著舊原文誤判)。"""
    raw_list = load_json(RAW_PATH, [])
    raw_by_name = {t["nameZh"]: dict(t) for t in raw_list}
    overrides = (load_json(OVERRIDES_PATH, {}) or {}).get("overrides", {})
    invalid_names = set()
    for name, ov in overrides.items():
        if ov.get("invalid"):
            invalid_names.add(name)
            continue
        r = raw_by_name.get(name)
        if not r:
            continue
        if "effectText" in ov:
            r["effectText"] = ov["effectText"]
        if "type" in ov:
            r["_override_type"] = ov["type"]
    return raw_by_name, invalid_names


DISCLOSURE_KEYS = ("_est", "_todo", "_note", "_note2", "_note_self", "_approx", "_real", "_conf")


def has_disclosure(p, extra_effects=()):
    """戰法頂層或指定效果是否已有揭露性標記。_src 只是來源URL不算揭露; _real/_conf 是整批重解
    年代("battle_parity"式)遺留的「附上真實效果全文+信心度」揭露慣例, 同樣視為已知且已誠實標註。"""
    for k in DISCLOSURE_KEYS:
        if p.get(k):
            return True
    for e in extra_effects:
        for k in DISCLOSURE_KEYS:
            if e.get(k):
                return True
    return False


VERSION_BLOCK_SEP = re.compile(r"\s*/\s*")


def split_version_blocks(txt):
    """部分戰法(如 火燒連營/累世立名/守而必固)的原始 effectText 是多個歷史版本(不同更新日期)
    的公告文字直接用 ' / ' 串接在同一個字串裡, 不同版本各自的「持續N回合」互不相干(對應
    不同版本的不同數值), 逐句規則若跨版本抓「全文第一個匹配」會誤把 A 版本的持續回合套到
    B 版本才有的效果上(v1 火燒連營/累世立名假陽性即因此產生)。用 ' / ' 切開版本區塊, 規則
    只在「同一區塊」內核對, 就不會跨版本誤配。無 ' / ' 分隔的文字回傳整段(仍视为單一區塊)。"""
    blocks = [b for b in VERSION_BLOCK_SEP.split(txt) if b.strip()]
    return blocks if blocks else [txt]


def split_clauses(txt):
    """依常見句界(。；;) 切子句, 供逐子句核對「受X影響」等局部語意, 避免跨子句誤綁。
    先按版本區塊切開(見 split_version_blocks), 只在各自區塊內再切句, 避免跨版本混抓。"""
    clauses = []
    for block in split_version_blocks(txt):
        clauses.extend(c for c in re.split(r"[。；;]", block) if c)
    return clauses


# ---------------------------------------------------------------------------
# R1: scale 缺失 —— 子句含「受X影響」但對應效果無 scale 且無揭露
#
# 「受X影響」的作用範圍常只是同一逗號分隔子句裡的「一個括號內數值」(如 嬰城自守
# 「治療率46%→92%,受智力影響」+「治療率31%→62%」兩個治療率同句但只有第一個有受X影響),
# 用「。；;」切出的粗粒度子句無法區分, 改用「緊鄰括號」窗口: 找「受X影響」往前最近的
# 一組「(數字%[→數字%])」, 只要求「數值最接近該括號數字」的效果需要 scale, 不要求同子句
# 所有同類效果都要 scale(避免嬰城自守類假陽性)。
# ---------------------------------------------------------------------------
NEAR_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)?\s*(\d+(?:\.\d+)?)?\s*%")
SCALE_INFLUENCE_RE = re.compile(r"受(武力|智力|統率|統帥|速度|魅力)(?:或(武力|智力|統率|統帥|速度|魅力))?(?:[或和及]\S{0,4})*影響")


def _nearest_pct_before(clause, pos):
    """clause[:pos] 裡最後一個百分比數值(取範圍上限), 回傳 0~1 之間的浮點數或 None。"""
    best = None
    for m in NEAR_PCT_RE.finditer(clause[:pos]):
        best = m
    if best is None:
        return None
    v = float(best.group(2)) if best.group(2) else float(best.group(1))
    return round(v / 100, 4)


def check_r1(p, txt):
    violations = []
    effects = p.get("effects", [])
    if not effects:
        return violations
    for clause in split_clauses(txt):
        for sm in SCALE_INFLUENCE_RE.finditer(clause):
            hit_attrs = [g for g in sm.groups() if g]
            expect_val = _nearest_pct_before(clause, sm.start())
            local = clause[:sm.start()]
            candidate_kinds = []
            if "治療" in local:
                candidate_kinds.append("heal")
            if "降低" in local or "減少" in local:
                candidate_kinds.append("mitig")
            if "提升" in local or "提高" in local or "增加" in local:
                candidate_kinds.extend(["amp", "stat"])
            if not candidate_kinds:
                continue
            # 傷害率子句(含「傷害率」但無治療/加值動詞於局部窗口內) 已由 kind 天然建模, 跳過
            if "傷害率" in local and "治療" not in local:
                continue
            matched = [e for e in effects if e.get("k") in candidate_kinds]
            if not matched:
                continue
            if expect_val is not None:
                # 依數值就近比對(coef 或 val, 誤差 5% 內視為同一子句所指效果), 縮小到單一效果,
                # 避免同句多個同類效果(如兩個heal)被無差別要求全部加scale。
                def _val_of(e):
                    return e.get("coef") if e.get("coef") is not None else e.get("val")
                scored = [(abs((_val_of(e) or 0) - expect_val), e) for e in matched if _val_of(e) is not None]
                if scored:
                    scored.sort(key=lambda x: x[0])
                    best_diff, best_e = scored[0]
                    if best_diff <= 0.06:
                        matched = [best_e]
            if any(e.get("scale") for e in matched):
                continue
            if has_disclosure(p, matched):
                continue
            violations.append({
                "name": p["nameZh"], "rule": "R1",
                "message": f"原文子句含「受{('/'.join(hit_attrs))}影響」但對應效果({'/'.join(sorted({e.get('k') for e in matched}))})無 scale 且無揭露",
                "evidence": clause.strip(),
            })
    return violations


# ---------------------------------------------------------------------------
# R2: 目標數錯位 —— coef>0 時, 原文目標描述(單體/群體N人/全體) 與 n/nMax 不符
# ---------------------------------------------------------------------------
def check_r2(p, txt):
    violations = []
    coef = p.get("coef") or 0
    if coef <= 0:
        return violations
    # hitsRepeat: n/nMax 語意被引擎重新定義為「同一(隨機)目標的重複命中次數」(如「隨機釋放
    # 2→4次」), 不是同時中招的目標人數, 與 R2 假設的「n=同時命中人數」完全不同語意, 跳過
    # (見 sgz.py apply_effects 主coef段 hitsRepeat 分支)。lockTarget 同理(單體鎖定, n本就是1)。
    if p.get("hitsRepeat"):
        return violations
    n = p.get("n")
    nmax = p.get("nMax")
    # 多版本區塊(' / ' 分隔的歷史更新公告)只在「含傷害率匹配」的那個區塊內找目標描述,
    # 避免跨版本誤配(見 split_version_blocks 說明)。取第一個含傷害率匹配的區塊。
    block = None
    dm = None
    for b in split_version_blocks(txt):
        m = DMG_RANGE.search(b) or DMG_SINGLE.search(b)
        if m:
            block, dm = b, m
            break
    if dm is None:
        return violations
    # 窗口: 只在「逗號/句界」切出的最近一個子片段裡找目標描述(比 R1 更緊, 因為目標描述
    # 通常緊貼在傷害率之前, 如「對敵軍單體造成...傷害率」), 避免抓到更早一個描述其他效果
    # (如「我軍群體(2人)」增益受眾)的人數片語(偃旗息鼓類假陽性)。
    starts = [block.rfind(ch, 0, dm.start()) for ch in "。；;，,"]
    window_start = max(starts) + 1
    window = block[window_start:dm.start() + 40]
    # 目標描述須是「敵軍」的目標(非「我軍」受眾描述的群體/單體), 用最近的「敵」字錨定範圍起點。
    enemy_pos = window.rfind("敵")
    if enemy_pos == -1:
        return violations
    window = window[enemy_pos:]

    expect_n, expect_nmax, desc = None, None, None
    grp_range = GROUP_RANGE_RE.search(window)
    grp = GROUP_TARGET_RE.search(window)
    if grp_range:
        expect_n, expect_nmax = int(grp_range.group(1)), int(grp_range.group(2))
        desc = f"({expect_n}~{expect_nmax}人)"
    elif grp and grp.group(1):
        expect_n = int(grp.group(1))
        desc = f"群體({expect_n}人)"
    elif SINGLE_TARGET_RE.search(window):
        expect_n = 1
        desc = "單體"
    elif ALL_TARGET_RE.search(window):
        desc = "全體"

    if desc is None:
        return violations

    if desc == "全體":
        if n is not None and n < 3:
            if not has_disclosure(p):
                violations.append({
                    "name": p["nameZh"], "rule": "R2",
                    "message": f"原文傷害段描述「全體」(應n>=3) 但 n={n}",
                    "evidence": window.strip(),
                })
        return violations

    if expect_n is not None and n != expect_n:
        if not has_disclosure(p):
            violations.append({
                "name": p["nameZh"], "rule": "R2",
                "message": f"原文傷害段描述「{desc}」(應n={expect_n}) 但 parsed n={n}",
                "evidence": window.strip(),
            })
    if expect_nmax is not None and nmax != expect_nmax:
        if not has_disclosure(p):
            violations.append({
                "name": p["nameZh"], "rule": "R2",
                "message": f"原文傷害段描述「{desc}」(應nMax={expect_nmax}) 但 parsed nMax={nmax}",
                "evidence": window.strip(),
            })
    return violations


# ---------------------------------------------------------------------------
# R3: 機率未建 —— 「X%機率」修飾主效果但 rate==1 且無 when.on 且無 _approx
# ---------------------------------------------------------------------------
EFFECT_KINDS_WITH_PROB = {"counter", "dodge"}                # 效果自帶 prob 欄位, 機率語意在效果層級已建模


def check_r3(p, txt):
    violations = []
    rate = p.get("rate")
    if rate is None or abs(rate - 1) > 1e-9:
        return violations
    if (p.get("when") or {}).get("on"):
        return violations
    if p.get("type") not in ("active", "command", "passive", "charge"):
        return violations
    dm = DMG_RANGE.search(txt) or DMG_SINGLE.search(txt)
    if not dm:
        return violations
    starts = [txt.rfind(ch, 0, dm.start()) for ch in "。；;/"]
    window = txt[max(starts) + 1:dm.start()]
    found = None
    for m in PROB_RE.finditer(window):
        after = window[m.end():m.end() + 2]
        if after.startswith(RATE_NOT_TRIGGER):
            continue
        if window.count("（", 0, m.start()) > window.count("）", 0, m.start()):
            continue
        if re.search(r"第\s*\d+\s*回合", window[m.end():]):
            continue
        found = m
    if found is None:
        return violations
    v = float(found.group(2)) if found.group(2) else float(found.group(1))
    if v >= 99.5:                                       # ~100% 機率描述, 不算「未建」
        return violations
    # 機率語意若已在「效果層級」的 prob 欄位建模(如 counter/dodge 自帶 prob, 與戰法整體
    # rate=1 是兩個不同的擲骰層), 就不算「未建」——只在效果層完全沒有對應 prob 時才算違規。
    prob01 = round(v / 100, 4)
    local_kind = None
    if "反擊" in window:
        local_kind = "counter"
    elif "規避" in window:
        local_kind = "dodge"
    if local_kind in EFFECT_KINDS_WITH_PROB:
        for e in p.get("effects", []):
            if e.get("k") == local_kind and e.get("prob") is not None \
                    and abs(e["prob"] - prob01) <= 0.06:
                return violations
    if has_disclosure(p):
        return violations
    violations.append({
        "name": p["nameZh"], "rule": "R3",
        "message": f"原文子句含「{found.group(0)}」修飾主效果, 但 rate=1(=必定觸發)且無揭露",
        "evidence": window.strip(),
    })
    return violations


# ---------------------------------------------------------------------------
# R4: 持續錯位 —— 「持續N回合」與效果 dur 不符(dur>=90 常駐慣例豁免)
# ---------------------------------------------------------------------------
KIND_KW = (
    ("治療", ("heal",)),
    ("震懾", ("stun",)),
    ("嘲諷", ("taunt",)),
    ("混亂", ("chaos",)),
    ("降低", ("mitig", "stat")), ("減傷", ("mitig",)), ("減少", ("mitig", "stat")),
    ("提升", ("amp", "stat")), ("提高", ("amp", "stat")), ("增加", ("amp", "stat")),
)


def _candidate_kinds_near(clause, pos):
    """在「持續N回合」匹配位置(pos)之前, 找最近的一個動作關鍵詞(見 KIND_KW), 決定它在描述
    哪一種效果; 只取最近的一個(同一子句可能先後描述多個效果, 各自接自己的「持續N回合」,
    如火燒連營同句先講灼燒dot持續3回合、後講震懾持續1回合)。"""
    best_kw, best_pos = None, -1
    for kw, kinds in KIND_KW:
        idx = clause.rfind(kw, 0, pos)
        if idx > best_pos:
            best_pos, best_kw = idx, kinds
    return best_kw


def check_r4(p, txt):
    violations = []
    effects = p.get("effects", [])
    if not effects:
        return violations
    seen = set()                                          # (效果id, expect_dur) 去重, 避免同一效果被多個「持續N回合」重複判定
    for clause in split_clauses(txt):
        for dm in DUR_ROUND_RE.finditer(clause):
            expect_dur = int(dm.group(1))
            candidate_kinds = _candidate_kinds_near(clause, dm.start())
            if not candidate_kinds:
                continue
            matched = [e for e in effects if e.get("k") in candidate_kinds]
            if not matched:
                continue
            for e in matched:
                cur = e.get("dur")
                if cur is None:
                    continue
                if cur >= 90:                                # 常駐慣例(全程生效)與「戰鬥中」對應者豁免
                    continue
                if abs(cur - expect_dur) <= 1e-9:
                    continue
                dedup_key = (id(e), expect_dur)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                if has_disclosure(p, [e]):
                    continue
                violations.append({
                    "name": p["nameZh"], "rule": "R4",
                    "message": f"原文「持續{expect_dur}回合」但效果{e.get('k')}.dur={cur}",
                    "evidence": clause.strip(),
                })
    return violations


# ---------------------------------------------------------------------------
# R5: 空效果 —— coef==0 且 effects 空 且無 extraHits/choices/proc 且原文有明確數值 且無揭露
# ---------------------------------------------------------------------------
NUM_HINT_RE = re.compile(r"(?:傷害率|治療率|提升|提高|降低|增加|機率|概率|幾率)\s*\d")


def check_r5(p, txt):
    if (p.get("coef") or 0) != 0:
        return []
    if p.get("effects"):
        return []
    if p.get("extraHits") or p.get("choices") or p.get("everyN") or p.get("proc"):
        return []
    if not NUM_HINT_RE.search(txt):
        return []
    if has_disclosure(p):
        return []
    return [{
        "name": p["nameZh"], "rule": "R5",
        "message": "coef=0 且 effects/extraHits/choices 皆空, 但原文有明確數值描述, 且無揭露(無效果落地)",
        "evidence": txt[:80].strip(),
    }]


# ---------------------------------------------------------------------------
# R6: 雙重計算 —— coef>0 且 effects 含 counter(反應同源) 或 settle(同一傷害段)
# ---------------------------------------------------------------------------
def check_r6(p, txt):
    violations = []
    coef = p.get("coef") or 0
    if coef <= 0:
        return violations
    effects = p.get("effects", [])
    for e in effects:
        if e.get("k") in ("counter", "settle"):
            if has_disclosure(p, [e]):
                continue
            violations.append({
                "name": p["nameZh"], "rule": "R6",
                "message": f"頂層 coef={coef} 且 effects 含 {e.get('k')}(可能與主傷害段重複計算同一次傷害), 需人工確認",
                "evidence": txt[:80].strip(),
            })
    return violations


# ---------------------------------------------------------------------------
# R7: stack 方向 —— stack 效果 who=="enemy"(引擎只放大自身輸出, 方向必錯)
# ---------------------------------------------------------------------------
def check_r7(p, txt):
    violations = []
    for e in p.get("effects", []):
        if e.get("k") == "stack" and e.get("who") == "enemy":
            violations.append({
                "name": p["nameZh"], "rule": "R7",
                "message": "stack 效果 who=enemy: stack 語意是施放者自身逐回合疊層增傷, 套在敵方身上方向錯誤",
                "evidence": json.dumps(e, ensure_ascii=False),
            })
    return violations


# ---------------------------------------------------------------------------
# R8: 擇一缺失 —— 原文「其中一種/三選一/隨機獲得...之一」但無 choices 且無揭露
# ---------------------------------------------------------------------------
def check_r8(p, txt):
    if not CHOICE_KW_RE.search(txt):
        return []
    if p.get("choices"):
        return []
    if has_disclosure(p):
        return []
    return [{
        "name": p["nameZh"], "rule": "R8",
        "message": "原文含擇一措辭(其中一種/三選一/擇一等)但無 choices 且無揭露",
        "evidence": txt[:100].strip(),
    }]


# ---------------------------------------------------------------------------
# R9: 幽靈欄位 —— 效果帶引擎不讀的欄位, 或 rate/when 用在引擎不支援的路徑
# ---------------------------------------------------------------------------
# 引擎(sgz.py apply_effects)實際會讀的效果層級欄位(依 k 分類), 供比對是否有"寫了但引擎不讀"的欄位。
KNOWN_EFFECT_FIELDS = {
    "k", "who", "dur", "durMax", "scale", "when", "targetSel", "ifTargetHas",
    "undispellable", "_est", "_todo", "_note", "_note2", "_approx", "_src",
}
PER_KIND_FIELDS = {
    "amp": {"val"}, "mitig": {"val"}, "stun": set(), "silence": set(), "disarm": set(),
    "chaos": set(), "ambush": set(), "insight": set(), "immune": {"types"}, "first": set(),
    "stat": {"stat", "add", "mult"}, "dot": {"coef"}, "extra": {"val"}, "stack": {"per", "max"},
    "decay": {"v0", "rounds"}, "swap": set(), "pierce": {"val"}, "counter": {"coef", "kind", "prob"},
    "taunt": set(), "shield": {"amt", "pct"}, "dodge": {"prob"}, "surehit": set(),
    "healblock": set(), "lifesteal": {"val"}, "rateup": {"val", "prepOnly", "nativeOnly", "inheritedOnly"},
    "chargeup": {"val", "prepOnly", "nativeOnly", "leaderBonus"}, "healBoost": {"val"},
    "healGiven": {"val"}, "fakeReport": set(), "dispel": {"what"}, "heal": {"coef", "once", "rate"},
    # 批22: heal 的 rate 欄位 —— 效果級 e.when.on(急救類反應式治療, 如陷陣營/雲聚影從/長健/
    # 三軍之眾)專用的「本次觸發機率」(區分於戰法整體 t.rate), 見 engine.js/sgz.py 的
    # onHitEffectTacs/onHitEq/onHitBs 註解。
    "redirect": {"guard", "share", "normalOnly"}, "settle": {"init", "max", "base", "per"},
    "block": {"val", "times"},  # 批22: 次數型格擋(抵禦/警戒同族) —— val:1.0全擋/0.x部分減傷, times:剩餘次數
}
# 資料撰寫慣例裡與戰鬥語意無關的雜項欄位(揭露/註解/來源標記), 任何 k 都可能帶, 不算幽靈:
MISC_DISCLOSURE_FIELDS = {"note", "name"}


def check_r9(p, txt):
    violations = []
    for e in p.get("effects", []):
        k = e.get("k")
        if k is None:
            violations.append({
                "name": p["nameZh"], "rule": "R9",
                "message": "effects 陣列內有物件缺少 k 欄位(非有效效果, 可能是誤放的筆記物件)",
                "evidence": json.dumps(e, ensure_ascii=False)[:120],
            })
            continue
        allowed = KNOWN_EFFECT_FIELDS | PER_KIND_FIELDS.get(k, set()) | MISC_DISCLOSURE_FIELDS
        extra_fields = set(e.keys()) - allowed
        if extra_fields:
            violations.append({
                "name": p["nameZh"], "rule": "R9",
                "message": f"效果 k={k} 含引擎不讀的未知欄位: {sorted(extra_fields)}",
                "evidence": json.dumps(e, ensure_ascii=False)[:150],
            })
        # when 只在 heal 種類 或 戰法級(t.when, 走 delayed_eq/round_ok) 有完整支援;
        # 批18已泛化 e.when 給非 heal 種類(見 sgz.py 796-803行), 故不再視為幽靈欄位, 此處不重複檢查。
    return violations


# ---------------------------------------------------------------------------
# R10: 選標缺失 —— 原文含「鎖定/兵力最低/武力最高/智力最低/統率最低/兵力最少/最殘/
# 損失兵力較多」等選標關鍵字, 但對應戰法/效果無 targetSel 且無揭露。
#
# targetSel 只在「效果實際依準則挑選單一目標」時才有意義——heal 效果本身天生固定選
# 「我方兵力最低一人」(見 engine_limitations.md #1 heal_only 通道), 不需要/不支援
# targetSel; mitig/amp 套用在角色集合(leader/subs/ally全體)上的选标(如「損失兵力較多的
# 副將」二選一分给不同副將)也不是 targetSel 能表達的「準則挑單一目標」語意, 屬於另一種
# 近似(engine_limitations 6.5/6.6一類), 這兩種情形本規則不強制要求 targetSel, 只在
# 「效果影響對象是 who=enemy/ally 的單體選定, 且原文明確用選標關鍵字描述該對象」時才視為
# 缺口。為降低誤報, 只在下列情形觸發:
#   - 命中選標關鍵字, 且
#   - 該子句同一版本區塊內存在 amp/mitig/dot/stun/silence/disarm/healblock/immune 等
#     「非heal」效果, 且這些效果都不是 who 對應 leader/subs 角色集合(這類角色選標非
#     targetSel 語意, 見上), 且
#   - 無 targetSel(戰法級/效果級/extraHits級皆無) 且無揭露
# ---------------------------------------------------------------------------
SELECT_KW_RE = re.compile(
    r"鎖定|兵力最低|武力最高|智力最低|統率最低|兵力最少|最殘|損失兵力較多|損失兵力最多|損失兵力較高"
)
ROLE_WHO = {"leader", "subs", "self"}


def _has_targetsel(p):
    if p.get("targetSel"):
        return True
    if any(e.get("targetSel") for e in p.get("effects", []) or []):
        return True
    if any(eh.get("targetSel") for eh in p.get("extraHits", []) or []):
        return True
    return False


def check_r10(p, txt):
    violations = []
    if not SELECT_KW_RE.search(txt):
        return violations
    if _has_targetsel(p):
        return violations
    # heal-only 戰法(唯一非role效果是 heal) 天生已用「我方最殘一人」通道, 不算缺口
    non_heal_non_role = [
        e for e in p.get("effects", []) or []
        if e.get("k") != "heal" and e.get("who") not in ROLE_WHO
    ]
    if not non_heal_non_role:
        return violations
    if has_disclosure(p, non_heal_non_role):
        return violations
    for clause in split_clauses(txt):
        m = SELECT_KW_RE.search(clause)
        if not m:
            continue
        violations.append({
            "name": p["nameZh"], "rule": "R10",
            "message": f"原文含選標關鍵字「{m.group(0)}」但無 targetSel 且無揭露",
            "evidence": clause.strip(),
        })
        break
    return violations


# ---------------------------------------------------------------------------
# R11: 機制錯置 —— 原文「造成…傷害/攻擊」(真實傷害輸出) 但對應 effect 是
# amp/mitig(增減傷 debuff), 且該戰法無 coef/dot/extraHits 承載該傷害、無 _approx 揭露
# (抓火燒連營類: 「引爆對敵軍全體造成謀略攻擊」是一次真實傷害輸出, 不該只建模成amp易傷)。
#
# 只在「傷害動詞緊鄰目標」且同一版本區塊內完全没有 coef(戰法頂層, 非該子句自身已由頂層
# coef承載的情形除外)/dot/extraHits 可以對應「這一次」傷害輸出時才觸發, 避免對「先造成一次
# 傷害(頂層coef已表達)、又額外加註amp易傷副效果」的正常戰法(如水淹七軍dot+amp並存)誤判。
# ---------------------------------------------------------------------------
REAL_DMG_ACTION_RE = re.compile(r"(?:對[^。；;，,]{0,12}造成[^。；;，,]{0,10}(?:攻擊|傷害)|引爆|焚營)")


def check_r11(p, txt):
    violations = []
    effects = p.get("effects", []) or []
    if not effects:
        return violations
    amp_mitig = [e for e in effects if e.get("k") in ("amp", "mitig")]
    if not amp_mitig:
        return violations
    dot_count = sum(1 for e in effects if e.get("k") == "dot")
    has_extra = bool(p.get("extraHits"))
    coef = p.get("coef") or 0
    for block in split_version_blocks(txt):
        for m in REAL_DMG_ACTION_RE.finditer(block):
            trigger_word = m.group(0)
            is_detonate = ("引爆" in trigger_word) or ("焚營" in trigger_word)
            if is_detonate:
                # 「引爆/焚營」宣告一次「獨立於DoT本體」的第二段傷害輸出——若同一版本區塊內
                # 已有DoT(灼燒本體, 對應「每回合持續造成傷害」子句)佔用了頂層coef/唯一dot,
                # 「引爆」這一段就必須有自己的傷害載體(第二個dot 或 extraHits), 否則等於這次
                # 傷害輸出被憑空丟棄、只剩下amp(易傷debuff)這個附帶效果。
                carrier_present = dot_count >= 2 or has_extra
                if not carrier_present:
                    if has_disclosure(p, amp_mitig):
                        continue
                    violations.append({
                        "name": p["nameZh"], "rule": "R11",
                        "message": f"原文「{trigger_word}」宣告獨立於主段的第二次傷害輸出, 但無額外 dot/extraHits 承載(僅有 amp/mitig 增減傷), 且無揭露",
                        "evidence": block.strip()[:120],
                    })
            else:
                # 一般「對X造成傷害/攻擊」: 若頂層coef==0 且無dot/extraHits, 且效果只有amp/mitig, 判定錯置
                if coef == 0 and dot_count == 0 and not has_extra:
                    if has_disclosure(p, amp_mitig):
                        continue
                    violations.append({
                        "name": p["nameZh"], "rule": "R11",
                        "message": f"原文「{trigger_word}」描述真實傷害輸出, 但 coef=0 且無 dot/extraHits 承載, 效果只有 amp/mitig(增減傷), 且無揭露",
                        "evidence": block.strip()[:120],
                    })
            break  # 每個版本區塊只報一次(避免同區塊多個傷害動詞重複產生違規)
    return violations


# ---------------------------------------------------------------------------
# R12: 清除/免疫缺失 —— 原文「清除/淨化/解除…狀態」但 effects 缺 dispel 且無揭露;
# 原文「免疫X狀態」(非「免疫所有控制」= insight 全免) 但缺 immune/insight 且無揭露。
# ---------------------------------------------------------------------------
DISPEL_KW_RE = re.compile(r"清除[^。；;，,]{0,10}(?:狀態|效果|負面)|淨化(?:自己|自身|我軍|其)?[^。；;，,]{0,6}(?:狀態|負面|效果)?|解除[^。；;，,]{0,10}(?:狀態|負面|效果)")
# 「可(以)免疫傷害」是抵禦/規避(shield/dodge)機制的固定括號註解措辭(解釋該狀態的效果, 非
# 宣告一個獨立的狀態免疫), 見 勇者得前/折衝禦侮(抵禦)/金丹秘術/妖術(規避/抵禦), 這幾筆
# 已用 shield/dodge 精確建模, 不屬 R12 要抓的「免疫X狀態但缺 immune/insight」語意, 排除
# ——用「免疫」後直接跟已知控制類狀態名(混亂/計窮/震懾/繳械/嘲諷/禁療/遇襲等, 對應 ctrl_k
# +ambush+healblock+taunt) 或「…狀態/效果」通用後綴來界定, 天然排除「免疫傷害」這個不同語意。
CONTROL_STATUS_NAMES = "混亂|計窮|震懾|繳械|嘲諷|禁療|遇襲"
IMMUNE_KW_RE = re.compile(rf"免疫(?!所有控制)(?:{CONTROL_STATUS_NAMES})|免疫(?!所有控制)(?!傷害)[^。；;，,、]{{0,8}}(?:狀態|效果)")
IMMUNE_ALL_RE = re.compile(r"免疫所有控制|洞察")


def check_r12(p, txt):
    violations = []
    effects = p.get("effects", []) or []
    has_dispel = any(e.get("k") == "dispel" for e in effects)
    has_immune_all = any(e.get("k") == "insight" for e in effects)
    immune_types_covered = set()
    for e in effects:
        if e.get("k") == "immune":
            immune_types_covered.update(e.get("types") or [])

    for clause in split_clauses(txt):
        dm = DISPEL_KW_RE.search(clause)
        if dm and not has_dispel:
            if not has_disclosure(p):
                violations.append({
                    "name": p["nameZh"], "rule": "R12",
                    "message": f"原文含清除/淨化/解除措辭「{dm.group(0)}」但 effects 無 dispel 且無揭露",
                    "evidence": clause.strip(),
                })

        im = IMMUNE_KW_RE.search(clause)
        if im and not IMMUNE_ALL_RE.search(clause):
            if not has_immune_all and not immune_types_covered:
                if not has_disclosure(p):
                    violations.append({
                        "name": p["nameZh"], "rule": "R12",
                        "message": f"原文含免疫措辭「{im.group(0)}」但 effects 無 immune/insight 且無揭露",
                        "evidence": clause.strip(),
                    })
    return violations


RULES = [
    ("R1", check_r1), ("R2", check_r2), ("R3", check_r3), ("R4", check_r4),
    ("R5", check_r5), ("R6", check_r6), ("R7", check_r7), ("R8", check_r8),
    ("R9", check_r9), ("R10", check_r10), ("R11", check_r11), ("R12", check_r12),
]


def lint():
    parsed = load_json(PARSED_PATH, [])
    raw_by_name, invalid_names = load_raw_texts()
    all_violations = []
    for p in parsed:
        name = p["nameZh"]
        if p.get("type") == "none" or name in invalid_names:
            continue
        r = raw_by_name.get(name)
        txt = (r.get("effectText") if r else "") or ""
        if not txt:
            continue
        for rule_id, fn in RULES:
            all_violations.extend(fn(p, txt))
    return all_violations


def summarize(violations):
    by_rule = {}
    for v in violations:
        by_rule.setdefault(v["rule"], []).append(v)
    lines = []
    lines.append("=== lint_tactics.py 違規摘要 ===")
    lines.append(f"總違規數: {len(violations)}")
    for rule_id, _ in RULES:
        vs = by_rule.get(rule_id, [])
        lines.append(f"\n--- {rule_id}: {len(vs)} 筆 ---")
        for v in vs[:15]:
            lines.append(f"  [{v['name']}] {v['message']}")
            lines.append(f"      證據: {v['evidence'][:90]}")
        if len(vs) > 15:
            lines.append(f"  ... 其餘 {len(vs) - 15} 筆省略(見 --json 輸出)")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="輸出完整違規清單(JSON)到指定路徑")
    ap.add_argument("--summary", action="store_true", help="印人看的摘要(預設行為)")
    args = ap.parse_args()

    violations = lint()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(violations, f, ensure_ascii=False, indent=1)
        print(f"已寫入 {len(violations)} 筆違規到 {args.json}")

    if args.summary or not args.json:
        print(summarize(violations))

    return 0 if not violations else 1


if __name__ == "__main__":
    sys.exit(main())
