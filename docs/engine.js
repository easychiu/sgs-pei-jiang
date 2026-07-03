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
  // 批18: 傷兵池(治療上限) —— user 遊戲實測: 受到的傷害按「當時回合數」轉化為「可救援(計入
  // 傷兵池, 治療只能回這部分)」vs「不可救援(直接陣亡, 治療無法挽回)」, 轉化率隨回合遞減
  // (見 docs/data/calibration_anchors.json → wounded_pool)。1~3回合90%、4~6回合80%、
  // 7~8回合67.5%(原文65~70%取中值)。準備階段(CUR_R=0)算第1回合檔(尚未進入回合迴圈, 但
  // 兵書/裝備/被動等準備階段效果如 dot/settle 快照造成的傷害仍需計入傷兵池)。
  const WOUNDED_RATES = [0.90, 0.90, 0.90, 0.80, 0.80, 0.80, 0.675, 0.675];  // index 0 = 第1回合
  const woundedRate = r => WOUNDED_RATES[Math.max(0, Math.min(WOUNDED_RATES.length, r || 1) - 1)];
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

  // 批24 D1: teamGate(隊伍構成前提) —— 判斷隊伍陣營組成是否符合戰法宣告的前提。
  // "allDiff": 三名武將陣營兩兩不同(潛龍陣「我軍三名武將陣營均不相同時」); "allSame":
  // 三名武將陣營皆相同(供未來同類戰法使用, 目前全庫無此案例但一併支援對稱語意)。
  // factions 為隊伍全體(含自己)的陣營陣列, 已在 fight() 建構 Unit 前準備好傳入。
  function teamGateOk(gate, factions) {
    if (!gate || !gate.factions) return true;
    const uniq = new Set(factions).size;
    if (gate.factions === "allDiff") return uniq === factions.length;
    if (gate.factions === "allSame") return uniq === 1;
    return true;                                    // 未知 gate 種類: 保守放行(不擋), 避免資料錯字導致戰法整組消失
  }
  class Unit {
    constructor(g, ttype, bsName, eqName, add, inherit, season, teamFactions) {
      this.g = g; this.ttype = ttype; this.troop = START_TROOP; this.stun = 0;
      this.silence = 0; this.disarm = 0; this.insight = 0; this.first = 0;  // 控制細分: 計窮/繳械/洞察(免控) + 先攻(優先行動, 剩餘回合數)
      this.chaos = 0;                              // 批12 ModeF: 混亂(不鎖行動, 但普攻/單體主動戰法改為敵我不分隨機選目標), 剩餘回合數
      this.ambush = 0;                              // 批18: 遇襲(先攻的反面, 遲緩) —— 剩餘回合數, 行動排序時與 first 一併算 effFirst(見 fight() 排序鍵)
      this.wounded = 0;                             // 批18: 傷兵池 —— 累積「可救援」量(受到的傷害按當時回合轉化率折算, 見 WOUNDED_RATES); 治療結算上限=min(治療量, wounded, START_TROOP-troop)
      // 自帶 + 傳承; 自帶戰法(g.tactic)淺拷貝附加 native:true 旗標(供 rateup/chargeup 的 nativeOnly
      // 修飾判斷「這是不是自帶戰法」, 如太平道法只加成張角自帶的五雷轟頂)。淺拷貝而非直接改
      // TACTICS 共享物件, 避免多個武將共用同一戰法物件時互相污染(如兩人都自帶白眉)。
      // 批24 D1: teamGate —— 開戰時(建構Unit當下, teamFactions已由fight()備妥)判定一次,
      // 不滿足前提的戰法整條從 this.tactics 過濾掉(不進入後續 cmdPassiveSrcs/onHitTacs/
      // onHitEffectTacs 等衍生快取, 亦不會被 applyPassives/回合迴圈讀到, 等同整戰法不生效)。
      this.tactics = (g.tactic ? [Object.assign({}, g.tactic, { native: true })] : []).concat((inherit || []).map(nm => TACTICS[nm]).filter(Boolean))
        .filter(t => {
          const ok = teamGateOk(t.teamGate, teamFactions || []);
          if (!ok && TRACE) lg(`【${g.name}】戰法【${t.nameZh}】不滿足隊伍構成前提(teamGate), 整戰法不生效`);
          return ok;
        });
      // 批18: fakeReport(偽報) 加強 —— 記錄「自己的指揮/被動戰法」名稱集合, 供 eff()/addbonus()
      // 判斷某條 adds/mods/statAdds 是否來自「本單位自己的指揮/被動戰法」(而非兵書/裝備/緣分/
      // 隊友戰法, 這些沒有 src 或 src 不在此集合中, 不受偽報影響)。user 實測: 偽報命中後,
      // 受害者「已生效」的指揮/被動效果(如暫避其鋒的減傷、太史慈神射的連擊)當下就失效, 到期
      // 才恢復 —— 故不能只靠 fight() 主迴圈抑制「本回合擲骰」(那只擋得住還沒生效的coef段/onHit
      // 反應), prep 階段已套用進 adds/mods/statAdds 的常駐效果需要在讀取時（eff/addbonus）過濾掉。
      this.cmdPassiveSrcs = new Set(this.tactics.filter(t => t.type === "command" || t.type === "passive").map(t => t.nameZh).filter(Boolean));
      const _bn = Array.isArray(bsName) ? bsName : (bsName ? [bsName] : []);
      const _bsAll = _bn.flatMap(nm => (BINGSHU[nm] && BINGSHU[nm].effects) || []);  // 兵書(主+副)合併; 缺 effects 欄降級空陣列(同 sgz.py .get)
      this.bs = _bsAll.filter(e => !(e.when && e.when.on));
      // 批22: 兵書效果級 e.when.on(急救類反應式治療, 如三軍之眾「戰鬥第2-4回合自身獲得急救」)
      // —— 與裝備 onHitEq 同慣例, 兵書效果本無獨立回合窗機制(applyPassives 只在 prep/healOnly
      // 套用整包 this.bs), 帶 e.when.on 的效果分離到此陣列, 於 onHit() 反應式事件點結算。
      this.onHitBs = _bsAll.filter(e => e.when && e.when.on);
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
      this.delayedEq = _eqAll.filter(e => e.when && !e.when.on);
      // 批22: 裝備效果級 e.when.on(急救類反應式治療, 如長健/青囊書「戰鬥首回合受傷時回復
      // 10%兵力」) —— 與上面 delayedEq(回合視窗一次性套用)不同語意: on:"damaged"/"attacked"
      // 是「受傷當下觸發」, 不是「特定回合開啟時套用一次」。與 onHitEffectTacs(戰法版本)
      // 對應的裝備版本, 在 onHit() 反應式事件點結算, 同樣可與 e.when.until/from 等回合窗口
      // 欄位並存(round_ok 檢查)。
      this.onHitEq = _eqAll.filter(e => e.when && e.when.on);
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
      // 批28 B1: 守護式反擊(counter.guardFor) —— 「A受擊時, B代為反擊」的方向(如虎衛軍
      // 「我軍主將即將受到普攻時, 副將反擊」), 與 this.counter(持有者自己受擊自己反擊)方向
      // 相反, 掛在「被保護者」(如主將)身上一份清單, 每個元素是{unit(反擊執行者), coef, kind,
      // prob}, 見 applyEffects 的 guardFor 分支與 hit() 內的觸發判斷。與 guardian(傷害轉移
      // 代承)是不同機制, 可並存不衝突。
      this.counterGuards = [];
      this.tauntBy = null; this.tauntDur = 0;      // 嘲諷: 被嘲諷時強制普攻/單體戰法指向 tauntBy, 剩餘回合
      this.shield = null;                          // 護盾: {amt, dur} 吸收固定量傷害, 先於兵力扣減
      this.block = [];                              // 批22: 次數型格擋(抵禦/警戒同族) —— [{val, n, src}], 消耗順序見 hit(); val=1.0全擋/0.x部分減傷, n=剩餘次數
      this.dodgeProb = 0; this.dodgeDur = 0;        // 規避: 機率完全迴避一次傷害
      this.surehitDur = 0;                          // 必中: 無視對方 dodge
      this.healblock = 0;                           // 批8: 禁療(healblock) 剩餘回合, >0 時 heal 效果對其無效
      this.whenFired = new Set();                   // 條件觸發(when.rounds/from/until) 已套用效果的戰法(一次性), 依戰法物件去重; 批8: delayedEq(裝備效果級when)共用同一個 Set(效果物件本身去重, 不與戰法物件撞)
      this.healRoundsFired = null;                  // 批15: heal 效果 e.when.rounds(明確列出的特定回合)的「每回合各觸發一次」去重, Map<效果物件, Set<已觸發回合數>>, 惰性建立(見 applyEffects 的 heal 分支)
      this.hitFlags = new Set();                    // 反應式觸發(when.on) 本回合已觸發的戰法, 每回合重置(防無限鏈)
      this.onHitTacs = this.tactics.filter(t => (t.type === "passive" || t.type === "command") && t.when && t.when.on);  // 預篩: 絕大多數單位為空, hit 熱路徑 O(0)
      // 批22: 效果級 e.when.on(急救類反應式治療, 如陷陣營/長健/雲聚影從「受到傷害時XX%機率
      // 獲得治療」) —— 與上面 t.when.on(戰法級, 整個戰法都是反應式)不同: 這類戰法本身有其他
      // 常駐效果(如陷陣營的武力/統率平加)需要在 prep 階段就套用, 只有其中的 heal 效果段是
      // 「受傷當下才觸發」的反應式語意, 不能把整個戰法標成 t.when.on(那樣會連帶讓武力/統率
      // 平加也不在 prep 套用, 語意跑掉)。onHitEffectTacs 收集這類「戰法本身無 t.when, 但至少
      // 一個效果帶 e.when.on」的戰法, onHit() 只讀取/結算符合的個別效果(不影響同戰法其餘無
      // e.when 的效果, 那些已在 prep 由 applyPassives 正常套用)。
      // 批23: 型別放寬含 active —— 過去只認 passive/command(「戰法本身有其他常駐效果, 只有
      // heal段是反應式」的典型模式, 如陷陣營/雲聚影從)。但草船借箭一類 type:"active" 戰法也有
      // 同樣模式(「使我軍獲得急救狀態, 受傷時機率觸發治療」是active發動後掛的一個反應式buff,
      // 不是常駐), 過去完全沒有機制承接, 只能誤把heal當成active發動當下的常駐治療(0分bug)。
      // 放寬後active戰法帶e.when.on的效果同樣走onHit()反應式結算, 該戰法主coef/其餘無when
      // 效果仍照常經由主動擲骰路徑(t0.rate)發動觸發(兩者互不干擾, 見applyEffects內新增的
      // opt.reactive閘門, 確保e.when.on效果不會在active擲骰命中時被重複套用)。
      this.onHitEffectTacs = this.tactics.filter(t => !t.when && (t.type === "passive" || t.type === "command" || t.type === "active") && (t.effects || []).some(e => e.when && e.when.on));
      // 批27 A: on:"dealtDamage" —— 「自身造成傷害時/後」反應式掛鉤(對比 onHitTacs 的
      // attacked/damaged 是「自己受擊」視角, 這裡是「自己打人」視角, 如白衣渡江「造成兵刃
      // 傷害時25%→50%機率使敵軍單體繳械」)。掛在 hit() 傷害結算後對 src(施加傷害的一方)
      // 掃描, 與 onHitTacs/onHitEffectTacs 完全對稱(戰法級 vs 效果級 兩種顆粒度)。
      // dmgType(選填, "phys"/"intel"): 區分「造成兵刃傷害時」vs「造成謀略傷害時」兩種不同
      // 觸發條件(白衣渡江 disarm 段只在兵刃傷害後觸發, silence 段只在謀略傷害後觸發), 沿用
      // amp/mitig 既有 dmgType 欄位命名慣例, 無此欄位視為兩種傷害類型皆可觸發(向後相容)。
      this.onDealTacs = this.tactics.filter(t => (t.type === "passive" || t.type === "command") && t.when && t.when.on === "dealtDamage");
      this.onDealEffectTacs = this.tactics.filter(t => !t.when && (t.type === "passive" || t.type === "command" || t.type === "active") && (t.effects || []).some(e => e.when && e.when.on === "dealtDamage"));
      this.lockedTargets = new Map();               // 批12 ModeG: lockTarget:true 戰法的鎖定目標, 鍵=戰法物件本身(同一戰法物件跨回合重用同一 Map)
      // 批16: 原語擴充包 —— 新增狀態欄位(現有資料無新欄位, 皆維持0/null預設值, 行為零變化)
      this.atkCount = new Map();                     // everyN: 自身普攻次數計數器, 鍵=戰法物件本身(同一戰法物件跨回合重用同一 Map)
      this.immune = [];                              // immuneTo: [type, dur] 陣列(單項控制免疫, 對比 insight 全免); O(小陣列)線性掃描, 資料上每單位條數<5
      this.hpBelowFired = new Set();                 // hpPct: when.hpBelow(一次性, 首次跨越即觸發) 已觸發的戰法, 依戰法物件去重
      this.fakeReportDur = 0;                        // fakeReport(偽報): 剩餘回合數, >0 時指揮/被動 coef 擲骰段與 onHit 反應式觸發受抑制(prep已套用效果不回收)

    }
    get alive() { return this.troop > 0; }
    // 批18: fakeReport(偽報) 期間, 來源為「自己的指揮/被動戰法」(src ∈ cmdPassiveSrcs) 的條目
    // 暫停參與計算(到期自動恢復, 不刪除條目本身 —— 條目仍在 adds/mods/statAdds 陣列裡, tick()
    // 到期照舊遞減/移除, 只是這裡讀取時跳過)。src 為 null/undefined(兵書/裝備/緣分/其他來源)
    // 或不在 cmdPassiveSrcs 中(隊友戰法/傳承戰法皆有各自 nameZh, 但這裡只關心「自己」的指揮/
    // 被動, 傳承戰法若也是 command/passive 型態一樣會被記入 cmdPassiveSrcs, 符合 user 描述的
    // 「指揮/被動戰法」泛用語意, 不分自帶/傳承)不受影響。
    // 批24: src 可能帶「:尾碼」區分同源多條目(rateup 的 :prepOnly/nativeOnly、dmgType 的
    // :phys/:intel, 見 pushAdd 呼叫端), 但 cmdPassiveSrcs 只存純戰法名(nameZh, 不含尾碼)。
    // 比對前先去除尾碼(取第一個':'之前的部分)還原成純戰法名, 避免帶尾碼的 src 永遠比對不到
    // cmdPassiveSrcs、讓偽報(fakeReport)抑制對這類條目完全失效(修正批16 rateup/chargeup
    // 尾碼慣例引入時就存在的潛在比對錯位, 批24新增的 dmgType 尾碼沿用同一約定一併受益)。
    suppressed(src) { if (!src || this.fakeReportDur <= 0) return false; const base = src.includes(":") ? src.slice(0, src.indexOf(":")) : src; return this.cmdPassiveSrcs.has(base); }
    eff(stat) {
      if (this.swap && (stat === "force" || stat === "intel")) stat = stat === "force" ? "intel" : "force";
      let v = this[stat];
      for (const [s, add, , src] of this.statAdds) if ((s === stat || s === "all") && !this.suppressed(src)) v += add;  // 裝備平加(獨立階段, 在陣營/兵種營後、戰法乘算前)
      for (const [s, m, , src] of this.mods) if ((s === stat || s === "all") && !this.suppressed(src)) v *= m;
      return v;
    }
    // 批24 D2: dmgType(可選) —— 只加總「該條目未宣告 dmgType, 或宣告的 dmgType 與呼叫端指定
    // 的 dmgType 相符」的項目, 供 amp/mitig 依「兵刃/謀略」傷害類型過濾(見 damage() 呼叫端)。
    // dmgType 省略(undefined)時完全維持原行為(不分類型全部加總), 向後相容全庫既有未帶
    // dmgType 的 amp/mitig 資料。
    // 批28 B3: isNormal(可選) —— 只加總「該條目未宣告 normalOnly, 或宣告 normalOnly 且本次
    // isNormal 為 true」的項目, 供 amp 表達「僅普攻傷害提升」(見至柔動剛)。未傳(undefined,
    // 如dot/counter/settle等非普攻傷害路徑)時安全側不套用 normalOnly 加成。
    addbonus(kind, dmgType, isNormal) {
      let s = 0;
      for (const a of this.adds) {
        if (a[0] !== kind || this.suppressed(a[3])) continue;
        const f = a[4];
        if (dmgType && f && f.dmgType && f.dmgType !== dmgType) continue;
        if (f && f.normalOnly && isNormal !== true) continue;
        s += a[1];
      }
      return s;
    }
    // rateup/chargeup 專用: 依戰法 t 的 prep/native 屬性, 只加總「修飾旗標吻合」的 adds 項。
    // adds[4] = flags({prepOnly,nativeOnly,inheritedOnly}|undefined, 見 pushAdd)。無旗標
    // (undefined/{}) 的加成一律計入(如虎豹騎的 chargeup 沒有 prepOnly/nativeOnly 限制)。
    // 批8: inheritedOnly(nativeOnly 反向) —— 只加「非自帶」(傳承)戰法, 如竭力佐謀「非自帶
    // 主動戰法發動率+100%」; !t.native 即傳承(Unit 建構時自帶戰法才標 native:true)。
    addbonusFor(kind, t) {
      let s = 0;
      for (const a of this.adds) {
        if (a[0] !== kind || this.suppressed(a[3])) continue;
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
    pushMod(stat, mult, dur, src, flags) {
      if (src) this.mods = this.mods.filter(m => !(m[0] === stat && m[3] === src));
      this.mods.push([stat, mult, dur, src, flags]);
    }
    pushStatAdd(stat, add, dur, src, flags) {                 // 屬性平加(裝備 stat.add): 同 pushMod 慣例, 同來源刷新不疊
      if (src) this.statAdds = this.statAdds.filter(a => !(a[0] === stat && a[3] === src));
      this.statAdds.push([stat, add, dur, src, flags]);
    }
    // 批22: block(次數型格擋, 抵禦/警戒同族) —— 與 shield/mitig 語意不同: 不是持續減傷/固定量
    // 吸收池, 而是「剩餘次數」計次器, 每次受擊消耗1次(而非按傷害量扣減), val=1.0時完全格擋
    // 該次傷害、val=0.x時該次傷害打折(如警戒 -75.35%≈val:0.7535)。同源(同 src)再次施加時
    // 疊加次數(而非同 pushAdd/pushMod 慣例的「同源刷新覆蓋」), 貼合原文「抵禦(N次)」「目前
    // 抵禦總次數為N」的疊次語意(見 docs/data/calibration_anchors.json battle_report_round_20260703
    // 戰報實測: 「抵禦(1)」用一次消一層, 「警戒(1)」-75.35%減傷/次用後消層)。
    pushBlock(val, n, src) {
      const existed = src && this.block.find(b => b.src === src && Math.abs(b.val - val) < 1e-9);
      if (existed) existed.n += n; else this.block.push({ val, n, src });
    }
    // 消耗一次格擋(若有): 從陣列頭(先加的先消耗, 貼合戰報「總次數」單一計數語意, 不分層級
    // 順序; 多筆不同 val 的 block 並存時採先進先出)扣1次, n<=0時整筆移除。回傳消耗到的 val
    // (0=無格擋可消耗, 呼叫端不應觸發)。
    consumeBlock() {
      if (!this.block.length) return 0;
      const b = this.block[0];
      b.n -= 1;
      const val = b.val;
      if (b.n <= 0) this.block.shift();
      return val;
    }
    // 批16: immuneTo —— 單項控制免疫(對比 insight 全免)。immune 陣列存 [type, dur], type ∈
    // stun/silence/disarm/chaos。isImmuneTo(type) 供控制施加處查詢(同 insight 判斷點並列)。
    isImmuneTo(type) { return this.immune.some(([ty]) => ty === type); }
    pushImmune(types, dur) { for (const ty of (types || [])) this.immune.push([ty, dur ?? 1]); }
    // 批16: everyN —— 自身每第N次普攻觸發指定戰法效果的計數器。傳回是否達標(達標即歸零重計)。
    tickEveryN(t) {
      const cfg = t.everyN; if (!cfg) return false;
      const cnt = (this.atkCount.get(t) || 0) + 1;
      if (cnt >= (cfg.count || 1)) { this.atkCount.set(t, 0); return true; }
      this.atkCount.set(t, cnt); return false;
    }
    // 批26 B2: stack.stackPer=="cast" 專用遞增入口 —— 原文常見「每次發動後傷害率提升X」(如
    // 水淹七軍/陷陣突襲), 是「本戰法每次成功發動」才+1層, 與回合數無關。round模式(預設)沿用
    // fight() 主迴圈既有逐回合遞增, 呼叫此方法對round模式應為no-op。
    applyStackCast() {
      if (this.stack && (this.stack.stackPer || "round") === "cast") this.stack.n = Math.min(this.stack.max, this.stack.n + 1);
    }
    // 批16: hpPct —— 自身兵力百分比(troop/START_TROOP), 供 when.hpBelow/hpAbove 檢查
    get hpPct() { return this.troop / START_TROOP; }
    // 批24 D2: amp(dmgType) —— dmgType 傳入時只加總該類型(或未宣告類型)的 amp 加成; stack/decay
    // 目前無 dmgType 概念(全庫暫無「僅對特定傷害類型疊層」的戰法), 維持無條件全額計入, 與呼叫端
    // 的 dmgType 過濾無關(不受影響, 向後相容)。批28 B3: isNormal(可選) —— 過濾 normalOnly
    // 標記的加成(僅普攻傷害生效, 見至柔動剛)。
    amp(dmgType, isNormal) {
      let a = this.addbonus("amp", dmgType, isNormal);
      if (this.stack) a += this.stack.per * this.stack.n;
      if (this.decay) a += this.decay.v0 * this.decay.left / this.decay.total;
      return a;
    }
    tick() {
      for (const d of this.dots) { this.troop -= d[0]; this.wounded += d[0] * woundedRate(CUR_R); }  // 批18: dot 掉血同樣按當前回合轉化率計入傷兵池
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
      this.ambush = Math.max(0, this.ambush - 1);     // 批18: 遇襲 逐回合遞減(先攻的反面, 遲緩)
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
      if (this.counter && --this.counter.dur <= 0) this.counter = null;  // 批23 A2: 反擊到期清除(過去 dur 幽靈欄位從不遞減, 帶時限的反擊變永久)
      this.hitFlags.clear();                           // 受擊觸發(when.on) 每回合各戰法重置一次觸發額度
      if (this.immune.length) this.immune = this.immune.filter(a => --a[1] > 0);  // 批16: immuneTo 逐回合遞減
      this.fakeReportDur = Math.max(0, this.fakeReportDur - 1);  // 批16: 偽報 逐回合遞減
    }
  }

  // 傷害公式旋鈕(批3 重塑): 社群拆解(知乎菜頭50級傷害模型 + B站櫻謀詭計錨點), 用實測錨點反解常數。
  // 錨點(兵10000/coef1.0/士氣100/無增減傷, moraleMult(100)=1.0 已併入取樣, 取隨機帶中值1.0):
  //   錨1 屬性差0   → 實測 ≈476 傷害 ⇒ DMG_A = 476/sqrt(10000) = 4.76
  //   錨2 屬性差200 → 實測 ≈764 傷害 ⇒ DMG_B = (764-476)/200 = 1.44
  //   錨3 屬性差大負值(保底) → 實測 ≈90  傷害 ⇒ DMG_FLOOR = 90/sqrt(10000) = 0.9
  // 之後有更多實測數據(不同兵力/等級)可再校準, 目前僅50級單一等級係數樣本, 折入常數中。
  const DMG_A = 4.76, DMG_B = 1.44, DMG_FLOOR = 0.9;
  function damage(src, dst, coef, kind, srcTroop, isNormal) {
    const troop = srcTroop == null ? src.troop : srcTroop;
    const atk = kind === "intel" ? src.eff("intel") : src.eff("force");
    const def = kind === "intel" ? dst.eff("intel") : dst.eff("command");
    const troopSqrt = Math.sqrt(Math.max(0, troop));
    let base = Math.max(DMG_A * troopSqrt + DMG_B * (atk - def), DMG_FLOOR * troopSqrt) * coef;
    base *= counterMult(src.ttype, dst.ttype);     // 克制: 隊伍兵種 vs 隊伍兵種
    base *= moraleMult(MORALE);
    // 批22: 輸出減益疊加上限 -90%(戰報實測: 荀彧-50%疊到-90.00%封頂, 輸出至少保留10%)。
    // 與 SCALE_CLAMP(±1.5, 單一效果值縮放後的per-effect clamp)是不同層級: 這裡是「多個amp
    // 效果加總後」的合計下限。例外: 虛弱(無法造成傷害)類戰法既有慣例用單一 amp val:-1.0
    // 精確歸零當回合傷害(克敵制勝/威謀靡亢/臨戰先登, 見批15/17/19), 這是「無法造成傷害」的
    // 二元語意, 不是「%減益疊加」, 故 amp 總和 <= -1.0 時維持完全歸零(不受-90%封頂影響),
    // 只在 -1.0 < 總和 < -0.9 這個「多重%減益疊加但尚未到虛弱程度」的區間套用-90%下限。
    // 批24 D2: dmgType 過濾 —— amp()/addbonus("mitig") 傳入本次傷害的 kind(phys/intel), 只
    // 加總「未宣告 dmgType 或宣告類型與本次相符」的加成/減傷, 讓「兵刃傷害提高/謀略傷害降低」
    // 這類定向效果不再誤及不該覆蓋的另一種傷害類型(見 e.dmgType 呼叫端, applyEffects k==="amp"/"mitig"分支)。
    // 批28 B3: isNormal(可選) —— 傳入本次傷害是否為普攻, 供 amp()/addbonus("mitig") 過濾
    // normalOnly 標記的加成/減傷(僅普攻傷害生效/受影響, 見至柔動剛「降低我軍及敵軍全體普通
    // 攻擊傷害35%」)。
    const totalAmp = src.amp(kind, isNormal);
    base *= totalAmp <= -1 ? 0 : 1 + Math.max(-0.9, totalAmp);
    const mit = dst.addbonus("mitig", kind, isNormal) * (1 - Math.min(1, src.addbonus("pierce")));
    base *= Math.max(0.1, 1 - mit);
    base *= 0.96 + rnd() * 0.08;   // 隨機帶 0.96~1.04(對稱): rnd()*0.08 涵蓋 [0,0.08), 起點0.96 → 上限0.96+0.08=1.04
    return Math.max(0, base);
  }
  function hit(src, dst, coef, kind, isNormal, onEvent, onDeal) {
    if (!src.surehitDur && dst.dodgeDur && rnd() < dst.dodgeProb) {  // 規避: 完全迴避一次傷害(必中無視)
      if (TRACE) lg(`　→ ${dst.nm} 規避了攻擊`);
      if (onEvent) onEvent(dst, src, isNormal);
      return;
    }
    let dmg = damage(src, dst, coef, kind, undefined, isNormal);  // 批28 B3: 傳入isNormal供amp()過濾normalOnly標記的加成
    // 批22: block(次數型格擋, 抵禦/警戒同族) —— 判定順序 dodge→block→shield→傷害(見紅線指示)。
    // 每次受擊消耗1次(不論本次傷害量多寡), val=1.0(如「抵禦」)完全格擋歸零本次傷害,
    // val=0.x(如「警戒」-75.35%)按比例打折。用光即從陣列移除, 供 TRACE 顯示「剩餘N層」。
    if (dst.block.length) {
      const b = dst.block[0];
      const blockVal = dst.consumeBlock();
      dmg *= Math.max(0, 1 - blockVal);
      if (TRACE) lg(`　▸ ${dst.nm} ${blockVal >= 1 ? "抵禦" : "警戒"}生效` + (blockVal < 1 ? `（減傷${Math.round(blockVal * 100)}%）` : "") + `（剩餘${b.n > 0 ? b.n : 0}層）`);
    }
    if (dst.shield && dst.shield.amt > 0) {                        // 護盾: 先於兵力扣減吸收傷害
      const absorb = Math.min(dst.shield.amt, dmg);
      dst.shield.amt -= absorb; dmg -= absorb;
      if (TRACE && absorb > 0) lg(`　▸ ${dst.nm} 護盾吸收 ${Math.round(absorb)}` + (dst.shield.amt <= 0 ? "（已破盾）" : ""));
      if (dst.shield.amt <= 0) dst.shield = null;
    }
    const g = dst.guardian;
    const wr = woundedRate(CUR_R);        // 批18: 傷兵池 —— 本次受到的傷害按當前回合轉化率計入(準備階段 CUR_R=0 用第1回合檔)
    if (g && g.alive && g !== dst && !(dst.guardNormalOnly && !isNormal)) {
      const gShare = dmg * dst.guardShare, dShare = dmg * (1 - dst.guardShare);
      g.troop -= gShare; g.wounded += gShare * wr;
      dst.troop -= dShare; dst.wounded += dShare * wr;
    }  // normalOnly 援護: 戰法傷害(isNormal=false)不轉移
    else { dst.troop -= dmg; dst.wounded += dmg * wr; }
    if (TRACE) lg(`　→ ${dst.nm} 損兵 ${Math.round(dmg)}，剩餘 ${Math.max(0, Math.round(dst.troop))}` + (dst.troop <= 0 ? " 【擊破】" : ""));
    if (dst.settle) dst.settle.layers = Math.min(dst.settle.max, dst.settle.layers + 1);
    const ls = src.addbonus("lifesteal");                            // 批8: 倒戈 —— 造成傷害時按比例回復自身兵力(以本次造成的傷害量 dmg 為基準), 上限 START_TROOP
    if (ls > 0 && src.alive) {
      const before = src.troop;
      src.troop = Math.min(START_TROOP, src.troop + dmg * ls);
      if (TRACE && src.troop - before >= 1) lg(`　▸ ${src.nm} 倒戈回復 +${Math.round(src.troop - before)}`);
    }
    if (onEvent) onEvent(dst, src, isNormal);
    // 批27 A: on:"dealtDamage" —— src(施加本次傷害的一方)反應式觸發, 只在非規避(確實造成
    // 傷害, 含被完全格擋/護盾吸收歸零的情形——「造成傷害」語意上仍是「打出了這一擊」, 只是
    // 傷害量被防禦手段抵銷, 與「規避=攻擊未命中」不同, 故僅 dodge 分支排除, block/shield
    // 歸零不排除)時才觸發, 傳入 kind 供 dmgType(兵刃/謀略)過濾判斷。
    if (onDeal && src.alive) onDeal(src, dst, isNormal, kind);
    const c = dst.counter;
    if (c && dst.alive && src.alive && rnd() < (c.prob ?? 1)) {
      const cd = damage(dst, src, c.coef ?? 1, c.kind || "phys"); src.troop -= cd; src.wounded += cd * woundedRate(CUR_R);
      if (TRACE) lg(`　↩ ${dst.nm} 反擊 ${src.nm} 損兵 ${Math.round(cd)}，剩餘 ${Math.max(0, Math.round(src.troop))}`);
    }
    // 批28 B1: 守護式反擊(counterGuards) —— dst(如隊伍主將)受到普攻時, 由登記在
    // dst.counterGuards 裡的其他單位(如副將)代為反擊 src, 而非 dst 自己還手(見虎衛軍
    // 「我軍主將即將受到普攻時, 副將...對攻擊者造成兵刃傷害」)。只在普攻(isNormal=true)
    // 時觸發; 每個守護單位每回合最多觸發1次(對應原文「每回合最多觸發1次」), 用 hitFlags
    // 以 guardian 自身+效果物件為鍵節流(與 when.on 反應式的既有節流慣例一致)。
    if (isNormal && dst.alive && src.alive) {
      for (const g of dst.counterGuards) {
        const gu = g.unit;
        if (!gu.alive || gu === dst) continue;
        if (gu.hitFlags.has(g)) continue;
        if (rnd() < (g.prob ?? 1)) {
          gu.hitFlags.add(g);
          const gd = damage(gu, src, g.coef ?? 1, g.kind || "phys");
          src.troop -= gd; src.wounded += gd * woundedRate(CUR_R);
          if (TRACE) lg(`　↩ ${gu.nm}(守護${dst.nm}) 反擊 ${src.nm} 損兵 ${Math.round(gd)}，剩餘 ${Math.max(0, Math.round(src.troop))}`);
        }
      }
    }
  }
  function roundOk(t, r) {                          // 條件觸發(when): 回合是否符合戰法的發動窗口
    const w = t.when;
    if (!w) return true;
    if (w.rounds) return w.rounds.includes(r);
    if (w.from != null && r < w.from) return false;
    if (w.until != null && r > w.until) return false;
    // 批16: parity(奇偶回合) + every(每N回合) —— 與 rounds/from/until 可並存(皆通過才算符合)
    if (w.parity === "odd" && r % 2 !== 1) return false;
    if (w.parity === "even" && r % 2 !== 0) return false;
    if (w.every && r % w.every !== 0) return false;
    return true;
  }
  // 批16: hpPct 觸發 —— 每回合窗口檢查自身兵力百分比(troop/START_TROOP)。hpBelow: 首次跨越即觸發
  // (一次性, whenFired慣例); hpAbove: 持續窗(只要條件成立, 每回合都可能觸發, 不去重)。
  // 與 roundOk 分開的獨立判定(hpPct 條件不是回合數, 需讀 unit.troop, 故不塞進 roundOk)。
  function hpOk(t, u) {
    const w = t.when;
    if (!w) return true;
    if (w.hpBelow != null && !(u.hpPct < w.hpBelow)) return false;
    if (w.hpAbove != null && !(u.hpPct > w.hpAbove)) return false;
    return true;
  }
  function extraCount(ex) { const i = Math.floor(ex); return i + (rnd() < ex - i ? 1 : 0); }
  // 批16: ifTargetHas —— 效果/extraHits 段條件: 只對「已有該狀態」的目標生效/結算。
  // dot: dots 陣列非空(=正在持續掉血); 控制類(stun/silence/disarm/chaos/insight): 對應欄位>0。
  function targetHas(u, type) {
    if (!u) return false;
    if (type === "dot") return u.dots.length > 0;
    if (type === "stun" || type === "silence" || type === "disarm" || type === "chaos" || type === "insight") return u[type] > 0;
    return false;
  }
  // 批16: dispel(驅散/淨化) —— 移除目標身上對應方向(buffs=正向增益/debuffs=負向減益)的條目,
  // 略過帶 undispellable 旗標(flags.undispellable, 見 pushAdd/pushMod/pushStatAdd 呼叫端 udFlags)的條目。
  // buffs: amp(正值)/mitig(正值)/stat mult>1或add>0/rateup/chargeup/shield/dodge/surehit/lifesteal/healBoost/healGiven/counter/pierce/extra/first/insight
  // debuffs: amp(負值)/mitig(負值)/stat mult<1或add<0 + 控制欄位(stun/silence/disarm/chaos/dot/healblock/fakeReport/swap)
  // 只挪動「數值型」adds/mods/statAdds 依正負號分類; 控制欄位(debuffs專屬)直接歸零/清空。
  function dispelUnit(u, what) {
    const isBuff = a => (a[0] === "amp" || a[0] === "mitig") ? a[1] > 0 : true;   // 除 amp/mitig 外的 adds 種類(rateup/chargeup/healBoost/healGiven/lifesteal/pierce/extra)一律視為buff
    const notUD = a => !(a[4] && a[4].undispellable);
    if (what === "buffs") {
      u.adds = u.adds.filter(a => !(isBuff(a) && notUD(a)));
      u.mods = u.mods.filter(m => !((m[1] >= 1) && notUD(m)));
      u.statAdds = u.statAdds.filter(a => !((a[1] >= 0) && notUD(a)));
      if (u.shield && !u.shield.undispellable) u.shield = null;
      if (u.block.length) u.block = [];      // 批22: block(抵禦/警戒)為防禦性增益, 同 shield 慣例被 buffs 驅散清除(現有資料未帶 undispellable block)
    } else {  // debuffs
      u.adds = u.adds.filter(a => !((a[0] === "amp" || a[0] === "mitig") && a[1] < 0 && notUD(a)));
      u.mods = u.mods.filter(m => !((m[1] < 1) && notUD(m)));
      u.statAdds = u.statAdds.filter(a => !((a[1] < 0) && notUD(a)));
      u.dots = u.dots.filter(d => d[2]);                 // 保留 undispellable(d[2]=true)的 dot, 清除其餘
      u.stun = 0; u.silence = 0; u.disarm = 0; u.chaos = 0; u.healblock = 0; u.fakeReportDur = 0; u.ambush = 0;
    }
    if (TRACE) lg(`　▸ ${u.nm} 被驅散〔${what === "buffs" ? "增益" : "減益"}〕`);
  }
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
  // 批18: targetSel(指定選標準則) —— user 實測: 混亂只影響「隨機」選目標的主動/突擊/普攻,
  // 「指定」類戰法(按準則選目標: 兵力最低/武力最高/智力最低/我方最殘等)不受混亂影響, 因為
  // 這些戰法根本不是隨機選標, 而是每次發動當下依準則重新篩選(非鎖定, 見批12/避實擊虛的
  // lockTarget vs 依屬性選標之辨)。KEY_FN: 準則→(單位→比較值), CMP: "min"取最小/"max"取最大。
  const TARGETSEL_KEY = {
    minTroop: u => u.troop, maxForce: u => u.eff("force"), minIntel: u => u.eff("intel"),
    maxIntel: u => u.eff("intel"), minCommand: u => u.eff("command"), mostDamaged: u => u.troop,
  };
  const TARGETSEL_MIN = new Set(["minTroop", "minIntel", "minCommand", "mostDamaged"]);
  function pickByCriterion(units, sel) {
    const keyFn = TARGETSEL_KEY[sel];
    if (!keyFn) return null;                        // 未知準則: 呼叫端應退回一般選標(保守, 不是無聲吃掉)
    const live = units.filter(u => u.alive);
    if (!live.length) return null;
    const wantMin = TARGETSEL_MIN.has(sel);
    let best = live[0], bestV = keyFn(best);
    for (const u of live.slice(1)) {
      const v = keyFn(u);
      if (wantMin ? v < bestV : v > bestV) { best = u; bestV = v; }
    }
    return best;
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

  // 批16: choices(擇一分支) —— 戰法欄 choices:[{weight, effects,...}], 發動時按權重隨機選一組
  // 效果套用(預設均分, 無 weight 視為1)。回傳中選分支物件本身(供 Object.assign 覆寫基礎戰法的
  // coef/kind/effects/extraHits/n/nMax 等欄位; 分支未提供的欄位保留基礎戰法原值)。
  function pickChoice(choices) {
    const ws = choices.map(c => c.weight ?? 1);
    const total = ws.reduce((a, b) => a + b, 0);
    let x = rnd() * total;
    for (let i = 0; i < choices.length; i++) { x -= ws[i]; if (x <= 0) return choices[i]; }
    return choices[choices.length - 1];
  }
  // 批13: extraHits —— 多段傷害(兵刃+謀略雙段/主傷+補刀等單一 coef/kind/n 無法表達的戰法)。
  // 戰法欄 "extraHits":[{coef,kind,n,nMax,rate,who,_note}]: 主 coef 結算後逐段獨立處理,
  // 每段各自 rate 擲骰(預設1必發)、選目標、hit()。who 可選: "sameTarget"(沿用主 coef 段已
  // 選定的(單體)目標, 如屠几上肉 兵刃+謀略同目標/一騎當千 主將加成同目標)、"enemyLeader"
  // (固定打敵方主將 foes[0], 如百騎劫營/暗藏玄機 額外段明確打敵軍主將)、不填則預設
  // pickTargets(敵方, 依 n/nMax)。與主 coef 段完全獨立(各自的 kind 可不同, 如兵刃主傷+謀略
  // 補刀), 不與 hitsRepeat/lockTarget 互斥(hitsRepeat/lockTarget 只影響主 coef 段的選標方式,
  // extraHits 段固定用上述規則)。
  function fireExtraHits(u, t, tgt, alliesOf, foesOf, onHit, onDeal) {
    if (!t.extraHits) return;
    for (const eh of t.extraHits) {
      if (rnd() >= (eh.rate ?? 1)) continue;
      const n = eh.n || 1;
      const cnt = eh.nMax ? n + Math.floor(rnd() * (eh.nMax - n + 1)) : n;
      let dests;
      // 批18: targetSel(指定選標準則) —— 段級欄位, 優先於 who 的其餘規則(sameTarget/enemyLeader/
      // 隨機)。如 上兵伐謀「分別對兵力最低、武力最高、智力最低的敵將」三段各自不同準則。
      if (eh.targetSel) { const picked = pickByCriterion(foesOf(u), eh.targetSel); dests = picked ? [picked] : []; }
      else if (eh.who === "sameTarget") dests = tgt && tgt.alive ? [tgt] : [];        // 沿用主段已選定的(單體)目標
      else if (eh.who === "enemyLeader") { const fl = foesOf(u)[0]; dests = (fl && fl.alive) ? [fl] : []; }  // 固定打敵方主將(index 0)
      else if (cnt <= 1 && tgt && tgt.alive && !eh.who) dests = [tgt];   // 未指定 who 且單體: 沿用主段目標(向後相容預設行為)
      else dests = pickTargets(foesOf(u), cnt);
      // 批16: ifTargetHas —— extraHits 段結算前檢查, 只對「已有該狀態」的目標結算此段傷害
      if (eh.ifTargetHas) dests = dests.filter(v => targetHas(v, eh.ifTargetHas));
      if (TRACE && dests.length) lg(`　▸ ${t.nameZh || "?"}〔額外段${eh.targetSel ? "·" + eh.targetSel : ""}〕${eh.kind === "intel" ? "謀略" : "兵刃"}傷害 → ${dests.map(v => v.nm).join("、")}` + (eh._note ? `（${eh._note}）` : ""));
      for (const v of dests) hit(u, v, eh.coef, eh.kind || "phys", false, onHit, onDeal);
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
      case "ambush": return `遇襲(遲緩)${d || "(1回合)"}`;
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
      case "block": { const bv = (e.scale && caster) ? Math.max(0, Math.min(1, (e.val ?? 1.0) * scaleOf(caster, e.scale))) : (e.val ?? 1.0); return `${bv >= 1 ? "抵禦" : `警戒(減傷${p(bv)})`}(${e.times ?? 1}次)` + sfx; }
      case "surehit": return `必中·無視規避${d}`;
      case "healblock": return `禁療·無法被治療${d || "(1回合)"}`;
      case "lifesteal": return `倒戈·造成傷害回復${p(val)}${d}` + sfx;
      case "immune": return `控制免疫〔${(e.types || []).join("、")}〕${d || "(1回合)"}`;
      case "healBoost": return `受治療效果${val >= 0 ? "+" : ""}${p(val)}${d}` + sfx;
      case "healGiven": return `施放治療效果${val >= 0 ? "+" : ""}${p(val)}${d}` + sfx;
      case "dispel": return `驅散〔${e.what === "buffs" ? "增益" : "減益"}〕`;
      case "fakeReport": return `偽報·被動指揮戰法失效${d || "(1回合)"}`;
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
      if (opt.healOnly && k !== "heal" && !e.everyRound) continue;  // 批30 A: everyRound 效果亦放行(見下方通用閘門)
      // 批18: e.when 泛化(非 heal 種類) —— heal 早已支援效果級 when(見下方 k==="heal" 分支的
      // opt.healOnly 閘門), 但其餘效果種類(amp/settle/stat/…)過去若帶 e.when 而母戰法無 t.when,
      // 會在 prep 階段(opt.skipWhenEffects=true 時, 見 fight() 呼叫端)被無聲當成「無 when 的常駐
      // 效果」立即套用, 忽略 e.when 指定的回合窗口(如 密計誅逆的 settle when:{rounds:[6]}/
      // 工神的 amp when:{from:4}, 見 _todo 揭露)。此處在 prep 呼叫時跳過這些效果, 改由 fight()
      // 回合迴圈的通用 e.when 掃描(仿 delayedEq 慣例)在視窗開啟時才套用, 見下方呼叫端。
      if (opt.skipWhenEffects && k !== "heal" && e.when && !t.when) continue;
      // 批23: e.when.on(反應式, 受擊當下觸發) 效果只應在 onHit() 事件點結算(opt.reactive=true
      // 的合成單效果呼叫), 不應在準備階段/主動主迴圈擲骰(fire=rnd()<t0.rate)/charge突擊等
      // 一般路徑被無條件套用。過去(草船借箭0分bug之一)heal 的 e.when.on 只被 heal 分支自己
      // 內部的 opt.healOnly 閘門過濾(見下方 k==="heal"), 但一般 active 主動戰法擲骰命中時
      // 呼叫 applyEffects() 完全不經過 opt.healOnly, 導致帶 e.when.on 的 heal 效果被當成
      // 「無 when 的常駐效果」在戰法觸發當下立即無條件治療一次, 與 onHit 反應式觸發疊加,
      // 造成雙重結算。此處統一擋下: 非 opt.reactive 呼叫時, 任何 k 只要帶 e.when.on 就跳過
      // (改由 onHit() 事件點才會結算, 見 onHitEffectTacs/onHitEq/onHitBs 呼叫端)。
      if (!opt.reactive && e.when && e.when.on) continue;
      // 批23 A4: 效果級 e.rate 折算一致性 —— 過去只有 onHit(反應式)/delayedEq(裝備回合窗)
      // 兩條路徑會讀 e.rate(見呼叫端各自的 evRate = e.rate ?? t.rate ?? 1 判定), 其餘路徑
      // (prep/active主動/charge突擊/when視窗一次性套用)完全忽略 e.rate, 造成同一戰法內
      // 「有的效果段折機率、有的沒折」(如草船借箭80%/魚鱗陣heal段25%/援救50%)。修法: 在
      // 這裡統一補上判定(套用時 rnd()<e.rate, 比EV折算更接近真實方差, 見批23 A4 brief)。
      // opt.rateChecked: 呼叫端(onHit/delayedEq 的合成單效果呼叫)已自行讀取並擲骰過同一個
      // e.rate, 傳此旗標避免在這裡對同一效果重複擲骰(機率會被平方, 造成低估)。
      if (!opt.rateChecked && e.rate != null && rnd() >= e.rate) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔${Math.round(e.rate * 100)}%機率〕未觸發`); continue; }
      // 批26: e.ifLeader —— 效果級「施放者須為隊伍主將(index 0)」條件閘門。原文常見「自身為
      // 主將時，額外XX」措辭(南蠻渠魁/江東小霸王/酒池肉林等), 過去無對應原語, 該效果段只能
      // 被迫「無條件對所有施放者套用」(高估非主將情形)或完全不建模。allies[0] 是隊伍主將慣例
      // (同 who==="leader" 分支既有假設), 只在 caster 就是 allies[0] 時才放行本效果段。
      if (e.ifLeader && !(allies && allies[0] === caster)) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔限主將〕${caster.nm}非主將, 未觸發`); continue; }
      // 批30 A: 非heal效果的逐回合重擲通道(e.everyRound) —— 過去只有 k==="heal" 在
      // opt.healOnly(見 applyPassives 的逐回合呼叫)這條路徑下逐回合重新掃描/擲骰套用, 其餘
      // k(amp/mitig/block/stat/...)一旦在 prep 套用一次就不會再被重新判定, 導致「每回合X%
      // 機率獲得1次抵禦/減傷」類戰法(機鑑先識/揮兵謀勝/魚鱗陣/枕戈坐甲等, 見
      // engine_limitations.md 第11節/25節)只能 EV 折算或截斷成一次性。修法: 把 heal 既有的
      // 「when視窗判定 + rounds去重 + rate擲骰」邏輯泛化成任何 k 皆可掛的通用閘門, 用
      // e.everyRound(效果級旗標, opt-in)標記「這個效果不在 prep 套用一次, 改在每回合常駐
      // 通道重新判定」。與 heal 共用同一份 healRoundsFired/whenFired 去重狀態(鍵是效果物件
      // 本身, heal 與 everyRound 不會撞鍵, 因為同一個效果物件只會是其中一種)。
      //
      // 語意與 heal 完全對稱: opt.healOnly 模式下, 非 heal 效果只有帶 e.everyRound 才會走到
      // 這裡(否則在函式開頭的 top-level k!=="heal" 過濾就被跳過); 帶 everyRound 的效果在
      // **非** opt.healOnly 呼叫路徑(prep/active/charge/when視窗)一律跳過(不套用), 因為它
      // 只該由 opt.healOnly 常駐通道結算 —— 對稱於 heal 在其他路徑各自決定是否觸發、不依賴
      // 這裡的慣例。
      if (e.everyRound && k !== "heal") {
        if (!opt.healOnly) continue;
        const hw = e.when || t.when;
        if (hw) {
          if (!roundOk({ when: hw }, CUR_R)) continue;
          if (hw.rounds) {
            if (!caster.healRoundsFired) caster.healRoundsFired = new Map();
            let seen = caster.healRoundsFired.get(e);
            if (!seen) { seen = new Set(); caster.healRoundsFired.set(e, seen); }
            if (seen.has(CUR_R)) continue;
            seen.add(CUR_R);
          }
        } else if (e.once) {
          if (caster.whenFired.has(e)) continue;
          caster.whenFired.add(e);
        }
        const evRate = e.rate ?? t.rate ?? 1;
        if (rnd() >= evRate) { if (TRACE) lg(`　▸ ${caster.nm} 〔${t.nameZh || "?"}〕每回合判定〔${Math.round(evRate * 100)}%機率〕未觸發`); continue; }
        // 通過閘門後不 continue —— 落到下方通用 who/dests 派發邏輯(amp/mitig/block/...),
        // 走與 prep 套用相同的效果分派, 只是改成每回合重新判定/套用一次。
      }
      if (k === "heal") {
        if (opt.noHeal) continue;
        if ((e.coef ?? 0.8) < 0) continue;              // 批10: 資料衛生防禦 —— 負 heal coef(如機略縱橫類 dot 誤標成 heal 負值)一律視為0並跳過, 避免資料錯誤反而扣友軍血
        // 批15: 指揮/被動的 heal 在 opt.healOnly(每回合無條件常駐掃描, 見 applyPassives 的
        // 逐回合呼叫)這條路徑下, 過去無視 t.when/t.rate/e.once, 每回合必定結算 —— 「第N回合
        // 治療一次」類戰法(如撫輯軍民/桃園結義/士別三日)被無聲放大成每回合治療(~8倍/回合數倍)。
        // 修正: 僅在 healOnly 常駐路徑套用下列語意閘門(其餘呼叫路徑, 如 when 視窗一次性套用/
        // active主動/charge突擊/onHit反應式, 呼叫前已各自決定是否該觸發, 不應再被此處二次過濾):
        //   1) e.when(效果級, 優先) 或 t.when(戰法級) 存在 → 用 roundOk 檢查回合是否落在視窗
        //      內, 不符合則本回合不治療。e.when 用途: 同一戰法內其餘效果(如撫輯軍民的
        //      mitig/amp)是「前3回合就生效」的常駐buff(無 when, 準備階段套用), 但 heal 段是
        //      「第4回合單次觸發」—— 兩者時間窗不同, 不能共用同一個 t.when(會連帶把 mitig/amp
        //      也延後到第4回合才套用), 故 heal 效果自己帶 e.when 覆蓋, 不影響同戰法其他效果
        //      的準備階段套用時機。
        //      - when.rounds(明確列出的單一/多個回合, 如「第4回合」「第3、5回合」): 語意是
        //        「只在這些特定回合各觸發一次」, 用 whenFired(效果+回合組合去重, 同 delayedEq
        //        慣例)確保 rounds:[3,5] 這種多回合列表在第3、第5回合各自觸發一次、不重複、
        //        也不會在其他回合誤觸發。
        //      - when.from/until(範圍視窗, 如「第3回合起, 持續3回合」「第5回合起」): 語意是
        //        「這幾回合每回合都要治療」(休整/持續恢復類戰法, 如金丹秘術/詐降/魚鱗陣),
        //        故只用 roundOk 檢查是否在窗內, 不做 whenFired 去重(讓窗內每回合都能重新
        //        擲骰/治療)。
        //   2) e.once === true(單次治療語意, 無 when 亦適用) → 觸發過一次即不再結算, 同樣用
        //      whenFired 去重。
        //   3) 無 when(e.when/t.when 皆無)且無 e.once → 維持原行為: 每回合持續治療(急救/
        //      休整類戰法本意如此)。
        //   以上都通過後才擲 t.rate 骰(rate<1 時只有部分回合真正治療, 而非年年必中)。
        if (opt.healOnly) {
          const hw = e.when || t.when;
          if (hw) {
            if (!roundOk({ when: hw }, CUR_R)) continue;
            if (hw.rounds) {                              // 明確列出的特定回合: 每個列出的回合各觸發一次(回合特定去重鍵, 而非整場只觸發一次)
              if (!caster.healRoundsFired) caster.healRoundsFired = new Map();  // Map<effect物件, Set<已觸發的回合數>>, 惰性建立(僅 rounds 型 heal 需要)
              let seen = caster.healRoundsFired.get(e);
              if (!seen) { seen = new Set(); caster.healRoundsFired.set(e, seen); }
              if (seen.has(CUR_R)) continue;
              seen.add(CUR_R);
            }
            // from/until(範圍視窗): 不去重, 窗內每回合都可能治療(休整類戰法本意如此, 如金丹秘術/詐降/魚鱗陣)
          } else if (e.once) {
            if (caster.whenFired.has(e)) continue;
            caster.whenFired.add(e);
          }
          if (rnd() >= (t.rate ?? 1)) continue;
        }
        let hurt = null;
        for (const a of allies) if (a.alive && !a.healblock && (!hurt || a.troop < hurt.troop)) hurt = a;  // 批8: 禁療(healblock) 中的目標跳過, 不參與「最殘一人」篩選
        if (hurt) {
          const before = hurt.troop;
          const hcoef = (e.coef ?? 0.8) * (e.scale ? scaleOf(caster, e.scale) : 1);
          // 批16: healBoost/healGiven —— 目標受到的治療量×(1+healBoost加總), 施放者施放的治療×(1+healGiven加總)
          const boostMult = Math.max(0, 1 + hurt.addbonus("healBoost")) * Math.max(0, 1 + caster.addbonus("healGiven"));
          const want = hcoef * caster.troop * 0.10 * boostMult;
          // 批18: 傷兵池 —— 治療只能回復傷兵池裡的量(可救援的傷兵), 不是無限回滿。實際回復 =
          // min(想治療量, 傷兵池餘量, 距滿編差額); 回復後從傷兵池扣掉對應量(此人已被救回, 不再
          // 算在池裡)。這會全域削弱治療(尤其後期, 陣亡比例升高、傷兵池餘量變少), 屬預期真實化。
          const actual = Math.max(0, Math.min(want, hurt.wounded, START_TROOP - hurt.troop));
          hurt.troop += actual; hurt.wounded -= actual;
          if (TRACE && hurt.troop - before >= 1) lg(`　▸ 治療 ${hurt.nm} +${Math.round(hurt.troop - before)}(傷兵池餘${Math.round(hurt.wounded)})` + (e.scale ? `（受${STAT_ZH[e.scale] || e.scale}影響, 實際治療率${Math.round(hcoef * 100)}%）` : "") + (boostMult !== 1 ? `（治療加成×${boostMult.toFixed(2)}）` : ""));
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
      // 批23 A1: 效果級 e.n(可配 e.nMax) —— 非CTRL效果(amp/mitig/stat/dot/healblock/rateup/…)
      // 過去無條件把 who:"enemy"/"ally" 放大成全體敵軍/我軍, 大量原文寫「單體」「目標」「我軍
      // 2人」的非控制效果被系統性高估成全體(見批23清單: 謙讓/殿後/破甲/談心/追傷/兵鋒/舌戰
      // 群儒/八門金鎖陣/進言/江東小霸王/眾動萬計/國士將風等)。修法: 有 e.n 時比照 CTRL_K
      // 群體控制的既有選標邏輯(pickTargets 隨機不重複; 單體時優先鎖定 tgt, 與 CTRL_K 慣例
      // 一致), 只是讀 e.n/e.nMax(效果自身欄位)而非 t.n/t.nMax(戰法頂層, CTRL_K 專用, 維持
      // 不變)。無 e.n 時完全維持原行為(全體敵軍/我軍), 向後相容 —— 大量「全體」條目依賴現行為。
      const hasEN = e.n != null;
      // 批18: targetSel(指定選標準則) —— 效果級欄位, 優先於 who 的預設隨機/群體邏輯: 依準則
      // (兵力最低/武力最高/智力最低/我方最殘等)在對應陣營(enemy用敵方, 其餘用我方)挑單一目標。
      // 「指定」不受混亂(chaos)影響(混亂只亂「隨機」選目標的普攻/主動/突擊, 見 pickTargetChaos
      // 呼叫端與本函式頂層 tgt 參數 —— targetSel 在此處直接決定 dests, 完全不經過受混亂影響的
      // tgt/pickTargets 隨機路徑)。
      if (e.targetSel) {
        const pool = who === "enemy" ? enemies : allies;
        const picked = pickByCriterion(pool, e.targetSel);
        dests = picked ? [picked] : [];
      }
      else if (who === "self") dests = caster.alive ? [caster] : [];
      else if (who === "leader") dests = (allies[0] && allies[0].alive) ? [allies[0]] : [];  // 批8: 主將限定(隊伍 index 0)
      else if (who === "subs") dests = allies.slice(1).filter(a => a.alive);  // 批13: 副將群限定(隊伍 index 0 以外; 如鋒矢陣/箕形陣副將分化段)
      // 批30 C: who:"sub1"/"sub2"(副將固定位置分派) —— 「subs」只能讓兩名副將套用同一份效果,
      // 無法表達「副將A只防兵刃, 副將B只防謀略」這種依隊伍固定位置(而非動態屬性準則)分派相異
      // 效果的語意(見箕形陣, engine_limitations.md 第25節/16節)。sub1=allies[1](副將A,
      // index 1), sub2=allies[2](副將B, index 2), 對稱於既有 who:"leader"=allies[0] 慣例。
      // 三人隊固定編制(index 0=主將/1/2=副將), 若隊伍不足3人或該位置陣亡則 dests 為空陣列。
      else if (who === "sub1") dests = (allies[1] && allies[1].alive) ? [allies[1]] : [];
      else if (who === "sub2") dests = (allies[2] && allies[2].alive) ? [allies[2]] : [];
      else if (who === "enemy") {
        if (CTRL_K) {                                 // 群體控制(n>1 或有 nMax)隨機挑不重複目標; 單體優先鎖定 tgt
          // 批26: CTRL類效果優先讀 e.n/e.nMax(效果自身欄位), 無則fallback到t.n/t.nMax(戰法
          // 頂層, 舊行為, 向後相容)。原本CTRL_K只認頂層n/nMax, 導致同一戰法內「多段各自不同
          // 目標數的chaos/stun等控制效果」(如神機莫測「1名必中混亂 + 另外N名各自獨立機率判定
          // 混亂」)無法用單一戰法頂層n表達出兩種不同的目標數, 被迫二選一近似成同一個n。
          const n = hasEN ? e.n : (t.n || 1);
          const nMax = hasEN ? e.nMax : t.nMax;
          const cnt = nMax ? n + Math.floor(rnd() * (nMax - n + 1)) : n;
          dests = cnt <= 1 ? (tgt && tgt.alive ? [tgt] : pickTargets(enemies, 1)) : pickTargets(enemies, cnt);
        } else if (hasEN) {                            // 批23 A1: 非CTRL效果讀 e.n/e.nMax
          const cnt = e.nMax ? e.n + Math.floor(rnd() * (e.nMax - e.n + 1)) : e.n;
          dests = cnt <= 1 ? (tgt && tgt.alive ? [tgt] : pickTargets(enemies, 1)) : pickTargets(enemies, cnt);
        } else dests = enemies.filter(x => x.alive);
      }
      else if (hasEN) {                                 // 批23 A1: who="ally"(含預設) 非CTRL效果讀 e.n/e.nMax(如「我軍2人」「自己及友軍單體」)
        const cnt = e.nMax ? e.n + Math.floor(rnd() * (e.nMax - e.n + 1)) : e.n;
        dests = cnt <= 1 ? (tgt && tgt.alive && allies.includes(tgt) ? [tgt] : pickTargets(allies, 1)) : pickTargets(allies, cnt);
      }
      else dests = allies.filter(a => a.alive);
      // 批16: ifTargetHas —— 效果段條件, 只對「已有該狀態」的目標生效; 選目標後過濾(不影響選目標邏輯本身)
      if (e.ifTargetHas) dests = dests.filter(u => targetHas(u, e.ifTargetHas));
      if (TRACE && dests.length) lg(`　▸ ${effDesc(k, e, caster)} → ${dests.map(u => u.nm).join("、")}`);
      // scale:"intel"|"force"|"command"|"speed"|"charm" 縮放(以施放者戰鬥內即時素質為準):
      // amp/mitig 的 val 直接乘 SCALE, clamp 到 ±SCALE_CLAMP 防止極端值; stat 的 mult 對
      // 1.0 的偏移量(增益/削弱幅度)乘 SCALE, 1.0 本身(無效果)不受縮放影響。
      const svVal = v => e.scale ? Math.max(-SCALE_CLAMP, Math.min(SCALE_CLAMP, v * scaleOf(caster, e.scale))) : v;
      const svMult = m => e.scale ? 1 + (m - 1) * scaleOf(caster, e.scale) : m;
      const svAdd = a => e.scale ? a * scaleOf(caster, e.scale) : a;  // 屬性平加縮放(如未來 scale 平加); 一般裝備平加無 scale 直接用原值
      // 批16: undispellable 旗標 —— 效果加此欄則 dispel 略過(附加進 pushAdd/pushMod 的 flags, 供 dispelUnit 讀取)
      // 批24 D2: dmgType 旗標 —— amp/mitig 效果可選填 e.dmgType:"phys"|"intel", 限定只對該類型
      // 傷害生效(damage() 結算時依 kind 過濾, 見 amp()/addbonus() 的 dmgType 參數)。與
      // undispellable 合併進同一個 flags 物件(pushAdd/pushMod 第5參數), 兩者互不干擾。
      // 批28 B3: normalOnly 旗標 —— amp/mitig 效果可選填 e.normalOnly:true, 限定只對普攻傷害
      // 生效/受影響(見至柔動剛「降低我軍及敵軍全體普通攻擊傷害35%」)。
      const normalOnly = (k === "amp" || k === "mitig") && !!e.normalOnly;
      const udFlags = (e.undispellable || e.dmgType || normalOnly) ? { undispellable: !!e.undispellable, dmgType: e.dmgType, normalOnly } : undefined;
      // dmgType 存在時, src 附加類型尾碼區分 dedup key(同一戰法內若有兩條不同 dmgType 的
      // amp/mitig, 如暫避其鋒「智力最高者減兵刃傷害」+「武力最高者減謀略傷害」, 兩者若共用
      // 同一個 src(戰法名)會被 pushAdd 的「同kind+同src刷新」去重機制互相蓋掉, 見 rateup 的
      // 既有 prepOnly/nativeOnly 尾碼慣例同理)。批28 B3: normalOnly 同理附加尾碼。
      let dtSrc = (src && e.dmgType) ? src + ":" + e.dmgType : src;
      if (normalOnly && src) dtSrc = (dtSrc || src) + ":normalOnly";
      for (const u of dests) {
        if (k === "amp") { const v = svVal(e.val); who === "enemy" && v > 0 ? u.pushAdd("mitig", -v, e.dur, dtSrc, udFlags) : u.pushAdd("amp", v, e.dur, dtSrc, udFlags); }
        else if (k === "mitig") u.pushAdd("mitig", svVal(e.val), e.dur, dtSrc, udFlags);
        // 批16: immuneTo(單項控制免疫) —— isImmuneTo(k) 只免疫清單內控制類型, 與 insight(全免) 並列判斷
        else if (k === "stun") { if (!u.insight && !u.isImmuneTo("stun")) { u.stun = Math.max(u.stun, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入震懾(全禁)`); } else if (TRACE) lg(`　▸ ${u.nm} 免疫震懾`); }
        else if (k === "silence") { if (!u.insight && !u.isImmuneTo("silence")) { u.silence = Math.max(u.silence, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入計窮(禁主動戰法)`); } else if (TRACE) lg(`　▸ ${u.nm} 免疫計窮`); }
        else if (k === "disarm") { if (!u.insight && !u.isImmuneTo("disarm")) { u.disarm = Math.max(u.disarm, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入繳械(禁普攻)`); } else if (TRACE) lg(`　▸ ${u.nm} 免疫繳械`); }
        else if (k === "chaos") { if (!u.insight && !u.isImmuneTo("chaos")) { u.chaos = Math.max(u.chaos, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入混亂(敵我不分)`); } else if (TRACE) lg(`　▸ ${u.nm} 免疫混亂`); }  // 批12 ModeF
        else if (k === "insight") { u.insight = Math.max(u.insight, (e.dur ?? 1) + 1); u.stun = 0; u.silence = 0; u.disarm = 0; u.chaos = 0; u.ambush = 0; }
        else if (k === "immune") { u.pushImmune(e.types, e.dur); if (TRACE) lg(`　▸ ${u.nm} 獲得控制免疫〔${(e.types || []).join("、")}〕`); }  // 批16: immuneTo
        else if (k === "first") u.first = Math.max(u.first, e.dur ?? 1);
        // 批18: ambush(遇襲, 先攻的反面/遲緩) —— 不鎖行動(仍可行動), 只影響排序(見 fight() 的
        // effFirst 三檔排序鍵)。insight(全免)/immuneTo(單項免疫)可免, 同其他控制類慣例。
        else if (k === "ambush") { if (!u.insight && !u.isImmuneTo("ambush")) { u.ambush = Math.max(u.ambush, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入遇襲(行動遲滯)`); } else if (TRACE) lg(`　▸ ${u.nm} 免疫遇襲`); }
        else if (k === "stat") { if (e.add != null) u.pushStatAdd(e.stat, svAdd(e.add), e.dur, src, udFlags); else u.pushMod(e.stat, svMult(e.mult ?? 1), e.dur, src, udFlags); }  // 裝備平加(add)與乘算(mult)擇一; add 為戰報所示「裝備獨立平加階段」
        // 批23 A3: dot 結算優先讀 e.kind(戰法整體是兵刃 t.kind="phys", 但灼燒/水攻類 dot 段
        // 依原文「受智力影響」應走謀略傷害類型, 過去誤用 t.kind 導致傷害類型錯位, 如天降火雨
        // 兵刃戰法掛的灼燒本應是 intel 類, 若戰法整體改記 kind="phys" 會連帶把 dot 也算成
        // 兵刃), 無 e.kind 時 fallback t.kind(向後相容既有無 e.kind 的 dot 資料)。
        else if (k === "dot") u.dots.push([damage(caster, u, e.coef ?? 0.5, e.kind || t.kind || "intel"), e.dur, !!e.undispellable]);  // dots[2]=undispellable 旗標(供 dispel 略過)
        else if (k === "extra") u.pushAdd("extra", e.val, e.dur, src);
        // 批26 B2: e.stackPer(可選, "round"預設/"cast") —— 過去疊層只有「每回合+1層」(見下方
        // fight() 主迴圈 tick 遞增), 原文常見「每次發動後傷害率提升X」(如水淹七軍/陷陣突襲)是
        // 「本戰法每次成功發動」才+1層, 與回合數無關。"cast"模式改由 applyStackCast() 在戰法
        // 命中/發動結算處呼叫遞增(見 fight() fire 分支)。刻意不覆寫既有 e.per 語意(per 一直是
        // "每層增傷倍率"數值欄位), 新增獨立欄位避免型別混淆。
        else if (k === "stack") u.stack = { per: e.per ?? 0.1, max: e.max ?? 5, n: 0, stackPer: e.stackPer || "round" };
        else if (k === "decay") u.decay = { v0: e.v0 ?? 0.5, left: e.rounds ?? 8, total: e.rounds ?? 8 };
        else if (k === "swap") u.swap = Math.max(u.swap, (e.dur ?? 1) + 1);
        else if (k === "pierce") u.pushAdd("pierce", e.val, e.dur, src);
        // 批23 A2: counter 讀 e.dur(過去是幽靈欄位, 從不寫入/遞減 —— 「反擊持續1回合」等
        // 帶時限的反擊被無聲變成常駐/永久, 見還擊/千里走單騎等)。dur 預設99(=常駐被動慣例,
        // 向後相容無 dur 欄位的既有反擊資料)。dur 記在 counter 物件上, tick() 逐回合遞減,
        // 歸零時清除(見 tick() 對應段落)。
        // 批28 B1: guardFor(守護式反擊) —— e.guardFor === "leader" 時, u(此效果解析出的
        // who=subs等目標)不掛自己的counter, 改把自己登記進「隊伍主將」的 counterGuards
        // 清單, 由 hit() 在主將受擊時代為觸發還擊(見虎衛軍「我軍主將即將受到普攻時, 副將
        // ...對攻擊者造成兵刃傷害」)。只支援 guardFor:"leader", 其餘 who 仍走原本路徑。
        else if (k === "counter") {
          if (e.guardFor === "leader" && allies.length && allies[0].alive) {
            allies[0].counterGuards.push({ unit: u, coef: e.coef ?? 1, kind: e.kind || "phys", prob: e.prob ?? 1 });
          } else {
            u.counter = { coef: e.coef ?? 1, kind: e.kind || "phys", prob: e.prob ?? 1, dur: (e.dur ?? 99) + 1 };
          }
        }
        else if (k === "taunt") { u.tauntBy = caster; u.tauntDur = Math.max(u.tauntDur, (e.dur ?? 1) + 1); }
        else if (k === "shield") {
          const amt = (e.amt ?? 0) + (e.pct ? e.pct * caster.troop : 0);
          u.shield = { amt: (u.shield ? u.shield.amt : 0) + amt, dur: (e.dur ?? 99) + 1, undispellable: !!e.undispellable };  // +1 補償: tick 施加當回合末即扣1, 與 taunt/dodge/surehit 慣例一致
        }
        else if (k === "dodge") { u.dodgeProb = e.prob ?? 0.2; u.dodgeDur = Math.max(u.dodgeDur, (e.dur ?? 1) + 1); }
        // 批22: block(次數型格擋, 抵禦/警戒同族) —— times:N(剩餘次數), val:1.0全擋/0.x部分減傷
        // (如警戒基礎40%受智力影響)。同源(同一戰法名 src)再次施加時疊加次數(pushBlock 內部
        // 處理), 不像 pushAdd/pushMod 的「同源刷新覆蓋」慣例 —— 貼合戰報「目前抵禦總次數為N」
        // 的疊次語意。val 的 scale 縮放用 0~1 專屬 clamp(非 svVal 的 ±SCALE_CLAMP, 因 block
        // val 是「減傷比例」語意, 不應為負值或超過1.0全擋)。
        else if (k === "block") {
          const bVal = e.scale ? Math.max(0, Math.min(1, (e.val ?? 1.0) * scaleOf(caster, e.scale))) : (e.val ?? 1.0);
          u.pushBlock(bVal, e.times ?? 1, src);
          if (TRACE) lg(`　▸ ${u.nm} 獲得${bVal >= 1 ? "抵禦" : `警戒(減傷${Math.round(bVal * 100)}%)`}(${e.times ?? 1}次)`);
        }
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
        // 批16: healBoost(受到的治療×(1+val)) / healGiven(施放的治療×(1+val)) —— 掛加成值, 實際套用在 heal 結算處(applyEffects 開頭 heal 分支)
        else if (k === "healBoost") u.pushAdd("healBoost", e.val, e.dur, src);
        else if (k === "healGiven") u.pushAdd("healGiven", e.val, e.dur, src);
        // 批16: fakeReport(偽報) —— 中招者被動+指揮戰法失效: 每回合擲骰的coef段與onHit反應被抑制
        // (prep已套用效果不回收, 近似)。insight 可免(同其他控制類慣例)。
        // 批22: 偽報疊加規則(戰報實測「身上已存在同等或更強的偽報效果」→不覆蓋) —— 新 dur
        // (以 e.dur 近似「強度」, 剩餘回合越多視為越強)必須 > 現有 fakeReportDur 才覆蓋,
        // 否則本次施加跳過(不刷新/不縮短), 並 TRACE 記錄「已存在同等或更強」。與既有其他控制類
        // (stun/silence/…)的 Math.max 慣例不同: 那些是「取較大值」語意(仍會刷新到較大值本身,
        // 只是不會變小), 偽報是「若新的不夠強則完全不生效」(連刷新都不做), 貼合原文用詞
        // 「已存在同等或更強的偽報效果」暗示的二元判定(而非簡單取max, 雖數值結果與取max相同,
        // 但需要能表達「本次施加被完全拒絕」的語意與對應TRACE訊息)。
        else if (k === "fakeReport") {
          if (u.insight) { if (TRACE) lg(`　▸ ${u.nm} 洞察免疫偽報`); }
          else {
            const newDur = (e.dur ?? 1) + 1;
            if (newDur > u.fakeReportDur) { u.fakeReportDur = newDur; if (TRACE) lg(`　▸ ${u.nm} 陷入偽報(被動/指揮戰法失效)`); }
            else if (TRACE) lg(`　▸ ${u.nm} 身上已存在同等或更強的偽報效果，本次不覆蓋`);
          }
        }
        // 批16: dispel(驅散/淨化) —— 移除目標 adds/mods/dots/控制欄位中對應方向(buffs/debuffs)的條目,
        // 略過標記 undispellable 的條目(adds/mods 第6欄, dots 第3欄)。
        else if (k === "dispel") dispelUnit(u, e.what || "debuffs");
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
    const factionsA = teamA.map(n => POOL[n].faction), factionsB = teamB.map(n => POOL[n].faction);  // 批24 D1: teamGate 判定依據(隊伍全體陣營陣列)
    const A = teamA.map((n, i) => Object.assign(new Unit(POOL[n], troopA, bsA[i], eqA[i], addA[i], inhA[i], seasonModsFor(POOL, POOL[n], i, teamA, scenario), factionsA), { nm: n, side: "我" }));
    const B = teamB.map((n, i) => Object.assign(new Unit(POOL[n], troopB, bsB[i], eqB[i], addB[i], inhB[i], seasonModsFor(POOL, POOL[n], i, teamB, scenario), factionsB), { nm: n, side: "敵" }));
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
      if (!dst.alive || (!dst.onHitTacs.length && !dst.onHitEffectTacs.length && !dst.onHitEq.length && !dst.onHitBs.length)) return;
      if (dst.fakeReportDur) return;                 // 批16: 偽報 —— 抑制 onHit 反應式觸發(被動/指揮戰法失效)
      for (const t0 of dst.onHitTacs) {
        if (t0.when.on === "attacked" && !isNormal) continue;   // attacked: 限普通攻擊觸發; damaged: 任意傷害都觸發
        // 批22: when.on 反應式戰法過去完全不檢查 rounds/from/until/parity/every(只認 on 事件本身),
        // 導致「戰鬥首回合獲得急救(受傷時回血)」這類「反應式觸發+回合窗口限定」的複合語意無法
        // 表達(如 長健/青囊書: 首回合內受傷才會回血, 而非全程)。roundOk() 對「無 rounds/from/
        // until/parity/every」的戰法一律回傳 true(見其實作), 故此檢查對絕大多數既有 when.on
        // 戰法(只帶 on, 無回合欄位)是無副作用的 no-op, 只在新資料明確加上回合窗口時才生效。
        if (!roundOk(t0, CUR_R)) continue;
        if (dst.hitFlags.has(t0)) continue;             // 同回合每單位每戰法最多觸發1次(防無限鏈), 鍵用t0(戰法原始物件)不受choices合成視圖影響
        if (rnd() >= t0.rate) continue;
        dst.hitFlags.add(t0);
        // 批27 C: choices(擇一分支) —— 過去 onHit() 反應式路徑完全不讀 t0.choices(見
        // engine_limitations.md §8: 魅惑「混亂/計窮/虛弱」三選一只能固定選其中一種, choices
        // 寫入也不會被消費), 主動/指揮/被動的常駐輪詢派發路徑(fight()主迴圈)已支援 choices,
        // 這裡補上同一套邏輯(先 pickChoice 選分支, 再用合成視圖 t 讀 coef/kind/effects/
        // extraHits, t0 保留給 hitFlags 去重/roundOk 等以物件本身為鍵的邏輯, 不受選分支影響)。
        const t = t0.choices ? Object.assign({}, t0, pickChoice(t0.choices)) : t0;
        if (TRACE) lg(`【${dst.side}】${dst.nm} 戰法【${t.nameZh}】（受擊觸發）發動`);
        if (t.coef) hit(dst, src, t.coef, t.kind, false, onHit, dealtDamage);
        if (t.extraHits) fireExtraHits(dst, t, src, alliesOf, foesOf, onHit, dealtDamage);  // 批13: 受擊觸發類多段傷害(如剛烈不屈 反擊後群體額外段)
        if (t.effects.length) applyEffects(dst, src, t, alliesOf(dst), foesOf(dst), { reactive: true });  // 批23: 戰法級when.on本身即反應式, 標記reactive供內部e.when.on效果(若有)一致判定
      }
      // 批22: 效果級 e.when.on(急救類反應式治療, 見 onHitEffectTacs 註解) —— 戰法本身無 t.when
      // (其餘效果如武力/統率平加仍在 prep 正常套用, 不受影響), 只有帶 e.when.on 的個別效果在
      // 此處反應式結算。用「合成單效果戰法」(effects:[e])呼叫 applyEffects, 讓 heal 分支的
      // 傷兵池/healBoost/healGiven 邏輯完整適用, 觸發率取 e.rate ?? t.rate ?? 1(效果自身優先,
      // 無則沿用戰法整體 rate)。去重鍵用效果物件本身(而非戰法物件), 因同一戰法可能有多個
      // e.when.on 效果, 需各自獨立節流(防同回合多次觸發同一效果)。
      for (const t of dst.onHitEffectTacs) {
        for (const e of t.effects) {
          if (!e.when || !e.when.on) continue;
          if (e.when.on === "attacked" && !isNormal) continue;
          if (!roundOk({ when: e.when }, CUR_R)) continue;
          if (dst.hitFlags.has(e)) continue;
          const evRate = e.rate ?? t.rate ?? 1;
          if (rnd() >= evRate) continue;
          dst.hitFlags.add(e);
          if (TRACE) lg(`【${dst.side}】${dst.nm} 戰法【${t.nameZh}】急救效果（受擊觸發）發動`);
          applyEffects(dst, src, { effects: [e], kind: t.kind || "phys", nameZh: t.nameZh }, alliesOf(dst), foesOf(dst), { rateChecked: true, reactive: true });  // 批23 A4: 這裡已擲過 e.rate, 避免 applyEffects 通用閘門重複擲骰; reactive:true 供內部 e.when.on 閘門判定放行
        }
      }
      // 批22: 裝備效果級 e.when.on(見 onHitEq 註解) —— 同上, 用合成單效果戰法呼叫 applyEffects
      for (const e of dst.onHitEq) {
        if (e.when.on === "attacked" && !isNormal) continue;
        if (!roundOk({ when: e.when }, CUR_R)) continue;
        if (dst.hitFlags.has(e)) continue;
        const evRate = e.rate ?? 1;
        if (rnd() >= evRate) continue;
        dst.hitFlags.add(e);
        if (TRACE) lg(`【${dst.side}】${dst.nm}〔特技·${e._eqNm || "?"}〕（受擊觸發）發動`);
        applyEffects(dst, src, { effects: [e], kind: "phys" }, alliesOf(dst), foesOf(dst), { rateChecked: true, reactive: true });  // 批23 A4/reactive: 已擲過 e.rate
      }
      // 批22: 兵書效果級 e.when.on(見 onHitBs 註解) —— 同上, 用合成單效果戰法呼叫 applyEffects
      for (const e of dst.onHitBs) {
        if (e.when.on === "attacked" && !isNormal) continue;
        if (!roundOk({ when: e.when }, CUR_R)) continue;
        if (dst.hitFlags.has(e)) continue;
        const evRate = e.rate ?? 1;
        if (rnd() >= evRate) continue;
        dst.hitFlags.add(e);
        if (TRACE) lg(`【${dst.side}】${dst.nm}〔兵書〕（受擊觸發）發動`);
        applyEffects(dst, src, { effects: [e], kind: "phys" }, alliesOf(dst), foesOf(dst), { rateChecked: true, reactive: true });  // 批23 A4/reactive: 已擲過 e.rate
      }
    };
    const dealtDamage = (src, dst, isNormal, kind) => {  // 批27 A: 反應式觸發(when.on:"dealtDamage") —— 自己造成傷害(對 dst)後掛到 hit() 事件點, 與 onHit(自己受擊視角)對稱
      if (!src.alive || (!src.onDealTacs.length && !src.onDealEffectTacs.length)) return;
      if (src.fakeReportDur) return;                 // 批16: 偽報 —— 抑制反應式觸發(被動/指揮戰法失效), 與 onHit 同慣例
      const dmgTypeOk = dt => !dt || dt === kind;     // dmgType 過濾: 未指定視為兵刃/謀略皆可觸發(向後相容)
      for (const t of src.onDealTacs) {               // 戰法級: 整個戰法都是「造成傷害時」反應式(如白衣渡江拆成兩個獨立戰法段時可用此形式)
        if (!dmgTypeOk(t.when.dmgType)) continue;
        if (!roundOk(t, CUR_R)) continue;
        if (src.hitFlags.has(t)) continue;            // 同回合每單位每戰法最多觸發1次(防無限鏈), 與 onHit 共用同一 hitFlags(不同方向的觸發各自用不同t/e鍵, 不會互相誤判)
        if (rnd() >= t.rate) continue;
        src.hitFlags.add(t);
        if (TRACE) lg(`【${src.side}】${src.nm} 戰法【${t.nameZh}】（造成傷害觸發）發動`);
        if (t.coef) hit(src, dst, t.coef, t.kind, false, onHit, dealtDamage);
        if (t.extraHits) fireExtraHits(src, t, dst, alliesOf, foesOf, onHit, dealtDamage);
        if (t.effects.length) applyEffects(src, dst, t, alliesOf(src), foesOf(src), { reactive: true });
      }
      // 效果級: 戰法本身有其他常駐效果, 只有部分效果段是「造成傷害時」反應式(如白衣渡江本身
      // 是常駐 command, disarm/silence 兩效果各自綁不同 dmgType, 與 onHitEffectTacs 同慣例)
      for (const t of src.onDealEffectTacs) {
        for (const e of t.effects) {
          if (!e.when || e.when.on !== "dealtDamage") continue;
          if (!dmgTypeOk(e.when.dmgType)) continue;
          if (!roundOk({ when: e.when }, CUR_R)) continue;
          if (src.hitFlags.has(e)) continue;
          const evRate = e.rate ?? t.rate ?? 1;
          if (rnd() >= evRate) continue;
          src.hitFlags.add(e);
          if (TRACE) lg(`【${src.side}】${src.nm} 戰法【${t.nameZh}】效果（造成傷害觸發）發動`);
          applyEffects(src, dst, { effects: [e], kind: t.kind || "phys", nameZh: t.nameZh }, alliesOf(src), foesOf(src), { rateChecked: true, reactive: true });  // 已擲過 e.rate, 避免重複擲骰; reactive 供 e.when.on 閘門放行
        }
      }
    };
    if (TRACE) {                                    // 準備階段標頭: 兵種 + 城建/陣營
      CUR_R = 0;
      lg(`〔採用兵種〕我方 ${troopA}兵　·　敵方 ${troopB}兵`);
      lg(`〔城建滿〕全員 武智統速 各+${CITY}　〔陣營滿〕全屬性 +${Math.round((FACTION - 1) * 100)}%`);
    }
    applyPassives({ noHeal: true, prep: true, skipWhenEffects: true });    // 依序套用並記錄各類戰法; skipWhenEffects: 批18 e.when泛化, 非heal效果帶e.when且母戰法無t.when時prep階段不套用, 改由回合迴圈通用掃描
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
      for (const u of [...A, ...B]) if (u.alive && u.stack && (u.stack.stackPer || "round") === "round") u.stack.n = Math.min(u.stack.max, u.stack.n + 1);  // 批26 B2: 僅stackPer=="round"(預設)才逐回合遞增, 向後相容
      applyPassives({ healOnly: true });
      for (const u of [...A, ...B]) {                 // 條件觸發(when.rounds/from/until): 窗口首次開啟時套用一次非傷害效果(dot/amp/…); when.on 為反應式, 不走此處
        if (!u.alive) continue;
        for (const t of u.tactics)
          if ((t.type === "passive" || t.type === "command") && t.when && !t.when.on && roundOk(t, r) && !u.whenFired.has(t)) {
            // 批16: hpPct —— when.hpBelow(一次性, 首次跨越即觸發, 用whenFired/hpBelowFired去重)
            // / when.hpAbove(持續窗, 不去重, 每回合條件成立都可能觸發, 故不進whenFired)。
            if (t.when.hpBelow != null || t.when.hpAbove != null) {
              if (!hpOk(t, u)) continue;
              if (t.when.hpBelow != null) { if (u.hpBelowFired.has(t)) continue; u.hpBelowFired.add(t); }
              // 批23 A5: 補 t.rate 判定(此路徑過去從不讀 t.rate, 機率戰法被當成必發)。hpAbove
              // 是持續窗(每回合都可能重新判定), 未中不消耗 whenFired(仍可下回合再擲); hpBelow
              // 是一次性(見上方 hpBelowFired 去重), 未中同樣不消耗 whenFired/hpBelowFired 之外
              // 的額外狀態(hpBelowFired 已在觸發判定前標記, 維持既有「首次跨越」語意不變)。
              if (rnd() >= (t.rate ?? 1)) { if (TRACE) lg(`【${u.side}】${u.nm}（第${r}回合條件）【${t.nameZh}】未發動`); continue; }
              // hpAbove: 不加入 whenFired(持續窗, 允許之後回合再次觸發), 直接套用後 continue 到下個戰法
              if (TRACE) lg(`【${u.side}】${u.nm}（第${r}回合兵力${t.when.hpBelow != null ? "低於" : "高於"}${Math.round((t.when.hpBelow ?? t.when.hpAbove) * 100)}%）發動【${t.nameZh}】`);
              applyEffects(u, null, t, alliesOf(u), foesOf(u), { noHeal: true });
              if (t.when.hpBelow != null) u.whenFired.add(t);  // hpBelow 一次性: 同時標記 whenFired 供其他路徑(如未來擴充)一致查詢
              continue;
            }
            // 批23 A5: 此路徑過去從不讀 t.rate —— 機率戰法(如盛氣凌敵 rate=1 不受影響, 但
            // 火神英風/鷹視狼顧一類 rate<1 的 when-gated 條目)一律當成必發, 高估命中率。
            // 主動/指揮/被動的「coef 傷害段」自有獨立擲骰(見下方主迴圈 fire=rnd()<t0.rate,
            // 已正確套用), 此處是「非coef、window開啟時一次性套用的 effects 段」, 需要補上
            // 同一份 t.rate 判定, 貼合原文機率語意(如「N%機率使目標...」)。未中同樣消耗
            // whenFired(此路徑本就是一次性視窗, 見函式頂端註解, 未中不重試, 與原「必發」相比
            // 現在只是「有機率完全不觸發」, 貼合原文機率語意)。
            u.whenFired.add(t);
            if (rnd() >= (t.rate ?? 1)) { if (TRACE) lg(`【${u.side}】${u.nm}（第${r}回合條件）【${t.nameZh}】未發動`); continue; }
            if (TRACE) lg(`【${u.side}】${u.nm}（第${r}回合條件）發動【${t.nameZh}】`);
            // 批15: noHeal:true —— heal 效果改由上面 applyPassives({healOnly:true}) 統一處理
            // (它自己會檢查 t.when/roundOk, 見 applyEffects 內 opt.healOnly 分支), 避免此處與
            // healOnly 常駐通道用不同的去重鍵(t vs e)各自判定, 造成同一 when 視窗開啟的回合
            // heal 被套用兩次(雙倍治療)。
            applyEffects(u, null, t, alliesOf(u), foesOf(u), { noHeal: true });
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
          applyEffects(u, null, { effects: [e], kind: "phys", n: e.n || 1, nMax: e.nMax || 0 }, alliesOf(u), foesOf(u), { noHeal: false, rateChecked: true });  // n/nMax傳遞: 群體控制(赳螑 敵軍群體2~3)按效果宣告選目標數; 批23 A4: 上一行已擲過 e.rate, 避免重複擲骰
        }
      }
      // 批18: e.when 泛化(非 heal 種類) —— 與上面 delayedEq 同一時點、同慣例: 掃描「母戰法無
      // t.when」的 passive/command 戰法, 找出其中帶 e.when 的非 heal 效果(prep 階段已被
      // skipWhenEffects 跳過, 這裡才是它們真正套用的時機點), 視窗開啟時一次性套用(whenFired
      // 以效果物件本身去重, 同 delayedEq/heal e.when.rounds 慣例)。heal 種類不進這裡(它有自己
      // 獨立的 opt.healOnly 常駐通道與去重機制, 見 applyEffects 內 k==="heal" 分支)。
      for (const u of [...A, ...B]) {
        if (!u.alive) continue;
        for (const t of u.tactics) {
          if (!(t.type === "passive" || t.type === "command") || t.when) continue;  // 母戰法有 t.when 的已由上面 t.when 掃描處理, 這裡只處理母戰法無 when 的情形
          for (const e of t.effects) {
            if (e.k === "heal" || !e.when) continue;
            if (!roundOk({ when: e.when }, r) || u.whenFired.has(e)) continue;
            if (e.when.rounds) u.whenFired.add(e);  // rounds(明確列出的特定回合): 一次性去重(同 delayedEq)
            // from/until(範圍視窗): 不加入 whenFired, 讓視窗內每回合都能重新套用(同 heal 的 from/until 慣例)
            if (TRACE) lg(`【${u.side}】${u.nm}（第${r}回合條件）〔${t.nameZh}〕效果段生效`);
            applyEffects(u, null, { effects: [e], kind: t.kind || "phys", n: t.n || 1, nMax: t.nMax || 0, nameZh: t.nameZh }, alliesOf(u), foesOf(u), { noHeal: true });
          }
        }
      }
      // 行動順序: 先攻(first)優先於速度; 同速平手隨機(先打亂再穩定排序, 修 A 隊固定先手偏差)
      // 批18: 遇襲(ambush, 先攻的反面/遲緩) —— 三檔 effFirst: 只有先攻→最先(1); 先攻+遇襲同時
      // 存在→抵消, 視為普通(0, 按速度排); 只有遇襲→排最後(-1, 遇襲者之間仍按速度排)。
      const effFirst = u => (u.first > 0 ? 1 : 0) - (u.ambush > 0 ? 1 : 0);
      const order = [...A, ...B].filter(u => u.alive);
      for (let i = order.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [order[i], order[j]] = [order[j], order[i]]; }
      order.sort((x, y) => (effFirst(y) - effFirst(x)) || (y.eff("speed") - x.eff("speed")));
      for (const u of order) {
        if (!u.alive) continue;
        if (u.stun) { lg(`【${u.side}】${u.nm} 被控制(震懾)，無法行動`); continue; }
        if (!pickTarget(foesOf(u))) break;
        if (u.silence && TRACE) lg(`【${u.side}】${u.nm} 陷入計窮，無法發動主動戰法`);
        if (!u.silence) for (const t0 of u.tactics) {   // 自帶 + 傳承: 各自獨立附加發動(計窮時跳過主動/指揮/被動)
          // 批16: fakeReport(偽報) —— 抑制指揮/被動每回合擲骰的coef段(prep已套用效果不回收, 不影響主動戰法)
          if ((t0.type === "command" || t0.type === "passive") && u.fakeReportDur) continue;
          let fire = false;
          // 批18: choices/extraHits 派發 —— coef=0 且頂層 effects 為空、內容完全放在 choices[].effects
          // 或 extraHits 裡的主動戰法(如三選一分支型/上兵伐謀式多段指定選標), 過去
          // (t0.coef || t0.effects.length) 兩者皆假則永遠不會觸發(choices/extraHits 只在 fire 之後
          // 才被讀取, 若從未 fire 等於整個戰法失效 —— 全庫掃描發現暗潮洶湧/暗潮湧動已是此模式且
          // 從未真正發動過)。加上 t0.choices.length / t0.extraHits.length 這兩個額外判斷條件, 讓
          // 「內容全在 choices/extraHits 裡」的戰法也能正常擲骰派發。
          if (t0.type === "active" && (t0.coef || t0.effects.length || (t0.choices && t0.choices.length) || (t0.extraHits && t0.extraHits.length)) && !(t0.prep && r === 1)) fire = rnd() < t0.rate + u.addbonusFor("rateup", t0);  // rateup: 提高自身主動戰法發動機率(如白眉); addbonusFor 依 t.prep/t.native 篩選 prepOnly/nativeOnly 修飾的加成(批7: 太平道法)
          else if ((t0.type === "command" || t0.type === "passive") && (t0.coef || (t0.choices && t0.choices.length)) && !(t0.when && t0.when.on) && roundOk(t0, r)) fire = rnd() < t0.rate;  // 指揮/被動: 每回合以資料 rate 擲骰(多數 rate=1 即每回合必發); when.rounds/from/until 只在符合回合才擲骰; when.on(反應式) 改由 onHit 事件點觸發, 不在此處常駐擲骰; 批18: choices 派發同active一併補coef=0情形
          if (fire) {
            // 批26 B2: stack.stackPer=="cast" —— 本戰法本次成功發動(fire), 若 u 身上已有
            // stackPer=="cast" 的疊層狀態則遞增1層(見 applyStackCast() 定義)。與round模式
            // (上方主迴圈逐回合遞增)互斥判斷, 不會重複遞增。
            u.applyStackCast();
            // 批16: choices(擇一分支) —— 發動時按權重隨機選一組效果(coef/kind/effects/extraHits/n/nMax
            // 可各自覆寫基礎戰法), 套用到本次發動; 未中選的分支本次不生效。權重預設均分。t0 為原始
            // 戰法物件(供 addbonusFor/whenFired 等以物件本身為鍵的邏輯保持穩定, 不因選分支而變動),
            // t 為「本次觸發實際使用」的合成視圖(不修改 t0 本身)。
            const t = t0.choices ? Object.assign({}, t0, pickChoice(t0.choices)) : t0;
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
              // 批18: targetSel(指定選標準則) —— 戰法級欄位, 主coef段按準則選單一目標(如避實擊虛
              // 「統率最低」), 優先於 lockTarget/hitsRepeat/隨機群體(不受混亂影響, 每次發動當下依
              // 準則重新篩選, 與 lockTarget 的「首次選定後鎖定沿用」語意方向相反, 不可混用)。
              if (t.targetSel) { const v = pickByCriterion(foesOf(u), t.targetSel); if (v) { hit(u, v, t.coef, t.kind, false, onHit, dealtDamage); _mainHitTgt = v; } }
              else if (t.lockTarget && cnt <= 1 && !t.hitsRepeat) { const v = resolveLockedTarget(u, t0, foesOf(u)); if (v) { hit(u, v, t.coef, t.kind, false, onHit, dealtDamage); _mainHitTgt = v; } }  // lockTarget 鍵用 t0(原始戰法物件), 避免 choices 每次合成新物件破壞跨回合鎖定
              else if (t.hitsRepeat) { for (let i = 0; i < cnt; i++) { const v = pickTarget(foesOf(u), u); if (v) { hit(u, v, t.coef, t.kind, false, onHit, dealtDamage); _mainHitTgt = v; } } }
              else { const vs = pickTargets(foesOf(u), cnt); for (const v of vs) hit(u, v, t.coef, t.kind, false, onHit, dealtDamage); if (vs.length === 1) _mainHitTgt = vs[0]; }
            }
            if (t.extraHits) fireExtraHits(u, t, _mainHitTgt, alliesOf, foesOf, onHit, dealtDamage);  // 批13: 多段傷害(兵刃+謀略雙段/主傷+補刀等)
            // 批12 ModeF: 混亂下單體主動戰法目標改敵我不分(pickTargetChaos); 群體/AoE(who=enemy 全體/
            // n>1)維持 applyEffects 內部既有邏輯不變 —— 這裡傳入的 tgt 只影響「單體優先鎖定」分支,
            // 群體戰法本就走 pickTargets(enemies,...) 不受此參數影響(近似, 群體戰法混亂下仍只打敵方)。
            // 批12 ModeG: lockTarget 的 applyEffects 目標(單體效果destination)同樣改用鎖定目標
            // (與混亂互斥: lockTarget 戰法目前資料上未與 chaos 共存, 若未來衝突以 lockTarget 優先,
            // 因 lockTarget 語意更明確針對特定戰法設計)。
            if (t.type === "active") applyEffects(u, t.lockTarget ? resolveLockedTarget(u, t0, foesOf(u)) : pickTargetChaos(u, alliesOf(u), foesOf(u)), t, alliesOf(u), foesOf(u));
            else if (t0.choices) {
              // 批27 B: command/passive 型戰法帶 choices —— 過去 pickChoice() 抽出的分支 t 只有
              // coef/extraHits 段會在上面被讀取套用, t.effects(分支自帶的效果, 如桃園結義三選一
              // 之一的heal)完全被憑空丟棄(見 engine_limitations.md §18a: applyEffects 對
              // command/passive 型戰法的呼叫管道是 applyPassives(), 讀的是 u.tactics 原始 t0,
              // 從未經過 pickChoice 解析)。此處補上: 只在 t0.choices 為真(=本次 t 是 choices
              // 合成視圖, 非 u.tactics 原始物件)時才呼叫 applyEffects(u, ..., t, ...) 套用分支的
              // effects, 且僅限於此(不對一般無choices的command/passive戰法重複套用——那些戰法
              // 的effects已由applyPassives()的prep/healOnly通道正確處理, 此處若無腦補上會造成
              // 雙重結算)。opt預設(noHeal:false, 未傳healOnly): 分支的heal效果視為「本次觸發的
              // 一次性治療」, 與applyPassives的healOnly常駐掃描是互斥的兩個通道: choices戰法的
              // t0.effects本身為空(內容全在choices[].effects裡), healOnly通道對空effects陣列
              // 天然no-op, 不會與此處重複治療。
              applyEffects(u, _mainHitTgt, t, alliesOf(u), foesOf(u), {});
            }
          }
        }
        const tgt = pickTargetChaos(u, alliesOf(u), foesOf(u));  // 普攻(常駐) + 連擊 + 突擊(繳械時跳過); 嘲諷: 強制指向施放者; 混亂: 敵我不分(批12 ModeF)
        if (tgt) {
          if (u.disarm) { if (TRACE) lg(`【${u.side}】${u.nm} 陷入繳械，無法普通攻擊`); }
          else {
            if (TRACE) lg(`【${u.side}】${u.nm} 普通攻擊 → ${tgt.nm}`);
            hit(u, tgt, 1.0, "phys", true, onHit, dealtDamage);
            for (let i = 0; i < extraCount(u.addbonus("extra")); i++) { const nt = pickTargetChaos(u, alliesOf(u), foesOf(u)); if (nt) { if (TRACE) lg(`【${u.side}】${u.nm} 連擊 → ${nt.nm}`); hit(u, nt, 1.0, "phys", true, onHit, dealtDamage); } }
            // 批16: everyN(計數觸發) —— 自身每第N次普攻觸發該戰法的 effects/extraHits(不含 coef 主傷段,
            // 資料上 everyN 戰法目前皆為輔助效果類, 若未來需要 coef 段可比照 fireExtraHits 擴充)。
            // 計數只算「本次真正命中的普攻」(disarm 時不會走到這裡, 故繳械回合不計數, 合理)。
            for (const t of u.tactics) if (t.everyN && t.everyN.on === "attack" && u.tickEveryN(t)) {
              if (TRACE) lg(`【${u.side}】${u.nm} 第${t.everyN.count}次普攻觸發【${t.nameZh}】`);
              if (t.extraHits) fireExtraHits(u, t, tgt, alliesOf, foesOf, onHit, dealtDamage);
              if (t.effects && t.effects.length) applyEffects(u, tgt, t, alliesOf(u), foesOf(u));
            }
            // 突擊(charge)擲骰: chargeup(突擊發動率加成, 如虎豹騎)只對真突擊戰法生效, 排除 t.proc===true 的
            // 特技偽戰法(user 明確指示: 特技不吃突擊加成, 例虎豹騎/三勢陣/經天緯地/陷陣突襲proc本身無此欄)。
            for (const t of u.tactics) if (t.type === "charge" && rnd() < t.rate + (t.proc ? 0 : u.addbonusFor("chargeup", t))) { if (TRACE) lg(`【${u.side}】${u.nm} 突擊【${t.nameZh}】`); if (t.coef) hit(u, tgt, t.coef, t.kind, false, onHit, dealtDamage); if (t.extraHits) fireExtraHits(u, t, tgt, alliesOf, foesOf, onHit, dealtDamage); applyEffects(u, tgt, t, alliesOf(u), foesOf(u)); }
          }
        }
      }
      for (const u of [...A, ...B]) {
        const s = u.settle; if (!s) continue;
        if (s.layers >= s.max || s.left <= 1) {
          for (const v of (setA.has(u) ? A : B)) if (v.alive) { const sd = damage(s.caster, v, s.base + s.per * s.layers, s.kind, s.snap); v.troop -= sd; v.wounded += sd * woundedRate(CUR_R); }
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
    Unit, hit, damage, pickTarget, pickTargets, pickTargetChaos, resolveLockedTarget, applyEffects, roundOk, fireExtraHits,
    hpOk, targetHas, dispelUnit, pickChoice };  // 批16 新原語供測試腳本直接驗證內部機制(同 sgz.py 直接測 Unit/hit)
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  root.SGZ = API;
})(typeof globalThis !== "undefined" ? globalThis : this);
