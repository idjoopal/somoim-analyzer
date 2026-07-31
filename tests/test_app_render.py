"""앱 렌더 스모크 테스트 — Streamlit AppTest로 실제 화면을 그려 본다.

순수 함수 테스트는 집계만 덮기 때문에, 렌더 코드가 옛 계약을 붙들고 있어도
통과해 버린다. 여기서는 가짜 스토어를 주입해 render 경로를 끝까지 실행한다.
"""

from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

from core.store import (
    ATTENDEE_FIX_COLS,
    NAME_MAP_COLS,
    PHOTO_FIX_COLS,
    POST_FIX_COLS,
    POST_KEYS,
    TAB_ATTENDEE_FIX,
    TAB_MEMBER_NAMES,
    TAB_HISTORY,
    TAB_NAME_MAP,
    TAB_PHOTO_FIX,
    TAB_POST_FIX,
    TAB_POSTS,
    CorrectionStore,
    RawStore,
    records_to_rows,
)

APP = "streamlit_app.py"
TIMEOUT = 60


# ── 가짜 시트 클라이언트 (tests/test_store.py와 동일한 인메모리 모델) ──
class FakeClient:
    def __init__(self, tabs=None):
        self.tabs = {k: [list(r) for r in v] for k, v in (tabs or {}).items()}

    def ensure_tabs(self, file_id, tabs):
        made = [t for t in tabs if t not in self.tabs]
        for t in made:
            self.tabs[t] = []
        return made

    def read(self, file_id, tab):
        return [list(r) for r in self.tabs.get(tab, [])]

    def write(self, file_id, tab, rows):
        self.tabs[tab] = [list(r) for r in rows]

    def append(self, file_id, tab, rows):
        self.tabs.setdefault(tab, []).extend([list(r) for r in rows])

    def write_row(self, file_id, tab, row, row_index=1):
        rows = self.tabs.setdefault(tab, [])
        while len(rows) < row_index:
            rows.append([])
        cur = rows[row_index - 1]
        rows[row_index - 1] = list(row) + list(cur[len(row):])


def notice(pid, outing_date, category="풍경", canceled=False, posted=None):
    return {
        "id": pid, "author": "닉", "wid": "w1", "title": f"[{category}] {pid}",
        "body": "닉 참석", "outing_date": outing_date,
        "posted_at": posted or datetime(2025, 9, 1, 10, 0),
        "cat": "A", "cat_label": "공지", "category": category,
        "is_outing": True, "is_canceled": canceled,
        "likes": 2, "comments": 1, "images": 0,
        "needs_review": False, "review_reason": "",
    }


def review(pid, posted):
    return {
        "id": pid, "author": "닉", "wid": "w1", "title": f"{pid} 후기",
        "body": "닉 다녀왔습니다", "outing_date": None, "posted_at": posted,
        "cat": "E", "cat_label": "후기", "category": "풍경",
        "is_outing": False, "is_canceled": False,
        "likes": 1, "comments": 0, "images": 0,
        "needs_review": False, "review_reason": "",
    }


def photo(pid, posted, has_comment=True):
    return {
        "id": pid, "author": "닉", "wid": "w1", "posted_at": posted,
        "likes": 3, "comments": 1 if has_comment else 0, "has_comment": has_comment,
        # st.image가 실제로 열어 보므로 http URL이어야 한다(네트워크 호출은 없음).
        "url_large": f"https://example.invalid/{pid}.png",
        "url_medium": f"https://example.invalid/{pid}m.png",
        "url_small": f"https://example.invalid/{pid}s.png",
        "url_thumb": f"https://example.invalid/{pid}n.png",
    }


MEMBERS = [["mid", "mn", "is_admin", "joined_at", "last_visit", "os", "push"],
           ["w1", "닉", "FALSE", "2025-08-01 00:00:00", "2026-03-01 00:00:00", "iOS", "TRUE"]]

def greeting(pid, wid, posted):
    return {
        "id": pid, "author": "닉", "wid": wid, "title": "가입인사",
        "body": "잘 부탁드립니다", "outing_date": None, "posted_at": posted,
        "cat": "J", "cat_label": "가입인사", "category": None,
        "is_outing": False, "is_canceled": False,
        "likes": 0, "comments": 0, "images": 0,
        "needs_review": False, "review_reason": "",
    }


MULTI_YEAR = [
    notice("a", "2025-09-05"),
    notice("b", "2025-12-20", category="인물"),
    notice("c", "2026-03-07"),
    notice("d", "2025-03-02"),          # 2026-03과 섞이면 안 되는 건
    notice("e", "2026-01-11", canceled=True),
    review("r1", datetime(2025, 9, 6, 9, 0)),
    review("r2", datetime(2026, 3, 8, 9, 0)),
    greeting("j1", "w1", datetime(2025, 9, 2, 9, 0)),    # 지금도 멤버
    greeting("j2", "gone", datetime(2026, 1, 5, 9, 0)),  # 나간 사람
]
PHOTOS = [
    photo("p1", datetime(2025, 9, 7, 9, 0)),
    photo("p2", datetime(2026, 3, 9, 9, 0)),
    photo("p3", datetime(2026, 3, 10, 9, 0), has_comment=False),
]


def make_stores(posts=None, photos=None, history=None, corrections=None,
                members=None):
    raw_tabs = {
        TAB_POSTS: records_to_rows(posts or [], POST_KEYS),
        "사진": records_to_rows(photos or [], [
            "id", "author", "wid", "posted_at", "likes", "comments", "has_comment",
            "url_large", "url_medium", "url_small", "url_thumb"]),
        "멤버": members or MEMBERS,
        TAB_HISTORY: [["수집시각", "시작월", "종료월", "게시글", "사진", "멤버"]]
                     + (history or []),
    }
    fix_tabs = corrections or {
        TAB_NAME_MAP: [NAME_MAP_COLS],
        TAB_POST_FIX: [POST_FIX_COLS],
        TAB_ATTENDEE_FIX: [ATTENDEE_FIX_COLS],
    }
    return (RawStore(FakeClient(raw_tabs), "RAW"),
            CorrectionStore(FakeClient(fix_tabs), "FIX"))


def run(stores=None, secrets=None):
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    for k, v in (secrets or {}).items():
        at.secrets[k] = v
    at.run()
    if stores is not None:
        at.session_state["_stores"] = stores
        at.run()
    return at


# ═══════════════════════════════════════════════════════════════
# 비밀번호 게이트
# ═══════════════════════════════════════════════════════════════

def test_no_password_configured_skips_gate():
    at = run()
    assert not at.exception, [str(e) for e in at.exception]
    assert not at.text_input                      # 비밀번호 입력이 없어야 한다


def test_password_gate_blocks_until_correct():
    at = run(secrets={"auth": {"password": "열려라"}})
    assert not at.exception, [str(e) for e in at.exception]
    assert at.text_input                          # 게이트가 떠 있고
    assert not at.tabs                            # 본문은 안 보인다

    at.text_input[0].set_value("틀림").run()
    at.button[0].click().run()
    assert any("올바르지 않" in e.value for e in at.error)
    assert "_authed" not in at.session_state


def test_password_gate_opens_on_correct_value():
    at = run(secrets={"auth": {"password": "열려라"}})
    at.text_input[0].set_value("열려라").run()
    at.button[0].click().run()
    assert not at.exception, [str(e) for e in at.exception]
    assert at.session_state["_authed"] is True


# ═══════════════════════════════════════════════════════════════
# 데이터 없음 / 미설정
# ═══════════════════════════════════════════════════════════════

def test_without_google_config_shows_setup_notice():
    at = run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any("구글 연동" in e.value for e in at.error)


def test_empty_store_prompts_collection():
    at = run(stores=make_stores())
    assert not at.exception, [str(e) for e in at.exception]
    assert any("수집" in i.value for i in at.info)
    assert not at.tabs


# ═══════════════════════════════════════════════════════════════
# 저장된 데이터로 즉시 분석 — 요구사항의 핵심
# ═══════════════════════════════════════════════════════════════

def test_existing_data_renders_without_collecting():
    """수집 버튼을 누르지 않아도 시트에 있는 데이터로 바로 결과가 떠야 한다."""
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    assert not at.exception, [str(e) for e in at.exception]
    labels = [t.label for t in at.tabs]
    assert "📊 개요" in labels and "👥 참석 & 후기" in labels
    # 탭 개수가 아니라 **이름**을 본다 — 개수만 세면 탭을 합치거나 나눌 때마다
    # 무의미하게 깨지고, 정작 탭이 사라진 것은 못 잡는다.
    for gone in ("📋 데이터", "🎨 테마사진", "👤 사용자", "📝 후기", "🏷️ 카테고리"):
        assert gone not in labels, f"{gone} 탭은 다른 곳으로 합쳤다"


def test_opening_the_app_fills_the_correction_sheet():
    """"보정 n건 필요"라고만 뜨고 시트는 비어 있으면 무엇을 할지 알 수 없다.

    실제로 그랬다 — 시딩이 수집 때만 돌아서, 수집이 중간에 실패하면 화면은
    건수를 말하는데 시트에는 헤더밖에 없었다. 후보는 이미 저장된 raw에서
    파생되므로 앱을 여는 것만으로 채워져야 한다.
    """
    raw, fix = make_stores(MULTI_YEAR, PHOTOS)
    at = run(stores=(raw, fix))
    assert not at.exception, [str(e) for e in at.exception]

    # 멤버 실명은 raw 멤버에서 바로 나온다 — 반드시 깔려 있어야 한다.
    roster = fix.c.tabs[TAB_MEMBER_NAMES]
    assert len(roster) > 1, "이름매핑1이 헤더뿐이면 채울 곳이 없다"


def test_panel_count_matches_what_is_actually_in_the_sheet():
    """화면 숫자와 시트 내용이 어긋나면 사용자는 아무것도 할 수 없다."""
    raw, fix = make_stores(MULTI_YEAR, PHOTOS)
    at = run(stores=(raw, fix))
    assert not at.exception, [str(e) for e in at.exception]

    pending = at.session_state["_analysis"]["pending"]
    for tab, n in pending.items():
        rows = fix.c.tabs.get(tab, [])
        assert n <= max(len(rows) - 1, 0), f"{tab}: 화면 {n}건 vs 시트 {len(rows) - 1}행"


def test_multi_year_range_renders():
    hist = [["2026-07-28 10:00:00", 202503, 202603, 5, 3, 1]]
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS, history=hist))
    assert not at.exception, [str(e) for e in at.exception]
    assert any("2025-03 ~ 2026-03" in s.value for s in at.subheader)


def test_single_month_renders():
    hist = [["2026-07-28 10:00:00", 202603, 202603, 1, 1, 1]]
    at = run(stores=make_stores([notice("c", "2026-03-07")],
                                [photo("p", datetime(2026, 3, 9, 9, 0))],
                                history=hist))
    assert not at.exception, [str(e) for e in at.exception]


def test_history_drives_the_axis_over_data_extent():
    """수집했지만 글이 없던 달도 축에 남아야 '없음'과 '안 봄'이 구분된다."""
    hist = [["2026-07-28 10:00:00", 202401, 202612, 1, 0, 1]]
    at = run(stores=make_stores([notice("c", "2026-03-07")], [], history=hist))
    assert not at.exception, [str(e) for e in at.exception]
    assert any("2024-01 ~ 2026-12" in s.value for s in at.subheader)


# ═══════════════════════════════════════════════════════════════
# 보정 반영
# ═══════════════════════════════════════════════════════════════

def test_corrections_from_sheet_are_applied():
    corrections = {
        TAB_NAME_MAP: [NAME_MAP_COLS],
        TAB_POST_FIX: [POST_FIX_COLS,
                       ["c", "제목", "인물", "2026-05-05", "", "", ""]],
        TAB_ATTENDEE_FIX: [ATTENDEE_FIX_COLS],
    }
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS, corrections=corrections))
    assert not at.exception, [str(e) for e in at.exception]
    fixed = [p for p in at.session_state["_analysis"]["posts"] if p["id"] == "c"][0]
    assert fixed["category"] == "인물"
    assert fixed["outing_date"] == "2026-05-05"


def test_excluded_posts_are_dropped_from_analysis():
    corrections = {
        TAB_NAME_MAP: [NAME_MAP_COLS],
        TAB_POST_FIX: [POST_FIX_COLS, ["d", "제목", "", "", "", "TRUE", ""]],
        TAB_ATTENDEE_FIX: [ATTENDEE_FIX_COLS],
    }
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS, corrections=corrections))
    assert not at.exception, [str(e) for e in at.exception]
    ids = [p["id"] for p in at.session_state["_analysis"]["posts"]]
    assert "d" not in ids and "c" in ids


def test_attendee_correction_survives_annotation():
    """annotate_attendees가 본문에서 다시 뽑아 덮어쓰지 않아야 한다."""
    corrections = {
        TAB_NAME_MAP: [NAME_MAP_COLS],
        TAB_POST_FIX: [POST_FIX_COLS],
        TAB_ATTENDEE_FIX: [ATTENDEE_FIX_COLS, ["r1", "제목", "정원석, 이하얀", ""]],
    }
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS, corrections=corrections))
    assert not at.exception, [str(e) for e in at.exception]
    r1 = [p for p in at.session_state["_analysis"]["posts"] if p["id"] == "r1"][0]
    assert r1["attendees"] == ["정원석", "이하얀"]


# ═══════════════════════════════════════════════════════════════
# 사이드바
# ═══════════════════════════════════════════════════════════════

def test_sidebar_rejects_reversed_collection_range():
    at = run(stores=make_stores())
    at.session_state["api_start_y"] = 2026
    at.session_state["api_start_m"] = 6
    at.session_state["api_end_y"] = 2026
    at.session_state["api_end_m"] = 3
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any("종료가 시작보다" in e.value for e in at.error)


def test_refresh_clears_analysis_cache():
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    assert "_analysis" in at.session_state
    [b for b in at.button if "새로고침" in b.label][0].click().run()
    assert not at.exception, [str(e) for e in at.exception]


def test_pending_corrections_are_surfaced():
    corrections = {
        TAB_NAME_MAP: [NAME_MAP_COLS, ["가나다", "", 3, ""]],
        TAB_POST_FIX: [POST_FIX_COLS],
        TAB_ATTENDEE_FIX: [ATTENDEE_FIX_COLS],
    }
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS, corrections=corrections))
    assert not at.exception, [str(e) for e in at.exception]
    assert any("채우지 않은 보정" in w.value for w in at.warning)


# ═══════════════════════════════════════════════════════════════
# 연동 실패가 화면에 보여야 한다
# ═══════════════════════════════════════════════════════════════
#
# Streamlit Cloud는 잡히지 않은 예외의 메시지를 가린다. 조치 방법을 적어 둔
# 안내가 로그에만 남으면 사용자는 무엇을 고쳐야 할지 알 수 없다.

FAKE_CREDS = '{"client_email": "a@b.iam.gserviceaccount.com", "private_key": "-----K-----"}'


def test_missing_folder_id_is_reported_before_calling_google():
    """folder_id 없이는 반드시 실패하므로, 시도하기 전에 알려 준다."""
    at = run(secrets={"gsheets": {"credentials": FAKE_CREDS}})
    assert not at.exception, [str(e) for e in at.exception]
    assert any("folder_id" in e.value for e in at.error)


def test_store_open_failure_shows_message_instead_of_crashing():
    """GSheetsError가 화면에 뜨고 앱은 계속 그려져야 한다.

    자격증명이 JSON이 아니면 `_open_stores` 안에서 `parse_credentials`가
    GSheetsError를 던진다 — 네트워크 없이 실제 실패 경로를 그대로 탄다.
    """
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    at.secrets["gsheets"] = {"credentials": "이건 JSON이 아님", "folder_id": "FOLDER1"}
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any("JSON" in e.value for e in at.error)


class BoomClient(FakeClient):
    """읽기만 터지는 시트 — 연동은 됐는데 시트를 못 읽는 상황."""

    def read(self, file_id, tab):
        raise RuntimeError("HttpError 403: caller does not have permission")


def _boom_stores():
    return (RawStore(BoomClient(), "RAW"), CorrectionStore(BoomClient(), "FIX"))


def test_a_failed_sheet_read_tells_the_user_what_to_check():
    """읽기 실패는 **원인별 진단이 아니라 확인 순서**를 준다.

    구글 API 예외는 같은 원인에도 메시지가 제각각이라 문자열로 갈라 짚으면
    틀린 쪽을 가리키기 쉽다.
    """
    at = run(stores=_boom_stores())
    assert not at.exception, [str(e) for e in at.exception]
    말 = " ".join(e.value for e in at.error)
    assert "읽지 못했습니다" in 말, 말
    for 단서 in ("새로고침", "공유", "탭"):
        assert 단서 in 말, (단서, 말)


def test_a_failed_read_does_not_tell_the_user_to_go_collect():
    """시트에 데이터가 멀쩡히 있는 사람에게 "수집하세요"는 엉뚱한 지시다.

    실패와 "아직 안 받음"은 둘 다 `load_analysis`가 None을 돌려주므로,
    구분하지 않으면 진짜 안내 밑에 어긋나는 말이 하나 더 붙는다.
    """
    at = run(stores=_boom_stores())
    assert not at.exception, [str(e) for e in at.exception]
    assert not any("수집" in i.value for i in at.info), [i.value for i in at.info]


def _btn(at, label):
    return next(b for b in at.button if label in b.label)


def test_the_guide_offers_both_a_retry_and_a_reconnect():
    """둘은 **하는 일이 다르다.** 읽기만 다시 하는 것과 캐시된 연결까지 버리는 것.

    어느 쪽이 필요한지 사용자가 스스로 알아내기는 어려우므로 둘 다 내놓는다.
    """
    at = run(stores=_boom_stores())
    labels = [b.label for b in at.button]
    assert any("다시 읽기" in x for x in labels), labels
    assert any("연결 다시 맺기" in x for x in labels), labels


def test_retrying_the_read_recovers_once_the_sheet_answers():
    at = run(stores=_boom_stores())
    at.session_state["_stores"] = make_stores(MULTI_YEAR, PHOTOS)
    _btn(at, "다시 읽기").click().run()
    assert not at.exception, [str(e) for e in at.exception]
    assert "_read_failed" not in at.session_state
    assert at.tabs


def test_the_connection_is_cached_and_can_be_dropped():
    """`연결 다시 맺기`가 기대는 API를 못 박는다.

    연결이 `@st.cache_resource`라 **브라우저 새로고침으로는 안 풀린다** —
    서버에 캐시돼 세션을 넘어 살아남는다. 버튼은 `.clear()`로 그것을 버리는데,
    데코레이터가 빠지면 `.clear`가 사라져 버튼이 터진다.
    """
    import streamlit_app

    assert callable(getattr(streamlit_app._open_stores, "clear", None))


def test_reconnecting_retries_and_does_not_crash():
    """캐시를 버린 뒤에도 앱이 멀쩡히 다시 그려지고, 읽기를 다시 시도한다."""
    at = run(stores=_boom_stores())
    at.session_state["_stores"] = make_stores(MULTI_YEAR, PHOTOS)
    _btn(at, "연결 다시 맺기").click().run()
    assert not at.exception, [str(e) for e in at.exception]
    assert "_read_failed" not in at.session_state
    assert at.tabs


def test_a_read_failure_clears_once_the_sheet_is_readable_again():
    """실패 표시가 세션에 눌어붙으면 고친 뒤에도 화면이 안 돌아온다."""
    at = run(stores=_boom_stores())
    assert "_read_failed" in at.session_state
    at.session_state["_stores"] = make_stores(MULTI_YEAR, PHOTOS)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    assert "_read_failed" not in at.session_state
    assert at.tabs                                   # 결과 화면이 돌아온다


def test_hidden_theme_photos_are_reachable_regardless_of_period():
    """숨긴 사진 목록은 "내가 뭘 숨겼나"이지 기간별 뷰가 아니다.

    기간으로 자르면 다른 기간에서 해제한 사진을 앱에서 되돌릴 수 없게 되고,
    보정 시트를 손으로 열어야만 복구할 수 있다.
    """
    raw, fix = make_stores(MULTI_YEAR, PHOTOS)
    at = run(stores=(raw, fix))
    assert not at.exception, [str(e) for e in at.exception]

    ids = {str(p["id"]) for p in at.session_state["_all_photos"]}
    assert ids == {"p1", "p2", "p3"}, "기간 밖 사진이 빠지면 되돌릴 수 없다"


def test_excel_export_is_hidden_while_the_builder_is_out_of_sync():
    """엑셀이 화면과 다른 내용을 내보내면 어느 쪽이 맞는지 알 수 없게 된다.

    숨긴 것이 실수가 아니라 결정이라는 것을 여기서 못 박는다. 엑셀을 화면에
    맞춘 뒤 `SHOW_EXPORT = True`로 돌리면 이 테스트를 뒤집으면 된다.
    """
    import streamlit_app

    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    assert not at.exception, [str(e) for e in at.exception]
    assert streamlit_app.SHOW_EXPORT is False
    labels = [b.label for b in at.sidebar.button] + \
             [b.label for b in at.sidebar.download_button]
    assert not any("엑셀" in x or "내보내기" in x for x in labels)


def test_members_tab_counts_joiners_who_already_left():
    """멤버 목록에는 나간 사람이 없다 — 가입인사로 세야 실제 가입 수가 나온다."""
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    assert not at.exception, [str(e) for e in at.exception]

    from streamlit_app import joiner_retention
    analysis = at.session_state["_analysis"]
    rows = joiner_retention(analysis["posts"], analysis["members"],
                            [202509, 202601])
    assert rows[0]["잔류"] == 1        # w1은 멤버 목록에 있다
    assert rows[1]["이탈"] == 1        # gone은 없다


# ═══════════════════════════════════════════════════════════════
# 테마사진 체크 — 사진에 붙어 있고, 눌러도 앱 전체가 다시 그려지지 않는다
# ═══════════════════════════════════════════════════════════════

class _RecordingBox:
    """`st.container(border=True)`가 돌려주는 칸 흉내."""

    def __init__(self):
        self.calls = []
        self.checkbox_value = None

    def image(self, *a, **k):
        self.calls.append("image")

    def checkbox(self, label, *a, **k):
        self.calls.append(f"checkbox:{label}")
        self.checkbox_value = k.get("value", False)
        return self.checkbox_value

    def caption(self, *a, **k):
        self.calls.append("caption")


class _RecordingCol:
    def __init__(self):
        self.box = _RecordingBox()
        self.border = None

    def container(self, border=False):
        self.border = border
        return self.box


PHOTO_CARD_SAMPLE = {"id": "p9", "author": "나무", "likes": 3, "comments": 1,
                     "url_small": "u"}


def test_theme_checkbox_sits_inside_the_photo_box():
    """체크박스가 사진과 **같은 테두리 칸 안, 바로 아래**에 있어야 한다.

    격자로 늘어놓으면 사진 사이 간격과 사진·체크박스 간격이 비슷해서, 테두리
    없이는 어느 사진의 체크인지 헷갈린다. 캡션이 사이에 끼어도 마찬가지다.
    """
    from streamlit_app import _photo_card

    col = _RecordingCol()
    _photo_card(col, PHOTO_CARD_SAMPLE, fix_store=object(), excluded_ids=set())

    assert col.border is True, "테두리가 없으면 무엇에 대한 체크인지 안 보인다"
    assert col.box.calls == ["image", "checkbox:테마 아님", "caption"]


def test_theme_section_is_a_fragment():
    """체크 한 번에 앱 전체를 다시 그리면 이어서 체크할 수가 없다.

    수천 장에 이름을 다시 붙이고 다섯 탭을 통째로 재계산하는 비용이라,
    프래그먼트가 아니면 이 화면은 쓸 수 없다.
    """
    from streamlit_app import _theme_section

    assert getattr(_theme_section, "__wrapped__", None) is not None
    assert "fragment" in _theme_section.__code__.co_filename


def open_theme_month(at, ym):
    """월 expander를 펼친다 — 닫힌 달의 사진은 아예 그려지지 않는다."""
    at.session_state[f"thm_open_{ym}"] = True
    at.run()
    return at


def test_checking_one_photo_enables_save_right_away():
    """체크 한 장이면 저장 버튼이 곧바로 켜져야 한다.

    예전에는 화면 위쪽 경고·버튼을 그린 **뒤에** 체크박스에서 상태를 모아서,
    한 장만 체크하면 저장 버튼이 계속 꺼져 있었다(한 박자 늦음).
    """
    at = open_theme_month(run(stores=make_stores(MULTI_YEAR, PHOTOS)), 202603)
    assert not at.exception, [str(e) for e in at.exception]

    at.checkbox(key="thm_p2").check().run()
    assert not at.exception, [str(e) for e in at.exception]
    assert at.session_state["_theme_pending"] == {"p2": True}
    save = [b for b in at.button if "변경 저장" in b.label][0]
    assert not save.disabled, "체크했는데 저장이 꺼져 있으면 저장할 방법이 없다"


def test_discarding_unchecks_the_boxes():
    """되돌리기 한 번으로 체크가 실제로 풀려야 한다."""
    at = open_theme_month(run(stores=make_stores(MULTI_YEAR, PHOTOS)), 202603)
    at.checkbox(key="thm_p2").check().run()
    assert at.session_state["_theme_pending"] == {"p2": True}

    [b for b in at.button if "되돌리기" in b.label][0].click().run()
    assert not at.exception, [str(e) for e in at.exception]
    assert at.session_state["_theme_pending"] == {}
    # 되돌린 뒤 그 달을 다시 열어 확인한다 — 체크가 실제로 풀려 있어야 한다.
    open_theme_month(at, 202603)
    assert at.checkbox(key="thm_p2").value is False


def test_discarding_clears_the_checkbox_widgets():
    """`_theme_pending`만 비우면 안 된다 — 체크박스 위젯이 켜진 채로 남으면
    다음 렌더에서 `_collect_pending`이 도로 주워 담는다."""
    import streamlit_app as app

    class _Stop(Exception):
        pass

    class _FakeSt:
        def __init__(self, state):
            self.session_state = state

        def rerun(self, **_):
            raise _Stop

    state = {"_theme_pending": {"p2": True}, "thm_p2": True, "thm_p3": False}
    orig = app.st
    app.st = _FakeSt(state)
    try:
        with pytest.raises(_Stop):
            app._discard_theme_flags()
    finally:
        app.st = orig

    assert state["_theme_pending"] == {}
    assert "thm_p2" not in state          # 켜진 채 남으면 되살아난다
    assert state["thm_p3"] is False       # 바꾼 적 없는 것은 건드리지 않는다


# ═══════════════════════════════════════════════════════════════
# 본문 잘림 안내
# ═══════════════════════════════════════════════════════════════

def _long_review(pid, posted, body):
    r = review(pid, posted)
    r["body"] = body
    return r


CUT = "가" * 120
TRUNCATED = [
    notice("a", "2026-03-05"),
    _long_review("r1", datetime(2026, 3, 6, 9, 0), CUT),
    _long_review("r2", datetime(2026, 3, 7, 9, 0), CUT),
    _long_review("r3", datetime(2026, 3, 8, 9, 0), CUT),
    _long_review("r4", datetime(2026, 3, 9, 9, 0), "닉 다녀왔습니다"),
]


def test_truncated_bodies_are_called_out_in_the_review_detail():
    """길이를 안 보여 주면, 명단이 짧은 게 진짜인지 잘린 탓인지 알 수 없다."""
    at = run(stores=make_stores(TRUNCATED, []))
    assert not at.exception, [str(e) for e in at.exception]

    warnings = [w.value for w in at.warning]
    assert any("120자" in w and "빠졌을 수 있습니다" in w for w in warnings)
    assert at.session_state["_analysis"]["body_cut"] == 120


def test_intact_data_says_nothing_about_truncation():
    """벽이 없으면 조용해야 한다 — 거짓 경고는 진짜 경고를 죽인다."""
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    assert not at.exception, [str(e) for e in at.exception]
    assert at.session_state["_analysis"]["body_cut"] is None
    assert not any("잘림" in w.value or "빠졌을 수" in w.value for w in at.warning)


def test_truncated_reviews_are_seeded_into_the_attendee_sheet():
    """화면에서 "고쳐야 한다"고만 하고 시트에 줄이 없으면 할 일을 알 수 없다."""
    raw, fix = make_stores(TRUNCATED, [])
    at = run(stores=(raw, fix))
    assert not at.exception, [str(e) for e in at.exception]

    rows = fix.c.tabs[TAB_ATTENDEE_FIX]
    assert rows[0] == ATTENDEE_FIX_COLS
    seeded = {r[0]: r for r in rows[1:]}
    assert {"r1", "r2", "r3"} <= set(seeded), "잘린 후기가 후보에 없다"
    assert seeded["r1"][5] == 120                     # 본문길이
    assert seeded["r1"][6] == "⚠️ 잘림 의심"


# ═══════════════════════════════════════════════════════════════
# 펼친 달이 접히지 않게 · 잘린 후기를 눈에 띄게
# ═══════════════════════════════════════════════════════════════
#
# AppTest는 브라우저와 달리 expander의 위젯 상태를 되돌려 보내지 않는다
# (`ElementTree.get_widget_states`는 **엘리먼트**만 훑고 expander는 블록이다).
# 그래서 "리런 뒤에도 열려 있는가"를 여기서 끝까지 볼 수는 없다. 대신 열린
# 채로 남게 만드는 **조건 자체**를 못 박는다 — `key` + `on_change`가 있어야
# 스트림릿이 expander를 위젯으로 등록하고, 그때만 proto에 id가 실린다.

def all_expanders(at):
    """아이콘이 붙은 expander를 AppTest는 `status`로 분류한다 — 둘 다 모은다."""
    return list(at.expander) + list(at.get("status"))


def test_theme_month_expanders_are_stateful():
    """상태 없는 expander는 리런마다 접힌다 — 체크를 이어서 할 수가 없다."""
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    assert not at.exception, [str(e) for e in at.exception]

    months = [e for e in all_expanders(at) if "테마사진" in e.label]
    assert months, "테마사진 월 expander가 없다"
    for e in months:
        assert e.proto.id, f"{e.label} — 상태를 안 가지면 체크할 때마다 접힌다"


def test_closed_theme_month_draws_nothing():
    """닫힌 달까지 그리면 체크 한 번에 수백 개 위젯을 다시 만든다."""
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    assert "thm_p2" not in [c.key for c in at.checkbox]

    at.session_state["thm_open_202603"] = True
    at.run()
    assert "thm_p2" in [c.key for c in at.checkbox]


def test_photo_card_shows_the_unsaved_check():
    """닫힌 달은 안 그리므로 위젯 상태가 버려진다.

    저장된 값으로만 되살리면 그 달을 다시 열었을 때 체크가 풀린 채 나타나고,
    **저장을 누르기도 전에 변경이 사라진다.**
    """
    from streamlit_app import _photo_card

    col = _RecordingCol()
    _photo_card(col, PHOTO_CARD_SAMPLE, fix_store=object(), excluded_ids=set(),
                pending={"p9": True})
    assert col.box.checkbox_value is True

    col = _RecordingCol()
    _photo_card(col, PHOTO_CARD_SAMPLE, fix_store=object(),
                excluded_ids={"p9"}, pending={})
    assert col.box.checkbox_value is True, "시트에 저장된 값도 그대로 보여야 한다"


def test_review_section_is_a_fragment():
    """필터를 켤 때마다 앱 전체를 다시 그릴 이유가 없다."""
    from streamlit_app import _review_section

    assert getattr(_review_section, "__wrapped__", None) is not None
    assert "fragment" in _review_section.__code__.co_filename


def test_month_label_says_how_many_are_truncated():
    """모든 달을 열어 모든 카드를 읽어야 손볼 것을 찾는다면 표시가 없는 것과 같다."""
    at = run(stores=make_stores(TRUNCATED, []))
    assert not at.exception, [str(e) for e in at.exception]

    labels = [e.label for e in all_expanders(at) if "— 후기" in e.label]
    assert any("✂️ 잘림 3건" in x for x in labels), labels
    icons = [e.icon for e in all_expanders(at) if "✂️ 잘림" in e.label]
    assert icons and all(i == "✂️" for i in icons)


def test_intact_months_get_no_mark():
    """모든 줄에 붙으면 표시가 아니라 배경이 된다."""
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    for e in all_expanders(at):
        if "— 후기" in e.label:
            assert "✂️" not in e.label and e.icon != "✂️"


def test_truncated_card_gets_a_red_badge():
    """회색 캡션 한 조각은 쭉 내리며 훑을 때 눈에 걸리지 않는다."""
    at = run(stores=make_stores(TRUNCATED, []))
    at.session_state["rev_open_202603"] = True
    at.run()
    assert not at.exception, [str(e) for e in at.exception]

    badges = [m.value for m in at.markdown if ":red-badge[" in str(m.value)]
    assert len(badges) == 3, f"잘린 3건에만 붙어야 한다: {badges}"
    assert all("✂️" in b for b in badges)


def test_showing_only_truncated_reviews():
    """표시를 키워도 수백 건을 훑는 것보다는 걸러 내는 편이 빠르다."""
    at = run(stores=make_stores(TRUNCATED, []))
    before = [e.label for e in all_expanders(at) if "— 후기" in e.label][0]
    assert "후기 4건" in before and "✂️ 잘림 3건" in before

    at.toggle(key="rev_only_cut").set_value(True).run()
    assert not at.exception, [str(e) for e in at.exception]
    after = [e.label for e in all_expanders(at) if "— 후기" in e.label][0]
    assert "후기 3건" in after, after


def test_no_truncation_no_filter():
    """잘린 게 없으면 거를 것도 없다 — 쓸모없는 스위치를 두지 않는다."""
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    assert not any("잘린 후기만" in t.label for t in at.toggle)


# ═══════════════════════════════════════════════════════════════
# 함께 간 사람 — 컬럼이 고정 7개
# ═══════════════════════════════════════════════════════════════

PAIR_MEMBERS = [
    ["mid", "mn", "is_admin", "joined_at", "last_visit", "os", "push"],
    ["w1", "나무", "FALSE", "2025-08-01 00:00:00", "2026-03-01 00:00:00", "iOS", "TRUE"],
    ["w2", "바다", "FALSE", "2025-08-01 00:00:00", "2026-03-01 00:00:00", "iOS", "TRUE"],
    ["w3", "하늘", "FALSE", "2025-08-01 00:00:00", "2026-03-01 00:00:00", "iOS", "TRUE"],
]


def _pair_review(pid, posted, body):
    r = review(pid, posted)
    r["body"] = body
    return r


PAIRS = [
    notice("n1", "2026-03-07"),
    notice("n2", "2026-03-14"),
    _pair_review("pr1", datetime(2026, 3, 8, 9, 0), "나무 바다 다녀왔습니다"),
    _pair_review("pr2", datetime(2026, 3, 15, 9, 0), "나무 하늘 다녀왔습니다"),
]


def test_co_attendance_table_has_fixed_columns():
    """이름을 컬럼으로 쓰던 시절엔 쌍이 늘어날수록 표가 옆으로 늘어났다.

    순수 함수 테스트만으로는 이 증상을 못 잡는다 — 넓어지는 곳이
    `pd.DataFrame`이기 때문이다.
    """
    from streamlit_app import CO_ATTENDANCE_COLS

    at = run(stores=make_stores(PAIRS, [], members=PAIR_MEMBERS))
    assert not at.exception, [str(e) for e in at.exception]

    tables = [d.value for d in at.dataframe if "사람 A" in list(d.value.columns)]
    assert tables, "함께 간 사람 표가 없다"
    df = tables[0]
    assert list(df.columns) == CO_ATTENDANCE_COLS
    assert len(df) == 2, "나무·바다 / 나무·하늘 두 쌍"
    # 값까지 본다 — `columns=`로 틀만 잡으면 이름이 새어도 빈 칸 일곱 개가
    # 그려져 컬럼 검사만으로는 통과해 버린다.
    assert set(df["사람 A"]) == {"나무"}
    assert set(df["사람 B"]) == {"바다", "하늘"}
    assert list(df["함께"]) == [1, 1]
    assert list(df["A 참석"]) == [2, 2]     # 나무는 두 번 다 갔다
    assert list(df["A 기준"]) == [50.0, 50.0]


# ═══════════════════════════════════════════════════════════════
# 테마사진 화면 배치
# ═══════════════════════════════════════════════════════════════

def _with_hidden_photo(pid="p2"):
    """`테마사진보정`에 해제 행을 넣은 보정 시트."""
    return {
        TAB_NAME_MAP: [NAME_MAP_COLS],
        TAB_POST_FIX: [POST_FIX_COLS],
        TAB_ATTENDEE_FIX: [ATTENDEE_FIX_COLS],
        TAB_PHOTO_FIX: [PHOTO_FIX_COLS, [pid, "닉", "TRUE", ""]],
    }


def test_hidden_photos_sit_next_to_the_save_button():
    """되돌리기는 체크와 같은 작업이다 — 화면 양 끝에 떨어져 있으면 안 된다.

    맨 아래 있으면 월 목록을 다 지나 내려갔다가, 저장하러 다시 올라와야 한다.
    """
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS,
                                corrections=_with_hidden_photo()))
    assert not at.exception, [str(e) for e in at.exception]

    labels = [e.label for e in at.expander]          # 문서 순서대로 나온다
    hidden_at = [i for i, x in enumerate(labels) if "테마 아님으로 표시" in x]
    months_at = [i for i, x in enumerate(labels) if "테마사진" in x and "장" in x
                 and "표시" not in x]
    assert hidden_at, f"해제 목록이 없다: {labels}"
    assert months_at, f"월 목록이 없다: {labels}"
    assert hidden_at[0] < months_at[0], labels


def test_theme_matrix_is_gone():
    """지운 것이 실수가 아니라 결정이라는 것을 여기서 못 박는다.

    되살리려면 이 테스트를 뒤집으면 된다.
    """
    import streamlit_app

    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    assert not at.exception, [str(e) for e in at.exception]
    assert not any("테마 매트릭스" in str(m.value) for m in at.markdown)
    assert not hasattr(streamlit_app, "heatmap"), "부르는 곳 없는 차트 코드"


def test_no_theme_photos_says_so():
    """히트맵과 함께 사라지면 테마사진이 없을 때 화면이 그냥 텅 빈다."""
    at = run(stores=make_stores(MULTI_YEAR, [
        photo("q1", datetime(2026, 3, 9, 9, 0), has_comment=False)]))
    assert not at.exception, [str(e) for e in at.exception]
    assert any("테마사진" in i.value and "없습니다" in i.value for i in at.info)


# ═══════════════════════════════════════════════════════════════
# 🖼 갤러리 — 올라온 사진 전부에 닿는다
#
# 지연 로딩을 위젯 키로 판정한다. 이미지 개수로 보면 📷 사진 탭이 이미 인기
# 12장을 그려 기준선이 0이 아니라, "닫힌 달이 안 그려졌다"를 구분할 수 없다.
# ═══════════════════════════════════════════════════════════════

def _many(pid, posted, author="닉", likes=3):
    p = photo(pid, posted, has_comment=False)
    p["author"] = author
    p["likes"] = likes
    return p


# 5월에 45장(두 페이지), 6월에 1장. 6월이 가장 최근이라 자동으로 펼쳐지므로
# **5월은 닫힌 채로 남는다** — 그래야 "닫힌 달은 안 그린다"를 볼 수 있다.
MANY_PHOTOS = (
    [_many(f"m{i}", datetime(2026, 5, i % 28 + 1, 9, 0),
           author="닉" if i % 3 else "다른사람", likes=i)
     for i in range(45)]
    + [_many("newest", datetime(2026, 6, 1, 9, 0))]
)


def test_gallery_tab_exists_next_to_the_photo_tab():
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS))
    assert not at.exception, [str(e) for e in at.exception]
    labels = [t.label for t in at.tabs]
    assert "🖼 갤러리" in labels and "🔎 멤버 상세" in labels
    assert labels.index("🖼 갤러리") == labels.index("📷 사진") + 1
    assert labels.index("🔎 멤버 상세") == labels.index("🧑‍🤝‍🧑 멤버") + 1


def test_gallery_is_a_fragment():
    """필터·정렬·페이지 조작이 앱 전체를 다시 그릴 이유가 없다."""
    from streamlit_app import _gallery_section

    assert getattr(_gallery_section, "__wrapped__", None) is not None
    assert "fragment" in _gallery_section.__code__.co_filename


def gallery_months(at):
    return [e for e in all_expanders(at) if e.label.endswith("장")]


def open_gallery_month(at, ym, page=None):
    """갤러리 달을 펼친다(필요하면 페이지도 지정).

    `open_theme_month`과 같은 이유로 매번 다시 세팅한다 — AppTest에서 위젯을
    건드리면 expander가 등록해 둔 상태(닫힘)로 돌아간다. 실제 브라우저에서는
    `key`+`on_change` 덕에 열린 채로 남는다.
    """
    at.session_state[f"gal_open_{ym}"] = True
    if page is not None:
        at.session_state[f"gal_page_{ym}"] = page
    at.run()
    return at


def test_gallery_month_expanders_are_stateful():
    """상태 없는 expander는 리런마다 접힌다 — 페이지를 넘길 수가 없다."""
    at = run(stores=make_stores(MULTI_YEAR, MANY_PHOTOS))
    assert not at.exception, [str(e) for e in at.exception]

    months = gallery_months(at)
    assert months, [e.label for e in all_expanders(at)]
    for e in months:
        assert e.proto.id, f"{e.label} — 상태를 안 가지면 페이지를 넘길 때 접힌다"


def test_closed_gallery_month_requests_nothing():
    """사진 한 장이 곧 CDN 요청 한 번이다 — 닫힌 달을 그리면 안 된다."""
    at = run(stores=make_stores(MULTI_YEAR, MANY_PHOTOS))
    assert not at.exception, [str(e) for e in at.exception]
    keys = lambda: [n.key for n in at.number_input]      # noqa: E731
    assert "gal_page_202605" not in keys(), "닫힌 5월이 그려졌다"

    open_gallery_month(at, 202605)
    assert "gal_page_202605" in keys()


def test_gallery_reaches_photos_beyond_the_popular_twelve():
    """인기 12장 밖의 사진에 닿는 것이 이 탭의 존재 이유다."""
    at = open_gallery_month(run(stores=make_stores(MULTI_YEAR, MANY_PHOTOS)), 202605)
    assert not at.exception, [str(e) for e in at.exception]
    assert at.number_input(key="gal_page_202605").max == 2, "45장이면 40장씩 두 페이지"

    open_gallery_month(at, 202605, page=2)
    assert not at.exception, [str(e) for e in at.exception]
    assert any("41–45 / 45장" in str(c.value) for c in at.caption), \
        [str(c.value) for c in at.caption]


def test_whole_period_view_sorts_across_every_month():
    """월별 보기만 두면 정렬이 달 안에서만 걸려 '전 기간 좋아요 1등'을 볼 수 없다."""
    at = run(stores=make_stores(MULTI_YEAR, MANY_PHOTOS))
    at.radio(key="gal_mode").set_value("전체").run()
    at.selectbox(key="gal_sort").set_value("좋아요순").run()
    assert not at.exception, [str(e) for e in at.exception]

    # 46장 전체가 한 목록이다 — 월별이었다면 45 / 1로 갈렸다.
    assert any("1–40 / 46장" in str(c.value) for c in at.caption)
    assert not gallery_months(at), "전체 보기에서는 월 목록을 그리지 않는다"


def test_narrowing_the_uploader_filter_does_not_crash():
    """페이지 번호가 남아 있는데 페이지 수가 줄면 그대로 예외가 된다."""
    at = run(stores=make_stores(MULTI_YEAR, MANY_PHOTOS))
    at.radio(key="gal_mode").set_value("전체").run()
    at.number_input(key="gal_page_all").set_value(2).run()
    assert not at.exception, [str(e) for e in at.exception]

    at.selectbox(key="gal_author").set_value("다른사람").run()
    assert not at.exception, [str(e) for e in at.exception]
    assert at.session_state["gal_page_all"] == 1


# ═══════════════════════════════════════════════════════════════
# 🔎 멤버 상세
# ═══════════════════════════════════════════════════════════════

def test_member_focus_is_a_fragment():
    """드롭박스를 바꿀 때마다 앱 전체를 다시 그리면 이어서 훑을 수가 없다."""
    from streamlit_app import _tab_member_focus

    assert getattr(_tab_member_focus, "__wrapped__", None) is not None
    assert "fragment" in _tab_member_focus.__code__.co_filename


def test_member_focus_has_a_picker_and_draws_that_person():
    at = run(stores=make_stores(PAIRS, PHOTOS, members=PAIR_MEMBERS))
    assert not at.exception, [str(e) for e in at.exception]

    pick = at.selectbox(key="mf_member")
    assert pick.value == "나무", "참석이 가장 많은 사람이 먼저 온다"
    # 표시 문자열에 활동량을 붙여 둔다 — 누구를 볼지 고르는 단서가 이름뿐이면
    # 활동이 없는 사람을 골라 놓고 화면이 빈 줄 모른다.
    assert [o.split(" · ")[0] for o in pick.options] == ["나무", "바다", "하늘"]
    assert all("참석" in o and "사진" in o for o in pick.options)
    assert "참석률" in [m.label for m in at.metric]


def test_member_focus_switches_person():
    at = run(stores=make_stores(PAIRS, PHOTOS, members=PAIR_MEMBERS))
    at.selectbox(key="mf_member").set_value("하늘").run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any("### 하늘" in str(m.value) for m in at.markdown)


def test_member_focus_shows_companions_the_global_table_would_hide():
    """전역 표는 상위 N쌍이라 조용한 사람은 한 줄도 못 올라간다."""
    from streamlit_app import COMPANION_COLS

    at = run(stores=make_stores(PAIRS, PHOTOS, members=PAIR_MEMBERS))
    at.selectbox(key="mf_member").set_value("하늘").run()

    tables = [d.value for d in at.dataframe
              if "함께 간 사람" in list(d.value.columns)]
    assert tables, "동행 표가 없다"
    assert list(tables[0].columns) == COMPANION_COLS
    assert set(tables[0]["함께 간 사람"]) == {"나무"}


FOCUS_MEMBERS = [
    ["mid", "mn", "is_admin", "joined_at", "last_visit", "os", "push"],
    ["w1", "나무", "TRUE", "2025-08-01 00:00:00", "2026-03-01 00:00:00", "iOS", "TRUE"],
    ["w2", "바다", "FALSE", "2025-08-01 00:00:00", "2026-03-01 00:00:00", "iOS", "TRUE"],
    ["w3", "하늘", "FALSE", "2025-08-01 00:00:00", "2026-03-01 00:00:00", "iOS", "TRUE"],
    ["w4", "유령", "FALSE", "2025-08-01 00:00:00", "2026-03-01 00:00:00", "iOS", "TRUE"],
]


def test_member_focus_marks_who_this_person_is():
    """운영진·유령은 숫자만 봐서는 알 수 없다 — 표 위에 배지로 세운다."""
    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    assert not at.exception, [str(e) for e in at.exception]
    badges = lambda: [str(m.value) for m in at.markdown if "badge[" in str(m.value)]  # noqa: E731
    assert badges() == [":blue-badge[운영진]"]

    at.selectbox(key="mf_member").set_value("유령").run()
    assert not at.exception, [str(e) for e in at.exception]
    assert badges() == [":gray-badge[유령 — 이 기간 활동 0건]"]


def test_member_with_no_activity_draws_every_section_anyway():
    """빈 구역을 통째로 지우면 화면이 고장 난 것처럼 보인다."""
    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    at.selectbox(key="mf_member").set_value("유령").run()
    assert not at.exception, [str(e) for e in at.exception]
    said = [str(c.value) for c in at.caption]
    for missing in ("개최한 출사가 없습니다.", "참석 기록이 없습니다.",
                    "작성한 후기가 없습니다.", "업로드한 사진이 없습니다."):
        assert missing in said, missing


def test_member_focus_without_a_member_list_says_so():
    at = run(stores=make_stores(MULTI_YEAR, PHOTOS, members=[
        ["mid", "mn", "is_admin", "joined_at", "last_visit", "os", "push"]]))
    assert not at.exception, [str(e) for e in at.exception]
    assert any("멤버 정보가 없습니다" in i.value for i in at.info)


# ═══════════════════════════════════════════════════════════════
# 캡션과 값이 어긋나지 않는다 — 상수를 둔 이유 그 자체
# ═══════════════════════════════════════════════════════════════

def test_pair_caption_quotes_the_real_limit():
    """캡션에 숫자를 손으로 적어 두면 값을 올릴 때 화면이 거짓말을 한다."""
    from streamlit_app import CO_ATTENDANCE_COLS, CO_ATTENDANCE_TOP

    at = run(stores=make_stores(PAIRS, [], members=PAIR_MEMBERS))
    assert any(f"상위 {CO_ATTENDANCE_TOP}쌍" in str(c.value) for c in at.caption), \
        [str(c.value) for c in at.caption]
    df = [d.value for d in at.dataframe
          if list(d.value.columns) == CO_ATTENDANCE_COLS][0]
    assert len(df) <= CO_ATTENDANCE_TOP


def test_preference_caption_quotes_the_real_limit():
    from streamlit_app import PREF_TOP_N

    at = run(stores=make_stores(PAIRS, [], members=PAIR_MEMBERS))
    assert any(f"최대 {PREF_TOP_N}개" in str(c.value) for c in at.caption), \
        [str(c.value) for c in at.caption]


# ═══════════════════════════════════════════════════════════════
# 멤버 상세 — 자리·등수·칭호
# ═══════════════════════════════════════════════════════════════

def _headings(at):
    """`#### 제목` 마크다운을 문서 순서대로."""
    return [str(m.value) for m in at.markdown if str(m.value).startswith("####")]


def test_reviews_sit_right_under_the_outings_this_person_hosted():
    """자기가 연 출사를 보고 "그럼 후기는 쓰고 있나"를 확인하는 건 한 동작이다.

    사이에 표가 끼면 두 번 스크롤해 눈으로 맞춰야 한다 — 자리 자체가
    요구사항이라 순서를 못 박는다.
    """
    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    assert not at.exception, [str(e) for e in at.exception]

    order = [h for h in _headings(at)
             if any(k in h for k in ("개최한 출사", "작성한 후기", "참석한 출사"))]
    assert len(order) == 3, order
    assert "개최한 출사" in order[0]
    assert "작성한 후기" in order[1]
    assert "참석한 출사" in order[2]


def test_rank_is_a_metric_not_a_grey_caption():
    """등수는 이 화면에서 가장 궁금한 숫자 축인데 가장 작게 그려져 있었다."""
    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    labels = {m.label: m.value for m in at.metric}
    ranked = {k: v for k, v in labels.items() if k.endswith("명 중)")}
    assert len(ranked) == 3, labels
    assert any(k.startswith("참석") for k in ranked)
    assert any(k.startswith("개최") for k in ranked)
    assert all(v.endswith("등") or v == "—" for v in ranked.values()), ranked


def test_a_member_with_nothing_going_on_gets_no_title_box():
    """칭호가 없으면 구역을 아예 안 그린다 — 없는 게 흠으로 읽히면 안 된다."""
    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    at.selectbox(key="mf_member").set_value("유령").run()
    assert not at.exception, [str(e) for e in at.exception]
    # 유령은 `유령 회원` 하나를 받으므로 구역이 그려진다 — 이름과 근거가 함께.
    assert any("유령 회원" in str(m.value) for m in at.markdown)
    assert any("글·사진·참석이 하나도 없" in str(c.value) for c in at.caption)


def test_titles_come_with_the_reason_they_were_given():
    """이름만 붙으면 왜 붙었는지 물어볼 데가 없고 잘못 붙어도 못 알아챈다."""
    from streamlit_app import club_titles

    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    analysis = at.session_state["_analysis"]
    got = club_titles(analysis["posts"], analysis["photos"],
                      analysis["members"], [202603])
    titles = got["나무"]
    assert titles, "활동이 있는 사람인데 칭호가 하나도 없다"
    for t in titles:
        assert t["근거"], t


# ═══════════════════════════════════════════════════════════════
# 🏆 칭호 분포 — 기준이 적당한지 보는 곳
# ═══════════════════════════════════════════════════════════════

def test_closed_distribution_panel_computes_nothing():
    """쉰 명분 칭호를 매 rerun마다 돌릴 이유가 없다."""
    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    assert not any("칭호별 수령 인원" in str(m.value) for m in at.markdown)

    at.session_state["mf_dist_open"] = True
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    assert any("칭호별 수령 인원" in str(m.value) for m in at.markdown)


def test_distribution_keeps_titles_nobody_earned():
    """아무도 못 받는 칭호가 있다는 사실이 기준 조정에 필요한 정보다.

    안 걸린 것을 빼 버리면 화면만 보고는 그 칭호가 있는지도 모른다.
    """
    from streamlit_app import FIXED_TITLE_NAMES

    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    at.session_state["mf_dist_open"] = True
    at.run()

    tables = [d.value for d in at.dataframe if "받은 사람" in list(d.value.columns)]
    assert tables, "칭호별 수령 인원 표가 없다"
    df = tables[0]
    assert set(FIXED_TITLE_NAMES) <= set(df["칭호"]), \
        set(FIXED_TITLE_NAMES) - set(df["칭호"])
    assert (df["인원"] == 0).any(), "이 픽스처에서는 못 받는 칭호가 있어야 한다"


def test_distribution_counts_everyone_including_the_empty_handed():
    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    at.session_state["mf_dist_open"] = True
    at.run()

    labels = {m.label: m.value for m in at.metric}
    assert labels.get("전체 멤버") == "4명"          # FOCUS_MEMBERS 네 명
    counts = [d.value for d in at.dataframe if "칭호 수" in list(d.value.columns)]
    assert counts, "개수 분포 표가 없다"
    assert list(counts[0]["칭호 수"]) == ["0개", "1개", "2개", "3개"]


def test_distribution_lists_the_renamed_titles_not_the_old_ones():
    """이름을 바꾸면 고정 목록도 갈아 끼워야 한다 — 안 그러면 분포 화면에
    옛 이름이 0명으로 남고 새 이름은 목록 밖으로 밀려난다."""
    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    at.session_state["mf_dist_open"] = True
    at.run()
    assert not at.exception, [str(e) for e in at.exception]

    df = [d.value for d in at.dataframe if "받은 사람" in list(d.value.columns)][0]
    shown = set(df["칭호"])
    assert {"테마사진 프로 참석러", "아이고 어르신", "정출킬러",
            "여기 제 인스타인데..", "소모임에요? 글쎄.."} <= shown
    assert not ({"테마 단골", "터줏대감", "개근왕", "다작왕", "마당발",
                 "한 달도 안 빠졌네", "이분 출사는 항상 만석"} & shown)


def test_distribution_explains_the_quota():
    """정원에 딱 붙은 숫자는 "조건이 느슨하다"는 뜻이라 읽는 법을 적어 둔다."""
    from streamlit_app import TITLE_QUOTA_DEFAULT

    at = run(stores=make_stores(PAIRS, PHOTOS, members=FOCUS_MEMBERS))
    at.session_state["mf_dist_open"] = True
    at.run()
    assert any(f"{TITLE_QUOTA_DEFAULT}명" in str(c.value) for c in at.caption)
