# -*- coding: utf-8 -*-
"""ComfyUI txt2img 驅動: 為武將生肖像(華麗中國風)。
run: python comfy_gen.py 呂布   # 單張試水
"""
import json
import os
import time
import urllib.parse
import urllib.request

HOST = "http://127.0.0.1:8188"
CKPT = "animagine-xl-4.0.safetensors"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cards", "portraits")
NEG = ("lowres, bad anatomy, bad hands, text, watermark, signature, error, "
       "missing fingers, extra digit, cropped, worst quality, low quality, "
       "jpeg artifacts, blurry, deformed, ugly, modern clothing")


def workflow(pos, seed):
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": pos, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 832, "height": 1216, "batch_size": 1}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": 28, "cfg": 5.5,
                         "sampler_name": "euler_ancestral", "scheduler": "karras",
                         "denoise": 1.0, "model": ["4", 0], "positive": ["6", 0],
                         "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "sgz", "images": ["8", 0]}},
    }


def _post(path, data):
    req = urllib.request.Request(HOST + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _get(path):
    return json.loads(urllib.request.urlopen(HOST + path, timeout=30).read())


def generate(pos, name, seed, timeout=180):
    os.makedirs(OUT, exist_ok=True)
    pid = _post("/prompt", {"prompt": workflow(pos, seed)})["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        hist = _get(f"/history/{pid}")
        if pid in hist:
            imgs = hist[pid]["outputs"]["9"]["images"][0]
            q = urllib.parse.urlencode({"filename": imgs["filename"],
                                        "subfolder": imgs["subfolder"], "type": imgs["type"]})
            data = urllib.request.urlopen(f"{HOST}/view?{q}", timeout=30).read()
            dest = os.path.join(OUT, f"{name}.png")
            open(dest, "wb").write(data)
            return dest
        time.sleep(2)
    raise TimeoutError(f"{name} 生成逾時")


# 華麗中國風 提示詞模板
STYLE = ("masterpiece, best quality, highly detailed, intricate, ornate, "
         "traditional chinese aesthetic, gold filigree, luxurious, dramatic lighting, "
         "chinese ink painting background, character portrait, upper body")


def prompt_for(name, gender, troop, faction, role):
    weap = {"騎": "on horseback", "盾": "holding a large shield", "弓": "holding an ornate bow",
            "槍": "holding a long spear", "器": "with siege weaponry"}.get(troop, "")
    fac = {"魏": "blue and silver armor", "蜀": "green and gold armor",
           "吳": "red and crimson armor", "群": "dark ornate armor"}.get(faction, "ornate armor")
    g = "1girl, beautiful" if gender == "f" else "1boy, majestic"
    return (f"{STYLE}, {g}, ancient chinese {role} {name}, Three Kingdoms era, "
            f"{fac}, {weap}, elaborate engravings, flowing robes")


if __name__ == "__main__":
    import sys
    import urllib.parse
    name = sys.argv[1] if len(sys.argv) > 1 else "呂布"
    p = prompt_for(name, "m", "騎", "群", "general warrior")
    print("prompt:", p)
    print("生成中...", generate(p, name, seed=12345))
