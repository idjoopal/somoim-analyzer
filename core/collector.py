"""
다감노📸 데이터 수집 모듈

외부 의존:
- requests
- (선택) tqdm

주요 함수:
- collect_posts(year, month=None, progress=None) -> list[dict]
- collect_photos(year, month=None, progress=None) -> list[dict]
"""

from __future__ import annotations

import re
import time
import requests
from datetime import datetime, date
from typing import Callable, Optional


# ═══════════════════════════════════════════════════════════════
# 설정
# ═══════════════════════════════════════════════════════════════

GROUP_ID   = "2d4b415a-d2f4-11eb-97b4-0a0d8e52bd411"
GROUP_NAME = "다감노📸"

BASE_URL = "https://www.somoim.co.kr"
CDN_BASE = "https://d3vo2hyhx9t76k.cloudfront.net"

EPOCH_OFFSET = 1_000_000_000  # unix_ts = (w_t or ot) + EPOCH_OFFSET

HEADERS = {
    "User-Agent":   "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1",
    "Content-Type": "application/json",
    "Referer":      f"{BASE_URL}/{GROUP_ID}1",
}

# 카테고리 분류
CAT_LABEL = {"A": "공지", "E": "후기", "J": "가입인사"}
OUTING_CATS     = ["인물", "인풍", "풍경", "1:1인물", "1:1인물출사"]
NON_OUTING_CATS = ["보정", "GN", "문화"]
ALL_CATS        = OUTING_CATS + NON_OUTING_CATS

CAT_RX    = re.compile(r"\[(" + "|".join(re.escape(c) for c in ALL_CATS) + r")\]")
CANCEL_RX = re.compile(r"[\(\[]\s*펑\s*[\)\]]")

DATE_PATTERN_WITH_YEAR = r"20(\d{2})[./\-](\d{1,2})[./\-](\d{1,2})"
DATE_PATTERNS_NO_YEAR  = [
    r"(\d{1,2})\.(\d{2})\s*[~\-]\s*\d{1,2}\.\d{2}",   # 범위
    r"(\d{1,2})\.(\d{2})",
    r"(\d{1,2})/(\d{2})",
    r"(\d{1,2})월\s*(\d{1,2})일",
]


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
    """제목에서 카테고리·취소여부 추출"""
    tags = CAT_RX.findall(title)
    category = tags[0] if tags else None
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
    target_year: int,
    progress: ProgressFn = None,
    progress_label: str = "수집",
) -> list[dict]:
    """공통 페이지네이션 수집기. 대상 연도-1까지 도달하면 중단."""
    all_items: list[dict] = []
    s_t = None
    stop_year = target_year - 1

    for page in range(1, 200):
        payload: dict = {"gid": GROUP_ID, "wql": 20}
        if s_t is not None:
            payload["s_t"] = s_t

        try:
            r = requests.post(BASE_URL + endpoint, headers=HEADERS, json=payload, timeout=10)
            r.raise_for_status()
        except Exception as e:
            _emit(progress, f"[ERROR] {endpoint} page {page}: {e}", 0.5)
            break

        data  = r.json()
        items = data.get(list_key, [])
        if not items:
            break

        all_items.extend(items)
        _emit(progress, f"{progress_label} {page}페이지, 누적 {len(all_items)}개", min(0.4, page / 50))

        # 대상 연도 이전이면 중단
        oldest = items[-1]
        oldest_ts = oldest["ot"] if oldest.get("w_t") == 2000000000 else oldest["w_t"]
        if _ts_to_dt(oldest_ts).year < stop_year:
            break

        if data.get("eof") == "Y" or len(items) < 20:
            break

        s_t = items[-1].get("ot") or items[-1].get("w_t")
        time.sleep(0.15)

    return all_items


# ═══════════════════════════════════════════════════════════════
# 게시글 수집
# ═══════════════════════════════════════════════════════════════

def collect_posts(
    year: int,
    month: Optional[int] = None,
    progress: ProgressFn = None,
) -> list[dict]:
    """
    게시글 수집 + 필터링.

    필터링 규칙:
    - cat=A (공지): 출사일 기준 (작성일 무관, 출사가 target 연/월에 있으면 포함)
    - cat=E (후기), cat=J (가입인사): 작성일 기준

    Args:
        year: 대상 연도 (예: 2026)
        month: 대상 월 (None이면 연 전체)
        progress: 진행 콜백 fn(msg: str, pct: float)

    Returns:
        list of dict with keys:
            id, author, wid, title, outing_date(str|None),
            posted_at(datetime), cat, cat_label, category,
            is_outing, is_canceled, likes, comments, images
    """
    _emit(progress, "게시글 수집 시작…", 0.0)
    raw = _fetch_paginated("/api/articles", "cs", year, progress, "게시글")
    _emit(progress, f"게시글 원본 {len(raw)}개 수집 완료", 0.4)

    posts: list[dict] = []
    for p in raw:
        dt   = _post_dt(p)
        cat  = p.get("cat", "")
        meta = _parse_title_meta(p["at"])

        if cat == "A":
            od = infer_outing_date(p["at"], p.get("c", ""), dt)
            if not od:
                continue
            if od.year != year:
                continue
            if month is not None and od.month != month:
                continue
            outing_date = od.isoformat()
        else:
            if dt.year != year:
                continue
            if month is not None and dt.month != month:
                continue
            outing_date = None

        posts.append({
            "id":          p["id"],
            "author":      p.get("wn", ""),
            "wid":         p.get("wid", ""),
            "title":       p["at"],
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
        })

    _emit(progress, f"게시글 필터 후 {len(posts)}개", 0.5)
    return posts


# ═══════════════════════════════════════════════════════════════
# 사진 수집
# ═══════════════════════════════════════════════════════════════

def collect_photos(
    year: int,
    month: Optional[int] = None,
    progress: ProgressFn = None,
) -> list[dict]:
    """
    사진 수집 + 필터링.

    작성일 기준 필터링.
    has_comment=True인 사진은 "테마 참여 예상"으로 표시.

    Args:
        year: 대상 연도
        month: 대상 월 (None이면 연 전체)
        progress: 진행 콜백

    Returns:
        list of dict with keys:
            id, author, wid, posted_at(datetime), likes, comments,
            has_comment, url_large, url_medium, url_small, url_thumb
    """
    _emit(progress, "사진 수집 시작…", 0.5)
    raw = _fetch_paginated("/api/photos", "ps", year, progress, "사진")
    _emit(progress, f"사진 원본 {len(raw)}개 수집 완료", 0.9)

    photos: list[dict] = []
    for p in raw:
        dt = _ts_to_dt(p["w_t"])
        if dt.year != year:
            continue
        if month is not None and dt.month != month:
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
# CLI 테스트
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    year  = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    month = int(sys.argv[2]) if len(sys.argv) > 2 else None

    def log(msg, pct):
        print(f"  [{pct*100:>5.1f}%] {msg}")

    print(f"\n=== {GROUP_NAME} {year}{'년 전체' if month is None else f'년 {month}월'} ===\n")
    posts  = collect_posts(year, month, progress=log)
    photos = collect_photos(year, month, progress=log)

    print(f"\n[요약]")
    print(f"  게시글: {len(posts)}개")
    print(f"  사진:   {len(photos)}개")
    print(f"  테마 예상: {sum(1 for p in photos if p['has_comment'])}개")
