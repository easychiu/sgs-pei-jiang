"use strict";
let RAW = {}, POOL = {};
const FACBG = { "魏": "var(--魏)", "蜀": "var(--蜀)", "吳": "var(--吳)", "群": "var(--群)" };
const STATN = { force: "武力", intel: "智力", command: "統率", speed: "速度", all: "全屬性" };
const TYPEN = { active: "主動", charge: "突擊", command: "指揮", passive: "被動" };
const TROOPS = ["騎", "盾", "弓", "槍", "器"];
const cardSrc = n => "cards/" + encodeURIComponent(n) + ".webp";
const teams = { A: [null, null, null], B: [null, null, null] };
const troops = { A: "", B: "" };                  // "" = 自動(依隊伍適性)
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
function statStr(g, t) {                            // 套兵種適性後的面板
  const m = aptMul(g, t);
  return `武${g.base.force * m | 0} 智${g.base.intel * m | 0} 統${g.base.command * m | 0} 速${g.base.speed * m | 0}`;
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
  const [g, t] = await Promise.all([
    fetch("data/generals.json").then(r => r.json()),
    fetch("data/tactics_parsed.json").then(r => r.json())]);
  g.forEach(x => RAW[x.name] = x);
  POOL = SGZ.buildPool(g, t).POOL;
  $("#stat").textContent = `${Object.keys(POOL).length} 武將 · ${t.filter(x => x.type !== "none").length} 戰法`;
  initTabs();
  ["A", "B"].forEach(s => {
    const sel = document.querySelector(`.troop[data-side="${s}"]`);
    sel.innerHTML = `<option value="">自動</option>` + TROOPS.map(x => `<option>${x}</option>`).join("");
    sel.onchange = () => { troops[s] = sel.value; renderSlots(s); $("#simResult").classList.add("hidden"); };
    renderSlots(s);
  });
  $("#runSim").onclick = runSim;
  $("#clearSim").onclick = () => { teams.A = [null, null, null]; teams.B = [null, null, null]; renderSlots("A"); renderSlots("B"); $("#simResult").classList.add("hidden"); };
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
    d.innerHTML = g
      ? `${facBadge(g.faction)}<div><div class="nm">${n} <span class="apt ${g.apt[tr] || ""}">${tr}${aptOf(g, tr)}</span></div><div class="sub">${statStr(g, tr)}</div></div>`
      : `<span class="ph">＋ 點選武將</span>`;
    d.onclick = () => openPicker(side, i);
    box.appendChild(d);
  }
}
function runSim() {
  const A = teams.A.filter(Boolean), B = teams.B.filter(Boolean);
  if (!A.length || !B.length) { alert("兩邊各至少放 1 名武將"); return; }
  const ta = effTroop("A"), tb = effTroop("B");
  const r = SGZ.simulate(POOL, A, B, 3000, ta, tb);
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
    teams.A = [...JSON.parse(li.dataset.team)]; troops.A = li.dataset.troop;
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
    teams[side][idx] = p.dataset.n; renderSlots(side); closeModal();
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
      <small style="color:#b8a987">${"★".repeat(raw.stars || 5)}</small></h2>
      <div style="margin:10px 0">兵種適性：${aptBadges(g)}</div>
      <div class="stat-row"><span><b>武</b> ${g.base.force | 0}</span><span><b>智</b> ${g.base.intel | 0}</span>
      <span><b>統</b> ${g.base.command | 0}</span><span><b>速</b> ${g.base.speed | 0}</span></div>
      <div class="sub">↑ 基礎面板；戰鬥時 ×隊伍兵種適性%（最佳：${bt}${aptOf(g, bt)} → ${statStr(g, bt)}）</div>
      <h3 class="gold" style="margin:14px 0 4px">自帶戰法</h3>
      ${tacticHTML(g)}
    </div></div>
    <div style="text-align:right;margin-top:12px"><button id="toSim" class="primary">加入我方</button></div>`;
  box.querySelector("#toSim").onclick = () => {
    const i = teams.A.indexOf(null); if (i < 0) { alert("我方已滿"); return; }
    teams.A[i] = n; renderSlots("A"); closeModal();
    document.querySelector('.tab[data-tab="sim"]').click();
  };
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }

load();
