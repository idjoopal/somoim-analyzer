"""카테고리 월별 집계 테스트 — 네트워크 무관.

streamlit_app의 순수 계산 헬퍼만 쓴다(st 위젯 호출 없음).
"""

from datetime import datetime

from streamlit_app import axis_labels, axis_values, category_monthly, monthly_table


def notice(pid, outing_date, category="풍경", canceled=False, posted=None):
    return {
        "id": pid, "author": "닉", "wid": "w1",
        "title": f"[{category}] {pid}", "body": "",
        "outing_date": outing_date,
        "posted_at": posted or datetime(2025, 9, 1, 10, 0),
        "cat": "A", "cat_label": "공지", "category": category,
        "is_outing": True, "is_canceled": canceled,
        "likes": 0, "comments": 0, "images": 0,
        "needs_review": False, "review_reason": "",
    }


# ═══════════════════════════════════════════════════════════════
# 다년 충돌 — 이번 작업의 핵심 회귀
# ═══════════════════════════════════════════════════════════════

def test_same_month_different_years_do_not_merge():
    """2025-03과 2026-03이 한 칸에 합쳐지면 안 된다."""
    posts = [notice("a", "2025-03-02"), notice("b", "2026-03-07")]
    months = [202503, 202603]
    rows, _ = category_monthly(posts, months)
    by_label = {r["월"]: r["공지 수"] for r in rows if r["카테고리"] == "풍경"}
    assert by_label["2025-03"] == 1
    assert by_label["2026-03"] == 1


def test_monthly_table_keeps_years_apart():
    posts = [notice("a", "2025-03-02"), notice("b", "2026-03-07"),
             notice("c", "2026-03-20")]
    mt = monthly_table(posts, photos=[])
    assert mt["진행 출사"][202503] == 1
    assert mt["진행 출사"][202603] == 2


# ═══════════════════════════════════════════════════════════════
# 축 채우기
# ═══════════════════════════════════════════════════════════════

def test_empty_months_stay_on_axis_as_zero():
    """활동이 없는 달도 축에서 사라지지 않아야 한다."""
    posts = [notice("a", "2026-01-10")]
    months = [202601, 202602, 202603]   # 한 해 안 → 라벨은 "1월" 형식
    rows, _ = category_monthly(posts, months)
    got = {r["월"]: r["공지 수"] for r in rows}
    assert got == {"1월": 1, "2월": 0, "3월": 0}


def test_axis_values_fills_gaps_with_zero():
    assert axis_values({202601: 3, 202603: 1}, [202601, 202602, 202603]) == [3, 0, 1]
    assert axis_values({}, [202605]) == [0]


def test_axis_labels_switch_on_multi_year():
    assert axis_labels([202601, 202602]) == ["1월", "2월"]
    assert axis_labels([202512, 202601]) == ["2025-12", "2026-01"]
    assert axis_labels([]) == []


# ═══════════════════════════════════════════════════════════════
# 제외 규칙 — 조용히 사라지지 않는지
# ═══════════════════════════════════════════════════════════════

def test_missing_outing_date_is_excluded_and_counted():
    posts = [notice("a", "2026-01-10"), notice("b", None)]
    rows, skipped = category_monthly(posts, [202601])
    assert sum(r["공지 수"] for r in rows) == 1
    assert skipped["출사일 미상"] == 1


def test_missing_category_is_excluded_and_counted():
    p = notice("b", "2026-01-10")
    p["category"] = None
    rows, skipped = category_monthly([notice("a", "2026-01-10"), p], [202601])
    assert sum(r["공지 수"] for r in rows) == 1
    assert skipped["카테고리 미상"] == 1


def test_exclude_canceled_toggle():
    posts = [notice("a", "2026-01-10"), notice("b", "2026-01-11", canceled=True)]

    rows, skipped = category_monthly(posts, [202601], exclude_canceled=False)
    assert sum(r["공지 수"] for r in rows) == 2
    assert skipped["취소 제외"] == 0

    rows, skipped = category_monthly(posts, [202601], exclude_canceled=True)
    assert sum(r["공지 수"] for r in rows) == 1
    assert skipped["취소 제외"] == 1


def test_only_cat_a_is_counted():
    review = notice("r", "2026-01-10")
    review["cat"] = "E"
    rows, _ = category_monthly([notice("a", "2026-01-10"), review], [202601])
    assert sum(r["공지 수"] for r in rows) == 1


def test_multiple_categories_are_separate_series():
    posts = [notice("a", "2026-01-10", category="풍경"),
             notice("b", "2026-01-11", category="인물"),
             notice("c", "2026-01-12", category="인물")]
    rows, _ = category_monthly(posts, [202601])
    got = {r["카테고리"]: r["공지 수"] for r in rows}
    assert got == {"풍경": 1, "인물": 2}


def test_empty_input_returns_no_rows():
    rows, skipped = category_monthly([], [202601, 202602])
    assert rows == []
    assert all(v == 0 for v in skipped.values())
