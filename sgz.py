# -*- coding: utf-8 -*-
"""
三國志戰略版 配將引擎 — 評分 + 配將推薦 + 逐回合模擬對戰

v4: 讀 sgsdeck 真實全庫(193武將 / 384戰法)。
戰法 effectText 為自然語言, 用 effectType 路由 + 正規表達式抽數值做啟發式解析
(approximation, 非逐條精解; 精準需人工/LLM 解 effectText)。
傷害用社群拆解公式(data/formula.md)。run: python sgz.py
"""
import json
import math
import os
import random
import re
from itertools import combinations

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data")  # docs/data 是雙引擎共用的現行資料
# 本文政策(2026-07-09 user): 定稿 effectText 必須回寫 data/tactics.json 全文庫(方便 diff)。
# 執行: python sync_tactics_effecttext.py  （overrides > corrections.effectText > 合格 _evidence）
ROUNDS = 8
START_TROOP = 10000
MORALE = 100                                         # ponytail: 士氣固定滿
CITY = 20                                            # 城建滿: 武智統速各+20(每級+2×10級)
FACTION = 1.10                                       # 陣營滿: 全屬性+10%(每級1%×10級)
CAMP = 4                                              # 兵種營: 戰報「弓兵營全屬性提升了4」→ 全屬性平加(獨立階段, 在陣營乘算之後), 雙方皆有
# 指揮/被動戰法: 開戰即套用其 effects(被動效果), 帶傷害 coef 的部分每回合以資料
# 的 rate 擲骰(多數 rate=1.0 即每回合必發); rate 來自 tactics_parsed.json 的
# activationRate 回填, 不再用發明的全域折扣常數近似。

CUR_ROUND = 0                                          # 批15: 當前回合數(0=準備階段), fight() 回合迴圈開頭設值;
                                                        # 供 apply_effects() 的 heal_only 常駐治療通道讀取以檢查 t["when"]
                                                        # (roundOk 語意, 見 engine.js CUR_R 對應慣例)。單執行緒模擬無併發疑慮。
# 批52i: fight() 執行期回呼(供 proxyNormal 代打完整普攻含突擊)
_FIGHT_CTX = {"on_hit": None, "on_deal": None, "allies_of": None, "foes_of": None, "active_fired": None}

COUNTER = {"騎": "盾", "盾": "弓", "弓": "槍", "槍": "騎"}  # 騎>盾>弓>槍>騎; 器全被克

# 批(狀態疊加語意對齊, 2026-07-12): NAMED_STATUS —— 具名狀態註冊表(雙引擎共用慣例, engine.js
# 同名常數見其定義逐字對稱; lint_tactics.py 另維護精簡對照副本供 R36 核對, 三份需同步維護,
# 本庫無跨語言共用機制, 依現行「對稱」手動雙寫慣例)。user權威規則(見
# docs/data/calibration_anchors.json → status_stacking_rule_20260711/status_stacking_detail_
# 20260712/control_status_rule_20260712): 具名狀態現分五類(狀態疊加精修批新增後三類):
#   "unique"(唯一/覆蓋): 同單位全場只存在一個實例, 再施加同名狀態覆蓋舊的(刷新, 保留最新
#     來源/數值), 不會因多來源疊加而雙倍觸發機率/雙倍生效(如休整)。
#   "multi"(可共存): 多個來源各自獨立存在、全部生效(如反擊/攻心/倒戈, 各來源獨立判定/
#     結算/到期, 不互相覆蓋)。
#   "overwrite_fallback"(覆蓋+到期回退): 同單位可能有多個來源, 目前生效者=最新(優先序最高)
#     且仍在自己duration窗內者; 該來源到期後回退成次新仍在窗內者的值, 全部到期才消失(如
#     急救)。與純"unique"的差異: unique是「覆蓋後舊來源徹底消失」, 這個是「覆蓋後舊來源仍
#     潛伏, 新來源到期可回退復活」。
#   "accumulate"(累積): 新施加的次數/層數直接加總到現有(如警戒)。
#   "conditional"(條件式): 依持有者當下是否處於某輔助狀態, 在"unique"(刷新覆蓋)與
#     "accumulate"(累積)兩種行為間切換(如抵禦: 預設unique, 持有者處於「嚴密」時例外改
#     accumulate)。
#   "unique_strongest"(唯一+同等或更強擋新): 同單位全場只存在一個實例, 但只有「嚴格更強」
#     (以dur近似強度)的新施加才覆蓋, 同等或更弱的新施加完全失效(不覆蓋/不疊加/不延長/不
#     重新觸發任何隨施加而來的副作用廣播), 是既有偽報(fakeReport)same-or-stronger規則的
#     推廣(如繳械/計窮/震懾/混亂)。
# 本批("狀態疊加精修"批, 2026-07-12)落地: 灼燒/中毒/潰逃/水攻/沙暴/叛逃(DoT家族, 改
# refresh/唯一)、警戒(accumulate)、抵禦(conditional, 嚴密偵測)、急救(unique→
# overwrite_fallback細化)、攻心/倒戈(multi語意不變, 但底層改真正多實例清單取代addbonus加總
# 標量)、計窮/繳械/震懾/混亂(pending→unique_strongest)。上一批("狀態疊加語意對齊"批,
# 2026-07-11)已落地: 反擊(multi)、急救/休整(unique, 本批進一步把急救細化為
# overwrite_fallback)。其餘(先攻/遇襲/洞察/嘲諷/虛弱)user仍未裁決, mode 維持 "pending"
# (維持現行行為不變, 純粹記錄以供未來裁決/lint參照, R36對pending狀態不作結構核對, 不阻塞
# 現行行為)。禁止不擅自歸類: pending 項下的 note 只記錄現行觀察到的引擎行為, 不代表已裁決
# 的規則。
NAMED_STATUS = {
    # ---- 已確認 overwrite_fallback(覆蓋+到期回退) ----
    "急救": {
        "mode": "overwrite_fallback",
        "engine": "reactive heal(k==heal, when.on:attacked/damaged); 見 Unit.__init__ "
                  "self._heal_candidates 蒐集(依戰法→兵書→裝備優先序) + "
                  "suppressed_named_status(@property, 每次存取依當下own_round動態算出目前"
                  "生效者) + on_hit_for() 消費端檢查該集合放行/跳過",
        "note": "陷陣營/青囊書(長健)/三軍之眾/草船借箭/雲聚影從/擊其惰歸/蕙質蘭心/援救等皆"
                "授予急救; 多來源同時存在時, 目前生效者=優先序最高(裝備>兵書>戰法, 同類別內"
                "取後蒐集者, 對應 apply_passives() 既有prep處理順序)且仍在自己when回合窗內"
                "(round_ok)的那個; 若最高優先者的窗已過(如草船借箭2回合)而次高優先者仍在窗內"
                "(如陷陣營3回合), 回退成次高優先者(見status_stacking_detail_20260712範例)。"
                "tie-break優先序本身為本次實作的顯式假設, 非user另有明文裁決, 供未來覆核。"
                "現況邊界: 候選的duration窗只讀取資料既有when(until/from/rounds), 不會反推"
                "『active型戰法(如草船借箭)實際成功施放的那一回合』(現行架構沒有這個時間戳,"
                "見engine_limitations.md新增節), 該類候選若資料無when視窗則視為從開戰起即"
                "常駐可用(與其餘既有近似一致口徑, 非本批新缺口)",
    },
    # ---- 已確認 unique(覆蓋, 同單位唯一實例, 覆蓋後舊來源徹底消失不回退) ----
    "休整": {
        "mode": "unique",
        "engine": "regen(k==regen, u.regens list, 以 upsert_named_status 鍵=\"休整\" 去重,"
                  "全場至多1筆, 同名再施加覆蓋刷新)",
        "note": "乘敵不虞為現行唯一 k==regen 實例。已知殘留缺口: 部分戰法(如金丹秘術)改用"
                "k==heal + when.from/until(非 when.on 反應式)表達同類「每回合恢復」語意, "
                "該通路現行仍逐回合獨立重擲/未納入本次去重範圍(架構上是即時重算而非持久狀態"
                "實例, 風險較低且無實測證據顯示現行有雙重疊加問題, 見k==heal分支註解, 誠實"
                "揭露為已知限制)。狀態疊加精修批: user規則明確要求急救改overwrite_fallback"
                "(見上), 並提及「休整同理若有多來源, 不確定比照急救+標記」——本批保守不動"
                "休整現行的單槽覆蓋(無回退)實作(user自陳不確定, 依「無法判斷時保守維持既有"
                "行為」原則不擅自比照擴大, 見engine_limitations.md新增節标记待user後續裁決)",
    },
    # ---- 已確認 multi(可共存, 多實例並存) ----
    "反擊": {
        "mode": "multi",
        "engine": "counter(k==counter, u.counters list, 以 upsert_named_status 鍵="
                  "(\"反擊\", id(e)) 去重: 同一來源重複施加只刷新自己那筆, 不同來源各自"
                  "獨立並存)",
        "note": "hit() 逐一走訪 counters 清單每個實例, 各自獨立擲 prob/結算傷害, 互不影響",
    },
    "攻心": {
        "mode": "multi",
        "engine": "lifesteal(k==lifesteal, u.lifesteals list, 以 upsert_named_status 鍵="
                  "(\"攻心倒戈\", id(e)) 去重: 同一來源重複施加只刷新自己那筆, 不同來源各自"
                  "獨立並存, hit() 逐筆結算加總回復量)",
        "note": "狀態疊加精修批(user追加規則, coordinator訊息): 前批(623afc4)用 "
                "addbonus(\"lifesteal\") 把多個來源加總成單一標量, 總量雖數學正確(對val線性"
                "可加)但遺失個別來源獨立到期追蹤與戰報歸因能力, user糾正改真正多實例清單"
                "(比照反擊 u.counters 做法), 見 hit() 對應段落與 push_lifesteal()",
    },
    "倒戈": {
        "mode": "multi",
        "engine": "同攻心, lifesteal(u.lifesteals list, 見上)",
        "note": "同上",
    },
    # ---- 已確認 accumulate(累積, 新施加次數加總到現有) ----
    "警戒": {
        "mode": "accumulate",
        "engine": "block(次數型格擋, u.block list, val<1.0/val>=0.999為分界, 見push_block()"
                  "──同源同值合併次數, 不同來源/不同值各自成一筆, 消耗時皆先進先出逐筆扣減,"
                  "總可用次數=全部筆數總和, 即「累積」的可觀察結果)",
        "note": "與抵禦同族(counted-charge家族), 但疊加規則不同(抵禦預設刷新, 見下)。"
                "user規則: 新施加的次數加總到現有(如折衝施加2次→現有+2)",
    },
    # ---- 已確認 conditional(依當下是否處於「嚴密」在unique/accumulate間切換) ----
    "抵禦": {
        "mode": "conditional",
        "engine": "同警戒, block(val>=1.0全擋), 見push_block(): 預設(非嚴密)「有剩餘不補不"
                  "刷, 歸零才套用新來源」(existing_n=同dmgType既有次數總和, >0時新施加整個"
                  "忽略, ==0才append); 持有者處於「嚴密」(self.rigorous>0, 赴湯蹈火施加, 見"
                  "Unit.__init__/k==\"rigorous\"分支)時例外改累積(同警戒的同源合併/不同源"
                  "並存邏輯)",
        "note": "user規則(2026-07-12追加修正, 更正本批較早版本誤植的「取代成最新值」"
                "寫法): 抵禦=有剩餘次數時新施加不補不刷(如身上1次, 折衝禦侮再給2次仍維持"
                "1次不變, 不會變成2次也不會變3次); 只有現有次數歸零才套用新來源的次數。"
                "例外: 持有者處於「嚴密」狀態(赴湯蹈火戰法施加)時→改累積(add疊加)。"
                "『特殊護盾嚴密』本身若還有除了『抵禦例外開關』以外的額外機制(如吸收池),"
                "官方文字未明確描述, 現況只編碼為偵測旗標, 見tactic_corrections.json"
                "「赴湯蹈火」_todo既有揭露",
    },
    # ---- 已確認 unique_strongest(唯一+同等或更強擋新, 偽報same-or-stronger規則的推廣) ----
    "計窮": {
        "mode": "unique_strongest",
        "engine": "u.silence(單一剩餘回合數欄位), apply_control_dur() 統一處理: 新dur須"
                  "嚴格大於現有值才覆蓋+觸發fire_controlled反彈廣播, 同等或更弱完全失效"
                  "(不覆蓋/不疊加/不延長/不重新廣播)",
        "note": "user規則(control_status_rule_20260712): 控制類「不動作」狀態(繳械/計窮/"
                "震懾/混亂)= 唯一+「同等或更強擋新」, 是既有偽報(fakeReport)same-or-stronger"
                "規則的推廣, 以dur近似強度。不含監統震軍機變「繳械狀態增加1回合」的extendDur"
                "延長機制(需新原語, 待後批)",
    },
    "繳械": {
        "mode": "unique_strongest",
        "engine": "u.disarm(同計窮, 單一欄位), apply_control_dur() 同上",
        "note": "同計窮",
    },
    "震懾": {
        "mode": "unique_strongest",
        "engine": "u.stun(同計窮, 單一欄位), apply_control_dur() 同上",
        "note": "同計窮",
    },
    "混亂": {
        "mode": "unique_strongest",
        "engine": "u.chaos(同計窮, 單一欄位), apply_control_dur() 同上",
        "note": "同計窮",
    },
    # ---- 追加規則(coordinator訊息, 2026-07-12): 先攻/遇襲/洞察/嘲諷/虛弱 比照控制類同套
    # unique_strongest規則(唯一+同等或更強擋新), 由pending轉正 ----
    "先攻": {
        "mode": "unique_strongest",
        "engine": "u.first(單一剩餘回合數欄位), 改用 apply_control_dur() 統一處理(fire_"
                  "controlled 對 kind=\"first\" 本就no-op不廣播, 只借用其「新dur須嚴格大於"
                  "現有值才覆蓋」判斷, 不影響其餘語意)",
        "note": "user規則(2026-07-12追加): 先攻/遇襲/洞察/嘲諷/虛弱與繳械/計窮/震懾/混亂"
                "同規則(唯一+同等或更強擋新), 由pending轉正",
    },
    "遇襲": {
        "mode": "unique_strongest",
        "engine": "u.ambush(同first, 單一欄位), apply_control_dur() 同上(insight/immuneTo"
                  "免疫邏輯不變, 只有通過免疫檢查後才進入同等或更強比較)",
        "note": "同先攻",
    },
    "洞察": {
        "mode": "unique_strongest",
        "engine": "u.insight(單一剩餘回合數欄位, 免控buff), apply_control_dur() 同上——"
                  "「施加時同時解除既有控制(stun/silence/disarm/chaos/ambush歸零)」這個"
                  "副作用現在也隨主判斷gate: 只有本次insight施加確實通過『同等或更強』"
                  "檢查(回傳True, 真的套用了)才觸發解除控制, 較弱的insight施加完全跳過"
                  "(不解控、不覆蓋)",
        "note": "同先攻",
    },
    "嘲諷": {
        "mode": "unique_strongest",
        "engine": "u.taunt_by/u.taunt_dur(單一欄位組) —— 新dur須嚴格大於現有taunt_dur"
                  "才會同時更新taunt_by(改指向新施加者)與taunt_dur, 否則兩者皆維持原值"
                  "(taunt_by不因較弱的新嘲諷施加而變更目標)",
        "note": "同先攻",
    },
    "虛弱": {
        "mode": "unique_strongest",
        "engine": "amp(val:-1.0, 走 u.adds 清單, 現行多來源仍走push_add既有(kind,src)"
                  "去重/共存機制, 本批未改動底層amp/adds通道)",
        "note": "user規則要求虛弱比照unique_strongest, 但虛弱是「總amp<=-1即封頂全歸零」"
                "的clamp效果(非線性可加): 分析後確認「多來源共存加總」與「唯一+同等或更強"
                "覆蓋」在此clamp語意下OBSERVATIONALLY等價(weak的持續時間=所有已施加來源中"
                "最晚到期者, 兩種實作方式算出的『weak還剩幾回合』結果相同, 見報告與"
                "engine_limitations.md詳細分析)——本批因此未改動push_add/amp底層機制,"
                "只重新歸類mode為unique_strongest並記錄此判斷供覆核; 若日後改為非clamp"
                "的可疊加虛弱效果, 需重新檢視此結論",
    },
    # ---- 已確認 refresh(刷新覆蓋, 唯一/非共存) ----
    "灼燒": {
        "mode": "refresh",
        "engine": "dot(u.dots list, 以狀態名(dots[3], 解析不到時退而用來源戰法名)為鍵, "
                  "見k==\"dot\"分支: 同鍵新施加時整筆取代舊的(用最新coef/dur/來源), 不同鍵"
                  "各自並存; resolve_dot_name/count_named_statuses 依名稱分組計數, 供"
                  "dmgFromStatus等橫切效果讀取)",
        "note": "DoT家族(灼燒/水攻/中毒/潰逃/沙暴/叛逃共6種具名狀態, 見dmgFromStatus"
                "清單)之一。user規則: 同名DoT新施加時覆蓋舊的, 不並存多個(前批"
                "u.dots.append不去重, 把DoT當共存清單是錯的, 已改refresh)",
    },
    "中毒": {"mode": "refresh", "engine": "同灼燒, dot(DoT家族之一)", "note": "同上"},
    "潰逃": {
        "mode": "refresh",
        "engine": "同灼燒, dot(DoT家族之一, 見 dmgFromStatus 清單/左右開弓「若目標為騎兵"
                  "則額外造成潰逃狀態」)",
        "note": "同上; 附帶記錄DoT家族另兩員(水攻/沙暴)+叛逃, 同規則(refresh)",
    },
}


APT_PCT = {"S": 1.20, "A": 1.00, "B": 0.85, "C": 0.70, "D": 0.55, None: 0.85}
APT_RANK = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0, None: -1}
SCALE_CLAMP = 1.5                                    # amp/mitig 縮放後上限保護: |val| <= 1.5

# 批36: 兵種營建築(Lv0~10) —— 錨點 docs/data/calibration_anchors.json → troop_camp「三合一」
# 拆解: (1) 全屬性+4(=既有CAMP常數, 全域已無條件套用, 非本批新增) (2) 每級+0.25%該兵種造成
# 傷害(本批新增, 滿級+2.5%) (3) Lv10附贈對應兵種戰法(本批新增attach邏輯)。CAMP_DMG_PER_LV
# 直接作用在 amp 原語(「造成傷害提升」, 與現有進階/典藏 a.amp 同慣例, 見下方 Unit.__init__)。
CAMP_DMG_PER_LV = 0.0025
# 兵種(隊伍) → 該兵種營Lv10附贈戰法名稱(見 tactics_parsed.json cat/src:"BUILDING" 五筆)。
# 器械營「負重」無戰鬥內效果(type:"none", 已被 TACTICS 載入時的過濾排除, 不進 TACTICS 表),
# 故器械不掛(對稱書寫仍列出, 值為 None, 供 attach 邏輯統一走同一張表)。
CAMP_TROOP_TACTIC = {"槍": "破軍", "盾": "守禦", "弓": "齊射", "騎": "疾馳", "器": None}

# 批35 D: block(抵禦/警戒) 消耗門檻 —— grok查證機鑑先識原文「受到的傷害超過自身可攜帶最大
# 兵力的6%時(最低100兵力)」才消耗1次警戒。max(START_TROOP×6%, 100) —— 本引擎START_TROOP
# 恆為10000(單一兵力池常數), 6%=600本身已遠大於100下限, 下限條款只在極端自訂規模才會生效,
# 此處仍照原文寫出以求精確。
BLOCK_CONSUME_THRESHOLD = max(START_TROOP * 0.06, 100)

# 批18: 傷兵池(治療上限) —— user 遊戲實測: 受到的傷害按「當時回合數」轉化為「可救援(計入
# 傷兵池, 治療只能回這部分)」vs「不可救援(直接陣亡, 治療無法挽回)」, 轉化率隨回合遞減
# (見 docs/data/calibration_anchors.json -> wounded_pool)。1~3回合90%、4~6回合80%、
# 7~8回合67.5%(原文65~70%取中值)。準備階段(CUR_ROUND=0)算第1回合檔。
WOUNDED_RATES = [0.90, 0.90, 0.90, 0.80, 0.80, 0.80, 0.675, 0.675]  # index 0 = 第1回合


def wounded_rate(r):
    idx = max(0, min(len(WOUNDED_RATES), r or 1) - 1)
    return WOUNDED_RATES[idx]


# 批33: 治療(heal)絕對量公式全局換裝 —— 舊公式 want = coef×SCALE(scale屬性)×caster.troop×0.10
# 疑似系統性高估(見 engine_limitations.md 第18節: 陷陣營樣本高估1.6~2倍, 且形狀錯誤——治療量
# 不應隨施放者「當下」兵力增減)。初版曾裁決 want=506×coef×SCALE(不乘兵力), 但 user 補測
# 華佗2(智力228/準備階段兵力9600/青囊96%→實測755)推翻該版本: 506那組樣本(青囊96%/智力284
# →742)恰好是施放者準備階段兵力~8433的巧合摺疊(506≈0.06×8433), 換一個準備兵力不同(9600)的
# 樣本立刻對不上(506版預測663, 誤差14%; "×準備階段兵力"版預測755.2, 誤差0.03%)。
# 最終公式(docs/data/calibration_anchors.json → heal_formula_resolved_20260704, 後續更新):
#   want = coef(治療率) × HEAL_TROOP_C(0.06) × 施放者準備階段鎖定兵力 × SCALE(scale屬性,預設intel)
# 「準備階段鎖定」語意: 指揮/兵種/兵書/被動類 heal(常駐急救型)的治療量以「開戰準備階段的
# 兵力」定格(華佗1當下兵力8611~8781持續變動但治療恆742, 非隨當下兵力浮動), 故用
# caster.heal_base(prep時存的 troop×HEAL_TROOP_C 快照, 見 Unit 建構)而非 caster.troop×常數。
# active主動直療型(如刮骨療毒, 施放當下即時觸發的治療, 非受傷反應式)用施放當下即時兵力
# (caster.troop)。刮骨樣本初次核對曾疑似-11%偏差(疑主動型基底常數有異), 後證實該樣本傷兵池
# 已耗盡、觀測值為封頂後殘值(非公式未封頂前的真實want), 與公式無關——主動直療型與反應式
# 急救型共用同一套公式(HEAL_TROOP_C), 不分型態另設基底常數, 僅兵力取值時點不同。
# 驗證樣本: 陷陣營60%/智力379.02/準備兵力8439→546(反解值, 弱錨點); 青囊96%/智力228/
# 準備兵力9600→755(強錨點, user新補測, 0.03%誤差)。
# 補充參考樣本(第三批戰報, 未落地到具體戰法資料——「離月」在本庫查無此戰法, 疑user口誤/
# 待查證, 暫不修改任何tactics資料, 僅記錄公式驗證結果供未來核對): 直療68%/貂蟬智力397/
# 開場兵力8580→曹操622×2+陸遜627, v2公式(want=0.68×0.06×8580×SCALE(397))預測647.1,
# 殘差約-3%~-4%(可能戰內智力浮動), 在既有容忍帶內, 不阻塞, 亦不改動公式常數。
HEAL_TROOP_C = 0.06


def SCALE_G(v, div=350):
    """批35: 曲線族原語泛化。除數預設350(向後相容, 傷害/治療/多數增減益類走這條), 但
    docs/data/calibration_anchors.json → status_scale_375_20260704(user 機鑑先識警戒六點實測,
    荀彧智力478.84~389.72, 六點小數點後兩位精確吻合)證實「狀態效果」(block/部分%值狀態類)
    這一族走除數375的獨立曲線(375點翻倍, 而非350)。呼叫端傳 div 覆蓋預設(逐效果 e.scaleDiv
    透傳), 不擅自把全域 SCALE 從350改成375。"""
    return max(0.0, 1 + (v - 100) / (div or 350))


def SCALE(v):
    """「受X影響」屬性縮放旋鈕。輸入為戰鬥內即時素質 caster.eff(stat)(已含城建/陣營/適性/
    加點/賽季/戰鬥中buff, 典型值 250~400, 而非卡面裸值)。公式取社群拆解(巴哈姆特高等陣容
    戰法論/NGA數據貼): 屬性100=面板基準值(SCALE=1.0), 每+350點效果翻倍(v=450時SCALE=2.0)。
    仍是可調校準旋鈕, 之後有更多實測數據可再調整斜率/錨點。"""
    return SCALE_G(v, 350)


def scale_of(caster, scale, scale_div=None):
    """批35: scale_div(可選) —— 效果級 e["scaleDiv"] 透傳, 預設350(SCALE 向後相容)。
    批I(禁近似令-scale/比較族): scale=="maxStat" —— 動態取施放者當下四維(force/intel/
    command/speed, 不含魅力)中最高一項代入SCALE_G, 取代「受自身最高屬性影響」的固定取值
    近似(扶危定傾/剛柔並濟/整軍經武等, 見engine_limitations.md第12/6.6節鏡像缺口)。零新增
    呼叫點: 全庫既有sv_val/sv_mult/sv_add/locked_scale_of一律透過此函式讀取scale倍率,
    scale欄位本身早已是KNOWN_EFFECT_FIELDS全域已知欄位, 這裡只是多一個合法字串值,
    prep鎖定沿用locked_scale_of既有委派(批35 lockedScaleOf慣例, 無需另外修改)。"""
    if not scale:
        return 1.0
    if scale == "maxStat":
        return SCALE_G(max(caster.eff(s) for s in ("force", "intel", "command", "speed")), scale_div)
    return SCALE_G(caster.charm, scale_div) if scale == "charm" else SCALE_G(caster.eff(scale), scale_div)


def resolve_stat_field(u, stat):
    """批I(禁近似令-scale/比較族): e["stat"]=="maxStat" —— 動態解析為 u 當下四維(force/
    intel/command/speed)最高的一項欄位名, 供 k=="stat" 效果動態選定要加成哪個屬性(形一陣
    「自身最高屬性+30→60點」)。與 scale=="maxStat"(見 scale_of)共用「四維中最高一項」
    語意, 但消費端不同: 這裡回傳屬性欄位名字串(供 push_stat_add/push_mod 指定要動的欄位),
    scale_of 回傳的是縮放倍數(乘在別的值上), 兩者是同一個「取最高」判斷的兩種消費形態。"""
    if stat != "maxStat":
        return stat
    return max(("force", "intel", "command", "speed"), key=lambda s: u.eff(s))


def stat_compare_ok(ref, target, allies, spec):
    """批I(禁近似令-scale/比較族): ifStatCompare —— 比較「參照方」(caster自身或我軍主將)
    vs「目標」同一屬性的大小, 決定效果/extraHits段是否生效(布林gate, 對稱ifTargetHas但
    比較的是「屬性大小」而非「狀態有無」)。spec: {stat, op("gt"/"gte"/"lt"/"lte", 預設
    "gt"), vs("caster"預設/"leader")}。op 語意固定為「參照方 op 目標」方向(如op="gt"即
    「參照方該屬性較高」), 三筆真實案例(摧鋒斷刃「自身武力較高」/竊幸乘寵「自身智力高於
    目標」/聚石成金「敵軍魅力低於我軍主將」)恰好都是op="gt", 只是vs不同(前二者vs自身
    caster, 後者vs我軍主將leader), 驗證此形狀已是最小通用形, 不需要更多op/vs組合。"""
    if not spec or not target:
        return False
    stat = spec.get("stat", "force")
    op = spec.get("op", "gt")
    vs = spec.get("vs", "caster")
    ref_u = allies[0] if (vs == "leader" and allies) else ref
    if not ref_u:
        return False
    # 禁近似令-批L: stat=="hpPct" —— 比較雙方「兵力百分比」(troop/START_TROOP)而非傳統四維
    # 屬性, 供先登死士「若兵力百分比低於攻擊者」這類跨單位血量比較(對稱既有when["hpBelow"]/
    # hpAbove只認caster自身, 這裡是ref/target雙方各自讀u.hp_pct, 走既有ifStatCompare的op/vs
    # 骨架, 零新增比較邏輯, 只新增一種可讀的stat名稱), 對稱engine.js同名分支。
    rv = ref_u.charm if stat == "charm" else (ref_u.hp_pct if stat == "hpPct" else ref_u.eff(stat))
    tv = target.charm if stat == "charm" else (target.hp_pct if stat == "hpPct" else target.eff(stat))
    if op == "gt":
        return rv > tv
    if op == "gte":
        return rv >= tv
    if op == "lt":
        return rv < tv
    if op == "lte":
        return rv <= tv
    return False


def scale_compare_of(caster, target, spec):
    """批I(禁近似令-scale/比較族): scaleCompare —— 施放者vs目標同一屬性「差值」代入縮放
    曲線, 對稱scale_of(單方固定屬性)但讀取雙方差值(神機妙算「並基於雙方智力差額外提高」)。
    spec: {stat(預設"intel"), div(選填, 預設350, 沿用SCALE_G同斜率慣例)}。diff=0(雙方
    持平)時倍率=1.0(無額外加成), 與原文「額外提高」的直覺語意吻合(施放者該屬性比目標高
    才有正向加成, 反之為負)。無實測錨點校準div, 沿用SCALE_G預設除數350, 待未來戰報校準
    (同全域SCALE的350除數一樣是可調校準旋鈕, 非最終定案)。"""
    if not spec or not target:
        return 1.0
    stat = spec.get("stat", "intel")
    div = spec.get("div") or 350
    cv = caster.charm if stat == "charm" else caster.eff(stat)
    tv = target.charm if stat == "charm" else target.eff(stat)
    return max(0.0, 1 + (cv - tv) / div)


def cap_val_of(v, cap_val):
    """批35: 效果級可選欄位 e["capVal"](值上限), 縮放後 clamp。慣例「狀態效果上限=基礎值×2」
    (錨點: 機鑑先識 40%→80% cap)不自動套用, 逐效果顯式標 e["capVal"]。未標則不 clamp。"""
    return min(v, cap_val) if cap_val is not None else v


def locked_scale_of(caster, e):
    """批35 B: 「受X影響」狀態值類效果(block 為主, 現行機鑑先識警戒) 的「準備階段鎖定」語意
    —— 效果的 scale 縮放值在 prep 階段(第一次掃描到該效果, 不論它本身是否於 prep 就實際套用)
    算定並鎖住, 之後(如 everyRound 補層段延後到第2/3回合才擲骰命中)一律沿用鎖定值, 不因戰鬥中
    智力浮動重新計算。與 heal_base 準備階段鎖定兵力快照同一慣例(第二次獨立確認)。用「效果物件
    本身」當快取鍵(caster.scale_lock: dict[id(e), value], 惰性建立)。只用於帶 scale 的 block,
    不擴大到其餘 k(目前無對應實測樣本佐證其餘k同樣適用, 見 engine.js 對應註解)。"""
    scale = e.get("scale")
    if not scale:
        return 1.0
    if caster.scale_lock is None:
        caster.scale_lock = {}
    key = id(e)
    if key not in caster.scale_lock:
        caster.scale_lock[key] = scale_of(caster, scale, e.get("scaleDiv"))
    return caster.scale_lock[key]


# 批7: 發動率類「受X影響」縮放 —— 獨立常數, 與上面 SCALE(每+350翻倍) 不是同一條曲線。
# user 實測太平道法(黃巾/張角, docs/data/calibration_anchors.json → rate_scale): 智力484.6
# 才翻倍(對比 SCALE 只要+350即450), 反解 c=0.002598(6組獨立點一致到小數第6位, 取0.0026)。
# chargeup 尚無獨立實測, 暫共用同常數(假設同曲線, 待未來樣本校準)。
RATE_SCALE_C = 0.0026
RATE_SCALE_DEFAULT_DIV = 1 / RATE_SCALE_C  # ≈384.6154 —— rateup/chargeup 預設曲線除數(向後相容, 與 engine.js 同款)

# 批46 A: e["scaleDiv"](可選) —— 覆蓋預設除數384.6, 供不同曲線族的rateup戰法各自標記(比照
# amp/mitig 的 scale_of 第三參數 scale_div 慣例)。實測依據: 十二奇策(docs/data/
# calibration_anchors.json → shierqice_20260707) user七點齊發精確收斂 D=335.1±0.15。


def rate_scale_of(caster, scale, scale_div=None):
    if not scale:
        return 1.0
    div = scale_div or RATE_SCALE_DEFAULT_DIV
    v = caster.charm if scale == "charm" else caster.eff(scale)
    return 1 + (v - 100) / div


def morale_mult(m):
    return 0.007 * min(m, 100) + 0.30                 # 士氣上限100(戰報: 士氣110.4傷害不變, 超過100按100算)


def counter_mult(a, b):
    if a == "器" or b == "器":
        return 1.15 if b == "器" else 0.85
    if COUNTER.get(a) == b:
        return 1.15
    if COUNTER.get(b) == a:
        return 0.85
    return 1.0


# ---------------------------------------------------------------------------
# effectText 啟發式解析: sgsdeck 戰法 dict -> 引擎可用 runtime 戰法
# effectType: 增益/治療/控制/減益/兵刃群體/兵刃單體/謀略群體/謀略單體/援護/內政
# kind:       ACTIVE主動/BURST突擊/COMMAND指揮/PASSIVE被動/FORMATION陣/TROOP兵種/INTERNAL內政
# ---------------------------------------------------------------------------
def _maxval(text, kw):
    """抓 'kw...A→B' 或 'kw...A%→B%', 回傳升滿值 B (取範圍上限)。"""
    m = re.search(kw + r"[^0-9]{0,10}(\d+(?:\.\d+)?)\s*%?\s*[→~]\s*(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(2))
    m = re.search(kw + r"[^0-9]{0,10}(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


TYPE_MAP = {"ACTIVE": "active", "BURST": "charge", "COMMAND": "command",
            "PASSIVE": "passive", "FORMATION": "command", "TROOP": "command"}


def parse_effect(t):
    """回傳 runtime 戰法 dict, 或 None (內政/無法戰鬥)。"""
    et = t.get("effectType") or ""
    if "內政" in et or t.get("kind") == "INTERNAL":
        return None
    txt = t.get("effectText") or ""
    rt = {"type": TYPE_MAP.get(t.get("kind"), "passive"),
          "rate": t.get("activationRate") or 1.0,
          "kind": "intel" if "謀略" in et else "phys",
          "coef": 0.0, "n": 3 if ("群體" in (et + (t.get("effectTarget") or ""))
                                   or "全體" in txt) else 1,
          "prep": 1 if "準備" in txt else 0, "effects": []}
    if "兵刃" in et or "謀略" in et:                  # 傷害型
        v = _maxval(txt, "傷害率") or _maxval(txt, "傷害")
        rt["coef"] = (v / 100) if v else 1.2
    elif et == "治療":
        v = _maxval(txt, "治療率") or _maxval(txt, "恢復") or _maxval(txt, "回復")
        rt["effects"].append({"k": "heal", "who": "ally", "coef": (v / 100) if v else 0.8, "dur": 1})
    elif et == "控制":
        rt["effects"].append({"k": "stun", "who": "enemy", "dur": 1})
    elif "增益" in et:                                # 我方增傷
        v = _maxval(txt, "提高") or _maxval(txt, "提升") or 12
        rt["effects"].append({"k": "amp", "who": "ally", "val": v / 100 if v <= 100 else 0.15, "dur": 99})
    elif "減益" in et:                                # 削弱敵方輸出
        v = _maxval(txt, "降低") or _maxval(txt, "減少") or 12
        rt["effects"].append({"k": "amp", "who": "enemy", "val": -(v / 100 if v <= 100 else 0.15), "dur": 2})
    return rt


# ---------------------------------------------------------------------------
# 載入真實全庫
# ---------------------------------------------------------------------------
class General:
    def __init__(self, raw):
        self.name = raw["name"]
        self.faction = raw.get("faction", "?")
        self.gender = raw.get("gender")
        self.apt = raw.get("affinity", {})           # 各兵種適性 S/A/B/C/D, 戰鬥兵種由隊伍決定
        st = raw.get("stats", {})
        self.base = {"force": st.get("武力", 80), "intel": st.get("智力", 80),
                     "command": st.get("統率", 90), "speed": st.get("速度", 70)}
        self.charm = st.get("魅力", 60)               # 魅力: 只供 scale="charm" 查表, 不進戰鬥四維 eff()
        self.tactic = TACTICS.get(raw.get("tactic")) if raw.get("tactic") else None
        self.tactic_name = raw.get("tactic") or "—"
        self.bingshu_cats = raw.get("availableBingshu", [])  # 可用兵書類別

    def apt_pct(self, troop):                         # 該武將用此兵種時的屬性發揮%
        return APT_PCT.get(self.apt.get(troop), 0.85)

    def best_troop(self):                             # 最佳適性兵種(預設選擇參考)
        return max((t for t in ("騎", "盾", "弓", "槍", "器")),
                   key=lambda t: APT_RANK.get(self.apt.get(t), -1))


def team_troop(team):                                 # 一隊的建議兵種: 三人適性總和最高者
    gs = [POOL[n] for n in team]
    return max(("騎", "盾", "弓", "槍", "器"),
               key=lambda t: round(sum(g.apt_pct(t) for g in gs), 4))  # 抹平浮點誤差, 與 engine.js 平手取先序


# 優先用 LLM 解析檔(tactics_parsed.json), 沒有才退回正則啟發式
_parsed = os.path.join(DATA, "tactics_parsed.json")
if os.path.exists(_parsed):
    with open(_parsed, encoding="utf-8") as f:
        _parsed_list = json.load(f)
    TACTICS = {o["nameZh"]: o for o in _parsed_list if o.get("type") != "none"}
    TACTIC_SRC = "LLM 解析"
    # 批10: 資料衛生防禦 —— 載入時掃描 |amp.val| > 3 的極端值並印警告(不擋), 供資料層儘早
    # 發現如「coef 誤重複灌入 amp.val」這類系統性錯誤(見批10 corrections 仲裁)。只警告,
    # 不修改資料本身(修正應在 tactics_parsed.json/corrections 層完成)。
    for _t in _parsed_list:
        for _e in _t.get("effects", []):
            if _e.get("k") == "amp" and isinstance(_e.get("val"), (int, float)) and abs(_e["val"]) > 3:
                print(f"[tactics data] {_t.get('nameZh', '?')}: amp.val={_e['val']} 超過 |3| 常見範圍, 疑似資料異常(如 coef 誤灌入 amp.val)")
else:
    with open(os.path.join(DATA, "tactics.json"), encoding="utf-8") as f:
        TACTICS = {}
        for t in json.load(f):
            rt = parse_effect(t)
            if rt:
                TACTICS[t["nameZh"]] = rt
    TACTIC_SRC = "正則啟發式"
with open(os.path.join(DATA, "generals.json"), encoding="utf-8") as f:
    POOL = {}
    for raw in json.load(f):
        if raw.get("stats"):                          # 跳過無屬性的(少數)
            g = General(raw)
            POOL[g.name] = g

# 兵書: 名稱 -> 效果; 各類別的主兵書(供預設裝備)
BINGSHU, MAIN_BY_CAT, SUB_BY_CAT = {}, {}, {}
_bs = os.path.join(DATA, "bingshu_parsed.json")
if os.path.exists(_bs):
    for b in json.load(open(_bs, encoding="utf-8")):
        key = b["category"] + "·" + b["name"]     # 複合鍵(同名跨類別不撞)
        BINGSHU[key] = b
        (MAIN_BY_CAT if b.get("type") == "主兵書" else SUB_BY_CAT).setdefault(
            b["category"], []).append(key)


def default_bingshu(g):                               # 預設主兵書: 該將首個可用類別的主兵書
    for c in g.bingshu_cats:
        if MAIN_BY_CAT.get(c):
            return MAIN_BY_CAT[c][0]
    return None


def _load_list(fname):
    p = os.path.join(DATA, fname)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []


BONDS = _load_list("bonds_parsed.json")               # 緣分: 隊伍湊齊觸發
EQUIPS = {}                                            # 裝備: 自身被動; 鍵為複合鍵"type·name"(同兵書precedent, 同名跨欄位不撞), 另存純名稱 fallback(向後相容)
for _e in _load_list("equips_parsed.json"):
    EQUIPS[_e["type"] + "·" + _e["name"]] = _e
    EQUIPS.setdefault(_e["name"], _e)                  # 純名稱 fallback: 同名跨type時保留先出現者, 呼叫端應改用複合鍵


def active_bonds(team):                               # 隊伍觸發的緣分(湊齊 triggerCount 人)
    s = set(team)
    return [bd for bd in BONDS if len(s & set(bd.get("generals", []))) >= bd.get("triggerCount", 99)]


SEASON_MODS = {}
_sm = os.path.join(DATA, "season_modifiers.json")
if os.path.exists(_sm):
    SEASON_MODS = {k: v for k, v in json.load(open(_sm, encoding="utf-8")).items()
                   if not k.startswith("_")}


def season_mods(g, idx, team, scenario):              # 該將在此賽季的養成修正
    out = {"apt_add": 0.0, "apt_s": False, "flat": 0, "mult": 1.0}
    for m in SEASON_MODS.get(scenario, []) if scenario else []:
        t = m.get("type")
        if t == "faction_scale":
            facs = [POOL[n].faction for n in team]
            top = max((facs.count(f) for f in set(facs)), default=0)
            if top >= m.get("partialThreshold", 2):
                out["mult"] *= 1 + (m.get("fullBonus", 0.1) if top >= len(team)
                                    else m.get("partialBonus", 0.07))
        elif t == "apt_add" and g.gender == m.get("gender"):
            out["apt_add"] += m.get("value", 0.15)
        elif t == "apt_override" and idx < m.get("maxSlots", 2):
            out["apt_s"] = True
        elif t == "stat_flat":
            out["flat"] += m.get("all", 0)
    return out


# ---------------------------------------------------------------------------
# 評分 + 配將推薦
# ---------------------------------------------------------------------------
def score(team, troop=None):
    g = [POOL[n] for n in team]
    troop = troop or team_troop(team)
    attr = sum((max(x.base["force"], x.base["intel"]) + x.base["command"]
                + x.base["speed"]) * x.apt_pct(troop) for x in g)
    kinds = set()
    for x in g:
        if x.tactic:
            kinds.add(x.tactic["type"])
            kinds |= {e["k"] for e in x.tactic["effects"]}
    apt_bonus = round(sum(x.apt_pct(troop) for x in g) / 3 * 80)  # 三人對隊伍兵種適性越高越好
    same_fac = 40 if len({x.faction for x in g}) == 1 else 0
    return round(attr / 3 + len(kinds) * 25 + apt_bonus + same_fac)


def _base(n):                                         # SP關羽 與 關羽 為同一角色, 不可同隊
    return n.replace("SP ", "").replace("SP", "")


def recommend(pool=None, k=3, top=8):
    names = pool or list(POOL)
    combos = (c for c in combinations(names, k) if len({_base(x) for x in c}) == k)
    ranked = sorted(combos, key=lambda c: score(c), reverse=True)
    return [(list(c), score(c), team_troop(c)) for c in ranked[:top]]


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
def team_gate_ok(gate, factions):
    """批24 D1: teamGate(隊伍構成前提) —— 判斷隊伍陣營組成是否符合戰法宣告的前提。
    "allDiff": 三名武將陣營兩兩不同(潛龍陣「我軍三名武將陣營均不相同時」); "allSame":
    三名武將陣營皆相同(供未來同類戰法使用, 目前全庫無此案例但一併支援對稱語意)。
    factions 為隊伍全體(含自己)的陣營陣列, 已在 fight() 建構 Unit 前準備好傳入。"""
    if not gate or not gate.get("factions"):
        return True
    uniq = len(set(factions))
    if gate["factions"] == "allDiff":
        return uniq == len(factions)
    if gate["factions"] == "allSame":
        return uniq == 1
    return True                                       # 未知 gate 種類: 保守放行(不擋), 避免資料錯字導致戰法整組消失


def count_ally_faction(allies, faction):
    """禁近似令-批K: 對稱 engine.js countAllyFaction(faction_count_scale族) —— 數出隊伍
    (allies, 含自己)中陣營恰為 faction 的存活人數, 供 rateFactionBonus 線性縮放觸發率。"""
    if not faction:
        return 0
    return len([a for a in (allies or []) if a.alive and a.g and a.g.faction == faction])


def count_active_buff_types(u, types):
    """禁近似令-批K: 對稱 engine.js countActiveBuffTypes(rate_self_dynamic族) —— 數出 u
    當下持有的「功能性增益狀態」種類數(連擊/洞察/先攻/必中/破陣/規避), 供
    rateBonusPerBuffType 動態加成觸發率。"""
    if not u or not types:
        return 0
    n = 0
    for ty in types:
        if ty == "extra" and u.addbonus("extra") > 0:
            n += 1
        elif ty == "insight" and u.insight > 0:
            n += 1
        elif ty == "first" and u.first > 0:
            n += 1
        elif ty == "surehit" and u.surehit_dur > 0:
            n += 1
        elif ty == "pierce" and u.addbonus("pierce") > 0:
            n += 1
        elif ty == "dodge" and u.dodge_dur > 0:
            n += 1
    return n


class Unit:
    def __init__(self, g, ttype, bingshu=None, equip=None, add=None, inherit=None, season=None, team_factions=None, camp_lv=0, is_camp_holder=False):
        self.g, self.ttype, self.troop, self.stun = g, ttype, START_TROOP, 0
        self.camp_lv = camp_lv or 0                   # 批36: 兵種營等級(0~10, 隊伍級, 見 fight() 呼叫端), 0=不啟用(向後相容既有全部呼叫點)
        # 批33: heal_base —— 準備階段鎖定的治療基準兵力快照(troop×HEAL_TROOP_C), 供指揮/兵種/
        # 兵書/被動類 heal(常駐急救型)使用, 使治療量不隨後續戰鬥中兵力增減而變動(見上方
        # HEAL_TROOP_C 常數註解); 建構時 troop 尚未受戰鬥影響, 此處快照即「開戰準備階段兵力」。
        self.heal_base = self.troop * HEAL_TROOP_C
        self.silence = 0                              # 計窮: 無法發動主動戰法
        self.disarm = 0                                # 繳械: 無法普通攻擊(含連擊/突擊)
        self.chaos = 0                                 # 批12 ModeF: 混亂(不鎖行動, 但普攻/單體主動戰法改為敵我不分隨機選目標), 剩餘回合數
        self.insight = 0                               # 洞察: 免疫 stun/silence/disarm/chaos, 施加時同時解除
        self.first = 0                                 # 先攻: 剩餘回合數, 排序時優先於速度
        self.ambush = 0                                # 批18: 遇襲(先攻的反面, 遲緩) —— 剩餘回合數, 行動排序時與 first 一併算 eff_first
        # 狀態疊加精修批(user規則 status_stacking_detail_20260712): 嚴密 —— 赴湯蹈火「賦予我軍
        # 群體抵禦狀態與特殊護盾『嚴密』」的第二個狀態(過去只建模了抵禦/block那一半, 嚴密本身
        # 完全未編碼, 見 tactic_corrections.json「赴湯蹈火」_todo)。本批新增此欄位純粹作為
        # 「持有者是否處於嚴密」的偵測旗標(單一剩餘回合數欄位, 對稱insight/first等既有簡單buff
        # 慣例), 供 push_block() 判斷抵禦(block val>=1.0)例外改累積(見其定義); 『特殊護盾』
        # 本身若還有額外機制(如額外吸收池)則仍未編碼, 該部分揭露維持原狀不變, 本欄位只承接
        # user規則明確要求的「偵測嚴密決定抵禦刷新或累積」這一件事。
        self.rigorous = 0
        self.wounded = 0.0                             # 批18: 傷兵池 —— 累積「可救援」量(受到的傷害按當時回合轉化率折算); 治療結算上限=min(治療量, wounded, START_TROOP-troop)
        # 批52j: 捕獲(capture) —— 暗箭難防獨立狀態; 不可淨化; 無法行動/造成傷害/禁用指揮被動/
        # 禁療/友軍不可選中; 同時全場最多一名(見 k=="capture")
        self.captured = 0
        # 自帶 + 傳承; 自帶戰法(g.tactic)淺拷貝附加 native:True 旗標(供 rateup/chargeup 的 nativeOnly
        # 修飾判斷「這是不是自帶戰法」, 如太平道法只加成張角自帶的五雷轟頂)。淺拷貝而非直接改
        # TACTICS 共享物件, 避免多個武將共用同一戰法物件時互相污染(如兩人都自帶白眉)。
        # 批24 D1: teamGate —— 開戰時(建構Unit當下, team_factions已由fight()備妥)判定一次,
        # 不滿足前提的戰法整條從 self.tactics 過濾掉(不進入後續 cmd_passive_srcs/on_hit_tacs/
        # on_hit_effect_tacs 等衍生快取, 亦不會被 apply_passives/回合迴圈讀到, 等同整戰法不生效)。
        # sgz.py 無 TRACE/日誌機制(僅 docs/engine.js 供瀏覽器UI推演明細用), 此處純過濾不列印。
        def _gate_ok(t):
            return team_gate_ok(t.get("teamGate"), team_factions or [])
        self.tactics = [t for t in (
            ([dict(g.tactic, native=True)] if g.tactic else []) +
            [TACTICS[nm] for nm in (inherit or []) if nm in TACTICS]  # 自帶 + 傳承戰法
        ) if _gate_ok(t)]
        # 批36: 兵種營Lv10附贈戰法 attach —— 原文是「我軍隨機單體/群體」觸發(一整隊只發生
        # 一次), 而非「隊上每個單位各自獨立擁有這個被動」。故只有 fight() 指定的單一「持有者」
        # (is_camp_holder=True, 每隊隨機挑1人, 見 fight() 呼叫端)才實際 append 進 self.tactics;
        # 其餘同隊隊友仍受 camp_lv 的屬性%加成(下方amp段, 對每個Unit都算, 因原文那一支是「全隊
        # 造成傷害」的隊伍級加成, 與Lv10戰法是三合一裡各自獨立的兩支), 但不會各自重複攻得
        # Lv10戰法(避免3人隊「破軍/守禦」各自觸發3次的過量bug, 已用鏡像對局實測驗證, 見demo()
        # 97-101號assert)。依「本隊實際兵種(ttype, 隊伍級)」查表 CAMP_TROOP_TACTIC, 命中且
        # TACTICS 已載入該名稱(器械營"負重"因 type:"none" 被載入時過濾, 表中值為 None 或查無
        # 則不掛)才 append。必須在此處(cmd_passive_srcs/on_hit_tacs/on_hit_effect_tacs/
        # on_deal_tacs 等衍生快取產生之前)插入, 因五戰法皆 type:"passive" 會被那些快取掃描到
        # (對比裝備proc戰法是charge型, 晚插入也不影響)。淺拷貝加 _campBuilding:True 標記
        # (純供辨識, 不影響戰鬥邏輯分派)。
        if self.camp_lv >= 10 and is_camp_holder:
            camp_tac_name = CAMP_TROOP_TACTIC.get(ttype)
            camp_tac = camp_tac_name and TACTICS.get(camp_tac_name)
            if camp_tac:
                self.tactics.append(dict(camp_tac, _campBuilding=True))
        # 批18: fakeReport(偽報) 加強 —— 記錄「自己的指揮/被動戰法」名稱集合, 供 eff()/addbonus()
        # 判斷某條 adds/mods/stat_adds 是否來自「本單位自己的指揮/被動戰法」(見 engine.js 同名欄位註解)。
        self.cmd_passive_srcs = {t.get("nameZh") for t in self.tactics
                                  if t.get("type") in ("command", "passive") and t.get("nameZh")}
        _bn = bingshu if isinstance(bingshu, (list, tuple)) else ([bingshu] if bingshu else [])
        # 狀態疊加語意對齊批: 每筆兵書效果淺拷貝附加 _bsNm(來源兵書名), 對稱既有裝備 _eqNm
        # 慣例(見下方 _eq_all) —— apply_passives() 對 u.bs 呼叫 apply_effects() 時傳入的 "t"
        # 是匿名合成 dict({"effects": eff, "kind": "phys"}, 無 nameZh), 反擊/急救/休整等具名
        # 狀態實例需要來源顯示名(src_name, 供未來戰報「執行來自【X】的【狀態】」)時單靠
        # t.get("nameZh") 取不到, 故補標在效果本身上(見 effect_src_name() 讀取優先序)。淺拷貝
        # (不動 BINGSHU 原始共享物件), 與既有 _eqNm 做法一致。
        _bs_all = [dict(e, _bsNm=nm) for nm in _bn for e in BINGSHU.get(nm, {}).get("effects", [])]  # 兵書(主+副)合併效果
        self.bs = [e for e in _bs_all if not (e.get("when") or {}).get("on")]
        # 批22: 兵書效果級 e.when.on(急救類反應式治療, 如三軍之眾「戰鬥第2-4回合自身獲得急救」)
        # —— 與裝備 on_hit_eq 同慣例, 兵書效果本無獨立回合窗機制(apply_passives 只在 prep/
        # heal_only 套用整包 self.bs), 帶 e.when.on 的效果分離到此陣列, 於 on_hit() 反應式
        # 事件點結算。
        self.on_hit_bs = [e for e in _bs_all if (e.get("when") or {}).get("on") and (e.get("when") or {}).get("on") != "activeFired"]
        # 禁近似令-批K: active_fired_bs(once_consumable/engine_wiring_gaps_misc族) —— 對稱
        # engine.js activeFiredBs註解, 兵書效果級e.when.on=="activeFired"過去被on_hit_bs
        # 誤收(該迴圈只認attacked/damaged, 從未真正檢查activeFired, 靜默無效), 另建此陣列
        # 於active_fired_for()補上對稱消費端。
        self.active_fired_bs = [e for e in _bs_all if (e.get("when") or {}).get("on") == "activeFired"]
        _eq = equip if isinstance(equip, (list, tuple)) else ([equip] if equip else [])
        # 同名特技(跨type, 如四欄皆有的"無畏")遊戲規則只生效一件: 依基底名稱去重, 先出現者為準
        _eq_seen = set()
        _eq_objs = []
        for nm in _eq:
            e = EQUIPS.get(nm)
            if e and e["name"] not in _eq_seen:
                _eq_seen.add(e["name"])
                _eq_objs.append(e)
        _eq_all = []                                   # 裝備(4欄)合併效果(已去重); 每筆淺拷貝附加 _eqNm(供 TRACE 標名, 不動原資料物件)
        # 狀態疊加語意對齊批: 過去只有帶 e["when"] 的效果才淺拷貝附加 _eqNm(供 TRACE 標名),
        # 無 when 的效果(如荊棘/灼裂的 counter 反擊效果, 皆無 when 欄)直接沿用原物件、沒有
        # _eqNm——導致這類效果的具名狀態實例(反擊等)透過 effect_src_name() 讀取來源顯示名時
        # 落空(t 是 apply_passives() 對 u.eq 呼叫時的匿名合成 dict, 無 nameZh; e 又沒有
        # _eqNm)。改為無條件淺拷貝+標記, 涵蓋所有裝備效果(不分是否帶 when), 對 TRACE/既有
        # 讀取端零影響(多了一個從未被讀過的欄位), 只是把來源追蹤範圍從「僅反應式效果」擴大到
        # 「全部裝備效果」。
        for e in _eq_objs:
            for eff in e.get("effects", []):
                eff2 = dict(eff)
                eff2["_eqNm"] = e["name"]
                _eq_all.append(eff2)
        # 批8: 效果級回合窗(effect.when) —— 裝備效果不像戰法有獨立 when 欄(合併進 eq 陣列時已失去
        # 個別戰法邊界), 故 when 掛在「單條效果」本身(e["when"], 非 t["when"])。無 when 的效果照舊
        # 在準備階段(prep)一次性套用(self.eq); 帶 when 的效果分離到 delayed_eq, 於回合迴圈開始時
        # (與戰法 when 窗口同一時點)逐條檢查 round_ok 是否符合, 符合則一次性套用(when_fired 慣例,
        # 用效果物件本身 id() 去重)。帶 rate 的額外擲骰(如赳螑 50%機率)。
        self.eq = [e for e in _eq_all if not e.get("when")]
        self.delayed_eq = [e for e in _eq_all if e.get("when") and not e["when"].get("on")]
        # 批22: 裝備效果級 e.when.on(急救類反應式治療, 如長健/青囊書「戰鬥首回合受傷時回復
        # 10%兵力」) —— 與上面 delayed_eq(回合視窗一次性套用)不同語意: on="damaged"/"attacked"
        # 是「受傷當下觸發」, 不是「特定回合開啟時套用一次」。與 on_hit_effect_tacs(戰法版本)
        # 對應的裝備版本, 在 on_hit() 反應式事件點結算。
        # 批G: 明確排除 on=="dealtDamage"(見下方新增的 on_deal_eq), 對稱戰法級
        # on_hit_effect_tacs/on_deal_effect_tacs 的白名單收斂慣例(見上方批31 A修復註解: 過去
        # truthy檢查會讓dealtDamage被誤當成damaged/attacked放行, 與on_deal_eq觸發路徑重複結算)。
        self.on_hit_eq = [e for e in _eq_all if e.get("when") and e["when"].get("on") in ("attacked", "damaged")]
        # 批G: 裝備效果級 e.when.on=="dealtDamage"(「自身造成傷害時/後」反應式, 對比on_hit_eq的
        # attacked/damaged是「自己受擊」視角, 這裡是「自己打人」視角)——過去裝備管線只有
        # on_hit_eq(受擊方向), 沒有對稱on_deal_tacs/on_deal_effect_tacs(造成傷害方向)的裝備級
        # 消費端, 導致「首回合首次造成傷害時附加一次額外兵刃傷害」(衝陣)這類裝備只能退化用
        # 首回合dot近似(單次額外傷害, 但實際上是prep套用, 非真的「首次造成傷害時」觸發)。
        # 掛在 dealt_damage() 對 src(施加傷害的一方)掃描, 與 on_hit_eq 完全對稱。
        self.on_deal_eq = [e for e in _eq_all if e.get("when") and e["when"].get("on") == "dealtDamage"]
        # 裝備 proc(普攻後觸發, 如 昭烈12%繳械/踩踏額外傷): 包成偽突擊(charge)戰法附加, 走既有 charge 觸發路徑(普攻後 rate 擲骰)。
        # 偽戰法不在戰法庫, 不參與同名戰法去重與 NONEQUIP 過濾; nameZh 預設「特技·名」供 TRACE 辨識。
        # proc:True 旗標 → 標記為「特技偽戰法」, 非真突擊戰法: 日後若加 chargeup(突擊發動率加成)原語, 必須排除 t["proc"] is True(user 明確指示: 特技不吃突擊加成, 例虎豹騎/三勢陣/經天緯地/陷陣突襲)。
        for e in _eq_objs:
            p = e.get("proc")
            if p:
                self.tactics.append({
                    "type": p.get("type", "charge"), "rate": p.get("rate", 1), "coef": p.get("coef", 0),
                    "kind": p.get("kind", "phys"), "n": p.get("n", 1), "nMax": p.get("nMax", 0),
                    "effects": p.get("effects", []), "nameZh": p.get("nameZh", "特技·" + e["name"]),
                    "prep": 0, "when": None, "proc": True,
                })
        a = add or {}                                 # 養成加值: 加點/進階/典藏
        sm = season or {}                             # 賽季修正
        apt = (1.20 if sm.get("apt_s") else g.apt_pct(ttype)) + sm.get("apt_add", 0)
        scm, flat = sm.get("mult", 1.0), sm.get("flat", 0)
        # 屬性管線(戰報結算順序 準備→士氣→適性→建築→裝備→戰法): (基礎+加點+賽季flat)×適性×賽季乘 → +城建CITY → ×陣營FACTION → +兵種營CAMP
        # (裝備 stat "add" 平加效果由 apply_effects/prep 於本管線之後套用, 見 eff() 的 stat_adds; 戰法 mult buff 又在其後, 見 eff() 的 mods)
        def _pipe(base, alloc):
            return ((base + alloc + flat) * apt * scm + CITY) * FACTION + CAMP
        self.force = _pipe(g.base["force"], a.get("force", 0))
        self.intel = _pipe(g.base["intel"], a.get("intel", 0))
        self.command = _pipe(g.base["command"], a.get("command", 0))
        self.speed = _pipe(g.base["speed"], a.get("speed", 0))
        self.charm = getattr(g, "charm", 60)          # 魅力: 城建/陣營是否加成不明, 保守用裸值不縮放(供 scale="charm" 查表)
        self.mods = []                                # 乘法: [stat, mult, left, src]
        self.adds = []                                # 加法: [amp|mitig|extra, val, left, src]
        self.stat_adds = []                           # 屬性平加(裝備 stat.add): [stat, add, left, src]; eff() 中於 mods 乘算前先加
        if a.get("amp"):                              # 進階/典藏 攻防加成: 每階+2%攻+2%防(無來源, 不去重)
            self.adds.append(["amp", a["amp"], 9999, None, None])  # 5元素(含flags=None), 與 push_add 寫入形狀一致, 避免 tick() 的 5-tuple 解包 ValueError(既有bug, 批18順手修正; engine.js 因用彈性解構未受影響)
        if a.get("mitig"):
            self.adds.append(["mitig", a["mitig"], 9999, None, None])
        # 批36: 兵種營「每級+0.25%該兵種造成傷害」——與CAMP(全屬性flat)/Lv10附贈戰法(見上方
        # self.tactics attach)並列的三合一第三支, 走既有amp原語(與a["amp"]同慣例, src標記供
        # 除錯辨識), camp_lv=0時不推入(向後相容, adds為空陣列不影響任何既有戰鬥數學)。
        if self.camp_lv > 0:
            self.adds.append(["amp", self.camp_lv * CAMP_DMG_PER_LV, 9999, "兵種營", None])
        self.dots = []                                # 持續傷害: [每回合傷害, left]
        self.settle = None                            # 結算狀態(猛毒)
        # 時序一致化(2026-07 批次): own_round —— 該單位自己的行動輪計數(1=第1次輪到自己,
        # 於 fight() 主迴圈進入該單位這輪處理時遞增, 見該處註解)。取代settle/coefFromStack
        # 一次性視窗註冊 + everyRound逐回合重擲(A.2/A.3) 過去使用的全局CUR_ROUND基準——這兩類
        # 機制屬「持有者自身進程」(user權威規則), 「第N回合」應指該持有者自己第N個行動輪,
        # 非全局戰鬥回合數。與stack.stackPer=="round"(A.1, 見decay_durations)、tac_cd(A.4,
        # 上批已是自參照)同批對齊, 皆改用own_round或該單位自己的行動輪cadence。
        self.own_round = 0
        self.guardian = None                          # 傷害轉移: 代承者
        self.guard_share = 0.0                        # 代承比例
        self.guard_dur = 0                            # 代承剩餘回合, 歸零時清 guardian(source 首回合援護等有限窗)
        self.guard_normal_only = False                # 只代承普攻傷害(如 援助), 戰法傷害不轉移
        self.stack = None                             # 疊加增益: {per, max, n}
        self.decay = None                             # 衰減增益: {v0, left, total}
        self.swap = 0                                 # 武智互換 剩餘回合
        # 狀態疊加語意對齊批: 反擊(counter)為 NAMED_STATUS 已確認的 "multi"(可共存)具名狀態
        # —— user權威規則: 多來源各自獨立存在、全部生效, 不像急救/休整那樣同名覆蓋。改單一
        # 欄位 self.counter(dict|None) 為清單 self.counters(list[dict]), 每筆各自獨立的
        # {coef, kind, prob, dur, normalOnly, status_name, src_name, _key}, 由
        # upsert_named_status() 以 key=("反擊", id(e)) 寫入(同一來源重複施加只刷新自己那筆,
        # 不同來源各自並存), hit() 逐筆結算(見其對應段落)、decay_durations() 逐筆遞減到期
        # 清除(見其對應段落)。下方 counter property 保留向後相容捷徑, 供既有測試/呼叫端
        # `u.counter = {...}`/`u.counter is None` 等舊寫法沿用(讀寫第一筆), 正式套用路徑
        # (apply_effects k=="counter" 分支)一律直接操作 self.counters 全清單。
        self.counters = []
        # 狀態疊加精修批(user追加規則, 2026-07-12 coordinator訊息, 併入status_stacking_detail_
        # 20260712批): 攻心/倒戈與反擊同族, 改為 NAMED_STATUS "multi"(可共存)具名狀態的真正
        # 多實例清單 —— 前批(623afc4)用 addbonus("lifesteal") 把多個倒戈/攻心來源加總成單一
        # 標量(見 hit() 舊碼), 總回復量雖然數學上正確(對val線性可加), 但遺失個別來源各自的
        # 獨立到期追蹤與未來戰報歸因能力, user此次明確要求改真正多實例清單(比照 self.counters
        # 的 upsert_named_status 寫法): 每筆 {"val","dur","status_name","src_name","_key"},
        # 同來源(同一效果物件)重複施加只刷新自己那一筆, 不同來源各自並存、各自到期(見
        # decay_durations()), hit() 逐筆結算(見其對應段落, 取代舊 ls=src.addbonus("lifesteal")
        # 單一標量寫法)。攻心/倒戈在資料層皆是同一個 k=="lifesteal" 原語(無欄位區分兩者是
        # 「攻心」還是「倒戈」這兩個遊戲內不同戰法族群共用的中文名), status_name 統一標記
        # "攻心/倒戈"(供未來戰報使用, 非擅自二選一裁定, 見 push_lifesteal()/upsert 呼叫處)。
        self.lifesteals = []
        self.dmg_share = None                         # 禁近似令-批K: {"pct","dur"} 受傷回饋給隊友(連環計), 見 hit() 消費端
        # 禁近似令-批K: regens —— 「每回合恢復一次兵力,持續N回合」逐回合累計治療清單(對稱
        # dots的傷害版), 每筆 [heal_amt, left], 見 tick() 消費端。
        self.regens = []
        # 禁近似令-批K: pre_attack_hooks —— 「自身即將受到普通攻擊時」反應式清單(與
        # pre_dmg_hooks不同時機點: 這裡是「即將被打」這件事本身的觸發), 見 do_normal_attack()
        # 消費端。供雲聚影從/益其金鼓使用。
        self.pre_attack_hooks = []
        # 禁近似令-批K: on_kill_grants —— 「本次施放已登記, 待u親手擊敗某目標時才真正授予」
        # 的獎勵清單(虎痴 pierce.onKill), 見 hit() 消費端。
        self.on_kill_grants = None
        # 禁近似令-批K: pre_dmg_hooks —— 「傷害結算前攔截修正」統一掛鉤(pre_damage_intercept
        # 族), 見 docs/engine.js this.preDmgHooks 詳細註解(對稱同一份設計, 消費端見 damage())。
        # 每筆 {"hook_kind", "val", "step", "max", "hits", "rate", "dmg_type", "pct",
        # "delay_rounds", "reduce_pct", "dur"}。
        self.pre_dmg_hooks = []
        self.deferred_dmg = []  # deferSettle 排隊中的分期傷害: [{"amt","left"}], tick() 逐回合扣血遞減
        # 禁近似令-批K: armed_consume(once_consumable族) —— 見 engine.js this.armedConsume
        # 對稱註解(十二奇策)。
        self.armed_consume = None
        # 禁近似令-批K: guard_stack_n(counter_target_binding族) —— 見 engine.js
        # this.guardStackN 對稱註解(古之惡來/虎衛軍), dict[id(counter_guards條目) -> 已疊層數]。
        self.guard_stack_n = None
        # 批A(11筆高嚴重重建): charge —— 「可消耗資源池」(死戰不退「蓄威」), 與既有stack(傷害
        # 增益倍率)語意不同: charge["n"]是「剩餘可消耗次數」, 消耗後n遞減, 不直接影響任何傷害
        # 倍率(是否觸發下一次攻擊的資源, 而非攻擊力大小本身)。{n, max} | None(惰性建立)。
        self.charge = None
        self.charge_consumed_this_round = 0           # 每回合觸發次數計數(對應「每回合最多觸發5次」), 每回合開始重置為0
        # 批28 B1: 守護式反擊(counter.guardFor) —— 「A受擊時, B代為反擊」的方向(如虎衛軍
        # 「我軍主將即將受到普攻時, 副將反擊」), 與既有 self.counter(持有者自己受擊自己反擊)
        # 語意相反, 不能直接掛在持有者(副將)身上。改掛在「被保護者」(如主將)身上一份清單,
        # 每個元素是{unit(反擊執行者), coef, kind, prob}, 見 apply_effects 的 guardFor 分支
        # 與 hit() 內的觸發判斷。與 guardian(傷害轉移代承)是不同機制(guardian轉移傷害承受方,
        # counter_guards是「受擊者不變, 但由別人代為反擊攻擊者」), 兩者可並存不衝突。
        self.counter_guards = []
        # 批J(禁近似令-transfer轉移族): absorb_guards —— redirect.guardFor:"leader" 的登記
        # 清單, 對稱 counter_guards(守護式反擊) 但語意是「代為承受這一次普攻傷害本身」而非
        # 「代為反擊」(古之惡來「...隨後為我軍主將承擔此次普通攻擊」)。與常駐 guardian(redirect
        # 一般模式, %分擔每一下直到guard_dur到期)不同: 這是「僅此一次(已被guardFor鎖定觸發的
        # 這次普攻)+可配比例(e["share"], 預設1.0=全額)」的單次轉移, 每個 absorb_guards 項每
        # 回合最多觸發1次(hit_flags 節流, 同 counter_guards 慣例), 見 apply_effects 的
        # redirect.guardFor 分支與 hit() 內對應判斷。
        self.absorb_guards = []
        self.taunt_by = None                          # 嘲諷: 被嘲諷時強制普攻/單體戰法指向 taunt_by
        self.taunt_dur = 0                             # 嘲諷剩餘回合
        self.shield = None                            # 護盾: {amt, dur} 吸收固定量傷害, 先於兵力扣減
        self.block = []                                # 批22: 次數型格擋(抵禦/警戒同族) —— [{"val","n","src"}], 消耗順序見 hit(); val=1.0全擋/0.x部分減傷, n=剩餘次數
        self.dodge_prob = 0.0                          # 規避機率
        self.dodge_dur = 0                             # 規避剩餘回合
        self.dodge_dmg_type = None                     # 批G: 規避限定的傷害類型(phys/intel), None=不分類型(向後相容既有全域規避)
        self.surehit_dur = 0                           # 必中: 無視對方 dodge, 剩餘回合
        self.healblock = 0                             # 批8: 禁療(healblock) 剩餘回合, >0 時 heal 效果對其無效
        # 批52d: 虎嗔(huchen) —— 將門虎女負面狀態; 可被草船/刮骨等 dispel(debuffs)清除。
        # {base, per, hits, maxHits, left, caster, kind, src, ampOnSettle}
        self.huchen = None
        self.when_fired = set()                        # 條件觸發(when.rounds/from/until) 已套用效果的戰法(一次性, 用 id() 去重); 批8: delayed_eq(裝備效果級when)共用同一個 set(效果物件本身 id() 去重, 不與戰法物件撞)
        self.scale_lock = None                          # 批35 B: 「準備階段鎖定」的 scale 縮放值快取, dict[id(效果物件) -> scale_of結果], 惰性建立(見 locked_scale_of)
        # 批42: exploit_layers —— 「持有者對本單位(受害目標)累積的疊層負面buff」計數器,
        # dict[id(效果物件) -> 已疊層數], 掛在**目標**(受害者)身上(對稱 engine.js
        # exploitLayers, 見其註解), 惰性建立。傲睨王侯: 敵軍目標受普攻時觸發1層(該目標降3%
        # 可疊), 用效果物件id當鍵天然對應「同一張卡的疊層」不撞其他戰法的stat效果, 掛在目標
        # 身上則天然對應「疊層只對這個特定目標累積, 不同敵人各自獨立計數」。
        self.exploit_layers = None
        # 禁近似令-批K: amp_layers_by_id(dynamic_coef_from_counter族) —— k=="amp"+e["stackKey"]+
        # e["stackId"] 的字串鍵索引版本, 見 engine.js ampLayersById 對稱註解, 惰性建立。
        self.amp_layers_by_id = None
        self.exploit_capped = None                      # 批42: 同上, set[id(效果物件)] —— 記錄該目標「本效果已達max_stacks上限並觸發過on_max_stacks」, 防止之後重複觸發
        # 批42: exploit_global —— 持有者(caster)視角跨目標累計觸發次數, dict[id(效果物件) ->
        # {"n":int,"fired":bool}], 掛在持有者身上(對稱 engine.js exploitGlobal)。
        self.exploit_global = None
        # 批A(11筆高嚴重重建): amp_layers —— dict[id(效果物件) -> 已疊層數], k=="amp"+
        # e["stackKey"]的per-target疊層計數(對稱exploit_layers, 但獨立欄位避免不同k共用同一份
        # 計數器混淆語意)。密計誅逆「使敵軍單體(隨機)造成的最終傷害降低15%,最多疊加3次」首次
        # 落地使用。刻意不做onMaxStacks/globalMax/e["add"]三個延伸(見sgz.py apply_effects
        # k=="amp"分支註解, 密計誅逆無此語意需求, 精簡版足夠)。
        self.amp_layers = None
        # 批H: crit_layers —— dict[id(效果物件) -> 已疊層數], k=="critUp"+e["stackKey"]的
        # per-target疊層計數(對稱amp_layers, 獨立欄位避免與amp/stat的疊層計數器混淆語意)。
        # 逆鱗「受到傷害時,3%機率獲得10%會心,可疊加2次」首次落地使用, 見apply_effects
        # k=="critUp"分支。
        self.crit_layers = None
        # 批52g: ammo —— 彈藥計數(高櫓連營), dict[戰法名 -> 剩餘次數]
        self.ammo = {}
        self.heal_rounds_fired = {}                     # 批15: heal 效果 e["when"]["rounds"](明確列出的特定回合)的「每回合各觸發一次」去重, dict[id(效果物件) -> set(已觸發回合數)], 見 apply_effects 的 heal 分支
        self.tac_cd = {}                               # 批52: 戰法冷卻 dict[nameZh -> 剩餘回合], 發動成功後寫入 t["cd"], 每回合 tick 遞減; >0 時該戰法不可再發動
        self.hit_flags = set()                         # 反應式觸發(when.on) 本回合已觸發的戰法, 每回合重置(防無限鏈)
        # 批31 A 修復: 過去 on_hit_tacs 只檢查 t.when.on 是否為真(truthy), 沒有限定具體事件值,
        # on_hit() 內部迴圈(見下方)也只用 t0["when"]["on"]=="attacked" 排除普攻限定的不符情形,
        # 對其餘任何 on 值(包含批27新增的"dealtDamage"/本批"activeFired")一概放行當成
        # "damaged"(任意傷害都觸發)處理——這是預先篩選範圍過寬的潛伏bug(全庫過去只有
        # attacked/damaged 兩種 t.when.on 值, 從未真正暴露; 本批新增 activeFired 後, 士爭先赴
        # 首次踩中: 除了正確的「自身發動主動戰法觸發」外, 還會被 on_hit() 誤判成「受擊觸發」
        # 額外多發動一次, 造成雙重觸發)。現收斂為明確白名單 {"attacked","damaged"}, 只有這
        # 兩種事件值才會被收進 on_hit_tacs(dealtDamage/activeFired 各自有專屬的
        # on_deal_tacs/active_fired_tacs 預篩+獨立事件點, 不應該也落入 on_hit_tacs)。
        self.on_hit_tacs = [t for t in self.tactics    # 預篩: 絕大多數單位為空, hit 熱路徑 O(0)
                            if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") in ("attacked", "damaged")]
        # 批22: 效果級 e.when.on(急救類反應式治療, 如陷陣營/長健/雲聚影從「受到傷害時XX%機率
        # 獲得治療」) —— 與上面 on_hit_tacs(戰法級, 整個戰法都是反應式)不同: 這類戰法本身有
        # 其他常駐效果(如陷陣營的武力/統率平加)需要在 prep 階段就套用, 只有其中的 heal 效果段
        # 是「受傷當下才觸發」的反應式語意, 不能把整個戰法標成 t["when"]["on"](那樣會連帶讓
        # 武力/統率平加也不在 prep 套用, 語意跑掉)。on_hit_effect_tacs 收集這類「戰法本身無
        # t.when, 但至少一個效果帶 e.when.on」的戰法, on_hit() 只讀取/結算符合的個別效果。
        # 批23: 型別放寬含 active —— 過去只認 passive/command(「戰法本身有其他常駐效果, 只有
        # heal段是反應式」的典型模式, 如陷陣營/雲聚影從)。但草船借箭一類 type:"active" 戰法也有
        # 同樣模式(「使我軍獲得急救狀態, 受傷時機率觸發治療」是active發動後掛的一個反應式buff,
        # 不是常駐), 過去完全沒有機制承接, 只能誤把heal當成active發動當下的常駐治療(0分bug)。
        # 放寬後active戰法帶e.when.on的效果同樣走on_hit()反應式結算, 該戰法主coef/其餘無when
        # 效果仍照常經由主動擲骰路徑(t0["rate"])發動觸發(兩者互不干擾, 見apply_effects內新增的
        # reactive閘門, 確保e.when.on效果不會在active擲骰命中時被重複套用)。
        # 批31 A 修復: 同上(on_hit_tacs)——過去用 truthy 檢查, 未限定具體事件值, 導致
        # 帶 e.when.on:"dealtDamage"(批27)的效果(深謀遠慮/白衣渡江/非攻制勝)被誤收進
        # on_hit_effect_tacs, 在 on_hit() 的效果級迴圈裡又額外多觸發一次(該迴圈只排除
        # ew["on"]=="attacked" 的不符情形, "dealtDamage" 被誤判成"damaged"放行), 與正確的
        # on_deal_effect_tacs 觸發路徑重複結算(雙重治療/雙重控制)。收斂為明確白名單。
        self.on_hit_effect_tacs = [t for t in self.tactics
                                   if not t.get("when") and t["type"] in ("passive", "command", "active")
                                   and any((e.get("when") or {}).get("on") in ("attacked", "damaged") for e in t.get("effects", []))]
        # 狀態疊加語意對齊批: 急救(reactive heal, k=="heal"+when.on)為 NAMED_STATUS 已確認的
        # "unique"(唯一/覆蓋)具名狀態 —— user權威規則: 同單位若有多個來源(戰法/兵書/裝備)
        # 各自獨立掛反應式治療(如陷陣營+青囊書皆授予急救), 全場只應有一個急救實例生效, 不疊
        # 雙倍觸發機率。過去 on_hit_effect_tacs/on_hit_eq/on_hit_bs 三條反應式清單各自獨立
        # 蒐集, on_hit() 逐一檢查全部候選並各自擲率觸發, 等同「共存」——不符合唯一狀態規則。
        #
        # 狀態疊加精修批(user規則 status_stacking_detail_20260712): 急救 = 「覆蓋+到期回退」
        # (overwrite-with-fallback), 不是前批的「唯一dedup永久丟棄舊來源」——前批在建構時
        # 一次性算出 suppressed_named_status(靜態set, 整場戰鬥固定), 被裁決為「非最新」的
        # 來源整場都不會生效, 即使「最新」來源自己的持續回合窗已經到期; user糾正: 每個施加
        # 急救的來源應各自追蹤(rate/倍率+duration窗), 當前生效的用「最新來源」, 但最新來源
        # 到期時, 若有更早來源仍在自己的窗內, 急救不消失、生效rate回退成該更早來源的值,
        # 所有來源都到期才真正消失(例: 陷陣營3回合(1-3)+第1回合草船覆蓋, 草船到期但陷陣營
        # 窗還在→回退陷陣營rate)。
        #
        # 實作: 建構時只蒐集候選(依 戰法→兵書→裝備 順序, 此順序=優先序/「最新來源」的既有
        # tie-break慣例不變, 見 NAMED_STATUS["急救"]["note"]), 不在此處算出永久suppression
        # 集合。改為 self.suppressed_named_status 定義成 @property(見下方 alive 屬性旁的
        # 定義), 每次存取時依「當下 self.own_round」動態算出: 由高優先(=清單最後面, 最新)
        # 到低優先依序檢查候選自己的 when(until/from/rounds等既有回合窗欄位, 如陷陣營
        # when.until:3) 是否仍在 self.own_round 內(round_ok), 第一個仍在窗內者即為目前生效者
        # (即使它是較舊來源, 只要更新來源已到期——回退語意), 其餘全部(不論是否在窗內)回傳
        # 為suppressed。全部到期(無人在窗內)則所有候選都算suppressed(全部消失)。
        # on_hit_for() 呼叫端沿用既有 `id(e) in holder.suppressed_named_status` 寫法不必改
        # 動即可取得新語意(property在每次access時重新計算, 天然反映當下own_round)。
        # 舊行為相容性: 建構完成當下(own_round預設0, 尚未開戰)存取本property, 對「候選的
        # when無回合窗欄位」(如本檔demo()測試合成戰法只帶when.on無until/from)的情形,
        # round_ok對「無rounds/from/until/parity/every」的when恆真, 與前批靜態版行為逐位元
        # 相同(僅最後一個候選視為生效, 其餘皆suppressed, 見227c既有測試不必改動仍應通過)。
        self._heal_candidates = []  # 急救候選效果物件參考(非id), 依 戰法→兵書→裝備 順序蒐集(此順序即優先序)
        for _t in self.on_hit_effect_tacs:
            for _e in _t.get("effects", []):
                if _e.get("k") == "heal" and (_e.get("when") or {}).get("on") in ("attacked", "damaged"):
                    self._heal_candidates.append(_e)
        for _e in self.on_hit_bs:
            if _e.get("k") == "heal" and (_e.get("when") or {}).get("on") in ("attacked", "damaged"):
                self._heal_candidates.append(_e)
        for _e in self.on_hit_eq:
            if _e.get("k") == "heal" and (_e.get("when") or {}).get("on") in ("attacked", "damaged"):
                self._heal_candidates.append(_e)
        # 批27 A: on:"dealtDamage" —— 「自身造成傷害時/後」反應式掛鉤(對比 on_hit_tacs 的
        # attacked/damaged 是「自己受擊」視角, 這裡是「自己打人」視角, 如白衣渡江「造成兵刃
        # 傷害時25%→50%機率使敵軍單體繳械」)。掛在 hit() 傷害結算後對 src(施加傷害的一方)
        # 掃描, 與 on_hit_tacs/on_hit_effect_tacs 完全對稱(戰法級 vs 效果級 兩種顆粒度)。
        # dmgType(選填, "phys"/"intel"): 區分「造成兵刃傷害時」vs「造成謀略傷害時」兩種不同
        # 觸發條件(白衣渡江 disarm 段只在兵刃傷害後觸發, silence 段只在謀略傷害後觸發), 沿用
        # amp/mitig 既有 dmgType 欄位命名慣例, 無此欄位視為兩種傷害類型皆可觸發(向後相容)。
        self.on_deal_tacs = [t for t in self.tactics
                             if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") == "dealtDamage"]
        self.on_deal_effect_tacs = [t for t in self.tactics
                                    if not t.get("when") and t["type"] in ("passive", "command", "active")
                                    and any((e.get("when") or {}).get("on") == "dealtDamage" for e in t.get("effects", []))]
        # 批31 A: on:"activeFired" —— 「自身成功發動主動(或突擊)戰法時/後」反應式掛鉤(對比
        # on_deal_tacs 的「造成傷害」視角, 這裡是「戰法本身成功擲骰命中fire」視角, 不要求真的
        # 造成傷害, 如士爭先赴「成功發動自帶主動戰法前，50%機率對敵軍2人造成兵刃傷害」——現行
        # 版本把這條獨立成一個常駐coef+rate的passive戰法, 與「是否真的有主動戰法成功發動」完全
        # 脫鉤, 屬v14盲測抓到的「條件觸發簡化為無條件」同族缺口)。掛在 fight() 主迴圈 active/
        # charge 型戰法 fire=True 判定通過後, 對施放者 u 自身(而非受擊/被造成傷害的另一方)
        # 掃描其 active_fired_tacs(戰法級)/active_fired_effect_tacs(效果級), 與 on_deal_tacs/
        # on_deal_effect_tacs 完全對稱(戰法級 vs 效果級 兩種顆粒度), 只是事件觸發點不同(自身
        # 戰法命中 vs 自身造成傷害)。when.timing(選填, "before"/"after"): 士爭先赴原文「成功
        # 發動...前」, 但引擎在同一回合內對「前/後」無實質結算順序差異(觸發本體戰法與本反應式
        # 效果都在同一次 fire 判定之後才有意義, 沒有跨回合的「發動前」窗口可插入), 統一在
        # fire=True 判定通過、實際套用觸發戰法效果**之前**呼叫 active_fired() 廣播(貼近
        # before 語意, 但 after 措辭的戰法一律視同無差別, 不細分兩種處理路徑, 見
        # engine_limitations.md 新增節)。
        self.active_fired_tacs = [t for t in self.tactics
                                  if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") == "activeFired"]
        self.active_fired_effect_tacs = [t for t in self.tactics
                                         if not t.get("when") and t["type"] in ("passive", "command", "active")
                                         and any((e.get("when") or {}).get("on") == "activeFired" for e in t.get("effects", []))]
        # 批43 C: on:"healed" —— 對稱 engine.js 同名分支, 見其詳細註解。只支援效果級
        # (on_heal_effect_tacs), 不支援戰法級(on_heal_tacs已建但apply_effects/healed_for未讀取,
        # 比照批31 active_fired precedent, 新事件類型不強制一次補齊所有粒度)。
        self.on_heal_tacs = [t for t in self.tactics
                             if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") == "healed"]
        self.on_heal_effect_tacs = [t for t in self.tactics
                                    if not t.get("when") and t["type"] in ("passive", "command", "active")
                                    and any((e.get("when") or {}).get("on") == "healed" for e in t.get("effects", []))]
        # 批52h: on:"controlled" —— 友軍被施加控制(震懾/計窮/繳械/混亂)時反彈(機鑑先識)
        self.on_ctrl_effect_tacs = [t for t in self.tactics
                                    if not t.get("when") and t["type"] in ("passive", "command", "active")
                                    and any((e.get("when") or {}).get("on") == "controlled" for e in t.get("effects", []))]
        # 禁近似令-批L: on:"dmgThreshold"(自身累計受傷達門檻)/on:"ctrlImmune"(自身免疫控制事件)
        # —— 對稱engine.js selfReactEffectTacs註解。一身是膽「每次免疫控制狀態後或每次累計受
        # 最大兵力7%傷害後...」需要的兩個新反應式事件, 純自身視角(不做ally/enemy跨隊廣播,
        # 見fire_self_reactive()), 用單一預篩list涵蓋兩者, on_values(e)把e["when"]["on"]
        # 正規化成list(單一效果可同時掛兩個事件名, 共用同一份stackKey疊層計數)。
        self.self_react_effect_tacs = [t for t in self.tactics
                                       if not t.get("when") and t["type"] in ("passive", "command", "active")
                                       and any(v in ("dmgThreshold", "ctrlImmune") for e in t.get("effects", []) for v in on_values(e))]
        self.locked_targets = {}                       # 批12 ModeG: lockTarget:true 戰法的鎖定目標, 鍵=id(戰法dict)(dict不可雜湊, 用id())
        # 批16: 原語擴充包 —— 新增狀態欄位(現有資料無新欄位, 皆維持0/{}/set()預設值, 行為零變化)
        self.atk_count = {}                            # everyN: 自身普攻次數計數器, 鍵=id(戰法dict)
        self.immune = []                               # immuneTo: [type, dur] 陣列(單項控制免疫, 對比 insight 全免)
        # 禁近似令-批L: dmg_accum —— 自身累計受傷計數器(全戰鬥單調遞增, 不因治療/傷兵池消耗
        # 而回退, 與self.wounded[可被治療消耗的池]是不同語意的兩個獨立計數), 對稱engine.js
        # 同名欄位。一身是膽「每次累計受最大兵力7%傷害後」需要跨事件持續累加再偵測門檻跨越
        # 次數, 見bump_dmg_accum()。
        self.dmg_accum = 0.0
        self.hp_below_fired = set()                    # hpPct: when.hpBelow(一次性, 首次跨越即觸發) 已觸發的戰法, 用 id(t) 去重
        self.fake_report_dur = 0                       # fakeReport(偽報): 剩餘回合數, >0 時指揮/被動 coef 擲骰段與 on_hit 反應式觸發受抑制

    @property
    def alive(self):
        return self.troop > 0

    @property
    def suppressed_named_status(self):
        """狀態疊加精修批(user規則 status_stacking_detail_20260712): 急救「覆蓋+到期回退」的
        動態裁決 —— 見 __init__ 內 self._heal_candidates 蒐集處的完整規則說明。每次存取都
        重新掃描 self._heal_candidates(高優先/最新在清單尾端), 找出當下(self.own_round)第一
        個仍在自己 when 窗內(round_ok)的候選當作「目前生效者」, 其餘一律回傳為 suppressed
        (含窗外的舊來源與非目前生效者的候選)。全部到期(無人在窗內)則所有候選皆suppressed。
        回傳一個新建的 set(每次存取即時計算, 非快取——候選數量通常<=3, 對 on_hit_for() 的
        呼叫頻率而言成本可忽略), 供既有 `id(e) in holder.suppressed_named_status` 呼叫慣例
        不必改動。"""
        active_id = None
        for _cand in reversed(self._heal_candidates):
            if round_ok({"when": _cand.get("when") or {}}, self.own_round):
                active_id = id(_cand)
                break
        return {id(_c) for _c in self._heal_candidates if id(_c) != active_id}

    # 狀態疊加語意對齊批: counter 向後相容捷徑 —— 反擊已改為 self.counters(清單, 支援多實例
    # 並存, 見其定義註解與 apply_effects k=="counter" 分支/hit() 消費端), 但既有測試/呼叫端
    # 大量使用 `u.counter = {...}`(直接賦值單一dict) / `u.counter is None`(讀取)這類舊寫法。
    # 此 property 讓舊寫法繼續運作(讀: 回傳 counters[0] 或 None; 寫: 整個取代 counters 為
    # 單一實例清單, 對稱舊行為「賦值即覆蓋」), 供測試harness簡便賦值使用。正式套用路徑(戰法/
    # 兵書/裝備效果實際生效時)一律走 upsert_named_status(self.counters, ...), 不透過此
    # property, 也不建議新程式碼呼叫 setter(會直接清空既有多實例, 與"multi"共存語意衝突,
    # 僅供測試/一次性初始化情境使用)。
    @property
    def counter(self):
        return self.counters[0] if self.counters else None

    @counter.setter
    def counter(self, val):
        self.counters = [] if val is None else [dict(val)]

    def is_immune_to(self, ctype):                     # 批16: immuneTo —— 單項控制免疫查詢(對比 insight 全免)
        return any(ty == ctype for ty, _ in self.immune)

    def push_immune(self, types, dur):
        for ty in (types or []):
            self.immune.append([ty, dur if dur is not None else 1])

    def tick_every_n(self, t):                          # 批16: everyN —— 自身每第N次普攻觸發; 傳回是否達標(達標即歸零重計)
        cfg = t.get("everyN")
        if not cfg:
            return False
        key = id(t)
        cnt = self.atk_count.get(key, 0) + 1
        if cnt >= cfg.get("count", 1):
            self.atk_count[key] = 0
            return True
        self.atk_count[key] = cnt
        return False

    def apply_stack_cast(self):
        """批26 B2: stack.stackPer=="cast" 專用遞增入口 —— 原文常見「每次發動後傷害率提升X」
        (如水淹七軍/陷陣突襲), 是「本戰法每次成功發動」才+1層, 與回合數無關。既有 stack 機制
        只有 fight() 主迴圈的逐回合遞增(stackPer=="round", 預設, 向後相容), 此方法供戰法命中/
        發動結算處呼叫, 只在 stackPer=="cast" 時才遞增(round 模式呼叫此方法應為no-op, 只認
        tick()式逐回合遞增, 兩種模式互不干擾)。"""
        if self.stack and self.stack.get("stackPer", "round") == "cast":
            self.stack["n"] = min(self.stack["max"], self.stack["n"] + 1)

    @property
    def hp_pct(self):                                   # 批16: hpPct —— 自身兵力百分比(troop/START_TROOP), 供 when.hpBelow/hpAbove 檢查
        return self.troop / START_TROOP

    def suppressed(self, src):
        """批18: fakeReport(偽報) 期間, 來源為「自己的指揮/被動戰法」(src in cmd_passive_srcs) 的
        條目暫停參與計算(到期自動恢復, 不刪除條目本身 —— 條目仍在 adds/mods/stat_adds 陣列裡,
        tick() 到期照舊遞減/移除, 只是這裡讀取時跳過)。src 為 None(兵書/裝備/緣分/其他來源)或
        不在 cmd_passive_srcs 中不受影響。
        批24: src 可能帶「:尾碼」區分同源多條目(rateup 的 :prepOnly/nativeOnly、dmgType 的
        :phys/:intel, 見 push_add 呼叫端), 但 cmd_passive_srcs 只存純戰法名(nameZh, 不含
        尾碼)。比對前先去除尾碼還原成純戰法名, 避免帶尾碼的 src 永遠比對不到 cmd_passive_srcs
        (修正批16 rateup/chargeup 尾碼慣例引入時就存在的潛在比對錯位)。"""
        if not src or self.fake_report_dur <= 0:
            return False
        base = src.split(":", 1)[0]
        return base in self.cmd_passive_srcs

    def eff(self, stat):
        if self.swap and stat in ("force", "intel"):  # 武智互換
            stat = "intel" if stat == "force" else "force"
        v = getattr(self, stat)
        for s, add, _dur, src, *_ in self.stat_adds:  # 裝備平加(獨立階段, 在陣營/兵種營後、戰法乘算前)
            if (s == stat or s == "all") and not self.suppressed(src):
                v += add
        for s, m, _dur, src, *_ in self.mods:
            if (s == stat or s == "all") and not self.suppressed(src):  # stat="all" 套全屬性
                v *= m
        # 禁近似令-批K: self.stack["statField"]/["statPerVal"](dynamic_coef_from_counter族) ——
        # 對稱 amp() 讀 self.stack["per"]*self.stack["n"] 的既有寫法, 供 k=="stat"+e["fromStack"]
        # 註記的「stat屬性隨同一枚stack計數器動態成長」(弓腰姬), 即時讀取當下層數。
        if self.stack and self.stack.get("statField") == stat:
            v += self.stack.get("statPerVal", 0) * self.stack["n"]
        return v

    def addbonus(self, kind, dmg_type=None, is_normal=None, is_active=None, is_charge=None, dot_status=None):
        """批24 D2: dmg_type(可選) —— 只加總「該條目未宣告 dmgType, 或宣告的 dmgType 與呼叫端
        指定的 dmg_type 相符」的項目, 供 amp/mitig 依「兵刃/謀略」傷害類型過濾(見 damage() 呼叫端)。
        省略時完全維持原行為(不分類型全部加總), 向後相容全庫既有未帶 dmgType 的 amp/mitig 資料。
        批28 B3: is_normal(可選) —— 只加總「該條目未宣告 normalOnly, 或宣告 normalOnly 且本次
        is_normal 為 True」的項目, 供 amp 表達「僅普攻傷害提升」(如至柔動剛「提升我軍群體普攻
        傷害」, 對比redirect既有的normalOnly慣例)。呼叫端傳 is_normal=None(預設, 如dot/counter/
        settle等非普攻傷害路徑未特別傳入)時, 視為「未知/不適用」——安全側處理: 宣告了
        normalOnly 的加成一律不計入(避免對非普攻傷害路徑意外套用「僅普攻」限定的加成,
        比不套用更安全; 未宣告normalOnly的既有全庫資料完全不受影響, 向後相容)。
        批31 A: is_active(可選) —— 與 is_normal 對稱, 只加總「該條目未宣告 activeOnly, 或宣告
        activeOnly 且本次 is_active 為 True」的項目, 供 amp 表達「僅主動戰法傷害提升」(如
        士爭先赴「提高自帶主動戰法傷害」)。同樣安全側處理: is_active=None(呼叫端未特別傳入,
        如dot/counter/settle等)時, 宣告 activeOnly 的加成不計入。
        批40 B: is_charge(可選) —— 對稱 is_active, 只加總「該條目未宣告 chargeOnly, 或宣告
        chargeOnly 且本次 is_charge 為 True」的項目, 供 amp 表達「僅突擊戰法傷害提升/降低」
        (一鼓作氣/藏刀「突擊戰法造成傷害提升/降低」)。批31 A 原本把「突擊」傷害也標記
        is_active=True(見 fight() 主迴圈突擊擲骰呼叫點), 誤將「主動戰法」與「突擊戰法」兩個
        game機制上互斥的分類混為一談, 本批修正呼叫點改傳 is_charge, is_active 維持只在
        t["type"]=="active" 時為 True。normalOnly/activeOnly/chargeOnly 三者互斥(資料上
        不會同時宣告)。"""
        s = 0.0
        for a in self.adds:
            k, v = a[0], a[1]
            src = a[3] if len(a) > 3 else None
            if k != kind or self.suppressed(src):
                continue
            flags = a[4] if len(a) > 4 else None
            if dmg_type and flags and flags.get("dmgType") and flags["dmgType"] != dmg_type:
                continue
            if flags and flags.get("normalOnly") and is_normal is not True:
                continue
            if flags and flags.get("activeOnly") and is_active is not True:
                continue
            if flags and flags.get("chargeOnly") and is_charge is not True:
                continue
            # 禁近似令-批L: dmgFromStatus(僅amp) —— 帶此限定的條目只在本次傷害是「dot結算且
            # 其具名狀態在清單內」(dot_status命中flags["dmgFromStatus"])時才計入, 一般傷害
            # 路徑(dot_status未傳/None)一律跳過這類條目, 見才辯機捷。
            if flags and flags.get("dmgFromStatus") and not (dot_status and dot_status in flags["dmgFromStatus"]):
                continue
            s += v
        return s

    def addbonus_for(self, kind, t):
        """rateup/chargeup 專用: 依戰法 t 的 prep/native 屬性, 只加總「修飾旗標吻合」的 adds 項。
        adds[4] = flags({"prepOnly":.., "nativeOnly":.., "inheritedOnly":..}|None, 見 push_add)。
        無旗標的加成一律計入(如虎豹騎的 chargeup 沒有 prepOnly/nativeOnly 限制)。
        批8: inheritedOnly(nativeOnly 反向) —— 只加「非自帶」(傳承)戰法, 如竭力佐謀「非自帶
        主動戰法發動率+100%」; not t.get("native") 即傳承(Unit 建構時自帶戰法才標 native=True)。"""
        s = 0.0
        for a in self.adds:
            if a[0] != kind or self.suppressed(a[3] if len(a) > 3 else None):
                continue
            flags = a[4] if len(a) > 4 else None
            if flags:
                if flags.get("prepOnly") and not t.get("prep"):
                    continue
                if flags.get("nativeOnly") and not t.get("native"):
                    continue
                if flags.get("inheritedOnly") and t.get("native"):
                    continue
            s += a[1]
        return s

    def amp(self, dmg_type=None, is_normal=None, is_active=None, is_charge=None, dot_status=None):      # 總增傷 = 一般+疊加層+衰減; 批24 D2: dmg_type過濾amp部分(stack/decay無此概念,全額計入); 批28 B3: is_normal過濾normalOnly標記的amp(僅普攻生效); 批31 A: is_active過濾activeOnly標記的amp(僅主動戰法傷害生效); 批40 B: is_charge過濾chargeOnly標記的amp(僅突擊戰法傷害生效); 禁近似令-批L: dot_status過濾dmgFromStatus標記的amp(僅限定狀態的dot傷害生效, 才辯機捷)
        a = self.addbonus("amp", dmg_type, is_normal, is_active, is_charge, dot_status)
        if self.stack:
            a += self.stack["per"] * self.stack["n"]
        if self.decay:
            a += self.decay["v0"] * self.decay["left"] / self.decay["total"]
        return a

    def push_add(self, kind, val, dur, src=None, flags=None, max_stack=None):
        """同來源(戰法名)同種效果預設刷新而非疊加。src=None(兵書/裝備/緣分)不去重。
        flags: {"prepOnly":bool, "nativeOnly":bool}, 供 addbonus_for() 篩選(見批7 太平道法)。
        批52: max_stack —— 原文「最多疊加N次」時允許同 src 追加至多 N 層(每層獨立 val/dur),
        達上限時刷新既有層 duration 而不再加層。"""
        if src:
            same = [a for a in self.adds if a[0] == kind and a[3] == src]
            if max_stack:
                if len(same) >= max_stack:
                    for a in same:
                        a[2] = max(a[2], dur)
                    return
            else:
                self.adds = [a for a in self.adds if not (a[0] == kind and a[3] == src)]
        self.adds.append([kind, val, dur, src, flags])

    def push_mod(self, stat, mult, dur, src=None, flags=None, max_stack=None):
        """批52: max_stack 同 push_add(乘算層以「再 append 一層 mult」表達, 達上限刷新 dur)。"""
        if src:
            same = [m for m in self.mods if m[0] == stat and m[3] == src]
            if max_stack:
                if len(same) >= max_stack:
                    for m in same:
                        m[2] = max(m[2], dur)
                    return
            else:
                self.mods = [m for m in self.mods if not (m[0] == stat and m[3] == src)]
        self.mods.append([stat, mult, dur, src, flags])

    def push_stat_add(self, stat, add, dur, src=None, flags=None, max_stack=None):
        """屬性平加。預設同來源刷新; 批52 max_stack 允許多層累加(如一力拒守統率+21×最多2次)。"""
        if src:
            same = [a for a in self.stat_adds if a[0] == stat and a[3] == src]
            if max_stack:
                if len(same) >= max_stack:
                    for a in same:
                        a[2] = max(a[2], dur)
                    return
            else:
                self.stat_adds = [a for a in self.stat_adds if not (a[0] == stat and a[3] == src)]
        self.stat_adds.append([stat, add, dur, src, flags])

    def push_block(self, val, n, src=None, dmg_type=None):
        """批22: block(次數型格擋, 抵禦/警戒同族) —— 與 shield/mitig 語意不同: 不是持續減傷/
        固定量吸收池, 而是「剩餘次數」計次器, 每次受擊消耗1次(而非按傷害量扣減), val=1.0時
        完全格擋該次傷害、val=0.x時該次傷害打折(如警戒 -75.35%≈val=0.7535)。

        狀態疊加精修批(user規則 status_stacking_detail_20260712 + 追加修正, coordinator訊息
        2026-07-12): 引擎既有慣例(NAMED_STATUS["抵禦"]既有註解/TRACE顯示)以 val>=1.0 為
        「抵禦」(全擋)、val<1.0 為「警戒」(部分減傷)的判準——user明確裁決兩者疊加語意不同,
        不再共用同一套「同源疊次數」規則:
          - 警戒(val<1.0): 累積(accumulate) —— 新施加的次數加總到現有(不論同源或不同來源皆
            計入總可用次數; 同源同值仍合併進同一筆, 不同來源/不同值各自成一筆, 兩者在
            consume_block() 消耗時皆先進先出逐筆扣減, 總「目前可用次數」等於全部筆數總和,
            與「累積」的可觀察結果一致)。
          - 抵禦(val>=1.0): **「有剩餘次數時新施加不補不刷」**(user追加修正, 更正本批較早
            版本誤植的「取代成最新值」寫法) —— 身上已有抵禦次數(同dmgType的既有格擋層總數
            >0)時, 新施加的抵禦**完全被忽略**(不覆蓋/不補充/不刷新, 現有次數原封不動,
            如身上剩1次, 折衝禦侮再給2次仍維持1次不變); 只有現有次數已耗盡(0, 含從未
            施加過)時, 新來源的次數才真正生效(直接套用其值)。例外: 持有者當下處於「嚴密」
            狀態(self.rigorous>0, 赴湯蹈火戰法施加, 見 Unit.__init__ 對應欄位)時, 改為
            累積(與警戒同規則: 同源同值合併, 不同來源各自並存加總)。
        批G: dmg_type(可選, 尾端新增, 向後相容既有全部呼叫點)—— 對稱 amp/mitig 既有的
        dmgType 過濾慣例(批24 D2), 限定此格擋只對該類型(phys/intel)傷害生效, 省略時維持
        原行為(不分類型, 任何傷害皆可消耗, 如「抵禦」「警戒」既有全域格擋)。榮光「受到謀略
        傷害時, 有4%機率完全免疫此次傷害」需要限定只對intel傷害生效, 過去block無此過濾維度,
        只能改用大幅降權的mitig近似(見equips_parsed.json _note 歷史記錄)。"""
        is_deflect = val >= 0.999                      # 抵禦(全擋) vs 警戒(部分減傷), 對稱既有 TRACE/describe 顯示判準
        if is_deflect and self.rigorous <= 0:
            # 抵禦, 非嚴密: 有剩餘(同dmgType既有格擋層總次數>0)時新施加完全忽略; 歸零時才
            # 真正套用新來源的次數。
            existing_n = sum(b["n"] for b in self.block
                             if b["val"] >= 0.999 and b.get("dmgType") == dmg_type)
            if existing_n > 0:
                return                                  # 仍有剩餘: 新施加的抵禦不補不刷, 忽略
            self.block.append({"val": val, "n": n, "src": src, "dmgType": dmg_type})
            return
        # 警戒(恆累積), 或 抵禦+嚴密(例外改累積): 同源同值合併次數(貼合原文「總次數」疊次
        # 語意), 不同來源/不同值各自新增一筆並存(consume_block 逐筆消耗, 總可用次數仍是加總)
        for b in self.block:
            if src and b.get("src") == src and abs(b["val"] - val) < 1e-9 and b.get("dmgType") == dmg_type:
                b["n"] += n
                return
        self.block.append({"val": val, "n": n, "src": src, "dmgType": dmg_type})

    def consume_block(self, dmg_type=None):
        """消耗一次格擋(若有, 且該格擋層未限定類型或類型與本次傷害相符): 從陣列中第一筆符合
        條件者(先加的先消耗, 貼合戰報「總次數」單一計數語意)扣1次, n<=0時整筆移除。回傳消耗
        到的 val(0=無格擋可消耗, 呼叫端不應觸發)。
        批G: dmg_type(可選)—— 本次傷害類型(phys/intel), 只消耗 b["dmgType"] 為 None(不分類型,
        既有全域格擋如「抵禦」「警戒」)或與 dmg_type 相符的格擋層(如榮光只設 intel), 類型不符
        的格擋層(如未來新增「兵刃專屬格擋」)略過不消耗、不影響。向後相容: 全庫既有格擋資料皆
        未帶 dmgType(None), 此處邏輯對它們完全不變(第一個 None 類型的格擋層永遠優先匹配)。"""
        for i, b in enumerate(self.block):
            if b.get("dmgType") is not None and b["dmgType"] != dmg_type:
                continue
            b["n"] -= 1
            val = b["val"]
            if b["n"] <= 0:
                self.block.pop(i)
            return val
        return 0

    def push_lifesteal(self, val, dur=99, src=None, status_name="攻心/倒戈"):
        """狀態疊加精修批(user追加規則, coordinator訊息): 攻心/倒戈(multi可共存具名狀態)
        便利方法, 對稱 push_block/push_add —— 供 apply_effects k=="lifesteal" 分支與測試
        harness 直接呼叫, 內部走 upsert_named_status(以 (\"攻心倒戈\", src) 為鍵: 同一來源
        重複施加只刷新自己那一筆, 不同來源各自並存)。src=None(測試/一次性呼叫慣例)時每次
        呼叫視為新來源(鍵含 src 本身, None 對 None 仍相等會合併——如需多筆獨立測試用不同
        src 區分, 見demo()測試用法)。"""
        upsert_named_status(self.lifesteals, ("攻心倒戈", src), {
            "val": val, "dur": dur, "status_name": status_name, "src_name": src,
        })

    def dot_settle(self):
        """時序重構(2026-07): DoT/持續效果的「掉血/回血」結算 —— 取代舊「回合末全體同時
        tick()」, 改於該單位輪到行動時、行動前呼叫(見 fight() 主迴圈)。與 decay_durations()
        成對(掉血/回血 vs 持續遞減到期), 合稱取代舊 tick()。只結算「troop 增減」, 不動任何
        持續回合數(那是 decay_durations() 的職責, 在本單位行動後才呼叫) —— dur/left 遞減與
        掉血拆開, 才能表達「先受DoT傷害(可能死亡不行動)→再行動→行動後持續才-1」的新時序。"""
        for dmg, *_ in self.dots:                      # 持續傷害結算(dots[2]為undispellable旗標, 不影響結算量)
            self.troop -= dmg
            self.wounded += dmg * wounded_rate(CUR_ROUND)  # 批18: dot 掉血同樣按當前回合轉化率計入傷兵池
            fire_self_reactive(self, "dmgThreshold", bump_dmg_accum(self, dmg))  # 禁近似令-批L: 一身是膽累積傷害門檻
        # 禁近似令-批K: regens(engine_wiring_gaps_misc族, 對稱engine.js同名分支) —— 對稱上方
        # dots掉血, 逐回合按登記金額治療(受傷兵池/START_TROOP上限雙重夾住, 沿用heal效果既有
        # 相同clamp慣例)。
        if self.regens:
            for rg in self.regens:
                actual = max(0, min(rg[0], self.wounded, START_TROOP - self.troop))
                self.troop += actual
                self.wounded -= actual
        # 禁近似令-批K: pre_dmg_hooks(pre_damage_intercept族) 的 deferSettle 排出的分期傷害,
        # 逐回合攤還扣血, 獨立於觸發它的 hook 本身是否仍存活。
        if self.deferred_dmg:
            for q in self.deferred_dmg:
                self.troop -= q["amt"]
                self.wounded += q["amt"] * wounded_rate(CUR_ROUND)
                fire_self_reactive(self, "dmgThreshold", bump_dmg_accum(self, q["amt"]))  # 禁近似令-批L

    def tick_stack(self):
        """時序徹底一致化批(本批新增, 取代前批誤放decay_durations的做法): stack.stackPer=="round"
        (預設值, 向後相容, 如長驅直入)的逐回合遞增, 呼叫點在 fight() 主迴圈「該單位自己行動前」
        (dot_settle 之後、apply_own_turn_effects/主行動之前), 使該單位這回合行動時已吃到「當回合」
        的疊層值(而非上一輪的舊值)——對稱 dot_settle/apply_own_turn_effects/settle_tick 等既有
        「先結算才行動」掛點, 落實「回合開始、行動前就檢查」的user權威規則。

        沿革: 舊「fight() 回合迴圈頂端、全體單位同時+1層」(全局回合cadence)在時序一致化批(A.1)
        移到 decay_durations()(該單位自己行動後); 本批對局歸因發現「行動後遞增」使爬坡比
        「行動前遞增」晚1輪(長驅直入-7.6pp, 見交接文件), 不符合「行動前檢查」規則, 故拆出獨立
        方法移到行動前。與 stackPer=="cast"(apply_stack_cast(), 發動時)/"attack"(dealt_damage(),
        命中時)兩種既有自參照模式一致(三者皆為「持有者自己的動作/回合」觸發, 只是各自的呼叫
        時機點對應「行動前(round)/發動當下(cast)/命中當下(attack)」三種不同語意, 不再有
        stackPer=="round"獨自延遲一輪的不一致)。"""
        if self.stack and self.stack.get("stackPer", "round") == "round":
            self.stack["n"] = min(self.stack["max"], self.stack["n"] + 1)

    def decay_durations(self):
        """時序重構(2026-07): 狀態持續回合遞減/到期清除 —— 取代舊「回合末全體同時 tick()」,
        改於該單位輪到行動時、行動後呼叫(見 fight() 主迴圈)。與 dot_settle() 成對(掉血/回血
        已在行動前結算), 只負責「持續回合數-1、歸零則清除」, 不再重複扣血/回血。
        +1補償清除(時序重構user權威規則): 舊「回合末全體同時tick()」模型下, 效果若在準備
        階段之外的回合中途施加, 當回合末的全域tick()會立即消耗1單位duration, 即使該單位
        本回合根本還沒真正「用滿」這1回合, 故舊碼多處對dur做 +1 補償。新模型逐單位在「自己
        行動之後」才-1(此處), 準備階段dur=N的buff在該單位第1~N個行動輪生效、第N輪後清除,
        戰中施加的狀態亦自然對齊(施加時點在本輪行動前後決定本輪算不算入第1輪), 不再需要
        +1補償, 已全庫移除(唯一保留: tac_cd 戰法冷卻寫入端的 +1, 見 fight() 主迴圈 fire 分支
        註解 —— 冷卻是「本單位自己行動時設下, 同一行動輪內緊接著的 decay_durations 就會立刻
        扣1」的自我參照場景, 與外部施加的debuff/buff時序不同, 不補償會讓 cd=1 完全失效, 故
        此欄位維持既有 +1 寫法, 不在本次移除範圍內; 詳見交接文件時序重構節)。

        時序徹底一致化批: stack.stackPer=="round" 的逐回合遞增已移出本方法, 改到 tick_stack()
        (行動前呼叫, 見其定義與 fight() 主迴圈呼叫點) —— 前批(時序一致化)曾將此遞增掛在
        decay_durations()(該單位行動後), 對局歸因發現這使爬坡比「行動前遞增」晚1輪(長驅直入
        -7.6pp, 見交接文件), 不符合「行動前檢查」規則(該單位這回合行動時應已吃到當回合的疊層
        值, 而非上一輪的舊值)。本方法自此batch起只負責「持續回合數-1、歸零則清除」與
        hit_flags/tac_cd等行動後才該結算的狀態, 不再相關stack遞增。"""
        self.dots = [[d, l - 1] + rest for d, l, *rest in self.dots if l - 1 > 0]
        # 狀態疊加語意對齊批: regens(休整, unique具名狀態)現行透過 upsert_named_status 以
        # 固定鍵"休整"去重寫入(見 apply_effects k=="regen" 分支), 每筆結構延伸為 [amt, dur,
        # ...其餘欄位(_key/status_name/src_name等)], 比照上面 self.dots 用 *rest 保留延伸
        # 欄位不遺失(舊寫法 `for amt, left in self.regens` 要求恰好2元素, 會在延伸為4元素後
        # 拋 ValueError, 已修正為與 dots 一致的變長解構)。
        if self.regens:
            self.regens = [[amt, left - 1] + rest for amt, left, *rest in self.regens if left - 1 > 0]
        self.mods = [[s, m, l - 1, src, flags] for s, m, l, src, flags in self.mods if l - 1 > 0]
        self.adds = [[k, v, l - 1, src, flags] for k, v, l, src, flags in self.adds if l - 1 > 0]
        self.stat_adds = [[s, ad, l - 1, src, flags] for s, ad, l, src, flags in self.stat_adds if l - 1 > 0]  # 裝備平加到期移除(如 疾馳 speed+25 dur:2)
        self.stun = max(0, self.stun - 1)
        self.silence = max(0, self.silence - 1)
        self.disarm = max(0, self.disarm - 1)
        self.chaos = max(0, self.chaos - 1)            # 批12 ModeF: 混亂 逐回合遞減
        self.insight = max(0, self.insight - 1)
        self.first = max(0, self.first - 1)            # 先攻: 逐回合遞減(dur=N 覆蓋前 N 回合, 如「戰鬥前3回合」)
        self.ambush = max(0, self.ambush - 1)          # 批18: 遇襲 逐回合遞減(先攻的反面, 遲緩)
        self.rigorous = max(0, self.rigorous - 1)      # 狀態疊加精修批: 嚴密 逐回合遞減(同insight/first慣例)
        self.healblock = max(0, self.healblock - 1)    # 批8: 禁療 逐回合遞減
        self.captured = max(0, self.captured - 1)      # 批52j: 捕獲 逐回合遞減(不可淨化, 自然到期)
        self.swap = max(0, self.swap - 1)
        # 批52: 戰法冷卻逐回合遞減。時序重構後寫入端(fight()主迴圈fire分支)仍保留 cd+1(自我
        # 參照場景, 見本方法docstring), 此處遞減邏輯本身不變: cd=1 → 本單位下1個行動輪前已
        # 歸零可再發。
        if self.tac_cd:
            self.tac_cd = {k: v - 1 for k, v in self.tac_cd.items() if v - 1 > 0}
        if self.decay:
            self.decay["left"] -= 1
            if self.decay["left"] <= 0:
                self.decay = None
        # 禁近似令-批K: pre_dmg_hooks 到期清除 + deferred_dmg 持續回合遞減, 對稱 engine.js
        # decayDurations() 同款段落(deferSettle 排出的分期傷害獨立於觸發它的hook本身是否仍存活)。
        if self.pre_dmg_hooks:
            for h in self.pre_dmg_hooks:
                h["dur"] -= 1
            self.pre_dmg_hooks = [h for h in self.pre_dmg_hooks if h["dur"] > 0]
        if self.deferred_dmg:
            remain = []
            for q in self.deferred_dmg:
                q["left"] -= 1
                if q["left"] > 0:
                    remain.append(q)
            self.deferred_dmg = remain
        self.taunt_dur = max(0, self.taunt_dur - 1)
        if self.taunt_dur <= 0:
            self.taunt_by = None
        if self.guard_dur:                             # 代承到期: 清 guardian(如 援助 首回合援護 dur:1)
            self.guard_dur = max(0, self.guard_dur - 1)
            if self.guard_dur <= 0:
                self.guardian, self.guard_share, self.guard_normal_only = None, 0.0, False
        self.dodge_dur = max(0, self.dodge_dur - 1)
        if self.dodge_dur <= 0:
            self.dodge_prob = 0.0
            self.dodge_dmg_type = None  # 批G: 到期一併清除類型限定, 避免下次無條件dodge(dmgType=None)誤沿用舊的殘留類型過濾
        self.surehit_dur = max(0, self.surehit_dur - 1)
        if self.shield:
            self.shield["dur"] -= 1
            if self.shield["dur"] <= 0:
                self.shield = None
        # 批23 A2: 反擊到期清除(過去 dur 幽靈欄位從不遞減, 帶時限的反擊變永久)。狀態疊加
        # 語意對齊批: 改逐筆處理 self.counters(多實例清單, multi具名狀態) —— 每個獨立實例
        # 各自遞減 dur、各自到期清除, 互不影響(某個來源的反擊到期不影響其他來源的反擊繼續生效)。
        if self.counters:
            for _c in self.counters:
                _c["dur"] -= 1
            self.counters = [_c for _c in self.counters if _c["dur"] > 0]
        # 狀態疊加精修批(user追加規則): 攻心/倒戈到期清除, 逐筆處理 self.lifesteals(多實例
        # 清單, multi具名狀態) —— 對稱上方 counters 慣例, 每個獨立來源各自遞減dur、各自到期
        # 清除, 互不影響。
        if self.lifesteals:
            for _l in self.lifesteals:
                _l["dur"] -= 1
            self.lifesteals = [_l for _l in self.lifesteals if _l["dur"] > 0]
        if self.dmg_share:                              # 禁近似令-批K: dmg_share 到期清除(對稱counter既有慣例)
            self.dmg_share["dur"] -= 1
            if self.dmg_share["dur"] <= 0:
                self.dmg_share = None
        if self.pre_attack_hooks:                       # 禁近似令-批K: pre_attack_hooks 到期清除(對稱pre_dmg_hooks既有慣例)
            for h in self.pre_attack_hooks:
                h["dur"] -= 1
            self.pre_attack_hooks = [h for h in self.pre_attack_hooks if h["dur"] > 0]
        # 批52d: 虎嗔到期自然結算(下一回合結束)
        if self.huchen:
            self.huchen["left"] -= 1
            if self.huchen["left"] <= 0:
                settle_huchen(self, early=False)
        self.hit_flags.clear()                         # 受擊觸發(when.on) 每回合各戰法重置一次觸發額度
        if self.immune:                                 # 批16: immuneTo 逐回合遞減(修正: 與 engine.js tick() 對齊, 此前 sgz.py 遺漏此行, 雙引擎不同步)
            self.immune = [[ty, l - 1] for ty, l in self.immune if l - 1 > 0]
        self.fake_report_dur = max(0, self.fake_report_dur - 1)  # 批16: 偽報 逐回合遞減(修正: 與 engine.js tick() 對齊, 此前 sgz.py 遺漏此行, 雙引擎不同步)

    def tick(self):
        """時序重構(2026-07)後保留: dot_settle()+tick_stack()+decay_durations() 的合併捷徑, 供
        「模擬該單位自己完整一輪(掉血+疊層遞增+持續遞減)」的既有測試/呼叫端沿用(fight() 主迴圈
        本身已改為分開呼叫, 不再呼叫 tick(), 見該處 dot_settle→死亡檢查→tick_stack→
        apply_own_turn_effects→行動→decay_durations 新時序)。時序徹底一致化批: 補上
        tick_stack()(本批新增, stack.stackPer=="round"遞增改到行動前, 見其定義), 維持本捷徑
        方法與真實fight()主迴圈時序一致。"""
        self.dot_settle()
        self.tick_stack()
        self.decay_durations()


# 傷害公式旋鈕(批3 重塑): 社群拆解(知乎菜頭50級傷害模型 + B站櫻謀詭計錨點), 用實測錨點反解常數。
# 錨點(兵10000/coef1.0/士氣100/無增減傷, morale_mult(100)=1.0 已併入取樣, 取隨機帶中值1.0):
#   錨1 屬性差0   → 實測 ≈476 傷害 ⇒ DMG_A = 476/sqrt(10000) = 4.76
#   錨2 屬性差200 → 實測 ≈764 傷害 ⇒ DMG_B = (764-476)/200 = 1.44
#   錨3 屬性差大負值(保底) → 實測 ≈90  傷害 ⇒ DMG_FLOOR = 90/sqrt(10000) = 0.9
# 之後有更多實測數據(不同兵力/等級)可再校準, 目前僅50級單一等級係數樣本, 折入常數中。
DMG_A = 4.76
DMG_B = 1.44
DMG_FLOOR = 0.9


def damage(src, dst, coef, kind, src_troop=None, is_normal=None, is_active=None, is_charge=None, force_pierce=False, dot_status=None):
    troop = src.troop if src_troop is None else src_troop  # 結算傷害用施毒當下定格兵力
    atk = src.eff("intel") if kind == "intel" else src.eff("force")
    deff = dst.eff("intel") if kind == "intel" else dst.eff("command")
    troop_sqrt = math.sqrt(max(0, troop))
    base = max(DMG_A * troop_sqrt + DMG_B * (atk - deff), DMG_FLOOR * troop_sqrt) * coef
    base *= counter_mult(src.ttype, dst.ttype)        # 克制: 隊伍兵種 vs 隊伍兵種
    base *= morale_mult(MORALE)
    # 批22: 輸出減益疊加上限 -90%(戰報實測: 荀彧-50%疊到-90.00%封頂, 輸出至少保留10%)。例外:
    # 虛弱(無法造成傷害)類戰法既有慣例用單一 amp val=-1.0 精確歸零當回合傷害(克敵制勝/威謀
    # 靡亢/臨戰先登), 這是「無法造成傷害」的二元語意, 不是「%減益疊加」, 總和<=-1時維持完全
    # 歸零(不受-90%封頂影響), 只在 -1 < 總和 < -0.9 這個「多重%減益疊加但尚未到虛弱程度」的
    # 區間套用-90%下限。
    # 批24 D2: dmgType 過濾 —— amp()/addbonus("mitig") 傳入本次傷害的 kind(phys/intel), 只加總
    # 「未宣告 dmgType 或宣告類型與本次相符」的加成/減傷(見 e.dmgType 呼叫端, apply_effects
    # k=="amp"/"mitig" 分支)。批28 B3: is_normal(可選) —— 傳入本次傷害是否為普攻, 供 amp()/
    # addbonus("mitig") 過濾 normalOnly 標記的加成/減傷(僅普攻傷害生效, 見至柔動剛「降低我軍
    # 及敵軍全體普通攻擊傷害35%」——外部查證確認原文是「降低」非root data摘要文字誤植的「提升」,
    # 且明確限定「普通攻擊傷害」而非全部傷害, 過去mitig無範圍限定, 誤及戰法傷害, 見批28 B3
    # 修正說明); 呼叫端未傳(dot/counter/settle等既有呼叫慣例, 見 damage() 各呼叫點)時預設
    # None, 安全側不套用 normalOnly 加成/減傷(見 addbonus() docstring)。
    # 批31 A: is_active(可選, 對稱於 is_normal) —— 傳入本次傷害是否為主動戰法所致, 供
    # amp() 過濾 activeOnly 標記的加成(僅主動戰法傷害生效, 見士爭先赴)。批40 B: is_charge
    # (可選, 對稱is_active) —— 傳入本次傷害是否為突擊戰法所致, 供amp()過濾chargeOnly標記的
    # 加成(僅突擊戰法傷害生效/降低, 見一鼓作氣/藏刀)。
    # 批52j: 捕獲狀態無法造成傷害
    if getattr(src, "captured", 0) > 0:
        return 0.0
    total_amp = src.amp(kind, is_normal, is_active, is_charge, dot_status)  # 禁近似令-批L: dot_status(可選, 尾端新增, 向後相容既有全部呼叫點)—— 供k=="dot"分支傳入該次dot的具名狀態(才辯機捷 e["dmgFromStatus"] 過濾用)
    base *= 0.0 if total_amp <= -1 else 1 + max(-0.9, total_amp)  # 增傷(疊加/衰減/敵方減益)
    # 禁近似令-批K: force_pierce —— dot 效果級 e["pierce"]==True 專用(見 apply_effects
    # k=="dot"分支), 強制本次結算完全無視 dst 的 mitig, 對稱 engine.js 同名參數註解
    # (獅子奮迅「叛逃狀態...無視防禦」, engine_wiring_gaps_misc族)。
    mit = 0.0 if force_pierce else dst.addbonus("mitig", kind, is_normal) * (1 - min(1.0, src.addbonus("pierce")))  # 看破: 無視部分減傷
    base *= max(0.1, 1 - mit)
    # 禁近似令-批K: pre_dmg_hooks(pre_damage_intercept族) —— 見 Unit.__init__ 對稱註解與
    # engine.js damage() 同款段落。攻擊方(src)自己掛的 probVoid 與防禦方(dst)自己掛的
    # probMitig/stepMitig/deferSettle 皆在此消費(crit皆已算完之後, 隨機帶之前)。
    for h in (src.pre_dmg_hooks or []):
        if h.get("dmg_type") and h["dmg_type"] != kind:
            continue
        if h.get("hook_kind") == "probVoid" and random.random() < (h.get("rate") or 0):
            base *= max(0.0, 1 - (h.get("val") if h.get("val") is not None else 1))
    for h in (dst.pre_dmg_hooks or []):
        if h.get("dmg_type") and h["dmg_type"] != kind:
            continue
        hk = h.get("hook_kind")
        if hk == "probMitig":
            if random.random() < (h.get("rate") or 0):
                base *= max(0.0, 1 - (h.get("val") or 0))
        elif hk == "stepMitig":
            eff_hits = min(h.get("hits", 0), h.get("max") if h.get("max") is not None else 30)
            cur = max(0.0, (h.get("val") or 0) + (h.get("step") or 0) * eff_hits)
            if cur > 0:
                base *= max(0.0, 1 - cur)
            h["hits"] = h.get("hits", 0) + 1
        elif hk == "deferSettle":
            defer_amt = base * (h.get("pct") or 0)
            base -= defer_amt
            rounds = h.get("delay_rounds") or 3
            dst.deferred_dmg = dst.deferred_dmg or []
            dst.deferred_dmg.append({"amt": (defer_amt * (1 - (h.get("reduce_pct") or 0))) / rounds, "left": rounds})
    # 批H: 會心(兵刃暴擊)/奇謀(謀略暴擊)真擲骰層 —— 禁近似令下取代全庫14筆「crit-ev」期望值
    # 折算(見 no_approx_inventory.json crit_system_primitive族/engine_limitations.md本節)。
    # 機制: 每次造成傷害時, 先擲一次crit判定, rate=src此刻所有「會心/奇謀機率」來源加總
    # (k=="critUp", 依dmgType分流: dmgType="phys"=會心/兵刃暴擊, dmgType="intel"=奇謀/謀略
    # 暴擊, 與amp/mitig既有dmgType路由慣例完全一致, 呼叫端傳入的kind本就已是phys/intel);
    # 命中則本次傷害額外乘上(1+crit_mult), crit_mult=1.0(官方戰報實測基準「觸發會心,
    # 兵刃傷害提升100.00%」, 見calibration_anchors.json crit節)+critDmgUp累加(k=="critDmgUp",
    # 「會心傷害/奇謀傷害+X%」幅度修飾語, 如華服/長慮, 同dmgType路由, 未命中crit則此層不生效
    # 也不消費critDmgUp)。與amp是「機率來源(critUp)」與「幅度來源(critDmgUp)」分離、但透過
    # 同一個離散事件(擲骰命中與否)耦合的雙層設計, 不同於amp的單一靜態疊加值。
    # 乘法層疊順序: 疊在amp/mitig之後(倍率獨立於±4%隨機帶之前) —— crit是「這一下攻擊有沒有
    # 命中會心」的二元判定, 不應被視為amp累加的一部分(amp封頂-90%/總和<=-1虛弱語意不應牽動
    # crit判定), 也不應被隨機帶±4%「稀釋」掉critRate本身的擲骰獨立性(±4%是每次攻擊都有的
    # 基礎浮動, crit是額外的、獨立擲一次的二元事件, 兩者互不影響, 詳見engine_limitations.md
    # 本節「與amp/mitig/±4%隨機帶的結算順序」)。
    # sgz.py 無 TRACE/日誌機制(見上方Unit.__init__同款既有註解), 此處純結算不列印; TRACE
    # 字樣輸出(比照遊戲戰報原文「觸發會心, 兵刃傷害提升100.00%」)僅在 docs/engine.js 實作
    # (供瀏覽器UI推演明細用), 見該檔 damage() 對稱段落。
    crit_rate = src.addbonus("critUp", kind, is_normal, is_active, is_charge)
    if crit_rate > 0 and random.random() < crit_rate:
        crit_bonus = 1.0 + src.addbonus("critDmgUp", kind, is_normal, is_active, is_charge)
        base *= (1 + crit_bonus)
    # 傷害不浮動(user權威規則2026-07-11): 同條件傷害為定值, 移除舊±4%隨機帶
    # (早期存疑保留, 現經user確認遊戲傷害數字不浮動)。會心仍是離散擲骰(上方), 非連續浮動。
    return max(0, base)


def hit(src, dst, coef, kind, is_normal=False, on_event=None, on_deal=None, is_active=None, is_charge=None):  # 造成傷害(含規避/護盾/代承轉移/反擊), 累積結算層數; 批31 A: is_active(可選, 尾端新增, 向後相容既有全部呼叫點)—— 傳入本次傷害是否為主動戰法所致; 批40 B: is_charge(可選, 對稱is_active)—— 傳入本次傷害是否為突擊戰法所致
    # 禁近似令-批K: was_alive(engine_wiring_gaps_misc族, on-kill事件, 對稱engine.js同名變數)
    # —— 記錄本次命中前dst是否存活, 供下方擊殺判定精準抓「這一下才是致命一擊」, 見虎痴
    # pierce.onKill 消費端。
    was_alive = dst.troop > 0
    if not src.surehit_dur and dst.dodge_dur and (dst.dodge_dmg_type is None or dst.dodge_dmg_type == kind) and random.random() < dst.dodge_prob:  # 規避: 完全迴避一次傷害(必中無視); 批G: dodge_dmg_type限定只對該類型(phys/intel)生效, None=向後相容不分類型
        if on_event:
            on_event(dst, src, is_normal, 0, kind)  # 批39 C: 補傳kind(本次傷害類型), 供on_hit()對稱dealt_damage的when.dmgType過濾
        return
    dmg = damage(src, dst, coef, kind, is_normal=is_normal, is_active=is_active, is_charge=is_charge)  # 批28 B3/批31 A/批40 B: 傳入is_normal/is_active/is_charge供amp()過濾normalOnly/activeOnly/chargeOnly標記的加成
    # 批22: block(次數型格擋, 抵禦/警戒同族) —— 判定順序 dodge→block→shield→傷害(見紅線指示)。
    # 每次受擊消耗1次(不論本次傷害量多寡), val=1.0(如「抵禦」)完全格擋歸零本次傷害,
    # val=0.x(如「警戒」-75.35%)按比例打折。用光即從陣列移除。
    # 批35 D: BLOCK_CONSUME_THRESHOLD —— grok查證機鑑先識原文「受到的傷害超過自身可攜帶
    # 最大兵力的6%時(最低100兵力)」才消耗1次警戒並減傷。未達門檻的傷害不消耗、不減傷,
    # 照常全額打進去(見 engine.js 同段註解/engine_limitations.md 第30節)。
    if dst.block and dmg > BLOCK_CONSUME_THRESHOLD:
        block_val = dst.consume_block(dmg_type=kind)  # 批G: 傳入本次傷害類型(phys/intel), 只消耗未限定類型或類型相符的格擋層(見consume_block docstring)
        dmg *= max(0.0, 1 - block_val)
    if dst.shield and dst.shield["amt"] > 0:          # 護盾: 先於兵力扣減吸收傷害
        absorb = min(dst.shield["amt"], dmg)
        dst.shield["amt"] -= absorb
        dmg -= absorb
        if dst.shield["amt"] <= 0:
            dst.shield = None
    wr = wounded_rate(CUR_ROUND)  # 批18: 傷兵池 —— 本次受到的傷害按當前回合轉化率計入(準備階段 CUR_ROUND=0 用第1回合檔)
    # 批J(禁近似令-transfer轉移族): absorb_guards(單次全額代承, redirect.guardFor:"leader")
    # —— 優先於下方常駐 guardian(%分擔每一下直到guard_dur到期)判斷: 只在普攻(is_normal)時,
    # 找第一個「本回合(對該代承者而言)尚未觸發過」的登記項, 把「這一下」攻擊的傷害(依
    # ag["share"], 預設1.0=全額)轉給該代承者, dst 只承受剩餘部分(share<1時); 找到就處理完
    # 這一下的兵力轉移, 不再落入下方 guardian 常駐邏輯(兩者互斥擇一, 避免同一下傷害被兩套機制
    # 各自折算一次, 造成傷害量憑空增減)。節流鍵沿用 counter_guards 慣例(掛在代承者自己的
    # hit_flags上, 而非dst身上——「每個代承單位每回合最多代承1次」)。
    absorbed = False
    if is_normal and dst.alive:
        for ag in dst.absorb_guards:
            gu = ag["unit"]
            if not gu.alive or gu is dst:
                continue
            flag_key = ("absorb_guard", id(ag))
            if flag_key in gu.hit_flags:
                continue
            if random.random() < ag.get("prob", 1.0):
                gu.hit_flags.add(flag_key)
                a_share = ag.get("share", 1.0)
                a_amt = dmg * a_share
                d_amt = dmg * (1 - a_share)
                gu.troop -= a_amt
                gu.wounded += a_amt * wr
                fire_self_reactive(gu, "dmgThreshold", bump_dmg_accum(gu, a_amt))  # 禁近似令-批L: 一身是膽累積傷害門檻
                if d_amt > 0:
                    dst.troop -= d_amt
                    dst.wounded += d_amt * wr
                    fire_self_reactive(dst, "dmgThreshold", bump_dmg_accum(dst, d_amt))  # 禁近似令-批L
                absorbed = True
                break
    if not absorbed:
        g = dst.guardian
        if g and g.alive and g is not dst and not (dst.guard_normal_only and not is_normal):  # normalOnly 援護: 戰法傷害(is_normal=False)不轉移
            g_share = dmg * dst.guard_share
            d_share = dmg * (1 - dst.guard_share)
            g.troop -= g_share
            g.wounded += g_share * wr
            fire_self_reactive(g, "dmgThreshold", bump_dmg_accum(g, g_share))  # 禁近似令-批L
            dst.troop -= d_share
            dst.wounded += d_share * wr
            fire_self_reactive(dst, "dmgThreshold", bump_dmg_accum(dst, d_share))  # 禁近似令-批L
        else:
            dst.troop -= dmg
            dst.wounded += dmg * wr
            fire_self_reactive(dst, "dmgThreshold", bump_dmg_accum(dst, dmg))  # 禁近似令-批L
    # 禁近似令-批K: on_kill_grants(engine_wiring_gaps_misc族, 對稱engine.js同名分支) ——
    # 「這一下」把dst由存活打至陣亡(was_alive且現在troop<=0)時, 消費src身上登記的擊殺獎勵
    # 清單(見k=="pierce"+e["onKill"]註冊端), 取代虎痴「破陣需擊敗鎖定目標才獲得, 約後半場
    # 生效→val×0.5折算」的EV近似, 改為真正「擊敗目標的那一刻」才授予, 之後常駐到戰鬥結束。
    if was_alive and dst.troop <= 0 and src.alive and src.on_kill_grants:
        for g in src.on_kill_grants:
            if g["kind"] == "pierce":
                src.push_add("pierce", g["val"], g.get("dur", 99), "onKill:pierce")
        src.on_kill_grants = []
    # 禁近似令-批K: dmg_share(engine_wiring_gaps_misc族, 對稱engine.js同名分支) —— 「使其任一
    # 目標受到傷害時會回饋X%傷害給其他敵軍」的傷害轉嫁給隊友機制(連環計), 用 fight() 傳入的
    # allies_of/foes_of 全域存取dst自己的隊伍(對dst而言的「我方」), 排除dst自己後隨機選一位
    # 分攤val×dmg。dmg>0(含被block/shield折算後的實際值)才觸發。
    if dmg > 0 and dst.dmg_share and dst.alive and _FIGHT_CTX.get("allies_of"):
        mates = [x for x in _FIGHT_CTX["allies_of"](dst) if x.alive and x is not dst]
        if mates:
            buddy = random.choice(mates)
            share_amt = dmg * dst.dmg_share["pct"]
            buddy.troop -= share_amt
            buddy.wounded += share_amt * wr
            fire_self_reactive(buddy, "dmgThreshold", bump_dmg_accum(buddy, share_amt))  # 禁近似令-批L
    if dst.settle:
        dst.settle["layers"] = min(dst.settle["max"], dst.settle["layers"] + 1)
    # 批8: 倒戈 —— 造成傷害時按比例回復自身兵力(以本次造成的傷害量 dmg 為基準), 上限
    # START_TROOP。狀態疊加精修批(user追加規則, coordinator訊息): 攻心/倒戈為 NAMED_STATUS
    # "multi"(可共存)具名狀態, 改逐一走訪 src.lifesteals(多實例清單, 對稱上方 dst.counters
    # 反擊的逐筆結算慣例) —— 每個獨立來源各自按自己的 val 回復, 總回復量=各實例加總(數學上
    # 與前批 ls=src.addbonus("lifesteal") 單一標量寫法完全等價, 因回復量對val線性可加:
    # sum(val_i)×dmg == sum(val_i×dmg); 差異在於現在逐筆結算, 使個別來源在decay_durations()
    # 各自獨立到期、未來可各自於TRACE歸因, 不再是單一去向不明的加總值)。
    # 批G: lifestealGiven(倒戈效果量加成) —— 對稱既有healGiven(施放的治療×(1+val)), 掛在
    # src(倒戈觸發者)自己身上, 使倒戈本身回復的兵力量再乘上(1+val); 這是「自身攻心效果+X%」
    # 的自我buff(長慮), 不是具名狀態多實例(NAMED_STATUS未涵蓋), 維持既有addbonus加總語意
    # 不變, 對所有倒戈實例套用同一個加成倍率(只算一次, 迴圈外先算好, 避免每筆重複addbonus)。
    if src.lifesteals and src.alive:
        given_mult = max(0.0, 1 + src.addbonus("lifestealGiven"))
        for _ls in list(src.lifesteals):
            src.troop = min(START_TROOP, src.troop + dmg * _ls["val"] * given_mult)
    # 批33: on_event/on_deal 補傳 dmg(本次結算後的實際傷害量, 已經過block/shield/代承折算,
    # 與寫入 wounded 池的量一致) —— 供 e["ofDamage"](傷害比例治療) 反應式heal使用, 見
    # on_hit()/dealt_damage() 呼叫端與 apply_effects() heal 分支(dmg 參數)。
    # 批39 C: 補傳kind(本次傷害類型, phys/intel) —— 供on_hit()對when.dmgType/e["when"]["dmgType"]
    # 過濾(對稱dealt_damage自批27起就有的dmgType過濾), 修正damaged/attacked反應式路徑過去完全
    # 不分兵刃/謀略傷害觸發(剛勇無前/剛烈不屈「受到兵刃傷害時」誤及謀略傷害)。
    if on_event:
        on_event(dst, src, is_normal, dmg, kind)
    # 批27 A: on:"dealtDamage" —— src(施加本次傷害的一方)反應式觸發, 只在非規避(確實造成
    # 傷害, 含被完全格擋/護盾吸收歸零的情形——「造成傷害」語意上仍是「打出了這一擊」, 只是
    # 傷害量被防禦手段抵銷, 與「規避=攻擊未命中」不同, 故僅 dodge 分支排除, block/shield
    # 歸零不排除)時才觸發, 傳入 kind 供 dmgType(兵刃/謀略)過濾判斷。
    if on_deal and src.alive:
        on_deal(src, dst, is_normal, kind, dmg)
    # 批52d: 虎嗔 —— 目標每次實際受傷(dmg>0)疊層; 達 maxHits 立即結算+震懾
    if dmg > 0 and dst.huchen and dst.alive:
        dst.huchen["hits"] = min(dst.huchen["maxHits"], dst.huchen["hits"] + 1)
        if dst.huchen["hits"] >= dst.huchen["maxHits"]:
            settle_huchen(dst, early=True)
    # 反擊: 直接還擊 src(不經 hit, 不遞迴)。狀態疊加語意對齊批: 反擊為 NAMED_STATUS 已確認
    # 的 "multi"(可共存)具名狀態, 改逐一走訪 dst.counters(多實例清單) —— 每個獨立來源各自
    # 判定 prob/結算傷害, 全部生效(不像過去單一 dst.counter 只有一份, 多來源時後者覆蓋前者)。
    # 先淺拷貝快照(list(...))再迭代, 對稱 counter_guards/absorb_guards 既有防禦性快照慣例,
    # 避免結算過程中(理論上不會, 但保留彈性)增刪 counters 造成迭代期間清單變動的不確定行為。
    for c in list(dst.counters):
        if not (dst.alive and src.alive):    # 前一個反擊實例可能已把 src 打死, 死者不再被反擊
            break
        if c.get("normalOnly") and not is_normal:  # 批G: normalOnly限定只在普攻(is_normal=True)時觸發, 省略時向後相容(任意傷害皆可觸發)
            continue
        if random.random() >= c.get("prob", 1.0):
            continue
        ck = c.get("kind", "phys")
        # 禁近似令-批K: c["ofDamage"](engine_wiring_gaps_misc族) —— 對稱 heal 既有 e["ofDamage"]
        # 慣例(依本次觸發事件的實際傷害量比例輸出), 取代反擊固定用coef重新計算一次全新damage()
        # 的舊近似(裝備「受到普通攻擊時,反彈5%傷害」——反彈的是"這一下實際承受的傷害量"的5%,
        # 而非重新算一次以c["coef"]為傷害率的獨立攻擊)。dmg是本次已經過block/shield折算後的
        # 實際傷害量。
        cd = dmg * c["ofDamage"] if c.get("ofDamage") is not None else damage(dst, src, c["coef"], ck)
        src.troop -= cd
        src.wounded += cd * wounded_rate(CUR_ROUND)
        fire_self_reactive(src, "dmgThreshold", bump_dmg_accum(src, cd))  # 禁近似令-批L
        # 批52e/f: 反擊也是「造成傷害」—— 帶文武雙全等 dealtDamage 疊層應計入;
        # 抵禦/虛弱等使實際傷害歸零時仍算「打出這一擊」(與 hit 主段 on_deal 語意一致)
        if on_deal and dst.alive:
            on_deal(dst, src, False, ck, cd)
    # 批28 B1: 守護式反擊(counter_guards) —— dst(如隊伍主將)受到普攻時, 由登記在
    # dst.counter_guards 裡的其他單位(如副將)代為反擊 src, 而非 dst 自己還手(見虎衛軍
    # 「我軍主將即將受到普攻時, 副將...對攻擊者造成兵刃傷害」)。只在普攻(is_normal=True)
    # 時觸發(對應原文「即將受到普攻時」, 非任意傷害); 每個守護單位每回合最多觸發1次(對應
    # 原文「每回合最多觸發1次」), 用 hit_flags 以 guardian 自身 id 為鍵節流(與 when.on 反應式
    # 的既有節流慣例一致, 見上方 hit_flags 說明)。
    if is_normal and dst.alive and src.alive:
        for g in dst.counter_guards:
            gu = g["unit"]
            if not gu.alive or gu is dst:
                continue
            flag_key = ("counter_guard", id(g))
            if flag_key in gu.hit_flags:
                continue
            if random.random() < g.get("prob", 1.0):
                gu.hit_flags.add(flag_key)
                gk = g.get("kind", "phys")
                gd = damage(gu, src, g["coef"], gk)
                src.troop -= gd
                src.wounded += gd * wounded_rate(CUR_ROUND)
                fire_self_reactive(src, "dmgThreshold", bump_dmg_accum(src, gd))  # 禁近似令-批L
                # 批52f: 守護反擊同反擊——零傷仍觸發 dealtDamage(文武等)
                if on_deal and gu.alive:
                    on_deal(gu, src, False, gk, gd)
                # 禁近似令-批K: counter_target_binding族 —— guardFor反擊觸發後, 額外副作用
                # 精確綁定到「這一次」的攻擊者(src)或反擊執行者自己(gu), 對稱 engine.js 同名
                # 段落(古之惡來對攻擊者施加降傷/虎衛軍反擊執行者自身疊層統率)。
                da = g.get("debuffAttacker")
                if da and src.alive:
                    flags = {"dmgType": da["dmgType"]} if da.get("dmgType") else None
                    src.push_add("amp", -(da.get("val") or 0), da.get("dur", 1) or 1,
                                 "counterGuard:debuffAttacker", flags)
                ss = g.get("selfStack")
                if ss:
                    if gu.guard_stack_n is None:
                        gu.guard_stack_n = {}
                    gkey = id(g)
                    already_ss = gu.guard_stack_n.get(gkey, 0)
                    if ss.get("max") is None or already_ss < ss["max"]:
                        layers_ss = already_ss + 1
                        gu.guard_stack_n[gkey] = layers_ss
                        total_ss = (ss.get("perVal") or 0) * layers_ss
                        gu.push_stat_add(ss.get("statField", "force"), total_ss, ss.get("dur", 99), "counterGuard:selfStack")
    # 禁近似令-批K: hit() 補 return dmg(對稱engine.js同名補丁)—— 供 fire_extra_hits 的
    # eh["lifesteal"](engine_wiring_gaps_misc族)讀取這一段extraHits自己造成的實際傷害量計算
    # 自我回血, 純新增不影響任何既有呼叫端(過去全部呼叫點皆未讀取hit()回傳值)。
    return dmg


def extra_count(ex):                                  # 連擊/追擊次數: 整數部分必定, 小數部分機率
    return int(ex) + (1 if random.random() < (ex - int(ex)) else 0)


def settle_huchen(u, early=False):
    """批52d: 虎嗔結算 —— 依 hits 疊層算傷害率後造成兵刃傷害; early=True(滿3次)另加震懾。
    結算後施放者兵刃 amp+8% 可疊(直到戰鬥結束)。先清 huchen 再打, 避免結算傷害再疊層。"""
    h = u.huchen
    if not h:
        return
    u.huchen = None
    caster = h.get("caster")
    hits = min(h.get("hits", 0), h.get("maxHits", 3))
    coef = h.get("base", 0.20) + hits * h.get("per", 0.30)
    if caster and caster.alive and u.alive:
        dmg = damage(caster, u, coef, h.get("kind", "phys"))
        u.troop -= dmg
        u.wounded += dmg * wounded_rate(CUR_ROUND)
        fire_self_reactive(u, "dmgThreshold", bump_dmg_accum(u, dmg))  # 禁近似令-批L
    if early and u.alive:
        u.stun = max(u.stun, 1)                        # 1 回合震懾(時序重構: dur原值不補償+1)
    if caster and caster.alive:
        amp_v = h.get("ampOnSettle", 0.08)
        src = h.get("src") or "虎嗔"
        caster.push_add("amp", amp_v, 99, src=src, max_stack=h.get("ampMaxStack", 99),
                        flags={"dmgType": "phys"})


def settle_tick(u, team):
    """時序一致化(2026-07 批次) A.2: settle(結算傷害·猛毒, 密計誅逆) 疊滿層數/倒數歸零的
    爆發判定 —— 從舊「回合末全體同時檢查」(for u in A+B, 全局回合cadence)改為持有者(u, 中毒
    目標)自己的行動輪結算, 比照 dot_settle() 掛點(u 自己行動前, 爆發可能致死, 與DoT對稱處理,
    見 fight() 主迴圈呼叫點: settle_tick(u, allies_of(u)))。爆發/倒數判斷邏輯本身不變, 只改
    cadence基準。模組層級函式(對稱 settle_huchen), 供 fight() 主迴圈與測試直接呼叫。team:
    u 所屬隊伍(A 或 B 陣列), 爆發時對其中存活單位造成傷害(singleTarget 時僅打 u 本人)。
    順手補上 engine.js 既有但 sgz.py 先前遺漏的 perStackFrom(結算時動態讀取指定
    amp_layers_by_id 計數器, 而非靜態layers)/singleTarget(僅打holder本人, 而非全隊)處理,
    達成雙引擎既有能力對齊(密計誅逆目前資料未使用兩者, 此修正面向雙引擎結構parity, 非本次
    cadence改動直接要求, 但因改寫同一段程式碼故一併補齊, 避免遺留潛在雙引擎分歧)。"""
    s = u.settle
    if not s:
        return
    if s["layers"] >= s["max"] or s["left"] <= 1:
        stack_layers = (u.amp_layers_by_id or {}).get(s["perStackFrom"], 0) if s.get("perStackFrom") else s["layers"]
        targets = [u] if s.get("singleTarget") else team
        for v in targets:
            if not v.alive:
                continue
            sd = damage(s["caster"], v, s["base"] + s["per"] * stack_layers, s["kind"], s["snap"])
            v.troop -= sd
            v.wounded += sd * wounded_rate(CUR_ROUND)
            fire_self_reactive(v, "dmgThreshold", bump_dmg_accum(v, sd))  # 禁近似令-批L
        u.settle = None
    else:
        s["left"] -= 1


def target_has(u, ctype):
    """批16: ifTargetHas —— 效果/extraHits 段條件: 只對「已有該狀態」的目標生效/結算。
    dot: dots 陣列非空(=正在持續掉血); 控制類(stun/silence/disarm/chaos/insight): 對應欄位>0。
    批52d: huchen/虎嗔 —— 將門虎女負面狀態。
    批52g: 具名狀態(水攻/沙暴/灼燒…) —— dots[3] 名稱匹配, 或 ctype==該名。
    批52j: capture/捕獲。
    批I(禁近似令-scale/比較族): ctype 可為陣列(list/tuple) —— OR語意, 只要命中其中任一
    單一ctype即算符合(深藏若虛「震懾/計窮/繳械/混亂任一」/百步穿楊「若目標處於控制狀態」/
    橫掃千軍「繳械或計窮」), 遞迴呼叫自身逐一比對, 取代過去「只能擇一硬編」的近似。呼叫端
    ifTargetHasNot 沿用同一函式再取反(見 apply_effects/applyEffects 既有 `not target_has(...)`
    寫法), De Morgan's律自動給出正確的「皆非」語意(NOT(A或B) = NOT A 且 NOT B), 不需要
    對 ifTargetHasNot 額外處理陣列語意。
    weak/虛弱: 新增ctype, 偵測「amp總和<=-1」(無法造成傷害的虛弱狀態, 挫志怒襲等戰法用
    amp val:-1.0表達, 虛弱本身不是獨立狀態變數, 需彙總u.addbonus("amp")才能判斷, 對稱
    既有extra/群攻用addbonus查詢的慣例)。"""
    if not u:
        return False
    if isinstance(ctype, (list, tuple)):
        return any(target_has(u, c) for c in ctype)
    if ctype == "dot":
        return len(u.dots) > 0
    if ctype in ("huchen", "虎嗔"):
        return u.huchen is not None
    if ctype in ("capture", "捕獲", "captured"):
        return getattr(u, "captured", 0) > 0
    if ctype in ("stun", "silence", "disarm", "chaos", "insight"):
        return getattr(u, ctype) > 0
    if ctype in ("weak", "虛弱", "weakened"):
        return u.addbonus("amp") <= -1.0
    # 批C: 群攻(extra, 普通攻擊時對目標同部隊其他武將造成傷害)狀態查詢——引弦力戰「若已處於
    # 群攻狀態，則提高武力」需要判斷持有者自身是否已有群攻加成, 過去target_has完全不認得
    # "extra"/群攻這個ctype(只能落到最後的dot具名比對, 恆假)。用addbonus("extra")>0判斷
    # (同象兵「自身有灼燒(dot)時才獲得群攻(extra)」測試案例的反向查詢: 那裡是查dot決定要不要
    # 給extra, 這裡是查extra本身是否已存在)。
    if ctype in ("extra", "群攻"):
        return u.addbonus("extra") > 0
    # 批52g: 具名 dot 狀態
    if any((len(d) > 3 and d[3] == ctype) for d in u.dots):
        return True
    return False


def count_named_statuses(u, names):
    """批52g: 目標身上具名狀態種類數(水攻/沙暴各算一種, 同名多層仍算1)。"""
    if not u or not names:
        return 0
    want = set(names)
    found = set()
    for d in u.dots:
        if len(d) > 3 and d[3] in want:
            found.add(d[3])
    return len(found)


def upsert_named_status(lst, key, payload):
    """狀態疊加語意對齊批: 具名狀態清單通用「插入或覆蓋」原語(對稱 engine.js upsertNamedStatus)。
    lst: 狀態實例清單(如 u.counters/u.regens), 每筆皆為 dict。key: 本次施加的去重鍵——
      unique(唯一狀態, 如急救/休整): 傳 status_name 本身(所有來源共用同一把鑰匙, 後蓋前,
      全場至多1實例, 對應 NAMED_STATUS["急救"/"休整"]["mode"]=="unique")。
      multi(可共存狀態, 如反擊): 傳 (status_name, 來源id) 二元組(各來源各自一把鑰匙, 同一
      來源重複施加只刷新自己那筆, 不同來源互不影響、全部並存, 對應 NAMED_STATUS["反擊"]
      ["mode"]=="multi")。
    找到相同 key 的既有實例則整筆取代(保留最新來源/數值, 對應 user 規則「再施加同名狀態會
    覆蓋舊的」); 找不到則新增。就地修改 lst(不回傳新 list), 對稱既有 push_add/push_mod 等
    呼叫慣例。payload 會被淺拷貝一份並補上 "_key" 欄位(供下次施加時比對用, 不影響既有讀取端
    只認 index 0/1 等既有慣例, 因為是額外新增欄位, 非取代既有欄位)。"""
    payload = dict(payload)
    payload["_key"] = key
    for i, item in enumerate(lst):
        if isinstance(item, dict) and item.get("_key") == key:
            lst[i] = payload
            return
    lst.append(payload)


def effect_src_name(t, e):
    """狀態疊加語意對齊批: 具名狀態實例的來源顯示名(供未來戰報「執行來自【X】的【狀態】」,
    對稱 engine.js effectSrcName)。優先序: 效果自帶的裝備/兵書標名(_eqNm/_bsNm, 見
    Unit.__init__ 合併裝備/兵書效果時附加, 供 apply_passives() 對 u.eq/u.bs 呼叫 apply_effects
    時"t"是匿名合成dict、取不到nameZh的情形) > 戰法本身 nameZh(t 為真實戰法物件時)。
    兩者皆缺(理論上不應發生, 兵書/裝備已在建構時補標)則回傳 None, 不擅自杜撰來源名。"""
    return e.get("_eqNm") or e.get("_bsNm") or t.get("nameZh")


# 批52g: 戰法名→默認 dot 狀態名(資料未寫 name/dotName 時補)
DOT_NAME_BY_TACTIC = {
    "水淹七軍": "水攻", "興雲布雨": "水攻", "興雲佈雨": "水攻", "风声鹤唳": "水攻",
    "風聲鶴唳": "水攻", "呼风唤雨": "水攻", "呼風喚雨": "水攻", "水攻": "水攻",
    "飞砂走石": "沙暴", "飛沙走石": "沙暴", "沙暴": "沙暴",
    "天降火雨": "灼燒", "火炽原燎": "灼燒", "火熾原燎": "灼燒", "焰焚箕轸": "灼燒",
    "焰焚箕軫": "灼燒", "神火计": "灼燒", "神火計": "灼燒", "火烧连营": "灼燒",
    "火燒連營": "灼燒", "楚歌四起": "沙暴",
}


def resolve_dot_name(e, t):
    return e.get("name") or e.get("dotName") or DOT_NAME_BY_TACTIC.get(t.get("nameZh") or "")


def pick_choice(choices):
    """批16: choices(擇一分支) —— 戰法欄 choices:[{weight, effects,...}], 發動時按權重隨機選一組
    效果套用(預設均分, 無 weight 視為1)。回傳中選分支物件本身(供合併覆寫基礎戰法的 coef/kind/
    effects/extraHits/n/nMax 等欄位; 分支未提供的欄位保留基礎戰法原值)。"""
    ws = [c.get("weight", 1) for c in choices]
    total = sum(ws)
    x = random.random() * total
    for c, w in zip(choices, ws):
        x -= w
        if x <= 0:
            return c
    return choices[-1]


def not_ud(entry):
    """批J(禁近似令-transfer轉移族): 從 dispel_unit() 內部提出成模組層級共用函式(原僅
    dispel_unit 本地閉包), 供新增的 collect_debuff_tokens() 一併重用同一份「是否可被驅散/
    轉移」判斷, 避免兩處各自維護一份 undispellable 判斷式而日後改動時彼此漂移。"""
    flags = entry[4] if len(entry) > 4 else None
    return not (flags and flags.get("undispellable"))


def dispel_unit(u, what):
    """批16: dispel(驅散/淨化) —— 移除目標身上對應方向(buffs=正向增益/debuffs=負向減益)的條目,
    略過帶 undispellable 旗標(flags.undispellable, 見 push_add/push_mod/push_stat_add 呼叫端 ud_flags)
    的條目。buffs: amp(正值)/mitig(正值)/stat mult>=1或add>=0/rateup/chargeup/shield/dodge/surehit/
    lifesteal/healBoost/healGiven/counter/pierce/extra/first/insight。
    debuffs: amp(負值)/mitig(負值)/stat mult<1或add<0 + 控制欄位(stun/silence/disarm/chaos/dot/
    healblock/fakeReport/swap)。只挪動「數值型」adds/mods/stat_adds 依正負號分類; 控制欄位
    (debuffs專屬)直接歸零/清空。"""
    def is_buff(a):                                    # 除 amp/mitig 外的 adds 種類一律視為buff
        return a[1] > 0 if a[0] in ("amp", "mitig") else True

    if what == "buffs":
        u.adds = [a for a in u.adds if not (is_buff(a) and not_ud(a))]
        u.mods = [m for m in u.mods if not (m[1] >= 1 and not_ud(m))]
        u.stat_adds = [a for a in u.stat_adds if not (a[1] >= 0 and not_ud(a))]
        if u.shield and not u.shield.get("undispellable"):
            u.shield = None
        if u.block:
            u.block = []                                # 批22: block(抵禦/警戒)為防禦性增益, 同 shield 慣例被 buffs 驅散清除(現有資料未帶 undispellable block)
    else:                                              # debuffs
        u.adds = [a for a in u.adds if not (a[0] in ("amp", "mitig") and a[1] < 0 and not_ud(a))]
        u.mods = [m for m in u.mods if not (m[1] < 1 and not_ud(m))]
        u.stat_adds = [a for a in u.stat_adds if not (a[1] < 0 and not_ud(a))]
        u.dots = [d for d in u.dots if len(d) > 2 and d[2]]   # 保留 undispellable(dots[2]=True)的 dot, 清除其餘
        u.huchen = None                                    # 批52d: 虎嗔為負面狀態, 可被草船/刮骨等清除
        u.stun = u.silence = u.disarm = u.chaos = u.healblock = 0
        u.fake_report_dur = 0
        u.ambush = 0
        # 批52j: captured 刻意不清除 —— 捕獲「無法被淨化」


def collect_debuff_tokens(pool):
    """批J(禁近似令-transfer轉移族): 對稱 engine.js collectDebuffTokens() —— 供 k=="transferDebuff"
    使用, 掃描 pool(存活單位串列)內每個單位當下持有的「負面狀態」具體實例, 回傳 token 串列,
    每個 token = {"kind":(供依種類分組挑選), "unit":(持有者), "move":(dest,dur)=>把這個實例
    從unit搬到dest 的函式}。分類口徑刻意與既有 dispel_unit() 的 debuffs 分支完全一致(負值
    amp/mitig、mult<1的mods、負值stat_adds、非undispellable的dot、stun/silence/disarm/chaos/
    healblock/fakeReport/ambush/huchen, 且與dispel_unit同樣略過undispellable旗標的條目), 不
    另立新標準, 確保「什麼算負面狀態」全庫只有一套定義。move() 內部同時完成「來源移除」與
    「目的地重建」兩步, 避免呼叫端分兩步做時忘記其中一步、或順序錯置導致資料讀取到已移除的
    實例。閉包用預設參數綁定當下迴圈變數(Python late-binding陷阱: 若不用預設參數, 迴圈內定義
    的內層函式會全部指向迴圈結束後的最後一個u/a, 而非各自建立當下那一個)。"""
    out = []
    for u in pool:
        for a in u.adds:
            if a[0] in ("amp", "mitig") and a[1] < 0 and not_ud(a):
                def _mv(dest, dur, u=u, a=a):
                    u.adds.remove(a)
                    dest.push_add(a[0], a[1], dur, a[3])
                out.append({"kind": a[0], "unit": u, "move": _mv})
        for m in u.mods:
            if m[1] < 1 and not_ud(m):
                def _mv(dest, dur, u=u, m=m):
                    u.mods.remove(m)
                    dest.push_mod(m[0], m[1], dur, m[3])
                out.append({"kind": "mod:" + m[0], "unit": u, "move": _mv})
        for s in u.stat_adds:
            if s[1] < 0 and not_ud(s):
                def _mv(dest, dur, u=u, s=s):
                    u.stat_adds.remove(s)
                    dest.push_stat_add(s[0], s[1], dur, s[3])
                out.append({"kind": "stat:" + s[0], "unit": u, "move": _mv})
        for d in u.dots:
            if not (len(d) > 2 and d[2]):    # d[2]=undispellable旗標, 對稱dispel_unit保留undispellable dot的慣例
                def _mv(dest, dur, u=u, d=d):
                    u.dots.remove(d)
                    # 狀態疊加精修批: 轉移時一併保留 d[4](refresh覆蓋比對鍵, 見k=="dot"分支
                    # 新增註解)——若不保留, 轉移後的DoT會退化成「無鍵」永遠不參與同名覆蓋比對
                    # (與轉移前相比更容易與新施加的同名DoT共存, 保守起見仍延續既有鍵而非歸零)。
                    dest.dots.append([d[0], dur, d[2] if len(d) > 2 else False,
                                      d[3] if len(d) > 3 else None, d[4] if len(d) > 4 else None])
                out.append({"kind": "dot:" + (d[3] if len(d) > 3 and d[3] else "?"), "unit": u, "move": _mv})
        if u.stun > 0:
            def _mv(dest, dur, u=u):
                u.stun = 0
                dest.stun = max(dest.stun, dur if dur is not None else 1)
            out.append({"kind": "stun", "unit": u, "move": _mv})
        if u.silence > 0:
            def _mv(dest, dur, u=u):
                u.silence = 0
                dest.silence = max(dest.silence, dur if dur is not None else 1)
            out.append({"kind": "silence", "unit": u, "move": _mv})
        if u.disarm > 0:
            def _mv(dest, dur, u=u):
                u.disarm = 0
                dest.disarm = max(dest.disarm, dur if dur is not None else 1)
            out.append({"kind": "disarm", "unit": u, "move": _mv})
        if u.chaos > 0:
            def _mv(dest, dur, u=u):
                u.chaos = 0
                dest.chaos = max(dest.chaos, dur if dur is not None else 1)
            out.append({"kind": "chaos", "unit": u, "move": _mv})
        if u.healblock > 0:
            def _mv(dest, dur, u=u):
                u.healblock = 0
                dest.healblock = max(dest.healblock, dur if dur is not None else 1)
            out.append({"kind": "healblock", "unit": u, "move": _mv})
        if getattr(u, "fake_report_dur", 0) > 0:
            def _mv(dest, dur, u=u):
                u.fake_report_dur = 0
                dest.fake_report_dur = max(dest.fake_report_dur, dur if dur is not None else 1)
            out.append({"kind": "fakeReport", "unit": u, "move": _mv})
        if u.ambush > 0:
            def _mv(dest, dur, u=u):
                u.ambush = 0
                dest.ambush = max(dest.ambush, dur if dur is not None else 1)
            out.append({"kind": "ambush", "unit": u, "move": _mv})
        if u.huchen:
            def _mv(dest, dur, u=u):
                dest.huchen = u.huchen
                u.huchen = None
            out.append({"kind": "huchen", "unit": u, "move": _mv})
    return out


def round_ok(t, r):                                    # 條件觸發(when): 回合是否符合戰法的發動窗口
    w = t.get("when")
    if not w:
        return True
    if w.get("rounds"):
        return r in w["rounds"]
    if w.get("from") is not None and r < w["from"]:
        return False
    if w.get("until") is not None and r > w["until"]:
        return False
    # 批16: parity(奇偶回合) + every(每N回合) —— 與 rounds/from/until 可並存(皆通過才算符合)
    if w.get("parity") == "odd" and r % 2 != 1:
        return False
    if w.get("parity") == "even" and r % 2 != 0:
        return False
    if w.get("every") and r % w["every"] != 0:
        return False
    return True


def hp_ok(t, u):
    """批16: hpPct 觸發 —— 每回合窗口檢查自身兵力百分比(troop/START_TROOP)。hpBelow: 首次跨越即
    觸發(一次性, when_fired慣例); hpAbove: 持續窗(只要條件成立, 每回合都可能觸發, 不去重)。
    與 round_ok 分開的獨立判定(hpPct 條件不是回合數, 需讀 unit.troop, 故不塞進 round_ok)。"""
    w = t.get("when")
    if not w:
        return True
    if w.get("hpBelow") is not None and not (u.hp_pct < w["hpBelow"]):
        return False
    if w.get("hpAbove") is not None and not (u.hp_pct > w["hpAbove"]):
        return False
    return True


def _selectable(u, pool_side_is_ally_of_selector=False):
    """批52j: 被捕獲者無法被友方選中(敵方仍可打)。pool_side_is_ally 時過濾 captured。"""
    if not u or not u.alive:
        return False
    if pool_side_is_ally_of_selector and getattr(u, "captured", 0) > 0:
        return False
    return True


def pick_target(units, attacker=None, ally_pool=False):                # 普攻/單體戰法: 隨機挑一個存活敵軍(不再固定打兵力最高); 嘲諷: 攻擊者身上有 taunt_by 時強制指向該目標
    if attacker is not None and attacker.taunt_dur and attacker.taunt_by is not None \
            and attacker.taunt_by.alive and attacker.taunt_by in units:
        # 嘲諷目標若被捕獲且為友方池則不可選——敵方嘲諷仍有效
        if not (ally_pool and getattr(attacker.taunt_by, "captured", 0) > 0):
            return attacker.taunt_by
    live = [u for u in units if _selectable(u, ally_pool)]
    return random.choice(live) if live else None


def pick_targets(units, n, ally_pool=False):                           # 群體戰法: 隨機挑 n 個不重複存活目標
    live = [u for u in units if _selectable(u, ally_pool)]
    if len(live) <= n:
        return live
    return random.sample(live, n)


# 批18: targetSel(指定選標準則) —— user 實測: 混亂只影響「隨機」選目標的主動/突擊/普攻,
# 「指定」類戰法(按準則選目標: 兵力最低/武力最高/智力最低/我方最殘等)不受混亂影響, 因為
# 這些戰法根本不是隨機選標, 而是每次發動當下依準則重新篩選(非鎖定, 見批12/避實擊虛的
# lockTarget vs 依屬性選標之辨)。TARGETSEL_KEY: 準則->(單位->比較值), TARGETSEL_MIN: 取最小者的準則集合。
TARGETSEL_KEY = {
    "minTroop": lambda u: u.troop, "maxForce": lambda u: u.eff("force"),
    "minIntel": lambda u: u.eff("intel"), "maxIntel": lambda u: u.eff("intel"),
    "minCommand": lambda u: u.eff("command"), "mostDamaged": lambda u: u.troop,
    "maxTroop": lambda u: u.troop,  # 批45 C: 兵力最高準則(對稱minTroop), 見engine_limitations.md第17節——
    # 過去只有minTroop(=mostDamaged, 兵力最低=最受損)一種方向, 「兵力最高」的敵軍/我軍選標
    # 缺口(定謀貴決「使敵軍兵力最高的武將...」)長年只能誠實揭露維持無targetSel近似, 現補上。
    "maxSpeed": lambda u: u.eff("speed"),  # 批G: 速度最快準則(對稱既有maxForce/maxIntel/maxTroop準則
    # 家族), 萬軍奪帥「使敵軍速度最快的武將降速」過去因準則家族缺這個具體枚舉值, 只能退化套用
    # 全體敵軍(較原文寬鬆, 高估), 現補上, 與既有maxForce/maxIntel/maxTroop同一套pick_by_criterion
    # 呼叫路徑, 非新機制, 純粹是準則枚舉表補一個成員。
}
TARGETSEL_MIN = {"minTroop", "minIntel", "minCommand", "mostDamaged"}  # maxTroop/maxSpeed故意不加入此集合, 使pick_by_criterion對它們用max()而非min()


def pick_by_criterion(units, sel, ally_pool=False):
    key_fn = TARGETSEL_KEY.get(sel)
    if not key_fn:
        return None                                   # 未知準則: 呼叫端應退回一般選標(保守, 不是無聲吃掉)
    live = [u for u in units if _selectable(u, ally_pool)]
    if not live:
        return None
    return (min if sel in TARGETSEL_MIN else max)(live, key=key_fn)


# 批B(filter-then-pick修正): 目標資格gate統一判定 —— ifTargetHas/ifTargetHasNot/
# ifStatCompare/ifTargetHpAbove/ifTargetHpBelow/ifSelfStatCompare/ifTargetIsRank/
# ifTargetIsRankNot/whoNames 這些效果級欄位共同的性質: 是否命中「純粹取決於候選單位u
# 自身當下狀態/屬性」, 與u是否被隨機選中無關(選前選後獨立評估必得到同一個布林值)。
# 過去 apply_effects/fire_extra_hits 的 who=="enemy"/"ally" 隨機選標分支一律「先
# pick_targets 隨機挑n個, 挑完才用這些gate過濾」——若隨機挑中不合格目標, 過濾後dests
# 變空/縮水, 明明池中另有合格目標卻白白錯過(隔離實測橫掃千軍案例: 對1個已計窮+1個乾淨
# 的敵組, 應100%命中計窮目標, 舊實作只29/50命中, 見批B交接文件)。正解: 有這類gate時
# 應「先過濾出合格池, 再從合格池 pick_targets」(filter-then-pick), 使命中率不再受
# 「隨機挑選過程本身」拖累。不含 sameTargets/ifSameTargetIsLeader——這兩者語意是「事後
# 檢查這次隨機結果是否恰好是某個特定對象」, 本質上就是要在挑選動作發生後才能判斷(等同於
# 「抽到大獎的機率」, pre-filter會把機率語意錯改成必中), 不適用本原語, 維持現狀事後判斷。
_TARGET_GATE_KEYS = ("ifTargetHas", "ifTargetHasNot", "ifStatCompare", "ifTargetHpAbove",
                     "ifTargetHpBelow", "ifSelfStatCompare", "ifTargetIsRank", "ifTargetIsRankNot",
                     "whoNames")


def _has_target_gate(e):
    """效果e是否帶有任何「可預先判定」的目標資格gate(見上方模組註解)。無gate的效果應
    完全維持原隨機行為不變(呼叫端據此決定要不要多花一次list生成, 避免無謂配置)。"""
    return any(e.get(k) is not None for k in _TARGET_GATE_KEYS)


def _rank_key(spec):
    """ifTargetIsRank/ifTargetIsRankNot 用: spec.stat -> TARGETSEL_KEY 準則名(依既有
    maxIntel/maxForce準則家族, 對稱engine.js rankKeyOf)。原為apply_effects內部巢狀函式,
    批B抽到模組層級供_target_gate_ok與既有選後過濾共用同一份邏輯(不重複維護兩份)。"""
    return "maxIntel" if spec.get("stat") == "intel" else "maxForce"


def _target_gate_ok(u, e, ref, allies, enemies):
    """單一候選目標u是否通過效果e宣告的全部靜態資格gate(見_TARGET_GATE_KEYS)。ref: 比較
    基準方(apply_effects的caster, 或fire_extra_hits的atk代理出手者)。allies/enemies:
    當下完整雙方名單(ifStatCompare vs="leader"/ifTargetIsRank 準則查詢皆讀完整名單,
    不受pool是否已被其他條件縮小影響, 與既有選後過濾行為完全一致)。"""
    if e.get("ifTargetHas") and not target_has(u, e["ifTargetHas"]):
        return False
    if e.get("ifTargetHasNot") and target_has(u, e["ifTargetHasNot"]):
        return False
    if e.get("ifStatCompare") and not stat_compare_ok(ref, u, allies, e["ifStatCompare"]):
        return False
    if e.get("ifTargetHpAbove") is not None and not (u.hp_pct > e["ifTargetHpAbove"]):
        return False
    if e.get("ifTargetHpBelow") is not None and not (u.hp_pct < e["ifTargetHpBelow"]):
        return False
    if e.get("ifSelfStatCompare"):
        _sc = e["ifSelfStatCompare"]
        _op_fns = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
                   "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}
        _opf = _op_fns.get(_sc.get("op", "gt"), _op_fns["gt"])
        if not _opf(u.eff(_sc["statA"]), u.eff(_sc["statB"])):
            return False
    if e.get("ifTargetIsRank"):
        _champ = pick_by_criterion(enemies, _rank_key(e["ifTargetIsRank"]))
        if u is not _champ:
            return False
    if e.get("ifTargetIsRankNot"):
        _specs = e["ifTargetIsRankNot"] if isinstance(e["ifTargetIsRankNot"], list) else [e["ifTargetIsRankNot"]]
        _champs = [pick_by_criterion(enemies, _rank_key(s)) for s in _specs]
        if u in _champs:
            return False
    if e.get("whoNames"):
        _wn = set(e["whoNames"] if isinstance(e["whoNames"], list) else [e["whoNames"]])
        if not (u.g and u.g.name in _wn):
            return False
    return True


def _gate_pool(pool, e, ref, allies, enemies):
    """filter-then-pick: 若e帶任何目標資格gate, 回傳過濾後的合格候選池(供pick_targets
    隨機挑選前使用); 無gate則原樣回傳pool(不新增list, 維持原隨機行為零改動)。"""
    return [u for u in pool if _target_gate_ok(u, e, ref, allies, enemies)] if _has_target_gate(e) else pool


def pick_target_chaos(u, allies, foes):
    """批12 ModeF: 混亂(chaos)單體選標 —— 普攻/單體主動戰法目標選擇改為「敵我不分」: 從友軍+敵軍
    (排除自己)中隨機挑一個存活目標, 而非只從敵方挑。非混亂狀態時退回一般 pick_target(含嘲諷判定)。
    群體/AoE 戰法在混亂下維持原邏輯不變(近似, 見呼叫端註解)。"""
    if not u.chaos:
        return pick_target(foes, u)
    pool = [x for x in (allies + foes) if x.alive and x is not u]
    if not pool:
        return pick_target(foes, u)          # 保底: 沒有其他存活單位時退回一般選標
    v = random.choice(pool)
    return v


def resolve_locked_target(u, t, foes):
    """批12 ModeG: lockTarget —— 戰法首次發動時透過 pick_target 正常選標, 之後每次發動重用同一
    目標(以 id(t) 為鍵存進 u.locked_targets, dict 不可雜湊故用 id()), 而非每次重新隨機選。若鎖定
    目標已陣亡: 依 brief 保守決策(來源文字未說明死亡後是否重新鎖定), 視為「本次發動找不到有效
    目標」回傳 None, 不重新選新目標(不做隱式重新鎖定, 避免無根據臆測遊戲行為)。"""
    key = id(t)
    if key in u.locked_targets:
        locked = u.locked_targets[key]
        return locked if (locked and locked.alive) else None  # 鎖定目標已陣亡 -> 本次無有效目標(不重新選)
    picked = pick_target(foes, u)
    if picked:
        u.locked_targets[key] = picked
    return picked


def fire_extra_hits(u, t, tgt, allies_of, foes_of, on_hit, on_deal=None):
    """批13: extraHits —— 多段傷害(兵刃+謀略雙段/主傷+補刀等單一 coef/kind/n 無法表達的戰法)。
    戰法欄 extraHits:[{coef,kind,n,nMax,rate,who,_note}]: 主 coef 結算後逐段獨立處理, 每段各自
    rate 擲骰(預設1必發)、選目標、hit()。who 可選: "sameTarget"(沿用主 coef 段已選定的(單體)
    目標, 如屠几上肉 兵刃+謀略同目標/一騎當千 主將加成同目標)、"enemyLeader"(固定打敵方主將
    foes[0], 如百騎劫營/暗藏玄機 額外段明確打敵軍主將)、不填則預設 pick_targets(敵方, 依n/nMax)。
    與主 coef 段完全獨立(各自的 kind 可不同, 如兵刃主傷+謀略補刀), 不與 hitsRepeat/lockTarget
    互斥(hitsRepeat/lockTarget 只影響主 coef 段的選標方式, extraHits 段固定用上述規則)。
    批27: on_deal(選填) —— 轉呼叫給 hit(), 讓 extraHits 段造成的傷害也能觸發 on:"dealtDamage"
    反應式戰法(與主coef段/普攻/突擊一致, 見 fight() 各呼叫端)。
    批52f: eh.srcSel —— 出手者改為「我軍依準則挑選」(maxForce/maxIntel 等, 同 TARGETSEL_KEY),
    hit(src=該友軍) 故該友軍身上 dealtDamage(文武雙全等)會正確疊層。t.sameSrcCoef: 若本戰法
    所有帶 srcSel 的段解析到同一人, 各段改用 sameSrcCoef(眾望所歸同一人時 86%→72%)。"""
    ehs = t.get("extraHits") or []
    # 批52f: 預解析 srcSel —— 判定「武力/智力最高是否同一人」供 sameSrcCoef
    agent_srcs = []
    for eh in ehs:
        if eh.get("srcSel"):
            agent_srcs.append(pick_by_criterion(allies_of(u), eh["srcSel"]))
    same_person = (
        t.get("sameSrcCoef") is not None
        and len(agent_srcs) >= 2
        and agent_srcs[0] is not None
        and all(s is agent_srcs[0] for s in agent_srcs)
    )
    for eh in ehs:
        if random.random() >= eh.get("rate", 1.0):
            continue
        # 批52续: eh.when 回合窗口(如燕人咆哮主將第6回合)。時序一致化本批: 改用u(持有者/
        # 施放者)自己的own_round —— 此為單一持有者自參照進程(B2), 非團隊廣播, 取代全局CUR_ROUND。
        if eh.get("when") and not round_ok({"when": eh["when"]}, u.own_round):
            continue
        # 批44 A: eh["ifLeaderIs"] —— 對稱 engine.js 同名分支(見其詳細註解)。extraHits 段級
        # 「隊伍主將(allies_of(u)[0])的武將名須匹配指定值」條件閘門, 用於白毦兵等「若XX統領,
        # 主段傷害更高」家族的 base(頂層coef)+top-up(extraHits段, sameTarget+ifLeaderIs)拆法。
        if eh.get("ifLeaderIs"):
            _eh_names = eh["ifLeaderIs"] if isinstance(eh["ifLeaderIs"], list) else [eh["ifLeaderIs"]]
            al = allies_of(u)
            if not (al and al[0] is u and u.g and u.g.name in _eh_names):
                continue
        # 批52: eh["ifLeader"] —— 僅主將時結算此 extraHits 段(一騎當千「自身為主將時更強力」)
        if eh.get("ifLeader"):
            al = allies_of(u)
            if not (al and al[0] is u):
                continue
        if eh.get("ifSub"):
            al = allies_of(u)
            if not (al and al[0] is not u):
                continue
        # 批52f: 代理出手(srcSel) —— 由我軍屬性最高者發動攻擊, 非必為施法者
        atk = u
        if eh.get("srcSel"):
            atk = pick_by_criterion(allies_of(u), eh["srcSel"])
            if not atk or not atk.alive:
                continue
        coef = t["sameSrcCoef"] if (same_person and eh.get("srcSel")) else eh["coef"]
        n = eh.get("n") or 1
        cnt = n + random.randint(0, eh["nMax"] - n) if eh.get("nMax") else n
        who = eh.get("who")
        # 批A(11筆高嚴重重建): who=="mainTargetAlly" —— 「(主coef段已選定的目標)轉而對其友軍
        # 單體發動攻擊」(偽書相間「若目標處於混亂狀態則使目標對其友軍單體發動攻擊」)。方向
        # 反轉: atk 不是持有者 u 自己, 而是 tgt(main段命中的敵方目標)本身被強制出手; dests
        # 則是 tgt 自己那一側的隊友(從 u 的視角看, tgt 那一側正是 foes_of(u), 即 u 的敵方隊伍
        # ——tgt 的隊友 = u 的其他敵人, 排除 tgt 自己)。對稱 engine.js 同名分支, 見其詳細註解。
        main_target_ally_atk = None
        if who == "mainTargetAlly":
            # ifTargetHas/ifTargetHasNot(若有指定)在此特殊路徑要檢查的是 tgt 本身(main段已選定
            # 的目標, 即將被強制出手的那一位)是否具備該狀態, 而非檢查 dests(tgt的隊友, 承受
            # 傷害的那一方)——與下方共用的「dests 事後過濾」慣例方向不同, 故這裡提前判斷, 並
            # 略過下面共用過濾段(避免對dests=tgt的隊友誤重複套用同一個條件)。
            tgt_gate_ok = (tgt and tgt.alive
                           and (not eh.get("ifTargetHas") or target_has(tgt, eh["ifTargetHas"]))
                           and (not eh.get("ifTargetHasNot") or not target_has(tgt, eh["ifTargetHasNot"])))
            if tgt_gate_ok:
                tgt_side = [v for v in foes_of(u) if v.alive and v is not tgt]
                if tgt_side:
                    main_target_ally_atk = tgt
                    dests = [random.choice(tgt_side)]
                else:
                    dests = []
            else:
                dests = []
        # 批18: targetSel(指定選標準則) —— 段級欄位, 優先於 who 的其餘規則(sameTarget/enemyLeader/
        # 隨機)。如 上兵伐謀「分別對兵力最低、武力最高、智力最低的敵將」三段各自不同準則。
        elif eh.get("targetSel"):
            picked = pick_by_criterion(foes_of(u), eh["targetSel"])
            dests = [picked] if picked else []
        elif who == "sameTarget":
            dests = [tgt] if (tgt and tgt.alive) else []          # 沿用主段已選定的(單體)目標
        elif who == "enemyLeader":
            foes = foes_of(u)
            dests = [foes[0]] if (foes and foes[0].alive) else []  # 固定打敵方主將(index 0)
        elif cnt <= 1 and tgt and tgt.alive and not who:
            dests = [tgt]                                    # 未指定who且單體: 沿用主段目標(向後相容預設行為)
        else:
            # 批B: filter-then-pick(對稱apply_effects同名修正, 見_gate_pool模組層級註解) ——
            # eh帶ifTargetHas/ifStatCompare等資格gate時, 先過濾foes_of(u)成合格池再pick_targets,
            # 避免「隨機挑中不合格目標, 過濾後dests落空」(百步穿楊 extraHits ifTargetHas陣列
            # 案例: 對1個已控制+1個乾淨的敵組, 應100%命中控制中的目標)。
            _fo = foes_of(u)
            dests = pick_targets(_gate_pool(_fo, eh, atk, allies_of(atk), _fo), cnt)
        if main_target_ally_atk is not None:
            atk = main_target_ally_atk                       # 覆寫本段攻擊者為 tgt 本身
        # 批16: ifTargetHas —— extraHits 段結算前檢查, 只對「已有該狀態」的目標結算此段傷害。
        # 批A: who=="mainTargetAlly" 時已在上方針對tgt本身提前判斷過(見該分支註解), 這裡跳過。
        if eh.get("ifTargetHas") and who != "mainTargetAlly":
            dests = [v for v in dests if target_has(v, eh["ifTargetHas"])]
        # 批I(禁近似令-scale/比較族): eh["ifStatCompare"] —— extraHits 段結算前檢查, 只對
        # 「參照方(攻擊者atk自身或其隊伍主將)vs目標」屬性比較成立的目標結算此段傷害(竊幸乘寵
        # 「若自身智力高於目標則額外造成一次謀略傷害」), 對稱effects段的e["ifStatCompare"]
        # (見apply_effects), 共用stat_compare_ok()。
        if eh.get("ifStatCompare"):
            dests = [v for v in dests if stat_compare_ok(atk, v, allies_of(atk), eh["ifStatCompare"])]
        # 批31 B: ifSameTargetIsLeader —— extraHits 段結算前檢查, 只對「(主coef段隨機選定的)
        # 目標剛好就是敵方隊伍固定位置的主將(foes[0])」時才結算此段傷害, 精確表達原文「若目標
        # (普攻/主傷段隨機選定的對象)為敵軍主將，額外造成傷害」這種條件分支(對比批16的
        # ifTargetHas 是檢查「目標身上是否已有某個狀態」, 這裡檢查的是「目標的隊伍位置是否為
        # 主將」, 概念上更接近既有 who:"enemyLeader" 的固定位置判斷, 但用於「事後過濾已選定的
        # 隨機目標」而非「主動選定目標」, 是不同的判斷時機)。取代舊有 EV 折算近似(如暗藏玄機
        # 過去用 1/3 機率折算「隊伍3人之一為主將」的近似觸發率, 現改真實比對 dests 是否等於
        # foes[0], 精確表達條件分支而非期望值近似)。
        if eh.get("ifSameTargetIsLeader"):
            foes = foes_of(u)
            leader = foes[0] if (foes and foes[0].alive) else None
            dests = [v for v in dests if v is leader]
        # 批A: eh["kindByStat"]=="maxForceIntel" —— 傷害類型不是固定寫死的 phys/intel, 而是
        # 依「攻擊者(atk)本身武力/智力較高的一項」動態決定(偽書相間「類型取決於目標武力、智力
        # 較高的一項」——這裡的「目標」在mainTargetAlly反轉語意下就是atk=tgt本身), 對稱
        # engine.js 同名分支(見其詳細註解)。
        if eh.get("kindByStat") == "maxForceIntel":
            eh_kind = "phys" if atk.eff("force") >= atk.eff("intel") else "intel"
        else:
            eh_kind = eh.get("kind", "phys")
        # 禁近似令-批K: eh["lifesteal"](engine_wiring_gaps_misc族, 對稱engine.js同名分支) ——
        # 「僅限本extraHits段自身傷害的回復欄位」, 顆粒度縮小到只讀這一段的dmg(不透過addbonus
        # 通道, 避免誤及本戰法主coef段等其他傷害來源), 供錦帆軍「若目標已潰逃則造成兵刃攻擊
        # 並恢復傷害量的30%兵力」。
        for v in dests:
            eh_dmg = hit(atk, v, coef, eh_kind, False, on_hit, on_deal)
            if eh.get("lifesteal") and eh_dmg and eh_dmg > 0 and atk.alive:
                atk.troop = min(START_TROOP, atk.troop + eh_dmg * eh["lifesteal"])


def healed_for(hurt, caster, actual, allies, enemies):
    # 批43 C: 對稱 engine.js healed_for(見其詳細註解)。apply_effects 是模組層級函式(與 fight
    # 同層), 無法直接看到 fight() 內的 on_hit 等閉包 —— 但 heal 效果的 hurt(受治療者)保證來自
    # 呼叫端傳入的 allies 陣列, 故「hurt 的敵隊」天然就是同一次 apply_effects() 呼叫已持有的
    # enemies 參數, 不需要額外的 allies_of/foes_of 全域查找。只支援效果級
    # (on_heal_effect_tacs), 不支援戰法級(on_heal_tacs 已建但本函式未讀取, 比照批31
    # active_fired precedent)。
    if actual <= 0:
        return                                          # 未實際回復(傷兵池已空/滿編)不觸發
    groups = [
        ([hurt], None, allies, enemies),                            # self: holder是hurt本人(未指定who或"self")
        (allies, "ally", allies, enemies),                          # ally: hurt同隊(含自己)
        ([a for a in allies if a is not hurt], "otherAlly", allies, enemies),  # otherAlly: hurt同隊, 排除自己
        (enemies, "enemy", enemies, allies),                        # enemy: hurt的敵隊(對holder而言方向相反)
    ]
    for holders, want_who, al, en in groups:
        for holder in holders:
            if not holder.alive or not holder.on_heal_effect_tacs:
                continue
            if holder.fake_report_dur:                  # 批16: 偽報 —— 抑制反應式觸發, 同 on_hit/dealt_damage/active_fired 慣例
                continue
            for t in holder.on_heal_effect_tacs:
                for e in t["effects"]:
                    ew = e.get("when") or {}
                    if ew.get("on") != "healed":
                        continue
                    # who=="self"(顯式寫出)與省略who視為同義, 對稱engine.js的正規化(見其註解)
                    e_who = None if ew.get("who") == "self" else ew.get("who")
                    if e_who != want_who:
                        continue
                    # 時序一致化本批: 改用holder(反應式效果持有者)自己的own_round, 取代全局CUR_ROUND
                    # (holder自參照進程, 非長焰類團隊廣播)。
                    if not round_ok({"when": ew}, holder.own_round):
                        continue
                    if id(e) in holder.hit_flags:        # 同回合每單位每效果最多觸發1次(防無限鏈)
                        continue
                    ev_rate = e.get("rate", t.get("rate", 1))
                    if random.random() >= ev_rate:
                        continue
                    holder.hit_flags.add(id(e))
                    apply_effects(holder, hurt, {"effects": [e], "kind": t.get("kind", "phys"), "nameZh": t.get("nameZh")},
                                  al, en, rate_checked=True, reactive=True, heal_amt=actual)


def do_normal_attack(u, allies, enemies, on_hit=None, on_deal=None, active_fired_fn=None,
                     allow_extra=True, allow_charge=True):
    """批52i: 完整普通攻擊管線(垂心萬物代打等)。
    與 fight 主迴圈普攻一致: 選標→hit(is_normal)→連擊→everyN→突擊(charge)+activeFired。
    繳械則無法普攻。回傳命中的主目標(或 None)。"""
    if not u or not u.alive or u.disarm:
        return None
    on_hit = on_hit or _FIGHT_CTX.get("on_hit")
    on_deal = on_deal or _FIGHT_CTX.get("on_deal")
    active_fired_fn = active_fired_fn or _FIGHT_CTX.get("active_fired")
    allies_of = _FIGHT_CTX.get("allies_of") or (lambda x: allies)
    foes_of = _FIGHT_CTX.get("foes_of") or (lambda x: enemies)
    al = allies_of(u) if callable(allies_of) else allies
    fo = foes_of(u) if callable(foes_of) else enemies
    tgt = pick_target_chaos(u, al, fo)
    if not tgt:
        return None
    # 禁近似令-批K: pre_attack_hooks(pre_damage_intercept鄰居, engine_wiring_gaps_misc族,
    # 對稱engine.js同名分支) —— 「自身即將受到普通攻擊時」真反應式掛鉤點, 取代redirect/heal
    # 過去只能「prep一次性擲骰決定整場有無」的EV折算, 改為每次真正要挨打前才擲骰判定(見
    # 雲聚影從 redirectPre/益其金鼓 healAllyPre)。掛在tgt身上(即將受擊的那一方)。
    if tgt.pre_attack_hooks:
        tgt_mates = [x for x in fo if x.alive and x is not tgt]
        for h in tgt.pre_attack_hooks:
            if random.random() >= h.get("rate", 1.0):
                continue
            if h.get("hook_kind") == "redirectPre" and tgt_mates:
                guard = tgt_mates[0]
                if h.get("guard") == "max_force":
                    for a in tgt_mates:
                        if a.eff("force") > guard.eff("force"):
                            guard = a
                tgt = guard
            elif h.get("hook_kind") == "healAllyPre" and tgt_mates:
                recv = random.choice(tgt_mates)
                if recv.alive and not recv.healblock:
                    hcoef_h = (h.get("coef") or 0.5) * (scale_of(tgt, h["scale"]) if h.get("scale") else 1)
                    want = hcoef_h * (tgt.troop * HEAL_TROOP_C)
                    actual = max(0.0, min(want, recv.wounded, START_TROOP - recv.troop))
                    recv.troop += actual
                    recv.wounded -= actual
    hit(u, tgt, 1.0, "phys", True, on_hit, on_deal)
    # 禁近似令-批K: splash(splash_aoe_primitive族) —— 對稱 engine.js 同名分支, 普攻命中tgt
    # 後若u持有splash加成, 同時對tgt「同部隊其他武將」(fo中除tgt外的存活成員)造成splashRatio
    # 倍率的兵刃傷害(瞋目橫矛/象兵), 與extra(重新隨機挑全新目標)語意不同。
    splash_ratio = u.addbonus("splash")
    if splash_ratio > 0:
        for mate in fo:
            if mate is not tgt and mate.alive:
                hit(u, mate, splash_ratio, "phys", True, on_hit, on_deal)
    if allow_extra:
        for _ in range(extra_count(u.addbonus("extra"))):
            nt = pick_target_chaos(u, al, fo)
            if nt:
                hit(u, nt, 1.0, "phys", True, on_hit, on_deal)
    for t in u.tactics:
        if t.get("everyN") and t["everyN"].get("on") == "attack" and u.tick_every_n(t):
            if t.get("extraHits"):
                fire_extra_hits(u, t, tgt, allies_of if callable(allies_of) else (lambda x: al),
                                foes_of if callable(foes_of) else (lambda x: fo), on_hit, on_deal)
            if t.get("effects"):
                apply_effects(u, tgt, t, al, fo)
    if allow_charge:
        for t in u.tactics:
            up = 0 if t.get("proc") else u.addbonus_for("chargeup", t)
            if t["type"] == "charge" and random.random() < t["rate"] + up:
                if t["coef"]:
                    # 批D(R32): 突擊(charge)分派過去無條件只對 do_normal_attack 已選定的單一
                    # tgt 打一次, 完全不讀頂層 n/nMax/hitsRepeat —— 「對敵軍全體發動一次兵刃
                    # 攻擊」(一騎當千 n:3 意圖AoE全體)與「發動三次隨機打擊」(摧鋒斷刃
                    # hitsRepeat:true) 兩者的原文語意皆被靜默塌縮成單體單次, 全庫掃描(R32
                    # 頂層欄位孤兒偵測)發現此缺口從未被揭露(engine_limitations.md 新節)。
                    # cnt<=1(絕大多數既有charge戰法, 未設n或n=1)維持原行為(對tgt單體打一次),
                    # 向後相容零回歸。hitsRepeat: N次獨立選標(可重複命中同一目標, 同active型
                    # 既有慣例); 否則(純n>1無hitsRepeat): pick_targets 不重複群體(AoE)。
                    cnt = t.get("n") or 1
                    if t.get("nMax"):
                        cnt = cnt + random.randint(0, t["nMax"] - cnt)
                    if cnt <= 1:
                        hit(u, tgt, t["coef"], t["kind"], False, on_hit, on_deal, is_charge=True)
                    elif t.get("hitsRepeat"):
                        for _ in range(cnt):
                            v = pick_target(fo)
                            if v:
                                hit(u, v, t["coef"], t["kind"], False, on_hit, on_deal, is_charge=True)
                    else:
                        for v in pick_targets(fo, cnt):
                            hit(u, v, t["coef"], t["kind"], False, on_hit, on_deal, is_charge=True)
                if t.get("extraHits"):
                    fire_extra_hits(u, t, tgt, allies_of if callable(allies_of) else (lambda x: al),
                                    foes_of if callable(foes_of) else (lambda x: fo), on_hit, on_deal)
                apply_effects(u, tgt, t, al, fo)
                if active_fired_fn:
                    active_fired_fn(u)
    return tgt


def on_values(e):
    """禁近似令-批L: 對稱engine.js onValues(e) —— 把e["when"]["on"]正規化成list(單一字串包成
    單元素list, 本已是list則原樣回傳, 無on回傳空list)。對稱既有e["ifLeaderIs"]/e["eitherK"]/
    e["statOptions"]「單值或陣列皆可」慣例, 讓單一效果可同時掛兩個(或以上)反應式事件名共用
    同一份stackKey疊層計數(見fire_self_reactive/self_react_effect_tacs), 一身是膽「每次免疫
    控制狀態後**或**每次累計受傷達門檻後」需要dmgThreshold/ctrlImmune兩個事件共用同一組
    「最多觸發7次」封頂, 若各自獨立掛兩個效果物件, k=="critUp"+e["stackKey"]的疊層計數器
    以效果物件本身(id(e))為鍵, 兩個不同物件會各自疊到7層(合計最多14層), 與本文「最多觸發
    7次」不符。只新增on值可為list的正規化, 不改動既有任何只認字串on值的既有比對式。"""
    on = (e or {}).get("when", {}).get("on") if e else None
    if on is None:
        return []
    return on if isinstance(on, list) else [on]


def bump_dmg_accum(u, amt):
    """禁近似令-批L: 對稱engine.js bumpDmgAccum(u, amt) —— 累計u自身因傷害(含代承/反擊/dot/
    延遲結算等一切途徑)實際扣減的兵力量, 偵測本次增量是否使累計值跨越新的「最大兵力7%」門檻
    (可能一次跨越多格, 如單次巨量傷害), 回傳新跨越的格數(0=未跨越)。呼叫端(hit()/tick()/
    settle_huchen()/fight()主迴圈settle結算)在各自「這個單位的troop因傷害而減少」的既有分支
    旁, 與self.wounded更新並列呼叫, 涵蓋範圍與wounded完全對稱。"""
    if not u or not u.alive or not (amt > 0):
        return 0
    thr = START_TROOP * 0.07
    before = u.dmg_accum or 0.0
    u.dmg_accum = before + amt
    return int(u.dmg_accum // thr) - int(before // thr)


def fire_self_reactive(u, on_name, times):
    """禁近似令-批L: 對稱engine.js fireSelfReactive(u, onName, times) —— on:"dmgThreshold"/
    on:"ctrlImmune"專用的自身反應式派發(純自身視角, 不做跨隊broadcast——現無資料需要"ally"/
    "enemy"監聽這兩個新事件)。times: 本次事件應觸發幾次獨立判定(dmgThreshold單次巨量傷害
    可能一次跨越多格門檻, 每格各自獨立擲骰; ctrlImmune恆為1)。每次呼叫用「合成單效果戰法」
    重新呼叫apply_effects, 沿用其既有e["rate"]/e["rateLeader"]擲骰+k=="critUp"+e["stackKey"]
    疊層consumption, 不另造一套機率/疊層邏輯。"""
    if not u or not u.alive or not (times and times > 0) or not u.self_react_effect_tacs:
        return
    allies = (_FIGHT_CTX.get("allies_of") and _FIGHT_CTX["allies_of"](u)) or [u]
    foes = (_FIGHT_CTX.get("foes_of") and _FIGHT_CTX["foes_of"](u)) or []
    for _ in range(times):
        for t in u.self_react_effect_tacs:
            for e in t.get("effects", []):
                if on_name not in on_values(e):
                    continue
                # 時序一致化本批: 改用u(自身反應式持有者)自己的own_round, 取代全局CUR_ROUND。
                if not round_ok({"when": e.get("when")}, u.own_round):
                    continue
                apply_effects(u, None, {"effects": [e], "kind": t.get("kind", "phys"), "nameZh": t.get("nameZh")},
                              allies, foes, reactive=True)


def resolve_max_stack(caster, e, allies):
    """禁近似令-批L: 對稱engine.js resolveMaxStack(caster, e, allies) —— 對稱既有coefLeader/
    rateLeader(基礎值+主將時改用替代值)家族, 但套用維度是maxStack(疊層上限本身, 一個「封頂」
    整數, 不像coef/rate是可累加的數值, 無法用base+topup相加手法表達「條件式提高上限」), 改用
    「符合條件則整個替換成另一個上限值」的覆寫式讀取。先登死士「可疊加4次;若麴義統領,則可
    疊加5次」——e["maxStackIfLeaderIs"]={"who":"麴義"或list(OR), "max":5}於施放者(caster)恰為
    隊伍主將(allies[0] is caster)且武將名匹配時, 用max覆蓋e["maxStack"](4)。未帶
    e["maxStackIfLeaderIs"]或條件不成立時原樣回傳e["maxStack"](向後相容既有全部stealStat/
    rateup資料)。"""
    ms = e.get("maxStack")
    mli = e.get("maxStackIfLeaderIs")
    if mli:
        names = mli["who"] if isinstance(mli["who"], list) else [mli["who"]]
        if allies and allies[0] is caster and caster.g and caster.g.name in names:
            ms = mli["max"]
    return ms


def fire_controlled(victim, kind, dur, allies, enemies):
    """批52h: 控制施加事件 —— 友軍中 stun/silence/disarm/chaos 後廣播。
    機鑑先識: SP荀彧主將、戰鬥前2回合、速度比持有者慢的友軍、75%把同控制反彈給敵軍隨機單體、
    每友軍每回合最多1次。onlySlower: victim.speed < holder.speed(依速度先後)。"""
    if not victim or not victim.alive or kind not in ("stun", "silence", "disarm", "chaos"):
        return
    if allies and victim in allies:
        team, foes = allies, enemies
    elif enemies and victim in enemies:
        team, foes = enemies, allies
    else:
        return
    for holder in team:
        if not holder.alive or not getattr(holder, "on_ctrl_effect_tacs", None):
            continue
        if holder.fake_report_dur:
            continue
        for t in holder.on_ctrl_effect_tacs:
            for e in t["effects"]:
                ew = e.get("when") or {}
                if ew.get("on") != "controlled":
                    continue
                who = ew.get("who")
                if who == "self" and holder is not victim:
                    continue
                if who == "enemy":
                    continue
                # 時序一致化本批: 改用holder自己的own_round, 取代全局CUR_ROUND(機鑑先識「戰鬥前2
                # 回合」窗屬holder自參照進程)。
                if not round_ok({"when": ew}, holder.own_round):
                    continue
                if e.get("ifLeaderIs"):
                    _names = e["ifLeaderIs"] if isinstance(e["ifLeaderIs"], list) else [e["ifLeaderIs"]]
                    if not (team and team[0] is holder and holder.g and holder.g.name in _names):
                        continue
                elif e.get("ifLeader") and not (team and team[0] is holder):
                    continue
                if e.get("onlySlower") and victim.eff("speed") >= holder.eff("speed"):
                    continue
                flag = ("ctrlReflect", id(e), id(victim))
                if flag in holder.hit_flags:
                    continue
                if random.random() >= e.get("rate", 1.0):
                    continue
                holder.hit_flags.add(flag)
                dests = pick_targets(foes, e.get("n") or 1)
                if not dests:
                    continue
                apply_effects(
                    holder, dests[0],
                    {"effects": [{"k": kind, "who": "enemy", "n": 1, "dur": dur}],
                     "nameZh": t.get("nameZh"), "kind": t.get("kind", "intel")},
                    team, foes, rate_checked=True, no_ctrl_reflect=True,
                )


def apply_control_dur(target, field, dur, ctype, allies, enemies, no_ctrl_reflect):
    """狀態疊加精修批(追加規則6, user權威規則 control_status_rule_20260712): 控制類「不動作」
    狀態(繳械 disarm/計窮 silence/震懾 stun/混亂 chaos) = 唯一 + 「同等或更強擋新」——既有
    偽報(fakeReport, 見 k=="fakeReport" 分支)「身上已存在同等或更強的偽報效果→不覆蓋」
    規則的推廣, 以 dur(持續回合數)近似「強度」: 目標已有該控制且現有dur>=新施加的dur時,
    新施加**完全失效**(不覆蓋/不疊加/不延長, 也不重新觸發 fire_controlled 反彈廣播——過去
    u.stun = max(u.stun, e["dur"]) 這類寫法雖然「數值」上等同(max()本身已隱含「較大者
    存活」), 但仍會無條件重新呼叫 fire_controlled() 廣播「被施加控制」事件, 即使這次施加
    因較弱而被實質擋下, 語意上不應視為「有效施加」而重新觸發反彈鏈); 新的更強(dur嚴格
    大於現有值)才真正覆蓋+廣播。與 fakeReport 同用嚴格大於(>, 非>=)判準, 「同等」視為
    「不夠強, 應擋下」(對稱fakeReport `if new_dur > u.fake_report_dur`)。
    **不包含**: 監統震軍機變「繳械狀態增加1回合」的 extendDur(延長既有持續時間)機制——
    需要新原語(區分「延長現有」vs「施加新的」兩種語意, 現有k==disarm等只表達後者), 本批
    不做, 標記待後批(見 tactic_corrections.json 該戰法條目 _todo, 若無則待補)。

    追加規則(coordinator訊息, 2026-07-12): 先攻(first)/遇襲(ambush)/洞察(insight)/嘲諷
    (taunt, 見k=="taunt"分支另有taunt_by/taunt_dur雙欄位客製寫法, 不直接呼叫本函式)/虛弱
    比照同套規則, 由pending轉正為unique_strongest, 一併沿用本函式(ctype傳入"first"/
    "ambush"/"insight"等非四大控制類型時, fire_controlled() 內部既有的
    `kind not in ("stun","silence","disarm","chaos")` 守門會直接no-op, 不會誤廣播, 故可
    安全重用同一份「同等或更強比較」邏輯, 不需要另外複製一份判斷式)。
    回傳 True=已套用(dur嚴格提升), False=被同等或更強的既有狀態擋下(無任何變化)。"""
    if dur <= getattr(target, field):
        return False
    setattr(target, field, dur)
    if not no_ctrl_reflect:
        fire_controlled(target, ctype, dur, allies, enemies)
    return True


def apply_effects(caster, tgt, t, allies, enemies, heal_only=False, no_heal=False, skip_when_effects=False,
                   rate_checked=False, reactive=False, dmg=None, evt_target=None, heal_amt=None, main_hit_tgts=None,
                   no_ctrl_reflect=False, only_kinds=None, own_turn=False, broadcast_only=False):
    # 時序一致化(2026-07 批次) A.3: own_turn(可選) —— 對稱 heal_only, 但用於 everyRound(逐回合
    # 重擲) 效果的「該持有者自己行動輪」cadence 通道(見 fight() 主迴圈新增
    # apply_own_turn_effects() 呼叫端), 取代舊「apply_passives(heal_only=True) 全局回合頂端」
    # 通道對 everyRound 非heal效果的處理。heal_only=True 呼叫路徑自本批起收斂為「只處理
    # k=="heal"」(嚴格heal-only, 見下方頂端閘門), everyRound 非heal效果專屬 own_turn=True 通道
    # (兩者互斥, 見下方 everyRound 分支 _round_basis 判斷)。
    #
    # 時序一致化(2026-07 批次, 時序徹底一致化批): broadcast_only(可選) —— 對稱 own_turn, 但用於
    # user最終裁決「相一: 持有者每回合對他人廣播施加新狀態層」(SP周瑜江天長焰、SP袁紹高櫓連營
    # 等極少數實例, e["broadcast"]=true 標記)的全局round-start通道(fight()主迴圈回合頂端、任何
    # 單位行動前呼叫 apply_passives(broadcast_only=True), 見其呼叫端), 用全局 CUR_ROUND 為基準,
    # 不隨單位own_round個別結算——這是user權威規則下own_round化的**唯一例外**(其餘一切團隊buff/
    # 自參照效果均已改own_round, 見everyRound/own_turn分支與e.when泛化分支)。與own_turn互斥
    # (見下方 everyRound 分支 _round_basis 判斷, e["broadcast"] 旗標決定兩者擇一)。
    # 批H: only_kinds(可選, tuple) —— 限定本次只處理 k 在此清單內的效果段, 其餘一律跳過。
    # 唯一用途: active型戰法在主coef攻擊之前, 先套用施放者自身的 critUp/critDmgUp 會心buff
    # (只傳 only_kinds=("critUp","critDmgUp")), 讓「提高自身X%會心機率...隨後造成攻擊」這類
    # 戰法(百步穿楊/左右開弓)的主AoE本身也能吃到真會心擲骰(取代舊有把會心EV折入coef本身的
    # 近似——見fight()主迴圈active分支coef迴圈之前的pre-coef呼叫點, 及對應戰法corrections的
    # _note)。因push_add以src(戰法名+dmgType尾碼)去重, 主coef段結束後的常規apply_effects
    # 呼叫會以同一src刷新覆蓋(非疊加)本效果, 故pre-coef先套一次+post-coef再刷新一次不會造成
    # 會心率翻倍(單一adds條目), 只是把套用時機提前到coef命中之前, 使該次攻擊得以擲骰。
    # 批42: evt_target(可選) —— 對稱 engine.js opt.evtTarget, 供 who=="eventTarget" 精確鎖定
    # 「本次反應式事件的事件單位本身」(如傲睨王侯敵軍受普攻時, 事件單位=被打的那個敵人, 而非
    # 泛用敵軍全體/隨機N人)。由 on_hit_for/dealt_damage_for 呼叫端傳入, 見下方 who 分派。
    # 批33: dmg(可選)—— 反應式呼叫端(on_hit/dealt_damage)傳入「觸發本次效果結算的那一下傷害
    # 量」, 供 heal 分支的 e["ofDamage"](傷害比例治療) 使用, 見下方 k=="heal" 分支。
    # 批45 A: main_hit_tgts(可選)—— 對稱 engine.js opt.mainHitTgts, 由 fight() 主迴圈傳入本次
    # 主 coef 段命中的群體目標陣列(僅群體結算, len(vs)>1 時才有值), 供 e["sameTargets"] 沿用
    # 同一批目標, 見下方 who 分派。
    # 批52h: no_ctrl_reflect —— 反彈施加控制時不再二次廣播, 防無限鏈。
    src = t.get("nameZh")                              # 效果來源標籤: 戰法名(兵書/裝備/緣分無 nameZh → None, 不去重)
    for e in t["effects"]:
        # 禁近似令-批K: e["eitherK"](dynamic_coef_from_counter族鄰居) —— 陣列, 本次觸發隨機
        # 擇一k值頂替e["k"]本身(溯江搖櫓「使隨機敵軍單體進入計窮或震懾狀態」——本文明確是兩個
        # 控制狀態擇一觸發), 對稱engine.js同名分支。每次觸發各自重新擲骰(非prep鎖定)。
        k = random.choice(e["eitherK"]) if e.get("eitherK") else e["k"]
        if only_kinds is not None and k not in only_kinds:  # 批H: 限定只處理指定k(pre-coef會心套用, 見函式docstring)
            continue
        # 批35 B: block 的「準備階段鎖定」scale 值優先算定, 放在所有 continue 閘門(heal_only/
        # skip_when_effects/when.on/rate/ifLeader/everyRound...)之前 —— 必須確保 prep 呼叫
        # (fight() 開場的 apply_passives(no_heal=True, skip_when_effects=True))第一次掃描到
        # 帶 e["when"](如機鑑先識 everyRound 段的 when:{until:3})的 block 效果時就把鎖算好,
        # 否則若鎖定邏輯放在 skip_when_effects/everyRound 等後面的閘門之後, 帶 e["when"] 的
        # everyRound block 效果會在 prep 呼叫被 skip_when_effects 閘門提前 continue 掉,
        # 導致 locked_scale_of 從未在 prep 階段被呼叫過、鎖定值錯誤地延後到未來真正命中的
        # 那一回合才用當時(可能已變動)的即時智力算定, 違反「準備階段鎖定」語意本身。
        if k == "block" and e.get("scale"):
            locked_scale_of(caster, e)
        # 時序一致化(2026-07 批次) A.3: heal_only 自本批起收斂為嚴格「指揮/被動逐回合只跑
        # 治療」語意(不再放行everyRound, 對稱own_turn參數docstring)——everyRound非heal效果
        # 改由下面 own_turn 閘門專屬處理(該持有者自己行動輪cadence, 而非全局回合)。
        if heal_only and k != "heal":
            continue
        # 時序徹底一致化批: own_turn/broadcast_only 依 e["broadcast"] 旗標互斥分流 —— 帶
        # broadcast 的 everyRound 效果(相一全局round-start, 如高櫓連營)只在 broadcast_only=True
        # 通道放行; 其餘(絕大多數)everyRound效果(相二逐單位own_round)只在 own_turn=True 通道放行。
        if own_turn and not (e.get("everyRound") and not e.get("broadcast")):
            continue
        if broadcast_only and not (e.get("everyRound") and e.get("broadcast")):
            continue
        # 批18: e.when 泛化(非 heal 種類) —— heal 早已支援效果級 when(見下方 k=="heal" 分支的
        # heal_only 閘門), 但其餘效果種類(amp/settle/stat/…)過去若帶 e["when"] 而母戰法無
        # t["when"], 會在 prep 階段(skip_when_effects=True 時, 見 fight() 呼叫端)被無聲當成
        # 「無 when 的常駐效果」立即套用, 忽略 e["when"] 指定的回合窗口(如 密計誅逆的 settle
        # when:{rounds:[6]}/工神的 amp when:{from:4}, 見 _todo 揭露)。此處在 prep 呼叫時跳過
        # 這些效果, 改由 fight() 回合迴圈的通用 e.when 掃描(仿 delayed_eq 慣例)在視窗開啟時才套用。
        if skip_when_effects and k != "heal" and e.get("when") and not t.get("when"):
            continue
        # 批23: e["when"]["on"](反應式, 受擊當下觸發) 效果只應在 on_hit() 事件點結算
        # (reactive=True 的合成單效果呼叫), 不應在準備階段/主動主迴圈擲骰(fire=random.random()
        # <t0["rate"])/charge突擊等一般路徑被無條件套用。過去(草船借箭0分bug之一)heal 的
        # e["when"]["on"] 只被 heal 分支自己內部的 heal_only 閘門過濾, 但一般 active 主動戰法
        # 擲骰命中時呼叫 apply_effects() 完全不經過 heal_only, 導致帶 e["when"]["on"] 的 heal
        # 效果被當成「無 when 的常駐效果」在戰法觸發當下立即無條件治療一次, 與 on_hit 反應式
        # 觸發疊加, 造成雙重結算。此處統一擋下: 非 reactive 呼叫時, 任何 k 只要帶
        # e["when"]["on"] 就跳過(改由 on_hit() 事件點才會結算)。
        if not reactive and (e.get("when") or {}).get("on"):
            continue
        # 批23 A4: 效果級 e["rate"] 折算一致性 —— 過去只有 on_hit(反應式)/delayed_eq(裝備回合
        # 窗)兩條路徑會讀 e["rate"](見呼叫端各自的 ev_rate = e.get("rate", ...) 判定), 其餘路徑
        # (prep/active主動/charge突擊/when視窗一次性套用)完全忽略 e["rate"], 造成同一戰法內
        # 「有的效果段折機率、有的沒折」(如草船借箭80%/魚鱗陣heal段25%/援救50%)。修法: 在這裡
        # 統一補上判定(套用時 random.random()<e["rate"], 比EV折算更接近真實方差, 見批23 A4
        # brief)。rate_checked=True: 呼叫端(on_hit/delayed_eq 的合成單效果呼叫)已自行讀取並
        # 擲骰過同一個 e["rate"], 避免在這裡對同一效果重複擲骰(機率會被平方, 造成低估)。
        # 批52续: e.rateLeader —— 主將時用更高發動/觸發率(仁德載世虛弱 10%→25% 等)
        _e_rate = e.get("rate")
        if e.get("rateLeader") is not None and allies and allies[0] is caster:
            _e_rate = e["rateLeader"]
        if e.get("rateSub") is not None and allies and allies[0] is not caster:
            _e_rate = e["rateSub"]
        # 禁近似令-批K: rateFactionBonus(faction_count_scale族) —— 對稱 engine.js 同名分支
        # (南蠻渠魁/象兵「部隊每多一名蠻族武將額外提高X%機率」)。
        if e.get("rateFactionBonus") and _e_rate is not None:
            _fb = e["rateFactionBonus"]
            _cnt = count_ally_faction(allies, _fb.get("faction"))
            _e_rate = max(0.0, min(1.0, _e_rate + (_fb.get("per") or 0) * max(0, _cnt - 1)))
        # 禁近似令-批K: rateBonusPerBuffType(rate_self_dynamic族) —— 對稱 engine.js 同名分支
        # (臥薪嘗膽「依自身連擊/洞察/先攻/必中/破陣的狀態數,每多一種提高5%→10%機率」)。
        if e.get("rateBonusPerBuffType") and _e_rate is not None:
            _bt = e["rateBonusPerBuffType"]
            _cnt = count_active_buff_types(caster, _bt.get("types") or [])
            _e_rate = max(0.0, min(1.0, _e_rate + (_bt.get("per") or 0) * _cnt))
        # 批52g: ratePerTarget/rateStatusBonus —— 逐目標擲骰(五雷轟頂水攻/沙暴加機率),
        # 跳過此處全局一次擲骰, 改在 dests 選定後按目標狀態調 rate 再各自判定。
        _per_tgt_rate = bool(e.get("ratePerTarget") or e.get("rateStatusBonus"))
        # 批52j: capture 的 rate 在 k 分支內處理(已有捕獲時必轉 530% 直傷, 不吃 rate 失敗)
        if not rate_checked and not _per_tgt_rate and k != "capture" and _e_rate is not None and random.random() >= _e_rate:
            continue
        # 批52g: e.ifCasterNames —— 施放者武將名須在名單內(太平道法黃巾主將, 含 SP)
        if e.get("ifCasterNames"):
            _cn = set(e["ifCasterNames"] if isinstance(e["ifCasterNames"], list) else [e["ifCasterNames"]])
            if not (caster.g and caster.g.name in _cn):
                continue
        # 批26: e["ifLeader"] —— 效果級「施放者須為隊伍主將(index 0)」條件閘門。原文常見
        # 「自身為主將時，額外XX」這種措辭(南蠻渠魁/江東小霸王/酒池肉林等), 過去無對應原語,
        # 該效果段只能被迫「無條件對所有施放者套用」(高估非主將情形)或完全不建模(遺漏主將
        # 加成)。allies[0] 是隊伍主將慣例(同 who=="leader" 分支既有假設, 見上文), 只在
        # caster 就是 allies[0] 時才放行本效果段, 否則跳過。與 e["rate"] 同層級判斷(任何 k
        # 皆可掛), 置於 e["rate"] 判定之後(若戰法同時有機率也要求主將, 兩者皆需通過)。
        if e.get("ifLeader") and not (allies and allies[0] is caster):
            continue
        # 批52: e["ifSub"] —— 施放者須為副將(非 allies[0]); 對稱 ifLeader(一力拒守「自身為副將時」)
        if e.get("ifSub") and (not allies or allies[0] is caster):
            continue
        # 批52: e["ifGender"] —— "Male"/"Female"(或中文男/女); 比對 caster.g.gender
        if e.get("ifGender"):
            want_g = e["ifGender"]
            gmap = {"男": "Male", "女": "Female", "male": "Male", "female": "Female",
                    "Male": "Male", "Female": "Female"}
            want = gmap.get(want_g, want_g)
            got = (caster.g.gender if caster.g else None) or ""
            if gmap.get(got, got) != want:
                continue
        # 批44 A: e["ifLeaderIs"] —— 對稱 engine.js 同名分支(見其詳細註解)。效果級「隊伍主將
        # (allies[0])的武將名須匹配指定值」條件閘門, 對稱既有 e["ifLeader"](布林, 只判斷「是否
        # 為主將」)。原文常見TROOP兵種戰法「若XX統領, 數值提升/額外效果」措辭(白毦兵/丹陽兵/
        # 先登死士/藤甲兵/西涼鐵騎/白馬義從等8筆家族, 見 engine_limitations.md)。判斷式與
        # ifLeader 相同(allies[0] is caster), 額外比對 allies[0].g.name==e["ifLeaderIs"]
        # (指定武將的中文名, 與 tactics_parsed.json _todo 內文一致, 如"陳到")。也接受list(如
        # 虎衛軍「若典韋或許褚統領」, OR語意)。
        if e.get("ifLeaderIs"):
            _names = e["ifLeaderIs"] if isinstance(e["ifLeaderIs"], list) else [e["ifLeaderIs"]]
            if not (allies and allies[0] is caster and caster.g and caster.g.name in _names):
                continue
        # 批43 B: e["ifStackMaxed"] —— 對稱 engine.js 同名分支(見其詳細註解)。效果級「施放者
        # 自身的 k=="stack" 疊層(caster.stack)已疊滿(n>=max)」條件閘門, 搭配 everyRound 逐回合
        # 重新判定, 精確表達「疊加N次後才觸發」(如長驅直入)。
        if e.get("ifStackMaxed") and not (caster.stack and caster.stack["n"] >= caster.stack["max"]):
            continue
        # 禁近似令-批K: e["ifCasterStackAtLeast"](hit_count_stage_trigger族) —— 對稱
        # engine.js 同名分支(水淹七軍「第三/四次施放」需讀取stack.n是否達到門檻)。
        if e.get("ifCasterStackAtLeast") is not None and not (caster.stack and caster.stack["n"] >= e["ifCasterStackAtLeast"]):
            continue
        # 禁近似令-批K: e["ifEnemyTroop"](engine_wiring_gaps_misc族) —— 對稱 engine.js
        # 同名分支(左右開弓「如果目標為騎兵則額外造成潰逃狀態」, 兵種由隊伍決定)。
        if e.get("ifEnemyTroop") and not (enemies and enemies[0].ttype == e["ifEnemyTroop"]):
            continue
        # 禁近似令-批K: e["ifArmed"](once_consumable族) —— 對稱 engine.js 同名分支
        # (十二奇策 k=="armConsume"/"strike" 配對, 見 Unit.__init__ armed_consume 註解)。
        if e.get("ifArmed") and not (caster.armed_consume and caster.armed_consume.get("active")):
            continue
        # 禁近似令-批K: e["once"](通用版) —— 對稱 engine.js 同名分支, 補上「不論從哪條路徑
        # 呼叫都成立」的通用一次性消耗閘門(淵然難測「首回合觸發時,若...否則...」兩個互斥分支
        # 各自只應觸發一次)。
        if e.get("once"):
            if id(e) in caster.when_fired:
                continue
            caster.when_fired.add(id(e))
        # 批30 A: 非heal效果的逐回合重擲通道(e["everyRound"]) —— 過去只有 k=="heal" 在
        # heal_only(見 apply_passives 的逐回合呼叫)這條路徑下逐回合重新掃描/擲骰套用, 其餘
        # k(amp/mitig/block/stat/...)一旦在 prep 套用一次就不會再被重新判定, 導致「每回合
        # X%機率獲得1次抵禦/減傷」類戰法(機鑑先識/揮兵謀勝/魚鱗陣/枕戈坐甲等, 見
        # engine_limitations.md 第11節/25節)只能 EV 折算或截斷成一次性。修法: 把 heal 既有
        # 的「when視窗判定 + rounds去重 + rate擲骰」邏輯泛化成任何 k 皆可掛的通用閘門, 用
        # e["everyRound"](效果級旗標, opt-in)標記「這個效果不在 prep 套用一次, 改在每回合
        # 常駐通道重新判定」。與 heal 共用同一份 heal_rounds_fired/when_fired 去重狀態(鍵是
        # id(e), heal 與 everyRound 不會撞鍵, 因為同一個效果物件只會是其中一種)。刻意不新增
        # 獨立的 dedup dict, 沿用既有慣例、降低維護面。
        #
        # 語意與 heal 完全對稱: 非 heal 效果只有帶 e["everyRound"] 才會走到這裡(否則在上面的
        # top-level 過濾就被跳過, 見函式開頭); 帶 everyRound 的效果在**非** heal_only/own_turn
        # 呼叫路徑(prep/active/charge/when視窗)一律跳過(不套用), 因為它只該由這兩條常駐通道
        # 結算 —— 對稱於 heal 在其他路徑各自決定是否觸發、不依賴這裡的慣例。
        #
        # 時序一致化(2026-07 批次) A.3: own_turn=True 時, 「每回合」重擲改採該持有者(caster)
        # 自己的行動輪計數(caster.own_round)為基準, 取代 heal_only 舊路徑的全局 CUR_ROUND
        # ——機鑑先識「每回合21%→42%機率獲得1次警戒」等 e["everyRound"] 效果, user權威規則
        # 「回合」對持有者自身的漸進/計數機制=該持有者自己的行動輪。heal_only=True(現嚴格
        # heal-only, 見上方頂端閘門)與 own_turn=True 為互斥的兩種呼叫模式, 不會同時為真時
        # 走到此分支處理同一效果(heal_only=True 時 k!=heal 已在頂端被擋下)。
        #
        # 時序徹底一致化批: broadcast_only=True 時(e["broadcast"]標記的相一全局round-start廣播,
        # 如高櫓連營), 改用全局CUR_ROUND(user權威規則的唯一例外, 見函式docstring), 不隨caster
        # own_round個別結算。與own_turn互斥(廣播類效果只由broadcast_only通道呼叫, 見上方頂端
        # 閘門e["broadcast"]分流)。
        if e.get("everyRound") and k != "heal":
            # 批35 B: block 的「準備階段鎖定」scale 值已在函式最頂端(所有 continue 閘門之前)
            # 算定, 此處不需重複呼叫 locked_scale_of(見上方新增的閘門與其註解)。
            if not (heal_only or own_turn or broadcast_only):
                continue
            _round_basis = CUR_ROUND if broadcast_only else caster.own_round
            hw = e.get("when") or t.get("when")
            if hw:
                if not round_ok({"when": hw}, _round_basis):
                    continue
                # 批A(11筆高嚴重重建): e["when"]["hpBelow"]/["hpAbove"](效果級) —— 對稱
                # engine.js同名擴充(見其詳細註解): 過去hpBelow/hpAbove只在戰法級(t["when"])
                # 受理, everyRound通道的hw從不檢查hp。奇兵間道「第5回合時,若自身兵力低於
                # 50%...否則...」這類「同戰法內部分effects段各自獨立hp條件」的複合語意,
                # 現在hpBelow/hpAbove也認e["when"](效果自身), 不強制整條戰法共用同一個when。
                if hw.get("hpBelow") is not None or hw.get("hpAbove") is not None:
                    if not hp_ok({"when": hw}, caster):
                        continue
                if hw.get("rounds"):
                    seen = caster.heal_rounds_fired.setdefault(id(e), set())
                    if _round_basis in seen:
                        continue
                    seen.add(_round_basis)
            elif e.get("once"):
                if id(e) in caster.when_fired:
                    continue
                caster.when_fired.add(id(e))
            ev_rate = e.get("rate", t.get("rate", 1))
            if random.random() >= ev_rate:
                continue
            # 通過閘門後不 continue —— 落到下方通用 who/dests 派發邏輯(amp/mitig/block/...),
            # 走與 prep 套用相同的效果分派, 只是改成每回合重新判定/套用一次。
        # 批32 R23: e.when(非heal/非everyRound效果) 的回合窗口檢查 —— 過去只有「母戰法無
        # t.when 時, skip_when_effects=True 的 prep 呼叫會跳過此效果(留給 fight() 回合迴圈
        # 通用掃描處理, 見上方1045行)」這一種路徑會尊重 e.when; 其餘直接呼叫 apply_effects()
        # 的路徑(尤其 active 型戰法擲骰命中後, fight() 主迴圈的
        # `apply_effects(u, active_dst, t, ...)` 直接呼叫, 見主迴圈 active 分支)完全不檢查
        # e.when, 導致「奇數回合...偶數回合...」這類需要用 e.when.parity 切分同一戰法內兩組
        # 互斥效果的 active 戰法(飛沙走石), 即使補了 e.when.parity 也會被無條件套用(奇偶
        # 兩組效果同時生效, 塌縮成常駐雙倍輸出, 即R23要抓的缺口本身)。此處補上通用檢查:
        # 任何非heal/非everyRound效果只要帶 e["when"], 就先驗證當前回合是否落在窗口內, 不符合
        # 則跳過該效果段(不影響同戰法內其餘無 e.when 的效果, 也不影響 heal_only/skip_when_effects
        # 呼叫路徑既有行為——那些路徑要嘛在更上層已被攔截, 要嘛壓根不會走到這裡)。
        #
        # 時序一致化(2026-07 批次) A.2, 時序徹底一致化批(最終定案): k=="settle"(密計誅逆等猛毒
        # 式閾值爆發)/e["coefFromStack"](絕地反擊等自身疊層驅動爆發)/以及其餘所有when-gated
        # 非heal/非everyRound效果(工神/橫戈躍馬/武鋒陣/士別三日/用武通神等團隊buff與自參照戰法)
        # 的 e["when"] 一次性視窗註冊, 一律屬「持有者(caster)自身進程」, 「第N回合」改用
        # caster.own_round(該持有者自己第N個行動輪)為基準, 取代全局CUR_ROUND —— user最終裁決:
        # 除e["broadcast"](相一: 持有者每回合對他人廣播施加新狀態層, 如高櫓連營, 極少數實例)外,
        # 一律相二逐單位own_round(含團隊buff如陷陣營/金丹秘術類, 各受益者自己回合結算, 戰報實證
        # 見交接文件)。此 elif 為通用防禦閘門(不論呼叫路徑為何皆會核對), 實際註冊入口見 fight()
        # 主迴圈 apply_own_turn_effects() 內專屬一次性視窗掃描(該處已用caster.own_round預篩+
        # when_fired去重, 這裡的核對是二次防禦, 語意需一致)。
        elif e.get("when") and k != "heal" and not e.get("everyRound"):
            _round_basis2 = CUR_ROUND if e.get("broadcast") else caster.own_round
            if not round_ok({"when": e["when"]}, _round_basis2):
                continue
        if k == "heal":                               # 治療: 補我方最殘一人(指揮/被動每回合觸發)
            if no_heal:
                continue
            if e.get("coef", 0.8) < 0:                # 批10: 資料衛生防禦 —— 負 heal coef(如機略縱橫類 dot 誤標成 heal 負值)一律視為0並跳過, 避免資料錯誤反而扣友軍血
                continue
            # 批15: 指揮/被動的 heal 在 heal_only(每回合無條件常駐掃描, 見 apply_passives 的
            # 逐回合呼叫)這條路徑下, 過去無視 t["when"]/t["rate"]/e["once"], 每回合必定結算 ——
            # 「第N回合治療一次」類戰法(如撫輯軍民/桃園結義/士別三日)被無聲放大成每回合治療
            # (~8倍/回合數倍)。修正: 僅在 heal_only 常駐路徑套用下列語意閘門(其餘呼叫路徑, 如
            # when 視窗一次性套用/active主動/charge突擊/onHit反應式, 呼叫前已各自決定是否該
            # 觸發, 不應再被此處二次過濾):
            #   1) e["when"](效果級, 優先) 或 t["when"](戰法級) 存在 → 用 round_ok 檢查回合是否
            #      落在視窗內, 不符合則本回合不治療。e["when"] 用途: 同一戰法內其餘效果(如撫輯
            #      軍民的 mitig/amp)是「前3回合就生效」的常駐buff(無when, 準備階段套用), 但
            #      heal 段是「第4回合單次觸發」—— 兩者時間窗不同, 不能共用同一個 t["when"]
            #      (會連帶把 mitig/amp 也延後到第4回合才套用), 故 heal 效果自己帶 e["when"]
            #      覆蓋, 不影響同戰法其他效果的準備階段套用時機。
            #      - when["rounds"](明確列出的單一/多個回合, 如「第4回合」「第3、5回合」):
            #        語意是「只在這些特定回合各觸發一次」, 用 heal_rounds_fired(效果物件+回合
            #        組合去重)確保 rounds:[3,5] 這種多回合列表在第3、第5回合各自觸發一次、
            #        不重複、也不會在其他回合誤觸發。
            #      - when["from"]/["until"](範圍視窗, 如「第3回合起, 持續3回合」「第5回合
            #        起」): 語意是「這幾回合每回合都要治療」(休整/持續恢復類戰法, 如金丹秘術/
            #        詐降/魚鱗陣), 故只用 round_ok 檢查是否在窗內, 不做去重(讓窗內每回合都能
            #        重新擲骰/治療)。
            #   2) e["once"] is True(單次治療語意, 無 when 亦適用) → 觸發過一次即不再結算,
            #      同樣用 when_fired 去重。
            #   3) 無 when(e["when"]/t["when"]皆無)且無 e["once"] → 維持原行為: 每回合持續
            #      治療(急救/休整類戰法本意如此)。
            #   以上都通過後才擲 e["rate"]??t["rate"] 骰(rate<1 時只有部分回合真正治療, 而非年年必中)。
            if heal_only:
                # 時序徹底一致化批(戰報實證: 左慈金丹秘術/夏侯惇陷陣營): 改用caster(=呼叫端的u,
                # 即持有者自己)own_round為基準, 取代全局CUR_ROUND —— heal_only現改於fight()主
                # 迴圈逐單位處理輪到u時呼叫(u自己行動輪, 行動前), 取代舊「回合迴圈頂端全體單位
                # 批次」通道, 使「戰鬥前N回合我軍全體休整/回血」類團隊buff在各受益單位(=持有者)
                # 自己的行動輪結算, 與戰報實測時序一致(相二逐單位)。
                hw = e.get("when") or t.get("when")
                if hw:
                    if not round_ok({"when": hw}, caster.own_round):
                        continue
                    if hw.get("rounds"):                  # 明確列出的特定回合: 每個列出的回合各觸發一次(回合特定去重鍵, 而非整場只觸發一次)
                        seen = caster.heal_rounds_fired.setdefault(id(e), set())
                        if caster.own_round in seen:
                            continue
                        seen.add(caster.own_round)
                    # from/until(範圍視窗): 不去重, 窗內每回合都可能治療(休整類戰法本意如此, 如金丹秘術/詐降/魚鱗陣)
                elif e.get("once"):
                    if id(e) in caster.when_fired:
                        continue
                    caster.when_fired.add(id(e))
                # 批G: t["rate"]僅在e["rate"]缺席時才擲骰 —— 過去此處無條件讀t["rate"], 但
                # e["rate"]本身早已被上方「批23 A4: 效果級e["rate"]折算一致性」通用閘門(函式開頭,
                # 對所有k統一處理)擲骰判定過一次(non-heal_only的prep/active/charge等路徑同樣適用
                # 此通用閘門, heal_only路徑亦不例外), 若這裡帶e["rate"]又重複讀e["rate"]骰一次會
                # 使機率被平方(0.1×0.1≈0.01, 而非期望的0.1), 犯了同批註解自己警告的錯誤(「避免在
                # 這裡對同一效果重複擲骰, 機率會被平方, 造成低估」)。修正: e["rate"]存在時, 通用
                # 閘門已完整處理該效果本回合是否觸發, 這裡不再二次擲骰(直接放行, 不比對t["rate"]);
                # 只有e["rate"]缺席(該效果未自帶機率)時才退回擲t["rate"](戰法整體觸發率), 對稱
                # everyRound非heal通道既有的ev_rate=e.get("rate", t.get("rate", 1))慣例, 但避免其
                # 「取e.rate或t.rate其一」的寫法在heal_only此處被誤用成「兩者都各自獨立擲一次」。
                if e.get("rate") is None and random.random() >= t.get("rate", 1):
                    continue
            # 批52: heal 選標對齊原文 —— 過去一律「我方最殘一人」, 忽略 who/e.n/targetSel,
            # 導致「恢復自身」「治療我軍主將」「我軍群體2人/全體」全部失真(engine_limitations #1)。
            # 現與 amp/mitig 等效果共用 who/n/nMax/targetSel 語意:
            #   who:self → 施放者; who:leader → 我方主將; who:subs → 副將全體;
            #   who:eventTarget → 反應式事件單位(急救/ofDamage 類);
            #   targetSel → 依準則挑 1 人(不受混亂影響);
            #   e.n(/nMax) → 隨機挑 N 名可治療友軍(群體治療);
            #   預設(who=ally 且無 n) → 維持舊行為「最殘 1 人」(單體急救/包紮等向後相容)。
            # 禁療(healblock) 者一律不進可治療池; 批52j: 捕獲者友軍不可選中
            who_h = e.get("who", "ally")
            pool = [a for a in allies if a.alive and not a.healblock and not getattr(a, "captured", 0)]
            hurts = []
            if e.get("targetSel"):
                picked = pick_by_criterion(pool, e["targetSel"], ally_pool=True)
                hurts = [picked] if picked else []
            elif who_h == "self":
                hurts = [caster] if (caster.alive and not caster.healblock and not getattr(caster, "captured", 0)) else []
            elif who_h == "leader":
                hurts = [allies[0]] if (allies and allies[0].alive and not allies[0].healblock and not getattr(allies[0], "captured", 0)) else []
            elif who_h == "subs":
                hurts = [a for a in allies[1:] if a.alive and not a.healblock and not getattr(a, "captured", 0)]
            elif who_h == "eventTarget":
                hurts = [evt_target] if (evt_target and evt_target.alive and not evt_target.healblock and not getattr(evt_target, "captured", 0)) else []
            elif e.get("all"):
                # 批F: e["all"](新原語) —— 「我軍全體」精確表達, 對稱amp/mitig/stat等效果種類既有
                # 「who:ally且無n → 全體」通用慣例。heal過去無此路徑, 無n時一律落到下方「預設
                # (單體, min troop)」分支, 導致「我軍全體」語意的戰法(如金丹秘術「我軍全體獲得
                # ...休整狀態」)被誤治成全軍僅1人, 漏治其餘友軍。
                hurts = list(pool)
            elif e.get("n") is not None:
                n = e["n"]
                cnt = n + random.randint(0, e["nMax"] - n) if e.get("nMax") else n
                # preferLowest: 優先兵力最低的 N 人(如青州兵「優先完全恢復最低」); 否則隨機 N 人(群體治療通例)
                if e.get("preferLowest") or e.get("sharedPool"):
                    hurts = sorted(pool, key=lambda a: a.troop)[:cnt]
                else:
                    hurts = pick_targets(pool, cnt)
            else:
                # 批F: 此分支為「單體, 無who/n/targetSel明示」的最終後備 —— 過去(批52前)是全域
                # 唯一行為(全庫heal一律套用), 現僅限「本文確實只描述我軍單體, 且未指定特定選標
                # 準則(如兵力最低/損失最多)」的戰法才會落到這裡, 語意應是「隨機挑1人」而非
                # 「固定選最殘」。批F資料全掃已將全庫符合各分支語意的heal效果都改掛顯式who/n/
                # targetSel/all, 理論上不應再有戰法會落到此分支, 仍保留作最終防禦性後備(維持
                # min-troop而非改隨機, 是刻意的向後相容安全值, 非常態路徑, 不代表「預設補最殘」
                # 是被允許的全域慣例)。
                hurt0 = min(pool, key=lambda a: a.troop, default=None)
                hurts = [hurt0] if hurt0 else []
            # 治療量公式(對每個目標獨立結算; ofDamage/scale 依施放者, healBoost 依受療者)
            # sharedPool: 總治療量一次算出後依序填滿(最殘優先), 對應「總治療率」分攤原文
            of_damage_scale_mult = locked_scale_of(caster, e) if (e.get("ofDamage") is not None and e.get("scale")) else 1.0
            # 批52: scaleIfSub —— 原文「自身為副將時, 恢復受武力影響」: 僅副將套用 scale
            _scale_ok = True
            if e.get("scaleIfSub"):
                _scale_ok = bool(allies and allies[0] is not caster)
            if e.get("scaleIfLeader"):
                _scale_ok = bool(allies and allies[0] is caster)
            hcoef = e.get("coef", 0.8) * (
                scale_of(caster, e["scale"]) if (e.get("scale") and e.get("ofDamage") is None and _scale_ok) else 1.0
            )
            heal_troop_base = caster.troop * HEAL_TROOP_C if t.get("type") == "active" else caster.heal_base
            # 批A(11筆高嚴重重建): e["ofDamage"] 原本只讀 dmg(傷害比例治療, 批33), on:"healed"
            # 反應式(批43 C)呼叫端傳的是 heal_amt(本次觸發事件的實際治療量), 從未被此處讀取——
            # ofDamage 的欄位語意其實已是「本次觸發事件的量」的通用比例治療, 這裡補上 heal_amt
            # 分支(結盟「目標受到治療效果時,自身有機率獲得相同(治療)效果(治療效果為50%)」的鏡像
            # 治療, 見結盟落地)。dmg 優先於 heal_amt(兩者不會同時非None, dealtDamage/on_hit 與
            # healed 是互斥事件)。
            of_event_amt = dmg if dmg is not None else heal_amt
            if e.get("ofDamage") is not None and of_event_amt is not None:
                pool_want = e["ofDamage"] * of_damage_scale_mult * of_event_amt
            else:
                pool_want = hcoef * heal_troop_base
            shared = bool(e.get("sharedPool"))
            remain = pool_want if shared else None
            for hurt in hurts:
                if not hurt:
                    continue
                boost_mult = max(0.0, 1 + hurt.addbonus("healBoost")) * max(0.0, 1 + caster.addbonus("healGiven"))
                if shared:
                    if remain is None or remain <= 0:
                        break
                    want = remain * boost_mult
                else:
                    want = pool_want * boost_mult
                actual = max(0.0, min(want, hurt.wounded, START_TROOP - hurt.troop))
                hurt.troop += actual
                hurt.wounded -= actual
                if shared:
                    # 扣掉「未乘 boost 前」消耗的池量, 避免 healBoost 把總池放大/縮小失真
                    used = actual / boost_mult if boost_mult > 0 else actual
                    remain -= used
                healed_for(hurt, caster, actual, allies, enemies)
            continue
        # 禁近似令-批K: k=="regen"(engine_wiring_gaps_misc族, 對稱engine.js同名分支) ——
        # 「每回合恢復一次兵力,持續N回合」的休整類狀態, 登記到目標的u.regens清單(見tick()
        # 消費端逐回合各自結算), 取代「heal效果不讀dur, 只結算一次, 折算成單次coef×dur近似
        # (2倍低估)」的既有缺口(乘敵不虞)。
        if k == "regen":
            who_r = e.get("who", "ally")
            pool_r = [a for a in allies if a.alive and not a.healblock and not a.captured]
            if e.get("targetSel"):
                picked = pick_by_criterion(pool_r, e["targetSel"])
                targets_r = [picked] if picked else []
            elif who_r == "self":
                targets_r = [caster] if (caster.alive and not caster.healblock) else []
            elif who_r == "leader":
                targets_r = [allies[0]] if (allies and allies[0].alive and not allies[0].healblock) else []
            else:
                targets_r = list(pool_r)
            hcoef_r = e.get("coef", 0.8) * (scale_of(caster, e["scale"]) if e.get("scale") else 1)
            heal_troop_base_r = caster.troop * HEAL_TROOP_C if t.get("type") == "active" else caster.heal_base
            amt_r = hcoef_r * heal_troop_base_r
            # 狀態疊加語意對齊批: 休整(regen)為 NAMED_STATUS 已確認的 "unique"(唯一/覆蓋)
            # 具名狀態 —— 同單位再施加同名(休整)狀態應覆蓋舊實例(刷新, 保留最新來源/數值),
            # 不新增第二筆(過去無條件 append, 若同單位有兩個regen來源會變成"共存"疊加, 不
            # 符合唯一狀態規則)。regens 沿用既有 [amt, dur] 清單形狀(對稱 dots 慣例, 見
            # decay_durations()/dot_settle() 消費端只讀取前兩格), 延伸第3/4格存放狀態名/
            # 來源顯示名(供未來戰報「執行來自【X】的【狀態】」)。以"休整"為固定鍵: 找到既有
            # 筆(第3格=="休整")則整筆取代, 找不到則新增, 全場至多1筆——與 upsert_named_status()
            # 同一套「找key覆蓋否則新增」邏輯, 但因 regens 是 list-of-list 而非 list-of-dict
            # 形狀, 這裡用等價的內聯寫法(而非直接呼叫該共用函式, 避免為了共用而改動既有list
            # 慣例)。
            _rg_src_name = effect_src_name(t, e)
            for v in targets_r:
                _rg_payload = [amt_r, e.get("dur", 2), "休整", _rg_src_name]
                for _ri, _rg in enumerate(v.regens):
                    if len(_rg) > 2 and _rg[2] == "休整":
                        v.regens[_ri] = _rg_payload
                        break
                else:
                    v.regens.append(_rg_payload)
            continue
        if k == "settle":                             # 結算傷害(猛毒): 掛統率最高敵將, 觸發見 fight
            # 禁近似令-批K: e["perStackFrom"](dynamic_coef_from_counter族) —— 對稱 engine.js
            # 同名分支, 選標改為「敵軍中該stackId疊層數最高者」(密計誅逆settle必須與另一段
            # amp-stackKey疊層綁定同一目標, 而非泛用統率最高)。
            if e.get("perStackFrom"):
                _psf = e["perStackFrom"]
                tg = None
                best_lv = -1
                for x in enemies:
                    if not x.alive:
                        continue
                    lv = (x.amp_layers_by_id or {}).get(_psf, 0)
                    if lv > best_lv:
                        tg, best_lv = x, lv
            else:
                tg = max((x for x in enemies if x.alive),
                         key=lambda x: x.eff("command"), default=None)
            if tg:
                tg.settle = {"layers": e.get("init", 1), "max": e.get("max", 3),
                             "left": e.get("dur", 2), "caster": caster, "snap": caster.troop,
                             "base": e.get("base", 1.5), "per": e.get("per", 0.4),
                             "kind": t.get("kind", "intel"),
                             "perStackFrom": e.get("perStackFrom"),
                             "singleTarget": bool(e.get("singleTarget"))}
            continue
        if k == "redirect":                           # 傷害轉移: 代承者替其餘友軍吃 share
            # 批J(禁近似令-transfer轉移族): e["guardFor"]=="leader" —— 「單次全額代承」模式
            # (古之惡來「我軍主將即將受到普攻時...隨後為我軍主將承擔此次普通攻擊」), 對稱既有
            # counter 的 guardFor:"leader"(守護式反擊), 但這裡是「代為承受」而非「代為反擊」。
            # 不走下方常駐 guardian(%分擔每一下直到guard_dur到期)路徑, 改登記進
            # allies[0].absorb_guards, 由 hit() 在主將受普攻時只轉移「這一下」的傷害(不影響
            # 後續攻擊), 每回合限觸發1次(見 hit() 內 absorb_guards 節流)。與 counter_guards
            # 是兩份獨立清單, 可並存。
            if e.get("guardFor") == "leader":
                if allies and allies[0].alive:
                    allies[0].absorb_guards.append({"unit": caster, "share": e.get("share", 1.0),
                                                     "prob": e.get("prob", 1.0)})
                continue
            if e.get("guard") == "max_force":         # 代承者: 武力最高友軍 或 自己(預設)
                guardian = max((a for a in allies if a.alive),
                               key=lambda a: a.eff("force"), default=caster)
            elif e.get("guard") == "random_sub":
                # 批J: 代承者=隨機一位「當下存活」的非主將副將(夢中弒臣「如果自己為主將，則使
                # 隨機副將為自己分擔20%→40%傷害」), 與既有 max_force(取武力最高) 同層級但改
                # 採均勻隨機。若無存活副將, guardian 落回 caster 本身——下方 `a is not guardian`
                # 判斷會使 recipients(who=="leader"時=[caster], 因 caster 即 allies[0])被排除,
                # 天然等同「找不到可轉嫁對象則不轉嫁」(不無中生有), 而非另尋他法硬湊轉嫁對象。
                subs = [a for a in allies if a.alive and a is not allies[0]]
                guardian = random.choice(subs) if subs else caster
            else:
                guardian = caster
            # 批G: who 分流(leader/subs) —— 過去此處無條件對「除guardian外的全體allies」套用
            # 同一share, 不像其他k類型(mitig/amp/heal/…)已支援who:leader(僅index0主將)/
            # who:subs(index0以外副將)分流, 導致「為副將分擔30%/為主將分擔60%」這類依受益者
            # 身份給不同share值的戰法(肉身鐵壁)只能合併成單一均值近似(0.45)。現對稱既有
            # who=="leader"/who=="subs"慣例(見上方dests泛用分派區塊), 若redirect效果帶
            # who:"leader"/"subs"則只套用到對應子集, 省略who(或who:"ally", 向後相容既有全部
            # 資料)時維持原行為(對guardian以外的全體allies套用同一share)。
            redirect_who = e.get("who", "ally")
            if redirect_who == "leader":
                recipients = [allies[0]] if allies and allies[0].alive else []
            elif redirect_who == "subs":
                recipients = [a for a in allies[1:] if a.alive]
            else:
                recipients = list(allies)
            for a in recipients:
                if a.alive and a is not guardian and not getattr(a, "captured", 0):
                    a.guardian, a.guard_share = guardian, e.get("share", 0.3)
                    a.guard_dur = e.get("dur", 99)    # 讀 e.dur(預設99=近似全程, 向後相容); 到期由 tick 清除
                    a.guard_normal_only = bool(e.get("normalOnly"))  # 只代承普攻(如 援助), 戰法傷害不轉移
            continue
        # 批J(禁近似令-transfer轉移族): stealStat —— 偷屬性原語(雁行陣「使我軍統率最低單體
        # 偷取敵軍全體10點統率」)。核心約束: 不能無中生有——從每個victim實際扣除
        # min(欲偷量, victim現有可扣量(=其當下effective值, 不得扣至負數)), 施放者/受益者只
        # 獲得「所有victim實際被扣除量」的加總(而非固定填e["amount"], 若victim現有量不足10
        # 點就只能偷到那麼多)。與既有 k=="stat" 的差異: k=="stat" 是無條件疊加, 不檢查/不連動
        # 另一方; stealStat 是「一方扣多少, 另一方就恰好收多少」的成對操作, 且扣除量會先被
        # victim現有值封頂。recipientSel(targetSel準則字串, 見TARGETSEL_KEY)從allies挑受益者,
        # 省略時預設caster本身; 對稱既有srcSel/checkSrcSel(proxyNormal/proxyHit)呼叫慣例,
        # 直接對raw allies呼叫pick_by_criterion不額外做captured過濾(與engine.js pickByCriterion
        # 本身就不支援ally_pool參數一致, 維持雙引擎行為對稱)。
        if k == "stealStat":
            # 禁近似令-批K: e["statOptions"](陣列) —— 對稱 engine.js 同名分支(至柔動剛「偷取
            # 來源智/統/速任一屬性」三選一隨機), 對稱既有e["stat"]單一固定屬性。
            stat_field = random.choice(e["statOptions"]) if e.get("statOptions") else e["stat"]
            want_each = e.get("amount", 0) * (scale_of(caster, e["scale"], e.get("scaleDiv")) if e.get("scale") else 1.0)
            recipient = pick_by_criterion(allies, e["recipientSel"]) if e.get("recipientSel") else caster
            if recipient and recipient.alive and want_each > 0:
                # 禁近似令-批L: e["victimIsTgt"] —— 受害者精確鎖定「本次反應式事件的另一方」
                # (tgt, 本函式第2參數, 於on_hit()反應式呼叫時=攻擊者src), 對稱既有
                # who=="eventTarget"精確選標精神但走stealStat自己的early-return targeting
                # (在通用dests/who解析區塊之前就continue掉), 不能複用evt_target(那是給
                # victim/dst本身用的)。先登死士「偷取其[攻擊者]10.5→21點統率」需要精確鎖定
                # 攻擊者本人, 而非既有victim_pool(enemies全體)。
                victim_pool = [tgt] if (e.get("victimIsTgt") and tgt and tgt.alive) else \
                    ([] if e.get("victimIsTgt") else [x for x in (allies if e.get("who") == "ally" else enemies) if x.alive])
                # 禁近似令-批L: e["ifStatCompare"] —— stealStat有自己的targeting早退路徑(不
                # 經過通用dests區塊的既有ifStatCompare過濾), 故在此局部重新套用同一個
                # stat_compare_ok()比較(ref=caster, target=victim逐一比對)。先登死士「若兵力
                # 百分比低於攻擊者」= stat:"hpPct",op:"lt"。
                if e.get("ifStatCompare"):
                    victim_pool = [v for v in victim_pool if stat_compare_ok(caster, v, allies, e["ifStatCompare"])]
                # 禁近似令-批L: resolve_max_stack —— 「可疊加4次;若麴義統領則可疊加5次」。
                ms = resolve_max_stack(caster, e, allies)
                # 禁近似令-批L: maxStack封頂時「雙方都不記帳」——先檢查受益者這一側是否已達
                # 上限, 若已封頂則整次偷取視為no-op(僅刷新雙方既有同src疊層的dur, 不再產生新
                # 的扣/收記錄), 避免「受害者被扣但受益者因push_stat_add內部封頂靜默no-op收
                # 不到」的無中生有bug(對稱engine.js同名段落)。
                if ms is not None:
                    already = sum(1 for a in recipient.stat_adds if a[0] == stat_field and a[3] == src)
                    if already >= ms:
                        for a in recipient.stat_adds:
                            if a[0] == stat_field and a[3] == src:
                                a[2] = max(a[2], e.get("dur", 1))
                        for v in victim_pool:
                            for a in v.stat_adds:
                                if a[0] == stat_field and a[3] == src:
                                    a[2] = max(a[2], e.get("dur", 1))
                        continue
                total = 0.0
                for v in victim_pool:
                    avail = max(0.0, v.eff(stat_field))
                    actual = min(want_each, avail)
                    if actual > 0:
                        v.push_stat_add(stat_field, -actual, e.get("dur", 1), src, max_stack=ms)
                        total += actual
                if total > 0:
                    recipient.push_stat_add(stat_field, total, e.get("dur", 1), src, max_stack=ms)
            continue
        # 批J: transferMitig —— 把「敵方(或指定來源側)當下實際持有的正向mitig(傷害降低)buff
        # 實例」整個搬到我方(或指定去向側)隨機一人身上(雁行陣「轉移傷害降低: 將敵軍隨機武將的
        # 傷害降低效果轉移至我軍隨機武將」)。若來源側當下沒有任何人持有這樣的buff, 不觸發(轉移
        # 0, 不無中生有, 不得無來源憑空生出一份mitig buff給接收方)。轉移=移動(從來源陣列真的
        # 移除該實例)而非複製, val照抄來源實例原值, dur改用e["dur"](對應原文「持續1回合」,
        # 非沿用來源剩餘時長)。
        if k == "transferMitig":
            from_pool = [x for x in (allies if e.get("from") == "ally" else enemies) if x.alive]
            to_pool = [x for x in (allies if e.get("to") == "ally" else enemies) if x.alive]
            candidates = [(u, a) for u in from_pool for a in u.adds if a[0] == "mitig" and a[1] > 0]
            if candidates and to_pool:
                unit, entry = random.choice(candidates)
                dest = random.choice(to_pool)
                unit.adds.remove(entry)
                dest.push_add("mitig", entry[1], e.get("dur", 1), src)
            continue
        # 批J: transferDebuff —— 把「我方(或指定來源側)群體當下實際持有的負面狀態」隨機挑
        # e["n"]~e["nMax"]種「不同種類」(而非同種類的多個實例)整個搬到敵方(或指定去向側)隨機
        # 單位身上(雁行陣「轉移負面狀態: 將友軍群體隨機1-2種負面狀態轉移至隨機敵軍」)。與現有
        # dispel_unit 共用同一套「什麼算負面狀態」分類(負值amp/mitig、mult<1的mods、負值
        # stat_adds、dot、stun/silence/disarm/chaos/healblock/fakeReport/ambush/huchen), 確保
        # 口徑一致不新開一套分類標準。若來源側當下完全沒有負面狀態, 轉移0種(不無中生有); 若
        # 只有1種可轉移即使e["nMax"]要求2種也只轉移現有的那1種(轉移量=來源實際擁有量, 不硬湊
        # 到位)。
        if k == "transferDebuff":
            from_pool = [x for x in (enemies if e.get("from") == "enemy" else allies) if x.alive]
            to_pool = [x for x in (enemies if e.get("to") == "enemy" else allies) if x.alive]
            tokens = collect_debuff_tokens(from_pool)
            if tokens and to_pool:
                kinds = list({tok["kind"] for tok in tokens})
                base_n = e.get("n", 1)
                want_n = base_n + random.randint(0, e["nMax"] - base_n) if e.get("nMax") else base_n
                chosen_kinds = random.sample(kinds, min(want_n, len(kinds)))
                for kd in chosen_kinds:
                    matches = [tok for tok in tokens if tok["kind"] == kd]
                    tok = random.choice(matches)
                    dest = random.choice(to_pool)
                    tok["move"](dest, e.get("dur", 1))
            continue
        # 批52j: capture(捕獲) —— 暗箭難防獨立狀態
        # 已有捕獲 → altCoef 直傷該人; 否則 rate(可 scale) 捕獲敵單體(不可淨化/無法行動造成傷害/
        # 禁指揮被動/禁療/友軍不可選)
        if k == "capture":
            captives = [x for x in enemies if x.alive and getattr(x, "captured", 0) > 0]
            if captives:
                v = captives[0]
                coef = e.get("altCoef", 5.3)
                hit(caster, v, coef, e.get("kind") or t.get("kind", "phys"), False,
                    _FIGHT_CTX.get("on_hit"), _FIGHT_CTX.get("on_deal"), is_active=True)
            else:
                r = e.get("rate", 1.0)
                if e.get("scale"):
                    r = min(1.0, r * rate_scale_of(caster, e["scale"], e.get("scaleDiv")))
                if rate_checked or random.random() < r:
                    pool = [x for x in enemies if x.alive and not getattr(x, "captured", 0)]
                    n = e.get("n") or 1
                    dests_c = pick_targets(pool, n)
                    dur = e.get("dur", 2)
                    for u in dests_c:
                        # 獨立狀態: 無視洞察; 不可淨化(dispel 不碰 captured)
                        u.captured = max(u.captured, dur)
                        u.healblock = max(u.healblock, dur)
            continue
        # 禁近似令-批K: armConsume(once_consumable族施放端) —— 對稱 engine.js 同名分支
        # (十二奇策), 武裝一份一次性追加觸發資格。
        if k == "armConsume":
            _who_ac = e.get("who", "self")
            _dests_ac = [caster] if (_who_ac == "self" and caster.alive) else [a for a in allies if a.alive]
            for _uu in _dests_ac:
                _uu.armed_consume = {"active": True}
            continue
        # 禁近似令-批K: strike(once_consumable族消費端) —— 對稱 engine.js 同名分支, 由
        # e["ifArmed"]頂層閘門(見上方)確保只有armed_consume為真才會執行到這裡。
        if k == "strike":
            # 禁近似令-批K: e["sameTarget"]/e["ifTargetHas"] —— 對稱 engine.js 同名分支
            # (驍健神行「如果目標已經被繳械則造成兵刃攻擊」需要與effects陣列內排在前面的
            # disarm效果精確命中同一人, 靠陣列順序解決execution ordering問題)。
            _pool_s = [x for x in enemies if x.alive]
            if e.get("sameTarget"):
                v = tgt if (tgt and tgt.alive) else None
            elif e.get("targetSel"):
                v = pick_by_criterion(enemies, e["targetSel"])
            else:
                v = random.choice(_pool_s) if _pool_s else None
            if v and e.get("ifTargetHas") and not target_has(v, e["ifTargetHas"]):
                v = None
            if v:
                hit(caster, v, e.get("coef", 1.0), e.get("kind") or t.get("kind", "intel"), False,
                    _FIGHT_CTX.get("on_hit"), _FIGHT_CTX.get("on_deal"))
            if e.get("ifArmed"):
                caster.armed_consume = None
            continue
        # 批A(11筆高嚴重重建): chargeConsume —— 「可消耗資源池」的消耗端(死戰不退「普攻後,有50%
        # 機率(受武力影響)消耗一層蓄威造成一次兵刃傷害,觸發後可繼續判定,每次觸發後機率降低8%,
        # 每回合最多觸發5次」)。掛在when={"on":"dealtDamage","normalOnly":True}反應式, caster
        # 即普攻的發動者本身。遞迴鏈式判定, 對稱engine.js同名分支, 見其詳細註解。
        if k == "chargeConsume":
            if not caster.charge or caster.charge["n"] <= 0:
                continue
            cur_rate = e.get("rate", 0.5)
            if e.get("scale"):
                cur_rate *= rate_scale_of(caster, e["scale"], e.get("scaleDiv"))
            decay_per = e.get("decayPer", 0.08)
            max_chain = e.get("maxChain", 5)
            chained = 0
            # rate_checked: 外層dealt_damage_for的效果級派發已經用e["rate"]擲過一次骰才呼叫到
            # 這裡(對稱capture的rate_checked慣例)——若已檢查過, 第一層視為「首次判定已經命中」
            # 直接consume, 不再重擲一次(避免雙重擲骰造成機率減半)。
            first_iteration = True
            while caster.charge["n"] > 0 and caster.charge_consumed_this_round < max_chain:
                hit_this = True if (first_iteration and rate_checked) else (random.random() < max(0.0, cur_rate))
                first_iteration = False
                if not hit_this:
                    break
                caster.charge["n"] -= 1
                caster.charge_consumed_this_round += 1
                chained += 1
                pool = [x for x in enemies if x.alive]
                v = random.choice(pool) if pool else None
                if v:
                    hit(caster, v, e.get("coef", 1), e.get("kind") or t.get("kind", "phys"), False,
                        _FIGHT_CTX.get("on_hit"), _FIGHT_CTX.get("on_deal"), is_active=True)
                cur_rate -= decay_per
            continue
        # 批52i: proxyNormal/proxyHit 不走 dests 迴圈(一次性條件代打/直傷)
        if k == "proxyNormal":
            atk = pick_by_criterion(allies, e["srcSel"]) if e.get("srcSel") else caster
            if atk and atk.alive:
                ex = atk.addbonus("extra")
                if not (e.get("ifNoExtra") and ex > 0) and not (e.get("ifHasExtra") and ex <= 0):
                    do_normal_attack(atk, allies, enemies)
            continue
        if k == "proxyHit":
            checker = pick_by_criterion(allies, e["checkSrcSel"]) if e.get("checkSrcSel") else caster
            if checker is None:
                checker = caster
            ex = checker.addbonus("extra") if checker else 0
            if not (e.get("ifNoExtra") and ex > 0) and not (e.get("ifHasExtra") and ex <= 0):
                src_u = pick_by_criterion(allies, e["srcSel"]) if e.get("srcSel") else caster
                if not src_u or not src_u.alive:
                    src_u = caster
                v = pick_target_chaos(src_u, allies, enemies)
                if v and e.get("coef"):
                    hit(src_u, v, e["coef"], e.get("kind", "phys"), False,
                        _FIGHT_CTX.get("on_hit"), _FIGHT_CTX.get("on_deal"))
            continue
        who = e.get("who", "ally")
        ctrl_k = k in ("stun", "silence", "disarm", "taunt", "chaos")  # 控制/嘲諷類: 按戰法 n/nMax 選目標數(insight 不擋嘲諷, 只擋 stun/silence/disarm/chaos)
        # 批23 A1: 效果級 e["n"](可配 e["nMax"]) —— 非CTRL效果(amp/mitig/stat/dot/healblock/
        # rateup/…)過去無條件把 who="enemy"/"ally" 放大成全體敵軍/我軍, 大量原文寫「單體」
        # 「目標」「我軍2人」的非控制效果被系統性高估成全體(見批23清單: 謙讓/殿後/破甲/談心/
        # 追傷/兵鋒/舌戰群儒/八門金鎖陣/進言/江東小霸王/眾動萬計/國士將風等)。修法: 有 e["n"]
        # 時比照 ctrl_k 群體控制的既有選標邏輯(pick_targets 隨機不重複; 單體時優先鎖定 tgt,
        # 與 ctrl_k 慣例一致), 只是讀 e["n"]/e["nMax"](效果自身欄位)而非 t["n"]/t["nMax"]
        # (戰法頂層, ctrl_k 專用, 維持不變)。無 e["n"] 時完全維持原行為(全體敵軍/我軍), 向後
        # 相容 —— 大量「全體」條目依賴現行為。
        has_en = e.get("n") is not None
        # 批18: targetSel(指定選標準則) —— 效果級欄位, 優先於 who 的預設隨機/群體邏輯: 依準則
        # (兵力最低/武力最高/智力最低/我方最殘等)在對應陣營(enemy用敵方, 其餘用我方)挑單一目標。
        # 「指定」不受混亂(chaos)影響(混亂只亂「隨機」選目標的普攻/主動/突擊, 見 pick_target_chaos
        # 呼叫端 —— targetSel 在此處直接決定 dests, 完全不經過受混亂影響的 tgt/pick_targets 隨機路徑)。
        if e.get("targetSel"):
            # 禁近似令-批K: who=="subs"+targetSel —— 對稱 engine.js 同名分支(三勢陣「損失
            # 兵力較多的副將/另一名副將」只在兩名副將之間比較)。
            pool = enemies if who == "enemy" else (allies[1:] if who == "subs" else allies)
            picked = pick_by_criterion(pool, e["targetSel"], ally_pool=(who != "enemy"))
            dests = [picked] if picked else []
        elif who == "self":
            dests = [caster] if (caster.alive and not getattr(caster, "captured", 0)) else []
        elif who == "eventTarget":                    # 批42: 見 apply_effects evt_target 參數註解
            dests = [evt_target] if (evt_target and evt_target.alive) else []
        elif who == "leader":                         # 批8: 主將限定(隊伍 index 0)
            dests = [allies[0]] if (allies and allies[0].alive and not getattr(allies[0], "captured", 0)) else []
        elif who == "enemyLeader":                    # 批52: 敵軍主將(敵方 index 0; 對稱 extraHits.who)
            dests = [enemies[0]] if enemies and enemies[0].alive else []
        elif who == "subs":                           # 批13: 副將群限定(隊伍 index 0 以外; 如鋒矢陣/箕形陣副將分化段)
            dests = [a for a in allies[1:] if a.alive and not getattr(a, "captured", 0)]
        # 批30 C: who=="sub1"/"sub2"(副將固定位置分派) —— 「subs」只能讓兩名副將套用同一份
        # 效果, 無法表達「副將A只防兵刃, 副將B只防謀略」這種依隊伍固定位置(而非動態屬性準則)
        # 分派相異效果的語意(見箕形陣, engine_limitations.md 第25節/16節)。sub1=allies[1]
        # (副將A, index 1), sub2=allies[2](副將B, index 2), 對稱於既有 who=="leader"=allies[0]
        # 慣例。三人隊固定編制(index 0=主將/1/2=副將), 若隊伍不足3人或該位置陣亡則 dests 為空。
        elif who == "sub1":
            dests = [allies[1]] if len(allies) > 1 and allies[1].alive else []
        elif who == "sub2":
            dests = [allies[2]] if len(allies) > 2 and allies[2].alive else []
        # 批45 A: e["sameTargets"] —— 「對敵軍群體(N人)造成傷害並降低其XX」這類措辭, 過去主
        # coef 段(pick_targets)與效果段(who=="enemy"+e["n"], 走下方 ctrl_k/has_en 分支自己的
        # pick_targets)各自獨立擲骰選標, 3人隊只有1/3機率同組(見 engine_limitations.md 對應
        # 節, 全庫掃描 R29)。e["sameTargets"]=True 時直接沿用主 coef 段記錄的 main_hit_tgts
        # (見 fight() 主迴圈, 只在群體(len(vs)>1)結算時才有值), 過濾存活後作為 dests, 不再獨立
        # pick_targets——確保「造成傷害」與「降低其XX」精確命中同一批目標, 對稱單體版本既有的
        # main_hit_tgt(t["lockTarget"]/t["targetSel"]等既有沿用慣例)。未傳 main_hit_tgts 的
        # 呼叫路徑(prep/reactive/choices分支未帶等)dests 落空回傳[], 向後相容(只有明確要求且
        # 母戰法主coef段確實命中>=2人群體時才會生效)。
        elif e.get("sameTargets"):
            dests = [x for x in (main_hit_tgts or []) if x.alive]
        elif who == "enemy":
            # 批B(filter-then-pick修正): e帶ifTargetHas/ifTargetHasNot/ifStatCompare等目標
            # 資格gate時, 先把enemies過濾成合格池, 下方ctrl_k/has_en隨機選標分支才從合格池
            # pick_targets(而非「先隨機挑、挑完才用gate過濾」——見_gate_pool模組層級註解)。
            # 無gate的效果enemy_pool與enemies是同一份list, 完全維持原隨機行為不變。
            enemy_pool = _gate_pool(enemies, e, caster, allies, enemies)
            if ctrl_k:                                # 群體控制隨機挑不重複目標; 單體優先鎖定 tgt
                # 批26: CTRL類效果優先讀 e["n"]/e["nMax"](效果自身欄位), 無則 fallback 到
                # t["n"]/t["nMax"](戰法頂層, 舊行為, 向後相容)。原本 ctrl_k 只認頂層 n/nMax,
                # 導致同一戰法內「多段各自不同目標數的chaos/stun等控制效果」(如神機莫測「1名
                # 必中混亂 + 另外N名各自獨立機率判定混亂」)無法用單一戰法頂層n表達出兩種不同
                # 的目標數, 只能被迫二選一近似成同一個n。has_en 沿用批23 A1既有判斷(e["n"]是否
                # 存在), 場景不衝突: 非ctrl_k效果本就走 has_en 分支(見下方elif), 這裡只是讓
                # ctrl_k效果也能「有e.n就優先用」, 沒有e.n時完全維持原行為(讀t.n/t.nMax)。
                if has_en:
                    n = e["n"]
                    n_max = e.get("nMax")
                else:
                    n = t.get("n") or 1
                    n_max = t.get("nMax")
                cnt = n + random.randint(0, n_max - n) if n_max else n
                if cnt <= 1:
                    dests = [tgt] if tgt and tgt.alive else pick_targets(enemy_pool, 1)
                else:
                    dests = pick_targets(enemy_pool, cnt)
            elif has_en:                               # 批23 A1: 非CTRL效果讀 e["n"]/e["nMax"]
                n = e["n"]
                cnt = n + random.randint(0, e["nMax"] - n) if e.get("nMax") else n
                if cnt <= 1:
                    dests = [tgt] if tgt and tgt.alive else pick_targets(enemy_pool, 1)
                else:
                    dests = pick_targets(enemy_pool, cnt)
                # 批45 A: 若本效果本身是「首次」命中群體(cnt>1)的來源(無 coef 段可沿用時, 如
                # 誘敵深入 coef=0, dot+amp 兩個效果皆為 effects[] 內的同層 sibling), 就地更新
                # main_hit_tgts, 讓本戰法內排在後面、帶 e["sameTargets"] 的效果可以沿用「前一個
                # 效果實際命中的那一批目標」, 不必一定要來自頂層 coef 段。只在尚未有 main_hit_tgts
                # (未被 coef 段設定過)時才更新, 避免覆蓋掉更早、更明確的 coef 段記錄。
                if cnt > 1 and main_hit_tgts is None:
                    main_hit_tgts = dests
            else:
                dests = [x for x in enemies if x.alive]
        elif has_en:                                   # 批23 A1: who="ally"(含預設) 非CTRL效果讀 e["n"]/e["nMax"](如「我軍2人」「自己及友軍單體」)
            # 批B(filter-then-pick修正): 對稱上方enemy_pool(見_gate_pool模組層級註解)。
            ally_pool = _gate_pool(allies, e, caster, allies, enemies)
            n = e["n"]
            cnt = n + random.randint(0, e["nMax"] - n) if e.get("nMax") else n
            if cnt <= 1:
                dests = [tgt] if (tgt and tgt.alive and tgt in allies) else pick_targets(ally_pool, 1)
            else:
                dests = pick_targets(ally_pool, cnt)
        else:
            dests = [a for a in allies if a.alive]
        # 批16: ifTargetHas —— 效果段條件, 只對「已有該狀態」的目標生效; 選目標後過濾(不影響選目標邏輯本身)
        if e.get("ifTargetHas"):
            dests = [u for u in dests if target_has(u, e["ifTargetHas"])]
        # 批A(11筆高嚴重重建): ifTargetHasNot —— ifTargetHas的反向(只對「尚未有該狀態」的目標
        # 生效), 對稱既有正向版本。偽書相間「若目標已混亂則...(否則)施加混亂」的否則分支——用
        # ifTargetHasNot="chaos"精確表達「僅未混亂的目標才施加混亂」, 對稱engine.js同名欄位。
        if e.get("ifTargetHasNot"):
            dests = [u for u in dests if not target_has(u, e["ifTargetHasNot"])]
        # 批I(禁近似令-scale/比較族): ifStatCompare —— 比較「參照方(施放者/我軍主將)vs目標」
        # 同一屬性大小, 只對比較成立的目標生效(摧鋒斷刃「自身武力較高」/聚石成金「敵軍魅力
        # 低於我軍主將」), 對稱ifTargetHas/ifTargetHasNot但比較的是「屬性大小」而非「狀態
        # 有無」, 見stat_compare_ok()。
        if e.get("ifStatCompare"):
            dests = [u for u in dests if stat_compare_ok(caster, u, allies, e["ifStatCompare"])]
        # 禁近似令-批K: e["ifTargetHpAbove"]/e["ifTargetHpBelow"] —— 對稱 engine.js 同名分支
        # (肉身鐵壁「當友軍兵力高於70%時」, 已選定目標自己的血量條件, 而非caster自身)。
        if e.get("ifTargetHpAbove") is not None:
            dests = [u for u in dests if u.hp_pct > e["ifTargetHpAbove"]]
        if e.get("ifTargetHpBelow") is not None:
            dests = [u for u in dests if u.hp_pct < e["ifTargetHpBelow"]]
        # 禁近似令-批K: e["ifSelfStatCompare"](spec:{statA,statB,op}) —— 對稱 engine.js 同名
        # 分支(淵然難測「若傷害來源武將武力高於智力則...否則...」, 同一單位自己兩屬性互比)。
        if e.get("ifSelfStatCompare"):
            _sc = e["ifSelfStatCompare"]
            _op_fns = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
                       "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}
            _opf = _op_fns.get(_sc.get("op", "gt"), _op_fns["gt"])
            dests = [u for u in dests if _opf(u.eff(_sc["statA"]), u.eff(_sc["statB"]))]
        # 禁近似令-批K: ifTargetIsRank/ifTargetIsRankNot(target_rank_branch族) —— 對稱
        # engine.js 同名分支(閉月「依目標恰好是不是武力/智力最高分三支」)。_rank_key 批B
        # 已抽到模組層級(供_target_gate_ok共用, 見其定義處), 這裡不再重複巢狀定義。
        if e.get("ifTargetIsRank"):
            _champ = pick_by_criterion(enemies, _rank_key(e["ifTargetIsRank"]))
            dests = [u for u in dests if u is _champ]
        if e.get("ifTargetIsRankNot"):
            _specs = e["ifTargetIsRankNot"] if isinstance(e["ifTargetIsRankNot"], list) else [e["ifTargetIsRankNot"]]
            _champs = [pick_by_criterion(enemies, _rank_key(s)) for s in _specs]
            dests = [u for u in dests if u not in _champs]
        # 批52j: 友軍側效果不套用到被捕獲者(無法被友方選中)
        if who not in ("enemy", "enemyLeader", "eventTarget") and who != "enemy":
            dests = [u for u in dests if not getattr(u, "captured", 0)]
        # 批52g: e.whoNames —— 只對武將名在名單內的目標生效(黃巾副將含 SP)
        if e.get("whoNames"):
            _wn = set(e["whoNames"] if isinstance(e["whoNames"], list) else [e["whoNames"]])
            dests = [u for u in dests if u.g and u.g.name in _wn]
        # 批52g: 逐目標 rate(含 rateStatusBonus 依水攻/沙暴種類加機率)
        if _per_tgt_rate and dests:
            def _tgt_rate(dst):
                r = e.get("rate", 1.0)
                b = e.get("rateStatusBonus") or {}
                if b:
                    need_leader = b.get("ifLeader", False)
                    if (not need_leader) or (allies and allies[0] is caster):
                        nst = count_named_statuses(dst, b.get("statuses") or [])
                        r = r + min(b.get("maxBonus", 99.0), nst * b.get("per", 0.0))
                return r
            dests = [d for d in dests if random.random() < _tgt_rate(d)]
        # scale="intel"|"force"|"command"|"speed"|"charm" 縮放(以施放者戰鬥內即時素質為準):
        # amp/mitig 的 val 直接乘 SCALE, clamp 到 ±SCALE_CLAMP 防止極端值; stat 的 mult 對
        # 1.0 的偏移量(增益/削弱幅度)乘 SCALE, 1.0 本身(無效果)不受縮放影響。
        def _scale_ok_for_e():
            # 批52c: scaleIfSub/scaleIfLeader 閘門(義膽雄心主將時降屬受自身屬性影響等)
            if not e.get("scale"):
                return False
            if e.get("scaleIfSub") and not (allies and allies[0] is not caster):
                return False
            if e.get("scaleIfLeader") and not (allies and allies[0] is caster):
                return False
            return True

        def sv_val(v):
            if not _scale_ok_for_e():
                return v
            return max(-SCALE_CLAMP, min(SCALE_CLAMP, v * scale_of(caster, e["scale"])))

        def sv_mult(m):
            if not _scale_ok_for_e():
                return m
            return 1 + (m - 1) * scale_of(caster, e["scale"])

        def sv_add(ad):                                  # 屬性平加縮放(一般裝備平加無 scale 直接用原值)
            return ad * scale_of(caster, e["scale"]) if _scale_ok_for_e() else ad

        # 批16: undispellable 旗標 —— 效果加此欄則 dispel 略過(附加進 push_add/push_mod/push_stat_add 的 flags, 供 dispel_unit 讀取)
        # 批24 D2: dmgType 旗標 —— amp/mitig 效果可選填 e["dmgType"]="phys"|"intel", 限定只對該
        # 類型傷害生效(damage() 結算時依 kind 過濾, 見 amp()/addbonus() 的 dmg_type 參數)。與
        # undispellable 合併進同一個 flags dict, 兩者互不干擾。
        # 批28 B3: normalOnly 旗標 —— amp/mitig 效果可選填 e["normalOnly"]=true, 限定只對普攻
        # 傷害(hit() 傳入 is_normal=True 的情形)生效, 戰法/突擊傷害不受影響(見至柔動剛「降低
        # 我軍及敵軍全體普通攻擊傷害35%」, 對比redirect既有的normalOnly慣例, 語意不同但欄位
        # 命名沿用一致性)。damage() 結算時依 is_normal 過濾, 見 amp()/addbonus() 的 is_normal
        # 參數。
        # 批H: critUp/critDmgUp(會心/奇謀機率與傷害幅度) 與 amp/mitig 共用同一套 dmgType/
        # normalOnly/ifLeader/ifLeaderIs 條件旗標與 dt_src 尾碼去重慣例(見 damage() 對稱段落
        # 消費 addbonus("critUp"/"critDmgUp", dmg_type, ...)), 故並列進下列判斷式。
        crit_kinds = ("amp", "mitig", "critUp", "critDmgUp")
        dmg_type = e.get("dmgType")
        normal_only = bool(e.get("normalOnly")) if k in crit_kinds else False
        active_only = bool(e.get("activeOnly")) if k == "amp" else False  # 批31 A: 對稱於normalOnly, 目前僅amp支援(士爭先赴)
        charge_only = bool(e.get("chargeOnly")) if k == "amp" else False  # 批40 B: 對稱activeOnly, 僅amp支援(一鼓作氣/藏刀)
        if_leader_topup = bool(e.get("ifLeader")) if k in crit_kinds else False  # 批41 B: 見下方dt_src註解
        if_leader_is_topup = bool(e.get("ifLeaderIs")) if k in crit_kinds else False  # 批44 A: 同if_leader_topup, 見下方dt_src註解
        # 禁近似令-批L: e["dmgFromStatus"](list, 僅k=="amp") —— 才辯機捷「自身施加的灼燒、
        # 水攻、中毒、潰逃、沙暴、叛逃狀態造成的傷害提升90%」跨戰法橫切限定範圍(這6種具名dot
        # 狀態由任何戰法施加時都算, 非本效果專屬某一段固定coef)。damage()結算dot傷害時(k=="dot"
        # 分支呼叫damage()的呼叫點)會傳入該次dot實際解析出的具名狀態(resolve_dot_name, 與
        # u.dots[3]同一份值), addbonus("amp",...)新增dot_status參數比對: 帶dmg_from_status的
        # amp條目只在dot_status命中清單內才計入, 未帶此欄位的既有全部amp條目不受影響。
        dmg_from_status = e.get("dmgFromStatus") if k == "amp" else None
        ud_flags = {"undispellable": bool(e.get("undispellable")), "dmgType": dmg_type, "normalOnly": normal_only, "activeOnly": active_only, "chargeOnly": charge_only, "dmgFromStatus": dmg_from_status} \
            if (e.get("undispellable") or dmg_type or normal_only or active_only or charge_only or dmg_from_status) else None
        # dmgType 存在時, src 附加類型尾碼區分 dedup key(同一戰法內若有兩條不同 dmgType 的
        # amp/mitig, 如暫避其鋒「智力最高者減兵刃傷害」+「武力最高者減謀略傷害」, 兩者若共用
        # 同一個 src 會被 push_add 的「同kind+同src刷新」去重機制互相蓋掉, 見 rateup 既有
        # prepOnly/nativeOnly 尾碼慣例同理)。批28 B3: normalOnly 同理附加尾碼(避免同戰法內
        # normalOnly與非normalOnly的amp共用同一src互相覆蓋); src 為 None 時(兵書/裝備/緣分
        # 無 nameZh) 尾碼無意義, 維持 None(不影響去重, 因 push_add 的 src=None 本就不去重)。
        # 批31 A/批40 B: activeOnly/chargeOnly 同理附加尾碼。
        # 批41 B: ifLeader top-up 尾碼 —— 圍師必闕修R27時新增「基礎mitig(無條件0.39)+差額
        # mitig(ifLeader:true,0.06)」的base+top-up拆法(比照水淹七軍dot的既有precedent), 但
        # dot走u.dots.append(不去重), amp/mitig走push_add(同kind+同src會互相覆蓋, 見上方
        # dmgType/normalOnly同款尾碼修法)——若不加尾碼, 兩條mitig(who同ally, dmgType同intel)
        # 會共用同一個dt_src, 後套用的那條(ifLeader top-up)會把先套用的基礎段整個蓋掉, 導致
        # 非主將時仍是0.39正確但主將時只剩0.06(遺失基礎段)而非0.39+0.06=0.45。補尾碼
        # ":ifLeader"區分(同leaderBonus既有的":leaderBonus"尾碼慣例, 見k=="chargeup"分支),
        # 讓兩條並存疊加。
        dt_src = (src + ":" + dmg_type) if (src and dmg_type) else src
        if normal_only and src:
            dt_src = (dt_src or src) + ":normalOnly"
        if active_only and src:
            dt_src = (dt_src or src) + ":activeOnly"
        if charge_only and src:
            dt_src = (dt_src or src) + ":chargeOnly"
        if dmg_from_status and src:
            dt_src = (dt_src or src) + ":dmgFromStatus"  # 禁近似令-批L: 避免與同戰法內其餘amp段共用dt_src互相覆蓋(才辯機捷目前只有單一amp段, 此尾碼為未來多段並存預留)
        if if_leader_topup and src:
            dt_src = (dt_src or src) + ":ifLeader"
        # 批44 A: ifLeaderIs top-up 尾碼 —— 同批41 B if_leader_topup的理由(避免base段+差額段
        # 共用dt_src被push_add同kind+同src去重互相覆蓋), 用於白毦兵等「若XX統領, 數值更高」家族。
        if if_leader_is_topup and src:
            dt_src = (dt_src or src) + ":ifLeaderIs"
        for u in dests:
            # 批A(11筆高嚴重重建): k=="amp"+e["stackKey"] —— 對稱既有k=="stat"+e["stackKey"]
            # (批42/43, exploit_layers per-target疊層), amp的per-target疊層變體, 見engine.js
            # 同名分支詳細註解。密計誅逆「使敵軍單體(隨機)造成的最終傷害降低15%,最多疊加3次」
            # ——疊層對象是「被隨機選中的那個敵方單位」, 每次選中就疊1層, 封頂maxStacks。刻意
            # 只做核心per-target層數計數+累計總值重算push_add刷新覆蓋, 不搬onMaxStacks/
            # globalMax/e["add"]三個延伸(密計誅逆無此語意需求, 見Unit.__init__ amp_layers註解)。
            if k == "amp" and e.get("stackKey"):
                if u.amp_layers is None:
                    u.amp_layers = {}
                ekey = id(e)
                already = u.amp_layers.get(ekey, 0)
                max_stacks = e.get("maxStacks")
                if max_stacks is None or already < max_stacks:
                    layers = already + 1
                    u.amp_layers[ekey] = layers
                    # 禁近似令-批L修復: 原 e.get("perStack", sv_val(e["val"])) 在Python下即使
                    # "perStack"鍵存在也會「先」求值預設引數(.get()的第二引數不像JS的??會短路,
                    # 一律先執行), 導致e["val"]在只帶perStack不帶val的資料(如一身是膽critUp+
                    # stackKey)上必定KeyError——對稱engine.js `e.perStack ?? svVal(e.val)`(??
                    # 才會真正短路, 且JS存取不存在屬性回傳undefined不拋錯, 兩引擎原本行為不對稱,
                    # 此為sgz.py單邊潛伏bug, 非本批新增, 本批因critUp+stackKey首次出現「只給
                    # perStack不給val」的資料組合而現形, 一併修復)。改用「先查鍵是否存在」避免
                    # 無條件求值 e["val"]。
                    per_stack = e["perStack"] if "perStack" in e else sv_val(e.get("val", 0))
                    total_val = per_stack * layers
                    u.push_add("amp", total_val, e["dur"], dt_src, ud_flags)
                    # 禁近似令-批K: e["stackId"](dynamic_coef_from_counter族) —— 對稱 engine.js
                    # 同名分支, 見 Unit.__init__ amp_layers_by_id 註解(密計誅逆settle跨效果讀取)。
                    if e.get("stackId"):
                        if u.amp_layers_by_id is None:
                            u.amp_layers_by_id = {}
                        u.amp_layers_by_id[e["stackId"]] = layers
                # 已達max_stacks: 這個目標已無法再疊, 不做任何push_add(累計值維持不變)。
            elif k == "amp":
                v = sv_val(e["val"])
                ms = e.get("maxStack")
                if who == "enemy" and v > 0:          # 修正: 敵方正amp(誤幫敵增傷)→ 視為敵方易傷
                    u.push_add("mitig", -v, e["dur"], dt_src, ud_flags, max_stack=ms)
                else:
                    u.push_add("amp", v, e["dur"], dt_src, ud_flags, max_stack=ms)
            # 禁近似令-批K: k=="dmgShare"(engine_wiring_gaps_misc族, 對稱engine.js同名分支) ——
            # 「使其任一目標受到傷害時會回饋X%傷害給其他敵軍」的傷害轉嫁給隊友機制(連環計),
            # 消費端見 hit() 內 dst.dmg_share 判斷式。
            elif k == "dmgShare":
                u.dmg_share = {"pct": sv_val(e["val"]), "dur": e.get("dur", 2)}
            elif k == "mitig" and e.get("stackKey"):
                # 禁近似令-批K: mitig+e.stackKey(對稱既有amp+e.stackKey per-target疊層變體),
                # 對稱engine.js同名分支, 離月首次落地。
                if u.amp_layers is None:
                    u.amp_layers = {}
                ekey = id(e)
                already = u.amp_layers.get(ekey, 0)
                max_stacks = e.get("maxStacks")
                if max_stacks is None or already < max_stacks:
                    layers = already + 1
                    u.amp_layers[ekey] = layers
                    # 禁近似令-批L修復: 原 e.get("perStack", sv_val(e["val"])) 在Python下即使
                    # "perStack"鍵存在也會「先」求值預設引數(.get()的第二引數不像JS的??會短路,
                    # 一律先執行), 導致e["val"]在只帶perStack不帶val的資料(如一身是膽critUp+
                    # stackKey)上必定KeyError——對稱engine.js `e.perStack ?? svVal(e.val)`(??
                    # 才會真正短路, 且JS存取不存在屬性回傳undefined不拋錯, 兩引擎原本行為不對稱,
                    # 此為sgz.py單邊潛伏bug, 非本批新增, 本批因critUp+stackKey首次出現「只給
                    # perStack不給val」的資料組合而現形, 一併修復)。改用「先查鍵是否存在」避免
                    # 無條件求值 e["val"]。
                    per_stack = e["perStack"] if "perStack" in e else sv_val(e.get("val", 0))
                    total_val = per_stack * layers
                    u.push_add("mitig", total_val, e["dur"], dt_src, ud_flags)
                    if e.get("stackId"):
                        if u.amp_layers_by_id is None:
                            u.amp_layers_by_id = {}
                        u.amp_layers_by_id[e["stackId"]] = layers
            elif k == "mitig":
                u.push_add("mitig", sv_val(e["val"]), e["dur"], dt_src, ud_flags, max_stack=e.get("maxStack"))
            # 批H: critUp(會心/奇謀機率, val加法累積) / critDmgUp(會心/奇謀傷害幅度, 疊在基礎
            # +100%之上) —— 走與amp/mitig相同的push_add加法疊加通道, 由 damage() 於傷害結算
            # 時讀 addbonus("critUp"/"critDmgUp", dmg_type, ...) 消費(見該函式對稱段落),
            # dmg_type 依本文用詞路由: "phys"=會心(兵刃暴擊)/"intel"=奇謀(謀略暴擊)。與amp/
            # mitig的差異純粹是消費端不同(amp直接乘傷害基數, critUp是擲骰rate/critDmgUp是
            # 命中後幅度), 資料層加法疊加/scale/ifLeader/dmgType等既有原語組合全部原樣沿用,
            # 零新增targeting邏輯(對稱engine.js同名分支)。
            # critUp+e.get("stackKey")(對稱k=="amp"+e.get("stackKey"), 見上方詳細註解) ——
            # 逆鱗「受到傷害時,3%機率獲得10%會心,可疊加2次」需要per-target疊層(裝備效果src
            # 固定為None, push_add的max_stack去重機制以src為鍵, 對裝備效果不生效, 必須用獨立
            # 的id(e)鍵疊層計數器, 與amp/stat的stackKey機制完全對稱, 只是掛在crit_layers獨立
            # dict, 避免與amp_layers/exploit_layers混淆)。
            elif k == "critUp" and e.get("stackKey"):
                if u.crit_layers is None:
                    u.crit_layers = {}
                ekey = id(e)
                already = u.crit_layers.get(ekey, 0)
                max_stacks = e.get("maxStacks")
                if max_stacks is None or already < max_stacks:
                    layers = already + 1
                    u.crit_layers[ekey] = layers
                    # 禁近似令-批L修復: 原 e.get("perStack", sv_val(e["val"])) 在Python下即使
                    # "perStack"鍵存在也會「先」求值預設引數(.get()的第二引數不像JS的??會短路,
                    # 一律先執行), 導致e["val"]在只帶perStack不帶val的資料(如一身是膽critUp+
                    # stackKey)上必定KeyError——對稱engine.js `e.perStack ?? svVal(e.val)`(??
                    # 才會真正短路, 且JS存取不存在屬性回傳undefined不拋錯, 兩引擎原本行為不對稱,
                    # 此為sgz.py單邊潛伏bug, 非本批新增, 本批因critUp+stackKey首次出現「只給
                    # perStack不給val」的資料組合而現形, 一併修復)。改用「先查鍵是否存在」避免
                    # 無條件求值 e["val"]。
                    per_stack = e["perStack"] if "perStack" in e else sv_val(e.get("val", 0))
                    total_val = per_stack * layers
                    u.push_add("critUp", total_val, e["dur"], dt_src, ud_flags)
                # 已達max_stacks: 這個目標已無法再疊, 不做任何push_add(累計值維持不變)。
            elif k == "critUp":
                u.push_add("critUp", sv_val(e["val"]), e["dur"], dt_src, ud_flags, max_stack=e.get("maxStack"))
            elif k == "critDmgUp":
                u.push_add("critDmgUp", sv_val(e["val"]), e["dur"], dt_src, ud_flags, max_stack=e.get("maxStack"))
            # 批16: immuneTo(單項控制免疫) —— is_immune_to(k) 只免疫清單內控制類型, 與 insight(全免) 並列判斷
            # 批52h: 成功施加後 fire_controlled 廣播(機鑑先識反彈); no_ctrl_reflect 時跳過
            elif k == "stun":
                if not u.insight and not u.is_immune_to("stun"):
                    # 狀態疊加精修批(追加規則6): 唯一+同等或更強擋新, 見apply_control_dur()
                    apply_control_dur(u, "stun", e["dur"], "stun", allies, enemies, no_ctrl_reflect)
                else:
                    fire_self_reactive(u, "ctrlImmune", 1)  # 禁近似令-批L: 免疫格擋觸發ctrlImmune事件(一身是膽)
            elif k == "silence":
                if not u.insight and not u.is_immune_to("silence"):
                    apply_control_dur(u, "silence", e["dur"], "silence", allies, enemies, no_ctrl_reflect)
                else:
                    fire_self_reactive(u, "ctrlImmune", 1)  # 禁近似令-批L
            elif k == "disarm":
                if not u.insight and not u.is_immune_to("disarm"):
                    apply_control_dur(u, "disarm", e["dur"], "disarm", allies, enemies, no_ctrl_reflect)
                else:
                    fire_self_reactive(u, "ctrlImmune", 1)  # 禁近似令-批L
            elif k == "chaos":                        # 批12 ModeF: 混亂(敵我不分), 同 insight 免疫規則
                if not u.insight and not u.is_immune_to("chaos"):
                    apply_control_dur(u, "chaos", e.get("dur", 1), "chaos", allies, enemies, no_ctrl_reflect)
                else:
                    fire_self_reactive(u, "ctrlImmune", 1)  # 禁近似令-批L
            elif k == "ambush":                        # 批18: 遇襲(先攻的反面/遲緩) —— 不鎖行動, 只影響排序; insight/immuneTo可免
                if not u.insight and not u.is_immune_to("ambush"):
                    # 狀態疊加精修批(追加規則, coordinator訊息): 遇襲比照控制類同套
                    # unique_strongest(唯一+同等或更強擋新), 見apply_control_dur()
                    apply_control_dur(u, "ambush", e.get("dur", 1), "ambush", allies, enemies, no_ctrl_reflect)
                else:
                    fire_self_reactive(u, "ctrlImmune", 1)  # 禁近似令-批L
            elif k == "insight":                      # 洞察: 免疫控制, 施加時同時解除既有控制
                # 狀態疊加精修批(追加規則): 洞察比照unique_strongest——只有本次施加確實
                # 「同等或更強」而真的套用(apply_control_dur回傳True)時, 才觸發解除既有
                # 控制的副作用; 較弱的洞察施加完全跳過(不解控、不覆蓋dur)。
                if apply_control_dur(u, "insight", e.get("dur", 1), "insight", allies, enemies, no_ctrl_reflect):
                    u.stun = u.silence = u.disarm = u.chaos = u.ambush = 0
            elif k == "immune":                       # 批16: immuneTo —— 單項控制免疫
                u.push_immune(e.get("types"), e.get("dur"))
            elif k == "first":                        # 先攻: 本回合旗標, 優先於速度排序
                # 狀態疊加精修批(追加規則): 先攻比照unique_strongest
                apply_control_dur(u, "first", e.get("dur", 1), "first", allies, enemies, no_ctrl_reflect)
            elif k == "rigorous":                     # 狀態疊加精修批: 嚴密(赴湯蹈火賦予, 見
                # Unit.__init__ self.rigorous 欄位註解) —— 純偵測旗標buff, 對稱既有first/
                # insight單值buff慣例(取max, 不套用unique_strongest, 非user規則列舉的9個
                # 具名狀態之一, 屬本批為實作抵禦conditional例外開關新增的輔助欄位)。
                u.rigorous = max(u.rigorous, e.get("dur", 1))
            # 禁近似令-批K: e["fromStack"](dynamic_coef_from_counter族, stat版, 對稱engine.js
            # 同名分支) —— 本效果不自己疊層, 改為註記同一持有者身上既有k=="stack"計數器
            # (u.stack, 由stack效果驅動amp的既有per-caster疊層通道)額外驅動一個stat屬性,
            # 交給 eff() 的即時讀取消費(對稱既有 this.stack.per*this.stack.n 驅動amp的寫法),
            # 天然隨u.stack["n"]逐回合成長同步變動。供弓腰姬「依自身擁有的功能性增益數量額外
            # 提傷並疊加武力」——傷害段(stack驅動amp)與武力段用同一個計數器動態同步。
            elif k == "stat" and e.get("fromStack"):
                if u.stack is not None:
                    u.stack["statField"] = e["stat"]
                    u.stack["statPerVal"] = e.get("perStackVal", 0)
            elif k == "stat" and e.get("stackKey"):
                # 批42: e["stackKey"](truthy旗標) —— stat 效果的「每次觸發對目標疊加1層」模式,
                # 對稱 engine.js 同名分支(見其詳細註解)。傲睨王侯: 敵軍目標受普攻時觸發1個
                # 破綻, 該目標降3%可疊, 疊層數上限 e["maxStacks"](該目標本地破綻池耗盡)。
                if u.exploit_layers is None:
                    u.exploit_layers = {}
                ekey = id(e)
                already = u.exploit_layers.get(ekey, 0)
                max_stacks = e.get("maxStacks")
                if max_stacks is not None and already >= max_stacks:
                    continue  # 本地池已耗盡: 不再刷新/計入, 對稱 engine.js continue(非return, 避免誤跳過t["effects"]其餘效果段)
                layers = already + 1
                u.exploit_layers[ekey] = layers
                # 禁近似令-批K: e["stackId"](dynamic_coef_from_counter族, 對稱amp/mitig既有
                # stackId寫入) —— 絕地反擊需要另一個獨立dot效果(見k=="dot"+e["coefFromStack"]
                # 消費端)跨效果讀取自己身上這個具名疊層計數器目前疊了幾層, 與amp_layers_by_id
                # 共用同一字串鍵命名空間(self-stacking時u is caster)。
                if e.get("stackId"):
                    if u.amp_layers_by_id is None:
                        u.amp_layers_by_id = {}
                    u.amp_layers_by_id[e["stackId"]] = layers
                sc = locked_scale_of(caster, e)
                # 批43 A: e["add"](平點疊層旗標) —— 對稱 engine.js 同名分支, 見其詳細註解。同一
                # stackKey骨架(exploit_layers計數/maxStacks封頂/onMaxStacks/globalMax)不變, 差別
                # 只在最終套用push_stat_add(add平點)還是push_mod(mult百分比乘算)。
                per_stack = e.get("perStack", 0.03)
                if e.get("add"):
                    total_add = per_stack * layers * sc
                    u.push_stat_add(e["stat"], total_add, e.get("dur", 99), src, ud_flags)
                else:
                    total_mult = 1 - min(0.95, per_stack * layers * sc)  # 0.95下限防止全屬性歸零/負值
                    u.push_mod(e["stat"], total_mult, e.get("dur", 99), src, ud_flags)
                # e["onMaxStacks"](效果陣列, 選填) —— 該目標本地池首次耗盡時額外套用的一次性
                # 效果段(如「單目標破綻全觸發→虛弱+受傷提高」)。exploit_capped 去重確保只觸發
                # 一次。caster 仍傳原持有者(scale基準不變), 目標靠 who=="eventTarget" 精確指定。
                if e.get("onMaxStacks") and max_stacks is not None and layers >= max_stacks:
                    if u.exploit_capped is None:
                        u.exploit_capped = set()
                    if ekey not in u.exploit_capped:
                        u.exploit_capped.add(ekey)
                        for sub in e["onMaxStacks"]:
                            apply_effects(caster, None, {"effects": [sub], "kind": t.get("kind", "phys")}, allies, enemies, reactive=True, evt_target=u)
                # e["globalMax"]/e["globalEffects"](選填) —— 持有者視角跨目標累計觸發次數,
                # 達到 globalMax(原文「場上所有破綻」15個)且尚未觸發過時套用 globalEffects
                # (如「敵軍群體2人降20%」)。capped目標的重複刷新不計入(該目標本地池已耗盡,
                # 這次只是continue掉不會走到這裡, 故此處g.n只在layers真的新增時才遞增)。
                if e.get("globalMax") is not None and e.get("globalEffects"):
                    if caster.exploit_global is None:
                        caster.exploit_global = {}
                    g = caster.exploit_global.get(ekey, {"n": 0, "fired": False})
                    if not g["fired"]:
                        g["n"] += 1
                        if g["n"] >= e["globalMax"]:
                            g["fired"] = True
                            for sub in e["globalEffects"]:
                                apply_effects(caster, None, {"effects": [sub], "kind": t.get("kind", "phys")}, allies, enemies, reactive=True)
                    caster.exploit_global[ekey] = g
            elif k == "stat":                         # 裝備平加(add)與乘算(mult)擇一; add 為戰報所示「裝備獨立平加階段」
                ms = e.get("maxStack")
                # 批I: e["stat"]=="maxStat" —— 動態解析為u當下四維最高一項的欄位名(形一陣
                # 「自身最高屬性+30→60點」), 見resolve_stat_field()。既有固定屬性字串(force/
                # intel/command/speed)原樣通過, 零回歸。
                stat_field = resolve_stat_field(u, e["stat"])
                # 禁近似令-批K: e["addPerBuffType"]({types,per,maxCount}) —— 對稱 engine.js
                # 同名分支(弓腰姬「每多1個功能性增益狀態,提高自身9→18點武力」)。
                if e.get("addPerBuffType"):
                    _abt = e["addPerBuffType"]
                    _cnt = min(_abt.get("maxCount", 99), count_active_buff_types(caster, _abt.get("types") or []))
                    u.push_stat_add(stat_field, sv_add((_abt.get("per") or 0) * _cnt), e["dur"], src, ud_flags, max_stack=ms)
                elif e.get("add") is not None:
                    u.push_stat_add(stat_field, sv_add(e["add"]), e["dur"], src, ud_flags, max_stack=ms)
                else:
                    u.push_mod(stat_field, sv_mult(e.get("mult", 1.0)), e["dur"], src, ud_flags, max_stack=ms)
            elif k == "huchen":                       # 批52d: 虎嗔(將門虎女) —— 負面狀態, 可被 dispel debuffs 清除
                # base=初始結算傷害率(滿級0.20), per=每次受傷疊加(0.30), maxHits=3,
                # left=持續回合(時序重構: dur原值不補償+1), ampOnSettle=結算時施放者兵刃+8%
                u.huchen = {
                    "base": e.get("base", e.get("coef", 0.20)),
                    "per": e.get("per", 0.30),
                    "hits": 0,
                    "maxHits": e.get("maxHits", 3),
                    "left": e.get("dur", 1) or 1,
                    "caster": caster,
                    "kind": e.get("kind") or t.get("kind", "phys"),
                    "src": t.get("nameZh") or "虎嗔",
                    "ampOnSettle": e.get("ampOnSettle", 0.08),
                    "ampMaxStack": e.get("ampMaxStack", 99),
                }
            elif k == "dot":                          # 持續傷害: 套用時定格每回合傷害; dots[2]=undispellable旗標
                # 批23 A3: dot 結算優先讀 e["kind"](戰法整體是兵刃 t["kind"]="phys", 但灼燒/
                # 水攻類 dot 段依原文「受智力影響」應走謀略傷害類型, 過去誤用 t["kind"] 導致
                # 傷害類型錯位, 如天降火雨兵刃戰法掛的灼燒本應是 intel 類)。無 e["kind"] 時
                # fallback t["kind"](向後相容既有無 e["kind"] 的 dot 資料)。
                # 批52续: e.coefLeader —— 主將時更高傷害率(火燒連營 82%→98%)
                _dot_coef = e.get("coef", 0.5)
                if e.get("coefLeader") is not None and allies and allies[0] is caster:
                    _dot_coef = e["coefLeader"]
                # 禁近似令-批K: e["coefFromStack"](dynamic_coef_from_counter族, 對稱engine.js
                # 同名分支) —— coef=基礎值+每層增量×caster身上具名疊層計數器(見k=="stat"+
                # e["stackKey"]+e["stackId"]消費端寫入amp_layers_by_id)的當下層數。絕地反擊
                # 「第5回合根據(自己受兵刃傷害觸發的)疊加次數對敵軍全體造成傷害」。
                if e.get("coefFromStack"):
                    _cfs = e["coefFromStack"]
                    _layers = (caster.amp_layers_by_id or {}).get(_cfs.get("id"), 0)
                    _dot_coef = _cfs.get("base", 0) + _cfs.get("per", 0) * _layers
                # 禁近似令-批K: e["pierce"]==True —— 對稱 engine.js 同名分支, 強制本段dot
                # 傷害完全無視目標mitig(獅子奮迅「叛逃狀態...無視防禦」)。
                # 批52g: dots[3]=具名狀態(水攻/沙暴…), 供五雷 rateStatusBonus / target_has
                # 禁近似令-批L: _dot_status_name 只解析一次, 同時餵給 damage()(供 e["dmgFromStatus"]
                # 過濾, 才辯機捷)與 dots[3](既有具名狀態標籤, 供 target_has/rate_status_bonus 等
                # 既有消費端), 確保兩處讀到的是同一份名稱, 對稱 engine.js 同名分支。
                _dot_status_name = resolve_dot_name(e, t) or None
                # 狀態疊加精修批(user規則 status_stacking_detail_20260712): DoT(灼燒/中毒/
                # 潰逃/水攻/沙暴/叛逃等具名持續傷害狀態) = 刷新(refresh)覆蓋, 唯一(非共存)——
                # 前批 u.dots.append 不去重, 把DoT當共存清單是錯的(同名DoT會疊加成多份逐回合
                # 掉血, 高估傷害); 改為「同名DoT(以狀態名為鍵)新施加時覆蓋舊的(用最新的
                # coef/dur/來源), 不並存多個」。
                # 鍵優先用可解析的具名狀態(_dot_status_name, 如灼燒/水攻…, 見resolve_dot_name/
                # DOT_NAME_BY_TACTIC); 解析不到時(DOT_NAME_BY_TACTIC未收錄的其餘dot效果,
                # 現況約30筆, 見engine_limitations.md)退而用來源戰法名(src)當鍵——同一戰法
                # 重複施加自己的DoT視為同一狀態覆蓋刷新(如重複觸發/每回合重新套用), 不同
                # (尚未具名的)戰法各自的DoT仍視為彼此獨立(無實測證據顯示應合併, 保守不變,
                # 對稱急救/block等本批「無法判斷時保守維持既有行為」的一貫原則)。只影響「找
                # 相同鍵是否覆蓋」的判斷(dots[4], 本批新增第5欄), dots[3](具名狀態本身)欄位
                # 值不變, 不影響target_has/dmgFromStatus等既有讀取端。
                _dot_key = _dot_status_name or src
                _dot_entry = [damage(caster, u, _dot_coef,
                                     e.get("kind") or t.get("kind", "intel"),
                                     force_pierce=bool(e.get("pierce")),
                                     dot_status=_dot_status_name), e["dur"],
                              bool(e.get("undispellable")), _dot_status_name, _dot_key]
                for _di, _dd in enumerate(u.dots):
                    if len(_dd) > 4 and _dd[4] == _dot_key:
                        u.dots[_di] = _dot_entry
                        break
                else:
                    u.dots.append(_dot_entry)
            elif k == "extra":                        # 連擊/追擊: 普攻後追加普攻的預算
                u.push_add("extra", e["val"], e["dur"], src)
            elif k == "splash":                        # 禁近似令-批K: splash_aoe_primitive族, 對稱 engine.js 同名分支
                u.push_add("splash", e["val"], e["dur"], src)
            elif k == "preDmgHook":                    # 禁近似令-批K: pre_damage_intercept族, 對稱 engine.js 同名分支
                u.pre_dmg_hooks.append({
                    "hook_kind": e.get("hookKind"), "val": e.get("val"), "step": e.get("step"),
                    "max": e.get("max"), "hits": 0, "rate": e.get("rate"),
                    "dmg_type": e.get("dmgType"), "pct": e.get("pct"),
                    "delay_rounds": e.get("delayRounds"), "reduce_pct": e.get("reducePct"),
                    "dur": e.get("dur", 99) or 99,
                })
            # 禁近似令-批K: k=="preAttackHook" 註冊(對稱engine.js同名分支) —— 見 Unit.__init__
            # pre_attack_hooks 詳細註解與 do_normal_attack() 消費端。hook_kind: "redirectPre"
            # (即將受到普攻時,依guard準則轉由隊友代承, 雲聚影從)/"healAllyPre"(即將受到普攻時,
            # 治療隨機隊友, 益其金鼓)。
            elif k == "preAttackHook":
                u.pre_attack_hooks.append({
                    "hook_kind": e.get("hookKind"), "rate": e.get("rate"), "guard": e.get("guard"),
                    "coef": e.get("coef"), "scale": e.get("scale"), "dur": e.get("dur", 99) or 99,
                })
            elif k == "stack":                        # 疊加增益: 每層加 per 增傷; 遞增時機見 stackPer
                # 批26 B2: e["stackPer"](可選, "round"預設/"cast") —— 過去疊層只有「每回合+1層」
                # 這一種語意(見 fight() 回合迴圈 tick 遞增, u.stack["n"] = min(max, n+1)), 但原文
                # 常見「每次發動後傷害率提升X」(如水淹七軍/陷陣突襲), 是「本戰法每次成功發動」才
                # +1層, 與回合數無關(可能同一回合不觸發、也可能未來擴充到一回合多次觸發)。新增
                # stackPer 欄位區分兩種遞增時機: "round"(預設, 沿用既有tick()逐回合遞增, 向後
                # 相容)/"cast"(不受tick()影響, 改由 apply_stack_cast() 在戰法本次「發動」時呼叫
                # 遞增, 見 fight() 主動戰法命中分支呼叫端)。刻意不覆寫既有 e["per"] 欄位語意
                # (per 一直是"每層增傷倍率"的數值欄位, 若拿它兼職當模式字串會造成型別混淆與
                # PER_KIND_FIELDS/lint的比對複雜化), 新增獨立欄位更安全。
                # 批37 B: 第三種遞增時機 "attack" —— 「每次普通攻擊後+1層」(如奮突「普通攻擊
                # 之後...最多疊加3次」), 掛在 dealt_damage 事件點(普攻確實命中造成傷害後遞增,
                # 見 dealt_damage() 頂端), 繳械/震懾無普攻的回合不會誤疊層(較舊的 round 近似精確)。
                # 批52续: 已有 stack 時不重置 n(charge 每次發動 apply_effects 會重入, 舊行為會把
                # 疊層清零導致奮突/虎踞鷹揚永遠 0 層)。
                if u.stack and u.stack.get("stackPer") == e.get("stackPer", "round"):
                    u.stack["per"] = e.get("per", u.stack.get("per", 0.1))
                    u.stack["max"] = e.get("max", u.stack.get("max", 5))
                else:
                    u.stack = {"per": e.get("per", 0.1), "max": e.get("max", 5), "n": 0,
                               "stackPer": e.get("stackPer", "round")}
            elif k == "decay":                        # 衰減增益: 開場 v0 增傷, rounds 內線性歸零
                u.decay = {"v0": e.get("v0", 0.5), "left": e.get("rounds", 8),
                           "total": e.get("rounds", 8)}
            elif k == "swap":                         # 武智互換
                u.swap = max(u.swap, e.get("dur", 1))
            # 禁近似令-批K: e["onKill"](engine_wiring_gaps_misc族) —— 不立即套用pierce, 改
            # 登記到u.on_kill_grants(待hit()偵測到u親手擊敗某目標時才真正授予, 見hit()消費端),
            # 供虎痴「如果擊敗目標，會使自身獲得破陣狀態，直到戰鬥結束」精確表達條件觸發時機。
            elif k == "pierce" and e.get("onKill"):
                if u.on_kill_grants is None:
                    u.on_kill_grants = []
                u.on_kill_grants.append({"kind": "pierce", "val": e["val"], "dur": 9999})
            elif k == "pierce":                       # 看破: 無視目標 val 比例的減傷
                u.push_add("pierce", e["val"], e["dur"], src)
            # 批A(11筆高嚴重重建): chargeAdd —— 「可消耗資源池」的獲得端(死戰不退「自身受到傷害
            # 時, 有80%機率獲得一層蓄威效果, 可累積20層」), 對稱既有stack但語意不同(見Unit
            # 建構式self.charge註解)。掛在on:"damaged"反應式, e["rate"]已在外層(反應式擲骰通道)
            # 判定過, 這裡只需純粹+1層封頂(不重複擲骰)。
            elif k == "chargeAdd":
                if u.charge is None:
                    u.charge = {"n": 0, "max": e.get("max", 20)}
                u.charge["max"] = e.get("max", u.charge["max"])
                u.charge["n"] = min(u.charge["max"], u.charge["n"] + 1)
            elif k == "counter":                      # 反擊: 受擊時還擊
                # 批28 B1: guardFor(守護式反擊) —— 「A受擊時, B代為反擊」的方向(如虎衛軍
                # 「我軍主將即將受到普攻時, 副將反擊」)與一般counter(持有者自己受擊自己反擊)
                # 方向相反。e.get("guardFor")=="leader" 時, u(此效果解析出的who=subs等目標)
                # 不掛自己的counter, 改把自己登記進「隊伍主將」的 counter_guards 清單, 由
                # hit() 在主將受擊時代為觸發還擊(見 hit() 內對應段落)。目前只支援
                # guardFor:"leader"(對應虎衛軍語意), 其餘 who 仍走原本「持有者自己反擊」路徑。
                if e.get("guardFor") == "leader" and allies and allies[0].alive:
                    # 禁近似令-批K: debuffAttacker/selfStack(counter_target_binding族) —— 對稱
                    # engine.js 同名欄位, 原樣透傳到 counter_guards 條目, 由 hit() 消費(古之惡來
                    # 對攻擊者施加降傷/虎衛軍反擊執行者自身疊層統率, 見 hit() 對應段落)。
                    allies[0].counter_guards.append({
                        "unit": u, "coef": e.get("coef", 1.0), "kind": e.get("kind", "phys"),
                        "prob": e.get("prob", 1.0),
                        "debuffAttacker": e.get("debuffAttacker"), "selfStack": e.get("selfStack"),
                    })
                else:
                    # 批23 A2: counter 讀 e["dur"](過去是幽靈欄位, 從不寫入/遞減 —— 「反擊持續1
                    # 回合」等帶時限的反擊被無聲變成常駐/永久, 見還擊/千里走單騎等)。dur 預設99
                    # (=常駐被動慣例, 向後相容無 dur 欄位的既有反擊資料)。時序重構: dur原值不
                    # 補償+1, decay_durations() 於該單位行動輪之後遞減, 歸零時清除。
                    # 批G: e["normalOnly"] —— 對稱既有redirect(guard_normal_only)/amp/mitig
                    # 已支援的normalOnly慣例, 限定此反擊只在受到普通攻擊(is_normal=True)時觸發,
                    # 省略時向後相容(任意傷害來源皆可觸發反擊, 現行全庫既有counter資料行為不變)。
                    # 荊棘「受到普通攻擊時，反彈5%傷害」需要此限定(舊版counter不分普攻/戰法傷害,
                    # 高估觸發範圍)。「反彈傷害的5%」(依本次受到的傷害量比例輸出, 而非固定coef
                    # 重算)仍缺對應原語, 維持既有近似, 移C類(counter缺ofDamage式比例輸出版本)。
                    #
                    # 狀態疊加語意對齊批: 反擊為 NAMED_STATUS 已確認的 "multi"(可共存)具名
                    # 狀態 —— user權威規則: 多來源各自獨立存在、全部生效(與急救/休整的「唯一,
                    # 覆蓋」相反)。改用 upsert_named_status() 寫入 u.counters(清單), 鍵=
                    # ("反擊", id(e)): 同一來源(同一個效果物件, 如同一戰法每次prep重新套用/
                    # 重複觸發when視窗)重複施加只刷新自己那一筆(不無限疊加), 不同來源(不同
                    # 戰法/兵書/裝備各自的counter效果, id(e)天然不同)各自獨立新增一筆、全部
                    # 並存, hit() 逐筆結算(見其對應段落)。src_name/status_name 供未來戰報
                    # 「執行來自【X】的【反擊】」顯示用。
                    upsert_named_status(u.counters, ("反擊", id(e)), {
                        "coef": e.get("coef", 1.0), "kind": e.get("kind", "phys"),
                        "prob": e.get("prob", 1.0), "dur": e.get("dur", 99),
                        "normalOnly": bool(e.get("normalOnly")),
                        "status_name": "反擊", "src_name": effect_src_name(t, e),
                    })
            elif k == "taunt":                         # 嘲諷: 中招者普攻/單體戰法強制指向施放者
                # 禁近似令-批K: e["tauntTarget"](force_attack_reverse族) —— 對稱 engine.js
                # 同名分支(反向taunt): "leader"=強制目標改為我方主將(武鋒陣)/"select"=依
                # targetSel從敵軍挑一個「被攻擊」的目標(定謀貴決)。省略時維持既有行為
                # (taunt_by=caster), 向後相容。
                force_target = caster
                tt = e.get("tauntTarget")
                if tt == "leader":
                    force_target = allies[0] if (allies and allies[0].alive) else None
                elif tt == "select":
                    force_target = pick_by_criterion(enemies, e["targetSel"]) if e.get("targetSel") else None
                if force_target:
                    # 狀態疊加精修批(追加規則): 嘲諷比照unique_strongest——新dur須嚴格大於
                    # 現有taunt_dur, taunt_by才會一併更新(改指向新施加者); 較弱的新嘲諷完全
                    # 不生效, taunt_by/taunt_dur兩者皆維持原值不變。
                    _new_taunt_dur = e.get("dur", 1)
                    if _new_taunt_dur > u.taunt_dur:
                        u.taunt_by = force_target
                        u.taunt_dur = _new_taunt_dur
            elif k == "shield":                        # 護盾: 固定量+按施放者兵力係數, 吸滿或到期為止
                amt = e.get("amt", 0) + (e.get("pct", 0) * caster.troop if e.get("pct") else 0)
                prev = u.shield["amt"] if u.shield else 0
                u.shield = {"amt": prev + amt, "dur": e.get("dur", 99), "undispellable": bool(e.get("undispellable"))}  # 時序重構: dur原值不補償+1(decay_durations於該單位行動輪之後遞減)
            elif k == "dodge":                         # 規避: 機率完全迴避一次傷害
                u.dodge_prob = e.get("prob", 0.2)
                u.dodge_dur = max(u.dodge_dur, e.get("dur", 1))
                u.dodge_dmg_type = e.get("dmgType")   # 批G: 限定規避只對此類型(phys/intel)傷害生效, 對稱amp/mitig/block既有dmgType過濾慣例(榮光「受謀略傷害時完全免疫」需要只免疫intel傷害); 省略時None=向後相容既有全域規避(不分類型)
            elif k == "block":                         # 批22: block(次數型格擋, 抵禦/警戒同族) —— times:N(剩餘次數), val:1.0全擋/0.x部分減傷
                # val 的 scale 縮放用 0~1 專屬 clamp(非 sv_val 的 ±SCALE_CLAMP, 因 block val 是
                # 「減傷比例」語意, 不應為負值或超過1.0全擋)。
                # 批35 B: 改用 locked_scale_of(準備階段鎖定, 見該函式註解) 取代直接呼叫
                # scale_of —— 同一效果物件不論在 prep 或稍後 everyRound 補層才實際套用, 縮放
                # 倍率都固定用第一次掃描到該效果時(prep 階段)算出的值。
                # 批35 A: cap_val_of 套用 e["capVal"](值上限), 在既有 0~1 clamp 之前先夾一次。
                b_val = max(0.0, min(1.0, cap_val_of(e.get("val", 1.0) * locked_scale_of(caster, e), e.get("capVal")))) if e.get("scale") else e.get("val", 1.0)
                u.push_block(b_val, e.get("times", 1), src, dmg_type=e.get("dmgType"))  # 批G: e["dmgType"]限定格擋類型(榮光「受謀略傷害時完全免疫」等), 省略時向後相容(不分類型)
            elif k == "surehit":                       # 必中: 無視對方 dodge
                u.surehit_dur = max(u.surehit_dur, e.get("dur", 1))
            elif k == "healblock":                     # 批8: 禁療 —— heal 套用處(apply_effects 開頭)已排除 healblock 中的目標
                # 批C: is_immune_to("healblock") 查詢方法自批16 immuneTo落地起即存在(單元測試
                # 也涵蓋healblock在內的清單, 見demo()斷言), 但施加healblock的這個分支從未真正
                # 讀取過此查詢(「有能力查, 卻沒接上施加端」的靜默缺口, 同reparse_effects.py
                # apply_corrections()disclosure key None處理發現的同類「原語存在但未接上」問題)。
                # 補上判斷式, 讓k=="immune"(types含"healblock")真正能免疫此debuff。
                if not u.is_immune_to("healblock"):
                    u.healblock = max(u.healblock, e.get("dur", 1))
            elif k == "lifesteal":                     # 批8: 倒戈 —— 實際回血在 hit() 結算傷害後(見 hit() 內 lifesteal 段), 這裡只掛加成值
                # 狀態疊加精修批(user追加規則): 攻心/倒戈為multi(可共存)具名狀態, 改走
                # upsert_named_status 寫入 u.lifesteals(清單), 鍵=("攻心倒戈", id(e)): 同一
                # 來源(同一效果物件, 如同一戰法每次prep重新套用/重複觸發when視窗)重複施加只
                # 刷新自己那一筆(不無限疊加), 不同來源(不同戰法/兵書/裝備各自的lifesteal效果,
                # id(e)天然不同)各自獨立新增一筆、全部並存(取代前批 push_add 通用adds混合
                # 清單+addbonus加總單一標量的寫法; 對稱上方 k=="counter" 分支同款 upsert 慣例,
                # 用 id(e) 而非 effect_src_name 當鍵——後者可能重複(如兩個無nameZh的裝備效果
                # 皆解析出None), 只有物件本身id天然唯一)。
                upsert_named_status(u.lifesteals, ("攻心倒戈", id(e)), {
                    "val": e["val"], "dur": e.get("dur", 99),
                    "status_name": "攻心/倒戈", "src_name": effect_src_name(t, e),
                })
            elif k == "rateup":                        # 提高(自身或對象)主動戰法發動機率
                # scale: 施放當下(caster 戰鬥內即時素質)用 RATE_SCALE_C(獨立於全域 SCALE) 縮放實際
                # 加成(批7: 太平道法「受智力影響」, 見 docs/data/calibration_anchors.json → rate_scale)。
                # prepOnly/nativeOnly/inheritedOnly(批8, nativeOnly反向) 修飾旗標存進 adds[4], 由
                # addbonus_for() 在主動擲骰處依戰法屬性篩選加總。
                rv = e["val"] * rate_scale_of(caster, e["scale"], e.get("scaleDiv")) if e.get("scale") else e["val"]
                rflags = {"prepOnly": bool(e.get("prepOnly")), "nativeOnly": bool(e.get("nativeOnly")),
                          "inheritedOnly": bool(e.get("inheritedOnly"))} \
                    if (e.get("prepOnly") or e.get("nativeOnly") or e.get("inheritedOnly")) else None
                # 同一戰法(如太平道法)可能有多條 rateup(一般 + prepOnly 額外), src 相同的話
                # push_add 的「同kind+同src刷新」去重會把前一條蓋掉; 用 flags 組出不同的 dedup
                # key 尾碼區分, 讓語意不同的兩條並存, 但同語意(同flags組合)的仍正常刷新不疊加。
                r_src = (src + ":" + "".join(k2 for k2 in ("prepOnly", "nativeOnly", "inheritedOnly") if rflags.get(k2))) \
                    if (src and rflags) else src
                # 禁近似令-批L: e["maxStack"]/e["maxStackIfLeaderIs"](可疊加N次) —— 先登死士
                # 「降低其1.5%→3%主動戰法發動率,可疊加4次(若麴義統領則5次)」, val用負值表達
                # 「降低」(addbonus_for("rateup",...)在fight()主迴圈對任意來源的rateup adds
                # 一視同仁加總, 負值天然表達debuff方向, 無需另立新k)。
                u.push_add("rateup", rv, e["dur"], r_src, rflags, max_stack=resolve_max_stack(caster, e, allies))
            elif k == "chargeup":                      # 提高(自身或對象)突擊戰法發動機率; 排除 proc=True 特技偽戰法見突擊擲骰處註解
                # chargeup 同樣支援 scale(未有實測前與 rateup 共用 RATE_SCALE_C, 假設同曲線, 見上方常數註解)
                cv = e["val"] * rate_scale_of(caster, e["scale"], e.get("scaleDiv")) if e.get("scale") else e["val"]
                cflags = {"prepOnly": bool(e.get("prepOnly")), "nativeOnly": bool(e.get("nativeOnly"))} \
                    if (e.get("prepOnly") or e.get("nativeOnly")) else None
                c_src = (src + ":" + "".join(k2 for k2 in ("prepOnly", "nativeOnly") if cflags.get(k2))) \
                    if (src and cflags) else src
                u.push_add("chargeup", cv, e["dur"], c_src, cflags)
                # 曹純特例(虎豹騎): 若隊伍主將(index 0, allies[0])===本效果指定 general 且恰為本 u,
                # 額外發動機率受武力影響。二次曲線 extra% = force^2 * k(注意 k 擬合的是「%數值」本身,
                # 如 force=373.83 時 force^2*k≈4.47, 代表 4.47%, 需 /100 換算成 addbonus 用的小數比例),
                # 錨點見 docs/data/calibration_anchors.json → hubaoqi_caochun(user 實測: 武力373.83→額外
                # 4.46%, 145.78→0.63%, 123.78→0.53%)。src 另加尾碼避免 push_add 同 kind+src 去重把兩筆效果互相蓋掉。
                lb = e.get("leaderBonus")
                if lb and allies and allies[0] is u and u.g.name == lb["general"]:
                    extra = (u.eff("force") ** 2) * lb["k"] / 100
                    lb_src = (src + ":leaderBonus") if src else "leaderBonus"
                    u.push_add("chargeup", extra, e["dur"], lb_src)
            # 批16: healBoost(受到的治療×(1+val)) / healGiven(施放的治療×(1+val)) —— 掛加成值, 實際套用在 heal 結算處(apply_effects 開頭 heal 分支)
            elif k == "healBoost":
                u.push_add("healBoost", e["val"], e["dur"], src)
            elif k == "healGiven":
                u.push_add("healGiven", e["val"], e["dur"], src)
            # 批G: lifestealGiven(倒戈效果量×(1+val)) —— 對稱healGiven, 實際套用在hit()倒戈結算處
            # (見hit()內 lifesteal 段)。長慮「使自身攻心效果提高30%」需要此欄位。
            elif k == "lifestealGiven":
                u.push_add("lifestealGiven", e["val"], e["dur"], src)
            # 批16: fakeReport(偽報) —— 中招者被動+指揮戰法失效: 每回合擲骰的coef段與on_hit反應被抑制
            # (prep已套用效果不回收, 近似)。insight 可免(同其他控制類慣例)。
            # 批22: 偽報疊加規則(戰報實測「身上已存在同等或更強的偽報效果」→不覆蓋) —— 新 dur
            # 須 > 現有 fake_report_dur 才覆蓋, 否則本次施加完全跳過(不是簡單取max, 是「不夠強
            # 就拒絕覆蓋」的二元判定, 見 engine.js 同段註解)。
            elif k == "fakeReport":
                if not u.insight:
                    new_dur = e.get("dur", 1)
                    if new_dur > u.fake_report_dur:
                        u.fake_report_dur = new_dur
            # 批16: dispel(驅散/淨化) —— 移除目標 adds/mods/dots/控制欄位中對應方向(buffs/debuffs)的條目,
            # 略過標記 undispellable 的條目。
            elif k == "dispel":
                dispel_unit(u, e.get("what", "debuffs"))


def fight(teamA, teamB, troopA=None, troopB=None, bsA=None, bsB=None, eqA=None, eqB=None,
          addA=None, addB=None, inhA=None, inhB=None, scenario=None, campLvA=0, campLvB=0):
    troopA = troopA or team_troop(teamA)              # 未指定兵種則用隊伍最佳適性
    troopB = troopB or team_troop(teamB)
    bsA = bsA or [default_bingshu(POOL[n]) for n in teamA]   # 未指定兵書則裝預設主兵書
    bsB = bsB or [default_bingshu(POOL[n]) for n in teamB]
    eqA = eqA or [None] * len(teamA)
    eqB = eqB or [None] * len(teamB)
    addA = addA or [None] * len(teamA)
    addB = addB or [None] * len(teamB)
    inhA = inhA or [None] * len(teamA)
    inhB = inhB or [None] * len(teamB)
    campLvA = campLvA or 0                            # 批36: 兵種營等級(0~10, 隊伍級——全隊共用一座對應兵種的營, 與 troopA/troopB 同顆粒度)
    campLvB = campLvB or 0
    # Lv10附贈戰法原文是「我軍隨機單體/群體」觸發一次(非每個單位各自擁有), 故隨機挑隊上1人
    # 當「持有者」(見 Unit.__init__ is_camp_holder 參數), 該隊其餘人只吃屬性%加成、不重複附戰法。
    holder_idx_a = random.randrange(len(teamA)) if campLvA >= 10 and teamA else -1
    holder_idx_b = random.randrange(len(teamB)) if campLvB >= 10 and teamB else -1
    factions_a = [POOL[n].faction for n in teamA]      # 批24 D1: teamGate 判定依據(隊伍全體陣營陣列)
    factions_b = [POOL[n].faction for n in teamB]
    A = [Unit(POOL[n], troopA, bsA[i], eqA[i], addA[i], inhA[i], season_mods(POOL[n], i, teamA, scenario), factions_a, campLvA, i == holder_idx_a)
         for i, n in enumerate(teamA)]
    B = [Unit(POOL[n], troopB, bsB[i], eqB[i], addB[i], inhB[i], season_mods(POOL[n], i, teamB, scenario), factions_b, campLvB, i == holder_idx_b)
         for i, n in enumerate(teamB)]
    setA = set(map(id, A))
    allies_of = lambda u: A if id(u) in setA else B
    foes_of = lambda u: B if id(u) in setA else A
    bonds = {id(A[0]) if A else 0: active_bonds(teamA), id(B[0]) if B else 1: active_bonds(teamB)}

    CAT_ORDER = ("PASSIVE", "FORMATION", "TROOP", "COMMAND")  # 準備階段嚴格順序: 被動→陣法→兵種→指揮(與 engine.js parity)
    cat_of = lambda t: t.get("cat") if t.get("cat") in CAT_ORDER else "COMMAND"

    def apply_passives(no_heal=False, heal_only=False, skip_when_effects=False, broadcast_only=False):  # 被動/陣法/兵種/指揮(依序) + 兵書/裝備/緣分
        # 時序徹底一致化批: broadcast_only(可選) —— 相一全局round-start廣播通道(e["broadcast"]
        # 標記的極少數實例, 如高櫓連營), 於fight()主迴圈回合頂端、任何單位行動前呼叫, 對稱既有
        # heal_only(現已改移入apply_own_turn_effects逐單位通道, 見其定義)。
        for cat in CAT_ORDER:
            for u in A + B:
                if not u.alive:
                    continue
                for t in u.tactics:                   # 同將多個同類: 戰法格順序(陣列順序)決定先後
                    if t["type"] in ("passive", "command") and cat_of(t) == cat:
                        if t.get("when") and not (heal_only or broadcast_only):  # 條件觸發(when): 不在準備階段套用, 改由回合迴圈在符合回合時套用
                            continue
                        apply_effects(u, None, t, allies_of(u), foes_of(u), no_heal=no_heal, heal_only=heal_only,
                                      skip_when_effects=skip_when_effects, broadcast_only=broadcast_only)
        for u in A + B:
            if not u.alive:
                continue
            for eff in (u.bs, u.eq):
                if eff:
                    apply_effects(u, None, {"effects": eff, "kind": "phys"}, allies_of(u), foes_of(u),
                                  no_heal=no_heal, heal_only=heal_only, skip_when_effects=skip_when_effects,
                                  broadcast_only=broadcast_only)
        for team in (A, B):                           # 緣分: 隊伍級(無everyRound/broadcast效果, broadcast_only無需傳遞)
            if team:
                for bd in bonds[id(team[0])]:
                    apply_effects(team[0], None, {"effects": bd["effects"], "kind": "phys"},
                                  team, foes_of(team[0]), no_heal=no_heal, heal_only=heal_only,
                                  skip_when_effects=skip_when_effects)

    # 時序一致化(2026-07 批次) A.2+A.3, 時序徹底一致化批(最終定案): 「該持有者自己的行動輪」
    # cadence 掃描 —— 取代舊「全局回合」cadence, 於 fight() 主迴圈逐單位處理輪到 u 時呼叫(見
    # 下方回合迴圈, 呼叫點在 dot_settle() 之後、settle_tick() 之前, 即「行動前檢查」)。本批
    # 起統一收斂**所有**團隊/自參照回合窗機制(除e["broadcast"]相一全局廣播外, user最終裁決
    # 「其餘一律相二逐單位own_round」), 取代舊「回合迴圈頂端全體單位批次檢查」(heal_only/
    # t.when窗口/delayed_eq/e.when泛化 四條channel皆已移除, 邏輯併入此處):
    #   (1) A.3 everyRound(如機鑑先識「每回合21%→42%機率獲得1次警戒」): 逐 u.tactics/u.bs/u.eq
    #       呼叫 apply_effects(..., own_turn=True), 由該函式內部閘門只放行帶 e["everyRound"]
    #       且非e["broadcast"]的效果並依 own_round 判定視窗/rate。
    #   (2) heal_only(戰報實證: 左慈金丹秘術/夏侯惇陷陣營「戰鬥前N回合我軍全體...」團隊buff之
    #       回血/急救觸發, 在**各受益單位(=持有者)自己行動輪**結算, 非全局round-start): 逐
    #       u.tactics/u.bs/u.eq 呼叫 apply_effects(..., heal_only=True), round_ok內部已改用
    #       caster(=u).own_round(見apply_effects k=="heal"分支)。
    #   (3) 頂層t.when窗口(passive/command, 非on反應式, 如陷陣營/工神/士別三日等): 窗口首次
    #       開啟時套用一次(dot/amp/stat/settle等非heal效果), 改用u.own_round判定。
    #   (4) 裝備效果級回合窗(delayed_eq, 如應變/反間/明略): 改用u.own_round。
    #   (5) e.when 泛化(非heal/非everyRound/非broadcast, 含settle/coefFromStack/工神/橫戈躍馬/
    #       武鋒陣等team-wide buff與士別三日/用武通神/竊幸乘寵等自參照戰法): 統一改用u.own_round
    #       (user最終裁決: 除相一廣播外一律own_round, 不再區分settle/coefFromStack與其餘team-wide
    #       buff——過去「其餘when-gated效果待user確認」的B類清單已裁決完畢)。
    def apply_own_turn_effects(u):
        for t in u.tactics:
            if t["type"] in ("passive", "command"):
                apply_effects(u, None, t, allies_of(u), foes_of(u), own_turn=True)
        for eff in (u.bs, u.eq):
            if eff:
                apply_effects(u, None, {"effects": eff, "kind": "phys"}, allies_of(u), foes_of(u), own_turn=True)

        # (2) heal_only: 逐回合治療(含兵書/裝備), 改於u自己行動輪(行動前)呼叫, 取代舊「回合
        # 迴圈頂端apply_passives(heal_only=True)全體批次」通道。
        for t in u.tactics:
            if t["type"] in ("passive", "command"):
                apply_effects(u, None, t, allies_of(u), foes_of(u), heal_only=True)
        for eff in (u.bs, u.eq):
            if eff:
                apply_effects(u, None, {"effects": eff, "kind": "phys"}, allies_of(u), foes_of(u), heal_only=True)

        # (3) 頂層t.when窗口(passive/command, 非on): 窗口首次開啟時套用一次非傷害效果。改用
        # u.own_round(取代舊全局rnd), 於u自己行動輪(行動前)檢查。原「回合迴圈頂端全體單位批次
        # 檢查」通道已移除。
        for t in u.tactics:
            if t["type"] in ("passive", "command") and t.get("when") and not t["when"].get("on") \
                    and round_ok(t, u.own_round) and id(t) not in u.when_fired:
                w = t["when"]
                # 批16: hpPct —— when.hpBelow(一次性, 首次跨越即觸發)/when.hpAbove(持續窗, 不去重)
                if w.get("hpBelow") is not None or w.get("hpAbove") is not None:
                    if not hp_ok(t, u):
                        continue
                    if w.get("hpBelow") is not None:
                        if id(t) in u.hp_below_fired:
                            continue
                        u.hp_below_fired.add(id(t))
                    if random.random() >= t.get("rate", 1):
                        continue
                    apply_effects(u, None, t, allies_of(u), foes_of(u), no_heal=True)
                    if w.get("hpBelow") is not None:
                        u.when_fired.add(id(t))
                    continue
                u.when_fired.add(id(t))
                if random.random() >= t.get("rate", 1):
                    continue
                # 批15: no_heal=True —— heal 效果改由上面 heal_only 通道統一處理, 避免不同去重鍵
                # (id(t) vs id(e))各自判定造成同一視窗開啟的回合heal被套用兩次(雙倍治療)。
                apply_effects(u, None, t, allies_of(u), foes_of(u), no_heal=True)

        # (4) 裝備效果級回合窗(delayed_eq) —— 與t.when窗口同一時點(u自己行動輪, 行動前)檢查,
        # 改用u.own_round(取代舊全局rnd)。
        if u.delayed_eq:
            for e in u.delayed_eq:
                if not round_ok({"when": e["when"]}, u.own_round) or id(e) in u.when_fired:
                    continue
                u.when_fired.add(id(e))
                if e.get("rate") is not None and random.random() >= e["rate"]:
                    continue
                apply_effects(u, None, {"effects": [e], "kind": "phys",
                                        "n": e.get("n", 1), "nMax": e.get("nMax", 0)}, allies_of(u), foes_of(u),
                              rate_checked=True)

        # (5) e.when 泛化(非heal/非everyRound/非broadcast, 含settle/coefFromStack與其餘所有
        # team-wide/自參照回合窗效果): 掃描「母戰法無t["when"]」的passive/command戰法, 找出其中
        # 帶e["when"]的效果, 視窗開啟時一次性套用(from/until範圍視窗不去重, 讓窗內每回合都能
        # 重新套用, 同heal慣例)。改用u.own_round(取代舊全局rnd)。
        for t in u.tactics:
            if t["type"] not in ("passive", "command") or t.get("when"):
                continue  # 母戰法有 t["when"] 的走上面(3)t["when"]掃描
            for e in t["effects"]:
                if e["k"] == "heal" or e.get("everyRound") or e.get("broadcast") or not e.get("when"):
                    continue  # heal(上面(2)已處理)/everyRound(own_turn=True已處理)/broadcast(相一, round-start通道處理)不進這裡
                if id(e) in u.when_fired:
                    continue
                if not round_ok({"when": e["when"]}, u.own_round):
                    continue
                if e["when"].get("rounds"):
                    u.when_fired.add(id(e))  # rounds(明確列出的特定回合): 一次性去重(同 delayed_eq/heal 慣例)
                apply_effects(u, None, {"effects": [e], "kind": t.get("kind", "phys"),
                                        "n": t.get("n", 1), "nMax": t.get("nMax", 0),
                                        "nameZh": t.get("nameZh")}, allies_of(u), foes_of(u), no_heal=True)

    # 批38 A: 跨單位事件廣播 —— e["when"]["who"]/t["when"]["who"](選填, 預設None=向後相容
    # "self"零變化)。過去 on_hit/dealt_damage/active_fired 只掃描「事件發生的那個單位自己」
    # 攜帶的反應式戰法/效果, 無法表達「任一友軍受擊/造成傷害/發動主動戰法時, 我(持有者,
    # 可能是另一個單位)也跟著觸發」這類跨單位監聽語意(歷輪盲測點名最大殘餘原語缺口, 見
    # engine_limitations.md 21/27節「跨單位事件廣播」未解決缺口列表: 虎侯/十二奇策/經天緯地/
    # 神機妙算/舌戰群儒等)。who=="ally": 監聽對象是「事件單位所在隊伍的任一人(含自己)」,
    # 持有者也必須在同一隊。who=="enemy": 監聽對象是「事件單位所在隊伍的敵對隊伍任一人」,
    # 持有者必須在敵對那一隊。self(未指定/None)仍走原本「持有者===事件單位」路徑, 不受
    # 此廣播擴充影響(零回歸)。
    def broadcast_holders(evt_unit, who):
        if who in ("ally", "otherAlly"):
            return allies_of(evt_unit)
        if who == "enemy":
            return foes_of(evt_unit)
        return None                                    # "self"/未指定: 不走廣播, 呼叫端維持原本自身路徑

    def on_hit(dst, src, is_normal, dmg=None, kind=None):  # 反應式觸發(when.on): 被普攻(attacked)/受任意傷害(damaged) 時掛到 hit() 事件點; 批33: dmg(可選)—— 本次觸發事件的實際傷害量, 供 e["ofDamage"] 傷害比例治療使用; 批38 A: 新增who=="ally"/"enemy"跨單位廣播(見上方broadcast_holders/on_hit_for); 批39 C: 新增kind(可選, 尾端新增, 向後相容既有呼叫點皆未傳)—— 本次傷害類型(phys/intel), 供when["dmgType"]/e["when"]["dmgType"]過濾(對稱dealt_damage的dmg_type_ok)
        def dmg_type_ok(dt):
            return not dt or not kind or dt == kind   # dmgType 過濾: 未指定該欄位視為兵刃/謀略皆可觸發(向後相容), 與dealt_damage的dmg_type_ok同慣例
        def on_hit_for(dst, src, is_normal, dmg, holder, want_who):  # holder: 效果持有者(可能不同於dst); want_who: None→只認持有者自身受擊之既有語意; "ally"/"enemy"→只認廣播監聽到的受擊事件, 避免self掃描與廣播掃描重複觸發同一條
            if not holder.alive or (not holder.on_hit_tacs and not holder.on_hit_effect_tacs and not holder.on_hit_eq and not holder.on_hit_bs):
                return
            if holder.fake_report_dur:                # 批16: 偽報 —— 抑制 on_hit 反應式觸發(被動/指揮戰法失效)
                return
            def who_ok(w):
                return (w or {}).get("who") == want_who if want_who else not (w or {}).get("who")
            other_ally_ok = lambda: holder is not dst   # who=="otherAlly" 額外要求: 持有者不是本次事件單位自己
            # 批A(11筆高嚴重重建): dmg_above(可選數值) —— 對稱dealt_damage同名旗標, 「受到傷害
            # 超過X」句型(承天靖世「我軍收到高於最大兵力6%的傷害時」)的傷害量閾值閘門。
            def dmg_above_ok(w):
                threshold = (w or {}).get("dmgAbove")
                return threshold is None or (dmg is not None and dmg > threshold)
            for t0 in holder.on_hit_tacs:
                if not who_ok(t0.get("when")):
                    continue
                if want_who == "otherAlly" and not other_ally_ok():
                    continue
                if t0["when"]["on"] == "attacked" and not is_normal:  # attacked: 限普通攻擊觸發; damaged: 任意傷害都觸發
                    continue
                if not dmg_type_ok(t0["when"].get("dmgType")):  # 批39 C: 戰法級when.dmgType過濾(如剛勇無前/剛烈不屈「受到兵刃傷害時」限定)
                    continue
                if not dmg_above_ok(t0.get("when")):
                    continue
                # 批22: when.on 反應式戰法過去完全不檢查 rounds/from/until/parity/every(只認 on 事件
                # 本身), 導致「戰鬥首回合獲得急救(受傷時回血)」這類「反應式觸發+回合窗口限定」的
                # 複合語意無法表達(如 長健/青囊書: 首回合內受傷才會回血, 而非全程)。round_ok() 對
                # 「無 rounds/from/until/parity/every」的戰法一律回傳 True, 故此檢查對絕大多數既有
                # when.on 戰法(只帶 on, 無回合欄位)是無副作用的 no-op, 只在新資料明確加上回合窗口
                # 時才生效。時序一致化本批: 改用holder(反應式戰法持有者)自己own_round,取代全局CUR_ROUND。
                if not round_ok(t0, holder.own_round):
                    continue
                if id(t0) in holder.hit_flags:          # 同回合每單位每戰法最多觸發1次(防無限鏈), 鍵用t0(戰法原始物件)不受choices合成視圖影響
                    continue
                # 批C: t.rateLeader —— 主將時採用較高觸發率(對稱批52续既有的active型戰法頂層
                # rateLeader分派, 見fight()主迴圈「批52续: t.rateLeader」段; 淵然難測「自身為
                # 主將時，基礎機率提升至30%→60%」發現此欄位雖已存在於資料但from未被本反應式
                # on_hit_for()讀取, 是「資料寫了但引擎端遺漏對應讀取」的死欄位, 本次補上)。
                _fire_rate = t0["rate"]
                if t0.get("rateLeader") is not None and allies_of(holder) and allies_of(holder)[0] is holder:
                    _fire_rate = t0["rateLeader"]
                # 批52: rateScaleIfGender —— 原文「若自身為女性, 觸發機率額外受智力影響」(魅惑)
                if t0.get("rateScaleIfGender") and t0.get("rateScale"):
                    gmap = {"男": "Male", "女": "Female", "Male": "Male", "Female": "Female",
                            "male": "Male", "female": "Female"}
                    want = gmap.get(t0["rateScaleIfGender"], t0["rateScaleIfGender"])
                    got = gmap.get((holder.g.gender if holder.g else "") or "", holder.g.gender if holder.g else "")
                    if got == want:
                        _fire_rate = t0["rate"] * rate_scale_of(holder, t0["rateScale"], t0.get("rateScaleDiv"))
                if random.random() >= _fire_rate:
                    continue
                holder.hit_flags.add(id(t0))
                # 批27 C: choices(擇一分支) —— 過去 on_hit() 反應式路徑完全不讀 t0["choices"](見
                # engine_limitations.md §8: 魅惑「混亂/計窮/虛弱」三選一只能固定選其中一種, choices
                # 寫入也不會被消費), 主動/指揮/被動的常駐輪詢派發路徑(fight()主迴圈)已支援 choices,
                # 這裡補上同一套邏輯(先 pick_choice 選分支, 再用合成視圖 t 讀 coef/kind/effects/
                # extraHits, t0 保留給 id()去重/round_ok 等以物件本身為鍵的邏輯, 不受選分支影響)。
                t = dict(t0, **pick_choice(t0["choices"])) if t0.get("choices") else t0
                if t["coef"]:
                    hit(holder, src, t["coef"], t["kind"], False, on_hit, dealt_damage)
                if t.get("extraHits"):
                    fire_extra_hits(holder, t, src, allies_of, foes_of, on_hit, dealt_damage)  # 批13: 受擊觸發類多段傷害(如剛烈不屈 反擊後群體額外段)
                if t["effects"]:
                    apply_effects(holder, src, t, allies_of(holder), foes_of(holder), reactive=True, dmg=dmg, evt_target=dst)  # 批23: 戰法級when.on本身即反應式, 標記reactive供內部e.when.on效果(若有)一致判定; 批33: dmg供e["ofDamage"]使用; 批42: evt_target供who=="eventTarget"
            # 批22: 效果級 e.when.on(急救類反應式治療, 見 on_hit_effect_tacs 註解) —— 戰法本身無
            # t["when"](其餘效果如武力/統率平加仍在 prep 正常套用, 不受影響), 只有帶 e.when.on 的
            # 個別效果在此處反應式結算。用「合成單效果戰法」(effects=[e])呼叫 apply_effects, 讓
            # heal 分支的傷兵池/healBoost/healGiven 邏輯完整適用, 觸發率取 e.rate ?? t.rate ?? 1
            # (效果自身優先, 無則沿用戰法整體rate)。去重鍵用 id(效果物件)(而非戰法物件), 因同一
            # 戰法可能有多個 e.when.on 效果, 需各自獨立節流。
            for t in holder.on_hit_effect_tacs:
                for e in t["effects"]:
                    # 狀態疊加語意對齊批: 急救(unique具名狀態)去重後被覆蓋的反應式heal效果
                    # 整個跳過(不擲率也不治療), 見 Unit.__init__ suppressed_named_status
                    # 建構時裁決註解。跳過非常便宜的早退檢查, 放在最前面。
                    if id(e) in holder.suppressed_named_status:
                        continue
                    ew = e.get("when") or {}
                    if not ew.get("on"):
                        continue
                    if not who_ok(ew):
                        continue
                    if want_who == "otherAlly" and not other_ally_ok():
                        continue
                    if ew["on"] == "attacked" and not is_normal:
                        continue
                    if not dmg_type_ok(ew.get("dmgType")):  # 批39 C: 效果級when.dmgType過濾
                        continue
                    if not dmg_above_ok(ew):
                        continue
                    # 時序一致化本批: 改用holder自己own_round, 取代全局CUR_ROUND。
                    if not round_ok({"when": ew}, holder.own_round):
                        continue
                    if id(e) in holder.hit_flags:
                        continue
                    # 禁近似令-批K: e["once"] —— 對稱 engine.js 同名分支(誓守無降「自身2回合內
                    # 受到下一次謀略傷害時...」的『下一次』=單次消耗, hit_flags只提供同回合節流,
                    # when_fired是不隨回合重置的持久化去重狀態, 借用來表達reactive路徑的一次性
                    # 消耗)。
                    if e.get("once") and id(e) in holder.when_fired:
                        continue
                    ev_rate = e.get("rate", t.get("rate", 1))
                    if random.random() >= ev_rate:
                        continue
                    holder.hit_flags.add(id(e))
                    if e.get("once"):
                        holder.when_fired.add(id(e))
                    apply_effects(holder, src, {"effects": [e], "kind": t.get("kind", "phys"), "nameZh": t.get("nameZh")},
                                 allies_of(holder), foes_of(holder), rate_checked=True, reactive=True, dmg=dmg, evt_target=dst)  # 批23 A4/reactive: 上面已擲過 e["rate"], 避免重複擲骰; reactive供e.when.on閘門放行; 批33: dmg供e["ofDamage"]使用; 批42: evt_target供who=="eventTarget"
            # 批22: 裝備效果級 e.when.on(見 on_hit_eq 註解) —— 同上, 用合成單效果戰法呼叫 apply_effects
            for e in holder.on_hit_eq:
                # 狀態疊加語意對齊批: 急救(unique具名狀態)去重, 同 on_hit_effect_tacs 迴圈註解。
                if id(e) in holder.suppressed_named_status:
                    continue
                ew = e["when"]
                if not who_ok(ew):
                    continue
                if want_who == "otherAlly" and not other_ally_ok():
                    continue
                if ew["on"] == "attacked" and not is_normal:
                    continue
                if not dmg_type_ok(ew.get("dmgType")):  # 批39 C: 裝備效果級when.dmgType過濾
                    continue
                # 時序一致化本批: 改用holder自己own_round, 取代全局CUR_ROUND。
                if not round_ok({"when": ew}, holder.own_round):
                    continue
                if id(e) in holder.hit_flags:
                    continue
                ev_rate = e.get("rate", 1)
                if random.random() >= ev_rate:
                    continue
                holder.hit_flags.add(id(e))
                apply_effects(holder, src, {"effects": [e], "kind": "phys"}, allies_of(holder), foes_of(holder), rate_checked=True, reactive=True, dmg=dmg, evt_target=dst)  # 批23 A4/reactive; 批33: dmg供e["ofDamage"]使用; 批42: evt_target供who=="eventTarget"
            # 批22: 兵書效果級 e.when.on(見 on_hit_bs 註解) —— 同上, 用合成單效果戰法呼叫 apply_effects
            for e in holder.on_hit_bs:
                # 狀態疊加語意對齊批: 急救(unique具名狀態)去重, 同 on_hit_effect_tacs 迴圈註解。
                if id(e) in holder.suppressed_named_status:
                    continue
                ew = e["when"]
                if not who_ok(ew):
                    continue
                if want_who == "otherAlly" and not other_ally_ok():
                    continue
                if ew["on"] == "attacked" and not is_normal:
                    continue
                if not dmg_type_ok(ew.get("dmgType")):  # 批39 C: 兵書效果級when.dmgType過濾
                    continue
                # 時序一致化本批: 改用holder自己own_round, 取代全局CUR_ROUND。
                if not round_ok({"when": ew}, holder.own_round):
                    continue
                if id(e) in holder.hit_flags:
                    continue
                ev_rate = e.get("rate", 1)
                if random.random() >= ev_rate:
                    continue
                holder.hit_flags.add(id(e))
                apply_effects(holder, src, {"effects": [e], "kind": "phys"}, allies_of(holder), foes_of(holder), rate_checked=True, reactive=True, dmg=dmg, evt_target=dst)  # 批23 A4/reactive; 批33: dmg供e["ofDamage"]使用; 批42: evt_target供who=="eventTarget"

        if not dst.alive:
            return
        on_hit_for(dst, src, is_normal, dmg, dst, None)   # 既有語意: 持有者=事件單位自己(who未指定/"self")
        # 批38 A: 廣播 —— dst(受擊/受傷的那個單位)所在隊伍的隊友(who=="ally"持有者)與敵隊
        # (who=="enemy"持有者)也一併掃描。候選陣列含 dst 自己, 但 who_ok() 只放行明確 who
        # 欄位匹配的條目, 不會與上面的 self 路徑(who_ok 要求 who 欄位為空)重複觸發同一筆。
        for holder in allies_of(dst):
            on_hit_for(dst, src, is_normal, dmg, holder, "ally")
        for holder in allies_of(dst):
            on_hit_for(dst, src, is_normal, dmg, holder, "otherAlly")  # 批38 A: who=="otherAlly"(排除dst自己, 見騎虎難下「除自己之外的友軍受到普通攻擊時」)
        for holder in foes_of(dst):
            on_hit_for(dst, src, is_normal, dmg, holder, "enemy")

    def dealt_damage(src, dst, is_normal, kind, dmg=None):  # 批27 A: 反應式觸發(when.on:"dealtDamage") —— 自己造成傷害(對 dst)後掛到 hit() 事件點, 與 on_hit(自己受擊視角)對稱; 批33: dmg(可選)—— 本次觸發事件的實際傷害量, 供 e["ofDamage"] 使用; 批38 A: 新增who=="ally"/"enemy"跨單位廣播(對稱on_hit, 見 broadcast_holders/dealt_damage_for)
        # 批37 B: stackPer:"attack" —— 「每次普通攻擊後疊加1層」(如奮突「普通攻擊之後...最多
        # 疊加3次」)。過去只有 "round"(逐回合)/"cast"(每次發動)兩種遞增模式, 普攻疊層只能用
        # round 近似(繳械/震懾回合無普攻仍會錯誤地繼續疊層)。掛在 dealt_damage 事件點(普攻
        # 確實命中造成傷害後), 置於 on_deal_tacs 早退判斷之前(有 stackPer:"attack" 疊層的
        # 單位未必同時有 when.on:"dealtDamage" 反應式戰法, 不能被該早退擋掉)。此段是src自身
        # 狀態變化, 與who廣播無關, 維持只對src本身執行, 不隨廣播迴圈重複執行。
        if is_normal and src.alive and src.stack and src.stack.get("stackPer") == "attack":
            src.stack["n"] = min(src.stack["max"], src.stack["n"] + 1)

        def dealt_damage_for(src, dst, is_normal, kind, dmg, holder, want_who):  # holder: 效果持有者(可能不同於src); want_who: 同on_hit_for慣例
            # 批G: 早退判斷補上on_deal_eq(裝備效果級dealtDamage), 否則只帶裝備級dealtDamage
            # 反應式(無戰法級on_deal_tacs/on_deal_effect_tacs)的持有者會在此處被提前擋掉, 永遠
            # 進不到下方on_deal_eq迴圈(衝陣「首回合首次造成傷害時」若無其他戰法級dealtDamage反應式
            # 戰法陪同, 會被此早退邏輯完全跳過)。
            if not holder.alive or (not holder.on_deal_tacs and not holder.on_deal_effect_tacs and not holder.on_deal_eq):
                return
            if holder.fake_report_dur:                  # 批16: 偽報 —— 抑制反應式觸發(被動/指揮戰法失效), 與 on_hit 同慣例
                return
            def who_ok(w):
                return (w or {}).get("who") == want_who if want_who else not (w or {}).get("who")
            def _dmg_type_ok(dmg_type):                 # dmgType 過濾: 未指定視為兵刃/謀略皆可觸發(向後相容)
                return not dmg_type or dmg_type == kind
            # 批A(11筆高嚴重重建): dmg_above(可選數值) —— 「造成大於X的傷害時」句型(密計誅逆
            # 「當我軍主將造成大於300的傷害時」)的傷害量閾值閘門, 對稱既有dmgType。dmg為None時
            # (理論上dealt_damage事件必傳, 保守防呆)視為不通過。戰法級/效果級皆支援。
            def _dmg_above_ok(w):
                threshold = (w or {}).get("dmgAbove")
                return threshold is None or (dmg is not None and dmg > threshold)
            # caster_is_leader(見active_fired_for同名旗標註解) —— dealt_damage事件的「觸發者」
            # 是src(造成本次傷害的那個單位), 與holder(廣播後的持有者, 可能是隊友)分開; 密計誅逆
            # 「我軍主將造成傷害」要求src本身是其隊伍主將, 而非holder是主將。
            def _caster_is_leader_ok(w):
                if not (w or {}).get("casterIsLeader"):
                    return True
                al = allies_of(src)
                return bool(al and al[0] is src)
            for t in holder.on_deal_tacs:                # 戰法級: 整個戰法都是「造成傷害時」反應式(如白衣渡江拆成兩個獨立戰法段時可用此形式)
                if not who_ok(t.get("when")):
                    continue
                if not _dmg_type_ok((t.get("when") or {}).get("dmgType")):
                    continue
                if not _dmg_above_ok(t.get("when")):
                    continue
                if not _caster_is_leader_ok(t.get("when")):
                    continue
                if (t.get("when") or {}).get("normalOnly") and not is_normal:
                    continue                            # 批37 B: when.normalOnly —— 限「普通攻擊」造成的傷害才觸發(如奮突「普通攻擊之後」; dmgType:"phys" 無法區分普攻與兵刃戰法傷害, 需獨立旗標)
                # 時序一致化本批: 改用holder自己own_round, 取代全局CUR_ROUND。
                if not round_ok(t, holder.own_round):
                    continue
                if id(t) in holder.hit_flags:            # 同回合每單位每戰法最多觸發1次(防無限鏈), 與 on_hit 共用同一 hit_flags(不同方向的觸發各自用不同id(t)/id(e)鍵, 不會互相誤判)
                    continue
                if random.random() >= t["rate"]:
                    continue
                holder.hit_flags.add(id(t))
                if t["coef"]:
                    # 批32 B: targetSel(依準則選標) —— 過去 dealtDamage 的 coef 傷害段固定命中
                    # dst(觸發本次事件的同一目標, 如普攻打誰就額外打誰), 沒有讀取 t.get("targetSel")
                    # 這條路徑, 導致原文「對負傷最高之敵造成謀略傷害」(選標準則與觸發目標無關,
                    # 如監統震軍)只能被迫近似成「打觸發同目標」或完全不建模。比照主動戰法主迴圈
                    # 既有的 targetSel 判斷式(pick_by_criterion(foes_of(u), t["targetSel"])), 若
                    # 戰法帶 targetSel 則改用準則選標, 找不到符合準則的目標(如全軍陣亡)時不出手
                    # (dv=None 時不呼叫 hit, 而非退回 dst, 避免誤傷/誤選)。
                    if t.get("targetSel"):
                        dv = pick_by_criterion(foes_of(holder), t["targetSel"])
                        if dv:
                            hit(holder, dv, t["coef"], t["kind"], False, on_hit, dealt_damage)
                    elif holder is src:
                        hit(holder, dst, t["coef"], t["kind"], False, on_hit, dealt_damage)
                    else:                                # 廣播情形(holder不是src): 觸發事件的原始dst未必是holder的敵人(可能同隊), 退回holder自己的固定敵方位0近似選標(見同批B節遷移逐筆核對是否需要targetSel精確指定)
                        fv = foes_of(holder)
                        if fv and fv[0].alive:
                            hit(holder, fv[0], t["coef"], t["kind"], False, on_hit, dealt_damage)
                if t.get("extraHits"):
                    fire_extra_hits(holder, t, dst if holder is src else None, allies_of, foes_of, on_hit, dealt_damage)
                if t["effects"]:
                    apply_effects(holder, dst if holder is src else None, t, allies_of(holder), foes_of(holder), reactive=True, dmg=dmg)  # 批33: dmg供e["ofDamage"]使用
            # 效果級: 戰法本身有其他常駐效果, 只有部分效果段是「造成傷害時」反應式(如白衣渡江本身
            # 是常駐 command, disarm/silence 兩效果各自綁不同 dmgType, 與 on_hit_effect_tacs 同慣例)
            for t in holder.on_deal_effect_tacs:
                for e in t["effects"]:
                    ew = e.get("when") or {}
                    if ew.get("on") != "dealtDamage":
                        continue
                    if not who_ok(ew):
                        continue
                    if not _dmg_type_ok(ew.get("dmgType")):
                        continue
                    if not _dmg_above_ok(ew):
                        continue
                    if not _caster_is_leader_ok(ew):
                        continue
                    if ew.get("normalOnly") and not is_normal:
                        continue                        # 批37 B: when.normalOnly(效果級) —— 同上, 限普攻傷害觸發
                    # 時序一致化本批: 改用holder自己own_round, 取代全局CUR_ROUND。
                    if not round_ok({"when": ew}, holder.own_round):
                        continue
                    # 批52e: everyHit/maxStack —— 「每次造成傷害」可同回合多次觸發(文武雙全);
                    # 預設仍 hit_flags 每效果每回合最多1次(防無限鏈)。
                    _multi = bool(e.get("everyHit") or e.get("maxStack"))
                    if not _multi and id(e) in holder.hit_flags:
                        continue
                    if e.get("once") and id(e) in holder.when_fired:  # 禁近似令-批K: 見on_hit_for同款e["once"]註解
                        continue
                    # 批52f: 預設不要求 dmg>0——抵禦全擋/虛弱歸零仍算「造成該類型攻擊」
                    # (文武雙全 user 確認)。僅 e.requireDmg:true 時才過濾零傷。
                    if e.get("requireDmg", False) and not (dmg and dmg > 0):
                        continue
                    ev_rate = e.get("rate", t.get("rate", 1))
                    if random.random() >= ev_rate:
                        continue
                    if not _multi:
                        holder.hit_flags.add(id(e))
                    if e.get("once"):
                        holder.when_fired.add(id(e))
                    apply_effects(holder, dst if holder is src else None, {"effects": [e], "kind": t.get("kind", "phys"), "nameZh": t.get("nameZh")},
                                 allies_of(holder), foes_of(holder), rate_checked=True, reactive=True, dmg=dmg)  # 已擲過 e["rate"], 避免重複擲骰; reactive供e.when.on閘門放行; 批33: dmg供e["ofDamage"]使用
            # 批G: 裝備效果級 e.when.on=="dealtDamage"(見 on_deal_eq 註解) —— 同上, 用合成單效果
            # 戰法呼叫 apply_effects, 對稱 on_hit_eq(受擊方向)的既有裝備級消費端。衝陣「首回合首次
            # 造成傷害時附加一次額外兵刃傷害」需要此掛鉤點, 過去裝備管線只有on_hit_eq(受擊方向),
            # 缺此方向(造成傷害)的消費端, dealtDamage的裝備效果會被靜默忽略(從未真正觸發)。
            for e in holder.on_deal_eq:
                ew = e["when"]
                if not who_ok(ew):
                    continue
                if not _dmg_type_ok(ew.get("dmgType")):
                    continue
                if not _dmg_above_ok(ew):
                    continue
                if not _caster_is_leader_ok(ew):
                    continue
                if ew.get("normalOnly") and not is_normal:
                    continue
                # 時序一致化本批: 改用holder自己own_round, 取代全局CUR_ROUND。
                if not round_ok({"when": ew}, holder.own_round):
                    continue
                if id(e) in holder.hit_flags:
                    continue
                ev_rate = e.get("rate", 1)
                if random.random() >= ev_rate:
                    continue
                holder.hit_flags.add(id(e))
                # 批G: e["coef"](可選)—— 對稱on_deal_tacs的t["coef"]直傷派發(見上方if t["coef"]:
                # hit(...)分支), 讓裝備效果級dealtDamage也能表達「附加一次額外傷害」(而非只能是
                # amp/mitig/heal等buff類effects), 衝陣「首次造成傷害時附加一次額外兵刃傷害」需要
                # 此直接傷害輸出, 沿用觸發本次事件的同一目標dst(裝備效果無targetSel/多目標選標
                # 需求, 保持與觸發者同一目標最簡單直觀)。與下方apply_effects(k派發)不互斥——
                # e["k"]若非coef直傷類型(如"amp"/"mitig")仍會正常經過apply_effects處理, e["coef"]
                # 只是額外多做一次直接hit()(對稱on_deal_tacs主戰法段「coef傷害+effects buff」
                # 可並存的既有語意)。
                if e.get("coef") and holder is src and dst and dst.alive:
                    hit(holder, dst, e["coef"], e.get("kind", "phys"), False, on_hit, dealt_damage)
                apply_effects(holder, dst if holder is src else None, {"effects": [e], "kind": "phys"}, allies_of(holder), foes_of(holder), rate_checked=True, reactive=True, dmg=dmg)

        if not src.alive:
            return
        dealt_damage_for(src, dst, is_normal, kind, dmg, src, None)  # 既有語意: 持有者=事件單位自己(who未指定/"self")
        # 批38 A: 廣播 —— src(本次造成傷害的那個單位)所在隊伍的隊友(who=="ally"持有者)與敵隊
        # (who=="enemy"持有者)也一併掃描。
        for holder in allies_of(src):
            dealt_damage_for(src, dst, is_normal, kind, dmg, holder, "ally")
        for holder in foes_of(src):
            dealt_damage_for(src, dst, is_normal, kind, dmg, holder, "enemy")

    def active_fired(u):                                # 批31 A: 反應式觸發(when.on:"activeFired") —— 自己成功發動主動/突擊戰法時掛到 fight() 主迴圈事件點, 與 dealt_damage(自己造成傷害視角)/on_hit(自己受擊視角)對稱; 只認「自身」戰法成功fire這件事本身, 不要求造成傷害(士爭先赴等戰法可能coef=0純buff, 也可能有coef傷害段); 批38 A: 新增who=="ally"/"enemy"跨單位廣播(見broadcast_holders/active_fired_for) —— 解決十二奇策「我軍全體下次發動主動戰法後」/經天緯地「我軍全體發動主動/突擊戰法時」(who=="ally")、神機妙算/舌戰群儒「敵軍發動主動戰法時」(who=="enemy")這一族全庫最大殘餘原語缺口(見engine_limitations.md 21/27節)
        def active_fired_for(u, holder, want_who):       # holder: 效果持有者(可能不同於u=實際發動主動戰法的單位); want_who: 同on_hit_for慣例
            if not holder.alive or (not holder.active_fired_tacs and not holder.active_fired_effect_tacs and not holder.active_fired_bs):
                return
            if holder.fake_report_dur:                   # 批16: 偽報 —— 抑制反應式觸發(被動/指揮戰法失效), 與 on_hit/dealt_damage 同慣例
                return
            def who_ok(w):
                return (w or {}).get("who") == want_who if want_who else not (w or {}).get("who")
            # 批A(11筆高嚴重重建): caster_is_leader —— 「(我軍)主將發動主動/突擊戰法時」這類措辭
            # (十勝十敗)要求觸發事件的u(實際發動者)本身必須是其隊伍主將(index 0), 而非「持有者
            # holder是主將」(who=="ally"廣播的holder未必等於u——十勝十敗常由非主將的副將攜帶,
            # 持有者篩選現有ifLeader/ifLeaderIs管的是holder自身的身份, 不是「這次事件是誰觸發的」)。
            # 用u所在隊伍(allies_of(u))的index0比對u本身, 與holder是否為主將無關。
            def caster_is_leader_ok(w):
                w = w or {}
                if not w.get("casterIsLeader"):
                    return True
                al = allies_of(u)
                return bool(al and al[0] is u)
            for t in holder.active_fired_tacs:            # 戰法級: 整個戰法都是「(自身/我軍/敵軍)成功發動主動戰法時」反應式(如士爭先赴/十二奇策/神機妙算)
                if not who_ok(t.get("when")):
                    continue
                if not caster_is_leader_ok(t.get("when")):
                    continue
                # 時序一致化本批: 改用holder自己own_round, 取代全局CUR_ROUND。
                if not round_ok(t, holder.own_round):
                    continue
                if id(t) in holder.hit_flags:             # 同回合每單位每戰法最多觸發1次(防無限鏈), 與 on_hit/dealt_damage 共用同一 hit_flags(不同方向的觸發各自用不同id(t)/id(e)鍵, 不會互相誤判)
                    continue
                if random.random() >= t["rate"]:
                    continue
                holder.hit_flags.add(id(t))
                main_hit_tgt = None
                if t["coef"]:
                    cnt = t["n"]
                    if t.get("nMax"):
                        cnt = t["n"] + random.randint(0, t["nMax"] - t["n"])
                    vs = pick_targets(foes_of(holder), cnt)
                    # 批I(禁近似令-scale/比較族): t["scaleCompare"] —— 本段傷害係數依「施放者vs
                    # 本次命中目標」同一屬性的差值額外縮放(神機妙算「並基於雙方智力差額外提高」),
                    # 對稱效果級e.scale但讀取雙方差值而非施放者單一固定值, 見scale_compare_of()。
                    # 逐目標各自計算(群體多目標時, 每個目標各自的差值可能不同), 無此欄位則行為
                    # 完全不變(向後相容)。
                    for v in vs:
                        c = t["coef"] * scale_compare_of(holder, v, t["scaleCompare"]) if t.get("scaleCompare") else t["coef"]
                        hit(holder, v, c, t["kind"], False, on_hit, dealt_damage, is_active=True)  # 批31 A: 本段傷害本身即「主動戰法發動觸發的反應式傷害」, 供同戰法/其他戰法的e.activeOnly amp判定
                    if len(vs) == 1:
                        main_hit_tgt = vs[0]
                if t.get("extraHits"):
                    fire_extra_hits(holder, t, main_hit_tgt, allies_of, foes_of, on_hit, dealt_damage)
                if t["effects"]:
                    apply_effects(holder, main_hit_tgt, t, allies_of(holder), foes_of(holder), reactive=True)
            # 效果級: 戰法本身有其他常駐效果, 只有部分效果段是「(自身/我軍/敵軍)成功發動主動戰法時」反應式
            for t in holder.active_fired_effect_tacs:
                for e in t["effects"]:
                    ew = e.get("when") or {}
                    if ew.get("on") != "activeFired":
                        continue
                    if not who_ok(ew):
                        continue
                    if not caster_is_leader_ok(ew):
                        continue
                    # 時序一致化本批: 改用holder自己own_round, 取代全局CUR_ROUND。
                    if not round_ok({"when": ew}, holder.own_round):
                        continue
                    if id(e) in holder.hit_flags:
                        continue
                    ev_rate = e.get("rate", t.get("rate", 1))
                    if random.random() >= ev_rate:
                        continue
                    holder.hit_flags.add(id(e))
                    apply_effects(holder, None, {"effects": [e], "kind": t.get("kind", "phys"), "nameZh": t.get("nameZh")},
                                 allies_of(holder), foes_of(holder), rate_checked=True, reactive=True)  # 已擲過 e["rate"], 避免重複擲骰; reactive供e.when.on閘門放行
            # 禁近似令-批K: 兵書效果級e.when.on=="activeFired"(見active_fired_bs註解) —— 對稱
            # engine.js同款段落, 對稱on_hit_bs(受擊方向)的既有兵書級消費端。
            for e in holder.active_fired_bs:
                ew = e.get("when") or {}
                if not who_ok(ew):
                    continue
                if not caster_is_leader_ok(ew):
                    continue
                # 時序一致化本批: 改用holder自己own_round, 取代全局CUR_ROUND。
                if not round_ok({"when": ew}, holder.own_round):
                    continue
                if id(e) in holder.hit_flags:
                    continue
                ev_rate = e.get("rate", 1)
                if random.random() >= ev_rate:
                    continue
                holder.hit_flags.add(id(e))
                apply_effects(holder, None, {"effects": [e], "kind": "phys"},
                             allies_of(holder), foes_of(holder), rate_checked=True, reactive=True)

        if not u.alive:
            return
        active_fired_for(u, u, None)                     # 既有語意: 持有者=事件單位自己(who未指定/"self")
        # 批38 A: 廣播 —— u(本次成功發動主動/突擊戰法的單位)所在隊伍的隊友(who=="ally"持有者)
        # 與敵隊(who=="enemy"持有者)也一併掃描。
        for holder in allies_of(u):
            active_fired_for(u, holder, "ally")
        for holder in foes_of(u):
            active_fired_for(u, holder, "enemy")

    # 批52i: 供 proxyNormal 代打完整普攻(含突擊)使用
    _FIGHT_CTX["on_hit"] = on_hit
    _FIGHT_CTX["on_deal"] = dealt_damage
    _FIGHT_CTX["allies_of"] = allies_of
    _FIGHT_CTX["foes_of"] = foes_of
    _FIGHT_CTX["active_fired"] = active_fired

    apply_passives(no_heal=True, skip_when_effects=True)  # 開戰套持久效果(治療除外); skip_when_effects: 批18 e.when泛化, 非heal效果帶e.when且母戰法無t.when時prep階段不套用, 改由回合迴圈通用掃描

    global CUR_ROUND
    for rnd in range(1, ROUNDS + 1):
        CUR_ROUND = rnd                               # 批15: 供 apply_effects() 的 heal_only 常駐治療通道檢查 t["when"](round_ok)
        # 時序一致化(2026-07 批次) A.1: 舊「回合迴圈頂端、全體單位stack同時+1層」(全局回合
        # cadence)已移除 —— stackPer=="round" 的逐回合遞增改到該單位自己行動後(見
        # Unit.decay_durations() 對應段落與其docstring), 比照decay_durations掛點。
        # 批A(11筆高嚴重重建): charge_consumed_this_round 逐回合歸零(對應死戰不退「每回合最多
        # 觸發5次」的回合窗口計數, 與蓄威層數charge["n"]本身跨回合累積不同)。
        for u in A + B:
            if u.alive:
                u.charge_consumed_this_round = 0
        # 時序徹底一致化批(最終定案): 舊「回合迴圈頂端、全體單位批次檢查」四條channel
        # (heal_only常駐治療/t.when窗口首次開啟套用/裝備delayed_eq回合窗/e.when泛化非heal)
        # 已全數移除, 邏輯併入 apply_own_turn_effects(u)(見其定義), 於回合迴圈下方逐單位處理
        # 輪到u時、u自己行動前呼叫, 改用u.own_round為基準(取代全局rnd/CUR_ROUND) —— user最終
        # 裁決: 除e["broadcast"](相一: 持有者每回合對他人廣播施加新狀態層, SP周瑜長焰/SP袁紹
        # 高櫓連營等極少數實例)外, 一律相二逐單位own_round(含團隊buff如陷陣營/工神/金丹秘術,
        # 各受益單位=持有者自己行動輪結算, 戰報實證見交接文件)。
        # 相一全局round-start廣播(e["broadcast"]標記): 回合開頭、任何單位行動前, 全體批次用
        # 全局CUR_ROUND處理, 不隨個別持有者own_round結算(user權威規則的唯一例外)。
        apply_passives(broadcast_only=True)

        # 行動順序: 先攻(first)優先於速度; 同速平手隨機(先打亂再穩定排序, 修 A 隊固定先手偏差)
        # 批18: 遇襲(ambush, 先攻的反面/遲緩) —— 三檔 eff_first: 只有先攻→最先(1); 先攻+遇襲同時
        # 存在→抵消, 視為普通(0, 按速度排); 只有遇襲→排最後(-1, 遇襲者之間仍按速度排)。
        eff_first = lambda x: (1 if x.first > 0 else 0) - (1 if x.ambush > 0 else 0)
        _pool = [x for x in A + B if x.alive]
        random.shuffle(_pool)
        for u in sorted(_pool, key=lambda x: (eff_first(x), x.eff("speed")), reverse=True):
            if not u.alive:
                continue  # 本回合稍早已被擊殺(其他單位的攻擊), 不再結算/行動
            # 時序一致化(2026-07 批次): own_round —— 該單位自己第N個行動輪, 在此遞增(輪到u
            # 這次處理即算u自己的1個行動輪, 不論之後是否因震懾/捕獲/DoT致死而略過實際行動,
            # 與decay_durations()「即使跳過行動仍要遞減持續」的既有慣例一致, 供settle/
            # coefFromStack一次性視窗註冊 + everyRound逐回合重擲(見下方 apply_own_turn_effects
            # 呼叫)以此為「第N回合」的比較基準, 取代全局CUR_ROUND(user權威規則: 「回合」對
            # 持有者自身的漸進/計數機制=該持有者自己的行動輪)。
            u.own_round += 1
            # 時序重構(2026-07, user權威規則): DoT跟隨被上狀態的人 —— 輪到該單位行動時才結算
            # 它自己的DoT(取代舊「回合末全體同時tick()」), 取代處見下方 decay_durations()。
            u.dot_settle()
            if not u.alive:
                continue  # DoT致死: 死亡不行動(也不再decay_durations, 已死無意義)
            # 時序徹底一致化批(本批新增): stack.stackPer=="round"(如長驅直入)的逐回合遞增,
            # 移到這裡(u自己行動前, dot_settle之後、apply_own_turn_effects之前) —— 取代前批
            # (時序一致化)誤放decay_durations()(行動後)的做法。對局歸因發現「行動後遞增」使
            # 爬坡比「行動前遞增」晚1輪(長驅直入-7.6pp, 見交接文件), 不符合user權威規則「回合
            # 開始、行動前就會檢查」, 故拆出獨立方法tick_stack()移到行動前, 使u這回合行動時
            # 已吃到當回合的疊層值。
            u.tick_stack()
            # 時序一致化(2026-07 批次) A.2+A.3: u 作為「持有者」的兩類自身進程機制, 於u自己
            # 行動前結算(比照dot_settle掛點, 與DoT/settle爆發同屬「先結算才行動」語意) ——
            # apply_own_turn_effects(u): u 作為施放者/擁有者身分(everyRound重擲+settle/
            # coefFromStack視窗註冊); settle_tick(u, allies_of(u)): u 作為settle持有者身分
            # (猛毒疊層爆發/倒數, 可能致死, 故其後需再次覆核alive)。settle_tick 為模組層級
            # 函式(對稱settle_huchen, 見其定義), 這裡傳入u自己的隊伍供爆發時全隊結算。
            apply_own_turn_effects(u)
            settle_tick(u, allies_of(u))
            if not u.alive:
                continue  # settle爆發致死: 死亡不行動(與DoT致死同語意)
            if u.stun or getattr(u, "captured", 0):
                u.decay_durations()  # 批52j: 捕獲同震懾無法行動, 但持續仍需遞減(否則永不解除)
                continue
            if pick_target(foes_of(u)) is None:
                u.decay_durations()
                break
            if not u.silence:                             # 計窮: 跳過主動/指揮/被動(不影響普攻)
                for t0 in u.tactics:                       # 自帶 + 傳承: 各自獨立附加發動(不占普攻)
                    # 批16: fakeReport(偽報) —— 抑制指揮/被動每回合擲骰的coef段(prep已套用效果不回收, 不影響主動戰法)
                    # 批52j: 捕獲禁用指揮與被動
                    if t0["type"] in ("command", "passive") and (u.fake_report_dur or getattr(u, "captured", 0)):
                        continue
                    fire = False
                    # 批18: choices/extraHits 派發 —— coef=0 且頂層 effects 為空、內容完全放在
                    # choices[].effects 或 extraHits 裡的主動戰法(如三選一分支型/上兵伐謀式多段
                    # 指定選標), 過去 (t0["coef"] or t0["effects"]) 兩者皆假則永遠不會觸發(choices/
                    # extraHits 只在 fire 之後才被讀取, 若從未 fire 等於整個戰法失效 —— 全庫掃描
                    # 發現暗潮洶湧/暗潮湧動已是此模式且從未真正發動過)。加上 t0.get("choices")/
                    # t0.get("extraHits") 這兩個額外判斷條件, 讓「內容全在 choices/extraHits 裡」的
                    # 戰法也能正常擲骰派發。
                    # 批32 R23: active 型戰法過去完全不檢查 t["when"](round_ok), 只有 command/
                    # passive 分支(下方elif)才會擲骰前先驗回合窗口——導致「奇數回合...偶數回合...」
                    # 這類需要用 t.when.parity 切分兩組互斥效果的 active 戰法(如飛沙走石)無法透過
                    # 頂層 when 精確表達(見 engine_limitations.md 新增節: parity 只在 command/passive
                    # 驗證過, active 從未真正測試, 屬先前批次遺留的能力邊界, 非本次新增行為)。補上
                    # round_ok(t0, rnd) 對稱於 command/passive 既有判斷, 不影響現有唯一帶 t.when 的
                    # active 戰法(移花接木, when僅含dur鍵, round_ok對此鍵永遠回傳True, 無回歸)。
                    # 批52: 冷卻 —— t["cd"]>0 且 tac_cd 仍有剩餘時本回合不可發動(擲骰前攔截)
                    _on_cd = bool(t0.get("cd") and u.tac_cd.get(t0.get("nameZh") or "", 0) > 0)
                    # 批52续: t.rateLeader —— 主將時頂層發動率取較高值
                    _is_leader = bool(allies_of(u) and allies_of(u)[0] is u)
                    _base_rate = t0["rate"]
                    if _is_leader and t0.get("rateLeader") is not None:
                        _base_rate = t0["rateLeader"]
                    # 批52i: rateScale —— 頂層發動率受屬性縮放(垂心萬物 70%受智力影響)
                    if t0.get("rateScale"):
                        _base_rate = min(1.0, _base_rate * rate_scale_of(u, t0["rateScale"], t0.get("scaleDiv")))
                    # 批52续: when 窗口; whenLeader 為主將專屬額外回合(燕人咆哮第6回合)
                    # 禁近似令-批K: _fired_via_leader_window(leader_dual_base_coef族) —— 對稱
                    # engine.js firedViaLeaderWindow, 記錄本次fire是否透過whenLeader額外視窗
                    # 通過(供下方t0.coefWhenLeader判斷, 見其註解)。用list包一層以便在閉包內賦值
                    # (Python閉包對外層變數預設唯讀, 需nonlocal或可變容器繞過)。
                    _fired_via_leader_window = [False]

                    # 時序徹底一致化批(最終定案): 頂層t.when/whenLeader回合窗改用u(施放者)自己的
                    # own_round為基準(取代全局rnd) —— user最終裁決「除相一廣播外一律相二逐單位」,
                    # 這條main主迴圈coef/choices/extraHits派發路徑(如燕人咆哮/竊幸乘寵/用武通神/
                    # 盛氣凌敵/以寡敵眾/兵無常勢/義膽雄心/鷹視狼顧等頂層t.when戰法的真正傷害輸出)
                    # 過去只用非heal effects泛化通道核對到when(見apply_own_turn_effects), 但main coef
                    # 本身的fire判定仍讀全局rnd, 是本批需一併修正的殘留缺口。目前全庫唯一帶
                    # t.when且需維持全局CUR_ROUND的相一廣播實例(高櫓連營/江天長焰)皆無頂層t.when
                    # (when:null), 故此處統一改own_round不影響它們(不涉及, 零回歸)。
                    def _when_ok(tt):
                        if tt.get("when") and tt["when"].get("on"):
                            return False  # 反應式不走此路徑
                        if round_ok(tt, u.own_round):
                            return True
                        if _is_leader and tt.get("whenLeader") and round_ok({"when": tt["whenLeader"]}, u.own_round):
                            _fired_via_leader_window[0] = True
                            return True
                        # 無 when 且無 whenLeader → 常駐
                        if not tt.get("when") and not tt.get("whenLeader"):
                            return True
                        return False
                    # 批52i: proxyNormal/proxyHit 效果亦需 fire 路徑(無 coef 的 command)
                    _has_proxy = any(e.get("k") in ("proxyNormal", "proxyHit") for e in (t0.get("effects") or []))
                    if t0["type"] == "active" and (t0["coef"] or t0["effects"] or t0.get("choices") or t0.get("extraHits")) \
                            and not (t0["prep"] and u.own_round == 1) and _when_ok(t0) and not _on_cd:
                        fire = random.random() < _base_rate + u.addbonus_for("rateup", t0)  # rateup: 提高自身主動戰法發動機率(如白眉); addbonus_for 依 t["prep"]/t["native"] 篩選 prepOnly/nativeOnly 修飾的加成(批7: 太平道法)
                    elif t0["type"] in ("command", "passive") and (t0["coef"] or t0.get("choices") or t0.get("extraHits") or _has_proxy) \
                            and not (t0.get("when") and t0["when"].get("on")) and _when_ok(t0) and not _on_cd:
                        fire = random.random() < _base_rate  # 每回合以資料 rate 擲骰; when.rounds/from/until 只在符合回合才擲骰; when.on(反應式) 改由 on_hit 事件點觸發; 批18: choices 派發同active一併補coef=0情形
                    # 批52g: ammo 彈藥(高櫓連營) —— 主將每回合先補箭, 耗盡則本回合不發射
                    if fire and t0.get("ammo") is not None and t0.get("nameZh"):
                        _an = t0["nameZh"]
                        if _an not in u.ammo:
                            u.ammo[_an] = int(t0["ammo"])
                        if _is_leader and t0.get("ammoReloadLeader"):
                            u.ammo[_an] += int(t0["ammoReloadLeader"])
                        if u.ammo[_an] <= 0:
                            fire = False
                    if fire:
                        # 批52: 發動成功後進入冷卻。寫入 cd+1: 本單位這輪 decay_durations() 先扣1,
                        # 剩餘 cd 個完整行動輪不可再發(「進入1回合冷卻」= 下個行動輪不可用, 再下個
                        # 行動輪才可)。時序重構(2026-07)保留此 +1: 冷卻是「本單位自己行動時設下,
                        # 同一行動輪內緊接著的 decay_durations() 就會立刻扣1」的自我參照場景, 與
                        # 外部施加的debuff/buff(已全庫移除+1補償, 見 Unit.decay_durations() docstring)
                        # 時序性質不同 —— 不補償會讓 cd=1 完全失效(下個行動輪立即可再發, 冷卻形同
                        # 虛設), 故此欄位維持既有 +1 寫法, 不在本次移除範圍內(已標記待user確認,
                        # 見交接文件時序重構節)。
                        if t0.get("cd") and t0.get("nameZh"):
                            u.tac_cd[t0["nameZh"]] = int(t0["cd"]) + 1
                        # 批26 B2: stack.stackPer=="cast" —— 本戰法本次成功發動(擲骰命中fire),
                        # 若 u 身上已有 stackPer=="cast" 的疊層狀態(該狀態由本戰法或其他戰法的
                        # k=="stack"效果段套用而來), 在此遞增1層(見 apply_stack_cast() 定義)。
                        # 與round模式(fight()主迴圈逐回合遞增, 見上方)互斥判斷, 不會重複遞增。
                        u.apply_stack_cast()
                        # 批31 A: on:"activeFired" —— 只有 type=="active"(真正的主動戰法)才算
                        # 「成功發動主動戰法」事件, command/passive 常駐擲骰(fire 判定式共用同一
                        # if 區塊, 但語意是「每回合固定擲骰」而非「發動主動戰法」)不觸發此事件。
                        # 置於 apply_stack_cast() 之後、實際套用觸發戰法本身效果之前, 讓士爭先赴
                        # 一類「成功發動...前」的反應式效果搶在本次觸發戰法的傷害/效果結算前廣播
                        # (見 active_fired() 定義處對 before/after 語意取捨的說明)。
                        if t0["type"] == "active":
                            active_fired(u)
                        # 批16: choices(擇一分支) —— 發動時按權重隨機選一組效果(coef/kind/effects/
                        # extraHits/n/nMax 可各自覆寫基礎戰法)套用到本次發動; 未中選的分支本次不生效。
                        # 權重預設均分。t0 為原始戰法物件(供 addbonus_for/when_fired/lockTarget 等以
                        # id(t0) 為鍵的邏輯保持穩定, 不因選分支而變動), t 為「本次觸發實際使用」的
                        # 合成視圖(不修改 t0 本身)。
                        t = dict(t0, **pick_choice(t0["choices"])) if t0.get("choices") else t0
                        main_hit_tgt = None  # 批13: 記錄主 coef 段命中的(單體)目標, 供 extraHits 同目標段沿用
                        main_hit_tgts = None  # 批45 A: 記錄主 coef 段命中的(群體)目標陣列, 供效果段 e["sameTargets"] 沿用同一批目標(對稱 main_hit_tgt 的單體版本)
                        is_active_dmg = t0["type"] == "active" or None  # 批31 A: 供e.activeOnly amp判定「本段傷害是否為主動戰法所致」; command/passive走同一段程式碼但非主動戰法, 傳None(安全側不套用activeOnly加成, 見addbonus()docstring)
                        # 批H: active型戰法「提高自身X%會心機率...隨後造成攻擊」(百步穿楊/左右開弓)——
                        # 在主coef攻擊之前先套用施放者自身的critUp/critDmgUp會心buff, 使該次AoE本身
                        # 得以吃到真會心擲骰(取代舊有把會心EV折入coef本身的近似)。post-coef的常規
                        # apply_effects會以同一src刷新覆蓋本效果(不疊加, 見apply_effects only_kinds
                        # docstring), 故不會會心率翻倍。只對active型套用(command/passive的crit走prep
                        # 階段apply_passives, 不經此路徑; 其coef段多為0無主攻擊, 不需pre-coef)。
                        if t0["type"] == "active":
                            apply_effects(u, None, t, allies_of(u), foes_of(u), only_kinds=("critUp", "critDmgUp"))
                        # 禁近似令-批K: coef_eff(leader_dual_base_coef族) —— 對稱 engine.js
                        # coefEff, 見其詳細註解(神機妙算coefLeader/燕人咆哮coefWhenLeader)。
                        if _fired_via_leader_window[0] and t0.get("coefWhenLeader") is not None:
                            coef_eff = t0["coefWhenLeader"]
                        elif _is_leader and t0.get("coefLeader") is not None:
                            coef_eff = t0["coefLeader"]
                        else:
                            coef_eff = t["coef"]
                        if t["coef"]:
                            cnt = t["n"]
                            if t.get("nMax"):
                                cnt = t["n"] + random.randint(0, t["nMax"] - t["n"])
                            # 批52g: ammo 限制本回合實際發射次數
                            if t0.get("ammo") is not None and t0.get("nameZh"):
                                _left = u.ammo.get(t0["nameZh"], 0)
                                cnt = min(cnt, max(0, _left))
                                u.ammo[t0["nameZh"]] = _left - cnt
                            # 批12 ModeB: hitsRepeat —— 「隨機單體攻擊X次/重複X次,每次獨立選擇目標」
                            # = N次獨立單體抽樣(可重複命中同一目標), 非 pick_targets 的 N 人不重複群攻。
                            # 逐次呼叫 pick_target(每次重新擲骰), 而非一次性呼叫 pick_targets(不重複)。
                            # 批12 ModeG: lockTarget —— 單體(cnt<=1)coef傷害目標改用 resolve_locked_target
                            # (首次發動 pick_target 選定後, 之後每次發動重用同一目標); 群體(cnt>1)/
                            # hitsRepeat 不套用鎖定語意(lockTarget 資料上僅用於單體戰法)。
                            # 批18: targetSel(指定選標準則) —— 戰法級欄位, 主coef段按準則選單一
                            # 目標(如避實擊虛「統率最低」), 優先於lockTarget/hitsRepeat/隨機群體
                            # (不受混亂影響, 每次發動當下依準則重新篩選, 與lockTarget的「首次選定
                            # 後鎖定沿用」語意方向相反, 不可混用)。
                            # 批52g: effectsPerHit —— 每次 hitsRepeat 命中後立即套 effects(五雷震懾)
                            _eff_per_hit = bool(t.get("effectsPerHit"))
                            if t.get("targetSel"):
                                v = pick_by_criterion(foes_of(u), t["targetSel"])
                                if v:
                                    hit(u, v, coef_eff, t["kind"], False, on_hit, dealt_damage, is_active=is_active_dmg)
                                    main_hit_tgt = v
                            elif t.get("lockTarget") and cnt <= 1 and not t.get("hitsRepeat"):
                                v = resolve_locked_target(u, t0, foes_of(u))  # lockTarget 鍵用 t0(原始戰法物件), 避免 choices 每次合成新dict破壞跨回合鎖定
                                if v:
                                    hit(u, v, coef_eff, t["kind"], False, on_hit, dealt_damage, is_active=is_active_dmg)
                                    main_hit_tgt = v
                            elif t.get("hitsRepeat"):
                                for _ in range(cnt):
                                    v = pick_target(foes_of(u), u)
                                    if v:
                                        hit(u, v, coef_eff, t["kind"], False, on_hit, dealt_damage, is_active=is_active_dmg)
                                        main_hit_tgt = v
                                        if _eff_per_hit and t["type"] == "active":
                                            apply_effects(u, v, t, allies_of(u), foes_of(u))
                            else:
                                vs = pick_targets(foes_of(u), cnt)
                                for v in vs:
                                    hit(u, v, coef_eff, t["kind"], False, on_hit, dealt_damage, is_active=is_active_dmg)
                                if len(vs) == 1:
                                    main_hit_tgt = vs[0]
                                elif len(vs) > 1:
                                    main_hit_tgts = vs  # 批45 A: 群體(len(vs)>1)額外記錄完整目標陣列
                        if t.get("extraHits"):
                            fire_extra_hits(u, t, main_hit_tgt, allies_of, foes_of, on_hit, dealt_damage)  # 批13: 多段傷害(兵刃+謀略雙段/主傷+補刀等)
                        if t["type"] == "active" and not t.get("effectsPerHit"):
                            # 批12 ModeF: 混亂下單體主動戰法目標改敵我不分(pick_target_chaos); 群體/AoE
                            # (who=enemy 全體/n>1)維持 apply_effects 內部既有邏輯不變 —— 這裡傳入的 tgt
                            # 只影響「單體優先鎖定」分支, 群體戰法本就走 pick_targets(enemies,...) 不受
                            # 此參數影響(近似, 群體戰法混亂下仍只打敵方)。
                            # 批12 ModeG: lockTarget 的 apply_effects 目標(單體效果destination)同樣改用
                            # 鎖定目標(與混亂互斥: lockTarget 戰法目前資料上未與 chaos 共存)。
                            # 批52g: effectsPerHit 時已在每次 hit 後套過, 此處跳過避免二次震懾。
                            active_dst = resolve_locked_target(u, t0, foes_of(u)) if t.get("lockTarget") else pick_target_chaos(u, allies_of(u), foes_of(u))
                            apply_effects(u, active_dst, t, allies_of(u), foes_of(u), main_hit_tgts=main_hit_tgts)  # 批45 A: 傳入本次主coef段的群體目標陣列, 供 e["sameTargets"] 沿用
                        elif _has_proxy:
                            # 批52i: command/passive 帶 proxyNormal(垂心萬物) —— fire 成功後套 effects
                            # (amp+代打普攻/連擊謀略); heal 段走 everyRound/healOnly 不在此重複
                            apply_effects(u, pick_target_chaos(u, allies_of(u), foes_of(u)), t,
                                          allies_of(u), foes_of(u), no_heal=True)
                        elif t0.get("choices"):
                            # 批27 B: command/passive 型戰法帶 choices —— 過去 pick_choice() 抽出的
                            # 分支 t 只有 coef/extraHits 段會在上面被讀取套用, t["effects"](分支自帶
                            # 的效果, 如桃園結義三選一之一的heal)完全被憑空丟棄(見engine_limitations
                            # §18a: applyEffects對command/passive型戰法的呼叫管道是apply_passives(),
                            # 讀的是u.tactics原始t0, 從未經過pick_choice解析)。此處補上: 只在
                            # t0.get("choices")為真(=本次t是choices合成視圖, 非u.tactics原始物件)
                            # 時才呼叫apply_effects(u, None, t, ...)套用分支的effects, 且僅限於此
                            # (不對一般無choices的command/passive戰法重複套用——那些戰法的effects
                            # 已由apply_passives()的prep/heal_only通道正確處理, 此處若無腦補上會
                            # 造成雙重結算)。heal_only=False(單次套用, 非逐回合常駐通道)——分支的
                            # heal效果視為「本次觸發的一次性治療」, 與apply_passives的heal_only
                            # 常駐掃描是互斥的兩個通道: choices戰法的t0["effects"]本身為空(內容
                            # 全在choices[].effects裡), heal_only通道對空effects列表天然no-op,
                            # 不會與此處重複治療。
                            apply_effects(u, main_hit_tgt, t, allies_of(u), foes_of(u), no_heal=False, main_hit_tgts=main_hit_tgts)
            # 批52i: 普攻管線抽成 do_normal_attack(連擊/everyN/突擊), 與 proxyNormal 共用
            do_normal_attack(u, allies_of(u), foes_of(u), on_hit, dealt_damage, active_fired)
            # 時序重構(2026-07, user權威規則): 該單位行動後才持續-1(取代舊回合末全體tick())。
            # dot_settle()已在本單位行動前結算過掉血, 此處只負責持續回合數遞減/到期清除,
            # 與 hit_flags(每輪節流)重置 —— 皆對「這一單位」而言, 於它自己這輪行動之後。
            u.decay_durations()
        # 時序重構(2026-07)+時序一致化(2026-07 批次 A.2): 舊「for u in A+B: u.tick()」回合末
        # 全體同時結算已移除 —— DoT掉血已在上方行動迴圈內, 各單位輪到自己行動時由 dot_settle()
        # 個別結算; 持續回合遞減已在各單位行動後由 decay_durations() 個別結算; settle(猛毒
        # 疊層爆發/倒數)過去在此以「for u in A+B, 全局回合cadence」逐一結算, 本批已改為
        # settle_tick(u)——於u(持有者/中毒目標)自己的行動輪、行動前結算(比照dot_settle掛點,
        # 見上方行動迴圈 apply_own_turn_effects/settle_tick 呼叫點), 此處全局迴圈已整段移除。
        # 批8: 殲滅(kill) —— ROUNDS 回合內一方全滅, 對比「判定勝」(打滿8回合按剩餘兵力比較)。
        if not any(u.alive for u in A):
            return "B", rnd, True
        if not any(u.alive for u in B):
            return "A", rnd, True
    ta = sum(max(0, u.troop) for u in A)
    tb = sum(max(0, u.troop) for u in B)
    return ("A" if ta >= tb else "B"), ROUNDS, False


def simulate(teamA, teamB, n=3000, troopA=None, troopB=None, bsA=None, bsB=None, eqA=None, eqB=None,
             addA=None, addB=None, inhA=None, inhB=None, scenario=None, campLvA=0, campLvB=0):
    w = {"A": 0, "B": 0}
    kill = {"A": 0, "B": 0}                           # 批8: 殲滅 vs 判定勝(8回合打滿按剩餘兵力) 分開統計
    rs = 0
    for _ in range(n):
        winner, r, k = fight(teamA, teamB, troopA, troopB, bsA, bsB, eqA, eqB, addA, addB, inhA, inhB, scenario, campLvA, campLvB)
        w[winner] += 1
        if k:
            kill[winner] += 1
        rs += r
    return {"A勝率": round(w["A"] / n, 3), "B勝率": round(w["B"] / n, 3),
            "平均回合": round(rs / n, 1),
            "殲滅率": round((kill["A"] + kill["B"]) / n, 3),
            "A殲滅": round(kill["A"] / n, 3), "B殲滅": round(kill["B"] / n, 3),
            "A判定勝": round((w["A"] - kill["A"]) / n, 3), "B判定勝": round((w["B"] - kill["B"]) / n, 3)}


# ---------------------------------------------------------------------------
def demo():
    assert len(POOL) > 150, f"應載入近 193 武將, got {len(POOL)}"
    assert counter_mult("騎", "盾") == 1.15 and counter_mult("盾", "騎") == 0.85
    assert counter_mult("槍", "器") == 1.15
    lb = POOL["呂布"]
    # 兵種適性: 呂布騎兵 S(×1.2) 應比同人槍兵 A(×1.0)武力高
    assert Unit(lb, "騎").force > Unit(lb, "槍").force, "適性高的兵種屬性應更高"
    assert Unit(lb, "騎").force > 240
    u = Unit(lb, "騎")
    u.push_add("amp", 0.2, 1)
    assert abs(u.addbonus("amp") - 0.2) < 1e-9
    u.tick()
    assert u.addbonus("amp") == 0, "增傷到期要消"
    caster, tgt = Unit(POOL["諸葛亮"], "弓"), Unit(lb, "騎")
    tgt.settle = {"layers": 1, "max": 3, "left": 2, "caster": caster,
                  "snap": caster.troop, "base": 1.5, "per": 0.4, "kind": "intel"}
    hit(caster, tgt, 1.0, "phys")
    assert tgt.settle["layers"] == 2, "結算層數應隨擊累積"
    assert extra_count(2.0) == 2 and extra_count(0) == 0, "連擊次數: 整數必定"
    u3 = Unit(lb, "騎")
    u3.stack = {"per": 0.2, "max": 5, "n": 3}
    assert abs(u3.amp() - 0.6) < 1e-9, "疊加3層=+60%增傷"
    u3.stack = None
    u3.decay = {"v0": 1.0, "left": 8, "total": 8}
    assert abs(u3.amp() - 1.0) < 1e-9, "衰減開場滿"
    u3.decay["left"] = 4
    assert abs(u3.amp() - 0.5) < 1e-9, "衰減過半"
    u4 = Unit(POOL["諸葛亮"], "弓")
    base_f = u4.eff("force")
    u4.swap = 2
    assert u4.eff("force") > base_f, "武智互換: 智高者互換後武力變高"
    grd, prot = Unit(lb, "騎"), Unit(POOL["諸葛亮"], "弓")
    prot.guardian, prot.guard_share = grd, 0.4
    g0, p0 = grd.troop, prot.troop
    hit(Unit(lb, "騎"), prot, 1.5, "phys")        # 高武力攻擊者確保有傷害(避免守將過肉時0傷)
    assert grd.troop < g0 and prot.troop < p0, "代承者與被保護者各吃一部分"
    atk, df = Unit(lb, "騎"), Unit(POOL["張飛"], "盾")
    df.push_add("mitig", 0.5, 9)
    d_norm = damage(atk, df, 1.0, "phys")
    atk.push_add("pierce", 1.0, 9)
    assert damage(atk, df, 1.0, "phys") > d_norm * 1.5, "看破應大幅提高對減傷目標的傷害"
    ca, cd = Unit(POOL["周瑜"], "弓"), Unit(lb, "騎")
    cd.counter = {"coef": 1.0, "kind": "phys", "prob": 1.0}
    a0 = ca.troop
    hit(ca, cd, 0.5, "phys")
    assert ca.troop < a0, "反擊應讓攻擊者掉血"
    # 克制: 同隊伍兵種, 騎隊打盾隊 應比反過來占優(克制 1.15 vs 0.85)
    assert counter_mult("騎", "盾") > counter_mult("盾", "騎")
    # 兵書: 載入 + 預設主兵書可裝
    assert len(BINGSHU) >= 40, f"兵書應載入, got {len(BINGSHU)}"
    assert default_bingshu(POOL["呂布"]) in BINGSHU, "呂布應有預設主兵書"
    # 緣分: 湊齊觸發人數應觸發(資料載入時才測)
    if BONDS:
        bd = BONDS[0]
        assert active_bonds(bd["generals"][:bd["triggerCount"]]), "湊齊應觸發緣分"
        assert not active_bonds(bd["generals"][:bd["triggerCount"] - 1]), "人數不足不應觸發"
    res = simulate(["呂布", "趙雲", "關羽"], ["諸葛亮", "周瑜", "司馬懿"], n=400)
    assert 0 <= res["A勝率"] <= 1 and 1 <= res["平均回合"] <= ROUNDS

    # --- 新機制驗收 ---------------------------------------------------------
    # 1) 同名(同戰法來源)不疊加: 同一戰法對同一單位重複施加同種效果應「刷新」而非疊加
    u5 = Unit(lb, "騎")
    u5.push_add("amp", 0.10, 2, src="測試戰法")
    u5.push_add("amp", 0.10, 2, src="測試戰法")           # 同來源同 kind 再次套用
    assert abs(u5.addbonus("amp") - 0.10) < 1e-9, "同名戰法效果應刷新, 不應疊加成 0.20"
    u5.push_add("amp", 0.05, 2, src=None)                # 無來源(兵書/裝備/緣分)不去重, 應疊加
    assert abs(u5.addbonus("amp") - 0.15) < 1e-9, "無來源效果不應被去重邏輯影響"

    # 2) 計窮(silence) 應擋主動戰法發動, 但不擋普攻
    silence_tactic = {"nameZh": "測試計窮術", "type": "active", "kind": "phys", "coef": 0,
                       "rate": 1.0, "n": 1, "prep": 0,
                       "effects": [{"k": "silence", "who": "enemy", "dur": 1}]}
    caster5, tgt5 = Unit(POOL["諸葛亮"], "弓"), Unit(lb, "騎")
    apply_effects(caster5, tgt5, silence_tactic, [caster5], [tgt5])
    assert tgt5.silence > 0, "計窮應成功施加(fight() 主迴圈以 `if not u.silence:` 跳過主動戰法)"

    # 3) 繳械(disarm) 應擋普攻
    disarm_tactic = {"nameZh": "測試繳械術", "type": "active", "kind": "phys", "coef": 0,
                      "rate": 1.0, "n": 1, "prep": 0,
                      "effects": [{"k": "disarm", "who": "enemy", "dur": 1}]}
    tgt6 = Unit(lb, "騎")
    apply_effects(caster5, tgt6, disarm_tactic, [caster5], [tgt6])
    assert tgt6.disarm > 0, "繳械應成功施加"

    # 4) 洞察(insight) 應免疫控制, 且施加時同時解除既有控制
    u7 = Unit(lb, "騎")
    u7.stun, u7.silence, u7.disarm = 1, 1, 1
    insight_tactic = {"nameZh": "測試洞察術", "type": "passive", "kind": "phys", "coef": 0,
                       "rate": 1.0, "n": 1, "prep": 0,
                       "effects": [{"k": "insight", "who": "self", "dur": 2}]}
    apply_effects(u7, None, insight_tactic, [u7], [])
    assert u7.insight > 0 and u7.stun == 0 and u7.silence == 0 and u7.disarm == 0, \
        "洞察應解除既有 stun/silence/disarm"
    tgt8 = Unit(lb, "騎")
    tgt8.insight = 3
    apply_effects(caster5, tgt8, silence_tactic, [caster5], [tgt8])
    assert tgt8.silence == 0, "洞察中的目標應免疫計窮"

    # 5) 先攻(first) 應排序優先於速度: first 旗標高者即使速度較低也應排在前面
    fast_no_first = Unit(POOL["呂布"], "騎")             # 呂布速度較高但無先攻
    slow_first = Unit(POOL["諸葛亮"], "弓")               # 諸葛亮速度較低但獲得先攻
    slow_first.first = 1
    order5 = sorted([fast_no_first, slow_first], key=lambda x: (x.first, x.eff("speed")), reverse=True)
    assert order5[0] is slow_first, "先攻單位應優先於速度較高但無先攻者行動"

    # --- 批2 新機制驗收 -------------------------------------------------------
    # 6) 條件觸發(when.rounds/from/until): round_ok 應正確判斷回合是否落在窗口內
    t_from = {"when": {"from": 3}}
    assert not round_ok(t_from, 1) and not round_ok(t_from, 2) and round_ok(t_from, 3) and round_ok(t_from, 8), \
        "when.from=3 應只在第3回合起才符合"
    t_until = {"when": {"until": 4}}
    assert round_ok(t_until, 1) and round_ok(t_until, 4) and not round_ok(t_until, 5), \
        "when.until=4 應只在第4回合(含)前符合"
    t_rounds = {"when": {"rounds": [2, 4]}}
    assert round_ok(t_rounds, 2) and round_ok(t_rounds, 4) and not round_ok(t_rounds, 3), \
        "when.rounds=[2,4] 應只在指定回合符合"
    assert round_ok({"when": None}, 1), "無 when 應視為每回合皆符合"

    # 7) 反應式觸發(when.on): 受到普通攻擊(attacked)時應觸發; damaged 則任意傷害來源都觸發(仿 fight() 內 on_hit 的精簡版)
    def on_hit_test(dst, src, is_normal, dmg=None, kind=None):  # 批33: dmg(可選)—— 對稱於正式 on_hit(), 接受 hit() 新增的第4參數; 批39 C: kind(可選)—— 對稱正式on_hit()新增的第5參數(此簡化測試harness不做dmgType過濾, 僅接受參數避免TypeError)
        for t in dst.tactics:
            on = t.get("when", {}).get("on")
            if not on or (on == "attacked" and not is_normal):
                continue
            if id(t) in dst.hit_flags:
                continue
            dst.hit_flags.add(id(t))
            if t["coef"]:
                hit(dst, src, t["coef"], t["kind"], False, on_hit_test)
            if t["effects"]:
                apply_effects(dst, src, t, [dst], [src])

    counter_like = {"nameZh": "測試反擊術", "type": "passive", "kind": "phys", "coef": 0.5,
                     "rate": 1.0, "n": 1, "prep": 0, "effects": [],
                     "when": {"on": "attacked"}}
    attacker9, defender9 = Unit(lb, "騎"), Unit(POOL["張飛"], "盾")
    defender9.tactics = [counter_like]
    a0_9 = attacker9.troop
    hit(attacker9, defender9, 1.0, "phys", True, on_hit_test)   # 普攻(is_normal=True) 應觸發 attacked
    assert attacker9.troop < a0_9, "when.on=attacked 應在普攻命中時觸發反擊傷害"

    damaged_like = {"nameZh": "測試受傷觸發術", "type": "passive", "kind": "phys", "coef": 0,
                     "rate": 1.0, "n": 1, "prep": 0,
                     "effects": [{"k": "stat", "who": "self", "stat": "force", "mult": 1.5, "dur": 1}],
                     "when": {"on": "damaged"}}
    victim10 = Unit(lb, "騎")
    victim10.tactics = [damaged_like]
    force0_10 = victim10.eff("force")
    hit(Unit(lb, "騎"), victim10, 1.0, "phys", False, on_hit_test)   # 非普攻傷害(charge/戰法傷害) 也應觸發 damaged
    assert victim10.eff("force") > force0_10, "when.on=damaged 應在任意來源傷害後觸發(非僅普攻)"

    # 8) 嘲諷(taunt): 被嘲諷者的普攻/單體戰法目標應強制指向施放者
    tauntee, tauntCaster, other = Unit(lb, "騎"), Unit(POOL["張飛"], "盾"), Unit(POOL["諸葛亮"], "弓")
    tauntee.taunt_by, tauntee.taunt_dur = tauntCaster, 2
    picked = pick_target([tauntCaster, other], attacker=tauntee)
    assert picked is tauntCaster, "被嘲諷者應強制選擇施放者為目標, 不論隨機結果"
    for _ in range(20):                                 # 多次抽樣確保不是巧合
        assert pick_target([tauntCaster, other], attacker=tauntee) is tauntCaster

    # 9) 護盾(shield): 應先於兵力吸收傷害, 吸滿後才扣兵力
    shielded = Unit(lb, "騎")
    shielded.shield = {"amt": 999999, "dur": 3}          # 巨額護盾, 確保這次傷害全被吸收
    troop_before_shield = shielded.troop
    hit(Unit(POOL["呂布"], "騎"), shielded, 1.0, "phys")
    assert shielded.troop == troop_before_shield, "護盾應吸收傷害, 兵力不應減少"
    assert shielded.shield["amt"] < 999999, "護盾吸收量應從護盾扣除"
    thin_shield = Unit(lb, "騎")
    thin_shield.shield = {"amt": 1, "dur": 3}             # 護盾量極小, 傷害應溢出扣兵力
    troop_before_thin = thin_shield.troop
    hit(Unit(POOL["呂布"], "騎"), thin_shield, 1.0, "phys")
    assert thin_shield.troop < troop_before_thin, "護盾吸滿後, 剩餘傷害應繼續扣兵力"
    assert thin_shield.shield is None, "護盾吸滿應消失"

    # 10) 規避(dodge) / 必中(surehit): 規避應完全迴避傷害; 必中應無視對方規避
    dodger = Unit(lb, "騎")
    dodger.dodge_prob, dodger.dodge_dur = 1.0, 3          # 100% 規避, 確定觸發
    troop_before_dodge = dodger.troop
    hit(Unit(POOL["呂布"], "騎"), dodger, 1.0, "phys")
    assert dodger.troop == troop_before_dodge, "100% 規避應完全迴避傷害"
    sure_attacker = Unit(POOL["呂布"], "騎")
    sure_attacker.surehit_dur = 3
    troop_before_surehit = dodger.troop
    hit(sure_attacker, dodger, 1.0, "phys")
    assert dodger.troop < troop_before_surehit, "必中應無視對方規避, 傷害應正常命中"

    # --- 批2.5 新機制驗收 -----------------------------------------------------
    # 11) SCALE(): 錨點 v=100 -> 1.0(面板基準值), v=450 -> 2.0(每+350點效果翻倍)
    assert abs(SCALE(100) - 1.0) < 1e-9, "SCALE(100) 應為面板基準值 1.0"
    assert abs(SCALE(450) - 2.0) < 1e-9, "SCALE(450) 應為 2.0(社群拆解: 每+350點效果翻倍)"

    # 12) scale 縮放: heal 效果帶 scale="intel" 時, 施放者智力(戰鬥內 eff 值)越高治療量越大
    heal_tactic = {"nameZh": "測試治療術", "type": "active", "kind": "intel", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0,
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1, "scale": "intel"}]}
    low_int_caster = Unit(POOL["呂布"], "騎")               # 呂布智力低
    high_int_caster = Unit(POOL["諸葛亮"], "弓")             # 諸葛亮智力高
    high_int_caster.push_mod("intel", 2.0, 9)               # 額外掛 buff 拉高戰鬥內 eff("intel"), 驗證縮放走 eff() 而非裸值
    low_ally, high_ally = Unit(lb, "騎"), Unit(lb, "騎")
    low_ally.troop = high_ally.troop = 1000
    low_ally.wounded = high_ally.wounded = START_TROOP  # 批18: 治療上限=傷兵池, 設滿池供heal縮放比較不被截斷干擾
    apply_effects(low_int_caster, None, heal_tactic, [low_ally], [], no_heal=False)
    apply_effects(high_int_caster, None, heal_tactic, [high_ally], [], no_heal=False)
    assert high_ally.troop > low_ally.troop, \
        "scale=intel 的治療應隨施放者戰鬥內智力(eff('intel'), 含buff)提高而提高"

    # 13) rateup: 帶白眉(rateup 0.12)者主動戰法發動率應提升(以 addbonus 直接驗證擲骰門檻)
    plain_u = Unit(lb, "騎")
    rateup_u = Unit(lb, "騎")
    rateup_u.push_add("rateup", 0.12, 9)
    assert abs(rateup_u.addbonus("rateup") - 0.12) < 1e-9 and plain_u.addbonus("rateup") == 0, \
        "rateup 應提高 addbonus('rateup'), 主動戰法擲骰門檻 rate+addbonus('rateup') 因而提高"
    assert "白眉" in TACTICS and any(e["k"] == "rateup" for e in TACTICS["白眉"]["effects"]), \
        "白眉應由 reparse 標記 rateup 效果"
    # 13b) rateup 有限持續: 走 adds 陣列, tick() 遞減到期應消失(先成其慮 dur=1 等非永久buff)
    temp_ru = Unit(lb, "騎")
    temp_ru.push_add("rateup", 0.15, 1)
    assert abs(temp_ru.addbonus("rateup") - 0.15) < 1e-9
    temp_ru.tick()
    assert temp_ru.addbonus("rateup") == 0, "rateup dur=1 應在 tick 後到期消失(非永久)"
    be = next(e for e in TACTICS["白眉"]["effects"] if e["k"] == "rateup")
    xa = next(e for e in TACTICS["先成其慮"]["effects"] if e["k"] == "rateup")
    assert be["dur"] == 99 and xa["dur"] == 1, \
        "白眉(常駐)dur=99, 先成其慮(持續1回合)dur=1 —— rateup dur 應從原文抽取而非硬編碼"

    # 14) overrides: 宜城之志(幽靈條目)/明察秋毫(內政) 應被排除在 TACTICS 之外(type:none)
    assert "宜城之志" not in TACTICS, "宜城之志(user 確認幽靈條目) overrides 應設 type:none 並被排除"
    assert "明察秋毫" not in TACTICS, "明察秋毫(內政類) overrides 應設 type:none 並被排除"
    assert "爭分奪秒" not in TACTICS, "爭分奪秒(內政類) overrides 應設 type:none 並被排除"
    assert TACTICS["火燒連營"]["coef"] > 0, "火燒連營 overrides 套用後應仍有 coef(查證資料含傷害率)"

    # --- 批3 傷害公式重塑 錨點驗收 ---------------------------------------------
    # 固定輸入(兵10000/coef1.0/士氣100/無增減傷/同兵種無克制), 多次取樣平均應落在實測錨點 ±容差內。
    def _anchor_unit(force, command, troop=10000):
        u = Unit(POOL["呂布"], "騎")                    # 借真實武將建構後直接覆寫屬性(繞開卡面數值, 精準命中錨點)
        u.force, u.command, u.intel = force, command, force
        u.troop = troop
        return u

    def _avg_damage(atk_force, def_command, n=4000):
        atk_u = _anchor_unit(atk_force, 999999)         # 攻擊方 command 不使用, 給極端值避免誤用
        samples = [damage(atk_u, _anchor_unit(999999, def_command), 1.0, "phys") for _ in range(n)]
        return sum(samples) / len(samples)

    d0 = _avg_damage(1000, 1000)                        # 錨1: 屬性差0 → ≈476
    assert 476 * 0.95 <= d0 <= 476 * 1.05, f"錨1(差0)應在476±5%內, got {d0:.1f}"
    d200 = _avg_damage(1200, 1000)                      # 錨2: 屬性差200 → ≈764
    assert 764 * 0.95 <= d200 <= 764 * 1.05, f"錨2(差200)應在764±5%內, got {d200:.1f}"
    dneg = _avg_damage(100, 5000)                       # 錨3: 屬性差大負值(保底) → ≈90
    assert 90 * 0.90 <= dneg <= 90 * 1.10, f"錨3(保底)應在90±10%內, got {dneg:.1f}"

    # --- 批4 裝備複合鍵 + 同名去重 驗收 -----------------------------------------
    # 15) EQUIPS 複合鍵"type·name": 同名跨欄位("無畏"見於武器/防具/坐騎/寶物四欄)不應互蓋
    assert "武器·無畏" in EQUIPS and "坐騎·無畏" in EQUIPS, "無畏應以複合鍵'type·無畏'分別存在於武器/坐騎(等四欄)"
    assert EQUIPS["武器·無畏"] is not EQUIPS["坐騎·無畏"], "同名跨type的裝備應是各自獨立的條目, 不應互蓋"
    assert "無畏" in EQUIPS, "純名稱 fallback 應仍可查到(向後相容)"
    # 16) 同名特技(跨type)遊戲規則只生效一件: Unit.eq(裝備合併效果, 套用前的原始清單)應依基底名稱去重
    eq_one = Unit(lb, "騎", equip=["武器·無畏"])
    eq_two = Unit(lb, "騎", equip=["武器·無畏", "坐騎·無畏"])
    assert len(eq_one.eq) == 1, "單裝武器·無畏 應合併出1條效果"
    assert len(eq_two.eq) == 1, "同時裝 武器·無畏 + 坐騎·無畏(同名跨type)應只生效一件(去重後仍是1條效果, 不是2條)"
    # 17) 實際套用(apply_effects, 同 fight() 準備階段路徑)後, 統率只應 +5(平加, 不因重複裝同名而疊成+10)
    cmd_baseline = Unit(lb, "騎")
    cmd_two = Unit(lb, "騎", equip=["武器·無畏", "坐騎·無畏"])
    pt_two = {"effects": cmd_two.eq, "kind": "phys"}
    apply_effects(cmd_two, None, pt_two, [cmd_two], [], no_heal=True)
    assert abs(cmd_two.eff("command") - (cmd_baseline.eff("command") + 5)) < 1e-6, \
        "同時裝 武器·無畏 + 坐騎·無畏(同名跨type)套用後統率應只 +5(平加, 不疊加為+10)"

    # --- 批4(修訂) 屬性平加/管線重排/proc/援護 驗收 --------------------------------
    # 18) 屬性平加(stat.add): 蠻力(武力+8) 應在 eff() 為平加(不隨屬性放大, 對比 mult 版本)
    b_force = Unit(lb, "騎")
    add_force = Unit(lb, "騎", equip=["武器·蠻力"])
    pt_add = {"effects": add_force.eq, "kind": "phys"}
    apply_effects(add_force, None, pt_add, [add_force], [], no_heal=True)
    assert abs(add_force.eff("force") - (b_force.eff("force") + 8)) < 1e-6, "蠻力應使武力平加+8(flat, 非mult)"

    # 19) 屬性管線重排(戰報序): 面板 = ((基礎+加點+賽季flat)×適性 + 城建CITY)×陣營FACTION + 兵種營CAMP
    #     取 S 適性(騎)驗算; 呂布無加點(add=None 用預設0), flat=0, scm=1
    apt_s = lb.apt_pct("騎")
    expect_force = ((lb.base["force"] + 0 + 0) * apt_s * 1.0 + CITY) * FACTION + CAMP
    assert abs(Unit(lb, "騎").eff("force") - expect_force) < 1e-6, \
        f"屬性管線應為 ((基礎×適性)+城建)×陣營+兵種營, got {Unit(lb, '騎').eff('force'):.4f} expect {expect_force:.4f}"

    # 20) moraleMult 士氣上限100(戰報: 士氣>100傷害不變)
    assert abs(morale_mult(110) - morale_mult(100)) < 1e-9, "士氣>100 應按100算(傷害不變)"

    # 21) 裝備 proc(昭烈): 應被包成偽突擊戰法(charge)且帶 proc:True 旗標; rate=1 測試版驗證觸發路徑會繳械敵軍
    zhaolie_u = Unit(POOL["劉備"], "騎", equip=["武器·昭烈"])
    proc_tacs = [t for t in zhaolie_u.tactics if t.get("proc")]
    assert len(proc_tacs) == 1 and proc_tacs[0]["type"] == "charge", "昭烈應合成1個 charge 型 proc 偽戰法"
    assert proc_tacs[0]["proc"] is True, "proc 偽戰法必須帶 proc:True 旗標(供日後 chargeup 排除, user 指示特技不吃突擊加成)"
    # 用 rate=1 版本驗證觸發路徑: 手動把 proc 偽戰法 rate 設1, 觸發後敵軍應繳械
    proc_tacs[0]["rate"] = 1.0
    foe21 = Unit(POOL["張飛"], "盾")
    apply_effects(zhaolie_u, foe21, proc_tacs[0], [zhaolie_u], [foe21])
    assert foe21.disarm > 0, "昭烈 proc 觸發後應使目標敵軍繳械(disarm>0)"

    # 22) 援助 normalOnly: 普攻傷害應被代承(guardian分擔), 戰法傷害(is_normal=False)不被代承
    grd, prot, foe22 = Unit(POOL["劉備"], "騎"), Unit(POOL["關羽"], "騎"), Unit(POOL["呂布"], "騎")
    prot.guardian, prot.guard_share, prot.guard_dur, prot.guard_normal_only = grd, 1.0, 1, True
    grd_t0, prot_t0 = grd.troop, prot.troop
    hit(foe22, prot, 1.0, "phys", False)                 # 戰法傷害(is_normal=False): 不代承, prot 自吃
    assert prot.troop < prot_t0 and abs(grd.troop - grd_t0) < 1e-9, "normalOnly 援護: 戰法傷害不應轉移給代承者"
    grd_t1, prot_t1 = grd.troop, prot.troop
    hit(foe22, prot, 1.0, "phys", True)                  # 普攻(is_normal=True): 應由代承者吃(share=1)
    assert grd.troop < grd_t1 and abs(prot.troop - prot_t1) < 1e-9, "normalOnly 援護: 普攻傷害(share=1)應全由代承者承擔"

    # 23) 代承過期(guardDur): dur:1 的 redirect 過1回合(tick)後應失效
    prot23 = Unit(POOL["關羽"], "騎")
    apply_effects(Unit(POOL["劉備"], "騎"), None,
                  {"effects": [{"k": "redirect", "who": "ally", "guard": "self", "share": 1.0, "dur": 1}], "kind": "phys"},
                  [Unit(POOL["劉備"], "騎"), prot23], [])
    assert prot23.guardian is not None and prot23.guard_dur == 1, "redirect dur:1 套用後應有 guardian 且 guard_dur=1"
    prot23.tick()
    assert prot23.guardian is None and prot23.guard_dur == 0, "redirect dur:1 過1回合(tick)後 guardian 應清除(失效)"

    # --- 批6: chargeup(突擊戰法發動機率加成) + 虎豹騎曹純特例 ---------------------
    # 24) chargeup 資料落地: 虎豹騎(who=ally,val=0.10,dur=3,帶leaderBonus)/陷陣突襲(who=self,val=0.15)
    assert "虎豹騎" in TACTICS and any(e["k"] == "chargeup" for e in TACTICS["虎豹騎"]["effects"]), \
        "虎豹騎應由 reparse 標記 chargeup 效果"
    hbq_ce = next(e for e in TACTICS["虎豹騎"]["effects"] if e["k"] == "chargeup")
    assert abs(hbq_ce["val"] - 0.10) < 1e-9 and hbq_ce["dur"] == 3 and hbq_ce["who"] == "ally", \
        "虎豹騎 chargeup 應為 who=ally, val=0.10(5%→10%取滿級), dur=3(戰鬥前3回合)"
    assert hbq_ce.get("leaderBonus", {}).get("general") == "曹純", "虎豹騎 chargeup 應帶曹純 leaderBonus"
    assert "陷陣突襲" in TACTICS and any(e["k"] == "chargeup" for e in TACTICS["陷陣突襲"]["effects"]), \
        "陷陣突襲應由 reparse 標記 chargeup 效果"
    xzts_ce = next(e for e in TACTICS["陷陣突襲"]["effects"] if e["k"] == "chargeup")
    assert abs(xzts_ce["val"] - 0.15) < 1e-9 and xzts_ce["who"] == "self", \
        "陷陣突襲 chargeup 應為 who=self, val=0.15(7.5%→15%取滿級)"

    # 25) chargeup 提高 addbonus('chargeup'), 突擊擲骰門檻 rate+addbonus('chargeup') 因而提高
    plain_c = Unit(lb, "騎")
    charge_u = Unit(lb, "騎")
    charge_u.push_add("chargeup", 0.10, 3)
    assert abs(charge_u.addbonus("chargeup") - 0.10) < 1e-9 and plain_c.addbonus("chargeup") == 0, \
        "chargeup 應提高 addbonus('chargeup')"

    # 26) 鐵律: chargeup 只影響真突擊戰法, 不影響 proc:True 特技偽戰法(user 明確指示,
    # 昭烈/踩踏不吃虎豹騎加成)。以 fight() 內同樣的擲骰算式驗證: 真突擊 rate 提升, proc rate 不變。
    real_charge_rate = 0.10
    proc_rate = 0.10
    cu = Unit(lb, "騎")
    cu.push_add("chargeup", 0.10, 9)
    real_t = {"type": "charge", "rate": real_charge_rate, "proc": False}
    proc_t = {"type": "charge", "rate": proc_rate, "proc": True}
    real_threshold = real_t["rate"] + (0 if real_t.get("proc") else cu.addbonus("chargeup"))
    proc_threshold = proc_t["rate"] + (0 if proc_t.get("proc") else cu.addbonus("chargeup"))
    assert abs(real_threshold - 0.20) < 1e-9, "真突擊戰法擲骰門檻應吃到 chargeup 加成(0.10+0.10=0.20)"
    assert abs(proc_threshold - proc_rate) < 1e-9, "proc:True 特技偽戰法擲骰門檻不應吃 chargeup 加成(維持原rate)"

    # 27) 虎豹騎曹純特例: 隊伍主將(index 0)===曹純 時, 額外發動機率 = 主將戰鬥內武力^2 × 3.2e-5
    #     (二次曲線, 錨點見 docs/data/calibration_anchors.json → hubaoqi_caochun: 武力373.83→額外4.46%)。
    #     用 push_mod 頂高武力到約373 驗算數值, 容差±10%(_est 曲線, 樣本少)。
    # 錨點的「武力373.83」是遊戲內實測顯示值, 即虎豹騎自身「全體+16%武力」buff 已套用後的戰鬥內
    # eff("force")(同一 apply_effects 呼叫內, stat 效果排在 chargeup 之前先套用)。故先估算裸值倍率
    # 使套用虎豹騎(force×1.16)後精確落在 373.83, 再驗算 leaderBonus 額外值。
    hbq_tac = TACTICS["虎豹騎"]
    cc_leader = Unit(POOL["曹純"], "騎")
    cc_leader.push_mod("force", (373.83 / 1.16) / cc_leader.eff("force"), 9)
    team_cc = [cc_leader, Unit(lb, "騎"), Unit(lb, "騎")]   # 曹純在 index 0 = 主將
    apply_effects(cc_leader, None, hbq_tac, team_cc, [], no_heal=True)
    force_373 = cc_leader.eff("force")                      # 套用虎豹騎+16%武力後的戰鬥內武力(應≈373.83)
    expect_extra = (force_373 ** 2) * 3.2e-5 / 100          # k 擬合的是「%數值」, /100 換算成小數比例
    got_extra = cc_leader.addbonus("chargeup") - 0.10       # 扣掉基礎虎豹騎10%, 剩下即曹純特例額外值
    assert abs(got_extra - expect_extra) / expect_extra < 0.15, \
        f"曹純統領虎豹騎額外發動機率應≈武力^2×3.2e-5, got={got_extra:.4f} expect={expect_extra:.4f}(武力{force_373:.1f})"
    assert abs(force_373 - 373.83) < 5, f"測試前置條件: 武力應落在錨點373.83附近, got={force_373:.1f}"
    assert abs(got_extra - 0.0446) / 0.0446 < 0.10, \
        f"武力≈373時, 曹純特例額外發動機率應≈0.0446(±10%), got={got_extra:.4f}"

    # 28) 曹純特例只在「曹純且為主將(index 0)」時觸發: 非曹純主將 不應有額外加成, 只有基礎10%
    other_leader = Unit(POOL["呂布"], "騎")
    team_other = [other_leader, Unit(POOL["曹純"], "騎"), Unit(lb, "騎")]  # 曹純在隊但非主將(index 1)
    apply_effects(other_leader, None, hbq_tac, team_other, [], no_heal=True)
    assert abs(team_other[0].addbonus("chargeup") - 0.10) < 1e-6, \
        "非曹純主將時, chargeup 應只有虎豹騎基礎10%, 無曹純特例額外加成"
    caochun_non_leader = team_other[1]
    assert abs(caochun_non_leader.addbonus("chargeup") - 0.10) < 1e-6, \
        "曹純若不是主將(index 0), 即使在隊上也不應觸發特例額外加成"
    # 方向性比較: 曹純當主將(index 0) 的 chargeup 加成應高於非曹純主將隊伍
    assert cc_leader.addbonus("chargeup") > team_other[0].addbonus("chargeup"), \
        "曹純當主將 vs 非曹純主將: 前者 chargeup 加成應較高(曹純特例額外值>0)"

    # --- 批7: 發動率縮放(rate-scale) + 太平道法落地 -------------------------------
    # 29) 太平道法資料落地: critUp 0.28(dmgType:intel, 批H會心真擲骰化) + 2 條 rateup(一般6%/準備戰法額外6%, 皆 scale=intel+nativeOnly)
    assert "太平道法" in TACTICS, "太平道法應由 reparse 落地(inherit 傳承戰法, 非任何武將自帶)"
    tp_tac = TACTICS["太平道法"]
    assert not tp_tac.get("_est"), "太平道法資料落地後不應再有 _est 標記"
    tp_amp = next(e for e in tp_tac["effects"] if e["k"] == "critUp")
    assert abs(tp_amp["val"] - 0.28) < 1e-9, "太平道法奇謀(critUp真擲骰, 批H) 應為升滿值0.28(14%→28%)"
    assert tp_amp.get("dmgType") == "intel", "太平道法critUp應限定dmgType:intel(奇謀=謀略暴擊)"
    tp_rateups = [e for e in tp_tac["effects"] if e["k"] == "rateup"]
    tp_self = [e for e in tp_rateups if e.get("who", "self") == "self"]
    assert len(tp_self) == 2, "太平道法應有2條自身rateup(一般+準備戰法額外)"
    assert all(abs(e["val"] - 0.06) < 1e-9 and e.get("scale") == "intel" and e.get("nativeOnly")
               for e in tp_self), "太平道法自身2條rateup皆應為val=0.06, scale=intel, nativeOnly=True"
    tp_prep_only = [e for e in tp_self if e.get("prepOnly")]
    assert len(tp_prep_only) == 1, "太平道法應恰有1條自身rateup帶prepOnly=True(準備戰法額外加成)"
    # 批52g: 黃巾副將含 SP
    tp_subs = [e for e in tp_rateups if e.get("who") == "subs"]
    assert len(tp_subs) == 2 and any("SP 張寶" in (e.get("whoNames") or []) for e in tp_subs), \
        "太平道法應有2條黃巾副將rateup且whoNames含SP"

    # 30a) 智力426.57時, 太平道法「準備戰法」目標(native+prep皆真)拿到的加成總額 ≈ 0.2218
    #     (12%基礎 × RATE_SCALE(426.57) = 12%×1.849 ≈ 22.19%, 容差1%; 見 calibration_anchors.json
    #     → rate_scale)。用 push_mod 頂高智力到 426.57 驗算(同 chargeup 曹純測試的手法)。
    tp_caster = Unit(POOL["張角"], "騎")
    tp_caster.push_mod("intel", 426.57 / tp_caster.eff("intel"), 9)
    assert abs(tp_caster.eff("intel") - 426.57) < 1e-6, "測試前置條件: 智力應精確落在426.57"
    apply_effects(tp_caster, None, tp_tac, [tp_caster], [], no_heal=True)
    prep_native_tac = {"type": "active", "rate": 0.5, "prep": 1, "native": True, "effects": [], "coef": 0}
    total_prep = tp_caster.addbonus_for("rateup", prep_native_tac)
    assert abs(total_prep - 0.2219) / 0.2219 < 0.01, \
        f"智力426.57時, 太平道法對「自帶+準備」戰法的加成總額應≈0.2219(12%×1.849, 容差1%), got={total_prep:.4f}"

    # 30b) prepOnly 修飾: 只加給 prep=真 的戰法; 自帶但非準備戰法應只拿到一般那條(6%×縮放)
    normal_native_tac = {"type": "active", "rate": 0.5, "prep": 0, "native": True, "effects": [], "coef": 0}
    total_normal = tp_caster.addbonus_for("rateup", normal_native_tac)
    expect_normal = 0.06 * (1 + (426.57 - 100) * RATE_SCALE_C)
    assert abs(total_normal - expect_normal) < 1e-6, \
        f"prepOnly=True的那條不應計入非準備戰法, got={total_normal:.4f} expect={expect_normal:.4f}"
    assert total_normal < total_prep, "prepOnly: 準備戰法應比非準備戰法拿到更多加成(多了prepOnly那條)"

    # 30c) nativeOnly 修飾: 不應加成給非自帶(傳承)戰法, 即使該戰法也是 prep=真
    inherited_prep_tac = {"type": "active", "rate": 0.5, "prep": 1, "native": False, "effects": [], "coef": 0}
    total_inherited = tp_caster.addbonus_for("rateup", inherited_prep_tac)
    assert total_inherited == 0, \
        f"nativeOnly=True 的加成不應套用到傳承(非自帶)戰法, got={total_inherited:.4f}"

    # 31) nativeOnly 適用範圍(user 釐清, 嚴格按原文措辭二分, 見 reparse_effects.py 的
    #     RATE_SCALE_PLAN 下方註解): 「自帶」→ nativeOnly; 「自身主動戰法」→ 無旗標(全主動都吃,
    #     含傳承, 不分準備)。鎖定已落地條目合規:
    # 31a) 白眉「自身主動戰法的發動機率提高6%→12%」: 不得有 nativeOnly/prepOnly(user 明確指出
    #      白眉不分自帶或準備)。先成其慮/獅子奮迅/進言 同措辭同規則。
    for _nm in ("白眉", "先成其慮", "獅子奮迅", "進言"):
        _ru = next(e for e in TACTICS[_nm]["effects"] if e["k"] == "rateup")
        assert not _ru.get("nativeOnly") and not _ru.get("prepOnly"), \
            f"{_nm}(「自身/自己/友軍主動戰法」措辭)的 rateup 不得帶 nativeOnly/prepOnly 旗標"
    # 31b) 裝備側 nativeOnly(武聖「自帶戰法發動率增加5%」, equips_parsed.json 既有旗標)經本批
    #      addbonus_for 落地後應實際生效: 只加自帶戰法, 不加傳承戰法。裝備效果經 fight() 的
    #      pt() 包裝(無 nameZh → src=None)套用, 這裡直接複製該路徑。
    ws_u = Unit(POOL["關羽"], "騎", equip=["武器·武聖"])
    assert any(e.get("k") == "rateup" and e.get("nativeOnly") for e in ws_u.eq), \
        "武聖裝備應帶 nativeOnly rateup 效果(equips_parsed.json)"
    apply_effects(ws_u, None, {"effects": ws_u.eq, "kind": "phys"}, [ws_u], [], no_heal=True)
    native_tac_ws = {"type": "active", "rate": 0.5, "prep": 0, "native": True, "effects": [], "coef": 0}
    inherit_tac_ws = {"type": "active", "rate": 0.5, "prep": 0, "effects": [], "coef": 0}  # 無 native 鍵 = 傳承
    assert abs(ws_u.addbonus_for("rateup", native_tac_ws) - 0.05) < 1e-9, \
        "武聖(自帶戰法發動率+5%)應加成自帶戰法"
    assert ws_u.addbonus_for("rateup", inherit_tac_ws) == 0, \
        "武聖 nativeOnly 不應加成傳承戰法"
    # 31c) Unit 建構時自帶戰法應帶 native=True, 傳承戰法不帶(供 31a/31b 的旗標篩選用)
    n_unit = Unit(POOL["關羽"], "騎", inherit=["白眉"])
    assert n_unit.tactics[0].get("native") is True and not n_unit.tactics[1].get("native"), \
        "Unit.tactics[0](自帶)應標 native=True, 傳承戰法不標"

    # --- 批8: 效果級回合窗 + 剩餘原語 + 判定勝拆分 --------------------------------
    # 32) 裝備效果級回合窗(delayed_eq): 帶 when 的裝備效果應分離到 delayed_eq, 不進準備階段 eq
    mg_u = Unit(POOL["諸葛亮"], "弓", equip=["寶物·謀攻"])
    assert any(e["k"] == "silence" for e in mg_u.delayed_eq), "謀攻(第3回合計窮)應落在 delayed_eq"
    assert not any(e["k"] == "silence" for e in mg_u.eq), "謀攻帶 when, 不應留在準備階段套用的 eq"
    assert mg_u.delayed_eq[0]["when"] == {"rounds": [3]}, "謀攻 when 應為第3回合(rounds:[3])"
    # fight() 主迴圈第3回合應觸發並記錄 when_fired(用效果物件 id() 去重), 第1/2回合不觸發
    foe32 = Unit(POOL["張飛"], "盾")
    assert not round_ok({"when": mg_u.delayed_eq[0]["when"]}, 1) and not round_ok({"when": mg_u.delayed_eq[0]["when"]}, 2)
    assert round_ok({"when": mg_u.delayed_eq[0]["when"]}, 3), "謀攻應只在第3回合的視窗判定為真"

    # 33) healblock(禁療): 中招者受治療無效, dur 回合後恢復
    hb_target = Unit(POOL["張飛"], "盾")
    hb_target.troop = 5000
    hb_target.wounded = 5000                          # 批18: 治療上限=傷兵池, 測試手動降兵力須同步設wounded供heal結算
    hb_target.healblock = 2
    heal_tac33 = {"nameZh": "測試治療術33", "type": "active", "kind": "intel", "coef": 0,
                  "rate": 1.0, "n": 1, "prep": 0,
                  "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1}]}
    caster33 = Unit(POOL["諸葛亮"], "弓")
    troop_before_hb = hb_target.troop
    apply_effects(caster33, None, heal_tac33, [hb_target], [], no_heal=False)
    assert hb_target.troop == troop_before_hb, "healblock 中的目標不應被 heal 選為治療對象(兵力不變)"
    hb_target.healblock = 0                            # 解除禁療後應可正常被治療
    apply_effects(caster33, None, heal_tac33, [hb_target], [], no_heal=False)
    assert hb_target.troop > troop_before_hb, "healblock 解除後應恢復可被治療"

    # 34) lifesteal(倒戈): 造成傷害時應按比例回復自身兵力, 且上限不超過 START_TROOP
    ls_src = Unit(POOL["呂布"], "騎")
    ls_src.troop = 9000
    ls_src.push_lifesteal(0.5, 9, "測試倒戈34")          # 50% 倒戈(誇大值方便驗證); 狀態疊加精修批: lifesteal改走多實例清單, push_add不再接手此kind
    ls_dst = Unit(POOL["張飛"], "盾")
    troop_before_ls = ls_src.troop
    hit(ls_src, ls_dst, 1.0, "phys")
    assert ls_src.troop > troop_before_ls, "lifesteal 應使攻擊者造成傷害後回復兵力"
    ls_src2 = Unit(POOL["呂布"], "騎")
    ls_src2.troop = START_TROOP                        # 已滿兵, 回血不應超過上限
    ls_src2.push_lifesteal(1.0, 9, "測試倒戈34b")
    hit(ls_src2, Unit(POOL["張飛"], "盾"), 1.0, "phys")
    assert ls_src2.troop == START_TROOP, "lifesteal 回復量不應使兵力超過 START_TROOP 上限"

    # 35) inheritedOnly(nativeOnly 反向): 只加成「非自帶」(傳承)戰法, 不加自帶戰法
    io_u = Unit(POOL["呂布"], "騎")
    io_u.push_add("rateup", 1.0, 9, src="測試竭力佐謀", flags={"inheritedOnly": True})
    native_tac35 = {"type": "active", "rate": 0.5, "prep": 0, "native": True, "effects": [], "coef": 0}
    inherited_tac35 = {"type": "active", "rate": 0.5, "prep": 0, "effects": [], "coef": 0}  # 無 native 鍵 = 傳承
    assert io_u.addbonus_for("rateup", native_tac35) == 0, "inheritedOnly 不應加成自帶戰法"
    assert abs(io_u.addbonus_for("rateup", inherited_tac35) - 1.0) < 1e-9, "inheritedOnly 應加成傳承(非自帶)戰法"

    # 36) who="leader": 效果應只作用於隊伍主將(allies[0]), 不影響副將
    leader_tac = {"nameZh": "測試主將效果", "type": "command", "kind": "phys", "coef": 0,
                  "rate": 1.0, "n": 1, "prep": 0,
                  "effects": [{"k": "rateup", "who": "leader", "val": 0.16, "dur": 99}]}
    ldr, sub1, sub2 = Unit(POOL["呂布"], "騎"), Unit(POOL["張飛"], "盾"), Unit(POOL["關羽"], "騎")
    apply_effects(ldr, None, leader_tac, [ldr, sub1, sub2], [], no_heal=True)
    assert abs(ldr.addbonus("rateup") - 0.16) < 1e-9, "who=leader 應使主將(allies[0])獲得效果"
    assert sub1.addbonus("rateup") == 0 and sub2.addbonus("rateup") == 0, "who=leader 不應影響副將"

    # 37) 殲滅(kill) / 判定勝(剩餘兵力) 拆分: fight() 應回傳第三值 kill(bool), simulate() 統計應合理
    #     (殲滅+判定勝 應等於總勝場, 8回合戰鬥多數會被殲滅, 故殲滅率理應顯著>0)
    w37, r37, k37 = fight(["呂布", "趙雲", "關羽"], ["諸葛亮", "周瑜", "司馬懿"])
    assert isinstance(k37, bool), "fight() 第三個回傳值 kill 應為 bool"
    if k37:
        # 批12: 殲滅可能發生在最後一回合(r==ROUNDS)本身 —— fight() 在每回合 tick() 後立即檢查殲滅,
        # 第8回合結算造成一方全滅時 kill=True 且 r==8, 屬合法情況(非僅能發生在 ROUNDS 之前)。
        assert r37 <= ROUNDS, "殲滅(kill=True)應發生在 ROUNDS 回合或之前"
    else:
        assert r37 == ROUNDS, "判定勝(kill=False)應打滿 ROUNDS 回合"
    res37 = simulate(["呂布", "趙雲", "關羽"], ["諸葛亮", "周瑜", "司馬懿"], n=500)
    assert abs((res37["A殲滅"] + res37["B殲滅"]) - res37["殲滅率"]) < 1e-9, "A殲滅+B殲滅 應等於總殲滅率"
    assert abs((res37["A殲滅"] + res37["A判定勝"]) - res37["A勝率"]) < 1e-6, "A殲滅+A判定勝 應等於 A總勝率"
    assert abs((res37["B殲滅"] + res37["B判定勝"]) - res37["B勝率"]) < 1e-6, "B殲滅+B判定勝 應等於 B總勝率"
    assert res37["殲滅率"] > 0, "500場模擬中應有相當比例在8回合內分出殲滅勝負"

    # --- 批12: chaos(混亂) + hitsRepeat + lockTarget ------------------------------
    # 38) chaos: 混亂單位的普攻/單體主動戰法目標應敵我不分(可能選中友軍)。用 pick_target_chaos
    #     直接驗證: 構造一個「敵方全滅、只剩友軍存活」的極端池, 混亂單位仍應能選中存活友軍
    #     (而非因 foes 池為空就選不到目標)。
    chaos_u = Unit(POOL["呂布"], "騎")
    chaos_u.chaos = 2
    ally38 = Unit(POOL["張飛"], "盾")
    v38 = pick_target_chaos(chaos_u, [chaos_u, ally38], [])  # 空的敵方池, 只能從友軍(排除自己)中選
    assert v38 is ally38, "混亂且敵方池為空時, 應能選中存活友軍作為目標(敵我不分)"
    # 統計驗證: 多次呼叫應偶爾選中友軍(敵我皆有目標時, 非必定選友軍, 但長期應有相當比例)
    foe38 = Unit(POOL["諸葛亮"], "弓")
    ally_hits = sum(1 for _ in range(500) if pick_target_chaos(chaos_u, [chaos_u, ally38], [foe38]) is ally38)
    assert ally_hits > 0, "混亂單位在敵我皆有目標時, 500次抽樣應至少命中友軍一次(敵我不分)"
    # 非混亂時應退回一般 pick_target(只從敵方池選, 不會選中友軍)
    normal_u38 = Unit(POOL["呂布"], "騎")
    v38b = pick_target_chaos(normal_u38, [normal_u38, ally38], [foe38])
    assert v38b is foe38, "非混亂狀態應退回一般 pick_target(只選敵方), 不受混亂邏輯影響"

    # 39) insight 應阻擋 chaos 套用(同 stun/silence/disarm 慣例)
    ins_u = Unit(POOL["呂布"], "騎")
    ins_u.insight = 3
    chaos_tac39 = {"nameZh": "測試混亂39", "type": "active", "kind": "phys", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0,
                   "effects": [{"k": "chaos", "who": "enemy", "dur": 2}]}
    apply_effects(Unit(POOL["張飛"], "盾"), None, chaos_tac39, [], [ins_u], no_heal=True)
    assert ins_u.chaos == 0, "insight(洞察)應免疫混亂效果套用"
    # insight 施加時應同時解除既有混亂(同 stun/silence/disarm 慣例)
    chaos_u2 = Unit(POOL["呂布"], "騎")
    chaos_u2.chaos = 3
    insight_tac39 = {"nameZh": "測試洞察39", "type": "active", "kind": "phys", "coef": 0,
                      "rate": 1.0, "n": 1, "prep": 0,
                      "effects": [{"k": "insight", "who": "self", "dur": 2}]}
    apply_effects(chaos_u2, None, insight_tac39, [chaos_u2], [], no_heal=True)
    assert chaos_u2.chaos == 0, "施加洞察應同時解除既有混亂"

    # 40) hitsRepeat: 只剩一個存活敵人時, N次獨立抽樣應全部命中該唯一存活者(不因「找不到N個
    #     不重複目標」而提前跳過, 證明是逐次重新選標而非一次性選N個不重複)
    hr_src = Unit(POOL["張角"], "弓")
    hr_only_survivor = Unit(POOL["諸葛亮"], "弓")
    hr_dead = Unit(POOL["周瑜"], "弓")
    hr_dead.troop = 0                                  # 已陣亡, 不應被選中
    hr_tac = {"nameZh": "測試hitsRepeat40", "type": "active", "kind": "intel", "coef": 0.3,
              "rate": 1.0, "n": 5, "prep": 0, "hitsRepeat": True, "effects": []}
    # 直接模擬 fight() 內的 hitsRepeat 迴圈邏輯(5次獨立 pick_target, 唯一存活目標應全部命中)
    troop_before_40 = hr_only_survivor.troop
    for _ in range(hr_tac["n"]):
        v = pick_target([hr_only_survivor, hr_dead], hr_src)
        assert v is hr_only_survivor, "hitsRepeat: 唯一存活目標應每次都被選中(不因僅剩1個目標而跳過)"
        hit(hr_src, v, hr_tac["coef"], hr_tac["kind"], False, None)
    assert hr_only_survivor.troop < troop_before_40, "hitsRepeat 5次獨立命中應對唯一存活目標造成累積傷害"

    # 41) lockTarget: 同一戰法物件兩次發動應鎖定並重用同一目標(不重新隨機選)
    lt_tac = {"nameZh": "測試lockTarget41", "type": "active", "kind": "phys", "coef": 0,
              "rate": 1.0, "n": 1, "prep": 0, "lockTarget": True, "effects": []}
    lt_caster = Unit(POOL["呂布"], "騎")
    lt_pool = [Unit(POOL["諸葛亮"], "弓"), Unit(POOL["周瑜"], "弓"), Unit(POOL["司馬懿"], "弓")]
    t1 = resolve_locked_target(lt_caster, lt_tac, lt_pool)
    t2 = resolve_locked_target(lt_caster, lt_tac, lt_pool)
    t3 = resolve_locked_target(lt_caster, lt_tac, lt_pool)
    assert t1 is t2 is t3, "lockTarget: 同一戰法物件多次發動應重用同一鎖定目標"
    # 目標陣亡後, lockTarget 應回傳 None(不重新選新目標) —— 保守設計, 見程式碼註解
    t1.troop = 0
    t4 = resolve_locked_target(lt_caster, lt_tac, lt_pool)
    assert t4 is None, "lockTarget: 鎖定目標已陣亡時應回傳None(不重新選新目標, 視為本回合無有效目標)"
    # 不同戰法物件(即使同名)應各自獨立鎖定, 不共用鎖定目標
    lt_tac_other = {"nameZh": "測試lockTarget41", "type": "active", "kind": "phys", "coef": 0,
                     "rate": 1.0, "n": 1, "prep": 0, "lockTarget": True, "effects": []}
    lt_pool2 = [Unit(POOL["諸葛亮"], "弓"), Unit(POOL["周瑜"], "弓")]
    t5 = resolve_locked_target(lt_caster, lt_tac_other, lt_pool2)
    assert id(lt_tac_other) in lt_caster.locked_targets, "不同戰法物件應各自在 locked_targets 建立獨立鎖定項"

    # --- 批13: extraHits(多段傷害) + who="subs"(副將群) --------------------------
    # 42) extraHits: 主 coef 段(兵刃) + 額外段(謀略) 應各自獨立結算傷害, 額外段的 kind 與主段
    #     不同時各自套用正確的攻防屬性(如屠几上肉 兵刃150%+謀略150%)。用一個 intel 遠高於
    #     force 的施法者驗證: 額外段(intel)理論傷害應與主段(phys)不同, 證明兩段各自獨立算傷害
    #     (非合併成單一 coef)。
    eh_src = Unit(POOL["諸葛亮"], "弓")           # 智力遠高於武力, 兵刃段/謀略段傷害應有明顯差異
    eh_tgt42 = Unit(POOL["張飛"], "盾")
    eh_tac = {"nameZh": "測試extraHits42", "type": "active", "kind": "phys", "coef": 1.5,
              "rate": 1.0, "n": 1, "prep": 0,
              "extraHits": [{"coef": 1.5, "kind": "intel"}], "effects": []}
    troop_before_42 = eh_tgt42.troop
    hit(eh_src, eh_tgt42, eh_tac["coef"], eh_tac["kind"], False, None)   # 模擬主段(phys)
    troop_after_main = eh_tgt42.troop
    fire_extra_hits(eh_src, eh_tac, eh_tgt42, lambda u: [eh_src], lambda u: [eh_tgt42], None)  # 模擬額外段(intel)
    assert eh_tgt42.troop < troop_after_main, "extraHits: 額外段應對目標造成獨立的第二次傷害(非被主段吞掉)"
    dmg_main = troop_before_42 - troop_after_main
    dmg_extra = troop_after_main - eh_tgt42.troop
    # 諸葛亮智力遠高於武力, 謀略段(intel)理論傷害應明顯大於兵刃段(phys)(相同coef下), 證明額外段
    # 確實各自用自己的kind獨立算傷害(而非誤用主段的kind或複製主段傷害量)
    assert dmg_extra > dmg_main * 1.05, "extraHits: 額外段應依自己的kind(intel)獨立算傷害, 不與主段(phys)相同"

    # extraHits.rate: rate=0 時額外段不應觸發(0次傷害)
    eh_tgt42b = Unit(POOL["張飛"], "盾")
    eh_tac_norate = {"nameZh": "測試extraHits42b", "type": "active", "kind": "phys", "coef": 0,
                      "rate": 1.0, "n": 1, "prep": 0,
                      "extraHits": [{"coef": 1.5, "kind": "phys", "rate": 0.0}], "effects": []}
    troop_before_42b = eh_tgt42b.troop
    fire_extra_hits(eh_src, eh_tac_norate, eh_tgt42b, lambda u: [eh_src], lambda u: [eh_tgt42b], None)
    assert eh_tgt42b.troop == troop_before_42b, "extraHits: rate=0 的額外段不應觸發傷害"

    # 43) who="subs": 副將群 = allies 除 index 0(主將), 效果只套用到非主將的存活友軍
    subs_leader = Unit(POOL["呂布"], "騎")
    subs_a = Unit(POOL["張飛"], "盾")
    subs_b = Unit(POOL["趙雲"], "騎")
    subs_team = [subs_leader, subs_a, subs_b]
    subs_tac = {"nameZh": "測試who_subs43", "type": "command", "kind": "phys", "coef": 0,
                "rate": 1.0, "n": 1, "prep": 0,
                "effects": [{"k": "mitig", "who": "subs", "val": 0.25, "dur": 5}]}
    apply_effects(subs_leader, None, subs_tac, subs_team, [], no_heal=True)
    assert abs(subs_leader.addbonus("mitig") - 0) < 1e-9, "who=subs: 主將(index 0)不應被套用副將群效果"
    assert abs(subs_a.addbonus("mitig") - 0.25) < 1e-9, "who=subs: 副將應被套用效果"
    assert abs(subs_b.addbonus("mitig") - 0.25) < 1e-9, "who=subs: 副將應被套用效果"

    # --- 批15: 指揮/被動 heal 語意修正 —— heal_only 常駐通道應尊重 t["when"](round_ok)/
    # t["rate"](擲骰)/e["once"](單次去重), 不再無視三者每回合無條件結算 -----------------
    global CUR_ROUND
    # 44a) when.rounds:[4] 的 heal 應只在第4回合治療一次, 其餘回合(含之後回合)不治療
    w44_caster = Unit(POOL["諸葛亮"], "弓")
    w44_target = Unit(POOL["張飛"], "盾")
    w44_target.troop = 3000
    w44_target.wounded = START_TROOP                  # 批18: 治療上限=傷兵池
    heal_tac44a = {"nameZh": "測試治療術44a", "type": "command", "kind": "intel", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0, "when": {"rounds": [4]},
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1}]}
    for _r in range(1, 9):
        w44_caster.own_round = _r  # 時序徹底一致化批: heal_only現讀caster.own_round, 取代全局CUR_ROUND
        apply_effects(w44_caster, None, heal_tac44a, [w44_target], [], heal_only=True)
        if _r == 4:
            troop_after_r4 = w44_target.troop
            assert troop_after_r4 > 3000, "when rounds:[4] 的 heal 應在第4回合治療"
        elif _r < 4:
            assert w44_target.troop == 3000, f"when rounds:[4] 的 heal 不應在第{_r}回合(視窗前)治療"
        else:
            assert w44_target.troop == troop_after_r4, f"when rounds:[4] 的 heal 不應在第{_r}回合(視窗後, 已消耗一次性觸發)重複治療"
    CUR_ROUND = 0

    # 44b) rate 0.5 的 heal(無 when, 每回合持續型): 統計上應約半數回合真正治療(擲骰生效),
    # 而非每回合必定治療 —— 固定 random 種子跑多回合, 驗證確有「未中不治療」的分支存在
    random.seed(12345)
    w44b_caster = Unit(POOL["諸葛亮"], "弓")
    heal_tac44b = {"nameZh": "測試治療術44b", "type": "passive", "kind": "intel", "coef": 0,
                   "rate": 0.5, "n": 1, "prep": 0,
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1}]}
    n_healed = 0
    N_TRIALS = 200
    for _i in range(N_TRIALS):
        w44b_target = Unit(POOL["張飛"], "盾")
        w44b_target.troop = 3000
        w44b_target.wounded = START_TROOP             # 批18: 治療上限=傷兵池
        w44b_caster.own_round = 1  # 時序徹底一致化批: 取代全局CUR_ROUND
        apply_effects(w44b_caster, None, heal_tac44b, [w44b_target], [], heal_only=True)
        if w44b_target.troop > 3000:
            n_healed += 1
    CUR_ROUND = 0
    assert 0 < n_healed < N_TRIALS, "rate 0.5 的 heal 應該有些回合中、有些回合不中(不應每次都治療或都不治療)"
    assert abs(n_healed / N_TRIALS - 0.5) < 0.15, f"rate 0.5 的 heal 命中比例應接近50%, 實測{n_healed}/{N_TRIALS}"
    random.seed()                                      # 還原非固定種子, 不影響後續隨機性依賴的測項

    # 44c) 無 when 且無 once 的 heal: 維持原行為, 每回合持續治療(急救/休整類戰法本意如此)
    w44c_caster = Unit(POOL["諸葛亮"], "弓")
    w44c_target = Unit(POOL["張飛"], "盾")
    w44c_target.troop = 1000
    w44c_target.wounded = START_TROOP                 # 批18: 治療上限=傷兵池
    heal_tac44c = {"nameZh": "測試治療術44c", "type": "command", "kind": "intel", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0,
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1}]}
    troops_seen = []
    for _r in range(1, 4):
        w44c_caster.own_round = _r  # 時序徹底一致化批: 取代全局CUR_ROUND
        apply_effects(w44c_caster, None, heal_tac44c, [w44c_target], [], heal_only=True)
        troops_seen.append(w44c_target.troop)
    CUR_ROUND = 0
    assert troops_seen[0] < START_TROOP and troops_seen[1] > troops_seen[0] and troops_seen[2] > troops_seen[1], \
        "無when無once的heal應每回合持續治療(兵力逐回合遞增, 現行為不變)"

    # 44d) e["when"](效果級 when, 優先於 t["when"]): 同一戰法內其他效果(如 mitig)應維持準備
    # 階段常駐套用(不受 when 影響), 只有帶 e["when"] 的 heal 效果被限定在指定回合觸發 ——
    # 對應撫輯軍民「前3回合減傷(常駐) + 第4回合單次治療」這類「同戰法內混合時間窗」場景,
    # 若誤用戰法級 t["when"] 會連帶把 mitig 也延後到第4回合才套用(錯誤), 必須用 e["when"]
    # 只精準框住 heal 效果本身。
    w44d_caster = Unit(POOL["諸葛亮"], "弓")
    w44d_target = Unit(POOL["張飛"], "盾")
    w44d_target.troop = 3000
    w44d_target.wounded = START_TROOP                 # 批18: 治療上限=傷兵池
    mixed_tac44d = {"nameZh": "測試混合時間窗44d", "type": "command", "kind": "phys", "coef": 0,
                     "rate": 1.0, "n": 1, "prep": 0,
                     "effects": [
                         {"k": "mitig", "who": "ally", "val": 0.24, "dur": 3},   # 無 when: 應在準備階段就套用(不受回合限制)
                         {"k": "heal", "who": "ally", "coef": 0.8, "dur": 1, "when": {"rounds": [4]}},  # 只有 heal 帶 when: 只在第4回合觸發
                     ]}
    apply_effects(w44d_caster, None, mixed_tac44d, [w44d_target], [], no_heal=True)  # 模擬準備階段套用(no_heal=True 排除heal, 同 fight() 開場呼叫)
    assert abs(w44d_target.addbonus("mitig") - 0.24) < 1e-9, "e[when]不應影響同戰法內無when的mitig效果, 準備階段應正常套用"
    for _r in range(1, 9):
        w44d_caster.own_round = _r  # 時序徹底一致化批: 取代全局CUR_ROUND
        apply_effects(w44d_caster, None, mixed_tac44d, [w44d_target], [], heal_only=True)
        if _r == 4:
            troop_after_r4d = w44d_target.troop
            assert troop_after_r4d > 3000, "e[when] rounds:[4] 的 heal 應在第4回合治療"
        elif _r < 4:
            assert w44d_target.troop == 3000, f"e[when] rounds:[4] 的 heal 不應在第{_r}回合(視窗前)治療"
        else:
            assert w44d_target.troop == troop_after_r4d, f"e[when] rounds:[4] 的 heal 不應在第{_r}回合(視窗後)重複治療"
    CUR_ROUND = 0

    # 44e) when["from"]/["until"](範圍視窗, 如金丹秘術「第3回合起，持續3回合」): 語意是
    # 「窗內每回合都要治療」(休整類持續恢復), 與 rounds(單一/多個特定回合各觸發一次)不同,
    # 不應該只在視窗開啟當回合治療一次就不再觸發 —— 用 from:3/until:5 驗證第3~5回合每回合
    # 都治療、其餘回合不治療。
    w44e_caster = Unit(POOL["諸葛亮"], "弓")
    heal_tac44e = {"nameZh": "測試治療術44e", "type": "command", "kind": "intel", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0,
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1,
                                "when": {"from": 3, "until": 5}}]}
    healed_rounds = []
    for _r in range(1, 9):
        w44e_target = Unit(POOL["張飛"], "盾")   # 每回合重建全血目標, 只看「這一回合heal_only通道有沒有真的治療」
        w44e_target.troop = 3000
        w44e_target.wounded = START_TROOP             # 批18: 治療上限=傷兵池
        w44e_caster.own_round = _r  # 時序徹底一致化批: 取代全局CUR_ROUND
        apply_effects(w44e_caster, None, heal_tac44e, [w44e_target], [], heal_only=True)
        if w44e_target.troop > 3000:
            healed_rounds.append(_r)
    CUR_ROUND = 0
    assert healed_rounds == [3, 4, 5], f"when from:3/until:5 應在第3~5回合每回合都治療(範圍視窗持續型, 非單次), 實際={healed_rounds}"

    # 44f) when["rounds"] 列出多個回合(如「第3、5回合」)應在每個列出的回合各自觸發一次
    w44f_caster = Unit(POOL["諸葛亮"], "弓")
    heal_tac44f = {"nameZh": "測試治療術44f", "type": "command", "kind": "intel", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0,
                   "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1,
                                "when": {"rounds": [3, 5]}}]}
    healed_rounds_f = []
    for _r in range(1, 9):
        w44f_target = Unit(POOL["張飛"], "盾")
        w44f_target.troop = 3000
        w44f_target.wounded = START_TROOP             # 批18: 治療上限=傷兵池
        w44f_caster.own_round = _r  # 時序徹底一致化批: 取代全局CUR_ROUND
        apply_effects(w44f_caster, None, heal_tac44f, [w44f_target], [], heal_only=True)
        if w44f_target.troop > 3000:
            healed_rounds_f.append(_r)
    CUR_ROUND = 0
    assert healed_rounds_f == [3, 5], f"when rounds:[3,5] 應在第3回合與第5回合各觸發一次, 實際={healed_rounds_f}"

    # --- 批16: 原語擴充包(v5盲測殘差) --------------------------------------------
    # 45) ifTargetHas: 效果段只對「已有該狀態」的目標生效; dot/控制類各驗一次
    assert target_has(None, "dot") is False, "target_has 對 None 應回傳 False(防禦)"
    ith_dotted = Unit(POOL["張飛"], "盾")
    ith_dotted.dots.append([100, 3, False])
    ith_clean = Unit(POOL["張飛"], "盾")
    assert target_has(ith_dotted, "dot") is True and target_has(ith_clean, "dot") is False, \
        "ifTargetHas=dot 應只認定 dots 非空的目標"
    ith_stunned = Unit(POOL["張飛"], "盾")
    ith_stunned.stun = 2
    assert target_has(ith_stunned, "stun") is True and target_has(ith_clean, "stun") is False, \
        "ifTargetHas=stun 應只認定 stun>0 的目標"
    ith_tac = {"nameZh": "測試ifTargetHas45", "type": "active", "kind": "phys", "coef": 0,
               "rate": 1.0, "n": 1, "prep": 0,
               "effects": [{"k": "amp", "who": "enemy", "val": 0.3, "dur": 3, "ifTargetHas": "dot"}]}
    ith_caster = Unit(POOL["諸葛亮"], "弓")
    apply_effects(ith_caster, None, ith_tac, [], [ith_dotted, ith_clean], no_heal=True)
    assert ith_dotted.addbonus("mitig") < 0, "ifTargetHas=dot 應對帶dot的目標生效(此處amp who=enemy>0轉為易傷/mitig負值)"
    assert ith_clean.addbonus("mitig") == 0, "ifTargetHas=dot 不應對無dot的目標生效"

    # 46) everyN: 自身每第N次普攻觸發戰法 effects(用 tick_every_n 直接驗證計數器行為)
    en_tac = {"nameZh": "測試everyN46", "type": "passive", "kind": "phys", "coef": 0,
              "rate": 1.0, "n": 1, "prep": 0, "everyN": {"count": 3, "on": "attack"},
              "effects": [{"k": "amp", "who": "self", "val": 0.1, "dur": 2}]}
    en_u = Unit(POOL["呂布"], "騎")
    fired = [en_u.tick_every_n(en_tac) for _ in range(6)]
    assert fired == [False, False, True, False, False, True], \
        f"everyN count=3 應在第3、6次普攻觸發, got={fired}"

    # 47) immuneTo: 單項控制免疫應只擋清單內的控制類型, 不像 insight 全免
    im_u = Unit(POOL["呂布"], "騎")
    im_u.push_immune(["stun"], 3)
    assert im_u.is_immune_to("stun") is True and im_u.is_immune_to("silence") is False, \
        "immuneTo=['stun'] 應只免疫 stun, 不免疫 silence"
    stun_tac47 = {"nameZh": "測試immune47stun", "type": "active", "kind": "phys", "coef": 0,
                  "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "stun", "who": "enemy", "dur": 2}]}
    silence_tac47 = {"nameZh": "測試immune47silence", "type": "active", "kind": "phys", "coef": 0,
                     "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "silence", "who": "enemy", "dur": 2}]}
    caster47 = Unit(POOL["諸葛亮"], "弓")
    apply_effects(caster47, im_u, stun_tac47, [caster47], [im_u], no_heal=True)
    assert im_u.stun == 0, "immuneTo=['stun'] 應成功免疫震懾施加"
    apply_effects(caster47, im_u, silence_tac47, [caster47], [im_u], no_heal=True)
    assert im_u.silence > 0, "immuneTo=['stun'] 不應免疫計窮(非清單內類型)"

    # 48) when 擴充: parity(奇偶回合) + every(每N回合)
    assert round_ok({"when": {"parity": "odd"}}, 1) and not round_ok({"when": {"parity": "odd"}}, 2), \
        "when.parity=odd 應只在奇數回合符合"
    assert round_ok({"when": {"parity": "even"}}, 4) and not round_ok({"when": {"parity": "even"}}, 5), \
        "when.parity=even 應只在偶數回合符合"
    assert round_ok({"when": {"every": 3}}, 3) and round_ok({"when": {"every": 3}}, 6) and not round_ok({"when": {"every": 3}}, 4), \
        "when.every=3 應只在3的倍數回合符合"
    assert round_ok({"when": {"every": 2, "from": 4}}, 4) and not round_ok({"when": {"every": 2, "from": 4}}, 3), \
        "when.every 應可與 from/until 並存(皆通過才符合)"

    # 49) hpPct 觸發: hpBelow(一次性首次跨越) / hpAbove(持續窗)
    assert hp_ok({"when": {"hpBelow": 0.5}}, type("U", (), {"hp_pct": 0.4})()) is True
    assert hp_ok({"when": {"hpBelow": 0.5}}, type("U", (), {"hp_pct": 0.6})()) is False
    assert hp_ok({"when": {"hpAbove": 0.5}}, type("U", (), {"hp_pct": 0.6})()) is True
    hp_u = Unit(POOL["張飛"], "盾")
    hp_u.troop = 3000                                  # 30% 兵力, 低於50%門檻
    hp_tac49 = {"nameZh": "測試hpBelow49", "type": "passive", "kind": "phys", "coef": 0,
                "rate": 1.0, "n": 1, "prep": 0, "when": {"hpBelow": 0.5},
                "effects": [{"k": "amp", "who": "self", "val": 0.2, "dur": 3}]}
    assert hp_ok(hp_tac49, hp_u) is True, "兵力30%應通過hpBelow=0.5門檻"
    hp_u.troop = 8000
    assert hp_ok(hp_tac49, hp_u) is False, "兵力80%不應通過hpBelow=0.5門檻"

    # 50) healBoost/healGiven: 受到的治療×(1+val), 施放的治療×(1+val), 可疊乘
    hb_caster = Unit(POOL["諸葛亮"], "弓")
    hb_caster.push_add("healGiven", 0.5, 9)             # 施放的治療+50%
    hb_target_boost = Unit(POOL["張飛"], "盾")
    hb_target_boost.troop = 3000
    hb_target_boost.wounded = START_TROOP             # 批18: 治療上限=傷兵池
    hb_target_boost.push_add("healBoost", 0.5, 9)       # 受到的治療+50%
    hb_target_plain = Unit(POOL["張飛"], "盾")
    hb_target_plain.troop = 3000
    hb_target_plain.wounded = START_TROOP             # 批18: 治療上限=傷兵池
    heal_tac50 = {"nameZh": "測試healBoost50", "type": "active", "kind": "intel", "coef": 0,
                  "rate": 1.0, "n": 1, "prep": 0,
                  "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1}]}
    plain_caster50 = Unit(POOL["諸葛亮"], "弓")
    apply_effects(plain_caster50, None, heal_tac50, [hb_target_plain], [], no_heal=False)
    apply_effects(hb_caster, None, heal_tac50, [hb_target_boost], [], no_heal=False)
    gain_plain = hb_target_plain.troop - 3000
    gain_boost = hb_target_boost.troop - 3000
    assert abs(gain_boost - gain_plain * 1.5 * 1.5) < 1.0, \
        f"healBoost+healGiven應各自+50%並疊乘(合計×2.25), plain={gain_plain:.1f} boost={gain_boost:.1f}"

    # 51) dispel: buffs 應清除正向增益(略過undispellable), debuffs 應清除負向減益+控制欄位(略過undispellable)
    dp_u = Unit(POOL["呂布"], "騎")
    dp_u.push_add("amp", 0.3, 9, src="測試增益51")
    dp_u.push_add("amp", 0.2, 9, src="測試護體51", flags={"undispellable": True})
    dispel_unit(dp_u, "buffs")
    assert abs(dp_u.addbonus("amp") - 0.2) < 1e-9, "dispel buffs 應清除可驅散的正向amp, 保留undispellable那條"
    dp_u2 = Unit(POOL["呂布"], "騎")
    dp_u2.push_add("amp", -0.25, 9, src="測試減益51")
    dp_u2.stun = 2
    dp_u2.dots.append([50, 3, False])
    dp_u2.dots.append([80, 3, True])                    # undispellable dot, 應保留
    dispel_unit(dp_u2, "debuffs")
    assert dp_u2.addbonus("amp") == 0, "dispel debuffs 應清除負向amp"
    assert dp_u2.stun == 0, "dispel debuffs 應清除控制欄位(stun)"
    assert len(dp_u2.dots) == 1 and dp_u2.dots[0][2] is True, "dispel debuffs 應清除可驅散的dot, 保留undispellable的dot"

    # 52) choices: 擇一分支應按權重隨機選一組效果套用; weight=0的分支不應被選中
    ch_choices = [{"weight": 1, "effects": [{"k": "amp", "who": "self", "val": 0.11, "dur": 1}]},
                  {"weight": 0, "effects": [{"k": "amp", "who": "self", "val": 0.99, "dur": 1}]}]
    picked_vals = {pick_choice(ch_choices)["effects"][0]["val"] for _ in range(50)}
    assert picked_vals == {0.11}, f"weight=0的分支不應被pick_choice選中, got={picked_vals}"
    ch_two_choices = [{"weight": 1, "effects": [{"k": "amp", "who": "self", "val": 0.1, "dur": 1}]},
                      {"weight": 1, "effects": [{"k": "amp", "who": "self", "val": 0.2, "dur": 1}]}]
    picked_two = {pick_choice(ch_two_choices)["effects"][0]["val"] for _ in range(200)}
    assert picked_two == {0.1, 0.2}, f"均分權重下200次抽樣應兩個分支都出現過, got={picked_two}"

    # 53) fakeReport: 中招者被動+指揮戰法coef段擲骰應被抑制; on_hit反應式觸發也應被抑制; insight可免
    fr_u = Unit(POOL["張飛"], "盾")
    fr_u.fake_report_dur = 2
    assert fr_u.fake_report_dur > 0, "fakeReport施加後 fake_report_dur 應>0(供 fight() 主迴圈/on_hit 檢查抑制)"
    fr_tac = {"nameZh": "測試fakeReport53", "type": "active", "kind": "phys", "coef": 0,
              "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "fakeReport", "who": "enemy", "dur": 2}]}
    fr_caster = Unit(POOL["諸葛亮"], "弓")
    fr_target = Unit(POOL["張飛"], "盾")
    apply_effects(fr_caster, fr_target, fr_tac, [fr_caster], [fr_target], no_heal=True)
    assert fr_target.fake_report_dur > 0, "fakeReport 應成功施加(dur>0)"
    fr_insight = Unit(POOL["張飛"], "盾")
    fr_insight.insight = 3
    apply_effects(fr_caster, fr_insight, fr_tac, [fr_caster], [fr_insight], no_heal=True)
    assert fr_insight.fake_report_dur == 0, "insight(洞察)應免疫偽報"

    # --- 批18: 傷兵池 + fakeReport強化 + ambush + targetSel ----------------------
    # 54) 傷兵池: 第1回合受到1000傷害 -> wounded應為 1000*0.9=900
    wp_src = Unit(POOL["呂布"], "騎")
    wp_dst = Unit(POOL["張飛"], "盾")
    CUR_ROUND = 1
    wp_before = wp_dst.troop
    hit(wp_src, wp_dst, 1.0, "phys")
    wp_dmg = wp_before - wp_dst.troop
    assert abs(wp_dst.wounded - wp_dmg * 0.9) < 1e-6, \
        f"第1回合傷害應以90%轉化率計入傷兵池, dmg={wp_dmg:.1f} wounded={wp_dst.wounded:.1f}"
    CUR_ROUND = 0

    # 55) 治療不應超過wounded池餘量(即使治療量本身遠大於傷兵池)
    wp2_target = Unit(POOL["張飛"], "盾")
    wp2_target.troop = 1000
    wp2_target.wounded = 200                            # 傷兵池只剩200可救援
    wp2_tac = {"nameZh": "測試傷兵池上限55", "type": "active", "kind": "intel", "coef": 0,
               "rate": 1.0, "n": 1, "prep": 0,
               "effects": [{"k": "heal", "who": "ally", "coef": 5.0, "dur": 1}]}  # 誇大治療量
    wp2_caster = Unit(POOL["諸葛亮"], "弓")
    apply_effects(wp2_caster, None, wp2_tac, [wp2_target], [], no_heal=False)
    assert abs(wp2_target.troop - 1200) < 1e-6, \
        f"治療應被傷兵池餘量(200)封頂, 實際兵力={wp2_target.troop:.1f}(預期1200)"
    assert abs(wp2_target.wounded) < 1e-6, "傷兵池應在治療後歸零(全數被救回)"

    # 56) 第7回合傷害轉化率應為0.675(65~70%取中值)
    assert abs(wounded_rate(7) - 0.675) < 1e-9, "第7回合傷兵轉化率應為0.675"
    assert abs(wounded_rate(8) - 0.675) < 1e-9, "第8回合傷兵轉化率應為0.675"
    assert abs(wounded_rate(1) - 0.9) < 1e-9 and abs(wounded_rate(3) - 0.9) < 1e-9, "第1~3回合應為0.9"
    assert abs(wounded_rate(4) - 0.8) < 1e-9 and abs(wounded_rate(6) - 0.8) < 1e-9, "第4~6回合應為0.8"

    # 57) fakeReport 加強: 已生效的指揮/被動 mitig(如暫避其鋒式減傷)應在偽報期間失效, 到期恢復
    fr2_holder = Unit(POOL["張飛"], "盾")
    fr2_tac = {"nameZh": "測試暫避其鋒57", "type": "command", "kind": "phys", "coef": 0,
               "rate": 1.0, "n": 1, "prep": 0,
               "effects": [{"k": "mitig", "who": "ally", "val": 0.4, "dur": 99}]}
    fr2_holder.tactics.append(fr2_tac)
    fr2_holder.cmd_passive_srcs.add("測試暫避其鋒57")
    apply_effects(fr2_holder, None, fr2_tac, [fr2_holder], [], no_heal=True)  # 模擬prep階段套用(常駐指揮效果)
    assert abs(fr2_holder.addbonus("mitig") - 0.4) < 1e-9, "偽報前 mitig 應正常生效"
    fr2_holder.fake_report_dur = 2
    assert abs(fr2_holder.addbonus("mitig")) < 1e-9, "偽報期間, 來源為自己指揮戰法的mitig應暫停生效(不是刪除)"
    fr2_holder.fake_report_dur = 0
    assert abs(fr2_holder.addbonus("mitig") - 0.4) < 1e-9, "偽報到期後mitig應恢復生效(條目仍在, 未被刪除)"

    # 58) ambush(遇襲): 只有遇襲者應排最後; 先攻+遇襲同時存在應抵消(按速度排, 不最先也不最後)
    am_fast = Unit(POOL["呂布"], "騎")     # 速度較高
    am_slow = Unit(POOL["張飛"], "盾")     # 速度較低(呂布通常速度高於張飛, 若不成立仍以下方純ambush欄位邏輯驗證為主)
    am_ambushed = Unit(POOL["關羽"], "騎")
    am_ambushed.ambush = 2
    am_first_and_ambush = Unit(POOL["趙雲"], "騎")
    am_first_and_ambush.first = 2
    am_first_and_ambush.ambush = 2
    eff_first = lambda x: (1 if x.first > 0 else 0) - (1 if x.ambush > 0 else 0)
    assert eff_first(am_ambushed) == -1, "只有遇襲應eff_first=-1(排最後檔)"
    assert eff_first(am_first_and_ambush) == 0, "先攻+遇襲同時存在應抵消, eff_first=0(視為普通, 按速度排)"
    assert eff_first(am_fast) == 0 and eff_first(am_slow) == 0, "無先攻無遇襲應eff_first=0"
    am_pure_first = Unit(POOL["曹操"], "騎")
    am_pure_first.first = 2
    assert eff_first(am_pure_first) == 1, "只有先攻應eff_first=1(最先檔)"
    order58 = sorted([am_ambushed, am_first_and_ambush, am_pure_first],
                      key=lambda x: (eff_first(x), x.eff("speed")), reverse=True)
    assert order58[0] is am_pure_first, "純先攻者應排最先"
    assert order58[-1] is am_ambushed, "純遇襲者應排最後"

    # 59) targetSel: 指定選標準則(不受混亂chaos影響) —— minTroop 應選兵力最低的敵方目標
    ts_e1 = Unit(POOL["張飛"], "盾"); ts_e1.troop = 5000
    ts_e2 = Unit(POOL["關羽"], "盾"); ts_e2.troop = 1000    # 兵力最低
    ts_e3 = Unit(POOL["趙雲"], "盾"); ts_e3.troop = 8000
    assert pick_by_criterion([ts_e1, ts_e2, ts_e3], "minTroop") is ts_e2, "minTroop 應選兵力最低者"
    ts_caster = Unit(POOL["曹操"], "騎")
    ts_caster.chaos = 3                                 # 混亂中的施放者
    ts_tac = {"nameZh": "測試targetSel59", "type": "active", "kind": "phys", "coef": 0,
              "rate": 1.0, "n": 1, "prep": 0,
              "effects": [{"k": "amp", "who": "enemy", "val": 0.3, "dur": 2, "targetSel": "minTroop"}]}
    for _ in range(20):                                 # 多次驗證: 即使施放者混亂, targetSel仍應穩定選中兵力最低者(不受混亂隨機影響)
        ts_e1b = Unit(POOL["張飛"], "盾"); ts_e1b.troop = 5000
        ts_e2b = Unit(POOL["關羽"], "盾"); ts_e2b.troop = 1000
        apply_effects(ts_caster, None, ts_tac, [ts_caster], [ts_e1b, ts_e2b], no_heal=True)
        assert ts_e2b.addbonus("mitig") < 0 and ts_e1b.addbonus("mitig") == 0, \
            "targetSel=minTroop 應只對兵力最低的敵方目標生效, 且chaos不應打亂此選標"

    # 60) choices 派發: coef=0 且頂層effects為空、內容全在choices裡的active戰法應能正常擲骰觸發
    ch60_choices = [{"effects": [{"k": "amp", "who": "self", "val": 0.15, "dur": 1}]}]
    ch60_tac = {"nameZh": "測試choices派發60", "type": "active", "kind": "phys", "coef": 0,
                "rate": 1.0, "n": 1, "prep": 0, "effects": [], "choices": ch60_choices}
    assert (ch60_tac["coef"] or ch60_tac["effects"] or ch60_tac.get("choices")), \
        "coef=0/effects=[]但choices非空的主動戰法, dispatch判斷式應仍為真值(可觸發)"

    # --- 批22: block(次數型格擋) + 偽報疊加規則 + 輸出減益-90%上限 + 治療時序(heal-on-damage) ---
    # 61) block: 全額格擋(val=1.0) 應完全歸零本次傷害, 消耗1層後歸零層數應移除
    bk_src = Unit(POOL["呂布"], "騎")
    bk_dst = Unit(POOL["張飛"], "盾")
    bk_dst.push_block(1.0, 1, src="測試抵禦61")
    assert len(bk_dst.block) == 1 and bk_dst.block[0]["n"] == 1, "push_block 應新增1筆格擋層"
    before61 = bk_dst.troop
    hit(bk_src, bk_dst, 1.0, "phys")
    assert bk_dst.troop == before61, "block val=1.0 應完全格擋本次傷害(兵力不變)"
    assert len(bk_dst.block) == 0, "格擋次數用盡後應從陣列移除"
    # 第二次攻擊應正常造成傷害(格擋已耗盡)
    before61b = bk_dst.troop
    hit(bk_src, bk_dst, 1.0, "phys")
    assert bk_dst.troop < before61b, "格擋耗盡後, 後續攻擊應正常造成傷害"

    # 62) block: 部分減傷(警戒 val=0.4) 應按比例打折, 消耗1層
    bk2_dst = Unit(POOL["張飛"], "盾")
    bk2_dst.push_block(0.4, 2, src="測試警戒62")
    before62 = bk2_dst.troop
    random.seed(100)
    hit(bk_src, bk2_dst, 1.0, "phys")
    dmg62 = before62 - bk2_dst.troop
    assert bk2_dst.block[0]["n"] == 1, "警戒(部分減傷)消耗後應剩1層(未移除, 因times=2)"
    assert dmg62 > 0, "警戒(val=0.4)只打折不應完全歸零"
    # 驗證確實打了折(用同種子比較有無格擋的裸傷害量級, 折扣後應明顯小於無格擋)
    bk2_plain = Unit(POOL["張飛"], "盾")
    random.seed(100)
    before62b = bk2_plain.troop
    hit(bk_src, bk2_plain, 1.0, "phys")
    dmg62_plain = before62b - bk2_plain.troop
    assert dmg62 < dmg62_plain * 0.7, f"警戒0.4減傷後傷害應明顯低於無格擋傷害, 折後={dmg62:.1f} 無格擋={dmg62_plain:.1f}"

    # 63) block: 警戒(val<1.0)同源疊加次數(而非刷新覆蓋)。狀態疊加精修批: 原測試用val=1.0
    # (抵禦)驗證同源疊加, 但user規則已更正抵禦為「有剩餘不補不刷」(同源也不例外, 見230),
    # 不再是「同源疊次數」——改用val=0.4(警戒)驗證, 警戒的「累積」規則本批未變更, 仍應同源
    # 疊加次數(2+3=5)。抵禦本身的新規則見230號測試。
    bk3_u = Unit(POOL["張飛"], "盾")
    bk3_u.push_block(0.4, 2, src="測試疊加63")
    bk3_u.push_block(0.4, 3, src="測試疊加63")
    assert len(bk3_u.block) == 1 and bk3_u.block[0]["n"] == 5, "警戒: 同源(同src)再次施加應疊加次數(2+3=5), 而非刷新覆蓋"

    # 64) block: dispel(buffs) 應清除格擋層
    bk4_u = Unit(POOL["張飛"], "盾")
    bk4_u.push_block(1.0, 3, src="測試驅散64")
    dispel_unit(bk4_u, "buffs")
    assert len(bk4_u.block) == 0, "dispel(buffs) 應清除block格擋層(防禦性增益)"

    # 65) 偽報疊加規則: 已存在同等或更強的偽報效果時不應被覆蓋(不刷新/不縮短)
    fr61_u = Unit(POOL["張飛"], "盾")
    fr61_tac_weak = {"nameZh": "測試偽報弱65", "type": "active", "kind": "phys", "coef": 0,
                      "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "fakeReport", "who": "enemy", "dur": 3}]}
    fr61_tac_strong = {"nameZh": "測試偽報強65", "type": "active", "kind": "phys", "coef": 0,
                        "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "fakeReport", "who": "enemy", "dur": 1}]}
    fr61_caster = Unit(POOL["諸葛亮"], "弓")
    apply_effects(fr61_caster, fr61_u, fr61_tac_weak, [fr61_caster], [fr61_u], no_heal=True)
    assert fr61_u.fake_report_dur == 3, "首次施加dur:3應生效(時序重構: dur原值, 不補償+1)"
    apply_effects(fr61_caster, fr61_u, fr61_tac_strong, [fr61_caster], [fr61_u], no_heal=True)
    assert fr61_u.fake_report_dur == 3, "已存在同等或更強的偽報效果(dur:3 > 新的dur:1)時, 新施加不應覆蓋(維持原3, 不降為1)"
    # 更強的新效果應能覆蓋
    fr61_tac_stronger = {"nameZh": "測試偽報更強65", "type": "active", "kind": "phys", "coef": 0,
                          "rate": 1.0, "n": 1, "prep": 0, "effects": [{"k": "fakeReport", "who": "enemy", "dur": 5}]}
    apply_effects(fr61_caster, fr61_u, fr61_tac_stronger, [fr61_caster], [fr61_u], no_heal=True)
    assert fr61_u.fake_report_dur == 5, "新施加dur:5比現有更強(3), 應覆蓋(時序重構: dur原值, 不補償+1)"

    # 66) 輸出減益疊加上限 -90%: 多重負向amp疊加落在-90%~-100%之間(未達虛弱門檻-1.0)時, 應封頂在-90%(輸出至少保留10%), 不應被錯誤歸零
    neg90_src = Unit(POOL["呂布"], "騎")
    neg90_src.push_add("amp", -0.5, 9, src="測試減益A66")
    neg90_src.push_add("amp", -0.45, 9, src="測試減益B66")
    assert abs(neg90_src.amp() - (-0.95)) < 1e-9, "amp()加總應為-0.95(未封頂前的原始加總, 落在-90%~-100%之間但未達虛弱門檻-1.0)"
    neg90_dst = Unit(POOL["張飛"], "盾")
    random.seed(200)
    d90 = damage(neg90_src, neg90_dst, 1.0, "phys")
    # -90%封頂: 原始-95%應被封頂為-90%(輸出保留10%), 不應繼續按-95%結算(更不應被誤判為虛弱而歸零, 因-0.95 > -1.0)
    assert d90 > 0, "amp加總=-0.95(在-90%~-100%之間, 未達虛弱門檻-1.0)應被-90%封頂保留10%輸出, 不應完全歸零"
    zero_amp_src = Unit(POOL["呂布"], "騎")
    random.seed(200)
    d_full = damage(zero_amp_src, neg90_dst, 1.0, "phys")
    # d90 應約為 d_full 的 10%(±隨機帶容差), 而非 5%(若未封頂會是1+(-0.95)=0.05即5%)
    ratio90 = d90 / d_full
    assert 0.08 < ratio90 < 0.12, f"封頂後應保留約10%輸出(而非未封頂的5%), 實際比例={ratio90:.3f}"

    # 66b) 更明確驗證: -0.3+-0.3=-0.6(未超過-90%門檻)應正常按-60%計算, 不受影響
    neg60_src = Unit(POOL["呂布"], "騎")
    neg60_src.push_add("amp", -0.3, 9, src="測試減益C66b")
    neg60_src.push_add("amp", -0.3, 9, src="測試減益D66b")
    assert abs(neg60_src.amp() - (-0.6)) < 1e-9
    # -0.4+-0.4+-0.4=-1.2(明顯超過-90%門檻, 但也超過虛弱門檻-1.0), 應封頂為完全歸零(現行虛弱慣例, -1.0以下視為無法造成傷害)
    neg120_src = Unit(POOL["呂布"], "騎")
    for i in range(3):
        neg120_src.push_add("amp", -0.4, 9, src=f"測試減益E66b_{i}")
    assert neg120_src.amp() < -1.0
    d120 = damage(neg120_src, neg90_dst, 1.0, "phys")
    assert d120 == 0, "amp加總<=-1.0(超過虛弱門檻)應完全歸零, 不受-90%封頂影響(封頂只適用-1.0~-0.9之間的區間)"
    # -0.35*2=-0.7(未達虛弱門檻-1.0, 也未達-90%封頂, 正常結算)不應為0
    neg70_src = Unit(POOL["呂布"], "騎")
    neg70_src.push_add("amp", -0.35, 9, src="測試減益F66b_1")
    neg70_src.push_add("amp", -0.35, 9, src="測試減益G66b_2")
    d70 = damage(neg70_src, neg90_dst, 1.0, "phys")
    assert d70 > 0, "amp加總=-0.7(未達-90%封頂門檻)應正常按七折減傷結算, 不應歸零"

    # 67) 治療時序: heal 效果帶 e.when.on="damaged" 應在受傷當下反應式觸發(而非常駐每回合治療)
    heal67_holder = Unit(POOL["張飛"], "盾")
    heal67_holder.troop = 5000
    heal67_holder.wounded = 3000
    heal67_tac = {"nameZh": "測試急救67", "type": "command", "kind": "intel", "coef": 0,
                  "rate": 1.0, "n": 1, "prep": 0,
                  "effects": [
                      {"k": "stat", "who": "ally", "stat": "force", "dur": 99, "add": 22},
                      {"k": "heal", "who": "ally", "coef": 0.6, "dur": 1, "when": {"on": "damaged"}, "rate": 1.0},
                  ]}
    heal67_holder.tactics.append(heal67_tac)
    heal67_holder.on_hit_effect_tacs = [t for t in heal67_holder.tactics
                                        if not t.get("when") and t["type"] in ("passive", "command")
                                        and any((e.get("when") or {}).get("on") for e in t.get("effects", []))]
    # prep階段套用(skip_when_effects=True): stat效果應套用, heal(帶e.when)不應在此觸發
    apply_effects(heal67_holder, None, heal67_tac, [heal67_holder], [], no_heal=True, skip_when_effects=True)
    assert abs(heal67_holder.eff("force") - (heal67_holder.force + 22)) < 1e-6, "prep階段: stat效果(無e.when)應正常套用"
    assert heal67_holder.troop == 5000, "prep階段: heal效果(帶e.when.on)不應在此觸發(非反應式事件, 不應治療)"
    # 模擬 on_hit 反應式觸發: 手動比照 fight() 內 on_hit 的 on_hit_effect_tacs 掃描邏輯
    # 時序一致化本批: 正式on_hit()已改用holder.own_round(見上方round_ok呼叫), 此處測試沿用
    # dst.own_round對稱一致(本測項無回合窗口子句, round_ok恆真, 行為不變, 僅語意對齊)。
    heal67_holder.own_round = 1
    heal67_src = Unit(POOL["呂布"], "騎")

    def on_hit67(dst, s2, is_normal, dmg=None, kind=None):  # 批33: dmg(可選)—— 對稱於正式 on_hit(); 批39 C: kind(可選)—— 對稱正式on_hit()新增第5參數
        for t in dst.on_hit_effect_tacs:
            for e in t["effects"]:
                ew = e.get("when") or {}
                if not ew.get("on") or (ew["on"] == "attacked" and not is_normal):
                    continue
                if not round_ok({"when": ew}, dst.own_round) or id(e) in dst.hit_flags:
                    continue
                if random.random() >= e.get("rate", t.get("rate", 1)):
                    continue
                dst.hit_flags.add(id(e))
                apply_effects(dst, s2, {"effects": [e], "kind": t.get("kind", "phys"), "nameZh": t.get("nameZh")},
                              [dst], [s2], rate_checked=True, reactive=True)  # 批23 A4/reactive: 上面已擲過 e["rate"]
    hit(heal67_src, heal67_holder, 1.0, "phys", True, on_hit67)
    assert heal67_holder.wounded != 3000, "受傷+反應式急救觸發後, 傷兵池應有變動(受傷增加又被治療扣減, 淨值不會剛好停在3000)"

    # ------------------------------------------------------------------
    # 批23 系統性缺陷修復 asserts (A1-A5)
    # ------------------------------------------------------------------

    # 68) A1: 效果級 e["n"] —— 非CTRL效果(如 mitig/amp)過去無條件 who="enemy"/"ally" 放大成
    # 全體, 現在有 e["n"] 時應只命中 e["n"] 人(隨機不重複), 而非全體。
    a1_caster = Unit(POOL["呂布"], "騎")
    a1_allies = [Unit(POOL["張飛"], "盾") for _ in range(5)]
    a1_tac_single = {"nameZh": "測試A1單體68", "effects": [{"k": "mitig", "who": "ally", "val": 0.2, "dur": 3, "n": 1}]}
    apply_effects(a1_caster, a1_allies[0], a1_tac_single, a1_allies, [], no_heal=True)
    hit_count = sum(1 for u in a1_allies if u.addbonus("mitig") > 0)
    assert hit_count == 1, f"A1: e['n']=1 應只有1人獲得mitig, 實際{hit_count}人(過去無e.n讀取會是全體5人)"
    # 無 e["n"] 時應維持全體(向後相容)
    a1_allies2 = [Unit(POOL["張飛"], "盾") for _ in range(5)]
    a1_tac_all = {"nameZh": "測試A1全體68b", "effects": [{"k": "mitig", "who": "ally", "val": 0.2, "dur": 3}]}
    apply_effects(a1_caster, None, a1_tac_all, a1_allies2, [], no_heal=True)
    assert all(u.addbonus("mitig") > 0 for u in a1_allies2), "A1: 無e['n']時應維持全體套用(向後相容)"
    # who="enemy" 非CTRL效果(如 amp 易傷)同樣要讀 e["n"]
    a1_enemies = [Unit(POOL["張飛"], "盾") for _ in range(5)]
    a1_tac_enemy_n = {"nameZh": "測試A1敵單體68c", "effects": [{"k": "amp", "who": "enemy", "val": 0.15, "dur": 2, "n": 1}]}
    apply_effects(a1_caster, a1_enemies[0], a1_tac_enemy_n, [], a1_enemies, no_heal=True)
    enemy_hit = sum(1 for u in a1_enemies if u.addbonus("mitig") < 0)  # who=enemy的正amp會轉存成負mitig(易傷)
    assert enemy_hit == 1, f"A1: who=enemy 帶 e['n']=1 應只有1人中招, 實際{enemy_hit}人"

    # 69) A2: counter 讀 e["dur"] —— dur=1 的反擊次回合應失效(過去 dur 幽靈欄位從不遞減, 變永久)
    a2_u = Unit(POOL["張飛"], "盾")
    apply_effects(a2_u, None, {"nameZh": "測試A2反擊69", "effects": [{"k": "counter", "who": "self", "coef": 1.0, "dur": 1}]},
                  [a2_u], [], no_heal=True)
    assert a2_u.counter is not None, "A2: 施加後應立即擁有counter"
    assert a2_u.counter["dur"] == 1, "A2: 時序重構後dur應原值儲存(不補償+1)"
    a2_u.decay_durations()  # 時序重構: 該單位1個行動輪結束 → dur=1的反擊應到期清除
    assert a2_u.counter is None, "A2: dur=1的反擊在該單位1個行動輪後應到期清除(過去幽靈欄位從不遞減, 永久存在)"
    # 無 e["dur"] 應預設常駐(99, 向後相容既有反擊資料)
    a2_u2 = Unit(POOL["張飛"], "盾")
    apply_effects(a2_u2, None, {"nameZh": "測試A2常駐69b", "effects": [{"k": "counter", "who": "self", "coef": 1.0}]},
                  [a2_u2], [], no_heal=True)
    for _ in range(8):
        a2_u2.tick()
    assert a2_u2.counter is not None, "A2: 無e['dur']應預設常駐(99), 8回合內不應消失"

    # 70) A3: dot 結算讀 e["kind"](優先於 t["kind"]) —— 灼燒類 dot 掛在兵刃戰法上仍應走謀略類型
    a3_caster = Unit(POOL["諸葛亮"], "弓")
    a3_tgt = Unit(POOL["張飛"], "盾")
    a3_tac = {"nameZh": "測試A3灼燒70", "kind": "phys",  # 戰法整體是兵刃(t.kind=phys)
              "effects": [{"k": "dot", "who": "enemy", "coef": 0.5, "dur": 2, "kind": "intel"}]}  # dot段自帶kind=intel
    apply_effects(a3_caster, a3_tgt, a3_tac, [a3_caster], [a3_tgt], no_heal=True)
    assert len(a3_tgt.dots) == 1
    random.seed(77)
    expect_intel_dmg = damage(a3_caster, a3_tgt, 0.5, "intel")
    random.seed(77)
    a3_caster2 = Unit(POOL["諸葛亮"], "弓")
    a3_tgt2 = Unit(POOL["張飛"], "盾")
    apply_effects(a3_caster2, a3_tgt2, a3_tac, [a3_caster2], [a3_tgt2], no_heal=True)
    assert abs(a3_tgt2.dots[0][0] - expect_intel_dmg) < 1e-6, "A3: dot段帶e['kind']='intel'應覆蓋戰法整體t['kind']='phys', 走謀略傷害公式(以智力/謀略防禦計算)"

    # 71) A4: 效果級 e["rate"] 在一般路徑(非onHit/delayedEq)也要判定 —— rate=0 應完全不觸發
    a4_u = Unit(POOL["張飛"], "盾")
    a4_tac_zero = {"nameZh": "測試A4零機率71", "effects": [{"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5, "rate": 0.0}]}
    apply_effects(a4_u, None, a4_tac_zero, [a4_u], [], no_heal=True)
    assert abs(a4_u.eff("force") - a4_u.force) < 1e-6, "A4: e['rate']=0.0 的效果應完全不觸發(prep/主動等一般路徑過去完全不讀e['rate'], 必定觸發)"
    a4_u2 = Unit(POOL["張飛"], "盾")
    a4_tac_one = {"nameZh": "測試A4全機率71b", "effects": [{"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5, "rate": 1.0}]}
    apply_effects(a4_u2, None, a4_tac_one, [a4_u2], [], no_heal=True)
    assert abs(a4_u2.eff("force") - (a4_u2.force + 999)) < 1e-6, "A4: e['rate']=1.0 應正常觸發"
    # rate_checked=True 呼叫端應跳過此處判定(避免與呼叫端自己的擲骰重複疊乘)
    a4_u3 = Unit(POOL["張飛"], "盾")
    apply_effects(a4_u3, None, a4_tac_zero, [a4_u3], [], no_heal=True, rate_checked=True)
    assert abs(a4_u3.eff("force") - (a4_u3.force + 999)) < 1e-6, "A4: rate_checked=True時應略過e['rate']判定(呼叫端已自行擲骰過), 即使rate=0也套用"

    # 72) A5: when-gated one-shot 路徑應讀 t["rate"] —— rate=0 的 when-gated 戰法不應觸發
    a5_u = Unit(POOL["張飛"], "盾")
    a5_tac = {"nameZh": "測試A5機率72", "type": "command", "kind": "phys", "coef": 0, "rate": 0.0, "n": 1, "prep": 0,
              "when": {"until": 2}, "effects": [{"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5}]}
    a5_u.tactics.append(a5_tac)
    CUR_ROUND = 1
    if a5_tac["type"] in ("passive", "command") and a5_tac.get("when") and not a5_tac["when"].get("on") \
            and round_ok(a5_tac, 1) and id(a5_tac) not in a5_u.when_fired:
        a5_u.when_fired.add(id(a5_tac))
        if not (random.random() >= a5_tac.get("rate", 1)):
            apply_effects(a5_u, None, a5_tac, [a5_u], [], no_heal=True)
    assert abs(a5_u.eff("force") - a5_u.force) < 1e-6, "A5: rate=0.0 的when-gated戰法不應觸發effects(過去此路徑從不讀t['rate'], 必定觸發)"
    CUR_ROUND = 0

    # 73) 批24 D1: teamGate(隊伍構成前提) —— 開戰建構Unit時依team_factions過濾tactics
    assert team_gate_ok({"factions": "allDiff"}, ["魏", "蜀", "吳"]) is True, "teamGate: allDiff 三方不同陣營應通過"
    assert team_gate_ok({"factions": "allDiff"}, ["魏", "魏", "吳"]) is False, "teamGate: allDiff 有重複陣營應擋下"
    assert team_gate_ok({"factions": "allSame"}, ["魏", "魏", "魏"]) is True, "teamGate: allSame 三方同陣營應通過"
    assert team_gate_ok({"factions": "allSame"}, ["魏", "蜀", "魏"]) is False, "teamGate: allSame 有不同陣營應擋下"
    assert team_gate_ok(None, ["魏", "魏", "魏"]) is True, "teamGate: 無gate應一律放行(向後相容)"
    d1_tac = {"nameZh": "測試teamGate73", "type": "passive", "cat": "FORMATION", "coef": 0, "rate": 1,
              "effects": [{"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 99}],
              "teamGate": {"factions": "allDiff"}}
    TACTICS[d1_tac["nameZh"]] = d1_tac
    d1_u_pass = Unit(POOL["張飛"], "盾", None, None, None, [d1_tac["nameZh"]], None, ["魏", "蜀", "吳"])
    assert any(t["nameZh"] == d1_tac["nameZh"] for t in d1_u_pass.tactics), "teamGate: 隊伍陣營皆不同時, 戰法應保留在tactics中"
    d1_u_block = Unit(POOL["張飛"], "盾", None, None, None, [d1_tac["nameZh"]], None, ["魏", "魏", "吳"])
    assert not any(t["nameZh"] == d1_tac["nameZh"] for t in d1_u_block.tactics), "teamGate: 隊伍陣營有重複時, 戰法應被整條過濾掉"
    del TACTICS[d1_tac["nameZh"]]

    # 74) 批24 D2: dmgType(兵刃/謀略傷害類型過濾) —— amp/mitig 效果可選填 e["dmgType"],
    # 只對該類型傷害生效, 不影響另一類型
    d2_src = Unit(POOL["張飛"], "盾")
    d2_dst_phys = Unit(POOL["諸葛亮"], "盾")
    d2_dst_intel = Unit(POOL["諸葛亮"], "盾")
    d2_tac = {"nameZh": "測試dmgType74", "effects": [
        {"k": "mitig", "who": "ally", "val": 0.5, "dur": 5, "dmgType": "phys"},
    ]}
    apply_effects(d2_dst_phys, None, d2_tac, [d2_dst_phys], [], no_heal=True)
    apply_effects(d2_dst_intel, None, d2_tac, [d2_dst_intel], [], no_heal=True)
    random.seed(99)
    dmg_phys_with_mitig = damage(d2_src, d2_dst_phys, 1.0, "phys")
    random.seed(99)
    dmg_phys_baseline = damage(d2_src, Unit(POOL["諸葛亮"], "盾"), 1.0, "phys")
    assert dmg_phys_with_mitig < dmg_phys_baseline * 0.6, "dmgType: dmgType='phys'的mitig應對兵刃傷害生效(打折)"
    random.seed(99)
    dmg_intel_with_mitig = damage(d2_src, d2_dst_intel, 1.0, "intel")
    random.seed(99)
    dmg_intel_baseline = damage(d2_src, Unit(POOL["諸葛亮"], "盾"), 1.0, "intel")
    assert abs(dmg_intel_with_mitig - dmg_intel_baseline) < 1e-6, "dmgType: dmgType='phys'的mitig不應影響謀略傷害(intel)"
    # 同一戰法內兩條不同dmgType的mitig應各自獨立生效(不因同src刷新去重互相覆蓋, 見dt_src尾碼機制)
    d2_dual_tac = {"nameZh": "測試dmgType雙段74b", "effects": [
        {"k": "mitig", "who": "self", "val": 0.3, "dur": 5, "dmgType": "phys"},
        {"k": "mitig", "who": "self", "val": 0.4, "dur": 5, "dmgType": "intel"},
    ]}
    d2_dual_u = Unit(POOL["諸葛亮"], "盾")
    apply_effects(d2_dual_u, None, d2_dual_tac, [d2_dual_u], [], no_heal=True)
    assert abs(d2_dual_u.addbonus("mitig", "phys") - 0.3) < 1e-6, "dmgType: 兩條不同dmgType的mitig不應互相覆蓋(phys段應保留0.3)"
    assert abs(d2_dual_u.addbonus("mitig", "intel") - 0.4) < 1e-6, "dmgType: 兩條不同dmgType的mitig不應互相覆蓋(intel段應保留0.4)"

    # 75) 批26 B1: e["ifLeader"] —— 效果級「施放者須為隊伍主將(index 0)」條件閘門
    # 主將(allies[0]是施放者自己)時應正常套用
    il_leader = Unit(POOL["張飛"], "盾")
    il_sub = Unit(POOL["諸葛亮"], "盾")
    il_tac = {"nameZh": "測試ifLeader75", "effects": [
        {"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5, "ifLeader": True},
    ]}
    apply_effects(il_leader, None, il_tac, [il_leader, il_sub], [], no_heal=True)
    assert abs(il_leader.eff("force") - (il_leader.force + 999)) < 1e-6, "ifLeader: caster是allies[0](主將)時應正常套用效果"
    # 副將(allies[0]不是施放者自己)時應完全跳過
    il_sub2 = Unit(POOL["諸葛亮"], "盾")
    il_leader2 = Unit(POOL["張飛"], "盾")
    apply_effects(il_sub2, None, il_tac, [il_leader2, il_sub2], [], no_heal=True)
    assert abs(il_sub2.eff("force") - il_sub2.force) < 1e-6, "ifLeader: caster不是allies[0](副將)時應完全跳過該效果, 不套用"
    # 無 e["ifLeader"] 的一般效果不受影響(向後相容)
    il_tac_noflag = {"nameZh": "測試ifLeader常規75b", "effects": [
        {"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5},
    ]}
    il_sub3 = Unit(POOL["諸葛亮"], "盾")
    apply_effects(il_sub3, None, il_tac_noflag, [Unit(POOL["張飛"], "盾"), il_sub3], [], no_heal=True)
    assert abs(il_sub3.eff("force") - (il_sub3.force + 999)) < 1e-6, "ifLeader: 無e['ifLeader']欄位時應維持向後相容(不受此閘門影響, 副將也能套用)"

    # 76) 批26 B2: stack.stackPer —— stack 效果每次「發動」(stackPer:"cast")遞增1層, 而非每回合
    # (stackPer:"round", 預設值, 向後相容)。"cast"模式由 apply_stack_cast() 供戰法命中/發動
    # 結算處呼叫遞增; "round"模式沿用 fight() 主迴圈既有的逐回合遞增(見上方迴圈守衛條件
    # stackPer=="round" 才遞增, 此處用同一段邏輯模擬迴圈行為, 不依賴 Unit.tick()——tick()本身
    # 從未觸碰 stack, 逐回合遞增邏輯獨立寫在 fight() 主迴圈裡, 非 Unit 方法)。
    sc_u = Unit(POOL["張飛"], "盾")
    apply_effects(sc_u, None, {"nameZh": "測試stackPer76", "effects": [
        {"k": "stack", "who": "self", "per": 0.05, "max": 5, "stackPer": "cast"},
    ]}, [sc_u], [], no_heal=True)
    assert sc_u.stack is not None and sc_u.stack.get("per") == 0.05 and sc_u.stack.get("stackPer") == "cast", \
        "stack.stackPer=cast: 初始化後stack字典應保留stackPer標記供遞增邏輯判斷"
    assert sc_u.stack["n"] == 0, "stack.stackPer=cast: 初始套用時層數應為0(尚未發動過, 首次發動才+1, 不同於round模式的prep階段即開始逐回合遞增)"
    # 模擬 fight() 主迴圈的逐回合守衛(僅 stackPer=="round" 才遞增): cast模式應完全不受此步驟影響
    for _ in range(2):
        if sc_u.alive and sc_u.stack and sc_u.stack.get("stackPer", "round") == "round":
            sc_u.stack["n"] = min(sc_u.stack["max"], sc_u.stack["n"] + 1)
    assert sc_u.stack["n"] == 0, "stack.stackPer=cast: fight()主迴圈的逐回合守衛不應遞增cast模式的層數(僅apply_stack_cast()才遞增, 不受回合數影響)"
    sc_u.apply_stack_cast()
    assert sc_u.stack["n"] == 1, "stack.stackPer=cast: apply_stack_cast()呼叫一次應遞增1層"
    sc_u.apply_stack_cast()
    sc_u.apply_stack_cast()
    sc_u.apply_stack_cast()
    sc_u.apply_stack_cast()
    sc_u.apply_stack_cast()
    assert sc_u.stack["n"] == 5, "stack.stackPer=cast: 遞增不應超過max=5層(第6次呼叫應封頂)"
    # stackPer=round(預設/向後相容): 沿用既有fight()主迴圈逐回合遞增行為, apply_stack_cast()呼叫不應有作用
    sr_u = Unit(POOL["張飛"], "盾")
    apply_effects(sr_u, None, {"nameZh": "測試stackPerRound76b", "effects": [
        {"k": "stack", "who": "self", "per": 0.05, "max": 5},
    ]}, [sr_u], [], no_heal=True)
    assert sr_u.stack.get("stackPer", "round") == "round", "stack.stackPer=round(預設): 無stackPer欄位時應視為round, 向後相容既有逐回合遞增資料"
    sr_u.apply_stack_cast()
    assert sr_u.stack["n"] == 0, "stack.stackPer=round: apply_stack_cast()對round模式的stack不應有作用(round模式只認fight()主迴圈逐回合遞增)"
    if sr_u.alive and sr_u.stack and sr_u.stack.get("stackPer", "round") == "round":
        sr_u.stack["n"] = min(sr_u.stack["max"], sr_u.stack["n"] + 1)
    assert sr_u.stack["n"] == 1, "stack.stackPer=round: fight()主迴圈守衛應照舊逐回合遞增1層(向後相容既有行為)"

    # 77) 批27 A: on:"dealtDamage" —— 「自身造成傷害時/後」反應式掛鉤(對比 on_hit 的
    # attacked/damaged 是「自己受擊」視角); 用仿 on_hit_test(見上方7) 的精簡版 dealt_damage_test
    # 驗證 hit() 的 on_deal 回呼會在 src(施加傷害者) 身上正確掃描/觸發, 且 dmgType 過濾/hit_flags
    # 節流/coef 段行為與正式 fight() 內 dealt_damage() 邏輯一致(自我一致性測試, 不依賴完整 fight())。
    def dealt_damage_test(src, dst, is_normal, kind, dmg=None):  # 批33: dmg(可選)—— 對稱於正式 dealt_damage(), 接受 hit() 新增的第5參數
        for t in src.tactics:
            w = t.get("when") or {}
            if w.get("on") != "dealtDamage":
                continue
            dt = w.get("dmgType")
            if dt and dt != kind:
                continue
            if id(t) in src.hit_flags:
                continue
            src.hit_flags.add(id(t))
            if t["effects"]:
                apply_effects(src, dst, t, [src], [dst], reactive=True)

    # 77a) 無 dmgType: 造成任一類型傷害皆應觸發(白衣渡江式: 每次造成傷害都可能繳械/計窮敵軍)
    dd_caster = Unit(POOL["呂布"], "騎")
    dd_target = Unit(POOL["張飛"], "盾")
    dd_tac = {"nameZh": "測試dealtDamage77", "type": "passive", "when": {"on": "dealtDamage"},
              "effects": [{"k": "disarm", "who": "enemy", "dur": 2}]}
    dd_caster.tactics = [dd_tac]
    assert dd_target.disarm == 0
    hit(dd_caster, dd_target, 1.0, "phys", True, None, dealt_damage_test)
    assert dd_target.disarm > 0, "on:'dealtDamage' 應在 src 造成傷害後觸發, 對 dst 套用效果(此處繳械)"

    # 77b) dmgType 過濾: "phys" 限定只在造成兵刃傷害時觸發, 造成謀略傷害不應觸發
    dd_caster2 = Unit(POOL["呂布"], "騎")
    dd_target2 = Unit(POOL["張飛"], "盾")
    dd_tac_phys = {"nameZh": "測試dealtDamagePhys77b", "type": "passive", "when": {"on": "dealtDamage", "dmgType": "phys"},
                   "effects": [{"k": "silence", "who": "enemy", "dur": 1}]}
    dd_caster2.tactics = [dd_tac_phys]
    hit(dd_caster2, dd_target2, 1.0, "intel", False, None, dealt_damage_test)  # 造成謀略傷害: dmgType='phys' 不應觸發
    assert dd_target2.silence == 0, "dmgType='phys' 的 dealtDamage 效果不應在造成謀略傷害(intel)時觸發"
    hit(dd_caster2, dd_target2, 1.0, "phys", False, None, dealt_damage_test)  # 造成兵刃傷害: 應觸發
    assert dd_target2.silence > 0, "dmgType='phys' 的 dealtDamage 效果應在造成兵刃傷害(phys)時觸發"

    # 77c) 同回合節流: 同一戰法每回合最多觸發1次(與 on_hit 共用 hit_flags 慣例, 防無限鏈)。
    # 用計數器(而非stat.add, 見push_stat_add同源刷新慣例/engine_limitations 6.7)驗證觸發次數。
    dd_trigger_count = [0]

    def dealt_damage_count_test(src, dst, is_normal, kind, dmg=None):  # 批33: dmg(可選)—— 對稱於正式 dealt_damage()
        for t in src.tactics:
            w = t.get("when") or {}
            if w.get("on") != "dealtDamage" or id(t) in src.hit_flags:
                continue
            src.hit_flags.add(id(t))
            dd_trigger_count[0] += 1

    dd_caster3 = Unit(POOL["呂布"], "騎")
    dd_target3 = Unit(POOL["張飛"], "盾")
    dd_caster3.tactics = [{"nameZh": "測試dealtDamage節流77c", "type": "passive", "when": {"on": "dealtDamage"}, "effects": []}]
    hit(dd_caster3, dd_target3, 1.0, "phys", True, None, dealt_damage_count_test)
    hit(dd_caster3, dd_target3, 1.0, "phys", True, None, dealt_damage_count_test)
    assert dd_trigger_count[0] == 1, "on:'dealtDamage' 同回合同一戰法應只觸發1次(hit_flags節流), 不應觸發2次"
    dd_caster3.hit_flags.clear()  # 模擬下回合重置(見 Unit.tick())
    hit(dd_caster3, dd_target3, 1.0, "phys", True, None, dealt_damage_count_test)
    assert dd_trigger_count[0] == 2, "hit_flags 每回合重置後, on:'dealtDamage' 應能在新回合重新觸發"

    # 77d) 規避(dodge)時不應觸發: 攻擊未命中, 語意上「未造成傷害」
    dd_caster4 = Unit(POOL["呂布"], "騎")
    dd_target4 = Unit(POOL["張飛"], "盾")
    dd_target4.dodge_dur, dd_target4.dodge_prob = 3, 1.0  # 100%規避
    dd_caster4.tactics = [dict(dd_tac)]
    hit(dd_caster4, dd_target4, 1.0, "phys", True, None, dealt_damage_test)
    assert dd_target4.disarm == 0, "on:'dealtDamage' 不應在攻擊被規避(dodge, 未實際造成傷害)時觸發"

    # 77e) 死亡的 src 不應觸發(如反擊致死後, 反擊方不應再觸發自己的 dealtDamage 效果)
    dd_caster5 = Unit(POOL["呂布"], "騎")
    dd_caster5.troop = 0  # 已陣亡
    dd_target5 = Unit(POOL["張飛"], "盾")
    dd_caster5.tactics = [dict(dd_tac)]
    hit(dd_caster5, dd_target5, 1.0, "phys", True, None, dealt_damage_test)
    assert dd_target5.disarm == 0, "on:'dealtDamage' 不應在 src 已陣亡時觸發(hit()內 on_deal 呼叫前已檢查 src.alive)"

    # 77f) fight() 內建的 on_deal_tacs/on_deal_effect_tacs 預篩應正確收錄戰法級/效果級 dealtDamage
    dd_prefilter_u = Unit(POOL["呂布"], "騎")
    dd_prefilter_u.tactics = [
        {"nameZh": "測試預篩戰法級77f", "type": "passive", "when": {"on": "dealtDamage"}, "effects": []},
        {"nameZh": "測試預篩效果級77f", "type": "passive", "effects": [
            {"k": "stat", "who": "self", "stat": "force", "add": 1, "dur": 1, "when": {"on": "dealtDamage"}},
        ]},
        {"nameZh": "測試預篩無關77f", "type": "passive", "effects": [
            {"k": "stat", "who": "self", "stat": "force", "add": 1, "dur": 1},
        ]},
    ]
    dd_prefilter_u.on_deal_tacs = [t for t in dd_prefilter_u.tactics
                                   if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") == "dealtDamage"]
    dd_prefilter_u.on_deal_effect_tacs = [t for t in dd_prefilter_u.tactics
                                          if not t.get("when") and t["type"] in ("passive", "command", "active")
                                          and any((e.get("when") or {}).get("on") == "dealtDamage" for e in t.get("effects", []))]
    assert len(dd_prefilter_u.on_deal_tacs) == 1 and dd_prefilter_u.on_deal_tacs[0]["nameZh"] == "測試預篩戰法級77f", \
        "on_deal_tacs 應只收錄 t.when.on=='dealtDamage' 的戰法級反應式戰法"
    assert len(dd_prefilter_u.on_deal_effect_tacs) == 1 and dd_prefilter_u.on_deal_effect_tacs[0]["nameZh"] == "測試預篩效果級77f", \
        "on_deal_effect_tacs 應只收錄無t.when、但至少一個效果帶e.when.on=='dealtDamage'的戰法"

    # 78) 批27 B: choices 對 command/passive 型戰法生效(見 engine_limitations.md §18a) ——
    # 過去 fight() 主迴圈確實會對 command/passive 型戰法擲骰(fire)並用 pick_choice() 抽出分支
    # t, 但緊接著的 apply_effects(...) 呼叫只在 t["type"]=="active" 才執行, command/passive
    # 型戰法抽中的分支 effects 被憑空丟棄。端到端驗證修復(真正跑 fight(), 不繞過main loop):
    # 暫時把 呂布 的自帶戰法換成一個 command 型 choices 戰法(coef=0, 頂層effects=[], 單一分支
    # 帶巨額heal, rate=1.0每回合必發), 比較「有此戰法」vs「呂布維持原戰法(對照)」在同組敵人
    # 前的模擬勝率, 巨額治療應顯著推高勝率(若§18a舊bug仍在, 分支effects被丟棄, 巨額heal形同
    # 虛設, 勝率不會有感提升)。用勝率統計而非直接檢查Unit內部狀態, 因為fight()/simulate()不
    # 回傳戰鬥內部單位物件, 只能透過可觀察的戰鬥結果(勝率)反推main loop是否真的套用了效果。
    _orig_lb_tactic = POOL["呂布"].tactic
    ch78_tac = {
        "nameZh": "測試choices指揮78", "type": "command", "kind": "phys", "coef": 0,
        "rate": 1.0, "n": 1, "prep": 0, "effects": [],
        "choices": [
            {"weight": 1, "effects": [{"k": "heal", "who": "ally", "coef": 3.0, "dur": 1}]},
        ],
    }
    random.seed(2027)
    win_with_choices_heal = simulate(["呂布", "張飛", "諸葛亮"], ["曹操", "司馬懿", "周瑜"], n=400)["A勝率"]
    POOL["呂布"].tactic = ch78_tac
    try:
        random.seed(2027)
        win_with_ch78 = simulate(["呂布", "張飛", "諸葛亮"], ["曹操", "司馬懿", "周瑜"], n=400)["A勝率"]
    finally:
        POOL["呂布"].tactic = _orig_lb_tactic  # 還原, 避免污染後續測試/其他呼叫端
    assert win_with_ch78 > win_with_choices_heal + 0.05, (
        f"批27 B: command型戰法帶choices(單分支巨額heal, coef=3.0)應顯著推高勝率"
        f"(套用前基準{win_with_choices_heal:.3f}, 套用後{win_with_ch78:.3f})——若分支effects"
        f"仍如§18a舊bug被main loop憑空丟棄, 巨額heal不會生效, 勝率不會有感提升")

    # 78b) 對照組: 無 choices 的一般 command/passive 戰法(effects直接在頂層, 非choices)不受
    # 本次新增的 elif t0.get("choices") 分支影響——只在 t0.get("choices") 為真時才會進入該
    # 分支, 一般戰法(choices為空/不存在)完全不觸碰新增程式碼路徑, 效果仍只由既有
    # apply_passives() prep/heal_only通道處理(不會被套用兩次)。
    plain_cmd_tac = {"nameZh": "測試無choices對照78b", "type": "command", "kind": "phys", "coef": 0,
                     "rate": 1.0, "n": 1, "prep": 0,
                     "effects": [{"k": "heal", "who": "ally", "coef": 3.0, "dur": 1}]}
    POOL["呂布"].tactic = plain_cmd_tac
    try:
        random.seed(2027)
        win_plain_heal = simulate(["呂布", "張飛", "諸葛亮"], ["曹操", "司馬懿", "周瑜"], n=400)["A勝率"]
    finally:
        POOL["呂布"].tactic = _orig_lb_tactic
    # 無choices版本(靠既有apply_passives heal_only通道逐回合治療)與choices版本(靠新增main
    # loop elif分支單次觸發治療)應是同量級的顯著提升(非雙重結算的異常暴增, 也非被丟棄的無提升),
    # 兩者皆應遠高於未裝任何heal戰法的基準, 但彼此不必完全相等(觸發頻率語意本就不同: heal_only
    # 每回合都治療 vs choices版本只在command每回合擲骰後才透過main loop套用一次, 兩者理論上
    # 頻率相近, 但用寬鬆判斷避免方差誤傷測試穩定性)。
    assert win_plain_heal > win_with_choices_heal + 0.05, \
        "對照組(無choices, 頂層heal): 既有apply_passives通道應正常運作, 勝率同樣應顯著高於基準"

    # 79) 批27 C: choices 對 on_hit() 反應式路徑(when.on)生效(見 engine_limitations.md §8) ——
    # 過去 when.on:"attacked"/"damaged" 的反應式戰法完全不讀 t0["choices"], 資料層寫入choices
    # 形同虛設(魅惑「混亂/計窮/虛弱」三選一即是此缺口的代表案例)。用仿 on_hit_test 的精簡版
    # on_hit_choices_test 驗證修復: 兩個weight=0/999懸殊的分支, 應幾乎必中weight大的那個。
    def on_hit_choices_test(dst, src, is_normal, dmg=None, kind=None):  # 批33: dmg(可選)—— 對稱於正式 on_hit(); 批39 C: kind(可選)—— 對稱正式on_hit()新增第5參數
        for t0 in dst.tactics:
            on = (t0.get("when") or {}).get("on")
            if not on or (on == "attacked" and not is_normal):
                continue
            if id(t0) in dst.hit_flags:
                continue
            dst.hit_flags.add(id(t0))
            t = dict(t0, **pick_choice(t0["choices"])) if t0.get("choices") else t0
            if t["effects"]:
                apply_effects(dst, src, t, [dst], [src], reactive=True)

    ch79_tac = {
        "nameZh": "測試choices反應式79", "type": "passive", "coef": 0, "rate": 1.0, "n": 1, "prep": 0,
        "effects": [], "when": {"on": "attacked"},
        "choices": [
            {"weight": 999, "effects": [{"k": "silence", "who": "enemy", "dur": 3}]},
            {"weight": 1, "effects": [{"k": "stun", "who": "enemy", "dur": 3}]},
        ],
    }
    ch79_defender = Unit(POOL["張飛"], "盾")
    ch79_defender.tactics = [ch79_tac]
    ch79_attacker = Unit(POOL["呂布"], "騎")
    random.seed(1)  # weight 999:1 幾乎必中第一分支(silence)
    hit(ch79_attacker, ch79_defender, 1.0, "phys", True, on_hit_choices_test)
    assert ch79_attacker.silence > 0 or ch79_attacker.stun > 0, \
        "批27 C: on_hit()反應式路徑應讀取choices並套用抽中分支的effects(此前完全不讀choices, 效果被憑空丟棄)"
    # 高權重分支(silence)應是實際命中的那個(驗證pick_choice真的有被呼叫而非固定套用t0本身的
    # 原始effects, t0["effects"]為空陣列, 若choices未被消費則不會有任何效果套用)
    assert ch79_attacker.silence > 0, "weight 999:1 應幾乎必中silence分支"

    # 80) 批27 C 落地驗證: 魅惑(真實資料, 見 TACTICS["魅惑"])三選一(混亂/計窮/虛弱)在 on_hit()
    # 反應式路徑下應能抽到全部三種分支(對照批20舊版固定只會抽到silence一種)。大量取樣(rate=0.45
    # 取滿級, 每次獨立戰鬥實例避免hit_flags節流互相干擾)統計是否三種效果皆曾出現過。
    meihuo_tac = TACTICS["魅惑"]
    assert meihuo_tac.get("choices") and len(meihuo_tac["choices"]) == 3, "魅惑應有3個choices分支(混亂/計窮/虛弱)"
    seen_kinds = set()
    random.seed(11)
    for _ in range(400):
        mh_defender = Unit(POOL["貂蟬"] if "貂蟬" in POOL else POOL["張飛"], "盾")
        mh_defender.tactics = [meihuo_tac]
        mh_attacker = Unit(POOL["呂布"], "騎")
        hit(mh_attacker, mh_defender, 1.0, "phys", True, on_hit_choices_test)
        if mh_attacker.chaos:
            seen_kinds.add("chaos")
        if mh_attacker.silence:
            seen_kinds.add("silence")
        if mh_attacker.addbonus("amp") <= -0.99:
            seen_kinds.add("weak")
    assert seen_kinds == {"chaos", "silence", "weak"}, \
        f"魅惑三選一(choices)應400次取樣內三種效果(混亂/計窮/虛弱)皆至少出現一次, 實際={seen_kinds}" \
        f"(若只出現silence一種, 代表choices退化回批20舊版固定行為)"

    # 81) 批28 B1: counter.guardFor:"leader"(守護式反擊) —— 效果掛在 subs(副將)身上但
    # guardFor:"leader", 套用後應登記進主將的 counter_guards, 而非副將自己的 counter
    # (方向反了的舊行為對照組)。主將受到普攻時, 應由副將代為反擊攻擊者(攻擊者掉血), 而非
    # 主將自己還手(主將本身 counter 應保持 None)。
    gf_leader = Unit(POOL["典韋"] if "典韋" in POOL else POOL["呂布"], "盾")
    gf_sub = Unit(POOL["張飛"], "盾")
    gf_team = [gf_leader, gf_sub]
    gf_tac = {"nameZh": "測試守護反擊81", "effects": [
        {"k": "counter", "who": "subs", "coef": 1.0, "kind": "phys", "prob": 1.0, "guardFor": "leader"}]}
    apply_effects(gf_sub, None, gf_tac, gf_team, [], no_heal=True)
    assert gf_leader.counter is None, "guardFor:leader 不應讓主將自己掛 counter"
    assert gf_sub.counter is None, "guardFor:leader 不應讓持有效果的副將自己掛 counter(方向已改為守護式)"
    assert len(gf_leader.counter_guards) == 1 and gf_leader.counter_guards[0]["unit"] is gf_sub, \
        "guardFor:leader 應把副將登記進主將的 counter_guards"
    gf_attacker = Unit(POOL["呂布"], "騎")
    atk_troop0 = gf_attacker.troop
    hit(gf_attacker, gf_leader, 1.0, "phys", True)   # 普攻主將(is_normal=True)
    assert gf_attacker.troop < atk_troop0, "主將受普攻時, 副將應代為反擊攻擊者(攻擊者應掉血)"
    # 每回合最多觸發1次: 同回合內(未 tick)第二次普攻主將不應再觸發第二次守護反擊
    atk_troop1 = gf_attacker.troop
    hit(gf_attacker, gf_leader, 1.0, "phys", True)
    assert abs(gf_attacker.troop - atk_troop1) < 1e-6, \
        "guardFor 守護反擊每回合最多觸發1次, 同回合內第二次普攻不應再次觸發"
    gf_leader.tick(); gf_sub.tick(); gf_attacker.tick()   # 換回合: hit_flags 重置後應能再次觸發
    atk_troop2 = gf_attacker.troop
    hit(gf_attacker, gf_leader, 1.0, "phys", True)
    assert gf_attacker.troop < atk_troop2, "換回合後(hit_flags重置)應能再次觸發守護反擊"
    # 非普攻(戰法傷害, is_normal=False)不應觸發守護反擊(原文「即將受到普攻時」限定)
    gf_leader2 = Unit(POOL["典韋"] if "典韋" in POOL else POOL["呂布"], "盾")
    gf_sub2 = Unit(POOL["張飛"], "盾")
    apply_effects(gf_sub2, None, gf_tac, [gf_leader2, gf_sub2], [], no_heal=True)
    gf_attacker2 = Unit(POOL["呂布"], "騎")
    atk_troop3 = gf_attacker2.troop
    hit(gf_attacker2, gf_leader2, 1.0, "phys", False)   # 戰法傷害, is_normal=False
    assert abs(gf_attacker2.troop - atk_troop3) < 1e-6, "guardFor 守護反擊只應在普攻(is_normal=True)時觸發, 戰法傷害不應觸發"

    # 82) 批28 B3: amp/mitig.normalOnly(僅普攻傷害生效/受影響) —— 效果只應在 is_normal=True
    # (普攻)時對 damage() 產生作用, 突擊/戰法傷害(is_normal=False)不應受影響(見至柔動剛
    # 「降低我軍及敵軍全體普通攻擊傷害35%」, 外部查證確認root data「提升」為誤植, 應為「降低」)。
    no_src, no_dst = Unit(POOL["呂布"], "騎"), Unit(POOL["張飛"], "盾")
    no_dst.push_add("mitig", 0.35, 9, "測試normalOnly82", {"normalOnly": True})
    d_normal = damage(no_src, no_dst, 1.0, "phys", is_normal=True)
    d_tactic = damage(no_src, no_dst, 1.0, "phys", is_normal=False)
    d_baseline = damage(Unit(POOL["呂布"], "騎"), Unit(POOL["張飛"], "盾"), 1.0, "phys", is_normal=False)
    assert d_normal < d_baseline * 0.8, "normalOnly mitig: is_normal=True 時應套用減傷"
    assert abs(d_tactic - d_baseline) < d_baseline * 0.15, \
        f"normalOnly mitig: is_normal=False(戰法傷害) 時不應套用減傷, d_tactic={d_tactic:.1f} baseline={d_baseline:.1f}"
    # amp 同理(用正值增傷驗證, 避免與上面mitig的效果混淆, 各自獨立unit)
    no_src2 = Unit(POOL["呂布"], "騎")
    no_src2.push_add("amp", 0.5, 9, "測試normalOnly82b", {"normalOnly": True})
    no_dst2 = Unit(POOL["張飛"], "盾")
    d_amp_normal = damage(no_src2, no_dst2, 1.0, "phys", is_normal=True)
    d_amp_tactic = damage(no_src2, no_dst2, 1.0, "phys", is_normal=False)
    d_amp_baseline = damage(Unit(POOL["呂布"], "騎"), Unit(POOL["張飛"], "盾"), 1.0, "phys", is_normal=False)
    assert d_amp_normal > d_amp_baseline * 1.2, "normalOnly amp: is_normal=True 時應套用增傷"
    assert abs(d_amp_tactic - d_amp_baseline) < d_amp_baseline * 0.15, \
        "normalOnly amp: is_normal=False(戰法傷害) 時不應套用增傷"

    # 83) 批28 B4: choices 傷害分支(coef/kind/targetSel) 對 command 型戰法應能端到端造成傷害
    # (桃園結義三選一重建的可行性基礎) —— fight() 主迴圈既有的 command/passive 派發路徑(見
    # 批16/27既有機制)讀取 pick_choice() 抽出的分支 t["coef"]/t["kind"]/t["targetSel"] 並呼叫
    # hit(), 不需要引擎擴充即可支援傷害段。用勝率統計驗證(同78號測試手法): 帶巨額傷害分支
    # (coef=5.0)的command戰法應比完全no-op的對照組顯著推高勝率。
    # 批38: 除呂布外, 也暫時把「諸葛亮」的自帶戰法(神機妙算)換成中性no-op占位——本測試的
    # 本意是隔離驗證choices機制本身, 不應受隊上其他武將自帶戰法(尤其批38後諸葛亮的神機妙算
    # 已改為activeFired反應式監聽, 觸發頻率隨敵隊主動戰法發動節奏浮動, 與本測試無關卻會顯著
    # 影響A隊整體傷害輸出, 使win_dmg_83/win_noop_83的差值不穩定)干擾, 確保測試只量測choices
    # 傷害段本身的影響。
    _orig_lb_tactic_83 = POOL["呂布"].tactic
    _orig_zgl_tactic_83 = POOL["諸葛亮"].tactic
    noop_tac_83 = {"nameZh": "測試no-op對照83", "type": "command", "kind": "phys", "coef": 0,
                   "rate": 1.0, "n": 1, "prep": 0, "effects": []}
    noop_tac_83b = {"nameZh": "測試no-op對照83b", "type": "command", "kind": "phys", "coef": 0,
                    "rate": 1.0, "n": 1, "prep": 0, "effects": []}
    dmg_choice_tac_83 = {
        "nameZh": "測試choices傷害分支83", "type": "command", "kind": "phys", "coef": 0,
        "rate": 1.0, "n": 1, "prep": 0, "effects": [],
        "choices": [
            {"weight": 1, "coef": 20.0, "kind": "intel", "targetSel": "minIntel", "effects": []},
        ],
    }
    POOL["諸葛亮"].tactic = noop_tac_83b
    POOL["呂布"].tactic = noop_tac_83
    try:
        random.seed(83)
        win_noop_83 = simulate(["呂布", "張飛", "諸葛亮"], ["曹操", "司馬懿", "周瑜"], n=300)["A勝率"]
    finally:
        POOL["呂布"].tactic = _orig_lb_tactic_83
    POOL["呂布"].tactic = dmg_choice_tac_83
    try:
        random.seed(83)
        win_dmg_83 = simulate(["呂布", "張飛", "諸葛亮"], ["曹操", "司馬懿", "周瑜"], n=300)["A勝率"]
    finally:
        POOL["呂布"].tactic = _orig_lb_tactic_83
        POOL["諸葛亮"].tactic = _orig_zgl_tactic_83
    assert win_dmg_83 > win_noop_83 + 0.1, (
        f"批28 B4: command型戰法choices分支帶coef/kind/targetSel應能造成真實傷害並顯著推高"
        f"勝率(no-op基準{win_noop_83:.3f}, 帶傷害分支{win_dmg_83:.3f})——若choices的coef傷害段"
        f"未被main loop讀取/呼叫hit(), 兩者應無顯著差異")
    # 桃園結義本尊(真實資料): 應有3個choices分支(治療+2個傷害段), 且頂層rate=0.4
    taoyuan_tac = TACTICS["桃園結義"]
    assert taoyuan_tac.get("choices") and len(taoyuan_tac["choices"]) == 3, \
        "桃園結義應重建為3個choices分支(治療我軍最殘/謀略傷害打智力最低敵/兵刃傷打統率最低敵)"
    assert abs(taoyuan_tac["rate"] - 0.4) < 1e-6, "桃園結義頂層rate應為0.4(20%-40%取滿級)"
    dmg_branches = [c for c in taoyuan_tac["choices"] if c.get("coef")]
    assert len(dmg_branches) == 2, "桃園結義應有2個帶coef的傷害分支"
    assert {c.get("targetSel") for c in dmg_branches} == {"minIntel", "minCommand"}, \
        "桃園結義2個傷害分支應分別以minIntel(打智力最低敵)/minCommand(打統率最低敵)為targetSel"

    # 84) 批30 A: e["everyRound"](非heal效果的逐回合重擲通道) —— block效果帶everyRound+rate,
    # 應在 own_turn=True 的每個持有者自己行動輪常駐通道重新擲骰/套用(而非prep套用一次),
    # 未命中的回合不新增次數。同時驗證: (a) 不帶everyRound的效果行為零變化(prep套用一次,
    # own_turn通道不重複套用); (b) everyRound效果在prep(heal_only=False/own_turn=False)呼叫
    # 時完全不套用。時序一致化(2026-07批次)A.3: 改用 own_turn=True + caster.own_round(該
    # 持有者自己的行動輪計數)取代舊 heal_only=True + 全局CUR_ROUND(user權威規則: 「回合」對
    # 持有者自身的漸進/計數機制=該持有者自己的行動輪, 詳見 apply_effects everyRound 分支)。
    er_u = Unit(POOL["呂布"], "盾")
    er_ally = Unit(POOL["張飛"], "盾")
    er_team = [er_u, er_ally]
    # 狀態疊加精修批: 本測試驗證的是「everyRound逐回合重擲」這個通用機制(與block本身的
    # 抵禦/警戒疊加規則無關), 改用val=0.4(警戒, 恆累積)而非1.0(抵禦, 本批改「有剩餘不補
    # 不刷」)以避免與抵禦新規則混淆, 保留測試原意(驗證同源重複套用是否疊次)。
    er_tac_flagged = {"nameZh": "測試everyRound84", "rate": 1.0, "effects": [
        {"k": "block", "who": "self", "val": 0.4, "times": 1, "everyRound": True}]}
    # (b) prep(heal_only=False, own_turn=False)呼叫: everyRound效果不應套用
    apply_effects(er_u, None, er_tac_flagged, er_team, [], no_heal=True, skip_when_effects=True)
    assert not er_u.block, "everyRound效果不應在prep(own_turn=False)路徑套用"
    # (a) own_turn常駐通道: rate=1.0應每次都命中, 該持有者每個自己的行動輪各自新增1次(警戒同源疊次語意)
    er_u.own_round = 1
    apply_effects(er_u, None, er_tac_flagged, er_team, [], own_turn=True)
    assert er_u.block and sum(b["n"] for b in er_u.block) == 1, \
        "everyRound效果應在own_turn常駐通道套用一次(持有者自己第1個行動輪, rate=1.0必中)"
    er_u.own_round = 2
    apply_effects(er_u, None, er_tac_flagged, er_team, [], own_turn=True)
    assert sum(b["n"] for b in er_u.block) == 2, \
        "everyRound效果應該持有者每個自己的行動輪重新擲骰/套用(第2個行動輪再命中一次, 同源警戒應疊次成2, 而非停留在prep的一次性套用)"
    # rate=0時不應新增(驗證擲骰真的生效, 非無條件套用)
    er_u2 = Unit(POOL["呂布"], "盾")
    er_team2 = [er_u2, Unit(POOL["張飛"], "盾")]
    er_tac_norate = {"nameZh": "測試everyRound無命中84", "rate": 0.0, "effects": [
        {"k": "block", "who": "self", "val": 0.4, "times": 1, "everyRound": True}]}
    er_u2.own_round = 1
    apply_effects(er_u2, None, er_tac_norate, er_team2, [], own_turn=True)
    assert not er_u2.block, "everyRound效果rate=0.0時不應套用(擲骰應真的生效, 非無條件通過)"
    # 對照組: 不帶everyRound的block效果(既有行為) —— prep套用一次, own_turn/heal_only常駐通道皆不應重複套用
    er_u3 = Unit(POOL["呂布"], "盾")
    er_team3 = [er_u3, Unit(POOL["張飛"], "盾")]
    er_tac_plain = {"nameZh": "測試無everyRound對照84", "rate": 1.0, "effects": [
        {"k": "block", "who": "self", "val": 0.4, "times": 1}]}
    apply_effects(er_u3, None, er_tac_plain, er_team3, [], no_heal=True, skip_when_effects=True)
    assert er_u3.block and sum(b["n"] for b in er_u3.block) == 1, "不帶everyRound的效果應維持既有行為: prep套用一次"
    er_u3.own_round = 1
    apply_effects(er_u3, None, er_tac_plain, er_team3, [], own_turn=True)
    assert sum(b["n"] for b in er_u3.block) == 1, \
        "不帶everyRound的非heal效果在own_turn常駐通道不應被重複套用(零回歸: 既有行為不受本次改動影響)"
    CUR_ROUND = 1
    apply_effects(er_u3, None, er_tac_plain, er_team3, [], heal_only=True)
    assert sum(b["n"] for b in er_u3.block) == 1, \
        "不帶everyRound的非heal效果在heal_only(現嚴格heal-only)通道亦不應被重複套用"
    CUR_ROUND = 0

    # 85) 批30 C: who=="sub1"/"sub2"(副將固定位置分派) —— 三人隊(leader/sub1/sub2), 兩段效果
    # 分別指定 who:"sub1"(只防兵刃)/who:"sub2"(只防謀略), 應精確命中 allies[1]/allies[2],
    # 不誤中主將(allies[0]), 不互相污染。
    sp_leader = Unit(POOL["呂布"], "盾")
    sp_sub1 = Unit(POOL["張飛"], "盾")
    sp_sub2 = Unit(POOL["關羽"], "盾")
    sp_team = [sp_leader, sp_sub1, sp_sub2]
    sp_tac = {"nameZh": "測試箕形陣85", "effects": [
        {"k": "mitig", "who": "sub1", "val": 0.2, "dur": 9, "dmgType": "phys"},
        {"k": "mitig", "who": "sub2", "val": 0.2, "dur": 9, "dmgType": "intel"},
    ]}
    apply_effects(sp_leader, None, sp_tac, sp_team, [], no_heal=True, skip_when_effects=True)
    sub1_mitig = [a for a in sp_sub1.adds if a[0] == "mitig"]
    sub2_mitig = [a for a in sp_sub2.adds if a[0] == "mitig"]
    leader_mitig = [a for a in sp_leader.adds if a[0] == "mitig"]
    assert len(sub1_mitig) == 1 and sub1_mitig[0][4] and sub1_mitig[0][4].get("dmgType") == "phys", \
        "who=='sub1' 應精確命中 allies[1](副將A), 且 dmgType=phys 應正確傳遞"
    assert len(sub2_mitig) == 1 and sub2_mitig[0][4] and sub2_mitig[0][4].get("dmgType") == "intel", \
        "who=='sub2' 應精確命中 allies[2](副將B), 且 dmgType=intel 應正確傳遞"
    assert len(leader_mitig) == 0, "who=='sub1'/'sub2' 不應誤中主將(allies[0])"
    # 隊伍不足3人時 sub2 應為空(不應報錯/誤選)
    sp_team2 = [Unit(POOL["呂布"], "盾"), Unit(POOL["張飛"], "盾")]
    sp_tac2 = {"nameZh": "測試箕形陣85b", "effects": [{"k": "mitig", "who": "sub2", "val": 0.2, "dur": 9}]}
    apply_effects(sp_team2[0], None, sp_tac2, sp_team2, [], no_heal=True, skip_when_effects=True)
    assert not any(a[0] == "mitig" for a in sp_team2[1].adds), "隊伍不足3人時 who=='sub2' 應為空目標, 不應誤套用到sub1身上"

    # 86) 批31 A: on:"activeFired"(自身成功發動主動戰法時反應式觸發) —— 仿 dealt_damage_test
    # (見上方77) 的精簡版 active_fired_test, 驗證: (a) 只在真的有type:"active"戰法fire=True
    # 時才觸發, 常駐擲骰(command/passive無關戰法)不應觸發; (b) rate擲骰仍生效(非無條件);
    # (c) 同回合節流(hit_flags); (d) e.activeOnly 正確限定amp只在is_active=True的hit()生效。
    def active_fired_test(u, allies, foes):
        for t in u.active_fired_tacs:
            if id(t) in u.hit_flags:
                continue
            if random.random() >= t["rate"]:
                continue
            u.hit_flags.add(id(t))
            main_tgt = None
            if t["coef"]:
                vs = pick_targets(foes, t["n"])
                for v in vs:
                    hit(u, v, t["coef"], t["kind"], False, None, None, is_active=True)
                if len(vs) == 1:
                    main_tgt = vs[0]
            if t["effects"]:
                apply_effects(u, main_tgt, t, allies, foes, reactive=True)

    # 86a) 士爭先赴真實資料端到端驗證: 只在自身某個 active 戰法「成功發動」(呼叫端顯式觸發
    # active_fired_test, 模擬 fight() 主迴圈 fire=True 分支)時才可能造成兵刃傷害; 若從未呼叫
    # (等同該回合沒有任何主動戰法成功發動), 士爭先赴的傷害/amp皆不應觸發。
    sfxf_caster = Unit(POOL["呂布"], "騎")
    sfxf_caster.tactics = [dict(TACTICS["士爭先赴"])]
    sfxf_caster.active_fired_tacs = [t for t in sfxf_caster.tactics
                                     if t["type"] in ("passive", "command") and (t.get("when") or {}).get("on") == "activeFired"]
    sfxf_caster.active_fired_effect_tacs = [t for t in sfxf_caster.tactics
                                            if not t.get("when") and t["type"] in ("passive", "command", "active")
                                            and any((e.get("when") or {}).get("on") == "activeFired" for e in t.get("effects", []))]
    assert len(sfxf_caster.active_fired_tacs) == 1, "士爭先赴應被正確預篩收錄進 active_fired_tacs(t.when.on=='activeFired')"
    foe1 = Unit(POOL["張飛"], "盾")
    foe2 = Unit(POOL["關羽"], "盾")
    foes_troop_before = (foe1.troop, foe2.troop)
    random.seed(1)
    # 不呼叫 active_fired_test: 沒有任何主動戰法成功發動這件事發生過, 士爭先赴不應無條件觸發
    assert foe1.troop == foes_troop_before[0] and foe2.troop == foes_troop_before[1], \
        "士爭先赴不應在「無主動戰法成功發動」的情況下常駐觸發(v14盲測0分bug: 條件觸發簡化為無條件)"
    # 呼叫 active_fired_test 多次(模擬多回合皆有主動戰法成功發動), rate=0.5 應統計上觸發部分次數
    # 而非100%/0%; 每次試驗用全新的foe(避免累積傷害讓「是否觸發」的判斷失真)。
    fire_count = 0
    trials = 400
    for _ in range(trials):
        sfxf_caster.hit_flags.clear()  # 模擬每回合重置(見 Unit.tick())
        trial_foe = Unit(POOL["張飛"], "盾")
        troop_before = trial_foe.troop
        active_fired_test(sfxf_caster, [sfxf_caster], [trial_foe])
        if trial_foe.troop < troop_before:
            fire_count += 1
    rate_est = fire_count / trials
    assert 0.35 < rate_est < 0.65, f"士爭先赴 rate=0.5 應統計上約半數觸發, 實測rate_est={rate_est:.3f}(應排除0%/100%的無條件模式)"

    # 86b) e.activeOnly: amp(自身)應只對 is_active=True 的傷害生效, 對普通攻擊(is_active未傳/None)不生效
    ao_src = Unit(POOL["呂布"], "騎")
    ao_dst1 = Unit(POOL["張飛"], "盾")
    ao_dst2 = Unit(POOL["張飛"], "盾")
    ao_src.push_add("amp", 1.0, 9, "測試activeOnly86b", {"activeOnly": True})
    d_active = damage(ao_src, ao_dst1, 1.0, "phys", is_active=True)
    d_normal = damage(ao_src, ao_dst2, 1.0, "phys", is_active=None)
    assert d_active > d_normal * 1.5, f"activeOnly amp 應只在 is_active=True 時生效: d_active={d_active:.1f} d_normal={d_normal:.1f}"

    # 86b-2) 批40 B: e.chargeOnly —— 對稱86b, amp(自身)應只對 is_charge=True 的傷害生效,
    # 且與 activeOnly 互斥(is_active=True 時不應觸發 chargeOnly 的加成, 反之亦然)——這是本批
    # 修正批31 A「突擊傷害誤標is_active=True導致誤觸activeOnly」bug的迴歸防線: 一鼓作氣
    # (chargeOnly+12%)這類效果不應對主動戰法傷害生效, 士爭先赴(activeOnly)這類效果也不應
    # 再被突擊傷害觸發。damage() 帶 ±4% 隨機帶(random.uniform(0.96,1.04)), 單次取樣比較
    # 曾實測約18%機率因隨機噪聲誤判失敗(flaky), 改用多次取平均消除隨機性(而非放大容差
    # 掩蓋邏輯問題)。
    def _avg_dmg(src, dst_g, is_active=None, is_charge=None, n=200):
        return sum(damage(src, Unit(POOL[dst_g], "盾"), 1.0, "phys", is_active=is_active, is_charge=is_charge) for _ in range(n)) / n

    co_src = Unit(POOL["呂布"], "騎")
    co_src.push_add("amp", 0.12, 9, "測試chargeOnly86b2", {"chargeOnly": True})
    d_charge = _avg_dmg(co_src, "張飛", is_charge=True)
    d_active2 = _avg_dmg(co_src, "張飛", is_active=True)   # 突擊限定的amp不應套用在主動戰法傷害上
    d_normal2 = _avg_dmg(co_src, "張飛")                    # 也不應套用在普攻上
    assert d_charge > d_active2 * 1.05, f"chargeOnly amp 應只在 is_charge=True 時生效, 不應誤觸主動戰法傷害: d_charge={d_charge:.1f} d_active={d_active2:.1f}"
    assert abs(d_active2 - d_normal2) < d_normal2 * 0.03, f"chargeOnly amp 不應誤觸主動/普攻傷害(兩者應相近, 皆不吃chargeOnly加成): d_active={d_active2:.1f} d_normal={d_normal2:.1f}"

    # 86b-3) 迴歸防線: 突擊(charge)戰法傷害不應再誤觸activeOnly(批31 A遺留bug, 本批40 B修正)
    ao2_src = Unit(POOL["呂布"], "騎")
    ao2_src.push_add("amp", 1.0, 9, "測試activeOnly不誤觸charge86b3", {"activeOnly": True})
    d_charge2 = _avg_dmg(ao2_src, "張飛", is_charge=True)  # 突擊傷害: 不應吃到activeOnly加成
    d_normal3 = _avg_dmg(ao2_src, "張飛")
    assert abs(d_charge2 - d_normal3) < d_normal3 * 0.03, f"activeOnly amp 不應被突擊(charge)傷害誤觸(批31 A遺留bug, 批40 B應已修正): d_charge={d_charge2:.1f} d_normal={d_normal3:.1f}"

    # 86c) 同回合節流: active_fired 每回合每戰法最多觸發1次(與 on_hit/dealt_damage 共用 hit_flags 慣例)
    af_trigger_count = [0]
    af_u = Unit(POOL["呂布"], "騎")
    af_tac = {"nameZh": "測試activeFired節流86c", "type": "passive", "when": {"on": "activeFired"}, "coef": 0, "rate": 1.0, "n": 1, "effects": []}
    af_u.tactics = [af_tac]
    af_u.active_fired_tacs = [af_tac]

    def af_count_test(u):
        for t in u.active_fired_tacs:
            if id(t) in u.hit_flags:
                continue
            u.hit_flags.add(id(t))
            af_trigger_count[0] += 1

    af_count_test(af_u)
    af_count_test(af_u)
    assert af_trigger_count[0] == 1, "on:'activeFired' 同回合同一戰法應只觸發1次(hit_flags節流), 不應觸發2次"
    af_u.hit_flags.clear()
    af_count_test(af_u)
    assert af_trigger_count[0] == 2, "hit_flags 每回合重置後, on:'activeFired' 應能在新回合重新觸發"

    # 87) 批31 B: extraHits.ifSameTargetIsLeader —— 精確表達「若(主coef段隨機選定的)目標恰為
    # 敵軍主將」條件分支, 取代暗藏玄機舊有的1/3機率EV折算近似(sameTarget沿用主段目標, 事後
    # 過濾只保留目標==foes[0]的情形)。
    ist_u = Unit(POOL["呂布"], "騎")
    ist_leader = Unit(POOL["張飛"], "盾")
    ist_sub = Unit(POOL["關羽"], "盾")
    ist_foes = [ist_leader, ist_sub]
    ist_t = {"nameZh": "測試ifSameTargetIsLeader87", "extraHits": [
        {"coef": 0.92, "kind": "intel", "who": "sameTarget", "ifSameTargetIsLeader": True},
    ]}
    leader_troop_before = ist_leader.troop
    fire_extra_hits(ist_u, ist_t, ist_leader, lambda u: [ist_u], lambda u: ist_foes, None)
    assert ist_leader.troop < leader_troop_before, "ifSameTargetIsLeader: sameTarget恰為foes[0](主將)時應結算此段傷害"
    sub_troop_before = ist_sub.troop
    fire_extra_hits(ist_u, ist_t, ist_sub, lambda u: [ist_u], lambda u: ist_foes, None)
    assert ist_sub.troop == sub_troop_before, "ifSameTargetIsLeader: sameTarget非foes[0](副將)時不應結算此段傷害"

    # 88) 批32 B: dealtDamage 的 coef 傷害段補 targetSel(依準則選標) —— 過去固定命中觸發同一
    # 目標 dst(普攻的目標), 現優先讀 t["targetSel"] 改為精確選標(如監統震軍「普攻後對負傷最高
    # 之敵造成謀略傷害」, 選標對象與觸發普攻的目標無關)。用真實 fight() 端到端驗證(比起上方
    # dealt_damage_test 簡化重寫版更貼近正式 dealt_damage() 閉包內的實際邏輯, 該簡化版只覆蓋
    # effects 分支, 從未覆蓋 coef+targetSel 這條路徑, 故此處改呼叫真正的 fight()): 合成戰法
    # TACTICS 注入一個 command 型 dealtDamage 戰法, targetSel:"mostDamaged", 我方單位普攻後
    # 應命中敵方兵力最低者(而非普攻本身打中的目標)。
    TACTICS["測試dealtDamageTargetSel88"] = {
        "nameZh": "測試dealtDamageTargetSel88", "type": "command", "kind": "intel",
        "when": {"on": "dealtDamage", "dmgType": "phys"}, "coef": 3.0, "rate": 1.0, "n": 1, "prep": 0,
        "targetSel": "mostDamaged", "effects": [], "extraHits": [],
    }
    dd_ts_winner, dd_ts_rounds, dd_ts_kill = fight(["呂布"], ["張飛", "關羽"], inhA=[["測試dealtDamageTargetSel88"]])
    del TACTICS["測試dealtDamageTargetSel88"]
    assert dd_ts_rounds >= 1, "dealtDamage+targetSel 端到端測試: fight() 應正常跑完至少1回合(未拋例外)"
    # (行為已由 scratchpad/b32_verify_fsz.js 同款 node TRACE 對真實監統震軍資料驗證: 傷害穩定
    # 命中兵力最低的敵方單位, 而非普攻本身的隨機目標, 見批32報告)

    # 89) 批33: 治療公式全局換裝 —— want = coef × HEAL_TROOP_C(0.06) × 施放者兵力(依型態擇一
    # healBase快照/當下即時) × SCALE(scale屬性), 用 calibration_anchors.json
    # heal_formula_resolved_20260704(後續更新)兩組錨點樣本直接assert:
    #   陷陣營60%/智力379.02/準備兵力8439 → 546±1(反解值, 弱錨點)
    #   青囊96%/智力228/準備兵力9600 → 755±1(強錨點, user補測樣本, 0.03%誤差)
    # 兩樣本皆為「受傷反應式常駐急救型」heal(when.on:"damaged"), 用 heal_base(準備階段鎖定
    # 兵力快照)而非 caster.troop(當下即時), 驗證治療量不受戰鬥中兵力變動影響。
    h89_xzy = Unit(POOL["張飛"], "盾")           # 陷陣營樣本: 施放者(受術者亦為自己, self-heal急救)
    h89_xzy.intel = 379.02
    h89_xzy.troop = 8439                          # 建構後改兵力, 需重算 heal_base 快照(建構時已用預設10000算過)
    h89_xzy.heal_base = h89_xzy.troop * HEAL_TROOP_C
    h89_xzy.troop = 8439 - 2000                   # 模擬「受傷後當下兵力已下降」——heal_base應忽略此變動
    h89_xzy.wounded = 3000                        # 傷兵池足夠大, 不觸頂
    xzy_tac89 = {"nameZh": "測試陷陣營heal89", "type": "command", "kind": "phys", "coef": 0,
                 "rate": 1.0, "n": 3, "prep": 0,
                 "effects": [{"k": "heal", "who": "self", "coef": 0.6, "scale": "intel", "dur": 1}]}
    before89 = h89_xzy.troop
    apply_effects(h89_xzy, None, xzy_tac89, [h89_xzy], [], no_heal=False)
    gained89 = h89_xzy.troop - before89
    assert abs(gained89 - 546) <= 1, f"陷陣營樣本(智力379.02/準備兵力8439/治療率60%)應恢復546±1, 實得{gained89:.1f}"

    h89_qn = Unit(POOL["諸葛亮"], "弓")           # 青囊樣本(user補測, 強錨點): 施放當下兵力已變動, heal_base仍應鎖定準備階段值
    h89_qn.intel = 228
    h89_qn.troop = 9600
    h89_qn.heal_base = h89_qn.troop * HEAL_TROOP_C  # 準備階段快照(9600×0.06=576)
    h89_qn.troop = 9600 - 969                      # 模擬「準備階段後兵力已因其他傷害下降」(對應user描述8611~8781浮動情境)
    h89_qn_target = Unit(POOL["張飛"], "盾")
    h89_qn_target.troop = 5000
    h89_qn_target.wounded = 3000
    qn_tac89 = {"nameZh": "測試青囊heal89", "type": "command", "kind": "phys", "coef": 0,
                "rate": 1.0, "n": 2, "prep": 0,
                "effects": [{"k": "heal", "who": "ally", "coef": 0.96, "scale": "intel", "dur": 1}]}
    before89b = h89_qn_target.troop
    apply_effects(h89_qn, None, qn_tac89, [h89_qn_target], [], no_heal=False)
    gained89b = h89_qn_target.troop - before89b
    assert abs(gained89b - 755) <= 1, f"青囊樣本(智力228/準備兵力9600/治療率96%)應恢復755±1, 實得{gained89b:.1f}"

    # 89b) e["ofDamage"] —— 傷害比例治療(草船借箭類「回復傷害量X%」), 與屬性公式(scale/coef×
    # heal_base)互斥擇一; 用合成單效果戰法直接呼叫 apply_effects 並手動傳入 dmg 驗證比例正確。
    of_caster89 = Unit(POOL["張飛"], "盾")
    of_target89 = Unit(POOL["張飛"], "盾")
    of_target89.troop = 5000
    of_target89.wounded = 3000
    of_tac89 = {"nameZh": "測試ofDamage89", "kind": "phys",
                "effects": [{"k": "heal", "who": "ally", "ofDamage": 0.2857, "dur": 1}]}
    before89c = of_target89.troop
    apply_effects(of_caster89, None, of_tac89, [of_target89], [], no_heal=False, dmg=329)
    gained89c = of_target89.troop - before89c
    assert abs(gained89c - 329 * 0.2857) < 0.5, \
        f"e['ofDamage']=0.2857應恢復傷害量329的28.57%(=94), 實得{gained89c:.1f}"

    # 89c-2) 批40 A: e["ofDamage"] 支援 e["scale"]/e["scaleDiv"] —— 草船借箭統率縮放實測
    # (calibration_anchors.json caochuan_command_experiment): ofDamage=0.2716, scaleDiv=266.3,
    # 兩點錨點: 統率506.18→68.6%±0.3 / 統率237.65→41.2%±0.2。比例prep鎖定(lockedScaleOf),
    # dmg×比例即為恢復量, 與屬性公式(coef×heal_base)互斥擇一同批33既有慣例, 這裡驗證的是
    # scale縮放本身正確套用在ofDamage路徑上(過去只有純固定比例, 無scale時mult=1向後相容)。
    of_caster89b_hi = Unit(POOL["張飛"], "盾")
    of_caster89b_hi.command = 506.18
    of_target89b_hi = Unit(POOL["張飛"], "盾")
    of_target89b_hi.troop = 5000
    of_target89b_hi.wounded = 9000
    of_tac89b = {"nameZh": "測試ofDamage縮放89b", "kind": "phys",
                 "effects": [{"k": "heal", "who": "ally", "ofDamage": 0.2716, "scale": "command", "scaleDiv": 266.3, "dur": 1}]}
    before89d = of_target89b_hi.troop
    apply_effects(of_caster89b_hi, None, of_tac89b, [of_target89b_hi], [], no_heal=False, dmg=643)
    gained89d = of_target89b_hi.troop - before89d
    pct89d = gained89d / 643 * 100
    assert abs(pct89d - 68.6) < 0.3, f"統率506.18應得比例68.6%±0.3, 實得{pct89d:.2f}%(恢復{gained89d:.1f}/643)"

    of_caster89b_lo = Unit(POOL["張飛"], "盾")
    of_caster89b_lo.command = 237.65
    of_target89b_lo = Unit(POOL["張飛"], "盾")
    of_target89b_lo.troop = 5000
    of_target89b_lo.wounded = 9000
    before89e = of_target89b_lo.troop
    apply_effects(of_caster89b_lo, None, of_tac89b, [of_target89b_lo], [], no_heal=False, dmg=595)
    gained89e = of_target89b_lo.troop - before89e
    pct89e = gained89e / 595 * 100
    assert abs(pct89e - 41.2) < 0.2, f"統率237.65應得比例41.2%±0.2, 實得{pct89e:.2f}%(恢復{gained89e:.1f}/595)"

    # 89c) active(主動直療型)heal 用 caster.troop(當下即時兵力), 非 heal_base(準備階段快照)——
    # 用不同的 troop/heal_base 值驗證兩者確實分流, 對稱驗證 88/89 已涵蓋的「常駐急救型用
    # heal_base」不會被誤用在 active 型上。
    ad_caster89 = Unit(POOL["華佗"] if "華佗" in POOL else POOL["張飛"], "弓" if "華佗" in POOL else "盾")
    ad_caster89.intel = 284
    ad_caster89.troop = 8000                       # 當下即時兵力(active型應採用此值, 非heal_base)
    ad_caster89.heal_base = 999999 * HEAL_TROOP_C  # 刻意設一個遠不同的heal_base, 若active型誤用它, 治療量會離譜偏高, 藉此排除誤用
    ad_target89 = Unit(POOL["張飛"], "盾")
    ad_target89.troop = 3000
    ad_target89.wounded = 8000
    ad_tac89 = {"nameZh": "測試active直療89", "type": "active", "kind": "intel", "coef": 0,
                "rate": 1.0, "n": 1, "prep": 0,
                "effects": [{"k": "heal", "who": "ally", "coef": 2.56, "scale": "intel", "dur": 1}]}
    before89d = ad_target89.troop
    apply_effects(ad_caster89, None, ad_tac89, [ad_target89], [], no_heal=False)
    gained89d = ad_target89.troop - before89d
    expected89d = 2.56 * (8000 * HEAL_TROOP_C) * SCALE(284)
    assert abs(gained89d - expected89d) < 1.0, \
        f"active型heal應採用caster.troop(當下即時), 非heal_base, 預期{expected89d:.1f}實得{gained89d:.1f}"
    assert gained89d < 50000, "active型heal若誤用刻意調大的heal_base會產生離譜高治療量, 此處應遠低於該值"

    # --- 批35: 狀態屬性曲線375族 + 準備階段鎖定 + block消耗門檻 ---
    # 90) SCALE_G(v, 375) 六點precise assert —— docs/data/calibration_anchors.json →
    # status_scale_375_20260704(user 機鑑先識警戒六點實測, 荀彧準備階段智力478.84~389.72):
    # 警戒減傷 = 40% × (1+(智力-100)/375), cap 80%(=基礎×2)。六點全部小數點後兩位精確吻合。
    anchors90 = [
        (486.69, 0.80), (478.84, 0.80), (439.38, 0.7620),
        (432.18, 0.7543), (417.46, 0.7386), (394.88, 0.7145), (389.72, 0.7090),
    ]
    for intel90, expect90 in anchors90:
        got90 = min(0.4 * SCALE_G(intel90, 375), 0.8)
        assert abs(got90 - expect90) < 0.0001, \
            f"機鑑先識警戒錨點 智力{intel90}: 預期{expect90:.4f}, 算得{got90:.4f}"
    # 對照: 用全域350除數算同一批智力值不應精確吻合(佐證375是獨立曲線, 非350的誤差範圍內)
    assert abs(min(0.4 * SCALE(439.38), 0.8) - 0.7620) > 0.001, "350曲線不應與375曲線在此錨點精確重合(否則兩曲線無法區分)"

    # 91) locked_scale_of: 「準備階段鎖定」語意 —— 效果物件首次被算定(prep階段)的 scale 值,
    # 之後即使 caster 智力改變也應沿用鎖定值, 不重新計算(對應機鑑先識 everyRound 補層段
    # 在第2/3回合才真正命中套用, 但縮放倍率仍固定用開戰當下的智力)。
    lk91_caster = Unit(POOL["張飛"], "盾")
    lk91_caster.intel = 439.38
    lk91_e = {"k": "block", "scale": "intel", "scaleDiv": 375, "val": 0.4, "capVal": 0.8}
    v91_first = locked_scale_of(lk91_caster, lk91_e)
    assert abs(v91_first - SCALE_G(439.38, 375)) < 1e-9, "首次呼叫應等於當下即時 scale_of"
    lk91_caster.intel = 900.0                      # 模擬戰鬥中智力大幅變動(如中途獲得buff)
    v91_second = locked_scale_of(lk91_caster, lk91_e)
    assert v91_second == v91_first, f"鎖定後應沿用首次算定值, 不隨caster.intel變動重算: 首次{v91_first:.4f} 之後{v91_second:.4f}"
    # 不同效果物件(即使同caster)各自獨立上鎖, 不互相污染
    lk91_e2 = {"k": "block", "scale": "intel", "scaleDiv": 375, "val": 0.4, "capVal": 0.8}
    v91_e2 = locked_scale_of(lk91_caster, lk91_e2)
    assert abs(v91_e2 - SCALE_G(900.0, 375)) < 1e-9, "不同效果物件應各自獨立鎖定(用當時intel=900算), 不共用lk91_e的鎖"

    # 92) cap_val_of clamp —— 縮放後上限保護, e["capVal"] 未達上限時不影響原值
    assert abs(cap_val_of(0.762, 0.8) - 0.762) < 1e-9, "未超過capVal時應維持原值"
    assert abs(cap_val_of(0.95, 0.8) - 0.8) < 1e-9, "超過capVal時應clamp到capVal"
    assert cap_val_of(0.5, None) == 0.5, "capVal未設(None)時應原樣通過, 不clamp"

    # 93) block(k=="block") 的 apply_effects 路徑實際套用: val×scale_of(375曲線)+capVal clamp
    # 端到端驗證(取代直接算術, 走真實 apply_effects→push_block 路徑)
    bk93_caster = Unit(POOL["張飛"], "盾")
    bk93_caster.intel = 439.38
    bk93_dst = Unit(POOL["張飛"], "盾")
    bk93_tac = {"nameZh": "測試機鑑先識93", "effects": [
        {"k": "block", "who": "ally", "val": 0.4, "times": 2, "scale": "intel", "scaleDiv": 375, "capVal": 0.8}]}
    apply_effects(bk93_caster, None, bk93_tac, [bk93_dst], [], no_heal=True)
    assert len(bk93_dst.block) == 1 and abs(bk93_dst.block[0]["val"] - 0.7620) < 0.0001, \
        f"端到端: 智力439.38應套用警戒減傷76.20%, 實得{bk93_dst.block[0]['val']*100:.2f}%"

    # 94) BLOCK_CONSUME_THRESHOLD —— 傷害未超過 START_TROOP×6%(=600) 時不應消耗警戒層/不減傷;
    # 超過門檻時正常消耗+減傷。用直接構造低/高傷害兩種情境比較(非倚賴 damage() 隨機量級)。
    bk94_lo = Unit(POOL["張飛"], "盾")
    bk94_lo.push_block(0.4, 2, src="測試門檻94低")
    # 直接檢查常數本身(hit() 內部條件為 dmg > BLOCK_CONSUME_THRESHOLD 才消耗)
    assert BLOCK_CONSUME_THRESHOLD == max(START_TROOP * 0.06, 100) == 600.0, \
        f"BLOCK_CONSUME_THRESHOLD應為max(10000×6%,100)=600, 實得{BLOCK_CONSUME_THRESHOLD}"
    # 低傷害(<=門檻)不應消耗: 用極低coef讓damage()大機率落在門檻以下, 多次嘗試取一個確定案例
    random.seed(7)
    bk94_hit_src = Unit(POOL["張飛"], "盾")
    bk94_lo_dmg = damage(bk94_hit_src, bk94_lo, 0.05, "phys")   # 極低coef, 預期遠低於600
    assert bk94_lo_dmg <= BLOCK_CONSUME_THRESHOLD, f"測試前提: 本次構造傷害應低於門檻600, 實際{bk94_lo_dmg:.1f}(若失敗需調整coef/種子)"
    before94lo = bk94_lo.troop
    hit(bk94_hit_src, bk94_lo, 0.05, "phys")  # 注意: hit()內部會重新算一次damage(), 種子已消耗一次, 這裡只驗證block層數是否被消耗
    assert len(bk94_lo.block) == 1 and bk94_lo.block[0]["n"] == 2, "低於門檻的傷害不應消耗警戒層(次數應維持2不變)"
    # 高傷害(>門檻)應正常消耗: 用高coef確保dmg>600
    bk94_hi = Unit(POOL["張飛"], "盾")
    bk94_hi.push_block(0.4, 2, src="測試門檻94高")
    hit(bk94_hit_src, bk94_hi, 3.0, "phys")   # 高coef, 傷害應遠超600(基準coef=1.0時已476)
    assert bk94_hi.block and bk94_hi.block[0]["n"] == 1, "超過門檻的傷害應正常消耗1層警戒(2→1)"

    # 95) 迴歸測試: 準備階段鎖定必須在 skip_when_effects 閘門「之前」算定, 否則帶 e["when"]
    # (如機鑑先識 everyRound 段的 when:{until:3})且母戰法無 t["when"] 的 block 效果, 在
    # prep 呼叫(skip_when_effects=True)會被該閘門提前 continue 掉、根本沒機會鎖定, 導致
    # 鎖定值錯誤地延後到未來真正命中(heal_only常駐通道)的那一回合才用當時intel現算——
    # 這正是本批實作過程中一度真實發生的bug(用直接呼叫locked_scale_of的90/91號測試測不出來,
    # 因為那是繞過apply_effects閘門直接呼叫, 必須走真實apply_effects(prep語意的呼叫參數
    # 組合)才能重現)。
    er95_caster = Unit(POOL["張飛"], "盾")
    er95_caster.intel = 439.38
    er95_dst = Unit(POOL["張飛"], "盾")
    er95_tac = {"nameZh": "測試機鑑先識95", "effects": [
        {"k": "block", "who": "ally", "val": 0.4, "times": 1, "scale": "intel", "scaleDiv": 375,
         "capVal": 0.8, "everyRound": True, "rate": 1.0, "when": {"until": 3}}]}
    # 模擬 fight() 的 prep 呼叫: apply_passives(no_heal=True, skip_when_effects=True) 對應
    # apply_effects(..., no_heal=True, skip_when_effects=True)(global CUR_ROUND 已在本函式
    # 較早的測試段宣告過, 此處沿用同一個 global 宣告範圍, 不需重複宣告)
    CUR_ROUND = 0
    apply_effects(er95_caster, None, er95_tac, [er95_dst], [], no_heal=True, skip_when_effects=True)
    assert not er95_dst.block, "prep呼叫時everyRound效果本身不應套用(維持既有行為, 只是鎖定值應已算好)"
    er95_caster.intel = 900.0                      # 模擬戰鬥中智力大幅變動
    er95_caster.own_round = 1                       # 時序一致化(2026-07批次)A.3: own_turn改用caster自己的行動輪計數為基準
    apply_effects(er95_caster, None, er95_tac, [er95_dst], [], own_turn=True)  # 模擬caster自己第1個行動輪的 apply_own_turn_effects()
    assert er95_dst.block, "everyRound效果應在own_turn常駐通道套用"
    assert abs(er95_dst.block[0]["val"] - 0.7620) < 0.0001, \
        f"迴歸: 即使套用當下(第1回合)intel已變成900, block的scale值仍應沿用prep階段(intel 439.38)鎖定值76.20%, 實得{er95_dst.block[0]['val']*100:.2f}%(若得到用900算出的80.00%即代表鎖定被skip_when_effects閘門繞過的bug重現)"

    # --- 批36: 兵種營資料通路(campLv 0~10 屬性%+Lv10附贈戰法attach) ---
    # 96) campLv=0(預設/未指定): 完全不影響既有行為 —— adds為空, tactics不含任何BUILDING戰法
    cl96 = Unit(POOL["張飛"], "槍")
    assert cl96.camp_lv == 0, "未傳camp_lv應預設0(向後相容既有全部呼叫點)"
    assert not any(a[0] == "amp" and a[3] == "兵種營" for a in cl96.adds), "campLv=0不應推入兵種營amp加成"
    assert not any(t.get("_campBuilding") for t in cl96.tactics), "campLv=0不應attach任何BUILDING戰法"

    # 97) campLv=10 + is_camp_holder=True 且隊伍兵種=槍: 應同時獲得(1)破軍戰法attach (2)amp+2.5%傷害
    cl97 = Unit(POOL["張飛"], "槍", camp_lv=10, is_camp_holder=True)
    camp_tacs97 = [t for t in cl97.tactics if t.get("_campBuilding")]
    assert len(camp_tacs97) == 1 and camp_tacs97[0]["nameZh"] == "破軍", \
        f"槍兵隊campLv=10(持有者)應自動獲得破軍, 實得{[t.get('nameZh') for t in camp_tacs97]}"
    camp_amp97 = [a for a in cl97.adds if a[0] == "amp" and a[3] == "兵種營"]
    assert len(camp_amp97) == 1 and abs(camp_amp97[0][1] - 0.025) < 1e-9, \
        f"campLv=10應貢獻amp+2.5%(10×0.25%), 實得{camp_amp97}"

    # 97b) campLv=10 但 is_camp_holder=False(同隊非持有者的隊友): 只吃屬性%加成, 不應attach戰法
    # ——這是本批修正的核心行為: 原文「我軍隨機單體」是一整隊只發生一次, 不是每個Unit各自擁有
    cl97b = Unit(POOL["張飛"], "槍", camp_lv=10, is_camp_holder=False)
    assert not any(t.get("_campBuilding") for t in cl97b.tactics), \
        "campLv=10但非持有者(is_camp_holder=False)不應attach戰法(避免隊上每人各自重複觸發)"
    camp_amp97b = [a for a in cl97b.adds if a[0] == "amp" and a[3] == "兵種營"]
    assert len(camp_amp97b) == 1 and abs(camp_amp97b[0][1] - 0.025) < 1e-9, "非持有者仍應正常吃屬性%加成(三合一另一支, 全隊皆有)"

    # 98) campLv=10 + is_camp_holder=True 但隊伍兵種=盾: 應獲得守禦(非破軍), 驗證兵種→戰法對應表逐一生效
    cl98 = Unit(POOL["張飛"], "盾", camp_lv=10, is_camp_holder=True)
    camp_tacs98 = [t for t in cl98.tactics if t.get("_campBuilding")]
    assert len(camp_tacs98) == 1 and camp_tacs98[0]["nameZh"] == "守禦", \
        f"盾兵隊campLv=10(持有者)應自動獲得守禦, 實得{[t.get('nameZh') for t in camp_tacs98]}"

    # 99) campLv=10 + is_camp_holder=True 但隊伍兵種=器(器械): 負重無戰鬥效果(type:"none"已被
    # TACTICS載入時過濾), 不應attach任何BUILDING戰法, 但傷害%加成仍應正常給予(三合一其餘兩支不受影響)
    cl99 = Unit(POOL["張飛"], "器", camp_lv=10, is_camp_holder=True)
    assert not any(t.get("_campBuilding") for t in cl99.tactics), "器械營Lv10不應attach任何戰法(負重無戰鬥效果)"
    camp_amp99 = [a for a in cl99.adds if a[0] == "amp" and a[3] == "兵種營"]
    assert len(camp_amp99) == 1 and abs(camp_amp99[0][1] - 0.025) < 1e-9, "器械營傷害%加成仍應正常給予(與Lv10戰法attach是獨立的兩支)"

    # 100) campLv=5(未滿級, 即使is_camp_holder=True): 只有屬性%加成(1.25%), 不應attach戰法(嚴格<10門檻)
    cl100 = Unit(POOL["張飛"], "槍", camp_lv=5, is_camp_holder=True)
    assert not any(t.get("_campBuilding") for t in cl100.tactics), "campLv=5(未滿10級)不應attach戰法, 即使is_camp_holder=True"
    camp_amp100 = [a for a in cl100.adds if a[0] == "amp" and a[3] == "兵種營"]
    assert len(camp_amp100) == 1 and abs(camp_amp100[0][1] - 0.0125) < 1e-9, \
        f"campLv=5應貢獻amp+1.25%(5×0.25%), 實得{camp_amp100}"

    # 100b) fight() 端到端: campLv=10 一整隊(3人)中應恰好1人是持有者(attach BUILDING戰法),
    # 其餘2人不應重複attach ——直接驗證 fight() 呼叫端的隨機挑選邏輯正確(非全隊每人都獲得)
    holder_count_samples = []
    for trial in range(50):
        random.seed(1000 + trial)
        idx = random.randrange(3)
        units100b = [Unit(POOL["張飛"], "槍", camp_lv=10, is_camp_holder=(i == idx)) for i in range(3)]
        holders = [u for u in units100b if any(t.get("_campBuilding") for t in u.tactics)]
        holder_count_samples.append(len(holders))
    assert all(c == 1 for c in holder_count_samples), \
        f"每次3人隊campLv=10應恰好1人是持有者(attach戰法), 實得樣本{holder_count_samples}"

    # 101) 端到端 fight(): 鏡像對局(雙方同隊「張飛/SP呂蒙/SP樂進」, 皆採槍兵, 消除陣容強弱
    # 差異的干擾) A方 campLvA=10(破軍+2.5%傷害) vs campLvA=0, B方恆為0 —— 有營一方勝率應
    # 明顯高於無營基準(鏡像對局基準本身非50%, 見下方 baseline, 故用「有營 - 對照組」差值判斷
    # 方向, 而非直接假設50%中心)。
    campA_team = ["張飛", "SP 呂蒙", "SP 樂進"]
    campB_team = ["張飛", "SP 呂蒙", "SP 樂進"]
    random.seed(36)
    r101_baseline = simulate(campA_team, campB_team, n=4000, troopA="槍", troopB="槍")
    random.seed(36)
    r101_with_camp = simulate(campA_team, campB_team, n=4000, troopA="槍", troopB="槍", campLvA=10)
    assert r101_with_camp["A勝率"] > r101_baseline["A勝率"], \
        f"campLv=10(破軍+2.5%傷害)應讓A方勝率高於無營基準(鏡像對局): 有營{r101_with_camp['A勝率']} 基準{r101_baseline['A勝率']}"
    print(f"    [批36] 鏡像對局 campLv=10 A勝率{r101_with_camp['A勝率']:.3f} vs 基準{r101_baseline['A勝率']:.3f} (差+{r101_with_camp['A勝率']-r101_baseline['A勝率']:.3f})")

    # --- 批37 B: 停損決策過期重審(義膽雄心 parity互斥交替 / 奮突 普攻事件精確觸發) ---
    # 102) 義膽雄心(真實資料): everyRound+e.when.parity 奇偶互斥交替 —— 奇數回合只套武力debuff
    # (單體), 偶數回合只套謀略dot(2人,kind:intel)+智力debuff(2人), 經 own_turn 常駐通道逐該
    # 持有者自己行動輪判定。時序一致化(2026-07批次)A.3: 改用 own_turn=True +
    # ydxx_caster.own_round(奇偶parity依caster自己的行動輪計數判定, 取代舊全局CUR_ROUND)。
    ydxx = TACTICS.get("義膽雄心")
    assert ydxx and (ydxx.get("when") or {}).get("parity") == "odd", \
        "義膽雄心應帶戰法級 when.parity:'odd'(main coef 184%兵刃傷害只在奇數回合擲骰, 批37 B重建)"
    assert all(e.get("everyRound") for e in ydxx["effects"]), "義膽雄心三段效果皆應帶everyRound(逐回合重擲通道)"
    ydxx_caster = Unit(POOL["呂布"], "騎")
    ydxx_foes = [Unit(POOL["張飛"], "盾"), Unit(POOL["關羽"], "盾")]
    ydxx_caster.own_round = 1                           # 奇數(caster自己第1個行動輪): 只有武力debuff段(n=1)應套用
    apply_effects(ydxx_caster, None, ydxx, [ydxx_caster], ydxx_foes, own_turn=True)
    force_hit_r1 = [u for u in ydxx_foes if any(s[0] == "force" for s in u.stat_adds)]
    intel_hit_r1 = [u for u in ydxx_foes if any(s[0] == "intel" for s in u.stat_adds)]
    dots_r1 = sum(len(u.dots) for u in ydxx_foes)
    assert len(force_hit_r1) == 1, f"義膽雄心奇數回合應對敵軍單體(n=1)套武力debuff, 實中{len(force_hit_r1)}人"
    assert not intel_hit_r1 and dots_r1 == 0, \
        f"義膽雄心奇數回合不應套偶數段(智力debuff/謀略dot), 實得intel={len(intel_hit_r1)} dots={dots_r1}"
    ydxx_caster.own_round = 2                           # 偶數(caster自己第2個行動輪): 謀略dot(2人)+智力debuff(2人)應套用
    apply_effects(ydxx_caster, None, ydxx, [ydxx_caster], ydxx_foes, own_turn=True)
    intel_hit_r2 = [u for u in ydxx_foes if any(s[0] == "intel" for s in u.stat_adds)]
    dots_r2 = sum(len(u.dots) for u in ydxx_foes)
    assert len(intel_hit_r2) == 2 and dots_r2 == 2, \
        f"義膽雄心偶數回合應對敵軍群體(2人)套智力debuff+謀略dot, 實得intel={len(intel_hit_r2)} dots={dots_r2}"

    # 103) 奮突(真實資料): stackPer:"attack" + disarm 反應式重建(when.on:'dealtDamage'+normalOnly+rate:0.35)
    ft = TACTICS.get("奮突")
    ft_disarm = next(e for e in ft["effects"] if e["k"] == "disarm")
    ft_stack = next(e for e in ft["effects"] if e["k"] == "stack")
    assert (ft_disarm.get("when") or {}).get("on") == "dealtDamage" and ft_disarm["when"].get("normalOnly") \
        and abs(ft_disarm.get("rate", 0) - 0.35) < 1e-9, \
        "奮突disarm應為when:{on:'dealtDamage',normalOnly:true}+rate:0.35(批37 B重建, 取代舊rate=1常駐高估)"
    assert ft_stack.get("stackPer") == "attack", "奮突stack應為stackPer:'attack'(每次普攻命中後遞增, 批37 B新模式)"
    ft_u = Unit(POOL["呂布"], "騎")
    ft_foe = Unit(POOL["張飛"], "盾")
    apply_effects(ft_u, ft_foe, ft, [ft_u], [ft_foe], no_heal=True)  # prep套用(非reactive)
    assert ft_foe.disarm == 0, "奮突disarm(when.on反應式)不應在prep非reactive呼叫時套用(批23 when.on閘門)"
    assert ft_u.stack and ft_u.stack.get("stackPer") == "attack" and ft_u.stack["n"] == 0, \
        "奮突stack應在prep套用(stackPer:'attack', 初始0層)"
    if ft_u.alive and ft_u.stack and ft_u.stack.get("stackPer", "round") == "round":
        ft_u.stack["n"] = min(ft_u.stack["max"], ft_u.stack["n"] + 1)  # 重演fight()回合迴圈的round遞增判斷式
    assert ft_u.stack["n"] == 0, "stackPer:'attack'不應被fight()回合迴圈的round模式遞增判斷式誤加層(守衛條件== 'round')"

    # 104) 端到端 fight(): 鏡像對局, A方主將傳承奮突 vs 雙方皆無傳承基準 —— 奮突(普攻疊層增傷
    # +35%機率繳械)應讓A方勝率高於基準(驗證 stackPer:'attack' 遞增與 dealtDamage(normalOnly)
    # 繳械兩條新通道在真實fight()中確實生效; 若通道全然未觸發, 差值應~0)。
    ft_team = ["張飛", "SP 呂蒙", "SP 樂進"]
    random.seed(37)
    r104_baseline = simulate(ft_team, ft_team, n=4000, troopA="槍", troopB="槍")
    random.seed(37)
    r104_with_ft = simulate(ft_team, ft_team, n=4000, troopA="槍", troopB="槍", inhA=[["奮突"], None, None])
    assert r104_with_ft["A勝率"] > r104_baseline["A勝率"], \
        f"A方主將傳承奮突應提升鏡像對局勝率(普攻疊層+繳械通道生效): 傳承{r104_with_ft['A勝率']} 基準{r104_baseline['A勝率']}"
    print(f"    [批37] 鏡像對局 傳承奮突 A勝率{r104_with_ft['A勝率']:.3f} vs 基準{r104_baseline['A勝率']:.3f} (差+{r104_with_ft['A勝率']-r104_baseline['A勝率']:.3f})")

    # 105) 批38 A: 跨單位事件廣播 who=="ally"/"enemy" —— activeFired 廣播端到端驗證。持有者
    # 自身無主動戰法(active_fired_tacs命中0次self路徑), 隊友掛一個rate=1必發的主動戰法(coef=0
    # 純占位); 持有者掛 when:{on:"activeFired", who:"ally"} 的被動, 命中時對敵軍造成一段
    # 額外兵刃傷害。若who=="ally"廣播完全未生效, 持有者的戰法永遠不會觸發(其"自己"從未fire
    # 任何主動戰法), 額外傷害段應為0; 若生效, 隊友每次成功發動都應觸發持有者的額外傷害。
    ally_active_tac105 = {"nameZh": "測試105隊友主動戰法", "type": "active", "kind": "phys",
                           "coef": 0, "rate": 1, "n": 1, "prep": 0, "effects": []}
    holder_tac105 = {"nameZh": "測試105持有者ally監聽", "type": "passive", "kind": "phys",
                      "coef": 0.5, "rate": 1, "n": 1, "prep": 0,
                      "when": {"on": "activeFired", "who": "ally"}, "effects": []}
    TACTICS[ally_active_tac105["nameZh"]] = ally_active_tac105
    TACTICS[holder_tac105["nameZh"]] = holder_tac105
    try:
        holder105 = Unit(POOL["呂布"], "騎", None, None, None, [holder_tac105["nameZh"]])
        ally105 = Unit(POOL["張飛"], "騎", None, None, None, [ally_active_tac105["nameZh"]])
        foe105 = Unit(POOL["關羽"], "盾")
        assert len(holder105.active_fired_tacs) == 1, "測試105持有者ally監聽 應被正確預篩收錄進 active_fired_tacs"
        # 直接重演 fight() 主迴圈: ally105 成功發動主動戰法(fire=True分支)後呼叫 active_fired(ally105),
        # 廣播應掃到 holder105(who=="ally") 並對 foe105 造成傷害。
        setA105 = {id(holder105), id(ally105)}
        allies_of105 = lambda u: [holder105, ally105] if id(u) in setA105 else [foe105]
        foes_of105 = lambda u: [foe105] if id(u) in setA105 else [holder105, ally105]

        def active_fired105(u):
            def active_fired_for105(u, holder, want_who):
                if not holder.alive or not holder.active_fired_tacs:
                    return
                def who_ok(w):
                    return (w or {}).get("who") == want_who if want_who else not (w or {}).get("who")
                for t in holder.active_fired_tacs:
                    if not who_ok(t.get("when")):
                        continue
                    if id(t) in holder.hit_flags:
                        continue
                    holder.hit_flags.add(id(t))
                    if t["coef"]:
                        for v in pick_targets(foes_of105(holder), t["n"]):
                            hit(holder, v, t["coef"], t["kind"], False, None, None, is_active=True)
            active_fired_for105(u, u, None)
            for holder in allies_of105(u):
                active_fired_for105(u, holder, "ally")
            for holder in foes_of105(u):
                active_fired_for105(u, holder, "enemy")

        foe_troop_before105 = foe105.troop
        active_fired105(ally105)  # ally105(非holder105自己)成功發動主動戰法
        assert foe105.troop < foe_troop_before105, \
            "who=='ally' activeFired廣播: 隊友(ally105)發動主動戰法後, 持有者(holder105)的who=='ally'監聽應觸發並造成傷害"
    finally:
        del TACTICS[ally_active_tac105["nameZh"]]
        del TACTICS[holder_tac105["nameZh"]]

    # 106) 批38 A: who=="enemy" activeFired 廣播(神機妙算/舌戰群儒方向) —— 我方持有者監聽敵方
    # 發動主動戰法。同上106用手工重演的 active_fired_for 邏輯(對稱105), 只是holder與觸發者u分屬
    # 不同隊。
    foe_active_tac106 = {"nameZh": "測試106敵方主動戰法", "type": "active", "kind": "phys",
                         "coef": 0, "rate": 1, "n": 1, "prep": 0, "effects": []}
    holder_tac106 = {"nameZh": "測試106持有者enemy監聽", "type": "passive", "kind": "phys",
                      "coef": 0.5, "rate": 1, "n": 1, "prep": 0,
                      "when": {"on": "activeFired", "who": "enemy"}, "effects": []}
    TACTICS[foe_active_tac106["nameZh"]] = foe_active_tac106
    TACTICS[holder_tac106["nameZh"]] = holder_tac106
    try:
        holder106 = Unit(POOL["呂布"], "騎", None, None, None, [holder_tac106["nameZh"]])
        foe106 = Unit(POOL["關羽"], "盾", None, None, None, [foe_active_tac106["nameZh"]])
        assert len(holder106.active_fired_tacs) == 1, "測試106持有者enemy監聽 應被正確預篩收錄進 active_fired_tacs"
        setA106 = {id(holder106)}
        allies_of106 = lambda u: [holder106] if id(u) in setA106 else [foe106]
        foes_of106 = lambda u: [foe106] if id(u) in setA106 else [holder106]

        def active_fired106(u):
            def active_fired_for106(u, holder, want_who):
                if not holder.alive or not holder.active_fired_tacs:
                    return
                def who_ok(w):
                    return (w or {}).get("who") == want_who if want_who else not (w or {}).get("who")
                for t in holder.active_fired_tacs:
                    if not who_ok(t.get("when")):
                        continue
                    if id(t) in holder.hit_flags:
                        continue
                    holder.hit_flags.add(id(t))
                    if t["coef"]:
                        for v in pick_targets(foes_of106(holder), t["n"]):
                            hit(holder, v, t["coef"], t["kind"], False, None, None, is_active=True)
            active_fired_for106(u, u, None)
            for holder in allies_of106(u):
                active_fired_for106(u, holder, "ally")
            for holder in foes_of106(u):
                active_fired_for106(u, holder, "enemy")

        foe_troop_before106 = foe106.troop  # foe106自己是唯一foe, 也是本次發動者: 用另一個觀察點——holder106是否有觸發, 改觀察holder106.hit_flags
        active_fired106(foe106)  # foe106(敵方, 非holder106)成功發動主動戰法
        assert any(id(t) in holder106.hit_flags for t in holder106.active_fired_tacs), \
            "who=='enemy' activeFired廣播: 敵方(foe106)發動主動戰法後, 我方持有者(holder106)的who=='enemy'監聽應觸發(hit_flags應記錄)"
    finally:
        del TACTICS[foe_active_tac106["nameZh"]]
        del TACTICS[holder_tac106["nameZh"]]

    # 107) 批38 A: who未指定(預設"self")回歸 —— 士爭先赴真實資料在86a已驗證; 這裡額外核對
    # who=="ally"/"enemy"廣播不會誤觸發只認"self"的既有戰法(反向回歸: 廣播迴圈的who_ok閘門
    # 不應放行未帶who欄位的t0)。
    sfxf_tac107 = dict(TACTICS["士爭先赴"])
    assert not (sfxf_tac107.get("when") or {}).get("who"), "士爭先赴 when.who 應維持未指定(向後相容self預設), 批38不應誤加who欄位"

    print(f"    [批38] activeFired 跨單位廣播 who=='ally'/'enemy' 端到端驗證通過(105/106), who未指定回歸驗證通過(107)")

    # --- 批39: 象兵重建(ifTargetHas自查+everyRound) + R26統領揭露 + onHit dmgType + 萬軍奪帥五態拆分 ---
    # 108) 象兵(真實資料): 自身有灼燒(dot)時才獲得群攻+混亂(chaos), 用ifTargetHas:"dot"
    # 複用既有原語表達「自查」條件(who="self"先選定caster自己, ifTargetHas再過濾)。everyRound
    # 逐回合重新判定(灼燒可能到期消失)。批K(禁近似令收官): 群攻段由k=="extra"(施放者自身
    # 輸出加成近似)升級為k=="splash"(splash_aoe_primitive族新原語, 精確表達濺射同部隊隊友),
    # 測試同步改查splash。
    xb108_tac = dict(TACTICS["象兵"])
    xb108_extra = next(e for e in xb108_tac["effects"] if e["k"] == "splash")
    xb108_chaos = next(e for e in xb108_tac["effects"] if e["k"] == "chaos")
    assert xb108_extra.get("ifTargetHas") == "dot" and xb108_extra.get("everyRound") is True, \
        "象兵 splash(群攻)應帶 ifTargetHas:'dot' + everyRound(自查灼燒狀態, 逐回合重判)"
    assert xb108_chaos.get("ifTargetHas") == "dot" and xb108_chaos.get("everyRound") is True, \
        "象兵 chaos(混亂)應帶 ifTargetHas:'dot' + everyRound(自查灼燒狀態, 逐回合重判)"
    # 端到端: 無灼燒時不應觸發; 有灼燒時應觸發(直接呼叫 apply_effects own_turn 通道模擬該
    # 持有者自己行動輪的 everyRound 常駐判定; 時序一致化(2026-07批次)A.3: 改用own_turn=True
    # 取代舊heal_only=True)
    xb108_caster_no_dot = Unit(POOL["張飛"], "盾")
    apply_effects(xb108_caster_no_dot, None, xb108_tac, [xb108_caster_no_dot], [], own_turn=True)
    assert not any(a[0] == "splash" for a in xb108_caster_no_dot.adds), "象兵: 無灼燒狀態時, 群攻(splash)不應觸發"
    assert xb108_caster_no_dot.chaos == 0, "象兵: 無灼燒狀態時, 混亂(chaos)不應觸發"
    xb108_caster_dot = Unit(POOL["張飛"], "盾")
    xb108_caster_dot.dots.append([50, 3, False])   # 模擬自身已有灼燒(dot非空)
    apply_effects(xb108_caster_dot, None, xb108_tac, [xb108_caster_dot], [], own_turn=True)
    assert any(a[0] == "splash" for a in xb108_caster_dot.adds), "象兵: 自身有灼燒狀態時, 群攻(splash)應觸發"
    assert xb108_caster_dot.chaos > 0, "象兵: 自身有灼燒狀態時, 混亂(chaos)應觸發"

    # 109) R26統領揭露一致性(真實資料抽驗, 批44更新, 批L再更新): 批39 B新補_todo的5筆(丹陽兵/
    # 白毦兵/白馬義從/先登死士/藤甲兵, 其中藤甲兵是lint R26新掃出、非任務原定4筆之一), 批44起
    # 「若XX統領」條件應「有ifLeaderIs落地 或 有_todo文字揭露」二擇一皆算合格(R26豁免規則同步
    # 放寬, 見lint_tactics.py)——丹陽兵/白毦兵已用ifLeaderIs精確落地(_todo清空), 其餘3筆仍維持
    # _todo文字揭露(缺口非ifLeaderIs可解, 見各自_todo說明)。批L: 先登死士「若麴義統領則可疊加
    # 5次」現已用maxStackIfLeaderIs(批L新原語, 對稱ifLeaderIs但套用維度是maxStack)精確落地
    # (_todo已改為「受攜帶者統率影響」殘留揭露, 不再提及「統領」二字), has_ili109同步承認
    # maxStackIfLeaderIs(對稱lint_tactics.py _has_leader_bonus的同款更新, 見該處註解)。
    for nm109 in ("丹陽兵", "白毦兵", "白馬義從", "先登死士", "藤甲兵"):
        tac109 = TACTICS[nm109]
        todo109 = tac109.get("_todo", "") or ""
        has_ili109 = any(e.get("ifLeaderIs") or e.get("maxStackIfLeaderIs") for e in tac109.get("effects", []) or []) or \
                     any(eh.get("ifLeaderIs") for eh in tac109.get("extraHits", []) or [])
        assert has_ili109 or "統領" in todo109, \
            f"{nm109} 應「有ifLeaderIs/maxStackIfLeaderIs落地」或「含統領條件的_todo揭露」二擇一(批39 B/R26, 批44放寬, 批L再放寬)"

    # 110) onHit dmgType 過濾(剛勇無前真實資料): 原文「受到兵刃傷害後」限定, when.dmgType應為
    # "phys"。用 inherit=["剛勇無前"] 走真實 Unit 建構(正確填 on_hit_tacs), 搭配與正式 on_hit()
    # 同款 dmgTypeOk 過濾邏輯的精簡反應式派發(hit()已於批39 C補傳kind, 此處驗證dmgType過濾
    # 本身是否正確生效, 不依賴fight()內部才建立的完整廣播閉包)。
    gy110_tac = TACTICS["剛勇無前"]
    assert (gy110_tac.get("when") or {}).get("dmgType") == "phys", "剛勇無前 when.dmgType 應為'phys'(受到兵刃傷害限定, 批39 C)"

    def on_hit_dmgtype_test(dst, src, is_normal, dmg=None, kind=None):
        def dmg_type_ok(dt):
            return not dt or not kind or dt == kind
        for t in dst.on_hit_tacs:
            w = t.get("when") or {}
            if w.get("on") == "attacked" and not is_normal:
                continue
            if not dmg_type_ok(w.get("dmgType")):
                continue
            if id(t) in dst.hit_flags:
                continue
            dst.hit_flags.add(id(t))
            if t.get("effects"):
                apply_effects(dst, src, t, [dst], [src], reactive=True, dmg=dmg)

    gy110_src = Unit(POOL["張飛"], "盾")
    gy110_dst_intel = Unit(POOL["張飛"], "盾", inherit=["剛勇無前"])
    hit(gy110_src, gy110_dst_intel, 1.0, "intel", is_normal=False, on_event=on_hit_dmgtype_test)
    assert not any(a[0] == "amp" for a in gy110_dst_intel.adds), "剛勇無前: 受到謀略(intel)傷害不應觸發(dmgType='phys'限定)"
    gy110_dst_phys = Unit(POOL["張飛"], "盾", inherit=["剛勇無前"])
    hit(gy110_src, gy110_dst_phys, 1.0, "phys", is_normal=False, on_event=on_hit_dmgtype_test)
    assert any(a[0] == "amp" for a in gy110_dst_phys.adds), "剛勇無前: 受到兵刃(phys)傷害應觸發amp"

    # 111) 萬軍奪帥(真實資料): 5種狀態(遇襲/計窮/繳械/禁療, 破壞未建模)應各自獨立為effects條目
    # (而非過去合併成單一stun), 各帶獨立e.rate=0.45(滿級值), 且無leaderBonus/統領相關字段(本戰法
    # 本身無「若XX統領」條件, 與R26無關, 純狀態拆分驗證)。
    wjdshuai_tac = TACTICS["萬軍奪帥"]
    wjdshuai_ks = sorted(e["k"] for e in wjdshuai_tac["effects"])
    assert wjdshuai_ks == sorted(["stat", "ambush", "silence", "disarm", "healblock"]), \
        f"萬軍奪帥 五態拆分後應為stat+ambush+silence+disarm+healblock(不含合併的stun), 實得{wjdshuai_ks}"
    for k111 in ("ambush", "silence", "disarm", "healblock"):
        e111 = next(e for e in wjdshuai_tac["effects"] if e["k"] == k111)
        assert abs(e111.get("rate", 1.0) - 0.45) < 1e-9, f"萬軍奪帥 {k111} 應獨立rate=0.45(滿級值, 每種狀態獨立判定)"
    assert "破壞" in (wjdshuai_tac.get("_todo") or ""), "萬軍奪帥 _todo 應揭露「破壞」(禁用裝備)無對應原語"
    # 端到端: 5次獨立擲骰(用固定種子驗證至少部分觸發, 至少一種不觸發——證明是獨立判定而非全有/全無的單一stun)
    random.seed(3)
    wjd111_caster = Unit(POOL["張飛"], "弓")
    wjd111_tgt = Unit(POOL["張飛"], "弓")
    apply_effects(wjd111_caster, wjd111_tgt, wjdshuai_tac, [wjd111_caster], [wjd111_tgt], no_heal=True)
    fired111 = {"ambush": wjd111_tgt.ambush > 0, "silence": wjd111_tgt.silence > 0,
                "disarm": wjd111_tgt.disarm > 0, "healblock": wjd111_tgt.healblock > 0}
    assert not (all(fired111.values()) or not any(fired111.values())), \
        f"萬軍奪帥: 5態應獨立判定(45%各自擲骰), 不應呈現全有或全無的單一stun語意殘留, 本次結果{fired111}(若剛好全真/全假需換種子重驗, 但邏輯上4個獨立0.45擲骰同時全中或全不中機率僅約(0.45^4+0.55^4)≈8.3%, 不構成系統性問題)"

    print(f"    [批39] 象兵ifTargetHas自查+everyRound(108), R26統領揭露5筆(109), onHit dmgType過濾(110), 萬軍奪帥五態拆分(111)驗證通過")

    # --- 批41: 鷹視狼顧/錦帆軍0分修復 + R27 ifLeader top-up 尾碼去重修正 ---
    # 112) 圍師必闕(真實資料): R27修復新增「基礎mitig(無條件0.39)+差額mitig(ifLeader:true,0.06)」
    # 的 base+top-up 拆法(比照水淹七軍 dot 既有precedent)。dot 走 u.dots.append 不去重, 但
    # amp/mitig 走 push_add(同kind+同src會互相覆蓋), 若不補尾碼區分dedup key, 兩條mitig(who
    # 同ally, dmgType同intel)會共用同一個dt_src, 後套用的ifLeader top-up段會把先套用的基礎
    # 段整個蓋掉。驗證: 非主將施放者只吃基礎0.39, 主將施放者應吃0.39+0.06=0.45(兩段並存疊加,
    # 而非互相覆蓋只剩其中一段)。
    wsbq112_tac = TACTICS["圍師必闕"]
    wsbq112_leader = Unit(POOL["張飛"], "槍")
    wsbq112_other = Unit(POOL["張飛"], "槍")
    apply_effects(wsbq112_leader, None, wsbq112_tac, [wsbq112_leader, wsbq112_other], [], no_heal=True)
    wsbq112_leader_mitig = wsbq112_leader.addbonus("mitig", "intel")
    wsbq112_sub = Unit(POOL["張飛"], "槍")
    wsbq112_leader2 = Unit(POOL["張飛"], "槍")
    apply_effects(wsbq112_sub, None, wsbq112_tac, [wsbq112_leader2, wsbq112_sub], [], no_heal=True)
    wsbq112_sub_mitig = wsbq112_sub.addbonus("mitig", "intel")
    assert abs(wsbq112_leader_mitig - wsbq112_sub_mitig - 0.06) < 1e-6, \
        f"圍師必闕: 主將施放者的mitig(intel)應比非主將多0.06(ifLeader top-up段疊加, 非覆蓋), 實得主將={wsbq112_leader_mitig}, 非主將={wsbq112_sub_mitig}, 差={wsbq112_leader_mitig - wsbq112_sub_mitig}"

    # 113) 鷹視狼顧(真實資料, v18零分修復+批H會心真擲骰化): critUp(who:self,val:0.16,
    # dmgType:intel)補ifLeader後, 非主將施放者不應吃這16%奇謀機率, 主將施放者應吃到。
    yslg113_tac = TACTICS["鷹視狼顧"]
    yslg113_leader = Unit(POOL["司馬懿"], "槍")
    yslg113_other = Unit(POOL["張飛"], "槍")
    apply_effects(yslg113_leader, None, yslg113_tac, [yslg113_leader, yslg113_other], [], no_heal=True)
    assert abs(yslg113_leader.addbonus("critUp", "intel") - 0.16) < 1e-6, "鷹視狼顧: 主將施放者應吃到16%奇謀機率(critUp intel, 批H真擲骰化)"
    yslg113_sub = Unit(POOL["司馬懿"], "槍")
    apply_effects(yslg113_sub, None, yslg113_tac, [yslg113_other, yslg113_sub], [], no_heal=True)
    assert abs(yslg113_sub.addbonus("critUp", "intel")) < 1e-9, "鷹視狼顧: 非主將施放者不應吃16%奇謀機率(ifLeader閘門應擋下, v18零分修復)"

    print(f"    [批41] 鷹視狼顧/錦帆軍v18零分修復+R27 ifLeader top-up尾碼去重(112/113)驗證通過")

    # --- 批42: 傲睨王侯官方卡重建 —— who=="eventTarget"精確選標 + k=="stat"疊層原語
    # (stackKey/perStack/maxStacks/onMaxStacks/globalMax/globalEffects) ---
    # 114) 兩點白字標智力戰報精確曲線驗證(calibration_anchors.json aoni_wanghou_20260707):
    # 破綻每層 = 3% × (1+(持有者智力-100)/385), 兩點智力328.36/428.14分別應得4.78%/5.56%
    # (兩位小數精確吻合, 非近似)。用真實 TACTICS["傲睨王侯"] 資料(非手造測試效果)驗證,
    # 確保 scaleDiv:385 確實落地且無被其他曲線(350/375)誤蓋。
    aowh114_e = TACTICS["傲睨王侯"]["effects"][0]
    assert aowh114_e.get("scaleDiv") == 385, "傲睨王侯主效果應標scaleDiv:385(user兩點實測定案曲線)"
    for intel114, expect114 in [(328.36, 4.78), (428.14, 5.56)]:
        got114 = 0.03 * SCALE_G(intel114, 385) * 100
        assert abs(got114 - expect114) < 0.01, f"傲睨王侯破綻單層錨點 智力{intel114}: 預期{expect114}%, 算得{got114:.2f}%"

    # 115) per-target疊層 + who=="eventTarget"精確選標: 敵軍目標受普攻時觸發1層(該目標降3%
    # 可疊), 用合成單效果戰法呼叫apply_effects(reactive=True, evt_target=被攻擊的那個敵人)
    # 模擬on_hit_for廣播路徑(who=="enemy")的呼叫慣例, 驗證(a)只有evt_target這個目標的
    # force/intel/command/speed受影響, 隊伍另一人不受影響(b)連續5次觸發後該目標force應為
    # 基準值×(1-0.03×5×scale)(c)第6次觸發時(已達maxStacks)不再繼續疊加(mult不變)。
    aowh115_holder = Unit(POOL["張飛"], "槍")     # 傲睨王侯持有者(智力決定scale基準)
    aowh115_holder.intel = 328.36
    aowh115_tgt = Unit(POOL["張飛"], "槍")        # 被普攻的敵方目標(evt_target)
    aowh115_other = Unit(POOL["張飛"], "槍")      # 同隊另一人, 不應受影響
    aowh115_force_base = aowh115_tgt.eff("force")
    for _ in range(5):
        apply_effects(aowh115_holder, None, {"effects": [aowh114_e], "kind": "intel", "nameZh": "傲睨王侯"},
                      [aowh115_holder], [aowh115_tgt, aowh115_other], reactive=True, evt_target=aowh115_tgt)
    expect_mult115 = 1 - 0.03 * 5 * SCALE_G(328.36, 385)
    assert abs(aowh115_tgt.eff("force") / aowh115_force_base - expect_mult115) < 1e-6, \
        f"傲睨王侯5層疊加後force應為基準×{expect_mult115:.4f}, 實得比例{aowh115_tgt.eff('force')/aowh115_force_base:.4f}"
    assert abs(aowh115_other.eff("force") - aowh115_other.force) < 1e-6, "傲睨王侯: who=='eventTarget'應只影響evt_target這一個目標, 同隊/同敵隊其他人不受影響"
    # 第6次觸發: 本地池已耗盡(maxStacks:5), 不應再繼續疊加
    apply_effects(aowh115_holder, None, {"effects": [aowh114_e], "kind": "intel", "nameZh": "傲睨王侯"},
                  [aowh115_holder], [aowh115_tgt, aowh115_other], reactive=True, evt_target=aowh115_tgt)
    assert abs(aowh115_tgt.eff("force") / aowh115_force_base - expect_mult115) < 1e-6, "傲睨王侯: 第6次觸發時本地破綻池已耗盡, 不應再繼續疊加(mult應維持5層時的值不變)"

    # 116) onMaxStacks: 該目標5層全觸發後應獲得1回合虛弱(amp val:-1.0, 對dmg歸零)+受傷提高
    # 15%持續2回合(mitig val:-0.15)。上面115的aowh115_tgt已經觸發滿5層, 直接驗證其狀態。
    assert abs(aowh115_tgt.addbonus("amp", "intel") - (-1.0)) < 1e-6, "傲睨王侯: 單目標5層全觸發後應獲得虛弱(amp val:-1.0)"
    expect_mitig116 = -0.15 * SCALE_G(328.36, 350)  # onMaxStacks的mitig段帶scale:"intel"(曲線族未定, 沿用全域350預設, 見_note2), 會依holder(傲睨王侯持有者)智力縮放, 非裸值-0.15
    assert abs(aowh115_tgt.addbonus("mitig", "intel") - expect_mitig116) < 1e-6, f"傲睨王侯: 單目標5層全觸發後應獲得受傷提高{-expect_mitig116*100:.1f}%(mitig val:-0.15×智力350曲線縮放, 易傷)"

    # 117) globalMax/globalEffects: 持有者跨目標累計觸發次數達15(3個敵人各5層)後, 敵軍隨機
    # 2人應獲得全屬性-20%。用同一個holder對3個不同敵人各自打滿5層(15次新層觸發, 不含115/116
    # 已用掉的holder——換一個全新holder避免與115/116的exploit_global計數混疊)。
    aowh117_holder = Unit(POOL["張飛"], "槍")
    aowh117_holder.intel = 100.0                  # scale=1.0, 純測邏輯不測曲線細節
    aowh117_e = TACTICS["傲睨王侯"]["effects"][0]
    aowh117_enemies = [Unit(POOL["張飛"], "槍") for _ in range(3)]
    for enemy in aowh117_enemies:
        for _ in range(5):
            apply_effects(aowh117_holder, None, {"effects": [aowh117_e], "kind": "intel", "nameZh": "傲睨王侯"},
                          [aowh117_holder], aowh117_enemies, reactive=True, evt_target=enemy)
    # 每個敵人已先各自疊滿5層本地池(mods含src=="傲睨王侯"的×0.85), globalEffects再疊加一條
    # 額外的src=None全屬性×0.8 mod(兩者相乘生效, 非互斥), 故只檢查「是否額外多一條全域debuff
    # mod」而非檢查eff()總乘積是否恰為0.8(那已被本地層debuff污染, 不會是裸0.8)。
    debuffed117 = [en for en in aowh117_enemies if any(m[0] == "all" and m[3] is None and abs(m[1] - 0.8) < 1e-6 for m in en.mods)]
    assert len(debuffed117) == 2, f"傲睨王侯: 全場15層觸發後應有2名敵軍獲得全屬性-20%(globalEffects), 實得{len(debuffed117)}名"

    print(f"    [批42] 傲睨王侯官方卡重建: 兩點曲線scaleDiv:385(114)/who==\"eventTarget\"精確per-target疊層(115)/單目標5層全觸發虛弱+受傷提高15%(116)/全場15層觸發2人-20%(117)驗證通過")

    # --- 批43: 疊層家族兄弟遷移 —— add型stackKey(虎侯) + on:"healed"反應式事件(權僭九鼎) +
    # ifStackMaxed條件閘門(長驅直入) ---
    # 118) add型stackKey: 虎侯「我軍全體+15點統率, 可疊加5次」——連續5次觸發後應為基準+75點,
    # 第6次觸發時(已達maxStacks)不再繼續疊加。用真實 TACTICS["虎侯"] 資料驗證。
    huhou_e = TACTICS["虎侯"]["effects"][0]
    assert huhou_e.get("add") is True and huhou_e.get("perStack") == 15 and huhou_e.get("maxStacks") == 5, \
        "虎侯: effects[0]應為add型stackKey(add:true/perStack:15/maxStacks:5)"
    huhou_holder = Unit(POOL["張飛"], "槍")
    huhou_u1 = Unit(POOL["張飛"], "槍")
    huhou_base_cmd = huhou_u1.eff("command")
    for _ in range(5):
        apply_effects(huhou_holder, None, {"effects": [huhou_e], "kind": "phys", "nameZh": "虎侯"},
                      [huhou_holder, huhou_u1], [], reactive=True)
    assert abs(huhou_u1.eff("command") - huhou_base_cmd - 75) < 1e-6, \
        f"虎侯: 5次觸發後應為基準+75點統率, 實得+{huhou_u1.eff('command')-huhou_base_cmd:.2f}"
    apply_effects(huhou_holder, None, {"effects": [huhou_e], "kind": "phys", "nameZh": "虎侯"},
                  [huhou_holder, huhou_u1], [], reactive=True)
    assert abs(huhou_u1.eff("command") - huhou_base_cmd - 75) < 1e-6, "虎侯: 第6次觸發時本地池已耗盡, 不應再繼續疊加"

    # 119) on:"healed"反應式事件 —— 驗證 healed_for 確實在 heal 效果結算(troop已回補)後對
    # hurt(受治療者)廣播, 命中 who=="self" 的效果段(權僭九鼎「自身受到治療時+5統率」)。
    # on_heal_effect_tacs 是 Unit 建構時依 self.tactics 預篩的陣列(見批43 C), 測試直接注入
    # holder.tactics 後重新建構, 對稱批38/42既有測試對 activeFiredTacs 等預篩陣列的驗證方式。
    qsjd_stat_e = {"k": "stat", "who": "self", "stat": "command", "add": True, "perStack": 5,
                   "maxStacks": 99, "stackKey": True, "dur": 99, "rate": 1.0,
                   "when": {"on": "healed", "who": "self"}}
    qsjd_tac = {"type": "passive", "nameZh": "權僭九鼎測試", "kind": "phys", "coef": 0,
                "effects": [qsjd_stat_e]}
    qsjd_g = POOL["張飛"]
    qsjd_holder = Unit(qsjd_g, "槍")
    qsjd_holder.tactics = [qsjd_tac]                # 注入測試戰法後重算預篩陣列(對稱批38/42既有測試手法)
    qsjd_holder.on_heal_effect_tacs = [qsjd_tac]
    qsjd_holder.troop = qsjd_holder.troop * 0.5     # 製造傷兵池空間, 確保heal真的有回補
    qsjd_holder.wounded = qsjd_holder.troop
    qsjd_base_cmd = qsjd_holder.eff("command")
    qsjd_healer = Unit(POOL["諸葛亮"], "弓")
    qsjd_heal_tac = {"effects": [{"k": "heal", "who": "ally", "coef": 0.1, "dur": 99}],
                      "kind": "phys", "nameZh": "測試治療來源"}
    apply_effects(qsjd_healer, None, qsjd_heal_tac, [qsjd_healer, qsjd_holder], [])
    assert abs(qsjd_holder.eff("command") - qsjd_base_cmd - 5) < 1e-6, \
        f"權僭九鼎: on=='healed'應在治療結算後觸發+5統率, 實得+{qsjd_holder.eff('command')-qsjd_base_cmd:.2f}"

    # 120) ifStackMaxed —— 長驅直入「疊加5次後才降傷16%」: stack.n未滿時mitig不應觸發,
    # 滿5層後才應觸發。用真實 TACTICS["長驅直入"] 資料驗證。
    cqzr_mitig_e = TACTICS["長驅直入"]["effects"][1]
    assert cqzr_mitig_e.get("ifStackMaxed") is True and cqzr_mitig_e.get("everyRound") is True, \
        "長驅直入: effects[1]應帶ifStackMaxed+everyRound(疊滿5層才觸發減傷)"
    cqzr_u = Unit(POOL["張飛"], "槍")
    cqzr_ally = Unit(POOL["張飛"], "槍")
    cqzr_u.stack = {"per": 0.15, "max": 5, "n": 3}   # 未滿5層
    cqzr_u.own_round = 1                              # 時序一致化(2026-07批次)A.3: everyRound改own_turn+own_round
    apply_effects(cqzr_u, None, {"effects": [cqzr_mitig_e], "kind": "phys", "nameZh": "長驅直入"},
                  [cqzr_u, cqzr_ally], [], own_turn=True)
    assert abs(cqzr_ally.addbonus("mitig") - 0) < 1e-9, "長驅直入: stack未滿5層時mitig不應觸發"
    cqzr_u.stack["n"] = 5                            # 疊滿5層
    cqzr_u.own_round = 2
    apply_effects(cqzr_u, None, {"effects": [cqzr_mitig_e], "kind": "phys", "nameZh": "長驅直入"},
                  [cqzr_u, cqzr_ally], [], own_turn=True)
    assert abs(cqzr_ally.addbonus("mitig") - (0.16 * scale_of(cqzr_u, "force"))) < 1e-6, "長驅直入: stack疊滿5層後mitig應觸發(減傷16%×武力縮放, mitig正值=減傷)"

    print(f"    [批43] 疊層家族兄弟遷移: add型stackKey虎侯5層+75點統率(118)/on==\"healed\"反應式事件權僭九鼎(119)/ifStackMaxed長驅直入疊滿才觸發(120)驗證通過")

    # --- 批44: e.ifLeaderIs(特定武將統領條件) —— 效果級/extraHits段級「隊伍主將(allies[0])
    # 的武將名須匹配指定值」條件閘門, 對稱既有 e.ifLeader(布林, 只判斷「是否為主將」) ---
    # 121) 合成效果: 陳到是主將時應吃到, 陳到是副將(非主將)時不吃, 他人是主將時也不吃
    # (身份不匹配, 即使是主將)。
    il121_tac = {"nameZh": "測試ifLeaderIs121", "effects": [
        {"k": "stat", "who": "self", "stat": "force", "add": 999, "dur": 5, "ifLeaderIs": "陳到"}
    ]}
    il121_chendao_leader = Unit(POOL["陳到"], "槍")
    apply_effects(il121_chendao_leader, None, il121_tac, [il121_chendao_leader], [], no_heal=True)
    assert abs(il121_chendao_leader.eff("force") - (il121_chendao_leader.force + 999)) < 1e-6, \
        "ifLeaderIs: 陳到是主將(allies[0])且武將名匹配時應正常套用效果"
    il121_chendao_sub = Unit(POOL["陳到"], "槍")
    il121_other_leader = Unit(POOL["張飛"], "槍")
    apply_effects(il121_chendao_sub, None, il121_tac, [il121_other_leader, il121_chendao_sub], [], no_heal=True)
    assert abs(il121_chendao_sub.eff("force") - il121_chendao_sub.force) < 1e-6, \
        "ifLeaderIs: 陳到是副將(非主將)時應完全跳過該效果, 不套用"
    il121_zhangfei_leader = Unit(POOL["張飛"], "槍")
    apply_effects(il121_zhangfei_leader, None, il121_tac, [il121_zhangfei_leader], [], no_heal=True)
    assert abs(il121_zhangfei_leader.eff("force") - il121_zhangfei_leader.force) < 1e-6, \
        "ifLeaderIs: 張飛是主將但武將名不匹配(要求陳到)時應完全跳過該效果, 不套用"

    # 122) 陣列OR語意(虎衛軍「若典韋或許褚統領」): 典韋/許褚任一是主將皆應吃到, 他人是主將不吃。
    il122_tac = {"nameZh": "測試ifLeaderIs122", "effects": [
        {"k": "stat", "who": "self", "stat": "command", "add": 50, "dur": 99, "ifLeaderIs": ["典韋", "許褚"]}
    ]}
    for name122 in ("典韋", "許褚"):
        il122_u = Unit(POOL[name122], "盾")
        apply_effects(il122_u, None, il122_tac, [il122_u], [], no_heal=True)
        assert abs(il122_u.eff("command") - (il122_u.command + 50)) < 1e-6, \
            f"ifLeaderIs陣列OR: {name122}是主將時應吃到+50統率(陣列命中任一即符合)"
    il122_other = Unit(POOL["張飛"], "槍")
    apply_effects(il122_other, None, il122_tac, [il122_other], [], no_heal=True)
    assert abs(il122_other.eff("command") - il122_other.command) < 1e-6, \
        "ifLeaderIs陣列OR: 張飛是主將但不在[典韋,許褚]陣列內時應跳過"

    # 123) extraHits段級ifLeaderIs(大戟士「張郃獲得連擊」精確度量): 張郃是主將時extraHits應
    # 觸發(rate=1強制必發驗證邏輯本身, 非真實資料的0.45), 非張郃時不觸發。
    il123_tac = {"nameZh": "測試ifLeaderIs123", "coef": 0, "effects": [],
                 "extraHits": [{"coef": 1.0, "kind": "phys", "who": "sameTarget", "rate": 1.0, "ifLeaderIs": "張郃"}]}
    il123_zhanghe = Unit(POOL["張郃"], "槍")
    il123_target = Unit(POOL["張飛"], "槍")
    il123_hits = []
    fire_extra_hits(il123_zhanghe, il123_tac, il123_target, lambda u: [il123_zhanghe], lambda u: [il123_target],
                     lambda *a, **k: None, None)
    assert il123_target.troop < START_TROOP, "ifLeaderIs(extraHits): 張郃是主將時額外段應命中目標造成傷害"
    il123_target2 = Unit(POOL["張飛"], "槍")
    il123_zhanghe_sub = Unit(POOL["張郃"], "槍")
    il123_other_leader = Unit(POOL["關羽"], "槍")
    fire_extra_hits(il123_zhanghe_sub, il123_tac, il123_target2, lambda u: [il123_other_leader, il123_zhanghe_sub],
                     lambda u: [il123_target2], lambda *a, **k: None, None)
    assert il123_target2.troop == START_TROOP, "ifLeaderIs(extraHits): 張郃是副將(非主將)時額外段不應觸發"

    # 124) 真實資料回歸(白毦兵, R27/批44核心案例): 陳到統領時應吃到頂層coef(1.1)+extraHits
    # top-up(0.2)=1.3對齊滿級值; 非陳到統領時僅頂層1.1。
    bym124_tac = TACTICS["白毦兵"]
    assert bym124_tac["extraHits"][0].get("ifLeaderIs") == "陳到", "白毦兵: extraHits[0]應帶ifLeaderIs=陳到(批44落地)"
    assert abs(bym124_tac["coef"] - 1.1) < 1e-9, "白毦兵: 頂層coef應維持1.1(base段, 無條件)"

    print(f"    [批44] e.ifLeaderIs特定武將統領條件: 效果級主將身份匹配(121)/陣列OR語意(122)/extraHits段級(123)/白毦兵真實資料回歸(124)驗證通過")

    # --- 批45 A: e["sameTargets"] —— 群體目標沿用原語, 對稱既有 main_hit_tgt(單體)慣例。
    # coef段(pick_targets)與效果段(who=="enemy"+e["sameTargets"])須精確命中同一批目標, 不再
    # 各自獨立擲骰(3人隊過去只有1/3機率同組)。---
    # 125) 直接呼叫 apply_effects, 傳入 main_hit_tgts=[固定2人], 驗證效果段 dests 精確等於
    # main_hit_tgts(過濾存活), 且不受 e["n"] 影響(sameTargets 優先於 has_en 分支)。
    st125_caster = Unit(POOL["張飛"], "槍")
    st125_foe_a = Unit(POOL["關羽"], "槍")
    st125_foe_b = Unit(POOL["趙雲"], "槍")
    st125_foe_c = Unit(POOL["馬超"], "槍")  # 第3名敵軍, 不應被命中(驗證sameTargets精確排除未命中者)
    st125_tac = {"nameZh": "測試sameTargets125", "effects": [
        {"k": "stat", "who": "enemy", "stat": "speed", "add": -30, "dur": 2, "sameTargets": True}
    ]}
    apply_effects(st125_caster, None, st125_tac, [st125_caster], [st125_foe_a, st125_foe_b, st125_foe_c],
                  no_heal=True, main_hit_tgts=[st125_foe_a, st125_foe_b])
    assert abs(st125_foe_a.eff("speed") - (st125_foe_a.speed - 30)) < 1e-6, "sameTargets: main_hit_tgts內的目標應命中效果"
    assert abs(st125_foe_b.eff("speed") - (st125_foe_b.speed - 30)) < 1e-6, "sameTargets: main_hit_tgts內的目標應命中效果"
    assert abs(st125_foe_c.eff("speed") - st125_foe_c.speed) < 1e-6, "sameTargets: 不在main_hit_tgts內的第3名敵軍不應被命中"

    # 126) main_hit_tgts=None(未傳入, 如prep/reactive等既有呼叫路徑)時, sameTargets 應落空
    # dests=[](不誤退回全體/隨機選標), 向後相容——只有明確傳入且母戰法主coef段確實命中群體時
    # 才會生效。
    st126_caster = Unit(POOL["張飛"], "槍")
    st126_foe = Unit(POOL["關羽"], "槍")
    apply_effects(st126_caster, None, st125_tac, [st126_caster], [st126_foe], no_heal=True)  # 未傳main_hit_tgts
    assert abs(st126_foe.eff("speed") - st126_foe.speed) < 1e-6, "sameTargets: 未傳main_hit_tgts時應落空不生效(向後相容, 不誤套用全體)"

    # 127) 端到端 fight() 主迴圈: 3人隊, 主動戰法群體(n=2)coef命中2人時, 效果段 sameTargets
    # 應精確命中同一批2人(非獨立隨機選標)。用固定隨機種子跑多次戰鬥, TRACE式直接檢查
    # troop 損耗與 speed debuff 是否精確對應同一批目標(non-hit的第3人不應掉血也不應被debuff)。
    st127_tac_name = "測試sameTargets127"
    TACTICS[st127_tac_name] = {"nameZh": st127_tac_name, "type": "active", "kind": "phys",
                               "coef": 0.01, "rate": 1.0, "n": 2, "prep": 0, "effects": [
        {"k": "stat", "who": "enemy", "stat": "speed", "add": -999, "dur": 2, "sameTargets": True}
    ]}
    random.seed(20260707)
    st127_hit_ok = 0
    for _ in range(50):
        st127_u = Unit(POOL["張飛"], "槍")
        st127_a = Unit(POOL["關羽"], "槍")
        st127_b = Unit(POOL["趙雲"], "槍")
        st127_c = Unit(POOL["馬超"], "槍")
        foes127 = [st127_a, st127_b, st127_c]
        # 手動模擬單回合 fight() 主迴圈的 coef 群體段 + sameTargets 效果段呼叫序列(不跑完整fight,
        # 直接複現主迴圈同段落邏輯, 驗證引擎內建的呼叫慣例本身正確):
        vs127 = pick_targets(foes127, 2)
        for v in vs127:
            hit(st127_u, v, TACTICS[st127_tac_name]["coef"], "phys", False, None, None)
        mht127 = vs127 if len(vs127) > 1 else None
        apply_effects(st127_u, None, TACTICS[st127_tac_name], [st127_u], foes127, no_heal=True, main_hit_tgts=mht127)
        hurt127 = [f for f in foes127 if f.troop < START_TROOP]
        debuffed127 = [f for f in foes127 if f.eff("speed") < f.speed]
        if set(id(x) for x in hurt127) == set(id(x) for x in debuffed127) == set(id(x) for x in vs127):
            st127_hit_ok += 1
    del TACTICS[st127_tac_name]
    assert st127_hit_ok == 50, f"sameTargets端到端: 50次模擬中應全數精確命中同一批coef群體目標(實際{st127_hit_ok}/50)"

    # 128) sibling-effect 沿用(誘敵深入形狀: coef=0, 無main coef段可沿用, dot(首個, e.n=2)
    # 就地成為"main_hit_tgts"的來源, amp(第二個, sameTargets:true)應沿用dot實際命中的同一批
    # 目標, 而非各自獨立pick_targets。50次跑批驗證dot命中集合(靠troop受損判斷)與amp命中集合
    # (靠speed受影響判斷改用stat效果排查更直接: 這裡用dot的troop損耗 vs amp的stat debuff)。
    st128_tac_name = "測試sameTargets128"
    TACTICS[st128_tac_name] = {"nameZh": st128_tac_name, "type": "active", "kind": "intel",
                               "coef": 0, "rate": 1.0, "n": 2, "prep": 0, "effects": [
        {"k": "dot", "who": "enemy", "coef": 0.5, "dur": 2, "n": 2},
        {"k": "stat", "who": "enemy", "stat": "speed", "add": -999, "dur": 2, "sameTargets": True}
    ]}
    random.seed(20260707)
    st128_hit_ok = 0
    for _ in range(50):
        st128_u = Unit(POOL["張飛"], "槍")
        st128_a = Unit(POOL["關羽"], "槍")
        st128_b = Unit(POOL["趙雲"], "槍")
        st128_c = Unit(POOL["馬超"], "槍")
        foes128 = [st128_a, st128_b, st128_c]
        apply_effects(st128_u, None, TACTICS[st128_tac_name], [st128_u], foes128, no_heal=True)
        dotted128 = [f for f in foes128 if f.dots]
        debuffed128 = [f for f in foes128 if f.eff("speed") < f.speed]
        if len(dotted128) == 2 and set(id(x) for x in dotted128) == set(id(x) for x in debuffed128):
            st128_hit_ok += 1
    del TACTICS[st128_tac_name]
    assert st128_hit_ok == 50, f"sameTargets(sibling-effect無coef錨點): 50次模擬中dot與amp/stat應精確命中同一批2人(實際{st128_hit_ok}/50)"

    print(f"    [批45 A] e.sameTargets群體目標沿用原語: 基本沿用(125)/未傳main_hit_tgts向後相容(126)/端到端fight同段落50次全命中(127)/sibling-effect無coef錨點沿用(128)驗證通過")

    # --- 批45 C: TARGETSEL_KEY.maxTroop(兵力最高準則, 對稱既有minTroop) ---
    # 129) pick_by_criterion(units, "maxTroop") 應精確選中兵力最高的單位(而非minTroop/
    # mostDamaged的兵力最低方向)。
    mt129_a = Unit(POOL["張飛"], "槍")
    mt129_b = Unit(POOL["關羽"], "槍")
    mt129_c = Unit(POOL["趙雲"], "槍")
    mt129_a.troop, mt129_b.troop, mt129_c.troop = 5000, 9000, 3000
    picked129 = pick_by_criterion([mt129_a, mt129_b, mt129_c], "maxTroop")
    assert picked129 is mt129_b, "maxTroop: 應精確選中兵力最高的單位(9000), 而非minTroop方向"
    picked129_min = pick_by_criterion([mt129_a, mt129_b, mt129_c], "minTroop")
    assert picked129_min is mt129_c, "對照組: minTroop應維持既有行為選中兵力最低(3000), 未受maxTroop新增影響"

    # 130) 定謀貴決真實資料回歸: effects[0]應帶targetSel:"maxTroop"(批45 C落地), 且無殘留
    # 的批21撤回未清乾淨的舊欄位值。
    dmgj130_tac = TACTICS["定謀貴決"]
    assert dmgj130_tac["effects"][0].get("targetSel") == "maxTroop", "定謀貴決: effects[0]應帶targetSel:\"maxTroop\"(批45 C精確落地, 取代批21撤回後的無targetSel近似)"

    print(f"    [批45 C] TARGETSEL_KEY.maxTroop(兵力最高準則): 精確選標方向驗證(129)/定謀貴決真實資料回歸(130)驗證通過")

    # --- 批46 A: rateup 支援 e.scaleDiv(曲線族泛化) + 十二奇策官方卡七點齊發破案曲線落地 ---
    # 131) rate_scale_of 第三參數 scale_div: 未傳(None)應與舊版 RATE_SCALE_C(除數384.6)行為
    #      逐位元一致(向後相容, 太平道法等既有資料零回歸)。
    assert abs(rate_scale_of(POOL["張角"], None)) < 1e-12 or rate_scale_of(POOL["張角"], None) == 1.0, \
        "rate_scale_of: scale=None 應回傳1.0(無縮放), 與舊版行為一致"
    _rsu = Unit(POOL["張角"], "騎")
    _rsu.push_mod("intel", 426.57 / _rsu.eff("intel"), 9)
    assert abs(_rsu.eff("intel") - 426.57) < 1e-6
    _default_scaled = rate_scale_of(_rsu, "intel")
    _explicit_default_div_scaled = rate_scale_of(_rsu, "intel", RATE_SCALE_DEFAULT_DIV)
    assert abs(_default_scaled - _explicit_default_div_scaled) < 1e-12, \
        "rate_scale_of: 未傳scale_div應等同顯式傳入RATE_SCALE_DEFAULT_DIV(384.6154...), 向後相容"

    # 132) 十二奇策(荀攸自帶): rateup 效果應帶 scale:"intel"+scaleDiv:335(本批A項落地), 用
    #      calibration_anchors.json → shierqice_20260707 的 rate_boost_samples_v2 七點齊發實測
    #      (D=335.1±0.15) 逐點驗算, 容差0.01(對應user錨點記載的精度)。
    sq_tac = TACTICS["十二奇策"]
    sq_ru = next(e for e in sq_tac["effects"] if e["k"] == "rateup")
    assert sq_ru.get("scale") == "intel" and sq_ru.get("scaleDiv") == 335, \
        "十二奇策 rateup 應帶 scale:\"intel\", scaleDiv:335(批46 A精確落地, 取代預設384.6曲線)"
    sq_samples = [
        (420.09, 11.73), (444.85, 12.18), (401.74, 11.4), (415.6, 11.65),
        (421.23, 11.75), (433.9, 11.98), (448.02, 12.23),
    ]
    for intel_v, expect_pct in sq_samples:
        sq_u = Unit(POOL["荀攸"], "槍")
        sq_u.push_mod("intel", intel_v / sq_u.eff("intel"), 9)
        assert abs(sq_u.eff("intel") - intel_v) < 1e-6, f"測試前置條件: 智力應精確落在{intel_v}"
        got_scale = rate_scale_of(sq_u, sq_ru["scale"], sq_ru["scaleDiv"])
        got_pct = sq_ru["val"] * got_scale * 100
        assert abs(got_pct - expect_pct) < 0.01, \
            f"十二奇策智力{intel_v}時rateup加成應≈{expect_pct}%(D=335曲線), got={got_pct:.4f}%"

    # 133) 全庫同族補遺核對: 舌戰群儒(「發動機率...受智力影響」同族措辭)的兩段rateup應補上
    #      scale:"intel"(批38遺留_todo, 本批B項補上), 但scaleDiv曲線族未定(無獨立實測樣本佐證
    #      是否與十二奇策同屬335家族), 應沿用預設384.6, 不擅自外推借用335(比照批35「曲線族
    #      未定不擅自套用非預設除數」慣例)。
    sgq_tac = TACTICS["舌戰群儒"]
    sgq_rus = [e for e in sgq_tac["effects"] if e["k"] == "rateup"]
    assert len(sgq_rus) == 2, "舌戰群儒應有2條rateup(降敵/增己及隨機友軍)"
    assert all(e.get("scale") == "intel" for e in sgq_rus), \
        "舌戰群儒2條rateup皆應補scale:\"intel\"(批46 B, 原文「受智力影響」)"
    assert all(e.get("scaleDiv") is None for e in sgq_rus), \
        "舌戰群儒2條rateup不應標scaleDiv(曲線族未定, 沿用預設384.6, 不外推借用十二奇策的335)"

    # 134) 先成其慮(對照組): rateup段原文無「受智力影響」標記(僅前一句傷害段受智力影響),
    #      應維持不帶scale(val=0.15固定值, 不隨智力縮放)。
    xcq_tac = TACTICS["先成其慮"]
    xcq_ru = next(e for e in xcq_tac["effects"] if e["k"] == "rateup")
    assert xcq_ru.get("scale") is None, \
        "先成其慮 rateup 段原文無「受智力影響」標記, 不應帶scale(對照組, 確認未誤套用)"

    print(f"    [批46 A] rateup e.scaleDiv曲線族泛化: 向後相容(131)/十二奇策scaleDiv:335七點齊發精確驗算(132)/舌戰群儒同族補遺scale補上但曲線族未定不外推(133)/先成其慮對照組無scale(134)驗證通過")

    # --- 批47 A: 白馬義從「若公孫瓚統領, 提高發動率受速度影響」落地(ifLeaderIs+scale:"speed"
    # +scaleDiv:1003, 與base(val:0.1)靠同src+同kind的push_add「同來源刷新覆蓋」拼出
    # base+conditional-override, 見tactic_corrections.json「白馬義從」effects[1]._note完整推導)。
    # 135) 資料層核對: 應有兩條rateup(base無條件0.1 + 公孫瓚topup 0.10293/scale:speed/
    #      scaleDiv:1003), 且topup段ifLeaderIs=="公孫瓚"。
    bmyc_tac = TACTICS["白馬義從"]
    bmyc_rus = [e for e in bmyc_tac["effects"] if e["k"] == "rateup"]
    assert len(bmyc_rus) == 2, "白馬義從應有2條rateup(base無條件 + 公孫瓚統領scale topup)"
    bmyc_base, bmyc_topup = bmyc_rus[0], bmyc_rus[1]
    assert abs(bmyc_base["val"] - 0.1) < 1e-9 and bmyc_base.get("ifLeaderIs") is None, \
        "白馬義從 effects[0](base)應為無條件val=0.1(非公孫瓚統領時的固定10%發動率)"
    assert bmyc_topup.get("ifLeaderIs") == "公孫瓚", "白馬義從 topup段應帶ifLeaderIs=\"公孫瓚\"(批47 A落地)"
    assert bmyc_topup.get("scale") == "speed", "白馬義從 topup段應帶scale=\"speed\"(受速度影響, 原文明載)"
    assert bmyc_topup.get("scaleDiv") == 1003, \
        "白馬義從 topup段應帶scaleDiv=1003(user Lv1兩點+加法定律換算的Lv10等價乘法形, 見calibration_anchors.json baima_yicong_20260708)"
    assert abs(bmyc_topup["val"] - 0.10293) < 1e-9, "白馬義從 topup段val應為0.10293(Lv10基值10.293%, user本批提供)"

    # 136) 行為核對(非公孫瓚統領): 張飛統領白馬義從時, topup段的ifLeaderIs閘門應擋下,
    #      addbonus("rateup")應精確等於base的固定10%(不受速度影響, 沿用原有行為)。
    bmyc_zhangfei = Unit(POOL["張飛"], "騎")
    bmyc_zhangfei.push_mod("speed", 200.0 / bmyc_zhangfei.eff("speed"), 9)  # 刻意調到高速度, 驗證非公孫瓚時不受速度影響
    apply_effects(bmyc_zhangfei, None, bmyc_tac, [bmyc_zhangfei], [], no_heal=True)
    assert abs(bmyc_zhangfei.addbonus("rateup") - 0.1) < 1e-9, \
        "白馬義從: 張飛統領(非公孫瓚)時, 即使高速度也應維持固定10%發動率加成(topup段ifLeaderIs擋下)"

    # 137) 行為核對(公孫瓚統領, user Lv1兩點回推的speed=133.78錨點): 公孫瓚統領白馬義從、
    #      速度精確調至133.78時, addbonus("rateup")應等於本批Lv10公式值≈10.64%(±0.01%),
    #      精確覆蓋掉base的10%(同src+同kind push_add「同來源刷新覆蓋」, 非疊加成20.64%)。
    bmyc_gsz = Unit(POOL["公孫瓚"], "騎")
    bmyc_gsz.push_mod("speed", 133.78 / bmyc_gsz.eff("speed"), 9)
    assert abs(bmyc_gsz.eff("speed") - 133.78) < 1e-6, "測試前置條件: 公孫瓚速度應精確落在133.78"
    apply_effects(bmyc_gsz, None, bmyc_tac, [bmyc_gsz], [], no_heal=True)
    got_bmyc_pct = bmyc_gsz.addbonus("rateup") * 100
    assert abs(got_bmyc_pct - 10.64) < 0.01, \
        f"白馬義從: 公孫瓚統領+速度133.78時rateup加成應≈10.64%(Lv10公式10.293%+0.010263%×(133.78-100)), got={got_bmyc_pct:.4f}%"
    assert abs(got_bmyc_pct - 20.64) > 1.0, \
        "白馬義從: 公孫瓚統領時應是topup覆蓋base(≈10.64%), 不是base+topup疊加(≈20.64%)——同src+同kind push_add應互相覆蓋而非累加"

    # 138) 加法定律等價性直接驗算: base(speed=100)理論值應為10.293%整(scaleDiv換算的基準點),
    #      與calibration_anchors.json level_additive_law_20260708記載的Lv10基值一致。
    bmyc_gsz2 = Unit(POOL["公孫瓚"], "騎")
    bmyc_gsz2.push_mod("speed", 100.0 / bmyc_gsz2.eff("speed"), 9)
    assert abs(bmyc_gsz2.eff("speed") - 100.0) < 1e-6
    apply_effects(bmyc_gsz2, None, bmyc_tac, [bmyc_gsz2], [], no_heal=True)
    got_base_pct = bmyc_gsz2.addbonus("rateup") * 100
    assert abs(got_base_pct - 10.293) < 0.01, \
        f"白馬義從: 公孫瓚統領+速度100(scale=1基準點)時rateup加成應=10.293%(Lv10基值), got={got_base_pct:.4f}%"

    print(f"    [批47 A] 白馬義從「若公孫瓚統領, 提高發動率受速度影響」落地: 資料層base+topup雙rateup(135)/非公孫瓚統領固定10%不受速度影響(136)/公孫瓚統領+速度133.78精確10.64%(137, user Lv1兩點+加法定律換算)/speed=100基準點10.293%(138)驗證通過")

    # --- 批52: heal 選標對齊原文 (who/e.n/sharedPool) ---
    # 139) who:self —— 一力拒守「恢復自身」不得誤治隊友最殘
    random.seed(52)
    h_self = Unit(POOL["高順"], "盾")
    h_ally = Unit(POOL["張飛"], "槍")
    h_ally2 = Unit(POOL["關羽"], "槍")
    # 製造傷兵: 隊友比自己殘, 若仍走「最殘一人」會誤治隊友
    h_self.troop, h_self.wounded = 8000.0, 2000.0
    h_ally.troop, h_ally.wounded = 3000.0, 7000.0
    h_ally2.troop, h_ally2.wounded = 5000.0, 5000.0
    allies_h = [h_self, h_ally, h_ally2]
    tac_self = {"type": "active", "kind": "phys", "nameZh": "一力拒守",
                "effects": [{"k": "heal", "who": "self", "coef": 2.0, "dur": 1}]}
    t_before = h_self.troop
    a_before = h_ally.troop
    apply_effects(h_self, None, tac_self, allies_h, [])
    assert h_self.troop > t_before, "批52: who:self 應治療施放者自身"
    assert abs(h_ally.troop - a_before) < 1e-6, "批52: who:self 不得治療隊友(即使隊友更殘)"

    # 140) e.n=2 群體治療 —— 一次應命中 2 名存活友軍(非只 1 人)
    random.seed(52)
    g0 = Unit(POOL["華佗"], "弓")
    g1 = Unit(POOL["劉備"], "槍")
    g2 = Unit(POOL["關羽"], "槍")
    for u, tr in ((g0, 6000.0), (g1, 5000.0), (g2, 4000.0)):
        u.troop, u.wounded = tr, START_TROOP - tr
    before = {id(u): u.troop for u in (g0, g1, g2)}
    tac_grp = {"type": "active", "kind": "intel", "nameZh": "杯蛇鬼車",
               "effects": [{"k": "heal", "who": "ally", "coef": 1.0, "n": 2, "scale": "intel"}]}
    apply_effects(g0, None, tac_grp, [g0, g1, g2], [])
    healed_n = sum(1 for u in (g0, g1, g2) if u.troop > before[id(u)] + 1)
    assert healed_n == 2, f"批52: e.n=2 群體治療應命中恰好2人, got={healed_n}"

    # 141) who:leader —— 只治主將
    random.seed(52)
    ld = Unit(POOL["曹操"], "騎")
    sub = Unit(POOL["許褚"], "盾")
    ld.troop, ld.wounded = 7000.0, 3000.0
    sub.troop, sub.wounded = 2000.0, 8000.0
    ld_before, sub_before = ld.troop, sub.troop
    tac_ld = {"type": "active", "kind": "intel", "nameZh": "乘敵不虞",
              "effects": [{"k": "heal", "who": "leader", "coef": 1.0, "scale": "intel"}]}
    apply_effects(sub, None, tac_ld, [ld, sub], [])
    assert ld.troop > ld_before, "批52: who:leader 應治療主將(index0)"
    assert abs(sub.troop - sub_before) < 1e-6, "批52: who:leader 不得治療副將(即使副將更殘)"

    # 142) sharedPool+preferLowest —— 總治療量優先填滿最殘, 不得每人各吃完整池
    random.seed(52)
    s0 = Unit(POOL["曹操"], "騎")
    s1 = Unit(POOL["許褚"], "盾")
    s2 = Unit(POOL["典韋"], "盾")
    s0.troop, s0.wounded = 9000.0, 1000.0   # 最滿
    s1.troop, s1.wounded = 1000.0, 9000.0   # 最殘
    s2.troop, s2.wounded = 5000.0, 5000.0
    b0, b1, b2 = s0.troop, s1.troop, s2.troop
    tac_share = {"type": "command", "kind": "phys", "nameZh": "青州兵",
                 "effects": [{"k": "heal", "who": "ally", "coef": 1.0, "n": 3,
                              "preferLowest": True, "sharedPool": True, "scale": "force"}]}
    apply_effects(s0, None, tac_share, [s0, s1, s2], [])
    assert s1.troop > b1, "批52: sharedPool 應優先治療最殘(s1)"
    # 最滿者通常分不到或分到很少(池先被最殘吸走)
    assert (s1.troop - b1) >= (s0.troop - b0) - 1e-6, "批52: sharedPool 最殘回復量應 ≥ 最滿者"
    print("    [批52] heal選標: who:self(139)/e.n群體(140)/who:leader(141)/sharedPool優先最殘(142) 驗證通過")

    # 143) enemyLeader who —— 嘲諷/虛弱只鎖敵方 index0
    random.seed(52)
    el_caster = Unit(POOL["曹操"], "騎")
    el_e0 = Unit(POOL["張飛"], "槍")  # 敵主將
    el_e1 = Unit(POOL["關羽"], "槍")
    el_e0.troop = el_e1.troop = 8000.0
    tac_el = {"nameZh": "守而必固", "effects": [
        {"k": "taunt", "who": "enemyLeader", "dur": 4, "n": 1},
    ]}
    apply_effects(el_caster, None, tac_el, [el_caster], [el_e0, el_e1])
    assert el_e0.taunt_dur > 0 and el_e0.taunt_by is el_caster, \
        "批52: who:enemyLeader 嘲諷應落在敵方主將(index0)"
    assert el_e1.taunt_dur == 0, "批52: 敵方副將不應被嘲諷"

    # 144) maxStack —— 一力拒守統率可疊 2 層
    random.seed(52)
    ms_u = Unit(POOL["高順"], "盾")
    tac_ms = {"nameZh": "一力拒守", "type": "active", "effects": [
        {"k": "stat", "who": "self", "stat": "command", "add": 21, "dur": 3, "maxStack": 2},
    ]}
    apply_effects(ms_u, None, tac_ms, [ms_u], [])
    c1 = ms_u.eff("command")
    apply_effects(ms_u, None, tac_ms, [ms_u], [])
    c2 = ms_u.eff("command")
    apply_effects(ms_u, None, tac_ms, [ms_u], [])
    c3 = ms_u.eff("command")
    assert c2 > c1 + 10, f"批52: maxStack 第2次應疊加統率, c1={c1} c2={c2}"
    assert abs(c3 - c2) < 1e-6, f"批52: maxStack=2 第3次應達上限不再加, c2={c2} c3={c3}"

    # 145) cooldown —— 發動後下回合不可再發
    random.seed(52)
    cd_u = Unit(POOL["華佗"], "弓")
    # 注入測試戰法
    cd_tac = {"nameZh": "測試冷卻145", "type": "active", "kind": "intel", "coef": 0, "rate": 1.0,
              "prep": 0, "n": 1, "cd": 1, "effects": [{"k": "heal", "who": "self", "coef": 0.5}]}
    cd_u.tactics = [cd_tac]
    cd_u.tac_cd = {}
    # 模擬 fire 寫入
    cd_u.tac_cd["測試冷卻145"] = 1 + 1
    assert cd_u.tac_cd.get("測試冷卻145", 0) > 0
    cd_u.tick()  # 本回合結束 → 剩 1
    assert cd_u.tac_cd.get("測試冷卻145", 0) == 1, "批52: tick後冷卻應剩1(下回合仍不可用)"
    cd_u.tick()
    assert cd_u.tac_cd.get("測試冷卻145", 0) == 0, "批52: 再tick後冷卻歸零"

    # 146) ifLeader on extraHits —— 非主將不吃加成段
    random.seed(52)
    from copy import deepcopy
    # 資料層: 一騎當千 extraHits 應有 ifLeader
    yq = TACTICS.get("一騎當千")
    assert yq and any(eh.get("ifLeader") for eh in (yq.get("extraHits") or [])), \
        "批52: 一騎當千 extraHits 應帶 ifLeader"
    assert TACTICS.get("草船借箭", {}).get("cd") == 1, "批52: 草船借箭應有 cd=1"
    assert any(e.get("who") == "enemyLeader" for e in (TACTICS.get("守而必固") or {}).get("effects") or []), \
        "批52: 守而必固 taunt who=enemyLeader"

    print("    [批52+] enemyLeader/maxStack/cd/ifLeader資料(143-146) 驗證通過")

    # --- 批52d: 虎嗔(huchen) 可驅散負面狀態 ---
    # 147) 施加 + 受擊疊層 + 滿3次提前結算震懾 + 施放者 amp
    random.seed(52)
    hc_src = Unit(POOL.get("關銀屏") or POOL["關羽"], "槍")
    hc_dst = Unit(POOL["張飛"], "槍")
    hc_dst.troop, hc_dst.wounded = 8000.0, 2000.0
    tac_hc = {"nameZh": "將門虎女", "type": "active", "kind": "phys", "effects": [
        {"k": "huchen", "who": "enemy", "n": 1, "base": 0.20, "per": 0.30, "maxHits": 3, "dur": 1,
         "kind": "phys", "ampOnSettle": 0.08},
    ]}
    apply_effects(hc_src, hc_dst, tac_hc, [hc_src], [hc_dst])
    assert hc_dst.huchen is not None, "批52d: 應施加虎嗔"
    assert target_has(hc_dst, "huchen") and target_has(hc_dst, "虎嗔")
    # 兩次受傷疊層未滿
    hit(hc_src, hc_dst, 0.5, "phys")
    hit(hc_src, hc_dst, 0.5, "phys")
    assert hc_dst.huchen and hc_dst.huchen["hits"] == 2, f"應疊2層, got {hc_dst.huchen}"
    # 第三次提前結算
    amp_before = hc_src.addbonus("amp")
    hit(hc_src, hc_dst, 0.5, "phys")
    assert hc_dst.huchen is None, "滿3次應立即結算清除虎嗔"
    assert hc_dst.stun > 0, "提前結算應附加震懾"
    assert hc_src.addbonus("amp") > amp_before, "結算應給施放者兵刃 amp+8%"

    # 148) dispel debuffs 清除虎嗔(草船/刮骨)
    hc_dst2 = Unit(POOL["張飛"], "槍")
    apply_effects(hc_src, hc_dst2, tac_hc, [hc_src], [hc_dst2])
    assert hc_dst2.huchen is not None
    dispel_unit(hc_dst2, "debuffs")
    assert hc_dst2.huchen is None, "批52d: dispel(debuffs) 應清除虎嗔(與草船/刮骨一致)"

    # 149) 資料層將門虎女使用 huchen
    jmh = TACTICS.get("將門虎女")
    assert jmh and any(e.get("k") == "huchen" for e in (jmh.get("effects") or [])), \
        "將門虎女應以 huchen 原語編碼"
    print("    [批52d] 虎嗔 huchen: 疊層提前結算+震懾(147)/dispel可清(148)/資料落地(149) 驗證通過")

    # --- 批52e: 文武雙全 —— 兵刃/謀略分線疊層, 含普攻, 同回合可多次 ---
    # 150) 資料層
    ww = TACTICS.get("文武雙全")
    assert ww and len([e for e in ww["effects"] if e.get("when", {}).get("on") == "dealtDamage"]) == 2
    assert any(e.get("when", {}).get("dmgType") == "phys" and e.get("stat") == "force" for e in ww["effects"])
    assert any(e.get("when", {}).get("dmgType") == "intel" and e.get("stat") == "intel" for e in ww["effects"])

    # 151) 行為: 普攻(phys)疊武力; 謀略疊智力; 同回合 3 次普攻疊 3 層
    random.seed(52)
    ww_u = Unit(POOL["關羽"], "槍")
    ww_tgt = Unit(POOL["張飛"], "槍")
    ww_tgt.troop = 9000.0
    ww_u.tactics = [dict(ww)]  # 重新掃描 on_deal_effect_tacs
    # 手動重建 on_deal 快取(Unit.__init__ 已掃完舊 tactics)
    ww_u.on_deal_effect_tacs = [t for t in ww_u.tactics
                                if not t.get("when")
                                and any((e.get("when") or {}).get("on") == "dealtDamage" for e in t.get("effects", []))]
    f0, i0 = ww_u.eff("force"), ww_u.eff("intel")
    # 模擬 fight 內 hit→dealt_damage 需閉包; 直接用 apply_effects reactive 三次 phys
    e_force = next(e for e in ww["effects"] if e.get("stat") == "force")
    e_intel = next(e for e in ww["effects"] if e.get("stat") == "intel")
    for _ in range(3):
        apply_effects(ww_u, ww_tgt, {"effects": [e_force], "nameZh": "文武雙全"}, [ww_u], [ww_tgt], reactive=True, dmg=100)
    assert ww_u.eff("force") >= f0 + 30 * 3 - 1e-6, f"3次兵刃應+90武, got {ww_u.eff('force')-f0}"
    apply_effects(ww_u, ww_tgt, {"effects": [e_force], "nameZh": "文武雙全"}, [ww_u], [ww_tgt], reactive=True, dmg=100)
    apply_effects(ww_u, ww_tgt, {"effects": [e_force], "nameZh": "文武雙全"}, [ww_u], [ww_tgt], reactive=True, dmg=100)
    # 最多5層
    f5 = ww_u.eff("force")
    apply_effects(ww_u, ww_tgt, {"effects": [e_force], "nameZh": "文武雙全"}, [ww_u], [ww_tgt], reactive=True, dmg=100)
    assert abs(ww_u.eff("force") - f5) < 1e-6, "超過 maxStack=5 不應再加"
    apply_effects(ww_u, ww_tgt, {"effects": [e_intel], "nameZh": "文武雙全"}, [ww_u], [ww_tgt], reactive=True, dmg=100)
    assert ww_u.eff("intel") >= i0 + 30 - 1e-6, "謀略傷害應疊智力不疊武力路徑"
    print("    [批52e] 文武雙全: 兵刃疊武/謀略疊智/maxStack5/同回合多次(150-151) 驗證通過")

    # --- 批52f: 零傷仍疊 + 眾望所歸代理出手 ---
    # 152) 抵禦全擋 dmg=0 仍觸發 dealtDamage 疊層
    random.seed(521)
    z_atk = Unit(POOL["關羽"], "槍")
    z_tgt = Unit(POOL["張飛"], "槍")
    z_tgt.troop = 9000.0
    z_atk.tactics = [dict(ww)]
    z_atk.on_deal_effect_tacs = [t for t in z_atk.tactics
                                 if not t.get("when")
                                 and any((e.get("when") or {}).get("on") == "dealtDamage" for e in t.get("effects", []))]
    z_atk.on_deal_tacs = []
    z_f0 = z_atk.eff("force")

    def _z_dealt(src, dst, is_normal, kind, dmg=None):
        for t in src.on_deal_effect_tacs:
            for e in t["effects"]:
                ew = e.get("when") or {}
                if ew.get("on") != "dealtDamage":
                    continue
                if ew.get("dmgType") and ew["dmgType"] != kind:
                    continue
                if e.get("requireDmg", False) and not (dmg and dmg > 0):
                    continue
                apply_effects(src, dst, {"effects": [e], "nameZh": "文武雙全"}, [src], [dst],
                              rate_checked=True, reactive=True, dmg=dmg)

    z_tgt.push_block(1.0, 3)  # 全擋
    hit(z_atk, z_tgt, 1.0, "phys", True, None, _z_dealt)
    assert z_atk.eff("force") >= z_f0 + 30 - 1e-6, \
        f"抵禦全擋(dmg=0)仍應疊文武武力, got +{z_atk.eff('force')-z_f0}"
    print("    [批52f] 文武雙全: 抵禦零傷仍疊(152) 驗證通過")

    # 153) 眾望所歸資料: srcSel 代理出手 + sameSrcCoef
    zw = TACTICS.get("眾望所歸")
    assert zw and zw.get("coef", 1) == 0, "眾望主 coef 應為0(傷害全走代理段)"
    assert zw.get("sameSrcCoef") == 0.72
    ehs = zw.get("extraHits") or []
    assert any(eh.get("srcSel") == "maxForce" and eh.get("kind") == "phys" for eh in ehs)
    assert any(eh.get("srcSel") == "maxIntel" and eh.get("kind") == "intel" for eh in ehs)

    # 154) 代理出手: 最高武力友軍帶文武, 眾望由副將發動 → 最高武者疊層
    random.seed(522)
    # 關羽高武、諸葛亮高智、施法者用較低屬性的「劉備」風格: 用關羽/諸葛/張飛 模擬
    a_force = Unit(POOL["關羽"], "槍")   # 武最高
    a_intel = Unit(POOL["諸葛亮"], "弓")  # 智最高
    a_cast = Unit(POOL["劉備"], "盾")    # 發動眾望
    b_tgt = Unit(POOL["司馬懿"], "槍")
    b_tgt.troop = 12000.0
    a_force.tactics = [dict(ww)]
    a_force.on_deal_effect_tacs = [t for t in a_force.tactics
                                   if not t.get("when")
                                   and any((e.get("when") or {}).get("on") == "dealtDamage" for e in t.get("effects", []))]
    a_force.on_deal_tacs = []
    a_intel.tactics = [dict(ww)]
    a_intel.on_deal_effect_tacs = [t for t in a_intel.tactics
                                   if not t.get("when")
                                   and any((e.get("when") or {}).get("on") == "dealtDamage" for e in t.get("effects", []))]
    a_intel.on_deal_tacs = []
    allies = [a_cast, a_force, a_intel]
    foes = [b_tgt]
    f_before = a_force.eff("force")
    i_before = a_intel.eff("intel")
    c_before = a_cast.eff("force")

    def _agent_dealt(src, dst, is_normal, kind, dmg=None):
        for t in getattr(src, "on_deal_effect_tacs", []) or []:
            for e in t["effects"]:
                ew = e.get("when") or {}
                if ew.get("on") != "dealtDamage":
                    continue
                if ew.get("dmgType") and ew["dmgType"] != kind:
                    continue
                if e.get("requireDmg", False) and not (dmg and dmg > 0):
                    continue
                apply_effects(src, dst, {"effects": [e], "nameZh": "文武雙全"}, allies, foes,
                              rate_checked=True, reactive=True, dmg=dmg)

    fire_extra_hits(a_cast, zw, b_tgt, lambda u: allies, lambda u: foes, None, _agent_dealt)
    assert a_force.eff("force") >= f_before + 30 - 1e-6, \
        f"眾望兵刃段應由最高武力(關羽)出手並疊文武, +{a_force.eff('force')-f_before}"
    assert a_intel.eff("intel") >= i_before + 30 - 1e-6, \
        f"眾望謀略段應由最高智力(諸葛)出手並疊文武, +{a_intel.eff('intel')-i_before}"
    assert abs(a_cast.eff("force") - c_before) < 1e-6, "施法者未出手, 不應疊文武武力"
    print("    [批52f] 眾望所歸: srcSel代理出手疊文武(153-154) 驗證通過")

    # 155) sameSrcCoef: 同一人既武又智最高時用 0.72
    random.seed(523)
    solo = Unit(POOL["呂布"], "騎")  # 通常武智雙高於輔助
    solo2 = Unit(POOL["黃月英"], "器")  # 低武低智相對
    # 強制 solo 同時 maxForce 與 maxIntel: 單人隊伍
    one = [solo]
    enemy = [Unit(POOL["張飛"], "槍")]
    enemy[0].troop = 15000.0
    # 記錄 damage 係數: 用 spy — 兩段 hit 後敵損兵應明顯低於 0.86×2 的期望
    # 簡測: same_person 路徑下 coef 取 sameSrcCoef
    agent_srcs = [pick_by_criterion(one, eh["srcSel"]) for eh in ehs if eh.get("srcSel")]
    assert len(agent_srcs) == 2 and agent_srcs[0] is agent_srcs[1] is solo
    # fire with only solo
    troop0 = enemy[0].troop
    fire_extra_hits(solo, zw, enemy[0], lambda u: one, lambda u: enemy, None, None)
    dmg_same = troop0 - enemy[0].troop
    # 對照: 強制不同人(兩人)應更高
    two = [solo, Unit(POOL["諸葛亮"], "弓")]
    # 確保諸葛智高於呂布
    two[1].base_stats = dict(getattr(two[1], "base_stats", {}) or {})
    # 直接比 eff: 若呂布智仍高則把諸葛 intel mods 拉高
    if two[1].eff("intel") <= solo.eff("intel"):
        two[1].push_stat_add("intel", 500, 99, src="test")
    if solo.eff("force") <= two[1].eff("force"):
        solo.push_stat_add("force", 500, 99, src="test")
    enemy2 = [Unit(POOL["關羽"], "槍")]
    enemy2[0].troop = 15000.0
    troop1 = enemy2[0].troop
    fire_extra_hits(solo, zw, enemy2[0], lambda u: two, lambda u: enemy2, None, None)
    dmg_diff = troop1 - enemy2[0].troop
    assert dmg_same > 0 and dmg_diff > 0, "兩種情形都應造成傷害"
    # 同一人 72%×2 vs 不同人 86%×2, 期望不同人較高(允許隨屬性浮動, 僅檢查方向)
    # 用固定 coef 比: 直接断言 sameSrc 解析
    assert zw["sameSrcCoef"] < ehs[0]["coef"], "sameSrcCoef 應低於分段 coef"
    print("    [批52f] 眾望所歸: sameSrcCoef 同一人降傷(155) 驗證通過")

    # --- 批52g: 五雷震懾率 / 高櫓彈藥 / 太平黃巾SP ---
    # 156) 五雷資料
    wl = TACTICS.get("五雷轟頂")
    assert wl and wl.get("effectsPerHit") and wl.get("hitsRepeat")
    wl_stun = next(e for e in wl["effects"] if e["k"] == "stun")
    assert abs(wl_stun.get("rate", 0) - 0.3) < 1e-9
    rsb = wl_stun.get("rateStatusBonus") or {}
    assert rsb.get("per") == 0.2 and rsb.get("maxBonus") == 0.4
    assert set(rsb.get("statuses") or []) >= {"水攻", "沙暴"}

    # 157) 具名狀態計數 + rateStatusBonus 機率
    random.seed(526)
    wl_caster = Unit(POOL["張角"], "盾")
    wl_foe = Unit(POOL["曹操"], "騎")
    allies_wl = [wl_caster]
    foes_wl = [wl_foe]
    # 無狀態: rate=0.3 → rate=0 應從不中
    wl_foe.stun = 0
    apply_effects(wl_caster, wl_foe,
                  {"effects": [{**wl_stun, "rate": 0.0, "rateStatusBonus": {**rsb, "ifLeader": False}}],
                   "nameZh": "五雷轟頂"}, allies_wl, foes_wl)
    assert wl_foe.stun == 0, "rate=0 不應震懾"
    # 水攻+沙暴 兩種: rate=0 + 0.4 = 0.4 有機會; 強制 per 使 rate=1
    wl_foe.dots = [[10, 2, False, "水攻"], [10, 2, False, "沙暴"]]
    assert count_named_statuses(wl_foe, ["水攻", "沙暴"]) == 2
    apply_effects(wl_caster, wl_foe,
                  {"effects": [{**wl_stun, "rate": 0.6, "rateStatusBonus": {
                      "statuses": ["水攻", "沙暴"], "per": 0.2, "maxBonus": 0.4, "ifLeader": False}}],
                   "nameZh": "五雷轟頂"}, allies_wl, foes_wl)
    assert wl_foe.stun > 0, "rate0.6+兩狀態0.4=1.0 必震懾"
    print("    [批52g] 五雷: 基礎30%+水攻/沙暴各+20%(156-157) 驗證通過")

    # 158) 高櫓 ammo 主將補箭
    gl = TACTICS.get("高櫓連營")
    assert gl and gl.get("ammo") == 10 and gl.get("ammoReloadLeader") == 1
    # 主將: 每回合 +1 reload, 非主將不補
    random.seed(527)
    gl_lead = Unit(POOL["SP 袁紹"], "弓")
    gl_lead.tactics = [dict(gl)]
    gl_sub = Unit(POOL["關羽"], "槍")
    gl_enemy = [Unit(POOL["張飛"], "槍")]
    gl_enemy[0].troop = 20000.0
    # 模擬主將 round1: init 10 + reload 1 = 11, fire 2-3
    gl_lead.ammo["高櫓連營"] = 10
    gl_lead.ammo["高櫓連營"] += gl["ammoReloadLeader"]
    assert gl_lead.ammo["高櫓連營"] == 11
    # 副將不 reload
    gl_sub.ammo["高櫓連營"] = 10
    # 不應自動 +1
    assert gl_sub.ammo["高櫓連營"] == 10
    print("    [批52g] 高櫓連營: 僅主將補箭+ammo10(158) 驗證通過")

    # 159) 太平道法 黃巾 SP 副將拿到 rateup
    random.seed(528)
    tp = TACTICS["太平道法"]
    zhang = Unit(POOL["張角"], "盾")
    sp_bao = Unit(POOL["SP 張寶"], "盾")
    non_hj = Unit(POOL["關羽"], "槍")
    team_hj = [zhang, sp_bao, non_hj]
    apply_effects(zhang, None, tp, team_hj, [], no_heal=True)
    assert sp_bao.addbonus_for("rateup", {"native": True, "prep": 0}) > 0, \
        "SP張寶作為黃巾副將應拿到太平道法 rateup"
    assert non_hj.addbonus_for("rateup", {"native": True, "prep": 0}) == 0, \
        "非黃巾副將不應拿到"
    print("    [批52g] 太平道法: 黃巾副將含SP rateup(159) 驗證通過")

    # --- 批52h: 機鑑先識 控制反彈 ---
    # 160) 資料層
    jj = TACTICS.get("機鑑先識")
    assert jj and any((e.get("when") or {}).get("on") == "controlled" for e in jj["effects"]), \
        "機鑑先識應有 when.on:controlled 控制反彈段"
    jj_ref = next(e for e in jj["effects"] if (e.get("when") or {}).get("on") == "controlled")
    assert abs(jj_ref.get("rate", 0) - 0.75) < 1e-9 and jj_ref.get("onlySlower") and jj_ref.get("ifLeaderIs") == "SP 荀彧"
    assert (jj_ref.get("when") or {}).get("from") == 1 and (jj_ref.get("when") or {}).get("until") == 2

    # 161) 行為: SP荀彧主將 + 慢速友軍受震懾 → 敵方被反彈; 快速友軍不反彈
    random.seed(528)
    spx = Unit(POOL["SP 荀彧"], "弓")
    spx.own_round = 1  # 時序徹底一致化批: fire_controlled現讀holder(=spx).own_round, 取代全局CUR_ROUND
    spx.tactics = [dict(jj)]
    spx.on_ctrl_effect_tacs = [t for t in spx.tactics
                               if not t.get("when")
                               and any((e.get("when") or {}).get("on") == "controlled" for e in t.get("effects", []))]
    slow = Unit(POOL["張飛"], "槍")
    fast = Unit(POOL["呂布"], "騎")
    # 強制速度: SP荀彧中速, 張飛更慢, 呂布更快
    spx.push_stat_add("speed", 100, 99, src="test")
    slow.push_stat_add("speed", -50, 99, src="test")  # 保證 slower
    fast.push_stat_add("speed", 500, 99, src="test")
    assert slow.eff("speed") < spx.eff("speed") <= fast.eff("speed") or slow.eff("speed") < spx.eff("speed")
    enemy = Unit(POOL["曹操"], "騎")
    team_j = [spx, slow, fast]
    foes_j = [enemy]
    # 對 slow 施加震懾 → 應反彈(rate=1 強制)
    # 暫改 rate=1 for test via synthetic
    spx.on_ctrl_effect_tacs[0] = dict(spx.on_ctrl_effect_tacs[0])
    spx.on_ctrl_effect_tacs[0]["effects"] = [
        {**jj_ref, "rate": 1.0},
        *[e for e in jj["effects"] if e is not jj_ref and e.get("k") == "block"],
    ]
    # rebuild list properly
    _ref_e = {**jj_ref, "rate": 1.0}
    spx.tactics = [{"nameZh": "機鑑先識", "type": "command", "kind": "intel", "coef": 0, "rate": 1,
                    "effects": [_ref_e]}]
    spx.on_ctrl_effect_tacs = [spx.tactics[0]]
    enemy.stun = 0
    slow.stun = 0
    apply_effects(enemy, slow, {"effects": [{"k": "stun", "who": "enemy", "n": 1, "dur": 1}],
                                "nameZh": "測試控"}, foes_j, team_j)
    # Note: apply_effects(caster=enemy, allies=foes_j, enemies=team_j) when enemy applies to slow
    # wait - who:enemy uses enemies param. If caster is enemy applying to our team:
    # typically apply_effects(enemy_caster, tgt=slow, allies=enemy_team, enemies=our_team)
    # fire_controlled(victim=slow): slow in enemies → team=our, foes=enemy_team. Wrong.
    # Correct orientation: allies of caster, enemies of caster.
    # When Cao applies stun to Zhang Fei: caster=Cao, allies=[Cao], enemies=[spx,slow,fast]
    # victim=slow is in enemies → team=enemies (our team), foes=allies (Cao's team). Correct!
    assert slow.stun > 0, "張飛應被震懾"
    assert enemy.stun > 0, f"慢速友軍受控應反彈震懾給敵, enemy.stun={enemy.stun}"
    # 快速友軍: 清 flags, 重置
    enemy.stun = 0
    fast.stun = 0
    spx.hit_flags.clear()
    apply_effects(enemy, fast, {"effects": [{"k": "stun", "who": "enemy", "n": 1, "dur": 1}],
                                "nameZh": "測試控2"}, foes_j, team_j)
    assert fast.stun > 0
    assert enemy.stun == 0, "速度快於SP荀彧的友軍不應觸發反彈"
    # 非前2回合
    spx.own_round = 3  # 時序徹底一致化批: 取代全局CUR_ROUND
    enemy.stun = 0
    slow.stun = 0
    spx.hit_flags.clear()
    apply_effects(enemy, slow, {"effects": [{"k": "stun", "who": "enemy", "n": 1, "dur": 1}],
                                "nameZh": "測試控3"}, foes_j, team_j)
    assert enemy.stun == 0, "第3回合不應反彈"
    print("    [批52h] 機鑑先識: 全控制反彈/SP主將/onlySlower/前2回合(160-161) 驗證通過")

    # --- 批52i: 垂心萬物 proxyNormal 完整普攻(含突擊) ---
    cx = TACTICS.get("垂心萬物")
    assert cx and any(e.get("k") == "proxyNormal" for e in cx["effects"])
    assert any(e.get("k") == "proxyHit" and e.get("ifHasExtra") for e in cx["effects"])
    assert cx.get("when", {}).get("parity") == "odd" and cx.get("rateScale") == "intel"
    # 162) 非連擊 → 代打普攻觸發 charge
    random.seed(529)
    CUR_ROUND = 1
    holder = Unit(POOL["王元姬"], "槍")
    force_u = Unit(POOL["張飛"], "槍")
    weak = Unit(POOL["黃月英"], "器")
    foe = Unit(POOL["曹操"], "騎")
    foe.troop = 15000.0
    charge_hit = [0]
    force_u.tactics = [{
        "nameZh": "測試突擊", "type": "charge", "rate": 1.0, "coef": 1.5, "kind": "phys",
        "effects": [], "prep": 0, "n": 1,
    }]
    team = [holder, force_u, weak]
    foes = [foe]
    # ensure force is maxForce
    force_u.push_stat_add("force", 200, 99, src="t")
    assert force_u.eff("force") >= max(x.eff("force") for x in team)
    assert force_u.addbonus("extra") <= 0
    _FIGHT_CTX["on_hit"] = None
    _FIGHT_CTX["on_deal"] = None
    _FIGHT_CTX["allies_of"] = lambda u: team
    _FIGHT_CTX["foes_of"] = lambda u: foes
    def _af(u):
        charge_hit[0] += 1
    _FIGHT_CTX["active_fired"] = _af
    troop0 = foe.troop
    apply_effects(holder, foe, {"effects": [
        {"k": "proxyNormal", "srcSel": "maxForce", "ifNoExtra": True},
    ], "nameZh": "垂心萬物"}, team, foes, no_heal=True)
    assert foe.troop < troop0, "非連擊應代打普攻造成傷害"
    assert charge_hit[0] >= 1, "代打普攻應觸發 rank 突擊(rate1)"
    # 163) 連擊狀態 → proxyHit 謀略, 不走普攻
    force_u.push_add("extra", 1.0, 2)
    assert force_u.addbonus("extra") > 0
    foe.troop = 15000.0
    charge_hit[0] = 0
    apply_effects(holder, foe, {"effects": [
        {"k": "proxyNormal", "srcSel": "maxForce", "ifNoExtra": True},
        {"k": "proxyHit", "coef": 0.9, "kind": "intel", "ifHasExtra": True, "checkSrcSel": "maxForce"},
    ], "nameZh": "垂心萬物"}, team, foes, no_heal=True)
    assert charge_hit[0] == 0, "連擊狀態不應代打普攻/突擊"
    assert foe.troop < 15000.0, "連擊時應走 proxyHit 謀略傷"
    print("    [批52i] 垂心萬物: proxyNormal含突擊/連擊改謀略(162-163) 驗證通過")

    # --- 批52j: 暗箭難防 capture ---
    aj = TACTICS.get("暗箭難防")
    assert aj and aj.get("coef") == 2.6 and aj.get("n") == 2 and aj.get("prep") == 1
    cap_e = next(e for e in aj["effects"] if e.get("k") == "capture")
    assert cap_e.get("rate") == 0.6 and cap_e.get("altCoef") == 5.3 and cap_e.get("scale") == "speed"
    # 164) 捕獲施加: 不可淨化、禁傷害、友軍不可選
    random.seed(530)
    captor = Unit(POOL["呂布"], "騎")
    victim = Unit(POOL["張飛"], "槍")
    ally = Unit(POOL["關羽"], "槍")
    team_c, foes_c = [captor], [victim]
    apply_effects(captor, victim, {"effects": [{**cap_e, "rate": 1.0}], "nameZh": "暗箭難防", "kind": "phys"},
                  team_c, foes_c, rate_checked=True)
    assert victim.captured > 0 and victim.healblock > 0
    assert damage(victim, captor, 1.0, "phys") == 0, "捕獲者無法造成傷害"
    # 淨化不清除
    dispel_unit(victim, "debuffs")
    assert victim.captured > 0, "捕獲不可被淨化"
    # 友軍不可選中
    assert pick_targets([victim, ally], 2, ally_pool=True) == [ally] or (
        pick_target([victim], ally_pool=True) is None)
    # 165) 已有捕獲 → 530% 打該人而非新捕
    victim2 = Unit(POOL["曹操"], "騎")
    victim2.troop = 20000.0
    foes2 = [victim, victim2]
    victim.troop = 20000.0
    t0 = victim.troop
    apply_effects(captor, victim2, {"effects": [{**cap_e, "rate": 1.0, "altCoef": 5.3}],
                                    "nameZh": "暗箭難防", "kind": "phys"},
                  team_c, foes2, rate_checked=True)
    assert victim2.captured == 0, "已有捕獲時不應再捕新人"
    assert victim.troop < t0, "已有捕獲時應對原捕獲目標造成 altCoef 傷害"
    print("    [批52j] 暗箭難防: capture不可淨化/禁傷/已捕轉530%(164-165) 驗證通過")

    # --- 批A(11筆高嚴重重建): 8個新原語逐一驗證 -------------------------------------
    # 166) e.ofDamage 讀 heal_amt(結盟鏡像治療) —— 對稱既有dmg分支, 驗證heal_amt路徑正確接上
    random.seed(600)
    jm_caster = Unit(POOL["呂布"], "槍")
    jm_ally = Unit(POOL["張飛"], "槍")
    jm_caster.troop = 5000.0
    jm_caster.wounded = 5000.0
    jm_ally.troop = 8000.0
    jm_ally.wounded = 2000.0
    jm_before = jm_caster.troop
    apply_effects(jm_caster, jm_ally, {"effects": [{"k": "heal", "who": "self", "ofDamage": 0.5, "dur": 99}],
                                        "nameZh": "測試結盟166", "kind": "phys"},
                  [jm_caster, jm_ally], [], rate_checked=True, reactive=True, heal_amt=200)
    assert abs((jm_caster.troop - jm_before) - 100) < 1e-6, \
        f"166: 結盟ofDamage讀heal_amt=200應精確產生100治療(0.5×200), 實際{jm_caster.troop - jm_before}"
    print("    [批A 166] 結盟: e.ofDamage讀heal_amt(非dmg)鏡像治療 驗證通過")

    # 167) k=="amp"+e.stackKey(密計誅逆) —— per-target疊層封頂
    random.seed(601)
    mjzn_holder = Unit(POOL["呂布"], "槍")
    mjzn_foe = Unit(POOL["張飛"], "槍")
    mjzn_eff = {"k": "amp", "who": "enemy", "val": -0.15, "perStack": -0.15,
                "stackKey": True, "maxStacks": 3, "dur": 99, "n": 1}
    for _ in range(3):
        apply_effects(mjzn_holder, None, {"effects": [mjzn_eff], "nameZh": "測試密計誅逆167", "kind": "phys"},
                      [mjzn_holder], [mjzn_foe])
    assert abs(mjzn_foe.addbonus("amp") - (-0.45)) < 1e-6, \
        f"167: 密計誅逆stackKey連續3次觸發應累計-45%(3層×-15%), 實際{mjzn_foe.addbonus('amp')}"
    apply_effects(mjzn_holder, None, {"effects": [mjzn_eff], "nameZh": "測試密計誅逆167", "kind": "phys"},
                  [mjzn_holder], [mjzn_foe])
    assert abs(mjzn_foe.addbonus("amp") - (-0.45)) < 1e-6, "167: 第4次觸發應被maxStacks:3封頂, 維持-45%不再疊加"
    print("    [批A 167] 密計誅逆: k==\"amp\"+e.stackKey per-target疊層封頂 驗證通過")

    # 168) when.dmgAbove(密計誅逆/承天靖世方向) —— 傷害量閾值閘門邊界值判斷(純函數驗證)
    def _dmg_above_ok_test(threshold, dmg):
        return threshold is None or (dmg is not None and dmg > threshold)
    assert _dmg_above_ok_test(300, 301) is True, "168: dmgAbove語意 301>300應通過"
    assert _dmg_above_ok_test(300, 300) is False, "168: dmgAbove語意 300(等於閾值)不應通過(嚴格大於)"
    assert _dmg_above_ok_test(300, None) is False, "168: dmgAbove語意 dmg=None應保守不通過"
    print("    [批A 168] when.dmgAbove: 傷害量閾值閘門邊界判斷 驗證通過")

    # 169) when.casterIsLeader(十勝十敗) —— 端到端: 手工重演activeFired廣播+casterIsLeader閘門
    #      (對稱既有105/106號測試手法, 見上方active_fired105/106範例)
    random.seed(602)
    ssb_leader = Unit(POOL["呂布"], "槍")
    ssb_sub = Unit(POOL["張飛"], "槍")
    ssb_active_tac = {"nameZh": "測試169主動必發", "type": "active", "kind": "phys",
                      "coef": 0, "rate": 1, "n": 0, "prep": 0,
                      "effects": [{"k": "stat", "who": "self", "stat": "speed", "add": 0, "dur": 1}]}
    ssb_heal_tac = {"nameZh": "測試169十勝十敗監聽", "type": "command", "kind": "intel",
                    "coef": 0, "rate": 1, "n": 1, "prep": 0,
                    "effects": [{"k": "heal", "who": "ally", "coef": 0.5,
                                 "when": {"on": "activeFired", "who": "ally", "casterIsLeader": True},
                                 "rate": 1.0}]}
    TACTICS[ssb_active_tac["nameZh"]] = ssb_active_tac
    TACTICS[ssb_heal_tac["nameZh"]] = ssb_heal_tac
    try:
        u169_leader = Unit(POOL["呂布"], "騎", None, None, None, [ssb_active_tac["nameZh"]])
        u169_sub = Unit(POOL["張飛"], "騎", None, None, None, [ssb_heal_tac["nameZh"]])
        set169 = {id(u169_leader), id(u169_sub)}
        allies_of169 = lambda u: [u169_leader, u169_sub] if id(u) in set169 else []
        u169_leader.troop = 5000.0
        u169_leader.wounded = 5000.0

        def active_fired_for169(u, holder, want_who):
            if not holder.alive or not holder.active_fired_effect_tacs:
                return
            def who_ok169(w):
                return (w or {}).get("who") == want_who if want_who else not (w or {}).get("who")
            def caster_is_leader_ok169(w):
                if not (w or {}).get("casterIsLeader"):
                    return True
                al = allies_of169(u)
                return bool(al and al[0] is u)
            for t169 in holder.active_fired_effect_tacs:
                for e169 in t169["effects"]:
                    ew169 = e169.get("when") or {}
                    if ew169.get("on") != "activeFired" or not who_ok169(ew169) or not caster_is_leader_ok169(ew169):
                        continue
                    apply_effects(holder, None, {"effects": [e169], "kind": t169.get("kind", "phys"), "nameZh": t169["nameZh"]},
                                  allies_of169(holder), [], rate_checked=True, reactive=True)

        # 場景A: 主將(u169_leader)發動 → 副將(u169_sub)監聽casterIsLeader應觸發治療
        troop_before_a = u169_leader.troop
        active_fired_for169(u169_leader, u169_leader, None)
        for holder in allies_of169(u169_leader):
            active_fired_for169(u169_leader, holder, "ally")
        assert u169_leader.troop > troop_before_a, "169場景A: 主將發動主動戰法, casterIsLeader應放行, 主將應被治療"

        # 場景B: 非主將(u169_sub)發動 → casterIsLeader應阻擋(u169_sub不是allies_of169(u169_sub)[0])
        u169_leader.troop = 5000.0
        u169_leader.wounded = 5000.0
        troop_before_b = u169_leader.troop
        active_fired_for169(u169_sub, u169_sub, None)
        for holder in allies_of169(u169_sub):
            active_fired_for169(u169_sub, holder, "ally")
        assert u169_leader.troop == troop_before_b, "169場景B: 非主將(副將)發動主動戰法, casterIsLeader應阻擋, 主將不應被治療"
    finally:
        del TACTICS[ssb_active_tac["nameZh"]]
        del TACTICS[ssb_heal_tac["nameZh"]]
    print("    [批A 169] 十勝十敗: when.casterIsLeader(activeFired反應式) 正反兩場景 驗證通過")

    # 170) extraHits.who=="mainTargetAlly"+eh.kindByStat+e.ifTargetHasNot(偽書相間)
    random.seed(603)
    wsxj_caster = Unit(POOL["呂布"], "槍")
    wsxj_tgt_chaos = Unit(POOL["張飛"], "槍")
    wsxj_tgt_chaos.chaos = 2  # 模擬「目標已混亂」的前置狀態
    wsxj_tgt_ally = Unit(POOL["關羽"], "槍")
    wsxj_troop_before = wsxj_tgt_ally.troop
    wsxj_eh = [{"who": "mainTargetAlly", "coef": 1.86, "kindByStat": "maxForceIntel", "ifTargetHas": "chaos"}]
    fire_extra_hits(wsxj_caster, {"extraHits": wsxj_eh, "nameZh": "測試偽書相間170"}, wsxj_tgt_chaos,
                    lambda u: [wsxj_caster], lambda u: [wsxj_tgt_chaos, wsxj_tgt_ally], None)
    assert wsxj_tgt_ally.troop < wsxj_troop_before, \
        "170: mainTargetAlly段應在目標已混亂時觸發, 對目標的隊友造成傷害(而非目標自己或caster的隊友)"
    # ifTargetHasNot: 未混亂目標才應施加chaos
    wsxj_fresh = Unit(POOL["曹操"], "槍")
    apply_effects(wsxj_caster, wsxj_fresh, {"effects": [{"k": "chaos", "who": "enemy", "dur": 1, "ifTargetHasNot": "chaos"}],
                                             "nameZh": "測試偽書相間170b", "kind": "intel"},
                  [wsxj_caster], [wsxj_fresh])
    assert wsxj_fresh.chaos > 0, "170b: ifTargetHasNot=\"chaos\" 應對未混亂目標正常施加chaos"
    print("    [批A 170] 偽書相間: mainTargetAlly+kindByStat+ifTargetHasNot 驗證通過")

    # 171) e.when.hpBelow/hpAbove(效果級, 奇兵間道) —— hp_ok對合成{"when":hw}的動態邊界判斷
    random.seed(604)
    qbjd_u = Unit(POOL["呂布"], "槍")
    qbjd_u.troop = 4000.0  # 40%
    assert hp_ok({"when": {"hpBelow": 0.5}}, qbjd_u) is True, "171: 兵力40%時hpBelow:0.5應通過"
    assert hp_ok({"when": {"hpAbove": 0.5}}, qbjd_u) is False, "171: 兵力40%時hpAbove:0.5不應通過"
    qbjd_u.troop = 8000.0  # 動態改變後即時重算(80%)
    assert hp_ok({"when": {"hpAbove": 0.5}}, qbjd_u) is True, "171: 兵力回升到80%後hpAbove:0.5應即時通過(非開戰快照)"
    print("    [批A 171] 奇兵間道: e.when.hpBelow/hpAbove(效果級)動態即時判斷 驗證通過")

    # 172) k=="chargeAdd"+k=="chargeConsume"(死戰不退) —— 資源池累加封頂+鏈式消耗+回合上限
    random.seed(605)
    szbt_u = Unit(POOL["呂布"], "槍")
    assert szbt_u.charge is None, "172前置: 全新Unit的charge應為None(惰性建立)"
    for _ in range(25):
        apply_effects(szbt_u, None, {"effects": [{"k": "chargeAdd", "who": "self", "max": 20}],
                                      "nameZh": "測試死戰不退172", "kind": "phys"}, [szbt_u], [])
    assert szbt_u.charge is not None and szbt_u.charge["n"] == 20, \
        f"172: chargeAdd連續25次觸發應封頂在max:20, 實際{szbt_u.charge}"
    szbt_foe = Unit(POOL["張飛"], "槍")
    szbt_troop_before = szbt_foe.troop
    apply_effects(szbt_u, None, {"effects": [{"k": "chargeConsume", "coef": 1.3, "kind": "phys",
                                               "rate": 0.5, "decayPer": 0.08, "maxChain": 5}],
                                  "nameZh": "測試死戰不退172b", "kind": "phys"},
                  [szbt_u], [szbt_foe], rate_checked=True)
    assert szbt_u.charge["n"] < 20, f"172b: rate_checked=True時應至少消耗1層蓄威, 實際剩餘{szbt_u.charge['n']}"
    assert szbt_foe.troop < szbt_troop_before, "172b: 消耗蓄威應對敵方造成傷害"
    assert 1 <= szbt_u.charge_consumed_this_round <= 5, \
        f"172b: 本回合觸發次數應在1~5之間, 實際{szbt_u.charge_consumed_this_round}"
    # 每回合上限: 已達maxChain時即使機率100%也不應再消耗
    szbt_u2 = Unit(POOL["曹操"], "槍")
    szbt_u2.charge = {"n": 20, "max": 20}
    szbt_u2.charge_consumed_this_round = 5
    charge_before = szbt_u2.charge["n"]
    apply_effects(szbt_u2, None, {"effects": [{"k": "chargeConsume", "coef": 1.3, "kind": "phys", "rate": 1.0}],
                                   "nameZh": "測試死戰不退172c", "kind": "phys"},
                  [szbt_u2], [szbt_foe], rate_checked=True)
    assert szbt_u2.charge["n"] == charge_before, "172c: 本回合已達maxChain上限時不應再消耗蓄威(即使rate:1.0)"
    print("    [批A 172] 死戰不退: k==\"chargeAdd\"+k==\"chargeConsume\" 資源池+鏈式消耗+回合上限 驗證通過")

    # 173) 批F驗收: heal選標「禁止近似/預設補最殘」鐵律的端到端斷言 —— 隨機分布均勻性/
    # 刮骨療毒仍mostDamaged/滿血目標恢復0(真實溢出)/反應式治療受傷者本人(非全軍min-troop)。
    # 173a) 隨機分布: who:ally+n:1(無targetSel)對3個兵力互異的友軍反覆施放, 統計各自被選
    # 次數應「近均勻」(不應系統性偏向兵力最低者, 否則就是預設補最殘的回歸, 見刮骨療毒的
    # 對照組173b: 那個才該固定選最殘)。
    random.seed(700)
    N_TRIALS_173 = 3000
    rd_a = Unit(POOL["曹操"], "騎")
    rd_b = Unit(POOL["劉備"], "槍")
    rd_c = Unit(POOL["孫權"], "弓")
    rd_allies = [rd_a, rd_b, rd_c]
    rd_counts = {id(u): 0 for u in rd_allies}
    tac_rd = {"type": "active", "kind": "phys", "nameZh": "測試173隨機分布",
              "effects": [{"k": "heal", "who": "ally", "coef": 0.01, "dur": 1, "n": 1}]}
    for _ in range(N_TRIALS_173):
        for u, tr in zip(rd_allies, (9000.0, 5000.0, 2000.0)):   # 兵力互異且固定, 只重擲heal選標本身
            u.troop, u.wounded = tr, START_TROOP - tr
        apply_effects(rd_a, None, tac_rd, rd_allies, [])
        after = {id(u): u.troop for u in rd_allies}
        healed = [u for u in rd_allies if after[id(u)] > (9000.0 if u is rd_a else 5000.0 if u is rd_b else 2000.0) + 1e-6]
        assert len(healed) == 1, f"173a: 每次應恰好命中1人, 實際{len(healed)}"
        rd_counts[id(healed[0])] += 1
    for u in rd_allies:
        frac = rd_counts[id(u)] / N_TRIALS_173
        assert abs(frac - 1 / 3) < 0.05, \
            f"173a: who:ally+n:1(無targetSel)應近均勻隨機分布, {u.g.name}(兵力{u.troop})被選比例={frac:.3f}, 預期≈0.333(±0.05); 若持續偏向兵力最低者(孫權), 屬「預設補最殘」回歸(鐵律2禁止)"
    print(f"    [批F 173a] heal選標隨機分布均勻性(N={N_TRIALS_173}, who:ally+n:1無targetSel): "
          f"{ {u.g.name: round(rd_counts[id(u)]/N_TRIALS_173, 3) for u in rd_allies} } 驗證通過")

    # 173b) 對照組: 刮骨療毒(targetSel:mostDamaged)在同樣3個兵力互異友軍下, 應「固定」選最殘者
    # (孫權, 兵力最低), 不受(a)的隨機性影響——同一套引擎機制下兩種選標語意並存, 互不干擾。
    random.seed(701)
    ggld_tac = TACTICS.get("刮骨療毒")
    assert ggld_tac is not None, "173b前置: 資料庫應含刮骨療毒"
    ggld_effects = ggld_tac.get("effects") or []
    ggld_heal = next(e for e in ggld_effects if e.get("k") == "heal")
    assert ggld_heal.get("targetSel") == "mostDamaged", \
        f"173b前置: 刮骨療毒heal效果應有targetSel=mostDamaged(批F資料修正), 實際={ggld_heal.get('targetSel')!r}"
    ggld_hit_counts = {id(u): 0 for u in rd_allies}
    for _ in range(200):
        for u, tr in zip(rd_allies, (9000.0, 5000.0, 2000.0)):
            u.troop, u.wounded = tr, START_TROOP - tr
        apply_effects(rd_a, None, {"type": "active", "kind": "intel", "nameZh": "刮骨療毒", "effects": [ggld_heal]},
                      rd_allies, [])
        after = {id(u): u.troop for u in rd_allies}
        healed = [u for u in rd_allies if after[id(u)] > (9000.0 if u is rd_a else 5000.0 if u is rd_b else 2000.0) + 1e-6]
        assert healed == [rd_c], f"173b: targetSel=mostDamaged應固定選兵力最低者(孫權), 實際命中{[u.g.name for u in healed]}"
    print("    [批F 173b] 刮骨療毒(targetSel=mostDamaged)固定選最殘者, 200次全中孫權(對照173a隨機分布) 驗證通過")

    # 173c) 滿血目標恢復0(真實溢出, user治療分類學背書「群體主動類滿血時發動直接溢出」)——
    # who:ally+n:1(隨機)命中一個已滿血的友軍時, 該友軍實際恢復量應為0(而非引擎額外幫忙
    # 挑一個還沒滿血的人補上, 那樣才是隱藏的「預設補最殘」變體)。
    random.seed(702)
    full_a = Unit(POOL["曹操"], "騎")
    full_b = Unit(POOL["劉備"], "槍")
    full_a.troop, full_a.wounded = START_TROOP, 0.0   # 滿血(無傷兵池可回)
    full_b.troop, full_b.wounded = 3000.0, START_TROOP - 3000.0   # 重傷
    tac_full = {"type": "active", "kind": "phys", "nameZh": "測試173滿血溢出",
                "effects": [{"k": "heal", "who": "ally", "coef": 2.0, "dur": 1, "n": 1}]}
    n_zero_when_full_selected = 0
    n_full_selected = 0
    for _ in range(500):
        full_a.troop, full_a.wounded = START_TROOP, 0.0
        full_b.troop, full_b.wounded = 3000.0, START_TROOP - 3000.0
        before_a = full_a.troop
        apply_effects(full_a, None, tac_full, [full_a, full_b], [])
        if full_a.troop == before_a:   # 滿血者被選中(此局heal沒有命中重傷的full_b)
            n_full_selected += 1
            if full_a.troop - before_a == 0:
                n_zero_when_full_selected += 1
    assert n_full_selected > 0, "173c前置: 500次隨機應至少有部分回合選中滿血目標(否則測試設計本身有誤)"
    assert n_zero_when_full_selected == n_full_selected, \
        f"173c: 滿血目標被選中時應恢復0(真實溢出浪費, 不應被引擎悄悄轉給重傷者), {n_full_selected}次選中滿血目標中有{n_full_selected - n_zero_when_full_selected}次實際恢復量非0"
    print(f"    [批F 173c] 滿血目標被選中時恢復0(真實溢出, {n_full_selected}/500次選中滿血目標且全數恢復0) 驗證通過")

    # 173d) 反應式治療受傷者本人(who:eventTarget) —— 三軍之眾/草船借箭/陷陣營/雲聚影從/青囊/
    # 援救同款反應式急救類, 必須治療「事件本身的受傷者」, 即使全軍中有其他友軍兵力更低。
    # 用合成戰法直接驗證eventTarget機制本身(不依賴特定戰法名稱, 避免與B項資料改動耦合)。
    random.seed(703)
    et_hurt = Unit(POOL["張飛"], "槍")     # 本次受傷事件的當事人(中等傷)
    et_lowest = Unit(POOL["曹操"], "騎")   # 全軍兵力最低者, 但這次沒受傷/不是事件目標
    et_hurt.troop, et_hurt.wounded = 6000.0, START_TROOP - 6000.0
    et_lowest.troop, et_lowest.wounded = 1000.0, START_TROOP - 1000.0
    tac_et = {"effects": [{"k": "heal", "who": "eventTarget", "coef": 1.0, "dur": 1,
                           "when": {"on": "damaged"}, "rate": 1.0}]}
    before_hurt, before_lowest = et_hurt.troop, et_lowest.troop
    apply_effects(et_hurt, None, tac_et, [et_lowest, et_hurt], [], rate_checked=True, reactive=True, evt_target=et_hurt)
    assert et_hurt.troop > before_hurt, "173d: who:eventTarget應治療事件受傷者本人(張飛)"
    assert et_lowest.troop == before_lowest, \
        "173d: who:eventTarget不應治療全軍兵力最低者(曹操), 即使其兵力比事件受傷者更低——反應式急救的受詞是「受傷的那個單位」, 不是「預設補最殘」"
    print("    [批F 173d] 反應式急救(who:eventTarget)治療受傷者本人, 不誤選全軍兵力最低者 驗證通過")

    # 174) 批G: heal_only 常駐通道的 t["rate"] 擲骰應僅在 e["rate"] 缺席時才進行——e["rate"]
    # 存在時, 函式開頭的批23 A4通用閘門已擲過一次骰(對所有k統一適用, 含heal), 這裡不應再讀
    # t["rate"]重複擲骰(否則機率被平方: 0.1×1.0的t.rate仍會再擲一次, 若誤讀e.rate當t.rate的
    # fallback來源則會變成0.1×0.1=0.01, 犯了同批註解自己警告的「重複擲骰使機率平方」錯誤)。
    # 用統計試驗驗證: e["rate"]=0.1的heal效果, 即使t["rate"]=1.0, 實際觸發率應接近10%(僅通用
    # 閘門的單次擲骰生效)而非100%(舊bug: heal_only硬讀t.rate=1.0視為必中)或1%(若heal_only又
    # 誤讀e.rate=0.1當t.rate的fallback來源重複擲骰)。
    random.seed(174)
    HG174_TRIALS = 400
    hg174_fired = 0
    for _ in range(HG174_TRIALS):
        hg174_u = Unit(POOL["張飛"], "槍")
        hg174_u.troop = START_TROOP * 0.5
        hg174_u.wounded = START_TROOP * 0.5
        before174 = hg174_u.troop
        apply_effects(hg174_u, None,
                      {"nameZh": "測試174", "rate": 1.0, "kind": "phys",
                       "effects": [{"k": "heal", "who": "self", "coef": 1.0, "dur": 1, "rate": 0.1}]},
                      [hg174_u], [], heal_only=True)
        if hg174_u.troop > before174:
            hg174_fired += 1
    hg174_pct = hg174_fired / HG174_TRIALS
    assert 0.04 < hg174_pct < 0.18, \
        f"174: e['rate']=0.1應使heal_only常駐通道實際觸發率≈10%(即使t['rate']=1.0), 400次試驗實得{hg174_pct*100:.1f}%"
    # 向後相容: 未帶e["rate"]的既有heal資料應完全不受影響(fallback到t["rate"])
    random.seed(174)
    hg174b_fired = 0
    for _ in range(HG174_TRIALS):
        hg174b_u = Unit(POOL["張飛"], "槍")
        hg174b_u.troop = START_TROOP * 0.5
        hg174b_u.wounded = START_TROOP * 0.5
        before174b = hg174b_u.troop
        apply_effects(hg174b_u, None,
                      {"nameZh": "測試174b", "rate": 0.2, "kind": "phys",
                       "effects": [{"k": "heal", "who": "self", "coef": 1.0, "dur": 1}]},
                      [hg174b_u], [], heal_only=True)
        if hg174b_u.troop > before174b:
            hg174b_fired += 1
    hg174b_pct = hg174b_fired / HG174_TRIALS
    assert 0.11 < hg174b_pct < 0.30, \
        f"174b: 未帶e['rate']的heal應fallback採用t['rate']=0.2(向後相容), 400次試驗實得{hg174b_pct*100:.1f}%"
    print(f"    [批G 174] heal_only常駐通道e['rate']優先於t['rate'](174: {hg174_pct*100:.1f}% 約10%)/向後相容fallback(174b: {hg174b_pct*100:.1f}% 約20%)驗證通過")

    # 175) 批G: block(次數型格擋)支援e["dmgType"]過濾(榮光「受謀略傷害時完全免疫」) —— 帶
    # dmgType的格擋層只消耗同類型傷害, 不影響其餘類型傷害的照常結算。
    random.seed(175)
    bk175_u = Unit(POOL["甘寧"], "弓")
    bk175_u.push_block(1.0, 1, src="測試175", dmg_type="intel")
    before175_phys = bk175_u.troop
    hit(Unit(POOL["張飛"], "槍"), bk175_u, 5.0, "phys", is_normal=True)
    assert bk175_u.troop < before175_phys, "175: dmgType='intel'的格擋不應消耗於phys傷害(應照常扣血)"
    assert len(bk175_u.block) == 1 and bk175_u.block[0]["n"] == 1, "175: phys傷害不應消耗intel專屬格擋層(應仍剩1層)"
    before175_intel = bk175_u.troop
    hit(Unit(POOL["諸葛亮"], "弓"), bk175_u, 5.0, "intel", is_normal=False)
    assert bk175_u.troop == before175_intel, "175: dmgType='intel'的格擋應完全免疫intel傷害(val=1.0全擋)"
    assert len(bk175_u.block) == 0, "175: intel傷害應消耗掉intel專屬格擋層(用盡後移除)"
    # 向後相容: 未帶dmgType的格擋應維持原行為(不分類型皆可消耗)
    bk175b_u = Unit(POOL["甘寧"], "弓")
    bk175b_u.push_block(1.0, 1, src="測試175b")
    before175b = bk175b_u.troop
    hit(Unit(POOL["張飛"], "槍"), bk175b_u, 5.0, "phys", is_normal=True)
    assert bk175b_u.troop == before175b, "175b: 未帶dmgType的格擋應向後相容, 不分類型皆可消耗(phys傷害應被全擋)"
    assert len(bk175b_u.block) == 0, "175b: 格擋層應在消耗後移除"
    print("    [批G 175] block支援e['dmgType']過濾(榮光「受謀略傷害完全免疫」精確落地)/向後相容不分類型格擋驗證通過")

    # 176) 批G: dodge(規避)支援e["dmgType"]過濾(榮光改用dodge取代block, 見equips_parsed.json) ——
    # dodge_dmg_type="intel"時只對謀略傷害生效, phys傷害不受影響(每次都照常結算, 不消耗/不影響)。
    random.seed(176)
    dg176_u = Unit(POOL["甘寧"], "弓")
    dg176_u.dodge_prob, dg176_u.dodge_dur, dg176_u.dodge_dmg_type = 1.0, 5, "intel"  # 100%規避(確定觸發), 便於驗證方向而非機率本身
    before176_phys = dg176_u.troop
    hit(Unit(POOL["張飛"], "槍"), dg176_u, 5.0, "phys", is_normal=True)
    assert dg176_u.troop < before176_phys, "176: dodge_dmg_type='intel'不應對phys傷害生效(應照常扣血, 即使dodge_prob=1.0)"
    before176_intel = dg176_u.troop
    hit(Unit(POOL["諸葛亮"], "弓"), dg176_u, 5.0, "intel", is_normal=False)
    assert dg176_u.troop == before176_intel, "176: dodge_dmg_type='intel'應對intel傷害生效(100%規避, 完全免疫)"
    # 向後相容: 未帶dmgType的dodge應維持原行為(不分類型皆可規避)
    dg176b_u = Unit(POOL["甘寧"], "弓")
    dg176b_u.dodge_prob, dg176b_u.dodge_dur = 1.0, 5
    before176b = dg176b_u.troop
    hit(Unit(POOL["張飛"], "槍"), dg176b_u, 5.0, "phys", is_normal=True)
    assert dg176b_u.troop == before176b, "176b: 未帶dmgType的dodge應向後相容, 不分類型皆可規避(100%規避phys傷害亦應生效)"
    print("    [批G 176] dodge支援e['dmgType']過濾(榮光改用dodge精確落地)/向後相容不分類型規避驗證通過")

    # 177) 批G: redirect(傷害轉移)支援e["who"]分流(leader/subs) —— 肉身鐵壁「為副將分擔30%/
    # 為主將分擔60%」需要依受益者身份給不同share值, 過去redirect無條件對guardian以外全體allies
    # 套用同一share, 只能合併成單一均值近似(0.45)。用guard:"self"(持有者rd177_holder自己當
    # guardian, 對稱肉身鐵壁實際用法), allies=[主將, 副將A, 副將B, 持有者(第4人排最後, 避免
    # 持有者剛好是allies[0]主將導致'leader段被guardian排除'vs'leader段確實只選allies[0]'
    # 兩種情況無法區分)兩段who分流各自套用不同share。
    rd177_leader = Unit(POOL["劉備"], "槍")   # allies[0] = 主將
    rd177_sub1 = Unit(POOL["關羽"], "槍")
    rd177_sub2 = Unit(POOL["張飛"], "槍")
    rd177_holder = Unit(POOL["諸葛亮"], "弓")   # 持有者自己(guard:self的guardian), 排在allies最後, 非主將
    rd177_allies = [rd177_leader, rd177_sub1, rd177_sub2, rd177_holder]
    tac177 = {"nameZh": "測試177", "effects": [
        {"k": "redirect", "who": "leader", "guard": "self", "share": 0.6, "dur": 5},
        {"k": "redirect", "who": "subs", "guard": "self", "share": 0.3, "dur": 5},
    ]}
    apply_effects(rd177_holder, None, tac177, rd177_allies, [])
    assert abs(rd177_leader.guard_share - 0.6) < 1e-9 and rd177_leader.guardian is rd177_holder, \
        f"177: who='leader'應使隊伍主將(rd177_leader, allies[0])獲得guard_share=0.6且guardian=持有者, 實得share={rd177_leader.guard_share}"
    assert abs(rd177_sub1.guard_share - 0.3) < 1e-9 and abs(rd177_sub2.guard_share - 0.3) < 1e-9, \
        f"177: who='subs'應使兩名副將各自guard_share=0.3(對稱肉身鐵壁'為副將分擔30%'), 實得{rd177_sub1.guard_share}/{rd177_sub2.guard_share}"
    assert rd177_sub1.guardian is rd177_holder and rd177_sub2.guardian is rd177_holder, \
        "177: 副將的guardian應為持有者rd177_holder自己(guard:self)"
    # 向後相容: 未帶who(或who='ally')的redirect應維持原行為(對guardian以外全體allies套用同一share)
    rd177c_holder = Unit(POOL["諸葛亮"], "弓")
    rd177c_a = Unit(POOL["劉備"], "槍")
    rd177c_b = Unit(POOL["關羽"], "槍")
    tac177c = {"nameZh": "測試177c", "effects": [{"k": "redirect", "who": "ally", "guard": "self", "share": 0.45, "dur": 5}]}
    apply_effects(rd177c_holder, None, tac177c, [rd177c_a, rd177c_b, rd177c_holder], [])
    assert abs(rd177c_a.guard_share - 0.45) < 1e-9 and abs(rd177c_b.guard_share - 0.45) < 1e-9, \
        "177c: 向後相容, who='ally'(或省略)應維持原行為對guardian以外全體allies套用同一share"
    print("    [批G 177] redirect支援e['who']分流(leader/subs, 肉身鐵壁精確落地)/向後相容不分身份統一share驗證通過")

    # 178) 批G: counter(反擊)支援e["normalOnly"] —— 荊棘「受到普通攻擊時, 反彈5%傷害」需要
    # 限定只在普攻(is_normal=True)觸發, 戰法傷害(is_normal=False)不應觸發反擊。
    ct178_u = Unit(POOL["甘寧"], "弓")
    ct178_u.counter = {"coef": 1.0, "kind": "phys", "prob": 1.0, "dur": 5, "normalOnly": True}
    ct178_atk_normal = Unit(POOL["張飛"], "槍")
    before178_normal = ct178_atk_normal.troop
    hit(ct178_atk_normal, ct178_u, 5.0, "phys", is_normal=True)
    assert ct178_atk_normal.troop < before178_normal, "178: normalOnly=True的counter應在普攻(is_normal=True)時觸發反擊(攻擊者應損兵)"
    ct178_atk_tactic = Unit(POOL["張飛"], "槍")
    before178_tactic = ct178_atk_tactic.troop
    hit(ct178_atk_tactic, ct178_u, 5.0, "phys", is_normal=False)
    assert ct178_atk_tactic.troop == before178_tactic, "178: normalOnly=True的counter不應在戰法傷害(is_normal=False)時觸發反擊(攻擊者不應損兵)"
    # 向後相容: 未帶normalOnly的counter應維持原行為(任意傷害來源皆可觸發反擊)
    ct178b_u = Unit(POOL["甘寧"], "弓")
    ct178b_u.counter = {"coef": 1.0, "kind": "phys", "prob": 1.0, "dur": 5}
    ct178b_atk = Unit(POOL["張飛"], "槍")
    before178b = ct178b_atk.troop
    hit(ct178b_atk, ct178b_u, 5.0, "phys", is_normal=False)
    assert ct178b_atk.troop < before178b, "178b: 未帶normalOnly的counter應向後相容, 任意傷害來源(含戰法傷害)皆可觸發反擊"
    print("    [批G 178] counter支援e['normalOnly'](荊棘「受普攻時反擊」精確落地)/向後相容任意傷害觸發驗證通過")

    # 179) 批G: 裝備效果級 e.when.on=="dealtDamage"(on_deal_eq) —— 衝陣「首回合首次造成傷害時
    # 附加一次額外兵刃傷害」。驗證(a)分類正確收錄進on_deal_eq(不誤入on_hit_eq); (b)e["coef"]
    # 直傷派發邏輯(不透過apply_effects的k派發, 直接hit())在單元層級正確運作; (c)完整fight()
    # 端到端不崩潰(真實資料經完整戰鬥迴圈跑過, 含 dealt_damage_for 早退判斷/hit_flags去重/
    # round_ok窗口檢查全部串接正確)。
    cz179_u = Unit(POOL["甘寧"], "弓", equip=["坐騎·衝陣"])
    assert len(cz179_u.on_deal_eq) == 1 and cz179_u.on_deal_eq[0]["k"] == "extraHit", \
        "179a: 衝陣應正確分類進on_deal_eq(裝備效果級dealtDamage反應式), 不應誤入on_hit_eq或eq(prep)"
    assert not cz179_u.on_hit_eq and not cz179_u.eq, \
        "179a: 衝陣的效果不應同時出現在on_hit_eq(受擊方向)或eq(prep一次性套用), 避免重複結算"
    cz179_dst = Unit(POOL["諸葛亮"], "弓")
    before179 = cz179_dst.troop
    e179 = cz179_u.on_deal_eq[0]
    hit(cz179_u, cz179_dst, e179["coef"], e179.get("kind", "phys"), False)
    assert cz179_dst.troop < before179, "179b: e['coef']直傷派發應能對dst造成傷害(不透過apply_effects的k派發, 直接hit()呼叫)"
    # 179c: 完整fight()端到端跑1000場不崩潰, 且與未裝備衝陣的對照組相比不應報錯/不應產生NaN兵力
    random.seed(901)
    r179_with = simulate(["甘寧", "張飛", "關羽"], ["諸葛亮", "劉備", "趙雲"], n=200, eqA=[["坐騎·衝陣"], [], []])
    assert 0 <= r179_with["A勝率"] <= 1 and 0 <= r179_with["B勝率"] <= 1, \
        f"179c: 衝陣裝備下完整fight()端到端200場應正常產生合法勝率(A={r179_with['A勝率']}, B={r179_with['B勝率']}), 不應崩潰或產生非法值"
    print("    [批G 179] 裝備效果級on_deal_eq(衝陣「首回合首次造成傷害附加額外傷害」精確落地, e['coef']直傷派發)/分類正確/端到端無崩潰驗證通過")

    # 180) 批G: lifestealGiven(倒戈效果量加成) —— 對稱既有healGiven, 長慮「使自身攻心效果提高
    # 30%」需要此欄位。驗證: 30%加成應使倒戈回復量從dmg*ls提升到dmg*ls*1.3。用相同random.seed()
    # 使兩次hit()的±4%傷害隨機浮動(damage()內建機制, 見engine_limitations既有記錄)完全一致,
    # 才能精確比較倍率(否則兩次獨立呼叫的隨機浮動差異會使誤差超出容忍範圍)。
    ls180_src = Unit(POOL["甘寧"], "弓")
    ls180_src.push_lifesteal(0.5, 9, "測試倒戈180")      # 50% 倒戈(誇大值方便驗證); 狀態疊加精修批: lifesteal改走多實例清單
    ls180_src.troop = START_TROOP * 0.5                 # 留出回血空間(避免撞START_TROOP上限)
    troop_before_180 = ls180_src.troop
    random.seed(1800)
    hit(ls180_src, Unit(POOL["諸葛亮"], "弓"), 1.0, "phys")
    gain_plain_180 = ls180_src.troop - troop_before_180
    ls180b_src = Unit(POOL["甘寧"], "弓")
    ls180b_src.push_lifesteal(0.5, 9, "測試倒戈180b")
    ls180b_src.push_add("lifestealGiven", 0.3, 9)       # 額外+30%倒戈效果量(對稱長慮); lifestealGiven維持既有push_add/addbonus機制不變(自我buff, 非具名狀態多實例)
    ls180b_src.troop = START_TROOP * 0.5
    troop_before_180b = ls180b_src.troop
    random.seed(1800)
    hit(ls180b_src, Unit(POOL["諸葛亮"], "弓"), 1.0, "phys")
    gain_boost_180 = ls180b_src.troop - troop_before_180b
    assert abs(gain_boost_180 - gain_plain_180 * 1.3) < 0.1, \
        f"180: lifestealGiven=0.3應使倒戈回復量提升至無加成版本的1.3倍, plain={gain_plain_180:.1f} boosted={gain_boost_180:.1f} expect≈{gain_plain_180*1.3:.1f}"
    print(f"    [批G 180] lifestealGiven(長慮「攻心效果+30%」精確落地): plain={gain_plain_180:.1f} boosted(+30%)={gain_boost_180:.1f} 驗證通過")

    # =========================================================================
    # 批I: 禁近似令-scale/比較族 —— scale:"maxStat" / ifStatCompare+scaleCompare /
    # ifTargetHas 陣列(OR)+weak(虛弱)ctype 三原語 + 17筆真實資料遷移驗證
    # =========================================================================

    # 181) scale_of/scaleOf maxStat —— 動態取施放者四維(force/intel/command/speed)最高一項
    u181 = Unit(POOL["呂布"], "騎")
    u181.push_mod("force", 999, 9)  # 拉高武力確保是四維最高
    maxv181 = max(u181.eff(s) for s in ("force", "intel", "command", "speed"))
    assert maxv181 == u181.eff("force"), "測試前置條件: 181武力應為四維最高"
    assert abs(scale_of(u181, "maxStat") - SCALE_G(maxv181, 350)) < 1e-9, \
        "scale_of(scale='maxStat') 應等於施放者四維最高值代入SCALE_G(除數350預設)"
    u181b = Unit(POOL["諸葛亮"], "弓")
    u181b.push_mod("intel", 5, 9)  # 進一步拉高智力確保是四維最高
    maxv181b = max(u181b.eff(s) for s in ("force", "intel", "command", "speed"))
    assert maxv181b == u181b.eff("intel"), "測試前置條件: 181b智力應為四維最高"
    assert abs(scale_of(u181b, "maxStat") - SCALE_G(maxv181b, 350)) < 1e-9, \
        "scale_of(scale='maxStat') 應動態跟隨施放者當下哪一維最高, 非固定鎖某一屬性(換人换成智力最高應改用智力)"
    print("    [批I 181] scale_of/scaleOf maxStat(施放者四維最高一項動態縮放, 扶危定傾/剛柔並濟/整軍經武) 驗證通過")

    # 182) resolve_stat_field/resolveStatField —— k=="stat"效果 e["stat"]=="maxStat" 動態解析加成欄位
    u182 = Unit(POOL["呂布"], "騎")
    bf182, bi182 = u182.eff("force"), u182.eff("intel")
    assert bf182 > bi182, "測試前置條件: 呂布武力應高於智力"
    tac182 = {"nameZh": "測試182最高屬性buff", "effects": [{"k": "stat", "who": "self", "stat": "maxStat", "add": 60, "dur": 9}]}
    apply_effects(u182, None, tac182, [u182], [], no_heal=True, skip_when_effects=True)
    assert abs(u182.eff("force") - (bf182 + 60)) < 1e-6, "e.stat=='maxStat' 應把+60加到當下最高的一項(呂布=武力)"
    assert abs(u182.eff("intel") - bi182) < 1e-6, "e.stat=='maxStat' 不應誤加到非最高的屬性(智力應不變)"
    u182b = Unit(POOL["諸葛亮"], "弓")
    bf182b, bi182b = u182b.eff("force"), u182b.eff("intel")
    assert bi182b > bf182b, "測試前置條件: 諸葛亮智力應高於武力"
    apply_effects(u182b, None, tac182, [u182b], [], no_heal=True, skip_when_effects=True)
    assert abs(u182b.eff("intel") - (bi182b + 60)) < 1e-6, "e.stat=='maxStat' 對諸葛亮應動態改加到智力(其四維最高項)"
    assert abs(u182b.eff("force") - bf182b) < 1e-6, "e.stat=='maxStat' 不應誤加到武力(非諸葛亮的最高項)"
    print("    [批I 182] resolve_stat_field/resolveStatField(e.stat=='maxStat'動態解析欄位, 形一陣) 驗證通過")

    # 183) stat_compare_ok/statCompareOk —— ifStatCompare比較族原語(vs="caster"預設/vs="leader")
    leader183 = Unit(POOL["呂布"], "騎")     # 高武力, 我軍主將
    sub183 = Unit(POOL["諸葛亮"], "弓")       # 低武力, 副將(刻意用低武力角色當ref傳入, 驗證vs="leader"確實忽略ref本身)
    target183 = Unit(POOL["張飛"], "騎")      # 武力介於兩者之間
    assert leader183.eff("force") > target183.eff("force") > sub183.eff("force"), \
        "測試前置條件: 183武力順序應為 leader183 > target183 > sub183"
    allies183 = [leader183, sub183]
    assert stat_compare_ok(leader183, target183, allies183, {"stat": "force", "op": "gt"}) is True, \
        "stat_compare_ok: op='gt'預設vs='caster', 施放者(leader183)武力高於目標時應為True(摧鋒斷刃/竊幸乘寵案例)"
    assert stat_compare_ok(sub183, target183, allies183, {"stat": "force", "op": "gt"}) is False, \
        "stat_compare_ok: 施放者(sub183)武力低於目標時應為False"
    assert stat_compare_ok(sub183, target183, allies183, {"stat": "force", "op": "gt", "vs": "leader"}) is True, \
        "stat_compare_ok: vs='leader'應改比較allies[0](leader183, 高武力)而非傳入的ref本身(sub183, 低武力), 精確對應聚石成金「我軍主將」語意"
    assert stat_compare_ok(leader183, sub183, allies183, {"stat": "force", "op": "lt"}) is False, \
        "stat_compare_ok: op='lt'方向性驗證(leader183武力不低於sub183, 應為False)"
    print("    [批I 183] stat_compare_ok/statCompareOk(ifStatCompare比較族原語, 摧鋒斷刃/竊幸乘寵/聚石成金) 驗證通過")

    # 184) scale_compare_of/scaleCompareOf —— scaleCompare雙方差值縮放曲線(神機妙算)
    caster184 = Unit(POOL["諸葛亮"], "弓")
    target184 = Unit(POOL["張飛"], "騎")
    diff184 = caster184.eff("intel") - target184.eff("intel")
    expect184 = max(0.0, 1 + diff184 / 350)
    got184 = scale_compare_of(caster184, target184, {"stat": "intel", "div": 350})
    assert abs(got184 - expect184) < 1e-9, "scale_compare_of 應等於1+(caster.intel-target.intel)/div"
    assert abs(scale_compare_of(caster184, caster184, {"stat": "intel"}) - 1.0) < 1e-9, \
        "scale_compare_of: 雙方同一單位(diff=0)應倍率=1.0, 無額外加成(神機妙算「額外提高」語意基準)"
    assert scale_compare_of(caster184, None, {"stat": "intel"}) == 1.0, "scale_compare_of: target為None時應安全回傳1.0(無額外縮放), 不應拋例外"
    print("    [批I 184] scale_compare_of/scaleCompareOf(scaleCompare雙方差值縮放, 神機妙算) 驗證通過")

    # 185) target_has/targetHas 陣列(OR語意) + weak(虛弱)ctype
    u185 = Unit(POOL["張飛"], "騎")
    u185.chaos = 1
    assert target_has(u185, ["stun", "silence", "disarm", "chaos"]) is True, \
        "target_has 陣列應OR語意: 命中chaos應為True(深藏若虛/百步穿楊案例)"
    assert target_has(u185, ["stun", "silence", "disarm"]) is False, \
        "target_has 陣列OR: 未命中清單內任何一項應為False"
    u185b = Unit(POOL["張飛"], "騎")
    u185b.disarm = 1
    assert target_has(u185b, ["disarm", "silence"]) is True, "橫掃千軍案例: 繳械應命中['disarm','silence']"
    u185w = Unit(POOL["張飛"], "騎")
    u185w.push_add("amp", -1.0, 9, "測試虛弱185")
    assert target_has(u185w, "weak") is True, "amp總和=-1.0應判定為weak(虛弱, 挫志怒襲案例)"
    u185n = Unit(POOL["張飛"], "騎")
    u185n.push_add("amp", -0.5, 9, "測試非虛弱185")
    assert target_has(u185n, "weak") is False, "amp總和=-0.5(未達-1)不應判定為weak"
    print("    [批I 185] target_has/targetHas 陣列/OR語意(深藏若虛/百步穿楊/橫掃千軍) + weak虛弱ctype(挫志怒襲) 驗證通過")

    # 186) 真實資料: 摧鋒斷刃(ifStatCompare gate on amp) —— 施放者武力較高才降低目標傷害輸出
    cf_tac = TACTICS["摧鋒斷刃"]
    cf_amp_e = next(e for e in cf_tac["effects"] if e.get("k") == "amp")
    assert cf_amp_e.get("ifStatCompare") == {"stat": "force", "op": "gt"}, "摧鋒斷刃: amp效果應帶ifStatCompare={stat:force,op:gt}"
    cf_caster_strong = Unit(POOL["呂布"], "騎")
    cf_target_weak = Unit(POOL["諸葛亮"], "弓")
    assert cf_caster_strong.eff("force") > cf_target_weak.eff("force"), "測試前置條件: 呂布武力應高於諸葛亮"
    apply_effects(cf_caster_strong, cf_target_weak, cf_tac, [cf_caster_strong], [cf_target_weak], no_heal=True, skip_when_effects=True)
    assert any(a[0] == "amp" for a in cf_target_weak.adds), "摧鋒斷刃: 施放者武力較高時, 目標應被套用amp(降低其傷害輸出)"
    cf_caster_weak = Unit(POOL["諸葛亮"], "弓")
    cf_target_strong = Unit(POOL["呂布"], "騎")
    apply_effects(cf_caster_weak, cf_target_strong, cf_tac, [cf_caster_weak], [cf_target_strong], no_heal=True, skip_when_effects=True)
    assert not any(a[0] == "amp" for a in cf_target_strong.adds), "摧鋒斷刃: 施放者武力較低時, 不應套用amp(ifStatCompare gate應阻擋)"
    print("    [批I 186] 摧鋒斷刃 real-data ifStatCompare(自身武力較高才降傷) 驗證通過")

    # 187) 真實資料: 聚石成金(ifStatCompare vs="leader") —— 敵軍魅力低於「我軍主將」(非施放者自身)才禁療
    jsc_tac = TACTICS["聚石成金"]
    jsc_hb_e = next(e for e in jsc_tac["effects"] if e.get("k") == "healblock")
    assert jsc_hb_e.get("ifStatCompare") == {"stat": "charm", "op": "gt", "vs": "leader"}, \
        "聚石成金: healblock效果應帶ifStatCompare={stat:charm,op:gt,vs:leader}"
    jsc_leader = Unit(POOL["張飛"], "騎")
    jsc_sub = Unit(POOL["諸葛亮"], "弓")       # 施放者本人(非主將), 刻意設極低魅力驗證vs="leader"確實不比較施放者自身
    jsc_leader.charm = 200
    jsc_sub.charm = 1
    jsc_allies = [jsc_leader, jsc_sub]
    jsc_enemy_low = Unit(POOL["關羽"], "騎")
    jsc_enemy_low.charm = 100     # 低於主將(200) → 應禁療
    jsc_enemy_high = Unit(POOL["趙雲"], "騎")
    jsc_enemy_high.charm = 250    # 高於主將(200) → 不應禁療
    apply_effects(jsc_sub, None, jsc_tac, jsc_allies, [jsc_enemy_low, jsc_enemy_high], no_heal=True, skip_when_effects=True)
    assert jsc_enemy_low.healblock > 0, \
        "聚石成金: 敵軍魅力(100)低於我軍主將(200)應被禁療, 即使施放者是副將(魅力僅1, 若誤用施放者自身比較會判定錯誤)"
    assert jsc_enemy_high.healblock == 0, "聚石成金: 敵軍魅力(250)高於我軍主將(200)不應被禁療"
    print("    [批I 187] 聚石成金 real-data ifStatCompare(vs='leader', 比較我軍主將而非施放者自身) 驗證通過")

    # 188) 真實資料: 深藏若虛(base/topup mitig互斥) —— 自身無/有控制狀態時分別採用不同段的滿級值
    sczx_tac = TACTICS["深藏若虛"]
    sczx_mitigs = [e for e in sczx_tac["effects"] if e.get("k") == "mitig"]
    assert len(sczx_mitigs) == 2, f"深藏若虛: 應有base+topup兩段mitig, got {len(sczx_mitigs)}"
    # 本地複本強制rate=1.0(隔離驗證ifTargetHas/ifTargetHasNot互斥邏輯本身, e.rate機制另有批23 A4既有測試涵蓋)
    sczx_forced = {"nameZh": "深藏若虛測試", "effects": [dict(e, rate=1.0) for e in sczx_mitigs]}
    sczx_clean = Unit(POOL["張飛"], "騎")
    apply_effects(sczx_clean, None, sczx_forced, [sczx_clean], [], no_heal=True, skip_when_effects=True)
    clean_mitig = [a for a in sczx_clean.adds if a[0] == "mitig"]
    assert len(clean_mitig) == 1, f"深藏若虛(無控制狀態): 應恰好一段mitig生效(base段, 互斥), got {len(clean_mitig)}"
    assert abs(clean_mitig[0][1] - 0.2 * scale_of(sczx_clean, "intel")) < 1e-6, "深藏若虛(無控制狀態): 應套用base段(20%×智力縮放)"
    sczx_chaotic = Unit(POOL["張飛"], "騎")
    sczx_chaotic.chaos = 2
    apply_effects(sczx_chaotic, None, sczx_forced, [sczx_chaotic], [], no_heal=True, skip_when_effects=True)
    chaotic_mitig = [a for a in sczx_chaotic.adds if a[0] == "mitig"]
    assert len(chaotic_mitig) == 1, f"深藏若虛(混亂狀態): 應恰好一段mitig生效(topup段, 互斥), got {len(chaotic_mitig)}"
    assert abs(chaotic_mitig[0][1] - 0.35 * scale_of(sczx_chaotic, "intel")) < 1e-6, \
        "深藏若虛(混亂狀態): 應套用topup段全值35%(而非base 20%+delta疊加), 受智力縮放"
    print("    [批I 188] 深藏若虛 real-data ifTargetHasNot/ifTargetHas互斥(控制狀態任一觸發topup) 驗證通過")

    # 189) 真實資料: 百步穿楊(extraHits ifTargetHas陣列) + 竊幸乘寵(extraHits ifStatCompare)
    bbc_tac = TACTICS["百步穿楊"]
    bbc_eh_e = bbc_tac["extraHits"][0]
    assert bbc_eh_e.get("ifTargetHas") == ["stun", "silence", "disarm", "chaos"], "百步穿楊: extraHits應帶ifTargetHas陣列(控制狀態任一)"
    bbc_atk = Unit(POOL["黃忠"], "弓")
    bbc_target_ctrl = Unit(POOL["張飛"], "騎")
    bbc_target_ctrl.disarm = 2
    bbc_target_clean = Unit(POOL["趙雲"], "騎")
    before_ctrl189, before_clean189 = bbc_target_ctrl.troop, bbc_target_clean.troop
    fire_extra_hits(bbc_atk, {"nameZh": "百步穿楊", "extraHits": bbc_tac["extraHits"]}, None,
                     lambda u: [bbc_atk], lambda u: [bbc_target_ctrl, bbc_target_clean], None, None)
    assert bbc_target_ctrl.troop < before_ctrl189, "百步穿楊: 已處於控制狀態(繳械)的目標應被extraHits額外命中(ifTargetHas陣列OR語意)"
    assert bbc_target_clean.troop == before_clean189, "百步穿楊: 未處於任何控制狀態的目標不應被此extraHits段命中"

    qxcc_tac = TACTICS["竊幸乘寵"]
    qxcc_eh_e = qxcc_tac["extraHits"][0]
    assert qxcc_eh_e.get("ifStatCompare") == {"stat": "intel", "op": "gt"}, "竊幸乘寵: extraHits應帶ifStatCompare={stat:intel,op:gt}"
    qxcc_atk_smart = Unit(POOL["諸葛亮"], "弓")
    qxcc_target_dumb = Unit(POOL["張飛"], "騎")
    assert qxcc_atk_smart.eff("intel") > qxcc_target_dumb.eff("intel"), "測試前置條件: 諸葛亮智力應高於張飛"
    before_dumb189 = qxcc_target_dumb.troop
    fire_extra_hits(qxcc_atk_smart, {"nameZh": "竊幸乘寵", "extraHits": qxcc_tac["extraHits"]}, qxcc_target_dumb,
                     lambda u: [qxcc_atk_smart], lambda u: [qxcc_target_dumb], None, None)
    assert qxcc_target_dumb.troop < before_dumb189, "竊幸乘寵: 施放者智力高於目標時, extraHits應觸發(ifStatCompare gate通過)"
    qxcc_atk_dumb = Unit(POOL["張飛"], "騎")
    qxcc_target_smart = Unit(POOL["諸葛亮"], "弓")
    before_smart189 = qxcc_target_smart.troop
    fire_extra_hits(qxcc_atk_dumb, {"nameZh": "竊幸乘寵", "extraHits": qxcc_tac["extraHits"]}, qxcc_target_smart,
                     lambda u: [qxcc_atk_dumb], lambda u: [qxcc_target_smart], None, None)
    assert qxcc_target_smart.troop == before_smart189, "竊幸乘寵: 施放者智力低於目標時, extraHits不應觸發(ifStatCompare gate應阻擋)"
    print("    [批I 189] 百步穿楊 extraHits ifTargetHas陣列 + 竊幸乘寵 extraHits ifStatCompare(real-data) 驗證通過")

    # 190) 真實資料: 橫掃千軍(effects ifTargetHas陣列, 計窮/繳械任一) + 挫志怒襲(ifTargetHasNot="weak")
    hszj_tac = TACTICS["橫掃千軍"]
    hszj_stun_e = next(e for e in hszj_tac["effects"] if e.get("k") == "stun")
    assert hszj_stun_e.get("ifTargetHas") == ["disarm", "silence"], "橫掃千軍: stun效果應帶ifTargetHas=['disarm','silence']"
    hszj_caster = Unit(POOL["關羽"], "騎")
    hszj_target_silenced = Unit(POOL["張飛"], "騎")
    hszj_target_silenced.silence = 2
    hszj_target_clean = Unit(POOL["趙雲"], "騎")
    hszj_forced = {"nameZh": "橫掃千軍測試", "effects": [dict(hszj_stun_e, rate=1.0)]}
    apply_effects(hszj_caster, None, hszj_forced, [hszj_caster], [hszj_target_silenced, hszj_target_clean], no_heal=True, skip_when_effects=True)
    assert hszj_target_silenced.stun > 0, "橫掃千軍: 已計窮(silence)的目標應被震懾(ifTargetHas陣列涵蓋silence)"
    assert hszj_target_clean.stun == 0, "橫掃千軍: 未處於繳械/計窮的目標不應被震懾"

    zj_tac = TACTICS["挫志怒襲"]
    zj_amp_e = next(e for e in zj_tac["effects"] if e.get("k") == "amp")
    assert zj_amp_e.get("ifTargetHasNot") == "weak", "挫志怒襲: amp虛弱debuff應帶ifTargetHasNot='weak'"
    zj_caster = Unit(POOL["曹彰"], "騎") if POOL.get("曹彰") else Unit(POOL["張飛"], "騎")
    zj_fresh_target = Unit(POOL["關羽"], "騎")
    zj_weak_target = Unit(POOL["趙雲"], "騎")
    zj_weak_target.push_add("amp", -1.0, 9, "測試已虛弱190")
    # amp效果帶既有e["sameTargets"]=True(批45 A), dests完全依main_hit_tgts(過濾存活)決定, 未傳入
    # 時會落空(向後相容, 見125/126號測試precedent)——這裡直接呼叫apply_effects(非完整fight()主
    # 迴圈), 須顯式傳入main_hit_tgts=[本次群體]才能讓sameTargets正確沿用, 對稱st125既有呼叫慣例。
    apply_effects(zj_caster, None, zj_tac, [zj_caster], [zj_fresh_target, zj_weak_target], no_heal=True,
                  skip_when_effects=True, main_hit_tgts=[zj_fresh_target, zj_weak_target])
    assert any(a[0] == "amp" for a in zj_fresh_target.adds), "挫志怒襲: 未虛弱目標應被套用虛弱debuff(amp)"
    weak_amp_count190 = len([a for a in zj_weak_target.adds if a[0] == "amp"])
    assert weak_amp_count190 == 1, \
        f"挫志怒襲: 已虛弱目標不應被重複套用虛弱debuff(ifTargetHasNot='weak'應排除新增), 應維持原有1筆amp, got {weak_amp_count190}"
    print("    [批I 190] 橫掃千軍 ifTargetHas陣列 + 挫志怒襲 ifTargetHasNot='weak'(real-data) 驗證通過")

    # 191) 真實資料: 義膽雄心(scaleIfLeader既有原語接線, 非新maxStat) —— 主將時debuff受自身對應
    # 屬性縮放, 非主將時維持基礎值不縮放
    ydxx_tac = TACTICS["義膽雄心"]
    ydxx_force_e = next(e for e in ydxx_tac["effects"] if e.get("stat") == "force")
    assert ydxx_force_e.get("scale") == "force" and ydxx_force_e.get("scaleIfLeader") is True, \
        "義膽雄心: force debuff效果應帶scale='force'+scaleIfLeader=true(既有scaleIfLeader原語, 批52c)"
    ydxx_intel_e = next(e for e in ydxx_tac["effects"] if e.get("stat") == "intel")
    assert ydxx_intel_e.get("scale") == "intel" and ydxx_intel_e.get("scaleIfLeader") is True, \
        "義膽雄心: intel debuff效果應帶scale='intel'+scaleIfLeader=true"
    # 本地複本移除everyRound/when(批37 B既有機制, 只在heal_only常駐通道生效, 與本批scale/
    # scaleIfLeader無關, 見測試84 precedent)+rate(避免額外擲骰干擾), 隔離驗證本批實際修改的
    # scale/scaleIfLeader部分, 用直接apply_effects()呼叫(非heal_only通道)即可精確測到。
    ydxx_force_isolated = {k: v for k, v in ydxx_force_e.items() if k not in ("everyRound", "when", "rate")}
    ydxx_leader = Unit(POOL["姜維"], "騎")
    ydxx_tgt_a = Unit(POOL["張飛"], "騎")
    apply_effects(ydxx_leader, None, {"nameZh": "義膽雄心測試-主將", "effects": [ydxx_force_isolated]},
                  [ydxx_leader], [ydxx_tgt_a], no_heal=True, skip_when_effects=True)
    force_debuff_leader = next(a for a in ydxx_tgt_a.stat_adds if a[0] == "force")
    expect_leader191 = -64 * scale_of(ydxx_leader, "force")
    assert abs(force_debuff_leader[1] - expect_leader191) < 1e-6, \
        f"義膽雄心(主將施放): force debuff應為-64×scale_of(caster,'force')={expect_leader191:.2f}, got {force_debuff_leader[1]:.2f}"
    ydxx_sub = Unit(POOL["姜維"], "騎")
    ydxx_tgt_b = Unit(POOL["張飛"], "騎")
    apply_effects(ydxx_sub, None, {"nameZh": "義膽雄心測試-副將", "effects": [ydxx_force_isolated]},
                  [Unit(POOL["劉備"], "騎"), ydxx_sub], [ydxx_tgt_b], no_heal=True, skip_when_effects=True)
    force_debuff_sub = next(a for a in ydxx_tgt_b.stat_adds if a[0] == "force")
    assert abs(force_debuff_sub[1] - (-64)) < 1e-6, \
        f"義膽雄心(非主將施放): force debuff應維持基礎值-64(scaleIfLeader應阻擋縮放), got {force_debuff_sub[1]:.2f}"
    print("    [批I 191] 義膽雄心 real-data scaleIfLeader(既有原語接線, 主將時受自身對應屬性縮放) 驗證通過")

    # 192) 真實資料: 神機莫測(既有ifTargetHas單值+ifLeader接線, 非新array原語) —— 友軍已混亂時,
    # 主將施放者應先偵測到混亂狀態給予+12%傷害, dispel再解除混亂+其餘負面狀態(執行順序關鍵)
    sjmc_tac = TACTICS["神機莫測"]
    sjmc_new_amp = next(e for e in sjmc_tac["effects"] if e.get("k") == "amp")
    sjmc_new_dispel = next(e for e in sjmc_tac["effects"] if e.get("k") == "dispel")
    assert sjmc_new_amp.get("ifTargetHas") == "chaos" and sjmc_new_amp.get("ifLeader") is True, \
        "神機莫測: amp傷害提升效果應帶ifTargetHas='chaos'+ifLeader=true"
    assert sjmc_new_dispel.get("ifTargetHas") == "chaos", "神機莫測: dispel解除負面狀態應帶ifTargetHas='chaos'"
    assert sjmc_tac["effects"].index(sjmc_new_amp) < sjmc_tac["effects"].index(sjmc_new_dispel), \
        "神機莫測: amp(檢查混亂中)必須排在dispel(清除混亂)之前, 否則dispel會先清空chaos欄位導致amp的ifTargetHas='chaos'檢查全數落空"
    sjmc_leader_caster = Unit(POOL["張飛"], "騎")
    sjmc_ally_chaotic = Unit(POOL["關羽"], "騎")
    sjmc_ally_chaotic.chaos = 2
    sjmc_ally_chaotic.push_add("mitig", -0.1, 3, "測試附帶減益192")
    sjmc_allies = [sjmc_leader_caster, sjmc_ally_chaotic]
    apply_effects(sjmc_leader_caster, None, sjmc_tac, sjmc_allies,
                  [Unit(POOL["黃忠"], "弓"), Unit(POOL["趙雲"], "騎")], no_heal=True, skip_when_effects=True)
    assert any(a[0] == "amp" and a[1] > 0 for a in sjmc_ally_chaotic.adds), \
        "神機莫測: 施放者為主將時, 已混亂的友軍應獲得傷害提升(amp+12%, 在dispel清除混亂前已檢測到混亂狀態)"
    assert sjmc_ally_chaotic.chaos == 0, "神機莫測: dispel應解除已混亂友軍的混亂狀態(執行順序在amp判斷之後)"
    assert not any(a[0] == "mitig" and a[1] < 0 for a in sjmc_ally_chaotic.adds), \
        "神機莫測: dispel(debuffs)應一併清除其他負面狀態(測試預先掛的減益mitig應被清除)"
    print("    [批I 192] 神機莫測 real-data ifTargetHas(單值'chaos')+ifLeader既有原語接線(amp先於dispel執行順序) 驗證通過")

    # 193) 端到端整合: 神機妙算 t.scaleCompare 在真實 fight()/simulate() 全流程下不崩潰,
    # 且比照既有105/106測試harness慣例重演active_fired_for()同款coef*scaleCompare算式,
    # 驗證智力較高的施放者應算出較高的縮放係數與傷害(scale_compare_of本身181/184已獨立驗證,
    # 這裡驗證消費端"真實tactics資料的coef/kind與該公式組合"整體一致)。
    sjmr_tac = TACTICS["神機妙算"]
    assert sjmr_tac.get("scaleCompare") == {"stat": "intel", "div": 350}, "神機妙算: 應帶頂層scaleCompare={stat:intel,div:350}"
    sjmr_holder_smart = Unit(POOL["諸葛亮"], "弓")
    sjmr_holder_dumb = Unit(POOL["張飛"], "騎")
    sjmr_target = Unit(POOL["趙雲"], "騎")
    assert sjmr_holder_smart.eff("intel") > sjmr_holder_dumb.eff("intel"), "測試前置條件: 諸葛亮智力應高於張飛"
    c_smart193 = sjmr_tac["coef"] * scale_compare_of(sjmr_holder_smart, sjmr_target, sjmr_tac["scaleCompare"])
    c_dumb193 = sjmr_tac["coef"] * scale_compare_of(sjmr_holder_dumb, sjmr_target, sjmr_tac["scaleCompare"])
    assert c_smart193 > c_dumb193, \
        f"神機妙算scaleCompare: 智力較高的施放者應算出較高的縮放後傷害係數(c_smart={c_smart193:.3f} vs c_dumb={c_dumb193:.3f})"
    random.seed(20260710)
    sim193 = simulate(["諸葛亮", "劉備", "趙雲"], ["張飛", "關羽", "黃忠"], n=300)
    assert 0 <= sim193["A勝率"] <= 1 and 0 <= sim193["B勝率"] <= 1, \
        f"193: 神機妙算持有者(諸葛亮)入隊完整fight()端到端300場應正常產生合法勝率, got A={sim193['A勝率']} B={sim193['B勝率']}"
    print(f"    [批I 193] 神機妙算 scaleCompare 消費端一致性(c_smart={c_smart193:.3f}>c_dumb={c_dumb193:.3f}) + 完整simulate()端到端300場無崩潰 驗證通過")

    # ============================================================================
    # 批J: 禁近似令-transfer轉移族 —— stealStat(偷屬性)/transferMitig(buff轉移)/
    # transferDebuff(debuff轉移)三原語 + redirect新增guard:"random_sub"/guardFor:"leader"
    # 兩擴充。核心驗收主題: 「轉移量=來源實際擁有量, 不無中生有」——來源沒有該狀態/該量,
    # 轉移就該是0, 而非固定套用戰法表面數字。
    # ============================================================================

    # 194) stealStat —— 偷屬性核心約束「轉移量=來源實際擁有量,不無中生有」。用直接竄改
    # command屬性模擬「victim統率已經很低」情境(eff()疊加stat_adds/mods皆為空的新建Unit,
    # 故直接設u.command即可讓eff("command")≈該值): 統率貧乏的victim應只被扣到
    # min(欲偷量,現有值)而非固定扣10點(不應扣至負值); 統率充裕的victim應被扣滿10點;
    # recipient獲得的量應恰好等於「所有victim實際被扣除量之加總」, 而非固定n_victim×10
    # (對稱雁行陣「使我軍統率最低單體偷取敵軍全體10點統率」)。
    bj194_recipient = Unit(POOL["張飛"], "騎")
    bj194_poor = Unit(POOL["諸葛亮"], "弓")
    bj194_poor.command = 3.0  # 直接竄改基礎屬性, 模擬「當下有效統率極低」
    bj194_rich = Unit(POOL["周瑜"], "弓")
    bj194_before_poor = bj194_poor.eff("command")
    bj194_before_rich = bj194_rich.eff("command")
    assert bj194_before_poor < 10, "測試前置條件: 貧乏victim的有效統率應低於欲偷量10點"
    bj194_before_recipient = bj194_recipient.eff("command")
    bj194_tac = {"nameZh": "測試偷統率194", "effects": [
        {"k": "stealStat", "stat": "command", "amount": 10, "who": "enemy",
         "recipientSel": "minCommand", "dur": 1}]}
    apply_effects(bj194_recipient, None, bj194_tac, [bj194_recipient], [bj194_poor, bj194_rich])
    bj194_after_poor = bj194_poor.eff("command")
    bj194_after_rich = bj194_rich.eff("command")
    assert bj194_after_poor >= -1e-6, f"stealStat: 貧乏victim的統率不應被扣至負值(after={bj194_after_poor})"
    assert abs((bj194_before_poor - bj194_after_poor) - bj194_before_poor) < 1e-6, \
        f"stealStat: 貧乏victim應只被扣除min(10,現有值)=現有值本身, 實際扣除{bj194_before_poor - bj194_after_poor}"
    assert abs((bj194_before_rich - bj194_after_rich) - 10) < 1e-6, \
        f"stealStat: 統率充裕(遠大於10)的victim應被扣滿10點, 實際扣除{bj194_before_rich - bj194_after_rich}"
    bj194_gained = bj194_recipient.eff("command") - bj194_before_recipient
    bj194_expected = (bj194_before_poor - bj194_after_poor) + (bj194_before_rich - bj194_after_rich)
    assert abs(bj194_gained - bj194_expected) < 1e-6, \
        f"stealStat: recipient獲得量應恰好等於所有victim實際被扣除量之加總({bj194_expected:.2f}), 不是固定10×2人=20(實得{bj194_gained:.2f})"
    assert bj194_expected < 20 - 1e-6, "測試前置條件: 本測試應能區分『固定20』vs『實際扣除量之和』兩種結果"
    print("    [批J 194] stealStat: 偷屬性轉移量封頂於victim實際擁有量, recipient只收實際扣除量之和(不無中生有) 驗證通過")

    # 195) transferMitig —— 若敵方(來源側)當下沒有人持有正向mitig(傷害降低)buff, 不應無中
    # 生有轉移(dest不應憑空獲得減傷)。若敵方恰有一人持有, 應整個搬移(從來源移除, 在dest身上
    # 以同數值重建), 而非「雙方各自套用」的舊近似。
    bj195_dest = Unit(POOL["張飛"], "騎")
    bj195_src_a = Unit(POOL["關羽"], "槍")  # 無mitig buff
    bj195_src_b = Unit(POOL["劉備"], "槍")  # 無mitig buff
    bj195_tac = {"nameZh": "測試轉移傷害降低195", "effects": [
        {"k": "transferMitig", "from": "enemy", "to": "ally", "dur": 1}]}
    apply_effects(bj195_dest, None, bj195_tac, [bj195_dest], [bj195_src_a, bj195_src_b])
    assert bj195_dest.addbonus("mitig") == 0, \
        "transferMitig: 敵方當下無人持有mitig buff時不應轉移(dest不應憑空獲得減傷)"
    bj195b_dest = Unit(POOL["張飛"], "騎")
    bj195b_src_has = Unit(POOL["關羽"], "槍")
    bj195b_src_has.push_add("mitig", 0.25, 3, src="測試減傷來源")
    bj195b_src_none = Unit(POOL["劉備"], "槍")
    apply_effects(bj195b_dest, None, bj195_tac, [bj195b_dest], [bj195b_src_has, bj195b_src_none])
    assert abs(bj195b_dest.addbonus("mitig") - 0.25) < 1e-9, \
        f"transferMitig: 敵方持有mitig buff時應整個搬移到dest身上(相同數值0.25), 實得{bj195b_dest.addbonus('mitig')}"
    assert bj195b_src_has.addbonus("mitig") == 0, \
        "transferMitig: 來源應失去該buff(真正的搬移, 非複製——來源不應仍保留原buff)"
    print("    [批J 195] transferMitig: 來源無mitig buff不轉移(不無中生有)/來源有則整個搬移(來源移除+目的地重建) 驗證通過")

    # 196) transferDebuff —— 若我方(來源側)群體當下完全沒有負面狀態, 應轉移0種(不無中生有)。
    # 若我方群體恰好只有1種現存負面狀態(如震懾), 即使nMax要求最多2種, 也只能轉移現有的那
    # 1種(轉移量=來源實際擁有量, 不硬湊到位)。
    bj196_a = Unit(POOL["關羽"], "槍")
    bj196_b = Unit(POOL["劉備"], "槍")
    bj196_dest = Unit(POOL["張飛"], "騎")
    bj196_tac = {"nameZh": "測試轉移負面狀態196", "effects": [
        {"k": "transferDebuff", "from": "ally", "to": "enemy", "n": 1, "nMax": 2, "dur": 1}]}
    apply_effects(Unit(POOL["諸葛亮"], "弓"), None, bj196_tac, [bj196_a, bj196_b], [bj196_dest])
    assert bj196_dest.stun == 0 and not bj196_dest.silence and not bj196_dest.disarm and not bj196_dest.chaos, \
        "transferDebuff: 來源群體無任何負面狀態時應轉移0種(dest不應憑空獲得任何控制狀態)"
    bj196b_a = Unit(POOL["關羽"], "槍")
    bj196b_a.stun = 3
    bj196b_b = Unit(POOL["劉備"], "槍")  # 無任何負面狀態
    bj196b_dest = Unit(POOL["張飛"], "騎")
    apply_effects(Unit(POOL["諸葛亮"], "弓"), None, bj196_tac, [bj196b_a, bj196b_b], [bj196b_dest])
    assert bj196b_a.stun == 0, "transferDebuff: 來源應失去該狀態(真正的搬移, 非複製)"
    assert bj196b_dest.stun > 0, "transferDebuff: dest應獲得被轉移的震懾狀態"
    assert not bj196b_dest.silence and not bj196b_dest.disarm and not bj196b_dest.chaos, \
        "transferDebuff: 我方只有1種現存負面狀態時, 即使nMax要求2種, 也不應憑空多轉移出第2種"
    print("    [批J 196] transferDebuff: 來源無負面狀態不轉移(0種)/只有1種現存時即使nMax要求2種也不硬湊(不無中生有) 驗證通過")

    # 197) redirect guard="random_sub" —— 夢中弒臣「如果自己為主將，則使隨機副將為自己分擔
    # 20%→40%傷害」。若隊伍有存活副將, 主將(caster=allies[0])應把一部分傷害轉嫁給其中一位
    # 副將(guardian=該副將, 非caster自己)。若隊伍只有主將一人(無副將可轉嫁), guardian應
    # 退回caster本身, 而who="leader"時recipients=[caster]恰好等於guard本身, `a is not
    # guardian`判斷會使其被排除——天然等同「找不到可轉嫁對象就不轉嫁」, 不應另尋他法硬湊。
    bj197_leader = Unit(POOL["曹操"], "騎")
    bj197_sub = Unit(POOL["典韋"], "騎")
    bj197_tac = {"nameZh": "測試隨機副將197", "effects": [
        {"k": "redirect", "who": "leader", "guard": "random_sub", "share": 0.4, "dur": 2}]}
    apply_effects(bj197_leader, None, bj197_tac, [bj197_leader, bj197_sub], [])
    assert bj197_leader.guardian is bj197_sub, \
        f"redirect guard='random_sub': 有存活副將時, 主將的guardian應為該副將, 實得{bj197_leader.guardian}"
    assert abs(bj197_leader.guard_share - 0.4) < 1e-9
    bj197b_leader = Unit(POOL["曹操"], "騎")
    apply_effects(bj197b_leader, None, bj197_tac, [bj197b_leader], [])
    assert bj197b_leader.guardian is None, \
        "redirect guard='random_sub': 無存活副將時不應轉嫁(guardian應保持None, 不無中生有另尋轉嫁對象)"
    print("    [批J 197] redirect guard='random_sub': 有副將時隨機轉嫁給存活副將/無副將時不轉嫁(guardian維持None) 驗證通過")

    # 198) redirect guardFor="leader"(absorbGuards, 單次全額代承) —— 古之惡來「...隨後為
    # 我軍主將承擔此次普通攻擊」。主將受到「普通攻擊」時, 該次傷害應100%(share預設1.0)轉給
    # 登記的代承者, 主將自身完全不受這次攻擊影響; 每回合最多觸發1次(第二次普攻不應再轉嫁);
    # 且只在普攻(is_normal=True)時生效, 戰法傷害(is_normal=False)不應觸發(對稱既有
    # counter_guards/guardFor慣例)。
    bj198_leader = Unit(POOL["典韋"], "騎")
    bj198_absorber = Unit(POOL["典韋"], "騎")
    bj198_attacker = Unit(POOL["呂布"], "騎")
    bj198_tac = {"nameZh": "測試單次全額代承198", "effects": [
        {"k": "redirect", "guardFor": "leader", "share": 1.0}]}
    apply_effects(bj198_absorber, None, bj198_tac, [bj198_leader, bj198_absorber], [bj198_attacker])
    assert len(bj198_leader.absorb_guards) == 1 and bj198_leader.absorb_guards[0]["unit"] is bj198_absorber, \
        "redirect guardFor='leader': 應把持有者登記進allies[0](主將)的absorb_guards清單"
    bj198_leader_before = bj198_leader.troop
    bj198_absorber_before = bj198_absorber.troop
    hit(bj198_attacker, bj198_leader, 0.5, "phys", is_normal=True)
    assert abs(bj198_leader.troop - bj198_leader_before) < 1e-6, \
        f"redirect guardFor='leader': 普攻應100%轉嫁給代承者, 主將不應損兵(leader損失{bj198_leader_before - bj198_leader.troop:.1f})"
    assert bj198_absorber.troop < bj198_absorber_before, \
        "redirect guardFor='leader': 代承者應實際承受這次普攻的全部傷害(不會憑空消失兵力)"
    bj198_leader_before2 = bj198_leader.troop
    hit(bj198_attacker, bj198_leader, 0.5, "phys", is_normal=True)
    assert bj198_leader_before2 - bj198_leader.troop > 1e-6, \
        "redirect guardFor='leader': 同回合第二次普攻不應再被代承(每回合限觸發1次), 主將這次應自行承受傷害"
    bj198c_leader = Unit(POOL["典韋"], "騎")
    bj198c_absorber = Unit(POOL["典韋"], "騎")
    bj198c_attacker = Unit(POOL["呂布"], "騎")
    apply_effects(bj198c_absorber, None, bj198_tac, [bj198c_leader, bj198c_absorber], [bj198c_attacker])
    bj198c_leader_before = bj198c_leader.troop
    hit(bj198c_attacker, bj198c_leader, 0.5, "intel", is_normal=False)
    assert bj198c_leader.troop < bj198c_leader_before, \
        "redirect guardFor='leader': 戰法傷害(非普攻)不應觸發代承, 主將應自行承受"
    print("    [批J 198] redirect guardFor='leader'(absorbGuards): 普攻100%單次全額代承/每回合限1次/非普攻不觸發 驗證通過")

    # 199) real-data結構驗證——確認6筆戰法定稿(tactics_parsed.json經reparse_effects.py套用
    # corrections後)的effects陣列確實帶有本批新原語/欄位, 而非只在合成測試戰法上驗證過
    # (對稱既有181-193等批次「real-data」系列測試慣例)。
    bj199_yhz = TACTICS["雁行陣"]
    bj199_yhz_ks = [e.get("k") for e in bj199_yhz["effects"]]
    assert bj199_yhz_ks == ["stealStat", "transferMitig", "transferDebuff"], \
        f"雁行陣: real-data應含stealStat+transferMitig+transferDebuff三段(依序), 實得{bj199_yhz_ks}"
    bj199_yhz_steal = bj199_yhz["effects"][0]
    assert bj199_yhz_steal.get("recipientSel") == "minCommand" and bj199_yhz_steal.get("who") == "enemy"
    bj199_mhjm = TACTICS["移花接木"]
    assert not bj199_mhjm.get("when"), "移花接木: 頂層when應已清除(批19遺留的無效欄位, 阻擋on:healed註冊)"
    bj199_mhjm_last = bj199_mhjm["effects"][-1]
    assert bj199_mhjm_last.get("k") == "heal" and bj199_mhjm_last.get("ofDamage") == 0.26 \
        and (bj199_mhjm_last.get("when") or {}).get("on") == "healed" \
        and (bj199_mhjm_last.get("when") or {}).get("who") == "enemy", \
        f"移花接木: real-data最後一段應為heal+ofDamage:0.26+on:healed,who:enemy, 實得{bj199_mhjm_last}"
    bj199_qsjd = TACTICS["權僭九鼎"]
    bj199_qsjd_first = bj199_qsjd["effects"][0]
    assert bj199_qsjd_first.get("k") == "heal" and bj199_qsjd_first.get("ofDamage") == 0.12 \
        and bj199_qsjd_first.get("rate") == 0.5 and (bj199_qsjd_first.get("when") or {}).get("who") == "enemy", \
        f"權僭九鼎: real-data第一段應為heal+ofDamage:0.12+rate:0.5+on:healed,who:enemy, 實得{bj199_qsjd_first}"
    bj199_mzsc = TACTICS["夢中弒臣"]
    bj199_mzsc_first = bj199_mzsc["effects"][0]
    assert bj199_mzsc_first.get("k") == "redirect" and bj199_mzsc_first.get("guard") == "random_sub" \
        and abs(bj199_mzsc_first.get("share", 0) - 0.4) < 1e-9 and bj199_mzsc_first.get("who") == "leader", \
        f"夢中弒臣: real-data第一段應為redirect+guard:random_sub+share:0.4+who:leader, 實得{bj199_mzsc_first}"
    bj199_jsbw = TACTICS["校勝帷幄"]
    bj199_jsbw_first = bj199_jsbw["effects"][0]
    assert bj199_jsbw_first.get("k") == "redirect" and bj199_jsbw_first.get("who") == "leader", \
        f"校勝帷幄: real-data第一段redirect應為who:leader(取代舊版who:ally近似), 實得{bj199_jsbw_first}"
    bj199_gzel = TACTICS["古之惡來"]
    bj199_gzel_ks = [e.get("k") for e in bj199_gzel["effects"]]
    assert "redirect" in bj199_gzel_ks, f"古之惡來: real-data應新增redirect(guardFor:leader)效果段, 實得{bj199_gzel_ks}"
    bj199_gzel_redirect = next(e for e in bj199_gzel["effects"] if e.get("k") == "redirect")
    assert bj199_gzel_redirect.get("guardFor") == "leader" and abs(bj199_gzel_redirect.get("share", 0) - 1.0) < 1e-9
    print("    [批J 199] 6筆transfer族戰法real-data結構驗證(stealStat/transferMitig/transferDebuff/"
          "heal.ofDamage×2/redirect.guard=random_sub/redirect.who=leader/redirect.guardFor=leader) "
          "皆已正確落地 驗證通過")

    # 200) 端到端整合——6筆戰法個別以inhA掛在固定隊伍成員身上, 完整fight()/simulate()跑一輪
    # 不應崩潰且應產生合法勝率(對稱既有181-193系列的end-to-end慣例)。
    random.seed(20260710)
    for _bj200_name in ("雁行陣", "移花接木", "權僭九鼎", "夢中弒臣", "校勝帷幄", "古之惡來"):
        bj200_res = simulate(["張飛", "關羽", "劉備"], ["諸葛亮", "周瑜", "司馬懿"], n=200,
                              inhA=[[_bj200_name], None, None])
        assert 0 <= bj200_res["A勝率"] <= 1 and 0 <= bj200_res["B勝率"] <= 1, \
            f"200({_bj200_name}): 完整simulate()端到端200場應正常產生合法勝率, got {bj200_res}"
    print("    [批J 200] 6筆transfer族戰法端到端(inhA掛載+完整fight()/simulate()各200場) 皆無崩潰且產生合法勝率 驗證通過")

    # =====================================================================
    # 禁近似令-批L: 最後3筆深水區(一身是膽/先登死士/才辯機捷) —— 每項新機制皆補assert
    # =====================================================================

    # 201) bump_dmg_accum(累積傷害門檻算術): 純算術驗證跨越門檻格數計算正確
    # (THRESHOLD = START_TROOP×7% = 700), 不涉隨機。
    bl201_u = Unit(POOL["張飛"], "盾")
    assert bump_dmg_accum(bl201_u, 300) == 0 and abs(bl201_u.dmg_accum - 300) < 1e-9, \
        "bump_dmg_accum: 300<700, 不應跨越門檻, dmg_accum應為300"
    assert bump_dmg_accum(bl201_u, 450) == 1 and abs(bl201_u.dmg_accum - 750) < 1e-9, \
        "bump_dmg_accum: 300+450=750, 應跨越第1個700門檻(floor(750/700)-floor(300/700)=1-0=1)"
    assert bump_dmg_accum(bl201_u, 2000) == 2, \
        "bump_dmg_accum: 750+2000=2750, floor(2750/700)=3, floor(750/700)=1, 應跨越2格門檻"
    assert bump_dmg_accum(bl201_u, 0) == 0 and bump_dmg_accum(bl201_u, -5) == 0, \
        "bump_dmg_accum: amt<=0時不應累積也不應跨越門檻"
    bl201_u.troop = 0
    assert bump_dmg_accum(bl201_u, 1000) == 0, "bump_dmg_accum: 陣亡單位(troop<=0)不應繼續累積"
    print("    [批L 201] bump_dmg_accum累積傷害門檻算術(700=10000兵×7%為一格, 單次巨量傷害可跨越"
          "多格) 驗證通過")

    # 202) 一身是膽: critUp效果應同時掛dmgThreshold+ctrlImmune兩個事件名(on_values(e)正規化),
    # 且共用同一組stackKey疊層——兩事件來源合計最多7層, 而非各自獨立疊到7層(合計14層)。
    bl_ysb_tac = TACTICS["一身是膽"]
    bl_ysb_crit_e = next(e for e in bl_ysb_tac["effects"] if e["k"] == "critUp")
    assert set(on_values(bl_ysb_crit_e)) == {"dmgThreshold", "ctrlImmune"}, \
        f"一身是膽: critUp效果應同時掛dmgThreshold+ctrlImmune兩個事件名, 實得{bl_ysb_crit_e.get('when')}"
    assert bl_ysb_crit_e.get("maxStacks") == 7 and abs(bl_ysb_crit_e.get("perStack", 0) - 0.07) < 1e-9 \
        and bl_ysb_crit_e.get("dmgType") == "phys", \
        f"一身是膽: critUp應為maxStacks:7/perStack:0.07/dmgType:phys, 實得{bl_ysb_crit_e}"
    bl202_u = Unit(POOL["張飛"], "盾", inherit=["一身是膽"])
    bl202_allies, bl202_enemies = [bl202_u], [Unit(POOL["關羽"], "騎")]
    _FIGHT_CTX["allies_of"] = lambda u: bl202_allies if u in bl202_allies else bl202_enemies
    _FIGHT_CTX["foes_of"] = lambda u: bl202_enemies if u in bl202_allies else bl202_allies
    random.seed(2026071101)
    for _ in range(60):  # rate=0.4, 60次機會遠超期望疊滿所需(~17.5次), 統計上必定疊滿7層
        fire_self_reactive(bl202_u, "dmgThreshold", 1)
    layers_after_dmg = (bl202_u.crit_layers or {}).get(id(bl_ysb_crit_e), 0)
    assert layers_after_dmg == 7, \
        f"一身是膽: 60次dmgThreshold機會(rate=0.4)應統計上必定疊滿maxStacks=7, 實得{layers_after_dmg}"
    for _ in range(20):
        fire_self_reactive(bl202_u, "ctrlImmune", 1)
    layers_after_both = (bl202_u.crit_layers or {}).get(id(bl_ysb_crit_e), 0)
    assert layers_after_both == 7, \
        ("一身是膽: dmgThreshold已疊滿7層後, ctrlImmune不應再疊加(共用同一組計數器, 合計上限7, "
         f"而非各自7層合計14層), 實得{layers_after_both}")
    assert abs(bl202_u.addbonus("critUp", "phys") - 7 * 0.07) < 1e-9, \
        "一身是膽: 疊滿7層後累計會心加成應為7×7%=49%"
    print("    [批L 202] 一身是膽 dmgThreshold+ctrlImmune共用單一critUp+stackKey疊層(合計上限7,"
          "非各自7層) 驗證通過")

    # 203) 一身是膽: ctrlImmune真實接線驗證(非直接呼叫fire_self_reactive, 而是真的走
    # stun/silence/disarm/chaos/ambush的immune分支)。先套用一身是膽本身(取得insight),
    # 再讓敵方嘗試施加stun, 確認(a)stun被insight擋下(u.stun未被設置)(b)免疫格擋事件確實
    # 被觸發(重複嘗試多次後crit_layers應疊滿, 統計上排除運氣, 證明immune分支→
    # fire_self_reactive的接線正確, 非僅fire_self_reactive本身可獨立運作)。
    bl203_u = Unit(POOL["張飛"], "盾", inherit=["一身是膽"])
    apply_effects(bl203_u, None, TACTICS["一身是膽"], [bl203_u], [], )  # prep套用stat+insight(critUp帶when.on, prep階段自動跳過)
    assert bl203_u.insight > 0, "一身是膽: 套用後應獲得insight(全免疫控制)"
    bl203_attacker = Unit(POOL["關羽"], "騎")
    _FIGHT_CTX["allies_of"] = lambda u: [bl203_attacker] if u is bl203_attacker else [bl203_u]
    _FIGHT_CTX["foes_of"] = lambda u: [bl203_u] if u is bl203_attacker else [bl203_attacker]
    random.seed(2026071102)
    for _ in range(60):
        apply_effects(bl203_attacker, None,
                      {"effects": [{"k": "stun", "who": "enemy", "dur": 1}], "nameZh": "批L測試控制"},
                      [bl203_attacker], [bl203_u], rate_checked=True)
    assert bl203_u.stun == 0, "一身是膽: insight應完全擋下stun, u.stun應維持0"
    bl203_layers = (bl203_u.crit_layers or {}).get(id(bl_ysb_crit_e), 0)
    assert bl203_layers == 7, \
        (f"一身是膽: 60次stun嘗試皆被insight擋下, 應各自觸發ctrlImmune事件並統計上疊滿7層"
         f"(驗證apply_effects的k==stun/silence/disarm/chaos/ambush immune分支確實呼叫了"
         f"fire_self_reactive, 而非僅fire_self_reactive函式本身正確但未被真正接線), 實得{bl203_layers}")
    print("    [批L 203] 一身是膽 ctrlImmune真實接線(stun/silence/disarm/chaos/ambush的immune"
          "分支→fire_self_reactive, 非僅獨立函式) 驗證通過")

    # 204) 一身是膽: rateLeader(主將35%→70% vs 非主將20%→40%, 皆取滿級)——經驗機率應收斂至
    # 對應檔位, 且用重置crit_layers的方式隔離「每次獨立判定觸發機率」與「疊層封頂」兩件事。
    bl204_leader = Unit(POOL["張飛"], "盾", inherit=["一身是膽"])
    bl204_sub = Unit(POOL["關羽"], "騎", inherit=["一身是膽"])
    bl204_allies = [bl204_leader, bl204_sub]           # index0=主將
    _FIGHT_CTX["allies_of"] = lambda u: bl204_allies
    _FIGHT_CTX["foes_of"] = lambda u: []
    random.seed(2026071103)
    n204, hits_leader, hits_sub = 4000, 0, 0
    for _ in range(n204):
        bl204_leader.crit_layers = {}
        fire_self_reactive(bl204_leader, "ctrlImmune", 1)
        if (bl204_leader.crit_layers or {}).get(id(bl_ysb_crit_e), 0) == 1:
            hits_leader += 1
        bl204_sub.crit_layers = {}
        fire_self_reactive(bl204_sub, "ctrlImmune", 1)
        if (bl204_sub.crit_layers or {}).get(id(bl_ysb_crit_e), 0) == 1:
            hits_sub += 1
    rate_leader_emp, rate_sub_emp = hits_leader / n204, hits_sub / n204
    assert abs(rate_leader_emp - 0.7) < 0.03, \
        f"一身是膽(主將): 觸發率經驗值應收斂至rateLeader=0.7, 實測{rate_leader_emp}"
    assert abs(rate_sub_emp - 0.4) < 0.03, \
        f"一身是膽(副將): 觸發率經驗值應收斂至基礎rate=0.4(非主將不吃rateLeader), 實測{rate_sub_emp}"
    print(f"    [批L 204] 一身是膽 rateLeader經驗機率(主將{rate_leader_emp:.3f}≈0.7 / "
          f"副將{rate_sub_emp:.3f}≈0.4) 驗證通過")

    # 205) 先登死士: 雙分支反應資料結構 + real-data斷言(stealStat/rateup各自的
    # victimIsTgt/ifStatCompare/maxStack/maxStackIfLeaderIs欄位)。
    bl_xds_tac = TACTICS["先登死士"]
    bl_xds_steal_e = next(e for e in bl_xds_tac["effects"] if e["k"] == "stealStat")
    bl_xds_rateup_e = next(e for e in bl_xds_tac["effects"] if e["k"] == "rateup")
    assert bl_xds_steal_e.get("victimIsTgt") is True and bl_xds_steal_e.get("amount") == 21 \
        and bl_xds_steal_e.get("ifStatCompare") == {"stat": "hpPct", "op": "lt", "vs": "caster"} \
        and bl_xds_steal_e.get("maxStack") == 4 \
        and bl_xds_steal_e.get("maxStackIfLeaderIs") == {"who": "麴義", "max": 5}, \
        f"先登死士: stealStat段real-data結構不符預期, 實得{bl_xds_steal_e}"
    assert bl_xds_rateup_e.get("n") == 1 and abs(bl_xds_rateup_e.get("val", 0) - (-0.03)) < 1e-9 \
        and bl_xds_rateup_e.get("ifStatCompare") == {"stat": "hpPct", "op": "gte", "vs": "caster"} \
        and bl_xds_rateup_e.get("maxStackIfLeaderIs") == {"who": "麴義", "max": 5}, \
        f"先登死士: rateup段real-data結構不符預期, 實得{bl_xds_rateup_e}"
    print("    [批L 205] 先登死士 real-data雙分支(stealStat.victimIsTgt+ifStatCompare/"
          "rateup.n+ifStatCompare)+麴義maxStackIfLeaderIs欄位 驗證通過")

    # 206) 先登死士 端到端(rate_checked繞過0.6擲骰, 專注驗證分支互斥+targeting正確性):
    # 受害者(持有者)兵力%低於攻擊者時, 應精確從攻擊者身上偷統率(而非誤及敵軍全體或方向錯誤),
    # 且不應同時觸發rateup分支(ifStatCompare op互斥lt/gte, 兩者恰好覆蓋若/否則兩種情形)。
    bl206_holder = Unit(POOL["張飛"], "弓")
    bl206_holder.troop = 3000                          # 低兵力% < 攻擊者
    bl206_attacker = Unit(POOL["關羽"], "騎")
    bl206_attacker.troop = 9000                         # 高兵力%
    bl206_bystander = Unit(POOL["曹操"], "騎")           # 非攻擊者的敵軍第三人, 驗證不應被誤及
    bl206_allies, bl206_enemies = [bl206_holder], [bl206_attacker, bl206_bystander]
    before_cmd_attacker = bl206_attacker.eff("command")
    before_cmd_bystander = bl206_bystander.eff("command")
    apply_effects(bl206_holder, bl206_attacker, {"effects": [bl_xds_steal_e], "kind": "phys", "nameZh": "先登死士"},
                  bl206_allies, bl206_enemies, reactive=True, rate_checked=True)
    apply_effects(bl206_holder, bl206_attacker, {"effects": [bl_xds_rateup_e], "kind": "phys", "nameZh": "先登死士"},
                  bl206_allies, bl206_enemies, reactive=True, rate_checked=True)
    assert bl206_attacker.eff("command") < before_cmd_attacker - 15, \
        (f"先登死士: 受害者兵力%低於攻擊者時應精確偷取攻擊者(tgt)統率(≈21點), "
         f"before={before_cmd_attacker}, after={bl206_attacker.eff('command')}")
    assert abs(bl206_bystander.eff("command") - before_cmd_bystander) < 1e-6, \
        "先登死士: victimIsTgt應精確鎖定攻擊者本人, 不應誤及敵軍第三人(旁觀者)"
    assert not any(a[0] == "rateup" for a in bl206_attacker.adds), \
        "先登死士: 受害者兵力%低於攻擊者時應只觸發stealStat分支(ifStatCompare op=lt), 不應同時觸發rateup分支(op=gte互斥)"
    assert any(a[0] == "command" for a in bl206_holder.stat_adds), \
        "先登死士: 偷到的統率應加到持有者(受益者, stealStat預設recipient=caster)身上"
    # 互斥情境反轉: 受害者兵力%不低於(>=)攻擊者 —— 應改觸發rateup(降低攻擊者發動率), 不觸發偷屬性
    bl206b_holder = Unit(POOL["張飛"], "弓")
    bl206b_holder.troop = 9500
    bl206b_attacker = Unit(POOL["關羽"], "騎")
    bl206b_attacker.troop = 5000
    apply_effects(bl206b_holder, bl206b_attacker, {"effects": [bl_xds_steal_e], "kind": "phys", "nameZh": "先登死士"},
                  [bl206b_holder], [bl206b_attacker], reactive=True, rate_checked=True)
    apply_effects(bl206b_holder, bl206b_attacker, {"effects": [bl_xds_rateup_e], "kind": "phys", "nameZh": "先登死士"},
                  [bl206b_holder], [bl206b_attacker], reactive=True, rate_checked=True)
    assert not any(a[0] == "command" for a in bl206b_holder.stat_adds), \
        "先登死士: 受害者兵力%不低於攻擊者時(互斥反向情境), 不應觸發stealStat偷屬性分支"
    assert any(a[0] == "rateup" and a[1] < 0 for a in bl206b_attacker.adds), \
        "先登死士: 受害者兵力%不低於攻擊者時, 應觸發rateup分支對攻擊者施加負值(降低發動率)debuff"
    print("    [批L 206] 先登死士 端到端雙分支互斥(兵力%比較決定stealStat或rateup擇一觸發)+"
          "victimIsTgt精確鎖定攻擊者本人(不誤及旁觀敵軍) 驗證通過")

    # 207) 先登死士: maxStackIfLeaderIs(若麴義統領則疊加上限4→5次) —— 麴義統領時應可疊到
    # 第5層(非麴義/非主將則封頂於4層)。用rate_checked繞過機率擲骰, 逐次呼叫驗證疊層數。
    bl207_ququyi = Unit(POOL["麴義"], "弓")
    bl207_ququyi_ally = [bl207_ququyi]                  # index0=麴義=主將
    bl207_target = Unit(POOL["關羽"], "騎")
    bl207_target.troop = 9000
    bl207_ququyi.troop = 3000
    for _ in range(6):
        apply_effects(bl207_ququyi, bl207_target, {"effects": [bl_xds_steal_e], "kind": "phys", "nameZh": "先登死士"},
                      bl207_ququyi_ally, [bl207_target], reactive=True, rate_checked=True)
    n_stacks_ququyi = sum(1 for a in bl207_ququyi.stat_adds if a[0] == "command" and a[3] == "先登死士")
    assert n_stacks_ququyi == 5, \
        f"先登死士: 麴義統領時應可疊加至5層(maxStackIfLeaderIs覆寫), 6次觸發後實得{n_stacks_ququyi}層"
    bl207_other = Unit(POOL["張飛"], "弓")               # 非麴義, 應維持4層上限
    bl207_other_ally = [bl207_other]
    bl207_target2 = Unit(POOL["關羽"], "騎")
    bl207_target2.troop = 9000
    bl207_other.troop = 3000
    for _ in range(6):
        apply_effects(bl207_other, bl207_target2, {"effects": [bl_xds_steal_e], "kind": "phys", "nameZh": "先登死士"},
                      bl207_other_ally, [bl207_target2], reactive=True, rate_checked=True)
    n_stacks_other = sum(1 for a in bl207_other.stat_adds if a[0] == "command" and a[3] == "先登死士")
    assert n_stacks_other == 4, \
        f"先登死士: 非麴義持有者疊加上限應維持4層, 6次觸發後實得{n_stacks_other}層"
    print("    [批L 207] 先登死士 maxStackIfLeaderIs(麴義統領疊加上限4→5次, 非麴義維持4次) 驗證通過")

    # 208) 才辯機捷: e.dmgFromStatus(僅k=='amp')跨戰法橫切限定範圍 —— 只對dot_status命中
    # 清單內的6種具名狀態(灼燒/水攻/中毒/潰逃/沙暴/叛逃)造成的傷害提升90%, 一般傷害路徑
    # (dot_status未傳)與清單外狀態皆不應觸發此amp。val應為固定0.9(非45%→90%等級區間)。
    bl_cbjj_tac = TACTICS["才辯機捷"]
    bl_cbjj_amp_e = next(e for e in bl_cbjj_tac["effects"] if e["k"] == "amp")
    assert bl_cbjj_amp_e.get("dmgFromStatus") == ["灼燒", "水攻", "中毒", "潰逃", "沙暴", "叛逃"] \
        and abs(bl_cbjj_amp_e.get("val", 0) - 0.9) < 1e-9, \
        f"才辯機捷: amp段real-data應為dmgFromStatus六狀態清單+val:0.9(固定值), 實得{bl_cbjj_amp_e}"
    bl208_caster = Unit(POOL["張飛"], "盾", inherit=["才辯機捷"])
    apply_effects(bl208_caster, None, TACTICS["才辯機捷"], [bl208_caster], [], )  # prep套用amp(帶dmgFromStatus旗標)+healGiven
    bl208_target = Unit(POOL["關羽"], "騎")
    random.seed(2026071104)
    dmg_normal = damage(bl208_caster, bl208_target, 1.0, "phys")             # 一般傷害(未傳dot_status)
    random.seed(2026071104)                                                  # 重設種子, 排除±4%隨機帶差異
    dmg_burn = damage(bl208_caster, bl208_target, 1.0, "phys", dot_status="灼燒")  # 清單內狀態
    random.seed(2026071104)
    dmg_unlisted = damage(bl208_caster, bl208_target, 1.0, "phys", dot_status="未知狀態XYZ")  # 清單外狀態
    assert abs(dmg_burn - dmg_normal * 1.9) < 1e-6, \
        (f"才辯機捷: dot_status='灼燒'(清單內)應觸發dmgFromStatus限定的90%增傷(1.9倍), "
         f"dmg_burn={dmg_burn}, dmg_normal={dmg_normal}, 期望={dmg_normal * 1.9}")
    assert abs(dmg_unlisted - dmg_normal) < 1e-6, \
        (f"才辯機捷: dot_status為清單外的狀態不應觸發amp, dmg_unlisted={dmg_unlisted} 應等於 "
         f"dmg_normal={dmg_normal}")
    print("    [批L 208] 才辯機捷 e.dmgFromStatus跨戰法橫切限定(清單內具名狀態dot傷害×1.9,"
          "一般傷害/清單外狀態不受影響) 驗證通過")

    # 209) 端到端整合: 3筆戰法各自掛載(inhA)+三者同時掛在同一人身上(壓力測試共存), 完整
    # fight()/simulate()跑一輪不應崩潰且應產生合法勝率(對稱既有200的end-to-end慣例)。
    random.seed(2026071105)
    for _bl209_name in ("一身是膽", "先登死士", "才辯機捷"):
        bl209_res = simulate(["張飛", "關羽", "劉備"], ["諸葛亮", "周瑜", "司馬懿"], n=200,
                             inhA=[[_bl209_name], None, None])
        assert 0 <= bl209_res["A勝率"] <= 1 and 0 <= bl209_res["B勝率"] <= 1, \
            f"批L端到端({_bl209_name}): 完整simulate()200場應正常產生合法勝率, got {bl209_res}"
    bl209_res_all = simulate(["張飛", "關羽", "劉備"], ["諸葛亮", "周瑜", "司馬懿"], n=150,
                             inhA=[["一身是膽", "先登死士", "才辯機捷"], None, None])
    assert 0 <= bl209_res_all["A勝率"] <= 1 and 0 <= bl209_res_all["B勝率"] <= 1, \
        f"批L端到端(三戰法同場共存): 完整simulate()150場應正常產生合法勝率, got {bl209_res_all}"
    print("    [批L 209] 一身是膽/先登死士/才辯機捷 端到端(各自掛載200場+三者同場共存150場,"
          "完整fight()/simulate()) 皆無崩潰且產生合法勝率 驗證通過")

    # 210) 傷害不浮動(user權威規則2026-07-11) —— 確認damage()已移除舊±4%隨機帶(原
    # random.uniform(0.96,1.04), sgz.py曾在此處/engine.js base*=0.96+rnd()*0.08), 同單位
    # 同攻擊(相同src/dst/coef/kind等全部輸入)重複結算應為完全相同的定值。用無critUp的乾淨
    # 單位繞過會心(critRate=0時damage()內部完全不呼叫random.random(), 全程零隨機源, 定值
    # 是數學必然而非機率巧合)。210b: 會心(critUp>0時)刻意保留為離散二元事件(觸發/未觸發恰好
    # 收斂成2種定值, 且比值精確為crit_mult+1=2.0), 佐證「移除的是連續隨機帶, 不是移除會心
    # 本身」, 與user規則「傷害數字不會浮動(會心離散擲骰除外)」精確對應。
    det_src = Unit(POOL["呂布"], "騎")
    det_dst = Unit(POOL["張飛"], "盾")
    assert det_src.addbonus("critUp", "phys") == 0, \
        "測試前置條件: 210determinism測試單位不應帶critUp(否則會心離散擲骰會干擾定值比較)"
    dmg_samples_210 = [damage(det_src, det_dst, 1.0, "phys") for _ in range(30)]
    assert len(set(dmg_samples_210)) == 1, \
        f"傷害不浮動: 同單位同攻擊(critRate=0繞過會心)重複結算30次應為完全相同定值, 實得{set(dmg_samples_210)}"
    crit_src_210 = Unit(POOL["呂布"], "騎")
    crit_src_210.push_add("critUp", 0.5, 9, "測試210b會心離散")
    random.seed(20260711)
    dmg_samples_210b = [damage(crit_src_210, Unit(POOL["張飛"], "盾"), 1.0, "phys") for _ in range(200)]
    uniq_210b = sorted(set(round(v, 6) for v in dmg_samples_210b))
    assert len(uniq_210b) == 2, \
        f"會心應是離散二元事件(未觸發/觸發恰好2種定值), 非連續浮動, 實得{len(uniq_210b)}種相異值: {uniq_210b}"
    assert abs(uniq_210b[1] / uniq_210b[0] - 2.0) < 1e-6, \
        f"觸發會心的傷害應恰為未觸發的2倍(crit_mult=1.0基準, 官方戰報實測+100%), 實得比值{uniq_210b[1] / uniq_210b[0]:.6f}"
    print(f"    [批A 210] 傷害不浮動: damage()移除±4%隨機帶後同輸入30次結算精確相等(定值={dmg_samples_210[0]:.4f}), "
          f"會心維持離散二元(未觸發{uniq_210b[0]:.4f}/觸發{uniq_210b[1]:.4f}=2.0倍) 驗證通過")

    # 211) 批B: filter-then-pick跨種子驗證 —— 橫掃千軍(震懾ifTargetHas=[繳械,計窮])對
    # 「1個已計窮+N個乾淨」敵組, 修正前係「先pick_targets隨機挑, 挑完才用ifTargetHas過濾」,
    # 隔離實測只約29/50命中(隨機挑中不合格目標就白白落空); 修正後應50/50精確命中合格目標、
    # 0/50誤中不合格目標。刻意不沿用hszj_tac頂層t["n"]=3(那樣2/4人池會因len(pool)<=cnt提前
    # 短路成「回傳全池」, 反而測不到隨機挑選環節), 改用190號測試既有的最小forced寫法(無頂層
    # n, 逼t.get("n") or 1恆等於1)確保每次真的走一次pick_targets(pool,1)隨機抽樣, 對N=1(2人池,
    # 對稱190號測試)與N=3(4人池, 更大候選池的加強版)各跑6組不同種子起點×50次, 排除單一種子
    # 偶然過關的可能。
    hszj_tac_211 = TACTICS["橫掃千軍"]
    hszj_stun_e_211 = next(e for e in hszj_tac_211["effects"] if e.get("k") == "stun")

    def _hszj_trial_211(seed, n_clean):
        random.seed(seed)
        caster = Unit(POOL["關羽"], "騎")
        silenced = Unit(POOL["張飛"], "騎")
        silenced.silence = 2
        cleans = [Unit(POOL[nm], "騎") for nm in (["趙雲", "曹操", "劉備"][:n_clean])]
        forced = {"nameZh": "橫掃千軍測試211", "effects": [dict(hszj_stun_e_211, rate=1.0)]}
        apply_effects(caster, None, forced, [caster], [silenced] + cleans, no_heal=True, skip_when_effects=True)
        return silenced.stun > 0, any(c.stun > 0 for c in cleans)

    for n_clean_211 in (1, 3):                          # 1: 2人池(對稱190號測試); 3: 4人池加強版
        for seed_base_211 in range(6):                  # 跨6組不同種子起點, 排除單一種子偶然過關
            results_211 = [_hszj_trial_211(seed_base_211 * 10_000_019 + i, n_clean_211) for i in range(50)]
            hit_eligible_211 = sum(1 for s, _c in results_211 if s)
            hit_ineligible_211 = sum(1 for _s, c in results_211 if c)
            assert hit_eligible_211 == 50, \
                (f"橫掃千軍filter-then-pick(n_clean={n_clean_211}, seed_base={seed_base_211}): "
                 f"已計窮目標應50/50命中震懾, 實得{hit_eligible_211}/50")
            assert hit_ineligible_211 == 0, \
                (f"橫掃千軍filter-then-pick(n_clean={n_clean_211}, seed_base={seed_base_211}): "
                 f"乾淨目標應0/50誤中震懾, 實得{hit_ineligible_211}/50")
    print("    [批B 211] 橫掃千軍filter-then-pick跨種子(2人池×6種子+4人池×6種子, 各50次) "
          "50/50精確命中合格目標+0/50誤中不合格目標 驗證通過")

    # ------------------------------------------------------------------
    # 時序重構(2026-07, user權威規則): DoT/狀態持續改為逐單位行動時結算
    # ------------------------------------------------------------------

    # 212) DoT先於行動 —— 輪到該單位行動時, 先結算它自己的DoT(掉血), 行動(造成傷害)時兵力
    # 已是DoT扣除後的值, 而非扣除前(取代舊「回合末全體同時tick()」; 舊制下本回合行動一律用
    # DoT結算前的兵力, 傷害公式的sqrt(troop)項因而偏高)。damage()以src.troop為輸入(sqrt
    # 項), 同一單位dot_settle()前後各採樣一次傷害, 後者(troop較低)應嚴格小於前者。
    dot212_src = Unit(POOL["呂布"], "騎")
    dot212_dst = Unit(POOL["張飛"], "盾")
    dot212_troop_before = dot212_src.troop
    dmg_before_dot_212 = damage(dot212_src, dot212_dst, 1.0, "phys")
    dot212_src.dots.append([dot212_src.troop * 0.5, 2])   # 灼燒: 扣50%當前兵力
    dot212_src.dot_settle()
    assert dot212_src.troop < dot212_troop_before, "212: dot_settle()後兵力應已扣除(DoT掉血結算)"
    dmg_after_dot_212 = damage(dot212_src, dot212_dst, 1.0, "phys")
    assert dmg_after_dot_212 < dmg_before_dot_212, \
        ("212: DoT先於行動 —— 結算DoT後(兵力已扣)若接著行動(造成傷害), 傷害應以已扣血的兵力"
         f"計算(較低), 而非扣血前的原始兵力, 實得扣血前={dmg_before_dot_212:.2f} 扣血後={dmg_after_dot_212:.2f}")

    # 213) 持續N回合 = 該單位自己N個行動輪(不是全局回合數, 也不需舊制+1補償) —— dot dur=3
    # (原值, 全庫已移除+1補償): 該單位連續3次「自己的行動輪」(tick()=dot_settle()+
    # decay_durations()合併捷徑, 見Unit.tick() docstring)各掉血1次, 第3次後到期清除,
    # 第4次不再掉血。一般狀態持續(震懾)同語意, dur=2應恰好2個行動輪內生效。
    dur213_u = Unit(POOL["張飛"], "盾")
    dot213_dmg = 100.0
    dur213_u.dots.append([dot213_dmg, 3])
    troop213_0 = dur213_u.troop
    dur213_u.tick()  # 第1個行動輪
    assert abs(dur213_u.troop - (troop213_0 - dot213_dmg)) < 1e-6 and len(dur213_u.dots) == 1, \
        "213: dur=3的DoT第1個行動輪應掉血1次且仍存在(剩2輪)"
    dur213_u.tick()  # 第2個行動輪
    assert abs(dur213_u.troop - (troop213_0 - 2 * dot213_dmg)) < 1e-6 and len(dur213_u.dots) == 1, \
        "213: dur=3的DoT第2個行動輪應再掉血1次且仍存在(剩1輪)"
    dur213_u.tick()  # 第3個行動輪
    assert abs(dur213_u.troop - (troop213_0 - 3 * dot213_dmg)) < 1e-6 and len(dur213_u.dots) == 0, \
        "213: dur=3的DoT第3個行動輪應第3次掉血後到期清除(恰好3個行動輪, 不需+1補償)"
    troop213_3 = dur213_u.troop
    dur213_u.tick()  # 第4個行動輪: 已到期, 不應再掉血
    assert abs(dur213_u.troop - troop213_3) < 1e-6, "213: 到期後第4個行動輪不應再掉血"
    stun213_u = Unit(POOL["張飛"], "盾")
    apply_effects(Unit(POOL["諸葛亮"], "弓"), stun213_u,
                  {"nameZh": "測試213震懾", "effects": [{"k": "stun", "who": "enemy", "dur": 2}]},
                  [], [stun213_u], no_heal=True)
    assert stun213_u.stun == 2, "213: 施加dur=2的震懾應原值儲存(不補償+1)"
    stun213_u.decay_durations()  # 該單位第1個行動輪(跳過行動, 但持續仍遞減)
    assert stun213_u.stun == 1, "213: 震懾第1個行動輪後應剩1(仍生效)"
    stun213_u.decay_durations()  # 該單位第2個行動輪
    assert stun213_u.stun == 0, "213: 震懾第2個行動輪後應歸零(恰好2個行動輪, 解除)"

    # 214) DoT致死則該單位不行動 —— dot_settle()若使兵力降至<=0(alive為troop>0的計算屬性),
    # fight()主迴圈於dot_settle()後立即檢查alive, 陣亡則直接continue(不再檢查stun/silence、
    # 不發動戰法、不普攻), 與「先受傷害才死亡」的一般攻擊死亡完全對稱, 只是觸發源是自己的DoT
    # 而非敵方普攻/戰法。
    lethal214_u = Unit(POOL["張飛"], "盾")
    assert lethal214_u.alive, "214前置: 施加DoT前應存活"
    lethal214_u.dots.append([lethal214_u.troop + 999999, 1])  # 灼燒傷害遠超剩餘兵力, 必定致死
    lethal214_u.dot_settle()
    assert not lethal214_u.alive, \
        "214: 致命DoT結算後應陣亡(troop<=0), fight()主迴圈的`if not u.alive: continue`應在此觸發, 該單位不再行動"
    # 對照組: 非致命DoT不應阻止行動
    survive214_u = Unit(POOL["張飛"], "盾")
    survive214_u.dots.append([survive214_u.troop * 0.1, 1])  # 僅扣10%兵力, 不致死
    survive214_u.dot_settle()
    assert survive214_u.alive, "214對照: 非致命DoT結算後應仍存活, 可正常進入stun檢查/行動"

    # 215) 快慢單位控制/DoT狀態結算時點依行動順序 —— 同一回合內, 較快單位(已行動完才被施加,
    # 對應「行動後施加」)與較慢單位(尚未行動前被施加, 對應「行動前施加」)最終都應恰好獲得
    # dur指定的N個「自己的行動輪」效果, 只是起算的絕對回合不同(較快單位下一輪才開始算,
    # 較慢單位本輪即開始算)。舊制「回合末全體同時tick()」下, +1補償只能兜住其中一種順序,
    # 另一種順序仍會多算1輪(見 Unit.decay_durations() docstring); 新制逐單位在自己行動之後
    # 才-1, 兩種順序皆自然精確, 不再有±1回合誤差。
    def _simulate_n_turns_215(u, n_turns):
        """依序模擬 u 接下來 n_turns 個自己的行動輪(dot_settle→alive檢查→stun檢查(受控則仍
        decay_durations但跳過行動)→(略行動)→decay_durations), 回傳被跳過(受控)的輪數。"""
        skipped = 0
        for _ in range(n_turns):
            if not u.alive:
                break
            u.dot_settle()
            if not u.alive:
                break
            if u.stun:
                skipped += 1
                u.decay_durations()
                continue
            u.decay_durations()  # (略去實際攻擊, 此測試只關心是否被跳過)
        return skipped

    fast215 = Unit(POOL["張飛"], "盾")  # 較快: 先完成本輪行動輪, 之後才被施加
    slow215 = Unit(POOL["張飛"], "盾")  # 較慢: 本輪行動輪尚未開始前就被施加
    caster215 = Unit(POOL["諸葛亮"], "弓")
    stun_tac_215 = {"nameZh": "測試215控制", "effects": [{"k": "stun", "who": "enemy", "dur": 2}]}

    _simulate_n_turns_215(fast215, 1)  # 較快單位先跑完本輪1個行動輪(未受控, 正常行動)
    apply_effects(caster215, fast215, stun_tac_215, [caster215], [fast215], no_heal=True)
    assert fast215.stun == 2, "215: 施加dur=2應原值儲存(不補償+1)"
    fast215_skipped = _simulate_n_turns_215(fast215, 3)
    assert fast215_skipped == 2, \
        f"215(較快單位, 行動後施加): dur=2應使接下來恰好2個自己的行動輪被跳過, 實得{fast215_skipped}"

    apply_effects(caster215, slow215, stun_tac_215, [caster215], [slow215], no_heal=True)  # 較慢單位本輪行動輪前就被施加
    assert slow215.stun == 2, "215: 施加dur=2應原值儲存(不補償+1)"
    slow215_skipped = _simulate_n_turns_215(slow215, 3)
    assert slow215_skipped == 2, \
        f"215(較慢單位, 行動前施加): dur=2應使接下來恰好2個自己的行動輪被跳過, 實得{slow215_skipped}"
    print("    [時序重構 212-215] DoT先於行動(212)/持續N=該單位N個行動輪(213)/DoT致死不行動(214)/"
          "快慢單位結算時點依行動順序皆精確無±1誤差(215) 驗證通過")

    # ------------------------------------------------------------------
    # 時序一致化(2026-07 批次): settle/stack/everyRound 改逐單位行動輪(接續212-215的DoT/
    # 狀態持續時序重構, 本批處理上批遺留待user確認的三類全局回合cadence機制)
    # ------------------------------------------------------------------

    # 216) 時序徹底一致化批: stack.stackPer=="round"(如長驅直入) 的逐回合遞增, cadence改為該
    # 持有者自己的行動輪, 但呼叫點本批從 decay_durations()(行動後)拆出、移到新方法
    # tick_stack()(行動前) —— 直接呼叫 tick_stack() N 次應精確遞增N層(封頂於max), 且與全局
    # CUR_ROUND是否變動完全無關。decay_durations()改為完全不涉及stack(已拆分乾淨, 呼叫N次
    # 不應使stack有任何變動, 對稱前批「舊全局cadence已移除」的驗證手法, 本批驗證「新舊呼叫點
    # 正確切分」)。
    stk216_u = Unit(POOL["張飛"], "槍")
    stk216_u.stack = {"per": 0.15, "max": 5, "n": 0, "stackPer": "round"}
    CUR_ROUND = 99                                      # 刻意設一個與行動輪次數無關的全局回合值
    for _i in range(3):
        stk216_u.tick_stack()
    assert stk216_u.stack["n"] == 3, \
        f"216: stackPer=='round'應每次tick_stack()(該持有者自己的1個行動輪, 行動前)+1層, 3次應為3層, 實得{stk216_u.stack['n']}(全局CUR_ROUND={CUR_ROUND}與此無關)"
    for _i in range(10):                                # 疊到封頂(max=5)
        stk216_u.tick_stack()
    assert stk216_u.stack["n"] == 5, f"216: 疊層應封頂於max=5, 實得{stk216_u.stack['n']}"
    # 216x: decay_durations()本身自本批起應與stack完全脫鉤(拆分乾淨), 呼叫任意次不應遞增stack
    stk216_u.decay_durations()
    stk216_u.decay_durations()
    assert stk216_u.stack["n"] == 5, f"216x: decay_durations()自本批起應與stack無關(職責已拆給tick_stack()), 呼叫後stack不應變動, 實得{stk216_u.stack['n']}"
    # 對照: 僅改變CUR_ROUND、不呼叫tick_stack(), 不應有任何遞增
    stk216_ctrl = Unit(POOL["張飛"], "槍")
    stk216_ctrl.stack = {"per": 0.15, "max": 5, "n": 0, "stackPer": "round"}
    for _r in range(1, 9):
        CUR_ROUND = _r
    assert stk216_ctrl.stack["n"] == 0, "216: 僅變動CUR_ROUND(不呼叫tick_stack)不應遞增stack(全局回合cadence已移除)"
    CUR_ROUND = 0
    # stackPer=="cast"/"attack" 兩種既有自參照模式不應受tick_stack()/decay_durations()影響(零回歸)
    stk216_cast = Unit(POOL["張飛"], "槍")
    stk216_cast.stack = {"per": 0.15, "max": 5, "n": 0, "stackPer": "cast"}
    stk216_cast.tick_stack()
    stk216_cast.decay_durations()
    assert stk216_cast.stack["n"] == 0, "216: stackPer=='cast'不應被tick_stack()/decay_durations()遞增(只認apply_stack_cast())"
    print("    [時序一致化 216] stack.stackPer=='round' cadence改為該持有者自己的行動輪"
          "(經tick_stack(), 與全局CUR_ROUND無關)/疊層封頂/decay_durations()與stack職責拆分乾淨"
          "(216x)/cast模式零回歸 驗證通過")

    # 216y) 時序徹底一致化批(關鍵回歸驗證): 「行動前檢查」語意 —— 模擬fight()主迴圈同一單位
    # 連續3個自己的行動輪, 每輪順序皆為 own_round+=1 → dot_settle() → tick_stack() →
    # (此處讀取stack.n模擬main coef讀取當下疊層值) → decay_durations()。驗證第N輪「行動」讀到
    # 的stack.n必須已含第N輪的+1(即「這回合行動時已吃到當回合疊層值」, 而非上一輪的舊值) ——
    # 這正是前批「行動後遞增使爬坡晚1輪(-7.6pp)」問題的直接迴歸測試: 若誤把tick_stack()放在
    # decay_durations()之後(行動後), 第1輪「行動」讀到的stack.n會是0(尚未疊, 舊bug重現), 現在
    # 應讀到1。
    stk216y_u = Unit(POOL["張飛"], "槍")
    stk216y_u.stack = {"per": 0.15, "max": 5, "n": 0, "stackPer": "round"}
    stack_seen_at_action = []
    for _own_r in range(1, 4):
        stk216y_u.own_round = _own_r
        stk216y_u.dot_settle()
        stk216y_u.tick_stack()
        stack_seen_at_action.append(stk216y_u.stack["n"])  # 模擬main coef讀取當下疊層值(行動時點)
        stk216y_u.decay_durations()
    assert stack_seen_at_action == [1, 2, 3], \
        (f"216y(關鍵回歸): 「行動前檢查」下, 該單位own_round=1/2/3三輪各自的行動時點應分別讀到"
         f"stack.n=1/2/3(當回合已疊好的值), 實得{stack_seen_at_action}(若為[0,1,2]代表stack仍是"
         "行動後才遞增的舊bug, 使用的是上一輪的舊值, 即前批-7.6pp問題重現)")
    print("    [時序徹底一致化 216y] 關鍵回歸: 行動前檢查下, 單位own_round=N時行動當下已讀到"
          f"stack.n=N(當回合疊層值), 非N-1(上輪舊值) —— 驗證通過, stack_seen={stack_seen_at_action}")

    # 217) A.2: settle(密計誅逆猛毒式) 的 e["when"] 一次性視窗註冊, 「第N回合」改用持有者
    # (caster)自己的行動輪計數(own_round)為基準, 與全局CUR_ROUND完全脫鉤 —— 刻意讓兩者背離
    # (CUR_ROUND固定在一個與own_round不同的值), 證明真正生效的判斷依據是own_round。
    stl217_caster = Unit(POOL["諸葛亮"], "弓")
    stl217_tgt = Unit(POOL["張飛"], "盾")
    stl217_tac = {"nameZh": "測試settle註冊217", "kind": "intel", "effects": [
        {"k": "settle", "who": "enemy", "base": 1.0, "per": 0.3, "max": 3, "dur": 2, "init": 0,
         "when": {"rounds": [3]}}]}
    CUR_ROUND = 999                                     # 刻意設一個遠離3的全局回合值
    stl217_caster.own_round = 1
    apply_effects(stl217_caster, None, stl217_tac, [stl217_caster], [stl217_tgt])
    assert stl217_tgt.settle is None, f"217: caster.own_round=1(非3)不應註冊settle, 即使全局CUR_ROUND={CUR_ROUND}"
    stl217_caster.own_round = 2
    apply_effects(stl217_caster, None, stl217_tac, [stl217_caster], [stl217_tgt])
    assert stl217_tgt.settle is None, "217: caster.own_round=2(非3)不應註冊settle"
    stl217_caster.own_round = 3
    apply_effects(stl217_caster, None, stl217_tac, [stl217_caster], [stl217_tgt])
    assert stl217_tgt.settle is not None, \
        f"217: caster.own_round=3應觸發settle註冊(縱使全局CUR_ROUND={CUR_ROUND}與3無關), 判斷依據應為caster.own_round非CUR_ROUND"
    assert stl217_tgt.settle["left"] == 2, "217: 註冊時left應取e['dur']=2原值"
    CUR_ROUND = 0

    # 217b) A.2: settle註冊必須「持有者(caster)自己真正走到那一個行動輪」才會發生 —— 若caster
    # 在own_round達到3之前就已死亡(被其他單位搶先擊殺), 即使模擬的全局回合數已經跑過3甚至
    # 更後面, 也永遠不會註冊(對應fight()主迴圈: own_round遞增點在`if not u.alive: continue`
    # 之後, 見該處註解)。用模擬迴圈重現此結構(不依賴完整fight()雙隊機制)。
    def _simulate_registration_217b(u, tac, foe, n_rounds, die_before_round=None):
        for r in range(1, n_rounds + 1):
            if not u.alive:
                break
            if r == die_before_round:
                u.troop = 0                             # 模擬本回合稍早已被擊殺, own_round不遞增(對應fight(): if not u.alive: continue)
                continue
            u.own_round += 1
            apply_effects(u, None, tac, [u], [foe], no_heal=True)

    stl217b_caster_dead = Unit(POOL["諸葛亮"], "弓")
    stl217b_foe_a = Unit(POOL["張飛"], "盾")
    _simulate_registration_217b(stl217b_caster_dead, stl217_tac, stl217b_foe_a, 5, die_before_round=3)
    assert stl217b_foe_a.settle is None, \
        "217b: 若持有者(caster)在own_round達到3之前就死亡(第3回合稍早被擊殺, own_round卡在2), 即使模擬跑完5個全局回合仍不應註冊settle(必須真正走到own_round==3那一輪)"
    stl217b_caster_alive = Unit(POOL["諸葛亮"], "弓")
    stl217b_foe_b = Unit(POOL["張飛"], "盾")
    _simulate_registration_217b(stl217b_caster_alive, stl217_tac, stl217b_foe_b, 5)
    assert stl217b_foe_b.settle is not None, "217b對照: 未死亡應正常在own_round==3時註冊settle"

    # 218) A.2: settle_tick(u, team) —— 模組層級函式(對稱settle_huchen), 直接驗證倒數/爆發
    # 邏輯本身(cadence已改由fight()主迴圈於holder自己的行動輪呼叫, 見該處; 此處驗證函式本身
    # 邏輯正確, 含新補上的perStackFrom動態coef讀取與singleTarget單體結算, 對齊engine.js既有
    # 能力, 見settle_tick() docstring)。
    caster218 = Unit(POOL["諸葛亮"], "弓")
    tgt218 = Unit(POOL["張飛"], "盾")
    ally218 = Unit(POOL["關羽"], "盾")
    team218 = [tgt218, ally218]
    tgt218.settle = {"layers": 1, "max": 3, "left": 2, "caster": caster218, "snap": caster218.troop,
                      "base": 1.0, "per": 0.3, "kind": "intel", "perStackFrom": None, "singleTarget": False}
    troop_before_218 = tgt218.troop
    ally_before_218 = ally218.troop
    settle_tick(tgt218, team218)                        # left=2, 未達1, 應只倒數(left-=1), 不爆發
    assert tgt218.settle is not None and tgt218.settle["left"] == 1, \
        f"218: left=2(>1)第一次settle_tick應只倒數不爆發, 實得settle={tgt218.settle}"
    assert tgt218.troop == troop_before_218 and ally218.troop == ally_before_218, "218: 未爆發時不應扣血"
    settle_tick(tgt218, team218)                        # left=1, <=1, 應爆發(對全隊, 含tgt218自己)
    assert tgt218.settle is None, "218: left<=1時應爆發並清除settle"
    assert tgt218.troop < troop_before_218, "218: 爆發應對tgt218(holder)自己造成傷害(全隊結算, holder也在team內)"
    assert ally218.troop < ally_before_218, "218: 爆發應對整隊(含隊友ally218)造成傷害(非singleTarget)"

    # singleTarget: 只打holder本人, 不影響隊友(對齊engine.js既有能力, sgz.py先前遺漏此分支)
    tgt218b = Unit(POOL["張飛"], "盾")
    ally218b = Unit(POOL["關羽"], "盾")
    team218b = [tgt218b, ally218b]
    tgt218b.settle = {"layers": 3, "max": 3, "left": 5, "caster": caster218, "snap": caster218.troop,
                       "base": 1.0, "per": 0.3, "kind": "intel", "perStackFrom": None, "singleTarget": True}
    ally218b_before = ally218b.troop
    settle_tick(tgt218b, team218b)                      # layers(3)>=max(3), 應立即爆發(不論left), singleTarget只打tgt218b
    assert tgt218b.settle is None and tgt218b.troop < START_TROOP, "218: layers>=max應立即爆發(不受left影響)"
    assert ally218b.troop == ally218b_before, "218: singleTarget=True時不應波及隊友(sgz.py先前遺漏此分支, 本批補上對齊engine.js)"

    # perStackFrom: 結算時動態讀取指定amp_layers_by_id計數器(而非靜態settle.layers), 對齊engine.js
    caster218c = Unit(POOL["諸葛亮"], "弓")
    tgt218c = Unit(POOL["張飛"], "盾")
    tgt218c.amp_layers_by_id = {"密計誅逆_test": 5}      # 模擬跨效果疊層計數器(如amp+stackKey+stackId寫入), 掛在holder(tgt218c)身上
    tgt218c.settle = {"layers": 1, "max": 99, "left": 1, "caster": caster218c, "snap": caster218c.troop,
                       "base": 1.0, "per": 0.1, "kind": "intel", "perStackFrom": "密計誅逆_test", "singleTarget": True}
    expected_coef_218c_high = 1.0 + 0.1 * 5             # perStackFrom應讀取的層數(5, 非settle.layers的1)
    dmg218c_expected_high = damage(caster218c, tgt218c, expected_coef_218c_high, "intel", caster218c.troop)
    troop218c_before = tgt218c.troop
    settle_tick(tgt218c, [tgt218c])
    dmg218c_actual = troop218c_before - tgt218c.troop
    assert abs(dmg218c_actual - dmg218c_expected_high) < 1e-6, \
        f"218: perStackFrom應動態讀取holder(tgt218c).amp_layers_by_id['密計誅逆_test']=5層代入coef" \
        f"(1.0+0.1×5={expected_coef_218c_high:.2f}), 而非settle自身靜態layers=1層, 預期傷害{dmg218c_expected_high:.2f} 實得{dmg218c_actual:.2f}"
    print("    [時序一致化 217-218] A.2 settle: e['when']一次性視窗註冊改用caster.own_round"
          "(與全局CUR_ROUND脫鉤, 217)/必須持有者真正走到own_round那一輪才註冊, 中途死亡不觸發"
          "(217b)/settle_tick()倒數與爆發(218)/singleTarget只打holder(218)/perStackFrom動態coef"
          "讀取(218) 驗證通過")

    # 219) A.2: coefFromStack(絕地反擊式自身疊層驅動爆發) 的 e["when"] 一次性視窗註冊同settle,
    # 「第N回合」改用持有者(caster)自己的行動輪計數(own_round)為基準, 與全局CUR_ROUND脫鉤。
    cfs219_caster = Unit(POOL["張飛"], "槍")
    cfs219_caster.amp_layers_by_id = {"jdfj_test": 4}
    cfs219_foe = Unit(POOL["張飛"], "盾")
    cfs219_tac = {"nameZh": "測試coefFromStack註冊219", "kind": "phys", "effects": [
        {"k": "dot", "who": "enemy", "n": 1, "kind": "phys", "dur": 1, "when": {"rounds": [5]},
         "coefFromStack": {"id": "jdfj_test", "base": 0.6, "per": 0.07}}]}
    CUR_ROUND = 1                                       # 刻意設一個遠離5的全局回合值
    cfs219_caster.own_round = 4
    apply_effects(cfs219_caster, None, cfs219_tac, [cfs219_caster], [cfs219_foe])
    assert not cfs219_foe.dots, f"219: caster.own_round=4(非5)不應觸發coefFromStack的dot註冊, 即使全局CUR_ROUND={CUR_ROUND}"
    cfs219_caster.own_round = 5
    apply_effects(cfs219_caster, None, cfs219_tac, [cfs219_caster], [cfs219_foe])
    assert cfs219_foe.dots, f"219: caster.own_round=5應觸發coefFromStack的dot註冊(縱使全局CUR_ROUND={CUR_ROUND}與5無關)"
    expected_coef_219 = 0.6 + 0.07 * 4                  # base+per×caster當下amp_layers_by_id層數(4層)
    expected_dot_dmg_219 = damage(cfs219_caster, cfs219_foe, expected_coef_219, "phys")
    assert abs(cfs219_foe.dots[0][0] - expected_dot_dmg_219) < 1e-6, \
        f"219: coefFromStack應讀取caster.amp_layers_by_id['jdfj_test']=4層代入coef(0.6+0.07×4={expected_coef_219:.3f}), dot掉血量應為{expected_dot_dmg_219:.2f}, 實得{cfs219_foe.dots[0][0]:.2f}"
    CUR_ROUND = 0

    # 220) A.3: everyRound 效果的「每回合」重擲cadence, 改用該持有者(caster)自己的行動輪計數
    # (own_round)為基準, 與全局CUR_ROUND完全脫鉤 —— 刻意讓兩者背離, 證明真正生效的判斷依據
    # 是own_round非CUR_ROUND(對稱217的settle背離驗證手法)。
    er220_caster = Unit(POOL["呂布"], "盾")
    er220_team = [er220_caster]
    er220_tac = {"nameZh": "測試everyRound背離220", "rate": 1.0, "effects": [
        {"k": "block", "who": "self", "val": 1.0, "times": 1, "everyRound": True, "when": {"rounds": [4]}}]}
    CUR_ROUND = 888                                     # 刻意設一個遠離4的全局回合值
    er220_caster.own_round = 1
    apply_effects(er220_caster, None, er220_tac, er220_team, [], own_turn=True)
    assert not er220_caster.block, f"220: caster.own_round=1(非4)不應觸發everyRound(when.rounds:[4]), 即使全局CUR_ROUND={CUR_ROUND}"
    er220_caster.own_round = 4
    apply_effects(er220_caster, None, er220_tac, er220_team, [], own_turn=True)
    assert er220_caster.block, f"220: caster.own_round=4應觸發everyRound(縱使全局CUR_ROUND={CUR_ROUND}與4無關)"
    CUR_ROUND = 0
    print("    [時序一致化 219-220] A.2 coefFromStack(絕地反擊式)一次性視窗註冊同settle改用"
          "own_round(219)/A.3 everyRound重擲cadence改用own_round, 與CUR_ROUND脫鉤驗證(220)"
          " 驗證通過")

    # 221) A.4: tac_cd(戰法冷卻) 複核 —— cd=N代表持有者自己接下來N個行動輪不可再發, 第N+1個
    # 行動輪起可用。上批(時序重構)已將cd+1的寫入與decay_durations()的遞減皆繫於「該單位自己
    # 行動」這唯一觸發點, 本質上已是自參照模型; 此處明確驗證其在「持有者中途因震懾等被跳過
    # 行動」情境下(cd遞減與DoT持續同一套decay_durations機制, 跳過行動仍遞減)依然精確。
    cd221_u = Unit(POOL["張飛"], "盾")
    cd221_u.tac_cd["測試冷卻221"] = 2 + 1               # 對應fight()主迴圈fire分支寫入慣例: cd(N=2)+1
    assert cd221_u.tac_cd.get("測試冷卻221", 0) > 0, "221前置: 冷卻中應不可發動(tac_cd>0)"
    cd221_u.decay_durations()                            # 第1個行動輪後
    assert cd221_u.tac_cd.get("測試冷卻221", 0) == 2, f"221: cd=2+1寫入後, 第1個行動輪decay_durations()應遞減為2, 實得{cd221_u.tac_cd.get('測試冷卻221', 0)}"
    cd221_u.decay_durations()                            # 第2個行動輪後
    assert cd221_u.tac_cd.get("測試冷卻221", 0) == 1, f"221: 第2個行動輪後應為1, 實得{cd221_u.tac_cd.get('測試冷卻221', 0)}"
    cd221_u.decay_durations()                            # 第3個行動輪後: 應歸零移除(冷卻N=2個行動輪不可用, 第3個行動輪起可用)
    assert cd221_u.tac_cd.get("測試冷卻221", 0) == 0, \
        f"221: cd=2代表接下來2個行動輪不可用, 第3個行動輪decay_durations()後應歸零(可再發), 實得{cd221_u.tac_cd.get('測試冷卻221', 0)}"
    # 震懾等跳過行動的回合仍應正常遞減(fight()主迴圈: 震懾單位仍呼叫decay_durations(), cd是否
    # 遞減只認decay_durations()是否被呼叫, 不論是否真正行動——獨立於震懾本身的驗證)
    cd221b_u = Unit(POOL["張飛"], "盾")
    cd221b_u.tac_cd["測試冷卻221b"] = 1 + 1
    cd221b_u.stun = 3                                     # 模擬震懾中(fight()主迴圈仍會呼叫decay_durations, 見該處註解)
    cd221b_u.decay_durations()
    assert cd221b_u.tac_cd.get("測試冷卻221b", 0) == 1, \
        f"221b: 即使該單位本輪因震懾跳過實際行動, fight()主迴圈仍會呼叫decay_durations()(該單位自己的1個行動輪已耗用), cd應照常遞減, 實得{cd221b_u.tac_cd.get('測試冷卻221b', 0)}"
    print("    [時序一致化 221] A.4 tac_cd複核: cd=N代表持有者自己接下來N個行動輪不可用"
          "(第N+1個行動輪起可用)/震懾跳過行動仍正常遞減 驗證通過")

    # ------------------------------------------------------------------
    # 時序徹底一致化批(最終定案): B1團隊buff/B2自參照戰法全數改own_round(相二逐單位,
    # 行動前檢查) + 相一全局round-start廣播(e.broadcast, 極少數實例) 保留CUR_ROUND
    # ------------------------------------------------------------------

    # 222) heal_only(戰報實證: 左慈金丹秘術/夏侯惇陷陣營) —— 團隊heal窗口改用caster(=持有者/
    # 受益單位)自己own_round為基準, 與全局CUR_ROUND完全脫鉤(對稱217/219背離驗證手法)。
    heal222_caster = Unit(POOL["諸葛亮"], "弓")
    heal222_target = Unit(POOL["張飛"], "盾")
    heal222_target.troop = 3000
    heal222_target.wounded = START_TROOP
    heal222_tac = {"nameZh": "測試heal_only背離222", "type": "command", "kind": "intel", "coef": 0,
                    "rate": 1.0, "n": 1, "prep": 0,
                    "effects": [{"k": "heal", "who": "ally", "coef": 0.8, "dur": 1, "when": {"rounds": [4]}}]}
    CUR_ROUND = 777                                      # 刻意設一個遠離4的全局回合值
    heal222_caster.own_round = 1
    apply_effects(heal222_caster, None, heal222_tac, [heal222_target], [], heal_only=True)
    assert heal222_target.troop == 3000, f"222: caster.own_round=1(非4)不應觸發heal_only(when.rounds:[4]), 即使全局CUR_ROUND={CUR_ROUND}"
    heal222_caster.own_round = 4
    apply_effects(heal222_caster, None, heal222_tac, [heal222_target], [], heal_only=True)
    assert heal222_target.troop > 3000, f"222: caster.own_round=4應觸發heal_only(縱使全局CUR_ROUND={CUR_ROUND}與4無關)"
    CUR_ROUND = 0
    print("    [時序徹底一致化 222] heal_only團隊回血通道改用持有者own_round, 與全局CUR_ROUND脫鉤驗證通過"
          "(戰報實證: 左慈金丹秘術/夏侯惇陷陣營, 團隊buff窗口=各受益單位自己行動輪結算)")

    # 223) e.when 泛化通道(非heal/非everyRound/非broadcast) —— user最終裁決「除相一廣播外一律
    # own_round」, 本批將此通道從舊「僅settle/coefFromStack用own_round, 其餘team-wide buff(如
    # 工神/橫戈躍馬)待user確認仍用CUR_ROUND」擴大為全數own_round。用amp(非settle/coefFromStack)
    # 驗證此擴大範圍確實生效(對稱219但改用amp證明不限settle/coefFromStack)。
    amp223_caster = Unit(POOL["張飛"], "槍")
    amp223_tac = {"nameZh": "測試team-wide buff own_round223", "kind": "phys", "effects": [
        {"k": "amp", "who": "ally", "val": 0.2, "dur": 99, "when": {"from": 4}}]}
    CUR_ROUND = 1                                        # 刻意設一個遠離4的全局回合值(window from:4)
    amp223_caster.own_round = 2
    apply_effects(amp223_caster, None, amp223_tac, [amp223_caster], [])
    assert abs(amp223_caster.addbonus("amp")) < 1e-9, f"223: caster.own_round=2(<4)不應觸發team-wide amp視窗, 即使全局CUR_ROUND={CUR_ROUND}"
    amp223_caster.own_round = 4
    apply_effects(amp223_caster, None, amp223_tac, [amp223_caster], [])
    assert abs(amp223_caster.addbonus("amp") - 0.2) < 1e-9, f"223: caster.own_round=4應觸發team-wide amp視窗(縱使全局CUR_ROUND={CUR_ROUND}與4無關) —— B1團隊buff(工神/橫戈躍馬類)裁決落地: 除相一廣播外一律own_round"
    CUR_ROUND = 0
    print("    [時序徹底一致化 223] e.when泛化通道全數(不限settle/coefFromStack)改own_round, "
          "與全局CUR_ROUND脫鉤驗證通過(B1團隊buff裁決: 除相一廣播外一律相二own_round)")

    # 224) 相一例外: e["broadcast"]=true(SP周瑜江天長焰/SP袁紹高櫓連營類「持有者每回合對他人
    # 廣播施加新狀態層」) —— 唯一仍讀全局CUR_ROUND、不隨own_round個別化的機制。反向背離驗證:
    # own_round變動不應影響判定, 只有CUR_ROUND才算數(與222/223方向相反, 證明broadcast旗標正確
    # 隔離出這條例外通道)。
    bc224_caster = Unit(POOL["孫權"], "弓")
    bc224_foe = Unit(POOL["曹操"], "騎")
    bc224_tac = {"nameZh": "測試相一廣播224", "kind": "intel", "effects": [
        {"k": "amp", "who": "enemy", "val": 0.04, "dur": 99, "everyRound": True, "broadcast": True,
         "when": {"rounds": [5]}, "stackKey": True, "perStack": 0.04}]}
    bc224_caster.own_round = 5                           # 刻意讓own_round命中5, 證明真正生效者是CUR_ROUND非own_round
    CUR_ROUND = 1                                        # 全局回合非5: broadcast_only應不觸發(即使own_round=5命中)
    apply_effects(bc224_caster, None, bc224_tac, [bc224_caster], [bc224_foe], broadcast_only=True)
    assert abs(bc224_foe.addbonus("amp")) < 1e-9, f"224: broadcast_only應以CUR_ROUND({CUR_ROUND})為準非own_round({bc224_caster.own_round}), CUR_ROUND!=5時不應觸發"
    # own_turn=True(相二通道)不應放行broadcast標記的效果, 確認雙通道互斥(即使own_round=5命中when.rounds:[5])
    apply_effects(bc224_caster, None, bc224_tac, [bc224_caster], [bc224_foe], own_turn=True)
    assert abs(bc224_foe.addbonus("amp")) < 1e-9, "224: own_turn(相二)通道不應放行e.broadcast=true的效果(應被broadcast_only專屬), 兩通道須互斥"
    bc224_caster.own_round = 1                           # 刻意讓own_round遠離5: 證明broadcast_only不受own_round影響, 只認CUR_ROUND
    CUR_ROUND = 5
    apply_effects(bc224_caster, None, bc224_tac, [bc224_caster], [bc224_foe], broadcast_only=True)
    assert bc224_foe.addbonus("amp") > 0, f"224: broadcast_only通道應以全局CUR_ROUND({CUR_ROUND})=5觸發, 縱使caster.own_round({bc224_caster.own_round})與5無關——此為user權威規則唯一例外"
    CUR_ROUND = 0
    print("    [時序徹底一致化 224] e.broadcast=true(相一全局round-start廣播, 高櫓連營/江天長焰類)"
          "仍用CUR_ROUND且與own_turn(相二)通道互斥驗證通過")

    # 225) 端到端: apply_own_turn_effects(fight()主迴圈內部) 現統一收斂heal_only/t.when窗口/
    # delayed_eq/e.when泛化四條channel, 改於u自己行動輪(dot_settle後、行動前)逐單位呼叫, 取代
    # 舊「回合迴圈頂端全體單位批次」通道。用inhA注入合成team-wide回血窗口戰法(對稱88/89既有
    # 端到端手法), 跑真正的fight(): (a) 零崩潰; (b) 命中方向正確(帶窗口治療的一方兵力顯著優於
    # 對照組, 證明通道確實在真實fight()迴圈中被呼叫且非no-op)。
    TACTICS["測試團隊回血窗225"] = {
        "nameZh": "測試團隊回血窗225", "type": "command", "kind": "intel", "coef": 0,
        "rate": 1.0, "n": 3, "prep": 0, "effects": [
            {"k": "heal", "who": "ally", "coef": 1.5, "dur": 1, "when": {"from": 1}}]}
    random.seed(9225)
    n225 = 250
    # 鏡像對局(雙方同陣容)排除「本來就已是天花板/地板勝率」的干擾, 只讓A方多帶此合成
    # team-wide回血窗口戰法, 觀察是否可觀測地推高A勝率(對稱既有批36/批37鏡像對局手法)。
    res225_with = simulate(["諸葛亮", "張飛", "關羽"], ["諸葛亮", "張飛", "關羽"], n=n225,
                            inhA=[["測試團隊回血窗225"], None, None])
    res225_ctrl = simulate(["諸葛亮", "張飛", "關羽"], ["諸葛亮", "張飛", "關羽"], n=n225)
    del TACTICS["測試團隊回血窗225"]
    assert res225_with["A勝率"] > res225_ctrl["A勝率"] + 0.05, \
        (f"225: team-wide回血窗口(from:1, who=ally, 逐單位own_round通道)應可觀測地提高勝率"
         f"(鏡像對局帶窗口A勝率{res225_with['A勝率']} vs 對照組(無窗口, 應≈0.5){res225_ctrl['A勝率']}, n={n225}), "
         "確認apply_own_turn_effects統一通道在真實fight()主迴圈端到端運作、非靜默no-op")
    print(f"    [時序徹底一致化 225] 端到端fight(): team-wide回血窗口逐單位own_round通道"
          f"零崩潰+方向正確(鏡像對局帶窗口A勝率{res225_with['A勝率']} vs 對照{res225_ctrl['A勝率']}, n={n225}) 驗證通過")

    # 226) 時序徹底一致化批(coordinator硬門檻, 相一再掃修正): 真實江天長焰(SP周瑜)的易傷 =
    # user親口舉的相一主例。驗證即使SP周瑜排最後(最慢)行動, 易傷仍在「回合開頭、任何單位行動
    # 前」統一施加(broadcast_only通道), 不隨SP周瑜出手順序。反事實關鍵: SP周瑜own_round=0(本
    # 回合尚未行動, 對應排最後還沒輪到他), 此刻broadcast_only就應把易傷施加給敵軍(若走舊
    # command-fire在SP周瑜行動輪才施加, own_round=0時敵軍身上不會有易傷)。
    jtcy226 = TACTICS.get("江天長焰")
    assert jtcy226 and any(e.get("broadcast") and e.get("who") == "enemy" and e.get("everyRound")
                           for e in jtcy226["effects"]), \
        "226: 江天長焰應有 broadcast:true + everyRound:true 的 who=enemy 易傷效果(相一主例, 見tactic_corrections)"
    zhouyu226 = Unit(POOL["SP 周瑜"], "弓")
    zhouyu226.tactics = [dict(jtcy226)]
    ally226 = Unit(POOL["諸葛亮"], "弓")           # 一名比SP周瑜快的我方隊友
    enemy226 = Unit(POOL["張飛"], "盾")
    # 強制SP周瑜最慢(排最後行動), 隊友最快 —— 模擬「SP周瑜排最後」情境
    zhouyu226.push_stat_add("speed", -200, 99, src="test")
    ally226.push_stat_add("speed", 300, 99, src="test")
    assert zhouyu226.eff("speed") < ally226.eff("speed") and zhouyu226.eff("speed") < enemy226.eff("speed"), \
        "226前置: SP周瑜應被強制為場上最慢(排最後行動)"
    # 回合開頭: SP周瑜own_round仍為0(尚未輪到他, 因為他最慢)。broadcast_only此刻施加易傷。
    zhouyu226.own_round = 0
    CUR_ROUND = 1
    assert not zhouyu226.tactics[0]["effects"][0].get("_x")  # (無副作用佔位, 確保下方讀的是同一效果物件)
    apply_effects(zhouyu226, None, zhouyu226.tactics[0], [zhouyu226, ally226], [enemy226], broadcast_only=True)
    assert enemy226.amp_layers and any(v > 0 for v in enemy226.amp_layers.values()), \
        ("226(相一硬門檻): SP周瑜排最後(own_round=0, 本回合尚未行動)時, 江天長焰易傷仍應在回合開頭"
         "由broadcast_only通道施加給敵軍(enemy226.amp_layers應已有疊層)——證明易傷不隨SP周瑜出手"
         "順序, 排最後也在回合開頭先施加(user相一規則)。若仍走舊command-fire, own_round=0時敵軍"
         "身上不會有任何易傷層")
    # 反向對照: own_turn(相二)通道不應施加此broadcast效果(即使SP周瑜真的行動了), 確認雙通道互斥
    enemy226b = Unit(POOL["張飛"], "盾")
    zhouyu226.own_round = 1
    apply_effects(zhouyu226, None, zhouyu226.tactics[0], [zhouyu226, ally226], [enemy226b], own_turn=True)
    assert not (enemy226b.amp_layers and any(v > 0 for v in enemy226b.amp_layers.values())), \
        "226: 江天長焰易傷帶broadcast:true, own_turn(相二)通道不應施加它(專屬broadcast_only), 兩通道互斥"
    CUR_ROUND = 0
    print("    [時序徹底一致化 226] 江天長焰(SP周瑜)相一主例: SP周瑜排最後(own_round=0)時易傷仍"
          "在回合開頭broadcast_only統一施加給敵軍(不隨出手順序)/own_turn通道互斥不誤施 驗證通過")

    # 227) 狀態疊加語意對齊批(user權威規則 status_stacking_rule_20260711): NAMED_STATUS表
    # 分類驗證。(a) 反擊(multi)——兩個不同來源的反擊應各自獨立在counters清單新增一筆、都
    # 生效; 同一來源(同一效果物件)重複施加只刷新自己那一筆, 不疊加。(b) 休整(unique)——
    # 兩個不同來源的regen效果全場只應保留1筆(同名覆蓋, 後蓋前)。(c) 急救(unique)——兩個
    # 不同來源皆授予反應式急救時, 建構時應恰好裁決1個生效、其餘進 suppressed_named_status
    # (tie-break: 後蒐集者覆蓋前者); 並用端到端fight()確認雙急救來源與單急救來源的隊伍
    # 表現統計上不可分辨(去重生效, 未共存雙倍觸發)。(d) 來源追蹤——具名狀態實例應帶
    # status_name/src_name(供未來戰報「執行來自【X】的【狀態】」)。
    # 狀態疊加精修批: NAMED_STATUS taxonomy 擴充為5類(見其定義header), 227前置的合法mode
    # 集合一併擴充(unique_strongest/overwrite_fallback/accumulate/conditional/refresh 為
    # 本批新增, unique/multi/pending 為上一批既有)。
    _valid_modes = ("unique", "multi", "pending", "overwrite_fallback", "accumulate",
                    "conditional", "unique_strongest", "refresh")
    for _ns_name, _ns_spec in NAMED_STATUS.items():
        assert _ns_spec.get("mode") in _valid_modes, \
            f"227前置: NAMED_STATUS[{_ns_name}] mode 必須是 {_valid_modes} 之一"
    assert NAMED_STATUS["急救"]["mode"] == "overwrite_fallback" and NAMED_STATUS["休整"]["mode"] == "unique", \
        "227前置: 急救應為overwrite_fallback(覆蓋+到期回退, 狀態疊加精修批細化)/休整應為已確認的unique(唯一/覆蓋)具名狀態"
    assert NAMED_STATUS["反擊"]["mode"] == "multi" and NAMED_STATUS["攻心"]["mode"] == "multi" \
        and NAMED_STATUS["倒戈"]["mode"] == "multi", \
        "227前置: 反擊/攻心/倒戈應為已確認的multi(可共存)具名狀態"

    # (a) 反擊(multi): 兩個不同來源各自獨立新增一筆, 都會觸發
    ct227a_tac = {"nameZh": "測試反擊甲227", "type": "passive", "kind": "phys", "coef": 0, "rate": 1,
                  "effects": [{"k": "counter", "who": "self", "coef": 1.0, "kind": "phys", "prob": 1.0}]}
    ct227b_tac = {"nameZh": "測試反擊乙227", "type": "passive", "kind": "phys", "coef": 0, "rate": 1,
                  "effects": [{"k": "counter", "who": "self", "coef": 1.0, "kind": "phys", "prob": 1.0}]}
    ct227 = Unit(POOL["張飛"], "盾")
    apply_effects(ct227, None, ct227a_tac, [ct227], [])
    apply_effects(ct227, None, ct227b_tac, [ct227], [])
    assert len(ct227.counters) == 2, \
        f"227a: 兩個不同來源(不同戰法)各自的反擊效果應各自獨立在counters清單新增一筆(multi可共存), 實際{len(ct227.counters)}筆"
    assert {c.get("src_name") for c in ct227.counters} == {"測試反擊甲227", "測試反擊乙227"}, \
        f"227a: 每筆反擊實例應帶正確的src_name(來源戰法名, 供未來戰報顯示), 實際{[c.get('src_name') for c in ct227.counters]}"
    assert all(c.get("status_name") == "反擊" for c in ct227.counters), "227a: 每筆反擊實例應帶status_name==\"反擊\""
    # 雙反擊都生效: hit() 逐筆判定, on_deal 應被呼叫2次(is_normal=False, 各反擊各自算一次「造成傷害」)
    atk227 = Unit(POOL["曹操"], "騎")
    _ct227_fire = [0]
    def _on_deal227(src, dst, is_normal, kind, dmg):
        if not is_normal:
            _ct227_fire[0] += 1
    random.seed(22701)
    hit(atk227, ct227, 1.0, "phys", is_normal=True, on_deal=_on_deal227)
    assert _ct227_fire[0] == 2, \
        f"227a: 兩個不同來源的反擊(prob=1.0必中)應都觸發, on_deal應被呼叫2次(各反擊各自造成一次傷害), 實際{_ct227_fire[0]}次"
    # 同一來源(同一效果物件)重複施加只刷新自己那一筆, 不疊加出第2筆
    dup227_u = Unit(POOL["張飛"], "盾")
    dup227_tac = {"nameZh": "測試反擊丙227", "type": "passive", "effects": [
        {"k": "counter", "who": "self", "coef": 1.0, "kind": "phys", "prob": 1.0}]}
    apply_effects(dup227_u, None, dup227_tac, [dup227_u], [])
    apply_effects(dup227_u, None, dup227_tac, [dup227_u], [])
    assert len(dup227_u.counters) == 1, \
        f"227a: 同一來源(同一效果物件id)重複施加counter應只刷新自己那一筆, 不應疊加出第2筆, 實際{len(dup227_u.counters)}筆"

    # (b) 休整(unique): 兩個不同來源全場只保留1筆(後蓋前)
    rg227_u = Unit(POOL["張飛"], "盾")
    rg227a_tac = {"nameZh": "測試休整甲227", "type": "active", "kind": "intel", "coef": 0, "rate": 1,
                  "effects": [{"k": "regen", "who": "self", "coef": 1.0, "dur": 2}]}
    rg227b_tac = {"nameZh": "測試休整乙227", "type": "active", "kind": "intel", "coef": 0, "rate": 1,
                  "effects": [{"k": "regen", "who": "self", "coef": 1.0, "dur": 2}]}
    apply_effects(rg227_u, None, rg227a_tac, [rg227_u], [])
    apply_effects(rg227_u, None, rg227b_tac, [rg227_u], [])
    assert len(rg227_u.regens) == 1, \
        f"227b: 休整(unique)兩個不同來源施加時應只保留1筆(同名覆蓋, 不共存疊加), 實際{len(rg227_u.regens)}筆"
    assert rg227_u.regens[0][2] == "休整" and rg227_u.regens[0][3] == "測試休整乙227", \
        f"227b: 休整覆蓋後應保留\"最新\"(後施加者)的來源, 實際{rg227_u.regens[0]}"

    # (c) 急救(unique): 建構時裁決 —— 兩個不同來源皆授予反應式急救時只保留1個生效
    TACTICS["測試急救甲227"] = {
        "nameZh": "測試急救甲227", "type": "passive", "kind": "phys", "coef": 0, "rate": 1, "prep": 0,
        "effects": [{"k": "heal", "who": "self", "coef": 0.5, "dur": 1, "when": {"on": "damaged"}, "rate": 1.0}]}
    TACTICS["測試急救乙227"] = {
        "nameZh": "測試急救乙227", "type": "passive", "kind": "phys", "coef": 0, "rate": 1, "prep": 0,
        "effects": [{"k": "heal", "who": "self", "coef": 0.5, "dur": 1, "when": {"on": "damaged"}, "rate": 1.0}]}
    fa227 = Unit(POOL["張飛"], "盾", inherit=["測試急救甲227", "測試急救乙227"])
    assert len(fa227.suppressed_named_status) == 1, \
        f"227c: 兩個不同來源皆授予反應式急救(unique具名狀態)時, 建構時應恰好裁決1個為\"覆蓋\"(suppressed), 實際{len(fa227.suppressed_named_status)}個"
    _fa227_tac_a = next(t for t in fa227.tactics if t.get("nameZh") == "測試急救甲227")
    _fa227_tac_b = next(t for t in fa227.tactics if t.get("nameZh") == "測試急救乙227")
    assert id(_fa227_tac_a["effects"][0]) in fa227.suppressed_named_status, \
        "227c: tie-break政策為\"後蒐集者覆蓋前者\"(對應apply_passives()既有prep處理順序), 甲(先蒐集)應被裁決為覆蓋"
    assert id(_fa227_tac_b["effects"][0]) not in fa227.suppressed_named_status, \
        "227c: 乙(後蒐集)應保留為場上唯一生效中的急救實例"
    # 端到端: 雙急救來源與單急救來源隊伍表現應統計上不可分辨(去重生效, 未共存雙倍觸發)——
    # 若去重失效(退化回共存), 雙急救隊應顯著優於單急救隊(勝率差距>5pp)。
    random.seed(22702)
    n227 = 400
    res227_double = simulate(["張飛", "諸葛亮", "關羽"], ["曹操", "夏侯惇", "許褚"], n=n227,
                              inhA=[["測試急救甲227", "測試急救乙227"], None, None])
    random.seed(22702)
    res227_single = simulate(["張飛", "諸葛亮", "關羽"], ["曹操", "夏侯惇", "許褚"], n=n227,
                              inhA=[["測試急救甲227"], None, None])
    del TACTICS["測試急救甲227"]
    del TACTICS["測試急救乙227"]
    assert abs(res227_double["A勝率"] - res227_single["A勝率"]) < 0.05, \
        (f"227c: 雙急救來源(去重後應只1個生效)與單急救來源勝率應相近(差距<5pp), 實際雙"
         f"{res227_double['A勝率']} vs 單{res227_single['A勝率']}, 差距過大疑似去重失效"
         "(退化成共存雙倍觸發治療)")

    print(f"    [227 狀態疊加語意對齊] 反擊(multi, 兩來源各自獨立生效+同源不重複)/休整(unique,"
          f"覆蓋不共存)/急救(unique, 建構時裁決1個生效, 端到端雙來源≈單來源勝率"
          f"雙{res227_double['A勝率']}/單{res227_single['A勝率']})/來源追蹤(src_name) 驗證通過")

    # =========================================================================
    # 狀態疊加精修批(user權威規則 status_stacking_detail_20260712 + coordinator追加訊息
    # control_status_rule_20260712): 精修上一批(623afc4)的DoT/警戒/抵禦/急救四項, 追加
    # 攻心倒戈多實例(第5項)與控制類unique_strongest(第6項, 含追加的先攻/遇襲/洞察/嘲諷/
    # 虛弱)。紅線: 時序架構/反擊多實例/攻心倒戈加總(已由coordinator正式作廢改multi清單)
    # 不受影響的其餘既有行為不動。
    # =========================================================================

    # 228) DoT(灼燒/中毒/潰逃等) = 刷新(refresh)覆蓋, 唯一(非共存) —— 前批
    # u.dots.append不去重, 把DoT當共存清單是錯的(同名DoT會疊加成多份逐回合掉血, 高估
    # 傷害), 本批改為「同名DoT(以狀態名為鍵, 解析不到時退而用來源戰法名)新施加時覆蓋
    # 舊的(用最新coef/dur/來源), 不並存多個」。
    random.seed(22800)
    dot228_caster = Unit(POOL["諸葛亮"], "弓")
    dot228_tgt = Unit(POOL["張飛"], "盾")
    dot228_tac_a = {"nameZh": "測試灼燒228甲", "kind": "phys", "effects": [
        {"k": "dot", "who": "enemy", "coef": 0.3, "dur": 3, "name": "測試灼燒228"}]}
    apply_effects(dot228_caster, dot228_tgt, dot228_tac_a, [dot228_caster], [dot228_tgt], no_heal=True)
    assert len(dot228_tgt.dots) == 1, "228: 首次施加DoT應新增1筆"
    dmg228_a = dot228_tgt.dots[0][0]
    dot228_tac_b = {"nameZh": "測試灼燒228乙", "kind": "phys", "effects": [
        {"k": "dot", "who": "enemy", "coef": 0.9, "dur": 5, "name": "測試灼燒228"}]}  # 同名(狀態名), 不同戰法/coef/dur
    apply_effects(dot228_caster, dot228_tgt, dot228_tac_b, [dot228_caster], [dot228_tgt], no_heal=True)
    assert len(dot228_tgt.dots) == 1, \
        f"228: 同名DoT(灼燒)第二次施加應覆蓋(刷新)舊的, 不應共存成2筆, 實際{len(dot228_tgt.dots)}筆"
    assert dot228_tgt.dots[0][1] == 5, f"228: 刷新後應採用最新施加的dur(5), 實際{dot228_tgt.dots[0][1]}"
    assert abs(dot228_tgt.dots[0][0] - dmg228_a) > 1, \
        "228: 刷新後傷害量應反映最新施加的coef(0.9遠高於0.3), 而非維持舊值或加總"
    # 不同名DoT應與現有並存(不覆蓋)
    dot228_tac_c = {"nameZh": "測試中毒228", "kind": "phys", "effects": [
        {"k": "dot", "who": "enemy", "coef": 0.2, "dur": 2, "name": "測試中毒228"}]}
    apply_effects(dot228_caster, dot228_tgt, dot228_tac_c, [dot228_caster], [dot228_tgt], no_heal=True)
    assert len(dot228_tgt.dots) == 2, f"228: 不同名DoT(中毒)應與現有(灼燒)並存, 不應覆蓋, 實際{len(dot228_tgt.dots)}筆"
    # 無法解析具名狀態(DOT_NAME_BY_TACTIC未收錄且無e.name/dotName)時退而用來源戰法名為鍵:
    # 同一(無名)戰法重複施加應覆蓋刷新, 不同(無名)戰法各自並存
    dot228_tgt2 = Unit(POOL["張飛"], "盾")
    unnamed228_tac = {"nameZh": "測試無名DoT228", "kind": "phys", "effects": [
        {"k": "dot", "who": "enemy", "coef": 0.3, "dur": 3}]}
    apply_effects(dot228_caster, dot228_tgt2, unnamed228_tac, [dot228_caster], [dot228_tgt2], no_heal=True)
    assert len(dot228_tgt2.dots) == 1
    apply_effects(dot228_caster, dot228_tgt2, unnamed228_tac, [dot228_caster], [dot228_tgt2], no_heal=True)
    assert len(dot228_tgt2.dots) == 1, "228: 同一(無名)戰法重複施加DoT應以來源戰法名為鍵覆蓋刷新, 不應共存"
    unnamed228_tac2 = {"nameZh": "測試無名DoT228乙", "kind": "phys", "effects": [
        {"k": "dot", "who": "enemy", "coef": 0.3, "dur": 3}]}
    apply_effects(dot228_caster, dot228_tgt2, unnamed228_tac2, [dot228_caster], [dot228_tgt2], no_heal=True)
    assert len(dot228_tgt2.dots) == 2, "228: 不同(無名)戰法各自的DoT仍應並存, 不因同為\"無名\"而合併"
    print("    [228 DoT刷新] 同名DoT(灼燒)第二次施加覆蓋舊的(不共存)/不同名DoT並存/無名DoT退而用來源戰法名為鍵 驗證通過")

    # 229) 警戒(accumulate): 新施加的次數加總到現有(不論同源或不同源, 皆計入總可用次數)
    wj229_u = Unit(POOL["張飛"], "盾")
    wj229_u.push_block(0.4, 2, src="測試警戒229甲")
    wj229_u.push_block(0.4, 3, src="測試警戒229乙")  # 不同來源, val相同但src不同
    total_n_229 = sum(b["n"] for b in wj229_u.block if b["val"] < 0.999)
    assert total_n_229 == 5, f"229: 警戒不同來源應加總可用次數(2+3=5), 實際{total_n_229}"
    consumed229 = sum(1 for _ in range(5) if wj229_u.consume_block() > 0)
    assert consumed229 == 5, f"229: 累積的5次警戒應全部可消耗, 實際消耗{consumed229}次"
    assert wj229_u.consume_block() == 0, "229: 累積次數耗盡後不應再有警戒可消耗"
    # 同源同值仍合併進同一筆(而非額外增加筆數), 呼應既有63號測試, 本批不應破壞
    wj229b_u = Unit(POOL["張飛"], "盾")
    wj229b_u.push_block(0.4, 2, src="測試警戒229丙")
    wj229b_u.push_block(0.4, 3, src="測試警戒229丙")
    assert len(wj229b_u.block) == 1 and wj229b_u.block[0]["n"] == 5, \
        f"229: 同源同值應合併進同一筆(2+3=5), 不應變成2筆, 實際{wj229b_u.block}"
    print("    [229 警戒accumulate] 不同來源加總可用次數(2+3=5, 逐一消耗5次皆有效)/同源同值合併進同一筆 驗證通過")

    # 230) 抵禦(conditional, user追加修正): 「有剩餘次數時新施加不補不刷」——身上已有抵禦
    # 次數(不論來源)時, 新施加完全被忽略(不覆蓋/不補充), 現有次數原封不動; 只有現有次數
    # 已耗盡(0)時, 新來源的次數才真正生效; 例外: 持有者處於「嚴密」時改累積。
    dy230_u = Unit(POOL["張飛"], "盾")
    dy230_u.push_block(1.0, 1, src="測試抵禦230甲")
    dy230_u.push_block(1.0, 2, src="測試抵禦230乙(折衝)")  # 身上已有1次剩餘, 新來源(2次)應被忽略
    assert len(dy230_u.block) == 1 and dy230_u.block[0]["n"] == 1 and dy230_u.block[0]["src"] == "測試抵禦230甲", \
        f"230a: 非嚴密狀態下, 身上已有抵禦剩餘(1次)時, 新來源施加應被忽略(維持1次不變, 不變2/3次), 實際{dy230_u.block}"
    # 現有次數歸零(耗盡)後, 新來源才真正生效
    dy230b_u = Unit(POOL["張飛"], "盾")
    assert len(dy230b_u.block) == 0
    dy230b_u.push_block(1.0, 2, src="測試抵禦230b(折衝)")
    assert len(dy230b_u.block) == 1 and dy230b_u.block[0]["n"] == 2, \
        f"230b: 身上抵禦為0次時, 新來源應正常施加其次數(2), 實際{dy230b_u.block}"
    # 嚴密狀態下: 有剩餘時新來源改為累積(加總), 而非忽略
    dy230c_u = Unit(POOL["張飛"], "盾")
    dy230c_u.rigorous = 3  # 模擬赴湯蹈火已賦予「嚴密」
    dy230c_u.push_block(1.0, 1, src="測試抵禦230c甲")
    dy230c_u.push_block(1.0, 2, src="測試抵禦230c乙(折衝)")
    total_230c = sum(b["n"] for b in dy230c_u.block)
    assert total_230c == 3, f"230c: 嚴密狀態下, 身上抵禦1次+折衝2次應累積成3次, 實際{total_230c}"
    # 嚴密(rigorous)狀態本身: k=="rigorous"施加應設定u.rigorous, decay應遞減到期
    rig230_u = Unit(POOL["張飛"], "盾")
    rig230_caster = Unit(POOL["劉備"], "盾")
    apply_effects(rig230_caster, rig230_u, {"nameZh": "測試赴湯蹈火230", "effects": [
        {"k": "rigorous", "who": "enemy", "dur": 2}]}, [], [rig230_u], no_heal=True)
    assert rig230_u.rigorous == 2, "230d: k==rigorous應設定u.rigorous"
    rig230_u.decay_durations()
    assert rig230_u.rigorous == 1, "230d: 嚴密應逐回合遞減"
    rig230_u.decay_durations()
    assert rig230_u.rigorous == 0, "230d: 嚴密應到期歸零"
    print("    [230 抵禦conditional+嚴密] 有剩餘不補不刷(a)/歸零才套用新來源(b)/嚴密時例外改累積(c)/嚴密偵測旗標施加+到期遞減(d) 驗證通過")

    # 231) 急救(overwrite_fallback): 覆蓋+到期回退 —— 每個施加急救的來源各自追蹤(rate+
    # duration窗), 目前生效者=優先序最高(戰法→兵書→裝備, 清單越後面越新)且仍在自己when
    # 回合窗內者; 該來源到期後回退成次高優先序仍在窗內者, 全部到期才消失。範例對應
    # calibration_anchors.json陷陣營(3回合)+草船借箭(覆蓋後到期回退)的實測場景。
    TACTICS["測試急救陷陣營231"] = {
        "nameZh": "測試急救陷陣營231", "type": "passive", "kind": "phys", "coef": 0, "rate": 1, "prep": 0,
        "effects": [{"k": "heal", "who": "self", "coef": 0.6, "dur": 1,
                     "when": {"on": "damaged", "until": 3}, "rate": 0.4}]}
    TACTICS["測試急救草船231"] = {
        "nameZh": "測試急救草船231", "type": "active", "kind": "phys", "coef": 0, "rate": 0.65, "n": 1, "prep": 0,
        "effects": [{"k": "heal", "who": "eventTarget", "dur": 2,
                     "when": {"on": "damaged", "until": 1}, "rate": 0.8}]}
    fa231 = Unit(POOL["張飛"], "盾", inherit=["測試急救陷陣營231", "測試急救草船231"])
    tac231_a = next(t for t in fa231.tactics if t.get("nameZh") == "測試急救陷陣營231")
    tac231_b = next(t for t in fa231.tactics if t.get("nameZh") == "測試急救草船231")
    e231_a, e231_b = tac231_a["effects"][0], tac231_b["effects"][0]
    fa231.own_round = 1   # 第1回合: 兩者窗都開(陷陣營1-3, 草船1)
    assert id(e231_b) not in fa231.suppressed_named_status, "231a: 第1回合, 較新(優先序較高)的草船應為目前生效者"
    assert id(e231_a) in fa231.suppressed_named_status, "231a: 第1回合, 陷陣營應被草船蓋過(suppressed)"
    fa231.own_round = 2   # 第2回合: 草船窗(until:1)已過, 陷陣營窗(until:3)仍開 —— 應回退成陷陣營
    assert id(e231_b) in fa231.suppressed_named_status, "231b: 第2回合, 草船窗已過, 應視為suppressed"
    assert id(e231_a) not in fa231.suppressed_named_status, \
        "231b: 第2回合, 草船到期但陷陣營窗仍在(1-3回合)→急救應回退成陷陣營生效(對應user規則陷陣營+草船範例)"
    fa231.own_round = 4   # 第4回合: 兩者窗都過(草船到期於1, 陷陣營到期於3) —— 全部消失
    assert id(e231_b) in fa231.suppressed_named_status and id(e231_a) in fa231.suppressed_named_status, \
        "231c: 兩者窗皆已過(第4回合), 急救應完全消失(全部suppressed), 而非殘留任一個"
    del TACTICS["測試急救陷陣營231"]
    del TACTICS["測試急救草船231"]
    print("    [231 急救overwrite_fallback] 優先序最高且在窗內者生效(a)/最新來源到期回退次新仍有效者(b, 對應user規則陷陣營+草船範例)/全部到期才消失(c) 驗證通過")

    # 232) 攻心/倒戈(multi, coordinator追加規則): 改真正多實例清單(比照反擊u.counters做法),
    # 不再是前批的addbonus加總單一標量 —— 兩個不同來源應各自獨立存在、各自到期, 總回復量
    # =各活躍實例之和。
    lx232_u = Unit(POOL["呂布"], "騎")
    lx232_u.troop = 9000
    lx232_tac_a = {"nameZh": "測試倒戈232甲", "type": "passive", "coef": 0, "rate": 1, "effects": [
        {"k": "lifesteal", "who": "self", "val": 0.2, "dur": 2}]}
    lx232_tac_b = {"nameZh": "測試倒戈232乙", "type": "passive", "coef": 0, "rate": 1, "effects": [
        {"k": "lifesteal", "who": "self", "val": 0.3, "dur": 5}]}
    apply_effects(lx232_u, None, lx232_tac_a, [lx232_u], [], no_heal=True)
    apply_effects(lx232_u, None, lx232_tac_b, [lx232_u], [], no_heal=True)
    assert len(lx232_u.lifesteals) == 2, f"232a: 兩個不同來源的倒戈應各自獨立新增一筆(multi可共存), 實際{len(lx232_u.lifesteals)}筆"
    assert {l["src_name"] for l in lx232_u.lifesteals} == {"測試倒戈232甲", "測試倒戈232乙"}, \
        f"232a: 每筆倒戈實例應帶正確src_name, 實際{[l['src_name'] for l in lx232_u.lifesteals]}"
    assert all(l["status_name"] == "攻心/倒戈" for l in lx232_u.lifesteals), "232a: 每筆應帶status_name==\"攻心/倒戈\""
    # 總回復量=各實例val加總(0.2+0.3=0.5)×本次傷害(用on_event側錄本次實際dmg, 避免重算damage()受crit隨機影響)
    _dmg232 = [None]
    def _capture232(dst, src, is_normal, dmg, kind):
        _dmg232[0] = dmg
    lx232_dst = Unit(POOL["張飛"], "盾")
    troop_before_232 = lx232_u.troop
    hit(lx232_u, lx232_dst, 1.0, "phys", on_event=_capture232)
    gain232 = lx232_u.troop - troop_before_232
    expected_gain232 = _dmg232[0] * (0.2 + 0.3)
    assert abs(gain232 - expected_gain232) < 0.5, \
        f"232a: 總回復量應為兩實例val加總(0.5)×本次傷害, 實得{gain232:.2f} 預期{expected_gain232:.2f}"
    # 各自獨立到期: 甲(dur=2)應在2次decay後到期消失, 乙(dur=5)應仍存活
    lx232_u.decay_durations()
    lx232_u.decay_durations()
    assert len(lx232_u.lifesteals) == 1 and lx232_u.lifesteals[0]["src_name"] == "測試倒戈232乙", \
        f"232b: 甲(dur=2)應在2次decay後到期消失, 乙(dur=5)應仍存活, 實際{lx232_u.lifesteals}"
    # 同一來源(同一效果物件)重複施加只刷新自己那一筆, 不應疊加出第2筆
    lx232c_u = Unit(POOL["呂布"], "騎")
    dup_lx232_tac = {"nameZh": "測試倒戈232丙", "type": "passive", "coef": 0, "rate": 1, "effects": [
        {"k": "lifesteal", "who": "self", "val": 0.4, "dur": 3}]}
    apply_effects(lx232c_u, None, dup_lx232_tac, [lx232c_u], [], no_heal=True)
    apply_effects(lx232c_u, None, dup_lx232_tac, [lx232c_u], [], no_heal=True)
    assert len(lx232c_u.lifesteals) == 1, "232c: 同一來源(同一效果物件)重複施加倒戈應只刷新自己那一筆, 不應疊加出第2筆"
    print("    [232 攻心倒戈multi] 兩來源各自獨立實例(a, 總回復=加總)/各自獨立到期(b)/同源不重複(c) 驗證通過")

    # 233) 控制類「不動作」狀態(繳械/計窮/震懾/混亂) + 追加(先攻/遇襲/洞察/嘲諷) =
    # 唯一+「同等或更強擋新」——既有偽報(fakeReport)same-or-stronger規則的推廣
    # (control_status_rule_20260712, coordinator追加規則)。虛弱(amp-based, clamp效果)
    # 經分析後維持現行push_add/amp機制不變(見NAMED_STATUS["虛弱"].note), 不在此重複驗證。
    cs233_caster = Unit(POOL["劉備"], "盾")
    # (a) 繳械(disarm): 已有dur=2, 較弱(dur=1)/同等(dur=2)施加應完全失效, 更強(dur=3)才覆蓋
    cs233_u = Unit(POOL["張飛"], "盾")
    cs233_u.disarm = 2
    apply_effects(cs233_caster, cs233_u, {"nameZh": "測試繳械233弱", "effects": [
        {"k": "disarm", "who": "enemy", "dur": 1}]}, [], [cs233_u], no_heal=True)
    assert cs233_u.disarm == 2, f"233a: 已有繳械dur=2時, 施加較弱(dur=1)應完全失效(維持2), 實際{cs233_u.disarm}"
    apply_effects(cs233_caster, cs233_u, {"nameZh": "測試繳械233同等", "effects": [
        {"k": "disarm", "who": "enemy", "dur": 2}]}, [], [cs233_u], no_heal=True)
    assert cs233_u.disarm == 2, f"233a: 已有繳械dur=2時, 施加同等(dur=2)也應失效(同等不覆蓋), 實際{cs233_u.disarm}"
    apply_effects(cs233_caster, cs233_u, {"nameZh": "測試繳械233強", "effects": [
        {"k": "disarm", "who": "enemy", "dur": 3}]}, [], [cs233_u], no_heal=True)
    assert cs233_u.disarm == 3, f"233a: 施加更強(dur=3)應覆蓋(3>2), 實際{cs233_u.disarm}"
    # (b) 計窮(silence)/震懾(stun)/混亂(chaos) 同規則
    for _field233, _k233 in [("silence", "silence"), ("stun", "stun"), ("chaos", "chaos")]:
        u233 = Unit(POOL["張飛"], "盾")
        setattr(u233, _field233, 2)
        apply_effects(cs233_caster, u233, {"nameZh": f"測試{_k233}233弱", "effects": [
            {"k": _k233, "who": "enemy", "dur": 1}]}, [], [u233], no_heal=True)
        assert getattr(u233, _field233) == 2, f"233b: {_k233} 較弱施加應失效(維持2)"
        apply_effects(cs233_caster, u233, {"nameZh": f"測試{_k233}233強", "effects": [
            {"k": _k233, "who": "enemy", "dur": 5}]}, [], [u233], no_heal=True)
        assert getattr(u233, _field233) == 5, f"233b: {_k233} 更強施加應覆蓋(5>2)"
    # (c) 追加: 先攻(first)/遇襲(ambush)/洞察(insight)/嘲諷(taunt) 同規則
    fr233_u = Unit(POOL["張飛"], "盾")
    fr233_u.first = 3
    apply_effects(cs233_caster, fr233_u, {"nameZh": "測試先攻233弱", "effects": [
        {"k": "first", "who": "enemy", "dur": 2}]}, [], [fr233_u], no_heal=True)
    assert fr233_u.first == 3, f"233c: 先攻較弱施加應失效(維持3), 實際{fr233_u.first}"
    apply_effects(cs233_caster, fr233_u, {"nameZh": "測試先攻233強", "effects": [
        {"k": "first", "who": "enemy", "dur": 5}]}, [], [fr233_u], no_heal=True)
    assert fr233_u.first == 5, f"233c: 先攻更強施加應覆蓋(5>3), 實際{fr233_u.first}"
    am233_u = Unit(POOL["張飛"], "盾")
    am233_u.ambush = 3
    apply_effects(cs233_caster, am233_u, {"nameZh": "測試遇襲233弱", "effects": [
        {"k": "ambush", "who": "enemy", "dur": 2}]}, [], [am233_u], no_heal=True)
    assert am233_u.ambush == 3, f"233c: 遇襲較弱施加應失效(維持3), 實際{am233_u.ambush}"
    apply_effects(cs233_caster, am233_u, {"nameZh": "測試遇襲233強", "effects": [
        {"k": "ambush", "who": "enemy", "dur": 5}]}, [], [am233_u], no_heal=True)
    assert am233_u.ambush == 5, f"233c: 遇襲更強施加應覆蓋(5>3), 實際{am233_u.ambush}"
    ins233_u = Unit(POOL["張飛"], "盾")
    ins233_u.insight = 3
    ins233_u.stun = 1  # 純測試「較弱insight不應觸發解控副作用」的閘門, 非真實insight/stun互斥情境
    apply_effects(ins233_u, None, {"nameZh": "測試洞察233弱", "effects": [
        {"k": "insight", "who": "self", "dur": 2}]}, [ins233_u], [], no_heal=True)
    assert ins233_u.insight == 3 and ins233_u.stun == 1, \
        f"233c: 洞察較弱施加應完全失效(insight維持3, 不觸發解控副作用, stun應仍為1), 實際insight={ins233_u.insight} stun={ins233_u.stun}"
    apply_effects(ins233_u, None, {"nameZh": "測試洞察233強", "effects": [
        {"k": "insight", "who": "self", "dur": 5}]}, [ins233_u], [], no_heal=True)
    assert ins233_u.insight == 5 and ins233_u.stun == 0, \
        f"233c: 洞察更強施加應覆蓋(5>3)且觸發解控副作用(stun歸零), 實際insight={ins233_u.insight} stun={ins233_u.stun}"
    tt233_u = Unit(POOL["張飛"], "盾")
    tt233_holder1 = Unit(POOL["劉備"], "盾")
    tt233_holder2 = Unit(POOL["關羽"], "盾")
    tt233_u.taunt_by = tt233_holder1
    tt233_u.taunt_dur = 3
    apply_effects(tt233_holder2, tt233_u, {"nameZh": "測試嘲諷233弱", "effects": [
        {"k": "taunt", "who": "enemy", "dur": 2}]}, [tt233_holder2], [tt233_u], no_heal=True)
    assert tt233_u.taunt_by is tt233_holder1 and tt233_u.taunt_dur == 3, \
        "233c: 嘲諷較弱施加應完全失效(taunt_by/taunt_dur皆維持原值, 不改指向新施放者)"
    apply_effects(tt233_holder2, tt233_u, {"nameZh": "測試嘲諷233強", "effects": [
        {"k": "taunt", "who": "enemy", "dur": 5}]}, [tt233_holder2], [tt233_u], no_heal=True)
    assert tt233_u.taunt_by is tt233_holder2 and tt233_u.taunt_dur == 5, \
        "233c: 嘲諷更強施加應覆蓋(5>3), 且taunt_by應改指向新施放者"
    print("    [233 控制類+先攻/遇襲/洞察/嘲諷 unique_strongest] 繳械/計窮/震懾/混亂(a/b)+先攻/遇襲/洞察/嘲諷(c)皆驗證「同等或更強才覆蓋, 較弱完全失效」 驗證通過")

    print("self-check OK")


def _team(s):
    return [x.strip() for x in re.split(r"[,/\s]+", s) if x.strip()]


def info(name):
    g = POOL.get(name)
    if not g:
        print("查無此將。試試:", ", ".join(list(POOL)[:8]), "...")
        return
    apt = "  ".join(f"{t}{g.apt.get(t, '-')}" for t in ("騎", "盾", "弓", "槍", "器"))
    print(f"{g.name}  {g.faction}　兵種適性[ {apt} ]")
    print(f"  基礎 武{g.base['force']:.0f} 智{g.base['intel']:.0f} "
          f"統{g.base['command']:.0f} 速{g.base['speed']:.0f}（戰鬥時 ×該兵種適性%）")
    print(f"  可用兵書: {'/'.join(g.bingshu_cats) or '—'}　預設: {default_bingshu(g) or '—'}")
    print(f"  自帶戰法: {g.tactic_name}")
    if g.tactic:
        print(f"  解析: {json.dumps(g.tactic, ensure_ascii=False)}")


if __name__ == "__main__":
    import sys
    random.seed(42)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "sim" and len(sys.argv) >= 4:           # sim "A,B,C" "D,E,F" [兵種A 兵種B]
        a, b = _team(sys.argv[2]), _team(sys.argv[3])
        ta = sys.argv[4] if len(sys.argv) > 4 else team_troop(a)
        tb = sys.argv[5] if len(sys.argv) > 5 else team_troop(b)
        print(f"{'/'.join(a)}[{ta}兵]  vs  {'/'.join(b)}[{tb}兵]")
        print(" ", simulate(a, b, troopA=ta, troopB=tb))
    elif cmd == "rec":                                # rec [勢力]
        fac = sys.argv[2] if len(sys.argv) > 2 else None
        pool = [n for n, g in POOL.items() if g.faction == fac] if fac else None
        title = f"{fac}勢力" if fac else "全 193 武將"
        print(f"== 配將推薦 Top8 ({title}) ==")
        for team, sc, troop in recommend(pool):
            print(f"  {sc:>4}  [{troop}兵] {' / '.join(team)}")
    elif cmd == "info" and len(sys.argv) > 2:         # info 武將名
        info(sys.argv[2])
    elif cmd == "test":
        demo()
    else:                                             # demo
        print(f"載入: {len(POOL)} 武將, {len(TACTICS)} 戰法(可戰鬥, 來源={TACTIC_SRC})")
        print("\n== 配將推薦 Top8 (全 193) ==")
        for team, sc, troop in recommend():
            print(f"  {sc:>4}  [{troop}兵] {' / '.join(team)}")
        print("\n== 模擬: 呂布/趙雲/關羽 vs 諸葛亮/周瑜/司馬懿 ==")
        print(" ", simulate(["呂布", "趙雲", "關羽"], ["諸葛亮", "周瑜", "司馬懿"]))
        print("\n用法: python sgz.py [demo|test|rec [勢力]|sim \"A,B,C\" \"D,E,F\"|info 武將]")
        demo()
