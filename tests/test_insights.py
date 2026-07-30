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

def _club(posts, photos, members, months=None):
    """(이름 → 칭호 목록). 화면이 하는 것과 같은 순서로 부른다."""
    from streamlit_app import (club_context, member_companions, member_profile,
                               member_titles)
    months = months or [202603]
    ctx = club_context(posts, photos, members)
    out = {}
    for m in members:
        n = m["mn"]
        prof = member_profile(n, posts, photos, members, ctx)
        out[n] = member_titles(n, prof, member_companions(n, posts, ctx["쌍"]),
                               posts, photos, months, ctx)
    return out


def _names(titles):
    return [t["칭호"] for t in titles]


def test_a_member_with_no_activity_gets_only_the_ghost_title():
    posts = [notice("n1", "2026-03-01", ["활동가"])]
    members = [member("w1", "활동가"), member("w2", "유령")]
    assert _names(_club(posts, [], members)["유령"]) == ["유령 회원"]


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
    assert "바다와 환상의 콤비" in got
    assert not any("따라다녀" in t for t in got)


def test_the_follower_title_names_the_person_followed():
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["따라", "인기"])
             for i in range(4)]
    # 인기는 혼자서도 많이 다닌다 → 서로 비율이 안 맞아 콤비가 아니다.
    posts += [notice(f"s{i}", "2026-04-%02d" % (i + 1), ["인기"])
              for i in range(8)]
    members = [member("w1", "따라"), member("w2", "인기")]
    assert "인기만 따라다녀" in _names(_club(posts, [], members)["따라"])


def test_a_lopsided_taste_names_the_category():
    posts = [notice(f"n{i}", "2026-03-%02d" % (i + 1), ["풍경러"],
                    category="풍경") for i in range(5)]
    posts.append(notice("x", "2026-03-20", ["풍경러"], category="인물"))
    got = _names(_club(posts, [], [member("w1", "풍경러")])["풍경러"])
    assert "풍경 사냥꾼" in got


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
