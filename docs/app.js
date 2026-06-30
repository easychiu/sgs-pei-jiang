"use strict";
let RAW = {}, POOL = {}, TECH_OPTIONS = {};   // TECH_OPTIONS: 各欄可選特技名單(權威, 來自參考app)
let TACTIC_CATS = {}, TAC_TYPE = {};          // TACTIC_CATS: {陣法:[],兵種:[]} ; TAC_TYPE: 戰法名→type
let NATIVE_TACTICS = new Set();               // 所有武將的自帶戰法(本傳簽名),戰法選單一律排除
let TAC_TIER = {};                            // 戰法名→品質階 S/A/B
const FACBG = { "魏": "var(--魏)", "蜀": "var(--蜀)", "吳": "var(--吳)", "群": "var(--群)" };
const STATN = { force: "武力", intel: "智力", command: "統率", speed: "速度", all: "全屬性" };
const TYPEN = { active: "主動", charge: "突擊", command: "指揮", passive: "被動" };
const TROOPS = ["騎", "盾", "弓", "槍", "器"];
const cardSrc = n => "cards/" + encodeURIComponent(n) + ".webp";
const teams = { A: [null, null, null], B: [null, null, null] };
const troops = { A: "", B: "" };                  // "" = 自動(依隊伍適性)
const bsel = { A: [null, null, null], B: [null, null, null] };  // 各將兵書(null=預設主兵書)
const eqsel = { A: [{}, {}, {}], B: [{}, {}, {}] };            // 各將裝備 {type:[特技,...]} 每欄最多2(雙特技)→全身最多8
const EQUIP_SLOTS = [{ t: "武器", l: "武器" }, { t: "防具", l: "護甲" }, { t: "坐騎", l: "馬匹" }, { t: "寶物", l: "寶物" }];
const eqSlot = (cfg, t) => { const v = cfg && cfg[t]; return Array.isArray(v) ? v.filter(Boolean) : (v ? [v] : []); };  // 容錯: 舊字串/新陣列
const eqNames = cfg => EQUIP_SLOTS.flatMap(s => eqSlot(cfg, s.t));   // 攤平成名單(最多8)→引擎合併effects
function eqByType(g, type) {
  const opts = (TECH_OPTIONS[type] || []).slice();           // 權威通用特技名單
  for (const e of Object.values(SGZ.equips())) {              // 加上該將專屬特技(專屬不在通用名單)
    if (e.type === type && e.exclusive && (e.exclusive === g.name || e.exclusive.includes(g.name)) && !opts.includes(e.name)) opts.push(e.name);
  }
  if (!opts.length) return Object.values(SGZ.equips()).filter(e => e.type === type && (!e.exclusive || e.exclusive === g.name)).map(e => e.name);  // 後備: 名單未載入
  return opts;
}
function eqSummary(cfg) {
  const on = EQUIP_SLOTS.map(s => [s, eqSlot(cfg, s.t)]).filter(([, v]) => v.length);
  return on.length ? on.map(([s, v]) => `${s.l[0]}·${v.join("+")}`).join(" ") : "無";
}
const builds = { A: [null, null, null], B: [null, null, null] }; // 養成(null=預設: 進階滿+主屬性)
const inhsel = { A: [[], [], []], B: [[], [], []] };           // 各將傳承戰法(最多2)
let TACTIC_NAMES = [];                                          // 全戰法名(供傳承選)
let SEASON_MODS = {}, CURRENT_SEASON = null;                    // 賽季修正 / 當前賽季 id
const STAT4 = ["force", "intel", "command", "speed"];
const STATLAB = { force: "武", intel: "智", command: "統", speed: "速" };
const maxAdv = g => (g.stars >= 5 ? 5 : 4);
const poolSize = (adv, col) => 50 + adv * 10 + (col ? 10 : 0);   // 加點池: 50 + 進階×10 + 典藏×10
const primaryStat = g => STAT4.reduce((a, b) => g.base[b] > g.base[a] ? b : a);
function defaultBuild(g) {                          // 預設: 進階滿、不典藏、點全加最高屬性
    const adv = maxAdv(g);
    return { advance: adv, collection: false, alloc: { [primaryStat(g)]: poolSize(adv, false) } };
}
const getBuild = (side, i) => builds[side][i] || defaultBuild(POOL[teams[side][i]]);
const combatPct = bd => (bd.advance + (bd.collection ? 1 : 0)) * 0.02;   // 進階/典藏: 每階+2%攻防
const buildAdd = (bd, bsOn) => ({ ...bd.alloc, amp: bsOn ? combatPct(bd) : 0, mitig: bsOn ? combatPct(bd) : 0 });
// 兵書 6 類別(顏色/圖示) — PK 賽季: 1 主兵書 + 2 副兵書
const BINGSHU_CAT = {
  "作戰": { c: "#c0392b", i: "⚔" }, "虛實": { c: "#8e44ad", i: "🚩" }, "軍形": { c: "#2e86de", i: "🛡" },
  "九變": { c: "#16a085", i: "☯" }, "始計": { c: "#b7950b", i: "⛑" }, "用間": { c: "#7f8c8d", i: "🥷" },
};
const SUBS_MAX = 2;
const bsKey = (cat, nm) => cat + "·" + nm;
function catList(g) { return g.bingshuOptions ? Object.keys(g.bingshuOptions) : (g.bingshuCats || []); }
function mainsFor(g, cat) {                          // 該將該類別的主兵書(優先用 bingshuOptions)
  if (!cat) return [];
  if (g.bingshuOptions && g.bingshuOptions[cat]) return (g.bingshuOptions[cat].primary || []).map(nm => bsKey(cat, nm));
  return SGZ.mainByCat()[cat] || [];
}
function subsFor(g, cat) {
  if (!cat) return [];
  if (g.bingshuOptions && g.bingshuOptions[cat]) return (g.bingshuOptions[cat].secondary || []).map(nm => bsKey(cat, nm));
  return SGZ.subByCat()[cat] || [];
}
function defaultBingshuCfg(g) {
  const cat = catList(g)[0] || null;
  return { on: true, category: cat, main: mainsFor(g, cat)[0] || null, subs: subsFor(g, cat).slice(0, SUBS_MAX) };
}
const getBsel = (side, i) => bsel[side][i] || defaultBingshuCfg(POOL[teams[side][i]]);
const bsNames = cfg => cfg.on ? [...new Set([cfg.main, ...(cfg.subs || [])].filter(Boolean))] : [];
const bsLabel = k => (k && k.includes("·")) ? k.split("·")[1] : (k || "—");
function bselSummary(cfg) {
  if (!cfg.on) return "兵書：<b>關</b>";
  const m = BINGSHU_CAT[cfg.category] || { i: "📖" };
  return `<span style="color:${m.c || "var(--gold2)"}">${m.i} ${cfg.category || "—"}</span>・${bsLabel(cfg.main)}＋${(cfg.subs || []).filter(Boolean).length}副`;
}
function setAll(side, red) {                        // 一鍵滿紅(進階滿+典藏+主屬性+開兵書) / 白板
  teams[side].forEach((n, i) => {
    if (!n) return;
    const g = POOL[n];
    if (red) {
      const adv = maxAdv(g);
      builds[side][i] = { advance: adv, collection: true, alloc: { [primaryStat(g)]: poolSize(adv, true) } };
      bsel[side][i] = defaultBingshuCfg(g);
    } else {
      builds[side][i] = { advance: 0, collection: false, alloc: {} };
      bsel[side][i] = { on: false, category: null, main: null, subs: [] };
    }
  });
  renderSlots(side);
  $("#simResult").classList.add("hidden");
}
function teamParams(side) {                         // 收集一隊的模擬參數
  const names = [], bs = [], eq = [], ad = [], inh = [];
  teams[side].forEach((n, i) => {
    if (n) { names.push(n); bs.push(bsNames(getBsel(side, i))); eq.push(eqNames(eqsel[side][i])); ad.push(buildAdd(getBuild(side, i), getBsel(side, i).on)); inh.push((inhsel[side][i] || []).filter(Boolean)); }
  });
  return { names, bs, eq, ad, inh };
}
function optimizeTeam() {                           // 為我方試 5 兵種, 模擬找最佳
  const pa = teamParams("A"), pb = teamParams("B");
  if (!pa.names.length) { alert("先放我方武將"); return; }
  const hasB = pb.names.length > 0;
  const foe = hasB ? pb.names : ["呂布", "趙雲", "關羽"];   // 無敵方則對基準隊
  const tb = hasB ? effTroop("B") : null;
  let best = null;
  for (const tr of SGZ.TROOPS) {
    const r = SGZ.simulate(POOL, pa.names, foe, 1000, tr, tb, pa.bs, hasB ? pb.bs : null,
      pa.eq, hasB ? pb.eq : null, pa.ad, hasB ? pb.ad : null, pa.inh, hasB ? pb.inh : null, CURRENT_SEASON);
    if (!best || r.winA > best.win) best = { troop: tr, win: r.winA };
  }
  troops.A = best.troop;
  document.querySelector('.troop[data-side="A"]').value = best.troop;
  renderSlots("A");
  const res = $("#simResult"); res.classList.remove("hidden");
  res.innerHTML = `<div>🔧 我方最佳兵種：<b class="gold">${best.troop}</b>　勝率 <b class="gold">${(best.win * 100).toFixed(0)}%</b>　${hasB ? "vs 敵方" : "vs 基準隊"}</div>`;
}
const $ = s => document.querySelector(s);
const pct = v => (v * 100).toFixed(0);
const APT_PCT = { S: 1.2, A: 1.0, B: 0.85, C: 0.7, D: 0.55 };
const aptOf = (g, t) => g.apt[t] || "-";
const aptMul = (g, t) => APT_PCT[g.apt[t]] ?? 0.85;

function effTroop(side) {                          // 該隊實際採用兵種
  if (troops[side]) return troops[side];
  const m = teams[side].filter(Boolean);
  return m.length ? SGZ.teamTroop(POOL, m) : "騎";
}
function statStr(g, t, add) {                       // 套養成加點 + 兵種適性後的面板
  const m = aptMul(g, t), a = add || {};
  return STAT4.map(s => `${STATLAB[s]}${(g.base[s] + (a[s] || 0)) * m | 0}`).join(" ");
}
function aptBadges(g) {
  return TROOPS.map(t => `<span class="apt ${g.apt[t] || ""}">${t}${aptOf(g, t)}</span>`).join("");
}

function effText(e) {
  switch (e.k) {
    case "amp": return e.who === "enemy" && e.val < 0
      ? `削弱敵方傷害 ${pct(-e.val)}%` : `${e.who === "self" ? "自身" : "我方"}增傷 +${pct(e.val)}%`;
    case "mitig": return `${e.who === "self" ? "自身" : "我方"}減傷 ${pct(e.val)}%`;
    case "stun": return `控制敵方 ${e.dur} 回合`;
    case "heal": return `治療我方（治療率 ${pct(e.coef)}%）`;
    case "stat": { const d = ((e.mult || 1) - 1) * 100; const w = e.who === "enemy" ? "敵方" : (e.who === "self" ? "自身" : "我方");
      return `${w} ${STATN[e.stat] || e.stat} ${d >= 0 ? "+" : ""}${d.toFixed(0)}%`; }
    case "dot": return `持續傷害敵方（每回合 ${pct(e.coef)}%）`;
    case "settle": return `猛毒結算·疊滿爆發（基礎 ${pct(e.base)}%＋每層 ${pct(e.per)}%）`;
    case "extra": return `連擊／追擊（${e.val} 次）`;
    case "redirect": return `傷害轉移·代承 ${pct(e.share)}%`;
    case "stack": return `疊加增益（每回合 +${pct(e.per)}%，上限 ${e.max} 層）`;
    case "decay": return `衰減增益（開場 +${pct(e.v0)}%）`;
    case "swap": return `武智互換`;
    case "pierce": return `看破·無視減傷 ${pct(e.val)}%`;
    case "counter": return `反擊（${pct(e.coef)}%）`;
    default: return e.k;
  }
}
function tacticHTML(g) {
  const t = g.tactic;
  if (!t) return `<div class="eff">${g.tacticName}（資料未建模）</div>`;
  const head = `${g.tacticName}　<small>[${TYPEN[t.type] || t.type}${t.coef ? ` · 傷害率 ${pct(t.coef)}%` : ""}${t.rate < 1 ? ` · 發動 ${pct(t.rate)}%` : ""}]</small>`;
  const fx = (t.effects || []).map(e => `<div class="eff">▸ ${effText(e)}</div>`).join("");
  return `<div class="eff" style="border-color:var(--gold2)"><b>${head}</b></div>${fx}`;
}
const facBadge = f => `<span class="fac" style="background:${FACBG[f] || "#777"}">${f}</span>`;

async function load() {
  const j = u => fetch(u).then(r => r.json()).catch(() => []);
  const jo = u => fetch(u).then(r => r.json()).catch(() => ({}));
  const [g, t, bs, bo, eq, sm, to, tc, ti] = await Promise.all([
    fetch("data/generals.json").then(r => r.json()),
    fetch("data/tactics_parsed.json").then(r => r.json()),
    j("data/bingshu_parsed.json"), j("data/bonds_parsed.json"), j("data/equips_parsed.json"),
    jo("data/season_modifiers.json"), jo("data/tech_options.json"), jo("data/tactic_categories.json"),
    jo("data/tactic_tiers.json")]);
  g.forEach(x => RAW[x.name] = x);
  NATIVE_TACTICS = new Set(g.map(x => x.tactic).filter(Boolean));   // 自帶簽名,選單排除
  TECH_OPTIONS = to;
  TACTIC_CATS = tc;
  TAC_TIER = {};
  for (const q of ["S", "A", "B"]) for (const nm of (ti[q] || [])) TAC_TIER[nm] = q;   // 建 名→階 索引
  t.forEach(x => TAC_TYPE[x.nameZh] = x.type);                // 戰法名→type(供分類)
  SEASON_MODS = sm;
  POOL = SGZ.buildPool(g, t, bs, bo, eq, sm).POOL;
  TACTIC_NAMES = t.filter(x => x.type !== "none").map(x => x.nameZh).sort((a, b) => a.localeCompare(b));
  const sc = await j("data/scenarios.json");                  // 賽季(資訊性)
  if (sc.length) {
    const ss = $("#season");
    ss.innerHTML = sc.map((s, i) => `<option value="${i}">${s.name}</option>`).join("");
    ss.value = String(sc.length - 1);
    const showSeason = () => {
      const s = sc[+ss.value];
      CURRENT_SEASON = s ? s.id : null;
      const mods = (SEASON_MODS[CURRENT_SEASON] || []).map(m => m.label).join("・");
      $("#seasonInfo").textContent = s ? "　" + (s.coreMechanics || []).join("・") + (mods ? "　✦特色生效: " + mods : "") : "";
      renderSlots("A"); renderSlots("B");
    };
    ss.onchange = showSeason; showSeason();
  }
  $("#stat").textContent = `${Object.keys(POOL).length} 武將 · ${t.filter(x => x.type !== "none").length} 戰法`;
  initTabs();
  ["A", "B"].forEach(s => {
    const sel = document.querySelector(`.troop[data-side="${s}"]`);
    sel.innerHTML = `<option value="">自動</option>` + TROOPS.map(x => `<option>${x}</option>`).join("");
    sel.onchange = () => { troops[s] = sel.value; renderSlots(s); $("#simResult").classList.add("hidden"); };
    document.querySelector(`.redall[data-side="${s}"]`).onclick = () => setAll(s, true);
    document.querySelector(`.blankall[data-side="${s}"]`).onclick = () => setAll(s, false);
    renderSlots(s);
  });
  $("#runSim").onclick = runSim;
  $("#optimize").onclick = optimizeTeam;
  $("#clearSim").onclick = () => { for (const s of ["A", "B"]) { teams[s] = [null, null, null]; bsel[s] = [null, null, null]; eqsel[s] = [{}, {}, {}]; builds[s] = [null, null, null]; inhsel[s] = [[], [], []]; } renderSlots("A"); renderSlots("B"); $("#simResult").classList.add("hidden"); };
  $("#runRec").onclick = runRec;
  $("#dexSearch").oninput = renderDex;
  $("#modal").onclick = e => { if (e.target.id === "modal") closeModal(); };
  renderDex();
}
function initTabs() {
  document.querySelectorAll(".tab").forEach(b => b.onclick = () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
    b.classList.add("active"); $("#" + b.dataset.tab).classList.add("active");
  });
}

function statBars(g, tr, alloc) {                  // 武智統速 + 加點(含適性)
  const m = aptMul(g, tr);
  return STAT4.map(s => {
    const v = (g.base[s] + (alloc[s] || 0)) * m;
    const add = alloc[s] ? `<i>+${alloc[s]}</i>` : "";
    return `<span class="st">${STATLAB[s]}<b>${v | 0}</b>${add}</span>`;
  }).join("");
}
function renderSlots(side) {
  const tr = effTroop(side);
  const sel = document.querySelector(`.troop[data-side="${side}"]`);
  if (sel && !troops[side] && sel.options[0]) sel.options[0].text = `自動（${tr}）`;  // 選項可能尚未填入(初次render早於兵種下拉建立)
  const box = document.querySelector(`.team[data-side="${side}"] .slots`);
  box.innerHTML = "";
  for (let i = 0; i < 3; i++) {
    const n = teams[side][i], g = n && POOL[n];
    const d = document.createElement("div");
    if (g) {
      const bd = getBuild(side, i), bc = getBsel(side, i);
      const act = g.tactic && g.tactic.type === "active" ? '<span class="gact">動</span>' : "";
      const inh = inhsel[side][i] || [];
      d.className = "gcard-sim";
      d.innerHTML = `
        <div class="grole ${i === 0 ? "main" : ""}">${i === 0 ? "主將" : "副將"}・總兵力 10000</div>
        <div class="gport" style="background-image:url('${cardSrc(n)}')"></div>
        <div class="gtag">${facBadge(g.faction)}<span class="gnm">${n}</span>${act}</div>
        <div class="glv">Lv50・採用 <b>${tr}兵</b></div>
        <div class="gapt">${aptBadges(g)}</div>
        <div class="gstats">${statBars(g, tr, bd.alloc)}</div>
        <div class="grow gtac">⚔ ${g.tacticName}　${[0, 1].map(k => `<button class="tac${inh[k] ? " on" : ""}" data-k="${k}" title="點選戰法">${inh[k] || "＋戰法"}</button>`).join("")}</div>
        <div class="grow">${bselSummary(bc)} <button class="book" title="兵書">📖</button></div>
        <div class="grow">${buildSummary(bd)} <button class="cog" title="養成">⚙</button></div>
        <div class="grow">🛡 ${eqSummary(eqsel[side][i])} <button class="equip" title="裝備(武器/護甲/馬匹/寶物)">＋</button></div>`;
      d.querySelector(".gport").onclick = () => openPicker(side, i);
      d.querySelector(".gtag").onclick = () => openPicker(side, i);
      d.querySelector(".cog").onclick = () => openBuild(side, i);
      d.querySelector(".book").onclick = () => openBingshu(side, i);
      d.querySelectorAll(".tac").forEach(b => b.onclick = () => openTactic(side, i, +b.dataset.k));
      d.querySelector(".equip").onclick = () => openEquip(side, i);
    } else {
      d.className = "gcard-sim empty";
      d.innerHTML = `<div class="gempty">＋<br>點選武將</div>`;
      d.onclick = () => openPicker(side, i);
    }
    box.appendChild(d);
  }
  const filled = teams[side].filter(Boolean);
  const bonds = filled.length ? SGZ.activeBonds(filled) : [];
  const bn = document.createElement("div");
  bn.className = "bondbar";
  bn.textContent = bonds.length ? `🔗 緣分：${bonds.map(b => b.name).join("、")}` : "";
  box.appendChild(bn);
}
function runSim() {
  const A = [], B = [], bsA = [], bsB = [], eqA = [], eqB = [], adA = [], adB = [], inA = [], inB = [];
  teams.A.forEach((n, i) => { if (n) { A.push(n); bsA.push(bsNames(getBsel("A", i))); eqA.push(eqNames(eqsel.A[i])); adA.push(buildAdd(getBuild("A", i), getBsel("A", i).on)); inA.push((inhsel.A[i] || []).filter(Boolean)); } });
  teams.B.forEach((n, i) => { if (n) { B.push(n); bsB.push(bsNames(getBsel("B", i))); eqB.push(eqNames(eqsel.B[i])); adB.push(buildAdd(getBuild("B", i), getBsel("B", i).on)); inB.push((inhsel.B[i] || []).filter(Boolean)); } });
  if (!A.length || !B.length) { alert("兩邊各至少放 1 名武將"); return; }
  const ta = effTroop("A"), tb = effTroop("B");
  const r = SGZ.simulate(POOL, A, B, 3000, ta, tb, bsA, bsB, eqA, eqB, adA, adB, inA, inB, CURRENT_SEASON);
  const res = $("#simResult"); res.classList.remove("hidden");
  res.innerHTML = `
    <div class="bar"><div class="a" style="width:${r.winA * 100}%">${pct(r.winA)}%</div>
    <div class="b" style="width:${r.winB * 100}%">${pct(r.winB)}%</div></div>
    <div>我方[${ta}兵] <b class="gold">${pct(r.winA)}%</b> 勝　·　敵方[${tb}兵] <b class="gold">${pct(r.winB)}%</b> 勝　·　平均 ${r.rounds} 回合</div>
    <div style="font-size:13px;color:#9a8b6a;margin-top:6px">${A.join("／")}　vs　${B.join("／")}</div>`;
}

function runRec() {
  const f = $("#recFaction").value;
  const pool = f ? Object.keys(POOL).filter(n => POOL[n].faction === f) : null;
  const list = SGZ.recommend(POOL, { pool, top: 10 });
  $("#recList").innerHTML = list.map(([team, sc, tr]) =>
    `<li data-team='${JSON.stringify(team)}' data-troop="${tr}"><span><b class="gold">[${tr}兵]</b> ${team.join("　／　")}</span><span class="sc">${sc}</span></li>`).join("");
  document.querySelectorAll("#recList li").forEach(li => li.onclick = () => {
    teams.A = [...JSON.parse(li.dataset.team)]; troops.A = li.dataset.troop; bsel.A = [null, null, null]; eqsel.A = [{}, {}, {}]; builds.A = [null, null, null]; inhsel.A = [[], [], []];
    document.querySelector(`.troop[data-side="A"]`).value = li.dataset.troop;
    renderSlots("A");
    document.querySelector('.tab[data-tab="sim"]').click();
  });
}

function renderDex() {
  const q = ($("#dexSearch").value || "").trim();
  const names = Object.keys(POOL).filter(n => !q || n.includes(q))
    .sort((a, b) => (RAW[b].stars || 0) - (RAW[a].stars || 0) || a.localeCompare(b));
  $("#dexGrid").innerHTML = names.map(n => {
    const g = POOL[n], bt = SGZ.bestTroop(g.apt);
    return `<div class="gcard" data-n="${n}">
      <img src="${cardSrc(n)}" loading="lazy" alt="${n}"
           onerror="this.style.background='var(--'+'${g.faction}'+')';this.removeAttribute('src')">
      <div class="info"><div class="nm">${n}</div>
      <div class="sub">${g.faction} · 主${bt}${aptOf(g, bt)}</div></div></div>`;
  }).join("");
  document.querySelectorAll(".gcard").forEach(c => c.onclick = () => showDetail(c.dataset.n));
}

function openPicker(side, idx) {
  const names = Object.keys(POOL).sort((a, b) => (RAW[b].stars || 0) - (RAW[a].stars || 0));
  const box = $("#modal .modal-box");
  box.innerHTML = `<h2 class="gold">選擇武將（${side === "A" ? "我方" : "敵方"}）</h2>
    <input id="pickSearch" placeholder="搜尋…" style="width:100%;padding:9px;margin:8px 0;background:#2a2018;color:var(--ink);border:1px solid var(--line);border-radius:6px">
    <div class="pick-grid"></div>`;
  const draw = q => box.querySelector(".pick-grid").innerHTML =
    names.filter(n => !q || n.includes(q)).map(n =>
      `<div class="pick" data-n="${n}">${facBadge(POOL[n].faction)} ${n}</div>`).join("");
  const bind = () => box.querySelectorAll(".pick").forEach(p => p.onclick = () => {
    teams[side][idx] = p.dataset.n; bsel[side][idx] = null; eqsel[side][idx] = {}; builds[side][idx] = null; inhsel[side][idx] = []; renderSlots(side); closeModal();
  });
  draw(""); bind();
  box.querySelector("#pickSearch").oninput = e => { draw(e.target.value.trim()); bind(); };
  $("#modal").classList.remove("hidden");
}
function showDetail(n) {
  const g = POOL[n], raw = RAW[n], bt = SGZ.bestTroop(g.apt);
  const box = $("#modal .modal-box");
  box.innerHTML = `<div class="detail">
    <img src="${cardSrc(n)}" onerror="this.style.display='none'" alt="${n}">
    <div class="meta"><h2 class="gold" style="margin:0">${facBadge(g.faction)} ${n}
      <small style="color:#b8a987">${"★".repeat(raw.stars || 5)} ${g.gender === "Female" ? "♀" : "♂"}</small></h2>
      <div style="margin:10px 0">兵種適性：${aptBadges(g)}</div>
      <div class="sub">可用兵書：${(g.bingshuCats || []).join("／") || "—"}</div>
      <div class="stat-row"><span><b>武</b> ${g.base.force | 0}</span><span><b>智</b> ${g.base.intel | 0}</span>
      <span><b>統</b> ${g.base.command | 0}</span><span><b>速</b> ${g.base.speed | 0}</span></div>
      <div class="sub">↑ 基礎面板；戰鬥時 ×隊伍兵種適性%（最佳：${bt}${aptOf(g, bt)} → ${statStr(g, bt)}）</div>
      <h3 class="gold" style="margin:14px 0 4px">自帶戰法</h3>
      ${tacticHTML(g)}
    </div></div>
    <div style="text-align:right;margin-top:12px"><button id="toSim" class="primary">加入我方</button></div>`;
  box.querySelector("#toSim").onclick = () => {
    const i = teams.A.indexOf(null); if (i < 0) { alert("我方已滿"); return; }
    teams.A[i] = n; bsel.A[i] = null; eqsel.A[i] = {}; builds.A[i] = null; inhsel.A[i] = []; renderSlots("A"); closeModal();
    document.querySelector('.tab[data-tab="sim"]').click();
  };
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }

function buildSummary(bd) {
  const al = STAT4.filter(k => bd.alloc[k] > 0).map(k => `${STATLAB[k]}+${bd.alloc[k]}`).join(" ");
  return `進階${bd.advance}${bd.collection ? " 典藏" : ""}・攻防+${Math.round(combatPct(bd) * 100)}%・加點 ${al || "未配"}`;
}
function openBuild(side, i) {
  const n = teams[side][i], g = POOL[n], max = maxAdv(g);
  const bd = JSON.parse(JSON.stringify(getBuild(side, i)));   // 工作副本
  const box = $("#modal .modal-box");
  const render = () => {
    const pool = poolSize(bd.advance, bd.collection);
    const left = pool - STAT4.reduce((s, k) => s + (bd.alloc[k] || 0), 0);
    box.innerHTML = `<h2 class="gold">${n}・養成加點</h2>
      <div class="brow">進階 <select id="bAdv"></select>
        　<label><input type="checkbox" id="bCol"${bd.collection ? " checked" : ""}> 典藏（+10點）</label></div>
      <div class="brow">攻防加成 <b class="gold">+${Math.round(combatPct(bd) * 100)}%</b>（進階/典藏每階 +2%攻 +2%防）</div>
      <div class="brow">加點可分配 <b class="gold">${pool}</b> 點，剩餘 <b id="bLeft" style="${left < 0 ? "color:#e36" : "color:var(--gold2)"}">${left}</b></div>
      <div class="balloc">${STAT4.map(k => `<label>${STATLAB[k]}<input type="number" min="0" data-k="${k}" value="${bd.alloc[k] || 0}"></label>`).join("")}</div>
      <div class="brow" style="color:#9a8b6a">面板（主兵種 ${SGZ.bestTroop(g.apt)}）：${statStr(g, SGZ.bestTroop(g.apt), bd.alloc)}</div>
      <div style="text-align:right;margin-top:14px">
        <button id="bAuto">主屬性全加</button>　<button id="bSave" class="primary">套用</button></div>`;
    const adv = box.querySelector("#bAdv");
    adv.innerHTML = Array.from({ length: max + 1 }, (_, x) => `<option${x === bd.advance ? " selected" : ""}>${x}</option>`).join("");
    adv.onchange = () => { bd.advance = +adv.value; render(); };
    box.querySelector("#bCol").onchange = e => { bd.collection = e.target.checked; render(); };
    box.querySelectorAll(".balloc input").forEach(inp => inp.onchange = () => { bd.alloc[inp.dataset.k] = Math.max(0, +inp.value || 0); render(); });
    box.querySelector("#bAuto").onclick = () => { bd.alloc = { [primaryStat(g)]: poolSize(bd.advance, bd.collection) }; render(); };
    box.querySelector("#bSave").onclick = () => {
      if (pool - STAT4.reduce((s, k) => s + (bd.alloc[k] || 0), 0) < 0) { alert("超出可分配點數"); return; }
      builds[side][i] = bd; renderSlots(side); closeModal();
    };
  };
  render();
  $("#modal").classList.remove("hidden");
}

function openBingshu(side, i) {
  const n = teams[side][i], g = POOL[n], bd = getBuild(side, i);
  const cfg = JSON.parse(JSON.stringify(getBsel(side, i)));
  const cats = catList(g);
  const box = $("#modal .modal-box");
  const render = () => {
    const mains = mainsFor(g, cfg.category);
    const subs = subsFor(g, cfg.category);
    const pct = cfg.on ? Math.round(combatPct(bd) * 100) : 0;
    box.innerHTML = `<h2 class="gold">${n}・兵書</h2>
      <div class="brow"><label><input type="checkbox" id="bsOn"${cfg.on ? " checked" : ""}> 開啟兵書（PK：主1＋副2）</label>
        　攻防加成 <b class="gold">+${pct}%</b><span style="color:#9a8b6a;font-size:13px">（進階/典藏，需開兵書）</span></div>
      <div id="bsBody" style="${cfg.on ? "" : "opacity:.4;pointer-events:none"}">
        <div class="brow">類別：<span class="catchips"></span></div>
        <div class="brow">大兵書 <select id="bsMain"></select></div>
        <div class="brow">小兵書 <select class="bsSub" data-x="0"></select> <select class="bsSub" data-x="1"></select></div>
      </div>
      <div style="text-align:right;margin-top:14px"><button id="bsSave" class="primary">套用</button></div>`;
    const chips = box.querySelector(".catchips");
    chips.innerHTML = cats.length ? cats.map(c => { const m = BINGSHU_CAT[c] || {}; const on = c === cfg.category;
      return `<button class="catchip" data-c="${c}" style="border-color:${m.c || "#777"};color:${on ? "#15100c" : (m.c || "#ccc")};background:${on ? (m.c || "#777") : "transparent"}">${m.i || ""} ${c}</button>`; }).join("")
      : '<span style="color:#9a8b6a">此武將可用兵書待補（Gemini）</span>';
    chips.querySelectorAll(".catchip").forEach(b => b.onclick = () => {
      cfg.category = b.dataset.c;
      cfg.main = mainsFor(g, cfg.category)[0] || null;
      cfg.subs = subsFor(g, cfg.category).slice(0, SUBS_MAX);
      render();
    });
    const mainSel = box.querySelector("#bsMain");
    mainSel.innerHTML = mains.map(x => `<option value="${x}"${x === cfg.main ? " selected" : ""}>${bsLabel(x)}</option>`).join("") || `<option value="">—</option>`;
    mainSel.onchange = () => cfg.main = mainSel.value || null;
    box.querySelectorAll(".bsSub").forEach(sel => {
      const x = +sel.dataset.x;
      sel.innerHTML = `<option value="">無</option>` + subs.map(s => `<option value="${s}"${s === (cfg.subs || [])[x] ? " selected" : ""}>${bsLabel(s)}</option>`).join("");
      sel.onchange = () => { cfg.subs = cfg.subs || []; cfg.subs[x] = sel.value || null; };
    });
    box.querySelector("#bsOn").onchange = e => { cfg.on = e.target.checked; render(); };
    box.querySelector("#bsSave").onclick = () => { bsel[side][i] = cfg; renderSlots(side); closeModal(); };
  };
  render();
  $("#modal").classList.remove("hidden");
}

const TAC_CHIPS = ["全部", "主動", "被動", "指揮", "陣法", "兵種", "突擊"];
const TYPE2CAT = { active: "主動", passive: "被動", command: "指揮", charge: "突擊" };
function tacCat(name) {                                        // 戰法分類: 陣法/兵種(名單) 否則由 type 推導
  if ((TACTIC_CATS["陣法"] || []).includes(name)) return "陣法";
  if ((TACTIC_CATS["兵種"] || []).includes(name)) return "兵種";
  return TYPE2CAT[TAC_TYPE[name]] || "其他";
}
const tacTier = name => TAC_TIER[name] || null;               // 品質階 S/A/B(無則 null)
const TIER_RANK = { S: 0, A: 1, B: 2 };
function teamHasCat(side, cat, exI, exK) {                     // 同隊(含自帶)是否已佔該分類, 排除(exI,exK)欄
  for (let gi = 0; gi < 3; gi++) {
    const nm = teams[side][gi]; if (!nm) continue;
    const g = POOL[nm];
    if (g && tacCat(g.tacticName) === cat) return true;       // 自帶戰法也計入
    const a = inhsel[side][gi] || [];
    for (let kk = 0; kk < a.length; kk++) { if (gi === exI && kk === exK) continue; if (a[kk] && tacCat(a[kk]) === cat) return true; }
  }
  return false;
}
function openTactic(side, i, k) {                              // 選武將卡第 k 個戰法欄(單選, 點即替換)
  const n = teams[side][i];
  const cur = (inhsel[side][i] || [])[k] || null;
  const lockedCats = ["陣法", "兵種"].filter(c => teamHasCat(side, c, i, k));  // 同隊已佔→不能再選
  let cat = "全部";
  const box = $("#modal .modal-box");
  box.innerHTML = `<h2 class="gold">${n}・戰法 ${k + 1}</h2>
    <div class="brow">目前：<b class="gold">${cur || "空"}</b>　<button id="tacClr">清除此欄</button></div>
    <div id="tacChips" class="catchips" style="margin:6px 0"></div>
    <input id="tacSearch" placeholder="搜尋戰法…" style="width:100%;padding:9px;margin:8px 0;background:#2a2018;color:var(--ink);border:1px solid var(--line);border-radius:6px">
    <div class="pick-grid"></div>`;
  const chipsBox = box.querySelector("#tacChips"), grid = box.querySelector(".pick-grid");
  const set = tac => {                                         // 寫入第 k 欄(固定2欄, 不與另一欄重複)
    const a = (inhsel[side][i] || []).slice(); while (a.length < 2) a.push(null);
    if (tac && a[1 - k] === tac) a[1 - k] = null;
    a[k] = tac;
    inhsel[side][i] = a;
    renderSlots(side); closeModal();
  };
  const drawChips = () => {
    chipsBox.innerHTML = TAC_CHIPS.map(c => {
      const lk = lockedCats.includes(c);
      return `<button class="catchip${c === cat ? " on" : ""}${lk ? " locked" : ""}" data-c="${c}"${lk ? " disabled" : ""}>${c}${lk ? "🔒" : ""}</button>`;
    }).join("");
    chipsBox.querySelectorAll(".catchip:not([disabled])").forEach(b => b.onclick = () => { cat = b.dataset.c; drawChips(); draw(); });
  };
  const draw = () => {
    const q = box.querySelector("#tacSearch").value.trim();
    const items = TACTIC_NAMES.filter(t => !NATIVE_TACTICS.has(t) && (cat === "全部" || tacCat(t) === cat) && (!q || t.includes(q)))
      .sort((a, b) => (TIER_RANK[tacTier(a)] ?? 9) - (TIER_RANK[tacTier(b)] ?? 9));   // S→A→B→未分級
    grid.innerHTML = items.slice(0, 150).map(t => {
      const lk = lockedCats.includes(tacCat(t)) && t !== cur;   // 該分類同隊已佔
      const tier = tacTier(t), badge = tier ? `<span class="tier t${tier}">${tier}</span>` : "";
      return `<div class="pick${t === cur ? " on" : ""}${lk ? " locked" : ""}" data-t="${t}"${lk ? ` title="同隊已有${tacCat(t)}戰法"` : ""}>${badge}${t}</div>`;
    }).join("");
    grid.querySelectorAll(".pick:not(.locked)").forEach(p => p.onclick = () => set(p.dataset.t));
  };
  drawChips(); draw();
  box.querySelector("#tacSearch").oninput = draw;
  box.querySelector("#tacClr").onclick = () => set(null);
  $("#modal").classList.remove("hidden");
}

function openEquip(side, i) {
  const n = teams[side][i], g = POOL[n];
  const cfg = {};                                              // 工作副本: {type:[特技1,特技2]}
  for (const s of EQUIP_SLOTS) cfg[s.t] = eqSlot(eqsel[side][i], s.t).slice();
  const box = $("#modal .modal-box");
  box.innerHTML = `<h2 class="gold">${n}・裝備</h2>
    <div class="sub" style="margin:-4px 0 8px">每欄主特技+副特技(雙特技),全身最多8</div>` +
    EQUIP_SLOTS.map(s => {
      const list = eqByType(g, s.t), cur = cfg[s.t];
      const dd = k => `<select data-t="${s.t}" data-k="${k}"><option value="">${k ? "—副特技—" : "—主特技—"}</option>` +
        list.map(x => `<option${x === cur[k] ? " selected" : ""}>${x}</option>`).join("") + `</select>`;
      return `<div class="brow">${s.l}　${dd(0)} ${dd(1)}</div>`;
    }).join("") +
    `<div style="text-align:right;margin-top:14px"><button id="eqSave" class="primary">套用</button></div>`;
  box.querySelectorAll("select[data-t]").forEach(sel => sel.onchange = () => { cfg[sel.dataset.t][+sel.dataset.k] = sel.value || null; });
  box.querySelector("#eqSave").onclick = () => {
    const out = {};
    for (const s of EQUIP_SLOTS) { const v = [...new Set(cfg[s.t].filter(Boolean))]; if (v.length) out[s.t] = v; }  // 去重+去空
    eqsel[side][i] = out; renderSlots(side); closeModal();
  };
  $("#modal").classList.remove("hidden");
}

load();
