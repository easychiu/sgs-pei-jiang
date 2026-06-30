"use strict";
let RAW = {}, POOL = {};
const FACBG = { "魏": "var(--魏)", "蜀": "var(--蜀)", "吳": "var(--吳)", "群": "var(--群)" };
const STATN = { force: "武力", intel: "智力", command: "統率", speed: "速度", all: "全屬性" };
const TYPEN = { active: "主動", charge: "突擊", command: "指揮", passive: "被動" };
const TROOPS = ["騎", "盾", "弓", "槍", "器"];
const cardSrc = n => "cards/" + encodeURIComponent(n) + ".webp";
const teams = { A: [null, null, null], B: [null, null, null] };
const troops = { A: "", B: "" };                  // "" = 自動(依隊伍適性)
const bsel = { A: [null, null, null], B: [null, null, null] };  // 各將兵書(null=預設主兵書)
const eqsel = { A: [null, null, null], B: [null, null, null] }; // 各將裝備(null=無)
const builds = { A: [null, null, null], B: [null, null, null] }; // 養成(null=預設: 進階滿+主屬性)
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
function defaultBingshuCfg(g) {
  const cat = (g.bingshuCats || [])[0] || null;
  const mains = cat ? (SGZ.mainByCat()[cat] || []) : [], subs = cat ? (SGZ.subByCat()[cat] || []) : [];
  return { on: true, category: cat, main: mains[0] || null, subs: subs.slice(0, SUBS_MAX) };
}
const getBsel = (side, i) => bsel[side][i] || defaultBingshuCfg(POOL[teams[side][i]]);
const bsNames = cfg => cfg.on ? [...new Set([cfg.main, ...(cfg.subs || [])].filter(Boolean))] : [];
function bselSummary(cfg) {
  if (!cfg.on) return "兵書：<b>關</b>";
  const m = BINGSHU_CAT[cfg.category] || { i: "📖" };
  return `<span style="color:${m.c || "var(--gold2)"}">${m.i} ${cfg.category || "—"}</span>・${cfg.main || "—"}＋${(cfg.subs || []).length}副`;
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
  const [g, t, bs, bo, eq] = await Promise.all([
    fetch("data/generals.json").then(r => r.json()),
    fetch("data/tactics_parsed.json").then(r => r.json()),
    j("data/bingshu_parsed.json"), j("data/bonds_parsed.json"), j("data/equips_parsed.json")]);
  g.forEach(x => RAW[x.name] = x);
  POOL = SGZ.buildPool(g, t, bs, bo, eq).POOL;
  $("#stat").textContent = `${Object.keys(POOL).length} 武將 · ${t.filter(x => x.type !== "none").length} 戰法`;
  initTabs();
  ["A", "B"].forEach(s => {
    const sel = document.querySelector(`.troop[data-side="${s}"]`);
    sel.innerHTML = `<option value="">自動</option>` + TROOPS.map(x => `<option>${x}</option>`).join("");
    sel.onchange = () => { troops[s] = sel.value; renderSlots(s); $("#simResult").classList.add("hidden"); };
    renderSlots(s);
  });
  $("#runSim").onclick = runSim;
  $("#clearSim").onclick = () => { for (const s of ["A", "B"]) { teams[s] = [null, null, null]; bsel[s] = [null, null, null]; eqsel[s] = [null, null, null]; builds[s] = [null, null, null]; } renderSlots("A"); renderSlots("B"); $("#simResult").classList.add("hidden"); };
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

function availEquips(g) {
  return Object.values(SGZ.equips()).filter(e => !e.exclusive || e.exclusive === g.name).map(e => e.name);
}
function renderSlots(side) {
  const tr = effTroop(side);
  const sel = document.querySelector(`.troop[data-side="${side}"]`);
  if (sel && !troops[side]) sel.options[0].text = `自動（${tr}）`;
  const box = document.querySelector(`.team[data-side="${side}"] .slots`);
  box.innerHTML = "";
  for (let i = 0; i < 3; i++) {
    const n = teams[side][i], g = n && POOL[n];
    const d = document.createElement("div");
    d.className = "slot";
    if (g) {
      const bd = getBuild(side, i), bc = getBsel(side, i);
      d.innerHTML = `${facBadge(g.faction)}<div style="flex:1">
        <div class="nm">${n} <span class="apt ${g.apt[tr] || ""}">${tr}${aptOf(g, tr)}</span>
          <button class="cog" title="養成加點">⚙</button></div>
        <div class="sub">${statStr(g, tr, bd.alloc)}</div>
        <div class="sub" style="color:#9a8b6a">${buildSummary(bd)}</div>
        <div class="sub">${bselSummary(bc)} <button class="book" title="兵書設定">📖</button>　裝備 <select class="eq"></select></div></div>`;
      d.querySelector(".cog").onclick = e => { e.stopPropagation(); openBuild(side, i); };
      d.querySelector(".book").onclick = e => { e.stopPropagation(); openBingshu(side, i); };
      const eqs = availEquips(g), curE = eqsel[side][i] || "";
      const eq = d.querySelector(".eq");
      eq.innerHTML = `<option value="">無</option>` + eqs.map(x => `<option${x === curE ? " selected" : ""}>${x}</option>`).join("");
      eq.onclick = e => e.stopPropagation();
      eq.onchange = e => { e.stopPropagation(); eqsel[side][i] = eq.value || null; };
    } else {
      d.innerHTML = `<span class="ph">＋ 點選武將</span>`;
    }
    d.onclick = () => openPicker(side, i);
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
  const A = [], B = [], bsA = [], bsB = [], eqA = [], eqB = [], adA = [], adB = [];
  teams.A.forEach((n, i) => { if (n) { A.push(n); bsA.push(bsNames(getBsel("A", i))); eqA.push(eqsel.A[i]); adA.push(buildAdd(getBuild("A", i), getBsel("A", i).on)); } });
  teams.B.forEach((n, i) => { if (n) { B.push(n); bsB.push(bsNames(getBsel("B", i))); eqB.push(eqsel.B[i]); adB.push(buildAdd(getBuild("B", i), getBsel("B", i).on)); } });
  if (!A.length || !B.length) { alert("兩邊各至少放 1 名武將"); return; }
  const ta = effTroop("A"), tb = effTroop("B");
  const r = SGZ.simulate(POOL, A, B, 3000, ta, tb, bsA, bsB, eqA, eqB, adA, adB);
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
    teams.A = [...JSON.parse(li.dataset.team)]; troops.A = li.dataset.troop; bsel.A = [null, null, null]; eqsel.A = [null, null, null]; builds.A = [null, null, null];
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
    teams[side][idx] = p.dataset.n; bsel[side][idx] = null; eqsel[side][idx] = null; builds[side][idx] = null; renderSlots(side); closeModal();
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
    teams.A[i] = n; bsel.A[i] = null; eqsel.A[i] = null; builds.A[i] = null; renderSlots("A"); closeModal();
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
  const cats = g.bingshuCats || [];
  const box = $("#modal .modal-box");
  const render = () => {
    const mains = cfg.category ? (SGZ.mainByCat()[cfg.category] || []) : [];
    const subs = cfg.category ? (SGZ.subByCat()[cfg.category] || []) : [];
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
      cfg.main = (SGZ.mainByCat()[cfg.category] || [])[0] || null;
      cfg.subs = (SGZ.subByCat()[cfg.category] || []).slice(0, SUBS_MAX);
      render();
    });
    const mainSel = box.querySelector("#bsMain");
    mainSel.innerHTML = mains.map(x => `<option${x === cfg.main ? " selected" : ""}>${x}</option>`).join("") || `<option value="">—</option>`;
    mainSel.onchange = () => cfg.main = mainSel.value || null;
    box.querySelectorAll(".bsSub").forEach(sel => {
      const x = +sel.dataset.x;
      sel.innerHTML = `<option value="">無</option>` + subs.map(s => `<option${s === (cfg.subs || [])[x] ? " selected" : ""}>${s}</option>`).join("");
      sel.onchange = () => { cfg.subs = cfg.subs || []; cfg.subs[x] = sel.value || null; };
    });
    box.querySelector("#bsOn").onchange = e => { cfg.on = e.target.checked; render(); };
    box.querySelector("#bsSave").onclick = () => { bsel[side][i] = cfg; renderSlots(side); closeModal(); };
  };
  render();
  $("#modal").classList.remove("hidden");
}

load();
