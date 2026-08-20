#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
연관키워드 수집 (주 1회) — SearchAd keywordstool

출력
  keywords_pool.json  : 카테고리별 상업성 통과 키워드 풀
  pool_history.json   : 회차별 검색량 스냅샷 (성장률 계산용, 최근 8회 보관)
"""
import json, os, re, time, hmac, hashlib, base64, urllib.request, urllib.parse, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commercial_filter import is_commercial, MIN_VOL, MIN_ADS  # noqa

API = os.environ["NAVER_API_KEY"]
SEC = os.environ["NAVER_SECRET"]
CUS = os.environ["NAVER_CUSTOMER"]

CAP = 400           # 카테고리당 보관 상한
HIST_KEEP = 8       # 스냅샷 보관 회차


def sign(ts):
    msg = ts + ".GET./keywordstool"
    return base64.b64encode(
        hmac.new(SEC.encode(), msg.encode(), hashlib.sha256).digest()).decode()


def num(v, default=0):
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v).replace("<", "").replace(",", "").strip())
    except Exception:
        return default


def fetch(seeds):
    hints = ",".join(s.replace(" ", "") for s in seeds)
    ts = str(round(time.time() * 1000))
    q = urllib.parse.urlencode({"hintKeywords": hints, "showDetail": "1"})
    req = urllib.request.Request(
        "https://api.searchad.naver.com/keywordstool?" + q,
        headers={"X-Timestamp": ts, "X-API-KEY": API,
                 "X-Customer": CUS, "X-Signature": sign(ts)})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r).get("keywordList", [])


def ads_of(item):
    # 월평균 노출 광고수 — 문서/응답에 따라 필드명이 갈려서 둘 다 본다
    for k in ("plAvgDepth", "plclAdCnt", "plAvgDept"):
        if k in item:
            return num(item[k], 0)
    return 0


def main():
    seedmap = json.load(open("seeds.json"))
    pool, seen, errors = {}, set(), []
    dropped = 0

    for cat, info in seedmap.items():
        rows = {}
        seeds = info["seeds"]
        for i in range(0, len(seeds), 5):
            batch = seeds[i:i + 5]
            try:
                items = fetch(batch)
            except Exception as e:
                errors.append("%s %s: %s" % (cat, batch, e))
                time.sleep(1.5)
                continue
            for it in items:
                kw = str(it.get("relKeyword", "")).strip()
                if not kw or kw in seen or kw in rows:
                    continue
                vol = num(it.get("monthlyPcQcCnt")) + num(it.get("monthlyMobileQcCnt"))
                if vol < MIN_VOL:
                    continue
                ads = ads_of(it)
                if not is_commercial(kw, ads, cat):
                    dropped += 1
                    continue
                rows[kw] = {
                    "kw": kw,
                    "vol": vol,
                    "ads": ads,
                    "comp": str(it.get("compIdx", "")),
                    "ctr": round(float(it.get("monthlyAveMobileCtr") or 0), 3),
                }
            time.sleep(0.45)

        top = sorted(rows.values(), key=lambda x: -x["vol"])[:CAP]
        for r in top:
            seen.add(r["kw"])
        pool[cat] = top
        print(cat, "raw", len(rows), "-> kept", len(top))

    stamp = time.strftime("%Y-%m-%d %H:%M UTC")
    json.dump({"updated": stamp, "min_vol": MIN_VOL, "min_ads": MIN_ADS,
               "errors": errors, "categories": pool},
              open("keywords_pool.json", "w"), ensure_ascii=False)

    # ── 성장률 계산용 스냅샷 누적 ───────────────────────────────
    try:
        hist = json.load(open("pool_history.json"))
    except Exception:
        hist = {"snapshots": []}
    snap = {"at": stamp, "vol": {}}
    for cat, rows in pool.items():
        for r in rows:
            snap["vol"][r["kw"]] = r["vol"]
    hist["snapshots"] = (hist.get("snapshots", []) + [snap])[-HIST_KEEP:]
    json.dump(hist, open("pool_history.json", "w"), ensure_ascii=False)

    total = sum(len(v) for v in pool.values())
    print("total", total, "| dropped(non-commercial)", dropped,
          "| errors", len(errors), "| snapshots", len(hist["snapshots"]))


if __name__ == "__main__":
    main()
