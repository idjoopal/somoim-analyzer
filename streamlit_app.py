"""
다감노📸 소모임 분석 — Streamlit 앱

흐름: 수집 → 분류 검토(드롭박스 보정) → 인사이트 + 엑셀 다운로드.
실행: streamlit run streamlit_app.py
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime

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
    ym_valid,
)
from core.excel_builder import build_excel
from core.gsheets import sheet_url

ALL_CATS = OUTING_CATS + NON_OUTING_CATS
CAT_OPTIONS = ALL_CATS + ["(없음)"]
STATUS_OPTIONS = ["진행", "취소"]


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
    return [p for p in posts if p.get("cat") == "E" and not p.get("matched_outing_id")]



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


def heatmap(photos: list[dict], months: list[int], max_authors: int = 30) -> alt.Chart | None:
    user_month, authors, _, _ = theme_matrix(photos, months)
    authors = authors[:max_authors]
    labels = axis_labels(months)
    long = [
        {"월": lab, "작성자": a, "장수": user_month[a].get(m, 0)}
        for a in authors
        for m, lab in zip(months, labels) if user_month[a].get(m, 0) > 0
    ]
    if not long:
        return None
    df = pd.DataFrame(long)
    return (
        alt.Chart(df)
        .mark_rect()
        .encode(
            x=alt.X("월:O", title="월", sort=labels),
            y=alt.Y("작성자:N", sort=authors, title=None),
            color=alt.Color("장수:Q", scale=alt.Scale(scheme="purples"), legend=alt.Legend(title="장수")),
            tooltip=["작성자", "월", "장수"],
        )
        .properties(title="월별 테마사진 제출 (작성자×월)", height=max(220, 22 * len(authors)))
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
        apply_corrections, filter_excluded, real_by_nickname,
        real_name_resolution, resolution_from_corrections,
    )

    posts = [dict(p) for p in raw.get("posts") or []]
    photos = [dict(p) for p in raw.get("photos") or []]
    members = raw.get("members") or []
    join_aliases = raw.get("join_aliases") or {}

    counts = apply_corrections(posts, corrections)
    posts = filter_excluded(posts)

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
        "applied": counts,
        "history": raw.get("history") or [],
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
                   applied: dict[str, int] | None = None) -> None:
    period = period_label(start_ym, end_ym)
    months = month_axis(start_ym, end_ym)
    st.subheader(f"{period} 인사이트")
    render_basis_box(posts, photos, period)

    kpis = compute_kpis(posts, photos)
    for col, (label, val) in zip(st.columns(len(kpis)), kpis.items()):
        col.metric(label, val)

    if applied and any(applied.values()):
        st.caption(f"📕 보정 시트 적용 — 공지 {applied.get('공지', 0)}건 · "
                   f"참석자 {applied.get('참석자', 0)}건")

    master_names = master.get("names") if isinstance(master, dict) else (master or set())
    duplicates = master.get("duplicates") if isinstance(master, dict) else set()

    tabs = st.tabs(
        ["📊 개요", "📌 출사", "📝 후기", "👥 참석", "📷 사진", "🎨 테마사진",
         "🏷️ 카테고리", "👤 사용자", "🧑‍🤝‍🧑 멤버", "📋 데이터"]
    )

    with tabs[0]:
        _tab_overview(posts, photos, months)
    with tabs[1]:
        _tab_outings(posts, months)
    with tabs[2]:
        _tab_reviews(posts, months)
    with tabs[3]:
        _tab_attendance(posts, master_names or set(), months)
    with tabs[4]:
        _tab_photos(photos, months)
    with tabs[5]:
        _tab_theme(photos, months)
    with tabs[6]:
        _tab_categories(posts, months)
    with tabs[7]:
        _tab_users(posts, photos)
    with tabs[8]:
        _tab_members(members or [], posts, photos, duplicates or set(), months)
    with tabs[9]:
        _tab_data(posts, photos)


def _tab_overview(posts: list[dict], photos: list[dict], months: list[int]) -> None:
    k = compute_kpis(posts, photos)
    c1, c2 = st.columns(2)
    with c1:
        if k["진행 출사"] + k["취소 출사"] > 0:
            st.altair_chart(
                donut({"진행": k["진행 출사"], "취소": k["취소 출사"]}, "출사 공지 진행/취소"),
                width="stretch",
            )
    with c2:
        cats = category_counts(posts)
        if cats:
            st.altair_chart(
                donut({r["카테고리"]: r["개수"] for r in cats}, "카테고리 분포", scheme="set2"),
                width="stretch",
            )
    st.altair_chart(monthly_trend_chart(monthly_table(posts, photos), months), width="stretch")
    st.caption("월별 추이 — 출사는 출사일 기준, 후기·사진·테마사진 참가는 작성일 기준.")

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
    if ex["top_photo_likes"]:
        tph = ex["top_photo_likes"]
        st.markdown(f"**최고 인기 사진 (좋아요 기준)** 👍{tph['likes']} — {tph['author']}")


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
        st.altair_chart(hbar(ranking, "작성자", "합계", "공지 수 TOP 10", n=10), width="stretch")
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

    st.markdown("#### 출사 취소(펑) 순위")
    st.caption("공지 3건 이상 작성자 중 취소율 높은 순. 취소 = 제목 (펑)/[펑].")
    cancels = cancel_ranking(posts, min_notices=3)
    if cancels:
        st.dataframe(
            _ranking_df(cancels, "취소"),
            hide_index=True, width="stretch",
            column_config={"취소율": st.column_config.NumberColumn("취소율", format="%.1f%%")},
        )
    else:
        st.info("공지 3건 이상인 작성자가 없습니다.")

    st.markdown("#### 출사 공지 전체 목록")
    rows = outings_table(posts)
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _tab_reviews(posts: list[dict], months: list[int]) -> None:
    """📝 후기 게시글 목록 — 월별 expander, 각 후기 카드에 정규화된 참석자 명단.

    참석자는 Stage 1에서 적용된 매핑(가입인사 자동 + 사용자 매핑)으로 마스터 닉네임에
    정규화돼 있다 — 동명이인은 한 명으로 합쳐 표시된다는 주의가 있긴 하지만, 이 탭은
    "어느 후기에 누가 적혔는지" 빠르게 훑어보는 용도. 사진은 표시하지 않음.
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
    if not reviews:
        st.caption("후기 게시글이 없습니다.")
        return

    by_month: dict[int, list[dict]] = defaultdict(list)
    for r in reviews:
        by_month[ym_of(r["posted_at"])].append(r)

    multi = is_multi_year(months[0], months[-1]) if months else True
    for m in sorted(by_month.keys(), reverse=True):
        items = by_month[m]
        with st.expander(f"{ym_label(m, multi_year=multi)} — 후기 {len(items)}건", expanded=False):
            for r in items:
                posted = r["posted_at"].strftime("%Y-%m-%d")
                title = r.get("title") or ""
                author = r.get("author") or "—"
                body = r.get("body") or ""
                attendees = r.get("attendees") or []
                with st.container(border=True):
                    st.markdown(f"**{title}**")
                    meta_bits = [f"🗓 {posted}", f"✍ {author}"]
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
                    if body:
                        st.markdown("**본문**")
                        st.text(body)


def _tab_attendance(posts: list[dict], master: set[str], months: list[int]) -> None:
    st.info(
        "📝 **후기 본문에 적힌 이름 명단으로 실제 참석자를 추적합니다.** "
        "댓글이 막혀 있어도 후기는 공개이고, 본문의 실명을 멤버 마스터와 매칭합니다. "
        "매칭이 안 되면 보정 시트에서 고칠 수 있습니다 — **`이름매핑1`에 실명을 채우는 것이 "
        "가장 효과가 큽니다.** 그래도 남는 것은 `후기이름매핑`·`참석자보정`에서 처리하세요.",
        icon="👥",
    )

    rate = real_attendance_rate(posts)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("출사 공지", rate["공지"])
    c2.metric("후기 매칭", rate["매칭"])
    c3.metric("실제 진행률", f"{rate['진행률']}%")
    c4.metric("멤버 마스터", f"{len(master)}명")

    st.markdown("#### 멤버별 참석 횟수")
    counts = attendance_counts(posts)
    if counts:
        pref = member_category_pref(posts)
        first, last = member_first_seen(posts)
        for r in counts:
            top = pref[r["멤버"]].most_common(2)
            r["선호 카테고리"] = ", ".join(f"{c}({n})" for c, n in top) or "—"
            r["첫 등장"] = first.get(r["멤버"], "—")
            r["최근"] = last.get(r["멤버"], "—")
        st.dataframe(
            _ranking_df(counts, "참석횟수"),
            hide_index=True, width="stretch",
            column_config={
                "참석횟수": st.column_config.ProgressColumn(
                    "참석횟수", min_value=0,
                    max_value=max(r["참석횟수"] for r in counts) or 1, format="%d"),
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

    orph = orphan_reviews(posts)
    if orph:
        with st.expander(f"⚠️ 공지와 매칭되지 않은 후기 {len(orph)}건"):
            for r in orph[:30]:
                d = r["posted_at"].strftime("%Y-%m-%d")
                att = ", ".join(r.get("attendees", [])) or "—"
                st.markdown(f"- **{d}** [{r['author']}] {r['title']} · 참석자: {att}")


def _tab_photos(photos: list[dict], months: list[int]) -> None:
    st.info(
        "💬 **댓글이 달린 사진을 '테마사진 참여'로 간주합니다.** "
        "(댓글 내용은 비공개라 사진 자체로 추정합니다)",
        icon="🎨",
    )

    st.markdown("#### 사진 업로드 순위")
    st.caption("작성자별 사진 수 · 테마예상 = 댓글 달린(테마사진 참여 추정) 사진 수 · 좋아요 합계.")
    ranking = photo_user_ranking(photos)
    if ranking:
        st.altair_chart(
            hbar(ranking, "작성자", "사진수", "사진 업로드 TOP 10", n=10, scheme="oranges"),
            width="stretch",
        )
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


def _tab_theme(photos: list[dict], months: list[int]) -> None:
    st.info(
        "🎨 **테마사진 = 댓글이 달린 사진(rn>0)** 입니다. 댓글 내용은 비공개라 "
        "테마 이벤트 참여를 *추정*한 값이니, 아래 월별 미리보기로 실제 테마사진인지 직접 확인하세요.",
        icon="🎨",
    )
    user_month, authors, mon_count, mon_list = theme_matrix(photos, months)
    by_month = themed_photos_by_month(photos)
    multi = is_multi_year(months[0], months[-1]) if months else True

    st.markdown("#### 월별 테마사진 제출 인원")
    st.bar_chart(pd.DataFrame({"참여 인원": [mon_count.get(m, 0) for m in months]},
                              index=axis_labels(months)))
    st.caption("각 월을 펼치면 참여자 명단과 그 달 테마사진(댓글 달린 사진) 미리보기를 볼 수 있습니다.")
    for m in [m for m in months if mon_list.get(m)]:
        ph = by_month.get(m, [])
        with st.expander(f"{ym_label(m, multi_year=multi)} — "
                         f"참여 {len(mon_list[m])}명 · 테마사진 {len(ph)}장"):
            st.write("**참여자:** " + ", ".join(mon_list[m]))
            for i in range(0, len(ph), 5):
                for col, p in zip(st.columns(5), ph[i:i + 5]):
                    col.image(
                        p["url_small"], width="stretch",
                        caption=f"{p['author']} · 👍{p['likes']} 💬{p['comments']}",
                    )

    st.markdown("#### 테마 매트릭스")
    ch = heatmap(photos, months)
    if ch is not None:
        st.altair_chart(ch, width="stretch")
    else:
        st.info("테마사진(댓글 달린 사진)이 없습니다.")

    st.markdown("#### 테마 참여자 순위")
    st.caption("참여월수(여러 달에 걸친 참여) 우선, 동률은 테마사진 수.")
    parts = theme_participant_ranking(photos)
    if parts:
        st.dataframe(_ranking_df(parts, "테마사진"), hide_index=True, width="stretch")


def _tab_categories(posts: list[dict], months: list[int]) -> None:
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
    st.altair_chart(
        hbar(rows, "카테고리", "개수", "카테고리별 공지 수", n=len(rows), scheme="teals"),
        width="stretch",
    )
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        column_config={
            "개수": st.column_config.ProgressColumn(
                "개수", min_value=0, max_value=max(r["개수"] for r in rows), format="%d"),
        },
    )


def _tab_users(posts: list[dict], photos: list[dict]) -> None:
    st.markdown("#### 사용자 활동 종합 랭킹")
    st.caption(
        "작성자별 게시글 수(공지+취소+후기)와 업로드한 사진 수. 게시글이나 사진이 1건 이상인 사용자 전체. "
        "게시글 수 → 사진 수 순으로 정렬, 좋아요는 게시글 좋아요 합계."
    )
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
    if rows:
        st.dataframe(
            _ranking_df(rows, "게시글"),
            hide_index=True, width="stretch",
            column_config={
                "게시글": st.column_config.ProgressColumn(
                    "게시글", min_value=0, max_value=max(r["게시글"] for r in rows) or 1, format="%d"),
                "사진": st.column_config.ProgressColumn(
                    "사진", min_value=0, max_value=max(r["사진"] for r in rows) or 1, format="%d"),
            },
        )
    else:
        st.info("데이터가 없습니다.")


def _raw_nick(m: dict) -> str:
    """표시용으로 실명이 붙기 전의 닉네임. **동일성 판정에는 이쪽을 쓴다.**"""
    return m.get("_raw_mn") or m.get("mn") or ""


def display_of(m: dict) -> str:
    return m.get("mn") or ""


def _tab_members(members: list[dict], posts: list[dict], photos: list[dict],
                  duplicates: set[str] | None = None,
                  months: list[int] | None = None) -> None:
    """🧑‍🤝‍🧑 활성 멤버 현황 — 유령/휴면 분류, 신규 가입 추이, 동명이인 마킹."""
    if not members:
        st.info("멤버 정보가 없습니다. 사이드바의 **API 수집**으로 받아오면 이 탭이 채워집니다.")
        return

    # 표시용으로 실명이 붙은 members가 들어오므로 원래 닉네임으로 되돌려 센다.
    duplicates = duplicates or find_duplicate_member_names(
        [{"mn": _raw_nick(m)} for m in members])
    months = months or []
    posts_A = [p for p in posts if p.get("cat") == "A"]
    active_authors = ({p.get("author", "") for p in posts}
                       | {p.get("author", "") for p in photos})
    attended = Counter()
    for a in posts_A:
        for n in a.get("attendees", []) or []:
            attended[n] += 1

    admins = sum(1 for m in members if m.get("is_admin"))
    ios = sum(1 for m in members if (m.get("os") or "") == "iOS")
    ghosts = [m for m in members
              if m["mn"] and m["mn"] not in active_authors
              and attended.get(m["mn"], 0) == 0]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("활성 멤버", len(members))
    c2.metric("운영진", admins)
    c3.metric("iOS / Android", f"{ios} / {len(members) - ios}")
    c4.metric("유령 멤버", len(ghosts))
    c5.metric("⚠️ 동명이인", len(duplicates))
    st.caption(
        "**유령 멤버**: 가입했지만 게시글·사진·참석 0건 — 마지막 방문일로 휴면 여부 추정. "
        "닉네임이 같은 활동 흔적은 매칭. **동명이인**: 같은 닉네임의 활성 멤버가 둘 이상 — "
        "후기 본문에선 분리 불가하니 보고서에서 합쳐 집계됨."
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
        joins: Counter = Counter()
        for m in members:
            joined = m.get("joined_at")
            if joined:
                joins[ym_of(joined)] += 1
        jdf = pd.DataFrame({"신규 가입": axis_values(joins, months)},
                            index=axis_labels(months))
        st.bar_chart(jdf, height=240)
        st.caption("가입일이 분석 기간 안에 든 활성 멤버만 집계됩니다.")


def _tab_data(posts: list[dict], photos: list[dict]) -> None:
    st.caption("수집·보정된 원본 데이터 전체입니다. 표 우측 상단에서 검색·정렬, 아래 버튼으로 CSV 저장이 가능합니다.")

    st.markdown(f"#### 게시글 데이터 ({len(posts)}건)")
    pdf = posts_dataframe(posts)
    st.dataframe(pdf, hide_index=True, width="stretch", height=360)
    st.download_button(
        "⬇️ 게시글 CSV", data=pdf.to_csv(index=False).encode("utf-8-sig"),
        file_name="다감노_게시글.csv", mime="text/csv",
    )

    st.markdown(f"#### 사진 데이터 ({len(photos)}건)")
    phdf = photos_dataframe(photos)
    st.dataframe(
        phdf, hide_index=True, width="stretch", height=360,
        column_config={
            "고화질 URL": st.column_config.LinkColumn("고화질 URL", display_text="열기"),
            "썸네일 URL": st.column_config.LinkColumn("썸네일 URL", display_text="열기"),
        },
    )
    st.download_button(
        "⬇️ 사진 CSV", data=phdf.to_csv(index=False).encode("utf-8-sig"),
        file_name="다감노_사진.csv", mime="text/csv",
    )


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
        try:
            pending = fix_store.pending_count()
        except Exception:  # noqa: BLE001
            pending = None
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
        if analysis and analysis.get("posts"):
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
            analysis = build_analysis(raw_store.load(), fix_store.load())
        except Exception as e:  # noqa: BLE001
            st.error(f"구글 시트를 읽지 못했습니다: {e}")
            return None
    st.session_state["_analysis"] = analysis
    return analysis


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
    render_results(view[0], view[1],
                   relabel_names(posts, real), relabel_names(photos, real),
                   analysis["master"],
                   members=relabel_names(analysis["members"], real),
                   applied=analysis["applied"])



if __name__ == "__main__":
    main()
