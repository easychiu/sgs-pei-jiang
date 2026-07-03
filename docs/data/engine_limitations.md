# 引擎已知限制清單

供未來稽核/資料維護引用同一基準。彙整自歷批(批2~批20)資料修正時發現、且短期內不打算
(或無法在不動 `sgz.py`/`engine.js` 引擎程式碼的前提下)修的結構性缺口。所有這裡列出的限制,
在 `docs/data/tactic_corrections.json` / `docs/data/tactics_parsed.json` 對應戰法上都應該
有 `_todo`/`_note`/`_approx` 揭露, 而不是靜默套用近似值。`lint_tactics.py` 的豁免規則
(`has_disclosure`)正是依賴這個「有揭露 = 已知且可接受」的約定。

新增限制時, 請同時: (1) 在此檔案加一節, (2) 在對應戰法的 correction/parsed 條目補
`_todo`/`_approx`, (3) 若該限制是規律性的(影響一整類戰法), 考慮加進 `lint_tactics.py`
的豁免白名單或新增規則。

## 維護規約: `_note`/`_todo` 聲稱的數值必須與資料實際值一致(批23新增, `lint_tactics.py` R14)

**背景**: 批23全庫核對時發現多筆「stale 揭露」——`_note`/`_todo` 文字曾經正確描述某個
`coef`/`dur`/`rate` 應該是多少, 但後續資料改動(如 correction 的 `set` 被拿掉、或新一輪
`reparse`覆寫)沒有同步更新揭露文字, 導致文字聲稱的數值與 `tactics_parsed.json` 實際欄位
矛盾(如神機妙算 `_todo` 曾聲稱「coef=1.28取主將分支值」但 `correction` 從未真正 `set`
`coef`, 實際欄位停留在舊值 1.0; 錦帆軍同理聲稱 coef=1.28 但實際仍是 0.64)。這類矛盾比
「完全沒揭露」更危險——維護者讀到 `_note` 會誤以為資料已經是對的, 不會再去複查實際欄位。

**規約**: 修改任何戰法的 `coef`/`dur`/`rate`/`n`/`nMax` 時, 若對應的 `_note`/`_todo` 文字
提到這些欄位的具體數字, 必須同步檢查文字是否仍與新值一致。歷史敘述(如「批X: 原coef=Y
改為Z」「移除舊版heal(coef=W)」)是合法且應保留的, 但「宣稱目前狀態是 X」的斷言句必須與
`set`/`effects` 實際寫入的值相符。

**自動檢查**: `lint_tactics.py` R14 掃描 `_note`/`_todo`/`_note2`/`_note_self`/`_approx`
文字裡 `coef=X`/`dur=X`/`rate=X` 這類精確斷言句型, 與該戰法(頂層)或該效果(effects[i])的
實際欄位值比對, 不一致則報違規。刻意排除 `n`/`nMax`(頂層 `n` 與效果級 `e.n` 是不同概念但
常共用「n=X」措辭, 比對會系統性誤判, 見批23 R14 設計註解), 也用歷史敘述關鍵字(原/舊/之前/
過去/曾/取代/先前/移除/刪除/歸零/誤用等)過濾掉合法的「敘述舊值」句型, 只抓「未被歷史詞
修飾的當前狀態斷言」, 維持低誤報優先原則。

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

## 4. 效果級機率(`e.rate`)折算 —— 批23 A4 已解決: 通用 `e.rate` 判定(非僅 `counter`/`dodge`)

**歷史限制(批22前)**: 效果級機率(prob)欄位僅 `counter`/`dodge` 兩種 k 原生支援 `prob` 欄位,
且 `delayedEq`/`heal`(僅 `e.when.on` 反應式路徑)才會依情境額外讀 `rate`/`prob`。大多數效果
種類(`amp`/`mitig`/`stat`/`stun` 等)一旦被 `apply_effects()` 套用就是100%發生, 沒有「這個
效果本身有X%機率生效」的通用欄位。

**批23 A4 修復**: `apply_effects()`(`engine.js`/`sgz.py`)現在對**任何** `k` 種類的效果, 只要
帶 `e.rate` 欄位, 套用前都會先擲骰判定(`rnd() < e.rate`, 未帶 `e.rate` 的效果維持無條件套用,
向後相容)。呼叫端若已自行讀取並擲骰過同一個 `e.rate`(如 `onHit`/`delayedEq` 的合成單效果
呼叫), 傳 `opt.rateChecked`(JS)/`rate_checked`(Python) 旗標避免重複擲骰(機率被平方, 造成
低估)。原文「有25%→50%機率獲得XX狀態」這種**單一效果的局部機率**現在可以精確用 `e.rate`
表達(套用時真實擲骰, 比 EV 折算更接近真實方差), 不需要再折算成期望值。

**仍維持 EV 折算的情形**(非本次修復範圍, 沿用既有慣例): 原文機率描述無法精確對應到單一
`e.rate` 判定點的情形(如同一戰法內多個效果共用同一句機率描述、或機率本身依動態條件縮放),
仍可用 `_approx:"prob-ev"` 折算成期望值, 見解煩衛(heal coef 已折算 60%機率×72%治療率=0.432)、
魚鱗陣(mitig 用機率值本身近似減傷比例)。對於本質上是 binary(全有全無)的效果(如 `insight`
全免疫控制)且無法明確定位單一 `e.rate` 擲骰點時, EV 折算仍會嚴重失真, 應保持誠實 `_todo`,
不強行折算。

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

## 11. 「抵禦(格擋)」單次防禦機制 —— 批22新增 `block` 原語(次數型格擋), 部分場景已解決

**發現於**: 揮兵謀勝、魚鱗陣等「受到傷害時有X%機率獲得1次抵禦(完全格擋當次傷害)」。

批22新增 `block` 原語(`k:"block"`, 欄位 `val`/`times`, 見 `sgz.py`/`engine.js` 的
`push_block`/`consume_block`/`hit()`): `val`(1.0=完全格擋當次傷害, 0.x=部分減傷如
「警戒」的0.4基礎值受智力縮放)、`times`(剩餘次數, 每次受擊消耗1次, 用光即失效)。
判定順序 `dodge → block → shield → 傷害`(見 `hit()`)。同源(同一戰法名)再次施加時
**疊加次數**(`push_block` 內部處理, 不同於 `push_add`/`push_mod` 的「同源刷新覆蓋」
慣例), 貼合戰報「目前抵禦總次數為N」的用語。`dispel(buffs)` 會清除格擋層(視為防禦性
增益, 同 `shield` 慣例)。

**已用 `block` 精確重建的場景**(戰報/官方文字明確給出「N次抵禦/警戒」, 且是**準備階段
或觸發當下一次性授予固定次數**, 非「每回合持續機率補充」): 靈動(裝備, 1次)、槍舞如風
(2次)、折衝禦侮(主將2次)、白衣渡江(首回合1次)、勇者得前(1次)、機鑑先識(準備階段固定
2次「警戒」, val=0.4 scale:intel)、赴湯蹈火(中段回合多層疊加, 官方文字無精確層數,
`times:5` 為 `_est` 粗估)。

**仍維持 `mitig` EV折算近似、未改用 `block` 的場景**: 揮兵謀勝(「每回合35%→70%機率
獲得1次抵禦」)、魚鱗陣(「前3回合每回合12.5%→25%機率獲得1次抵禦」)、枕戈坐甲(兵書,
「戰鬥第2-6回合每回合25%機率獲得1次抵禦」)。這些是**持續性機率補充**(每回合都重新
擲骰判定是否再拿到一次格擋機會), 而非一次性固定次數授予。`block` 原語本身只是「已擁有
的格擋次數」的計次器, 不解決「如何逐回合重新判定是否新增格擋次數」這個更上層的問題——
引擎目前只有 `heal` 種類走 `applyPassives({healOnly:true})` 的逐回合重新擲骰通道(見
第4節), 其餘 `k`(含 `block`)在準備階段套用後不會逐回合重新判定/追加。要精確重建這幾個
戰法, 需要新增「非heal效果的逐回合機率重擲」通用機制(修改 `fight()`/`applyPassives`
的核心分派邏輯), 超出批22「新增block原語」的授權範圍, 故維持 `mitig`(把機率值當減傷
比例代入)的既有近似, 已在對應 correction 的 `_note` 誠實揭露此工程判斷。

**同類受影響戰法**: 任何原文寫「每回合X%機率獲得N次抵禦/警戒」(持續性機率補充)的戰法,
都應優先評估是否為「一次性固定次數」(可用 `block` 精確表達)或「持續性機率補充」(現況
仍需 `mitig` EV近似, 待未來擴充逐回合重擲機制)。

## 12. 「依目標(而非施放者)當下狀態縮放」無對應 scale 原語

**發現於**: 批21, 鴟苕鳳姿「普通攻擊傷害提高40%→80%，受目標損失兵力影響」。

`scale` 機制(`sv_val`/`sv_add`/`scale_of()`, 見 `sgz.py`)只支援「施放者(caster)自身當下
固定屬性」縮放(`intel`/`force`/`command`/`speed`/`charm`), 縮放函式簽名固定是
`scale_of(caster, e["scale"])`——沒有「依目標(target)當下某個動態狀態(如已損失兵力
百分比)縮放」的路徑。原文「受目標損失兵力影響」的縮放基準是**目標**逐次攻擊前才知道
的兵力損耗比例(隨戰鬥推進動態變化), 與現有 `scale` 完全是不同的一個維度(施放者固定
屬性 vs 目標動態戰鬥狀態), 現況只能固定取滿級值近似(可能高估早期回合、低估目標接近
陣亡時的真實加成)。

**同類受影響戰法**: 任何原文寫「受目標XX影響」(而非「受自身XX影響」)的動態縮放語意,
都有此缺口, 應在 `_todo` 誠實揭露, 不要誤用現有 `scale` 欄位(它只會被解讀成施放者
屬性, 套用後語意完全跑掉)。

## 13. 普通攻擊(pick_target/pick_target_chaos)不讀 targetSel/lockTarget

**發現於**: 批21, 鴟苕鳳姿「普通攻擊...且鎖定敵方兵力最低單體」。

`targetSel`(依準則選標, 見6.5節) 與 `lockTarget`(首次選定後鎖定沿用) 都只在**戰法自身
的 coef 段/effects 段/extraHits 段**生效(`sgz.py` 主 coef 段的 `t.get("targetSel")`
判斷式, `apply_effects()` 內 `e.get("targetSel")` 判斷式, `fire_extra_hits()` 內
`eh.get("targetSel")` 判斷式)——單位的**普通攻擊**(每回合固定執行, 走
`pick_target_chaos()` → `pick_target()`)完全不查詢任何戰法的 `targetSel`/`lockTarget`
欄位, 只會隨機選敵(唯一例外是 `taunt_by` 嘲諷鎖定)。任何原文要求「普通攻擊指定/鎖定
XX目標」(而非「戰法造成傷害指定目標」)的被動/自帶效果, 現有原語都無法讓普攻本身改變
目標選擇邏輯, 只能誠實揭露「普攻仍隨機選敵, 未鎖定至指定目標」。

**修法方向(供未來引擎端擴充參考, 本批不動引擎)**: 在 `pick_target_chaos()`/
`pick_target()` 呼叫處新增「若攻擊者身上有 passive 宣告 `normalAttackTargetSel`
之類的欄位, 優先依準則選標, 否則退回隨機」的分支, 需要在 `Unit.__init__` 預先掃描
自身 passive 戰法收集此類欄位(仿照現有 `on_hit_tacs` 預篩機制)。

## 14. 效果級 `e.when` 不支援 `hpBelow`/`hpAbove`(只有戰法級 `t.when` 支援)

**發現於**: 批21 重新核對義心昭烈(批17已有 `_todo` 記載相關取捨, 本批進一步查證根因)。

`when.hpBelow`/`when.hpAbove` 的實際判斷函式是 `hp_ok(t, u)`(`sgz.py` 677行), 只在
「母戰法帶 `t["when"]`」的那條回合迴圈掃描路徑(1140-1152行)才會被呼叫。效果級 `e.when`
走的是另一條獨立路徑(「母戰法無 `t.when`」時的通用掃描, 1180-1196行, 見6.4節), 但那條
路徑呼叫的是 `round_ok({"when": e["when"]}, rnd)`, 而 `round_ok()`(657-674行)完全不
檢查 `hpBelow`/`hpAbove` 這兩個鍵——對一個只含 `hpBelow` 的 `when` 物件, `round_ok()`
會直接回傳 `True`(視同無條件通過)。也就是說: 若在 `e.when` 裡塞 `hpBelow`, 該效果會在
**準備階段/第1回合**就無條件套用, 與「等到兵力低於門檻才觸發」的原意完全相反、且比
「誠實揭露未建模」更危險(同8節「choices 在反應式路徑形同虛設」的同類陷阱——資料層
看起來已表達了條件, 引擎實際上完全沒有檢查該條件)。

**後果**: `hpBelow`/`hpAbove` 目前只能用在「戰法級 `t.when`」(且該戰法本身沒有其他
需要不同時間窗的效果, 見6.4節的取捨), 效果級用法目前完全不可用, 資料維護者不應嘗試
用 `e.when.hpBelow` 繞過6.4節提到的 `t.when` 連坐限制——這條路走不通(不是尚未測試,
是已驗證會產生錯誤行為)。

**修法方向(供未來引擎端擴充參考, 本批不動引擎)**: 在 `round_ok()` 內比照 `hp_ok()`
補上 `hpBelow`/`hpAbove` 判斷分支(需要傳入 `u` 參數, 目前 `round_ok(t, r)` 簽名沒有
單位參數, 呼叫端 `round_ok({"when": e["when"]}, rnd)` 也需要一併改成傳入 `u`)。

## 15. `on_hit()` 反應式觸發只認「受擊者自己」, 無法表達「隊友受擊時, 我方觸發」

**發現於**: 批21, 騎虎難下「當除自己之外的友軍受到普通攻擊時，有20%→35%機率...」。

`on_hit(dst, src, is_normal)`(`sgz.py` 1103行) 是「單位 `dst` 被 `src` 攻擊時」的事件
鉤子, 只會掃描 **`dst`(受擊者自己)** 身上帶 `when.on` 的戰法(`dst.on_hit_tacs`)。
它沒有「廣播給隊友」的機制——若某戰法要表達「隊友(非自己)受到攻擊時, 自己觸發反應」
這種第三方旁觀語意, 現有 `on_hit()` 完全無法掛載(掛在被打的人身上不對, 那個人不是
效果的施放者; 掛在效果施放者自己身上也不對, 因為施放者沒有被打, `on_hit()` 根本不會
呼叫到他)。目前只能改用「常駐 coef 擲骰」近似(如騎虎難下改用前, 該戰法被當成
「自己每回合對敵軍發動攻擊」, 與原文『友軍受擊時』的觸發者/時機完全對不上), 或至少
改用 `when.on:"attacked"` 骨架(雖然仍不會被引擎正確觸發於「隊友受擊」情境, 但至少
資料語意上更接近, 且為未來引擎擴充預留掛載點)。

**修法方向(供未來引擎端擴充參考, 本批不動引擎)**: 需要在 `hit()` 結算處, 除了呼叫
被攻擊者 `dst` 的 `on_hit` 外, 額外掃描 `dst` 隊友身上是否有
`when.on:"allyAttacked"` 一類的新事件類型並觸發, 屬於新事件廣播機制, 非資料層可解。

## 16. `mitig`/`amp` 無 `dmgType`(兵刃/謀略)過濾欄位

**發現於**: 批21, 暫避其鋒「我軍智力最高的武將受到的兵刃傷害降低X%」+「我軍武力最高的
武將受到的謀略傷害降低X%」(兩個不同目標各自只减免對應的一種傷害類型)。

`mitig`(減傷)/`amp`(增減傷) 套用時是無差別減免/增加該單位「受到的所有傷害」
(`sgz.py` 552行 `mit = dst.addbonus("mitig") * ...`, 不論來源 `hit()` 呼叫時的
`kind` 是 `phys` 或 `intel`), 沒有欄位可以宣告「只對兵刃傷害生效」或「只對謀略傷害
生效」。原文若明確要求「只減兵刃傷害」或「只減謀略傷害」這種定向减傷, 現有原語只能
近似成「不分類型的全類型減傷」(較原文寬鬆, 覆蓋了不該覆蓋的傷害類型)。

**修法方向(供未來引擎端擴充參考, 本批不動引擎)**: 在 `mitig`/`amp` 效果物件新增
`dmgType: "phys"|"intel"` 選填欄位, `damage()`/`hit()` 結算減傷時依 `kind` 比對過濾,
無此欄位則維持現行「全類型生效」行為(向後相容)。

## 17. `targetSel` 無 `maxTroop`(兵力最高)準則, 只有 `minTroop`

**發現於**: 批21, 定謀貴決「使敵軍兵力最高的武將嘲諷我軍全體」。

`TARGETSEL_KEY`(`sgz.py` 710-714行)目前支援 `minTroop`/`maxForce`/`minIntel`/
`maxIntel`/`minCommand`/`mostDamaged` 六種準則, 其中 `mostDamaged` 的鍵函式是
`lambda u: u.troop` 且被歸入 `TARGETSEL_MIN` 集合(故 `pick_by_criterion` 對它用
`min()`), 語意等同 `minTroop`(兵力最低=受損最重)——**没有任何準則對應「兵力最高」
(`max(troop)`)**。本批曾誤以為 `mostDamaged` 可以表達「兵力最高」(望文生義誤解,
「損傷最多」被誤讀成「兵力數值本身」的排序方向), 實際套用後方向恰好相反(會精確選中
兵力最低而非最高的敵方單位), 已在核對階段發現並撤回, 改為 `_note` 誠實揭露維持現況
近似(定謀貴決 amp 效果不掛 targetSel, 依舊走預設隨機/群體選標)。

**同類受影響戰法**: 任何原文明確要求「兵力最高的敵軍/我軍」作為選標準則(而非「兵力
最低/最殘」)的戰法, 目前都無法用 targetSel 精確表達, 應誠實揭露維持近似, 不要嘗試
借用 `mostDamaged` 或其他現有準則替代(方向必定錯誤)。

**修法方向(供未來引擎端擴充參考, 本批不動引擎)**: 在 `TARGETSEL_KEY` 新增
`"maxTroop": lambda u: u.troop`(不加入 `TARGETSEL_MIN`, 使 `pick_by_criterion` 對它
用 `max()`), 同時建議把 `mostDamaged` 從語意混淆的別名改成明確只保留 `minTroop`
一種寫法(或至少在文件/命名上更清楚區分, 避免未來又發生同樣的方向誤判)。

## 18a. `choices`(擇一分支) 對 `command`/`passive` 型戰法完全不生效(僅 `active` 型可用)

**發現於**: 批24, 核對桃園結義(治療我軍單體/謀略傷害智力最低敵單體/兵刃傷害統率最低敵單體
三選一, `type:"command"`)能否用 `choices` 精確表達三選一時發現。

`choices` 的派發判定(`fight()` 主迴圈, `engine.js` 1093/1099行, `sgz.py` 對應段落)對
`command`/`passive` 型戰法確實會擲骰決定 `fire` 並呼叫 `pickChoice(t0.choices)` 解析出
本次觸發實際使用的分支 `t`——**但緊接著套用 `applyEffects(...)` 的呼叫(`engine.js` 1127行)
只在 `if (t.type === "active")` 條件下才執行**，`command`/`passive` 型別完全跳過這一步、
不會把已經抽出的分支效果套用到任何人身上。而 `command`/`passive` 型戰法真正的效果落地
管道是 `applyPassives()`(準備階段 `prep`/每回合 `healOnly`), 它呼叫 `applyEffects(u, null,
t, ...)` 時傳入的是**戰法原始物件 `t0`**(從未經過 `pickChoice` 解析), 並直接迭代
`t.effects`(戰法頂層, 未涉及 `choices` 陣列)——也就是說`command`/`passive` 型戰法的
`choices` 陣列**在任何路徑下都不會被讀取執行**。這與第8節的 `when.on` 反應式路徑缺口是
另一個獨立的缺口（`when.on` 路徑的問題是 `on_hit()` 完全不讀 `choices`；這裡是主迴圈確實讀了
`choices` 並抽出分支，卻只把抽出結果喂給 `active` 型戰法的落地呼叫，`command`/`passive` 型
戰法的抽選結果被憑空丟棄）。

**後果**: 若在 `command`/`passive` 型戰法上寫入 `choices`(且該戰法無 `when.on`), 該戰法會
正常按 `rate` 擲骰"發動"(`TRACE` 甚至會印出發動訊息與抽中分支), 但抽中分支的 `effects`
完全不會套用到任何單位身上——比「完全不寫 `choices`, 誠實揭露未建模」更危險(看似已表達
擇一機制, 實際上該次觸發是空轉)。

**現況**: 全庫掃描確認目前**沒有** `command`/`passive` 型戰法帶 `choices`(批24排查, 見批24
報告), 此缺口目前是「潛在地雷」而非「已踩雷的資料錯誤」, 但因此類戰法(如桃園結義的三選一)
只能繼續維持「固定選其中一個分支(本例為治療)+誠實揭露另兩分支未建模」的既有近似, 不可
嘗試用 `choices` 繞過。

**修法方向(供未來引擎端擴充參考, 本批不動引擎)**: 在 `applyPassives()` 呼叫
`applyEffects()` 之前, 仿主迴圈既有邏輯先做 `const useT = t.choices ? Object.assign({},
t, pickChoice(t.choices)) : t;`, 再用 `useT` 呼叫 `applyEffects`, 讓 `command`/`passive`
型戰法的 `choices` 分支能在 prep/healOnly 路徑正確解析套用。

## 18. `heal` 絕對量公式疑似系統性高估(~1.6~2倍) —— 待校準, 本批不動公式

**發現於**: 批24, user 遊戲截圖實測(`docs/data/calibration_anchors.json` →
`heal_firstaid_20260704`, 陷陣營急救效果)。

**觀測**: 夏侯惇(智力379.02, 觸發前兵力約8331)受到急救觸發(治療率60%, `scale:"intel"`),
實測回復量 **546**(user 確認未觸傷兵池上限, 即公式值本身就是546, 非被封頂後的殘值)。

**引擎現行公式重算**(`applyEffects()` heal 分支: `want = hcoef * caster.troop * 0.10 *
boostMult`, `hcoef = coef * SCALE(intel)`, `SCALE(v) = max(0, 1+(v-100)/350)`):
`SCALE(379.02) ≈ 1.797`, `hcoef ≈ 0.6 × 1.797 ≈ 1.078`, `want ≈ 1.078 × 8331 × 0.10
≈ 898`。

**偏差**: 引擎公式值(≈898~1080, 依採用的即時兵力估計略有浮動)相對實測值(546)高估約
**1.6~2倍**，超出 coordinator 訂定的 ±30% 校準容忍帶。

**已排除的干擾因素**: 同一批次的另一實測樣本(智力385, 普攻傷害189, 恢復170)precisely
驗證 `189 × 0.90(第1~3回合傷兵池轉化率) = 170.1 ≈ 170`——傷兵池封頂邏輯(`WOUNDED_RATES`)
本身精確無誤, 偏差不是傷兵池機制造成，而是 heal 絕對量公式(`troop × 0.10 × hcoef` 這條
公式本身的量級)可能系統性偏高。

**本批決策(遵 coordinator 指示)**: 不擅自調整 heal 公式常數(`0.10` 這個絕對量基準或
`SCALE` 曲線), 待統籌者裁決(可能需要更多實測樣本才能反解出精確常數, 目前僅2組樣本、
且只有1組是"未封頂"的乾淨樣本, 樣本量不足以直接反解新常數)。此節僅記錄偏差供後續校準
批次參考, `heal` 相關的 `_note`/`_todo` 揭露維持現狀不變(治療率/scale 語意本身建模正確,
只有最終絕對量可能需要整體乘上一個待定的校正係數)。

---

## 附錄: 全部引擎支援的效果原語(k)一覽

供對照哪些機制已有原語、哪些仍是缺口。見 `sgz.py` `apply_effects()` 的 k 分支
(792-1051行)與 `lint_tactics.py` 的 `PER_KIND_FIELDS`。

`heal` / `settle` / `redirect` / `amp` / `mitig` / `stun` / `silence` / `disarm` /
`chaos` / `ambush` / `insight` / `immune` / `first` / `stat` / `dot` / `extra` /
`stack` / `decay` / `swap` / `pierce` / `counter` / `taunt` / `shield` / `dodge` /
`surehit` / `healblock` / `lifesteal` / `rateup` / `chargeup` / `healBoost` /
`healGiven` / `fakeReport` / `dispel` / `block`(批22新增, 次數型格擋, 見第11節)

戰法/效果級輔助欄位: `when`(rounds/from/until/parity/every/on/hpBelow/hpAbove) /
`targetSel` / `ifTargetHas` / `scale` / `choices` / `extraHits` / `everyN` /
`hitsRepeat` / `lockTarget` / `undispellable` / `nativeOnly` / `inheritedOnly` /
`prepOnly` / `e.n`・`e.nMax`(批23新增, 見下方說明) / `e.rate`(批23新增, 見下方說明)

**批23新增: 效果級 `e.n`/`e.nMax`(A1) 與 `e.rate`(A4)** —— 過去非CTRL效果(`amp`/`mitig`/
`stat`/`dot`/`healblock`/`rateup`/`insight`/`extra`/`lifesteal`/`healBoost`/`healGiven`/
`pierce`/`surehit`/`dodge`/`shield`/`swap`/`fakeReport`/`immune` 等)的 `who:"enemy"`/
`who:"ally"` 無條件套用到全體敵軍/我軍, 大量原文寫「單體」「N人」的非控制效果被系統性
高估成全體(CTRL_K 家族 stun/silence/disarm/taunt/chaos 不受影響, 它們一直讀戰法頂層
`t.n`/`t.nMax`)。現在效果自帶 `e.n`(可配 `e.nMax`)時, 比照 CTRL_K 群體選標邏輯隨機挑
`e.n`(~`e.nMax`)個不重複目標(單體時優先鎖定當下 `tgt`); 無 `e.n` 時完全維持原行為(全體),
向後相容。`lint_tactics.py` R13 掃描全庫比對原文目標數描述與 `e.n` 是否一致。

同時, 效果級 `e.rate` 現在於 `apply_effects()` 通用套用(不限 `onHit`/`delayedEq` 兩條
路徑), 套用前擲骰判定, 見上方第4節。
