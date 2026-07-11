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
# 批F: 原本要求「人」後緊接右括號(\s*[)）]), 漏掉「（2-3 人，治療率...）」這種「人」與右括號
# 之間還夾了逗號+其他子句內容的常見寫法(仁德載世/金城湯池等「N-M 人」句型全庫核對18筆中
# 唯一的漏網案例, 因「人」前多一個空格+後面接逗號子句)。改用「同一括號內, 人後面允許非右括號
# 字元(不含右括號本身, 避免跨括號誤配到下一組全然無關的括號內容)直到右括號出現為止」, 仍要求
# 右括號存在(避免括號未閉合的異常文字誤配), 只是不再要求「人」與右括號之間淨空。
GROUP_RANGE_RE = re.compile(r"[（(]\s*(\d+)\s*[~～-]\s*(\d+)\s*人[^（）()]*[)）]")
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
    # 批B(R2窗口溢出bug修復): 向前延伸(+40字, 涵蓋「傷害率X%）每次目標獨立選擇...」這類
    # 傷害率之後仍屬同一子句的尾隨修飾語)須以「下一個句界(。；;)」為硬上限, 不能無條件延伸
    # 滿40字——若命中的傷害率離句尾(；/。)很近(如暗箭難防「...傷害率260%）；並有60%概率
    # ...捕獲敵軍單體武將...」, 傷害率260%後僅8字就遇到句界；), +40字會直接跨越句界溢入
    # 下一個無關子句(「捕獲敵軍單體武將」), 導致window_end用該不相干子句裡的「敵軍單體」
    # 誤判目標數(見批B任務背景實測案例)。改用「+40字」與「下一個硬句界位置」取更近者
    # (min), 逗號(，,)不算硬句界(維持既有「傷害率後接，可能仍是同子句尾隨修飾」的行為
    # 不變, 只防止跨越；/。這種真正的子句邊界)。
    hard_ends = [block.find(ch, dm.start()) for ch in "。；;"]
    hard_ends = [e for e in hard_ends if e != -1]
    window_end = min(dm.start() + 40, min(hard_ends) if hard_ends else len(block))
    window = block[window_start:window_end]
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
    "ifLeaderIs",  # 批44 A: 施放者須為隊伍主將(index 0)且武將名匹配指定值(字串或陣列OR)才套用該效果段, 對稱ifLeader, 跨所有k種類通用
    "maxStackIfLeaderIs",  # 禁近似令-批L: {who, max} —— 對稱coefLeader/rateLeader(基礎值+主將時
    # 替代值)家族, 但套用維度是maxStack本身(封頂整數, 無法像coef/rate用base+topup相加表達),
    # 施放者恰為隊伍主將且武將名匹配時整個覆寫e.maxStack為指定值。跨k種類通用(見resolveMaxStack/
    # resolve_max_stack, stealStat與rateup皆讀取), 先登死士「可疊加4次;若麴義統領則5次」首次落地。
    "everyRound",  # 批30 A: 非heal效果的逐回合重擲通道旗標, 跨所有k種類通用(見 apply_effects 的 e.everyRound 通用閘門判斷)
    "ifStackMaxed",  # 批43 B: 施放者自身k=="stack"疊層已滿(caster.stack.n>=caster.stack.max)才套用該效果段, 跨所有k種類通用(見 apply_effects 對 e.ifLeader 之後新增的判斷式), 搭配 everyRound 表達「疊加N次後才觸發」(如長驅直入)
    "scaleDiv", "capVal",  # 批35: 曲線族原語泛化 —— 與 scale 同層級的跨k通用欄位(任何帶 scale
    # 的效果都可選配), scaleDiv覆蓋SCALE縮放除數(預設350), capVal為縮放後值上限clamp,
    # 見 engine.js/sgz.py 的 SCALE_G/scale_of/cap_val_of + lockedScaleOf/locked_scale_of。
    # 批35當時全庫只有機鑑先識(block)實際使用, 但欄位本身語意不限定k=="block"; 批46 A:
    # rateup 效果也讀 e.scaleDiv(獨立於 SCALE_G 的另一條 rateScaleOf/rate_scale_of 曲線族,
    # 預設除數384.6, 十二奇策 scaleDiv:335, 見 rateScaleOf/rate_scale_of 定義處), 兩者共用
    # 同一個欄位名稱與「覆蓋預設除數」語意, 但各自的預設值/曲線函式彼此獨立(scaleDiv的具體
    # 數值意義依 k 決定用哪條 rateScaleOf 或 scaleOf 曲線, 非全域單一常數)。
    "sameTargets",  # 批45 A: 沿用同一次apply_effects呼叫內先前已命中的群體目標(main_hit_tgts),
    # 取代「coef段與效果段各自獨立pick_targets」的舊近似, 跨所有k種類通用(見 apply_effects
    # 對 e.sameTargets 的判斷式, engine_limitations.md 第45節)。
    "ifTargetHasNot",  # 批A(11筆高嚴重重建): ifTargetHas的反向(只對「尚未有該狀態」的目標生效),
    # 跨所有k種類通用(對稱ifTargetHas本身即是全域欄位), 見engine.js/sgz.py applyEffects()/
    # apply_effects()對e.ifTargetHasNot的過濾判斷, 偽書相間首次落地, engine_limitations.md第46節。
    "kindByStat",  # 批A: extraHits段欄位(非effects段, 但PER_KIND_FIELDS掃描邏輯共用同一份白名單
    # 結構), 動態比較atk本身force/intel兩項屬性決定傷害類型, 見fireExtraHits()/fire_extra_hits(),
    # 偽書相間首次落地。
    "ratePerTarget", "rateStatusBonus",  # 批52g: 逐目標擲骰(取代單次全域e.rate擲骰), 跨所有k
    # 種類通用的機率結算模式旗標(見 apply_effects 對 e.rate 判定式前的 _per_tgt_rate/perTgtRate
    # 分流, 非任何k專屬), 目前全庫僅五雷轟頂(k=="stun")使用, 但欄位語意不限定k。rateStatusBonus
    # 為dict(statuses/per/maxBonus/ifLeader), 依目標身上具名狀態(水攻/沙暴)數量疊加機率, 內含
    # 巢狀ifLeader(主將時才套用此加成, 見 apply_effects 對 b.get("ifLeader") 的判斷)——與頂層
    # e.ifLeader(整段效果的主將閘門)是不同層級的概念, 此為「加成部分」限定主將, 基礎機率則不
    # 分主將/副將皆生效, 精確對應「基礎機率X%(全員)+主將時每種狀態額外+Y%」的複合語意。
    "ifCasterNames", "whoNames",  # 批52g: 施放者/目標武將名單過濾(太平道法「自身為黃巾軍主將時,
    # 黃巾副將同樣獲得...」), 跨所有k種類通用(見 apply_effects 對 e.ifCasterNames/e.whoNames 的
    # 判斷式, 位於 k 分派之前的共用前置檢查段), 目前全庫僅太平道法(k=="rateup")使用, 但欄位
    # 語意不限定k。ifCasterNames: 施放者武將名須在名單內; whoNames: 只對武將名在名單內的目標
    # (dests過濾)生效, 兩者常搭配使用(施放者身份判定+目標身份判定)。
    "onlySlower",  # 批52h: fire_controlled()/fireControlled() 專用的「僅速度慢於持有者的友軍
    # 才觸發反彈」條件(機鑑先識「速度比持有者慢的友軍」), 綁定 when.on:"controlled" 反應式事件
    # (非 apply_effects 的一般 k 分派路徑), 掛在哪個 k 值上純屬資料撰寫慣例(現用k=="amp"當
    # 占位容器, 見機鑑先識該效果_note「k:stat add:0為占位」同款慣例), 欄位語意不限定k, 故列
    # 為全域欄位而非PER_KIND_FIELDS["amp"]專屬。
    # 批B: ifSameTargetIsLeader 刻意不登記為全域已知欄位——批31 B新增此欄位時, 讀取邏輯只寫在
    # fire_extra_hits()/fireExtraHits()(extraHits段專屬), 從未接上 apply_effects()/applyEffects()
    # 的一般 effects[] 分派路徑(見sgz.py 1394行 eh.get("ifSameTargetIsLeader"), eh=extraHits
    # entry, 非effects entry)。若某戰法把此欄位直接掛在 effects[].k(如將行其疾的silence效果,
    # 非extraHits[]內), 該欄位會被引擎完全忽略, 是真正的幽靈欄位(對比kindByStat/ifTargetHasNot
    # 兩者在extraHits與effects兩種context下皆有對應讀取邏輯, 語意一致故可全域登記; 此欄位只有
    # extraHits一種context被實作, 不可比照全域登記, 否則會讓「掛在effects[]上不生效」的真缺口
    # 被白名單靜默放行)。將行其疾的用法屬於後者(effects[]直接掛, 非extraHits[]), 已移除該欄位
    # 並保留其誠實揭露(_todo已載明此為未解決的overshoot), 見tactic_corrections.json。
    # 批C: 批52續系列(rateLeader/rateSub/ifSub/ifGender/scaleIfSub/scaleIfLeader)在 apply_effects()/
    # applyEffects() 的效果級通用前置判斷段落實作(與 ifLeader/ifLeaderIs 同一層級, k 分派之前
    # 就讀取, 跨所有k種類通用), 但當時新增時遺漏同步登記進本白名單, 導致R9把這些真實可用的效果
    # 級欄位誤判成「引擎不讀的幽靈欄位」。核對位置: sgz.py 1605/1607行(rateLeader/rateSub,
    # 效果級「主將/副將時用不同觸發率」)、1629行(ifSub, 效果級「施放者須為副將」)、1632行
    # (ifGender, 效果級「施放者性別須匹配」)、1803/1805行(heal分支內scaleIfSub/scaleIfLeader,
    # 「僅副將/主將時套用scale縮放」)、2070/2072行(非heal效果的scaleIfSub/scaleIfLeader鏡像,
    # 同一組欄位名稱、同一語意, 只是掛在不同k分支各自實作, 故仍歸為全域已知欄位而非
    # PER_KIND_FIELDS["heal"]專屬)。對稱engine.js同名分支(1220-1223/1237-1240/1407-1410/
    # 1645-1649行)。
    "rateLeader", "rateSub",  # 批52續: 效果級「主將/副將時採用不同機率值」, 取代基礎e.rate
    "ifSub",  # 批52: 效果級「施放者須為隊伍副將(非index 0)」條件閘門, 對稱ifLeader
    "ifGender",  # 批52: 效果級「施放者性別須匹配Male/Female(或中文男/女)」條件閘門
    "scaleIfSub", "scaleIfLeader",  # 批52/52c: 「僅副將/主將身份時才套用e.scale縮放」旗標,
    # heal與非heal效果(如義膽雄心)各自有一份實作但欄位語意/名稱相同
    "ifStatCompare",  # 批I(禁近似令-scale/比較族): 比較「參照方(施放者/我軍主將)vs目標」同一
    # 屬性大小, 決定效果段是否生效(布林gate, 對稱ifTargetHas但比較的是「屬性大小」而非「狀態
    # 有無」), 見sgz.py/engine.js stat_compare_ok()/statCompareOk()。跨所有k種類通用(k派發
    # 之前的共用前置檢查段), 且同一欄位名稱也用於extraHits段(eh.ifStatCompare, 見
    # fire_extra_hits()/fireExtraHits()), 對稱kindByStat/ifTargetHasNot兩種context皆有對應
    # 讀取邏輯的既有慣例, 摧鋒斷刃(vs預設"caster")/竊幸乘寵(extraHits段)/聚石成金(vs:"leader")
    # 首次落地, engine_limitations.md本批新節。
    "maxStack",  # 批52(補登記, R9登記缺口): 同來源(戰法名)同種效果允許至多N層獨立實例並存
    # (見push_add/push_mod/push_stat_add的max_stack參數), 跨所有k種類通用(pushAdd/pushMod/
    # pushStatAdd三者共用同一套機制), 智計/累世立名(批K)首次在資料層實際使用而發現此登記缺口
    # (欄位本身/引擎讀取邏輯自批52即存在, 純粹是R9白名單當時忘記登記)。
    # 禁近似令-批K新增跨k通用欄位 ------------------------------------------------------
    "once",  # 通用一次性消耗閘門(對稱既有everyRound內部的e.once, 本批擴充成apply_effects開頭
    # 的通用閘門, 任何k/任何呼叫路徑皆生效), 見Unit.whenFired/self.when_fired, 誓守無降/
    # 淵然難測首次落地, 見上方ENGINE_CAPABILITY_ALIASES「反應式一次性消耗」條目。
    "ifSelfStatCompare",  # 比較「已選定的效果目標自己」兩項屬性大小(同單位自己互比, 與
    # ifStatCompare的跨單位比較方向不同), 淵然難測首次落地, 見上方別名「同一單位自己兩屬性
    # 互比」條目。
    "ifTargetHpAbove", "ifTargetHpBelow",  # 已選定的效果目標(受益者)自己兵力百分比條件(與
    # 既有when.hpAbove/hpBelow只認caster自身不同), 肉身鐵壁首次落地, 見上方別名「他方單位
    # 血量條件」條目。
    "ifEnemyTroop",  # 敵隊兵種(兵種由隊伍決定)條件閘門, 左右開弓首次落地, 見上方別名「依
    # 隊伍兵種類型分支」條目。
    "ifCasterStackAtLeast",  # 施放者k=="stack"疊層數達門檻(對稱既有ifStackMaxed但門檻可調),
    # 見上方別名「疊加次數作為觸發條件」條目(目前資料尚未實際使用, 已建之engine capability)。
    "ifArmed",  # once_consumable族: caster.armedConsume.active為真才放行, 十二奇策首次落地
    # (若使用, 見上方別名「消耗態狀態機」條目)。
    "ifTargetIsRank", "ifTargetIsRankNot",  # target_rank_branch族: 已選定目標是否恰為
    # pickByCriterion排名冠軍, 見上方別名「目標恰好符合排名準則」條目(目前資料尚未實際使用)。
    "rateFactionBonus",  # faction_count_scale族: 依隊伍陣營人數線性加成觸發率, 見上方別名
    # 「陣營計數」條目(目前資料尚未實際使用)。
    "rateBonusPerBuffType",  # rate_self_dynamic族: 依自身持有增益狀態種類數動態加成觸發率,
    # 見上方別名「依自身持有增益狀態數動態調整」條目(目前資料尚未實際使用)。
    "eitherK",  # 批K7: 陣列, 本次觸發隨機擇一k值頂替e.k本身(於k分派之前處理, 跨所有k種類通用),
    # 溯江搖櫓首次落地, 見上方別名「狀態擇一觸發」條目。
    "broadcast",  # 時序徹底一致化批: 跨所有k種類通用旗標, 搭配everyRound標記「相一: 持有者
    # 每回合對他人(敵/我)廣播施加新狀態層」的極少數實例(user權威裁決: SP周瑜江天長焰/SP袁紹
    # 高櫓連營同類, 對稱「大多數效果=相二逐單位own_round」的預設值)。broadcast=true時,
    # apply_effects()/applyEffects()的everyRound分支改用全局CUR_ROUND/CUR_R為回合基準且由
    # fight()主迴圈回合頂端apply_passives(broadcast_only=True)/applyPassives({broadcastOnly:true})
    # (任何單位行動前, 全體批次)呼叫, 不進逐單位own_round的apply_own_turn_effects()/
    # applyOwnTurnEffects()通道(該通道明確排除e.broadcast的effects, 見其函式定義)。目前全庫
    # 僅高櫓連營2個effects段使用(amp/disarm, 見tactic_corrections.json「高櫓連營」條目)。
}
PER_KIND_FIELDS = {
    "amp": {"val", "dmgType", "normalOnly", "activeOnly", "chargeOnly",
             "stackKey", "perStack", "maxStacks", "stackId", "dmgFromStatus"},
    # 禁近似令-批L: dmgFromStatus(list, 僅k=="amp") —— 限定「只對這些具名dot狀態(灼燒/水攻/
    # 中毒/潰逃/沙暴/叛逃等)造成的傷害生效」跨戰法橫切範圍(才辯機捷「自身施加的灼燒、水攻、
    # 中毒、潰逃、沙暴、叛逃狀態造成的傷害提升90%」), 見engine.js/sgz.py damage()新增
    # dotStatus參數→addbonus("amp",...,dotStatus)過濾。
    # 批K: stackId(dynamic_coef_from_counter族) —— 額外把amp+stackKey的疊層數寫進字串鍵
    # 索引(u.ampLayersById/self.amp_layers_by_id), 供k=="settle"+e.perStackFrom跨效果讀取
    # (密計誅逆settle結算需要讀取這段amp疊層數代入coef公式, 見settle條目/上方別名
    # 「settle讀取指定疊層計數器」)。
    # 批A: k=="amp"+e.stackKey(per-target疊層變體, 對稱既有k=="stat"+stackKey, 但不支援
    # onMaxStacks/globalMax/e.add三個延伸, 見engine.js/sgz.py k==="amp"分支的ampLayers/
    # amp_layers計數器, 密計誅逆首次落地, engine_limitations.md第46節)。
    "critUp": {"val", "dmgType", "normalOnly", "stackKey", "perStack", "maxStacks"},
    # 批H: 會心(兵刃暴擊)/奇謀(謀略暴擊)機率, 真擲骰(取代全庫14筆crit-ev EV折算, 見
    # no_approx_inventory.json crit_system_primitive族/engine_limitations.md本節)。val加法
    # 累積, dmgType路由"phys"=會心/"intel"=奇謀, stackKey+perStack+maxStacks對稱amp的
    # per-target疊層變體(逆鱗「受到傷害時3%機率獲得10%會心可疊加2次」首次使用此組合), 見
    # engine.js/sgz.py k=="critUp"分支(damage()讀addbonus("critUp",...)擲骰消費)。
    "critDmgUp": {"val", "dmgType", "normalOnly"},
    # 批H: 會心/奇謀傷害幅度加成(疊在critUp觸發後的基礎+100%之上, 如「+20%會心傷害」使
    # 觸發倍率變成120%), 純幅度修飾語, 若持有者無對應dmgType的critUp來源則不生效(見華服/
    # 長慮「幅度類修飾語缺乏機率類觸發事件」下游消費端precedent)。
    "mitig": {"val", "dmgType", "normalOnly", "stackKey", "perStack", "maxStacks"},
    # 批K: mitig+stackKey(對稱既有amp+stackKey per-target疊層變體), 離月首次落地, 見上方
    # 別名「settle讀取指定疊層計數器」相鄰的stackKey家族說明(amp/mitig/critUp三者共用同一套
    # stackKey機制形狀, 僅消費端各自獨立)。
    "stun": set(), "silence": set(), "disarm": set(),  # dmgType: 批24 D2, 兵刃/謀略傷害類型過濾; normalOnly: 批28 B3, 僅普攻傷害生效/受影響; activeOnly: 批31 A, 僅主動/突擊戰法傷害生效(amp限定)
    "chaos": set(), "ambush": set(), "insight": set(), "immune": {"types"}, "first": set(),
    "stat": {"stat", "add", "mult",
              "stackKey", "perStack", "maxStacks", "onMaxStacks", "globalMax", "globalEffects",
              "addPerBuffType", "stackId", "fromStack", "perStackVal"},
    # 批K7: stackId(dynamic_coef_from_counter族) —— stat+stackKey額外把疊層數寫進amp_layers_by_id
    # (供k=="dot"+e.coefFromStack跨效果讀取, 絕地反擊)。fromStack+perStackVal(同族stat版) ——
    # 不自己疊層, 改讀同一持有者k=="stack"計數器(this.stack.n)即時驅動, 見上方別名「stat隨stack
    # 動態同步」條目(弓腰姬)。
    # 批K: addPerBuffType(rate_self_dynamic族stat版本) —— {types,per,maxCount}, 依自身持有
    # 增益狀態種類數動態疊加stat平加, 弓腰姬首次落地, 見上方別名「依自身持有增益狀態數動態
    # 調整」條目。
    # 批42: stackKey(truthy旗標)/perStack(每層量級)/maxStacks(單目標疊層上限)/onMaxStacks
    # (該目標本地池耗盡時額外套用的效果陣列)/globalMax(持有者跨目標累計觸發次數上限)/
    # globalEffects(全場觸發後套用的效果陣列) —— 傲睨王侯「敵軍目標受普攻時觸發1個破綻,
    # 該目標降3%可疊…單目標破綻全觸發→…全場破綻觸發後→…」per-target疊層+雙閾值原語,
    # 見 engine.js/sgz.py k=="stat"&&e.stackKey 分支與 engine_limitations.md 第40節。
    "dot": {"coef", "kind", "coefLeader", "pierce", "coefFromStack", "name"},  # 批23 A3: e.kind(dot段自帶傷害類型, 優先於t.kind,
    # 見damage()呼叫端); coefLeader: 批52續, 主將時採用較高傷害率(取代基礎e.coef), 見sgz.py
    # 2270-2273行/engine.js 1825-1827行, 火燒連營首次登記(雖然該戰法實際案例是extraHits段
    # 用ifLeader/ifSub互斥拆分, 非dot段本身用coefLeader, 但欄位本身確實只在k=="dot"分支實作)。
    # 批K: pierce(true, engine_wiring_gaps_misc族) —— 強制本段dot完全無視目標mitig(damage()
    # forcePierce參數), 獅子奮迅首次落地, 見上方別名「無視防禦」條目。
    "extra": {"val"}, "splash": {"val"},  # 批K: splash(splash_aoe_primitive族) —— 對稱既有extra,
    # 消費於doNormalAttack/do_normal_attack(非damage()內), 瞋目橫矛/象兵/橫掃首次落地, 見上方
    # 別名「濺射」條目。
    "stack": {"per", "max", "stackPer"},  # stackPer: 批26 B2, "round"預設/"cast"每次發動遞增
    "decay": {"v0", "rounds"}, "swap": set(), "pierce": {"val", "onKill"},
    # 批K7: onKill(engine_wiring_gaps_misc族) —— 不立即套用, 改登記待hit()偵測到本單位親手
    # 擊敗某目標時才真正授予, 虎痴首次落地, 見上方別名「擊敗後授予」條目。
    "preAttackHook": {"hookKind", "coef", "scale", "guard"},
    # 批K7: engine_wiring_gaps_misc族 —— 「即將受到普通攻擊」真反應式掛鉤點, hookKind:
    # "redirectPre"(guard準則轉由隊友代承)/"healAllyPre"(coef/scale治療隨機隊友), 消費於
    # do_normal_attack()/doNormalAttack(), 雲聚影從/益其金鼓首次落地, 見上方別名「即將受到
    # 普通攻擊」條目。
    "regen": {"coef", "scale"},
    # 批K7: engine_wiring_gaps_misc族 —— 對稱dot的傷害版但方向是治療, 每回合各自結算, 消費
    # 於tick(), 乘敵不虞首次落地, 見上方別名「每回合恢復持續N回合」條目。
    "dmgShare": {"val", "scale"},
    # 批K7: engine_wiring_gaps_misc族 —— 目標受傷後額外對其隊友分攤傷害, 消費於hit(), 連環計
    # 首次落地, 見上方別名「受傷回饋隊友」條目。
    "counter": {"coef", "kind", "prob", "guardFor", "ofDamage", "debuffAttacker", "selfStack"},
    # guardFor: 批28 B1, 守護式反擊("leader"=登記進主將counter_guards, 由代為受擊者反擊)。
    # 批K: ofDamage(engine_wiring_gaps_misc族) —— 依本次受到的實際傷害量比例反彈(對稱heal
    # 既有ofDamage), 荊棘裝備首次落地(equips_parsed.json, 不受本linter掃描但登記備查), 見
    # 上方別名「counter依本次傷害量比例輸出」條目。debuffAttacker/selfStack(counter_target_
    # binding族) —— guardFor反擊觸發後對攻擊者/反擊執行者的額外副作用, 消費於hit()內
    # counter_guards迴圈(非apply_effects的k派發), 古之惡來/虎衛軍首次落地, 見上方別名
    # 「guardFor反擊精確綁定同一人」條目。
    "taunt": {"tauntTarget"},  # 批K: tauntTarget(force_attack_reverse族) —— "leader"/"select",
    # 定謀貴決/武鋒陣首次落地, 見上方別名「反向taunt」條目。
    "preDmgHook": {"hookKind", "val", "step", "max", "dmgType", "pct", "delayRounds", "reducePct"},
    # 批K: pre_damage_intercept族, 消費於damage()內, 見上方別名「傷害結算前攔截」條目。
    "strike": {"sameTarget", "coef", "kind"},  # 批K: once_consumable族消費端(十二奇策)/
    # engine_wiring_gaps_misc族(驍健神行沿用tgt同目標追加攻擊)——coef/kind為本k實際使用的
    # 傷害參數(對稱dot/counter等其他k各自登記coef/kind的既有慣例, 非全域欄位); targetSel/
    # ifTargetHas/ifArmed皆屬既有全域或已登記欄位, 這裡額外只需登記sameTarget(沿用tgt而非
    # 重新選標), 見上方別名「同一次觸發同一目標的追加傷害」條目。
    "shield": {"amt", "pct"}, "dodge": {"prob"}, "surehit": set(),
    "healblock": set(), "lifesteal": {"val"}, "rateup": {"val", "prepOnly", "nativeOnly", "inheritedOnly"},
    "chargeup": {"val", "prepOnly", "nativeOnly", "leaderBonus"}, "healBoost": {"val"},
    "healGiven": {"val"}, "fakeReport": set(), "dispel": {"what"}, "heal": {"coef", "once", "rate", "ofDamage",
                                                                              "all", "preferLowest", "sharedPool"},
    # 批22: heal 的 rate 欄位 —— 效果級 e.when.on(急救類反應式治療, 如陷陣營/雲聚影從/長健/
    # 三軍之眾)專用的「本次觸發機率」(區分於戰法整體 t.rate), 見 engine.js/sgz.py 的
    # onHitEffectTacs/onHitEq/onHitBs 註解。
    # 批33: heal 的 ofDamage 欄位 —— 傷害比例治療(非屬性公式), 治療量=ofDamage×本次觸發事件的
    # 實際傷害量(dmg 由反應式呼叫端傳入), 與 coef/scale 屬性公式互斥擇一, 見草船借箭。
    # 批F: heal 選標對齊本文全面改造新增3個heal專屬欄位(who/n/nMax/targetSel已是全域欄位,
    # 不重複於此列出) —— all: 「我軍全體」精確表達(對稱amp/mitig/stat等效果種類既有
    # 「who:ally無n→全體」通用慣例, heal過去無此路徑, 見金丹秘術); preferLowest: 群體治療時
    # 優先完全恢復兵力最低者(青州兵「優先完全恢復我軍兵力最低單體」); sharedPool: 總治療量
    # 一次算出後依序填滿(對應「總治療率」分攤語意, 與preferLowest常搭配使用, 見青州兵/既有
    # sgz.py 批52 selftest 142號斷言)。三者皆只在 k=="heal" 分支實作(非跨k通用欄位), 故歸入
    # PER_KIND_FIELDS 而非 KNOWN_EFFECT_FIELDS。
    # 批J(禁近似令-transfer轉移族): redirect新增guardFor欄位——對稱既有counter的
    # guardFor:"leader"(守護式反擊), 這裡是「單次全額(或e.share指定比例)代承」模式: 登記進
    # allies[0].absorbGuards/absorb_guards, 由hit()在主將受普攻時只轉移「這一下」的傷害(古之
    # 惡來「...隨後為我軍主將承擔此次普通攻擊」), 見engine.js/sgz.py redirect分支對
    # e.guardFor==="leader"的判斷。guard欄位新增合法值"random_sub"(隨機非主將副將, 夢中弒臣
    # 「使隨機副將為自己分擔」), 沿用既有guard欄位本身(R9只檢查欄位名而非列舉值), 不需額外
    # 登記。
    "redirect": {"guard", "share", "normalOnly", "guardFor"},
    "settle": {"init", "max", "base", "per", "perStackFrom", "singleTarget"},
    # 批K: perStackFrom(讀取指定stackId的疊層數代入coef公式)/singleTarget(結算只打單一目標
    # 而非整隊)(dynamic_coef_from_counter族), 密計誅逆首次落地, 見上方別名「settle讀取指定
    # 疊層計數器」條目。
    # 批J: stealStat(偷屬性) —— stat(欲偷的屬性名)/amount(基礎欲偷量, 受scale縮放)/
    # recipientSel(targetSel準則字串, 從allies挑受益者, 省略時預設caster本身)。who/dur/scale/
    # scaleDiv/everyRound/rate皆屬KNOWN_EFFECT_FIELDS全域欄位, 不重複於此列出。見
    # engine.js/sgz.py k==="stealStat"分支, 雁行陣「使我軍統率最低單體偷取敵軍全體10點統率」
    # 首次落地, engine_limitations.md本批新節。
    "stealStat": {"stat", "amount", "recipientSel", "statOptions", "victimIsTgt"},
    # 批K: statOptions(陣列) —— 每次觸發隨機從陣列選一個屬性欄位偷取(至柔動剛「任一屬性」),
    # 見上方別名「任一屬性隨機三選一」條目。
    # 禁近似令-批L: victimIsTgt(bool) —— 受害者精確鎖定「本次反應式事件的另一方」(apply_effects
    # 第2參數tgt, 於on_hit反應式呼叫時=攻擊者), 對稱who=="eventTarget"精確選標精神但stealStat
    # 有自己的targeting早退路徑不經過通用dests/who pipeline, 故另立此欄位。先登死士首次落地。
    # 批J: transferMitig(把來源側當下實際持有的正向mitig buff實例整個搬到去向側隨機一人身上)
    # ——from/to(各為"enemy"/"ally", 指定來源/去向側)。dur屬KNOWN_EFFECT_FIELDS全域欄位。見
    # engine.js/sgz.py k==="transferMitig"分支, 雁行陣「轉移傷害降低」首次落地。
    "transferMitig": {"from", "to"},
    # 批J: transferDebuff(把來源側群體當下實際持有的負面狀態隨機挑n~nMax種不同種類整個搬到
    # 去向側隨機單位身上)——from/to同transferMitig。n/nMax/dur皆屬KNOWN_EFFECT_FIELDS全域
    # 欄位。見engine.js/sgz.py k==="transferDebuff"分支, 雁行陣「轉移負面狀態」首次落地。
    "transferDebuff": {"from", "to"},
    "block": {"val", "times"},  # 批22: 次數型格擋(抵禦/警戒同族) —— val:1.0全擋/0.x部分減傷, times:剩餘次數
    "chargeAdd": {"max"},  # 批A(11筆高嚴重重建): 「可消耗資源池」獲得端(死戰不退「蓄威」),
    # max=封頂層數(預設20); 觸發機率靠effects級通用的e.rate(見KNOWN_EFFECT_FIELDS)+
    # when.on:"damaged"反應式擲骰通道, 非chargeAdd自身欄位。見engine.js/sgz.py Unit.charge/
    # self.charge欄位, engine_limitations.md第46節。
    "chargeConsume": {"coef", "kind", "decayPer", "maxChain"},  # 批A: 「可消耗資源池」消耗端,
    # coef=每次消耗造成的傷害率, kind=傷害類型, decayPer=每次觸發後機率遞減量(預設0.08),
    # maxChain=每回合最多觸發次數(預設5, 見chargeConsumedThisRound/charge_consumed_this_round
    # 逐回合歸零計數器)。基礎機率靠effects級通用的e.rate/e.scale(見KNOWN_EFFECT_FIELDS)。
    "capture": {"altCoef"},  # 批52j: 捕獲(暗箭難防獨立狀態) —— altCoef(選填, 預設5.3): 已有
    # 敵軍武將被捕獲時, 本次改對該已捕獲者造成altCoef傷害率直傷(取代再次擲rate捕獲判定), 見
    # engine.js/sgz.py k=="capture"分支對e.altCoef的讀取。n/dur/rate/scale/scaleDiv 皆屬
    # KNOWN_EFFECT_FIELDS全域欄位(捕獲判定的目標數/持續回合/發動率/受速度等屬性縮放), 不重複
    # 於此列出。
    "proxyNormal": {"srcSel", "ifNoExtra", "ifHasExtra"},  # 批52i: 代理完整普通攻擊(垂心萬物
    # 「若其不處於連擊狀態則使...對敵軍單體發動一次普通攻擊」) —— srcSel(依準則挑選出手的
    # 我軍武將, 如maxForce=武力最高)、ifNoExtra/ifHasExtra(選填, 依代理出手者當下是否處於
    # 連擊狀態決定本效果是否觸發, 與proxyHit互斥搭配表達「非連擊才代打完整普攻, 連擊時改另一
    # 效果」的分支對), 見engine.js/sgz.py k=="proxyNormal"分支(doNormalAttack/do_normal_attack
    # 完整管線代打, 含突擊/everyN/連擊)。
    "proxyHit": {"srcSel", "checkSrcSel", "ifNoExtra", "ifHasExtra", "coef", "kind"},  # 批52i:
    # 代理單次直傷(垂心萬物「連擊時改謀略90%」分支) —— checkSrcSel(依準則挑選「檢查連擊狀態」
    # 的對象, 可與srcSel出手者不同人)、srcSel(依準則挑選實際出手的我軍武將)、ifNoExtra/
    # ifHasExtra(同proxyNormal, 依checkSrcSel挑出的人當下是否連擊決定觸發)、coef/kind(本次
    # 直傷的傷害率與類型, 對稱一般hit()呼叫), 見engine.js/sgz.py k=="proxyHit"分支。
    "huchen": {"base", "per", "maxHits", "kind", "ampOnSettle", "ampMaxStack"},  # 批52d: 虎嗔
    # (將門虎女負面狀態, 專為本戰法建置的原語) —— base(初始結算傷害率, 預設0.20)、per(每次
    # 受傷疊加量, 預設0.30)、maxHits(滿幾次疊層立即提前結算, 預設3)、kind(結算時的傷害類型,
    # 預設沿用t.kind或"phys")、ampOnSettle(結算時施放者獲得的兵刃傷害amp加成, 預設0.08)、
    # ampMaxStack(該amp加成的疊加上限, 預設99=近似無上限), 見engine.js/sgz.py k=="huchen"
    # 分支對u.huchen狀態的建立, 以及settleHuchen()/結算函式對滿maxHits或到期時的提前/自然
    # 結算判斷。dur(維持回合數)/who/n(目標數)皆屬KNOWN_EFFECT_FIELDS全域欄位, 不重複列出。
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
# dispel 也排除(各自有獨立選標邏輯, 不讀 R13 讀取的 e["n"]全體池慣例; heal 有專屬的 R33
# 規則負責, 見下方——批52/批F後 heal 已支援 who/e.n/targetSel/e.all, 不再是"固定選我方兵力
# 最低者"的舊行為, 但其選標語意與 amp/mitig/stat 等全體池效果不同款(who:eventTarget反應式/
# targetSel準則選標/e.all全體皆為heal特有分支), 故不直接併入本規則, 另開R33專屬處理;
# dispel 目前對 who 指定的整組 dests 生效, settle/redirect 有各自的目標決定方式, 見
# engine_limitations.md)。
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
    """收集戰法頂層 + 所有效果層級的「非空字串」揭露文字(裸 bool 如 _est:true 不算)。
    批41 B(R27設計時發現的既有盲點修復): 原本只掃 p 頂層 + p["effects"], 完全遺漏
    p["extraHits"]/p["choices"](及其內部 effects)的揭露文字——火燒連營的「自身為主將時
    提高至70%」條件實際已在 extraHits[0]._note 誠實揭露(取主將滿級值), 但因未被此函式
    掃到, 會被任何倚賴 _topic_disclosed(effect=None) 的規則(R20/R25/R26/新R27)誤判為
    未揭露。全庫核對(scratchpad, 17處extraHits/choices含揭露文字)後補掃描, 只會讓既有
    豁免範圍變寬(不會把已抓到的真違規變不違規, 因現況全庫0違規基準線), 淨效果是消除
    這類假陽性, 不影響任何其他規則的既有判定。"""
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
    for eh in p.get("extraHits", []) or []:
        for k in TEXT_DISC_KEYS:
            v = eh.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v)
    for ch in p.get("choices", []) or []:
        for k in TEXT_DISC_KEYS:
            v = ch.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v)
        for e in ch.get("effects", []) or []:
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


R13_TOPIC_PROXIMITY_WINDOW = 40  # 批45 D: keywords與kind_kw命中位置的相鄰窗口(字元距離), 見下方 _topic_disclosed docstring。
# 40字取自校勝帷幄回歸樣例的實測距離(「amp」與「受智力影響」相隔30字, 同一句子內的正常語意
# 修飾距離)+安全餘裕, 同時鴆毒案例的假豁免主要靠 AMBIGUOUS_TOPIC_KW 語境門檻擋下(「目標」與
# 「統率」相隔僅7~10字, 在此window內, 但因兩者分屬不同句子語意不相關, 靠語境門檻而非
# window本身排除), 兩個修法互補, 不衝突。


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
    #
    # 批45 D: 歧義詞需搭配強語境(修復鴆毒漏網案例) —— 原本只要求 top_texts 內「主題關鍵字」
    # (keywords)與「該k的自稱詞彙」(kind_kw)各自在文字裡任意位置出現一次即算數(both present
    # anywhere)。多數呼叫端的 keywords/kind_kw 本身已是語意明確的詞彙(如"settle"/"counter"/
    # "scale", 幾乎不會用在其他語境), 兩者同文字內各出現一次已是足夠訊號, 不應收緊。但少數
    # 泛用中文詞(見 AMBIGUOUS_TOPIC_KW, 目前僅"目標"/"人)")語意含糊, 可能指涉"bug本身所在
    # 的目標邏輯"而非"目標範圍描述"(鴆毒實測案例: 頂層_note同時討論「(1)武力降低30%數值
    # 修正」與「(2)dot結算目標邏輯錯誤打統率最高敵將而非中毒目標本身」兩個無關子議題, 前者
    # 巧合含kind_kw「武力」, 後者巧合含keywords「目標」與kind_kw「統率」湊巧同句相鄰, 兩者
    # 分居不同句子卻被視為同一組"明確指涉"而豁免了完全不相關的stat效果e.n缺口)。修法: 僅
    # 對 AMBIGUOUS_TOPIC_KW 成員要求額外搭配 AMBIGUOUS_TARGET_CONTEXT_RE(N人/目標數/→/~/
    # e.n等明確的目標範圍複合語境)才算數, 其餘正常語意明確的關鍵字維持原有"同文字內出現即可"
    # 的既有寬度, 不影響R1/R6/R20等其他規則既有的正確豁免判定。
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
        kw_positions = []
        for kw in keywords:
            for m in re.finditer(re.escape(kw), txt):
                if kw in AMBIGUOUS_TOPIC_KW:
                    window = txt[max(0, m.start() - 6):m.start() + len(kw) + 6]
                    if not AMBIGUOUS_TARGET_CONTEXT_RE.search(window):
                        continue  # 歧義詞("目標"/"人)")須鄰近明確目標範圍語境才算數, 純粹提及不算
                kw_positions.append(m.start())
        if not kw_positions:
            continue
        if kind_kw is None:
            return True  # 無對照表可查, fallback 維持批28 A2 舊寬度(仍要求keywords本身有命中)
        kind_positions = [m.start() for kw in kind_kw for m in re.finditer(re.escape(kw), txt)]
        if not kind_positions:
            continue
        for kp in kw_positions:
            for kdp in kind_positions:
                if abs(kp - kdp) <= R13_TOPIC_PROXIMITY_WINDOW:
                    return True
    return False


# 批45 D: 語意含糊、可能指涉「與目標範圍無關的其他事物」的泛用中文詞——目前僅"目標"(可能指
# "bug所在的目標邏輯"而非"目標範圍描述")與"人)"(可能是任意帶"人"字的片語巧合帶括號)。"單體"/
# "群體"/"全體"本身已是強訊號(這三詞在本庫語境幾乎只用於目標範圍描述), 不需要額外語境即可
# 採信, 不納入此集合。
AMBIGUOUS_TOPIC_KW = ("目標", "人)")
AMBIGUOUS_TARGET_CONTEXT_RE = re.compile(r"\d\s*人|目標數|→|~|e\.n|e\['n'\]|e\[\"n\"\]")  # 歧義詞需鄰近此類明確目標範圍複合語境才算數


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
    "若XX統領": "ifLeaderIs(批44新增, 效果級/extraHits段級「隊伍主將(allies[0])的武將名須匹配指定"
                "值」條件閘門, 對稱ifLeader(布林)——ifLeaderIs 額外要求 allies[0].g.name 等於指定"
                "值(字串或陣列, 陣列為OR語意), 見 engine.js/sgz.py applyEffects/fireExtraHits 對"
                "e.ifLeaderIs/eh.ifLeaderIs 的判斷。「特定武將統領時, XX加成」這類措辭若聲稱"
                "「引擎無持有者=特定武將的條件判斷」屬 ifLeaderIs 落地前的舊近似說明, 落地後應"
                "改寫並補上 ifLeaderIs; 惟需注意 ifLeaderIs 只解決「條件判斷」本身, 若該戰法的"
                "缺口是「上游效果/scale曲線/rate本身缺失」等其他維度, ifLeaderIs 無法單獨解決,"
                "應個別核實, 不可一概而論已解決)",
    "統領時提升": "ifLeaderIs(同上)",
    "持有者為": "ifLeaderIs(同上)",
    "傷害來源區分": "dmgType/normalOnly(批24/28新增, amp/mitig 效果欄位, dmgType 區分兵刃/謀略"
                "來源, normalOnly 區分普攻/戰法來源, 見 engine.js addbonus() 的 dmgType/isNormal"
                "過濾參數; 「引擎mitig無傷害來源區分」這類措辭應視為兩原語落地前的舊近似說明)",
    "普攻傷害": "normalOnly(批28新增, amp/mitig/redirect 效果欄位, 限定只對「普通攻擊」造成/受到的"
                "傷害生效, 見 engine.js addbonus()/hit() 對 f.normalOnly + isNormal 的過濾判斷)",
    "代替主將": "guardFor(批28新增, counter 效果欄位 guardFor:\"leader\", 登記進主將"
                "counterGuards 清單, 由 hit() 在主將受普攻時代為觸發還擊, 見 engine.js/sgz.py)",
    "主將受擊時": "guardFor(同上)",
    "為主將承受": "guardFor(同上; 若該筆描述的是「代為反擊攻擊者」而非「代為承受傷害轉移」, 對應"
                "counter.guardFor:\"leader\"; 若確實是傷害轉移/代承語意, 批J(禁近似令-transfer"
                "轉移族)已新增 redirect.guardFor:\"leader\"(單次全額或e.share指定比例代承, 登記進"
                "allies[0].absorbGuards/absorb_guards, 見hit()內對應判斷, 古之惡來「隨後為我軍"
                "主將承擔此次普通攻擊」首次落地)——「redirect機制只能保護全體我軍分擔傷害, 無法"
                "精確限定僅主將+僅這一次」這類措辭是redirect.guardFor落地前的舊近似說明, 落地後"
                "應改寫並補上k:\"redirect\"+guardFor:\"leader\"。",
    "單一目標傷害轉移": "redirect.guardFor:\"leader\"(批J新增, 同上「為主將承受」條目)",
    "單次代承": "redirect.guardFor:\"leader\"(批J新增, 同上「為主將承受」條目; 「單次」對應"
                "guardFor機制本身每回合限觸發1次的節流慣例, 「全額」對應e.share預設1.0)",
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
    "任一控制狀態": "ifTargetHas 支援陣列(批I新增, 禁近似令-scale/比較族)——OR語意, 目標命中陣列"
                "內任一單一狀態即算符合(震懾/計窮/繳械/混亂等), 見 sgz.py/engine.js target_has()/"
                "targetHas() 對 list/Array 輸入的遞迴OR判斷, ifTargetHasNot 沿用同一函式取反"
                "(De Morgan's律自動給出正確的「皆非」語意)。深藏若虛/百步穿楊/橫掃千軍首次落地。"
                "「ifTargetHas只能擇一/單值」這類措辭是陣列支援落地前的舊近似說明, 落地後應改寫"
                "並補上陣列值。",
    "繳械或計窮": "ifTargetHas 支援陣列(同上, 橫掃千軍原文用字)",
    "虛弱狀態": "target_has()/targetHas() 新增 weak/虛弱 ctype(批I新增, 見「u.addbonus('amp')<=-1」"
                "判斷, 對稱既有extra/群攻用addbonus查詢的慣例), 可作 ifTargetHas/ifTargetHasNot 的"
                "值使用(挫志怒襲「已處於虛弱狀態」)。「引擎無法偵測amp(-1)虛弱狀態」這類措辭是"
                "weak ctype落地前的舊近似說明, 落地後應改寫並補上 ifTargetHasNot:\"weak\"。",
    "受自身最高屬性影響": "scale:\"maxStat\"(批I新增, 禁近似令-scale/比較族)——動態取施放者當下"
                "四維(force/intel/command/speed, 不含魅力)最高一項代入SCALE_G, 見 sgz.py/engine.js"
                " scale_of()/scaleOf() 對 scale===\"maxStat\" 的特判分支, 零新增呼叫點(全庫既有"
                "svVal/svMult/svAdd/lockedScaleOf 一律透過scale_of()讀取)。扶危定傾/剛柔並濟/"
                "整軍經武首次落地。「scale只支援固定單一屬性, 無法表達取最高者」這類措辭是"
                "maxStat落地前的舊近似說明, 落地後應改寫並補上 scale:\"maxStat\"。",
    "受最高屬性影響": "scale:\"maxStat\"(同上)",
    "自身最高屬性": "e.stat===\"maxStat\"(批I新增, k===\"stat\"效果動態解析為施放者當下四維最高"
                "一項的欄位名, 見 sgz.py resolve_stat_field()/engine.js resolveStatField(); 與"
                "scale===\"maxStat\"共用「四維中最高一項」判斷但消費端不同(一個回傳倍數, 一個回傳"
                "欄位名)。形一陣首次落地。",
    "取最高一項": "scale:\"maxStat\" 或 e.stat===\"maxStat\"(同上兩則, 依語境擇一: 縮放倍數用"
                "scale, 屬性欄位選擇用e.stat)",
    "武力較高": "ifStatCompare(批I新增, 禁近似令-scale/比較族)——比較「參照方(施放者/我軍主將)"
                "vs目標」同一屬性大小, 決定效果/extraHits段是否生效, 見 sgz.py stat_compare_ok()/"
                "engine.js statCompareOk()。摧鋒斷刃「若自身武力較高」首次落地。「引擎無法比較"
                "施放者與目標屬性高低」這類措辭是ifStatCompare落地前的舊近似說明, 落地後應改寫"
                "並補上 ifStatCompare。",
    "智力高於": "ifStatCompare(同上; 竊幸乘寵「若自身智力高於目標」)",
    "魅力低於": "ifStatCompare(同上; vs:\"leader\"變體, 比較目標vs我軍隊伍主將而非施放者自身,"
                "聚石成金「敵軍魅力低於我軍主將」)",
    "雙方智力差": "scaleCompare(批I新增, 禁近似令-scale/比較族)——施放者vs目標同一屬性「差值」"
                "代入縮放曲線(對稱scale_of單方固定屬性, 但讀取雙方差值), 見 sgz.py"
                " scale_compare_of()/engine.js scaleCompareOf()。神機妙算「並基於雙方智力差額外"
                "提高」首次落地(掛在頂層t.scaleCompare, 於active_fired_for()/activeFiredFor()"
                "主coef傷害段消費)。「智力差加成機制完全未建模,現有scale機制只讀取單方屬性」"
                "這類措辭是scaleCompare落地前的舊近似說明, 落地後應改寫並補上 t.scaleCompare。",
    "雙方武力之差": "scaleCompare(同上模式; 若該筆已因文本修正/查證而確認本文無此描述, 應視為"
                "stale交叉引用移除, 非仍待落地, 見才辯機捷批I複核個案)",
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
    "突擊戰法造成的傷害": "chargeOnly(批40 B新增, amp 效果欄位, 對稱於既有 activeOnly, 見 sgz.py/"
                "engine.js 的 is_charge/isCharge 參數穿透 hit()→damage()→amp()/addbonus(); "
                "批31 A 原本把「突擊」傷害誤標記 is_active=True(見 fight() 主迴圈突擊擲骰呼叫點),"
                "與「主動戰法」(士爭先赴)混為一談, 批40 B 已修正呼叫點改傳 is_charge, 兩者現為"
                "互斥分類, 見一鼓作氣「突擊戰法造成傷害提升12%」/藏刀「突擊戰法造成傷害降低5%」)",
    "突擊戰法造成傷害": "chargeOnly(同上)",
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
                "值上限clamp, 見 engine.js/sgz.py 的 SCALE_G/scale_of/cap_val_of; 批46 A: rateup/"
                "chargeup 系另有獨立曲線(rateScaleOf/rate_scale_of, 預設除數384.6, 十二奇策"
                "scaleDiv:335, 見calibration_anchors shierqice_20260707), 與此處SCALE_G系(350/375)"
                "是兩條互不相干的曲線族, 完整清單見engine_limitations.md「曲線圖鑑」節)",
    "375": "scaleDiv(同上; 「除數375」「375曲線」等措辭指此欄位)",
    "335": "rateup的e.scaleDiv(批46 A新增, 十二奇策專屬曲線, 見calibration_anchors "
                "shierqice_20260707; 與上方SCALE_G系350/375是不同曲線函式, 不可混用)",
    "值上限": "capVal(批35新增, 效果級可選欄位, 縮放後clamp, 見 cap_val_of/capValOf; "
                "「狀態效果上限=基礎值×2」慣例不自動套用, 逐效果顯式標)",
    "準備階段鎖定": "lockedScaleOf/locked_scale_of(批35新增, 見 engine.js/sgz.py 對 block 效果"
                "scale 縮放值的準備階段鎖定快取, caster.scaleLock/scale_lock, 效果物件本身當鍵;"
                "「開戰後智力變動會重新計算」這類措辭若指 block 是 stale 的, 落地後應改寫)",
    # 批42: eventTarget(who值)/stackKey+perStack+maxStacks(per-target疊層)/onMaxStacks+
    # globalMax+globalEffects(雙閾值觸發) —— 傲睨王侯「敵軍目標受普攻時觸發1個破綻, 該目標
    # 降3%可疊…單目標破綻全觸發→…全場破綻觸發後→…」首次落地新增的一族原語, 見
    # engine.js/sgz.py k=="stat"&&e.stackKey 分支 + who=="eventTarget" 分派 +
    # engine_limitations.md 第40節。刻意不收錄過於通用的別名(如「疊加5次」「破綻」這類
    # 純自然語言措辭, 大量既有戰法的_note/_todo會用相近字眼描述「仍未解決」的其他獨立缺口
    # ——如虎侯要的是add固定點數疊層, 本批只解決了mult百分比疊層, 兩者不同機制, 若收錄
    # 「疊加5次」當別名, 虎侯正確描述剩餘缺口的措辭反而會被R20誤判成stale——只收錄不含糊
    # 指向本批新增能力本身的精確措辭。
    "事件單位本身": "who:\"eventTarget\"(批42新增, 效果級who值, 精確鎖定跨單位事件廣播"
                "(when.who:\"ally\"/\"enemy\")的事件單位本身, 而非泛用敵軍全體/隨機N人, 見"
                "engine.js/sgz.py opt.evtTarget/evt_target 參數)",
    # 批42: 用比「疊加5次」/「破綻」精確得多的完整片語當別名, 只命中「聲稱stat完全不能疊層」
    # 這種已被推翻的blanket claim, 不會誤傷「stackKey機制現階段只支援mult不支援add」這類
    # 正確描述剩餘缺口(add類疊層仍未落地)的措辭(見虎侯_note改寫)。
    "stat原語完全無疊層計數能力": "stackKey/perStack/maxStacks(批42新增, k==\"stat\"效果欄位,"
                "支援mult百分比per-target疊層, 見傲睨王侯; onMaxStacks(該目標本地池耗盡時"
                "額外套用的效果陣列)/globalMax+globalEffects(持有者跨目標累計觸發次數達門檻"
                "時套用的效果陣列)為同批新增的雙閾值觸發原語, 見engine.js/sgz.py k==\"stat\""
                "&&e.stackKey分支。若戰法描述的是add固定點數疊層(如虎侯「+15點可疊加5次」),"
                "此原語仍不支援, 非stale, 見engine_limitations.md第40節)",
    # 批43: add型stackKey(平點疊層)/on:"healed"(受到治療反應式事件)/ifStackMaxed(疊滿條件閘門)
    # —— 全庫兄弟遷移掃描(虎侯/權僭九鼎/長驅直入)首次落地新增的三個原語, 見
    # engine.js/sgz.py k=="stat"&&e.stackKey分支的e.add路由 + healedFor/healed_for +
    # applyEffects對e.ifLeader之後新增的e.ifStackMaxed判斷式 + engine_limitations.md第43節。
    "stackKey機制現階段只支援mult不支援add": "add型stackKey(批43新增, k==\"stat\"效果欄位"
                "e.add:true+e.stackKey/perStack/maxStacks, 支援平點(add)per-target疊層, 對稱"
                "批42既有的mult型, 見engine.js/sgz.py k==\"stat\"&&e.stackKey分支的e.add路由,"
                "虎侯已用此原語遷移「+15點統率可疊加5次」)。若戰法描述的疊層形態仍是本原語"
                "涵蓋範圍外的其他機制(如飛熊軍「累計治療量」連續數值計數器, 非離散觸發次數),"
                "此原語不適用, 非stale)",
    "add類疊層原語仍未落地": "add型stackKey(同上, 批43新增)",
    "受治療時觸發": "on:\"healed\"(批43新增, when.on==\"healed\", 見engine.js/sgz.py的"
                "onHealTacs/onHealEffectTacs + healedFor()/healed_for() 掛在 applyEffects() 的"
                "k==\"heal\"分支結算完成(hurt.troop已回補)之後, 對受治療者(hurt)/其隊友/其敵隊"
                "廣播; 支援who:\"self\"(含省略, 兩者視為同義)/\"ally\"/\"otherAlly\"/\"enemy\","
                "見權僭九鼎「自身受到治療時+5統率智力, 可疊加」遷移。只支援效果級"
                "(onHealEffectTacs), 不支援戰法級(比照批31 activeFired precedent); 「造成治療"
                "效果時」(caster-framed, 如義心昭烈「自身造成治療效果時」)是相反方向的事件"
                "(以施法者為主詞, 對稱dealtDamage), 本批healed事件是receiver-framed(以受治療者"
                "為主詞, 對稱onHit), 兩者不同方向, 若戰法描述的是caster-framed語意, 此原語不"
                "適用, 非stale, 見engine_limitations.md第43節)",
    "無此事件": "on:\"healed\"(同上; 「治療觸發, 引擎無對應when.on類型」這類措辭若指"
                "receiver-framed「受到治療時」, 落地後應改寫)",
    "疊加N次後才觸發": "ifStackMaxed(批43新增, 效果級旗標, 讀取caster.stack.n>=caster.stack.max"
                "既有狀態的條件閘門, 搭配既有everyRound逐回合重新判定, 精確表達「疊加N次後"
                "才生效」的延後窗口, 見engine.js/sgz.py applyEffects對e.ifLeader之後新增的判斷式,"
                "長驅直入「疊加5次後...降低16%」已用此組合遷移。「戰法級when會連帶鎖stack段」"
                "的舊困境已由效果級ifStackMaxed(非戰法級when)解套, 見engine_limitations.md第43節)",
    "群體目標各自獨立選標": "sameTargets(批45新增, 效果級旗標, 見engine.js/sgz.py applyEffects"
                "對e.sameTargets的判斷式, 沿用同一次apply_effects呼叫內先前已命中的群體目標"
                "(main_hit_tgts, 來自主coef段的pick_targets結果, 或母戰法無coef時由本戰法內"
                "首個命中群體的sibling效果就地提供), 取代「coef段與效果段各自獨立pick_targets,"
                "3人隊僅1/3機率同組」的舊近似, 見engine_limitations.md第45節)",
    "各自獨立pick_targets": "sameTargets(同上)",
    # 批A(11筆高嚴重重建): 8個新原語, 見engine_limitations.md第46節。
    "目標對其友軍單體": "extraHits.who:\"mainTargetAlly\"(批A新增, 對稱既有sameTarget/enemyLeader,"
                "方向反轉——攻擊者不是持有者u自己, 而是main段命中的目標tgt本身被強制對其own"
                "隊友出手, 見engine.js/sgz.py fireExtraHits()/fire_extra_hits()的"
                "mainTargetAllyAtk/main_target_ally_atk分支, 偽書相間首次落地)",
    "強制攻擊己方": "extraHits.who:\"mainTargetAlly\"(同上)",
    "類型取決於": "eh.kindByStat:\"maxForceIntel\"(批A新增, 動態比較atk本身force/intel"
                "兩項屬性取較高者決定傷害類型, 對比批34胡笳餘音「取較高者」措辭遇到同類需求時"
                "只能靜態近似取intel的舊慣例, 這裡是真正runtime動態比較, 見fireExtraHits()/"
                "fire_extra_hits()尾端的ehKind/eh_kind計算式, 偽書相間首次落地)",
    "武力、智力較高的一項": "eh.kindByStat:\"maxForceIntel\"(同上)",
    "否則施加": "e.ifTargetHasNot(批A新增, ifTargetHas的反向, 只對「尚未有該狀態」的目標生效,"
                "見engine.js/sgz.py applyEffects()/apply_effects()對e.ifTargetHasNot的過濾判斷,"
                "偽書相間「否則施加混亂」首次落地)",
    "主將發動主動": "when.casterIsLeader(批A新增, activeFired反應式的效果級/戰法級旗標, 限定觸發"
                "事件的發動者u本身須為其隊伍主將allies[0], 而非要求持有者holder是主將——與"
                "who:\"ally\"廣播疊加使用(who負責「誰能聽到」, casterIsLeader負責「發動者是不是"
                "主將」), 見engine.js/sgz.py activeFiredFor()/active_fired_for()的"
                "casterIsLeaderOk/caster_is_leader_ok判斷式, 十勝十敗首次落地; 密計誅逆同批"
                "復用於dealtDamage事件)",
    "我軍主將發動": "when.casterIsLeader(同上)",
    "造成大於300的傷害": "when.dmgAbove(批A新增, dealtDamage/damaged反應式的傷害量閾值閘門,"
                "戰法級/效果級皆支援, 見engine.js/sgz.py dealtDamageFor()/dealt_damage_for()與"
                "onHitFor()/on_hit_for()新增的dmgAboveOk/dmg_above_ok判斷式, 密計誅逆(dealtDamage"
                "方向)/承天靖世(damaged方向, 延伸到on:\"damaged\")首次落地)",
    "收到高於最大兵力": "when.dmgAbove(同上, damaged方向)",
    "傷害量閾值閘門": "when.dmgAbove(批A新增, 同上; 「無傷害量條件欄位」這類措辭若指此需求,"
                "落地後應改寫並補上when.dmgAbove)",
    "最終傷害降低15%": "k:\"amp\"+e.stackKey(批A新增, amp效果的per-target疊層變體, 對稱既有"
                "k:\"stat\"+stackKey, 見engine.js/sgz.py k===\"amp\"分支的ampLayers/amp_layers"
                "計數器, 密計誅逆「敵軍單體造成的最終傷害降低15%...最多疊加3次」首次落地;"
                "若戰法描述的疊層形態是k:\"stat\"(屬性)而非k:\"amp\"(傷害加成), 仍應用既有"
                "stackKey機制, 非本條新增範圍)",
    "動態遞減機率的多次觸發消耗品層數": "k:\"chargeAdd\"+k:\"chargeConsume\"(批A新增, 「可消耗"
                "資源池」機制, 對稱既有k:\"stack\"但語意為「剩餘可消耗次數」而非「傷害增益倍率」,"
                "見engine.js/sgz.py Unit.charge/self.charge欄位+applyEffects/apply_effects的"
                "chargeAdd/chargeConsume分支, 死戰不退「蓄威層+消耗連鎖」首次落地; chargeAdd負責"
                "受擊觸發疊層(掛on:\"damaged\"), chargeConsume負責普攻後鏈式消耗造成傷害"
                "(掛on:\"dealtDamage\"+normalOnly, e.decayPer每次觸發後機率遞減量+e.maxChain每回合"
                "最多觸發次數上限, 見chargeConsumedThisRound/charge_consumed_this_round逐回合"
                "歸零計數器)",
    "受傷觸發概率疊層": "k:\"chargeAdd\"(同上)",
    "消耗時遞減觸發率": "k:\"chargeConsume\"(同上)",
    "效果級起始回合無法單獨表達": "e.when.hpBelow/hpAbove(批A新增, 效果級, 對稱既有戰法級"
                "t.when.hpBelow/hpAbove, 見engine.js/sgz.py everyRound分支新增的hpOk/hp_ok判斷式,"
                "奇兵間道「第5回合起若兵力低於50%...否則...」首次落地——過去戰法級t.when會連帶"
                "鎖住同戰法內其餘不需要hp條件的effects段, 現在同一戰法內部分effects段可各自"
                "獨立掛hp條件, 不強制共用同一個when)",
    "戰法級when會連帶鎖住前4回合": "e.when.hpBelow/hpAbove(同上)",
    # 批H: 會心(兵刃暴擊)/奇謀(謀略暴擊)真擲骰系統 —— k:"critUp"(機率, val加法累積,
    # dmgType路由"phys"=會心/"intel"=奇謀)+k:"critDmgUp"(觸發後傷害幅度加成, 疊在基礎
    # +100%之上), 見engine.js/sgz.py damage()對稱段落(擲骰rnd()<critRate命中則
    # base*=1+critBonus, TRACE「觸發會心,兵刃傷害提升100.00%」比照官方戰報原文)。取代
    # 全庫14筆(10戰法+4裝備)「crit-ev」機率×幅度EV折算常駐amp近似, 見
    # no_approx_inventory.json crit_system_primitive族/engine_limitations.md本節。
    "會心": "critUp/critDmgUp(批H新增, k:\"critUp\"=兵刃暴擊機率/k:\"critDmgUp\"=觸發後傷害"
                "幅度加成, 見damage()對稱段落; 「引擎無獨立會心判定事件/crit系統/crit_system_"
                "primitive原語族未落地」這類措辭是本原語落地前的舊近似說明, 落地後應改寫並"
                "補上critUp(dmgType:\"phys\"))",
    "奇謀": "critUp/critDmgUp(同上; dmgType:\"intel\"=奇謀/謀略暴擊)",
    "會心機率": "critUp(同上, dmgType:\"phys\")",
    "奇謀機率": "critUp(同上, dmgType:\"intel\")",
    "會心傷害": "critDmgUp(同上, dmgType:\"phys\", 幅度修飾語疊在critUp觸發後的基礎+100%之上)",
    "奇謀傷害": "critDmgUp(同上, dmgType:\"intel\")",
    "crit_system_primitive": "critUp/critDmgUp(批H落地, 見上方「會心」條目; no_approx_"
                "inventory.json該族14筆已逐筆遷移, 若仍見「待C類crit_system_primitive原語族"
                "落地」字樣屬批H之前的舊揭露, 應改寫)",
    "犧牲會心的二元觸發性質": "critUp(批H新增, 見上方「會心」條目——真機率擲骰已保留二元"
                "觸發性質與方差, 不再犧牲, 此措辭是crit-ev EV折算年代的舊近似說明)",
    # 批J: 禁近似令-transfer轉移族 —— stealStat(偷屬性)/transferMitig(buff轉移)/
    # transferDebuff(debuff轉移)三原語 + redirect新增guard:"random_sub"(隨機非主將副將代承)
    # /guardFor:"leader"(單次全額代承, 見上方「為主將承受」條目更新)。三者共同的核心約束:
    # 轉移量/轉移種類必須等於來源實際擁有的量/種類, 來源沒有就轉移0, 不無中生有。見
    # engine.js/sgz.py k==="stealStat"/"transferMitig"/"transferDebuff"分支 +
    # collectDebuffTokens/collect_debuff_tokens + engine_limitations.md本批新節。
    "偷取統率": "stealStat(批J新增, 禁近似令-transfer轉移族)——偷屬性原語, 從每個victim實際"
                "扣除min(e.amount×scale, victim現有可扣量), 不得扣至負值, e.recipientSel"
                "(targetSel準則字串)挑選受益者, 受益者只獲得所有victim實際被扣除量之加總(而非"
                "固定套用戰法表面數字), 見engine.js/sgz.py k===\"stealStat\"分支, 雁行陣「使我軍"
                "統率最低單體偷取敵軍全體10點統率」首次落地。「偷取統率/偷屬性完全無對應原語,"
                "用stat組合對稱近似」這類措辭是stealStat落地前的舊近似說明, 落地後應改寫並補上"
                "k:\"stealStat\"。",
    "偷屬性": "stealStat(同上)",
    "轉移傷害降低": "transferMitig(批J新增, 禁近似令-transfer轉移族)——把e.from側當下實際"
                "持有的正向mitig(傷害降低)buff實例整個搬到e.to側隨機一人身上, 若來源側當下無人"
                "持有這類buff則不觸發(轉移0, 不無中生有), 見engine.js/sgz.py"
                "k===\"transferMitig\"分支, 雁行陣首次落地。「需要buff實例內省+移除+複製到另一"
                "單位的原語, 現有dispel/redirect皆非此語意」這類措辭是transferMitig落地前的舊"
                "近似說明, 落地後應改寫。",
    "轉移負面狀態": "transferDebuff(批J新增, 禁近似令-transfer轉移族)——把e.from側群體當下"
                "實際持有的負面狀態隨機挑n~nMax種不同種類整個搬到e.to側隨機單位身上, 若來源側"
                "當下沒有負面狀態則轉移0種、只有部分種類現存也不硬湊到nMax要求, 見"
                "engine.js/sgz.py k===\"transferDebuff\"分支+collectDebuffTokens/"
                "collect_debuff_tokens, 雁行陣首次落地。「需要buff/debuff實例內省...現有dispel/"
                "redirect/雙方各自套用對稱效果近似皆非此語意」這類措辭是transferDebuff落地前的"
                "舊近似說明, 落地後應改寫。",
    "隨機副將分擔": "redirect guard:\"random_sub\"(批J新增, 禁近似令-transfer轉移族)——代承者"
                "=隨機一位當下存活的非主將副將(若無存活副將則guard退回caster本身, 天然等同"
                "「找不到可轉嫁對象就不轉嫁」, 不無中生有另尋轉嫁對象), 見engine.js/sgz.py"
                "redirect分支對e.guard===\"random_sub\"的判斷, 夢中弒臣「使隨機副將為自己分擔"
                "傷害」首次落地。「redirect原語的guardian/guarded方向與本戰法相反, 無法表達"
                "主將的傷害轉嫁給隨機1名副將」這類措辭是guard:\"random_sub\"落地前的舊近似說明,"
                "落地後應改寫並補上guard:\"random_sub\"。",
    "e.ofHeal": "e.ofDamage(既有原語, 批33新增/批A擴充)——ofDamage欄位語意其實已是「本次觸發"
                "事件的量」的通用比例治療, on:\"healed\"反應式(批43新增)呼叫端傳的是opt.healAmt/"
                "heal_amt(本次觸發事件的實際治療量), 批A(11筆高嚴重重建)已補上讀取此分支的程式碼"
                "(`ofEventAmt = opt.dmg != null ? opt.dmg : opt.healAmt`), 故治療量比例轉移不需要"
                "另外新增e.ofHeal欄位, 直接用既有e.ofDamage+on:healed組合即可精確表達(權僭九鼎/"
                "移花接木批J首次以此組合遷移heal-siphon類戰法)。「引擎現無e.ofDamage的heal版本,"
                "需要e.ofHeal讀opt.healAmt」這類措辭是批A能力落地前的舊近似說明, 落地後應改寫。",
    # 批K(禁近似令-收官): pre_damage_intercept族 —— k:"preDmgHook"(hookKind分流probVoid/
    # probMitig/stepMitig/deferSettle), 消費於damage()內(src/dst兩方向皆讀), 見engine.js/
    # sgz.py damage()對稱段落。取代「hit()只有事後廣播, 無法在troop-=dmg之前修改本次dmg」的
    # 舊架構限制說明。
    "傷害結算前攔截": "k:\"preDmgHook\"(批K新增, pre_damage_intercept族)——hookKind:"
                "\"probVoid\"(攻擊方自己掛, 每次造成傷害時機率使本次傷害乘(1-val), 挫銳)/"
                "\"probMitig\"(防禦方自己掛, 每次受到傷害時機率額外折減)/\"stepMitig\"(防禦方"
                "自己掛, 每次受擊按目前hits數遞減折減比例, 捨身救主/蕙質蘭心)/\"deferSettle\""
                "(防禦方自己掛, 每次受到傷害時pct比例移出, 以reducePct打折後分delayRounds"
                "回合攤還, 象兵),"
                "皆消費於damage()內(見engine.js/sgz.py對應段落), 見Unit.preDmgHooks/"
                "self.pre_dmg_hooks + deferredDmg/deferred_dmg。「無法在傷害結算前修改本次"
                "dmg數值, 只有事後廣播」這類措辭是preDmgHook落地前的舊架構限制說明, 落地後"
                "應改寫。",
    "無視防禦": "e.pierce:true(批K新增, dot效果級, engine_wiring_gaps_misc族)——強制該dot段"
                "damage()呼叫時forcePierce=true, 完全無視目標mitig(獅子奮迅), 見damage()"
                "forcePierce第9參數。",
    "攔截傷害": "preDmgHook(同上, hookKind:\"probVoid\")",
    # 批K: force_attack_reverse族 —— k:"taunt"+e.tauntTarget("leader"/"select")
    "反向taunt": "e.tauntTarget(批K新增, force_attack_reverse族)——\"leader\"=強制目標改為"
                "我方主將(武鋒陣)/\"select\"=依targetSel從敵軍挑一個「被攻擊」的目標(定謀貴決),"
                "見engine.js/sgz.py k===\"taunt\"分支。「taunt只有敵方被迫攻擊我方施放者一個"
                "方向, 無法指定其他目標」這類措辭是tauntTarget落地前的舊近似說明, 落地後應"
                "改寫。",
    "嘲諷反向": "e.tauntTarget(同上)",
    # 批K: splash_aoe_primitive族 —— k:"splash", 消費於doNormalAttack/do_normal_attack
    "濺射": "k:\"splash\"(批K新增, splash_aoe_primitive族)——普攻命中tgt後, 同時對tgt「同"
                "部隊其他武將」(fo中除tgt外存活成員)造成splashRatio倍率兵刃傷害, 見"
                "doNormalAttack()/do_normal_attack()消費端(瞋目橫矛/象兵/橫掃)。「引擎無濺射"
                "原語, 以extra額外傷害輸出近似」這類措辭是splash落地前的舊近似說明, 落地後"
                "應改寫。",
    "群攻濺射": "k:\"splash\"(同上)",
    # 批K: leader_dual_base_coef族 —— t.coefLeader/t.coefWhenLeader, 消費於fight()主迴圈
    "主將非主將兩個基礎係數": "t.coefLeader/t.coefWhenLeader(批K新增, leader_dual_base_coef"
                "族)——coefLeader: 主將時無條件切換頂層coef(神機妙算); coefWhenLeader: 僅"
                "當fire恰好透過whenLeader額外視窗通過時才切換(燕人咆哮第6回合), 見fight()"
                "主迴圈coefEff/coef_eff計算段。「頂層coef是戰法級單一值, 無法表達主將/非"
                "主將兩個不同基礎值分支」這類措辭是此二欄位落地前的舊近似說明, 落地後應改寫。",
    # 批K: faction_count_scale族 —— countAllyFaction()+e.rateFactionBonus
    "陣營計數": "countAllyFaction()/count_ally_faction()+e.rateFactionBonus(批K新增, "
                "faction_count_scale族)——數出隊伍中特定陣營人數, per×max(0,count-1)加成"
                "觸發率(南蠻渠魁/象兵), 見applyEffects/apply_effects的eRate計算段。「teamGate"
                "只回傳布林值, 無法數出隊伍中特定陣營人數」這類措辭是此組合落地前的舊近似"
                "說明, 落地後應改寫。",
    # 批K: rate_self_dynamic族 —— countActiveBuffTypes()+e.rateBonusPerBuffType/e.addPerBuffType
    "依自身持有增益狀態數動態調整": "countActiveBuffTypes()/count_active_buff_types()+"
                "e.rateBonusPerBuffType(觸發率)/e.addPerBuffType(stat平加)(批K新增, "
                "rate_self_dynamic族)——數出自身當下持有連擊/洞察/先攻/必中/破陣/規避狀態"
                "種類數, per×count動態加成(臥薪嘗膽/弓腰姬)。「e.rate是靜態擲骰值, 無法依"
                "當下持有幾種增益狀態動態相加」這類措辭是此組合落地前的舊近似說明, 落地後"
                "應改寫。",
    # 批K: dynamic_coef_from_counter族 —— e.stackId/ampLayersById + settle e.perStackFrom/e.singleTarget
    "settle讀取指定疊層計數器": "e.stackId(amp+stackKey效果, 寫入u.ampLayersById字串鍵索引)"
                "+k:\"settle\"的e.perStackFrom(讀取指定stackId的當下疊層數代入coef公式)+"
                "e.singleTarget(結算只打單一目標而非整隊)(批K新增, dynamic_coef_from_counter"
                "族), 見settle registration/discharge兩端(密計誅逆第6回合斬殺傷害率隨"
                "疊層數增幅)。「settle的base/per是靜態值, 無法讀取另一個效果物件的疊層數"
                "並代入coef公式」這類措辭是此組合落地前的舊近似說明, 落地後應改寫。",
    # 批K: counter_target_binding族 —— counterGuards.debuffAttacker/selfStack
    "guardFor反擊精確綁定同一人": "counterGuards條目的debuffAttacker(對攻擊者施加debuff)/"
                "selfStack(反擊執行者自身疊層增益)兩欄位(批K新增, counter_target_binding族),"
                "消費於hit()內counterGuards迴圈(古之惡來/虎衛軍), 見hit()/"
                "counter_guards消費端。「guardFor觸發的反擊與本戰法其他效果段的目標選標各自"
                "獨立, 無法讓兩者鎖定同一人」這類措辭是此組合落地前的舊近似說明, 落地後應"
                "改寫。",
    # 批K: once_consumable族 —— k:"armConsume"/k:"strike"+e.ifArmed, 通用e.once
    "消耗態狀態機": "k:\"armConsume\"(武裝一次性資格)+k:\"strike\"+e.ifArmed(消費, 消費後"
                "歸null)(批K新增, once_consumable族), 見applyEffects/apply_effects的"
                "armConsume/strike分支+Unit.armedConsume/self.armed_consume(十二奇策"
                "「下次發動主動戰法後」延遲單次消耗傷害)。「無「執行N次某動作後失效」的通用"
                "計數器」這類措辭(高櫓連營的ammo/ammoReloadLeader批52g已解決, 十二奇策的"
                "跨單位延遲消耗由本批armConsume/strike解決)是這兩組原語落地前的舊近似說明,"
                "落地後應改寫。",
    "一次性消耗資格": "k:\"armConsume\"/k:\"strike\"+e.ifArmed(同上)",
    "反應式一次性消耗": "e.once(批K新增通用版, 對稱既有everyRound內部的e.once, 現擴充成"
                "applyEffects/apply_effects開頭的通用閘門, 任何呼叫路徑皆生效)——效果級「整場"
                "戰鬥內只消耗一次」的持久化去重(誓守無降/淵然難測), 用caster.whenFired/"
                "self.when_fired(不隨回合重置)去重, 見applyEffects/apply_effects的e.once"
                "閘門與各反應式迴圈(onHitFor/on_hit_for等)的e.once檢查。「hitFlags只提供"
                "同回合節流, 無法表達整場戰鬥內只消耗一次」這類措辭是e.once通用化落地前的"
                "舊近似說明, 落地後應改寫。",
    # 批K: 其他小型原語(target_rank_branch/scale_compare延伸/hit_count_stage_trigger/兵種/血量條件)
    "目標恰好符合排名準則": "e.ifTargetIsRank/e.ifTargetIsRankNot(批K新增, target_rank_branch"
                "族)——比對已選定的效果目標是否恰為pickByCriterion(enemies,\"maxForce\"/"
                "\"maxIntel\")選出的排名冠軍, 見applyEffects/apply_effects的dests filter"
                "(閉月依目標恰為武力/智力最高分三支)。「targetSel只支援依準則主動挑目標,"
                "沒有已選定目標是否恰好符合某排名準則的事後判斷」這類措辭是此組合落地前的"
                "舊近似說明, 落地後應改寫。",
    "同一單位自己兩屬性互比": "e.ifSelfStatCompare(批K新增, scale_compare族延伸)——比較"
                "「已選定的效果目標自己」兩項屬性大小(與ifStatCompare的跨單位比較方向不同),"
                "見applyEffects/apply_effects的dests filter(淵然難測若傷害來源武將武力"
                "高於智力則...否則...)。",
    "依隊伍兵種類型分支": "e.ifEnemyTroop(批K新增, engine_wiring_gaps_misc族)——兵種由隊伍"
                "決定, enemies[0].ttype即代表整支敵隊兵種, 只在敵隊兵種恰好符合指定值時"
                "效果才生效(左右開弓如果目標為騎兵), 見applyEffects/apply_effects的效果級"
                "閘門。",
    "疊加次數作為觸發條件": "e.ifCasterStackAtLeast(批K新增, hit_count_stage_trigger族)——"
                "對稱既有e.ifStackMaxed(僅認已疊滿特例), 這裡是通用門檻caster.stack.n是否"
                "達到指定層數(水淹七軍第三/四次施放觸發settle/extraHits), 見applyEffects/"
                "apply_effects效果級閘門。",
    "他方單位血量條件": "e.ifTargetHpAbove/e.ifTargetHpBelow(批K新增)——已選定的效果目標"
                "(受益者)自己兵力百分比條件(肉身鐵壁當友軍兵力高於70%時), 與既有when.hpAbove/"
                "hpBelow(只認caster自身)方向不同, 見applyEffects/apply_effects的dests"
                "filter。",
    "任一屬性隨機三選一": "e.statOptions(批K新增, k:\"stealStat\"擴充)——每次觸發隨機從陣列"
                "選一個屬性欄位偷取(至柔動剛偷取來源智/統/速任一屬性), 見k===\"stealStat\""
                "分支。",
    "subs池targetSel": "who===\"subs\"+e.targetSel組合(批K新增)——targetSel的pool限縮到"
                "副將二人(三勢陣損失兵力較多的副將/另一名副將只在兩名副將間比較), 見"
                "applyEffects/apply_effects的targetSel pool判斷。",
    "同一次觸發同一目標的追加傷害": "k:\"strike\"+e.sameTarget/e.ifTargetHas(批K新增)——沿用"
                "本次applyEffects呼叫傳入的tgt(通常=同戰法effects陣列內排在前面的disarm等"
                "狀態效果剛命中的同一人), 靠effects陣列內順序執行解決execution ordering問題"
                "(驍健神行如果目標已經被繳械則造成兵刃攻擊), 見k===\"strike\"分支。",
    "counter依本次傷害量比例輸出": "c.ofDamage/counter物件的ofDamage欄位(批K新增, 對稱heal"
                "既有e.ofDamage慣例)——依本次受到的實際傷害量(dmg, 已經過block/shield折算)"
                "比例反彈, 取代固定coef重新計算一次全新damage()的做法, 見hit()的counter"
                "消費段(荊棘裝備受到普通攻擊時反彈5%傷害)。",
    "兵書效果無activeFired接線": "Unit.activeFiredBs/self.active_fired_bs(批K新增)——兵書"
                "效果級e.when.on===\"activeFired\"對稱既有onHitBs(受擊方向)的接線, 於"
                "activeFiredFor()/active_fired_for()補上消費端(大謀不謀每次成功發動主動"
                "戰法時...), 見Unit建構式與activeFiredFor對稱段落。「兵書效果只走self.bs"
                "獨立管線, 無法被active_fired_tacs掃描, 沒有when.on:activeFired接線」這類"
                "措辭是此接線落地前的舊近似說明, 落地後應改寫。",
    "兵書stackKey疊層消費端": "critUp+e.stackKey(既有原語, 批H)本就支援per-target疊層, 只"
                "是兵書效果過去缺乏activeFired接線讓觸發時機無法對上(見上方「兵書效果無"
                "activeFired接線」條目), 兩者組合(activeFiredBs+critUp+stackKey)即可精確"
                "表達(大謀不謀), 非「stackKey消費端本身不支援」的問題。",
    # 批K7(收官續): 剩餘_approx族群新增原語 —— 全部消費於engine.js/sgz.py對稱段落。
    "動態coef讀取": "e.coefFromStack(批K7新增, dynamic_coef_from_counter族)——k:\"dot\"效果"
                "讀取caster身上另一個k:\"stat\"+e.stackKey+e.stackId效果的當下疊層數(經"
                "amp_layers_by_id字串鍵跨效果傳遞), coef=base+per×layers, 見engine.js/sgz.py"
                "dot分支對coefFromStack的判斷(絕地反擊第5回合根據疊層數AoE傷害)。「需要settle"
                "風格動態coef讀取」這類措辭是coefFromStack落地前的舊近似說明。",
    "受傷回饋隊友": "k:\"dmgShare\"(批K7新增, engine_wiring_gaps_misc族)——目標受傷後, 額外"
                "對其隊伍中隨機一位其他成員造成val×dmg的分攤傷害, 見hit()/sgz.py hit()內"
                "dst.dmgShare/dst.dmg_share判斷式(連環計鐵鎖連環)。「傷害轉移給第三方無對應"
                "原語」這類措辭是dmgShare落地前的舊近似說明。",
    "狀態擇一觸發": "e.eitherK(批K7新增, 陣列)——效果套用時隨機從陣列擇一k值頂替e.k本身, 見"
                "engine.js/sgz.py applyEffects/apply_effects開頭對e.eitherK的判斷(溯江搖櫓"
                "計窮或震懾擇一)。「簡化為固定單一狀態,未表達擇一語意」這類措辭是eitherK落地"
                "前的舊近似說明。",
    "stat隨stack動態同步": "e.fromStack+perStackVal(批K7新增, dynamic_coef_from_counter族)"
                "——stat效果不自己疊層, 改註記同一持有者的k:\"stack\"計數器(this.stack/"
                "self.stack)額外驅動該stat屬性, 交給eff()/Unit.eff()即時讀取this.stack.n×"
                "perStackVal, 見engine.js eff()/sgz.py Unit.eff()對stack[\"statField\"]的"
                "消費(弓腰姬武力隨功能性增益數量動態成長)。「取單層滿級值靜態近似,與傷害段"
                "動態疊層不同步」這類措辭是fromStack落地前的舊近似說明。",
    "擊敗後授予": "e.onKill(批K7新增, engine_wiring_gaps_misc族)——k:\"pierce\"效果不立即"
                "套用, 改登記到Unit.onKillGrants/self.on_kill_grants, 待hit()偵測到本單位"
                "親手擊敗某目標(was_alive且troop<=0)時才真正授予, 見hit()對onKillGrants的"
                "消費段(虎痴破陣)。「約後半場生效,需val折算」這類措辭是onKill落地前的舊EV"
                "近似說明。",
    "即將受到普通攻擊": "k:\"preAttackHook\"(批K7新增, engine_wiring_gaps_misc族)——hookKind:"
                "\"redirectPre\"(即將受擊時依guard準則轉由隊友代承, 雲聚影從)/\"healAllyPre\""
                "(即將受擊時治療隨機隊友, 益其金鼓), 於do_normal_attack()/doNormalAttack()"
                "呼叫主hit()之前消費tgt.preAttackHooks/tgt.pre_attack_hooks, 每次真實擲骰"
                "(非prep一次性)。「engine無即將受擊事件掛鉤點, 只能prep一次性擲骰決定整場"
                "有無」這類措辭是preAttackHook落地前的舊EV近似說明。",
    "每回合恢復持續N回合": "k:\"regen\"(批K7新增, engine_wiring_gaps_misc族)——對稱dot的"
                "傷害版但方向是治療, 登記到目標Unit.regens/self.regens清單, 見tick()逐回合"
                "消費端(乘敵不虞休整狀態)。「heal效果不讀dur,只結算一次,折算成單次數值"
                "(2倍低估)」這類措辭是regen落地前的舊近似說明。",
    "extraHits自身傷害回血": "eh.lifesteal(批K7新增, engine_wiring_gaps_misc族)——對稱既有"
                "lifesteal但顆粒度縮小到只讀該extraHits段自身造成的傷害量, 見fire_extra_hits/"
                "fireExtraHits內對eh.lifesteal的消費(錦帆軍恢復傷害量30%兵力)。「30%傷害量"
                "回血未建模」這類措辭是eh.lifesteal落地前的舊缺口說明。",
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
    # 批B: 內部實作細節/純資料撰寫慣例欄位, 非「戰法撰寫者可能誤稱不支援」的通用能力語意
    # ——這些token不適合登記進ENGINE_CAPABILITY_ALIASES(該表要求別名描述能對應「原文常見
    # 措辭」, 但下列token要嘛是內部bookkeeping、要嘛是單一戰法專屬且窄用的機制名稱, 沒有
    # 「戰法撰寫者會誤以為引擎不支援」的自然語言對應描述):
    "_id",  # ctrlReflect去重旗標鍵組成用的內部id, 非資料欄位(engine.js第1163行e._id僅作為
    # flag key的組成部分, 非戰法JSON會設定的欄位), 誤觸_scan_engine_js_tokens的\be\.正則。
    # 批52j: 捕獲(暗箭難防獨立狀態) k=="capture"專屬, 見PER_KIND_FIELDS["capture"]已登記。
    # 單一戰法專屬窄機制, 無自然語言對照措辭會被誤稱「引擎不支援」。
    "capture", "altCoef",
    # 批52i: 代理普攻/代理直傷(垂心萬物專屬), 見PER_KIND_FIELDS["proxyNormal"/"proxyHit"]
    # 已登記。單一戰法專屬窄機制。
    "proxyNormal", "proxyHit", "srcSel", "checkSrcSel", "ifNoExtra", "ifHasExtra",
    # 批52d: 虎嗔(將門虎女專屬), 見PER_KIND_FIELDS["huchen"]已登記。單一戰法專屬窄機制。
    "huchen", "maxHits", "ampOnSettle", "ampMaxStack",
    # 批52g: 逐目標機率/具名狀態機率加成(五雷轟頂專屬)+施放者/目標武將名單過濾(太平道法
    # 專屬), 見KNOWN_EFFECT_FIELDS已登記。皆為單一戰法專屬窄機制。
    "ratePerTarget", "rateStatusBonus", "ifCasterNames", "whoNames",
    # 批52h: fireControlled()/fire_controlled()專屬的「速度慢於持有者才觸發」條件(機鑑先識
    # 專屬), 見KNOWN_EFFECT_FIELDS已登記。單一戰法專屬窄機制。
    "onlySlower",
    # 批52h: on:"controlled"(狀態施加反應式事件) —— 刻意不加入ENGINE_CAPABILITY_ALIASES:
    # 目前只支援「機鑑先識」這種窄用場景(持有者反彈同一種控制狀態給隨機敵軍), 並非engine_
    # limitations.md第25節第1類「狀態鏡射/廣播事件」描述的通用「任一單位監聽任一控制狀態
    # 施加」廣播基礎設施(該節仍誠實記錄此為未解決缺口, 見該節「需要新增第四種事件」段落)。
    # 若在此加上泛用別名(如「狀態鏡射」→controlled), 會讓R20誤判engine_limitations.md自身
    # 誠實記錄的「仍未解決」措辭為stale, 造成規則自相矛盾, 故僅登記進忽略清單(維持window
    # 已支援/尚未泛化的現況陳述, 不誤導成「已完全解決」)。
    "controlled",
    # 批52續系列: 主將/副將條件式數值覆寫+屬性縮放閘門, 皆為窄用的效果級旗標, 目前全庫
    # 使用案例稀少(各自1~3筆), 且與已登記的"主將時"(ifLeader布林閘門)概念相近但非同一
    # 欄位, 若強行registered成別名易與ifLeader的別名描述混淆(兩者都會命中"主將"字樣但
    # 修正方式不同: ifLeader是「是否套用」的二元閘門, 這些是「套用哪個數值/是否縮放」的
    # 覆寫式旗標), 暫列入忽略清單, 待未來若有戰法明確用這些欄位家族且需要R20保護時再評估
    # 個別登記別名。
    "coefLeader", "rateLeader", "rateSub", "ifGender", "ifSub",
    "scaleIfLeader", "scaleIfSub", "requireDmg",
    # 批52e: 效果級「同回合可多次觸發」旗標(文武雙全專屬場景), 窄用。
    "everyHit",
    # 批52g: dot具名狀態(供ifTargetHas按名稱匹配), 窄用欄位, 非「新能力」語意(dot本身早已
    # 在忽略清單, 具名只是既有dot機制的識別標籤延伸)。
    "dotName",
    # 批52: heal選標「優先兵力最低者」/「共用同一份治療池」, 見既有heal相關_note; 窄用欄位,
    # heal本身已在忽略清單, 這兩者是既有heal選標邏輯的延伸旗標, 非獨立新能力。
    "preferLowest", "sharedPool",
    # 批G: lifestealGiven(倒戈效果量加成, 對稱既有已忽略的healGiven, 見hit()倒戈結算處),
    # 長慮「攻心效果+30%」專屬新增, 與healGiven同性質(窄用的「XGiven」加成族), 比照
    # healBoost/healGiven既有忽略慣例列入, 不另立自然語言別名(避免與healGiven的既有別名
    # 描述混淆, 兩者字面都含「Given」但服務不同結算管線)。
    "lifestealGiven",
    # 批G: extraHit(衝陣專屬k標記字串, 非任何dispatch分支消費的實際k類型, 純供資料辨識,
    # 見equips_parsed.json「衝陣」_note——實際觸發邏輯由e.coef驅動, 走on_deal_eq的coef
    # 直傷派發, 非k派發), 單一裝備專屬窄機制, 無自然語言對照措辭。
    "extraHit",
    # 禁近似令-批L: dmgFromStatus(才辯機捷專屬, k=="amp"限定「只對6種具名dot狀態造成的傷害
    # 生效」跨戰法橫切範圍)/victimIsTgt(先登死士專屬, stealStat受害者精確鎖定反應式事件另一方)/
    # maxStackIfLeaderIs(先登死士專屬, coefLeader/rateLeader家族的maxStack維度變體)——三者皆為
    # 單一或少數戰法專屬窄用欄位(見PER_KIND_FIELDS/KNOWN_EFFECT_FIELDS已登記完整語意), 無
    # 「戰法撰寫者可能誤稱引擎不支援」的通用自然語言對照措辭, 不適合登記進
    # ENGINE_CAPABILITY_ALIASES(該表要求別名描述能對應「原文常見措辭」), 比照上方lifestealGiven/
    # extraHit等同類窄用欄位慣例列入忽略清單。
    "dmgFromStatus", "victimIsTgt", "maxStackIfLeaderIs",
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


def _counter_has_damage(p):
    # 批H0(R25補遺): counter(反擊)效果自帶獨立 coef, 承載「受到普通攻擊時對攻擊者造成傷害」
    # 這類反應式傷害宣告(如還擊/千里走單騎/古之惡來等 8 筆全庫既有案例, top coef=0 為設計常態
    # ——傷害走 counter 段而非戰法主段), 與 dot/extraHits/choices 同屬「傷害由其他承載管道
    # 表達, 非零輸出」的合法設計, 應計入白名單, 否則任何原文用「對攻擊者造成傷害」措辭且無
    # 其他傷害段的 counter-only 戰法都會被 R25 誤判(還擊即實例)。
    return any(e.get("k") == "counter" and (e.get("coef") or 0) > 0 for e in (p.get("effects") or []))


def check_r25(p, txt):
    violations = []
    if (p.get("coef") or 0) > 0:
        return violations
    if _extra_hits_has_damage(p) or _choices_has_damage(p) or _dot_has_damage(p) or _counter_has_damage(p):
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


# ---------------------------------------------------------------------------
# R26(批39 B): 「若/由 XX統領」揭露一致性防線 —— 原文含「若/由 <特定武將>統領」的條件式加成
# 措辭(西涼鐵騎/虎豹騎/大戟士/無當飛軍/虎衛軍/陷陣營/錦帆軍等12筆既有precedent, 全庫核對
# 後其中4筆丹陽兵/白毦兵/白馬義從/先登死士完全漏揭露, 見批39任務背景), 但該戰法既無
# leaderBonus(唯一已知可精確建模此類條件的原語, 見虎豹騎曹純特例, k===\"chargeup\"專屬硬編碼
# 力²公式)、也沒有任何提到「統領」字面的揭露文字(_todo/_note/_note2/_note_self/_approx,
# 戰法級或效果級皆可, 見 _topic_disclosed(effect=None) 全文掃描), 視為「條件加成完全被
# 沉默丟棄」的違規。
#
# 低誤報設計:
# - 只抓「若<1~8字任意武將名>統領」或「由<1~8字任意武將名>統領」句型(排除純泛稱如「若統領
#   為蠻族」這類陣營條件, 非特定武將——見象兵批39 A, 那是另一種完全不同的缺口(陣營計數),
#   不屬本規則管轄範圍, 用排除詞「陣營」「蠻族」「異族」「同族」濾掉泛稱條件, 只保留「統領」
#   前緊鄰疑似人名的具體武將條件)。
# - leaderBonus(戰法級或任一效果級)存在即視為已妥善建模(不論是否為「該筆」特定武將, 因
#   leaderBonus本身是為此類條件而生的專屬機制, 已使用即非沉默省略), 不算違規。
# - 揭露判定用 _topic_disclosed(p, R26_TOPIC_KW, effect=None)(戰法整體層級掃描, 而非批37
#   效果級精準化——「統領」條件通常修飾整個戰法的某個數值調整, 不對應單一固定的k類別, 故
#   採 R3/R5/R8/R16/R20 同慣例的戰法級全文掃描, 非R13/R25式效果級窄化)。
# - 版本區塊感知: 只在含「統領」措辭的版本區塊內核對(比照R22慣例只信任最新區塊, 避免舊版本
#   已被取代的統領條件描述誤判為當前版本缺口)。
# ---------------------------------------------------------------------------
R26_LEADER_COND_RE = re.compile(r"[若由][^。；;，,]{0,8}統領")
R26_FACTION_EXCLUDE_RE = re.compile(r"陣營|蠻族|異族|同族|漢族|善戰|統領為")
R26_TOPIC_KW = ("統領", "leaderBonus")


def check_r26(p, txt):
    violations = []
    if _has_leader_bonus(p):
        return violations
    for block in split_version_blocks(txt):
        for clause in split_clauses(block):
            m = R26_LEADER_COND_RE.search(clause)
            if not m or R26_FACTION_EXCLUDE_RE.search(clause):
                continue
            if _topic_disclosed(p, R26_TOPIC_KW):
                return violations
            violations.append({
                "name": p["nameZh"], "rule": "R26",
                "message": f"原文「{m.group(0)}」為特定武將統領條件式加成, 但戰法/效果皆無"
                           "leaderBonus(批26原語, 目前唯一已知可建模此類條件的機制)且無提及"
                           "「統領」的揭露文字, 條件加成完全被沉默省略",
                "evidence": clause.strip()[:120],
            })
            break
        if violations:
            break
    return violations


def _has_leader_bonus(p):
    if p.get("leaderBonus"):
        return True
    if any(e.get("leaderBonus") for e in (p.get("effects") or [])):
        return True
    # 禁近似令-批L: ifLeaderIs(批44)/maxStackIfLeaderIs(批L)同屬「若XX統領」條件式加成的
    # 已建模機制(對稱既有leaderBonus, 只是換了不同的套用維度——ifLeaderIs是條件閘門本身,
    # maxStackIfLeaderIs是maxStack維度的覆寫式讀取), 具備任一者即視為已妥善建模, 不算沉默
    # 省略。R26自批39建立時leaderBonus是唯一機制, 批44新增ifLeaderIs時未同步更新此函式(對8筆
    # TROOP家族實務上未現形, 因那批戰法的_note文字恰好也都提及「統領」二字, 靠_topic_disclosed
    # 文字路徑消音, 非靠本函式), 先登死士(本批maxStackIfLeaderIs)首次真正踩中此登記缺口
    # (對稱R20漂移偵測「新原語需同步更新既有規則的已解決判定」精神), 本次一併補齊ifLeaderIs
    # 承認, 徹底解決而非只消音本筆。
    return any(e.get("ifLeaderIs") or e.get("maxStackIfLeaderIs") for e in (p.get("effects") or []))


# ---------------------------------------------------------------------------
# R27(批41): 主將身份揭露一致性防線 —— 原文含「自身為主將時/若為主將/若自身為主將/
# 若我軍主將」的施放者身份條件式加成措辭(全庫40餘筆含此類措辭, 見批41任務背景), 但戰法
# 整體既無 ifLeader(批26原語, 效果級「施放者須為隊伍主將」布林閘門)、也無 leaderBonus
# (R26管轄的另一種「特定武將統領」條件, 語意不同但同樣可能承載主將相關條件)、且無任何
# 提及「主將/leader/ifLeader」字面的揭露文字(_todo/_note/_note2/_note_self/_approx,
# 戰法級全文掃描, 效果級無法窄化——原因見下方「掃描範圍」說明), 視為「主將身份條件式
# 加成被沉默省略」的違規。
#
# 掃描範圍(戰法級而非批37效果級窄化): 「自身為主將時」條件通常修飾整句效果描述中的某個
# 數值(如「基礎值提升至X」「機率提升至Y」「傷害率提升至Z」), 不對應單一固定k類別(可能是
# amp/mitig/stat/stack/dot任一種, 甚至像奉令平虜完全落在effects=[]的未建模範圍裡), 故比照
# R3/R5/R8/R16/R20/R26同慣例採 _topic_disclosed(p, keywords, effect=None) 戰法整體層級
# 掃描, 不做R13/R25式效果級精準化。
#
# 低誤報設計:
# - 修復方式不強制要求ifLeader欄位本身——若原文是「基礎值A, 主將時提升至B」這種對既有
#   機率/數值的縮放(非新增獨立效果段), ifLeader二元閘門無法精確表達(同engine_limitations.md
#   第9節「觸發機率的按施放者身份條件縮放」缺口, 見仁德載世R24precedent), 此時補一則提及
#   「主將」字面的_todo誠實揭露即可豁免, 不強制修欄位; 若原文是「基礎值A(無條件)+主將時
#   額外加成到B」這種可拆分成「基礎段+ifLeader top-up差額段」的複合效果(見水淹七軍批37 B
#   precedent: 基礎dot 0.96無條件+差額dot 0.12僅ifLeader), 則應比照該precedent用兩段
#   同k效果(基礎+top-up)精確建模。兩種修法本規則皆不強制擇一, 只要求「不可沉默省略」。
# - 排除「分擔效果無效」等否定句型(校勝帷幄「自身為主將時分擔效果無效」是主將時*關閉*
#   某效果, 非新增加成, 語意方向相反, 但實務上這類戰法通常仍會在其他地方留下「主將」
#   相關揭露, 由_topic_disclosed戰法級掃描天然涵蓋, 不需要額外正則排除——保留此註解供
#   未來若出現真正只靠這條排除才能通過的案例時參考)。
# - 版本區塊感知(split_version_blocks): 只在含匹配的版本區塊內核對, 避免跨版本誤配
#   (比照R22/R26慣例)。
# ---------------------------------------------------------------------------
R27_LEADER_COND_RE = re.compile(r"自身為主將時|若自身為主將|自身若為主將|若為主將|若我軍主將")
R27_TOPIC_KW = ("主將", "ifLeader", "leader", "leaderBonus")


def _has_if_leader(p):
    if p.get("ifLeader"):
        return True
    for e in p.get("effects") or []:
        if e.get("ifLeader"):
            return True
    for eh in p.get("extraHits") or []:
        if eh.get("ifLeader"):
            return True
    for ch in p.get("choices") or []:
        if ch.get("ifLeader"):
            return True
        for e in ch.get("effects") or []:
            if e.get("ifLeader"):
                return True
    return False


def check_r27(p, txt):
    violations = []
    if _has_if_leader(p) or _has_leader_bonus(p):
        return violations
    for block in split_version_blocks(txt):
        for clause in split_clauses(block):
            m = R27_LEADER_COND_RE.search(clause)
            if not m:
                continue
            if _topic_disclosed(p, R27_TOPIC_KW):
                return violations
            violations.append({
                "name": p["nameZh"], "rule": "R27",
                "message": f"原文「{m.group(0)}」為施放者主將身份條件式加成, 但戰法/效果皆無"
                           "ifLeader(批26原語)/leaderBonus, 且無提及「主將/leader」的揭露"
                           "文字, 主將身份加成完全被沉默省略",
                "evidence": clause.strip()[:120],
            })
            break
        if violations:
            break
    return violations


# ---------------------------------------------------------------------------
# R28(批41): 數值一致性抽查 —— 原文明確宣告「傷害率X%→Y%」「治療率X%→Y%」的滿級值Y, 與
# 對應 parsed coef(×100)比對, 偏差>5%且無揭露解釋(EV折算/機率折入/版本分支/取滿級值等)
# 視為違規(見批41任務背景: 錦帆軍coef方向搞反是本規則的直接觸發案例)。
#
# 設計取捨(寧缺勿濫優先於覆蓋率, 見任務指示"假陽性寧缺勿濫,參考R25方法論"):
# - 本規則對「一個宣告↔一個coef欄位」的精確配對要求極高信心才觸發: 只在同一版本區塊內
#   「傷害率宣告數」與「戰法內dmg類coef欄位數」相等(=1:1可嚴格排序配對)時才比較, 或
#   「治療率宣告數」與「heal類coef欄位數」相等時才比較。多段宣告(如百步穿楊兩個傷害率
#   子句對應兩個不同coef欄位)因為「宣告在文字裡的順序」與「coef欄位在JSON裡的收錄順序」
#   未必一致(順序配對曾在此規則的前期試跑中產生大量假陽性, 见battle41 scratchpad試跑
#   記錄), 沒有可靠的通用配對演算法, 保守跳過(不判定, 比照R1/R13"窄化不到單一效果時
#   退回不判定"的既有慣例, 只是本規則退回的是「不觸發」而非「戰法級掃描」)。
# - 每個戰法只在「唯一dmg段」或「唯一heal段」的簡單情形下比較, 大幅降低誤配風險。
# - 揭露豁免用兩層判定: (1) 主題關鍵字(EV/折算/期望/近似/取滿級值/版本/分支/機率折入/
#   輪替/EV折算), 或 (2) 揭露文字內逐字出現該宣告的百分比字串(如"222%"或"128%→256%"的
#   任一段數字), 因為維護者慣例常直接把原文百分比抄進_note裡說明折算來源(如"折算0.535×
#   1.0≈0.54(原coef 1.0=每回合必觸發, 高估約1.9×)"), 逐字比對比關鍵字更精確不會誤判。
#   兩者任一命中即豁免(戰法級 _topic_disclosed 全文掃描, 比照R20/R26慣例)。
# - 版本區塊感知(split_version_blocks): 只用「含宣告」的第一個版本區塊(比照R2/R22慣例),
#   避免跨版本誤配。
# ---------------------------------------------------------------------------
R28_DMG_RE = re.compile(r"傷害率\s*(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)?\s*(\d+(?:\.\d+)?)?\s*%")
R28_HEAL_RE = re.compile(r"治療率\s*(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)?\s*(\d+(?:\.\d+)?)?\s*%")
R28_TOPIC_KW = ("EV", "折算", "期望", "近似", "取滿級值", "版本", "分支", "機率折入", "輪替",
                "折半", "高估", "低估")
R28_TOLERANCE = 0.05


def _r28_dmg_coefs(p):
    """收集戰法內「傷害類」coef欄位(非heal), 回傳list of (label, value)。只收頂層+
    effects(排除k==heal)+extraHits+choices(含choices.effects, 排除k==heal), 與R25/R13
    既有「傷害承載段」認定範圍一致(見_extra_hits_has_damage/_choices_has_damage)。"""
    out = []
    if p.get("coef"):
        out.append(("top.coef", p["coef"]))
    for e in p.get("effects") or []:
        if e.get("k") != "heal" and e.get("coef"):
            out.append((f'effects.{e.get("k")}.coef', e["coef"]))
    for eh in p.get("extraHits") or []:
        if eh.get("coef"):
            out.append(("extraHits.coef", eh["coef"]))
    for ch in p.get("choices") or []:
        if ch.get("coef"):
            out.append(("choices.coef", ch["coef"]))
        for e in ch.get("effects") or []:
            if e.get("k") != "heal" and e.get("coef"):
                out.append((f'choices.effects.{e.get("k")}.coef', e["coef"]))
    return out


def _r28_heal_coefs(p):
    """收集戰法內 k==heal 的 coef 欄位, 回傳 list of (label, value)。"""
    out = []
    for e in p.get("effects") or []:
        if e.get("k") == "heal" and e.get("coef") is not None:
            out.append(("effects.heal.coef", e["coef"]))
    for ch in p.get("choices") or []:
        for e in ch.get("effects") or []:
            if e.get("k") == "heal" and e.get("coef") is not None:
                out.append(("choices.effects.heal.coef", e["coef"]))
    return out


def _r28_topic_disclosed(p, pct_strs):
    """R28專屬揭露判定: 主題關鍵字 或 宣告的百分比字串逐字出現在揭露文字中, 任一命中即豁免。"""
    texts = _disclosure_texts(p)
    for txt in texts:
        if any(kw in txt for kw in R28_TOPIC_KW):
            return True
        if any(pct in txt for pct in pct_strs if pct):
            return True
    return False


def check_r28(p, txt):
    violations = []
    dmg_coefs = _r28_dmg_coefs(p)
    heal_coefs = _r28_heal_coefs(p)
    if not dmg_coefs and not heal_coefs:
        return violations
    for block in split_version_blocks(txt):
        dmg_matches = list(R28_DMG_RE.finditer(block))
        heal_matches = list(R28_HEAL_RE.finditer(block))
        if not dmg_matches and not heal_matches:
            continue
        # 傷害率: 只在「宣告恰好1筆」且「dmg類coef欄位恰好1筆」時比較(見上方設計取捨)
        if len(dmg_matches) == 1 and len(dmg_coefs) == 1:
            m = dmg_matches[0]
            hi = m.group(2) or m.group(1)
            val = round(float(hi) / 100, 4)
            label, cval = dmg_coefs[0]
            if abs(cval - val) > R28_TOLERANCE * max(val, 0.01):
                pct_strs = [m.group(1) + "%", (m.group(2) or "") + "%", m.group(0)]
                if not _r28_topic_disclosed(p, pct_strs):
                    violations.append({
                        "name": p["nameZh"], "rule": "R28",
                        "message": f"原文宣告「{m.group(0)}」滿級值{val*100:.1f}%, 但{label}="
                                   f"{cval}(偏差{abs(cval - val)*100:.1f}個百分點), 且無揭露"
                                   "解釋(EV折算/版本分支等), 疑似數值誤植或敘事方向錯誤",
                        "evidence": block.strip()[:150],
                    })
        # 治療率: 同理只在雙方各恰好1筆時比較
        if len(heal_matches) == 1 and len(heal_coefs) == 1:
            m = heal_matches[0]
            hi = m.group(2) or m.group(1)
            val = round(float(hi) / 100, 4)
            label, cval = heal_coefs[0]
            if abs(cval - val) > R28_TOLERANCE * max(val, 0.01):
                pct_strs = [m.group(1) + "%", (m.group(2) or "") + "%", m.group(0)]
                if not _r28_topic_disclosed(p, pct_strs):
                    violations.append({
                        "name": p["nameZh"], "rule": "R28",
                        "message": f"原文宣告「{m.group(0)}」滿級值{val*100:.1f}%, 但{label}="
                                   f"{cval}(偏差{abs(cval - val)*100:.1f}個百分點), 且無揭露"
                                   "解釋(EV折算/版本分支等), 疑似數值誤植或敘事方向錯誤",
                        "evidence": block.strip()[:150],
                    })
        break  # 只用第一個含宣告的版本區塊(同R2/R22慣例)
    return violations


# ---------------------------------------------------------------------------
# R29(批45 A): 群體目標沿用缺失 —— 原文「對敵軍群體(N人)造成傷害...並/并...其XX」這類句型,
# 「其」回指前段主coef段命中的同一批敵軍群體, 但 coef段(pick_targets)與效果段(who="enemy"+
# e.n, 各自獨立呼叫 pick_targets)實際上各自隨機選標, 3人隊只有1/3機率同組(見
# engine_limitations.md 對應節, 批45 A 新增 e["sameTargets"] 原語解決此缺口)。
#
# 低誤報設計(寧缺勿濫, 同R25/R28既有方法論):
# - 只抓「並/并」+「其」在同一子句內回指的明確句型(R29_BACK_REF_RE), 且該子句同時緊鄰
#   「群體」/「N人」描述(避免誤傷「並使我軍全體」这类非回指語意, 或"並對其發動"這種"其"
#   指涉更早獨立宣告的主將/單體目標而非本效果群體的情形)。
# - 只在 coef>0(有實質傷害輸出, 排除purely-buff戰法)+ t.n>1(戰法頂層宣告群體, 非單體)時才
#   檢查(coef=0 的戰法如誘敵深入走另一條「sibling-effect互相沿用」路徑, engine 側已支援
#   main_hit_tgts 就地更新機制, 但此類「無main coef段可回指」的情形需要人工核對哪個效果是
#   「首個命中群體」的效果才能決定sameTargets該掛在哪一段, 非本規則的低誤報掃描範圍, 保守
#   跳過不自動判定)。
# - 效果須已有 e["n"](>1, 通常對齊 t["n"])且無 e["sameTargets"], 且 who=="enemy"(coef恆定
#   命中foes, 只有enemy方向的效果段才有「與coef是否同組」的問題; who=="ally"的己方群體
#   buff與coef命中的敵方目標本就是不同陣營, 不適用)。
# - CTRL_K(stun/silence/disarm/taunt/chaos)效果不掛此規則——它們沿用 t.n/t.nMax 的既有
#   fallback邏輯已是「與coef段人數一致」的口徑(雖然仍是獨立pick_targets, 但這是舊有更大範圍
#   的既有已知近似, 非本次新增原語處理範圍, 且CTRL_K有自己的獨立選標語意慣例, 不宜與
#   sameTargets混用改動既有大量戰法的行為)。
# - 揭露豁免: 主題關鍵字(sameTargets/同一批/同批目標/同組/精確命中)或提及「並降低其/並使其/
#   對其」等回指語意本身的說明, 任一命中即豁免(比照R13/R20主題綁定慣例)。
# ---------------------------------------------------------------------------
R29_BACK_REF_RE = re.compile(r"[並并].{0,6}其|對其(?:發動|造成)")
R29_TOPIC_KW = ("sameTargets", "同一批", "同批目標", "同組", "精確命中", "回指", "main_hit_tgts", "mainHitTgts")


def check_r29(p, txt):
    violations = []
    if not p.get("coef"):
        return violations
    tn = p.get("n")
    if not tn or tn < 2:
        return violations
    effects = p.get("effects", []) or []
    if not effects:
        return violations
    for clause in split_clauses(txt):
        if not R29_BACK_REF_RE.search(clause):
            continue
        if not (GROUP_TARGET_RE.search(clause) or GROUP_RANGE_RE.search(clause)):
            continue
        for e in effects:
            k = e.get("k")
            if k in ("stun", "silence", "disarm", "taunt", "chaos"):
                continue
            if e.get("who", "ally") != "enemy":
                continue
            if e.get("n") is None or e.get("n") < 2:
                continue
            if e.get("sameTargets"):
                continue
            if _topic_disclosed(p, R29_TOPIC_KW, effect=e):
                continue
            violations.append({
                "name": p["nameZh"], "rule": "R29",
                "message": f"原文「並/其」回指同一批敵軍群體(effects[k={k}]已有e.n={e.get('n')}對齊t.n={tn}), "
                           "但缺 e['sameTargets'], coef段與本效果段各自獨立pick_targets, 3人隊僅1/3機率同組",
                "evidence": clause.strip()[:150],
            })
        break  # 只用第一個命中回指句型的子句(避免同一戰法多個子句重複觸發同一效果)
    return violations


# ---------------------------------------------------------------------------
# R30(批45 B): 純dot誤留頂層coef(雙重傷害) —— 原文只描述單一「施加XX狀態,每回合持續造成
# 傷害」的持續傷害機制(無獨立的「造成一次...攻擊」即時傷害動詞), 但戰法頂層 coef 與
# effects 內某個 dot 效果的 coef 數值相同, 代表同一段傷害率百分比被前期 reparse 管線同時
# 誤填進「頂層coef(一次性攻擊)」與「dot.coef(持續傷害)」兩處, 造成傷害被算兩次(一次即時+
# 一次持續, 高估約2倍)。
#
# 低誤報設計(寧缺勿濫, 同R25/R28/R29既有方法論):
# - 只在「戰法內恰有一個 dot 效果」且「該 dot.coef 與頂層 t.coef 數值相同(誤差<1e-6)」時才
#   判定——多個dot效果或數值不同(即使系出同源但已分別調整, 如天降火雨coef=1.18/dot.coef=
#   0.66明顯不同)的情形一律不判定(保守, 避免誤傷真正兩段式機制的巧合同值案例)。
# - 排除「合法雙動詞結構」(放火/毒氣precedent): 原文若同時含「造成...攻擊」(一次性攻擊
#   宣告)與「每回合持續造成...傷害」(持續傷害宣告)兩個獨立動詞子句, 視為legitimate雙段,
#   不判定(R30_IMMEDIATE_ATTACK_RE)。
# - 揭露豁免: 提及「雙重計算」「雙算」「dot」「持續傷害」等主題關鍵字的揭露文字才算數
#   (比照R13/R20主題綁定慣例, 純粹複誦原文百分比不算揭露)。
# ---------------------------------------------------------------------------
R30_IMMEDIATE_ATTACK_RE = re.compile(r"造成(?:一次)?(?:兵刃|謀略)?攻擊")
R30_DOT_DESC_RE = re.compile(r"每回合持續造成")
R30_TOPIC_KW = ("雙重計算", "雙算", "coef歸零", "coef:0", "coef=0", "dot", "持續傷害", "誤填", "重複計算")


def check_r30(p, txt):
    violations = []
    tcoef = p.get("coef")
    if not tcoef:
        return violations
    dots = [e for e in (p.get("effects") or []) if e.get("k") == "dot" and e.get("coef") is not None]
    if len(dots) != 1:
        return violations
    dot = dots[0]
    if abs(dot["coef"] - tcoef) > 1e-6:
        return violations
    if not R30_DOT_DESC_RE.search(txt):
        return violations  # 原文須明確有「每回合持續造成」措辭才判定為dot機制本身
    if R30_IMMEDIATE_ATTACK_RE.search(txt):
        return violations  # 合法雙動詞結構(放火/毒氣precedent): 原文另有獨立的「造成...攻擊」一次性宣告
    if _topic_disclosed(p, R30_TOPIC_KW):
        return violations
    violations.append({
        "name": p["nameZh"], "rule": "R30",
        "message": f"原文僅單一「每回合持續造成傷害」描述(無獨立一次性攻擊動詞), 但頂層coef={tcoef}"
                   f"與dot.coef={dot['coef']}數值相同, 疑似同一傷害率被誤填進兩處造成雙重計算(高估約2倍)",
        "evidence": txt.strip()[:150],
    })
    return violations


# ---------------------------------------------------------------------------
# R31(批45 C): 「撤回/移除某欄位」揭露與資料矛盾 —— _note/_todo 聲稱「已撤回」/「已移除」/
# 「恢復不含X的版本」某個具體欄位(如 targetSel), 但該欄位實際上仍寫在效果/戰法資料裡, 資料
# 從未真正被刪除, 只有揭露文字單方面聲稱撤回動作已完成(見定謀貴決根因: 批21_note聲稱撤回
# targetSel:"mostDamaged", 但effects[0]的targetSel欄位從未真正被移除, 存活4批(21→44)未被
# 抓到)。
#
# 與既有 R14(數值矛盾)/R20(stale能力聲明)的區別: R14抓「聲稱coef=X但實際≠X」(數值不一致),
# R20抓「聲稱引擎不支援X但引擎已支援X」(能力聲明過期), 兩者都不是本規則要抓的「聲稱刪除了
# 某欄位但欄位其實還在」——這需要專門解析「撤回類」動詞+具體欄位名稱, 兩者詞表皆不涵蓋
# (R14的STALE_ASSERT_RE只認coef/dur/rate精確斷言句型, R20的NEGATION_KW是「不支援能力」語氣,
# 都不是「撤回動作」語氣), 是本次任務發現的第三種stale揭露形狀, 故獨立成新規則而非塞進既有
# 兩條規則的詞表。
#
# 低誤報設計: 只在 _note/_todo 文字明確含 R31_WITHDRAWAL_KW(撤回/移除/恢復不含.../已撤回)
# 且緊鄰(<=30字內)提到某個 KNOWN_EFFECT_FIELDS 已知欄位名稱時才觸發, 且該欄位名稱必須真的
# 是該效果/戰法目前實際持有的欄位鍵(field present)才算矛盾(欄位名稱只是被提及但未真的存在
# 於資料裡, 不算違規——那正是「揭露屬實, 已撤回」的正常情形)。
# ---------------------------------------------------------------------------
R31_WITHDRAWAL_KW = ("撤回", "已撤回", "移除此欄位", "恢復不含", "撤銷", "已移除該欄位")
R31_WITHDRAWAL_LOOKAHEAD = 30
# 「不再是撤回後的.../已非撤回狀態」是雙重否定(描述"現在不處於撤回狀態", 即欄位已正確補回),
# 與「已撤回(=欄位現在不該存在)」語意相反, 若不排除會對「先撤回過、後來又正確補上該欄位」
# 的正當敘述(如定謀貴決改用maxTroop後的_note)誤判。緊鄰(<=4字)撤回關鍵字前方出現這些詞時,
# 視為雙重否定, 跳過本次match。
R31_DOUBLE_NEGATION_KW = ("不再是", "不再", "非")
R31_DOUBLE_NEGATION_LOOKBACK = 4


def _r31_scan_dict_fields(p, d, scope):
    """在單一 dict(戰法頂層或某個效果)裡, 檢查其揭露文字是否聲稱撤回了某個「該dict目前
    實際仍持有」的已知欄位鍵, 回傳violation list。"""
    violations = []
    texts = []
    for k in TEXT_DISC_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            texts.append(v)
    reported_fields = set()  # 同一dict同一欄位只報一次(避免"撤回"/"已撤回"等多個關鍵字重複命中洗違規數)
    for text in texts:
        for kw in R31_WITHDRAWAL_KW:
            idx = text.find(kw)
            if idx == -1:
                continue
            lookback = text[max(0, idx - R31_DOUBLE_NEGATION_LOOKBACK):idx]
            if any(neg in lookback for neg in R31_DOUBLE_NEGATION_KW):
                continue  # 雙重否定("不再是撤回..."/"非撤回..."), 描述的是"現在已不處於撤回狀態", 非stale斷言
            window = text[idx:idx + len(kw) + R31_WITHDRAWAL_LOOKAHEAD]
            for field in KNOWN_EFFECT_FIELDS:
                if field in ("k", "who", "n", "nMax", "dur", "rate"):
                    continue  # 太通用的欄位名稱容易在窗口內巧合出現, 不具辨識力, 排除
                if field in reported_fields:
                    continue
                if field in window and field in d:
                    reported_fields.add(field)
                    violations.append({
                        "name": p["nameZh"], "rule": "R31",
                        "message": f"{scope} 揭露文字聲稱「{kw}」了欄位 {field!r}, 但該欄位實際上仍存在"
                                   f"於資料中(值={d.get(field)!r}), 撤回動作可能只改了揭露文字未真正"
                                   "刪除資料欄位本身(stale揭露, 見engine_limitations.md第45節C項)",
                        "evidence": window.strip(),
                    })
    return violations


def check_r31(p, txt):
    violations = []
    violations.extend(_r31_scan_dict_fields(p, p, "頂層"))
    for i, e in enumerate(p.get("effects", []) or []):
        violations.extend(_r31_scan_dict_fields(p, e, f"effects[{i}](k={e.get('k')})"))
    return violations


# ---------------------------------------------------------------------------
# R32(批D): 頂層戰法欄位孤兒偵測 —— 對稱 R9(效果級, 依 e["k"] 分類白名單), 但檢查戰法
# 「頂層」欄位(effects[]陣列以外的鍵, 如 lockTarget/targetSel/cd/rateLeader/rateScale/
# hitsRepeat/ammo等)。這類欄位過去完全無規則巡檢——R9 只逐一走訪 p.get("effects", [])
# 內的物件, 從不檢查 p 自身的頂層鍵。
#
# 病根: 引擎依「type + 頂層 when.on」把戰法分派到不同函式各自處理(fight()主迴圈的
# active/command/passive一般路徑 / do_normal_attack()的charge突擊分支 / on_hit_for()、
# dealt_damage_for()、healed_for()、active_fired_for()、fire_controlled() 等反應式事件
# 函式), 每個函式各自只讀取自己認得的頂層欄位子集。某戰法宣告的頂層欄位若剛好落在「引擎
# 實際會分派到的那個函式不讀取」的名單外, 就是100%讀不到的孤兒(批D實測案例: 虎痴
# type==passive+coef==0 連fire擲骰條件都不成立, 其 lockTarget:true 從未被讀取; 陷陣突襲
# when.on=="activeFired" 走 active_fired_for(), 該函式的coef傷害段固定 pick_targets(),
# 完全不讀 lockTarget; 修復前的摧鋒斷刃 type==charge, do_normal_attack()的charge分支過去
# 只对已選定的單一目標打一次, 完全不讀 n/nMax/hitsRepeat, 「發動三次隨機打擊」被靜默塌縮
# 成一次——本批已修復引擎本身補上讀取, 見 sgz.py/engine.js do_normal_attack()/
# doNormalAttack() 同批註解)。詳見 engine_limitations.md 對應節。
#
# 下列白名單逐一核對 sgz.py/engine.js 原始碼行為得出(非猜測/非直接沿用資料本身現狀),
# 核對方法見批D任務: 讀 apply_effects()/applyEffects() 完整分派段 + fight() 主迴圈 +
# do_normal_attack()/doNormalAttack() + on_hit_for()/dealt_damage_for()/healed_for()/
# active_fired_for()/fire_controlled() 六個涉及頂層欄位讀取的函式全文, 逐一確認每個頂層
# 欄位的讀取條件式。
# ---------------------------------------------------------------------------

def _r32_dispatch_shape(p):
    """回傳本戰法的「頂層欄位讀取路徑」形狀字串, 供比對 PER_SHAPE_TOP_FIELDS。
    reactive(頂層 when.on 存在): fight()主迴圈與do_normal_attack()明確排除這類戰法
    (見 _when_ok()/_whenOk() 對 when.on 的排除式, 以及 command/passive 分支條件式的
    同款排除, sgz.py fight() 對應段), 改由 on 值對應的事件函式讀取, 形狀 = "reactive:<on>"。
    charge/active/command/passive: 直接對應 type 值。
    其餘(如 type=="none", 內政/幽靈條目不參與戰鬥): 回傳 None, 呼叫端整戰法跳過
    (比照既有 R1-R31 對 type=="none" 的一致處理)。"""
    when = p.get("when") or {}
    if when.get("on"):
        return f"reactive:{when['on']}"
    t = p.get("type")
    if t in ("active", "command", "passive", "charge"):
        return t
    return None


# 全域安全欄位: 與 dispatch shape 完全無關, 任何 type/when.on 組合皆讀得到。
# "_"開頭的揭露欄位(_todo/_note/_est/_evidence/_bucket/_history/_diff等任意寫法)在
# check_r32() 內用 field.startswith("_") 統一放行, 不需要在此逐一列舉。
R32_UNIVERSAL_TOP_FIELDS = {
    "nameZh", "type", "kind", "coef", "rate", "n", "nMax", "prep", "effects",
    "quality", "cat", "src", "note", "name",
    # troopLimit(批E新增, reparse_effects.py 從 data/tactics.json 原樣複製): 團隊構築約束
    # (該戰法可被哪些兵種傳承/裝載), 非戰鬥結算約束——已裝載戰法的戰鬥數學不受兵種影響,
    # 由 docs/matchmaker.js 的 pickInheritTactics()/troopMismatch() 在「指派傳承戰法給隊伍」
    # 這一步驟讀取並過濾非法配置, 依設計不應也不需要被 sgz.py/engine.js 任何 dispatch shape
    # 讀取(同 quality/cat 一樣是「戰鬥外」的metadata, 供上層UI/配將器使用), 故與它們同列
    # universal 安全清單, 不算孤兒欄位。
    "troopLimit",
    "extraHits",  # fight()主迴圈(active/command/passive)/do_normal_attack()(charge)/
    # on_hit_for()/dealt_damage_for()/active_fired_for() 皆讀取 t.get("extraHits") 呼叫
    # fire_extra_hits(), 六個分派路徑一致支援, 全域安全。
    "choices",    # 同上, 六個分派路徑皆用 dict(t0, **pick_choice(t0["choices"])) 合成視圖
    # 的一致慣例(fight()主迴圈/on_hit_for()/dealt_damage_for()皆有此段), 全域安全。
    "when",       # 本身就是決定 shape 的欄位; reactive shape 下 when 顯然被讀取(否則無從
    # 判斷shape); 非reactive shape 下 when.rounds/from/until/parity 由 round_ok()/roundOk()
    # 統一支援(fight()主迴圈active/command/passive分支皆呼叫 _when_ok()/_whenOk()), 安全。
    # teamGate: Unit.__init__ 建構時期一次性過濾(team_gate_ok()/teamGateOk()), 早於任何
    # dispatch shape 判斷發生, 全域安全。
    "teamGate",
    # everyN: do_normal_attack() 對 u.tactics 做「無條件全體掃描」(tick_every_n()/
    # tickEveryN()), 與「本戰法自己的 type/when.on 是否真的 fire」完全無關(即使該戰法
    # 本身從未透過 active/command/passive/charge 任何一種方式成功 fire 過, everyN 仍會被
    # 這個獨立的、每回合對 u.tactics 全體掃描的迴圈掃到), 全域安全。
    "everyN",
}

# 依 dispatch shape 分類的白名單(對稱 R9 的 PER_KIND_FIELDS, 但 key 換成 shape 字串而非 k)。
# active/command/passive 三者在 fight() 主迴圈共用同一段「if fire:」之後的程式碼(ammo/cd/
# rateLeader/rateScale等頂層欄位讀取邏輯完全共用, 見 sgz.py fight() 對應段), 故共用同一組
# _ACPA_SHARED。但 lockTarget/targetSel(頂層)/hitsRepeat/effectsPerHit 這4個只在
# t["coef"] 為真值時才會進入讀取它們的「if t['coef']:」程式碼區塊——coef==0 的戰法(如
# 虎痴)連這個區塊都進不去, 故額外用 _ACPA_COEF_GATED 分離出來, 由 check_r32() 依
# p.get("coef") 決定是否納入允許集合。
_ACPA_SHARED = {
    "cd", "rateLeader", "rateScale", "rateScaleDiv", "scaleDiv", "whenLeader",
    "ammo", "ammoReloadLeader", "sameSrcCoef", "rateScaleIfGender",
}
_ACPA_COEF_GATED = {"lockTarget", "targetSel", "hitsRepeat", "effectsPerHit"}
PER_SHAPE_TOP_FIELDS = {
    "active": _ACPA_SHARED | _ACPA_COEF_GATED,
    "command": _ACPA_SHARED | _ACPA_COEF_GATED,
    "passive": _ACPA_SHARED | _ACPA_COEF_GATED,
    # 批D(R32): do_normal_attack()/doNormalAttack() 的 charge 分支新增 n/nMax/hitsRepeat
    # 支援(cnt<=1 沿用原行為單體單次, 零回歸; hitsRepeat 時N次獨立選標可重複命中同一目標;
    # 否則 pick_targets 不重複群體/AoE), 見同批引擎註解。lockTarget/targetSel(頂層)/
    # effectsPerHit 對 charge 型目前仍不支援(do_normal_attack() 未實作, 全庫核對目前無
    # 戰法需要, 未來若有新戰法需求須另外擴充引擎+於此登記, 否則就是下一個孤兒)。
    "charge": {"hitsRepeat"},
    # 反應式各 on 值分派到不同事件函式, 各自實作進度不同(逐一讀原始碼核對, 非通用假設):
    # on_hit_for(): 讀 rateLeader(批C新增, 對稱active型既有頂層rateLeader分派)/
    # rateScale+rateScaleIfGender(批52既有, 魅惑)。
    "reactive:attacked": {"rateLeader", "rateScale", "rateScaleIfGender", "rateScaleDiv"},
    "reactive:damaged": {"rateLeader", "rateScale", "rateScaleIfGender", "rateScaleDiv"},
    # dealt_damage_for(): 讀 targetSel(批32 B新增, 監統震軍「對負傷最高之敵造成謀略傷害」)。
    # 未讀 rateLeader/rateScale(全庫核對目前無 on:"dealtDamage" 戰法需要, 若未來新增必須
    # 同步在此登記+補 dealt_damage_for()/dealtDamageFor() 讀取, 否則會是下一個孤兒)。
    "reactive:dealtDamage": {"targetSel"},
    # active_fired_for()/healed_for()/fire_controlled(): 逐一讀原始碼確認皆不讀本規則
    # 列管的任何頂層稀有欄位(active_fired_for()的coef傷害段固定用pick_targets(), 無
    # lockTarget/targetSel/rateLeader任何分支) —— 陷陣突襲的lockTarget死欄位即屬此類。
    # healed_for() 本身甚至只支援效果級 on_heal_effect_tacs, 不支援戰法級 on_heal_tacs
    # (已建但未讀, 見Unit.__init__/healed_for()註解), 若未來有戰法宣告頂層
    # when:{"on":"healed"} 會是比單一欄位孤兒更嚴重的「整戰法死亡」, 全庫核對目前無此案例
    # (見lint_tactics.py R32 selftest對此的陰性樣例覆蓋)。
    # 批I(禁近似令-scale/比較族): active_fired_for()/activeFiredFor() 新增讀取頂層
    # t["scaleCompare"](僅本函式的主coef傷害段, 神機妙算「並基於雙方智力差額外提高」), 依
    # 施放者vs本次命中目標的屬性差值額外縮放coef, 見scale_compare_of()/scaleCompareOf()。
    "reactive:activeFired": {"scaleCompare"},
    "reactive:healed": set(),
    "reactive:controlled": set(),
}


def check_r32(p, txt):
    violations = []
    shape = _r32_dispatch_shape(p)
    if shape is None:                          # type=="none"等非戰鬥形狀, 不參與戰鬥, 跳過(同既有R1-R31慣例)
        return violations
    allowed = R32_UNIVERSAL_TOP_FIELDS | PER_SHAPE_TOP_FIELDS.get(shape, set())
    coef_note = ""
    if shape in ("active", "command", "passive") and not p.get("coef"):
        allowed = allowed - _ACPA_COEF_GATED    # coef==0 時 coef段專屬的4個欄位讀不到(見上方PER_SHAPE_TOP_FIELDS說明)
        coef_note = ", coef=0(該shape的coef段專屬欄位讀不到)"
    for field, val in p.items():
        if field.startswith("_") or field in allowed:
            continue
        violations.append({
            "name": p["nameZh"], "rule": "R32",
            "message": f"頂層欄位 {field!r}={val!r} 在本戰法的 dispatch shape={shape}{coef_note} "
                       "下無任何引擎程式碼讀取(孤兒欄位), 見engine_limitations.md R32節",
            "evidence": f"type={p.get('type')} when={json.dumps(p.get('when'), ensure_ascii=False)} coef={p.get('coef')}",
        })
    return violations


# ---------------------------------------------------------------------------
# R33(批F): heal 效果選標必須與本文措辭一致 —— 「引擎不得有『預設補最殘』全域慣例」
# (user鐵律2)的機械化防線, 對稱 R13(其餘非CTRL效果種類的單體/N人/全體目標數檢查), 但 R13
# 明確排除 heal(見 R13_NONCTRL_KINDS 上方註解「heal 固定選『我方兵力最低者』」)——那條註解
# 描述的是批52之前的舊行為, 批52/批F已讓 heal 支援 who/e.n/targetSel/e.all, 不再有理由排除。
#
# 三個子檢查(各自獨立觸發, 同一效果可能同時違反多條):
#
# (a) 本文含「損失兵力最X/兵力最低/最低血量/負傷最高」等選標語意 + 緊鄰治療動詞(治療/恢復/
#     回復) → 該 heal 效果必須有 targetSel 屬於 HEAL_MOSTDAMAGED_TARGETSEL(mostDamaged/
#     minTroop, 兩者在 pick_by_criterion 都是選目前兵力最低者, 見 TARGETSEL_MIN 集合、
#     刮骨療毒用mostDamaged/垂心萬物用minTroop兩種既有寫法皆對應同一種選標行為)。
#
# (b) 本文含「隨機」緊鄰「單體/目標」(如「隨機單體」「目標隨機」, 見 HEAL_EXPLICIT_RANDOM_RE)
#     → 該 heal 效果不得落入「無 who/n/targetSel 明示」的隱含後備分支(該後備分支選最殘 1人,
#     方向與『隨機』相反)。用嚴格緊鄰的正則(而非泛見「隨機」二字)避免誤判——「隨機執行N次」
#     (勠力同心)/「隨機兵刃反擊」(揮兵謀勝, 「或」字並列另一效果非治療本身)/「隨機敵軍單體」
#     (百計多謀, 修飾敵方效果非治療)/「對隨機敵人」(眾志成城, 同左)都不該觸發, 這些「隨機」
#     修飾的是別的效果或次數, 不是heal的目標選擇, 全庫核對後只有「隨機單體」「目標隨機」兩種
#     緊鄰句型才是本規則要抓的本義(見批F分類D益其金鼓/解煩衛兩筆真實案例)。
#
# (c) 本文含「單體」(緊鄰治療動詞, 沿用 R13 的 window 定位手法) 但缺 who=self/leader/
#     eventTarget/targetSel 且 e.n 不是 1 → 目標數與本文「單體」不符(比照 R13 對其他效果種類
#     的既有邏輯, 只是 R13 本身不管 heal, 這裡另開一條 heal 專屬版本, 因為 heal 的「單體」
#     還需要额外排除 self/leader/eventTarget/targetSel 這些「已經是精確單體選標, 只是不透過
#     e.n=1 表達」的合法寫法, 不能直接套 R13 原始邏輯)。
#
# (d) 本文含「群體(N人)」/「(N~M人)」(緊鄰治療動詞) 但 e.n 不等於該數字(或 e.all 未設而本文
#     其實是「全體」且隊伍人數與該群體數相同的巧合情形, 保守不在此規則額外判定, 避免與(c)/
#     (a)重疊誤判——群體人數比對維持簡單直接: e.n 存在時比對是否相符, e.n 不存在時才報)。
#
# 低誤報設計(比照全庫既有慣例): 只在窗口內有明確錨點時才判定, 抓不到就不報; 任何主題綁定
# 揭露(_note/_todo等提及「目標」「單體」「群體」「最殘」「兵力最低」「隨機」等關鍵字, 沿用
# R13_TARGET_TOPIC_KW的既有慣例) 一律豁免——heal 效果級的既有 _note(如批F新增的逐筆說明)
# 天然滿足此豁免條件, 故本規則主要用於「捕捉批F之後任何新資料若重新踩進『預設補最殘』回歸」
# 的防線, 而非本次批F本身(本次修正已逐筆手寫_note, 自然豁免, 全庫應為0違規)。
# ---------------------------------------------------------------------------
HEAL_ACTION_KW = ("治療", "恢復", "回復")
HEAL_MOSTDAMAGED_KW_RE = re.compile(r"損失兵力最\S|兵力最低|最低血量|負傷最高|兵力損失最")
HEAL_MOSTDAMAGED_TARGETSEL = {"mostDamaged", "minTroop"}
# 嚴格緊鄰: 「隨機」直接接「單體/目標」, 或「目標」接「隨機」(見上方(b)子檢查docstring
# 三個負樣例的排除理由)。
HEAL_EXPLICIT_RANDOM_RE = re.compile(r"隨機(?:單體|目標)|目標\s*隨機")
# heal 專屬「單體」錨點: 緊鄰治療動詞(6字內, 比照R13 R13_ALL_ADJACENT_LOOKBACK同量級窗口),
# 排除「其中一種/三選一」等擇一分支句型(choices自身結構已表達, 不重複由R33判定)。
HEAL_SINGLE_ANCHOR_RE = re.compile(r"(?:治療|恢復|回復)[^。；;]{0,6}單體|單體[^。；;]{0,10}(?:治療|恢復|回復)")


def _r33_window_for_heal(txt):
    """在原文中找出含治療動詞的子句(沿用 split_clauses 版本區塊切分, 避免跨歷史版本誤配)。
    一個戰法可能有多個 heal 效果對應多個子句, 但目前全庫核對後同一戰法內若有多個heal效果,
    各自的選標語意通常已能個別對上明確的子句——保守起見, 回傳「所有含治療動詞的子句」讓
    呼叫端逐一嘗試比對(而非只取第一個), 找不到任何子句時回傳空list(不誤報)。"""
    out = []
    for clause in split_clauses(txt):
        if any(kw in clause for kw in HEAL_ACTION_KW):
            out.append(clause)
    return out


def check_r33(p, txt):
    violations = []
    if not txt:
        return violations
    effects = p.get("effects", []) or []
    heal_effects = [(i, e) for i, e in enumerate(effects) if e.get("k") == "heal"]
    if not heal_effects:
        return violations
    clauses = _r33_window_for_heal(txt)
    if not clauses:
        return violations
    for i, e in heal_effects:
        who = e.get("who", "ally")
        target_sel = e.get("targetSel")
        n = e.get("n")
        scope = f"effects[{i}](k=heal)"
        # 效果級揭露先豁免一次(主題綁定, 同R13慣例)——批F新增的_note逐筆說明會自然命中此豁免。
        if _topic_disclosed(p, R13_TARGET_TOPIC_KW, effect=e):
            continue
        for clause in clauses:
            # (a) 「損失最多/兵力最低/最低血量/負傷最高」→ 必須有 targetSel(mostDamaged/minTroop)
            if HEAL_MOSTDAMAGED_KW_RE.search(clause):
                if target_sel not in HEAL_MOSTDAMAGED_TARGETSEL and who not in ("self", "leader", "eventTarget"):
                    violations.append({
                        "name": p["nameZh"], "rule": "R33",
                        "message": f"{scope} 本文措辭「兵力最低/損失最多」但缺 targetSel(mostDamaged/minTroop), "
                                   f"現況 who={who!r} targetSel={target_sel!r}(引擎不應再有『預設補最殘』"
                                   "全域慣例, 須顯式聲明選標準則, 見engine_limitations.md R33節)",
                        "evidence": clause.strip(),
                    })
                    break  # 同一效果同一原因只報一次, 換下一個heal效果
            # (b) 「隨機單體/目標隨機」→ 不得落入隱含後備(無who/n/targetSel明示的min-troop分支)
            if HEAL_EXPLICIT_RANDOM_RE.search(clause):
                implicit_fallback = (
                    not target_sel and who == "ally" and n is None
                    and not e.get("all") and not e.get("sharedPool")
                )
                if implicit_fallback:
                    violations.append({
                        "name": p["nameZh"], "rule": "R33",
                        "message": f"{scope} 本文措辭明寫「目標隨機」但缺 e.n(隱含後備分支選最殘1人, "
                                   "與本文『隨機』方向相反, 見engine_limitations.md R33節)",
                        "evidence": clause.strip(),
                    })
                    break
            # (c) 「單體」(緊鄰治療動詞) → 需要 who=self/leader/eventTarget/targetSel 或 e.n==1,
            #     否則若 e.n 存在但不是1(如群體N人卻誤標成1), 或 e.n 缺失時(仍會落入隱含後備
            #     min-troop分支, 與批F後不應再有此慣例的原則衝突, 即使結果剛好是1人也要求
            #     顯式化), 皆屬違規——但已被(a)/(b)命中的效果不重複由(c)再報一次(避免同一句
            #     子多條規則對同一根因洗版), 故只在(a)/(b)關鍵字都不命中此子句時才檢查(c)。
            if (not HEAL_MOSTDAMAGED_KW_RE.search(clause) and not HEAL_EXPLICIT_RANDOM_RE.search(clause)
                    and HEAL_SINGLE_ANCHOR_RE.search(clause)):
                is_explicit_single = (
                    who in ("self", "leader", "eventTarget") or target_sel is not None or n == 1
                )
                if not is_explicit_single:
                    violations.append({
                        "name": p["nameZh"], "rule": "R33",
                        "message": f"{scope} 本文措辭「單體」但缺顯式選標(who=self/leader/eventTarget, "
                                   f"targetSel, 或 e.n=1), 現況 who={who!r} n={n!r} targetSel={target_sel!r}"
                                   "(不應依賴隱含min-troop後備分支表達單體語意, 見engine_limitations.md R33節)",
                        "evidence": clause.strip(),
                    })
                    break
            # (d) 「群體(N人)」/「(N~M人)」→ e.n 存在時須數字相符(e.n 缺失時不在此條額外重複
            #     報告, 因為「單體」錨點條件互斥、且「群體缺e.n」已由既有 R13 姊妹規則的heal
            #     排除範圍外情形——批F後 heal 亦適用同一數字核對, 故仍在此條檢查數字相符性,
            #     但「完全缺e.n」的情形已足夠由(c)以外的獨立分支涵蓋: 若本文是群體N人而缺
            #     e.n, 又不含「單體」關鍵字, (c)不會觸發, 需要(d)自己補上此檢查)。
            grp_range = GROUP_RANGE_RE.search(clause)
            grp = GROUP_TARGET_RE.search(clause)
            expect_n = None
            if grp_range:
                expect_n = int(grp_range.group(1))
            elif grp and grp.group(1):
                expect_n = int(grp.group(1))
            if expect_n is not None and expect_n > 1:
                if who in ("self", "leader", "eventTarget"):
                    continue  # 角色選標本身已是明確語意, 與「群體N人」的全體池語意無關(比照R13既有R13_ROLE_WHO排除)
                if n != expect_n and not e.get("all") and not e.get("sharedPool"):
                    violations.append({
                        "name": p["nameZh"], "rule": "R33",
                        "message": f"{scope} 本文措辭「群體({expect_n}人)」但 e.n={n!r} 不符"
                                   "(引擎不應依賴隱含min-troop後備分支表達群體語意, 見engine_limitations.md R33節)",
                        "evidence": clause.strip(),
                    })
                    break
    return violations


# ---------------------------------------------------------------------------
# R34(批H): 會心/奇謀措辭必須有真crit原語 —— 「禁近似令-批H」的機械化防線, 對稱既有
# R19(dmgType適用而未用)/R33(heal選標必須與本文一致)的「本文明確語意 vs parsed 欄位
# 不符」機械檢查手法, 但這裡抓的是「一整個機制家族(crit_system_primitive)是否已用真
# 原語(critUp/critDmgUp)取代EV折算」, 而非單一欄位缺失。
#
# 背景: no_approx_inventory.json crit_system_primitive族盤點時, 全庫14筆(10戰法+4裝備)
# 「會心/奇謀」相關戰法/裝備一律用crit-ev(機率×觸發幅度)EV折算成常駐amp值, 完全犧牲
# 觸發的二元性(方差)。批H新增k:"critUp"(機率, 真擲骰)/k:"critDmgUp"(觸發後傷害幅度
# 加成)取代之(見engine.js/sgz.py damage()對稱段落), 14筆逐一遷移完成, 另全庫掃描補上
# 3筆equips(長慮/逆鱗/王道)+3筆bingshu(將威/大謀不謀/出奇制勝殘留)+2筆bonds(五虎上將/
# 五子良將)共22筆。本規則是防未來新資料回歸「本文寫會心/奇謀, 卻仍用amp/mitig假裝」
# 的舊模式, 而非本次批H全庫掃描本身(本次掃描後全庫應為0違規)。
#
# 判定邏輯: 原文含「會心」或「奇謀」(游戲UI正式機制名稱, 排除純戰法/裝備自身名稱裡的
# 巧合字眼, 因為本規則吃的是 effectText/root data原文, 不是 nameZh, 「虛實奇謀」這類
# 純flavor名稱的效果本文若完全不含會心/奇謀字樣不會誤觸發, 已核對全庫此假陽性風險
# 極低——「奇謀」二字若出現在effectText內, 幾乎必定指涉暴擊機制本身, 非泛用中文詞彙,
# 與R3/R19等既有規則的關鍵字選字風格一致) + 該戰法/裝備的effects陣列裡完全沒有任何
# k in ("critUp","critDmgUp") 的效果 + 無主題相關揭露(_topic_disclosed, 見下方
# R34_TOPIC_KW, 涵蓋所有現存的合法停損案例: 百步穿楊的coef-fold結構性限制/西涼鐵騎與
# 錦帆軍等的ifLeaderIs+scale複合缺口/鋒芒畢露與大謀不謀等的無數字錨點估計值揭露) →
# 違規。
#
# 低誤報設計: 「會心」「奇謀」在effectText裡幾乎不會用於其他語意(對比R10的"目標"這種
# 泛用詞需要AMBIGUOUS_TOPIC_KW額外語境檢查, 這兩個詞在遊戲文本裡是專有機制名稱), 故
# 不需要窄化語境比對, 直接子字串搜尋即可(同R1/R3/R19對明確技術詞彙的既有慣例)。
# ---------------------------------------------------------------------------
R34_CRIT_KW_RE = re.compile(r"會心|奇謀")
R34_CRIT_KINDS = {"critUp", "critDmgUp"}
# 揭露關鍵字須指向「crit原語本身/EV折算/暴擊機制」相關的技術詞彙, 涵蓋現存全部合法
# 停損案例的實際用詞(見上方batch H各筆_note/_todo逐一核對): critUp/critDmgUp(原語
# 名稱本身)/crit_system_primitive(族名)/crit-ev(舊EV折算標記, 歷史文字提及)/折算/EV
# (泛用折算詞彙)/暴擊(中文別稱)/二元觸發/擲骰(真機率相關描述)。任一命中即視為已知
# 且已誠實揭露(同全庫_topic_disclosed既有慣例), 不要求逐字比對「critUp」三個字本身
# ——鋒芒畢露/大謀不謀等「數值缺乏原文佐證」案例的揭露文字雖已改用critUp但仍帶_est/
# _todo繼續揭露「數值本身」這個殘留問題, 同樣需要被本規則豁免(該殘留問題屬D類/待查證
# 範圍, 非R34要抓的「完全沒用crit原語」核心問題)。
R34_TOPIC_KW = ("critUp", "critDmgUp", "crit_system_primitive", "crit-ev", "折算", "EV",
                "暴擊", "二元觸發", "擲骰")


def check_r34(p, txt):
    violations = []
    if not R34_CRIT_KW_RE.search(txt):
        return violations
    effects = p.get("effects") or []
    if any(e.get("k") in R34_CRIT_KINDS for e in effects):
        return violations
    if _topic_disclosed(p, R34_TOPIC_KW):
        return violations
    m = R34_CRIT_KW_RE.search(txt)
    violations.append({
        "name": p["nameZh"], "rule": "R34",
        "message": f"原文含「{m.group(0)}」(會心=兵刃暴擊/奇謀=謀略暴擊, 批H新增k:\"critUp\""
                   "真機率原語), 但effects陣列無任何critUp/critDmgUp效果, 且無揭露"
                   "(crit_system_primitive族應已用critUp/critDmgUp取代EV折算, 見"
                   "engine_limitations.md本節)",
        "evidence": txt[:120].strip(),
    })
    return violations


# ---------------------------------------------------------------------------
# R35(批K, 禁近似令-收官永久防線): _approx 欄位永久禁止 —— 制度化「禁近似令」F~K全部戰果
# (全庫_approx從116清零至0), 任何未來新增的 _approx 欄位一律違規, 防止已清零的近似值透過
# 後續資料編輯(手動補資料/未來agy或grok外部查證誤植/reparse邏輯改動)悄悄回歸。
#
# 判定邏輯(兩層, 皆對稱既有規則的「低誤報優先」設計原則):
#   (1) 硬性(100%確定性): 對 parsed 戰法物件遞迴掃描任何層級(頂層/effects/extraHits/set/
#       choices及其巢狀effects)的 dict 是否含 "_approx" 鍵 → 違規, **無任何揭露可豁免**
#       (_approx 本身就是被禁止的標記, 不存在"已揭露的_approx"這種合法狀態, 與其餘規則的
#       「有_note/_todo等主題相關揭露即豁免」慣例不同, 這是刻意設計: R35 就是要讓 _approx
#       這個鍵完全消失, 不是要讓它"被解釋")。
#   (2) 軟性(散文包裝規避偵測): 只掃 **_todo** 欄位(不含_note)。全庫慣例: _note 是「歷史
#       敘述」欄位(記錄「批X做了什麼改動/曾經是什麼近似/為何如此決策」, 天生大量合法提及
#       「近似/EV折算」等字樣描述過去狀態, 全庫389筆戰法的_note若不分青紅皂白全文掃描,
#       誤報率高到淹沒真正的違規, 批K實測全文掃描_note會拉出200+筆歷史敘述假警報); _todo
#       則是「當下仍缺什麼」的前瞻欄位, 若_todo本身用「近似/EV折算」措辭描述現況, 才是真正
#       疑似「散文包裝規避硬性_approx檢查」的情形。_todo若含這類字樣, 且全文內找不到「未
#       建模/未實作/誠實維持/尚未/仍未/維持未/停損」等「已誠實揭露缺口」訊號詞(_todo本業
#       就該是揭露缺口, 這裡只是要求措辭清楚地說「沒做」而非「做了但是近似」)→ 違規。
# ---------------------------------------------------------------------------
R35_APPROX_KW_RE = re.compile(r"近似|EV折算|期望值折算|prob-ev|crit-ev|暫用")
R35_RESOLVED_SIGNAL_RE = re.compile(
    r"已(?:改用|解決|移除|精確|落地|建|接線|明確)|現已|取代|曾(?:是|用|近似|經)|"
    r"不再(?:是|近似|折算)|誠實(?:維持|揭露)|未建模|未實作|未建立|完全空白|尚未|仍未|維持未|停損|"
    r"無對應原語|未表達|未編碼|未知|需新增.{0,12}原語"
)


def _todo_texts(p):
    """收集戰法頂層 + 所有效果層級(effects/extraHits/choices及其巢狀effects)的「非空字串」
    _todo 文字(不含_note/_note2, 見上方 check_r35 對 _todo vs _note 語意分工的設計說明)。"""
    out = []
    v = p.get("_todo")
    if isinstance(v, str) and v.strip():
        out.append(v)
    for e in p.get("effects", []) or []:
        v = e.get("_todo")
        if isinstance(v, str) and v.strip():
            out.append(v)
    for eh in p.get("extraHits", []) or []:
        v = eh.get("_todo")
        if isinstance(v, str) and v.strip():
            out.append(v)
    for ch in p.get("choices", []) or []:
        v = ch.get("_todo")
        if isinstance(v, str) and v.strip():
            out.append(v)
        for e in ch.get("effects", []) or []:
            v = e.get("_todo")
            if isinstance(v, str) and v.strip():
                out.append(v)
    return out


def _find_approx_key(obj):
    """遞迴掃描: 任何層級的 dict 是否含 "_approx" 鍵(list 內每個元素也遞迴檢查)。"""
    if isinstance(obj, dict):
        if "_approx" in obj:
            return True
        return any(_find_approx_key(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_find_approx_key(v) for v in obj)
    return False


def check_r35(p, txt):
    violations = []
    if _find_approx_key(p):
        violations.append({
            "name": p["nameZh"], "rule": "R35",
            "message": "戰法(或其效果/extraHits/set/choices任一層級)含 _approx 欄位——"
                       "禁近似令批K收官後全庫_approx應為0, 任何_approx出現一律視為違規"
                       "(即使有_note/_todo等其他揭露亦不豁免, _approx本身就是被禁止的"
                       "近似值標記, 不存在'已揭露的_approx'這種合法狀態)。",
            "evidence": "found _approx key in parsed tactic",
        })
    seen_blobs = set()
    for txt_blob in _todo_texts(p):
        if txt_blob in seen_blobs:
            continue
        seen_blobs.add(txt_blob)
        m = R35_APPROX_KW_RE.search(txt_blob)
        if m and not R35_RESOLVED_SIGNAL_RE.search(txt_blob):
            violations.append({
                "name": p["nameZh"], "rule": "R35",
                "message": f"_todo揭露文字含「{m.group(0)}」但全文找不到「未建模/未實作/"
                           "誠實維持/尚未」等清楚的缺口揭露訊號詞, 疑似現行仍生效的近似值"
                           "以_todo散文包裝規避R35硬性_approx檢查(_todo應清楚說「沒做」"
                           "而非「做了但是近似」)。",
                "evidence": txt_blob[:150].strip(),
            })
    return violations


RULES = [
    ("R1", check_r1), ("R2", check_r2), ("R3", check_r3), ("R4", check_r4),
    ("R5", check_r5), ("R6", check_r6), ("R7", check_r7), ("R8", check_r8),
    ("R9", check_r9), ("R10", check_r10), ("R11", check_r11), ("R12", check_r12),
    ("R13", check_r13), ("R14", check_r14), ("R15", check_r15),
    ("R16", check_r16), ("R17", check_r17), ("R18", check_r18), ("R19", check_r19),
    ("R20", check_r20), ("R21", check_r21), ("R22", check_r22),
    ("R23", check_r23), ("R24", check_r24), ("R25", check_r25), ("R26", check_r26),
    ("R27", check_r27), ("R28", check_r28), ("R29", check_r29), ("R30", check_r30),
    ("R31", check_r31), ("R32", check_r32), ("R33", check_r33), ("R34", check_r34),
    ("R35", check_r35),
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
        ("鴆毒式回歸(批45 D修復前應漏抓, 修復後應抓到): 頂層_note同時討論兩個無關子議題,"
         "「武力」(kind_kw)與「目標」(keywords, 巧合出現在另一段settle→dot重構bug描述裡)"
         "湊巧同文字內各出現一次但語意不相關, 不應被視為\"明確指涉\"本stat效果的目標數主題",
         _base_tactic(coef=0, type="active", effects=[
             {"k": "stat", "who": "enemy", "stat": "force", "mult": 0.7, "dur": 1},
             {"k": "dot", "who": "enemy", "coef": 2.26, "dur": 1,
              "_note": "『對施放目標單體, 下回合造成傷害』——改用dot取代舊版settle, 舊版目標選取邏輯錯誤打統率最高敵將而非中毒目標本身"},
             {"k": "stat", "who": "enemy", "stat": "command", "add": -60, "dur": 99},
         ], _note="agy查證: (1)武力降低應為30%; (2)『毒發』改用dot取代舊版settle(結算目標邏輯錯誤打統率最高敵將而非中毒目標本身)"),
         "對敵軍單體施加鴆毒，使其武力降低30%，持續1回合；1回合後毒發，對目標造成謀略傷害（傷害率226%），並使其統率降低60點，可無限疊加", True),
        ("鴆毒式回歸(修復後三效果皆補e.n=1不應誤報)",
         _base_tactic(coef=0, type="active", effects=[
             {"k": "stat", "who": "enemy", "stat": "force", "mult": 0.7, "dur": 1, "n": 1},
             {"k": "dot", "who": "enemy", "coef": 2.26, "dur": 1, "n": 1,
              "_note": "『對施放目標單體, 下回合造成傷害』——改用dot取代舊版settle, 舊版目標選取邏輯錯誤打統率最高敵將而非中毒目標本身"},
             {"k": "stat", "who": "enemy", "stat": "command", "add": -60, "dur": 99, "n": 1},
         ], _note="agy查證: (1)武力降低應為30%; (2)『毒發』改用dot取代舊版settle(結算目標邏輯錯誤打統率最高敵將而非中毒目標本身)"),
         "對敵軍單體施加鴆毒，使其武力降低30%，持續1回合；1回合後毒發，對目標造成謀略傷害（傷害率226%），並使其統率降低60點，可無限疊加", False),
        ("圍師必闕式假陽性防線: 「群體(N人)」是觸發條件子句(敵軍需N人處於某狀態才觸發),"
         "非本效果(who=ally)的目標範圍描述, 有主題相關揭露澄清後不應誤判為e.n缺口",
         _base_tactic(coef=1.2, type="command", n=3, effects=[
             {"k": "mitig", "who": "ally", "val": 0.39, "dur": 2,
              "_note": "「群體(2人)」是觸發本效果的條件子句(敵軍需有2人處於特定狀態才觸發此mitig),"
                       "並非本效果(who=ally, 目標是我方)的目標範圍描述, e.n在此不適用"},
         ]),
         "當敵軍群體（2人）處於潰逃或叛逃狀態時壓制敵軍，使敵軍造成的謀略傷害降低19.5%→39%", False),
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
        ("否定詞隔了引號內無關名詞不應誤報(批29回歸測試: 挫銳假陽性案例; 批K更新: 「攔截"
         "傷害」已於批K落地為k:\"preDmgHook\"並登記別名, 原挫銳文字現屬「應被抓到」的合理"
         "stale情境, 改用結構相同但無關聯的虛構詞彙保留本測試原本驗證的「引號內夾雜無關"
         "名詞不誤判否定詞」語法穩健性意圖)",
         _base_tactic(coef=0, _approx="「造成傷害時65%機率完全無法造成傷害」引擎無「某未登記虛構原語」原語，以amp近似"),
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
    "R26": [
        ("若XX統領條件但無leaderBonus且無揭露應抓到",
         _base_tactic(effects=[{"k": "mitig", "who": "ally", "val": 0.18, "dur": 99}]),
         "我軍全體受到謀略傷害降低18%→36%；若陶謙統領，則抵擋比例提升至20%→40%", True),
        ("有leaderBonus不應誤報(比照虎豹騎)",
         _base_tactic(effects=[{"k": "chargeup", "who": "self", "val": 0.05, "dur": 99,
                                 "leaderBonus": {"general": "曹純", "k": 0.032}}]),
         "若曹純統領，突擊發動機率額外受武力影響", False),
        ("有揭露統領缺口不應誤報",
         _base_tactic(effects=[{"k": "mitig", "who": "ally", "val": 0.18, "dur": 99}],
                      _todo="若陶謙統領的加成條件未建模, 無ifLeaderIsGeneral通用機制"),
         "我軍全體受到謀略傷害降低18%→36%；若陶謙統領，則抵擋比例提升至20%→40%", False),
        ("陣營泛稱條件(非特定武將)不應誤報",
         _base_tactic(effects=[{"k": "mitig", "who": "ally", "val": 0.1, "dur": 99}]),
         "若統領為蠻族，部隊每多1名蠻族武將結算的傷害額外降低5%→10%", False),
    ],
    "R27": [
        ("鷹視狼顧回歸(修復前無ifLeader應抓到)",
         _base_tactic(type="command", coef=1.54, rate=0.8,
                      effects=[{"k": "amp", "who": "self", "val": 0.16, "dur": 99, "_approx": "crit-ev"}]),
         "第5回合起，每回合對1→2個敵軍單體造成謀略傷害；自身為主將時，獲得8%→16%奇謀機率", True),
        ("鷹視狼顧回歸(修復後補ifLeader不應誤報)",
         _base_tactic(type="command", coef=1.54, rate=0.8,
                      effects=[{"k": "amp", "who": "self", "val": 0.16, "dur": 99, "ifLeader": True}]),
         "第5回合起，每回合對1→2個敵軍單體造成謀略傷害；自身為主將時，獲得8%→16%奇謀機率", False),
        ("有揭露(仁德載世式機率縮放停損)不應誤報",
         _base_tactic(coef=0, effects=[{"k": "mitig", "who": "enemy", "val": 0.1, "dur": 1,
                                        "_todo": "自身為主將時機率縮放, ifLeader無法表達複合語意, 暫不建模"}]),
         "自身為主將時，施加虛弱狀態的機率提高至12.5%→25%", False),
        ("有leaderBonus不應誤報",
         _base_tactic(effects=[{"k": "chargeup", "who": "self", "val": 0.05, "dur": 99,
                                 "leaderBonus": {"general": "曹純", "k": 0.032}}]),
         "自身為主將時，額外提升5%突擊發動機率", False),
    ],
    "R28": [
        ("錦帆軍回歸(修復前coef方向搞反應抓到): 宣告64%但coef=1.28(2倍)",
         _base_tactic(type="command", coef=1.28, rate=0.45),
         "部隊普通攻擊時，有45%機率使目標進入潰逃狀態（傷害率32%→64%，受武力影響），持續2回合", True),
        ("錦帆軍回歸(修復後coef=0.64與宣告一致不應誤報)",
         _base_tactic(type="command", coef=0.64, rate=0.45),
         "部隊普通攻擊時，有45%機率使目標進入潰逃狀態（傷害率32%→64%，受武力影響），持續2回合", False),
        ("治療率宣告與heal coef不符應抓到",
         _base_tactic(coef=0, effects=[{"k": "heal", "who": "ally", "coef": 0.5, "dur": 1}]),
         "治療我軍單體（治療率100%，受智力影響）", True),
        ("有EV折算揭露(關鍵字)不應誤報",
         _base_tactic(coef=0, effects=[{"k": "heal", "who": "ally", "coef": 0.5, "dur": 1,
                                        "_note": "50%機率×治療率100%期望折算=0.5"}]),
         "治療我軍單體（治療率100%，受智力影響）", False),
        ("多段宣告(N!=1)保守不判定不應誤報(避免多段誤配, 見任務背景方法論)",
         _base_tactic(type="command", coef=0.64, rate=0.45,
                      extraHits=[{"coef": 1.1, "kind": "phys", "who": "sameTarget", "rate": 0.6}]),
         "部隊普通攻擊時，使目標進入潰逃狀態（傷害率32%→64%）；若目標已潰逃則造成兵刃攻擊（傷害率55%→110%）", False),
    ],
    "R29": [
        ("聲東擊西式回歸(修復前缺sameTargets應抓到): coef群體傷害+並降低其速度",
         _base_tactic(type="active", coef=1.75, rate=0.4, n=2,
                      effects=[{"k": "stat", "who": "enemy", "stat": "speed", "add": -30, "dur": 2, "n": 2}]),
         "對敵軍群體（2人）造成謀略攻擊（傷害率87.5%→175%）并降低其15→30點速度，持續2回合", True),
        ("聲東擊西式回歸(修復後補sameTargets不應誤報)",
         _base_tactic(type="active", coef=1.75, rate=0.4, n=2,
                      effects=[{"k": "stat", "who": "enemy", "stat": "speed", "add": -30, "dur": 2, "n": 2,
                                "sameTargets": True}]),
         "對敵軍群體（2人）造成謀略攻擊（傷害率87.5%→175%）并降低其15→30點速度，持續2回合", False),
        ("單體(n=1)不應誤報(regime不適用群體目標沿用)",
         _base_tactic(type="active", coef=1.0, rate=0.4, n=1,
                      effects=[{"k": "stat", "who": "enemy", "stat": "speed", "add": -30, "dur": 2}]),
         "對敵軍單體造成謀略攻擊（傷害率100%）并降低其速度，持續2回合", False),
        ("who=ally不應誤報(coef恆命中敵方, 己方群體buff與此規則無關)",
         _base_tactic(type="active", coef=1.0, rate=0.4, n=2,
                      effects=[{"k": "amp", "who": "ally", "val": 0.1, "dur": 2, "n": 2}]),
         "對敵軍群體（2人）造成謀略攻擊（傷害率100%）并使我軍全體提高傷害，持續2回合", False),
        ("stun(CTRL_K)不應誤報(既有t.n/t.nMax fallback慣例, 非本規則管轄)",
         _base_tactic(type="active", coef=1.0, rate=0.4, n=2,
                      effects=[{"k": "stun", "who": "enemy", "dur": 1, "n": 2}]),
         "對敵軍群體（2人）造成謀略攻擊（傷害率100%）并使其陷入震懾，持續1回合", False),
        ("有主題揭露(提及sameTargets)不應誤報",
         _base_tactic(type="active", coef=1.75, rate=0.4, n=2,
                      effects=[{"k": "stat", "who": "enemy", "stat": "speed", "add": -30, "dur": 2, "n": 2,
                                "_todo": "sameTargets評估中, 暫維持獨立選標近似"}]),
         "對敵軍群體（2人）造成謀略攻擊（傷害率87.5%→175%）并降低其15→30點速度，持續2回合", False),
    ],
    "R30": [
        ("決水潰城式回歸(修復前coef與dot.coef相同應抓到): 純dot誤留頂層coef雙重計算",
         _base_tactic(type="active", coef=1.12, rate=0.45, n=2,
                      effects=[{"k": "dot", "who": "enemy", "coef": 1.12, "dur": 2, "n": 2}]),
         "準備1回合，對敵軍群體（2人）施加水攻狀態，每回合持續造成傷害（傷害率56%→112%），持續2回合", True),
        ("決水潰城式回歸(修復後coef歸零不應誤報)",
         _base_tactic(type="active", coef=0, rate=0.45, n=2,
                      effects=[{"k": "dot", "who": "enemy", "coef": 1.12, "dur": 2, "n": 2}]),
         "準備1回合，對敵軍群體（2人）施加水攻狀態，每回合持續造成傷害（傷害率56%→112%），持續2回合", False),
        ("放火/毒氣式合法雙動詞結構不應誤報(有獨立「造成...攻擊」一次性宣告)",
         _base_tactic(type="active", coef=1.0, rate=0.5, n=1,
                      effects=[{"k": "dot", "who": "enemy", "coef": 1.0, "dur": 1}]),
         "對敵軍單體造成謀略攻擊（傷害率100%），並使其陷入中毒狀態，每回合持續造成傷害（傷害率100%），持續1回合", False),
        ("coef與dot.coef不同不應誤報(非雙重計算, 兩段各自獨立數值)",
         _base_tactic(type="active", coef=1.18, rate=0.5, n=2,
                      effects=[{"k": "dot", "who": "enemy", "coef": 0.66, "dur": 1, "n": 2}]),
         "準備1回合，對敵軍群體（2人）造成一次兵刃攻擊（傷害率59%→118%）并附加灼燒狀態，每回合持續造成傷害（傷害率33%→66%），持續1回合", False),
        ("有主題揭露(提及雙重計算)不應誤報",
         _base_tactic(type="active", coef=1.12, rate=0.45, n=2,
                      effects=[{"k": "dot", "who": "enemy", "coef": 1.12, "dur": 2, "n": 2,
                                "_todo": "coef與dot疑似雙重計算, 待核實"}]),
         "準備1回合，對敵軍群體（2人）施加水攻狀態，每回合持續造成傷害（傷害率56%→112%），持續2回合", False),
    ],
    "R31": [
        ("定謀貴決式回歸(修復前撤回聲明與資料矛盾應抓到): _note聲稱撤回targetSel但欄位仍在",
         _base_tactic(coef=0, effects=[{"k": "amp", "who": "enemy", "val": 0.2, "dur": 2,
                                        "targetSel": "mostDamaged",
                                        "_note": "批21覆核後撤回: 曾嘗試補targetSel:\"mostDamaged\"...已撤回此欄位, 恢復不含targetSel的版本"}]),
         "使敵軍兵力最高的武將嘲諷我軍全體，並使其受到的傷害提高10%→20%", True),
        ("定謀貴決式回歸(修復後真正刪除欄位不應誤報)",
         _base_tactic(coef=0, effects=[{"k": "amp", "who": "enemy", "val": 0.2, "dur": 2,
                                        "_note": "批21覆核後撤回: 曾嘗試補targetSel:\"mostDamaged\"...已撤回此欄位, 恢復不含targetSel的版本"}]),
         "使敵軍兵力最高的武將嘲諷我軍全體，並使其受到的傷害提高10%→20%", False),
        ("已改用maxTroop不應誤報(欄位仍在但不是被聲稱撤回的那個, 且_note已更新為精確落地敘述)",
         _base_tactic(coef=0, effects=[{"k": "amp", "who": "enemy", "val": 0.2, "dur": 2,
                                        "targetSel": "maxTroop",
                                        "_note": "已補targetSel:\"maxTroop\"精確落地, 不再是撤回後的無targetSel近似"}]),
         "使敵軍兵力最高的武將嘲諷我軍全體，並使其受到的傷害提高10%→20%", False),
        ("無撤回關鍵字不應誤報(純粹提及欄位名稱的正常揭露)",
         _base_tactic(coef=0, effects=[{"k": "amp", "who": "enemy", "val": 0.2, "dur": 2,
                                        "targetSel": "maxTroop",
                                        "_note": "targetSel:\"maxTroop\"精確選中兵力最高目標"}]),
         "使敵軍兵力最高的武將嘲諷我軍全體，並使其受到的傷害提高10%→20%", False),
    ],
    "R32": [
        ("虎痴式回歸(coef=0的passive型lockTarget讀不到, 應抓到)",
         _base_tactic(type="passive", coef=0, lockTarget=True),
         "戰鬥中，每回合選擇一名敵軍單體…", True),
        ("同一lockTarget欄位, coef>0的active型讀得到, 不應誤報",
         _base_tactic(type="active", coef=1.5, n=1, lockTarget=True),
         "戰鬥中，每回合選擇一名敵軍單體…", False),
        ("陷陣突襲式回歸(when.on==activeFired反應式不支援lockTarget, 應抓到)",
         _base_tactic(type="passive", coef=0.95, when={"on": "activeFired"}, lockTarget=True),
         "自身成功發動突擊戰法後，對目標發動1次兵刃攻擊", True),
        ("摧鋒斷刃式回歸(charge型+hitsRepeat, 批D修復後應讀得到, 不應誤報)",
         _base_tactic(type="charge", coef=0.5, n=3, hitsRepeat=True),
         "普攻後發動三次隨機打擊", False),
        ("rateLeader用在reactive:dealtDamage(該shape未支援rateLeader讀取, 應抓到)",
         _base_tactic(type="passive", coef=1.0, when={"on": "dealtDamage"}, rateLeader=0.6),
         "自身造成傷害時…自身為主將時機率提升", True),
        ("同一rateLeader用在reactive:attacked(淵然難測式, on_hit_for()已支援, 不應誤報)",
         _base_tactic(type="passive", coef=0, when={"on": "attacked"}, rateLeader=0.6,
                      effects=[{"k": "amp", "who": "self", "val": 0.03, "dur": 1}]),
         "自身受到攻擊時…自身為主將時機率提升", False),
        ("完全未知的頂層欄位名稱(疑似手誤/新原語忘記登記, 應抓到)",
         _base_tactic(type="active", coef=1.0, n=1, totallyUnknownField=True),
         "對敵軍單體造成兵刃攻擊", True),
        ("type==none的內政戰法整戰法跳過, 不應誤報(即使帶著奇怪欄位)",
         _base_tactic(type="none", coef=0, totallyUnknownField=True),
         "內政類戰法，不參與戰鬥", False),
    ],
    "R33": [
        # (a) 「兵力最低/損失最多」→ 需要 targetSel(mostDamaged/minTroop)
        ("刮骨療毒式回歸(本文兵力最高但heal缺targetSel, 應抓到)",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 2.56, "dur": 1, "scale": "intel"}]),
         "為損失兵力最高的我軍單體清除負面狀態並為其恢復兵力（治療率256%，受智力影響）", True),
        ("同一戰法補上targetSel:mostDamaged後不應誤報",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 2.56, "dur": 1, "scale": "intel",
                                 "targetSel": "mostDamaged"}]),
         "為損失兵力最高的我軍單體清除負面狀態並為其恢復兵力（治療率256%，受智力影響）", False),
        ("targetSel:minTroop亦視為合格(垂心萬物既有寫法, 不應誤報)",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 1.0, "dur": 1, "targetSel": "minTroop"}]),
         "為我軍兵力最低的武將恢復兵力（治療率100%）", False),
        # (b) 「隨機單體/目標隨機」→ 不得落入隱含min-troop後備分支
        ("益其金鼓式回歸(本文明寫隨機單體但heal缺e.n落入隱含min-troop, 應抓到)",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 0.75, "dur": 1, "scale": "force"}]),
         "自身即將受到普通攻擊時，治療我軍隨機單體（治療率75%）", True),
        ("補上e.n=1後不應誤報(顯式隨機挑1人, 取代隱含後備)",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 0.75, "dur": 1, "scale": "force", "n": 1}]),
         "自身即將受到普通攻擊時，治療我軍隨機單體（治療率75%）", False),
        ("勠力同心式陰性樣例('隨機執行N次'修飾次數而非目標, e.n=1後只殘留(c)的合格判定, 不應誤報, 防止(b)的隨機正則誤傷)",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 1.0, "dur": 1, "n": 1}]),
         "治療我軍單體並提升其統率，隨機執行1-4次、受智力影響、統率提升16點。", False),
        ("(b)獨立驗證: '隨機執行N次'即使heal完全無選標(who=ally無n)也不應被(b)誤觸發(only(c)才該管, 見上一筆同文字改用n=1後的對照); 此處刻意用who=self規避(c), 純粹隔離驗證(b)regex本身不誤傷",
         _base_tactic(effects=[{"k": "heal", "who": "self", "coef": 1.0, "dur": 1}]),
         "治療我軍單體並提升其統率，隨機執行1-4次、受智力影響、統率提升16點。", False),
        # (c) 「單體」(緊鄰治療動詞) → 需要 who=self/leader/eventTarget/targetSel 或 e.n==1
        ("百計多謀式回歸(本文單體但heal缺顯式選標落入隱含min-troop, 應抓到)",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 0.4, "dur": 1, "scale": "intel"}]),
         "戰鬥中，每回合有機率治療我軍單體（治療率40%，受智力影響）", True),
        ("補上e.n=1後不應誤報",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 0.4, "dur": 1, "scale": "intel", "n": 1}]),
         "戰鬥中，每回合有機率治療我軍單體（治療率40%，受智力影響）", False),
        ("who:eventTarget亦視為合格單體選標(反應式急救類, 不應誤報)",
         _base_tactic(effects=[{"k": "heal", "who": "eventTarget", "coef": 0.75, "dur": 1,
                                 "when": {"on": "damaged"}, "rate": 0.5}]),
         "使我軍單體獲得急救狀態，每次受到傷害時有50%機率回復一定兵力（治療率75%）", False),
        ("who:self亦視為合格單體選標(不應誤報)",
         _base_tactic(effects=[{"k": "heal", "who": "self", "coef": 0.5, "dur": 2, "scale": "force"}]),
         "恢復自身兵力並提高統率", False),
        # (d) 「群體(N人)」→ e.n 須數字相符
        ("仁德載世式回歸(本文群體2-3人但heal缺e.n, 應抓到)",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 0.68, "dur": 1, "scale": "intel"}]),
         "每回合治療我軍群體（2-3 人，治療率34%→68%，受智力影響）", True),
        ("補上e.n=2後不應誤報(nMax=3的範圍取下界比對, 主要驗證非0非None)",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 0.68, "dur": 1, "scale": "intel",
                                 "n": 2, "nMax": 3}]),
         "每回合治療我軍群體（2-3 人，治療率34%→68%，受智力影響）", False),
        ("e.n數字不符應抓到(群體2人但誤標n=1)",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 1.2, "dur": 1, "n": 1}]),
         "為我軍群體（2人）恢復兵力（治療率120%）", True),
        # 金丹秘術式陰性樣例: e.all:true 精確表達「我軍全體」, 群體N人數字檢查應放行(不誤報)
        ("e.all:true對應本文全體語意, 不應誤報",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 0.58, "dur": 1, "scale": "intel", "all": True}]),
         "使我軍全體獲得休整狀態，每回合恢復一次兵力（回復58%，受智力影響）", False),
        # 主題綁定揭露豁免: 效果級_note提及目標相關關鍵字時應豁免(對稱R13既有慣例)
        ("已有主題相符的_note揭露時應豁免不誤報(topic關鍵字'單體'命中R13_TARGET_TOPIC_KW)",
         _base_tactic(effects=[{"k": "heal", "who": "ally", "coef": 2.56, "dur": 1,
                                 "_note": "已知本文為我軍單體選標, 待未來校準targetSel"}]),
         "為損失兵力最高的我軍單體恢復兵力（治療率256%）", False),
        # 無heal效果/無本文時應直接跳過(不誤報)
        ("無heal效果時不應觸發(其他k種類不受R33管轄)",
         _base_tactic(effects=[{"k": "amp", "who": "ally", "val": 0.1, "dur": 1}]),
         "為損失兵力最高的我軍單體提升攻擊力", False),
    ],
    "R34": [
        ("本文含會心但effects仍是amp EV折算應抓到(百步穿楊/猛擊類舊近似回歸測試)",
         _base_tactic(effects=[{"k": "amp", "who": "self", "val": 0.15, "dur": 2}]),
         "普通攻擊之後，提高自身7.5%→15%會心機率（觸發時兵刃傷害提高100%），持續2回合", True),
        ("補上critUp後不應誤報(猛擊批H遷移後的正確形狀)",
         _base_tactic(effects=[{"k": "critUp", "who": "self", "val": 0.15, "dur": 2, "dmgType": "phys"}]),
         "普通攻擊之後，提高自身7.5%→15%會心機率（觸發時兵刃傷害提高100%），持續2回合", False),
        ("本文含奇謀但effects仍是amp EV折算應抓到(太平道法類舊近似回歸測試)",
         _base_tactic(effects=[{"k": "amp", "who": "self", "val": 0.28, "dur": 99}]),
         "獲得14%→28%奇謀並提高自帶主動戰法發動機率", True),
        ("補上critUp(dmgType:intel)後不應誤報",
         _base_tactic(effects=[{"k": "critUp", "who": "self", "val": 0.28, "dur": 99, "dmgType": "intel"}]),
         "獲得14%→28%奇謀並提高自帶主動戰法發動機率", False),
        ("critDmgUp(幅度修飾語, 如華服)亦視為合格crit原語, 不應誤報",
         _base_tactic(effects=[{"k": "critDmgUp", "who": "self", "val": 0.12, "dur": 99, "dmgType": "phys"}]),
         "提高12%會心傷害", False),
        ("critUp+stackKey疊層形狀(逆鱗, 對稱amp+stackKey既有precedent)亦視為合格, 不應誤報",
         _base_tactic(effects=[{"k": "critUp", "who": "self", "val": 0.10, "dur": 2, "dmgType": "phys",
                                 "rate": 0.03, "when": {"on": "damaged"}, "stackKey": True, "maxStacks": 2}]),
         "受到傷害時，有3%機率獲得10%會心，持續2回合，可疊加2次", False),
        ("有主題相符的折算/EV揭露時應豁免不誤報(西涼鐵騎ifLeaderIs+scale複合缺口既有停損案例, 已用critUp但仍有殘留_todo)",
         _base_tactic(effects=[{"k": "critUp", "who": "ally", "val": 0.25, "dur": 3, "dmgType": "phys"}],
                       _todo="若馬騰統領則提高會心機率受速度影響, 條件式scale複合缺口, EV折算已由critUp取代但此殘留仍需查證"),
         "戰鬥前3回合，提高我軍全體12.5%→25%會心機率（觸發時兵刃傷害提高100%）；若馬騰統領，則提高會心機率受速度影響", False),
        ("完全無crit原語且無揭露應抓到(即使有ifLeaderIs等其他欄位, 仍應被R34獨立抓出crit本身缺失)",
         _base_tactic(effects=[{"k": "amp", "who": "ally", "val": 0.06, "dur": 99, "ifLeaderIs": "甘寧"}]),
         "若甘寧統領，提高友軍3%→6%會心", True),
        ("無會心/奇謀字樣的本文不應觸發(其他戰法不受R34管轄)",
         _base_tactic(effects=[{"k": "amp", "who": "ally", "val": 0.1, "dur": 1}]),
         "提高我軍全體造成的傷害", False),
    ],
    "R35": [
        ("effects內含_approx欄位應抓到(硬性檢查, 戰法頂層無其他問題但_approx本身就違規)",
         _base_tactic(effects=[{"k": "amp", "who": "self", "val": 0.1, "dur": 1, "_approx": "prob-ev"}]),
         "提高自身造成的傷害", True),
        ("_approx帶完整_note揭露文字亦不豁免(R35刻意設計: _approx無可豁免狀態)",
         _base_tactic(effects=[{"k": "amp", "who": "self", "val": 0.1, "dur": 1,
                                 "_approx": "已知折算", "_note": "這是很詳細的揭露說明文字"}]),
         "提高自身造成的傷害", True),
        ("戰法頂層(非effects內)含_approx亦應抓到(遞迴掃描, 對稱一騎當千/絕地反擊等頂層案例)",
         _base_tactic(_approx="某段近似敘述", effects=[]),
         "提高自身造成的傷害", True),
        ("extraHits內含_approx亦應抓到(遞迴掃描涵蓋extraHits)",
         _base_tactic(effects=[], extraHits=[{"who": "sameTarget", "coef": 1.0, "_approx": "prob-ev"}]),
         "提高自身造成的傷害", True),
        ("set內含_approx亦應抓到(遞迴掃描涵蓋set, 對稱絕計折謀等set._approx案例)",
         _base_tactic(effects=[], set={"coef": 0, "_approx": "殘留標記"}),
         "提高自身造成的傷害", True),
        ("choices內effects含_approx亦應抓到(遞迴掃描涵蓋choices巢狀effects)",
         _base_tactic(effects=[], choices=[{"weight": 1, "effects": [{"k": "amp", "val": 0.1, "_approx": "x"}]}]),
         "提高自身造成的傷害", True),
        ("無_approx且_note為歷史紀錄(含「已改用/取代」訊號詞)不應誤報(全庫常態寫法)",
         _base_tactic(effects=[{"k": "critUp", "who": "self", "val": 0.1, "dur": 1, "dmgType": "phys",
                                 "_note": "原本用amp近似折算EV, 現已改用critUp真擲骰取代舊近似"}]),
         "提高自身造成的傷害", False),
        ("無_approx且_todo誠實揭露未建模(非現行近似, 是誠實維持未實作)不應誤報",
         _base_tactic(effects=[],
                      _todo="本文某段機制過於複合, engine無法表達, 誠實維持未建模, 完全空白不擅自近似"),
         "提高自身造成的傷害", False),
        ("無_approx且完全無揭露文字亦不應誤報(R35只管_approx欄位與散文包裝關鍵字, 不是全庫揭露稽核)",
         _base_tactic(effects=[{"k": "amp", "who": "self", "val": 0.1, "dur": 1}]),
         "提高自身造成的傷害", False),
        ("_note含'近似'字樣但_note不在R35軟性檢查範圍內(只查_todo, 見check_r35設計說明),"
         "不應誤報——_note是歷史敘述欄位, 全庫389筆戰法常態合法提及'近似'描述過去狀態,"
         "若連_note都掃會拉出大量無關歷史敘述假警報(批K實測200+筆), 故只查_todo",
         _base_tactic(effects=[{"k": "amp", "who": "self", "val": 0.1, "dur": 1,
                                 "_note": "此處數值為近似值, 依原文機率折算而來"}]),
         "提高自身造成的傷害", False),
        ("_todo含'近似'字樣且全文找不到任何缺口揭露訊號詞應抓到(疑似用_todo散文規避硬性"
         "_approx檢查, 真正的R35軟性檢查目標案例)",
         _base_tactic(effects=[{"k": "amp", "who": "self", "val": 0.1, "dur": 1}],
                      _todo="此處數值為近似值, 依原文機率折算而來"),
         "提高自身造成的傷害", True),
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
