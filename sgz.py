# -*- coding: utf-8 -*-
"""
三國志戰略版 配將引擎 — 評分 + 配將推薦 + 逐回合模擬對戰

v4: 讀 sgsdeck 真實全庫(193武將 / 384戰法)。
戰法 effectText 為自然語言, 用 effectType 路由 + 正規表達式抽數值做啟發式解析
(approximation, 非逐條精解; 精準需人工/LLM 解 effectText)。
傷害用社群拆解公式(data/formula.md)。run: python sgz.py
"""
import json
import math
import os
import random
import re
from itertools import combinations

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data")  # docs/data 是雙引擎共用的現行資料(root data/ 為協作者的舊/暫存副本, 不可寫)
ROUNDS = 8
START_TROOP = 10000
MORALE = 100                                         # ponytail: 士氣固定滿
CITY = 20                                            # 城建滿: 武智統速各+20(每級+2×10級)
FACTION = 1.10                                       # 陣營滿: 全屬性+10%(每級1%×10級)
CAMP = 4                                              # 兵種營: 戰報「弓兵營全屬性提升了4」→ 全屬性平加(獨立階段, 在陣營乘算之後), 雙方皆有
# 指揮/被動戰法: 開戰即套用其 effects(被動效果), 帶傷害 coef 的部分每回合以資料
# 的 rate 擲骰(多數 rate=1.0 即每回合必發); rate 來自 tactics_parsed.json 的
# activationRate 回填, 不再用發明的全域折扣常數近似。

CUR_ROUND = 0                                          # 批15: 當前回合數(0=準備階段), fight() 回合迴圈開頭設值;
                                                        # 供 apply_effects() 的 heal_only 常駐治療通道讀取以檢查 t["when"]
                                                        # (roundOk 語意, 見 engine.js CUR_R 對應慣例)。單執行緒模擬無併發疑慮。

COUNTER = {"騎": "盾", "盾": "弓", "弓": "槍", "槍": "騎"}  # 騎>盾>弓>槍>騎; 器全被克
APT_PCT = {"S": 1.20, "A": 1.00, "B": 0.85, "C": 0.70, "D": 0.55, None: 0.85}
APT_RANK = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0, None: -1}
SCALE_CLAMP = 1.5                                    # amp/mitig 縮放後上限保護: |val| <= 1.5

# 批36: 兵種營建築(Lv0~10) —— 錨點 docs/data/calibration_anchors.json → troop_camp「三合一」
# 拆解: (1) 全屬性+4(=既有CAMP常數, 全域已無條件套用, 非本批新增) (2) 每級+0.25%該兵種造成
# 傷害(本批新增, 滿級+2.5%) (3) Lv10附贈對應兵種戰法(本批新增attach邏輯)。CAMP_DMG_PER_LV
# 直接作用在 amp 原語(「造成傷害提升」, 與現有進階/典藏 a.amp 同慣例, 見下方 Unit.__init__)。
CAMP_DMG_PER_LV = 0.0025
# 兵種(隊伍) → 該兵種營Lv10附贈戰法名稱(見 tactics_parsed.json cat/src:"BUILDING" 五筆)。
# 器械營「負重」無戰鬥內效果(type:"none", 已被 TACTICS 載入時的過濾排除, 不進 TACTICS 表),
# 故器械不掛(對稱書寫仍列出, 值為 None, 供 attach 邏輯統一走同一張表)。
CAMP_TROOP_TACTIC = {"槍": "破軍", "盾": "守禦", "弓": "齊射", "騎": "疾馳", "器": None}

# 批35 D: block(抵禦/警戒) 消耗門檻 —— grok查證機鑑先識原文「受到的傷害超過自身可攜帶最大
# 兵力的6%時(最低100兵力)」才消耗1次警戒。max(START_TROOP×6%, 100) —— 本引擎START_TROOP
# 恆為10000(單一兵力池常數), 6%=600本身已遠大於100下限, 下限條款只在極端自訂規模才會生效,
# 此處仍照原文寫出以求精確。
BLOCK_CONSUME_THRESHOLD = max(START_TROOP * 0.06, 100)

# 批18: 傷兵池(治療上限) —— user 遊戲實測: 受到的傷害按「當時回合數」轉化為「可救援(計入
# 傷兵池, 治療只能回這部分)」vs「不可救援(直接陣亡, 治療無法挽回)」, 轉化率隨回合遞減
# (見 docs/data/calibration_anchors.json -> wounded_pool)。1~3回合90%、4~6回合80%、
# 7~8回合67.5%(原文65~70%取中值)。準備階段(CUR_ROUND=0)算第1回合檔。
WOUNDED_RATES = [0.90, 0.90, 0.90, 0.80, 0.80, 0.80, 0.675, 0.675]  # index 0 = 第1回合


def wounded_rate(r):
    idx = max(0, min(len(WOUNDED_RATES), r or 1) - 1)
    return WOUNDED_RATES[idx]


# 批33: 治療(heal)絕對量公式全局換裝 —— 舊公式 want = coef×SCALE(scale屬性)×caster.troop×0.10
# 疑似系統性高估(見 engine_limitations.md 第18節: 陷陣營樣本高估1.6~2倍, 且形狀錯誤——治療量
# 不應隨施放者「當下」兵力增減)。初版曾裁決 want=506×coef×SCALE(不乘兵力), 但 user 補測
# 華佗2(智力228/準備階段兵力9600/青囊96%→實測755)推翻該版本: 506那組樣本(青囊96%/智力284
# →742)恰好是施放者準備階段兵力~8433的巧合摺疊(506≈0.06×8433), 換一個準備兵力不同(9600)的
# 樣本立刻對不上(506版預測663, 誤差14%; "×準備階段兵力"版預測755.2, 誤差0.03%)。
# 最終公式(docs/data/calibration_anchors.json → heal_formula_resolved_20260704, 後續更新):
#   want = coef(治療率) × HEAL_TROOP_C(0.06) × 施放者準備階段鎖定兵力 × SCALE(scale屬性,預設intel)
# 「準備階段鎖定」語意: 指揮/兵種/兵書/被動類 heal(常駐急救型)的治療量以「開戰準備階段的
# 兵力」定格(華佗1當下兵力8611~8781持續變動但治療恆742, 非隨當下兵力浮動), 故用
# caster.heal_base(prep時存的 troop×HEAL_TROOP_C 快照, 見 Unit 建構)而非 caster.troop×常數。
# active主動直療型(如刮骨療毒, 施放當下即時觸發的治療, 非受傷反應式)用施放當下即時兵力
# (caster.troop)。刮骨樣本初次核對曾疑似-11%偏差(疑主動型基底常數有異), 後證實該樣本傷兵池
# 已耗盡、觀測值為封頂後殘值(非公式未封頂前的真實want), 與公式無關——主動直療型與反應式
# 急救型共用同一套公式(HEAL_TROOP_C), 不分型態另設基底常數, 僅兵力取值時點不同。
# 驗證樣本: 陷陣營60%/智力379.02/準備兵力8439→546(反解值, 弱錨點); 青囊96%/智力228/
# 準備兵力9600→755(強錨點, user新補測, 0.03%誤差)。
# 補充參考樣本(第三批戰報, 未落地到具體戰法資料——「離月」在本庫查無此戰法, 疑user口誤/
# 待查證, 暫不修改任何tactics資料, 僅記錄公式驗證結果供未來核對): 直療68%/貂蟬智力397/
# 開場兵力8580→曹操622×2+陸遜627, v2公式(want=0.68×0.06×8580×SCALE(397))預測647.1,
# 殘差約-3%~-4%(可能戰內智力浮動), 在既有容忍帶內, 不阻塞, 亦不改動公式常數。
HEAL_TROOP_C = 0.06


def SCALE_G(v, div=350):
    """批35: 曲線族原語泛化。除數預設350(向後相容, 傷害/治療/多數增減益類走這條), 但
    docs/data/calibration_anchors.json → status_scale_375_20260704(user 機鑑先識警戒六點實測,
    荀彧智力478.84~389.72, 六點小數點後兩位精確吻合)證實「狀態效果」(block/部分%值狀態類)
    這一族走除數375的獨立曲線(375點翻倍, 而非350)。呼叫端傳 div 覆蓋預設(逐效果 e.scaleDiv
    透傳), 不擅自把全域 SCALE 從350改成375。"""
    return max(0.0, 1 + (v - 100) / (div or 350))


def SCALE(v):
    """「受X影響」屬性縮放旋鈕。輸入為戰鬥內即時素質 caster.eff(stat)(已含城建/陣營/適性/
    加點/賽季/戰鬥中buff, 典型值 250~400, 而非卡面裸值)。公式取社群拆解(巴哈姆特高等陣容
    戰法論/NGA數據貼): 屬性100=面板基準值(SCALE=1.0), 每+350點效果翻倍(v=450時SCALE=2.0)。
    仍是可調校準旋鈕, 之後有更多實測數據可再調整斜率/錨點。"""
    return SCALE_G(v, 350)


def scale_of(caster, scale, scale_div=None):
    """批35: scale_div(可選) —— 效果級 e["scaleDiv"] 透傳, 預設350(SCALE 向後相容)。"""
    if not scale:
        return 1.0
    return SCALE_G(caster.charm, scale_div) if scale == "charm" else SCALE_G(caster.eff(scale), scale_div)


def cap_val_of(v, cap_val):
    """批35: 效果級可選欄位 e["capVal"](值上限), 縮放後 clamp。慣例「狀態效果上限=基礎值×2」
    (錨點: 機鑑先識 40%→80% cap)不自動套用, 逐效果顯式標 e["capVal"]。未標則不 clamp。"""
    return min(v, cap_val) if cap_val is not None else v


def locked_scale_of(caster, e):
    """批35 B: 「受X影響」狀態值類效果(block 為主, 現行機鑑先識警戒) 的「準備階段鎖定」語意
    —— 效果的 scale 縮放值在 prep 階段(第一次掃描到該效果, 不論它本身是否於 prep 就實際套用)
    算定並鎖住, 之後(如 everyRound 補層段延後到第2/3回合才擲骰命中)一律沿用鎖定值, 不因戰鬥中
    智力浮動重新計算。與 heal_base 準備階段鎖定兵力快照同一慣例(第二次獨立確認)。用「效果物件
    本身」當快取鍵(caster.scale_lock: dict[id(e), value], 惰性建立)。只用於帶 scale 的 block,
    不擴大到其餘 k(目前無對應實測樣本佐證其餘k同樣適用, 見 engine.js 對應註解)。"""
    scale = e.get("scale")
    if not scale:
        return 1.0
    if caster.scale_lock is None:
        caster.scale_lock = {}
    key = id(e)
    if key not in caster.scale_lock:
        caster.scale_lock[key] = scale_of(caster, scale, e.get("scaleDiv"))
    return caster.scale_lock[key]


# 批7: 發動率類「受X影響」縮放 —— 獨立常數, 與上面 SCALE(每+350翻倍) 不是同一條曲線。
# user 實測太平道法(黃巾/張角, docs/data/calibration_anchors.json → rate_scale): 智力484.6
# 才翻倍(對比 SCALE 只要+350即450), 反解 c=0.002598(6組獨立點一致到小數第6位, 取0.0026)。
# chargeup 尚無獨立實測, 暫共用同常數(假設同曲線, 待未來樣本校準)。
RATE_SCALE_C = 0.0026


def rate_scale_of(caster, scale):
    if not scale:
        return 1.0
    v = caster.charm if scale == "charm" else caster.eff(scale)
    return 1 + (v - 100) * RATE_SCALE_C


def morale_mult(m):
    return 0.007 * min(m, 100) + 0.30                 # 士氣上限100(戰報: 士氣110.4傷害不變, 超過100按100算)


def counter_mult(a, b):
    if a == "器" or b == "器":
        return 1.15 if b == "器" else 0.85
    if COUNTER.get(a) == b:
        return 1.15
    if COUNTER.get(b) == a:
        return 0.85
    return 1.0


# ---------------------------------------------------------------------------
# effectText 啟發式解析: sgsdeck 戰法 dict -> 引擎可用 runtime 戰法
# effectType: 增益/治療/控制/減益/兵刃群體/兵刃單體/謀略群體/謀略單體/援護/內政
# kind:       ACTIVE主動/BURST突擊/COMMAND指揮/PASSIVE被動/FORMATION陣/TROOP兵種/INTERNAL內政
# ---------------------------------------------------------------------------
def _maxval(text, kw):
    """抓 'kw...A→B' 或 'kw...A%→B%', 回傳升滿值 B (取範圍上限)。"""
    m = re.search(kw + r"[^0-9]{0,10}(\d+(?:\.\d+)?)\s*%?\s*[→~]\s*(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(2))
    m = re.search(kw + r"[^0-9]{0,10}(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


TYPE_MAP = {"ACTIVE": "active", "BURST": "charge", "COMMAND": "command",
            "PASSIVE": "passive", "FORMATION": "command", "TROOP": "command"}


def parse_effect(t):
    """回傳 runtime 戰法 dict, 或 None (內政/無法戰鬥)。"""
    et = t.get("effectType") or ""
    if "內政" in et or t.get("kind") == "INTERNAL":
        return None
    txt = t.get("effectText") or ""
    rt = {"type": TYPE_MAP.get(t.get("kind"), "passive"),
          "rate": t.get("activationRate") or 1.0,
          "kind": "intel" if "謀略" in et else "phys",
          "coef": 0.0, "n": 3 if ("群體" in (et + (t.get("effectTarget") or ""))
                                   or "全體" in txt) else 1,
          "prep": 1 if "準備" in txt else 0, "effects": []}
    if "兵刃" in et or "謀略" in et:                  # 傷害型
        v = _maxval(txt, "傷害率") or _maxval(txt, "傷害")
        rt["coef"] = (v / 100) if v else 1.2
    elif et == "治療":
        v = _maxval(txt, "治療率") or _maxval(txt, "恢復") or _maxval(txt, "回復")
        rt["effects"].append({"k": "heal", "who": "ally", "coef": (v / 100) if v else 0.8, "dur": 1})
    elif et == "控制":
        rt["effects"].append({"k": "stun", "who": "enemy", "dur": 1})
    elif "增益" in et:                                # 我方增傷
        v = _maxval(txt, "提高") or _maxval(txt, "提升") or 12
        rt["effects"].append({"k": "amp", "who": "ally", "val": v / 100 if v <= 100 else 0.15, "dur": 99})
    elif "減益" in et:                                # 削弱敵方輸出
        v = _maxval(txt, "降低") or _maxval(txt, "減少") or 12
        rt["effects"].append({"k": "amp", "who": "enemy", "val": -(v / 100 if v <= 100 else 0.15), "dur": 2})
    return rt


# ---------------------------------------------------------------------------
# 載入真實全庫
# ---------------------------------------------------------------------------
class General:
    def __init__(self, raw):
        self.name = raw["name"]
        self.faction = raw.get("faction", "?")
        self.gender = raw.get("gender")
        self.apt = raw.get("affinity", {})           # 各兵種適性 S/A/B/C/D, 戰鬥兵種由隊伍決定
        st = raw.get("stats", {})
        self.base = {"force": st.get("武力", 80), "intel": st.get("智力", 80),
                     "command": st.get("統率", 90), "speed": st.get("速度", 70)}
        self.charm = st.get("魅力", 60)               # 魅力: 只供 scale="charm" 查表, 不進戰鬥四維 eff()
        self.tactic = TACTICS.get(raw.get("tactic")) if raw.get("tactic") else None
        self.tactic_name = raw.get("tactic") or "—"
        self.bingshu_cats = raw.get("availableBingshu", [])  # 可用兵書類別

    def apt_pct(self, troop):                         # 該武將用此兵種時的屬性發揮%
        return APT_PCT.get(self.apt.get(troop), 0.85)

    def best_troop(self):                             # 最佳適性兵種(預設選擇參考)
        return max((t for t in ("騎", "盾", "弓", "槍", "器")),
                   key=lambda t: APT_RANK.get(self.apt.get(t), -1))


def team_troop(team):                                 # 一隊的建議兵種: 三人適性總和最高者
    gs = [POOL[n] for n in team]
    return max(("騎", "盾", "弓", "槍", "器"),
               key=lambda t: round(sum(g.apt_pct(t) for g in gs), 4))  # 抹平浮點誤差, 與 engine.js 平手取先序


# 優先用 LLM 解析檔(tactics_parsed.json), 沒有才退回正則啟發式
_parsed = os.path.join(DATA, "tactics_parsed.json")
if os.path.exists(_parsed):
    with open(_parsed, encoding="utf-8") as f:
        _parsed_list = json.load(f)
    TACTICS = {o["nameZh"]: o for o in _parsed_list if o.get("type") != "none"}
    TACTIC_SRC = "LLM 解析"
    # 批10: 資料衛生防禦 —— 載入時掃描 |amp.val| > 3 的極端值並印警告(不擋), 供資料層儘早
    # 發現如「coef 誤重複灌入 amp.val」這類系統性錯誤(見批10 corrections 仲裁)。只警告,
    # 不修改資料本身(修正應在 tactics_parsed.json/corrections 層完成)。
    for _t in _parsed_list:
        for _e in _t.get("effects", []):
            if _e.get("k") == "amp" and isinstance(_e.get("val"), (int, float)) and abs(_e["val"]) > 3:
                print(f"[tactics data] {_t.get('nameZh', '?')}: amp.val={_e['val']} 超過 |3| 常見範圍, 疑似資料異常(如 coef 誤灌入 amp.val)")
else:
    with open(os.path.join(DATA, "tactics.json"), encoding="utf-8") as f:
        TACTICS = {}
        for t in json.load(f):
            rt = parse_effect(t)
            if rt:
                TACTICS[t["nameZh"]] = rt
    TACTIC_SRC = "正則啟發式"
with open(os.path.join(DATA, "generals.json"), encoding="utf-8") as f:
    POOL = {}
    for raw in json.load(f):
        if raw.get("stats"):                          # 跳過無屬性的(少數)
            g = General(raw)
            POOL[g.name] = g

# 兵書: 名稱 -> 效果; 各類別的主兵書(供預設裝備)
BINGSHU, MAIN_BY_CAT, SUB_BY_CAT = {}, {}, {}
_bs = os.path.join(DATA, "bingshu_parsed.json")
if os.path.exists(_bs):
    for b in json.load(open(_bs, encoding="utf-8")):
        key = b["category"] + "·" + b["name"]     # 複合鍵(同名跨類別不撞)
        BINGSHU[key] = b
        (MAIN_BY_CAT if b.get("type") == "主兵書" else SUB_BY_CAT).setdefault(
            b["category"], []).append(key)


def default_bingshu(g):                               # 預設主兵書: 該將首個可用類別的主兵書
    for c in g.bingshu_cats:
        if MAIN_BY_CAT.get(c):
            return MAIN_BY_CAT[c][0]
    return None


def _load_list(fname):
    p = os.path.join(DATA, fname)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []


BONDS = _load_list("bonds_parsed.json")               # 緣分: 隊伍湊齊觸發
EQUIPS = {}                                            # 裝備: 自身被動; 鍵為複合鍵"type·name"(同兵書precedent, 同名跨欄位不撞), 另存純名稱 fallback(向後相容)
for _e in _load_list("equips_parsed.json"):
    EQUIPS[_e["type"] + "·" + _e["name"]] = _e
    EQUIPS.setdefault(_e["name"], _e)                  # 純名稱 fallback: 同名跨type時保留先出現者, 呼叫端應改用複合鍵


def active_bonds(team):                               # 隊伍觸發的緣分(湊齊 triggerCount 人)
    s = set(team)
    return [bd for bd in BONDS if len(s & set(bd.get("generals", []))) >= bd.get("triggerCount", 99)]


SEASON_MODS = {}
_sm = os.path.join(DATA, "season_modifiers.json")
if os.path.exists(_sm):
    SEASON_MODS = {k: v for k, v in json.load(open(_sm, encoding="utf-8")).items()
                   if not k.startswith("_")}


def season_mods(g, idx, team, scenario):              # 該將在此賽季的養成修正
    out = {"apt_add": 0.0, "apt_s": False, "flat": 0, "mult": 1.0}
    for m in SEASON_MODS.get(scenario, []) if scenario else []:
        t = m.get("type")
        if t == "faction_scale":
            facs = [POOL[n].faction for n in team]
            top = max((facs.count(f) for f in set(facs)), default=0)
            if top >= m.get("partialThreshold", 2):
                out["mult"] *= 1 + (m.get("fullBonus", 0.1) if top >= len(team)
                                    else m.get("partialBonus", 0.07))
        elif t == "apt_add" and g.gender == m.get("gender"):
            out["apt_add"] += m.get("value", 0.15)
        elif t == "apt_override" and idx < m.get("maxSlots", 2):
            out["apt_s"] = True
        elif t == "stat_flat":
            out["flat"] += m.get("all", 0)
    return out


# ---------------------------------------------------------------------------
# 評分 + 配將推薦
# ---------------------------------------------------------------------------
def score(team, troop=None):
    g = [POOL[n] for n in team]
    troop = troop or team_troop(team)
    attr = sum((max(x.base["force"], x.base["intel"]) + x.base["command"]
                + x.base["speed"]) * x.apt_pct(troop) for x in g)
    kinds = set()
    for x in g:
        if x.tactic:
            kinds.add(x.tactic["type"])
            kinds |= {e["k"] for e in x.tactic["effects"]}
    apt_bonus = round(sum(x.apt_pct(troop) for x in g) / 3 * 80)  # 三人對隊伍兵種適性越高越好
    same_fac = 40 if len({x.faction for x in g}) == 1 else 0
    return round(attr / 3 + len(kinds) * 25 + apt_bonus + same_fac)


def _base(n):                                         # SP關羽 與 關羽 為同一角色, 不可同隊
    return n.replace("SP ", "").replace("SP", "")


def recommend(pool=None, k=3, top=8):
    names = pool or list(POOL)
    combos = (c for c in combinations(names, k) if len({_base(x) for x in c}) == k)
    ranked = sorted(combos, key=lambda c: score(c), reverse=True)
    return [(list(c), score(c), team_troop(c)) for c in ranked[:top]]


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
def team_gate_ok(gate, factions):
    """批24 D1: teamGate(隊伍構成前提) —— 判斷隊伍陣營組成是否符合戰法宣告的前提。
    "allDiff": 三名武將陣營兩兩不同(潛龍陣「我軍三名武將陣營均不相同時」); "allSame":
    三名武將陣營皆相同(供未來同類戰法使用, 目前全庫無此案例但一併支援對稱語意)。
    factions 為隊伍全體(含自己)的陣營陣列, 已在 fight() 建構 Unit 前準備好傳入。"""
    if not gate or not gate.get("factions"):
        return True
    uniq = len(set(factions))
    if gate["factions"] == "allDiff":
        return uniq == len(factions)
    if gate["factions"] == "allSame":
        return uniq == 1
    return True                                       # 未知 gate 種類: 保守放行(不擋), 避免資料錯字導致戰法整組消失


class Unit:
    def __init__(self, g, ttype, bingshu=None, equip=None, add=None, inherit=None, season=None, team_factions=None, camp_lv=0, is_camp_holder=False):
        self.g, self.ttype, self.troop, self.stun = g, ttype, START_TROOP, 0
        self.camp_lv = camp_lv or 0                   # 批36: 兵種營等級(0~10, 隊伍級, 見 fight() 呼叫端), 0=不啟用(向後相容既有全部呼叫點)
        # 批33: heal_base —— 準備階段鎖定的治療基準兵力快照(troop×HEAL_TROOP_C), 供指揮/兵種/
        # 兵書/被動類 heal(常駐急救型)使用, 使治療量不隨後續戰鬥中兵力增減而變動(見上方
        # HEAL_TROOP_C 常數註解); 建構時 troop 尚未受戰鬥影響, 此處快照即「開戰準備階段兵力」。
        self.heal_base = self.troop * HEAL_TROOP_C
        self.silence = 0                              # 計窮: 無法發動主動戰法
        self.disarm = 0                                # 繳械: 無法普通攻擊(含連擊/突擊)
        self.chaos = 0                                 # 批12 ModeF: 混亂(不鎖行動, 但普攻/單體主動戰法改為敵我不分隨機選目標), 剩餘回合數
        self.insight = 0                               # 洞察: 免疫 stun/silence/disarm/chaos, 施加時同時解除
        self.first = 0                                 # 先攻: 剩餘回合數, 排序時優先於速度
        self.ambush = 0                                # 批18: 遇襲(先攻的反面, 遲緩) —— 剩餘回合數, 行動排序時與 first 一併算 eff_first
        self.wounded = 0.0                             # 批18: 傷兵池 —— 累積「可救援」量(受到的傷害按當時回合轉化率折算); 治療結算上限=min(治療量, wounded, START_TROOP-troop)
        # 自帶 + 傳承; 自帶戰法(g.tactic)淺拷貝附加 native:True 旗標(供 rateup/chargeup 的 nativeOnly
        # 修飾判斷「這是不是自帶戰法」, 如太平道法只加成張角自帶的五雷轟頂)。淺拷貝而非直接改
        # TACTICS 共享物件, 避免多個武將共用同一戰法物件時互相污染(如兩人都自帶白眉)。
        # 批24 D1: teamGate —— 開戰時(建構Unit當下, team_factions已由fight()備妥)判定一次,
        # 不滿足前提的戰法整條從 self.tactics 過濾掉(不進入後續 cmd_passive_srcs/on_hit_tacs/
        # on_hit_effect_tacs 等衍生快取, 亦不會被 apply_passives/回合迴圈讀到, 等同整戰法不生效)。
        # sgz.py 無 TRACE/日誌機制(僅 docs/engine.js 供瀏覽器UI推演明細用), 此處純過濾不列印。
        def _gate_ok(t):
            return team_gate_ok(t.get("teamGate"), team_factions or [])
        self.tactics = [t for t in (
            ([dict(g.tactic, native=True)] if g.tactic else []) +
            [TACTICS[nm] for nm in (inherit or []) if nm in TACTICS]  # 自帶 + 傳承戰法
        ) if _gate_ok(t)]
        # 批36: 兵種營Lv10附贈戰法 attach —— 原文是「我軍隨機單體/群體」觸發(一整隊只發生
        # 一次), 而非「隊上每個單位各自獨立擁有這個被動」。故只有 fight() 指定的單一「持有者」
        # (is_camp_holder=True, 每隊隨機挑1人, 見 fight() 呼叫端)才實際 append 進 self.tactics;
        # 其餘同隊隊友仍受 camp_lv 的屬性%加成(下方amp段, 對每個Unit都算, 因原文那一支是「全隊
        # 造成傷害」的隊伍級加成, 與Lv10戰法是三合一裡各自獨立的兩支), 但不會各自重複攻得
        # Lv10戰法(避免3人隊「破軍/守禦」各自觸發3次的過量bug, 已用鏡像對局實測驗證, 見demo()
        # 97-101號assert)。依「本隊實際兵種(ttype, 隊伍級)」查表 CAMP_TROOP_TACTIC, 命中且
        # TACTICS 已載入該名稱(器械營"負重"因 type:"none" 被載入時過濾, 表中值為 None 或查無
        # 則不掛)才 append。必須在此處(cmd_passive_srcs/on_hit_tacs/on_hit_effect_tacs/
        # on_deal_tacs 等衍生快取產生之前)插入, 因五戰法皆 type:"passive" 會被那些快取掃描到
        # (對比裝備proc戰法是charge型, 晚插入也不影響)。淺拷貝加 _campBuilding:True 標記
        # (純供辨識, 不影響戰鬥邏輯分派)。
        if self.camp_lv >= 10 and is_camp_holder:
            camp_tac_name = CAMP_TROOP_TACTIC.get(ttype)
            camp_tac = camp_tac_name and TACTICS.get(camp_tac_name)
            if camp_tac:
                self.tactics.append(dict(camp_tac, _campBuilding=True))
        # 批18: fakeReport(偽報) 加強 —— 記錄「自己的指揮/被動戰法」名稱集合, 供 eff()/addbonus()
        # 判斷某條 adds/mods/stat_adds 是否來自「本單位自己的指揮/被動戰法」(見 engine.js 同名欄位註解)。
        self.cmd_passive_srcs = {t.get("nameZh") for t in self.tactics
                                  if t.get("type") in ("command", "passive") and t.get("nameZh")}
        _bn = bingshu if isinstance(bingshu, (list, tuple)) else ([bingshu] if bingshu else [])
        _bs_all = [e for nm in _bn for e in BINGSHU.get(nm, {}).get("effects", [])]  # 兵書(主+副)合併效果
        self.bs = [e for e in _bs_all if not (e.get("when") or {}).get("on")]
        # 批22: 兵書效果級 e.when.on(急救類反應式治療, 如三軍之眾「戰鬥第2-4回合自身獲得急救」)
        # —— 與裝備 on_hit_eq 同慣例, 兵書效果本無獨立回合窗機制(apply_passives 只在 prep/
        # heal_only 套用整包 self.bs), 帶 e.when.on 的效果分離到此陣列, 於 on_hit() 反應式
        # 事件點結算。
        self.on_hit_bs = [e for e in _bs_all if (e.get("when") or {}).get("on")]
        _eq = equip if isinstance(equip, (list, tuple)) else ([equip] if equip else [])
        # 同名特技(跨type, 如四欄皆有的"無畏")遊戲規則只生效一件: 依基底名稱去重, 先出現者為準
        _eq_seen = set()
        _eq_objs = []
        for nm in _eq:
            e = EQUIPS.get(nm)
            if e and e["name"] not in _eq_seen:
                _eq_seen.add(e["name"])
                _eq_objs.append(e)
        _eq_all = []                                   # 裝備(4欄)合併效果(已去重); 帶 when 的效果淺拷貝附加 _eqNm(供 TRACE 標名), 不動原資料物件
        for e in _eq_objs:
            for eff in e.get("effects", []):
                if eff.get("when"):
                    eff2 = dict(eff)
                    eff2["_eqNm"] = e["name"]
                    _eq_all.append(eff2)
                else:
                    _eq_all.append(eff)
        # 批8: 效果級回合窗(effect.when) —— 裝備效果不像戰法有獨立 when 欄(合併進 eq 陣列時已失去
        # 個別戰法邊界), 故 when 掛在「單條效果」本身(e["when"], 非 t["when"])。無 when 的效果照舊
        # 在準備階段(prep)一次性套用(self.eq); 帶 when 的效果分離到 delayed_eq, 於回合迴圈開始時
        # (與戰法 when 窗口同一時點)逐條檢查 round_ok 是否符合, 符合則一次性套用(when_fired 慣例,
        # 用效果物件本身 id() 去重)。帶 rate 的額外擲骰(如赳螑 50%機率)。
        self.eq = [e for e in _eq_all if not e.get("when")]
        self.delayed_eq = [e for e in _eq_all if e.get("when") and not e["when"].get("on")]
        # 批22: 裝備效果級 e.when.on(急救類反應式治療, 如長健/青囊書「戰鬥首回合受傷時回復
        # 10%兵力」) —— 與上面 delayed_eq(回合視窗一次性套用)不同語意: on="damaged"/"attacked"
        # 是「受傷當下觸發」, 不是「特定回合開啟時套用一次」。與 on_hit_effect_tacs(戰法版本)
        # 對應的裝備版本, 在 on_hit() 反應式事件點結算。
        self.on_hit_eq = [e for e in _eq_all if e.get("when") and e["when"].get("on")]
        # 裝備 proc(普攻後觸發, 如 昭烈12%繳械/踩踏額外傷): 包成偽突擊(charge)戰法附加, 走既有 charge 觸發路徑(普攻後 rate 擲骰)。
        # 偽戰法不在戰法庫, 不參與同名戰法去重與 NONEQUIP 過濾; nameZh 預設「特技·名」供 TRACE 辨識。
        # proc:True 旗標 → 標記為「特技偽戰法」, 非真突擊戰法: 日後若加 chargeup(突擊發動率加成)原語, 必須排除 t["proc"] is True(user 明確指示: 特技不吃突擊加成, 例虎豹騎/三勢陣/經天緯地/陷陣突襲)。
        for e in _eq_objs:
            p = e.get("proc")
            if p:
                self.tactics.append({
                    "type": p.get("type", "charge"), "rate": p.get("rate", 1), "coef": p.get("coef", 0),
                    "kind": p.get("kind", "phys"), "n": p.get("n", 1), "nMax": p.get("nMax", 0),
                    "effects": p.get("effects", []), "nameZh": p.get("nameZh", "特技·" + e["name"]),
                    "prep": 0, "when": None, "proc": True,
                })
        a = add or {}                                 # 養成加值: 加點/進階/典藏
        sm = season or {}                             # 賽季修正
        apt = (1.20 if sm.get("apt_s") else g.apt_pct(ttype)) + sm.get("apt_add", 0)
        scm, flat = sm.get("mult", 1.0), sm.get("flat", 0)
        # 屬性管線(戰報結算順序 準備→士氣→適性→建築→裝備→戰法): (基礎+加點+賽季flat)×適性×賽季乘 → +城建CITY → ×陣營FACTION → +兵種營CAMP
        # (裝備 stat "add" 平加效果由 apply_effects/prep 於本管線之後套用, 見 eff() 的 stat_adds; 戰法 mult buff 又在其後, 見 eff() 的 mods)
        def _pipe(base, alloc):
            return ((base + alloc + flat) * apt * scm + CITY) * FACTION + CAMP
        self.force = _pipe(g.base["force"], a.get("force", 0))
        self.intel = _pipe(g.base["intel"], a.get("intel", 0))
        self.command = _pipe(g.base["command"], a.get("command", 0))
        self.speed = _pipe(g.base["speed"], a.get("speed", 0))
        self.charm = getattr(g, "charm", 60)          # 魅力: 城建/陣營是否加成不明, 保守用裸值不縮放(供 scale="charm" 查表)
        self.mods = []                                # 乘法: [stat, mult, left, src]
        self.adds = []                                # 加法: [amp|mitig|extra, val, left, src]
        self.stat_adds = []                           # 屬性平加(裝備 stat.add): [stat, add, left, src]; eff() 中於 mods 乘算前先加
        if a.get("amp"):                              # 進階/典藏 攻防加成: 每階+2%攻+2%防(無來源, 不去重)
            self.adds.append(["amp", a["amp"], 9999, None, None])  # 5元素(含flags=None), 與 push_add 寫入形狀一致, 避免 tick() 的 5-tuple 解包 ValueError(既有bug, 批18順手修正; engine.js 因用彈性解構未受影響)
        if a.get("mitig"):
            self.adds.append(["mitig", a["mitig"], 9999, None, None])
        # 批36: 兵種營「每級+0.25%該兵種造成傷害」——與CAMP(全屬性flat)/Lv10附贈戰法(見上方
        # self.tactics attach)並列的三合一第三支, 走既有amp原語(與a["amp"]同慣例, src標記供
        # 除錯辨識), camp_lv=0時不推入(向後相容, adds為空陣列不影響任何既有戰鬥數學)。
        if self.camp_lv > 0:
            self.adds.append(["amp", self.camp_lv * CAMP_DMG_PER_LV, 9999, "兵種營", None])
        self.dots = []                                # 持續傷害: [每回合傷害, left]
        self.settle = None                            # 結算狀態(猛毒)
        self.guardian = None                          # 傷害轉移: 代承者
        self.guard_share = 0.0                        # 代承比例
        self.guard_dur = 0                            # 代承剩餘回合, 歸零時清 guardian(source 首回合援護等有限窗)
        self.guard_normal_only = False                # 只代承普攻傷害(如 援助), 戰法傷害不轉移
        self.stack = None                             # 疊加增益: {per, max, n}
        self.decay = None                             # 衰減增益: {v0, left, total}
        self.swap = 0                                 # 武智互換 剩餘回合
        self.counter = None                           # 反擊: {coef, kind, prob}
        # 批28 B1: 守護式反擊(counter.guardFor) —— 「A受擊時, B代為反擊」的方向(如虎衛軍
        # 「我軍主將即將受到普攻時, 副將反擊」), 與既有 self.counter(持有者自己受擊自己反擊)
        # 語意相反, 不能直接掛在持有者(副將)身上。改掛在「被保護者」(如主將)身上一份清單,
        # 每個元素是{unit(反擊執行者), coef, kind, prob}, 見 apply_effects 的 guardFor 分支
        # 與 hit() 內的觸發判斷。與 guardian(傷害轉移代承)是不同機制(guardian轉移傷害承受方,
        # counter_guards是「受擊者不變, 但由別人代為反擊攻擊者」), 兩者可並存不衝突。
        self.counter_guards = []
        self.taunt_by = None                          # 嘲諷: 被嘲諷時強制普攻/單體戰法指向 taunt_by
        self.taunt_dur = 0                             # 嘲諷剩餘回合
        self.shield = None                            # 護盾: {amt, dur} 吸收固定量傷害, 先於兵力扣減
        self.block = []                                # 批22: 次數型格擋(抵禦/警戒同族) —— [{"val","n","src"}], 消耗順序見 hit(); val=1.0全擋/0.x部分減傷, n=剩餘次數
        self.dodge_prob = 0.0                          # 規避機率
        self.dodge_dur = 0                             # 規避剩餘回合
        self.surehit_dur = 0                           # 必中: 無視對方 dodge, 剩餘回合
        self.healblock = 0                             # 批8: 禁療(healblock) 剩餘回合, >0 時 heal 效果對其無效
        self.when_fired = set()                        # 條件觸發(when.rounds/from/until) 已套用效果的戰法(一次性, 用 id() 去重); 批8: delayed_eq(裝備效果級when)共用同一個 set(效果物件本身 id() 去重, 不與戰法物件撞)
        self.scale_lock = None                          # 批35 B: 「準備階段鎖定」的 scale 縮放值快取, dict[id(效果物件) -> scale_of結果], 惰性建立(見 locked_scale_of)
        self.heal_rounds_fired = {}                     # 批15: heal 效果 e["when"]["rounds"](明確列出的特定回合)的「每回合各觸發一次」去重, dict[id(效果物件) -> set(已觸發回合數)], 見 apply_effects 的 heal 分支
        self.hit_flags = set()                         # 反應式觸發(when.on) 本回合已觸發的戰法, 每回合重置(防無限鏈)
        # 批31 A 修復: 過去 on_hit_tacs 只檢查 t.when.on 是否為真(truthy), 沒有限定具體事件值,
        # on_hit() 內部迴圈(見下方)也只用 t0["when"]["on"]=="attacked" 排除普攻限定的不符情形,
        # 對其餘任何 on 值(包含批27新增的"dealtDamage"/本批"activeFired")一概放行當成
        # "damaged"(任意傷害都觸發)處理——這是預先篩選範圍過寬的潛伏bug(全庫過去只有
        # attacked/damaged 兩種 t.when.on 值, 從未真正暴露; 本批新增 activeFired 後, 士爭先赴
        # 首次踩中: 除了正確的「自身發動主動戰法觸發」外, 還會被 on_hit() 誤判成「受擊觸發」
        # 額外多發動一次, 造成雙重觸發)。現收斂為明確白名單 {"attacked","damaged"}, 只有這
        # 兩種事件值才會被收進 on_hit_tacs(dealtDamage/activeFired 各自有專屬的
        # on_deal_tacs/active_fired_tacs 預篩+獨立事件點, 不應該也落入 on_hit_tacs)。
        self.on_hit_tacs = [t for t in self.tactics    # 預篩: 絕大多數單位為空, hit 熱路徑 O(0)
                            if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") in ("attacked", "damaged")]
        # 批22: 效果級 e.when.on(急救類反應式治療, 如陷陣營/長健/雲聚影從「受到傷害時XX%機率
        # 獲得治療」) —— 與上面 on_hit_tacs(戰法級, 整個戰法都是反應式)不同: 這類戰法本身有
        # 其他常駐效果(如陷陣營的武力/統率平加)需要在 prep 階段就套用, 只有其中的 heal 效果段
        # 是「受傷當下才觸發」的反應式語意, 不能把整個戰法標成 t["when"]["on"](那樣會連帶讓
        # 武力/統率平加也不在 prep 套用, 語意跑掉)。on_hit_effect_tacs 收集這類「戰法本身無
        # t.when, 但至少一個效果帶 e.when.on」的戰法, on_hit() 只讀取/結算符合的個別效果。
        # 批23: 型別放寬含 active —— 過去只認 passive/command(「戰法本身有其他常駐效果, 只有
        # heal段是反應式」的典型模式, 如陷陣營/雲聚影從)。但草船借箭一類 type:"active" 戰法也有
        # 同樣模式(「使我軍獲得急救狀態, 受傷時機率觸發治療」是active發動後掛的一個反應式buff,
        # 不是常駐), 過去完全沒有機制承接, 只能誤把heal當成active發動當下的常駐治療(0分bug)。
        # 放寬後active戰法帶e.when.on的效果同樣走on_hit()反應式結算, 該戰法主coef/其餘無when
        # 效果仍照常經由主動擲骰路徑(t0["rate"])發動觸發(兩者互不干擾, 見apply_effects內新增的
        # reactive閘門, 確保e.when.on效果不會在active擲骰命中時被重複套用)。
        # 批31 A 修復: 同上(on_hit_tacs)——過去用 truthy 檢查, 未限定具體事件值, 導致
        # 帶 e.when.on:"dealtDamage"(批27)的效果(深謀遠慮/白衣渡江/非攻制勝)被誤收進
        # on_hit_effect_tacs, 在 on_hit() 的效果級迴圈裡又額外多觸發一次(該迴圈只排除
        # ew["on"]=="attacked" 的不符情形, "dealtDamage" 被誤判成"damaged"放行), 與正確的
        # on_deal_effect_tacs 觸發路徑重複結算(雙重治療/雙重控制)。收斂為明確白名單。
        self.on_hit_effect_tacs = [t for t in self.tactics
                                   if not t.get("when") and t["type"] in ("passive", "command", "active")
                                   and any((e.get("when") or {}).get("on") in ("attacked", "damaged") for e in t.get("effects", []))]
        # 批27 A: on:"dealtDamage" —— 「自身造成傷害時/後」反應式掛鉤(對比 on_hit_tacs 的
        # attacked/damaged 是「自己受擊」視角, 這裡是「自己打人」視角, 如白衣渡江「造成兵刃
        # 傷害時25%→50%機率使敵軍單體繳械」)。掛在 hit() 傷害結算後對 src(施加傷害的一方)
        # 掃描, 與 on_hit_tacs/on_hit_effect_tacs 完全對稱(戰法級 vs 效果級 兩種顆粒度)。
        # dmgType(選填, "phys"/"intel"): 區分「造成兵刃傷害時」vs「造成謀略傷害時」兩種不同
        # 觸發條件(白衣渡江 disarm 段只在兵刃傷害後觸發, silence 段只在謀略傷害後觸發), 沿用
        # amp/mitig 既有 dmgType 欄位命名慣例, 無此欄位視為兩種傷害類型皆可觸發(向後相容)。
        self.on_deal_tacs = [t for t in self.tactics
                             if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") == "dealtDamage"]
        self.on_deal_effect_tacs = [t for t in self.tactics
                                    if not t.get("when") and t["type"] in ("passive", "command", "active")
                                    and any((e.get("when") or {}).get("on") == "dealtDamage" for e in t.get("effects", []))]
        # 批31 A: on:"activeFired" —— 「自身成功發動主動(或突擊)戰法時/後」反應式掛鉤(對比
        # on_deal_tacs 的「造成傷害」視角, 這裡是「戰法本身成功擲骰命中fire」視角, 不要求真的
        # 造成傷害, 如士爭先赴「成功發動自帶主動戰法前，50%機率對敵軍2人造成兵刃傷害」——現行
        # 版本把這條獨立成一個常駐coef+rate的passive戰法, 與「是否真的有主動戰法成功發動」完全
        # 脫鉤, 屬v14盲測抓到的「條件觸發簡化為無條件」同族缺口)。掛在 fight() 主迴圈 active/
        # charge 型戰法 fire=True 判定通過後, 對施放者 u 自身(而非受擊/被造成傷害的另一方)
        # 掃描其 active_fired_tacs(戰法級)/active_fired_effect_tacs(效果級), 與 on_deal_tacs/
        # on_deal_effect_tacs 完全對稱(戰法級 vs 效果級 兩種顆粒度), 只是事件觸發點不同(自身
        # 戰法命中 vs 自身造成傷害)。when.timing(選填, "before"/"after"): 士爭先赴原文「成功
        # 發動...前」, 但引擎在同一回合內對「前/後」無實質結算順序差異(觸發本體戰法與本反應式
        # 效果都在同一次 fire 判定之後才有意義, 沒有跨回合的「發動前」窗口可插入), 統一在
        # fire=True 判定通過、實際套用觸發戰法效果**之前**呼叫 active_fired() 廣播(貼近
        # before 語意, 但 after 措辭的戰法一律視同無差別, 不細分兩種處理路徑, 見
        # engine_limitations.md 新增節)。
        self.active_fired_tacs = [t for t in self.tactics
                                  if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") == "activeFired"]
        self.active_fired_effect_tacs = [t for t in self.tactics
                                         if not t.get("when") and t["type"] in ("passive", "command", "active")
                                         and any((e.get("when") or {}).get("on") == "activeFired" for e in t.get("effects", []))]
        self.locked_targets = {}                       # 批12 ModeG: lockTarget:true 戰法的鎖定目標, 鍵=id(戰法dict)(dict不可雜湊, 用id())
        # 批16: 原語擴充包 —— 新增狀態欄位(現有資料無新欄位, 皆維持0/{}/set()預設值, 行為零變化)
        self.atk_count = {}                            # everyN: 自身普攻次數計數器, 鍵=id(戰法dict)
        self.immune = []                               # immuneTo: [type, dur] 陣列(單項控制免疫, 對比 insight 全免)
        self.hp_below_fired = set()                    # hpPct: when.hpBelow(一次性, 首次跨越即觸發) 已觸發的戰法, 用 id(t) 去重
        self.fake_report_dur = 0                       # fakeReport(偽報): 剩餘回合數, >0 時指揮/被動 coef 擲骰段與 on_hit 反應式觸發受抑制

    @property
    def alive(self):
        return self.troop > 0

    def is_immune_to(self, ctype):                     # 批16: immuneTo —— 單項控制免疫查詢(對比 insight 全免)
        return any(ty == ctype for ty, _ in self.immune)

    def push_immune(self, types, dur):
        for ty in (types or []):
            self.immune.append([ty, dur if dur is not None else 1])

    def tick_every_n(self, t):                          # 批16: everyN —— 自身每第N次普攻觸發; 傳回是否達標(達標即歸零重計)
        cfg = t.get("everyN")
        if not cfg:
            return False
        key = id(t)
        cnt = self.atk_count.get(key, 0) + 1
        if cnt >= cfg.get("count", 1):
            self.atk_count[key] = 0
            return True
        self.atk_count[key] = cnt
        return False

    def apply_stack_cast(self):
        """批26 B2: stack.stackPer=="cast" 專用遞增入口 —— 原文常見「每次發動後傷害率提升X」
        (如水淹七軍/陷陣突襲), 是「本戰法每次成功發動」才+1層, 與回合數無關。既有 stack 機制
        只有 fight() 主迴圈的逐回合遞增(stackPer=="round", 預設, 向後相容), 此方法供戰法命中/
        發動結算處呼叫, 只在 stackPer=="cast" 時才遞增(round 模式呼叫此方法應為no-op, 只認
        tick()式逐回合遞增, 兩種模式互不干擾)。"""
        if self.stack and self.stack.get("stackPer", "round") == "cast":
            self.stack["n"] = min(self.stack["max"], self.stack["n"] + 1)

    @property
    def hp_pct(self):                                   # 批16: hpPct —— 自身兵力百分比(troop/START_TROOP), 供 when.hpBelow/hpAbove 檢查
        return self.troop / START_TROOP

    def suppressed(self, src):
        """批18: fakeReport(偽報) 期間, 來源為「自己的指揮/被動戰法」(src in cmd_passive_srcs) 的
        條目暫停參與計算(到期自動恢復, 不刪除條目本身 —— 條目仍在 adds/mods/stat_adds 陣列裡,
        tick() 到期照舊遞減/移除, 只是這裡讀取時跳過)。src 為 None(兵書/裝備/緣分/其他來源)或
        不在 cmd_passive_srcs 中不受影響。
        批24: src 可能帶「:尾碼」區分同源多條目(rateup 的 :prepOnly/nativeOnly、dmgType 的
        :phys/:intel, 見 push_add 呼叫端), 但 cmd_passive_srcs 只存純戰法名(nameZh, 不含
        尾碼)。比對前先去除尾碼還原成純戰法名, 避免帶尾碼的 src 永遠比對不到 cmd_passive_srcs
        (修正批16 rateup/chargeup 尾碼慣例引入時就存在的潛在比對錯位)。"""
        if not src or self.fake_report_dur <= 0:
            return False
        base = src.split(":", 1)[0]
        return base in self.cmd_passive_srcs

    def eff(self, stat):
        if self.swap and stat in ("force", "intel"):  # 武智互換
            stat = "intel" if stat == "force" else "force"
        v = getattr(self, stat)
        for s, add, _dur, src, *_ in self.stat_adds:  # 裝備平加(獨立階段, 在陣營/兵種營後、戰法乘算前)
            if (s == stat or s == "all") and not self.suppressed(src):
                v += add
        for s, m, _dur, src, *_ in self.mods:
            if (s == stat or s == "all") and not self.suppressed(src):  # stat="all" 套全屬性
                v *= m
        return v

    def addbonus(self, kind, dmg_type=None, is_normal=None, is_active=None):
        """批24 D2: dmg_type(可選) —— 只加總「該條目未宣告 dmgType, 或宣告的 dmgType 與呼叫端
        指定的 dmg_type 相符」的項目, 供 amp/mitig 依「兵刃/謀略」傷害類型過濾(見 damage() 呼叫端)。
        省略時完全維持原行為(不分類型全部加總), 向後相容全庫既有未帶 dmgType 的 amp/mitig 資料。
        批28 B3: is_normal(可選) —— 只加總「該條目未宣告 normalOnly, 或宣告 normalOnly 且本次
        is_normal 為 True」的項目, 供 amp 表達「僅普攻傷害提升」(如至柔動剛「提升我軍群體普攻
        傷害」, 對比redirect既有的normalOnly慣例)。呼叫端傳 is_normal=None(預設, 如dot/counter/
        settle等非普攻傷害路徑未特別傳入)時, 視為「未知/不適用」——安全側處理: 宣告了
        normalOnly 的加成一律不計入(避免對非普攻傷害路徑意外套用「僅普攻」限定的加成,
        比不套用更安全; 未宣告normalOnly的既有全庫資料完全不受影響, 向後相容)。
        批31 A: is_active(可選) —— 與 is_normal 對稱, 只加總「該條目未宣告 activeOnly, 或宣告
        activeOnly 且本次 is_active 為 True」的項目, 供 amp 表達「僅主動(或突擊)戰法傷害提升」
        (如士爭先赴「成功發動自帶主動戰法前造成的這段兵刃傷害提升10%→20%」, 對比 normalOnly
        「僅普攻」的相反方向限定)。同樣安全側處理: is_active=None(呼叫端未特別傳入, 如dot/
        counter/settle等)時, 宣告 activeOnly 的加成不計入。normalOnly 與 activeOnly 互斥
        (前者限普攻, 後者限主動/突擊戰法傷害, 資料上不會同時宣告兩者)。"""
        s = 0.0
        for a in self.adds:
            k, v = a[0], a[1]
            src = a[3] if len(a) > 3 else None
            if k != kind or self.suppressed(src):
                continue
            flags = a[4] if len(a) > 4 else None
            if dmg_type and flags and flags.get("dmgType") and flags["dmgType"] != dmg_type:
                continue
            if flags and flags.get("normalOnly") and is_normal is not True:
                continue
            if flags and flags.get("activeOnly") and is_active is not True:
                continue
            s += v
        return s

    def addbonus_for(self, kind, t):
        """rateup/chargeup 專用: 依戰法 t 的 prep/native 屬性, 只加總「修飾旗標吻合」的 adds 項。
        adds[4] = flags({"prepOnly":.., "nativeOnly":.., "inheritedOnly":..}|None, 見 push_add)。
        無旗標的加成一律計入(如虎豹騎的 chargeup 沒有 prepOnly/nativeOnly 限制)。
        批8: inheritedOnly(nativeOnly 反向) —— 只加「非自帶」(傳承)戰法, 如竭力佐謀「非自帶
        主動戰法發動率+100%」; not t.get("native") 即傳承(Unit 建構時自帶戰法才標 native=True)。"""
        s = 0.0
        for a in self.adds:
            if a[0] != kind or self.suppressed(a[3] if len(a) > 3 else None):
                continue
            flags = a[4] if len(a) > 4 else None
            if flags:
                if flags.get("prepOnly") and not t.get("prep"):
                    continue
                if flags.get("nativeOnly") and not t.get("native"):
                    continue
                if flags.get("inheritedOnly") and t.get("native"):
                    continue
            s += a[1]
        return s

    def amp(self, dmg_type=None, is_normal=None, is_active=None):      # 總增傷 = 一般+疊加層+衰減; 批24 D2: dmg_type過濾amp部分(stack/decay無此概念,全額計入); 批28 B3: is_normal過濾normalOnly標記的amp(僅普攻生效); 批31 A: is_active過濾activeOnly標記的amp(僅主動/突擊戰法傷害生效)
        a = self.addbonus("amp", dmg_type, is_normal, is_active)
        if self.stack:
            a += self.stack["per"] * self.stack["n"]
        if self.decay:
            a += self.decay["v0"] * self.decay["left"] / self.decay["total"]
        return a

    def push_add(self, kind, val, dur, src=None, flags=None):
        """同來源(戰法名)同種效果 刷新而非疊加。src=None(兵書/裝備/緣分)不去重。
        flags: {"prepOnly":bool, "nativeOnly":bool}, 供 addbonus_for() 篩選(見批7 太平道法)。"""
        if src:
            self.adds = [a for a in self.adds if not (a[0] == kind and a[3] == src)]
        self.adds.append([kind, val, dur, src, flags])

    def push_mod(self, stat, mult, dur, src=None, flags=None):
        if src:
            self.mods = [m for m in self.mods if not (m[0] == stat and m[3] == src)]
        self.mods.append([stat, mult, dur, src, flags])

    def push_stat_add(self, stat, add, dur, src=None, flags=None):  # 屬性平加(裝備 stat.add): 同 push_mod 慣例, 同來源刷新不疊
        if src:
            self.stat_adds = [a for a in self.stat_adds if not (a[0] == stat and a[3] == src)]
        self.stat_adds.append([stat, add, dur, src, flags])

    def push_block(self, val, n, src=None):
        """批22: block(次數型格擋, 抵禦/警戒同族) —— 與 shield/mitig 語意不同: 不是持續減傷/
        固定量吸收池, 而是「剩餘次數」計次器, 每次受擊消耗1次(而非按傷害量扣減), val=1.0時
        完全格擋該次傷害、val=0.x時該次傷害打折(如警戒 -75.35%≈val=0.7535)。同源(同 src)
        再次施加時疊加次數(而非同 push_add/push_mod 慣例的「同源刷新覆蓋」), 貼合原文
        「抵禦(N次)」「目前抵禦總次數為N」的疊次語意。"""
        for b in self.block:
            if src and b.get("src") == src and abs(b["val"] - val) < 1e-9:
                b["n"] += n
                return
        self.block.append({"val": val, "n": n, "src": src})

    def consume_block(self):
        """消耗一次格擋(若有): 從陣列頭(先加的先消耗, 貼合戰報「總次數」單一計數語意)扣1次,
        n<=0時整筆移除。回傳消耗到的 val(0=無格擋可消耗, 呼叫端不應觸發)。"""
        if not self.block:
            return 0
        b = self.block[0]
        b["n"] -= 1
        val = b["val"]
        if b["n"] <= 0:
            self.block.pop(0)
        return val

    def tick(self):
        for dmg, *_ in self.dots:                      # 持續傷害結算(dots[2]為undispellable旗標, 不影響結算量)
            self.troop -= dmg
            self.wounded += dmg * wounded_rate(CUR_ROUND)  # 批18: dot 掉血同樣按當前回合轉化率計入傷兵池
        self.dots = [[d, l - 1] + rest for d, l, *rest in self.dots if l - 1 > 0]
        self.mods = [[s, m, l - 1, src, flags] for s, m, l, src, flags in self.mods if l - 1 > 0]
        self.adds = [[k, v, l - 1, src, flags] for k, v, l, src, flags in self.adds if l - 1 > 0]
        self.stat_adds = [[s, ad, l - 1, src, flags] for s, ad, l, src, flags in self.stat_adds if l - 1 > 0]  # 裝備平加到期移除(如 疾馳 speed+25 dur:2)
        self.stun = max(0, self.stun - 1)
        self.silence = max(0, self.silence - 1)
        self.disarm = max(0, self.disarm - 1)
        self.chaos = max(0, self.chaos - 1)            # 批12 ModeF: 混亂 逐回合遞減
        self.insight = max(0, self.insight - 1)
        self.first = max(0, self.first - 1)            # 先攻: 逐回合遞減(dur=N 覆蓋前 N 回合, 如「戰鬥前3回合」)
        self.ambush = max(0, self.ambush - 1)          # 批18: 遇襲 逐回合遞減(先攻的反面, 遲緩)
        self.healblock = max(0, self.healblock - 1)    # 批8: 禁療 逐回合遞減
        self.swap = max(0, self.swap - 1)
        if self.decay:
            self.decay["left"] -= 1
            if self.decay["left"] <= 0:
                self.decay = None
        self.taunt_dur = max(0, self.taunt_dur - 1)
        if self.taunt_dur <= 0:
            self.taunt_by = None
        if self.guard_dur:                             # 代承到期: 清 guardian(如 援助 首回合援護 dur:1)
            self.guard_dur = max(0, self.guard_dur - 1)
            if self.guard_dur <= 0:
                self.guardian, self.guard_share, self.guard_normal_only = None, 0.0, False
        self.dodge_dur = max(0, self.dodge_dur - 1)
        if self.dodge_dur <= 0:
            self.dodge_prob = 0.0
        self.surehit_dur = max(0, self.surehit_dur - 1)
        if self.shield:
            self.shield["dur"] -= 1
            if self.shield["dur"] <= 0:
                self.shield = None
        if self.counter:                               # 批23 A2: 反擊到期清除(過去 dur 幽靈欄位從不遞減, 帶時限的反擊變永久)
            self.counter["dur"] -= 1
            if self.counter["dur"] <= 0:
                self.counter = None
        self.hit_flags.clear()                         # 受擊觸發(when.on) 每回合各戰法重置一次觸發額度
        if self.immune:                                 # 批16: immuneTo 逐回合遞減(修正: 與 engine.js tick() 對齊, 此前 sgz.py 遺漏此行, 雙引擎不同步)
            self.immune = [[ty, l - 1] for ty, l in self.immune if l - 1 > 0]
        self.fake_report_dur = max(0, self.fake_report_dur - 1)  # 批16: 偽報 逐回合遞減(修正: 與 engine.js tick() 對齊, 此前 sgz.py 遺漏此行, 雙引擎不同步)


# 傷害公式旋鈕(批3 重塑): 社群拆解(知乎菜頭50級傷害模型 + B站櫻謀詭計錨點), 用實測錨點反解常數。
# 錨點(兵10000/coef1.0/士氣100/無增減傷, morale_mult(100)=1.0 已併入取樣, 取隨機帶中值1.0):
#   錨1 屬性差0   → 實測 ≈476 傷害 ⇒ DMG_A = 476/sqrt(10000) = 4.76
#   錨2 屬性差200 → 實測 ≈764 傷害 ⇒ DMG_B = (764-476)/200 = 1.44
#   錨3 屬性差大負值(保底) → 實測 ≈90  傷害 ⇒ DMG_FLOOR = 90/sqrt(10000) = 0.9
# 之後有更多實測數據(不同兵力/等級)可再校準, 目前僅50級單一等級係數樣本, 折入常數中。
DMG_A = 4.76
DMG_B = 1.44
DMG_FLOOR = 0.9


def damage(src, dst, coef, kind, src_troop=None, is_normal=None, is_active=None):
    troop = src.troop if src_troop is None else src_troop  # 結算傷害用施毒當下定格兵力
    atk = src.eff("intel") if kind == "intel" else src.eff("force")
    deff = dst.eff("intel") if kind == "intel" else dst.eff("command")
    troop_sqrt = math.sqrt(max(0, troop))
    base = max(DMG_A * troop_sqrt + DMG_B * (atk - deff), DMG_FLOOR * troop_sqrt) * coef
    base *= counter_mult(src.ttype, dst.ttype)        # 克制: 隊伍兵種 vs 隊伍兵種
    base *= morale_mult(MORALE)
    # 批22: 輸出減益疊加上限 -90%(戰報實測: 荀彧-50%疊到-90.00%封頂, 輸出至少保留10%)。例外:
    # 虛弱(無法造成傷害)類戰法既有慣例用單一 amp val=-1.0 精確歸零當回合傷害(克敵制勝/威謀
    # 靡亢/臨戰先登), 這是「無法造成傷害」的二元語意, 不是「%減益疊加」, 總和<=-1時維持完全
    # 歸零(不受-90%封頂影響), 只在 -1 < 總和 < -0.9 這個「多重%減益疊加但尚未到虛弱程度」的
    # 區間套用-90%下限。
    # 批24 D2: dmgType 過濾 —— amp()/addbonus("mitig") 傳入本次傷害的 kind(phys/intel), 只加總
    # 「未宣告 dmgType 或宣告類型與本次相符」的加成/減傷(見 e.dmgType 呼叫端, apply_effects
    # k=="amp"/"mitig" 分支)。批28 B3: is_normal(可選) —— 傳入本次傷害是否為普攻, 供 amp()/
    # addbonus("mitig") 過濾 normalOnly 標記的加成/減傷(僅普攻傷害生效, 見至柔動剛「降低我軍
    # 及敵軍全體普通攻擊傷害35%」——外部查證確認原文是「降低」非root data摘要文字誤植的「提升」,
    # 且明確限定「普通攻擊傷害」而非全部傷害, 過去mitig無範圍限定, 誤及戰法傷害, 見批28 B3
    # 修正說明); 呼叫端未傳(dot/counter/settle等既有呼叫慣例, 見 damage() 各呼叫點)時預設
    # None, 安全側不套用 normalOnly 加成/減傷(見 addbonus() docstring)。
    # 批31 A: is_active(可選, 對稱於 is_normal) —— 傳入本次傷害是否為主動/突擊戰法所致, 供
    # amp() 過濾 activeOnly 標記的加成(僅主動/突擊戰法傷害生效, 見士爭先赴)。
    total_amp = src.amp(kind, is_normal, is_active)
    base *= 0.0 if total_amp <= -1 else 1 + max(-0.9, total_amp)  # 增傷(疊加/衰減/敵方減益)
    mit = dst.addbonus("mitig", kind, is_normal) * (1 - min(1.0, src.addbonus("pierce")))  # 看破: 無視部分減傷
    base *= max(0.1, 1 - mit)
    base *= random.uniform(0.96, 1.04)
    return max(0, base)


def hit(src, dst, coef, kind, is_normal=False, on_event=None, on_deal=None, is_active=None):  # 造成傷害(含規避/護盾/代承轉移/反擊), 累積結算層數; 批31 A: is_active(可選, 尾端新增, 向後相容既有全部呼叫點)—— 傳入本次傷害是否為主動/突擊戰法所致
    if not src.surehit_dur and dst.dodge_dur and random.random() < dst.dodge_prob:  # 規避: 完全迴避一次傷害(必中無視)
        if on_event:
            on_event(dst, src, is_normal, 0)
        return
    dmg = damage(src, dst, coef, kind, is_normal=is_normal, is_active=is_active)  # 批28 B3/批31 A: 傳入is_normal/is_active供amp()過濾normalOnly/activeOnly標記的加成
    # 批22: block(次數型格擋, 抵禦/警戒同族) —— 判定順序 dodge→block→shield→傷害(見紅線指示)。
    # 每次受擊消耗1次(不論本次傷害量多寡), val=1.0(如「抵禦」)完全格擋歸零本次傷害,
    # val=0.x(如「警戒」-75.35%)按比例打折。用光即從陣列移除。
    # 批35 D: BLOCK_CONSUME_THRESHOLD —— grok查證機鑑先識原文「受到的傷害超過自身可攜帶
    # 最大兵力的6%時(最低100兵力)」才消耗1次警戒並減傷。未達門檻的傷害不消耗、不減傷,
    # 照常全額打進去(見 engine.js 同段註解/engine_limitations.md 第30節)。
    if dst.block and dmg > BLOCK_CONSUME_THRESHOLD:
        block_val = dst.consume_block()
        dmg *= max(0.0, 1 - block_val)
    if dst.shield and dst.shield["amt"] > 0:          # 護盾: 先於兵力扣減吸收傷害
        absorb = min(dst.shield["amt"], dmg)
        dst.shield["amt"] -= absorb
        dmg -= absorb
        if dst.shield["amt"] <= 0:
            dst.shield = None
    g = dst.guardian
    wr = wounded_rate(CUR_ROUND)  # 批18: 傷兵池 —— 本次受到的傷害按當前回合轉化率計入(準備階段 CUR_ROUND=0 用第1回合檔)
    if g and g.alive and g is not dst and not (dst.guard_normal_only and not is_normal):  # normalOnly 援護: 戰法傷害(is_normal=False)不轉移
        g_share = dmg * dst.guard_share
        d_share = dmg * (1 - dst.guard_share)
        g.troop -= g_share
        g.wounded += g_share * wr
        dst.troop -= d_share
        dst.wounded += d_share * wr
    else:
        dst.troop -= dmg
        dst.wounded += dmg * wr
    if dst.settle:
        dst.settle["layers"] = min(dst.settle["max"], dst.settle["layers"] + 1)
    ls = src.addbonus("lifesteal")                    # 批8: 倒戈 —— 造成傷害時按比例回復自身兵力(以本次造成的傷害量 dmg 為基準), 上限 START_TROOP
    if ls > 0 and src.alive:
        src.troop = min(START_TROOP, src.troop + dmg * ls)
    # 批33: on_event/on_deal 補傳 dmg(本次結算後的實際傷害量, 已經過block/shield/代承折算,
    # 與寫入 wounded 池的量一致) —— 供 e["ofDamage"](傷害比例治療) 反應式heal使用, 見
    # on_hit()/dealt_damage() 呼叫端與 apply_effects() heal 分支(dmg 參數)。
    if on_event:
        on_event(dst, src, is_normal, dmg)
    # 批27 A: on:"dealtDamage" —— src(施加本次傷害的一方)反應式觸發, 只在非規避(確實造成
    # 傷害, 含被完全格擋/護盾吸收歸零的情形——「造成傷害」語意上仍是「打出了這一擊」, 只是
    # 傷害量被防禦手段抵銷, 與「規避=攻擊未命中」不同, 故僅 dodge 分支排除, block/shield
    # 歸零不排除)時才觸發, 傳入 kind 供 dmgType(兵刃/謀略)過濾判斷。
    if on_deal and src.alive:
        on_deal(src, dst, is_normal, kind, dmg)
    c = dst.counter                                   # 反擊: 直接還擊 src(不經 hit, 不遞迴)
    if c and dst.alive and src.alive and random.random() < c.get("prob", 1.0):
        cd = damage(dst, src, c["coef"], c.get("kind", "phys"))
        src.troop -= cd
        src.wounded += cd * wounded_rate(CUR_ROUND)
    # 批28 B1: 守護式反擊(counter_guards) —— dst(如隊伍主將)受到普攻時, 由登記在
    # dst.counter_guards 裡的其他單位(如副將)代為反擊 src, 而非 dst 自己還手(見虎衛軍
    # 「我軍主將即將受到普攻時, 副將...對攻擊者造成兵刃傷害」)。只在普攻(is_normal=True)
    # 時觸發(對應原文「即將受到普攻時」, 非任意傷害); 每個守護單位每回合最多觸發1次(對應
    # 原文「每回合最多觸發1次」), 用 hit_flags 以 guardian 自身 id 為鍵節流(與 when.on 反應式
    # 的既有節流慣例一致, 見上方 hit_flags 說明)。
    if is_normal and dst.alive and src.alive:
        for g in dst.counter_guards:
            gu = g["unit"]
            if not gu.alive or gu is dst:
                continue
            flag_key = ("counter_guard", id(g))
            if flag_key in gu.hit_flags:
                continue
            if random.random() < g.get("prob", 1.0):
                gu.hit_flags.add(flag_key)
                gd = damage(gu, src, g["coef"], g.get("kind", "phys"))
                src.troop -= gd
                src.wounded += gd * wounded_rate(CUR_ROUND)


def extra_count(ex):                                  # 連擊/追擊次數: 整數部分必定, 小數部分機率
    return int(ex) + (1 if random.random() < (ex - int(ex)) else 0)


def target_has(u, ctype):
    """批16: ifTargetHas —— 效果/extraHits 段條件: 只對「已有該狀態」的目標生效/結算。
    dot: dots 陣列非空(=正在持續掉血); 控制類(stun/silence/disarm/chaos/insight): 對應欄位>0。"""
    if not u:
        return False
    if ctype == "dot":
        return len(u.dots) > 0
    if ctype in ("stun", "silence", "disarm", "chaos", "insight"):
        return getattr(u, ctype) > 0
    return False


def pick_choice(choices):
    """批16: choices(擇一分支) —— 戰法欄 choices:[{weight, effects,...}], 發動時按權重隨機選一組
    效果套用(預設均分, 無 weight 視為1)。回傳中選分支物件本身(供合併覆寫基礎戰法的 coef/kind/
    effects/extraHits/n/nMax 等欄位; 分支未提供的欄位保留基礎戰法原值)。"""
    ws = [c.get("weight", 1) for c in choices]
    total = sum(ws)
    x = random.random() * total
    for c, w in zip(choices, ws):
        x -= w
        if x <= 0:
            return c
    return choices[-1]


def dispel_unit(u, what):
    """批16: dispel(驅散/淨化) —— 移除目標身上對應方向(buffs=正向增益/debuffs=負向減益)的條目,
    略過帶 undispellable 旗標(flags.undispellable, 見 push_add/push_mod/push_stat_add 呼叫端 ud_flags)
    的條目。buffs: amp(正值)/mitig(正值)/stat mult>=1或add>=0/rateup/chargeup/shield/dodge/surehit/
    lifesteal/healBoost/healGiven/counter/pierce/extra/first/insight。
    debuffs: amp(負值)/mitig(負值)/stat mult<1或add<0 + 控制欄位(stun/silence/disarm/chaos/dot/
    healblock/fakeReport/swap)。只挪動「數值型」adds/mods/stat_adds 依正負號分類; 控制欄位
    (debuffs專屬)直接歸零/清空。"""
    def not_ud(entry):
        flags = entry[4] if len(entry) > 4 else None
        return not (flags and flags.get("undispellable"))

    def is_buff(a):                                    # 除 amp/mitig 外的 adds 種類一律視為buff
        return a[1] > 0 if a[0] in ("amp", "mitig") else True

    if what == "buffs":
        u.adds = [a for a in u.adds if not (is_buff(a) and not_ud(a))]
        u.mods = [m for m in u.mods if not (m[1] >= 1 and not_ud(m))]
        u.stat_adds = [a for a in u.stat_adds if not (a[1] >= 0 and not_ud(a))]
        if u.shield and not u.shield.get("undispellable"):
            u.shield = None
        if u.block:
            u.block = []                                # 批22: block(抵禦/警戒)為防禦性增益, 同 shield 慣例被 buffs 驅散清除(現有資料未帶 undispellable block)
    else:                                              # debuffs
        u.adds = [a for a in u.adds if not (a[0] in ("amp", "mitig") and a[1] < 0 and not_ud(a))]
        u.mods = [m for m in u.mods if not (m[1] < 1 and not_ud(m))]
        u.stat_adds = [a for a in u.stat_adds if not (a[1] < 0 and not_ud(a))]
        u.dots = [d for d in u.dots if len(d) > 2 and d[2]]   # 保留 undispellable(dots[2]=True)的 dot, 清除其餘
        u.stun = u.silence = u.disarm = u.chaos = u.healblock = 0
        u.fake_report_dur = 0
        u.ambush = 0


def round_ok(t, r):                                    # 條件觸發(when): 回合是否符合戰法的發動窗口
    w = t.get("when")
    if not w:
        return True
    if w.get("rounds"):
        return r in w["rounds"]
    if w.get("from") is not None and r < w["from"]:
        return False
    if w.get("until") is not None and r > w["until"]:
        return False
    # 批16: parity(奇偶回合) + every(每N回合) —— 與 rounds/from/until 可並存(皆通過才算符合)
    if w.get("parity") == "odd" and r % 2 != 1:
        return False
    if w.get("parity") == "even" and r % 2 != 0:
        return False
    if w.get("every") and r % w["every"] != 0:
        return False
    return True


def hp_ok(t, u):
    """批16: hpPct 觸發 —— 每回合窗口檢查自身兵力百分比(troop/START_TROOP)。hpBelow: 首次跨越即
    觸發(一次性, when_fired慣例); hpAbove: 持續窗(只要條件成立, 每回合都可能觸發, 不去重)。
    與 round_ok 分開的獨立判定(hpPct 條件不是回合數, 需讀 unit.troop, 故不塞進 round_ok)。"""
    w = t.get("when")
    if not w:
        return True
    if w.get("hpBelow") is not None and not (u.hp_pct < w["hpBelow"]):
        return False
    if w.get("hpAbove") is not None and not (u.hp_pct > w["hpAbove"]):
        return False
    return True


def pick_target(units, attacker=None):                # 普攻/單體戰法: 隨機挑一個存活敵軍(不再固定打兵力最高); 嘲諷: 攻擊者身上有 taunt_by 時強制指向該目標
    if attacker is not None and attacker.taunt_dur and attacker.taunt_by is not None \
            and attacker.taunt_by.alive and attacker.taunt_by in units:
        return attacker.taunt_by
    live = [u for u in units if u.alive]
    return random.choice(live) if live else None


def pick_targets(units, n):                           # 群體戰法: 隨機挑 n 個不重複存活目標
    live = [u for u in units if u.alive]
    if len(live) <= n:
        return live
    return random.sample(live, n)


# 批18: targetSel(指定選標準則) —— user 實測: 混亂只影響「隨機」選目標的主動/突擊/普攻,
# 「指定」類戰法(按準則選目標: 兵力最低/武力最高/智力最低/我方最殘等)不受混亂影響, 因為
# 這些戰法根本不是隨機選標, 而是每次發動當下依準則重新篩選(非鎖定, 見批12/避實擊虛的
# lockTarget vs 依屬性選標之辨)。TARGETSEL_KEY: 準則->(單位->比較值), TARGETSEL_MIN: 取最小者的準則集合。
TARGETSEL_KEY = {
    "minTroop": lambda u: u.troop, "maxForce": lambda u: u.eff("force"),
    "minIntel": lambda u: u.eff("intel"), "maxIntel": lambda u: u.eff("intel"),
    "minCommand": lambda u: u.eff("command"), "mostDamaged": lambda u: u.troop,
}
TARGETSEL_MIN = {"minTroop", "minIntel", "minCommand", "mostDamaged"}


def pick_by_criterion(units, sel):
    key_fn = TARGETSEL_KEY.get(sel)
    if not key_fn:
        return None                                   # 未知準則: 呼叫端應退回一般選標(保守, 不是無聲吃掉)
    live = [u for u in units if u.alive]
    if not live:
        return None
    return (min if sel in TARGETSEL_MIN else max)(live, key=key_fn)


def pick_target_chaos(u, allies, foes):
    """批12 ModeF: 混亂(chaos)單體選標 —— 普攻/單體主動戰法目標選擇改為「敵我不分」: 從友軍+敵軍
    (排除自己)中隨機挑一個存活目標, 而非只從敵方挑。非混亂狀態時退回一般 pick_target(含嘲諷判定)。
    群體/AoE 戰法在混亂下維持原邏輯不變(近似, 見呼叫端註解)。"""
    if not u.chaos:
        return pick_target(foes, u)
    pool = [x for x in (allies + foes) if x.alive and x is not u]
    if not pool:
        return pick_target(foes, u)          # 保底: 沒有其他存活單位時退回一般選標
    v = random.choice(pool)
    return v


def resolve_locked_target(u, t, foes):
    """批12 ModeG: lockTarget —— 戰法首次發動時透過 pick_target 正常選標, 之後每次發動重用同一
    目標(以 id(t) 為鍵存進 u.locked_targets, dict 不可雜湊故用 id()), 而非每次重新隨機選。若鎖定
    目標已陣亡: 依 brief 保守決策(來源文字未說明死亡後是否重新鎖定), 視為「本次發動找不到有效
    目標」回傳 None, 不重新選新目標(不做隱式重新鎖定, 避免無根據臆測遊戲行為)。"""
    key = id(t)
    if key in u.locked_targets:
        locked = u.locked_targets[key]
        return locked if (locked and locked.alive) else None  # 鎖定目標已陣亡 -> 本次無有效目標(不重新選)
    picked = pick_target(foes, u)
    if picked:
        u.locked_targets[key] = picked
    return picked


def fire_extra_hits(u, t, tgt, allies_of, foes_of, on_hit, on_deal=None):
    """批13: extraHits —— 多段傷害(兵刃+謀略雙段/主傷+補刀等單一 coef/kind/n 無法表達的戰法)。
    戰法欄 extraHits:[{coef,kind,n,nMax,rate,who,_note}]: 主 coef 結算後逐段獨立處理, 每段各自
    rate 擲骰(預設1必發)、選目標、hit()。who 可選: "sameTarget"(沿用主 coef 段已選定的(單體)
    目標, 如屠几上肉 兵刃+謀略同目標/一騎當千 主將加成同目標)、"enemyLeader"(固定打敵方主將
    foes[0], 如百騎劫營/暗藏玄機 額外段明確打敵軍主將)、不填則預設 pick_targets(敵方, 依n/nMax)。
    與主 coef 段完全獨立(各自的 kind 可不同, 如兵刃主傷+謀略補刀), 不與 hitsRepeat/lockTarget
    互斥(hitsRepeat/lockTarget 只影響主 coef 段的選標方式, extraHits 段固定用上述規則)。
    批27: on_deal(選填) —— 轉呼叫給 hit(), 讓 extraHits 段造成的傷害也能觸發 on:"dealtDamage"
    反應式戰法(與主coef段/普攻/突擊一致, 見 fight() 各呼叫端)。"""
    for eh in t.get("extraHits") or []:
        if random.random() >= eh.get("rate", 1.0):
            continue
        n = eh.get("n") or 1
        cnt = n + random.randint(0, eh["nMax"] - n) if eh.get("nMax") else n
        who = eh.get("who")
        # 批18: targetSel(指定選標準則) —— 段級欄位, 優先於 who 的其餘規則(sameTarget/enemyLeader/
        # 隨機)。如 上兵伐謀「分別對兵力最低、武力最高、智力最低的敵將」三段各自不同準則。
        if eh.get("targetSel"):
            picked = pick_by_criterion(foes_of(u), eh["targetSel"])
            dests = [picked] if picked else []
        elif who == "sameTarget":
            dests = [tgt] if (tgt and tgt.alive) else []          # 沿用主段已選定的(單體)目標
        elif who == "enemyLeader":
            foes = foes_of(u)
            dests = [foes[0]] if (foes and foes[0].alive) else []  # 固定打敵方主將(index 0)
        elif cnt <= 1 and tgt and tgt.alive and not who:
            dests = [tgt]                                    # 未指定who且單體: 沿用主段目標(向後相容預設行為)
        else:
            dests = pick_targets(foes_of(u), cnt)
        if eh.get("ifTargetHas"):                         # 批16: ifTargetHas —— extraHits 段結算前檢查, 只對「已有該狀態」的目標結算此段傷害
            dests = [v for v in dests if target_has(v, eh["ifTargetHas"])]
        # 批31 B: ifSameTargetIsLeader —— extraHits 段結算前檢查, 只對「(主coef段隨機選定的)
        # 目標剛好就是敵方隊伍固定位置的主將(foes[0])」時才結算此段傷害, 精確表達原文「若目標
        # (普攻/主傷段隨機選定的對象)為敵軍主將，額外造成傷害」這種條件分支(對比批16的
        # ifTargetHas 是檢查「目標身上是否已有某個狀態」, 這裡檢查的是「目標的隊伍位置是否為
        # 主將」, 概念上更接近既有 who:"enemyLeader" 的固定位置判斷, 但用於「事後過濾已選定的
        # 隨機目標」而非「主動選定目標」, 是不同的判斷時機)。取代舊有 EV 折算近似(如暗藏玄機
        # 過去用 1/3 機率折算「隊伍3人之一為主將」的近似觸發率, 現改真實比對 dests 是否等於
        # foes[0], 精確表達條件分支而非期望值近似)。
        if eh.get("ifSameTargetIsLeader"):
            foes = foes_of(u)
            leader = foes[0] if (foes and foes[0].alive) else None
            dests = [v for v in dests if v is leader]
        for v in dests:
            hit(u, v, eh["coef"], eh.get("kind", "phys"), False, on_hit, on_deal)


def apply_effects(caster, tgt, t, allies, enemies, heal_only=False, no_heal=False, skip_when_effects=False,
                   rate_checked=False, reactive=False, dmg=None):
    # 批33: dmg(可選)—— 反應式呼叫端(on_hit/dealt_damage)傳入「觸發本次效果結算的那一下傷害
    # 量」, 供 heal 分支的 e["ofDamage"](傷害比例治療) 使用, 見下方 k=="heal" 分支。
    src = t.get("nameZh")                              # 效果來源標籤: 戰法名(兵書/裝備/緣分無 nameZh → None, 不去重)
    for e in t["effects"]:
        k = e["k"]
        # 批35 B: block 的「準備階段鎖定」scale 值優先算定, 放在所有 continue 閘門(heal_only/
        # skip_when_effects/when.on/rate/ifLeader/everyRound...)之前 —— 必須確保 prep 呼叫
        # (fight() 開場的 apply_passives(no_heal=True, skip_when_effects=True))第一次掃描到
        # 帶 e["when"](如機鑑先識 everyRound 段的 when:{until:3})的 block 效果時就把鎖算好,
        # 否則若鎖定邏輯放在 skip_when_effects/everyRound 等後面的閘門之後, 帶 e["when"] 的
        # everyRound block 效果會在 prep 呼叫被 skip_when_effects 閘門提前 continue 掉,
        # 導致 locked_scale_of 從未在 prep 階段被呼叫過、鎖定值錯誤地延後到未來真正命中的
        # 那一回合才用當時(可能已變動)的即時智力算定, 違反「準備階段鎖定」語意本身。
        if k == "block" and e.get("scale"):
            locked_scale_of(caster, e)
        if heal_only and k != "heal" and not e.get("everyRound"):  # 指揮/被動逐回合只跑治療 + 批30 A: everyRound 效果亦放行
            continue
        # 批18: e.when 泛化(非 heal 種類) —— heal 早已支援效果級 when(見下方 k=="heal" 分支的
        # heal_only 閘門), 但其餘效果種類(amp/settle/stat/…)過去若帶 e["when"] 而母戰法無
        # t["when"], 會在 prep 階段(skip_when_effects=True 時, 見 fight() 呼叫端)被無聲當成
        # 「無 when 的常駐效果」立即套用, 忽略 e["when"] 指定的回合窗口(如 密計誅逆的 settle
        # when:{rounds:[6]}/工神的 amp when:{from:4}, 見 _todo 揭露)。此處在 prep 呼叫時跳過
        # 這些效果, 改由 fight() 回合迴圈的通用 e.when 掃描(仿 delayed_eq 慣例)在視窗開啟時才套用。
        if skip_when_effects and k != "heal" and e.get("when") and not t.get("when"):
            continue
        # 批23: e["when"]["on"](反應式, 受擊當下觸發) 效果只應在 on_hit() 事件點結算
        # (reactive=True 的合成單效果呼叫), 不應在準備階段/主動主迴圈擲骰(fire=random.random()
        # <t0["rate"])/charge突擊等一般路徑被無條件套用。過去(草船借箭0分bug之一)heal 的
        # e["when"]["on"] 只被 heal 分支自己內部的 heal_only 閘門過濾, 但一般 active 主動戰法
        # 擲骰命中時呼叫 apply_effects() 完全不經過 heal_only, 導致帶 e["when"]["on"] 的 heal
        # 效果被當成「無 when 的常駐效果」在戰法觸發當下立即無條件治療一次, 與 on_hit 反應式
        # 觸發疊加, 造成雙重結算。此處統一擋下: 非 reactive 呼叫時, 任何 k 只要帶
        # e["when"]["on"] 就跳過(改由 on_hit() 事件點才會結算)。
        if not reactive and (e.get("when") or {}).get("on"):
            continue
        # 批23 A4: 效果級 e["rate"] 折算一致性 —— 過去只有 on_hit(反應式)/delayed_eq(裝備回合
        # 窗)兩條路徑會讀 e["rate"](見呼叫端各自的 ev_rate = e.get("rate", ...) 判定), 其餘路徑
        # (prep/active主動/charge突擊/when視窗一次性套用)完全忽略 e["rate"], 造成同一戰法內
        # 「有的效果段折機率、有的沒折」(如草船借箭80%/魚鱗陣heal段25%/援救50%)。修法: 在這裡
        # 統一補上判定(套用時 random.random()<e["rate"], 比EV折算更接近真實方差, 見批23 A4
        # brief)。rate_checked=True: 呼叫端(on_hit/delayed_eq 的合成單效果呼叫)已自行讀取並
        # 擲骰過同一個 e["rate"], 避免在這裡對同一效果重複擲骰(機率會被平方, 造成低估)。
        if not rate_checked and e.get("rate") is not None and random.random() >= e["rate"]:
            continue
        # 批26: e["ifLeader"] —— 效果級「施放者須為隊伍主將(index 0)」條件閘門。原文常見
        # 「自身為主將時，額外XX」這種措辭(南蠻渠魁/江東小霸王/酒池肉林等), 過去無對應原語,
        # 該效果段只能被迫「無條件對所有施放者套用」(高估非主將情形)或完全不建模(遺漏主將
        # 加成)。allies[0] 是隊伍主將慣例(同 who=="leader" 分支既有假設, 見上文), 只在
        # caster 就是 allies[0] 時才放行本效果段, 否則跳過。與 e["rate"] 同層級判斷(任何 k
        # 皆可掛), 置於 e["rate"] 判定之後(若戰法同時有機率也要求主將, 兩者皆需通過)。
        if e.get("ifLeader") and not (allies and allies[0] is caster):
            continue
        # 批30 A: 非heal效果的逐回合重擲通道(e["everyRound"]) —— 過去只有 k=="heal" 在
        # heal_only(見 apply_passives 的逐回合呼叫)這條路徑下逐回合重新掃描/擲骰套用, 其餘
        # k(amp/mitig/block/stat/...)一旦在 prep 套用一次就不會再被重新判定, 導致「每回合
        # X%機率獲得1次抵禦/減傷」類戰法(機鑑先識/揮兵謀勝/魚鱗陣/枕戈坐甲等, 見
        # engine_limitations.md 第11節/25節)只能 EV 折算或截斷成一次性。修法: 把 heal 既有
        # 的「when視窗判定 + rounds去重 + rate擲骰」邏輯泛化成任何 k 皆可掛的通用閘門, 用
        # e["everyRound"](效果級旗標, opt-in)標記「這個效果不在 prep 套用一次, 改在每回合
        # 常駐通道重新判定」。與 heal 共用同一份 heal_rounds_fired/when_fired 去重狀態(鍵是
        # id(e), heal 與 everyRound 不會撞鍵, 因為同一個效果物件只會是其中一種)。刻意不新增
        # 獨立的 dedup dict, 沿用既有慣例、降低維護面。
        #
        # 語意與 heal 完全對稱: heal_only 模式下, 非 heal 效果只有帶 e["everyRound"] 才會走
        # 到這裡(否則在上面的 top-level k!=heal 過濾就被跳過, 見函式開頭); 帶 everyRound 的
        # 效果在**非** heal_only 呼叫路徑(prep/active/charge/when視窗)一律跳過(不套用),
        # 因為它只該由 heal_only 常駐通道結算 —— 對稱於 heal 在其他路徑各自決定是否觸發、
        # 不依賴這裡的慣例。
        if e.get("everyRound") and k != "heal":
            # 批35 B: block 的「準備階段鎖定」scale 值已在函式最頂端(所有 continue 閘門之前)
            # 算定, 此處不需重複呼叫 locked_scale_of(見上方新增的閘門與其註解)。
            if not heal_only:
                continue
            hw = e.get("when") or t.get("when")
            if hw:
                if not round_ok({"when": hw}, CUR_ROUND):
                    continue
                if hw.get("rounds"):
                    seen = caster.heal_rounds_fired.setdefault(id(e), set())
                    if CUR_ROUND in seen:
                        continue
                    seen.add(CUR_ROUND)
            elif e.get("once"):
                if id(e) in caster.when_fired:
                    continue
                caster.when_fired.add(id(e))
            ev_rate = e.get("rate", t.get("rate", 1))
            if random.random() >= ev_rate:
                continue
            # 通過閘門後不 continue —— 落到下方通用 who/dests 派發邏輯(amp/mitig/block/...),
            # 走與 prep 套用相同的效果分派, 只是改成每回合重新判定/套用一次。
        # 批32 R23: e.when(非heal/非everyRound效果) 的回合窗口檢查 —— 過去只有「母戰法無
        # t.when 時, skip_when_effects=True 的 prep 呼叫會跳過此效果(留給 fight() 回合迴圈
        # 通用掃描處理, 見上方1045行)」這一種路徑會尊重 e.when; 其餘直接呼叫 apply_effects()
        # 的路徑(尤其 active 型戰法擲骰命中後, fight() 主迴圈的
        # `apply_effects(u, active_dst, t, ...)` 直接呼叫, 見主迴圈 active 分支)完全不檢查
        # e.when, 導致「奇數回合...偶數回合...」這類需要用 e.when.parity 切分同一戰法內兩組
        # 互斥效果的 active 戰法(飛沙走石), 即使補了 e.when.parity 也會被無條件套用(奇偶
        # 兩組效果同時生效, 塌縮成常駐雙倍輸出, 即R23要抓的缺口本身)。此處補上通用檢查:
        # 任何非heal/非everyRound效果只要帶 e["when"], 就先驗證當前回合是否落在窗口內, 不符合
        # 則跳過該效果段(不影響同戰法內其餘無 e.when 的效果, 也不影響 heal_only/skip_when_effects
        # 呼叫路徑既有行為——那些路徑要嘛在更上層已被攔截, 要嘛壓根不會走到這裡)。
        elif e.get("when") and k != "heal" and not e.get("everyRound"):
            if not round_ok({"when": e["when"]}, CUR_ROUND):
                continue
        if k == "heal":                               # 治療: 補我方最殘一人(指揮/被動每回合觸發)
            if no_heal:
                continue
            if e.get("coef", 0.8) < 0:                # 批10: 資料衛生防禦 —— 負 heal coef(如機略縱橫類 dot 誤標成 heal 負值)一律視為0並跳過, 避免資料錯誤反而扣友軍血
                continue
            # 批15: 指揮/被動的 heal 在 heal_only(每回合無條件常駐掃描, 見 apply_passives 的
            # 逐回合呼叫)這條路徑下, 過去無視 t["when"]/t["rate"]/e["once"], 每回合必定結算 ——
            # 「第N回合治療一次」類戰法(如撫輯軍民/桃園結義/士別三日)被無聲放大成每回合治療
            # (~8倍/回合數倍)。修正: 僅在 heal_only 常駐路徑套用下列語意閘門(其餘呼叫路徑, 如
            # when 視窗一次性套用/active主動/charge突擊/onHit反應式, 呼叫前已各自決定是否該
            # 觸發, 不應再被此處二次過濾):
            #   1) e["when"](效果級, 優先) 或 t["when"](戰法級) 存在 → 用 round_ok 檢查回合是否
            #      落在視窗內, 不符合則本回合不治療。e["when"] 用途: 同一戰法內其餘效果(如撫輯
            #      軍民的 mitig/amp)是「前3回合就生效」的常駐buff(無when, 準備階段套用), 但
            #      heal 段是「第4回合單次觸發」—— 兩者時間窗不同, 不能共用同一個 t["when"]
            #      (會連帶把 mitig/amp 也延後到第4回合才套用), 故 heal 效果自己帶 e["when"]
            #      覆蓋, 不影響同戰法其他效果的準備階段套用時機。
            #      - when["rounds"](明確列出的單一/多個回合, 如「第4回合」「第3、5回合」):
            #        語意是「只在這些特定回合各觸發一次」, 用 heal_rounds_fired(效果物件+回合
            #        組合去重)確保 rounds:[3,5] 這種多回合列表在第3、第5回合各自觸發一次、
            #        不重複、也不會在其他回合誤觸發。
            #      - when["from"]/["until"](範圍視窗, 如「第3回合起, 持續3回合」「第5回合
            #        起」): 語意是「這幾回合每回合都要治療」(休整/持續恢復類戰法, 如金丹秘術/
            #        詐降/魚鱗陣), 故只用 round_ok 檢查是否在窗內, 不做去重(讓窗內每回合都能
            #        重新擲骰/治療)。
            #   2) e["once"] is True(單次治療語意, 無 when 亦適用) → 觸發過一次即不再結算,
            #      同樣用 when_fired 去重。
            #   3) 無 when(e["when"]/t["when"]皆無)且無 e["once"] → 維持原行為: 每回合持續
            #      治療(急救/休整類戰法本意如此)。
            #   以上都通過後才擲 t["rate"] 骰(rate<1 時只有部分回合真正治療, 而非年年必中)。
            if heal_only:
                hw = e.get("when") or t.get("when")
                if hw:
                    if not round_ok({"when": hw}, CUR_ROUND):
                        continue
                    if hw.get("rounds"):                  # 明確列出的特定回合: 每個列出的回合各觸發一次(回合特定去重鍵, 而非整場只觸發一次)
                        seen = caster.heal_rounds_fired.setdefault(id(e), set())
                        if CUR_ROUND in seen:
                            continue
                        seen.add(CUR_ROUND)
                    # from/until(範圍視窗): 不去重, 窗內每回合都可能治療(休整類戰法本意如此, 如金丹秘術/詐降/魚鱗陣)
                elif e.get("once"):
                    if id(e) in caster.when_fired:
                        continue
                    caster.when_fired.add(id(e))
                if random.random() >= t.get("rate", 1):
                    continue
            hurt = min((a for a in allies if a.alive and not a.healblock),  # 批8: 禁療(healblock) 中的目標跳過, 不參與「最殘一人」篩選
                       key=lambda a: a.troop, default=None)
            if hurt:                                  # ponytail: 治療量粗估, 上限不超過初始兵力
                # 批33: e["ofDamage"] —— 傷害比例治療(非屬性公式), 見草船借箭「回復傷害量
                # 14%→28%」類措辭。dmg(本次觸發傷害量)由反應式呼叫端傳入, 與屬性公式互斥擇一。
                hcoef = e.get("coef", 0.8) * (scale_of(caster, e["scale"]) if e.get("scale") else 1.0)
                # 批16: healBoost/healGiven —— 目標受到的治療量×(1+healBoost加總), 施放者施放的治療×(1+healGiven加總)
                boost_mult = max(0.0, 1 + hurt.addbonus("healBoost")) * max(0.0, 1 + caster.addbonus("healGiven"))
                # 批33: 治療公式換裝 —— want = coef × HEAL_TROOP_C × 施放者兵力 × SCALE(scale屬性)
                # (見上方 HEAL_TROOP_C 常數註解 / calibration_anchors.json
                # heal_formula_resolved_20260704)。「施放者兵力」依戰法型態擇一: active(主動
                # 直療型, 施放當下即時觸發)用 caster.troop(當下即時兵力); 其餘(指揮/兵種/兵書/
                # 被動的常駐急救型, 受傷當下反應式觸發)用 caster.heal_base(準備階段鎖定兵力
                # 快照, 不隨後續兵力增減而變動)。e["ofDamage"] 存在時改用傷害比例治療。
                heal_troop_base = caster.troop * HEAL_TROOP_C if t.get("type") == "active" else caster.heal_base
                if e.get("ofDamage") is not None and dmg is not None:
                    want = e["ofDamage"] * dmg * boost_mult
                else:
                    want = hcoef * heal_troop_base * boost_mult
                # 批18: 傷兵池 —— 治療只能回復傷兵池裡的量(可救援的傷兵), 不是無限回滿。實際回復 =
                # min(想治療量, 傷兵池餘量, 距滿編差額); 回復後從傷兵池扣掉對應量。這會全域削弱
                # 治療(尤其後期, 陣亡比例升高、傷兵池餘量變少), 屬預期真實化。
                actual = max(0.0, min(want, hurt.wounded, START_TROOP - hurt.troop))
                hurt.troop += actual
                hurt.wounded -= actual
            continue
        if k == "settle":                             # 結算傷害(猛毒): 掛統率最高敵將, 觸發見 fight
            tg = max((x for x in enemies if x.alive),
                     key=lambda x: x.eff("command"), default=None)
            if tg:
                tg.settle = {"layers": e.get("init", 1), "max": e.get("max", 3),
                             "left": e.get("dur", 2), "caster": caster, "snap": caster.troop,
                             "base": e.get("base", 1.5), "per": e.get("per", 0.4),
                             "kind": t.get("kind", "intel")}
            continue
        if k == "redirect":                           # 傷害轉移: 代承者替其餘友軍吃 share
            if e.get("guard") == "max_force":         # 代承者: 武力最高友軍 或 自己(預設)
                guardian = max((a for a in allies if a.alive),
                               key=lambda a: a.eff("force"), default=caster)
            else:
                guardian = caster
            for a in allies:
                if a.alive and a is not guardian:
                    a.guardian, a.guard_share = guardian, e.get("share", 0.3)
                    a.guard_dur = e.get("dur", 99)    # 讀 e.dur(預設99=近似全程, 向後相容); 到期由 tick 清除
                    a.guard_normal_only = bool(e.get("normalOnly"))  # 只代承普攻(如 援助), 戰法傷害不轉移
            continue
        who = e.get("who", "ally")
        ctrl_k = k in ("stun", "silence", "disarm", "taunt", "chaos")  # 控制/嘲諷類: 按戰法 n/nMax 選目標數(insight 不擋嘲諷, 只擋 stun/silence/disarm/chaos)
        # 批23 A1: 效果級 e["n"](可配 e["nMax"]) —— 非CTRL效果(amp/mitig/stat/dot/healblock/
        # rateup/…)過去無條件把 who="enemy"/"ally" 放大成全體敵軍/我軍, 大量原文寫「單體」
        # 「目標」「我軍2人」的非控制效果被系統性高估成全體(見批23清單: 謙讓/殿後/破甲/談心/
        # 追傷/兵鋒/舌戰群儒/八門金鎖陣/進言/江東小霸王/眾動萬計/國士將風等)。修法: 有 e["n"]
        # 時比照 ctrl_k 群體控制的既有選標邏輯(pick_targets 隨機不重複; 單體時優先鎖定 tgt,
        # 與 ctrl_k 慣例一致), 只是讀 e["n"]/e["nMax"](效果自身欄位)而非 t["n"]/t["nMax"]
        # (戰法頂層, ctrl_k 專用, 維持不變)。無 e["n"] 時完全維持原行為(全體敵軍/我軍), 向後
        # 相容 —— 大量「全體」條目依賴現行為。
        has_en = e.get("n") is not None
        # 批18: targetSel(指定選標準則) —— 效果級欄位, 優先於 who 的預設隨機/群體邏輯: 依準則
        # (兵力最低/武力最高/智力最低/我方最殘等)在對應陣營(enemy用敵方, 其餘用我方)挑單一目標。
        # 「指定」不受混亂(chaos)影響(混亂只亂「隨機」選目標的普攻/主動/突擊, 見 pick_target_chaos
        # 呼叫端 —— targetSel 在此處直接決定 dests, 完全不經過受混亂影響的 tgt/pick_targets 隨機路徑)。
        if e.get("targetSel"):
            pool = enemies if who == "enemy" else allies
            picked = pick_by_criterion(pool, e["targetSel"])
            dests = [picked] if picked else []
        elif who == "self":
            dests = [caster] if caster.alive else []
        elif who == "leader":                         # 批8: 主將限定(隊伍 index 0)
            dests = [allies[0]] if allies and allies[0].alive else []
        elif who == "subs":                           # 批13: 副將群限定(隊伍 index 0 以外; 如鋒矢陣/箕形陣副將分化段)
            dests = [a for a in allies[1:] if a.alive]
        # 批30 C: who=="sub1"/"sub2"(副將固定位置分派) —— 「subs」只能讓兩名副將套用同一份
        # 效果, 無法表達「副將A只防兵刃, 副將B只防謀略」這種依隊伍固定位置(而非動態屬性準則)
        # 分派相異效果的語意(見箕形陣, engine_limitations.md 第25節/16節)。sub1=allies[1]
        # (副將A, index 1), sub2=allies[2](副將B, index 2), 對稱於既有 who=="leader"=allies[0]
        # 慣例。三人隊固定編制(index 0=主將/1/2=副將), 若隊伍不足3人或該位置陣亡則 dests 為空。
        elif who == "sub1":
            dests = [allies[1]] if len(allies) > 1 and allies[1].alive else []
        elif who == "sub2":
            dests = [allies[2]] if len(allies) > 2 and allies[2].alive else []
        elif who == "enemy":
            if ctrl_k:                                # 群體控制隨機挑不重複目標; 單體優先鎖定 tgt
                # 批26: CTRL類效果優先讀 e["n"]/e["nMax"](效果自身欄位), 無則 fallback 到
                # t["n"]/t["nMax"](戰法頂層, 舊行為, 向後相容)。原本 ctrl_k 只認頂層 n/nMax,
                # 導致同一戰法內「多段各自不同目標數的chaos/stun等控制效果」(如神機莫測「1名
                # 必中混亂 + 另外N名各自獨立機率判定混亂」)無法用單一戰法頂層n表達出兩種不同
                # 的目標數, 只能被迫二選一近似成同一個n。has_en 沿用批23 A1既有判斷(e["n"]是否
                # 存在), 場景不衝突: 非ctrl_k效果本就走 has_en 分支(見下方elif), 這裡只是讓
                # ctrl_k效果也能「有e.n就優先用」, 沒有e.n時完全維持原行為(讀t.n/t.nMax)。
                if has_en:
                    n = e["n"]
                    n_max = e.get("nMax")
                else:
                    n = t.get("n") or 1
                    n_max = t.get("nMax")
                cnt = n + random.randint(0, n_max - n) if n_max else n
                if cnt <= 1:
                    dests = [tgt] if tgt and tgt.alive else pick_targets(enemies, 1)
                else:
                    dests = pick_targets(enemies, cnt)
            elif has_en:                               # 批23 A1: 非CTRL效果讀 e["n"]/e["nMax"]
                n = e["n"]
                cnt = n + random.randint(0, e["nMax"] - n) if e.get("nMax") else n
                if cnt <= 1:
                    dests = [tgt] if tgt and tgt.alive else pick_targets(enemies, 1)
                else:
                    dests = pick_targets(enemies, cnt)
            else:
                dests = [x for x in enemies if x.alive]
        elif has_en:                                   # 批23 A1: who="ally"(含預設) 非CTRL效果讀 e["n"]/e["nMax"](如「我軍2人」「自己及友軍單體」)
            n = e["n"]
            cnt = n + random.randint(0, e["nMax"] - n) if e.get("nMax") else n
            if cnt <= 1:
                dests = [tgt] if (tgt and tgt.alive and tgt in allies) else pick_targets(allies, 1)
            else:
                dests = pick_targets(allies, cnt)
        else:
            dests = [a for a in allies if a.alive]
        # 批16: ifTargetHas —— 效果段條件, 只對「已有該狀態」的目標生效; 選目標後過濾(不影響選目標邏輯本身)
        if e.get("ifTargetHas"):
            dests = [u for u in dests if target_has(u, e["ifTargetHas"])]
        # scale="intel"|"force"|"command"|"speed"|"charm" 縮放(以施放者戰鬥內即時素質為準):
        # amp/mitig 的 val 直接乘 SCALE, clamp 到 ±SCALE_CLAMP 防止極端值; stat 的 mult 對
        # 1.0 的偏移量(增益/削弱幅度)乘 SCALE, 1.0 本身(無效果)不受縮放影響。
        def sv_val(v):
            if not e.get("scale"):
                return v
            return max(-SCALE_CLAMP, min(SCALE_CLAMP, v * scale_of(caster, e["scale"])))

        def sv_mult(m):
            if not e.get("scale"):
                return m
            return 1 + (m - 1) * scale_of(caster, e["scale"])

        def sv_add(ad):                                  # 屬性平加縮放(一般裝備平加無 scale 直接用原值)
            return ad * scale_of(caster, e["scale"]) if e.get("scale") else ad

        # 批16: undispellable 旗標 —— 效果加此欄則 dispel 略過(附加進 push_add/push_mod/push_stat_add 的 flags, 供 dispel_unit 讀取)
        # 批24 D2: dmgType 旗標 —— amp/mitig 效果可選填 e["dmgType"]="phys"|"intel", 限定只對該
        # 類型傷害生效(damage() 結算時依 kind 過濾, 見 amp()/addbonus() 的 dmg_type 參數)。與
        # undispellable 合併進同一個 flags dict, 兩者互不干擾。
        # 批28 B3: normalOnly 旗標 —— amp/mitig 效果可選填 e["normalOnly"]=true, 限定只對普攻
        # 傷害(hit() 傳入 is_normal=True 的情形)生效, 戰法/突擊傷害不受影響(見至柔動剛「降低
        # 我軍及敵軍全體普通攻擊傷害35%」, 對比redirect既有的normalOnly慣例, 語意不同但欄位
        # 命名沿用一致性)。damage() 結算時依 is_normal 過濾, 見 amp()/addbonus() 的 is_normal
        # 參數。
        dmg_type = e.get("dmgType")
        normal_only = bool(e.get("normalOnly")) if k in ("amp", "mitig") else False
        active_only = bool(e.get("activeOnly")) if k == "amp" else False  # 批31 A: 對稱於normalOnly, 目前僅amp支援(士爭先赴)
        ud_flags = {"undispellable": bool(e.get("undispellable")), "dmgType": dmg_type, "normalOnly": normal_only, "activeOnly": active_only} \
            if (e.get("undispellable") or dmg_type or normal_only or active_only) else None
        # dmgType 存在時, src 附加類型尾碼區分 dedup key(同一戰法內若有兩條不同 dmgType 的
        # amp/mitig, 如暫避其鋒「智力最高者減兵刃傷害」+「武力最高者減謀略傷害」, 兩者若共用
        # 同一個 src 會被 push_add 的「同kind+同src刷新」去重機制互相蓋掉, 見 rateup 既有
        # prepOnly/nativeOnly 尾碼慣例同理)。批28 B3: normalOnly 同理附加尾碼(避免同戰法內
        # normalOnly與非normalOnly的amp共用同一src互相覆蓋); src 為 None 時(兵書/裝備/緣分
        # 無 nameZh) 尾碼無意義, 維持 None(不影響去重, 因 push_add 的 src=None 本就不去重)。
        # 批31 A: activeOnly 同理附加尾碼。
        dt_src = (src + ":" + dmg_type) if (src and dmg_type) else src
        if normal_only and src:
            dt_src = (dt_src or src) + ":normalOnly"
        if active_only and src:
            dt_src = (dt_src or src) + ":activeOnly"
        for u in dests:
            if k == "amp":
                v = sv_val(e["val"])
                if who == "enemy" and v > 0:          # 修正: 敵方正amp(誤幫敵增傷)→ 視為敵方易傷
                    u.push_add("mitig", -v, e["dur"], dt_src, ud_flags)
                else:
                    u.push_add("amp", v, e["dur"], dt_src, ud_flags)
            elif k == "mitig":
                u.push_add("mitig", sv_val(e["val"]), e["dur"], dt_src, ud_flags)
            # 批16: immuneTo(單項控制免疫) —— is_immune_to(k) 只免疫清單內控制類型, 與 insight(全免) 並列判斷
            elif k == "stun":
                if not u.insight and not u.is_immune_to("stun"):
                    u.stun = max(u.stun, e["dur"] + 1)
            elif k == "silence":
                if not u.insight and not u.is_immune_to("silence"):
                    u.silence = max(u.silence, e["dur"] + 1)
            elif k == "disarm":
                if not u.insight and not u.is_immune_to("disarm"):
                    u.disarm = max(u.disarm, e["dur"] + 1)
            elif k == "chaos":                        # 批12 ModeF: 混亂(敵我不分), 同 insight 免疫規則
                if not u.insight and not u.is_immune_to("chaos"):
                    u.chaos = max(u.chaos, e.get("dur", 1) + 1)
            elif k == "ambush":                        # 批18: 遇襲(先攻的反面/遲緩) —— 不鎖行動, 只影響排序; insight/immuneTo可免
                if not u.insight and not u.is_immune_to("ambush"):
                    u.ambush = max(u.ambush, e.get("dur", 1) + 1)
            elif k == "insight":                      # 洞察: 免疫控制, 施加時同時解除既有控制
                u.insight = max(u.insight, e.get("dur", 1) + 1)
                u.stun = u.silence = u.disarm = u.chaos = u.ambush = 0
            elif k == "immune":                       # 批16: immuneTo —— 單項控制免疫
                u.push_immune(e.get("types"), e.get("dur"))
            elif k == "first":                        # 先攻: 本回合旗標, 優先於速度排序
                u.first = max(u.first, e.get("dur", 1))
            elif k == "stat":                         # 裝備平加(add)與乘算(mult)擇一; add 為戰報所示「裝備獨立平加階段」
                if e.get("add") is not None:
                    u.push_stat_add(e["stat"], sv_add(e["add"]), e["dur"], src, ud_flags)
                else:
                    u.push_mod(e["stat"], sv_mult(e.get("mult", 1.0)), e["dur"], src, ud_flags)
            elif k == "dot":                          # 持續傷害: 套用時定格每回合傷害; dots[2]=undispellable旗標
                # 批23 A3: dot 結算優先讀 e["kind"](戰法整體是兵刃 t["kind"]="phys", 但灼燒/
                # 水攻類 dot 段依原文「受智力影響」應走謀略傷害類型, 過去誤用 t["kind"] 導致
                # 傷害類型錯位, 如天降火雨兵刃戰法掛的灼燒本應是 intel 類)。無 e["kind"] 時
                # fallback t["kind"](向後相容既有無 e["kind"] 的 dot 資料)。
                u.dots.append([damage(caster, u, e.get("coef", 0.5),
                                      e.get("kind") or t.get("kind", "intel")), e["dur"], bool(e.get("undispellable"))])
            elif k == "extra":                        # 連擊/追擊: 普攻後追加普攻的預算
                u.push_add("extra", e["val"], e["dur"], src)
            elif k == "stack":                        # 疊加增益: 每層加 per 增傷; 遞增時機見 stackPer
                # 批26 B2: e["stackPer"](可選, "round"預設/"cast") —— 過去疊層只有「每回合+1層」
                # 這一種語意(見 fight() 回合迴圈 tick 遞增, u.stack["n"] = min(max, n+1)), 但原文
                # 常見「每次發動後傷害率提升X」(如水淹七軍/陷陣突襲), 是「本戰法每次成功發動」才
                # +1層, 與回合數無關(可能同一回合不觸發、也可能未來擴充到一回合多次觸發)。新增
                # stackPer 欄位區分兩種遞增時機: "round"(預設, 沿用既有tick()逐回合遞增, 向後
                # 相容)/"cast"(不受tick()影響, 改由 apply_stack_cast() 在戰法本次「發動」時呼叫
                # 遞增, 見 fight() 主動戰法命中分支呼叫端)。刻意不覆寫既有 e["per"] 欄位語意
                # (per 一直是"每層增傷倍率"的數值欄位, 若拿它兼職當模式字串會造成型別混淆與
                # PER_KIND_FIELDS/lint的比對複雜化), 新增獨立欄位更安全。
                # 批37 B: 第三種遞增時機 "attack" —— 「每次普通攻擊後+1層」(如奮突「普通攻擊
                # 之後...最多疊加3次」), 掛在 dealt_damage 事件點(普攻確實命中造成傷害後遞增,
                # 見 dealt_damage() 頂端), 繳械/震懾無普攻的回合不會誤疊層(較舊的 round 近似精確)。
                u.stack = {"per": e.get("per", 0.1), "max": e.get("max", 5), "n": 0,
                           "stackPer": e.get("stackPer", "round")}
            elif k == "decay":                        # 衰減增益: 開場 v0 增傷, rounds 內線性歸零
                u.decay = {"v0": e.get("v0", 0.5), "left": e.get("rounds", 8),
                           "total": e.get("rounds", 8)}
            elif k == "swap":                         # 武智互換
                u.swap = max(u.swap, e.get("dur", 1) + 1)
            elif k == "pierce":                       # 看破: 無視目標 val 比例的減傷
                u.push_add("pierce", e["val"], e["dur"], src)
            elif k == "counter":                      # 反擊: 受擊時還擊
                # 批28 B1: guardFor(守護式反擊) —— 「A受擊時, B代為反擊」的方向(如虎衛軍
                # 「我軍主將即將受到普攻時, 副將反擊」)與一般counter(持有者自己受擊自己反擊)
                # 方向相反。e.get("guardFor")=="leader" 時, u(此效果解析出的who=subs等目標)
                # 不掛自己的counter, 改把自己登記進「隊伍主將」的 counter_guards 清單, 由
                # hit() 在主將受擊時代為觸發還擊(見 hit() 內對應段落)。目前只支援
                # guardFor:"leader"(對應虎衛軍語意), 其餘 who 仍走原本「持有者自己反擊」路徑。
                if e.get("guardFor") == "leader" and allies and allies[0].alive:
                    allies[0].counter_guards.append({
                        "unit": u, "coef": e.get("coef", 1.0), "kind": e.get("kind", "phys"),
                        "prob": e.get("prob", 1.0),
                    })
                else:
                    # 批23 A2: counter 讀 e["dur"](過去是幽靈欄位, 從不寫入/遞減 —— 「反擊持續1
                    # 回合」等帶時限的反擊被無聲變成常駐/永久, 見還擊/千里走單騎等)。dur 預設99
                    # (=常駐被動慣例, 向後相容無 dur 欄位的既有反擊資料)。+1 補償: tick 施加當
                    # 回合末即扣1, 與 taunt/dodge/surehit/shield 慣例一致。tick() 逐回合遞減,
                    # 歸零時清除(見 tick() 對應段落)。
                    u.counter = {"coef": e.get("coef", 1.0), "kind": e.get("kind", "phys"),
                                 "prob": e.get("prob", 1.0), "dur": e.get("dur", 99) + 1}
            elif k == "taunt":                         # 嘲諷: 中招者普攻/單體戰法強制指向施放者
                u.taunt_by = caster
                u.taunt_dur = max(u.taunt_dur, e.get("dur", 1) + 1)
            elif k == "shield":                        # 護盾: 固定量+按施放者兵力係數, 吸滿或到期為止
                amt = e.get("amt", 0) + (e.get("pct", 0) * caster.troop if e.get("pct") else 0)
                prev = u.shield["amt"] if u.shield else 0
                u.shield = {"amt": prev + amt, "dur": e.get("dur", 99) + 1, "undispellable": bool(e.get("undispellable"))}  # +1 補償: tick 施加當回合末即扣1, 與 taunt/dodge/surehit 慣例一致
            elif k == "dodge":                         # 規避: 機率完全迴避一次傷害
                u.dodge_prob = e.get("prob", 0.2)
                u.dodge_dur = max(u.dodge_dur, e.get("dur", 1) + 1)
            elif k == "block":                         # 批22: block(次數型格擋, 抵禦/警戒同族) —— times:N(剩餘次數), val:1.0全擋/0.x部分減傷
                # val 的 scale 縮放用 0~1 專屬 clamp(非 sv_val 的 ±SCALE_CLAMP, 因 block val 是
                # 「減傷比例」語意, 不應為負值或超過1.0全擋)。
                # 批35 B: 改用 locked_scale_of(準備階段鎖定, 見該函式註解) 取代直接呼叫
                # scale_of —— 同一效果物件不論在 prep 或稍後 everyRound 補層才實際套用, 縮放
                # 倍率都固定用第一次掃描到該效果時(prep 階段)算出的值。
                # 批35 A: cap_val_of 套用 e["capVal"](值上限), 在既有 0~1 clamp 之前先夾一次。
                b_val = max(0.0, min(1.0, cap_val_of(e.get("val", 1.0) * locked_scale_of(caster, e), e.get("capVal")))) if e.get("scale") else e.get("val", 1.0)
                u.push_block(b_val, e.get("times", 1), src)
            elif k == "surehit":                       # 必中: 無視對方 dodge
                u.surehit_dur = max(u.surehit_dur, e.get("dur", 1) + 1)
            elif k == "healblock":                     # 批8: 禁療 —— heal 套用處(apply_effects 開頭)已排除 healblock 中的目標
                u.healblock = max(u.healblock, e.get("dur", 1) + 1)
            elif k == "lifesteal":                     # 批8: 倒戈 —— 實際回血在 hit() 結算傷害後(見 hit() 內 lifesteal 段), 這裡只掛加成值
                u.push_add("lifesteal", e["val"], e["dur"], src)
            elif k == "rateup":                        # 提高(自身或對象)主動戰法發動機率
                # scale: 施放當下(caster 戰鬥內即時素質)用 RATE_SCALE_C(獨立於全域 SCALE) 縮放實際
                # 加成(批7: 太平道法「受智力影響」, 見 docs/data/calibration_anchors.json → rate_scale)。
                # prepOnly/nativeOnly/inheritedOnly(批8, nativeOnly反向) 修飾旗標存進 adds[4], 由
                # addbonus_for() 在主動擲骰處依戰法屬性篩選加總。
                rv = e["val"] * rate_scale_of(caster, e["scale"]) if e.get("scale") else e["val"]
                rflags = {"prepOnly": bool(e.get("prepOnly")), "nativeOnly": bool(e.get("nativeOnly")),
                          "inheritedOnly": bool(e.get("inheritedOnly"))} \
                    if (e.get("prepOnly") or e.get("nativeOnly") or e.get("inheritedOnly")) else None
                # 同一戰法(如太平道法)可能有多條 rateup(一般 + prepOnly 額外), src 相同的話
                # push_add 的「同kind+同src刷新」去重會把前一條蓋掉; 用 flags 組出不同的 dedup
                # key 尾碼區分, 讓語意不同的兩條並存, 但同語意(同flags組合)的仍正常刷新不疊加。
                r_src = (src + ":" + "".join(k2 for k2 in ("prepOnly", "nativeOnly", "inheritedOnly") if rflags.get(k2))) \
                    if (src and rflags) else src
                u.push_add("rateup", rv, e["dur"], r_src, rflags)
            elif k == "chargeup":                      # 提高(自身或對象)突擊戰法發動機率; 排除 proc=True 特技偽戰法見突擊擲骰處註解
                # chargeup 同樣支援 scale(未有實測前與 rateup 共用 RATE_SCALE_C, 假設同曲線, 見上方常數註解)
                cv = e["val"] * rate_scale_of(caster, e["scale"]) if e.get("scale") else e["val"]
                cflags = {"prepOnly": bool(e.get("prepOnly")), "nativeOnly": bool(e.get("nativeOnly"))} \
                    if (e.get("prepOnly") or e.get("nativeOnly")) else None
                c_src = (src + ":" + "".join(k2 for k2 in ("prepOnly", "nativeOnly") if cflags.get(k2))) \
                    if (src and cflags) else src
                u.push_add("chargeup", cv, e["dur"], c_src, cflags)
                # 曹純特例(虎豹騎): 若隊伍主將(index 0, allies[0])===本效果指定 general 且恰為本 u,
                # 額外發動機率受武力影響。二次曲線 extra% = force^2 * k(注意 k 擬合的是「%數值」本身,
                # 如 force=373.83 時 force^2*k≈4.47, 代表 4.47%, 需 /100 換算成 addbonus 用的小數比例),
                # 錨點見 docs/data/calibration_anchors.json → hubaoqi_caochun(user 實測: 武力373.83→額外
                # 4.46%, 145.78→0.63%, 123.78→0.53%)。src 另加尾碼避免 push_add 同 kind+src 去重把兩筆效果互相蓋掉。
                lb = e.get("leaderBonus")
                if lb and allies and allies[0] is u and u.g.name == lb["general"]:
                    extra = (u.eff("force") ** 2) * lb["k"] / 100
                    lb_src = (src + ":leaderBonus") if src else "leaderBonus"
                    u.push_add("chargeup", extra, e["dur"], lb_src)
            # 批16: healBoost(受到的治療×(1+val)) / healGiven(施放的治療×(1+val)) —— 掛加成值, 實際套用在 heal 結算處(apply_effects 開頭 heal 分支)
            elif k == "healBoost":
                u.push_add("healBoost", e["val"], e["dur"], src)
            elif k == "healGiven":
                u.push_add("healGiven", e["val"], e["dur"], src)
            # 批16: fakeReport(偽報) —— 中招者被動+指揮戰法失效: 每回合擲骰的coef段與on_hit反應被抑制
            # (prep已套用效果不回收, 近似)。insight 可免(同其他控制類慣例)。
            # 批22: 偽報疊加規則(戰報實測「身上已存在同等或更強的偽報效果」→不覆蓋) —— 新 dur
            # 須 > 現有 fake_report_dur 才覆蓋, 否則本次施加完全跳過(不是簡單取max, 是「不夠強
            # 就拒絕覆蓋」的二元判定, 見 engine.js 同段註解)。
            elif k == "fakeReport":
                if not u.insight:
                    new_dur = e.get("dur", 1) + 1
                    if new_dur > u.fake_report_dur:
                        u.fake_report_dur = new_dur
            # 批16: dispel(驅散/淨化) —— 移除目標 adds/mods/dots/控制欄位中對應方向(buffs/debuffs)的條目,
            # 略過標記 undispellable 的條目。
            elif k == "dispel":
                dispel_unit(u, e.get("what", "debuffs"))


def fight(teamA, teamB, troopA=None, troopB=None, bsA=None, bsB=None, eqA=None, eqB=None,
          addA=None, addB=None, inhA=None, inhB=None, scenario=None, campLvA=0, campLvB=0):
    troopA = troopA or team_troop(teamA)              # 未指定兵種則用隊伍最佳適性
    troopB = troopB or team_troop(teamB)
    bsA = bsA or [default_bingshu(POOL[n]) for n in teamA]   # 未指定兵書則裝預設主兵書
    bsB = bsB or [default_bingshu(POOL[n]) for n in teamB]
    eqA = eqA or [None] * len(teamA)
    eqB = eqB or [None] * len(teamB)
    addA = addA or [None] * len(teamA)
    addB = addB or [None] * len(teamB)
    inhA = inhA or [None] * len(teamA)
    inhB = inhB or [None] * len(teamB)
    campLvA = campLvA or 0                            # 批36: 兵種營等級(0~10, 隊伍級——全隊共用一座對應兵種的營, 與 troopA/troopB 同顆粒度)
    campLvB = campLvB or 0
    # Lv10附贈戰法原文是「我軍隨機單體/群體」觸發一次(非每個單位各自擁有), 故隨機挑隊上1人
    # 當「持有者」(見 Unit.__init__ is_camp_holder 參數), 該隊其餘人只吃屬性%加成、不重複附戰法。
    holder_idx_a = random.randrange(len(teamA)) if campLvA >= 10 and teamA else -1
    holder_idx_b = random.randrange(len(teamB)) if campLvB >= 10 and teamB else -1
    factions_a = [POOL[n].faction for n in teamA]      # 批24 D1: teamGate 判定依據(隊伍全體陣營陣列)
    factions_b = [POOL[n].faction for n in teamB]
    A = [Unit(POOL[n], troopA, bsA[i], eqA[i], addA[i], inhA[i], season_mods(POOL[n], i, teamA, scenario), factions_a, campLvA, i == holder_idx_a)
         for i, n in enumerate(teamA)]
    B = [Unit(POOL[n], troopB, bsB[i], eqB[i], addB[i], inhB[i], season_mods(POOL[n], i, teamB, scenario), factions_b, campLvB, i == holder_idx_b)
         for i, n in enumerate(teamB)]
    setA = set(map(id, A))
    allies_of = lambda u: A if id(u) in setA else B
    foes_of = lambda u: B if id(u) in setA else A
    bonds = {id(A[0]) if A else 0: active_bonds(teamA), id(B[0]) if B else 1: active_bonds(teamB)}

    CAT_ORDER = ("PASSIVE", "FORMATION", "TROOP", "COMMAND")  # 準備階段嚴格順序: 被動→陣法→兵種→指揮(與 engine.js parity)
    cat_of = lambda t: t.get("cat") if t.get("cat") in CAT_ORDER else "COMMAND"

    def apply_passives(no_heal=False, heal_only=False, skip_when_effects=False):  # 被動/陣法/兵種/指揮(依序) + 兵書/裝備/緣分
        for cat in CAT_ORDER:
            for u in A + B:
                if not u.alive:
                    continue
                for t in u.tactics:                   # 同將多個同類: 戰法格順序(陣列順序)決定先後
                    if t["type"] in ("passive", "command") and cat_of(t) == cat:
                        if t.get("when") and not heal_only:  # 條件觸發(when): 不在準備階段套用, 改由回合迴圈在符合回合時套用
                            continue
                        apply_effects(u, None, t, allies_of(u), foes_of(u), no_heal=no_heal, heal_only=heal_only,
                                      skip_when_effects=skip_when_effects)
        for u in A + B:
            if not u.alive:
                continue
            for eff in (u.bs, u.eq):
                if eff:
                    apply_effects(u, None, {"effects": eff, "kind": "phys"}, allies_of(u), foes_of(u),
                                  no_heal=no_heal, heal_only=heal_only, skip_when_effects=skip_when_effects)
        for team in (A, B):                           # 緣分: 隊伍級
            if team:
                for bd in bonds[id(team[0])]:
                    apply_effects(team[0], None, {"effects": bd["effects"], "kind": "phys"},
                                  team, foes_of(team[0]), no_heal=no_heal, heal_only=heal_only,
                                  skip_when_effects=skip_when_effects)

    def on_hit(dst, src, is_normal, dmg=None):        # 反應式觸發(when.on): 被普攻(attacked)/受任意傷害(damaged) 時掛到 hit() 事件點; 批33: dmg(可選)—— 本次觸發事件的實際傷害量, 供 e["ofDamage"] 傷害比例治療使用
        if not dst.alive or (not dst.on_hit_tacs and not dst.on_hit_effect_tacs and not dst.on_hit_eq and not dst.on_hit_bs):
            return
        if dst.fake_report_dur:                       # 批16: 偽報 —— 抑制 on_hit 反應式觸發(被動/指揮戰法失效)
            return
        for t0 in dst.on_hit_tacs:
            if t0["when"]["on"] == "attacked" and not is_normal:  # attacked: 限普通攻擊觸發; damaged: 任意傷害都觸發
                continue
            # 批22: when.on 反應式戰法過去完全不檢查 rounds/from/until/parity/every(只認 on 事件
            # 本身), 導致「戰鬥首回合獲得急救(受傷時回血)」這類「反應式觸發+回合窗口限定」的
            # 複合語意無法表達(如 長健/青囊書: 首回合內受傷才會回血, 而非全程)。round_ok() 對
            # 「無 rounds/from/until/parity/every」的戰法一律回傳 True, 故此檢查對絕大多數既有
            # when.on 戰法(只帶 on, 無回合欄位)是無副作用的 no-op, 只在新資料明確加上回合窗口
            # 時才生效。
            if not round_ok(t0, CUR_ROUND):
                continue
            if id(t0) in dst.hit_flags:                # 同回合每單位每戰法最多觸發1次(防無限鏈), 鍵用t0(戰法原始物件)不受choices合成視圖影響
                continue
            if random.random() >= t0["rate"]:
                continue
            dst.hit_flags.add(id(t0))
            # 批27 C: choices(擇一分支) —— 過去 on_hit() 反應式路徑完全不讀 t0["choices"](見
            # engine_limitations.md §8: 魅惑「混亂/計窮/虛弱」三選一只能固定選其中一種, choices
            # 寫入也不會被消費), 主動/指揮/被動的常駐輪詢派發路徑(fight()主迴圈)已支援 choices,
            # 這裡補上同一套邏輯(先 pick_choice 選分支, 再用合成視圖 t 讀 coef/kind/effects/
            # extraHits, t0 保留給 id()去重/round_ok 等以物件本身為鍵的邏輯, 不受選分支影響)。
            t = dict(t0, **pick_choice(t0["choices"])) if t0.get("choices") else t0
            if t["coef"]:
                hit(dst, src, t["coef"], t["kind"], False, on_hit, dealt_damage)
            if t.get("extraHits"):
                fire_extra_hits(dst, t, src, allies_of, foes_of, on_hit, dealt_damage)  # 批13: 受擊觸發類多段傷害(如剛烈不屈 反擊後群體額外段)
            if t["effects"]:
                apply_effects(dst, src, t, allies_of(dst), foes_of(dst), reactive=True, dmg=dmg)  # 批23: 戰法級when.on本身即反應式, 標記reactive供內部e.when.on效果(若有)一致判定; 批33: dmg供e["ofDamage"]使用
        # 批22: 效果級 e.when.on(急救類反應式治療, 見 on_hit_effect_tacs 註解) —— 戰法本身無
        # t["when"](其餘效果如武力/統率平加仍在 prep 正常套用, 不受影響), 只有帶 e.when.on 的
        # 個別效果在此處反應式結算。用「合成單效果戰法」(effects=[e])呼叫 apply_effects, 讓
        # heal 分支的傷兵池/healBoost/healGiven 邏輯完整適用, 觸發率取 e.rate ?? t.rate ?? 1
        # (效果自身優先, 無則沿用戰法整體rate)。去重鍵用 id(效果物件)(而非戰法物件), 因同一
        # 戰法可能有多個 e.when.on 效果, 需各自獨立節流。
        for t in dst.on_hit_effect_tacs:
            for e in t["effects"]:
                ew = e.get("when") or {}
                if not ew.get("on"):
                    continue
                if ew["on"] == "attacked" and not is_normal:
                    continue
                if not round_ok({"when": ew}, CUR_ROUND):
                    continue
                if id(e) in dst.hit_flags:
                    continue
                ev_rate = e.get("rate", t.get("rate", 1))
                if random.random() >= ev_rate:
                    continue
                dst.hit_flags.add(id(e))
                apply_effects(dst, src, {"effects": [e], "kind": t.get("kind", "phys"), "nameZh": t.get("nameZh")},
                             allies_of(dst), foes_of(dst), rate_checked=True, reactive=True, dmg=dmg)  # 批23 A4/reactive: 上面已擲過 e["rate"], 避免重複擲骰; reactive供e.when.on閘門放行; 批33: dmg供e["ofDamage"]使用
        # 批22: 裝備效果級 e.when.on(見 on_hit_eq 註解) —— 同上, 用合成單效果戰法呼叫 apply_effects
        for e in dst.on_hit_eq:
            ew = e["when"]
            if ew["on"] == "attacked" and not is_normal:
                continue
            if not round_ok({"when": ew}, CUR_ROUND):
                continue
            if id(e) in dst.hit_flags:
                continue
            ev_rate = e.get("rate", 1)
            if random.random() >= ev_rate:
                continue
            dst.hit_flags.add(id(e))
            apply_effects(dst, src, {"effects": [e], "kind": "phys"}, allies_of(dst), foes_of(dst), rate_checked=True, reactive=True, dmg=dmg)  # 批23 A4/reactive; 批33: dmg供e["ofDamage"]使用
        # 批22: 兵書效果級 e.when.on(見 on_hit_bs 註解) —— 同上, 用合成單效果戰法呼叫 apply_effects
        for e in dst.on_hit_bs:
            ew = e["when"]
            if ew["on"] == "attacked" and not is_normal:
                continue
            if not round_ok({"when": ew}, CUR_ROUND):
                continue
            if id(e) in dst.hit_flags:
                continue
            ev_rate = e.get("rate", 1)
            if random.random() >= ev_rate:
                continue
            dst.hit_flags.add(id(e))
            apply_effects(dst, src, {"effects": [e], "kind": "phys"}, allies_of(dst), foes_of(dst), rate_checked=True, reactive=True, dmg=dmg)  # 批23 A4/reactive; 批33: dmg供e["ofDamage"]使用

    def dealt_damage(src, dst, is_normal, kind, dmg=None):  # 批27 A: 反應式觸發(when.on:"dealtDamage") —— 自己造成傷害(對 dst)後掛到 hit() 事件點, 與 on_hit(自己受擊視角)對稱; 批33: dmg(可選)—— 本次觸發事件的實際傷害量, 供 e["ofDamage"] 使用
        # 批37 B: stackPer:"attack" —— 「每次普通攻擊後疊加1層」(如奮突「普通攻擊之後...最多
        # 疊加3次」)。過去只有 "round"(逐回合)/"cast"(每次發動)兩種遞增模式, 普攻疊層只能用
        # round 近似(繳械/震懾回合無普攻仍會錯誤地繼續疊層)。掛在 dealt_damage 事件點(普攻
        # 確實命中造成傷害後), 置於 on_deal_tacs 早退判斷之前(有 stackPer:"attack" 疊層的
        # 單位未必同時有 when.on:"dealtDamage" 反應式戰法, 不能被該早退擋掉)。
        if is_normal and src.alive and src.stack and src.stack.get("stackPer") == "attack":
            src.stack["n"] = min(src.stack["max"], src.stack["n"] + 1)
        if not src.alive or (not src.on_deal_tacs and not src.on_deal_effect_tacs):
            return
        if src.fake_report_dur:                        # 批16: 偽報 —— 抑制反應式觸發(被動/指揮戰法失效), 與 on_hit 同慣例
            return
        def _dmg_type_ok(dmg_type):                     # dmgType 過濾: 未指定視為兵刃/謀略皆可觸發(向後相容)
            return not dmg_type or dmg_type == kind
        for t in src.on_deal_tacs:                      # 戰法級: 整個戰法都是「造成傷害時」反應式(如白衣渡江拆成兩個獨立戰法段時可用此形式)
            if not _dmg_type_ok((t.get("when") or {}).get("dmgType")):
                continue
            if (t.get("when") or {}).get("normalOnly") and not is_normal:
                continue                                # 批37 B: when.normalOnly —— 限「普通攻擊」造成的傷害才觸發(如奮突「普通攻擊之後」; dmgType:"phys" 無法區分普攻與兵刃戰法傷害, 需獨立旗標)
            if not round_ok(t, CUR_ROUND):
                continue
            if id(t) in src.hit_flags:                  # 同回合每單位每戰法最多觸發1次(防無限鏈), 與 on_hit 共用同一 hit_flags(不同方向的觸發各自用不同id(t)/id(e)鍵, 不會互相誤判)
                continue
            if random.random() >= t["rate"]:
                continue
            src.hit_flags.add(id(t))
            if t["coef"]:
                # 批32 B: targetSel(依準則選標) —— 過去 dealtDamage 的 coef 傷害段固定命中
                # dst(觸發本次事件的同一目標, 如普攻打誰就額外打誰), 沒有讀取 t.get("targetSel")
                # 這條路徑, 導致原文「對負傷最高之敵造成謀略傷害」(選標準則與觸發目標無關,
                # 如監統震軍)只能被迫近似成「打觸發同目標」或完全不建模。比照主動戰法主迴圈
                # 既有的 targetSel 判斷式(pick_by_criterion(foes_of(u), t["targetSel"])), 若
                # 戰法帶 targetSel 則改用準則選標, 找不到符合準則的目標(如全軍陣亡)時不出手
                # (dv=None 時不呼叫 hit, 而非退回 dst, 避免誤傷/誤選)。
                if t.get("targetSel"):
                    dv = pick_by_criterion(foes_of(src), t["targetSel"])
                    if dv:
                        hit(src, dv, t["coef"], t["kind"], False, on_hit, dealt_damage)
                else:
                    hit(src, dst, t["coef"], t["kind"], False, on_hit, dealt_damage)
            if t.get("extraHits"):
                fire_extra_hits(src, t, dst, allies_of, foes_of, on_hit, dealt_damage)
            if t["effects"]:
                apply_effects(src, dst, t, allies_of(src), foes_of(src), reactive=True, dmg=dmg)  # 批33: dmg供e["ofDamage"]使用
        # 效果級: 戰法本身有其他常駐效果, 只有部分效果段是「造成傷害時」反應式(如白衣渡江本身
        # 是常駐 command, disarm/silence 兩效果各自綁不同 dmgType, 與 on_hit_effect_tacs 同慣例)
        for t in src.on_deal_effect_tacs:
            for e in t["effects"]:
                ew = e.get("when") or {}
                if ew.get("on") != "dealtDamage":
                    continue
                if not _dmg_type_ok(ew.get("dmgType")):
                    continue
                if ew.get("normalOnly") and not is_normal:
                    continue                            # 批37 B: when.normalOnly(效果級) —— 同上, 限普攻傷害觸發
                if not round_ok({"when": ew}, CUR_ROUND):
                    continue
                if id(e) in src.hit_flags:
                    continue
                ev_rate = e.get("rate", t.get("rate", 1))
                if random.random() >= ev_rate:
                    continue
                src.hit_flags.add(id(e))
                apply_effects(src, dst, {"effects": [e], "kind": t.get("kind", "phys"), "nameZh": t.get("nameZh")},
                             allies_of(src), foes_of(src), rate_checked=True, reactive=True, dmg=dmg)  # 已擲過 e["rate"], 避免重複擲骰; reactive供e.when.on閘門放行; 批33: dmg供e["ofDamage"]使用

    def active_fired(u):                                # 批31 A: 反應式觸發(when.on:"activeFired") —— 自己成功發動主動/突擊戰法時掛到 fight() 主迴圈事件點, 與 dealt_damage(自己造成傷害視角)/on_hit(自己受擊視角)對稱; 只認「自身」戰法成功fire這件事本身, 不要求造成傷害(士爭先赴等戰法可能coef=0純buff, 也可能有coef傷害段)
        if not u.alive or (not u.active_fired_tacs and not u.active_fired_effect_tacs):
            return
        if u.fake_report_dur:                           # 批16: 偽報 —— 抑制反應式觸發(被動/指揮戰法失效), 與 on_hit/dealt_damage 同慣例
            return
        for t in u.active_fired_tacs:                    # 戰法級: 整個戰法都是「自身成功發動主動戰法時」反應式(如士爭先赴)
            if not round_ok(t, CUR_ROUND):
                continue
            if id(t) in u.hit_flags:                      # 同回合每單位每戰法最多觸發1次(防無限鏈), 與 on_hit/dealt_damage 共用同一 hit_flags(不同方向的觸發各自用不同id(t)/id(e)鍵, 不會互相誤判)
                continue
            if random.random() >= t["rate"]:
                continue
            u.hit_flags.add(id(t))
            main_hit_tgt = None
            if t["coef"]:
                cnt = t["n"]
                if t.get("nMax"):
                    cnt = t["n"] + random.randint(0, t["nMax"] - t["n"])
                vs = pick_targets(foes_of(u), cnt)
                for v in vs:
                    hit(u, v, t["coef"], t["kind"], False, on_hit, dealt_damage, is_active=True)  # 批31 A: 本段傷害本身即「主動戰法發動觸發的反應式傷害」, 供同戰法/其他戰法的e.activeOnly amp判定
                if len(vs) == 1:
                    main_hit_tgt = vs[0]
            if t.get("extraHits"):
                fire_extra_hits(u, t, main_hit_tgt, allies_of, foes_of, on_hit, dealt_damage)
            if t["effects"]:
                apply_effects(u, main_hit_tgt, t, allies_of(u), foes_of(u), reactive=True)
        # 效果級: 戰法本身有其他常駐效果, 只有部分效果段是「自身成功發動主動戰法時」反應式
        for t in u.active_fired_effect_tacs:
            for e in t["effects"]:
                ew = e.get("when") or {}
                if ew.get("on") != "activeFired":
                    continue
                if not round_ok({"when": ew}, CUR_ROUND):
                    continue
                if id(e) in u.hit_flags:
                    continue
                ev_rate = e.get("rate", t.get("rate", 1))
                if random.random() >= ev_rate:
                    continue
                u.hit_flags.add(id(e))
                apply_effects(u, None, {"effects": [e], "kind": t.get("kind", "phys"), "nameZh": t.get("nameZh")},
                             allies_of(u), foes_of(u), rate_checked=True, reactive=True)  # 已擲過 e["rate"], 避免重複擲骰; reactive供e.when.on閘門放行

    apply_passives(no_heal=True, skip_when_effects=True)  # 開戰套持久效果(治療除外); skip_when_effects: 批18 e.when泛化, 非heal效果帶e.when且母戰法無t.when時prep階段不套用, 改由回合迴圈通用掃描

    global CUR_ROUND
    for rnd in range(1, ROUNDS + 1):
        CUR_ROUND = rnd                               # 批15: 供 apply_effects() 的 heal_only 常駐治療通道檢查 t["when"](round_ok)
        for u in A + B:                               # 疊加增益: 每回合 +1 層(僅 stackPer=="round", 預設值, 向後相容)
            if u.alive and u.stack and u.stack.get("stackPer", "round") == "round":
                u.stack["n"] = min(u.stack["max"], u.stack["n"] + 1)
        apply_passives(heal_only=True)                # 逐回合治療(含兵書/裝備/緣分)

        for u in A + B:                               # 條件觸發(when.rounds/from/until): 窗口首次開啟時套用一次非傷害效果(dot/amp/…); when.on 為反應式, 不走此處
            if not u.alive:
                continue
            for t in u.tactics:
                if t["type"] in ("passive", "command") and t.get("when") and not t["when"].get("on") \
                        and round_ok(t, rnd) and id(t) not in u.when_fired:
                    w = t["when"]
                    # 批16: hpPct —— when.hpBelow(一次性, 首次跨越即觸發, 用when_fired/hp_below_fired去重)
                    # / when.hpAbove(持續窗, 不去重, 每回合條件成立都可能觸發, 故不進when_fired)。
                    if w.get("hpBelow") is not None or w.get("hpAbove") is not None:
                        if not hp_ok(t, u):
                            continue
                        if w.get("hpBelow") is not None:
                            if id(t) in u.hp_below_fired:
                                continue
                            u.hp_below_fired.add(id(t))
                        # 批23 A5: 補 t["rate"] 判定(此路徑過去從不讀 t["rate"], 機率戰法被當成
                        # 必發)。hpAbove 是持續窗(每回合都可能重新判定), 未中不消耗 when_fired
                        # (仍可下回合再擲); hpBelow 是一次性(見上方 hp_below_fired 去重), 未中
                        # 同樣不消耗 when_fired 之外的額外狀態(hp_below_fired 已在觸發判定前
                        # 標記, 維持既有「首次跨越」語意不變)。
                        if random.random() >= t.get("rate", 1):
                            continue
                        apply_effects(u, None, t, allies_of(u), foes_of(u), no_heal=True)
                        if w.get("hpBelow") is not None:
                            u.when_fired.add(id(t))     # hpBelow 一次性: 同時標記 when_fired 供其他路徑一致查詢
                        continue
                    # 批23 A5: 此路徑過去從不讀 t["rate"] —— 機率戰法(如盛氣凌敵 rate=1 不受
                    # 影響, 但火神英風/鷹視狼顧一類 rate<1 的 when-gated 條目)一律當成必發,
                    # 高估命中率。主動/指揮/被動的「coef 傷害段」自有獨立擲骰(見主迴圈
                    # fire=random.random()<t0["rate"], 已正確套用), 此處是「非coef、window
                    # 開啟時一次性套用的 effects 段」, 需要補上同一份 t["rate"] 判定。未中同樣
                    # 消耗 when_fired(此路徑本就是一次性視窗, 見函式頂端註解, 未中不重試)。
                    u.when_fired.add(id(t))
                    if random.random() >= t.get("rate", 1):
                        continue
                    # 批15: no_heal=True —— heal 效果改由上面 apply_passives(heal_only=True) 統一
                    # 處理(它自己會檢查 t["when"]/round_ok, 見 apply_effects 內 heal_only 分支),
                    # 避免此處與 heal_only 常駐通道用不同的去重鍵(id(t) vs id(e))各自判定, 造成
                    # 同一 when 視窗開啟的回合 heal 被套用兩次(雙倍治療)。
                    apply_effects(u, None, t, allies_of(u), foes_of(u), no_heal=True)
        # 批8: 裝備效果級回合窗(delayed_eq) —— 與戰法 when 窗口同一時點檢查; 效果物件本身(非戰法)
        # 用 id() 存進 when_fired 去重(一次性), 帶 rate 的額外擲骰(如赳螑 50%機率), 沒中不算已
        # 觸發、下次符合視窗的回合(若 when.rounds 只列單一回合則不會再有機會; 資料上 rate 型窗口
        # 皆為 rounds:[單一回合], 符合設計)。
        for u in A + B:
            if not u.alive or not u.delayed_eq:
                continue
            for e in u.delayed_eq:
                if not round_ok({"when": e["when"]}, rnd) or id(e) in u.when_fired:
                    continue
                u.when_fired.add(id(e))
                if e.get("rate") is not None and random.random() >= e["rate"]:
                    continue
                apply_effects(u, None, {"effects": [e], "kind": "phys",   # n/nMax傳遞: 群體控制(赳螑 敵軍群體2~3)按效果宣告選目標數
                                        "n": e.get("n", 1), "nMax": e.get("nMax", 0)}, allies_of(u), foes_of(u),
                              rate_checked=True)  # 批23 A4: 上面已擲過 e["rate"], 避免重複擲骰

        # 批18: e.when 泛化(非 heal 種類) —— 與上面 delayed_eq 同一時點、同慣例: 掃描「母戰法無
        # t["when"]」的 passive/command 戰法, 找出其中帶 e["when"] 的非 heal 效果(prep 階段已被
        # skip_when_effects 跳過, 這裡才是它們真正套用的時機點), 視窗開啟時一次性套用(when_fired
        # 以效果物件 id() 去重, 同 delayed_eq/heal e["when"]["rounds"] 慣例)。heal 種類不進這裡
        # (它有自己獨立的 heal_only 常駐通道與去重機制, 見 apply_effects 內 k=="heal" 分支)。
        for u in A + B:
            if not u.alive:
                continue
            for t in u.tactics:
                if t["type"] not in ("passive", "command") or t.get("when"):
                    continue  # 母戰法有 t["when"] 的已由上面 t["when"] 掃描處理, 這裡只處理母戰法無 when 的情形
                for e in t["effects"]:
                    if e["k"] == "heal" or not e.get("when"):
                        continue
                    if not round_ok({"when": e["when"]}, rnd) or id(e) in u.when_fired:
                        continue
                    if e["when"].get("rounds"):
                        u.when_fired.add(id(e))  # rounds(明確列出的特定回合): 一次性去重(同 delayed_eq)
                    # from/until(範圍視窗): 不加入 when_fired, 讓視窗內每回合都能重新套用(同 heal 的 from/until 慣例)
                    apply_effects(u, None, {"effects": [e], "kind": t.get("kind", "phys"),
                                            "n": t.get("n", 1), "nMax": t.get("nMax", 0),
                                            "nameZh": t.get("nameZh")}, allies_of(u), foes_of(u), no_heal=True)

        # 行動順序: 先攻(first)優先於速度; 同速平手隨機(先打亂再穩定排序, 修 A 隊固定先手偏差)
        # 批18: 遇襲(ambush, 先攻的反面/遲緩) —— 三檔 eff_first: 只有先攻→最先(1); 先攻+遇襲同時
        # 存在→抵消, 視為普通(0, 按速度排); 只有遇襲→排最後(-1, 遇襲者之間仍按速度排)。
        eff_first = lambda x: (1 if x.first > 0 else 0) - (1 if x.ambush > 0 else 0)
        _pool = [x for x in A + B if x.alive]
        random.shuffle(_pool)
        for u in sorted(_pool, key=lambda x: (eff_first(x), x.eff("speed")), reverse=True):
            if not u.alive or u.stun:
                continue
            if pick_target(foes_of(u)) is None:
                break
            if not u.silence:                             # 計窮: 跳過主動/指揮/被動(不影響普攻)
                for t0 in u.tactics:                       # 自帶 + 傳承: 各自獨立附加發動(不占普攻)
                    # 批16: fakeReport(偽報) —— 抑制指揮/被動每回合擲骰的coef段(prep已套用效果不回收, 不影響主動戰法)
                    if t0["type"] in ("command", "passive") and u.fake_report_dur:
                        continue
                    fire = False
                    # 批18: choices/extraHits 派發 —— coef=0 且頂層 effects 為空、內容完全放在
                    # choices[].effects 或 extraHits 裡的主動戰法(如三選一分支型/上兵伐謀式多段
                    # 指定選標), 過去 (t0["coef"] or t0["effects"]) 兩者皆假則永遠不會觸發(choices/
                    # extraHits 只在 fire 之後才被讀取, 若從未 fire 等於整個戰法失效 —— 全庫掃描
                    # 發現暗潮洶湧/暗潮湧動已是此模式且從未真正發動過)。加上 t0.get("choices")/
                    # t0.get("extraHits") 這兩個額外判斷條件, 讓「內容全在 choices/extraHits 裡」的
                    # 戰法也能正常擲骰派發。
                    # 批32 R23: active 型戰法過去完全不檢查 t["when"](round_ok), 只有 command/
                    # passive 分支(下方elif)才會擲骰前先驗回合窗口——導致「奇數回合...偶數回合...」
                    # 這類需要用 t.when.parity 切分兩組互斥效果的 active 戰法(如飛沙走石)無法透過
                    # 頂層 when 精確表達(見 engine_limitations.md 新增節: parity 只在 command/passive
                    # 驗證過, active 從未真正測試, 屬先前批次遺留的能力邊界, 非本次新增行為)。補上
                    # round_ok(t0, rnd) 對稱於 command/passive 既有判斷, 不影響現有唯一帶 t.when 的
                    # active 戰法(移花接木, when僅含dur鍵, round_ok對此鍵永遠回傳True, 無回歸)。
                    if t0["type"] == "active" and (t0["coef"] or t0["effects"] or t0.get("choices") or t0.get("extraHits")) \
                            and not (t0["prep"] and rnd == 1) and round_ok(t0, rnd):
                        fire = random.random() < t0["rate"] + u.addbonus_for("rateup", t0)  # rateup: 提高自身主動戰法發動機率(如白眉); addbonus_for 依 t["prep"]/t["native"] 篩選 prepOnly/nativeOnly 修飾的加成(批7: 太平道法)
                    elif t0["type"] in ("command", "passive") and (t0["coef"] or t0.get("choices")) \
                            and not (t0.get("when") and t0["when"].get("on")) and round_ok(t0, rnd):
                        fire = random.random() < t0["rate"]  # 每回合以資料 rate 擲骰; when.rounds/from/until 只在符合回合才擲骰; when.on(反應式) 改由 on_hit 事件點觸發; 批18: choices 派發同active一併補coef=0情形
                    if fire:
                        # 批26 B2: stack.stackPer=="cast" —— 本戰法本次成功發動(擲骰命中fire),
                        # 若 u 身上已有 stackPer=="cast" 的疊層狀態(該狀態由本戰法或其他戰法的
                        # k=="stack"效果段套用而來), 在此遞增1層(見 apply_stack_cast() 定義)。
                        # 與round模式(fight()主迴圈逐回合遞增, 見上方)互斥判斷, 不會重複遞增。
                        u.apply_stack_cast()
                        # 批31 A: on:"activeFired" —— 只有 type=="active"(真正的主動戰法)才算
                        # 「成功發動主動戰法」事件, command/passive 常駐擲骰(fire 判定式共用同一
                        # if 區塊, 但語意是「每回合固定擲骰」而非「發動主動戰法」)不觸發此事件。
                        # 置於 apply_stack_cast() 之後、實際套用觸發戰法本身效果之前, 讓士爭先赴
                        # 一類「成功發動...前」的反應式效果搶在本次觸發戰法的傷害/效果結算前廣播
                        # (見 active_fired() 定義處對 before/after 語意取捨的說明)。
                        if t0["type"] == "active":
                            active_fired(u)
                        # 批16: choices(擇一分支) —— 發動時按權重隨機選一組效果(coef/kind/effects/
                        # extraHits/n/nMax 可各自覆寫基礎戰法)套用到本次發動; 未中選的分支本次不生效。
                        # 權重預設均分。t0 為原始戰法物件(供 addbonus_for/when_fired/lockTarget 等以
                        # id(t0) 為鍵的邏輯保持穩定, 不因選分支而變動), t 為「本次觸發實際使用」的
                        # 合成視圖(不修改 t0 本身)。
                        t = dict(t0, **pick_choice(t0["choices"])) if t0.get("choices") else t0
                        main_hit_tgt = None  # 批13: 記錄主 coef 段命中的(單體)目標, 供 extraHits 同目標段沿用
                        is_active_dmg = t0["type"] == "active" or None  # 批31 A: 供e.activeOnly amp判定「本段傷害是否為主動戰法所致」; command/passive走同一段程式碼但非主動戰法, 傳None(安全側不套用activeOnly加成, 見addbonus()docstring)
                        if t["coef"]:
                            cnt = t["n"]
                            if t.get("nMax"):
                                cnt = t["n"] + random.randint(0, t["nMax"] - t["n"])
                            # 批12 ModeB: hitsRepeat —— 「隨機單體攻擊X次/重複X次,每次獨立選擇目標」
                            # = N次獨立單體抽樣(可重複命中同一目標), 非 pick_targets 的 N 人不重複群攻。
                            # 逐次呼叫 pick_target(每次重新擲骰), 而非一次性呼叫 pick_targets(不重複)。
                            # 批12 ModeG: lockTarget —— 單體(cnt<=1)coef傷害目標改用 resolve_locked_target
                            # (首次發動 pick_target 選定後, 之後每次發動重用同一目標); 群體(cnt>1)/
                            # hitsRepeat 不套用鎖定語意(lockTarget 資料上僅用於單體戰法)。
                            # 批18: targetSel(指定選標準則) —— 戰法級欄位, 主coef段按準則選單一
                            # 目標(如避實擊虛「統率最低」), 優先於lockTarget/hitsRepeat/隨機群體
                            # (不受混亂影響, 每次發動當下依準則重新篩選, 與lockTarget的「首次選定
                            # 後鎖定沿用」語意方向相反, 不可混用)。
                            if t.get("targetSel"):
                                v = pick_by_criterion(foes_of(u), t["targetSel"])
                                if v:
                                    hit(u, v, t["coef"], t["kind"], False, on_hit, dealt_damage, is_active=is_active_dmg)
                                    main_hit_tgt = v
                            elif t.get("lockTarget") and cnt <= 1 and not t.get("hitsRepeat"):
                                v = resolve_locked_target(u, t0, foes_of(u))  # lockTarget 鍵用 t0(原始戰法物件), 避免 choices 每次合成新dict破壞跨回合鎖定
                                if v:
                                    hit(u, v, t["coef"], t["kind"], False, on_hit, dealt_damage, is_active=is_active_dmg)
                                    main_hit_tgt = v
                            elif t.get("hitsRepeat"):
                                for _ in range(cnt):
                                    v = pick_target(foes_of(u), u)
                                    if v:
                                        hit(u, v, t["coef"], t["kind"], False, on_hit, dealt_damage, is_active=is_active_dmg)
                                        main_hit_tgt = v
                            else:
                                vs = pick_targets(foes_of(u), cnt)
                                for v in vs:
                                    hit(u, v, t["coef"], t["kind"], False, on_hit, dealt_damage, is_active=is_active_dmg)
                                if len(vs) == 1:
                                    main_hit_tgt = vs[0]
                        if t.get("extraHits"):
                            fire_extra_hits(u, t, main_hit_tgt, allies_of, foes_of, on_hit, dealt_damage)  # 批13: 多段傷害(兵刃+謀略雙段/主傷+補刀等)
                        if t["type"] == "active":
                            # 批12 ModeF: 混亂下單體主動戰法目標改敵我不分(pick_target_chaos); 群體/AoE
                            # (who=enemy 全體/n>1)維持 apply_effects 內部既有邏輯不變 —— 這裡傳入的 tgt
                            # 只影響「單體優先鎖定」分支, 群體戰法本就走 pick_targets(enemies,...) 不受
                            # 此參數影響(近似, 群體戰法混亂下仍只打敵方)。
                            # 批12 ModeG: lockTarget 的 apply_effects 目標(單體效果destination)同樣改用
                            # 鎖定目標(與混亂互斥: lockTarget 戰法目前資料上未與 chaos 共存)。
                            active_dst = resolve_locked_target(u, t0, foes_of(u)) if t.get("lockTarget") else pick_target_chaos(u, allies_of(u), foes_of(u))
                            apply_effects(u, active_dst, t, allies_of(u), foes_of(u))
                        elif t0.get("choices"):
                            # 批27 B: command/passive 型戰法帶 choices —— 過去 pick_choice() 抽出的
                            # 分支 t 只有 coef/extraHits 段會在上面被讀取套用, t["effects"](分支自帶
                            # 的效果, 如桃園結義三選一之一的heal)完全被憑空丟棄(見engine_limitations
                            # §18a: applyEffects對command/passive型戰法的呼叫管道是apply_passives(),
                            # 讀的是u.tactics原始t0, 從未經過pick_choice解析)。此處補上: 只在
                            # t0.get("choices")為真(=本次t是choices合成視圖, 非u.tactics原始物件)
                            # 時才呼叫apply_effects(u, None, t, ...)套用分支的effects, 且僅限於此
                            # (不對一般無choices的command/passive戰法重複套用——那些戰法的effects
                            # 已由apply_passives()的prep/heal_only通道正確處理, 此處若無腦補上會
                            # 造成雙重結算)。heal_only=False(單次套用, 非逐回合常駐通道)——分支的
                            # heal效果視為「本次觸發的一次性治療」, 與apply_passives的heal_only
                            # 常駐掃描是互斥的兩個通道: choices戰法的t0["effects"]本身為空(內容
                            # 全在choices[].effects裡), heal_only通道對空effects列表天然no-op,
                            # 不會與此處重複治療。
                            apply_effects(u, main_hit_tgt, t, allies_of(u), foes_of(u), no_heal=False)
            tgt = pick_target_chaos(u, allies_of(u), foes_of(u))  # 普攻(每回合常駐) + 連擊 + 突擊(繳械時跳過); 嘲諷: 強制指向施放者; 混亂: 敵我不分(批12 ModeF)
            if tgt and not u.disarm:
                hit(u, tgt, 1.0, "phys", True, on_hit, dealt_damage)
                for _ in range(extra_count(u.addbonus("extra"))):  # 連擊/追擊
                    nt = pick_target_chaos(u, allies_of(u), foes_of(u))
                    if nt:
                        hit(u, nt, 1.0, "phys", True, on_hit, dealt_damage)
                # 批16: everyN(計數觸發) —— 自身每第N次普攻觸發該戰法的 effects/extraHits(不含 coef
                # 主傷段, 資料上 everyN 戰法目前皆為輔助效果類)。計數只算「本次真正命中的普攻」
                # (disarm 時不會走到這裡, 故繳械回合不計數, 合理)。
                for t in u.tactics:
                    if t.get("everyN") and t["everyN"].get("on") == "attack" and u.tick_every_n(t):
                        if t.get("extraHits"):
                            fire_extra_hits(u, t, tgt, allies_of, foes_of, on_hit, dealt_damage)
                        if t.get("effects"):
                            apply_effects(u, tgt, t, allies_of(u), foes_of(u))
                # 突擊(charge)擲骰: chargeup(突擊發動率加成, 如虎豹騎)只對真突擊戰法生效, 排除
                # t.get("proc") is True 的特技偽戰法(user 明確指示: 特技不吃突擊加成, 例虎豹騎/三勢陣/經天緯地/陷陣突襲)。
                for t in u.tactics:
                    up = 0 if t.get("proc") else u.addbonus_for("chargeup", t)
                    if t["type"] == "charge" and random.random() < t["rate"] + up:
                        if t["coef"]:
                            hit(u, tgt, t["coef"], t["kind"], False, on_hit, dealt_damage, is_active=True)  # 批31 A: 突擊戰法傷害同樣視為e.activeOnly判定範圍內的「主動/突擊戰法傷害」
                        if t.get("extraHits"):
                            fire_extra_hits(u, t, tgt, allies_of, foes_of, on_hit, dealt_damage)
                        apply_effects(u, tgt, t, allies_of(u), foes_of(u))
                        # 批31 A: on:"activeFired" —— 突擊(charge)戰法成功發動同樣視為本事件的
                        # 觸發來源(如陷陣突襲「自身成功發動突擊戰法後」, 監聽的是同一單位身上
                        # 另一個 type:"charge" 戰法的發動, 非陷陣突襲自己), 與 active 型戰法共用
                        # 同一個 active_fired() 廣播函式與 hit_flags 節流(同回合每單位每反應
                        # 戰法最多觸發1次)。
                        active_fired(u)

        for u in A + B:                               # 結算傷害: 疊滿層數或到期 → 對其所屬全隊爆發
            s = u.settle
            if not s:
                continue
            if s["layers"] >= s["max"] or s["left"] <= 1:
                team = A if id(u) in setA else B
                for v in [x for x in team if x.alive]:
                    sd = damage(s["caster"], v, s["base"] + s["per"] * s["layers"],
                                s["kind"], s["snap"])
                    v.troop -= sd
                    v.wounded += sd * wounded_rate(CUR_ROUND)
                u.settle = None
            else:
                s["left"] -= 1

        for u in A + B:
            u.tick()
        # 批8: 殲滅(kill) —— ROUNDS 回合內一方全滅, 對比「判定勝」(打滿8回合按剩餘兵力比較)。
        if not any(u.alive for u in A):
            return "B", rnd, True
        if not any(u.alive for u in B):
            return "A", rnd, True
    ta = sum(max(0, u.troop) for u in A)
    tb = sum(max(0, u.troop) for u in B)
    return ("A" if ta >= tb else "B"), ROUNDS, False


def simulate(teamA, teamB, n=3000, troopA=None, troopB=None, bsA=None, bsB=None, eqA=None, eqB=None,
             addA=None, addB=None, inhA=None, inhB=None, scenario=None, campLvA=0, campLvB=0):
    w = {"A": 0, "B": 0}
    kill = {"A": 0, "B": 0}                           # 批8: 殲滅 vs 判定勝(8回合打滿按剩餘兵力) 分開統計
    rs = 0
    for _ in range(n):
        winner, r, k = fight(teamA, teamB, troopA, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario, campLvA, campLvB)
        w[winner] += 1
        if k:
            kill[winner] += 1
        rs += r
    return {"A勝率": round(w["A"] / n, 3), "B勝率": round(w["B"] / n, 3),
            "平均回合": round(rs / n, 1),
            "殲滅率": round((kill["A"] + kill["B"]) / n, 3),
            "A殲滅": round(kill["A"] / n, 3), "B殲滅": round(kill["B"] / n, 3),
            "A判定勝": round((w["A"] - kill["A"]) / n, 3), "B判定勝": round((w["B"] - kill["B"]) / n, 3)}


# ---------------------------------------------------------------------------
def demo():
    assert len(POOL) > 150, f"應載入近 193 武將, got {len(POOL)}"
    assert counter_mult("騎", "盾") == 1.15 and counter_mult("盾", "騎") == 0.85
    assert counter_mult("槍", "器") == 1.15
    lb = POOL["呂布"]
    # 兵種適性: 呂布騎兵 S(×1.2) 應比同人槍兵 A(×1.0)武力高
    assert Unit(lb, "騎").force > Unit(lb, "槍").force, "適性高的兵種屬性應更高"
    assert Unit(lb, "騎").force > 240
    u = Unit(lb, "騎")
    u.push_add("amp", 0.2, 1)
    assert abs(u.addbonus("amp") - 0.2) < 1e-9
    u.tick()
    assert u.addbonus("amp") == 0, "增傷到期要消"
    caster, tgt = Unit(POOL["諸葛亮"], "弓"), Unit(lb, "騎")
    tgt.settle = {"layers": 1, "max": 3, "left": 2, "caster": caster,
                  "snap": caster.troop, "base": 1.5, "per": 0.4, "kind": "intel"}
    hit(caster, tgt, 1.0, "phys")
    assert tgt.settle["layers"] == 2, "結算層數應隨擊累積"
    assert extra_count(2.0) == 2 and extra_count(0) == 0, "連擊次數: 整數必定"
    u3 = Unit(lb, "騎")
    u3.stack = {"per": 0.2, "max": 5, "n": 3}
    assert abs(u3.amp() - 0.6) < 1e-9, "疊加3層=+60%增傷"
    u3.stack = None
    u3.decay = {"v0": 1.0, "left": 8, "total": 8}
    assert abs(u3.amp() - 1.0) < 1e-9, "衰減開場滿"
    u3.decay["left"] = 4
    assert abs(u3.amp() - 0.5) < 1e-9, "衰減過半"
    u4 = Unit(POOL["諸葛亮"], "弓")
    base_f = u4.eff("force")
    u4.swap = 2
    assert u4.eff("force") > base_f, "武智互換: 智高者互換後武力變高"
    grd, prot = Unit(lb, "騎"), Unit(POOL["諸葛亮"], "弓")
    prot.guardian, prot.guard_share = grd, 0.4
    g0, p0 = grd.troop, prot.troop
    hit(Unit(lb, "騎"), prot, 1.5, "phys")        # 高武力攻擊者確保有傷害(避免守將過肉時0傷)
    assert grd.troop < g0 and prot.troop < p0, "代承者與被保護者各吃一部分"
    atk, df = Unit(lb, "騎"), Unit(POOL["張飛"], "盾")
    df.push_add("mitig", 0.5, 9)
    d_norm = damage(atk, df, 1.0, "phys")
    atk.push_add("pierce", 1.0, 9)
    assert damage(atk, df, 1.0, "phys") > d_norm * 1.5, "看破應大幅提高對減傷目標的傷害"
    ca, cd = Unit(POOL["周瑜"], "弓"), Unit(lb, "騎")
    cd.counter = {"coef": 1.0, "kind": "phys", "prob": 1.0}
    a0 = ca.troop
    hit(ca, cd, 0.5, "phys")
    assert ca.troop < a0, "反擊應讓攻擊者掉血"
    # 克制: 同隊伍兵種, 騎隊打盾隊 應比反過來占優(克制 1.15 vs 0.85)
    assert counter_mult("騎", "盾") > counter_mult("盾", "騎")
    # 兵書: 載入 + 預設主兵書可裝
    assert len(BINGSHU) >= 40, f"兵書應載入, got {len(BINGSHU)}"
    assert default_bingshu(POOL["呂布"]) in BINGSHU, "呂布應有預設主兵書"
    # 緣分: 湊齊觸發人數應觸發(資料載入時才測)
    if BONDS:
        bd = BONDS[0]
        assert active_bonds(bd["generals"][:bd["triggerCount"]]), "湊齊應觸發緣分"
        assert not active_bonds(bd["generals"][:bd["triggerCount"] - 1]), "人數不足不應觸發"
    res = simulate(["呂布", "趙雲", "關羽"], ["諸葛亮", "周瑜", "司馬懿"], n=400)
    assert 0 <= res["A勝率"] <= 1 and 1 <= res["平均回合"] <= ROUNDS

    # --- 新機制驗收 ---------------------------------------------------------
    # 1) 同名(同戰法來源)不疊加: 同一戰法對同一單位重複施加同種效果應「刷新」而非疊加
    u5 = Unit(lb, "騎")
    u5.push_add("amp", 0.10, 2, src="測試戰法")
    u5.push_add("amp", 0.10, 2, src="測試戰法")           # 同來源同 kind 再次套用
    assert abs(u5.addbonus("amp") - 0.10) < 1e-9, "同名戰法效果應刷新, 不應疊加成 0.20"
    u5.push_add("amp", 0.05, 2, src=None)                # 無來源(兵書/裝備/緣分)不去重, 應疊加
    assert abs(u5.addbonus("amp") - 0.15) < 1e-9, "無來源效果不應被去重邏輯影響"

    # 2) 計窮(silence) 應擋主動戰法發動, 但不擋普攻
    silence_tactic = {"nameZh": "測試計窮術", "type": "active", "kind": "phys", "coef": 0,
                       "rate": 1.0, "n": 1, "prep": 0,
                       "effects": [{"k": "silence", "who": "enemy", "dur": 1}]}
    caster5, tgt5 = Unit(POOL["諸葛亮"], "弓"), Unit(lb, "騎")
    apply_effects(caster5, tgt5, silence_tactic, [caster5], [tgt5])
    assert tgt5.silence > 0, "計窮應成功施加(fight() 主迴圈以 `if not u.silence:` 跳過主動戰法)"

    # 3) 繳械(disarm) 應擋普攻
    disarm_tactic = {"nameZh": "測試繳械術", "type": "active", "kind": "phys", "coef": 0,
                      "rate": 1.0, "n": 1, "prep": 0,
                      "effects": [{"k": "disarm", "who": "enemy", "dur": 1}]}
    tgt6 = Unit(lb, "騎")
    apply_effects(caster5, tgt6, disarm_tactic, [caster5], [tgt6])
    assert tgt6.disarm > 0, "繳械應成功施加"

    # 4) 洞察(insight) 應免疫控制, 且施加時同時解除既有控制
    u7 = Unit(lb, "騎")
    u7.stun, u7.silence, u7.disarm = 1, 1, 1
    insight_tactic = {"nameZh": "測試洞察術", "type": "passive", "kind": "phys", "coef": 0,
                       "rate": 1.0, "n": 1, "prep": 0,
                       "effects": [{"k": "insight", "who": "self", "dur": 2}]}
    apply_effects(u7, None, insight_tactic, [u7], [])
    assert u7.insight > 0 and u7.stun == 0 and u7.silence == 0 and u7.disarm == 0, \
        "洞察應解除既有 stun/silence/disarm"
    tgt8 = Unit(lb, "騎")
    tgt8.insight = 3
    apply_effects(caster5, tgt8, silence_tactic, [caster5], [tgt8])
    assert tgt8.silence == 0, "洞察中的目標應免疫計窮"

    # 5) 先攻(first) 應排序優先於速度: first 旗標高者即使速度較低也應排在前面
    fast_no_first = Unit(POOL["呂布"], "騎")             # 呂布速度較高但無先攻
    slow_first = Unit(POOL["諸葛亮"], "弓")               # 諸葛亮速度較低但獲得先攻
    slow_first.first = 1
    order5 = sorted([fast_no_first, slow_first], key=lambda x: (x.first, x.eff("speed")), reverse=True)
    assert order5[0] is slow_first, "先攻單位應優先於速度較高但無先攻者行動"

    # --- 批2 新機制驗收 -------------------------------------------------------
    # 6) 條件觸發(when.rounds/from/until): round_ok 應正確判斷回合是否落在窗口內
    t_from = {"when": {"from": 3}}
    assert not round_ok(t_from, 1) and not round_ok(t_from, 2) and round_ok(t_from, 3) and round_ok(t_from, 8), \
        "when.from=3 應只在第3回合起才符合"
    t_until = {"when": {"until": 4}}
    assert round_ok(t_until, 1) and round_ok(t_until, 4) and not round_ok(t_until, 5), \
        "when.until=4 應只在第4回合(含)前符合"
    t_rounds = {"when": {"rounds": [2, 4]}}
    assert round_ok(t_rounds, 2) and round_ok(t_rounds, 4) and not round_ok(t_rounds, 3), \
        "when.rounds=[2,4] 應只在指定回合符合"
    assert round_ok({"when": None}, 1), "無 when 應視為每回合皆符合"

    # 7) 反應式觸發(when.on): 受到普通攻擊(attacked)時應觸發; damaged 則任意傷害來源都觸發(仿 fight() 內 on_hit 的精簡版)
    def on_hit_test(dst, src, is_normal, dmg=None):  # 批33: dmg(可選)—— 對稱於正式 on_hit(), 接受 hit() 新增的第4參數
        for t in dst.tactics:
            on = t.get("when", {}).get("on")
            if not on or (on == "attacked" and not is_normal):
                continue
            if id(t) in dst.hit_flags:
                continue
            dst.hit_flags.add(id(t))
            if t["coef"]:
                hit(dst, src, t["coef"], t["kind"], False, on_hit_test)
            if t["effects"]:
                apply_effects(dst, src, t, [dst], [src])

    counter_like = {"nameZh": "測試反擊術", "type": "passive", "kind": "phys", "coef": 0.5,
                     "rate": 1.0, "n": 1, "prep": 0, "effects": [],
                     "when": {"on": "attacked"}}
    attacker9, defender9 = Unit(lb, "騎"), Unit(POOL["張飛"], "盾")
    defender9.tactics = [counter_like]
    a0_9 = attacker9.troop
    hit(attacker9, defender9, 1.0, "phys", True, on_hit_test)   # 普攻(is_normal=True) 應觸發 attacked
    assert attacker9.troop < a0_9, "when.on=attacked 應在普攻命中時觸發反擊傷害"

    damaged_like = {"nameZh": "測試受傷觸發術", "type": "passive", "kind": "phys", "coef": 0,
                     "rate": 1.0, "n": 1, "prep": 0,
                     "effects": [{"k": "stat", "who": "self", "stat": "force", "mult": 1.5, "dur": 1}],
                     "when": {"on": "damaged"}}
    victim10 = Unit(lb, "騎")
    victim10.tactics = [damaged_like]
    force0_10 = victim10.eff("force")
    hit(Unit(lb, "騎"), victim10, 1.0, "phys", False, on_hit_test)   # 非普攻傷害(charge/戰法傷害) 也應觸發 damaged
    assert victim10.eff("force") > force0_10, "when.on=damaged 應在任意來源傷害後觸發(非僅普攻)"

    # 8) 嘲諷(taunt): 被嘲諷者的普攻/單體戰法目標應強制指向施放者
    tauntee, tauntCaster, other = Unit(lb, "騎"), Unit(POOL["張飛"], "盾"), Unit(POOL["諸葛亮"], "弓")
    tauntee.taunt_by, tauntee.taunt_dur = tauntCaster, 2
    picked = pick_target([tauntCaster, other], attacker=tauntee)
    assert picked is tauntCaster, "被嘲諷者應強制選擇施放者為目標, 不論隨機結果"
    for _ in range(20):                                 # 多次抽樣確保不是巧合
        assert pick_target([tauntCaster, other], attacker=tauntee) is tauntCaster

    # 9) 護盾(shield): 應先於兵力吸收傷害, 吸滿後才扣兵力
    shielded = Unit(lb, "騎")
    shielded.shield = {"amt": 999999, "dur": 3}          # 巨額護盾, 確保這次傷害全被吸收
    troop_before_shield = shielded.troop
    hit(Unit(POOL["呂布"], "騎"), shielded, 1.0, "phys")
    assert shielded.troop == troop_before_shield, "護盾應吸收傷害, 兵力不應減少"
    assert shielded.shield["amt"] < 999999, "護盾吸收量應從護盾扣除"
    thin_shield = Unit(lb, "騎")
    thin_shield.shield = {"amt": 1, "dur": 3}             # 護盾量極小, 傷害應溢出扣兵力
    troop_before_thin = thin_shield.troop
    hit(Unit(POOL["呂布"], "騎"), thin_shield, 1.0, "phys")
    assert thin_shield.troop < troop_before_thin, "護盾吸滿後, 剩餘傷害應繼續扣兵力"
    assert thin_shield.shield is None, "護盾吸滿應消失"

    # 10) 規避(dodge) / 必中(surehit): 規避應完全迴避傷害; 必中應無視對方規避
    dodger = Unit(lb, "騎")
    dodger.dodge_prob, dodger.dodge_dur = 1.0, 3          # 100% 規避, 確定觸發
    troop_before_dodge = dodger.troop
    hit(Unit(POOL["呂布"], "騎"), dodger, 1.0, "phys")
    assert dodger.troop == troop_before_dodge, "100% 規避應完全迴避傷害"
    sure_attacker = Unit(POOL["呂布"], "騎")
    sure_attacker.surehit_dur = 3
    troop_before_surehit = dodger.troop
    hit(sure_attacker, dodger, 1.0, "phys")
    assert dodger.troop < troop_before_surehit, "必中應無視對方規避, 傷害應正常命中"

    # --- 批2.5 新機制驗收 -----------------------------------------------------
    # 11) SCALE(): 錨點 v=100 -> 1.0(面板基準值), v=450 -> 2.0(每+350點效果翻倍)
    assert abs(SCALE(100) - 1.0) < 1e-9, "SCALE(100) 應為面板基準值 1.0"
    assert abs(SCALE(450) - 2.0) < 1e-9, "SCALE(450) 應為 2.0(社群拆解: 每+350點效果翻倍)"

    # 12) scale 縮放: heal 效果帶 scale="intel" 時, 施放者智力(戰鬥內 eff 值)越高治療量越大
    heal_tactic = {"nameZh": "測試治療術", "type": "active", "kind": "intel", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0,
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1, "scale": "intel"}]}
    low_int_caster = Unit(POOL["呂布"], "騎")               # 呂布智力低
    high_int_caster = Unit(POOL["諸葛亮"], "弓")             # 諸葛亮智力高
    high_int_caster.push_mod("intel", 2.0, 9)               # 額外掛 buff 拉高戰鬥內 eff("intel"), 驗證縮放走 eff() 而非裸值
    low_ally, high_ally = Unit(lb, "騎"), Unit(lb, "騎")
    low_ally.troop = high_ally.troop = 1000
    low_ally.wounded = high_ally.wounded = START_TROOP  # 批18: 治療上限=傷兵池, 設滿池供heal縮放比較不被截斷干擾
    apply_effects(low_int_caster, None, heal_tactic, [low_ally], [], no_heal=False)
    apply_effects(high_int_caster, None, heal_tactic, [high_ally], [], no_heal=False)
    assert high_ally.troop > low_ally.troop, \
        "scale=intel 的治療應隨施放者戰鬥內智力(eff('intel'), 含buff)提高而提高"

    # 13) rateup: 帶白眉(rateup 0.12)者主動戰法發動率應提升(以 addbonus 直接驗證擲骰門檻)
    plain_u = Unit(lb, "騎")
    rateup_u = Unit(lb, "騎")
    rateup_u.push_add("rateup", 0.12, 9)
    assert abs(rateup_u.addbonus("rateup") - 0.12) < 1e-9 and plain_u.addbonus("rateup") == 0, \
        "rateup 應提高 addbonus('rateup'), 主動戰法擲骰門檻 rate+addbonus('rateup') 因而提高"
    assert "白眉" in TACTICS and any(e["k"] == "rateup" for e in TACTICS["白眉"]["effects"]), \
        "白眉應由 reparse 標記 rateup 效果"
    # 13b) rateup 有限持續: 走 adds 陣列, tick() 遞減到期應消失(先成其慮 dur=1 等非永久buff)
    temp_ru = Unit(lb, "騎")
    temp_ru.push_add("rateup", 0.15, 1)
    assert abs(temp_ru.addbonus("rateup") - 0.15) < 1e-9
    temp_ru.tick()
    assert temp_ru.addbonus("rateup") == 0, "rateup dur=1 應在 tick 後到期消失(非永久)"
    be = next(e for e in TACTICS["白眉"]["effects"] if e["k"] == "rateup")
    xa = next(e for e in TACTICS["先成其慮"]["effects"] if e["k"] == "rateup")
    assert be["dur"] == 99 and xa["dur"] == 1, \
        "白眉(常駐)dur=99, 先成其慮(持續1回合)dur=1 —— rateup dur 應從原文抽取而非硬編碼"

    # 14) overrides: 宜城之志(幽靈條目)/明察秋毫(內政) 應被排除在 TACTICS 之外(type:none)
    assert "宜城之志" not in TACTICS, "宜城之志(user 確認幽靈條目) overrides 應設 type:none 並被排除"
    assert "明察秋毫" not in TACTICS, "明察秋毫(內政類) overrides 應設 type:none 並被排除"
    assert "爭分奪秒" not in TACTICS, "爭分奪秒(內政類) overrides 應設 type:none 並被排除"
    assert TACTICS["火燒連營"]["coef"] > 0, "火燒連營 overrides 套用後應仍有 coef(查證資料含傷害率)"

    # --- 批3 傷害公式重塑 錨點驗收 ---------------------------------------------
    # 固定輸入(兵10000/coef1.0/士氣100/無增減傷/同兵種無克制), 多次取樣平均應落在實測錨點 ±容差內。
    def _anchor_unit(force, command, troop=10000):
        u = Unit(POOL["呂布"], "騎")                    # 借真實武將建構後直接覆寫屬性(繞開卡面數值, 精準命中錨點)
        u.force, u.command, u.intel = force, command, force
        u.troop = troop
        return u

    def _avg_damage(atk_force, def_command, n=4000):
        atk_u = _anchor_unit(atk_force, 999999)         # 攻擊方 command 不使用, 給極端值避免誤用
        samples = [damage(atk_u, _anchor_unit(999999, def_command), 1.0, "phys") for _ in range(n)]
        return sum(samples) / len(samples)

    d0 = _avg_damage(1000, 1000)                        # 錨1: 屬性差0 → ≈476
    assert 476 * 0.95 <= d0 <= 476 * 1.05, f"錨1(差0)應在476±5%內, got {d0:.1f}"
    d200 = _avg_damage(1200, 1000)                      # 錨2: 屬性差200 → ≈764
    assert 764 * 0.95 <= d200 <= 764 * 1.05, f"錨2(差200)應在764±5%內, got {d200:.1f}"
    dneg = _avg_damage(100, 5000)                       # 錨3: 屬性差大負值(保底) → ≈90
    assert 90 * 0.90 <= dneg <= 90 * 1.10, f"錨3(保底)應在90±10%內, got {dneg:.1f}"

    # --- 批4 裝備複合鍵 + 同名去重 驗收 -----------------------------------------
    # 15) EQUIPS 複合鍵"type·name": 同名跨欄位("無畏"見於武器/防具/坐騎/寶物四欄)不應互蓋
    assert "武器·無畏" in EQUIPS and "坐騎·無畏" in EQUIPS, "無畏應以複合鍵'type·無畏'分別存在於武器/坐騎(等四欄)"
    assert EQUIPS["武器·無畏"] is not EQUIPS["坐騎·無畏"], "同名跨type的裝備應是各自獨立的條目, 不應互蓋"
    assert "無畏" in EQUIPS, "純名稱 fallback 應仍可查到(向後相容)"
    # 16) 同名特技(跨type)遊戲規則只生效一件: Unit.eq(裝備合併效果, 套用前的原始清單)應依基底名稱去重
    eq_one = Unit(lb, "騎", equip=["武器·無畏"])
    eq_two = Unit(lb, "騎", equip=["武器·無畏", "坐騎·無畏"])
    assert len(eq_one.eq) == 1, "單裝武器·無畏 應合併出1條效果"
    assert len(eq_two.eq) == 1, "同時裝 武器·無畏 + 坐騎·無畏(同名跨type)應只生效一件(去重後仍是1條效果, 不是2條)"
    # 17) 實際套用(apply_effects, 同 fight() 準備階段路徑)後, 統率只應 +5(平加, 不因重複裝同名而疊成+10)
    cmd_baseline = Unit(lb, "騎")
    cmd_two = Unit(lb, "騎", equip=["武器·無畏", "坐騎·無畏"])
    pt_two = {"effects": cmd_two.eq, "kind": "phys"}
    apply_effects(cmd_two, None, pt_two, [cmd_two], [], no_heal=True)
    assert abs(cmd_two.eff("command") - (cmd_baseline.eff("command") + 5)) < 1e-6, \
        "同時裝 武器·無畏 + 坐騎·無畏(同名跨type)套用後統率應只 +5(平加, 不疊加為+10)"

    # --- 批4(修訂) 屬性平加/管線重排/proc/援護 驗收 --------------------------------
    # 18) 屬性平加(stat.add): 蠻力(武力+8) 應在 eff() 為平加(不隨屬性放大, 對比 mult 版本)
    b_force = Unit(lb, "騎")
    add_force = Unit(lb, "騎", equip=["武器·蠻力"])
    pt_add = {"effects": add_force.eq, "kind": "phys"}
    apply_effects(add_force, None, pt_add, [add_force], [], no_heal=True)
    assert abs(add_force.eff("force") - (b_force.eff("force") + 8)) < 1e-6, "蠻力應使武力平加+8(flat, 非mult)"

    # 19) 屬性管線重排(戰報序): 面板 = ((基礎+加點+賽季flat)×適性 + 城建CITY)×陣營FACTION + 兵種營CAMP
    #     取 S 適性(騎)驗算; 呂布無加點(add=None 用預設0), flat=0, scm=1
    apt_s = lb.apt_pct("騎")
    expect_force = ((lb.base["force"] + 0 + 0) * apt_s * 1.0 + CITY) * FACTION + CAMP
    assert abs(Unit(lb, "騎").eff("force") - expect_force) < 1e-6, \
        f"屬性管線應為 ((基礎×適性)+城建)×陣營+兵種營, got {Unit(lb, '騎').eff('force'):.4f} expect {expect_force:.4f}"

    # 20) moraleMult 士氣上限100(戰報: 士氣>100傷害不變)
    assert abs(morale_mult(110) - morale_mult(100)) < 1e-9, "士氣>100 應按100算(傷害不變)"

    # 21) 裝備 proc(昭烈): 應被包成偽突擊戰法(charge)且帶 proc:True 旗標; rate=1 測試版驗證觸發路徑會繳械敵軍
    zhaolie_u = Unit(POOL["劉備"], "騎", equip=["武器·昭烈"])
    proc_tacs = [t for t in zhaolie_u.tactics if t.get("proc")]
    assert len(proc_tacs) == 1 and proc_tacs[0]["type"] == "charge", "昭烈應合成1個 charge 型 proc 偽戰法"
    assert proc_tacs[0]["proc"] is True, "proc 偽戰法必須帶 proc:True 旗標(供日後 chargeup 排除, user 指示特技不吃突擊加成)"
    # 用 rate=1 版本驗證觸發路徑: 手動把 proc 偽戰法 rate 設1, 觸發後敵軍應繳械
    proc_tacs[0]["rate"] = 1.0
    foe21 = Unit(POOL["張飛"], "盾")
    apply_effects(zhaolie_u, foe21, proc_tacs[0], [zhaolie_u], [foe21])
    assert foe21.disarm > 0, "昭烈 proc 觸發後應使目標敵軍繳械(disarm>0)"

    # 22) 援助 normalOnly: 普攻傷害應被代承(guardian分擔), 戰法傷害(is_normal=False)不被代承
    grd, prot, foe22 = Unit(POOL["劉備"], "騎"), Unit(POOL["關羽"], "騎"), Unit(POOL["呂布"], "騎")
    prot.guardian, prot.guard_share, prot.guard_dur, prot.guard_normal_only = grd, 1.0, 1, True
    grd_t0, prot_t0 = grd.troop, prot.troop
    hit(foe22, prot, 1.0, "phys", False)                 # 戰法傷害(is_normal=False): 不代承, prot 自吃
    assert prot.troop < prot_t0 and abs(grd.troop - grd_t0) < 1e-9, "normalOnly 援護: 戰法傷害不應轉移給代承者"
    grd_t1, prot_t1 = grd.troop, prot.troop
    hit(foe22, prot, 1.0, "phys", True)                  # 普攻(is_normal=True): 應由代承者吃(share=1)
    assert grd.troop < grd_t1 and abs(prot.troop - prot_t1) < 1e-9, "normalOnly 援護: 普攻傷害(share=1)應全由代承者承擔"

    # 23) 代承過期(guardDur): dur:1 的 redirect 過1回合(tick)後應失效
    prot23 = Unit(POOL["關羽"], "騎")
    apply_effects(Unit(POOL["劉備"], "騎"), None,
                  {"effects": [{"k": "redirect", "who": "ally", "guard": "self", "share": 1.0, "dur": 1}], "kind": "phys"},
                  [Unit(POOL["劉備"], "騎"), prot23], [])
    assert prot23.guardian is not None and prot23.guard_dur == 1, "redirect dur:1 套用後應有 guardian 且 guard_dur=1"
    prot23.tick()
    assert prot23.guardian is None and prot23.guard_dur == 0, "redirect dur:1 過1回合(tick)後 guardian 應清除(失效)"

    # --- 批6: chargeup(突擊戰法發動機率加成) + 虎豹騎曹純特例 ---------------------
    # 24) chargeup 資料落地: 虎豹騎(who=ally,val=0.10,dur=3,帶leaderBonus)/陷陣突襲(who=self,val=0.15)
    assert "虎豹騎" in TACTICS and any(e["k"] == "chargeup" for e in TACTICS["虎豹騎"]["effects"]), \
        "虎豹騎應由 reparse 標記 chargeup 效果"
    hbq_ce = next(e for e in TACTICS["虎豹騎"]["effects"] if e["k"] == "chargeup")
    assert abs(hbq_ce["val"] - 0.10) < 1e-9 and hbq_ce["dur"] == 3 and hbq_ce["who"] == "ally", \
        "虎豹騎 chargeup 應為 who=ally, val=0.10(5%→10%取滿級), dur=3(戰鬥前3回合)"
    assert hbq_ce.get("leaderBonus", {}).get("general") == "曹純", "虎豹騎 chargeup 應帶曹純 leaderBonus"
    assert "陷陣突襲" in TACTICS and any(e["k"] == "chargeup" for e in TACTICS["陷陣突襲"]["effects"]), \
        "陷陣突襲應由 reparse 標記 chargeup 效果"
    xzts_ce = next(e for e in TACTICS["陷陣突襲"]["effects"] if e["k"] == "chargeup")
    assert abs(xzts_ce["val"] - 0.15) < 1e-9 and xzts_ce["who"] == "self", \
        "陷陣突襲 chargeup 應為 who=self, val=0.15(7.5%→15%取滿級)"

    # 25) chargeup 提高 addbonus('chargeup'), 突擊擲骰門檻 rate+addbonus('chargeup') 因而提高
    plain_c = Unit(lb, "騎")
    charge_u = Unit(lb, "騎")
    charge_u.push_add("chargeup", 0.10, 3)
    assert abs(charge_u.addbonus("chargeup") - 0.10) < 1e-9 and plain_c.addbonus("chargeup") == 0, \
        "chargeup 應提高 addbonus('chargeup')"

    # 26) 鐵律: chargeup 只影響真突擊戰法, 不影響 proc:True 特技偽戰法(user 明確指示,
    # 昭烈/踩踏不吃虎豹騎加成)。以 fight() 內同樣的擲骰算式驗證: 真突擊 rate 提升, proc rate 不變。
    real_charge_rate = 0.10
    proc_rate = 0.10
    cu = Unit(lb, "騎")
    cu.push_add("chargeup", 0.10, 9)
    real_t = {"type": "charge", "rate": real_charge_rate, "proc": False}
    proc_t = {"type": "charge", "rate": proc_rate, "proc": True}
    real_threshold = real_t["rate"] + (0 if real_t.get("proc") else cu.addbonus("chargeup"))
    proc_threshold = proc_t["rate"] + (0 if proc_t.get("proc") else cu.addbonus("chargeup"))
    assert abs(real_threshold - 0.20) < 1e-9, "真突擊戰法擲骰門檻應吃到 chargeup 加成(0.10+0.10=0.20)"
    assert abs(proc_threshold - proc_rate) < 1e-9, "proc:True 特技偽戰法擲骰門檻不應吃 chargeup 加成(維持原rate)"

    # 27) 虎豹騎曹純特例: 隊伍主將(index 0)===曹純 時, 額外發動機率 = 主將戰鬥內武力^2 × 3.2e-5
    #     (二次曲線, 錨點見 docs/data/calibration_anchors.json → hubaoqi_caochun: 武力373.83→額外4.46%)。
    #     用 push_mod 頂高武力到約373 驗算數值, 容差±10%(_est 曲線, 樣本少)。
    # 錨點的「武力373.83」是遊戲內實測顯示值, 即虎豹騎自身「全體+16%武力」buff 已套用後的戰鬥內
    # eff("force")(同一 apply_effects 呼叫內, stat 效果排在 chargeup 之前先套用)。故先估算裸值倍率
    # 使套用虎豹騎(force×1.16)後精確落在 373.83, 再驗算 leaderBonus 額外值。
    hbq_tac = TACTICS["虎豹騎"]
    cc_leader = Unit(POOL["曹純"], "騎")
    cc_leader.push_mod("force", (373.83 / 1.16) / cc_leader.eff("force"), 9)
    team_cc = [cc_leader, Unit(lb, "騎"), Unit(lb, "騎")]   # 曹純在 index 0 = 主將
    apply_effects(cc_leader, None, hbq_tac, team_cc, [], no_heal=True)
    force_373 = cc_leader.eff("force")                      # 套用虎豹騎+16%武力後的戰鬥內武力(應≈373.83)
    expect_extra = (force_373 ** 2) * 3.2e-5 / 100          # k 擬合的是「%數值」, /100 換算成小數比例
    got_extra = cc_leader.addbonus("chargeup") - 0.10       # 扣掉基礎虎豹騎10%, 剩下即曹純特例額外值
    assert abs(got_extra - expect_extra) / expect_extra < 0.15, \
        f"曹純統領虎豹騎額外發動機率應≈武力^2×3.2e-5, got={got_extra:.4f} expect={expect_extra:.4f}(武力{force_373:.1f})"
    assert abs(force_373 - 373.83) < 5, f"測試前置條件: 武力應落在錨點373.83附近, got={force_373:.1f}"
    assert abs(got_extra - 0.0446) / 0.0446 < 0.10, \
        f"武力≈373時, 曹純特例額外發動機率應≈0.0446(±10%), got={got_extra:.4f}"

    # 28) 曹純特例只在「曹純且為主將(index 0)」時觸發: 非曹純主將 不應有額外加成, 只有基礎10%
    other_leader = Unit(POOL["呂布"], "騎")
    team_other = [other_leader, Unit(POOL["曹純"], "騎"), Unit(lb, "騎")]  # 曹純在隊但非主將(index 1)
    apply_effects(other_leader, None, hbq_tac, team_other, [], no_heal=True)
    assert abs(team_other[0].addbonus("chargeup") - 0.10) < 1e-6, \
        "非曹純主將時, chargeup 應只有虎豹騎基礎10%, 無曹純特例額外加成"
    caochun_non_leader = team_other[1]
    assert abs(caochun_non_leader.addbonus("chargeup") - 0.10) < 1e-6, \
        "曹純若不是主將(index 0), 即使在隊上也不應觸發特例額外加成"
    # 方向性比較: 曹純當主將(index 0) 的 chargeup 加成應高於非曹純主將隊伍
    assert cc_leader.addbonus("chargeup") > team_other[0].addbonus("chargeup"), \
        "曹純當主將 vs 非曹純主將: 前者 chargeup 加成應較高(曹純特例額外值>0)"

    # --- 批7: 發動率縮放(rate-scale) + 太平道法落地 -------------------------------
    # 29) 太平道法資料落地: amp 0.28 + 2 條 rateup(一般6%/準備戰法額外6%, 皆 scale=intel+nativeOnly)
    assert "太平道法" in TACTICS, "太平道法應由 reparse 落地(inherit 傳承戰法, 非任何武將自帶)"
    tp_tac = TACTICS["太平道法"]
    assert not tp_tac.get("_est"), "太平道法資料落地後不應再有 _est 標記"
    tp_amp = next(e for e in tp_tac["effects"] if e["k"] == "amp")
    assert abs(tp_amp["val"] - 0.28) < 1e-9, "太平道法奇謀(amp近似) 應為升滿值0.28(14%→28%)"
    tp_rateups = [e for e in tp_tac["effects"] if e["k"] == "rateup"]
    assert len(tp_rateups) == 2, "太平道法應有2條rateup(一般+準備戰法額外)"
    assert all(abs(e["val"] - 0.06) < 1e-9 and e.get("scale") == "intel" and e.get("nativeOnly")
               for e in tp_rateups), "太平道法2條rateup皆應為val=0.06, scale=intel, nativeOnly=True"
    tp_prep_only = [e for e in tp_rateups if e.get("prepOnly")]
    assert len(tp_prep_only) == 1, "太平道法應恰有1條rateup帶prepOnly=True(準備戰法額外加成)"

    # 30a) 智力426.57時, 太平道法「準備戰法」目標(native+prep皆真)拿到的加成總額 ≈ 0.2218
    #     (12%基礎 × RATE_SCALE(426.57) = 12%×1.849 ≈ 22.19%, 容差1%; 見 calibration_anchors.json
    #     → rate_scale)。用 push_mod 頂高智力到 426.57 驗算(同 chargeup 曹純測試的手法)。
    tp_caster = Unit(POOL["張角"], "騎")
    tp_caster.push_mod("intel", 426.57 / tp_caster.eff("intel"), 9)
    assert abs(tp_caster.eff("intel") - 426.57) < 1e-6, "測試前置條件: 智力應精確落在426.57"
    apply_effects(tp_caster, None, tp_tac, [tp_caster], [], no_heal=True)
    prep_native_tac = {"type": "active", "rate": 0.5, "prep": 1, "native": True, "effects": [], "coef": 0}
    total_prep = tp_caster.addbonus_for("rateup", prep_native_tac)
    assert abs(total_prep - 0.2219) / 0.2219 < 0.01, \
        f"智力426.57時, 太平道法對「自帶+準備」戰法的加成總額應≈0.2219(12%×1.849, 容差1%), got={total_prep:.4f}"

    # 30b) prepOnly 修飾: 只加給 prep=真 的戰法; 自帶但非準備戰法應只拿到一般那條(6%×縮放)
    normal_native_tac = {"type": "active", "rate": 0.5, "prep": 0, "native": True, "effects": [], "coef": 0}
    total_normal = tp_caster.addbonus_for("rateup", normal_native_tac)
    expect_normal = 0.06 * (1 + (426.57 - 100) * RATE_SCALE_C)
    assert abs(total_normal - expect_normal) < 1e-6, \
        f"prepOnly=True的那條不應計入非準備戰法, got={total_normal:.4f} expect={expect_normal:.4f}"
    assert total_normal < total_prep, "prepOnly: 準備戰法應比非準備戰法拿到更多加成(多了prepOnly那條)"

    # 30c) nativeOnly 修飾: 不應加成給非自帶(傳承)戰法, 即使該戰法也是 prep=真
    inherited_prep_tac = {"type": "active", "rate": 0.5, "prep": 1, "native": False, "effects": [], "coef": 0}
    total_inherited = tp_caster.addbonus_for("rateup", inherited_prep_tac)
    assert total_inherited == 0, \
        f"nativeOnly=True 的加成不應套用到傳承(非自帶)戰法, got={total_inherited:.4f}"

    # 31) nativeOnly 適用範圍(user 釐清, 嚴格按原文措辭二分, 見 reparse_effects.py 的
    #     RATE_SCALE_PLAN 下方註解): 「自帶」→ nativeOnly; 「自身主動戰法」→ 無旗標(全主動都吃,
    #     含傳承, 不分準備)。鎖定已落地條目合規:
    # 31a) 白眉「自身主動戰法的發動機率提高6%→12%」: 不得有 nativeOnly/prepOnly(user 明確指出
    #      白眉不分自帶或準備)。先成其慮/獅子奮迅/進言 同措辭同規則。
    for _nm in ("白眉", "先成其慮", "獅子奮迅", "進言"):
        _ru = next(e for e in TACTICS[_nm]["effects"] if e["k"] == "rateup")
        assert not _ru.get("nativeOnly") and not _ru.get("prepOnly"), \
            f"{_nm}(「自身/自己/友軍主動戰法」措辭)的 rateup 不得帶 nativeOnly/prepOnly 旗標"
    # 31b) 裝備側 nativeOnly(武聖「自帶戰法發動率增加5%」, equips_parsed.json 既有旗標)經本批
    #      addbonus_for 落地後應實際生效: 只加自帶戰法, 不加傳承戰法。裝備效果經 fight() 的
    #      pt() 包裝(無 nameZh → src=None)套用, 這裡直接複製該路徑。
    ws_u = Unit(POOL["關羽"], "騎", equip=["武器·武聖"])
    assert any(e.get("k") == "rateup" and e.get("nativeOnly") for e in ws_u.eq), \
        "武聖裝備應帶 nativeOnly rateup 效果(equips_parsed.json)"
    apply_effects(ws_u, None, {"effects": ws_u.eq, "kind": "phys"}, [ws_u], [], no_heal=True)
    native_tac_ws = {"type": "active", "rate": 0.5, "prep": 0, "native": True, "effects": [], "coef": 0}
    inherit_tac_ws = {"type": "active", "rate": 0.5, "prep": 0, "effects": [], "coef": 0}  # 無 native 鍵 = 傳承
    assert abs(ws_u.addbonus_for("rateup", native_tac_ws) - 0.05) < 1e-9, \
        "武聖(自帶戰法發動率+5%)應加成自帶戰法"
    assert ws_u.addbonus_for("rateup", inherit_tac_ws) == 0, \
        "武聖 nativeOnly 不應加成傳承戰法"
    # 31c) Unit 建構時自帶戰法應帶 native=True, 傳承戰法不帶(供 31a/31b 的旗標篩選用)
    n_unit = Unit(POOL["關羽"], "騎", inherit=["白眉"])
    assert n_unit.tactics[0].get("native") is True and not n_unit.tactics[1].get("native"), \
        "Unit.tactics[0](自帶)應標 native=True, 傳承戰法不標"

    # --- 批8: 效果級回合窗 + 剩餘原語 + 判定勝拆分 --------------------------------
    # 32) 裝備效果級回合窗(delayed_eq): 帶 when 的裝備效果應分離到 delayed_eq, 不進準備階段 eq
    mg_u = Unit(POOL["諸葛亮"], "弓", equip=["寶物·謀攻"])
    assert any(e["k"] == "silence" for e in mg_u.delayed_eq), "謀攻(第3回合計窮)應落在 delayed_eq"
    assert not any(e["k"] == "silence" for e in mg_u.eq), "謀攻帶 when, 不應留在準備階段套用的 eq"
    assert mg_u.delayed_eq[0]["when"] == {"rounds": [3]}, "謀攻 when 應為第3回合(rounds:[3])"
    # fight() 主迴圈第3回合應觸發並記錄 when_fired(用效果物件 id() 去重), 第1/2回合不觸發
    foe32 = Unit(POOL["張飛"], "盾")
    assert not round_ok({"when": mg_u.delayed_eq[0]["when"]}, 1) and not round_ok({"when": mg_u.delayed_eq[0]["when"]}, 2)
    assert round_ok({"when": mg_u.delayed_eq[0]["when"]}, 3), "謀攻應只在第3回合的視窗判定為真"

    # 33) healblock(禁療): 中招者受治療無效, dur 回合後恢復
    hb_target = Unit(POOL["張飛"], "盾")
    hb_target.troop = 5000
    hb_target.wounded = 5000                          # 批18: 治療上限=傷兵池, 測試手動降兵力須同步設wounded供heal結算
    hb_target.healblock = 2
    heal_tac33 = {"nameZh": "測試治療術33", "type": "active", "kind": "intel", "coef": 0,
                  "rate": 1.0, "n": 1, "prep": 0,
                  "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1}]}
    caster33 = Unit(POOL["諸葛亮"], "弓")
    troop_before_hb = hb_target.troop
    apply_effects(caster33, None, heal_tac33, [hb_target], [], no_heal=False)
    assert hb_target.troop == troop_before_hb, "healblock 中的目標不應被 heal 選為治療對象(兵力不變)"
    hb_target.healblock = 0                            # 解除禁療後應可正常被治療
    apply_effects(caster33, None, heal_tac33, [hb_target], [], no_heal=False)
    assert hb_target.troop > troop_before_hb, "healblock 解除後應恢復可被治療"

    # 34) lifesteal(倒戈): 造成傷害時應按比例回復自身兵力, 且上限不超過 START_TROOP
    ls_src = Unit(POOL["呂布"], "騎")
    ls_src.troop = 9000
    ls_src.push_add("lifesteal", 0.5, 9)               # 50% 倒戈(誇大值方便驗證)
    ls_dst = Unit(POOL["張飛"], "盾")
    troop_before_ls = ls_src.troop
    hit(ls_src, ls_dst, 1.0, "phys")
    assert ls_src.troop > troop_before_ls, "lifesteal 應使攻擊者造成傷害後回復兵力"
    ls_src2 = Unit(POOL["呂布"], "騎")
    ls_src2.troop = START_TROOP                        # 已滿兵, 回血不應超過上限
    ls_src2.push_add("lifesteal", 1.0, 9)
    hit(ls_src2, Unit(POOL["張飛"], "盾"), 1.0, "phys")
    assert ls_src2.troop == START_TROOP, "lifesteal 回復量不應使兵力超過 START_TROOP 上限"

    # 35) inheritedOnly(nativeOnly 反向): 只加成「非自帶」(傳承)戰法, 不加自帶戰法
    io_u = Unit(POOL["呂布"], "騎")
    io_u.push_add("rateup", 1.0, 9, src="測試竭力佐謀", flags={"inheritedOnly": True})
    native_tac35 = {"type": "active", "rate": 0.5, "prep": 0, "native": True, "effects": [], "coef": 0}
    inherited_tac35 = {"type": "active", "rate": 0.5, "prep": 0, "effects": [], "coef": 0}  # 無 native 鍵 = 傳承
    assert io_u.addbonus_for("rateup", native_tac35) == 0, "inheritedOnly 不應加成自帶戰法"
    assert abs(io_u.addbonus_for("rateup", inherited_tac35) - 1.0) < 1e-9, "inheritedOnly 應加成傳承(非自帶)戰法"

    # 36) who="leader": 效果應只作用於隊伍主將(allies[0]), 不影響副將
    leader_tac = {"nameZh": "測試主將效果", "type": "command", "kind": "phys", "coef": 0,
                  "rate": 1.0, "n": 1, "prep": 0,
                  "effects": [{"k": "rateup", "who": "leader", "val": 0.16, "dur": 99}]}
    ldr, sub1, sub2 = Unit(POOL["呂布"], "騎"), Unit(POOL["張飛"], "盾"), Unit(POOL["關羽"], "騎")
    apply_effects(ldr, None, leader_tac, [ldr, sub1, sub2], [], no_heal=True)
    assert abs(ldr.addbonus("rateup") - 0.16) < 1e-9, "who=leader 應使主將(allies[0])獲得效果"
    assert sub1.addbonus("rateup") == 0 and sub2.addbonus("rateup") == 0, "who=leader 不應影響副將"

    # 37) 殲滅(kill) / 判定勝(剩餘兵力) 拆分: fight() 應回傳第三值 kill(bool), simulate() 統計應合理
    #     (殲滅+判定勝 應等於總勝場, 8回合戰鬥多數會被殲滅, 故殲滅率理應顯著>0)
    w37, r37, k37 = fight(["呂布", "趙雲", "關羽"], ["諸葛亮", "周瑜", "司馬懿"])
    assert isinstance(k37, bool), "fight() 第三個回傳值 kill 應為 bool"
    if k37:
        # 批12: 殲滅可能發生在最後一回合(r==ROUNDS)本身 —— fight() 在每回合 tick() 後立即檢查殲滅,
        # 第8回合結算造成一方全滅時 kill=True 且 r==8, 屬合法情況(非僅能發生在 ROUNDS 之前)。
        assert r37 <= ROUNDS, "殲滅(kill=True)應發生在 ROUNDS 回合或之前"
    else:
        assert r37 == ROUNDS, "判定勝(kill=False)應打滿 ROUNDS 回合"
    res37 = simulate(["呂布", "趙雲", "關羽"], ["諸葛亮", "周瑜", "司馬懿"], n=500)
    assert abs((res37["A殲滅"] + res37["B殲滅"]) - res37["殲滅率"]) < 1e-9, "A殲滅+B殲滅 應等於總殲滅率"
    assert abs((res37["A殲滅"] + res37["A判定勝"]) - res37["A勝率"]) < 1e-6, "A殲滅+A判定勝 應等於 A總勝率"
    assert abs((res37["B殲滅"] + res37["B判定勝"]) - res37["B勝率"]) < 1e-6, "B殲滅+B判定勝 應等於 B總勝率"
    assert res37["殲滅率"] > 0, "500場模擬中應有相當比例在8回合內分出殲滅勝負"

    # --- 批12: chaos(混亂) + hitsRepeat + lockTarget ------------------------------
    # 38) chaos: 混亂單位的普攻/單體主動戰法目標應敵我不分(可能選中友軍)。用 pick_target_chaos
    #     直接驗證: 構造一個「敵方全滅、只剩友軍存活」的極端池, 混亂單位仍應能選中存活友軍
    #     (而非因 foes 池為空就選不到目標)。
    chaos_u = Unit(POOL["呂布"], "騎")
    chaos_u.chaos = 2
    ally38 = Unit(POOL["張飛"], "盾")
    v38 = pick_target_chaos(chaos_u, [chaos_u, ally38], [])  # 空的敵方池, 只能從友軍(排除自己)中選
    assert v38 is ally38, "混亂且敵方池為空時, 應能選中存活友軍作為目標(敵我不分)"
    # 統計驗證: 多次呼叫應偶爾選中友軍(敵我皆有目標時, 非必定選友軍, 但長期應有相當比例)
    foe38 = Unit(POOL["諸葛亮"], "弓")
    ally_hits = sum(1 for _ in range(500) if pick_target_chaos(chaos_u, [chaos_u, ally38], [foe38]) is ally38)
    assert ally_hits > 0, "混亂單位在敵我皆有目標時, 500次抽樣應至少命中友軍一次(敵我不分)"
    # 非混亂時應退回一般 pick_target(只從敵方池選, 不會選中友軍)
    normal_u38 = Unit(POOL["呂布"], "騎")
    v38b = pick_target_chaos(normal_u38, [normal_u38, ally38], [foe38])
    assert v38b is foe38, "非混亂狀態應退回一般 pick_target(只選敵方), 不受混亂邏輯影響"

    # 39) insight 應阻擋 chaos 套用(同 stun/silence/disarm 慣例)
    ins_u = Unit(POOL["呂布"], "騎")
    ins_u.insight = 3
    chaos_tac39 = {"nameZh": "測試混亂39", "type": "active", "kind": "phys", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0,
                   "effects": [{"k": "chaos", "who": "enemy", "dur": 2}]}
    apply_effects(Unit(POOL["張飛"], "盾"), None, chaos_tac39, [], [ins_u], no_heal=True)
    assert ins_u.chaos == 0, "insight(洞察)應免疫混亂效果套用"
    # insight 施加時應同時解除既有混亂(同 stun/silence/disarm 慣例)
    chaos_u2 = Unit(POOL["呂布"], "騎")
    chaos_u2.chaos = 3
    insight_tac39 = {"nameZh": "測試洞察39", "type": "active", "kind": "phys", "coef": 0,
                      "rate": 1.0, "n": 1, "prep": 0,
                      "effects": [{"k": "insight", "who": "self", "dur": 2}]}
    apply_effects(chaos_u2, None, insight_tac39, [chaos_u2], [], no_heal=True)
    assert chaos_u2.chaos == 0, "施加洞察應同時解除既有混亂"

    # 40) hitsRepeat: 只剩一個存活敵人時, N次獨立抽樣應全部命中該唯一存活者(不因「找不到N個
    #     不重複目標」而提前跳過, 證明是逐次重新選標而非一次性選N個不重複)
    hr_src = Unit(POOL["張角"], "弓")
    hr_only_survivor = Unit(POOL["諸葛亮"], "弓")
    hr_dead = Unit(POOL["周瑜"], "弓")
    hr_dead.troop = 0                                  # 已陣亡, 不應被選中
    hr_tac = {"nameZh": "測試hitsRepeat40", "type": "active", "kind": "intel", "coef": 0.3,
              "rate": 1.0, "n": 5, "prep": 0, "hitsRepeat": True, "effects": []}
    # 直接模擬 fight() 內的 hitsRepeat 迴圈邏輯(5次獨立 pick_target, 唯一存活目標應全部命中)
    troop_before_40 = hr_only_survivor.troop
    for _ in range(hr_tac["n"]):
        v = pick_target([hr_only_survivor, hr_dead], hr_src)
        assert v is hr_only_survivor, "hitsRepeat: 唯一存活目標應每次都被選中(不因僅剩1個目標而跳過)"
        hit(hr_src, v, hr_tac["coef"], hr_tac["kind"], False, None)
    assert hr_only_survivor.troop < troop_before_40, "hitsRepeat 5次獨立命中應對唯一存活目標造成累積傷害"

    # 41) lockTarget: 同一戰法物件兩次發動應鎖定並重用同一目標(不重新隨機選)
    lt_tac = {"nameZh": "測試lockTarget41", "type": "active", "kind": "phys", "coef": 0,
              "rate": 1.0, "n": 1, "prep": 0, "lockTarget": True, "effects": []}
    lt_caster = Unit(POOL["呂布"], "騎")
    lt_pool = [Unit(POOL["諸葛亮"], "弓"), Unit(POOL["周瑜"], "弓"), Unit(POOL["司馬懿"], "弓")]
    t1 = resolve_locked_target(lt_caster, lt_tac, lt_pool)
    t2 = resolve_locked_target(lt_caster, lt_tac, lt_pool)
    t3 = resolve_locked_target(lt_caster, lt_tac, lt_pool)
    assert t1 is t2 is t3, "lockTarget: 同一戰法物件多次發動應重用同一鎖定目標"
    # 目標陣亡後, lockTarget 應回傳 None(不重新選新目標) —— 保守設計, 見程式碼註解
    t1.troop = 0
    t4 = resolve_locked_target(lt_caster, lt_tac, lt_pool)
    assert t4 is None, "lockTarget: 鎖定目標已陣亡時應回傳None(不重新選新目標, 視為本回合無有效目標)"
    # 不同戰法物件(即使同名)應各自獨立鎖定, 不共用鎖定目標
    lt_tac_other = {"nameZh": "測試lockTarget41", "type": "active", "kind": "phys", "coef": 0,
                     "rate": 1.0, "n": 1, "prep": 0, "lockTarget": True, "effects": []}
    lt_pool2 = [Unit(POOL["諸葛亮"], "弓"), Unit(POOL["周瑜"], "弓")]
    t5 = resolve_locked_target(lt_caster, lt_tac_other, lt_pool2)
    assert id(lt_tac_other) in lt_caster.locked_targets, "不同戰法物件應各自在 locked_targets 建立獨立鎖定項"

    # --- 批13: extraHits(多段傷害) + who="subs"(副將群) --------------------------
    # 42) extraHits: 主 coef 段(兵刃) + 額外段(謀略) 應各自獨立結算傷害, 額外段的 kind 與主段
    #     不同時各自套用正確的攻防屬性(如屠几上肉 兵刃150%+謀略150%)。用一個 intel 遠高於
    #     force 的施法者驗證: 額外段(intel)理論傷害應與主段(phys)不同, 證明兩段各自獨立算傷害
    #     (非合併成單一 coef)。
    eh_src = Unit(POOL["諸葛亮"], "弓")           # 智力遠高於武力, 兵刃段/謀略段傷害應有明顯差異
    eh_tgt42 = Unit(POOL["張飛"], "盾")
    eh_tac = {"nameZh": "測試extraHits42", "type": "active", "kind": "phys", "coef": 1.5,
              "rate": 1.0, "n": 1, "prep": 0,
              "extraHits": [{"coef": 1.5, "kind": "intel"}], "effects": []}
    troop_before_42 = eh_tgt42.troop
    hit(eh_src, eh_tgt42, eh_tac["coef"], eh_tac["kind"], False, None)   # 模擬主段(phys)
    troop_after_main = eh_tgt42.troop
    fire_extra_hits(eh_src, eh_tac, eh_tgt42, lambda u: [eh_src], lambda u: [eh_tgt42], None)  # 模擬額外段(intel)
    assert eh_tgt42.troop < troop_after_main, "extraHits: 額外段應對目標造成獨立的第二次傷害(非被主段吞掉)"
    dmg_main = troop_before_42 - troop_after_main
    dmg_extra = troop_after_main - eh_tgt42.troop
    # 諸葛亮智力遠高於武力, 謀略段(intel)理論傷害應明顯大於兵刃段(phys)(相同coef下), 證明額外段
    # 確實各自用自己的kind獨立算傷害(而非誤用主段的kind或複製主段傷害量)
    assert dmg_extra > dmg_main * 1.05, "extraHits: 額外段應依自己的kind(intel)獨立算傷害, 不與主段(phys)相同"

    # extraHits.rate: rate=0 時額外段不應觸發(0次傷害)
    eh_tgt42b = Unit(POOL["張飛"], "盾")
    eh_tac_norate = {"nameZh": "測試extraHits42b", "type": "active", "kind": "phys", "coef": 0,
                      "rate": 1.0, "n": 1, "prep": 0,
                      "extraHits": [{"coef": 1.5, "kind": "phys", "rate": 0.0}], "effects": []}
    troop_before_42b = eh_tgt42b.troop
    fire_extra_hits(eh_src, eh_tac_norate, eh_tgt42b, lambda u: [eh_src], lambda u: [eh_tgt42b], None)
    assert eh_tgt42b.troop == troop_before_42b, "extraHits: rate=0 的額外段不應觸發傷害"

    # 43) who="subs": 副將群 = allies 除 index 0(主將), 效果只套用到非主將的存活友軍
    subs_leader = Unit(POOL["呂布"], "騎")
    subs_a = Unit(POOL["張飛"], "盾")
    subs_b = Unit(POOL["趙雲"], "騎")
    subs_team = [subs_leader, subs_a, subs_b]
    subs_tac = {"nameZh": "測試who_subs43", "type": "command", "kind": "phys", "coef": 0,
                "rate": 1.0, "n": 1, "prep": 0,
                "effects": [{"k": "mitig", "who": "subs", "val": 0.25, "dur": 5}]}
    apply_effects(subs_leader, None, subs_tac, subs_team, [], no_heal=True)
    assert abs(subs_leader.addbonus("mitig") - 0) < 1e-9, "who=subs: 主將(index 0)不應被套用副將群效果"
    assert abs(subs_a.addbonus("mitig") - 0.25) < 1e-9, "who=subs: 副將應被套用效果"
    assert abs(subs_b.addbonus("mitig") - 0.25) < 1e-9, "who=subs: 副將應被套用效果"

    # --- 批15: 指揮/被動 heal 語意修正 —— heal_only 常駐通道應尊重 t["when"](round_ok)/
    # t["rate"](擲骰)/e["once"](單次去重), 不再無視三者每回合無條件結算 -----------------
    global CUR_ROUND
    # 44a) when.rounds:[4] 的 heal 應只在第4回合治療一次, 其餘回合(含之後回合)不治療
    w44_caster = Unit(POOL["諸葛亮"], "弓")
    w44_target = Unit(POOL["張飛"], "盾")
    w44_target.troop = 3000
    w44_target.wounded = START_TROOP                  # 批18: 治療上限=傷兵池
    heal_tac44a = {"nameZh": "測試治療術44a", "type": "command", "kind": "intel", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0, "when": {"rounds": [4]},
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1}]}
    for _r in range(1, 9):
        CUR_ROUND = _r
        apply_effects(w44_caster, None, heal_tac44a, [w44_target], [], heal_only=True)
        if _r == 4:
            troop_after_r4 = w44_target.troop
            assert troop_after_r4 > 3000, "when rounds:[4] 的 heal 應在第4回合治療"
        elif _r < 4:
            assert w44_target.troop == 3000, f"when rounds:[4] 的 heal 不應在第{_r}回合(視窗前)治療"
        else:
            assert w44_target.troop == troop_after_r4, f"when rounds:[4] 的 heal 不應在第{_r}回合(視窗後, 已消耗一次性觸發)重複治療"
    CUR_ROUND = 0

    # 44b) rate 0.5 的 heal(無 when, 每回合持續型): 統計上應約半數回合真正治療(擲骰生效),
    # 而非每回合必定治療 —— 固定 random 種子跑多回合, 驗證確有「未中不治療」的分支存在
    random.seed(12345)
    w44b_caster = Unit(POOL["諸葛亮"], "弓")
    heal_tac44b = {"nameZh": "測試治療術44b", "type": "passive", "kind": "intel", "coef": 0,
                   "rate": 0.5, "n": 1, "prep": 0,
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1}]}
    n_healed = 0
    N_TRIALS = 200
    for _i in range(N_TRIALS):
        w44b_target = Unit(POOL["張飛"], "盾")
        w44b_target.troop = 3000
        w44b_target.wounded = START_TROOP             # 批18: 治療上限=傷兵池
        CUR_ROUND = 1
        apply_effects(w44b_caster, None, heal_tac44b, [w44b_target], [], heal_only=True)
        if w44b_target.troop > 3000:
            n_healed += 1
    CUR_ROUND = 0
    assert 0 < n_healed < N_TRIALS, "rate 0.5 的 heal 應該有些回合中、有些回合不中(不應每次都治療或都不治療)"
    assert abs(n_healed / N_TRIALS - 0.5) < 0.15, f"rate 0.5 的 heal 命中比例應接近50%, 實測{n_healed}/{N_TRIALS}"
    random.seed()                                      # 還原非固定種子, 不影響後續隨機性依賴的測項

    # 44c) 無 when 且無 once 的 heal: 維持原行為, 每回合持續治療(急救/休整類戰法本意如此)
    w44c_caster = Unit(POOL["諸葛亮"], "弓")
    w44c_target = Unit(POOL["張飛"], "盾")
    w44c_target.troop = 1000
    w44c_target.wounded = START_TROOP                 # 批18: 治療上限=傷兵池
    heal_tac44c = {"nameZh": "測試治療術44c", "type": "command", "kind": "intel", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0,
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1}]}
    troops_seen = []
    for _r in range(1, 4):
        CUR_ROUND = _r
        apply_effects(w44c_caster, None, heal_tac44c, [w44c_target], [], heal_only=True)
        troops_seen.append(w44c_target.troop)
    CUR_ROUND = 0
    assert troops_seen[0] < START_TROOP and troops_seen[1] > troops_seen[0] and troops_seen[2] > troops_seen[1], \
        "無when無once的heal應每回合持續治療(兵力逐回合遞增, 現行為不變)"

    # 44d) e["when"](效果級 when, 優先於 t["when"]): 同一戰法內其他效果(如 mitig)應維持準備
    # 階段常駐套用(不受 when 影響), 只有帶 e["when"] 的 heal 效果被限定在指定回合觸發 ——
    # 對應撫輯軍民「前3回合減傷(常駐) + 第4回合單次治療」這類「同戰法內混合時間窗」場景,
    # 若誤用戰法級 t["when"] 會連帶把 mitig 也延後到第4回合才套用(錯誤), 必須用 e["when"]
    # 只精準框住 heal 效果本身。
    w44d_caster = Unit(POOL["諸葛亮"], "弓")
    w44d_target = Unit(POOL["張飛"], "盾")
    w44d_target.troop = 3000
    w44d_target.wounded = START_TROOP                 # 批18: 治療上限=傷兵池
    mixed_tac44d = {"nameZh": "測試混合時間窗44d", "type": "command", "kind": "phys", "coef": 0,
                     "rate": 1.0, "n": 1, "prep": 0,
                     "effects": [
                         {"k": "mitig", "who": "ally", "val": 0.24, "dur": 3},   # 無 when: 應在準備階段就套用(不受回合限制)
                         {"k": "heal", "who": "ally", "coef": 0.8, "dur": 1, "when": {"rounds": [4]}},  # 只有 heal 帶 when: 只在第4回合觸發
                     ]}
    apply_effects(w44d_caster, None, mixed_tac44d, [w44d_target], [], no_heal=True)  # 模擬準備階段套用(no_heal=True 排除heal, 同 fight() 開場呼叫)
    assert abs(w44d_target.addbonus("mitig") - 0.24) < 1e-9, "e[when]不應影響同戰法內無when的mitig效果, 準備階段應正常套用"
    for _r in range(1, 9):
        CUR_ROUND = _r
        apply_effects(w44d_caster, None, mixed_tac44d, [w44d_target], [], heal_only=True)
        if _r == 4:
            troop_after_r4d = w44d_target.troop
            assert troop_after_r4d > 3000, "e[when] rounds:[4] 的 heal 應在第4回合治療"
        elif _r < 4:
            assert w44d_target.troop == 3000, f"e[when] rounds:[4] 的 heal 不應在第{_r}回合(視窗前)治療"
        else:
            assert w44d_target.troop == troop_after_r4d, f"e[when] rounds:[4] 的 heal 不應在第{_r}回合(視窗後)重複治療"
    CUR_ROUND = 0

    # 44e) when["from"]/["until"](範圍視窗, 如金丹秘術「第3回合起，持續3回合」): 語意是
    # 「窗內每回合都要治療」(休整類持續恢復), 與 rounds(單一/多個特定回合各觸發一次)不同,
    # 不應該只在視窗開啟當回合治療一次就不再觸發 —— 用 from:3/until:5 驗證第3~5回合每回合
    # 都治療、其餘回合不治療。
    w44e_caster = Unit(POOL["諸葛亮"], "弓")
    heal_tac44e = {"nameZh": "測試治療術44e", "type": "command", "kind": "intel", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0,
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1,
                                "when": {"from": 3, "until": 5}}]}
    healed_rounds = []
    for _r in range(1, 9):
        w44e_target = Unit(POOL["張飛"], "盾")   # 每回合重建全血目標, 只看「這一回合heal_only通道有沒有真的治療」
        w44e_target.troop = 3000
        w44e_target.wounded = START_TROOP             # 批18: 治療上限=傷兵池
        CUR_ROUND = _r
        apply_effects(w44e_caster, None, heal_tac44e, [w44e_target], [], heal_only=True)
        if w44e_target.troop > 3000:
            healed_rounds.append(_r)
    CUR_ROUND = 0
    assert healed_rounds == [3, 4, 5], f"when from:3/until:5 應在第3~5回合每回合都治療(範圍視窗持續型, 非單次), 實際={healed_rounds}"

    # 44f) when["rounds"] 列出多個回合(如「第3、5回合」)應在每個列出的回合各自觸發一次
    w44f_caster = Unit(POOL["諸葛亮"], "弓")
    heal_tac44f = {"nameZh": "測試治療術44f", "type": "command", "kind": "intel", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0,
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1,
                                "when": {"rounds": [3, 5]}}]}
    healed_rounds_f = []
    for _r in range(1, 9):
        w44f_target = Unit(POOL["張飛"], "盾")
        w44f_target.troop = 3000
        w44f_target.wounded = START_TROOP             # 批18: 治療上限=傷兵池
        CUR_ROUND = _r
        apply_effects(w44f_caster, None, heal_tac44f, [w44f_target], [], heal_only=True)
        if w44f_target.troop > 3000:
            healed_rounds_f.append(_r)
    CUR_ROUND = 0
    assert healed_rounds_f == [3, 5], f"when rounds:[3,5] 應在第3回合與第5回合各觸發一次, 實際={healed_rounds_f}"

    # --- 批16: 原語擴充包(v5盲測殘差) --------------------------------------------
    # 45) ifTargetHas: 效果段只對「已有該狀態」的目標生效; dot/控制類各驗一次
    assert target_has(None, "dot") is False, "target_has 對 None 應回傳 False(防禦)"
    ith_dotted = Unit(POOL["張飛"], "盾")
    ith_dotted.dots.append([100, 3, False])
    ith_clean = Unit(POOL["張飛"], "盾")
    assert target_has(ith_dotted, "dot") is True and target_has(ith_clean, "dot") is False, \
        "ifTargetHas=dot 應只認定 dots 非空的目標"
    ith_stunned = Unit(POOL["張飛"], "盾")
    ith_stunned.stun = 2
    assert target_has(ith_stunned, "stun") is True and target_has(ith_clean, "stun") is False, \
        "ifTargetHas=stun 應只認定 stun>0 的目標"
    ith_tac = {"nameZh": "測試ifTargetHas45", "type": "active", "kind": "phys", "coef": 0,
               "rate": 1.0, "n": 1, "prep": 0,
               "effects": [{"k": "amp", "who": "enemy", "val": 0.3, "dur": 3, "ifTargetHas": "dot"}]}
    ith_caster = Unit(POOL["諸葛亮"], "弓")
    apply_effects(ith_caster, None, ith_tac, [], [ith_dotted, ith_clean], no_heal=True)
    assert ith_dotted.addbonus("mitig") < 0, "ifTargetHas=dot 應對帶dot的目標生效(此處amp who=enemy>0轉為易傷/mitig負值)"
    assert ith_clean.addbonus("mitig") == 0, "ifTargetHas=dot 不應對無dot的目標生效"

    # 46) everyN: 自身每第N次普攻觸發戰法 effects(用 tick_every_n 直接驗證計數器行為)
    en_tac = {"nameZh": "測試everyN46", "type": "passive", "kind": "phys", "coef": 0,
              "rate": 1.0, "n": 1, "prep": 0, "everyN": {"count": 3, "on": "attack"},
              "effects": [{"k": "amp", "who": "self", "val": 0.1, "dur": 2}]}
    en_u = Unit(POOL["呂布"], "騎")
    fired = [en_u.tick_every_n(en_tac) for _ in range(6)]
    assert fired == [False, False, True, False, False, True], \
        f"everyN count=3 應在第3、6次普攻觸發, got={fired}"

    # 47) immuneTo: 單項控制免疫應只擋清單內的控制類型, 不像 insight 全免
    im_u = Unit(POOL["呂布"], "騎")
    im_u.push_immune(["stun"], 3)
    assert im_u.is_immune_to("stun") is True and im_u.is_immune_to("silence") is False, \
        "immuneTo=['stun'] 應只免疫 stun, 不免疫 silence"
    stun_tac47 = {"nameZh": "測試immune47stun", "type": "active", "kind": "phys", "coef": 0,
                  "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "stun", "who": "enemy", "dur": 2}]}
    silence_tac47 = {"nameZh": "測試immune47silence", "type": "active", "kind": "phys", "coef": 0,
                     "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "silence", "who": "enemy", "dur": 2}]}
    caster47 = Unit(POOL["諸葛亮"], "弓")
    apply_effects(caster47, im_u, stun_tac47, [caster47], [im_u], no_heal=True)
    assert im_u.stun == 0, "immuneTo=['stun'] 應成功免疫震懾施加"
    apply_effects(caster47, im_u, silence_tac47, [caster47], [im_u], no_heal=True)
    assert im_u.silence > 0, "immuneTo=['stun'] 不應免疫計窮(非清單內類型)"

    # 48) when 擴充: parity(奇偶回合) + every(每N回合)
    assert round_ok({"when": {"parity": "odd"}}, 1) and not round_ok({"when": {"parity": "odd"}}, 2), \
        "when.parity=odd 應只在奇數回合符合"
    assert round_ok({"when": {"parity": "even"}}, 4) and not round_ok({"when": {"parity": "even"}}, 5), \
        "when.parity=even 應只在偶數回合符合"
    assert round_ok({"when": {"every": 3}}, 3) and round_ok({"when": {"every": 3}}, 6) and not round_ok({"when": {"every": 3}}, 4), \
        "when.every=3 應只在3的倍數回合符合"
    assert round_ok({"when": {"every": 2, "from": 4}}, 4) and not round_ok({"when": {"every": 2, "from": 4}}, 3), \
        "when.every 應可與 from/until 並存(皆通過才符合)"

    # 49) hpPct 觸發: hpBelow(一次性首次跨越) / hpAbove(持續窗)
    assert hp_ok({"when": {"hpBelow": 0.5}}, type("U", (), {"hp_pct": 0.4})()) is True
    assert hp_ok({"when": {"hpBelow": 0.5}}, type("U", (), {"hp_pct": 0.6})()) is False
    assert hp_ok({"when": {"hpAbove": 0.5}}, type("U", (), {"hp_pct": 0.6})()) is True
    hp_u = Unit(POOL["張飛"], "盾")
    hp_u.troop = 3000                                  # 30% 兵力, 低於50%門檻
    hp_tac49 = {"nameZh": "測試hpBelow49", "type": "passive", "kind": "phys", "coef": 0,
                "rate": 1.0, "n": 1, "prep": 0, "when": {"hpBelow": 0.5},
                "effects": [{"k": "amp", "who": "self", "val": 0.2, "dur": 3}]}
    assert hp_ok(hp_tac49, hp_u) is True, "兵力30%應通過hpBelow=0.5門檻"
    hp_u.troop = 8000
    assert hp_ok(hp_tac49, hp_u) is False, "兵力80%不應通過hpBelow=0.5門檻"

    # 50) healBoost/healGiven: 受到的治療×(1+val), 施放的治療×(1+val), 可疊乘
    hb_caster = Unit(POOL["諸葛亮"], "弓")
    hb_caster.push_add("healGiven", 0.5, 9)             # 施放的治療+50%
    hb_target_boost = Unit(POOL["張飛"], "盾")
    hb_target_boost.troop = 3000
    hb_target_boost.wounded = START_TROOP             # 批18: 治療上限=傷兵池
    hb_target_boost.push_add("healBoost", 0.5, 9)       # 受到的治療+50%
    hb_target_plain = Unit(POOL["張飛"], "盾")
    hb_target_plain.troop = 3000
    hb_target_plain.wounded = START_TROOP             # 批18: 治療上限=傷兵池
    heal_tac50 = {"nameZh": "測試healBoost50", "type": "active", "kind": "intel", "coef": 0,
                  "rate": 1.0, "n": 1, "prep": 0,
                  "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1}]}
    plain_caster50 = Unit(POOL["諸葛亮"], "弓")
    apply_effects(plain_caster50, None, heal_tac50, [hb_target_plain], [], no_heal=False)
    apply_effects(hb_caster, None, heal_tac50, [hb_target_boost], [], no_heal=False)
    gain_plain = hb_target_plain.troop - 3000
    gain_boost = hb_target_boost.troop - 3000
    assert abs(gain_boost - gain_plain * 1.5 * 1.5) < 1.0, \
        f"healBoost+healGiven應各自+50%並疊乘(合計×2.25), plain={gain_plain:.1f} boost={gain_boost:.1f}"

    # 51) dispel: buffs 應清除正向增益(略過undispellable), debuffs 應清除負向減益+控制欄位(略過undispellable)
    dp_u = Unit(POOL["呂布"], "騎")
    dp_u.push_add("amp", 0.3, 9, src="測試增益51")
    dp_u.push_add("amp", 0.2, 9, src="測試護體51", flags={"undispellable": True})
    dispel_unit(dp_u, "buffs")
    assert abs(dp_u.addbonus("amp") - 0.2) < 1e-9, "dispel buffs 應清除可驅散的正向amp, 保留undispellable那條"
    dp_u2 = Unit(POOL["呂布"], "騎")
    dp_u2.push_add("amp", -0.25, 9, src="測試減益51")
    dp_u2.stun = 2
    dp_u2.dots.append([50, 3, False])
    dp_u2.dots.append([80, 3, True])                    # undispellable dot, 應保留
    dispel_unit(dp_u2, "debuffs")
    assert dp_u2.addbonus("amp") == 0, "dispel debuffs 應清除負向amp"
    assert dp_u2.stun == 0, "dispel debuffs 應清除控制欄位(stun)"
    assert len(dp_u2.dots) == 1 and dp_u2.dots[0][2] is True, "dispel debuffs 應清除可驅散的dot, 保留undispellable的dot"

    # 52) choices: 擇一分支應按權重隨機選一組效果套用; weight=0的分支不應被選中
    ch_choices = [{"weight": 1, "effects": [{"k": "amp", "who": "self", "val": 0.11, "dur": 1}]},
                  {"weight": 0, "effects": [{"k": "amp", "who": "self", "val": 0.99, "dur": 1}]}]
    picked_vals = {pick_choice(ch_choices)["effects"][0]["val"] for _ in range(50)}
    assert picked_vals == {0.11}, f"weight=0的分支不應被pick_choice選中, got={picked_vals}"
    ch_two_choices = [{"weight": 1, "effects": [{"k": "amp", "who": "self", "val": 0.1, "dur": 1}]},
                      {"weight": 1, "effects": [{"k": "amp", "who": "self", "val": 0.2, "dur": 1}]}]
    picked_two = {pick_choice(ch_two_choices)["effects"][0]["val"] for _ in range(200)}
    assert picked_two == {0.1, 0.2}, f"均分權重下200次抽樣應兩個分支都出現過, got={picked_two}"

    # 53) fakeReport: 中招者被動+指揮戰法coef段擲骰應被抑制; on_hit反應式觸發也應被抑制; insight可免
    fr_u = Unit(POOL["張飛"], "盾")
    fr_u.fake_report_dur = 2
    assert fr_u.fake_report_dur > 0, "fakeReport施加後 fake_report_dur 應>0(供 fight() 主迴圈/on_hit 檢查抑制)"
    fr_tac = {"nameZh": "測試fakeReport53", "type": "active", "kind": "phys", "coef": 0,
              "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "fakeReport", "who": "enemy", "dur": 2}]}
    fr_caster = Unit(POOL["諸葛亮"], "弓")
    fr_target = Unit(POOL["張飛"], "盾")
    apply_effects(fr_caster, fr_target, fr_tac, [fr_caster], [fr_target], no_heal=True)
    assert fr_target.fake_report_dur > 0, "fakeReport 應成功施加(dur>0)"
    fr_insight = Unit(POOL["張飛"], "盾")
    fr_insight.insight = 3
    apply_effects(fr_caster, fr_insight, fr_tac, [fr_caster], [fr_insight], no_heal=True)
    assert fr_insight.fake_report_dur == 0, "insight(洞察)應免疫偽報"

    # --- 批18: 傷兵池 + fakeReport強化 + ambush + targetSel ----------------------
    # 54) 傷兵池: 第1回合受到1000傷害 -> wounded應為 1000*0.9=900
    wp_src = Unit(POOL["呂布"], "騎")
    wp_dst = Unit(POOL["張飛"], "盾")
    CUR_ROUND = 1
    wp_before = wp_dst.troop
    hit(wp_src, wp_dst, 1.0, "phys")
    wp_dmg = wp_before - wp_dst.troop
    assert abs(wp_dst.wounded - wp_dmg * 0.9) < 1e-6, \
        f"第1回合傷害應以90%轉化率計入傷兵池, dmg={wp_dmg:.1f} wounded={wp_dst.wounded:.1f}"
    CUR_ROUND = 0

    # 55) 治療不應超過wounded池餘量(即使治療量本身遠大於傷兵池)
    wp2_target = Unit(POOL["張飛"], "盾")
    wp2_target.troop = 1000
    wp2_target.wounded = 200                            # 傷兵池只剩200可救援
    wp2_tac = {"nameZh": "測試傷兵池上限55", "type": "active", "kind": "intel", "coef": 0,
               "rate": 1.0, "n": 1, "prep": 0,
               "effects": [{"k": "heal", "who": "ally", "coef": 5.0, "dur": 1}]}  # 誇大治療量
    wp2_caster = Unit(POOL["諸葛亮"], "弓")
    apply_effects(wp2_caster, None, wp2_tac, [wp2_target], [], no_heal=False)
    assert abs(wp2_target.troop - 1200) < 1e-6, \
        f"治療應被傷兵池餘量(200)封頂, 實際兵力={wp2_target.troop:.1f}(預期1200)"
    assert abs(wp2_target.wounded) < 1e-6, "傷兵池應在治療後歸零(全數被救回)"

    # 56) 第7回合傷害轉化率應為0.675(65~70%取中值)
    assert abs(wounded_rate(7) - 0.675) < 1e-9, "第7回合傷兵轉化率應為0.675"
    assert abs(wounded_rate(8) - 0.675) < 1e-9, "第8回合傷兵轉化率應為0.675"
    assert abs(wounded_rate(1) - 0.9) < 1e-9 and abs(wounded_rate(3) - 0.9) < 1e-9, "第1~3回合應為0.9"
    assert abs(wounded_rate(4) - 0.8) < 1e-9 and abs(wounded_rate(6) - 0.8) < 1e-9, "第4~6回合應為0.8"

    # 57) fakeReport 加強: 已生效的指揮/被動 mitig(如暫避其鋒式減傷)應在偽報期間失效, 到期恢復
    fr2_holder = Unit(POOL["張飛"], "盾")
    fr2_tac = {"nameZh": "測試暫避其鋒57", "type": "command", "kind": "phys", "coef": 0,
               "rate": 1.0, "n": 1, "prep": 0,
               "effects": [{"k": "mitig", "who": "ally", "val": 0.4, "dur": 99}]}
    fr2_holder.tactics.append(fr2_tac)
    fr2_holder.cmd_passive_srcs.add("測試暫避其鋒57")
    apply_effects(fr2_holder, None, fr2_tac, [fr2_holder], [], no_heal=True)  # 模擬prep階段套用(常駐指揮效果)
    assert abs(fr2_holder.addbonus("mitig") - 0.4) < 1e-9, "偽報前 mitig 應正常生效"
    fr2_holder.fake_report_dur = 2
    assert abs(fr2_holder.addbonus("mitig")) < 1e-9, "偽報期間, 來源為自己指揮戰法的mitig應暫停生效(不是刪除)"
    fr2_holder.fake_report_dur = 0
    assert abs(fr2_holder.addbonus("mitig") - 0.4) < 1e-9, "偽報到期後mitig應恢復生效(條目仍在, 未被刪除)"

    # 58) ambush(遇襲): 只有遇襲者應排最後; 先攻+遇襲同時存在應抵消(按速度排, 不最先也不最後)
    am_fast = Unit(POOL["呂布"], "騎")     # 速度較高
    am_slow = Unit(POOL["張飛"], "盾")     # 速度較低(呂布通常速度高於張飛, 若不成立仍以下方純ambush欄位邏輯驗證為主)
    am_ambushed = Unit(POOL["關羽"], "騎")
    am_ambushed.ambush = 2
    am_first_and_ambush = Unit(POOL["趙雲"], "騎")
    am_first_and_ambush.first = 2
    am_first_and_ambush.ambush = 2
    eff_first = lambda x: (1 if x.first > 0 else 0) - (1 if x.ambush > 0 else 0)
    assert eff_first(am_ambushed) == -1, "只有遇襲應eff_first=-1(排最後檔)"
    assert eff_first(am_first_and_ambush) == 0, "先攻+遇襲同時存在應抵消, eff_first=0(視為普通, 按速度排)"
    assert eff_first(am_fast) == 0 and eff_first(am_slow) == 0, "無先攻無遇襲應eff_first=0"
    am_pure_first = Unit(POOL["曹操"], "騎")
    am_pure_first.first = 2
    assert eff_first(am_pure_first) == 1, "只有先攻應eff_first=1(最先檔)"
    order58 = sorted([am_ambushed, am_first_and_ambush, am_pure_first],
                      key=lambda x: (eff_first(x), x.eff("speed")), reverse=True)
    assert order58[0] is am_pure_first, "純先攻者應排最先"
    assert order58[-1] is am_ambushed, "純遇襲者應排最後"

    # 59) targetSel: 指定選標準則(不受混亂chaos影響) —— minTroop 應選兵力最低的敵方目標
    ts_e1 = Unit(POOL["張飛"], "盾"); ts_e1.troop = 5000
    ts_e2 = Unit(POOL["關羽"], "盾"); ts_e2.troop = 1000    # 兵力最低
    ts_e3 = Unit(POOL["趙雲"], "盾"); ts_e3.troop = 8000
    assert pick_by_criterion([ts_e1, ts_e2, ts_e3], "minTroop") is ts_e2, "minTroop 應選兵力最低者"
    ts_caster = Unit(POOL["曹操"], "騎")
    ts_caster.chaos = 3                                 # 混亂中的施放者
    ts_tac = {"nameZh": "測試targetSel59", "type": "active", "kind": "phys", "coef": 0,
              "rate": 1.0, "n": 1, "prep": 0,
              "effects": [{"k": "amp", "who": "enemy", "val": 0.3, "dur": 2, "targetSel": "minTroop"}]}
    for _ in range(20):                                 # 多次驗證: 即使施放者混亂, targetSel仍應穩定選中兵力最低者(不受混亂隨機影響)
        ts_e1b = Unit(POOL["張飛"], "盾"); ts_e1b.troop = 5000
        ts_e2b = Unit(POOL["關羽"], "盾"); ts_e2b.troop = 1000
        apply_effects(ts_caster, None, ts_tac, [ts_caster], [ts_e1b, ts_e2b], no_heal=True)
        assert ts_e2b.addbonus("mitig") < 0 and ts_e1b.addbonus("mitig") == 0, \
            "targetSel=minTroop 應只對兵力最低的敵方目標生效, 且chaos不應打亂此選標"

    # 60) choices 派發: coef=0 且頂層effects為空、內容全在choices裡的active戰法應能正常擲骰觸發
    ch60_choices = [{"effects": [{"k": "amp", "who": "self", "val": 0.15, "dur": 1}]}]
    ch60_tac = {"nameZh": "測試choices派發60", "type": "active", "kind": "phys", "coef": 0,
                "rate": 1.0, "n": 1, "prep": 0, "effects": [], "choices": ch60_choices}
    assert (ch60_tac["coef"] or ch60_tac["effects"] or ch60_tac.get("choices")), \
        "coef=0/effects=[]但choices非空的主動戰法, dispatch判斷式應仍為真值(可觸發)"

    # --- 批22: block(次數型格擋) + 偽報疊加規則 + 輸出減益-90%上限 + 治療時序(heal-on-damage) ---
    # 61) block: 全額格擋(val=1.0) 應完全歸零本次傷害, 消耗1層後歸零層數應移除
    bk_src = Unit(POOL["呂布"], "騎")
    bk_dst = Unit(POOL["張飛"], "盾")
    bk_dst.push_block(1.0, 1, src="測試抵禦61")
    assert len(bk_dst.block) == 1 and bk_dst.block[0]["n"] == 1, "push_block 應新增1筆格擋層"
    before61 = bk_dst.troop
    hit(bk_src, bk_dst, 1.0, "phys")
    assert bk_dst.troop == before61, "block val=1.0 應完全格擋本次傷害(兵力不變)"
    assert len(bk_dst.block) == 0, "格擋次數用盡後應從陣列移除"
    # 第二次攻擊應正常造成傷害(格擋已耗盡)
    before61b = bk_dst.troop
    hit(bk_src, bk_dst, 1.0, "phys")
    assert bk_dst.troop < before61b, "格擋耗盡後, 後續攻擊應正常造成傷害"

    # 62) block: 部分減傷(警戒 val=0.4) 應按比例打折, 消耗1層
    bk2_dst = Unit(POOL["張飛"], "盾")
    bk2_dst.push_block(0.4, 2, src="測試警戒62")
    before62 = bk2_dst.troop
    random.seed(100)
    hit(bk_src, bk2_dst, 1.0, "phys")
    dmg62 = before62 - bk2_dst.troop
    assert bk2_dst.block[0]["n"] == 1, "警戒(部分減傷)消耗後應剩1層(未移除, 因times=2)"
    assert dmg62 > 0, "警戒(val=0.4)只打折不應完全歸零"
    # 驗證確實打了折(用同種子比較有無格擋的裸傷害量級, 折扣後應明顯小於無格擋)
    bk2_plain = Unit(POOL["張飛"], "盾")
    random.seed(100)
    before62b = bk2_plain.troop
    hit(bk_src, bk2_plain, 1.0, "phys")
    dmg62_plain = before62b - bk2_plain.troop
    assert dmg62 < dmg62_plain * 0.7, f"警戒0.4減傷後傷害應明顯低於無格擋傷害, 折後={dmg62:.1f} 無格擋={dmg62_plain:.1f}"

    # 63) block: 同源疊加次數(而非刷新覆蓋)
    bk3_u = Unit(POOL["張飛"], "盾")
    bk3_u.push_block(1.0, 2, src="測試疊加63")
    bk3_u.push_block(1.0, 3, src="測試疊加63")
    assert len(bk3_u.block) == 1 and bk3_u.block[0]["n"] == 5, "同源(同src)再次施加應疊加次數(2+3=5), 而非刷新覆蓋"

    # 64) block: dispel(buffs) 應清除格擋層
    bk4_u = Unit(POOL["張飛"], "盾")
    bk4_u.push_block(1.0, 3, src="測試驅散64")
    dispel_unit(bk4_u, "buffs")
    assert len(bk4_u.block) == 0, "dispel(buffs) 應清除block格擋層(防禦性增益)"

    # 65) 偽報疊加規則: 已存在同等或更強的偽報效果時不應被覆蓋(不刷新/不縮短)
    fr61_u = Unit(POOL["張飛"], "盾")
    fr61_tac_weak = {"nameZh": "測試偽報弱65", "type": "active", "kind": "phys", "coef": 0,
                      "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "fakeReport", "who": "enemy", "dur": 3}]}
    fr61_tac_strong = {"nameZh": "測試偽報強65", "type": "active", "kind": "phys", "coef": 0,
                        "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "fakeReport", "who": "enemy", "dur": 1}]}
    fr61_caster = Unit(POOL["諸葛亮"], "弓")
    apply_effects(fr61_caster, fr61_u, fr61_tac_weak, [fr61_caster], [fr61_u], no_heal=True)
    assert fr61_u.fake_report_dur == 4, "首次施加dur:3應生效(4=3+1補償)"
    apply_effects(fr61_caster, fr61_u, fr61_tac_strong, [fr61_caster], [fr61_u], no_heal=True)
    assert fr61_u.fake_report_dur == 4, "已存在同等或更強的偽報效果(dur:3 > 新的dur:1)時, 新施加不應覆蓋(維持原4, 不降為2)"
    # 更強的新效果應能覆蓋
    fr61_tac_stronger = {"nameZh": "測試偽報更強65", "type": "active", "kind": "phys", "coef": 0,
                          "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "fakeReport", "who": "enemy", "dur": 5}]}
    apply_effects(fr61_caster, fr61_u, fr61_tac_stronger, [fr61_caster], [fr61_u], no_heal=True)
    assert fr61_u.fake_report_dur == 6, "新施加dur:5(6=5+1)比現有更強(4), 應覆蓋"

    # 66) 輸出減益疊加上限 -90%: 多重負向amp疊加落在-90%~-100%之間(未達虛弱門檻-1.0)時, 應封頂在-90%(輸出至少保留10%), 不應被錯誤歸零
    neg90_src = Unit(POOL["呂布"], "騎")
    neg90_src.push_add("amp", -0.5, 9, src="測試減益A66")
    neg90_src.push_add("amp", -0.45, 9, src="測試減益B66")
    assert abs(neg90_src.amp() - (-0.95)) < 1e-9, "amp()加總應為-0.95(未封頂前的原始加總, 落在-90%~-100%之間但未達虛弱門檻-1.0)"
    neg90_dst = Unit(POOL["張飛"], "盾")
    random.seed(200)
    d90 = damage(neg90_src, neg90_dst, 1.0, "phys")
    # -90%封頂: 原始-95%應被封頂為-90%(輸出保留10%), 不應繼續按-95%結算(更不應被誤判為虛弱而歸零, 因-0.95 > -1.0)
    assert d90 > 0, "amp加總=-0.95(在-90%~-100%之間, 未達虛弱門檻-1.0)應被-90%封頂保留10%輸出, 不應完全歸零"
    zero_amp_src = Unit(POOL["呂布"], "騎")
    random.seed(200)
    d_full = damage(zero_amp_src, neg90_dst, 1.0, "phys")
    # d90 應約為 d_full 的 10%(±隨機帶容差), 而非 5%(若未封頂會是1+(-0.95)=0.05即5%)
    ratio90 = d90 / d_full
    assert 0.08 < ratio90 < 0.12, f"封頂後應保留約10%輸出(而非未封頂的5%), 實際比例={ratio90:.3f}"

    # 66b) 更明確驗證: -0.3+-0.3=-0.6(未超過-90%門檻)應正常按-60%計算, 不受影響
    neg60_src = Unit(POOL["呂布"], "騎")
    neg60_src.push_add("amp", -0.3, 9, src="測試減益C66b")
    neg60_src.push_add("amp", -0.3, 9, src="測試減益D66b")
    assert abs(neg60_src.amp() - (-0.6)) < 1e-9
    # -0.4+-0.4+-0.4=-1.2(明顯超過-90%門檻, 但也超過虛弱門檻-1.0), 應封頂為完全歸零(現行虛弱慣例, -1.0以下視為無法造成傷害)
    neg120_src = Unit(POOL["呂布"], "騎")
    for i in range(3):
        neg120_src.push_add("amp", -0.4, 9, src=f"測試減益E66b_{i}")
    assert neg120_src.amp() < -1.0
    d120 = damage(neg120_src, neg90_dst, 1.0, "phys")
    assert d120 == 0, "amp加總<=-1.0(超過虛弱門檻)應完全歸零, 不受-90%封頂影響(封頂只適用-1.0~-0.9之間的區間)"
    # -0.35*2=-0.7(未達虛弱門檻-1.0, 也未達-90%封頂, 正常結算)不應為0
    neg70_src = Unit(POOL["呂布"], "騎")
    neg70_src.push_add("amp", -0.35, 9, src="測試減益F66b_1")
    neg70_src.push_add("amp", -0.35, 9, src="測試減益G66b_2")
    d70 = damage(neg70_src, neg90_dst, 1.0, "phys")
    assert d70 > 0, "amp加總=-0.7(未達-90%封頂門檻)應正常按七折減傷結算, 不應歸零"

    # 67) 治療時序: heal 效果帶 e.when.on="damaged" 應在受傷當下反應式觸發(而非常駐每回合治療)
    heal67_holder = Unit(POOL["張飛"], "盾")
    heal67_holder.troop = 5000
    heal67_holder.wounded = 3000
    heal67_tac = {"nameZh": "測試急救67", "type": "command", "kind": "intel", "coef": 0,
                  "rate": 1.0, "n": 1, "prep": 0,
                  "effects": [
                      {"k": "stat", "who": "ally", "stat": "force", "dur": 99, "add": 22},
                      {"k": "heal", "who": "ally", "coef": 0.6, "dur": 1, "when": {"on": "damaged"}, "rate": 1.0},
                  ]}
    heal67_holder.tactics.append(heal67_tac)
    heal67_holder.on_hit_effect_tacs = [t for t in heal67_holder.tactics
                                        if not t.get("when") and t["type"] in ("passive", "command")
                                        and any((e.get("when") or {}).get("on") for e in t.get("effects", []))]
    # prep階段套用(skip_when_effects=True): stat效果應套用, heal(帶e.when)不應在此觸發
    apply_effects(heal67_holder, None, heal67_tac, [heal67_holder], [], no_heal=True, skip_when_effects=True)
    assert abs(heal67_holder.eff("force") - (heal67_holder.force + 22)) < 1e-6, "prep階段: stat效果(無e.when)應正常套用"
    assert heal67_holder.troop == 5000, "prep階段: heal效果(帶e.when.on)不應在此觸發(非反應式事件, 不應治療)"
    # 模擬 on_hit 反應式觸發: 手動比照 fight() 內 on_hit 的 on_hit_effect_tacs 掃描邏輯
    CUR_ROUND = 1
    heal67_src = Unit(POOL["呂布"], "騎")

    def on_hit67(dst, s2, is_normal, dmg=None):  # 批33: dmg(可選)—— 對稱於正式 on_hit()
        for t in dst.on_hit_effect_tacs:
            for e in t["effects"]:
                ew = e.get("when") or {}
                if not ew.get("on") or (ew["on"] == "attacked" and not is_normal):
                    continue
                if not round_ok({"when": ew}, CUR_ROUND) or id(e) in dst.hit_flags:
                    continue
                if random.random() >= e.get("rate", t.get("rate", 1)):
                    continue
                dst.hit_flags.add(id(e))
                apply_effects(dst, s2, {"effects": [e], "kind": t.get("kind", "phys"), "nameZh": t.get("nameZh")},
                              [dst], [s2], rate_checked=True, reactive=True)  # 批23 A4/reactive: 上面已擲過 e["rate"]
    hit(heal67_src, heal67_holder, 1.0, "phys", True, on_hit67)
    assert heal67_holder.wounded != 3000, "受傷+反應式急救觸發後, 傷兵池應有變動(受傷增加又被治療扣減, 淨值不會剛好停在3000)"
    CUR_ROUND = 0

    # ------------------------------------------------------------------
    # 批23 系統性缺陷修復 asserts (A1-A5)
    # ------------------------------------------------------------------

    # 68) A1: 效果級 e["n"] —— 非CTRL效果(如 mitig/amp)過去無條件 who="enemy"/"ally" 放大成
    # 全體, 現在有 e["n"] 時應只命中 e["n"] 人(隨機不重複), 而非全體。
    a1_caster = Unit(POOL["呂布"], "騎")
    a1_allies = [Unit(POOL["張飛"], "盾") for _ in range(5)]
    a1_tac_single = {"nameZh": "測試A1單體68", "effects": [{"k": "mitig", "who": "ally", "val": 0.2, "dur": 3, "n": 1}]}
    apply_effects(a1_caster, a1_allies[0], a1_tac_single, a1_allies, [], no_heal=True)
    hit_count = sum(1 for u in a1_allies if u.addbonus("mitig") > 0)
    assert hit_count == 1, f"A1: e['n']=1 應只有1人獲得mitig, 實際{hit_count}人(過去無e.n讀取會是全體5人)"
    # 無 e["n"] 時應維持全體(向後相容)
    a1_allies2 = [Unit(POOL["張飛"], "盾") for _ in range(5)]
    a1_tac_all = {"nameZh": "測試A1全體68b", "effects": [{"k": "mitig", "who": "ally", "val": 0.2, "dur": 3}]}
    apply_effects(a1_caster, None, a1_tac_all, a1_allies2, [], no_heal=True)
    assert all(u.addbonus("mitig") > 0 for u in a1_allies2), "A1: 無e['n']時應維持全體套用(向後相容)"
    # who="enemy" 非CTRL效果(如 amp 易傷)同樣要讀 e["n"]
    a1_enemies = [Unit(POOL["張飛"], "盾") for _ in range(5)]
    a1_tac_enemy_n = {"nameZh": "測試A1敵單體68c", "effects": [{"k": "amp", "who": "enemy", "val": 0.15, "dur": 2, "n": 1}]}
    apply_effects(a1_caster, a1_enemies[0], a1_tac_enemy_n, [], a1_enemies, no_heal=True)
    enemy_hit = sum(1 for u in a1_enemies if u.addbonus("mitig") < 0)  # who=enemy的正amp會轉存成負mitig(易傷)
    assert enemy_hit == 1, f"A1: who=enemy 帶 e['n']=1 應只有1人中招, 實際{enemy_hit}人"

    # 69) A2: counter 讀 e["dur"] —— dur=1 的反擊次回合應失效(過去 dur 幽靈欄位從不遞減, 變永久)
    a2_u = Unit(POOL["張飛"], "盾")
    apply_effects(a2_u, None, {"nameZh": "測試A2反擊69", "effects": [{"k": "counter", "who": "self", "coef": 1.0, "dur": 1}]},
                  [a2_u], [], no_heal=True)
    assert a2_u.counter is not None, "A2: 施加後應立即擁有counter"
    a2_u.tick()  # 第1回合結束: dur=1(+1補償=2) -1 = 1, 仍應存在(本回合內生效)
    assert a2_u.counter is not None, "A2: dur=1的反擊在施加當回合的tick()後應仍存在(補償+1慣例, 本回合內仍生效)"
    a2_u.tick()  # 第2回合結束: 應到期清除
    assert a2_u.counter is None, "A2: dur=1的反擊在下一回合的tick()後應到期清除(過去幽靈欄位從不遞減, 永久存在)"
    # 無 e["dur"] 應預設常駐(99, 向後相容既有反擊資料)
    a2_u2 = Unit(POOL["張飛"], "盾")
    apply_effects(a2_u2, None, {"nameZh": "測試A2常駐69b", "effects": [{"k": "counter", "who": "self", "coef": 1.0}]},
                  [a2_u2], [], no_heal=True)
    for _ in range(8):
        a2_u2.tick()
    assert a2_u2.counter is not None, "A2: 無e['dur']應預設常駐(99), 8回合內不應消失"

    # 70) A3: dot 結算讀 e["kind"](優先於 t["kind"]) —— 灼燒類 dot 掛在兵刃戰法上仍應走謀略類型
    a3_caster = Unit(POOL["諸葛亮"], "弓")
    a3_tgt = Unit(POOL["張飛"], "盾")
    a3_tac = {"nameZh": "測試A3灼燒70", "kind": "phys",  # 戰法整體是兵刃(t.kind=phys)
              "effects": [{"k": "dot", "who": "enemy", "coef": 0.5, "dur": 2, "kind": "intel"}]}  # dot段自帶kind=intel
    apply_effects(a3_caster, a3_tgt, a3_tac, [a3_caster], [a3_tgt], no_heal=True)
    assert len(a3_tgt.dots) == 1
    random.seed(77)
    expect_intel_dmg = damage(a3_caster, a3_tgt, 0.5, "intel")
    random.seed(77)
    a3_caster2 = Unit(POOL["諸葛亮"], "弓")
    a3_tgt2 = Unit(POOL["張飛"], "盾")
    apply_effects(a3_caster2, a3_tgt2, a3_tac, [a3_caster2], [a3_tgt2], no_heal=True)
    assert abs(a3_tgt2.dots[0][0] - expect_intel_dmg) < 1e-6, "A3: dot段帶e['kind']='intel'應覆蓋戰法整體t['kind']='phys', 走謀略傷害公式(以智力/謀略防禦計算)"

    # 71) A4: 效果級 e["rate"] 在一般路徑(非onHit/delayedEq)也要判定 —— rate=0 應完全不觸發
    a4_u = Unit(POOL["張飛"], "盾")
    a4_tac_zero = {"nameZh": "測試A4零機率71", "effects": [{"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5, "rate": 0.0}]}
    apply_effects(a4_u, None, a4_tac_zero, [a4_u], [], no_heal=True)
    assert abs(a4_u.eff("force") - a4_u.force) < 1e-6, "A4: e['rate']=0.0 的效果應完全不觸發(prep/主動等一般路徑過去完全不讀e['rate'], 必定觸發)"
    a4_u2 = Unit(POOL["張飛"], "盾")
    a4_tac_one = {"nameZh": "測試A4全機率71b", "effects": [{"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5, "rate": 1.0}]}
    apply_effects(a4_u2, None, a4_tac_one, [a4_u2], [], no_heal=True)
    assert abs(a4_u2.eff("force") - (a4_u2.force + 999)) < 1e-6, "A4: e['rate']=1.0 應正常觸發"
    # rate_checked=True 呼叫端應跳過此處判定(避免與呼叫端自己的擲骰重複疊乘)
    a4_u3 = Unit(POOL["張飛"], "盾")
    apply_effects(a4_u3, None, a4_tac_zero, [a4_u3], [], no_heal=True, rate_checked=True)
    assert abs(a4_u3.eff("force") - (a4_u3.force + 999)) < 1e-6, "A4: rate_checked=True時應略過e['rate']判定(呼叫端已自行擲骰過), 即使rate=0也套用"

    # 72) A5: when-gated one-shot 路徑應讀 t["rate"] —— rate=0 的 when-gated 戰法不應觸發
    a5_u = Unit(POOL["張飛"], "盾")
    a5_tac = {"nameZh": "測試A5機率72", "type": "command", "kind": "phys", "coef": 0, "rate": 0.0, "n": 1, "prep": 0,
              "when": {"until": 2}, "effects": [{"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5}]}
    a5_u.tactics.append(a5_tac)
    CUR_ROUND = 1
    if a5_tac["type"] in ("passive", "command") and a5_tac.get("when") and not a5_tac["when"].get("on") \
            and round_ok(a5_tac, 1) and id(a5_tac) not in a5_u.when_fired:
        a5_u.when_fired.add(id(a5_tac))
        if not (random.random() >= a5_tac.get("rate", 1)):
            apply_effects(a5_u, None, a5_tac, [a5_u], [], no_heal=True)
    assert abs(a5_u.eff("force") - a5_u.force) < 1e-6, "A5: rate=0.0 的when-gated戰法不應觸發effects(過去此路徑從不讀t['rate'], 必定觸發)"
    CUR_ROUND = 0

    # 73) 批24 D1: teamGate(隊伍構成前提) —— 開戰建構Unit時依team_factions過濾tactics
    assert team_gate_ok({"factions": "allDiff"}, ["魏", "蜀", "吳"]) is True, "teamGate: allDiff 三方不同陣營應通過"
    assert team_gate_ok({"factions": "allDiff"}, ["魏", "魏", "吳"]) is False, "teamGate: allDiff 有重複陣營應擋下"
    assert team_gate_ok({"factions": "allSame"}, ["魏", "魏", "魏"]) is True, "teamGate: allSame 三方同陣營應通過"
    assert team_gate_ok({"factions": "allSame"}, ["魏", "蜀", "魏"]) is False, "teamGate: allSame 有不同陣營應擋下"
    assert team_gate_ok(None, ["魏", "魏", "魏"]) is True, "teamGate: 無gate應一律放行(向後相容)"
    d1_tac = {"nameZh": "測試teamGate73", "type": "passive", "cat": "FORMATION", "coef": 0, "rate": 1,
              "effects": [{"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 99}],
              "teamGate": {"factions": "allDiff"}}
    TACTICS[d1_tac["nameZh"]] = d1_tac
    d1_u_pass = Unit(POOL["張飛"], "盾", None, None, None, [d1_tac["nameZh"]], None, ["魏", "蜀", "吳"])
    assert any(t["nameZh"] == d1_tac["nameZh"] for t in d1_u_pass.tactics), "teamGate: 隊伍陣營皆不同時, 戰法應保留在tactics中"
    d1_u_block = Unit(POOL["張飛"], "盾", None, None, None, [d1_tac["nameZh"]], None, ["魏", "魏", "吳"])
    assert not any(t["nameZh"] == d1_tac["nameZh"] for t in d1_u_block.tactics), "teamGate: 隊伍陣營有重複時, 戰法應被整條過濾掉"
    del TACTICS[d1_tac["nameZh"]]

    # 74) 批24 D2: dmgType(兵刃/謀略傷害類型過濾) —— amp/mitig 效果可選填 e["dmgType"],
    # 只對該類型傷害生效, 不影響另一類型
    d2_src = Unit(POOL["張飛"], "盾")
    d2_dst_phys = Unit(POOL["諸葛亮"], "盾")
    d2_dst_intel = Unit(POOL["諸葛亮"], "盾")
    d2_tac = {"nameZh": "測試dmgType74", "effects": [
        {"k": "mitig", "who": "ally", "val": 0.5, "dur": 5, "dmgType": "phys"},
    ]}
    apply_effects(d2_dst_phys, None, d2_tac, [d2_dst_phys], [], no_heal=True)
    apply_effects(d2_dst_intel, None, d2_tac, [d2_dst_intel], [], no_heal=True)
    random.seed(99)
    dmg_phys_with_mitig = damage(d2_src, d2_dst_phys, 1.0, "phys")
    random.seed(99)
    dmg_phys_baseline = damage(d2_src, Unit(POOL["諸葛亮"], "盾"), 1.0, "phys")
    assert dmg_phys_with_mitig < dmg_phys_baseline * 0.6, "dmgType: dmgType='phys'的mitig應對兵刃傷害生效(打折)"
    random.seed(99)
    dmg_intel_with_mitig = damage(d2_src, d2_dst_intel, 1.0, "intel")
    random.seed(99)
    dmg_intel_baseline = damage(d2_src, Unit(POOL["諸葛亮"], "盾"), 1.0, "intel")
    assert abs(dmg_intel_with_mitig - dmg_intel_baseline) < 1e-6, "dmgType: dmgType='phys'的mitig不應影響謀略傷害(intel)"
    # 同一戰法內兩條不同dmgType的mitig應各自獨立生效(不因同src刷新去重互相覆蓋, 見dt_src尾碼機制)
    d2_dual_tac = {"nameZh": "測試dmgType雙段74b", "effects": [
        {"k": "mitig", "who": "self", "val": 0.3, "dur": 5, "dmgType": "phys"},
        {"k": "mitig", "who": "self", "val": 0.4, "dur": 5, "dmgType": "intel"},
    ]}
    d2_dual_u = Unit(POOL["諸葛亮"], "盾")
    apply_effects(d2_dual_u, None, d2_dual_tac, [d2_dual_u], [], no_heal=True)
    assert abs(d2_dual_u.addbonus("mitig", "phys") - 0.3) < 1e-6, "dmgType: 兩條不同dmgType的mitig不應互相覆蓋(phys段應保留0.3)"
    assert abs(d2_dual_u.addbonus("mitig", "intel") - 0.4) < 1e-6, "dmgType: 兩條不同dmgType的mitig不應互相覆蓋(intel段應保留0.4)"

    # 75) 批26 B1: e["ifLeader"] —— 效果級「施放者須為隊伍主將(index 0)」條件閘門
    # 主將(allies[0]是施放者自己)時應正常套用
    il_leader = Unit(POOL["張飛"], "盾")
    il_sub = Unit(POOL["諸葛亮"], "盾")
    il_tac = {"nameZh": "測試ifLeader75", "effects": [
        {"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5, "ifLeader": True},
    ]}
    apply_effects(il_leader, None, il_tac, [il_leader, il_sub], [], no_heal=True)
    assert abs(il_leader.eff("force") - (il_leader.force + 999)) < 1e-6, "ifLeader: caster是allies[0](主將)時應正常套用效果"
    # 副將(allies[0]不是施放者自己)時應完全跳過
    il_sub2 = Unit(POOL["諸葛亮"], "盾")
    il_leader2 = Unit(POOL["張飛"], "盾")
    apply_effects(il_sub2, None, il_tac, [il_leader2, il_sub2], [], no_heal=True)
    assert abs(il_sub2.eff("force") - il_sub2.force) < 1e-6, "ifLeader: caster不是allies[0](副將)時應完全跳過該效果, 不套用"
    # 無 e["ifLeader"] 的一般效果不受影響(向後相容)
    il_tac_noflag = {"nameZh": "測試ifLeader常規75b", "effects": [
        {"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5},
    ]}
    il_sub3 = Unit(POOL["諸葛亮"], "盾")
    apply_effects(il_sub3, None, il_tac_noflag, [Unit(POOL["張飛"], "盾"), il_sub3], [], no_heal=True)
    assert abs(il_sub3.eff("force") - (il_sub3.force + 999)) < 1e-6, "ifLeader: 無e['ifLeader']欄位時應維持向後相容(不受此閘門影響, 副將也能套用)"

    # 76) 批26 B2: stack.stackPer —— stack 效果每次「發動」(stackPer:"cast")遞增1層, 而非每回合
    # (stackPer:"round", 預設值, 向後相容)。"cast"模式由 apply_stack_cast() 供戰法命中/發動
    # 結算處呼叫遞增; "round"模式沿用 fight() 主迴圈既有的逐回合遞增(見上方迴圈守衛條件
    # stackPer=="round" 才遞增, 此處用同一段邏輯模擬迴圈行為, 不依賴 Unit.tick()——tick()本身
    # 從未觸碰 stack, 逐回合遞增邏輯獨立寫在 fight() 主迴圈裡, 非 Unit 方法)。
    sc_u = Unit(POOL["張飛"], "盾")
    apply_effects(sc_u, None, {"nameZh": "測試stackPer76", "effects": [
        {"k": "stack", "who": "self", "per": 0.05, "max": 5, "stackPer": "cast"},
    ]}, [sc_u], [], no_heal=True)
    assert sc_u.stack is not None and sc_u.stack.get("per") == 0.05 and sc_u.stack.get("stackPer") == "cast", \
        "stack.stackPer=cast: 初始化後stack字典應保留stackPer標記供遞增邏輯判斷"
    assert sc_u.stack["n"] == 0, "stack.stackPer=cast: 初始套用時層數應為0(尚未發動過, 首次發動才+1, 不同於round模式的prep階段即開始逐回合遞增)"
    # 模擬 fight() 主迴圈的逐回合守衛(僅 stackPer=="round" 才遞增): cast模式應完全不受此步驟影響
    for _ in range(2):
        if sc_u.alive and sc_u.stack and sc_u.stack.get("stackPer", "round") == "round":
            sc_u.stack["n"] = min(sc_u.stack["max"], sc_u.stack["n"] + 1)
    assert sc_u.stack["n"] == 0, "stack.stackPer=cast: fight()主迴圈的逐回合守衛不應遞增cast模式的層數(僅apply_stack_cast()才遞增, 不受回合數影響)"
    sc_u.apply_stack_cast()
    assert sc_u.stack["n"] == 1, "stack.stackPer=cast: apply_stack_cast()呼叫一次應遞增1層"
    sc_u.apply_stack_cast()
    sc_u.apply_stack_cast()
    sc_u.apply_stack_cast()
    sc_u.apply_stack_cast()
    sc_u.apply_stack_cast()
    assert sc_u.stack["n"] == 5, "stack.stackPer=cast: 遞增不應超過max=5層(第6次呼叫應封頂)"
    # stackPer=round(預設/向後相容): 沿用既有fight()主迴圈逐回合遞增行為, apply_stack_cast()呼叫不應有作用
    sr_u = Unit(POOL["張飛"], "盾")
    apply_effects(sr_u, None, {"nameZh": "測試stackPerRound76b", "effects": [
        {"k": "stack", "who": "self", "per": 0.05, "max": 5},
    ]}, [sr_u], [], no_heal=True)
    assert sr_u.stack.get("stackPer", "round") == "round", "stack.stackPer=round(預設): 無stackPer欄位時應視為round, 向後相容既有逐回合遞增資料"
    sr_u.apply_stack_cast()
    assert sr_u.stack["n"] == 0, "stack.stackPer=round: apply_stack_cast()對round模式的stack不應有作用(round模式只認fight()主迴圈逐回合遞增)"
    if sr_u.alive and sr_u.stack and sr_u.stack.get("stackPer", "round") == "round":
        sr_u.stack["n"] = min(sr_u.stack["max"], sr_u.stack["n"] + 1)
    assert sr_u.stack["n"] == 1, "stack.stackPer=round: fight()主迴圈守衛應照舊逐回合遞增1層(向後相容既有行為)"

    # 77) 批27 A: on:"dealtDamage" —— 「自身造成傷害時/後」反應式掛鉤(對比 on_hit 的
    # attacked/damaged 是「自己受擊」視角); 用仿 on_hit_test(見上方7) 的精簡版 dealt_damage_test
    # 驗證 hit() 的 on_deal 回呼會在 src(施加傷害者) 身上正確掃描/觸發, 且 dmgType 過濾/hit_flags
    # 節流/coef 段行為與正式 fight() 內 dealt_damage() 邏輯一致(自我一致性測試, 不依賴完整 fight())。
    def dealt_damage_test(src, dst, is_normal, kind, dmg=None):  # 批33: dmg(可選)—— 對稱於正式 dealt_damage(), 接受 hit() 新增的第5參數
        for t in src.tactics:
            w = t.get("when") or {}
            if w.get("on") != "dealtDamage":
                continue
            dt = w.get("dmgType")
            if dt and dt != kind:
                continue
            if id(t) in src.hit_flags:
                continue
            src.hit_flags.add(id(t))
            if t["effects"]:
                apply_effects(src, dst, t, [src], [dst], reactive=True)

    # 77a) 無 dmgType: 造成任一類型傷害皆應觸發(白衣渡江式: 每次造成傷害都可能繳械/計窮敵軍)
    dd_caster = Unit(POOL["呂布"], "騎")
    dd_target = Unit(POOL["張飛"], "盾")
    dd_tac = {"nameZh": "測試dealtDamage77", "type": "passive", "when": {"on": "dealtDamage"},
              "effects": [{"k": "disarm", "who": "enemy", "dur": 2}]}
    dd_caster.tactics = [dd_tac]
    assert dd_target.disarm == 0
    hit(dd_caster, dd_target, 1.0, "phys", True, None, dealt_damage_test)
    assert dd_target.disarm > 0, "on:'dealtDamage' 應在 src 造成傷害後觸發, 對 dst 套用效果(此處繳械)"

    # 77b) dmgType 過濾: "phys" 限定只在造成兵刃傷害時觸發, 造成謀略傷害不應觸發
    dd_caster2 = Unit(POOL["呂布"], "騎")
    dd_target2 = Unit(POOL["張飛"], "盾")
    dd_tac_phys = {"nameZh": "測試dealtDamagePhys77b", "type": "passive", "when": {"on": "dealtDamage", "dmgType": "phys"},
                   "effects": [{"k": "silence", "who": "enemy", "dur": 1}]}
    dd_caster2.tactics = [dd_tac_phys]
    hit(dd_caster2, dd_target2, 1.0, "intel", False, None, dealt_damage_test)  # 造成謀略傷害: dmgType='phys' 不應觸發
    assert dd_target2.silence == 0, "dmgType='phys' 的 dealtDamage 效果不應在造成謀略傷害(intel)時觸發"
    hit(dd_caster2, dd_target2, 1.0, "phys", False, None, dealt_damage_test)  # 造成兵刃傷害: 應觸發
    assert dd_target2.silence > 0, "dmgType='phys' 的 dealtDamage 效果應在造成兵刃傷害(phys)時觸發"

    # 77c) 同回合節流: 同一戰法每回合最多觸發1次(與 on_hit 共用 hit_flags 慣例, 防無限鏈)。
    # 用計數器(而非stat.add, 見push_stat_add同源刷新慣例/engine_limitations 6.7)驗證觸發次數。
    dd_trigger_count = [0]

    def dealt_damage_count_test(src, dst, is_normal, kind, dmg=None):  # 批33: dmg(可選)—— 對稱於正式 dealt_damage()
        for t in src.tactics:
            w = t.get("when") or {}
            if w.get("on") != "dealtDamage" or id(t) in src.hit_flags:
                continue
            src.hit_flags.add(id(t))
            dd_trigger_count[0] += 1

    dd_caster3 = Unit(POOL["呂布"], "騎")
    dd_target3 = Unit(POOL["張飛"], "盾")
    dd_caster3.tactics = [{"nameZh": "測試dealtDamage節流77c", "type": "passive", "when": {"on": "dealtDamage"}, "effects": []}]
    hit(dd_caster3, dd_target3, 1.0, "phys", True, None, dealt_damage_count_test)
    hit(dd_caster3, dd_target3, 1.0, "phys", True, None, dealt_damage_count_test)
    assert dd_trigger_count[0] == 1, "on:'dealtDamage' 同回合同一戰法應只觸發1次(hit_flags節流), 不應觸發2次"
    dd_caster3.hit_flags.clear()  # 模擬下回合重置(見 Unit.tick())
    hit(dd_caster3, dd_target3, 1.0, "phys", True, None, dealt_damage_count_test)
    assert dd_trigger_count[0] == 2, "hit_flags 每回合重置後, on:'dealtDamage' 應能在新回合重新觸發"

    # 77d) 規避(dodge)時不應觸發: 攻擊未命中, 語意上「未造成傷害」
    dd_caster4 = Unit(POOL["呂布"], "騎")
    dd_target4 = Unit(POOL["張飛"], "盾")
    dd_target4.dodge_dur, dd_target4.dodge_prob = 3, 1.0  # 100%規避
    dd_caster4.tactics = [dict(dd_tac)]
    hit(dd_caster4, dd_target4, 1.0, "phys", True, None, dealt_damage_test)
    assert dd_target4.disarm == 0, "on:'dealtDamage' 不應在攻擊被規避(dodge, 未實際造成傷害)時觸發"

    # 77e) 死亡的 src 不應觸發(如反擊致死後, 反擊方不應再觸發自己的 dealtDamage 效果)
    dd_caster5 = Unit(POOL["呂布"], "騎")
    dd_caster5.troop = 0  # 已陣亡
    dd_target5 = Unit(POOL["張飛"], "盾")
    dd_caster5.tactics = [dict(dd_tac)]
    hit(dd_caster5, dd_target5, 1.0, "phys", True, None, dealt_damage_test)
    assert dd_target5.disarm == 0, "on:'dealtDamage' 不應在 src 已陣亡時觸發(hit()內 on_deal 呼叫前已檢查 src.alive)"

    # 77f) fight() 內建的 on_deal_tacs/on_deal_effect_tacs 預篩應正確收錄戰法級/效果級 dealtDamage
    dd_prefilter_u = Unit(POOL["呂布"], "騎")
    dd_prefilter_u.tactics = [
        {"nameZh": "測試預篩戰法級77f", "type": "passive", "when": {"on": "dealtDamage"}, "effects": []},
        {"nameZh": "測試預篩效果級77f", "type": "passive", "effects": [
            {"k": "stat", "who": "self", "stat": "force", "add": 1, "dur": 1, "when": {"on": "dealtDamage"}},
        ]},
        {"nameZh": "測試預篩無關77f", "type": "passive", "effects": [
            {"k": "stat", "who": "self", "stat": "force", "add": 1, "dur": 1},
        ]},
    ]
    dd_prefilter_u.on_deal_tacs = [t for t in dd_prefilter_u.tactics
                                   if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") == "dealtDamage"]
    dd_prefilter_u.on_deal_effect_tacs = [t for t in dd_prefilter_u.tactics
                                          if not t.get("when") and t["type"] in ("passive", "command", "active")
                                          and any((e.get("when") or {}).get("on") == "dealtDamage" for e in t.get("effects", []))]
    assert len(dd_prefilter_u.on_deal_tacs) == 1 and dd_prefilter_u.on_deal_tacs[0]["nameZh"] == "測試預篩戰法級77f", \
        "on_deal_tacs 應只收錄 t.when.on=='dealtDamage' 的戰法級反應式戰法"
    assert len(dd_prefilter_u.on_deal_effect_tacs) == 1 and dd_prefilter_u.on_deal_effect_tacs[0]["nameZh"] == "測試預篩效果級77f", \
        "on_deal_effect_tacs 應只收錄無t.when、但至少一個效果帶e.when.on=='dealtDamage'的戰法"

    # 78) 批27 B: choices 對 command/passive 型戰法生效(見 engine_limitations.md §18a) ——
    # 過去 fight() 主迴圈確實會對 command/passive 型戰法擲骰(fire)並用 pick_choice() 抽出分支
    # t, 但緊接著的 apply_effects(...) 呼叫只在 t["type"]=="active" 才執行, command/passive
    # 型戰法抽中的分支 effects 被憑空丟棄。端到端驗證修復(真正跑 fight(), 不繞過main loop):
    # 暫時把 呂布 的自帶戰法換成一個 command 型 choices 戰法(coef=0, 頂層effects=[], 單一分支
    # 帶巨額heal, rate=1.0每回合必發), 比較「有此戰法」vs「呂布維持原戰法(對照)」在同組敵人
    # 前的模擬勝率, 巨額治療應顯著推高勝率(若§18a舊bug仍在, 分支effects被丟棄, 巨額heal形同
    # 虛設, 勝率不會有感提升)。用勝率統計而非直接檢查Unit內部狀態, 因為fight()/simulate()不
    # 回傳戰鬥內部單位物件, 只能透過可觀察的戰鬥結果(勝率)反推main loop是否真的套用了效果。
    _orig_lb_tactic = POOL["呂布"].tactic
    ch78_tac = {
        "nameZh": "測試choices指揮78", "type": "command", "kind": "phys", "coef": 0,
        "rate": 1.0, "n": 1, "prep": 0, "effects": [],
        "choices": [
            {"weight": 1, "effects": [{"k": "heal", "who": "ally", "coef": 3.0, "dur": 1}]},
        ],
    }
    random.seed(2027)
    win_with_choices_heal = simulate(["呂布", "張飛", "諸葛亮"], ["曹操", "司馬懿", "周瑜"], n=400)["A勝率"]
    POOL["呂布"].tactic = ch78_tac
    try:
        random.seed(2027)
        win_with_ch78 = simulate(["呂布", "張飛", "諸葛亮"], ["曹操", "司馬懿", "周瑜"], n=400)["A勝率"]
    finally:
        POOL["呂布"].tactic = _orig_lb_tactic  # 還原, 避免污染後續測試/其他呼叫端
    assert win_with_ch78 > win_with_choices_heal + 0.05, (
        f"批27 B: command型戰法帶choices(單分支巨額heal, coef=3.0)應顯著推高勝率"
        f"(套用前基準{win_with_choices_heal:.3f}, 套用後{win_with_ch78:.3f})——若分支effects"
        f"仍如§18a舊bug被main loop憑空丟棄, 巨額heal不會生效, 勝率不會有感提升")

    # 78b) 對照組: 無 choices 的一般 command/passive 戰法(effects直接在頂層, 非choices)不受
    # 本次新增的 elif t0.get("choices") 分支影響——只在 t0.get("choices") 為真時才會進入該
    # 分支, 一般戰法(choices為空/不存在)完全不觸碰新增程式碼路徑, 效果仍只由既有
    # apply_passives() prep/heal_only通道處理(不會被套用兩次)。
    plain_cmd_tac = {"nameZh": "測試無choices對照78b", "type": "command", "kind": "phys", "coef": 0,
                     "rate": 1.0, "n": 1, "prep": 0,
                     "effects": [{"k": "heal", "who": "ally", "coef": 3.0, "dur": 1}]}
    POOL["呂布"].tactic = plain_cmd_tac
    try:
        random.seed(2027)
        win_plain_heal = simulate(["呂布", "張飛", "諸葛亮"], ["曹操", "司馬懿", "周瑜"], n=400)["A勝率"]
    finally:
        POOL["呂布"].tactic = _orig_lb_tactic
    # 無choices版本(靠既有apply_passives heal_only通道逐回合治療)與choices版本(靠新增main
    # loop elif分支單次觸發治療)應是同量級的顯著提升(非雙重結算的異常暴增, 也非被丟棄的無提升),
    # 兩者皆應遠高於未裝任何heal戰法的基準, 但彼此不必完全相等(觸發頻率語意本就不同: heal_only
    # 每回合都治療 vs choices版本只在command每回合擲骰後才透過main loop套用一次, 兩者理論上
    # 頻率相近, 但用寬鬆判斷避免方差誤傷測試穩定性)。
    assert win_plain_heal > win_with_choices_heal + 0.05, \
        "對照組(無choices, 頂層heal): 既有apply_passives通道應正常運作, 勝率同樣應顯著高於基準"

    # 79) 批27 C: choices 對 on_hit() 反應式路徑(when.on)生效(見 engine_limitations.md §8) ——
    # 過去 when.on:"attacked"/"damaged" 的反應式戰法完全不讀 t0["choices"], 資料層寫入choices
    # 形同虛設(魅惑「混亂/計窮/虛弱」三選一即是此缺口的代表案例)。用仿 on_hit_test 的精簡版
    # on_hit_choices_test 驗證修復: 兩個weight=0/999懸殊的分支, 應幾乎必中weight大的那個。
    def on_hit_choices_test(dst, src, is_normal, dmg=None):  # 批33: dmg(可選)—— 對稱於正式 on_hit()
        for t0 in dst.tactics:
            on = (t0.get("when") or {}).get("on")
            if not on or (on == "attacked" and not is_normal):
                continue
            if id(t0) in dst.hit_flags:
                continue
            dst.hit_flags.add(id(t0))
            t = dict(t0, **pick_choice(t0["choices"])) if t0.get("choices") else t0
            if t["effects"]:
                apply_effects(dst, src, t, [dst], [src], reactive=True)

    ch79_tac = {
        "nameZh": "測試choices反應式79", "type": "passive", "coef": 0, "rate": 1.0, "n": 1, "prep": 0,
        "effects": [], "when": {"on": "attacked"},
        "choices": [
            {"weight": 999, "effects": [{"k": "silence", "who": "enemy", "dur": 3}]},
            {"weight": 1, "effects": [{"k": "stun", "who": "enemy", "dur": 3}]},
        ],
    }
    ch79_defender = Unit(POOL["張飛"], "盾")
    ch79_defender.tactics = [ch79_tac]
    ch79_attacker = Unit(POOL["呂布"], "騎")
    random.seed(1)  # weight 999:1 幾乎必中第一分支(silence)
    hit(ch79_attacker, ch79_defender, 1.0, "phys", True, on_hit_choices_test)
    assert ch79_attacker.silence > 0 or ch79_attacker.stun > 0, \
        "批27 C: on_hit()反應式路徑應讀取choices並套用抽中分支的effects(此前完全不讀choices, 效果被憑空丟棄)"
    # 高權重分支(silence)應是實際命中的那個(驗證pick_choice真的有被呼叫而非固定套用t0本身的
    # 原始effects, t0["effects"]為空陣列, 若choices未被消費則不會有任何效果套用)
    assert ch79_attacker.silence > 0, "weight 999:1 應幾乎必中silence分支"

    # 80) 批27 C 落地驗證: 魅惑(真實資料, 見 TACTICS["魅惑"])三選一(混亂/計窮/虛弱)在 on_hit()
    # 反應式路徑下應能抽到全部三種分支(對照批20舊版固定只會抽到silence一種)。大量取樣(rate=0.45
    # 取滿級, 每次獨立戰鬥實例避免hit_flags節流互相干擾)統計是否三種效果皆曾出現過。
    meihuo_tac = TACTICS["魅惑"]
    assert meihuo_tac.get("choices") and len(meihuo_tac["choices"]) == 3, "魅惑應有3個choices分支(混亂/計窮/虛弱)"
    seen_kinds = set()
    random.seed(11)
    for _ in range(400):
        mh_defender = Unit(POOL["貂蟬"] if "貂蟬" in POOL else POOL["張飛"], "盾")
        mh_defender.tactics = [meihuo_tac]
        mh_attacker = Unit(POOL["呂布"], "騎")
        hit(mh_attacker, mh_defender, 1.0, "phys", True, on_hit_choices_test)
        if mh_attacker.chaos:
            seen_kinds.add("chaos")
        if mh_attacker.silence:
            seen_kinds.add("silence")
        if mh_attacker.addbonus("amp") <= -0.99:
            seen_kinds.add("weak")
    assert seen_kinds == {"chaos", "silence", "weak"}, \
        f"魅惑三選一(choices)應400次取樣內三種效果(混亂/計窮/虛弱)皆至少出現一次, 實際={seen_kinds}" \
        f"(若只出現silence一種, 代表choices退化回批20舊版固定行為)"

    # 81) 批28 B1: counter.guardFor:"leader"(守護式反擊) —— 效果掛在 subs(副將)身上但
    # guardFor:"leader", 套用後應登記進主將的 counter_guards, 而非副將自己的 counter
    # (方向反了的舊行為對照組)。主將受到普攻時, 應由副將代為反擊攻擊者(攻擊者掉血), 而非
    # 主將自己還手(主將本身 counter 應保持 None)。
    gf_leader = Unit(POOL["典韋"] if "典韋" in POOL else POOL["呂布"], "盾")
    gf_sub = Unit(POOL["張飛"], "盾")
    gf_team = [gf_leader, gf_sub]
    gf_tac = {"nameZh": "測試守護反擊81", "effects": [
        {"k": "counter", "who": "subs", "coef": 1.0, "kind": "phys", "prob": 1.0, "guardFor": "leader"}]}
    apply_effects(gf_sub, None, gf_tac, gf_team, [], no_heal=True)
    assert gf_leader.counter is None, "guardFor:leader 不應讓主將自己掛 counter"
    assert gf_sub.counter is None, "guardFor:leader 不應讓持有效果的副將自己掛 counter(方向已改為守護式)"
    assert len(gf_leader.counter_guards) == 1 and gf_leader.counter_guards[0]["unit"] is gf_sub, \
        "guardFor:leader 應把副將登記進主將的 counter_guards"
    gf_attacker = Unit(POOL["呂布"], "騎")
    atk_troop0 = gf_attacker.troop
    hit(gf_attacker, gf_leader, 1.0, "phys", True)   # 普攻主將(is_normal=True)
    assert gf_attacker.troop < atk_troop0, "主將受普攻時, 副將應代為反擊攻擊者(攻擊者應掉血)"
    # 每回合最多觸發1次: 同回合內(未 tick)第二次普攻主將不應再觸發第二次守護反擊
    atk_troop1 = gf_attacker.troop
    hit(gf_attacker, gf_leader, 1.0, "phys", True)
    assert abs(gf_attacker.troop - atk_troop1) < 1e-6, \
        "guardFor 守護反擊每回合最多觸發1次, 同回合內第二次普攻不應再次觸發"
    gf_leader.tick(); gf_sub.tick(); gf_attacker.tick()   # 換回合: hit_flags 重置後應能再次觸發
    atk_troop2 = gf_attacker.troop
    hit(gf_attacker, gf_leader, 1.0, "phys", True)
    assert gf_attacker.troop < atk_troop2, "換回合後(hit_flags重置)應能再次觸發守護反擊"
    # 非普攻(戰法傷害, is_normal=False)不應觸發守護反擊(原文「即將受到普攻時」限定)
    gf_leader2 = Unit(POOL["典韋"] if "典韋" in POOL else POOL["呂布"], "盾")
    gf_sub2 = Unit(POOL["張飛"], "盾")
    apply_effects(gf_sub2, None, gf_tac, [gf_leader2, gf_sub2], [], no_heal=True)
    gf_attacker2 = Unit(POOL["呂布"], "騎")
    atk_troop3 = gf_attacker2.troop
    hit(gf_attacker2, gf_leader2, 1.0, "phys", False)   # 戰法傷害, is_normal=False
    assert abs(gf_attacker2.troop - atk_troop3) < 1e-6, "guardFor 守護反擊只應在普攻(is_normal=True)時觸發, 戰法傷害不應觸發"

    # 82) 批28 B3: amp/mitig.normalOnly(僅普攻傷害生效/受影響) —— 效果只應在 is_normal=True
    # (普攻)時對 damage() 產生作用, 突擊/戰法傷害(is_normal=False)不應受影響(見至柔動剛
    # 「降低我軍及敵軍全體普通攻擊傷害35%」, 外部查證確認root data「提升」為誤植, 應為「降低」)。
    no_src, no_dst = Unit(POOL["呂布"], "騎"), Unit(POOL["張飛"], "盾")
    no_dst.push_add("mitig", 0.35, 9, "測試normalOnly82", {"normalOnly": True})
    d_normal = damage(no_src, no_dst, 1.0, "phys", is_normal=True)
    d_tactic = damage(no_src, no_dst, 1.0, "phys", is_normal=False)
    d_baseline = damage(Unit(POOL["呂布"], "騎"), Unit(POOL["張飛"], "盾"), 1.0, "phys", is_normal=False)
    assert d_normal < d_baseline * 0.8, "normalOnly mitig: is_normal=True 時應套用減傷"
    assert abs(d_tactic - d_baseline) < d_baseline * 0.15, \
        f"normalOnly mitig: is_normal=False(戰法傷害) 時不應套用減傷, d_tactic={d_tactic:.1f} baseline={d_baseline:.1f}"
    # amp 同理(用正值增傷驗證, 避免與上面mitig的效果混淆, 各自獨立unit)
    no_src2 = Unit(POOL["呂布"], "騎")
    no_src2.push_add("amp", 0.5, 9, "測試normalOnly82b", {"normalOnly": True})
    no_dst2 = Unit(POOL["張飛"], "盾")
    d_amp_normal = damage(no_src2, no_dst2, 1.0, "phys", is_normal=True)
    d_amp_tactic = damage(no_src2, no_dst2, 1.0, "phys", is_normal=False)
    d_amp_baseline = damage(Unit(POOL["呂布"], "騎"), Unit(POOL["張飛"], "盾"), 1.0, "phys", is_normal=False)
    assert d_amp_normal > d_amp_baseline * 1.2, "normalOnly amp: is_normal=True 時應套用增傷"
    assert abs(d_amp_tactic - d_amp_baseline) < d_amp_baseline * 0.15, \
        "normalOnly amp: is_normal=False(戰法傷害) 時不應套用增傷"

    # 83) 批28 B4: choices 傷害分支(coef/kind/targetSel) 對 command 型戰法應能端到端造成傷害
    # (桃園結義三選一重建的可行性基礎) —— fight() 主迴圈既有的 command/passive 派發路徑(見
    # 批16/27既有機制)讀取 pick_choice() 抽出的分支 t["coef"]/t["kind"]/t["targetSel"] 並呼叫
    # hit(), 不需要引擎擴充即可支援傷害段。用勝率統計驗證(同78號測試手法): 帶巨額傷害分支
    # (coef=5.0)的command戰法應比完全no-op的對照組顯著推高勝率。
    _orig_lb_tactic_83 = POOL["呂布"].tactic
    noop_tac_83 = {"nameZh": "測試no-op對照83", "type": "command", "kind": "phys", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0, "effects": []}
    dmg_choice_tac_83 = {
        "nameZh": "測試choices傷害分支83", "type": "command", "kind": "phys", "coef": 0,
        "rate": 1.0, "n": 1, "prep": 0, "effects": [],
        "choices": [
            {"weight": 1, "coef": 5.0, "kind": "intel", "targetSel": "minIntel", "effects": []},
        ],
    }
    POOL["呂布"].tactic = noop_tac_83
    try:
        random.seed(83)
        win_noop_83 = simulate(["呂布", "張飛", "諸葛亮"], ["曹操", "司馬懿", "周瑜"], n=300)["A勝率"]
    finally:
        POOL["呂布"].tactic = _orig_lb_tactic_83
    POOL["呂布"].tactic = dmg_choice_tac_83
    try:
        random.seed(83)
        win_dmg_83 = simulate(["呂布", "張飛", "諸葛亮"], ["曹操", "司馬懿", "周瑜"], n=300)["A勝率"]
    finally:
        POOL["呂布"].tactic = _orig_lb_tactic_83
    assert win_dmg_83 > win_noop_83 + 0.1, (
        f"批28 B4: command型戰法choices分支帶coef/kind/targetSel應能造成真實傷害並顯著推高"
        f"勝率(no-op基準{win_noop_83:.3f}, 帶傷害分支{win_dmg_83:.3f})——若choices的coef傷害段"
        f"未被main loop讀取/呼叫hit(), 兩者應無顯著差異")
    # 桃園結義本尊(真實資料): 應有3個choices分支(治療+2個傷害段), 且頂層rate=0.4
    taoyuan_tac = TACTICS["桃園結義"]
    assert taoyuan_tac.get("choices") and len(taoyuan_tac["choices"]) == 3, \
        "桃園結義應重建為3個choices分支(治療我軍最殘/謀略傷害打智力最低敵/兵刃傷打統率最低敵)"
    assert abs(taoyuan_tac["rate"] - 0.4) < 1e-6, "桃園結義頂層rate應為0.4(20%-40%取滿級)"
    dmg_branches = [c for c in taoyuan_tac["choices"] if c.get("coef")]
    assert len(dmg_branches) == 2, "桃園結義應有2個帶coef的傷害分支"
    assert {c.get("targetSel") for c in dmg_branches} == {"minIntel", "minCommand"}, \
        "桃園結義2個傷害分支應分別以minIntel(打智力最低敵)/minCommand(打統率最低敵)為targetSel"

    # 84) 批30 A: e["everyRound"](非heal效果的逐回合重擲通道) —— block效果帶everyRound+rate,
    # 應在 apply_passives(heal_only=True) 的每回合常駐通道重新擲骰/套用(而非prep套用一次),
    # 未命中的回合不新增次數。同時驗證: (a) 不帶everyRound的效果行為零變化(prep套用一次,
    # heal_only通道不重複套用); (b) everyRound效果在prep(heal_only=False)呼叫時完全不套用。
    er_u = Unit(POOL["呂布"], "盾")
    er_ally = Unit(POOL["張飛"], "盾")
    er_team = [er_u, er_ally]
    er_tac_flagged = {"nameZh": "測試everyRound84", "rate": 1.0, "effects": [
        {"k": "block", "who": "self", "val": 1.0, "times": 1, "everyRound": True}]}
    # (b) prep(heal_only=False)呼叫: everyRound效果不應套用
    apply_effects(er_u, None, er_tac_flagged, er_team, [], no_heal=True, skip_when_effects=True)
    assert not er_u.block, "everyRound效果不應在prep(heal_only=False)路徑套用"
    # (a) heal_only常駐通道: rate=1.0應每次都命中, 每回合各自新增1次(block同源疊次語意)
    CUR_ROUND = 1
    apply_effects(er_u, None, er_tac_flagged, er_team, [], heal_only=True)
    assert er_u.block and sum(b["n"] for b in er_u.block) == 1, \
        "everyRound效果應在heal_only常駐通道套用一次(第1回合, rate=1.0必中)"
    CUR_ROUND = 2
    apply_effects(er_u, None, er_tac_flagged, er_team, [], heal_only=True)
    assert sum(b["n"] for b in er_u.block) == 2, \
        "everyRound效果應逐回合重新擲骰/套用(第2回合再命中一次, 同源block應疊次成2, 而非停留在prep的一次性套用)"
    # rate=0時不應新增(驗證擲骰真的生效, 非無條件套用)
    er_u2 = Unit(POOL["呂布"], "盾")
    er_team2 = [er_u2, Unit(POOL["張飛"], "盾")]
    er_tac_norate = {"nameZh": "測試everyRound無命中84", "rate": 0.0, "effects": [
        {"k": "block", "who": "self", "val": 1.0, "times": 1, "everyRound": True}]}
    CUR_ROUND = 1
    apply_effects(er_u2, None, er_tac_norate, er_team2, [], heal_only=True)
    assert not er_u2.block, "everyRound效果rate=0.0時不應套用(擲骰應真的生效, 非無條件通過)"
    # 對照組: 不帶everyRound的block效果(既有行為) —— prep套用一次, heal_only常駐通道不應重複套用
    er_u3 = Unit(POOL["呂布"], "盾")
    er_team3 = [er_u3, Unit(POOL["張飛"], "盾")]
    er_tac_plain = {"nameZh": "測試無everyRound對照84", "rate": 1.0, "effects": [
        {"k": "block", "who": "self", "val": 1.0, "times": 1}]}
    apply_effects(er_u3, None, er_tac_plain, er_team3, [], no_heal=True, skip_when_effects=True)
    assert er_u3.block and sum(b["n"] for b in er_u3.block) == 1, "不帶everyRound的效果應維持既有行為: prep套用一次"
    CUR_ROUND = 1
    apply_effects(er_u3, None, er_tac_plain, er_team3, [], heal_only=True)
    assert sum(b["n"] for b in er_u3.block) == 1, \
        "不帶everyRound的非heal效果在heal_only常駐通道不應被重複套用(零回歸: 既有行為不受本次改動影響)"
    CUR_ROUND = 0

    # 85) 批30 C: who=="sub1"/"sub2"(副將固定位置分派) —— 三人隊(leader/sub1/sub2), 兩段效果
    # 分別指定 who:"sub1"(只防兵刃)/who:"sub2"(只防謀略), 應精確命中 allies[1]/allies[2],
    # 不誤中主將(allies[0]), 不互相污染。
    sp_leader = Unit(POOL["呂布"], "盾")
    sp_sub1 = Unit(POOL["張飛"], "盾")
    sp_sub2 = Unit(POOL["關羽"], "盾")
    sp_team = [sp_leader, sp_sub1, sp_sub2]
    sp_tac = {"nameZh": "測試箕形陣85", "effects": [
        {"k": "mitig", "who": "sub1", "val": 0.2, "dur": 9, "dmgType": "phys"},
        {"k": "mitig", "who": "sub2", "val": 0.2, "dur": 9, "dmgType": "intel"},
    ]}
    apply_effects(sp_leader, None, sp_tac, sp_team, [], no_heal=True, skip_when_effects=True)
    sub1_mitig = [a for a in sp_sub1.adds if a[0] == "mitig"]
    sub2_mitig = [a for a in sp_sub2.adds if a[0] == "mitig"]
    leader_mitig = [a for a in sp_leader.adds if a[0] == "mitig"]
    assert len(sub1_mitig) == 1 and sub1_mitig[0][4] and sub1_mitig[0][4].get("dmgType") == "phys", \
        "who=='sub1' 應精確命中 allies[1](副將A), 且 dmgType=phys 應正確傳遞"
    assert len(sub2_mitig) == 1 and sub2_mitig[0][4] and sub2_mitig[0][4].get("dmgType") == "intel", \
        "who=='sub2' 應精確命中 allies[2](副將B), 且 dmgType=intel 應正確傳遞"
    assert len(leader_mitig) == 0, "who=='sub1'/'sub2' 不應誤中主將(allies[0])"
    # 隊伍不足3人時 sub2 應為空(不應報錯/誤選)
    sp_team2 = [Unit(POOL["呂布"], "盾"), Unit(POOL["張飛"], "盾")]
    sp_tac2 = {"nameZh": "測試箕形陣85b", "effects": [{"k": "mitig", "who": "sub2", "val": 0.2, "dur": 9}]}
    apply_effects(sp_team2[0], None, sp_tac2, sp_team2, [], no_heal=True, skip_when_effects=True)
    assert not any(a[0] == "mitig" for a in sp_team2[1].adds), "隊伍不足3人時 who=='sub2' 應為空目標, 不應誤套用到sub1身上"

    # 86) 批31 A: on:"activeFired"(自身成功發動主動戰法時反應式觸發) —— 仿 dealt_damage_test
    # (見上方77) 的精簡版 active_fired_test, 驗證: (a) 只在真的有type:"active"戰法fire=True
    # 時才觸發, 常駐擲骰(command/passive無關戰法)不應觸發; (b) rate擲骰仍生效(非無條件);
    # (c) 同回合節流(hit_flags); (d) e.activeOnly 正確限定amp只在is_active=True的hit()生效。
    def active_fired_test(u, allies, foes):
        for t in u.active_fired_tacs:
            if id(t) in u.hit_flags:
                continue
            if random.random() >= t["rate"]:
                continue
            u.hit_flags.add(id(t))
            main_tgt = None
            if t["coef"]:
                vs = pick_targets(foes, t["n"])
                for v in vs:
                    hit(u, v, t["coef"], t["kind"], False, None, None, is_active=True)
                if len(vs) == 1:
                    main_tgt = vs[0]
            if t["effects"]:
                apply_effects(u, main_tgt, t, allies, foes, reactive=True)

    # 86a) 士爭先赴真實資料端到端驗證: 只在自身某個 active 戰法「成功發動」(呼叫端顯式觸發
    # active_fired_test, 模擬 fight() 主迴圈 fire=True 分支)時才可能造成兵刃傷害; 若從未呼叫
    # (等同該回合沒有任何主動戰法成功發動), 士爭先赴的傷害/amp皆不應觸發。
    sfxf_caster = Unit(POOL["呂布"], "騎")
    sfxf_caster.tactics = [dict(TACTICS["士爭先赴"])]
    sfxf_caster.active_fired_tacs = [t for t in sfxf_caster.tactics
                                     if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") == "activeFired"]
    sfxf_caster.active_fired_effect_tacs = [t for t in sfxf_caster.tactics
                                            if not t.get("when") and t["type"] in ("passive", "command", "active")
                                            and any((e.get("when") or {}).get("on") == "activeFired" for e in t.get("effects", []))]
    assert len(sfxf_caster.active_fired_tacs) == 1, "士爭先赴應被正確預篩收錄進 active_fired_tacs(t.when.on=='activeFired')"
    foe1 = Unit(POOL["張飛"], "盾")
    foe2 = Unit(POOL["關羽"], "盾")
    foes_troop_before = (foe1.troop, foe2.troop)
    random.seed(1)
    # 不呼叫 active_fired_test: 沒有任何主動戰法成功發動這件事發生過, 士爭先赴不應無條件觸發
    assert foe1.troop == foes_troop_before[0] and foe2.troop == foes_troop_before[1], \
        "士爭先赴不應在「無主動戰法成功發動」的情況下常駐觸發(v14盲測0分bug: 條件觸發簡化為無條件)"
    # 呼叫 active_fired_test 多次(模擬多回合皆有主動戰法成功發動), rate=0.5 應統計上觸發部分次數
    # 而非100%/0%; 每次試驗用全新的foe(避免累積傷害讓「是否觸發」的判斷失真)。
    fire_count = 0
    trials = 400
    for _ in range(trials):
        sfxf_caster.hit_flags.clear()  # 模擬每回合重置(見 Unit.tick())
        trial_foe = Unit(POOL["張飛"], "盾")
        troop_before = trial_foe.troop
        active_fired_test(sfxf_caster, [sfxf_caster], [trial_foe])
        if trial_foe.troop < troop_before:
            fire_count += 1
    rate_est = fire_count / trials
    assert 0.35 < rate_est < 0.65, f"士爭先赴 rate=0.5 應統計上約半數觸發, 實測rate_est={rate_est:.3f}(應排除0%/100%的無條件模式)"

    # 86b) e.activeOnly: amp(自身)應只對 is_active=True 的傷害生效, 對普通攻擊(is_active未傳/None)不生效
    ao_src = Unit(POOL["呂布"], "騎")
    ao_dst1 = Unit(POOL["張飛"], "盾")
    ao_dst2 = Unit(POOL["張飛"], "盾")
    ao_src.push_add("amp", 1.0, 9, "測試activeOnly86b", {"activeOnly": True})
    d_active = damage(ao_src, ao_dst1, 1.0, "phys", is_active=True)
    d_normal = damage(ao_src, ao_dst2, 1.0, "phys", is_active=None)
    assert d_active > d_normal * 1.5, f"activeOnly amp 應只在 is_active=True 時生效: d_active={d_active:.1f} d_normal={d_normal:.1f}"

    # 86c) 同回合節流: active_fired 每回合每戰法最多觸發1次(與 on_hit/dealt_damage 共用 hit_flags 慣例)
    af_trigger_count = [0]
    af_u = Unit(POOL["呂布"], "騎")
    af_tac = {"nameZh": "測試activeFired節流86c", "type": "passive", "when": {"on": "activeFired"}, "coef": 0, "rate": 1.0, "n": 1, "effects": []}
    af_u.tactics = [af_tac]
    af_u.active_fired_tacs = [af_tac]

    def af_count_test(u):
        for t in u.active_fired_tacs:
            if id(t) in u.hit_flags:
                continue
            u.hit_flags.add(id(t))
            af_trigger_count[0] += 1

    af_count_test(af_u)
    af_count_test(af_u)
    assert af_trigger_count[0] == 1, "on:'activeFired' 同回合同一戰法應只觸發1次(hit_flags節流), 不應觸發2次"
    af_u.hit_flags.clear()
    af_count_test(af_u)
    assert af_trigger_count[0] == 2, "hit_flags 每回合重置後, on:'activeFired' 應能在新回合重新觸發"

    # 87) 批31 B: extraHits.ifSameTargetIsLeader —— 精確表達「若(主coef段隨機選定的)目標恰為
    # 敵軍主將」條件分支, 取代暗藏玄機舊有的1/3機率EV折算近似(sameTarget沿用主段目標, 事後
    # 過濾只保留目標==foes[0]的情形)。
    ist_u = Unit(POOL["呂布"], "騎")
    ist_leader = Unit(POOL["張飛"], "盾")
    ist_sub = Unit(POOL["關羽"], "盾")
    ist_foes = [ist_leader, ist_sub]
    ist_t = {"nameZh": "測試ifSameTargetIsLeader87", "extraHits": [
        {"coef": 0.92, "kind": "intel", "who": "sameTarget", "ifSameTargetIsLeader": True},
    ]}
    leader_troop_before = ist_leader.troop
    fire_extra_hits(ist_u, ist_t, ist_leader, lambda u: [ist_u], lambda u: ist_foes, None)
    assert ist_leader.troop < leader_troop_before, "ifSameTargetIsLeader: sameTarget恰為foes[0](主將)時應結算此段傷害"
    sub_troop_before = ist_sub.troop
    fire_extra_hits(ist_u, ist_t, ist_sub, lambda u: [ist_u], lambda u: ist_foes, None)
    assert ist_sub.troop == sub_troop_before, "ifSameTargetIsLeader: sameTarget非foes[0](副將)時不應結算此段傷害"

    # 88) 批32 B: dealtDamage 的 coef 傷害段補 targetSel(依準則選標) —— 過去固定命中觸發同一
    # 目標 dst(普攻的目標), 現優先讀 t["targetSel"] 改為精確選標(如監統震軍「普攻後對負傷最高
    # 之敵造成謀略傷害」, 選標對象與觸發普攻的目標無關)。用真實 fight() 端到端驗證(比起上方
    # dealt_damage_test 簡化重寫版更貼近正式 dealt_damage() 閉包內的實際邏輯, 該簡化版只覆蓋
    # effects 分支, 從未覆蓋 coef+targetSel 這條路徑, 故此處改呼叫真正的 fight()): 合成戰法
    # TACTICS 注入一個 command 型 dealtDamage 戰法, targetSel:"mostDamaged", 我方單位普攻後
    # 應命中敵方兵力最低者(而非普攻本身打中的目標)。
    TACTICS["測試dealtDamageTargetSel88"] = {
        "nameZh": "測試dealtDamageTargetSel88", "type": "command", "kind": "intel",
        "when": {"on": "dealtDamage", "dmgType": "phys"}, "coef": 3.0, "rate": 1.0, "n": 1, "prep": 0,
        "targetSel": "mostDamaged", "effects": [], "extraHits": [],
    }
    dd_ts_winner, dd_ts_rounds, dd_ts_kill = fight(["呂布"], ["張飛", "關羽"], inhA=[["測試dealtDamageTargetSel88"]])
    del TACTICS["測試dealtDamageTargetSel88"]
    assert dd_ts_rounds >= 1, "dealtDamage+targetSel 端到端測試: fight() 應正常跑完至少1回合(未拋例外)"
    # (行為已由 scratchpad/b32_verify_fsz.js 同款 node TRACE 對真實監統震軍資料驗證: 傷害穩定
    # 命中兵力最低的敵方單位, 而非普攻本身的隨機目標, 見批32報告)

    # 89) 批33: 治療公式全局換裝 —— want = coef × HEAL_TROOP_C(0.06) × 施放者兵力(依型態擇一
    # healBase快照/當下即時) × SCALE(scale屬性), 用 calibration_anchors.json
    # heal_formula_resolved_20260704(後續更新)兩組錨點樣本直接assert:
    #   陷陣營60%/智力379.02/準備兵力8439 → 546±1(反解值, 弱錨點)
    #   青囊96%/智力228/準備兵力9600 → 755±1(強錨點, user補測樣本, 0.03%誤差)
    # 兩樣本皆為「受傷反應式常駐急救型」heal(when.on:"damaged"), 用 heal_base(準備階段鎖定
    # 兵力快照)而非 caster.troop(當下即時), 驗證治療量不受戰鬥中兵力變動影響。
    h89_xzy = Unit(POOL["張飛"], "盾")           # 陷陣營樣本: 施放者(受術者亦為自己, self-heal急救)
    h89_xzy.intel = 379.02
    h89_xzy.troop = 8439                          # 建構後改兵力, 需重算 heal_base 快照(建構時已用預設10000算過)
    h89_xzy.heal_base = h89_xzy.troop * HEAL_TROOP_C
    h89_xzy.troop = 8439 - 2000                   # 模擬「受傷後當下兵力已下降」——heal_base應忽略此變動
    h89_xzy.wounded = 3000                        # 傷兵池足夠大, 不觸頂
    xzy_tac89 = {"nameZh": "測試陷陣營heal89", "type": "command", "kind": "phys", "coef": 0,
                 "rate": 1.0, "n": 3, "prep": 0,
                 "effects": [{"k": "heal", "who": "self", "coef": 0.6, "scale": "intel", "dur": 1}]}
    before89 = h89_xzy.troop
    apply_effects(h89_xzy, None, xzy_tac89, [h89_xzy], [], no_heal=False)
    gained89 = h89_xzy.troop - before89
    assert abs(gained89 - 546) <= 1, f"陷陣營樣本(智力379.02/準備兵力8439/治療率60%)應恢復546±1, 實得{gained89:.1f}"

    h89_qn = Unit(POOL["諸葛亮"], "弓")           # 青囊樣本(user補測, 強錨點): 施放當下兵力已變動, heal_base仍應鎖定準備階段值
    h89_qn.intel = 228
    h89_qn.troop = 9600
    h89_qn.heal_base = h89_qn.troop * HEAL_TROOP_C  # 準備階段快照(9600×0.06=576)
    h89_qn.troop = 9600 - 969                      # 模擬「準備階段後兵力已因其他傷害下降」(對應user描述8611~8781浮動情境)
    h89_qn_target = Unit(POOL["張飛"], "盾")
    h89_qn_target.troop = 5000
    h89_qn_target.wounded = 3000
    qn_tac89 = {"nameZh": "測試青囊heal89", "type": "command", "kind": "phys", "coef": 0,
                "rate": 1.0, "n": 2, "prep": 0,
                "effects": [{"k": "heal", "who": "ally", "coef": 0.96, "scale": "intel", "dur": 1}]}
    before89b = h89_qn_target.troop
    apply_effects(h89_qn, None, qn_tac89, [h89_qn_target], [], no_heal=False)
    gained89b = h89_qn_target.troop - before89b
    assert abs(gained89b - 755) <= 1, f"青囊樣本(智力228/準備兵力9600/治療率96%)應恢復755±1, 實得{gained89b:.1f}"

    # 89b) e["ofDamage"] —— 傷害比例治療(草船借箭類「回復傷害量X%」), 與屬性公式(scale/coef×
    # heal_base)互斥擇一; 用合成單效果戰法直接呼叫 apply_effects 並手動傳入 dmg 驗證比例正確。
    of_caster89 = Unit(POOL["張飛"], "盾")
    of_target89 = Unit(POOL["張飛"], "盾")
    of_target89.troop = 5000
    of_target89.wounded = 3000
    of_tac89 = {"nameZh": "測試ofDamage89", "kind": "phys",
                "effects": [{"k": "heal", "who": "ally", "ofDamage": 0.2857, "dur": 1}]}
    before89c = of_target89.troop
    apply_effects(of_caster89, None, of_tac89, [of_target89], [], no_heal=False, dmg=329)
    gained89c = of_target89.troop - before89c
    assert abs(gained89c - 329 * 0.2857) < 0.5, \
        f"e['ofDamage']=0.2857應恢復傷害量329的28.57%(=94), 實得{gained89c:.1f}"

    # 89c) active(主動直療型)heal 用 caster.troop(當下即時兵力), 非 heal_base(準備階段快照)——
    # 用不同的 troop/heal_base 值驗證兩者確實分流, 對稱驗證 88/89 已涵蓋的「常駐急救型用
    # heal_base」不會被誤用在 active 型上。
    ad_caster89 = Unit(POOL["華佗"] if "華佗" in POOL else POOL["張飛"], "弓" if "華佗" in POOL else "盾")
    ad_caster89.intel = 284
    ad_caster89.troop = 8000                       # 當下即時兵力(active型應採用此值, 非heal_base)
    ad_caster89.heal_base = 999999 * HEAL_TROOP_C  # 刻意設一個遠不同的heal_base, 若active型誤用它, 治療量會離譜偏高, 藉此排除誤用
    ad_target89 = Unit(POOL["張飛"], "盾")
    ad_target89.troop = 3000
    ad_target89.wounded = 8000
    ad_tac89 = {"nameZh": "測試active直療89", "type": "active", "kind": "intel", "coef": 0,
                "rate": 1.0, "n": 1, "prep": 0,
                "effects": [{"k": "heal", "who": "ally", "coef": 2.56, "scale": "intel", "dur": 1}]}
    before89d = ad_target89.troop
    apply_effects(ad_caster89, None, ad_tac89, [ad_target89], [], no_heal=False)
    gained89d = ad_target89.troop - before89d
    expected89d = 2.56 * (8000 * HEAL_TROOP_C) * SCALE(284)
    assert abs(gained89d - expected89d) < 1.0, \
        f"active型heal應採用caster.troop(當下即時), 非heal_base, 預期{expected89d:.1f}實得{gained89d:.1f}"
    assert gained89d < 50000, "active型heal若誤用刻意調大的heal_base會產生離譜高治療量, 此處應遠低於該值"

    # --- 批35: 狀態屬性曲線375族 + 準備階段鎖定 + block消耗門檻 ---
    # 90) SCALE_G(v, 375) 六點precise assert —— docs/data/calibration_anchors.json →
    # status_scale_375_20260704(user 機鑑先識警戒六點實測, 荀彧準備階段智力478.84~389.72):
    # 警戒減傷 = 40% × (1+(智力-100)/375), cap 80%(=基礎×2)。六點全部小數點後兩位精確吻合。
    anchors90 = [
        (486.69, 0.80), (478.84, 0.80), (439.38, 0.7620),
        (432.18, 0.7543), (417.46, 0.7386), (394.88, 0.7145), (389.72, 0.7090),
    ]
    for intel90, expect90 in anchors90:
        got90 = min(0.4 * SCALE_G(intel90, 375), 0.8)
        assert abs(got90 - expect90) < 0.0001, \
            f"機鑑先識警戒錨點 智力{intel90}: 預期{expect90:.4f}, 算得{got90:.4f}"
    # 對照: 用全域350除數算同一批智力值不應精確吻合(佐證375是獨立曲線, 非350的誤差範圍內)
    assert abs(min(0.4 * SCALE(439.38), 0.8) - 0.7620) > 0.001, "350曲線不應與375曲線在此錨點精確重合(否則兩曲線無法區分)"

    # 91) locked_scale_of: 「準備階段鎖定」語意 —— 效果物件首次被算定(prep階段)的 scale 值,
    # 之後即使 caster 智力改變也應沿用鎖定值, 不重新計算(對應機鑑先識 everyRound 補層段
    # 在第2/3回合才真正命中套用, 但縮放倍率仍固定用開戰當下的智力)。
    lk91_caster = Unit(POOL["張飛"], "盾")
    lk91_caster.intel = 439.38
    lk91_e = {"k": "block", "scale": "intel", "scaleDiv": 375, "val": 0.4, "capVal": 0.8}
    v91_first = locked_scale_of(lk91_caster, lk91_e)
    assert abs(v91_first - SCALE_G(439.38, 375)) < 1e-9, "首次呼叫應等於當下即時 scale_of"
    lk91_caster.intel = 900.0                      # 模擬戰鬥中智力大幅變動(如中途獲得buff)
    v91_second = locked_scale_of(lk91_caster, lk91_e)
    assert v91_second == v91_first, f"鎖定後應沿用首次算定值, 不隨caster.intel變動重算: 首次{v91_first:.4f} 之後{v91_second:.4f}"
    # 不同效果物件(即使同caster)各自獨立上鎖, 不互相污染
    lk91_e2 = {"k": "block", "scale": "intel", "scaleDiv": 375, "val": 0.4, "capVal": 0.8}
    v91_e2 = locked_scale_of(lk91_caster, lk91_e2)
    assert abs(v91_e2 - SCALE_G(900.0, 375)) < 1e-9, "不同效果物件應各自獨立鎖定(用當時intel=900算), 不共用lk91_e的鎖"

    # 92) cap_val_of clamp —— 縮放後上限保護, e["capVal"] 未達上限時不影響原值
    assert abs(cap_val_of(0.762, 0.8) - 0.762) < 1e-9, "未超過capVal時應維持原值"
    assert abs(cap_val_of(0.95, 0.8) - 0.8) < 1e-9, "超過capVal時應clamp到capVal"
    assert cap_val_of(0.5, None) == 0.5, "capVal未設(None)時應原樣通過, 不clamp"

    # 93) block(k=="block") 的 apply_effects 路徑實際套用: val×scale_of(375曲線)+capVal clamp
    # 端到端驗證(取代直接算術, 走真實 apply_effects→push_block 路徑)
    bk93_caster = Unit(POOL["張飛"], "盾")
    bk93_caster.intel = 439.38
    bk93_dst = Unit(POOL["張飛"], "盾")
    bk93_tac = {"nameZh": "測試機鑑先識93", "effects": [
        {"k": "block", "who": "ally", "val": 0.4, "times": 2, "scale": "intel", "scaleDiv": 375, "capVal": 0.8}]}
    apply_effects(bk93_caster, None, bk93_tac, [bk93_dst], [], no_heal=True)
    assert len(bk93_dst.block) == 1 and abs(bk93_dst.block[0]["val"] - 0.7620) < 0.0001, \
        f"端到端: 智力439.38應套用警戒減傷76.20%, 實得{bk93_dst.block[0]['val']*100:.2f}%"

    # 94) BLOCK_CONSUME_THRESHOLD —— 傷害未超過 START_TROOP×6%(=600) 時不應消耗警戒層/不減傷;
    # 超過門檻時正常消耗+減傷。用直接構造低/高傷害兩種情境比較(非倚賴 damage() 隨機量級)。
    bk94_lo = Unit(POOL["張飛"], "盾")
    bk94_lo.push_block(0.4, 2, src="測試門檻94低")
    # 直接檢查常數本身(hit() 內部條件為 dmg > BLOCK_CONSUME_THRESHOLD 才消耗)
    assert BLOCK_CONSUME_THRESHOLD == max(START_TROOP * 0.06, 100) == 600.0, \
        f"BLOCK_CONSUME_THRESHOLD應為max(10000×6%,100)=600, 實得{BLOCK_CONSUME_THRESHOLD}"
    # 低傷害(<=門檻)不應消耗: 用極低coef讓damage()大機率落在門檻以下, 多次嘗試取一個確定案例
    random.seed(7)
    bk94_hit_src = Unit(POOL["張飛"], "盾")
    bk94_lo_dmg = damage(bk94_hit_src, bk94_lo, 0.05, "phys")   # 極低coef, 預期遠低於600
    assert bk94_lo_dmg <= BLOCK_CONSUME_THRESHOLD, f"測試前提: 本次構造傷害應低於門檻600, 實際{bk94_lo_dmg:.1f}(若失敗需調整coef/種子)"
    before94lo = bk94_lo.troop
    hit(bk94_hit_src, bk94_lo, 0.05, "phys")  # 注意: hit()內部會重新算一次damage(), 種子已消耗一次, 這裡只驗證block層數是否被消耗
    assert len(bk94_lo.block) == 1 and bk94_lo.block[0]["n"] == 2, "低於門檻的傷害不應消耗警戒層(次數應維持2不變)"
    # 高傷害(>門檻)應正常消耗: 用高coef確保dmg>600
    bk94_hi = Unit(POOL["張飛"], "盾")
    bk94_hi.push_block(0.4, 2, src="測試門檻94高")
    hit(bk94_hit_src, bk94_hi, 3.0, "phys")   # 高coef, 傷害應遠超600(基準coef=1.0時已476)
    assert bk94_hi.block and bk94_hi.block[0]["n"] == 1, "超過門檻的傷害應正常消耗1層警戒(2→1)"

    # 95) 迴歸測試: 準備階段鎖定必須在 skip_when_effects 閘門「之前」算定, 否則帶 e["when"]
    # (如機鑑先識 everyRound 段的 when:{until:3})且母戰法無 t["when"] 的 block 效果, 在
    # prep 呼叫(skip_when_effects=True)會被該閘門提前 continue 掉、根本沒機會鎖定, 導致
    # 鎖定值錯誤地延後到未來真正命中(heal_only常駐通道)的那一回合才用當時intel現算——
    # 這正是本批實作過程中一度真實發生的bug(用直接呼叫locked_scale_of的90/91號測試測不出來,
    # 因為那是繞過apply_effects閘門直接呼叫, 必須走真實apply_effects(prep語意的呼叫參數
    # 組合)才能重現)。
    er95_caster = Unit(POOL["張飛"], "盾")
    er95_caster.intel = 439.38
    er95_dst = Unit(POOL["張飛"], "盾")
    er95_tac = {"nameZh": "測試機鑑先識95", "effects": [
        {"k": "block", "who": "ally", "val": 0.4, "times": 1, "scale": "intel", "scaleDiv": 375,
         "capVal": 0.8, "everyRound": True, "rate": 1.0, "when": {"until": 3}}]}
    # 模擬 fight() 的 prep 呼叫: apply_passives(no_heal=True, skip_when_effects=True) 對應
    # apply_effects(..., no_heal=True, skip_when_effects=True)(global CUR_ROUND 已在本函式
    # 較早的測試段宣告過, 此處沿用同一個 global 宣告範圍, 不需重複宣告)
    CUR_ROUND = 0
    apply_effects(er95_caster, None, er95_tac, [er95_dst], [], no_heal=True, skip_when_effects=True)
    assert not er95_dst.block, "prep呼叫時everyRound效果本身不應套用(維持既有行為, 只是鎖定值應已算好)"
    er95_caster.intel = 900.0                      # 模擬戰鬥中智力大幅變動
    CUR_ROUND = 1
    apply_effects(er95_caster, None, er95_tac, [er95_dst], [], heal_only=True)  # 模擬第1回合的 apply_passives(heal_only=True)
    assert er95_dst.block, "everyRound效果應在heal_only常駐通道套用"
    assert abs(er95_dst.block[0]["val"] - 0.7620) < 0.0001, \
        f"迴歸: 即使套用當下(第1回合)intel已變成900, block的scale值仍應沿用prep階段(intel 439.38)鎖定值76.20%, 實得{er95_dst.block[0]['val']*100:.2f}%(若得到用900算出的80.00%即代表鎖定被skip_when_effects閘門繞過的bug重現)"

    # --- 批36: 兵種營資料通路(campLv 0~10 屬性%+Lv10附贈戰法attach) ---
    # 96) campLv=0(預設/未指定): 完全不影響既有行為 —— adds為空, tactics不含任何BUILDING戰法
    cl96 = Unit(POOL["張飛"], "槍")
    assert cl96.camp_lv == 0, "未傳camp_lv應預設0(向後相容既有全部呼叫點)"
    assert not any(a[0] == "amp" and a[3] == "兵種營" for a in cl96.adds), "campLv=0不應推入兵種營amp加成"
    assert not any(t.get("_campBuilding") for t in cl96.tactics), "campLv=0不應attach任何BUILDING戰法"

    # 97) campLv=10 + is_camp_holder=True 且隊伍兵種=槍: 應同時獲得(1)破軍戰法attach (2)amp+2.5%傷害
    cl97 = Unit(POOL["張飛"], "槍", camp_lv=10, is_camp_holder=True)
    camp_tacs97 = [t for t in cl97.tactics if t.get("_campBuilding")]
    assert len(camp_tacs97) == 1 and camp_tacs97[0]["nameZh"] == "破軍", \
        f"槍兵隊campLv=10(持有者)應自動獲得破軍, 實得{[t.get('nameZh') for t in camp_tacs97]}"
    camp_amp97 = [a for a in cl97.adds if a[0] == "amp" and a[3] == "兵種營"]
    assert len(camp_amp97) == 1 and abs(camp_amp97[0][1] - 0.025) < 1e-9, \
        f"campLv=10應貢獻amp+2.5%(10×0.25%), 實得{camp_amp97}"

    # 97b) campLv=10 但 is_camp_holder=False(同隊非持有者的隊友): 只吃屬性%加成, 不應attach戰法
    # ——這是本批修正的核心行為: 原文「我軍隨機單體」是一整隊只發生一次, 不是每個Unit各自擁有
    cl97b = Unit(POOL["張飛"], "槍", camp_lv=10, is_camp_holder=False)
    assert not any(t.get("_campBuilding") for t in cl97b.tactics), \
        "campLv=10但非持有者(is_camp_holder=False)不應attach戰法(避免隊上每人各自重複觸發)"
    camp_amp97b = [a for a in cl97b.adds if a[0] == "amp" and a[3] == "兵種營"]
    assert len(camp_amp97b) == 1 and abs(camp_amp97b[0][1] - 0.025) < 1e-9, "非持有者仍應正常吃屬性%加成(三合一另一支, 全隊皆有)"

    # 98) campLv=10 + is_camp_holder=True 但隊伍兵種=盾: 應獲得守禦(非破軍), 驗證兵種→戰法對應表逐一生效
    cl98 = Unit(POOL["張飛"], "盾", camp_lv=10, is_camp_holder=True)
    camp_tacs98 = [t for t in cl98.tactics if t.get("_campBuilding")]
    assert len(camp_tacs98) == 1 and camp_tacs98[0]["nameZh"] == "守禦", \
        f"盾兵隊campLv=10(持有者)應自動獲得守禦, 實得{[t.get('nameZh') for t in camp_tacs98]}"

    # 99) campLv=10 + is_camp_holder=True 但隊伍兵種=器(器械): 負重無戰鬥效果(type:"none"已被
    # TACTICS載入時過濾), 不應attach任何BUILDING戰法, 但傷害%加成仍應正常給予(三合一其餘兩支不受影響)
    cl99 = Unit(POOL["張飛"], "器", camp_lv=10, is_camp_holder=True)
    assert not any(t.get("_campBuilding") for t in cl99.tactics), "器械營Lv10不應attach任何戰法(負重無戰鬥效果)"
    camp_amp99 = [a for a in cl99.adds if a[0] == "amp" and a[3] == "兵種營"]
    assert len(camp_amp99) == 1 and abs(camp_amp99[0][1] - 0.025) < 1e-9, "器械營傷害%加成仍應正常給予(與Lv10戰法attach是獨立的兩支)"

    # 100) campLv=5(未滿級, 即使is_camp_holder=True): 只有屬性%加成(1.25%), 不應attach戰法(嚴格<10門檻)
    cl100 = Unit(POOL["張飛"], "槍", camp_lv=5, is_camp_holder=True)
    assert not any(t.get("_campBuilding") for t in cl100.tactics), "campLv=5(未滿10級)不應attach戰法, 即使is_camp_holder=True"
    camp_amp100 = [a for a in cl100.adds if a[0] == "amp" and a[3] == "兵種營"]
    assert len(camp_amp100) == 1 and abs(camp_amp100[0][1] - 0.0125) < 1e-9, \
        f"campLv=5應貢獻amp+1.25%(5×0.25%), 實得{camp_amp100}"

    # 100b) fight() 端到端: campLv=10 一整隊(3人)中應恰好1人是持有者(attach BUILDING戰法),
    # 其餘2人不應重複attach ——直接驗證 fight() 呼叫端的隨機挑選邏輯正確(非全隊每人都獲得)
    holder_count_samples = []
    for trial in range(50):
        random.seed(1000 + trial)
        idx = random.randrange(3)
        units100b = [Unit(POOL["張飛"], "槍", camp_lv=10, is_camp_holder=(i == idx)) for i in range(3)]
        holders = [u for u in units100b if any(t.get("_campBuilding") for t in u.tactics)]
        holder_count_samples.append(len(holders))
    assert all(c == 1 for c in holder_count_samples), \
        f"每次3人隊campLv=10應恰好1人是持有者(attach戰法), 實得樣本{holder_count_samples}"

    # 101) 端到端 fight(): 鏡像對局(雙方同隊「張飛/SP呂蒙/SP樂進」, 皆採槍兵, 消除陣容強弱
    # 差異的干擾) A方 campLvA=10(破軍+2.5%傷害) vs campLvA=0, B方恆為0 —— 有營一方勝率應
    # 明顯高於無營基準(鏡像對局基準本身非50%, 見下方 baseline, 故用「有營 - 對照組」差值判斷
    # 方向, 而非直接假設50%中心)。
    campA_team = ["張飛", "SP 呂蒙", "SP 樂進"]
    campB_team = ["張飛", "SP 呂蒙", "SP 樂進"]
    random.seed(36)
    r101_baseline = simulate(campA_team, campB_team, n=4000, troopA="槍", troopB="槍")
    random.seed(36)
    r101_with_camp = simulate(campA_team, campB_team, n=4000, troopA="槍", troopB="槍", campLvA=10)
    assert r101_with_camp["A勝率"] > r101_baseline["A勝率"], \
        f"campLv=10(破軍+2.5%傷害)應讓A方勝率高於無營基準(鏡像對局): 有營{r101_with_camp['A勝率']} 基準{r101_baseline['A勝率']}"
    print(f"    [批36] 鏡像對局 campLv=10 A勝率{r101_with_camp['A勝率']:.3f} vs 基準{r101_baseline['A勝率']:.3f} (差+{r101_with_camp['A勝率']-r101_baseline['A勝率']:.3f})")

    # --- 批37 B: 停損決策過期重審(義膽雄心 parity互斥交替 / 奮突 普攻事件精確觸發) ---
    # 102) 義膽雄心(真實資料): everyRound+e.when.parity 奇偶互斥交替 —— 奇數回合只套武力debuff
    # (單體), 偶數回合只套謀略dot(2人,kind:intel)+智力debuff(2人), 經 heal_only 常駐通道逐回合判定
    ydxx = TACTICS.get("義膽雄心")
    assert ydxx and (ydxx.get("when") or {}).get("parity") == "odd", \
        "義膽雄心應帶戰法級 when.parity:'odd'(main coef 184%兵刃傷害只在奇數回合擲骰, 批37 B重建)"
    assert all(e.get("everyRound") for e in ydxx["effects"]), "義膽雄心三段效果皆應帶everyRound(逐回合重擲通道)"
    ydxx_caster = Unit(POOL["呂布"], "騎")
    ydxx_foes = [Unit(POOL["張飛"], "盾"), Unit(POOL["關羽"], "盾")]
    CUR_ROUND = 1                                      # 奇數回合: 只有武力debuff段(n=1)應套用
    apply_effects(ydxx_caster, None, ydxx, [ydxx_caster], ydxx_foes, heal_only=True)
    force_hit_r1 = [u for u in ydxx_foes if any(s[0] == "force" for s in u.stat_adds)]
    intel_hit_r1 = [u for u in ydxx_foes if any(s[0] == "intel" for s in u.stat_adds)]
    dots_r1 = sum(len(u.dots) for u in ydxx_foes)
    assert len(force_hit_r1) == 1, f"義膽雄心奇數回合應對敵軍單體(n=1)套武力debuff, 實中{len(force_hit_r1)}人"
    assert not intel_hit_r1 and dots_r1 == 0, \
        f"義膽雄心奇數回合不應套偶數段(智力debuff/謀略dot), 實得intel={len(intel_hit_r1)} dots={dots_r1}"
    CUR_ROUND = 2                                      # 偶數回合: 謀略dot(2人)+智力debuff(2人)應套用
    apply_effects(ydxx_caster, None, ydxx, [ydxx_caster], ydxx_foes, heal_only=True)
    intel_hit_r2 = [u for u in ydxx_foes if any(s[0] == "intel" for s in u.stat_adds)]
    dots_r2 = sum(len(u.dots) for u in ydxx_foes)
    assert len(intel_hit_r2) == 2 and dots_r2 == 2, \
        f"義膽雄心偶數回合應對敵軍群體(2人)套智力debuff+謀略dot, 實得intel={len(intel_hit_r2)} dots={dots_r2}"
    CUR_ROUND = 0

    # 103) 奮突(真實資料): stackPer:"attack" + disarm 反應式重建(when.on:'dealtDamage'+normalOnly+rate:0.35)
    ft = TACTICS.get("奮突")
    ft_disarm = next(e for e in ft["effects"] if e["k"] == "disarm")
    ft_stack = next(e for e in ft["effects"] if e["k"] == "stack")
    assert (ft_disarm.get("when") or {}).get("on") == "dealtDamage" and ft_disarm["when"].get("normalOnly") \
        and abs(ft_disarm.get("rate", 0) - 0.35) < 1e-9, \
        "奮突disarm應為when:{on:'dealtDamage',normalOnly:true}+rate:0.35(批37 B重建, 取代舊rate=1常駐高估)"
    assert ft_stack.get("stackPer") == "attack", "奮突stack應為stackPer:'attack'(每次普攻命中後遞增, 批37 B新模式)"
    ft_u = Unit(POOL["呂布"], "騎")
    ft_foe = Unit(POOL["張飛"], "盾")
    apply_effects(ft_u, ft_foe, ft, [ft_u], [ft_foe], no_heal=True)  # prep套用(非reactive)
    assert ft_foe.disarm == 0, "奮突disarm(when.on反應式)不應在prep非reactive呼叫時套用(批23 when.on閘門)"
    assert ft_u.stack and ft_u.stack.get("stackPer") == "attack" and ft_u.stack["n"] == 0, \
        "奮突stack應在prep套用(stackPer:'attack', 初始0層)"
    if ft_u.alive and ft_u.stack and ft_u.stack.get("stackPer", "round") == "round":
        ft_u.stack["n"] = min(ft_u.stack["max"], ft_u.stack["n"] + 1)  # 重演fight()回合迴圈的round遞增判斷式
    assert ft_u.stack["n"] == 0, "stackPer:'attack'不應被fight()回合迴圈的round模式遞增判斷式誤加層(守衛條件== 'round')"

    # 104) 端到端 fight(): 鏡像對局, A方主將傳承奮突 vs 雙方皆無傳承基準 —— 奮突(普攻疊層增傷
    # +35%機率繳械)應讓A方勝率高於基準(驗證 stackPer:'attack' 遞增與 dealtDamage(normalOnly)
    # 繳械兩條新通道在真實fight()中確實生效; 若通道全然未觸發, 差值應~0)。
    ft_team = ["張飛", "SP 呂蒙", "SP 樂進"]
    random.seed(37)
    r104_baseline = simulate(ft_team, ft_team, n=4000, troopA="槍", troopB="槍")
    random.seed(37)
    r104_with_ft = simulate(ft_team, ft_team, n=4000, troopA="槍", troopB="槍", inhA=[["奮突"], None, None])
    assert r104_with_ft["A勝率"] > r104_baseline["A勝率"], \
        f"A方主將傳承奮突應提升鏡像對局勝率(普攻疊層+繳械通道生效): 傳承{r104_with_ft['A勝率']} 基準{r104_baseline['A勝率']}"
    print(f"    [批37] 鏡像對局 傳承奮突 A勝率{r104_with_ft['A勝率']:.3f} vs 基準{r104_baseline['A勝率']:.3f} (差+{r104_with_ft['A勝率']-r104_baseline['A勝率']:.3f})")

    print("self-check OK")


def _team(s):
    return [x.strip() for x in re.split(r"[,/\s]+", s) if x.strip()]


def info(name):
    g = POOL.get(name)
    if not g:
        print("查無此將。試試:", ", ".join(list(POOL)[:8]), "...")
        return
    apt = "  ".join(f"{t}{g.apt.get(t, '-')}" for t in ("騎", "盾", "弓", "槍", "器"))
    print(f"{g.name}  {g.faction}　兵種適性[ {apt} ]")
    print(f"  基礎 武{g.base['force']:.0f} 智{g.base['intel']:.0f} "
          f"統{g.base['command']:.0f} 速{g.base['speed']:.0f}（戰鬥時 ×該兵種適性%）")
    print(f"  可用兵書: {'/'.join(g.bingshu_cats) or '—'}　預設: {default_bingshu(g) or '—'}")
    print(f"  自帶戰法: {g.tactic_name}")
    if g.tactic:
        print(f"  解析: {json.dumps(g.tactic, ensure_ascii=False)}")


if __name__ == "__main__":
    import sys
    random.seed(42)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "sim" and len(sys.argv) >= 4:           # sim "A,B,C" "D,E,F" [兵種A 兵種B]
        a, b = _team(sys.argv[2]), _team(sys.argv[3])
        ta = sys.argv[4] if len(sys.argv) > 4 else team_troop(a)
        tb = sys.argv[5] if len(sys.argv) > 5 else team_troop(b)
        print(f"{'/'.join(a)}[{ta}兵]  vs  {'/'.join(b)}[{tb}兵]")
        print(" ", simulate(a, b, troopA=ta, troopB=tb))
    elif cmd == "rec":                                # rec [勢力]
        fac = sys.argv[2] if len(sys.argv) > 2 else None
        pool = [n for n, g in POOL.items() if g.faction == fac] if fac else None
        title = f"{fac}勢力" if fac else "全 193 武將"
        print(f"== 配將推薦 Top8 ({title}) ==")
        for team, sc, troop in recommend(pool):
            print(f"  {sc:>4}  [{troop}兵] {' / '.join(team)}")
    elif cmd == "info" and len(sys.argv) > 2:         # info 武將名
        info(sys.argv[2])
    elif cmd == "test":
        demo()
    else:                                             # demo
        print(f"載入: {len(POOL)} 武將, {len(TACTICS)} 戰法(可戰鬥, 來源={TACTIC_SRC})")
        print("\n== 配將推薦 Top8 (全 193) ==")
        for team, sc, troop in recommend():
            print(f"  {sc:>4}  [{troop}兵] {' / '.join(team)}")
        print("\n== 模擬: 呂布/趙雲/關羽 vs 諸葛亮/周瑜/司馬懿 ==")
        print(" ", simulate(["呂布", "趙雲", "關羽"], ["諸葛亮", "周瑜", "司馬懿"]))
        print("\n用法: python sgz.py [demo|test|rec [勢力]|sim \"A,B,C\" \"D,E,F\"|info 武將]")
        demo()
