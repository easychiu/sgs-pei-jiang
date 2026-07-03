# tactics.json 源頭資料問題清單(給Gemini)

來源: 批10-15 全庫驗證/重推導過程發現。修正建議都只針對 data/tactics.json 源頭。

1. 五雷轟頂 effectText 內含兩個版本文案('2020-11-04更新'+'原始版本')，機率/係數不一致(震懾機率30% vs 50%)，建議root data僅保留最新版文案，避免解析時誤取舊版數值。
2. 刀出如霆 raw data/tactics.json 的 effectText 與 overrides.json 差異巨大(raw僅一句籠統描述'獲得倒戈與掠陣狀態，疊加後可大幅提升敵軍受傷'，缺乏具體數值)，應以overrides為準並考慮回寫更新raw effectText。
3. 槍舞如風 raw effectText 完全未提及'掠陣'或'武力+40'機制，但parsed資料的_real欄位卻描述了掠陣疊加機制，疑似源自外部連結(ali213.net)資料與官方文案不一致，建議查證來源可信度。
4. 搦戰群雄 raw effectText 與 parsed 的 _real 欄位(標示_conf:high)矛盾：raw僅描述減傷20%→40%，_real卻聲稱另有'自身兵刃增傷25%'，且與raw的'20%→40%'數字都對不上(_real用25%)，建議查核來源https://m.ali213.net/wiki/sgzzlb/zf64.html 是否為舊版數據。
5. 威謀靡亢/挫志怒襲 兩戰法均為'先致虛弱狀態(本身無傷害)，若目標已虛弱則產生實際傷害'的相同套路，但raw effectText未清楚說明'虛弱'與後續傷害是否同回合生效或需等到下一次施放才判定'已虛弱'，語意存在歧義，建議向原始資料源二次確認觸發時序。
6. 十面埋伏 raw effectText 內含'2024-06-25更新'與'原始版本'兩段文案，兩段對禁療叛逃/謀略傷害的先後順序描述不同，數值本身一致但敘事順序反了，可能造成解析時序理解偏差。
7. 水淹七軍 raw effectText 描述了'第二次及之後施放'、'第三次及之後施放'、'第四次施放後'的多階段疊加機制(共4個階段)，現行parsed完全無法表達這種'施放次數計數器'語意，屬引擎能力邊界問題，非單純解析錯誤，建議未來評估是否需要新增stack-based原語。
8. 「鳩毒」與「鴆毒」為疑似重複條目(同名異字，nameZh分別為'鳩毒'/'鴆毒')，raw中「鳩毒」effectText僅有更新註記(待補充)且需靠overrides才能還原完整效果，「鴆毒」則有完整effectText；兩者activationRate/coef完全相同(0.7/2.26)，高度疑似同一戰法的重複資料，建議root data比對後合併或刪除其一。
9. 「橫掃」raw effectText與effectTarget均未标明目標人數為單體/群體/全體，僅由_parsed的n=1/quality=B間接推測，難以確認finding所稱『n應為3』是否指目標人數或攻擊次數，建議root data補充effectTarget欄位以消歧義。
10. 「絕計折謀」raw effectText僅「對敵方隨機武將造成高額謀略傷害」無具體coef數值，需外部資料查證滿級傷害率才能填入coef，目前coef=0與_est標記合理但長期仍待補值。
11. 「移花接木」raw effectText完全無數值(無百分比)，「將部分治療效果轉移給自身」的轉移比例無從得知，需要root data或wiki補充。
12. 「累世立名」raw effectText本體標記「待補充」，完整效果需依賴overrides；overrides本身效果文字（一次性兵刃傷害+灼燒DoT+主將時全隊統率增益）與現行_real註記存在出入(_real稱mult近似非固定加點)，建議以overrides為準重新整批解析而非依賴_real的舊估算。
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
24. 奇計良謀(全庫掃描時發現,非212筆findings原始範圍): effects陣列內有兩個完全相同的amp(who=enemy,val=-0.32,dur=3,scale=speed)效果重複,疑似抽取時誤將「武力最高」與「智力最高」兩個目標子句解析成同一個效果兩次,應拆分成分別作用於不同目標選取邏輯(現有engine無「屬性最高單體」目標選取原語,重複儲存至少應去重為一筆,不建議這次批10動,留給後續批次)
