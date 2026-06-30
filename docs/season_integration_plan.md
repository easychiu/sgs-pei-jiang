# 賽季特色整合 — 最終方案（綜合提案 + 架構審閱 + 實戰數值研究）

整合三份來源:原提案、架構代理(扣我們15原語/雙引擎成本)、實戰代理(上網查真實數值)。

## 〇、修正原提案的兩個誤解

1. **官渡 2+1 不是「+28 固定評分」** —— 陣營協力基礎是 **+10% 全屬性**(3人同陣),2+1 給 70% = **+7% 全屬性**。應作用在**屬性乘數層**(強將等比例受益),非固定加分。
2. **巾幗不是「+15% 評分加權」** —— 是在**兵種適性乘數層加法 +15%**(S槍 120%→135%)。
3. gender 欄位**已存在**(男172/女21),不用再補。

## 一、核心洞察:高CP賽季幾乎都是「靜態屬性修正」,我們引擎已有機制

值得做的賽季,效果都落在我們現成的能力上 → 成本低,且**可同時進 score 與 simulate**(不必像原提案只停在 score):

| 賽季效果類型 | 我們現成的對應 |
|------|------|
| 全屬性 +X%(官渡/巾幗/都尉) | `add` 養成加值(每將四維加點) 或 stat mult 效果 |
| 兵種適性強制 S(漢焰/都尉天賦) | Unit 算屬性時 apt 覆寫為 S(×1.20),小改 |
| 全程減傷/增傷(信符青羅) | amp / mitig 原語 |
| 固定屬性點(信符候印 +14) | `add` 養成加值 |
| 陣營 2+1 | score 的同陣營判斷 + sim 的屬性注入 |

所以**賽季 = 一組「自動套用的修正預設」**,套在現有 add/stat/amp/mitig/apt 機制上。雙引擎同步成本:每個 modifier 約數行。

## 二、優先序(兩代理一致)

| 階段 | 賽季 | 機制 | 真實數值 | 對應 |
|------|------|------|----------|------|
| **P0** | 官渡之戰 | 2+1 陣營 | 3同=+10%全屬, 2同=+7% | faction 加成改乘數 |
| **P0** | 北定中原 | 巾幗 | 女將適性層 +15% | 女將 apt 額外 +0.15 |
| **P0** | 漢焰長明 | 雙S兵種 | 選2將各1兵種→S(+20%) | apt 覆寫 S |
| **P1** | 英雄命世 | 都尉天賦 | 身經百戰+5%四維、天賦升S | add + apt 覆寫 |
| **P1** | 王師秉節 | 信符靜態套 | 候印+14點、青羅-1.8%受傷 | add + mitig |
| **P2** | 軍爭地利 | 地形指令 | 各地形給對應兵種增益 | 兵種條件 amp/mitig/stun |
| **P2** | 赤壁之戰 | 水軍適性 | 水地 S+5/A+2/B-1/C-5% | 需補 water 適性欄 |
| **P2** | 九州兵興 | 治軍戰法 | 強化版免費傳承 | 戰法評分上浮 / inherit |
| **跳過** | 兵戰四時、早期賽季 | 資源/地圖/內政 | 無戰鬥數值 | `modifiers:[]` |

戰中觸發類(信符鼓吹35%抵禦、奇略卡)→ **初期用期望值折算**(如35%抵禦≈+5%等效),不做逐回合。

## 三、資料 Schema(Gemini 照此補 scenarios.json)

每個賽季加 `modifiers[]`:
```json
{
  "id": "pk_guandu",
  "modifiers": [
    { "id":"guandu_2plus1", "label":"2+1陣營協調", "inject":"both", "type":"faction_scale",
      "cond":{"sameFactionCount":2}, "effect":{"fullBonus":0.10,"partialBonus":0.07,"partialThreshold":2} }
  ]
}
```
```json
{ "id":"pk_beiding", "modifiers":[
  { "id":"jinguo","label":"巾幗","inject":"both","type":"apt_add","cond":{"gender":"Female"},
    "effect":{"aptAdd":0.15}, "userToggle":true }
]}
```
```json
{ "id":"pk_hanyan", "modifiers":[
  { "id":"double_s","label":"雙S兵種","inject":"both","type":"apt_override",
    "cond":{"playerPick":true,"maxSlots":2},"effect":{"apt":"S"} }
]}
```
```json
{ "id":"pk_wangshi", "modifiers":[
  { "id":"houyin","label":"候印信符","inject":"both","type":"stat_flat","effect":{"all":14},"userToggle":true },
  { "id":"qingluo","label":"青羅信符","inject":"both","type":"effect","effect":{"k":"mitig","val":0.018,"who":"ally"},"userToggle":true }
]}
```
欄位:`inject`=score_only|unit_init|both;`type`=faction_scale|apt_add|apt_override|stat_flat|effect(直接給15原語的k/val);`cond`=觸發條件(gender/faction/playerPick);`userToggle`=需UI讓玩家開關/選擇。

## 四、分工

- **Gemini 補**:scenarios.json 的 `modifiers[]`,P0→P1→P2 順序;查不到精確值的標 `_conf:"est"`。C類賽季標 `modifiers:[]`。
- **我做引擎**:
  1. score()/simulate() 接 `scenario` 參數,讀 modifiers 套用(faction_scale/apt_add/apt_override/stat_flat → 注入現有 add/apt;effect → 走 apply_effects)。Python/JS 同步。
  2. UI:賽季下拉已有 → 選賽季後,有 `userToggle`/`playerPick` 的 modifier 顯示對應開關(如巾幗勾選、漢焰選2將兵種、信符選套裝)。

## 五、與原提案的差異(結論)

| 項目 | 原提案 | 本方案 |
|------|--------|--------|
| 2+1 | +28 固定分 | +7% 全屬性(乘數) |
| 巾幗 | +15% 評分 | 適性層 +15%(加法) |
| 作用範圍 | 只 score | score + simulate(因已有 add/apt 機制,成本低) |
| 驅動 | if/elif 硬編碼 | schema(modifiers[])驅動,加賽季不改程式 |
| gender | 說要補 | 已有 |

## 六、下一步

先做 P0 三個(官渡/巾幗/漢焰)——都是純靜態屬性,改動小、效果顯著、可立即驗收。Gemini 補這三個的 modifiers,我接引擎 + UI。
