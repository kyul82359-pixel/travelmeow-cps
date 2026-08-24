#!/usr/bin/env python3
"""쿠팡 파트너스 — 카테고리별 판매 베스트를 모아 coupang.json 을 만든다.

키(COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY)가 없으면 아무 일도 하지 않고
정상 종료한다. index.html 은 coupang.json 이 없거나 비면 쿠팡 탭을 숨긴다.
그래서 최종승인 전에 미리 얹어둬도 사이트가 깨지지 않는다.

⚠️ 파트너스 딥링크(productUrl)는 일부러 저장하지 않는다.
   딥링크를 그대로 붙이면 클릭 수익이 사이트 주인에게 귀속된다.
   수강생이 자기 파트너스 계정으로 직접 링크를 발급해야 하므로
   여기서는 순위·상품명·가격·로켓배송 여부만 남긴다.

⚠️ 호출 제한. 검색 API 는 시간당 10회로 알려져 있다(공식 문서의 분당 50회와 다름,
   3회 위반 시 계정 정지). 베스트 카테고리는 별도지만 한도가 공개돼 있지 않아
   하루 1회 · 카테고리당 1회 · 사이 2초로 보수적으로 돈다.
   실시간 호출은 절대 넣지 말 것 — 반드시 이 배치가 만든 JSON 만 읽어 쓴다.
"""
import hashlib
import hmac
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

ACCESS = os.environ.get("COUPANG_ACCESS_KEY", "").strip()
SECRET = os.environ.get("COUPANG_SECRET_KEY", "").strip()
DOMAIN = "https://api-gateway.coupang.com"

# 파트너스 베스트 카테고리 ID. 이름은 응답의 categoryName 으로 덮어쓰므로
# 여기 적힌 이름이 틀려도 결과는 정확하다. 유효하지 않은 ID 는 조용히 건너뛴다.
CATEGORIES = [
    (1001, "여성패션"), (1002, "남성패션"), (1003, "베이비패션"),
    (1004, "유아동패션"), (1005, "뷰티"), (1006, "출산/유아동"),
    (1007, "식품"), (1008, "주방용품"), (1009, "생활용품"),
    (1010, "홈인테리어"), (1011, "가전디지털"), (1012, "스포츠/레저"),
    (1013, "자동차용품"), (1014, "도서/음반/DVD"), (1015, "완구/취미"),
    (1016, "문구/오피스"), (1017, "반려동물용품"), (1018, "헬스/건강식품"),
    (1019, "국내여행"), (1020, "해외여행"),
]

WANT = 50          # 카테고리당 목표 개수 (API 가 덜 주면 주는 만큼만)
GAP = 2.0          # 호출 간격(초)
OUT = "coupang.json"

# 경로가 두 벌 돌아다닌다. v1 이 문서 기준이고, 예전 경로도 아직 살아 있다.
PATHS = [
    "/v2/providers/affiliate_open_api/apis/openapi/v1/products/bestcategories/%d",
    "/v2/providers/affiliate_open_api/apis/openapi/products/bestcategories/%d",
]


def auth(method, path, query):
    dtm = time.strftime("%y%m%dT%H%M%SZ", time.gmtime())
    msg = dtm + method + path + query
    sig = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return "CEA algorithm=HmacSHA256, access-key=%s, signed-date=%s, signature=%s" % (
        ACCESS, dtm, sig)


def call(path, query):
    url = DOMAIN + path + ("?" + query if query else "")
    req = urllib.request.Request(url, headers={
        "Authorization": auth("GET", path, query),
        "Content-Type": "application/json;charset=UTF-8",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        return json.load(r)


def fetch_best(cid):
    """(카테고리명, [상품…]) 또는 (None, None). 예외는 여기서 삼킨다."""
    query = "limit=%d" % WANT
    last = None
    for tpl in PATHS:
        path = tpl % cid
        try:
            res = call(path, query)
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (400, 404):
                continue          # 경로가 아니면 다음 경로로
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            print("  ! %d %s %s" % (cid, last, body))
            return None, None
        except Exception as e:
            last = str(e)
            continue

        rows = res.get("data") or []
        if not isinstance(rows, list) or not rows:
            print("  · %d 빈 응답 (rCode=%s %s)" % (cid, res.get("rCode"), res.get("rMessage")))
            return None, None

        name = ""
        items = []
        for i, it in enumerate(rows):
            nm = it.get("productName")
            if not nm:
                continue
            name = name or (it.get("categoryName") or "")
            price = it.get("productPrice")
            items.append({
                "r": len(items) + 1,
                "n": nm,
                "p": int(price) if isinstance(price, (int, float)) and price > 0 else None,
                "rocket": bool(it.get("isRocket")),
                "free": bool(it.get("isFreeShipping")),
            })
        if items:
            return (name or None), items
        return None, None

    print("  ! %d 실패 (%s)" % (cid, last))
    return None, None


def main():
    if not (ACCESS and SECRET):
        print("COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY 없음 → 쿠팡 탭 생략")
        return 0

    t0 = time.time()
    cats, total = [], 0
    for cid, fallback in CATEGORIES:
        name, items = fetch_best(cid)
        if items:
            cats.append({"id": cid, "name": name or fallback, "items": items})
            total += len(items)
            print("  %-14s %2d개" % (name or fallback, len(items)))
        time.sleep(GAP)

    if not cats:
        # 한 건도 못 받았으면 기존 파일을 지우지 않는다 (어제 값이라도 남기는 게 낫다).
        print("수집 0건 — coupang.json 갱신하지 않음")
        return 1

    out = {
        "updated": time.strftime("%Y-%m-%d %H:%M UTC"),
        "want": WANT,
        "cats": cats,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("쿠팡 %d개 카테고리 · 상품 %d개 (%.0fs)" % (len(cats), total, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
