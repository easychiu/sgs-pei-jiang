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


def extract_dmg_pct(txt):
    """取滿級傷害率(取範圍上限), 找不到回傳 None。"""
    m = DMG_RANGE.search(txt)
    if m:
        return float(m.group(2))
    m = DMG_SINGLE.search(txt)
    if m:
        return float(m.group(1))
    return None


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
                if e.get("k") == "first":              # first 的 dur 來自「戰鬥前N回合」(6c), 非「持續N回合」
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
    print(f"no-op 戰法: {noop_before} -> {noop_after}")
    print(f"抽不到數字, 標記 _est=true 的戰法數: {n_est}")


if __name__ == "__main__":
    main()
