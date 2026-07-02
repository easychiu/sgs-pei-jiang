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
    noop_before = sum(1 for p in parsed if p.get("type") != "none"
                       and not p.get("coef") and not p.get("effects"))

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
                if e.get("k") in ("first", "taunt", "dodge", "surehit"):
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

    noop_after = sum(1 for p in parsed if p.get("type") != "none"
                      and not p.get("coef") and not p.get("effects"))

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
    print(f"overrides(查證資料整合) 套用 effectText/type: {len(overrides_applied)} 筆"
          f"（{', '.join(sorted(overrides_applied)) if overrides_applied else '無'}）")
    print(f"overrides invalid(幽靈條目→type:none): {n_overrides_invalid} 筆"
          f"（{', '.join(sorted(overrides_invalid)) if overrides_invalid else '無'}）")
    print(f"overrides type:none(內政類等): {n_overrides_type_none} 筆")
    print(f"overrides 手動落地(概數措辭 regex 抽不到, 見 MANUAL_FILL): {n_manual_filled} 個欄位/效果"
          f"（{', '.join(sorted(MANUAL_FILL))}）")
    print(f"no-op 戰法: {noop_before} -> {noop_after}（口徑: before 在 overrides type:none 生效前計, 與 HEAD 直接比較一致）")
    print(f"抽不到數字, 標記 _est=true 的戰法數: {n_est}")


if __name__ == "__main__":
    main()
