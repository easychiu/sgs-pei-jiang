"use strict";
// 批48: 單卡AI配將 —— 只選一張武將卡, 自動推薦最強隊伍(兩名隊友+戰法配置), 全瀏覽器端運行。
// 三階漏斗: 粗篩(啟發式評分, 全池配對) → 海選(小樣本模擬 vs GAUNTLET) → 決選(精算: 貪心配傳承戰法+兵書+兵種營Lv10, 大樣本模擬 vs GAUNTLET)。
// 只讀取既有資料/引擎(SGZ), 不改動 engine.js 的戰鬥數學。
// 批49: AI配將語意約束 —— user實測批48抓到「怪點」: SP典韋帶捨身救主(捨身護主向)卻被排當
// 主將/槍隊主將卻配到盾兵專屬丹陽兵/諸葛恪配丹陽兵沒吃到陶謙ifLeaderIs加成/無主動戰法者
// 配到只在自身主動戰法發動時才有效的戰法或兵書。決選階段新增: (A)主將位排列比較(語意過濾
// 「捨身護主類」不得當主將+「ifLeader/ifLeaderIs」優先當主將), (B)兵種雙方案比較(隊伍適性
// 加權前2名兵種各跑一輪, 取較優者), (C)戰法/兵書指派語意過濾(ifLeaderIs指名不在隊者降檔,
// teamGate不滿足陣法排除, TROOP類戰法兵種不符排除, activeOnly/activeFired類戰法或兵書無
// active持有者排除), (D)推薦理由文字(緣分/陣營/角色互補/主將安排原因)。
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

  // ---- 批49 A: 主將位語意判斷 ----
  // 「捨身護主類」戰法 —— 結構特徵是「為主將擋/代主將反擊」, 語意上該戰法只有「holder 不是
  // 主將」時才有意義(捨身救主: 自身受擊時減傷, 設計上是給副將擋刀; 古之惡來/虎衛軍等則是
  // 引擎 counter.guardFor 欄位, guardFor==="leader" 結構上要求 allies[0]!==caster 才會觸發,
  // holder 若自己就是主將, guardFor 分支形同虛設)。guardFor 有引擎欄位可查, 但捨身救主一類
  // 「純自身減傷、無 guardFor 欄位」的設計意圖無法從 effects 結構穩定辨識(高自減傷被動不少,
  // 硬用數值門檻會誤傷藏器待時等非護主向戰法), 故額外併入一份少量、經校對確認的具名清單
  // (捨身救主=SP典韋、古之惡來=典韋, 皆為「代主將擋/反擊」的明文設計)。
  const GUARD_LEADER_NAMES = new Set(["捨身救主", "古之惡來"]);
  function hasGuardForEffect(t) {
    return !!(t && (t.effects || []).some(e => e.guardFor === "leader"));
  }
  function isGuardLeaderTactic(t) {
    if (!t) return false;
    if (GUARD_LEADER_NAMES.has(t.nameZh)) return true;
    return hasGuardForEffect(t);
  }
  // 持有 ifLeader/ifLeaderIs 效果(戰法級或效果級)者「當主將才有意義」——優先當主將。
  // ifLeaderIs 額外指名武將, 若隊上有該武將, 該武將應為主將人選(非僅「優先」, 是唯一能觸發者)。
  function leaderSeekingInfo(t) {
    if (!t) return { seeks: false, names: null };
    const effs = t.effects || [];
    const namedEffs = effs.filter(e => e.ifLeaderIs);
    if (namedEffs.length) {
      const names = new Set();
      namedEffs.forEach(e => (Array.isArray(e.ifLeaderIs) ? e.ifLeaderIs : [e.ifLeaderIs]).forEach(n => names.add(n)));
      return { seeks: true, names };
    }
    const hasIfLeader = effs.some(e => e.ifLeader) || (t.when && t.when.ifLeader);
    return { seeks: hasIfLeader, names: null };
  }
  // 一個武將在候選隊中「是否適合當主將」的粗分類(供排列篩選/排序): forbid(捨身護主類, 不得
  // 當主將) > seek(ifLeader/ifLeaderIs 持有者, 優先當主將) > named(ifLeaderIs 指名到隊上某
  // 武將, 該武將應為主將) > neutral(其餘, 按統率/指揮系戰法排序)。
  function leaderFitness(g) {
    const t = g.tactic;
    if (isGuardLeaderTactic(t)) return "forbid";
    const info = leaderSeekingInfo(t);
    if (info.seeks) return "seek";
    return "neutral";
  }
  // 一隊三人中, 是否有 ifLeaderIs 指名到隊上另一位武將(如丹陽兵指名陶謙) —— 若有, 被指名者
  // 應為主將(即使指名者自己不在隊上, 只要隊上有人持有該 ifLeaderIs 戰法且指名對象也在隊上)。
  // 這裡只看「自帶」戰法, 傳承戰法的 ifLeaderIs 由決選階段 pickInheritTactics 之後另行核對
  // (見 stage3 排列比較, 傳承戰法要等貪心指派完成才知道, 故主將排列先用自帶戰法近似排序,
  // 傳承戰法造成的「應為主將」只在指派後才確定, 已超出「排列比較」的合理成本範圍, 留待
  // pickInheritTactics 內以「隊上已定主將」為前提做語意過濾, 不倒過來動搖主將排列)。
  function namedLeaderInTeam(POOL, team) {            // team: [name,...], 回傳 ifLeaderIs 指名且在隊上的武將名(找不到回 null)
    for (const n of team) {
      const g = POOL && POOL[n];
      if (!g || !g.tactic) continue;
      const info = leaderSeekingInfo(g.tactic);
      if (info.seeks && info.names) {
        for (const cand of info.names) if (team.includes(cand)) return cand;
      }
    }
    return null;
  }

  // 產生一隊(3人)語意合法的主將排列 —— 全部3種「誰當主將」排列(其餘兩人順序不影響模擬,
  // 固定按原順序排 index1/2), 過濾掉 forbid(捨身護主類)者當主將的排列, 若濾光則回退成
  // 「僅保留非forbid的一種」(全員皆forbid的極端情況不太可能發生, 保守回退避免拋出空陣列)。
  // ifLeaderIs 指名對象在隊上時, 只保留該武將當主將的排列(唯一有意義的安排)。
  function leaderPermutations(POOL, team) {
    const named = namedLeaderInTeam(POOL, team);
    const perms = team.map((leaderName) => {
      const rest = team.filter(n => n !== leaderName);
      return [leaderName, ...rest];
    });
    if (named) {
      const only = perms.filter(p => p[0] === named);
      if (only.length) return only;
    }
    const legal = perms.filter(p => leaderFitness(POOL[p[0]]) !== "forbid");
    return legal.length ? legal : perms.slice(0, 1);
  }
  function leaderReason(POOL, team) {                 // 供 D 項推薦理由: 描述主將安排原因
    const leader = POOL[team[0]];
    if (!leader) return "";
    const t = leader.tactic;
    if (isGuardLeaderTactic(t)) return "";            // 不應發生(排列已過濾), 保底不生成矛盾說詞
    const info = leaderSeekingInfo(t);
    if (info.names && info.names.size) return `${team[0]}居主將以觸發「${t.nameZh}」統領加成`;
    if (info.seeks) return `${team[0]}居主將以發揮「${t.nameZh}」主將限定效果`;
    const guards = team.slice(1).filter(n => POOL[n] && isGuardLeaderTactic(POOL[n].tactic));
    if (guards.length) return `${guards.join("、")}居副將以發揮「${(POOL[guards[0]].tactic || {}).nameZh || ""}」`;
    return "";
  }

  // ---- 批49→批E 沿革: 戰法/兵書指派語意過濾輔助 ----
  // 批49曾用「僅 cat==="TROOP" + 22條人工校對具名表」近似 troopLimit(當時 troopLimit 欄位
  // 未隨 tactics_parsed.json 流入瀏覽器, 見 docs/data/engine_limitations.md 對應段落)。
  // 批E: reparse_effects.py 已把 troopLimit 原樣從 data/tactics.json 帶入
  // tactics_parsed.json(見該檔「1b」步驟), 現全面改讀本文權威欄位取代該具名表——稽核發現
  // 全庫373筆戰法中實際有35筆帶真正限制(不限於TROOP類, COMMAND/ACTIVE/PASSIVE/FORMATION/
  // BURST皆有, 如鋒矢陣/魚鱗陣/雁行陣等陣法、上兵伐謀/深謀遠慮/藏器待時等指揮被動), 舊表的
  // 22條(全部TROOP類)驗證後與本文完全一致(無回歸), 淨新增13筆先前完全無限制覆蓋的戰法。
  // troopLimit 值域為 CAVALRY/SHIELD/BOW/SPEAR/SIEGE(英文enum, 見 data/tactics.json),
  // 對映引擎慣用的中文單字兵種名(SGZ.TROOPS = ["騎","盾","弓","槍","器"], 見 engine.js)。
  const TROOP_ENUM_TO_ZH = { CAVALRY: "騎", SHIELD: "盾", BOW: "弓", SPEAR: "槍", SIEGE: "器" };
  // 語意(對齊 data/tactics.json 原始欄位, 非本檔推導): troopLimit 缺欄位/null/空陣列 = 資料
  // 未標註限制(373筆中66筆為此狀態, 幾乎全是 source:"INHERITANCE" 的一般傳承戰法如白眉——
  // 資料缺口, 不是「限制成0種兵種可用」, 若誤判成後者會讓66筆正常戰法變成永遠配不出去的
  // 死欄位), 一律視為「不限制」(可裝載於任何兵種)。非空陣列且不含目標兵種才是真正限制。
  function troopMismatch(t, troop) {                  // true=戰法要求的兵種與隊伍選定兵種不符, 應整條排除
    if (!t || !Array.isArray(t.troopLimit) || !t.troopLimit.length) return false;
    return !t.troopLimit.some(en => TROOP_ENUM_TO_ZH[en] === troop);
  }
  // 「主動觸發依賴」戰法/兵書 —— 效果掛 activeOnly(僅主動戰法造成的傷害才吃, 如鬼謀/士爭
  // 先赴)或戰法級 when.on==="activeFired" 且未指定 who(隱含=自身, 綁定「自己」的主動戰法
  // 成功發動這件事, 如士爭先赴), 對「整套 kit 無 type:"active" 戰法」的持有者(含傳承後)
  // 而言形同虛設, 指派時應排除。teamHasActiveTactic 檢查含自帶+已指派傳承戰法。
  function requiresOwnActive(t) {
    if (!t) return false;
    if ((t.effects || []).some(e => e.activeOnly)) return true;
    if (t.when && t.when.on === "activeFired" && (t.when.who === undefined || t.when.who === "self")) return true;
    return false;
  }
  function holderHasActiveTactic(g, inheritedNames, TAC_DATA) {
    if (g.tactic && g.tactic.type === "active") return true;
    return (inheritedNames || []).some(nm => TAC_DATA[nm] && TAC_DATA[nm].type === "active");
  }

  // ---- 粗篩: 啟發式評分(毫秒級), 全池配對取前 N ----
  // 評分項: 緣分命中 + 同陣營加成 + 兵種適性重疊(隊伍兵種一致性) + 角色互補
  // 批50: 權重依據化改造。原權重(緣分+60/陣營+25/適性×18/角色+14/主將位±8~10)為批48拍腦袋
  // 訂定, 從未驗證過與實際模擬勝率的關係。實驗方法: 對呂布(輸出)/華佗(輔助)/張角(控制)三個
  // 代表anchor各自全池配對(~18314組)算粗篩分, 從排名151-500與500名以後分層抽樣共400組,
  // 用 vsGauntletWinRate(n=100, 與現行stage2同函式同口徑)取得「真實模擬勝率」金標準, 量測
  // 粗篩分數與勝率的Spearman相關、各子項單項相關、並用該樣本做子項對勝率的最小二乘回歸。
  // 見 scratchpad/funnel_validation.json(原始量測資料)。核心發現(2026-07-09驗證):
  //  1) 舊權重的「漏網率」達100%——三個anchor中, 抽樣池(排名151名以後)裡真實勝率最高的
  //     5組, 沒有一組的粗篩分數搆得上進舊top-5的門檻(舊top5分數普遍135~178, 但抽樣池裡
  //     真實勝率最高的組合分數常只有110~140)。舊top-5平均勝率(0.34~0.61)雖仍優於中後段
  //     平均(高於中後段均值的比例僅約14%), 代表粗篩不是純噪音, 但個別「漏網好組合」大量
  //     存在, 值得改造。
  //  2) 粗篩分數 vs 勝率 Spearman: 呂布0.37 / 華佗0.42 / 張角0.13(三anchor不穩定, 對控制型
  //     anchor幾乎失效)。
  //  3) 子項單項相關(pooled回歸, win ~ 子項, 見scratchpad/regression_beta.json): 兵種適性
  //     (troopScore)迴歸係數換算「全值域勝率擺幅」約+1.60(全樣本最強訊號, 遠超其餘四項總和),
  //     角色互補(roleScore)約+0.40居次, 緣分(bondScore)約+0.20(樣本中緣分命中僅3/1200組,
  //     訊號方向為正但因稀疏, 信賴區間寬, 不宜就此斷定緣分無用——緣分效果本身在引擎內是
  //     真實加成, 只是「抽樣中太少見, 統計檢定力不足」, 保守做法是保留但不特意放大), 陣營
  //     (facScore)約-0.04(接近零甚至微負, 三人同陣營在粗篩層級幾乎不預測勝率——注意這不等於
  //     「陣營戰法/兵書的隊內加成無效」, 只是「粗篩用陣營人數當代理變數」這個做法本身在這批
  //     樣本裡沒有鑑別力), 主將位啟發式(leaderScore)約+0.001(幾乎零, 三anchor的leaderScore
  //     符號甚至偏負相關——判斷合理: 決選階段批49已有精確的leaderPermutations語意排列比較,
  //     粗篩層級這個「統率最高者當主將」的近似規則對勝率没有預測力, 是純噪音項)。
  //  4) 改造方案: 用同一份400×3樣本重新配權重(保留原公式結構, 只調係數, 維持程式碼可讀性
  //     與既有呼叫介面), 在獨立(不同亂數種子)holdout樣本驗證: 新權重在中後段樣本池選出的
  //     top5, 平均模擬勝率呂布0.518→0.612 / 華佗0.185→0.370 / 張角0.620→0.749, 三anchor
  //     全面優於舊權重, 未劣化。主將位啟發式(舊±8/-10)因對勝率無預測力, 予以移除(決選階段
  //     leaderPermutations已涵蓋語意層面的主將排列判斷, 粗篩不需要這個近似)。
  function heuristicScore(POOL, BONDS, anchor, mate1, mate2) {
    const names = [anchor.name, mate1.name, mate2.name];
    const team = [anchor, mate1, mate2];
    let score = 0;
    // 緣分: 命中的緣分效果數量加權(每條+18, 封頂60)。批50驗證: 樣本中緣分命中太稀疏
    // (1200組中僅3組), 迴歸信賴區間寬但方向為正, 維持原權重不變(保留而非放大或砍除)。
    const bonds = (BONDS || []).filter(b => (b.generals || []).filter(n => names.includes(n)).length >= (b.triggerCount || 99));
    score += Math.min(60, bonds.reduce((s, b) => s + (b.effects || []).length * 18, 0));
    // 同陣營: 批50驗證迴歸係數接近零(全值域擺幅約-0.04, 三人同陣營對勝率幾乎無鑑別力),
    // 原+25/+10大幅下修為+4/+1.6(降至約1/6, 保留小幅存在感而非完全歸零——理論上陣營戰法/
    // 兵書仍可能有隊內協同, 只是這個「數人頭」代理變數量不到, 不宜武斷歸零外推)。
    const facCount = {};
    team.forEach(g => facCount[g.faction] = (facCount[g.faction] || 0) + 1);
    const maxFac = Math.max(...Object.values(facCount));
    score += maxFac === 3 ? 4 : (maxFac === 2 ? 1.6 : 0);
    // 兵種適性重疊: 批50驗證全樣本最強訊號(全值域擺幅約+1.60, 遠超其餘四項總和), 原×18
    // 倍率上修為×36(約2倍), 讓粗篩排序更貼近「兵種適性是勝率的主要驅動」這個實測結論。
    let bestTroopSum = 0;
    for (const tp of SGZ.TROOPS) {
      const s = team.reduce((a, g) => a + SGZ.aptPct(g, tp), 0);
      if (s > bestTroopSum) bestTroopSum = s;
    }
    score += bestTroopSum * 36;                       // 3人全S(1.2×3=3.6)上限約130
    // 角色互補: 批50驗證訊號居次(全值域擺幅約+0.40), 原×14小幅上修為×16。
    const roleUnion = new Set();
    team.forEach(g => generalRoles(g).forEach(r => roleUnion.add(r)));
    score += roleUnion.size * 16;                      // 最多4種 -> +64(16/角色, 與原14相近但驗證後小幅上修)
    // 批50: 移除原「主將位安排」項(錨點統率高低±8/-10) —— 迴歸係數約+0.001(全值域擺幅
    // 接近零, 三anchor符號甚至偏負), 對勝率無預測力, 純噪音。主將排列的語意判斷已由決選
    // 階段 leaderPermutations(批49)精確處理(guardFor/ifLeaderIs等結構化訊號), 粗篩層級這個
    // 「統率最高者當主將」的粗略近似不需要保留。
    return Math.round(score);
  }

  const baseOf = n => (n || "").replace(/^SP\s*/, "");

  // ---- 批53 A-1: 「M為核心」路徑 —— 沿用批48-49既有邏輯, 角色互補湊隊友(anchor固定在
  // team[0], 之後 stage2/stage3 的 leaderPermutations 仍會依語意重新排列主將, 這裡的順序
  // 只是候選生成階段的暫定順序)。----
  function stage1Heuristic(POOL, BONDS, anchorName, { limit = 250 } = {}) {
    const anchor = POOL[anchorName];
    const names = Object.keys(POOL).filter(n => n !== anchorName);
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

  // ---- 批53 A-2: 「M為拼圖」路徑 —— user實測坐實批48-49假設「選的卡=隊伍核心」的盲點:
  // 冷門/純輸出卡的最強用法常是「塞進現成強核當拼圖」(實例: 華雄自己當主將隊只5成多, 但
  // 塞進SP法正/關銀屏那組現成強核可達8成7)。做法借批52 league.js buildGuestTeams思路: 用
  // ratings.json(全池聯賽制評分, 見docs/league.js)裡「已知強核」當種子, 對每個強核嘗試把M
  // 頂替進去(頂替一個非主將位——保留原隊主將位的統領/主動戰法安排, 該位置通常是該隊之所以
  // 強的關鍵, 同批52 buildGuestTeams的取捨), 產生候選團隊。
  //
  // 種子強核來源: RATINGS(data/ratings.json的.generals, 每位武將的.teams含該將所有已知隊,
  // 含anchor隊與客串隊)——攤平去重成「隊伍→勝率」映射, 依勝率降冪排序, 取前 seedLimit 支
  // 當候選種子核(不需要重新聯賽, 直接複用批51/52已算好的571支隊伍全池, 見批52 note)。
  // RATINGS 缺失(未載入data/ratings.json, 如舊瀏覽器快取或離線測試)時優雅退化為空陣列——
  // 呼叫端(run())只是少了這一路候選, 「M為核心」路徑仍照常運作, 不拋錯。
  function seedSquadsFromRatings(RATINGS) {
    if (!RATINGS) return [];
    const seen = new Map();          // teamKey(sorted) -> {team(原順序), winRate}
    for (const rec of Object.values(RATINGS)) {
      if (!rec || !Array.isArray(rec.teams)) continue;
      for (const t of rec.teams) {
        if (!t || !Array.isArray(t.team) || t.team.length !== 3) continue;
        const key = t.team.slice().sort().join("|");
        const prev = seen.get(key);
        if (!prev || (t.winRate || 0) > prev.winRate) seen.set(key, { team: t.team, winRate: t.winRate || 0 });
      }
    }
    return Array.from(seen.values()).sort((a, b) => b.winRate - a.winRate);
  }

  // 對每個強核種子, 嘗試把 anchorName 頂替進去(跳過核心已含同源武將的種子, 如SP版本/本體
  // 撞名), 用 heuristicScore 粗評「頂替位1」vs「頂替位2」何者較優(同批52 buildGuestTeams
  // 手法: 保留種子隊的原主將位, 只在兩個非主將位之間選頂替點), 取分數較優的一種當候選團隊。
  // seedLimit 控制嘗試的種子數量(全池571支隊伍太多, 只取排名最前面的一批強核, 因為拼圖路徑
  // 的價值在於「M能不能搭上現成最強隊」, 排名靠後的種子核本身勝率就不出色, 頂替後也難超車)。
  function stage1Guest(POOL, BONDS, RATINGS, anchorName, { seedLimit = 120 } = {}) {
    const anchor = POOL[anchorName];
    if (!anchor) return [];
    const anchorBase = baseOf(anchorName);
    const seeds = seedSquadsFromRatings(RATINGS).slice(0, seedLimit);
    const candidates = [];
    const dedupe = new Set();
    for (const seed of seeds) {
      const squad = seed.team.filter(n => POOL[n]);       // 防禦: 種子隊含資料池已無的武將(理論上不會, 保守過濾)
      if (squad.length !== 3) continue;
      const bases = squad.map(baseOf);
      if (bases.includes(anchorBase)) continue;            // M自己(或其SP/本體)已在此強核, 跳過(頂替自己無意義)
      const [leader, s1, s2] = squad;
      const g1 = POOL[s1], g2 = POOL[s2];
      if (!g1 || !g2) continue;
      // 保留種子隊主將位(該隊之所以強, 主將位安排通常已針對種子隊本身優化), 只在兩個副將位
      // 之間選頂替點: 用 heuristicScore 粗評「M替掉位1」vs「M替掉位2」, 取分數較優者。
      const scoreReplace1 = heuristicScore(POOL, BONDS, POOL[leader], anchor, g2);   // M頂替位1(s1)
      const scoreReplace2 = heuristicScore(POOL, BONDS, POOL[leader], g1, anchor);   // M頂替位2(s2)
      const team = scoreReplace1 >= scoreReplace2 ? [leader, anchorName, s2] : [leader, s1, anchorName];
      const key = team.slice().sort().join("|");
      if (dedupe.has(key)) continue;
      dedupe.add(key);
      candidates.push({ team, score: Math.max(scoreReplace1, scoreReplace2), seedWinRate: seed.winRate, guestSeed: true });
    }
    return candidates;
  }

  // ---- GAUNTLET: 固定天梯陣容組 —— 批54前(手選6隊, 中等強度)覆蓋兵刃/謀略/控制/治療不同風格。----
  // 批54: user診斷坐實——舊天梯強度太弱, 所有配將器推薦隊(甚至白板馬鈞)vs舊天梯都飽和在
  // 95~100%勝率, 勝率失去鑑別力(使用者填不同卡看到的勝率都差不多高)。原因: GAUNTLET_DEF
  // 是批48手選的「中等隊」, 從未用批51/52的全池聯賽制實測校準過強度基準。
  // 保留 GAUNTLET_DEF 供 fallback(ratings.json 缺失時, 如league.js自身重新生成評分時的
  // 冷啟動, 或瀏覽器離線測試) —— 向後相容, league.js/舊呼叫點不受影響。
  const GAUNTLET_DEF = [
    { names: ["呂布", "趙雲", "關羽"], label: "兵刃猛攻" },
    { names: ["諸葛亮", "周瑜", "陸遜"], label: "謀略持續傷害" },
    { names: ["張角", "司馬懿", "貂蟬"], label: "控制壓制" },
    { names: ["曹操", "郭嘉", "華佗"], label: "指揮治療續戰" },
    { names: ["馬超", "黃忠", "張飛"], label: "兵刃爆發二型" },
    { names: ["劉備", "孫權", "孫策"], label: "指揮輔助" },
  ];
  // 批54: 強天梯 —— 從 ratings.json(批51/52全池聯賽制實測, 見docs/league.js) 的每位武將
  // .teams 攤平取「聯賽實測勝率最高」的隊伍, 依「成員去重」做多樣性過濾(避免天梯6~12隊
  // 全是同一組核心武將的排列組合——實測發現全池571隊裡勝率最高的一大票隊伍幾乎都共用
  // 「SP法正+關銀屏」這組槍隊核心, top100隊裡法正出現89次/關銀屏86次, 直接取topN或僅用
  // 「與上一隊最多共用1人」的寬鬆過濾都無法阻止這兩人反覆出現在多支天梯隊——故改用「每位
  // 武將全天梯只能出場1次」的硬性去重(usedMembers, 一旦某將入選任一天梯隊即整批鎖住不得
  // 再入選其他隊), 逼天梯真正覆蓋不同核心組合而非同一組核心的排列組合), 且盡量覆蓋四種
  // 兵種(槍/弓/騎/盾)各1~2支。
  // 過濾規則: 依隊伍勝率降冪掃描, 用「兵種輪詢」(每輪嘗試槍→弓→騎→盾各挑1支)取代單純
  // 掃全序, 讓弱勢兵種(場次少/評分低的騎兵隊等)不會被強勢兵種(槍兵隊集中在高分區)擠光。
  function seedGauntletTeamsFromRatings(RATINGS) {
    if (!RATINGS) return [];
    const seen = new Map();          // teamKey(sorted) -> {team(原順序), winRate, troop}
    for (const rec of Object.values(RATINGS)) {
      if (!rec || !Array.isArray(rec.teams)) continue;
      for (const t of rec.teams) {
        if (!t || !Array.isArray(t.team) || t.team.length !== 3) continue;
        const key = t.team.slice().sort().join("|");
        const prev = seen.get(key);
        if (!prev || (t.winRate || 0) > prev.winRate) seen.set(key, { team: t.team, winRate: t.winRate || 0, troop: t.troop || null });
      }
    }
    return Array.from(seen.values()).sort((a, b) => b.winRate - a.winRate);
  }
  // 批54實測校準(過程見scratchpad/b54_validate.js多輪試跑): ratings.json 全池571隊裡「真正
  // 頂尖」的隊伍高度集中在少數幾位glue武將(SP法正/關銀屏/魯肅/王異/諸葛亮反覆組合, top100
  // 隊裡法正出現89次/關銀屏86次)——嘗試過「每位武將全天梯只出場1次」的硬性去重, 結果只能
  // 湊出7隊且第4隊以後驟降至29~44%勝率(排名150名以後的隊伍本身戰力就明顯較弱), 拖累天梯
  // 整體強度, 反讓「拼圖式塞入強核」的候選能靠痛扁天梯後段弱隊洗高平均勝率, 白板馬鈞都能到
  // 86%——不符「vs頂尖天梯」的語意。改成「member cap=6不分兵種」測試後, 雖天梯整體夠強
  // (8隊皆80~98%), 卻又暴露另一個問題: 天梯8隊有6隊是槍/弓兵(法正/關銀屏/魯肅/王異圈子
  // 幾乎只打槍弓), 讓「弓兵S級適性+自帶強戰法」的呂布能靠單一兵種相性優勢衝到91%勝率——
  // 這不是呂布真的環境強(呂布在對571隊隨機對手的全池聯賽裡實際只是C階/rank165/勝率
  // 42.8%, 見ratings.json), 而是天梯兵種覆蓋太窄, 給了「兵種相性剋制」這個單一因素過大的
  // 槓桿。最終方案: 「glue武將」(法正/關銀屏/魯肅/王異/諸葛亮——反覆出現在各兵種top隊的
  // 高流動性核心, 允許沿用, 不然槍弓以外的兵種找不到夠強的隊伍)cap放寬到8, 其餘「非glue」
  // 成員(隊伍裡的第三人)cap收緊到1(不重複), 且用兵種輪詢強制湊滿槍/弓/騎/盾各2支——讓
  // 天梯維持高強度(全數76%+)之餘, 四種兵種都有代表隊伍, 不會被單一兵種相性優勢鑽漏洞。
  const RATINGS_GAUNTLET_GLUE = new Set(["法正", "關銀屏", "魯肅", "王異", "諸葛亮"]);
  const RATINGS_GAUNTLET_GLUE_CAP = 8;
  const RATINGS_GAUNTLET_OTHER_CAP = 1;
  const RATINGS_GAUNTLET_TROOPS = ["槍", "弓", "騎", "盾"];
  const RATINGS_GAUNTLET_PER_TROOP = 2;
  function buildRatingsGauntlet(RATINGS, { perTroop = RATINGS_GAUNTLET_PER_TROOP } = {}) {
    const rows = seedGauntletTeamsFromRatings(RATINGS);
    if (!rows.length) return null;
    const picked = [];
    const pickedKeys = new Set();
    const usage = new Map();          // baseOf(name) -> 已入選天梯隊數
    const troopCount = new Map();
    const tryPick = (filterTroop) => {
      for (const row of rows) {
        const key = row.team.slice().sort().join("|");
        if (pickedKeys.has(key)) continue;
        if (filterTroop && row.troop !== filterTroop) continue;
        if ((troopCount.get(row.troop) || 0) >= perTroop) continue;
        const bases = row.team.map(baseOf);
        const ok = bases.every(b => {
          const cap = RATINGS_GAUNTLET_GLUE.has(b) ? RATINGS_GAUNTLET_GLUE_CAP : RATINGS_GAUNTLET_OTHER_CAP;
          return (usage.get(b) || 0) < cap;
        });
        if (!ok) continue;
        picked.push(row);
        pickedKeys.add(key);
        bases.forEach(b => usage.set(b, (usage.get(b) || 0) + 1));
        troopCount.set(row.troop, (troopCount.get(row.troop) || 0) + 1);
        return true;
      }
      return false;
    };
    for (let round = 0; round < perTroop; round++) {
      for (const tp of RATINGS_GAUNTLET_TROOPS) tryPick(tp);
    }
    picked.sort((a, b) => b.winRate - a.winRate);
    return picked.map((row, i) => ({
      names: row.team, label: `聯賽第${i + 1}強（${row.troop || "?"}兵・勝率${Math.round(row.winRate * 100)}%）`,
    }));
  }
  // 批54: buildGauntlet(POOL, RATINGS) —— RATINGS 存在且能組出 >=6 隊強天梯時採用, 否則
  // fallback 回舊手選天梯(GAUNTLET_DEF)。RATINGS 為可選第二參數(舊呼叫點如league.js只傳
  // POOL 時 arguments.length===1, 不受影響, 向後相容)。
  function buildGauntlet(POOL, RATINGS) {
    if (RATINGS) {
      const strong = buildRatingsGauntlet(RATINGS);
      if (strong) {
        const filtered = strong.map(d => ({ ...d, names: d.names.filter(n => POOL[n]) }))
          .filter(d => d.names.length === 3);
        if (filtered.length >= 6) return filtered;
      }
    }
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

  function vsGauntletWinRate(POOL, team, params, gauntlet, n, scenario, troopAOverride) {
    const troopA = troopAOverride || SGZ.teamTroop(POOL, team);
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

  // ---- 批49 B: 兵種雙方案 —— 「隊伍適性加權」前2名兵種(主將適性加權高一點, 主將S優先於
  // 副將S, 貼合「主將兵種適性決定戰場定位」的直覺), 供決選階段各跑一輪比較, 取較優者。----
  function weightedTroopRanking(POOL, team) {
    const scored = SGZ.TROOPS.map(tp => {
      const s = team.reduce((a, n, i) => a + SGZ.aptPct(POOL[n], tp) * (i === 0 ? 1.5 : 1), 0);
      return { troop: tp, score: s };
    });
    scored.sort((a, b) => b.score - a.score);
    return scored;
  }
  function top2Troops(POOL, team) {
    const ranked = weightedTroopRanking(POOL, team);
    const out = [ranked[0].troop];
    if (ranked[1] && ranked[1].troop !== ranked[0].troop) out.push(ranked[1].troop);
    return out;
  }

  // ---- 決選: 貪心配傳承戰法 —— 按 quality S>A>B 優先 + type 與隊伍現有戰法互補(避免同隊重複 command/charge 過量) + 不與自帶衝突(同名) ----
  // 批49 C: team 現為「主將排列後」的陣列(team[0]=主將), troop 為本輪決選採用的兵種(供
  // troopLimit 兵種合法性過濾)。新增語意過濾: (1) teamGate 不滿足的陣法整條排除(如潛龍陣三
  // 陣營異等但隊伍不符); (2) 批E: troopLimit(見上方 troopMismatch)與 troop 不符者整條排除
  // (非降檔, 直接不指派——傳承戰法只應指派給隊伍實際能裝載的兵種, 否則是非法配置);
  // (3) activeOnly/activeFired 依賴「持有者自身有主動戰法」的戰法, 持有者(含此次已指派的
  // 傳承)沒有 type:"active" 戰法時排除; (4) ifLeaderIs 指名武將不在隊上時降檔(排到候選池
  // 最後, 除非其 base 段本身仍勝過其他選項才會被選中——用「附加惰性排序鍵」而非直接排除,
  // 因為 base 段仍可能生效, 只是「統領增益」這塊機會成本高)。
  function pickInheritTactics(POOL, TAC_DATA, TAC_TIER, NONEQUIP, team, troop, teamFactions) {
    const tierRank = { S: 0, A: 1, B: 2 };
    const factions = teamFactions || team.map(n => POOL[n] && POOL[n].faction);
    const names = Object.keys(TAC_DATA).filter(nm => !NONEQUIP.has(nm));
    const pool = names.map(nm => ({ nm, t: TAC_DATA[nm], tier: TAC_TIER[nm] || "C" }))
      .filter(x => x.t && x.t.type !== "none")
      .filter(x => !(x.t.teamGate && !teamGateOkLocal(x.t.teamGate, factions)))     // (1) teamGate 不滿足整條排除
      .filter(x => !troopMismatch(x.t, troop))                                     // (2) troopLimit 兵種不符整條排除(批E)
      .sort((a, b) => {
        const dl = (leaderIsUndeployed(a.t, team) ? 1 : 0) - (leaderIsUndeployed(b.t, team) ? 1 : 0);   // (4) ifLeaderIs指名不在隊降檔
        if (dl) return dl;
        return (tierRank[a.tier] ?? 9) - (tierRank[b.tier] ?? 9);
      });
    // 陣法/兵種類(FORMATION/TROOP)全隊只應有一人持有(同 app.js teamHasCat 慣例), 貪心指派時
    // 隊上已有人佔用該分類後, 後續其他人不再指派同分類的傳承戰法(留給未來更精細的位置感知再優化)。
    const teamCatTaken = new Set();
    const inh = team.map(() => [null, null]);
    const inhNames = team.map(() => []);                // 供 (3) 逐步累積判斷「此人是否已有主動戰法」
    const used = new Set();                            // 已指派的戰法名(隊內不重複)
    for (let slot = 0; slot < 2; slot++) {              // 每人最多2個傳承欄
      for (let i = 0; i < team.length; i++) {
        const g = POOL[team[i]];
        if (!g) continue;
        const nativeName = g.tacticName;
        const pick = pool.find(x => !used.has(x.nm) && x.nm !== nativeName &&
          !inh[i].includes(x.nm) &&
          !((x.t.cat === "FORMATION" || x.t.cat === "TROOP") && teamCatTaken.has(x.t.cat)) &&
          !(requiresOwnActive(x.t) && !holderHasActiveTactic(g, inhNames[i], TAC_DATA))   // (3) 無主動戰法者排除主動依賴戰法
        );
        if (pick) {
          inh[i][slot] = pick.nm; used.add(pick.nm); inhNames[i].push(pick.nm);
          if (pick.t.cat === "FORMATION" || pick.t.cat === "TROOP") teamCatTaken.add(pick.t.cat);
        }
      }
    }
    return inh.map(a => a.filter(Boolean));
  }
  function teamGateOkLocal(gate, factions) {          // 對稱 engine.js teamGateOk(不 import 內部函式, 這裡自帶一份供指派前置過濾)
    if (!gate || !gate.factions) return true;
    const uniq = new Set(factions).size;
    if (gate.factions === "allDiff") return uniq === factions.length;
    if (gate.factions === "allSame") return uniq === 1;
    return true;
  }
  function leaderIsUndeployed(t, team) {              // true = 此戰法 ifLeaderIs 指名的武將都不在隊上(統領加成必吃不到, 只剩 base 段)
    const info = leaderSeekingInfo(t);
    if (!info.seeks || !info.names) return false;
    for (const n of info.names) if (team.includes(n)) return false;
    return true;
  }

  // ---- 主流程: runMatchmaker(POOL, BONDS, TAC_DATA, TAC_TIER, NONEQUIP, anchorName, opts) ----
  // opts: { scenario, onProgress(stage, pct), stage1Limit, stage2Keep, stage2N, stage3N }
  // 批53: ctx 新增可選 RATINGS(data/ratings.json的.generals, 見docs/league.js) —— 供「M為
  // 拼圖」路徑(stage1Guest)當強核種子來源。缺失時該路徑退化為空陣列, 只剩「M為核心」路徑,
  // 行為與批48-52相同(向後相容, 不破壞既有呼叫點如league.js/scratchpad驗證腳本)。
  async function run(ctx, anchorName, opts) {
    const { POOL, BONDS, TAC_DATA, TAC_TIER, NONEQUIP, scenario, RATINGS } = ctx;
    const onProgress = opts && opts.onProgress || (() => {});
    const stage1Limit = (opts && opts.stage1Limit) || 260;
    const guestSeedLimit = (opts && opts.guestSeedLimit) || 120;   // 批53 A-2: 拼圖路徑嘗試的強核種子數上限
    const stage2Keep = (opts && opts.stage2Keep) || 20;
    const stage2N = (opts && opts.stage2N) || 150;
    const stage3N = (opts && opts.stage3N) || 800;
    const comboN = (opts && opts.comboN) || 150;        // 批49 A/B: 主將排列×兵種雙方案比較用的小樣本(預設150, 對稱user規格「n=150」)
    const topOut = (opts && opts.topOut) || 5;

    if (!POOL[anchorName]) throw new Error("武將不存在於當前資料池: " + anchorName);
    // 批54: 傳入 RATINGS 供 buildGauntlet 組「強天梯」(vs 頂尖對手, 見該函式註解), RATINGS
    // 缺失時內部優雅退化回舊手選天梯 GAUNTLET_DEF, 呼叫端(app.js單卡配將)不需額外改動。
    const gauntlet = buildGauntlet(POOL, RATINGS);

    // Stage 1: 粗篩(啟發式, 全池配對) — 分片跑避免長任務凍結 UI。批53: 雙路構造合併——
    // (A-1)「M為核心」角色互補湊隊友(批48-49既有邏輯) + (A-2)「M為拼圖」塞進ratings.json
    // 已知強核(批53新增), 依 teamKey 去重合併後一起送進 stage2 海選(同一套漏斗, 不特殊
    // 待遇——拼圖路徑候選數天然少很多(至多guestSeedLimit支), 不會排擠核心路徑的名額)。
    onProgress("stage1", 0);
    const coreCandidates = stage1Heuristic(POOL, BONDS, anchorName, { limit: stage1Limit });
    const guestCandidates = stage1Guest(POOL, BONDS, RATINGS, anchorName, { seedLimit: guestSeedLimit });
    const seenKeys = new Set(coreCandidates.map(c => c.team.slice().sort().join("|")));
    const mergedGuest = guestCandidates.filter(c => {
      const key = c.team.slice().sort().join("|");
      if (seenKeys.has(key)) return false;
      seenKeys.add(key);
      return true;
    });
    const candidates = coreCandidates.concat(mergedGuest);
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

    // Stage 3: 決選(精算) — 貪心配傳承戰法 + 兵書預設 + 兵種營Lv10, 大樣本模擬(同上: 時間預算分片)。
    // 批49 A/B: 決選前先跑一輪「組合比較」——主將排列(語意過濾後最多3種)× 兵種雙方案(隊伍
    // 適性加權前2名), 每種組合用小樣本(comboN)快篩, 取勝率最高的組合才進最終大樣本(stage3N)
    // 精算, 避免把「排列比較」的額外模擬量全數乘上 stage3N(會讓總時長暴增數倍, 超出45s預算)。
    const results = [];
    sliceStart = nowMs();
    for (let i = 0; i < finalists.length; i++) {
      const c = finalists[i];
      const leaderPerms = leaderPermutations(POOL, c.team);        // A: 語意過濾後的主將排列(1~3種)
      let bestCombo = null;
      for (const perm of leaderPerms) {
        const troops2 = top2Troops(POOL, perm);                    // B: 該排列下的兵種雙方案(1~2種)
        for (const troop of troops2) {
          const factions = perm.map(n => POOL[n] && POOL[n].faction);
          const inh = pickInheritTactics(POOL, TAC_DATA, TAC_TIER, NONEQUIP, perm, troop, factions);
          const params = defaultParamsFor(POOL, perm);
          params.inh = inh;
          const win = vsGauntletWinRate(POOL, perm, params, gauntlet, comboN, scenario, troop);
          if (!bestCombo || win > bestCombo.win) bestCombo = { team: perm, troop, inh, params, win };
        }
      }
      // 精算: 取上一步最佳組合, 大樣本(stage3N) vs GAUNTLET 精確評分
      const { team, troop, inh, params } = bestCombo;
      let totalWin = 0, totalRounds = 0, count = 0;
      for (const foe of gauntlet) {
        const fp = defaultParamsFor(POOL, foe.names);
        const troopB = SGZ.teamTroop(POOL, foe.names);
        const r = SGZ.simulate(POOL, team, foe.names, stage3N, troop, troopB,
          params.bs, fp.bs, params.eq, fp.eq, params.ad, fp.ad, params.inh, fp.inh, scenario, 10, 10);
        totalWin += r.winA; totalRounds += r.rounds; count++;
      }
      // 批53: M(anchorName)在最終排列中的角色 —— 主將(team[0]===anchorName)或副將。UI據此
      // 標註「M當主將／M當副將(拼圖)」, 不論候選來自A-1核心路徑或A-2拼圖路徑, 一律以「決選後
      // 實際排列」判斷(拼圖路徑候選也可能因語意排列篩選/組合快篩結果被排到主將位, 反之核心
      // 路徑候選也可能因guardFor等語意約束被排到副將位——role以事實為準, 不看候選來源)。
      const anchorRole = team[0] === anchorName ? "leader" : "support";
      results.push({
        team, win: totalWin / count, rounds: totalRounds / count,
        troop, bs: params.bs, inh, ad: params.ad,
        anchorRole,
        reason: buildReason(POOL, BONDS, team, troop, anchorName, anchorRole),   // D: 推薦理由(批53: 補M角色說明)
      });
      onProgress("stage3", Math.round(((i + 1) / finalists.length) * 100));
      if (nowMs() - sliceStart > 80) { await tick(); sliceStart = nowMs(); }
    }
    results.sort((a, b) => b.win - a.win);
    return { top: results.slice(0, topOut), gauntlet };
  }

  // ---- 批49 D: 推薦理由 —— 命中的緣分名/陣營加成/角色互補標籤/主將安排原因, 供user判斷推薦合理性 ----
  // 批53: 新增 anchorName/anchorRole 參數, 補一句「M當主將／M塞進XX當拼圖副將」說明——user
  // 的核心訴求是「用這張卡能排出的最強陣容」, 尤其當M是拼圖式塞進他人強核時, 使用者需要
  // 明確知道「這隊為什麼強」不是M自己的功勞, 而是M搭上了現成強核(如SP法正/關銀屏)。
  function buildReason(POOL, BONDS, team, troop, anchorName, anchorRole) {
    const parts = [];
    if (anchorName) {
      if (anchorRole === "leader") parts.push(`${anchorName}當主將（核心陣容）`);
      else {
        const others = team.filter(n => n !== anchorName);
        parts.push(`${anchorName}塞進「${others.join("／")}」強核當副將（拼圖式搭配）`);
      }
    }
    const bonds = (BONDS || []).filter(b => (b.generals || []).filter(n => team.includes(n)).length >= (b.triggerCount || 99));
    if (bonds.length) parts.push("緣分：" + bonds.map(b => b.name).join("、"));
    const facCount = {};
    team.forEach(n => { const f = POOL[n] && POOL[n].faction; facCount[f] = (facCount[f] || 0) + 1; });
    const maxFacEntry = Object.entries(facCount).sort((a, b) => b[1] - a[1])[0];
    if (maxFacEntry && maxFacEntry[1] >= 2) parts.push(`陣營：${maxFacEntry[1]}人同屬「${maxFacEntry[0]}」`);
    const roleUnion = new Set();
    team.forEach(n => POOL[n] && generalRoles(POOL[n]).forEach(r => roleUnion.add(r)));
    const ROLE_LABEL = { dmg: "輸出", ctrl: "控制", heal: "治療", support: "輔助" };
    if (roleUnion.size) parts.push("角色互補：" + Array.from(roleUnion).map(r => ROLE_LABEL[r] || r).join("、"));
    const lr = leaderReason(POOL, team);
    if (lr) parts.push("主將安排：" + lr);
    parts.push(`兵種：${troop}兵`);
    return parts.join("　｜　");
  }

  const nowMs = () => (typeof performance !== "undefined" ? performance.now() : Date.now());
  function tick() { return new Promise(res => setTimeout(res, 0)); }

  root.Matchmaker = {
    run, buildGauntlet, stage1Heuristic, heuristicScore, tacticRoles, generalRoles, defaultParamsFor, pickInheritTactics, GAUNTLET_DEF,
    // 批49: 供 E2E 測試腳本直接驗證語意約束
    isGuardLeaderTactic, leaderFitness, leaderPermutations, leaderSeekingInfo, namedLeaderInTeam,
    troopMismatch, requiresOwnActive, holderHasActiveTactic, weightedTroopRanking, top2Troops, buildReason,
    // 批50: 供漏斗驗證實驗腳本(scratchpad/funnel_validate.js)直接呼叫, 與 stage2 用同一支勝率函式(口徑一致)
    vsGauntletWinRate,
    // 批53: 「M為拼圖」路徑, 供 E2E 測試腳本直接驗證(如檢查華雄的候選是否含SP法正/關銀屏強核)
    stage1Guest, seedSquadsFromRatings,
    // 批54: 強天梯, 供 league.js/驗證腳本直接檢視組成或重跑診斷
    buildRatingsGauntlet, seedGauntletTeamsFromRatings,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.Matchmaker;
})(typeof window !== "undefined" ? window : globalThis);
