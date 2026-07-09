"use strict";
// 批51: 全池聯賽制武將評分 —— 離線 Node 管線(不在瀏覽器執行)。
// 批52: 客串評分修正 —— 批51診斷: anchor-primary(武將分只算自己當隊長那隊的勝率)系統性低估
// 輔助/治療核。原因是 matchmaker 決選管線的 anchor 隊隊友是「該anchor視角下的最佳搭子」,
// 對治療/謀士這類「自己當隊長時發揮有限、但塞進別人的攻擊隊才強」的武將並不公平——批51雖已
// 設計 guestWinRate 欄位, 但客串隊來源僅「湊巧被其他anchor選中當隊友」, 實測全池193名中只有
// 43名有任何客串出場record(guestMatches>0), 其餘(含華佗/賈詡/呂布/郭嘉等)guestWinRate皆為
// null——客串樣本太稀疏, 形同虛設。本批新增「主動建客串隊」: 對每位武將明確嘗試塞入強力
// 主將隊, 用同一套決選比較(主將排列×兵種雙方案×傳承戰法貪心)產生一支專屬客串隊, 不再被動
// 依賴「湊巧被選中」。詳見下方 buildGuestTeams。
//
// 四階段:
//   A) 建隊池 —— 對全部 193 名武將(POOL 全池, buildPool 已排除無效卡, 見下方查證註解)各跑
//      matchmaker 決選管線(複用 docs/matchmaker.js, 直接 Node require, 不碰引擎/戰鬥數學),
//      每將取 top-2 隊, 同隊(依 sorted 武將名 key)去重合併。
//   A2) 建客串隊(批52新增) —— 對每位武將M, 從隊池取mmWin最高的N個「強力方陣」當候選主將隊,
//      各自嘗試「M頂替一個副將位」, 用同一套決選比較(主將排列×兵種雙方案×傳承戰法)選出M的
//      最佳客串隊, 取分數最高的一隊併入隊池(去重: 若巧合等於既有隊, 只補記sourceAnchors)。
//   B) 聯賽 —— 隊池內(含新增客串隊)隨機配對(每隊 vs K 個隨機對手, 固定種子), 每對局 n=150
//      模擬, 用 SGZ.simulate 真實對戰(非啟發式), 統計場次加權勝率。
//   C) 評分 —— 武將分 = combine(anchorWinRate, guestWinRate), 取兩者較高者(該將「最強表現」,
//      無論是當隊長或當客串), 樣本量不足時退回可用的那個。詳見 aggregateGeneralScore。
//
// 查證(2026-07-09): raw docs/data/generals.json 193 筆, SGZ.buildPool 後 POOL 仍為 193 筆
// (Object.keys(POOL).length === 193, 逐一比對 generals.json 每筆 name 均在 POOL 內), 故本批
// 「全池」= Object.keys(POOL) 全部 193 名, 無需額外過濾 type:none(那是戰法層級欄位, 非武將
// 層級, 武將本身沒有「無效卡」的概念殘留在 buildPool 輸出)。
//
// 可重現性: SGZ.simulate 內部戰鬥擲骰用裸 Math.random()(非參數化種子), 為讓「隊伍選誰當對手」
// 與「戰鬥本身的隨機性」兩者都可重現, 本腳本啟動時整體 monkey-patch globalThis.Math.random 為
// mulberry32 固定種子 PRNG(僅本離線 Node 進程內生效, 不影響瀏覽器端 app.js/engine.js 本體)。
//
// 用法: node docs/league.js [--gen=N] [--k=N] [--n=N] [--topPerGeneral=N] [--seed=N] [--quick]
//   [--guestCand=N] [--guestComboN=N]
//   --quick: 大幅縮小規模的煙霧測試模式(供開發驗證管線正確性, 非正式評分產出)。
const fs = require("fs");
const path = require("path");

// ---- 參數(可由 CLI 覆寫) ----
const ARGS = process.argv.slice(2).reduce((a, s) => {
  const m = s.match(/^--([\w]+)(?:=(.*))?$/);
  if (m) a[m[1]] = m[2] === undefined ? true : m[2];
  return a;
}, {});
const QUICK = !!ARGS.quick;
const SEED = ARGS.seed ? +ARGS.seed : 20260709;                 // 固定亂數種子(對稱本專案批50慣例日期式種子)
const TOP_PER_GENERAL = ARGS.topPerGeneral ? +ARGS.topPerGeneral : (QUICK ? 1 : 2);   // 每將取top-N隊
const K_OPPONENTS = ARGS.k ? +ARGS.k : (QUICK ? 6 : 70);         // 每隊聯賽場次: vs K個隨機對手
const LEAGUE_N = ARGS.n ? +ARGS.n : (QUICK ? 20 : 150);          // 每對局模擬局數
const GEN_LIMIT = ARGS.gen ? +ARGS.gen : (QUICK ? 12 : Infinity); // 建隊池只處理前N名武將(quick模式用)
// 建隊管線參數(批48-49精算決選的縮小版——全池跑193次, 精度可略降但需守住「找得到能打的隊」)
const MM_OPTS = QUICK
  ? { stage1Limit: 60, stage2Keep: 4, stage2N: 24, stage3N: 40, comboN: 24, topOut: TOP_PER_GENERAL }
  : { stage1Limit: 100, stage2Keep: 5, stage2N: 40, stage3N: 100, comboN: 40, topOut: TOP_PER_GENERAL };
// 批52: 客串隊建構參數 —— 每位武將嘗試頂替進前 GUEST_CAND_ANCHORS 名「最強方陣」(依 stage A
// 決選 mmWin 排序)當候選, 用 GUEST_COMBO_N 樣本數的組合比較(主將排列×兵種雙方案)選最佳客串隊。
const GUEST_CAND_ANCHORS = ARGS.guestCand ? +ARGS.guestCand : (QUICK ? 3 : 6);
const GUEST_COMBO_N = ARGS.guestComboN ? +ARGS.guestComboN : (QUICK ? 16 : 30);

// ---- 可重現亂數: mulberry32(對稱 scratchpad/funnel_validate.js 批50既有做法) ----
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rng = mulberry32(SEED);
Math.random = rng;   // 全局覆寫: 整條管線(matchmaker模擬+聯賽配對+聯賽模擬)皆可重現。僅限本離線 Node 進程。

const { loadCtx } = require(path.join(__dirname, "..", "scratchpad", "mm_harness.js"));
const ctx = loadCtx();
const { SGZ, Matchmaker, POOL } = ctx;

function log(msg) { console.log(`[${new Date().toISOString().slice(11, 19)}] ${msg}`); }
function fmtMs(ms) { const s = ms / 1000; return s < 60 ? `${s.toFixed(1)}s` : `${(s / 60).toFixed(1)}min`; }

// ---- A. 建隊池 ----
// 每位武將呼叫 Matchmaker.run 拿 top-N 隊, 隊伍以「排序後武將名陣列」當 key 去重(不同 anchor
// 可能收斂到同一隊, 如張飛跑出的第2隊剛好是呂布的第1隊)。去重後保留：teamKey -> {team(原順序,
// 第一人為主將), troop, bs, inh, ad, win(對gauntlet的決選勝率, 僅供debug參考不是聯賽分數),
// sourceAnchors(哪些anchor產出過這隊, 供debug)}。
async function buildTeamPool() {
  const allNames = Object.keys(POOL);
  const names = GEN_LIMIT === Infinity ? allNames : allNames.slice(0, GEN_LIMIT);
  log(`A. 建隊池開始 — ${names.length}名武將 × top-${TOP_PER_GENERAL}隊, 參數=${JSON.stringify(MM_OPTS)}`);
  const teamMap = new Map();      // teamKey -> team record
  const perGeneralTeams = {};     // generalName -> [teamKey,...]（該將入選的隊, 供評分階段anchor/客串判定）
  const t0 = Date.now();
  let errCount = 0;
  for (let i = 0; i < names.length; i++) {
    const anchorName = names[i];
    perGeneralTeams[anchorName] = perGeneralTeams[anchorName] || [];
    try {
      const res = await Matchmaker.run(ctx, anchorName, MM_OPTS);
      for (const r of res.top) {
        const teamKey = r.team.slice().sort().join("|");
        if (!teamMap.has(teamKey)) {
          // 批51: Matchmaker.run 的結果不含 eq(決選管線本身不配裝備, defaultParamsFor 的
          // eq 恆為 names.map(()=>[])——空陣列), 這裡明確補上對稱值, 避免誤用 ad 佔位造成
          // simulate 的裝備參數與加點參數混淆(曾在草稿中誤寫 recA.ad.map(()=>[]), 已修正)。
          teamMap.set(teamKey, {
            team: r.team, troop: r.troop, bs: r.bs, eq: r.team.map(() => []), inh: r.inh, ad: r.ad,
            mmWin: r.win, sourceAnchors: [],
          });
        }
        teamMap.get(teamKey).sourceAnchors.push(anchorName);
        for (const member of r.team) {
          perGeneralTeams[member] = perGeneralTeams[member] || [];
          if (!perGeneralTeams[member].includes(teamKey)) perGeneralTeams[member].push(teamKey);
        }
      }
    } catch (e) {
      errCount++;
      log(`  [WARN] ${anchorName} matchmaker失敗, 跳過: ${e.message}`);
    }
    if ((i + 1) % 10 === 0 || i === names.length - 1) {
      const elapsed = Date.now() - t0;
      const etaMs = (elapsed / (i + 1)) * (names.length - i - 1);
      log(`  進度 ${i + 1}/${names.length} — 已花 ${fmtMs(elapsed)}, 預估剩餘 ${fmtMs(etaMs)}, 隊池目前 ${teamMap.size} 隊`);
    }
  }
  log(`A. 建隊池完成 — ${teamMap.size} 隊(去重後), 耗時 ${fmtMs(Date.now() - t0)}, 失敗 ${errCount} 名`);
  return { teamMap, perGeneralTeams };
}

// ---- A2. 建客串隊(批52新增) ----
// 動機: 批51的客串樣本純粹「湊巧被其他anchor選中當隊友」, 193名中僅43名有任何客串record——
// 對治療/謀士類(自己當隊長時發揮有限, 塞進別人的強力輸出隊才強)完全失效(如華佗0客串場次)。
// 本函式對每位武將M主動嘗試「頂替進強力方陣」, 不再被動等待被選中。
//
// 做法: 取隊池中 mmWin(決選階段對gauntlet勝率)最高的方陣, 依序嘗試 GUEST_CAND_ANCHORS 個
// (跳過與M同源SP/本體撞名的方陣, 見 baseOf), 對每個候選方陣分別嘗試「M頂替副將位1」與「M頂替
// 副將位2」兩種頂替, 用與 matchmaker stage3 相同的組合比較(leaderPermutations×top2Troops→
// pickInheritTactics→vsGauntletWinRate小樣本)算出該候選客串隊的實力分, 取全部候選中分數最高
// 的一隊當M的客串隊, 併入 teamMap(去重: 若該隊組合恰好已存在於隊池, 只需標記M入selection,
// 不重複建隊——這種情況下該隊在聯賽的角色照舊, M透過 perGeneralTeams 掛勾即可分到客串場次)。
//
// 成本控制: 只取「單一最佳」客串隊(不像anchor隊取top-2), 且用小樣本(GUEST_COMBO_N)組合比較
// (同 matchmaker stage3 comboN 用途, 只是這裡的候選數少很多, 不需要 stage1/stage2 篩選——
// 候選方陣本身已是全池最強隊, 不需要再從193^2組合中海選)。
function baseOf(n) { return (n || "").replace(/^SP\s*/, ""); }

async function buildGuestTeams(teamMap, perGeneralTeams) {
  const { POOL, BONDS, TAC_DATA, TAC_TIER, NONEQUIP, scenario } = ctx;
  const allNames = Object.keys(POOL);
  const names = GEN_LIMIT === Infinity ? allNames : allNames.slice(0, GEN_LIMIT);
  const gauntlet = Matchmaker.buildGauntlet(POOL);

  // 強力方陣候選池: 依 mmWin 降冪排序, 供每位武將依序嘗試頂替(跳過撞名者各自往下取)。
  const strongSquads = Array.from(teamMap.values()).slice().sort((a, b) => b.mmWin - a.mmWin);

  log(`A2. 建客串隊開始 — ${names.length}名武將 × 前${GUEST_CAND_ANCHORS}強方陣候選, comboN=${GUEST_COMBO_N}`);
  const t0 = Date.now();
  let addedNew = 0, mergedExisting = 0, skipped = 0;

  for (let i = 0; i < names.length; i++) {
    const m = names[i];
    const mBase = baseOf(m);
    let bestCand = null;   // {team, troop, inh, params, win, teamKey}
    let tried = 0;
    for (const squad of strongSquads) {
      if (tried >= GUEST_CAND_ANCHORS) break;
      const squadBases = squad.team.map(baseOf);
      if (squadBases.includes(mBase)) continue;               // M自己(或其SP/本體)已在此方陣, 跳過
      tried++;
      // 對兩個副將位(index1/index2)各嘗試一次頂替, 主將位保留給方陣原主將(該方陣之所以強,
      // 主將位的統領/主動戰法安排通常已對其量身優化, 用heuristicScore粗評兩個頂替位何者較優,
      // 避免每個候選都跑2次完整組合比較(2倍成本)。
      const anchor = POOL[squad.team[0]];
      const cand1 = POOL[squad.team[1]], cand2 = POOL[squad.team[2]];
      const scoreReplace1 = Matchmaker.heuristicScore(POOL, BONDS, anchor, POOL[m], cand2);   // M替掉位1
      const scoreReplace2 = Matchmaker.heuristicScore(POOL, BONDS, anchor, cand1, POOL[m]);   // M替掉位2
      const candTeam = scoreReplace1 >= scoreReplace2
        ? [squad.team[0], m, squad.team[2]]
        : [squad.team[0], squad.team[1], m];

      // 組合比較: 主將排列(語意過濾)×兵種雙方案, 各跑小樣本, 取最佳配置(同 matchmaker stage3)。
      const leaderPerms = Matchmaker.leaderPermutations(POOL, candTeam);
      for (const perm of leaderPerms) {
        const troops2 = Matchmaker.top2Troops(POOL, perm);
        for (const troop of troops2) {
          const factions = perm.map(n => POOL[n] && POOL[n].faction);
          const inh = Matchmaker.pickInheritTactics(POOL, TAC_DATA, TAC_TIER, NONEQUIP, perm, troop, factions);
          const params = Matchmaker.defaultParamsFor(POOL, perm);
          params.inh = inh;
          const win = Matchmaker.vsGauntletWinRate(POOL, perm, params, gauntlet, GUEST_COMBO_N, scenario, troop);
          if (!bestCand || win > bestCand.win) bestCand = { team: perm, troop, inh, params, win };
        }
      }
    }

    if (!bestCand) { skipped++; continue; }   // 找不到不撞名的候選方陣(極端情況, 如全池同源撞名), 略過
    const teamKey = bestCand.team.slice().sort().join("|");
    if (!teamMap.has(teamKey)) {
      teamMap.set(teamKey, {
        team: bestCand.team, troop: bestCand.troop, bs: bestCand.params.bs,
        eq: bestCand.team.map(() => []), inh: bestCand.inh, ad: bestCand.params.ad,
        mmWin: bestCand.win, sourceAnchors: [], isGuestBuilt: true,
      });
      addedNew++;
    } else {
      mergedExisting++;   // 巧合已存在於隊池(如兩位武將客串出同一隊), 沿用既有隊記錄即可
    }
    for (const member of bestCand.team) {
      perGeneralTeams[member] = perGeneralTeams[member] || [];
      if (!perGeneralTeams[member].includes(teamKey)) perGeneralTeams[member].push(teamKey);
    }
    if ((i + 1) % 20 === 0 || i === names.length - 1) {
      const elapsed = Date.now() - t0;
      const etaMs = (elapsed / (i + 1)) * (names.length - i - 1);
      log(`  進度 ${i + 1}/${names.length} — 已花 ${fmtMs(elapsed)}, 預估剩餘 ${fmtMs(etaMs)}, 隊池目前 ${teamMap.size} 隊`);
    }
  }
  log(`A2. 建客串隊完成 — 新增${addedNew}隊/併入既有${mergedExisting}隊/略過${skipped}名, 隊池共${teamMap.size}隊, 耗時 ${fmtMs(Date.now() - t0)}`);
  return { teamMap, perGeneralTeams };
}

// ---- B. 聯賽: 隨機配對制 ----
// 每隊 vs K 個隨機對手(從隊池抽樣, 允許重複抽到同一對手不同場次以外——這裡做「不重複對手」
// 抽樣, 若隊池小於K+1則對手取「除自己外全部」)。用固定種子 rng 抽樣, 對局結果雙向記入雙方
// 的 wins/losses/matches 累計(對稱記帳: A vs B 的結果同時是 B vs A 的結果, 只需模擬一次)。
function shuffleInPlace(arr) {                       // Fisher-Yates, 用全局(已被種子覆寫的) Math.random
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
}
function runLeague(teamMap) {
  const teamKeys = Array.from(teamMap.keys());
  const nTeams = teamKeys.length;
  log(`B. 聯賽開始 — ${nTeams}隊, 每隊vs${K_OPPONENTS}個隨機對手, 每對局n=${LEAGUE_N}`);
  const stats = new Map();     // teamKey -> {wins, matches, roundsSum}
  teamKeys.forEach(k => stats.set(k, { wins: 0, matches: 0 }));

  // 生成配對排程: 每隊抽K個不同對手(排除自己); 用 pairSeen 避免同一對(A,B)雙向重複模擬。
  const pairSeen = new Set();
  const schedule = [];
  for (const key of teamKeys) {
    const others = teamKeys.filter(k => k !== key);
    shuffleInPlace(others);
    const k = Math.min(K_OPPONENTS, others.length);
    for (let i = 0; i < k; i++) {
      const opp = others[i];
      const pairKey = [key, opp].sort().join("~~");
      if (pairSeen.has(pairKey)) continue;
      pairSeen.add(pairKey);
      schedule.push([key, opp]);
    }
  }
  log(`  配對排程共 ${schedule.length} 場對局(去重雙向), 預估總模擬局數 ${schedule.length * LEAGUE_N}`);

  const t0 = Date.now();
  for (let i = 0; i < schedule.length; i++) {
    const [keyA, keyB] = schedule[i];
    const recA = teamMap.get(keyA), recB = teamMap.get(keyB);
    const r = SGZ.simulate(POOL, recA.team, recB.team, LEAGUE_N, recA.troop, recB.troop,
      recA.bs, recB.bs, recA.eq, recB.eq, recA.ad, recB.ad, recA.inh, recB.inh,
      ctx.scenario, 10, 10);
    const sA = stats.get(keyA), sB = stats.get(keyB);
    sA.wins += r.winA * LEAGUE_N; sA.matches += LEAGUE_N;
    sB.wins += r.winB * LEAGUE_N; sB.matches += LEAGUE_N;
    if ((i + 1) % 500 === 0 || i === schedule.length - 1) {
      const elapsed = Date.now() - t0;
      const etaMs = (elapsed / (i + 1)) * (schedule.length - i - 1);
      log(`  進度 ${i + 1}/${schedule.length} — 已花 ${fmtMs(elapsed)}, 預估剩餘 ${fmtMs(etaMs)}`);
    }
  }
  log(`B. 聯賽完成 — 耗時 ${fmtMs(Date.now() - t0)}, 總模擬局數 ${schedule.length * LEAGUE_N}`);
  return stats;
}

// ---- C. 評分(批52修正) ----
// 批51診斷: anchor-primary(武將分只算自己當隊長那隊的勝率)系統性低估輔助/治療核——華佗
// 22.8%墊底、SP龐德配爛隊19%, 但這類武將塞進別人的強力輸出隊(如諸葛亮隊)實測勝率可達
// 90%+(見批52 A2 buildGuestTeams的驗證)。user評分哲學是「整體勝率」, 不是「只看你能不能
// 自己扛隊」, 必須把「這位武將最強的真實貢獻方式」納入計分——輸出核大多自己當隊長就是最強
// 陣容, 但輔助/治療/謀士核往往要「進別人的隊」才發揮得出來, 兩者都是「整體勝率」的一部分。
//
// 批52評分公式: winRate(主分) = max(anchorWinRate, guestWinRate), 條件式取捨如下:
//   - 兩者都有足夠樣本(matches >= MIN_SAMPLE_MATCHES)時, 取較高者當主分——回答「這位武將
//     以他最擅長的方式加入隊伍, 打出的真實勝率是多少」, 對每位武將一致公平: 輸出核通常
//     anchor隊胜出(自己當核心火力最強), 輔助/治療核通常guest隊胜出(塞進強隊當潤滑劑更強),
//     两类都不會被另一半的短板拖累。
//   - 若一方樣本不足(<MIN_SAMPLE_MATCHES, 理論上不太會發生, 因為A2對每位武將都會嘗試建至少
//     一支客串隊——除非全池撞名跳過), 退回另一方; 兩者皆無則null(建隊徹底失敗的極端情況)。
//   - anchorWinRate/guestWinRate 原始值皆保留輸出(不只留max後的winRate), 供UI/複查對照——
//     使用者可同時看到「自己扛隊能打多少」與「客串能打多少」, 不是黑盒單一數字。
// isAnchor 判定沿用批51: sourceAnchors.includes(name)(涵蓋「自己的隊先被別的anchor產出過
// (去重碰撞)」的歸屬情況, 比 sourceAnchors[0]===name 更準確)。
const MIN_SAMPLE_MATCHES = LEAGUE_N;   // 至少一場對局(K_OPPONENTS=1個對手×LEAGUE_N局)的樣本量才採信

function aggregateGeneralScore(teamMap, stats, perGeneralTeams) {
  const ratings = {};
  for (const [name, teamKeys] of Object.entries(perGeneralTeams)) {
    if (!teamKeys.length) { ratings[name] = null; continue; }   // 建隊失敗(matchmaker錯誤跳過的將)
    let aWin = 0, aMatch = 0, gWin = 0, gMatch = 0;
    const teamsOut = [];
    for (const key of teamKeys) {
      const rec = teamMap.get(key);
      const s = stats.get(key);
      if (!s || !s.matches) continue;
      const isAnchor = rec.sourceAnchors.includes(name);
      if (isAnchor) { aWin += s.wins; aMatch += s.matches; }
      else { gWin += s.wins; gMatch += s.matches; }
      teamsOut.push({
        team: rec.team, troop: rec.troop, winRate: s.wins / s.matches, matches: s.matches, isAnchor,
        isGuestBuilt: !!rec.isGuestBuilt,
      });
    }
    teamsOut.sort((a, b) => b.winRate - a.winRate);
    const anchorTeams = teamsOut.filter(t => t.isAnchor);
    const guestTeams = teamsOut.filter(t => !t.isAnchor);
    const anchorWinRate = aMatch ? aWin / aMatch : null;
    const guestWinRate = gMatch ? gWin / gMatch : null;
    const anchorOk = aMatch >= MIN_SAMPLE_MATCHES;
    const guestOk = gMatch >= MIN_SAMPLE_MATCHES;
    // 批52主分: 兩者皆有效樣本時取較高者(該將「最強表現」); 否則退回有效的那個; 皆無則null。
    let winRate, scoreSource;
    if (anchorOk && guestOk) {
      if (anchorWinRate >= guestWinRate) { winRate = anchorWinRate; scoreSource = "anchor"; }
      else { winRate = guestWinRate; scoreSource = "guest"; }
    } else if (anchorOk) { winRate = anchorWinRate; scoreSource = "anchor"; }
    else if (guestOk) { winRate = guestWinRate; scoreSource = "guest"; }
    else { winRate = anchorWinRate ?? guestWinRate; scoreSource = anchorWinRate != null ? "anchor(樣本不足)" : (guestWinRate != null ? "guest(樣本不足)" : null); }
    ratings[name] = {
      winRate,                                    // 批52主分=max(anchor,guest)(樣本量足夠時), 見scoreSource
      scoreSource,                                 // 主分取自anchor還是guest, 供UI/複查對照
      matches: (scoreSource === "guest" || scoreSource === "guest(樣本不足)") ? gMatch : aMatch,
      anchorWinRate, anchorMatches: aMatch,        // 批52: 完整保留兩邊原始值(不只留max後的winRate)
      guestWinRate, guestMatches: gMatch,
      teams: teamsOut,
      anchorTeam: anchorTeams[0] || teamsOut[0],                            // 顯示用代表隊(勝率最高的anchor隊)
      guestTeam: guestTeams[0] || null,                                     // 批52新增: 顯示用代表客串隊(勝率最高)
      // 批51統籌補充: 自帶戰法是否已建模(POOL[name].tactic 存在與否)。83/193 武將戰法未建模
      // (既有資料缺失, 非本批引入), 其聯賽分是「被資料缺失壓低的地板分」而非真實強度——
      // 顯示層據此欄位降級樣式(灰色/半透明徽章+提示文字), 避免誤導使用者。
      dataComplete: !!(POOL[name] && POOL[name].tactic),
    };
  }
  return ratings;
}

// ---- T階切分: 按勝率分布分位數(參數化) ----
function assignTiers(ratings, { sTop = 0.10, aTop = 0.25, bTop = 0.50 } = {}) {
  const entries = Object.entries(ratings).filter(([, r]) => r && r.winRate != null);
  entries.sort((a, b) => b[1].winRate - a[1].winRate);
  const n = entries.length;
  entries.forEach(([name, r], i) => {
    const pct = (i + 1) / n;                      // 由高勝率往低排, pct=累積分位(1/n ~ 1.0)
    r.tier = pct <= sTop ? "S" : pct <= aTop ? "A" : pct <= bTop ? "B" : "C";
    r.rank = i + 1;
  });
  return { total: n, sCount: entries.filter(([, r]) => r.tier === "S").length,
    aCount: entries.filter(([, r]) => r.tier === "A").length,
    bCount: entries.filter(([, r]) => r.tier === "B").length,
    cCount: entries.filter(([, r]) => r.tier === "C").length };
}

// ---- main ----
async function main() {
  const t0 = Date.now();
  log(`批52 客串評分修正 開始 — seed=${SEED} quick=${QUICK} topPerGeneral=${TOP_PER_GENERAL} K=${K_OPPONENTS} n=${LEAGUE_N} guestCand=${GUEST_CAND_ANCHORS} guestComboN=${GUEST_COMBO_N}`);
  const { teamMap, perGeneralTeams } = await buildTeamPool();
  await buildGuestTeams(teamMap, perGeneralTeams);
  const stats = runLeague(teamMap);
  const ratings = aggregateGeneralScore(teamMap, stats, perGeneralTeams);
  const tierStats = assignTiers(ratings);

  const totalMs = Date.now() - t0;
  const winRates = Object.values(ratings).filter(r => r && r.winRate != null).map(r => r.winRate);
  winRates.sort((a, b) => a - b);
  const pctile = p => winRates.length ? winRates[Math.min(winRates.length - 1, Math.floor(p * winRates.length))] : null;
  const scoreSourceCounts = Object.values(ratings).filter(Boolean).reduce((acc, r) => {
    acc[r.scoreSource] = (acc[r.scoreSource] || 0) + 1;
    return acc;
  }, {});

  const out = {
    metadata: {
      batch: "批E",
      generation: 3,                                  // 批51=1(anchor-primary), 批52=2(客串修正), 批E=3(戰法定稿對齊後重跑)
      seed: SEED,
      date: "2026-07-09",                              // 固定字串(非硬編當前系統時間, user規格)
      poolSize: teamMap.size,                          // 隊池規模(含A建隊池+A2客串隊)
      teamPoolSize: teamMap.size,                       // 相容批51欄位名(供舊程式碼/文件對照)
      generalsCovered: Object.keys(ratings).filter(n => ratings[n]).length,
      generalsTotal: Object.keys(POOL).length,
      matchCount: Array.from(stats.values()).reduce((s, x) => s + x.matches, 0) / 2 / LEAGUE_N,
      params: {
        topPerGeneral: TOP_PER_GENERAL, K: K_OPPONENTS, kOpponents: K_OPPONENTS, n: LEAGUE_N, leagueN: LEAGUE_N,
        tierCut: { sTop: 0.10, aTop: 0.25, bTop: 0.50 },
        mmOpts: MM_OPTS,
        guestCandAnchors: GUEST_CAND_ANCHORS, guestComboN: GUEST_COMBO_N,
        minSampleMatches: MIN_SAMPLE_MATCHES,
      },
      // stats.matches 以「模擬局」為單位累計(每對局加 LEAGUE_N, 雙向各記一次), 故:
      // 總對局(隊vs隊配對數) = sum/2/LEAGUE_N; 總模擬局數 = sum/2。
      totalMatchups: Array.from(stats.values()).reduce((s, x) => s + x.matches, 0) / 2 / LEAGUE_N,
      totalSimRounds: Array.from(stats.values()).reduce((s, x) => s + x.matches, 0) / 2,
      elapsedMs: totalMs,
      tierSplit: tierStats,
      winRateDistribution: { min: winRates[0], p25: pctile(0.25), median: pctile(0.5), p75: pctile(0.75), max: winRates[winRates.length - 1] },
      scoreSourceCounts,                                // 主分取自anchor/guest/樣本不足的人數分布, 供驗收核對
      scoringMethod:
        "武將分 = max(anchorWinRate, guestWinRate)(兩者樣本量皆>=minSampleMatches時); 若僅一方有效樣本則退回該方; " +
        "皆無則null。anchorWinRate=該將自己當隊長跑出的隊伍(matchmaker決選top-2, 見buildTeamPool)之場次加權勝率; " +
        "guestWinRate=該將被動(湊巧被其他anchor選中當隊友, 批51既有機制)+主動建構(批52 buildGuestTeams: 對每將" +
        "嘗試頂替進全池前六強方陣, 取最佳一支)兩者合計的客串隊場次加權勝率。取max而非平均, 理由: 回答「這位武將" +
        "以他最擅長的方式加入隊伍, 真實整體勝率是多少」——輸出核通常自己當隊長最強(anchor胜出), 輔助/治療/謀士" +
        "核往往要進別人的強隊才發揮得出來(guest胜出), 取max讓兩類角色都用各自的最佳代表隊評分, 不被另一半的" +
        "結構性短板拖累; 純內政/無戰法建模的白板卡兩邊皆弱, 仍會落在低分區, 不受此修正影響。",
      note: "批E: 戰法定稿對齊(批A-D, 全面改實作貼合data/tactics.json本文)+troopLimit接線" +
        "(matchmaker.js pickInheritTactics/troopMismatch改讀本文權威troopLimit, 取代批49" +
        "22條手工兵種對照表, 35筆有實質限制的戰法現正確過濾非法傳承指派)後重跑, generation:3。" +
        "沿用批52聯賽參數(topPerGeneral/K/n/seed皆同, 見params/seed)不變, 評分公式" +
        "(scoringMethod)本身未改動——本次評分差異全部來自「戰法定稿對齊」與「傳承合法性過濾」" +
        "兩項底層資料/邏輯變動, 而非評分方法論本身的調整。",
    },
    generals: ratings,
  };

  const outPath = path.join(__dirname, "data", QUICK ? "ratings.quick.json" : "ratings.json");
  fs.writeFileSync(outPath, JSON.stringify(out, null, QUICK ? 2 : 0));
  log(`完成 — 寫入 ${outPath}, 總耗時 ${fmtMs(totalMs)}`);
  log(`隊池 ${teamMap.size} 隊 / 覆蓋 ${out.metadata.generalsCovered}/${out.metadata.generalsTotal} 將 / 總對局 ${out.metadata.totalMatchups} 場 / 總模擬局數 ${out.metadata.totalSimRounds}`);
  log(`T階切分: S=${tierStats.sCount} A=${tierStats.aCount} B=${tierStats.bCount} C=${tierStats.cCount} (共${tierStats.total})`);
  log(`主分來源: ${JSON.stringify(scoreSourceCounts)}`);
  log(`勝率分布: min=${out.metadata.winRateDistribution.min?.toFixed(3)} p25=${out.metadata.winRateDistribution.p25?.toFixed(3)} median=${out.metadata.winRateDistribution.median?.toFixed(3)} p75=${out.metadata.winRateDistribution.p75?.toFixed(3)} max=${out.metadata.winRateDistribution.max?.toFixed(3)}`);
}

main().catch(e => { console.error(e); process.exit(1); });
