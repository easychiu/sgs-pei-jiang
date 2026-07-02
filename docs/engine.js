// 三國志戰略版 配將引擎 — sgz.py 的 JS 移植(瀏覽器/node 通用)
// 兵種由「隊伍」決定; 各武將只有對該兵種的適性(S/A/B/C/D)決定屬性發揮。克制為隊伍兵種 vs 隊伍兵種。
// 原語: coef amp mitig stun heal stat dot settle extra redirect stack decay swap pierce counter
//       silence disarm insight first taunt shield dodge surehit rateup
"use strict";
(function (root) {
  const ROUNDS = 8, START_TROOP = 10000, MORALE = 100;
  const CITY = 20, FACTION = 1.10;                   // 城建滿(武智統速各+20) + 陣營滿(全屬性+10%), 雙方皆有
  const CAMP = 4;                                     // 兵種營: 戰報「弓兵營全屬性提升了4」→ 全屬性平加(獨立階段, 在陣營乘算之後), 雙方皆有
  // 「受X影響」屬性縮放旋鈕。輸入為戰鬥內即時素質 caster.eff(stat)(已含城建/陣營/適性/
  // 加點/賽季/戰鬥中buff, 典型值 250~400, 而非卡面裸值)。公式取社群拆解(巴哈姆特高等陣容
  // 戰法論/NGA數據貼): 屬性100=面板基準值(SCALE=1.0), 每+350點效果翻倍(v=450時SCALE=2.0)。
  // 仍是可調校準旋鈕, 之後有更多實測數據可再調整斜率/錨點。
  const SCALE = v => Math.max(0, 1 + (v - 100) / 350);
  const SCALE_CLAMP = 1.5;                            // amp/mitig 縮放後上限保護: |val| <= 1.5
  const scaleOf = (caster, scale) => scale === "charm" ? SCALE(caster.charm) : (scale ? SCALE(caster.eff(scale)) : 1);
  // 批7: 發動率類「受X影響」縮放 —— 獨立常數, 與上面 SCALE(每+350翻倍) 不是同一條曲線。
  // user 實測太平道法(黃巾/張角, docs/data/calibration_anchors.json → rate_scale): 智力484.6
  // 才翻倍(對比 SCALE 只要+350即450), 反解 c=0.002598(6組獨立點一致到小數第6位, 取0.0026)。
  // chargeup 尚無獨立實測, 暫共用同常數(假設同曲線, 待未來樣本校準)。
  const RATE_SCALE_C = 0.0026;
  const rateScaleOf = (caster, scale) => scale === "charm" ? 1 + (caster.charm - 100) * RATE_SCALE_C : (scale ? 1 + (caster.eff(scale) - 100) * RATE_SCALE_C : 1);
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

  const moraleMult = m => 0.007 * Math.min(m, 100) + 0.30;  // 士氣上限100(戰報: 士氣110.4傷害不變, 超過100按100算)
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
      const s = Math.round(team.reduce((a, n) => a + aptPct(POOL[n], t), 0) * 1e4) / 1e4; // 抹平浮點誤差, 平手時與 sgz.py 同取先序
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
    // 批10: 資料衛生防禦 —— 載入時掃描 |amp.val| > 3 的極端值並印警告(不擋), 供資料層儘早
    // 發現如「coef 誤重複灌入 amp.val」這類系統性錯誤(見批10 corrections 仲裁)。只警告,
    // 不修改資料本身(修正應在 tactics_parsed.json/corrections 層完成)。
    for (const t of tactics) {
      for (const e of (t.effects || [])) {
        if (e.k === "amp" && typeof e.val === "number" && Math.abs(e.val) > 3) {
          console.warn(`[tactics data] ${t.nameZh || "?"}: amp.val=${e.val} 超過 |3| 常見範圍, 疑似資料異常(如 coef 誤灌入 amp.val)`);
        }
      }
    }
    BINGSHU = {}; MAIN_BY_CAT = {}; SUB_BY_CAT = {}; BONDS = bonds || []; EQUIPS = {};
    for (const b of (bingshu || [])) {
      const key = b.category + "·" + b.name;        // 複合鍵(同名跨類別不撞)
      BINGSHU[key] = b;
      const m = b.type === "主兵書" ? MAIN_BY_CAT : SUB_BY_CAT;
      (m[b.category] = m[b.category] || []).push(key);
    }
    for (const e of (equips || [])) {
      EQUIPS[e.type + "·" + e.name] = e;             // 複合鍵(同名跨欄位不撞, 同兵書precedent)
      if (!(e.name in EQUIPS)) EQUIPS[e.name] = e;    // 純名稱 fallback(向後相容; 同名跨type時保留先出現者, 呼叫端應改用複合鍵)
    }
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
        charm: st["魅力"] ?? 60,                    // 魅力: 只供 scale:"charm" 查表, 不進戰鬥四維 eff()
        tacticName: raw.tactic || "—", tactic: raw.tactic ? (TAC[raw.tactic] || null) : null,
      };
    }
    return { POOL, TAC };
  }

  class Unit {
    constructor(g, ttype, bsName, eqName, add, inherit, season) {
      this.g = g; this.ttype = ttype; this.troop = START_TROOP; this.stun = 0;
      this.silence = 0; this.disarm = 0; this.insight = 0; this.first = 0;  // 控制細分: 計窮/繳械/洞察(免控) + 先攻(優先行動, 剩餘回合數)
      this.chaos = 0;                              // 批12 ModeF: 混亂(不鎖行動, 但普攻/單體主動戰法改為敵我不分隨機選目標), 剩餘回合數
      // 自帶 + 傳承; 自帶戰法(g.tactic)淺拷貝附加 native:true 旗標(供 rateup/chargeup 的 nativeOnly
      // 修飾判斷「這是不是自帶戰法」, 如太平道法只加成張角自帶的五雷轟頂)。淺拷貝而非直接改
      // TACTICS 共享物件, 避免多個武將共用同一戰法物件時互相污染(如兩人都自帶白眉)。
      this.tactics = (g.tactic ? [Object.assign({}, g.tactic, { native: true })] : []).concat((inherit || []).map(nm => TACTICS[nm]).filter(Boolean));
      const _bn = Array.isArray(bsName) ? bsName : (bsName ? [bsName] : []);
      this.bs = _bn.flatMap(nm => (BINGSHU[nm] && BINGSHU[nm].effects) || []);  // 兵書(主+副)合併; 缺 effects 欄降級空陣列(同 sgz.py .get)
      const _eq = Array.isArray(eqName) ? eqName : (eqName ? [eqName] : []);
      const _eqSeen = new Set();                      // 同名特技(跨type, 如四欄皆有的"無畏")遊戲規則只生效一件: 依基底名稱去重, 先出現者為準
      const _eqObjs = _eq.map(nm => EQUIPS[nm]).filter(Boolean).filter(e => !_eqSeen.has(e.name) && (_eqSeen.add(e.name), true));
      const _eqAll = _eqObjs.flatMap(e => (e.effects || []).map(eff => eff.when ? Object.assign({}, eff, { _eqNm: e.name }) : eff));   // 裝備(4欄)合併(已去重); nm 可為複合鍵"type·name"或純名稱(向後相容, 見 buildPool 註記); 帶 when 的效果淺拷貝附加 _eqNm(供 TRACE 標名), 不動原資料物件
      // 批8: 效果級回合窗(effect.when) —— 裝備效果不像戰法有獨立 when 欄(合併進 eq 陣列時已失去
      // 個別戰法邊界), 故 when 掛在「單條效果」本身(e.when, 非 t.when)。無 when 的效果照舊在準備
      // 階段(prep)一次性套用(this.eq); 帶 when 的效果分離到 delayedEq, 於回合迴圈開始時(與戰法
      // when 窗口同一時點)逐條檢查 roundOk 是否符合, 符合則一次性套用(whenFired 慣例, 用效果物件
      // 本身去重)。帶 rate 的額外擲骰(如赳螑 50%機率)。
      this.eq = _eqAll.filter(e => !e.when);
      this.delayedEq = _eqAll.filter(e => e.when);
      // 裝備 proc(普攻後觸發, 如 昭烈12%繳械/踩踏額外傷): 包成偽突擊(charge)戰法附加, 走既有 charge 觸發路徑(普攻後 rate 擲骰)。
      // 偽戰法不在戰法庫, 不參與同名戰法去重與 NONEQUIP 過濾; nameZh 預設「特技·名」供 TRACE 辨識。
      // proc:true 旗標 → 標記為「特技偽戰法」, 非真突擊戰法: 日後若加 chargeup(突擊發動率加成)原語, 必須排除 t.proc===true(user 明確指示: 特技不吃突擊加成, 例虎豹騎/三勢陣/經天緯地/陷陣突襲)。
      for (const e of _eqObjs) if (e.proc) this.tactics.push({ type: e.proc.type || "charge", rate: e.proc.rate ?? 1, coef: e.proc.coef || 0, kind: e.proc.kind || "phys", n: e.proc.n || 1, nMax: e.proc.nMax || 0, effects: e.proc.effects || [], nameZh: e.proc.nameZh || ("特技·" + e.name), prep: 0, when: null, proc: true });
      const a = add || {}, sm = season || {};      // 養成加值 + 賽季修正
      const apt = (sm.aptS ? 1.20 : aptPct(g, ttype)) + (sm.aptAdd || 0);
      const scm = sm.mult || 1.0, flat = sm.flat || 0;
      // 屬性管線(戰報結算順序 準備→士氣→適性→建築→裝備→戰法): (基礎+加點+賽季flat)×適性×賽季乘 → +城建CITY → ×陣營FACTION → +兵種營CAMP
      // (裝備 stat "add" 平加效果由 applyEffects/prep 於本管線之後套用, 見 eff() 的 statAdds; 戰法 mult buff 又在其後, 見 eff() 的 mods)
      const pipe = (base, alloc) => ((base + (alloc || 0) + flat) * apt * scm + CITY) * FACTION + CAMP;
      this.force = pipe(g.base.force, a.force); this.intel = pipe(g.base.intel, a.intel);
      this.command = pipe(g.base.command, a.command); this.speed = pipe(g.base.speed, a.speed);
      this.charm = g.charm || 60;                  // 魅力: 城建/陣營是否加成不明, 保守用裸值不縮放(供 scale:"charm" 查表)
      this.mods = []; this.adds = []; this.dots = []; this.statAdds = [];  // statAdds: 屬性平加(裝備 stat.add, [stat, add, dur, src]); 在 eff() 中於 mods 乘算前先加
      if (a.amp) this.adds.push(["amp", a.amp, 9999]);    // 進階/典藏 攻防加成
      if (a.mitig) this.adds.push(["mitig", a.mitig, 9999]);
      this.settle = null; this.guardian = null; this.guardShare = 0; this.guardDur = 0; this.guardNormalOnly = false;  // guardDur: 代承剩餘回合, 歸零清 guardian; guardNormalOnly: 只代承普攻傷害(如 援助), 戰法傷害不轉移
      this.stack = null; this.decay = null; this.swap = 0; this.counter = null;
      this.tauntBy = null; this.tauntDur = 0;      // 嘲諷: 被嘲諷時強制普攻/單體戰法指向 tauntBy, 剩餘回合
      this.shield = null;                          // 護盾: {amt, dur} 吸收固定量傷害, 先於兵力扣減
      this.dodgeProb = 0; this.dodgeDur = 0;        // 規避: 機率完全迴避一次傷害
      this.surehitDur = 0;                          // 必中: 無視對方 dodge
      this.healblock = 0;                           // 批8: 禁療(healblock) 剩餘回合, >0 時 heal 效果對其無效
      this.whenFired = new Set();                   // 條件觸發(when.rounds/from/until) 已套用效果的戰法(一次性), 依戰法物件去重; 批8: delayedEq(裝備效果級when)共用同一個 Set(效果物件本身去重, 不與戰法物件撞)
      this.hitFlags = new Set();                    // 反應式觸發(when.on) 本回合已觸發的戰法, 每回合重置(防無限鏈)
      this.onHitTacs = this.tactics.filter(t => (t.type === "passive" || t.type === "command") && t.when && t.when.on);  // 預篩: 絕大多數單位為空, hit 熱路徑 O(0)
      this.lockedTargets = new Map();               // 批12 ModeG: lockTarget:true 戰法的鎖定目標, 鍵=戰法物件本身(同一戰法物件跨回合重用同一 Map)
    }
    get alive() { return this.troop > 0; }
    eff(stat) {
      if (this.swap && (stat === "force" || stat === "intel")) stat = stat === "force" ? "intel" : "force";
      let v = this[stat];
      for (const [s, add] of this.statAdds) if (s === stat || s === "all") v += add;  // 裝備平加(獨立階段, 在陣營/兵種營後、戰法乘算前)
      for (const [s, m] of this.mods) if (s === stat || s === "all") v *= m;
      return v;
    }
    addbonus(kind) { let s = 0; for (const a of this.adds) if (a[0] === kind) s += a[1]; return s; }
    // rateup/chargeup 專用: 依戰法 t 的 prep/native 屬性, 只加總「修飾旗標吻合」的 adds 項。
    // adds[4] = flags({prepOnly,nativeOnly,inheritedOnly}|undefined, 見 pushAdd)。無旗標
    // (undefined/{}) 的加成一律計入(如虎豹騎的 chargeup 沒有 prepOnly/nativeOnly 限制)。
    // 批8: inheritedOnly(nativeOnly 反向) —— 只加「非自帶」(傳承)戰法, 如竭力佐謀「非自帶
    // 主動戰法發動率+100%」; !t.native 即傳承(Unit 建構時自帶戰法才標 native:true)。
    addbonusFor(kind, t) {
      let s = 0;
      for (const a of this.adds) {
        if (a[0] !== kind) continue;
        const f = a[4];
        if (f && f.prepOnly && !t.prep) continue;
        if (f && f.nativeOnly && !t.native) continue;
        if (f && f.inheritedOnly && t.native) continue;
        s += a[1];
      }
      return s;
    }
    // 同來源(戰法名)同種效果 刷新而非疊加: push 前先移除同 kind(或 stat) + 同 src 的舊項。
    // src=null/undefined(兵書/裝備/緣分, 開戰只套一次) 不做去重, 維持原行為。
    pushAdd(kind, val, dur, src, flags) {
      if (src) this.adds = this.adds.filter(a => !(a[0] === kind && a[3] === src));
      this.adds.push([kind, val, dur, src, flags]);
    }
    pushMod(stat, mult, dur, src) {
      if (src) this.mods = this.mods.filter(m => !(m[0] === stat && m[3] === src));
      this.mods.push([stat, mult, dur, src]);
    }
    pushStatAdd(stat, add, dur, src) {                 // 屬性平加(裝備 stat.add): 同 pushMod 慣例, 同來源刷新不疊
      if (src) this.statAdds = this.statAdds.filter(a => !(a[0] === stat && a[3] === src));
      this.statAdds.push([stat, add, dur, src]);
    }
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
      this.statAdds = this.statAdds.filter(a => --a[2] > 0);   // 裝備平加到期移除(如 疾馳 speed+25 dur:2)
      this.stun = Math.max(0, this.stun - 1);
      this.silence = Math.max(0, this.silence - 1);
      this.disarm = Math.max(0, this.disarm - 1);
      this.chaos = Math.max(0, this.chaos - 1);      // 批12 ModeF: 混亂 逐回合遞減
      this.insight = Math.max(0, this.insight - 1);
      this.first = Math.max(0, this.first - 1);       // 先攻: 逐回合遞減(dur=N 覆蓋前 N 回合, 如「戰鬥前3回合」)
      this.healblock = Math.max(0, this.healblock - 1);  // 批8: 禁療 逐回合遞減
      this.swap = Math.max(0, this.swap - 1);
      if (this.decay && --this.decay.left <= 0) this.decay = null;
      this.tauntDur = Math.max(0, this.tauntDur - 1);
      if (this.tauntDur <= 0) this.tauntBy = null;
      if (this.guardDur) { this.guardDur = Math.max(0, this.guardDur - 1); if (this.guardDur <= 0) { this.guardian = null; this.guardShare = 0; this.guardNormalOnly = false; } }  // 代承到期: 清 guardian(如 援助 首回合援護 dur:1)
      this.dodgeDur = Math.max(0, this.dodgeDur - 1);
      if (this.dodgeDur <= 0) this.dodgeProb = 0;
      this.surehitDur = Math.max(0, this.surehitDur - 1);
      if (this.shield && --this.shield.dur <= 0) this.shield = null;
      this.hitFlags.clear();                           // 受擊觸發(when.on) 每回合各戰法重置一次觸發額度
    }
  }

  // 傷害公式旋鈕(批3 重塑): 社群拆解(知乎菜頭50級傷害模型 + B站櫻謀詭計錨點), 用實測錨點反解常數。
  // 錨點(兵10000/coef1.0/士氣100/無增減傷, moraleMult(100)=1.0 已併入取樣, 取隨機帶中值1.0):
  //   錨1 屬性差0   → 實測 ≈476 傷害 ⇒ DMG_A = 476/sqrt(10000) = 4.76
  //   錨2 屬性差200 → 實測 ≈764 傷害 ⇒ DMG_B = (764-476)/200 = 1.44
  //   錨3 屬性差大負值(保底) → 實測 ≈90  傷害 ⇒ DMG_FLOOR = 90/sqrt(10000) = 0.9
  // 之後有更多實測數據(不同兵力/等級)可再校準, 目前僅50級單一等級係數樣本, 折入常數中。
  const DMG_A = 4.76, DMG_B = 1.44, DMG_FLOOR = 0.9;
  function damage(src, dst, coef, kind, srcTroop) {
    const troop = srcTroop == null ? src.troop : srcTroop;
    const atk = kind === "intel" ? src.eff("intel") : src.eff("force");
    const def = kind === "intel" ? dst.eff("intel") : dst.eff("command");
    const troopSqrt = Math.sqrt(Math.max(0, troop));
    let base = Math.max(DMG_A * troopSqrt + DMG_B * (atk - def), DMG_FLOOR * troopSqrt) * coef;
    base *= counterMult(src.ttype, dst.ttype);     // 克制: 隊伍兵種 vs 隊伍兵種
    base *= moraleMult(MORALE);
    base *= Math.max(0, 1 + src.amp());
    const mit = dst.addbonus("mitig") * (1 - Math.min(1, src.addbonus("pierce")));
    base *= Math.max(0.1, 1 - mit);
    base *= 0.96 + rnd() * 0.08;   // 隨機帶 0.96~1.04(對稱): rnd()*0.08 涵蓋 [0,0.08), 起點0.96 → 上限0.96+0.08=1.04
    return Math.max(0, base);
  }
  function hit(src, dst, coef, kind, isNormal, onEvent) {
    if (!src.surehitDur && dst.dodgeDur && rnd() < dst.dodgeProb) {  // 規避: 完全迴避一次傷害(必中無視)
      if (TRACE) lg(`　→ ${dst.nm} 規避了攻擊`);
      if (onEvent) onEvent(dst, src, isNormal);
      return;
    }
    let dmg = damage(src, dst, coef, kind);
    if (dst.shield && dst.shield.amt > 0) {                        // 護盾: 先於兵力扣減吸收傷害
      const absorb = Math.min(dst.shield.amt, dmg);
      dst.shield.amt -= absorb; dmg -= absorb;
      if (TRACE && absorb > 0) lg(`　▸ ${dst.nm} 護盾吸收 ${Math.round(absorb)}` + (dst.shield.amt <= 0 ? "（已破盾）" : ""));
      if (dst.shield.amt <= 0) dst.shield = null;
    }
    const g = dst.guardian;
    if (g && g.alive && g !== dst && !(dst.guardNormalOnly && !isNormal)) { g.troop -= dmg * dst.guardShare; dst.troop -= dmg * (1 - dst.guardShare); }  // normalOnly 援護: 戰法傷害(isNormal=false)不轉移
    else dst.troop -= dmg;
    if (TRACE) lg(`　→ ${dst.nm} 損兵 ${Math.round(dmg)}，剩餘 ${Math.max(0, Math.round(dst.troop))}` + (dst.troop <= 0 ? " 【擊破】" : ""));
    if (dst.settle) dst.settle.layers = Math.min(dst.settle.max, dst.settle.layers + 1);
    const ls = src.addbonus("lifesteal");                            // 批8: 倒戈 —— 造成傷害時按比例回復自身兵力(以本次造成的傷害量 dmg 為基準), 上限 START_TROOP
    if (ls > 0 && src.alive) {
      const before = src.troop;
      src.troop = Math.min(START_TROOP, src.troop + dmg * ls);
      if (TRACE && src.troop - before >= 1) lg(`　▸ ${src.nm} 倒戈回復 +${Math.round(src.troop - before)}`);
    }
    if (onEvent) onEvent(dst, src, isNormal);
    const c = dst.counter;
    if (c && dst.alive && src.alive && rnd() < (c.prob ?? 1)) {
      const cd = damage(dst, src, c.coef ?? 1, c.kind || "phys"); src.troop -= cd;
      if (TRACE) lg(`　↩ ${dst.nm} 反擊 ${src.nm} 損兵 ${Math.round(cd)}，剩餘 ${Math.max(0, Math.round(src.troop))}`);
    }
  }
  function roundOk(t, r) {                          // 條件觸發(when): 回合是否符合戰法的發動窗口
    const w = t.when;
    if (!w) return true;
    if (w.rounds) return w.rounds.includes(r);
    if (w.from != null && r < w.from) return false;
    if (w.until != null && r > w.until) return false;
    return true;
  }
  function extraCount(ex) { const i = Math.floor(ex); return i + (rnd() < ex - i ? 1 : 0); }
  function pickTarget(units, attacker) {            // 普攻/單體戰法: 隨機挑一個存活敵軍(不再固定打兵力最高); 嘲諷: 攻擊者身上有 tauntBy 時強制指向該目標
    if (attacker && attacker.tauntDur && attacker.tauntBy && attacker.tauntBy.alive && units.includes(attacker.tauntBy)) return attacker.tauntBy;
    const live = units.filter(u => u.alive);
    return live.length ? live[Math.floor(rnd() * live.length)] : null;
  }
  // 批12 ModeF: 混亂(chaos)單體選標 —— 普攻/單體主動戰法目標選擇改為「敵我不分」: 從友軍+敵軍
  // (排除自己)中隨機挑一個存活目標, 而非只從敵方挑。非混亂狀態時退回一般 pickTarget(含嘲諷判定)。
  // 群體/AoE 戰法在混亂下維持原邏輯不變(近似, 見呼叫端註解)。
  function pickTargetChaos(u, allies, foes) {
    if (!u.chaos) return pickTarget(foes, u);
    const pool = allies.concat(foes).filter(x => x.alive && x !== u);
    if (!pool.length) return pickTarget(foes, u);   // 保底: 沒有其他存活單位(理論上不會發生, 至少u自己還在foes/allies之外)時退回一般選標
    const v = pool[Math.floor(rnd() * pool.length)];
    if (TRACE && allies.includes(v)) lg(`　▸ ${u.nm} 混亂誤擊友軍 → ${v.nm}`);
    return v;
  }
  function pickTargets(units, n) {                  // 群體戰法: 隨機挑 n 個不重複存活目標
    const live = units.filter(u => u.alive);
    if (live.length <= n) return live;
    const pool = live.slice(), out = [];
    for (let i = 0; i < n && pool.length; i++) { const idx = Math.floor(rnd() * pool.length); out.push(pool[idx]); pool.splice(idx, 1); }
    return out;
  }
  // 批12 ModeG: lockTarget —— 戰法首次發動時透過 pickTarget 正常選標, 之後每次發動重用同一目標
  // (以戰法物件本身為鍵存進 caster.lockedTargets), 而非每次重新隨機選。若鎖定目標已陣亡: 依 brief
  // 保守決策(來源文字未說明死亡後是否重新鎖定), 視為「本次發動找不到有效目標」回傳 null, 不重新選
  // 新目標(不做隱式重新鎖定, 避免無根據臆測遊戲行為)。
  function resolveLockedTarget(u, t, foes) {
    if (u.lockedTargets.has(t)) {
      const locked = u.lockedTargets.get(t);
      return (locked && locked.alive) ? locked : null;  // 鎖定目標已陣亡 -> 本次無有效目標(不重新選)
    }
    const picked = pickTarget(foes, u);
    if (picked) u.lockedTargets.set(t, picked);
    return picked;
  }

  // 批13: extraHits —— 多段傷害(兵刃+謀略雙段/主傷+補刀等單一 coef/kind/n 無法表達的戰法)。
  // 戰法欄 "extraHits":[{coef,kind,n,nMax,rate,who,_note}]: 主 coef 結算後逐段獨立處理,
  // 每段各自 rate 擲骰(預設1必發)、選目標、hit()。who 可選: "sameTarget"(沿用主 coef 段已
  // 選定的(單體)目標, 如屠几上肉 兵刃+謀略同目標/一騎當千 主將加成同目標)、"enemyLeader"
  // (固定打敵方主將 foes[0], 如百騎劫營/暗藏玄機 額外段明確打敵軍主將)、不填則預設
  // pickTargets(敵方, 依 n/nMax)。與主 coef 段完全獨立(各自的 kind 可不同, 如兵刃主傷+謀略
  // 補刀), 不與 hitsRepeat/lockTarget 互斥(hitsRepeat/lockTarget 只影響主 coef 段的選標方式,
  // extraHits 段固定用上述規則)。
  function fireExtraHits(u, t, tgt, alliesOf, foesOf, onHit) {
    if (!t.extraHits) return;
    for (const eh of t.extraHits) {
      if (rnd() >= (eh.rate ?? 1)) continue;
      const n = eh.n || 1;
      const cnt = eh.nMax ? n + Math.floor(rnd() * (eh.nMax - n + 1)) : n;
      let dests;
      if (eh.who === "sameTarget") dests = tgt && tgt.alive ? [tgt] : [];        // 沿用主段已選定的(單體)目標
      else if (eh.who === "enemyLeader") { const fl = foesOf(u)[0]; dests = (fl && fl.alive) ? [fl] : []; }  // 固定打敵方主將(index 0)
      else if (cnt <= 1 && tgt && tgt.alive && !eh.who) dests = [tgt];   // 未指定 who 且單體: 沿用主段目標(向後相容預設行為)
      else dests = pickTargets(foesOf(u), cnt);
      if (TRACE && dests.length) lg(`　▸ ${t.nameZh || "?"}〔額外段〕${eh.kind === "intel" ? "謀略" : "兵刃"}傷害` + (eh._note ? `（${eh._note}）` : ""));
      for (const v of dests) hit(u, v, eh.coef, eh.kind || "phys", false, onHit);
    }
  }

  const STAT_ZH = { force: "武力", intel: "智力", command: "統率", speed: "速度", all: "全屬性", charm: "魅力" };
  function effDesc(k, e, caster) {                  // 把15原語效果翻成可讀中文(供日誌); caster 供 scale 縮放後實際值顯示
    const p = v => Math.round(Math.abs(v) * 100) + "%";
    const d = e.dur && e.dur < 90 ? `(${e.dur}回合)` : "";
    const sfx = e.scale && caster ? `〔受${STAT_ZH[e.scale] || e.scale}影響, ×${scaleOf(caster, e.scale).toFixed(2)}〕` : "";
    const val = (e.scale && caster) ? Math.max(-SCALE_CLAMP, Math.min(SCALE_CLAMP, e.val * scaleOf(caster, e.scale))) : e.val;
    const mult = (e.scale && caster) ? 1 + ((e.mult ?? 1) - 1) * scaleOf(caster, e.scale) : e.mult;
    switch (k) {
      case "amp": return (e.who === "enemy" && val > 0 ? `易傷+${p(val)}${d}` : (val >= 0 ? `增傷+${p(val)}${d}` : `減傷${p(val)}${d}`)) + sfx;
      case "mitig": return (val >= 0 ? `減傷+${p(val)}${d}` : `易傷+${p(val)}${d}`) + sfx;
      case "stun": return `震懾·全禁${d || "(1回合)"}`;
      case "silence": return `計窮·禁主動戰法${d || "(1回合)"}`;
      case "disarm": return `繳械·禁普攻${d || "(1回合)"}`;
      case "chaos": return `混亂(敵我不分)${d || "(1回合)"}`;
      case "insight": return `洞察·免疫控制${d || "(1回合)"}`;
      case "first": return "先攻·優先行動";
      case "stat": return e.add != null ? `${STAT_ZH[e.stat] || e.stat} +${(e.scale && caster ? e.add * scaleOf(caster, e.scale) : e.add)}${d}${sfx}` : `${STAT_ZH[e.stat] || e.stat} ×${mult.toFixed(2)}${d}${sfx}`;
      case "dot": return `持續傷害${d}`;
      case "extra": return `額外攻擊+${e.val}`;
      case "stack": return "疊加增傷";
      case "decay": return "遞減增傷(開場高)";
      case "swap": return `武智互換${d}`;
      case "pierce": return "無視減傷";
      case "counter": return "反擊";
      case "redirect": return `代承傷害(分擔${Math.round((e.share ?? 0.3) * 100)}%)`;
      case "settle": return "猛毒·結算傷害";
      case "heal": return "治療";
      case "taunt": return `嘲諷·強制指向施放者${d || "(1回合)"}`;
      case "shield": return `護盾${e.amt ? "+" + Math.round(e.amt) : ""}${e.pct ? "(相當於" + p(e.pct) + "兵力)" : ""}${d}`;
      case "dodge": return `規避${p(e.prob ?? 0)}${d}`;
      case "surehit": return `必中·無視規避${d}`;
      case "healblock": return `禁療·無法被治療${d || "(1回合)"}`;
      case "lifesteal": return `倒戈·造成傷害回復${p(val)}${d}` + sfx;
      // rateup/chargeup 的 scale 用獨立的 RATE_SCALE_C(非上面 amp/mitig/stat 共用的 scaleOf/SCALE),
      // 故不沿用外層算好的 val/sfx, 另外用 rateScaleOf 算實際值(批7)。
      case "rateup": case "chargeup": {
        const rsfx = e.scale && caster ? `〔受${STAT_ZH[e.scale] || e.scale}影響〕` : "";
        const rv = e.scale && caster ? e.val * rateScaleOf(caster, e.scale) : e.val;
        const label = k === "rateup" ? "主動戰法發動機率" : "突擊發動機率";
        return `${label}+${p(rv)}${d}${rsfx}`;
      }
      default: return k;
    }
  }
  function applyEffects(caster, tgt, t, allies, enemies, opt) {
    opt = opt || {};
    const src = t.nameZh || null;                     // 效果來源標籤: 戰法名(兵書/裝備/緣分無 nameZh → null, 不去重)
    for (const e of t.effects) {
      const k = e.k;
      if (opt.healOnly && k !== "heal") continue;
      if (k === "heal") {
        if (opt.noHeal) continue;
        if ((e.coef ?? 0.8) < 0) continue;              // 批10: 資料衛生防禦 —— 負 heal coef(如機略縱橫類 dot 誤標成 heal 負值)一律視為0並跳過, 避免資料錯誤反而扣友軍血
        let hurt = null;
        for (const a of allies) if (a.alive && !a.healblock && (!hurt || a.troop < hurt.troop)) hurt = a;  // 批8: 禁療(healblock) 中的目標跳過, 不參與「最殘一人」篩選
        if (hurt) {
          const before = hurt.troop;
          const hcoef = (e.coef ?? 0.8) * (e.scale ? scaleOf(caster, e.scale) : 1);
          hurt.troop = Math.min(START_TROOP, hurt.troop + hcoef * caster.troop * 0.10);
          if (TRACE && hurt.troop - before >= 1) lg(`　▸ 治療 ${hurt.nm} +${Math.round(hurt.troop - before)}` + (e.scale ? `（受${STAT_ZH[e.scale] || e.scale}影響, 實際治療率${Math.round(hcoef * 100)}%）` : ""));
        }
        continue;
      }
      if (k === "settle") {
        let tg = null;
        for (const x of enemies) if (x.alive && (!tg || x.eff("command") > tg.eff("command"))) tg = x;
        if (tg) { tg.settle = { layers: e.init ?? 1, max: e.max ?? 3, left: e.dur ?? 2, caster, snap: caster.troop, base: e.base ?? 1.5, per: e.per ?? 0.4, kind: t.kind || "intel" }; if (TRACE) lg(`　▸ 猛毒·結算傷害 → ${tg.nm}`); }
        continue;
      }
      if (k === "redirect") {
        let guard = caster;
        if (e.guard === "max_force") { for (const a of allies) if (a.alive && (guard === caster || a.eff("force") > guard.eff("force"))) guard = a; }
        for (const a of allies) if (a.alive && a !== guard) { a.guardian = guard; a.guardShare = e.share ?? 0.3; a.guardDur = e.dur ?? 99; a.guardNormalOnly = !!e.normalOnly; }  // 讀 e.dur(預設99=近似全程, 向後相容) + e.normalOnly(只代承普攻); 到期由 tick 清除
        if (TRACE) lg(`　▸ ${guard.nm} 代承友軍傷害(分擔${Math.round((e.share ?? 0.3) * 100)}%${e.dur && e.dur < 90 ? `, ${e.dur}回合` : ""})`);
        continue;
      }
      const who = e.who || "ally";
      const CTRL_K = k === "stun" || k === "silence" || k === "disarm" || k === "taunt" || k === "chaos";  // 控制/嘲諷類: 按戰法 n/nMax 選目標數(insight 不擋嘲諷, 只擋 stun/silence/disarm/chaos)
      let dests;
      if (who === "self") dests = caster.alive ? [caster] : [];
      else if (who === "leader") dests = (allies[0] && allies[0].alive) ? [allies[0]] : [];  // 批8: 主將限定(隊伍 index 0)
      else if (who === "subs") dests = allies.slice(1).filter(a => a.alive);  // 批13: 副將群限定(隊伍 index 0 以外; 如鋒矢陣/箕形陣副將分化段)
      else if (who === "enemy") {
        if (CTRL_K) {                                 // 群體控制(n>1 或有 nMax)隨機挑不重複目標; 單體優先鎖定 tgt
          const n = t.n || 1;
          const cnt = t.nMax ? n + Math.floor(rnd() * (t.nMax - n + 1)) : n;
          dests = cnt <= 1 ? (tgt && tgt.alive ? [tgt] : pickTargets(enemies, 1)) : pickTargets(enemies, cnt);
        } else dests = enemies.filter(x => x.alive);
      }
      else dests = allies.filter(a => a.alive);
      if (TRACE && dests.length) lg(`　▸ ${effDesc(k, e, caster)} → ${dests.map(u => u.nm).join("、")}`);
      // scale:"intel"|"force"|"command"|"speed"|"charm" 縮放(以施放者戰鬥內即時素質為準):
      // amp/mitig 的 val 直接乘 SCALE, clamp 到 ±SCALE_CLAMP 防止極端值; stat 的 mult 對
      // 1.0 的偏移量(增益/削弱幅度)乘 SCALE, 1.0 本身(無效果)不受縮放影響。
      const svVal = v => e.scale ? Math.max(-SCALE_CLAMP, Math.min(SCALE_CLAMP, v * scaleOf(caster, e.scale))) : v;
      const svMult = m => e.scale ? 1 + (m - 1) * scaleOf(caster, e.scale) : m;
      const svAdd = a => e.scale ? a * scaleOf(caster, e.scale) : a;  // 屬性平加縮放(如未來 scale 平加); 一般裝備平加無 scale 直接用原值
      for (const u of dests) {
        if (k === "amp") { const v = svVal(e.val); who === "enemy" && v > 0 ? u.pushAdd("mitig", -v, e.dur, src) : u.pushAdd("amp", v, e.dur, src); }
        else if (k === "mitig") u.pushAdd("mitig", svVal(e.val), e.dur, src);
        else if (k === "stun") { if (!u.insight) { u.stun = Math.max(u.stun, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入震懾(全禁)`); } else if (TRACE) lg(`　▸ ${u.nm} 洞察免疫震懾`); }
        else if (k === "silence") { if (!u.insight) { u.silence = Math.max(u.silence, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入計窮(禁主動戰法)`); } else if (TRACE) lg(`　▸ ${u.nm} 洞察免疫計窮`); }
        else if (k === "disarm") { if (!u.insight) { u.disarm = Math.max(u.disarm, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入繳械(禁普攻)`); } else if (TRACE) lg(`　▸ ${u.nm} 洞察免疫繳械`); }
        else if (k === "chaos") { if (!u.insight) { u.chaos = Math.max(u.chaos, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入混亂(敵我不分)`); } else if (TRACE) lg(`　▸ ${u.nm} 洞察免疫混亂`); }  // 批12 ModeF
        else if (k === "insight") { u.insight = Math.max(u.insight, (e.dur ?? 1) + 1); u.stun = 0; u.silence = 0; u.disarm = 0; u.chaos = 0; }
        else if (k === "first") u.first = Math.max(u.first, e.dur ?? 1);
        else if (k === "stat") { if (e.add != null) u.pushStatAdd(e.stat, svAdd(e.add), e.dur, src); else u.pushMod(e.stat, svMult(e.mult ?? 1), e.dur, src); }  // 裝備平加(add)與乘算(mult)擇一; add 為戰報所示「裝備獨立平加階段」
        else if (k === "dot") u.dots.push([damage(caster, u, e.coef ?? 0.5, t.kind || "intel"), e.dur]);
        else if (k === "extra") u.pushAdd("extra", e.val, e.dur, src);
        else if (k === "stack") u.stack = { per: e.per ?? 0.1, max: e.max ?? 5, n: 0 };
        else if (k === "decay") u.decay = { v0: e.v0 ?? 0.5, left: e.rounds ?? 8, total: e.rounds ?? 8 };
        else if (k === "swap") u.swap = Math.max(u.swap, (e.dur ?? 1) + 1);
        else if (k === "pierce") u.pushAdd("pierce", e.val, e.dur, src);
        else if (k === "counter") u.counter = { coef: e.coef ?? 1, kind: e.kind || "phys", prob: e.prob ?? 1 };
        else if (k === "taunt") { u.tauntBy = caster; u.tauntDur = Math.max(u.tauntDur, (e.dur ?? 1) + 1); }
        else if (k === "shield") {
          const amt = (e.amt ?? 0) + (e.pct ? e.pct * caster.troop : 0);
          u.shield = { amt: (u.shield ? u.shield.amt : 0) + amt, dur: (e.dur ?? 99) + 1 };  // +1 補償: tick 施加當回合末即扣1, 與 taunt/dodge/surehit 慣例一致
        }
        else if (k === "dodge") { u.dodgeProb = e.prob ?? 0.2; u.dodgeDur = Math.max(u.dodgeDur, (e.dur ?? 1) + 1); }
        else if (k === "surehit") u.surehitDur = Math.max(u.surehitDur, (e.dur ?? 1) + 1);
        else if (k === "healblock") u.healblock = Math.max(u.healblock, (e.dur ?? 1) + 1);  // 批8: 禁療 —— heal 套用處(applyEffects 開頭)已排除 healblock 中的目標
        else if (k === "lifesteal") u.pushAdd("lifesteal", e.val, e.dur, src);  // 批8: 倒戈 —— 實際回血在 hit() 結算傷害後(見 hit() 內 lifesteal 段), 這裡只掛加成值
        else if (k === "rateup") {                       // 提高(自身或對象)主動戰法發動機率
          // scale: 施放當下(caster 戰鬥內即時素質)用 RATE_SCALE_C(獨立於全域 SCALE) 縮放實際加成
          // (批7: 太平道法「受智力影響」, 見 docs/data/calibration_anchors.json → rate_scale)。
          // prepOnly/nativeOnly/inheritedOnly(批8, nativeOnly反向) 修飾旗標存進 adds[4], 由
          // addbonusFor() 在主動擲骰處依戰法屬性篩選加總。
          const rv = e.scale ? e.val * rateScaleOf(caster, e.scale) : e.val;
          const flags = (e.prepOnly || e.nativeOnly || e.inheritedOnly) ? { prepOnly: !!e.prepOnly, nativeOnly: !!e.nativeOnly, inheritedOnly: !!e.inheritedOnly } : undefined;
          // 同一戰法(如太平道法)可能有多條 rateup(一般 + prepOnly 額外), src 相同的話 pushAdd
          // 的「同kind+同src刷新」去重會把前一條蓋掉; 用 flags 組出不同的 dedup key 尾碼區分,
          // 讓語意不同的兩條並存, 但同語意(同flags組合)的仍正常刷新不疊加。
          const rSrc = (src && flags) ? src + ":" + ["prepOnly", "nativeOnly", "inheritedOnly"].filter(f => flags[f]).join("") : src;
          u.pushAdd("rateup", rv, e.dur, rSrc, flags);
        }
        else if (k === "chargeup") {                    // 提高(自身或對象)突擊戰法發動機率; 排除 t.proc===true 特技偽戰法見突擊擲骰處註解
          // chargeup 同樣支援 scale(未有實測前與 rateup 共用 RATE_SCALE_C, 假設同曲線, 見上方常數註解)
          const cv = e.scale ? e.val * rateScaleOf(caster, e.scale) : e.val;
          const cflags = (e.prepOnly || e.nativeOnly) ? { prepOnly: !!e.prepOnly, nativeOnly: !!e.nativeOnly } : undefined;
          const cSrc = (src && cflags) ? src + ":" + ["prepOnly", "nativeOnly"].filter(f => cflags[f]).join("") : src;
          u.pushAdd("chargeup", cv, e.dur, cSrc, cflags);
          // 曹純特例(虎豹騎): 若隊伍主將(index 0, allies[0])===本效果指定 general 且恰為本 u,
          // 額外發動機率受武力影響。二次曲線 extra% = force^2 * k(注意 k 擬合的是「%數值」本身,
          // 如 force=373.83 時 force^2*k≈4.47, 代表 4.47%, 需 /100 換算成 addbonus 用的小數比例),
          // 錨點見 docs/data/calibration_anchors.json → hubaoqi_caochun(user 實測: 武力373.83→額外
          // 4.46%, 145.78→0.63%, 123.78→0.53%)。src 另加尾碼避免 pushAdd 同 kind+src 去重把兩筆效果互相蓋掉。
          if (e.leaderBonus && allies[0] === u && u.g.name === e.leaderBonus.general) {
            const lb = e.leaderBonus;
            const extra = Math.pow(u.eff("force"), 2) * lb.k / 100;
            u.pushAdd("chargeup", extra, e.dur, src ? src + ":leaderBonus" : "leaderBonus");
            if (TRACE) lg(`　▸ ${u.nm}〔${lb.general}統領〕突擊發動機率額外+${Math.round(extra * 1000) / 10}%〔受武力影響, 武${Math.round(u.eff("force"))}〕`);
          }
        }
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
    const CAT_ORDER = ["PASSIVE", "FORMATION", "TROOP", "COMMAND"];   // 準備階段嚴格順序: 被動→陣法→兵種→指揮
    const CAT_LABEL = { PASSIVE: "被動", FORMATION: "陣法", TROOP: "兵種", COMMAND: "指揮" };
    const catOf = t => CAT_ORDER.includes(t.cat) ? t.cat : "COMMAND";
    const applyPassives = opt => {                  // 被動/陣法/兵種/指揮(依序) + 兵書/裝備/緣分
      for (const cat of CAT_ORDER)
        for (const u of [...A, ...B]) {
          if (!u.alive) continue;
          for (const t of u.tactics)            // 同將多個同類: 戰法格順序(陣列順序)決定先後
            if ((t.type === "passive" || t.type === "command") && catOf(t) === cat) {
              if (t.when && !opt.healOnly) continue;  // 條件觸發(when): 不在準備階段套用, 改由回合迴圈的 applyWhenTactics 在符合回合時套用
              if (TRACE && opt.prep) lg(`【${u.side}】${u.nm}〔${CAT_LABEL[cat]}〕${t.nameZh}`);
              applyEffects(u, null, t, alliesOf(u), foesOf(u), opt);
            }
        }
      for (const u of [...A, ...B]) {
        if (!u.alive) continue;
        if (u.bs.length) { if (TRACE && opt.prep) lg(`【${u.side}】${u.nm}〔兵書〕`); applyEffects(u, null, pt(u.bs), alliesOf(u), foesOf(u), opt); }
        if (u.eq.length) { if (TRACE && opt.prep) lg(`【${u.side}】${u.nm}〔裝備〕`); applyEffects(u, null, pt(u.eq), alliesOf(u), foesOf(u), opt); }
      }
      for (const [team, bds] of [[A, bondsA], [B, bondsB]])
        if (team.length) for (const bd of bds) {
          if (TRACE && opt.prep) lg(`【${team[0].side}】〔緣分〕${bd.name}`);
          applyEffects(team[0], null, pt(bd.effects), team, foesOf(team[0]), opt);
        }
    };
    const onHit = (dst, src, isNormal) => {          // 反應式觸發(when.on): 被普攻(attacked)/受任意傷害(damaged) 時掛到 hit() 事件點
      if (!dst.alive || !dst.onHitTacs.length) return;
      for (const t of dst.onHitTacs) {
        if (t.when.on === "attacked" && !isNormal) continue;   // attacked: 限普通攻擊觸發; damaged: 任意傷害都觸發
        if (dst.hitFlags.has(t)) continue;             // 同回合每單位每戰法最多觸發1次(防無限鏈)
        if (rnd() >= t.rate) continue;
        dst.hitFlags.add(t);
        if (TRACE) lg(`【${dst.side}】${dst.nm} 戰法【${t.nameZh}】（受擊觸發）發動`);
        if (t.coef) hit(dst, src, t.coef, t.kind, false, onHit);
        if (t.extraHits) fireExtraHits(dst, t, src, alliesOf, foesOf, onHit);  // 批13: 受擊觸發類多段傷害(如剛烈不屈 反擊後群體額外段)
        if (t.effects.length) applyEffects(dst, src, t, alliesOf(dst), foesOf(dst));
      }
    };
    if (TRACE) {                                    // 準備階段標頭: 兵種 + 城建/陣營
      CUR_R = 0;
      lg(`〔採用兵種〕我方 ${troopA}兵　·　敵方 ${troopB}兵`);
      lg(`〔城建滿〕全員 武智統速 各+${CITY}　〔陣營滿〕全屬性 +${Math.round((FACTION - 1) * 100)}%`);
    }
    applyPassives({ noHeal: true, prep: true });    // 依序套用並記錄各類戰法
    if (TRACE) {                                    // 備戰後面板(含適性) + 預備戰法
      lg("〔備戰面板〕屬性 = (基礎+加點+城建)×兵種適性×陣營");
      for (const u of [...A, ...B]) {
        const ap = (u.g.apt || {})[u.ttype] || "—";
        lg(`【${u.side}】${u.nm}（${u.ttype}兵·適性${ap}）　武${Math.round(u.eff("force"))} 智${Math.round(u.eff("intel"))} 統${Math.round(u.eff("command"))} 速${Math.round(u.eff("speed"))}`);
      }
      for (const u of [...A, ...B]) for (const t of u.tactics) if (t.type === "active" && t.prep) lg(`【${u.side}】${u.nm} 戰法【${t.nameZh}】進入預備(首回合後生效)`);
    }

    for (let r = 1; r <= ROUNDS; r++) {
      CUR_R = r;
      for (const u of [...A, ...B]) if (u.alive && u.stack) u.stack.n = Math.min(u.stack.max, u.stack.n + 1);
      applyPassives({ healOnly: true });
      for (const u of [...A, ...B]) {                 // 條件觸發(when.rounds/from/until): 窗口首次開啟時套用一次非傷害效果(dot/amp/…); when.on 為反應式, 不走此處
        if (!u.alive) continue;
        for (const t of u.tactics)
          if ((t.type === "passive" || t.type === "command") && t.when && !t.when.on && roundOk(t, r) && !u.whenFired.has(t)) {
            u.whenFired.add(t);
            if (TRACE) lg(`【${u.side}】${u.nm}（第${r}回合條件）發動【${t.nameZh}】`);
            applyEffects(u, null, t, alliesOf(u), foesOf(u), { noHeal: false });
          }
      }
      // 批8: 裝備效果級回合窗(delayedEq) —— 與戰法 when 窗口同一時點檢查; 效果物件本身(非戰法)
      // 存進 whenFired 去重(一次性), 帶 rate 的額外擲骰(如赳螑 50%機率), 沒中不算已觸發、下次
      // 符合視窗的回合(若 when.rounds 只列單一回合則不會再有機會; from/until 型窗口每回合僅嘗試一次
      // 因為 whenFired 一觸發即封, 若未中也封—— 資料上 rate 型窗口皆為 rounds:[單一回合], 符合設計)。
      for (const u of [...A, ...B]) {
        if (!u.alive || !u.delayedEq.length) continue;
        for (const e of u.delayedEq) {
          if (!roundOk({ when: e.when }, r) || u.whenFired.has(e)) continue;
          u.whenFired.add(e);
          const lbl = `〔特技·${e._eqNm || "?"}〕`;
          if (e.rate != null && rnd() >= e.rate) { if (TRACE) lg(`【${u.side}】${u.nm}${lbl}（第${r}回合條件）未發動`); continue; }
          if (TRACE) lg(`【${u.side}】${u.nm}${lbl}（第${r}回合）發動`);
          applyEffects(u, null, { effects: [e], kind: "phys", n: e.n || 1, nMax: e.nMax || 0 }, alliesOf(u), foesOf(u), { noHeal: false });  // n/nMax傳遞: 群體控制(赳螑 敵軍群體2~3)按效果宣告選目標數
        }
      }
      // 行動順序: 先攻(first)優先於速度; 同速平手隨機(先打亂再穩定排序, 修 A 隊固定先手偏差)
      const order = [...A, ...B].filter(u => u.alive);
      for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
      order.sort((x, y) => (y.first - x.first) || (y.eff("speed") - x.eff("speed")));
      for (const u of order) {
        if (!u.alive) continue;
        if (u.stun) { lg(`【${u.side}】${u.nm} 被控制(震懾)，無法行動`); continue; }
        if (!pickTarget(foesOf(u))) break;
        if (u.silence && TRACE) lg(`【${u.side}】${u.nm} 陷入計窮，無法發動主動戰法`);
        if (!u.silence) for (const t of u.tactics) {   // 自帶 + 傳承: 各自獨立附加發動(計窮時跳過主動/指揮/被動)
          let fire = false;
          if (t.type === "active" && (t.coef || t.effects.length) && !(t.prep && r === 1)) fire = rnd() < t.rate + u.addbonusFor("rateup", t);  // rateup: 提高自身主動戰法發動機率(如白眉); addbonusFor 依 t.prep/t.native 篩選 prepOnly/nativeOnly 修飾的加成(批7: 太平道法)
          else if ((t.type === "command" || t.type === "passive") && t.coef && !(t.when && t.when.on) && roundOk(t, r)) fire = rnd() < t.rate;  // 指揮/被動: 每回合以資料 rate 擲骰(多數 rate=1 即每回合必發); when.rounds/from/until 只在符合回合才擲骰; when.on(反應式) 改由 onHit 事件點觸發, 不在此處常駐擲骰
          if (fire) {
            if (TRACE) lg(`【${u.side}】${u.nm} 發動戰法【${t.nameZh}】` + (t.when ? `（第${r}回合條件）` : ""));
            let _mainHitTgt = null;   // 批13: 記錄主 coef 段命中的(單體)目標, 供 extraHits 同目標段(如屠几上肉 兵刃+謀略打同一人)沿用
            if (t.coef) {
              const cnt = t.nMax ? (t.n + Math.floor(rnd() * (t.nMax - t.n + 1))) : t.n;
              // 批12 ModeB: hitsRepeat —— 「隨機單體攻擊X次/重複X次,每次獨立選擇目標」= N次獨立單體
              // 抽樣(可重複命中同一目標), 非 pickTargets 的 N 人不重複群攻。逐次呼叫 pickTarget
              // (每次重新擲骰), 而非一次性呼叫 pickTargets(不重複)。維持只打敵方(不套用 ModeF 混亂
              // 敵我不分, brief 僅明確要求普攻與單體主動戰法目標受混亂影響, hitsRepeat 屬多段傷害coef
              // 迴圈不在明確範圍內, 維持保守foes-only)。
              // 批12 ModeG: lockTarget —— 單體(cnt<=1)coef傷害目標改用 resolveLockedTarget(首次發動
              // pickTarget 選定後, 之後每次發動重用同一目標); 群體(cnt>1)/hitsRepeat 不套用鎖定語意
              // (lockTarget 資料上僅用於單體戰法, 群體/多段傷害維持原邏輯)。
              if (t.lockTarget && cnt <= 1 && !t.hitsRepeat) { const v = resolveLockedTarget(u, t, foesOf(u)); if (v) { hit(u, v, t.coef, t.kind, false, onHit); _mainHitTgt = v; } }
              else if (t.hitsRepeat) { for (let i = 0; i < cnt; i++) { const v = pickTarget(foesOf(u), u); if (v) { hit(u, v, t.coef, t.kind, false, onHit); _mainHitTgt = v; } } }
              else { const vs = pickTargets(foesOf(u), cnt); for (const v of vs) hit(u, v, t.coef, t.kind, false, onHit); if (vs.length === 1) _mainHitTgt = vs[0]; }
            }
            if (t.extraHits) fireExtraHits(u, t, _mainHitTgt, alliesOf, foesOf, onHit);  // 批13: 多段傷害(兵刃+謀略雙段/主傷+補刀等)
            // 批12 ModeF: 混亂下單體主動戰法目標改敵我不分(pickTargetChaos); 群體/AoE(who=enemy 全體/
            // n>1)維持 applyEffects 內部既有邏輯不變 —— 這裡傳入的 tgt 只影響「單體優先鎖定」分支,
            // 群體戰法本就走 pickTargets(enemies,...) 不受此參數影響(近似, 群體戰法混亂下仍只打敵方)。
            // 批12 ModeG: lockTarget 的 applyEffects 目標(單體效果destination)同樣改用鎖定目標
            // (與混亂互斥: lockTarget 戰法目前資料上未與 chaos 共存, 若未來衝突以 lockTarget 優先,
            // 因 lockTarget 語意更明確針對特定戰法設計)。
            if (t.type === "active") applyEffects(u, t.lockTarget ? resolveLockedTarget(u, t, foesOf(u)) : pickTargetChaos(u, alliesOf(u), foesOf(u)), t, alliesOf(u), foesOf(u));
          }
        }
        const tgt = pickTargetChaos(u, alliesOf(u), foesOf(u));  // 普攻(常駐) + 連擊 + 突擊(繳械時跳過); 嘲諷: 強制指向施放者; 混亂: 敵我不分(批12 ModeF)
        if (tgt) {
          if (u.disarm) { if (TRACE) lg(`【${u.side}】${u.nm} 陷入繳械，無法普通攻擊`); }
          else {
            if (TRACE) lg(`【${u.side}】${u.nm} 普通攻擊 → ${tgt.nm}`);
            hit(u, tgt, 1.0, "phys", true, onHit);
            for (let i = 0; i < extraCount(u.addbonus("extra")); i++) { const nt = pickTargetChaos(u, alliesOf(u), foesOf(u)); if (nt) { if (TRACE) lg(`【${u.side}】${u.nm} 連擊 → ${nt.nm}`); hit(u, nt, 1.0, "phys", true, onHit); } }
            // 突擊(charge)擲骰: chargeup(突擊發動率加成, 如虎豹騎)只對真突擊戰法生效, 排除 t.proc===true 的
            // 特技偽戰法(user 明確指示: 特技不吃突擊加成, 例虎豹騎/三勢陣/經天緯地/陷陣突襲proc本身無此欄)。
            for (const t of u.tactics) if (t.type === "charge" && rnd() < t.rate + (t.proc ? 0 : u.addbonusFor("chargeup", t))) { if (TRACE) lg(`【${u.side}】${u.nm} 突擊【${t.nameZh}】`); if (t.coef) hit(u, tgt, t.coef, t.kind, false, onHit); if (t.extraHits) fireExtraHits(u, t, tgt, alliesOf, foesOf, onHit); applyEffects(u, tgt, t, alliesOf(u), foesOf(u)); }
          }
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
      // 批8: 殲滅(kill) —— ROUNDS 回合內一方全滅, 對比「判定勝」(打滿8回合按剩餘兵力比較)。
      if (!A.some(u => u.alive)) { if (TRACE) lg(`〔戰鬥結束〕敵方【殲滅】我方，第${r}回合`); return { winner: "B", rounds: r, kill: true }; }
      if (!B.some(u => u.alive)) { if (TRACE) lg(`〔戰鬥結束〕我方【殲滅】敵方，第${r}回合`); return { winner: "A", rounds: r, kill: true }; }
    }
    const ta = A.reduce((s, u) => s + Math.max(0, u.troop), 0), tb = B.reduce((s, u) => s + Math.max(0, u.troop), 0);
    const winner = ta >= tb ? "A" : "B";
    if (TRACE) lg(`〔戰鬥結束〕【判定勝(剩餘兵力)】${winner === "A" ? "我方" : "敵方"}　我方剩餘${Math.round(ta)}　敵方剩餘${Math.round(tb)}`);
    return { winner, rounds: ROUNDS, kill: false };
  }

  function trace(POOL, teamA, teamB, troopA = null, troopB = null, bsA = null, bsB = null, eqA = null, eqB = null, addA = null, addB = null, inhA = null, inhB = null, scenario = null) {
    TRACE = []; CUR_R = 0;                           // 跑一場並記錄事件日誌
    const r = fight(POOL, teamA, troopA, teamB, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario);
    const log = TRACE; TRACE = null;
    return { ...r, log };
  }
  function simulate(POOL, teamA, teamB, n = 2000, troopA = null, troopB = null, bsA = null, bsB = null, eqA = null, eqB = null, addA = null, addB = null, inhA = null, inhB = null, scenario = null) {
    let a = 0, rs = 0, killA = 0, killB = 0;          // 批8: 殲滅(kill) vs 判定勝(8回合打滿按剩餘兵力) 分開統計
    for (let i = 0; i < n; i++) {
      const r = fight(POOL, teamA, troopA, teamB, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario);
      if (r.winner === "A") { a++; if (r.kill) killA++; } else if (r.kill) killB++;
      rs += r.rounds;
    }
    return {
      winA: +(a / n).toFixed(3), winB: +(1 - a / n).toFixed(3), rounds: +(rs / n).toFixed(1),
      killRate: +((killA + killB) / n).toFixed(3), killA: +(killA / n).toFixed(3), killB: +(killB / n).toFixed(3),
      judgeA: +((a - killA) / n).toFixed(3), judgeB: +((n - a - killB) / n).toFixed(3),  // 判定勝(剩餘兵力比較, 非殲滅)分邊統計
    };
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
    Unit, hit, damage, pickTarget, pickTargets, pickTargetChaos, resolveLockedTarget, applyEffects, roundOk, fireExtraHits };  // 供測試腳本直接驗證內部機制(同 sgz.py 直接測 Unit/hit)
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  root.SGZ = API;
})(typeof globalThis !== "undefined" ? globalThis : this);
