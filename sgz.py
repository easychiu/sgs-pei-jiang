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

COUNTER = {"騎": "盾", "盾": "弓", "弓": "槍", "槍": "騎"}  # 騎>盾>弓>槍>騎; 器全被克
APT_PCT = {"S": 1.20, "A": 1.00, "B": 0.85, "C": 0.70, "D": 0.55, None: 0.85}
APT_RANK = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0, None: -1}
SCALE_CLAMP = 1.5                                    # amp/mitig 縮放後上限保護: |val| <= 1.5


def SCALE(v):
    """「受X影響」屬性縮放旋鈕。輸入為戰鬥內即時素質 caster.eff(stat)(已含城建/陣營/適性/
    加點/賽季/戰鬥中buff, 典型值 250~400, 而非卡面裸值)。公式取社群拆解(巴哈姆特高等陣容
    戰法論/NGA數據貼): 屬性100=面板基準值(SCALE=1.0), 每+350點效果翻倍(v=450時SCALE=2.0)。
    仍是可調校準旋鈕, 之後有更多實測數據可再調整斜率/錨點。"""
    return max(0.0, 1 + (v - 100) / 350)


def scale_of(caster, scale):
    if not scale:
        return 1.0
    return SCALE(caster.charm) if scale == "charm" else SCALE(caster.eff(scale))


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
        TACTICS = {o["nameZh"]: o for o in json.load(f) if o.get("type") != "none"}
    TACTIC_SRC = "LLM 解析"
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
class Unit:
    def __init__(self, g, ttype, bingshu=None, equip=None, add=None, inherit=None, season=None):
        self.g, self.ttype, self.troop, self.stun = g, ttype, START_TROOP, 0
        self.silence = 0                              # 計窮: 無法發動主動戰法
        self.disarm = 0                                # 繳械: 無法普通攻擊(含連擊/突擊)
        self.insight = 0                               # 洞察: 免疫 stun/silence/disarm, 施加時同時解除
        self.first = 0                                 # 先攻: 剩餘回合數, 排序時優先於速度
        # 自帶 + 傳承; 自帶戰法(g.tactic)淺拷貝附加 native:True 旗標(供 rateup/chargeup 的 nativeOnly
        # 修飾判斷「這是不是自帶戰法」, 如太平道法只加成張角自帶的五雷轟頂)。淺拷貝而非直接改
        # TACTICS 共享物件, 避免多個武將共用同一戰法物件時互相污染(如兩人都自帶白眉)。
        self.tactics = ([dict(g.tactic, native=True)] if g.tactic else []) + \
            [TACTICS[nm] for nm in (inherit or []) if nm in TACTICS]  # 自帶 + 傳承戰法
        _bn = bingshu if isinstance(bingshu, (list, tuple)) else ([bingshu] if bingshu else [])
        self.bs = [e for nm in _bn for e in BINGSHU.get(nm, {}).get("effects", [])]  # 兵書(主+副)合併效果
        _eq = equip if isinstance(equip, (list, tuple)) else ([equip] if equip else [])
        # 同名特技(跨type, 如四欄皆有的"無畏")遊戲規則只生效一件: 依基底名稱去重, 先出現者為準
        _eq_seen = set()
        _eq_objs = []
        for nm in _eq:
            e = EQUIPS.get(nm)
            if e and e["name"] not in _eq_seen:
                _eq_seen.add(e["name"])
                _eq_objs.append(e)
        self.eq = [eff for e in _eq_objs for eff in e.get("effects", [])]  # 裝備(4欄)合併效果(已去重)
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
            self.adds.append(["amp", a["amp"], 9999, None])
        if a.get("mitig"):
            self.adds.append(["mitig", a["mitig"], 9999, None])
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
        self.taunt_by = None                          # 嘲諷: 被嘲諷時強制普攻/單體戰法指向 taunt_by
        self.taunt_dur = 0                             # 嘲諷剩餘回合
        self.shield = None                            # 護盾: {amt, dur} 吸收固定量傷害, 先於兵力扣減
        self.dodge_prob = 0.0                          # 規避機率
        self.dodge_dur = 0                             # 規避剩餘回合
        self.surehit_dur = 0                           # 必中: 無視對方 dodge, 剩餘回合
        self.when_fired = set()                        # 條件觸發(when.rounds/from/until) 已套用效果的戰法(一次性, 用 id() 去重)
        self.hit_flags = set()                         # 反應式觸發(when.on) 本回合已觸發的戰法, 每回合重置(防無限鏈)
        self.on_hit_tacs = [t for t in self.tactics    # 預篩: 絕大多數單位為空, hit 熱路徑 O(0)
                            if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on")]

    @property
    def alive(self):
        return self.troop > 0

    def eff(self, stat):
        if self.swap and stat in ("force", "intel"):  # 武智互換
            stat = "intel" if stat == "force" else "force"
        v = getattr(self, stat)
        for s, add, *_ in self.stat_adds:             # 裝備平加(獨立階段, 在陣營/兵種營後、戰法乘算前)
            if s == stat or s == "all":
                v += add
        for s, m, *_ in self.mods:
            if s == stat or s == "all":               # stat="all" 套全屬性
                v *= m
        return v

    def addbonus(self, kind):
        return sum(v for k, v, *_ in self.adds if k == kind)

    def addbonus_for(self, kind, t):
        """rateup/chargeup 專用: 依戰法 t 的 prep/native 屬性, 只加總「修飾旗標吻合」的 adds 項。
        adds[4] = flags({"prepOnly":.., "nativeOnly":..}|None, 見 push_add)。無旗標的加成一律計入
        (如虎豹騎的 chargeup 沒有 prepOnly/nativeOnly 限制)。"""
        s = 0.0
        for a in self.adds:
            if a[0] != kind:
                continue
            flags = a[4] if len(a) > 4 else None
            if flags:
                if flags.get("prepOnly") and not t.get("prep"):
                    continue
                if flags.get("nativeOnly") and not t.get("native"):
                    continue
            s += a[1]
        return s

    def amp(self):                                    # 總增傷 = 一般+疊加層+衰減
        a = self.addbonus("amp")
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

    def push_mod(self, stat, mult, dur, src=None):
        if src:
            self.mods = [m for m in self.mods if not (m[0] == stat and m[3] == src)]
        self.mods.append([stat, mult, dur, src])

    def push_stat_add(self, stat, add, dur, src=None):  # 屬性平加(裝備 stat.add): 同 push_mod 慣例, 同來源刷新不疊
        if src:
            self.stat_adds = [a for a in self.stat_adds if not (a[0] == stat and a[3] == src)]
        self.stat_adds.append([stat, add, dur, src])

    def tick(self):
        for dmg, _ in self.dots:                      # 持續傷害結算
            self.troop -= dmg
        self.dots = [[d, l - 1] for d, l in self.dots if l - 1 > 0]
        self.mods = [[s, m, l - 1, src] for s, m, l, src in self.mods if l - 1 > 0]
        self.adds = [[k, v, l - 1, src, flags] for k, v, l, src, flags in self.adds if l - 1 > 0]
        self.stat_adds = [[s, ad, l - 1, src] for s, ad, l, src in self.stat_adds if l - 1 > 0]  # 裝備平加到期移除(如 疾馳 speed+25 dur:2)
        self.stun = max(0, self.stun - 1)
        self.silence = max(0, self.silence - 1)
        self.disarm = max(0, self.disarm - 1)
        self.insight = max(0, self.insight - 1)
        self.first = max(0, self.first - 1)            # 先攻: 逐回合遞減(dur=N 覆蓋前 N 回合, 如「戰鬥前3回合」)
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
        self.hit_flags.clear()                         # 受擊觸發(when.on) 每回合各戰法重置一次觸發額度


# 傷害公式旋鈕(批3 重塑): 社群拆解(知乎菜頭50級傷害模型 + B站櫻謀詭計錨點), 用實測錨點反解常數。
# 錨點(兵10000/coef1.0/士氣100/無增減傷, morale_mult(100)=1.0 已併入取樣, 取隨機帶中值1.0):
#   錨1 屬性差0   → 實測 ≈476 傷害 ⇒ DMG_A = 476/sqrt(10000) = 4.76
#   錨2 屬性差200 → 實測 ≈764 傷害 ⇒ DMG_B = (764-476)/200 = 1.44
#   錨3 屬性差大負值(保底) → 實測 ≈90  傷害 ⇒ DMG_FLOOR = 90/sqrt(10000) = 0.9
# 之後有更多實測數據(不同兵力/等級)可再校準, 目前僅50級單一等級係數樣本, 折入常數中。
DMG_A = 4.76
DMG_B = 1.44
DMG_FLOOR = 0.9


def damage(src, dst, coef, kind, src_troop=None):
    troop = src.troop if src_troop is None else src_troop  # 結算傷害用施毒當下定格兵力
    atk = src.eff("intel") if kind == "intel" else src.eff("force")
    deff = dst.eff("intel") if kind == "intel" else dst.eff("command")
    troop_sqrt = math.sqrt(max(0, troop))
    base = max(DMG_A * troop_sqrt + DMG_B * (atk - deff), DMG_FLOOR * troop_sqrt) * coef
    base *= counter_mult(src.ttype, dst.ttype)        # 克制: 隊伍兵種 vs 隊伍兵種
    base *= morale_mult(MORALE)
    base *= max(0.0, 1 + src.amp())                   # 增傷(疊加/衰減/敵方減益)
    mit = dst.addbonus("mitig") * (1 - min(1.0, src.addbonus("pierce")))  # 看破: 無視部分減傷
    base *= max(0.1, 1 - mit)
    base *= random.uniform(0.96, 1.04)
    return max(0, base)


def hit(src, dst, coef, kind, is_normal=False, on_event=None):  # 造成傷害(含規避/護盾/代承轉移/反擊), 累積結算層數
    if not src.surehit_dur and dst.dodge_dur and random.random() < dst.dodge_prob:  # 規避: 完全迴避一次傷害(必中無視)
        if on_event:
            on_event(dst, src, is_normal)
        return
    dmg = damage(src, dst, coef, kind)
    if dst.shield and dst.shield["amt"] > 0:          # 護盾: 先於兵力扣減吸收傷害
        absorb = min(dst.shield["amt"], dmg)
        dst.shield["amt"] -= absorb
        dmg -= absorb
        if dst.shield["amt"] <= 0:
            dst.shield = None
    g = dst.guardian
    if g and g.alive and g is not dst and not (dst.guard_normal_only and not is_normal):  # normalOnly 援護: 戰法傷害(is_normal=False)不轉移
        g.troop -= dmg * dst.guard_share
        dst.troop -= dmg * (1 - dst.guard_share)
    else:
        dst.troop -= dmg
    if dst.settle:
        dst.settle["layers"] = min(dst.settle["max"], dst.settle["layers"] + 1)
    if on_event:
        on_event(dst, src, is_normal)
    c = dst.counter                                   # 反擊: 直接還擊 src(不經 hit, 不遞迴)
    if c and dst.alive and src.alive and random.random() < c.get("prob", 1.0):
        src.troop -= damage(dst, src, c["coef"], c.get("kind", "phys"))


def extra_count(ex):                                  # 連擊/追擊次數: 整數部分必定, 小數部分機率
    return int(ex) + (1 if random.random() < (ex - int(ex)) else 0)


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


def apply_effects(caster, tgt, t, allies, enemies, heal_only=False, no_heal=False):
    src = t.get("nameZh")                              # 效果來源標籤: 戰法名(兵書/裝備/緣分無 nameZh → None, 不去重)
    for e in t["effects"]:
        k = e["k"]
        if heal_only and k != "heal":                 # 指揮/被動逐回合只跑治療
            continue
        if k == "heal":                               # 治療: 補我方最殘一人(指揮/被動每回合觸發)
            if no_heal:
                continue
            hurt = min((a for a in allies if a.alive),
                       key=lambda a: a.troop, default=None)
            if hurt:                                  # ponytail: 治療量粗估, 上限不超過初始兵力
                hcoef = e.get("coef", 0.8) * (scale_of(caster, e["scale"]) if e.get("scale") else 1.0)
                hurt.troop = min(START_TROOP, hurt.troop + hcoef * caster.troop * 0.10)
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
        ctrl_k = k in ("stun", "silence", "disarm", "taunt")  # 控制/嘲諷類: 按戰法 n/nMax 選目標數(insight 不擋嘲諷, 只擋 stun/silence/disarm)
        if who == "self":
            dests = [caster] if caster.alive else []
        elif who == "enemy":
            if ctrl_k:                                # 群體控制隨機挑不重複目標; 單體優先鎖定 tgt
                n = t.get("n") or 1
                cnt = n + random.randint(0, t["nMax"] - n) if t.get("nMax") else n
                if cnt <= 1:
                    dests = [tgt] if tgt and tgt.alive else pick_targets(enemies, 1)
                else:
                    dests = pick_targets(enemies, cnt)
            else:
                dests = [x for x in enemies if x.alive]
        else:
            dests = [a for a in allies if a.alive]
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

        for u in dests:
            if k == "amp":
                v = sv_val(e["val"])
                if who == "enemy" and v > 0:          # 修正: 敵方正amp(誤幫敵增傷)→ 視為敵方易傷
                    u.push_add("mitig", -v, e["dur"], src)
                else:
                    u.push_add("amp", v, e["dur"], src)
            elif k == "mitig":
                u.push_add("mitig", sv_val(e["val"]), e["dur"], src)
            elif k == "stun":
                if not u.insight:
                    u.stun = max(u.stun, e["dur"] + 1)
            elif k == "silence":
                if not u.insight:
                    u.silence = max(u.silence, e["dur"] + 1)
            elif k == "disarm":
                if not u.insight:
                    u.disarm = max(u.disarm, e["dur"] + 1)
            elif k == "insight":                      # 洞察: 免疫控制, 施加時同時解除既有控制
                u.insight = max(u.insight, e.get("dur", 1) + 1)
                u.stun = u.silence = u.disarm = 0
            elif k == "first":                        # 先攻: 本回合旗標, 優先於速度排序
                u.first = max(u.first, e.get("dur", 1))
            elif k == "stat":                         # 裝備平加(add)與乘算(mult)擇一; add 為戰報所示「裝備獨立平加階段」
                if e.get("add") is not None:
                    u.push_stat_add(e["stat"], sv_add(e["add"]), e["dur"], src)
                else:
                    u.push_mod(e["stat"], sv_mult(e.get("mult", 1.0)), e["dur"], src)
            elif k == "dot":                          # 持續傷害: 套用時定格每回合傷害
                u.dots.append([damage(caster, u, e.get("coef", 0.5),
                                      t.get("kind", "intel")), e["dur"]])
            elif k == "extra":                        # 連擊/追擊: 普攻後追加普攻的預算
                u.push_add("extra", e["val"], e["dur"], src)
            elif k == "stack":                        # 疊加增益: 每回合+1層, 每層加 per 增傷
                u.stack = {"per": e.get("per", 0.1), "max": e.get("max", 5), "n": 0}
            elif k == "decay":                        # 衰減增益: 開場 v0 增傷, rounds 內線性歸零
                u.decay = {"v0": e.get("v0", 0.5), "left": e.get("rounds", 8),
                           "total": e.get("rounds", 8)}
            elif k == "swap":                         # 武智互換
                u.swap = max(u.swap, e.get("dur", 1) + 1)
            elif k == "pierce":                       # 看破: 無視目標 val 比例的減傷
                u.push_add("pierce", e["val"], e["dur"], src)
            elif k == "counter":                      # 反擊: 受擊時還擊
                u.counter = {"coef": e.get("coef", 1.0), "kind": e.get("kind", "phys"),
                             "prob": e.get("prob", 1.0)}
            elif k == "taunt":                         # 嘲諷: 中招者普攻/單體戰法強制指向施放者
                u.taunt_by = caster
                u.taunt_dur = max(u.taunt_dur, e.get("dur", 1) + 1)
            elif k == "shield":                        # 護盾: 固定量+按施放者兵力係數, 吸滿或到期為止
                amt = e.get("amt", 0) + (e.get("pct", 0) * caster.troop if e.get("pct") else 0)
                prev = u.shield["amt"] if u.shield else 0
                u.shield = {"amt": prev + amt, "dur": e.get("dur", 99) + 1}  # +1 補償: tick 施加當回合末即扣1, 與 taunt/dodge/surehit 慣例一致
            elif k == "dodge":                         # 規避: 機率完全迴避一次傷害
                u.dodge_prob = e.get("prob", 0.2)
                u.dodge_dur = max(u.dodge_dur, e.get("dur", 1) + 1)
            elif k == "surehit":                       # 必中: 無視對方 dodge
                u.surehit_dur = max(u.surehit_dur, e.get("dur", 1) + 1)
            elif k == "rateup":                        # 提高(自身或對象)主動戰法發動機率
                # scale: 施放當下(caster 戰鬥內即時素質)用 RATE_SCALE_C(獨立於全域 SCALE) 縮放實際
                # 加成(批7: 太平道法「受智力影響」, 見 docs/data/calibration_anchors.json → rate_scale)。
                # prepOnly/nativeOnly 修飾旗標存進 adds[4], 由 addbonus_for() 在主動擲骰處依戰法屬性篩選加總。
                rv = e["val"] * rate_scale_of(caster, e["scale"]) if e.get("scale") else e["val"]
                rflags = {"prepOnly": bool(e.get("prepOnly")), "nativeOnly": bool(e.get("nativeOnly"))} \
                    if (e.get("prepOnly") or e.get("nativeOnly")) else None
                # 同一戰法(如太平道法)可能有多條 rateup(一般 + prepOnly 額外), src 相同的話
                # push_add 的「同kind+同src刷新」去重會把前一條蓋掉; 用 flags 組出不同的 dedup
                # key 尾碼區分, 讓語意不同的兩條並存, 但同語意(同flags組合)的仍正常刷新不疊加。
                r_src = (src + ":" + "".join(k2 for k2 in ("prepOnly", "nativeOnly") if rflags.get(k2))) \
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


def fight(teamA, teamB, troopA=None, troopB=None, bsA=None, bsB=None, eqA=None, eqB=None,
          addA=None, addB=None, inhA=None, inhB=None, scenario=None):
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
    A = [Unit(POOL[n], troopA, bsA[i], eqA[i], addA[i], inhA[i], season_mods(POOL[n], i, teamA, scenario))
         for i, n in enumerate(teamA)]
    B = [Unit(POOL[n], troopB, bsB[i], eqB[i], addB[i], inhB[i], season_mods(POOL[n], i, teamB, scenario))
         for i, n in enumerate(teamB)]
    setA = set(map(id, A))
    allies_of = lambda u: A if id(u) in setA else B
    foes_of = lambda u: B if id(u) in setA else A
    bonds = {id(A[0]) if A else 0: active_bonds(teamA), id(B[0]) if B else 1: active_bonds(teamB)}

    CAT_ORDER = ("PASSIVE", "FORMATION", "TROOP", "COMMAND")  # 準備階段嚴格順序: 被動→陣法→兵種→指揮(與 engine.js parity)
    cat_of = lambda t: t.get("cat") if t.get("cat") in CAT_ORDER else "COMMAND"

    def apply_passives(no_heal=False, heal_only=False):  # 被動/陣法/兵種/指揮(依序) + 兵書/裝備/緣分
        for cat in CAT_ORDER:
            for u in A + B:
                if not u.alive:
                    continue
                for t in u.tactics:                   # 同將多個同類: 戰法格順序(陣列順序)決定先後
                    if t["type"] in ("passive", "command") and cat_of(t) == cat:
                        if t.get("when") and not heal_only:  # 條件觸發(when): 不在準備階段套用, 改由回合迴圈在符合回合時套用
                            continue
                        apply_effects(u, None, t, allies_of(u), foes_of(u), no_heal=no_heal, heal_only=heal_only)
        for u in A + B:
            if not u.alive:
                continue
            for eff in (u.bs, u.eq):
                if eff:
                    apply_effects(u, None, {"effects": eff, "kind": "phys"}, allies_of(u), foes_of(u),
                                  no_heal=no_heal, heal_only=heal_only)
        for team in (A, B):                           # 緣分: 隊伍級
            if team:
                for bd in bonds[id(team[0])]:
                    apply_effects(team[0], None, {"effects": bd["effects"], "kind": "phys"},
                                  team, foes_of(team[0]), no_heal=no_heal, heal_only=heal_only)

    def on_hit(dst, src, is_normal):                  # 反應式觸發(when.on): 被普攻(attacked)/受任意傷害(damaged) 時掛到 hit() 事件點
        if not dst.alive or not dst.on_hit_tacs:
            return
        for t in dst.on_hit_tacs:
            if t["when"]["on"] == "attacked" and not is_normal:  # attacked: 限普通攻擊觸發; damaged: 任意傷害都觸發
                continue
            if id(t) in dst.hit_flags:                # 同回合每單位每戰法最多觸發1次(防無限鏈)
                continue
            if random.random() >= t["rate"]:
                continue
            dst.hit_flags.add(id(t))
            if t["coef"]:
                hit(dst, src, t["coef"], t["kind"], False, on_hit)
            if t["effects"]:
                apply_effects(dst, src, t, allies_of(dst), foes_of(dst))

    apply_passives(no_heal=True)                      # 開戰套持久效果(治療除外)

    for rnd in range(1, ROUNDS + 1):
        for u in A + B:                               # 疊加增益: 每回合 +1 層
            if u.alive and u.stack:
                u.stack["n"] = min(u.stack["max"], u.stack["n"] + 1)
        apply_passives(heal_only=True)                # 逐回合治療(含兵書/裝備/緣分)

        for u in A + B:                               # 條件觸發(when.rounds/from/until): 窗口首次開啟時套用一次非傷害效果(dot/amp/…); when.on 為反應式, 不走此處
            if not u.alive:
                continue
            for t in u.tactics:
                if t["type"] in ("passive", "command") and t.get("when") and not t["when"].get("on") \
                        and round_ok(t, rnd) and id(t) not in u.when_fired:
                    u.when_fired.add(id(t))
                    apply_effects(u, None, t, allies_of(u), foes_of(u))

        # 行動順序: 先攻(first)優先於速度; 同速平手隨機(先打亂再穩定排序, 修 A 隊固定先手偏差)
        _pool = [x for x in A + B if x.alive]
        random.shuffle(_pool)
        for u in sorted(_pool, key=lambda x: (x.first, x.eff("speed")), reverse=True):
            if not u.alive or u.stun:
                continue
            if pick_target(foes_of(u)) is None:
                break
            if not u.silence:                             # 計窮: 跳過主動/指揮/被動(不影響普攻)
                for t in u.tactics:                       # 自帶 + 傳承: 各自獨立附加發動(不占普攻)
                    fire = False
                    if t["type"] == "active" and (t["coef"] or t["effects"]) \
                            and not (t["prep"] and rnd == 1):
                        fire = random.random() < t["rate"] + u.addbonus_for("rateup", t)  # rateup: 提高自身主動戰法發動機率(如白眉); addbonus_for 依 t["prep"]/t["native"] 篩選 prepOnly/nativeOnly 修飾的加成(批7: 太平道法)
                    elif t["type"] in ("command", "passive") and t["coef"] \
                            and not (t.get("when") and t["when"].get("on")) and round_ok(t, rnd):
                        fire = random.random() < t["rate"]  # 每回合以資料 rate 擲骰; when.rounds/from/until 只在符合回合才擲骰; when.on(反應式) 改由 on_hit 事件點觸發
                    if fire:
                        if t["coef"]:
                            cnt = t["n"]
                            if t.get("nMax"):
                                cnt = t["n"] + random.randint(0, t["nMax"] - t["n"])
                            for v in pick_targets(foes_of(u), cnt):
                                hit(u, v, t["coef"], t["kind"], False, on_hit)
                        if t["type"] == "active":
                            apply_effects(u, pick_target(foes_of(u), u), t, allies_of(u), foes_of(u))
            tgt = pick_target(foes_of(u), u)               # 普攻(每回合常駐) + 連擊 + 突擊(繳械時跳過); 嘲諷: 強制指向施放者
            if tgt and not u.disarm:
                hit(u, tgt, 1.0, "phys", True, on_hit)
                for _ in range(extra_count(u.addbonus("extra"))):  # 連擊/追擊
                    nt = pick_target(foes_of(u), u)
                    if nt:
                        hit(u, nt, 1.0, "phys", True, on_hit)
                # 突擊(charge)擲骰: chargeup(突擊發動率加成, 如虎豹騎)只對真突擊戰法生效, 排除
                # t.get("proc") is True 的特技偽戰法(user 明確指示: 特技不吃突擊加成, 例虎豹騎/三勢陣/經天緯地/陷陣突襲)。
                for t in u.tactics:
                    up = 0 if t.get("proc") else u.addbonus_for("chargeup", t)
                    if t["type"] == "charge" and random.random() < t["rate"] + up:
                        if t["coef"]:
                            hit(u, tgt, t["coef"], t["kind"], False, on_hit)
                        apply_effects(u, tgt, t, allies_of(u), foes_of(u))

        for u in A + B:                               # 結算傷害: 疊滿層數或到期 → 對其所屬全隊爆發
            s = u.settle
            if not s:
                continue
            if s["layers"] >= s["max"] or s["left"] <= 1:
                team = A if id(u) in setA else B
                for v in [x for x in team if x.alive]:
                    v.troop -= damage(s["caster"], v, s["base"] + s["per"] * s["layers"],
                                      s["kind"], s["snap"])
                u.settle = None
            else:
                s["left"] -= 1

        for u in A + B:
            u.tick()
        if not any(u.alive for u in A):
            return "B", rnd
        if not any(u.alive for u in B):
            return "A", rnd
    ta = sum(max(0, u.troop) for u in A)
    tb = sum(max(0, u.troop) for u in B)
    return ("A" if ta >= tb else "B"), ROUNDS


def simulate(teamA, teamB, n=3000, troopA=None, troopB=None, bsA=None, bsB=None, eqA=None, eqB=None,
             addA=None, addB=None, inhA=None, inhB=None, scenario=None):
    w = {"A": 0, "B": 0}
    rs = 0
    for _ in range(n):
        winner, r = fight(teamA, teamB, troopA, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario)
        w[winner] += 1
        rs += r
    return {"A勝率": round(w["A"] / n, 3), "B勝率": round(w["B"] / n, 3),
            "平均回合": round(rs / n, 1)}


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
    def on_hit_test(dst, src, is_normal):
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
