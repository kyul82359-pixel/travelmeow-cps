# -*- coding: utf-8 -*-
"""
2026-08-20 15:00 KST 에 네이버 블로그 검색 API로 직접 측정한 실제 날짜 분포로
새 발행량 알고리즘을 검증한다. 배포 전 반드시 통과해야 한다.

실행: python3 scripts/test_blog_volume.py
"""
import sys, os
import datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from blog_volume import measure, PAGE, SATURATED_MONTHLY  # noqa


def _sparse(newest, oldest, total):
    """newest~oldest 사이에 total건을 고르게 흩뿌린 (날짜, 건수) 목록 (최신순).
    빈 날이 섞인 저빈도 키워드를 재현한다."""
    d0 = dt.date(int(newest[:4]), int(newest[4:6]), int(newest[6:8]))
    d1 = dt.date(int(oldest[:4]), int(oldest[4:6]), int(oldest[6:8]))
    span = (d0 - d1).days + 1
    out = []
    for i in range(total):
        day = d0 - dt.timedelta(days=(i * (span - 1)) // (total - 1))
        key = day.strftime("%Y%m%d")
        if out and out[-1][0] == key:
            out[-1] = (key, out[-1][1] + 1)
        else:
            out.append((key, 1))
    return out


def pager(day_counts):
    """{'20260820': 18, '20260819': 28, ...} (최신순) 를 페이지 단위로 흘려주는 가짜 API.
    실제 API처럼 start 1000 이후로는 아무것도 주지 않는다."""
    seq = []
    for d, n in day_counts:
        seq.extend([d] * n)

    def fetch(kw, start):
        chunk = seq[start - 1: start - 1 + PAGE]
        return chunk, len(chunk)
    return fetch


# ── 오늘(8/20) 15:00에 실제로 측정한 분포 ───────────────────────────
# (키워드, [(날짜, 건수)...], 현재 사이트 표시값, 기대 판정)
CASES = [
    # 100건 전수 조회로 날짜별 건수를 직접 센 케이스
    ("에이블트라이크",
     [("20260820", 18), ("20260819", 28), ("20260818", 34),
      ("20260817", 14), ("20260816", 6)],
     600, (700, 850)),          # 완전한 날 8/17~8/19 → (14+34+28)/3*30 = 760

    # 저빈도: 100건이 104일에 걸침 (1번째 8/20, 100번째 5/8) — 하루 0~1건, 중간에 빈 날 다수
    ("분홍코끼리", _sparse("20260820", "20260508", 100), 28, (24, 33)),

    # 초고빈도: 1,000건이 전부 오늘 (호텔) → 측정 불가 → 포화 확정
    ("호텔", [("20260820", 1200)], 3000, (SATURATED_MONTHLY, SATURATED_MONTHLY)),

    # 꽃다발: 오늘 ~450건, 8/19 ~430건, 8/18 이후
    ("꽃다발",
     [("20260820", 450), ("20260819", 430), ("20260818", 300)],
     3000, (11000, 14000)),     # 완전한 날 8/19 하나 → 430*30 = 12,900

    # 팔찌: 101=8/20, 501=8/19, 1000=8/17
    ("팔찌",
     [("20260820", 300), ("20260819", 350), ("20260818", 250), ("20260817", 200)],
     3000, (8500, 9500)),       # 완전한 날 8/19·8/18 → (350+250)/2*30 = 9,000

    # 홍콩여행: 101=8/19, 301=8/18, 601=8/16
    ("홍콩여행",
     [("20260820", 60), ("20260819", 120), ("20260818", 170),
      ("20260817", 150), ("20260816", 130)],
     1500, (4000, 4800)),       # 완전한 날 8/19·8/18 → (120+170)/2*30 = 4,350
]


def run():
    print("%-16s %8s %10s %10s   %s" % ("키워드", "기존값", "새 알고리즘", "기대범위", "판정"))
    print("-" * 74)
    ok = True
    for kw, dist, old, (lo, hi) in CASES:
        est, sat = measure(pager(dist), kw)
        passed = lo <= est <= hi
        ok = ok and passed
        print("%-16s %8s %10s %10s   %s%s" % (
            kw, f"{old:,}", f"{est:,}", f"{lo:,}~{hi:,}",
            "✅" if passed else "❌ 실패",
            " (포화)" if sat else ""))
    print("-" * 74)

    # 경계 조건
    edge = [
        ("빈 결과", [], 0, 0),
        # 전체 결과가 5건뿐이고 전부 오늘 → 총량이 곧 5건 (천장 오판 금지)
        ("전체 5건만(고갈)", [("20260820", 5)], 5, 5),
        ("이틀치(고갈)", [("20260820", 3), ("20260819", 7)], 210, 210),
        # 예전 방식이 3,000을 뱉던 자리 — 페이징 후 완전한 날 8/19·8/18 평균 (50+40)/2*30
        ("100건 전부 오늘(+페이징)",
         [("20260820", 100), ("20260819", 50), ("20260818", 40)], 1350, 1350),
        # 주말 편차: 완전한 날 2개를 평균내는지
        ("이틀 평균(20,40)",
         [("20260820", 10), ("20260819", 20), ("20260818", 40), ("20260817", 30)],
         900, 900),
    ]
    for name, dist, lo, hi in edge:
        est, _ = measure(pager(dist), name)
        p = lo <= est <= hi
        ok = ok and p
        print("%-24s → %-8s %s" % (name, f"{est:,}", "✅" if p else f"❌ (기대 {lo}~{hi})"))

    print()
    print("전체:", "✅ 통과" if ok else "❌ 실패")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
