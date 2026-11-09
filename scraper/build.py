"""scraped/ の CSV をサイト用データ（data/<年>.json, data/dict.json）に変換する。

data/<年>.json は1件9個の数値を並べた配列:
  区分(0:男長 1:男短 2:女長 3:女短), 順位, 名前番号, 年齢, 種目番号, 所属番号, 大会番号, タイム(1/100秒), ポイント
番号は data/dict.json の names / clubs / meets / evs の位置。
既存の番号は変えずに、新しい名前などは末尾に追加する（他の年のデータがそのまま使えるように）。
"""
import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SCRAPED = ROOT / "scraped"


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()   # 全角スペースも半角1つに


def to_int(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else 0


def to_cs(s):
    """'54.45' / '4:29.58' / '1:02:03.45' → 1/100秒"""
    parts = (s or "").strip().split(":")
    sec = float(parts[-1])
    mins = int(parts[-2]) if len(parts) > 1 else 0
    hrs = int(parts[-3]) if len(parts) > 2 else 0
    return round((hrs * 3600 + mins * 60 + sec) * 100)


class Index:
    def __init__(self, items):
        self.items = items
        self.pos = {v: i for i, v in enumerate(items)}

    def __call__(self, v):
        if v not in self.pos:
            self.pos[v] = len(self.items)
            self.items.append(v)
        return self.pos[v]


def main():
    metas = sorted(SCRAPED.glob("*.meta.json"))
    if not metas:
        print("取得データがありません")
        return
    by_year = {}
    for m in metas:
        meta = json.loads(m.read_text(encoding="utf-8"))
        if not meta.get("complete"):
            sys.exit(f"{m.name} はお試し取得（途中まで）なので反映しません")
        by_year.setdefault(meta["year"], {})[meta["group"]] = meta

    D = json.loads((DATA / "dict.json").read_text(encoding="utf-8"))
    names, clubs, meets, evs = (Index(D[k]) for k in ("names", "clubs", "meets", "evs"))

    changed = False
    for year, groups in sorted(by_year.items()):
        if sorted(groups) != [0, 1, 2, 3]:
            sys.exit(f"{year}年の4区分がそろっていません: {sorted(groups)}")
        old = {}
        f = DATA / f"{year}.json"
        if f.exists():
            a = json.loads(f.read_text())
            for i in range(0, len(a), 9):
                old[a[i]] = old.get(a[i], 0) + 1

        out = []
        for g in range(4):
            with open(SCRAPED / f"{year}_{g}.csv", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            # 安全確認：件数が大きく減っていたら取得ミスとみなして止める
            if old.get(g) and len(rows) < old[g] * 0.95:
                sys.exit(f"{year}年 区分{g}: 件数が減っています（{old.get(g)} → {len(rows)}）。反映を中止します")
            for r in rows:
                out += [g, to_int(r["rank"]), names(clean(r["name"])), to_int(r["age"]),
                        evs(clean(r["event"])), clubs(clean(r["club"])), meets(clean(r["meet"])),
                        to_cs(r["time"]), to_int(r["points"])]
            print(f"{year}年 区分{g}: {old.get(g, 0)} → {len(rows)} 件")
        f.write_text(json.dumps(out, separators=(",", ":")))
        if year not in D["years"]:
            D["years"].append(year)
        changed = True

    if changed:
        D["years"] = sorted(D["years"])
        D["updated"] = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        (DATA / "dict.json").write_text(json.dumps(D, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print("data/ を更新しました")


if __name__ == "__main__":
    main()
