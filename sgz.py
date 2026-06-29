# -*- coding: utf-8 -*-
"""
三國志戰略版 配將引擎 — 評分 + 配將推薦 + 逐回合模擬對戰

v4: 讀 sgsdeck 真實全庫(193武將 / 384戰法)。
戰法 effectText 為自然語言, 用 effectType 路由 + 正規表達式抽數值做啟發式解析
(approximation, 非逐條精解; 精準需人工/LLM 解 effectText)。
傷害用社群拆解公式(data/formula.md)。run: python sgz.py
"""
import json
import os
import random
import re
from itertools import combinations

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ROUNDS = 8
START_TROOP = 10000
MORALE = 100                                         # ponytail: 士氣固定滿
# 指揮/被動戰法的傷害多為條件觸發(敵出主動時、第N回合起…), 引擎無法判條件,
# 用觸發折扣近似。ponytail: 全域旋鈕, 要精準得逐戰法建模條件
CMD_TRIGGER = 0.40
PASSIVE_TRIGGER = 0.45

COUNTER = {"騎": "盾", "盾": "弓", "弓": "槍", "槍": "騎"}  # 騎>盾>弓>槍>騎; 器全被克
APT_PCT = {"S": 1.20, "A": 1.00, "B": 0.85, "C": 0.70, "D": 0.55, None: 0.85}
APT_RANK = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0, None: -1}


def morale_mult(m):
    return 0.007 * m + 0.30


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
        self.apt = raw.get("affinity", {})           # 各兵種適性 S/A/B/C/D, 戰鬥兵種由隊伍決定
        st = raw.get("stats", {})
        self.base = {"force": st.get("武力", 80), "intel": st.get("智力", 80),
                     "command": st.get("統率", 90), "speed": st.get("速度", 70)}
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
               key=lambda t: sum(g.apt_pct(t) for g in gs))


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
BINGSHU, MAIN_BY_CAT = {}, {}
_bs = os.path.join(DATA, "bingshu_parsed.json")
if os.path.exists(_bs):
    for b in json.load(open(_bs, encoding="utf-8")):
        BINGSHU[b["name"]] = b
        if b.get("type") == "主兵書":
            MAIN_BY_CAT.setdefault(b["category"], []).append(b["name"])


def default_bingshu(g):                               # 預設主兵書: 該將首個可用類別的主兵書
    for c in g.bingshu_cats:
        if MAIN_BY_CAT.get(c):
            return MAIN_BY_CAT[c][0]
    return None


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
    def __init__(self, g, ttype, bingshu=None):
        self.g, self.ttype, self.troop, self.stun = g, ttype, START_TROOP, 0
        self.bs = BINGSHU.get(bingshu, {}).get("effects", []) if bingshu else []  # 兵書被動效果
        mult = g.apt_pct(ttype)                       # 屬性 = 基礎 × 該兵種適性%
        self.force = g.base["force"] * mult
        self.intel = g.base["intel"] * mult
        self.command = g.base["command"] * mult
        self.speed = g.base["speed"] * mult
        self.mods = []                                # 乘法: [stat, mult, left]
        self.adds = []                                # 加法: [amp|mitig|extra, val, left]
        self.dots = []                                # 持續傷害: [每回合傷害, left]
        self.settle = None                            # 結算狀態(猛毒)
        self.guardian = None                          # 傷害轉移: 代承者
        self.guard_share = 0.0                        # 代承比例
        self.stack = None                             # 疊加增益: {per, max, n}
        self.decay = None                             # 衰減增益: {v0, left, total}
        self.swap = 0                                 # 武智互換 剩餘回合
        self.counter = None                           # 反擊: {coef, kind, prob}

    @property
    def alive(self):
        return self.troop > 0

    def eff(self, stat):
        if self.swap and stat in ("force", "intel"):  # 武智互換
            stat = "intel" if stat == "force" else "force"
        v = getattr(self, stat)
        for s, m, _ in self.mods:
            if s == stat or s == "all":               # stat="all" 套全屬性
                v *= m
        return v

    def addbonus(self, kind):
        return sum(v for k, v, _ in self.adds if k == kind)

    def amp(self):                                    # 總增傷 = 一般+疊加層+衰減
        a = self.addbonus("amp")
        if self.stack:
            a += self.stack["per"] * self.stack["n"]
        if self.decay:
            a += self.decay["v0"] * self.decay["left"] / self.decay["total"]
        return a

    def tick(self):
        for dmg, _ in self.dots:                      # 持續傷害結算
            self.troop -= dmg
        self.dots = [[d, l - 1] for d, l in self.dots if l - 1 > 0]
        self.mods = [[s, m, l - 1] for s, m, l in self.mods if l - 1 > 0]
        self.adds = [[k, v, l - 1] for k, v, l in self.adds if l - 1 > 0]
        self.stun = max(0, self.stun - 1)
        self.swap = max(0, self.swap - 1)
        if self.decay:
            self.decay["left"] -= 1
            if self.decay["left"] <= 0:
                self.decay = None


def damage(src, dst, coef, kind, src_troop=None):
    troop = src.troop if src_troop is None else src_troop  # 結算傷害用施毒當下定格兵力
    atk = src.eff("intel") if kind == "intel" else src.eff("force")
    deff = dst.eff("intel") if kind == "intel" else dst.eff("command")
    base = ((atk - deff) / 150 + 1) * (troop / 20) * coef
    base *= counter_mult(src.ttype, dst.ttype)        # 克制: 隊伍兵種 vs 隊伍兵種
    base *= morale_mult(MORALE)
    base *= max(0.0, 1 + src.amp())                   # 增傷(疊加/衰減/敵方減益)
    mit = dst.addbonus("mitig") * (1 - min(1.0, src.addbonus("pierce")))  # 看破: 無視部分減傷
    base *= max(0.1, 1 - mit)
    base *= random.uniform(0.96, 1.04)
    return max(0, base)


def hit(src, dst, coef, kind):                        # 造成傷害(含代承轉移/反擊), 累積結算層數
    dmg = damage(src, dst, coef, kind)
    g = dst.guardian
    if g and g.alive and g is not dst:                # 傷害轉移: 代承者吃一部分
        g.troop -= dmg * dst.guard_share
        dst.troop -= dmg * (1 - dst.guard_share)
    else:
        dst.troop -= dmg
    if dst.settle:
        dst.settle["layers"] = min(dst.settle["max"], dst.settle["layers"] + 1)
    c = dst.counter                                   # 反擊: 直接還擊 src(不經 hit, 不遞迴)
    if c and dst.alive and src.alive and random.random() < c.get("prob", 1.0):
        src.troop -= damage(dst, src, c["coef"], c.get("kind", "phys"))


def extra_count(ex):                                  # 連擊/追擊次數: 整數部分必定, 小數部分機率
    return int(ex) + (1 if random.random() < (ex - int(ex)) else 0)


def pick_target(units):
    live = [u for u in units if u.alive]
    return max(live, key=lambda u: u.troop) if live else None


def apply_effects(caster, tgt, t, allies, enemies, heal_only=False, no_heal=False):
    for e in t["effects"]:
        k = e["k"]
        if heal_only and k != "heal":                 # 指揮/被動逐回合只跑治療
            continue
        if k == "heal":                               # 治療: 補我方最殘一人(指揮/被動每回合觸發)
            if no_heal:
                continue
            hurt = min((a for a in allies if a.alive),
                       key=lambda a: a.troop, default=None)
            if hurt:                                  # ponytail: 治療量粗估
                hurt.troop += e.get("coef", 0.8) * caster.troop * 0.10
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
            continue
        who = e.get("who", "ally")
        if who == "self":
            dests = [caster] if caster.alive else []
        elif who == "enemy":
            dests = ([tgt] if tgt and tgt.alive else []) if k == "stun" \
                else [x for x in enemies if x.alive]
        else:
            dests = [a for a in allies if a.alive]
        for u in dests:
            if k == "amp":
                u.adds.append(["amp", e["val"], e["dur"]])
            elif k == "mitig":
                u.adds.append(["mitig", e["val"], e["dur"]])
            elif k == "stun":
                u.stun = max(u.stun, e["dur"] + 1)
            elif k == "stat":
                u.mods.append([e["stat"], e.get("mult", 1.0), e["dur"]])
            elif k == "dot":                          # 持續傷害: 套用時定格每回合傷害
                u.dots.append([damage(caster, u, e.get("coef", 0.5),
                                      t.get("kind", "intel")), e["dur"]])
            elif k == "extra":                        # 連擊/追擊: 普攻後追加普攻的預算
                u.adds.append(["extra", e["val"], e["dur"]])
            elif k == "stack":                        # 疊加增益: 每回合+1層, 每層加 per 增傷
                u.stack = {"per": e.get("per", 0.1), "max": e.get("max", 5), "n": 0}
            elif k == "decay":                        # 衰減增益: 開場 v0 增傷, rounds 內線性歸零
                u.decay = {"v0": e.get("v0", 0.5), "left": e.get("rounds", 8),
                           "total": e.get("rounds", 8)}
            elif k == "swap":                         # 武智互換
                u.swap = max(u.swap, e.get("dur", 1) + 1)
            elif k == "pierce":                       # 看破: 無視目標 val 比例的減傷
                u.adds.append(["pierce", e["val"], e["dur"]])
            elif k == "counter":                      # 反擊: 受擊時還擊
                u.counter = {"coef": e.get("coef", 1.0), "kind": e.get("kind", "phys"),
                             "prob": e.get("prob", 1.0)}


def fight(teamA, teamB, troopA=None, troopB=None, bsA=None, bsB=None):
    troopA = troopA or team_troop(teamA)              # 未指定兵種則用隊伍最佳適性
    troopB = troopB or team_troop(teamB)
    bsA = bsA or [default_bingshu(POOL[n]) for n in teamA]   # 未指定兵書則裝預設主兵書
    bsB = bsB or [default_bingshu(POOL[n]) for n in teamB]
    A = [Unit(POOL[n], troopA, bsA[i]) for i, n in enumerate(teamA)]
    B = [Unit(POOL[n], troopB, bsB[i]) for i, n in enumerate(teamB)]
    setA = set(map(id, A))
    allies_of = lambda u: A if id(u) in setA else B
    foes_of = lambda u: B if id(u) in setA else A

    for u in A + B:                                   # 被動/指揮 + 兵書 的持久效果: 開戰套一次(治療除外)
        if u.g.tactic and u.g.tactic["type"] in ("passive", "command"):
            apply_effects(u, None, u.g.tactic, allies_of(u), foes_of(u), no_heal=True)
        if u.bs:
            apply_effects(u, None, {"effects": u.bs, "kind": "phys"}, allies_of(u), foes_of(u), no_heal=True)

    for rnd in range(1, ROUNDS + 1):
        for u in A + B:
            if not u.alive:
                continue
            if u.stack:                               # 疊加增益: 每回合 +1 層
                u.stack["n"] = min(u.stack["max"], u.stack["n"] + 1)
            if u.g.tactic and u.g.tactic["type"] in ("passive", "command"):
                apply_effects(u, None, u.g.tactic, allies_of(u), foes_of(u), heal_only=True)
            if u.bs:                                  # 兵書治療逐回合
                apply_effects(u, None, {"effects": u.bs, "kind": "phys"}, allies_of(u), foes_of(u), heal_only=True)

        for u in sorted([x for x in A + B if x.alive],
                        key=lambda x: x.eff("speed"), reverse=True):
            if not u.alive or u.stun:
                continue
            if pick_target(foes_of(u)) is None:
                break
            t = u.g.tactic
            cast = False
            if t:                                         # 哪種戰法這回合發動?
                if t["type"] == "active" and (t["coef"] or t["effects"]) \
                        and not (t["prep"] and rnd == 1):
                    cast = random.random() < t["rate"]    # 主動: 有傷害或有效果就試發
                elif t["type"] == "command" and t["coef"]:
                    cast = random.random() < CMD_TRIGGER  # 指揮傷害: 條件觸發折扣
                elif t["type"] == "passive" and t["coef"]:
                    cast = random.random() < t["rate"] * PASSIVE_TRIGGER  # 被動傷害: 折扣
            if cast:
                for _ in range(t["n"]):
                    v = pick_target(foes_of(u))
                    if v and t["coef"]:
                        hit(u, v, t["coef"], t["kind"])
                if t["type"] == "active":                 # 主動附帶效果隨擊發動(指揮/被動效果已在開戰套)
                    apply_effects(u, pick_target(foes_of(u)), t, allies_of(u), foes_of(u))
            else:
                tgt = pick_target(foes_of(u))
                hit(u, tgt, 1.0, "phys")
                for _ in range(extra_count(u.addbonus("extra"))):  # 連擊/追擊
                    nt = pick_target(foes_of(u))
                    if nt:
                        hit(u, nt, 1.0, "phys")
                if t and t["type"] == "charge" and random.random() < t["rate"]:
                    if t["coef"]:
                        hit(u, tgt, t["coef"], t["kind"])
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


def simulate(teamA, teamB, n=3000, troopA=None, troopB=None, bsA=None, bsB=None):
    w = {"A": 0, "B": 0}
    rs = 0
    for _ in range(n):
        winner, r = fight(teamA, teamB, troopA, troopB, bsA, bsB)
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
    u.adds.append(["amp", 0.2, 1])
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
    hit(Unit(POOL["周瑜"], "弓"), prot, 1.0, "phys")
    assert grd.troop < g0 and prot.troop < p0, "代承者與被保護者各吃一部分"
    atk, df = Unit(lb, "騎"), Unit(POOL["張飛"], "盾")
    df.adds.append(["mitig", 0.5, 9])
    d_norm = damage(atk, df, 1.0, "phys")
    atk.adds.append(["pierce", 1.0, 9])
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
    res = simulate(["呂布", "趙雲", "關羽"], ["諸葛亮", "周瑜", "司馬懿"], n=400)
    assert 0 <= res["A勝率"] <= 1 and 1 <= res["平均回合"] <= ROUNDS
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
