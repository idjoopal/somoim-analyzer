"""인사이트 집계 테스트 — 순수 함수라 네트워크·Streamlit 무관.

**신뢰도 카운트가 가장 중요하다.** 이 숫자가 틀리면 사용자는 "다 채웠다"고
믿고 덜 채워진 결과를 그대로 읽게 된다.
"""

from datetime import date, datetime

from core.store import (
    TAB_ATTENDEE_FIX,
    TAB_MEMBER_NAMES,
    TAB_NAME_MAP,
    TAB_POST_FIX,
)
from streamlit_app import (
    avg_attendance_trend,
    co_attendance,
    confidence_report,
    dormant_members,
    newcomer_settling,
)


def notice(pid, outing_date="2026-03-07", attendees=None, held=True, **kw):
    base = {
        "id": pid, "cat": "A", "author": "닉", "title": f"[풍경] {pid}",
        "outing_date": outing_date, "posted_at": datetime(2026, 3, 1),
        "category": "풍경", "is_outing": True, "is_canceled": False,
        "actually_held": held, "attendees": attendees or [],
        "needs_review": False,
    }
    base.update(kw)
    return base


def review(pid, attendees=None, matched="n1", **kw):
    base = {
        "id": pid, "cat": "E", "author": "닉", "title": f"후기 {pid}",
        "posted_at": datetime(2026, 3, 8), "attendees": attendees or [],
        "matched_outing_id": matched, "attendees_needs_review": False,
    }
    base.update(kw)
    return base


def member(mid, mn, joined=None, **kw):
    return {"mid": mid, "mn": mn, "joined_at": joined, "last_visit": None,
            "is_admin": False, "os": "", "push": False, **kw}


# ═══════════════════════════════════════════════════════════════
# 신뢰도 — 진짜 없는 것과 아직 안 채운 것을 구분한다
# ═══════════════════════════════════════════════════════════════

def _by_item(rows):
    return {r["항목"]: r["건수"] for r in rows}


def test_counts_come_from_the_sheet_not_a_second_guess():
    """화면과 시트가 어긋나면 안 된다.

    예전에는 여기서 판정을 다시 써서 시딩 조건과 조금씩 달랐다. 그래서
    "참석자 보정 7건 필요"라고 떠 있는데 **시트에는 헤더밖에 없어** 무엇을
    해야 할지 알 수 없는 상태가 됐다. 이제 미기입 행 수를 그대로 쓴다.
    """
    got = _by_item(confidence_report([], {
        TAB_MEMBER_NAMES: 3, TAB_ATTENDEE_FIX: 7,
        TAB_NAME_MAP: 2, TAB_POST_FIX: 1,
    }))
    assert got["실명 미기입 멤버"] == 3
    assert got["참석자 못 뽑은 후기"] == 7
    assert got["해소 안 된 이름"] == 2
    assert got["검토 대상 공지"] == 1


def test_empty_sheet_means_nothing_pending():
    assert all(r["건수"] == 0 for r in confidence_report([], {}))


def test_orphan_reviews_are_counted_from_posts():
    """시트에 자리가 없는 유일한 항목 — 사람이 채울 게 아니라 사실 통보다."""
    posts = [review("r1", matched=None), review("r2", matched="n1")]
    assert _by_item(confidence_report(posts, {}))["공지와 안 이어진 후기"] == 1


def test_every_row_says_where_to_fix_it():
    """숫자만 보여 주고 어디를 채우라는 말이 없으면 쓸모가 없다."""
    for r in confidence_report([], {}):
        assert r["어디서"] and r["설명"]


# ═══════════════════════════════════════════════════════════════
# 출사당 평균 참석 인원
# ═══════════════════════════════════════════════════════════════

def test_average_counts_outings_with_nobody_listed():
    """참석자를 못 뽑은 출사를 빼면 모임 규모가 실제보다 부풀려진다."""
    posts = [notice("n1", "2026-03-01", ["a", "b", "c", "d"]),
             notice("n2", "2026-03-08", [])]
    assert avg_attendance_trend(posts, [202603])[202603] == 2.0


def test_month_without_outings_is_none_not_zero():
    """0으로 그리면 '아무도 안 왔다'로 읽힌다 — 출사가 없던 것과 다르다."""
    got = avg_attendance_trend([notice("n1", "2026-03-01", ["a"])], [202602, 202603])
    assert got[202602] is None
    assert got[202603] == 1.0


def test_unmatched_outings_are_excluded_from_the_average():
    posts = [notice("n1", "2026-03-01", ["a", "b"]),
             notice("n2", "2026-03-08", [], held=False)]
    assert avg_attendance_trend(posts, [202603])[202603] == 2.0


def test_average_ignores_outings_outside_the_axis():
    got = avg_attendance_trend([notice("n1", "2025-01-05", ["a"])], [202603])
    assert got == {202603: None}


# ═══════════════════════════════════════════════════════════════
# 함께 간 사람
# ═══════════════════════════════════════════════════════════════

def test_pairs_are_symmetric_and_counted_once():
    posts = [notice("n1", "2026-03-01", ["나무", "바다"]),
             notice("n2", "2026-03-08", ["바다", "나무"])]   # 순서만 다름
    rows = co_attendance(posts)
    assert len(rows) == 1
    assert rows[0]["함께"] == 2


def test_solo_outing_makes_no_pair():
    assert co_attendance([notice("n1", attendees=["나무"])]) == []


def test_pair_row_shows_each_persons_total():
    posts = [notice("n1", "2026-03-01", ["나무", "바다"]),
             notice("n2", "2026-03-08", ["나무"])]
    row = co_attendance(posts)[0]
    assert row["나무"] == 2 and row["바다"] == 1


def test_duplicate_name_in_one_outing_does_not_pair_with_itself():
    rows = co_attendance([notice("n1", attendees=["나무", "나무", "바다"])])
    assert len(rows) == 1 and rows[0]["함께"] == 1


# ═══════════════════════════════════════════════════════════════
# 이탈 조짐 — 유령 멤버(전 기간 0건)와 다르다
# ═══════════════════════════════════════════════════════════════

def test_recently_active_member_is_not_dormant():
    posts = [notice("n1", "2026-03-01", ["나무"])]
    assert dormant_members(posts, [member("m1", "나무")],
                           as_of=date(2026, 3, 20)) == []


def test_member_quiet_for_months_is_flagged():
    posts = [notice("n1", "2026-01-05", ["나무"])]
    got = dormant_members(posts, [member("m1", "나무")], as_of=date(2026, 6, 1))
    assert [r["멤버"] for r in got] == ["나무"]


def test_never_attended_member_is_not_called_dormant():
    """한 번도 안 온 사람은 이탈이 아니라 미유입 — 가입 직후일 수도 있다."""
    posts = [notice("n1", "2026-01-05", ["나무"])]
    got = dormant_members(posts, [member("m1", "나무"), member("m2", "신입")],
                          as_of=date(2026, 6, 1))
    assert "신입" not in [r["멤버"] for r in got]


def test_dormant_needs_attendance_history_to_report_anything():
    assert dormant_members([], [member("m1", "나무")]) == []


# ═══════════════════════════════════════════════════════════════
# 신규 멤버 정착
# ═══════════════════════════════════════════════════════════════

def test_days_from_joining_to_first_attendance():
    posts = [notice("n1", "2026-03-11", ["나무"])]
    got = newcomer_settling([member("m1", "나무", datetime(2026, 3, 1))], posts)
    assert got[0]["가입→첫 참석(일)"] == 10


def test_member_who_never_came_has_no_first_attendance():
    """이 값이 비어 있는 사람 수가 곧 유입의 질이다."""
    got = newcomer_settling([member("m1", "신입", datetime(2026, 3, 1))], [])
    assert got[0]["첫 참석"] is None
    assert got[0]["가입→첫 참석(일)"] is None


def test_attendance_before_joining_date_is_not_reported_as_negative():
    """닉네임 재사용·가입일 보정 등으로 뒤집힐 수 있다 — 음수 일수는 무의미하다."""
    posts = [notice("n1", "2026-02-01", ["나무"])]
    got = newcomer_settling([member("m1", "나무", datetime(2026, 3, 1))], posts)
    assert got[0]["가입→첫 참석(일)"] is None


def test_member_without_a_join_date_is_skipped():
    assert newcomer_settling([member("m1", "나무", None)], []) == []
