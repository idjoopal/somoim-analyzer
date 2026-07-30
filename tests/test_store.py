"""구글 시트 저장 계층 테스트 — 네트워크 무관 (SheetsClient를 가짜로 주입).

가장 중요한 것은 **불변식**이다: 수집이 보정 시트의 기존 값을 건드리지 않고,
기간이 달라져도 보정이 계속 적용되어야 한다. 그게 이 설계 전체의 존재 이유다.
"""

from datetime import date, datetime

import pytest

from core.collector import (
    ALL_CATS, LEFT_MEMBER, NOT_A_NAME, OUTING_CATS,
    body_cut_length, summarize_body_lengths, summarize_raw_fields,
)
from core.store import (
    CORRECTION_TITLE,
    RAW_TITLE,
    open_stores,
    ATTENDEE_FIX_COLS,
    MEMBER_NAME_COLS,
    NAME_MAP_COLS,
    PHOTO_FIX_COLS,
    POST_FIX_COLS,
    POST_KEYS,
    FIELD_COLS,
    TAB_ATTENDEE_FIX,
    TAB_BANNED,
    TAB_FIELDS,
    TAB_GUIDE,
    TAB_HISTORY,
    TAB_MEMBER_NAMES,
    TAB_NAME_MAP,
    TAB_PHOTO_FIX,
    TAB_POST_FIX,
    CorrectionStore,
    RawStore,
    apply_corrections,
    coerce_bool,
    coerce_dt,
    coerce_iso_date,
    attendee_fix_rows,
    correction_candidates,
    display_name,
    dropdowns,
    member_name_candidates,
    parse_member_names,
    real_by_nickname,
    real_name_resolution,
    filter_excluded,
    missing_rows,
    normalize_record,
    parse_corrections,
    records_to_rows,
    resolution_from_corrections,
    rows_to_records,
    truncated_body_length,
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

    def write_row(self, file_id, tab, row, row_index=1):
        # 실제 `values.update`처럼 **그 행만** 닿는다. 통째로 다시 쓰는
        # `write`와 다르다는 것을 가짜도 지켜야 헤더 넓히기가 검증된다.
        rows = self.tabs.setdefault(tab, [])
        while len(rows) < row_index:
            rows.append([])
        cur = rows[row_index - 1]
        rows[row_index - 1] = list(row) + list(cur[len(row):])


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


# ═══════════════════════════════════════════════════════════════
# 일반공지 — 어느 카테고리에도 안 걸리는 공지를 사람이 지정한다
# ═══════════════════════════════════════════════════════════════

def test_general_notice_is_a_selectable_non_outing_category():
    assert "일반공지" in ALL_CATS
    assert "일반공지" not in OUTING_CATS      # 출사가 아니다


def test_general_notice_is_offered_in_the_dropdown():
    by_col = {(t, col): v for t, col, v in dropdowns()}
    assert "일반공지" in by_col[(TAB_POST_FIX, 2)]


def test_correcting_the_category_also_flips_is_outing():
    """이걸 빼면 `일반공지`로 바꿔도 출사로 계속 집계된다."""
    posts = [post("p1", category="풍경", is_outing=True)]
    apply_corrections(posts, {"posts": {"p1": {"category": "일반공지"}}})
    assert posts[0]["category"] == "일반공지"
    assert posts[0]["is_outing"] is False


def test_correcting_to_an_outing_category_sets_is_outing():
    posts = [post("p1", category="보정", is_outing=False)]
    apply_corrections(posts, {"posts": {"p1": {"category": "인물"}}})
    assert posts[0]["is_outing"] is True


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


class TwoFileClient:
    """파일 id별로 탭을 나눠 갖는 가짜 — raw와 보정이 한 클라이언트를 공유한다.

    한 dict에 몰아 넣으면 두 시트의 탭이 섞여, "raw를 읽어 보정을 채운다"는
    경로를 검증할 수 없다.
    """

    def __init__(self, files=None):
        self.files = {f: {t: [list(r) for r in rows] for t, rows in tabs.items()}
                      for f, tabs in (files or {}).items()}

    def _t(self, file_id):
        return self.files.setdefault(file_id, {})

    def ensure_tabs(self, file_id, tabs):
        t = self._t(file_id)
        made = [x for x in tabs if x not in t]
        for x in made:
            t[x] = []
        return made

    def read(self, file_id, tab):
        return [list(r) for r in self._t(file_id).get(tab, [])]

    def write(self, file_id, tab, rows):
        self._t(file_id)[tab] = [list(r) for r in rows]

    def append(self, file_id, tab, rows):
        self._t(file_id).setdefault(tab, []).extend([list(r) for r in rows])

    def write_row(self, file_id, tab, row, row_index=1):
        rows = self._t(file_id).setdefault(tab, [])
        while len(rows) < row_index:
            rows.append([])
        cur = rows[row_index - 1]
        rows[row_index - 1] = list(row) + list(cur[len(row):])

    def sheet_ids(self, file_id):
        return {t: 100 + i for i, t in enumerate(self._t(file_id))}

    def set_header_notes(self, *a, **kw): pass
    def set_validation(self, *a, **kw): pass
    def freeze_header(self, *a, **kw): pass
    def rename_tab(self, *a, **kw): return False


def _raw_with_members():
    return TwoFileClient({f"ID_{RAW_TITLE}": {
        "멤버": [["mid", "mn", "is_admin", "joined_at", "last_visit", "os", "push"]]
                + [[m["mid"], m["mn"], "", "", "", "", ""] for m in MEMBERS],
        "가입인사매핑": [["실명", "닉네임"], ["정원석", "원석사진"]],
    }})


def test_opening_the_app_seeds_the_roster_without_collecting():
    """멤버 명단은 raw에서 파생된다 — API 재수집을 기다릴 이유가 없다.

    이게 안 되면 사용자는 헤더만 있는 빈 탭을 보게 된다(실제로 그랬다).
    """
    c = _raw_with_members()
    _, fix = open_stores(FakeDriveStore(), c)

    rows = c.read(fix.file_id, TAB_MEMBER_NAMES)
    assert rows[0] == MEMBER_NAME_COLS
    assert [r[1] for r in rows[1:]] == ["나무", "바다", "원석사진"]   # 닉네임 순
    assert {r[1]: r[2] for r in rows[1:]}["원석사진"] == "정원석"      # 가입인사 반영


def test_reopening_does_not_duplicate_or_overwrite_the_roster():
    c = _raw_with_members()
    _, fix = open_stores(FakeDriveStore(), c)
    for row in c.files[fix.file_id][TAB_MEMBER_NAMES][1:]:
        if row[1] == "나무":
            row[2] = "김나무"

    open_stores(FakeDriveStore(), c)                     # 앱을 다시 열었다
    rows = c.read(fix.file_id, TAB_MEMBER_NAMES)
    assert len(rows) == 1 + len(MEMBERS)
    assert {r[1]: r[2] for r in rows[1:]}["나무"] == "김나무"


def test_new_member_shows_up_on_the_next_open():
    c = _raw_with_members()
    fix_id = f"ID_{CORRECTION_TITLE}"
    open_stores(FakeDriveStore(), c)
    c.files[f"ID_{RAW_TITLE}"]["멤버"].append(["m9", "신입", "", "", "", "", ""])

    open_stores(FakeDriveStore(), c)
    assert "신입" in [r[1] for r in c.read(fix_id, TAB_MEMBER_NAMES)[1:]]


def test_empty_raw_leaves_the_roster_alone():
    """수집 전이라 멤버가 없으면 헤더만 — 빈 행을 만들어 두면 안 된다."""
    c = TwoFileClient()
    _, fix = open_stores(FakeDriveStore(), c)
    assert c.read(fix.file_id, TAB_MEMBER_NAMES) == [MEMBER_NAME_COLS]


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
# 멤버 실명 — 보정의 1단계
#
# 핵심은 **키가 mid라는 것**이다. 닉네임을 키로 쓰면 닉네임이 바뀌는 순간
# 사람이 채운 실명이 고아가 된다.
# ═══════════════════════════════════════════════════════════════

def member(mid, mn, **kw):
    return {"mid": mid, "mn": mn, "is_admin": False, "joined_at": None,
            "last_visit": None, "os": "", "push": False, **kw}


MEMBERS = [member("m2", "나무"), member("m1", "원석사진"), member("m3", "바다")]


def test_roster_lists_every_member_sorted_by_nickname():
    """닉네임 순이라 갖고 있던 명단을 열에 통째로 붙여넣을 수 있다."""
    rows = member_name_candidates(MEMBERS)
    assert [r[1] for r in rows] == ["나무", "바다", "원석사진"]
    assert [r[0] for r in rows] == ["m2", "m3", "m1"]


def test_roster_prefills_real_names_found_in_join_greetings():
    """가입인사에서 이미 알아낸 것을 사람이 다시 칠 이유가 없다."""
    rows = member_name_candidates(MEMBERS, {"정원석": "원석사진"})
    by_nick = {r[1]: r[2] for r in rows}
    assert by_nick["원석사진"] == "정원석"
    assert by_nick["나무"] == ""            # 모르는 것은 빈칸으로 남긴다


def test_roster_skips_members_without_an_id():
    assert member_name_candidates([{"mid": "", "mn": "유령"}]) == []


def test_real_names_survive_a_nickname_change():
    """이 설계의 존재 이유 — mid가 키라 닉네임이 바뀌어도 실명이 따라간다."""
    names = {"m1": "정원석"}
    renamed = [member("m1", "새닉네임")]
    assert real_name_resolution(names, renamed) == {"정원석": "새닉네임"}


def test_real_name_equal_to_nickname_is_not_mapped():
    assert real_name_resolution({"m1": "원석사진"}, MEMBERS) == {}


def test_real_name_for_unknown_member_is_dropped():
    """탈퇴해서 멤버 목록에 없으면 매핑할 닉네임이 없다."""
    assert real_name_resolution({"없는사람": "홍길동"}, MEMBERS) == {}


def test_parse_member_names_treats_blank_as_not_filled():
    rows = [MEMBER_NAME_COLS, ["m1", "원석사진", "정원석", ""], ["m2", "나무", "", ""]]
    assert parse_member_names(rows) == {"m1": "정원석"}


def test_real_by_nickname_for_display():
    assert real_by_nickname({"m1": "정원석"}, MEMBERS) == {"원석사진": "정원석"}


@pytest.mark.parametrize("nick,real_map,expected", [
    ("원석사진", {"원석사진": "정원석"}, "원석사진(정원석)"),
    ("나무", {"원석사진": "정원석"}, "나무"),          # 실명을 모르면 그대로
    ("정원석", {"정원석": "정원석"}, "정원석"),        # 같으면 병기하지 않는다
    ("", {}, ""),
])
def test_display_name(nick, real_map, expected):
    assert display_name(nick, real_map) == expected


def test_roster_is_seeded_and_user_values_are_never_touched():
    c = FakeClient()
    store = CorrectionStore(c, "F")
    store.ensure()
    store.seed(correction_candidates([], {}, {"names": {}, "posts": {}, "attendees": {}},
                                     members=MEMBERS))
    # 사람이 실명을 채운다
    for row in c.tabs[TAB_MEMBER_NAMES][1:]:
        if row[0] == "m1":
            row[2] = "정원석"

    store.seed(correction_candidates([], {}, {"names": {}, "posts": {}, "attendees": {}},
                                     members=MEMBERS))          # 재수집
    filled = [r for r in c.tabs[TAB_MEMBER_NAMES][1:] if r[0] == "m1"]
    assert len(filled) == 1 and filled[0][2] == "정원석"          # 중복도, 유실도 없다


def test_real_names_resolve_review_tokens_end_to_end():
    """이 작업의 존재 이유 — 실명을 채우면 후기 본문의 그 이름이 멤버로 인식된다."""
    from core.collector import annotate_attendees

    posts = [post("r1", cat="E", author="나무",
                  body="오늘 정원석 님과 함께했습니다", attendees=[])]
    resolution = real_name_resolution({"m1": "정원석"}, MEMBERS)
    annotate_attendees(posts, {"나무", "원석사진", "바다"}, resolution)

    assert "원석사진" in posts[0]["attendees"]      # 실명이 닉네임으로 풀렸다
    assert "정원석" not in posts[0].get("unresolved_names", [])


def test_resolved_real_names_drop_out_of_the_leftover_tab():
    """풀린 이름이 후기이름매핑에 계속 쌓이면 채워도 줄지 않는다."""
    resolved = correction_candidates(
        [], {}, {"names": {}, "posts": {}, "attendees": {}})
    assert resolved[TAB_NAME_MAP] == []

    leftover = correction_candidates(
        [], {"오타이름": 3}, {"names": {}, "posts": {}, "attendees": {}})
    assert [r[0] for r in leftover[TAB_NAME_MAP]] == ["오타이름"]


def test_relabel_names_does_not_mutate_the_original():
    """원본을 건드리면 매칭 키가 깨진다."""
    from core.store import relabel_names

    posts = [post("p1", attendees=["원석사진", "나무"])]
    shown = relabel_names(posts, {"원석사진": "정원석"})

    assert shown[0]["attendees"] == ["원석사진(정원석)", "나무"]
    assert posts[0]["attendees"] == ["원석사진", "나무"]


def test_relabel_is_a_noop_without_real_names():
    from core.store import relabel_names
    posts = [post("p1", attendees=["원석사진"])]
    assert relabel_names(posts, {}) is posts


def test_relabel_covers_author_and_member_nickname_too():
    """참석자만 바꾸면 작성자·업로더 랭킹은 닉네임 그대로 남는다."""
    from core.store import relabel_names

    real = {"원석사진": "정원석"}
    assert relabel_names([post("p1", author="원석사진")], real)[0]["author"] \
        == "원석사진(정원석)"
    assert relabel_names([{"mid": "m1", "mn": "원석사진"}], real)[0]["mn"] \
        == "원석사진(정원석)"


def test_relabel_keeps_the_raw_nickname_for_identity_checks():
    """동명이인 판정은 표시 이름이 아니라 원래 닉네임으로 해야 한다.

    실명을 붙이면 두 사람이 서로 다른 이름이 되지만, 후기 본문에는 여전히
    닉네임만 적히므로 합쳐 집계되는 문제는 그대로다 — 경고가 사라지면 안 된다.
    """
    from core.store import relabel_names

    shown = relabel_names([{"mid": "m1", "mn": "민수"}], {"민수": "김민수"})[0]
    assert shown["mn"] == "민수(김민수)"
    assert shown["_raw_mn"] == "민수"


def test_relabel_leaves_records_untouched_when_name_is_unchanged():
    """실명이 없거나 닉네임과 같으면 사본을 만들 이유도 없다."""
    from core.store import relabel_names
    rec = {"mid": "m1", "mn": "나무"}
    assert relabel_names([rec], {"원석사진": "정원석"})[0] is rec


def test_load_exposes_member_names():
    c = FakeClient({TAB_MEMBER_NAMES: [MEMBER_NAME_COLS,
                                       ["m1", "원석사진", "정원석", ""]]})
    assert CorrectionStore(c, "F").load()["member_names"] == {"m1": "정원석"}


# ═══════════════════════════════════════════════════════════════
# 이름매핑 → 후기이름매핑 이관
# ═══════════════════════════════════════════════════════════════

class RenamingClient(FakeClient):
    """탭 이름 변경을 지원하는 가짜 — 실제처럼 내용을 그대로 옮긴다."""

    def rename_tab(self, file_id, old, new):
        if old not in self.tabs or new in self.tabs:
            return False
        self.tabs = {(new if k == old else k): v for k, v in self.tabs.items()}
        return True


def test_legacy_tab_is_renamed_keeping_its_rows():
    """상수만 바꾸면 빈 탭이 새로 생기고 사람이 채운 값이 끊긴다."""
    c = RenamingClient({"이름매핑": [NAME_MAP_COLS, ["가나다", "원석사진", 3, "확인함"]]})
    CorrectionStore(c, "F").ensure()

    assert "이름매핑" not in c.tabs
    assert c.tabs[TAB_NAME_MAP][1] == ["가나다", "원석사진", 3, "확인함"]


def test_migration_is_safe_to_run_twice():
    c = RenamingClient({"이름매핑": [NAME_MAP_COLS, ["가나다", "원석사진", 3, ""]]})
    store = CorrectionStore(c, "F")
    store.ensure()
    store.ensure()
    assert len(c.tabs[TAB_NAME_MAP]) == 2


def test_migration_does_nothing_when_new_tab_already_has_data():
    """둘 다 있으면 새 탭이 진짜다 — 옛 탭으로 덮어쓰면 안 된다."""
    c = RenamingClient({
        "이름매핑": [NAME_MAP_COLS, ["옛것", "", 1, ""]],
        TAB_NAME_MAP: [NAME_MAP_COLS, ["새것", "원석사진", 1, ""]],
    })
    CorrectionStore(c, "F").ensure()
    assert c.tabs[TAB_NAME_MAP][1][0] == "새것"


def test_client_without_rename_support_still_works():
    """서식·이관 실패가 보정 자체를 막아서는 안 된다."""
    c = FakeClient()                       # rename_tab 없음
    CorrectionStore(c, "F").ensure()
    assert c.tabs[TAB_NAME_MAP] == [NAME_MAP_COLS]


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
    assert CorrectionStore(c, "F").pending_count() == {
        TAB_MEMBER_NAMES: 0, TAB_NAME_MAP: 1, TAB_POST_FIX: 1, TAB_ATTENDEE_FIX: 0}


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


# ═══════════════════════════════════════════════════════════════
# 사진보정 — 앱이 사람의 판단을 대신 적는 유일한 탭
# ═══════════════════════════════════════════════════════════════

def test_photo_fix_is_never_seeded():
    """댓글 달린 사진이 전부 후보다 — 시딩하면 수천 행이 깔린다."""
    c = FakeClient()
    store = CorrectionStore(c, "F")
    store.ensure()
    store.seed(correction_candidates([], {}, {"names": {}, "posts": {}, "attendees": {}},
                                     members=MEMBERS))
    assert c.tabs[TAB_PHOTO_FIX] == [PHOTO_FIX_COLS]      # 헤더뿐


def test_saving_a_flag_creates_only_that_row():
    c = FakeClient()
    store = CorrectionStore(c, "F")
    store.ensure()
    assert store.save_photo_flags({"ph1": True}, {"ph1": "나무"}) == 1

    rows = c.tabs[TAB_PHOTO_FIX]
    assert rows[0] == PHOTO_FIX_COLS
    assert rows[1][:3] == ["ph1", "나무", "TRUE"]
    assert len(rows) == 2


def test_unmarking_flips_the_existing_row_instead_of_adding_one():
    c = FakeClient()
    store = CorrectionStore(c, "F")
    store.ensure()
    store.save_photo_flags({"ph1": True})
    store.save_photo_flags({"ph1": False})

    rows = c.tabs[TAB_PHOTO_FIX]
    assert len(rows) == 2                                 # 중복 행이 생기면 안 된다
    assert rows[1][2] == "FALSE"


def test_saving_the_same_value_writes_nothing():
    c = FakeClient()
    store = CorrectionStore(c, "F")
    store.ensure()
    store.save_photo_flags({"ph1": True})
    before = [list(r) for r in c.tabs[TAB_PHOTO_FIX]]
    assert store.save_photo_flags({"ph1": True}) == 0
    assert c.tabs[TAB_PHOTO_FIX] == before


def test_saving_one_flag_leaves_the_others_alone():
    """다른 보정과 같은 규칙 — 사용자가 건드린 id의 행만 바꾼다."""
    c = FakeClient()
    store = CorrectionStore(c, "F")
    store.ensure()
    store.save_photo_flags({"ph1": True, "ph2": True})
    store.save_photo_flags({"ph1": False})

    by_id = {r[0]: r[2] for r in c.tabs[TAB_PHOTO_FIX][1:]}
    assert by_id == {"ph1": "FALSE", "ph2": "TRUE"}


def test_load_exposes_photo_flags():
    c = FakeClient({TAB_PHOTO_FIX: [PHOTO_FIX_COLS,
                                    ["ph1", "나무", "TRUE", ""],
                                    ["ph2", "바다", "FALSE", ""]]})
    flags = CorrectionStore(c, "F").load()["photos"]
    assert flags == {"ph1": True, "ph2": False}


# ═══════════════════════════════════════════════════════════════
# 강퇴멤버 — ban=Y는 탈퇴가 아니다
#
# 자발적으로 나간 사람은 멤버 목록에 아예 없다. `탈퇴멤버`라는 이름이
# 뜻을 잘못 말하고 있었다.
# ═══════════════════════════════════════════════════════════════

def test_legacy_banned_tab_is_renamed_keeping_its_rows():
    """상수만 바꾸면 빈 탭이 새로 생기고 쌓인 명단이 옛 탭에 고립된다."""
    c = RenamingClient({"탈퇴멤버": [["닉네임"], ["강퇴당한사람"]]})
    RawStore(c, "F").ensure()

    assert "탈퇴멤버" not in c.tabs
    assert c.tabs[TAB_BANNED] == [["닉네임"], ["강퇴당한사람"]]


def test_banned_migration_is_safe_to_run_twice():
    c = RenamingClient({"탈퇴멤버": [["닉네임"], ["a"]]})
    store = RawStore(c, "F")
    store.ensure()
    store.ensure()
    assert c.tabs[TAB_BANNED] == [["닉네임"], ["a"]]


def test_banned_migration_does_not_clobber_an_existing_new_tab():
    c = RenamingClient({"탈퇴멤버": [["닉네임"], ["옛것"]],
                        TAB_BANNED: [["닉네임"], ["새것"]]})
    RawStore(c, "F").ensure()
    assert c.tabs[TAB_BANNED][1] == ["새것"]


def test_raw_store_works_with_a_client_that_cannot_rename():
    """이관 실패가 수집을 막아서는 안 된다."""
    c = FakeClient()                       # rename_tab 없음
    RawStore(c, "F").ensure()
    assert TAB_BANNED in c.tabs


def test_kicked_member_is_accepted_as_a_left_marker():
    """`__LEFT__`는 탈퇴와 강퇴를 모두 담는다."""
    assert resolution_from_corrections({"names": {"가": "강퇴"}}) == {"가": LEFT_MEMBER}
    assert resolution_from_corrections({"names": {"나": "탈퇴"}}) == {"나": LEFT_MEMBER}


# ═══════════════════════════════════════════════════════════════
# 본문 잘림 — 참석자 보정이 왜 필요한지 알려 주는 근거
# ═══════════════════════════════════════════════════════════════

def test_body_cut_length_finds_the_wall():
    """서로 다른 글이 정확히 같은 길이에서 끝나고 그보다 긴 글이 없으면 잘린 것."""
    assert body_cut_length([12, 40, 120, 120, 120, 87]) == 120


def test_body_cut_length_stays_quiet_without_a_wall():
    """가장 긴 글 하나는 그냥 가장 긴 글이다 — 잘렸다고 말하면 거짓 경고다."""
    assert body_cut_length([12, 40, 87, 120]) is None
    assert body_cut_length([]) is None
    assert body_cut_length([0, 0, 0]) is None       # 빈 본문은 길이가 아니다


def test_a_short_wall_is_not_truncation():
    """짧은 글 셋이 우연히 같은 길이인 쪽이 훨씬 흔하다.

    "닉 다녀왔습니다"만 적힌 후기 셋이 그렇다. 미리보기를 준다면 문장 몇 개는
    주지, 여덟 글자에서 끊지 않는다. 거짓 경고는 진짜 경고를 죽인다.
    """
    assert body_cut_length([4, 4, 8, 8, 8]) is None


def test_body_cut_length_survives_a_pile_of_empty_bodies():
    """최빈값으로 재면 빈 본문이 많을 때 **잘리고 있는데 아니라고 답한다.**

    공지처럼 본문이 없는 글이 잘린 후기보다 많은 것은 흔한 일이다.
    """
    assert body_cut_length([0] * 50 + [120, 120, 120]) == 120


def test_truncated_reviews_become_correction_candidates():
    """이름을 뽑았어도 본문이 잘렸으면 그 명단이 전부라는 근거가 없다."""
    long_body = "가" * 120
    posts = [
        post("r1", cat="E", body=long_body, attendees=["나무"],
             attendees_needs_review=False),
        post("r2", cat="E", body=long_body, attendees=["바다"],
             attendees_needs_review=False),
        post("r3", cat="E", body=long_body, attendees=["하늘"],
             attendees_needs_review=False),
        post("r4", cat="E", body="짧은 후기", attendees=["구름"],
             attendees_needs_review=False),
    ]
    rows = attendee_fix_rows(posts)
    assert [r[0] for r in rows] == ["r1", "r2", "r3"], "온전한 후기는 부르지 않는다"
    assert rows[0][4] == "나무"          # 추출된 참석자를 미리 채운다
    assert rows[0][5] == 120             # 본문길이
    assert rows[0][6] == "⚠️ 잘림 의심"


def test_intact_review_that_lost_its_names_still_becomes_a_candidate():
    """잘림만 보면, 이름을 못 뽑은 짧은 후기를 놓친다."""
    rows = attendee_fix_rows([post("r9", cat="E", body="짧다", attendees=[],
                                   attendees_needs_review=True)])
    assert [r[0] for r in rows] == ["r9"]
    assert rows[0][4] == ""              # 뽑힌 이름이 없다
    assert rows[0][6] == "온전"           # 본문 탓이 아니라는 것을 밝힌다


def test_zero_attendee_review_is_a_candidate_without_any_flag():
    """참석자 0명이면 그것만으로 보정 대상이다 — 아무도 안 간 출사는 없다.

    앱이 쓰는 `annotate_attendees`는 `attendees_needs_review`를 붙이지 않는다.
    그래서 이 조건이 없으면 **본문이 짧으면서 이름이 하나도 없는 후기**가
    영원히 시트에 안 올라온다(실제로 두 건이 새어 나갔다).
    """
    rows = attendee_fix_rows([
        post("r1", cat="E", body="늦은시간까지 참여해주셔서 감사합니다", attendees=[]),
        post("r2", cat="E", body="짧은 후기", attendees=["구름"]),
    ])
    assert [r[0] for r in rows] == ["r1"]
    assert rows[0][6] == "온전", "본문 탓이 아니라 양식을 안 지킨 것이다"


def test_meetup_system_post_is_not_a_candidate():
    """소모임 정모 게시글은 후기가 아니라 참석자가 있을 수 없다.

    올려 두면 사람이 **영원히 못 채우는 행**이 시트에 남아, 남은 건수가
    끝까지 0으로 안 떨어진다.
    """
    rows = attendee_fix_rows([
        post("m1", cat="E", attendees=[],
             body="📌 정모 정보\n📅 4월 2일(목)\n📍 현충원\n💰 1/n"),
        post("m2", cat="E", attendees=[],
             body="이 게시글에서 정모에 대한 이야기를 나눠보세요."),
        post("r1", cat="E", body="후기입니다", attendees=[]),
    ])
    assert [r[0] for r in rows] == ["r1"]


def test_confirmed_reviews_drop_out_of_the_candidates():
    """`참석자`를 채워 확인했으면 다시 부르지 않는다 — 그래야 건수가 줄어든다."""
    long_body = "가" * 120
    posts = [post(f"r{i}", cat="E", body=long_body, attendees=["나무"],
                  attendees_needs_review=False) for i in range(3)]
    rows = attendee_fix_rows(posts, done_att={"r0": ["나무"]})
    assert [r[0] for r in rows] == ["r1", "r2"]


def test_body_cut_is_measured_over_every_post_not_just_reviews():
    """공지도 같은 API에서 오므로 벽을 재는 표본에 함께 들어간다."""
    posts = [post("a", body="가" * 120), post("b", body="가" * 120),
             post("c", cat="E", body="가" * 120)]
    assert truncated_body_length(posts) == 120


def test_widening_the_header_keeps_what_people_typed():
    """이미 쓰던 시트의 옛 헤더를 넓히되, 아래 행은 손대지 않는다."""
    old_head = ["후기 id", "제목", "참석자", "비고"]
    c = FakeClient({TAB_ATTENDEE_FIX: [old_head, ["r1", "후기", "나무, 바다", "확인함"]]})
    CorrectionStore(c, "F").widen_headers()

    rows = c.tabs[TAB_ATTENDEE_FIX]
    assert rows[0] == ATTENDEE_FIX_COLS
    assert rows[1] == ["r1", "후기", "나무, 바다", "확인함"], "사람이 채운 행은 그대로"
    # 넓힌 뒤에도 같은 값이 같은 이름으로 읽혀야 한다.
    assert rows_to_records(rows)[0]["참석자"] == "나무, 바다"


def test_widening_leaves_an_unfamiliar_header_alone():
    """앞 열 이름이 다르면 우리가 아는 시트가 아니다 — 건드리면 뜻이 어긋난다."""
    weird = ["후기 id", "누가 갔나"]
    c = FakeClient({TAB_ATTENDEE_FIX: [weird, ["r1", "나무"]]})
    CorrectionStore(c, "F").widen_headers()
    assert c.tabs[TAB_ATTENDEE_FIX][0] == weird
