#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v7 일일 로테이션 — data.json 자동 생성 (세션 없이 GitHub Actions에서만 실행)

입력
  seeds.json          카테고리 정의
  keywords_pool.json  harvest가 모은 상업성 키워드 풀
  pool_history.json   회차별 검색량 스냅샷 (성장률)
  rotation.json       최근 회차별 노출 키워드 (중복 방지)
  data.json           직전 회차 (순위 변동 / NEW 판정)

출력
  data.json           사이트가 읽는 최종 데이터
  rotation.json       갱신

환경변수
  필수: 없음 (풀만 있으면 동작)
  선택(택1): NAVER_HUB_CLIENT_ID / NAVER_HUB_CLIENT_SECRET   ← NAVER API HUB (권장)
             NAVER_CLIENT_ID / NAVER_CLIENT_SECRET           ← 구 개발자센터 키
        → 데이터랩 검색어트렌드(성장률·스파크라인·검색지수) + 블로그 월간 발행량 추가
"""
import json, os, re, sys, time, math, random
import datetime as dt
import urllib.request, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commercial_filter import is_commercial  # noqa

# ── 인증 백엔드 ────────────────────────────────
# 네이버 검색/데이터랭 API는 NAVER API HUB(네이버 클라우드 플랫폼)로 이관됨.
# 개발자센터(openapi.naver.com) 키는 2027-06-30까지만 유효하고,
# 신규 앱에는 '검색' API 자체가 목록에 없어 401이 난다.
#   1순위: NAVER_HUB_CLIENT_ID / NAVER_HUB_CLIENT_SECRET  (API HUB)
#   2순위: NAVER_CLIENT_ID / NAVER_CLIENT_SECRET          (기존 개발자센터 키)
HUB_ID = os.environ.get("NAVER_HUB_CLIENT_ID", "").strip()
HUB_SEC = os.environ.get("NAVER_HUB_CLIENT_SECRET", "").strip()
CID = os.environ.get("NAVER_CLIENT_ID", "").strip()
CSEC = os.environ.get("NAVER_CLIENT_SECRET", "").strip()

if HUB_ID and HUB_SEC:
    BACKEND = "hub"
    API_BASE = "https://naverapihub.apigw.ntruss.com"
    AUTH = {"X-NCP-APIGW-API-KEY-ID": HUB_ID, "X-NCP-APIGW-API-KEY": HUB_SEC}
    BLOG_PATH, TREND_PATH = "/search/v1/blog", "/search-trend/v1/search"
elif CID and CSEC:
    BACKEND = "legacy"
    API_BASE = "https://openapi.naver.com"
    AUTH = {"X-Naver-Client-Id": CID, "X-Naver-Client-Secret": CSEC}
    BLOG_PATH, TREND_PATH = "/v1/search/blog.json", "/v1/datalab/search"
else:
    BACKEND = None
    API_BASE = AUTH = BLOG_PATH = TREND_PATH = None

RICH = BACKEND is not None
MAX_FAILS = 12          # 연속 실패가 이만큼 쌓이면 보강을 포기한다

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)

TABS = ["생활/건강", "디지털/가전", "식품", "가구/인테리어", "스포츠/레저",
        "패션의류", "출산/육아", "화장품/미용", "패션잡화", "여가/생활편의",
        "여행:커넥트", "여행:마이리얼트립"]

PER_TAB = 20          # 탭당 노출 개수
CANDIDATES = 35       # 보강 API를 태울 후보 수 (탭당)
EXCLUDE_ROUNDS = 3    # 최근 N회차에 나온 키워드는 제외
BLOG_TH = [3333, 10000]   # 월간 발행량 기준 낮음/보통/높음

ROUND_SLOTS = [(8, "아침 회차 ①"), (13, "점심 회차 ②"), (20, "저녁 회차 ③")]


# ══════════════════════════════════════════════════════════════
# 공통 유틸
# ══════════════════════════════════════════════════════════════
def jload(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def current_slot():
    """지금 시각(KST)에 가장 가까운 다음 회차 라벨."""
    h = NOW.hour
    for i, (hh, label) in enumerate(ROUND_SLOTS):
        if h <= hh:
            return i, label
    return 0, ROUND_SLOTS[0][1]


# ══════════════════════════════════════════════════════════════
# 네이버 오픈API (선택)
# ══════════════════════════════════════════════════════════════
def _open_api(path, data=None, query=None):
    url = API_BASE + path + (("?" + urllib.parse.urlencode(query)) if query else "")
    headers = dict(AUTH)
    if data is not None:
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                     headers=headers, method="POST")
    else:
        req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def datalab_batch(keywords):
    """[kw] → {kw: {'spark':[...], 'wow':float, 'idx':float}}  (최대 5개씩)"""
    end = NOW.date()
    start = end - dt.timedelta(days=29)
    body = {
        "startDate": start.isoformat(), "endDate": end.isoformat(),
        "timeUnit": "date",
        "keywordGroups": [{"groupName": k, "keywords": [k]} for k in keywords[:5]],
    }
    res = _open_api(TREND_PATH, body)
    out = {}
    for g in res.get("results", []):
        name = g.get("title")
        pts = [float(p.get("ratio", 0)) for p in g.get("data", [])]
        if len(pts) < 8:
            continue
        last7 = pts[-7:]
        prev7 = pts[-14:-7] if len(pts) >= 14 else pts[:-7]
        a = sum(last7) / max(1, len(last7))
        b = sum(prev7) / max(1, len(prev7))
        wow = ((a / b) - 1) * 100 if b > 0.01 else 0.0
        spark = pts[-14:]
        mx = max(spark) or 1
        out[name] = {
            "spark": [round(v / mx * 100) for v in spark],
            "wow": round(max(-90, min(300, wow)), 1),
            "idx": round(pts[-1] / (max(pts) or 1) * 100),
        }
    return out


_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})")


def blog_monthly(kw):
    """최근 100건이 쌓이는 데 걸린 기간으로 월간 발행량을 추정."""
    res = _open_api(BLOG_PATH, query={"query": kw, "display": 100, "sort": "date"})
    items = res.get("items", [])
    total = int(res.get("total", 0) or 0)
    if not items:
        return 0
    dates = []
    for it in items:
        m = _DATE.search(it.get("postdate", "") or "")
        if m:
            dates.append(dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    if not dates:
        return None
    span = max(1, (NOW.date() - min(dates)).days + 1)
    est = len(dates) / span * 30.0
    # 누적 총량보다 클 수는 없다
    return int(min(est, total))


# ══════════════════════════════════════════════════════════════
# 점수 계산
# ══════════════════════════════════════════════════════════════
def growth_ratio(kw, hist):
    """pool_history 스냅샷으로 검색량 성장 배수. 데이터 없으면 1.0"""
    snaps = hist.get("snapshots", [])
    if len(snaps) < 2:
        return None
    now = snaps[-1]["vol"].get(kw)
    for prev_snap in reversed(snaps[:-1]):
        prev = prev_snap["vol"].get(kw)
        if now and prev:
            return now / prev
    return None


COMP_E = {"낮음": 1.30, "중간": 0.95, "높음": 0.62}


def ease_from_blog(blog):
    if blog is None:
        return None
    if blog < BLOG_TH[0]:
        return 1.35
    if blog < BLOG_TH[1]:
        return 0.95
    return max(0.5, 0.95 * (BLOG_TH[1] / max(blog, 1)) ** 0.35)


# 수요 스위트스팟 — 월 6만 검색 부근이 만점.
# 초대형 키워드(수십만)는 개인 블로그가 상위노출 못 하므로 오히려 감점한다.
SWEET_LOG = math.log10(60000)
SWEET_W = 0.95


def score_of(row, lo=None, hi=None):
    vol = max(row.get("vol") or 1, 1)
    # D: 수요 — 스위트스팟 곡선 (카테고리 무관 절대 기준)
    lv = math.log10(vol)
    D = math.exp(-(((lv - SWEET_LOG) / SWEET_W) ** 2))

    # G: 성장 배수
    g = row.get("_growth")
    if row.get("wow") is not None:
        g = 1 + (row["wow"] / 100.0)
    G = 1.0 if g is None else max(0.75, min(1.55, g))

    # E: 경쟁 여유
    E = ease_from_blog(row.get("blog"))
    if E is None:
        E = COMP_E.get(row.get("comp", ""), 0.95)
    # 광고가 많이 붙은 키워드는 상업성 가산 (0~15 → 1.0~1.12)
    ads_bonus = 1.0 + min(row.get("ads") or 0, 15) / 125.0

    s = 100 * (0.35 + 0.65 * D) * G * E * ads_bonus
    return round(min(160, max(12, s)))


# ══════════════════════════════════════════════════════════════
# 글감 로테이션 (앵글 16종 × 상황 6종)
# ══════════════════════════════════════════════════════════════
SITUATIONS = ["1인 가구", "신혼부부", "자취생", "아이 있는 집", "부모님 선물", "사무실"]

SHOP_ANGLES = [
    lambda k, c, m, s: f"{k} TOP 3 비교 — 가격·성능 한눈에",
    lambda k, c, m, s: f"{k} 최저가로 사는 타이밍과 할인 정보",
    lambda k, c, m, s: f"{k} 처음 고를 때 꼭 봐야 할 3가지",
    lambda k, c, m, s: f"{k} 살 때 피해야 할 실수 3가지",
    lambda k, c, m, s: f"{k} 한 달 써본 솔직 후기",
    lambda k, c, m, s: f"{s}에 맞는 {k} 고르는 법",
    lambda k, c, m, s: f"{m}월 {k} 트렌드와 지금 사야 하는 이유",
    lambda k, c, m, s: f"{k} vs 대체품, 뭐가 더 이득일까",
    lambda k, c, m, s: f"{k} 가격대별 정리 — 3만원·10만원·30만원",
    lambda k, c, m, s: f"{k} 실패 없는 브랜드 3곳과 그 이유",
    lambda k, c, m, s: f"{k} 사기 전 확인할 체크리스트 7가지",
    lambda k, c, m, s: f"{k} 오래 쓰는 관리법과 교체 주기",
    lambda k, c, m, s: f"{k} 온라인 vs 오프라인, 어디가 더 쌀까",
    lambda k, c, m, s: f"{k} 리뷰 조작 걸러내는 법",
    lambda k, c, m, s: f"{k} 사은품·쿠폰까지 챙기는 구매 순서",
    lambda k, c, m, s: f"{k} 후회한 사람들이 공통으로 놓친 것",
]

TRAVEL_ANGLES = [
    lambda k, c, m, s: f"{k} 항공권 싸게 잡는 법",
    lambda k, c, m, s: f"{k} 3박 4일 경비 총정리",
    lambda k, c, m, s: f"{k} 숙소 어디가 좋을까 — 지역별 비교",
    lambda k, c, m, s: f"{k} 필수 준비물 체크리스트",
    lambda k, c, m, s: f"{k} 투어·입장권 가격 비교",
    lambda k, c, m, s: f"{k} 처음 가는 사람을 위한 코스 추천",
    lambda k, c, m, s: f"{m}월 {k} 날씨와 옷차림",
    lambda k, c, m, s: f"{k} 이심·유심·환전 준비 가이드",
    lambda k, c, m, s: f"{k} 공항에서 시내까지 가는 법 3가지",
    lambda k, c, m, s: f"{k} 현지 교통패스, 사야 할까 말아야 할까",
    lambda k, c, m, s: f"{k} 아이와 함께 갈 때 알아야 할 것",
    lambda k, c, m, s: f"{k} 혼자 가도 괜찮을까 — 1인 여행 기준",
    lambda k, c, m, s: f"{k} 예산 100만원으로 가능할까",
    lambda k, c, m, s: f"{k} 여행자보험 꼭 들어야 하는 이유",
    lambda k, c, m, s: f"{k} 가서 후회한 것 vs 잘한 것",
    lambda k, c, m, s: f"{k} 성수기 피하는 최적 시기",
]

SOJAE_N = 3          # 키워드당 노출할 글감 수
PICK_STRIDE = 5      # 앵글 목록에서 몇 칸씩 건너뛰며 고를지


def _kwhash(kw):
    return sum(ord(c) for c in kw)


def sojae_for(kw, cat, rank, round_no):
    angles = TRAVEL_ANGLES if cat.startswith("여행") else SHOP_ANGLES
    n = len(angles)
    h = _kwhash(kw)
    base = (round_no * 3 + rank + h) % n
    sit = SITUATIONS[(round_no + rank + h) % len(SITUATIONS)]
    picks = [(base + PICK_STRIDE * j) % n for j in range(SOJAE_N)]
    return [angles[i](kw, cat, NOW.month, sit) for i in picks]


# ══════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════
def main():
    global RICH
    pool = jload("keywords_pool.json", {}).get("categories", {})
    hist = jload("pool_history.json", {"snapshots": []})
    rot = jload("rotation.json", {"round": 0, "history": []})
    prev = jload("data.json", {})
    prev_tabs = prev.get("tabs", {})

    if not pool:
        print("keywords_pool.json 이 비어 있습니다. harvest 먼저 실행하세요.")
        return 1

    round_no = int(rot.get("round", 0)) + 1
    slot_i, slot_label = current_slot()

    recent = set()
    for h in rot.get("history", [])[-EXCLUDE_ROUNDS:]:
        for kws in h.get("kws", {}).values():
            recent.update(kws)
    print("최근 %d회차 제외 대상: %d개" % (EXCLUDE_ROUNDS, len(recent)))

    # ── 1차: SearchAd 신호만으로 후보 추림 ──────────────────────
    cand = {}
    for cat in TABS:
        rows = [dict(r) for r in pool.get(cat, [])]
        rows = [r for r in rows if is_commercial(r.get("kw"), r.get("ads"), cat)]
        if not rows:
            print("!! 풀 없음:", cat)
            cand[cat] = []
            continue
        for r in rows:
            r["_growth"] = growth_ratio(r["kw"], hist)
        for r in rows:
            r["_pre"] = score_of(r)

        fresh = [r for r in rows if r["kw"] not in recent]
        stale = [r for r in rows if r["kw"] in recent]
        fresh.sort(key=lambda r: -r["_pre"])
        stale.sort(key=lambda r: -r["_pre"])
        # 신선한 것 우선, 모자라면 오래된 것으로 채움
        cand[cat] = (fresh + stale)[:CANDIDATES]

    # ── 2차: 오픈API 보강 (키가 있을 때만) ─────────────────────
    if RICH:
        allkw, seen = [], set()
        for cat in TABS:
            for r in cand.get(cat, []):
                if r["kw"] not in seen:
                    seen.add(r["kw"])
                    allkw.append(r["kw"])
        print("보강 대상 키워드 %d (backend=%s)" % (len(allkw), BACKEND))

        trend, blogs = {}, {}
        fails = 0
        for i in range(0, len(allkw), 5):
            if fails >= MAX_FAILS:
                print("!! 검색어트렌드 연속 실패 %d회 — 보강 중단" % fails)
                break
            try:
                trend.update(datalab_batch(allkw[i:i + 5]))
                fails = 0
            except Exception as e:
                fails += 1
                if fails <= 3:
                    print("datalab fail", allkw[i:i + 5], e)
            time.sleep(0.15)
        print("datalab ok", len(trend))

        fails = 0
        for k in allkw:
            if fails >= MAX_FAILS:
                print("!! 블로그 검색 연속 실패 %d회 — 보강 중단" % fails)
                break
            try:
                blogs[k] = blog_monthly(k)
                fails = 0
            except Exception as e:
                fails += 1
                if fails <= 3:
                    print("blog fail", k, e)
            time.sleep(0.12)
        print("blog ok", len(blogs))

        for cat in TABS:
            for r in cand.get(cat, []):
                t = trend.get(r["kw"])
                if t:
                    r["spark"] = t["spark"]
                    r["wow"] = t["wow"]
                    r["idx"] = t["idx"]
                if blogs.get(r["kw"]) is not None:
                    r["blog"] = blogs[r["kw"]]

        # 실제로 아무것도 못 받아왔으면 rich=False 로 정직하게 표시
        if not trend and not blogs:
            RICH = False
            print("!! 보강 데이터 0건 — SearchAd 신호만으로 산출 (키/권한 확인 필요)")
    else:
        print("네이버 API 키 없음 → SearchAd 신호만으로 산출")

    # ── 최종 점수 & 선정 ────────────────────────────────────────
    tabs = {}
    for cat in TABS:
        rows = cand.get(cat, [])
        for r in rows:
            r["score"] = score_of(r)
        # 동점 시 광고수 → 검색량 순으로 안정 정렬
        rows.sort(key=lambda r: (-r["score"], -(r.get("ads") or 0), -(r.get("vol") or 0)))
        picked = rows[:PER_TAB]

        prev_rank = {it["kw"]: i for i, it in enumerate(prev_tabs.get(cat, []))}
        out = []
        for i, r in enumerate(picked):
            item = {
                "kw": r["kw"],
                "cat": cat.replace(":", " · "),
                "score": r["score"],
                "vol": r.get("vol"),
                "sojae": sojae_for(r["kw"], cat, i, round_no),
            }
            for f in ("wow", "idx", "blog", "spark"):
                if r.get(f) is not None:
                    item[f] = r[f]
            if item.get("wow") is not None and item["wow"] >= 25:
                item["hot"] = True
            elif r.get("_growth") and r["_growth"] >= 1.25:
                item["hot"] = True
            if r["kw"] in prev_rank:
                item["rankd"] = prev_rank[r["kw"]] - i
            else:
                item["new"] = True
            out.append(item)
        tabs[cat] = out
        print("%-16s %2d개 · 최고 %s" % (cat, len(out), out[0]["score"] if out else "-"))

    # ── 전체 탭 (교차 카테고리 TOP 20) ──────────────────────────
    merged, seen = [], set()
    for cat in TABS:
        for it in tabs[cat]:
            if it["kw"] in seen:
                continue
            seen.add(it["kw"])
            merged.append(dict(it))
    merged.sort(key=lambda x: (-x["score"], -(x.get("vol") or 0)))
    # 한 카테고리가 전체 탭을 독식하지 않도록 상한
    per_cat, all_tab = {}, []
    for cap in (3, 5, PER_TAB):          # 3개씩 → 부족하면 완화
        for it in merged:
            if len(all_tab) >= PER_TAB:
                break
            if it in all_tab:
                continue
            c = it.get("cat", "")
            if per_cat.get(c, 0) >= cap:
                continue
            per_cat[c] = per_cat.get(c, 0) + 1
            all_tab.append(it)
        if len(all_tab) >= PER_TAB:
            break
    prev_all = {it["kw"]: i for i, it in enumerate(prev_tabs.get("전체", []))}
    for i, it in enumerate(all_tab):
        it.pop("rankd", None)
        it.pop("new", None)
        if it["kw"] in prev_all:
            it["rankd"] = prev_all[it["kw"]] - i
        else:
            it["new"] = True
    tabs = {"전체": all_tab, **tabs}

    # ── 🆕 오늘 새로 들어온 키워드 ──────────────────────────────
    prev_all_kws = set()
    for items in prev_tabs.values():
        for it in items:
            prev_all_kws.add(it.get("kw"))
    newkw = []
    for cat in TABS:
        for it in tabs[cat]:
            if it["kw"] not in prev_all_kws:
                newkw.append({"kw": it["kw"], "cat": cat, "score": it["score"],
                              "vol": it.get("vol"), "blog": it.get("blog")})
    newkw.sort(key=lambda x: -x["score"])
    newkw = newkw[:8]

    # ── 히어로 ─────────────────────────────────────────────────
    h = all_tab[0] if all_tab else None
    hero = {}
    if h:
        bits = []
        if h.get("vol"):
            bits.append("월 검색 %s회" % format(h["vol"], ","))
        if h.get("wow") is not None:
            bits.append("전주 대비 %s%d%%" % ("+" if h["wow"] > 0 else "", round(h["wow"])))
        if h.get("blog") is not None:
            lvl = "낮음" if h["blog"] < BLOG_TH[0] else ("보통" if h["blog"] < BLOG_TH[1] else "높음")
            bits.append("블로그 경쟁 %s" % lvl)
        elif h.get("cat"):
            bits.append("광고 경쟁 지표 기준 여유 있는 구간")
        hero = {
            "kw": h["kw"],
            "title": "「%s」 지금이 기회예요" % h["kw"],
            "score": h["score"],
            "reason": ", ".join(bits) + ". 오늘 글감 메모 3개를 카드에서 확인하세요.",
        }

    data = {
        "updated": "%d.%d (%s) %02d시 갱신" % (
            NOW.month, NOW.day, "월화수목금토일"[NOW.weekday()], ROUND_SLOTS[slot_i][0]),
        "round": slot_label,
        "roundNo": round_no,
        "blogTh": BLOG_TH,
        "rich": RICH,
        "hero": hero,
        "newkw": newkw,
        "tabs": tabs,
    }
    json.dump(data, open("data.json", "w"), ensure_ascii=False)

    rot["round"] = round_no
    rot["history"] = (rot.get("history", []) + [{
        "round": round_no,
        "at": NOW.strftime("%Y-%m-%d %H:%M KST"),
        "kws": {cat: [it["kw"] for it in tabs[cat]] for cat in TABS},
    }])[-(EXCLUDE_ROUNDS + 2):]
    json.dump(rot, open("rotation.json", "w"), ensure_ascii=False)

    print("=== 회차 %d (%s) | 신규 %d개 | rich=%s ===" %
          (round_no, slot_label, len(newkw), RICH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
