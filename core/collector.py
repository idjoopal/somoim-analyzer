"""
다감노📸 데이터 수집 모듈

외부 의존:
- requests
- (선택) tqdm

주요 함수:
- collect_posts(start_ym, end_ym, progress=None) -> list[dict]
- collect_photos(start_ym, end_ym, progress=None) -> list[dict]

기간은 YYYYMM 정수 쌍(start_ym, end_ym)으로 표현한다 — 아래 "기간" 절 참고.
"""

from __future__ import annotations

import re
import time
import requests
from collections import Counter
from datetime import datetime, date, timedelta
from typing import Callable, Optional


# ═══════════════════════════════════════════════════════════════
# 설정
# ═══════════════════════════════════════════════════════════════

GROUP_ID   = "2d4b415a-d2f4-11eb-97b4-0a0d8e52bd411"
GROUP_NAME = "다감노📸"

BASE_URL = "https://www.somoim.co.kr"
CDN_BASE = "https://d3vo2hyhx9t76k.cloudfront.net"

EPOCH_OFFSET = 1_000_000_000  # unix_ts = (w_t or ot) + EPOCH_OFFSET

MAX_PAGES = 200  # /api/articles 페이지 한도 (20건/페이지)
# 공지는 '출사일' 기준으로 거르는데 작성일은 그보다 앞설 수 있어, 기간 시작보다
# 이만큼 과거까지 더 받아 둔다(예: 12월에 올린 1월 출사 공지).
FETCH_MARGIN_MONTHS = 12

HEADERS = {
    "User-Agent":   "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1",
    "Content-Type": "application/json",
    "Referer":      f"{BASE_URL}/{GROUP_ID}1",
}

# 카테고리 분류
CAT_LABEL = {"A": "공지", "E": "후기", "J": "가입인사"}

# 제목에서 탐지할 원본 태그 (regex용) — 겹치는 1:1 쌍은 긴 것을 먼저
RAW_CATS = ["1:1인물출사", "1:1인물", "인물", "인풍", "풍경", "GN", "보정", "문화"]
# 원본 태그 → 집계·표시용 정규화 카테고리
CAT_NORMALIZE = {"1:1인물": "인물", "1:1인물출사": "인물", "인풍": "인물&풍경"}

OUTING_CATS     = ["인물", "인물&풍경", "풍경", "GN"]
# `일반공지`는 제목 태그로 오지 않는다 — 어느 카테고리에도 안 걸리는 공지를
# 사람이 보정에서 지정하는 값이다. 출사가 아니므로 집계에서 빠진다.
NON_OUTING_CATS = ["보정", "문화", "일반공지"]
ALL_CATS        = OUTING_CATS + NON_OUTING_CATS

CAT_RX    = re.compile(r"\[(" + "|".join(re.escape(c) for c in RAW_CATS) + r")\]")
CANCEL_RX = re.compile(r"[\(\[]\s*펑\s*[\)\]]")

DATE_PATTERN_WITH_YEAR = r"20(\d{2})[./\-](\d{1,2})[./\-](\d{1,2})"
# 구분자 없이 붙여 쓴 `260308`. 연 20~29·월 01~12·일 01~31만 받고 앞뒤에 숫자가
# 붙으면 물린다 — 그래야 다른 여섯 자리 숫자를 날짜로 오해하지 않는다.
DATE_PATTERN_COMPACT   = r"(?<!\d)(2\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"
DATE_PATTERNS_NO_YEAR  = [
    r"(\d{1,2})\.(\d{2})\s*[~\-]\s*\d{1,2}\.\d{2}",   # 범위
    r"(\d{1,2})\.(\d{2})",
    r"(\d{1,2})/(\d{2})",
    r"(\d{1,2})월\s*(\d{1,2})일",
]


# ═══════════════════════════════════════════════════════════════
# 기간 (YYYYMM) — 단일 진리원
# ═══════════════════════════════════════════════════════════════
#
# 수집·필터·집계·표시가 모두 이 함수들을 공유해 판정이 갈라지지 않게 한다.
# 기간은 (start_ym, end_ym) 정수 쌍이며 ym = YYYY*100 + MM (예: 202603).
#   한 해 전체 → (202601, 202612)   한 달 → (202605, 202605)
#
# `month_axis`가 월 축의 원천이다 — 데이터가 아니라 **선택한 기간**에서 축을
# 만들기 때문에 활동이 없는 달도 0으로 남는다. 연도를 버리지 않으므로
# 2025-03과 2026-03이 한 칸에 합쳐지지 않는다.

def ym_of(d) -> int:
    """date/datetime → YYYYMM 정수."""
    return d.year * 100 + d.month


def ym_split(ymv: int) -> tuple[int, int]:
    """YYYYMM → (연, 월)."""
    return divmod(int(ymv), 100)


def ym_valid(ymv) -> bool:
    """YYYYMM 형식이 유효한지(월이 1~12인지) 검사."""
    try:
        y, m = divmod(int(ymv), 100)
    except (TypeError, ValueError):
        return False
    return 1900 <= y <= 9999 and 1 <= m <= 12


def ym_add(ymv: int, months: int) -> int:
    """YYYYMM에 월 단위 가감 (연도 넘김 처리). `ym_add(202601, -1) == 202512`."""
    y, m = divmod(int(ymv), 100)
    total = y * 12 + (m - 1) + months
    return (total // 12) * 100 + (total % 12) + 1


def ym_diff(a: int, b: int) -> int:
    """a - b 를 개월 수로. `ym_diff(202603, 202601) == 2`."""
    ya, ma = divmod(int(a), 100)
    yb, mb = divmod(int(b), 100)
    return (ya * 12 + ma) - (yb * 12 + mb)


def in_ym_range(ymv: int, start_ym: int, end_ym: int) -> bool:
    """ym이 [start_ym, end_ym] 안에 있는지 (양끝 포함)."""
    return int(start_ym) <= int(ymv) <= int(end_ym)


def month_axis(start_ym: int, end_ym: int) -> list[int]:
    """범위 내 모든 월을 오름차순 리스트로. 역전된 입력은 정렬해서 받아준다."""
    start_ym, end_ym = int(start_ym), int(end_ym)
    if end_ym < start_ym:
        start_ym, end_ym = end_ym, start_ym
    return [ym_add(start_ym, i) for i in range(ym_diff(end_ym, start_ym) + 1)]


def is_multi_year(start_ym: int, end_ym: int) -> bool:
    """범위가 두 해 이상에 걸치는지 — 월 라벨에 연도를 넣을지 결정."""
    return int(start_ym) // 100 != int(end_ym) // 100


def ym_label(ymv: int, *, multi_year: bool = True) -> str:
    """월 축 라벨. 다년이면 `2026-03`, 한 해 안이면 `3월`."""
    y, m = divmod(int(ymv), 100)
    return f"{y}-{m:02d}" if multi_year else f"{m}월"


def period_label(start_ym: int, end_ym: int) -> str:
    """사람이 읽는 기간 표기. 한 해 전체·단일 월은 기존 표기를 그대로 유지."""
    sy, sm = divmod(int(start_ym), 100)
    ey, em = divmod(int(end_ym), 100)
    if int(start_ym) == int(end_ym):
        return f"{sy}년 {sm}월"
    if sy == ey:
        return f"{sy}년 전체" if (sm, em) == (1, 12) else f"{sy}년 {sm}~{em}월"
    return f"{sy}-{sm:02d} ~ {ey}-{em:02d}"


def period_tag(start_ym: int, end_ym: int) -> str:
    """파일명·커밋 메시지용 짧은 태그."""
    return str(int(start_ym)) if int(start_ym) == int(end_ym) else f"{int(start_ym)}-{int(end_ym)}"


# ═══════════════════════════════════════════════════════════════
# 헬퍼
# ═══════════════════════════════════════════════════════════════

ProgressFn = Optional[Callable[[str, float], None]]


def _ts_to_dt(ts: int) -> datetime:
    """소모임 자체 타임스탬프 → datetime"""
    return datetime.fromtimestamp(ts + EPOCH_OFFSET)


def _post_dt(p: dict) -> datetime:
    """게시글 작성 시각 (공지 핀고정시 ot 사용)"""
    ts = p["ot"] if p.get("w_t") == 2000000000 else p["w_t"]
    return _ts_to_dt(ts)


def _parse_title_meta(title: str) -> dict:
    """제목에서 카테고리·취소여부 추출 (원본 태그를 정규화 카테고리로 변환)"""
    tags = CAT_RX.findall(title)
    raw = tags[0] if tags else None
    category = CAT_NORMALIZE.get(raw, raw) if raw else None
    return {
        "category":    category,
        "is_outing":   category in OUTING_CATS if category else False,
        "is_canceled": bool(CANCEL_RX.search(title)),
    }


def infer_outing_date(title: str, content: str, posted_dt: datetime) -> Optional[date]:
    """
    출사 날짜 추론.

    추론 순서:
    1) 내용의 '출사진행날짜 : YY.MM.DD' (연도 명시 → 그대로 신뢰)
    2) 제목의 'YYYY.MM.DD' 패턴 (연도 명시 → 그대로 신뢰)
    3) 제목의 MM.DD 패턴 (연도 없음) — 작성일 기반 추론
       * 같은 해 → 다음 해 순서로 시도
       * 출사일 ≥ 작성일 AND (출사일 − 작성일) < 365일
    """
    posted_date = posted_dt.date()

    # 1) 내용 '출사진행날짜' 필드
    m = re.search(r"출사진행날짜\s*[:\-]\s*" + DATE_PATTERN_WITH_YEAR, content)
    if m:
        try:
            return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 노이즈 제거
    t = CANCEL_RX.sub("", title)
    t = re.sub(r"[<>《》]", " ", t)

    # 2) 제목 'YYYY.MM.DD'
    m = re.search(DATE_PATTERN_WITH_YEAR, t)
    if m:
        try:
            return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 3) MM.DD 패턴 (연도 없음)
    md = None
    for pat in DATE_PATTERNS_NO_YEAR:
        m = re.search(pat, t)
        if m:
            try:
                mo, day = int(m.group(1)), int(m.group(2))
                if 1 <= mo <= 12 and 1 <= day <= 31:
                    md = (mo, day)
                    break
            except ValueError:
                continue

    if not md:
        return None

    mo, day = md
    for year_offset in (0, 1):
        try:
            cand = date(posted_date.year + year_offset, mo, day)
        except ValueError:
            continue
        if cand >= posted_date and (cand - posted_date).days < 365:
            return cand
    return None


def _emit(progress: ProgressFn, msg: str, pct: float) -> None:
    if progress is not None:
        progress(msg, pct)


# ═══════════════════════════════════════════════════════════════
# API 호출
# ═══════════════════════════════════════════════════════════════

def _fetch_paginated(
    endpoint: str,
    list_key: str,
    stop_before_ym: Optional[int],
    progress: ProgressFn = None,
    progress_label: str = "수집",
    extra_payload: Optional[dict] = None,
    should_stop: Optional[Callable[[list[dict], datetime], bool]] = None,
) -> list[dict]:
    """공통 페이지네이션 수집기 (최신순 → 과거로).

    Args:
        stop_before_ym: 이 YYYYMM보다 오래된 글을 만나면 중단. None이면 연도 기반
            종료를 하지 않는다(가입인사처럼 전 기간을 훑어야 할 때).
        extra_payload: API 호출에 추가할 페이로드(예: `{"cat": "J"}`)로 서버 측 필터 활용.
        should_stop: 각 페이지 누적 후 호출하는 조기 종료 콜백 — `(all_items, oldest_dt) -> bool`.
    """
    all_items: list[dict] = []
    s_t = None
    base_payload: dict = {"gid": GROUP_ID, "wql": 20}
    if extra_payload:
        base_payload.update(extra_payload)

    hit_cap = True  # 아래 루프가 break 없이 끝나면 페이지 한도에 닿은 것
    for page in range(1, MAX_PAGES + 1):
        payload = dict(base_payload)
        if s_t is not None:
            payload["s_t"] = s_t

        try:
            r = requests.post(BASE_URL + endpoint, headers=HEADERS, json=payload, timeout=10)
            r.raise_for_status()
        except Exception as e:
            _emit(progress, f"[ERROR] {endpoint} page {page}: {e}", 0.5)
            hit_cap = False
            break

        data  = r.json()
        items = data.get(list_key, [])
        if not items:
            hit_cap = False
            break

        all_items.extend(items)
        _emit(progress, f"{progress_label} {page}페이지, 누적 {len(all_items)}개", min(0.4, page / 50))

        # 대상 기간보다 오래된 글에 닿으면 중단
        oldest = items[-1]
        oldest_ts = oldest["ot"] if oldest.get("w_t") == 2000000000 else oldest["w_t"]
        oldest_dt = _ts_to_dt(oldest_ts)
        if stop_before_ym is not None and ym_of(oldest_dt) < stop_before_ym:
            hit_cap = False
            break

        if should_stop is not None and should_stop(all_items, oldest_dt):
            hit_cap = False
            break

        if data.get("eof") == "Y" or len(items) < 20:
            hit_cap = False
            break

        s_t = items[-1].get("ot") or items[-1].get("w_t")
        time.sleep(0.15)

    if hit_cap:
        # 조용한 절단은 "데이터가 없다"로 오해되므로 반드시 알린다.
        _emit(progress,
              f"⚠️ {progress_label}: 페이지 한도({MAX_PAGES}p, 약 {MAX_PAGES * 20}건)에 도달해 "
              "더 과거는 받지 못했습니다. 기간을 좁혀 나눠 수집하세요.", 0.5)

    return all_items


# ═══════════════════════════════════════════════════════════════
# API 응답 진단
# ═══════════════════════════════════════════════════════════════
#
# 비공식 API라 응답 스펙 문서가 없다. 우리가 쓰는 키만 꺼내 쓰다 보니
# **무엇을 안 쓰고 있는지**를 모른다 — 첨부 이미지 id가 오는지, 본문이
# 전문인지 미리보기인지도 추측만 가능하다. 그래서 실제 응답을 요약해 남긴다.

# collect_posts가 원본 항목에서 실제로 읽는 키
CONSUMED_POST_KEYS = {"id", "at", "c", "cat", "wn", "wid", "lc", "rn", "ic", "w_t", "ot"}

_SAMPLE_MAX = 120   # 예시 값이 길면 잘라 둔다(본문이 통째로 들어오면 시트가 터진다)


def summarize_raw_fields(raw_items: list[dict],
                         consumed: Optional[set[str]] = None) -> list[dict]:
    """원본 응답에 실제로 담겨 온 필드를 요약.

    반환 각 행: `{필드, 사용중, 건수, 예시, 비고}`
    사용하지 않는 키가 이미지 id 같은 쓸 만한 정보를 담고 있는지 눈으로 확인하는 용도.
    """
    consumed = CONSUMED_POST_KEYS if consumed is None else consumed
    counts: Counter = Counter()
    sample: dict[str, str] = {}
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            counts[k] += 1
            if k not in sample and v not in (None, "", [], {}):
                s = str(v)
                sample[k] = s if len(s) <= _SAMPLE_MAX else s[:_SAMPLE_MAX] + "…"
    return [
        {"필드": k, "사용중": "예" if k in consumed else "",
         "건수": n, "예시": sample.get(k, ""),
         "비고": "" if k in consumed else "미사용 — 쓸 만한지 확인"}
        for k, n in counts.most_common()
    ]


MIN_CUT_LENGTH = 40   # 이보다 짧은 벽은 잘림으로 보지 않는다 (아래 설명)


def body_cut_length(lengths, min_hits: int = 3,
                    min_len: int = MIN_CUT_LENGTH) -> Optional[int]:
    """본문이 잘리는 길이를 **데이터에서** 알아낸다. 안 잘렸으면 None.

    목록 API가 몇 자에서 자르는지는 문서에 없다. 상수로 박아 두면 API가 값을
    바꿀 때 조용히 틀린 안내를 하게 된다. 대신 길이 분포의 **벽**을 찾는다 —
    서로 다른 글 여러 개가 정확히 같은 길이에서 끝나고 그보다 긴 글이 하나도
    없으면, 그건 우연이 아니라 잘린 것이다.

    두 가지로 오탐을 막는다.

    · `min_hits`건 미만이면 판단하지 않는다. 가장 긴 글 하나는 그냥 가장 긴
      글일 뿐이다.
    · `min_len`보다 짧은 벽도 무시한다. 미리보기를 준다면 문장 몇 개는 준다.
      "닉 다녀왔습니다" 같은 짧은 글 셋이 우연히 같은 길이인 쪽이 훨씬 흔하고,
      **거짓 경고는 진짜 경고를 죽인다.** 이 값은 답이 아니라 "이보다 짧으면
      추측하지 않겠다"는 선이다 — 실제 잘리는 길이는 여전히 데이터가 정한다.

    `Counter.most_common`을 쓰지 않는 이유: 본문이 아예 빈 글이 잘린 글보다
    많으면 최빈값은 0이 되고, **정작 잘리고 있는데 아니라고 답한다.**
    """
    counts = Counter(int(n) for n in lengths if n)
    if not counts:
        return None
    longest = max(counts)
    if longest < min_len or counts[longest] < min_hits:
        return None
    return longest


def summarize_body_lengths(raw_items: list[dict], key: str = "c") -> dict:
    """본문 길이 분포 — API가 본문을 잘라 주는지 판별한다.

    서로 다른 글 수백 개가 **정확히 같은 길이**라면 그건 우연이 아니라 잘린
    것이다. 잘린 본문은 참석자 추출·출사일 추론의 입력이라 분석 정확도에
    직접 영향을 준다.
    """
    lens = [len(str(it.get(key) or "")) for it in (raw_items or [])
            if isinstance(it, dict)]
    if not lens:
        return {"건수": 0}
    ordered = sorted(lens)
    top_len, top_n = Counter(lens).most_common(1)[0]
    cut = body_cut_length(lens)
    return {
        "건수": len(lens),
        "최소": ordered[0],
        "중앙": ordered[len(ordered) // 2],
        "최대": ordered[-1],
        "최빈길이": top_len,
        "최빈길이_건수": top_n,
        "잘림_의심": cut is not None,
        "잘린길이": cut,
    }


# ═══════════════════════════════════════════════════════════════
# 게시글 수집
# ═══════════════════════════════════════════════════════════════

def collect_posts(
    start_ym: int,
    end_ym: int,
    progress: ProgressFn = None,
    keep_unclassified: bool = False,
    field_report: Optional[dict] = None,
) -> list[dict]:
    """
    게시글 수집 + 기간 필터링.

    필터링 규칙:
    - cat=A (공지): **출사일** 기준 (작성일 무관 — 출사가 기간 안이면 포함)
    - cat=E (후기), cat=J (가입인사): **작성일** 기준

    Args:
        start_ym, end_ym: 대상 기간 YYYYMM (양끝 포함). 한 해 전체는 202601~202612.
        progress: 진행 콜백 fn(msg: str, pct: float)
        keep_unclassified: True면 출사일 추론 실패한 cat=A 공지를 버리지 않고
            outing_date=None으로 포함(기간 게이트는 작성일 기준)하고 검토 대상으로 표시.
            기본 False는 기존 동작(추론 실패 공지 제외)을 그대로 유지.
        field_report: dict를 주면 원본 응답 요약(`fields`·`body`)을 채워 넣는다.
            추가 API 호출 없이 이미 받은 응답을 그대로 들여다본다.

    Returns:
        list of dict with keys:
            id, author, wid, title, body(str), outing_date(str|None),
            posted_at(datetime), cat, cat_label, category,
            is_outing, is_canceled, likes, comments, images,
            needs_review(bool), review_reason(str)
    """
    _emit(progress, "게시글 수집 시작…", 0.0)
    raw = _fetch_paginated("/api/articles", "cs",
                           ym_add(start_ym, -FETCH_MARGIN_MONTHS),
                           progress, "게시글")
    _emit(progress, f"게시글 원본 {len(raw)}개 수집 완료", 0.4)

    if field_report is not None:
        field_report["fields"] = summarize_raw_fields(raw)
        field_report["body"] = summarize_body_lengths(raw)

    posts: list[dict] = []
    for p in raw:
        dt   = _post_dt(p)
        cat  = p.get("cat", "")
        meta = _parse_title_meta(p["at"])
        review_reasons: list[str] = []

        if cat == "A":
            od = infer_outing_date(p["at"], p.get("c", ""), dt)
            if od is None:
                # 출사일 추론 실패 → 작성일로 폴백 게이트
                if not keep_unclassified:
                    continue
                if not in_ym_range(ym_of(dt), start_ym, end_ym):
                    continue
                outing_date = None
                review_reasons.append("출사일 미상")
            else:
                if not in_ym_range(ym_of(od), start_ym, end_ym):
                    continue
                outing_date = od.isoformat()
        else:
            if not in_ym_range(ym_of(dt), start_ym, end_ym):
                continue
            outing_date = None

        if meta["category"] is None and cat in ("A", "E"):
            review_reasons.append("카테고리 미상")

        posts.append({
            "id":          p["id"],
            "author":      p.get("wn", ""),
            "wid":         p.get("wid", ""),
            "title":       p["at"],
            "body":        p.get("c", ""),
            "outing_date": outing_date,
            "posted_at":   dt,
            "cat":         cat,
            "cat_label":   CAT_LABEL.get(cat, cat),
            "category":    meta["category"],
            "is_outing":   meta["is_outing"],
            "is_canceled": meta["is_canceled"] and cat == "A",
            "likes":       p.get("lc", 0),
            "comments":    p.get("rn", 0),
            "images":      p.get("ic", 0),
            "needs_review":  bool(review_reasons),
            "review_reason": ", ".join(review_reasons),
        })

    _emit(progress, f"게시글 필터 후 {len(posts)}개", 0.5)
    return posts


# ═══════════════════════════════════════════════════════════════
# 사진 수집
# ═══════════════════════════════════════════════════════════════

def collect_photos(
    start_ym: int,
    end_ym: int,
    progress: ProgressFn = None,
) -> list[dict]:
    """
    사진 수집 + 기간 필터링 (**작성일** 기준).

    has_comment=True인 사진은 "테마 참여 예상"으로 표시.

    Args:
        start_ym, end_ym: 대상 기간 YYYYMM (양끝 포함)
        progress: 진행 콜백

    Returns:
        list of dict with keys:
            id, author, wid, posted_at(datetime), likes, comments,
            has_comment, url_large, url_medium, url_small, url_thumb
    """
    _emit(progress, "사진 수집 시작…", 0.5)
    # 사진은 작성일 기준이라 마진 없이 기간 시작까지만 받으면 된다.
    raw = _fetch_paginated("/api/photos", "ps", start_ym, progress, "사진")
    _emit(progress, f"사진 원본 {len(raw)}개 수집 완료", 0.9)

    photos: list[dict] = []
    for p in raw:
        dt = _ts_to_dt(p["w_t"])
        if not in_ym_range(ym_of(dt), start_ym, end_ym):
            continue

        pid = p["id"]
        photos.append({
            "id":          pid,
            "author":      p.get("wn", ""),
            "wid":         p.get("wid", ""),
            "posted_at":   dt,
            "likes":       p.get("lc", 0),
            "comments":    p.get("rn", 0),
            "has_comment": p.get("rn", 0) > 0,
            "url_large":   f"{CDN_BASE}/{pid}.png",
            "url_medium":  f"{CDN_BASE}/{pid}m.png",
            "url_small":   f"{CDN_BASE}/{pid}s.png",
            "url_thumb":   f"{CDN_BASE}/{pid}n.png",
        })

    _emit(progress, f"사진 필터 후 {len(photos)}개", 1.0)
    return photos


# ═══════════════════════════════════════════════════════════════
# 후기 본문 기반 참석자 추적
# ═══════════════════════════════════════════════════════════════
#
# 소모임 댓글 내용은 비공개라 가져올 수 없지만, 후기글 본문(`body`)에
# 참석자 명단이 적혀 있어 이를 파싱해 "어떤 출사에 누가 참석했나"를 만든다.
# 핵심 전제(실측): 후기 본문의 이름은 '실명', 게시글 작성자명은 '닉네임'이라
# 이름공간이 다르다 → 멤버 마스터는 실명↔닉네임 매핑을 함께 보유한다.

# 본문에서 사람 이름으로 오인되는 일반 명사/카테고리어 (추출 제외)
NAME_BLACKLIST: set[str] = {
    "정모", "정보", "후기", "사진", "출사", "촬영", "참여", "참석", "참가",
    "오늘", "내일", "어제", "이번", "다음", "지난", "다같이", "모두", "다들",
    "감사", "수고", "고생", "준비", "진행", "마무리", "종료", "시작",
    "그리고", "그래서", "하지만", "정도", "조금", "많이", "정말", "너무",
    "모임장", "운영진", "신입", "회원", "멤버", "여러분", "님들",
    "습니다", "니다", "있습", "있었", "있는", "없는", "했습", "됩니다",
    # 정규화 카테고리어 (제목/본문에 태그가 그대로 들어온 경우)
    "인물", "인물&풍경", "풍경", "보정", "문화",
    # 영문 일반 단어 (영어 닉네임 정규식 확장 후 노이즈)
    "the", "and", "for", "with", "you", "are", "this", "that", "have",
    "The", "And", "For", "With", "You", "Are", "This", "That", "Have",
    "https", "http", "www", "com", "kr", "net", "org",
}

NAME_RX = re.compile(r"[가-힣]{2,4}|[A-Za-z]{2,10}")

# 이름 해소 매핑(name_resolution)의 특수값.
# 후기에서 추출된 이름이 마스터에 없을 때 사용자가 드롭다운으로 지정한 처리:
#   - LEFT_MEMBER: 나간 멤버(탈퇴·강퇴) → 추적하지 않음
#   - NOT_A_NAME:  이름 아님(노이즈) → 추적하지 않음
#   - 그 외 문자열: 그 마스터 닉네임으로 정규화(예: "음승구" → "승구")
LEFT_MEMBER = "__LEFT__"
NOT_A_NAME  = "__NOISE__"

MATCH_MAX_DAYS_EXACT    = 7     # 후기 출사일이 파싱된 경우 매칭 허용 거리
MATCH_MAX_DAYS_FALLBACK = 45    # 작성일 근접 fallback 허용 거리

# 매칭 점수의 저울. **날짜만으로는 갈리지 않는다** — 같은 날 두 출사가 열리고,
# 제목의 날짜가 틀리기도 한다. 그래서 증거를 힘 센 순으로 쌓는다:
#
#   제목 > 작성자 > 카테고리
#
# 제목이 가장 세다. 장소·컨셉 이름("노들섬", "코타츠")은 우연히 겹치지 않는다.
# 작성자가 다음이다 — 대개 출사를 연 사람이 후기도 쓴다(실제 데이터에서 후기
# 254건 중 작성자가 다른 것은 1건뿐이었다). 카테고리가 가장 약하다 — 같은
# 출사인데 공지는 `[인물]`, 후기는 `[인풍]`로 적히는 일이 흔하다.
#
# 예전에는 카테고리가 100으로 나머지를 전부 눌렀다. 그래서 **같은 날·같은
# 사람이 쓴 짝을 놔두고 나흘 떨어진 남의 출사로** 붙는 일이 생겼다.
TITLE_MATCH_BONUS       = 12    # 제목이 닮은 정도(0~1)에 곱한다
AUTHOR_MATCH_BONUS      = 8     # 공지를 연 사람이 후기도 썼다
CAT_MATCH_BONUS         = 3     # 카테고리 표기가 같다


def parse_member_csv(text: str) -> tuple[set[str], dict[str, str], dict[str, str]]:
    """멤버 명단 CSV/TXT 파싱.

    형식(헤더 줄 선택): 각 줄 `실명,닉네임[,별칭;별칭...]`. 한 컬럼만 있으면 실명으로 간주.

    Returns:
        (member_names, nick_to_real, real_to_nick)
        - member_names: 실명+닉네임+별칭 집합 (본문 추출 매칭용)
        - nick_to_real: 닉네임/별칭 → 실명
        - real_to_nick: 실명 → 닉네임 (표시용)
    """
    member_names: set[str] = set()
    nick_to_real: dict[str, str] = {}
    real_to_nick: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [c.strip() for c in line.split(",")]
        real = parts[0] if parts else ""
        if not real or real.lower() in ("실명", "이름", "name", "성명"):  # 헤더 스킵
            continue
        member_names.add(real)
        nick = parts[1] if len(parts) > 1 else ""
        if nick:
            nick_to_real[nick] = real
            real_to_nick[real] = nick
            member_names.add(nick)
        if len(parts) > 2 and parts[2]:
            for alias in parts[2].split(";"):
                alias = alias.strip()
                if alias:
                    member_names.add(alias)
                    nick_to_real.setdefault(alias, real)
    member_names.difference_update(NAME_BLACKLIST)
    return member_names, nick_to_real, real_to_nick


def build_member_master(
    posts: list[dict],
    photos: list[dict],
    min_freq: int = 3,
    extra_names: Optional[set[str]] = None,
) -> set[str]:
    """멤버 마스터(실명 추정 집합) 구축 — 후기 본문 추출 매칭용.

    소스:
    - 후기 본문에서 min_freq회 이상 등장하는 한글 토큰 (실명 후보)
    - extra_names (CSV의 실명·닉네임·별칭)

    NOTE: 게시글 작성자·사진 업로더는 '닉네임'이라 실명 본문엔 거의 안 나오므로
    추출 매칭 집합에는 넣지 않는다(식별자 매핑은 parse_member_csv가 담당). photos는
    시그니처 호환·향후 확장을 위해 받되 현재 빈도 집계엔 쓰지 않는다.
    """
    name_freq: Counter = Counter()
    for p in posts:
        if p.get("cat") != "E":
            continue
        body, title = p.get("body", ""), p.get("title", "")
        cleaned = body.replace(title, " ") if title else body
        for n in NAME_RX.findall(cleaned):
            if n not in NAME_BLACKLIST:
                name_freq[n] += 1
    master = {n for n, c in name_freq.items() if c >= min_freq}
    if extra_names:
        master.update(extra_names)
    master.difference_update(NAME_BLACKLIST)
    return master


def build_member_candidates(
    posts: list[dict],
    photos: list[dict],
    min_freq: int = 3,
) -> list[dict]:
    """마스터 editor 사전 채우기용 후보 행 (실명/닉네임 분리).

    각 행: {"실명": str, "닉네임": str, "별칭": str, "포함": bool}.
    소스:
    - 후기 본문 빈도 ≥ min_freq 토큰 → '실명' 후보 (닉네임 공란)
    - 게시글 작성자·사진 업로더 → '닉네임' 후보 (실명 공란)
    동일 토큰이 여러 소스에 잡히면 같은 행으로 병합, 빈 칸은 채움. 블랙리스트 토큰은 '포함'=False로 초기화(노출은 함).
    정렬: 포함 우선 → 빈도 desc → 토큰.
    """
    name_freq: Counter = Counter()
    for p in posts:
        if p.get("cat") != "E":
            continue
        body, title = p.get("body", ""), p.get("title", "")
        cleaned = body.replace(title, " ") if title else body
        for n in NAME_RX.findall(cleaned):
            if n not in NAME_BLACKLIST:
                name_freq[n] += 1
    body_real = {n: c for n, c in name_freq.items() if c >= min_freq}

    author_freq: Counter = Counter(p["author"] for p in posts if p.get("author"))
    uploader_freq: Counter = Counter(p["author"] for p in photos if p.get("author"))

    rows: dict[str, dict] = {}
    freq_map: dict[str, int] = {}

    def add(token: str, *, real: str = "", nick: str = "", freq: int = 0) -> None:
        if not token:
            return
        r = rows.setdefault(token, {"실명": "", "닉네임": "", "별칭": "", "포함": True})
        if real and not r["실명"]:
            r["실명"] = real
        if nick and not r["닉네임"]:
            r["닉네임"] = nick
        freq_map[token] = max(freq_map.get(token, 0), freq)

    for n, c in body_real.items():
        add(n, real=n, freq=c)
    for n, c in author_freq.items():
        add(n, nick=n, freq=c)
    for n, c in uploader_freq.items():
        add(n, nick=n, freq=c)

    for token in rows:
        if token in NAME_BLACKLIST:
            rows[token]["포함"] = False

    return sorted(
        rows.values(),
        key=lambda x: (not x["포함"],
                       -freq_map.get(x["실명"] or x["닉네임"], 0),
                       x["실명"] or x["닉네임"]),
    )


def extract_attendees(body: str, title: str, member_names: set[str]) -> list[str]:
    """후기 본문에서 마스터 매칭된 이름 추출 (등장 순서 유지, 중복 제거)."""
    if not body:
        return []
    cleaned = body.replace(title, " ") if title else body
    out = [n for n in NAME_RX.findall(cleaned)
           if n in member_names and n not in NAME_BLACKLIST]
    return list(dict.fromkeys(out))


def parse_review_outing_date(
    title: str, content: str, posted_dt: datetime
) -> Optional[date]:
    """후기 제목/내용에서 '본 출사'의 날짜 추출.

    후기는 출사 이후에 작성되므로 infer_outing_date(미래 지향)와 반대로,
    MM.DD는 **작성일 이전의 가장 가까운 날짜**로 읽는다. 예전에는 90일까지만
    읽고 그보다 오래된 것은 버렸는데, 버리면 작성일로 떨어진다 — 그리고
    **몇 달 늦게 쓴 후기에서 작성일은 제목보다 훨씬 나쁜 단서다.** 2월 출사
    후기를 7월에 올리면 7월에 열린 남의 출사에 가서 붙는다. 제목이 틀렸다면
    어차피 매칭 창(±7일)이 걸러 낸다.
    명시 연도(출사진행날짜 / 제목 YYYY.MM.DD)는 그대로 신뢰. 기존 날짜 정규식 재사용.
    """
    posted_date = posted_dt.date()

    m = re.search(r"출사진행날짜\s*[:\-]\s*" + DATE_PATTERN_WITH_YEAR, content or "")
    if m:
        try:
            return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    t = CANCEL_RX.sub("", title or "")
    t = re.sub(r"[<>《》]", " ", t)

    for pat in (DATE_PATTERN_WITH_YEAR, DATE_PATTERN_COMPACT):
        m = re.search(pat, t)
        if m:
            try:
                return date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass

    md = None
    for pat in DATE_PATTERNS_NO_YEAR:
        m = re.search(pat, t)
        if m:
            try:
                mo, day = int(m.group(1)), int(m.group(2))
                if 1 <= mo <= 12 and 1 <= day <= 31:
                    md = (mo, day)
                    break
            except ValueError:
                continue
    if not md:
        return None

    mo, day = md
    for off in (0, -1):
        try:
            cand = date(posted_date.year + off, mo, day)
        except ValueError:
            continue
        if cand <= posted_date:
            return cand
    return None


def annotate_review_attendees(
    posts: list[dict],
    member_names: set[str],
    nick_to_real: Optional[dict[str, str]] = None,
) -> tuple[list[dict], Counter]:
    """cat=E 후기에 참석자 정보를 부착(in-place).

    부착 키: attendees_raw(원본 매칭 토큰), attendees(실명 정규화·중복 제거),
    attendees_needs_review/attendees_review_reason(자가검증), review_outing_date,
    matched_outing_id(초기 None — match 단계에서 채움).

    자가검증: 작성자(닉네임)를 nick_to_real로 실명 변환 후 명단 포함 여부 확인.
    매핑이 없으면 '명단 비었음'만으로 판정.

    Returns: (posts, unknown_freq) — 마스터에 없는 본문 토큰의 빈도(명단 보강 참고용).
    """
    nick_to_real = nick_to_real or {}
    unknown_freq: Counter = Counter()

    for p in posts:
        if p.get("cat") != "E":
            continue
        title = p.get("title", "")
        body = p.get("body", "")
        author = p.get("author", "")

        raw = extract_attendees(body, title, member_names)
        canon = list(dict.fromkeys(nick_to_real.get(n, n) for n in raw))

        needs, reason = False, ""
        if not canon:
            needs, reason = True, "본문에서 이름을 찾지 못함"
        elif nick_to_real:
            author_real = nick_to_real.get(author, author)
            if author_real not in canon:
                needs, reason = True, f"작성자({author})가 명단에 없음"

        p["attendees_raw"] = raw
        p["attendees"] = canon
        p["attendees_needs_review"] = needs
        p["attendees_review_reason"] = reason
        d = parse_review_outing_date(title, body, p["posted_at"])
        p["review_outing_date"] = d.isoformat() if d else None
        p["matched_outing_id"] = None

        cleaned = body.replace(title, " ") if title else body
        for n in NAME_RX.findall(cleaned):
            if n not in member_names and n not in NAME_BLACKLIST:
                unknown_freq[n] += 1

    return posts, unknown_freq


# 소모임의 "정모" 기능이 자동으로 만드는 게시글. 후기로 분류되지만 후기가 아니고
# (본문이 `📌 정모 정보 / 📅 날짜 / 📍 장소` 틀 그대로다) 참석자도 없다. 그런데도
# 매칭에 끼면 **엉뚱한 공지를 선점해 진짜 후기가 못 붙는다.**
MEETUP_POST_RX = re.compile(r"📌\s*정모 정보|정모에 대한 이야기를 나눠보세요")

# 작성일로 날짜를 재는 약한 매칭에서 요구하는 제목 닮음의 바닥.
# 0.22("반차 쓰고 벚꽃" ↔ "올공 밤벚꽃" — `벚꽃` 하나만 겹친다)는 통과하면 안 된다.
TITLE_MIN_AFFINITY = 0.4

_TITLE_NOISE_RX = re.compile(
    r"[\[\(<《][^\]\)>》]*[\]\)>》]"          # [인풍] (펑) <03.04> 같은 딱지
    r"|\d+[./\-]?\d*"                        # 날짜·숫자
    r"|후기|출사|정출|모임|사진|촬영|컨셉|다녀|가기|하기|번개"
)


def title_affinity(a: str, b: str) -> float:
    """두 제목이 닮은 정도 0~1. 글자 2연 겹침(Dice)으로 잰다.

    낱말 단위로 자르면 띄어쓰기 하나에 무너진다 — 실제로 공지는
    `비오는 노들섬`, 후기는 `비오는노들섬 후기`였다. 글자 단위로 재면
    붙여 쓰든 띄어 쓰든 같은 곳을 가리키는 것이 보인다.

    딱지·날짜와 아무 출사에나 붙는 말(후기·출사·정출…)은 먼저 지운다.
    안 지우면 `[인풍] … 출사 후기`끼리 전부 닮아 보인다.
    """
    def grams(s: str) -> set[str]:
        s = re.sub(r"\s+", "", _TITLE_NOISE_RX.sub(" ", s or ""))
        return {s[i:i + 2] for i in range(len(s) - 1)}

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def match_outings_with_reviews(posts: list[dict]) -> list[dict]:
    """출사 공지(cat=A)와 후기(cat=E)를 짝지어 준다(in-place).

    공지: matched_review_id, attendees(매칭 후기의 참석자), actually_held.
    후기: matched_outing_id.

    점수(작을수록 우선) = 날짜거리 − 제목닮음×12 − 작성자일치×8 − 카테고리일치×3.
    후기 출사일이 파싱되면 outing_date와 근접(±EXACT), 아니면 작성일 근접(±FALLBACK).

    **짝은 전체에서 점수가 좋은 순으로 확정한다.** 예전에는 후기를 하나씩 돌며
    그때그때 제일 나은 공지를 집어갔는데, 같은 날 열린 두 출사의 후기 둘이
    서로 상대의 공지를 가져가 **맞바꿔 붙는** 일이 생겼다. 전체를 놓고 가장
    확실한 짝부터 확정하면 그런 뒤바뀜이 안 생긴다.

    **펑 난 출사는 후보에서 뺀다.** 안 간 출사에는 후기가 없다. 그런데도 붙으면
    `actually_held`가 서고 참석자까지 그 출사에 얹혀, 열리지도 않은 날짜에
    사람들이 참석한 것으로 집계된다.
    """
    notices = [p for p in posts
               if p.get("cat") == "A" and p.get("outing_date")
               and not p.get("is_canceled")]
    reviews = []
    for p in posts:
        if p.get("cat") != "E":
            continue
        p["matched_outing_id"] = None
        p["is_meetup_post"] = bool(MEETUP_POST_RX.search(p.get("body") or ""))
        if not p["is_meetup_post"]:
            reviews.append(p)
    # 펑 난 공지까지 함께 비운다 — 후보에서 빠졌다고 예전 값이 남아 있으면
    # 열리지도 않은 출사가 참석자를 달고 집계에 들어간다.
    for n in posts:
        if n.get("cat") == "A":
            n["matched_review_id"] = None
            n["attendees"] = []
            n["actually_held"] = False

    def pairs(rs: list[dict], *, use_posted: bool):
        """(점수, 후기, 공지) 후보 전부.

        **출사일을 못 읽어 작성일로 재는 짝은 제목이나 작성자가 걸려야 낸다.**
        작성일은 출사일이 아니다 — 45일 창을 열어 두고 거리만 보면 아무 후기나
        아무 공지에 가서 붙고, 실제로 그렇게 붙었다.
        """
        out = []
        for r in rs:
            rod = None if use_posted else r.get("review_outing_date")
            r_date = date.fromisoformat(rod) if rod else r["posted_at"].date()
            limit = MATCH_MAX_DAYS_EXACT if rod else MATCH_MAX_DAYS_FALLBACK
            for n in notices:
                dist = abs((r_date - date.fromisoformat(n["outing_date"])).days)
                if dist > limit:
                    continue
                aff = title_affinity(r.get("title", ""), n.get("title", ""))
                same_author = bool(n.get("author")) and n["author"] == r.get("author")
                if rod is None and aff < TITLE_MIN_AFFINITY and not same_author:
                    continue
                score = dist - aff * TITLE_MATCH_BONUS
                if same_author:
                    score -= AUTHOR_MATCH_BONUS
                if n.get("category") and r.get("category") \
                        and n["category"] == r["category"]:
                    score -= CAT_MATCH_BONUS
                out.append((score, r, n))
        return sorted(out, key=lambda t: (t[0], t[1]["id"], t[2]["id"]))

    def assign(cands) -> None:
        for _, r, n in cands:
            if r["matched_outing_id"] or n["matched_review_id"]:
                continue
            n["matched_review_id"] = r["id"]
            n["attendees"] = list(r.get("attendees", []))
            n["actually_held"] = True
            r["matched_outing_id"] = n["id"]

    assign(pairs(reviews, use_posted=False))
    # 제목의 날짜가 틀려 1차에서 못 붙은 후기는 작성일로 한 번 더 본다.
    left = [r for r in reviews if not r["matched_outing_id"]]
    if left:
        assign(pairs(left, use_posted=True))
    return posts


# ═══════════════════════════════════════════════════════════════
# 멤버 API + 이름 해소 (v2)
# ═══════════════════════════════════════════════════════════════
#
# /api/group에서 활성 멤버 목록을 가져와 마스터(=`mn` 집합)로 사용.
# 후기에서 추출했지만 마스터에 정확히 일치하지 않는 이름은
# 사용자가 드롭다운으로 LEFT_MEMBER/NOT_A_NAME/마스터닉네임 중 하나로
# 해소(resolution dict). 자동 추론 없음.

_OS_LABEL = {"i1": "iOS", "a1": "Android"}


def collect_members(
    progress: ProgressFn = None,
    active_only: bool = True,
) -> tuple[list[dict], set[str]]:
    """/api/group에서 멤버 목록 수집.

    Args:
        active_only: True면 ban=N(활성)만 반환.

    Returns:
        (members, master_names)
            members: dict 리스트 — keys: mid, mn, is_admin, joined_at,
                     last_visit, os, push
            master_names: 활성 멤버 `mn` 집합 (본문 추출 매칭용)
    """
    _emit(progress, "멤버 목록 수집 중…", 0.0)
    r = requests.post(BASE_URL + "/api/group",
                      headers=HEADERS, json={"gid": GROUP_ID}, timeout=10)
    r.raise_for_status()
    raw = r.json().get("m", []) or []

    members: list[dict] = []
    for m in raw:
        if active_only and m.get("ban") != "N":
            continue
        members.append({
            "mid":        m.get("mid", ""),
            "mn":         m.get("mn", ""),
            "is_admin":   m.get("i_m") == "Y",
            "joined_at":  _ts_to_dt(m["j_t"]) if m.get("j_t") else None,
            "last_visit": _ts_to_dt(m["v_t"]) if m.get("v_t") else None,
            "os":         _OS_LABEL.get(m.get("os", ""), m.get("os", "") or ""),
            "push":       m.get("push") == "Y",
        })
    master = {m["mn"] for m in members if m["mn"]}
    _emit(progress, f"활성 멤버 {len(members)}명", 1.0)
    return members, master


def collect_banned_names() -> set[str]:
    """**강퇴된**(ban=Y) 멤버 닉네임 집합.

    자발적으로 나간 사람은 멤버 목록에 아예 없으므로 여기 잡히지 않는다 —
    `ban=Y`는 탈퇴가 아니라 강퇴다. 이름 해소의 참고 정보로 쌓아 둔다
    (자동 선택은 하지 않는다 — 사람이 `후기이름매핑`에서 지정한다).
    """
    r = requests.post(BASE_URL + "/api/group",
                      headers=HEADERS, json={"gid": GROUP_ID}, timeout=10)
    r.raise_for_status()
    return {m["mn"] for m in r.json().get("m", []) or []
            if m.get("ban") == "Y" and m.get("mn")}


def find_duplicate_member_names(members: list[dict]) -> set[str]:
    """활성 멤버 중 동일 `mn`이 2명 이상인 닉네임 집합.

    mid로는 구별 가능하지만 후기 본문에서 추출되는 이름은 닉네임 문자열뿐이라
    이 집합에 든 닉네임은 보고서에서 둘 이상의 사람이 합쳐 표시됨 — UI에서 마킹.
    """
    c: Counter = Counter(m["mn"] for m in members if m.get("mn"))
    return {mn for mn, cnt in c.items() if cnt >= 2}


# ═══════════════════════════════════════════════════════════════
# 가입인사 자동 매핑 (실명 → 닉네임)
# ═══════════════════════════════════════════════════════════════
#
# 가입인사 본문에 `이름 : XXX` 형식으로 실명이 적혀 있어 (닉네임=작성자명, 실명=본문)
# 쌍을 자동 추출 → resolve_names 자동 기본값으로 사용.

JOIN_NAME_RX = re.compile(r"(?:이름|성함|본명)\s*[:：\-]\s*([가-힣]{2,4})(?![가-힣])")


def collect_join_greetings(
    progress: ProgressFn = None,
    active_members: Optional[list[dict]] = None,
    min_joined_at: Optional[datetime] = None,
) -> list[dict]:
    """cat=J(가입인사) 글 수집.

    서버에 `{"cat": "J"}`를 함께 보내 가입인사만 받아오므로 게시글 전체를 긁고
    클라이언트에서 거르는 것보다 훨씬 빠르다.

    Args:
        active_members: 활성 멤버 dict 리스트(`mid`, `mn` 포함). 지정하면 게시글의
            `wid`(작성자 user id)가 활성 멤버 `mid`에 속한 글만 keep한다. 닉네임 일치가
            아니라 user id로 거르므로 동명이인이나 닉네임 변경(과거→현재) 케이스에서
            엉뚱한 글을 끌어오지 않는다. 활성 멤버 전원의 가입인사를 확보하면 조기 종료.
        min_joined_at: 활성 멤버의 가장 이른 가입 시각. 지정하면 페이지의 가장
            오래된 글이 이 시각보다 이전일 때 종료(안전망 — 가입인사 안 쓴 멤버 있을 때).
    """
    _emit(progress, "가입인사 수집 시작…", 0.0)

    mid_to_mn: dict[str, str] = {
        m["mid"]: m.get("mn", "")
        for m in (active_members or [])
        if m.get("mid")
    }
    active_mids: set[str] = set(mid_to_mn)
    seen_mids: set[str] = set()
    cutoff = min_joined_at - timedelta(days=7) if min_joined_at else None

    def stop(all_items: list[dict], oldest_dt: datetime) -> bool:
        if active_mids:
            for it in all_items:
                wid = it.get("wid", "")
                if wid in active_mids:
                    seen_mids.add(wid)
            if seen_mids >= active_mids:
                return True
        if cutoff is not None and oldest_dt < cutoff:
            return True
        return False

    raw = _fetch_paginated(
        # 가입인사는 분석 기간과 무관하게 전 기간을 훑는다(활성 멤버 전원 확보 시
        # should_stop이 끊음) → 기간 기반 종료를 끈다.
        "/api/articles", "cs", None, progress, "가입인사",
        extra_payload={"cat": "J"},
        should_stop=stop,
    )

    out: list[dict] = []
    for p in raw:
        if p.get("cat") != "J":
            continue
        wid = p.get("wid", "")
        if active_mids and wid not in active_mids:
            continue
        # author는 현재 닉네임을 우선(닉네임 변경 케이스 보정), 없으면 작성 시점 wn
        author = mid_to_mn.get(wid) or p.get("wn", "")
        out.append({
            "id":             p["id"],
            "wid":            wid,
            "author":         author,
            "author_at_post": p.get("wn", ""),
            "title":          p.get("at", ""),
            "body":           p.get("c", ""),
            "posted_at":      _post_dt(p),
        })
    _emit(progress, f"가입인사 {len(out)}개 추출", 1.0)
    return out


def parse_join_name_aliases(
    join_posts: list[dict],
    active_mns: Optional[set[str]] = None,
) -> dict[str, str]:
    """가입인사 본문에서 `이름 : XXX` 패턴으로 (실명 → 닉네임) 매핑 추출.

    동일 실명이 여러 글에 등장하면 가장 최근 글의 작성자(닉네임)로 덮어쓴다.
    실명==닉네임이면 의미 없으니 제외.

    Args:
        join_posts: collect_join_greetings 결과.
        active_mns: 활성 멤버 닉네임 집합. 지정하면 닉네임이 이 집합에 있을 때만
            매핑에 포함(닉네임 변경/탈퇴로 현재 멤버에 없는 author는 제외).
    """
    out: dict[str, str] = {}
    for p in sorted(join_posts, key=lambda x: x.get("posted_at") or datetime.min):
        body = p.get("body") or ""
        author = p.get("author") or ""
        if not author:
            continue
        if active_mns is not None and author not in active_mns:
            continue
        for real in JOIN_NAME_RX.findall(body):
            if real and real != author:
                out[real] = author
    return out


def extract_raw_names(body: str, title: str) -> list[str]:
    """후기 본문에서 이름 후보를 추출(마스터 필터 X, 블랙리스트만 제외).

    `extract_attendees`와 달리 마스터 매칭 전 단계의 모든 토큰을 반환해
    `resolve_names`가 다음 단계에서 마스터/해소맵/미해소로 분류한다.
    """
    if not body:
        return []
    cleaned = body.replace(title, " ") if title else body
    out = [n for n in NAME_RX.findall(cleaned) if n not in NAME_BLACKLIST]
    return list(dict.fromkeys(out))


def resolve_names(
    raw_names: list[str],
    master: set[str],
    resolution: dict[str, str],
) -> tuple[list[str], list[str]]:
    """추출된 이름을 마스터/해소맵으로 정규화.

    Returns:
        (confirmed, unresolved)
            confirmed: 최종 참석자(마스터 닉네임으로 정규화, 순서 유지·중복 제거)
            unresolved: 마스터에도, resolution에도 없는 미해소 이름
    """
    confirmed: list[str] = []
    unresolved: list[str] = []
    for name in raw_names:
        if name in master:
            confirmed.append(name)
        elif name in resolution:
            target = resolution[name]
            if target in (LEFT_MEMBER, NOT_A_NAME):
                continue
            confirmed.append(target)
        else:
            unresolved.append(name)
    return list(dict.fromkeys(confirmed)), list(dict.fromkeys(unresolved))


def annotate_attendees(
    posts: list[dict],
    master: set[str],
    resolution: Optional[dict[str, str]] = None,
) -> list[dict]:
    """cat=E 후기에 참석자 정보를 부착(in-place).

    부착 키:
      - attendees_raw:    extract_raw_names 결과(원본 토큰)
      - attendees:        resolve_names의 confirmed(마스터 닉네임 정규화)
      - unresolved_names: 마스터·resolution 어디에도 없는 미해소 이름
      - review_outing_date: 후기 제목/본문에서 추정한 출사일(있을 때)
      - matched_outing_id: None (match_outings_with_reviews에서 채움)
    """
    resolution = resolution or {}
    for p in posts:
        if p.get("cat") != "E":
            continue
        title = p.get("title", "")
        body = p.get("body", "")
        raw = extract_raw_names(body, title)
        confirmed, unresolved = resolve_names(raw, master, resolution)
        p["attendees_raw"] = raw
        p["attendees"] = confirmed
        p["unresolved_names"] = unresolved
        d = parse_review_outing_date(title, body, p["posted_at"])
        p["review_outing_date"] = d.isoformat() if d else None
        p["matched_outing_id"] = None
    return posts


def collect_all_unresolved(posts: list[dict]) -> Counter:
    """모든 후기의 unresolved_names 빈도 집계 (드롭다운 정렬용)."""
    c: Counter = Counter()
    for p in posts:
        if p.get("cat") == "E":
            for n in p.get("unresolved_names", []):
                c[n] += 1
    return c


# ═══════════════════════════════════════════════════════════════
# CLI 테스트
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    # 사용: python -m core.collector [START_YM] [END_YM]   (예: 202509 202603)
    start_ym = int(sys.argv[1]) if len(sys.argv) > 1 else 202601
    end_ym   = int(sys.argv[2]) if len(sys.argv) > 2 else start_ym // 100 * 100 + 12

    def log(msg, pct):
        print(f"  [{pct*100:>5.1f}%] {msg}")

    print(f"\n=== {GROUP_NAME} {period_label(start_ym, end_ym)} ===\n")
    posts  = collect_posts(start_ym, end_ym, progress=log)
    photos = collect_photos(start_ym, end_ym, progress=log)

    print(f"\n[요약]")
    print(f"  게시글: {len(posts)}개")
    print(f"  사진:   {len(photos)}개")
    print(f"  테마 예상: {sum(1 for p in photos if p['has_comment'])}개")
