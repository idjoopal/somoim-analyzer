"""앱 렌더 스모크 테스트 — Streamlit AppTest로 3단계 화면을 실제로 그려 본다.

순수 함수 테스트는 집계만 덮기 때문에, 탭 렌더가 옛 계약(12개월 고정 인덱싱 등)을
붙들고 있어도 통과해 버린다. 여기서는 세션 상태를 심어 render 경로를 끝까지 실행해
예외가 없는지 확인한다 — 특히 **다년 범위**에서.
"""

from datetime import datetime

from streamlit.testing.v1 import AppTest

APP = "streamlit_app.py"
TIMEOUT = 60


def notice(pid, outing_date, category="풍경", canceled=False, posted=None,
           author="닉", attendees=("닉",)):
    return {
        "id": pid, "author": author, "wid": "w1",
        "title": f"[{category}] {pid}", "body": "닉 참석",
        "outing_date": outing_date,
        "posted_at": posted or datetime(2025, 9, 1, 10, 0),
        "cat": "A", "cat_label": "공지", "category": category,
        "is_outing": True, "is_canceled": canceled,
        "likes": 2, "comments": 1, "images": 0,
        "needs_review": False, "review_reason": "",
        "is_active": True,
        "actually_held": not canceled,
        "attendees": list(attendees),
    }


def review(pid, posted, author="닉"):
    return {
        "id": pid, "author": author, "wid": "w1",
        "title": f"{pid} 후기", "body": "닉 다녀왔습니다",
        "outing_date": None, "posted_at": posted,
        "cat": "E", "cat_label": "후기", "category": "풍경",
        "is_outing": False, "is_canceled": False,
        "likes": 1, "comments": 0, "images": 0,
        "needs_review": False, "review_reason": "",
        "is_active": True, "attendees": ["닉"],
    }


def photo(pid, posted, has_comment=True):
    return {
        "id": pid, "author": "닉", "wid": "w1", "posted_at": posted,
        "likes": 3, "comments": 1 if has_comment else 0,
        "has_comment": has_comment, "is_active": True,
        # st.image가 실제로 열어 보므로 http URL이어야 한다(네트워크 호출은 없음).
        "url_large": f"https://example.invalid/{pid}.png",
        "url_medium": f"https://example.invalid/{pid}m.png",
        "url_small": f"https://example.invalid/{pid}s.png",
        "url_thumb": f"https://example.invalid/{pid}n.png",
    }


def seed(at, start_ym, end_ym, posts, photos, members=None):
    """③ 인사이트까지 바로 진입하도록 세션 상태를 심는다."""
    master = {"names": {"닉"}, "members": members or [], "banned": set(),
              "resolution": {}, "join_aliases": {}, "duplicates": set()}
    at.session_state["data"] = (start_ym, end_ym, posts, photos,
                                members or [], set(), {}, {})
    at.session_state["master"] = master
    at.session_state["result"] = (start_ym, end_ym, posts, photos, b"",
                                  master, members or [], {})


MULTI_YEAR_POSTS = [
    notice("a", "2025-09-05"),
    notice("b", "2025-12-20", category="인물"),
    notice("c", "2026-03-07"),
    notice("d", "2025-03-02"),          # 2026-03과 충돌하면 안 되는 건
    notice("e", "2026-01-11", canceled=True),
    review("r1", datetime(2025, 9, 6, 9, 0)),
    review("r2", datetime(2026, 3, 8, 9, 0)),
]
MULTI_YEAR_PHOTOS = [
    photo("p1", datetime(2025, 9, 7, 9, 0)),
    photo("p2", datetime(2026, 3, 9, 9, 0)),
    photo("p3", datetime(2026, 3, 10, 9, 0), has_comment=False),
]


def test_renders_multi_year_range_without_exception():
    """다년 범위에서 10개 탭이 모두 예외 없이 그려져야 한다."""
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()
    seed(at, 202503, 202603, MULTI_YEAR_POSTS, MULTI_YEAR_PHOTOS)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]


def test_renders_single_month_range_without_exception():
    """축이 1칸이어도 차트·표가 깨지지 않아야 한다."""
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()
    seed(at, 202603, 202603,
         [notice("c", "2026-03-07"), review("r", datetime(2026, 3, 8, 9, 0))],
         [photo("p", datetime(2026, 3, 9, 9, 0))])
    at.run()
    assert not at.exception, [str(e) for e in at.exception]


def test_renders_empty_dataset_without_exception():
    """수집 결과가 비어도 렌더가 죽지 않아야 한다."""
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()
    seed(at, 202601, 202612, [], [])
    at.run()
    assert not at.exception, [str(e) for e in at.exception]


def test_period_label_shown_for_multi_year():
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()
    seed(at, 202509, 202603, MULTI_YEAR_POSTS, MULTI_YEAR_PHOTOS)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    text = " ".join(s.value for s in at.subheader)
    assert "2025-09 ~ 2026-03" in text


def test_sidebar_rejects_reversed_range():
    """종료가 시작보다 빠르면 에러를 띄우고 수집 버튼을 막는다."""
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()
    at.session_state["api_start_y"] = 2026
    at.session_state["api_start_m"] = 6
    at.session_state["api_end_y"] = 2026
    at.session_state["api_end_m"] = 3
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any("종료가 시작보다" in e.value for e in at.error)


MEMBERS = [{"mid": "w1", "mn": "닉", "joined_at": datetime(2025, 8, 1),
            "last_visit": datetime(2026, 3, 1), "is_admin": False, "os": "iOS"}]


def _stage1(posts):
    """① 미매칭 이름 정리 화면까지 진입한 AppTest를 돌려준다."""
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.run()
    at.session_state["data"] = (202603, 202603, posts, [], MEMBERS, set(), {}, {})
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    return at


def test_stage1_bulk_noise_button_marks_pending():
    """① 단계의 '빈도 1회 전체 ❌' 한 번으로 노이즈가 모두 대기 목록에 담긴다."""
    posts = [review("r1", datetime(2026, 3, 8, 9, 0))]
    posts[0]["body"] = "가나다 라마바 와 함께 촬영했어요"
    at = _stage1(posts)

    targets = [b for b in at.button if "빈도 1회 전체" in b.label]
    assert targets, [b.label for b in at.button]
    targets[0].click().run()
    assert not at.exception, [str(e) for e in at.exception]
    # 본문에서 뽑힌 1회짜리 토큰이 전부 ❌ 대기로 들어가야 한다
    assert "가나다" in at.session_state["_noise_pending"]
    assert "라마바" in at.session_state["_noise_pending"]


def test_stage1_clear_all_resets_pending():
    posts = [review("r1", datetime(2026, 3, 8, 9, 0))]
    posts[0]["body"] = "가나다 라마바 와 함께 촬영했어요"
    at = _stage1(posts)

    [b for b in at.button if "빈도 1회 전체" in b.label][0].click().run()
    assert at.session_state["_noise_pending"]

    [b for b in at.button if "전부 해제" in b.label][0].click().run()
    assert not at.exception, [str(e) for e in at.exception]
    assert not at.session_state["_noise_pending"]


def test_stage1_editor_key_changes_so_checkboxes_refresh():
    """일괄 버튼은 editor nonce를 올려야 한다 — 안 그러면 표에 반영되지 않는다."""
    posts = [review("r1", datetime(2026, 3, 8, 9, 0))]
    posts[0]["body"] = "가나다 라마바 와 함께 촬영했어요"
    at = _stage1(posts)

    before = at.session_state["_res_editor_nonce"]
    [b for b in at.button if "빈도 1회 전체" in b.label][0].click().run()
    assert at.session_state["_res_editor_nonce"] > before
