# -*- coding: utf-8 -*-
"""
一次性腳本: 用 data/tactics.json 的 effectText(含真實數值) 回填
docs/data/tactics_parsed.json 的 coef/rate/n/nMax/dur/heal-coef。

原則(保守): 只修數值欄位, 不重寫 effects 的語意結構(k/who 等 LLM 既有判斷保留)。
抽不到就不動, 並標記 "_est": true。

新增: 依 effectText 關鍵字偵測「計窮/繳械/震懾/洞察」等控制/免控語意:
- 既有 k="stun"(who=enemy) 效果, 若文字段落「恰好命中其中一種」關鍵字(計窮/技窮→silence;
  繳械→disarm; 震懾→維持 stun), 精分類為更準確的原語; 混雜多種關鍵字時保守不動(維持 stun)。
- 洞察(免疫控制): 僅在 effectTarget 精確等於「自己」時, 才新增一個 k="insight" 效果
  (無法從既有 15 原語表達)。「先攻」語意因目標對象(自己/我軍主將/我軍全體)混雜,
  regex 無法安全判斷, 保守跳過, 留給引擎端(item 5)人工判斷或後續 LLM 精解。

批2.5: 受屬性影響縮放 + rateup + overrides ---------------------------------
- 受屬性影響縮放: effectText 含「受X影響」(X=智力/武力/統率/統帥/速度/魅力)的 heal/amp/
  mitig/stat 子句, 白名單標記對應 effect 物件的 scale="intel"|"force"|"command"|"speed"|
  "charm"(引擎端依施放者戰鬥內即時素質縮放, 見 engine.js/sgz.py 的 SCALE())。傷害 coef
  的「受X影響」已經由 kind(phys用武力/intel用智力) 天然建模, 不重複標記; kind 與文字提及
  的屬性不一致的罕見案例(如 phys 主戰法內嵌一段 intel 的灼燒/謀略子傷害) 保守跳過。
  dot 效果的傷害同樣經由 damage() 用 caster 的 force/intel 天然建模, 不需額外 scale。
  詳細判定見 SCALE_MAP/_scale_candidates_for_match()。
- rateup: 「主動戰法(的)發動機率提高X%→Y%」句型 → 新增 k="rateup" 效果(who=self, dur=99,
  val=Y/100), 見 RATEUP_RE。
- overrides: 讀 docs/data/tactics_overrides.json, 在抽取前先用其 effectText/type 覆蓋
  raw_by_name 對應戰法(見 _apply_overrides())。invalid=true 的條目(如宜城之志, user 已確認
  遊戲內無此戰法) 直接把 parsed 的 type 設 "none"(排除戰鬥與選單), 略過其餘抽取步驟。

用法: python reparse_effects.py
輸出: 就地覆寫 docs/data/tactics_parsed.json, 並印出回填報告。
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW_PATH = os.path.join(ROOT, "data", "tactics.json")
PARSED_PATH = os.path.join(ROOT, "docs", "data", "tactics_parsed.json")
OVERRIDES_PATH = os.path.join(ROOT, "docs", "data", "tactics_overrides.json")
CORRECTIONS_PATH = os.path.join(ROOT, "docs", "data", "tactic_corrections.json")

DMG_RANGE = re.compile(r"傷害率\s*(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)\s*%?")
DMG_SINGLE = re.compile(r"傷害率\s*(\d+(?:\.\d+)?)\s*%")
HEAL_RANGE = re.compile(r"治療率\s*(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)\s*%?")
HEAL_SINGLE = re.compile(r"治療率\s*(\d+(?:\.\d+)?)\s*%")
DUR_RE = re.compile(r"持續\s*(\d+(?:\.\d+)?)\s*(?:[-~]\s*(\d+(?:\.\d+)?)\s*)?回合")
TARGET_N_RANGE = re.compile(r"(\d+)\s*[-~]\s*(\d+)\s*人")
TARGET_N_SINGLE = re.compile(r"(\d+)\s*人")
# 指揮/被動戰法內文常見的「條件觸發機率」(如「有 25%→35% 機率…」), 與戰法整體
# activationRate(常為 1, 代表該戰法本身無外部次數限制)是兩件事: 真正逐回合的
# 觸發機率藏在 effectText 裡。只在「有明確傷害 coef 且 activationRate==1」時才需要,
# 否則沿用既有 rate, 避免對純被動增益類戰法做不必要的改動。
# 機率同義詞: 機率/概率/幾率 都常見(繁簡混雜的社群文本)。
INLINE_RATE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:→|~|-)?\s*(\d+(?:\.\d+)?)?\s*%?\s*(?:的)?\s*(?:機率|概率|幾率)")

SILENCE_KW = ("計窮", "技窮")
DISARM_KW = ("繳械",)
STUN_KW = ("震懾",)
INSIGHT_KW = ("洞察",)
# 機率關鍵字後緊跟這些詞 = 那是「狀態自身的機率」(規避率/會心率…), 不是傷害觸發機率
RATE_NOT_TRIGGER = ("規避", "會心", "奇謀")
# 先攻(優先行動): 只認「語意明確的直接授予句型」, 對象限 自己/我軍主將/我軍全體。
# 條件式(坐斷東南隨機獲得)/狀態引用(臥薪嘗膽依狀態數)/含糊敘述(蓄勢待發)一律跳過。
FIRST_GRANT_RE = re.compile(
    r"使我軍全體獲得先攻|我軍全體戰鬥前\s*\d+\s*回合獲得先攻|使我軍主將獲得先攻"
    r"|使自己及友軍單體獲得先攻|使自己獲得先攻|自身獲得先攻")
FIRST_DUR_RE = re.compile(r"戰鬥前\s*(\d+)\s*回合")

# ---------------------------------------------------------------------------
# 批2: 條件觸發(when) + 新原語(taunt/shield/dodge/surehit)
#
# 條件觸發/新原語語意在 384 條 effectText 裡幾乎每條都夾雜多個子句(不同回合窗口、
# 不同觸發條件混在同一段話裡), 通用 regex 容易對到錯的子句(把「always-on」的效果
# 錯套上某個無關子句的回合窗口, 或把「必中/規避」拿來描述另一個效果的說明文字誤標)。
# 沿用本檔一貫的保守原則: 逐條人工核對 effectText 後, 只用「白名單」方式標記語意
# 100%明確、且該戰法的 coef/effects 整體恰好對應同一子句(不會誤套到不相關子句)的
# 案例; 其餘一律跳過, 保留給後續 LLM 精解。跳過清單見腳本輸出。
# ---------------------------------------------------------------------------

# --- when.rounds/from/until: coef+effects 整體只對應同一個回合窗口子句 --------
# 「戰鬥第2回合開始」dot/群體傷害才開始生效(prep 時的 dur 只能表示「維持多久」,
# 無法表示「延後開始」, 是既有 15 原語表達不到的缺口, 需要新的 when.from)。
WHEN_FROM = {
    "興雲佈雨": 2, "興雲布雨": 2,          # 戰鬥第2回合開始, 使敵軍全體進入水攻狀態(dot/amp 皆對應此子句)
}
# 「戰鬥前N回合」的 until 語意多數已由既有 dur=N (prep 時套用一次, N 回合後到期) 天然覆蓋,
# 不需要額外 when; WHEN_UNTIL 僅保留給未來若需要嚴格重跑驗證用, 目前無新增案例。
WHEN_UNTIL = {}

# --- when.on: 反應式觸發, coef+effects 整體只對應「受到普通攻擊時/受到傷害後」子句 ---
ON_ATTACKED = {
    "氣凌三軍": "受到普通攻擊時對攻擊者進行一次反擊",
    "後發制人": "受到普通攻擊時對攻擊者進行一次反擊",
    "眾動萬計": "受到普通攻擊時,45%機率對攻擊來源造成兵刃傷害並使其下次傷害降低",
    "魅惑": "自身受到普通攻擊時,機率使攻擊者進入計窮等狀態的一種",
}
ON_DAMAGED = {
    "剛勇無前": "受到兵刃傷害後,下回合行動時提高會心並使下一擊傷害提高",
}

# --- rate 白名單覆寫: on 觸發類戰法的內文機率(activationRate=1 但實際觸發率藏在
# effectText, 且 coef=0 走不到既有 item 2 的 inline rate 抽取路徑)。
# 魅惑「有 22.5%→45% 機率使攻擊者進入…」滿級 0.45; 不覆寫的話 rate=1 =
# 每次被普攻 100% 觸發控制, 嚴重高估(比照 眾動萬計 rate=0.45 的既有處理)。
RATE_OVERRIDE = {"魅惑": 0.45}

# --- taunt(嘲諷): 中招敵軍強制普攻/單體戰法指向施放者。只認「嘲諷」關鍵字;
# 「偽報」在本庫實際語意因戰法而異(當鋒摧決定義成「禁用被動/指揮戰法」, 並非目標
# 改向), 與任務描述的「隨機目標」假設不符, 保守全部跳過偽報, 只標「嘲諷」。
# 威風凜凜「嘲諷…或繳械…持續2回合」擇一施加: disarm 那半既已標記, taunt 那半
# 同句同確定性一併補標(引擎近似為兩者皆施加, 略強於實際擇一, 可接受)。
# 定謀貴決 保守跳過: 原文「使敵軍兵力最高的武將嘲諷我軍全體」是反向嘲諷——我方
# 全體被迫集火該敵將(=標記/集火語意), 與 taunt 原語(中招者攻擊施放者)方向恰好
# 相反, 硬標會把集火目標弄反; 等未來加「標記集火」原語再處理。
# ADD(不取代既有 stun/redirect 等效果, 避免動到既有平衡/回歸): 額外附加 taunt 效果。
TAUNT_DUR = {
    "固若金湯": 2, "守而必固": 4, "江東猛虎": 2, "威風凜凜": 2,
    "獨行赴鬥": 2, "益其金鼓": 1, "唇槍舌戰": 1, "挑釁": 1,
}

# --- dodge(規避): 語意明確標出「機率規避」且無其他子句混雜共用同一效果格。
# 金丹秘術: 既有 mitig(val=0.35) 其實是誤將「規避可免疫傷害」近似成部分減傷,
# 數值(0.35)恰好對應規避機率, 這裡連 k 一併改正(mitig -> dodge)較準確;
# 因利制權: 「獲得12.5%→25%規避…持續1回合」effects=[] 空的, 純新增, prob 從原文抽;
# 士別三日: 「戰鬥前3回合…獲得15%→30%機率規避效果」prob 從原文抽, dur=3(前3回合);
# 蓄勢待發: 「每回合機率賦予我軍群體規避狀態」語意明確但原文無數值(概要式文本),
#   prob 用引擎預設 0.2 並記錄假設, dur=1(每回合重新賦予的近似)。
DODGE_ADD = {
    "因利制權": {"prob_kw": "規避", "dur": 1, "who": "ally"},
    "士別三日": {"prob_kw": "規避", "dur": 3, "who": "self"},
    "蓄勢待發": {"prob": 0.2, "dur": 1, "who": "ally"},   # 原文無數值, 取引擎預設 0.2(_假設_)
}
DODGE_FIX_MITIG = ("金丹秘術",)                            # 既有 mitig 誤用, 改標成 dodge(沿用原 val/dur)

# --- surehit(必中): 語意為施放者(或指定我方單體)明確獲得必中, 且不是「必中/洞察/
# 先攻/連擊隨機一種」這類多選一的模糊描述(坐斷東南等跳過)。
# 驍健神行: 「使自身獲得必中…持續2回合」;
# 國士將風: 「戰鬥前3回合, 使自己及友軍單體獲得先攻…和必中…」同句 first 已標 dur=3, 必中比照;
# 萬軍取將: 「自身獲得必中及破陣」概要式文本無持續回合, dur=1 保守假設(單回合)。
SUREHIT_ADD = {"驍健神行": 2, "國士將風": 3, "萬軍取將": 1}

# --- shield(護盾): 需要可解析的具體吸收量(固定值或兵力%), 全庫僅「赴湯蹈火」提及
# 護盾但敘述是「多層抵禦疊加機制」, 無具體數字可抽, 保守跳過(見報告)。
SHIELD_ADD = {}

# ---------------------------------------------------------------------------
# 批2.5 item1: 受屬性影響縮放(scale) 白名單
#
# effectText 176 處「受X影響」(X=智力/武力/統率/統帥/速度/魅力), 語意是該子句效果量隨
# 施放者對應屬性縮放。逐條核對後, 只在滿足下列條件時標記:
#   - 該子句對應 heal/amp/mitig/stat 其中一種既有 effect(dot 的傷害已由 damage() 的
#     kind=intel/phys 天然用 caster 屬性建模, 不需額外標記; 傷害coef同理已用kind建模)。
#   - 「受X影響」的 X 不是機率/發動率(那是觸發機率縮放, 15原語無對應概念, 跳過)。
#   - 對應到「唯一」或「結構相同(可安全複用同一scale)」的 effect 物件, 無歧義。
# 多個候選 effect 且彼此結構不同(如 竭忠盡智/虛實奇謀 的機率類子句誤觸發、扶危定傾的
# 「受自身最高屬性影響」無法對應單一固定屬性) 一律保守跳過, 詳見腳本輸出的 skip 清單。
# 值格式: {戰法名: [(effects索引, scale屬性), ...]}
SCALE_PLAN = {
    "一力拒守": [(0, "force")],
    "乘敵不虞": [(1, "intel")],
    "亂世奸雄": [(0, "intel"), (1, "intel")],
    "仁德載世": [(0, "intel"), (1, "intel")],
    "以寡敵眾": [(0, "force")],
    "以逸待勞": [(0, "intel"), (1, "intel")],
    "傲睨王侯": [(1, "intel")],
    "八門金鎖陣": [(0, "intel")],
    "兵無常勢": [(0, "intel")],
    "刮骨療毒": [(0, "intel")],
    "千里饋糧": [(0, "force")],      # 「使自身獲得急救狀態(受武力影響)」→ heal效果; stat(提高武力)無scale措辭
    "包紮": [(0, "intel")],
    "國士將風": [(0, "speed")],
    "圍師必闕": [(0, "command")],
    "坐守孤城": [(0, "intel")],
    "垂心萬物": [(1, "intel")],
    "天下無雙": [(0, "force")],
    "奇兵間道": [(0, "force")],      # 「主動戰法造成的傷害提高15%→30%（受武力影響）」= idx0(val0.3,dur4); idx1(倒戈0.45)無scale措辭
    "奇計良謀": [(0, "speed"), (1, "speed")],
    "奪魂挾魄": [(0, "intel"), (1, "intel"), (2, "intel"), (3, "intel")],  # 偷取武智速統各一效果, 同句「受智力影響」可安全複用
    "嬰城自守": [(0, "intel")],
    "定軍斬將": [(0, "force")],
    "密計誅逆": [(0, "command")],
    "掣刀斫敵": [(0, "force")],
    "揮兵謀勝": [(1, "intel")],
    "援救": [(0, "intel")],
    "搦戰群雄": [(1, "force")],
    "撫輯軍民": [(0, "intel"), (1, "command")],
    "擊其惰歸": [(0, "command"), (1, "command")],
    "斂眾而擊": [(0, "force")],
    "智計": [(0, "intel"), (1, "intel")],       # 「武力、智力降低19→38（受智力影響）」兩個stat同句可安全複用
    "暫避其鋒": [(0, "intel")],
    "杯蛇鬼車": [(0, "intel")],
    "機鑑先識": [(0, "intel")],
    "水淹七軍": [(1, "force")],
    "沉斷機謀": [(1, "intel")],      # 「統率、智力降低15%→30%（受智力影響）」→ stat效果; idx0 amp為同句近似, 依審查僅標stat
    "江天長焰": [(0, "intel")],
    "江東小霸王": [(1, "force")],
    "江東猛虎": [(2, "force")],
    "深藏若虛": [(0, "intel"), (1, "intel")],
    "深謀遠慮": [(0, "intel")],
    "淑懿之德": [(0, "intel"), (1, "intel")],   # 「智力、統率提升1.5%→3%（受智力影響）」兩個stat同句可安全複用
    "淵然難測": [(0, "command")],
    "濟貧好施": [(1, "intel"), (2, "intel")],
    "火神英風": [(2, "force")],
    "焰逐風飛": [(1, "intel")],
    "百計多謀": [(1, "intel")],
    "眾志成城": [(0, "intel")],
    "破軍威勝": [(0, "force")],
    "破陣摧堅": [(0, "force"), (1, "force")],   # 「統率、智力降低40→80點（受武力影響）」兩個stat同句可安全複用
    "箕形陣": [(0, "force")],
    "義心昭烈": [(0, "intel")],
    "肉身鐵壁": [(1, "command")],
    "胡笳餘音": [(0, "intel"), (1, "intel")],
    "草船借箭": [(0, "command")],
    "蕙質蘭心": [(2, "intel"), (3, "intel")],
    "藏器待時": [(0, "command")],
    "藤甲兵": [(0, "command")],
    "虎痴": [(0, "force")],
    "計定謀決": [(2, "intel")],
    "詐降": [(1, "intel")],
    "誘敵深入": [(0, "intel")],
    "謙讓": [(0, "intel")],
    "金丹秘術": [(1, "intel")],
    "金城湯池": [(0, "intel")],
    "錦囊妙計": [(1, "intel")],
    "鎮扼防拒": [(1, "intel")],
    "長驅直入": [(1, "force")],
    "陷陣營": [(2, "intel")],
    "離月": [(0, "intel"), (1, "intel")],
    "青囊": [(1, "intel")],
    "青州兵": [(0, "force")],
    "顧盼生姿": [(0, "intel"), (1, "intel")],   # 「偷取敵軍單體18→36點智力及武力（受智力影響）」兩個stat同句可安全複用
    "飛沙走石": [(0, "intel")],
    "魚鱗陣": [(1, "command")],
}

# --- 批2.5 item2: rateup(主動戰法發動機率提升) --------------------------------
# 「(自己/自身/友軍單體)主動戰法(的)發動機率|概率|幾率提高|提升X%→Y%」句型 →
# k="rateup", val=Y/100(取升滿值), who: 句中出現「友軍/我軍」等他指對象時為 ally, 否則
# (自己/自身/無主語, 即預設施放者自身) 為 self。
# dur: 從匹配位置後方近距離(30字內, 同一句語境)抽「持續X回合」寫入; 抽不到 = 常駐句型
# (如白眉「戰鬥中…」無持續字樣) 取 99。引擎端 rateup 走 adds 陣列, tick() 每回合遞減
# 自然到期, 無需額外處理。
# 已知案例: 白眉(6%→12%取0.12, 常駐→99)、先成其慮(持續1回合)、獅子奮迅(持續2回合)、
# 進言(持續2回合, who=ally)。宜城之志經 user 確認為幽靈條目, 與白眉是不同戰法。
RATEUP_RE = re.compile(
    r"(?P<who>自己|自身|友軍單體|友軍)?主動戰法(?:的)?發動(?:機率|概率|幾率)"
    r"(?:提高|提升)\s*(?:\d+(?:\.\d+)?\s*%\s*(?:→|~|-)\s*)?(?P<val>\d+(?:\.\d+)?)\s*%")
RATEUP_DUR_RE = re.compile(r"持續\s*(\d+)\s*回合")

# --- 批2.5 item3補: overrides 落地手動白名單 -----------------------------------
# 桃園結義 的查證文全用概數措辭(「治療率約36%」「傷害率約46%」), 通用 regex(傷害率\s*\d)
# 對「約」抽不到, 若不處理會靜默落空(coef:0/effects:[] 無任何效果)。依查證文手動落地:
# 每回合 20%→40% 機率三選一(治療單體/對智力最低謀略傷害/對統率最低兵刃傷害)。近似:
# coef=0.46(傷害擇一, kind 沿用既有 phys), rate=0.4(取滿級), 另加 heal 效果 coef=0.36
# (受智力影響→scale)。「三選一」機制引擎無法表達, 近似成「傷害擲骰+治療每回合」略強於
# 實際; 查證 confidence=med, 依原始任務規格保留 _est 標記(force_est)。
MANUAL_FILL = {
    "桃園結義": {
        "coef": 0.46, "rate": 0.4, "n": 1,
        "effects_add": [{"k": "heal", "who": "ally", "coef": 0.36, "dur": 1, "scale": "intel"}],
        "force_est": True,
    },
}

# --- 批6: chargeup(突擊戰法發動機率提升) 白名單 -------------------------------
# 「突擊戰法發動率|機率提高X%→Y%」句型: 取升滿值 val=Y/100。who/dur 逐條人工核對原文
# (與 rateup 同樣保守原則: 全庫只白名單標記語意 100% 明確的案例, 其餘跳過)。
#
# 落地(2個戰法):
# - 虎豹騎(data/tactics.json「將騎兵進階為天下驍銳的虎豹騎」2020-04-01重做版): 「戰鬥前3回合,
#   我軍全體突擊戰法發動率提高5%→10%」→ who=ally, val=0.10, dur=3。另「若曹純統領時, 提升的
#   發動機率額外受武力影響」→ leaderBonus(見 engine.js/sgz.py 的 chargeup 特例, 二次曲線錨點
#   docs/data/calibration_anchors.json → hubaoqi_caochun)。
# - 陷陣突襲: 「自身突擊戰法發動機率提高7.5%→15%」→ who=self, val=0.15, 無持續字樣(「戰鬥中,
#   自己普通攻擊…」整段常駐句型, 比照 rateup 白眉的常駐慣例) dur=99。
#
# 跳過(2個戰法, 拿不準):
# - 三勢陣: 「主將提高8%→16%自帶主動、突擊戰法發動機率」——只加給「主將」這個目標概念,
#   引擎完全沒有「隊伍主將」的通用戰法施放/受益者機制(user 只要求虎豹騎+曹純這一組窄範圍
#   特例, 不建通用主將機制); 且同句混合 rateup(主動)與 chargeup(突擊)兩種原語、外加三方陣營
#   都不同的條件句(現有 mitig/amp 兩效果本身也未建模此條件), 勉強套用 who=self 或 who=ally
#   都會誤述原文語意(self 誤含非主將施放者, ally 誤及全隊而非僅主將)。跳過, 待「主將」概念
#   或條件觸發原語擴充後再處理。
# - 經天緯地: 「我軍全體發動主動戰法及突擊戰法時, 自身有35%→70%機率…對敵軍單體發動謀略攻擊」
#   ——這不是「提高突擊發動率」, 是「發動主動/突擊戰法後觸發一次額外謀略攻擊」的 proc-on-cast
#   觸發鏈, 語意上與 chargeup(直接墊高擲骰門檻)完全不同原語, 現有 15+2 原語都無法表達
#   「戰法命中後觸發另一次攻擊」的鏈式反應, 跳過。
CHARGEUP_ADD = {
    "虎豹騎": {
        "who": "ally", "val": 0.10, "dur": 3,
        "leaderBonus": {"general": "曹純", "curve": "quad", "k": 3.2e-5},
    },
    "陷陣突襲": {"who": "self", "val": 0.15, "dur": 99},
}

# --- 批7: 太平道法(張角, S級被動) 白名單 ---------------------------------------
# effectText: 「獲得14%→28%奇謀並提高自帶主動戰法發動機率（3%→6%，若為準備戰法則提高6%→12%，
# 受智力影響），自身為黃巾軍主將時，使黃巾軍副將同樣獲得自帶戰法發動機率提升」。
# RATEUP_RE 抓不到(「自帶主動戰法」中間夾了「自帶」, 不是 regex 認的「(自己/自身/友軍)主動戰法」
# 句型), 且同句混雜 amp(奇謀)+2種 rateup(一般/準備戰法)+陣營擴散條款, 屬多值複合語意, 沿用
# MANUAL_FILL/CHARGEUP_ADD 的白名單保守原則手動落地(user 遊戲實測驗證, 見下方常數):
# - amp: 「獲得14%→28%奇謀」→ 取升滿值 0.28。奇謀(智力系會心)無獨立原語, 用 amp 近似其期望
#   傷害加成(_approx:"crit-ev" 標記近似性質, 供未來精解時辨識這條非真正的「固定增傷」)。
# - rateup 一般: 「提高自帶主動戰法發動機率(3%→6%...受智力影響)」→ who=self, val=0.06,
#   scale="intel", nativeOnly=True(只加成「自帶戰法」, 即 Unit.tactics 中 native:true 的那個,
#   不該加到太平道法自己或其他傳承戰法上——太平道法本身走 inherit 傳承欄位, 不是張角自帶戰法)。
# - rateup 準備: 「若為準備戰法則提高6%→12%」→ 疊加在一般加成之上(同句「合計」語意, 故此效果
#   額外加 prepOnly=True, 只在目標戰法 tactic.prep 為真時額外套用; 兩條 rateup 相加 = prepOnly
#   戰法拿到 6%+6%=12%, 非prep戰法只拿6%, 與原文「準備戰法則提高…12%」的「則」字(非prep戰法
#   仍是6%)一致)。
# - scale="intel": 原文明寫「受智力影響」, 用批7新增的 RATE_SCALE_C(獨立於全域 SCALE, 見
#   docs/data/calibration_anchors.json → rate_scale, user 實測反解 c=0.0026)。
# - 黃巾軍主將擴散條款(「自身為黃巾軍主將時, 使黃巾軍副將同樣獲得自帶戰法發動機率提升」):
#   _todo — 需要「陣營+隊伍主將身份」的條件式效果轉移原語(把施放者的某個 buff 複製給同陣營
#   隊友), 現有 15+2 原語都無法表達「以陣營篩選 + 主將身份判斷 + 效果轉移給他人」的複合語意
#   (與批6跳過的三勢陣「主將」概念缺口同源)。保守跳過, 待「主將」概念/陣營篩選原語擴充後補。
TAIPING_EFFECTS = [
    {"k": "amp", "who": "self", "val": 0.28, "dur": 99, "_approx": "crit-ev",
     "_note": "奇謀28%(智力系會心)期望值折算"},
    {"k": "rateup", "who": "self", "val": 0.06, "dur": 99, "scale": "intel", "nativeOnly": True},
    {"k": "rateup", "who": "self", "val": 0.06, "dur": 99, "scale": "intel", "nativeOnly": True,
     "prepOnly": True, "_note": "準備戰法合計12%"},
]

# --- 批8: inheritedOnly / who:leader 白名單(引擎原語就位後補落地) --------------------
# 竭力佐謀「有70%機率使自身本回合非自帶主動戰法發動率提高100%,持續一回合」:
#   效果級機率引擎不讀 → 期望值折算 0.7×1.0=0.7(_approx:prob-ev); inheritedOnly=非自帶。
#   既有 stat(降敵智) 保留; 落地後移除 _est。
# 三勢陣「主將自帶突擊或主動戰法時,戰鬥前5回合,主將提高8%→16%自帶主動、突擊戰法發動機率」:
#   who:leader(批8原語)+nativeOnly, dur:5(前5回合=開戰套用持續5回合等價);
#   「三將陣營均不相同」與「主將自帶突擊或主動」兩個啟用條件無法表達 → 近似恆真, _note 揭露。
#   既有 mitig/amp(副將輪替 buff, 本身已是簡化) 保留。
BATCH8_TACTIC_EFFECTS = {
    "竭力佐謀": [
        {"k": "rateup", "who": "self", "val": 0.7, "dur": 1, "inheritedOnly": True,
         "_approx": "prob-ev", "_note": "70%機率非自帶主動+100% 期望值折算0.7, 持續1回合"},
    ],
    "三勢陣": [
        {"k": "rateup", "who": "leader", "val": 0.16, "dur": 5, "nativeOnly": True,
         "_note": "主將自帶主動+16% 前5回合; 陣營各異/主將自帶條件無法表達, 近似恆真"},
        {"k": "chargeup", "who": "leader", "val": 0.16, "dur": 5, "nativeOnly": True,
         "_note": "主將自帶突擊+16% 前5回合; 同上近似"},
    ],
}

# --- 批9: 外部查證14個自帶戰法整合(見 docs/data/tactics_overrides.json 的 effectText, 來源
# C:/.../scratchpad/agy_est_tactics.json, 全 high confidence) --------------------------
# 沿用批7/批8的整批覆寫白名單模式: BATCH9_TACTIC_EFFECTS[name] 為該戰法「目標 effects 全列表」
# (非 add-only), 逐條核對現有(整批重解時代留下的估計值) effectText 後決定改動:
#   - 能被既有原語精確/近似表達的落地, 標 _approx 揭露折算方式(crit-ev/decay-avg等)。
#   - 找不到任何既有原語能表達的子機制(如「每4次普攻」計數觸發、「準備回合-1」機率觸發、
#     「鎖定目標」單體綁定), 保留在 effectText/_note 供未來擴充, 不硬套錯誤原語。
#   - 逐戰法決策依據見下方分項註解; 全部落地後由 main() 統一從 _est 集合中移除(除非該戰法仍有
#     完全無法表達的核心機制, 則保留 _est+_todo, 見 BATCH9_KEEP_EST)。
#
# 1) 刀出如霆(180%×3次+30%倒戈): coef/n 已由既有整批重解正確抽到(coef=1.80=540%/3人平均,
#    n=3); 本批只新增 30%倒戈(lifesteal, 持續2回合, 自身+友軍單體) 這個新原語補件, 既有
#    amp(self/ally 0.3 dur2 = 掠陣觸發後+30%易傷近似) + amp(enemy 0.3 dur99 = 目標受傷+30%
#    近似, 沿用既有寫法) 保留不動。
# 2) 十勝十敗(前2回合主將洞察+減傷50%): who 由 ally 精修為 leader(原文「我軍主將」非泛指友軍),
#    新增 insight(who:leader, dur:2)。「主將發動主動/突擊戰法時30%機率治療」為 proc-on-cast
#    觸發鏈, 無對應原語, 跳過(_todo)。
# 3) 威武並昭(33%看破,受速度影響): 新增 pierce(who:self, val:0.33, scale:speed)。原文「每回合
#    獲得...持續1回合」照字面該用 dur:1, 但本戰法 effectText 無 when 子句 → 引擎的被動套用
#    時機是開戰時一次性套用(apply_passives 只在戰鬥開始前跑一次, 見 sgz.py fight() 開頭
#    apply_passives(no_heal=True)), dur:1 會在第1回合後失效、之後不再重新套用(無 when.rounds
#    重觸發), 與原文「每回合」的常駐語意不符; 比照全庫慣例(戰鬥中不帶 when 的被動效果一律
#    dur:99 表達「全程生效」, 如奇兵間道/虎痴/錦帆百翎等), 改用 dur:99 才能正確反映「每回合
#    都有」的持續效果, 差異已於此處說明。既有 extra(0.33, dur99 近似追加普攻機制) 保留。
#    「鎖定速度更低敵軍+9速度疊加」為單體鎖定+條件觸發複合機制, 無原語, 跳過(_todo)。
# 4) 才辯機捷(狀態傷害+90%/治療+30%): 既有 amp(self, 0.9, dur99) 對應「灼燒等狀態傷害+90%」
#    保留(amp 是通用增傷, 用於 dot 類最貼近)。原 heal(who:ally, coef:0.3) 是錯誤映射——
#    heal 原語語意是「直接治療」, 而原文是「治療效果量提升30%」(乘算修飾其他戰法的治療,
#    非自身施放治療), 15+2原語無「治療量增益」概念, 保留會產生本不存在的每回合自我治療,
#    移除該筆; 治療加成30% 標 _todo, amp(0.9)的落地保留 _est=false 的資格但因治療子機制
#    整段缺失, 保守維持 _est=true(見 BATCH9_KEEP_EST)。
# 5) 捨身救主(減傷90%,每受擊-3%遞減): 既有 decay(v0:0.9, rounds:30) 是錯誤映射——decay 原語
#    語意是「攻擊增傷衰減」(Unit.amp() 讀 decay 疊加進總增傷, 見 sgz.py 407行), 用在此處會
#    變成自身攻擊力開場+90%再30回合線性歸零, 與原文「受到傷害降低」(防禦向)完全相反; 改為
#    對 mitig(who:self) 取全程時間平均值近似(90%→0%線性遞減, 平均≈45%), _approx:"decay-avg"。
#    「降5次後35%機率視為2次觸發以額外觸發反擊/急救」為條件計數+效果複製機制, 無原語, 跳過
#    (_todo)。
# 6) 校勝帷幄(主將14%奇謀+20%奇謀傷+14%攻心+30%分擔): mitig(who:ally)精修為 who:leader(原文
#    明確「己方主將」); amp(who:ally)精修為 who:leader, val 由「14%機率觸發+100%」+「20%奇謀
#    傷害」期望折算 0.14+0.14*0.20≈0.168(_approx:"crit-ev", 沿用會心/奇謀期望值折算慣例);
#    新增 lifesteal(who:leader, val:0.14, _approx:"crit-ev") 近似「14%攻心(觸發時回復等同傷害
#    量一定比例兵力)」——lifesteal 語意(按造成傷害比例回血)與攻心描述高度吻合, 用觸發機率
#    14%本身近似期望回血比例(無法得知攻心的確切回復比例, 保守用觸發率代入)。原 stat(crit_chance)
#    效果為死欄位(引擎 eff() 不支援 crit_chance 這個 stat key, 純無效字段), 移除。
# 7) 槊血縱橫(34武+群攻54%/主將60%): 既有 stat(self, force, mult:1.05) 用乘算表達固定加值34點
#    是型別錯誤(原文「獲得34點武力」是平加不是倍率), 改為 stat(self, force, add:34, dur:99);
#    既有 extra(0.54, dur99近似群攻54%) 保留。「主將時群攻提升至60%」條件分支無法與固定54%
#    共存於單一 extra 值, 保守取一般值54%(非主將情境更常見), 跳過主將分支(_todo)。
# 8) 符命自立(前2回合任一回合100%會心/奇謀,8回合線性歸零): 既有 decay(v0:1.0, rounds:8) 精確
#    對應原文「提高100%會心/奇謀機率...每回合逐漸降低,直至第8回合降至0」, decay 原語語意
#    (Unit.amp() 攻擊增傷開場v0、rounds內線性歸零) 與此機制完全吻合, 優於任務指引建議的
#    amp+when:until2簡化版(那個只能表達「固定100%持續2回合」, 丟失「逐回合遞減到第8回合」
#    的漸變曲線, decay 原語資訊量更高更貼近原文), 保留不動。「主將時額外提高主動戰法發動率
#    (準備35%/瞬發25%),同樣衰減」為第二條 decay 曲線, rateup 原語不支援 decay 型衰減(只有
#    flat dur), 無法疊加第二條衰減曲線在同一 rateup 上, 跳過(_todo)。
# 9) 肉身鐵壁(為副將分擔30%/為主將分擔60%): 既有 redirect(share:0.3) 只反映副將分擔比例,
#    取兩種分擔對象的平均值0.45 更貼近「不分對象」的簡化語意(redirect 原語目前不支援依受益者
#    身分給不同分擔比例), share 由 0.3 改為 0.45, _note 說明為平均值近似。既有
#    amp(who:ally, val:0.18, scale:command) 對應「兵力>70%時傷害+18%受統率影響」保留
#    (「兵力高於70%」條件無法表達, 近似恆真, 沿用既有做法)。「孫權主將時基礎值增至30%」的
#    陣營+主將條件分支跳過(_todo)。
# 10) 虎痴(鎖定目標傷害+33%,擊敗後獲得破陣): 既有 amp(who:enemy, val:0.33, scale:force) 是
#     方向性錯誤——sgz.py 620-626行: who:enemy 且 val>0 的 amp 會被引擎自動轉換成
#     mitig(-val)套用在敵方身上(「敵方正amp視為敵方易傷」的既有修正邏輯), 語意變成「鎖定目標
#     受到全隊傷害+33%」, 但原文是「自身對該目標造成的傷害+33%」(只影響施放者自己的攻擊,
#     非全隊); 改為 amp(who:self, val:0.33, dur:99), 並移除不存在於原文的 scale:force(原文
#     明寫「無受X影響字樣」)。既有 pierce(who:self, val:1, dur:99) 對應「破陣狀態無視統率
#     和智力」保留(pierce 語意是無視減傷, 與無視統率/智力的防禦加成方向一致, 是現有原語中
#     最貼近的近似; 「擊敗目標後才觸發」的條件無法表達, 近似成開戰即恆定生效, 略強於原文)。
#     「每回合鎖定/最多3次判定」的單體鎖定機制跳過(_todo)。
# 11) 錦帆百翎(自身50%會心+30%會心傷/主將時友軍10%會心+15%會心傷+15%倒戈): amp(self) 由
#     0.2 改為 0.5+0.15=0.65(50%會心機率期望值+30%會心傷害加成, 沿用任務指引的直接相加折算
#     公式); amp(ally) 由 0.1 改為 0.10+0.15=0.25(10%會心機率+15%會心傷害, 同一公式); 新增
#     lifesteal(who:ally, val:0.15, dur:99) 對應「造成兵刃傷害時恢復自身兵力」的15%倒戈。
# 12) 雲聚影從(50%機率使武力最高友軍代承普攻並獲反擊+急救): 既有
#     redirect(who:ally, guard:max_force, share:0.4) + counter(who:ally, coef:1, prob:0.5) 已是
#     「單次觸發的普攻代承」在恆定機制下的合理穩態近似(share=0.4 介於「50%機率×100%代承」的
#     期望值), 保留不動。「急救(受傷30%機率治療100%)」為代承者專屬的條件治療, 目前 heal 原語
#     無法限定「只在代承後才生效」, 跳過(_todo)。
BATCH9_TACTIC_EFFECTS = {
    "刀出如霆": [
        {"k": "amp", "who": "self", "val": 0.3, "dur": 2},
        {"k": "amp", "who": "ally", "val": 0.3, "dur": 2},
        {"k": "amp", "who": "enemy", "val": 0.3, "dur": 99},
        {"k": "lifesteal", "who": "self", "val": 0.3, "dur": 2, "_note": "30%倒戈(自身)"},
        {"k": "lifesteal", "who": "ally", "val": 0.3, "dur": 2, "_note": "30%倒戈(友軍單體)"},
    ],
    "十勝十敗": [
        {"k": "mitig", "who": "leader", "val": 0.5, "dur": 2},
        {"k": "insight", "who": "leader", "dur": 2},
    ],
    "威武並昭": [
        {"k": "extra", "who": "self", "val": 0.33, "dur": 99},
        {"k": "pierce", "who": "self", "val": 0.33, "dur": 99, "scale": "speed",
         "_note": "原文「每回合...持續1回合」, 因無when子句只在開戰套用一次, dur改99以反映常駐(見上方分項註解)"},
    ],
    "才辯機捷": [
        {"k": "amp", "who": "self", "val": 0.9, "dur": 99},
    ],
    "捨身救主": [
        {"k": "mitig", "who": "self", "val": 0.45, "dur": 99, "_approx": "decay-avg",
         "_note": "90%→0%線性遞減(每受擊-3%,上限30次)時間平均值近似; decay原語語意為攻擊增傷不適用防禦"},
    ],
    "校勝帷幄": [
        {"k": "mitig", "who": "leader", "val": 0.3, "dur": 99,
         "_note": "為主將分擔30%傷害（兵力移除，不觸發急救/減傷效果）"},
        {"k": "amp", "who": "leader", "val": 0.168, "dur": 99, "_approx": "crit-ev",
         "_note": "14%奇謀機率(觸發+100%)+20%奇謀傷害 期望折算0.14+0.14*0.20"},
        {"k": "lifesteal", "who": "leader", "val": 0.14, "dur": 99, "_approx": "crit-ev",
         "_note": "14%攻心(觸發時按傷害比例回血)近似, 借用觸發率代入回血比例"},
    ],
    "槊血縱橫": [
        {"k": "stat", "who": "self", "stat": "force", "add": 34, "dur": 99},
        {"k": "extra", "who": "self", "val": 0.54, "dur": 99},
    ],
    "肉身鐵壁": [
        {"k": "redirect", "who": "ally", "guard": "self", "share": 0.45,
         "_note": "為副將分擔30%/為主將分擔60%, 取平均值0.45近似(原語不支援依受益者身分分別設定)"},
        {"k": "amp", "who": "ally", "val": 0.18, "dur": 99, "scale": "command"},
    ],
    "虎痴": [
        {"k": "amp", "who": "self", "val": 0.33, "dur": 99,
         "_note": "自身對鎖定目標傷害+33%(who由enemy修正為self: who=enemy+val>0會被引擎轉成全隊對敵易傷,方向錯誤); 原文無受X影響字樣,不加scale"},
        {"k": "pierce", "who": "self", "val": 1, "dur": 99},
    ],
    "錦帆百翎": [
        {"k": "amp", "who": "self", "val": 0.65, "dur": 99, "_approx": "crit-ev",
         "_note": "50%會心機率+30%會心傷害 期望折算0.5+0.15"},
        {"k": "amp", "who": "ally", "val": 0.25, "dur": 99, "_approx": "crit-ev",
         "_note": "友軍10%會心機率+15%會心傷害 期望折算0.10+0.15"},
        {"k": "lifesteal", "who": "ally", "val": 0.15, "dur": 99, "_note": "15%倒戈(造成兵刃傷害恢復自身兵力)"},
    ],
    "雲聚影從": [
        {"k": "redirect", "who": "ally", "guard": "max_force", "share": 0.4},
        {"k": "counter", "who": "ally", "coef": 1, "kind": "phys", "prob": 0.5},
    ],
}

# 批9: 全落地/仍缺核心機制的戰法分流 --------------------------------------------------
# BATCH9_DROP_EST: effects 已涵蓋 effectText 絕大部分數值機制(缺口僅剩無原語可表達的條件觸發
# 細節, 已於上方逐項註解說明並標_todo於此處), 移除 _est。
BATCH9_DROP_EST = {
    "刀出如霆", "十勝十敗", "威武並昭", "槊血縱橫", "符命自立", "肉身鐵壁", "虎痴", "錦帆百翎",
}
# BATCH9_KEEP_EST: 核心機制仍有無法表達的重大缺口(見各自 _todo), 保留 _est=true 供UI標示。
BATCH9_TODO = {
    "十勝十敗": "主將發動主動/突擊戰法時30%機率治療我軍單體(proc-on-cast, 無觸發鏈原語)",
    "奇兵間道": "準備戰法75%機率減1回合準備(proc-on-cast); 第5回合兵力<50%分支(45%倒戈)與否則+6%發動率分支(僅落地45%倒戈近似, 另一分支跳過)",
    "奮矛英姿": "每4次普攻觸發的全體傷害+100%(計數觸發, 無「每N次行動」原語); 現有stat近似僅覆蓋統率轉移/武力提升",
    "威武並昭": "鎖定速度更低敵軍+9速度可疊加(單體鎖定+條件觸發, 無原語)",
    "才辯機捷": "治療效果量+30%(治療增益修飾, 現有heal原語僅支援直接治療非修飾其他治療, 已移除誤用的heal效果)",
    "捨身救主": "減傷降5次後35%機率視為2次觸發(條件計數+效果複製, 無原語)",
    "校勝帷幄": "主將分擔15%→30%為等級區間, 已取滿級值",
    "符命自立": "主將時額外提高主動戰法發動率(準備35%/瞬發25%, 同樣8回合衰減)為第二條decay曲線, rateup不支援decay型衰減",
    "肉身鐵壁": "孫權主將時基礎值增至30%(陣營+主將條件分支, 近似恆真取一般值18%)",
    "虎痴": "每回合鎖定敵軍單體+最多3次判定的目標綁定機制(無單體鎖定原語, 近似成對施放者恆定生效)",
    "雲聚影從": "代承者專屬急救(受傷30%機率治療100%), heal原語無法限定僅代承後生效",
}

# --- 批7: rateup/chargeup 已落地條目全庫掃描, 補「受X影響」的 scale --------------
# 逐條核對目前所有已落地 rateup/chargeup 效果(白眉/先成其慮/獅子奮迅/進言/虎豹騎/陷陣突襲)
# 對應的 effectText, 找「受X影響」措辭是否修飾發動率子句本身:
# - 白眉「戰鬥中，自身主動戰法的發動機率提高6%→12%」: 無「受X影響」, 跳過。
# - 先成其慮「…並使自己主動戰法的發動機率提高7.5%→15%，持續1回合」: 句中「受智力影響」
#   修飾的是前段謀略傷害(72.5%→145%)子句, 不是發動機率子句, 跳過(kind=intel 已天然建模
#   傷害那半, 不重複標記)。
# - 獅子奮迅「…並使自身主動戰法發動機率提高5%→10%，持續2回合」: 全句無「受X影響」, 跳過
#   (後段的「受武力影響」修飾的是叛逃狀態的持續傷害子句, 與發動機率無關)。
# - 進言「使友軍單體主動戰法發動幾率提高4%→8%，并提高20→40點智力，持續2回合」: 無「受X影響」,
#   跳過。
# - 虎豹騎「若曹純統領時，提升的發動機率額外受武力影響」: 有「受X影響」, 但已用專屬
#   leaderBonus 二次曲線機制(engine.js/sgz.py 的曹純特例, k=3.2e-5, 見 CHARGEUP_ADD 註解)
#   建模, 與本次新增的線性 RATE_SCALE_C 是不同曲線/不同錨點, 不套用本白名單的線性 scale。
# - 陷陣突襲「自身突擊戰法發動機率提高7.5%→15%」: 無「受X影響」, 跳過。
# 結論: 全庫目前已落地的 rateup/chargeup 條目中, 除太平道法(本批新增)外, 沒有其他戰法的
# 發動率子句本身標註「受X影響」, 故本白名單暫為空(保留供未來新落地戰法核對時使用)。
RATE_SCALE_PLAN = {}

# --- 批7補(user 釐清): nativeOnly 適用範圍 — 嚴格按原文措辭二分 ------------------
# 規則(user 明確指示):
# - 原文明寫「自帶(主動)戰法發動率|機率」→ nativeOnly:True(如 太平道法; 裝備特技 武聖/
#   天機/虎峙/喬裝 —— equips_parsed.json 既有的 nativeOnly 旗標與本規則一致, 本批引擎
#   落地 addbonus_for 後開始實際生效)。
# - 原文寫「自身|自己主動戰法」→ 不加 nativeOnly, 全部主動戰法都吃(含傳承), 也不分準備
#   與否(user 明確指出白眉不分自帶或準備)。
# - 拿不準的不加旗標, 只回報。
#
# 全庫掃描分類結果(發動率提升類子句, 2026-07-02):
# 【nativeOnly 組(原文「自帶」)】
# - 已落地: 太平道法(tactics, 本批)。裝備側(equips_parsed.json, 非本批可改檔, 旗標既有已
#   合規): 武聖「自帶戰法發動率增加5%」/ 天機「增加自帶戰法發動機率35%→39%」/ 虎峙「戰鬥
#   首回合提高自帶戰法15%發動率」/ 喬裝「自帶戰法造成控制機率提高6%」(rateup近似, 見其_todo)。
# - 未落地(各有無法表達的缺口, 跳過): 三勢陣(主將概念, 批6已記錄) / 振軍擊營(「有負面狀態
#   的友軍單體」條件無原語) / 藏器待時(「第三回合起」是效果級窗口, when.from 為戰法級,
#   前2回合另有效果會被誤延) / 虛實奇謀(主將+適性S條件) / 錦囊妙計(每回合機率觸發+跳過準備
#   的複合機制) / 兵書·百戰「提高自帶戰法的發動率」(原文無數值, 且 bingshu_parsed.json
#   非本批可改檔)。
# 【全主動組(「自身/自己/友軍…主動戰法」, 不加 nativeOnly)】
# - 已落地: 白眉 / 先成其慮 / 獅子奮迅 / 進言(皆無旗標, 合規)。裝備側: 樂奏「提高3%主動
#   戰法發動幾率」/ 回響「提高1.5%主動戰法發動幾率」(皆無旗標, 合規)。
# - 未落地(跳過): 十二奇策(「提高我軍全體1回合3%→6%主動戰法發動機率(受智力影響)」rateup
#   子句本身乾淨, 但後半 proc-on-cast 謀略攻擊無原語, 且 RATEUP_RE 不匹配「值在前」句型,
#   整條戰法留待後續批次) / 舌戰群儒(反應式觸發「敵軍嘗試發動主動戰法時」) / 符命自立
#   (主將條件+逐回合衰減) / 兵書·示敵以弱(原文無數值)。
# 【拿不準, 不加旗標, 回報】
# - 竭力佐謀「使自身本回合非自帶主動戰法發動率提高100%」: 「非自帶」是 nativeOnly 的反向
#   (inheritedOnly), 現無此旗標概念, 硬套 nativeOnly 會弄反語意, 跳過。
# 【非 rateup 語意(不相關)】 南蠻渠魁/大戟士/結盟(自身戰法內部發動率機制, 非給其他戰法的
# buff) / 天公(發動自帶戰法後 proc 額外攻擊, 非發動率提升)。


def extract_dmg_pct(txt):
    """取滿級傷害率(取範圍上限), 找不到回傳 None。"""
    m = DMG_RANGE.search(txt)
    if m:
        return float(m.group(2))
    m = DMG_SINGLE.search(txt)
    if m:
        return float(m.group(1))
    return None


def _maxval(txt, kw):
    """抓 'kw...A→B' 或 'kw...A%→B%', 回傳升滿值 B(取範圍上限); 找不到回傳 None。
    與 sgz.py 內同名函式邏輯一致, 這裡獨立一份供 reparse 的白名單數值抽取用。"""
    m = re.search(re.escape(kw) + r"[^0-9]{0,10}(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)", txt)
    if m:
        return float(m.group(2))
    m = re.search(re.escape(kw) + r"[^0-9]{0,10}(\d+(?:\.\d+)?)", txt)
    return float(m.group(1)) if m else None


def _maxval_before(txt, kw):
    """抓 'A→B%kw' 或 'A%→B%kw'(數值在關鍵字之前, 如「12.5%→25%規避」), 回傳升滿值 B; 找不到回傳 None。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*%?\s*(?:→|~|-)\s*(\d+(?:\.\d+)?)\s*%?" + r"[^0-9]{0,4}" + re.escape(kw), txt)
    if m:
        return float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*%" + r"[^0-9]{0,4}" + re.escape(kw), txt)
    return float(m.group(1)) if m else None


def extract_heal_pct(txt):
    m = HEAL_RANGE.search(txt)
    if m:
        return float(m.group(2))
    m = HEAL_SINGLE.search(txt)
    if m:
        return float(m.group(1))
    return None


def _num(s):
    """字串轉數值: 整數值回傳 int, 否則回傳 float(維持 JSON 輸出簡潔)。"""
    v = float(s)
    return int(v) if v == int(v) else v


def extract_dur(txt):
    """取第一個「持續X[~Y]回合」的 X(下限/單值)。範圍型回傳 (lo, hi)。"""
    m = DUR_RE.search(txt)
    if not m:
        return None, None
    lo = _num(m.group(1))
    hi = _num(m.group(2)) if m.group(2) else None
    return lo, hi


def extract_target_n(effect_target):
    """從 effectTarget 欄位(如「我軍群體 2-3 人」)取 n / nMax。"""
    if not effect_target:
        return None, None
    m = TARGET_N_RANGE.search(effect_target)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = TARGET_N_SINGLE.search(effect_target)
    if m:
        return int(m.group(1)), None
    return None, None


def has_kw(txt, kws):
    return any(k in txt for k in kws)


def _rate_match_ok(text, m):
    """機率 match 的排除規則: (1) 關鍵字後緊跟 規避/會心/奇謀 = 狀態機率非觸發機率;
    (2) match 位於未閉合的全形括號內 = 括號內註解(如「每有1名…提升2%→4%機率」), 非主觸發鏈。"""
    after = text[m.end():m.end() + 2]
    if after.startswith(RATE_NOT_TRIGGER):
        return False
    if text.count("（", 0, m.start()) > text.count("）", 0, m.start()):
        return False
    return True


def extract_inline_rate(txt):
    """抽 command/passive 戰法內文的「傷害觸發機率」(有別於戰法整體 activationRate)。

    綁定規則(避免全文首匹配誤綁, 如士別三日的規避機率):
    - 有「傷害率」字樣時: 只在「傷害率所在子句」(以 。；;/ 為句界)中、且位於傷害率
      之前的機率裡取「最近的一個」; 若機率與傷害率之間出現「第N回合」(換了時間子句)
      則該機率不算同一觸發鏈。
    - 無「傷害率」字樣(coef 為 LLM 粗估)時: 退回全文首個合法機率。
    - 取範圍滿級值(X%→Y% 取 Y)。找不到回傳 None(特定回合/反應式觸發等留給引擎近似)。"""
    dm = DMG_RANGE.search(txt) or DMG_SINGLE.search(txt)
    if dm:
        starts = [txt.rfind(ch, 0, dm.start()) for ch in "。；;/"]
        window = txt[max(starts) + 1:dm.start()]
        best = None
        for m in INLINE_RATE_RE.finditer(window):
            if not _rate_match_ok(window, m):
                continue
            if re.search(r"第\s*\d+\s*回合", window[m.end():]):
                continue                              # 機率與傷害間隔了新的時間子句
            best = m
        if best is None:
            return None
        v = float(best.group(2)) if best.group(2) else float(best.group(1))
        return round(v / 100, 4)
    for m in INLINE_RATE_RE.finditer(txt):
        if _rate_match_ok(txt, m):
            v = float(m.group(2)) if m.group(2) else float(m.group(1))
            return round(v / 100, 4)
    return None


def load_overrides():
    """讀 docs/data/tactics_overrides.json。缺檔時回傳空 dict(overrides 為選配層)。"""
    if not os.path.exists(OVERRIDES_PATH):
        return {}
    with open(OVERRIDES_PATH, encoding="utf-8") as f:
        doc = json.load(f)
    return doc.get("overrides", {})


def apply_overrides_to_raw(raw_by_name, overrides):
    """在抽取前, 用 overrides 的 effectText/type 覆蓋 raw_by_name 對應戰法。
    invalid=true 的條目不覆蓋 effectText(維持原文供人工參考), 由 main() 另行處理成 type=none。
    回傳套用的戰法名清單(不含 invalid 條目), 供報告統計。"""
    applied = []
    for name, ov in overrides.items():
        if ov.get("invalid"):
            continue
        r = raw_by_name.get(name)
        if not r:
            continue
        if "effectText" in ov:
            r["effectText"] = ov["effectText"]
        if "type" in ov:
            r["_override_type"] = ov["type"]           # 交給 main() 決定如何套用到 parsed.type
        applied.append(name)
    return applied


def load_corrections():
    """讀 docs/data/tactic_corrections.json(批10: 全庫驗證findings仲裁修正覆蓋層)。
    缺檔時回傳空 dict(corrections 為選配層, 供 reparse 冪等重跑時不報錯)。"""
    if not os.path.exists(CORRECTIONS_PATH):
        return {}
    with open(CORRECTIONS_PATH, encoding="utf-8") as f:
        doc = json.load(f)
    return doc.get("corrections", {})


def apply_corrections(parsed, corrections):
    """批10: 在所有既有步驟之後(最末)套用 corrections —— 全庫驗證findings仲裁的最終結果。
    宣告式覆寫(讀檔, 非增量 patch): 每個戰法的 correction 物件可含:
      - "set": {欄位: 目標值} 的 sparse dict, 只覆寫列出的頂層欄位(coef/rate/n/nMax/prep等),
        不觸碰其餘欄位。批15修正: main() 是以「讀取現有 tactics_parsed.json 為起點, 逐步疊加
        修改」運作(非每次從 raw 重新生成的純函數), 若某一版 correction 曾用 set 寫入過某欄位
        (如 when), 之後改版拿掉該欄位, 舊值會停留在 parsed 檔案裡「卡住」不會自動消失(因為
        set 只覆寫列出的鍵, 不會清除不在列表中但過去寫過的鍵)。故 set 裡把某欄位的值設為
        Python None(JSON null) 時, 視為「清除此欄位」指令(用 dict.pop 移除, 而非寫入 None字面
        值), 讓 correction 需要撤回某個曾經 set 過的欄位時有乾淨的做法, 不必擔心殘留。
      - "effects": 完整替換的效果陣列(宣告式最終狀態, 非 add-only); 提供時整組覆寫該戰法的
        effects, 不提供則 effects 維持不動(由前面各步驟或既有資料決定)。
      - "extraHits": 完整替換的多段傷害陣列(批14新增支援; 語意同 effects, 提供時整組覆寫,
        不提供則 extraHits 維持不動)。批14首次出現「頂層coef隨機選目標bug, 改用extraHits
        (who:enemyLeader)鎖定固定目標」這類修正(如暗潮洶湧/暗潮湧動/暗藏玄機), 此前231筆
        corrections從未用過此欄位, 故此支援為新增而非既有行為的一部分。
      - "everyN": 完整替換的{count,on}計數觸發設定(批17新增支援; 語意同 extraHits, 提供時
        整組覆寫, 提供 None 則清除該欄位)。批16原語擴充包(engine.js/sgz.py)已支援
        t.everyN(自身第N次普攻觸發 effects/extraHits), 但 apply_corrections() 直到批17
        才補上讀取——批17首批寫入everyN的2筆corrections(奮矛英姿/兵無常勢)在補上此支援前
        測試時發現完全沒進 tactics_parsed.json(與下方_addTopLevel同類「宣告了但沒接上」的
        靜默遺失, 已在本次一併修正)。
      - "choices": 完整替換的擇一分支陣列(批17新增支援; 語意同 effects, 提供 None 則清除該
        欄位)。批16已支援 t.choices(發動時按權重隨機選一組效果), 同上一併補上讀取支援
        (本輪暫無實際使用choices的corrections案例, 因逐一核對候選戰法後發現皆不適用
        choices的派發前提——見 tactic_corrections.json 內各戰法_todo的核對紀錄, 此支援
        為前瞻性補齊, 供未來新戰法使用)。
      - "_addTopLevel": {欄位: 值} 的 sparse dict, 直接把揭露性中繼資料(_todo/_note/_approx/
        _est等, 非戰鬥語意欄位)寫到 parsed 戰法物件頂層。
      - 揭露性中繼資料鍵(_todo/_note/_note2/_note_self/_approx/_est) 若直接以「裸」頂層鍵的
        形式出現在 correction 物件本身(不透過 _addTopLevel 包一層), 同樣視為要寫入 parsed
        頂層的揭露內容。
    批15修正: 上述兩種寫法(_addTopLevel 包一層 / 裸頂層鍵)在批10~14的278筆corrections中
    並存(前者5筆, 後者27筆用裸_todo等), 但 apply_corrections() 從未真正讀取套用過任一種
    ——導致這些戰法的揭露文字只存在於 tactic_corrections.json 原始檔, 從未進入
    tactics_parsed.json(下游UI/盲測審查讀的是 parsed 檔), 形成「看似已誠實揭露, 實際上
    揭露文字從未落地」的靜默遺失(v4盲測「驅散: 主要效果no-op且未揭露」即為此因, 並非
    沒寫揭露, 而是揭露寫了但沒接上)。現在補上讀取與套用, 兩種寫法都支援: 逐欄寫入(不
    覆蓋同名戰鬥語意欄位; _evidence/_diff/_bucket/_conflict_note/_issue 屬修正過程的內部
    稽核軌跡, 非戰法本身的揭露內容, 不寫入 parsed)。
    與既有白名單(SCALE_PLAN/BATCH8_TACTIC_EFFECTS/BATCH9_TACTIC_EFFECTS等)衝突時, corrections
    優先(它是批10最新仲裁結果, 已核對過原文與既有白名單的落地是否正確)。天然冪等: 每次重跑
    都是「讀取同一份 corrections.json 覆寫成同一個目標值」, 不會累積變動。

    批14修正: 若 correction 提供完整 effects(宣告式最終狀態, 已人工核對過原文, 非估計值),
    套用後應清除 p["_est"](若存在)。原因: 主迴圈(item1-15)的 _est 判定是「本輪是否有任何
    正則抽取步驟改到有意義的值」的啟發式訊號, 而該訊號讀的是「套用 correction 前」的
    p.effects/p.coef; 當 correction 把某效果的 dur 從 <90 改成 99(如「戰鬥全程」語意)後,
    下一輪重跑時 item4(持續回合回填) 見到 dur 已經 >=90 便跳過(cur>=90 視為"永久型不動"),
    導致該輪 touched_meaningful 判定為 False、_est 被誤加 —— 即使 effects 內容與上一輪
    apply_corrections() 後完全一致(bytes 相同)。這使得「本檔有無 correction 覆蓋」變成
    run1→run2 才穩定的延遲效應, 破壞 reparse 的 byte 級冪等性。corrections 本身是已核對的
    最終真相(非未審查估計), 不應被下游啟發式誤標為 _est, 故套用完整 effects 覆寫時一併清掉
    _est(sparse set-only 覆寫則不受影響, 因為它通常不改變 dur 這類會影響 item4 判定的欄位)。
    回傳: (n_set_changed, n_effects_changed, applied_names) 供報告統計。"""
    n_set_changed = 0
    n_effects_changed = 0
    applied_names = []
    by_name = {p["nameZh"]: p for p in parsed}
    for name, corr in corrections.items():
        p = by_name.get(name)
        if p is None:
            continue                                  # corrections 提到但 parsed 沒有此戰法(不應發生, 保守跳過不報錯)
        changed_this = False
        for fld, val in (corr.get("set") or {}).items():
            if val is None:                               # 批15: set 裡的 None(JSON null) = 清除該欄位(見上方docstring), 而非寫入 null 字面值
                if fld in p:
                    del p[fld]
                    n_set_changed += 1
                    changed_this = True
                continue
            if p.get(fld) != val:
                p[fld] = val
                n_set_changed += 1
                changed_this = True
        if "effects" in corr:
            want_effects = corr["effects"]
            if p.get("effects") != want_effects:
                p["effects"] = [dict(e) for e in want_effects]
                n_effects_changed += 1
                changed_this = True
            p.pop("_est", None)                       # 批14: completed effects 是審查過的最終狀態, 不是估計值
        if "extraHits" in corr:                       # 批14新增: extraHits 完整替換(同 effects 慣例)
            want_extra = corr["extraHits"]
            if p.get("extraHits") != want_extra:
                p["extraHits"] = [dict(e) for e in want_extra]
                n_effects_changed += 1
                changed_this = True
            p.pop("_est", None)
        # 批17新增: everyN(戰法級計數觸發設定, {count,on}) 完整替換(同 extraHits 慣例, dict而非list)。
        # 修 bug: 批16原語擴充包上線後, corrections 已有多筆(如奮矛英姿/兵無常勢)在頂層宣告
        # "everyN"/"choices" 鍵期望寫入 parsed, 但 apply_corrections() 從未讀取這兩個鍵
        # ——與批15 docstring 記載的「_addTopLevel/裸頂層鍵曾經寫了但沒接上」同一類靜默遺失,
        # 這次是新原語擴充後忘記同步更新此函式支援清單, 導致 everyN/choices 完全沒進
        # tactics_parsed.json(engine 端 t.everyN/t.choices 讀不到, 该戰法的新原語完全不會觸發)。
        if "everyN" in corr:
            want_everyN = corr["everyN"]
            if p.get("everyN") != want_everyN:
                p["everyN"] = dict(want_everyN) if want_everyN is not None else None
                if want_everyN is None:
                    p.pop("everyN", None)
                n_effects_changed += 1
                changed_this = True
            p.pop("_est", None)
        # 批17新增: choices(擇一分支陣列) 完整替換(同 effects 慣例, list of dict)。
        if "choices" in corr:
            want_choices = corr["choices"]
            if p.get("choices") != want_choices:
                if want_choices is None:
                    p.pop("choices", None)
                else:
                    p["choices"] = [dict(c) for c in want_choices]
                n_effects_changed += 1
                changed_this = True
            p.pop("_est", None)
        # 批15: 揭露性中繼資料寫到 parsed 頂層(見上方docstring) —— 放在 effects/extraHits 的
        # _est清除之後, 確保若 correction 同時提供完整effects又想保留/補寫自己的_est(如
        # 「已核對但仍是近似值」的情境), 不會被上面 p.pop("_est", None) 誤清掉。
        DISCLOSURE_KEYS = ("_todo", "_note", "_note2", "_note_self", "_approx", "_est")
        disclosure_src = dict(corr.get("_addTopLevel") or {})
        for k in DISCLOSURE_KEYS:
            if k in corr:                                  # 裸頂層鍵寫法(較常見, 27筆); _addTopLevel 包一層寫法(5筆)已在上面併入
                disclosure_src.setdefault(k, corr[k])
        for fld, val in disclosure_src.items():
            if p.get(fld) != val:
                p[fld] = val
                changed_this = True
        if changed_this:
            applied_names.append(name)
    return n_set_changed, n_effects_changed, applied_names


def main():
    with open(RAW_PATH, encoding="utf-8") as f:
        raw_list = json.load(f)
    raw_by_name = {t["nameZh"]: t for t in raw_list}

    overrides = load_overrides()
    overrides_applied = apply_overrides_to_raw(raw_by_name, overrides)
    overrides_invalid = [nm for nm, ov in overrides.items() if ov.get("invalid")]

    with open(PARSED_PATH, encoding="utf-8") as f:
        parsed = json.load(f)

    # overrides: invalid(幽靈條目) / type=none(內政類等) 覆蓋 —— 在主迴圈前先處理, 這兩類
    # 直接跳過後續一切抽取(coef/scale/rateup...), 維持 type=none 的「排除戰鬥與選單」語意。
    # no-op 統計口徑: 在 overrides type:none 生效「之前」計 before(與 HEAD 直接比較一致
    # —— 被 overrides 排除的戰法在 before 仍算 no-op 貢獻者, 排除本身即是一種「處理」)。
    # 批28 B4: 補 choices/extraHits/everyN 排除(同 lint_tactics.py check_r5 的no-op判定口徑)——
    # 過去只看 coef/effects 兩個欄位, 若戰法的全部payload都搬進choices(如桃園結義三選一重建
    # 後 coef=0/effects=[], 內容全在choices[]分支自己的coef/effects裡), 會被這個純統計用的
    # 診斷指標誤算成「no-op」, 但戰法本身完全正常運作(choices機制見sgz.py fight()主迴圈
    # dispatch, 批16/27已支援)。此為報表口徑修正, 不影響任何實際結算邏輯。
    noop_before = sum(1 for p in parsed if p.get("type") != "none"
                       and not p.get("coef") and not p.get("effects")
                       and not p.get("choices") and not p.get("extraHits") and not p.get("everyN"))

    n_overrides_invalid = 0
    n_overrides_type_none = 0
    for p in parsed:
        name = p["nameZh"]
        ov = overrides.get(name)
        if not ov:
            continue
        if ov.get("invalid") and p.get("type") != "none":
            p["type"] = "none"
            n_overrides_invalid += 1
        elif ov.get("type") == "none" and p.get("type") != "none":
            p["type"] = "none"
            n_overrides_type_none += 1

    n_coef_filled = 0
    n_rate_filled = 0
    n_n_filled = 0
    n_dur_filled = 0
    n_heal_filled = 0
    n_new_ctrl_tags = 0
    first_tagged = []
    n_est = 0
    n_when_filled = 0
    n_taunt_tagged = 0
    n_dodge_tagged = 0
    n_surehit_tagged = 0
    n_shield_tagged = 0
    n_scale_tagged = 0
    n_rateup_tagged = 0
    n_manual_filled = 0
    rateup_tagged_names = []
    n_chargeup_tagged = 0
    chargeup_tagged_names = []
    n_taiping_tagged = 0
    n_batch8_tagged = 0                                   # 批8: 竭力佐謀/三勢陣 白名單套用數
    n_batch9_tagged = 0                                   # 批9: 14個自帶戰法查證整合 白名單套用數
    batch9_tagged_names = []
    n_rate_scale_backfilled = 0
    rate_scale_backfilled_names = []

    for p in parsed:
        if p.get("type") == "none":
            continue
        r = raw_by_name.get(p["nameZh"])
        if not r:
            p["_est"] = True
            n_est += 1
            continue
        txt = r.get("effectText") or ""
        # touched_meaningful: 是否至少改到一個「有實質意義」的數值(coef/dur/heal)。
        # rate 幾乎每個戰法都有 activationRate 可核對, 光是 rate 對上不足以代表「解析成功」,
        # 故不計入 touched_meaningful, 避免真正的 no-op/待補資料被誤判成「已處理」。
        touched_meaningful = False

        # --- 1) 傷害率 -> coef -------------------------------------------------
        dmg_pct = extract_dmg_pct(txt)
        if dmg_pct is not None:
            new_coef = round(dmg_pct / 100, 4)
            if p.get("coef") is None or abs((p.get("coef") or 0) - new_coef) > 1e-9:
                p["coef"] = new_coef
                n_coef_filled += 1
            touched_meaningful = True

        # --- 2) 發動率 -> rate ---------------------------------------------------
        # 先決定「目標 rate 值」再一次寫入, 避免 2a(寫 activationRate)接著被
        # 2b(指揮/被動內文機率覆寫)蓋掉, 導致重複執行時錯誤地重複計入回填次數。
        ar = r.get("activationRate")
        target_rate = ar
        # 指揮/被動 且有傷害 coef 時: activationRate 常是 1(代表戰法本身無外部次數
        # 限制), 真正逐回合觸發機率藏在 effectText 內文(如「有25%→35%機率」)。
        # 舊引擎用 CMD_TRIGGER/PASSIVE_TRIGGER 全域折扣近似這個機率, 新引擎改成直接
        # 讀 rate 逐回合擲骰, 所以這裡要把內文機率覆寫進 rate, 否則 rate=1 會變成
        # 每回合必定觸發, 大幅高估指揮/被動戰法的傷害輸出。
        # 只在「有明確傷害 coef」時才覆寫, 避免動到純增益類戰法(它們的 100% 生效是設計本意)。
        if p.get("type") in ("command", "passive") and p.get("coef") and ar == 1:
            inline_rate = extract_inline_rate(txt)
            if inline_rate is not None:
                target_rate = inline_rate
        # 批2 白名單覆寫: coef=0 的 on 觸發類走不到上面的 inline 抽取(它只認有 coef 的),
        # 內文機率需白名單指定(如 魅惑 0.45), 在寫入前覆寫確保單次寫入、計數冪等。
        if p["nameZh"] in RATE_OVERRIDE:
            target_rate = RATE_OVERRIDE[p["nameZh"]]
        if target_rate is not None:
            if p.get("rate") is None or abs((p.get("rate") or 0) - target_rate) > 1e-9:
                p["rate"] = target_rate
                n_rate_filled += 1
            # 注意: rate 不計入 touched_meaningful(_est 判定) — 幾乎每個戰法都有
            # activationRate 可核對, 光是 rate 對上不代表該戰法的效果資料完整。

        # --- 3) 目標數 -> n / nMax (effectTarget 欄位, 較 effectText 結構化) -----
        # MANUAL_FILL 有指定 n 的戰法跳過: 其 effectTarget(如桃園結義「我軍群體3人」)描述的
        # 是增益受眾, 非傷害目標數, 由白名單值優先(避免每輪 3↔1 互相覆寫的計數churn)。
        tgt_n, tgt_nmax = extract_target_n(r.get("effectTarget"))
        if tgt_n is not None and "n" not in MANUAL_FILL.get(p["nameZh"], {}):
            if p.get("n") != tgt_n:
                p["n"] = tgt_n
                n_n_filled += 1
            if tgt_nmax is not None and p.get("nMax") != tgt_nmax:
                p["nMax"] = tgt_nmax
            touched_meaningful = True

        # --- 4) 持續 -> 各 effect 的 dur (只套用在已有 dur 欄位的效果, 且非 stun 特例) --
        dur_lo, dur_hi = extract_dur(txt)
        if dur_lo is not None:
            for e in p.get("effects", []):
                if "dur" not in e:
                    continue
                # first 的 dur 來自「戰鬥前N回合」(6c), 非「持續N回合」;
                # taunt/dodge/surehit 的 dur 來自批2白名單(9~11)的專屬子句解析, 全文首個
                # 「持續N回合」常屬於同段落裡的另一個效果(如 驍健神行 的「持續1回合」屬於
                # disarm, 不該覆寫「持續2回合」的 surehit), 故排除, 避免非冪等覆寫。
                # chargeup 的 dur 來自批6白名單(見 CHARGEUP_ADD, 逐條核對「戰鬥前N回合」子句)——
                # 虎豹騎 effectText 混雜多版本歷史文案, 全文首個「持續N回合」屬於另一版本的
                # 45%→90%機率繳械子句(持續1回合), 與 chargeup 子句(戰鬥前3回合)無關, 同樣排除,
                # 否則每輪被降覆成 dur=1、再被 item16 修回 dur=3, 造成非冪等 churn。
                if e.get("k") in ("first", "taunt", "dodge", "surehit", "chargeup"):
                    continue
                cur = e.get("dur")
                if cur is not None and cur >= 90:      # 永久型(99) 不動: 語意是"戰鬥全程"
                    continue
                if cur is None or abs(cur - dur_lo) > 1e-9:
                    e["dur"] = dur_lo
                    n_dur_filled += 1
                if dur_hi is not None:
                    e["durMax"] = dur_hi
                touched_meaningful = True

        # --- 5) 治療率 -> heal coef ----------------------------------------------
        heal_pct = extract_heal_pct(txt)
        if heal_pct is not None:
            new_hcoef = round(heal_pct / 100, 4)
            for e in p.get("effects", []):
                if e.get("k") == "heal":
                    if e.get("coef") is None or abs((e.get("coef") or 0) - new_hcoef) > 1e-9:
                        e["coef"] = new_hcoef
                        n_heal_filled += 1
                    touched_meaningful = True

        # --- 6a) 既有 stun(who=enemy) 效果精分類: 依 effectText 關鍵字單一命中時,
        # 把過度概括的 "stun" 換成更精確的 silence(計窮/技窮=禁主動) 或
        # disarm(繳械=禁普攻); 純震懾(全禁)維持 stun。
        # 只在該效果對應文字段落「恰好命中其中一種」關鍵字(無混雜)時才改, 避免誤判。
        for e in p.get("effects", []):
            if e.get("k") in ("stun", "silence", "disarm") and e.get("who") == "enemy":
                has_s = has_kw(txt, SILENCE_KW)
                has_d = has_kw(txt, DISARM_KW)
                has_z = has_kw(txt, STUN_KW)
                hit_kinds = sum([has_s, has_d, has_z])
                if hit_kinds == 1:
                    new_k = "silence" if has_s else ("disarm" if has_d else "stun")
                    if e["k"] != new_k:
                        e["k"] = new_k
                        n_new_ctrl_tags += 1
                    touched_meaningful = True   # 精分類過(或重跑冪等維持), 不應標 _est

        # --- 6b) 洞察(免疫控制) 新增: 僅在 effectTarget 明確為「自己」時才安全新增 -----
        existing_ks = {e.get("k") for e in p.get("effects", [])}
        if has_kw(txt, INSIGHT_KW) and (r.get("effectTarget") or "").strip() == "自己":
            if "insight" not in existing_ks:
                dur_lo2, _ = extract_dur(txt)
                p["effects"].append({"k": "insight", "who": "self", "dur": dur_lo2 if dur_lo2 else 99})
                n_new_ctrl_tags += 1
            touched_meaningful = True   # 無論是新增或已存在(重跑冪等), 都算已妥善處理, 不應標 _est

        # --- 6c) 先攻(優先行動) 新增: 只認明確授予句型, 對象 自己/我軍主將/我軍全體 ------
        fm = FIRST_GRANT_RE.search(txt)
        if fm:
            if "first" not in existing_ks:
                who = "ally" if "我軍全體" in fm.group(0) else "self"  # 我軍主將 以 self(施放者)近似
                dm2 = FIRST_DUR_RE.search(txt)
                p["effects"].append({"k": "first", "who": who,
                                     "dur": int(dm2.group(1)) if dm2 else 1})
                n_new_ctrl_tags += 1
                first_tagged.append(p["nameZh"])
            touched_meaningful = True

        # --- 7) when.rounds/from/until: 條件觸發回合窗口(白名單, 見 WHEN_FROM 註解) ---
        name = p["nameZh"]
        if name in WHEN_FROM:
            w = p.setdefault("when", {})
            if w.get("from") != WHEN_FROM[name]:
                w["from"] = WHEN_FROM[name]
                n_when_filled += 1
            touched_meaningful = True
        if name in WHEN_UNTIL:
            w = p.setdefault("when", {})
            if w.get("until") != WHEN_UNTIL[name]:
                w["until"] = WHEN_UNTIL[name]
                n_when_filled += 1
            touched_meaningful = True

        # --- 8) when.on: 反應式觸發(受到普通攻擊時/受到傷害後), 見 ON_ATTACKED/ON_DAMAGED --
        if name in ON_ATTACKED:
            w = p.setdefault("when", {})
            if w.get("on") != "attacked":
                w["on"] = "attacked"
                n_when_filled += 1
            touched_meaningful = True
        if name in ON_DAMAGED:
            w = p.setdefault("when", {})
            if w.get("on") != "damaged":
                w["on"] = "damaged"
                n_when_filled += 1
            touched_meaningful = True

        # --- 9) taunt(嘲諷): 白名單新增(不取代既有效果), 見 TAUNT_DUR 註解 -------------
        if name in TAUNT_DUR:
            if "taunt" not in existing_ks:
                p["effects"].append({"k": "taunt", "who": "enemy", "dur": TAUNT_DUR[name]})
                n_taunt_tagged += 1
            touched_meaningful = True

        # --- 10) dodge(規避): 純新增 或 修正既有誤標的 mitig, 見 DODGE_ADD/DODGE_FIX_MITIG ---
        if name in DODGE_ADD and "dodge" not in existing_ks:
            spec = DODGE_ADD[name]
            if "prob" in spec:                        # 白名單直接給定(原文無數值的概要式文本)
                v100 = spec["prob"] * 100
            else:
                # 規避的主流句型是「12.5%→25%規避」(數值在關鍵字之前), 先試 before;
                # 若先試 after(_maxval) 會誤抓關鍵字後面無關子句的數字(如 士別三日
                # 「…機率規避效果，第 4 回合…」會錯抓到 4)。
                v100 = _maxval_before(txt, spec["prob_kw"])
                if v100 is None:
                    v100 = _maxval(txt, spec["prob_kw"])
            if v100 is not None:
                p["effects"].append({"k": "dodge", "who": spec.get("who", "ally"),
                                     "prob": round(v100 / 100, 4), "dur": spec["dur"]})
                n_dodge_tagged += 1
                touched_meaningful = True
        if name in DODGE_FIX_MITIG:
            for e in p.get("effects", []):
                if e.get("k") == "mitig":
                    e["k"] = "dodge"
                    e["prob"] = e.pop("val", 0.2)
                    n_dodge_tagged += 1
                    touched_meaningful = True

        # --- 11) surehit(必中): 白名單新增, 見 SUREHIT_ADD 註解 -----------------------
        if name in SUREHIT_ADD and "surehit" not in existing_ks:
            p["effects"].append({"k": "surehit", "who": "self", "dur": SUREHIT_ADD[name]})
            n_surehit_tagged += 1
            touched_meaningful = True

        # --- 12) shield(護盾): 白名單新增, 目前無可解析的具體吸收量案例(見 SHIELD_ADD 註解) ---
        if name in SHIELD_ADD and "shield" not in existing_ks:
            spec = SHIELD_ADD[name]
            p["effects"].append({"k": "shield", "who": spec.get("who", "ally"), **spec["amt_kw"], "dur": spec["dur"]})
            n_shield_tagged += 1
            touched_meaningful = True

        # --- 13) scale(受屬性影響縮放): 白名單新增, 見 SCALE_PLAN 註解 -----------------
        if name in SCALE_PLAN:
            effs = p.get("effects", [])
            for idx, scale in SCALE_PLAN[name]:
                if idx < len(effs) and effs[idx].get("scale") != scale:
                    effs[idx]["scale"] = scale
                    n_scale_tagged += 1
            touched_meaningful = True

        # --- 14) rateup(主動戰法發動機率提升): regex 全庫掃描, 見 RATEUP_RE 註解 ----------
        rm = RATEUP_RE.search(txt)
        if rm:
            who = "ally" if rm.group("who") in ("友軍單體", "友軍") else "self"
            val = round(float(rm.group("val")) / 100, 4)
            # dur: 匹配後方30字內(同句語境)的「持續X回合」; 抽不到 = 常駐(戰鬥中…) → 99
            dm3 = RATEUP_DUR_RE.search(txt[rm.end():rm.end() + 30])
            dur = int(dm3.group(1)) if dm3 else 99
            existing_rateup = next((e for e in p.get("effects", []) if e.get("k") == "rateup"), None)
            if existing_rateup is None:
                p.setdefault("effects", []).append({"k": "rateup", "who": who, "val": val, "dur": dur})
                n_rateup_tagged += 1
                rateup_tagged_names.append(f"{name}(val={val},dur={dur})")
            elif existing_rateup.get("val") != val or existing_rateup.get("dur") != dur \
                    or existing_rateup.get("who") != who:
                # 收斂既有條目(如舊版硬編碼 dur=99 的資料), 保持重跑冪等
                existing_rateup.update({"who": who, "val": val, "dur": dur})
                n_rateup_tagged += 1
                rateup_tagged_names.append(f"{name}(val={val},dur={dur},更新)")
            touched_meaningful = True

        # --- 16) chargeup(突擊戰法發動機率提升): 白名單新增, 見 CHARGEUP_ADD 註解 --------
        if name in CHARGEUP_ADD:
            spec = CHARGEUP_ADD[name]
            existing_chargeup = next((eft for eft in p.get("effects", []) if eft.get("k") == "chargeup"), None)
            want = {"k": "chargeup", "who": spec["who"], "val": spec["val"], "dur": spec["dur"]}
            if "leaderBonus" in spec:
                want["leaderBonus"] = dict(spec["leaderBonus"])
            if existing_chargeup is None:
                p.setdefault("effects", []).append(want)
                n_chargeup_tagged += 1
                chargeup_tagged_names.append(f"{name}(val={spec['val']},dur={spec['dur']})")
            elif existing_chargeup != want:
                existing_chargeup.clear()
                existing_chargeup.update(want)
                n_chargeup_tagged += 1
                chargeup_tagged_names.append(f"{name}(val={spec['val']},dur={spec['dur']},更新)")
            touched_meaningful = True

        # --- 17) 太平道法(張角S級被動): 白名單新增, 見 TAIPING_EFFECTS 註解 ---------------
        if name == "太平道法":
            existing_ks3 = [(i, e.get("k"), e.get("prepOnly")) for i, e in enumerate(p.get("effects", []))]
            want_list = [dict(eff) for eff in TAIPING_EFFECTS]
            # 冪等比對: 逐一核對「同k+同prepOnly」是否已是目標值, 不同才寫入(重跑不重複計數)
            cur_effects = p.get("effects", [])
            matched_idxs = set()
            changed = False
            for want in want_list:
                found_idx = None
                for i, k2, prep2 in existing_ks3:
                    if i in matched_idxs:
                        continue
                    if k2 == want["k"] and bool(prep2) == bool(want.get("prepOnly")):
                        found_idx = i
                        break
                if found_idx is None:
                    cur_effects.append(want)
                    changed = True
                elif cur_effects[found_idx] != want:
                    cur_effects[found_idx] = want
                    changed = True
                    matched_idxs.add(found_idx)
                else:
                    matched_idxs.add(found_idx)
            p["effects"] = cur_effects
            if p.pop("_est", None) is not None:
                changed = True                            # 移除 _est(資料已落地, 不再是估計)
            if changed:
                n_taiping_tagged += 1
            touched_meaningful = True

        # --- 19) 批8: 竭力佐謀(inheritedOnly)/三勢陣(who:leader) 白名單, 見 BATCH8_TACTIC_EFFECTS --
        if name in BATCH8_TACTIC_EFFECTS:
            cur8 = p.get("effects", [])
            changed8 = False
            for want in [dict(x) for x in BATCH8_TACTIC_EFFECTS[name]]:
                hit8 = next((i for i, e in enumerate(cur8)
                             if e.get("k") == want["k"] and e.get("who") == want["who"]), None)
                if hit8 is None:
                    cur8.append(want)
                    changed8 = True
                elif cur8[hit8] != want:
                    cur8[hit8] = want
                    changed8 = True
            p["effects"] = cur8
            if name == "竭力佐謀" and p.pop("_est", None) is not None:
                changed8 = True                           # 數值已全落地
            if changed8:
                n_batch8_tagged += 1
            touched_meaningful = True

        # --- 20) 批9: 外部查證14個自帶戰法整合, 見 BATCH9_TACTIC_EFFECTS 註解 -----------------
        # 全列表覆寫(非 add-only): 這批戰法的既有 effects 是「整批重解」年代留下的粗估, 逐條核對
        # 後決定整體結構調整(如 who 精修、mult→add 型別修正、錯誤 decay 換成 mitig 等), 用完整
        # 目標列表直接比對覆寫, 比逐條 index/k 比對更不容易在結構變動時產生殘留舊效果。
        if name in BATCH9_TACTIC_EFFECTS:
            want9 = [dict(x) for x in BATCH9_TACTIC_EFFECTS[name]]
            if p.get("effects") != want9:
                p["effects"] = want9
                n_batch9_tagged += 1
                batch9_tagged_names.append(name)
            if name in BATCH9_DROP_EST and p.pop("_est", None) is not None:
                n_batch9_tagged += 1                      # 移除 _est 也算一次變動, 供冪等統計
            touched_meaningful = True
        elif name in BATCH9_DROP_EST:
            # 符命自立: effects(既有 decay 曲線) 已是精確映射, 不動 effects 本身, 只移除 _est
            # (見 BATCH9_TACTIC_EFFECTS 上方分項註解 8, decay 優於任務指引的 amp+when 簡化版)。
            if p.pop("_est", None) is not None:
                n_batch9_tagged += 1
                batch9_tagged_names.append(f"{name}(僅移除_est)")
            touched_meaningful = True

        # --- 18) rateup/chargeup 已落地條目全庫掃描: 原文「受X影響」有寫的補 scale, 沒寫的不加 --
        # 見 RATE_SCALE_PLAN 註解(批7新增, 與 SCALE_PLAN 同保守白名單原則, 只是這次逐條核對後
        # 全部落空, 詳見腳本輸出的 skip 清單)。
        if p.get("nameZh") in RATE_SCALE_PLAN:
            for idx, scale in RATE_SCALE_PLAN[p["nameZh"]]:
                effs3 = p.get("effects", [])
                if idx < len(effs3) and effs3[idx].get("k") in ("rateup", "chargeup") \
                        and effs3[idx].get("scale") != scale:
                    effs3[idx]["scale"] = scale
                    n_rate_scale_backfilled += 1
                    rate_scale_backfilled_names.append(f"{name}[{idx}]→scale={scale}")
            touched_meaningful = True

        # --- 15) overrides 落地手動白名單(桃園結義等概數措辭 regex 抽不到的), 見 MANUAL_FILL --
        if name in MANUAL_FILL:
            spec = MANUAL_FILL[name]
            for fld in ("coef", "rate", "n"):
                if fld in spec and p.get(fld) != spec[fld]:
                    p[fld] = spec[fld]
                    n_manual_filled += 1
            existing_ks2 = {e.get("k") for e in p.get("effects", [])}
            for eff in spec.get("effects_add", []):
                if eff["k"] not in existing_ks2:
                    p.setdefault("effects", []).append(dict(eff))
                    n_manual_filled += 1
            if spec.get("force_est"):
                p["_est"] = True                       # med confidence, 依任務規格保持可見的估計標記
            touched_meaningful = True

        if not touched_meaningful:
            p["_est"] = True
            n_est += 1

    # --- 批10: 全庫驗證findings仲裁修正覆蓋層(corrections) —— 在所有既有步驟之後(最末)套用 ---
    # 見 apply_corrections() 說明。corrections 是 212 筆 findings 仲裁後的宣告式最終結果, 優先
    # 於前面任何白名單(SCALE_PLAN/BATCH8/BATCH9等)的落地值。
    corrections = load_corrections()
    n_corr_set, n_corr_effects, corrections_applied = apply_corrections(parsed, corrections)

    noop_after = sum(1 for p in parsed if p.get("type") != "none"
                      and not p.get("coef") and not p.get("effects")
                      and not p.get("choices") and not p.get("extraHits") and not p.get("everyN"))

    with open(PARSED_PATH, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print("=== effectText 回填報告 ===")
    print(f"coef 回填: {n_coef_filled}")
    print(f"rate 回填: {n_rate_filled}")
    print(f"n(目標數) 回填: {n_n_filled}")
    print(f"dur(持續回合) 回填: {n_dur_filled}")
    print(f"heal coef 回填: {n_heal_filled}")
    print(f"新增控制/先攻原語標記(silence/disarm/insight/first): {n_new_ctrl_tags}")
    print(f"先攻(first) 標記戰法: {', '.join(first_tagged) if first_tagged else '無'}")
    print(f"--- 批2: 條件觸發 + 新原語 ---")
    print(f"when(條件觸發回合窗口/反應式) 回填: {n_when_filled} 個戰法"
          f"（from: {sorted(WHEN_FROM)}；until: {sorted(WHEN_UNTIL)}；"
          f"on=attacked: {sorted(ON_ATTACKED)}；on=damaged: {sorted(ON_DAMAGED)}）")
    print(f"rate 白名單覆寫: {sorted(RATE_OVERRIDE.items())}")
    print(f"taunt(嘲諷) 標記: {n_taunt_tagged} 個戰法（{sorted(TAUNT_DUR)}）"
          f"（定謀貴決 跳過: 反向嘲諷/集火語意, 與 taunt 方向相反）")
    print(f"dodge(規避) 標記: {n_dodge_tagged} 個戰法（新增: {sorted(DODGE_ADD)}；"
          f"修正mitig誤標: {sorted(DODGE_FIX_MITIG)}）")
    print(f"surehit(必中) 標記: {n_surehit_tagged} 個戰法（{sorted(SUREHIT_ADD)}）")
    print(f"shield(護盾) 標記: {n_shield_tagged} 個戰法（{sorted(SHIELD_ADD) if SHIELD_ADD else '無, 全庫僅赴湯蹈火提及但無具體吸收量數字可抽, 保守跳過'}）")
    print(f"--- 批2.5: 受屬性影響縮放 + rateup + overrides ---")
    print(f"scale(受屬性影響縮放) 標記: {n_scale_tagged} 處效果, 涵蓋 {len(SCALE_PLAN)} 個戰法")
    print(f"rateup(主動戰法發動機率提升) 標記: {n_rateup_tagged} 個戰法"
          f"（{', '.join(rateup_tagged_names) if rateup_tagged_names else '無'}）")
    print(f"--- 批6: chargeup(突擊戰法發動機率提升) + 曹純特例 ---")
    print(f"chargeup(突擊戰法發動機率提升) 標記: {n_chargeup_tagged} 個戰法"
          f"（{', '.join(chargeup_tagged_names) if chargeup_tagged_names else '無'}）"
          f"；跳過: 三勢陣(僅限「主將」概念, 引擎無通用主將機制)"
          f"、經天緯地(proc-on-cast觸發鏈, 非chargeup原語)——詳見 CHARGEUP_ADD 註解")
    print(f"批8 白名單(竭力佐謀 inheritedOnly / 三勢陣 who:leader): {n_batch8_tagged} 個戰法套用/更新(0=已是目標值冪等)")
    print(f"批9 白名單(14個自帶戰法外部查證整合): {n_batch9_tagged} 個戰法套用/更新"
          f"（{', '.join(batch9_tagged_names) if batch9_tagged_names else '無變動(冪等)'}）"
          f"；移除_est: {sorted(BATCH9_DROP_EST)}；仍保留_est(缺口見BATCH9_TODO): "
          f"{sorted(set(BATCH9_TACTIC_EFFECTS) - BATCH9_DROP_EST)}")
    print(f"--- 批7: 發動率縮放(rate-scale) + 太平道法落地 ---")
    print(f"太平道法 白名單落地: {'已套用/更新' if n_taiping_tagged else '已是目標值(無變動, 冪等)'}"
          f"（amp 0.28 + rateup×2〔一般6% nativeOnly + 準備戰法額外6% prepOnly+nativeOnly, "
          f"皆 scale=intel〕；黃巾軍主將擴散條款 _todo 跳過, 見 TAIPING_EFFECTS 註解）")
    _rsb_skip_note = ("無, 逐條核對白眉/先成其慮/獅子奮迅/進言/虎豹騎/陷陣突襲後全部跳過"
                       "(原文發動率子句本身未寫「受X影響」, 或已用專屬曲線如虎豹騎曹純特例)"
                       "——詳見 RATE_SCALE_PLAN 註解")
    print(f"rateup/chargeup 已落地條目全庫掃描補 scale: {n_rate_scale_backfilled} 處"
          f"（{', '.join(rate_scale_backfilled_names) if rate_scale_backfilled_names else _rsb_skip_note}）")
    print("nativeOnly 適用範圍(user 釐清, 嚴格按原文措辭二分——詳見 RATE_SCALE_PLAN 下方註解): "
          "nativeOnly組=太平道法(戰法)+武聖/天機/虎峙/喬裝(裝備, 旗標既有已合規)；"
          "全主動組=白眉/先成其慮/獅子奮迅/進言(戰法)+樂奏/回響(裝備), 皆無旗標合規；"
          "拿不準跳過=竭力佐謀(「非自帶」為反向旗標, 無原語)")
    print(f"overrides(查證資料整合) 套用 effectText/type: {len(overrides_applied)} 筆"
          f"（{', '.join(sorted(overrides_applied)) if overrides_applied else '無'}）")
    print(f"overrides invalid(幽靈條目→type:none): {n_overrides_invalid} 筆"
          f"（{', '.join(sorted(overrides_invalid)) if overrides_invalid else '無'}）")
    print(f"overrides type:none(內政類等): {n_overrides_type_none} 筆")
    print(f"overrides 手動落地(概數措辭 regex 抽不到, 見 MANUAL_FILL): {n_manual_filled} 個欄位/效果"
          f"（{', '.join(sorted(MANUAL_FILL))}）")
    print(f"no-op 戰法: {noop_before} -> {noop_after}（口徑: before 在 overrides type:none 生效前計, 與 HEAD 直接比較一致）")
    print(f"抽不到數字, 標記 _est=true 的戰法數: {n_est}")
    print(f"--- 批10: 全庫驗證findings仲裁修正覆蓋層(tactic_corrections.json) ---")
    print(f"corrections 套用: {len(corrections)} 筆定義, {len(corrections_applied)} 個戰法本輪套用時值有異動"
          f"（set欄位變動 {n_corr_set} 處, effects整組替換 {n_corr_effects} 個戰法）"
          f"（{', '.join(sorted(corrections_applied)) if corrections_applied else '無變動'}）"
          f"（注意: 此計數為「套用前(經前述步驟1-20處理後的中間值) vs 套用後」的差異, corrections 對某些"
          f"戰法會覆寫掉前面步驟依 raw effectText/effectTarget 重新推導的中間值(如 3), 最終落地成仲裁值"
          f"(如 2); 這是設計上每輪都會發生的正常覆寫, 不代表輸出檔案不穩定——實際冪等性以輸出檔案 byte-diff"
          f"是否為空為準(已驗證兩輪 diff 皆空), 不是以此計數是否為0為準）")


if __name__ == "__main__":
    main()
