# -*- coding: utf-8 -*-
"""把肖像 + 武將資料合成「華麗中國風」卡片。
run: python card.py 呂布   (需先有 cards/portraits/<name>.png)
"""
import json
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.join(HERE, "cards", "portraits")
OUT = os.path.join(HERE, "cards", "out")
GOLD = (212, 175, 55)
FAC_COLOR = {"魏": (70, 110, 190), "蜀": (60, 160, 90),
             "吳": (200, 70, 70), "群": (150, 110, 180)}


def font(sz):
    for f in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\simhei.ttf"):
        if os.path.exists(f):
            return ImageFont.truetype(f, sz)
    return ImageFont.load_default()


def _best_troop(apt):
    rank = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}
    m = {"騎": "騎", "盾": "盾", "弓": "弓", "槍": "槍", "器": "器"}
    best = max(apt, key=lambda k: rank.get(apt.get(k), -1), default="騎")
    return m.get(best, "騎"), apt.get(best, "?")


def make_card(g):
    name = g["name"]
    p = os.path.join(PORT, name + ".png")
    if not os.path.exists(p):
        return None
    img = Image.open(p).convert("RGB").resize((832, 1216))
    W, H = img.size
    d = ImageDraw.Draw(img, "RGBA")

    # 底部漸層暗化(放文字)
    panel = 360
    grad = Image.new("L", (1, panel), 0)
    for y in range(panel):
        grad.putpixel((0, y), int(235 * (y / panel) ** 1.2))
    mask = grad.resize((W, panel))
    dark = Image.new("RGBA", (W, panel), (10, 8, 6, 255))
    dark.putalpha(mask)
    img.paste(Image.new("RGB", (W, panel), (10, 8, 6)), (0, H - panel), dark)

    # 金色雙框
    for off, wd in ((14, 4), (24, 1)):
        d.rectangle([off, off, W - off, H - off], outline=GOLD, width=wd)

    fac = g.get("faction", "?")
    fc = FAC_COLOR.get(fac, GOLD)
    troop, grade = _best_troop(g.get("affinity", {}))
    st = g.get("stats", {})

    # 左上: 勢力印 + 星級
    d.ellipse([34, 34, 110, 110], fill=fc + (230,), outline=GOLD, width=3)
    fn = font(46)
    d.text((72, 72), fac, font=fn, fill=(255, 255, 255), anchor="mm")
    d.text((124, 50), "★" * int(g.get("stars", 5)), font=font(28), fill=GOLD)

    # 名稱
    d.text((W // 2, H - panel + 30), name, font=font(72), fill=GOLD, anchor="mt",
           stroke_width=2, stroke_fill=(40, 20, 0))
    d.text((W // 2, H - panel + 120), f"{fac}  ·  {troop}兵({grade})  ·  {g.get('tactic') or '—'}",
           font=font(34), fill=(235, 220, 180), anchor="mt")

    # 六維
    stats = [("武", st.get("武力")), ("智", st.get("智力")),
             ("統", st.get("統率")), ("速", st.get("速度"))]
    bx, by, gap = 96, H - 150, 170
    for i, (lab, val) in enumerate(stats):
        x = bx + i * gap
        d.text((x, by), lab, font=font(36), fill=GOLD, anchor="mm")
        d.text((x, by + 52), f"{val:.0f}" if val else "—", font=font(44),
               fill=(255, 255, 255), anchor="mm")

    os.makedirs(OUT, exist_ok=True)
    dest = os.path.join(OUT, name + ".png")
    img.save(dest)
    return dest


if __name__ == "__main__":
    import sys
    G = {g["name"]: g for g in json.load(open(os.path.join(HERE, "data", "generals.json"),
                                              encoding="utf-8"))}
    name = sys.argv[1] if len(sys.argv) > 1 else "呂布"
    print(make_card(G[name]))
