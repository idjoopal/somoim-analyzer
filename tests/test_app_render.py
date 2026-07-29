"""앱 렌더 스모크 테스트 — Streamlit AppTest로 실제 화면을 그려 본다.

순수 함수 테스트는 집계만 덮기 때문에, 렌더 코드가 옛 계약을 붙들고 있어도
통과해 버린다. 여기서는 가짜 스토어를 주입해 render 경로를 끝까지 실행한다.
"""

from datetime import datetime

from streamlit.testing.v1 import AppTest

from core.store import (
    ATTENDEE_FIX_COLS,
    NAME_MAP_COLS,
    POST_FIX_COLS,
    POST_KEYS,
    TAB_ATTENDEE_FIX,
    TAB_MEMBER_NAMES,
    TAB_HISTORY,
    TAB_NAME_MAP,
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

MULTI_YEAR = [
    notice("a", "2025-09-05"),
    notice("b", "2025-12-20", category="인물"),
    notice("c", "2026-03-07"),
    notice("d", "2025-03-02"),          # 2026-03과 섞이면 안 되는 건
    notice("e", "2026-01-11", canceled=True),
    review("r1", datetime(2025, 9, 6, 9, 0)),
    review("r2", datetime(2026, 3, 8, 9, 0)),
]
PHOTOS = [
    photo("p1", datetime(2025, 9, 7, 9, 0)),
    photo("p2", datetime(2026, 3, 9, 9, 0)),
    photo("p3", datetime(2026, 3, 10, 9, 0), has_comment=False),
]


def make_stores(posts=None, photos=None, history=None, corrections=None):
    raw_tabs = {
        TAB_POSTS: records_to_rows(posts or [], POST_KEYS),
        "사진": records_to_rows(photos or [], [
            "id", "author", "wid", "posted_at", "likes", "comments", "has_comment",
            "url_large", "url_medium", "url_small", "url_thumb"]),
        "멤버": MEMBERS,
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
