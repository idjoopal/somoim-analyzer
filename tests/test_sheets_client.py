"""SheetsClient · find_or_create 테스트 — 네트워크 무관 (구글 API 전부 모킹).

`tests/test_gsheets.py`의 FakeDrive 패턴을 Sheets values API로 확장한 것.
"""

import pytest

from core.gsheets import (
    MAX_WRITE_BYTES,
    MIME_SHEET,
    GoogleSheetsStore,
    GSheetsError,
    SheetsClient,
    a1_range,
    chunk_rows,
)

FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


# ═══════════════════════════════════════════════════════════════
# 순수 함수
# ═══════════════════════════════════════════════════════════════

def test_a1_range_quotes_tab_name():
    assert a1_range("게시글") == "'게시글'!A1"
    assert a1_range("보정", 5) == "'보정'!A5"


def test_a1_range_escapes_single_quote():
    """탭 이름에 작은따옴표가 있으면 두 번 써서 이스케이프해야 범위가 깨지지 않는다."""
    assert a1_range("it's") == "'it''s'!A1"


def test_chunk_rows_splits_by_bytes_not_row_count():
    """본문이 긴 행은 적게, 짧은 행은 많이 — 바이트 기준으로 나뉘어야 한다."""
    fat = [["x" * 500_000] for _ in range(30)]
    thin = [["x"] for _ in range(30)]
    assert len(chunk_rows(fat)) > len(chunk_rows(thin))
    assert len(chunk_rows(thin)) == 1


def test_chunk_rows_preserves_all_rows_in_order():
    rows = [[f"r{i}", "x" * 1000] for i in range(500)]
    chunks = chunk_rows(rows, max_bytes=50_000)
    assert [r for c in chunks for r in c] == rows


def test_chunk_rows_oversized_single_row_gets_own_chunk():
    """한 행이 홀로 한도를 넘어도 버리거나 자르지 않는다."""
    huge = ["y" * (MAX_WRITE_BYTES + 1000)]
    chunks = chunk_rows([["small"], huge, ["small2"]])
    flat = [r for c in chunks for r in c]
    assert huge in flat and len(flat) == 3


def test_chunk_rows_empty():
    assert chunk_rows([]) == []


# ═══════════════════════════════════════════════════════════════
# 가짜 Sheets 서비스
# ═══════════════════════════════════════════════════════════════

class FakeReq:
    def __init__(self, result, error=None):
        self._result, self._error = result, error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


class FakeHttpError(Exception):
    """googleapiclient.errors.HttpError 흉내 — `.resp.status`만 있으면 된다."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("R", (), {"status": status})()


class FakeValues:
    def __init__(self, parent):
        self.p = parent

    def get(self, spreadsheetId=None, range=None):  # noqa: A002
        if self.p.read_error:
            return FakeReq(None, self.p.read_error)
        tab = range.split("'")[1].replace("''", "'")
        return FakeReq({"values": self.p.tabs.get(tab, [])})

    def update(self, spreadsheetId=None, range=None, valueInputOption=None, body=None):  # noqa: A002
        self.p.updates.append({"range": range, "rows": body["values"],
                               "valueInputOption": valueInputOption})
        tab = range.split("'")[1].replace("''", "'")
        self.p.tabs.setdefault(tab, []).extend(body["values"])
        return FakeReq({})

    def append(self, spreadsheetId=None, range=None, valueInputOption=None,  # noqa: A002
               insertDataOption=None, body=None):
        self.p.appends.append({"range": range, "rows": body["values"]})
        tab = range.split("'")[1].replace("''", "'")
        self.p.tabs.setdefault(tab, []).extend(body["values"])
        return FakeReq({})

    def clear(self, spreadsheetId=None, range=None, body=None):  # noqa: A002
        if self.p.clear_error:
            return FakeReq(None, self.p.clear_error)
        tab = range.split("'")[1].replace("''", "'")
        self.p.cleared.append(tab)
        self.p.tabs[tab] = []
        return FakeReq({})


class FakeSpreadsheets:
    def __init__(self, parent):
        self.p = parent

    def values(self):
        return FakeValues(self.p)

    def get(self, spreadsheetId=None, fields=None):
        return FakeReq({"sheets": [{"properties": {"title": t}} for t in self.p.tabs]})

    def batchUpdate(self, spreadsheetId=None, body=None):  # noqa: N802
        for req in body["requests"]:
            self.p.tabs.setdefault(req["addSheet"]["properties"]["title"], [])
        self.p.batch_updates.append(body)
        return FakeReq({})


class FakeSheetsService:
    def __init__(self, tabs=None, read_error=None, clear_error=None):
        self.tabs = dict(tabs or {})
        self.read_error, self.clear_error = read_error, clear_error
        self.updates, self.appends, self.cleared, self.batch_updates = [], [], [], []

    def spreadsheets(self):
        return FakeSpreadsheets(self)


def client(**kw):
    return SheetsClient({}, service=FakeSheetsService(**kw))


# ═══════════════════════════════════════════════════════════════
# 탭
# ═══════════════════════════════════════════════════════════════

def test_tab_names():
    assert client(tabs={"게시글": [], "사진": []}).tab_names(FILE_ID) == ["게시글", "사진"]


def test_ensure_tabs_creates_only_missing():
    svc = FakeSheetsService(tabs={"게시글": [["a"]]})
    made = SheetsClient({}, service=svc).ensure_tabs(FILE_ID, ["게시글", "사진", "_수집이력"])
    assert made == ["사진", "_수집이력"]
    assert svc.tabs["게시글"] == [["a"]]      # 기존 탭 내용 보존


def test_ensure_tabs_noop_when_all_present():
    svc = FakeSheetsService(tabs={"게시글": [], "사진": []})
    assert SheetsClient({}, service=svc).ensure_tabs(FILE_ID, ["게시글"]) == []
    assert svc.batch_updates == []             # 요청 자체를 보내지 않아야 한다


# ═══════════════════════════════════════════════════════════════
# 읽기
# ═══════════════════════════════════════════════════════════════

def test_read_returns_rows():
    c = client(tabs={"게시글": [["id", "title"], ["p1", "글"]]})
    assert c.read(FILE_ID, "게시글") == [["id", "title"], ["p1", "글"]]


def test_read_missing_tab_returns_empty_not_error():
    """없는 탭은 400이 오는데, 빈 값으로 다뤄야 첫 실행이 매끄럽다."""
    assert client(read_error=FakeHttpError(400)).read(FILE_ID, "없는탭") == []


def test_read_real_error_is_raised():
    with pytest.raises(GSheetsError, match="권한"):
        client(read_error=FakeHttpError(403)).read(FILE_ID, "게시글")


# ═══════════════════════════════════════════════════════════════
# 쓰기
# ═══════════════════════════════════════════════════════════════

def test_write_clears_before_updating():
    """행이 줄어드는 경우까지 반영하려면 먼저 비워야 한다."""
    svc = FakeSheetsService(tabs={"게시글": [["old"], ["old2"], ["old3"]]})
    SheetsClient({}, service=svc).write(FILE_ID, "게시글", [["new"]])
    assert svc.cleared == ["게시글"]
    assert svc.tabs["게시글"] == [["new"]]


def test_write_uses_raw_input_option():
    """USER_ENTERED면 구글이 '2026-03'을 날짜로 재해석해 값이 변형된다."""
    svc = FakeSheetsService()
    SheetsClient({}, service=svc).write(FILE_ID, "게시글", [["2026-03"]])
    assert svc.updates[0]["valueInputOption"] == "RAW"


def test_write_chunks_large_payload_with_advancing_ranges():
    svc = FakeSheetsService()
    rows = [["x" * 200_000] for _ in range(60)]
    SheetsClient({}, service=svc).write(FILE_ID, "게시글", rows)
    assert len(svc.updates) > 1
    assert svc.updates[0]["range"] == "'게시글'!A1"
    # 두 번째 청크는 첫 청크 다음 행부터 시작해야 덮어쓰지 않는다
    assert svc.updates[1]["range"] == f"'게시글'!A{1 + len(svc.updates[0]['rows'])}"
    assert sum(len(u["rows"]) for u in svc.updates) == 60


def test_write_empty_clears_only():
    svc = FakeSheetsService(tabs={"게시글": [["old"]]})
    SheetsClient({}, service=svc).write(FILE_ID, "게시글", [])
    assert svc.cleared == ["게시글"] and svc.updates == []


def test_append_preserves_existing():
    svc = FakeSheetsService(tabs={"_수집이력": [["헤더"]]})
    SheetsClient({}, service=svc).append(FILE_ID, "_수집이력", [["run1"]])
    assert svc.tabs["_수집이력"] == [["헤더"], ["run1"]]
    assert svc.cleared == []                   # append는 절대 비우지 않는다


def test_append_empty_is_noop():
    svc = FakeSheetsService()
    SheetsClient({}, service=svc).append(FILE_ID, "_수집이력", [])
    assert svc.appends == []


# ═══════════════════════════════════════════════════════════════
# find_or_create (Drive)
# ═══════════════════════════════════════════════════════════════

class FakeFiles:
    def __init__(self, parent):
        self.p = parent

    def list(self, **kw):
        self.p.list_kwargs = kw
        if self.p.list_error:
            return FakeReq(None, self.p.list_error)
        return FakeReq({"files": self.p.found})

    def create(self, **kw):
        self.p.create_kwargs = kw
        return FakeReq({"id": "NEW_FILE_ID"})


class FakeDrive:
    def __init__(self, found=None, list_error=None):
        self.found = found or []
        self.list_error = list_error
        self.list_kwargs = self.create_kwargs = None

    def files(self):
        return FakeFiles(self)


def store(drive, folder_id="FOLDER1"):
    return GoogleSheetsStore({}, folder_id=folder_id, service=drive)


def test_find_or_create_returns_existing():
    drive = FakeDrive(found=[{"id": FILE_ID, "name": "다감노_raw"}])
    assert store(drive).find_or_create("다감노_raw") == (FILE_ID, False)
    assert drive.create_kwargs is None          # 이미 있으면 만들지 않는다


def test_find_or_create_creates_when_absent():
    drive = FakeDrive(found=[])
    assert store(drive).find_or_create("다감노_raw") == ("NEW_FILE_ID", True)
    body = drive.create_kwargs["body"]
    assert body["mimeType"] == MIME_SHEET
    assert body["parents"] == ["FOLDER1"]


def test_find_by_name_scopes_query_to_folder_and_type():
    drive = FakeDrive(found=[])
    store(drive).find_by_name("다감노_보정")
    q = drive.list_kwargs["q"]
    assert "name = '다감노_보정'" in q
    assert MIME_SHEET in q
    assert "'FOLDER1' in parents" in q
    assert "trashed = false" in q


def test_find_by_name_without_folder_omits_parent_clause():
    drive = FakeDrive(found=[])
    store(drive, folder_id=None).find_by_name("다감노_raw")
    assert "in parents" not in drive.list_kwargs["q"]


def test_find_by_name_picks_oldest_when_duplicated():
    """사람이 사본을 만들어 둬도 원본을 계속 써서 데이터가 갈라지지 않게."""
    drive = FakeDrive(found=[{"id": "OLDEST"}, {"id": "COPY"}])
    assert store(drive).find_by_name("다감노_raw") == "OLDEST"
    assert drive.list_kwargs["orderBy"] == "createdTime"


def test_find_by_name_error_is_wrapped():
    with pytest.raises(GSheetsError):
        store(FakeDrive(list_error=FakeHttpError(403))).find_by_name("다감노_raw")
