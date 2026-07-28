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
    LEFT_MEMBER,
    NAME_BLACKLIST,
    NON_OUTING_CATS,
    NOT_A_NAME,
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
)
from core.excel_builder import build_excel, load_excel_bundle

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
# 분류 검토 (triage)
# ═══════════════════════════════════════════════════════════════

def build_editor_df(cat_a_sorted: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame([{
        "검토": p["review_reason"] or "",
        "공지일": p["posted_at"].date(),
        "작성자": p["author"],
        "제목": p["title"],
        "카테고리": p["category"] or "(없음)",
        "출사일": date.fromisoformat(p["outing_date"]) if p["outing_date"] else None,
        "상태": "취소" if p["is_canceled"] else "진행",
    } for p in cat_a_sorted])
    if not df.empty:
        df["공지일"] = pd.to_datetime(df["공지일"])
        df["출사일"] = pd.to_datetime(df["출사일"])
    return df


def apply_triage(raw_posts: list[dict], cat_a_sorted: list[dict],
                 edited: pd.DataFrame, start_ym: int, end_ym: int) -> list[dict]:
    """편집된 cat=A 분류를 적용하고, 수집기와 **같은 헬퍼**로 기간 필터.

    수집기와 판정이 갈라지지 않도록 `in_ym_range`를 공유한다.
    """
    final = [dict(p) for p in raw_posts if p["cat"] != "A"]

    for orig, (_, row) in zip(cat_a_sorted, edited.iterrows()):
        p = dict(orig)
        cat_val = row["카테고리"]
        p["category"] = None if cat_val == "(없음)" else cat_val
        p["is_outing"] = p["category"] in OUTING_CATS
        p["is_canceled"] = row["상태"] == "취소"

        od = row["출사일"]
        if pd.isna(od):
            continue  # 출사일 미상 → 분석 제외
        od = od.date() if hasattr(od, "date") else od
        if not in_ym_range(ym_of(od), start_ym, end_ym):
            continue
        p["outing_date"] = od.isoformat()
        p["needs_review"] = False
        p["review_reason"] = ""
        final.append(p)

    return final


def build_attendees_editor_df(reviews_sorted: list[dict]) -> pd.DataFrame:
    """후기(cat=E) 자동 추출 참석자를 편집할 표. 참석자 컬럼만 편집 가능."""
    rows = []
    for p in reviews_sorted:
        body = p.get("body", "") or ""
        unresolved = p.get("unresolved_names") or []
        rows.append({
            "검토": f"미해소 {len(unresolved)}명" if unresolved else "",
            "작성일": p["posted_at"].date(),
            "작성자": p["author"],
            "제목": p["title"],
            "본문": body,
            "참석자": ", ".join(p.get("attendees", [])),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["작성일"] = pd.to_datetime(df["작성일"], errors="coerce")
    return df


def apply_attendees_edits(
    final_posts: list[dict], reviews_sorted: list[dict], edited_att: pd.DataFrame,
) -> list[dict]:
    """편집된 참석자(쉼표) 문자열을 final_posts에 id 기준으로 적용.
    apply_triage의 dict(p) 얕은복사로 attendees 리스트가 raw와 alias이므로 항상 새 리스트를 할당한다.
    """
    if edited_att is None or edited_att.empty:
        return final_posts
    by_id = {p["id"]: p for p in final_posts if p.get("cat") == "E"}
    for orig, (_, row) in zip(reviews_sorted, edited_att.iterrows()):
        rid = orig["id"]
        target = by_id.get(rid)
        if target is None:
            continue
        raw = row.get("참석자")
        text = str(raw) if pd.notna(raw) else ""
        attendees = [n.strip() for n in text.split(",") if n.strip()]
        target["attendees"] = attendees  # 새 리스트 (in-place mutate 금지)
        target["unresolved_names"] = []   # 사용자가 직접 명시 → 미해소 없음
    return final_posts


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
# 수집 파이프라인 — 엑셀은 검토 후 생성
# ═══════════════════════════════════════════════════════════════

def collect_data(start_ym: int, end_ym: int, on_progress=None):
    """somoim 수집. 진행 콜백이 st를 호출하므로 @st.cache_data 대신 세션 캐시 사용
    (cache_data 안에서 st 호출 시 캐시 히트 replay가 CacheReplayClosureError로 실패)."""
    key = (start_ym, end_ym)
    cached = st.session_state.get("_collect_cache")
    if cached and cached["key"] == key:
        return cached["data"]
    posts = collect_posts(start_ym, end_ym, progress=on_progress, keep_unclassified=True)
    photos = collect_photos(start_ym, end_ym, progress=on_progress)
    st.session_state["_collect_cache"] = {"key": key, "data": (posts, photos)}
    return posts, photos


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


def _set_data(start_ym: int, end_ym: int, posts: list[dict], photos: list[dict],
              members: list[dict] | None = None,
              banned: set[str] | None = None,
              resolution: dict[str, str] | None = None,
              join_aliases: dict[str, str] | None = None) -> None:
    """수집/업로드 양쪽에서 호출. session_state['data'] 설정 + 후속 단계 키 클리어.

    data 튜플: (start_ym, end_ym, posts, photos, members, banned, resolution, join_aliases)
    """
    active_mids = {m["mid"] for m in (members or []) if m.get("mid")}
    _mark_active(posts, active_mids)
    _mark_active(photos, active_mids)
    st.session_state["data"] = (
        int(start_ym), int(end_ym), posts, photos,
        members or [], set(banned or set()), dict(resolution or {}),
        dict(join_aliases or {}),
    )
    for k in ("master", "result"):
        st.session_state.pop(k, None)


def _parse_resolution_csv(text: str) -> dict[str, str]:
    """매핑 CSV(`이름,처리`) → resolution dict. 첫 행이 헤더이면 스킵."""
    out: dict[str, str] = {}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return out
    start = 1 if "이름" in lines[0] and "처리" in lines[0] else 0
    for line in lines[start:]:
        if "," not in line:
            continue
        k, v = line.split(",", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


def _resolution_to_csv(resolution: dict[str, str]) -> bytes:
    """resolution dict → CSV bytes(utf-8-sig)."""
    lines = ["이름,처리"]
    for k, v in resolution.items():
        if "," in k or "," in v:
            continue  # 콤마 포함 토큰은 한 번 건너뜀(매우 드묾)
        lines.append(f"{k},{v}")
    return ("\n".join(lines) + "\n").encode("utf-8-sig")


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


OPT_SKIP  = "(선택 안 함)"
OPT_LEFT  = "🚪 탈퇴 멤버 (추적 안 함)"


def render_resolution(start_ym: int, end_ym: int, posts: list[dict],
                       photos: list[dict], members: list[dict],
                       banned: set[str], resolution_in: dict[str, str],
                       join_aliases: dict[str, str] | None = None) -> None:
    """Stage 1: 미매칭 이름 해소.

    마스터(`master_names`) = 활성 멤버 `mn`. 후기 본문에서 추출한 토큰 중
    마스터에 정확히 일치하지 않는 이름을 사용자가 드롭다운 3택(마스터 닉네임 /
    탈퇴 / 노이즈)으로 해소. 매핑은 누적 재사용.

    `join_aliases`(가입인사 자동 추출 `실명→닉네임`)는 사용자 매핑(`resolution_in`)과
    `{**join_aliases, **resolution_in}`으로 머지해 annotate 시 자동 base로 사용.
    드롭다운 기본값도 자동 매핑이 있으면 그 닉네임으로 미리 채움(사용자가 덮어쓰면 그 값 우선).
    """
    st.divider()
    st.subheader("① 미매칭 이름 정리")

    master_names: set[str] = {m["mn"] for m in members if m.get("mn")}
    if not master_names:
        st.error(
            "활성 멤버 명단이 없습니다. 이전 버전 엑셀을 업로드하셨다면 "
            "**🔄 처음으로** 후 **API 수집**으로 한 번 더 받아주세요."
        )
        return

    join_aliases = dict(join_aliases or {})
    user_res = dict(resolution_in or {})
    duplicates = find_duplicate_member_names(members)
    # 자동 매핑은 사용자 매핑이 비어 있을 때만 적용 (사용자 override 우선)
    effective = {**join_aliases, **user_res}

    # 매핑 보정 후 다시 매기기 위해 우선 항상 in-place 재주석
    annotate_attendees(posts, master_names, effective)
    unresolved_freq = collect_all_unresolved(posts)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("활성 멤버", len(master_names))
    c2.metric("미해소 이름", len(unresolved_freq))
    c3.metric("기존 매핑", len(user_res))
    c4.metric("🪪 가입인사 자동", len(join_aliases))

    if duplicates:
        st.warning(
            f"⚠️ **동명이인 닉네임 {len(duplicates)}개**: {', '.join(sorted(duplicates))} "
            "— 이 닉네임으로 매핑하면 두 사람이 한 명으로 합쳐 집계됩니다. "
            "후기 본문이 닉네임만 담고 있어 분리가 불가하니, 보고서 해석 시 참고하세요.",
            icon="⚠️",
        )

    if join_aliases:
        with st.expander(f"📖 가입인사 자동 매핑 보기 ({len(join_aliases)}건)"):
            st.caption(
                "가입인사 본문에서 `이름 : XXX` 패턴을 추출한 (실명 → 활성 멤버 닉네임) 매핑입니다. "
                "아래 표에서 같은 이름을 만나면 자동으로 이 닉네임으로 매핑되며, "
                "드롭다운에서 다른 값을 선택하면 사용자 매핑이 우선합니다."
            )
            st.dataframe(
                pd.DataFrame(
                    sorted(join_aliases.items()),
                    columns=["실명", "→ 활성 멤버 닉네임"],
                ),
                hide_index=True, width="stretch", height=240,
            )

    st.info(
        "후기에 적혔지만 **활성 멤버 명단과 정확히 일치하지 않는** 이름입니다. "
        "각 이름을 **① 마스터 닉네임으로 매핑**(닉네임 변형/오타) 또는 "
        "**② 탈퇴 멤버**(추적 안 함)로 지정하세요. "
        "이름이 아닌 단어(노이즈)는 **이름 ❌** 체크박스로 빠르게 제외할 수 있습니다. "
        "🪪 가입인사 본문에서 자동 추출된 매핑은 미리 채워져 있으니 그대로 두면 적용됩니다. "
        "한 번 지정한 매핑은 같은 엑셀을 올리거나 매핑 CSV로 재사용됩니다.",
        icon="🧭",
    )

    if not unresolved_freq:
        st.success("모든 이름이 마스터와 매칭됐어요. 다음 단계로 진행하세요.")
        if st.button("✅ 다음 단계로", type="primary"):
            st.session_state["master"] = {
                "names": master_names,
                "members": members,
                "banned": banned,
                "resolution": user_res,
                "join_aliases": join_aliases,
                "duplicates": duplicates,
            }
            st.rerun()
        return

    master_sorted = sorted(master_names)
    options = [OPT_SKIP, OPT_LEFT] + master_sorted

    # ── 이름 ❌ 일괄 처리 ─────────────────────────────────────────
    # 노이즈 토큰(조사·일반명사)은 수십 개씩 나오는데 체크박스를 한 행씩 누르는 게
    # 이 단계의 병목이었다. data_editor 체크박스는 드래그 채우기를 지원하지 않아
    # 표 위에 일괄 처리 바를 둔다. 여기서 정한 값은 표의 기본값으로만 반영되고,
    # 확정은 기존 "✅ 이 매핑으로 분석 진행" 버튼이 그대로 담당한다.
    noise_pending: set[str] = st.session_state.setdefault("_noise_pending", set())
    nonce = st.session_state.setdefault("_res_editor_nonce", 0)

    def _bump_editor() -> None:
        """data_editor는 key가 그대로면 이전 편집 델타를 붙들고 있어 입력 DataFrame을
        바꿔도 반영되지 않는다. key에 nonce를 섞어 위젯을 새로 만든다."""
        st.session_state["_res_editor_nonce"] += 1

    all_names = [n for n, _ in unresolved_freq.most_common()]
    once_names = [n for n in all_names if unresolved_freq[n] == 1]

    with st.container(border=True):
        st.markdown("**이름 ❌ 일괄 처리** — 이름이 아닌 단어를 한 번에 골라 표시합니다.")
        picked = st.multiselect(
            "이름 여러 개 선택 (빈도순 · 타이핑으로 검색)",
            options=all_names, default=[], key="noise_multiselect",
            format_func=lambda n: f"{n} ({unresolved_freq[n]}회)",
        )
        b1, b2, b3 = st.columns(3)
        if b1.button("선택 ❌ 처리", width="stretch", disabled=not picked):
            noise_pending.update(picked)
            st.session_state["noise_multiselect"] = []
            _bump_editor()
            st.rerun()
        if b2.button(f"빈도 1회 전체 ❌ ({len(once_names)}건)",
                     width="stretch", disabled=not once_names,
                     help="한 번만 등장한 토큰은 대개 오탈자·조사 등 노이즈입니다."):
            noise_pending.update(once_names)
            _bump_editor()
            st.rerun()
        if b3.button("❌ 전부 해제", width="stretch", disabled=not noise_pending):
            noise_pending.clear()
            _bump_editor()
            st.rerun()
        if noise_pending:
            st.caption(f"❌ 표시 대기 {len(noise_pending)}건 — 아래 **확정 버튼**을 눌러야 저장됩니다.")

    rows = []
    for name, cnt in unresolved_freq.most_common():
        current = user_res.get(name)
        auto = join_aliases.get(name)
        is_noise = (current == NOT_A_NAME) or (name in noise_pending)
        if current == LEFT_MEMBER:
            default = OPT_LEFT
        elif current in master_names:
            default = current
        elif auto in master_names:
            default = auto
        else:
            default = OPT_SKIP
        notes = []
        if auto in master_names:
            notes.append(f"🪪 가입인사 → {auto}")
        if name in (banned or set()):
            notes.append("🚪 탈퇴명단")
        if default in duplicates:
            notes.append("⚠️ 동명이인")
        rows.append({
            "이름": name,
            "빈도": int(cnt),
            "참고": " · ".join(notes),
            "이름 ❌": is_noise,
            "처리": default,
        })

    edited = st.data_editor(
        pd.DataFrame(rows),
        column_config={
            "이름": st.column_config.TextColumn("이름", disabled=True),
            "빈도": st.column_config.NumberColumn("빈도", disabled=True, width="small"),
            "참고": st.column_config.TextColumn("참고", disabled=True, width="medium"),
            "이름 ❌": st.column_config.CheckboxColumn(
                "이름 ❌", width="small",
                help="체크하면 이름이 아닌 것(노이즈)으로 즉시 처리 — 드롭다운 선택보다 우선합니다.",
            ),
            "처리": st.column_config.SelectboxColumn(
                "처리", options=options, required=True, width="medium",
                help="마스터 닉네임으로 매핑하려면 위 옵션 뒤에서 선택. ⚠️ 표시는 동명이인.",
            ),
        },
        column_order=["이름", "빈도", "참고", "이름 ❌", "처리"],
        hide_index=True, width="stretch", num_rows="fixed",
        key=f"resolution_editor_{start_ym}_{end_ym}_{nonce}",
    )

    if st.button("✅ 이 매핑으로 분석 진행", type="primary"):
        new_user_res = dict(user_res)
        for _, row in edited.iterrows():
            name = str(row.get("이름") or "")
            choice = str(row.get("처리") or OPT_SKIP)
            noise_flag = bool(row.get("이름 ❌"))
            auto = join_aliases.get(name)
            # 체크박스가 켜져 있으면 드롭다운 선택보다 우선 → 노이즈로 확정
            if noise_flag:
                new_user_res[name] = NOT_A_NAME
            elif choice == OPT_SKIP:
                new_user_res.pop(name, None)
            elif choice == OPT_LEFT:
                new_user_res[name] = LEFT_MEMBER
            elif choice == auto:
                # 사용자가 자동 매핑 기본값을 그대로 유지 — user_res에 기록 불필요
                new_user_res.pop(name, None)
            else:
                new_user_res[name] = choice
        final_effective = {**join_aliases, **new_user_res}
        annotate_attendees(posts, master_names, final_effective)
        st.session_state["_noise_pending"] = set()  # 확정됐으니 대기 상태 비움
        st.session_state["master"] = {
            "names": master_names,
            "members": members,
            "banned": banned,
            "resolution": new_user_res,
            "join_aliases": join_aliases,
            "duplicates": duplicates,
        }
        st.rerun()


def render_triage(start_ym: int, end_ym: int, raw_posts: list[dict],
                   photos: list[dict], master: dict) -> None:
    st.divider()
    st.subheader("② 분류 · 참석자 검토")
    cat_a = [p for p in raw_posts if p["cat"] == "A"]
    cat_a_sorted = sorted(cat_a, key=lambda p: (not p["needs_review"], p["posted_at"]))
    n_review = sum(1 for p in cat_a if p["needs_review"])

    reviews = [p for p in raw_posts if p["cat"] == "E"]
    reviews_sorted = sorted(
        reviews, key=lambda p: (not p.get("unresolved_names"), p["posted_at"]),
    )
    n_att_review = sum(1 for p in reviews if not p.get("attendees"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("출사 공지", len(cat_a))
    c2.metric("⚠️ 분류 검토", n_review)
    c3.metric("후기", len(reviews))
    c4.metric("참석자 0명", n_att_review)
    st.caption(
        "**분류 검토**: 자동 분류가 애매한 공지(출사일 추론 실패·카테고리 미상)의 카테고리·출사일·진행/취소를 보정. 출사일을 비우면 분석 제외. "
        "**참석자 보정**: ①에서 미해소한 이름이 있거나 자동 추출이 부족한 후기를 직접 수정(쉼표 구분). "
        "아래 버튼을 누르면 그 보정 분류로 인사이트·엑셀이 생성됩니다."
    )

    # ── 분류 editor ────────────────────────────────────────────
    st.markdown("##### 분류 보정 (출사 공지)")
    editor_df = build_editor_df(cat_a_sorted)
    col_config = {
        "검토": st.column_config.TextColumn("검토", disabled=True, width="small"),
        "공지일": st.column_config.DateColumn("공지일", disabled=True, format="YYYY-MM-DD"),
        "작성자": st.column_config.TextColumn("작성자", disabled=True, width="small"),
        "제목": st.column_config.TextColumn("제목", disabled=True, width="large"),
        "카테고리": st.column_config.SelectboxColumn("카테고리", options=CAT_OPTIONS, required=True),
        "출사일": st.column_config.DateColumn("출사일", format="YYYY-MM-DD"),
        "상태": st.column_config.SelectboxColumn("상태", options=STATUS_OPTIONS, required=True),
    }
    editor_kwargs = dict(
        column_config=col_config, hide_index=True, width="stretch",
        num_rows="fixed", key=f"editor_{start_ym}_{end_ym}",
    )

    if n_review > 0:
        st.warning(f"검토가 필요한 공지 {n_review}건이 표 위쪽에 있습니다.")
        edited = st.data_editor(editor_df, **editor_kwargs)
    else:
        with st.expander("분류 직접 보정 (선택) — 자동 분류 확인/수정", expanded=False):
            edited = st.data_editor(editor_df, **editor_kwargs)

    # ── 참석자 editor ──────────────────────────────────────────
    st.markdown("##### 참석자 보정 (후기 본문)")
    att_df = build_attendees_editor_df(reviews_sorted)
    att_config = {
        "검토": st.column_config.TextColumn("검토", disabled=True, width="small"),
        "작성일": st.column_config.DateColumn("작성일", disabled=True, format="YYYY-MM-DD"),
        "작성자": st.column_config.TextColumn("작성자", disabled=True, width="small"),
        "제목": st.column_config.TextColumn("제목", disabled=True, width="medium"),
        "본문": st.column_config.TextColumn(
            "본문", disabled=True, width="large",
            help="셀을 클릭하면 전체 본문을 펼쳐 볼 수 있습니다.",
        ),
        "참석자": st.column_config.TextColumn(
            "참석자(쉼표 구분)", help="실명을 쉼표로 구분", width="large"),
    }
    att_kwargs = dict(
        column_config=att_config, hide_index=True, width="stretch",
        num_rows="fixed", key=f"att_editor_{start_ym}_{end_ym}",
    )
    if reviews:
        if n_att_review > 0:
            st.warning(f"참석자가 비어 있는 후기 {n_att_review}건이 표 위쪽에 있습니다.")
            edited_att = st.data_editor(att_df, **att_kwargs)
        else:
            with st.expander("참석자 직접 보정 (선택)", expanded=False):
                edited_att = st.data_editor(att_df, **att_kwargs)
    else:
        edited_att = att_df
        st.caption("후기글이 없어 참석자 보정 단계는 건너뜁니다.")

    if st.button("✅ 이 분류·참석자로 분석 진행", type="primary"):
        final_posts = apply_triage(raw_posts, cat_a_sorted, edited, start_ym, end_ym)
        apply_attendees_edits(final_posts, reviews_sorted, edited_att)
        match_outings_with_reviews(final_posts)
        members = master.get("members", []) if isinstance(master, dict) else []
        banned = master.get("banned", set()) if isinstance(master, dict) else set()
        resolution = master.get("resolution", {}) if isinstance(master, dict) else {}
        join_aliases = master.get("join_aliases", {}) if isinstance(master, dict) else {}
        xlsx = build_excel(final_posts, photos, start_ym, end_ym,
                           members=members, banned=banned, resolution=resolution,
                           join_aliases=join_aliases)
        st.session_state["result"] = (start_ym, end_ym, final_posts, photos, xlsx,
                                       master, members, resolution)
        st.rerun()


def _ranking_df(rows: list[dict], count_col: str) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if not df.empty:
        df.insert(0, "순위", range(1, len(df) + 1))
    return df


def render_results(start_ym: int, end_ym: int, posts: list[dict],
                   photos: list[dict], xlsx: bytes, master: dict,
                   members: list[dict] | None = None,
                   resolution: dict[str, str] | None = None) -> None:
    period = period_label(start_ym, end_ym)
    months = month_axis(start_ym, end_ym)
    st.divider()
    st.subheader(f"③ {period} 인사이트")
    render_basis_box(posts, photos, period)

    kpis = compute_kpis(posts, photos)
    for col, (label, val) in zip(st.columns(len(kpis)), kpis.items()):
        col.metric(label, val)

    st.caption("📥 **엑셀 다운로드는 왼쪽 사이드바**의 *다운로드* 섹션에서.")

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
        "본인이 명단에 없거나 본문에서 이름을 찾지 못한 후기는 분류 검토 단계의 "
        "**참석자 보정** 표에서 수정할 수 있습니다. (사이드바에 멤버 명단 CSV를 올리면 정확도↑)",
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


def _tab_members(members: list[dict], posts: list[dict], photos: list[dict],
                  duplicates: set[str] | None = None,
                  months: list[int] | None = None) -> None:
    """🧑‍🤝‍🧑 활성 멤버 현황 — 유령/휴면 분류, 신규 가입 추이, 동명이인 마킹."""
    if not members:
        st.info("멤버 정보가 없습니다. 사이드바의 **API 수집**으로 받아오면 이 탭이 채워집니다.")
        return

    duplicates = duplicates or find_duplicate_member_names(members)
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
            same = [m for m in members if m.get("mn") == mn]
            for m in same:
                dup_rows.append({
                    "닉네임": f"⚠️ {mn}",
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
            "닉네임": (f"⚠️ {m['mn']}" if m["mn"] in duplicates else m["mn"]),
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

def render_sidebar() -> None:
    """사이드바: 데이터 소스(API/엑셀 업로드) + 매핑 CSV + 엑셀 다운로드 + 처음으로."""
    with st.sidebar:
        st.subheader("📥 데이터 소스")
        source = st.radio(
            "입력 방법", ["API 수집", "엑셀 업로드"],
            horizontal=True, key="data_source", label_visibility="collapsed",
        )

        if source == "API 수집":
            current_year = datetime.now().year
            years = list(range(current_year, current_year - 6, -1))
            st.caption("분석할 기간을 시작~종료로 지정합니다. 한 달만 보려면 시작·종료를 같게 두세요.")
            c1, c2 = st.columns(2)
            with c1:
                sy = st.selectbox("시작 년", years, key="api_start_y")
                sm = st.selectbox("시작 월", list(range(1, 13)), key="api_start_m")
            with c2:
                ey = st.selectbox("종료 년", years, key="api_end_y")
                em = st.selectbox("종료 월", list(range(1, 13)), index=11, key="api_end_m")

            start_ym, end_ym = sy * 100 + sm, ey * 100 + em
            valid_range = start_ym <= end_ym
            if valid_range:
                n_months = len(month_axis(start_ym, end_ym))
                st.caption(f"📅 **{period_label(start_ym, end_ym)}** · {n_months}개월")
                if n_months > 24:
                    st.warning("기간이 길면 API 페이지 한도에 걸려 과거 일부가 누락될 수 있습니다. "
                               "나눠서 수집한 뒤 엑셀을 각각 보관하는 편이 안전합니다.")
            else:
                st.error("종료가 시작보다 빠릅니다. 기간을 다시 지정하세요.")

            if st.button("분석 시작", type="primary", width="stretch",
                         disabled=not valid_range):
                progress_bar = st.progress(0.0, text="시작 준비 중…")
                with st.status("데이터 수집 중…", expanded=True) as status:
                    def on_progress(msg: str, pct: float) -> None:
                        progress_bar.progress(min(max(pct, 0.0), 1.0), text=msg)
                        st.write(msg)
                    try:
                        posts, photos = collect_data(start_ym, end_ym, on_progress=on_progress)
                        on_progress("멤버 목록 수집…", 0.95)
                        members, _ = collect_members()
                        banned = collect_banned_names()
                        active_mns = {m["mn"] for m in members if m.get("mn")}
                        joined_dates = [m["joined_at"] for m in members if m.get("joined_at")]
                        min_joined = min(joined_dates) if joined_dates else None
                        joins = collect_join_greetings(
                            progress=on_progress,
                            active_members=members,
                            min_joined_at=min_joined,
                        )
                        join_aliases = parse_join_name_aliases(joins, active_mns)
                    except Exception as e:  # noqa: BLE001
                        status.update(label="수집 실패", state="error")
                        st.error("수집 중 오류가 발생했습니다. (somoim API/네트워크 확인)")
                        st.exception(e)
                        st.stop()
                    progress_bar.progress(1.0, text="완료")
                    status.update(
                        label=(f"수집 완료 · 게시글 {len(posts)} / 사진 {len(photos)} "
                               f"/ 멤버 {len(members)} / 가입인사 자동 매핑 {len(join_aliases)}"),
                        state="complete",
                    )
                _set_data(start_ym, end_ym, posts, photos,
                           members=members, banned=banned, resolution=None,
                           join_aliases=join_aliases)
                st.rerun()
        else:
            st.caption("이전에 받은 **분석 엑셀**을 올리면 API 호출 없이 즉시 분석합니다.")
            f = st.file_uploader("엑셀 파일 (.xlsx)", type=["xlsx"], key="excel_upload")
            if f is not None and st.button("📥 불러오기", type="primary", width="stretch"):
                try:
                    bundle = load_excel_bundle(f.getvalue())
                except Exception as e:  # noqa: BLE001
                    st.error(f"엑셀 파일 오류: {e}")
                else:
                    _set_data(bundle["start_ym"], bundle["end_ym"],
                              bundle["posts"], bundle["photos"],
                              members=bundle.get("members") or [],
                              banned=bundle.get("banned") or set(),
                              resolution=bundle.get("resolution") or {},
                              join_aliases=bundle.get("join_aliases") or {})
                    st.success(
                        f"엑셀 로드 · 게시글 {len(bundle['posts'])} / 사진 {len(bundle['photos'])} "
                        f"· 멤버 {len(bundle.get('members') or [])} "
                        f"· 매핑 {len(bundle.get('resolution') or {})} "
                        f"· 가입인사 자동 {len(bundle.get('join_aliases') or {})}"
                    )
                    st.rerun()

        # ── 이름 매핑 CSV (어느 단계에서나 노출) ──
        if "data" in st.session_state:
            start_d, end_d, _p, _ph, _m, _b, res_uploaded, _ja = st.session_state["data"]
            current_res = (st.session_state.get("master", {}).get("resolution")
                           if isinstance(st.session_state.get("master"), dict) else None)
            export_res = current_res if current_res else res_uploaded
            st.divider()
            st.subheader("🧭 이름 매핑")
            if export_res:
                tag = period_tag(start_d, end_d)
                st.download_button(
                    "⬇️ 매핑 CSV 저장",
                    data=_resolution_to_csv(export_res),
                    file_name=f"다감노_{tag}_이름매핑.csv",
                    mime="text/csv", width="stretch",
                )
            else:
                st.caption("아직 저장된 매핑이 없습니다. ① 단계에서 매핑을 지정한 뒤 받아오세요.")
            up_csv = st.file_uploader("⬆️ 매핑 CSV 불러오기", type=["csv"],
                                       key="resolution_csv_upload")
            if up_csv is not None and st.button("매핑 적용", width="stretch"):
                try:
                    loaded = _parse_resolution_csv(up_csv.getvalue().decode("utf-8-sig"))
                except Exception as e:  # noqa: BLE001
                    st.error(f"CSV 형식 오류: {e}")
                else:
                    # 업로드 매핑은 data 튜플에 보존하고 master/result 클리어해서 Stage 1로 재진입
                    s_ym, e_ym, posts, photos, members, banned, _, join_aliases = st.session_state["data"]
                    _set_data(s_ym, e_ym, posts, photos,
                              members=members, banned=banned, resolution=loaded,
                              join_aliases=join_aliases)
                    st.success(f"{len(loaded)}개 매핑 적용 — 다시 ①부터 진행하세요.")
                    st.rerun()

        if "result" in st.session_state:
            start_r, end_r = st.session_state["result"][0], st.session_state["result"][1]
            xlsx_r = st.session_state["result"][4]
            tag = period_tag(start_r, end_r)
            st.divider()
            st.subheader("💾 다운로드")
            st.download_button(
                "📥 엑셀 (인사이트 + 원본)",
                data=xlsx_r,
                file_name=f"다감노_{tag}_분석.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )
            st.caption("이 엑셀을 다음에 그대로 업로드하면 API 호출 없이 같은 분석을 다시 볼 수 있어요.")

        if "data" in st.session_state:
            st.divider()
            if st.button("🔄 처음으로", width="stretch"):
                for k in ("data", "master", "result", "_collect_cache",
                          "api_start_y", "api_start_m", "api_end_y", "api_end_m",
                          "excel_upload", "resolution_csv_upload",
                          "_noise_pending", "_res_editor_nonce", "noise_multiselect"):
                    st.session_state.pop(k, None)
                st.rerun()


def main() -> None:
    st.set_page_config(page_title="다감노 분석", page_icon="📸", layout="wide")
    st.title("📸 다감노 분석")
    st.caption(f"{GROUP_NAME} 게시글·사진을 수집·검토하고 통계 엑셀을 생성합니다.")

    render_sidebar()

    if "data" not in st.session_state:
        st.info(
            "👈 사이드바에서 **API 수집**으로 데이터를 모으거나, "
            "이전에 다운로드한 **분석 엑셀**을 업로드해 주세요."
        )
        return

    start_ym, end_ym, posts, photos, members, banned, resolution_uploaded, join_aliases = \
        st.session_state["data"]

    if "result" in st.session_state:
        render_results(*st.session_state["result"])
    elif "master" in st.session_state:
        render_triage(start_ym, end_ym, posts, photos, st.session_state["master"])
    else:
        render_resolution(start_ym, end_ym, posts, photos, members,
                           banned, resolution_uploaded, join_aliases)


if __name__ == "__main__":
    main()
