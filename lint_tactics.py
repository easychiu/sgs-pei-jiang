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
# 批28 A2: R1 原用 has_disclosure(p, matched)(任一揭露欄位即豁免, 不論主題), 改主題綁定
# ——揭露文字須提到「scale/影響」相關關鍵字才算數(全庫核對: 現存scale相關揭露文字皆含
# "scale"字面或「受X影響」措辭, 見批28 A2稽核樣本), 避免同一效果因無關議題(如R19的
# dmgType/R21的點數修正)留下的舊_note連帶豁免掉真正的scale缺失。
R1_SCALE_TOPIC_KW = ("scale", "影響")


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
            if _topic_disclosed(p, R1_SCALE_TOPIC_KW):
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

    # 批28 A2: 原用 has_disclosure(p)(任一頂層揭露欄位即豁免, 不論主題), 改主題綁定
    # ——沿用 R13 的目標數主題關鍵字(單體/目標/群體/全體/e.n等), 避免無關議題的舊_note
    # (如R14/R19/R21等其他規則的揭露)連帶豁免掉R2真正的目標數錯位。
    if desc == "全體":
        if n is not None and n < 3:
            if not _topic_disclosed(p, R13_TARGET_TOPIC_KW):
                violations.append({
                    "name": p["nameZh"], "rule": "R2",
                    "message": f"原文傷害段描述「全體」(應n>=3) 但 n={n}",
                    "evidence": window.strip(),
                })
        return violations

    if expect_n is not None and n != expect_n:
        if not _topic_disclosed(p, R13_TARGET_TOPIC_KW):
            violations.append({
                "name": p["nameZh"], "rule": "R2",
                "message": f"原文傷害段描述「{desc}」(應n={expect_n}) 但 parsed n={n}",
                "evidence": window.strip(),
            })
    if expect_nmax is not None and nmax != expect_nmax:
        if not _topic_disclosed(p, R13_TARGET_TOPIC_KW):
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
# 批28 A2: R3 原用 has_disclosure(p)(任一頂層揭露欄位即豁免, 不論主題), 改主題綁定——
# 揭露文字須提到機率/rate/折算相關關鍵字才算數, 避免無關議題的舊_note連帶豁免掉真正的
# 「機率未建」缺口。
R3_RATE_TOPIC_KW = ("rate", "機率", "概率", "幾率", "prob", "折算", "-ev", "EV", "穩態")


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
    if _topic_disclosed(p, R3_RATE_TOPIC_KW):
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
    # 批28 A2: 補 dot(持續傷害類狀態)關鍵字——原本完全缺席, 導致「陷入水攻/沙暴/灼燒/中毒
    # 狀態,持續N回合」這類子句的「持續N回合」找不到dot對應關鍵字, 回退比對到句子更早處的
    # 「降低/提升」等無關關鍵字, 誤把dot自己的持續回合拿去核對其他效果的dur(飛沙走石/水淹
    # 七軍等假陽性案例, 見批28 A2稽核記錄)。用「陷入」(常見引導詞)+常見dot狀態名。
    ("陷入", ("dot",)), ("水攻", ("dot",)), ("沙暴", ("dot",)), ("灼燒", ("dot",)), ("中毒", ("dot",)),
    ("潰逃", ("dot",)),
    ("計窮", ("silence",)), ("繳械", ("disarm",)),  # 批28 A2: 補silence/disarm關鍵字(原本缺席同dot類問題)
    ("必中", ("surehit",)), ("抵禦", ("block",)), ("警戒", ("block",)),
)
# 批28 A2: R4 原用 has_disclosure(p, [e])(該效果任一揭露欄位即豁免, 不論主題), 改主題綁定
# ——揭露文字須提到持續回合/dur相關關鍵字才算數, 避免同一效果因無關議題(如R19 dmgType
# 補丁)的_note連帶豁免掉真正的持續回合錯位。
R4_DUR_TOPIC_KW = ("dur", "回合", "持續", "常駐")


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
                if _topic_disclosed(p, R4_DUR_TOPIC_KW):
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
# 批28 A2: R5 原用 has_disclosure(p)(任一頂層揭露欄位即豁免, 不論主題), 改主題綁定——
# 揭露文字須提到「為何空/無原語/未落地」相關關鍵字才算數(全庫核對現存真正on-topic的
# 揭露文字皆會提到下列詞之一, 見批28 A2稽核樣本 奉令平虜/結盟/枯木逢春/鳩毒), 避免無關
# 議題的舊_note連帶豁免掉真正的「完全無效果落地」缺口。
R5_EMPTY_TOPIC_KW = ("無對應原語", "無此原語", "無法表達", "未落地", "不參與戰鬥", "type:none",
                     "保留_todo", "同一戰法", "誤標", "無效果")


def check_r5(p, txt):
    if (p.get("coef") or 0) != 0:
        return []
    if p.get("effects"):
        return []
    if p.get("extraHits") or p.get("choices") or p.get("everyN") or p.get("proc"):
        return []
    if not NUM_HINT_RE.search(txt):
        return []
    if _topic_disclosed(p, R5_EMPTY_TOPIC_KW):
        return []
    return [{
        "name": p["nameZh"], "rule": "R5",
        "message": "coef=0 且 effects/extraHits/choices 皆空, 但原文有明確數值描述, 且無揭露(無效果落地)",
        "evidence": txt[:80].strip(),
    }]


# ---------------------------------------------------------------------------
# R6: 雙重計算 —— coef>0 且 effects 含 counter(反應同源) 或 settle(同一傷害段)
# ---------------------------------------------------------------------------
# 批28 A2: 原用 has_disclosure(p, [e])(該效果任一揭露欄位即豁免, 不論主題), 改主題綁定
# ——揭露文字須提到重複計算/counter/settle相關關鍵字才算數。
R6_DOUBLE_COUNT_TOPIC_KW = ("重複計算", "雙重", "counter", "settle", "同一次傷害", "已人工確認")


def check_r6(p, txt):
    violations = []
    coef = p.get("coef") or 0
    if coef <= 0:
        return violations
    effects = p.get("effects", [])
    for e in effects:
        if e.get("k") in ("counter", "settle"):
            if _topic_disclosed(p, R6_DOUBLE_COUNT_TOPIC_KW):
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
# 批28 A2: 原用 has_disclosure(p)(任一頂層揭露欄位即豁免, 不論主題), 改主題綁定——揭露
# 文字須提到擇一/choices相關關鍵字才算數。
R8_CHOICE_TOPIC_KW = ("choices", "擇一", "三選一", "二選一", "分支", "其中一種")


def check_r8(p, txt):
    if not CHOICE_KW_RE.search(txt):
        return []
    if p.get("choices"):
        return []
    if _topic_disclosed(p, R8_CHOICE_TOPIC_KW):
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
# 批23: n/nMax(A1, 非CTRL效果的效果級目標數) 與 rate(A4, 效果級機率折算一致性) 是跨所有k
# 種類通用的欄位(見 apply_effects 的 has_en/hasEN 與 e.rate 通用閘門判斷, 非per-kind限定),
# 故加進全域 KNOWN_EFFECT_FIELDS, 不需要為每個 k 逐一在 PER_KIND_FIELDS 補列。
KNOWN_EFFECT_FIELDS = {
    "k", "who", "dur", "durMax", "scale", "when", "targetSel", "ifTargetHas",
    "undispellable", "_est", "_todo", "_note", "_note2", "_approx", "_src",
    "n", "nMax", "rate", "ifLeader",  # ifLeader: 批26 B1, 施放者須為隊伍主將(index 0)才套用該效果段
}
PER_KIND_FIELDS = {
    "amp": {"val", "dmgType", "normalOnly"}, "mitig": {"val", "dmgType", "normalOnly"}, "stun": set(), "silence": set(), "disarm": set(),  # dmgType: 批24 D2, 兵刃/謀略傷害類型過濾; normalOnly: 批28 B3, 僅普攻傷害生效/受影響
    "chaos": set(), "ambush": set(), "insight": set(), "immune": {"types"}, "first": set(),
    "stat": {"stat", "add", "mult"},
    "dot": {"coef", "kind"},  # 批23 A3: e.kind(dot段自帶傷害類型, 優先於t.kind, 見damage()呼叫端)
    "extra": {"val"}, "stack": {"per", "max", "stackPer"},  # stackPer: 批26 B2, "round"預設/"cast"每次發動遞增
    "decay": {"v0", "rounds"}, "swap": set(), "pierce": {"val"}, "counter": {"coef", "kind", "prob", "guardFor"},  # guardFor: 批28 B1, 守護式反擊("leader"=登記進主將counter_guards, 由代為受擊者反擊)
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
# 批28 A2: 原用 has_disclosure(p, non_heal_non_role)(這些效果任一揭露欄位即豁免, 不論主題),
# 改主題綁定——揭露文字須提到選標/targetSel相關關鍵字才算數。
R10_SELECT_TOPIC_KW = ("targetSel", "選標", "鎖定", "最低", "最高", "最殘", "選一名")


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
    if _topic_disclosed(p, R10_SELECT_TOPIC_KW):
        return violations
    for clause in split_clauses(txt):
        m = SELECT_KW_RE.search(clause)
        if not m:
            continue
        # 批28 A2: 該子句若是描述heal目標選取(如「治療我軍兵力最低單體」), 屬heal-only既有
        # 豁免通道管轄範圍(見上方heal-only說明), 即使戰法內有其他非heal效果(如rateup)也不該
        # 被這句「治療...最低」選標語意連累——用「治療」是否緊鄰選標關鍵字局部窗口判斷此子句
        # 是否為heal選標敘述(而非真的在描述其他非heal效果的選標), 避免像錦囊妙計(select關鍵字
        # 只出現在heal子句, 但因戰法內另有rateup效果而被誤判整體缺targetSel)。
        local = clause[:m.start()]
        if "治療" in local[-10:] or "恢復" in local[-10:]:
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
# 批28 A2: 排除「無法造成傷害/攻擊」(負面狀態的說明性後綴, 如虛弱「無法造成傷害」, 是描述
# 該debuff效果本身的語意註解, 非施放者對目標造成的真實傷害輸出), 避免與「對X造成傷害」的
# 正常語序混淆(仁德載世「施加虛弱(無法造成傷害)狀態」誤判為真實傷害輸出案例)。「無法」緊接
# 在「造成」之前(如"無法造成傷害"), 故用負向後顧(lookbehind)排除, 而非原本誤植的向前看
# (「無法」實際出現在「造成」之前, 非之後, 之前的 lookahead 寫法完全沒排除到)。
REAL_DMG_ACTION_RE = re.compile(r"(?:對[^。；;，,]{0,12}(?<!無法)造成[^。；;，,]{0,10}(?:攻擊|傷害)|引爆|焚營)")
# 批28 A2: 原用 has_disclosure(p, amp_mitig)(這些效果任一揭露欄位即豁免, 不論主題), 改
# 主題綁定——揭露文字須提到「第二次傷害/dot/extraHits/傷害段」相關關鍵字才算數。
R11_MISPLACE_TOPIC_KW = ("dot", "extraHits", "第二次傷害", "第二段", "傷害段", "coef", "承載")


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
                    if _topic_disclosed(p, R11_MISPLACE_TOPIC_KW):
                        continue
                    violations.append({
                        "name": p["nameZh"], "rule": "R11",
                        "message": f"原文「{trigger_word}」宣告獨立於主段的第二次傷害輸出, 但無額外 dot/extraHits 承載(僅有 amp/mitig 增減傷), 且無揭露",
                        "evidence": block.strip()[:120],
                    })
            else:
                # 一般「對X造成傷害/攻擊」: 若頂層coef==0 且無dot/extraHits, 且效果只有amp/mitig, 判定錯置
                if coef == 0 and dot_count == 0 and not has_extra:
                    if _topic_disclosed(p, R11_MISPLACE_TOPIC_KW):
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
# 批28 A2: 原用 has_disclosure(p)(任一頂層揭露欄位即豁免, 不論主題), 改主題綁定——dispel
# 與 immune 是兩個不同子題, 各自要求揭露文字提到對應關鍵字才算數。
R12_DISPEL_TOPIC_KW = ("dispel", "清除", "淨化", "解除", "負面狀態", "負面效果")
R12_IMMUNE_TOPIC_KW = ("immune", "insight", "免疫", "洞察")


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
            if not _topic_disclosed(p, R12_DISPEL_TOPIC_KW):
                violations.append({
                    "name": p["nameZh"], "rule": "R12",
                    "message": f"原文含清除/淨化/解除措辭「{dm.group(0)}」但 effects 無 dispel 且無揭露",
                    "evidence": clause.strip(),
                })

        im = IMMUNE_KW_RE.search(clause)
        if im and not IMMUNE_ALL_RE.search(clause):
            if not has_immune_all and not immune_types_covered:
                if not _topic_disclosed(p, R12_IMMUNE_TOPIC_KW):
                    violations.append({
                        "name": p["nameZh"], "rule": "R12",
                        "message": f"原文含免疫措辭「{im.group(0)}」但 effects 無 immune/insight 且無揭露",
                        "evidence": clause.strip(),
                    })
    return violations


# ---------------------------------------------------------------------------
# R13(批23 A1): 非CTRL效果目標數缺 e["n"] —— 原文明確「單體」「目標」「N人」描述某個非控制類
# 效果(amp/mitig/stat/dot/healblock/rateup/extra/insight/lifesteal/healBoost/healGiven/
# pierce/surehit/dodge/shield/swap/fakeReport/immune)的受眾範圍, 但該效果缺 e["n"](批23
# 新增: 有 e["n"] 才會限制目標數, 無則維持全體, 見 sgz.py/engine.js apply_effects 的
# has_en 判斷式)。
#
# CTRL_K 家族(stun/silence/disarm/taunt/chaos)不受本規則管轄(它們讀 t["n"]/t["nMax"], 早已
# 由既有機制正確處理群體/單體選標, 見 apply_effects 的 ctrl_k 分支)。heal/settle/redirect/
# dispel 也排除(各自有獨立選標邏輯, 不讀 e["n"]; heal 固定選"我方兵力最低者", dispel 目前
# 對 who 指定的整組 dests 生效, settle/redirect 有各自的目標決定方式, 見 engine_limitations.md)。
#
# 只在效果的 who 對應「敵/我方全體池」(enemy/ally, 含未指定預設ally)時才有意義——who 為
# self/leader/subs 本身就是明確的角色選標, 不受"單體/N人"全體池語意影響, 不算本規則管轄範圍。
#
# 低誤報設計: 只在「效果緊鄰的局部窗口」內找到單體/N人措辭時才觸發, 用類似 R1 的「緊鄰窗口」
# 手法(該效果對應的關鍵詞在原文中定位, 非全文任意位置的單體/N人描述都算數)。
# ---------------------------------------------------------------------------
R13_NONCTRL_KINDS = {
    "amp", "mitig", "stat", "dot", "healblock", "rateup", "extra", "insight",
    "lifesteal", "healBoost", "healGiven", "pierce", "surehit", "dodge",
    "shield", "swap", "fakeReport", "immune",
}
R13_ROLE_WHO = {"self", "leader", "subs"}
# 效果動作關鍵詞(用於在原文中定位該效果對應的子句/局部窗口), 對應到 R13_NONCTRL_KINDS 的
# 常見中文動詞/名詞片語(不要求完全枚舉, 只取最常見、確定性高的觸發詞, 降低誤報)。
# 批28 A1 盲點b修復: dot 關鍵詞表原本只有「持續傷害/灼燒/中毒/水攻」四個緊鄰子字串, 漏掉
# 「每回合持續造成兵刃/謀略傷害」這種「持續」與「傷害」被「造成兵刃/謀略」隔開的常見措辭
# (v12盲測踩雷: 刺傷「每回合持續造成兵刃傷害」不含子字串「持續傷害」, 完全定位不到窗口)。
# 改用正則(見 R13_DOT_PATTERN_RE)取代純子字串表, 涵蓋「持續X造成Y傷害」的間隔寫法。
R13_ACTION_KW = (
    "降低", "提升", "提高", "增加", "減少",  # amp/mitig/stat 常見動詞
    "持續傷害", "灼燒", "中毒", "水攻",       # dot
    "禁療", "無法恢復兵力",                   # healblock
    "發動機率",                               # rateup
    "連擊", "追擊",                           # extra
    "免疫",                                   # insight/immune(注意: R12已管「免疫X狀態但缺immune/insight」,
                                               # R13管的是"有immune/insight但缺e.n"的目標數問題, 不重複)
    "倒戈",                                   # lifesteal
    "護盾",                                   # shield
    "武智互換",                               # swap
    "偽報",                                   # fakeReport
)
# dot 專用定位正則: 「持續」與「傷害」之間允許最多8字的任意間隔(涵蓋「造成兵刃」「造成謀略」
# 「造成」等常見插入語), 比純子字串表更能命中「每回合持續造成兵刃傷害」這類寫法。
R13_DOT_PATTERN_RE = re.compile(r"持續[^。；;，,]{0,8}傷害|灼燒|中毒|水攻")


def _r13_window_for_effect(txt, e):
    """在原文中找到與此效果種類相關的局部窗口(緊鄰"單體"/"N人"等描述的那一段)。
    用 R13_ACTION_KW 裡「這個效果k常見的動詞」定位——批28 A1 盲點b修復: 原本固定取動詞
    前後各20字, 漏掉「其」代詞回指單體目標的情形(如「壓制」原文「對敵軍單體造成一次兵刃
    攻擊...並使其統率降低10→20點」, 「單體」在句子前段, 距離「降低」超過20字, 舊窗口切不到;
    但「其」在近距離代指前面已宣告的單體目標)。改為: 先取「該動詞所在的完整子句」(比
    split_clauses 更寬的整句, 因為目標描述與動作描述通常同句), 再輔以動詞前後各20字的
    窄窗口作為次要嘗試——整句範圍涵蓋句首的「單體/群體/全體」宣告, 同時仍用窄窗口優先(避免
    整句範圍在少數情形误抓到不相關的其他語段)。回傳「整句」而非窄窗口, 交由呼叫端的
    SINGLE_TARGET_RE/GROUP_TARGET_RE 等在整句範圍內比對(這些正則本身已夠精確, 不需要窄窗口
    降噪; 窄窗口反而是本次盲點b的根因)。
    找不到明確錨點時回傳 None(不誤報)。"""
    k = e.get("k")
    kw_map = {
        "amp": ("降低", "提升", "提高", "增加"), "mitig": ("降低", "減少"),
        "stat": ("提升", "提高", "降低", "點"), "dot": None,  # dot 改用 R13_DOT_PATTERN_RE 正則定位, 見下方特判
        "healblock": ("禁療", "無法恢復兵力"), "rateup": ("發動機率",),
        "extra": ("連擊", "追擊"), "insight": ("免疫",), "immune": ("免疫",),
        "lifesteal": ("倒戈",), "shield": ("護盾",), "swap": ("武智互換",),
        "fakeReport": ("偽報",), "healBoost": ("治療",), "healGiven": ("治療",),
        "pierce": ("看破", "無視"), "surehit": ("必中",), "dodge": ("規避",),
    }
    if k not in kw_map:
        return None
    if k == "dot":
        for clause in split_clauses(txt):
            m = R13_DOT_PATTERN_RE.search(clause)
            if m:
                return clause
        return None
    kws = kw_map.get(k)
    if not kws:
        return None
    for clause in split_clauses(txt):
        for kw in kws:
            idx = clause.find(kw)
            if idx == -1:
                continue
            # 回傳整句(而非動詞前後20字窄窗), 讓「單體」等宣告即使出現在句子前段(透過「其」
            # 代詞回指)也能被下游的 SINGLE_TARGET_RE/GROUP_TARGET_RE 掃到(見上方docstring)。
            return clause
    return None


# 批28 A2: R13 原本用 has_disclosure(p, [e])(任何揭露欄位即豁免, 不論主題)——同一戰法若
# 因完全無關的原因(如R21點數修正)在頂層留了 _note, 會連帶把R13的目標數缺口也豁免掉(v12
# 盲點a: 短兵相見/牽制的 _note 都是"stat.mult→stat.add修正"的R21揭露, 與R13的「單體/N人」
# 主題無關)。改用主題綁定豁免, 揭露文字須提到目標數相關關鍵字才算數。
# 同一組關鍵字也共用給 R2(頂層 n/nMax 目標數錯位, 語意同屬「目標數」主題, 見 R2 呼叫端),
# 命名保留 R13 前綴僅為歷史沿革, 非 R13 專屬。
R13_TARGET_TOPIC_KW = ("單體", "目標", "群體", "全體", "e.n", "e['n']", '"n"', "人)", "e[\"n\"]")


def check_r13(p, txt):
    violations = []
    effects = p.get("effects", []) or []
    if not effects:
        return violations
    seen_kinds = set()  # 同一戰法同一k只報一次(避免多個同k效果重複刷violation, 取第一個代表)
    for e in effects:
        k = e.get("k")
        if k not in R13_NONCTRL_KINDS:
            continue
        who = e.get("who", "ally")
        if who in R13_ROLE_WHO:
            continue
        if e.get("n") is not None:
            continue
        if k in seen_kinds:
            continue
        window = _r13_window_for_effect(txt, e)
        if window is None:
            continue
        expect_n, desc = None, None
        grp_range = GROUP_RANGE_RE.search(window)
        grp = GROUP_TARGET_RE.search(window)
        if grp_range:
            expect_n, desc = int(grp_range.group(1)), f"({grp_range.group(1)}~{grp_range.group(2)}人)"
        elif grp and grp.group(1):
            expect_n, desc = int(grp.group(1)), f"群體({grp.group(1)}人)"
        elif SINGLE_TARGET_RE.search(window):
            expect_n, desc = 1, "單體"
        else:
            continue  # 窗口內無明確單體/N人措辭(可能是"全體", 本規則不管全體情形, 全體=無e.n的既有向後相容行為)
        # 主題綁定豁免(批28 A2): 只有「提到目標數主題」的揭露才算數, 不再被無關的R21/其他
        # 揭露文字連帶豁免(見上方 R13_TARGET_TOPIC_KW 說明)。
        if _topic_disclosed(p, R13_TARGET_TOPIC_KW):
            continue
        seen_kinds.add(k)
        violations.append({
            "name": p["nameZh"], "rule": "R13",
            "message": f"原文描述效果(k={k})目標為「{desc}」但缺 e['n']={expect_n}(非CTRL效果無e.n時套用到全體, 過去系統性高估覆蓋人數, 見批23 A1)",
            "evidence": window.strip(),
        })
    return violations


# ---------------------------------------------------------------------------
# R14(批23): _note 數字矛盾 —— _note/_todo 內聲稱的具體數值(coef/dur/rate)與該戰法/效果
# 實際欄位值不一致(stale揭露: 揭露文字曾經正確, 但後續資料改動未同步更新, 見engine_limitations.md
# 維護規約「_note 聲稱的數值必須與資料實際值一致」)。
#
# 低誤報設計: 只抓「_note/_todo 明確寫出 coef=X/dur=X/rate=X」這種精確斷言句型(非模糊描述如
# "较高"、"偏低"), 且能從文字裡穩定抽出數字時才比對; 抽不到就不判定(保守)。
#
# 不含 n/nMax: 批23後 "e.n=X"/"n=X" 這類措辭在自由文字裡經常指「效果級 e.n」(而非戰法頂層
# n), 頂層 note 常敘述"補e.n=1"這種effect-scope斷言, 若拿來與頂層p["n"]比對會系統性誤判
# (兩個完全不同概念的欄位恰好同名)。coef/dur/rate 在頂層/效果層級的名稱雖然也重疊, 但語意
# 上較不易混淆(coef/rate 通常討論的就是"這句話所在scope"的coef/rate, 不像n有跨層級歧義),
# 保留這三個, 排除n/nMax以避免高誤報。
# 「原X=Y」「舊X=Y」「之前X=Y」「過去X=Y」「取代...折算X=Y」是敘述歷史舊值的常見措辭(非對
# 目前狀態的斷言), 用「比對數字前 HISTORICAL_LOOKBACK 字內是否出現歷史詞」排除, 而非單純
# 緊鄰前綴(舊版/過去等修飾詞與coef=之間常夾雜"的EV機率折算"這類描述性文字, 緊鄰式負向前瞻
# 抓不到, 需要窗口式排除)。
STALE_ASSERT_RE = re.compile(r"(coef|dur|rate)\s*[=＝]\s*(-?\d+(?:\.\d+)?)")
HISTORICAL_KW = ("原", "舊", "之前", "過去", "曾", "取代", "先前", "移除", "刪除")
HISTORICAL_LOOKBACK = 24
# 「頂層coef=0.68與dot重複計算,一併歸零」這類句型: 歷史詞出現在數字"之後"(描述該數值即將/
# 已經被清零\移除\改掉), 而非之前。lookahead窗口內出現這些詞同樣視為歷史/已變更值的敘述。
HISTORICAL_AFTER_KW = ("歸零", "已移除", "已刪除", "改為0", "清零", "已修正", "誤用", "誤標", "誤植")
HISTORICAL_LOOKAHEAD = 20


def _collect_disclosure_texts(p):
    """收集戰法頂層與所有效果層級的揭露文字(_note/_todo/_note2/_note_self), 供R14掃描斷言句。
    回傳 [(text, scope, field_getter)] —— scope 用於錯誤訊息標示, field_getter(field_name)
    用於查詢實際值(頂層查 p, 效果層級查該效果 dict)。"""
    out = []
    for k in ("_note", "_todo", "_note2", "_note_self"):
        if isinstance(p.get(k), str) and p.get(k):
            out.append((p[k], "頂層", p))
    for i, e in enumerate(p.get("effects", []) or []):
        for k in ("_note", "_todo", "_note2", "_note_self", "_approx"):
            if isinstance(e.get(k), str) and e[k]:
                out.append((e[k], f"effects[{i}](k={e.get('k')})", e))
    return out


def check_r14(p, txt):
    violations = []
    for text, scope, field_src in _collect_disclosure_texts(p):
        for m in STALE_ASSERT_RE.finditer(text):
            lookback = text[max(0, m.start() - HISTORICAL_LOOKBACK):m.start()]
            if any(kw in lookback for kw in HISTORICAL_KW):
                continue  # 「原/舊/之前/過去/曾/取代/先前 coef=X」是敘述歷史舊值, 非對目前狀態的斷言
            lookahead = text[m.end():m.end() + HISTORICAL_LOOKAHEAD]
            if any(kw in lookahead for kw in HISTORICAL_AFTER_KW):
                continue  # 「coef=X...一併歸零/已移除」是敘述"此數值已被清除/改掉", 歷史詞出現在數字之後
            field, claimed_str = m.group(1), m.group(2)
            claimed = float(claimed_str) if "." in claimed_str else int(claimed_str)
            actual = field_src.get(field)
            if actual is None:
                continue  # 欄位在該scope不存在(可能斷言的是另一個scope的欄位), 不誤判
            try:
                actual_num = float(actual)
                claimed_num = float(claimed)
            except (TypeError, ValueError):
                continue
            if abs(actual_num - claimed_num) > 1e-9:
                violations.append({
                    "name": p["nameZh"], "rule": "R14",
                    "message": f"{scope} 揭露文字聲稱 {field}={claimed}, 但實際 {field}={actual}(數字矛盾, stale揭露未同步)",
                    "evidence": text[:150].strip(),
                })
    return violations


# ---------------------------------------------------------------------------
# R15(批24 C): 屬性點數(stat add)主效果遺漏 —— 原文含「提高/降低/提升/減少 N點
# 統率/武力/智力/速度」(或「統率/速度提高N點」語序相反寫法), 但 effects 沒有對應
# stat 效果(k=="stat"且stat欄位匹配, add或mult皆可能, 只要求"有對應這個屬性的stat
# 效果存在", 不要求add的精確數值——精確數值比對已有既有的欄位回填/人工核對流程,
# R15只抓"完全遺漏, 主效果整段消失"這種最嚴重的情形), 且無揭露。
#
# 兩種常見語序皆需支援(見批24全庫核對, 28筆樣本):
#   (a) 「提高/降低/提升/減少 N[→M] 點 X」(動詞在前, 如"降低100點統率")
#   (b) 「X(、X2...) 提高/降低/提升/減少 N[→M] 點」(屬性名在前, 逗號/頓號分隔多個
#       屬性共用同一動詞+數值, 如"統率、速度提高11→22點")
#
# 低誤報設計: 只在"完全沒有任何一個stat效果匹配該屬性"時才報(不要求add數值精確,
# 那是其他既有流程的職責); type=="none"(內政類)由lint()主迴圈統一跳過; 有揭露
# (_todo/_note/_approx等, 常見於"caster-is-leader條件式疊加"一類複雜情境如威武並昭)
# 一律豁免。
# ---------------------------------------------------------------------------
STAT_NAME_ALT = "武力|智力|統率|統帥|速度|魅力"
STAT_ZH2K = {"武力": "force", "智力": "intel", "統率": "command", "統帥": "command", "速度": "speed", "魅力": "charm"}
R15_VERB_FIRST_RE = re.compile(
    rf"(?:提高|提升|增加|降低|減少)\s*\d+(?:\.\d+)?\s*(?:→|~|-)?\s*(?:\d+(?:\.\d+)?)?\s*點\s*({STAT_NAME_ALT})"
)
R15_STAT_FIRST_RE = re.compile(
    rf"(({STAT_NAME_ALT})(?:[、,，](?:{STAT_NAME_ALT}))*)\s*(?:提高|提升|增加|降低|減少)\s*\d+(?:\.\d+)?\s*(?:→|~|-)?\s*(?:\d+(?:\.\d+)?)?\s*點"
)
# 批28 A2: 原用 has_disclosure(p)(任一頂層揭露欄位即豁免, 不論主題), 改主題綁定——揭露
# 文字須提到屬性點數/stat相關關鍵字才算數。
R15_STAT_TOPIC_KW = ("stat", "點", "屬性", "武力", "智力", "統率", "統帥", "速度", "魅力")


def check_r15(p, txt):
    violations = []
    effects = p.get("effects", []) or []
    stat_effect_attrs = {e.get("stat") for e in effects if e.get("k") == "stat" and e.get("stat")}
    expect_attrs = set()
    for block in split_version_blocks(txt):
        for m in R15_VERB_FIRST_RE.finditer(block):
            expect_attrs.add(m.group(1))
        for m in R15_STAT_FIRST_RE.finditer(block):
            for name in re.split(r"[、,，]", m.group(1)):
                if name:
                    expect_attrs.add(name)
    if not expect_attrs:
        return violations
    missing = sorted({name for name in expect_attrs if STAT_ZH2K.get(name) not in stat_effect_attrs})
    if not missing:
        return violations
    if _topic_disclosed(p, R15_STAT_TOPIC_KW):
        return violations
    violations.append({
        "name": p["nameZh"], "rule": "R15",
        "message": f"原文含屬性點數描述({'/'.join(missing)}), 但 effects 無對應 stat 效果(k=='stat'且stat欄位匹配), 且無揭露(疑似主效果遺漏)",
        "evidence": txt[:100].strip(),
    })
    return violations


# ---------------------------------------------------------------------------
# 文字揭露輔助: 只承認「有實質文字內容」的揭露(_todo/_note/_note2/_note_self/_approx 為
# 非空字串), 排除裸 bool 旗標(如 _est:true 本身不含任何解釋性文字, 不能拿來當作"任何問題
# 都已揭露"的萬用通行證)。R16/R17/R18 都吃過"has_disclosure() 太寬鬆導致真違規被無關的
# 舊揭露文字擋下"的虧(批25診斷: 每批只修被抽中條目, 同類病在未抽中的兄弟條目上原地不動,
# 根因之一正是這個過寬的豁免判斷), 故新規則一律採用「揭露文字須含主題關鍵字」的窄核對,
# 不能只看"有沒有任何 disclosure 欄位"。
# ---------------------------------------------------------------------------
TEXT_DISC_KEYS = ("_todo", "_note", "_note2", "_note_self", "_approx")


def _disclosure_texts(p):
    """收集戰法頂層 + 所有效果層級的「非空字串」揭露文字(裸 bool 如 _est:true 不算)。"""
    out = []
    for k in TEXT_DISC_KEYS:
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v)
    for e in p.get("effects", []) or []:
        for k in TEXT_DISC_KEYS:
            v = e.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v)
    return out


def _topic_disclosed(p, keywords):
    """揭露文字(見上)裡, 是否有任一則提到 keywords 中的任一關鍵字。用於「主題相關揭露」
    判定(而非任何揭露都算數), 避免 R16/R17/R18 被無關主題的舊揭露誤豁免。"""
    for txt in _disclosure_texts(p):
        if any(kw in txt for kw in keywords):
            return True
    return False


# ---------------------------------------------------------------------------
# R16(批25): 回合閘門缺失 —— 原文「第N回合(時)」描述單一時間點爆發效果(非「第N回合起」
# 的常駐窗口, 那種已由既有 dur/R4 覆蓋, 見下方說明), 但戰法頂層 coef>0 且無 t["when"]
# (=沒有任何回合閘門, 該傷害段會每回合重複觸發, 而非只在第N回合觸發一次, 典型放大倍數
# =戰鬥總回合數, 常見8回合戰鬥即~8倍), 且無「主題相關」揭露(揭露文字須提到回合/when/
# 時機/延後/提前/窗口/視窗, 泛用的其他揭露如"疊層近似"不算數, 見_topic_disclosed)。
#
# 範圍界定(低誤報優先):
# - 只抓「第N回合」+ 非「起/開始」後綴(那是持續窗口語意, 常由 dur=N 或既有 when.from 天然
#   覆蓋, 不在本規則管轄, 見 engine_limitations.md 6.3/R4 既有機制)。
# - 只在 coef>0(=有實質傷害段可能被錯誤放大)時觸發; coef==0 的戰法此問題不適用(其 effects
#   段的回合窗口問題屬於 R1/R4/e.when 既有機制管轄範圍, 不重複)。
# - 版本區塊感知(split_version_blocks): 只在含該匹配的版本區塊內核對, 避免跨版本誤配。
# ---------------------------------------------------------------------------
R16_POINT_ROUND_RE = re.compile(r"第\s*(\d+)\s*回合(?!\s*(?:起|開始))")
R16_TIMING_KW = ("回合", "when", "時機", "延後", "提前", "窗口", "視窗")


def check_r16(p, txt):
    violations = []
    coef = p.get("coef") or 0
    if coef <= 0:
        return violations
    if p.get("when"):
        return violations
    if p.get("type") not in ("active", "command", "passive", "charge"):
        return violations
    for block in split_version_blocks(txt):
        m = R16_POINT_ROUND_RE.search(block)
        if not m:
            continue
        if _topic_disclosed(p, R16_TIMING_KW):
            return violations
        violations.append({
            "name": p["nameZh"], "rule": "R16",
            "message": f"原文「{m.group(0)}」描述單一時間點觸發, 但戰法 coef={coef} 且無 t.when 閘門"
                       f"(command/passive 無 when 時每回合皆會判定觸發, active 無 when 亦不受 prep 以外的窗口限制,"
                       f"未揭露的~N倍放大, N=戰鬥總回合數)",
            "evidence": block.strip()[:120],
        })
        break
    return violations


# ---------------------------------------------------------------------------
# R17(批25): 反應式治療缺失 —— 原文「受到(兵刃/謀略)傷害(時/後)...恢復/治療/回復」但
# heal 效果無 when.on(=常駐每回合無條件治療, 而非受傷當下反應式觸發), 且無「主題相關」
# 揭露(揭露文字須提到 when.on/反應式/onHit, 純粹的舊式EV折算說明(如"prob-ev")不算數
# ——那正是本規則要抓的"還沒升級成精確when.on建模"的殘留舊近似, 見 陷陣營/草船借箭 的
# 批22/23精確重建慣例作為對照組)。
# ---------------------------------------------------------------------------
R17_REACTIVE_HEAL_RE = re.compile(r"受到(?:兵刃|謀略)?傷害(?:時|後)[^。；;]{0,20}(?:恢復|治療|回復)")
R17_WHENON_KW = ("when.on", "反應式", "onHit", "on_hit")


def check_r17(p, txt):
    violations = []
    if not R17_REACTIVE_HEAL_RE.search(txt):
        return violations
    heals = [e for e in (p.get("effects") or []) if e.get("k") == "heal"]
    if not heals:
        return violations
    for h in heals:
        if (h.get("when") or {}).get("on"):
            continue
        if _topic_disclosed(p, R17_WHENON_KW):
            continue
        violations.append({
            "name": p["nameZh"], "rule": "R17",
            "message": "原文「受到傷害時/後...恢復/治療」描述反應式急救(受傷當下觸發), 但 heal 效果無"
                       " when.on(=被建模成常駐每回合治療), 且無提及 when.on/反應式 的揭露(可能是尚未"
                       "升級的舊EV折算殘留, 比照陷陣營/草船借箭已用 e.when.on+e.rate 精確重建的慣例)",
            "evidence": txt[:100].strip(),
        })
    return violations


# ---------------------------------------------------------------------------
# R18(批25): 無效佔位 —— 效果值全為0(heal.coef==0 / mitig.val==0 / amp.val==0 /
# stat.add==0且mult==1)但原文描述了實質機制動詞(恢復/治療/回復/降低/提高/提升/增加/傷害),
# 且該效果本身無「文字」揭露(裸 _est:true 不算, 見上方 _disclosure_texts 設計說明——
# 臨危救主即是"_est:true但兩個效果皆為0且無一字解釋"的典型案例, 過去的 has_disclosure()
# 誤把裸 _est 當成"已誠實揭露", 實際上完全沒有解釋內容)。
#
# 與既有 R5 的差異: R5 要求「原文有明確**數值**描述」(NUM_HINT正則抓百分比/機率等數字),
# 對「原文本身就沒給數值」(如臨危救主"我軍普攻有機率恢復兵力最低者並獲謀略減傷", 一個
# 數字都沒有)束手無策; R18 改用「機制動詞」(不要求數字), 專門補這個盲區——原文可以完全
# 沒有數字, 只要描述了真實機制(不是純裝飾性文字), 效果卻是全0佔位, 就算違規。
# ---------------------------------------------------------------------------
R18_MECH_VERB_RE = re.compile(r"恢復|治療|回復|降低|提高|提升|增加|減少|傷害")


def _is_zero_effect(e):
    k = e.get("k")
    if k == "heal":
        return (e.get("coef") or 0) == 0
    if k == "mitig":
        return (e.get("val") or 0) == 0
    if k == "amp":
        return (e.get("val") or 0) == 0
    if k == "stat":
        return (e.get("add") or 0) == 0 and e.get("mult", 1) == 1
    return False


def check_r18(p, txt):
    violations = []
    effects = p.get("effects") or []
    if not effects:
        return violations
    if not R18_MECH_VERB_RE.search(txt):
        return violations
    zeros = [e for e in effects if _is_zero_effect(e)]
    if not zeros:
        return violations
    for e in zeros:
        # 效果本身或戰法頂層須有「非空字串」的揭露文字才豁免(裸 _est/_todo:true 不算數)。
        has_text_disc = any(isinstance(p.get(k), str) and p.get(k).strip() for k in TEXT_DISC_KEYS) \
            or any(isinstance(e.get(k), str) and e.get(k).strip() for k in TEXT_DISC_KEYS)
        if has_text_disc:
            continue
        violations.append({
            "name": p["nameZh"], "rule": "R18",
            "message": f"效果 k={e.get('k')} 數值全為0(無效佔位), 但原文描述實質機制動詞, 且無文字揭露"
                       f"(裸 _est/_todo bool 旗標不算揭露, 需要實際解釋文字)",
            "evidence": txt[:100].strip(),
        })
    return violations


# ---------------------------------------------------------------------------
# R19(批25): dmgType 適用而未用 —— 原文明說「兵刃傷害降低/謀略傷害降低/兵刃傷害提高/
# 謀略傷害提高」等單一傷害類型措辭(排除「兵刃傷害和/與/及/、謀略傷害」這種雙型並列=
# 不分類型的合法寫法, 那種本就該讓 amp/mitig 不掛 dmgType, 全類型生效才是正確近似),
# 但「緊鄰該措辭、且數值最接近」的 amp/mitig 效果缺 dmgType, 且無「主題相關」揭露(揭露
# 文字須提到 dmgType/傷害類型/兵刃謀略區分等關鍵字; 若揭露文字聲稱"引擎無傷害類型過濾
# 機制"這類話術, 那本身就是 R20 要抓的 stale 用語, 因為 dmgType 原語自批24已存在——這裡
# 刻意讓 R19 的揭露豁免用嚴格關鍵字比對, 讓這類 stale 說法無法豁免掉 R19, 兩條規則相輔
# 相成)。
#
# 低誤報設計(比照 R1 的緊鄰數值綁定手法, 修正初版"戰法內任一單型措辭即懷疑全部amp/mitig"
# 的過寬綁定——長驅直入「使自己造成兵刃傷害提升」實際對應 stack 效果、緊接著另一句「使我軍
# 全體受到傷害降低」(不分型)才對應 mitig, 若不做數值綁定會誤把 stack 段的型別語意套到不
# 相關的 mitig 效果上):
# - 用視窗排除法找出「單一類型」命中(前後10字內若有 X傷害[和與及、]Y傷害 的並列模式,
#   判定為雙型合寫, 排除)。
# - 每個命中的「單一類型」措辭, 找該子句(逗號/句界切分)內最近的一個百分比數值(_nearest_pct_
#   like 邏輯), 只在能找到數值、且該數值與某個 amp/mitig 的 val 相差 <=6%(比照 R1 的綁定
#   容忍度)時, 才判定「這個效果對應這句話」, 缺 dmgType 才算違規——避免對著整戰法無差別
#   核對, 只精準核對真正被這句話描述的那一個效果。
# ---------------------------------------------------------------------------
R19_TYPED_DMG_RE = re.compile(
    r"(兵刃|謀略)傷害(?:降低|提高|提升|減少|增加)\s*(\d+(?:\.\d+)?)\s*%?"
    r"(?:\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)\s*%?)?"
)
R19_COMBINED_WINDOW_RE = re.compile(r"(兵刃|謀略)傷害[和與及、](兵刃|謀略)傷害")
R19_DMGTYPE_DISC_KW = ("dmgType", "傷害類型", "兵刃/謀略", "兵刃謀略", "不分兵刃", "不分類型")
R19_TYPE_ZH2K = {"兵刃": "phys", "謀略": "intel"}


def check_r19(p, txt):
    violations = []
    amp_mitig = [e for e in (p.get("effects") or []) if e.get("k") in ("amp", "mitig")]
    if not amp_mitig:
        return violations
    if all(e.get("dmgType") for e in amp_mitig):
        return violations
    if _topic_disclosed(p, R19_DMGTYPE_DISC_KW):
        return violations
    seen_effect_ids = set()
    for clause in split_clauses(txt):
        for m in R19_TYPED_DMG_RE.finditer(clause):
            window = clause[max(0, m.start() - 10):m.end() + 2]
            if R19_COMBINED_WINDOW_RE.search(window):
                continue
            expect_val = float(m.group(3) if m.group(3) else m.group(2)) / 100 if m.group(2) else None
            # 就近比對: 該子句內數值與某個缺 dmgType 的 amp/mitig 效果 val 相差 <=6% 才綁定
            # (同 R1 的容忍度慣例), 找不到明確可比對的效果就不猜, 保守跳過。
            candidates = [e for e in amp_mitig if not e.get("dmgType")]
            if not candidates:
                continue
            # 會心/奇謀EV折算特例(如 猛擊/校勝帷幄/錦帆百翎): 原文固定寫「觸發時XX傷害提高
            # 100%」(暴擊倍率本身是100%, 非戰法實際val), 但落地的amp.val是「機率×倍率」EV
            # 折算後的小數值(如65%機率×130%=0.65), 與文字裡的100%數值相差極大, 一般數值
            # 綁定會誤判無關聯而放過。用_approx=="crit-ev"辨識這個已知模式, 100%特判直接
            # 綁定同一子句唯一的crit-ev候選(不比對數值, 因為此模式下文字數值恆為100%不具
            # 綁定意義)。
            crit_ev_candidates = [e for e in candidates if e.get("_approx") == "crit-ev"]
            # 機率折算過的效果(_approx含"prob-ev"或"crit-ev"標記)其 val 已經是"機率×倍率"
            # 折算後的小數, 與原句子面數字(折算前的單一倍率或觸發機率)不會相等, 數值比對
            # 天然失效——這類效果只能靠「唯一候選」或「who/身份關鍵字」消歧義, 不能靠數值。
            folded = {"prob-ev", "crit-ev"}
            is_fold = lambda e: e.get("_approx") in folded or (
                isinstance(e.get("_approx"), str) and "prob-ev" in e.get("_approx", ""))
            bound = None
            if expect_val is not None and abs(expect_val - 1.0) < 1e-9 and len(crit_ev_candidates) >= 1:
                # 100%命中crit-ev折算模式(如 猛擊/校勝帷幄/錦帆百翎「觸發時XX傷害提高100%」):
                # 子句提及「自身」或「友軍/我軍」時用 who 縮小到單一候選; 只有一個候選時
                # 直接綁定; 多候選且無法消歧義時保守跳過。
                if len(crit_ev_candidates) == 1:
                    bound = crit_ev_candidates
                elif "自身" in clause or "自己" in clause:
                    who_matched = [e for e in crit_ev_candidates if e.get("who") == "self"]
                    if len(who_matched) == 1:
                        bound = who_matched
                elif "友軍" in clause or "我軍" in clause:
                    who_matched = [e for e in crit_ev_candidates if e.get("who") in ("ally", "leader", "subs")]
                    if len(who_matched) == 1:
                        bound = who_matched
            elif expect_val is not None:
                # 一般數值就近比對(候選不論一個或多個, 只要精確對得上才綁定, 避免長驅直入
                # 這類"唯一候選但數值明顯對不上"的假綁定——若唯一候選本身是機率折算值,
                # 改用下面的"抽不到可信數值"分支處理)。
                non_folded = [e for e in candidates if not is_fold(e)]
                if non_folded:
                    scored = [(abs(abs(e.get("val", 0)) - expect_val), e) for e in non_folded]
                    scored.sort(key=lambda x: x[0])
                    best_diff, best_e = scored[0]
                    if best_diff <= 0.06:
                        bound = [best_e]
                if bound is None:
                    # 非折算候選裡沒有對得上數值的: 若唯一候選是機率折算值(如焰逐風飛的
                    # amp, prob-ev折算後與原句"6%→12%"不再一致), 仍可放心綁定(無歧義);
                    # 有多個折算候選則保守跳過。
                    folded_candidates = [e for e in candidates if is_fold(e)]
                    if len(folded_candidates) == 1 and not non_folded:
                        bound = folded_candidates
            else:
                # 抽不到百分比數值(如「提高兵刃傷害」無跟隨數字): 只在該戰法唯一一個
                # amp/mitig 缺 dmgType 時保守綁定(不需要猜是哪一個), 多個候選時跳過。
                if len(candidates) == 1:
                    bound = candidates
            if bound is None:
                continue
            for e in bound:
                if id(e) in seen_effect_ids:
                    continue
                seen_effect_ids.add(id(e))
                dmg_type_zh = m.group(1)
                violations.append({
                    "name": p["nameZh"], "rule": "R19",
                    "message": f"原文含單一傷害類型措辭「{m.group(0).strip()}」(非兵刃謀略並列), 緊鄰數值綁定到"
                               f" k={e.get('k')}(val={e.get('val')}) 效果, 該效果缺 dmgType"
                               f"(應為 dmgType:\"{R19_TYPE_ZH2K.get(dmg_type_zh)}\"), 且無揭露"
                               f"(dmgType 原語自批24已存在, 應可精確表達)",
                    "evidence": clause.strip()[:120],
                })
    return violations


# ---------------------------------------------------------------------------
# R20(批25): stale 能力聲明總巡檢 —— _todo/_note 等揭露文字聲稱「引擎無X/不支援X/無對應
# 原語」, 但 X 實際上是引擎已支援的能力(見 ENGINE_CAPABILITIES, 從 engine.js/sgz.py 的
# apply_effects() k 分派段 + when/targetSel/ifTargetHas/e.n/e.rate/dmgType 等輔助欄位
# 整理而成)。制度化批24 A項「stale揭露清剿」的手工做法: 把「引擎已支援的能力清單」做成
# 資料結構, 未來新原語落地時只要更新這份清單, 舊 stale 註記就會被本規則自動抓出來,
# 不必每批重新人工全庫翻找。
#
# 判定: 揭露文字裡若同時出現「否定語氣詞」(引擎無/不支援/無對應原語/無此原語/引擎目前無)
# 與某個「能力關鍵字」的緊鄰片語(如「引擎無「單次格擋」原語」), 且該能力關鍵字對應
# ENGINE_CAPABILITIES 裡「已支援」的項目, 判定為 stale。用能力關鍵字→中文別名清單的
# 對照表, 而非泛用比對, 避免誤傷"能力關鍵字剛好出現在句子裡但語意其實是別的東西"的情形
# (低誤報優先, 只收錄批25全庫核對後確認的高信度別名)。
# ---------------------------------------------------------------------------
NEGATION_KW = ("引擎無", "不支援", "無對應原語", "無此原語", "引擎不支援", "引擎目前無", "不分", "無法區分")

# 能力關鍵字(中文別名) -> 該能力在引擎的實際名稱(供違規訊息顯示); 只收錄「批25核對後
# 確認引擎確實已支援、且全庫仍可能有 stale 用語提及」的能力, 非全量能力清單(全量能力見
# engine_limitations.md 附錄; 這裡刻意保守, 只收錄已知會被誤稱「不支援」的高風險別名,
# 避免用寬鬆關鍵字誤傷措辭相近但語意不同的合法揭露, 如"引擎無per-instance機率原語"
# 這種語意精確、目前真的不支援的措辭不應命中任何別名)。
ENGINE_CAPABILITY_ALIASES = {
    "單次格擋": "block(次數型格擋, 批22新增, val/times欄位, 見 push_block/consume_block)",
    "狀態免疫": "immune(單項控制免疫, 批16新增, k:\"immune\"+types欄位, 見 push_immune/is_immune_to;"
                "僅涵蓋 stun/silence/disarm/chaos/ambush 五種控制類型, 不含 healblock/其他非控制狀態,"
                "故「免疫禁療」等非控制類狀態的免疫聲明不算 stale)",
    "傷害類型過濾": "dmgType(批24新增, amp/mitig 效果欄位, 見 sgz.py amp()/addbonus() dmg_type 參數)",
    "傷害類型區分": "dmgType(同上)",
    "兵刃/謀略": "dmgType(批24新增, amp/mitig 效果欄位, 見 sgz.py amp()/addbonus() dmg_type 參數;"
                "「不分兵刃/謀略」等措辭應視為 dmgType 落地前的舊近似說明, 落地後需重寫)",
    "造成傷害時": "dealtDamage(批27新增, when.on==\"dealtDamage\", 見 sgz.py/engine.js 的"
                "on_deal_tacs/on_deal_effect_tacs + dealt_damage()/dealtDamage() 掛在 hit() 傷害"
                "結算後對 src 掃描; 支援選填 when.dmgType 區分兵刃/謀略觸發條件)",
}


def check_r20(p, txt):
    violations = []
    for text in _disclosure_texts(p):
        for alias, capability_desc in ENGINE_CAPABILITY_ALIASES.items():
            idx = text.find(alias)
            if idx == -1:
                continue
            # 「否定語氣詞」須出現在能力關鍵字前方鄰近處(同一子句, 用 20 字窗口), 確保
            # 兩者語法上構成「引擎無/不支援 + 這個能力」的否定斷言, 而非各自獨立出現。
            window = text[max(0, idx - 20):idx]
            if not any(neg in window for neg in NEGATION_KW):
                continue
            violations.append({
                "name": p["nameZh"], "rule": "R20",
                "message": f"揭露文字聲稱引擎不支援「{alias}」, 但引擎已支援: {capability_desc}"
                           f"(stale能力聲明, 應重建或改寫措辭)",
                "evidence": text[:150].strip(),
            })
            break  # 同一則文字只報一次(避免同段落多個別名重複命中洗違規數)
    return violations


# ---------------------------------------------------------------------------
# R21(批26): 點數(flat)誤用 stat.mult —— 原文對某個屬性的加成/降低描述是「點」(固定數值,
# 見 engine_limitations.md 6.3 批12 ModeA 基準)而非「%」(百分比乘算), 但對應 stat 效果仍用
# mult(百分比乘算欄位)而非 add(固定值平加欄位), 且無揭露。
#
# 批12 ModeA 已系統性把「點」語意的戰法從 stat.mult 改成 stat.add, 但批26盲測病因排查
# 發現仍有漏網之魚(奮矛英姿 effects[0]/魚鱗陣 effects[0]): 前者甚至頂層 _note 已聲稱
# "stat.mult→stat.add修正"完成, 但該戰法另一個 stat 效果(同一戰法內, 不同的目標/屬性)
# 仍留著未修的 mult, 形成"聲稱已修但實際只修了一半"的 stale 局面, 比完全沒修更容易被
# 誤認為已解決, 值得長期防線。
#
# 兩種原文語意需要偵測(對應批26實際踩雷的兩種措辭):
#   (a) 「動詞 N[→M] 點 屬性」(動詞在前, 點字明示, 如"降低敵方15點統率")
#   (b) 「屬性 提升/降低等 N[→M]」且緊接著的字元不是 % 也不是 點(裸數值, 無任何單位
#       符號, 如"統率提升27.5→55"——這種寫法在本庫「點」語意戰法中偶爾省略單位字,
#       但绝不会省略 % 符號, 故"緊鄰無%"是可靠的裸點數訊號)。
#
# 低誤報設計: 只在「該屬性緊鄰窗口內完全找不到 %」時才觸發(排除所有正常的百分比乘算
# 戰法, 見批26全庫掃描: 36 筆 stat.mult 候選中僅 2 筆符合此條件, 其餘 34 筆窗口內皆有
# % 符號, 正確使用 mult); mult 效果若同時有 add(理論上不會共存, 但保守判斷)不觸發;
# 已有揭露一律豁免。
# ---------------------------------------------------------------------------
R21_VERB_FIRST_POINT_RE = re.compile(
    rf"(?:提高|提升|增加|降低|減少)\s*\d+(?:\.\d+)?\s*(?:→|~|-)?\s*(?:\d+(?:\.\d+)?)?\s*點\s*({STAT_NAME_ALT})"
)
R21_STAT_FIRST_BARE_RE = re.compile(
    rf"({STAT_NAME_ALT})\s*(?:提高|提升|增加|降低|減少)\s*(\d+(?:\.\d+)?)\s*(?:→|~|-)?\s*(\d+(?:\.\d+)?)?"
)
# 批28 A2: 原用 has_disclosure(p, hit)(該效果任一揭露欄位即豁免, 不論主題), 改主題綁定
# ——本規則docstring自己就記載過"聲稱已修但實際只修了一半"的教訓(奮矛英姿/魚鱗陣), 卻仍
# 用blanket豁免, 是本批A2稽核發現的最諷刺的一個盲點: 揭露文字須提到 mult/add/點數(flat)
# 相關關鍵字才算數, 不能被同一效果上其他無關議題的_note(如R19 dmgType)矇混過關。
R21_POINT_TOPIC_KW = ("mult", "add", "點", "flat", "stat.add", "ModeA")


def check_r21(p, txt):
    violations = []
    effects = p.get("effects", []) or []
    mult_stats = [e for e in effects if e.get("k") == "stat" and e.get("mult") is not None and e.get("add") is None]
    if not mult_stats:
        return violations
    point_attrs = set()
    for block in split_version_blocks(txt):
        for m in R21_VERB_FIRST_POINT_RE.finditer(block):
            point_attrs.add(m.group(1))
        for m in R21_STAT_FIRST_BARE_RE.finditer(block):
            after = block[m.end():m.end() + 3]
            if "%" in after or "點" in after or "点" in after:
                continue  # 有單位符號跟隨(百分比或已明確點字), 非本規則要抓的"裸數值"情形
            point_attrs.add(m.group(1))
    if not point_attrs:
        return violations
    point_stats_k = {STAT_ZH2K.get(name) for name in point_attrs}
    hit = [e for e in mult_stats if e.get("stat") in point_stats_k]
    if not hit:
        return violations
    if _topic_disclosed(p, R21_POINT_TOPIC_KW):
        return violations
    violations.append({
        "name": p["nameZh"], "rule": "R21",
        "message": f"原文屬性描述({'/'.join(sorted(point_attrs))})為「點」(flat)語意(無%符號), 但對應 stat 效果仍用"
                   f" mult(百分比乘算)而非 add(固定值平加), 且無揭露(批12 ModeA 基準: 點數應用stat.add)",
        "evidence": txt[:120].strip(),
    })
    return violations



# ---------------------------------------------------------------------------
# R22(批27 A): dealtDamage 可用而未用 —— 原文含「(自身/自己)造成兵刃/謀略傷害時/後...」描述
# 「自己打人的瞬間」觸發某效果(對比 R17 反應式治療管的是「受到傷害時」=被打視角), 但戰法/
# 效果都沒有 when.on=="dealtDamage"(批27新增原語, 見 engine_limitations.md), 且無「主題
# 相關」揭露(揭露文字須提到 dealtDamage/造成傷害時/反應式等關鍵字; 純舊式「引擎無造成傷害時
# 反應式掛鉤」這類話術本身就是 stale 能力聲明, 落地後應改寫, 見 ENGINE_CAPABILITY_ALIASES
# 註冊, 不算合格豁免)。
#
# 低誤報設計:
# - 只抓「(自身/自己)?造成(兵刃/謀略)?傷害(時|後)」或「每次造成傷害/每次命中」這類明確描述
#   施放者自己造成傷害那一刻觸發的句型, 不含「受到傷害時」(那是 attacked/damaged, 已有機制,
#   見 R17/既有 on_hit_tacs)。
# - 只在該戰法完全沒有任何 dealtDamage 掛鉤(戰法級 t.when.on 或任一效果 e.when.on)時才觸發,
#   已局部落地(如白衣渡江遷移後只剩其中一段)不算違規(避免對「已用新原語但同戰法內其他
#   效果段仍維持既有近似, 且該近似有自己揭露」的正常漸進遷移狀態誤判)。
# - who=="enemy"/"ally"的lifesteal(倒戈)/攻心類效果若已通過既有「造成傷害時按比例回復」
#   常駐機制(addbonus("lifesteal")在hit()内建結算, 非反應式回合輪詢)精確表達, 不算本規則
#   管轄範圍(lifesteal本身就是根據「本次造成的傷害量」由hit()直接結算, 不需要when.on掛鉤;
#   只有「造成傷害後才附加另一個獨立狀態效果」如disarm/silence/dot/stat疊加等, 才是
#   dealtDamage原語要補的缺口)——用效果k白名單排除純lifesteal戰法降低誤報。
# ---------------------------------------------------------------------------
R22_DEALT_DMG_RE = re.compile(r"(?:自身|自己)?造成(?:的)?(?:兵刃|謀略)?(?:傷害|攻擊)(?:時|後)|每次(?:造成|命中)")
R22_ATTACKED_EXCLUDE_RE = re.compile(r"受到[^。；;，,]{0,6}(?:傷害|攻擊)(?:時|後)")
R22_TOPIC_KW = ("dealtDamage", "造成傷害時", "造成傷害後", "反應式", "on_deal")
R22_LIFESTEAL_ONLY_KINDS = {"lifesteal"}
# 「倒戈」是本庫對 lifesteal(hit()內建按傷害量比例回復機制, 非反應式回合輪詢)的固定措辭,
# 該子句本身即使命中「造成XX傷害時」句型也不算 R22 缺口(見上方規則說明); 逐子句排除(而非
# 整戰法排除), 避免像魯莽(倒戈+獨立的insight效果同戰法共存)這種「戰法內有lifesteal也有其他
# 效果, 但lifesteal那一句本身已被正確建模」被誤判。
# 批28 A2: 「看破」(pierce, 無視目標減傷比例/統率智力等) 與 lifesteal 同一類——sgz.py
# hit()結算時 src.addbonus("pierce") 在計算減傷公式當下直接讀取(見 mit = ... * (1-pierce
# 比例)), 是「每次造成傷害時都會自動套用」的內建機制, 非需要when.on掛鉤的反應式觸發, 故
# 「造成傷害時無視...受傷降低效果/統率和智力」這類描述pierce天生機制的子句同樣排除(威武並昭
# /虎痴皆為此案例, 後者措辭是「無視目標的統率和智力」不含「看破」字面但語意相同, 用「無視」
# 關鍵字涵蓋)。「攻心」是本庫對lifesteal的另一固定措辭(與「倒戈」同義, 見經天緯地原文自我
# 定義「攻心(造成謀略傷害時,恢復自身基於傷害量的一定兵力)」), 同樣排除(校勝帷幄案例)。
R22_LIFESTEAL_CLAUSE_KW = ("倒戈", "看破", "無視", "攻心")


def check_r22(p, txt):
    violations = []
    effects = p.get("effects", []) or []
    if not effects:
        return violations
    # 已有 dealtDamage 掛鉤(戰法級或任一效果級)即視為已用新原語, 不論是否還有其他近似效果段
    if (p.get("when") or {}).get("on") == "dealtDamage":
        return violations
    if any((e.get("when") or {}).get("on") == "dealtDamage" for e in effects):
        return violations
    # 批28 A2: 只掃「最新版本區塊」(見 split_version_blocks 說明: 多版本公告文字以 ' / ' 串接,
    # 慣例上第一個區塊是最新更新, 之後才是歷史舊版/原始版本)——舊版本的機制敘述可能與目前
    # parsed 資料(對應最新版本)完全不同(如青州兵: 舊版"部隊造成傷害時..."已被2021重做版的
    # "受到普通攻擊時反擊"完全取代), 若逐一核對所有歷史版本區塊, 會對著已經不存在的舊機制
    # 誤判「缺口」。R22是較新規則(批27), 選擇只信任最新區塊, 避免此類版本混淆假陽性。
    newest_block = split_version_blocks(txt)[0]
    for clause in split_clauses(newest_block):
        if any(kw in clause for kw in R22_LIFESTEAL_CLAUSE_KW):
            continue  # 該子句描述的是倒戈(lifesteal), 已由 hit() 內建機制精確表達, 非本規則管轄
        if R22_ATTACKED_EXCLUDE_RE.search(clause) and not R22_DEALT_DMG_RE.search(clause.replace("受到", "", 1)):
            continue  # 純「受到傷害時」(被打視角, 已有 attacked/damaged 機制), 非本規則管轄
        m = R22_DEALT_DMG_RE.search(clause)
        if not m:
            continue
        if _topic_disclosed(p, R22_TOPIC_KW):
            return violations
        violations.append({
            "name": p["nameZh"], "rule": "R22",
            "message": f"原文「{m.group(0)}」描述自身造成傷害時/後觸發(反應式), 但戰法/效果皆無"
                       " when.on==\"dealtDamage\"(批27新增原語)且無揭露",
            "evidence": clause.strip()[:120],
        })
        break
    return violations


RULES = [
    ("R1", check_r1), ("R2", check_r2), ("R3", check_r3), ("R4", check_r4),
    ("R5", check_r5), ("R6", check_r6), ("R7", check_r7), ("R8", check_r8),
    ("R9", check_r9), ("R10", check_r10), ("R11", check_r11), ("R12", check_r12),
    ("R13", check_r13), ("R14", check_r14), ("R15", check_r15),
    ("R16", check_r16), ("R17", check_r17), ("R18", check_r18), ("R19", check_r19),
    ("R20", check_r20), ("R21", check_r21), ("R22", check_r22),
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


# =============================================================================
# 批28 A3: 對抗性自我測試(--selftest) —— 每條規則至少一組「必須抓到」的合成陽性樣例 +
# 一組「不得誤報」的合成陰性樣例, 防未來規則改壞(如批28 A1/A2發現的兩類盲點: 豁免判斷
# 過寬/窗口錨點過窄, 都應該能被對應的陽性樣例抓到才對——若當初有這份self-test, 這些盲點
# 會在改動當下就被抓到, 不必等到盲測才發現)。
#
# 設計: 每筆樣例是一個 (name, parsed_dict, raw_text, expect_violation: bool) 元組, 呼叫
# 對應的 check_rN(parsed_dict, raw_text) 並斷言「有無回傳非空list」符合 expect_violation。
# 樣例刻意精簡(只含該規則需要的最小欄位), 不依賴全庫真實資料, 執行快且不受資料變動影響。
# =============================================================================

def _base_tactic(**kw):
    """自我測試用的最小合法戰法骨架(type=active, 無其他預設欄位), 呼叫端疊加需要的欄位。"""
    d = {"nameZh": "測試戰法", "type": "active", "kind": "phys", "coef": 0, "rate": 0.5, "n": 1, "prep": 0, "effects": []}
    d.update(kw)
    return d


SELFTEST_CASES = {
    "R1": [
        ("scale缺失應抓到",
         _base_tactic(effects=[{"k": "mitig", "who": "ally", "val": 0.2, "dur": 2}]),
         "使我軍群體受到傷害降低20%（受智力影響），持續2回合", True),
        ("已有scale不應誤報",
         _base_tactic(effects=[{"k": "mitig", "who": "ally", "val": 0.2, "dur": 2, "scale": "intel"}]),
         "使我軍群體受到傷害降低20%（受智力影響），持續2回合", False),
    ],
    "R2": [
        ("單體描述但n不符應抓到",
         _base_tactic(coef=2.0, n=2),
         "對敵軍單體造成一次兵刃攻擊（傷害率100%）", True),
        ("單體描述且n=1不應誤報",
         _base_tactic(coef=2.0, n=1),
         "對敵軍單體造成一次兵刃攻擊（傷害率100%）", False),
    ],
    "R3": [
        ("機率修飾但rate=1應抓到",
         _base_tactic(rate=1, effects=[{"k": "amp", "who": "enemy", "val": 0.1, "dur": 1}]),
         "有30%機率對敵軍單體造成謀略攻擊（傷害率100%）", True),
        ("rate已等於機率不應誤報",
         _base_tactic(rate=0.3, effects=[{"k": "amp", "who": "enemy", "val": 0.1, "dur": 1}]),
         "有30%機率對敵軍單體造成謀略攻擊（傷害率100%）", False),
    ],
    "R4": [
        ("持續回合不符應抓到",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 0.5, "dur": 3}]),
         "治療我軍單體，持續1回合", True),
        ("持續回合相符不應誤報",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 0.5, "dur": 1}]),
         "治療我軍單體，持續1回合", False),
    ],
    "R5": [
        ("空效果但原文有數值應抓到",
         _base_tactic(coef=0, effects=[]),
         "對敵軍單體造成謀略攻擊（傷害率50%）", True),
        ("空效果但無數值描述不應誤報",
         _base_tactic(coef=0, effects=[], type="none"),
         "內政類戰法，不參與戰鬥", False),
    ],
    "R6": [
        ("coef>0且含counter應抓到",
         _base_tactic(coef=1.0, effects=[{"k": "counter", "who": "self", "coef": 0.5, "kind": "phys", "prob": 1}]),
         "對敵軍單體造成兵刃攻擊（傷害率100%）", True),
        ("coef=0且含counter不應誤報",
         _base_tactic(coef=0, effects=[{"k": "counter", "who": "self", "coef": 0.5, "kind": "phys", "prob": 1}]),
         "自身受到攻擊時反擊", False),
    ],
    "R7": [
        ("stack掛enemy應抓到",
         _base_tactic(effects=[{"k": "stack", "who": "enemy", "per": 0.1, "max": 3}]),
         "隨意文字", True),
        ("stack掛self不應誤報",
         _base_tactic(effects=[{"k": "stack", "who": "self", "per": 0.1, "max": 3}]),
         "隨意文字", False),
    ],
    "R8": [
        ("三選一但無choices應抓到",
         _base_tactic(coef=0),
         "觸發時從以下效果中隨機獲得其中一種：治療/增傷/減傷", True),
        ("有choices不應誤報",
         _base_tactic(coef=0, choices=[{"weight": 1, "effects": []}]),
         "觸發時從以下效果中隨機獲得其中一種：治療/增傷/減傷", False),
    ],
    "R9": [
        ("未知欄位應抓到",
         _base_tactic(effects=[{"k": "amp", "who": "enemy", "val": 0.1, "dur": 1, "bogusField": 123}]),
         "隨意文字", True),
        ("已知欄位不應誤報",
         _base_tactic(effects=[{"k": "amp", "who": "enemy", "val": 0.1, "dur": 1, "dmgType": "phys"}]),
         "隨意文字", False),
    ],
    "R10": [
        ("選標關鍵字但無targetSel應抓到",
         _base_tactic(effects=[{"k": "amp", "who": "enemy", "val": -0.1, "dur": 1}]),
         "使敵軍武力最高的武將造成傷害降低10%", True),
        ("有targetSel不應誤報",
         _base_tactic(effects=[{"k": "amp", "who": "enemy", "val": -0.1, "dur": 1, "targetSel": "maxForce"}]),
         "使敵軍武力最高的武將造成傷害降低10%", False),
    ],
    "R11": [
        ("真實傷害卻只有amp應抓到",
         _base_tactic(coef=0, effects=[{"k": "amp", "who": "enemy", "val": 0.1, "dur": 1}]),
         "對敵軍單體造成謀略攻擊（傷害率50%）", True),
        ("負面狀態的無法造成傷害說明不應誤報",
         _base_tactic(coef=0, effects=[{"k": "amp", "who": "enemy", "val": -1.0, "dur": 1}]),
         "對敵軍單體施加虛弱（無法造成傷害）狀態，持續1回合", False),
    ],
    "R12": [
        ("清除狀態但缺dispel應抓到",
         _base_tactic(coef=0, effects=[{"k": "heal", "who": "ally", "coef": 0.3, "dur": 1}]),
         "治療我軍單體並清除其負面狀態", True),
        ("有dispel不應誤報",
         _base_tactic(coef=0, effects=[{"k": "dispel", "who": "ally", "what": "debuffs"}]),
         "清除我軍單體的負面狀態", False),
    ],
    "R13": [
        ("單體描述但缺e.n應抓到(主題綁定豁免修復後的核心迴歸測試)",
         _base_tactic(coef=1.0, effects=[{"k": "stat", "who": "enemy", "stat": "command", "add": -20, "dur": 1}]),
         "使敵軍單體降低20點統率，持續1回合，並對其造成一次兵刃攻擊（傷害率100%）", True),
        ("已有e.n不應誤報",
         _base_tactic(coef=1.0, effects=[{"k": "stat", "who": "enemy", "stat": "command", "add": -20, "dur": 1, "n": 1}]),
         "使敵軍單體降低20點統率，持續1回合，並對其造成一次兵刃攻擊（傷害率100%）", False),
        ("無關主題的揭露不應豁免掉真違規(對應批28 A1盲點a回歸測試)",
         _base_tactic(coef=1.0, effects=[{"k": "stat", "who": "enemy", "stat": "command", "add": -20, "dur": 1,
                                          "_note": "批12 ModeA: 原文為\"點\"(flat)非\"%\", stat.mult→stat.add修正"}]),
         "使敵軍單體降低20點統率，持續1回合，並對其造成一次兵刃攻擊（傷害率100%）", True),
        ("持續造成X傷害間隔寫法應正確定位dot(對應批28 A1盲點b回歸測試)",
         _base_tactic(coef=0, effects=[{"k": "dot", "who": "enemy", "coef": 0.5, "dur": 3}]),
         "對敵軍單體施加潰逃狀態，每回合持續造成兵刃傷害（傷害率50%），持續3回合", True),
    ],
    "R14": [
        ("_note聲稱的coef與實際不符應抓到",
         _base_tactic(coef=0.5, _note="coef=0.8, 已核對"),
         "隨意文字", True),
        ("_note聲稱的coef與實際相符不應誤報",
         _base_tactic(coef=0.5, _note="coef=0.5, 已核對"),
         "隨意文字", False),
    ],
    "R15": [
        ("屬性點數描述但缺stat效果應抓到",
         _base_tactic(coef=0, effects=[]),
         "使我軍單體提升20點武力", True),
        ("有對應stat效果不應誤報",
         _base_tactic(coef=0, effects=[{"k": "stat", "who": "ally", "stat": "force", "add": 20, "dur": 99}]),
         "使我軍單體提升20點武力", False),
    ],
    "R16": [
        ("單一時間點觸發但無when應抓到",
         _base_tactic(coef=1.5, type="passive"),
         "第5回合時，對敵軍全體造成謀略攻擊（傷害率100%）", True),
        ("有when.rounds不應誤報",
         {**_base_tactic(coef=1.5, type="passive"), "when": {"rounds": [5]}},
         "第5回合時，對敵軍全體造成謀略攻擊（傷害率100%）", False),
    ],
    "R17": [
        ("受傷時反應式治療但無when.on應抓到",
         _base_tactic(coef=0, effects=[{"k": "heal", "who": "self", "coef": 0.3, "dur": 99}]),
         "自身受到傷害時恢復兵力", True),
        ("有when.on不應誤報",
         _base_tactic(coef=0, effects=[{"k": "heal", "who": "self", "coef": 0.3, "dur": 99, "when": {"on": "attacked"}}]),
         "自身受到傷害時恢復兵力", False),
    ],
    "R18": [
        ("機制動詞但效果全0應抓到",
         _base_tactic(coef=0, effects=[{"k": "heal", "who": "self", "coef": 0, "dur": 99}]),
         "我軍普攻有機率恢復兵力最低者", True),
        ("有文字揭露不應誤報",
         _base_tactic(coef=0, effects=[{"k": "heal", "who": "self", "coef": 0, "dur": 99,
                                        "_todo": "機制未編碼, 保留占位"}]),
         "我軍普攻有機率恢復兵力最低者", False),
    ],
    "R19": [
        ("單一傷害類型措辭但缺dmgType應抓到",
         _base_tactic(coef=0, effects=[{"k": "mitig", "who": "ally", "val": 0.2, "dur": 2}]),
         "使我軍群體受到的兵刃傷害降低20%，持續2回合", True),
        ("已有dmgType不應誤報",
         _base_tactic(coef=0, effects=[{"k": "mitig", "who": "ally", "val": 0.2, "dur": 2, "dmgType": "phys"}]),
         "使我軍群體受到的兵刃傷害降低20%，持續2回合", False),
    ],
    "R20": [
        ("聲稱引擎不支援已支援能力應抓到",
         _base_tactic(coef=0, _todo="引擎無「傷害類型過濾」機制，未編碼"),
         "隨意文字", True),
        ("正常措辭不應誤報",
         _base_tactic(coef=0, _todo="引擎無per-instance機率原語，未編碼"),
         "隨意文字", False),
    ],
    "R21": [
        ("點語意卻用mult應抓到",
         _base_tactic(effects=[{"k": "stat", "who": "enemy", "stat": "command", "mult": 0.8, "dur": 1}]),
         "使敵軍降低15點統率", True),
        ("百分比語意用mult不應誤報",
         _base_tactic(effects=[{"k": "stat", "who": "enemy", "stat": "command", "mult": 0.8, "dur": 1}]),
         "統率降低15%", False),
    ],
    "R22": [
        ("造成傷害時觸發但無dealtDamage應抓到",
         _base_tactic(coef=0, effects=[{"k": "stat", "who": "self", "stat": "intel", "add": 10, "dur": 99}]),
         "自己每次造成謀略傷害時，增加10點智力", True),
        ("有dealtDamage不應誤報",
         _base_tactic(coef=0, effects=[{"k": "stat", "who": "self", "stat": "intel", "add": 10, "dur": 99,
                                        "when": {"on": "dealtDamage"}}]),
         "自己每次造成謀略傷害時，增加10點智力", False),
    ],
}


def run_selftest():
    """對每條規則跑其陽性/陰性樣例, 回傳 (n_pass, n_fail, fail_details)。"""
    rule_fn_by_id = dict(RULES)
    n_pass, n_fail = 0, 0
    fail_details = []
    for rule_id, cases in SELFTEST_CASES.items():
        fn = rule_fn_by_id.get(rule_id)
        if fn is None:
            fail_details.append(f"[{rule_id}] 找不到對應的 check_r 函式(RULES 未註冊)")
            n_fail += 1
            continue
        for case_desc, p, txt, expect_violation in cases:
            try:
                result = fn(p, txt)
            except Exception as exc:
                n_fail += 1
                fail_details.append(f"[{rule_id}] {case_desc}: 執行時拋出例外 {exc!r}")
                continue
            got_violation = bool(result)
            if got_violation == expect_violation:
                n_pass += 1
            else:
                n_fail += 1
                expect_str = "應抓到違規" if expect_violation else "不應誤報"
                got_str = f"實際抓到{len(result)}筆" if result else "實際無違規"
                fail_details.append(f"[{rule_id}] {case_desc}: 預期{expect_str}, {got_str}")
    return n_pass, n_fail, fail_details


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="輸出完整違規清單(JSON)到指定路徑")
    ap.add_argument("--summary", action="store_true", help="印人看的摘要(預設行為)")
    ap.add_argument("--selftest", action="store_true", help="跑對抗性自我測試(每條規則的陽性/陰性合成樣例), 不觸碰真實資料")
    args = ap.parse_args()

    if args.selftest:
        n_pass, n_fail, fail_details = run_selftest()
        print(f"=== lint_tactics.py --selftest ===")
        print(f"通過: {n_pass}  失敗: {n_fail}")
        for d in fail_details:
            print(f"  FAIL {d}")
        covered_rules = set(SELFTEST_CASES.keys())
        all_rules = {rid for rid, _ in RULES}
        missing = sorted(all_rules - covered_rules, key=lambda x: int(x[1:]))
        if missing:
            print(f"警告: 尚無self-test樣例的規則: {missing}")
        return 0 if n_fail == 0 else 1

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
