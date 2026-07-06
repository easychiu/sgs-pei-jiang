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
            # 批37 A: local 原本取「clause開頭到本次匹配」的全段(可能跨多個逗號分隔的獨立
            # 子片語, 見功不唐捐假陽性: "...傷害提升15%→30%,並獲得...,自身施加的負面狀態
            # 有35%→70%機率(受智力影響)不可被驅散"整句只用逗號分隔, 舊local會把句首"提升"
            # 誤判為"受智力影響"的動詞根據, 但"受智力影響"實際修飾的是後面的"機率"(抗驅散
            # 觸發率, 與amp/stat無關))。改為只取「本次匹配」往前最近一個逗號(，,)分隔的
            # 局部片語, 比照_nearest_pct_before"就近"精神, 避免跨越同句更早、不相干的動詞。
            local_start = max(clause.rfind("，", 0, sm.start()), clause.rfind(",", 0, sm.start())) + 1
            local = clause[local_start:sm.start()]
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
            # 批37 A: matched 若已就近窄化到單一效果, 傳入該效果做效果級優先判斷(避免兄弟
            # 效果的無關_note連帶豁免); 窄化不到單一效果時維持戰法級掃描(舊行為, 避免因
            # 「多個候選都可能是那句話所指」而誤判成假陰性)。
            scope_effect = matched[0] if len(matched) == 1 else None
            if _topic_disclosed(p, R1_SCALE_TOPIC_KW, effect=scope_effect):
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
                # 批37 A: 傳入 effect=e 做效果級優先判斷(該效果自身 dur/持續 相關_note才算數,
                # 不再被其他兄弟效果的無關_note連帶豁免)。
                if _topic_disclosed(p, R4_DUR_TOPIC_KW, effect=e):
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
            # 批37 A: 傳入 effect=e 做效果級優先判斷。
            if _topic_disclosed(p, R6_DOUBLE_COUNT_TOPIC_KW, effect=e):
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
    "everyRound",  # 批30 A: 非heal效果的逐回合重擲通道旗標, 跨所有k種類通用(見 apply_effects 的 e.everyRound 通用閘門判斷)
    "scaleDiv", "capVal",  # 批35: 曲線族原語泛化 —— 與 scale 同層級的跨k通用欄位(任何帶 scale
    # 的效果都可選配), scaleDiv覆蓋SCALE縮放除數(預設350), capVal為縮放後值上限clamp,
    # 見 engine.js/sgz.py 的 SCALE_G/scale_of/cap_val_of + lockedScaleOf/locked_scale_of。
    # 現階段全庫只有機鑑先識(block)實際使用, 但欄位本身語意不限定k=="block"。
}
PER_KIND_FIELDS = {
    "amp": {"val", "dmgType", "normalOnly", "activeOnly"}, "mitig": {"val", "dmgType", "normalOnly"}, "stun": set(), "silence": set(), "disarm": set(),  # dmgType: 批24 D2, 兵刃/謀略傷害類型過濾; normalOnly: 批28 B3, 僅普攻傷害生效/受影響; activeOnly: 批31 A, 僅主動/突擊戰法傷害生效(amp限定)
    "chaos": set(), "ambush": set(), "insight": set(), "immune": {"types"}, "first": set(),
    "stat": {"stat", "add", "mult"},
    "dot": {"coef", "kind"},  # 批23 A3: e.kind(dot段自帶傷害類型, 優先於t.kind, 見damage()呼叫端)
    "extra": {"val"}, "stack": {"per", "max", "stackPer"},  # stackPer: 批26 B2, "round"預設/"cast"每次發動遞增
    "decay": {"v0", "rounds"}, "swap": set(), "pierce": {"val"}, "counter": {"coef", "kind", "prob", "guardFor"},  # guardFor: 批28 B1, 守護式反擊("leader"=登記進主將counter_guards, 由代為受擊者反擊)
    "taunt": set(), "shield": {"amt", "pct"}, "dodge": {"prob"}, "surehit": set(),
    "healblock": set(), "lifesteal": {"val"}, "rateup": {"val", "prepOnly", "nativeOnly", "inheritedOnly"},
    "chargeup": {"val", "prepOnly", "nativeOnly", "leaderBonus"}, "healBoost": {"val"},
    "healGiven": {"val"}, "fakeReport": set(), "dispel": {"what"}, "heal": {"coef", "once", "rate", "ofDamage"},
    # 批22: heal 的 rate 欄位 —— 效果級 e.when.on(急救類反應式治療, 如陷陣營/雲聚影從/長健/
    # 三軍之眾)專用的「本次觸發機率」(區分於戰法整體 t.rate), 見 engine.js/sgz.py 的
    # onHitEffectTacs/onHitEq/onHitBs 註解。
    # 批33: heal 的 ofDamage 欄位 —— 傷害比例治療(非屬性公式), 治療量=ofDamage×本次觸發事件的
    # 實際傷害量(dmg 由反應式呼叫端傳入), 與 coef/scale 屬性公式互斥擇一, 見草船借箭。
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
R13_KIND_VERB_MAP = {
    "amp": ("降低", "提升", "提高", "增加"), "mitig": ("降低", "減少"),
    "stat": ("提升", "提高", "降低", "點"),
    "healblock": ("禁療", "無法恢復兵力"), "rateup": ("發動機率",),
    "extra": ("連擊", "追擊"), "insight": ("免疫",), "immune": ("免疫",),
    "lifesteal": ("倒戈",), "shield": ("護盾",), "swap": ("武智互換",),
    "fakeReport": ("偽報",), "healBoost": ("治療",), "healGiven": ("治療",),
    "pierce": ("看破", "無視"), "surehit": ("必中",), "dodge": ("規避",),
}
# 批37 A: 「全體」宣告與該效果自身動詞的緊鄰距離門檻——若視窗內「全體」出現在該效果自身
# 動詞關鍵字(如stat的"提高"/"點")之前且距離在此字數內(如"我軍全體武力提高7→14點"), 視為
# 這個效果的目標已由"全體"明確宣告, 不應該再被視窗內"更後面、屬於另一個不相干子句"的
# 單體/群體措辭覆蓋(見大戟士假陽性: "我軍全體武力提高7→14點,進行普通攻擊時,有35%機率對
# 敵軍單體造成兵刃傷害"整句只用逗號分隔, "單體"其實是後面兵刃傷害段的目標描述, 與更早的
# stat效果無關, 但視窗涵蓋整句導致SINGLE_TARGET_RE誤中)。門檻選8字(略寬於"我軍全體武力"
# 4字距離, 但不會寬到吃進下一個獨立子句的目標宣告)。
R13_ALL_ADJACENT_LOOKBACK = 8


def _r13_all_target_adjacent(window, e, kind_verb_map=None):
    """視窗內是否有"全體"緊鄰(往前 R13_ALL_ADJACENT_LOOKBACK 字內)該效果自身的動詞關鍵字
    ——若是, 視為此效果的目標已被"全體"明確宣告, 呼叫端應優先採信, 不再繼續往視窗更後面
    掃描可能屬於其他子句的單體/群體措辭。"""
    kind_verb_map = kind_verb_map or R13_KIND_VERB_MAP
    kws = kind_verb_map.get(e.get("k"))
    if not kws:
        return False
    for kw in kws:
        idx = window.find(kw)
        if idx == -1:
            continue
        lookback = window[max(0, idx - R13_ALL_ADJACENT_LOOKBACK):idx]
        if ALL_TARGET_RE.search(lookback):
            return True
    return False


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
    if k == "dot":
        for clause in split_clauses(txt):
            m = R13_DOT_PATTERN_RE.search(clause)
            if m:
                return clause
        return None
    if k not in R13_KIND_VERB_MAP:
        return None
    kws = R13_KIND_VERB_MAP.get(k)
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
        # 批37 A: 若視窗內"全體"緊鄰該效果自身的動詞關鍵字(如"我軍全體武力提高7→14點"),
        # 視為此效果目標已被"全體"明確宣告, 不再繼續掃視窗更後面(可能屬於另一個不相干
        # 子句)的單體/群體措辭(大戟士假陽性根因: 視窗涵蓋整句, "對敵軍單體造成兵刃傷害"
        # 是後段獨立子句的目標描述, 與更早的stat效果無關, 見R13_ALL_ADJACENT_LOOKBACK說明)。
        if _r13_all_target_adjacent(window, e):
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
        # 批37 A: 傳入 effect=e, 改為效果級優先——同一戰法其他兄弟效果(如decay)的揭露文字
        # 恰含"群體"不再連帶豁免本效果的e.n缺口(v16盲測: 形一陣amp/撫輯軍民heal即此問題)。
        if _topic_disclosed(p, R13_TARGET_TOPIC_KW, effect=e):
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


def _own_disclosure_texts(e):
    """單一效果 dict 自身(不含戰法頂層、不含其他兄弟效果)的「非空字串」揭露文字。"""
    out = []
    for k in TEXT_DISC_KEYS:
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v)
    return out


# 批37 A: k 名稱 → 該效果類別在原文中常見的中文自稱詞彙, 供「戰法級揭露文字是否明確指涉
# 該效果類別」判斷用(見 _topic_disclosed 下方 effect 參數說明)。不求窮舉, 只取最常見、
# 確定性高的詞彙, 找不到對應詞彙表的 k 一律 fallback 為「不做類別窄化」(維持批28 A2 舊行為,
# 避免無對照表可查的 k 出現假陰性)。
R_EFFECT_KIND_SELF_KW = {
    "amp": ("amp", "造成傷害提高", "造成傷害降低", "易傷", "增傷"),
    "mitig": ("mitig", "受到傷害降低", "受到傷害提高", "減傷"),
    "decay": ("decay", "遞減", "衰減"),
    "heal": ("heal", "治療", "恢復兵力", "回復"),
    "stat": ("stat", "屬性", "武力", "智力", "統率", "統帥", "速度", "魅力"),
    "dot": ("dot", "持續傷害", "灼燒", "中毒", "水攻"),
    "stun": ("stun", "震懾", "眩暈"),
    "silence": ("silence", "沉默"),
    "disarm": ("disarm", "繳械"),
    "chaos": ("chaos", "混亂"),
    "ambush": ("ambush", "伏兵"),
    "taunt": ("taunt", "嘲諷"),
    "counter": ("counter", "反擊"),
    "settle": ("settle", "結算"),
    "dispel": ("dispel", "清除", "淨化", "解除"),
    "immune": ("immune", "免疫"),
    "insight": ("insight", "免疫"),
}


def _topic_disclosed(p, keywords, effect=None):
    """揭露文字裡, 是否有任一則提到 keywords 中的任一關鍵字。用於「主題相關揭露」判定
    (而非任何揭露都算數), 避免規則被無關主題的舊揭露誤豁免。

    批37 A(_topic_disclosed 精準化): 原本不分效果層級, 對戰法內任何一則揭露文字(含其他
    兄弟效果的 _note)做全文子字串掃描——同一戰法若某效果的 _note 恰好含主題關鍵字(如
    "群體"), 會連帶把完全不相干的另一個效果的同主題缺口也豁免掉(v16盲測實證: 形一陣
    decay效果的衰減註記含"群體", 連帶豁免了amp效果缺e.n的R13違規; 撫輯軍民mitig效果的
    scale族註記同款誤豁免heal/amp的e.n缺口, 見批37任務背景兩案例)。

    修法(效果級優先, 僅當呼叫端傳入 effect 參數時啟用窄化; 不傳則維持戰法級掃描, 相容
    R3/R5/R8/R16/R20等本就是「戰法整體」層級的既有判斷):
    1. 先查該效果自身的揭露文字(_own_disclosure_texts) 是否含關鍵字——效果自己承認的
       缺口, 直接視為已揭露。
    2. 若效果自身無揭露, 才退而查戰法頂層欄位(不含兄弟效果!) 的揭露文字——且要求該
       頂層文字同時明確指涉這個效果類別(k名稱字面, 或 R_EFFECT_KIND_SELF_KW 裡該k的
       自稱詞彙鄰近出現), 而不是恰好共用同一個泛用主題詞(如"群體")就算數。
       R_EFFECT_KIND_SELF_KW 查無對應k時, fallback 為「頂層文字含主題關鍵字即算」
       (維持批28 A2 既有寬度, 避免無對照表可查的k出現假陰性)。
    3. 完全不查其他兄弟效果的揭露文字(這正是v16兩案例的錯誤來源)。
    """
    if effect is None:
        for txt in _disclosure_texts(p):
            if any(kw in txt for kw in keywords):
                return True
        return False

    # 1) 效果自身揭露優先
    for txt in _own_disclosure_texts(effect):
        if any(kw in txt for kw in keywords):
            return True

    # 2) 退而查戰法頂層(不含兄弟效果), 且需明確指涉該效果類別
    top_texts = []
    for k in TEXT_DISC_KEYS:
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            top_texts.append(v)
    if not top_texts:
        return False
    k = effect.get("k")
    kind_kw = R_EFFECT_KIND_SELF_KW.get(k)
    for txt in top_texts:
        if not any(kw in txt for kw in keywords):
            continue
        if kind_kw is None:
            return True  # 無對照表可查, fallback 維持批28 A2 舊寬度
        if any(kw in txt for kw in kind_kw):
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
        # 批37 A: 傳入 effect=h 做效果級優先判斷。
        if _topic_disclosed(p, R17_WHENON_KW, effect=h):
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
    # 批37 A: 移除舊的戰法級early-exit(_topic_disclosed(p, ...)無effect scope時, 任一
    # 兄弟效果的無關_note含"dmgType"字面就會豁免掉整個戰法所有amp/mitig的缺口)。改為
    # 下方就近綁定到具體效果(bound)後才做效果級豁免判斷, 見下方迴圈內的呼叫。
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
                if _topic_disclosed(p, R19_DMGTYPE_DISC_KW, effect=e):
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
# 批37 B(R26決策): 補「無法表達」——義膽雄心批32停損註記「engine無法表達回合奇偶交替條件」
# 用的正是此措辭, 且不在原否定詞表中, 加上 alias 表也沒有「奇偶交替」條目, 兩個洞疊加讓該
# stale停損逃過R20直到v16盲測才現形。批37裁決: 不新增基於「停損措辭」(不硬修/暫不/超出本批等)
# 的獨立規則R26——該類措辭大量出現於「實測樣本缺口」(非引擎能力缺口, 如陷陣營高順條件)與
# 「複核後已確認仍成立」的正當停損(誤報難控), 改為(a)強化R20(本處否定詞+下方parity別名),
# (b)批37一次性清剿(掃描13筆/9戰法, 重建3過期/註記6仍成立), (c)維護規約寫入engine_limitations.md
# (停損註記必須點名具體缺失原語, 讓R20 alias掃描在原語落地時自動抓到過期)。
NEGATION_KW = ("引擎無", "不支援", "無對應原語", "無此原語", "引擎不支援", "引擎目前無", "不分", "無法區分", "無法表達")

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
    "主將時": "ifLeader(批26新增, 效果級「施放者須為隊伍主將(index 0)」條件閘門, 見 engine.js/sgz.py"
                " applyEffects 對 e.ifLeader 的判斷: caster!==allies[0] 時該效果段整段不觸發)",
    "無條件判斷": "ifLeader(同上; 「限X時觸發,引擎無條件判斷」這類措辭是 ifLeader 落地前的舊近似"
                "說明, 落地後應改寫並補上 e.ifLeader:true)",
    "傷害來源區分": "dmgType/normalOnly(批24/28新增, amp/mitig 效果欄位, dmgType 區分兵刃/謀略"
                "來源, normalOnly 區分普攻/戰法來源, 見 engine.js addbonus() 的 dmgType/isNormal"
                "過濾參數; 「引擎mitig無傷害來源區分」這類措辭應視為兩原語落地前的舊近似說明)",
    "普攻傷害": "normalOnly(批28新增, amp/mitig/redirect 效果欄位, 限定只對「普通攻擊」造成/受到的"
                "傷害生效, 見 engine.js addbonus()/hit() 對 f.normalOnly + isNormal 的過濾判斷)",
    "代替主將": "guardFor(批28新增, counter 效果欄位 guardFor:\"leader\", 登記進主將"
                "counterGuards 清單, 由 hit() 在主將受普攻時代為觸發還擊, 見 engine.js/sgz.py)",
    "主將受擊時": "guardFor(同上)",
    "為主將承受": "guardFor(同上; 若該筆描述的是「代為反擊攻擊者」而非「代為承受傷害轉移」, 對應"
                "counter.guardFor:\"leader\"; 若確實是傷害轉移/代承語意, 引擎現有 redirect 機制只能"
                "保護全體我軍而非單獨鎖定主將, 此限制仍真實存在, 不算 stale, 見 engine_limitations.md)",
    "每次發動": "stackPer(批26新增, stack 效果欄位 stackPer:\"cast\", 每次成功發動遞增疊層,"
                "對比預設 stackPer:\"round\" 逐回合遞增, 見 engine.js/sgz.py applyStackCast();"
                "批37 B 新增第三種模式 stackPer:\"attack\", 每次普攻確實命中造成傷害後遞增,"
                "掛 dealtDamage 事件點, 見 dealtDamage()/dealt_damage() 頂端)",
    "普攻疊加": "stackPer(批37 B 新增 stackPer:\"attack\", 每次普攻命中後+1層, 見上方「每次發動」"
                "條目; 「stack以回合遞增近似逐次普攻疊加」這類措辭是 attack 模式落地前的舊近似"
                "說明, 落地後應改寫並補上 stackPer:\"attack\")",
    "普攻後觸發": "when.normalOnly(批37 B新增, dealtDamage 反應式的觸發過濾旗標(戰法級 t.when 與"
                "效果級 e.when 皆可), 限「普通攻擊」造成的傷害才觸發(dmgType:\"phys\" 無法區分普攻"
                "與兵刃戰法傷害), 見 engine.js dealtDamage()/sgz.py dealt_damage() 的 normalOnly 判斷)",
    "普通攻擊之後": "when.normalOnly(同上; 「普通攻擊之後...機率使目標X」句型用"
                "when:{on:\"dealtDamage\",normalOnly:true}+e.rate 精確表達, 見奮突批37重建)",
    "三選一": "choices(批16新增, 戰法欄 choices:[{weight,effects,...}], 發動時按權重隨機選一組"
                "效果; 批27 C 已擴充支援反應式 onHit 路徑同樣消費 choices, 見 engine.js pickChoice())",
    "二選一": "choices(同上)",
    "其中一種": "choices(同上)",
    "選標": "targetSel(批18新增, 戰法/效果/extraHits級欄位, 依準則(如maxForce/minCommand等)挑選"
                "單一目標, 見 engine.js pickByCriterion())",
    "指定目標": "targetSel(同上)",
    "已有該狀態": "ifTargetHas(批16新增, 效果/extraHits級條件欄位, 只對「已具備該狀態」的目標"
                "生效/結算, 見 engine.js targetHas()/ifTargetHas 過濾)",
    "既有狀態": "ifTargetHas(同上)",
    "奇偶交替": "when.parity(批16新增, 戰法級t.when與效果級e.when皆支援 parity:\"odd\"/\"even\","
                "見 roundOk()/round_ok(); passive/command 的 coef 傷害段擲骰自批32起亦讀 roundOk;"
                "配合批30 everyRound 可表達「奇數回合效果A/偶數回合效果B」互斥交替(義膽雄心批37重建"
                "先例); 「無法表達回合奇偶交替」這類措辭是該組合落地前的舊近似說明, 落地後應重建)",
    "回合奇偶": "when.parity(同上)",
    "每回合機率": "everyRound(批30新增, 效果級旗標 e.everyRound:true, 非heal效果的逐回合重擲通道,"
                "與既有heal_only常駐通道對稱, 見 engine.js/sgz.py applyEffects 的 e.everyRound 判斷式;"
                "「引擎只有heal逐回合重擲/其餘k在prep套用後不會逐回合重新判定」這類措辭是 everyRound"
                "落地前的舊近似說明, 落地後應改寫並補上 e.everyRound:true)",
    "逐回合重擲": "everyRound(同上)",
    "逐回合重新判定": "everyRound(同上)",
    # 批31: activeFired 只覆蓋「自身」成功發動主動/突擊戰法這個方向的事件廣播(見
    # active_fired_tacs/active_fired_effect_tacs + active_fired()/activeFired())。批38 A 已擴充
    # when.who("ally"/"enemy")跨單位事件廣播(activeFired/onHit/dealtDamage三事件點皆支援, 見
    # broadcast_holders/active_fired_for/on_hit_for/dealt_damage_for), 「敵軍/友軍發動戰法時」
    # (舌戰群儒/神機妙算/經天緯地方向)已可用 when:{on:"activeFired", who:"enemy"/"ally"} 精確
    # 表達並已完成遷移(見 engine_limitations.md 批38節)。但仍非萬能: (1) 單一tactic物件只有
    # 一個t.when, 無法同時掛「自身主動施放」與「監聽隊友之後發動」兩個獨立事件(十二奇策這類
    # 「先買buff, 之後由任一人觸發」的複合時序仍是缺口, 見該筆_todo); (2) who:"ally"/"enemy"
    # 目前只支援 attacked/damaged/dealtDamage/activeFired 三種既有事件的廣播擴展, 不支援全新
    # 事件種類(如「狀態施加」, 機鑑先識缺口仍在); (3) activeFired 事件本身只在 fire===true
    # (真正成功)之後才廣播, 「敵軍嘗試發動」(不論成功與否)與「敵軍成功發動」之間仍有粒度落差
    # (神機妙算/舌戰群儒遷移時已誠實記錄此點, 非新缺口, 是fire機制設計本身的既有邊界)。
    "自帶主動戰法": "activeFired(批31新增, when.on==\"activeFired\", 見 sgz.py/engine.js 的"
                "active_fired_tacs/active_fired_effect_tacs + active_fired()/activeFired() 掛在"
                "fight() 主迴圈 active/charge 型戰法 fire===true 判定通過後對施放者自身掃描;"
                "批38 A 新增 when.who(\"ally\"/\"enemy\")後, 亦可監聽隊友/敵軍的同一事件, 見下方"
                "「友軍發動戰法」/「敵軍發動主動戰法」條目)",
    "成功發動": "activeFired(同上; 「成功發動自帶主動戰法前/後」「自身成功發動突擊戰法後」這類"
                "第一人稱措辭若指的是自身主動/突擊戰法發動事件, 是 activeFired 的落地範圍;"
                "批38 A 起「敵軍/友軍發動戰法時」也已可用 when.who 精確表達, 見下方條目, 不再"
                "一概視為未解決缺口——需逐筆核對是否仍卡在「複合時序」(如十二奇策)或「全新"
                "事件種類」(如機鑑先識狀態鏡射)等批38範圍外的阻塞點)",
    "友軍發動戰法": "activeFired+who:\"ally\"(批38 A新增, when:{on:\"activeFired\",who:\"ally\"},"
                "見 sgz.py/engine.js 的 broadcast_holders/active_fired_for(\"ally\")分支; 持有者"
                "監聽「我軍全體(含自己)任一人成功發動主動/突擊戰法」事件, 見經天緯地批38遷移)",
    "友軍發動主動戰法": "activeFired+who:\"ally\"(同上, 見十二奇策/經天緯地)",
    "我軍全體發動": "activeFired+who:\"ally\"(同上; 若該筆同時有「自己先施放買buff」與「之後由"
                "隊友觸發」兩個獨立事件並存於同一戰法, 單一t.when仍無法表達複合時序, 需個案核對"
                "是否仍為缺口, 見十二奇策批38 _todo)",
    "敵軍發動主動戰法": "activeFired+who:\"enemy\"(批38 A新增, when:{on:\"activeFired\",who:\"enemy\"},"
                "見 sgz.py/engine.js 的 broadcast_holders/active_fired_for(\"enemy\")分支; 持有者"
                "監聽「敵軍任一人成功發動主動/突擊戰法」事件, 見神機妙算/舌戰群儒批38遷移; 事件"
                "只在敵方該次發動fire===true成功後才廣播, 「嘗試發動」與「成功發動」的粒度落差"
                "仍存在, 已在遷移note誠實記錄, 不算stale)",
    "敵軍嘗試發動": "activeFired+who:\"enemy\"(同上)",
    "僅主動戰法傷害": "activeOnly(批31新增, amp 效果欄位, 對稱於既有 normalOnly, 見 sgz.py/"
                "engine.js 的 is_active/isActive 參數穿透 hit()→damage()→amp()/addbonus())",
    "目標為敵軍主將": "ifSameTargetIsLeader(批31新增, extraHits 段條件過濾欄位, 見 sgz.py"
                "fire_extra_hits()/engine.js fireExtraHits() 對 dests 的事後過濾: 只保留"
                "dests 中恰好是 foes[0] 的目標, 取代舊有 1/3 機率 EV 折算近似)",
    "回復傷害量": "ofDamage(批33新增, heal 效果欄位 e.ofDamage, 傷害比例治療(非屬性公式): 治療量="
                "ofDamage×本次觸發事件的實際傷害量, dmg 由 on_hit/dealt_damage 反應式呼叫端傳入"
                "apply_effects(), 見 sgz.py/engine.js 的 hit() 補傳 dmg 參數 + heal 分支判斷式;"
                "取代舊近似(coef×SCALE(scale屬性)屬性公式)套用在「回復傷害量X%」這類明確描述"
                "傷害金額比例的措辭上, 見草船借箭)",
    "恢復傷害量": "ofDamage(同上)",
    "傷害量的": "ofDamage(同上; 「傷害量的X%兵力」句型, 如錦帆軍)",
    # 批35: 曲線族原語泛化 —— scaleDiv(效果級可選欄位, 覆蓋 SCALE 縮放除數, 預設350) +
    # capVal(效果級可選欄位, 縮放後值上限 clamp)。錨點: docs/data/calibration_anchors.json →
    # status_scale_375_20260704(user 機鑑先識警戒六點實測), 見 engine.js/sgz.py 的
    # SCALE_G/scale_of(scale_div參數)/cap_val_of。
    "曲線族": "scaleDiv/capVal(批35新增, 效果級可選欄位, scaleDiv覆蓋SCALE縮放除數(預設350,"
                "狀態效果類走375, 見calibration_anchors status_scale_375_20260704), capVal為縮放後"
                "值上限clamp, 見 engine.js/sgz.py 的 SCALE_G/scale_of/cap_val_of)",
    "375": "scaleDiv(同上; 「除數375」「375曲線」等措辭指此欄位)",
    "值上限": "capVal(批35新增, 效果級可選欄位, 縮放後clamp, 見 cap_val_of/capValOf; "
                "「狀態效果上限=基礎值×2」慣例不自動套用, 逐效果顯式標)",
    "準備階段鎖定": "lockedScaleOf/locked_scale_of(批35新增, 見 engine.js/sgz.py 對 block 效果"
                "scale 縮放值的準備階段鎖定快取, caster.scaleLock/scale_lock, 效果物件本身當鍵;"
                "「開戰後智力變動會重新計算」這類措辭若指 block 是 stale 的, 落地後應改寫)",
}

# =============================================================================
# 批29 A: R20 漂移自動偵測(制度性堵洞) —— ENGINE_CAPABILITY_ALIASES 手工維護, 每次engine.js
# 新增原語都要記得補登, 過去批26-28的新原語(ifLeader/stackPer/dealtDamage/guardFor/
# normalOnly/choices傷害段擴充)有登記延遲的情形(如江東猛虎的stale註記逃過偵測到批29
# 才現形)。制度化解法: 從 docs/engine.js 原始碼自動盤點「引擎實際支援的能力」(k 分派值,
# 全自動; e.xxx 效果欄位讀取, 全自動; when.on/guardFor/stackPer 等字面值分派, 全自動),
# 與人工維護的 ENGINE_CAPABILITY_ALIASES 做交叉比對——engine.js 有實作、但沒有任何別名
# 描述文字提及該 token 字面值的, 視為「登記缺口」, --selftest 印出警告(不是紅色failure,
# 因為這是提醒人工補別名, 不是資料本身的違規; 全自動只做得到「k/字面值」這個粒度, 欄位級
# 语意判斷仍需人工白名單, 見下方 KNOWN engine token 白名單)。
# =============================================================================
ENGINE_JS_PATH = os.path.join(ROOT, "docs", "engine.js")

# 這些 token 屬於「基礎骨架能力」(自批12前後即存在, 或性質上不會被誤稱「引擎不支援」的
# 常態欄位/種類, 如 who/dur/scale 等), 即使目前沒有別名描述提及也不算「新原語漏登記」,
# 不需要 R20 警示——避免每個 k/欄位都得為了消音而硬湊一條別名。只有「較新(批16之後)、
# 較容易被誤稱不支援」的能力才需要在 ENGINE_CAPABILITY_ALIASES 裡有對應描述。
CAPABILITY_INVENTORY_IGNORE = {
    # 效果種類(k): 批16之前已存在的基礎種類, 或性質單純不會被誤稱不支援的種類
    "amp", "mitig", "stun", "silence", "disarm", "chaos", "ambush", "first", "stat",
    "dot", "extra", "decay", "swap", "pierce", "taunt", "shield", "dodge", "surehit",
    "healblock", "lifesteal", "rateup", "chargeup", "healBoost", "healGiven",
    "fakeReport", "dispel", "heal", "redirect", "settle", "counter", "insight", "immune",
    "block",
    # 效果欄位(e.xxx): 通用/基礎欄位, 非「新能力」語意
    "k", "who", "dur", "scale", "when", "n", "nMax", "val", "coef", "kind", "dur",
    "durMax", "undispellable", "rate", "prob", "add", "mult", "stat", "types",
    "what", "once", "v0", "rounds", "per", "max", "amt", "pct", "share", "guard",
    "init", "base", "prepOnly", "nativeOnly", "inheritedOnly", "leaderBonus", "proc",
    "name", "type", "_eqNm",
    # when.on 基礎字面值: attack(自身普攻次數計數, everyN機制)/attacked(受擊反應式,
    # 批8即存在的既有機制, R17已管轄「反應式治療缺失」不需要R20額外重複警示)/damaged
    # (受任意傷害反應式, 與attacked同批既有機制, 批31 A修復onHitTacs/onHitEffectTacs預篩
    # 從truthy檢查收斂為明確白名單後, "damaged"字面值首次出現在原始碼裡被R20掃描器盤點到,
    # 非新原語, 補進忽略清單避免誤報)
    "attack", "attacked", "damaged",
}


def _scan_engine_js_tokens(path=ENGINE_JS_PATH):
    """從 docs/engine.js 原始碼自動盤點引擎實際支援的能力 token(半自動偵測的「全自動」那半):
    - k 分派值(k === "xxx" 模式) —— 效果種類全集
    - e.xxx 讀取的效果層級欄位名
    - when.on / guardFor / stackPer 等字面值分派(較細顆粒度的子能力, 如 dealtDamage/leader/cast)
    回傳 set of token 字串。讀不到檔案(理論上不應發生, engine.js 是本庫核心檔案)回傳空集合,
    呼叫端視為「無法盤點」而跳過警示, 不因檔案暫時缺失而誤報。"""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        src = f.read()
    tokens = set()
    tokens.update(re.findall(r'k\s*===\s*"([a-zA-Z]+)"', src))
    tokens.update(re.findall(r'\be\.([a-zA-Z_][a-zA-Z0-9_]*)', src))
    tokens.update(re.findall(r'\.on\s*===?\s*"([a-zA-Z]+)"', src))
    tokens.update(re.findall(r'guardFor\s*===?\s*"([a-zA-Z]+)"', src))
    tokens.update(re.findall(r'stackPer.*?===?\s*"([a-zA-Z]+)"', src))
    return tokens


def check_r20_capability_drift():
    """R20漂移偵測: engine.js 實際支援(掃描出的token)裡, 有哪些不在 CAPABILITY_INVENTORY_IGNORE
    白名單、且沒有任何 ENGINE_CAPABILITY_ALIASES 別名描述文字提及該 token 字面值——這些是
    「引擎已有能力, 但R20別名表還沒登記, 未來若有戰法用stale說法聲稱不支援會漏抓」的缺口。
    回傳 sorted list of token 字串(供 --selftest 印警告; 不是 lint() 的 violation, 不計入
    全庫違規數, 純粹是給維護者的提醒)。"""
    tokens = _scan_engine_js_tokens()
    if not tokens:
        return []
    alias_text = " ".join(ENGINE_CAPABILITY_ALIASES.values())
    gaps = []
    for tok in sorted(tokens):
        if tok in CAPABILITY_INVENTORY_IGNORE:
            continue
        if tok in alias_text:
            continue
        gaps.append(tok)
    return gaps


R20_WINDOW = 20
R20_AFTER_GAP = 6  # 「能力關鍵字在前、否定語氣詞在後」句型的緊鄰窗口(見下方說明), 比前向窗口窄很多
# 批37 B: 歷史敘述排除 —— 停損決策「已重建/已過期」的解決紀錄註記會複誦舊的stale措辭當歷史
# 引文(如義膽雄心批37註記「批32停損理由『engine無法表達回合奇偶交替條件』已因後續批次補齊
# 能力而過期」), 這是對「已解決狀態」的敘述, 非對目前狀態的斷言, 不應被R20誤咬(與R14的
# HISTORICAL_KW lookback同一設計問題)。判定: alias命中位置前後的上下文窗口內出現「已過期/
# 已重建」等解決性措辭, 視為歷史敘述跳過。
R20_RESOLVED_KW = ("已過期", "過期重建", "已重建", "已解決", "已落地", "落地後應重建", "已因後續", "複核仍成立", "落地前的舊近似")
R20_RESOLVED_LOOKBACK = 40   # alias 之前(涵蓋「批32停損理由「engine無法表達...」句型的引文前導)
R20_RESOLVED_LOOKAHEAD = 40  # alias 之後(涵蓋「...奇偶交替條件, 現簡化為...(可能高估)」已過期重建」這類引文較長的收尾句型)


def check_r20(p, txt):
    violations = []
    for text in _disclosure_texts(p):
        for alias, capability_desc in ENGINE_CAPABILITY_ALIASES.items():
            idx = text.find(alias)
            if idx == -1:
                continue
            # 「否定語氣詞」可能出現在能力關鍵字前方(如「引擎無「單次格擋」原語」)或後方
            # (如「限自身為主將時觸發,引擎無條件判斷」——「主將時」在前,「無條件判斷」這個
            # 否定斷言接在後面, 描述的是「引擎不會判斷這個條件」, 語意上仍是對同一能力的
            # 否定斷言)。批29 A: 原本只查前方20字窗口, 漏掉「能力關鍵字在前、否定語氣詞
            # 在後」的句型(江東猛虎 ifLeader stale 註記即此句型, 逃過批25-28的偵測)。
            #
            # 後向窗口需比前向窄很多且加額外排除條件, 否則會誤傷「能力關鍵字之後接了另一個
            # 帶引號的無關能力名詞, 否定詞其實是在否定那個引號內名詞」的情形(批29實測案例:
            # 挫銳「造成傷害時65%機率完全無法造成傷害」引擎無「攔截傷害」原語——否定的是
            # 「攔截傷害」不是「造成傷害時」; 錦囊妙計「(主將時100%)跳過1回合準備」無對應
            # 原語——否定的是「跳過1回合準備」不是「主將時」)。這兩個假陽性案例的共同特徵:
            # 否定詞與能力關鍵字之間隔了一段較長文字(>6字)且中間出現「」引號標記(暗示否定詞
            # 實際指向引號內的另一個具體名詞, 語法上與能力關鍵字脫鉤)。改用: (a) 緊鄰窗口
            # (<=6字, 江東猛虎真陽性案例間隔僅"觸發,"3字) 且 (b) 窗口內不含「/」引號字元
            # 兩條件皆成立才算後向命中, 排除上述假陽性同時保留真陽性。
            before = text[max(0, idx - R20_WINDOW):idx]
            after_gap = text[idx + len(alias):idx + len(alias) + R20_AFTER_GAP]
            after_hit = (
                any(neg in after_gap for neg in NEGATION_KW)
                and "「" not in after_gap and "」" not in after_gap
            )
            if not any(neg in before for neg in NEGATION_KW) and not after_hit:
                continue
            # 批37 B: 歷史敘述排除(見 R20_RESOLVED_KW 說明) —— alias 前後上下文含「已過期/已重建」
            # 等解決性措辭時, 視為對已解決狀態的歷史引文敘述, 非目前狀態的stale斷言, 跳過。
            resolved_ctx = text[max(0, idx - R20_RESOLVED_LOOKBACK):idx + len(alias) + R20_RESOLVED_LOOKAHEAD]
            if any(kw in resolved_ctx for kw in R20_RESOLVED_KW):
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


# ---------------------------------------------------------------------------
# R23(批32): 奇偶交替塌縮 —— 原文含「奇數回合...偶數回合...」/「單數回合...雙數回合...」/
# 「奇偶交替」等明確互斥切分回合的措辭, 但戰法/效果皆無 when.parity(批16已支援, 見武鋒陣/
# 垂心萬物既有 precedent, engine_limitations.md 第24節), 且無「主題相關」揭露(揭露文字須
# 提到 parity/奇偶/交替 等關鍵字, 純「機制簡化為同時生效」這類舊措辭若沒點出parity原語
# 本身存在與否, 仍應被抓——因為這代表尚未升級成精確when.parity建模, 而非刻意的已知取捨)。
#
# 低誤報設計:
# - 只抓「奇數回合」+「偶數回合」同時出現(或「單數回合」+「雙數回合」)的明確互斥切分句型,
#   不對單獨出現「第N回合」(那是R16管轄的單點爆發)或「每回合」(常駐)誤判。
# - 版本區塊感知(split_version_blocks): 只在含兩個關鍵詞的版本區塊內核對, 避免跨版本誤配
#   (如舊版只有奇數回合機制、新版改成每回合, 不應該對著舊版文字誤判新版資料缺parity)。
# - 揭露豁免要求主題相關關鍵字(parity/奇偶/交替/單數/雙數), 避免無關舊揭露(如純疊層近似
#   說明)頂替掉真正的奇偶塌縮問題。
# ---------------------------------------------------------------------------
R23_ODD_KW = ("奇數回合", "單數回合")
R23_EVEN_KW = ("偶數回合", "雙數回合")
R23_ALT_KW = ("奇偶交替", "奇偶輪替")
R23_PARITY_TOPIC_KW = ("parity", "奇偶", "交替", "單數", "雙數", "奇數回合", "偶數回合")


def _has_parity_when(obj):
    return (obj.get("when") or {}).get("parity") in ("odd", "even")


def check_r23(p, txt):
    violations = []
    for block in split_version_blocks(txt):
        has_odd = any(kw in block for kw in R23_ODD_KW)
        has_even = any(kw in block for kw in R23_EVEN_KW)
        has_alt = any(kw in block for kw in R23_ALT_KW)
        if not ((has_odd and has_even) or has_alt):
            continue
        # 戰法頂層或任一效果帶 when.parity 即視為已精確表達, 不論是否仍有其他效果段未升級
        # (漸進遷移中的部分落地不算違規, 比照 R22/R17 對「已局部落地」的寬容慣例)。
        if _has_parity_when(p):
            return violations
        if any(_has_parity_when(e) for e in (p.get("effects") or [])):
            return violations
        if _topic_disclosed(p, R23_PARITY_TOPIC_KW):
            return violations
        kw_hit = next((kw for kw in R23_ODD_KW + R23_EVEN_KW + R23_ALT_KW if kw in block), "")
        violations.append({
            "name": p["nameZh"], "rule": "R23",
            "message": f"原文含「{kw_hit}」等奇偶回合交替互斥措辭, 但戰法/效果皆無 when.parity"
                       "(批16已支援, 見武鋒陣/垂心萬物既有用法)且無主題相關揭露, 疑似奇偶兩組"
                       "效果被無條件同時套用(塌縮成常駐雙倍生效, 而非各自單回合互斥觸發)",
            "evidence": block.strip()[:150],
        })
        break
    return violations


# ---------------------------------------------------------------------------
# R24(批32): 虛弱誤建暈眩 —— 原文明確寫「虛弱」且附帶「(無法造成傷害)」註解(本庫對「虛弱」
# 一詞的固定官方定義: 只封鎖傷害輸出, 不影響行動/其他效果觸發), 但落地效果用了 k=="stun"
# (震懾, 整回合鎖死該單位一切行動, 見 sgz.py/engine.js `if u.stun: continue` 全禁機制,
# 威力遠大於虛弱), 且無「主題相關」揭露(揭露文字須提到虛弱/amp:-1/無法造成傷害 等關鍵字,
# 且揭露內容須點出「非stun」或「非全禁」等語意, 純粹複誦原文機率數字不算數——那只是說明
# 機率為何, 沒有承認stun本身就是錯誤的機制替換)。
#
# 慣例: 「虛弱(無法造成傷害)」= amp(val:-1, 對 who="enemy" 走 mitig 分支全額封鎖傷害輸出,
# 見批23乘敵不虞既有慣例), 不得升格用 stun(全面控制)頂替。
#
# 低誤報設計:
# - 只抓「虛弱」緊鄰「(無法造成傷害)」或「無法造成傷害」註解的措辭(排除純「虛弱」二字被
#   用在其他無關語意的情形, 全庫核對後確認本詞固定伴隨此註解使用, 無需額外排除表)。
# - 只在該戰法確實用了 k=="stun" 效果時才觸發(若已正確改用 amp/mitig, 自然不會誤報)。
# - 揭露豁免要求主題相關關鍵字, 且不接受「控制無法折值,保守單建」這類只解釋機率處理方式、
#   完全未提及stun本身是否合適的舊措辭頂替(見仁德載世批28遺留案例, 即本規則設計初衷)。
# ---------------------------------------------------------------------------
R24_WEAK_RE = re.compile(r"虛弱(?:（|\()\s*無法造成傷害\s*(?:）|\))|虛弱[^。；;]{0,10}無法造成傷害")
# 刻意不用裸「虛弱」/「無法造成傷害」當豁免關鍵字——這兩詞是原文本身就會出現的固定描述,
# 任何複誦原文機率數字的舊_note(如"虛弱5-10%機率/回合")都會天然包含這些字, 卻完全沒有
# 承認「用了stun而非amp是錯誤機制替換」這件事(見仁德載世批28遺留案例, 本規則設計初衷)。
# 只接受明確點出「stun/amp/全禁/震懾」等機制層級對比詞彙的揭露, 才算真正承認了問題所在。
R24_TOPIC_KW = ("amp:-1", "amp(val:-1)", "非stun", "非全禁", "並非震懾", "不封鎖行動",
                "stun過重", "stun過度", "误用stun", "誤用stun", "不應用stun", "應為amp")


def check_r24(p, txt):
    violations = []
    if not R24_WEAK_RE.search(txt):
        return violations
    stuns = [e for e in (p.get("effects") or []) if e.get("k") == "stun"]
    if not stuns:
        return violations
    # 批37 A: 只有單一stun候選時才能明確scope到該效果(多個stun時無法判斷"虛弱"對應哪一個,
    # 保守維持戰法級掃描, 與R19/R1的"窄化不到單一效果時退回舊行為"慣例一致)。
    scope_effect = stuns[0] if len(stuns) == 1 else None
    if _topic_disclosed(p, R24_TOPIC_KW, effect=scope_effect):
        return violations
    violations.append({
        "name": p["nameZh"], "rule": "R24",
        "message": "原文「虛弱(無法造成傷害)」只封鎖傷害輸出(慣例對應 amp(val:-1)), 但落地用了"
                   " k=\"stun\"(震懾, 全禁一切行動, 威力遠大於虛弱)且無主題相關揭露, 屬機制"
                   "替換錯誤(非近似, 是錯誤的debuff類型置換)",
        "evidence": txt[:100].strip(),
    })
    return violations


# ---------------------------------------------------------------------------
# R25(批32): 傷害宣告零輸出 —— 原文明說「造成兵刃攻擊/傷害」「謀略傷害」「兵刃/謀略攻擊」
# 但戰法頂層 t.coef==0 且無 extraHits 傷害段、無 choices 傷害分支、無其他效果(dot等)承載
# 傷害輸出, 導致該戰法完全零輸出(引擎 `if (t.coef)` 為false時整段主傷害呼叫被跳過, 見
# engine.js/sgz.py fight() 主迴圈), 且無「主題相關」揭露(揭露文字須提到coef/傷害/輸出等
# 關鍵字說明為何缺傷害段, 純粹描述其他機制的舊揭露不算數, 見短兵相接批28遺留案例)。
#
# 低誤報設計:
# - 只抓「(對|向)...造成(一次)?(兵刃|謀略)?(攻擊|傷害)」或「(再次)?發起(一次)?(兵刃|謀略)?
#   攻擊」這類明確宣告「本戰法對目標輸出一次傷害」的句型(要求「對/向」介詞+目標, 或「發起」
#   動詞), 排除兩類常見假陽性:
#   (1)「無法造成傷害」(虛弱類負面狀態描述, 已由R24/R11管轄, 不是本戰法自己要輸出傷害);
#   (2)「使(自己/自身/我軍主將/...)造成的?(兵刃)?傷害提高/提升/降低/增加」(這是amp增傷/
#      減傷描述, "造成傷害"是被修飾的受詞而非本戰法直接輸出的傷害宣告, 如乘勝長驅"使自身
#      造成傷害提高5.5%"、長驅直入"使自己造成兵刃傷害提升7.5%"、威武並昭"造成傷害時無視
#      目標降低效果"(看破/pierce機制) 皆屬此類, 全庫核對後確認此排除不影響真正的零輸出案例)。
# - 若戰法已有 extraHits 且其中至少一段有 coef>0(傷害由額外段承載, 如屠几上肉的雙段結構),
#   或有 choices 且其中至少一分支的 effects 含傷害段, 皆視為已建模(承載管道不同, 非零輸出),
#   不算違規。
# - 若 effects 內含 dot(持續傷害)且 dot.coef>0, 視為傷害輸出已由DoT段精確承載(如熯天熾地
#   的火攻+灼燒, 主段coef本身有值, 不觸發本規則; 但也涵蓋"coef==0純DoT輸出"的合法設計),
#   不算違規。
# - 版本區塊感知: 只在含「造成傷害」措辭的版本區塊內核對。
# ---------------------------------------------------------------------------
R25_DEAL_DMG_RE = re.compile(r"(?:對|向)[^。；;，,]{0,12}造成(?:一次)?(?:兵刃|謀略)?(?:攻擊|傷害)|"
                             r"(?:再次)?發起(?:一次)?(?:兵刃|謀略)?(?:攻擊|傷害)")
R25_NO_DMG_EXCLUDE_RE = re.compile(r"無法造成傷害")
R25_AMP_EXCLUDE_RE = re.compile(r"造成(?:的)?(?:兵刃|謀略)?傷害(?:時|後)?\s*(?:提高|提升|降低|增加|減少)")
R25_TOPIC_KW = ("coef", "傷害", "輸出", "傷害段")


def _extra_hits_has_damage(p):
    for eh in p.get("extraHits") or []:
        if (eh.get("coef") or 0) > 0:
            return True
    return False


def _choices_has_damage(p):
    for ch in p.get("choices") or []:
        for e in ch.get("effects") or []:
            if e.get("k") == "dot" and (e.get("coef") or 0) > 0:
                return True
        if (ch.get("coef") or 0) > 0:
            return True
    return False


def _dot_has_damage(p):
    return any(e.get("k") == "dot" and (e.get("coef") or 0) > 0 for e in (p.get("effects") or []))


def check_r25(p, txt):
    violations = []
    if (p.get("coef") or 0) > 0:
        return violations
    if _extra_hits_has_damage(p) or _choices_has_damage(p) or _dot_has_damage(p):
        return violations
    for block in split_version_blocks(txt):
        # 逐子句核對, 排除「無法造成傷害」(虛弱類負面狀態措辭, 非本戰法自身傷害輸出宣告)
        for clause in split_clauses(block):
            if R25_AMP_EXCLUDE_RE.search(clause):
                continue  # 「使...造成傷害提高/提升」是amp增傷描述, 非本戰法自身傷害宣告
            clause_no_neg = R25_NO_DMG_EXCLUDE_RE.sub("", clause)
            m = R25_DEAL_DMG_RE.search(clause_no_neg)
            if not m:
                continue
            if _topic_disclosed(p, R25_TOPIC_KW):
                return violations
            violations.append({
                "name": p["nameZh"], "rule": "R25",
                "message": f"原文「{m.group(0)}」明說造成兵刃/謀略攻擊或傷害, 但戰法 coef=0 且無"
                           "extraHits/choices/dot 等其他傷害承載段, 且無主題相關揭露, 核心傷害"
                           "輸出完全掛零(引擎 `if(t.coef)` 為false時整段跳過)",
                "evidence": clause.strip()[:120],
            })
            break
        if violations:
            break
    return violations


RULES = [
    ("R1", check_r1), ("R2", check_r2), ("R3", check_r3), ("R4", check_r4),
    ("R5", check_r5), ("R6", check_r6), ("R7", check_r7), ("R8", check_r8),
    ("R9", check_r9), ("R10", check_r10), ("R11", check_r11), ("R12", check_r12),
    ("R13", check_r13), ("R14", check_r14), ("R15", check_r15),
    ("R16", check_r16), ("R17", check_r17), ("R18", check_r18), ("R19", check_r19),
    ("R20", check_r20), ("R21", check_r21), ("R22", check_r22),
    ("R23", check_r23), ("R24", check_r24), ("R25", check_r25),
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
        ("兄弟效果的無關_note恰含主題關鍵字不應連帶豁免本效果缺口"
         "(批37 A回歸測試: v16盲測形一陣案例——decay效果的衰減註記含'群體', 過去會誤豁免amp效果的e.n缺口)",
         _base_tactic(coef=0, effects=[
             {"k": "decay", "who": "ally", "v0": -0.3, "rounds": 3,
              "_note": "友軍群體衰減效果的說明文字, 恰好含群體字樣但與amp效果的目標數無關"},
             {"k": "amp", "who": "ally", "val": 0.16, "dur": 99},
         ]),
         "友軍群體（2人）造成傷害降低30%，該效果結束後每回合使其造成傷害提高16%", True),
        ("同上情境但缺口效果自身有揭露時應豁免"
         "(對照組: 驗證修復只擋兄弟效果誤豁免, 不影響效果自身的正當揭露)",
         _base_tactic(coef=0, effects=[
             {"k": "decay", "who": "ally", "v0": -0.3, "rounds": 3,
              "_note": "友軍群體衰減效果的說明文字, 恰好含群體字樣但與amp效果的目標數無關"},
             {"k": "amp", "who": "ally", "val": 0.16, "dur": 99,
              "_note": "群體（2人）目標數缺e.n, 已知未補, 保留占位"},
         ]),
         "友軍群體（2人）造成傷害降低30%，該效果結束後每回合使其造成傷害提高16%", False),
        ("兄弟效果的無關_note2(scale曲線族揭露)恰含主題關鍵字不應連帶豁免另一效果"
         "(批37 A回歸測試: v16盲測撫輯軍民案例——mitig效果的scale族註記含'群體', 過去會誤豁免amp效果的e.n缺口)",
         _base_tactic(coef=0, effects=[
             {"k": "mitig", "who": "ally", "val": 0.24, "dur": 3,
              "_note2": "本效果帶scale但曲線族未經實測樣本裁決, 群體效果曲線族待未來實測樣本才裁決"},
             {"k": "amp", "who": "ally", "val": -0.24, "dur": 3},
         ]),
         "使我軍群體（2人）造成的兵刃傷害降低12%，受到的兵刃傷害降低12%", True),
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
        ("能力關鍵字在前否定詞在後應抓到(批29回歸測試: 江東猛虎ifLeader stale句型)",
         _base_tactic(coef=0, _approx="限自身為主將時觸發,引擎無條件判斷,暫維持為恆定生效"),
         "隨意文字", True),
        ("否定詞隔了引號內無關名詞不應誤報(批29回歸測試: 挫銳假陽性案例)",
         _base_tactic(coef=0, _approx="「造成傷害時65%機率完全無法造成傷害」引擎無「攔截傷害」原語，以amp近似"),
         "隨意文字", False),
        ("否定詞隔了引號內無關名詞不應誤報(批29回歸測試: 錦囊妙計假陽性案例)",
         _base_tactic(coef=0, _note="「17.5%→35%(主將時100%)跳過1回合準備」無對應原語(chargeup=突擊發動率非準備跳過)，未建模"),
         "隨意文字", False),
        ("「無法表達」否定詞+奇偶交替別名應抓到(批37 B回歸測試: 義膽雄心批32 stale停損句型)",
         _base_tactic(coef=0, _note="與奇數回合段實際為互斥交替觸發,engine無法表達回合奇偶交替條件,現簡化為兩者同時常駐生效"),
         "隨意文字", True),
        ("歷史敘述(已過期重建)複誦舊stale措辭不應誤報(批37 B: 停損解決紀錄的引文豁免)",
         _base_tactic(coef=0, _note="批37 B: 批32停損理由「engine無法表達回合奇偶交替條件」已因後續批次補齊能力而過期, 本批全面重建為精確互斥交替"),
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
    "R23": [
        ("奇偶回合交替但無when.parity應抓到",
         _base_tactic(coef=0.5, effects=[{"k": "stat", "who": "enemy", "stat": "intel", "add": -20, "dur": 1},
                                          {"k": "dot", "who": "enemy", "coef": 0.5, "dur": 2}]),
         "奇數回合使敵軍智力降低20點，偶數回合使敵軍陷入沙暴狀態，傷害率50%", True),
        ("戰法級when.parity不應誤報",
         {**_base_tactic(coef=0.5, effects=[{"k": "dot", "who": "enemy", "coef": 0.5, "dur": 2}]),
          "when": {"parity": "odd"}},
         "奇數回合使敵軍智力降低20點，偶數回合使敵軍陷入沙暴狀態，傷害率50%", False),
        ("效果級when.parity不應誤報",
         _base_tactic(coef=0.5, effects=[{"k": "stat", "who": "enemy", "stat": "intel", "add": -20, "dur": 1,
                                           "when": {"parity": "odd"}},
                                          {"k": "dot", "who": "enemy", "coef": 0.5, "dur": 2,
                                           "when": {"parity": "even"}}]),
         "奇數回合使敵軍智力降低20點，偶數回合使敵軍陷入沙暴狀態，傷害率50%", False),
    ],
    "R24": [
        ("虛弱誤建stun應抓到",
         _base_tactic(coef=0, effects=[{"k": "stun", "who": "enemy", "dur": 1}]),
         "有10%機率對敵軍單體施加虛弱（無法造成傷害）狀態，持續1回合", True),
        ("改用amp不應誤報",
         _base_tactic(coef=0, effects=[{"k": "amp", "who": "enemy", "val": -1.0, "dur": 1}]),
         "有10%機率對敵軍單體施加虛弱（無法造成傷害）狀態，持續1回合", False),
        ("主題相關揭露應豁免",
         _base_tactic(coef=0, effects=[{"k": "stun", "who": "enemy", "dur": 1,
                                        "_todo": "虛弱應為amp:-1非全禁stun，待修正"}]),
         "有10%機率對敵軍單體施加虛弱（無法造成傷害）狀態，持續1回合", False),
    ],
    "R25": [
        ("宣告造成傷害但coef=0且無承載段應抓到",
         _base_tactic(coef=0, effects=[{"k": "stat", "who": "enemy", "stat": "command", "mult": 0.88, "dur": 1, "n": 1}]),
         "對敵軍單體造成兵刃攻擊並降低其統率屬性", True),
        ("coef>0不應誤報",
         _base_tactic(coef=1.0, effects=[{"k": "stat", "who": "enemy", "stat": "command", "mult": 0.88, "dur": 1, "n": 1}]),
         "對敵軍單體造成兵刃攻擊並降低其統率屬性", False),
        ("extraHits有傷害段不應誤報",
         _base_tactic(coef=0, extraHits=[{"who": "sameTarget", "coef": 1.5, "kind": "intel"}]),
         "對敵軍單體造成一次兵刃攻擊及謀略攻擊", False),
        ("無法造成傷害(虛弱描述)不應誤判為本戰法自身傷害宣告",
         _base_tactic(coef=0, effects=[{"k": "amp", "who": "enemy", "val": -1.0, "dur": 1}]),
         "對敵軍單體施加虛弱（無法造成傷害）狀態，持續1回合", False),
    ],
}


def run_selftest():
    """對每條規則跑其陽性/陰性樣例, 回傳 (n_pass, n_fail, fail_details)。
    批29 A: 額外把 R20 漂移偵測(check_r20_capability_drift)納入 selftest 的失敗條件——
    engine.js 掃描出的能力 token 若有不在 CAPABILITY_INVENTORY_IGNORE 白名單、且沒有任何
    ENGINE_CAPABILITY_ALIASES 別名描述提及的, 視為「新原語落地後忘記登記別名」的制度性
    缺口, selftest 直接失敗(而非只印警告), 逼維護者當下就補登, 不必等到下次盲測才發現
    (見批29任務背景: 批26-28新原語ifLeader/stackPer/dealtDamage/guardFor/normalOnly/
    choices傷害段擴充全沒登記, 讓江東猛虎的stale註記逃過偵測)。"""
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

    drift_gaps = check_r20_capability_drift()
    if drift_gaps:
        n_fail += 1
        fail_details.append(
            "[R20-drift] engine.js 支援下列能力 token, 但 ENGINE_CAPABILITY_ALIASES 無任何別名"
            f"描述提及, 疑似新原語落地後忘記登記(或應加入 CAPABILITY_INVENTORY_IGNORE 白名單"
            f"若確認不需要別名): {drift_gaps}"
        )
    else:
        n_pass += 1
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
