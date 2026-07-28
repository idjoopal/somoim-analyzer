"""구글 시트 저장 계층 테스트 — 네트워크 무관 (SheetsClient를 가짜로 주입).

가장 중요한 것은 **불변식**이다: 수집이 보정 시트의 기존 값을 건드리지 않고,
기간이 달라져도 보정이 계속 적용되어야 한다. 그게 이 설계 전체의 존재 이유다.
"""

from datetime import date, datetime

import pytest

from core.collector import (
    ALL_CATS, LEFT_MEMBER, NOT_A_NAME,
    summarize_body_lengths, summarize_raw_fields,
)
from core.store import (
    CORRECTION_TITLE,
    RAW_TITLE,
    open_stores,
    ATTENDEE_FIX_COLS,
    NAME_MAP_COLS,
    POST_FIX_COLS,
    POST_KEYS,
    FIELD_COLS,
    TAB_ATTENDEE_FIX,
    TAB_FIELDS,
    TAB_GUIDE,
    TAB_HISTORY,
    TAB_NAME_MAP,
    TAB_POSTS,
    TAB_POST_FIX,
    CorrectionStore,
    RawStore,
    apply_corrections,
    coerce_bool,
    coerce_dt,
    coerce_iso_date,
    correction_candidates,
    filter_excluded,
    missing_rows,
    normalize_record,
    parse_corrections,
    records_to_rows,
    resolution_from_corrections,
    rows_to_records,
    upsert,
)


# ═══════════════════════════════════════════════════════════════
# 가짜 SheetsClient — 탭을 dict로 들고 있는 인메모리 시트
# ═══════════════════════════════════════════════════════════════

class FakeClient:
    def __init__(self, tabs=None):
        self.tabs = {k: [list(r) for r in v] for k, v in (tabs or {}).items()}
        self.cleared, self.appended = [], []
        self.notes, self.validations, self.frozen = {}, [], []

    def sheet_ids(self, file_id):
        return {t: 100 + i for i, t in enumerate(self.tabs)}

    def set_header_notes(self, file_id, tab, notes, sheet_id=None):
        self.notes[tab] = dict(notes)

    def set_validation(self, file_id, tab, col, values, strict=False, sheet_id=None):
        self.validations.append((tab, col, list(values)))

    def freeze_header(self, file_id, tab, sheet_id=None):
        self.frozen.append(tab)

    def ensure_tabs(self, file_id, tabs):
        made = [t for t in tabs if t not in self.tabs]
        for t in made:
            self.tabs[t] = []
        return made

    def read(self, file_id, tab):
        return [list(r) for r in self.tabs.get(tab, [])]

    def write(self, file_id, tab, rows):
        self.cleared.append(tab)
        self.tabs[tab] = [list(r) for r in rows]

    def append(self, file_id, tab, rows):
        self.appended.append((tab, len(rows)))
        self.tabs.setdefault(tab, []).extend([list(r) for r in rows])


def post(pid, cat="A", **kw):
    base = {
        "id": pid, "author": "닉", "wid": "w1", "title": f"[풍경] {pid}",
        "body": "본문", "outing_date": "2026-03-07",
        "posted_at": datetime(2026, 3, 1, 12, 0),
        "cat": cat, "cat_label": "공지", "category": "풍경",
        "is_outing": True, "is_canceled": False,
        "likes": 1, "comments": 0, "images": 0,
        "needs_review": False, "review_reason": "",
    }
    base.update(kw)
    return base


# ═══════════════════════════════════════════════════════════════
# 타입 정규화
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    (datetime(2026, 3, 7, 21, 30), datetime(2026, 3, 7, 21, 30)),
    (date(2026, 3, 7), datetime(2026, 3, 7)),
    ("2026-03-07T21:30:00", datetime(2026, 3, 7, 21, 30)),
    ("2026-03-07 21:30:00", datetime(2026, 3, 7, 21, 30)),
    ("2026/03/07", datetime(2026, 3, 7)),
    (46088, datetime(2026, 3, 7)),
])
def test_coerce_dt_handles_every_shape(raw, expected):
    assert coerce_dt(raw) == expected


@pytest.mark.parametrize("bad", [None, "", "   ", "아무말"])
def test_coerce_dt_none_for_unusable(bad):
    assert coerce_dt(bad) is None


def test_coerce_iso_date_normalizes():
    assert coerce_iso_date(datetime(2026, 3, 7, 9)) == "2026-03-07"
    assert coerce_iso_date("2026-03-07") == "2026-03-07"
    assert coerce_iso_date(None) is None


@pytest.mark.parametrize("v,expected", [
    ("TRUE", True), ("true", True), ("Y", True), (1, True), ("예", True), (True, True),
    ("FALSE", False), ("", False), (None, False), ("N", False), (0, False),
])
def test_coerce_bool(v, expected):
    assert coerce_bool(v) is expected


def test_normalize_record_fills_missing_keys():
    rec = normalize_record({"id": "p1"}, POST_KEYS)
    assert set(rec) == set(POST_KEYS)
    assert rec["posted_at"] is None and rec["is_canceled"] is False
    assert rec["review_reason"] == "" and rec["likes"] == 0


# ═══════════════════════════════════════════════════════════════
# 시트 ↔ 레코드
# ═══════════════════════════════════════════════════════════════

def test_rows_to_records_pads_short_rows():
    """구글은 뒤쪽 빈 셀을 생략해 돌려준다 — 키가 빠지면 안 된다."""
    rows = [["id", "title", "body"], ["p1", "제목"]]
    assert rows_to_records(rows) == [{"id": "p1", "title": "제목", "body": None}]


def test_rows_to_records_skips_blank_lines():
    rows = [["id"], ["p1"], [""], [None]]
    assert len(rows_to_records(rows)) == 1


def test_rows_to_records_empty():
    assert rows_to_records([]) == []


def test_records_to_rows_round_trip():
    original = [post("p1")]
    restored = rows_to_records(records_to_rows(original, POST_KEYS), POST_KEYS)
    assert restored[0]["posted_at"] == datetime(2026, 3, 1, 12, 0)
    assert restored[0]["outing_date"] == "2026-03-07"
    assert restored[0]["is_outing"] is True
    assert restored[0]["likes"] == 1


def test_records_to_rows_truncates_long_body():
    row = records_to_rows([post("p1", body="가" * 40000)], POST_KEYS)[1]
    assert len(row[POST_KEYS.index("body")]) == 32000


# ═══════════════════════════════════════════════════════════════
# upsert
# ═══════════════════════════════════════════════════════════════

def test_upsert_replaces_same_id_and_appends_new():
    existing = [{"id": "a", "likes": 1}, {"id": "b", "likes": 2}]
    merged = upsert(existing, [{"id": "b", "likes": 99}, {"id": "c", "likes": 3}])
    assert merged == [{"id": "a", "likes": 1}, {"id": "b", "likes": 99}, {"id": "c", "likes": 3}]


def test_upsert_keeps_existing_order():
    """시트를 열었을 때 행이 튀지 않아야 한다."""
    existing = [{"id": str(i)} for i in range(5)]
    merged = upsert(existing, [{"id": "2"}, {"id": "0"}])
    assert [r["id"] for r in merged] == ["0", "1", "2", "3", "4"]


def test_upsert_ignores_keyless_rows():
    assert upsert([], [{"id": None, "x": 1}, {"id": "", "x": 2}]) == []


def test_upsert_by_alternate_key():
    merged = upsert([{"mid": "m1", "mn": "옛"}], [{"mid": "m1", "mn": "새"}], key="mid")
    assert merged == [{"mid": "m1", "mn": "새"}]


# ═══════════════════════════════════════════════════════════════
# 보정 파싱
# ═══════════════════════════════════════════════════════════════

def test_parse_corrections_ignores_unfilled_rows():
    """값이 비어 있으면 '아직 보정 안 함' — 빈 문자열로 덮어쓰면 안 된다."""
    names = [NAME_MAP_COLS, ["가나다", "", 3, ""], ["라마바", "닉네임", 1, ""]]
    posts = [POST_FIX_COLS, ["p1", "제목", "", "", "", "", ""]]
    att = [ATTENDEE_FIX_COLS, ["r1", "제목", "", ""]]
    c = parse_corrections(names, posts, att)
    assert c["names"] == {"라마바": "닉네임"}
    assert c["posts"] == {}
    assert c["attendees"] == {}


def test_parse_corrections_reads_each_field():
    posts = [POST_FIX_COLS, ["p1", "제목", "인물", "2026-05-05", "TRUE", "FALSE", ""]]
    c = parse_corrections([], posts, [])
    assert c["posts"]["p1"] == {"category": "인물", "outing_date": "2026-05-05",
                                "is_canceled": True, "excluded": False}


def test_parse_corrections_splits_attendees():
    att = [ATTENDEE_FIX_COLS, ["r1", "제목", " 정원석, 이하얀 ,", ""]]
    assert parse_corrections([], [], att)["attendees"]["r1"] == ["정원석", "이하얀"]


def test_parse_corrections_trims_whitespace_keys():
    names = [NAME_MAP_COLS, ["  가나다  ", "  닉  ", 1, ""]]
    assert parse_corrections(names, [], [])["names"] == {"가나다": "닉"}


def test_resolution_accepts_korean_aliases():
    c = {"names": {"a": "__LEFT__", "b": "탈퇴", "c": "노이즈", "d": "❌", "e": "원석닉"}}
    r = resolution_from_corrections(c)
    assert r == {"a": LEFT_MEMBER, "b": LEFT_MEMBER, "c": NOT_A_NAME,
                 "d": NOT_A_NAME, "e": "원석닉"}


# ═══════════════════════════════════════════════════════════════
# 보정 적용
# ═══════════════════════════════════════════════════════════════

def test_apply_corrections_overrides_post_fields():
    posts = [post("p1", category=None, outing_date=None, needs_review=True,
                  review_reason="출사일 미상")]
    apply_corrections(posts, {"posts": {"p1": {"category": "인물",
                                               "outing_date": "2026-05-05",
                                               "is_canceled": True}}})
    p = posts[0]
    assert p["category"] == "인물" and p["outing_date"] == "2026-05-05"
    assert p["is_canceled"] is True
    assert p["needs_review"] is False and p["review_reason"] == ""


def test_apply_corrections_replaces_attendees():
    posts = [post("r1", cat="E", attendees=["잘못된"])]
    apply_corrections(posts, {"attendees": {"r1": ["정원석", "이하얀"]}})
    assert posts[0]["attendees"] == ["정원석", "이하얀"]
    assert posts[0]["attendees_needs_review"] is False


def test_excluded_is_flagged_not_dropped():
    """무엇이 빠졌는지 셀 수 있도록 목록에서 바로 지우지 않는다."""
    posts = [post("p1"), post("p2")]
    apply_corrections(posts, {"posts": {"p2": {"excluded": True}}})
    assert len(posts) == 2
    assert filter_excluded(posts) == [posts[0]]


def test_apply_corrections_counts():
    posts = [post("p1"), post("r1", cat="E")]
    counts = apply_corrections(posts, {"posts": {"p1": {"category": "인물"}},
                                       "attendees": {"r1": ["정원석"]}})
    assert counts == {"공지": 1, "참석자": 1}


def test_apply_corrections_empty_is_safe():
    posts = [post("p1")]
    assert apply_corrections(posts, {}) == {"공지": 0, "참석자": 0}
    assert posts[0]["category"] == "풍경"


# ═══════════════════════════════════════════════════════════════
# 🔒 불변식 — 이 설계의 핵심
# ═══════════════════════════════════════════════════════════════

def test_missing_rows_excludes_keys_already_present():
    existing = [NAME_MAP_COLS, ["가나다", "닉네임", 3, ""]]
    candidates = [["가나다", "", 5, ""], ["라마바", "", 1, ""]]
    assert missing_rows(existing, candidates) == [["라마바", "", 1, ""]]


def test_missing_rows_keeps_user_blank_rows_out():
    """사용자가 일부러 비워 둔 행이 시딩으로 되살아나 중복되면 안 된다."""
    existing = [NAME_MAP_COLS, ["가나다", "", 3, ""]]
    assert missing_rows(existing, [["가나다", "", 5, ""]]) == []


def test_seed_never_touches_existing_rows():
    """수집을 반복해도 사람이 채운 값이 그대로여야 한다."""
    c = FakeClient({TAB_NAME_MAP: [NAME_MAP_COLS, ["가나다", "정원석", 3, "확인함"]]})
    store = CorrectionStore(c, "F")
    before = [list(r) for r in c.tabs[TAB_NAME_MAP]]

    for _ in range(3):      # 세 번 수집한 셈
        store.seed({TAB_NAME_MAP: [["가나다", "", 99, ""], ["라마바", "", 1, ""]]})

    assert c.tabs[TAB_NAME_MAP][:2] == before          # 기존 행 불변
    assert c.cleared == []                             # 덮어쓰기 자체를 안 함
    assert [r[0] for r in c.tabs[TAB_NAME_MAP][1:]] == ["가나다", "라마바"]


def test_seed_reports_added_counts():
    c = FakeClient({TAB_NAME_MAP: [NAME_MAP_COLS], TAB_POST_FIX: [POST_FIX_COLS],
                    TAB_ATTENDEE_FIX: [ATTENDEE_FIX_COLS]})
    added = CorrectionStore(c, "F").seed({
        TAB_NAME_MAP: [["a", "", 1, ""]],
        TAB_POST_FIX: [], TAB_ATTENDEE_FIX: [["r1", "t", "", ""]],
    })
    assert added == {TAB_NAME_MAP: 1, TAB_POST_FIX: 0, TAB_ATTENDEE_FIX: 1}


def test_corrections_survive_a_different_period():
    """202501로 만든 보정이 202601 수집 결과에도 그대로 붙어야 한다 — 요구사항의 핵심."""
    c = FakeClient({
        TAB_NAME_MAP: [NAME_MAP_COLS, ["가나다", "정원석", 3, ""]],
        TAB_POST_FIX: [POST_FIX_COLS, ["p_old", "옛 공지", "인물", "2025-01-05", "", "", ""]],
        TAB_ATTENDEE_FIX: [ATTENDEE_FIX_COLS],
    })
    store = CorrectionStore(c, "F")
    corrections = store.load()

    # 완전히 다른 기간의 새 수집 결과에 같은 id가 섞여 있는 상황
    new_posts = [post("p_old", category=None, outing_date=None, needs_review=True),
                 post("p_new_2026")]
    apply_corrections(new_posts, corrections)

    assert new_posts[0]["category"] == "인물"
    assert new_posts[0]["outing_date"] == "2025-01-05"
    assert resolution_from_corrections(corrections)["가나다"] == "정원석"


def test_candidates_exclude_already_corrected():
    posts = [post("p1", needs_review=True), post("p2", needs_review=True),
             post("r1", cat="E", attendees_needs_review=True)]
    corrections = {"names": {"가나다": "정원석"}, "posts": {"p1": {"category": "인물"}},
                   "attendees": {}}
    cand = correction_candidates(posts, {"가나다": 3, "라마바": 1}, corrections)

    assert [r[0] for r in cand[TAB_NAME_MAP]] == ["라마바"]
    assert [r[0] for r in cand[TAB_POST_FIX]] == ["p2"]
    assert [r[0] for r in cand[TAB_ATTENDEE_FIX]] == ["r1"]


def test_candidates_sort_names_by_frequency():
    cand = correction_candidates([], {"드묾": 1, "잦음": 9, "보통": 4}, {})
    assert [r[0] for r in cand[TAB_NAME_MAP]] == ["잦음", "보통", "드묾"]


# ═══════════════════════════════════════════════════════════════
# RawStore
# ═══════════════════════════════════════════════════════════════

def test_raw_store_round_trip():
    c = FakeClient()
    store = RawStore(c, "F")
    store.ensure()
    store.save(posts=[post("p1")], photos=[], period=(202601, 202612))

    loaded = store.load()
    assert len(loaded["posts"]) == 1
    assert loaded["posts"][0]["posted_at"] == datetime(2026, 3, 1, 12, 0)
    assert loaded["posts"][0]["outing_date"] == "2026-03-07"


def test_raw_store_upserts_across_collections():
    """기간이 겹쳐 다시 수집해도 중복되지 않고 최신 값으로 갱신된다."""
    c = FakeClient()
    store = RawStore(c, "F")
    store.ensure()
    store.save(posts=[post("p1", likes=1), post("p2")], photos=[])
    store.save(posts=[post("p1", likes=99), post("p3")], photos=[])

    posts = store.load()["posts"]
    assert [p["id"] for p in posts] == ["p1", "p2", "p3"]
    assert posts[0]["likes"] == 99


def test_raw_store_history_appends_each_run():
    c = FakeClient()
    store = RawStore(c, "F")
    store.ensure()
    store.save(posts=[post("p1")], photos=[], period=(202509, 202603),
               now=datetime(2026, 7, 28, 10, 0))
    store.save(posts=[post("p2")], photos=[], period=(202401, 202412),
               now=datetime(2026, 7, 28, 11, 0))

    hist = c.tabs[TAB_HISTORY]
    assert hist[0][:3] == ["수집시각", "시작월", "종료월"]
    assert [r[1:3] for r in hist[1:]] == [[202509, 202603], [202401, 202412]]


def test_raw_store_merges_banned_and_aliases():
    c = FakeClient()
    store = RawStore(c, "F")
    store.ensure()
    store.save(banned={"탈퇴A"}, join_aliases={"정원석": "원석닉"})
    store.save(banned={"탈퇴B"}, join_aliases={"이하얀": "하얀"})

    loaded = store.load()
    assert loaded["banned"] == {"탈퇴A", "탈퇴B"}
    assert loaded["join_aliases"] == {"정원석": "원석닉", "이하얀": "하얀"}


def test_raw_store_load_empty_sheet():
    loaded = RawStore(FakeClient(), "F").load()
    assert loaded["posts"] == [] and loaded["banned"] == set()


# ═══════════════════════════════════════════════════════════════
# 상세 조회로 늘어난 열·탭 — 기존 시트를 깨지 않아야 한다
# ═══════════════════════════════════════════════════════════════

def test_old_sheet_without_the_new_columns_still_loads():
    """이미 쌓아 둔 시트에는 detail_at·image_urls 열도 댓글 탭도 없다.

    마이그레이션 없이 읽혀야 한다 — 안 그러면 사용자가 시트를 지워야 한다.
    """
    old_cols = [k for k in POST_KEYS if k not in ("detail_at", "image_urls")]
    c = FakeClient({TAB_POSTS: [old_cols,
                                [post("p1").get(k) for k in old_cols]]})
    loaded = RawStore(c, "F").load()

    assert len(loaded["posts"]) == 1
    assert loaded["posts"][0]["detail_at"] is None      # 없는 키는 None으로 채워진다
    assert loaded["comments"] == []


def test_detail_at_survives_recollection():
    """살아남지 않으면 재수집 때마다 수백 건을 다시 받는다."""
    c = FakeClient()
    store = RawStore(c, "F")
    store.ensure()
    store.save(posts=[post("p1", detail_at="2026-07-28 10:00:00", body="전문")],
               photos=[])
    store.save(posts=[post("p2")], photos=[])          # 다른 기간 재수집

    by_id = {p["id"]: p for p in store.load()["posts"]}
    assert by_id["p1"]["detail_at"] == "2026-07-28 10:00:00"
    assert by_id["p1"]["body"] == "전문"


def test_comments_upsert_by_id_across_collections():
    c = FakeClient()
    store = RawStore(c, "F")
    store.ensure()
    store.save(comments=[{"id": "c1", "post_id": "p1", "body": "처음"}])
    store.save(comments=[{"id": "c1", "post_id": "p1", "body": "수정됨"},
                         {"id": "c2", "post_id": "p1", "body": "새 댓글"}])

    comments = store.load()["comments"]
    assert [x["id"] for x in comments] == ["c1", "c2"]
    assert comments[0]["body"] == "수정됨"


def test_save_without_comments_leaves_the_tab_alone():
    """댓글을 안 넘긴 수집이 이미 쌓인 댓글을 지우면 안 된다."""
    c = FakeClient()
    store = RawStore(c, "F")
    store.ensure()
    store.save(comments=[{"id": "c1", "post_id": "p1", "body": "유지"}])
    store.save(posts=[post("p9")], photos=[])          # comments 미지정

    assert [x["id"] for x in store.load()["comments"]] == ["c1"]


# ═══════════════════════════════════════════════════════════════
# open_stores — 파일 id를 직접 주면 이름 탐색을 건너뛴다
#
# 이름 매칭은 글자 하나만 달라도 "파일이 없다 → 만들려다 403"으로 나타난다.
# ═══════════════════════════════════════════════════════════════

class FakeDriveStore:
    def __init__(self):
        self.looked_up = []

    def find_or_create(self, title):
        self.looked_up.append(title)
        return f"ID_{title}", False


def test_open_stores_finds_by_name_when_no_ids_given():
    drive = FakeDriveStore()
    raw, fix = open_stores(drive, FakeClient())
    assert drive.looked_up == [RAW_TITLE, CORRECTION_TITLE]
    assert (raw.file_id, fix.file_id) == (f"ID_{RAW_TITLE}", f"ID_{CORRECTION_TITLE}")


def test_open_stores_pinned_ids_skip_name_lookup_entirely():
    drive = FakeDriveStore()
    raw, fix = open_stores(drive, FakeClient(),
                           raw_file_id="RAW1", correction_file_id="FIX1")
    assert drive.looked_up == []                  # 드라이브 탐색을 아예 안 한다
    assert (raw.file_id, fix.file_id) == ("RAW1", "FIX1")


def test_open_stores_can_pin_just_one():
    drive = FakeDriveStore()
    raw, fix = open_stores(drive, FakeClient(), raw_file_id="RAW1")
    assert drive.looked_up == [CORRECTION_TITLE]
    assert (raw.file_id, fix.file_id) == ("RAW1", f"ID_{CORRECTION_TITLE}")


# ═══════════════════════════════════════════════════════════════
# CorrectionStore
# ═══════════════════════════════════════════════════════════════

def test_correction_store_ensure_writes_headers_once():
    c = FakeClient()
    CorrectionStore(c, "F").ensure()
    assert c.tabs[TAB_NAME_MAP] == [NAME_MAP_COLS]

    c.tabs[TAB_NAME_MAP].append(["가나다", "정원석", 1, ""])
    CorrectionStore(c, "F").ensure()                 # 두 번째 호출
    assert len(c.tabs[TAB_NAME_MAP]) == 2            # 헤더로 덮어쓰지 않음


# ═══════════════════════════════════════════════════════════════
# 보정 시트 가이드 — 설명 없는 보정 시트는 채울 수가 없다
# ═══════════════════════════════════════════════════════════════

def test_ensure_creates_usage_tab_with_the_three_special_values():
    c = FakeClient()
    CorrectionStore(c, "F").ensure()

    text = "\n".join(r[0] for r in c.tabs[TAB_GUIDE])
    assert LEFT_MEMBER in text and NOT_A_NAME in text
    assert "YYYY-MM-DD" in text                  # 출사일 형식
    assert "빈칸" in text                         # 빈칸 = 아직 보정 안 함


def test_ensure_notes_every_column_of_every_tab():
    """설명 없는 칸이 하나라도 있으면 거기서 막힌다."""
    c = FakeClient()
    CorrectionStore(c, "F").ensure()

    for tab, cols in ((TAB_NAME_MAP, NAME_MAP_COLS),
                      (TAB_POST_FIX, POST_FIX_COLS),
                      (TAB_ATTENDEE_FIX, ATTENDEE_FIX_COLS)):
        assert sorted(c.notes[tab]) == list(range(len(cols))), tab
        assert all(c.notes[tab].values()), tab
        assert tab in c.frozen


def test_ensure_without_members_still_offers_category_and_bool_dropdowns():
    """멤버 명단은 수집 전엔 없지만 카테고리·TRUE/FALSE는 항상 고정이다."""
    c = FakeClient()
    CorrectionStore(c, "F").ensure()

    by_col = {(t, col): v for t, col, v in c.validations}
    assert by_col[(TAB_POST_FIX, 2)] == list(ALL_CATS)
    assert by_col[(TAB_POST_FIX, 4)] == ["TRUE", "FALSE"]
    assert (TAB_NAME_MAP, 1) not in by_col       # 목록이 비면 걸지 않는다


def test_name_dropdown_lists_members_plus_special_values():
    c = FakeClient()
    CorrectionStore(c, "F").ensure(master_names={"정원석", "나무"})

    values = dict(((t, col), v) for t, col, v in c.validations)[(TAB_NAME_MAP, 1)]
    assert values == ["나무", "정원석", LEFT_MEMBER, NOT_A_NAME]


def test_seed_refreshes_name_dropdown_with_current_members():
    """멤버가 새로 들어오면 목록도 따라와야 한다 — 목록은 seed마다 갱신된다."""
    c = FakeClient()
    store = CorrectionStore(c, "F")
    store.ensure(master_names={"정원석"})
    c.validations.clear()

    store.seed({TAB_NAME_MAP: [["가나다", "", 1, ""]]}, master_names={"정원석", "신입"})
    assert dict(((t, col), v) for t, col, v in c.validations)[(TAB_NAME_MAP, 1)] == [
        "신입", "정원석", LEFT_MEMBER, NOT_A_NAME]


def test_guide_does_not_touch_user_filled_rows():
    """안내를 갱신한다고 사람이 채운 값이 사라지면 안 된다."""
    c = FakeClient({TAB_NAME_MAP: [NAME_MAP_COLS, ["가나다", "정원석", 1, "확인함"]]})
    CorrectionStore(c, "F").ensure(master_names={"정원석"})
    assert c.tabs[TAB_NAME_MAP][1] == ["가나다", "정원석", 1, "확인함"]


def test_formatting_failure_never_blocks_corrections():
    """서식은 부가 기능이다 — 실패해도 보정 시트 자체는 쓸 수 있어야 한다."""
    class NoFormatting(FakeClient):
        def sheet_ids(self, file_id):
            raise RuntimeError("이 시트에는 서식 권한이 없다")

    c = NoFormatting()
    CorrectionStore(c, "F").ensure()
    assert c.tabs[TAB_NAME_MAP] == [NAME_MAP_COLS]


def test_pending_count_counts_unfilled_rows():
    c = FakeClient({
        TAB_NAME_MAP: [NAME_MAP_COLS, ["a", "정원석", 1, ""], ["b", "", 1, ""]],
        TAB_POST_FIX: [POST_FIX_COLS, ["p1", "제목", "", "", "", "", ""]],
        TAB_ATTENDEE_FIX: [ATTENDEE_FIX_COLS, ["r1", "제목", "정원석", ""]],
    })
    assert CorrectionStore(c, "F").pending_count() == 2   # b, p1


# ═══════════════════════════════════════════════════════════════
# API 응답 진단 — 본문 잘림을 잡아내는 것이 목적
# ═══════════════════════════════════════════════════════════════

def test_body_lengths_flags_truncation():
    """서로 다른 글이 정확히 같은 길이에서 끝나면 우연이 아니라 잘린 것이다."""
    raw = [{"c": "가" * 500} for _ in range(50)]
    rep = summarize_body_lengths(raw)
    assert rep["잘림_의심"] is True
    assert rep["최빈길이"] == 500 and rep["최빈길이_건수"] == 50


def test_body_lengths_does_not_flag_natural_variation():
    raw = [{"c": "가" * n} for n in range(10, 400, 7)]
    assert summarize_body_lengths(raw)["잘림_의심"] is False


def test_body_lengths_ignores_short_duplicates_at_the_top():
    """긴 글이 따로 있으면 짧은 글이 겹치는 건 잘림이 아니다."""
    raw = [{"c": "짧음"} for _ in range(20)] + [{"c": "가" * 5000}]
    assert summarize_body_lengths(raw)["잘림_의심"] is False


def test_body_lengths_empty_input():
    assert summarize_body_lengths([])["건수"] == 0


def test_summarize_raw_fields_marks_unused_keys():
    """쓰지 않는 키가 이미지 id 같은 걸 담고 있는지 눈에 띄어야 한다."""
    raw = [{"id": "p1", "c": "본문", "imgs": "111,222", "unknown": 7}]
    by_field = {r["필드"]: r for r in summarize_raw_fields(raw)}
    assert by_field["c"]["사용중"] == "예"
    assert by_field["imgs"]["사용중"] == ""
    assert "미사용" in by_field["imgs"]["비고"]
    assert by_field["imgs"]["예시"] == "111,222"


def test_summarize_raw_fields_truncates_long_samples():
    """예시에 본문이 통째로 들어가면 시트가 감당하지 못한다."""
    sample = [r for r in summarize_raw_fields([{"c": "가" * 5000}])
              if r["필드"] == "c"][0]["예시"]
    assert len(sample) < 200 and sample.endswith("…")


def test_summarize_raw_fields_counts_occurrences():
    raw = [{"id": "1", "imgs": "a"}, {"id": "2"}, {"id": "3", "imgs": "b"}]
    by_field = {r["필드"]: r for r in summarize_raw_fields(raw)}
    assert by_field["id"]["건수"] == 3
    assert by_field["imgs"]["건수"] == 2


def test_summarize_raw_fields_skips_empty_values_for_sample():
    """빈 값이 먼저 와도 실제 값이 예시로 잡혀야 쓸모가 있다."""
    raw = [{"imgs": ""}, {"imgs": None}, {"imgs": "111"}]
    got = [r for r in summarize_raw_fields(raw) if r["필드"] == "imgs"][0]
    assert got["예시"] == "111"


def test_field_report_tab_leads_with_body_length():
    c = FakeClient()
    store = RawStore(c, "F")
    store.ensure()
    store.save_field_report({
        "body": {"건수": 50, "최소": 500, "중앙": 500, "최대": 500,
                 "최빈길이": 500, "최빈길이_건수": 50, "잘림_의심": True},
        "fields": [{"필드": "imgs", "사용중": "", "건수": 12,
                    "예시": "111", "비고": "미사용 — 쓸 만한지 확인"}],
    })
    rows = c.tabs[TAB_FIELDS]
    assert rows[0] == FIELD_COLS
    assert rows[1][0] == "(본문 길이)"          # 가장 먼저 눈에 들어와야 한다
    assert "잘림 의심" in rows[1][4]
    assert rows[2][0] == "imgs"


def test_field_report_replaces_previous_run():
    """진단은 최신 수집 기준이어야 한다 — 옛 결과가 섞이면 오판한다."""
    c = FakeClient()
    store = RawStore(c, "F")
    store.save_field_report({"fields": [{"필드": "옛것", "사용중": "", "건수": 1,
                                         "예시": "", "비고": ""}]})
    store.save_field_report({"fields": [{"필드": "새것", "사용중": "", "건수": 1,
                                         "예시": "", "비고": ""}]})
    fields = [r[0] for r in c.tabs[TAB_FIELDS][1:]]
    assert fields == ["새것"]
