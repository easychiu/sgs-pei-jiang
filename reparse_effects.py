# -*- coding: utf-8 -*-
"""
一次性腳本: 用 data/tactics.json 的 effectText(含真實數值) 回填
docs/data/tactics_parsed.json 的 coef/rate/n/nMax/dur/heal-coef。

原則(保守): 只修數值欄位, 不重寫 effects 的語意結構(k/who 等 LLM 既有判斷保留)。
抽不到就不動, 並標記 "_est": true。

新增: 依 effectText 關鍵字偵測「計窮/繳械/震懾/洞察」等控制/免控語意:
- 既有 k="stun"(who=enemy) 效果, 若文字段落「恰好命中其中一種」關鍵字(計窮/技窮→silence;
  繳械→disarm; 震懾→維持 stun), 精分類為更準確的原語; 混雜多種關鍵字時保守不動(維持 stun)。
- 洞察(免疫控制): 僅在 effectTarget 精確等於「自己」時, 才新增一個 k="insight" 效果
  (無法從既有 15 原語表達)。「先攻」語意因目標對象(自己/我軍主將/我軍全體)混雜,
  regex 無法安全判斷, 保守跳過, 留給引擎端(item 5)人工判斷或後續 LLM 精解。

用法: python reparse_effects.py
輸出: 就地覆寫 docs/data/tactics_parsed.json, 並印出回填報告。
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW_PATH = os.path.join(ROOT, "data", "tactics.json")
PARSED_PATH = os.path.join(ROOT, "docs", "data", "tactics_parsed.json")

DMG_RANGE = re.compile(r"傷害率\s*(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)\s*%?")
DMG_SINGLE = re.compile(r"傷害率\s*(\d+(?:\.\d+)?)\s*%")
HEAL_RANGE = re.compile(r"治療率\s*(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)\s*%?")
HEAL_SINGLE = re.compile(r"治療率\s*(\d+(?:\.\d+)?)\s*%")
DUR_RE = re.compile(r"持續\s*(\d+(?:\.\d+)?)\s*(?:[-~]\s*(\d+(?:\.\d+)?)\s*)?回合")
TARGET_N_RANGE = re.compile(r"(\d+)\s*[-~]\s*(\d+)\s*人")
TARGET_N_SINGLE = re.compile(r"(\d+)\s*人")
# 指揮/被動戰法內文常見的「條件觸發機率」(如「有 25%→35% 機率…」), 與戰法整體
# activationRate(常為 1, 代表該戰法本身無外部次數限制)是兩件事: 真正逐回合的
# 觸發機率藏在 effectText 裡。只在「有明確傷害 coef 且 activationRate==1」時才需要,
# 否則沿用既有 rate, 避免對純被動增益類戰法做不必要的改動。
# 機率同義詞: 機率/概率/幾率 都常見(繁簡混雜的社群文本)。
INLINE_RATE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:→|~|-)?\s*(\d+(?:\.\d+)?)?\s*%?\s*(?:的)?\s*(?:機率|概率|幾率)")

SILENCE_KW = ("計窮", "技窮")
DISARM_KW = ("繳械",)
STUN_KW = ("震懾",)
INSIGHT_KW = ("洞察",)
# 機率關鍵字後緊跟這些詞 = 那是「狀態自身的機率」(規避率/會心率…), 不是傷害觸發機率
RATE_NOT_TRIGGER = ("規避", "會心", "奇謀")
# 先攻(優先行動): 只認「語意明確的直接授予句型」, 對象限 自己/我軍主將/我軍全體。
# 條件式(坐斷東南隨機獲得)/狀態引用(臥薪嘗膽依狀態數)/含糊敘述(蓄勢待發)一律跳過。
FIRST_GRANT_RE = re.compile(
    r"使我軍全體獲得先攻|我軍全體戰鬥前\s*\d+\s*回合獲得先攻|使我軍主將獲得先攻"
    r"|使自己及友軍單體獲得先攻|使自己獲得先攻|自身獲得先攻")
FIRST_DUR_RE = re.compile(r"戰鬥前\s*(\d+)\s*回合")

# ---------------------------------------------------------------------------
# 批2: 條件觸發(when) + 新原語(taunt/shield/dodge/surehit)
#
# 條件觸發/新原語語意在 384 條 effectText 裡幾乎每條都夾雜多個子句(不同回合窗口、
# 不同觸發條件混在同一段話裡), 通用 regex 容易對到錯的子句(把「always-on」的效果
# 錯套上某個無關子句的回合窗口, 或把「必中/規避」拿來描述另一個效果的說明文字誤標)。
# 沿用本檔一貫的保守原則: 逐條人工核對 effectText 後, 只用「白名單」方式標記語意
# 100%明確、且該戰法的 coef/effects 整體恰好對應同一子句(不會誤套到不相關子句)的
# 案例; 其餘一律跳過, 保留給後續 LLM 精解。跳過清單見腳本輸出。
# ---------------------------------------------------------------------------

# --- when.rounds/from/until: coef+effects 整體只對應同一個回合窗口子句 --------
# 「戰鬥第2回合開始」dot/群體傷害才開始生效(prep 時的 dur 只能表示「維持多久」,
# 無法表示「延後開始」, 是既有 15 原語表達不到的缺口, 需要新的 when.from)。
WHEN_FROM = {
    "興雲佈雨": 2, "興雲布雨": 2,          # 戰鬥第2回合開始, 使敵軍全體進入水攻狀態(dot/amp 皆對應此子句)
}
# 「戰鬥前N回合」的 until 語意多數已由既有 dur=N (prep 時套用一次, N 回合後到期) 天然覆蓋,
# 不需要額外 when; WHEN_UNTIL 僅保留給未來若需要嚴格重跑驗證用, 目前無新增案例。
WHEN_UNTIL = {}

# --- when.on: 反應式觸發, coef+effects 整體只對應「受到普通攻擊時/受到傷害後」子句 ---
ON_ATTACKED = {
    "氣凌三軍": "受到普通攻擊時對攻擊者進行一次反擊",
    "後發制人": "受到普通攻擊時對攻擊者進行一次反擊",
    "眾動萬計": "受到普通攻擊時,45%機率對攻擊來源造成兵刃傷害並使其下次傷害降低",
    "魅惑": "自身受到普通攻擊時,機率使攻擊者進入計窮等狀態的一種",
}
ON_DAMAGED = {
    "剛勇無前": "受到兵刃傷害後,下回合行動時提高會心並使下一擊傷害提高",
}

# --- rate 白名單覆寫: on 觸發類戰法的內文機率(activationRate=1 但實際觸發率藏在
# effectText, 且 coef=0 走不到既有 item 2 的 inline rate 抽取路徑)。
# 魅惑「有 22.5%→45% 機率使攻擊者進入…」滿級 0.45; 不覆寫的話 rate=1 =
# 每次被普攻 100% 觸發控制, 嚴重高估(比照 眾動萬計 rate=0.45 的既有處理)。
RATE_OVERRIDE = {"魅惑": 0.45}

# --- taunt(嘲諷): 中招敵軍強制普攻/單體戰法指向施放者。只認「嘲諷」關鍵字;
# 「偽報」在本庫實際語意因戰法而異(當鋒摧決定義成「禁用被動/指揮戰法」, 並非目標
# 改向), 與任務描述的「隨機目標」假設不符, 保守全部跳過偽報, 只標「嘲諷」。
# 威風凜凜「嘲諷…或繳械…持續2回合」擇一施加: disarm 那半既已標記, taunt 那半
# 同句同確定性一併補標(引擎近似為兩者皆施加, 略強於實際擇一, 可接受)。
# 定謀貴決 保守跳過: 原文「使敵軍兵力最高的武將嘲諷我軍全體」是反向嘲諷——我方
# 全體被迫集火該敵將(=標記/集火語意), 與 taunt 原語(中招者攻擊施放者)方向恰好
# 相反, 硬標會把集火目標弄反; 等未來加「標記集火」原語再處理。
# ADD(不取代既有 stun/redirect 等效果, 避免動到既有平衡/回歸): 額外附加 taunt 效果。
TAUNT_DUR = {
    "固若金湯": 2, "守而必固": 4, "江東猛虎": 2, "威風凜凜": 2,
    "獨行赴鬥": 2, "益其金鼓": 1, "唇槍舌戰": 1, "挑釁": 1,
}

# --- dodge(規避): 語意明確標出「機率規避」且無其他子句混雜共用同一效果格。
# 金丹秘術: 既有 mitig(val=0.35) 其實是誤將「規避可免疫傷害」近似成部分減傷,
# 數值(0.35)恰好對應規避機率, 這裡連 k 一併改正(mitig -> dodge)較準確;
# 因利制權: 「獲得12.5%→25%規避…持續1回合」effects=[] 空的, 純新增, prob 從原文抽;
# 士別三日: 「戰鬥前3回合…獲得15%→30%機率規避效果」prob 從原文抽, dur=3(前3回合);
# 蓄勢待發: 「每回合機率賦予我軍群體規避狀態」語意明確但原文無數值(概要式文本),
#   prob 用引擎預設 0.2 並記錄假設, dur=1(每回合重新賦予的近似)。
DODGE_ADD = {
    "因利制權": {"prob_kw": "規避", "dur": 1, "who": "ally"},
    "士別三日": {"prob_kw": "規避", "dur": 3, "who": "self"},
    "蓄勢待發": {"prob": 0.2, "dur": 1, "who": "ally"},   # 原文無數值, 取引擎預設 0.2(_假設_)
}
DODGE_FIX_MITIG = ("金丹秘術",)                            # 既有 mitig 誤用, 改標成 dodge(沿用原 val/dur)

# --- surehit(必中): 語意為施放者(或指定我方單體)明確獲得必中, 且不是「必中/洞察/
# 先攻/連擊隨機一種」這類多選一的模糊描述(坐斷東南等跳過)。
# 驍健神行: 「使自身獲得必中…持續2回合」;
# 國士將風: 「戰鬥前3回合, 使自己及友軍單體獲得先攻…和必中…」同句 first 已標 dur=3, 必中比照;
# 萬軍取將: 「自身獲得必中及破陣」概要式文本無持續回合, dur=1 保守假設(單回合)。
SUREHIT_ADD = {"驍健神行": 2, "國士將風": 3, "萬軍取將": 1}

# --- shield(護盾): 需要可解析的具體吸收量(固定值或兵力%), 全庫僅「赴湯蹈火」提及
# 護盾但敘述是「多層抵禦疊加機制」, 無具體數字可抽, 保守跳過(見報告)。
SHIELD_ADD = {}


def extract_dmg_pct(txt):
    """取滿級傷害率(取範圍上限), 找不到回傳 None。"""
    m = DMG_RANGE.search(txt)
    if m:
        return float(m.group(2))
    m = DMG_SINGLE.search(txt)
    if m:
        return float(m.group(1))
    return None


def _maxval(txt, kw):
    """抓 'kw...A→B' 或 'kw...A%→B%', 回傳升滿值 B(取範圍上限); 找不到回傳 None。
    與 sgz.py 內同名函式邏輯一致, 這裡獨立一份供 reparse 的白名單數值抽取用。"""
    m = re.search(re.escape(kw) + r"[^0-9]{0,10}(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)", txt)
    if m:
        return float(m.group(2))
    m = re.search(re.escape(kw) + r"[^0-9]{0,10}(\d+(?:\.\d+)?)", txt)
    return float(m.group(1)) if m else None


def _maxval_before(txt, kw):
    """抓 'A→B%kw' 或 'A%→B%kw'(數值在關鍵字之前, 如「12.5%→25%規避」), 回傳升滿值 B; 找不到回傳 None。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)\s*%?" + r"[^0-9]{0,4}" + re.escape(kw), txt)
    if m:
        return float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*%" + r"[^0-9]{0,4}" + re.escape(kw), txt)
    return float(m.group(1)) if m else None


def extract_heal_pct(txt):
    m = HEAL_RANGE.search(txt)
    if m:
        return float(m.group(2))
    m = HEAL_SINGLE.search(txt)
    if m:
        return float(m.group(1))
    return None


def _num(s):
    """字串轉數值: 整數值回傳 int, 否則回傳 float(維持 JSON 輸出簡潔)。"""
    v = float(s)
    return int(v) if v == int(v) else v


def extract_dur(txt):
    """取第一個「持續X[~Y]回合」的 X(下限/單值)。範圍型回傳 (lo, hi)。"""
    m = DUR_RE.search(txt)
    if not m:
        return None, None
    lo = _num(m.group(1))
    hi = _num(m.group(2)) if m.group(2) else None
    return lo, hi


def extract_target_n(effect_target):
    """從 effectTarget 欄位(如「我軍群體 2-3 人」)取 n / nMax。"""
    if not effect_target:
        return None, None
    m = TARGET_N_RANGE.search(effect_target)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = TARGET_N_SINGLE.search(effect_target)
    if m:
        return int(m.group(1)), None
    return None, None


def has_kw(txt, kws):
    return any(k in txt for k in kws)


def _rate_match_ok(text, m):
    """機率 match 的排除規則: (1) 關鍵字後緊跟 規避/會心/奇謀 = 狀態機率非觸發機率;
    (2) match 位於未閉合的全形括號內 = 括號內註解(如「每有1名…提升2%→4%機率」), 非主觸發鏈。"""
    after = text[m.end():m.end() + 2]
    if after.startswith(RATE_NOT_TRIGGER):
        return False
    if text.count("（", 0, m.start()) > text.count("）", 0, m.start()):
        return False
    return True


def extract_inline_rate(txt):
    """抽 command/passive 戰法內文的「傷害觸發機率」(有別於戰法整體 activationRate)。

    綁定規則(避免全文首匹配誤綁, 如士別三日的規避機率):
    - 有「傷害率」字樣時: 只在「傷害率所在子句」(以 。；;/ 為句界)中、且位於傷害率
      之前的機率裡取「最近的一個」; 若機率與傷害率之間出現「第N回合」(換了時間子句)
      則該機率不算同一觸發鏈。
    - 無「傷害率」字樣(coef 為 LLM 粗估)時: 退回全文首個合法機率。
    - 取範圍滿級值(X%→Y% 取 Y)。找不到回傳 None(特定回合/反應式觸發等留給引擎近似)。"""
    dm = DMG_RANGE.search(txt) or DMG_SINGLE.search(txt)
    if dm:
        starts = [txt.rfind(ch, 0, dm.start()) for ch in "。；;/"]
        window = txt[max(starts) + 1:dm.start()]
        best = None
        for m in INLINE_RATE_RE.finditer(window):
            if not _rate_match_ok(window, m):
                continue
            if re.search(r"第\s*\d+\s*回合", window[m.end():]):
                continue                              # 機率與傷害間隔了新的時間子句
            best = m
        if best is None:
            return None
        v = float(best.group(2)) if best.group(2) else float(best.group(1))
        return round(v / 100, 4)
    for m in INLINE_RATE_RE.finditer(txt):
        if _rate_match_ok(txt, m):
            v = float(m.group(2)) if m.group(2) else float(m.group(1))
            return round(v / 100, 4)
    return None


def main():
    with open(RAW_PATH, encoding="utf-8") as f:
        raw_list = json.load(f)
    raw_by_name = {t["nameZh"]: t for t in raw_list}

    with open(PARSED_PATH, encoding="utf-8") as f:
        parsed = json.load(f)

    n_coef_filled = 0
    n_rate_filled = 0
    n_n_filled = 0
    n_dur_filled = 0
    n_heal_filled = 0
    n_new_ctrl_tags = 0
    first_tagged = []
    n_est = 0
    n_when_filled = 0
    n_taunt_tagged = 0
    n_dodge_tagged = 0
    n_surehit_tagged = 0
    n_shield_tagged = 0
    noop_before = sum(1 for p in parsed if p.get("type") != "none"
                       and not p.get("coef") and not p.get("effects"))

    for p in parsed:
        if p.get("type") == "none":
            continue
        r = raw_by_name.get(p["nameZh"])
        if not r:
            p["_est"] = True
            n_est += 1
            continue
        txt = r.get("effectText") or ""
        # touched_meaningful: 是否至少改到一個「有實質意義」的數值(coef/dur/heal)。
        # rate 幾乎每個戰法都有 activationRate 可核對, 光是 rate 對上不足以代表「解析成功」,
        # 故不計入 touched_meaningful, 避免真正的 no-op/待補資料被誤判成「已處理」。
        touched_meaningful = False

        # --- 1) 傷害率 -> coef -------------------------------------------------
        dmg_pct = extract_dmg_pct(txt)
        if dmg_pct is not None:
            new_coef = round(dmg_pct / 100, 4)
            if p.get("coef") is None or abs((p.get("coef") or 0) - new_coef) > 1e-9:
                p["coef"] = new_coef
                n_coef_filled += 1
            touched_meaningful = True

        # --- 2) 發動率 -> rate ---------------------------------------------------
        # 先決定「目標 rate 值」再一次寫入, 避免 2a(寫 activationRate)接著被
        # 2b(指揮/被動內文機率覆寫)蓋掉, 導致重複執行時錯誤地重複計入回填次數。
        ar = r.get("activationRate")
        target_rate = ar
        # 指揮/被動 且有傷害 coef 時: activationRate 常是 1(代表戰法本身無外部次數
        # 限制), 真正逐回合觸發機率藏在 effectText 內文(如「有25%→35%機率」)。
        # 舊引擎用 CMD_TRIGGER/PASSIVE_TRIGGER 全域折扣近似這個機率, 新引擎改成直接
        # 讀 rate 逐回合擲骰, 所以這裡要把內文機率覆寫進 rate, 否則 rate=1 會變成
        # 每回合必定觸發, 大幅高估指揮/被動戰法的傷害輸出。
        # 只在「有明確傷害 coef」時才覆寫, 避免動到純增益類戰法(它們的 100% 生效是設計本意)。
        if p.get("type") in ("command", "passive") and p.get("coef") and ar == 1:
            inline_rate = extract_inline_rate(txt)
            if inline_rate is not None:
                target_rate = inline_rate
        # 批2 白名單覆寫: coef=0 的 on 觸發類走不到上面的 inline 抽取(它只認有 coef 的),
        # 內文機率需白名單指定(如 魅惑 0.45), 在寫入前覆寫確保單次寫入、計數冪等。
        if p["nameZh"] in RATE_OVERRIDE:
            target_rate = RATE_OVERRIDE[p["nameZh"]]
        if target_rate is not None:
            if p.get("rate") is None or abs((p.get("rate") or 0) - target_rate) > 1e-9:
                p["rate"] = target_rate
                n_rate_filled += 1
            # 注意: rate 不計入 touched_meaningful(_est 判定) — 幾乎每個戰法都有
            # activationRate 可核對, 光是 rate 對上不代表該戰法的效果資料完整。

        # --- 3) 目標數 -> n / nMax (effectTarget 欄位, 較 effectText 結構化) -----
        tgt_n, tgt_nmax = extract_target_n(r.get("effectTarget"))
        if tgt_n is not None:
            if p.get("n") != tgt_n:
                p["n"] = tgt_n
                n_n_filled += 1
            if tgt_nmax is not None and p.get("nMax") != tgt_nmax:
                p["nMax"] = tgt_nmax
            touched_meaningful = True

        # --- 4) 持續 -> 各 effect 的 dur (只套用在已有 dur 欄位的效果, 且非 stun 特例) --
        dur_lo, dur_hi = extract_dur(txt)
        if dur_lo is not None:
            for e in p.get("effects", []):
                if "dur" not in e:
                    continue
                # first 的 dur 來自「戰鬥前N回合」(6c), 非「持續N回合」;
                # taunt/dodge/surehit 的 dur 來自批2白名單(9~11)的專屬子句解析, 全文首個
                # 「持續N回合」常屬於同段落裡的另一個效果(如 驍健神行 的「持續1回合」屬於
                # disarm, 不該覆寫「持續2回合」的 surehit), 故排除, 避免非冪等覆寫。
                if e.get("k") in ("first", "taunt", "dodge", "surehit"):
                    continue
                cur = e.get("dur")
                if cur is not None and cur >= 90:      # 永久型(99) 不動: 語意是"戰鬥全程"
                    continue
                if cur is None or abs(cur - dur_lo) > 1e-9:
                    e["dur"] = dur_lo
                    n_dur_filled += 1
                if dur_hi is not None:
                    e["durMax"] = dur_hi
                touched_meaningful = True

        # --- 5) 治療率 -> heal coef ----------------------------------------------
        heal_pct = extract_heal_pct(txt)
        if heal_pct is not None:
            new_hcoef = round(heal_pct / 100, 4)
            for e in p.get("effects", []):
                if e.get("k") == "heal":
                    if e.get("coef") is None or abs((e.get("coef") or 0) - new_hcoef) > 1e-9:
                        e["coef"] = new_hcoef
                        n_heal_filled += 1
                    touched_meaningful = True

        # --- 6a) 既有 stun(who=enemy) 效果精分類: 依 effectText 關鍵字單一命中時,
        # 把過度概括的 "stun" 換成更精確的 silence(計窮/技窮=禁主動) 或
        # disarm(繳械=禁普攻); 純震懾(全禁)維持 stun。
        # 只在該效果對應文字段落「恰好命中其中一種」關鍵字(無混雜)時才改, 避免誤判。
        for e in p.get("effects", []):
            if e.get("k") in ("stun", "silence", "disarm") and e.get("who") == "enemy":
                has_s = has_kw(txt, SILENCE_KW)
                has_d = has_kw(txt, DISARM_KW)
                has_z = has_kw(txt, STUN_KW)
                hit_kinds = sum([has_s, has_d, has_z])
                if hit_kinds == 1:
                    new_k = "silence" if has_s else ("disarm" if has_d else "stun")
                    if e["k"] != new_k:
                        e["k"] = new_k
                        n_new_ctrl_tags += 1
                    touched_meaningful = True   # 精分類過(或重跑冪等維持), 不應標 _est

        # --- 6b) 洞察(免疫控制) 新增: 僅在 effectTarget 明確為「自己」時才安全新增 -----
        existing_ks = {e.get("k") for e in p.get("effects", [])}
        if has_kw(txt, INSIGHT_KW) and (r.get("effectTarget") or "").strip() == "自己":
            if "insight" not in existing_ks:
                dur_lo2, _ = extract_dur(txt)
                p["effects"].append({"k": "insight", "who": "self", "dur": dur_lo2 if dur_lo2 else 99})
                n_new_ctrl_tags += 1
            touched_meaningful = True   # 無論是新增或已存在(重跑冪等), 都算已妥善處理, 不應標 _est

        # --- 6c) 先攻(優先行動) 新增: 只認明確授予句型, 對象 自己/我軍主將/我軍全體 ------
        fm = FIRST_GRANT_RE.search(txt)
        if fm:
            if "first" not in existing_ks:
                who = "ally" if "我軍全體" in fm.group(0) else "self"  # 我軍主將 以 self(施放者)近似
                dm2 = FIRST_DUR_RE.search(txt)
                p["effects"].append({"k": "first", "who": who,
                                     "dur": int(dm2.group(1)) if dm2 else 1})
                n_new_ctrl_tags += 1
                first_tagged.append(p["nameZh"])
            touched_meaningful = True

        # --- 7) when.rounds/from/until: 條件觸發回合窗口(白名單, 見 WHEN_FROM 註解) ---
        name = p["nameZh"]
        if name in WHEN_FROM:
            w = p.setdefault("when", {})
            if w.get("from") != WHEN_FROM[name]:
                w["from"] = WHEN_FROM[name]
                n_when_filled += 1
            touched_meaningful = True
        if name in WHEN_UNTIL:
            w = p.setdefault("when", {})
            if w.get("until") != WHEN_UNTIL[name]:
                w["until"] = WHEN_UNTIL[name]
                n_when_filled += 1
            touched_meaningful = True

        # --- 8) when.on: 反應式觸發(受到普通攻擊時/受到傷害後), 見 ON_ATTACKED/ON_DAMAGED --
        if name in ON_ATTACKED:
            w = p.setdefault("when", {})
            if w.get("on") != "attacked":
                w["on"] = "attacked"
                n_when_filled += 1
            touched_meaningful = True
        if name in ON_DAMAGED:
            w = p.setdefault("when", {})
            if w.get("on") != "damaged":
                w["on"] = "damaged"
                n_when_filled += 1
            touched_meaningful = True

        # --- 9) taunt(嘲諷): 白名單新增(不取代既有效果), 見 TAUNT_DUR 註解 -------------
        if name in TAUNT_DUR:
            if "taunt" not in existing_ks:
                p["effects"].append({"k": "taunt", "who": "enemy", "dur": TAUNT_DUR[name]})
                n_taunt_tagged += 1
            touched_meaningful = True

        # --- 10) dodge(規避): 純新增 或 修正既有誤標的 mitig, 見 DODGE_ADD/DODGE_FIX_MITIG ---
        if name in DODGE_ADD and "dodge" not in existing_ks:
            spec = DODGE_ADD[name]
            if "prob" in spec:                        # 白名單直接給定(原文無數值的概要式文本)
                v100 = spec["prob"] * 100
            else:
                # 規避的主流句型是「12.5%→25%規避」(數值在關鍵字之前), 先試 before;
                # 若先試 after(_maxval) 會誤抓關鍵字後面無關子句的數字(如 士別三日
                # 「…機率規避效果，第 4 回合…」會錯抓到 4)。
                v100 = _maxval_before(txt, spec["prob_kw"])
                if v100 is None:
                    v100 = _maxval(txt, spec["prob_kw"])
            if v100 is not None:
                p["effects"].append({"k": "dodge", "who": spec.get("who", "ally"),
                                     "prob": round(v100 / 100, 4), "dur": spec["dur"]})
                n_dodge_tagged += 1
                touched_meaningful = True
        if name in DODGE_FIX_MITIG:
            for e in p.get("effects", []):
                if e.get("k") == "mitig":
                    e["k"] = "dodge"
                    e["prob"] = e.pop("val", 0.2)
                    n_dodge_tagged += 1
                    touched_meaningful = True

        # --- 11) surehit(必中): 白名單新增, 見 SUREHIT_ADD 註解 -----------------------
        if name in SUREHIT_ADD and "surehit" not in existing_ks:
            p["effects"].append({"k": "surehit", "who": "self", "dur": SUREHIT_ADD[name]})
            n_surehit_tagged += 1
            touched_meaningful = True

        # --- 12) shield(護盾): 白名單新增, 目前無可解析的具體吸收量案例(見 SHIELD_ADD 註解) ---
        if name in SHIELD_ADD and "shield" not in existing_ks:
            spec = SHIELD_ADD[name]
            p["effects"].append({"k": "shield", "who": spec.get("who", "ally"), **spec["amt_kw"], "dur": spec["dur"]})
            n_shield_tagged += 1
            touched_meaningful = True

        if not touched_meaningful:
            p["_est"] = True
            n_est += 1

    noop_after = sum(1 for p in parsed if p.get("type") != "none"
                      and not p.get("coef") and not p.get("effects"))

    with open(PARSED_PATH, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print("=== effectText 回填報告 ===")
    print(f"coef 回填: {n_coef_filled}")
    print(f"rate 回填: {n_rate_filled}")
    print(f"n(目標數) 回填: {n_n_filled}")
    print(f"dur(持續回合) 回填: {n_dur_filled}")
    print(f"heal coef 回填: {n_heal_filled}")
    print(f"新增控制/先攻原語標記(silence/disarm/insight/first): {n_new_ctrl_tags}")
    print(f"先攻(first) 標記戰法: {', '.join(first_tagged) if first_tagged else '無'}")
    print(f"--- 批2: 條件觸發 + 新原語 ---")
    print(f"when(條件觸發回合窗口/反應式) 回填: {n_when_filled} 個戰法"
          f"（from: {sorted(WHEN_FROM)}；until: {sorted(WHEN_UNTIL)}；"
          f"on=attacked: {sorted(ON_ATTACKED)}；on=damaged: {sorted(ON_DAMAGED)}）")
    print(f"rate 白名單覆寫: {sorted(RATE_OVERRIDE.items())}")
    print(f"taunt(嘲諷) 標記: {n_taunt_tagged} 個戰法（{sorted(TAUNT_DUR)}）"
          f"（定謀貴決 跳過: 反向嘲諷/集火語意, 與 taunt 方向相反）")
    print(f"dodge(規避) 標記: {n_dodge_tagged} 個戰法（新增: {sorted(DODGE_ADD)}；"
          f"修正mitig誤標: {sorted(DODGE_FIX_MITIG)}）")
    print(f"surehit(必中) 標記: {n_surehit_tagged} 個戰法（{sorted(SUREHIT_ADD)}）")
    print(f"shield(護盾) 標記: {n_shield_tagged} 個戰法（{sorted(SHIELD_ADD) if SHIELD_ADD else '無, 全庫僅赴湯蹈火提及但無具體吸收量數字可抽, 保守跳過'}）")
    print(f"no-op 戰法: {noop_before} -> {noop_after}")
    print(f"抽不到數字, 標記 _est=true 的戰法數: {n_est}")


if __name__ == "__main__":
    main()
