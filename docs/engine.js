// 三國志戰略版 配將引擎 — sgz.py 的 JS 移植(瀏覽器/node 通用)
// 兵種由「隊伍」決定; 各武將只有對該兵種的適性(S/A/B/C/D)決定屬性發揮。克制為隊伍兵種 vs 隊伍兵種。
// 原語: coef amp mitig stun heal stat dot settle extra redirect stack decay swap pierce counter
//       silence disarm insight first taunt shield dodge surehit rateup
"use strict";
(function (root) {
  const ROUNDS = 8, START_TROOP = 10000, MORALE = 100;
  const CITY = 20, FACTION = 1.10;                   // 城建滿(武智統速各+20) + 陣營滿(全屬性+10%), 雙方皆有
  const CAMP = 4;                                     // 兵種營: 戰報「弓兵營全屬性提升了4」→ 全屬性平加(獨立階段, 在陣營乘算之後), 雙方皆有
  // 批35 D: block(抵禦/警戒) 消耗門檻 —— grok查證機鑑先識原文「受到的傷害超過自身可攜帶
  // 最大兵力的6%時(最低100兵力)」才消耗1次警戒。max(START_TROOP×6%, 100) —— 本引擎
  // START_TROOP恆為10000(單一兵力池常數, 無「可攜帶最大兵力」與「當下兵力」之分), 6%=600
  // 本身已遠大於100下限, 下限條款只在極端自訂規模才會生效, 此處仍照原文寫出以求精確。
  const BLOCK_CONSUME_THRESHOLD = Math.max(START_TROOP * 0.06, 100);
  // 「受X影響」屬性縮放旋鈕。輸入為戰鬥內即時素質 caster.eff(stat)(已含城建/陣營/適性/
  // 加點/賽季/戰鬥中buff, 典型值 250~400, 而非卡面裸值)。公式取社群拆解(巴哈姆特高等陣容
  // 戰法論/NGA數據貼): 屬性100=面板基準值(SCALE=1.0), 每+350點效果翻倍(v=450時SCALE=2.0)。
  // 仍是可調校準旋鈕, 之後有更多實測數據可再調整斜率/錨點。
  // 批35: SCALE_G(v, div) —— 曲線族原語泛化。除數預設350(向後相容, 傷害/治療/多數增減益類走
  // 這條), 但 docs/data/calibration_anchors.json → status_scale_375_20260704(user 機鑑先識
  // 警戒六點實測, 荀彧智力478.84~389.72, 六點小數點後兩位精確吻合)證實「狀態效果」(block/
  // 部分%值狀態類)這一族走除數375的獨立曲線(375點翻倍, 而非350)。e.scaleDiv(效果級可選欄位)
  // 覆蓋預設除數, 供逐效果標記走哪條曲線 —— 不擅自把全域 SCALE 從350改成375, 只有明確有實測
  // 錨點佐證的效果才標 scaleDiv:375(見 tactics_parsed.json「機鑑先識」)。
  const SCALE_G = (v, div) => Math.max(0, 1 + (v - 100) / (div || 350));
  const SCALE = v => SCALE_G(v, 350);
  const SCALE_CLAMP = 1.5;                            // amp/mitig 縮放後上限保護: |val| <= 1.5
  // 批35: scaleOf 第三參數 scaleDiv(可選) —— 效果級 e.scaleDiv 透傳, 預設350(SCALE 向後相容)。
  // 批I(禁近似令-scale/比較族): scale==="maxStat" —— 動態取施放者當下四維(force/intel/
  // command/speed, 不含魅力)中最高一項代入SCALE_G, 取代「受自身最高屬性影響」的固定取值
  // 近似(扶危定傾/剛柔並濟/整軍經武等, 見engine_limitations.md第12/6.6節鏡像缺口)。零新增
  // 呼叫點: 全庫既有svVal/svMult/svAdd/lockedScaleOf一律透過此函式讀取scale倍率, scale
  // 欄位本身早已是全域已知欄位, 只是多一個合法字串值, prep鎖定沿用lockedScaleOf既有委派。
  const scaleOf = (caster, scale, scaleDiv) => {
    if (!scale) return 1;
    if (scale === "maxStat") return SCALE_G(Math.max(caster.eff("force"), caster.eff("intel"), caster.eff("command"), caster.eff("speed")), scaleDiv);
    return scale === "charm" ? SCALE_G(caster.charm, scaleDiv) : SCALE_G(caster.eff(scale), scaleDiv);
  };
  // 批I: e.stat==="maxStat" —— 動態解析為 u 當下四維最高的一項欄位名, 供 k==="stat" 效果
  // 動態選定要加成哪個屬性(形一陣「自身最高屬性+30→60點」)。與 scale==="maxStat"(見上)
  // 共用「四維中最高一項」語意, 但消費端不同(這裡回傳屬性欄位名字串, 上面回傳縮放倍數)。
  const resolveStatField = (u, stat) => {
    if (stat !== "maxStat") return stat;
    const stats4 = ["force", "intel", "command", "speed"];
    return stats4.reduce((best, s) => (u.eff(s) > u.eff(best) ? s : best), stats4[0]);
  };
  // 批I: ifStatCompare —— 比較「參照方」(caster自身或我軍主將)vs「目標」同一屬性的大小,
  // 決定效果/extraHits段是否生效(布林gate, 對稱ifTargetHas但比較的是「屬性大小」而非
  // 「狀態有無」)。spec: {stat, op("gt"/"gte"/"lt"/"lte", 預設"gt"), vs("caster"預設/
  // "leader")}。op 語意固定為「參照方 op 目標」方向。見 sgz.py statCompareOk 同名對稱函式
  // 詳細註解(三筆真實案例驗證此形狀已是最小通用形)。
  const statCompareOk = (ref, target, allies, spec) => {
    if (!spec || !target) return false;
    const stat = spec.stat || "force";
    const op = spec.op || "gt";
    const vs = spec.vs || "caster";
    const refU = (vs === "leader" && allies && allies.length) ? allies[0] : ref;
    if (!refU) return false;
    // 禁近似令-批L: stat==="hpPct" —— 比較雙方「兵力百分比」(troop/START_TROOP)而非傳統
    // 四維屬性, 供先登死士「若兵力百分比低於攻擊者」這類跨單位血量比較(對稱既有when.hpBelow/
    // hpAbove只認caster自身, 這裡是ref/target雙方各自讀u.hpPct, 走既有ifStatCompare的op/vs
    // 骨架, 零新增比較邏輯, 只新增一種可讀的stat名稱)。
    const rv = stat === "charm" ? refU.charm : (stat === "hpPct" ? refU.hpPct : refU.eff(stat));
    const tv = stat === "charm" ? target.charm : (stat === "hpPct" ? target.hpPct : target.eff(stat));
    if (op === "gt") return rv > tv;
    if (op === "gte") return rv >= tv;
    if (op === "lt") return rv < tv;
    if (op === "lte") return rv <= tv;
    return false;
  };
  // 批I: scaleCompare —— 施放者vs目標同一屬性「差值」代入縮放曲線, 對稱scaleOf(單方固定
  // 屬性)但讀取雙方差值(神機妙算「並基於雙方智力差額外提高」)。spec: {stat(預設"intel"),
  // div(選填, 預設350)}。diff=0時倍率=1.0(無額外加成)。見 sgz.py scaleCompareOf 同名對稱
  // 函式詳細註解。
  const scaleCompareOf = (caster, target, spec) => {
    if (!spec || !target) return 1;
    const stat = spec.stat || "intel";
    const div = spec.div || 350;
    const cv = stat === "charm" ? caster.charm : caster.eff(stat);
    const tv = stat === "charm" ? target.charm : target.eff(stat);
    return Math.max(0, 1 + (cv - tv) / div);
  };
  // 批35: capValOf(v, capVal) —— 效果級可選欄位 e.capVal(值上限), 縮放後 clamp。慣例「狀態效果
  // 上限=基礎值×2」(錨點: 機鑑先識 40%→80% cap)不自動套用(每個效果的「基礎值」需自行定義,
  // 無法在此泛化推得), 逐效果顯式標 e.capVal(如機鑑先識 val:0.4 → capVal:0.8)。未標則不 clamp
  // (向後相容既有資料, 只受既有 SCALE_CLAMP/block 0~1 clamp 等既有保護)。
  const capValOf = (v, capVal) => capVal != null ? Math.min(v, capVal) : v;
  // 批35 B: 「受X影響」狀態值類效果(block 為主, 現行機鑑先識警戒) 的「準備階段鎖定」語意
  // —— 效果的 scale 縮放值(scaleOf 結果)在 prep 階段(第一次掃描到該效果, 不論它本身是否於
  // prep 就實際套用)算定並鎖住, 之後(如 everyRound 補層段延後到第2/3回合才擲骰命中)一律沿用
  // 鎖定值, 不因戰鬥中智力浮動(如中途獲得的 stat buff)重新計算。見 docs/data/
  // calibration_anchors.json → status_scale_375_20260704 laws: 「數值鎖定準備階段: 裝備
  // (出奇馬)在戰法計算前生效算入, 開戰後智力變動不影響」——與 heal 的 healBase 準備階段鎖定
  // 兵力快照同一慣例(第二次獨立確認)。用「效果物件本身」當 Map 鍵(同一效果物件的 prep 掃描與
  // 之後任何回合的重新套用共用同一把鎖, 天然對應同一戰法同一效果段); 只用於帶 e.scale 的
  // block(機鑑先識等狀態值類效果的代表原語), 不擴大到其餘所有 k(amp/mitig/stat 等目前無
  // 對應實測樣本佐證「準備階段鎖定」是否同樣適用, 保守只鎖 block, 見批35 brief B段)。
  function lockedScaleOf(caster, e) {
    if (!e.scale) return 1;
    if (!caster.scaleLock) caster.scaleLock = new Map();
    let v = caster.scaleLock.get(e);
    if (v === undefined) { v = scaleOf(caster, e.scale, e.scaleDiv); caster.scaleLock.set(e, v); }
    return v;
  }
  // 批7: 發動率類「受X影響」縮放 —— 獨立常數, 與上面 SCALE(每+350翻倍) 不是同一條曲線。
  // user 實測太平道法(黃巾/張角, docs/data/calibration_anchors.json → rate_scale): 智力484.6
  // 才翻倍(對比 SCALE 只要+350即450), 反解 c=0.002598(6組獨立點一致到小數第6位, 取0.0026)。
  // chargeup 尚無獨立實測, 暫共用同常數(假設同曲線, 待未來樣本校準)。
  const RATE_SCALE_C = 0.0026;                        // 對應除數 1/0.0026≈384.6(太平道法曲線, RATE_SCALE_DEFAULT_DIV 的等價斜率寫法, 兩者數學等價, 保留常數名稱向後相容)
  const RATE_SCALE_DEFAULT_DIV = 1 / RATE_SCALE_C;    // ≈384.6154 —— rateup/chargeup 預設曲線除數(向後相容, 未標 e.scaleDiv 的既有資料沿用此值, 結果與舊版 RATE_SCALE_C 逐位元一致)
  // 批46 A: e.scaleDiv(比照 amp/mitig 的 scaleOf 第三參數慣例) —— 效果級可選欄位, 覆蓋預設除數,
  // 供逐效果標記走哪條「發動機率受X影響」曲線。實測依據: 十二奇策(docs/data/
  // calibration_anchors.json → shierqice_20260707) user七點齊發精確收斂 D=335.1±0.15, 與太平道法
  // 的384.6是兩條不同斜率的獨立曲線(同語意「受智力影響提高發動機率」, 但不同戰法数值出處不同,
  // 比照 SCALE_G 的 375/350 慣例, 不擅自把預設384.6改掉, 只在有明確實測錨點佐證的效果才標
  // scaleDiv:335)。
  const rateScaleOf = (caster, scale, scaleDiv) => {
    const div = scaleDiv || RATE_SCALE_DEFAULT_DIV;
    return scale === "charm" ? 1 + (caster.charm - 100) / div : (scale ? 1 + (caster.eff(scale) - 100) / div : 1);
  };
  // 批18: 傷兵池(治療上限) —— user 遊戲實測: 受到的傷害按「當時回合數」轉化為「可救援(計入
  // 傷兵池, 治療只能回這部分)」vs「不可救援(直接陣亡, 治療無法挽回)」, 轉化率隨回合遞減
  // (見 docs/data/calibration_anchors.json → wounded_pool)。1~3回合90%、4~6回合80%、
  // 7~8回合67.5%(原文65~70%取中值)。準備階段(CUR_R=0)算第1回合檔(尚未進入回合迴圈, 但
  // 兵書/裝備/被動等準備階段效果如 dot/settle 快照造成的傷害仍需計入傷兵池)。
  const WOUNDED_RATES = [0.90, 0.90, 0.90, 0.80, 0.80, 0.80, 0.675, 0.675];  // index 0 = 第1回合
  const woundedRate = r => WOUNDED_RATES[Math.max(0, Math.min(WOUNDED_RATES.length, r || 1) - 1)];
  // 批33: 治療(heal)絕對量公式全局換裝 —— 舊公式 want = coef×SCALE(scale屬性)×caster.troop×0.10
  // 疑似系統性高估(見 engine_limitations.md 第18節: 陷陣營樣本高估1.6~2倍, 且形狀錯誤——
  // 治療量不應隨施放者「當下」兵力增減, 官方戰報顯示同一施放者兵力隨戰鬥推移下降時治療量不變)。
  // 初版曾裁決 want=506×coef×SCALE(不乘兵力), 但 user 補測華佗2(智力228/準備階段兵力9600/
  // 青囊96%→實測755)推翻該版本: 506那組樣本(青囊96%/智力284→742)恰好是施放者準備階段兵力
  // ~8433的巧合摺疊(506≈0.06×8433), 換一個準備兵力不同(9600)的樣本立刻對不上(506版預測
  // 663, 誤差14%; 而"×準備階段兵力"版預測755.2, 誤差0.03%)。
  // 最終公式(docs/data/calibration_anchors.json → heal_formula_resolved_20260704, 後續更新):
  //   want = coef(治療率) × HEAL_TROOP_C(0.06) × 施放者準備階段鎖定兵力 × SCALE(scale屬性,預設intel)
  // 「準備階段鎖定」語意: 指揮/兵種/兵書/被動類 heal(常駐急救型)的治療量以「開戰準備階段的
  // 兵力」定格(華佗1當下兵力8611~8781持續變動但治療恆742, 非隨當下兵力浮動), 故用
  // caster.healBase(prep時存的 troop×HEAL_TROOP_C 快照, 見 Unit 建構)而非 caster.troop×常數。
  // active主動直療型(如刮骨療毒, 施放當下即時觸發的治療, 非受傷反應式)用施放當下即時兵力
  // (caster.troop)。刮骨樣本初次核對曾疑似-11%偏差(疑主動型基底常數有異), 後證實該樣本
  // 傷兵池已耗盡、觀測值為封頂後殘值(非公式未封頂前的真實want), 與公式無關——主動直療型與
  // 反應式急救型共用同一套公式(HEAL_TROOP_C), 不分型態另設基底常數, 僅兵力取值時點不同。
  // 驗證樣本: 陷陣營60%/智力379.02/準備兵力8439→546(反解值, 弱錨點); 青囊96%/智力228/
  // 準備兵力9600→755(強錨點, user新補測, 0.03%誤差)。
  // 補充參考樣本(第三批戰報, 未落地到具體戰法資料——「離月」在本庫查無此戰法, 疑user口誤/
  // 待查證, 暫不修改任何tactics資料, 僅記錄公式驗證結果供未來核對): 直療68%/貂蟬智力397/
  // 開場兵力8580→曹操622×2+陸遜627, v2公式(want=0.68×0.06×8580×SCALE(397))預測647.1,
  // 殘差約-3%~-4%(可能戰內智力浮動), 在既有容忍帶內, 不阻塞, 亦不改動公式常數。
  const HEAL_TROOP_C = 0.06;
  const COUNTER = { "騎": "盾", "盾": "弓", "弓": "槍", "槍": "騎" };
  const APT_PCT = { S: 1.20, A: 1.00, B: 0.85, C: 0.70, D: 0.55 };
  const APT_RANK = { S: 4, A: 3, B: 2, C: 1, D: 0 };
  const TROOPS = ["騎", "盾", "弓", "槍", "器"];
  // 批36: 兵種營建築(Lv0~10) —— 錨點 docs/data/calibration_anchors.json → troop_camp「三合一」
  // 拆解: (1) 全屬性+4(=既有CAMP常數, 全域已無條件套用, 非本批新增) (2) 每級+0.25%該兵種造成
  // 傷害(本批新增, 滿級+2.5%) (3) Lv10附贈對應兵種戰法(本批新增attach邏輯)。CAMP_DMG_PER_LV
  // 直接作用在 amp 原語(「造成傷害提升」, 與現有進階/典藏 a.amp 同慣例, 見下方 Unit 建構)。
  const CAMP_DMG_PER_LV = 0.0025;
  // 兵種(隊伍) → 該兵種營Lv10附贈戰法名稱(見 tactics_parsed.json cat/src:"BUILDING" 五筆)。
  // 器械營「負重」無戰鬥內效果(type:"none", 已被 buildPool 的 t.type!=="none" 過濾, 不進
  // TACTICS 表), 故器械不掛(對稱書寫仍列出, 值為 null, 供 attach 邏輯統一走同一張表)。
  const CAMP_TROOP_TACTIC = { "槍": "破軍", "盾": "守禦", "弓": "齊射", "騎": "疾馳", "器": null };
  let BINGSHU = {}, MAIN_BY_CAT = {}, SUB_BY_CAT = {};  // 兵書: 名稱→效果; 類別→主/副兵書們
  let TACTICS = {};                                 // 名稱→戰法(供傳承查詢)
  let BONDS = [], EQUIPS = {};                       // 緣分(隊伍級) / 裝備(自身)
  let SEASON_MODS = {};                              // 賽季修正 {id:[mod]}
  let TRACE = null, CUR_R = 0;                        // 推演日誌: TRACE=陣列時記錄事件; CUR_R=當前回合(0=準備)
  // 批52i: fight 期回呼(proxyNormal 代打完整普攻含突擊)
  let _FIGHT_CTX = { onHit: null, onDeal: null, alliesOf: null, foesOf: null, activeFired: null };
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
  // 禁近似令-批K: countAllyFaction(faction_count_scale族) —— 數出隊伍(allies, 含自己)中
  // 陣營恰為 faction 的存活人數。與 teamGateOk(只回傳布林值allDiff/allSame)不同層級: 這裡
  // 回傳實際計數, 供 rateFactionBonus(見 applyEffects eRate 計算段)線性縮放觸發率使用
  // (南蠻渠魁/象兵「部隊每多1名蠻族武將額外提高X%機率」)。
  function countAllyFaction(allies, faction) {
    if (!faction) return 0;
    return (allies || []).filter(a => a.alive && a.g && a.g.faction === faction).length;
  }
  // 禁近似令-批K: countActiveBuffTypes(rate_self_dynamic族) —— 數出 u 當下持有的「功能性
  // 增益狀態」種類數(僅認連擊/洞察/先攻/必中/破陣五種, 對應臥薪嘗膽本文列舉的候選池), 供
  // rateBonusPerBuffType(見 applyEffects eRate 計算段)動態加成觸發率使用。
  function countActiveBuffTypes(u, types) {
    if (!u || !types || !types.length) return 0;
    let n = 0;
    for (const ty of types) {
      if (ty === "extra" && u.addbonus("extra") > 0) n++;
      else if (ty === "insight" && u.insight > 0) n++;
      else if (ty === "first" && u.first > 0) n++;
      else if (ty === "surehit" && u.surehitDur > 0) n++;
      else if (ty === "pierce" && u.addbonus("pierce") > 0) n++;
      else if (ty === "dodge" && u.dodgeDur > 0) n++;
    }
    return n;
  }
  class Unit {
    constructor(g, ttype, bsName, eqName, add, inherit, season, teamFactions, campLv, isCampHolder) {
      this.g = g; this.ttype = ttype; this.troop = START_TROOP; this.stun = 0;
      this.campLv = campLv || 0;                   // 批36: 兵種營等級(0~10, 隊伍級, 見 fight() 呼叫端), 0=不啟用(向後相容既有全部呼叫點)
      // 批33: healBase —— 準備階段鎖定的治療基準兵力快照(troop×HEAL_TROOP_C), 供指揮/兵種/
      // 兵書/被動類 heal(常駐急救型)使用, 使治療量不隨後續戰鬥中兵力增減而變動(見上方
      // HEAL_TROOP_C 常數註解); 建構時 troop 尚未受戰鬥影響, 此處快照即「開戰準備階段兵力」。
      this.healBase = this.troop * HEAL_TROOP_C;
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
      // 批36: 兵種營Lv10附贈戰法 attach —— 原文是「我軍隨機單體/群體」觸發(一整隊只發生一次),
      // 而非「隊上每個單位各自獨立擁有這個被動」。故只有 fight() 指定的單一「持有者」
      // (isCampHolder===true, 每隊隨機挑1人, 見 fight() 呼叫端)才實際 push 進 this.tactics;
      // 其餘同隊隊友仍受 campLv 的屬性%加成(下方amp段, 對每個Unit都算, 因原文那一支是「全隊
      // 造成傷害」的隊伍級加成, 與Lv10戰法是三合一裡各自獨立的兩支), 但不會各自重複攻得
      // Lv10戰法(避免3人隊「破軍/守禦」各自觸發3次的過量bug——已用鏡像對局實測驗證修正前後
      // 差異, 見sgz.py demo() 97-101號assert)。依「本隊實際兵種(ttype, 隊伍級)」查表
      // CAMP_TROOP_TACTIC, 命中且 TACTICS 已載入該名稱(器械營"負重"因 type:"none" 被
      // buildPool 過濾, 表中值為 null 或查無則不掛)才 push。必須在此處(cmdPassiveSrcs/
      // onHitTacs/onHitEffectTacs/onDealTacs 等衍生快取產生之前)插入, 因五戰法皆 type:"passive"
      // 會被那些快取掃描到(對比裝備proc戰法是charge型, 晚插入也不影響, 見下方_eqObjs迴圈)。
      // 淺拷貝加 _campBuilding 標記(供TRACE/除錯辨識, 不影響戰鬥邏輯分派)。
      if (this.campLv >= 10 && isCampHolder) {
        const campTacName = CAMP_TROOP_TACTIC[ttype];
        const campTac = campTacName && TACTICS[campTacName];
        if (campTac) this.tactics.push(Object.assign({}, campTac, { _campBuilding: true }));
      }
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
      this.onHitBs = _bsAll.filter(e => e.when && e.when.on && e.when.on !== "activeFired");
      // 禁近似令-批K: activeFiredBs(once_consumable/engine_wiring_gaps_misc族) —— 兵書效果
      // 走 self.bs 獨立管線, 過去只有 onHitBs(on:damaged/attacked方向)接線, 沒有對稱
      // activeFired(自身/我軍/敵軍成功發動主動戰法時)方向的消費端, 導致「每次成功發動主動
      // 戰法時...」措辭的兵書效果(如逆鱗)只能無聲被 onHitBs 的迴圈誤判(該迴圈只認
      // attacked/damaged, 從未真正檢查 on 值是否為 activeFired, 過去這類資料只是靜默無效)。
      // 從 onHitBs 中排除 activeFired 者, 另建此陣列, 於 activeFiredFor() 補上對稱消費端。
      this.activeFiredBs = _bsAll.filter(e => e.when && e.when.on === "activeFired");
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
      // 批G: 明確排除 on==="dealtDamage"(見下方新增的 onDealEq), 對稱戰法級
      // onHitEffectTacs/onDealEffectTacs 的白名單收斂慣例(過去truthy檢查會讓dealtDamage被
      // 誤當成damaged/attacked放行, 與onDealEq觸發路徑重複結算)。
      this.onHitEq = _eqAll.filter(e => e.when && (e.when.on === "attacked" || e.when.on === "damaged"));
      // 批G: 裝備效果級 e.when.on==="dealtDamage"(「自身造成傷害時/後」反應式, 對比onHitEq的
      // attacked/damaged是「自己受擊」視角, 這裡是「自己打人」視角)——過去裝備管線只有onHitEq
      // (受擊方向), 沒有對稱onDealTacs/onDealEffectTacs(造成傷害方向)的裝備級消費端, 導致
      // 「首回合首次造成傷害時附加一次額外兵刃傷害」(衝陣)這類裝備只能退化用首回合dot近似。
      // 掛在 dealtDamage() 對 src(施加傷害的一方)掃描, 與 onHitEq 完全對稱。
      this.onDealEq = _eqAll.filter(e => e.when && e.when.on === "dealtDamage");
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
      // 批36: 兵種營「每級+0.25%該兵種造成傷害」——與CAMP(全屬性flat)/Lv10附贈戰法(見上方
      // this.tactics attach)並列的三合一第三支, 走既有amp原語(與a.amp同慣例, src標記供TRACE/
      // 除錯辨識), campLv=0時不推入(向後相容, adds為空陣列不影響任何既有戰鬥數學)。
      if (this.campLv > 0) this.adds.push(["amp", this.campLv * CAMP_DMG_PER_LV, 9999, "兵種營"]);
      this.settle = null; this.guardian = null; this.guardShare = 0; this.guardDur = 0; this.guardNormalOnly = false;  // guardDur: 代承剩餘回合, 歸零清 guardian; guardNormalOnly: 只代承普攻傷害(如 援助), 戰法傷害不轉移
      this.stack = null; this.decay = null; this.swap = 0; this.counter = null; this.dmgShare = null;
      // 禁近似令-批K: regens(engine_wiring_gaps_misc族) —— 「每回合恢復一次兵力,持續N回合」
      // 的休整/regen類狀態獨立逐回合累計治療清單(對稱this.dots的傷害版, 見tick()消費端),
      // 取代乘敵不虞「引擎active heal不讀dur,實際只治1次=2倍低估, 改用單次折算216%近似」的
      // 缺口——改為真正逐回合各自結算108%, 不需要把2回合份折算成單次數值。每筆[healAmt, left]。
      this.regens = [];
      // 禁近似令-批K: preDmgHook —— 「傷害結算前攔截修正」統一掛鉤, 取代 pre_damage_intercept
      // 族長年的「hit()只有事後廣播、無法在troop-=dmg之前修改本次dmg」缺口(見engine_limitations.md
      // 該節/no_approx_inventory.json pre_damage_intercept族)。掛在 damage() 內部(src/dst 兩個
      // 方向皆消費同一個陣列欄位, 見 damage() 對應段落), 每筆 {hookKind, val, step, max, hits,
      // rate, dmgType, pct, delayRounds, reducePct, dur}:
      //   probVoid(攻擊方自己掛, 消費src.preDmgHooks): 每次造成傷害時rate機率本次傷害乘(1-val)
      //     (val=1即完全歸零, 挫銳「造成傷害時65%機率完全無法造成傷害」)。
      //   probMitig(防禦方自己掛, 消費dst.preDmgHooks): 每次受到傷害時rate機率本次傷害額外
      //     乘(1-val)(承天靖世「受到謀略傷害有X%機率可被統帥屬性降低」)。
      //   stepMitig(防禦方自己掛): 每次受到傷害必定按目前hits數算出(val+step×min(hits,max))
      //     的比例減傷, hits每次受擊+1(不歸零, 上限max次不再繼續遞減, 捨身救主「每次受到傷害後
      //     該減傷效果降低3%,上限降低30次」)。
      //   deferSettle(防禦方自己掛): 每次受到傷害時, pct比例的本次傷害移出, 按(1-reducePct)
      //     打折後平均攤到delayRounds回合(於tick()逐回合扣血), 而非當下立即扣(象兵「將傷害的
      //     25%-50%延後於3回合內逐步結算,並使結算傷害降低10%-20%」)。
      // dur: 掛鉤本身的有效期(回合數, tick()遞減歸零移除); deferSettle已排出的隊列不受dur影響,
      // 獨立於deferredDmg欄位持續攤還到底(即使觸發hook本身已到期, 已排入隊的錢仍要付完)。
      this.preDmgHooks = [];
      this.deferredDmg = [];  // deferSettle 排隊中的分期傷害: [{amt, left}], tick() 逐回合扣血遞減
      // 禁近似令-批K: preAttackHooks(engine_wiring_gaps_misc族) —— 「自身即將受到普通攻擊時」
      // 反應式清單(見 doNormalAttack() 消費端), 與 preDmgHooks(傷害已確定發生後的攔截/修正)
      // 是不同時機點: 這裡是「即將被打」這件事本身的觸發, 供雲聚影從/益其金鼓等使用。
      this.preAttackHooks = [];
      // 禁近似令-批K: armedConsume(once_consumable族) —— 「本次施放已武裝一份一次性追加觸發
      // 資格, 待我軍(含自己)下次成功發動主動戰法時消耗」的旗標(十二奇策), 見 k==="armConsume"
      // (施放端)/k==="strike"+e.ifArmed(消費端, 消費後歸null)。null=尚未武裝(向後相容既有
      // 全部未使用此機制的資料)。
      this.armedConsume = null;
      // 禁近似令-批K: guardStackN(counter_target_binding族) —— counterGuards觸發反擊成功時,
      // 「反擊執行者自己」或「被反擊的攻擊者」額外累積的疊層計數(古之惡來對攻擊者施加降傷/
      // 虎衛軍反擊者自身統率提升), 見 hit() 內 counterGuards 迴圈消費端。Map<counterGuards
      // 條目本身, 已疊層數>, 惰性建立, 掛在「反擊執行者」(gu)身上(與該筆counterGuards條目
      // 本身綁定, 不同條目各自獨立計數)。
      this.guardStackN = null;
      // 批A(11筆高嚴重重建): charge —— 「可消耗資源池」(死戰不退「蓄威」), 與既有 stack(傷害
      // 增益倍率, 疊層本身就是最終傷害的一部分)語意不同: charge.n 是「剩餘可消耗次數」, 消耗後
      // n 遞減, 不直接影響任何傷害倍率(是否觸發下一次攻擊的資源, 而非攻擊力大小本身)。
      // {n: 目前層數, max: 上限} | null(未曾獲得過任何層時為 null, 惰性建立)。
      this.charge = null;
      this.chargeConsumedThisRound = 0;  // 每回合觸發次數計數(對應原文「每回合最多觸發5次」), 每回合開始重置為0(見 fight() 主迴圈 tick 段)
      // 批28 B1: 守護式反擊(counter.guardFor) —— 「A受擊時, B代為反擊」的方向(如虎衛軍
      // 「我軍主將即將受到普攻時, 副將反擊」), 與 this.counter(持有者自己受擊自己反擊)方向
      // 相反, 掛在「被保護者」(如主將)身上一份清單, 每個元素是{unit(反擊執行者), coef, kind,
      // prob}, 見 applyEffects 的 guardFor 分支與 hit() 內的觸發判斷。與 guardian(傷害轉移
      // 代承)是不同機制, 可並存不衝突。
      this.counterGuards = [];
      // 批J(禁近似令-transfer轉移族): absorbGuards —— redirect.guardFor:"leader" 的登記清單,
      // 對稱 counterGuards(守護式反擊) 但語意是「代為承受這一次普攻傷害本身」而非「代為反擊」
      // (古之惡來「...隨後為我軍主將承擔此次普通攻擊」)。與常駐 guardian(redirect 一般模式,
      // %分擔every hit直到guardDur到期)不同: 這是「僅此一次(已被guardFor鎖定觸發的這次普攻)
      // +可配比例(e.share, 預設1.0=全額)」的單次轉移, 每個 absorbGuards 項每回合最多觸發1次
      // (hitFlags 節流, 同 counterGuards 慣例), 見 applyEffects 的 redirect.guardFor 分支與
      // hit() 內對應判斷。
      this.absorbGuards = [];
      this.tauntBy = null; this.tauntDur = 0;      // 嘲諷: 被嘲諷時強制普攻/單體戰法指向 tauntBy, 剩餘回合
      this.shield = null;                          // 護盾: {amt, dur} 吸收固定量傷害, 先於兵力扣減
      this.block = [];                              // 批22: 次數型格擋(抵禦/警戒同族) —— [{val, n, src}], 消耗順序見 hit(); val=1.0全擋/0.x部分減傷, n=剩餘次數
      this.ammo = {};                               // 批52g: 彈藥計數(高櫓連營) name->剩餘
      this.captured = 0;                            // 批52j: 捕獲(暗箭難防)不可淨化
      this.dodgeProb = 0; this.dodgeDur = 0;        // 規避: 機率完全迴避一次傷害
      this.dodgeDmgType = null;                     // 批G: 規避限定的傷害類型(phys/intel), null=不分類型(向後相容既有全域規避)
      this.surehitDur = 0;                          // 必中: 無視對方 dodge
      this.healblock = 0;                           // 批8: 禁療(healblock) 剩餘回合, >0 時 heal 效果對其無效
      this.huchen = null;                           // 批52d: 虎嗔(將門虎女負面狀態, 可被 dispel debuffs 清除)
      this.whenFired = new Set();                   // 條件觸發(when.rounds/from/until) 已套用效果的戰法(一次性), 依戰法物件去重; 批8: delayedEq(裝備效果級when)共用同一個 Set(效果物件本身去重, 不與戰法物件撞)
      this.scaleLock = null;                        // 批35 B: 「準備階段鎖定」的 scale 縮放值快取, Map<效果物件, scaleOf結果>, 惰性建立(見 lockedScaleOf)
      // 批42: exploitLayers —— 「持有者對本單位(受害目標)累積的疊層負面buff」計數器, Map<效果
      // 物件, 已疊層數>, 掛在**目標**(受害者)身上而非持有者(與stack/scaleLock等「掛在自己
      // 身上」的既有欄位方向相反, 因為疊層語意是「敵人身上累積的破綻層數」, 不是「自己累積的
      // 增傷層數」)。惰性建立。見傲睨王侯(k:"stat"+e.stackKey/e.maxStacks): 敵軍目標受普攻時
      // 觸發1層(該目標降3%可疊), 用「效果物件」當鍵天然對應「同一張卡的疊層」不會跟其他戰法的
      // stat效果撞鍵; 掛在目標身上則天然對應「疊層只對這個特定目標累積, 不同敵人各自獨立計數」
      // (foesOf(holder)全體共用同一個效果物件, 但各自的 exploitLayers 是自己 Unit 實例上的
      // 獨立Map, 天然不互相干擾)。
      this.exploitLayers = null;
      // 禁近似令-批K: ampLayersById(dynamic_coef_from_counter族) —— k:"amp"+e.stackKey+
      // e.stackId 的字串鍵索引版本(見該分支詳細註解), 惰性建立(null直到第一次疊層才建物件)。
      this.ampLayersById = null;
      this.exploitCapped = null;                    // 批42: 同上, Set<效果物件> —— 記錄該目標「本效果已達maxStacks上限並觸發過onMaxStacks」, 防止之後每次疊層(已封頂不再增加)重複觸發onMaxStacks次數效果(如傲睨王侯「單體破綻全觸發→虛弱+受傷提高」只應在剛好達到15/5層那一次觸發, 非之後同目標若又被攻擊而重複觸發)。
      // 批42: exploitGlobal —— 「持有者(施放者/caster)」視角的跨目標累計觸發次數計數器,
      // Map<效果物件, {n, fired}>, 掛在持有者(而非目標)身上, 對應原文「場上所有破綻觸發後」
      // (15個破綻分布全體敵軍, 不論落在哪個目標身上, 全數觸發完才算數, 與exploitLayers的
      // 「單一目標各自累積到maxStacks」是兩個不同層級的計數, 前者跨目標加總、後者單目標各自
      // 封頂)。fired旗標防止15層全觸發後, 之後同陣營若還有普攻事件持續進來時重複觸發
      // globalEffects(全域效果只應觸發一次, 對應「觸發後」的一次性語意, 非常駐狀態)。
      this.exploitGlobal = null;
      this.healRoundsFired = null;                  // 批15: heal 效果 e.when.rounds(明確列出的特定回合)的「每回合各觸發一次」去重, Map<效果物件, Set<已觸發回合數>>, 惰性建立(見 applyEffects 的 heal 分支)
      this.tacCd = {};                              // 批52: 戰法冷卻 {nameZh: 剩餘回合}
      this.hitFlags = new Set();                    // 反應式觸發(when.on) 本回合已觸發的戰法, 每回合重置(防無限鏈)
      // 批31 A 修復: 過去只檢查 t.when.on 是否為真(truthy), 未限定具體事件值, onHit() 內部
      // 迴圈也只用 t0.when.on==="attacked" 排除普攻限定的不符情形, 對其餘任何 on 值(包含
      // 批27新增的"dealtDamage"/本批"activeFired")一概放行當成"damaged"處理——潛伏bug,
      // 全庫過去只有 attacked/damaged 兩種 t.when.on 值從未真正暴露, 本批新增 activeFired
      // 後士爭先赴首次踩中(除正確的「自身發動主動戰法觸發」外, 還被 onHit() 誤判成「受擊
      // 觸發」額外多發動一次)。收斂為明確白名單。
      this.onHitTacs = this.tactics.filter(t => (t.type === "passive" || t.type === "command") && t.when && (t.when.on === "attacked" || t.when.on === "damaged"));  // 預篩: 絕大多數單位為空, hit 熱路徑 O(0)
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
      // 批31 A 修復: 同上(onHitTacs)——過去用 truthy 檢查, 導致帶 e.when.on:"dealtDamage"
      // (批27)的效果(深謀遠慮/白衣渡江/非攻制勝)被誤收進 onHitEffectTacs, 在 onHit() 的效果級
      // 迴圈裡又額外多觸發一次, 與正確的 onDealEffectTacs 觸發路徑重複結算。收斂為明確白名單。
      this.onHitEffectTacs = this.tactics.filter(t => !t.when && (t.type === "passive" || t.type === "command" || t.type === "active") && (t.effects || []).some(e => e.when && (e.when.on === "attacked" || e.when.on === "damaged")));
      // 批27 A: on:"dealtDamage" —— 「自身造成傷害時/後」反應式掛鉤(對比 onHitTacs 的
      // attacked/damaged 是「自己受擊」視角, 這裡是「自己打人」視角, 如白衣渡江「造成兵刃
      // 傷害時25%→50%機率使敵軍單體繳械」)。掛在 hit() 傷害結算後對 src(施加傷害的一方)
      // 掃描, 與 onHitTacs/onHitEffectTacs 完全對稱(戰法級 vs 效果級 兩種顆粒度)。
      // dmgType(選填, "phys"/"intel"): 區分「造成兵刃傷害時」vs「造成謀略傷害時」兩種不同
      // 觸發條件(白衣渡江 disarm 段只在兵刃傷害後觸發, silence 段只在謀略傷害後觸發), 沿用
      // amp/mitig 既有 dmgType 欄位命名慣例, 無此欄位視為兩種傷害類型皆可觸發(向後相容)。
      this.onDealTacs = this.tactics.filter(t => (t.type === "passive" || t.type === "command") && t.when && t.when.on === "dealtDamage");
      this.onDealEffectTacs = this.tactics.filter(t => !t.when && (t.type === "passive" || t.type === "command" || t.type === "active") && (t.effects || []).some(e => e.when && e.when.on === "dealtDamage"));
      // 批31 A: on:"activeFired" —— 「自身成功發動主動(或突擊)戰法時/後」反應式掛鉤(對比
      // onDealTacs 的「造成傷害」視角, 這裡是「戰法本身成功擲骰命中fire」視角, 不要求真的造成
      // 傷害, 如士爭先赴「成功發動自帶主動戰法前，50%機率對敵軍2人造成兵刃傷害」——現行版本把
      // 這條獨立成一個常駐coef+rate的passive戰法, 與「是否真的有主動戰法成功發動」完全脫鉤,
      // 屬v14盲測抓到的「條件觸發簡化為無條件」同族缺口)。掛在 fight() 主迴圈 active/charge
      // 型戰法 fire===true 判定通過後, 對施放者 u 自身掃描其 activeFiredTacs(戰法級)/
      // activeFiredEffectTacs(效果級), 與 onDealTacs/onDealEffectTacs 完全對稱(戰法級 vs
      // 效果級 兩種顆粒度), 只是事件觸發點不同(自身戰法命中 vs 自身造成傷害)。when.timing
      // (選填, "before"/"after"): 統一在 fire 判定通過、實際套用觸發戰法效果之前廣播(貼近
      // before 語意, after 措辭的戰法一律視同無差別, 見 engine_limitations.md 新增節)。
      this.activeFiredTacs = this.tactics.filter(t => (t.type === "passive" || t.type === "command") && t.when && t.when.on === "activeFired");
      this.activeFiredEffectTacs = this.tactics.filter(t => !t.when && (t.type === "passive" || t.type === "command" || t.type === "active") && (t.effects || []).some(e => e.when && e.when.on === "activeFired"));
      // 批43 C: on:"healed" —— 「(自身/我軍/敵軍)受到治療時」反應式掛鉤, 與 onHitTacs(受擊
      // 視角)/onDealTacs(造成傷害視角)/activeFiredTacs(自身發動視角) 完全對稱(戰法級 vs 效果級
      // 兩種顆粒度), 只是事件觸發點改在 heal 效果實際結算(applyEffects 的 k==="heal" 分支,
      // hurt.troop 已扣減傷兵池/回補之後)之後, 對**受治療的那個單位(hurt, 事件的"dst")**
      // 掃描 —— 對稱 onHit 以受擊者為錨點廣播(而非 dealtDamage 以施加者為錨點), 因為本族
      // 全庫候選(權僭九鼎「自身受到治療時+5統率智力, 可疊加」「敵軍受治療時偷取12%」)兩句皆是
      // 「以接受治療的單位」為敘述主詞的receiver-framed語意, 與onHit(以受擊者為主詞)同構。
      // 「自身造成治療效果時」(caster-framed, 如義心昭烈)是相反方向(以施法者/健者為主詞,
      // 對稱dealtDamage), 本次不建caster-framed的第二個事件方向(見engine_limitations.md新增
      // 節說明, 該族維持既有停損近似, 非本次healed事件涵蓋範圍)。批31precedent: 新事件類型
      // 不強制一次補齊裝備/兵書層級掛鉤(activeFired當年也未補onHitEq/onHitBs同款), 此處
      // onHealEffectTacs 僅涵蓋戰法效果層級, 現無資料需要裝備/兵書層級的heal反應式。
      this.onHealTacs = this.tactics.filter(t => (t.type === "passive" || t.type === "command") && t.when && t.when.on === "healed");
      this.onHealEffectTacs = this.tactics.filter(t => !t.when && (t.type === "passive" || t.type === "command" || t.type === "active") && (t.effects || []).some(e => e.when && e.when.on === "healed"));
      // 批52h: on:"controlled" —— 友軍被施加控制時反彈(機鑑先識)
      this.onCtrlEffectTacs = this.tactics.filter(t => !t.when && (t.type === "passive" || t.type === "command" || t.type === "active") && (t.effects || []).some(e => e.when && e.when.on === "controlled"));
      // 禁近似令-批L: on:"dmgThreshold"(自身累計受傷達門檻)/on:"ctrlImmune"(自身免疫控制事件)
      // —— 一身是膽「每次免疫控制狀態後或每次累計受最大兵力7%傷害後...」需要的兩個新反應式
      // 事件, 純自身視角(不做ally/enemy跨隊廣播, 現無資料需要, 見fireSelfReactive註解), 故
      // 用單一預篩陣列(selfReactEffectTacs)涵蓋兩者, 由呼叫端(fireSelfReactive的onName參數)
      // 決定要精確比對哪一個事件名。onValues(e)把e.when.on正規化成陣列(單一效果可同時掛兩個
      // 事件名, 共用同一份stackKey疊層計數, 見k==="critUp"+e.stackKey消費端, 對稱既有
      // e.eitherK/e.ifLeaderIs的字串或陣列慣例)。
      this.selfReactEffectTacs = this.tactics.filter(t => !t.when && (t.type === "passive" || t.type === "command" || t.type === "active") && (t.effects || []).some(e => onValues(e).some(v => v === "dmgThreshold" || v === "ctrlImmune")));
      this.lockedTargets = new Map();               // 批12 ModeG: lockTarget:true 戰法的鎖定目標, 鍵=戰法物件本身(同一戰法物件跨回合重用同一 Map)
      // 批16: 原語擴充包 —— 新增狀態欄位(現有資料無新欄位, 皆維持0/null預設值, 行為零變化)
      this.atkCount = new Map();                     // everyN: 自身普攻次數計數器, 鍵=戰法物件本身(同一戰法物件跨回合重用同一 Map)
      this.immune = [];                              // immuneTo: [type, dur] 陣列(單項控制免疫, 對比 insight 全免); O(小陣列)線性掃描, 資料上每單位條數<5
      // 禁近似令-批L: dmgAccum —— 自身累計受傷計數器(全戰鬥單調遞增, 不因治療/傷兵池消耗
      // 而回退, 與this.wounded[可被治療消耗的池]是不同語意的兩個獨立計數)。一身是膽「每次
      // 累計受最大兵力7%傷害後」需要跨事件持續累加再偵測門檻跨越次數, 見bumpDmgAccum()。
      this.dmgAccum = 0;
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
      // 禁近似令-批K: this.stack.statField/statPerVal(dynamic_coef_from_counter族) —— 對稱
      // amp() 讀 this.stack.per×this.stack.n 的既有寫法, 供 k:"stat"+e.fromStack 註記的
      // 「stat屬性隨同一枚stack計數器動態成長」(弓腰姬, 見其註冊端註解), 即時讀取當下層數,
      // 天然跟隨stack.n逐回合變化同步, 不需要每回合重新pushStatAdd。
      if (this.stack && this.stack.statField === stat) v += (this.stack.statPerVal || 0) * this.stack.n;
      return v;
    }
    // 批24 D2: dmgType(可選) —— 只加總「該條目未宣告 dmgType, 或宣告的 dmgType 與呼叫端指定
    // 的 dmgType 相符」的項目, 供 amp/mitig 依「兵刃/謀略」傷害類型過濾(見 damage() 呼叫端)。
    // dmgType 省略(undefined)時完全維持原行為(不分類型全部加總), 向後相容全庫既有未帶
    // dmgType 的 amp/mitig 資料。
    // 批28 B3: isNormal(可選) —— 只加總「該條目未宣告 normalOnly, 或宣告 normalOnly 且本次
    // isNormal 為 true」的項目, 供 amp 表達「僅普攻傷害提升」(見至柔動剛)。未傳(undefined,
    // 如dot/counter/settle等非普攻傷害路徑)時安全側不套用 normalOnly 加成。
    // 批31 A: isActive(可選, 對稱於 isNormal) —— 只加總「該條目未宣告 activeOnly, 或宣告
    // activeOnly 且本次 isActive 為 true」的項目, 供 amp 表達「僅主動戰法傷害提升」(如
    // 士爭先赴「提高自帶主動戰法傷害」)。未傳(undefined)時安全側不套用 activeOnly 加成。
    // 批40 B: isCharge(可選, 尾端新增, 向後相容) —— 對稱 isActive, 供 amp 表達「僅突擊戰法
    // 傷害提升/降低」(一鼓作氣/藏刀「突擊戰法造成傷害提升/降低」)。批31 A 原本把「突擊」
    // 傷害也標記 isActive=true(見 fight() 主迴圈突擊擲骰呼叫點), 誤將「主動戰法」與「突擊
    // 戰法」兩個game機制上互斥的分類(士爭先赴明確是「自帶主動戰法」, 不含突擊; 一鼓作氣/
    // 藏刀明確只講「突擊戰法」, 不含主動)混為一談——本批修正呼叫點改傳 isCharge, isActive
    // 維持只在真正 t.type==="active" 時為 true(見下方呼叫點修正)。
    addbonus(kind, dmgType, isNormal, isActive, isCharge, dotStatus) {
      let s = 0;
      for (const a of this.adds) {
        if (a[0] !== kind || this.suppressed(a[3])) continue;
        const f = a[4];
        if (dmgType && f && f.dmgType && f.dmgType !== dmgType) continue;
        if (f && f.normalOnly && isNormal !== true) continue;
        if (f && f.activeOnly && isActive !== true) continue;
        if (f && f.chargeOnly && isCharge !== true) continue;
        // 禁近似令-批L: dmgFromStatus(僅amp) —— 帶此限定的條目只在本次傷害是「dot結算且其
        // 具名狀態在清單內」(dotStatus命中f.dmgFromStatus)時才計入, 一般傷害路徑(dotStatus
        // 未傳/undefined)一律跳過這類條目, 見才辯機捷。
        if (f && f.dmgFromStatus && !(dotStatus && f.dmgFromStatus.includes(dotStatus))) continue;
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
    // 同來源(戰法名)同種效果預設刷新而非疊加。批52 maxStack: 允許同 src 追加至多 N 層。
    // src=null/undefined(兵書/裝備/緣分, 開戰只套一次) 不做去重, 維持原行為。
    pushAdd(kind, val, dur, src, flags, maxStack) {
      if (src) {
        const same = this.adds.filter(a => a[0] === kind && a[3] === src);
        if (maxStack) {
          if (same.length >= maxStack) { for (const a of same) a[2] = Math.max(a[2], dur); return; }
        } else this.adds = this.adds.filter(a => !(a[0] === kind && a[3] === src));
      }
      this.adds.push([kind, val, dur, src, flags]);
    }
    pushMod(stat, mult, dur, src, flags, maxStack) {
      if (src) {
        const same = this.mods.filter(m => m[0] === stat && m[3] === src);
        if (maxStack) {
          if (same.length >= maxStack) { for (const m of same) m[2] = Math.max(m[2], dur); return; }
        } else this.mods = this.mods.filter(m => !(m[0] === stat && m[3] === src));
      }
      this.mods.push([stat, mult, dur, src, flags]);
    }
    pushStatAdd(stat, add, dur, src, flags, maxStack) {
      if (src) {
        const same = this.statAdds.filter(a => a[0] === stat && a[3] === src);
        if (maxStack) {
          if (same.length >= maxStack) { for (const a of same) a[2] = Math.max(a[2], dur); return; }
        } else this.statAdds = this.statAdds.filter(a => !(a[0] === stat && a[3] === src));
      }
      this.statAdds.push([stat, add, dur, src, flags]);
    }
    // 批22: block(次數型格擋, 抵禦/警戒同族) —— 與 shield/mitig 語意不同: 不是持續減傷/固定量
    // 吸收池, 而是「剩餘次數」計次器, 每次受擊消耗1次(而非按傷害量扣減), val=1.0時完全格擋
    // 該次傷害、val=0.x時該次傷害打折(如警戒 -75.35%≈val:0.7535)。同源(同 src)再次施加時
    // 疊加次數(而非同 pushAdd/pushMod 慣例的「同源刷新覆蓋」), 貼合原文「抵禦(N次)」「目前
    // 抵禦總次數為N」的疊次語意(見 docs/data/calibration_anchors.json battle_report_round_20260703
    // 戰報實測: 「抵禦(1)」用一次消一層, 「警戒(1)」-75.35%減傷/次用後消層)。
    // 批G: dmgType(可選, 尾端新增, 向後相容既有全部呼叫點)—— 對稱 amp/mitig 既有的 dmgType
    // 過濾慣例(批24 D2), 限定此格擋只對該類型(phys/intel)傷害生效, 省略時維持原行為(不分
    // 類型, 任何傷害皆可消耗, 如「抵禦」「警戒」既有全域格擋)。榮光「受到謀略傷害時, 有4%
    // 機率完全免疫此次傷害」需要限定只對 intel 傷害生效, 過去 block 無此過濾維度。
    pushBlock(val, n, src, dmgType) {
      const existed = src && this.block.find(b => b.src === src && Math.abs(b.val - val) < 1e-9 && b.dmgType === dmgType);
      if (existed) existed.n += n; else this.block.push({ val, n, src, dmgType });
    }
    // 消耗一次格擋(若有, 且該格擋層未限定類型或類型與本次傷害相符): 從陣列中第一筆符合條件者
    // (先加的先消耗, 貼合戰報「總次數」單一計數語意)扣1次, n<=0時整筆移除。回傳消耗到的 val
    // (0=無格擋可消耗, 呼叫端不應觸發)。
    // 批G: dmgType(可選)—— 本次傷害類型(phys/intel), 只消耗 b.dmgType 為 undefined(不分類型,
    // 既有全域格擋)或與 dmgType 相符的格擋層, 類型不符的格擋層略過不消耗。向後相容: 全庫既有
    // 格擋資料皆未帶 dmgType, 行為完全不變。
    consumeBlock(dmgType) {
      for (let i = 0; i < this.block.length; i++) {
        const b = this.block[i];
        if (b.dmgType != null && b.dmgType !== dmgType) continue;
        b.n -= 1;
        const val = b.val;
        if (b.n <= 0) this.block.splice(i, 1);
        return val;
      }
      return 0;
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
    // 標記的加成(僅普攻傷害生效, 見至柔動剛)。批31 A: isActive(可選) —— 過濾 activeOnly
    // 標記的加成(僅主動戰法傷害生效, 見士爭先赴)。批40 B: isCharge(可選) —— 過濾 chargeOnly
    // 標記的加成(僅突擊戰法傷害生效, 見一鼓作氣/藏刀)。
    amp(dmgType, isNormal, isActive, isCharge, dotStatus) {
      let a = this.addbonus("amp", dmgType, isNormal, isActive, isCharge, dotStatus);
      if (this.stack) a += this.stack.per * this.stack.n;
      if (this.decay) a += this.decay.v0 * this.decay.left / this.decay.total;
      return a;
    }
    tick() {
      for (const d of this.dots) { this.troop -= d[0]; this.wounded += d[0] * woundedRate(CUR_R); fireSelfReactive(this, "dmgThreshold", bumpDmgAccum(this, d[0])); }  // 批18: dot 掉血同樣按當前回合轉化率計入傷兵池; 禁近似令-批L: 一身是膽累積傷害門檻
      this.dots = this.dots.filter(d => --d[1] > 0);
      // 禁近似令-批K: regens(engine_wiring_gaps_misc族) —— 對稱上方dots掉血, 逐回合按登記
      // 金額治療(受傷兵池/START_TROOP上限雙重夾住, 沿用heal效果既有相同clamp慣例), 到期
      // 遞減移除(對稱dots的--d[1]>0慣例)。
      if (this.regens.length) {
        for (const rg of this.regens) {
          const actual = Math.max(0, Math.min(rg[0], this.wounded, START_TROOP - this.troop));
          this.troop += actual; this.wounded -= actual;
        }
        this.regens = this.regens.filter(rg => --rg[1] > 0);
      }
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
      this.captured = Math.max(0, this.captured - 1);    // 批52j: 捕獲自然到期
      this.swap = Math.max(0, this.swap - 1);
      // 批52d: 虎嗔到期自然結算
      if (this.huchen) {
        this.huchen.left -= 1;
        if (this.huchen.left <= 0) settleHuchen(this, false);
      }
      if (this.decay && --this.decay.left <= 0) this.decay = null;
      // 禁近似令-批K: preDmgHook 到期清除(見 Unit 建構式註解) + deferredDmg 逐回合攤還扣血
      // (deferSettle 排出的分期傷害獨立於觸發它的 hook 本身是否仍存活, 已排入隊的錢仍要付完)。
      if (this.preDmgHooks.length) this.preDmgHooks = this.preDmgHooks.filter(h => --h.dur > 0);
      if (this.preAttackHooks.length) this.preAttackHooks = this.preAttackHooks.filter(h => --h.dur > 0);
      if (this.deferredDmg.length) {
        let paid = 0;
        this.deferredDmg = this.deferredDmg.filter(q => {
          this.troop -= q.amt; this.wounded += q.amt * woundedRate(CUR_R); paid += q.amt;
          fireSelfReactive(this, "dmgThreshold", bumpDmgAccum(this, q.amt));  // 禁近似令-批L
          return --q.left > 0;
        });
        if (TRACE && paid >= 1) lg(`　▸ ${this.nm} 延後傷害分期結算 -${Math.round(paid)}`);
      }
      this.tauntDur = Math.max(0, this.tauntDur - 1);
      if (this.tauntDur <= 0) this.tauntBy = null;
      if (this.guardDur) { this.guardDur = Math.max(0, this.guardDur - 1); if (this.guardDur <= 0) { this.guardian = null; this.guardShare = 0; this.guardNormalOnly = false; } }  // 代承到期: 清 guardian(如 援助 首回合援護 dur:1)
      this.dodgeDur = Math.max(0, this.dodgeDur - 1);
      if (this.dodgeDur <= 0) { this.dodgeProb = 0; this.dodgeDmgType = null; }  // 批G: 到期一併清除類型限定, 避免下次無條件dodge誤沿用舊的殘留類型過濾
      this.surehitDur = Math.max(0, this.surehitDur - 1);
      if (this.shield && --this.shield.dur <= 0) this.shield = null;
      if (this.counter && --this.counter.dur <= 0) this.counter = null;  // 批23 A2: 反擊到期清除(過去 dur 幽靈欄位從不遞減, 帶時限的反擊變永久)
      if (this.dmgShare && --this.dmgShare.dur <= 0) this.dmgShare = null;  // 禁近似令-批K: dmgShare 到期清除(對稱counter既有慣例)
      this.hitFlags.clear();                           // 受擊觸發(when.on) 每回合各戰法重置一次觸發額度
      if (this.immune.length) this.immune = this.immune.filter(a => --a[1] > 0);  // 批16: immuneTo 逐回合遞減
      this.fakeReportDur = Math.max(0, this.fakeReportDur - 1);  // 批16: 偽報 逐回合遞減
      // 批52: 戰法冷卻逐回合遞減
      if (this.tacCd) {
        const next = {};
        for (const k of Object.keys(this.tacCd)) { const v = this.tacCd[k] - 1; if (v > 0) next[k] = v; }
        this.tacCd = next;
      }
    }
  }

  // 傷害公式旋鈕(批3 重塑): 社群拆解(知乎菜頭50級傷害模型 + B站櫻謀詭計錨點), 用實測錨點反解常數。
  // 錨點(兵10000/coef1.0/士氣100/無增減傷, moraleMult(100)=1.0 已併入取樣, 取隨機帶中值1.0):
  //   錨1 屬性差0   → 實測 ≈476 傷害 ⇒ DMG_A = 476/sqrt(10000) = 4.76
  //   錨2 屬性差200 → 實測 ≈764 傷害 ⇒ DMG_B = (764-476)/200 = 1.44
  //   錨3 屬性差大負值(保底) → 實測 ≈90  傷害 ⇒ DMG_FLOOR = 90/sqrt(10000) = 0.9
  // 之後有更多實測數據(不同兵力/等級)可再校準, 目前僅50級單一等級係數樣本, 折入常數中。
  const DMG_A = 4.76, DMG_B = 1.44, DMG_FLOOR = 0.9;
  function damage(src, dst, coef, kind, srcTroop, isNormal, isActive, isCharge, forcePierce, dotStatus) {
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
    // 攻擊傷害35%」)。批31 A: isActive(可選, 對稱於 isNormal) —— 傳入本次傷害是否為主動
    // 戰法所致, 供 amp() 過濾 activeOnly 標記的加成(僅主動戰法傷害生效, 見士爭先赴)。批40 B:
    // isCharge(可選, 對稱isActive) —— 傳入本次傷害是否為突擊戰法所致, 供 amp() 過濾
    // chargeOnly 標記的加成(僅突擊戰法傷害生效/降低, 見一鼓作氣/藏刀)。
    // 批52j: 捕獲狀態無法造成傷害
    if (src.captured > 0) return 0;
    const totalAmp = src.amp(kind, isNormal, isActive, isCharge, dotStatus);  // 禁近似令-批L: dotStatus(可選, 尾端新增, 向後相容既有全部呼叫點)—— 供k==="dot"分支傳入該次dot的具名狀態(才辯機捷 e.dmgFromStatus 過濾用)
    base *= totalAmp <= -1 ? 0 : 1 + Math.max(-0.9, totalAmp);
    // 禁近似令-批K: forcePierce(可選, 尾端新增, 向後相容既有全部呼叫點) —— dot 效果級 e.pierce:true
    // 專用(見 applyEffects k==="dot"分支), 強制本次結算完全無視 dst 的 mitig(無論 src 的
    // pierce 累加值多少), 取代「無視防禦」與「無視統率智力(atk-def公式本身)」被迫混用src.
    // addbonus("pierce")(會連帶影響caster所有其他傷害來源, 而非只影響這一個dot段)的舊近似
    // (獅子奮迅「叛逃狀態...無視防禦」, engine_wiring_gaps_misc族)。
    const mit = forcePierce ? 0 : dst.addbonus("mitig", kind, isNormal) * (1 - Math.min(1, src.addbonus("pierce")));
    base *= Math.max(0.1, 1 - mit);
    // 禁近似令-批K: preDmgHook —— 「傷害結算前攔截修正」(pre_damage_intercept族, 見 Unit
    // 建構式 this.preDmgHooks 註解)。攻擊方(src)自己掛的 probVoid 與防禦方(dst)自己掛的
    // probMitig/stepMitig/deferSettle 皆在此處(amp/mitig/crit皆已算完之後, 隨機帶之前)消費,
    // 與 crit 同屬「這一下攻擊有沒有命中某個離散事件」的獨立判定層, 不受 amp -90%封頂/crit
    // 隨機帶影響, 也不影響它們。
    if (src.preDmgHooks && src.preDmgHooks.length) {
      for (const h of src.preDmgHooks) {
        if (h.dmgType && h.dmgType !== kind) continue;
        if (h.hookKind === "probVoid" && rnd() < (h.rate || 0)) {
          base *= Math.max(0, 1 - (h.val ?? 1));
          if (TRACE) lg(`　▸ ${src.nm} 攻擊結算前被攔截, 本次傷害降低${Math.round((h.val ?? 1) * 100)}%`);
        }
      }
    }
    if (dst.preDmgHooks && dst.preDmgHooks.length) {
      for (const h of dst.preDmgHooks) {
        if (h.dmgType && h.dmgType !== kind) continue;
        if (h.hookKind === "probMitig") {
          if (rnd() < (h.rate || 0)) {
            base *= Math.max(0, 1 - (h.val || 0));
            if (TRACE) lg(`　▸ ${dst.nm} 受到傷害結算前被攔截降低${Math.round((h.val || 0) * 100)}%`);
          }
        } else if (h.hookKind === "stepMitig") {
          const effHits = Math.min(h.hits, h.max ?? 30);
          const cur = Math.max(0, (h.val ?? 0) + (h.step ?? 0) * effHits);
          if (cur > 0) base *= Math.max(0, 1 - cur);
          h.hits += 1;
        } else if (h.hookKind === "deferSettle") {
          const deferAmt = base * (h.pct || 0);
          base -= deferAmt;
          const rounds = h.delayRounds || 3;
          dst.deferredDmg = dst.deferredDmg || [];
          dst.deferredDmg.push({ amt: (deferAmt * (1 - (h.reducePct || 0))) / rounds, left: rounds });
          if (TRACE) lg(`　▸ ${dst.nm} ${Math.round(deferAmt)}傷害延後結算(降低${Math.round((h.reducePct || 0) * 100)}%後分${rounds}回合)`);
        }
      }
    }
    // 批H: 會心(兵刃暴擊)/奇謀(謀略暴擊)真擲骰層 —— 禁近似令下取代全庫14筆「crit-ev」期望值
    // 折算(見 no_approx_inventory.json crit_system_primitive族/engine_limitations.md本節)。
    // 機制: 每次造成傷害時, 先擲一次crit判定, rate=src此刻所有「會心/奇謀機率」來源加總
    // (k==="critUp", 依dmgType分流: dmgType="phys"=會心/兵刃暴擊, dmgType="intel"=奇謀/謀略
    // 暴擊, 與amp/mitig既有dmgType路由慣例完全一致, 呼叫端傳入的kind本就已是phys/intel);
    // 命中則本次傷害額外乘上(1+critMult), critMult=1.0(官方戰報實測基準「觸發會心,
    // 兵刃傷害提升100.00%」, 見calibration_anchors.json crit節)+critDmgUp累加(k==="critDmgUp",
    // 「會心傷害/奇謀傷害+X%」幅度修飾語, 如華服/長慮, 同dmgType路由, 未命中crit則此層不
    // 生效也不消費critDmgUp)。與amp是「機率來源(critUp)」與「幅度來源(critDmgUp)」分離、
    // 但透過同一個離散事件(擲骰命中與否)耦合的雙層設計, 不同於amp的單一靜態疊加值。
    // 乘法層疊順序: 疊在amp/mitig之後(倍率獨立於±4%隨機帶之前) —— crit是「這一下攻擊有沒有
    // 命中會心」的二元判定, 不應被視為amp累加的一部分(amp封頂-90%/總和<=-1虛弱語意不應牽動
    // crit判定), 也不應被隨機帶±4%「稀釋」掉critRate本身的擲骰獨立性(±4%是每次攻擊都有的
    // 基礎浮動, crit是額外的、獨立擲一次的二元事件, 兩者互不影響, 詳見engine_limitations.md
    // 本節「與amp/mitig/±4%隨機帶的結算順序」)。
    const critRate = src.addbonus("critUp", kind, isNormal, isActive, isCharge);
    if (critRate > 0 && rnd() < critRate) {
      const critBonus = 1.0 + src.addbonus("critDmgUp", kind, isNormal, isActive, isCharge);
      base *= (1 + critBonus);
      if (TRACE) lg(`　▸ ${src.nm} 觸發${kind === "phys" ? "會心" : "奇謀"}, ${kind === "phys" ? "兵刃" : "謀略"}傷害提升${(critBonus * 100).toFixed(2)}%`);
    }
    // 傷害不浮動(user權威規則2026-07-11): 同條件傷害為定值, 移除舊±4%隨機帶
    // (早期存疑保留, 現經user確認遊戲傷害數字不浮動)。會心仍是離散擲骰(上方), 非連續浮動。
    return Math.max(0, base);
  }
  function hit(src, dst, coef, kind, isNormal, onEvent, onDeal, isActive, isCharge) {  // 批31 A: isActive(可選, 尾端新增, 向後相容既有全部呼叫點)—— 傳入本次傷害是否為主動戰法所致; 批40 B: isCharge(可選, 對稱isActive)—— 傳入本次傷害是否為突擊戰法所致
    // 禁近似令-批K: wasAlive(engine_wiring_gaps_misc族, on-kill事件) —— 記錄本次命中前dst是否
    // 存活, 供下方「本次命中後dst.troop<=0」的擊殺判定精準抓「這一下才是致命一擊」(而非對已死
    // 單位重複觸發), 見虎痴 pierce.onKill 消費端。
    const wasAlive = dst.troop > 0;
    if (!src.surehitDur && dst.dodgeDur && (dst.dodgeDmgType == null || dst.dodgeDmgType === kind) && rnd() < dst.dodgeProb) {  // 規避: 完全迴避一次傷害(必中無視); 批G: dodgeDmgType限定只對該類型(phys/intel)生效, null=向後相容不分類型
      if (TRACE) lg(`　→ ${dst.nm} 規避了攻擊`);
      if (onEvent) onEvent(dst, src, isNormal, 0, kind);  // 批39 C: 補傳kind(本次傷害類型), 供onHit()對稱dealtDamage的e.when.dmgType過濾(見下方onEvent呼叫端與onHit定義)
      return;
    }
    let dmg = damage(src, dst, coef, kind, undefined, isNormal, isActive, isCharge);  // 批28 B3/批31 A/批40 B: 傳入isNormal/isActive/isCharge供amp()過濾normalOnly/activeOnly/chargeOnly標記的加成
    // 批22: block(次數型格擋, 抵禦/警戒同族) —— 判定順序 dodge→block→shield→傷害(見紅線指示)。
    // 每次受擊消耗1次(不論本次傷害量多寡), val=1.0(如「抵禦」)完全格擋歸零本次傷害,
    // val=0.x(如「警戒」-75.35%)按比例打折。用光即從陣列移除, 供 TRACE 顯示「剩餘N層」。
    // 批35 D: BLOCK_CONSUME_THRESHOLD —— grok查證機鑑先識原文「受到的傷害超過自身可攜帶
    // 最大兵力的6%時(最低100兵力)」才消耗1次警戒並減傷(見 engine_limitations.md 第30節/
    // tactic_corrections.json「機鑑先識」)。過去版本無門檻, 每次受擊必消耗, 高估警戒觸發
    // 頻率(低傷害的普攻/持續傷害也會誤耗掉寶貴的警戒層數)。用本次「格擋前原始傷害」dmg
    // 與 BLOCK_CONSUME_THRESHOLD(=START_TROOP×6%, 下限100)比較, 未達門檻則不消耗、不減傷,
    // 照常全額打進去(與抵禦/警戒完全跳過同義)。
    if (dst.block.length && dmg > BLOCK_CONSUME_THRESHOLD) {
      // 批G: 傳入本次傷害類型kind, 只消耗未限定類型或類型相符的格擋層(見consumeBlock docstring);
      // TRACE用的殘餘層數b改為consumeBlock內部消耗後的那一筆(第一筆符合dmgType條件者), 而非
      // 恆定讀取block[0](dmgType過濾後可能消耗到非索引0的格擋層)。
      const matchIdx = dst.block.findIndex(bb => bb.dmgType == null || bb.dmgType === kind);
      const b = matchIdx >= 0 ? dst.block[matchIdx] : null;
      const blockVal = dst.consumeBlock(kind);
      dmg *= Math.max(0, 1 - blockVal);
      if (TRACE && b) lg(`　▸ ${dst.nm} ${blockVal >= 1 ? "抵禦" : "警戒"}生效` + (blockVal < 1 ? `（減傷${Math.round(blockVal * 100)}%）` : "") + `（剩餘${b.n > 0 ? b.n : 0}層）`);
    }
    if (dst.shield && dst.shield.amt > 0) {                        // 護盾: 先於兵力扣減吸收傷害
      const absorb = Math.min(dst.shield.amt, dmg);
      dst.shield.amt -= absorb; dmg -= absorb;
      if (TRACE && absorb > 0) lg(`　▸ ${dst.nm} 護盾吸收 ${Math.round(absorb)}` + (dst.shield.amt <= 0 ? "（已破盾）" : ""));
      if (dst.shield.amt <= 0) dst.shield = null;
    }
    const wr = woundedRate(CUR_R);        // 批18: 傷兵池 —— 本次受到的傷害按當前回合轉化率計入(準備階段 CUR_R=0 用第1回合檔)
    // 批J(禁近似令-transfer轉移族): absorbGuards(單次全額代承, redirect.guardFor:"leader")
    // —— 優先於下方常駐 guardian(%分擔every hit直到guardDur到期)判斷: 只在普攻(isNormal)時,
    // 找第一個「本回合(對該代承者而言)尚未觸發過」的登記項, 把「這一下」攻擊的傷害(依
    // ag.share, 預設1.0=全額)轉給該代承者, dst 只承受剩餘部分(share<1時); 找到就處理完這一下
    // 的兵力轉移, 不再落入下方 guardian 常駐邏輯(兩者互斥擇一, 避免同一下傷害被兩套機制各自
    // 折算一次, 造成傷害量憑空增減)。節流鍵沿用 counterGuards 慣例(掛在代承者自己的 hitFlags
    // 上, 而非 dst 身上——「每個代承單位每回合最多代承1次」, 對應原文guardFor機制既有的節流
    // 語意)。
    let absorbed = false;
    if (isNormal && dst.alive) {
      for (const ag of dst.absorbGuards) {
        if (!ag.unit.alive || ag.unit === dst) continue;
        if (ag.unit.hitFlags.has(ag)) continue;
        if (rnd() >= (ag.prob ?? 1)) continue;
        ag.unit.hitFlags.add(ag);
        const aShare = ag.share ?? 1.0, aAmt = dmg * aShare, dAmt = dmg * (1 - aShare);
        ag.unit.troop -= aAmt; ag.unit.wounded += aAmt * wr;
        fireSelfReactive(ag.unit, "dmgThreshold", bumpDmgAccum(ag.unit, aAmt));  // 禁近似令-批L: 一身是膽累積傷害門檻(見bumpDmgAccum/fireSelfReactive註解), 涵蓋範圍對稱wounded
        if (dAmt > 0) { dst.troop -= dAmt; dst.wounded += dAmt * wr; fireSelfReactive(dst, "dmgThreshold", bumpDmgAccum(dst, dAmt)); }
        if (TRACE) lg(`　▸ ${ag.unit.nm} 代${dst.nm}承受此次普攻傷害 ${Math.round(aAmt)}` + (aShare < 1 ? `（${dst.nm}自行承受剩餘${Math.round(dAmt)}）` : ""));
        absorbed = true;
        break;
      }
    }
    if (!absorbed) {
      const g = dst.guardian;
      if (g && g.alive && g !== dst && !(dst.guardNormalOnly && !isNormal)) {
        const gShare = dmg * dst.guardShare, dShare = dmg * (1 - dst.guardShare);
        g.troop -= gShare; g.wounded += gShare * wr;
        fireSelfReactive(g, "dmgThreshold", bumpDmgAccum(g, gShare));  // 禁近似令-批L
        dst.troop -= dShare; dst.wounded += dShare * wr;
        fireSelfReactive(dst, "dmgThreshold", bumpDmgAccum(dst, dShare));  // 禁近似令-批L
      }  // normalOnly 援護: 戰法傷害(isNormal=false)不轉移
      else { dst.troop -= dmg; dst.wounded += dmg * wr; fireSelfReactive(dst, "dmgThreshold", bumpDmgAccum(dst, dmg)); }  // 禁近似令-批L
    }
    if (TRACE) lg(`　→ ${dst.nm} 損兵 ${Math.round(dmg)}，剩餘 ${Math.max(0, Math.round(dst.troop))}` + (dst.troop <= 0 ? " 【擊破】" : ""));
    // 禁近似令-批K: onKillGrants(engine_wiring_gaps_misc族) —— 「這一下」把dst由存活打至
    // 陣亡(wasAlive且現在troop<=0)時, 消費src身上登記的擊殺獎勵清單(見k==="pierce"+e.onKill
    // 註冊端), 取代虎痴「破陣(擊敗鎖定目標後無視統率智力)需擊敗鎖定目標才獲得, 約後半場生效
    // →val×0.5折算」的EV近似, 改為真正「擊敗目標的那一刻」才授予, 之後常駐到戰鬥結束。
    if (wasAlive && dst.troop <= 0 && src.alive && src.onKillGrants && src.onKillGrants.length) {
      for (const g of src.onKillGrants) {
        if (g.kind === "pierce") src.pushAdd("pierce", g.val, g.dur ?? 99, "onKill:pierce");
        if (TRACE) lg(`　▸ ${src.nm} 擊敗${dst.nm}, 獲得破陣(無視統率智力)`);
      }
      src.onKillGrants = [];
    }
    if (dst.settle) dst.settle.layers = Math.min(dst.settle.max, dst.settle.layers + 1);
    // 禁近似令-批K: dmgShare(engine_wiring_gaps_misc族) —— 「使其任一目標受到傷害時會回饋X%
    // 傷害給其他敵軍」的傷害轉嫁給隊友機制(連環計), 與既有redirect(轉移給我方指定守護者,
    // 承受方向)/absorbGuards/counter(還擊來源自己)方向都不同——這裡是「dst自己已經吃了這下
    // 傷害之後, 額外再拉一個dst的隊友一起分攤」, 用 _FIGHT_CTX.alliesOf(dst) 取得dst自己的
    // 隊伍(對dst而言的「我方」, 即src視角的敵方隊伍), 排除dst自己後隨機選一位分攤val×dmg。
    // dmg>0(含被block/shield折算後的實際值)才觸發, 避免對零傷害攻擊也拉一個隊友陪打。
    if (dmg > 0 && dst.dmgShare && dst.alive && _FIGHT_CTX.alliesOf) {
      const mates = _FIGHT_CTX.alliesOf(dst).filter(x => x.alive && x !== dst);
      if (mates.length) {
        const buddy = mates[Math.floor(rnd() * mates.length)];
        const shareAmt = dmg * dst.dmgShare.pct;
        buddy.troop -= shareAmt; buddy.wounded += shareAmt * wr;
        fireSelfReactive(buddy, "dmgThreshold", bumpDmgAccum(buddy, shareAmt));  // 禁近似令-批L
        if (TRACE) lg(`　▸ ${dst.nm} 受傷回饋 ${Math.round(shareAmt)} 給 ${buddy.nm}`);
      }
    }
    const ls = src.addbonus("lifesteal");                            // 批8: 倒戈 —— 造成傷害時按比例回復自身兵力(以本次造成的傷害量 dmg 為基準), 上限 START_TROOP
    if (ls > 0 && src.alive) {
      const before = src.troop;
      // 批G: lifestealGiven(倒戈效果量加成) —— 對稱既有healGiven, 長慮「使自身攻心效果提高
      // 30%」需要此欄位("攻心"=倒戈lifesteal的裝備稱呼)。
      src.troop = Math.min(START_TROOP, src.troop + dmg * ls * Math.max(0, 1 + src.addbonus("lifestealGiven")));
      if (TRACE && src.troop - before >= 1) lg(`　▸ ${src.nm} 倒戈回復 +${Math.round(src.troop - before)}`);
    }
    // 批33: onEvent/onDeal 補傳 dmg(本次結算後的實際傷害量, 已經過block/shield/代承折算,
    // 與寫入 wounded 池的量一致) —— 供 e.ofDamage(傷害比例治療) 反應式heal使用, 見
    // onHit()/dealtDamage() 呼叫端與 applyEffects() heal 分支(opt.dmg)。
    // 批39 C: 補傳kind(本次傷害類型, phys/intel) —— 供onHit()對when.dmgType/e.when.dmgType過濾
    // (對稱dealtDamage自批27起就有的dmgType過濾, 見下方dealtDamage定義), 修正damaged/attacked
    // 反應式路徑過去完全不分兵刃/謀略傷害觸發(剛勇無前/剛烈不屈「受到兵刃傷害時」誤及謀略傷害)。
    if (onEvent) onEvent(dst, src, isNormal, dmg, kind);
    // 批27 A: on:"dealtDamage" —— src(施加本次傷害的一方)反應式觸發, 只在非規避(確實造成
    // 傷害, 含被完全格擋/護盾吸收歸零的情形——「造成傷害」語意上仍是「打出了這一擊」, 只是
    // 傷害量被防禦手段抵銷, 與「規避=攻擊未命中」不同, 故僅 dodge 分支排除, block/shield
    // 歸零不排除)時才觸發, 傳入 kind 供 dmgType(兵刃/謀略)過濾判斷。
    if (onDeal && src.alive) onDeal(src, dst, isNormal, kind, dmg);
    // 批52d: 虎嗔 —— 實際受傷疊層, 滿 maxHits 立即結算+震懾
    if (dmg > 0 && dst.huchen && dst.alive) {
      dst.huchen.hits = Math.min(dst.huchen.maxHits, dst.huchen.hits + 1);
      if (dst.huchen.hits >= dst.huchen.maxHits) settleHuchen(dst, true);
    }
    const c = dst.counter;
    if (c && dst.alive && src.alive && !(c.normalOnly && !isNormal) && rnd() < (c.prob ?? 1)) {  // 批G: normalOnly限定只在普攻(isNormal=true)時觸發, 省略時向後相容
      const ck = c.kind || "phys";
      // 禁近似令-批K: c.ofDamage(engine_wiring_gaps_misc族) —— 對稱heal既有e.ofDamage慣例
      // (依本次觸發事件的實際傷害量比例輸出), 取代反擊固定用coef重新計算一次全新damage()的
      // 舊近似(裝備「受到普通攻擊時,反彈5%傷害」——反彈的是「這一下實際承受的傷害量」的5%,
      // dmg是本次已經過block/shield折算後的實際傷害量)。
      const cd = c.ofDamage != null ? dmg * c.ofDamage : damage(dst, src, c.coef ?? 1, ck); src.troop -= cd; src.wounded += cd * woundedRate(CUR_R);
      fireSelfReactive(src, "dmgThreshold", bumpDmgAccum(src, cd));  // 禁近似令-批L
      if (TRACE) lg(`　↩ ${dst.nm} 反擊 ${src.nm} 損兵 ${Math.round(cd)}，剩餘 ${Math.max(0, Math.round(src.troop))}`);
      // 批52e/f: 反擊亦計「造成傷害」(文武雙全等); 零傷(抵禦/虛弱)仍觸發
      if (onDeal && dst.alive) onDeal(dst, src, false, ck, cd);
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
          const gk = g.kind || "phys";
          const gd = damage(gu, src, g.coef ?? 1, gk);
          src.troop -= gd; src.wounded += gd * woundedRate(CUR_R);
          fireSelfReactive(src, "dmgThreshold", bumpDmgAccum(src, gd));  // 禁近似令-批L
          if (TRACE) lg(`　↩ ${gu.nm}(守護${dst.nm}) 反擊 ${src.nm} 損兵 ${Math.round(gd)}，剩餘 ${Math.max(0, Math.round(src.troop))}`);
          // 批52f: 守護反擊零傷仍觸發 dealtDamage
          if (onDeal && gu.alive) onDeal(gu, src, false, gk, gd);
          // 禁近似令-批K: counter_target_binding族 —— guardFor反擊觸發後, 額外副作用精確
          // 綁定到「這一次」的攻擊者(src)或反擊執行者自己(gu), 不透過 applyEffects 的 who
          // 派發(hit()無隊伍context, 見上方 g.debuffAttacker/g.selfStack 註冊處註解)。
          if (g.debuffAttacker && src.alive) {
            const da = g.debuffAttacker;
            src.pushAdd("amp", -(da.val || 0), (da.dur ?? 1) + 1, "counterGuard:debuffAttacker", da.dmgType ? { dmgType: da.dmgType } : undefined);
            if (TRACE) lg(`　▸ ${src.nm} 被${gu.nm}反擊命中, 造成傷害降低${Math.round((da.val || 0) * 100)}%(${(da.dur ?? 1)}回合)`);
          }
          if (g.selfStack) {
            const ss = g.selfStack;
            if (!gu.guardStackN) gu.guardStackN = new Map();
            const already = gu.guardStackN.get(g) || 0;
            if (ss.max == null || already < ss.max) {
              const layers = already + 1;
              gu.guardStackN.set(g, layers);
              const total = (ss.perVal || 0) * layers;
              gu.pushStatAdd(ss.statField || "force", total, ss.dur ?? 99, "counterGuard:selfStack");
              if (TRACE) lg(`　▸ ${gu.nm} 守護反擊疊層 第${layers}層（累計${STAT_ZH[ss.statField] || ss.statField || "武力"}+${total.toFixed(1)}）`);
            }
          }
        }
      }
    }
    // 禁近似令-批K: hit() 補 return dmg(過去無回傳值, 呼叫端一律另讀 damage() 的回傳值)——
    // 供 fireExtraHits 的 eh.lifesteal(engine_wiring_gaps_misc族)讀取「這一段 extraHits 自己
    // 造成的實際傷害量」計算自我回血, 純新增不影響任何既有呼叫端(過去全部呼叫點皆未讀取
    // hit() 回傳值, 零回歸)。
    return dmg;
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
  // 批52d: 虎嗔結算
  function settleHuchen(u, early) {
    const h = u.huchen;
    if (!h) return;
    u.huchen = null;
    const caster = h.caster;
    const hits = Math.min(h.hits || 0, h.maxHits || 3);
    const coef = (h.base ?? 0.20) + hits * (h.per ?? 0.30);
    if (caster && caster.alive && u.alive) {
      const dmg = damage(caster, u, coef, h.kind || "phys");
      u.troop -= dmg; u.wounded += dmg * woundedRate(CUR_R);
      fireSelfReactive(u, "dmgThreshold", bumpDmgAccum(u, dmg));  // 禁近似令-批L
      if (TRACE) lg(`　▸ 虎嗔結算 → ${u.nm} 傷${Math.round(dmg)}（率${Math.round(coef * 100)}%${early ? ", 提前" : ""}）`);
    }
    if (early && u.alive) u.stun = Math.max(u.stun, 2);
    if (caster && caster.alive) {
      caster.pushAdd("amp", h.ampOnSettle ?? 0.08, 99, h.src || "虎嗔", { dmgType: "phys" }, h.ampMaxStack ?? 99);
    }
  }
  function targetHas(u, type) {
    if (!u) return false;
    // 批I(禁近似令-scale/比較族): type 可為陣列 —— OR語意, 只要命中其中任一單一type即算
    // 符合(深藏若虛「震懾/計窮/繳械/混亂任一」/百步穿楊/橫掃千軍), 遞迴呼叫自身逐一比對。
    // 呼叫端 ifTargetHasNot 沿用同一函式再取反, De Morgan's律自動給出正確的「皆非」語意,
    // 不需要對 ifTargetHasNot 額外處理陣列語意。
    if (Array.isArray(type)) return type.some(t => targetHas(u, t));
    if (type === "dot") return u.dots.length > 0;
    if (type === "huchen" || type === "虎嗔") return !!u.huchen;
    if (type === "capture" || type === "捕獲" || type === "captured") return (u.captured || 0) > 0;
    if (type === "stun" || type === "silence" || type === "disarm" || type === "chaos" || type === "insight") return u[type] > 0;
    // 批I: weak/虛弱 —— 偵測「amp總和<=-1」(無法造成傷害的虛弱狀態, 挫志怒襲等戰法用amp
    // val:-1.0表達), 對稱既有extra/群攻用addbonus查詢的慣例。
    if (type === "weak" || type === "虛弱" || type === "weakened") return u.addbonus("amp") <= -1;
    // 批C: 群攻(extra)狀態查詢——對稱sgz.py target_has同名分支, 見其詳細註解(引弦力戰「若已
    // 處於群攻狀態」需要判斷持有者自身是否已有extra加成)。
    if (type === "extra" || type === "群攻") return u.addbonus("extra") > 0;
    // 批52g: 具名 dot(水攻/沙暴…)
    if (u.dots.some(d => d[3] === type)) return true;
    return false;
  }
  function countNamedStatuses(u, names) {
    if (!u || !names || !names.length) return 0;
    const want = new Set(names), found = new Set();
    for (const d of u.dots) if (d[3] && want.has(d[3])) found.add(d[3]);
    return found.size;
  }
  // 批52g: 戰法名→默認 dot 狀態名
  const DOT_NAME_BY_TACTIC = {
    "水淹七軍": "水攻", "興雲布雨": "水攻", "興雲佈雨": "水攻", "風聲鶴唳": "水攻",
    "呼風喚雨": "水攻", "飛沙走石": "沙暴", "天降火雨": "灼燒", "火熾原燎": "灼燒",
    "焰焚箕軫": "灼燒", "神火計": "灼燒", "火燒連營": "灼燒", "楚歌四起": "沙暴",
  };
  function resolveDotName(e, t) {
    return e.name || e.dotName || DOT_NAME_BY_TACTIC[t.nameZh || ""] || null;
  }
  // 批16: dispel(驅散/淨化) —— 移除目標身上對應方向(buffs=正向增益/debuffs=負向減益)的條目,
  // 略過帶 undispellable 旗標(flags.undispellable, 見 pushAdd/pushMod/pushStatAdd 呼叫端 udFlags)的條目。
  // buffs: amp(正值)/mitig(正值)/stat mult>1或add>0/rateup/chargeup/shield/dodge/surehit/lifesteal/healBoost/healGiven/counter/pierce/extra/first/insight
  // debuffs: amp(負值)/mitig(負值)/stat mult<1或add<0 + 控制欄位(stun/silence/disarm/chaos/dot/healblock/fakeReport/swap)
  // 只挪動「數值型」adds/mods/statAdds 依正負號分類; 控制欄位(debuffs專屬)直接歸零/清空。
  // 批J(禁近似令-transfer轉移族): notUD 從 dispelUnit 內部提出成共用函式(原僅 dispelUnit
  // 本地閉包), 供新增的 collectDebuffTokens 一併重用同一份「是否可被驅散/轉移」判斷, 避免
  // 兩處各自維護一份 undispellable 判斷式而日後改動時彼此漂移。
  function notUD(entry) { return !(entry[4] && entry[4].undispellable); }
  function dispelUnit(u, what) {
    const isBuff = a => (a[0] === "amp" || a[0] === "mitig") ? a[1] > 0 : true;   // 除 amp/mitig 外的 adds 種類(rateup/chargeup/healBoost/healGiven/lifesteal/pierce/extra)一律視為buff
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
      u.huchen = null;                                   // 批52d: 虎嗔為負面狀態, 草船/刮骨可清
      u.stun = 0; u.silence = 0; u.disarm = 0; u.chaos = 0; u.healblock = 0; u.fakeReportDur = 0; u.ambush = 0;
      // 批52j: captured 不清除(無法被淨化)
    }
    if (TRACE) lg(`　▸ ${u.nm} 被驅散〔${what === "buffs" ? "增益" : "減益"}〕`);
  }
  // 批J(禁近似令-transfer轉移族): collectDebuffTokens —— 供 k:"transferDebuff" 使用, 掃描
  // pool(存活單位陣列)內每個單位當下持有的「負面狀態」具體實例, 回傳 token 陣列, 每個 token
  // = {kind(供依種類分組挑選), unit(持有者), move(dest,dur)=>把這個實例從unit搬到dest}。
  // 分類口徑刻意與既有 dispelUnit 的 debuffs 分支完全一致(負值amp/mitig、mult<1的mods、
  // 負值statAdds、dot、stun/silence/disarm/chaos/healblock/fakeReport/ambush/huchen), 不另立
  // 新標準, 確保「什麼算負面狀態」全庫只有一套定義。move() 內部同時完成「來源移除」與「目的地
  // 重建」兩步, 避免呼叫端分兩步做時忘記其中一步、或順序錯置導致資料读取到已移除的實例。
  function collectDebuffTokens(pool) {
    const out = [];
    for (const u of pool) {
      // 批J: notUD(undispellable) 過濾 —— 與 dispelUnit 一致, 標記 undispellable 的實例不可
      // 被驅散, 同理也不該能被 transferDebuff 這個「移除來源實例」的操作繞過, 故一併排除。
      for (const a of u.adds) if ((a[0] === "amp" || a[0] === "mitig") && a[1] < 0 && notUD(a)) {
        out.push({ kind: a[0], unit: u, move: (dest, dur) => { u.adds.splice(u.adds.indexOf(a), 1); dest.pushAdd(a[0], a[1], dur, a[3]); } });
      }
      for (const m of u.mods) if (m[1] < 1 && notUD(m)) {
        out.push({ kind: "mod:" + m[0], unit: u, move: (dest, dur) => { u.mods.splice(u.mods.indexOf(m), 1); dest.pushMod(m[0], m[1], dur, m[3]); } });
      }
      for (const s of u.statAdds) if (s[1] < 0 && notUD(s)) {
        out.push({ kind: "stat:" + s[0], unit: u, move: (dest, dur) => { u.statAdds.splice(u.statAdds.indexOf(s), 1); dest.pushStatAdd(s[0], s[1], dur, s[3]); } });
      }
      for (const d of u.dots) if (!d[2]) {   // d[2]=undispellable旗標(見dot k-type施加處), 對稱dispelUnit保留undispellable dot的慣例
        out.push({ kind: "dot:" + (d[3] || "?"), unit: u, move: (dest, dur) => { u.dots.splice(u.dots.indexOf(d), 1); dest.dots.push([d[0], dur, d[2], d[3]]); } });
      }
      if (u.stun > 0) out.push({ kind: "stun", unit: u, move: (dest, dur) => { u.stun = 0; dest.stun = Math.max(dest.stun, (dur ?? 1) + 1); } });
      if (u.silence > 0) out.push({ kind: "silence", unit: u, move: (dest, dur) => { u.silence = 0; dest.silence = Math.max(dest.silence, (dur ?? 1) + 1); } });
      if (u.disarm > 0) out.push({ kind: "disarm", unit: u, move: (dest, dur) => { u.disarm = 0; dest.disarm = Math.max(dest.disarm, (dur ?? 1) + 1); } });
      if (u.chaos > 0) out.push({ kind: "chaos", unit: u, move: (dest, dur) => { u.chaos = 0; dest.chaos = Math.max(dest.chaos, (dur ?? 1) + 1); } });
      if (u.healblock > 0) out.push({ kind: "healblock", unit: u, move: (dest, dur) => { u.healblock = 0; dest.healblock = Math.max(dest.healblock, (dur ?? 1) + 1); } });
      if (u.fakeReportDur > 0) out.push({ kind: "fakeReport", unit: u, move: (dest, dur) => { u.fakeReportDur = 0; dest.fakeReportDur = Math.max(dest.fakeReportDur, (dur ?? 1) + 1); } });
      if (u.ambush > 0) out.push({ kind: "ambush", unit: u, move: (dest, dur) => { u.ambush = 0; dest.ambush = Math.max(dest.ambush, (dur ?? 1) + 1); } });
      if (u.huchen) out.push({ kind: "huchen", unit: u, move: (dest) => { dest.huchen = u.huchen; u.huchen = null; } });
    }
    return out;
  }
  // 批J: pickN —— 通用「從陣列隨機挑n個不重複元素」, 對稱既有 pickTargets(Unit專用, 含.alive
  // 過濾), 但這裡的元素是任意值(如 kind 字串), 不做 alive 過濾。n>=陣列長度時回傳整份洗牌拷貝。
  function pickN(arr, n) {
    const pool = arr.slice(), out = [];
    for (let i = 0; i < n && pool.length; i++) { const idx = Math.floor(rnd() * pool.length); out.push(pool[idx]); pool.splice(idx, 1); }
    return out;
  }
  function pickTarget(units, attacker, allyPool) {            // 普攻/單體戰法: 隨機挑一個存活敵軍(不再固定打兵力最高); 嘲諷: 攻擊者身上有 tauntBy 時強制指向該目標
    if (attacker && attacker.tauntDur && attacker.tauntBy && attacker.tauntBy.alive && units.includes(attacker.tauntBy)
        && !(allyPool && attacker.tauntBy.captured)) return attacker.tauntBy;
    const live = units.filter(u => u.alive && !(allyPool && u.captured));
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
    maxTroop: u => u.troop,  // 批45 C: 兵力最高準則(對稱minTroop), 見engine_limitations.md第17節——
    // 過去只有minTroop(=mostDamaged, 兵力最低=最受損)一種方向, 「兵力最高」的敵軍/我軍選標
    // 缺口(定謀貴決「使敵軍兵力最高的武將...」)長年只能誠實揭露維持無targetSel近似, 現補上。
    maxSpeed: u => u.eff("speed"),  // 批G: 速度最快準則(對稱既有maxForce/maxIntel/maxTroop準則
    // 家族), 萬軍奪帥「使敵軍速度最快的武將降速」過去因準則家族缺這個具體枚舉值, 只能退化套用
    // 全體敵軍(較原文寬鬆, 高估), 現補上, 非新機制, 純粹是準則枚舉表補一個成員。
  };
  const TARGETSEL_MIN = new Set(["minTroop", "minIntel", "minCommand", "mostDamaged"]);  // maxTroop/maxSpeed故意不加入此集合, 使pickByCriterion對它們用max()而非min()
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
  // 批B(filter-then-pick修正): 目標資格gate統一判定 —— 對稱 sgz.py _target_gate_ok/
  // _has_target_gate/_gate_pool 模組層級註解。ifTargetHas/ifTargetHasNot/ifStatCompare/
  // ifTargetHpAbove/ifTargetHpBelow/ifSelfStatCompare/ifTargetIsRank/ifTargetIsRankNot/
  // whoNames 這些效果級欄位共同的性質: 是否命中「純粹取決於候選單位u自身當下狀態/屬性」,
  // 與u是否被隨機選中無關(選前選後獨立評估必得到同一個布林值)。過去 applyEffects/
  // fireExtraHits 的 who==="enemy"/"ally" 隨機選標分支一律「先 pickTargets 隨機挑n個,
  // 挑完才用這些gate過濾」——若隨機挑中不合格目標, 過濾後dests變空/縮水, 明明池中另有合格
  // 目標卻白白錯過(隔離實測橫掃千軍案例: 對1個已計窮+1個乾淨的敵組, 應100%命中計窮目標,
  // 舊實作只29/50命中, 見批B交接文件)。正解: 有這類gate時應「先過濾出合格池, 再從合格池
  // pickTargets」(filter-then-pick)。不含 sameTargets/ifSameTargetIsLeader——這兩者語意是
  // 「事後檢查這次隨機結果是否恰好是某個特定對象」, 本質上就是要在挑選動作發生後才能判斷
  // (等同於「抽到大獎的機率」, pre-filter會把機率語意錯改成必中), 不適用本原語。
  const TARGET_GATE_KEYS = ["ifTargetHas", "ifTargetHasNot", "ifStatCompare", "ifTargetHpAbove",
    "ifTargetHpBelow", "ifSelfStatCompare", "ifTargetIsRank", "ifTargetIsRankNot", "whoNames"];
  function hasTargetGate(e) {
    return TARGET_GATE_KEYS.some(k => e[k] != null);
  }
  // ifTargetIsRank/ifTargetIsRankNot 用: spec.stat -> 準則名。原為 applyEffects 內部區域
  // 變數, 批B抽到頂層供 targetGateOk 與既有選後過濾共用同一份邏輯(對稱 sgz.py _rank_key)。
  function rankKeyOf(spec) {
    return spec.stat === "intel" ? "maxIntel" : "maxForce";
  }
  function targetGateOk(u, e, ref, allies, enemies) {
    if (e.ifTargetHas && !targetHas(u, e.ifTargetHas)) return false;
    if (e.ifTargetHasNot && targetHas(u, e.ifTargetHasNot)) return false;
    if (e.ifStatCompare && !statCompareOk(ref, u, allies, e.ifStatCompare)) return false;
    if (e.ifTargetHpAbove != null && !(u.hpPct > e.ifTargetHpAbove)) return false;
    if (e.ifTargetHpBelow != null && !(u.hpPct < e.ifTargetHpBelow)) return false;
    if (e.ifSelfStatCompare) {
      const spec = e.ifSelfStatCompare, opFn = {
        gt: (a, b) => a > b, gte: (a, b) => a >= b, lt: (a, b) => a < b, lte: (a, b) => a <= b,
      }[spec.op || "gt"];
      if (!opFn(u.eff(spec.statA), u.eff(spec.statB))) return false;
    }
    if (e.ifTargetIsRank) {
      const champ = pickByCriterion(enemies, rankKeyOf(e.ifTargetIsRank));
      if (u !== champ) return false;
    }
    if (e.ifTargetIsRankNot) {
      const specs = Array.isArray(e.ifTargetIsRankNot) ? e.ifTargetIsRankNot : [e.ifTargetIsRankNot];
      const champs = specs.map(s => pickByCriterion(enemies, rankKeyOf(s)));
      if (champs.includes(u)) return false;
    }
    if (e.whoNames) {
      const wn = Array.isArray(e.whoNames) ? e.whoNames : [e.whoNames];
      if (!(u.g && wn.includes(u.g.name))) return false;
    }
    return true;
  }
  // filter-then-pick: 若e帶任何目標資格gate, 回傳過濾後的合格候選池(供pickTargets隨機挑選
  // 前使用); 無gate則原樣回傳pool(不新增array, 維持原隨機行為零改動)。
  function gatePool(pool, e, ref, allies, enemies) {
    return hasTargetGate(e) ? pool.filter(u => targetGateOk(u, e, ref, allies, enemies)) : pool;
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

  // 批52i: 完整普通攻擊管線(垂心萬物 proxyNormal 與主迴圈普攻共用)
  function doNormalAttack(u, allies, enemies, onHit, onDeal, activeFiredFn, allowExtra, allowCharge) {
    if (!u || !u.alive || u.disarm) return null;
    onHit = onHit || _FIGHT_CTX.onHit;
    onDeal = onDeal || _FIGHT_CTX.onDeal;
    activeFiredFn = activeFiredFn || _FIGHT_CTX.activeFired;
    const alliesOf = _FIGHT_CTX.alliesOf || (() => allies);
    const foesOf = _FIGHT_CTX.foesOf || (() => enemies);
    const al = typeof alliesOf === "function" ? alliesOf(u) : allies;
    const fo = typeof foesOf === "function" ? foesOf(u) : enemies;
    let tgt = pickTargetChaos(u, al, fo);
    if (!tgt) return null;
    // 禁近似令-批K: preAttackHooks(pre_damage_intercept鄰居, engine_wiring_gaps_misc族) ——
    // 「自身即將受到普通攻擊時」的真反應式掛鉤點(區別於existing preDmgHooks, 那是攻擊/防禦方
    // 傷害已確定要發生後的修正; 這裡是「即將被打」這件事本身觸發, 傷害是否照常落在tgt身上都
    // 還未定), 取代 redirect/heal 過去只能「prep一次性擲骰決定整場有無」的EV折算, 改為每次
    // 真正要挨打前才擲骰判定(見雲聚影從 redirectPre/益其金鼓 healAllyPre)。掛在tgt身上(即將
    // 受擊的那一方), 只在普攻路徑觸發(原文皆明寫「即將受到普通攻擊」)。
    if (tgt.preAttackHooks && tgt.preAttackHooks.length) {
      const tgtMates = fo.filter(x => x.alive && x !== tgt);
      for (const h of tgt.preAttackHooks) {
        if (rnd() >= (h.rate ?? 1)) continue;
        if (h.hookKind === "redirectPre" && tgtMates.length) {
          let guard = tgtMates[0];
          if (h.guard === "max_force") for (const a of tgtMates) if (a.eff("force") > guard.eff("force")) guard = a;
          if (TRACE) lg(`　▸ ${tgt.nm} 觸發代承(preAttack), 改由 ${guard.nm} 承受此次普通攻擊`);
          tgt = guard;
        } else if (h.hookKind === "healAllyPre" && tgtMates.length) {
          const recv = tgtMates[Math.floor(rnd() * tgtMates.length)];
          if (recv.alive && !recv.healblock) {
            const hcoefH = (h.coef ?? 0.5) * (h.scale ? scaleOf(tgt, h.scale) : 1);
            const want = hcoefH * (tgt.troop * HEAL_TROOP_C);
            const actual = Math.max(0, Math.min(want, recv.wounded, START_TROOP - recv.troop));
            const before = recv.troop;
            recv.troop += actual; recv.wounded -= actual;
            if (TRACE && recv.troop - before >= 1) lg(`　▸ ${tgt.nm} 觸發即將受擊治療(preAttack) → ${recv.nm} +${Math.round(recv.troop - before)}`);
          }
        }
      }
    }
    hit(u, tgt, 1.0, "phys", true, onHit, onDeal);
    // 禁近似令-批K: splash(splash_aoe_primitive族) —— 普攻命中tgt後, 若u持有splash加成
    // (val=濺射比例), 同時對tgt「同部隊其他武將」(即tgt所在敵隊除tgt外的存活成員)造成
    // splashRatio倍率的兵刃傷害, 與extra(重新隨機挑一個全新目標, 不保證同隊)語意不同——
    // 這裡精確鎖定tgt本人的隊友, 真正的多目標同時結算(瞋目橫矛/象兵)。
    const splashRatio = u.addbonus("splash");
    if (splashRatio > 0) {
      for (const mate of fo) if (mate !== tgt && mate.alive) hit(u, mate, splashRatio, "phys", true, onHit, onDeal);
    }
    if (allowExtra !== false) {
      for (let i = 0; i < extraCount(u.addbonus("extra")); i++) {
        const nt = pickTargetChaos(u, al, fo);
        if (nt) hit(u, nt, 1.0, "phys", true, onHit, onDeal);
      }
    }
    for (const t of u.tactics) {
      if (t.everyN && t.everyN.on === "attack" && u.tickEveryN && u.tickEveryN(t)) {
        if (t.extraHits) fireExtraHits(u, t, tgt, alliesOf, foesOf, onHit, onDeal);
        if (t.effects && t.effects.length) applyEffects(u, tgt, t, al, fo);
      }
    }
    if (allowCharge !== false) {
      for (const t of u.tactics) {
        const up = t.proc ? 0 : u.addbonusFor("chargeup", t);
        if (t.type === "charge" && rnd() < t.rate + up) {
          if (t.coef) {
            // 批D(R32): 對稱 sgz.py 同名分支(見其詳細註解) —— 突擊分派過去無條件只對已選定
            // 的單一 tgt 打一次, 不讀頂層 n/nMax/hitsRepeat, 「對敵軍全體」(一騎當千)AoE與
            // 「發動三次隨機打擊」(摧鋒斷刃 hitsRepeat)皆被靜默塌縮成單體單次。cnt<=1 維持
            // 原行為零回歸。
            let cnt = t.n || 1;
            if (t.nMax) cnt = cnt + Math.floor(rnd() * (t.nMax - cnt + 1));
            if (cnt <= 1) {
              hit(u, tgt, t.coef, t.kind, false, onHit, onDeal, undefined, true);
            } else if (t.hitsRepeat) {
              for (let i = 0; i < cnt; i++) {
                const v = pickTarget(fo);
                if (v) hit(u, v, t.coef, t.kind, false, onHit, onDeal, undefined, true);
              }
            } else {
              for (const v of pickTargets(fo, cnt)) hit(u, v, t.coef, t.kind, false, onHit, onDeal, undefined, true);
            }
          }
          if (t.extraHits) fireExtraHits(u, t, tgt, alliesOf, foesOf, onHit, onDeal);
          applyEffects(u, tgt, t, al, fo);
          if (activeFiredFn) activeFiredFn(u);
        }
      }
    }
    return tgt;
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
    // 批52f: 預解析 srcSel —— 判定「武力/智力最高是否同一人」供 sameSrcCoef(眾望所歸)
    const agentSrcs = t.extraHits.filter(eh => eh.srcSel).map(eh => pickByCriterion(alliesOf(u), eh.srcSel));
    const samePerson = t.sameSrcCoef != null && agentSrcs.length >= 2
      && agentSrcs[0] != null && agentSrcs.every(s => s === agentSrcs[0]);
    for (const eh of t.extraHits) {
      if (rnd() >= (eh.rate ?? 1)) continue;
      // 批52续: eh.when 回合窗口
      if (eh.when && !roundOk({ when: eh.when }, CUR_R)) continue;
      // 批44 A: eh.ifLeaderIs —— extraHits 段級「隊伍主將(allies[0])的武將名須匹配指定值」
      // 條件閘門, 對稱 applyEffects() 的 e.ifLeaderIs(見其詳細註解)。用於白毦兵等「若XX統領,
      // 主段傷害更高」家族的 base(頂層coef, 無條件)+top-up(extraHits段, sameTarget+
      // ifLeaderIs)拆法——頂層coef是command型戰法固有的主傷害段, 無法像effects段那樣掛
      // ifLeaderIs, 改用extraHits補一段「命中同一目標的差額傷害」達成等價的base+topup效果。
      if (eh.ifLeaderIs) { const names = Array.isArray(eh.ifLeaderIs) ? eh.ifLeaderIs : [eh.ifLeaderIs]; const al = alliesOf(u); if (!(al && al[0] === u && u.g && names.includes(u.g.name))) continue; }
      // 批52: eh.ifLeader / ifSub —— 僅主將/僅副將時結算此 extraHits 段
      if (eh.ifLeader) { const al = alliesOf(u); if (!(al && al[0] === u)) continue; }
      if (eh.ifSub) { const al = alliesOf(u); if (!(al && al[0] !== u)) continue; }
      // 批52f: 代理出手(srcSel) —— 我軍屬性最高者發動, hit 的 src 為該友軍(文武等 dealtDamage 掛其身)
      let atk = u;
      if (eh.srcSel) {
        atk = pickByCriterion(alliesOf(u), eh.srcSel);
        if (!atk || !atk.alive) continue;
      }
      const coef = (samePerson && eh.srcSel) ? t.sameSrcCoef : eh.coef;
      const n = eh.n || 1;
      const cnt = eh.nMax ? n + Math.floor(rnd() * (eh.nMax - n + 1)) : n;
      let dests;
      // 批18: targetSel(指定選標準則) —— 段級欄位, 優先於 who 的其餘規則(sameTarget/enemyLeader/
      // 隨機)。如 上兵伐謀「分別對兵力最低、武力最高、智力最低的敵將」三段各自不同準則。
      // 批A(11筆高嚴重重建): eh.who === "mainTargetAlly" —— 「(主coef段已選定的目標)轉而對
      // 其友軍單體發動攻擊」(偽書相間「若目標處於混亂狀態則使目標對其友軍單體發動攻擊」)。
      // 方向反轉: atk 不是持有者 u 自己, 而是 tgt(main段命中的敵方目標)本身被強制出手;
      // dests 則是 tgt 自己那一側的隊友(從 u 的視角看, tgt 那一側正是 foesOf(u), 即 u 的
      // 敵方隊伍——tgt 的隊友 = u 的其他敵人, 排除 tgt 自己)。與既有「u 打某個目標」的所有
      // who 值方向相反, 屬於全新的「事件目標反過來打自己人」語意, 現有 sameTarget/
      // enemyLeader/預設 泛用選標都無法表達此反轉方向, 故新增獨立 who 值。
      let mainTargetAllyAtk = null;
      if (eh.who === "mainTargetAlly") {
        // ifTargetHas/ifTargetHasNot(若有指定)在此特殊路徑要檢查的是 tgt 本身(main段已選定的
        // 目標, 即將被強制出手的那一位)是否具備該狀態, 而非檢查 dests(tgt的隊友, 承受傷害的
        // 那一方)——與下方共用的「dests 事後過濾」慣例方向不同, 故這裡提前判斷, 並把 eh 上的
        // ifTargetHas/ifTargetHasNot 標記為已處理(避免下面共用過濾段再次誤用 dests 錯誤過濾)。
        const tgtGateOk = tgt && tgt.alive
          && (!eh.ifTargetHas || targetHas(tgt, eh.ifTargetHas))
          && (!eh.ifTargetHasNot || !targetHas(tgt, eh.ifTargetHasNot));
        if (tgtGateOk) {
          const tgtSide = foesOf(u).filter(v => v.alive && v !== tgt);  // tgt自己那一側其餘存活隊友
          if (tgtSide.length) { mainTargetAllyAtk = tgt; dests = [tgtSide[Math.floor(rnd() * tgtSide.length)]]; }
          else dests = [];
        } else dests = [];
      }
      else if (eh.targetSel) { const picked = pickByCriterion(foesOf(u), eh.targetSel); dests = picked ? [picked] : []; }
      else if (eh.who === "sameTarget") dests = tgt && tgt.alive ? [tgt] : [];        // 沿用主段已選定的(單體)目標
      else if (eh.who === "enemyLeader") { const fl = foesOf(u)[0]; dests = (fl && fl.alive) ? [fl] : []; }  // 固定打敵方主將(index 0)
      else if (cnt <= 1 && tgt && tgt.alive && !eh.who) dests = [tgt];   // 未指定 who 且單體: 沿用主段目標(向後相容預設行為)
      else {
        // 批B: filter-then-pick(對稱 applyEffects 同名修正, 見 gatePool 頂層註解) —— eh帶
        // ifTargetHas/ifStatCompare等資格gate時, 先過濾foesOf(u)成合格池再pickTargets, 避免
        // 「隨機挑中不合格目標, 過濾後dests落空」(百步穿楊 extraHits ifTargetHas陣列案例:
        // 對1個已控制+1個乾淨的敵組, 應100%命中控制中的目標)。
        const fo = foesOf(u);
        dests = pickTargets(gatePool(fo, eh, atk, alliesOf(atk), fo), cnt);
      }
      if (mainTargetAllyAtk) atk = mainTargetAllyAtk;    // 覆寫本段攻擊者為 tgt 本身(見上方who==="mainTargetAlly"分支)
      // 批16: ifTargetHas —— extraHits 段結算前檢查, 只對「已有該狀態」的目標結算此段傷害。
      // 批A: who==="mainTargetAlly" 時 ifTargetHas/ifTargetHasNot 已在上方針對 tgt(main段
      // 目標本身)提前判斷過(見該分支註解), 這裡跳過(避免對 dests=tgt的隊友 誤重複套用同一個
      // 條件, 那些隊友身上通常沒有該狀態, 會被錯誤過濾掉)。
      if (eh.ifTargetHas && eh.who !== "mainTargetAlly") dests = dests.filter(v => targetHas(v, eh.ifTargetHas));
      // 批I(禁近似令-scale/比較族): eh.ifStatCompare —— extraHits 段結算前檢查, 只對
      // 「參照方(攻擊者atk自身或其隊伍主將)vs目標」屬性比較成立的目標結算此段傷害(竊幸乘寵
      // 「若自身智力高於目標則額外造成一次謀略傷害」), 對稱effects段的e.ifStatCompare
      // (見applyEffects), 共用statCompareOk()。
      if (eh.ifStatCompare) dests = dests.filter(v => statCompareOk(atk, v, alliesOf(atk), eh.ifStatCompare));
      // 批31 B: ifSameTargetIsLeader —— extraHits 段結算前檢查, 只對「(主coef段隨機選定的)
      // 目標剛好就是敵方隊伍固定位置的主將(foes[0])」時才結算此段傷害, 精確表達原文「若目標
      // (普攻/主傷段隨機選定的對象)為敵軍主將，額外造成傷害」這種條件分支。取代舊有EV折算
      // 近似(如暗藏玄機過去用1/3機率折算「隊伍3人之一為主將」的近似觸發率)。
      if (eh.ifSameTargetIsLeader) { const fl = foesOf(u)[0]; const leader = (fl && fl.alive) ? fl : null; dests = dests.filter(v => v === leader); }
      // 批A: eh.kindByStat === "maxForceIntel" —— 傷害類型不是固定寫死的 phys/intel, 而是
      // 依「攻擊者(atk)本身武力/智力較高的一項」動態決定(偽書相間「類型取決於目標武力、智力
      // 較高的一項」——這裡的「目標」在mainTargetAlly反轉語意下就是atk=tgt本身)。與批34
      // 胡笳餘音(遇到同類「取較高者」措辭時只能靜態近似取intel, 見tactic_corrections.json
      // 該筆_note)不同, 這裡改為真正動態比較atk.eff("force")與atk.eff("intel"), 更精確。
      const ehKind = eh.kindByStat === "maxForceIntel" ? (atk.eff("force") >= atk.eff("intel") ? "phys" : "intel") : (eh.kind || "phys");
      if (TRACE && dests.length) lg(`　▸ ${t.nameZh || "?"}〔額外段${eh.srcSel ? "·出手" + eh.srcSel : ""}${eh.targetSel ? "·" + eh.targetSel : ""}${mainTargetAllyAtk ? "·mainTargetAlly(" + atk.nm + "被迫出手)" : ""}〕${ehKind === "intel" ? "謀略" : "兵刃"}傷害(${Math.round(coef * 100)}%) by ${atk.nm} → ${dests.map(v => v.nm).join("、")}` + (eh._note ? `（${eh._note}）` : ""));
      // 禁近似令-批K: eh.lifesteal(engine_wiring_gaps_misc族) —— 「僅限本extraHits段自身傷害
      // 的回復欄位」, 對稱既有 lifesteal(持有者身上的standing addbonus, 對該單位往後所有
      // 傷害都生效)但顆粒度縮小到只讀這一段的dmg(不透過addbonus通道, 避免誤及本戰法主coef段
      // 等其他傷害來源), 供錦帆軍「若目標已潰逃則造成兵刃攻擊並恢復傷害量的30%兵力」——取代
      // 「30%傷害量回血未建模(保守)」的既有缺口。
      for (const v of dests) {
        const ehDmg = hit(atk, v, coef, ehKind, false, onHit, onDeal);
        if (eh.lifesteal && ehDmg > 0 && atk.alive) {
          const before = atk.troop;
          atk.troop = Math.min(START_TROOP, atk.troop + ehDmg * eh.lifesteal);
          if (TRACE && atk.troop - before >= 1) lg(`　▸ ${atk.nm} extraHits倒戈回復 +${Math.round(atk.troop - before)}`);
        }
      }
    }
  }

  const STAT_ZH = { force: "武力", intel: "智力", command: "統率", speed: "速度", all: "全屬性", charm: "魅力" };
  function effDesc(k, e, caster) {                  // 把15原語效果翻成可讀中文(供日誌); caster 供 scale 縮放後實際值顯示
    const p = v => Math.round(Math.abs(v) * 100) + "%";
    const d = e.dur && e.dur < 90 ? `(${e.dur}回合)` : "";
    // 批35 B: k==="block" 顯示用 lockedScaleOf(準備階段鎖定值, 與實際套用時一致), 其餘 k 維持
    // scaleOf 即時值(現階段僅 block 有實測樣本佐證鎖定語意, 見 lockedScaleOf 註解)。
    const scOf = k === "block" ? lockedScaleOf : (c, ee) => scaleOf(c, ee.scale, ee.scaleDiv);
    const sfx = e.scale && caster ? `〔受${STAT_ZH[e.scale] || e.scale}影響, ×${scOf(caster, e).toFixed(2)}〕` : "";
    const val = (e.scale && caster) ? Math.max(-SCALE_CLAMP, Math.min(SCALE_CLAMP, e.val * scOf(caster, e))) : e.val;
    const mult = (e.scale && caster) ? 1 + ((e.mult ?? 1) - 1) * scOf(caster, e) : e.mult;
    switch (k) {
      case "amp": return (e.who === "enemy" && val > 0 ? `易傷+${p(val)}${d}` : (val >= 0 ? `增傷+${p(val)}${d}` : `減傷${p(val)}${d}`)) + sfx;
      case "mitig": return (val >= 0 ? `減傷+${p(val)}${d}` : `易傷+${p(val)}${d}`) + sfx;
      // 批H: critUp(會心/奇謀機率, 依dmgType分流顯示中文名)/critDmgUp(會心/奇謀傷害幅度加成,
      // 疊在基礎+100%之上), 見 damage() 對稱段落。
      case "critUp": return `${e.dmgType === "intel" ? "奇謀" : "會心"}機率+${p(val)}${d}` + sfx;
      case "critDmgUp": return `${e.dmgType === "intel" ? "奇謀" : "會心"}傷害+${p(val)}${d}` + sfx;
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
      case "block": { const bv = (e.scale && caster) ? Math.max(0, Math.min(1, capValOf((e.val ?? 1.0) * lockedScaleOf(caster, e), e.capVal))) : (e.val ?? 1.0); return `${bv >= 1 ? "抵禦" : `警戒(減傷${p(bv)})`}(${e.times ?? 1}次)` + sfx; }
      case "surehit": return `必中·無視規避${d}`;
      case "healblock": return `禁療·無法被治療${d || "(1回合)"}`;
      case "lifesteal": return `倒戈·造成傷害回復${p(val)}${d}` + sfx;
      case "immune": return `控制免疫〔${(e.types || []).join("、")}〕${d || "(1回合)"}`;
      case "healBoost": return `受治療效果${val >= 0 ? "+" : ""}${p(val)}${d}` + sfx;
      case "healGiven": return `施放治療效果${val >= 0 ? "+" : ""}${p(val)}${d}` + sfx;
      case "dispel": return `驅散〔${e.what === "buffs" ? "增益" : "減益"}〕`;
      case "fakeReport": return `偽報·被動指揮戰法失效${d || "(1回合)"}`;
      // rateup/chargeup 的 scale 用獨立的 rateScaleOf(非上面 amp/mitig/stat 共用的 scaleOf/SCALE),
      // 故不沿用外層算好的 val/sfx, 另外用 rateScaleOf 算實際值(批7; 批46 A: e.scaleDiv 透傳)。
      case "rateup": case "chargeup": {
        const rsfx = e.scale && caster ? `〔受${STAT_ZH[e.scale] || e.scale}影響〕` : "";
        const rv = e.scale && caster ? e.val * rateScaleOf(caster, e.scale, e.scaleDiv) : e.val;
        const label = k === "rateup" ? "主動戰法發動機率" : "突擊發動機率";
        return `${label}+${p(rv)}${d}${rsfx}`;
      }
      default: return k;
    }
  }
  // 批43 C: healed(反應式派發, on:"healed") —— 「(自身/我軍/敵軍)受到治療時」事件, 掛在
  // applyEffects() 的 k==="heal" 分支結算完成(hurt.troop 已回補)之後呼叫。與 onHit/dealtDamage/
  // activeFired(定義在 fight() 內部, 閉包用 A/B/alliesOf/foesOf 全局隊伍狀態)不同, applyEffects
  // 本身是模組層級函式(與 fight 同層, 見上方 hit()/applyEffects()/fight() 三者皆為頂層函式),
  // 無法直接看到 fight() 內的 onHit 等閉包 —— 但不需要: heal 效果的 hurt(受治療者)保證來自
  // 呼叫端傳入的 allies 陣列(見 heal 分支 for (const a of allies) 篩選 hurt 的既有邏輯), 故
  // 「hurt 的敵隊」天然就是同一次 applyEffects() 呼叫已持有的 enemies 參數, 不需要額外的
  // alliesOf/foesOf 全域查找。只支援效果級(onHealEffectTacs, 見 Unit 建構式), 不支援戰法級
  // (onHealTacs 陣列已建但本函式未讀取, 現無資料需要「整個戰法都是on:healed反應式」這種粒度,
  // 比照批31 activeFired precedent, 新事件類型不強制一次補齊所有粒度, 見 Unit 建構式該陣列
  // 註解), 也不支援 t.coef/extraHits 主傷害段(現無資料需要「受治療時對敵造成傷害」這種
  // 複合語意, 只有 stat/heal 等非傷害效果段, 故省略 hit() 呼叫路徑, 保持函式精簡)。
  function healedFor(hurt, caster, actual, allies, enemies) {
    if (actual <= 0) return;                        // 未實際回復(傷兵池已空/滿編)不觸發, 對應「受到治療」語意本身要求真的有治療發生
    // 候選持有者分四組, 各自對應wantWho比對值與正確的allies/enemies定向(遞迴呼叫applyEffects
    // 時必須以holder自身視角傳入, 而非一律沿用hurt的視角——ally/otherAlly組holder與hurt同隊,
    // 沿用原allies/enemies; enemy組holder是hurt的敵隊成員, 對它而言allies/enemies方向相反,
    // 需對調傳入, 否則該holder的戰法會把自己隊友誤判成敵人反之亦然):
    const groups = [
      { holders: [hurt], wantWho: undefined, al: allies, en: enemies },              // self: holder===hurt本人(未指定who或"self")
      { holders: allies, wantWho: "ally", al: allies, en: enemies },                 // ally: hurt同隊(含自己)
      { holders: allies.filter(a => a !== hurt), wantWho: "otherAlly", al: allies, en: enemies },  // otherAlly: hurt同隊, 排除自己
      { holders: enemies, wantWho: "enemy", al: enemies, en: allies },               // enemy: hurt的敵隊(對holder而言方向相反)
    ];
    for (const { holders, wantWho, al, en } of groups) {
      for (const holder of holders) {
        if (!holder.alive || !holder.onHealEffectTacs.length) continue;
        if (holder.fakeReportDur) continue;         // 批16: 偽報 —— 抑制反應式觸發, 同 onHit/dealtDamage/activeFired 慣例
        for (const t of holder.onHealEffectTacs) {
          for (const e of t.effects) {
            if (!e.when || e.when.on !== "healed") continue;
            // who:"self"(顯式寫出)與省略who欄位視為同義(對稱onHit/dealtDamage省略即self的既有
            // 慣例, 但healed是全新事件, 不像那些歷史資料已固定只用「省略」寫法——為了資料撰寫
            // 直覺(「自身受到治療時」寫who:"self"更明確易讀), 此處額外正規化"self"→undefined
            // 再比對, 零風險放寬(不影響ally/otherAlly/enemy三組的既有嚴格比對)。
            const eWho = e.when.who === "self" ? undefined : e.when.who;
            if ((eWho || undefined) !== wantWho) continue;
            if (!roundOk({ when: e.when }, CUR_R)) continue;
            if (holder.hitFlags.has(e)) continue;   // 同回合每單位每效果最多觸發1次(防無限鏈), 沿用 onHit/dealtDamage 共用的 hitFlags 慣例
            const evRate = e.rate ?? t.rate ?? 1;
            if (rnd() >= evRate) continue;
            holder.hitFlags.add(e);
            if (TRACE) lg(`【${holder.side}】${holder.nm} 戰法【${t.nameZh}】效果（受到治療觸發）發動`);
            applyEffects(holder, hurt, { effects: [e], kind: t.kind || "phys", nameZh: t.nameZh }, al, en, { rateChecked: true, reactive: true, healAmt: actual });
          }
        }
      }
    }
  }
  // 禁近似令-批L: onValues(e) —— 把 e.when.on 正規化成陣列(單一字串包成單元素陣列, 本已是
  // 陣列則原樣回傳, 無 on 回傳空陣列)。對稱既有 e.ifLeaderIs/e.eitherK/e.statOptions「單值
  // 或陣列皆可」慣例, 讓單一效果可同時掛兩個(或以上)反應式事件名共用同一份 stackKey 疊層
  // 計數(見 fireSelfReactive/selfReactEffectTacs), 一身是膽「每次免疫控制狀態後**或**每次
  // 累計受傷達門檻後」需要 dmgThreshold/ctrlImmune 兩個事件共用同一組「最多觸發7次」封頂,
  // 若各自獨立掛兩個效果物件, k==="critUp"+e.stackKey 的疊層計數器以效果物件本身(id(e))為
  // 鍵, 兩個不同物件會各自疊到7層(合計最多14層), 與本文「最多觸發7次」不符。此處只新增
  // on 值可為陣列的正規化, 不改動既有任何只認字串 on 值的既有比對式(如 onHitTacs 等既有
  // prefilter 仍用 === 比對, 不受影響, 因為它們過濾的 on 值集合(attacked/damaged/dealtDamage/
  // activeFired/healed/controlled)目前全庫沒有任何資料把 on 寫成陣列)。
  function onValues(e) {
    const on = e && e.when && e.when.on;
    if (on == null) return [];
    return Array.isArray(on) ? on : [on];
  }
  // 禁近似令-批L: bumpDmgAccum(u, amt) —— 累計u自身因傷害(含代承/反擊/dot/延遲結算等一切
  // 途徑)實際扣減的兵力量, 偵測本次增量是否使累計值跨越新的「最大兵力7%」門檻(可能一次跨越
  // 多格, 如單次巨量傷害), 回傳新跨越的格數(0=未跨越)。呼叫端(hit()/tick()/settleHuchen()/
  // fight()主迴圈settle結算)在各自「這個單位的troop因傷害而減少」的既有分支旁, 與this.wounded
  // 更新並列呼叫, 涵蓋範圍與wounded完全對稱(凡wounded有算的傷害來源, dmgAccum同步計入), 確保
  // 「自身累計受...傷害」是「這個單位自己實際承受的傷害總量」的忠實累加, 不遺漏任何結算路徑。
  function bumpDmgAccum(u, amt) {
    if (!u || !u.alive || !(amt > 0)) return 0;
    const thr = START_TROOP * 0.07;
    const before = u.dmgAccum || 0;
    u.dmgAccum = before + amt;
    return Math.floor(u.dmgAccum / thr) - Math.floor(before / thr);
  }
  // 禁近似令-批L: fireSelfReactive(u, onName, times) —— on:"dmgThreshold"/on:"ctrlImmune"
  // 專用的自身反應式派發(純自身視角, 不做跨隊broadcast——現無資料需要"ally"/"enemy"監聽這兩個
  // 新事件, 若未來有需要可仿fireControlled/onHitFor補上broadcastHolders廣播, 現維持最小可用
  // 形狀)。times: 本次事件應觸發幾次獨立判定(dmgThreshold單次巨量傷害可能一次跨越多格門檻,
  // 每格各自獨立擲骰; ctrlImmune恆為1)。每次呼叫用「合成單效果戰法」重新呼叫applyEffects,
  // 沿用其既有e.rate/e.rateLeader擲骰+k==="critUp"+e.stackKey疊層consumption, 不另造一套
  // 機率/疊層邏輯。
  function fireSelfReactive(u, onName, times) {
    if (!u || !u.alive || !(times > 0) || !u.selfReactEffectTacs || !u.selfReactEffectTacs.length) return;
    const allies = (_FIGHT_CTX.alliesOf && _FIGHT_CTX.alliesOf(u)) || [u];
    const foes = (_FIGHT_CTX.foesOf && _FIGHT_CTX.foesOf(u)) || [];
    for (let i = 0; i < times; i++) {
      for (const t of u.selfReactEffectTacs) {
        for (const e of t.effects) {
          if (!onValues(e).includes(onName)) continue;
          if (!roundOk({ when: e.when }, CUR_R)) continue;
          applyEffects(u, null, { effects: [e], kind: t.kind || "phys", nameZh: t.nameZh }, allies, foes, { reactive: true });
        }
      }
    }
  }
  // 禁近似令-批L: resolveMaxStack(caster, e, allies) —— 對稱既有coefLeader/rateLeader(基礎值
  // +主將時改用替代值)家族, 但套用維度是maxStack(疊層上限本身, 一個「封頂」整數, 不像coef/
  // rate是可累加的數值, 無法用base+topup相加手法表達「條件式提高上限」), 改用「符合條件則
  // 整個替換成另一個上限值」的覆寫式讀取。先登死士「可疊加4次;若麴義統領,則可疊加5次」——
  // e.maxStackIfLeaderIs:{who:"麴義"或陣列(OR), max:5} 於施放者(caster)恰為隊伍主將
  // (allies[0]===caster)且武將名匹配時, 用max覆蓋e.maxStack(4)。未帶e.maxStackIfLeaderIs
  // 或條件不成立時原樣回傳e.maxStack(向後相容既有全部stealStat/rateup資料)。
  function resolveMaxStack(caster, e, allies) {
    let ms = e.maxStack;
    if (e.maxStackIfLeaderIs) {
      const names = Array.isArray(e.maxStackIfLeaderIs.who) ? e.maxStackIfLeaderIs.who : [e.maxStackIfLeaderIs.who];
      if (allies && allies[0] === caster && caster.g && names.includes(caster.g.name)) ms = e.maxStackIfLeaderIs.max;
    }
    return ms;
  }
  // 批52h: 控制施加事件(機鑑先識反彈) —— onlySlower=速度慢於持有者的友軍才有
  function fireControlled(victim, kind, dur, allies, enemies) {
    if (!victim || !victim.alive || !["stun", "silence", "disarm", "chaos"].includes(kind)) return;
    let team, foes;
    if (allies && allies.includes(victim)) { team = allies; foes = enemies; }
    else if (enemies && enemies.includes(victim)) { team = enemies; foes = allies; }
    else return;
    for (const holder of team) {
      if (!holder.alive || !holder.onCtrlEffectTacs || !holder.onCtrlEffectTacs.length) continue;
      if (holder.fakeReportDur) continue;
      for (const t of holder.onCtrlEffectTacs) {
        for (const e of t.effects) {
          if (!e.when || e.when.on !== "controlled") continue;
          const who = e.when.who;
          if (who === "self" && holder !== victim) continue;
          if (who === "enemy") continue;
          if (!roundOk({ when: e.when }, CUR_R)) continue;
          if (e.ifLeaderIs) {
            const names = Array.isArray(e.ifLeaderIs) ? e.ifLeaderIs : [e.ifLeaderIs];
            if (!(team[0] === holder && holder.g && names.includes(holder.g.name))) continue;
          } else if (e.ifLeader && !(team[0] === holder)) continue;
          if (e.onlySlower && victim.eff("speed") >= holder.eff("speed")) continue;
          const flag = "ctrlReflect:" + (e._id || "") + ":" + (victim.nm || "");
          // use object identity via hitFlags Set with composite key object
          const flagKey = { k: "ctrlReflect", e, v: victim };
          // hitFlags is Set of mixed keys; use string key
          const fstr = "ctrlReflect|" + t.nameZh + "|" + victim.nm + "|" + kind;
          if (holder.hitFlags.has(fstr)) continue;
          if (rnd() >= (e.rate ?? 1)) continue;
          holder.hitFlags.add(fstr);
          const dests = pickTargets(foes, e.n || 1);
          if (!dests.length) continue;
          if (TRACE) lg(`【${holder.side}】${holder.nm}【${t.nameZh}】控制反彈 ${kind} → ${dests[0].nm}`);
          applyEffects(holder, dests[0], {
            effects: [{ k: kind, who: "enemy", n: 1, dur }],
            nameZh: t.nameZh, kind: t.kind || "intel",
          }, team, foes, { rateChecked: true, noCtrlReflect: true });
        }
      }
    }
  }
  function applyEffects(caster, tgt, t, allies, enemies, opt) {
    opt = opt || {};
    const src = t.nameZh || null;                     // 效果來源標籤: 戰法名(兵書/裝備/緣分無 nameZh → null, 不去重)
    // 批H: opt.onlyKinds(可選, 陣列) —— 限定本次只處理 k 在此清單內的效果段, 其餘一律跳過。
    // 唯一用途: active型戰法在主coef攻擊之前, 先套用施放者自身的 critUp/critDmgUp 會心buff
    // (只傳 onlyKinds:["critUp","critDmgUp"]), 讓「提高自身X%會心機率...隨後造成攻擊」這類
    // 戰法(百步穿楊/左右開弓)的主AoE本身也能吃到真會心擲骰(取代舊有把會心EV折入coef本身的
    // 近似)。因pushAdd以src(戰法名+dmgType尾碼)去重, 主coef段結束後的常規applyEffects呼叫會
    // 以同一src刷新覆蓋(非疊加)本效果, 故pre-coef先套一次+post-coef再刷新一次不會會心率翻倍。
    for (const e of t.effects) {
      // 禁近似令-批K: e.eitherK(dynamic_coef_from_counter族/target_rank_branch鄰居) —— 陣列,
      // 本次觸發隨機擇一k值頂替e.k本身(溯江搖櫓「使隨機敵軍單體進入計窮或震懾狀態」——本文
      // 明確是兩個控制狀態擇一觸發, 而非固定套用其中一種), 取代舊有「簡化為固定stun, 未表達
      // 計窮或震懾的擇一語意」近似。每次觸發各自重新擲骰(非prep鎖定, 反應式觸發本身就該每次
      // 獨立判定選中哪一種)。
      const k = e.eitherK ? e.eitherK[Math.floor(rnd() * e.eitherK.length)] : e.k;
      if (opt.onlyKinds && !opt.onlyKinds.includes(k)) continue;  // 批H: 限定只處理指定k(pre-coef會心套用, 見上方註解)
      // 批35 B: block 的「準備階段鎖定」scale 值優先算定, 放在所有 continue 閘門(healOnly/
      // skipWhenEffects/when.on/rate/ifLeader/everyRound...)之前 —— 必須確保 prep 呼叫
      // (fight() 開場的 applyPassives({prep:true, skipWhenEffects:true}))第一次掃描到
      // 帶 e.when(如機鑑先識 everyRound 段的 when:{until:3})的 block 效果時就把鎖算好,
      // 否則若鎖定邏輯放在 skipWhenEffects/everyRound 等後面的閘門之後, 帶 e.when 的
      // everyRound block 效果會在 prep 呼叫被 skipWhenEffects 閘門提前 continue 掉,
      // 導致 lockedScaleOf 從未在 prep 階段被呼叫過、鎖定值錯誤地延後到未來真正命中
      // 的那一回合才用當時(可能已變動)的即時智力算定, 违反「準備階段鎖定」語意本身。
      if (k === "block" && e.scale) lockedScaleOf(caster, e);
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
      // 批52续: e.rateLeader / rateSub —— 主將/副將時用不同觸發率
      let eRate = e.rate;
      if (e.rateLeader != null && allies && allies[0] === caster) eRate = e.rateLeader;
      if (e.rateSub != null && allies && allies[0] !== caster) eRate = e.rateSub;
      // 禁近似令-批K: rateFactionBonus(faction_count_scale族) —— 依隊伍陣營構成人數線性加成
      // 觸發率(南蠻渠魁/象兵「部隊每多一名蠻族武將額外提高X%機率」)。額外加成=per×max(0,
      // 隊伍中該陣營人數-1)(「每多一名」=超過持有者自己以外的同陣營人數), 見countAllyFaction()。
      if (e.rateFactionBonus && eRate != null) {
        const cnt = countAllyFaction(allies, e.rateFactionBonus.faction);
        eRate = Math.max(0, Math.min(1, eRate + (e.rateFactionBonus.per || 0) * Math.max(0, cnt - 1)));
      }
      // 禁近似令-批K: rateBonusPerBuffType(rate_self_dynamic族) —— 依自身當下持有的功能性
      // 增益「種類數」動態加成觸發率(臥薪嘗膽「依自身連擊/洞察/先攻/必中/破陣的狀態數,每多
      // 一種提高5%→10%機率」), 取代e.rate只能是靜態擲骰值的既有限制, 見countActiveBuffTypes()。
      if (e.rateBonusPerBuffType && eRate != null) {
        const cnt = countActiveBuffTypes(caster, e.rateBonusPerBuffType.types || []);
        eRate = Math.max(0, Math.min(1, eRate + (e.rateBonusPerBuffType.per || 0) * cnt));
      }
      // 批52g: ratePerTarget/rateStatusBonus —— 逐目標擲骰, 跳過全局一次 rate
      const perTgtRate = !!(e.ratePerTarget || e.rateStatusBonus);
      if (!opt.rateChecked && !perTgtRate && eRate != null && rnd() >= eRate) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔${Math.round(eRate * 100)}%機率〕未觸發`); continue; }
      // 批52g: ifCasterNames —— 施放者武將名須在名單(太平道法黃巾主將含 SP)
      if (e.ifCasterNames) {
        const cn = Array.isArray(e.ifCasterNames) ? e.ifCasterNames : [e.ifCasterNames];
        if (!(caster.g && cn.includes(caster.g.name))) continue;
      }
      // 批26: e.ifLeader —— 效果級「施放者須為隊伍主將(index 0)」條件閘門。原文常見「自身為
      // 主將時，額外XX」措辭(南蠻渠魁/江東小霸王/酒池肉林等), 過去無對應原語, 該效果段只能
      // 被迫「無條件對所有施放者套用」(高估非主將情形)或完全不建模。allies[0] 是隊伍主將慣例
      // (同 who==="leader" 分支既有假設), 只在 caster 就是 allies[0] 時才放行本效果段。
      if (e.ifLeader && !(allies && allies[0] === caster)) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔限主將〕${caster.nm}非主將, 未觸發`); continue; }
      // 批52: e.ifSub —— 施放者須為副將; e.ifGender —— Male/Female
      if (e.ifSub && (!allies || allies[0] === caster)) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔限副將〕${caster.nm}為主將, 未觸發`); continue; }
      if (e.ifGender) {
        const gmap = { "男": "Male", "女": "Female", male: "Male", female: "Female", Male: "Male", Female: "Female" };
        const want = gmap[e.ifGender] || e.ifGender;
        const got = gmap[(caster.g && caster.g.gender) || ""] || ((caster.g && caster.g.gender) || "");
        if (got !== want) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔限${want}〕未觸發`); continue; }
      }
      // 批44 A: e.ifLeaderIs —— 效果級「隊伍主將(allies[0])的武將名須匹配指定值」條件閘門,
      // 對稱既有 e.ifLeader(布林, 只判斷「是否為主將」)。原文常見TROOP兵種戰法「若XX統領,
      // 數值提升/額外效果」措辭(白毦兵/丹陽兵/先登死士/藤甲兵/西涼鐵騎/白馬義從等8筆家族,
      // 見 engine_limitations.md), 過去只有「是否為主將」(ifLeader)與「chargeup專屬曹純
      // 力²硬編碼」(leaderBonus)兩種機制, 皆無法表達「主將須為特定武將(可代入任意人選)」這種
      // 通用條件。判斷式與 ifLeader 相同(allies[0]===caster), 額外比對 allies[0].g.name===
      // e.ifLeaderIs(指定武將的中文名, 與 tactics_parsed.json _todo 內文一致, 如"陳到")。也接受
      // 陣列(如虎衛軍「若典韋或許褚統領」, OR 語意: 名字在陣列內任一即符合)。與 ifLeader 是不同
      // 的判斷(ifLeaderIs 蘊含 ifLeader, 但額外要求身份匹配), 兩者不會同時出現在同一效果上,
      // 若同時存在則兩個條件皆需滿足(允許但無實際資料使用此組合)。
      if (e.ifLeaderIs) { const names = Array.isArray(e.ifLeaderIs) ? e.ifLeaderIs : [e.ifLeaderIs]; if (!(allies && allies[0] === caster && caster.g && names.includes(caster.g.name))) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔限${names.join("/")}統領〕${caster.nm}非${names.join("/")}或非主將, 未觸發`); continue; } }
      // 批43 B: e.ifStackMaxed —— 效果級「施放者自身的 k==="stack" 疊層(見 u.stack, 批26既有
      // 「每次發動/普攻+1層增傷」原語)已疊滿(caster.stack.n>=caster.stack.max)」條件閘門。
      // 原文族「疊加N次後, 使我軍全體減傷X%, 持續2回合」(如長驅直入「疊加5次後...降低16%...
      // 持續2回合」)過去只能在 prep 一次性套用 mitig(整場恆定, 與「疊滿才生效」的後半場窗口
      // 完全錯位, 見批43 B前的既有_approx近似)。搭配 e.everyRound(逐回合重新判定, 見下方)
      // 即可精確表達「每回合檢查一次, 疊滿才觸發, 未疊滿則本回合不生效」, 使 mitig 真正延後到
      // caster.stack.n 首次達到 max 的那個回合才開始生效(而非prep就套用整場)。與 k==="stack"
      // 本身(掛在 caster/holder 身上累計自身層數, 不像 exploitLayers 是掛在受害目標身上的
      // 疊層機制)是不同的計數器, 兩者不衝突——ifStackMaxed 只是讀取既有 stack.n/stack.max
      // 狀態做條件判斷, 不修改/不新增計數邏輯本身, 成本低(對比批42 exploitLayers/批43 A
      // add型疊層需要新增整套計數/封頂/onMaxStacks原語, 本欄位只是既有stack狀態的讀取閘門)。
      if (e.ifStackMaxed && !(caster.stack && caster.stack.n >= caster.stack.max)) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔限疊層已滿〕${caster.nm}尚未疊滿(${caster.stack ? caster.stack.n : 0}/${caster.stack ? caster.stack.max : "?"}), 未觸發`); continue; }
      // 禁近似令-批K: e.ifCasterStackAtLeast(數值) —— 對稱既有 e.ifStackMaxed(僅認「已疊滿」
      // 這個特例), 這裡是通用門檻「caster.stack.n 是否達到指定層數」(水淹七軍「第三次及之後
      // 施放」= stack.n>=2 才觸發settle式即時結算/「第四次施放後」= stack.n>=3 才觸發
      // extraHits, hit_count_stage_trigger族——stack.n 本身已由 stackPer:"cast" 於每次成功
      // 發動時遞增, 只是過去無「讀取層數作為另一段效果觸發條件」的原語)。
      if (e.ifCasterStackAtLeast != null && !(caster.stack && caster.stack.n >= e.ifCasterStackAtLeast)) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔限疊層達${e.ifCasterStackAtLeast}〕${caster.nm}僅${caster.stack ? caster.stack.n : 0}層, 未觸發`); continue; }
      // 禁近似令-批K: e.ifEnemyTroop(兵種字串, "騎"/"盾"/"弓"/"槍"/"器") —— 兵種由「隊伍」
      // 決定(非個別武將), enemies[0].ttype 即代表整支敵隊的兵種, 只在敵隊兵種恰好符合指定
      // 值時本效果才生效(左右開弓「如果目標為騎兵則額外造成潰逃狀態」, engine_wiring_gaps_misc
      // 族「依隊伍兵種類型」分支, 過去引擎完全無法區分兵種, 只能對全體目標近似套用)。
      if (e.ifEnemyTroop && !(enemies && enemies.length && enemies[0].ttype === e.ifEnemyTroop)) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔限敵隊為${e.ifEnemyTroop}兵〕敵隊為${enemies && enemies[0] ? enemies[0].ttype : "?"}, 未觸發`); continue; }
      // 禁近似令-批K: e.once(通用版) —— 對稱既有 everyRound/onHit 等個別路徑各自的 e.once
      // 檢查(見 onHitFor 等), 這裡補上「不論從哪條路徑呼叫都成立」的通用一次性消耗閘門, 用
      // caster.whenFired(不隨回合重置的持久化去重狀態)以效果物件本身為鍵。淵然難測「首回合
      // 觸發時, 若...否則...」的兩個互斥分支各自只應觸發一次(不論母戰法是走反應式戰法級或
      // 效果級路徑)。
      if (e.once && caster.whenFired.has(e)) { continue; }
      if (e.once) caster.whenFired.add(e);
      // 禁近似令-批K: e.ifArmed(once_consumable族, k:"armConsume"/"strike"配對) —— 「消耗態
      // 狀態機」通用旗標門檻: 只有 caster.armedConsume.active 為真才放行(見k==="strike"消費端
      // 與k==="armConsume"施放端)。十二奇策「並使其下次發動主動戰法後,對敵軍單體造成謀略
      // 攻擊」——armConsume(who:self, 十二奇策成功發動當下套用)武裝一次性資格, strike(掛在
      // e.when.on:"activeFired",who:"ally", 監聽包含自己在內的我軍任一人下次成功發動主動
      // 戰法)命中時消耗掉這份資格並造成傷害, 不消耗則不觸發(未被武裝時視為條件不成立)。
      if (e.ifArmed && !(caster.armedConsume && caster.armedConsume.active)) { if (TRACE) lg(`　▸ ${effDesc(k, e, caster)}〔待消耗武裝〕${caster.nm}尚未取得可消耗的觸發資格, 未觸發`); continue; }
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
        // 批35 B: block 的「準備階段鎖定」scale 值已在函式最頂端(所有 continue 閘門之前)
        // 算定, 此處不需重複呼叫 lockedScaleOf(見上方新增的閘門與其註解)。
        if (!opt.healOnly) continue;
        const hw = e.when || t.when;
        if (hw) {
          if (!roundOk({ when: hw }, CUR_R)) continue;
          // 批A(11筆高嚴重重建): e.when.hpBelow/hpAbove(效果級) —— 過去 hpBelow/hpAbove 只在
          // 戰法級(t.when, 見 fight() 主迴圈 754 行後段的獨立 hpOk 判斷)受理, everyRound 通道
          // 的 hw(可能是 e.when 也可能 fallback 到 t.when)只走 roundOk, 從不檢查 hp。奇兵間道
          // 「第5回合時, 若自身兵力低於50%...否則...」這類「同一戰法內, 某些effects段是常駐
          // buff(前4回合amp), 另一些段要依動態兵力%分流」的複合語意, 過去因「戰法級t.when會
          // 連帶鎖住其餘不需要hp條件的effects段」而卡住, 只能退回EV折算近似(見奇兵間道舊
          // _note)。現在 hpBelow/hpAbove 也認 e.when(效果自身), 不強制整條戰法共用同一個
          // when, 讓「同戰法內部分effects段各自獨立hp條件」成為可能。hpOk(t,u) 讀 t.when, 這裡
          // 用合成 {when:hw} 呼叫沿用同一份函式(hpOk 不管傳入的 t 是真戰法還是合成物件)。
          if (hw.hpBelow != null || hw.hpAbove != null) { if (!hpOk({ when: hw }, caster)) continue; }
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
      } else if (e.when && k !== "heal" && !e.everyRound) {
        // 批32 R23: e.when(非heal/非everyRound效果) 的回合窗口檢查 —— 過去只有「母戰法無
        // t.when 時, opt.skipWhenEffects=true 的 prep 呼叫會跳過此效果(留給 fight() 回合
        // 迴圈通用掃描處理, 見上方754行)」這一種路徑會尊重 e.when; 其餘直接呼叫
        // applyEffects() 的路徑(尤其 active 型戰法擲骰命中後, fight() 主迴圈直接呼叫)完全
        // 不檢查 e.when, 導致「奇數回合...偶數回合...」這類需要用 e.when.parity 切分同一
        // 戰法內兩組互斥效果的 active 戰法(飛沙走石), 即使補了 e.when.parity 也會被無條件
        // 套用(奇偶兩組效果同時生效, 塌縮成常駐雙倍輸出, 即R23要抓的缺口本身)。此處補上
        // 通用檢查: 任何非heal/非everyRound效果只要帶 e.when, 就先驗證當前回合是否落在窗口
        // 內, 不符合則跳過該效果段。
        if (!roundOk({ when: e.when }, CUR_R)) continue;
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
        //   以上都通過後, 若 e.rate 缺席才擲 t.rate 骰(rate<1 時只有部分回合真正治療, 而非
        //   年年必中)。
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
          // 批G: t.rate 僅在 e.rate 缺席時才擲骰 —— 過去此處無條件讀 t.rate, 但 e.rate 本身
          // 早已被上方「批23 A4: 效果級 e.rate 折算一致性」通用閘門(函式開頭, 對所有 k 統一
          // 處理, 見 1246 行)擲骰判定過一次, 若這裡帶 e.rate 又重複讀 e.rate 骰一次會使機率
          // 被平方(0.1×0.1≈0.01, 而非期望的0.1)。修正: e.rate 存在時, 通用閘門已完整處理該
          // 效果本回合是否觸發, 這裡不再二次擲骰(直接放行); 只有 e.rate 缺席(該效果未自帶
          // 機率)時才退回擲 t.rate(戰法整體觸發率), 使「奇數回合X%機率/偶數回合Y%機率」這類
          // 同一戰法內 heal 自身機率隨 parity 變動的語意可用 e.rate 精確表達(錦囊妙計: 奇數
          // 32%/偶數75%), 同時不影響既有僅帶 t.rate 的 heal 資料(向後相容零回歸)。
          if (e.rate == null && rnd() >= (t.rate ?? 1)) continue;
        }
        // 批52: heal 選標對齊原文 —— 過去一律「我方最殘一人」, 忽略 who/e.n/targetSel,
        // 導致「恢復自身」「治療我軍主將」「我軍群體2人/全體」全部失真(engine_limitations #1)。
        // 現與 amp/mitig 等效果共用 who/n/nMax/targetSel 語意:
        //   who:self → 施放者; who:leader → 我方主將; who:subs → 副將全體;
        //   who:eventTarget → 反應式事件單位(急救/ofDamage 類);
        //   targetSel → 依準則挑 1 人; e.n(/nMax) → 隨機挑 N 名可治療友軍;
        //   預設(who=ally 且無 n) → 維持舊行為「最殘 1 人」(單體向後相容)。
        // 禁療(healblock) 者一律不進可治療池; 批F: 補captured過濾(對稱sgz.py既有行為, 被捕獲
        // 單位不應被選為heal目標, 過去engine.js此處遺漏, 屬雙引擎同步缺口, 隨heal選標改造一併補上)。
        const whoH = e.who || "ally";
        const pool = allies.filter(a => a.alive && !a.healblock && !a.captured);
        let hurts = [];
        if (e.targetSel) {
          const picked = pickByCriterion(pool, e.targetSel);
          hurts = picked ? [picked] : [];
        } else if (whoH === "self") {
          hurts = (caster.alive && !caster.healblock) ? [caster] : [];
        } else if (whoH === "leader") {
          hurts = (allies[0] && allies[0].alive && !allies[0].healblock) ? [allies[0]] : [];
        } else if (whoH === "subs") {
          hurts = allies.slice(1).filter(a => a.alive && !a.healblock);
        } else if (whoH === "eventTarget") {
          const et = opt.evtTarget;
          hurts = (et && et.alive && !et.healblock) ? [et] : [];
        } else if (e.all) {
          // 批F: e.all(新原語) —— 「我軍全體」精確表達, 對稱amp/mitig/stat等效果種類既有
          // 「who:ally且無n → 全體」通用慣例(見上方dests的預設分支 `dests = allies.filter(...)`)。
          // heal過去無此路徑, 無n時一律落到下方「預設(單體, min troop)」分支, 導致「我軍全體」
          // 語意的戰法(如金丹秘術「我軍全體獲得...休整狀態」)被誤治成全軍僅1人, 漏治其餘友軍。
          hurts = pool.slice();
        } else if (e.n != null) {
          const n = e.n;
          const cnt = e.nMax != null ? n + Math.floor(rnd() * (e.nMax - n + 1)) : n;
          // preferLowest/sharedPool: 優先兵力最低的 N 人; 否則隨機 N 人(群體治療通例)
          if (e.preferLowest || e.sharedPool) hurts = pool.slice().sort((a, b) => a.troop - b.troop).slice(0, cnt);
          else hurts = pickTargets(pool, cnt);
        } else {
          // 批F: 此分支為「單體, 無who/n/targetSel明示」的最終後備 —— 過去(批52前)是全域唯一
          // 行為(全庫heal一律套用), 現僅限「本文確實只描述我軍單體, 且未指定特定選標準則(如
          // 兵力最低/損失最多)」的戰法才會落到這裡, 語意應是「隨機挑1人」而非「固定選最殘」。
          // 批F資料全掃已將全庫「本文明示兵力最低/損失最多」的heal效果都改掛顯式targetSel、
          // 「本文明示群體N人」的都改掛e.n、「本文明示隨機/單體無準則」的都改掛e.n:1、反應式
          // 急救類都改掛who:eventTarget —— 理論上不應再有戰法會落到此分支(全庫掃描後仍保留
          // 此行為僅作最終防禦性後備, 避免未來新戰法資料一時漏標時直接治療對象變成空陣列)。
          // 維持既有min-troop實作(非改隨機)是刻意選擇: 此為向後相容的安全後備值, 不代表「預設
          // 補最殘」是被允許的全域慣例(那條慣例已於批F移除, 全庫戰法皆改顯式選標, 見上方各
          // 分支), 只是「萬一資料遺漏時」的保守後備、而非常態路徑。
          let hurt0 = null;
          for (const a of pool) if (!hurt0 || a.troop < hurt0.troop) hurt0 = a;
          hurts = hurt0 ? [hurt0] : [];
        }
        const ofDamageScaleMult = e.ofDamage != null ? (e.scale ? lockedScaleOf(caster, e) : 1) : 1;
        // 批52: scaleIfSub / scaleIfLeader —— 僅副將/主將套用 scale
        let scaleOk = true;
        if (e.scaleIfSub) scaleOk = !!(allies && allies[0] !== caster);
        if (e.scaleIfLeader) scaleOk = !!(allies && allies[0] === caster);
        const hcoef = (e.coef ?? 0.8) * (e.scale && e.ofDamage == null && scaleOk ? scaleOf(caster, e.scale) : 1);
        const healTroopBase = t.type === "active" ? caster.troop * HEAL_TROOP_C : caster.healBase;
        // 批A(11筆高嚴重重建): e.ofDamage 原本只讀 opt.dmg(傷害比例治療, 批33), on:"healed"
        // 反應式(批43 C)呼叫 healedFor() 時傳的是 opt.healAmt(本次觸發事件的實際治療量), 從未
        // 被此處讀取——ofDamage 的欄位語意其實已是「本次觸發事件的量」的通用比例治療(docstring
        // 早已這樣描述, 只是實作只接上了dmg一種事件來源), 這裡補上 opt.healAmt 分支(結盟「目標
        // 受到治療效果時,自身有機率獲得相同(治療)效果(治療效果為50%)」的鏡像治療, 見結盟落地)。
        // dmg 優先於 healAmt(兩者不會同時非null, 因 dealtDamage/onHit 與 healed 是互斥事件)。
        const ofEventAmt = opt.dmg != null ? opt.dmg : opt.healAmt;
        const poolWant = (e.ofDamage != null && ofEventAmt != null) ? e.ofDamage * ofDamageScaleMult * ofEventAmt : hcoef * healTroopBase;
        const shared = !!e.sharedPool;
        let remain = shared ? poolWant : null;
        for (const hurt of hurts) {
          if (!hurt) continue;
          const before = hurt.troop;
          const boostMult = Math.max(0, 1 + hurt.addbonus("healBoost")) * Math.max(0, 1 + caster.addbonus("healGiven"));
          if (shared && (remain == null || remain <= 0)) break;
          const want = shared ? remain * boostMult : poolWant * boostMult;
          const actual = Math.max(0, Math.min(want, hurt.wounded, START_TROOP - hurt.troop));
          hurt.troop += actual; hurt.wounded -= actual;
          if (shared) remain -= boostMult > 0 ? actual / boostMult : actual;
          if (TRACE && hurt.troop - before >= 1) lg(`　▸ 治療 ${hurt.nm} +${Math.round(hurt.troop - before)}(傷兵池餘${Math.round(hurt.wounded)})` + (e.ofDamage != null && ofEventAmt != null ? `（${opt.dmg != null ? "傷害" : "治療"}量比例治療×${(e.ofDamage * ofDamageScaleMult * 100).toFixed(1)}%）` : (e.scale ? `（受${STAT_ZH[e.scale] || e.scale}影響, 實際治療率${Math.round(hcoef * 100)}%）` : "")) + (boostMult !== 1 ? `（治療加成×${boostMult.toFixed(2)}）` : ""));
          healedFor(hurt, caster, hurt.troop - before, allies, enemies);
        }
        continue;
      }
      // 禁近似令-批K: k==="regen"(engine_wiring_gaps_misc族) —— 「每回合恢復一次兵力,持續N
      // 回合」的休整類狀態, 登記到目標的this.regens清單(見tick()消費端逐回合各自結算), 取代
      // 「heal效果不讀dur, 只結算一次, 折算成單次coef×dur近似(2倍低估)」的既有缺口。coef/
      // scale/healTroopBase公式與heal effects完全同款(僅治療對象選標簡化為self/leader/
      // targetSel/預設全體, 本戰法族群通常只需單體, 無heal完整who矩陣的必要)。
      if (k === "regen") {
        const whoR = e.who || "ally";
        const poolR = allies.filter(a => a.alive && !a.healblock && !a.captured);
        let targetsR;
        if (e.targetSel) { const picked = pickByCriterion(poolR, e.targetSel); targetsR = picked ? [picked] : []; }
        else if (whoR === "self") targetsR = (caster.alive && !caster.healblock) ? [caster] : [];
        else if (whoR === "leader") targetsR = (allies[0] && allies[0].alive && !allies[0].healblock) ? [allies[0]] : [];
        else targetsR = poolR.slice();
        const hcoefR = (e.coef ?? 0.8) * (e.scale ? scaleOf(caster, e.scale) : 1);
        const healTroopBaseR = t.type === "active" ? caster.troop * HEAL_TROOP_C : caster.healBase;
        const amtR = hcoefR * healTroopBaseR;
        for (const v of targetsR) { v.regens.push([amtR, e.dur ?? 2]); if (TRACE) lg(`　▸ ${v.nm} 獲得休整(每回合恢復${Math.round(amtR)}, 持續${e.dur ?? 2}回合)`); }
        continue;
      }
      if (k === "settle") {
        let tg = null;
        // 禁近似令-批K: e.perStackFrom(dynamic_coef_from_counter族) —— 選標改為「敵軍中該
        // stackId疊層數最高者」(對應「最終降傷施加次數」——被施加最多次的那個目標), 取代
        // 預設的「統率最高」選標(密計誅逆settle結算的目標必須與另一段amp-stackKey疊層的
        // 目標一致, 而非泛用統率最高)。
        if (e.perStackFrom) {
          for (const x of enemies) if (x.alive) {
            const lv = (x.ampLayersById && x.ampLayersById[e.perStackFrom]) || 0;
            const bestLv = tg ? ((tg.ampLayersById && tg.ampLayersById[e.perStackFrom]) || 0) : -1;
            if (lv > bestLv) tg = x;
          }
        } else {
          for (const x of enemies) if (x.alive && (!tg || x.eff("command") > tg.eff("command"))) tg = x;
        }
        if (tg) {
          tg.settle = {
            layers: e.init ?? 1, max: e.max ?? 3, left: e.dur ?? 2, caster, snap: caster.troop,
            base: e.base ?? 1.5, per: e.per ?? 0.4, kind: t.kind || "intel",
            perStackFrom: e.perStackFrom || null,
            // 禁近似令-批K: e.singleTarget(true) —— 結算只打tg本人(密計誅逆「對敵軍單體造成
            // 一次斬殺傷害」), 省略時維持既有行為(打tg所在整隊, 猛毒既有慣例)。
            singleTarget: !!e.singleTarget,
          };
          if (TRACE) lg(`　▸ 猛毒·結算傷害 → ${tg.nm}`);
        }
        continue;
      }
      if (k === "redirect") {
        // 批J(禁近似令-transfer轉移族): e.guardFor==="leader" —— 「單次全額代承」模式(古之惡來
        // 「我軍主將即將受到普攻時...隨後為我軍主將承擔此次普通攻擊」), 對稱既有 counter 的
        // guardFor:"leader"(守護式反擊), 但這裡是「代為承受」而非「代為反擊」。不走下方常駐
        // guardian(%分擔每一下直到guardDur到期)的路徑, 改登記進 allies[0].absorbGuards, 由
        // hit() 在主將受普攻時只轉移「這一下」的傷害(不影響後續攻擊), 每回合限觸發1次(見
        // hit() 內 absorbGuards 節流)。與 counterGuards 是兩份獨立清單, 可並存(同一次guardFor
        // 觸發時兩者互不干擾, 各自的 hitFlags 節流鍵不同)。
        if (e.guardFor === "leader") {
          if (allies.length && allies[0].alive) allies[0].absorbGuards.push({ unit: caster, share: e.share ?? 1.0, prob: e.prob ?? 1 });
          continue;
        }
        let guard = caster;
        if (e.guard === "max_force") { for (const a of allies) if (a.alive && (guard === caster || a.eff("force") > guard.eff("force"))) guard = a; }
        // 批J: e.guard==="random_sub" —— 代承者=隨機一位「當下存活」的非主將副將(夢中弒臣
        // 「如果自己為主將，則使隨機副將為自己分擔20%→40%傷害」), 與既有 max_force(取武力
        // 最高) 同層級但改採均勻隨機。若無存活副將(全滅或本隊僅1人), guard 落回 caster 本身
        // ——下方 `a !== guard` 判斷會使 recipients(=[caster], 因 who:"leader" 時 caster 即
        // allies[0])被排除, 天然等同「找不到可轉嫁對象則不轉嫁」(不無中生有), 而非另尋他法
        // 硬湊一個轉嫁對象。此隨機挑選在效果套用當下(戰鬥前2回合首次生效時)決定一次, 之後
        // 隨 guardDur 持續固定, 不逐回合/逐次攻擊重新抽選(與既有 max_force 挑選時機一致)。
        else if (e.guard === "random_sub") {
          const subs = allies.filter(a => a.alive && a !== allies[0]);
          guard = subs.length ? subs[Math.floor(rnd() * subs.length)] : caster;
        }
        // 批G: who 分流(leader/subs) —— 過去無條件對「除guard外的全體allies」套用同一share,
        // 不像其他k類型已支援who:leader(僅index0主將)/who:subs(index0以外副將)分流, 導致
        // 「為副將分擔30%/為主將分擔60%」這類依受益者身份給不同share值的戰法(肉身鐵壁)只能
        // 合併成單一均值近似。省略who(或who:"ally", 向後相容既有全部資料)時維持原行為。
        let recipients;
        if (e.who === "leader") recipients = (allies[0] && allies[0].alive) ? [allies[0]] : [];
        else if (e.who === "subs") recipients = allies.slice(1).filter(a => a.alive);
        else recipients = allies;
        for (const a of recipients) if (a.alive && a !== guard && !a.captured) { a.guardian = guard; a.guardShare = e.share ?? 0.3; a.guardDur = e.dur ?? 99; a.guardNormalOnly = !!e.normalOnly; }  // 讀 e.dur(預設99=近似全程, 向後相容) + e.normalOnly(只代承普攻); 到期由 tick 清除
        if (TRACE) lg(`　▸ ${guard.nm} 代承友軍傷害(分擔${Math.round((e.share ?? 0.3) * 100)}%${e.dur && e.dur < 90 ? `, ${e.dur}回合` : ""})`);
        continue;
      }
      // 批J(禁近似令-transfer轉移族): stealStat —— 偷屬性原語(雁行陣「使我軍統率最低單體
      // 偷取敵軍全體10點統率」)。核心約束: 不能無中生有——從每個victim實際扣除
      // min(欲偷量, victim現有可扣量(=其當下effective值, 不得扣至負數)), 施放者/受益者
      // 只獲得「所有victim實際被扣除量」的加總(而非固定填e.amount, 若victim現有量不足10點
      // 就只能偷到那麼多)。與既有 k:"stat" 的差異: k:"stat" 是無條件疊加, 不檢查/不連動另一方;
      // stealStat 是「一方扣多少, 另一方就恰好收多少」的成對操作, 且扣除量會先被victim現有值
      // 封頂。recipientSel(targetSel準則字串, 見TARGETSEL_KEY)從allies挑受益者, 省略時預設
      // caster本身。
      if (k === "stealStat") {
        // 禁近似令-批K: e.statOptions(陣列) —— 「任一屬性(隨機)」語意(至柔動剛「偷取來源智/
        // 統/速任一屬性」), 每次觸發隨機從陣列選一個屬性欄位, 取代固定只認e.stat單一屬性的
        // 既有近似(過去只能挑一個代表屬性, 現精確表達三選一隨機)。
        const statField = e.statOptions ? e.statOptions[Math.floor(rnd() * e.statOptions.length)] : e.stat;
        const wantEach = (e.amount ?? 0) * (e.scale ? scaleOf(caster, e.scale, e.scaleDiv) : 1);
        const recipient = e.recipientSel ? pickByCriterion(allies, e.recipientSel) : caster;
        if (recipient && recipient.alive && wantEach > 0) {
          // 禁近似令-批L: e.victimIsTgt —— 受害者精確鎖定「本次反應式事件的另一方」(tgt, 本函式
          // 第2參數, 於onHit()反應式呼叫時=攻擊者src), 對稱既有who==="eventTarget"精確選標
          // 精神但走stealStat自己的early-return targeting(在general dests/who解析區塊之前
          // 就continue掉, 不經過那條pipeline), 故不能複用opt.evtTarget(那是給victim/dst本身
          // 用的, 見onHitFor的evtTarget:dst)。先登死士「偷取其[攻擊者]10.5→21點統率」需要
          // 精確鎖定攻擊者本人, 而非既有victimPool(enemies全體)。
          let victimPool = e.victimIsTgt ? (tgt && tgt.alive ? [tgt] : []) : (e.who === "ally" ? allies : enemies).filter(x => x.alive);
          // 禁近似令-批L: e.ifStatCompare —— stealStat有自己的targeting早退路徑(不經過通用
          // dests區塊的既有ifStatCompare過濾, 見該區塊「if (e.ifStatCompare) dests = ...」),
          // 故在此局部重新套用同一個statCompareOk()比較, 語意與通用路徑完全一致(ref=caster,
          // target=victim逐一比對)。先登死士「若兵力百分比低於攻擊者」= stat:"hpPct",op:"lt"。
          if (e.ifStatCompare) victimPool = victimPool.filter(v => statCompareOk(caster, v, allies, e.ifStatCompare));
          // 禁近似令-批L: resolveMaxStack —— 「可疊加4次;若麴義統領則可疊加5次」, 見其定義註解。
          const ms = resolveMaxStack(caster, e, allies);
          // 禁近似令-批L: maxStack封頂時「雙方都不記帳」——先檢查受益者這一側是否已達上限,
          // 若已封頂則整次偷取視為no-op(僅刷新雙方既有同src疊層的dur, 不再產生新的扣/收記錄),
          // 避免「受害者被扣但受益者因push_stat_add內部封頂靜默no-op收不到」的無中生有bug
          // (pushStatAdd達max_stack時只refresh dur、不新增條目, 若這裡不預先檢查, victim那側
          // 仍會被扣掉stat卻沒有對應的recipient收益, 違反stealStat「一方扣多少另一方就恰好收
          // 多少」的核心設計約束)。
          if (ms != null) {
            const already = recipient.statAdds.filter(a => a[0] === statField && a[3] === src).length;
            if (already >= ms) {
              for (const a of recipient.statAdds) if (a[0] === statField && a[3] === src) a[2] = Math.max(a[2], e.dur ?? 1);
              for (const v of victimPool) for (const a of v.statAdds) if (a[0] === statField && a[3] === src) a[2] = Math.max(a[2], e.dur ?? 1);
              continue;
            }
          }
          let total = 0;
          for (const v of victimPool) {
            const avail = Math.max(0, v.eff(statField));
            const actual = Math.min(wantEach, avail);
            if (actual > 0) { v.pushStatAdd(statField, -actual, e.dur ?? 1, src, undefined, ms); total += actual; }
          }
          if (total > 0) {
            recipient.pushStatAdd(statField, total, e.dur ?? 1, src, undefined, ms);
            if (TRACE) lg(`　▸ ${recipient.nm} 偷取${STAT_ZH[statField] || statField} +${total.toFixed(1)}(來源實際扣除量之和, 不無中生有)`);
          }
        }
        continue;
      }
      // 批J: transferMitig —— 把「敵方(或指定來源側)當下實際持有的正向mitig(傷害降低)buff
      // 實例」整個搬到我方(或指定去向側)隨機一人身上(雁行陣「轉移傷害降低: 將敵軍隨機武將的
      // 傷害降低效果轉移至我軍隨機武將」)。若來源側當下沒有任何人持有這樣的buff, 不觸發(轉移
      // 0, 不無中生有, 不得無來源憑空生出一份mitig buff給接收方)。轉移=移動(從來源陣列真的
      // splice移除該實例)而非複製, val照抄來源實例原值, dur改用e.dur(對應原文「持續1回合」,
      // 非沿用來源剩餘時長)。
      if (k === "transferMitig") {
        const fromPool = (e.from === "ally" ? allies : enemies).filter(x => x.alive);
        const toPool = (e.to === "ally" ? allies : enemies).filter(x => x.alive);
        const candidates = [];
        for (const u of fromPool) for (const a of u.adds) if (a[0] === "mitig" && a[1] > 0) candidates.push({ unit: u, entry: a });
        if (candidates.length && toPool.length) {
          const pick = candidates[Math.floor(rnd() * candidates.length)];
          const dest = toPool[Math.floor(rnd() * toPool.length)];
          pick.unit.adds.splice(pick.unit.adds.indexOf(pick.entry), 1);
          dest.pushAdd("mitig", pick.entry[1], e.dur ?? 1, src);
          if (TRACE) lg(`　▸ 轉移傷害降低: ${pick.unit.nm} → ${dest.nm}(減傷${Math.round(pick.entry[1] * 100)}%)`);
        }
        continue;
      }
      // 批J: transferDebuff —— 把「我方(或指定來源側)群體當下實際持有的負面狀態」隨機挑
      // e.n~e.nMax種「不同種類」(而非同種類的多個實例)整個搬到敵方(或指定去向側)隨機單位身上
      // (雁行陣「轉移負面狀態: 將友軍群體隨機1-2種負面狀態轉移至隨機敵軍」)。與現有dispelUnit
      // 共用同一套「什麼算負面狀態」分類(負值amp/mitig、mult<1的mods、負值statAdds、dot、
      // stun/silence/disarm/chaos/healblock/fakeReport/ambush/huchen), 確保口徑一致不新開
      // 一套分類標準。若來源側當下完全沒有負面狀態, 轉移0種(不無中生有); 若只有1種可轉移即使
      // e.nMax要求2種也只轉移現有的那1種(轉移量=來源實際擁有量, 不硬湊到位)。
      if (k === "transferDebuff") {
        const fromPool = (e.from === "enemy" ? enemies : allies).filter(x => x.alive);
        const toPool = (e.to === "enemy" ? enemies : allies).filter(x => x.alive);
        const tokens = collectDebuffTokens(fromPool);
        if (tokens.length && toPool.length) {
          const kinds = [...new Set(tokens.map(x => x.kind))];
          const wantN = e.nMax != null ? (e.n ?? 1) + Math.floor(rnd() * (e.nMax - (e.n ?? 1) + 1)) : (e.n ?? 1);
          const chosenKinds = pickN(kinds, Math.min(wantN, kinds.length));
          for (const kd of chosenKinds) {
            const matches = tokens.filter(x => x.kind === kd);
            const tok = matches[Math.floor(rnd() * matches.length)];
            const dest = toPool[Math.floor(rnd() * toPool.length)];
            tok.move(dest, e.dur ?? 1);
            if (TRACE) lg(`　▸ 轉移負面狀態(${kd}): ${tok.unit.nm} → ${dest.nm}`);
          }
        }
        continue;
      }
      // 批52j: capture(捕獲, 暗箭難防) —— 已有則 altCoef 直傷; 否則 rate 捕獲(不可淨化)
      if (k === "capture") {
        const captives = enemies.filter(x => x.alive && x.captured > 0);
        if (captives.length) {
          const v = captives[0], coef = e.altCoef ?? 5.3;
          hit(caster, v, coef, e.kind || t.kind || "phys", false, _FIGHT_CTX.onHit, _FIGHT_CTX.onDeal, true);
          if (TRACE) lg(`　▸ 捕獲已存在 → ${v.nm} 追加兵刃${Math.round(coef * 100)}%`);
        } else {
          let r = e.rate ?? 1;
          if (e.scale) r = Math.min(1, r * rateScaleOf(caster, e.scale, e.scaleDiv));
          if (opt.rateChecked || rnd() < r) {
            const pool = enemies.filter(x => x.alive && !x.captured);
            const destsC = pickTargets(pool, e.n || 1);
            const dur = e.dur ?? 2;
            for (const u of destsC) {
              u.captured = Math.max(u.captured, dur + 1);
              u.healblock = Math.max(u.healblock, dur + 1);
              if (TRACE) lg(`　▸ ${u.nm} 被捕獲(${dur}回合, 不可淨化)`);
            }
          }
        }
        continue;
      }
      // 禁近似令-批K: armConsume(once_consumable族施放端) —— 武裝一份一次性追加觸發資格
      // (見 Unit 建構式 this.armedConsume 註解/e.ifArmed 頂層閘門/k==="strike"消費端)。
      if (k === "armConsume") {
        const whoAC = e.who || "self";
        const destsAC = whoAC === "self" ? (caster.alive ? [caster] : []) : allies.filter(a => a.alive);
        for (const uu of destsAC) { uu.armedConsume = { active: true }; if (TRACE) lg(`　▸ ${uu.nm} 取得下次隊友發動主動戰法後的追加觸發資格`); }
        continue;
      }
      // 禁近似令-批K: strike(once_consumable族消費端) —— 由 e.ifArmed 頂層閘門(見上方)確保
      // 只有 caster.armedConsume.active 為真才會執行到這裡, 對敵軍(targetSel 準則或隨機單體)
      // 造成一次即時傷害後消耗掉這份資格(十二奇策「並使其下次發動主動戰法後,對敵軍單體造成
      // 謀略攻擊」——與戰法頂層coef段的差異: 頂層coef是「本戰法自己發動當下」的傷害, 這裡是
      // 「buff生效期間, 我軍任一人(含自己)下一次成功發動主動戰法」時才觸發的延遲、單次消耗
      // 傷害, 兩者時機完全不同, 不可能用同一個頂層coef欄位表達)。
      if (k === "strike") {
        // 禁近似令-批K: e.sameTarget(true) —— 沿用本次applyEffects呼叫傳入的tgt(通常=本
        // 戰法主段/disarm等狀態效果剛剛命中的同一人), 而非重新targetSel/隨機選(驍健神行
        // 「如果目標已經被繳械則造成兵刃攻擊」——需要與effects陣列內排在前面的disarm效果
        // 精確命中同一人, 且靠陣列內順序執行, 使disarm先於本段套用完成)。e.ifTargetHas
        // (可選) —— 只在該目標已符合狀態時才出手(取代extraHits版「傷害在施加繳械前判定」
        // 的執行順序缺陷: extraHits與effects是兩個獨立陣列, fireExtraHits在本戰法effects
        // 套用之前就已執行完畢, 無法讀到「本次」才剛套用的disarm; 改成effects陣列內的
        // sameTarget+ifTargetHas, 陣列本身依序執行, 天然解決了先後次序問題)。
        const poolS = enemies.filter(x => x.alive);
        let v = e.sameTarget ? (tgt && tgt.alive ? tgt : null)
          : (e.targetSel ? pickByCriterion(enemies, e.targetSel) : (poolS.length ? poolS[Math.floor(rnd() * poolS.length)] : null));
        if (v && e.ifTargetHas && !targetHas(v, e.ifTargetHas)) v = null;
        if (v) { hit(caster, v, e.coef ?? 1, e.kind || t.kind || "intel", false, _FIGHT_CTX.onHit, _FIGHT_CTX.onDeal); if (TRACE) lg(`　▸ ${caster.nm} 消耗觸發資格 → ${v.nm} 追加攻擊`); }
        if (e.ifArmed) caster.armedConsume = null;
        continue;
      }
      // 批A(11筆高嚴重重建): chargeConsume —— 「可消耗資源池」的消耗端(死戰不退「普攻後,有50%
      // 機率(受武力影響)消耗一層蓄威造成一次兵刃傷害,觸發後可繼續判定,每次觸發後機率降低8%,
      // 每回合最多觸發5次」)。掛在 when:{on:"dealtDamage",normalOnly:true} 反應式(普攻確實
      // 命中造成傷害後), caster 即普攻的發動者本身。遞迴鏈式判定: 首次機率 e.rate(受
      // e.scale/e.scaleDiv縮放, 對應「受武力影響」), 每次成功消耗1層+造成e.coef傷害給隨機
      // 敵軍單體, 機率遞減 e.decayPer(預設0.08), 直到(a)未命中 或(b)蓄威層數耗盡 或(c)本回合
      // 已觸發次數達 e.maxChain(預設5, 讀 caster.chargeConsumedThisRound 計數, 見 fight()
      // 主迴圈逐回合歸零)為止。與既有反應式k的「單次判定」慣例不同, 這是本效果自己內部的
      // while迴圈鏈式判定(因為「觸發後可繼續判定」的語意本身就是同一個事件裡的連鎖反應,
      // 非跨事件的重複觸發)。
      if (k === "chargeConsume") {
        if (!caster.charge || caster.charge.n <= 0) { continue; }
        let curRate = e.rate ?? 0.5;
        if (e.scale) curRate *= rateScaleOf(caster, e.scale, e.scaleDiv);
        const decayPer = e.decayPer ?? 0.08;
        const maxChain = e.maxChain ?? 5;
        let chained = 0;
        // opt.rateChecked: 外層 dealtDamageFor 的效果級派發(見 2196 行 evRate/rnd() 判斷)已經
        // 用 e.rate 擲過一次骰才呼叫到這裡(對稱capture的opt.rateChecked慣例)——若已檢查過,
        // 第一層視為「首次判定已經命中」直接consume, 不再重擲一次(避免雙重擲骰造成機率減半);
        // 若未經檢查(如測試腳本直接呼叫), 則第一層也要自行擲骰(rnd() < curRate)。
        let firstIteration = true;
        while (caster.charge.n > 0 && caster.chargeConsumedThisRound < maxChain) {
          const hitThis = (firstIteration && opt.rateChecked) ? true : rnd() < Math.max(0, curRate);
          firstIteration = false;
          if (!hitThis) break;
          caster.charge.n -= 1;
          caster.chargeConsumedThisRound += 1;
          chained += 1;
          const pool = enemies.filter(x => x.alive);
          const v = pool.length ? pool[Math.floor(rnd() * pool.length)] : null;
          if (v) {
            hit(caster, v, e.coef ?? 1, e.kind || t.kind || "phys", false, _FIGHT_CTX.onHit, _FIGHT_CTX.onDeal, true);
            if (TRACE) lg(`　▸ ${caster.nm} 消耗蓄威第${chained}層 → ${v.nm} 兵刃傷害(${Math.round((e.coef ?? 1) * 100)}%)（剩餘蓄威${caster.charge.n}層, 本回合已觸發${caster.chargeConsumedThisRound}/${maxChain}次）`);
          }
          curRate -= decayPer;
        }
        continue;
      }
      // 批52i: proxyNormal/proxyHit(垂心萬物代打完整普攻/連擊謀略)
      if (k === "proxyNormal") {
        const atk = e.srcSel ? pickByCriterion(allies, e.srcSel) : caster;
        if (atk && atk.alive) {
          const ex = atk.addbonus("extra");
          if (!(e.ifNoExtra && ex > 0) && !(e.ifHasExtra && ex <= 0)) doNormalAttack(atk, allies, enemies);
        }
        continue;
      }
      if (k === "proxyHit") {
        let checker = e.checkSrcSel ? pickByCriterion(allies, e.checkSrcSel) : caster;
        if (!checker) checker = caster;
        const ex = checker ? checker.addbonus("extra") : 0;
        if (!(e.ifNoExtra && ex > 0) && !(e.ifHasExtra && ex <= 0)) {
          let srcU = e.srcSel ? pickByCriterion(allies, e.srcSel) : caster;
          if (!srcU || !srcU.alive) srcU = caster;
          const v = pickTargetChaos(srcU, allies, enemies);
          if (v && e.coef) hit(srcU, v, e.coef, e.kind || "phys", false, _FIGHT_CTX.onHit, _FIGHT_CTX.onDeal);
        }
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
        // 禁近似令-批K: who==="subs"+targetSel(counter_target_binding/split-ev族) —— pool
        // 限縮到「副將二人」(allies.slice(1)), 供「損失兵力較多的副將/另一名副將」這類只在
        // 兩名副將之間比較(而非全隊)的targetSel使用(三勢陣, 對稱既有enemy/ally兩種pool)。
        const pool = who === "enemy" ? enemies : (who === "subs" ? allies.slice(1) : allies);
        const picked = pickByCriterion(pool, e.targetSel);
        dests = picked ? [picked] : [];
      }
      else if (who === "self") dests = caster.alive ? [caster] : [];
      // 批42: who:"eventTarget" —— 精確鎖定「本次反應式事件的事件單位本身」(如 when.who:"enemy"
      // 廣播監聽敵軍受普攻時, 事件單位是「被打的那個敵人」, 而非泛用敵軍全體/隨機N人)。過去
      // onHit()/dealtDamage() 呼叫 applyEffects() 傳入的 tgt 參數固定是「觸發本次事件的另一方」
      // (如受擊事件傳 src=攻擊者), 沒有任何管道能表達「效果套用對象=事件單位自己」這種語意
      // (見傲睨王侯「敵軍目標受普攻時, 該目標降3%」——目標是被打的那個敵人, 不是打人的攻擊者,
      // 也不是敵軍全體/隨機選)。opt.evtTarget(見 onHitFor/dealtDamageFor 呼叫端新增的第7參數)
      // 由事件迴圈直接傳入「事件單位本身」, 與既有 tgt(攻擊者/施法目標)語意分離, 兩者互不干擾
      // (未傳 opt.evtTarget 的既有呼叫路徑, who:"eventTarget" 會落空回傳[], 等同無效——只有
      // 明確走事件廣播且明確傳入 evtTarget 的新資料才會用到, 零回歸)。
      else if (who === "eventTarget") dests = (opt.evtTarget && opt.evtTarget.alive) ? [opt.evtTarget] : [];
      else if (who === "leader") dests = (allies[0] && allies[0].alive) ? [allies[0]] : [];  // 批8: 主將限定(隊伍 index 0)
      else if (who === "enemyLeader") dests = (enemies[0] && enemies[0].alive) ? [enemies[0]] : [];  // 批52: 敵軍主將
      else if (who === "subs") dests = allies.slice(1).filter(a => a.alive);  // 批13: 副將群限定(隊伍 index 0 以外; 如鋒矢陣/箕形陣副將分化段)
      // 批30 C: who:"sub1"/"sub2"(副將固定位置分派) —— 「subs」只能讓兩名副將套用同一份效果,
      // 無法表達「副將A只防兵刃, 副將B只防謀略」這種依隊伍固定位置(而非動態屬性準則)分派相異
      // 效果的語意(見箕形陣, engine_limitations.md 第25節/16節)。sub1=allies[1](副將A,
      // index 1), sub2=allies[2](副將B, index 2), 對稱於既有 who:"leader"=allies[0] 慣例。
      // 三人隊固定編制(index 0=主將/1/2=副將), 若隊伍不足3人或該位置陣亡則 dests 為空陣列。
      else if (who === "sub1") dests = (allies[1] && allies[1].alive) ? [allies[1]] : [];
      else if (who === "sub2") dests = (allies[2] && allies[2].alive) ? [allies[2]] : [];
      // 批45 A: e.sameTargets —— 「對敵軍群體(N人)造成傷害並降低其XX」這類措辭, 過去主 coef 段
      // (pickTargets)與效果段(who:"enemy"+e.n, 走下方 CTRL_K/hasEN 分支自己的 pickTargets)各自
      // 獨立擲骰選標, 3人隊只有1/3機率同組(見 engine_limitations.md 對應節, 全庫掃描 R29)。
      // e.sameTargets:true 時直接沿用主 coef 段記錄的 opt.mainHitTgts(見 fight() 主迴圈
      // _mainHitTgts, 只在群體(vs.length>1)結算時才有值), 過濾存活後作為 dests, 不再獨立
      // pickTargets——確保「造成傷害」與「降低其XX」精確命中同一批目標, 對稱單體版本既有的
      // _mainHitTgt(t.lockTarget/t.targetSel等既有沿用慣例)。未傳 opt.mainHitTgts 的呼叫路徑
      // (prep/reactive/choices分支未帶等)dests 落空回傳[], 向後相容(只有明確要求且母戰法主
      // coef段確實命中>=2人群體時才會生效)。
      else if (e.sameTargets) dests = (opt.mainHitTgts || []).filter(x => x.alive);
      else if (who === "enemy") {
        // 批B(filter-then-pick修正): e帶ifTargetHas/ifTargetHasNot/ifStatCompare等目標
        // 資格gate時, 先把enemies過濾成合格池, 下方CTRL_K/hasEN隨機選標分支才從合格池
        // pickTargets(而非「先隨機挑、挑完才用gate過濾」——見gatePool頂層註解)。無gate的
        // 效果enemyPool與enemies是同一份array, 完全維持原隨機行為不變。
        const enemyPool = gatePool(enemies, e, caster, allies, enemies);
        if (CTRL_K) {                                 // 群體控制(n>1 或有 nMax)隨機挑不重複目標; 單體優先鎖定 tgt
          // 批26: CTRL類效果優先讀 e.n/e.nMax(效果自身欄位), 無則fallback到t.n/t.nMax(戰法
          // 頂層, 舊行為, 向後相容)。原本CTRL_K只認頂層n/nMax, 導致同一戰法內「多段各自不同
          // 目標數的chaos/stun等控制效果」(如神機莫測「1名必中混亂 + 另外N名各自獨立機率判定
          // 混亂」)無法用單一戰法頂層n表達出兩種不同的目標數, 被迫二選一近似成同一個n。
          const n = hasEN ? e.n : (t.n || 1);
          const nMax = hasEN ? e.nMax : t.nMax;
          const cnt = nMax ? n + Math.floor(rnd() * (nMax - n + 1)) : n;
          dests = cnt <= 1 ? (tgt && tgt.alive ? [tgt] : pickTargets(enemyPool, 1)) : pickTargets(enemyPool, cnt);
        } else if (hasEN) {                            // 批23 A1: 非CTRL效果讀 e.n/e.nMax
          const cnt = e.nMax ? e.n + Math.floor(rnd() * (e.nMax - e.n + 1)) : e.n;
          dests = cnt <= 1 ? (tgt && tgt.alive ? [tgt] : pickTargets(enemyPool, 1)) : pickTargets(enemyPool, cnt);
          // 批45 A: 若本效果本身是「首次」命中群體(cnt>1)的來源(無 coef 段可沿用時, 如誘敵深入
          // coef=0, dot+amp 兩個效果皆為 effects 陣列內的同層 sibling), 就地更新 opt.mainHitTgts,
          // 讓本戰法內排在後面、帶 e.sameTargets 的效果可以沿用「前一個效果實際命中的那一批
          // 目標」, 不必一定要來自頂層 coef 段。只在尚未有 opt.mainHitTgts(未被 coef 段設定過)
          // 時才更新, 避免覆蓋掉更早、更明確的 coef 段記錄。
          if (cnt > 1 && opt.mainHitTgts == null) opt.mainHitTgts = dests;
        } else dests = enemies.filter(x => x.alive);
      }
      else if (hasEN) {                                 // 批23 A1: who="ally"(含預設) 非CTRL效果讀 e.n/e.nMax(如「我軍2人」「自己及友軍單體」)
        const allyPool = gatePool(allies, e, caster, allies, enemies);  // 批B: filter-then-pick(見gatePool頂層註解)
        const cnt = e.nMax ? e.n + Math.floor(rnd() * (e.nMax - e.n + 1)) : e.n;
        dests = cnt <= 1 ? (tgt && tgt.alive && allies.includes(tgt) ? [tgt] : pickTargets(allyPool, 1)) : pickTargets(allyPool, cnt);
      }
      else dests = allies.filter(a => a.alive);
      // 批16: ifTargetHas —— 效果段條件, 只對「已有該狀態」的目標生效; 選目標後過濾(不影響選目標邏輯本身)
      if (e.ifTargetHas) dests = dests.filter(u => targetHas(u, e.ifTargetHas));
      // 批A(11筆高嚴重重建): ifTargetHasNot —— ifTargetHas的反向(只對「尚未有該狀態」的目標
      // 生效), 對稱既有正向版本, 獨立欄位(非在ifTargetHas上加negate旗標)以維持既有呼叫端
      // 零改動、新舊資料互不干擾。偽書相間「若目標已混亂則...(否則)施加混亂」的否則分支
      // ——用ifTargetHasNot:"chaos"精確表達「僅未混亂的目標才施加混亂」, 與extraHits段的
      // ifTargetHas:"chaos"(僅已混亂才強制打友軍)形成互斥的if/else對, 避免同回合對已混亂
      // 目標重複刷新chaos(雖然dur:1的重複刷新本身無害, 但精確表達原文"否則"的互斥語意仍
      // 優於放任兩分支都生效)。
      if (e.ifTargetHasNot) dests = dests.filter(u => !targetHas(u, e.ifTargetHasNot));
      // 批I(禁近似令-scale/比較族): ifStatCompare —— 比較「參照方(施放者/我軍主將)vs目標」
      // 同一屬性大小, 只對比較成立的目標生效(摧鋒斷刃「自身武力較高」/聚石成金「敵軍魅力
      // 低於我軍主將」), 對稱ifTargetHas/ifTargetHasNot但比較的是「屬性大小」而非「狀態
      // 有無」, 見statCompareOk()。
      if (e.ifStatCompare) dests = dests.filter(u => statCompareOk(caster, u, allies, e.ifStatCompare));
      // 禁近似令-批K: e.ifTargetHpAbove/ifTargetHpBelow —— 對稱既有when.hpAbove/hpBelow
      // (只認caster自身), 這裡是「已選定的效果目標(受益者)自己」兵力百分比條件(肉身鐵壁
      // 「當友軍兵力高於70%時」——受益的是友軍而非施放者自己, engine既有hpOk只查caster,
      // 無法表達「他方單位」的血量條件)。
      if (e.ifTargetHpAbove != null) dests = dests.filter(u => u.hpPct > e.ifTargetHpAbove);
      if (e.ifTargetHpBelow != null) dests = dests.filter(u => u.hpPct < e.ifTargetHpBelow);
      // 禁近似令-批K: e.ifSelfStatCompare(spec:{statA,statB,op}) —— 「已選定的效果目標自己」
      // 兩項屬性大小互比(與ifStatCompare的「參照方vs目標」跨單位比較方向不同, 這裡同一單位
      // 自己的兩個屬性互比), 淵然難測「若傷害來源武將武力高於智力則...否則...」需要判斷
      // 「觸發本次反應式的攻擊者自己」武力vs智力。
      if (e.ifSelfStatCompare) {
        const spec = e.ifSelfStatCompare, opFn = {
          gt: (a, b) => a > b, gte: (a, b) => a >= b, lt: (a, b) => a < b, lte: (a, b) => a <= b,
        }[spec.op || "gt"];
        dests = dests.filter(u => opFn(u.eff(spec.statA), u.eff(spec.statB)));
      }
      // 禁近似令-批K: ifTargetIsRank/ifTargetIsRankNot(target_rank_branch族) —— 「已選定的
      // 目標是否恰好符合某屬性排名準則」的事後判斷, 與 targetSel(依準則主動挑目標)方向相反。
      // spec: {stat:"force"|"intel", rank:"max"|"min"}。閉月「依目標恰好是不是武力/智力最高
      // 分三支」: 混亂分支 ifTargetIsRank(武力最高) / 計窮分支 ifTargetIsRank(智力最高) /
      // 否則分支 ifTargetIsRankNot([武力最高,智力最高])(兩者皆不是才生效)。用既有
      // pickByCriterion(enemies, 準則)找出當下真正的排名冠軍, 與 dests 內每個目標比對是否
      // 為同一人(嚴格 unit 物件相等, 因排名冠軍全隊唯一)。rankKeyOf 批B已抽到頂層(供
      // targetGateOk共用, 見其定義處), 這裡不再重複定義。
      if (e.ifTargetIsRank) {
        const champ = pickByCriterion(enemies, rankKeyOf(e.ifTargetIsRank));
        dests = dests.filter(u => u === champ);
      }
      if (e.ifTargetIsRankNot) {
        const specs = Array.isArray(e.ifTargetIsRankNot) ? e.ifTargetIsRankNot : [e.ifTargetIsRankNot];
        const champs = specs.map(s => pickByCriterion(enemies, rankKeyOf(s)));
        dests = dests.filter(u => !champs.includes(u));
      }
      // 批52g: whoNames —— 只對武將名在名單內的目標(黃巾副將含 SP)
      if (e.whoNames) {
        const wn = Array.isArray(e.whoNames) ? e.whoNames : [e.whoNames];
        dests = dests.filter(u => u.g && wn.includes(u.g.name));
      }
      // 批52g: 逐目標 rate + rateStatusBonus(水攻/沙暴各+20%)
      if (perTgtRate && dests.length) {
        dests = dests.filter(d => {
          let r = e.rate ?? 1;
          const b = e.rateStatusBonus;
          if (b) {
            const needL = !!b.ifLeader;
            if (!needL || (allies && allies[0] === caster)) {
              const nst = countNamedStatuses(d, b.statuses || []);
              r = r + Math.min(b.maxBonus ?? 99, nst * (b.per ?? 0));
            }
          }
          return rnd() < r;
        });
      }
      if (TRACE && dests.length) lg(`　▸ ${effDesc(k, e, caster)} → ${dests.map(u => u.nm).join("、")}`);
      // scale:"intel"|"force"|"command"|"speed"|"charm" 縮放(以施放者戰鬥內即時素質為準):
      // amp/mitig 的 val 直接乘 SCALE, clamp 到 ±SCALE_CLAMP 防止極端值; stat 的 mult 對
      // 1.0 的偏移量(增益/削弱幅度)乘 SCALE, 1.0 本身(無效果)不受縮放影響。
      // 批52c: scaleIfSub / scaleIfLeader
      const scaleOkE = () => {
        if (!e.scale) return false;
        if (e.scaleIfSub && !(allies && allies[0] !== caster)) return false;
        if (e.scaleIfLeader && !(allies && allies[0] === caster)) return false;
        return true;
      };
      const svVal = v => scaleOkE() ? Math.max(-SCALE_CLAMP, Math.min(SCALE_CLAMP, v * scaleOf(caster, e.scale))) : v;
      const svMult = m => scaleOkE() ? 1 + (m - 1) * scaleOf(caster, e.scale) : m;
      const svAdd = a => scaleOkE() ? a * scaleOf(caster, e.scale) : a;
      // 批16: undispellable 旗標 —— 效果加此欄則 dispel 略過(附加進 pushAdd/pushMod 的 flags, 供 dispelUnit 讀取)
      // 批24 D2: dmgType 旗標 —— amp/mitig 效果可選填 e.dmgType:"phys"|"intel", 限定只對該類型
      // 傷害生效(damage() 結算時依 kind 過濾, 見 amp()/addbonus() 的 dmgType 參數)。與
      // undispellable 合併進同一個 flags 物件(pushAdd/pushMod 第5參數), 兩者互不干擾。
      // 批28 B3: normalOnly 旗標 —— amp/mitig 效果可選填 e.normalOnly:true, 限定只對普攻傷害
      // 生效/受影響(見至柔動剛「降低我軍及敵軍全體普通攻擊傷害35%」)。
      // 批31 A: activeOnly 旗標(僅 amp 支援, 對稱於 normalOnly) —— 效果可選填 e.activeOnly:true,
      // 限定只對主動戰法傷害生效(見士爭先赴)。批40 B: chargeOnly 旗標(同族, 對稱activeOnly)
      // —— 效果可選填 e.chargeOnly:true, 限定只對突擊戰法傷害生效(見一鼓作氣「突擊戰法造成
      // 傷害提升12%」/藏刀「突擊戰法造成傷害降低5%」)。
      // 批H: critUp/critDmgUp(會心/奇謀機率與傷害幅度) 與 amp/mitig 共用同一套 dmgType/
      // normalOnly/ifLeader/ifLeaderIs 條件旗標與 dtSrc 尾碼去重慣例(見 damage() 對稱段落
      // 消費 addbonus("critUp"/"critDmgUp", dmgType, ...)), 故並列進下列判斷式。
      const CRIT_KINDS = k === "amp" || k === "mitig" || k === "critUp" || k === "critDmgUp";
      const normalOnly = CRIT_KINDS && !!e.normalOnly;
      const activeOnly = k === "amp" && !!e.activeOnly;
      const chargeOnly = k === "amp" && !!e.chargeOnly;
      const ifLeaderTopup = CRIT_KINDS && !!e.ifLeader;  // 批41 B: 見下方dtSrc註解
      const ifLeaderIsTopup = CRIT_KINDS && !!e.ifLeaderIs;  // 批44 A: 同ifLeaderTopup, 見下方dtSrc註解
      // 禁近似令-批L: e.dmgFromStatus(陣列, 僅k==="amp") —— 才辯機捷「自身施加的灼燒、水攻、
      // 中毒、潰逃、沙暴、叛逃狀態造成的傷害提升90%」跨戰法橫切限定範圍(這6種具名dot狀態由
      // 任何戰法施加時都算, 非本效果專屬某一段固定coef)。damage()結算dot傷害時(k==="dot"分支
      // 呼叫damage()的呼叫點)會傳入該次dot實際解析出的具名狀態(resolveDotName, 與u.dots[3]
      // 同一份值), addbonus("amp",...)新增dotStatus參數比對: 帶dmgFromStatus的amp條目只在
      // dotStatus命中清單內才計入, 未帶此欄位的既有全部amp條目不受影響(dotStatus為undefined
      // 時等同falsy, 帶dmgFromStatus的條目會被跳過——但一般傷害路徑本就不該吃到這個限定範圍的
      // 加成, 故此為正確行為而非副作用)。
      const dmgFromStatus = (k === "amp" && e.dmgFromStatus) ? e.dmgFromStatus : null;
      const udFlags = (e.undispellable || e.dmgType || normalOnly || activeOnly || chargeOnly || dmgFromStatus) ? { undispellable: !!e.undispellable, dmgType: e.dmgType, normalOnly, activeOnly, chargeOnly, dmgFromStatus } : undefined;
      // dmgType 存在時, src 附加類型尾碼區分 dedup key(同一戰法內若有兩條不同 dmgType 的
      // amp/mitig, 如暫避其鋒「智力最高者減兵刃傷害」+「武力最高者減謀略傷害」, 兩者若共用
      // 同一個 src(戰法名)會被 pushAdd 的「同kind+同src刷新」去重機制互相蓋掉, 見 rateup 的
      // 既有 prepOnly/nativeOnly 尾碼慣例同理)。批28 B3: normalOnly 同理附加尾碼。批31 A/
      // 批40 B: activeOnly/chargeOnly 同理附加尾碼。
      // 批41 B: ifLeader top-up 尾碼 —— 圍師必闕修R27時新增「基礎mitig(無條件0.39)+差額
      // mitig(ifLeader:true,0.06)」的base+top-up拆法(比照水淹七軍dot的既有precedent, 但dot
      // 走 u.dots.push 不去重, amp/mitig走pushAdd同kind+同src會覆蓋), 若不加尾碼兩條mitig
      // (who同ally, dmgType同intel)會共用dtSrc, 後套用的ifLeader top-up會把基礎段整個蓋掉。
      // 補尾碼":ifLeader"區分(同leaderBonus既有的":leaderBonus"尾碼慣例, 見k==="chargeup"分支),
      // 讓兩條並存疊加。
      let dtSrc = (src && e.dmgType) ? src + ":" + e.dmgType : src;
      if (normalOnly && src) dtSrc = (dtSrc || src) + ":normalOnly";
      if (activeOnly && src) dtSrc = (dtSrc || src) + ":activeOnly";
      if (chargeOnly && src) dtSrc = (dtSrc || src) + ":chargeOnly";
      if (dmgFromStatus && src) dtSrc = (dtSrc || src) + ":dmgFromStatus";  // 禁近似令-批L: 避免與同戰法內其餘amp段共用dtSrc互相覆蓋(才辯機捷目前只有單一amp段, 此尾碼為未來多段並存預留)
      if (ifLeaderTopup && src) dtSrc = (dtSrc || src) + ":ifLeader";
      // 批44 A: ifLeaderIs top-up 尾碼 —— 同批41 B ifLeader top-up的理由(避免base段+差額段
      // 共用dtSrc被pushAdd同kind+同src去重互相覆蓋), 用於白毦兵等「若XX統領, 數值更高」家族的
      // base(無條件)+top-up(ifLeaderIs:"XX")拆法。
      if (ifLeaderIsTopup && src) dtSrc = (dtSrc || src) + ":ifLeaderIs";
      for (const u of dests) {
        // 批A(11筆高嚴重重建): k==="amp"+e.stackKey —— 對稱既有k==="stat"+e.stackKey(批42/43,
        // exploitLayers per-target疊層), 補上amp的per-target疊層變體。密計誅逆「使敵軍單體
        // (隨機)造成的最終傷害降低15%,持續2回合,最多疊加3次」——疊層對象是「被隨機選中的那個
        // 敵方單位」, 每次選中(可能重複選到同一人也可能選到不同人, 隨機性沿用既有pickTargets)
        // 就疊1層, 封頂maxStacks(3層)。刻意只做「per-target層數計數+累計總值重算後pushAdd
        // 刷新覆蓋」核心邏輯, 不搬既有stat stackKey的onMaxStacks/globalMax/e.add(平點)三個
        // 延伸功能(密計誅逆本身無「疊滿後額外效果」或「跨目標累計觸發全場效果」的原文語意,
        // 硬搬會增加未使用的複雜度且無資料驗證其正確性, 精簡版足夠表達本戰法需求; 若未來有
        // 戰法需要amp版onMaxStacks/globalMax再擴充, 屆時比照stat stackKey的模式加回)。用
        // 獨立Map(u.ampLayers, 與stat的exploitLayers分開, 避免不同k共用同一份計數器混淆語意)。
        if (k === "amp" && e.stackKey) {
          if (!u.ampLayers) u.ampLayers = new Map();
          const already = u.ampLayers.get(e) || 0;
          if (e.maxStacks == null || already < e.maxStacks) {
            const layers = already + 1;
            u.ampLayers.set(e, layers);
            const perStack = e.perStack ?? svVal(e.val);
            const totalVal = perStack * layers;
            u.pushAdd("amp", totalVal, e.dur, dtSrc, udFlags);
            // 禁近似令-批K: e.stackId(dynamic_coef_from_counter族) —— 同時把本次疊層數寫進
            // 字串鍵索引 u.ampLayersById[stackId], 供另一個獨立效果(k==="settle"+e.perStackFrom,
            // 見其消費端)跨效果讀取「這個目標身上, 這個具名疊層計數器目前疊了幾層」, 解決
            // JSON序列化下兩個效果物件無法互相持有物件參考(只能用共同約定的字串id間接引用)
            // 的問題(密計誅逆「第6回合斬殺傷害率100%+25%×最終降傷施加次數」——settle結算時
            // 需要讀取「本段amp-stackKey疊層」的當下層數代入coef公式)。
            if (e.stackId) { u.ampLayersById = u.ampLayersById || {}; u.ampLayersById[e.stackId] = layers; }
            if (TRACE) lg(`　▸ ${u.nm} 疊層 第${layers}層（累計易傷/減傷${(totalVal * 100).toFixed(1)}%）`);
          }
          // 已達maxStacks: 語意上「這個目標已無法再疊」, 不做任何pushAdd(既有累計值維持不變,
          // 對稱stat stackKey的continue慣例, 差別是這裡沒有其餘k要處理無需continue跳出迴圈)。
        }
        else if (k === "amp") { const v = svVal(e.val); const ms = e.maxStack; who === "enemy" && v > 0 ? u.pushAdd("mitig", -v, e.dur, dtSrc, udFlags, ms) : u.pushAdd("amp", v, e.dur, dtSrc, udFlags, ms); }
        // 禁近似令-批K: k==="dmgShare"(engine_wiring_gaps_misc族) —— 「使其任一目標受到傷害時
        // 會回饋X%傷害給其他敵軍」的傷害轉嫁給隊友機制(連環計), 消費端見 hit() 內 dst.dmgShare
        // 判斷式。取代舊有「用amp(敵全體固定+15%受傷)作EV代理近似, 方向類似但觸發條件/轉移
        // 對象皆與原文不同」的結構性近似。
        else if (k === "dmgShare") { u.dmgShare = { pct: svVal(e.val), dur: (e.dur ?? 2) + 1 }; if (TRACE) lg(`　▸ ${u.nm} 中鐵鎖連環(受傷回饋${(svVal(e.val) * 100).toFixed(1)}%給隊友)`); }
        else if (k === "mitig" && e.stackKey) {
          // 禁近似令-批K: mitig+e.stackKey(對稱既有amp+e.stackKey per-target疊層變體, 見上方
          // k==="amp"分支)——離月「友軍受到治療時,40%機率+3%減傷,可疊5層,持續2回合」需要
          // 每次觸發對目標疊1層(而非固定值), 沿用amp的ampLayers計數器與相同的疊層演算法。
          if (!u.ampLayers) u.ampLayers = new Map();
          const already = u.ampLayers.get(e) || 0;
          if (e.maxStacks == null || already < e.maxStacks) {
            const layers = already + 1;
            u.ampLayers.set(e, layers);
            const perStack = e.perStack ?? svVal(e.val);
            const totalVal = perStack * layers;
            u.pushAdd("mitig", totalVal, e.dur, dtSrc, udFlags);
            if (e.stackId) { u.ampLayersById = u.ampLayersById || {}; u.ampLayersById[e.stackId] = layers; }
          }
        }
        else if (k === "mitig") u.pushAdd("mitig", svVal(e.val), e.dur, dtSrc, udFlags, e.maxStack);
        // 批H: critUp(會心/奇謀機率, val加法累積) / critDmgUp(會心/奇謀傷害幅度, 疊在基礎
        // +100%之上) —— 走與amp/mitig相同的pushAdd加法疊加通道, 由 damage() 於傷害結算時
        // 讀 addbonus("critUp"/"critDmgUp", dmgType, ...) 消費(見該函式對稱段落), dmgType
        // 依本文用詞路由: "phys"=會心(兵刃暴擊)/"intel"=奇謀(謀略暴擊)。與amp/mitig的差異
        // 純粹是消費端不同(amp直接乘傷害基數, critUp是擲骰rate/critDmgUp是命中後幅度), 資料
        // 層加法疊加/scale/ifLeader/dmgType等既有原語組合全部原樣沿用, 零新增targeting邏輯。
        // critUp+e.stackKey(對稱k==="amp"+e.stackKey, 見上方詳細註解) —— 逆鱗「受到傷害時,
        // 3%機率獲得10%會心,可疊加2次」需要per-target疊層(裝備效果src固定為null, push_add
        // 的max_stack去重機制以src為鍵, 對裝備效果不生效, 必須用獨立的id(e)鍵疊層計數器,
        // 與amp/stat的stackKey機制完全對稱, 只是掛在critLayers/crit_layers獨立Map, 避免與
        // ampLayers/exploitLayers混淆)。
        else if (k === "critUp" && e.stackKey) {
          if (!u.critLayers) u.critLayers = new Map();
          const already = u.critLayers.get(e) || 0;
          if (e.maxStacks == null || already < e.maxStacks) {
            const layers = already + 1;
            u.critLayers.set(e, layers);
            const perStack = e.perStack ?? svVal(e.val);
            const totalVal = perStack * layers;
            u.pushAdd("critUp", totalVal, e.dur, dtSrc, udFlags);
            if (TRACE) lg(`　▸ ${u.nm} 會心疊層 第${layers}層（累計會心機率${(totalVal * 100).toFixed(1)}%）`);
          }
        }
        else if (k === "critUp") u.pushAdd("critUp", svVal(e.val), e.dur, dtSrc, udFlags, e.maxStack);
        else if (k === "critDmgUp") u.pushAdd("critDmgUp", svVal(e.val), e.dur, dtSrc, udFlags, e.maxStack);
        // 批16: immuneTo(單項控制免疫) —— isImmuneTo(k) 只免疫清單內控制類型, 與 insight(全免) 並列判斷
        // 批52h: 成功施加後 fireControlled(機鑑反彈); opt.noCtrlReflect 時跳過
        else if (k === "stun") { if (!u.insight && !u.isImmuneTo("stun")) { u.stun = Math.max(u.stun, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入震懾(全禁)`); if (!opt.noCtrlReflect) fireControlled(u, "stun", e.dur ?? 1, allies, enemies); } else { fireSelfReactive(u, "ctrlImmune", 1); if (TRACE) lg(`　▸ ${u.nm} 免疫震懾`); } }  // 禁近似令-批L: 免疫格擋觸發ctrlImmune事件(一身是膽)
        else if (k === "silence") { if (!u.insight && !u.isImmuneTo("silence")) { u.silence = Math.max(u.silence, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入計窮(禁主動戰法)`); if (!opt.noCtrlReflect) fireControlled(u, "silence", e.dur ?? 1, allies, enemies); } else { fireSelfReactive(u, "ctrlImmune", 1); if (TRACE) lg(`　▸ ${u.nm} 免疫計窮`); } }
        else if (k === "disarm") { if (!u.insight && !u.isImmuneTo("disarm")) { u.disarm = Math.max(u.disarm, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入繳械(禁普攻)`); if (!opt.noCtrlReflect) fireControlled(u, "disarm", e.dur ?? 1, allies, enemies); } else { fireSelfReactive(u, "ctrlImmune", 1); if (TRACE) lg(`　▸ ${u.nm} 免疫繳械`); } }
        else if (k === "chaos") { if (!u.insight && !u.isImmuneTo("chaos")) { u.chaos = Math.max(u.chaos, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入混亂(敵我不分)`); if (!opt.noCtrlReflect) fireControlled(u, "chaos", e.dur ?? 1, allies, enemies); } else { fireSelfReactive(u, "ctrlImmune", 1); if (TRACE) lg(`　▸ ${u.nm} 免疫混亂`); } }  // 批12 ModeF
        else if (k === "insight") { u.insight = Math.max(u.insight, (e.dur ?? 1) + 1); u.stun = 0; u.silence = 0; u.disarm = 0; u.chaos = 0; u.ambush = 0; }
        else if (k === "immune") { u.pushImmune(e.types, e.dur); if (TRACE) lg(`　▸ ${u.nm} 獲得控制免疫〔${(e.types || []).join("、")}〕`); }  // 批16: immuneTo
        else if (k === "first") u.first = Math.max(u.first, e.dur ?? 1);
        // 批18: ambush(遇襲, 先攻的反面/遲緩) —— 不鎖行動(仍可行動), 只影響排序(見 fight() 的
        // effFirst 三檔排序鍵)。insight(全免)/immuneTo(單項免疫)可免, 同其他控制類慣例。
        else if (k === "ambush") { if (!u.insight && !u.isImmuneTo("ambush")) { u.ambush = Math.max(u.ambush, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 陷入遇襲(行動遲滯)`); } else { fireSelfReactive(u, "ctrlImmune", 1); if (TRACE) lg(`　▸ ${u.nm} 免疫遇襲`); } }
        // 批42: e.stackKey(truthy旗標) —— stat 效果的「每次觸發對目標疊加1層」模式, 取代既有
        // add/mult 二選一的「單次套用」語意。原文族: 傲睨王侯「敵軍目標受普攻時觸發1個破綻,
        // 該目標降3%武智統速(受智力影響)可疊加」——每次事件命中對「這一個目標」疊1層, 疊層數
        // 上限 e.maxStacks(該目標本地破綻池耗盡), 用「效果物件」當Map鍵掛在目標身上
        // (u.exploitLayers, 見Unit建構式註解), 每層量級 e.perStack(預設0.03)×lockedScaleOf
        // (「數值鎖定準備階段」慣例, calibration_anchors.json aoni_wanghou laws「prep鎖定再證」
        // ——同場多層恆定%, 不因戰鬥中智力浮動重算)。用pushMod(同src同stat「刷新覆蓋」既有慣例)
        // 每次重新算「當前層數×每層量級」的總乘數並覆寫, 天然等同疊加(因為新值已包含新層數),
        // 不需要另外的疊加型pushXxx原語。
        // 禁近似令-批K: e.fromStack(dynamic_coef_from_counter族, stat版) —— 「本效果不自己
        // 疊層, 改為註記同一持有者身上既有k:"stack"計數器(this.stack, 由stack效果驅動amp的
        // 既有per-caster疊層通道)額外驅動一個stat屬性」, 註記後交給 eff() 的即時讀取(見其
        // 對 this.stack.statField/statPerVal 的消費, 對稱既有 this.stack.per×this.stack.n
        // 驅動amp的寫法)天然隨u.stack.n逐回合成長同步變動, 不需要每回合重新pushStatAdd。供
        // 弓腰姬「依自身擁有的功能性增益數量額外提傷並疊加武力」——傷害段(stack驅動amp)與武力
        // 段用同一個計數器動態同步, 取代舊有「取單層滿級值18點靜態近似, 與傷害段動態疊層不
        // 同步」的做法。若u.stack尚未建立(effects陣列順序異常)則安全側no-op。
        else if (k === "stat" && e.fromStack) {
          if (u.stack) { u.stack.statField = e.stat; u.stack.statPerVal = e.perStackVal ?? 0; }
        }
        else if (k === "stat" && e.stackKey) {
          if (!u.exploitLayers) u.exploitLayers = new Map();
          const already = u.exploitLayers.get(e) || 0;
          // 批42: 該目標本地破綻池若已耗盡(達maxStacks), 語意上「已無破綻可觸發」——之後同一
          // 目標再受普攻, 不應再刷新/計入任何東西(非「持續疊加但封頂」, 是「這個池子空了」)。
          // 用 continue(跳過dests迴圈本次u, 非return——本函式外層還有t.effects的其餘效果段
          // 待處理, return會誤將它們一併跳過)避免重複pushMod/誤增全場計數。
          if (e.maxStacks != null && already >= e.maxStacks) continue;
          const layers = already + 1;
          u.exploitLayers.set(e, layers);
          // 禁近似令-批K: e.stackId(dynamic_coef_from_counter族, 對稱amp/mitig+stackKey既有
          // stackId寫入) —— 絕地反擊「自己每次受兵刃傷害+3→6點武力,最大疊加10次;第5回合根據
          // 疊加次數對敵軍全體造成傷害」需要另一個獨立的dot效果(見k==="dot"+e.coefFromStack
          // 消費端)跨效果讀取「自己身上這個具名疊層計數器目前疊了幾層」, 供第5回合AoE傷害的
          // coef動態代入, 與ampLayersById共用同一個字串鍵命名空間(self-stacking時u===caster)。
          if (e.stackId) { u.ampLayersById = u.ampLayersById || {}; u.ampLayersById[e.stackId] = layers; }
          const sc = lockedScaleOf(caster, e);
          // 批43 A: e.add(平點疊層) —— 批42 stackKey原先只支援 e.stat+mult(逐層%乘算, 如傲睨
          // 王侯), 全庫兄弟遷移掃描發現「可疊加N次」家族另有平點(add)疊層形態(如虎侯「+15點
          // 統率, 可疊加5次」——每層固定+15點, 非百分比)。同一 stackKey 骨架(exploitLayers計數
          // /maxStacks封頂/onMaxStacks/globalMax全部沿用不變), 差別只在最終套用的原語(pushMod
          // mult vs pushStatAdd add)與量級公式(mult是"1-per×layers×sc"的乘法衰減, add是單純
          // "perStack×layers×sc"的疊加平點, 無0.95下限保護的必要, 平點不會出現mult型「全屬性
          // 歸零/負值」的極端情形)。e.add(truthy旗標, 非數值本身——量級仍讀e.perStack, 沿用
          // 既有欄位命名, add僅作為「選add分支還是mult分支」的路由旗標避免新增額外欄位)。
          const perStack = e.perStack ?? 0.03;
          if (e.add) {
            const totalAdd = perStack * layers * sc;
            u.pushStatAdd(e.stat, totalAdd, e.dur ?? 99, src, udFlags);
            if (TRACE) lg(`　▸ ${u.nm} 疊層 第${layers}層（累計${STAT_ZH[e.stat] || e.stat}+${totalAdd.toFixed(1)}, 受${STAT_ZH[e.scale] || e.scale}影響）`);
          } else {
            const totalMult = 1 - Math.min(0.95, perStack * layers * sc);  // 0.95下限防止全屬性歸零/負值(既有SCALE_CLAMP同族安全側保護, 本效果無實測樣本佐證超過maxStacks後的極端行為, 保守夾住)
            u.pushMod(e.stat, totalMult, e.dur ?? 99, src, udFlags);
            if (TRACE) lg(`　▸ ${u.nm} 破綻 第${layers}層（累計${STAT_ZH[e.stat] || e.stat}×${totalMult.toFixed(3)}, 受${STAT_ZH[e.scale] || e.scale}影響）`);
          }
          // 批42: e.onMaxStacks(效果陣列, 選填) —— 該目標本地破綻池首次耗盡(layers達maxStacks)
          // 時額外套用的一次性效果段(如傲睨王侯「單目標破綻全觸發→1回合虛弱+受傷提高15%持續2
          // 回合」), 用exploitCapped(Set<效果物件>)去重, 確保同一目標只觸發一次(之後即使繼續
          // 被普攻, layers已封頂不再增加, 也不重複觸發此段)。用合成單效果戰法遞迴呼叫
          // applyEffects: caster仍傳原持有者(holder, 保持scale=智力縮放的基準人物不變, 對應
          // 「受智力影響」是持有者的智力, 非目標的), 目標則靠who:"eventTarget"+opt.evtTarget:u
          // 精確指定為u(此目標), 不能用who:"self"(那會需要caster=u, 但caster換成u會連帶讓
          // scale錯誤地改用目標智力, 兩者互斥, 故採eventTarget機制解耦「持有者(scale基準)」
          // 與「效果套用目標」)。
          if (e.onMaxStacks && e.maxStacks != null && layers >= e.maxStacks) {
            if (!u.exploitCapped) u.exploitCapped = new Set();
            if (!u.exploitCapped.has(e)) {
              u.exploitCapped.add(e);
              if (TRACE) lg(`　▸ ${u.nm} 破綻全觸發（本地池耗盡）`);
              for (const sub of e.onMaxStacks) applyEffects(caster, null, { effects: [sub], kind: t.kind || "phys" }, allies, enemies, { reactive: true, evtTarget: u });
            }
          }
          // 批42: e.globalMax/e.globalEffects(選填) —— 持有者視角跨目標累計觸發次數(不論落在
          // 哪個目標身上, 每次成功疊層都+1, 見exploitGlobal掛在caster/holder身上而非目標),
          // 達到e.globalMax(原文「場上所有破綻」15個)且尚未觸發過時, 套用e.globalEffects
          // (如傲睨王侯「敵軍群體2人武智統速降20%」)。fired旗標防重複觸發(一次性語意)。
          if (e.globalMax != null && e.globalEffects) {  // 走到這裡代表上面已通過「本地池未耗盡」的continue閘門(見already>=maxStacks時已continue跳過), 故此處必為新層, 不需再額外檢查capped
            if (!caster.exploitGlobal) caster.exploitGlobal = new Map();
            const g = caster.exploitGlobal.get(e) || { n: 0, fired: false };
            if (!g.fired) {
              g.n += 1;
              if (g.n >= e.globalMax) {
                g.fired = true;
                if (TRACE) lg(`　▸ ${caster.nm} 破綻全場觸發（累計${g.n}/${e.globalMax}）`);
                for (const sub of e.globalEffects) applyEffects(caster, null, { effects: [sub], kind: t.kind || "phys" }, allies, enemies, { reactive: true });
              }
            }
            caster.exploitGlobal.set(e, g);
          }
        }
        else if (k === "stat") {
          const ms = e.maxStack; const statField = resolveStatField(u, e.stat);
          // 禁近似令-批K: e.addPerBuffType({types,per}) —— rate_self_dynamic族的stat版本
          // (弓腰姬「每多1個功能性增益狀態,提高自身9→18點武力,最多疊加5次」與傷害段stack
          // 同源計數但驅動stat而非amp, 過去無法讓stat讀取「當下持有幾種增益狀態」動態疊加,
          // 取單層滿級值靜態近似)。add=per×count(封頂由呼叫端於e.maxCount控制, 對應「最多
          // 疊加5次」——count本身已受countActiveBuffTypes天花板(最多5種可能類型)自然限制,
          // 額外的e.maxCount再夾一次上限, 兩者皆非設計猜測而是既有候選類型數量的結構性上限)。
          if (e.addPerBuffType) {
            const cnt = Math.min(e.addPerBuffType.maxCount ?? 99, countActiveBuffTypes(caster, e.addPerBuffType.types || []));
            u.pushStatAdd(statField, svAdd((e.addPerBuffType.per ?? 0) * cnt), e.dur, src, udFlags, ms);
          }
          else if (e.add != null) u.pushStatAdd(statField, svAdd(e.add), e.dur, src, udFlags, ms);
          else u.pushMod(statField, svMult(e.mult ?? 1), e.dur, src, udFlags, ms);
        }  // 裝備平加(add)與乘算(mult)擇一; 批52 maxStack; 批I: e.stat==="maxStat"動態解析(resolveStatField)
        // 批23 A3: dot 結算優先讀 e.kind(戰法整體是兵刃 t.kind="phys", 但灼燒/水攻類 dot 段
        // 依原文「受智力影響」應走謀略傷害類型, 過去誤用 t.kind 導致傷害類型錯位, 如天降火雨
        // 兵刃戰法掛的灼燒本應是 intel 類, 若戰法整體改記 kind="phys" 會連帶把 dot 也算成
        // 兵刃), 無 e.kind 時 fallback t.kind(向後相容既有無 e.kind 的 dot 資料)。
        else if (k === "huchen") {
          // 批52d: 虎嗔(將門虎女負面狀態)
          u.huchen = {
            base: e.base ?? e.coef ?? 0.20,
            per: e.per ?? 0.30,
            hits: 0,
            maxHits: e.maxHits ?? 3,
            left: (e.dur ?? 1) + 1,
            caster,
            kind: e.kind || t.kind || "phys",
            src: t.nameZh || "虎嗔",
            ampOnSettle: e.ampOnSettle ?? 0.08,
            ampMaxStack: e.ampMaxStack ?? 99,
          };
          if (TRACE) lg(`　▸ ${u.nm} 陷入虎嗔`);
        }
        else if (k === "dot") {
          // 批52续: e.coefLeader —— 主將時更高傷害率
          let dotCoef = e.coef ?? 0.5;
          if (e.coefLeader != null && allies && allies[0] === caster) dotCoef = e.coefLeader;
          // 禁近似令-批K: e.coefFromStack(dynamic_coef_from_counter族) —— coef不是固定值,
          // 而是「基礎值+每層增量×caster身上具名疊層計數器(見k==="stat"+e.stackKey+e.stackId
          // 消費端寫入ampLayersById)的當下層數」。絕地反擊「第5回合根據(自己受兵刃傷害觸發的)
          // 疊加次數對敵軍全體造成傷害(60%→120%,每次+7%→14%)」——取代舊有「第5回合單一觸發
          // 用EV rate=0.125折算全戰鬥期望值」的近似, 改為真正逐次疊加後、在第5回合精確讀取
          // 當下疊層數代入coef公式(base+per×layers)。
          if (e.coefFromStack) {
            const layers = (caster.ampLayersById && caster.ampLayersById[e.coefFromStack.id]) || 0;
            dotCoef = (e.coefFromStack.base ?? 0) + (e.coefFromStack.per ?? 0) * layers;
          }
          // 禁近似令-批K: e.pierce:true —— 「無視防禦」(獅子奮迅叛逃狀態), 強制本段 dot 傷害
          // 完全無視目標 mitig(見 damage() 的 forcePierce 第9參數), 與 caster 自身的 pierce
          // 累加值(會影響caster所有傷害來源)無關, 只影響這一個dot段本身。
          // 批52g: dots[3]=具名狀態(水攻/沙暴…)
          // 禁近似令-批L: dotStatusName 只解析一次, 同時餵給 damage()(供 e.dmgFromStatus 過濾,
          // 才辯機捷)與 dots[3](既有具名狀態標籤, 供 ifTargetHas/rateStatusBonus 等既有消費端),
          // 確保兩處讀到的是同一份名稱, 不會出現「兩套獨立解析結果不一致」的情形。
          const dotStatusName = resolveDotName(e, t);
          u.dots.push([damage(caster, u, dotCoef, e.kind || t.kind || "intel", undefined, undefined, undefined, undefined, !!e.pierce, dotStatusName), e.dur, !!e.undispellable, dotStatusName]);
        }
        else if (k === "extra") u.pushAdd("extra", e.val, e.dur, src);
        // 禁近似令-批K: splash(splash_aoe_primitive族) —— 「普攻命中目標時, 濺射傷害給目標
        // 同部隊其他武將」的真群攻, 取代extra(額外傷害輸出, 施放者視角常駐加成)近似(瞋目橫矛/
        // 象兵「群攻(普攻時對同部隊其他武將濺射70%/50%傷害)」)。val=濺射傷害率(相對於普攻本身
        // 100%的比例), 累加式(pushAdd, 同extra/pierce既有慣例), 消費端見 doNormalAttack()。
        else if (k === "splash") u.pushAdd("splash", e.val, e.dur, src);
        // 批26 B2: e.stackPer(可選, "round"預設/"cast") —— 過去疊層只有「每回合+1層」(見下方
        // fight() 主迴圈 tick 遞增), 原文常見「每次發動後傷害率提升X」(如水淹七軍/陷陣突襲)是
        // 「本戰法每次成功發動」才+1層, 與回合數無關。"cast"模式改由 applyStackCast() 在戰法
        // 命中/發動結算處呼叫遞增(見 fight() fire 分支)。刻意不覆寫既有 e.per 語意(per 一直是
        // "每層增傷倍率"數值欄位), 新增獨立欄位避免型別混淆。
        // 批37 B: 第三種遞增時機 "attack" —— 「每次普通攻擊後+1層」(如奮突「普通攻擊之後...
        // 最多疊加3次」), 掛在 dealtDamage 事件點(普攻確實命中造成傷害後遞增, 見 dealtDamage()
        // 頂端), 繳械/震懾無普攻的回合不會誤疊層(較舊的 round 近似精確)。
        else if (k === "stack") {
          // 批52续: 已有 stack 不重置 n(避免 charge 重入清零)
          const sp = e.stackPer || "round";
          if (u.stack && u.stack.stackPer === sp) {
            u.stack.per = e.per ?? u.stack.per;
            u.stack.max = e.max ?? u.stack.max;
          } else {
            u.stack = { per: e.per ?? 0.1, max: e.max ?? 5, n: 0, stackPer: sp };
          }
        }
        else if (k === "decay") u.decay = { v0: e.v0 ?? 0.5, left: e.rounds ?? 8, total: e.rounds ?? 8 };
        // 禁近似令-批K: preDmgHook 註冊 —— 見 Unit 建構式 this.preDmgHooks 詳細註解與 damage()
        // 消費端。e.hookKind 決定方向與語意(probVoid=攻擊方自己掛/probMitig,stepMitig,
        // deferSettle=防禦方自己掛), who 決定掛在誰身上(挫銳 who:"enemy" 掛在目標敵人身上,
        // 之後該敵人自己出手攻擊時消費 src.preDmgHooks; 承天靖世/象兵/捨身救主 who:"self"或
        // "ally" 掛在自己/我軍身上, 之後受到攻擊時消費 dst.preDmgHooks)。
        else if (k === "preDmgHook") {
          u.preDmgHooks.push({
            hookKind: e.hookKind, val: e.val, step: e.step, max: e.max, hits: 0,
            rate: e.rate, dmgType: e.dmgType, pct: e.pct, delayRounds: e.delayRounds,
            reducePct: e.reducePct, dur: (e.dur ?? 99) + 1,
          });
          if (TRACE) lg(`　▸ ${u.nm} 獲得傷害結算前攔截〔${e.hookKind}〕`);
        }
        // 禁近似令-批K: k==="preAttackHook" 註冊 —— 見 Unit 建構式 this.preAttackHooks 詳細
        // 註解與 doNormalAttack() 消費端。e.hookKind: "redirectPre"(即將受到普攻時,依guard
        // 準則轉由隊友代承, 雲聚影從)/"healAllyPre"(即將受到普攻時,治療隨機隊友, 益其金鼓)。
        // e.rate=每次觸發機率(每次普攻前重新擲骰, 非prep一次性), dur=常駐(整場戰鬥有效)。
        else if (k === "preAttackHook") {
          u.preAttackHooks.push({ hookKind: e.hookKind, rate: e.rate, guard: e.guard, coef: e.coef, scale: e.scale, dur: (e.dur ?? 99) + 1 });
          if (TRACE) lg(`　▸ ${u.nm} 獲得即將受擊觸發〔${e.hookKind}〕`);
        }
        else if (k === "swap") u.swap = Math.max(u.swap, (e.dur ?? 1) + 1);
        // 禁近似令-批K: e.onKill(engine_wiring_gaps_misc族) —— 不立即套用pierce, 改登記到
        // u.onKillGrants(待hit()偵測到u親手擊敗某目標時才真正授予, 見hit()消費端), 供虎痴
        // 「如果擊敗目標，會使自身獲得破陣狀態，直到戰鬥結束」精確表達條件觸發時機(取代舊有
        // 「首回合即常駐生效, 無條件全程無視減傷」的高估)。
        else if (k === "pierce" && e.onKill) { u.onKillGrants = u.onKillGrants || []; u.onKillGrants.push({ kind: "pierce", val: e.val, dur: 9999 }); }
        else if (k === "pierce") u.pushAdd("pierce", e.val, e.dur, src);
        // 批A(11筆高嚴重重建): chargeAdd —— 「可消耗資源池」的獲得端(死戰不退「自身受到傷害時,
        // 有80%機率獲得一層蓄威效果,可累積20層」), 對稱既有stack但語意不同(見Unit建構式
        // this.charge註解: charge是「剩餘可消耗次數」的資源池, 非傷害增益倍率)。掛在
        // on:"damaged"反應式(見onHitFor/onHit()呼叫端), e.rate已在外層(反應式擲骰通道)判定
        // 過, 這裡只需純粹+1層封頂(不重複擲骰)。
        else if (k === "chargeAdd") {
          if (!u.charge) u.charge = { n: 0, max: e.max ?? 20 };
          u.charge.max = e.max ?? u.charge.max;
          u.charge.n = Math.min(u.charge.max, u.charge.n + 1);
          if (TRACE) lg(`　▸ ${u.nm} 蓄威+1層(現${u.charge.n}/${u.charge.max})`);
        }
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
            // 禁近似令-批K: e.debuffAttacker/e.selfStack(counter_target_binding族) —— guardFor
            // 反擊觸發後, 「同一次」額外副作用精確綁定到「觸發本次反擊的攻擊者」(古之惡來
            // 「使其造成兵刃傷害降低9%→18%」——同一次猛擊命中的那個攻擊者本人, 而非泛用
            // who:enemy全體)或反擊執行者「自己」的疊層增益(虎衛軍「副將提高6→12武力,最多
            // 提高5次」——每次成功反擊+1層, 累加, 見 hit() counterGuards 消費端一併處理這兩
            // 個欄位)。原樣透傳到 counterGuards 條目上, 供 hit() 內對 src(攻擊者)/gu(反擊者)
            // 直接施加, 不經過 applyEffects 的 who/dests 派發(hit()無隊伍context, 見 hit()
            // 內consumeGuardExtra() 詳細註解)。
            allies[0].counterGuards.push({ unit: u, coef: e.coef ?? 1, kind: e.kind || "phys", prob: e.prob ?? 1, debuffAttacker: e.debuffAttacker || null, selfStack: e.selfStack || null });
          } else {
            // 批G: e.normalOnly —— 對稱既有redirect(guardNormalOnly)/amp/mitig已支援的
            // normalOnly慣例, 限定此反擊只在受到普通攻擊(isNormal=true)時觸發, 省略時向後
            // 相容(任意傷害來源皆可觸發反擊)。荊棘「受到普通攻擊時，反彈5%傷害」需要此限定。
            u.counter = { coef: e.coef ?? 1, kind: e.kind || "phys", prob: e.prob ?? 1, dur: (e.dur ?? 99) + 1, normalOnly: !!e.normalOnly };
          }
        }
        else if (k === "taunt") {
          // 禁近似令-批K: e.tauntTarget(force_attack_reverse族) —— 反向taunt, 被強制攻擊的
          // 目標不再永遠是「caster自己」, 可改指定:
          //   "leader": 目標=我方隊伍主將(武鋒陣「主將優先成為敵軍戰法目標」, dests=enemies,
          //     每個敵人u的tauntBy改指向allies[0]而非caster)。
          //   "select": 依e.targetSel從敵軍挑一個「被攻擊」的目標(定謀貴決「使敵軍兵力最高的
          //     武將嘲諷我軍全體」, dests=allies, 我方全隊tauntBy皆指向該敵人)。
          // 省略e.tauntTarget維持既有行為(u.tauntBy=caster), 向後相容既有全部taunt資料。
          let forceTarget = caster;
          if (e.tauntTarget === "leader") forceTarget = (allies && allies[0] && allies[0].alive) ? allies[0] : null;
          else if (e.tauntTarget === "select") forceTarget = e.targetSel ? pickByCriterion(enemies, e.targetSel) : null;
          if (forceTarget) { u.tauntBy = forceTarget; u.tauntDur = Math.max(u.tauntDur, (e.dur ?? 1) + 1); if (TRACE) lg(`　▸ ${u.nm} 被迫優先攻擊 ${forceTarget.nm}`); }
        }
        else if (k === "shield") {
          const amt = (e.amt ?? 0) + (e.pct ? e.pct * caster.troop : 0);
          u.shield = { amt: (u.shield ? u.shield.amt : 0) + amt, dur: (e.dur ?? 99) + 1, undispellable: !!e.undispellable };  // +1 補償: tick 施加當回合末即扣1, 與 taunt/dodge/surehit 慣例一致
        }
        else if (k === "dodge") { u.dodgeProb = e.prob ?? 0.2; u.dodgeDur = Math.max(u.dodgeDur, (e.dur ?? 1) + 1); u.dodgeDmgType = e.dmgType ?? null; }  // 批G: e.dmgType限定規避類型(榮光「受謀略傷害時完全免疫」等), 對稱amp/mitig/block既有dmgType過濾慣例
        // 批22: block(次數型格擋, 抵禦/警戒同族) —— times:N(剩餘次數), val:1.0全擋/0.x部分減傷
        // (如警戒基礎40%受智力影響)。同源(同一戰法名 src)再次施加時疊加次數(pushBlock 內部
        // 處理), 不像 pushAdd/pushMod 的「同源刷新覆蓋」慣例 —— 貼合戰報「目前抵禦總次數為N」
        // 的疊次語意。val 的 scale 縮放用 0~1 專屬 clamp(非 svVal 的 ±SCALE_CLAMP, 因 block
        // val 是「減傷比例」語意, 不應為負值或超過1.0全擋)。
        else if (k === "block") {
          // 批35 B: block 的 scale 縮放改用 lockedScaleOf(準備階段鎖定, 見上方常數區註解) 取代
          // 直接呼叫 scaleOf —— 同一效果物件(e)不論在 prep 或稍後 everyRound 補層才實際套用,
          // 縮放倍率都固定用「第一次掃描到該效果時」(即 prep 階段, 見 applyEffects 內
          // e.everyRound 閘門的 primeScaleLock 呼叫)算出的值, 不隨戰鬥中智力變動重算。
          // 批35 A: capValOf 套用 e.capVal(值上限, 縮放後 clamp), 在既有 0~1 clamp 之前先夾一次
          // (機鑑先識 val:0.4 capVal:0.8 → 最終仍受 min(1) 保護, 但 capVal 通常更嚴格先生效)。
          const bVal = e.scale ? Math.max(0, Math.min(1, capValOf((e.val ?? 1.0) * lockedScaleOf(caster, e), e.capVal))) : (e.val ?? 1.0);
          u.pushBlock(bVal, e.times ?? 1, src, e.dmgType);  // 批G: e.dmgType 限定格擋類型(榮光「受謀略傷害時完全免疫」等), 省略時向後相容(不分類型)
          if (TRACE) lg(`　▸ ${u.nm} 獲得${bVal >= 1 ? "抵禦" : `警戒(減傷${Math.round(bVal * 100)}%)`}(${e.times ?? 1}次)`);
        }
        else if (k === "surehit") u.surehitDur = Math.max(u.surehitDur, (e.dur ?? 1) + 1);
        else if (k === "healblock") { if (!u.isImmuneTo("healblock")) u.healblock = Math.max(u.healblock, (e.dur ?? 1) + 1); }  // 批8: 禁療 —— heal 套用處(applyEffects 開頭)已排除 healblock 中的目標; 批C: isImmuneTo("healblock")查詢方法自批16即存在但施加端從未讀取(對稱ambush的既有寫法, 見上方k==="ambush"分支), 補上判斷式使k=="immune"(types含healblock)真正生效
        else if (k === "lifesteal") u.pushAdd("lifesteal", e.val, e.dur, src);  // 批8: 倒戈 —— 實際回血在 hit() 結算傷害後(見 hit() 內 lifesteal 段), 這裡只掛加成值
        else if (k === "rateup") {                       // 提高(自身或對象)主動戰法發動機率
          // scale: 施放當下(caster 戰鬥內即時素質)用 rateScaleOf(獨立於全域 SCALE) 縮放實際加成
          // (批7: 太平道法「受智力影響」, 見 docs/data/calibration_anchors.json → rate_scale)。
          // prepOnly/nativeOnly/inheritedOnly(批8, nativeOnly反向) 修飾旗標存進 adds[4], 由
          // addbonusFor() 在主動擲骰處依戰法屬性篩選加總。批46 A: e.scaleDiv(選填) —— 覆蓋預設
          // 除數384.6, 供不同曲線族的rateup戰法各自標記(見十二奇策 scaleDiv:335, calibration_
          // anchors.json → shierqice_20260707)。
          const rv = e.scale ? e.val * rateScaleOf(caster, e.scale, e.scaleDiv) : e.val;
          const flags = (e.prepOnly || e.nativeOnly || e.inheritedOnly) ? { prepOnly: !!e.prepOnly, nativeOnly: !!e.nativeOnly, inheritedOnly: !!e.inheritedOnly } : undefined;
          // 同一戰法(如太平道法)可能有多條 rateup(一般 + prepOnly 額外), src 相同的話 pushAdd
          // 的「同kind+同src刷新」去重會把前一條蓋掉; 用 flags 組出不同的 dedup key 尾碼區分,
          // 讓語意不同的兩條並存, 但同語意(同flags組合)的仍正常刷新不疊加。
          const rSrc = (src && flags) ? src + ":" + ["prepOnly", "nativeOnly", "inheritedOnly"].filter(f => flags[f]).join("") : src;
          // 禁近似令-批L: e.maxStack/e.maxStackIfLeaderIs(可疊加N次) —— 先登死士「降低其
          // 1.5%→3%主動戰法發動率,可疊加4次(若麴義統領則5次)」, val用負值表達「降低」(見
          // damage()呼叫端fight()主迴圈的addbonusFor("rateup",...)對任意來源的rateup adds
          // 一視同仁加總, 負值天然表達debuff方向, 無需另立新k)。
          u.pushAdd("rateup", rv, e.dur, rSrc, flags, resolveMaxStack(caster, e, allies));
        }
        else if (k === "chargeup") {                    // 提高(自身或對象)突擊戰法發動機率; 排除 t.proc===true 特技偽戰法見突擊擲骰處註解
          // chargeup 同樣支援 scale(未有實測前與 rateup 共用預設曲線, 假設同曲線, 見上方常數註解); e.scaleDiv 比照 rateup 透傳(批46 A, 目前 chargeup 尚無獨立實測需要非預設曲線的樣本, 保留擴充點)
          const cv = e.scale ? e.val * rateScaleOf(caster, e.scale, e.scaleDiv) : e.val;
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
        // 批G: lifestealGiven(倒戈效果量×(1+val)) —— 對稱healGiven, 實際套用在hit()倒戈結算處。
        else if (k === "lifestealGiven") u.pushAdd("lifestealGiven", e.val, e.dur, src);
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

  function fight(POOL, teamA, troopA, teamB, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario, campLvA, campLvB) {
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
    // 批36: 兵種營等級(0~10, 隊伍級——全隊共用一座對應兵種的營, 與 troopA/troopB 同顆粒度)。
    campLvA = campLvA || 0; campLvB = campLvB || 0;
    // Lv10附贈戰法原文是「我軍隨機單體/群體」觸發一次(非每個單位各自擁有), 故隨機挑隊上1人
    // 當「持有者」(見 Unit 建構子 isCampHolder 參數), 該隊其餘人只吃屬性%加成、不重複附戰法。
    const holderIdxA = campLvA >= 10 ? Math.floor(rnd() * teamA.length) : -1;
    const holderIdxB = campLvB >= 10 ? Math.floor(rnd() * teamB.length) : -1;
    const factionsA = teamA.map(n => POOL[n].faction), factionsB = teamB.map(n => POOL[n].faction);  // 批24 D1: teamGate 判定依據(隊伍全體陣營陣列)
    const A = teamA.map((n, i) => Object.assign(new Unit(POOL[n], troopA, bsA[i], eqA[i], addA[i], inhA[i], seasonModsFor(POOL, POOL[n], i, teamA, scenario), factionsA, campLvA, i === holderIdxA), { nm: n, side: "我" }));
    const B = teamB.map((n, i) => Object.assign(new Unit(POOL[n], troopB, bsB[i], eqB[i], addB[i], inhB[i], seasonModsFor(POOL, POOL[n], i, teamB, scenario), factionsB, campLvB, i === holderIdxB), { nm: n, side: "敵" }));
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
    // 批38 A: 跨單位事件廣播 —— e.when.who/t.when.who(選填, 預設"self"向後相容零變化)。
    // 過去 onHit/dealtDamage/activeFired 只掃描「事件發生的那個單位自己」攜帶的反應式戰法/
    // 效果(見上方各陣列皆以「持有者=事件單位本身」為前提預篩+觸發), 無法表達「任一友軍受擊/
    // 造成傷害/發動主動戰法時, 我(持有者, 可能是另一個單位)也跟著觸發」這類跨單位監聽語意
    // (歷輪盲測點名最大殘餘原語缺口, 見 engine_limitations.md 21/27節「跨單位事件廣播」
    // 未解決缺口列表: 虎侯/十二奇策/經天緯地/神機妙算/舌戰群儒/騎虎難下等)。
    // who:"ally" —— 監聽對象是「事件單位所在隊伍的任一人(含事件單位自己)」, 持有者也必須
    // 在同一隊(廣播範圍限同隊, 不含對面)。who:"otherAlly" —— 同"ally", 但明確排除事件單位
    // 自己(對應原文「除自己之外的友軍...時」這類措辭, 如騎虎難下)。who:"enemy" —— 監聽對象
    // 是「事件單位所在隊伍的敵對隊伍任一人」, 持有者必須在敵對那一隊。實作: broadcastHolders
    // (evtUnit, who) 回傳所有「應該被視為此事件監聽對象」的持有者候選隊伍(ally/otherAlly→
    // evtUnit自己隊伍全體; enemy→evtUnit的敵隊全體), self(未指定或"self")仍走原本「持有者
    // ===事件單位」路徑, 不受此廣播擴充影響(零回歸)。效能: 每次事件多掃隊伍其餘1~2人的預篩
    // 陣列(已是O(小常數)), 3000場預算內可忽略(見驗收效能數字)。
    const broadcastHolders = (evtUnit, who) => {
      if (who === "ally" || who === "otherAlly") return alliesOf(evtUnit);
      if (who === "enemy") return foesOf(evtUnit);
      return null;                                    // "self"/未指定: 不走廣播, 呼叫端維持原本 dst/src 自身路徑
    };
    const onHit = (dst, src, isNormal, dmg, kind) => {          // 反應式觸發(when.on): 被普攻(attacked)/受任意傷害(damaged) 時掛到 hit() 事件點; 批33: dmg(可選, 尾端新增)—— 本次觸發事件的實際傷害量, 供 e.ofDamage 傷害比例治療使用; 批38 A: 新增who:"ally"/"otherAlly"/"enemy"跨單位廣播(見上方broadcastHolders/onHitFor); 批39 C: 新增kind(可選, 尾端新增, 向後相容既有呼叫點皆未傳)—— 本次傷害類型(phys/intel), 供when.dmgType/e.when.dmgType過濾(對稱dealtDamage的dmgTypeOk)
      const dmgTypeOk = dt => !dt || dt === kind;  // dmgType 過濾: 未指定該欄位視為兵刃/謀略皆可觸發(向後相容), 與dealtDamage的dmgTypeOk同慣例
      const onHitFor = (dst, src, isNormal, dmg, holder, wantWho) => {  // 批38 A: 抽出可重用核心 —— holder(效果持有者)可能不同於dst(受擊/受傷的事件單位本身), wantWho: 本次呼叫只處理與此匹配的when.who(undefined/"self"→只認持有者自身受擊之既有語意; "ally"/"enemy"→只認持有者從隊友/敵軍廣播監聽到的受擊事件, 避免self掃描與廣播掃描重複觸發同一條)
        if (!holder.alive || (!holder.onHitTacs.length && !holder.onHitEffectTacs.length && !holder.onHitEq.length && !holder.onHitBs.length)) return;
        if (holder.fakeReportDur) return;             // 批16: 偽報 —— 抑制 onHit 反應式觸發(被動/指揮戰法失效)
        // 批38 A: who:"otherAlly" 與 who:"ally" 共用同一組廣播候選隊伍(見broadcastHolders),
        // 差別只在「事件單位本身是否算數」——otherAlly 明確排除 holder===dst(受擊者自己)的
        // 情形(對應原文「除自己之外」), ally 則含自己。用 whoOk 統一比對 w.who 是否等於
        // wantWho(嚴格字串相等, "otherAlly"!=="ally", 兩者是各自獨立的 when.who 值, 不會
        // 互相誤放行), 再另外用 holder!==dst 這條額外閘門收斂 otherAlly 的範圍。
        const whoOk = w => (w && w.who) === wantWho || (!wantWho && !(w && w.who));  // wantWho未傳(undefined)時只放行無who欄位(含"self")的既有寫法; 傳"ally"/"otherAlly"/"enemy"時只放行明確標記該值的新寫法
        const otherAllyOk = () => holder !== dst;      // who:"otherAlly" 額外要求: 持有者不是本次事件單位自己
        // 批A(11筆高嚴重重建): dmgAbove(可選數值) —— 對稱dealtDamage同名旗標(見其註解), 這裡
        // 是「受到傷害超過X」句型(承天靖世「我軍收到高於最大兵力6%的傷害時」, dmgAbove:600,
        // START_TROOP=10000×6%)的傷害量閾值閘門, dmg為此次onHit事件的實際傷害量。
        const dmgAboveOk = w => w.dmgAbove == null || (dmg != null && dmg > w.dmgAbove);
        for (const t0 of holder.onHitTacs) {
          if (!whoOk(t0.when)) continue;
          if (wantWho === "otherAlly" && !otherAllyOk()) continue;
          if (t0.when.on === "attacked" && !isNormal) continue;   // attacked: 限普通攻擊觸發; damaged: 任意傷害都觸發
          if (!dmgTypeOk(t0.when.dmgType)) continue;  // 批39 C: 戰法級when.dmgType過濾(如剛勇無前/剛烈不屈「受到兵刃傷害時」限定)
          if (!dmgAboveOk(t0.when)) continue;
          // 批22: when.on 反應式戰法過去完全不檢查 rounds/from/until/parity/every(只認 on 事件本身),
          // 導致「戰鬥首回合獲得急救(受傷時回血)」這類「反應式觸發+回合窗口限定」的複合語意無法
          // 表達(如 長健/青囊書: 首回合內受傷才會回血, 而非全程)。roundOk() 對「無 rounds/from/
          // until/parity/every」的戰法一律回傳 true(見其實作), 故此檢查對絕大多數既有 when.on
          // 戰法(只帶 on, 無回合欄位)是無副作用的 no-op, 只在新資料明確加上回合窗口時才生效。
          if (!roundOk(t0, CUR_R)) continue;
          if (holder.hitFlags.has(t0)) continue;             // 同回合每單位每戰法最多觸發1次(防無限鏈), 鍵用t0(戰法原始物件)不受choices合成視圖影響
          // 批C: t.rateLeader —— 主將時採用較高觸發率(對稱既有active型戰法頂層rateLeader分派,
          // 見fight()主迴圈「批52续: t.rateLeader」段; 淵然難測發現此欄位雖已存在於資料但從未
          // 被本反應式onHitFor()讀取, 是「資料寫了但引擎端遺漏對應讀取」的死欄位, 本次補上)。
          let fireRate = t0.rate;
          if (t0.rateLeader != null && alliesOf(holder) && alliesOf(holder)[0] === holder) fireRate = t0.rateLeader;
          // 批52: rateScaleIfGender —— 女性持有者觸發率受 rateScale 屬性縮放(魅惑)
          if (t0.rateScaleIfGender && t0.rateScale) {
            const gmap = { "男": "Male", "女": "Female", Male: "Male", Female: "Female", male: "Male", female: "Female" };
            const want = gmap[t0.rateScaleIfGender] || t0.rateScaleIfGender;
            const got = gmap[(holder.g && holder.g.gender) || ""] || ((holder.g && holder.g.gender) || "");
            if (got === want) fireRate = t0.rate * rateScaleOf(holder, t0.rateScale, t0.rateScaleDiv);
          }
          if (rnd() >= fireRate) continue;
          holder.hitFlags.add(t0);
          // 批27 C: choices(擇一分支) —— 過去 onHit() 反應式路徑完全不讀 t0.choices(見
          // engine_limitations.md §8: 魅惑「混亂/計窮/虛弱」三選一只能固定選其中一種, choices
          // 寫入也不會被消費), 主動/指揮/被動的常駐輪詢派發路徑(fight()主迴圈)已支援 choices,
          // 這裡補上同一套邏輯(先 pickChoice 選分支, 再用合成視圖 t 讀 coef/kind/effects/
          // extraHits, t0 保留給 hitFlags 去重/roundOk 等以物件本身為鍵的邏輯, 不受選分支影響)。
          const t = t0.choices ? Object.assign({}, t0, pickChoice(t0.choices)) : t0;
          if (TRACE) lg(`【${holder.side}】${holder.nm} 戰法【${t.nameZh}】（${holder === dst ? "受擊觸發" : "友軍/敵軍受擊觸發"}）發動`);
          if (t.coef) hit(holder, src, t.coef, t.kind, false, onHit, dealtDamage);
          if (t.extraHits) fireExtraHits(holder, t, src, alliesOf, foesOf, onHit, dealtDamage);  // 批13: 受擊觸發類多段傷害(如剛烈不屈 反擊後群體額外段)
          if (t.effects.length) applyEffects(holder, src, t, alliesOf(holder), foesOf(holder), { reactive: true, dmg, evtTarget: dst });  // 批23: 戰法級when.on本身即反應式, 標記reactive供內部e.when.on效果(若有)一致判定; 批33: 傳入dmg供e.ofDamage使用; 批42: evtTarget=dst(事件單位本身)供who:"eventTarget"精確選標(如傲睨王侯"敵軍目標受普攻時,該目標降3%")
        }
        // 批22: 效果級 e.when.on(急救類反應式治療, 見 onHitEffectTacs 註解) —— 戰法本身無 t.when
        // (其餘效果如武力/統率平加仍在 prep 正常套用, 不受影響), 只有帶 e.when.on 的個別效果在
        // 此處反應式結算。用「合成單效果戰法」(effects:[e])呼叫 applyEffects, 讓 heal 分支的
        // 傷兵池/healBoost/healGiven 邏輯完整適用, 觸發率取 e.rate ?? t.rate ?? 1(效果自身優先,
        // 無則沿用戰法整體 rate)。去重鍵用效果物件本身(而非戰法物件), 因同一戰法可能有多個
        // e.when.on 效果, 需各自獨立節流(防同回合多次觸發同一效果)。
        for (const t of holder.onHitEffectTacs) {
          for (const e of t.effects) {
            if (!e.when || !e.when.on) continue;
            if (!whoOk(e.when)) continue;
            if (wantWho === "otherAlly" && !otherAllyOk()) continue;
            if (e.when.on === "attacked" && !isNormal) continue;
            if (!dmgTypeOk(e.when.dmgType)) continue;  // 批39 C: 效果級when.dmgType過濾
            if (!dmgAboveOk(e.when)) continue;
            if (!roundOk({ when: e.when }, CUR_R)) continue;
            if (holder.hitFlags.has(e)) continue;
            // 禁近似令-批K: e.once(反應式on:damaged/on:dealtDamage/on:activeFired/on:healed
            // 皆共用此效果級迴圈) —— hitFlags只提供「同回合節流」(見holder.hitFlags每回合
            // tick()清空), 無法表達「整場戰鬥內只消耗一次」(誓守無降「自身2回合內受到下一次
            // 謀略傷害時,計窮敵軍主將」的『下一次』=單次消耗, 而非每回合都可能重新觸發)。
            // whenFired(見Unit建構式)是「效果物件去重, 不隨回合重置」的既有欄位(既有when.rounds
            // /e.once等機制早已使用), 借用同一份持久化去重狀態表達reactive路徑的一次性消耗。
            if (e.once && holder.whenFired.has(e)) continue;
            const evRate = e.rate ?? t.rate ?? 1;
            if (rnd() >= evRate) continue;
            holder.hitFlags.add(e);
            if (e.once) holder.whenFired.add(e);
            if (TRACE) lg(`【${holder.side}】${holder.nm} 戰法【${t.nameZh}】急救效果（${holder === dst ? "受擊觸發" : "友軍/敵軍受擊觸發"}）發動`);
            applyEffects(holder, src, { effects: [e], kind: t.kind || "phys", nameZh: t.nameZh }, alliesOf(holder), foesOf(holder), { rateChecked: true, reactive: true, dmg, evtTarget: dst });  // 批23 A4: 這裡已擲過 e.rate, 避免 applyEffects 通用閘門重複擲骰; reactive:true 供內部 e.when.on 閘門判定放行; 批33: dmg供e.ofDamage使用; 批42: evtTarget供who:"eventTarget"
          }
        }
        // 批22: 裝備效果級 e.when.on(見 onHitEq 註解) —— 同上, 用合成單效果戰法呼叫 applyEffects
        for (const e of holder.onHitEq) {
          if (!whoOk(e.when)) continue;
          if (wantWho === "otherAlly" && !otherAllyOk()) continue;
          if (e.when.on === "attacked" && !isNormal) continue;
          if (!dmgTypeOk(e.when.dmgType)) continue;  // 批39 C: 裝備效果級when.dmgType過濾
          if (!roundOk({ when: e.when }, CUR_R)) continue;
          if (holder.hitFlags.has(e)) continue;
          const evRate = e.rate ?? 1;
          if (rnd() >= evRate) continue;
          holder.hitFlags.add(e);
          if (TRACE) lg(`【${holder.side}】${holder.nm}〔特技·${e._eqNm || "?"}〕（${holder === dst ? "受擊觸發" : "友軍/敵軍受擊觸發"}）發動`);
          applyEffects(holder, src, { effects: [e], kind: "phys" }, alliesOf(holder), foesOf(holder), { rateChecked: true, reactive: true, dmg, evtTarget: dst });  // 批23 A4/reactive: 已擲過 e.rate; 批33: dmg供e.ofDamage使用; 批42: evtTarget供who:"eventTarget"
        }
        // 批22: 兵書效果級 e.when.on(見 onHitBs 註解) —— 同上, 用合成單效果戰法呼叫 applyEffects
        for (const e of holder.onHitBs) {
          if (!whoOk(e.when)) continue;
          if (wantWho === "otherAlly" && !otherAllyOk()) continue;
          if (e.when.on === "attacked" && !isNormal) continue;
          if (!dmgTypeOk(e.when.dmgType)) continue;  // 批39 C: 兵書效果級when.dmgType過濾
          if (!roundOk({ when: e.when }, CUR_R)) continue;
          if (holder.hitFlags.has(e)) continue;
          const evRate = e.rate ?? 1;
          if (rnd() >= evRate) continue;
          holder.hitFlags.add(e);
          if (TRACE) lg(`【${holder.side}】${holder.nm}〔兵書〕（${holder === dst ? "受擊觸發" : "友軍/敵軍受擊觸發"}）發動`);
          applyEffects(holder, src, { effects: [e], kind: "phys" }, alliesOf(holder), foesOf(holder), { rateChecked: true, reactive: true, dmg, evtTarget: dst });  // 批23 A4/reactive: 已擲過 e.rate; 批33: dmg供e.ofDamage使用; 批42: evtTarget供who:"eventTarget"
        }
      };
      if (!dst.alive) return;
      onHitFor(dst, src, isNormal, dmg, dst, undefined);   // 既有語意: 持有者=事件單位自己(who未指定/"self")
      // 批38 A: 廣播 —— dst(受擊/受傷的那個單位)所在隊伍的隊友(who:"ally"持有者)與敵隊
      // (who:"enemy"持有者)也一併掃描。候選陣列含 dst 自己, 但 whoOk() 只放行明確 who
      // 欄位匹配的條目, 不會與上面的 self 路徑(whoOk 要求 who 欄位為空)重複觸發同一筆。
      for (const holder of alliesOf(dst)) onHitFor(dst, src, isNormal, dmg, holder, "ally");
      for (const holder of alliesOf(dst)) onHitFor(dst, src, isNormal, dmg, holder, "otherAlly");  // 批38 A: who:"otherAlly"(排除dst自己, 見騎虎難下「除自己之外的友軍受到普通攻擊時」)
      for (const holder of foesOf(dst)) onHitFor(dst, src, isNormal, dmg, holder, "enemy");
    };
    const dealtDamage = (src, dst, isNormal, kind, dmg) => {  // 批27 A: 反應式觸發(when.on:"dealtDamage") —— 自己造成傷害(對 dst)後掛到 hit() 事件點, 與 onHit(自己受擊視角)對稱; 批33: dmg(可選, 尾端新增)—— 本次觸發事件的實際傷害量, 供 e.ofDamage 使用; 批38 A: 新增who:"ally"/"enemy"跨單位廣播(對稱onHit, 見 broadcastHolders/dealtDamageFor)
      // 批37 B: stackPer:"attack" —— 「每次普通攻擊後疊加1層」(如奮突「普通攻擊之後...最多
      // 疊加3次」)。過去只有 "round"(逐回合)/"cast"(每次發動)兩種遞增模式, 普攻疊層只能用
      // round 近似(繳械/震懾回合無普攻仍會錯誤地繼續疊層)。掛在 dealtDamage 事件點(普攻確實
      // 命中造成傷害後), 置於 onDealTacs 早退判斷之前(有 stackPer:"attack" 疊層的單位未必
      // 同時有 when.on:"dealtDamage" 反應式戰法, 不能被該早退擋掉)。此段是src自身狀態變化,
      // 與who廣播無關, 維持只對src本身執行, 不隨廣播迴圈重複執行。
      if (isNormal && src.alive && src.stack && src.stack.stackPer === "attack") src.stack.n = Math.min(src.stack.max, src.stack.n + 1);
      const dealtDamageFor = (src, dst, isNormal, kind, dmg, holder, wantWho) => {  // holder: 效果持有者(可能不同於src本身); wantWho: 同onHitFor慣例
        // 批G: 早退判斷補上onDealEq(裝備效果級dealtDamage), 否則只帶裝備級dealtDamage反應式
        // (無戰法級onDealTacs/onDealEffectTacs)的持有者會在此處被提前擋掉, 永遠進不到下方
        // onDealEq迴圈(衝陣「首回合首次造成傷害時」若無其他戰法級dealtDamage反應式戰法陪同,
        // 會被此早退邏輯完全跳過)。
        if (!holder.alive || (!holder.onDealTacs.length && !holder.onDealEffectTacs.length && !holder.onDealEq.length)) return;
        if (holder.fakeReportDur) return;             // 批16: 偽報 —— 抑制反應式觸發(被動/指揮戰法失效), 與 onHit 同慣例
        const whoOk = w => (w && w.who) === wantWho || (!wantWho && !(w && w.who));
        const dmgTypeOk = dt => !dt || dt === kind;   // dmgType 過濾: 未指定視為兵刃/謀略皆可觸發(向後相容)
        // 批A(11筆高嚴重重建): dmgAbove(可選數值) —— 「造成大於X的傷害時」句型(密計誅逆「當我軍
        // 主將造成大於300的傷害時」)的傷害量閾值閘門, 對稱既有dmgType(傷害種類過濾)。dmg為
        // undefined時(理論上dealtDamage事件必傳, 保守起見仍防呆)視為不通過(嚴格>比較, 0/undefined
        // 皆不算超過任何正數門檻)。戰法級/效果級皆支援(下方兩處呼叫點)。
        const dmgAboveOk = w => w.dmgAbove == null || (dmg != null && dmg > w.dmgAbove);
        // casterIsLeader(見activeFiredFor同名旗標註解) —— dealtDamage事件的「觸發者」是src
        // (造成本次傷害的那個單位), 與holder(廣播後的持有者, 可能是隊友)分開; 密計誅逆「我軍
        // 主將造成傷害」要求src本身是其隊伍主將, 而非holder是主將。
        const casterIsLeaderOk = w => !w.casterIsLeader || (alliesOf(src)[0] === src);
        for (const t of holder.onDealTacs) {           // 戰法級: 整個戰法都是「造成傷害時」反應式(如白衣渡江拆成兩個獨立戰法段時可用此形式)
          if (!whoOk(t.when)) continue;
          if (!dmgTypeOk(t.when.dmgType)) continue;
          if (!dmgAboveOk(t.when)) continue;
          if (!casterIsLeaderOk(t.when)) continue;
          if (t.when.normalOnly && !isNormal) continue; // 批37 B: when.normalOnly —— 限「普通攻擊」造成的傷害才觸發(如奮突「普通攻擊之後」; dmgType:"phys" 無法區分普攻與兵刃戰法傷害, 需獨立旗標)
          if (!roundOk(t, CUR_R)) continue;
          if (holder.hitFlags.has(t)) continue;        // 同回合每單位每戰法最多觸發1次(防無限鏈), 與 onHit 共用同一 hitFlags(不同方向的觸發各自用不同t/e鍵, 不會互相誤判)
          if (rnd() >= t.rate) continue;
          holder.hitFlags.add(t);
          if (TRACE) lg(`【${holder.side}】${holder.nm} 戰法【${t.nameZh}】（${holder === src ? "造成傷害觸發" : "友軍/敵軍造成傷害觸發"}）發動`);
          // 批32 B: targetSel(依準則選標) —— 過去 dealtDamage 的 coef 傷害段固定命中 dst(觸發
          // 本次事件的同一目標), 沒有讀取 t.targetSel 這條路徑, 導致原文「對負傷最高之敵造成
          // 謀略傷害」(選標準則與觸發目標無關, 如監統震軍)只能被迫近似或完全不建模。比照主動
          // 戰法主迴圈既有的 targetSel 判斷式, 若戰法帶 targetSel 則改用準則選標, 找不到符合
          // 準則的目標時不出手(而非退回 dst, 避免誤傷/誤選)。
          if (t.targetSel) {
            const dv = pickByCriterion(foesOf(holder), t.targetSel);
            if (dv) hit(holder, dv, t.coef, t.kind, false, onHit, dealtDamage);
          } else if (t.coef) hit(holder, holder === src ? dst : (foesOf(holder)[0] || dst), t.coef, t.kind, false, onHit, dealtDamage);  // 廣播情形(holder!==src)下, 觸發事件的原始dst未必是holder的敵人(可能同隊), 退回holder自己的固定敵方位0近似選標(見同批B節遷移逐筆核對是否需要targetSel精確指定)
          if (t.extraHits) fireExtraHits(holder, t, holder === src ? dst : null, alliesOf, foesOf, onHit, dealtDamage);
          if (t.effects.length) applyEffects(holder, holder === src ? dst : null, t, alliesOf(holder), foesOf(holder), { reactive: true, dmg });  // 批33: 傳入dmg供e.ofDamage使用
        }
        // 效果級: 戰法本身有其他常駐效果, 只有部分效果段是「造成傷害時」反應式(如白衣渡江本身
        // 是常駐 command, disarm/silence 兩效果各自綁不同 dmgType, 與 onHitEffectTacs 同慣例)
        for (const t of holder.onDealEffectTacs) {
          for (const e of t.effects) {
            if (!e.when || e.when.on !== "dealtDamage") continue;
            if (!whoOk(e.when)) continue;
            if (!dmgTypeOk(e.when.dmgType)) continue;
            if (!dmgAboveOk(e.when)) continue;
            if (!casterIsLeaderOk(e.when)) continue;
            if (e.when.normalOnly && !isNormal) continue; // 批37 B: when.normalOnly(效果級) —— 同上, 限普攻傷害觸發
            if (!roundOk({ when: e.when }, CUR_R)) continue;
            // 批52e: everyHit/maxStack —— 每次造成傷害可同回合多次(文武雙全); 預設每效果每回合1次
            const multi = !!(e.everyHit || e.maxStack);
            if (!multi && holder.hitFlags.has(e)) continue;
            if (e.once && holder.whenFired.has(e)) continue;  // 禁近似令-批K: 見onHitFor同款e.once註解
            // 批52f: 預設不要求 dmg>0(抵禦/虛弱歸零仍算); 僅 e.requireDmg===true 才過濾
            if ((e.requireDmg != null ? e.requireDmg : false) && !(dmg > 0)) continue;
            const evRate = e.rate ?? t.rate ?? 1;
            if (rnd() >= evRate) continue;
            if (!multi) holder.hitFlags.add(e);
            if (e.once) holder.whenFired.add(e);
            if (TRACE) lg(`【${holder.side}】${holder.nm} 戰法【${t.nameZh}】效果（${holder === src ? "造成傷害觸發" : "友軍/敵軍造成傷害觸發"}）發動`);
            applyEffects(holder, holder === src ? dst : null, { effects: [e], kind: t.kind || "phys", nameZh: t.nameZh }, alliesOf(holder), foesOf(holder), { rateChecked: true, reactive: true, dmg });  // 已擲過 e.rate, 避免重複擲骰; reactive 供 e.when.on 閘門放行; 批33: dmg供e.ofDamage使用
          }
        }
        // 批G: 裝備效果級 e.when.on==="dealtDamage"(見 onDealEq 註解) —— 同上, 用合成單效果戰法
        // 呼叫applyEffects, 對稱onHitEq(受擊方向)的既有裝備級消費端。
        for (const e of holder.onDealEq) {
          const ew = e.when;
          if (!whoOk(ew)) continue;
          if (!dmgTypeOk(ew.dmgType)) continue;
          if (!dmgAboveOk(ew)) continue;
          if (!casterIsLeaderOk(ew)) continue;
          if (ew.normalOnly && !isNormal) continue;
          if (!roundOk({ when: ew }, CUR_R)) continue;
          if (holder.hitFlags.has(e)) continue;
          const evRate = e.rate ?? 1;
          if (rnd() >= evRate) continue;
          holder.hitFlags.add(e);
          // 批G: e.coef(可選)—— 對稱onDealTacs的t.coef直傷派發, 讓裝備效果級dealtDamage也能
          // 表達「附加一次額外傷害」(而非只能是amp/mitig/heal等buff類effects), 衝陣「首次造成
          // 傷害時附加一次額外兵刃傷害」需要此直接傷害輸出, 沿用觸發本次事件的同一目標dst。
          if (e.coef && holder === src && dst && dst.alive) hit(holder, dst, e.coef, e.kind || "phys", false, onHit, dealtDamage);
          applyEffects(holder, holder === src ? dst : null, { effects: [e], kind: "phys" }, alliesOf(holder), foesOf(holder), { rateChecked: true, reactive: true, dmg });
        }
      };
      if (!src.alive) return;
      dealtDamageFor(src, dst, isNormal, kind, dmg, src, undefined);  // 既有語意: 持有者=事件單位自己(who未指定/"self")
      // 批38 A: 廣播 —— src(本次造成傷害的那個單位)所在隊伍的隊友(who:"ally"持有者)與敵隊
      // (who:"enemy"持有者)也一併掃描。
      for (const holder of alliesOf(src)) dealtDamageFor(src, dst, isNormal, kind, dmg, holder, "ally");
      for (const holder of foesOf(src)) dealtDamageFor(src, dst, isNormal, kind, dmg, holder, "enemy");
    };
    const activeFired = (u) => {  // 批31 A: 反應式觸發(when.on:"activeFired") —— 自己成功發動主動/突擊戰法時掛到 fight() 主迴圈事件點, 與 dealtDamage(自己造成傷害視角)/onHit(自己受擊視角)對稱; 只認「自身」戰法成功fire這件事本身, 不要求造成傷害; 批38 A: 新增who:"ally"/"enemy"跨單位廣播(見 broadcastHolders/activeFiredFor) —— 解決十二奇策「我軍全體下次發動主動戰法後」/經天緯地「我軍全體發動主動/突擊戰法時」(who:"ally")、神機妙算/舌戰群儒「敵軍發動主動戰法時」(who:"enemy")這一族全庫最大殘餘原語缺口(見engine_limitations.md 21/27節)
      const activeFiredFor = (u, holder, wantWho) => {  // holder: 效果持有者(可能不同於u=實際發動主動戰法的單位); wantWho: 同onHitFor慣例
        if (!holder.alive || (!holder.activeFiredTacs.length && !holder.activeFiredEffectTacs.length && !holder.activeFiredBs.length)) return;
        if (holder.fakeReportDur) return;             // 批16: 偽報 —— 抑制反應式觸發(被動/指揮戰法失效), 與 onHit/dealtDamage 同慣例
        const whoOk = w => (w && w.who) === wantWho || (!wantWho && !(w && w.who));
        // 批A(11筆高嚴重重建): casterIsLeader —— 「(我軍)主將發動主動/突擊戰法時」這類措辭
        // (十勝十敗)要求觸發事件的u(實際發動者)本身必須是其隊伍主將(index 0), 而非「持有者
        // holder是主將」(who:"ally"廣播的holder未必等於u——十勝十敗常由非主將的副將攜帶,
        // 持有者篩選現有ifLeader/ifLeaderIs管的是holder自身的身份, 不是「這次事件是誰觸發的」)。
        // 用u所在隊伍(alliesOf(u))的index0比對u本身, 與holder是否為主將無關。
        const casterIsLeaderOk = w => !w || !w.casterIsLeader || (alliesOf(u)[0] === u);
        for (const t of holder.activeFiredTacs) {      // 戰法級: 整個戰法都是「(自身/我軍/敵軍)成功發動主動戰法時」反應式(如士爭先赴/十二奇策/神機妙算)
          if (!whoOk(t.when)) continue;
          if (!casterIsLeaderOk(t.when)) continue;
          if (!roundOk(t, CUR_R)) continue;
          if (holder.hitFlags.has(t)) continue;        // 同回合每單位每戰法最多觸發1次(防無限鏈), 與 onHit/dealtDamage 共用同一 hitFlags
          if (rnd() >= t.rate) continue;
          holder.hitFlags.add(t);
          if (TRACE) lg(`【${holder.side}】${holder.nm} 戰法【${t.nameZh}】（${holder === u ? "自身發動主動戰法觸發" : "友軍/敵軍發動主動戰法觸發"}）發動`);
          let mainHitTgt = null;
          if (t.coef) {
            const cnt = t.nMax ? (t.n + Math.floor(rnd() * (t.nMax - t.n + 1))) : t.n;
            const vs = pickTargets(foesOf(holder), cnt);
            // 批I(禁近似令-scale/比較族): t.scaleCompare —— 本段傷害係數依「施放者vs本次命中
            // 目標」同一屬性的差值額外縮放(神機妙算「並基於雙方智力差額外提高」), 對稱效果級
            // e.scale但讀取雙方差值而非施放者單一固定值, 見scaleCompareOf()。逐目標各自計算,
            // 無此欄位則行為完全不變(向後相容)。
            for (const v of vs) {
              const c = t.scaleCompare ? t.coef * scaleCompareOf(holder, v, t.scaleCompare) : t.coef;
              hit(holder, v, c, t.kind, false, onHit, dealtDamage, true);  // 批31 A: 本段傷害本身即「主動戰法發動觸發的反應式傷害」, isActive=true供同戰法/其他戰法的e.activeOnly amp判定
            }
            if (vs.length === 1) mainHitTgt = vs[0];
          }
          if (t.extraHits) fireExtraHits(holder, t, mainHitTgt, alliesOf, foesOf, onHit, dealtDamage);
          if (t.effects.length) applyEffects(holder, mainHitTgt, t, alliesOf(holder), foesOf(holder), { reactive: true });
        }
        // 效果級: 戰法本身有其他常駐效果, 只有部分效果段是「(自身/我軍/敵軍)成功發動主動戰法時」反應式
        for (const t of holder.activeFiredEffectTacs) {
          for (const e of t.effects) {
            if (!e.when || e.when.on !== "activeFired") continue;
            if (!whoOk(e.when)) continue;
            if (!casterIsLeaderOk(e.when)) continue;
            if (!roundOk({ when: e.when }, CUR_R)) continue;
            if (holder.hitFlags.has(e)) continue;
            const evRate = e.rate ?? t.rate ?? 1;
            if (rnd() >= evRate) continue;
            holder.hitFlags.add(e);
            if (TRACE) lg(`【${holder.side}】${holder.nm} 戰法【${t.nameZh}】效果（${holder === u ? "自身發動主動戰法觸發" : "友軍/敵軍發動主動戰法觸發"}）發動`);
            applyEffects(holder, null, { effects: [e], kind: t.kind || "phys", nameZh: t.nameZh }, alliesOf(holder), foesOf(holder), { rateChecked: true, reactive: true });  // 已擲過 e.rate, 避免重複擲骰; reactive 供 e.when.on 閘門放行
          }
        }
        // 禁近似令-批K: 兵書效果級 e.when.on==="activeFired"(見 activeFiredBs 註解) —— 同上,
        // 用合成單效果戰法呼叫 applyEffects, 對稱 onHitBs(受擊方向)的既有兵書級消費端。
        for (const e of holder.activeFiredBs) {
          if (!whoOk(e.when)) continue;
          if (!casterIsLeaderOk(e.when)) continue;
          if (!roundOk({ when: e.when }, CUR_R)) continue;
          if (holder.hitFlags.has(e)) continue;
          const evRate = e.rate ?? 1;
          if (rnd() >= evRate) continue;
          holder.hitFlags.add(e);
          if (TRACE) lg(`【${holder.side}】${holder.nm}〔兵書〕（${holder === u ? "自身發動主動戰法觸發" : "友軍/敵軍發動主動戰法觸發"}）發動`);
          applyEffects(holder, null, { effects: [e], kind: "phys" }, alliesOf(holder), foesOf(holder), { rateChecked: true, reactive: true });
        }
      };
      if (!u.alive) return;
      activeFiredFor(u, u, undefined);                 // 既有語意: 持有者=事件單位自己(who未指定/"self")
      // 批38 A: 廣播 —— u(本次成功發動主動/突擊戰法的單位)所在隊伍的隊友(who:"ally"持有者)
      // 與敵隊(who:"enemy"持有者)也一併掃描。
      for (const holder of alliesOf(u)) activeFiredFor(u, holder, "ally");
      for (const holder of foesOf(u)) activeFiredFor(u, holder, "enemy");
    };
    // 批52i: proxyNormal 代打完整普攻用
    _FIGHT_CTX = { onHit, onDeal: dealtDamage, alliesOf, foesOf, activeFired };
    if (TRACE) {                                    // 準備階段標頭: 兵種 + 城建/陣營
      CUR_R = 0;
      lg(`〔採用兵種〕我方 ${troopA}兵　·　敵方 ${troopB}兵`);
      lg(`〔城建滿〕全員 武智統速 各+${CITY}　〔陣營滿〕全屬性 +${Math.round((FACTION - 1) * 100)}%`);
      // 批36: 兵種營等級標頭 —— 僅任一方 campLv>0 才印(0=舊行為, 不多噪音); Lv10額外標註附贈戰法名(若有)
      if (campLvA > 0 || campLvB > 0) {
        const campNote = (lv, tt) => {
          if (!lv) return "無";
          const dmg = `+${(lv * CAMP_DMG_PER_LV * 100).toFixed(2)}%${tt}兵傷害`;
          const tacNm = lv >= 10 ? CAMP_TROOP_TACTIC[tt] : null;
          return `Lv${lv}（${dmg}${tacNm ? "　Lv10附贈【" + tacNm + "】" : ""}）`;
        };
        lg(`〔兵種營〕我方 ${campNote(campLvA, troopA)}　·　敵方 ${campNote(campLvB, troopB)}`);
      }
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
      // 批A(11筆高嚴重重建): chargeConsumedThisRound 逐回合歸零(對應死戰不退「每回合最多觸發5次」
      // 的回合窗口計數, 與蓄威層數charge.n本身跨回合累積不同, 這個計數器只管「這一回合已觸發
      // 過幾次消耗鏈」, 每回合開始重置)。
      for (const u of [...A, ...B]) if (u.alive) u.chargeConsumedThisRound = 0;
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
        if (u.stun || u.captured) { if (TRACE) lg(`【${u.side}】${u.nm} 被控制(${u.captured ? "捕獲" : "震懾"})，無法行動`); continue; }
        if (!pickTarget(foesOf(u))) break;
        if (u.silence && TRACE) lg(`【${u.side}】${u.nm} 陷入計窮，無法發動主動戰法`);
        if (!u.silence) for (const t0 of u.tactics) {   // 自帶 + 傳承: 各自獨立附加發動(計窮時跳過主動/指揮/被動)
          // 批16: fakeReport(偽報) —— 抑制指揮/被動每回合擲骰的coef段(prep已套用效果不回收, 不影響主動戰法)
          // 批52j: 捕獲禁用指揮與被動
          if ((t0.type === "command" || t0.type === "passive") && (u.fakeReportDur || u.captured)) continue;
          let fire = false;
          // 批18: choices/extraHits 派發 —— coef=0 且頂層 effects 為空、內容完全放在 choices[].effects
          // 或 extraHits 裡的主動戰法(如三選一分支型/上兵伐謀式多段指定選標), 過去
          // (t0.coef || t0.effects.length) 兩者皆假則永遠不會觸發(choices/extraHits 只在 fire 之後
          // 才被讀取, 若從未 fire 等於整個戰法失效 —— 全庫掃描發現暗潮洶湧/暗潮湧動已是此模式且
          // 從未真正發動過)。加上 t0.choices.length / t0.extraHits.length 這兩個額外判斷條件, 讓
          // 「內容全在 choices/extraHits 裡」的戰法也能正常擲骰派發。
          // 批32 R23: active 型戰法過去完全不檢查 t.when(roundOk), 只有 command/passive 分支
          // (下一行elif)才會擲骰前先驗回合窗口——導致需要 t.when.parity 切分奇偶互斥效果的
          // active 戰法(如飛沙走石)無法用頂層 when 精確表達。補上 roundOk(t0, r) 對稱於
          // command/passive 既有判斷, 對唯一既有帶 t.when 的 active 戰法(移花接木, when僅含
          // dur鍵)無回歸(roundOk 對未知鍵一律回傳true)。
          // 批52: 冷卻攔截 + 發動成功後寫入 cd+1
          const onCd = !!(t0.cd && u.tacCd && (u.tacCd[t0.nameZh] || 0) > 0);
          const isLeader = !!(alliesOf(u)[0] === u);
          let baseRate = t0.rate;
          if (isLeader && t0.rateLeader != null) baseRate = t0.rateLeader;
          // 批52i: rateScale 頂層發動率受屬性縮放
          if (t0.rateScale) baseRate = Math.min(1, baseRate * rateScaleOf(u, t0.rateScale, t0.scaleDiv));
          // 批52续: when + whenLeader(主將專屬額外回合)
          // 禁近似令-批K: firedViaLeaderWindow(leader_dual_base_coef族) —— 記錄本次fire是
          // 「透過whenLeader額外開放的回合視窗」通過, 而非base t.when本身通過, 供下方
          // t0.coefWhenLeader(僅在透過whenLeader視窗fire時才切換的coef分支)判斷(燕人咆哮
          // 「自身為主將時,第6回合對敵軍全體發動兵刃攻擊(44%→88%,不同於第2/4回合的104%)」
          // ——base視窗(第2/4回合)不論是否主將皆用基礎coef, 只有透過whenLeader開的額外視窗
          // (第6回合)才切換成不同的coefWhenLeader值)。
          let firedViaLeaderWindow = false;
          const whenOk = (tt) => {
            if (tt.when && tt.when.on) return false;
            if (roundOk(tt, r)) return true;
            if (isLeader && tt.whenLeader && roundOk({ when: tt.whenLeader }, r)) { firedViaLeaderWindow = true; return true; }
            if (!tt.when && !tt.whenLeader) return true;
            return false;
          };
          const hasProxy = (t0.effects || []).some(e => e.k === "proxyNormal" || e.k === "proxyHit");
          if (t0.type === "active" && (t0.coef || t0.effects.length || (t0.choices && t0.choices.length) || (t0.extraHits && t0.extraHits.length)) && !(t0.prep && r === 1) && whenOk(t0) && !onCd) fire = rnd() < baseRate + u.addbonusFor("rateup", t0);  // rateup: 提高自身主動戰法發動機率(如白眉); addbonusFor 依 t.prep/t.native 篩選 prepOnly/nativeOnly 修飾的加成(批7: 太平道法)
          else if ((t0.type === "command" || t0.type === "passive") && (t0.coef || (t0.choices && t0.choices.length) || (t0.extraHits && t0.extraHits.length) || hasProxy) && !(t0.when && t0.when.on) && whenOk(t0) && !onCd) fire = rnd() < baseRate;  // 指揮/被動: 每回合以資料 rate 擲骰; 批52续: whenLeader + extraHits 亦可觸發; 批52i: proxyNormal
          // 批52g: ammo —— 主將每回合補箭, 耗盡不發射(高櫓連營)
          if (fire && t0.ammo != null && t0.nameZh) {
            if (u.ammo[t0.nameZh] == null) u.ammo[t0.nameZh] = t0.ammo | 0;
            if (isLeader && t0.ammoReloadLeader) u.ammo[t0.nameZh] += t0.ammoReloadLeader | 0;
            if (u.ammo[t0.nameZh] <= 0) fire = false;
          }
          if (fire) {
            if (t0.cd && t0.nameZh) u.tacCd[t0.nameZh] = (t0.cd | 0) + 1;
            // 批26 B2: stack.stackPer=="cast" —— 本戰法本次成功發動(fire), 若 u 身上已有
            // stackPer=="cast" 的疊層狀態則遞增1層(見 applyStackCast() 定義)。與round模式
            // (上方主迴圈逐回合遞增)互斥判斷, 不會重複遞增。
            u.applyStackCast();
            // 批31 A: on:"activeFired" —— 只有 type==="active"(真正的主動戰法)才算「成功發動
            // 主動戰法」事件, command/passive 常駐擲骰(fire 判定式共用同一 if 區塊, 但語意是
            // 「每回合固定擲骰」而非「發動主動戰法」)不觸發此事件。置於 applyStackCast() 之後、
            // 實際套用觸發戰法本身效果之前, 讓士爭先赴一類「成功發動...前」的反應式效果搶在
            // 本次觸發戰法的傷害/效果結算前廣播(見 activeFired() 定義處對 before/after 語意
            // 取捨的說明)。
            if (t0.type === "active") activeFired(u);
            // 批16: choices(擇一分支) —— 發動時按權重隨機選一組效果(coef/kind/effects/extraHits/n/nMax
            // 可各自覆寫基礎戰法), 套用到本次發動; 未中選的分支本次不生效。權重預設均分。t0 為原始
            // 戰法物件(供 addbonusFor/whenFired 等以物件本身為鍵的邏輯保持穩定, 不因選分支而變動),
            // t 為「本次觸發實際使用」的合成視圖(不修改 t0 本身)。
            const t = t0.choices ? Object.assign({}, t0, pickChoice(t0.choices)) : t0;
            if (TRACE) lg(`【${u.side}】${u.nm} 發動戰法【${t.nameZh}】` + (t.when ? `（第${r}回合條件）` : ""));
            let _mainHitTgt = null;   // 批13: 記錄主 coef 段命中的(單體)目標, 供 extraHits 同目標段(如屠几上肉 兵刃+謀略打同一人)沿用
            let _mainHitTgts = null;  // 批45 A: 記錄主 coef 段命中的(群體)目標陣列, 供效果段 e.sameTargets 沿用同一批目標(對稱 _mainHitTgt 的單體版本)——群體目標沿用原語, 見 applyEffects 的 opt.mainHitTgts/e.sameTargets
            // 批H: active型戰法「提高自身X%會心機率...隨後造成攻擊」(百步穿楊/左右開弓)——在主coef
            // 攻擊之前先套用施放者自身的critUp/critDmgUp會心buff, 使該次AoE本身得以吃到真會心擲骰
            // (取代舊有把會心EV折入coef本身的近似)。post-coef的常規applyEffects會以同一src刷新覆蓋
            // 本效果(不疊加, 見applyEffects opt.onlyKinds註解), 故不會會心率翻倍。只對active型套用
            // (command/passive的crit走prep階段applyPassives, 不經此路徑; 其coef段多為0無主攻擊)。
            if (t0.type === "active") applyEffects(u, null, t, alliesOf(u), foesOf(u), { onlyKinds: ["critUp", "critDmgUp"] });
            // 禁近似令-批K: coefEff(leader_dual_base_coef族) —— 頂層coef「主將/非主將兩個
            // 完全不同基礎係數分支」, 取代「基礎值+單一topup」既有慣例力有未逮之處(神機妙算
            // 「coef=1.28僅主將分支,非主將應為基礎值100%」——t0.coefLeader優先權高於t.coef,
            // 只在isLeader時切換; t0.coefWhenLeader優先權最高但只在fire恰好透過whenLeader
            // 額外視窗通過時才切換, 兩者可獨立存在於同一戰法(視需要組合), 皆省略時完全維持
            // t.coef既有行為, 向後相容全庫既有資料)。
            const coefEff = (firedViaLeaderWindow && t0.coefWhenLeader != null) ? t0.coefWhenLeader
              : (isLeader && t0.coefLeader != null) ? t0.coefLeader
              : t.coef;
            if (t.coef) {
              let cnt = t.nMax ? (t.n + Math.floor(rnd() * (t.nMax - t.n + 1))) : t.n;
              // 批52g: ammo 限制本回合發射次數
              if (t0.ammo != null && t0.nameZh) {
                const left = u.ammo[t0.nameZh] || 0;
                cnt = Math.min(cnt, Math.max(0, left));
                u.ammo[t0.nameZh] = left - cnt;
              }
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
              // 批52g: effectsPerHit —— 每次 hitsRepeat 後立即套 effects(五雷震懾)
              const isActiveDmg = t0.type === "active" ? true : undefined;  // 批31 A: 供e.activeOnly amp判定「本段傷害是否為主動戰法所致」; command/passive走同一段程式碼但非主動戰法, 傳undefined(安全側不套用activeOnly加成, 見addbonus()註解)
              const effPerHit = !!t.effectsPerHit;
              if (t.targetSel) { const v = pickByCriterion(foesOf(u), t.targetSel); if (v) { hit(u, v, coefEff, t.kind, false, onHit, dealtDamage, isActiveDmg); _mainHitTgt = v; } }
              else if (t.lockTarget && cnt <= 1 && !t.hitsRepeat) { const v = resolveLockedTarget(u, t0, foesOf(u)); if (v) { hit(u, v, coefEff, t.kind, false, onHit, dealtDamage, isActiveDmg); _mainHitTgt = v; } }  // lockTarget 鍵用 t0(原始戰法物件), 避免 choices 每次合成新物件破壞跨回合鎖定
              else if (t.hitsRepeat) {
                for (let i = 0; i < cnt; i++) {
                  const v = pickTarget(foesOf(u), u);
                  if (v) {
                    hit(u, v, coefEff, t.kind, false, onHit, dealtDamage, isActiveDmg);
                    _mainHitTgt = v;
                    if (effPerHit && t.type === "active") applyEffects(u, v, t, alliesOf(u), foesOf(u));
                  }
                }
              }
              else { const vs = pickTargets(foesOf(u), cnt); for (const v of vs) hit(u, v, coefEff, t.kind, false, onHit, dealtDamage, isActiveDmg); if (vs.length === 1) _mainHitTgt = vs[0]; else _mainHitTgts = vs; }  // 批45 A: 群體(vs.length>1)額外記錄完整目標陣列
            }
            if (t.extraHits) fireExtraHits(u, t, _mainHitTgt, alliesOf, foesOf, onHit, dealtDamage);  // 批13: 多段傷害(兵刃+謀略雙段/主傷+補刀等)
            // 批12 ModeF: 混亂下單體主動戰法目標改敵我不分(pickTargetChaos); 群體/AoE(who=enemy 全體/
            // n>1)維持 applyEffects 內部既有邏輯不變 —— 這裡傳入的 tgt 只影響「單體優先鎖定」分支,
            // 群體戰法本就走 pickTargets(enemies,...) 不受此參數影響(近似, 群體戰法混亂下仍只打敵方)。
            // 批12 ModeG: lockTarget 的 applyEffects 目標(單體效果destination)同樣改用鎖定目標
            // (與混亂互斥: lockTarget 戰法目前資料上未與 chaos 共存, 若未來衝突以 lockTarget 優先,
            // 因 lockTarget 語意更明確針對特定戰法設計)。
            // 批52g: effectsPerHit 已逐 hit 套過, 跳過二次
            if (t.type === "active" && !t.effectsPerHit) applyEffects(u, t.lockTarget ? resolveLockedTarget(u, t0, foesOf(u)) : pickTargetChaos(u, alliesOf(u), foesOf(u)), t, alliesOf(u), foesOf(u), { mainHitTgts: _mainHitTgts });  // 批45 A: 傳入本次主coef段的群體目標陣列, 供 e.sameTargets 沿用
            else if (hasProxy) {
              // 批52i: 垂心萬物等 proxyNormal command —— fire 後套 effects(noHeal, heal 走 everyRound)
              applyEffects(u, pickTargetChaos(u, alliesOf(u), foesOf(u)), t, alliesOf(u), foesOf(u), { noHeal: true });
            }
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
              applyEffects(u, _mainHitTgt, t, alliesOf(u), foesOf(u), { mainHitTgts: _mainHitTgts });
            }
          }
        }
        // 批52i: 普攻管線與 proxyNormal 共用 doNormalAttack(含連擊/everyN/突擊)
        if (!u.disarm) {
          if (TRACE) lg(`【${u.side}】${u.nm} 普通攻擊`);
          doNormalAttack(u, alliesOf(u), foesOf(u), onHit, dealtDamage, activeFired);
        } else if (TRACE) lg(`【${u.side}】${u.nm} 陷入繳械，無法普通攻擊`);
      }
      for (const u of [...A, ...B]) {
        const s = u.settle; if (!s) continue;
        if (s.layers >= s.max || s.left <= 1) {
          // 禁近似令-批K: e.perStackFrom(dynamic_coef_from_counter族) —— 結算coef改讀u身上
          // 該stackId的當下疊層數(而非settle自己的內部layers計數, 兩者是不同的計數器,
          // 見registration端perStackFrom註解), 「最終降傷施加次數」取結算當下(第6回合)的層數。
          const stackLayers = s.perStackFrom ? ((u.ampLayersById && u.ampLayersById[s.perStackFrom]) || 0) : s.layers;
          const targets = s.singleTarget ? [u] : (setA.has(u) ? A : B);
          for (const v of targets) if (v.alive) { const sd = damage(s.caster, v, s.base + s.per * stackLayers, s.kind, s.snap); v.troop -= sd; v.wounded += sd * woundedRate(CUR_R); fireSelfReactive(v, "dmgThreshold", bumpDmgAccum(v, sd)); }
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

  function trace(POOL, teamA, teamB, troopA = null, troopB = null, bsA = null, bsB = null, eqA = null, eqB = null, addA = null, addB = null, inhA = null, inhB = null, scenario = null, campLvA = 0, campLvB = 0) {
    TRACE = []; CUR_R = 0;                           // 跑一場並記錄事件日誌
    const r = fight(POOL, teamA, troopA, teamB, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario, campLvA, campLvB);
    const log = TRACE; TRACE = null;
    return { ...r, log };
  }
  function simulate(POOL, teamA, teamB, n = 2000, troopA = null, troopB = null, bsA = null, bsB = null, eqA = null, eqB = null, addA = null, addB = null, inhA = null, inhB = null, scenario = null, campLvA = 0, campLvB = 0) {
    let a = 0, rs = 0, killA = 0, killB = 0;          // 批8: 殲滅(kill) vs 判定勝(8回合打滿按剩餘兵力) 分開統計
    for (let i = 0; i < n; i++) {
      const r = fight(POOL, teamA, troopA, teamB, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario, campLvA, campLvB);
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
    hpOk, targetHas, dispelUnit, pickChoice, pickByCriterion };  // 批16 新原語供測試腳本直接驗證內部機制(同 sgz.py 直接測 Unit/hit); 批45 C: pickByCriterion供測試腳本直接驗證targetSel(如maxTroop)選標方向
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  root.SGZ = API;
})(typeof globalThis !== "undefined" ? globalThis : this);
