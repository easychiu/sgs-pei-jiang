# 引擎已知限制清單

供未來稽核/資料維護引用同一基準。彙整自歷批(批2~批20)資料修正時發現、且短期內不打算
(或無法在不動 `sgz.py`/`engine.js` 引擎程式碼的前提下)修的結構性缺口。所有這裡列出的限制,
在 `docs/data/tactic_corrections.json` / `docs/data/tactics_parsed.json` 對應戰法上都應該
有 `_todo`/`_note`/`_approx` 揭露, 而不是靜默套用近似值。`lint_tactics.py` 的豁免規則
(`has_disclosure`)正是依賴這個「有揭露 = 已知且可接受」的約定。

新增限制時, 請同時: (1) 在此檔案加一節, (2) 在對應戰法的 correction/parsed 條目補
`_todo`/`_approx`, (3) 若該限制是規律性的(影響一整類戰法), 考慮加進 `lint_tactics.py`
的豁免白名單或新增規則。

## 1. 治療(heal)只補「最殘一人」, 無法指定/群體同時治療多人

`apply_effects()` 的 `heal_only` 常駐通道(見 `sgz.py` 的 `k=="heal"` 分支)固定選
`min(alive_and_not_healblocked, key=troop)` 這一人, 不支援「治療我軍群體N人」或「治療我軍
全體」——所有 heal 效果無論原文寫「單體/群體2人/全體」, 實際都只會治療兵力最低的那一人。
這是全庫最大宗的近似來源之一(heal 類戰法全部受影響)。

## 2. 無 cooldown(冷卻)原語

原文常見「每N回合限發動一次」/「發動後進入1回合冷卻」等機制, 引擎的 `rate` 是逐回合獨立
擲骰(無記憶性), 無法表達「發動過一次後接下來M回合不能再發動」。既有近似慣例: 用期望值折算
(EV, expected value)把冷卻的稀釋效果算進 `rate` 裡, 如 `rate_effective = p/(1+p)`(1回合冷卻
且原機率p的情形, 見 臨機制勝 correction 的 `_note`)。

## 3. 無效果轉移(transfer)/治療轉移原語

移花接木類「將受到治療的一部分轉移到自己」機制沒有專屬原語, 只能用近似(如把「轉移量」折
算成一個獨立的 heal 效果, 損失了「轉移」本身要從對方身上扣除的雙向語意)。

## 4. 效果級機率(prob)欄位僅 `counter`/`dodge` 兩種 k 支援, 且 `delayedEq`/`heal` 才會依情境
額外讀 `rate`/`prob`

大多數效果種類(`amp`/`mitig`/`stat`/`stun` 等)一旦被 `apply_effects()` 套用就是
100%發生, 沒有「這個效果本身有X%機率生效」的欄位。原文常見「有25%→50%機率獲得XX狀態」
這種**單一效果的局部機率**(不同於戰法整體的 `rate`/`activationRate`), 目前只能:
- 用 `_approx:"prob-ev"` 折算成期望值(例如把二元效果的 `dur` 或 `val` 乘上機率), 見
  解煩衛(heal coef 已折算 60%機率×72%治療率=0.432)、魚鱗陣(mitig 用機率值本身近似減傷比例)。
- 對於本質上是 binary(全有全無)的效果(如 `insight` 全免疫控制), EV 折算會嚴重失真
  (「縮短持續時間」≠「有機率完全不觸發」), 這類情形應保持誠實 `_todo`, 不強行折算
  (見 `tactic_corrections.json` 142行的案例)。

## 5. 傷兵池(wounded pool)機制

battle 18 新增: 受到的傷害依「當時回合數」轉換成「可救援(計入傷兵池, 治療只能回這部分)」
vs「不可救援(直接陣亡量, 治療無法挽回)」兩部分, 轉換率隨回合遞減
(`WOUNDED_RATES = [0.90,0.90,0.90,0.80,0.80,0.80,0.675,0.675]`, 見 `sgz.py`)。這是遊戲內
實測機制, 已正確建模, 但意味著「治療率X%」不能簡單理解成「回復X%×基礎兵力」——後期回合的
實際回復量會因傷兵池餘量不足而被削弱, 純數值比對(不跑模擬)容易誤判治療類戰法強度。

## 6. 近似慣例一覽(EV折算/群控隨機/點數mult豁免清單等)

以下是歷批已建立、且被視為「可接受的標準近似」的清單, 遇到同類情形應優先沿用既有慣例,
不要各自發明新近似方式(維持全庫一致性)。

### 6.1 EV(期望值)折算
「每回合X%機率觸發」若不能用 `when`(rounds/from/until/parity/every)精確表達回合窗口
(通常是因為同一戰法內還有其他效果需要在不同時間窗常駐生效, 若都套用 `t["when"]` 會把
不該延後的效果也延後, 見下方6.4), 改用 `rate`(戰法整體發動率)或效果 `val`/`coef` 直接
乘上機率折算成穩態期望值。

### 6.2 群體控制隨機選標
「群體N人」的控制效果(stun/silence/disarm/taunt/chaos, 即 `ctrl_k` 集合)用
`pick_targets(enemies, cnt)` 隨機不重複選 cnt 個目標, 不支援按條件(如「兵力最高的N人」)
篩選後再隨機——只有 `targetSel`(單體, 見下方6.5) 支援條件選標, 群體控制只能全隨機。

### 6.3 點數(flat)vs百分比(mult) —— 批12 ModeA 修正基準
原文常見「統率提升X→Y點」這種**固定點數**加成, 早期整批重解誤用 `stat.mult`(百分比乘算)
表示, 批12 ModeA 已系統性修正為 `stat.add`(固定值平加)。日後若發現任何戰法仍是
「點」語意卻用了 `mult`, 應視為同一類回歸(bug), 直接修正, 不需要另外近似。

### 6.4 e.when 的「母戰法無 t.when」限制
效果級 `e.when` 只在**母戰法本身沒有 `t.when`** 的情形下, 由 `fight()` 回合迴圈的通用
掃描(非 heal 種類, 見 `sgz.py` 1180-1196行)在符合窗口時套用一次。若母戰法 `t.when` 已存在
(如某戰法整體只在第N回合觸發), 該戰法內其餘效果(即使自己也想要不同窗口)的 `e.when` 會被
`skip_when_effects` 邏輯忽略, 因為準備階段套用邏輯是「先看 t.when, 沒有才落到效果自己的
e.when」, 兩者不能同時套用不同窗口在同一戰法的不同效果上(這正是 heal 需要獨立通道
`heal_only` 的原因——heal 的 e.when 優先於 t.when 判斷, 其餘效果種類目前沒有這個優先權)。
這是「一個戰法內有多個不同時間窗效果」時常見的取捨來源, 遇到時通常用 EV 折算(6.1)或
選擇性只精確表達其中一個窗口、其餘維持常駐近似。

### 6.5 targetSel(指定選標準則) vs 群體隨機(ctrl_k)
`targetSel` 只支援**單體**依準則挑選(`minTroop`/`maxForce`/`minIntel`/`maxIntel`/
`minCommand`/`mostDamaged`, 見 `sgz.py` `TARGETSEL_KEY`), 且只能選我方或敵方整體池,
無法表達「隊伍固定 index 位置」(如「敵軍主將」=敵方隊伍 index 0 這種**結構性位置**,
不是「兵力/屬性最高最低」這種**動態計算的準則**)。`extraHits` 段有 `who:"enemyLeader"`
可以固定選敵方 index 0, 但**主效果(`effects`)層級沒有對應的 `who` 值**——這是本批(批20)
守而必固「敵軍主將」被迫近似成「隨機挑1名敵軍」的根因, 詳見下方第7節。

### 6.6 「受X或Y最高一項影響」/「受自身最高屬性影響」的 scale 近似
`scale` 只支援單一固定屬性(`intel`/`force`/`command`/`speed`/`charm`), 原文若寫「受武力
或智力最高一項影響」(如 解煩衛/益其金鼓/魚鱗陣)或「受自身最高屬性影響」(如 扶危定傾的
首段減傷), 無法動態判斷「哪個屬性當下比較高」。近似慣例: 固定取其中一個具代表性的屬性
(通常武將卡面主屬性偏向的那一項, 如 force), 或保守不加 scale(如扶危定傾的5屬性最高值情形,
候選過多且無主次之分, 不強加單一猜測)。

### 6.7 「疊加/可疊加」的疊加次數 vs push_add 同源刷新
引擎的 `push_add`/`push_mod`/`push_stat_add` 對「同 kind + 同 src(戰法名)」的效果採
**刷新而非疊加**(見 `sgz.py` `push_add` docstring)。原文寫「可疊加N次」的戰法(如一力拒守
「最多疊加2次」)若同一戰法重複觸發, 引擎只會刷新成同一份效果值, 不會真的疊加到N倍。這是
已知的低估來源, 目前沒有通用解法(需要為疊加類效果新增獨立的「疊加層數」狀態機制, 類似
`stack` 原語但要能綁定到任意 k, 超出現有 `stack`(僅支援自身每回合固定遞增1層增傷)的適用
範圍)。

## 7. 引擎已知限制: 效果級選標無「敵軍主將(隊伍固定 index)」原語

**發現於**: 批20, 守而必固「戰鬥開始時，嘲諷敵軍主將」。

`taunt`(嘲諷)效果的 `who` 只支援 `self`/`leader`/`subs`/`enemy`/`ally` 這幾種集合式選標
(`enemy` = 敵方全體或依 `ctrl_k` 邏輯隨機選N人), 沒有對應 `extraHits` 段 `who:"enemyLeader"`
的「固定選敵方隊伍 index 0(主將位)」。當前用 `n=1`(戰法頂層) 讓 `taunt` 的群體隨機選標邏輯
退化成「隨機挑1名敵軍」, 這比精確的「固定敵方主將」更寬鬆錯位(可能命中副將而非主將)。

**同類受影響戰法**: 任何原文明確寫「敵軍主將」/「我方主將」作為主效果目標、但目標不是靠
屬性動態計算(那種可以用 `targetSel` 解決, 見6.5)而是純粹「隊伍固定位置」的戰法, 都有這個
缺口(如果本批之外還有其他戰法用到, 應一併補上此節的揭露慣例)。

**修法方向(供未來引擎端擴充參考, 本批不動引擎)**: 在 `sgz.py` 的 `apply_effects()` 主
`who` 判斷式(886-913行)新增 `who=="enemyLeader"`/`who=="allyLeader"` 分支, 邏輯與
`extraHits` 段既有的 `enemyLeader` 完全相同(`dests=[foes[0]] if foes and foes[0].alive
else []`), 讓效果層級也能精確表達「固定隊伍主將位」這個目標, 而不必依賴頂層 `n`/群體隨機
近似。

## 8. 引擎已知限制: 反應式觸發(`when.on`)路徑不支援 `choices`(擇一分支)

**發現於**: 批20, 魅惑「有22.5%→45%機率使攻擊者進入混亂/計窮/虛弱狀態的一種」(三選一)。

`choices`(批16原語, 按權重隨機選一組效果套用)只在 `fight()` 回合迴圈的主動/指揮/被動常駐
輪詢派發路徑被讀取(`sgz.py` 約1225-1234行, `t0.get("choices")` 判斷式), 該路徑**明確排除**
`t0["when"].get("on")` 為真的戰法(見1225-1226行 `not(t0.get("when") and
t0["when"].get("on"))`)。所有 `when.on:"attacked"`/`when.on:"damaged"` 的反應式戰法完全
走獨立的 `on_hit()` 事件觸發路徑(1103-1121行), 該路徑只讀 `t["coef"]`/`t["effects"]`/
`t.get("extraHits")`, 從未讀取 `t.get("choices")`。

**後果**: 若在反應式戰法上寫入 `choices`, 資料層看起來像是支援了「擇一分支」機制, 但引擎
運行時完全不會消費這個欄位——`on_hit()` 只會執行 `t["effects"]`(固定的), `choices` 形同
虛設。這比「已知未建模但誠實揭露」更危險, 因為它讓人誤以為機制已經修好。

**同類受影響戰法**: 任何 `type` 為 `passive`/`command` 且帶 `when.on` 的「N選一」反應式
戰法, 都無法用 `choices` 表達多選一, 只能維持「固定選其中一種效果+揭露另外N-1種未建模」的
近似(見魅惑, 選了 silence 作代表, 混亂/虛弱兩種缺失)。

**修法方向(供未來引擎端擴充參考, 本批不動引擎)**: 在 `on_hit()` 內(`sgz.py` 1116-1121行,
`if t["coef"]:`/`if t["effects"]:` 之前)比照回合迴圈的 `t = dict(t0, **pick_choice(
t0["choices"])) if t0.get("choices") else t0` 邏輯, 先做一次 choices 派發, 再用派發後的
`t` 執行後續 `coef`/`extraHits`/`effects`。

## 9. 引擎已知限制: 觸發機率的「按施放者身份條件縮放」無判斷點

**發現於**: 批20, 魅惑「若自身為女性，觸發機率額外受智力影響」。

`Unit.g.gender` 資料確實存在(`General.gender`, 已用於緣分 `apt_add` 條件判斷, 見 `sgz.py`
250行), 但 `round_ok()`/`apply_effects()` 的 `when`/`scale` 機制只支援「回合窗口條件」
(rounds/from/until/parity/every/hpBelow/hpAbove) 與「固定屬性縮放」(scale:"intel"等),
沒有「依施放者的某個離散身份屬性(性別/陣營/兵種適性等級)條件切換觸發機率是否額外縮放」這種
判斷點。這與6.5(targetSel 無法選隊伍固定位置)、批6/批7跳過的「主將」概念缺口(見
`reparse_effects.py` CHARGEUP_ADD/RATE_SCALE_PLAN 註解, 三勢陣/十二奇策等)同屬一類
「引擎缺乏施放者條件分支原語」的問題, 只是條件種類不同(性別 vs 隊伍位置 vs 陣營)。

**修法方向**: 需要在 rate 擲骰前(`sgz.py` `fire = random.random() < t0["rate"] + ...`
一類位置)插入依 `caster.g.gender`/`caster.g.faction` 等條件判斷是否套用額外 `scale` 的
分支, 屬引擎邏輯擴充, 非資料層可迂迴解決。

## 10. `insight`(洞察) 等二元(binary)效果無法用 n/nMax 限制受益人數

**發現於**: 批20 lint R9, 虛實奇謀「使群體(2-3人)獲得剛毅(insight)」。

`insight`/`stun`/`silence`/`disarm`/`chaos`/`taunt` 這些控制/免控效果, 只有後五者
(`ctrl_k = {"stun","silence","disarm","taunt","chaos"}`, 見 `sgz.py` 887行) 在
`who=="enemy"` 時會依戰法 `n`/`nMax` 做群體隨機選標；`insight` 不在 `ctrl_k` 集合內,
其 `who` 只能是全體(`ally`=全體我軍/`self`=施放者), 效果本身即使帶 `n`/`nMax` 欄位也
完全不會被讀取(純幽靈欄位, 見 `lint_tactics.py` R9)。原文「群體2-3人」這種受益範圍限定
語意目前只能靠 `_note` 揭露, 無法用資料實際限制人數(比原文更寬鬆, 全體我軍都會受益)。

**同類受影響效果**: 任何原文要求 `insight`「只給群體N人/單體1人」而非「全體我軍」的戰法,
都有這個缺口, 應以 `_note` 誠實揭露(較原文寬鬆), 不要保留死的 `n`/`nMax` 欄位誤導未來
維護者以為有在限制人數。

## 11. 「抵禦(格擋)」單次防禦機制無專屬原語

**發現於**: 揮兵謀勝、魚鱗陣等「受到傷害時有X%機率獲得1次抵禦(完全格擋當次傷害)」。

`shield`(護盾) 原語是「固定吸收量, 吸滿或到期為止」, 沒有 `prob` 欄位(見
`lint_tactics.py` PER_KIND_FIELDS), 無法表達「按機率觸發的單次完全格擋」這種二元機制。
既有近似慣例: 用 `mitig`(持續性減傷)且直接把「觸發機率」的數值當作「減傷比例」代入
(如 25%機率抵禦 → 近似成 25%持續減傷), 這是兩種不同機制的數值代入近似(機率意義變成
了強度意義), 已在多個戰法沿用(揮兵謀勝、魚鱗陣, 見批20)。

---

## 附錄: 全部引擎支援的效果原語(k)一覽

供對照哪些機制已有原語、哪些仍是缺口。見 `sgz.py` `apply_effects()` 的 k 分支
(792-1051行)與 `lint_tactics.py` 的 `PER_KIND_FIELDS`。

`heal` / `settle` / `redirect` / `amp` / `mitig` / `stun` / `silence` / `disarm` /
`chaos` / `ambush` / `insight` / `immune` / `first` / `stat` / `dot` / `extra` /
`stack` / `decay` / `swap` / `pierce` / `counter` / `taunt` / `shield` / `dodge` /
`surehit` / `healblock` / `lifesteal` / `rateup` / `chargeup` / `healBoost` /
`healGiven` / `fakeReport` / `dispel`

戰法/效果級輔助欄位: `when`(rounds/from/until/parity/every/on/hpBelow/hpAbove) /
`targetSel` / `ifTargetHas` / `scale` / `choices` / `extraHits` / `everyN` /
`hitsRepeat` / `lockTarget` / `undispellable` / `nativeOnly` / `inheritedOnly` /
`prepOnly`
