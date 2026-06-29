# -*- coding: utf-8 -*-
"""全量生成 193 武將卡片: ComfyUI 肖像 + 合成卡。可續傳(跳過已存在)。
run: python batch_cards.py
"""
import json
import os

import card
import comfy_gen

HERE = os.path.dirname(os.path.abspath(__file__))
G = json.load(open(os.path.join(HERE, "data", "generals.json"), encoding="utf-8"))

# 已知女性武將(去 SP/空白比對)
FEMALE = {"貂蟬", "孫尚香", "大喬", "小喬", "蔡文姬", "甄姬", "甄宓", "祝融", "祝融夫人",
          "王元姬", "步練師", "董白", "張春華", "王異", "卞夫人", "鄒氏", "樊氏", "馬雲騄",
          "辛憲英", "關銀屏", "花鬘", "吳國太", "黃月英", "孫魯班", "丁氏", "杜夫人",
          "曹節", "郭女王", "謝道韞", "李姬", "陳宮夫人", "鄭氏"}


def gender(name):
    base = name.replace("SP", "").replace(" ", "")
    return "f" if base in FEMALE else "m"


def role(g, gen):
    st = g.get("stats", {})
    f, i = st.get("武力", 0) or 0, st.get("智力", 0) or 0
    if gen == "f":
        return "elegant lady warrior" if f >= 80 else "beautiful court lady"
    if i > f + 20:
        return "wise strategist advisor"
    if f > i + 20:
        return "fierce general warrior"
    return "noble military general"


def best_troop(apt):
    rank = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}
    return max(apt, key=lambda k: rank.get(apt.get(k), -1), default="騎") if apt else "騎"


def main():
    done = err = 0
    for k, g in enumerate(G):
        name = g["name"]
        if os.path.exists(os.path.join(card.OUT, name + ".png")):
            done += 1
            continue
        gen = gender(name)
        troop = best_troop(g.get("affinity", {}))
        prompt = comfy_gen.prompt_for(name.replace("SP ", ""), gen, troop,
                                      g.get("faction", "群"), role(g, gen))
        seed = (hash(name) % 2_000_000_000) + 1     # 穩定種子(同名重跑一致)
        try:
            comfy_gen.generate(prompt, name, seed)
            card.make_card(g)
            done += 1
        except Exception as e:
            err += 1
            print(f"  ! {name}: {e}")
        if (k + 1) % 10 == 0:
            print(f"  進度 {k + 1}/{len(G)}  (完成 {done}, 失敗 {err})")
    print(f"完成 {done}/{len(G)} 張卡片, 失敗 {err} → cards/out/")


if __name__ == "__main__":
    main()
