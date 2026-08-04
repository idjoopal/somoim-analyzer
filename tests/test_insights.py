"""인사이트 집계 테스트 — 순수 함수라 네트워크·Streamlit 무관.

**신뢰도 카운트가 가장 중요하다.** 이 숫자가 틀리면 사용자는 "다 채웠다"고
믿고 덜 채워진 결과를 그대로 읽게 된다.
"""

from datetime import date, datetime, timedelta

from core.store import (
    TAB_ATTENDEE_FIX,
    TAB_MEMBER_NAMES,
    TAB_NAME_MAP,
    TAB_POST_FIX,
)
from streamlit_app import (
    BADGE_TITLES,
    FIXED_TITLE_NAMES,
    TITLE_LIMIT,
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
    assert row["사람 A"] == "나무" and row["사람 B"] == "바다"
    assert row["A 참석"] == 2 and row["B 참석"] == 1


def test_ratio_is_reported_from_both_sides():
    """"8번 함께"가 한쪽에겐 대부분이고 다른 쪽에겐 일부일 수 있다."""
    posts = [notice("n1", "2026-03-01", ["나무", "바다"]),
             notice("n2", "2026-03-08", ["바다"]),
             notice("n3", "2026-03-15", ["바다"]),
             notice("n4", "2026-03-22", ["바다"])]
    row = co_attendance(posts)[0]
    assert row["함께"] == 1
    assert row["A 기준"] == 100.0          # 나무는 한 번 갔고 그게 바다와 함께
    assert row["B 기준"] == 25.0           # 바다는 네 번 중 한 번


def test_duplicate_name_in_one_outing_does_not_pair_with_itself():
    rows = co_attendance([notice("n1", attendees=["나무", "나무", "바다"])])
    assert len(rows) == 1 and rows[0]["함께"] == 1


def test_columns_do_not_carry_user_names():
    """이름을 키로 쓰면 `pd.DataFrame`이 모든 행의 키를 합집합으로 모은다.

    쌍마다 사람이 다르면 컬럼이 쌍 수 × 4개까지 불어나고, 자기 행이 아닌
    칸은 전부 빈칸이 된다. **실제로 80칸짜리 표가 나왔다.**
    """
    from streamlit_app import CO_ATTENDANCE_COLS

    people = ["나무", "바다", "하늘", "구름", "노을", "안개"]
    posts = [notice(f"n{i}", "2026-03-0%d" % (i + 1), [people[i], people[i + 1]])
             for i in range(len(people) - 1)]
    rows = co_attendance(posts)
    assert len(rows) == 5

    keys = {k for r in rows for k in r}
    assert keys == set(CO_ATTENDANCE_COLS), keys
    assert not any(p in k for r in rows for k in r for p in people)


def test_pair_is_split_into_two_columns():
    """`나무 · 바다`로 붙여 두면 왼쪽 숫자가 누구 것인지 알 방법이 없다."""
    row = co_attendance([notice("n1", attendees=["나무", "바다"])])[0]
    assert row["사람 A"] == "나무" and row["사람 B"] == "바다"
    assert "·" not in row["사람 A"]


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
    rows, _ = newcomer_settling([member("m1", "나무", datetime(2026, 3, 1))], posts)
    assert rows[0]["가입→첫 참석(일)"] == 10


def test_members_who_joined_before_the_collected_period_are_excluded():
    """수집 전에 가입한 사람은 첫 참석이 데이터 밖일 수 있어 "N일 만에"가 거짓이다."""
    members = [member("m1", "고참", datetime(2024, 5, 1)),
               member("m2", "신입", datetime(2026, 3, 1))]
    rows, skipped = newcomer_settling(members, [], since_ym=202601)
    assert [r["멤버"] for r in rows] == ["신입"]
    assert skipped == 1                     # 조용히 빠지면 명단이 틀린 것처럼 보인다


def test_without_a_period_nobody_is_excluded():
    rows, skipped = newcomer_settling(
        [member("m1", "고참", datetime(2024, 5, 1))], [])
    assert len(rows) == 1 and skipped == 0


def test_member_who_never_came_has_no_first_attendance():
    """이 값이 비어 있는 사람 수가 곧 유입의 질이다."""
    rows, _ = newcomer_settling([member("m1", "신입", datetime(2026, 3, 1))], [])
    assert rows[0]["첫 참석"] is None
    assert rows[0]["가입→첫 참석(일)"] is None


def test_attendance_before_joining_date_is_not_reported_as_negative():
    """닉네임 재사용·가입일 보정 등으로 뒤집힐 수 있다 — 음수 일수는 무의미하다."""
    posts = [notice("n1", "2026-02-01", ["나무"])]
    rows, _ = newcomer_settling([member("m1", "나무", datetime(2026, 3, 1))], posts)
    assert rows[0]["가입→첫 참석(일)"] is None


def test_member_without_a_join_date_is_skipped():
    rows, _ = newcomer_settling([member("m1", "나무", None)], [])
    assert rows == []


# ═══════════════════════════════════════════════════════════════
# 테마사진 보정 — has_comment가 단일 게이트라는 전제가 실제로 성립하는가
#
# 여기가 이번 작업의 핵심이다. 한 곳만 뒤집어도 KPI·월별 추이·업로더 비율·
# 매트릭스·참여자 순위가 전부 따라와야 한다. 한 군데라도 안 따라오면
# 화면마다 다른 숫자를 말하게 된다.
# ═══════════════════════════════════════════════════════════════

def themed(pid, author="나무", posted=None, comments=2):
    return {"id": pid, "author": author, "wid": "w1",
            "posted_at": posted or datetime(2026, 3, 5),
            "likes": 3, "comments": comments, "has_comment": comments > 0,
            "url_large": "", "url_medium": "", "url_small": "", "url_thumb": ""}


def test_unmarking_flows_into_every_theme_aggregation():
    from core.store import apply_photo_corrections
    from streamlit_app import (compute_kpis, monthly_table, photo_user_ranking,
                               theme_matrix, theme_participant_ranking)

    photos = [themed("p1"), themed("p2")]
    before = compute_kpis([], photos)["테마 예상"]
    assert before == 2

    apply_photo_corrections(photos, {"photos": {"p1": True}})

    assert compute_kpis([], photos)["테마 예상"] == 1
    assert monthly_table([], photos)["테마사진 참가"][202603] == 1
    assert photo_user_ranking(photos)[0]["테마예상"] == 1
    assert theme_matrix(photos, [202603])[2].get(202603) == 1     # 참여 인원
    assert theme_participant_ranking(photos)[0]["테마사진"] == 1


def test_restoring_returns_the_exact_original_numbers():
    """되돌리기가 정확히 복귀하지 않으면 사용자는 실수를 되돌릴 수 없다."""
    from core.store import apply_photo_corrections
    from streamlit_app import compute_kpis

    def fresh():
        return [themed("p1"), themed("p2")]

    original = compute_kpis([], fresh())
    photos = fresh()
    apply_photo_corrections(photos, {"photos": {"p1": True}})
    restored = fresh()
    apply_photo_corrections(restored, {"photos": {"p1": False}})
    assert compute_kpis([], restored) == original


def test_marking_a_photo_that_was_never_themed_changes_nothing():
    from core.store import apply_photo_corrections
    photos = [themed("p1", comments=0)]
    assert apply_photo_corrections(photos, {"photos": {"p1": True}}) == 0
    assert photos[0]["has_comment"] is False


def test_no_flags_means_no_work():
    from core.store import apply_photo_corrections
    photos = [themed("p1")]
    assert apply_photo_corrections(photos, {}) == 0
    assert photos[0]["has_comment"] is True


# ═══════════════════════════════════════════════════════════════
# 가입인사 기준 정착·이탈
#
# 멤버 목록은 지금 남아 있는 사람뿐이라 거기서 센 월별 가입은 이탈이 많았던
# 달일수록 작아 보인다 — 정반대로 읽힌다. 가입인사는 사람이 나가도 남는다.
# ═══════════════════════════════════════════════════════════════

def greeting(pid, wid, author="닉", posted=None):
    return {"id": pid, "cat": "J", "wid": wid, "author": author,
            "posted_at": posted or datetime(2026, 3, 5),
            "title": "가입인사", "body": "잘 부탁드립니다"}


def test_departed_joiner_is_counted_even_though_they_left():
    """이게 존재 이유다 — 멤버 목록으로는 나간 사람을 셀 수 없다."""
    from streamlit_app import joiner_retention
    posts = [greeting("j1", "w1"), greeting("j2", "w2")]
    rows = joiner_retention(posts, [member("w1", "남은사람")], [202603])
    assert rows[0] == {"월": "3월", "가입": 2, "잔류": 1, "이탈": 1}


def test_retention_matches_by_user_id_not_nickname():
    """닉네임으로 맞추면 닉네임을 바꾼 사람이 나간 것으로 잡힌다."""
    from streamlit_app import joiner_retention
    posts = [greeting("j1", "w1", author="옛닉네임")]
    rows = joiner_retention(posts, [member("w1", "새닉네임")], [202603])
    assert rows[0]["잔류"] == 1 and rows[0]["이탈"] == 0


def test_months_without_greetings_are_zero_not_missing():
    from streamlit_app import joiner_retention
    rows = joiner_retention([greeting("j1", "w1")], [member("w1", "닉")],
                            [202602, 202603])
    assert [r["가입"] for r in rows] == [0, 1]


def test_only_join_greetings_count():
    """공지·후기 작성자는 가입 이벤트가 아니다."""
    from streamlit_app import joiner_retention
    posts = [greeting("j1", "w1"), notice("n1", "2026-03-01"), review("r1")]
    assert joiner_retention(posts, [member("w1", "닉")], [202603])[0]["가입"] == 1


def test_without_a_member_list_retention_is_not_guessed():
    """멤버를 모르면 전원 잔류로 보이는데, 그건 사실이 아니라 무지다."""
    from streamlit_app import joiner_retention
    assert joiner_retention([greeting("j1", "w1")], [], [202603]) == []


def test_departed_list_shows_how_long_they_stayed():
    """인사만 쓰고 사라진 것과 한참 활동하다 나간 것은 뜻이 전혀 다르다."""
    from streamlit_app import departed_joiners
    posts = [greeting("j1", "w9", author="떠난사람", posted=datetime(2026, 1, 1)),
             {"id": "n1", "cat": "A", "wid": "w9", "author": "떠난사람",
              "posted_at": datetime(2026, 3, 11)}]
    rows = departed_joiners(posts, [], [member("w1", "남은사람")])
    assert rows[0]["멤버"] == "떠난사람"
    assert rows[0]["활동 기간(일)"] == 69


def test_departed_list_excludes_current_members():
    from streamlit_app import departed_joiners
    posts = [greeting("j1", "w1", author="남은사람")]
    assert departed_joiners(posts, [], [member("w1", "남은사람")]) == []


def test_last_activity_includes_photos():
    """사진만 올리고 글은 안 쓰는 사람도 있다."""
    from streamlit_app import departed_joiners
    posts = [greeting("j1", "w9", posted=datetime(2026, 1, 1))]
    photos = [{"id": "p1", "wid": "w9", "author": "닉",
               "posted_at": datetime(2026, 2, 10)}]
    rows = departed_joiners(posts, photos, [member("w1", "다른사람")])
    assert rows[0]["마지막 활동"] == "2026-02-10"


# ═══════════════════════════════════════════════════════════════
# 표에 몇 개를 싣나 — 캡션과 값이 어긋나면 화면이 거짓말을 한다
# ═══════════════════════════════════════════════════════════════

def test_preference_label_shows_up_to_five():
    """2개만 보이던 시절엔 3위 이하가 잘린 줄 모르고 읽었다."""
    from collections import Counter

    from streamlit_app import PREF_TOP_N, top_category_label
    pref = Counter({"인물": 9, "풍경": 8, "GN": 7, "보정": 6,
                    "문화": 5, "인물&풍경": 4, "일반공지": 3})
    label = top_category_label(pref)
    assert label.count(",") == PREF_TOP_N - 1
    assert label.startswith("인물(9), 풍경(8)")
    assert "일반공지" not in label            # 7위는 잘린다


def test_preference_label_of_an_empty_counter():
    from collections import Counter

    from streamlit_app import top_category_label
    assert top_category_label(Counter()) == "—"


def _chain(n):
    """서로 다른 사람 두 명씩 짝지은 출사 n건 — 겹치지 않는 쌍 n개."""
    return [notice(f"n{i}", "2026-03-01", [f"사람{i}a", f"사람{i}b"])
            for i in range(n)]


def test_pair_table_is_capped_at_the_documented_number():
    from streamlit_app import CO_ATTENDANCE_TOP
    rows = co_attendance(_chain(CO_ATTENDANCE_TOP + 5))
    assert len(rows) == CO_ATTENDANCE_TOP


# ═══════════════════════════════════════════════════════════════
# 함께 간 사람 — 전역 상위와 개인 화면이 같은 규칙을 봐야 한다
# ═══════════════════════════════════════════════════════════════

def test_global_and_personal_views_agree_on_the_same_pair():
    """쌍 세는 규칙이 두 벌이면 참석 탭과 멤버 상세 탭이 다른 숫자를 말한다."""
    from streamlit_app import member_companions
    posts = [notice("n1", "2026-03-01", ["나무", "바다"]),
             notice("n2", "2026-03-08", ["나무", "바다"]),
             notice("n3", "2026-03-15", ["바다"])]
    pair = co_attendance(posts)[0]
    mine = member_companions("나무", posts)[0]
    assert mine["함께 간 사람"] == "바다"
    assert mine["함께"] == pair["함께"] == 2
    assert mine["내 기준"] == pair["A 기준"]
    assert mine["상대 참석"] == pair["B 참석"] == 3
    assert mine["상대 기준"] == pair["B 기준"]


def test_personal_view_finds_pairs_outside_the_global_top_n():
    """전역 상위 N에 못 드는 사람도 동행이 있다 — 이 헬퍼의 존재 이유."""
    from streamlit_app import CO_ATTENDANCE_TOP, member_companions
    # 상위를 가득 채울 만큼 자주 함께 간 쌍들 + 딱 한 번 간 조용한 쌍 하나
    posts = [notice(f"n{i}_{k}", "2026-03-01", [f"사람{i}a", f"사람{i}b"])
             for i in range(CO_ATTENDANCE_TOP) for k in range(2)]
    posts.append(notice("quiet", "2026-03-20", ["조용", "한사람"]))

    top = co_attendance(posts)
    assert "조용" not in {r["사람 A"] for r in top} | {r["사람 B"] for r in top}
    assert member_companions("조용", posts) == [
        {"함께 간 사람": "한사람", "함께": 1, "내 기준": 100.0,
         "상대 참석": 1, "상대 기준": 100.0}]


def test_companion_columns_do_not_carry_user_names():
    """`co_attendance`와 같은 이유 — 이름을 키로 쓰면 표가 옆으로 늘어난다."""
    from streamlit_app import COMPANION_COLS, member_companions
    posts = [notice("n1", "2026-03-01", ["나무", "바다"]),
             notice("n2", "2026-03-08", ["나무", "하늘"])]
    rows = member_companions("나무", posts)
    assert {k for r in rows for k in r} == set(COMPANION_COLS)


def test_companion_headers_differ_from_the_global_table():
    """헤더가 같으면 어느 화면을 보고 있는지 알 수 없다.

    렌더 테스트가 `사람 A` 컬럼으로 전역 표를 골라내기도 한다 — 개인 표가
    같은 헤더를 쓰면 엉뚱한 표를 집는다.
    """
    from streamlit_app import CO_ATTENDANCE_COLS, COMPANION_COLS
    assert "사람 A" in CO_ATTENDANCE_COLS
    assert not (set(COMPANION_COLS) & set(CO_ATTENDANCE_COLS) - {"함께"})


def test_a_solo_attendee_has_no_companions():
    from streamlit_app import member_companions
    assert member_companions("나무", [notice("n1", attendees=["나무"])]) == []


# ═══════════════════════════════════════════════════════════════
# 멤버 상세 — 한 사람의 숫자
# ═══════════════════════════════════════════════════════════════

def photo(pid, author="닉", posted=None, likes=0, comments=0, themed=False, **kw):
    return {"id": pid, "author": author, "wid": "w1",
            "posted_at": posted or datetime(2026, 3, 5),
            "likes": likes, "comments": comments, "has_comment": themed,
            "url_large": f"https://example.invalid/{pid}.png",
            "url_medium": f"https://example.invalid/{pid}m.png",
            "url_small": f"https://example.invalid/{pid}s.png",
            "url_thumb": f"https://example.invalid/{pid}n.png", **kw}


def test_options_keep_members_with_no_activity_at_all():
    """가입만 하고 아무것도 안 했다는 사실 자체가 확인할 값이다."""
    from streamlit_app import member_options
    opts = member_options([member("w1", "유령")], [], [])
    assert opts == [{"이름": "유령", "참석": 0, "게시글": 0, "사진": 0}]


def test_options_are_ordered_by_activity():
    from streamlit_app import member_options
    posts = [notice("n1", "2026-03-01", ["바쁜사람"])]
    opts = member_options([member("w1", "유령"), member("w2", "바쁜사람")],
                          posts, [])
    assert [o["이름"] for o in opts] == ["바쁜사람", "유령"]


def test_attendance_rate_denominator_is_matched_outings_only():
    """후기 없는 출사는 누가 갔는지 알 수 없다 — 분모에 넣으면 모두가 낮아진다."""
    from streamlit_app import member_profile
    posts = [notice("n1", "2026-03-01", ["나무"], held=True),
             notice("n2", "2026-03-08", [], held=False)]
    prof = member_profile("나무", posts, [], [member("w1", "나무")])
    assert prof["매칭 출사"] == 1
    assert prof["참석률"] == 100.0


def test_hosted_outings_include_canceled_ones():
    """취소를 빼면 '이 사람이 몇 번 펑을 냈나'를 볼 수 없다."""
    from streamlit_app import member_hosted_outings, member_profile
    posts = [notice("n1", "2026-03-01", author="주최"),
             notice("n2", "2026-03-08", author="주최", is_canceled=True, held=False)]
    rows = member_hosted_outings("주최", posts)
    assert [r["상태"] for r in rows] == ["취소", "진행"]      # 최신 출사일 순
    prof = member_profile("주최", posts, [], [member("w1", "주최")])
    assert prof["개최"] == 2 and prof["개최 취소"] == 1 and prof["취소율"] == 50.0


def test_attended_list_does_not_match_a_name_inside_another_name():
    """참석자를 이어 붙인 문자열로 거르면 `나무`가 `나무늘보`를 집는다."""
    from streamlit_app import member_attended_outings
    posts = [notice("n1", "2026-03-01", ["나무늘보"])]
    assert member_attended_outings("나무", posts) == []
    assert len(member_attended_outings("나무늘보", posts)) == 1


def test_photo_stats_count_only_that_persons_photos():
    from streamlit_app import member_profile
    photos = [photo("p1", "나무", likes=10, themed=True),
              photo("p2", "나무", likes=4, posted=datetime(2026, 4, 5)),
              photo("p3", "바다", likes=99)]
    prof = member_profile("나무", [], photos, [member("w1", "나무")])
    assert prof["사진"] == 2 and prof["사진 좋아요"] == 14
    assert prof["장당 좋아요"] == 7.0
    assert prof["테마사진"] == 1 and prof["테마 참여월"] == 1


def test_dormant_badge_uses_the_member_tabs_definition():
    """같은 사람이 한 화면에선 휴면이고 다른 화면에선 아니면 안 된다."""
    from streamlit_app import member_profile
    posts = [notice("n1", "2026-01-01", ["옛날사람"]),
             notice("n2", "2026-06-01", ["요즘사람"])]
    members = [member("w1", "옛날사람"), member("w2", "요즘사람")]
    dorm = {r["멤버"] for r in dormant_members(posts, members)}
    assert member_profile("옛날사람", posts, [], members)["휴면"] is ("옛날사람" in dorm)
    assert member_profile("옛날사람", posts, [], members)["휴면"] is True
    assert member_profile("요즘사람", posts, [], members)["휴면"] is False


def test_ghost_is_zero_everywhere_not_just_zero_attendance():
    from streamlit_app import member_profile
    members = [member("w1", "유령"), member("w2", "사진만")]
    photos = [photo("p1", "사진만")]
    assert member_profile("유령", [], photos, members)["유령"] is True
    assert member_profile("사진만", [], photos, members)["유령"] is False


def test_join_greeting_is_not_activity():
    """이 모임은 전원이 가입인사를 쓴다 — 세면 유령이 영원히 0명이 된다."""
    from streamlit_app import activity_authors
    posts = [notice("n1", "2026-03-01", author="공지자"),
             {"id": "j1", "cat": "J", "author": "인사만", "title": "가입인사",
              "posted_at": datetime(2026, 3, 2)}]
    got = activity_authors(posts, [])
    assert "공지자" in got and "인사만" not in got


def test_a_brand_new_member_is_not_a_ghost():
    """갓 들어온 사람에게 유령은 가혹하다 — 아직 나갈 출사가 안 열렸을 수 있다."""
    from streamlit_app import joined_recently
    months = [202603]                     # 기간 끝 = 2026-03-31
    assert joined_recently(datetime(2026, 3, 20), months) is True
    assert joined_recently(datetime(2026, 3, 2), months) is True     # 29일 전
    assert joined_recently(datetime(2026, 3, 1), months) is False    # 30일 전
    assert joined_recently(datetime(2026, 1, 5), months) is False
    assert joined_recently(None, months) is False
    assert joined_recently(datetime(2026, 3, 20), []) is False


def test_grace_period_counts_days_not_months():
    """달로 재면 **28일 된 사람이 유령이 된다** — 짧은 달에서 어긋난다.

    2026-01-31 가입, 기간 끝 2026-02-28 → 28일밖에 안 됐다. 그런데 "가입월이
    기간 마지막 달인가"로 재면 1월 ≠ 2월이라 걸러진다.
    """
    from streamlit_app import joined_recently
    assert joined_recently(datetime(2026, 1, 31), [202602]) is True
    # 반대쪽 — 딱 30일이면 유예가 끝난다(2026-03-31 → 2026-04-30).
    assert joined_recently(datetime(2026, 3, 31), [202604]) is False


def test_ghost_badge_and_member_tab_agree():
    """같은 사람이 한 화면에선 유령이고 다른 화면에선 아니면 안 된다."""
    from streamlit_app import (
        activity_authors, club_context, joined_recently, member_profile)
    months = [202603]
    posts = [notice("n1", "2026-03-01", ["참석자"]),
             {"id": "j1", "cat": "J", "author": "오래된유령", "title": "가입인사",
              "posted_at": datetime(2026, 3, 2)},
             {"id": "j2", "cat": "J", "author": "새내기", "title": "가입인사",
              "posted_at": datetime(2026, 3, 25)}]
    members = [member("w1", "참석자", joined=datetime(2025, 1, 1)),
               member("w2", "오래된유령", joined=datetime(2025, 1, 1)),
               member("w3", "새내기", joined=datetime(2026, 3, 25))]
    # 멤버 탭이 세는 방식 그대로
    act = activity_authors(posts, [])
    attended = {n for p in posts if p.get("cat") == "A"
                for n in p.get("attendees") or []}
    tab_ghosts = {m["mn"] for m in members
                  if m["mn"] not in act and m["mn"] not in attended
                  and not joined_recently(m.get("joined_at"), months)}
    assert tab_ghosts == {"오래된유령"}

    ctx = club_context(posts, [], members, months)
    badge = {m["mn"] for m in members
             if member_profile(m["mn"], posts, [], members, ctx)["유령"]
             and not member_profile(m["mn"], posts, [], members, ctx)["신입"]}
    assert badge == tab_ghosts


def test_a_newcomer_with_no_activity_gets_the_waiting_title_not_the_ghost_one():
    from streamlit_app import club_titles
    months = [202603]
    members = [member("w1", "새내기", joined=datetime(2026, 3, 25)),
               member("w2", "오래된유령", joined=datetime(2025, 1, 1))]
    got = club_titles([], [], members, months)
    assert [t["칭호"] for t in got["새내기"]] == ["아직 첫 출사 전"]
    assert [t["칭호"] for t in got["오래된유령"]] == ["유령 회원"]


def test_dropdown_post_count_excludes_join_greetings():
    """목록엔 `글 1`인데 들어가면 "유령"이면 같은 사람이 둘로 보인다."""
    from streamlit_app import member_options
    posts = [{"id": "j1", "cat": "J", "author": "인사만", "title": "가입인사",
              "posted_at": datetime(2026, 3, 2)}]
    opts = {o["이름"]: o for o in member_options([member("w1", "인사만")], posts, [])}
    assert opts["인사만"]["게시글"] == 0


def test_rank_says_where_the_number_sits():
    """'12회 참석'만으로는 그게 많은 건지 알 수 없다."""
    from streamlit_app import member_profile
    posts = [notice("n1", "2026-03-01", ["1등", "2등"]),
             notice("n2", "2026-03-08", ["1등"])]
    prof = member_profile("2등", posts, [], [member("w1", "2등")])
    assert prof["참석 순위"] == 2 and prof["참석 모수"] == 2


# ═══════════════════════════════════════════════════════════════
# 갤러리 — 올라온 사진 전부에 닿는다
# ═══════════════════════════════════════════════════════════════

def test_gallery_keeps_photos_without_comments():
    """테마 미리보기와 달리 갤러리는 전부를 보여 준다."""
    from streamlit_app import photos_by_month, themed_photos_by_month
    photos = [photo("p1", themed=True), photo("p2", themed=False)]
    assert len(photos_by_month(photos)[202603]) == 2
    assert len(themed_photos_by_month(photos)[202603]) == 1


def test_sort_orders_are_what_they_say():
    from streamlit_app import gallery_photos
    photos = [photo("p1", posted=datetime(2026, 3, 1), likes=1, comments=9),
              photo("p2", posted=datetime(2026, 3, 9), likes=5, comments=0)]
    ids = lambda s: [p["id"] for p in gallery_photos(photos, sort=s)]  # noqa: E731
    assert ids("최신순") == ["p2", "p1"]
    assert ids("오래된순") == ["p1", "p2"]
    assert ids("좋아요순") == ["p2", "p1"]
    assert ids("댓글순") == ["p1", "p2"]


def test_ties_break_deterministically():
    """정렬이 흔들리면 페이지를 넘길 때 같은 사진이 두 번 나오거나 사라진다."""
    from streamlit_app import gallery_photos
    photos = [photo(f"p{i}", likes=3) for i in range(10)]
    once = [p["id"] for p in gallery_photos(photos, sort="좋아요순")]
    twice = [p["id"] for p in gallery_photos(list(reversed(photos)), sort="좋아요순")]
    assert once == twice


def test_gallery_filters_by_uploader_and_theme():
    from streamlit_app import gallery_photos
    photos = [photo("p1", "나무", themed=True), photo("p2", "나무"),
              photo("p3", "바다")]
    # 업로드 시각이 같아 id로 끊긴다 — 그래서 순서가 정해진다.
    assert [p["id"] for p in gallery_photos(photos, author="나무")] == ["p1", "p2"]
    assert [p["id"] for p in gallery_photos(photos, themed_only=True)] == ["p1"]


def test_uploader_options_include_people_who_left():
    """나간 사람이 올린 사진도 찾을 수 있어야 한다 — 사진은 남아 있다."""
    from streamlit_app import photo_uploaders
    photos = [photo("p1", "떠난사람", is_active=False), photo("p2", "남은사람")]
    assert {u["작성자"] for u in photo_uploaders(photos)} == {"떠난사람", "남은사람"}


def test_page_slice_reports_pages_and_start():
    from streamlit_app import page_slice
    rows, pages, start = page_slice(list(range(45)), 2, 40)
    assert rows == list(range(40, 45)) and pages == 2 and start == 40


def test_page_slice_clamps_a_page_that_no_longer_exists():
    """필터를 좁히면 페이지 수가 준다 — 예전 번호가 남으면 빈 화면이 된다."""
    from streamlit_app import page_slice
    rows, pages, start = page_slice(list(range(5)), 9, 40)
    assert rows == list(range(5)) and pages == 1 and start == 0


def test_page_slice_of_an_empty_list_still_has_one_page():
    from streamlit_app import page_slice
    assert page_slice([], 1, 40) == ([], 1, 0)


# ═══════════════════════════════════════════════════════════════
# 공동 등수 — 같은 숫자면 같은 등수
# ═══════════════════════════════════════════════════════════════

def test_ties_share_a_rank_and_the_next_one_skips():
    """5회가 셋이면 7·8·9등이 아니라 전부 같은 등수여야 한다."""
    from streamlit_app import competition_rank
    scores = [("가", 9), ("나", 5), ("다", 5), ("라", 1)]
    assert competition_rank("가", scores) == (1, 4)
    assert competition_rank("나", scores) == (2, 4)
    assert competition_rank("다", scores) == (2, 4)
    assert competition_rank("라", scores) == (4, 4)      # 3등은 건너뛴다


def test_rank_does_not_depend_on_input_order():
    """부르는 쪽마다 정렬이 다르다 — `outing_user_ranking`은 합계 순이다."""
    from streamlit_app import competition_rank
    scores = [("가", 9), ("나", 5), ("다", 1)]
    assert competition_rank("나", scores) == competition_rank("나", scores[::-1])


def test_rank_of_someone_who_never_did_it():
    from streamlit_app import competition_rank
    assert competition_rank("없는사람", [("가", 1)]) == (None, 1)


def test_attendance_rank_no_longer_splits_equals():
    """지난 판에서는 목록 위치를 등수로 써서 동점자가 갈라졌다."""
    from streamlit_app import member_profile
    posts = [notice("n1", "2026-03-01", ["가", "나", "다"]),
             notice("n2", "2026-03-08", ["가"])]
    members = [member(f"w{i}", n) for i, n in enumerate("가나다")]
    ranks = {n: member_profile(n, posts, [], members)["참석 순위"] for n in "가나다"}
    assert ranks == {"가": 1, "나": 2, "다": 2}


def test_host_rank_counts_outings_that_happened_not_the_total():
    """펑을 많이 낸 사람이 합계로 앞서면 안 된다."""
    from streamlit_app import member_profile
    posts = [notice(f"c{i}", "2026-03-01", author="펑쟁이",
                    is_canceled=True, held=False) for i in range(5)]
    posts.append(notice("k1", "2026-03-02", ["펑쟁이"], author="펑쟁이"))
    posts += [notice(f"g{i}", "2026-03-0%d" % (i + 3), ["성실"], author="성실")
              for i in range(3)]
    members = [member("w1", "펑쟁이"), member("w2", "성실")]
    assert member_profile("성실", posts, [], members)["개최 순위"] == 1
    assert member_profile("펑쟁이", posts, [], members)["개최 순위"] == 2


def test_someone_who_only_ever_canceled_is_not_in_the_host_pool():
    """'펑 아닌 출사를 연 사람들 중 몇 등'이라야 말이 된다."""
    from streamlit_app import member_profile
    posts = [notice("c1", "2026-03-01", author="펑만", is_canceled=True, held=False),
             notice("g1", "2026-03-02", ["연사람"], author="연사람")]
    members = [member("w1", "펑만"), member("w2", "연사람")]
    prof = member_profile("펑만", posts, [], members)
    assert prof["개최 순위"] is None and prof["개최 모수"] == 1


def test_like_rank_ignores_people_with_almost_no_photos():
    """한 장 올려 좋아요 9를 받은 사람이 1등이면 그 등수는 뜻이 없다."""
    from streamlit_app import LIKE_RANK_MIN_PHOTOS, member_profile
    photos = [photo("lucky", "한장", likes=99)]
    photos += [photo(f"m{i}", "꾸준", likes=5)
               for i in range(LIKE_RANK_MIN_PHOTOS)]
    members = [member("w1", "한장"), member("w2", "꾸준")]
    assert member_profile("한장", [], photos, members)["좋아요 순위"] is None
    assert member_profile("꾸준", [], photos, members)["좋아요 순위"] == 1


# ═══════════════════════════════════════════════════════════════
# 자기 출사 후기율 — 내가 연 출사에 내가 후기를 썼나
# ═══════════════════════════════════════════════════════════════

def _hosted_with_review(pid, host, writer, day="01"):
    """개최 공지 + 그 공지에 매칭된 후기 한 쌍."""
    return [notice(pid, f"2026-03-{day}", ["누구"], author=host,
                   matched_review_id=f"r{pid}"),
            review(f"r{pid}", author=writer, matched=pid)]


def test_self_review_rate_counts_only_reviews_this_person_wrote():
    """후기를 꼭 개최자가 쓰는 것은 아니다 — 있는지가 아니라 누가 썼는지를 센다."""
    from streamlit_app import member_profile
    posts = (_hosted_with_review("a", "주최", "주최", "01")
             + _hosted_with_review("b", "주최", "딴사람", "08"))
    prof = member_profile("주최", posts, [], [member("w1", "주최")])
    assert prof["개최 진행"] == 2
    assert prof["자기 출사 후기"] == 1 and prof["자기 출사 후기율"] == 50.0


def test_self_review_rate_leaves_canceled_outings_out_of_the_denominator():
    """취소된 출사는 후기를 쓸 일이 없다 — 분모에 넣으면 펑 낸 사람만 손해다."""
    from streamlit_app import member_profile
    posts = _hosted_with_review("a", "주최", "주최", "01")
    posts.append(notice("c1", "2026-03-08", author="주최",
                        is_canceled=True, held=False))
    prof = member_profile("주최", posts, [], [member("w1", "주최")])
    assert prof["개최"] == 2 and prof["개최 진행"] == 1
    assert prof["자기 출사 후기율"] == 100.0


def _ctx_on(day, posts, photos, members):
    """오늘을 고정한 ctx — 미래 판정의 경계를 테스트가 붙잡는다."""
    from streamlit_app import club_context
    ctx = club_context(posts, photos, members, [202603])
    ctx["오늘"] = day
    return ctx


def test_outings_not_yet_held_are_out_of_the_review_denominator():
    """아직 안 다녀온 출사에 후기가 없는 것은 당연하다.

    분모에 넣으면 **공지를 미리 올리는 사람일수록 후기율이 떨어진다** —
    부지런한 쪽이 벌을 받는다.
    """
    from streamlit_app import member_profile
    posts = _hosted_with_review("a", "주최", "주최", "01")
    posts.append(notice("future", "2026-03-28", author="주최", held=False))
    members = [member("w1", "주최")]
    ctx = _ctx_on(date(2026, 3, 10), posts, [], members)
    prof = member_profile("주최", posts, [], members, ctx)
    assert prof["개최 진행"] == 2, "몇 건 열었나는 미래 출사도 연 것이다"
    assert prof["자기 출사 후기 분모"] == 1
    assert prof["자기 출사 후기율"] == 100.0


def test_an_outing_already_held_stays_in_the_denominator():
    """다녀온 뒤로는 후기를 쓸 수 있었으니 분모에 남는다."""
    from streamlit_app import member_profile
    posts = _hosted_with_review("a", "주최", "주최", "01")
    posts.append(notice("done", "2026-03-05", author="주최", held=False))
    members = [member("w1", "주최")]
    prof = member_profile("주최", posts, [], members,
                          _ctx_on(date(2026, 3, 10), posts, [], members))
    assert prof["자기 출사 후기 분모"] == 2 and prof["자기 출사 후기율"] == 50.0


def test_an_outing_with_no_date_stays_in_the_denominator():
    """날짜를 모르는 것과 미래인 것은 다르다 — 모르면 남긴다."""
    from streamlit_app import member_profile
    posts = _hosted_with_review("a", "주최", "주최", "01")
    posts.append(notice("undated", None, author="주최", held=False))
    members = [member("w1", "주최")]
    prof = member_profile("주최", posts, [], members,
                          _ctx_on(date(2026, 3, 10), posts, [], members))
    assert prof["자기 출사 후기 분모"] == 2


# ═══════════════════════════════════════════════════════════════
# 상대 기준 — 고정값이면 아무도 못 받거나 전원이 받는다
# ═══════════════════════════════════════════════════════════════

def test_a_tiny_pool_gives_the_title_to_nobody():
    """세 사람뿐인 판의 '상위 25%'는 1등 한 명을 돌려 말한 것뿐이다."""
    from streamlit_app import top_share
    tiny = [("가", 9), ("나", 5), ("다", 1)]
    assert top_share("가", tiny, 0.30) is False


def test_everyone_tied_means_everyone_is_in_the_top_share():
    """같은 값인데 한 명만 빼면 그게 공동 등수가 고친 바로 그 문제다."""
    from streamlit_app import top_share
    flat = [(n, 5) for n in "가나다라마"]
    assert all(top_share(n, flat, 0.30) for n in "가나다라마")


def test_top_share_needs_a_minimum_absolute_value_too():
    """전원이 한 장씩 올린 기간에는 상위 30%도 그냥 한 장이다."""
    from streamlit_app import top_share
    scores = [(n, 1) for n in "가나다라마"]
    assert top_share("가", scores, 0.30, min_value=2) is False


def test_bottom_share_finds_the_quiet_end():
    from streamlit_app import bottom_share
    scores = [("가", 9), ("나", 7), ("다", 5), ("라", 1)]
    assert bottom_share("라", scores, 0.25) is True
    assert bottom_share("가", scores, 0.25) is False


# ═══════════════════════════════════════════════════════════════
# 🏆 칭호
# ═══════════════════════════════════════════════════════════════

def _club(posts, photos, members, months=None, 벙포함=False):
    """(이름 → 칭호 목록). 화면이 부르는 것과 같은 함수를 쓴다.

    **정원이 있어 한 사람만 따로 낼 수 없다** — 같은 칭호를 몇 명이 받는지
    알아야 자르기 때문에 전원을 함께 낸다.
    """
    from streamlit_app import club_titles
    return club_titles(posts, photos, members, months or [202603],
                       벙포함=벙포함)


def _names(titles):
    return [t["칭호"] for t in titles]


def test_a_member_with_no_activity_gets_only_the_ghost_title():
    posts = [notice("n1", "2026-03-01", ["활동가"])]
    members = [member("w1", "활동가"), member("w2", "유령", datetime(2024, 1, 1))]
    assert _names(_club(posts, [], members)["유령"]) == ["유령 회원"]


def test_a_join_greeting_is_not_activity():
    """이 모임은 가입인사를 안 쓰면 12시간 안에 강퇴한다 — **전원이 쓴다.**

    그것까지 활동으로 세면 유령 멤버가 구조적으로 영원히 0명이 된다.
    실제 데이터에서 활동 0건인 11명이 아무 칭호도 못 받고 있었다.
    """
    posts = [notice("n1", "2026-03-01", ["활동가"]),
             {"id": "j1", "cat": "J", "author": "인사만", "wid": "w2",
              "title": "가입인사", "body": "", "outing_date": None,
              "posted_at": datetime(2026, 3, 2), "category": None,
              "is_canceled": False, "likes": 0, "comments": 0, "images": 0}]
    members = [member("w1", "활동가"), member("w2", "인사만", datetime(2024, 1, 1))]
    assert _names(_club(posts, [], members)["인사만"]) == ["유령 회원"]


def test_someone_who_just_joined_is_not_called_a_ghost():
    """가입한 지 한 달도 안 됐으면 아직 첫 출사가 안 열렸을 수도 있다."""
    posts = [notice("n1", "2026-03-01", ["활동가"])]
    members = [member("w1", "활동가"), member("w2", "새로온분", datetime(2026, 3, 5))]
    assert _names(_club(posts, [], members, [202602, 202603])["새로온분"]) \
        == ["아직 첫 출사 전"]


def test_titles_are_capped_and_sorted_by_rarity():
    """칭호가 줄줄이 달리면 아무것도 특별해 보이지 않는다."""
    from streamlit_app import TITLE_LIMIT
    posts, members = _busy_club()
    titles = _club(posts, _busy_photos(), members)["으뜸"]
    assert len(titles) <= TITLE_LIMIT
    assert [t["우선"] for t in titles] == sorted((t["우선"] for t in titles),
                                                reverse=True)


def test_only_one_title_per_metric():
    """사진 1등이 `다작왕`·`부지런한 업로더`로 두 칸을 먹으면 안 된다."""
    posts, members = _busy_club()
    for titles in _club(posts, _busy_photos(), members).values():
        지표 = [t["지표"] for t in titles]
        assert len(지표) == len(set(지표)), 지표


def _busy_club():
    """여섯 명이 활동하는 판 — 칭호가 여러 종류 붙을 만큼."""
    people = ["으뜸", "버금", "셋째", "넷째", "다섯", "여섯"]
    posts = []
    for i in range(12):
        # 으뜸은 전부 참석, 뒤로 갈수록 드물게.
        att = [p for j, p in enumerate(people) if i % (j + 1) == 0]
        posts.append(notice(f"n{i}", "2026-03-%02d" % (i + 1), att,
                            author=people[i % 3],
                            category=["풍경", "인물", "GN", "인물&풍경"][i % 4]))
    return posts, [member(f"w{i}", p) for i, p in enumerate(people)]


def _busy_photos():
    out = []
    for i, p in enumerate(["으뜸", "버금", "셋째", "넷째", "다섯", "여섯"]):
        for j in range(20 - i * 3):
            out.append(photo(f"ph{p}{j}", p, likes=10 - i,
                             themed=(j % 3 == 0)))
    return out


def test_a_combo_partner_never_also_gets_the_follower_title():
    """서로 붙어 다니는 것과 한쪽이 쫓아다니는 것은 다른 얘기다."""
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["나무", "바다"])
             for i in range(4)]
    members = [member("w1", "나무"), member("w2", "바다")]
    got = _names(_club(posts, [], members)["나무"])
    assert "바다님과 2인 1조" in got
    assert not any("오늘도 뵙네요" in t for t in got)


def test_the_follower_title_names_the_person_followed():
    """이 사람 출사만 골라 간다 — 상대의 전체 출석률보다 확실히 높다."""
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["따라", "인기"])
             for i in range(4)]
    # 인기는 12건 중 9건에 나온다 → 상대 기준 44%라 콤비는 아니고,
    # 따라의 100%는 인기의 출석률 75%를 25%p 웃돈다.
    posts += [notice(f"s{i}", "2026-04-%02d" % (i + 1), ["인기", "남들"])
              for i in range(5)]
    posts += [notice(f"t{i}", "2026-05-%02d" % (i + 1), ["남들"])
              for i in range(3)]
    members = [member("w1", "따라"), member("w2", "인기"), member("w3", "남들")]
    assert "인기님 오늘도 뵙네요" in _names(_club(posts, [], members)["따라"])


def test_nobody_follows_the_person_who_goes_to_everything():
    """한 사람이 거의 모든 출사에 나오면 **누구의 내 기준이든 높아진다.**

    실제 데이터에서 한 사람에게 17명이 붙었다. 따라다니는 게 아니라 확률이다.
    """
    people = ["가", "나", "다", "라", "마"]
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["항상", people[i % 5]])
             for i in range(10)]
    members = [member("w0", "항상")] + [member(f"w{i+1}", p)
                                       for i, p in enumerate(people)]
    for p in people:
        assert not any("오늘도 뵙네요" in t for t in _names(_club(posts, [], members)[p]))


def test_a_category_regular_is_measured_against_that_categorys_outings():
    """**분모는 "내 참석"이 아니라 "그 카테고리 출사 전부"다.**

    예전에는 "내가 간 출사의 75%가 풍경"이었는데, 이 모임은 인물&풍경이
    압도적이라 나머지 카테고리로는 그 비율이 나올 수가 없었다.
    """
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["풍경러"], category="풍경")
             for i in range(5)]
    posts += [notice(f"m{i}", "2026-03-2%d" % i, ["남들"], category="인물")
              for i in range(4)]
    members = [member("w1", "풍경러"), member("w2", "남들")]
    assert "풍경 사냥꾼" in _cands("풍경러", posts, [], members)


def test_a_small_category_can_earn_a_title_now():
    """문화 출사가 네 번뿐이어도 그중 셋에 나왔으면 그 사람이 문화 담당이다.

    옛 기준(내 참석의 75%)으로는 주력 카테고리를 함께 다니는 순간 불가능해
    `문화?시민`·`GN 마니아`·`풍경 사냥꾼`이 전부 0명이었다.
    """
    posts = [notice(f"c{i}", "2026-03-0%d" % (i + 1), ["문화인"], category="문화")
             for i in range(3)]
    posts.append(notice("c9", "2026-03-09", ["남들"], category="문화"))
    # 주력 카테고리도 가끔 간다(12건 중 3건). 비율이 가장 높은 카테고리
    # 하나만 뽑으므로 문화(75%)가 인물&풍경(25%)을 이긴다.
    posts += [notice(f"p{i}", "2026-03-1%d" % i,
                     ["문화인", "남들"] if i < 3 else ["남들"],
                     category="인물&풍경") for i in range(12)]
    members = [member("w1", "문화인"), member("w2", "남들")]
    assert "문화?시민" in _cands("문화인", posts, [], members)


def test_dropping_by_a_category_now_and_then_earns_nothing():
    """스무 건 중 세 건이면 그 카테고리 사람이라고 할 수 없다."""
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1),
                    ["가끔"] if i < 3 else ["남들"], category="풍경")
             for i in range(20)]
    members = [member("w1", "가끔"), member("w2", "남들")]
    got = _cands("가끔", posts, [], members)
    assert not any("사냥꾼" in t or "마니아" in t for t in got), got


def test_a_category_with_almost_no_outings_is_ignored():
    """두세 건뿐인 카테고리는 한 번만 나와도 비율이 튄다."""
    posts = [notice(f"g{i}", "2026-03-0%d" % (i + 1), ["운좋"], category="GN")
             for i in range(3)]
    posts += [notice(f"p{i}", "2026-03-1%d" % i, ["운좋", "남들"],
                     category="인물&풍경") for i in range(9)]
    members = [member("w1", "운좋"), member("w2", "남들")]
    assert "GN 마니아" not in _cands("운좋", posts, [], members)


def test_nothing_matches_means_no_titles():
    """조건에 안 걸리면 빈 목록 — 화면은 그때 구역을 안 그린다.

    한 번 왔다 만 사람이라도 **최근에** 왔으면 `새싹`이 붙는다. 아무것도 안
    붙는 자리는 "예전에 잠깐 왔고 그 뒤로 조용한" 사람이다.
    """
    posts = [notice("first", "2026-01-05", ["남", "들"]),
             notice("n1", "2026-01-15", ["잠깐", "남"]),
             notice("n2", "2026-03-02", ["남", "들"]),
             notice("n3", "2026-03-03", ["남", "들"])]
    members = [member(f"w{i}", n) for i, n in enumerate(["잠깐", "남", "들"])]
    assert _club(posts, [], members, [202601, 202602, 202603])["잠깐"] == []


def test_passing_a_context_does_not_change_the_answer():
    """`ctx`는 속도를 위한 통로일 뿐 — 값이 달라지면 두 화면이 어긋난다."""
    from streamlit_app import club_context, member_companions, member_profile
    posts, members = _busy_club()
    photos = _busy_photos()
    ctx = club_context(posts, photos, members)
    assert (member_profile("으뜸", posts, photos, members)
            == member_profile("으뜸", posts, photos, members, ctx))
    assert (member_companions("으뜸", posts)
            == member_companions("으뜸", posts, ctx["쌍"]))


# ═══════════════════════════════════════════════════════════════
# 정원 — 스무 명이 받는 것은 칭호가 아니라 그 모임의 평균이다
# ═══════════════════════════════════════════════════════════════

def _cands_full(name, posts, photos, members, months=None):
    """`_cands`와 같되 칭호 이름이 아니라 **후보 전체**를 돌려준다."""
    from streamlit_app import (_title_candidates, club_context,
                               member_companions, member_profile)
    ctx = club_context(posts, photos, members, months or [202603])
    return _title_candidates(
        name, member_profile(name, posts, photos, members, ctx),
        member_companions(name, posts, ctx["쌍"]), posts, photos,
        months or [202603], ctx)


def _cands(name, posts, photos, members, months=None):
    """정원·3개 제한을 걸기 **전**의 후보 이름들.

    규칙 하나하나("이 조건이면 붙나")는 여기서 본다. 최종 목록은 지표당 하나와
    상위 3개로 잘려 있어, 규칙이 맞아도 다른 칭호에 밀려 안 보일 수 있다.
    """
    from streamlit_app import (_title_candidates, club_context,
                               member_companions, member_profile)
    ctx = club_context(posts, photos, members, months or [202603])
    return [t["칭호"] for t in _title_candidates(
        name, member_profile(name, posts, photos, members, ctx),
        member_companions(name, posts, ctx["쌍"]), posts, photos,
        months or [202603], ctx)]


def _old_crowd(n=30):
    """전원이 2025-06 이전에 가입해 전원이 참석하는 판 — 서른 명이 조건을 만족한다."""
    people = [f"고참{i:02d}" for i in range(n)]
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), people) for i in range(5)]
    members = [member(f"w{i}", p, datetime(2024, 1 + i % 12, 1 + i % 28))
               for i, p in enumerate(people)]
    return posts, members


def _cands_all(posts, photos, members, months=None):
    """전원의 후보 `{이름: [칭호…]}` — ctx를 **한 번만** 만든다.

    `_cands`는 부를 때마다 ctx를 새로 만들어, 수십 명한테 돌리면 그것만으로
    몇 초가 든다.
    """
    from streamlit_app import (_title_candidates, club_context,
                               member_companions, member_profile)
    months = months or [202603]
    ctx = club_context(posts, photos, members, months)
    return {m["mn"]: [t["칭호"] for t in _title_candidates(
        m["mn"], member_profile(m["mn"], posts, photos, members, ctx),
        member_companions(m["mn"], posts, ctx["쌍"]), posts, photos,
        months, ctx)] for m in members}


def _uneven_crowd(n=80):
    """참석 횟수가 저마다 다른 판 — `i`번은 `i+1`회 나온다.

    `_old_crowd`는 전원이 똑같이 나와 강도가 전부 동점이라, **정원이 무엇으로
    줄을 세우는지**는 이 판으로만 볼 수 있다. 여든 명인 것은 `프로 참석러`가
    상위 15%여서, 후보가 정원 9명을 넘으려면 그만큼 필요하기 때문이다.
    """
    people = [f"고참{i:02d}" for i in range(n)]
    day = date(2026, 3, 1)
    posts = [notice(f"n{j}", str(day + timedelta(days=j)), people[j:])
             for j in range(n)]
    members = [member(f"w{i}", p, datetime(2024, 1 + i % 12, 1 + i % 28))
               for i, p in enumerate(people)]
    return posts, members


def test_no_title_goes_over_its_quota():
    """실제 데이터에서 `인풍 애호가`가 94명 중 20명에게 붙었다."""
    from collections import Counter as C

    from streamlit_app import BADGE_TITLES, TITLE_QUOTA_DEFAULT
    posts, members = _old_crowd()
    counts = C(t["칭호"] for ts in _club(posts, [], members).values() for t in ts)
    assert counts["이게 본업이에요"] == TITLE_QUOTA_DEFAULT
    over = {k: v for k, v in counts.items()
            if v > TITLE_QUOTA_DEFAULT and k not in BADGE_TITLES}
    assert not over, over


def test_a_badge_title_ignores_the_quota():
    """배지형은 정원을 안 탄다 — 서른 명이 자격이면 서른 명이 받는다.

    실데이터에서 `아이고 어르신` 자격자 14명 중 한 명이, 칭호가 하나뿐인데도
    정원 9명에 잘렸다. 가입일이 가장 늦다는 이유였다 — 활동으로 진 것이
    아니라 자리가 없어서 진 것이라 규칙이 잘못이었다.
    """
    from collections import Counter as C

    from streamlit_app import TITLE_QUOTA_DEFAULT
    posts, members = _old_crowd(30)
    counts = C(t["칭호"] for ts in _club(posts, [], members).values() for t in ts)
    assert counts["아이고 어르신"] == 30 > TITLE_QUOTA_DEFAULT


def test_the_quota_keeps_the_strongest():
    """자를 때는 그 칭호가 재는 바로 그 숫자로 줄을 세운다 — 여기서는 참석 수."""
    posts, members = _uneven_crowd()
    att = {m["mn"]: sum(m["mn"] in p["attendees"] for p in posts)
           for m in members}
    got = _club(posts, [], members)
    kept = [n for n, ts in got.items()
            if any(t["칭호"] == "프로 참석러" for t in ts)]
    # 후보이면서 못 받은 사람 = 정원에 잘린 사람. 1등은 `이게 본업이에요`로
    # 빠지므로 후보 목록에서 직접 캔다.
    cand = _cands_all(posts, [], members)
    cut = [n for n, ts in cand.items()
           if "프로 참석러" in ts and n not in kept]
    assert kept and cut
    assert min(att[k] for k in kept) >= max(att[c] for c in cut)


def test_a_metric_keeps_more_than_one_candidate():
    """같은 지표의 후보를 elif로 묶으면 **위쪽이 정원에 잘릴 때 대체가 없다.**

    `감노 때부터 계셨네`가 잘린 사람은 같은 연차 지표의 `새싹`으로 떨어질 수
    있어야 하므로, 후보 단계에서는 둘 다 남아 있어야 한다.
    """
    posts, members = _old_crowd(4)
    연차 = [t for t in _cands(members[0]["mn"], posts, [], members)
           if t in ("아이고 어르신", "새싹")]
    assert set(연차) == {"아이고 어르신", "새싹"}, 연차


def test_a_cut_person_still_keeps_titles_from_other_metrics():
    """한 칭호에서 밀렸다고 다른 지표까지 사라지면 안 된다."""
    posts, members = _old_crowd(12)
    got = _club(posts, [], members)
    cut = [m["mn"] for m in members
           if "이게 본업이에요" not in _names(got[m["mn"]])]
    assert cut, "정원에 밀린 사람이 없다 — 픽스처가 잘못됐다"
    assert all(got[n] for n in cut), [n for n in cut if not got[n]]


# ═══════════════════════════════════════════════════════════════
# 배지형 칭호 — 활동에 밀려 사라지면 안 되는 사실
# ═══════════════════════════════════════════════════════════════

def _busy_oldtimer(n=6):
    """세 칸이 꽉 차고도 남을 만큼 활동하는 고참 한 명 + 들러리들."""
    me = "고참"
    others = [f"동료{i}" for i in range(n)]
    posts = [notice(f"n{j}", "2026-03-%02d" % (j + 1), [me, *others],
                    author=me) for j in range(10)]
    members = [member("w0", me, datetime(2021, 6, 22))]
    members += [member(f"w{i + 1}", p, datetime(2025, 12, 1))
                for i, p in enumerate(others)]
    return posts, members


def test_a_badge_title_survives_a_full_three_slots():
    """세 칸이 다 차도 배지형은 나온다.

    실데이터에서 가장 활동이 많은 분이 95·93·90짜리 셋으로 칸을 채워
    `아이고 어르신`(79)이 밀려 있었다. 2021년부터 자리를 지켜 온 것이 요즘
    부지런하다는 이유로 가려지면 안 된다.
    """
    posts, members = _busy_oldtimer()
    got = _names(_club(posts, [], members)["고참"])
    assert "아이고 어르신" in got, got
    assert len(got) > TITLE_LIMIT, got


def test_a_badge_title_does_not_eat_a_slot():
    """배지형을 받아도 기간 칭호는 여전히 셋까지 받는다."""
    posts, members = _busy_oldtimer()
    got = _club(posts, [], members)["고참"]
    기간 = [t for t in got if t["칭호"] not in BADGE_TITLES]
    assert len(기간) == TITLE_LIMIT, _names(got)


def test_a_badge_title_leaves_its_metric_to_the_others():
    """배지형은 지표 경쟁에서도 빠진다.

    `아이고 어르신`과 `돌아오세요`는 둘 다 `연차` 지표다. 지표당 하나 규칙에
    묶여 있으면, 오래 계신 분이 한동안 안 나오셨다는 사실이 통째로 가려진다.
    """
    # 다섯 번 나오시다 넉 달째 조용한 고참 한 분. 카테고리를 흩어 놓은 것은
    # 한 갈래로 몰리면 카테고리 칭호가 붙어 칸을 먹기 때문이다.
    me, others = "고참", [f"동료{i}" for i in range(4)]
    cats = ["풍경", "인물", "GN", "인물&풍경", "풍경"]
    early = ["2026-03-07", "2026-03-14", "2026-03-21", "2026-03-28",
             "2026-04-04"]
    late = ["2026-08-01", "2026-08-08", "2026-08-15"]
    posts = [notice(f"n{j}", d, [me, *others], author="동료0",
                    category=cats[j], title=f"[{cats[j]}] n{j}")
             for j, d in enumerate(early)]
    posts += [notice(f"m{j}", d, others, author="동료0", category=cats[j],
                     title=f"[{cats[j]}] m{j}") for j, d in enumerate(late)]
    members = [member("w0", me, datetime(2021, 6, 22))]
    members += [member(f"w{i + 1}", p, datetime(2025, 12, 1))
                for i, p in enumerate(others)]
    months = [202603, 202604, 202605, 202606, 202607, 202608]

    got = _names(_club(posts, [], members, months)[me])
    연차 = [t for t in got if t in ("아이고 어르신", "돌아오세요")]
    assert set(연차) == {"아이고 어르신", "돌아오세요"}, got


def test_a_badge_title_ignores_the_period():
    """기간을 좁혀도 받는 사람이 그대로다 — 배지형의 자격 요건.

    `아이고 어르신`은 가입일로만 재서 이 성질을 지킨다. 옆자리 `첫 출사 못
    참지`는 `첫등장`을 써서 기간을 좁히면 사라진다 — 그래서 배지형이 아니다.
    """
    people = [f"고참{i:02d}" for i in range(6)]
    early = [notice(f"e{j}", "2026-01-%02d" % (j + 1), people) for j in range(3)]
    late = [notice(f"l{j}", "2026-03-%02d" % (j + 1), people) for j in range(3)]
    members = [member(f"w{i}", p, datetime(2024, 1 + i, 1))
               for i, p in enumerate(people)]

    def holders(posts, months):
        return {n for n, ts in _club(posts, [], members, months).items()
                if "아이고 어르신" in _names(ts)}

    전체 = holders(early + late, [202601, 202602, 202603])
    최근 = holders(late, [202603])
    assert 전체 == 최근 == set(people), (전체, 최근)


def test_only_period_proof_titles_are_badges():
    """배지형 목록에 기간 종속 칭호가 섞이면 안 된다.

    `첫 출사 못 참지`가 대표적인 유혹이다 — 가입 직후 나왔다는 사실은 안
    변할 것 같지만, 기간을 좁히면 `첫등장`이 밀려 값이 커지고 사라진다.
    """
    assert "첫 출사 못 참지" not in BADGE_TITLES
    assert "새싹" not in BADGE_TITLES
    assert BADGE_TITLES <= set(FIXED_TITLE_NAMES), \
        BADGE_TITLES - set(FIXED_TITLE_NAMES)


def test_badge_titles_come_first():
    """화면이 배지형과 기간형을 갈라 그릴 수 있게 앞에 모아 둔다."""
    posts, members = _busy_oldtimer()
    got = _names(_club(posts, [], members)["고참"])
    assert got[0] in BADGE_TITLES, got
    assert not (set(got[1:]) & BADGE_TITLES), got


def test_the_screen_splits_badges_from_period_titles():
    """멤버 상세가 두 줄로 나눠 그리는 근거 — 하나도 흘리지 않는다."""
    from streamlit_app import split_badge_titles
    posts, members = _busy_oldtimer()
    titles = _club(posts, [], members)["고참"]
    badge, period = split_badge_titles(titles)
    assert [t["칭호"] for t in badge] == ["아이고 어르신"]
    assert len(period) == TITLE_LIMIT
    assert badge + period == titles, "순서나 개수가 바뀌면 화면이 어긋난다"


def test_splitting_titles_handles_an_empty_list():
    """칭호가 하나도 없는 사람도 화면을 그린다."""
    from streamlit_app import split_badge_titles
    assert split_badge_titles([]) == ([], [])


def test_ties_are_broken_differently_for_each_title():
    """동점을 이름순으로 끊으면 **가나다 뒤쪽 사람이 모든 칭호에서 밀린다.**

    활동이 똑같은 서른 명이 있으면 한 명은 세 칸이 차고 다른 한 명은 빈손이
    되는데, 그 차이가 실력이 아니라 이름이다. 칭호마다 순서를 달리 섞어
    동점자에게 골고루 돌아가게 한다.
    """
    posts, members = _old_crowd(30)
    got = _club(posts, [], members)
    assert all(got[m["mn"]] for m in members), \
        [m["mn"] for m in members if not got[m["mn"]]]


def test_the_tiebreak_is_stable_across_runs():
    """새로고침할 때마다 칭호가 바뀌면 안 된다 — `hash()`는 실행마다 다르다."""
    posts, members = _old_crowd(30)
    assert _club(posts, [], members) == _club(posts, [], members)


def test_relationship_titles_have_a_tighter_quota():
    """`○○님 오늘도 뵙네요`는 다섯 명까지 — 한 사람에게 17명이 붙은 적이 있다."""
    from streamlit_app import TITLE_QUOTA

    # 팬 여덟이 인기가 가는 날만 골라 네 번씩 나온다. 팬끼리는 최대 세 번만
    # 겹치게 어긋 배치해, 각자의 **최다 동행이 인기**가 되도록 한다.
    fans = [f"팬{j}" for j in range(8)]
    posts = []
    for i in range(11):                      # 인기가 나오는 11건
        att = ["인기"] + [f for j, f in enumerate(fans) if j <= i <= j + 3]
        posts.append(notice(f"n{i:02d}", "2026-03-%02d" % (i + 1), att))
    for i in range(3):                       # 인기가 안 나오는 3건
        posts.append(notice(f"b{i}", "2026-04-%02d" % (i + 1), ["남들"]))
    members = ([member("w0", "인기"), member("w99", "남들")]
               + [member(f"w{j}", f) for j, f in enumerate(fans)])
    got = _club(posts, [], members, [202603, 202604])
    followers = sum(1 for ts in got.values()
                    for t in ts if t["칭호"] == "인기님 오늘도 뵙네요")
    assert 0 < followers <= TITLE_QUOTA["관계"], followers


# ═══════════════════════════════════════════════════════════════
# 새 칭호 11개 — 조건마다 하나씩
# ═══════════════════════════════════════════════════════════════

def _hosted(pid, host, day, attendees, posted=None, **kw):
    return notice(pid, f"2026-03-{day}", attendees, author=host,
                  posted_at=posted or datetime(2026, 3, 1),
                  matched_review_id=f"r{pid}", **kw)


def _extras(n=5):
    """모수를 채우는 들러리 — `top_share`는 네 명 미만이면 아무에게도 안 준다."""
    posts = [notice(f"x{i}", "2026-03-2%d" % (i % 9), [f"들러리{i}"])
             for i in range(n)]
    return posts, [member(f"e{i}", f"들러리{i}") for i in range(n)]


def test_fast_reviewer():
    """출사 다음날까지 후기를 올리는 사람."""
    posts = []
    for i, day in enumerate(["02", "09", "16"]):
        posts.append(_hosted(f"n{i}", "빠름", day, ["빠름"]))
        posts.append(review(f"rn{i}", author="빠름", matched=f"n{i}",
                            posted_at=datetime(2026, 3, int(day) + 1)))
    assert "후기는 따끈할때" in _cands("빠름", posts, [], [member("w1", "빠름")])


def test_unmatched_reviews_are_not_counted_as_instant():
    """매칭된 공지가 없으면 간격을 잴 수 없다 — 0일로 치면 제일 빨라 보인다."""
    posts = [review(f"r{i}", author="미매칭", matched=None,
                    posted_at=datetime(2026, 3, 5)) for i in range(3)]
    posts.append(notice("n1", "2026-03-01", ["미매칭"]))
    assert "후기는 따끈할때" not in _cands("미매칭", posts, [], [member("w1", "미매칭")])


def test_flash_organizer():
    """공지 이틀 안에 떠나는 출사를 자주 여는 사람."""
    posts = [_hosted(f"n{i}", "번개", "%02d" % (i * 3 + 2), ["번개"],
                     posted=datetime(2026, 3, i * 3 + 1)) for i in range(4)]
    assert "내일 출사가실분?" in _cands("번개", posts, [], [member("w1", "번개")])


def test_joined_and_came_right_away():
    posts = [notice("n1", "2026-03-05", ["신입"])]
    members = [member("w1", "신입", datetime(2026, 3, 1))]
    assert "첫 출사 못 참지" in _cands("신입", posts, [], members)


def test_weekday_regular():
    """2026-03의 02·03·04·05·06은 월~금이다."""
    posts = [notice(f"n{i}", "2026-03-%02d" % d, ["평일러"])
             for i, d in enumerate([2, 3, 4, 5, 6])]
    assert "프로 평일러" in _cands("평일러", posts, [],
                                       [member("w1", "평일러")])


def test_a_weekend_goer_gets_no_weekday_title():
    """2026-03의 07·14·21·28은 토요일, 01은 일요일이다."""
    posts = [notice(f"n{i}", "2026-03-%02d" % d, ["주말러"])
             for i, d in enumerate([1, 7, 14, 21, 28])]
    assert "프로 평일러" not in _cands("주말러", posts, [],
                                           [member("w1", "주말러")])


def test_the_one_who_only_shows_up_when_it_is_crowded():
    """`정출킬러` — 참석한 출사의 평균 인원이 상위 15%."""
    crowd = [f"떼거리{j}" for j in range(9)]
    posts = [notice(f"b{i}", "2026-03-0%d" % (i + 1), ["북적"] + crowd)
             for i in range(4)]
    # 모수를 채운다 — 조용한 출사만 다니는 사람이 있어야 "상위"가 뜻을 가진다.
    posts += [notice(f"q{j}", "2026-03-1%d" % j, [f"조용{j}", "짝"]) for j in range(5)]
    members = ([member("w1", "북적"), member("w2", "짝")]
               + [member(f"c{j}", c) for j, c in enumerate(crowd)]
               + [member(f"s{j}", f"조용{j}") for j in range(5)])
    assert "정출킬러" in _cands("북적", posts, [], members)


def test_the_one_who_picks_quiet_outings():
    """`소수정예` — 같은 자를 반대쪽에서 읽는다."""
    posts = [notice(f"s{i}", "2026-03-0%d" % (i + 1), ["소수", "짝"]) for i in range(4)]
    posts += [notice(f"b{i}", "2026-03-1%d" % i, [f"떼거리{j}" for j in range(9)])
              for i in range(4)]
    members = ([member("w1", "소수"), member("w2", "짝")]
               + [member(f"c{j}", f"떼거리{j}") for j in range(9)])
    assert "소수정예" in _cands("소수", posts, [], members)


def test_the_two_size_titles_are_never_both_true():
    """같은 자의 양 끝이라 한 사람에게 둘 다 붙을 수 없다."""
    from streamlit_app import club_titles
    crowd = [f"떼거리{j}" for j in range(9)]
    posts = [notice(f"b{i}", "2026-03-0%d" % (i + 1), ["북적"] + crowd)
             for i in range(4)]
    posts += [notice(f"s{i}", "2026-03-1%d" % i, ["소수", "짝"]) for i in range(4)]
    members = ([member("w1", "북적"), member("w2", "소수"), member("w3", "짝")]
               + [member(f"c{j}", c) for j, c in enumerate(crowd)])
    for ts in club_titles(posts, [], members, [202603]).values():
        names = _names(ts)
        assert not ("정출킬러" in names and "소수정예" in names)


def test_watcher_and_uploader_are_opposite_ends():
    posts, members = _extras(5)
    posts += [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["눈으로", "남"])
              for i in range(6)]
    photos = [photo(f"p{i}", "사진만", likes=1) for i in range(12)]
    photos += [photo(f"e{i}", f"들러리{i}", likes=1) for i in range(5)]
    members += [member("w1", "눈으로"), member("w2", "사진만"), member("w3", "남")]
    assert "소모임에요? 글쎄.." in _cands("눈으로", posts, photos, members)
    assert "제가 사진이 좀 많아요" in _cands("사진만", posts, photos, members)


def test_never_canceled_host():
    posts = [_hosted(f"n{i}", "무사고", "%02d" % (i + 1), ["무사고"]) for i in range(5)]
    assert "펑이 뭐죠?" in _cands("무사고", posts, [], [member("w1", "무사고")])


def test_someone_else_writes_the_reviews():
    """`책임감 100만점`의 정반대 — 열기는 하는데 후기는 안 쓴다."""
    posts = []
    for i in range(3):
        posts.append(_hosted(f"n{i}", "개최만", "%02d" % (i + 1), ["개최만", "딴사람"]))
        posts.append(review(f"rn{i}", author="딴사람", matched=f"n{i}"))
    members = [member("w1", "개최만"), member("w2", "딴사람")]
    assert "아맞다후기" in _cands("개최만", posts, [], members)


def _host_with_rate(host, opened, wrote, day0):
    """`opened`건을 열고 그중 `wrote`건만 본인이 후기를 쓴 사람.

    후기 지표만 보려고 **조용하게** 만든다 — 참석자를 안 넣고(참석·동행 칭호
    없음) 공지와 출사일을 멀리 띄운다(`내일 출사가실분?` 없음).
    """
    out = []
    for i in range(opened):
        pid = f"{host}{i}"
        out.append(notice(pid, "2026-03-%02d" % (day0 + i), [], author=host,
                          posted_at=datetime(2026, 3, 1),
                          matched_review_id=f"r{pid}"))
        out.append(review(f"r{pid}", author=host if i < wrote else "딴사람",
                          matched=pid))
    return out


def test_the_awol_reviewer_is_the_single_lowest_writer():
    """후기 0건인 사람이 실제로 없어서, **가장 낮은 한 명**을 부른다."""
    posts = (_host_with_rate("바닥", 4, 1, 11)       # 25%
             + _host_with_rate("중간", 4, 2, 16))     # 50%
    members = [member("w1", "바닥"), member("w2", "중간"), member("w3", "딴사람")]
    got = _club(posts, [], members)
    assert "아맞다후기" in _names(got["바닥"])
    assert "아맞다후기" not in _names(got["중간"]), "정원 1 — 한 명만 받는다"


def test_the_awol_reviewer_stops_at_eighty_percent():
    """후기는 늘 쓰는 것이 맞으니 81%인 사람에게 이 이름은 틀린 말이다."""
    ok = _host_with_rate("잘씀", 5, 4, 11)           # 80% — 받는다
    members = [member("w1", "잘씀"), member("w2", "딴사람")]
    assert "아맞다후기" in _cands("잘씀", ok, [], members)

    posts = _host_with_rate("더잘씀", 10, 9, 11)      # 90% — 안 받는다
    members = [member("w1", "더잘씀"), member("w2", "딴사람")]
    assert "아맞다후기" not in _cands("더잘씀", posts, [], members)


def test_nobody_gets_the_awol_title_when_everyone_writes():
    """다들 잘 쓰는 해에는 수령자가 없다 — 억지로 한 명을 만들지 않는다."""
    posts = (_host_with_rate("가", 10, 9, 1) + _host_with_rate("나", 10, 10, 12))
    members = [member("w1", "가"), member("w2", "나"), member("w3", "딴사람")]
    got = _club(posts, [], members)
    assert not any("아맞다후기" in _names(ts) for ts in got.values())


def test_the_awol_reason_separates_none_from_some():
    """"한 건도 없다"와 "3건 중 1건"은 다른 얘기다 — 근거도 달라야 한다."""
    def 근거(host, opened, wrote, day0):
        posts = _host_with_rate(host, opened, wrote, day0)
        members = [member("w1", host), member("w2", "딴사람")]
        t = next(t for t in _cands_full(host, posts, [], members)
                 if t["칭호"] == "아맞다후기")
        return t["근거"]
    assert "한 건도 없습니다" in 근거("아무것도", 3, 0, 11)
    assert "가장 낮습니다" in 근거("조금만", 5, 1, 11)


def test_the_responsible_host_reason_drops_the_word_directly():
    """후기는 원래 본인이 쓰는 것이라 '직접'은 당연한 말이다."""
    posts = _host_with_rate("성실", 5, 5, 11)
    t = next(t for t in _cands_full("성실", posts, [],
                                    [member("w1", "성실")])
             if t["칭호"] == "책임감 100만점")
    assert "직접" not in t["근거"] and "전부" in t["근거"]


def test_the_responsible_host_needs_five_outings_now():
    """개최 2건이면 '둘 다 내가 썼다'가 너무 흔하다 — 실제로 15명이 받았다."""
    def club(n):
        posts = []
        for i in range(n):
            posts.append(_hosted(f"n{i}", "성실", "%02d" % (i + 1), ["성실"]))
            posts.append(review(f"rn{i}", author="성실", matched=f"n{i}"))
        return _cands("성실", posts, [], [member("w1", "성실")])
    assert "책임감 100만점" not in club(3)
    assert "책임감 100만점" in club(5)


# ═══════════════════════════════════════════════════════════════
# 감노 때부터 계셨네 — 가입일로 잰다
# ═══════════════════════════════════════════════════════════════

def _old_club_posts():
    return [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["고참", "신참"])
            for i in range(3)]


def test_old_timer_is_decided_by_the_join_date():
    members = [member("w1", "고참", datetime(2025, 3, 1)),
               member("w2", "신참", datetime(2025, 9, 1))]
    posts = _old_club_posts()
    assert "아이고 어르신" in _cands("고참", posts, [], members)
    assert "아이고 어르신" not in _cands("신참", posts, [], members)


def test_old_timer_needs_a_known_join_date():
    members = [member("w1", "고참", None), member("w2", "신참", datetime(2025, 9, 1))]
    assert "아이고 어르신" not in _cands("고참", _old_club_posts(), [], members)


def test_old_timer_does_not_change_when_the_period_narrows():
    """예전 `터줏대감`은 '이 기간에 가장 먼저 나온 사람'이라 기간마다 바뀌었다."""
    members = [member("w1", "고참", datetime(2025, 3, 1)),
               member("w2", "신참", datetime(2025, 9, 1))]
    posts = _old_club_posts()
    wide = "아이고 어르신" in _cands("고참", posts, [], members,
                                   [202601, 202602, 202603])
    narrow = "아이고 어르신" in _cands("고참", posts, [], members, [202603])
    assert wide is narrow is True


def test_renamed_titles_use_the_new_strings():
    from streamlit_app import CATEGORY_TITLES, FIXED_TITLE_NAMES
    for gone in ("테마 단골", "판을 여는 사람", "자주 여는 사람", "개근왕", "다작왕",
                 "기록하는 사람", "혼자가 편한 사람", "터줏대감", "마당발",
                 "매번 초면", "열심 참석러", "좋아요 수집가", "가리지 않는 사람",
                 "감노 때부터 계셨네", "이분 출사는 항상 만석",
                 "한 달도 안 빠졌네", "한결같은 사람",
                 "골고루 하는 사람", "펑 한 번 없는 사람", "펑의 달인"):
        assert gone not in FIXED_TITLE_NAMES, gone
    for now in ("테마사진 프로 참석러", "출사장도 장이다", "심심한데 출사쳐야지",
                "이게 본업이에요", "여기 제 인스타인데..", "책임감 100만점",
                "저 신입 아닌데요", "아이고 어르신", "정출킬러", "잡식성",
                "프로 평일러", "아맞다후기", "후기는 따끈할때", "내일 출사가실분?",
                "소모임에요? 글쎄..", "제가 사진이 좀 많아요", "첫 출사 못 참지",
                "다 아는 사람들 이구먼", "사진 좋아요 1위", "느좋 사진러",
                "틈틈이 골고루", "펑이 뭐죠?", "그럴만한 이유가..."):
        assert now in FIXED_TITLE_NAMES, now
    assert set(CATEGORY_TITLES.values()) <= set(FIXED_TITLE_NAMES)


def test_the_steady_titles_are_gone():
    """`한 달도 안 빠졌네`·`한결같은 사람`은 뺐다 — 되살리려면 이걸 뒤집는다."""
    import streamlit_app
    from streamlit_app import FIXED_TITLE_NAMES
    assert "한 달도 안 빠졌네" not in FIXED_TITLE_NAMES
    assert "한결같은 사람" not in FIXED_TITLE_NAMES
    assert not hasattr(streamlit_app, "_attendance_streak"), "부르는 곳 없는 코드"


# ═══════════════════════════════════════════════════════════════
# 1등은 없지만 빠지지도 않는 사람 — 실제 데이터에서 통째로 비어 있었다
# ═══════════════════════════════════════════════════════════════

def test_doing_all_four_things_earns_a_title():
    """참석 13·개최 3·후기 2·사진 18인 사람이 칭호가 하나도 없었다.

    어느 지표에서도 상위 15~30%에 못 들었기 때문인데, **네 가지를 다 하는
    것 자체가 드물다**(실제 데이터에서 94명 중 24명).
    """
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["만능", "남들"])
             for i in range(6)]
    posts += [notice(f"h{i}", "2026-04-0%d" % (i + 1), ["만능"], author="만능")
              for i in range(2)]
    posts += [review(f"r{i}", author="만능", matched=f"n{i}") for i in range(2)]
    photos = [photo(f"p{i}", "만능") for i in range(5)]
    members = [member("w1", "만능"), member("w2", "남들")]
    assert "틈틈이 골고루" in _cands("만능", posts, photos, members)


def test_missing_one_of_the_four_earns_nothing():
    """셋만 하면 "골고루"가 아니다."""
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["셋만"]) for i in range(6)]
    posts += [notice(f"h{i}", "2026-04-0%d" % (i + 1), ["셋만"], author="셋만")
              for i in range(2)]
    photos = [photo(f"p{i}", "셋만") for i in range(5)]
    assert "틈틈이 골고루" not in _cands("셋만", posts, photos,
                                      [member("w1", "셋만")])


def test_a_latecomer_is_measured_per_month():
    """늦게 합류한 사람은 **누적 참석으로는 영원히 위로 못 간다.**

    가입 이후 몇 달을 다녔는지로 나누면 "짧은 기간에 촘촘히"가 보인다.
    """
    months = [202601 + i for i in range(6)]
    # 고참은 여섯 달에 걸쳐 열 번, 늦게 온 사람은 마지막 두 달에 여덟 번.
    posts = [notice(f"o{i}", "2026-%02d-05" % (i % 6 + 1), ["고참"]) for i in range(10)]
    posts += [notice(f"l{i}", "2026-0%d-1%d" % (5 + i % 2, i), ["늦둥이"])
              for i in range(8)]
    posts += [notice(f"x{i}", "2026-0%d-2%d" % (i % 6 + 1, i), [f"남들{i}"])
              for i in range(6)]
    members = ([member("w1", "고참", datetime(2026, 1, 1)),
                member("w2", "늦둥이", datetime(2026, 5, 1))]
               + [member(f"w{i+3}", f"남들{i}", datetime(2026, 1, 1)) for i in range(6)])
    assert "짧은 기간에 진심" in _cands("늦둥이", posts, [], members, months)


def test_the_density_pool_divides_by_time_in_the_club():
    """가입 전 기간까지 분모에 넣으면 늦게 온 사람이 손해를 본다."""
    from streamlit_app import _active_months
    months = [202601 + i for i in range(6)]
    assert _active_months(datetime(2026, 5, 1), months) == 2      # 5·6월
    assert _active_months(datetime(2020, 1, 1), months) == 6      # 기간 전체
    assert _active_months(None, months) == 6


# ═══════════════════════════════════════════════════════════════
# 근거 — 왜 붙었는지 읽고 알 수 있어야 한다
# ═══════════════════════════════════════════════════════════════

def test_the_two_size_titles_do_not_share_a_reason():
    """`정출킬러`와 `소수정예`의 근거가 **글자까지 같았다.**

    둘 다 "참석한 출사의 평균 인원 8.2명"이라고만 적혀서, 그게 많다는
    얘기인지 적다는 얘기인지 화면만 보고는 알 수 없었다.
    """
    crowd = [f"떼거리{j}" for j in range(9)]
    posts = [notice(f"b{i}", "2026-03-0%d" % (i + 1), ["북적"] + crowd)
             for i in range(4)]
    posts += [notice(f"s{i}", "2026-03-1%d" % i, ["소수", "짝"]) for i in range(4)]
    members = ([member("w1", "북적"), member("w2", "소수"), member("w3", "짝")]
               + [member(f"c{j}", c) for j, c in enumerate(crowd)])

    def reason(n, 칭호):
        return next((t for t in _cands_full(n, posts, [], members)
                     if t["칭호"] == 칭호), {}).get("근거")

    big, small = reason("북적", "정출킬러"), reason("소수", "소수정예")
    assert big and small and big != small, (big, small)
    assert "상위" in big and "하위" in small


def test_a_first_place_reason_differs_from_a_top_share_reason():
    """1등과 "상위 15%"는 전혀 다른 얘기다 — 근거도 달라야 한다."""
    people = [f"사람{i:02d}" for i in range(20)]
    # 뒤로 갈수록 한 번씩 덜 나온다 — 동점 없이 1등과 그 아래가 갈린다.
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), people[:20 - i])
             for i in range(20)]
    members = [member(f"w{i}", p) for i, p in enumerate(people)]
    got = _club(posts, [], members)

    첫째 = next(t for t in got["사람00"] if t["칭호"] == "이게 본업이에요")
    assert "가장 많이 나온" in 첫째["근거"], 첫째["근거"]
    아래 = [t for ts in got.values() for t in ts if t["칭호"] == "프로 참석러"]
    assert 아래 and all("상위" in t["근거"] for t in 아래), 아래


def test_every_reason_says_more_than_a_bare_number():
    """숫자만 적혀 있으면 무엇을 의도한 칭호인지 알 수 없다."""
    posts, members = _old_crowd(12)
    photos = [photo(f"p{i}", members[i % 12]["mn"], likes=i) for i in range(40)]
    for ts in _club(posts, photos, members).values():
        for t in ts:
            근거 = t["근거"]
            assert len(근거) >= 20, t
            # 조사·서술어가 있어야 문장이다 — `참석 13 · 사진 18` 같은
            # 나열만으로는 기준을 알 수 없다.
            assert any(k in 근거 for k in ("니다", "입니다", "요", "습니")), t


# ═══════════════════════════════════════════════════════════════
# 💞 인연 — 우연 대비(lift)
# ═══════════════════════════════════════════════════════════════

def _bond(posts, members, months=None):
    """전 멤버의 인연 칭호만 `{이름: 칭호}`로 추린다."""
    got = _club(posts, [], members, months)
    return {n: t["칭호"] for n, ts in got.items() for t in ts
            if "짜고 나오시나요" in t["칭호"] or "알림 켜두셨죠" in t["칭호"]}


# 인연은 **전체 출사 수가 분모**라 열 건짜리 표본으로는 재지지 않는다.
# 아래 픽스처는 100건을 네 달에 흩어 만든다.
_BOND_MONTHS = [202603, 202604, 202605, 202606]


def _spread(seq):
    """`[(참석자, 건수), …]` → 날짜가 안 겹치는 공지 100건."""
    out = []
    for att, n in seq:
        for _ in range(n):
            i = len(out)
            out.append(notice(f"n{i:03d}",
                              "2026-%02d-%02d" % (3 + i // 28, i % 28 + 1),
                              list(att)))
    return out


def _cast(*names):
    return [member(f"w{i}", n) for i, n in enumerate(names)]


def test_a_thin_sample_is_discounted_more_than_a_thick_one():
    """3회 100%와 30회 100%는 관측 비율이 같지만 믿을 만한 정도가 다르다.

    하한을 안 쓰면 두세 번 만난 쌍이 순위를 다 차지한다.
    """
    from streamlit_app import _wilson_low
    assert _wilson_low(3, 3) < _wilson_low(30, 30)
    assert _wilson_low(30, 30) < 1.0          # 100%여도 1에는 못 닿는다
    assert _wilson_low(0, 0) == 0.0           # 참석 0인 사람에서 안 터진다


def test_lift_divides_out_the_chance_of_meeting_at_all():
    """`lift`는 "우연히 겹칠 때보다 몇 배"다 — 상대의 출석률로 나눈다."""
    from streamlit_app import _affinity
    # 전체 10건 중 나 4건, 상대 5건 → 우연이라면 2회 겹친다.
    a = _affinity(4, 4, 5, 10)
    assert a["기대"] == 2.0
    assert a["lift"] == 2.0                   # 4회 겹쳤으니 우연의 두 배
    assert _affinity(0, 0, 5, 10)["lift"] == 0.0    # 0으로 안 나눈다


def test_the_person_who_goes_to_everything_is_never_the_affinity_top():
    """실데이터 회귀 — 서동훈↔엄태진은 69회 함께지만 우연의 1.18배다.

    함께 간 **횟수**로만 고르면 이런 쌍이 늘 1등이 된다. 그게 이 칭호를
    따로 만든 이유이므로, 못 받는 것을 못 박아 둔다.
    """
    # 둘 다 20건 중 18건에 나온다 → 겹치는 게 당연하다.
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["항상", "노상", f"뜨내기{i}"])
             for i in range(18)]
    posts += [notice(f"x{i}", "2026-04-%02d" % (i + 1), [f"뜨내기{i}"])
              for i in range(2)]
    members = ([member("w0", "항상"), member("w1", "노상")]
               + [member(f"t{i}", f"뜨내기{i}") for i in range(18)])
    got = _bond(posts, members, [202603, 202604])
    assert "항상" not in got and "노상" not in got, got


def test_a_rare_pair_that_always_shows_up_together_is_mutual():
    """네 번뿐이어도 **우연이라면 한 번** 겹칠 사이면 우연이 아니다.

    둘 다 100건 중 10건에만 나오므로 기대 겹침은 1.0회다. 서로에게 40%씩이라
    `2인 1조`(양쪽 50%)에는 못 미치는 — 횟수로는 안 보이는 쌍이다.
    """
    posts = _spread([(["왼짝", "오른짝"], 4), (["왼짝"], 6),
                     (["오른짝"], 6), (["행인"], 84)])
    got = _bond(posts, _cast("왼짝", "오른짝", "행인"), _BOND_MONTHS)
    assert got.get("왼짝") == "오른짝님과 짜고 나오시나요?", got
    assert got.get("오른짝") == "왼짝님과 짜고 나오시나요?", got


def test_one_sided_affinity_becomes_the_crush_title():
    """내 1순위인데 **상대의 1순위는 내가 아니면** 짝사랑이다."""
    # 스타는 20건에 나온다. 단짝은 그중 12건에 늘 붙어 다녀 스타의 1순위이고,
    # 팬은 자기 10건 중 8건을 스타 옆에서 보내지만 스타에게는 두 번째다.
    posts = _spread([(["스타", "단짝"], 12), (["스타", "팬"], 8),
                     (["팬"], 2), (["행인"], 78)])
    got = _bond(posts, _cast("스타", "단짝", "팬", "행인"), _BOND_MONTHS)
    assert got.get("팬") == "스타님 알림 켜두셨죠?", got
    assert "알림" not in got.get("스타", ""), got


def test_a_crush_needs_half_of_my_own_outings():
    """내 1순위여도 내 출사의 일부면 "따라다닌다"고 할 수 없다."""
    # 팬은 25건에 나오는데 스타와는 3건뿐 — 우연 대비로는 높아도 8%다.
    posts = _spread([(["스타", "팬"], 3), (["스타", "단짝"], 12),
                     (["팬"], 22), (["행인"], 63)])
    assert "팬" not in _bond(posts, _cast("스타", "단짝", "팬", "행인"),
                            _BOND_MONTHS)


def test_the_combo_partner_gets_no_second_heart_for_the_same_person():
    """같은 이름으로 💞가 두 번 붙으면 한 칸을 낭비한다 — 실데이터 3명.

    **짝사랑으로 흘러내려도 안 된다.** 서로가 서로를 1순위로 꼽는 쌍인데
    "정작 이분의 1순위는 따로 있고요"라고 하면 거짓말이 된다.
    """
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["나무", "바다"])
             for i in range(4)]
    posts += [notice(f"o{i}", "2026-04-%02d" % (i + 1), ["남들"])
              for i in range(8)]
    members = [member("w1", "나무"), member("w2", "바다"), member("w3", "남들")]
    got = _club(posts, [], members, [202603, 202604])
    for who in ("나무", "바다"):
        names = _names(got[who])
        assert any("2인 1조" in t for t in names), names
        assert not any("짜고 나오시나요" in t for t in names), names
        assert not any("알림 켜두셨죠" in t for t in names), names


def test_the_two_metrics_can_name_two_different_people():
    """`동행`과 `인연`은 지표가 달라 서로 자리를 안 뺏는다.

    실데이터에서 둘 다 받는 14명 중 11명은 상대가 다르다 — 이게 이 칭호를
    더한 이유다. 따라의 **최다 동행은 인기**(14회)지만, 인기는 100건 중
    40건에 나오니 겹치는 게 당연하다. 우연 대비 1순위는 여섯 번만 나오는
    **숨은짝**이다.
    """
    posts = _spread([(["인기", "따라"], 14), (["숨은짝", "따라"], 5),
                     (["따라"], 1), (["인기"], 26), (["숨은짝"], 1),
                     (["행인"], 53)])
    names = _names(_club(posts, [], _cast("인기", "따라", "숨은짝", "행인"),
                         _BOND_MONTHS)["따라"])
    assert "인기님 오늘도 뵙네요" in names, names
    assert any("숨은짝" in t and "짜고" in t for t in names), names


def test_the_reason_explains_the_multiplier_in_plain_words():
    """`lift 5.7`은 아무 뜻이 없다 — **기대 겹침 횟수**로 바꿔 적는다."""
    posts = _spread([(["왼짝", "오른짝"], 4), (["왼짝"], 6),
                     (["오른짝"], 6), (["행인"], 84)])
    t = next(t for t in _club(posts, [], _cast("왼짝", "오른짝", "행인"),
                              _BOND_MONTHS)["왼짝"] if "짜고" in t["칭호"])
    assert "lift" not in t["근거"].lower(), t["근거"]
    assert "겹" in t["근거"], t["근거"]


def test_a_sub_one_expectation_drops_the_multiplier():
    """"한 번도 안 겹쳤을 법한데 18.8배"는 앞뒤가 어긋나 보인다.

    기대가 1회 미만이면 배수를 빼고 **실제 횟수**로 말한다.
    """
    from streamlit_app import _chance_phrase
    작다 = _chance_phrase({"기대": 0.2, "lift": 18.8, "함께": 3})
    assert "배" not in 작다 and "3회" in 작다, 작다
    크다 = _chance_phrase({"기대": 1.4, "lift": 5.7, "함께": 8})
    assert "5.7배" in 크다 and "1.4회" in 크다, 크다


def test_a_name_in_a_title_is_always_followed_by_nim():
    """사람 이름이 박히는 칭호는 **넷 다 `님`**을 붙인다.

    높임말이라 읽기 좋기도 하지만, 조사가 붙는 자리를 `님`이 대신 받아 주는
    것이 더 크다 — `님`은 받침이 있어 뒤따르는 조사가 이름과 무관하게 늘
    `과`로 고정된다. 이름을 그대로 쓰면 `엄태진과`/`바다와`를 갈라 줘야 하고,
    `Bale(이상현)`처럼 괄호·영문으로 끝나는 표시 이름에서 마지막 한글 음절을
    찾아내는 판정이 따로 필요해진다.
    """
    # 받침 있는 이름(`오른짝`)과 없는 이름(`바다`)을 한 번에 지난다.
    posts = _spread([(["왼짝", "오른짝"], 4), (["왼짝"], 6),
                     (["오른짝"], 6), (["행인"], 84)])
    got = _club(posts, [], _cast("왼짝", "오른짝", "행인"), _BOND_MONTHS)
    assert "오른짝님과 짜고 나오시나요?" in _names(got["왼짝"]), got["왼짝"]

    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["나무", "바다"])
             for i in range(4)]
    got = _club(posts, [], _cast("나무", "바다"), [202603])
    assert "바다님과 2인 1조" in _names(got["나무"]), got["나무"]

    # 어느 칭호든 이름 바로 뒤가 `님`이 아닌 채로 조사가 붙으면 안 된다.
    for who, ts in got.items():
        for t in ts:
            for 조사 in ("와 ", "과 "):
                if 조사 in t["칭호"]:
                    앞 = t["칭호"][:t["칭호"].index(조사)]
                    assert 앞.endswith("님"), t["칭호"]


# ═══════════════════════════════════════════════════════════════
# 🎯 출사와 벙 — 이 모임이 무엇을 하러 모이나
# ═══════════════════════════════════════════════════════════════

def _bung(pid, day, attendees, *, cat="보정", title=None, **kw):
    """벙 공지. 제목에 `온라인`을 넣으면 온라인 벙이 된다."""
    return notice(pid, day, attendees, category=cat,
                  title=title or f"[{cat}] {pid}", **kw)


def test_online_is_read_from_the_title_not_stored():
    """저장하면 `POST_KEYS`가 늘어 raw 시트 스키마가 바뀐다 — 제목에서 읽는다."""
    from core.collector import is_online_title
    assert is_online_title("07.28.(화) [보정] 온라인 보정벙")
    assert is_online_title("10.14(화) [보정]온라인보정벙")      # 붙여쓰기
    assert is_online_title("[온라인] 보정")                     # 괄호
    assert is_online_title("온 라인 보정")                      # 띄어쓰기
    assert not is_online_title("10.15 (수) [보정] 이수 보정벙")  # 장소명
    assert not is_online_title("")
    assert not is_online_title(None)


def test_the_scope_filter_touches_notices_only():
    """후기·가입인사까지 빼면 후기율·연차 칭호가 벙 스위치에 흔들린다.

    후기를 쓴 사실은 그 글이 출사 후기냐 벙 후기냐와 무관하다.
    """
    from streamlit_app import denom_posts
    posts = [notice("n1", "2026-03-01", ["나무"]),
             _bung("b1", "2026-03-02", ["나무"]),
             review("r1", ["나무"]),
             {"id": "j1", "cat": "J", "author": "나무",
              "posted_at": datetime(2026, 3, 3), "title": "가입인사"}]
    좁힘 = denom_posts(posts, False)
    assert [p["id"] for p in 좁힘] == ["n1", "r1", "j1"]     # 벙만 빠진다
    assert denom_posts(posts, True) == posts                # 켜면 지금과 같다


def test_a_notice_that_is_neither_shoot_nor_bung_leaves_the_scope():
    """`일반공지`·카테고리 미상은 출사도 벙도 아니다 — 모수에서 뺀다.

    무엇인지 모르는 공지가 참석률 분모에 있으면 그 비율이 뜻을 잃는다.
    """
    from streamlit_app import denom_posts, is_bung, is_shoot
    일반 = notice("g1", "2026-03-01", ["나무"], category="일반공지")
    미상 = notice("u1", "2026-03-02", ["나무"], category=None)
    assert not is_shoot(일반) and not is_bung(일반)
    assert not is_shoot(미상) and not is_bung(미상)
    assert denom_posts([일반, 미상], False) == []


def test_dropping_bung_changes_the_attendance_denominator():
    """실데이터에서 참석률 순위가 여섯 계단까지 움직인 바로 그 효과."""
    from streamlit_app import member_profile
    posts = [notice("n1", "2026-03-01", ["출사러", "벙러"]),
             notice("n2", "2026-03-02", ["출사러"]),
             _bung("b1", "2026-03-03", ["벙러"]),
             _bung("b2", "2026-03-04", ["벙러"], cat="문화")]
    members = [member("w1", "출사러"), member("w2", "벙러")]

    끔 = member_profile("출사러", posts, [], members)
    assert (끔["매칭 출사"], 끔["참석률"]) == (2, 100.0)
    켬 = member_profile("출사러", posts, [], members, 벙포함=True)
    assert (켬["매칭 출사"], 켬["참석률"]) == (4, 50.0)

    # 벙러는 반대로 움직인다 — 스위치를 끄면 분모도 분자도 준다.
    assert member_profile("벙러", posts, [], members)["참석"] == 1
    assert member_profile("벙러", posts, [], members,
                          벙포함=True)["참석"] == 3


def test_bung_only_attendees_are_not_ghosts():
    """벙에만 나온 사람이 스위치 하나로 유령이 되면 안 된다.

    실데이터에 정확히 그런 분이 있다 — 출사 0회, 벙 1회.
    """
    from streamlit_app import club_context, member_profile
    posts = [notice("n1", "2026-03-01", ["출사러"]),
             _bung("b1", "2026-03-02", ["벙러", "출사러"])]
    members = [member("w1", "출사러"), member("w2", "벙러")]
    ctx = club_context(posts, [], members, [202603])
    prof = member_profile("벙러", posts, [], members, ctx)
    assert prof["벙 참석"] == 1
    assert prof["참석"] == 0          # 출사 모수로는 0회가 맞다
    assert not prof["유령"]           # 그렇다고 유령은 아니다
    assert "벙러" not in ctx["휴면"]   # 휴면도 아니다


def test_bung_metrics_ignore_the_switch():
    """벙 지표는 벙 스위치와 무관하게 늘 전량을 본다."""
    from streamlit_app import club_context
    posts = [notice("n1", "2026-03-01", ["나무"]),
             _bung("b1", "2026-03-02", ["나무"],
                   title="[보정] 온라인 보정벙"),
             _bung("b2", "2026-03-03", ["나무"], cat="문화")]
    members = [member("w1", "나무")]
    for 벙포함 in (False, True):
        ctx = club_context(posts, [], members, [202603], 벙포함=벙포함)
        assert ctx["벙참석수"]["나무"] == 2
        assert ctx["온라인참석수"]["나무"] == 1


def test_the_category_denominator_is_never_filtered():
    """벙을 빼면 `모여서 보정하실 분?`·`문화?시민`이 **영영 0명**이 된다.

    카테고리 칭호는 비율이라 자기 분모를 스스로 들고 있어 출사 모수와 안 섞인다.
    """
    from streamlit_app import club_context
    posts = [notice(f"n{i}", "2026-03-0%d" % (i + 1), ["나무"])
             for i in range(4)]
    posts += [_bung(f"b{i}", "2026-03-1%d" % i, ["나무"]) for i in range(4)]
    ctx = club_context(posts, [], [member("w1", "나무")], [202603])
    assert ctx["카테고리총계"]["보정"] == 4      # 스위치가 꺼져 있어도 센다
    assert ctx["전체참석수"]["나무"] == 8        # 문턱이 읽는 안 걸러진 참석 수


def test_a_bung_regular_gets_a_title_of_their_own():
    """참석의 40% 이상이 벙이면 그 사실이 그 사람다운 얘기다."""
    # 벙 4 / 출사 6 = 40% — 경계에서 붙는다.
    posts = [notice(f"n{i}", "2026-03-0%d" % (i + 1), ["벙러", "남들"])
             for i in range(6)]
    posts += [_bung(f"b{i}", "2026-03-1%d" % i, ["벙러"]) for i in range(4)]
    members = [member("w1", "벙러"), member("w2", "남들")]
    got = _names(_club(posts, [], members, [202603])["벙러"])
    assert "카메라는 두고 왔어요" in got, got


def test_a_bung_regular_below_the_share_gets_nothing():
    """벙 3회여도 내 참석의 3분의 1이면 그냥 가끔 가는 것이다."""
    posts = [notice(f"n{i}", "2026-03-0%d" % (i + 1), ["벙러"])
             for i in range(6)]
    posts += [_bung(f"b{i}", "2026-03-1%d" % i, ["벙러"]) for i in range(3)]
    got = _names(_club(posts, [], [member("w1", "벙러")], [202603])["벙러"])
    assert "카메라는 두고 왔어요" not in got, got


def test_online_beats_plain_bung_for_the_same_person():
    """둘 다 해당하면 더 특수한 쪽만 뜬다 — 같은 지표라 하나만 붙는다."""
    posts = [notice(f"n{i}", "2026-03-0%d" % (i + 1), ["집순이"])
             for i in range(6)]
    posts += [_bung(f"b{i}", "2026-03-1%d" % i, ["집순이"],
                    title=f"[보정] 온라인 보정벙 {i}") for i in range(4)]
    got = _names(_club(posts, [], [member("w1", "집순이")], [202603])["집순이"])
    assert "집에서 뵙겠습니다" in got, got
    assert "카메라는 두고 왔어요" not in got, got


def test_a_non_admin_who_hosts_is_recognised():
    """운영진이 아닌데 출사를 여는 것 — 이 모임이 바라는 바다."""
    posts = [notice(f"n{i}", "2026-03-0%d" % (i + 1), ["나무", "바다"],
                    author="나무") for i in range(2)]
    posts += [notice(f"m{i}", "2026-03-1%d" % i, ["나무", "바다"],
                     author="바다") for i in range(2)]
    members = [member("w1", "나무"), member("w2", "바다", is_admin=True)]
    got = _club(posts, [], members, [202603])
    assert "운영진도 아닌데" in _names(got["나무"]), got["나무"]
    assert "운영진도 아닌데" not in _names(got["바다"]), got["바다"]


def test_hosting_a_bung_is_not_hosting_an_outing():
    """`운영진도 아닌데`는 **출사** 개최만 센다.

    `개최 진행`을 그대로 쓰면 스위치를 켰을 때 보정·문화 벙이 섞여, 출사를
    여는 사람을 알아보자는 칭호가 벙만 연 사람에게 붙는다(실측에서 그랬다).
    """
    posts = [_bung(f"b{i}", "2026-03-0%d" % (i + 1), ["벙주", "남들"],
                   author="벙주") for i in range(3)]
    posts += [notice("n1", "2026-03-20", ["남들"], author="남들")]
    members = [member("w1", "벙주"), member("w2", "남들")]
    for 벙포함 in (False, True):
        got = _club(posts, [], members, [202603], 벙포함=벙포함)
        assert "운영진도 아닌데" not in _names(got["벙주"]), (벙포함, got["벙주"])


def test_hosting_outranks_attending():
    """이 모임은 개최 부담을 나눠 지는 것을 참석보다 귀하게 본다."""
    from streamlit_app import _title_candidates, club_context, member_profile
    # `tier`는 개최·참석 **양쪽** 모수가 4명 이상이어야 1등을 인정한다.
    넷 = ["주최", "손님", "셋째", "넷째"]
    posts = [notice(f"n{i}", "2026-03-0%d" % (i + 1), 넷, author="주최")
             for i in range(4)]
    posts += [notice(f"x{i}", "2026-03-1%d" % i, 넷, author=who)
              for i, who in enumerate(넷[1:])]
    members = [member(f"w{i}", n) for i, n in enumerate(넷)]
    ctx = club_context(posts, [], members, [202603])
    prof = member_profile("주최", posts, [], members, ctx)
    cands = _title_candidates("주최", prof, [], posts, [], [202603], ctx)
    개최 = next(t for t in cands if t["지표"] == "개최")
    참석 = next(t for t in cands if t["지표"] == "참석")
    assert 개최["우선"] > 참석["우선"], (개최, 참석)


def test_the_switch_on_reproduces_todays_numbers():
    """스위치를 켠 상태가 **지금 동작과 같다** — 이 변경이 안전하다는 증거다."""
    from streamlit_app import club_context, denom_posts
    posts = [notice("n1", "2026-03-01", ["나무"]),
             _bung("b1", "2026-03-02", ["나무"])]
    assert denom_posts(posts, True) == posts
    ctx = club_context(posts, [], [member("w1", "나무")], [202603], 벙포함=True)
    assert dict(ctx["참석"])["나무"] == 2       # 벙까지 세던 예전 값
