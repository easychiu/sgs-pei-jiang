"use strict";
// 批48: 單卡AI配將 —— 只選一張武將卡, 自動推薦最強隊伍(兩名隊友+戰法配置), 全瀏覽器端運行。
// 三階漏斗: 粗篩(啟發式評分, 全池配對) → 海選(小樣本模擬 vs GAUNTLET) → 決選(精算: 貪心配傳承戰法+兵書+兵種營Lv10, 大樣本模擬 vs GAUNTLET)。
// 只讀取既有資料/引擎(SGZ), 不改動 engine.js 的戰鬥數學。
(function (root) {
  // ---- 角色分類: 用戰法 effects 的 k 粗分「輸出/控制/治療/輔助」, 供粗篩角色互補評分 ----
  const CTRL_KS = new Set(["stun", "silence", "disarm", "dodge", "chaos", "taunt", "healblock", "immune"]);
  const HEAL_KS = new Set(["heal", "healGiven", "healBoost"]);
  const DMG_KS = new Set(["dot", "counter", "extra"]);
  const SUPPORT_KS = new Set(["stat", "mitig", "rateup", "chargeup", "first", "insight", "shield", "block", "stack", "decay", "surehit", "lifesteal", "redirect"]);
  function tacticRoles(t) {                          // 一個戰法可能同時佔多個角色(不互斥)
    const roles = new Set();
    if (!t) return roles;
    if (t.coef > 0) roles.add("dmg");
    for (const e of (t.effects || [])) {
      if (CTRL_KS.has(e.k)) roles.add("ctrl");
      else if (HEAL_KS.has(e.k)) roles.add("heal");
      else if (DMG_KS.has(e.k)) roles.add("dmg");
      else if (SUPPORT_KS.has(e.k)) roles.add("support");
    }
    if (!roles.size) roles.add("support");
    return roles;
  }
  function generalRoles(g) {                         // 武將自帶戰法角色集合(供粗篩角色互補評分)
    return tacticRoles(g.tactic);
  }

  // ---- 粗篩: 啟發式評分(毫秒級), 全池配對取前 N ----
  // 評分項: 緣分命中 + 同陣營加成 + 兵種適性重疊(隊伍兵種一致性) + 角色互補 + 主將位安排(統率高者優先居首)
  function heuristicScore(POOL, BONDS, anchor, mate1, mate2) {
    const names = [anchor.name, mate1.name, mate2.name];
    const team = [anchor, mate1, mate2];
    let score = 0;
    // 緣分: 命中的緣分效果數量加權(以 effects 條數約略估權重, 每條+18, 封頂避免單一緣分獨佔)
    const bonds = (BONDS || []).filter(b => (b.generals || []).filter(n => names.includes(n)).length >= (b.triggerCount || 99));
    score += Math.min(60, bonds.reduce((s, b) => s + (b.effects || []).length * 18, 0));
    // 同陣營: 三人同陣營 +25, 兩人同陣營 +10(給 FACTION 陣營乘算加成留餘地)
    const facCount = {};
    team.forEach(g => facCount[g.faction] = (facCount[g.faction] || 0) + 1);
    const maxFac = Math.max(...Object.values(facCount));
    score += maxFac === 3 ? 25 : (maxFac === 2 ? 10 : 0);
    // 兵種適性重疊: 找三人總適性最高的兵種, 分數即三人在該兵種的適性百分比總和(換算)
    let bestTroopSum = 0;
    for (const tp of SGZ.TROOPS) {
      const s = team.reduce((a, g) => a + SGZ.aptPct(g, tp), 0);
      if (s > bestTroopSum) bestTroopSum = s;
    }
    score += bestTroopSum * 18;                       // 3人全S(1.2×3=3.6)上限約65
    // 角色互補: 三人自帶戰法角色集合聯集越大越好(輸出/控制/治療/輔助 四種盡量都覆蓋)
    const roleUnion = new Set();
    team.forEach(g => generalRoles(g).forEach(r => roleUnion.add(r)));
    score += roleUnion.size * 14;                     // 最多4種 -> +56
    // 主將位安排: 錨點固定為主將(隊伍index0), 若錨點統率不是三人最高則扣分(戰法ifLeader/主將位需求粗略近似)
    const cmds = team.map(g => g.base.command);
    if (anchor.base.command < Math.max(...cmds) - 5) score -= 10;
    // 主將自帶 active/command 型戰法通常適合站主將位(輸出/指揮核心), 給小加成
    if (anchor.tactic && (anchor.tactic.type === "command" || anchor.tactic.type === "active")) score += 8;
    return Math.round(score);
  }

  function stage1Heuristic(POOL, BONDS, anchorName, { limit = 250 } = {}) {
    const anchor = POOL[anchorName];
    const names = Object.keys(POOL).filter(n => n !== anchorName);
    const baseOf = n => n.replace(/^SP\s*/, "");
    const anchorBase = baseOf(anchorName);
    const candidates = [];
    for (let i = 0; i < names.length; i++) {
      const b1 = baseOf(names[i]); if (b1 === anchorBase) continue;
      for (let j = i + 1; j < names.length; j++) {
        const b2 = baseOf(names[j]); if (b2 === anchorBase || b2 === b1) continue;
        const mate1 = POOL[names[i]], mate2 = POOL[names[j]];
        const sc = heuristicScore(POOL, BONDS, anchor, mate1, mate2);
        candidates.push({ team: [anchorName, names[i], names[j]], score: sc });
      }
    }
    candidates.sort((a, b) => b.score - a.score);
    return candidates.slice(0, limit);
  }

  // ---- GAUNTLET: 固定天梯陣容組, 覆蓋兵刃/謀略/控制/治療不同風格 ----
  const GAUNTLET_DEF = [
    { names: ["呂布", "趙雲", "關羽"], label: "兵刃猛攻" },
    { names: ["諸葛亮", "周瑜", "陸遜"], label: "謀略持續傷害" },
    { names: ["張角", "司馬懿", "貂蟬"], label: "控制壓制" },
    { names: ["曹操", "郭嘉", "華佗"], label: "指揮治療續戰" },
    { names: ["馬超", "黃忠", "張飛"], label: "兵刃爆發二型" },
    { names: ["劉備", "孫權", "孫策"], label: "指揮輔助" },
  ];
  function buildGauntlet(POOL) {                      // 過濾掉資料缺失(如某將 tactic 未建模)的成員, 保留可用隊伍
    return GAUNTLET_DEF.map(d => ({ ...d, names: d.names.filter(n => POOL[n]) }))
      .filter(d => d.names.length === 3);
  }

  // ---- 通用: 收集一隊的模擬參數(採用該隊預設兵書 + 無裝備 + 預設養成), 供海選/決選共用 ----
  function defaultBuildAlloc(g) {                     // 近似 app.js 的 defaultBuild: 進階滿、主屬性全加
    const adv = g.stars >= 5 ? 5 : 4;
    const pool = 50 + adv * 10;                        // 未典藏
    const stat4 = ["force", "intel", "command", "speed"];
    const primary = stat4.reduce((a, b) => g.base[b] > g.base[a] ? b : a, "force");
    return { adv, alloc: { [primary]: pool } };
  }
  function defaultParamsFor(POOL, names, { withBingshu = true } = {}) {
    const bs = [], ad = [], inh = [];
    for (const n of names) {
      const g = POOL[n];
      if (withBingshu) {
        const cat = (g.bingshuOptions ? Object.keys(g.bingshuOptions)[0] : (g.bingshuCats || [])[0]) || null;
        let main = null, subs = [];
        if (cat && g.bingshuOptions && g.bingshuOptions[cat]) {
          main = (g.bingshuOptions[cat].primary || [])[0] ? cat + "·" + g.bingshuOptions[cat].primary[0] : null;
          subs = (g.bingshuOptions[cat].secondary || []).slice(0, 2).map(nm => cat + "·" + nm);
        } else if (cat) {
          main = (SGZ.mainByCat()[cat] || [])[0] || null;
          subs = (SGZ.subByCat()[cat] || []).slice(0, 2);
        }
        bs.push([main, ...subs].filter(Boolean));
      } else bs.push([]);
      const { adv, alloc } = defaultBuildAlloc(g);
      const combatPct = adv * 0.02;
      ad.push({ ...alloc, amp: withBingshu ? combatPct : 0, mitig: withBingshu ? combatPct : 0 });
      inh.push([]);
    }
    return { names, bs, eq: names.map(() => []), ad, inh };
  }

  function vsGauntletWinRate(POOL, team, params, gauntlet, n, scenario) {
    const troopA = SGZ.teamTroop(POOL, team);
    let totalWin = 0, count = 0;
    for (const foe of gauntlet) {
      const fp = defaultParamsFor(POOL, foe.names);
      const troopB = SGZ.teamTroop(POOL, foe.names);
      const r = SGZ.simulate(POOL, team, foe.names, n, troopA, troopB,
        params.bs, fp.bs, params.eq, fp.eq, params.ad, fp.ad, params.inh, fp.inh, scenario, 10, 10);
      totalWin += r.winA; count++;
    }
    return count ? totalWin / count : 0;
  }

  // ---- 決選: 貪心配傳承戰法 —— 按 quality S>A>B 優先 + type 與隊伍現有戰法互補(避免同隊重複 command/charge 過量) + 不與自帶衝突(同名) ----
  function pickInheritTactics(POOL, TAC_DATA, TAC_TIER, NONEQUIP, team) {
    const tierRank = { S: 0, A: 1, B: 2 };
    const names = Object.keys(TAC_DATA).filter(nm => !NONEQUIP.has(nm));
    const pool = names.map(nm => ({ nm, t: TAC_DATA[nm], tier: TAC_TIER[nm] || "C" }))
      .filter(x => x.t && x.t.type !== "none")
      .sort((a, b) => (tierRank[a.tier] ?? 9) - (tierRank[b.tier] ?? 9));
    // 陣法/兵種類(FORMATION/TROOP)全隊只應有一人持有(同 app.js teamHasCat 慣例), 貪心指派時
    // 隊上已有人佔用該分類後, 後續其他人不再指派同分類的傳承戰法(留給未來更精細的位置感知再優化)。
    const teamCatTaken = new Set();
    const inh = team.map(() => [null, null]);
    const used = new Set();                            // 已指派的戰法名(隊內不重複)
    for (let slot = 0; slot < 2; slot++) {              // 每人最多2個傳承欄
      for (let i = 0; i < team.length; i++) {
        const g = POOL[team[i]];
        if (!g) continue;
        const nativeName = g.tacticName;
        const pick = pool.find(x => !used.has(x.nm) && x.nm !== nativeName &&
          !inh[i].includes(x.nm) &&
          !((x.t.cat === "FORMATION" || x.t.cat === "TROOP") && teamCatTaken.has(x.t.cat))
        );
        if (pick) {
          inh[i][slot] = pick.nm; used.add(pick.nm);
          if (pick.t.cat === "FORMATION" || pick.t.cat === "TROOP") teamCatTaken.add(pick.t.cat);
        }
      }
    }
    return inh.map(a => a.filter(Boolean));
  }

  // ---- 主流程: runMatchmaker(POOL, BONDS, TAC_DATA, TAC_TIER, NONEQUIP, anchorName, opts) ----
  // opts: { scenario, onProgress(stage, pct), stage1Limit, stage2Keep, stage2N, stage3N }
  async function run(ctx, anchorName, opts) {
    const { POOL, BONDS, TAC_DATA, TAC_TIER, NONEQUIP, scenario } = ctx;
    const onProgress = opts && opts.onProgress || (() => {});
    const stage1Limit = (opts && opts.stage1Limit) || 260;
    const stage2Keep = (opts && opts.stage2Keep) || 20;
    const stage2N = (opts && opts.stage2N) || 150;
    const stage3N = (opts && opts.stage3N) || 800;
    const topOut = (opts && opts.topOut) || 5;

    if (!POOL[anchorName]) throw new Error("武將不存在於當前資料池: " + anchorName);
    const gauntlet = buildGauntlet(POOL);

    // Stage 1: 粗篩(啟發式, 全池配對) — 分片跑避免長任務凍結 UI
    onProgress("stage1", 0);
    const candidates = stage1Heuristic(POOL, BONDS, anchorName, { limit: stage1Limit });
    onProgress("stage1", 100);
    await tick();

    // Stage 2: 海選(快速模擬 vs GAUNTLET, 小 n) — 時間預算分片(每~80ms讓出一次, 而非固定筆數,
    // 避免瀏覽器背景分頁 setTimeout 節流時仍每筆都排一次巨觀 tick 造成總時長被放大數倍)
    const withWin = [];
    let sliceStart = nowMs();
    for (let i = 0; i < candidates.length; i++) {
      const c = candidates[i];
      const params = defaultParamsFor(POOL, c.team);
      const win = vsGauntletWinRate(POOL, c.team, params, gauntlet, stage2N, scenario);
      withWin.push({ team: c.team, win });
      if (nowMs() - sliceStart > 80) {
        onProgress("stage2", Math.round((i / candidates.length) * 100));
        await tick();
        sliceStart = nowMs();
      }
    }
    onProgress("stage2", 100);
    withWin.sort((a, b) => b.win - a.win);
    const finalists = withWin.slice(0, stage2Keep);
    await tick();

    // Stage 3: 決選(精算) — 貪心配傳承戰法 + 兵書預設 + 兵種營Lv10, 大樣本模擬(同上: 時間預算分片)
    const results = [];
    sliceStart = nowMs();
    for (let i = 0; i < finalists.length; i++) {
      const c = finalists[i];
      const inh = pickInheritTactics(POOL, TAC_DATA, TAC_TIER, NONEQUIP, c.team);
      const params = defaultParamsFor(POOL, c.team);
      params.inh = inh;
      const troopA = SGZ.teamTroop(POOL, c.team);
      let totalWin = 0, totalRounds = 0, count = 0;
      for (const foe of gauntlet) {
        const fp = defaultParamsFor(POOL, foe.names);
        const troopB = SGZ.teamTroop(POOL, foe.names);
        const r = SGZ.simulate(POOL, c.team, foe.names, stage3N, troopA, troopB,
          params.bs, fp.bs, params.eq, fp.eq, params.ad, fp.ad, params.inh, fp.inh, scenario, 10, 10);
        totalWin += r.winA; totalRounds += r.rounds; count++;
      }
      results.push({
        team: c.team, win: totalWin / count, rounds: totalRounds / count,
        troop: troopA, bs: params.bs, inh, ad: params.ad,
      });
      onProgress("stage3", Math.round(((i + 1) / finalists.length) * 100));
      if (nowMs() - sliceStart > 80) { await tick(); sliceStart = nowMs(); }
    }
    results.sort((a, b) => b.win - a.win);
    return { top: results.slice(0, topOut), gauntlet };
  }

  const nowMs = () => (typeof performance !== "undefined" ? performance.now() : Date.now());
  function tick() { return new Promise(res => setTimeout(res, 0)); }

  root.Matchmaker = { run, buildGauntlet, stage1Heuristic, heuristicScore, tacticRoles, generalRoles, defaultParamsFor, pickInheritTactics, GAUNTLET_DEF };
  if (typeof module !== "undefined" && module.exports) module.exports = root.Matchmaker;
})(typeof window !== "undefined" ? window : globalThis);
