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
from collections import Counter
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

# 제목에서 탐지할 원본 태그 (regex용) — 겹치는 1:1 쌍은 긴 것을 먼저
RAW_CATS = ["1:1인물출사", "1:1인물", "인물", "인풍", "풍경", "GN", "보정", "문화"]
# 원본 태그 → 집계·표시용 정규화 카테고리
CAT_NORMALIZE = {"1:1인물": "인물", "1:1인물출사": "인물", "인풍": "인물&풍경"}

OUTING_CATS     = ["인물", "인물&풍경", "풍경", "GN"]
NON_OUTING_CATS = ["보정", "문화"]
ALL_CATS        = OUTING_CATS + NON_OUTING_CATS

CAT_RX    = re.compile(r"\[(" + "|".join(re.escape(c) for c in RAW_CATS) + r")\]")
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
    keep_unclassified: bool = False,
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
        keep_unclassified: True면 출사일 추론 실패한 cat=A 공지를 버리지 않고
            outing_date=None으로 포함(연/월 게이트는 작성일 기준)하고 검토 대상으로 표시.
            기본 False는 기존 동작(추론 실패 공지 제외)을 그대로 유지.

    Returns:
        list of dict with keys:
            id, author, wid, title, body(str), outing_date(str|None),
            posted_at(datetime), cat, cat_label, category,
            is_outing, is_canceled, likes, comments, images,
            needs_review(bool), review_reason(str)
    """
    _emit(progress, "게시글 수집 시작…", 0.0)
    raw = _fetch_paginated("/api/articles", "cs", year, progress, "게시글")
    _emit(progress, f"게시글 원본 {len(raw)}개 수집 완료", 0.4)

    posts: list[dict] = []
    for p in raw:
        dt   = _post_dt(p)
        cat  = p.get("cat", "")
        meta = _parse_title_meta(p["at"])
        review_reasons: list[str] = []

        if cat == "A":
            od = infer_outing_date(p["at"], p.get("c", ""), dt)
            if od is None:
                if not keep_unclassified:
                    continue
                if dt.year != year:
                    continue
                if month is not None and dt.month != month:
                    continue
                outing_date = None
                review_reasons.append("출사일 미상")
            else:
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
    # 정규화 카테고리어 (제목/본문에 태그가 그대로 들어온 경우) — GN은 영문이라 NAME_RX 미해당
    "인물", "인물&풍경", "풍경", "보정", "문화",
}

NAME_RX = re.compile(r"[가-힣]{2,4}")

REVIEW_LOOKBACK_DAYS    = 90    # 후기 제목의 MM.DD를 과거로 해석할 최대 범위
MATCH_MAX_DAYS_EXACT    = 7     # 후기 출사일이 파싱된 경우 매칭 허용 거리
MATCH_MAX_DAYS_FALLBACK = 45    # 작성일 근접 fallback 허용 거리
CAT_MATCH_BONUS         = 100   # 카테고리 일치 시 점수 감점(우선)
AUTHOR_MATCH_BONUS      = 5     # 작성자 일치 시 점수 감점


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
    MM.DD를 작성일 이전의 가장 최근(≤ posted, ≤ REVIEW_LOOKBACK_DAYS) 날짜로 해석한다.
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

    m = re.search(DATE_PATTERN_WITH_YEAR, t)
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
        if cand <= posted_date and (posted_date - cand).days <= REVIEW_LOOKBACK_DAYS:
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


def match_outings_with_reviews(posts: list[dict]) -> list[dict]:
    """출사 공지(cat=A)와 후기(cat=E)를 출사일·카테고리로 매칭(in-place).

    공지: matched_review_id, attendees(매칭 후기의 참석자), actually_held.
    후기: matched_outing_id.
    매칭 점수(작을수록 우선) = 날짜거리 − 카테고리일치보너스 − 작성자일치보너스.
    후기 출사일이 파싱되면 outing_date와 근접(±EXACT) 매칭, 아니면 작성일 근접(±FALLBACK).
    """
    notices = [p for p in posts if p.get("cat") == "A" and p.get("outing_date")]
    reviews = [p for p in posts if p.get("cat") == "E"]

    for n in notices:
        n["matched_review_id"] = None
        n["attendees"] = []
        n["actually_held"] = False
    for r in reviews:
        r.setdefault("matched_outing_id", None)

    def best_match(r: dict):
        rod = r.get("review_outing_date")
        r_date = date.fromisoformat(rod) if rod else r["posted_at"].date()
        r_cat = r.get("category")
        limit = MATCH_MAX_DAYS_EXACT if rod else MATCH_MAX_DAYS_FALLBACK
        best, best_score, best_dist = None, float("inf"), None
        for n in notices:
            if n["matched_review_id"]:
                continue
            dist = abs((r_date - date.fromisoformat(n["outing_date"])).days)
            if dist > limit:
                continue
            score = dist
            if n.get("category") and r_cat and n["category"] == r_cat:
                score -= CAT_MATCH_BONUS
            if n.get("author") and n["author"] == r.get("author"):
                score -= AUTHOR_MATCH_BONUS
            if score < best_score:
                best, best_score, best_dist = n, score, dist
        return best, best_dist

    # 가장 가까운 후기부터 공지를 선점 → 먼 후기가 가로채는 것 방지
    order = sorted(
        reviews,
        key=lambda r: (best_match(r)[1] if best_match(r)[1] is not None else 10**9),
    )
    for r in order:
        n, _ = best_match(r)
        if n is not None:
            n["matched_review_id"] = r["id"]
            n["attendees"] = list(r.get("attendees", []))
            n["actually_held"] = True
            r["matched_outing_id"] = n["id"]
    return posts


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
