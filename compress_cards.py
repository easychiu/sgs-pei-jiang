# -*- coding: utf-8 -*-
"""壓縮卡片給網頁: cards/out/*.png → docs/cards/*.webp(寬500, q80)。可續傳。
run: python compress_cards.py"""
import os
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "cards", "out")
DST = os.path.join(HERE, "docs", "cards")
W = 500

os.makedirs(DST, exist_ok=True)
n = skip = 0
for f in os.listdir(SRC):
    if not f.endswith(".png"):
        continue
    out = os.path.join(DST, f[:-4] + ".webp")
    if os.path.exists(out):
        skip += 1
        continue
    im = Image.open(os.path.join(SRC, f)).convert("RGB")
    im = im.resize((W, round(W * im.height / im.width)))
    im.save(out, "WEBP", quality=80, method=6)
    n += 1
print(f"壓縮 {n} 張, 跳過 {skip} → docs/cards/ ({len(os.listdir(DST))} 張)")
