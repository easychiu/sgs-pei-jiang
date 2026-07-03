# tactics.json 源頭資料問題清單(給Gemini)

來源: 批10-15 全庫驗證/重推導過程發現。修正建議都只針對 data/tactics.json 源頭。

1. 五雷轟頂 effectText 內含兩個版本文案('2020-11-04更新'+'原始版本')，機率/係數不一致(震懾機率30% vs 50%)，建議root data僅保留最新版文案，避免解析時誤取舊版數值。
A.30%，需要自身是主將(首位或是淺龍陣的二三號位)，這時敵方有狀態'水攻'或'沙暴' 每多一種多20%機率
[RESOLVED via user實測(db_issues item1 回覆) — 批19整合] 基礎震懾機率採信30%(非raw另一版本的50%)。已寫入docs/data/tactics_overrides.json「五雷轟頂」與docs/data/tactic_corrections.json同名correction(_note已更新反映本次結論)；「自身為主將」+「目標水攻/沙暴狀態每多一種+20%機率」的條件式rate調整仍受限於引擎缺乏「隊伍主將身份判定」與「動態調整rate擲骰本身」原語，維持近似必定觸發(已知揭露，未變動)。

2. 刀出如霆 raw data/tactics.json 的 effectText 與 overrides.json 差異巨大(raw僅一句籠統描述'獲得倒戈與掠陣狀態，疊加後可大幅提升敵軍受傷'，缺乏具體數值)，應以overrides為準並考慮回寫更新raw effectText。
A. overrides.json 是對的 180%傷害跟掠陣狀態 隨機打三下 有可能不同敵方 30%倒戈, 並對低方施加掠陣，掠陣觸發兩次 移除敵方掠陣狀態使其受到兵刃傷害增傷30%

3. 槍舞如風 raw effectText 完全未提及'掠陣'或'武力+40'機制，但parsed資料的_real欄位卻描述了掠陣疊加機制，疑似源自外部連結(ali213.net)資料與官方文案不一致，建議查證來源可信度。
A.  raw effectText 不可全信，使我軍群體2~3人 獲得2次抵禦，並且使自身本回合普攻後對目標造成兵刃傷害240%及掠陣狀態，掠陣累積兩次移除狀態並增加自身40武力可疊加
[RESOLVED via user實測(db_issues item3回覆, 2025-10-22調整版本) — 批19整合] 採信user答案，取代舊版把240%誤植為頂層coef主動觸發段的語意錯誤(實際為「普攻後」觸發，非戰法自身35%機率主動打一次)。已改用extraHits(普攻後兵刃240%)+stat(self,force+40)近似掠陣機制，寫入docs/data/tactics_overrides.json/tactic_corrections.json「槍舞如風」。掠陣「疊2層移除轉換為self增益」的計次器機制引擎無對應原語，以「每次都視為已集滿」的靜態近似替代（略高估），已在corrections的_note/_todo中揭露。
 
4. 搦戰群雄 raw effectText 與 parsed 的 _real 欄位(標示_conf:high)矛盾：raw僅描述減傷20%→40%，_real卻聲稱另有'自身兵刃增傷25%'，且與raw的'20%→40%'數字都對不上(_real用25%)，建議查核來源https://m.ali213.net/wiki/sgzzlb/zf64.html 是否為舊版數據。
[RESOLVED via grok第二輪查證(grok_errata2.md) — 批19整合] 三方來源(巴哈姆特戰法一覽/陸服sgz.ejoy.com官方攻略站/台版官方戰略家營地頁)交叉確認滿級同時有「自身兵刃增傷25%」+「自身兵刃減傷25%(受武力影響)」，取代raw舊有的「僅減傷20%→40%」(查無來源支持，判定為錯誤數字)。已寫入docs/data/tactics_overrides.json/tactic_corrections.json「搦戰群雄」(新增amp self+0.25增傷段，mitig改為0.25)。
5. 威謀靡亢/挫志怒襲 兩戰法均為'先致虛弱狀態(本身無傷害)，若目標已虛弱則產生實際傷害'的相同套路，但raw effectText未清楚說明'虛弱'與後續傷害是否同回合生效或需等到下一次施放才判定'已虛弱'，語意存在歧義，建議向原始資料源二次確認觸發時序。
[RESOLVED via agy查證(agy_errata3.json, high confidence) — 批19整合] 觸發時序為「施放當下」的結算判定：若目標在判定當下已處於虛弱狀態(來自上一次施放殘留，或隊友如劉備/挫志怒襲提前施加)則觸發叛逃傷害，否則僅重新施加虛弱；非同一次施放內部同時判定兩者。已用批16 ifTargetHas:"disarm"(disarm借用作虛弱的可辨識標記)精確建模此條件式時序，取代舊版EV機率折算，並修正了effects陣列順序bug(dot段須排在disarm段之前，否則會讀到本次剛套用的標記而非上次殘留，經trace_batch19實測驗證)。見docs/data/tactic_corrections.json「威謀靡亢」。挫志怒襲本次未一併處理(不在本批11筆範圍內)。
6. 十面埋伏 raw effectText 內含'2024-06-25更新'與'原始版本'兩段文案，兩段對禁療叛逃/謀略傷害的先後順序描述不同，數值本身一致但敘事順序反了，可能造成解析時序理解偏差。
7. 水淹七軍 raw effectText 描述了'第二次及之後施放'、'第三次及之後施放'、'第四次施放後'的多階段疊加機制(共4個階段)，現行parsed完全無法表達這種'施放次數計數器'語意，屬引擎能力邊界問題，非單純解析錯誤，建議未來評估是否需要新增stack-based原語。
8. 「鳩毒」與「鴆毒」為疑似重複條目(同名異字，nameZh分別為'鳩毒'/'鴆毒')，raw中「鳩毒」effectText僅有更新註記(待補充)且需靠overrides才能還原完整效果，「鴆毒」則有完整effectText；兩者activationRate/coef完全相同(0.7/2.26)，高度疑似同一戰法的重複資料，建議root data比對後合併或刪除其一。
[RESOLVED via agy查證(agy_errata3.json, high confidence) — 批19整合] 確認「鳩毒」為「鴆毒」的字形相似誤寫重複條目(同一戰法，李儒自帶主動)，非獨立戰法。已在docs/data/tactics_overrides.json將「鳩毒」強制設type:"none"(排除戰鬥與選單)，並同步清空docs/data/tactic_corrections.json「鳩毒」的effects/coef(避免apply_corrections()在type:none閘門之後仍無條件覆寫回戰鬥效果)。唯一有效版本為「鴆毒」：已修正武力降低30%(原批12誤植為60%)，並將「1回合後毒發」從失效的settle(參數配置在8回合戰內恆不觸發，且結算目標邏輯錯誤打統率最高敵將而非中毒目標本身)改用dot(dur:1)精確結算於正確目標，root data無需再變動(工程層已完全消化此重複問題)。
9. 「橫掃」raw effectText與effectTarget均未标明目標人數為單體/群體/全體，僅由_parsed的n=1/quality=B間接推測，難以確認finding所稱『n應為3』是否指目標人數或攻擊次數，建議root data補充effectTarget欄位以消歧義。
[未解決 — 批19查證嘗試失敗] grok第一輪與第二輪查證(grok_errata.md/grok_errata2.md)皆對「橫掃」的查詢在WebFetch階段直接拋出tool_output_error，未取得任何實際回覆內容，無可用查證文本。維持現行corrections(n=3)不變，未杜撰數值。待下一輪查證重試。
10. 「絕計折謀」raw effectText僅「對敵方隨機武將造成高額謀略傷害」無具體coef數值，需外部資料查證滿級傷害率才能填入coef，目前coef=0與_est標記合理但長期仍待補值。
[未解決 — 批19查證嘗試失敗] grok第一輪查證(grok_errata.md)對「絕計折謀」的查詢在WebFetch階段直接拋出tool_output_error，未取得任何實際回覆內容(僅留下查詢語句本身)，無可用查證文本；第二輪查證未再次嘗試此戰法。維持現行corrections(coef=1.5估值+_est=true)不變，未杜撰數值。待下一輪查證重試。
11. 「移花接木」raw effectText完全無數值(無百分比)，「將部分治療效果轉移給自身」的轉移比例無從得知，需要root data或wiki補充。
[RESOLVED via agy查證(agy_errata3.json, high confidence) — 批19整合] 補上完整數值：使敵我全體受到治療提升18%(受自身最高屬性影響)，並將敵軍全體受到治療的26%(受自身最高屬性影響)轉移到自身，持續1回合，發動機率50%。已寫入docs/data/tactics_overrides.json/tactic_corrections.json「移花接木」；18%治療提升段已用healBoost(who:enemy/ally各一條)精確建模，26%跨陣營治療竊取仍無transfer/steal原語，以healGiven(self,+0.26)近似替代並明確揭露(非精確建模，方向近似但機制不同)。
12. 「累世立名」raw effectText本體標記「待補充」，完整效果需依賴overrides；overrides本身效果文字（一次性兵刃傷害+灼燒DoT+主將時全隊統率增益）與現行_real註記存在出入(_real稱mult近似非固定加點)，建議以overrides為準重新整批解析而非依賴_real的舊估算。
[未解決 — 批19查證嘗試失敗] grok第一輪與第二輪查證(grok_errata.md/grok_errata2.md)皆對「累世立名」的查詢在WebFetch階段直接拋出tool_output_error，未取得任何實際回覆內容，無可用查證文本。維持現行overrides/corrections(n=3, 一次性兵刃傷害+灼燒DoT+主將統率增益)不變，未杜撰數值。待下一輪查證重試。
13. 垂心萬物(command): effects同時用頂層coef/rate與effects裡的extra(who=ally,val=0.35,dur=99)表達同一個『奇數回合35-70%機率額外攻擊』機制,造成雙重觸發路徑,且heal(治療)理應只在攻擊未發動時的else分支才觸發,現行dur=1的heal沒有條件限制,需要Gemini重新設計這條戰法的effects結構(建議:頂層coef/rate負責攻擊本體,刪除extra,heal加when.on或機率互斥處理)。
14. 江天長焰(command): stack原語在engine.js中恆定套用於施放者自身的amp()(this.stack.per*this.stack.n 加進 caster 自己的攻擊加成),與effect.who欄位無關;現行資料把stack的who設為enemy,實際執行效果是誤把疊加加成套到敵方自己的攻擊力上(方向相反於原文『使敵軍受到的傷害提高』),屬於原語與語意不匹配的系統性風險,建議排查所有stack who=enemy的戰法是否有同樣問題,並考慮讓stack原語支援作用在『對方受到傷害』方向(需engine.js改動)或改用其他原語表達。
15. 承天靖世(command): 原文『無視防禦』對應的pierce效果目前完全缺漏,且『受統帥影響』的傷害縮放無法用現有kind:phys/intel二選一的schema表達,需另外評估是否新增kind:command或改用effect-level scale覆寫damage()的atk/def選取邏輯。
16. 用武通神(command): 原文是4個不同回合(2/4/6/8)各自不同傷害率的遞增攻擊,現行單一coef+單一n的schema無法精確表達逐回合遞增值,只能用平均值近似,精度有損,若want更精確表達需要engine支援『per-when多組coef』的新機制。
17. data/tactics.json 對『鷹視狼顧』存在兩筆不同id的條目(INNATE cmnzy57zt 與 INHERITANCE cmnzuqc7y),而 tactics_parsed.json 另外多出一條獨立的『鷹視』(_est=true,無對應raw),疑似是INHERITANCE條目在早期抽取時名稱被截斷/複製成新條目,建議協作者於root data層核實兩者關係(是否應合併或刪除'鷹視'這個多餘名稱)。
18. 藏器待時: 現行effects[1]用amp val=0.2表達「造成傷害時有15%→30%機率附帶10%→20%破陣」,語意應是對敵方施加pierce debuff(有機率觸發)而非自身泛用增傷amp,此為既有(非本次finding提交)的可疑誤判,建議後續批次覆核。
19. 義膽雄心: raw effectText中「自身為主將時,降低屬性效果受自身對應屬性影響」暗示stat debuff應加scale欄位,目前修正未處理scale部分,留待後續補強。
20. 奮突/一身是膽/乘勝長驅: 三者原文皆為「每次觸發/每回合疊加」型態,但引擎stack原語語意是「每回合固定+1層」,與「每次普攻觸發/每次免疫控制後機率觸發」的疊加條件不完全吻合,可能造成疊層速度與原文機率不符,建議評估是否需要新增per-event疊層原語。
21. 先登死士 raw effectText 含兩段版本(2024更新版與原始版本)以 ' /  / ' 分隔,更新版多了'若麴義統領則可疊加5次'的額外條件,parsed未區分取用哪一版本,建議統一取最新更新版本內容再抽取。
22. 丹陽兵/象兵/解煩衛/潛龍陣 等多筆raw effectText同樣含新舊兩版本併存(以'2024→XX→XX 更新'與'原始版本'分段),數值不同(如丹陽兵謀略抵擋18%→36% vs 陶謙統領20%→40%),抽取腳本應明確只取最新版本區塊,避免版本混淆導致取值錯誤。
23. 飛熊軍 effectText 完全無任何百分比/點數數值,屬於原始資料本身描述不完整(可能被截斷或原始文案本就模糊),非parsed抽取錯誤,建議請Gemini協作查證app內是否有更完整數值文案。
[RESOLVED via agy查證(agy_errata3.json, high confidence) — 批19整合] 補上完整數值：我軍全體受到的治療效果提升16%(董卓統領時20%)；我軍主將每回合行動時60%機率對敵軍群體(2人)造成謀略傷害(傷害率64%，受我軍全體累計造成治療量影響，觸發後重置累計進度)。已寫入docs/data/tactics_overrides.json/tactic_corrections.json「飛熊軍」；治療提升段用healBoost(ally,+0.16)精確建模(董卓統領條件分支因無「持有統領武將」判定原語，保守取基礎16%)；主將60%機率2人群攻用command型coef=0.64/rate=0.6/n=2近似(「受累計治療量影響+觸發後重置」的計數器機制無對應原語，維持_todo)。
24. 奇計良謀(全庫掃描時發現,非212筆findings原始範圍): effects陣列內有兩個完全相同的amp(who=enemy,val=-0.32,dur=3,scale=speed)效果重複,疑似抽取時誤將「武力最高」與「智力最高」兩個目標子句解析成同一個效果兩次,應拆分成分別作用於不同目標選取邏輯(現有engine無「屬性最高單體」目標選取原語,重複儲存至少應去重為一筆,不建議這次批10動,留給後續批次)
25. 雁行陣 raw effectText(「轉移傷害降低/轉移負面狀態/偷取統率」三選一)完全無機率公式與偷取數值，批12/批17皆因引擎無transfer(把A身上效果/狀態移到B身上)原語而維持effects=[]空白。
[RESOLVED(部分) via agy查證(agy_errata3.json, high confidence) — 批19整合] 補上機率公式：每回合35%機率(受統率影響，公式(統率+570)/570)，三種效果為「各自獨立判定」(非三選一，同回合可能0/1/2/3個同時觸發，choices的擇一分支語意不適用)；偷取統率10點(公式(統率+233)/233)。已寫入docs/data/tactics_overrides.json/tactic_corrections.json「雁行陣」的_note/_todo供未來若新增transfer原語時直接落地。三種效果本體(轉移傷害降低/轉移負面狀態/偷取統率)仍因引擎缺乏transfer原語而維持effects=[]，此為engine.js/sgz.py層級的擴充需求，非corrections層可解，故標記「部分解決」(數值/機率公式已查明並記錄，機制本身仍待未來新增原語)。
