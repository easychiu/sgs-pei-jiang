# -*- coding: utf-8 -*-
"""回寫戰法本文到 data/tactics.json（全文庫）。

規則（user 2026-07-09）:
  定稿本文必須回寫 raw 全文庫，方便 diff 對照。

來源優先序（高→低）:
  1) docs/data/tactics_overrides.json 的 effectText（invalid 略過）
  2) docs/data/tactic_corrections.json 條目的 effectText
  3) 同檔 _evidence —— 僅當判定為「可取代全文」時才寫入（避免片段摘錄蓋掉完整句）

可取代全文判定:
  - 長度 >= 80
  - 且（新文不比舊文短於 85%，或舊文含多版本雜訊標記）
  - 多版本雜訊: 「原始版本」「更新」「/  /」等

用法:
  python sync_tactics_effecttext.py
  python sync_tactics_effecttext.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW_PATH = os.path.join(ROOT, "data", "tactics.json")
RAW_BACKUP = os.path.join(ROOT, "data", "tactics_backup.json")
OVERRIDES_PATH = os.path.join(ROOT, "docs", "data", "tactics_overrides.json")
CORRECTIONS_PATH = os.path.join(ROOT, "docs", "data", "tactic_corrections.json")

MULTI_VER_RE = re.compile(
    r"原始版本|更新（|更新\(|\d{4}→\d{2}→\d{2}|/\s*/|舊版|新版本"
)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data, indent=2):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.write("\n")


def collect_overrides(doc):
    """name -> full effectText from overrides layer."""
    out = {}
    nested = doc.get("overrides") or {}
    for name, ov in nested.items():
        if not isinstance(ov, dict) or ov.get("invalid"):
            continue
        t = (ov.get("effectText") or "").strip()
        if t:
            out[name] = t
    # top-level accidental keys (e.g. 暗箭難防)
    for name, ov in doc.items():
        if name in ("_note", "overrides") or not isinstance(ov, dict):
            continue
        if ov.get("invalid"):
            continue
        t = (ov.get("effectText") or "").strip()
        if t:
            out[name] = t
    return out


def collect_corrections(doc):
    """name -> (text, source_field, force_full)."""
    out = {}
    # nested main map
    nested = doc.get("corrections") or {}
    items = dict(nested)
    # top-level skill keys (recent batch writes)
    for name, entry in doc.items():
        if str(name).startswith("_") or name == "corrections":
            continue
        if isinstance(entry, dict):
            items[name] = entry

    for name, entry in items.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("effectText"):
            out[name] = (entry["effectText"].strip(), "effectText", True)
        elif entry.get("_evidence"):
            out[name] = (entry["_evidence"].strip(), "_evidence", False)
    return out


def is_replaceable(new: str, old: str, force: bool) -> bool:
    """force=True（overrides / 顯式 effectText）可任意覆寫。
    _evidence 僅在「夠長且不比舊文短 15% 以上」時回寫，避免片段摘錄砍掉全文。"""
    if not new:
        return False
    if force:
        return True
    if len(new) < 80:
        return False
    if not old:
        return True
    if new == old:
        return False
    # 非 force：禁止明顯縮短（_evidence 多為子句摘錄）
    if len(new) >= int(len(old) * 0.85):
        return True
    return False


def merge_canon(overrides, corrections):
    """name -> (text, source_tag)."""
    canon = {}
    # lower priority first
    for name, (text, field, force) in corrections.items():
        canon[name] = (text, f"corrections.{field}", force)
    for name, text in overrides.items():
        canon[name] = (text, "overrides.effectText", True)
    return canon


def apply_to_raw(raw_list, canon, dry_run=False):
    changed = []
    skipped = []
    missing = []
    by_name = {t.get("nameZh"): t for t in raw_list if t.get("nameZh")}
    for name, (text, src, force) in sorted(canon.items()):
        t = by_name.get(name)
        if not t:
            missing.append(name)
            continue
        old = (t.get("effectText") or "").strip()
        if old == text:
            continue
        if not is_replaceable(text, old, force):
            skipped.append((name, src, len(text), len(old)))
            continue
        if not dry_run:
            t["effectText"] = text
        changed.append((name, src, len(old), len(text)))
    return changed, skipped, missing


def fold_toplevel_corrections(doc):
    """Move top-level skill keys into corrections{} for schema cleanliness."""
    nested = doc.setdefault("corrections", {})
    moved = []
    for name in list(doc.keys()):
        if str(name).startswith("_") or name == "corrections":
            continue
        entry = doc[name]
        if not isinstance(entry, dict):
            continue
        # merge into nested (top wins for effectText/_evidence)
        base = dict(nested.get(name) or {})
        base.update(entry)
        if entry.get("_evidence") and not base.get("effectText"):
            # promote full user evidence to effectText when force-worthy
            ev = entry["_evidence"].strip()
            if len(ev) >= 80 or entry.get("_fullText"):
                base["effectText"] = ev
        nested[name] = base
        del doc[name]
        moved.append(name)
    return moved


def main():
    ap = argparse.ArgumentParser(description="回寫戰法本文到 data/tactics.json")
    ap.add_argument("--dry-run", action="store_true", help="只報告不寫檔")
    args = ap.parse_args()

    ov = load_json(OVERRIDES_PATH)
    corr = load_json(CORRECTIONS_PATH)
    raw = load_json(RAW_PATH)

    overrides = collect_overrides(ov)
    corrections = collect_corrections(corr)
    canon = merge_canon(overrides, corrections)

    changed, skipped, missing = apply_to_raw(raw, canon, dry_run=args.dry_run)

    print("=== sync_tactics_effecttext ===")
    print(f"canon sources: overrides={len(overrides)} corrections_text={len(corrections)} merged={len(canon)}")
    print(f"would_change/changed: {len(changed)}")
    for name, src, ol, nl in changed[:40]:
        print(f"  ✓ {name}  [{src}]  {ol}→{nl} chars")
    if len(changed) > 40:
        print(f"  ... +{len(changed) - 40} more")
    print(f"skipped (excerpt risk): {len(skipped)}")
    for name, src, nl, ol in skipped[:15]:
        print(f"  · {name}  [{src}]  new={nl} old={ol}")
    if missing:
        print(f"missing in raw: {missing}")

    if args.dry_run:
        print("[dry-run] no files written")
        return 0

    save_json(RAW_PATH, raw, indent=2)
    if os.path.exists(RAW_BACKUP):
        bak = load_json(RAW_BACKUP)
        apply_to_raw(bak, canon, dry_run=False)
        save_json(RAW_BACKUP, bak, indent=2)

    # fold top-level correction keys + promote effectText for user-full entries
    moved = fold_toplevel_corrections(corr)
    # ensure 暗箭 etc have effectText in nested
    for name in moved:
        e = corr["corrections"].get(name) or {}
        if e.get("_evidence") and not e.get("effectText"):
            ev = e["_evidence"].strip()
            if len(ev) >= 80 or name in ("暗箭難防", "垂心萬物", "機鑑先識", "五雷轟頂", "太平道法", "高櫓連營", "眾望所歸"):
                e["effectText"] = ev
                corr["corrections"][name] = e
    # stamp policy note once
    note = corr.get("_note") or ""
    stamp = "【本文回寫】定稿 effectText/_evidence 必須回寫 data/tactics.json（sync_tactics_effecttext.py）。"
    if stamp not in note:
        corr["_note"] = note + " " + stamp
    save_json(CORRECTIONS_PATH, corr, indent=1)

    # overrides note stamp
    onote = ov.get("_note") or ""
    if stamp not in onote:
        ov["_note"] = onote + " " + stamp
    # move top-level 暗箭 into overrides map if present
    for name in list(ov.keys()):
        if name in ("_note", "overrides"):
            continue
        if isinstance(ov[name], dict) and ov[name].get("effectText"):
            ov.setdefault("overrides", {})[name] = ov[name]
            del ov[name]
    save_json(OVERRIDES_PATH, ov, indent=1)

    print(f"wrote {RAW_PATH}")
    print(f"wrote {RAW_BACKUP}" if os.path.exists(RAW_BACKUP) else "")
    print(f"folded top-level corrections: {moved}")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
