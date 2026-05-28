"""후기 본문 기반 참석자 추적 — 단위 테스트 (pytest).

실행: 레포 루트에서 `python -m pytest tests/ -q`
"""
from datetime import datetime, date

from core.collector import (
    parse_member_csv,
    build_member_master,
    extract_attendees,
    parse_review_outing_date,
    annotate_review_attendees,
    match_outings_with_reviews,
)


# ── extract_attendees ──────────────────────────────────────────
def test_extract_basic():
    master = {"정원석", "이하얀", "김민수", "권두흥"}
    body = "정원석 이하얀 김민수 권두흥 모르는분 후기입니다"
    assert extract_attendees(body, "후기", master) == ["정원석", "이하얀", "김민수", "권두흥"]


def test_extract_blacklist_wins_over_master():
    master = {"엄태진", "후기"}  # "후기"가 마스터에 있어도
    assert "후기" not in extract_attendees("엄태진 후기", "", master)


def test_extract_dedup_and_order():
    master = {"엄태진", "이민영"}
    assert extract_attendees("엄태진 이민영 엄태진", "", master) == ["엄태진", "이민영"]


def test_extract_strips_title():
    master = {"정원석", "장미"}
    title = "5월 정출 장미 후기"
    assert extract_attendees(title + " 정원석", title, master) == ["정원석"]


def test_extract_empty_body():
    assert extract_attendees("", "제목", {"김철수"}) == []


# ── parse_member_csv ───────────────────────────────────────────
def test_parse_member_csv_real_and_nick():
    names, n2r, r2n = parse_member_csv("실명,닉네임\n정원석,원석사진\n이하얀,하얀")
    assert {"정원석", "원석사진", "이하얀", "하얀"} <= names
    assert n2r["원석사진"] == "정원석"
    assert r2n["이하얀"] == "하얀"


def test_parse_member_csv_single_col_and_alias():
    names, n2r, _ = parse_member_csv("김민수\n권두흥,두흥,두흥이;권두")
    assert "김민수" in names
    assert n2r["두흥"] == "권두흥"
    assert n2r["두흥이"] == "권두흥" and n2r["권두"] == "권두흥"


# ── build_member_master ────────────────────────────────────────
def test_build_master_frequency_threshold():
    body = "철수 영희 민수"
    posts = [{"cat": "E", "title": "", "body": body, "author": "a"} for _ in range(3)]
    master = build_member_master(posts, [], min_freq=3)
    assert {"철수", "영희", "민수"} <= master


def test_build_master_below_freq_excluded():
    posts = [{"cat": "E", "title": "", "body": "철수 영희", "author": "a"}]
    assert "철수" not in build_member_master(posts, [], min_freq=3)


def test_build_master_extra_names_and_blacklist():
    master = build_member_master([], [], extra_names={"정원석", "후기"})
    assert "정원석" in master
    assert "후기" not in master  # 블랙리스트 제거


# ── parse_review_outing_date (과거 해석) ───────────────────────
def test_review_date_most_recent_past_same_year():
    assert parse_review_outing_date("06.06 식물원 후기", "", datetime(2026, 6, 20)) == date(2026, 6, 6)


def test_review_date_wraps_to_previous_year():
    assert parse_review_outing_date("12.28 송년 후기", "", datetime(2026, 1, 5)) == date(2025, 12, 28)


def test_review_date_explicit_year_trusted():
    assert parse_review_outing_date("2026.06.06 후기", "", datetime(2026, 6, 20)) == date(2026, 6, 6)


def test_review_date_beyond_lookback_is_none():
    assert parse_review_outing_date("01.02 후기", "", datetime(2026, 12, 1)) is None


def test_review_date_none_when_absent():
    assert parse_review_outing_date("그냥 후기", "", datetime(2026, 6, 20)) is None


# ── annotate_review_attendees (자가검증) ───────────────────────
def test_annotate_flags_when_author_missing():
    posts = [{
        "cat": "E", "title": "후기", "body": "이하얀 와주셨어요",
        "author": "딴사람닉", "posted_at": datetime(2026, 6, 20),
    }]
    annotate_review_attendees(posts, {"정원석", "이하얀"}, {"원석닉": "정원석"})
    assert posts[0]["attendees"] == ["이하얀"]
    assert posts[0]["attendees_needs_review"] is True
    assert "명단에 없음" in posts[0]["attendees_review_reason"]


def test_annotate_ok_when_author_present():
    posts = [{
        "cat": "E", "title": "후기", "body": "정원석 이하얀 함께",
        "author": "원석닉", "posted_at": datetime(2026, 6, 20),
    }]
    annotate_review_attendees(posts, {"정원석", "이하얀"}, {"원석닉": "정원석"})
    assert posts[0]["attendees_needs_review"] is False
    assert posts[0]["attendees"] == ["정원석", "이하얀"]


def test_annotate_flags_empty():
    posts = [{
        "cat": "E", "title": "후기", "body": "다 같이 즐거웠어요",
        "author": "닉", "posted_at": datetime(2026, 6, 20),
    }]
    annotate_review_attendees(posts, {"정원석"}, {})
    assert posts[0]["attendees"] == []
    assert posts[0]["attendees_needs_review"] is True


def test_annotate_canonicalizes_nick_to_real():
    posts = [{
        "cat": "E", "title": "", "body": "원석닉 참석",
        "author": "원석닉", "posted_at": datetime(2026, 6, 20),
    }]
    annotate_review_attendees(posts, {"정원석", "원석닉"}, {"원석닉": "정원석"})
    assert posts[0]["attendees"] == ["정원석"]


# ── match_outings_with_reviews ─────────────────────────────────
def _notice(nid, d, cat, author="공지자"):
    return {"id": nid, "cat": "A", "outing_date": d, "category": cat,
            "author": author, "posted_at": datetime.fromisoformat(d + "T00:00:00")}


def _review(rid, attendees, posted, cat=None, author="작성자", rod=None):
    return {"id": rid, "cat": "E", "attendees": list(attendees), "posted_at": posted,
            "category": cat, "review_outing_date": rod}


def test_match_category_breaks_date_tie():
    notices = [_notice("n1", "2026-06-06", "풍경"), _notice("n2", "2026-06-06", "인물")]
    rev = _review("r1", ["정원석"], datetime(2026, 6, 7), cat="인물", rod="2026-06-06")
    match_outings_with_reviews(notices + [rev])
    by_id = {n["id"]: n for n in notices}
    assert by_id["n2"]["matched_review_id"] == "r1"
    assert by_id["n1"]["matched_review_id"] is None
    assert by_id["n2"]["attendees"] == ["정원석"]
    assert rev["matched_outing_id"] == "n2"


def test_match_orphan_review_far_date():
    notices = [_notice("n1", "2026-01-01", "인물")]
    rev = _review("r1", ["정원석"], datetime(2026, 6, 7), cat="인물", rod="2026-06-06")
    match_outings_with_reviews(notices + [rev])
    assert rev["matched_outing_id"] is None
    assert notices[0]["actually_held"] is False
