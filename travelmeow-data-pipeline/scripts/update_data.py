#!/usr/bin/env python3
"""
data.json 자동 갱신 스크립트 (GitHub Actions에서 실행, Claude/토큰 사용 없음)

자동 갱신: idx(검색지수), wow(전주대비%), spark(14일 추이), blog(월간 발행량,
하루 1회만), score, hot, rankd/new(직전 실행 대비 순위 변동)
동결 유지(수동으로만 바꿀 수 있음): sojae(글감 메모), hero.title/reason 문구 톤,
deals(마이리얼트립 상품 목록)

주의: 네이버 API HUB로 이전된 지 얼마 안 된 엔드포인트라 요청 스키마가
문서화가 부족한 부분(특히 category/keywords)이 있음 — 실행 로그(Actions 탭)를
보고 배치 크기나 필드명을 조정해야 할 수 있음. 실패한 키워드는 이전 값을
그대로 유지하고 건너뛰므로 전체 파이프라인이 죽지는 않음.
"""
import json, os, time, datetime as dt, urllib.request, urllib.error

HUB = "https://naverapihub.apigw.ntruss.com"
CID = os.environ["NAVER_HUB_CLIENT_ID"]
CSEC = os.environ["NAVER_HUB_CLIENT_SECRET"]
MRT_BASE = "https://partner-ext-api.myrealtrip.com"
MRT_KEY = os.environ.get("MYREALTRIP_API_KEY")

CATEGORY_IDS = {
    "생활/건강": "50000008", "디지털/가전": "50000003", "식품": "50000006",
    "가구/인테리어": "50000004", "스포츠/레저": "50000007", "패션의류": "50000000",
    "출산/육아": "50000005", "화장품/미용": "50000002", "패션잡화": "50000001",
    "여가/생활편의": "50000009",
}

def hub_post(path, body, retries=2):
    req = urllib.request.Request(
        HUB + path, data=json.dumps(body).encode(),
        headers={"X-NCP-APIGW-API-KEY-ID": CID, "X-NCP-APIGW-API-KEY": CSEC,
                 "Content-Type": "application/json"}, method="POST")
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except Exception as e:
            print("POST fail", path, body.get("category") or body.get("keywordGroups"), e)
            time.sleep(1.0)
    return None

def hub_get_blog_count_30d(kw, cap_pages=4):
    """최근 30일 내 발행된 블로그 글 수 추정 (최신순 페이지를 넘기며 카운트)."""
    cutoff = dt.datetime.utcnow() + dt.timedelta(hours=9) - dt.timedelta(days=30)
    total = 0
    for page in range(cap_pages):
        start = page * 100 + 1
        url = (HUB + "/search/v1/blog?query=" + urllib.parse.quote(kw)
               + f"&display=100&start={start}&sort=date")
        req = urllib.request.Request(url, headers={
            "X-NCP-APIGW-API-KEY-ID": CID, "X-NCP-APIGW-API-KEY": CSEC})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.load(r)
        except Exception as e:
            print("blog fail", kw, e)
            break
        items = res.get("items", [])
        if not items:
            break
        stop = False
        for it in items:
            try:
                pub = dt.datetime.strptime(it["postdate"], "%Y%m%d") if "postdate" in it \
                    else dt.datetime.strptime(it["pubDate"][:16], "%a, %d %b %Y")
            except Exception:
                total += 1
                continue
            if pub < cutoff:
                stop = True
                break
            total += 1
        if stop or len(items) < 100:
            break
        time.sleep(0.15)
    return total

import urllib.parse

def kst_range(days=13):
    now_kst = dt.datetime.utcnow() + dt.timedelta(hours=9)
    end = (now_kst - dt.timedelta(days=1)).date()
    start = end - dt.timedelta(days=days)
    return start.isoformat(), end.isoformat()

def fetch_keyword_trend_shopping(cat_name, cat_id, kws, start, end):
    """분야 내 키워드별 트렌드 (5개씩 배치)."""
    out = {}
    for i in range(0, len(kws), 5):
        batch = kws[i:i+5]
        body = {"startDate": start, "endDate": end, "timeUnit": "date",
                "category": cat_id,
                "keyword": [{"name": k, "param": [k]} for k in batch]}
        res = hub_post("/shopping/v1/category/keywords", body)
        if res:
            for r in res.get("results", []):
                out[r["title"]] = [p["ratio"] for p in r["data"]]
        time.sleep(0.3)
    return out

def fetch_keyword_trend_search(kws, start, end):
    """일반 검색어 트렌드 (여행 키워드용) — 그룹당 1개 키워드, 5개씩 배치."""
    out = {}
    for i in range(0, len(kws), 5):
        batch = kws[i:i+5]
        body = {"startDate": start, "endDate": end, "timeUnit": "date",
                "keywordGroups": [{"groupName": k, "keywords": [k]} for k in batch]}
        res = hub_post("/search-trend/v1/search", body)
        if res:
            for r in res.get("results", []):
                out[r["title"]] = [p["ratio"] for p in r["data"]]
        time.sleep(0.3)
    return out

def fetch_myrealtrip_deals(kw, size=5):
    """투어티켓 상품 검색 — 판매량순 상위 N개를 deals 형식으로 변환."""
    if not MRT_KEY:
        return None
    query = kw[:-2] if kw.endswith("여행") else kw  # "싱가포르여행" -> "싱가포르"
    body = {"keyword": query, "sort": "selling_count_desc", "page": 1, "size": size}
    req = urllib.request.Request(
        MRT_BASE + "/v1/products/tna/search", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {MRT_KEY}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.load(r)
    except Exception as e:
        print("myrealtrip fail", kw, e)
        return None
    items = (res.get("data") or {}).get("items", [])
    if not items:
        return None
    return [{"name": f"판매 {i+1}위 · {it['itemName']}", "price": it.get("salePrice"),
              "rating": it.get("reviewScore"), "url": it.get("productUrl")}
             for i, it in enumerate(items)]

def pct(new, old):
    if not old:
        return None
    return (new - old) / old * 100

def score_of(idx, wow, blog):
    growth = 1 + max(-60, min(150, wow or 0)) / 200
    if blog is None:
        comp = 1.0
    elif blog < 4000:
        comp = 1.15
    elif blog > 15000:
        comp = 0.85
    else:
        comp = 1.0
    return round(idx * growth * comp)

def main():
    with open("data.json", encoding="utf-8") as f:
        prev = json.load(f)

    start, end = kst_range()
    do_blog_today = (dt.datetime.utcnow().hour == 0)  # 블로그 발행량은 하루 1회만 갱신 (API 호출 절약)
    blog_cache = {}
    if not do_blog_today:
        for items in prev.get("tabs", {}).values():
            for it in items:
                if it.get("blog") is not None:
                    blog_cache[it["kw"]] = it["blog"]

    new_tabs = {}
    prev_ranks = {}  # tab -> {kw: rank}
    for tab, items in prev.get("tabs", {}).items():
        prev_ranks[tab] = {it["kw"]: i + 1 for i, it in enumerate(items)}

    # 1) 쇼핑 카테고리 탭들
    for cat_name, cat_id in CATEGORY_IDS.items():
        pool_items = prev["tabs"].get(cat_name, [])
        kws = [it["kw"] for it in pool_items]
        trend = fetch_keyword_trend_shopping(cat_name, cat_id, kws, start, end)
        rows = []
        for it in pool_items:
            kw = it["kw"]
            series = trend.get(kw)
            if not series:
                rows.append(it)  # 실패하면 이전 값 유지
                continue
            idx = series[-1]
            wow = pct(idx, series[-8]) if len(series) >= 8 else None
            blog = blog_cache.get(kw) if not do_blog_today else hub_get_blog_count_30d(kw)
            if do_blog_today:
                blog_cache[kw] = blog
            sc = score_of(idx, wow or 0, blog)
            rows.append({**it, "idx": round(idx, 1), "wow": round(wow, 1) if wow is not None else None,
                         "hot": bool(wow and wow >= 30), "blog": blog, "spark": [round(v, 1) for v in series],
                         "score": sc})
        rows.sort(key=lambda r: r["score"], reverse=True)
        for i, r in enumerate(rows):
            old_rank = prev_ranks.get(cat_name, {}).get(r["kw"])
            r["new"] = old_rank is None
            r["rankd"] = (old_rank - (i + 1)) if old_rank else 0
        new_tabs[cat_name] = rows

    # 2) 여행:마이리얼트립 (검색어 트렌드 API 사용, deals는 동결)
    mrt_items = prev["tabs"].get("여행:마이리얼트립", [])
    mrt_kws = [it["kw"] for it in mrt_items]
    mrt_trend = fetch_keyword_trend_search(mrt_kws, start, end)
    mrt_rows = []
    for it in mrt_items:
        kw = it["kw"]
        series = mrt_trend.get(kw)
        if not series:
            mrt_rows.append(it)
            continue
        idx = series[-1]
        wow = pct(idx, series[-8]) if len(series) >= 8 else None
        blog = blog_cache.get(kw) if not do_blog_today else hub_get_blog_count_30d(kw)
        if do_blog_today:
            blog_cache[kw] = blog
        sc = score_of(idx, wow or 0, blog)
        deals = fetch_myrealtrip_deals(kw) or it.get("deals")  # 실패하면 이전 딜 유지
        mrt_rows.append({**it, "idx": round(idx, 1), "wow": round(wow, 1) if wow is not None else None,
                          "hot": bool(wow and wow >= 30), "blog": blog, "spark": [round(v, 1) for v in series],
                          "score": sc, "deals": deals})
    mrt_rows.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(mrt_rows):
        old_rank = prev_ranks.get("여행:마이리얼트립", {}).get(r["kw"])
        r["new"] = old_rank is None
        r["rankd"] = (old_rank - (i + 1)) if old_rank else 0
    new_tabs["여행:마이리얼트립"] = mrt_rows

    # 3) 여행:커넥트 — 이미 다른 탭에 있는 쇼핑 키워드를 재사용 (별도 API 호출 없음)
    all_shopping = {r["kw"]: r for tab in CATEGORY_IDS for r in new_tabs.get(tab, [])}
    connect_items = prev["tabs"].get("여행:커넥트", [])
    new_tabs["여행:커넥트"] = [all_shopping.get(it["kw"], it) for it in connect_items]

    # 4) 전체 탭 — 전 분야 합쳐서 점수 상위 20
    pool = []
    for tab, rows in new_tabs.items():
        if tab.startswith("여행:커넥트"):
            continue
        pool.extend(rows)
    pool.sort(key=lambda r: r.get("score", 0), reverse=True)
    seen, top = set(), []
    for r in pool:
        if r["kw"] in seen:
            continue
        seen.add(r["kw"])
        top.append(r)
        if len(top) >= 20:
            break
    new_tabs["전체"] = top

    # 5) 히어로 (자동 생성 — 문장은 템플릿, 수치는 실시간)
    best = top[0] if top else None
    hero = prev.get("hero", {})
    if best:
        trend_word = "급상승 중이에요" if best.get("hot") else "꾸준히 관심받고 있어요"
        hero = {
            "kw": best["kw"], "title": f"「{best['kw']}」 지금이 기회예요",
            "score": best["score"],
            "reason": f"{best.get('cat','')} 분야 검색지수 {best.get('idx','-')}, "
                      f"전주 대비 {'+' if (best.get('wow') or 0) >= 0 else ''}{best.get('wow','-')}%, {trend_word}",
        }

    now_kst = dt.datetime.utcnow() + dt.timedelta(hours=9)
    out = {
        "updated": now_kst.strftime("%m.%d (%a) %H시 자동갱신").replace(
            "Mon", "월").replace("Tue", "화").replace("Wed", "수").replace("Thu", "목")
            .replace("Fri", "금").replace("Sat", "토").replace("Sun", "일"),
        "round": "자동 갱신 (GitHub Actions)",
        "hero": hero,
        "blogTh": [4000, 15000],
        "tabs": new_tabs,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("done. tabs:", {k: len(v) for k, v in new_tabs.items()})

if __name__ == "__main__":
    main()
