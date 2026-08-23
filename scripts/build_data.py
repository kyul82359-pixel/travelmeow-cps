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
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import urllib.request, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commercial_filter import is_commercial  # noqa
import blog_volume  # noqa

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
    CAFE_PATH = "/search/v1/cafearticle"
elif CID and CSEC:
    BACKEND = "legacy"
    API_BASE = "https://openapi.naver.com"
    AUTH = {"X-Naver-Client-Id": CID, "X-Naver-Client-Secret": CSEC}
    BLOG_PATH, TREND_PATH = "/v1/search/blog.json", "/v1/datalab/search"
    CAFE_PATH = "/v1/search/cafearticle.json"
else:
    BACKEND = None
    API_BASE = AUTH = BLOG_PATH = TREND_PATH = CAFE_PATH = None

RICH = BACKEND is not None
MAX_FAILS = 12          # 연속 실패가 이만큼 쌓이면 보강을 포기한다
WORKERS = 6             # 동시 API 호출 수 (HUB 한도 50 RPS 내)

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)

TABS = ["생활/건강", "디지털/가전", "식품", "가구/인테리어", "스포츠/레저",
        "패션의류", "출산/육아", "화장품/미용", "패션잡화", "여가/생활편의",
        "여행:커넥트", "여행:마이리얼트립"]

PER_TAB = 20          # 탭당 노출 개수
CANDIDATES = 35       # 보강 API를 태울 후보 수 (탭당)
EXCLUDE_ROUNDS = 3    # 최근 N회차에 나온 키워드는 제외
BLOG_TH = [1000, 5000]    # 월간 발행량 기준: 낮음 <1,000 / 보통 <5,000 / 높음
                          # (2026-08-20 실측 재보정: 에이블트라이크 760, 홍콩여행 4,350,
                          #  팔찌 9,000, 꽃다발 12,900, 호텔 30,000+)

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


def _int(v, default=0):
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v).replace("<", "").replace(",", "").strip())
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
    """완전한 하루들의 실제 건수로 월간 발행량 산출.

    첫 페이지 응답의 total(누적 발행량)도 같이 주워 담는다 — 추가 호출 없이
    경쟁 별점의 '콘텐츠 포화도' 항목에 쓰인다.
    → (월간 발행량, 포화여부, 누적 발행량)
    """
    box = {"total": None}

    def page(k, start):
        res = _open_api(BLOG_PATH, query={"query": k, "display": blog_volume.PAGE,
                                          "sort": "date", "start": start})
        if box["total"] is None:
            box["total"] = _int(res.get("total"), 0)
        items = res.get("items", [])
        dates = []
        for it in items:
            m = _DATE.search(it.get("postdate", "") or "")
            if m:
                dates.append(m.group(0))
        return dates, len(items)

    v, sat = blog_volume.measure(page, kw)
    return v, sat, box["total"]


def cafe_total(kw):
    """카페글 누적 발행량.

    네이버 카페글 검색 API는 블로그와 달리 postdate 를 주지 않는다
    (title/link/description/cafename/cafeurl 뿐). 그래서 '월간' 카페 발행량은
    원리적으로 측정할 수 없고, 누적 total 만 얻을 수 있다.
    → 경쟁 별점에서는 블로그도 누적으로 맞춰서 (블로그누적+카페누적)/월검색량
      이라는 같은 단위의 비율로 합산한다.
    display=1 이므로 키워드당 딱 1회 호출.
    """
    res = _open_api(CAFE_PATH, query={"query": kw, "display": 1, "sort": "sim"})
    return _int(res.get("total"), 0)


# ══════════════════════════════════════════════════════════════
# 검색광고 API — 선정된 키워드의 검색량을 실행 시점 기준으로 재조회
# (keywords_pool.json 은 주 1회 갱신이라 최대 7일 묵은 값이다)
# ══════════════════════════════════════════════════════════════
import hmac, hashlib, base64  # noqa: E402

AD_KEY = os.environ.get("NAVER_API_KEY", "").strip()
AD_SEC = os.environ.get("NAVER_SECRET", "").strip()
AD_CUS = os.environ.get("NAVER_CUSTOMER", "").strip()
AD_OK = bool(AD_KEY and AD_SEC and AD_CUS)


def searchad_volumes(keywords):
    """[kw] → {kw: {'vol':월검색량, 'ads':노출광고수, 'comp':광고경쟁도}}. 5개씩 배치.

    검색량뿐 아니라 광고 경쟁도(compIdx)·노출 광고 수(plAvgDepth)도 같은 응답에
    들어 있으므로 함께 최신화한다 — 경쟁 별점의 3.0점이 여기서 나온다.
    """
    out = {}
    if not AD_OK:
        return out

    def one(batch):
        hints = ",".join(k.replace(" ", "") for k in batch)
        ts = str(round(time.time() * 1000))
        sig = base64.b64encode(hmac.new(
            AD_SEC.encode(), (ts + ".GET./keywordstool").encode(),
            hashlib.sha256).digest()).decode()
        q = urllib.parse.urlencode({"hintKeywords": hints, "showDetail": "1"})
        req = urllib.request.Request(
            "https://api.searchad.naver.com/keywordstool?" + q,
            headers={"X-Timestamp": ts, "X-API-KEY": AD_KEY,
                     "X-Customer": AD_CUS, "X-Signature": sig})
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.load(r)
        wanted = {k.replace(" ", "").upper(): k for k in batch}
        got = {}
        for item in res.get("keywordList", []):
            rel = str(item.get("relKeyword", "")).replace(" ", "").upper()
            if rel in wanted:
                got[wanted[rel]] = {
                    "vol": (_int(item.get("monthlyPcQcCnt"))
                            + _int(item.get("monthlyMobileQcCnt"))),
                    "ads": _int(item.get("plAvgDepth"), 0),
                    "comp": str(item.get("compIdx") or "").strip(),
                }
        return got

    batches = [keywords[i:i + 5] for i in range(0, len(keywords), 5)]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for got in ex.map(lambda b: _safe(one, b, {}), batches):
            out.update(got)
    return out


def _safe(fn, arg, default):
    try:
        return fn(arg)
    except Exception as e:
        print("  ! %s(%s) %s" % (getattr(fn, "__name__", "fn"), arg, e))
        return default


# ══════════════════════════════════════════════════════════════
# 마이리얼트립 — 키워드별 판매 TOP5 (여행:마이리얼트립 탭에만)
# ══════════════════════════════════════════════════════════════
MRT_KEY = os.environ.get("MYREALTRIP_API_KEY", "").strip()
MRT_BASE = "https://partner-ext-api.myrealtrip.com"
MRT_OK = bool(MRT_KEY)


def _won(v):
    if isinstance(v, (int, float)) and v > 0:
        return format(int(v), ",") + "원~"
    s = str(v or "").strip()
    return s if s else ""


# 검색어를 지역명만 남기고 다듬는다.
# 마이리얼트립 검색은 '일본북해도'·'베트남사파' 처럼 나라+도시가 붙은 말을
# 못 알아듣고 엉뚱한 인기 상품(오사카·다낭)을 돌려준다. 접두 국가명과
# 접미 상품어를 떼어 도시명만 남기면 정확히 매칭된다.
MRT_SUF = ("자유여행", "패키지여행", "항공권", "패키지", "입장권", "여행", "호텔",
           "숙소", "투어", "티켓")
MRT_PRE = ("말레이시아", "인도네시아", "필리핀", "싱가포르", "베트남", "태국",
           "일본", "중국", "대만", "미국", "유럽", "국내")


def mrt_query(kw):
    q = kw.strip()
    for suf in MRT_SUF:
        if q.endswith(suf) and len(q) - len(suf) >= 1:
            q = q[:-len(suf)]
            break
    for pre in MRT_PRE:
        if q.startswith(pre) and len(q) - len(pre) >= 1:
            q = q[len(pre):]
            break
    return q or kw


def fetch_myrealtrip_deals(kw, size=5):
    """투어·티켓 상품을 판매량순으로 상위 N개. 실패하면 None (빌드는 계속 간다)."""
    if not MRT_OK:
        return None
    query = mrt_query(kw)
    body = {"keyword": query, "sort": "selling_count_desc", "page": 1, "size": size}
    req = urllib.request.Request(
        MRT_BASE + "/v1/products/tna/search", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + MRT_KEY, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        res = json.load(r)
    items = (res.get("data") or {}).get("items") or res.get("items") or []
    out = []
    for i, it in enumerate(items[:size]):
        name = it.get("itemName") or it.get("name")
        url = it.get("productUrl") or it.get("url")
        if not (name and url):
            continue
        out.append({
            "name": "판매 %d위 · %s" % (len(out) + 1, name),
            "price": _won(it.get("salePrice")),
            "rating": it.get("reviewScore") or "",
            "url": url,
        })
    return out or None


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


# ══════════════════════════════════════════════════════════════
# 경쟁 별점 — 5.0 만점, 별이 많을수록 "진입하기 좋다"(경쟁 여유가 크다)
#
#   A 광고 경쟁도 compIdx              2.0점   검색광고 API — 정확
#   B 콘텐츠 포화도 (블로그+카페 누적)  1.5점   검색 API — 절대값은 과소, 순위는 신뢰
#   C 노출 광고 수 plAvgDepth           1.0점   검색광고 API — 정확
#   D 수요 성장 추세 (전주 대비)        0.5점   데이터랩 — 정확
#
# 5점 중 3.5점을 오차 없는 검색광고/데이터랩 값에서 뽑는다. 우리 블로그 검색
# 지수가 상용 도구보다 좁다는 걸 알고 있기 때문에, 그 영향이 별점 전체를
# 좌우하지 못하도록 일부러 1.5점으로 묶어둔 것이다.
# ══════════════════════════════════════════════════════════════
# ── 배점표는 2026-08-23 실측 420개 분포로 보정했다 ──────────────
# 상업성 필터(광고 3개 이상)를 통과한 키워드만 다루므로, 이 풀에는
# compIdx '낮음'이 거의 없고(실측 중간 32% / 높음 68%),
# plAvgDepth 도 8~10에 몰린다(10이 API 상한). 절대 기준을 그대로 쓰면
# 전 키워드가 1~2점에 깔려서 5점 만점이 무의미해진다. 그래서
# "상업 키워드들 사이에서의 상대적 여유"가 드러나도록 눈금을 맞췄다.
STAR_AD = {"낮음": 2.0, "중간": 1.55, "높음": 0.95}
STAR_AD_UNKNOWN = 1.25

# 포화도 = (블로그 누적 + 카페 누적) ÷ 월 검색량
# 실측 분포: p10 1.44 · p25 5.70 · 중앙 19.21 · p75 77.74 · p90 168.22
SAT_BEST = 1.5        # 이 이하면 만점
SAT_WORST = 200.0     # 이 이상이면 0점
SAT_MAX = 1.5


def _sat_points(posts, vol):
    """포화도: 검색 1회당 이미 쌓여 있는 글이 몇 개인가. 적을수록 별이 많다."""
    if posts is None or not vol or vol <= 0:
        return None
    r = posts / float(vol)
    if r <= SAT_BEST:
        return SAT_MAX
    if r >= SAT_WORST:
        return 0.0
    span = math.log10(SAT_WORST) - math.log10(SAT_BEST)
    return SAT_MAX * (1.0 - (math.log10(r) - math.log10(SAT_BEST)) / span)


def _depth_points(ads):
    """노출 광고 수: 광고주가 많이 붙을수록 그 자리를 두고 싸우는 사람이 많다.
    plAvgDepth 는 10에서 잘리므로 10은 '10 이상'으로 읽는다."""
    if ads is None:
        return 0.6
    if ads <= 4:
        return 1.0
    if ads <= 6:
        return 0.85
    if ads <= 7:
        return 0.72
    if ads <= 8:
        return 0.6
    if ads <= 9:
        return 0.48
    return 0.38


def _trend_points(wow):
    """수요가 커지는 중이면 새로 들어갈 자리도 같이 생긴다."""
    if wow is None:
        return 0.25
    if wow >= 25:
        return 0.5
    if wow >= 5:
        return 0.35
    if wow >= -5:
        return 0.25
    if wow >= -25:
        return 0.15
    return 0.05


def competition_stars(row):
    """→ (별점 0.0~5.0 (0.5 단위), 항목별 점수 dict)"""
    a = STAR_AD.get(row.get("comp") or "", STAR_AD_UNKNOWN)
    b = _sat_points(row.get("posts"), row.get("vol"))
    c = _depth_points(row.get("ads"))
    d = _trend_points(row.get("wow"))
    est = b is None
    if est:
        # 발행량을 못 잰 키워드 — 모른다고 만점을 주지는 않는다. 중립값(0.75)으로 채운다.
        b = SAT_MAX * 0.5
    parts = {"ad": a, "sat": round(b, 2), "depth": c, "trend": d, "est": est}
    posts, vol = row.get("posts"), row.get("vol")
    if posts is not None and vol:
        # 원자료도 같이 남긴다 — 다음 보정 때 로그를 다시 뒤지지 않아도 되도록
        parts["r"] = round(posts / float(vol), 2)
        parts["posts"] = posts
    total = a + b + c + d
    return round(min(5.0, max(0.0, total)) * 2) / 2.0, parts


def ease_from_stars(stars):
    """별점 → 기회 점수의 경쟁 계수 E. 0★ 0.62 ~ 5★ 1.35 (기존 범위와 동일)"""
    if stars is None:
        return None
    return 0.62 + (stars / 5.0) * 0.73


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

    # E: 경쟁 여유 — 사이트에 보여주는 별점과 같은 근거를 쓴다
    #    (별점 ↔ 기회 점수가 서로 엇갈리지 않도록)
    stars, _ = competition_stars(row)
    E = ease_from_stars(stars)
    if E is None:
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

        trend, blogs, saturated = {}, {}, set()
        btotal, ctotal = {}, {}          # 누적 발행량 (블로그 / 카페)
        t0 = time.time()

        # ── 검색어트렌드 (5개씩 배치, 병렬) ─────────────────────
        batches = [allkw[i:i + 5] for i in range(0, len(allkw), 5)]
        fails = [0]

        def do_trend(b):
            if fails[0] >= MAX_FAILS:
                return {}
            try:
                r = datalab_batch(b)
                fails[0] = 0
                return r
            except Exception as e:
                fails[0] += 1
                if fails[0] <= 3:
                    print("datalab fail", b, e)
                return {}

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for r in ex.map(do_trend, batches):
                trend.update(r)
        if fails[0] >= MAX_FAILS:
            print("!! 검색어트렌드 연속 실패 — 일부 중단")
        print("datalab ok %d/%d (%.0fs)" % (len(trend), len(allkw), time.time() - t0))

        # ── 블로그 월간 발행량 (키워드당 1~10페이지, 병렬) ───────
        t1 = time.time()
        bfails = [0]

        def do_blog(k):
            if bfails[0] >= MAX_FAILS:
                return k, None, False, None
            try:
                v, sat, tot = blog_monthly(k)
                bfails[0] = 0
                return k, v, sat, tot
            except Exception as e:
                bfails[0] += 1
                if bfails[0] <= 3:
                    print("blog fail", k, e)
                return k, None, False, None

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for k, v, sat, tot in ex.map(do_blog, allkw):
                if v is not None:
                    blogs[k] = v
                    if sat:
                        saturated.add(k)
                if tot is not None:
                    btotal[k] = tot
        if bfails[0] >= MAX_FAILS:
            print("!! 블로그 검색 연속 실패 — 일부 중단")
        print("blog ok %d/%d · 포화(측정한계) %d개 (%.0fs)"
              % (len(blogs), len(allkw), len(saturated), time.time() - t1))

        # ── 카페 누적 발행량 (키워드당 1회, 병렬) ────────────────
        # 카페글 검색 API 는 postdate 가 없어 '월간'을 잴 수 없다 → 누적만 쓴다.
        t1c = time.time()
        cfails = [0]

        def do_cafe(k):
            if cfails[0] >= MAX_FAILS:
                return k, None
            try:
                t = cafe_total(k)
                cfails[0] = 0
                return k, t
            except Exception as e:
                cfails[0] += 1
                if cfails[0] <= 3:
                    print("cafe fail", k, e)
                return k, None

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for k, t in ex.map(do_cafe, allkw):
                if t is not None:
                    ctotal[k] = t
        if cfails[0] >= MAX_FAILS:
            print("!! 카페 검색 연속 실패 — 블로그 누적만으로 포화도 계산")
        print("cafe ok %d/%d (%.0fs)" % (len(ctotal), len(allkw), time.time() - t1c))

        # ── 검색량 최신화 (풀은 주 1회라 최대 7일 묵음) ──────────
        t2 = time.time()
        fresh = searchad_volumes(allkw)
        print("검색량 갱신 %d/%d (%.0fs)" % (len(fresh), len(allkw), time.time() - t2))

        for cat in TABS:
            for r in cand.get(cat, []):
                k = r["kw"]
                t = trend.get(k)
                if t:
                    r["spark"] = t["spark"]
                    r["wow"] = t["wow"]
                    r["idx"] = t["idx"]
                if blogs.get(k) is not None:
                    r["blog"] = blogs[k]
                    if k in saturated:
                        r["blogMin"] = True      # "이상"임을 사이트에 알림
                f = fresh.get(k)
                if f:
                    if f.get("vol"):
                        r["vol"] = f["vol"]
                    if f.get("ads"):
                        r["ads"] = f["ads"]
                    if f.get("comp"):
                        r["comp"] = f["comp"]
                # 콘텐츠 포화도용 누적 발행량 = 블로그 + 카페
                if btotal.get(k) is not None:
                    r["posts"] = btotal[k] + (ctotal.get(k) or 0)
                    r["bTot"] = btotal[k]
                    if ctotal.get(k) is not None:
                        r["cTot"] = ctotal[k]

        # 실제로 아무것도 못 받아왔으면 rich=False 로 정직하게 표시
        if not trend and not blogs:
            RICH = False
            print("!! 보강 데이터 0건 — SearchAd 신호만으로 산출 (키/권한 확인 필요)")

        # ── 포화도 분포 로그 (별점 보정용 — Actions 로그에서 확인) ──
        ratios = sorted(r["posts"] / float(r["vol"])
                        for cat in TABS for r in cand.get(cat, [])
                        if r.get("posts") is not None and (r.get("vol") or 0) > 0)
        if ratios:
            def q(p):
                return ratios[min(len(ratios) - 1, int(len(ratios) * p))]
            print("포화도(누적글÷월검색) 분포 n=%d — p10 %.2f · p25 %.2f · 중앙 %.2f "
                  "· p75 %.2f · p90 %.2f · 최대 %.2f"
                  % (len(ratios), q(.10), q(.25), q(.50), q(.75), q(.90), ratios[-1]))
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
            stars, parts = competition_stars(r)
            item = {
                "kw": r["kw"],
                "cat": cat.replace(":", " · "),
                "score": r["score"],
                "vol": r.get("vol"),
                "stars": stars,
                "starParts": parts,
                "sojae": sojae_for(r["kw"], cat, i, round_no),
            }
            for f in ("wow", "idx", "spark", "ads", "comp"):
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

    # ── 마이리얼트립 판매 TOP5 (여행:마이리얼트립 탭에만) ────────
    MRT_CAT = "여행:마이리얼트립"
    if MRT_OK and tabs.get(MRT_CAT):
        t3 = time.time()
        mkws = [it["kw"] for it in tabs[MRT_CAT]]

        def do_mrt(k):
            try:
                return k, fetch_myrealtrip_deals(k)
            except Exception as e:
                print("myrealtrip fail", k, e)
                return k, None

        got = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            for k, d in ex.map(do_mrt, mkws):
                if d:
                    got[k] = d
        for it in tabs[MRT_CAT]:
            if got.get(it["kw"]):
                it["deals"] = got[it["kw"]]
        print("마이리얼트립 상품 %d/%d (%.0fs)" % (len(got), len(mkws), time.time() - t3))
    elif not MRT_OK:
        print("MYREALTRIP_API_KEY 없음 → 연결 상품 생략")

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
                              "vol": it.get("vol"), "stars": it.get("stars")})
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
        if h.get("stars") is not None:
            bits.append("경쟁 여유 %s점" % ("%.1f" % h["stars"]).replace(".0", ""))
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
        "starMax": 5,
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
