"""
다감노📸 소모임 분석 — Streamlit 앱

흐름: 수집 → 분류 검토(드롭박스 보정) → 인사이트 + 엑셀 다운로드.
실행: streamlit run streamlit_app.py
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from core.collector import (
    GROUP_NAME,
    NON_OUTING_CATS,
    OUTING_CATS,
    annotate_attendees,
    collect_all_unresolved,
    collect_banned_names,
    collect_join_greetings,
    collect_members,
    collect_photos,
    collect_posts,
    find_duplicate_member_names,
    in_ym_range,
    is_multi_year,
    match_outings_with_reviews,
    month_axis,
    parse_join_name_aliases,
    period_label,
    period_tag,
    ym_label,
    ym_of,
    ym_split,
    ym_valid,
)
from core.excel_builder import build_excel
from core.gsheets import sheet_url

ALL_CATS = OUTING_CATS + NON_OUTING_CATS
CAT_OPTIONS = ALL_CATS + ["(없음)"]

# 엑셀 내보내기를 잠시 숨긴다.
#
# `core/excel_builder.py`가 화면과 어긋났다 — 없어진 `👤 사용자` 시트가 그대로
# 있고, 신뢰도·함께 간 사람·정착률 같은 새 통계는 빠져 있다. **화면과 다른
# 결과 파일을 내보내면 어느 쪽이 맞는지 알 수 없게 되므로** 맞출 때까지 숨긴다.
#
# 구글 시트 내보내기도 같은 `build_excel` 바이트를 올리는 방식이라 함께 숨긴다.
# 코드는 그대로 두었으니 이 값만 True로 돌리면 살아난다.
SHOW_EXPORT = False
STATUS_OPTIONS = ["진행", "취소"]

# 표에 몇 개를 실을지는 **한 곳에서만 정한다.**
#
# 캡션에 숫자를 손으로 적어 두면 값을 올릴 때 캡션이 그대로 남는다 — 실제로
# "상위 20쌍"이라 써 놓고 40쌍을 그리게 된다. 화면이 제 숫자를 잘못 설명하면
# 어느 쪽이 맞는지 사용자가 알 방법이 없다.
CO_ATTENDANCE_TOP = 40      # 함께 간 사람 — 표시할 쌍 수
PREF_TOP_N = 5              # 선호 카테고리 — 멤버당 표시할 개수
GALLERY_COLS = 4            # 갤러리 격자 — 인기 사진 갤러리와 같은 열 수
GALLERY_PAGE = 40           # 한 페이지에 그릴 사진 수 (4열 × 10줄)


# ═══════════════════════════════════════════════════════════════
# 순수 계산 헬퍼 (Streamlit 비의존 — 단독 테스트 가능)
# ═══════════════════════════════════════════════════════════════
#
# 월별 집계는 전부 `{ym: 건수}` (ym = YYYYMM)로 모으고, 표시 직전에
# `month_axis`가 만든 축에 맞춰 펼친다. `.month`만 쓰면 다년 범위에서
# 2025-03과 2026-03이 한 칸에 합쳐지므로 연도를 절대 버리지 않는다.

def axis_values(counts: dict[int, int], months: list[int]) -> list[int]:
    """`{ym: 건수}` → 축 순서대로 펼친 리스트 (데이터 없는 달은 0)."""
    return [counts.get(m, 0) for m in months]


def axis_labels(months: list[int]) -> list[str]:
    """월 축 라벨. 다년 범위면 `2026-03`, 한 해 안이면 `3월`."""
    if not months:
        return []
    multi = is_multi_year(months[0], months[-1])
    return [ym_label(m, multi_year=multi) for m in months]


def post_ym(p: dict) -> int | None:
    """공지의 시간축은 출사일 — 미상이면 None(축에 넣을 수 없음)."""
    od = p.get("outing_date")
    return ym_of(date.fromisoformat(od)) if od else None


def compute_kpis(posts: list[dict], photos: list[dict]) -> dict[str, int]:
    posts_A = [p for p in posts if p["cat"] == "A"]
    return {
        "전체 게시글": len(posts),
        "진행 출사":  sum(1 for p in posts_A if not p["is_canceled"]),
        "취소 출사":  sum(1 for p in posts_A if p["is_canceled"]),
        "후기글":     sum(1 for p in posts if p["cat"] == "E"),
        "사진 업로드": len(photos),
        "테마 예상":  sum(1 for p in photos if p["has_comment"]),
    }


def monthly_table(posts: list[dict], photos: list[dict]) -> dict[str, dict[int, int]]:
    """월별 활동 집계 — `{구분: {ym: 건수}}`. 축은 호출부가 결정한다."""
    posts_A  = [p for p in posts if p["cat"] == "A"]
    active   = [p for p in posts_A if not p["is_canceled"]]
    canceled = [p for p in posts_A if p["is_canceled"]]
    reviews  = [p for p in posts if p["cat"] == "E"]
    themed   = [p for p in photos if p["has_comment"]]

    def by_outing(items: list[dict]) -> dict[int, int]:
        out: Counter = Counter()
        for x in items:
            ym = post_ym(x)
            if ym is not None:
                out[ym] += 1
        return dict(out)

    def by_posted(items: list[dict]) -> dict[int, int]:
        out: Counter = Counter()
        for x in items:
            out[ym_of(x["posted_at"])] += 1
        return dict(out)

    return {
        "진행 출사":   by_outing(active),
        "취소 출사":   by_outing(canceled),
        "후기글":      by_posted(reviews),
        "사진":        by_posted(photos),
        "테마사진 참가": by_posted(themed),
    }


def top_posters(posts: list[dict], n: int = 10) -> list[dict]:
    """작성자별 게시 활동 랭킹. 활성 멤버(is_active)만 집계.

    탈퇴 멤버는 통계에서 제외 — 단, 매칭/참석 집계는 별도 함수에서 원본을 그대로 본다.
    """
    agg: dict[str, dict] = {}
    for p in posts:
        if not p.get("is_active", True):
            continue
        s = agg.setdefault(p["author"], {
            "작성자": p["author"], "게시글": 0, "공지": 0, "취소": 0, "후기": 0, "좋아요": 0,
        })
        s["게시글"] += 1
        if p["cat"] == "A":
            s["취소" if p["is_canceled"] else "공지"] += 1
        elif p["cat"] == "E":
            s["후기"] += 1
        s["좋아요"] += p["likes"]
    return sorted(agg.values(), key=lambda x: -x["게시글"])[:n]


def category_counts(posts: list[dict]) -> list[dict]:
    posts_A = [p for p in posts if p["cat"] == "A"]
    rows = []
    for c in ALL_CATS:
        sub = [p for p in posts_A if p["category"] == c]
        if sub:
            rows.append({
                "카테고리": c,
                "유형": "출사" if c in OUTING_CATS else "활동",
                "개수": len(sub),
                "좋아요": sum(p["likes"] for p in sub),
            })
    return sorted(rows, key=lambda x: -x["개수"])


def category_monthly(posts: list[dict], months: list[int], *,
                     exclude_canceled: bool = False) -> tuple[list[dict], dict[str, int]]:
    """출사 공지(cat=A)를 (월 × 카테고리)로 집계 — Altair long-format.

    월 소스는 **출사일**(공지의 시간축). 축에 놓을 수 없는 건은 세어서 함께 돌려주고
    호출부가 caption으로 노출한다 — 조용히 누락되면 "그 달엔 출사가 없었다"로 오해된다.

    Returns:
        (rows, skipped) — rows는 `[{"월": "2026-03", "카테고리": "인물", "공지 수": 4}, ...]`,
        skipped는 `{"출사일 미상": n, "카테고리 미상": n, "취소 제외": n}`.
    """
    labels = axis_labels(months)
    label_of = dict(zip(months, labels))
    skipped = {"출사일 미상": 0, "카테고리 미상": 0, "취소 제외": 0}

    counts: dict[tuple[int, str], int] = Counter()
    seen_cats: set[str] = set()
    for p in posts:
        if p.get("cat") != "A":
            continue
        if exclude_canceled and p.get("is_canceled"):
            skipped["취소 제외"] += 1
            continue
        ym = post_ym(p)
        if ym is None:
            skipped["출사일 미상"] += 1
            continue
        cat = p.get("category")
        if not cat:
            skipped["카테고리 미상"] += 1
            continue
        if ym not in label_of:
            continue  # 축 밖(기간 외) — 정상 경로에선 나오지 않음
        counts[(ym, cat)] += 1
        seen_cats.add(cat)

    # 빈 달도 0으로 채워 축이 끊기지 않게 한다.
    ordered_cats = [c for c in ALL_CATS if c in seen_cats]
    rows = [
        {"월": label_of[m], "카테고리": c, "공지 수": counts.get((m, c), 0)}
        for m in months for c in ordered_cats
    ]
    return rows, skipped


def outing_user_ranking(posts: list[dict]) -> list[dict]:
    """공지(cat=A) 작성자 랭킹. 활성 멤버만 집계."""
    agg: dict[str, dict] = {}
    for p in posts:
        if p["cat"] != "A":
            continue
        if not p.get("is_active", True):
            continue
        s = agg.setdefault(p["author"], {"작성자": p["author"], "진행": 0, "취소": 0})
        s["취소" if p["is_canceled"] else "진행"] += 1
    rows = []
    for s in agg.values():
        tot = s["진행"] + s["취소"]
        s["합계"] = tot
        s["취소율"] = round(s["취소"] / tot * 100, 1) if tot else 0.0
        rows.append(s)
    return sorted(rows, key=lambda x: -x["합계"])


def cancel_ranking(posts: list[dict], min_notices: int = 3) -> list[dict]:
    rows = [r for r in outing_user_ranking(posts) if r["합계"] >= min_notices]
    return sorted(rows, key=lambda x: (-x["취소율"], -x["취소"]))


def photo_user_ranking(photos: list[dict]) -> list[dict]:
    """사진 업로더 랭킹. 활성 멤버만 집계."""
    agg: dict[str, dict] = {}
    for p in photos:
        if not p.get("is_active", True):
            continue
        s = agg.setdefault(p["author"], {
            "작성자": p["author"], "사진수": 0, "테마예상": 0, "좋아요": 0, "댓글": 0,
        })
        s["사진수"] += 1
        if p["has_comment"]:
            s["테마예상"] += 1
        s["좋아요"] += p["likes"]
        s["댓글"] += p["comments"]
    rows = []
    for s in agg.values():
        s["테마비율"] = round(s["테마예상"] / s["사진수"] * 100, 1) if s["사진수"] else 0.0
        s["장당좋아요"] = round(s["좋아요"] / s["사진수"], 1) if s["사진수"] else 0.0
        rows.append(s)
    return sorted(rows, key=lambda x: -x["사진수"])


def theme_matrix(photos: list[dict], months: list[int] | None = None):
    """테마사진(댓글>0) 작성자×월 매트릭스. 활성 멤버만 집계.

    월 키는 ym(YYYYMM). `months`를 주면 그 축의 달을 모두 채우고(없으면 빈 값),
    생략하면 데이터에 등장한 달만 담는다.
    """
    user_month: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for p in photos:
        if not p.get("is_active", True):
            continue
        if p["has_comment"]:
            user_month[p["author"]][ym_of(p["posted_at"])] += 1
    authors = sorted(
        user_month,
        key=lambda a: (-len(user_month[a]), -sum(user_month[a].values())),
    )
    axis = months if months is not None else sorted({m for d in user_month.values() for m in d})
    mon_list = {m: sorted(a for a in user_month if m in user_month[a]) for m in axis}
    mon_count = {m: len(mon_list[m]) for m in axis}
    return user_month, authors, mon_count, mon_list


def theme_participant_ranking(photos: list[dict]) -> list[dict]:
    user_month, authors, _, _ = theme_matrix(photos)
    return [
        {"작성자": a, "참여월수": len(user_month[a]), "테마사진": sum(user_month[a].values())}
        for a in authors
    ]


def themed_photos_by_month(photos: list[dict]) -> dict[int, list[dict]]:
    """댓글 달린 사진(테마사진 후보)을 월(ym)별로 모아 작성자순 정렬 — 미리보기 검증용."""
    out: dict[int, list[dict]] = defaultdict(list)
    for p in photos:
        if p["has_comment"]:
            out[ym_of(p["posted_at"])].append(p)
    for m in out:
        out[m].sort(key=lambda x: (x["author"], -x["likes"]))
    return out


def outings_table(posts: list[dict]) -> list[dict]:
    rows = []
    for p in sorted((p for p in posts if p["cat"] == "A"),
                    key=lambda x: x["outing_date"] or "0000", reverse=True):
        od = p["outing_date"]
        dday = (date.fromisoformat(od) - p["posted_at"].date()).days if od else None
        rows.append({
            "출사일": od or "-",
            "공지일": p["posted_at"].strftime("%Y-%m-%d"),
            "D-day": f"+{dday}" if dday is not None and dday >= 0 else (str(dday) if dday is not None else "-"),
            "작성자": p["author"],
            "카테고리": p["category"] or "-",
            "유형": "출사" if p["is_outing"] else "활동",
            "상태": "취소" if p["is_canceled"] else "진행",
            "제목": p["title"],
            "좋아요": p["likes"],
            "댓글": p["comments"],
        })
    return rows


def reviews_table(posts: list[dict]) -> list[dict]:
    rows = []
    for p in sorted((p for p in posts if p["cat"] == "E"),
                    key=lambda x: x["posted_at"], reverse=True):
        rows.append({
            "작성일": p["posted_at"].strftime("%Y-%m-%d"),
            "월": p["posted_at"].strftime("%Y-%m"),
            "작성자": p["author"],
            "카테고리": p["category"] or "-",
            "제목": p["title"],
            "좋아요": p["likes"],
            "댓글": p["comments"],
        })
    return rows


def posts_dataframe(posts: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "ID": p["id"],
        "작성자": p["author"],
        "유형": p["cat_label"],
        "카테고리": p["category"] or "",
        "제목": p["title"],
        "작성일": p["posted_at"].strftime("%Y-%m-%d %H:%M"),
        "출사일": p["outing_date"] or "",
        "상태": "취소" if p["is_canceled"] else ("진행" if p["cat"] == "A" else ""),
        "좋아요": p["likes"],
        "댓글": p["comments"],
        "이미지수": p["images"],
    } for p in sorted(posts, key=lambda x: x["posted_at"], reverse=True)])


def photos_dataframe(photos: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "ID": p["id"],
        "작성자": p["author"],
        "업로드일": p["posted_at"].strftime("%Y-%m-%d %H:%M"),
        "좋아요": p["likes"],
        "댓글": p["comments"],
        "테마예상": "🎨" if p["has_comment"] else "",
        "고화질 URL": p["url_large"],
        "썸네일 URL": p["url_thumb"],
    } for p in sorted(photos, key=lambda x: x["posted_at"], reverse=True)])


def top_photos(photos: list[dict], n: int = 12) -> list[dict]:
    """인기 = 좋아요(lc) 내림차순, 동률은 댓글(rn)."""
    return sorted(photos, key=lambda p: (-p["likes"], -p["comments"]))[:n]


def summary_extras(posts: list[dict], photos: list[dict]) -> dict:
    return {
        "게시글 좋아요": sum(p["likes"] for p in posts),
        "게시글 댓글": sum(p["comments"] for p in posts),
        "사진 좋아요": sum(p["likes"] for p in photos),
        "사진 댓글": sum(p["comments"] for p in photos),
        "top_post_likes":    max(posts, key=lambda p: p["likes"]) if posts else None,
        "top_post_comments": max(posts, key=lambda p: p["comments"]) if posts else None,
        "top_photo_likes":   max(photos, key=lambda p: p["likes"]) if photos else None,
    }


def period_coverage(posts: list[dict], photos: list[dict]):
    dts = [p["posted_at"] for p in posts] + [p["posted_at"] for p in photos]
    return (min(dts).date(), max(dts).date()) if dts else None


# ── 후기 본문 기반 참석 (PR2: tab 데이터 헬퍼) ──────────────────

def attendance_counts(posts: list[dict]) -> list[dict]:
    """매칭된 출사(actually_held)의 참석자(실명) 합계."""
    cnt: Counter = Counter()
    for n in posts:
        if n.get("cat") == "A" and n.get("actually_held"):
            for name in n.get("attendees", []):
                cnt[name] += 1
    return [{"멤버": name, "참석횟수": c} for name, c in cnt.most_common()]


def member_category_pref(posts: list[dict]) -> dict[str, Counter]:
    pref: dict[str, Counter] = defaultdict(Counter)
    for n in posts:
        if n.get("cat") == "A" and n.get("actually_held"):
            cat = n.get("category")
            if cat:
                for name in n.get("attendees", []):
                    pref[name][cat] += 1
    return pref


def top_category_label(pref: Counter, n: int = PREF_TOP_N) -> str:
    """`인물(12), 풍경(9), …` 한 줄. 비면 `—`.

    2개만 보이던 시절엔 "이 사람은 인물·풍경만 간다"처럼 읽혔는데, 실제로는
    3위 이하가 잘려 있었을 뿐이다. 카테고리가 7종뿐이라 5개면 사실상 전부다.

    **표 안에서 f-string으로 조립하지 않는다** — 그러면 상한을 확인하려고
    앱을 통째로 렌더해야 한다. 여기 있으면 한 줄 테스트로 고정된다.
    """
    return ", ".join(f"{c}({v})" for c, v in pref.most_common(n)) or "—"


def attendance_monthly_matrix(posts: list[dict]):
    """(member_month dict[name->dict[ym->count]], members_sorted_by_total). 월 키는 YYYYMM."""
    mm: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for n in posts:
        if n.get("cat") != "A" or not n.get("actually_held"):
            continue
        m = post_ym(n)
        if m is None:
            continue
        for name in n.get("attendees", []):
            mm[name][m] += 1
    members = sorted(mm, key=lambda x: -sum(mm[x].values()))
    return mm, members


def real_attendance_rate(posts: list[dict]) -> dict:
    notices = [p for p in posts if p.get("cat") == "A"]
    held = [p for p in notices if p.get("actually_held")]
    return {
        "공지": len(notices),
        "매칭": len(held),
        "진행률": round(len(held) / len(notices) * 100, 1) if notices else 0.0,
    }


def member_first_seen(posts: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """(first_seen_iso, last_seen_iso) per 실명 — '신규 멤버 등장 시점' 산출용."""
    held = sorted(
        (p for p in posts
         if p.get("cat") == "A" and p.get("actually_held") and p.get("outing_date")),
        key=lambda x: x["outing_date"],
    )
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    for n in held:
        for name in n.get("attendees", []):
            first.setdefault(name, n["outing_date"])
            last[name] = n["outing_date"]
    return first, last


def attendees_table(posts: list[dict]) -> list[dict]:
    """출사별 참석자 표. 취소(펑) 출사는 제외하고, 매칭된 후기 제목과 함께 보여
    매칭이 제대로 됐는지 사용자가 검증할 수 있게 한다. 후기가 없는 출사는 상태
    컬럼으로 강조해 누락을 인지시킨다.
    """
    review_by_id = {p["id"]: p for p in posts if p.get("cat") == "E"}
    rows = []
    for p in sorted(
        (p for p in posts if p.get("cat") == "A" and not p.get("is_canceled")),
        key=lambda x: x.get("outing_date") or "0000", reverse=True,
    ):
        att = p.get("attendees", [])
        matched_id = p.get("matched_review_id")
        review = review_by_id.get(matched_id) if matched_id else None
        rows.append({
            "출사일": p.get("outing_date") or "-",
            "카테고리": p.get("category") or "-",
            "상태": "✓ 매칭" if review else "⚠️ 후기 없음",
            "공지자": p["author"],
            "참석자수": len(att),
            "참석자": ", ".join(att) if att else "—",
            "공지 제목": p["title"],
            "후기 제목": review["title"] if review else "—",
        })
    return rows


def orphan_reviews(posts: list[dict]) -> list[dict]:
    """공지와 못 이어진 후기 — 참석자가 집계에서 빠지는 것들.

    소모임이 자동으로 만든 정모 게시글은 뺀다. 후기가 아니라 참석자도 없어서
    **사람이 할 일이 없는데**, 신뢰도 패널에 남으면 영원히 안 줄어드는 숙제로
    보인다.
    """
    return [p for p in posts if p.get("cat") == "E"
            and not p.get("matched_outing_id") and not p.get("is_meetup_post")]


def activity_ranking(posts: list[dict], photos: list[dict]) -> list[dict]:
    """작성자별 게시글·사진 종합. 글이나 사진이 1건이라도 있으면 포함."""
    photo_cnt = {r["작성자"]: r["사진수"] for r in photo_user_ranking(photos)}
    by_author = {r["작성자"]: r for r in top_posters(posts, n=max(len(posts), 1))}
    rows = []
    for author in set(by_author) | set(photo_cnt):
        pr = by_author.get(author)
        rows.append({
            "작성자": author,
            "게시글": pr["게시글"] if pr else 0,
            "사진": photo_cnt.get(author, 0),
            "공지": pr["공지"] if pr else 0,
            "취소": pr["취소"] if pr else 0,
            "후기": pr["후기"] if pr else 0,
            "좋아요": pr["좋아요"] if pr else 0,
        })
    rows.sort(key=lambda x: (-x["게시글"], -x["사진"]))
    return rows


# ═══════════════════════════════════════════════════════════════
# 신뢰도 — 이 숫자를 얼마나 믿어도 되나
#
# 본문이 잘려 들어오는 탓에 참석자·출사일이 비어 있는 경우가 있다. 그때
# **진짜 없는 것과 아직 안 채운 것을 구분할 수 없으면** 결과를 잘못 읽는다.
# 그래서 무엇이 얼마나 비어 있는지를 결과 화면 맨 앞에 세운다.
# ═══════════════════════════════════════════════════════════════

def confidence_report(posts: list[dict],
                      pending: dict[str, int] | None = None) -> list[dict]:
    """보정이 필요한 항목을 종류별로. 각 행은 `{항목, 건수, 어디서, 설명}`.

    **건수는 보정 시트의 미기입 행 수를 그대로 쓴다.** 여기서 판정을 다시
    쓰면 시딩 조건과 조금씩 어긋나, 화면은 "7건 필요"라는데 시트에는 아무것도
    없는 상태가 된다(실제로 그랬다). 사이드바와도 같은 숫자를 보게 된다.

    고아 후기만 시트에 자리가 없어 여기서 센다 — 사람이 채울 것이 아니라
    매칭이 안 됐다는 사실 자체를 알리는 항목이다.
    """
    from core.store import (TAB_ATTENDEE_FIX, TAB_MEMBER_NAMES, TAB_NAME_MAP,
                            TAB_POST_FIX)
    pending = pending or {}
    return [
        {"항목": "실명 미기입 멤버", "건수": pending.get(TAB_MEMBER_NAMES, 0),
         "어디서": TAB_MEMBER_NAMES,
         "설명": "실명을 채우면 후기 본문의 이름이 자동으로 멤버와 이어집니다"},
        {"항목": "참석자 못 뽑은 후기", "건수": pending.get(TAB_ATTENDEE_FIX, 0),
         "어디서": TAB_ATTENDEE_FIX,
         "설명": "본문이 잘려 오면 뒤쪽 참석자가 통째로 빠집니다"},
        {"항목": "해소 안 된 이름", "건수": pending.get(TAB_NAME_MAP, 0),
         "어디서": TAB_NAME_MAP,
         "설명": "실명 명단으로도 멤버와 이어지지 않은 이름 (오타·나간 멤버 등)"},
        {"항목": "검토 대상 공지", "건수": pending.get(TAB_POST_FIX, 0),
         "어디서": TAB_POST_FIX,
         "설명": "출사일·카테고리를 자동으로 확정하지 못한 공지"},
        {"항목": "공지와 안 이어진 후기", "건수": len(orphan_reviews(posts)),
         "어디서": "—",
         "설명": "짝이 될 출사 공지를 못 찾은 후기 (참석 집계에서 빠짐)"},
    ]


def avg_attendance_trend(posts: list[dict],
                         months: list[int]) -> dict[int, float | None]:
    """월별 **출사당 평균 참석 인원**. 출사가 없는 달은 None(0이 아니다).

    참석자 0명인 출사도 분모에 넣는다 — 빼면 "인원이 적힌 출사"만 평균 내게
    되어 모임 규모가 실제보다 부풀려진다.
    """
    per_month: dict[int, list[int]] = {m: [] for m in months}
    for p in posts:
        if p.get("cat") != "A" or not p.get("actually_held"):
            continue
        m = post_ym(p)
        if m in per_month:
            per_month[m].append(len(p.get("attendees") or []))
    return {m: (round(sum(v) / len(v), 1) if v else None)
            for m, v in per_month.items()}


def _attendance_pairs(posts: list[dict]) -> tuple[Counter, Counter]:
    """(쌍 횟수, 사람별 참석 횟수). 매칭된(actually_held) 출사만 센다.

    전역 상위 쌍(`co_attendance`)과 한 사람의 동행 순위(`member_companions`)가
    **같은 규칙을 봐야 한다** — 한 출사에 같은 이름이 두 번 적혀도 자기 자신과
    짝이 되지 않고(그래서 `set`), A-B와 B-A는 한 쌍이다(그래서 `sorted`).
    두 곳에서 따로 세면 참석 탭과 멤버 상세 탭이 다른 숫자를 말하게 되는데,
    사용자는 그걸 버그로 읽는다.
    """
    pair: Counter = Counter()
    solo: Counter = Counter()
    for p in posts:
        if p.get("cat") != "A" or not p.get("actually_held"):
            continue
        names = sorted({n for n in (p.get("attendees") or []) if n})
        for n in names:
            solo[n] += 1
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                pair[(a, b)] += 1
    return pair, solo


def _pct(n: int, total: int) -> float:
    return round(n / total * 100, 1) if total else 0.0


def _pctstr(v: float) -> str:
    """`70.0%`가 아니라 `70%`. 소수점이 필요할 때만 붙인다."""
    return f"{v:g}%"


def _wilson_low(k: int, n: int, z: float = 1.96) -> float:
    """비율 `k/n`의 95% 신뢰구간 하한. **표본이 얇을수록 더 깎는다.**

    3회 중 3회(100%)와 30회 중 30회(100%)는 관측 비율이 같지만 믿을 만한
    정도가 다르다. 그대로 쓰면 두세 번 만난 쌍이 순위를 다 차지한다.
    """
    if n <= 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d)


def _affinity(n: int, mine: int, theirs: int, total: int) -> dict:
    """두 사람의 겹침을 연관 규칙으로 잰 값.

    `lift`는 "우연히 겹칠 때보다 몇 배 자주 만나나"다. 함께 간 **횟수**만 보면
    전체 출사의 절반에 나오는 사람이 늘 1등이 된다 — 그 사람과 겹치는 건
    따라다녀서가 아니라 그냥 확률이다. lift는 그 확률을 나눠서 지운다.

    다만 생 lift를 그대로 고르는 데 쓰면 **표본이 얇을 때 폭발한다**(3회
    만나고 18.8배). 그래서 **고르고 자르는 데에는 `하한`을 쓰고**, 사람에게
    보여 주는 문구에는 관측값 `lift`를 쓴다.

    `기대`는 lift를 말로 풀기 위한 값이다 — "우연이라면 0.5회쯤 겹쳤을 텐데".
    lift 4.5는 일반인에게 아무 뜻이 없지만 이 문장은 읽으면 안다.
    """
    if not mine or not theirs or not total:
        return {"기대": 0.0, "lift": 0.0, "하한": 0.0}
    base = theirs / total
    return {"기대": mine * theirs / total,
            "lift": (n / mine) / base,
            "하한": _wilson_low(n, mine) / base}


def _held_count(posts: list[dict]) -> int:
    """실제로 다녀온 출사 수 — `_affinity`의 분모."""
    return sum(1 for p in posts
               if p.get("cat") == "A" and p.get("actually_held"))


def _affinity_top(pair: Counter, solo: Counter, total: int) -> dict[str, dict]:
    """`{이름: 우연 대비 1순위}`. **전원분을 한 번에 만든다.**

    비율은 원 정수(`pair`·`solo`)에서 바로 잰다 — `member_companions`의
    `내 기준`은 소수점 한 자리로 반올림한 표시용 값이라 나눗셈에 쓰면 오차가
    쌓인다.

    1순위는 **하한**으로 고른다. 관측 lift로 고르면 세 번 만난 쌍이 늘 이긴다.
    """
    cands: defaultdict[str, list] = defaultdict(list)
    for (a, b), n in pair.items():
        if n < PAIR_MIN_JOINT:
            continue
        for me, other in ((a, b), (b, a)):
            mine, theirs = solo.get(me, 0), solo.get(other, 0)
            row = _affinity(n, mine, theirs, total)
            cands[me].append({"상대": other, "함께": n,
                              "내비율": _pct(n, mine), **row})
    # 동점은 이름으로 끊는다 — 같은 데이터면 늘 같은 짝이 나와야 한다.
    return {me: max(rows, key=lambda r: (r["하한"], r["함께"], r["상대"]))
            for me, rows in cands.items()}


def co_attendance(posts: list[dict], top_n: int = CO_ATTENDANCE_TOP) -> list[dict]:
    """함께 간 횟수 상위 쌍.

    쌍은 정렬해 담으므로 A-B와 B-A가 한 행으로 합쳐진다. 참석자가 한 명뿐인
    출사에서는 쌍이 생기지 않는다.

    **비율은 양쪽 기준을 모두 낸다.** 두 사람의 전체 참석 수가 달라
    "8번 함께"가 한쪽에겐 대부분이고 다른 쪽에겐 일부일 수 있다.

    **컬럼 이름은 고정이고 사람 이름은 값으로만 들어간다.** 예전에는
    `f"{a} 참석"`처럼 이름을 키로 썼는데, `pd.DataFrame`은 모든 행의 키를
    합집합으로 모으므로 20쌍이 전부 다른 사람이면 **컬럼이 80개**가 되고
    자기 행이 아닌 칸은 전부 빈칸이었다. 표가 옆으로 끝없이 길어질 뿐
    아니라, 이름이 박힌 헤더만 봐서는 그 숫자가 횟수인지 퍼센트인지도
    알 수 없었다.

    `top_n` 기본값은 `CO_ATTENDANCE_TOP`이다 — 화면 캡션도 같은 상수를 읽어야
    "상위 N쌍"이라는 말과 실제 행 수가 어긋나지 않는다.
    """
    pair, solo = _attendance_pairs(posts)
    rows = []
    for (a, b), n in pair.most_common(top_n):
        rows.append({
            # 이름을 두 칸으로 나눈다 — 그래야 `A 참석`·`A 기준`이 누구
            # 얘기인지 헤더만 보고 안다. `나무 · 바다`로 붙여 두면 왼쪽
            # 숫자가 누구 것인지 표 어디에도 단서가 없다.
            "사람 A": a, "사람 B": b, "함께": n,
            "A 참석": solo[a], "A 기준": _pct(n, solo[a]),
            "B 참석": solo[b], "B 기준": _pct(n, solo[b]),
        })
    return rows


CO_ATTENDANCE_COLS = ["사람 A", "사람 B", "함께", "A 참석", "A 기준",
                      "B 참석", "B 기준"]

COMPANION_COLS = ["함께 간 사람", "함께", "내 기준", "상대 참석", "상대 기준"]


def member_companions(name: str, posts: list[dict],
                      pairs: tuple[Counter, Counter] | None = None) -> list[dict]:
    """한 사람의 동행 **전원**. 상한을 두지 않는다.

    `co_attendance`로는 이 화면을 만들 수 없다 — 그쪽은 **전역 상위 N쌍**이라
    참석이 적은 사람은 목록에 한 줄도 못 올라간다. 그 사람 화면에서 "함께 간
    사람이 없다"로 보이면 **사실이 아니다.**

    컬럼은 `COMPANION_COLS`로 고정하고 이름은 값으로만 넣는다(`co_attendance`
    주석의 80칸 표 사고와 같은 이유). 다만 `사람 A`/`사람 B`는 쓰지 않는다 —
    여기서는 기준이 되는 사람이 이미 정해져 있어 한 칸이면 되고, 참석 탭의
    표와 헤더가 같으면 어느 화면을 보고 있는지 헷갈린다.

    `pairs`는 이미 세어 둔 `_attendance_pairs` 결과를 넘기는 통로다 — 전 멤버의
    칭호를 낼 때 쉰 명분 쌍을 쉰 번 다시 세면 그 구역을 열 때마다 멈춘다.
    안 넘기면 스스로 센다(단독 호출·테스트가 그대로 돈다).
    """
    pair, solo = pairs if pairs is not None else _attendance_pairs(posts)
    mine = solo.get(name, 0)
    rows = []
    for (a, b), n in pair.items():
        if a == name:
            other = b
        elif b == name:
            other = a
        else:
            continue
        rows.append({
            "함께 간 사람": other, "함께": n,
            "내 기준": _pct(n, mine),
            "상대 참석": solo[other], "상대 기준": _pct(n, solo[other]),
        })
    return sorted(rows, key=lambda r: (-r["함께"], -r["상대 기준"], r["함께 간 사람"]))


# ═══════════════════════════════════════════════════════════════
# 카테고리 — 📌 출사 탭으로 합치면서 통계를 붙인다
# ═══════════════════════════════════════════════════════════════

def category_author_ranking(posts: list[dict]) -> list[dict]:
    """작성자 × 카테고리 교차표. 누가 어떤 출사를 주로 여는지."""
    agg: dict[str, dict] = {}
    for p in posts:
        if p.get("cat") != "A" or not p.get("is_active", True):
            continue
        cat = p.get("category")
        if not cat:
            continue
        row = agg.setdefault(p["author"], {"작성자": p["author"], "합계": 0})
        row[cat] = row.get(cat, 0) + 1
        row["합계"] += 1
    cols = [c for c in ALL_CATS if any(c in r for r in agg.values())]
    rows = []
    for r in sorted(agg.values(), key=lambda x: -x["합계"]):
        rows.append({"작성자": r["작성자"], "합계": r["합계"],
                     **{c: r.get(c, 0) for c in cols}})
    return rows


def _like_stats(groups: dict[str, list[dict]], key_name: str) -> list[dict]:
    """`{키: 공지들}` → 건수·좋아요 합·평균.

    **평균만 두면 1건 쓰고 좋아요 많이 받은 쪽이 1위가 된다.** 건수를 같이
    실어 표본이 작다는 것이 눈에 보이게 한다.
    """
    rows = []
    for k, items in groups.items():
        if not items:
            continue
        total = sum(p.get("likes") or 0 for p in items)
        rows.append({key_name: k, "공지 수": len(items), "좋아요 합": total,
                     "평균 좋아요": round(total / len(items), 1)})
    return sorted(rows, key=lambda r: -r["평균 좋아요"])


def category_likes(posts: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        if p.get("cat") == "A" and p.get("category"):
            groups[p["category"]].append(p)
    return _like_stats(groups, "카테고리")


def author_likes(posts: list[dict], min_posts: int = 2) -> list[dict]:
    """작성자별 공지 평균 좋아요. 표본이 너무 작으면 순위가 무의미해 걸러 낸다."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in posts:
        if p.get("cat") == "A" and p.get("is_active", True):
            groups[p["author"]].append(p)
    return [r for r in _like_stats(groups, "작성자") if r["공지 수"] >= min_posts]


def dormant_members(posts: list[dict], members: list[dict],
                    as_of: date | None = None,
                    quiet_months: int = 3) -> list[dict]:
    """참석 이력이 있는데 최근 `quiet_months` 동안 조용한 멤버.

    유령 멤버(전 기간 0건)와 다르다 — **활동하다 끊긴 사람**을 잡는 것이 목적이다.
    한 번도 참석한 적 없는 사람은 여기 넣지 않는다(이탈이 아니라 미유입이고,
    가입 직후라 아직 기회가 없었을 수도 있다).
    """
    _, last = member_first_seen(posts)
    if not last:
        return []
    as_of = as_of or max(date.fromisoformat(d) for d in last.values())
    cutoff = as_of - timedelta(days=int(quiet_months) * 30)

    by_nick = {m.get("mn"): m for m in members or [] if m.get("mn")}
    rows = []
    for name, iso in last.items():
        seen = date.fromisoformat(iso)
        if seen > cutoff:
            continue
        m = by_nick.get(name) or {}
        rows.append({
            "멤버": name,
            "마지막 참석": iso,
            "쉰 기간(일)": (as_of - seen).days,
            "마지막 방문": (m["last_visit"].strftime("%Y-%m-%d")
                        if m.get("last_visit") else "—"),
        })
    return sorted(rows, key=lambda r: r["마지막 참석"])


# ═══════════════════════════════════════════════════════════════
# 가입인사 기준 정착·이탈
#
# 멤버 목록(`members`)은 **지금 남아 있는 사람**뿐이라, 거기서 센 월별 가입은
# "그 달에 가입해서 아직 남아 있는 사람 수"다. 나간 사람은 애초에 목록에
# 없으므로 **이탈이 많았던 달일수록 가입이 적어 보인다** — 정반대로 읽힌다.
#
# 가입인사는 사람이 나가도 글이 남는다. 이 모임은 가입인사를 쓰지 않으면
# 12시간 안에 강퇴하므로, **가입인사 작성자 = 제대로 가입한 사람의 명단**이다.
# ═══════════════════════════════════════════════════════════════

def joiner_retention(posts: list[dict], members: list[dict],
                     months: list[int]) -> list[dict]:
    """월별 `{월, 가입, 잔류, 이탈}`. 가입인사(cat=J) 작성자 기준.

    잔류 판정은 **작성자 `wid` ↔ 현재 멤버 `mid`** 로 한다. 닉네임으로 맞추면
    닉네임을 바꾼 사람이 나간 것으로 잡힌다.
    """
    active = {str(m.get("mid")) for m in members or [] if m.get("mid")}
    if not active:
        return []
    label_of = dict(zip(months, axis_labels(months)))
    rows = {m: {"월": label_of[m], "가입": 0, "잔류": 0, "이탈": 0} for m in months}
    for p in posts:
        if p.get("cat") != "J" or not p.get("posted_at"):
            continue
        m = ym_of(p["posted_at"])
        if m not in rows:
            continue
        rows[m]["가입"] += 1
        rows[m]["잔류" if str(p.get("wid")) in active else "이탈"] += 1
    return [rows[m] for m in months]


def departed_joiners(posts: list[dict], photos: list[dict],
                     members: list[dict]) -> list[dict]:
    """가입인사를 썼지만 지금 멤버 목록에 없는 사람 — 나간 사람.

    마지막 활동일을 함께 낸다. 가입인사만 쓰고 사라졌는지, 한참 활동하다
    나갔는지는 뜻이 전혀 다르다.
    """
    active = {str(m.get("mid")) for m in members or [] if m.get("mid")}
    if not active:
        return []

    last: dict[str, datetime] = {}
    for it in list(posts) + list(photos):
        wid, at = str(it.get("wid")), it.get("posted_at")
        if wid and at and at > last.get(wid, datetime.min):
            last[wid] = at

    rows = []
    for p in posts:
        wid = str(p.get("wid"))
        if p.get("cat") != "J" or not wid or wid in active:
            continue
        seen = last.get(wid)
        joined = p.get("posted_at")
        rows.append({
            "멤버": p.get("author") or "—",
            "가입인사": joined.strftime("%Y-%m-%d") if joined else "—",
            "마지막 활동": seen.strftime("%Y-%m-%d") if seen else "—",
            "활동 기간(일)": (seen - joined).days if seen and joined else None,
        })
    return sorted(rows, key=lambda r: r["가입인사"], reverse=True)


def newcomer_settling(members: list[dict], posts: list[dict],
                      since_ym: int | None = None) -> tuple[list[dict], int]:
    """가입 후 첫 참석까지 걸린 기간. `(행 목록, 제외한 인원 수)`.

    **수집 기간보다 먼저 가입한 사람은 뺀다.** 그 사람의 첫 참석은 데이터
    밖에 있을 수 있어, "가입 후 N일 만에 첫 참석"이 사실이 아니게 된다.
    아직 안 온 사람은 `첫 참석`이 None — 그 수가 곧 유입의 질이다.

    제외 수를 함께 돌려주는 이유: 조용히 빠지면 명단이 틀린 것처럼 보인다.
    """
    first, _ = member_first_seen(posts)
    rows, skipped = [], 0
    for m in members or []:
        nick, joined = m.get("mn"), m.get("joined_at")
        if not nick or not joined:
            continue
        if since_ym is not None and ym_of(joined) < int(since_ym):
            skipped += 1
            continue
        iso = first.get(nick)
        days = None
        if iso:
            days = (date.fromisoformat(iso) - joined.date()).days
        rows.append({
            "멤버": nick,
            "가입일": joined.strftime("%Y-%m-%d"),
            "첫 참석": iso,
            "가입→첫 참석(일)": days if days is not None and days >= 0 else None,
        })
    return sorted(rows, key=lambda r: r["가입일"], reverse=True), skipped


# ═══════════════════════════════════════════════════════════════
# 멤버 상세 — 흩어져 있는 한 사람의 숫자를 한곳에 모은다
#
# 다른 탭은 전부 "한 행이 한 사람"인 전체 집계다. 사람 하나를 알아보려면 탭
# 다섯 개를 오가며 표에서 그 이름을 눈으로 찾아야 했다.
#
# **이름은 표시 이름(`닉네임(실명)`) 하나로 조인한다.** `core.store.relabel_names`가
# posts.author · photos.author · attendees[] · members.mn 네 곳에 **같은 문자열**을
# 박아 두므로 별도의 키가 필요 없다.
#
# **대상은 활성 멤버(`members`)뿐이다.** 탈퇴자까지 넣으면 `_mark_active`가
# 붙인 `is_active=False` 때문에 랭킹 헬퍼들이 그 사람을 통째로 빼는 것을 매번
# 우회해야 하고, 기간을 좁혔을 때 드롭다운에서 사람이 사라지는 문제도 생긴다.
# ═══════════════════════════════════════════════════════════════

# 장당 좋아요 순위에 들어가려면 있어야 할 최소 사진 수.
# 한 장 올려 좋아요 9를 받은 사람이 1등이면 그 등수는 아무 뜻이 없다.
LIKE_RANK_MIN_PHOTOS = 10


def competition_rank(name: str,
                     scores: list[tuple[str, float]]) -> tuple[int | None, int]:
    """(공동 등수, 모수). 같은 값이면 같은 등수, 다음은 건너뛴다 — 1·2·2·4.

    목록 위치를 그대로 등수로 쓰면 5회가 셋일 때 7·8·9등으로 갈린다. **같은
    숫자인데 등수가 다르면 본인 화면에서 그건 그냥 틀린 값이다.**

    입력 순서에 기대지 않고 **여기서 값 기준으로 정렬한다** — 부르는 쪽마다
    정렬이 다르면(`outing_user_ranking`은 합계 순, `attendance_counts`는 횟수
    순) 같은 함수가 화면마다 다른 등수를 낸다.
    """
    total = len(scores)
    mine = next((v for n, v in scores if n == name), None)
    if mine is None:
        return None, total
    return sum(1 for _, v in scores if v > mine) + 1, total


def top_share(name: str, scores: list[tuple[str, float]], share: float, *,
              min_people: int = 4, min_value: float = 1) -> bool:
    """모수 중 **상위 `share`**(0~1)에 드는가.

    고정값(`테마사진 3장 이상`)으로 칭호를 주면 모임 활동량에 따라 아무도 못
    받거나 전원이 받는다. 분위로 재면 어떤 기간을 봐도 일정 비율이 받는다.

    **모수가 작으면 아무에게도 안 준다** — 세 사람만 사진을 올린 기간의
    "상위 25%"는 1등 한 명을 돌려 말한 것뿐이라 칭호가 되지 못한다.

    **최소 절대값도 함께 본다** — 전원이 한 장씩 올린 기간에는 상위 30%도 그냥
    한 장이다. 다만 이 바닥은 낮게 잡고, 걸러 내는 일은 분위가 한다.
    """
    if len(scores) < min_people:
        return False
    mine = next((v for n, v in scores if n == name), None)
    if mine is None or mine < min_value:
        return False
    rank, total = competition_rank(name, scores)
    # 동점자가 많아 경계에 걸치면 넣어 준다 — 같은 값인데 한 명만 빼면 그게
    # `competition_rank`가 고친 바로 그 문제다.
    return rank is not None and rank <= max(1, round(total * share))


def bottom_share(name: str, scores: list[tuple[str, float]], share: float, *,
                 min_people: int = 4) -> bool:
    """`top_share`의 반대쪽. `혼자가 편한 사람`처럼 적을수록 걸리는 칭호용."""
    if len(scores) < min_people:
        return False
    flipped = [(n, -v) for n, v in scores]
    return top_share(name, flipped, share, min_people=min_people,
                     min_value=float("-inf"))


def club_context(posts: list[dict], photos: list[dict], members: list[dict],
                 months: list[int] | None = None) -> dict:
    """전 멤버 공통 집계 — 순위표·쌍·첫 등장·휴면 명단. **한 번 만들어 돌려 쓴다.**

    `member_profile`은 안에서 `attendance_counts`·`photo_user_ranking`·
    `outing_user_ranking`·`dormant_members`·`member_first_seen`을 매번 다시
    돈다. 한 사람만 볼 때는 문제가 없지만 **칭호 분포는 전 멤버를 훑으므로**
    쉰 명이면 그게 쉰 번이 되어 구역을 열 때마다 몇 초씩 멈춘다.
    """
    photo_rows = photo_user_ranking(photos)
    pair, solo = _attendance_pairs(posts)

    # 쌍 그래프의 차수 = 그 사람이 함께 가 본 사람 수. 쌍을 이미 다 세어
    # 두었으므로 한 번 더 훑기만 하면 된다.
    deg: Counter = Counter()
    for a, b in pair:
        deg[a] += 1
        deg[b] += 1

    first, last = member_first_seen(posts)

    # 활동 기간 대비 참석 — 늦게 합류한 사람은 누적으로는 영원히 위로 못 간다.
    # 월 축을 알아야 재므로 `months`를 준 경우에만 만든다.
    attended: dict[str, int] = Counter()
    for p in posts:
        if p.get("cat") == "A" and p.get("actually_held"):
            for n in p.get("attendees") or []:
                attended[n] += 1
    density = [(m["mn"], round(attended.get(m["mn"], 0)
                               / _active_months(m.get("joined_at"), months), 2))
               for m in members or [] if m.get("mn")] if months else []

    return {
        "참석": [(r["멤버"], r["참석횟수"]) for r in attendance_counts(posts)],
        # 펑만 낸 사람은 모수에서 뺀다 — "펑 아닌 출사를 연 사람들 중 몇 등"이라야
        # 말이 된다. 정렬은 믿지 않는다(`outing_user_ranking`은 합계 순이다).
        "개최": [(r["작성자"], r["진행"])
                for r in outing_user_ranking(posts) if r["진행"]],
        "사진": [(r["작성자"], r["사진수"]) for r in photo_rows],
        "테마": [(r["작성자"], r["테마예상"]) for r in photo_rows if r["테마예상"]],
        "좋아요": [(r["작성자"], r["장당좋아요"]) for r in photo_rows
                if r["사진수"] >= LIKE_RANK_MIN_PHOTOS],
        "동행자": [(n, c) for n, c in deg.items()],
        # 참석한 출사의 평균 인원 — `정출킬러`·`소수정예`가 양 끝에서 읽는다.
        "참석인원": list(_attended_crowd_all(posts).items()),
        "쌍": (pair, solo),
        # 우연 대비 1순위. **전원분을 여기서 한 번에 만든다** — "서로가 서로의
        # 1순위"는 상대 것도 알아야 판정되므로, 사람마다 다시 세면 전 멤버
        # 칭호를 뽑을 때 O(사람 × 쌍)이 된다.
        "인연1위": _affinity_top(pair, solo, _held_count(posts)),
        "첫등장": first,
        "최근": last,
        "휴면": {r["멤버"] for r in dormant_members(posts, members)},
        "밀도": density,
        "_축": months,
        # 여기서 한 번만 읽는다. 멤버마다 `date.today()`를 부르면 자정을 넘기는
        # 순간 같은 화면 안에서 사람마다 기준이 갈린다. 테스트는 이 값을
        # 갈아끼워 경계를 고정한다(`dormant_members(as_of=…)`와 같은 결).
        "오늘": date.today(),
        "후기저자": {p["id"]: p.get("author")
                  for p in posts if p.get("cat") == "E"},
        # 아래 셋은 칭호가 멤버마다 다시 돌면 O(게시글×참석자)가 되는 것들이다.
        # 전 멤버를 훑는 경로가 생겼으므로 여기서 한 번만 만든다.
        "카테고리": member_category_pref(posts),
        # 카테고리별로 **몇 건이나 열렸나**. 카테고리 칭호의 분모다.
        "카테고리총계": Counter(
            p.get("category") for p in posts
            if p.get("cat") == "A" and p.get("actually_held") and p.get("category")),
        "공지": {p["id"]: p for p in posts if p.get("cat") == "A"},
    }

GHOST_GRACE_DAYS = 30    # 가입 직후 이만큼은 유령이라 부르지 않는다


def activity_authors(posts: list[dict], photos: list[dict]) -> set[str]:
    """활동 흔적이 있는 이름 — **가입인사는 빼고** 센다.

    이 모임은 가입인사를 안 쓰면 12시간 안에 강퇴하므로 **전원이 가입인사를
    쓴다.** 그것까지 활동으로 세면 유령 멤버가 구조적으로 영원히 0명이 된다
    (실제로 0명이었다).
    """
    return ({p.get("author", "") for p in posts if p.get("cat") != "J"}
            | {p.get("author", "") for p in photos})


def joined_recently(joined, months: list[int],
                    days: int = GHOST_GRACE_DAYS) -> bool:
    """가입한 지 `days`일이 안 됐나 — **기간 마지막 달의 말일** 기준.

    오늘이 아니라 기간 끝으로 재는 이유는 `_is_newcomer`와 같다 — 과거 기간을
    들여다볼 때 궁금한 것은 "지금 신입"이 아니라 "그때 신입"이다.

    달 수가 아니라 날짜로 재는 이유: "가입월이 기간 마지막 달인가"로 재면 짧은
    달에서 어긋난다 — 1월 31일에 가입한 사람은 2월 말 기준 **28일밖에 안 됐는데**
    가입월이 달라서 유령이 된다.
    """
    if not joined or not months:
        return False
    y, m = ym_split(months[-1])
    last = date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)
    j = joined.date() if isinstance(joined, datetime) else joined
    return 0 <= (last - j).days < days


def member_options(members: list[dict], posts: list[dict],
                   photos: list[dict]) -> list[dict]:
    """드롭다운 후보 — `{이름, 참석, 게시글, 사진}`. 활동이 많은 순.

    활동이 0건인 유령 멤버도 남긴다. 그 페이지가 전부 0으로 나오는 것 자체가
    "가입만 하고 아무것도 안 했다"는 정보다 — 목록에서 빼면 그걸 확인할 방법이
    없어진다.

    `게시글`에서 **가입인사는 뺀다.** 안 빼면 아무것도 안 한 사람이 목록에서는
    `글 1`인데 골라 들어가면 "유령" 배지가 붙어, 같은 사람이 두 화면에서 다르게
    보인다.
    """
    attended: Counter = Counter()
    for p in posts:
        if p.get("cat") == "A" and p.get("actually_held"):
            for n in p.get("attendees") or []:
                attended[n] += 1
    wrote = Counter(p.get("author") for p in posts
                    if p.get("author") and p.get("cat") != "J")
    shot = Counter(p.get("author") for p in photos if p.get("author"))

    rows = [{"이름": m["mn"], "참석": attended.get(m["mn"], 0),
             "게시글": wrote.get(m["mn"], 0), "사진": shot.get(m["mn"], 0)}
            for m in members or [] if m.get("mn")]
    return sorted(rows, key=lambda r: (-r["참석"], -r["게시글"], -r["사진"], r["이름"]))


def member_hosted_outings(name: str, posts: list[dict]) -> list[dict]:
    """그 사람이 연 출사 공지. **취소(펑) 건도 포함**하고 최신 출사일 순.

    `outings_table`을 필터링하지 않는다 — 거기엔 참석자수가 없다. 누군가의
    출사를 평가할 때 정작 궁금한 것이 "몇 명이나 왔나"인데 그게 빠진다.
    """
    rows = []
    for p in sorted((p for p in posts
                     if p.get("cat") == "A" and p.get("author") == name),
                    key=lambda x: (x.get("outing_date") or "0000",
                                   x["posted_at"]), reverse=True):
        rows.append({
            "출사일": p.get("outing_date") or "-",
            "카테고리": p.get("category") or "-",
            "상태": "취소" if p.get("is_canceled") else
                    ("진행" if p.get("actually_held") else "후기 없음"),
            "제목": p.get("title") or "",
            "참석자수": len(p.get("attendees") or []),
            "좋아요": p.get("likes", 0),
            "댓글": p.get("comments", 0),
        })
    return rows


def member_attended_outings(name: str, posts: list[dict]) -> list[dict]:
    """그 사람이 참석한 출사. 매칭된(actually_held) 출사만.

    **`attendees_table`의 행을 걸러 만들지 않는다.** 그 표는 참석자를 한
    문자열로 이어 붙여 두어서 `"나무" in row["참석자"]`가 `나무늘보`만 참석한
    출사까지 집어 온다. 명단은 리스트로 있을 때 정확히 비교해야 한다.
    """
    rows = []
    for p in sorted((p for p in posts
                     if p.get("cat") == "A" and p.get("actually_held")
                     and name in (p.get("attendees") or [])),
                    key=lambda x: x.get("outing_date") or "0000", reverse=True):
        rows.append({
            "출사일": p.get("outing_date") or "-",
            "카테고리": p.get("category") or "-",
            "공지자": p.get("author") or "-",
            "제목": p.get("title") or "",
            "참석자수": len(p.get("attendees") or []),
        })
    return rows


def member_reviews(name: str, posts: list[dict],
                   body_cut: int | None = None) -> list[dict]:
    """그 사람이 쓴 후기. 참석 추적의 근거가 되는 글이다.

    본문이 잘린 글은 ✂️로 표시한다 — 그 후기에서 뽑아낸 참석자 명단이 전부가
    아닐 수 있다는 뜻이라, 목록을 훑을 때 바로 보여야 한다.
    """
    rows = []
    for p in sorted((p for p in posts
                     if p.get("cat") == "E" and p.get("author") == name),
                    key=lambda x: x["posted_at"], reverse=True):
        rows.append({
            "작성일": p["posted_at"].strftime("%Y-%m-%d"),
            "제목": p.get("title") or "",
            "참석자수": len(p.get("attendees") or []),
            "매칭": "✓" if p.get("matched_outing_id") else "⚠️ 없음",
            "잘림": "✂️" if _is_truncated(p, body_cut) else "",
            "좋아요": p.get("likes", 0),
        })
    return rows


def _active_months(joined, months: list[int] | None) -> int:
    """이 사람이 **다닐 수 있었던 개월 수**. 가입 전 기간은 빼야 공평하다."""
    if not months:
        return 1
    start = months[0]
    if joined:
        start = max(start, ym_of(joined))
    end = months[-1]
    return max(1, (end // 100 - start // 100) * 12 + (end % 100 - start % 100) + 1)


def _days_to_first(joined, first_iso: str | None) -> int | None:
    """가입일 → 첫 참석까지 걸린 날. 둘 중 하나라도 없으면 잴 수 없다."""
    if not joined or not first_iso:
        return None
    return (date.fromisoformat(first_iso) - joined.date()).days


def member_profile(name: str, posts: list[dict], photos: list[dict],
                   members: list[dict], ctx: dict | None = None) -> dict:
    """한 사람의 모든 스칼라. 화면 맨 위 배지·KPI가 여기서 나온다.

    **참석률의 분모는 매칭된 출사뿐이다.** 후기가 없는 출사는 누가 갔는지 알
    방법이 없어서, 분모에 넣으면 아무 잘못 없이 모두의 참석률이 낮아진다.

    **휴면 판정은 `dormant_members`를 그대로 쓴다.** 여기서 다시 계산하면 🧑‍🤝‍🧑
    멤버 탭의 "최근 조용해진 멤버"와 기준이 어긋나, 같은 사람이 한 화면에선
    휴면이고 다른 화면에선 아닌 상태가 된다.

    **순위는 공동 등수다**(`competition_rank`). 목록 위치를 등수로 쓰면 같은
    횟수인 사람들이 서로 다른 등수를 받는다.

    `ctx`는 `club_context`를 넘기는 통로다 — 전 멤버를 훑을 때 공통 집계를
    쉰 번 다시 돌지 않기 위한 것. 안 넘기면 스스로 만든다.
    """
    ctx = ctx or club_context(posts, photos, members)
    m = next((x for x in members or [] if x.get("mn") == name), {})
    my_posts = [p for p in posts if p.get("author") == name]
    # **가입인사는 활동이 아니다** — 이유는 `activity_authors`에 적어 두었다.
    # 🧑‍🤝‍🧑 멤버 탭도 같은 규칙을 쓴다(갈라지면 같은 사람이 탭마다 달라진다).
    my_real = [p for p in my_posts if p.get("cat") != "J"]
    my_photos = [p for p in photos if p.get("author") == name]

    held = [p for p in posts if p.get("cat") == "A" and p.get("actually_held")]
    attended = [p for p in held if name in (p.get("attendees") or [])]

    hosted = [p for p in my_posts if p.get("cat") == "A"]
    canceled = [p for p in hosted if p.get("is_canceled")]
    ran = [p for p in hosted if not p.get("is_canceled")]
    reviews = [p for p in my_posts if p.get("cat") == "E"]

    # **"이 출사에 후기가 있나"가 아니라 "그 후기를 이 사람이 썼나"** 를 센다 —
    # 후기를 반드시 개최자가 쓰는 것은 아니다(매칭은 작성자 일치에 가산점만 준다).
    #
    # 분모에서 두 가지를 뺀다.
    #   - **펑**: 취소된 출사는 애초에 후기를 쓸 일이 없다.
    #   - **아직 안 다녀온 출사**: 다음 주 출사에 후기가 없는 것은 당연한데,
    #     분모에 넣으면 **공지를 미리 올리는 사람일수록 후기율이 떨어진다.**
    #
    # 출사일을 모르는 공지는 남긴다 — 미래라고 볼 근거가 없다.
    today = (ctx.get("오늘") or date.today()).isoformat()
    reviewable = [p for p in ran
                  if not p.get("outing_date") or p["outing_date"] <= today]
    self_reviewed = sum(
        1 for p in reviewable
        if ctx["후기저자"].get(p.get("matched_review_id")) == name)

    themed = [p for p in my_photos if p.get("has_comment")]
    likes = sum(p.get("likes", 0) for p in my_photos)

    # 순위는 전체에서의 자리 — "12회 참석"만으로는 그게 많은 건지 모른다.
    ranks = {k: competition_rank(name, ctx[k])
             for k in ("참석", "개최", "사진", "테마", "좋아요")}
    out = {
        "이름": name,
        "운영진": bool(m.get("is_admin")),
        "OS": m.get("os") or "—",
        "가입일": m["joined_at"].strftime("%Y-%m-%d") if m.get("joined_at") else "—",
        "마지막 방문": (m["last_visit"].strftime("%Y-%m-%d")
                    if m.get("last_visit") else "—"),
        "첫 등장": ctx["첫등장"].get(name, "—"),
        "최근 참석": ctx["최근"].get(name, "—"),
        "참석": len(attended),
        "매칭 출사": len(held),
        "참석률": _pct(len(attended), len(held)),
        "개최": len(hosted), "개최 취소": len(canceled), "개최 진행": len(ran),
        "취소율": _pct(len(canceled), len(hosted)),
        "후기": len(reviews),
        # 분모를 함께 싣는다 — 화면 문구와 칭호가 **같은 수**를 쓰게 하려는
        # 것이다. `개최 진행`으로 나누면 아직 안 다녀온 출사가 섞인다.
        "자기 출사 후기 분모": len(reviewable),
        "자기 출사 후기": self_reviewed,
        "자기 출사 후기율": _pct(self_reviewed, len(reviewable)),
        "게시글 좋아요": sum(p.get("likes", 0) for p in my_posts),
        "사진": len(my_photos),
        "사진 좋아요": likes,
        "장당 좋아요": round(likes / len(my_photos), 1) if my_photos else 0.0,
        "테마사진": len(themed),
        "테마 참여월": len({ym_of(p["posted_at"]) for p in themed}),
        "동행자": dict(ctx["동행자"]).get(name, 0),
        # `유령`은 **활동 0건이라는 사실 그대로** 둔다. 갓 가입한 사람을 여기서
        # 빼면 칭호가 `유령 회원`과 `아직 첫 출사 전`을 가를 근거를 잃는다.
        # 화면에 "유령"이라 쓸지는 `신입`을 함께 보고 정한다.
        "유령": not my_real and not my_photos and not attended,
        "신입": joined_recently(m.get("joined_at"), ctx.get("_축") or []),
        "휴면": name in ctx["휴면"],
        # 아래 둘은 칭호(`감노 때부터 계셨네`·`가입하자마자 출동`)가 쓴다.
        # `가입일`은 표시용 문자열이라 날짜 비교에 쓸 수 없어 원본을 함께 둔다.
        "_가입": m.get("joined_at"),
        "활동 개월": _active_months(m.get("joined_at"), ctx.get("_축")),
        "가입→첫 참석": _days_to_first(m.get("joined_at"), ctx["첫등장"].get(name)),
    }
    for k, (rank, total) in ranks.items():
        out[f"{k} 순위"], out[f"{k} 모수"] = rank, total
    return out


# ═══════════════════════════════════════════════════════════════
# 🏆 칭호 — 숫자만 있고 사람이 없던 화면에 "나는 이런 사람"을 붙인다
#
# 세 가지가 설계의 전부다.
#
# **① 기준은 상대값이다.** `테마사진 3장 이상` 같은 고정값은 모임 활동량에
# 따라 아무도 못 받거나 전원이 받는다. 분위(`top_share`)로 재면 어떤 기간을
# 봐도 일정 비율이 받는다. 고정값은 "이 밑으로는 아무리 상위여도 안 준다"는
# 최소 바닥으로만 쓴다.
#
# **② 지표당 하나만 남긴다.** 안 그러면 사진을 많이 올리는 사람이 `다작왕`·
# `좋아요 수집가`·`부지런한 업로더` 세 개로 세 칸을 다 채워, 칭호 셋이 전부
# 같은 얘기를 한다. 지표로 묶어 하나만 남기면 **사진 1개 + 다른 지표 2개**가
# 되어 훨씬 그 사람다워진다.
#
# **③ 칭호마다 정원이 있다.** 실제 데이터에서 `인풍 애호가`가 94명 중 20명,
# `서동훈만 따라다녀`가 17명에게 붙었다. 스무 명이 받는 것은 칭호가 아니라
# 그냥 그 모임의 평균이다. 조건을 조여도 실제 몇 명이 될지는 돌려 봐야 알기
# 때문에, 넘치면 **강한 순으로 자르는** 정원을 함께 둔다.
# ═══════════════════════════════════════════════════════════════

TITLE_LIMIT = 3                 # 한 사람에게 붙는 최대 개수
TITLE_QUOTA_DEFAULT = 9         # 한 칭호를 받을 수 있는 최대 인원
TITLE_QUOTA = {"관계": 5, "카테고리": 5, "유일": 1}    # 갈래별 정원

# ── 인연 칭호(우연 대비) ────────────────────────────────────────
# `동행`이 **함께 간 횟수와 비율**로 짝을 고르는 데 반해, `인연`은 **우연히
# 겹칠 양을 빼고 남는 것**으로 고른다. 실제 데이터에서 두 지표의 1위는
# 대부분 다른 사람이다 — 서동훈↔엄태진은 69회를 함께 다녔지만 둘 다 전체의
# 절반쯤 나와서 우연 기대치가 58.5회, 겨우 1.18배다. 반대로 김준영↔김희규는
# 6회뿐이지만 우연의 11.5배다. 그래서 두 지표를 **따로** 둔다.
PAIR_MIN_JOINT = 3      # 이보다 적게 만난 쌍은 우연과 구분되지 않는다
PAIR_MIN_LIFT = 1.4     # lift **하한**의 문턱(생 lift가 아니다)
BOND_MIN_SHARE = 50     # 짝사랑 쪽에 요구하는 내 출사 비중

# `아맞다후기`를 받을 수 있는 후기율의 위 끝. 후기는 늘 쓰는 것이 맞으니
# 여기까지가 "낮다"고 부를 수 있는 선이고, 그보다 잘 쓰면 아무도 안 받는다.
AWOL_REVIEW_MAX = 80

# 구-감노 시절의 끝. 이 앞에 가입해 지금까지 남아 있는 사람을 가려낸다.
OLD_CLUB_UNTIL = date(2025, 6, 1)

# 카테고리 쏠림 칭호의 이름. 없는 카테고리는 `{이름} 마니아`로 떨어진다.
CATEGORY_TITLES = {
    "풍경": "풍경 사냥꾼", "인물": "인물 전문", "인물&풍경": "인풍 애호가",
    "GN": "GN 마니아", "보정": "모여서 보정하실 분?", "문화": "문화?시민",
}


def club_titles(posts: list[dict], photos: list[dict], members: list[dict],
                months: list[int], ctx: dict | None = None) -> dict[str, list[dict]]:
    """전 멤버의 최종 칭호 `{이름: [칭호…]}`.

    **한 사람만 따로 낼 수 없다.** 같은 칭호를 몇 명이 받는지 알아야 정원을
    적용할 수 있기 때문이다. 화면은 이 결과에서 자기 것을 꺼내 쓴다.

    순서가 중요하다:

    1. 전원의 **후보**를 만든다(`_title_candidates`)
    2. 사람별로 지표당 하나만 남기고 우선순위 상위 `TITLE_LIMIT`개를 뽑는다
    3. **화면에 뜬 것**을 세어 정원을 넘긴 칭호는 약한 쪽을 뺀다
    4. 빠진 사람은 다음 후보로 다시 뽑는다 — 넘치지 않을 때까지 반복

    정원을 후보 단계에서만 세면 **화면에 안 뜰 사람이 자리를 잡아먹는다.**

    비용은 멤버 94명·사진 1만 장에서 0.1초대다 — `ctx`가 공통 집계를 들고
    있어서 사람마다 다시 도는 것이 거의 없다.
    """
    ctx = ctx or club_context(posts, photos, members, months)
    pair = ctx["쌍"]

    cand: dict[str, list[dict]] = {}
    for m in members or []:
        n = m.get("mn")
        if not n:
            continue
        prof = member_profile(n, posts, photos, members, ctx)
        cand[n] = _title_candidates(
            n, prof, member_companions(n, posts, pair), posts, photos, months, ctx)

    # ── ②③④ 정원 · 지표당 하나 · 상위 N개를 **함께** 푼다 ────────
    #
    # 정원을 후보 단계에서만 세면 **화면에 안 뜰 사람이 자리를 잡아먹는다.**
    # 실제로 `인풍 애호가`의 유일한 후보가 이미 세 칸이 찬 사람이라 그 칭호가
    # 0명이 됐다. 정원은 **화면에 실제로 뜬 것**을 세야 뜻이 맞는다.
    #
    # 그래서 "뽑고 → 넘치면 약한 쪽을 빼고 → 빠진 사람은 다음 후보로 다시
    # 뽑는" 것을 더 이상 넘치지 않을 때까지 반복한다. 뺀 사람은 계속 쌓이기만
    # 하므로 반드시 멈춘다.
    banned: dict[str, set[str]] = defaultdict(set)

    def pick(n: str) -> list[dict]:
        best: dict[str, dict] = {}
        for t in cand[n]:
            if n in banned[t["칭호"]]:
                continue                     # 정원에 밀린 칭호는 건너뛴다
            cur = best.get(t["지표"])
            if cur is None or t["우선"] > cur["우선"]:
                best[t["지표"]] = t
        return sorted(best.values(), key=lambda t: -t["우선"])[:TITLE_LIMIT]

    while True:
        out = {n: pick(n) for n in cand}
        holders: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for n, ts in out.items():
            for t in ts:
                holders[t["칭호"]].append((t["강도"], n))
        cut = False
        for 칭호, rows in holders.items():
            quota = TITLE_QUOTA.get(_quota_kind(cand, 칭호), TITLE_QUOTA_DEFAULT)
            if len(rows) <= quota:
                continue
            # 강도 내림차순, **동점은 칭호마다 다른 순서로** 끊는다.
            #
            # 이름순으로 끊으면 활동량이 비슷한 사람들 사이에서 **늘 같은
            # 사람이 모든 칭호에서 밀린다** — 가나다순 뒤쪽에 있다는 이유로
            # 한 명은 세 칸이 차고 다른 한 명은 빈손이 된다.
            #
            # `hash()`는 실행마다 값이 달라져 못 쓴다(같은 데이터인데 새로고침할
            # 때마다 칭호가 바뀐다). md5는 어디서 돌려도 같은 값이다.
            rows.sort(key=lambda r: (-r[0], _tiebreak(칭호, r[1])))
            for _, n in rows[quota:]:
                banned[칭호].add(n)
                cut = True
        if not cut:
            return out
    return out


def _tiebreak(칭호: str, name: str) -> str:
    """동점자 순서 — 칭호마다 다르되 같은 데이터면 늘 같다."""
    return hashlib.md5(f"{칭호}\0{name}".encode()).hexdigest()


def _quota_kind(cand: dict[str, list[dict]], 칭호: str) -> str:
    for ts in cand.values():
        for t in ts:
            if t["칭호"] == 칭호:
                return t["갈래"]
    return "일반"


def _title_candidates(name: str, prof: dict, companions: list[dict],
                      posts: list[dict], photos: list[dict], months: list[int],
                      ctx: dict | None = None) -> list[dict]:
    """정원을 적용하기 **전**의 후보 전부. 최종 목록은 `club_titles`가 낸다.

    각 칭호는 **근거 한 줄**을 함께 낸다. 이름만 붙으면 왜 붙었는지 물어볼
    데가 없고, 잘못 붙었을 때 알아챌 수도 없다.

    `강도`는 정원을 넘쳤을 때 누구를 남길지 정하는 값이다 — 그 칭호가 재는
    바로 그 숫자를 쓴다(참석 수, 쏠림 비율, 가입일이 이른 정도 …).
    """
    # ctx가 없으면 만든다. 멤버 명단이 비어도 여기서 쓰는 값은 안 달라진다
    # (휴면은 `prof`에서 본다).
    ctx = ctx or club_context(posts, photos, [])
    # 후보는 **겹쳐도 된다.** 같은 지표에서 여럿이 걸리면 `club_titles`가 우선
    # 순위로 하나만 고르는데, **정원에 밀려 위쪽이 잘리면 아래쪽으로 떨어진다.**
    # elif로 묶으면 그 대체가 안 일어나 그 사람의 그 지표가 통째로 빈다.
    add: list[dict] = []

    def put(지표, 우선, 아이콘, 칭호, 근거, 강도=0.0, 갈래="일반"):
        add.append({"지표": 지표, "우선": 우선, "아이콘": 아이콘, "칭호": 칭호,
                    "근거": 근거, "강도": float(강도), "갈래": 갈래})

    # 유령이면 여기서 끝. 활동 0건인 사람에게 다른 칭호가 걸릴 일도 없지만,
    # 규칙으로 못 박아 둔다.
    if prof["유령"]:
        # 가입한 지 한 달도 안 된 사람에게 "유령"은 가혹하다 — 아직 첫 출사가
        # 안 열렸을 수도 있다. 오래 있었는데 0건인 것과는 다른 얘기다.
        # 판정은 `joined_recently` 한 곳에서만 한다(멤버 탭·배지와 같은 규칙).
        if prof["신입"]:
            return [{"지표": "연차", "우선": 52, "아이콘": "🚪", "칭호": "아직 첫 출사 전",
                     "근거": f"{prof['가입일']} 가입 — 아직 첫 출사 전입니다. "
                            "곧 뵙겠습니다",
                     "강도": 0.0, "갈래": "일반"}]
        return [{"지표": "연차", "우선": 50, "아이콘": "👻", "칭호": "유령 회원",
                 "근거": "이 기간에 글·사진·참석이 하나도 없습니다 "
                        "(가입인사는 활동으로 세지 않습니다)",
                 "강도": 0.0, "갈래": "일반"}]

    att, hosted_ran = prof["참석"], prof["개최 진행"]
    n_photo, n_theme = prof["사진"], prof["테마사진"]

    # ── 동행 — **횟수와 비율**로만 잰다 ────────────────────────
    #
    # 이름을 관찰 말투로 둔다. 여기서 말할 수 있는 것은 "많이 겹쳤다"까지이고,
    # 그게 우연인지 아닌지는 아래 `인연`이 따로 말한다. 예전 이름
    # (`환상의 콤비`·`출사가세요?`)은 특별한 인연처럼 읽혀 두 얘기가 섞였다.
    #
    # **사람 이름 뒤에는 늘 `님`을 붙인다.** 높임말이라 읽기 좋기도 하지만,
    # 조사가 붙는 자리를 `님`이 대신 받아 주는 것이 더 크다 — `님`은 받침이
    # 있으므로 뒤따르는 조사가 **이름과 무관하게 언제나 `과`**로 고정된다.
    # 이름을 그대로 쓰면 `엄태진과`/`바다와`를 받침으로 갈라 줘야 하는데,
    # 표시 이름에는 `Bale(이상현)`·`🍭(김민규)`처럼 괄호·영문·이모지가 섞여
    # 마지막 한글 음절을 찾아내는 판정이 따로 필요했다. `님`이 그 문제를
    # 통째로 없앤다.
    top = companions[0] if companions else None
    combo = _combo_partner(companions)
    if combo:
        put("동행", 95, "💞", f"{combo}님과 2인 1조",
            f"{top['함께']}회를 함께 다녔습니다 — 내 출사의 "
            f"{_pctstr(top['내 기준'])}, 상대 출사의 "
            f"{_pctstr(top['상대 기준'])}가 서로 겹칩니다. "
            "한 분이 보이면 다른 한 분도 있는 셈입니다",
            top["함께"], "관계")
    # 콤비가 붙은 상대에게는 일방형을 안 붙인다 — 서로 붙어 다니는 것과
    # 한쪽이 쫓아다니는 것은 다른 얘기라, 같은 상대로 둘 다 붙으면 앞말이
    # 뒷말을 부정한다.
    elif top and att >= 4 and _follows(top, prof):
        put("동행", 88, "🐾", f"{top['함께 간 사람']}님 오늘도 뵙네요",
            f"내 출사 {att}회 중 {top['함께']}회에 이분이 계셨습니다"
            f"({_pctstr(top['내 기준'])}). 이분이 전체 출사에 나오는 비율"
            f"({_pctstr(_pct(top['상대 참석'], prof['매칭 출사']))})보다 "
            "훨씬 자주 만나셨네요",
            top["내 기준"], "관계")

    # ── 인연 — **우연히 겹칠 양을 뺀 뒤** 남는 것 ───────────────
    #
    # `동행`과 자리를 다투지 않도록 지표를 따로 판다. 한 사람이 두 지표에서
    # **서로 다른 사람**을 받을 수 있고, 그게 오히려 이 칭호의 핵심이다.
    me = (ctx.get("인연1위") or {}).get(name)
    # 콤비와 상대가 같으면 인연은 **아무 말도 안 한다.** 여기서 끊어야지
    # 상호형 조건에만 붙이면 조건을 못 지난 사람이 아래 짝사랑 가지로
    # 흘러내려 "정작 이분의 1순위는 따로 있고요"라고 하는데, 서로가 서로를
    # 1순위로 꼽는 쌍이므로 그 말은 거짓이 된다.
    if me and combo == me["상대"]:
        me = None
    if me and me["하한"] >= PAIR_MIN_LIFT:
        저쪽 = (ctx.get("인연1위") or {}).get(me["상대"])
        if 저쪽 and 저쪽["상대"] == name:
            put("인연", 87, "💞", f"{me['상대']}님과 짜고 나오시나요?",
                f"같은 출사에 {me['함께']}회 함께했습니다. {_chance_phrase(me)}했고, "
                "두 분 다 서로를 1순위로 꼽습니다",
                me["하한"], "관계")
        elif me["내비율"] >= BOND_MIN_SHARE:
            put("인연", 84, "🐾", f"{me['상대']}님 알림 켜두셨죠?",
                f"내 출사 {att}회 중 {me['함께']}회가 이분과 함께"
                f"({_pctstr(me['내비율'])}). {_chance_phrase(me)}했습니다 — "
                "정작 이분의 1순위는 따로 있고요",
                me["하한"], "관계")
    if top_share(name, ctx["동행자"], 0.20, min_value=4):
        put("동행", 80, "🕸", "다 아는 사람들 이구먼",
            f"함께 출사해 본 사람이 {prof['동행자']}명 — 모임에서 아는 얼굴이 "
            "가장 많은 20% 안에 듭니다", prof["동행자"])
    if att >= 3 and len(ctx["동행자"]) >= 4 and (
            # 아무와도 안 겹친 사람은 `동행자` 목록에 아예 없다 — 분위로만 재면
            # 가장 혼자인 사람이 빠져나간다.
            prof["동행자"] == 0 or bottom_share(name, ctx["동행자"], 0.25)):
        put("동행", 62, "🕊", "저 신입 아닌데요",
            f"참석 {att}회에 함께 간 사람 {prof['동행자']}명 — 모임에서 동행이 "
            "가장 적은 25%입니다", -prof["동행자"])

    # ── 테마 · 개최 · 참석 · 사진 · 좋아요 (1등 → 상위 분위) ──
    def tier(지표, 위: tuple, 아래: tuple, *, 바닥: bool, 사실: str,
             순서말: str, 강도: float, share: float):
        """1등이면 윗 칭호, 아니면 상위 `share`에 아랫 칭호. 둘 다 `바닥`을 넘어야.

        `위`·`아래`는 `(우선, 아이콘, 이름)`.

        **근거는 두 갈래로 갈라 쓴다.** 1등과 "상위 15%"는 전혀 다른 얘기인데
        같은 문구를 쓰면 무엇으로 받았는지 알 수 없다. `사실`(그 사람의 숫자)
        뒤에 어느 기준으로 걸렸는지를 붙인다.
        """
        if not 바닥:
            return
        # 1등도 모수를 함께 본다 — 두 사람뿐인 판의 1등은 1등이 아니다.
        if prof[f"{지표} 순위"] == 1 and len(ctx[지표]) >= 4:
            put(지표, *위, f"{사실}. 모임에서 가장 {순서말} 분입니다", 강도)
        elif top_share(name, ctx[지표], share, min_value=0):
            put(지표, *아래,
                f"{사실}. {순서말} 순으로 상위 {round(share * 100)}% 안입니다", 강도)

    tier("테마", (90, "🎨", "테마사진의 제왕"), (78, "🖌", "테마사진 프로 참석러"),
         바닥=n_theme >= 2, 강도=n_theme, share=0.15, 순서말="많이 낸",
         사실=f"테마사진 {n_theme}장을 {prof['테마 참여월']}개월에 걸쳐 냈습니다")
    tier("개최", (90, "📢", "출사장도 장이다"), (77, "🗣", "심심한데 출사쳐야지"),
         바닥=hosted_ran >= 2, 강도=hosted_ran, share=0.30, 순서말="많이 연",
         사실=f"펑이 아닌 출사를 {hosted_ran}건 열었습니다")
    tier("참석", (90, "🥾", "이게 본업이에요"), (76, "🔥", "프로 참석러"),
         바닥=att >= 3, 강도=att, share=0.15, 순서말="많이 나온",
         사실=f"참석 {att}회 — 후기가 매칭된 출사의 "
              f"{_pctstr(prof['참석률'])}에 나왔습니다")
    tier("사진", (85, "📸", "여기 제 인스타인데.."), (75, "🖼", "부지런한 업로더"),
         바닥=n_photo >= 5, 강도=n_photo, share=0.30, 순서말="많이 올린",
         사실=f"사진 {n_photo}장 · 받은 좋아요 {prof['사진 좋아요']}")
    tier("좋아요", (85, "❤️", "사진 좋아요 1위"), (74, "💗", "느좋 사진러"),
         바닥=n_photo >= LIKE_RANK_MIN_PHOTOS, 강도=prof["장당 좋아요"], share=0.30,
         순서말="장당 좋아요가 높은",
         사실=f"{n_photo}장을 올려 장당 좋아요 {prof['장당 좋아요']}를 받았습니다")

    # ── 후기 (자기 출사 후기율의 양 끝) ──────────────────────
    # 분모는 `개최 진행`이 아니라 **후기를 쓸 수 있었던 출사**다(펑과 아직 안
    # 다녀온 출사를 뺀 것). 근거에도 그 수를 적어야 조건과 문구가 안 갈라진다.
    셀만한개최 = prof["자기 출사 후기 분모"]
    # 개최 2건이면 "둘 다 내가 썼다"가 너무 흔하다(실제로 15명이 받았다).
    if 셀만한개최 >= 5 and prof["자기 출사 후기율"] >= 100:
        put("후기", 80, "✍️", "책임감 100만점",
            f"본인이 연 출사 {셀만한개최}건에 **전부** 후기를 썼습니다 (100%)",
            셀만한개최)
    # 후기를 한 건도 안 쓰는 사람은 실제로 없었다(수령자 0명). 그래서 **가장
    # 낮은 한 명**을 부르되, 80%를 넘게 쓰는 사람에게 `아맞다후기`는 틀린
    # 말이라 거기서 끊는다 — 다들 잘 쓰는 해에는 아무도 안 받는다.
    #
    # "한 명"은 갈래 `유일`(정원 1)이 만든다. 강도가 후기율의 반대라 제일 낮은
    # 사람이 제일 강해, 정원이 나머지를 잘라 낸다.
    if 셀만한개최 >= 3 and prof["자기 출사 후기율"] <= AWOL_REVIEW_MAX:
        n_wrote = prof["자기 출사 후기"]
        put("후기", 64, "🙈", "아맞다후기",
            (f"출사를 {셀만한개최}건 열었는데 본인이 쓴 후기는 한 건도 없습니다 — "
             "후기는 다른 분들이 써 주셨네요") if not n_wrote else
            (f"연 출사 {셀만한개최}건 중 본인이 후기를 쓴 것은 {n_wrote}건입니다"
             f"({_pctstr(prof['자기 출사 후기율'])}) — 모임에서 가장 낮습니다"),
            -prof["자기 출사 후기율"], "유일")

    # ── 속도 (날짜 간격) ────────────────────────────────────
    gap = _review_lag(name, posts, ctx)
    if gap is not None and gap[1] >= 3 and gap[0] <= 1:
        put("속도", 82, "🚀", "후기는 따끈할때",
            f"후기 {gap[1]}건을 출사일로부터 평균 {gap[0]}일 만에 올렸습니다 — "
            "다녀온 다음 날이면 올라옵니다", -gap[0])
    flash = _flash_ratio(name, posts)
    if flash is not None and flash[1] >= 3 and flash[0] >= 50:
        put("속도", 81, "⚡", "내일 출사가실분?",
            f"연 출사 {flash[1]}건 중 {_pctstr(flash[0])}가 공지한 지 이틀 안에 출발 — "
            "번개를 자주 여십니다", flash[0])

    # ── 규모 (참석한 출사 인원의 **양 끝**) ─────────────────
    # 같은 자를 양쪽에서 읽는다 — 늘 북적이는 자리만 가는 사람과 조용한
    # 자리만 고르는 사람. 정의상 한 사람에게 둘 다 붙지 않는다.
    if att >= 4:
        mine = dict(ctx["참석인원"]).get(name)
        if mine is not None and top_share(name, ctx["참석인원"], 0.15, min_value=0):
            put("규모", 83, "🎪", "정출킬러",
                f"참석한 출사의 평균 인원이 {mine:.1f}명 — 북적이는 자리만 골라 "
                "가는 상위 15%입니다", mine)
        if mine is not None and bottom_share(name, ctx["참석인원"], 0.15):
            put("규모", 69, "🤏", "소수정예",
                f"참석한 출사의 평균 인원이 {mine:.1f}명 — 조용한 자리만 골라 "
                "가는 하위 15%입니다", -mine)

    # ── 균형 (참석 대 사진의 엇갈림) ────────────────────────
    if att >= 3 and n_photo <= 2 and top_share(name, ctx["참석"], 0.30, min_value=0):
        put("균형", 67, "👀", "소모임에요? 글쎄..",
            f"참석은 {att}회(상위 30%)인데 올린 사진은 {n_photo}장 — 부지런히 "
            "다니시면서 사진은 거의 안 올리십니다", att)
    if att <= 2 and n_photo >= 5 and top_share(name, ctx["사진"], 0.30, min_value=0):
        put("균형", 66, "🖨", "제가 사진이 좀 많아요",
            f"사진은 {n_photo}장(상위 30%)인데 참석은 {att}회 — 출사보다 "
            "사진으로 만나는 분입니다", n_photo)

    # ── 종합 (네 가지를 다 하는 사람) ───────────────────────
    # 1등은 하나도 없는데 참석·개최·후기·사진을 **전부** 하는 사람이 있다.
    # 대부분은 한두 가지만 한다 — 실제로 넷 다 하는 사람은 94명 중 24명뿐이다.
    # 강도는 **가장 약한 축이 바닥의 몇 배인가** — "골고루"니까 제일 처지는
    # 쪽으로 잰다.
    floors = ((att, 5), (hosted_ran, 2), (prof["후기"], 2), (n_photo, 5))
    if all(v >= f for v, f in floors):
        put("종합", 76, "🎭", "틈틈이 골고루",
            f"참석 {att} · 개최 {hosted_ran} · 후기 {prof['후기']} · 사진 {n_photo} — "
            "1등은 없어도 네 가지를 **모두** 하십니다. 흔치 않은 조합입니다",
            min(v / f for v, f in floors))

    # ── 밀도 (활동 기간 대비 참석) ──────────────────────────
    # 늦게 합류한 사람은 누적 참석으로는 영원히 위로 못 간다. 가입 이후 몇
    # 달을 다녔는지로 나누면 "짧은 기간에 얼마나 촘촘히 다녔나"가 보인다.
    #
    # 우선순위를 낮게 둔 것은 의도다 — 누적 상위권은 어차피 세 칸이 차서
    # 이 칭호를 표시하지 않고, 그러면 정원이 아래로 흘러 중간층에 닿는다.
    if att >= 5 and top_share(name, ctx.get("밀도") or [], 0.30, min_value=0):
        rate = dict(ctx.get("밀도") or []).get(name, 0)
        put("밀도", 68, "🔥", "짧은 기간에 진심",
            f"가입 후 {prof['활동 개월']}개월 동안 {att}회 — 달마다 {rate:.1f}회꼴로, "
            "활동 기간 대비 상위 30%입니다", rate)

    # ── 습관 (취소율의 양 끝) ───────────────────────────────
    if prof["개최"] >= 5 and prof["개최 취소"] == 0:
        put("습관", 73, "🛡", "펑이 뭐죠?",
            f"출사를 {prof['개최']}건 열면서 펑이 한 번도 없었습니다", prof["개최"])
    if prof["개최"] >= 3 and prof["취소율"] >= 40:
        put("습관", 60, "💥", "그럴만한 이유가...",
            f"연 출사 {prof['개최']}건 중 {prof['개최 취소']}건이 펑 "
            f"({_pctstr(prof['취소율'])}) — 사정이 많으셨나 봅니다", prof["취소율"])

    # ── 성향 ────────────────────────────────────────────────
    pref = ctx["카테고리"].get(name) or Counter()
    total = sum(pref.values())

    # **큰 계열과 작은 계열은 서로 다른 자로 재야 한다.** 셋 중 하나면 붙인다.
    #
    # ① 내 참석의 75%가 그 카테고리 — "이 사람은 인풍만 간다".
    # ② 그 카테고리 출사의 40%에 참석 — "문화 18건 중 9건에 나왔다".
    # ③ 그 카테고리를 **평균의 세 배로** 다닌다 — 배수(쏠림 ÷ 그 계열의 전체
    #    비중). 풍경은 전체의 7.6%뿐이라, 내 참석의 31%가 풍경이면 평균의
    #    네 배다. ①에는 한참 못 미치고 ②도 못 넘지만 분명히 풍경 사람이다.
    #
    # 하나만 쓰면 반대쪽 크기의 계열이 통째로 0명이 된다 — 실제로 ①만 썼을 때
    # 풍경·보정·문화가, ②만 썼을 때 인물&풍경·인물이 0명이었다. ③이 그 사이
    # 크기(19~28건)의 계열을 메운다.
    all_held = sum(ctx["카테고리총계"].values())
    best = None
    for cat, cnt in pref.items():
        held = ctx["카테고리총계"].get(cat, 0)
        own = _pct(cnt, total)
        # 두세 건뿐인 카테고리는 한 번만 나와도 비율이 튄다.
        ratio = _pct(cnt, held) if held >= 4 and cnt >= 3 else 0.0
        lift = ((cnt / total) / (held / all_held)
                if held >= 4 and cnt >= 3 and total >= 5 and all_held else 0.0)
        if not ((att >= 6 and own >= 75) or ratio >= 40 or lift >= 3):
            continue
        # 어느 쪽으로 걸렸든 가장 또렷한 숫자를 근거로 보여 준다.
        score = max(own if att >= 6 and own >= 75 else 0, ratio, lift * 10)
        if best is None or score > best[1]:
            best = (cat, score, cnt, held, own, ratio, lift)
    if best:
        cat, score, cnt, held, own, ratio, lift = best
        # **어느 자로 걸렸는지 밝힌다.** 셋이 뜻하는 바가 전혀 달라서, 숫자만
        # 적어 두면 "이 계열을 많이 간다"는 건지 "이 계열이 열리면 꼭 온다"는
        # 건지 알 수 없다.
        if ratio >= 40:
            근거 = (f"{cat} 출사 {held}건 중 {cnt}건에 나왔습니다({_pctstr(ratio)}) — "
                  "이 계열이 열리면 거의 오십니다")
        elif own >= 75:
            근거 = (f"참석 {total}회 중 {cnt}회가 {cat}({_pctstr(own)}) — "
                  "이 계열만 골라 다니십니다")
        else:
            근거 = (f"참석의 {_pctstr(own)}가 {cat}({cnt}회) — 이 계열은 전체 출사의 "
                  f"{_pctstr(_pct(held, all_held))}뿐이라 모임 평균의 {lift:.1f}배입니다")
        put("성향", 73, "🏞", CATEGORY_TITLES.get(cat, f"{cat} 마니아"),
            근거, score, "카테고리")
    elif att >= 6 and pref and len(pref) >= 4:
        cat, cnt = pref.most_common(1)[0]
        share = _pct(cnt, total)
        if share <= 40:
            put("성향", 71, "🌈", "잡식성",
                f"{len(pref)}가지 카테고리를 고루 다니십니다 — 가장 많은 {cat}조차 "
            f"{_pctstr(share)}뿐이라 어느 한쪽으로 쏠리지 않습니다", len(pref))
    # 요일은 성향과 **다른 축**이다 — 무엇을 찍느냐(카테고리)와 언제 가느냐는
    # 같이 나와도 서로 겹치는 말이 아니다. 한 지표로 묶으면 둘 중 하나가
    # 늘 묻힌다.
    if att >= 5:
        wd = _weekday_ratio(name, posts)
        if wd is not None and wd >= 60:
            put("요일", 74, "🗓", "프로 평일러",
                f"참석한 출사의 {_pctstr(wd)}가 평일 — 주말보다 평일에 더 나오십니다", wd)

    # ── 연차 ────────────────────────────────────────────────
    joined = prof.get("_가입")
    if att >= 1 and joined and joined.date() < OLD_CLUB_UNTIL:
        # **가입일로 잰다.** 예전에는 "이 기간에 가장 먼저 나타난 사람"이라
        # 분석 기간을 좁히면 터줏대감이 바뀌었다. 다감노 이전부터 자리를
        # 지켜 온 사람이라는 뜻이 기간에 흔들리면 안 된다.
        put("연차", 79, "🌳", "아이고 어르신",
            f"{joined.strftime('%Y-%m-%d')} 가입 — 다감노 이전, 구-감노 시절부터 "
            "나가지 않고 자리를 지켜 오셨습니다",
            -joined.toordinal())
    if prof["가입→첫 참석"] is not None and 0 <= prof["가입→첫 참석"] <= 7:
        put("연차", 71, "🏃", "첫 출사 못 참지",
            f"가입하고 {prof['가입→첫 참석']}일 만에 첫 출사 — 망설임이 없으셨네요",
            -prof["가입→첫 참석"])
    if att >= 1 and prof["첫 등장"] != "—" and months and _is_newcomer(
            prof["첫 등장"], months):
        put("연차", 70, "🌱", "새싹",
            f"첫 참석이 {prof['첫 등장']} — 최근에 합류하셨습니다", att)
    if att >= 5 and prof["휴면"]:
        # 두 번 나오고 조용해진 사람까지 부르면 열두 명이 된다.
        put("연차", 55, "🌙", "돌아오세요",
            f"{att}회나 나오시다 {prof['최근 참석']}을 끝으로 3개월 넘게 "
            "조용하십니다", att)

    return add


def _combo_partner(companions: list[dict]) -> str | None:
    """`2인 1조` 조건을 통과한 상대 이름. 없으면 `None`.

    `동행` 블록과 `인연` 블록이 **같은 판정을 두 군데 적지 않도록** 한 곳에
    둔다. 조건이 갈리면 같은 상대로 💞가 두 번 붙는다.
    """
    top = companions[0] if companions else None
    if top and top["함께"] >= 3 and top["내 기준"] >= 50 and top["상대 기준"] >= 50:
        return top["함께 간 사람"]
    return None


def _chance_phrase(me: dict) -> str:
    """우연 대비를 **일반인이 읽는 말**로. lift라는 낱말은 안 쓴다.

    `lift 4.5`는 아무 뜻이 없지만 "우연이라면 1.4회쯤 겹쳤을 텐데 5.7배나
    함께"는 읽으면 안다. 기대 겹침이 1회 미만이면 배수를 **빼고** 실제 횟수로
    말한다 — "한 번도 안 겹쳤을 법한데 18.8배나 함께했고"는 앞뒤가 어긋나
    보인다(0.2회의 18.8배가 3회라는 뜻이지만 그렇게 안 읽힌다).
    """
    if me["기대"] >= 1:
        return (f"우연이라면 {me['기대']:.1f}회쯤 겹쳤을 텐데 "
                f"{me['lift']:.1f}배나 함께")
    return ("서로 상관없이 다니셨다면 한 번 겹칠까 말까인데 "
            f"{me['함께']}회나 함께")


def _follows(top: dict, prof: dict) -> bool:
    """`{상대}님 오늘도 뵙네요`를 붙일 만한가 — **상대의 출석률을 기준선으로 뺀다.**

    실제 데이터에서 한 사람에게 17명이 붙었다. 그 사람이 거의 모든 출사에
    나오기 때문인데, 그러면 **누구의 내 기준이든 높게 나온다.** 따라다니는
    것이 아니라 그냥 확률이다. 상대가 전체 출사의 몇 %에 나오는지를 빼고,
    그보다 확실히 높을 때만 "따라다닌다"고 말할 수 있다.
    """
    baseline = _pct(top["상대 참석"], prof["매칭 출사"])
    return top["내 기준"] >= 60 and top["내 기준"] - baseline >= 15


def _review_lag(name: str, posts: list[dict],
                ctx: dict) -> tuple[float, int] | None:
    """(출사일 → 후기 작성일 평균 일수, 잰 후기 수).

    매칭된 공지가 없는 후기는 **간격을 잴 수 없으므로 세지 않는다** — 0으로
    치면 매칭이 안 된 사람이 제일 빨라 보인다.
    """
    gaps = []
    for p in posts:
        if p.get("cat") != "E" or p.get("author") != name:
            continue
        notice = ctx["공지"].get(p.get("matched_outing_id"))
        od = (notice or {}).get("outing_date")
        if not od:
            continue
        gaps.append((p["posted_at"].date() - date.fromisoformat(od)).days)
    return (round(sum(gaps) / len(gaps), 1), len(gaps)) if gaps else None


def _flash_ratio(name: str, posts: list[dict]) -> tuple[float, int] | None:
    """(공지 이틀 안에 출발한 비율, 연 출사 수). 취소는 빼고 센다."""
    gaps = []
    for p in posts:
        if (p.get("cat") != "A" or p.get("author") != name
                or p.get("is_canceled") or not p.get("outing_date")):
            continue
        gaps.append((date.fromisoformat(p["outing_date"])
                     - p["posted_at"].date()).days)
    if not gaps:
        return None
    return _pct(sum(1 for g in gaps if 0 <= g <= 2), len(gaps)), len(gaps)


def _weekday_ratio(name: str, posts: list[dict]) -> float | None:
    """참석한 출사 중 평일 비율. 출사일이 없는 건은 못 센다."""
    days = [date.fromisoformat(p["outing_date"]).weekday()
            for p in posts
            if p.get("cat") == "A" and p.get("actually_held")
            and p.get("outing_date") and name in (p.get("attendees") or [])]
    return _pct(sum(1 for d in days if d < 5), len(days)) if days else None


def _attended_crowd_all(posts: list[dict]) -> dict[str, float]:
    """사람별 **참석한 출사의 평균 인원**. `소수정예`의 모수."""
    sizes: dict[str, list[int]] = defaultdict(list)
    for p in posts:
        if p.get("cat") != "A" or not p.get("actually_held"):
            continue
        names = p.get("attendees") or []
        for n in names:
            sizes[n].append(len(names))
    return {n: sum(v) / len(v) for n, v in sizes.items() if v}


def _is_newcomer(first_iso: str, months: list[int], within: int = 2) -> bool:
    """첫 등장이 **기간 마지막 달** 기준 `within`개월 이내인가.

    오늘 날짜가 아니라 기간 끝을 기준으로 삼는다 — 과거 기간을 들여다보면
    "지금 신입"이 아니라 "그때 신입"이 궁금하기 때문이다.
    """
    ym = int(first_iso[:4]) * 100 + int(first_iso[5:7])
    end = months[-1]
    gap = (end // 100 - ym // 100) * 12 + (end % 100 - ym % 100)
    return 0 <= gap < within


# ═══════════════════════════════════════════════════════════════
# 갤러리 — 올라온 사진 전부에 닿는다
#
# 지금까지는 `top_photos`로 좋아요 상위 12장만 볼 수 있었고 나머지 수천 장은
# 앱에서 볼 방법이 아예 없었다. 사진 한 장을 그리는 것이 곧 CloudFront 요청
# 한 번이라, "전부 보여 준다"는 곧 "한 번에 몇 장을 그릴지 정한다"는 뜻이다.
# ═══════════════════════════════════════════════════════════════

GALLERY_SORTS = ["최신순", "오래된순", "좋아요순", "댓글순"]


def gallery_photos(photos: list[dict], *, author: str | None = None,
                   themed_only: bool = False,
                   sort: str = "최신순") -> list[dict]:
    """필터·정렬을 마친 사진 목록.

    **동률은 id로 끊는다.** 좋아요가 같은 사진이 수두룩한데 정렬이 흔들리면
    페이지를 넘길 때마다 순서가 바뀌어, 같은 사진이 두 페이지에 나오거나 아예
    한 번도 안 나오는 사진이 생긴다.
    """
    sel = [p for p in photos
           if (author is None or p.get("author") == author)
           and (not themed_only or p.get("has_comment"))]
    keys = {
        "최신순":   lambda p: (-p["posted_at"].timestamp(), str(p["id"])),
        "오래된순": lambda p: (p["posted_at"].timestamp(), str(p["id"])),
        "좋아요순": lambda p: (-p.get("likes", 0), -p.get("comments", 0), str(p["id"])),
        "댓글순":   lambda p: (-p.get("comments", 0), -p.get("likes", 0), str(p["id"])),
    }
    return sorted(sel, key=keys.get(sort, keys["최신순"]))


def photos_by_month(photos: list[dict]) -> dict[int, list[dict]]:
    """월(ym)별 묶음. 들어온 순서를 그대로 유지한다(정렬은 호출 전에 끝낸다).

    `themed_photos_by_month`와 합치지 않는다 — 그쪽은 **댓글 달린 사진만**
    담고 **작성자순**으로 정렬한다. 같은 사람 사진을 나란히 놓고 테마인지
    판별하는 화면이라 그 정렬이 목적이기 때문이다. 갤러리는 "무엇이 있나"를
    보는 화면이라 기준이 다르다.
    """
    out: dict[int, list[dict]] = defaultdict(list)
    for p in photos:
        out[ym_of(p["posted_at"])].append(p)
    return dict(out)


def photo_uploaders(photos: list[dict]) -> list[dict]:
    """업로더 필터 옵션 `{작성자, 사진수}`.

    `photo_user_ranking`을 쓰지 않는다 — 그쪽은 활성 멤버만 센다. 갤러리는
    **올라온 사진 전부에 닿는 것**이 목적이라, 나간 사람이 올린 사진도 찾을
    수 있어야 한다.
    """
    cnt = Counter(p.get("author") or "—" for p in photos)
    return [{"작성자": a, "사진수": n} for a, n in cnt.most_common()]


def page_slice(items: list, page: int, per_page: int) -> tuple[list, int, int]:
    """(그 페이지 항목, 전체 페이지 수, 0-based 시작 인덱스).

    범위를 벗어난 `page`는 자른다 — 업로더 필터를 좁히면 페이지 수가 줄어드는데,
    그때 예전 페이지 번호가 남아 있으면 빈 화면이 나온다.
    """
    per_page = max(1, int(per_page))
    pages = max(1, -(-len(items) // per_page))
    page = min(max(1, int(page)), pages)
    start = (page - 1) * per_page
    return items[start:start + per_page], pages, start


# ═══════════════════════════════════════════════════════════════
# Altair 차트
# ═══════════════════════════════════════════════════════════════

def donut(data: dict[str, int], title: str, scheme: str = "tableau10") -> alt.Chart:
    df = pd.DataFrame({"구분": list(data.keys()), "값": list(data.values())})
    return (
        alt.Chart(df)
        .mark_arc(innerRadius=55)
        .encode(
            theta=alt.Theta("값:Q"),
            color=alt.Color("구분:N", scale=alt.Scale(scheme=scheme), legend=alt.Legend(title=None)),
            tooltip=["구분", "값"],
        )
        .properties(title=title, height=260)
    )


def hbar(rows: list[dict], cat_col: str, val_col: str, title: str,
         n: int = 10, scheme: str = "blues") -> alt.Chart:
    df = pd.DataFrame(rows).head(n)
    return (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X(f"{val_col}:Q", title=val_col),
            y=alt.Y(f"{cat_col}:N", sort="-x", title=None),
            color=alt.Color(f"{val_col}:Q", scale=alt.Scale(scheme=scheme), legend=None),
            tooltip=list(df.columns),
        )
        .properties(title=title, height=max(180, 30 * len(df)))
    )


def monthly_trend_chart(monthly: dict[str, dict[int, int]], months: list[int],
                        title: str = "월별 활동 추이") -> alt.Chart:
    labels = axis_labels(months)
    rows = [
        {"월": lab, "구분": label, "건수": counts.get(m, 0)}
        for label, counts in monthly.items()
        for m, lab in zip(months, labels)
    ]
    df = pd.DataFrame(rows)
    return (
        alt.Chart(df)
        .mark_line(point=True)
        .encode(
            x=alt.X("월:O", title="월", sort=labels),
            y=alt.Y("건수:Q", title="건수"),
            color=alt.Color("구분:N", legend=alt.Legend(title=None)),
            tooltip=["월", "구분", "건수"],
        )
        .properties(title=title, height=320)
    )


def stacked_bar(rows: list[dict], x_col: str, y_col: str, color_col: str,
                title: str, *, x_sort: list[str] | None = None,
                scheme: str = "tableau10") -> alt.Chart:
    """월별 누적 막대 — x축은 월, 색은 카테고리처럼 쌓아 올릴 구분값."""
    df = pd.DataFrame(rows)
    return (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X(f"{x_col}:O", title=x_col, sort=x_sort),
            y=alt.Y(f"{y_col}:Q", title=y_col, stack="zero"),
            color=alt.Color(f"{color_col}:N", scale=alt.Scale(scheme=scheme),
                            legend=alt.Legend(title=None)),
            tooltip=[x_col, color_col, y_col],
        )
        .properties(title=title, height=340)
    )


# ═══════════════════════════════════════════════════════════════
# 데이터 세팅 (수집·엑셀 업로드 양쪽에서 호출)
# ═══════════════════════════════════════════════════════════════

def _mark_active(items: list[dict], active_mids: set[str] | None) -> None:
    """각 item에 `is_active`를 in-place로 마킹 — 작성자(wid)가 활성 멤버(mid)인지.

    active_mids가 비어 있으면 모두 True로 두어 멤버 정보 없는 환경(예: 멤버 없이
    재로드한 엑셀)에서도 랭킹·집계가 빈 화면이 되지 않도록 한다(필터 무력화).
    """
    if not active_mids:
        for it in items:
            it["is_active"] = True
        return
    for it in items:
        it["is_active"] = it.get("wid", "") in active_mids



def _auth_ok() -> bool:
    """평문 비밀번호 게이트.

    앱 URL은 공개(`*.streamlit.app`)라 우연한 접근을 막는 최소 장치일 뿐이다 —
    해싱·시도 제한·세션 만료가 없다. 실제 데이터 보호는 구글 드라이브 폴더의
    공유 설정에 달려 있다(보정 시트에 실명이 들어간다).
    secrets에 [auth]가 없으면 게이트를 건너뛴다(로컬 개발 편의).
    """
    try:
        expected = (st.secrets.get("auth") or {}).get("password")
    except Exception:  # noqa: BLE001 — secrets 파일 자체가 없으면 예외
        expected = None
    if not expected:
        return True
    if st.session_state.get("_authed"):
        return True

    st.title("📸 다감노 분석")
    with st.form("auth"):
        pw = st.text_input("비밀번호", type="password")
        if st.form_submit_button("입력", type="primary") :
            if pw == expected:
                st.session_state["_authed"] = True
                st.rerun()
            st.error("비밀번호가 올바르지 않습니다.")
    return False


@st.cache_resource(show_spinner=False)
def _open_stores(_conf_key: str):
    """드라이브 폴더에서 raw·보정 스프레드시트를 열고(없으면 생성) 캐시한다.

    `cache_resource`라 세션마다 파일을 다시 찾지 않는다. `_conf_key`는 secrets가
    바뀌면 캐시를 무효화하기 위한 것.
    """
    conf = _gsheets_conf() or {}
    from core.gsheets import GoogleSheetsStore, SheetsClient, parse_credentials
    from core.store import open_stores

    creds = parse_credentials(conf.get("credentials"))
    drive = GoogleSheetsStore(creds, folder_id=conf.get("folder_id"))
    client = SheetsClient(creds)
    return open_stores(drive, client,
                       raw_file_id=conf.get("raw_file_id") or None,
                       correction_file_id=conf.get("correction_file_id") or None)


def get_stores():
    """(raw_store, correction_store). secrets 미설정이거나 열기 실패면 None.

    `session_state["_stores"]`는 **테스트가 가짜 스토어를 주입하는 통로**로만
    읽는다. 앱이 여기에 쓰지 않는 이유: 세션 상태는 코드가 다시 배포돼도
    비워지지 않아, 옛 모듈에 묶인 스토어 객체가 그대로 살아남는다. 캐싱은
    Streamlit이 관리하는 `@st.cache_resource` 하나로 충분하다.

    실패를 예외로 올리지 않고 `st.error`로 보여 준 뒤 None을 돌려준다:
    Streamlit Cloud는 **잡히지 않은 예외의 메시지를 가리므로**, 그대로 두면
    조치 방법을 적어 둔 안내가 화면에 못 뜨고 로그를 열어야만 보인다.
    """
    if st.session_state.get("_stores") is not None:
        return st.session_state["_stores"]
    conf = _gsheets_conf()
    if not conf:
        return None
    if not conf.get("folder_id"):
        st.error(
            "`[gsheets]`에 **`folder_id`가 없습니다.** 서비스 계정은 자기 드라이브에 "
            "파일을 만들 수 없어, 사용자가 공유해 준 폴더를 반드시 지정해야 합니다.\n\n"
            "`folder_id`가 `[auth]` 같은 다른 섹션 아래로 내려가 있지 않은지도 "
            "확인하세요 — TOML은 바로 위 섹션에 속합니다.", icon="⚙️",
        )
        return None
    try:
        stores = _open_stores(str(sorted(conf.items())))
    except Exception as e:  # noqa: BLE001 — 메시지를 화면에 띄우는 것이 목적
        st.error(str(e), icon="⚠️")
        return None
    return stores


def build_analysis(raw: dict, corrections: dict) -> dict:
    """raw + 보정 → 분석 가능한 상태로 조립 (순수 — 네트워크·st 무관).

    보정이 이름 해소보다 **먼저** 적용된다: 공지 분류가 바뀌면 매칭 결과가
    달라지므로 순서가 중요하다.
    """
    from core.store import (
        apply_corrections, apply_photo_corrections, filter_excluded,
        real_by_nickname, real_name_resolution, resolution_from_corrections,
        truncated_body_length,
    )

    posts = [dict(p) for p in raw.get("posts") or []]
    photos = [dict(p) for p in raw.get("photos") or []]
    members = raw.get("members") or []
    join_aliases = raw.get("join_aliases") or {}

    counts = apply_corrections(posts, corrections)
    posts = filter_excluded(posts)
    # 테마 해제는 **모든 집계보다 먼저**. has_comment가 단일 게이트라 여기서
    # 뒤집으면 KPI·월별 추이·업로더 비율·매트릭스·참여자 순위가 전부 따라온다.
    counts["테마해제"] = apply_photo_corrections(photos, corrections)

    active_mids = {m["mid"] for m in members if m.get("mid")}
    _mark_active(posts, active_mids)
    _mark_active(photos, active_mids)

    master_names = {m["mn"] for m in members if m.get("mn")}
    # 실명 → 닉네임이 가입인사 자동 추출보다 뒤에 온다: 사람이 채운 값이 이긴다.
    member_names = corrections.get("member_names") or {}
    resolution = {**join_aliases,
                  **real_name_resolution(member_names, members),
                  **resolution_from_corrections(corrections)}
    annotate_attendees(posts, master_names, resolution)
    # 보정으로 참석자를 직접 지정한 후기는 annotate가 덮어쓰므로 다시 적용한다.
    apply_corrections(posts, {"attendees": corrections.get("attendees") or {}})
    match_outings_with_reviews(posts)

    return {
        "posts": posts, "photos": photos, "members": members,
        "master": {"names": master_names,
                   "duplicates": find_duplicate_member_names(members)},
        "resolution": resolution,
        "real_names": real_by_nickname(member_names, members),
        "photo_flags": corrections.get("photos") or {},
        "applied": counts,
        "history": raw.get("history") or [],
        # 기간을 자르기 **전** 전체로 잰다 — 한 달만 보면 표본이 모자라
        # 벽이 안 보이고, 보는 달마다 다른 답이 나온다.
        "body_cut": truncated_body_length(posts),
    }


def collected_range(history: list[dict], posts: list[dict],
                    photos: list[dict]) -> tuple[int, int] | None:
    """분석 축이 될 기간 — `_수집이력`의 합집합, 없으면 데이터 실측 범위."""
    months: list[int] = []
    for h in history or []:
        for k in ("시작월", "종료월"):
            try:
                v = int(float(h.get(k)))
            except (TypeError, ValueError):
                continue
            if ym_valid(v):
                months.append(v)
    if not months:
        for p in posts or []:
            m = post_ym(p) or (ym_of(p["posted_at"]) if p.get("posted_at") else None)
            if m:
                months.append(m)
        for p in photos or []:
            if p.get("posted_at"):
                months.append(ym_of(p["posted_at"]))
    return (min(months), max(months)) if months else None


def slice_period(analysis: dict, start_ym: int, end_ym: int) -> tuple[list[dict], list[dict]]:
    """보기 기간으로 좁힌 (posts, photos). 공지는 출사일, 그 외는 작성일 기준."""
    posts = []
    for p in analysis["posts"]:
        m = post_ym(p) if p.get("cat") == "A" else (
            ym_of(p["posted_at"]) if p.get("posted_at") else None)
        if m is None or in_ym_range(m, start_ym, end_ym):
            posts.append(p)
    photos = [p for p in analysis["photos"]
              if p.get("posted_at") and in_ym_range(ym_of(p["posted_at"]), start_ym, end_ym)]
    return posts, photos


# ═══════════════════════════════════════════════════════════════
# 렌더링
# ═══════════════════════════════════════════════════════════════

def render_basis_box(posts: list[dict], photos: list[dict], period_label: str) -> None:
    cov = period_coverage(posts, photos)
    rng = ""
    if cov:
        rng = f" · 실제 데이터 {cov[0].isoformat()} ~ {cov[1].isoformat()}"
    st.info(
        f"**분석 기준** — 대상 기간: {period_label}{rng}\n\n"
        "- **기간 기준**: 출사 공지(공지글)는 *출사일*, 후기·가입인사·사진은 *작성일* 기준\n"
        "- **인기**: 좋아요 수(lc)로 정렬, 댓글 수(rn) 병기\n"
        "- **테마사진 참가**: 댓글이 달린 사진(rn>0)을 테마사진 참여로 간주 — 댓글 내용은 비공개라 *추정*\n"
        "- **취소(펑)**: 제목에 `(펑)`/`[펑]` 포함 · **출사 카테고리**: 인물(1:1인물·1:1인물출사 포함)·인물&풍경·풍경·GN / 활동: 보정·문화",
        icon="ℹ️",
    )

def _ranking_df(rows: list[dict], count_col: str) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if not df.empty:
        df.insert(0, "순위", range(1, len(df) + 1))
    return df


def render_results(start_ym: int, end_ym: int, posts: list[dict],
                   photos: list[dict], master: dict,
                   members: list[dict] | None = None,
                   applied: dict[str, int] | None = None,
                   pending: dict[str, int] | None = None,
                   correction_url: str | None = None,
                   fix_store=None, body_cut: int | None = None) -> None:
    period = period_label(start_ym, end_ym)
    months = month_axis(start_ym, end_ym)
    st.subheader(f"{period} 인사이트")

    kpis = compute_kpis(posts, photos)
    for col, (label, val) in zip(st.columns(len(kpis)), kpis.items()):
        col.metric(label, val)

    if applied and any(applied.values()):
        st.caption(f"📕 보정 시트 적용 — 공지 {applied.get('공지', 0)}건 · "
                   f"참석자 {applied.get('참석자', 0)}건")
    render_basis_box(posts, photos, period)

    duplicates = master.get("duplicates") if isinstance(master, dict) else set()

    # 예전의 10→5 통합은 **서로의 상위집합이던 탭**을 합친 것이었다(`_tab_overview`의
    # "👤 사용자 탭을 흡수", `_tab_photos`의 "같은 사진 데이터를 두 탭에서" 주석).
    # 새로 붙는 둘은 기존 집계의 중복이 아니라 각각 *열람 모드*와 *드릴다운
    # 모드*라 그 규칙에 걸리지 않는다. 자리는 소재 인접 — 갤러리는 사진 옆,
    # 멤버 상세는 멤버 옆.
    tabs = st.tabs(
        ["📊 개요", "📌 출사", "👥 참석 & 후기", "📷 사진", "🖼 갤러리",
         "🧑‍🤝‍🧑 멤버", "🔎 멤버 상세"]
    )

    with tabs[0]:
        _tab_overview(posts, photos, months, pending, correction_url)
    with tabs[1]:
        _tab_outings(posts, months)
    with tabs[2]:
        _tab_attendance(posts, months, body_cut=body_cut)
    with tabs[3]:
        _tab_photos(photos, months, fix_store=fix_store)
    with tabs[4]:
        _gallery_section(photos, months)
    with tabs[5]:
        _tab_members(members or [], posts, photos, duplicates or set(), months,
                     since_ym=start_ym)
    with tabs[6]:
        _tab_member_focus(posts, photos, members or [], months,
                          duplicates=duplicates or set(), body_cut=body_cut)


def render_confidence(posts: list[dict], pending: dict[str, int] | None,
                      correction_url: str | None = None) -> None:
    """이 숫자를 얼마나 믿어도 되는지 — 결과 화면의 맨 앞.

    본문이 잘려 들어오는 탓에 참석자가 비어 있을 수 있다. **진짜 없는 것과
    아직 안 채운 것을 구분할 수 없으면** 결과를 잘못 읽는다.
    """
    rows = confidence_report(posts, pending)
    pending = [r for r in rows if r["건수"]]
    if not pending:
        st.success("보정이 필요한 항목이 없습니다. 아래 숫자를 그대로 보셔도 됩니다.", icon="✅")
        return

    st.warning(
        f"**보정이 필요한 항목 {sum(r['건수'] for r in pending)}건** — "
        "아래 숫자는 이만큼 덜 채워진 상태입니다.", icon="🎯")
    cols = st.columns(min(len(pending), 5))
    for col, r in zip(cols, pending):
        col.metric(r["항목"], r["건수"], help=f"{r['설명']}\n\n→ {r['어디서']}")
    with st.expander("무엇을 어디서 채우면 되나"):
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if correction_url:
            st.link_button("📕 보정 시트 열기", correction_url, width="stretch")


def _tab_overview(posts: list[dict], photos: list[dict], months: list[int],
                  pending: dict[str, int] | None = None,
                  correction_url: str | None = None) -> None:
    render_confidence(posts, pending, correction_url)
    st.divider()

    k = compute_kpis(posts, photos)
    if k["진행 출사"] + k["취소 출사"] > 0:
        # 카테고리 분포 도넛은 🏷️ 카테고리 탭과 완전히 겹쳐 여기서 뺐다.
        st.altair_chart(
            donut({"진행": k["진행 출사"], "취소": k["취소 출사"]}, "출사 공지 진행/취소"),
            width="stretch",
        )
    st.altair_chart(monthly_trend_chart(monthly_table(posts, photos), months), width="stretch")
    st.caption("월별 추이 — 출사는 출사일 기준, 후기·사진·테마사진 참가는 작성일 기준. "
               "출사·사진·테마 각각의 월별 상세는 해당 탭에 있습니다.")

    ex = summary_extras(posts, photos)
    st.markdown("#### 핵심 숫자")
    c = st.columns(4)
    c[0].metric("게시글 좋아요 합", ex["게시글 좋아요"])
    c[1].metric("게시글 댓글 합", ex["게시글 댓글"])
    c[2].metric("사진 좋아요 합", ex["사진 좋아요"])
    c[3].metric("사진 댓글 합", ex["사진 댓글"])
    if ex["top_post_likes"]:
        tp = ex["top_post_likes"]
        st.markdown(f"**최고 인기 게시글 (좋아요 기준)** 👍{tp['likes']} (💬{tp['comments']}) — {tp['author']} · {tp['title']}")
    if ex["top_post_comments"]:
        tc = ex["top_post_comments"]
        st.markdown(f"**최고 인기 게시글 (댓글 기준)** 💬{tc['comments']} (👍{tc['likes']}) — {tc['author']} · {tc['title']}")

    # 👤 사용자 탭을 흡수 — 출사·사진 랭킹의 상위집합이라 탭 하나를 따로 둘 이유가 없었다.
    st.markdown("#### 활동 종합 랭킹")
    st.caption("작성자별 게시글 수(공지+취소+후기)와 업로드한 사진 수. "
               "게시글 수 → 사진 수 순으로 정렬, 좋아요는 게시글 좋아요 합계.")
    rows = activity_ranking(posts, photos)
    if rows:
        st.dataframe(
            _ranking_df(rows, "게시글"),
            hide_index=True, width="stretch", height=360,
            column_config={
                "게시글": st.column_config.ProgressColumn(
                    "게시글", min_value=0, max_value=max(r["게시글"] for r in rows) or 1, format="%d"),
                "사진": st.column_config.ProgressColumn(
                    "사진", min_value=0, max_value=max(r["사진"] for r in rows) or 1, format="%d"),
            },
        )
    else:
        st.info("데이터가 없습니다.")


def _tab_outings(posts: list[dict], months: list[int]) -> None:
    st.markdown("#### 월별 출사 공지 (진행/취소)")
    mt = monthly_table(posts, photos=[])
    mdf = pd.DataFrame(
        {"진행 출사": axis_values(mt["진행 출사"], months),
         "취소 출사": axis_values(mt["취소 출사"], months)},
        index=axis_labels(months),
    )
    st.bar_chart(mdf)
    st.caption("출사일 기준 월별 집계.")

    st.markdown("#### 출사 공지 작성 순위")
    st.caption("작성자별 cat=A 공지 수 (진행+취소). 출사일이 대상 기간에 든 공지만 집계.")
    ranking = outing_user_ranking(posts)
    if ranking:
        st.dataframe(
            _ranking_df(ranking, "합계"),
            hide_index=True, width="stretch",
            column_config={
                "합계": st.column_config.ProgressColumn(
                    "합계", min_value=0, max_value=max(r["합계"] for r in ranking), format="%d"),
                "취소율": st.column_config.NumberColumn("취소율", format="%.1f%%"),
            },
        )
    else:
        st.info("출사 공지가 없습니다.")

    st.markdown("#### 출사당 평균 참석 인원")
    st.caption("모임 규모가 커지는지 줄어드는지. 참석자를 못 뽑은 출사도 분모에 들어가므로, "
               "📊 개요의 보정 대기가 많으면 실제보다 낮게 나옵니다.")
    avg = avg_attendance_trend(posts, months)
    if any(v is not None for v in avg.values()):
        st.line_chart(pd.DataFrame({"평균 참석 인원": [avg[m] for m in months]},
                                   index=axis_labels(months)))
    else:
        st.caption("매칭된 출사가 없습니다.")

    st.markdown("#### 출사 공지 전체 목록")
    rows = outings_table(posts)
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    # 🏷️ 카테고리 탭을 합쳤다 — 카테고리는 결국 출사를 나누는 축이다.
    st.divider()
    st.markdown("### 🏷️ 카테고리")
    _category_section(posts, months)


def _is_truncated(r: dict, body_cut: int | None) -> bool:
    """본문이 잘림 지점에 닿았는지. `body_cut`이 없으면 아무것도 잘리지 않았다."""
    return bool(body_cut) and len(r.get("body") or "") >= int(body_cut)


@st.fragment
def _review_section(posts: list[dict], months: list[int],
                    body_cut: int | None = None) -> None:
    """📝 후기 게시글 목록 — 월별 expander, 각 후기 카드에 정규화된 참석자 명단.

    참석자는 Stage 1에서 적용된 매핑(가입인사 자동 + 사용자 매핑)으로 마스터 닉네임에
    정규화돼 있다 — 동명이인은 한 명으로 합쳐 표시된다는 주의가 있긴 하지만, 이 탭은
    "어느 후기에 누가 적혔는지" 빠르게 훑어보는 용도. 사진은 표시하지 않음.

    **프래그먼트다.** 필터를 켜거나 달을 펼칠 때마다 앱 전체를 다시 그릴 이유가
    없다 — 여기서 바뀌는 것은 이 구역의 화면뿐이다.
    """
    reviews = sorted(
        (p for p in posts if p.get("cat") == "E"),
        key=lambda x: x["posted_at"], reverse=True,
    )
    notice_by_id = {p["id"]: p for p in posts if p.get("cat") == "A"}

    st.info(
        "각 후기에 본문에서 추출·매핑된 참석자가 **마스터 닉네임으로 통일**되어 표시됩니다. "
        "매칭된 출사 공지가 있으면 출사일·카테고리를 함께 보여줍니다. 사진은 🎨 테마사진 탭에서.",
        icon="📝",
    )
    only_cut = False
    if body_cut:
        cut_n = sum(1 for r in reviews if _is_truncated(r, body_cut))
        st.warning(
            f"소모임 목록 API가 본문을 **{body_cut}자**에서 끊어 보냅니다. "
            f"이 기간 후기 {cut_n}건이 그 길이라 **뒤쪽에 적힌 참석자가 통째로 "
            "빠졌을 수 있습니다.** 잘린 후기는 월 이름 옆 ✂️와 카드 위 빨간 "
            "배지로 표시되며, `참석자보정` 시트에 이미 후보로 올라가 있습니다.",
            icon="✂️",
        )
        # 표시를 아무리 키워도 수백 건을 훑는 것보다는 걸러 내는 편이 빠르다.
        only_cut = st.toggle(f"✂️ 잘린 후기만 보기 ({cut_n}건)", key="rev_only_cut")
        if only_cut:
            reviews = [r for r in reviews if _is_truncated(r, body_cut)]
    if not reviews:
        st.caption("잘린 후기가 없습니다." if only_cut else "후기 게시글이 없습니다.")
        return

    by_month: dict[int, list[dict]] = defaultdict(list)
    for r in reviews:
        by_month[ym_of(r["posted_at"])].append(r)

    multi = is_multi_year(months[0], months[-1]) if months else True
    for m in sorted(by_month.keys(), reverse=True):
        items = by_month[m]
        n_cut = sum(1 for r in items if _is_truncated(r, body_cut))
        # 잘린 게 없는 달에는 아무 표시도 붙이지 않는다 — 모든 줄에 붙으면
        # 그건 표시가 아니라 배경이 되고, 찾는 데 도움이 안 된다.
        label = f"{ym_label(m, multi_year=multi)} — 후기 {len(items)}건"
        if n_cut:
            label += f" · ✂️ 잘림 {n_cut}건"
        exp = st.expander(label, icon="✂️" if n_cut else None,
                          key=f"rev_open_{m}", on_change="rerun")
        with exp:
            if not exp.open:
                continue
            for r in items:
                posted = r["posted_at"].strftime("%Y-%m-%d")
                title = r.get("title") or ""
                author = r.get("author") or "—"
                body = r.get("body") or ""
                attendees = r.get("attendees") or []
                blen = len(body)
                truncated = _is_truncated(r, body_cut)
                with st.container(border=True):
                    if truncated:
                        # 카드 맨 위 · 빨강. 회색 캡션 한 조각으로는 쭉 내리며
                        # 훑을 때 눈에 걸리지 않는다.
                        st.badge("✂️ 본문 잘림 — 참석자 확인 필요", color="red")
                    st.markdown(f"**{title}**")
                    meta_bits = [f"🗓 {posted}", f"✍ {author}",
                                 ("✂️ 본문 %d자 (잘림)" if truncated
                                  else "📏 본문 %d자") % blen]
                    cat = r.get("category")
                    if cat:
                        meta_bits.append(f"🏷 {cat}")
                    matched_id = r.get("matched_outing_id")
                    if matched_id and matched_id in notice_by_id:
                        n = notice_by_id[matched_id]
                        od = n.get("outing_date") or "-"
                        ncat = n.get("category") or "-"
                        meta_bits.append(f"📌 {od} ({ncat})")
                    st.caption(" · ".join(meta_bits))
                    if attendees:
                        st.markdown(
                            f"**참석자 ({len(attendees)}명)** — "
                            + ", ".join(attendees)
                        )
                    else:
                        st.markdown("**참석자** — _명단 없음_")
                    if truncated:
                        st.caption(
                            f"✂️ 본문이 {body_cut}자에서 끊겼습니다 — 이 명단이 "
                            "전부라고 볼 수 없습니다. 소모임에서 원문을 확인하고 "
                            "`참석자보정` 시트에 채워 주세요.")
                    if body:
                        st.markdown("**본문**")
                        st.text(body)


def _tab_attendance(posts: list[dict], months: list[int],
                    body_cut: int | None = None) -> None:
    st.caption(
        "후기 본문에 적힌 이름으로 실제 참석자를 추적합니다. 매칭이 안 되면 "
        "**`이름매핑1`에 실명을 채우는 것이 가장 효과가 큽니다** — 남는 것만 "
        "`후기이름매핑`·`참석자보정`에서 처리하세요."
    )

    # 멤버 마스터 수는 🧑‍🤝‍🧑 멤버 탭에 있어 여기서 뺐다.
    rate = real_attendance_rate(posts)
    c1, c2, c3 = st.columns(3)
    c1.metric("출사 공지", rate["공지"])
    c2.metric("후기 매칭", rate["매칭"])
    c3.metric("실제 진행률", f"{rate['진행률']}%")

    st.markdown("#### 멤버별 참석 횟수")
    st.caption(f"`선호 카테고리`는 그 사람이 많이 간 순으로 최대 {PREF_TOP_N}개. "
               f"카테고리가 모두 {len(ALL_CATS)}종이라 사실상 전체 분포이고, "
               "한 사람만 자세히 보려면 🔎 멤버 상세 탭으로 가세요.")
    counts = attendance_counts(posts)
    if counts:
        pref = member_category_pref(posts)
        first, last = member_first_seen(posts)
        for r in counts:
            r["선호 카테고리"] = top_category_label(pref[r["멤버"]])
            r["첫 등장"] = first.get(r["멤버"], "—")
            r["최근"] = last.get(r["멤버"], "—")
        st.dataframe(
            _ranking_df(counts, "참석횟수"),
            hide_index=True, width="stretch",
            column_config={
                "참석횟수": st.column_config.ProgressColumn(
                    "참석횟수", min_value=0,
                    max_value=max(r["참석횟수"] for r in counts) or 1, format="%d"),
                # 5개면 `인물(12), 인물&풍경(9), 풍경(7), GN(3), 보정(1)`까지
                # 들어가 기본 폭에서는 말줄임으로 잘린다.
                "선호 카테고리": st.column_config.TextColumn(
                    "선호 카테고리", width="large"),
            },
        )
    else:
        st.info("매칭된 출사가 없습니다.")

    st.markdown("#### 월별 참석 매트릭스")
    mm, members = attendance_monthly_matrix(posts)
    if members:
        labels = axis_labels(months)
        rows = []
        for name in members[:50]:
            row = {"멤버": name, "합계": sum(mm[name].values())}
            for m, lab in zip(months, labels):
                row[lab] = mm[name].get(m, 0)
            rows.append(row)
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption("매칭된 출사가 아직 없어 매트릭스를 표시할 수 없습니다.")

    st.markdown("#### 출사별 참석자")
    st.caption("취소(펑) 출사는 제외. 공지 제목과 매칭된 후기 제목을 함께 표시해 "
                "매칭 정확도를 검증할 수 있습니다. **⚠️ 후기 없음** 행은 후기가 "
                "작성되지 않은 출사입니다.")
    rows = attendees_table(posts)
    if rows:
        n_missing = sum(1 for r in rows if r["상태"].startswith("⚠️"))
        if n_missing:
            st.warning(f"후기가 작성되지 않은 출사 {n_missing}건 — 표에서 ⚠️로 표시됩니다.")
        df = pd.DataFrame(rows)
        styled = df.style.apply(
            lambda row: ["background-color: #fff4e5" if row["상태"].startswith("⚠️") else ""
                          for _ in row],
            axis=1,
        )
        st.dataframe(styled, hide_index=True, width="stretch", height=400)

    st.markdown("#### 함께 간 사람")
    st.caption(f"같은 출사에 함께 참석한 횟수 상위 {CO_ATTENDANCE_TOP}쌍. "
               "`A 참석`은 그 사람의 전체 참석 횟수, `A 기준`은 그중 상대와 "
               "함께한 비율입니다 — 같은 8회라도 한쪽에겐 대부분, 다른 쪽에겐 "
               "일부일 수 있습니다. **여기는 전체 상위 쌍이라 참석이 적은 "
               "사람은 올라오지 않습니다** — 특정인의 동행 전체는 🔎 멤버 상세 "
               "탭에서 봅니다.")
    pairs = co_attendance(posts)
    if pairs:
        # 횟수는 `회`, 비율은 막대. 두 종류가 같은 숫자 모양이면 무엇을 보고
        # 있는지 알 수 없다 — 예전 표의 실제 문제였다.
        def _ratio(label: str, whose: str):
            return st.column_config.ProgressColumn(
                label, min_value=0, max_value=100, format="%.0f%%",
                help=f"{whose}가 간 전체 출사 중 상대와 함께한 비율")

        st.dataframe(
            pd.DataFrame(pairs, columns=CO_ATTENDANCE_COLS),
            # 40쌍을 320px에 담으면 아홉 줄만 보인다 — 상한을 올린 뜻이 없다.
            hide_index=True, width="stretch", height=520,
            column_config={
                "사람 A": st.column_config.TextColumn("사람 A", width="medium"),
                "사람 B": st.column_config.TextColumn("사람 B", width="medium"),
                "함께": st.column_config.NumberColumn(
                    "함께", format="%d회", help="두 사람이 같은 출사에 함께 간 횟수"),
                "A 참석": st.column_config.NumberColumn(
                    "A 참석", format="%d회", help="사람 A의 전체 참석 횟수"),
                "A 기준": _ratio("A 기준", "사람 A"),
                "B 참석": st.column_config.NumberColumn(
                    "B 참석", format="%d회", help="사람 B의 전체 참석 횟수"),
                "B 기준": _ratio("B 기준", "사람 B"),
            },
        )
    else:
        st.caption("두 명 이상이 참석한 출사가 없습니다.")

    orph = orphan_reviews(posts)
    if orph:
        with st.expander(f"⚠️ 공지와 매칭되지 않은 후기 {len(orph)}건"):
            for r in orph[:30]:
                d = r["posted_at"].strftime("%Y-%m-%d")
                att = ", ".join(r.get("attendees", [])) or "—"
                st.markdown(f"- **{d}** [{r['author']}] {r['title']} · 참석자: {att}")

    # 📝 후기 탭을 합쳤다 — 참석자 표에서 이상한 걸 보면 같은 탭에서 원문을 연다.
    st.divider()
    st.markdown("### 📖 후기 본문 상세 확인")
    _review_section(posts, months, body_cut)


def _tab_photos(photos: list[dict], months: list[int], fix_store=None) -> None:
    st.caption("💬 댓글이 달린 사진(rn>0)을 **테마사진 참여로 추정**합니다 — "
               "댓글 내용이 비공개라 사진 자체로만 판단합니다. 아래 월별 미리보기로 "
               "실제 테마사진인지 직접 확인하세요.")

    st.markdown("#### 사진 업로드 순위")
    st.caption("작성자별 사진 수 · 테마예상 = 댓글 달린(테마사진 참여 추정) 사진 수 · 좋아요 합계.")
    ranking = photo_user_ranking(photos)
    if ranking:
        st.dataframe(
            _ranking_df(ranking, "사진수"),
            hide_index=True, width="stretch",
            column_config={
                "사진수": st.column_config.ProgressColumn(
                    "사진수", min_value=0, max_value=max(r["사진수"] for r in ranking), format="%d"),
                "테마비율": st.column_config.NumberColumn("테마비율", format="%.1f%%"),
            },
        )

    st.markdown("#### 월별 사진 업로드")
    mt = monthly_table(posts=[], photos=photos)
    st.bar_chart(pd.DataFrame({"사진": axis_values(mt["사진"], months),
                               "테마사진 참가": axis_values(mt["테마사진 참가"], months)},
                              index=axis_labels(months)))

    st.markdown("#### 인기 사진 갤러리")
    st.caption("좋아요(lc) 상위 12장 · 👍 좋아요 / 💬 댓글 병기.")
    tops = top_photos(photos, 12)
    if tops:
        for i in range(0, len(tops), 4):
            for col, p in zip(st.columns(4), tops[i:i + 4]):
                col.image(p["url_medium"], width="stretch",
                          caption=f"{p['author']} · 👍{p['likes']} 💬{p['comments']}")
    else:
        st.info("사진이 없습니다.")

    # 🎨 테마사진 탭을 합쳤다 — 같은 사진 데이터를 두 탭에서 갈라 보던 것.
    st.divider()
    st.markdown("### 🎨 테마사진")
    _theme_section(photos, months, fix_store)


_PENDING = "_theme_pending"       # 저장 전 체크박스 상태 {사진 id: 테마아님}


def _theme_key(pid: str) -> str:
    return f"thm_{pid}"


def _collect_pending(all_photos: list[dict], excluded_ids: set) -> dict[str, bool]:
    """체크박스 위젯 상태를 훑어 '저장할 것'을 다시 계산한다.

    위젯 값은 스크립트가 돌기 **전에** 확정된다. 그래서 여기서 한 번에 모으면
    화면 위쪽의 경고 문구와 저장 버튼이 방금 누른 체크를 곧바로 반영한다.
    체크박스를 그리는 자리에서 모으면 그 둘이 **한 박자 늦어** 한 장만 체크한
    상태에서는 저장 버튼이 계속 꺼져 있었다.

    기준은 체크 상태와 **시트에 저장된 값**의 차이다. 같아졌으면 저장할 것이
    없으니 지운다.
    """
    pending = st.session_state.setdefault(_PENDING, {})
    for p in all_photos:
        pid = str(p.get("id"))
        key = _theme_key(pid)
        if key not in st.session_state:
            continue                     # 이번 화면에 안 그려진 사진 — 건드리지 않는다
        now = bool(st.session_state[key])
        if now != (pid in excluded_ids):
            pending[pid] = now
        else:
            pending.pop(pid, None)       # 원래대로 돌아왔으면 저장할 것이 없다
    return pending


def _photo_card(col, p: dict, fix_store, excluded_ids: set,
                pending: dict | None = None) -> None:
    """사진 한 장과 그 체크박스를 **테두리 한 칸**에 함께 담는다.

    격자로 늘어놓으면 사진 사이 간격과 사진·체크박스 사이 간격이 비슷해서
    어느 사진에 대한 체크인지 헷갈린다. 사진 높이가 제각각이라 줄이 어긋나면
    더 그렇다. 한 칸으로 묶으면 그 질문 자체가 없어진다.

    체크박스 초기값은 **저장 전 상태(`pending`)를 먼저** 본다. 닫힌 달은
    그리지 않으므로 그 달의 위젯 상태는 버려지는데, 저장된 값으로만 되살리면
    다시 열었을 때 체크가 풀린 채 나타나고 **변경이 조용히 사라진다.**
    """
    pid = str(p.get("id"))
    saved = pid in excluded_ids
    box = col.container(border=True)
    box.image(p["url_small"], width="stretch")
    if fix_store is not None:
        # 사진 바로 아래 — 캡션보다 위에 둬야 사진에 붙어 보인다.
        box.checkbox("테마 아님", value=(pending or {}).get(pid, saved),
                     key=_theme_key(pid))
    box.caption(f"{p.get('author', '')} · 👍{p.get('likes', 0)} "
                f"💬{p.get('comments', 0)}")


@st.fragment
def _theme_section(photos: list[dict], months: list[int], fix_store=None) -> None:
    """테마사진 확인·보정.

    **프래그먼트다.** 체크박스 하나 누를 때마다 앱 전체를 다시 그리면 수천 장
    이름 다시 붙이고 다섯 탭을 통째로 재계산해서, 이어서 체크할 수가 없었다.
    프래그먼트로 두면 체크는 이 구역만 다시 그린다. 전체 갱신은 **저장했을
    때만** — 그때는 통계가 실제로 바뀌므로 앱 전체를 다시 돌린다.
    """
    all_photos = st.session_state.get("_all_photos") or photos
    excluded_ids = st.session_state.get("_theme_excluded") or set()
    pending = _collect_pending(all_photos, excluded_ids)

    _, _, mon_count, mon_list = theme_matrix(photos, months)
    by_month = themed_photos_by_month(photos)
    multi = is_multi_year(months[0], months[-1]) if months else True

    st.markdown("#### 월별 테마사진 제출 인원")
    st.bar_chart(pd.DataFrame({"참여 인원": [mon_count.get(m, 0) for m in months]},
                              index=axis_labels(months)))
    st.caption("각 월을 펼쳐 실제 테마사진인지 확인하고, 아니면 사진 아래 "
               "**테마 아님**에 체크하세요. 체크는 이 구역만 다시 그리니 여러 "
               "장을 이어서 고른 뒤, 아래 **변경 저장**을 한 번 누르면 됩니다.")

    if pending and fix_store is not None:
        st.warning(f"저장하지 않은 변경 {len(pending)}건 — 저장해야 통계에 반영됩니다.",
                   icon="✍️")
    if fix_store is not None:
        c1, c2 = st.columns([1, 4])
        if c1.button("💾 변경 저장", type="primary", disabled=not pending,
                     width="stretch"):
            _save_theme_flags(fix_store, all_photos)
        if c2.button("되돌리기(저장 안 함)", disabled=not pending):
            _discard_theme_flags()

    # 해제한 사진은 **저장 버튼 바로 아래**에 둔다. 잘못 체크한 것을 되돌리는
    # 일은 체크하는 일과 같은 작업인데, 화면 맨 끝에 있으면 월 목록을 다 지나
    # 내려갔다가 저장하러 다시 올라와야 한다. 접힌 상태라 평소엔 한 줄이다.
    hidden = [p for p in all_photos if str(p.get("id")) in excluded_ids]
    if hidden:
        exp = st.expander(f"🚫 테마 아님으로 표시한 사진 {len(hidden)}장",
                          key="thm_open_hidden", on_change="rerun")
        with exp:
            if exp.open:
                st.caption("체크를 풀고 **변경 저장**을 누르면 다시 테마사진으로 셉니다.")
                for i in range(0, len(hidden), 5):
                    for col, p in zip(st.columns(5), hidden[i:i + 5]):
                        _photo_card(col, p, fix_store, excluded_ids, pending)

    open_months = [m for m in months if mon_list.get(m)]
    if not open_months:
        # 예전엔 이 안내가 히트맵의 else 가지에만 있었다. 히트맵을 지우면서
        # 함께 사라지면, 테마사진이 없을 때 화면이 그냥 텅 빈다.
        st.info("이 기간에 테마사진(댓글 달린 사진)이 없습니다.")
    for m in open_months:
        ph = by_month.get(m, [])
        # `key` + `on_change`가 있어야 expander가 **상태를 갖는 위젯**이 된다.
        # 기본값(`on_change="ignore"`)이면 아무것도 기억하지 않아, 체크 한 번에
        # 펼쳐 둔 달이 도로 접혔다 — 연달아 작업할 수가 없었다.
        exp = st.expander(f"{ym_label(m, multi_year=multi)} — "
                          f"참여 {len(mon_list[m])}명 · 테마사진 {len(ph)}장",
                          key=f"thm_open_{m}", on_change="rerun")
        with exp:
            if not exp.open:
                continue        # 닫힌 달은 그리지 않는다 — 리런이 그만큼 가볍다
            st.write("**참여자:** " + ", ".join(mon_list[m]))
            for i in range(0, len(ph), 5):
                for col, p in zip(st.columns(5), ph[i:i + 5]):
                    _photo_card(col, p, fix_store, excluded_ids, pending)

    # 테마 매트릭스(작성자×월 히트맵)는 지웠다 — 세로로 660px까지 늘어나면서
    # 바로 위 월별 목록·아래 참여자 순위와 같은 말을 하고 있었다.
    st.markdown("#### 테마 참여자 순위")
    st.caption("참여월수(여러 달에 걸친 참여) 우선, 동률은 테마사진 수.")
    parts = theme_participant_ranking(photos)
    if parts:
        st.dataframe(_ranking_df(parts, "테마사진"), hide_index=True, width="stretch")


def _discard_theme_flags() -> None:
    """체크를 전부 저장 전 상태로.

    `_theme_pending`만 비우면 안 된다 — 체크박스 **위젯 상태**가 그대로라
    다음 렌더에서 `_collect_pending`이 도로 주워 담는다. 위젯 키를 지우면
    저장된 값으로 다시 초기화된다.
    """
    for pid in list(st.session_state.get(_PENDING) or {}):
        st.session_state.pop(_theme_key(pid), None)
    st.session_state[_PENDING] = {}
    st.rerun()          # 앱 전체 — 저장·되돌리기 두 번만 이렇게 한다


def _save_theme_flags(fix_store, all_photos: list[dict]) -> None:
    pending = st.session_state.get(_PENDING) or {}
    if not pending:
        return
    authors = {str(p.get("id")): p.get("author", "") for p in all_photos}
    try:
        n = fix_store.save_photo_flags(pending, authors)
    except Exception as e:  # noqa: BLE001
        st.error(f"저장하지 못했습니다: {e}")
        return
    st.session_state[_PENDING] = {}
    _rebuild_analysis()
    st.toast(f"{n}건 저장했습니다.", icon="✅")
    st.rerun(scope="app")       # 통계가 바뀌었으니 이때만 앱 전체를 다시 그린다


def _rebuild_analysis() -> None:
    """시트를 다시 읽지 않고 분석만 다시 조립한다.

    raw는 수천 행이라 매번 읽으면 체크 한 번에 몇 초가 걸린다. 세션에 캐시해
    둔 raw로 `build_analysis`만 돌리면 제출 인원·참여자·테마사진 수가 갱신된다.
    """
    raw = st.session_state.get("_raw")
    stores = st.session_state.get("_stores")
    if raw is None or not stores:
        st.session_state.pop("_analysis", None)      # 캐시가 없으면 다시 읽는다
        return
    try:
        corrections = stores[1].load()
    except Exception:  # noqa: BLE001
        st.session_state.pop("_analysis", None)
        return
    analysis = build_analysis(raw, corrections)
    analysis["pending"] = st.session_state.get("_analysis", {}).get("pending", {})
    st.session_state["_analysis"] = analysis


def _photo_grid(items: list[dict]) -> None:
    """인기 사진 갤러리와 **같은 형태**의 격자. 사진 한 장 = CDN 요청 한 번."""
    for i in range(0, len(items), GALLERY_COLS):
        for col, p in zip(st.columns(GALLERY_COLS), items[i:i + GALLERY_COLS]):
            mark = " 🎨" if p.get("has_comment") else ""
            col.image(p["url_medium"], width="stretch",
                      caption=f"{p.get('author', '')}{mark} · "
                              f"👍{p.get('likes', 0)} 💬{p.get('comments', 0)}")


def _gallery_page(items: list[dict], key: str) -> None:
    """한 묶음을 페이지로 끊어 그린다.

    **위젯을 만들기 전에 페이지 번호를 자른다.** 업로더 필터를 좁히면 페이지
    수가 줄어드는데, 세션에 남은 예전 번호는 `number_input`의 범위를 벗어나
    그대로 예외가 된다 — 필터를 거는 평범한 동작에서 앱이 죽는다.
    """
    _, pages, _ = page_slice(items, 1, GALLERY_PAGE)
    if int(st.session_state.get(key, 1) or 1) > pages:
        st.session_state[key] = pages
    page = 1
    if pages > 1:
        page = st.number_input(f"페이지 (1–{pages})", min_value=1, max_value=pages,
                               step=1, key=key)
    rows, _, start = page_slice(items, page, GALLERY_PAGE)
    st.caption(f"{start + 1}–{start + len(rows)} / {len(items)}장")
    _photo_grid(rows)


@st.fragment
def _gallery_section(photos: list[dict], months: list[int]) -> None:
    """🖼 올라온 사진 전부를 열람하는 화면.

    **보기 방식이 둘이다.** 월별 펼치기만 두면 정렬이 달 안에서만 걸려서
    "전 기간 좋아요 1등"을 볼 방법이 아예 없다. 반대로 전체 페이지만 두면
    "작년 3월에 뭘 찍었더라"를 찾을 축이 사라진다. 둘 다 필요하다.

    | 보기 | 하는 일 |
    |---|---|
    | 월별 | 펼친 달만 그린다. 정렬은 그 달 안에서 |
    | 전체 | 달 구분 없이 전 기간에 정렬·필터. 1페이지 첫 장이 곧 전체 1등 |

    **닫힌 달은 아예 그리지 않는다**(`if not exp.open: continue`). 사진 한 장이
    곧 CloudFront 요청 한 번이라 비용은 데이터 양이 아니라 **그린 장 수**에
    비례한다. 🎨 테마사진 구역이 월별 expander만으로 충분했던 것은 거기가
    댓글 달린 일부만 그리기 때문이고, 갤러리는 전부를 그리므로 한 달에도
    수백 장이 들어 있을 수 있다 — 그래서 달 안에서도 페이지로 끊는다.

    **프래그먼트다.** 필터·정렬·페이지 조작이 앱 전체를 다시 그릴 이유가 없다.
    """
    st.caption("이 탭은 위쪽 **분석 기간**을 그대로 따릅니다(기본값이 수집 전 "
               "기간이라 평소엔 올라온 사진 전부입니다). 닫힌 달은 불러오지 "
               f"않고, 한 번에 {GALLERY_PAGE}장씩 받습니다.")

    uploaders = photo_uploaders(photos)
    c1, c2, c3, c4 = st.columns([1, 2, 2, 1])
    mode = c1.radio("보기", ["월별", "전체"], horizontal=True, key="gal_mode",
                    help="전 기간 좋아요·댓글 순위를 보려면 **전체**를 고르세요 — "
                         "월별에서는 정렬이 그 달 안에서만 걸립니다.")
    author = c2.selectbox("업로더", ["전체"] + [u["작성자"] for u in uploaders],
                          key="gal_author",
                          format_func=lambda a: a if a == "전체" else
                          f"{a} ({next(u['사진수'] for u in uploaders if u['작성자'] == a)}장)")
    sort = c3.selectbox("정렬", GALLERY_SORTS, key="gal_sort")
    themed_only = c4.toggle("테마사진만", key="gal_themed")

    sel = gallery_photos(photos, author=None if author == "전체" else author,
                         themed_only=themed_only, sort=sort)
    if not sel:
        st.info("조건에 맞는 사진이 없습니다.")
        return
    st.caption(f"총 {len(sel)}장 · 업로더 {len(uploaders)}명")

    if mode == "전체":
        _gallery_page(sel, "gal_page_all")
        return

    by_month = photos_by_month(sel)
    multi = is_multi_year(months[0], months[-1]) if months else True
    open_months = sorted(by_month, reverse=True)
    # 가장 최근 달 하나만 펼쳐 둔다 — 전부 접힌 탭은 고장 난 것처럼 보이는데,
    # 비용은 한 페이지뿐이다.
    st.session_state.setdefault(f"gal_open_{open_months[0]}", True)
    for m in open_months:
        items = by_month[m]
        # 라벨에 `테마사진`·`— 후기`를 넣지 않는다 — 테스트가 expander를 라벨
        # 부분문자열로 분류하므로 겹치면 갤러리 달이 그쪽으로 잘못 분류된다.
        exp = st.expander(f"{ym_label(m, multi_year=multi)} — {len(items)}장",
                          key=f"gal_open_{m}", on_change="rerun")
        with exp:
            if not exp.open:
                continue        # 닫힌 달은 CDN 요청이 한 건도 나가지 않는다
            _gallery_page(items, f"gal_page_{m}")


def _category_section(posts: list[dict], months: list[int]) -> None:
    st.caption("출사 공지(cat=A) 제목의 [카테고리] 태그 기준. 출사: 인물(1:1인물·1:1인물출사 포함)·인물&풍경·풍경·GN / 활동: 보정·문화.")
    rows = category_counts(posts)
    if not rows:
        st.info("분류된 카테고리가 없습니다.")
        return

    st.markdown("#### 월별 카테고리 추이")
    exclude_canceled = st.checkbox("취소(펑) 제외", value=False, key="cat_monthly_excl")
    mrows, skipped = category_monthly(posts, months, exclude_canceled=exclude_canceled)
    if any(r["공지 수"] for r in mrows):
        st.altair_chart(
            stacked_bar(mrows, "월", "공지 수", "카테고리",
                        "월별 카테고리 분포 (출사일 기준)",
                        x_sort=axis_labels(months)),
            width="stretch",
        )
    else:
        st.caption("월 축에 놓을 수 있는 공지가 없습니다.")
    notes = [f"{k} {v}건" for k, v in skipped.items() if v]
    st.caption("출사일 기준 집계." + (f" 제외: {', '.join(notes)}." if notes else ""))

    st.markdown("#### 기간 전체 분포")
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        column_config={
            "개수": st.column_config.ProgressColumn(
                "개수", min_value=0, max_value=max(r["개수"] for r in rows), format="%d"),
        },
    )

    st.markdown("#### 카테고리별 작성자")
    st.caption("누가 어떤 출사를 주로 여는지. 카테고리가 붙은 공지만 집계합니다.")
    cross = category_author_ranking(posts)
    if cross:
        st.dataframe(_ranking_df(cross, "합계"), hide_index=True,
                     width="stretch", height=320)
    else:
        st.caption("카테고리가 붙은 공지가 없습니다.")

    st.markdown("#### 좋아요 통계")
    st.caption("**공지 수를 함께 보세요** — 1건 쓰고 좋아요를 많이 받으면 "
               "평균만으로는 1위가 됩니다.")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**카테고리별**")
        cl = category_likes(posts)
        if cl:
            st.dataframe(pd.DataFrame(cl), hide_index=True, width="stretch")
        else:
            st.caption("집계할 공지가 없습니다.")
    with c2:
        st.markdown("**작성자별** (공지 2건 이상)")
        al = author_likes(posts)
        if al:
            st.dataframe(pd.DataFrame(al), hide_index=True,
                         width="stretch", height=320)
        else:
            st.caption("공지 2건 이상인 작성자가 없습니다.")


def _raw_nick(m: dict) -> str:
    """표시용으로 실명이 붙기 전의 닉네임. **동일성 판정에는 이쪽을 쓴다.**"""
    return m.get("_raw_mn") or m.get("mn") or ""


def display_of(m: dict) -> str:
    return m.get("mn") or ""


def _tab_members(members: list[dict], posts: list[dict], photos: list[dict],
                  duplicates: set[str] | None = None,
                  months: list[int] | None = None,
                  since_ym: int | None = None) -> None:
    """🧑‍🤝‍🧑 활성 멤버 현황 — 유령/휴면 분류, 신규 가입 추이, 동명이인 마킹."""
    if not members:
        st.info("멤버 정보가 없습니다. 사이드바의 **API 수집**으로 받아오면 이 탭이 채워집니다.")
        return

    # 표시용으로 실명이 붙은 members가 들어오므로 원래 닉네임으로 되돌려 센다.
    duplicates = duplicates or find_duplicate_member_names(
        [{"mn": _raw_nick(m)} for m in members])
    months = months or []
    posts_A = [p for p in posts if p.get("cat") == "A"]
    active_authors = activity_authors(posts, photos)
    attended = Counter()
    for a in posts_A:
        for n in a.get("attendees", []) or []:
            attended[n] += 1

    admins = sum(1 for m in members if m.get("is_admin"))
    ios = sum(1 for m in members if (m.get("os") or "") == "iOS")
    # 갓 가입한 사람은 뺀다 — 아직 나갈 출사가 안 열렸을 수도 있다. 안 빼면
    # 유령 명단이 사실상 **신규 가입자 명단**이 된다(실제로 14명 중 13명이
    # 그달 가입자였다).
    ghosts = [m for m in members
              if m["mn"] and m["mn"] not in active_authors
              and attended.get(m["mn"], 0) == 0
              and not joined_recently(m.get("joined_at"), months)]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("활성 멤버", len(members))
    c2.metric("운영진", admins)
    c3.metric("iOS / Android", f"{ios} / {len(members) - ios}")
    c4.metric("유령 멤버", len(ghosts))
    c5.metric("⚠️ 동명이인", len(duplicates))
    st.caption(
        f"**유령 멤버**: 가입인사만 쓰고 그 뒤로 게시글·사진·참석이 0건인 사람. "
        f"가입인사는 활동으로 세지 않습니다 — 안 쓰면 내보내는 규칙이라 전원이 "
        f"쓰기 때문입니다. **가입한 지 {GHOST_GRACE_DAYS}일이 안 된 분은 뺍니다** "
        f"(아직 나갈 출사가 안 열렸을 수도 있습니다). **동명이인**: 같은 닉네임의 "
        f"활성 멤버가 둘 이상 — 후기 본문에선 분리 불가하니 합쳐 집계됨."
    )

    if duplicates:
        dup_rows = []
        for mn in sorted(duplicates):
            # 동명이인 판정은 **원래 닉네임**으로 한다. 실명을 병기하면 두 사람이
            # 서로 다른 이름이 되지만, 후기 본문에는 여전히 닉네임만 적히므로
            # 보고서에서 합쳐 집계되는 문제는 그대로다 — 경고는 유지돼야 한다.
            same = [m for m in members if _raw_nick(m) == mn]
            for m in same:
                dup_rows.append({
                    "닉네임": f"⚠️ {display_of(m)}",
                    "mid": m.get("mid", ""),
                    "가입일": m["joined_at"].strftime("%Y-%m-%d") if m.get("joined_at") else "-",
                    "마지막 방문": m["last_visit"].strftime("%Y-%m-%d") if m.get("last_visit") else "-",
                    "OS": m.get("os") or "",
                    "운영진": "Y" if m.get("is_admin") else "",
                })
        st.markdown(f"#### ⚠️ 동명이인 ({len(duplicates)}개 닉네임 · {len(dup_rows)}명)")
        st.dataframe(pd.DataFrame(dup_rows), hide_index=True, width="stretch")

    st.markdown(f"#### 유령 멤버 ({len(ghosts)}명)")
    if ghosts:
        gdf = pd.DataFrame([{
            "닉네임": (f"⚠️ {display_of(m)}" if _raw_nick(m) in duplicates
                     else display_of(m)),
            "가입일": m["joined_at"].strftime("%Y-%m-%d") if m.get("joined_at") else "-",
            "마지막 방문": m["last_visit"].strftime("%Y-%m-%d") if m.get("last_visit") else "-",
            "OS": m.get("os") or "",
            "운영진": "Y" if m.get("is_admin") else "",
        } for m in sorted(ghosts,
                           key=lambda x: x.get("last_visit") or datetime.min)])
        st.dataframe(gdf, hide_index=True, width="stretch", height=320)
    else:
        st.caption("유령 멤버가 없습니다.")

    # 선택한 분석 기간에 맞춰 집계한다 (예전에는 datetime.now().year로 '올해'에 고정돼
    # 과거 기간을 분석해도 늘 올해 가입만 보였다).
    if months:
        st.markdown(f"#### {period_label(months[0], months[-1])} 월별 신규 가입")
        retention = joiner_retention(posts, members, months)
        joined_total = sum(r["가입"] for r in retention)
        if joined_total:
            st.caption("**가입인사 기준**입니다. 이 모임은 가입인사를 쓰지 않으면 "
                       "12시간 안에 강퇴하므로, 가입인사가 곧 제대로 가입한 사람의 "
                       "명단입니다. 인사 글은 사람이 나가도 남아 있어 **이탈까지 셀 수 "
                       "있습니다.**")
            df = pd.DataFrame(retention).set_index("월")
            st.bar_chart(df[["잔류", "이탈"]], height=240)
            left = sum(r["이탈"] for r in retention)
            c1, c2, c3 = st.columns(3)
            c1.metric("가입", f"{joined_total}명")
            c2.metric("잔류", f"{joined_total - left}명")
            c3.metric("이탈", f"{left}명",
                      delta=f"-{round(left / joined_total * 100, 1)}%",
                      delta_color="inverse")

            gone = departed_joiners(posts, photos, members)
            if gone:
                with st.expander(f"나간 멤버 {len(gone)}명 — 가입인사는 썼지만 지금 없음"):
                    st.caption("**활동 기간**이 짧으면 인사만 쓰고 사라진 것이고, "
                               "길면 한참 활동하다 나간 것입니다 — 뜻이 전혀 다릅니다.")
                    st.dataframe(pd.DataFrame(gone), hide_index=True,
                                 width="stretch", height=280)
        else:
            # 폴백: 현재 멤버의 가입일. **나간 사람이 빠져 있어 과소 집계된다.**
            joins: Counter = Counter()
            for m in members:
                joined = m.get("joined_at")
                if joined:
                    joins[ym_of(joined)] += 1
            st.bar_chart(pd.DataFrame({"신규 가입": axis_values(joins, months)},
                                      index=axis_labels(months)), height=240)
            st.warning(
                "이 기간에 수집된 가입인사가 없어 **현재 멤버의 가입일**로 셌습니다. "
                "나간 사람은 멤버 목록에 없으므로 **실제보다 적게 나오고, 이탈이 "
                "많았던 달일수록 더 적어 보입니다.** 해당 기간을 수집하면 가입인사 "
                "기준으로 바뀝니다.", icon="⚠️")

    # 유령 멤버는 *전 기간* 0건만 잡는다 — 활동하다 끊긴 사람은 여기서 본다.
    st.markdown("#### 최근 조용해진 멤버")
    st.caption("참석 이력이 있는데 3개월 넘게 참석이 없는 멤버. "
               "한 번도 참석한 적 없는 사람은 위 유령 멤버 쪽입니다.")
    dormant = dormant_members(posts, members)
    if dormant:
        st.dataframe(pd.DataFrame(dormant), hide_index=True, width="stretch", height=280)
    else:
        st.caption("최근 3개월 안에 다들 한 번은 참석했습니다.")

    st.markdown("#### 신규 멤버 정착")
    st.caption("가입 후 첫 참석까지 걸린 기간. **첫 참석이 비어 있으면 가입만 하고 아직 안 온 분**입니다. "
               "수집 기간보다 먼저 가입한 분은 첫 참석이 데이터 밖일 수 있어 제외합니다.")
    settle, skipped = newcomer_settling(members, posts, since_ym)
    if skipped:
        st.caption(f"수집 기간 이전 가입 {skipped}명은 집계에서 제외했습니다.")
    if settle:
        arrived = [r for r in settle if r["가입→첫 참석(일)"] is not None]
        c1, c2 = st.columns(2)
        c1.metric("첫 참석까지 (중앙값)",
                  f"{sorted(r['가입→첫 참석(일)'] for r in arrived)[len(arrived) // 2]}일"
                  if arrived else "—")
        c2.metric("가입만 하고 미참석", f"{len(settle) - len(arrived)}명")
        st.dataframe(pd.DataFrame(settle), hide_index=True, width="stretch", height=280)
    else:
        st.caption("수집 기간 안에 가입한 멤버가 없습니다.")


@st.fragment
def _tab_member_focus(posts: list[dict], photos: list[dict],
                      members: list[dict], months: list[int],
                      duplicates: set[str] | None = None,
                      body_cut: int | None = None) -> None:
    """🔎 한 사람만 골라서 세로로 보는 화면.

    나머지 여섯 탭은 전부 "한 행이 한 사람"인 가로 집계라, 사람 하나를
    알아보려면 탭을 오가며 표에서 이름을 눈으로 찾아야 했다.

    **프래그먼트다.** 드롭박스를 바꿀 때마다 앱 전체를 다시 그리면 사진 수천
    장에 이름을 다시 붙이고 여섯 탭을 통째로 재계산한다 — 사람을 이어서
    훑어볼 수가 없다(🎨 테마사진 체크박스와 똑같은 이유).
    """
    if not members:
        st.info("멤버 정보가 없습니다. 사이드바의 **📥 수집**으로 받아오면 이 탭이 채워집니다.")
        return

    opts = member_options(members, posts, photos)
    if not opts:
        st.info("분석할 멤버가 없습니다.")
        return

    names = [o["이름"] for o in opts]
    by_name = {o["이름"]: o for o in opts}
    # 이 드롭다운이 탭 전체의 조종간인데, 회색 캡션 밑에 평범한 셀렉트박스로
    # 두면 지나치기 쉽다. 테두리로 묶고 제목을 얹어 눈에 걸리게 한다.
    with st.container(border=True):
        st.markdown("#### 👤 어떤 멤버를 볼까요?")
        name = st.selectbox(
            "멤버 선택", names, key="mf_member", label_visibility="collapsed",
            format_func=lambda n: (f"{n} · 참석 {by_name[n]['참석']} · "
                                   f"글 {by_name[n]['게시글']} · 사진 {by_name[n]['사진']}"),
            help="활성 멤버 명단에서 고릅니다. 활동이 0건인 유령 멤버도 목록에 "
                 "있습니다 — 아무것도 안 했다는 사실 자체가 확인할 값입니다.",
        )
    if not name:
        return

    # 공통 집계는 한 번만 만들어 프로필·동행·칭호가 나눠 쓴다.
    ctx = club_context(posts, photos, members)
    prof = member_profile(name, posts, photos, members, ctx)
    comp = member_companions(name, posts, ctx["쌍"])
    my_posts = [p for p in posts if p.get("author") == name]
    my_photos = [p for p in photos if p.get("author") == name]

    st.markdown(f"### {name}")
    badges = []
    if prof["운영진"]:
        badges.append(("운영진", "blue"))
    if prof["유령"] and prof["신입"]:
        # 갓 들어온 사람에게 "유령"은 가혹하다 — 아직 나갈 출사가 안 열렸을 수도
        # 있다. 멤버 탭의 유령 명단도 같은 이유로 이 사람을 빼고 센다.
        badges.append(("🚪 아직 첫 출사 전 — 가입한 지 얼마 안 됐습니다", "gray"))
    elif prof["유령"]:
        badges.append(("유령 — 이 기간 활동 0건", "gray"))
    elif prof["휴면"]:
        badges.append(("휴면 — 3개월 넘게 참석 없음", "orange"))
    dup_hit = any(_raw_nick(m) in (duplicates or set())
                  for m in members if m.get("mn") == name)
    if dup_hit:
        badges.append(("⚠️ 동명이인", "red"))
    if badges:
        for col, (text, color) in zip(st.columns(len(badges)), badges):
            col.badge(text, color=color)
    if dup_hit:
        st.warning("같은 닉네임의 활성 멤버가 둘 이상입니다. 후기 본문에서는 둘을 "
                   "가를 수 없어 **아래 숫자는 두 사람이 합쳐진 값**입니다.", icon="⚠️")

    # 정원이 있어 한 사람만 따로 낼 수 없다 — 같은 칭호를 몇 명이 받는지
    # 알아야 자르기 때문이다. 전원을 내고 자기 것을 꺼낸다(0.1초대).
    all_titles = club_titles(posts, photos, members, months, ctx)
    titles = all_titles.get(name) or []
    if titles:
        with st.container(border=True):
            for col, t in zip(st.columns(len(titles)), titles):
                col.markdown(f"##### {t['아이콘']} {t['칭호']}")
                col.caption(t["근거"])
        st.caption("칭호는 **선택한 분석 기간 안의 활동**으로만 매깁니다 — 기간을 "
                   "좁히면 달라집니다. 재미로 붙이는 것이니 너무 진지하게 보지 마세요.")

    c = st.columns(5)
    c[0].metric("참석", prof["참석"])
    c[1].metric("개최한 출사", prof["개최"])
    c[2].metric("후기", prof["후기"])
    c[3].metric("사진", prof["사진"])
    c[4].metric("테마사진", prof["테마사진"])
    c = st.columns(5)
    c[0].metric("참석률", f"{prof['참석률']}%")
    c[1].metric("첫 등장", prof["첫 등장"])
    c[2].metric("최근 참석", prof["최근 참석"])
    c[3].metric("가입일", prof["가입일"])
    c[4].metric("마지막 방문", prof["마지막 방문"])
    st.caption(f"**참석률의 분모는 후기가 매칭된 출사 {prof['매칭 출사']}건**입니다. "
               "후기가 없는 출사는 누가 갔는지 알 방법이 없어, 분모에 넣으면 아무 "
               "잘못 없이 모두의 참석률이 낮아집니다.")

    st.markdown("#### 🏅 모임 내 순위")
    # 회색 캡션 한 줄이던 것을 metric으로 올렸다 — 이 화면에서 가장 궁금한
    # 숫자 축에 드는데 가장 작게 그려져 있어 눈에 걸리지 않았다.
    #
    # 모수는 **라벨에** 넣는다. `delta`로 빼면 증감 화살표가 붙어 "18명 늘었다"로
    # 읽힌다.
    for col, key in zip(st.columns(3), ("참석", "개최", "사진")):
        rank = prof[f"{key} 순위"]
        col.metric(f"{key} ({prof[f'{key} 모수']}명 중)",
                   f"{rank}등" if rank else "—")
    st.caption("같은 횟수면 **같은 등수**입니다(1·2·2·4). 모수는 그 활동을 한 번이라도 "
               "한 사람 수 — 개최는 펑 아닌 출사를 연 사람만 셉니다.")

    st.markdown("#### 월별 활동 추이")
    mm, _ = attendance_monthly_matrix(posts)
    mine = mm.get(name, {})
    mt = monthly_table(my_posts, my_photos)
    st.bar_chart(pd.DataFrame({
        "참석": [mine.get(m, 0) for m in months],
        "개최": axis_values(mt["진행 출사"], months),
        "후기": axis_values(mt["후기글"], months),
        "사진": axis_values(mt["사진"], months),
    }, index=axis_labels(months)), height=260)
    st.caption("개최는 출사일, 후기·사진은 작성일 기준 — 다른 탭의 월별 추이와 같습니다. "
               "취소된 출사는 `개최`에 들어가지 않습니다.")

    pref = member_category_pref(posts).get(name)
    if pref:
        st.markdown("#### 선호 카테고리")
        st.caption("참석한 출사의 카테고리 **전체 분포**입니다. "
                   f"👥 참석 & 후기 탭의 표는 상위 {PREF_TOP_N}개만 보여 줍니다.")
        st.altair_chart(donut(dict(pref.most_common()), "참석 카테고리"),
                        width="stretch")

    st.markdown(f"#### 개최한 출사 ({prof['개최']}건)")
    hosted = member_hosted_outings(name, posts)
    if hosted:
        st.caption(f"취소(펑) {prof['개최 취소']}건 포함 · 취소율 {prof['취소율']}%. "
                   "`후기 없음`은 공지는 있었지만 짝이 될 후기를 못 찾은 출사입니다.")
        st.dataframe(pd.DataFrame(hosted), hide_index=True, width="stretch",
                     height=min(420, 40 * len(hosted) + 40))
    else:
        st.caption("개최한 출사가 없습니다.")

    # 후기는 **개최한 출사 바로 밑**에 둔다 — 자기가 연 출사를 보고 나서 "그럼
    # 후기는 쓰고 있나"를 확인하는 것이 한 동작인데, 사이에 표가 끼면 두 번
    # 스크롤해 눈으로 맞춰야 한다.
    st.markdown(f"#### 작성한 후기 ({prof['후기']}건)")
    if prof["자기 출사 후기 분모"]:
        st.caption(f"본인이 연 출사 {prof['자기 출사 후기 분모']}건"
                   "(펑과 아직 안 다녀온 출사 제외) 중 "
                   f"**{prof['자기 출사 후기']}건은 본인이 후기를 썼습니다 "
                   f"({prof['자기 출사 후기율']}%).** 나머지는 다른 사람이 썼거나 "
                   "아직 후기가 없습니다 — 후기를 꼭 개최자가 쓰는 것은 아닙니다.")
    revs = member_reviews(name, posts, body_cut)
    if revs:
        n_cut = sum(1 for r in revs if r["잘림"])
        if n_cut:
            st.caption(f"✂️ 본문이 잘린 후기 {n_cut}건 — 그 글에서 뽑은 참석자 명단이 "
                       "전부가 아닐 수 있습니다. 원문은 👥 참석 & 후기 탭에서 봅니다.")
        st.dataframe(pd.DataFrame(revs), hide_index=True, width="stretch",
                     height=min(420, 40 * len(revs) + 40))
    else:
        st.caption("작성한 후기가 없습니다.")

    st.markdown(f"#### 참석한 출사 ({prof['참석']}건)")
    attended = member_attended_outings(name, posts)
    if attended:
        st.dataframe(pd.DataFrame(attended), hide_index=True, width="stretch",
                     height=min(420, 40 * len(attended) + 40))
    else:
        st.caption("참석 기록이 없습니다.")

    st.markdown("#### 함께 간 사람")
    if comp:
        st.caption("이 사람과 같은 출사에 함께 간 **전원**입니다 — 👥 참석 & 후기 "
                   f"탭의 표는 전체 상위 {CO_ATTENDANCE_TOP}쌍만 보여 주므로 여기 "
                   "있는 사람이 거기엔 없을 수 있습니다. `내 기준`은 이 사람의 "
                   "전체 참석 중 상대와 함께한 비율, `상대 기준`은 그 반대입니다.")
        st.dataframe(
            pd.DataFrame(comp, columns=COMPANION_COLS),
            hide_index=True, width="stretch", height=min(460, 40 * len(comp) + 40),
            column_config={
                "함께": st.column_config.NumberColumn("함께", format="%d회"),
                "상대 참석": st.column_config.NumberColumn("상대 참석", format="%d회"),
                "내 기준": st.column_config.ProgressColumn(
                    "내 기준", min_value=0, max_value=100, format="%.0f%%",
                    help=f"{name}이(가) 간 전체 출사 중 이 사람과 함께한 비율"),
                "상대 기준": st.column_config.ProgressColumn(
                    "상대 기준", min_value=0, max_value=100, format="%.0f%%",
                    help="상대가 간 전체 출사 중 이 사람과 함께한 비율"),
            },
        )
    else:
        st.caption("두 명 이상이 참석한 출사에 함께한 기록이 없습니다.")

    st.markdown(f"#### 사진 ({prof['사진']}장)")
    if my_photos:
        c = st.columns(4)
        c[0].metric("좋아요 합", prof["사진 좋아요"])
        c[1].metric("장당 좋아요", prof["장당 좋아요"])
        c[2].metric("테마사진", prof["테마사진"])
        c[3].metric("테마 참여월", f"{prof['테마 참여월']}개월")
        tops = top_photos(my_photos, 8)
        st.caption("좋아요 상위 8장. 전체는 🖼 갤러리 탭에서 업로더로 걸러 보세요.")
        for i in range(0, len(tops), GALLERY_COLS):
            for col, p in zip(st.columns(GALLERY_COLS), tops[i:i + GALLERY_COLS]):
                col.image(p["url_medium"], width="stretch",
                          caption=f"👍{p['likes']} 💬{p['comments']}")
    else:
        st.caption("업로드한 사진이 없습니다.")

    st.divider()
    _title_distribution(names, all_titles)


def _title_distribution(names: list[str],
                        all_titles: dict[str, list[dict]]) -> None:
    """전 멤버에게 칭호가 어떻게 퍼졌는지 — **기준을 조정하려고 보는 화면.**

    칭호는 아무도 못 받아도, 몇 사람이 싹쓸이해도 재미가 없다. 그런데 그건
    조건을 아무리 들여다봐도 알 수 없고 **실제 데이터에 대고 세어 봐야만**
    안다. 그래서 세는 화면을 함께 둔다.

    `_theme_section`과 같은 상태 있는 expander다 — **닫혀 있으면 전 멤버
    계산을 아예 안 한다.** 쉰 명분 칭호를 매 rerun마다 돌릴 이유가 없다.
    """
    exp = st.expander("🏆 칭호 분포 — 기준이 적당한지 보는 곳",
                      key="mf_dist_open", on_change="rerun")
    with exp:
        if not exp.open:
            return
        st.caption(f"칭호마다 **정원**이 있습니다 — 일반 {TITLE_QUOTA_DEFAULT}명, "
                   f"관계형·카테고리형 {TITLE_QUOTA['관계']}명. 넘치면 강한 순으로 "
                   "자르므로 **인원이 정원에 딱 붙어 있으면 조건이 느슨하다는 뜻**"
                   "입니다. 반대로 **수령 0명인 칭호**는 너무 조인 것입니다.")

        got: dict[str, list[str]] = defaultdict(list)
        per_person: dict[str, int] = {}
        for n in names:
            ts = all_titles.get(n) or []
            per_person[n] = len(ts)
            for t in ts:
                got[t["칭호"]].append(n)

        held = sum(1 for v in per_person.values() if v)
        c = st.columns(3)
        c[0].metric("칭호를 받은 사람", f"{held}명")
        c[1].metric("전체 멤버", f"{len(names)}명")
        c[2].metric("1인 평균",
                    f"{round(sum(per_person.values()) / len(names), 2)}개"
                    if names else "—")

        st.markdown("##### 칭호별 수령 인원")
        # **수령 0명인 칭호도 행으로 남긴다** — 아무도 못 받는 칭호가 있다는
        # 사실이 조정에 필요한 정보다. 안 걸린 것을 빼 버리면 화면만 보고는
        # 그 칭호가 존재하는지도 모른다.
        rows = [{"칭호": t, "인원": len(got.get(t, [])),
                 "받은 사람": ", ".join(got.get(t, [])[:8]) or "—"}
                for t in _all_title_names(got)]
        st.dataframe(pd.DataFrame(rows).sort_values("인원", ascending=False),
                     hide_index=True, width="stretch", height=460)

        st.markdown("##### 한 사람이 받은 개수")
        dist = Counter(per_person.values())
        st.dataframe(pd.DataFrame(
            [{"칭호 수": f"{k}개", "인원": dist.get(k, 0)}
             for k in range(TITLE_LIMIT + 1)]),
            hide_index=True, width="stretch")

        none_got = [n for n, v in per_person.items() if not v]
        if none_got:
            st.caption(f"**하나도 못 받은 {len(none_got)}명** — "
                       + ", ".join(none_got))


# 이름에 사람·카테고리가 박히는 칭호(`{상대}님과 2인 1조`)는 미리 나열할 수
# 없다. 그래서 고정 이름 목록에 **이번에 실제로 나온 것**을 합쳐 보여 준다.
FIXED_TITLE_NAMES = [
    "테마사진의 제왕", "테마사진 프로 참석러", "출사장도 장이다", "심심한데 출사쳐야지",
    "이게 본업이에요", "프로 참석러", "여기 제 인스타인데..", "부지런한 업로더",
    "사진 좋아요 1위", "느좋 사진러", "다 아는 사람들 이구먼", "저 신입 아닌데요",
    "책임감 100만점", "아맞다후기", "후기는 따끈할때", "내일 출사가실분?",
    "정출킬러", "소수정예",
    "소모임에요? 글쎄..", "제가 사진이 좀 많아요", "펑이 뭐죠?", "그럴만한 이유가...",
    "잡식성", "프로 평일러",
    "틈틈이 골고루", "짧은 기간에 진심",
    "아이고 어르신", "첫 출사 못 참지", "새싹", "돌아오세요",
    "아직 첫 출사 전", "유령 회원",
    *CATEGORY_TITLES.values(),
]


def _all_title_names(got: dict[str, list[str]]) -> list[str]:
    return FIXED_TITLE_NAMES + sorted(set(got) - set(FIXED_TITLE_NAMES))


# ═══════════════════════════════════════════════════════════════
# 메인 UI
# ═══════════════════════════════════════════════════════════════

def _gsheets_conf() -> dict | None:
    """secrets의 [gsheets] 설정. 없으면 None (관련 UI를 통째로 숨긴다)."""
    try:
        conf = st.secrets.get("gsheets")
    except Exception:  # noqa: BLE001 — secrets 파일 자체가 없으면 예외가 난다
        return None
    if not conf or not conf.get("credentials"):
        return None
    return dict(conf)


def _gsheets_configured() -> bool:
    return _gsheets_conf() is not None


def _gsheets_store():
    """설정된 자격증명으로 Drive 스토어를 만든다. 실패 시 GSheetsError."""
    conf = _gsheets_conf() or {}
    from core.gsheets import GoogleSheetsStore, parse_credentials
    return GoogleSheetsStore(
        parse_credentials(conf.get("credentials")),
        folder_id=conf.get("folder_id"),
    )


def _period_picker(full: tuple[int, int]) -> tuple[int, int]:
    """분석 기간 좁히기 — raw에는 수집한 전 기간이 쌓이므로 보기 범위를 고를 수 있게.

    기본값은 전체. 5년치가 쌓이면 월 축이 60칸이 되어 표·엑셀이 다루기 어려워진다.
    """
    axis = month_axis(*full)
    labels = [f"{m // 100}-{m % 100:02d}" for m in axis]
    if len(axis) == 1:
        return full
    i, j = st.select_slider(
        "분석 기간", options=list(range(len(axis))),
        value=(0, len(axis) - 1),
        format_func=lambda k: labels[k], key="view_range",
    )
    return axis[i], axis[j]


def render_sidebar(stores, analysis: dict | None) -> None:
    """수집 · 데이터 현황 · 보정 안내 · 다운로드."""
    with st.sidebar:
        if stores is None:
            st.error(
                "구글 연동이 설정되지 않았습니다. `secrets.toml`에 `[gsheets]`"
                "(credentials · folder_id)를 넣어 주세요.", icon="⚙️",
            )
            return
        raw_store, fix_store = stores

        # ── 수집 ────────────────────────────────────────────────
        st.subheader("📥 수집")
        st.caption("소모임 API에서 기간만큼 받아 구글 시트에 쌓습니다. "
                   "이미 있는 글은 최신 내용으로 갱신됩니다.")
        cur = datetime.now().year
        years = list(range(cur, cur - 8, -1))
        c1, c2 = st.columns(2)
        with c1:
            sy = st.selectbox("시작 년", years, key="api_start_y")
            sm = st.selectbox("시작 월", list(range(1, 13)), key="api_start_m")
        with c2:
            ey = st.selectbox("종료 년", years, key="api_end_y")
            em = st.selectbox("종료 월", list(range(1, 13)), index=11, key="api_end_m")
        start_ym, end_ym = sy * 100 + sm, ey * 100 + em
        valid = start_ym <= end_ym
        if valid:
            n = len(month_axis(start_ym, end_ym))
            st.caption(f"📅 **{period_label(start_ym, end_ym)}** · {n}개월")
            if n > 24:
                st.warning("기간이 길면 API 페이지 한도에 걸려 과거 일부가 "
                           "누락될 수 있습니다. 나눠서 수집하세요.")
        else:
            st.error("종료가 시작보다 빠릅니다.")

        if st.button("수집 시작", type="primary", width="stretch", disabled=not valid):
            _run_collection(raw_store, fix_store, start_ym, end_ym)

        # ── 데이터 현황 ─────────────────────────────────────────
        st.divider()
        st.subheader("🗂 데이터 현황")
        # 원본 표를 앱에서 다시 그리던 📋 데이터 탭을 없앴다 — raw가 구글 시트에
        # 있으니 시트를 열면 정렬·필터·다운로드가 다 된다.
        st.link_button("📗 raw 시트 열기", sheet_url(raw_store.file_id),
                       width="stretch")
        history = (analysis or {}).get("history") or []
        if history:
            rng = collected_range(history, [], [])
            if rng:
                st.metric("수집한 기간", period_label(*rng))
            st.caption(f"게시글 {len((analysis or {}).get('posts') or [])}건 · "
                       f"사진 {len((analysis or {}).get('photos') or [])}건")
            with st.expander(f"수집 이력 {len(history)}회"):
                st.dataframe(pd.DataFrame(history[::-1]), hide_index=True,
                             width="stretch", height=200)
        else:
            st.caption("아직 수집한 데이터가 없습니다.")

        # ── 보정 ────────────────────────────────────────────────
        st.divider()
        st.subheader("📝 보정")
        st.caption("보정은 **구글 시트에서 직접** 합니다. 고친 뒤 새로고침하면 "
                   "분석에 반영되고, 다시 수집해도 그대로 유지됩니다.")
        st.link_button("📕 보정 시트 열기", sheet_url(fix_store.file_id),
                       width="stretch")
        # 분석할 때 이미 센 값을 그대로 쓴다 — 여기서 다시 읽으면 시트를 한 번 더
        # 부르는 데다, 두 숫자가 갈라질 여지가 생긴다.
        pending = (analysis or {}).get("pending")
        if pending is not None:
            from core.store import TAB_MEMBER_NAMES
            # 실명이 먼저다 — 채우고 나면 나머지 후보 자체가 줄어든다.
            if pending.get(TAB_MEMBER_NAMES):
                st.warning(
                    f"**① 실명 미기입 {pending[TAB_MEMBER_NAMES]}명** — "
                    "`이름매핑1`부터 채우시면 아래 항목이 줄어듭니다.", icon="👤")
            rest = {t: n for t, n in pending.items()
                    if t != TAB_MEMBER_NAMES and n}
            if rest:
                st.warning("아직 채우지 않은 보정 · "
                           + " · ".join(f"{t} {n}건" for t, n in rest.items()),
                           icon="✍️")
            elif not pending.get(TAB_MEMBER_NAMES):
                st.success("보정 대기 없음", icon="✅")
        if st.button("🔄 새로고침", width="stretch"):
            st.session_state.pop("_analysis", None)
            st.rerun()

        # ── 다운로드 ────────────────────────────────────────────
        if SHOW_EXPORT and analysis and analysis.get("posts"):
            st.divider()
            st.subheader("💾 다운로드")
            rng = st.session_state.get("_view_range") or collected_range(
                analysis["history"], analysis["posts"], analysis["photos"])
            if rng:
                tag = period_tag(*rng)
                from core.store import relabel_names
                real = analysis.get("real_names")
                xlsx = build_excel(
                    relabel_names(analysis["posts"], real),
                    relabel_names(analysis["photos"], real), rng[0], rng[1],
                    members=relabel_names(analysis["members"], real),
                    resolution=analysis["resolution"],
                )
                st.download_button(
                    "📥 엑셀 (인사이트)", data=xlsx,
                    file_name=f"다감노_{tag}_분석.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width="stretch",
                )
                if st.button("📤 구글 시트로 내보내기", width="stretch"):
                    with st.spinner("올리는 중…"):
                        try:
                            from core.gsheets import default_title
                            fid, url = _gsheets_store().upload(xlsx, default_title(tag))
                        except Exception as e:  # noqa: BLE001
                            st.error(f"내보내기 실패: {e}")
                        else:
                            st.session_state["_gsheet_url"] = url
            if st.session_state.get("_gsheet_url"):
                st.link_button("🔗 방금 내보낸 시트 열기",
                               st.session_state["_gsheet_url"], width="stretch")


def _run_collection(raw_store, fix_store, start_ym: int, end_ym: int) -> None:
    """수집 → raw 저장 → 보정 후보 시딩. 보정 시트의 기존 값은 건드리지 않는다."""
    from core.store import correction_candidates

    bar = st.progress(0.0, text="시작 준비 중…")
    with st.status("수집 중…", expanded=True) as status:
        def on_progress(msg: str, pct: float) -> None:
            bar.progress(min(max(pct, 0.0), 1.0), text=msg)
            st.write(msg)
        try:
            field_report: dict = {}
            posts = collect_posts(start_ym, end_ym, progress=on_progress,
                                  keep_unclassified=True,
                                  field_report=field_report)
            photos = collect_photos(start_ym, end_ym, progress=on_progress)
            on_progress("멤버 목록 수집…", 0.92)
            members, _ = collect_members()
            banned = collect_banned_names()
            active_mns = {m["mn"] for m in members if m.get("mn")}
            joined = [m["joined_at"] for m in members if m.get("joined_at")]
            joins = collect_join_greetings(progress=on_progress,
                                           active_members=members,
                                           min_joined_at=min(joined) if joined else None)
            join_aliases = parse_join_name_aliases(joins, active_mns)

            on_progress("구글 시트에 저장…", 0.96)
            totals = raw_store.save(posts=posts, photos=photos, members=members,
                                    banned=banned, join_aliases=join_aliases,
                                    period=(start_ym, end_ym))
            raw_store.save_field_report(field_report)

            on_progress("보정 후보 정리…", 0.99)
            # 후보 계산도 분석과 **같은 해소 기준**으로 해야 한다. 실명을 빠뜨리면
            # 이미 풀린 이름이 후기이름매핑에 계속 쌓인다.
            corrections = fix_store.load()
            annotate_attendees(posts, active_mns,
                               {**join_aliases,
                                **real_name_resolution(
                                    corrections.get("member_names") or {}, members),
                                **_resolution_of(corrections)})
            # 시딩은 앱을 열 때도 돈다(`seed_and_count`). 여기서는 방금 받은
            # 데이터로 바로 반영해 수집 직후에 후보를 볼 수 있게 한다.
            added = fix_store.seed(
                correction_candidates(posts, dict(collect_all_unresolved(posts)),
                                      corrections, members=members,
                                      join_aliases=join_aliases),
                master_names=active_mns)
        except Exception as e:  # noqa: BLE001
            status.update(label="수집 실패", state="error")
            st.error("수집 중 오류가 발생했습니다. (API·네트워크·구글 권한 확인)")
            st.exception(e)
            st.stop()
        bar.progress(1.0, text="완료")
        status.update(
            label=(f"완료 · 누적 게시글 {totals['게시글']} / 사진 {totals['사진']} "
                   f"· 보정 후보 {sum(added.values())}건 추가"),
            state="complete",
        )
    st.session_state.pop("_analysis", None)
    st.rerun()


def _resolution_of(corrections: dict) -> dict[str, str]:
    from core.store import resolution_from_corrections
    return resolution_from_corrections(corrections)


def real_name_resolution(member_names, members):
    from core.store import real_name_resolution as _f
    return _f(member_names, members)


def load_analysis(stores) -> dict | None:
    """시트에서 읽어 분석 상태를 만든다. 세션에 캐시 — 매 rerun마다 읽지 않도록."""
    if stores is None:
        return None
    if "_analysis" in st.session_state:
        return st.session_state["_analysis"]
    raw_store, fix_store = stores
    with st.spinner("구글 시트에서 데이터를 읽는 중…"):
        try:
            raw = raw_store.load()
            corrections = fix_store.load()
            analysis = build_analysis(raw, corrections)
        except Exception as e:  # noqa: BLE001
            # **원인별 안내가 아니라 확인 순서를 적는다.** 구글 API 예외는
            # 같은 원인에도 메시지가 제각각이라(404/403/`invalid_grant`…)
            # 문자열로 갈라 짚으면 틀린 쪽을 가리키기 쉽다. 대신 흔한 순서로
            # 늘어놓아 사용자가 위에서부터 지워 나가게 한다.
            from core.store import RAW_TABS

            st.session_state["_read_failed"] = True
            st.error(
                "**구글 시트를 읽지 못했습니다.** 쌓인 데이터는 시트에 그대로 "
                "있고, 앱이 그것을 못 가져온 상태입니다.\n\n"
                "위에서부터 확인해 주세요.\n\n"
                "1. **잠시 뒤 새로고침** — 구글 API가 잠깐 막히면(429·503) "
                "다시 시도하는 것만으로 풀립니다.\n"
                "2. **폴더 공유** — `[gsheets] folder_id`의 폴더가 서비스 계정 "
                "이메일에 **편집자**로 공유돼 있는지 봅니다. 공유가 풀리면 "
                "폴더가 아예 없는 것처럼 보입니다.\n"
                "3. **탭 이름** — 시트에서 탭을 지웠거나 이름을 바꿨다면 "
                f"되돌립니다(raw: {' · '.join(RAW_TABS)}).\n\n"
                "다 맞는데도 그대로면 사이드바 **📥 수집**으로 다시 받아 "
                "시트를 새로 세울 수 있습니다.",
                icon="⚠️",
            )
            # 원문은 접어 둔다. `st.exception`은 트레이스백을 통째로 쏟아
            # 안내를 밀어내고, 대부분의 사용자에게는 읽을 것이 없다.
            with st.expander("오류 원문"):
                st.code(f"{type(e).__name__}: {e}")
            return None
        # 테마 해제 후 시트를 다시 읽지 않고 재조립하려면 raw가 필요하다.
        st.session_state["_raw"] = raw
        analysis["pending"] = seed_and_count(fix_store, raw, corrections, analysis)
    st.session_state.pop("_read_failed", None)
    st.session_state["_analysis"] = analysis
    return analysis


def seed_and_count(fix_store, raw: dict, corrections: dict,
                   analysis: dict) -> dict[str, int]:
    """보정 후보를 시트에 채워 넣고 미기입 수를 돌려준다.

    **수집이 아니라 앱을 열 때 한다.** 후보는 이미 저장된 raw에서 파생되는
    것이라 API를 다시 부를 이유가 없다. 예전에는 수집 때만 시딩해서, 수집이
    중간에 실패하면 "보정 n건 필요"라고만 뜨고 **정작 시트에는 아무것도 없어
    무엇을 해야 할지 알 수 없었다.**

    미기입 수도 여기서 돌려준다 — 화면과 사이드바가 **시트라는 한 곳**을
    보게 해서 두 숫자가 갈라지지 않도록.
    """
    from core.store import correction_candidates

    try:
        fix_store.seed(
            correction_candidates(
                analysis["posts"],
                dict(collect_all_unresolved(analysis["posts"])),
                corrections,
                members=raw.get("members") or [],
                join_aliases=raw.get("join_aliases") or {},
            ),
            master_names=analysis["master"].get("names"),
        )
        return fix_store.pending_count()
    except Exception as e:  # noqa: BLE001 — 시딩 실패가 분석을 막아서는 안 된다
        st.warning(f"보정 후보를 시트에 채우지 못했습니다: {e}", icon="✍️")
        return {}


def main() -> None:
    st.set_page_config(page_title="다감노 분석", page_icon="📸", layout="wide")
    if not _auth_ok():
        return

    st.title("📸 다감노 분석")
    st.caption(f"{GROUP_NAME} 게시글·사진을 구글 시트에 쌓아 두고 분석합니다. "
               "보정은 시트에서 한 번만 하면 이후 수집에도 계속 적용됩니다.")

    stores = get_stores()
    analysis = load_analysis(stores)
    render_sidebar(stores, analysis)

    if stores is None:
        st.info("👈 먼저 구글 연동을 설정해 주세요.")
        return
    # 읽기가 **실패**한 것과 아직 **안 받은** 것은 다르다. 실패했는데
    # "수집에서 받아오세요"를 띄우면 시트에 데이터가 멀쩡히 있는 사람에게
    # 엉뚱한 일을 시키게 되고, 위에 뜬 진짜 안내와 말이 어긋난다.
    if st.session_state.get("_read_failed"):
        return
    if not analysis or not (analysis.get("posts") or analysis.get("photos")):
        st.info("👈 사이드바 **📥 수집**에서 기간을 지정해 데이터를 받아오세요.")
        return

    full = collected_range(analysis["history"], analysis["posts"], analysis["photos"])
    if full is None:
        st.warning("기간을 판단할 수 있는 데이터가 없습니다.")
        return

    view = _period_picker(full)
    st.session_state["_view_range"] = view
    posts, photos = slice_period(analysis, *view)
    from core.store import relabel_names
    real = analysis.get("real_names")
    # 테마 해제한 사진은 has_comment=False라 월별 미리보기에 안 나온다.
    # 되돌리려면 해제된 id와 사진이 필요한데, **기간으로 자르면 안 된다** —
    # 다른 기간에서 해제한 것을 되돌릴 수 없게 된다. "내가 뭘 숨겼나"는
    # 기간별 뷰가 아니다.
    st.session_state["_all_photos"] = relabel_names(analysis["photos"], real)
    st.session_state["_theme_excluded"] = {
        pid for pid, off in (analysis.get("photo_flags") or {}).items() if off}
    render_results(view[0], view[1],
                   relabel_names(posts, real), relabel_names(photos, real),
                   analysis["master"],
                   members=relabel_names(analysis["members"], real),
                   applied=analysis["applied"],
                   pending=analysis.get("pending"),
                   correction_url=sheet_url(stores[1].file_id) if stores else None,
                   fix_store=stores[1] if stores else None,
                   body_cut=analysis.get("body_cut"))



if __name__ == "__main__":
    main()
