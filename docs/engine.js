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
  let TACTICS = {};                                 // 名稱→戰法(供傳承查詢)
  let BONDS = [], EQUIPS = {};                       // 緣分(隊伍級) / 裝備(自身)
  let SEASON_MODS = {};                              // 賽季修正 {id:[mod]}
  let TRACE = null, CUR_R = 0;                        // 推演日誌: TRACE=陣列時記錄事件; CUR_R=當前回合(0=準備)
  const lg = t => { if (TRACE) TRACE.push({ r: CUR_R, t }); };
  function seasonModsFor(POOL, g, idx, team, scenario) {
    const out = { aptAdd: 0, aptS: false, flat: 0, mult: 1.0 };
    for (const m of (scenario ? (SEASON_MODS[scenario] || []) : [])) {
      if (m.type === "faction_scale") {
        const facs = team.map(n => POOL[n].faction);
        const top = Math.max(0, ...[...new Set(facs)].map(f => facs.filter(x => x === f).length));
        if (top >= (m.partialThreshold ?? 2)) out.mult *= 1 + (top >= team.length ? (m.fullBonus ?? 0.1) : (m.partialBonus ?? 0.07));
      } else if (m.type === "apt_add" && g.gender === m.gender) out.aptAdd += m.value ?? 0.15;
      else if (m.type === "apt_override" && idx < (m.maxSlots ?? 2)) out.aptS = true;
      else if (m.type === "stat_flat") out.flat += m.all ?? 0;
    }
    return out;
  }
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

  function buildPool(generals, tactics, bingshu, bonds, equips, seasonMods) {
    SEASON_MODS = seasonMods || {};
    const TAC = {};
    for (const t of tactics) if (t.type !== "none") TAC[t.nameZh] = t;
    TACTICS = TAC;
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
        bingshuOptions: raw.bingshuOptions || null,
        gender: raw.gender, growth: raw.growthStats || null,
        base: { force: st["武力"] ?? 80, intel: st["智力"] ?? 80, command: st["統率"] ?? 90, speed: st["速度"] ?? 70 },
        tacticName: raw.tactic || "—", tactic: raw.tactic ? (TAC[raw.tactic] || null) : null,
      };
    }
    return { POOL, TAC };
  }

  class Unit {
    constructor(g, ttype, bsName, eqName, add, inherit, season) {
      this.g = g; this.ttype = ttype; this.troop = START_TROOP; this.stun = 0;
      this.tactics = (g.tactic ? [g.tactic] : []).concat((inherit || []).map(nm => TACTICS[nm]).filter(Boolean));  // 自帶 + 傳承
      const _bn = Array.isArray(bsName) ? bsName : (bsName ? [bsName] : []);
      this.bs = _bn.flatMap(nm => (BINGSHU[nm] ? BINGSHU[nm].effects : []));  // 兵書(主+副)合併
      const _eq = Array.isArray(eqName) ? eqName : (eqName ? [eqName] : []);
      this.eq = _eq.flatMap(nm => (EQUIPS[nm] ? EQUIPS[nm].effects : []));   // 裝備(4欄)合併
      const a = add || {}, sm = season || {};      // 養成加值 + 賽季修正
      const apt = (sm.aptS ? 1.20 : aptPct(g, ttype)) + (sm.aptAdd || 0);
      const scm = sm.mult || 1.0, flat = sm.flat || 0;  // 屬性=(基礎+養成+賽季固定)×適性×賽季乘數
      this.force = (g.base.force + (a.force || 0) + flat) * apt * scm; this.intel = (g.base.intel + (a.intel || 0) + flat) * apt * scm;
      this.command = (g.base.command + (a.command || 0) + flat) * apt * scm; this.speed = (g.base.speed + (a.speed || 0) + flat) * apt * scm;
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
    if (TRACE) lg(`　→ ${dst.nm} 損兵 ${Math.round(dmg)}，剩餘 ${Math.max(0, Math.round(dst.troop))}` + (dst.troop <= 0 ? " 【擊破】" : ""));
    if (dst.settle) dst.settle.layers = Math.min(dst.settle.max, dst.settle.layers + 1);
    const c = dst.counter;
    if (c && dst.alive && src.alive && rnd() < (c.prob ?? 1)) {
      const cd = damage(dst, src, c.coef ?? 1, c.kind || "phys"); src.troop -= cd;
      if (TRACE) lg(`　↩ ${dst.nm} 反擊 ${src.nm} 損兵 ${Math.round(cd)}，剩餘 ${Math.max(0, Math.round(src.troop))}`);
    }
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
        if (k === "amp") u.adds.push(who === "enemy" && e.val > 0 ? ["mitig", -e.val, e.dur] : ["amp", e.val, e.dur]);
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

  function fight(POOL, teamA, troopA, teamB, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario) {
    troopA = troopA || teamTroop(POOL, teamA);
    troopB = troopB || teamTroop(POOL, teamB);
    bsA = bsA || teamA.map(n => defaultBingshu(POOL[n]));
    bsB = bsB || teamB.map(n => defaultBingshu(POOL[n]));
    eqA = eqA || teamA.map(() => null);
    eqB = eqB || teamB.map(() => null);
    addA = addA || teamA.map(() => null);
    addB = addB || teamB.map(() => null);
    inhA = inhA || teamA.map(() => null);
    inhB = inhB || teamB.map(() => null);
    const A = teamA.map((n, i) => Object.assign(new Unit(POOL[n], troopA, bsA[i], eqA[i], addA[i], inhA[i], seasonModsFor(POOL, POOL[n], i, teamA, scenario)), { nm: n, side: "我" }));
    const B = teamB.map((n, i) => Object.assign(new Unit(POOL[n], troopB, bsB[i], eqB[i], addB[i], inhB[i], seasonModsFor(POOL, POOL[n], i, teamB, scenario)), { nm: n, side: "敵" }));
    const setA = new Set(A);
    const alliesOf = u => setA.has(u) ? A : B, foesOf = u => setA.has(u) ? B : A;
    const bondsA = activeBonds(teamA), bondsB = activeBonds(teamB);
    const pt = eff => ({ effects: eff, kind: "phys" });
    const applyPassives = opt => {                  // 被動/指揮/兵書/裝備/緣分 統一套用
      for (const u of [...A, ...B]) {
        if (!u.alive) continue;
        for (const t of u.tactics)
          if (t.type === "passive" || t.type === "command")
            applyEffects(u, null, t, alliesOf(u), foesOf(u), opt);
        if (u.bs.length) applyEffects(u, null, pt(u.bs), alliesOf(u), foesOf(u), opt);
        if (u.eq.length) applyEffects(u, null, pt(u.eq), alliesOf(u), foesOf(u), opt);
      }
      for (const [team, bds] of [[A, bondsA], [B, bondsB]])
        if (team.length) for (const bd of bds)
          applyEffects(team[0], null, pt(bd.effects), team, foesOf(team[0]), opt);
    };
    applyPassives({ noHeal: true });
    if (TRACE) {                                    // 準備階段: 列出各將備戰後面板 + 套用系統
      CUR_R = 0;
      lg(`〔採用兵種〕我方 ${troopA}兵　·　敵方 ${troopB}兵`);
      for (const u of [...A, ...B]) {
        const sys = [u.tactics.map(t => t.nameZh).filter(Boolean).join("／") || "無戰法",
          u.bs.length ? "兵書" : "", u.eq.length ? "裝備" : ""].filter(Boolean).join("・");
        lg(`【${u.side}】${u.nm}　武${Math.round(u.eff("force"))} 智${Math.round(u.eff("intel"))} 統${Math.round(u.eff("command"))} 速${Math.round(u.eff("speed"))}　${sys}`);
      }
      for (const [team, bds] of [[A, bondsA], [B, bondsB]]) if (team.length && bds.length) lg(`【${team[0].side}】緣分發動: ${bds.map(b => b.name).join("、")}`);
    }

    for (let r = 1; r <= ROUNDS; r++) {
      CUR_R = r;
      for (const u of [...A, ...B]) if (u.alive && u.stack) u.stack.n = Math.min(u.stack.max, u.stack.n + 1);
      applyPassives({ healOnly: true });
      const order = [...A, ...B].filter(u => u.alive).sort((x, y) => y.eff("speed") - x.eff("speed"));
      for (const u of order) {
        if (!u.alive) continue;
        if (u.stun) { lg(`【${u.side}】${u.nm} 被控制，無法行動`); continue; }
        if (!pickTarget(foesOf(u))) break;
        for (const t of u.tactics) {                  // 自帶 + 傳承: 各自獨立附加發動
          let fire = false;
          if (t.type === "active" && (t.coef || t.effects.length) && !(t.prep && r === 1)) fire = rnd() < t.rate;
          else if (t.type === "command" && t.coef) fire = rnd() < CMD_TRIGGER;
          else if (t.type === "passive" && t.coef) fire = rnd() < t.rate * PASSIVE_TRIGGER;
          if (fire) {
            if (TRACE) lg(`【${u.side}】${u.nm} 發動戰法【${t.nameZh}】`);
            for (let i = 0; i < t.n; i++) { const v = pickTarget(foesOf(u)); if (v && t.coef) hit(u, v, t.coef, t.kind); }
            if (t.type === "active") applyEffects(u, pickTarget(foesOf(u)), t, alliesOf(u), foesOf(u));
          }
        }
        const tgt = pickTarget(foesOf(u));            // 普攻(常駐) + 連擊 + 突擊
        if (tgt) {
          if (TRACE) lg(`【${u.side}】${u.nm} 普通攻擊 → ${tgt.nm}`);
          hit(u, tgt, 1.0, "phys");
          for (let i = 0; i < extraCount(u.addbonus("extra")); i++) { const nt = pickTarget(foesOf(u)); if (nt) { if (TRACE) lg(`【${u.side}】${u.nm} 連擊 → ${nt.nm}`); hit(u, nt, 1.0, "phys"); } }
          for (const t of u.tactics) if (t.type === "charge" && rnd() < t.rate) { if (TRACE) lg(`【${u.side}】${u.nm} 突擊【${t.nameZh}】`); if (t.coef) hit(u, tgt, t.coef, t.kind); applyEffects(u, tgt, t, alliesOf(u), foesOf(u)); }
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

  function trace(POOL, teamA, teamB, troopA = null, troopB = null, bsA = null, bsB = null, eqA = null, eqB = null, addA = null, addB = null, inhA = null, inhB = null, scenario = null) {
    TRACE = []; CUR_R = 0;                           // 跑一場並記錄事件日誌
    const r = fight(POOL, teamA, troopA, teamB, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario);
    const log = TRACE; TRACE = null;
    return { ...r, log };
  }
  function simulate(POOL, teamA, teamB, n = 2000, troopA = null, troopB = null, bsA = null, bsB = null, eqA = null, eqB = null, addA = null, addB = null, inhA = null, inhB = null, scenario = null) {
    let a = 0, rs = 0;
    for (let i = 0; i < n; i++) { const r = fight(POOL, teamA, troopA, teamB, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario); if (r.winner === "A") a++; rs += r.rounds; }
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

  const API = { buildPool, simulate, trace, score, recommend, fight, teamTroop, aptPct, bestTroop, TROOPS,
    defaultBingshu, activeBonds, seasonModsFor, mainByCat: () => MAIN_BY_CAT, subByCat: () => SUB_BY_CAT, bingshu: () => BINGSHU,
    bonds: () => BONDS, equips: () => EQUIPS,
    setKnobs: (c, p) => { CMD_TRIGGER = c; PASSIVE_TRIGGER = p; } };
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  root.SGZ = API;
})(typeof globalThis !== "undefined" ? globalThis : this);
