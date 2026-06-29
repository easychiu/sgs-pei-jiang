# 三國志戰略版 配將引擎(國際/台服)

武將配將評分 + 逐回合模擬對戰。資料爬自台服社群站 [sgsdeck.com](https://sgsdeck.com)
(193 武將 / 384 戰法),傷害用社群拆解的公式。**非官方、純學習用途。**

## 用法

```bash
python sgz.py                       # demo: 載入摘要 + Top8 推薦 + 一場模擬
python sgz.py rec [勢力]            # 配將推薦, 可選 魏/蜀/吳/群 過濾
python sgz.py sim "呂布,趙雲,關羽" "諸葛亮,周瑜,司馬懿"   # 模擬兩隊 (3000 場勝率)
python sgz.py info 諸葛亮           # 查武將面板 + 自帶戰法解析
python sgz.py test                  # 跑自檢
```
Windows 主控台中文亂碼時加 `PYTHONUTF8=1`。

## 檔案

| 檔案 | 用途 |
|------|------|
| `sgz.py` | 引擎: 載入 / 評分 / 推薦 / 模擬 / CLI |
| `scrape.py` | sgsdeck RSC 頁面解析器(抽內嵌 JSON) |
| `build_dataset.py` | 爬 193 武將詳細頁 → `data/generals.json` + `data/tactics.json` |
| `merge_tactics.py` | 合併 LLM 解析的戰法分片 → `data/tactics_parsed.json` |
| `data/generals.json` | 193 武將: 勢力 / Lv50 六維 / 5 兵種適性 / 自帶戰法 |
| `data/tactics.json` | 384 戰法原始資料(sgsdeck schema, 含 effectText) |
| `data/tactics_parsed.json` | 384 戰法 → 引擎效果(LLM 解析 effectText) |
| `data/formula.md` | 傷害/克制/士氣/適性 公式常數彙整(附來源) |
| `data/reference/` | sgsdeck schema、武將總表、原始樣本 |

引擎優先讀 `tactics_parsed.json`(LLM 解析);缺檔時退回 `sgz.py` 內的正則啟發式。

## 戰鬥模型

傷害公式(社群拆解,見 `data/formula.md`):
```
傷害 = ((攻-防)/150 + 1) × (兵力/20) × 戰法係數
       × 兵種克制 × 士氣係數 × 增傷 × (1-減傷) × random(0.96~1.04)
```
- 兵刃: 攻=武力, 防=敵統率;謀略: 攻=智力, 防=敵智力
- 兵種克制 騎>盾>弓>槍>騎(器械全被克):克制 ×1.15 / 被克 ×0.85
- 兵種適性 S/A/B/C/D → 屬性發揮 120/100/85/70/55%
- 逐回合、速度排序行動;主動戰法依發動率、突擊普攻後觸發、指揮/被動穩定
- 戰法效果原語: 增傷(amp)/減傷(mitig)/控制(stun)/治療(heal)/屬性增減(stat)/
  持續傷害(dot)/結算傷害(settle, 猛毒)/連擊(extra)/傷害轉移(redirect, 代承)/
  疊加增益(stack, 每回合+1層越打越強)/衰減增益(decay, 開場強逐回合歸零)/武智互換(swap)/
  看破(pierce, 無視目標減傷)/反擊(counter, 受擊還擊)

## 已知近似(精度天花板)

引擎是**趨勢正確的近似**,非逐幀還原。明知的失真:

- **條件/多模式戰法**:如「敵出主動時觸發」「第5回合起」「奇偶回合切換」無法判條件,
  指揮/被動的傷害用全域觸發折扣近似(`CMD_TRIGGER` / `PASSIVE_TRIGGER` 旋鈕)。
- **DoT(中毒/灼燒/水攻)** 已支援(套用時定格每回合傷害, 單條近似多狀態獨立疊加)。
- **結算傷害(猛毒)** 已支援(層數累積+延遲群體爆發);淨化提前觸發、蠻族額外加層 未建模(任意受擊+1層近似)。
- **連擊/疊加/衰減/代承/武智互換/看破/反擊** 均已有對應原語(見上)。
- 仍只能粗略折算(非乾淨原語):隨機狀態(以 amp+連擊近似)、多模式隔回合切換(取主要一模式)。
- **未建模**:緣分、兵書、裝備、洗練、準備回合的占用、士氣行軍衰減(固定滿)。
- 部分戰法數值經小弟上網考據修正, 帶 `_src`/`_real`/`_conf` 註記(見 tactics_parsed.json)。
  `_conf=med` 者數值未必精準, 以官方面板為準。
- 武將屬性取 Lv50,雙方同級(等級壓制略過)。

要更準,就逐戰法精修 `data/tactics_parsed.json`、補上述機制。所有數值都在 JSON,
改資料不必動引擎。校準旋鈕在 `sgz.py` 頂部。
