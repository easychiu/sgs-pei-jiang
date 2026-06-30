// 三國志戰略版 配將引擎 — sgz.py 的 JS 移植(瀏覽器/node 通用)
// 兵種由「隊伍」決定; 各武將只有對該兵種的適性(S/A/B/C/D)決定屬性發揮。克制為隊伍兵種 vs 隊伍兵種。
// 15 原語: coef amp mitig stun heal stat dot settle extra redirect stack decay swap pierce counter
"use strict";
(function (root) {
  const ROUNDS = 8, START_TROOP = 10000, MORALE = 100;
  let CMD_TRIGGER = 0.40, PASSIVE_TRIGGER = 0.45;
  const COUNTER = { "騎": "盾", "盾": "弓", "弓": "槍", "槍": "騎" };
  const APT_PCT = { S: 1.20, A: 1.00, B: 0.85, C: 0.70, D: 0.55 };
  const APT_RANK = { S: 4, A: 3, B: 2, C: 1, D: 0 };
  const TROOPS = ["騎", "盾", "弓", "槍", "器"];
  let BINGSHU = {}, MAIN_BY_CAT = {}, SUB_BY_CAT = {};  // 兵書: 名稱→效果; 類別→主/副兵書們
  let BONDS = [], EQUIPS = {};                       // 緣分(隊伍級) / 裝備(自身)
  const rnd = () => Math.random();

  const moraleMult = m => 0.007 * m + 0.30;
  function counterMult(a, b) {
    if (a === "器" || b === "器") return b === "器" ? 1.15 : 0.85;
    if (COUNTER[a] === b) return 1.15;
    if (COUNTER[b] === a) return 0.85;
    return 1.0;
  }
  const aptPct = (g, troop) => APT_PCT[g.apt[troop]] ?? 0.85;
  function bestTroop(apt) {
    let best = "騎", r = -1;
    for (const t of TROOPS) { const rr = APT_RANK[apt[t]] ?? -1; if (rr > r) { r = rr; best = t; } }
    return best;
  }
  function teamTroop(POOL, team) {                 // 一隊的建議兵種: 三人對該兵種適性總和最高
    let best = "騎", bs = -1;
    for (const t of TROOPS) {
      const s = team.reduce((a, n) => a + aptPct(POOL[n], t), 0);
      if (s > bs) { bs = s; best = t; }
    }
    return best;
  }

  function defaultBingshu(g) {                       // 預設主兵書: 首個可用類別的主兵書
    for (const c of (g.bingshuCats || [])) if (MAIN_BY_CAT[c]) return MAIN_BY_CAT[c][0];
    return null;
  }

  function activeBonds(team) {                       // 隊伍湊齊 triggerCount 人即觸發
    return BONDS.filter(b => (b.generals || []).filter(n => team.includes(n)).length >= (b.triggerCount || 99));
  }

  function buildPool(generals, tactics, bingshu, bonds, equips) {
    const TAC = {};
    for (const t of tactics) if (t.type !== "none") TAC[t.nameZh] = t;
    BINGSHU = {}; MAIN_BY_CAT = {}; SUB_BY_CAT = {}; BONDS = bonds || []; EQUIPS = {};
    for (const b of (bingshu || [])) {
      const key = b.category + "·" + b.name;        // 複合鍵(同名跨類別不撞)
      BINGSHU[key] = b;
      const m = b.type === "主兵書" ? MAIN_BY_CAT : SUB_BY_CAT;
      (m[b.category] = m[b.category] || []).push(key);
    }
    for (const e of (equips || [])) EQUIPS[e.name] = e;
    const POOL = {};
    for (const raw of generals) {
      if (!raw.stats) continue;
      const st = raw.stats;
      POOL[raw.name] = {
        name: raw.name, faction: raw.faction || "?", stars: raw.stars,
        apt: raw.affinity || {}, bingshuCats: raw.availableBingshu || [],
        gender: raw.gender, growth: raw.growthStats || null,
        base: { force: st["武力"] ?? 80, intel: st["智力"] ?? 80, command: st["統率"] ?? 90, speed: st["速度"] ?? 70 },
        tacticName: raw.tactic || "—", tactic: raw.tactic ? (TAC[raw.tactic] || null) : null,
      };
    }
    return { POOL, TAC };
  }

  class Unit {
    constructor(g, ttype, bsName, eqName, add) {
      this.g = g; this.ttype = ttype; this.troop = START_TROOP; this.stun = 0;
      const _bn = Array.isArray(bsName) ? bsName : (bsName ? [bsName] : []);
      this.bs = _bn.flatMap(nm => (BINGSHU[nm] ? BINGSHU[nm].effects : []));  // 兵書(主+副)合併
      this.eq = (eqName && EQUIPS[eqName]) ? EQUIPS[eqName].effects : [];    // 裝備被動
      const a = add || {};                         // 養成加值: 加點/進階/典藏(適性前疊加)
      const m = aptPct(g, ttype);                  // 屬性 = (基礎+養成) × 該兵種適性%
      this.force = (g.base.force + (a.force || 0)) * m; this.intel = (g.base.intel + (a.intel || 0)) * m;
      this.command = (g.base.command + (a.command || 0)) * m; this.speed = (g.base.speed + (a.speed || 0)) * m;
      this.mods = []; this.adds = []; this.dots = [];
      if (a.amp) this.adds.push(["amp", a.amp, 9999]);    // 進階/典藏 攻防加成
      if (a.mitig) this.adds.push(["mitig", a.mitig, 9999]);
      this.settle = null; this.guardian = null; this.guardShare = 0;
      this.stack = null; this.decay = null; this.swap = 0; this.counter = null;
    }
    get alive() { return this.troop > 0; }
    eff(stat) {
      if (this.swap && (stat === "force" || stat === "intel")) stat = stat === "force" ? "intel" : "force";
      let v = this[stat];
      for (const [s, m] of this.mods) if (s === stat || s === "all") v *= m;
      return v;
    }
    addbonus(kind) { let s = 0; for (const a of this.adds) if (a[0] === kind) s += a[1]; return s; }
    amp() {
      let a = this.addbonus("amp");
      if (this.stack) a += this.stack.per * this.stack.n;
      if (this.decay) a += this.decay.v0 * this.decay.left / this.decay.total;
      return a;
    }
    tick() {
      for (const d of this.dots) this.troop -= d[0];
      this.dots = this.dots.filter(d => --d[1] > 0);
      this.mods = this.mods.filter(m => --m[2] > 0);
      this.adds = this.adds.filter(a => --a[2] > 0);
      this.stun = Math.max(0, this.stun - 1);
      this.swap = Math.max(0, this.swap - 1);
      if (this.decay && --this.decay.left <= 0) this.decay = null;
    }
  }

  function damage(src, dst, coef, kind, srcTroop) {
    const troop = srcTroop == null ? src.troop : srcTroop;
    const atk = kind === "intel" ? src.eff("intel") : src.eff("force");
    const def = kind === "intel" ? dst.eff("intel") : dst.eff("command");
    let base = ((atk - def) / 150 + 1) * (troop / 20) * coef;
    base *= counterMult(src.ttype, dst.ttype);     // 克制: 隊伍兵種 vs 隊伍兵種
    base *= moraleMult(MORALE);
    base *= Math.max(0, 1 + src.amp());
    const mit = dst.addbonus("mitig") * (1 - Math.min(1, src.addbonus("pierce")));
    base *= Math.max(0.1, 1 - mit);
    base *= 0.96 + rnd() * 0.08;
    return Math.max(0, base);
  }
  function hit(src, dst, coef, kind) {
    const dmg = damage(src, dst, coef, kind);
    const g = dst.guardian;
    if (g && g.alive && g !== dst) { g.troop -= dmg * dst.guardShare; dst.troop -= dmg * (1 - dst.guardShare); }
    else dst.troop -= dmg;
    if (dst.settle) dst.settle.layers = Math.min(dst.settle.max, dst.settle.layers + 1);
    const c = dst.counter;
    if (c && dst.alive && src.alive && rnd() < (c.prob ?? 1))
      src.troop -= damage(dst, src, c.coef ?? 1, c.kind || "phys");
  }
  function extraCount(ex) { const i = Math.floor(ex); return i + (rnd() < ex - i ? 1 : 0); }
  function pickTarget(units) {
    let best = null;
    for (const u of units) if (u.alive && (!best || u.troop > best.troop)) best = u;
    return best;
  }

  function applyEffects(caster, tgt, t, allies, enemies, opt) {
    opt = opt || {};
    for (const e of t.effects) {
      const k = e.k;
      if (opt.healOnly && k !== "heal") continue;
      if (k === "heal") {
        if (opt.noHeal) continue;
        let hurt = null;
        for (const a of allies) if (a.alive && (!hurt || a.troop < hurt.troop)) hurt = a;
        if (hurt) hurt.troop += (e.coef ?? 0.8) * caster.troop * 0.10;
        continue;
      }
      if (k === "settle") {
        let tg = null;
        for (const x of enemies) if (x.alive && (!tg || x.eff("command") > tg.eff("command"))) tg = x;
        if (tg) tg.settle = { layers: e.init ?? 1, max: e.max ?? 3, left: e.dur ?? 2, caster, snap: caster.troop, base: e.base ?? 1.5, per: e.per ?? 0.4, kind: t.kind || "intel" };
        continue;
      }
      if (k === "redirect") {
        let guard = caster;
        if (e.guard === "max_force") { for (const a of allies) if (a.alive && (guard === caster || a.eff("force") > guard.eff("force"))) guard = a; }
        for (const a of allies) if (a.alive && a !== guard) { a.guardian = guard; a.guardShare = e.share ?? 0.3; }
        continue;
      }
      const who = e.who || "ally";
      let dests;
      if (who === "self") dests = caster.alive ? [caster] : [];
      else if (who === "enemy") dests = (k === "stun") ? (tgt && tgt.alive ? [tgt] : []) : enemies.filter(x => x.alive);
      else dests = allies.filter(a => a.alive);
      for (const u of dests) {
        if (k === "amp") u.adds.push(["amp", e.val, e.dur]);
        else if (k === "mitig") u.adds.push(["mitig", e.val, e.dur]);
        else if (k === "stun") u.stun = Math.max(u.stun, (e.dur ?? 1) + 1);
        else if (k === "stat") u.mods.push([e.stat, e.mult ?? 1, e.dur]);
        else if (k === "dot") u.dots.push([damage(caster, u, e.coef ?? 0.5, t.kind || "intel"), e.dur]);
        else if (k === "extra") u.adds.push(["extra", e.val, e.dur]);
        else if (k === "stack") u.stack = { per: e.per ?? 0.1, max: e.max ?? 5, n: 0 };
        else if (k === "decay") u.decay = { v0: e.v0 ?? 0.5, left: e.rounds ?? 8, total: e.rounds ?? 8 };
        else if (k === "swap") u.swap = Math.max(u.swap, (e.dur ?? 1) + 1);
        else if (k === "pierce") u.adds.push(["pierce", e.val, e.dur]);
        else if (k === "counter") u.counter = { coef: e.coef ?? 1, kind: e.kind || "phys", prob: e.prob ?? 1 };
      }
    }
  }

  function fight(POOL, teamA, troopA, teamB, troopB, bsA, bsB, eqA, eqB, addA, addB) {
    troopA = troopA || teamTroop(POOL, teamA);
    troopB = troopB || teamTroop(POOL, teamB);
    bsA = bsA || teamA.map(n => defaultBingshu(POOL[n]));
    bsB = bsB || teamB.map(n => defaultBingshu(POOL[n]));
    eqA = eqA || teamA.map(() => null);
    eqB = eqB || teamB.map(() => null);
    addA = addA || teamA.map(() => null);
    addB = addB || teamB.map(() => null);
    const A = teamA.map((n, i) => new Unit(POOL[n], troopA, bsA[i], eqA[i], addA[i])), B = teamB.map((n, i) => new Unit(POOL[n], troopB, bsB[i], eqB[i], addB[i]));
    const setA = new Set(A);
    const alliesOf = u => setA.has(u) ? A : B, foesOf = u => setA.has(u) ? B : A;
    const bondsA = activeBonds(teamA), bondsB = activeBonds(teamB);
    const pt = eff => ({ effects: eff, kind: "phys" });
    const applyPassives = opt => {                  // 被動/指揮/兵書/裝備/緣分 統一套用
      for (const u of [...A, ...B]) {
        if (!u.alive) continue;
        if (u.g.tactic && (u.g.tactic.type === "passive" || u.g.tactic.type === "command"))
          applyEffects(u, null, u.g.tactic, alliesOf(u), foesOf(u), opt);
        if (u.bs.length) applyEffects(u, null, pt(u.bs), alliesOf(u), foesOf(u), opt);
        if (u.eq.length) applyEffects(u, null, pt(u.eq), alliesOf(u), foesOf(u), opt);
      }
      for (const [team, bds] of [[A, bondsA], [B, bondsB]])
        if (team.length) for (const bd of bds)
          applyEffects(team[0], null, pt(bd.effects), team, foesOf(team[0]), opt);
    };
    applyPassives({ noHeal: true });

    for (let r = 1; r <= ROUNDS; r++) {
      for (const u of [...A, ...B]) if (u.alive && u.stack) u.stack.n = Math.min(u.stack.max, u.stack.n + 1);
      applyPassives({ healOnly: true });
      const order = [...A, ...B].filter(u => u.alive).sort((x, y) => y.eff("speed") - x.eff("speed"));
      for (const u of order) {
        if (!u.alive || u.stun) continue;
        if (!pickTarget(foesOf(u))) break;
        const t = u.g.tactic;
        let cast = false;
        if (t) {
          if (t.type === "active" && (t.coef || t.effects.length) && !(t.prep && r === 1)) cast = rnd() < t.rate;
          else if (t.type === "command" && t.coef) cast = rnd() < CMD_TRIGGER;
          else if (t.type === "passive" && t.coef) cast = rnd() < t.rate * PASSIVE_TRIGGER;
        }
        if (cast) {
          for (let i = 0; i < t.n; i++) { const v = pickTarget(foesOf(u)); if (v && t.coef) hit(u, v, t.coef, t.kind); }
          if (t.type === "active") applyEffects(u, pickTarget(foesOf(u)), t, alliesOf(u), foesOf(u));
        } else {
          const tgt = pickTarget(foesOf(u));
          hit(u, tgt, 1.0, "phys");
          for (let i = 0; i < extraCount(u.addbonus("extra")); i++) { const nt = pickTarget(foesOf(u)); if (nt) hit(u, nt, 1.0, "phys"); }
          if (t && t.type === "charge" && rnd() < t.rate) { if (t.coef) hit(u, tgt, t.coef, t.kind); applyEffects(u, tgt, t, alliesOf(u), foesOf(u)); }
        }
      }
      for (const u of [...A, ...B]) {
        const s = u.settle; if (!s) continue;
        if (s.layers >= s.max || s.left <= 1) {
          for (const v of (setA.has(u) ? A : B)) if (v.alive) v.troop -= damage(s.caster, v, s.base + s.per * s.layers, s.kind, s.snap);
          u.settle = null;
        } else s.left -= 1;
      }
      for (const u of [...A, ...B]) u.tick();
      if (!A.some(u => u.alive)) return { winner: "B", rounds: r };
      if (!B.some(u => u.alive)) return { winner: "A", rounds: r };
    }
    const ta = A.reduce((s, u) => s + Math.max(0, u.troop), 0), tb = B.reduce((s, u) => s + Math.max(0, u.troop), 0);
    return { winner: ta >= tb ? "A" : "B", rounds: ROUNDS };
  }

  function simulate(POOL, teamA, teamB, n = 2000, troopA = null, troopB = null, bsA = null, bsB = null, eqA = null, eqB = null, addA = null, addB = null) {
    let a = 0, rs = 0;
    for (let i = 0; i < n; i++) { const r = fight(POOL, teamA, troopA, teamB, troopB, bsA, bsB, eqA, eqB, addA, addB); if (r.winner === "A") a++; rs += r.rounds; }
    return { winA: +(a / n).toFixed(3), winB: +(1 - a / n).toFixed(3), rounds: +(rs / n).toFixed(1) };
  }

  function score(POOL, team, troop) {
    troop = troop || teamTroop(POOL, team);
    const g = team.map(n => POOL[n]);
    const attr = g.reduce((s, x) => s + (Math.max(x.base.force, x.base.intel) + x.base.command + x.base.speed) * aptPct(x, troop), 0);
    const kinds = new Set();
    for (const x of g) if (x.tactic) { kinds.add(x.tactic.type); for (const e of x.tactic.effects) kinds.add(e.k); }
    const aptBonus = Math.round(g.reduce((s, x) => s + aptPct(x, troop), 0) / 3 * 80);
    const sameFac = new Set(g.map(x => x.faction)).size === 1 ? 40 : 0;
    return Math.round(attr / 3 + kinds.size * 25 + aptBonus + sameFac);
  }
  const baseName = n => n.replace("SP ", "").replace("SP", "");
  function recommend(POOL, { pool, k = 3, top = 8 } = {}) {
    const names = pool || Object.keys(POOL), out = [];
    for (let i = 0; i < names.length; i++) for (let j = i + 1; j < names.length; j++) for (let l = j + 1; l < names.length; l++) {
      const c = [names[i], names[j], names[l]];
      if (new Set(c.map(baseName)).size < 3) continue;
      out.push(c);
    }
    out.sort((a, b) => score(POOL, b) - score(POOL, a));
    return out.slice(0, top).map(c => [c, score(POOL, c), teamTroop(POOL, c)]);
  }

  const API = { buildPool, simulate, score, recommend, fight, teamTroop, aptPct, bestTroop, TROOPS,
    defaultBingshu, activeBonds, mainByCat: () => MAIN_BY_CAT, subByCat: () => SUB_BY_CAT, bingshu: () => BINGSHU,
    bonds: () => BONDS, equips: () => EQUIPS,
    setKnobs: (c, p) => { CMD_TRIGGER = c; PASSIVE_TRIGGER = p; } };
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  root.SGZ = API;
})(typeof globalThis !== "undefined" ? globalThis : this);
