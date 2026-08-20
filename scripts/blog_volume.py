# -*- coding: utf-8 -*-
"""
블로그 월간 발행량 측정

기존 방식(폐기)
    최신 100건을 가져와 "며칠에 걸쳐 쌓였나"로 나눔.
    → 날짜(YYYYMMDD) 단위라 시각 정보가 없어서, 발행량이 많을수록
      반올림 오차가 지배적이 됨. 100건이 하루 안에 다 들어오면
      100/1×30 = 3,000 이 최대치가 되어 천장에 걸림.
      실측: 호텔 3,000(표시) vs 47,600+(실제) — 16배 과소평가.

새 방식
    "완전한 하루"들의 실제 건수를 직접 센다.
    - 가장 최신 날짜 = 오늘(진행 중) → 잘린 하루이므로 버림
    - 가장 오래된 날짜 = 조회 한도에서 끊긴 하루 → 역시 버림
    - 남은 구간(양끝이 온전한 날들)의 건수 C, 걸친 일수 D
    - 월 발행량 = C / D × 30
    반올림 오차가 원리적으로 사라진다.

    3개 이상의 서로 다른 날짜가 나올 때까지 100건씩 페이징한다.
    네이버 검색 API는 start 최댓값이 1000이라 최대 1,000건까지 볼 수 있고,
    그 안에서도 날짜가 안 갈리면(= 하루에 1,000건 이상) 측정 불가로 보고
    SATURATED_MONTHLY 로 확정한다.
"""
import datetime as dt

PAGE = 100
MAX_START = 1000                  # 네이버 검색 API 상한
MAX_PAGES = MAX_START // PAGE     # 10
SATURATED_MONTHLY = 30000         # 1,000건이 하루 안 → 월 3만+ 확정


def _groups(dates):
    """['20260820','20260820','20260819', ...] → [('20260820',2), ('20260819',1), ...]
    (최신순 정렬 입력 가정, 연속 구간을 묶는다)"""
    out = []
    for d in dates:
        if out and out[-1][0] == d:
            out[-1][1] += 1
        else:
            out.append([d, 1])
    return [(d, n) for d, n in out]


def _to_date(s):
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


MIN_COMPLETE_DAYS = 2     # 요일 편차를 줄이려면 완전한 날이 최소 2개는 필요


def _core(g, exhausted):
    """온전한 날짜 그룹만 남긴다.
    - 맨 앞(가장 최신) = 오늘, 아직 진행 중 → 항상 버림
    - 맨 뒤(가장 오래된) = 조회 한도에서 잘린 날 → 버림.
      단 결과가 바닥난(exhausted) 경우엔 잘린 게 아니라 진짜 끝이므로 살림
    """
    return g[1:] if exhausted else g[1:-1]


def _monthly(core):
    C = sum(n for _, n in core)
    D = (_to_date(core[0][0]) - _to_date(core[-1][0])).days + 1
    return int(round(C / max(1, D) * 30))


def estimate_from_dates(dates, exhausted):
    """수집한 postdate 목록(최신순) → 월 발행량. 아직 판단 불가면 None."""
    if not dates:
        return 0
    core = _core(_groups(dates), exhausted)
    if len(core) < MIN_COMPLETE_DAYS:
        return None                      # 페이지를 더 봐야 한다
    return _monthly(core)


def measure(fetch_page, kw):
    """fetch_page(kw, start) -> (postdate 리스트, 그 페이지 건수)

    반환: (월 발행량, 포화 여부)
    """
    dates, exhausted = [], False
    for p in range(MAX_PAGES):
        page_dates, n = fetch_page(kw, p * PAGE + 1)
        dates.extend(page_dates)
        if n < PAGE:
            exhausted = True
        est = estimate_from_dates(dates, exhausted)
        if est is not None:
            return est, False
        if exhausted:
            break

    g = _groups(dates)

    # ── 검색 결과가 바닥난 경우: 있는 게 전부다 ──────────────────
    if exhausted:
        if len(g) <= 1:
            # 전부 같은 날 + 그게 전체 결과 → 그 건수가 곧 총량
            return len(dates), False
        core = g[1:]                       # 오늘만 버리고 나머지 전부 사용
        return _monthly(core), False

    # ── 1,000건을 다 봤는데도 완전한 날 2개를 못 만든 경우 ────────
    if len(g) == 1:
        # 하루에 1,000건 이상 — 측정 불가, 최상위 경쟁으로 확정
        return SATURATED_MONTHLY, True
    if len(g) >= 3:
        # 완전한 날이 1개뿐이지만 값은 신뢰할 수 있다
        return _monthly(g[1:-1]), True
    # 그룹 2개(오늘 + 잘린 어제) — 어제분은 하한선일 뿐
    return max(SATURATED_MONTHLY // 3, g[1][1] * 30), True
