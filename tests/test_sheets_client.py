"""SheetsClient · find_or_create 테스트 — 네트워크 무관 (구글 API 전부 모킹).

`tests/test_gsheets.py`의 FakeDrive 패턴을 Sheets values API로 확장한 것.
"""

import json

import pytest

from core.gsheets import (
    MAX_WRITE_BYTES,
    MIME_SHEET,
    GoogleSheetsStore,
    GSheetsError,
    SheetsClient,
    _is_missing_tab,
    a1_range,
    a1_tab,
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
    """googleapiclient.errors.HttpError 흉내 — `.resp.status` + JSON `content`."""

    def __init__(self, status, message="boom"):
        super().__init__(f"HTTP {status}")
        self.resp = type("R", (), {"status": status})()
        self.content = json.dumps(
            {"error": {"code": status, "message": message}}).encode()


def missing_tab_error(tab: str) -> FakeHttpError:
    """구글이 없는 탭에 돌려주는 실제 응답 — 400 + "Unable to parse range"."""
    return FakeHttpError(400, f"Unable to parse range: '{tab}'!A1")


def _tab_of(rng: str) -> str:
    return rng.split("'")[1].replace("''", "'")


def _start_row_of(rng: str):
    """`'탭'!A5` → 5, `'탭'` → None(탭 전체).

    구글은 이 차이를 지킨다. `'탭'!A1`로 읽으면 **A1 한 칸만** 돌려준다.
    가짜가 이걸 뭉개면 "저장은 되는데 못 읽는" 버그가 테스트를 그냥 통과한다.
    """
    _, _, rest = rng.partition("!")
    return int(rest[1:]) if rest.startswith("A") and rest[1:].isdigit() else None


class FakeValues:
    """범위 표기를 실제 API처럼 지킨다 — 탭 전체와 단일 셀을 구분한다."""

    def __init__(self, parent):
        self.p = parent

    def _require(self, tab):
        return None if tab in self.p.tabs else missing_tab_error(tab)

    def get(self, spreadsheetId=None, range=None):  # noqa: A002
        self.p.read_ranges.append(range)
        if self.p.read_error:
            return FakeReq(None, self.p.read_error)
        tab = _tab_of(range)
        if (err := self._require(tab)):
            return FakeReq(None, err)
        rows = self.p.tabs[tab]
        row = _start_row_of(range)
        if row is not None:                    # 단일 셀 — 딱 그 칸만
            cell = rows[row - 1][:1] if row - 1 < len(rows) and rows[row - 1] else []
            return FakeReq({"values": [cell] if cell else []})
        return FakeReq({"values": [list(r) for r in rows]})

    def update(self, spreadsheetId=None, range=None, valueInputOption=None, body=None):  # noqa: A002
        tab = _tab_of(range)
        if (err := self._require(tab)):
            return FakeReq(None, err)
        self.p.updates.append({"range": range, "rows": body["values"],
                               "valueInputOption": valueInputOption})
        self.p.tabs[tab].extend(body["values"])
        return FakeReq({})

    def append(self, spreadsheetId=None, range=None, valueInputOption=None,  # noqa: A002
               insertDataOption=None, body=None):
        tab = _tab_of(range)
        if (err := self._require(tab)):
            return FakeReq(None, err)
        self.p.appends.append({"range": range, "rows": body["values"]})
        self.p.tabs[tab].extend(body["values"])
        return FakeReq({})

    def clear(self, spreadsheetId=None, range=None, body=None):  # noqa: A002
        self.p.clear_ranges.append(range)
        if self.p.clear_error:
            return FakeReq(None, self.p.clear_error)
        tab = _tab_of(range)
        if (err := self._require(tab)):
            return FakeReq(None, err)
        self.p.cleared.append(tab)
        row = _start_row_of(range)
        if row is None:
            self.p.tabs[tab] = []
        elif row - 1 < len(self.p.tabs[tab]):   # 단일 셀 — 그 칸만 지워진다
            self.p.tabs[tab][row - 1] = self.p.tabs[tab][row - 1][1:]
        return FakeReq({})


class FakeSpreadsheets:
    def __init__(self, parent):
        self.p = parent

    def values(self):
        return FakeValues(self.p)

    def get(self, spreadsheetId=None, fields=None):
        return FakeReq({"sheets": [
            {"properties": {"title": t, "sheetId": self.p.sheet_id_of(t)}}
            for t in self.p.tabs]})

    def batchUpdate(self, spreadsheetId=None, body=None):  # noqa: N802
        for req in body["requests"]:
            if "addSheet" in req:
                self.p.tabs.setdefault(req["addSheet"]["properties"]["title"], [])
        self.p.batch_updates.append(body)
        return FakeReq({})


class FakeSheetsService:
    def __init__(self, tabs=None, read_error=None, clear_error=None):
        self.tabs = dict(tabs or {})
        self.read_error, self.clear_error = read_error, clear_error
        self.updates, self.appends, self.cleared, self.batch_updates = [], [], [], []
        self.read_ranges, self.clear_ranges = [], []

    def sheet_id_of(self, tab: str) -> int:
        """탭마다 고유한 숫자 id — 실제 시트처럼 title과 별개의 식별자를 준다."""
        return 100 + list(self.tabs).index(tab)

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
# 서식 — 사람이 채우는 시트를 쓰기 좋게
# ═══════════════════════════════════════════════════════════════

def _reqs(svc) -> list[dict]:
    return [r for b in svc.batch_updates for r in b["requests"]]


def test_sheet_ids_maps_title_to_numeric_id():
    """서식·메모·드롭다운은 title이 아니라 sheetId로 지정한다."""
    svc = FakeSheetsService(tabs={"이름매핑": [], "공지보정": []})
    assert SheetsClient({}, service=svc).sheet_ids(FILE_ID) == {
        "이름매핑": 100, "공지보정": 101}


def test_validation_is_not_strict_so_new_nicknames_can_be_typed():
    """목록에 없는 새 닉네임을 입력조차 못 하게 막으면 보정이 불가능해진다."""
    svc = FakeSheetsService(tabs={"이름매핑": []})
    SheetsClient({}, service=svc).set_validation(
        FILE_ID, "이름매핑", 1, ["원석사진", "__LEFT__", "__NOISE__"])

    rule = _reqs(svc)[0]["setDataValidation"]["rule"]
    assert rule["strict"] is False
    assert rule["showCustomUi"] is True        # 드롭다운 화살표가 보여야 한다
    assert [v["userEnteredValue"] for v in rule["condition"]["values"]] == [
        "원석사진", "__LEFT__", "__NOISE__"]


def test_validation_targets_the_right_column_and_skips_header():
    svc = FakeSheetsService(tabs={"이름매핑": []})
    SheetsClient({}, service=svc).set_validation(FILE_ID, "이름매핑", 1, ["a"])

    rng = _reqs(svc)[0]["setDataValidation"]["range"]
    assert rng["sheetId"] == 100
    assert rng["startRowIndex"] == 1           # 헤더에는 걸지 않는다
    assert (rng["startColumnIndex"], rng["endColumnIndex"]) == (1, 2)


def test_validation_range_covers_rows_added_later():
    """끝 행을 안 적으면 일부 행에만 걸린다 — 실제로 330행부터만 생긴 적이 있다."""
    svc = FakeSheetsService(tabs={"이름매핑": []})
    SheetsClient({}, service=svc).set_validation(FILE_ID, "이름매핑", 1, ["a"])

    rng = _reqs(svc)[0]["setDataValidation"]["range"]
    assert rng["endRowIndex"] >= 10_000


def test_validation_on_unknown_tab_is_noop():
    """탭이 아직 없다고 죽으면 안 된다 — 서식은 부가 기능이다."""
    svc = FakeSheetsService(tabs={"이름매핑": []})
    SheetsClient({}, service=svc).set_validation(FILE_ID, "없는탭", 1, ["a"])
    assert svc.batch_updates == []


def test_validation_with_empty_list_is_noop():
    svc = FakeSheetsService(tabs={"이름매핑": []})
    SheetsClient({}, service=svc).set_validation(FILE_ID, "이름매핑", 1, [])
    assert svc.batch_updates == []


def test_header_notes_attach_to_header_row_without_changing_text():
    """헤더 텍스트는 파싱 키다 — 설명은 메모로만 붙일 수 있다."""
    svc = FakeSheetsService(tabs={"공지보정": []})
    SheetsClient({}, service=svc).set_header_notes(
        FILE_ID, "공지보정", {0: "게시글 id입니다", 3: "YYYY-MM-DD"})

    cells = [r["updateCells"] for r in _reqs(svc)]
    assert [c["fields"] for c in cells] == ["note", "note"]      # 값은 안 건드린다
    assert [c["rows"][0]["values"][0]["note"] for c in cells] == [
        "게시글 id입니다", "YYYY-MM-DD"]
    assert all(c["range"]["endRowIndex"] == 1 for c in cells)    # 1행만


def test_header_notes_empty_sends_nothing():
    svc = FakeSheetsService(tabs={"공지보정": []})
    SheetsClient({}, service=svc).set_header_notes(FILE_ID, "공지보정", {})
    assert svc.batch_updates == []


def test_rename_tab_changes_only_the_title():
    """내용은 그대로 두고 이름만 갈아 끼운다 — 새로 만들면 사람이 채운 값이 끊긴다."""
    svc = FakeSheetsService(tabs={"이름매핑": [["a"]], "공지보정": []})
    assert SheetsClient({}, service=svc).rename_tab(
        FILE_ID, "이름매핑", "후기이름매핑") is True

    req = _reqs(svc)[0]["updateSheetProperties"]
    assert req["properties"] == {"sheetId": 100, "title": "후기이름매핑"}
    assert req["fields"] == "title"            # 다른 속성은 건드리지 않는다


def test_rename_tab_is_a_noop_when_target_exists():
    """둘 다 있으면 새 탭이 진짜다 — 덮어쓰면 안 된다."""
    svc = FakeSheetsService(tabs={"이름매핑": [], "후기이름매핑": []})
    assert SheetsClient({}, service=svc).rename_tab(
        FILE_ID, "이름매핑", "후기이름매핑") is False
    assert svc.batch_updates == []


def test_rename_tab_is_a_noop_when_source_missing():
    """두 번째 실행 — 이미 이관이 끝난 상태."""
    svc = FakeSheetsService(tabs={"후기이름매핑": []})
    assert SheetsClient({}, service=svc).rename_tab(
        FILE_ID, "이름매핑", "후기이름매핑") is False
    assert svc.batch_updates == []


def test_freeze_header_pins_first_row():
    svc = FakeSheetsService(tabs={"이름매핑": [], "공지보정": []})
    SheetsClient({}, service=svc).freeze_header(FILE_ID, "공지보정")

    req = _reqs(svc)[0]["updateSheetProperties"]
    assert req["properties"]["sheetId"] == 101
    assert req["properties"]["gridProperties"]["frozenRowCount"] == 1
    assert req["fields"] == "gridProperties.frozenRowCount"


def test_formatting_reuses_given_sheet_id_without_refetching():
    """탭마다 sheetId를 다시 조회하면 시트 하나 꾸미는 데 요청이 몇 배로 는다."""
    svc = FakeSheetsService(tabs={"이름매핑": []})

    class NoGet(FakeSpreadsheets):
        def get(self, spreadsheetId=None, fields=None):
            raise AssertionError("sheet_id를 줬으면 다시 조회하면 안 된다")

    svc.spreadsheets = lambda: NoGet(svc)
    SheetsClient({}, service=svc).freeze_header(FILE_ID, "이름매핑", sheet_id=7)
    assert _reqs(svc)[0]["updateSheetProperties"]["properties"]["sheetId"] == 7


# ═══════════════════════════════════════════════════════════════
# 읽기
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 범위 표기 — 셀 하나와 탭 전체는 다르다
#
# 읽기·비우기에 `'탭'!A1`을 쓰면 구글은 A1 한 칸만 다룬다. 반면 쓰기는 A1에서
# 배열만큼 펼쳐지므로 멀쩡해 보인다. 그래서 **시트에는 데이터가 다 들어갔는데
# 앱은 하나도 못 읽는** 상태가 만들어졌다.
# ═══════════════════════════════════════════════════════════════

def test_a1_tab_has_no_cell_reference():
    assert a1_tab("게시글") == "'게시글'"
    assert "!" not in a1_tab("게시글")


def test_a1_tab_escapes_single_quote():
    assert a1_tab("i'm") == "'i''m'"


def test_read_asks_for_the_whole_tab_not_one_cell():
    svc = FakeSheetsService(tabs={"게시글": [["id"], ["p1"]]})
    SheetsClient({}, service=svc).read(FILE_ID, "게시글")
    assert svc.read_ranges == ["'게시글'"]


def test_clear_empties_the_whole_tab_not_one_cell():
    """A1만 비우면 새 데이터보다 아래 있던 옛 행이 그대로 남는다."""
    svc = FakeSheetsService(tabs={"게시글": [["a"], ["b"], ["c"]]})
    SheetsClient({}, service=svc).clear(FILE_ID, "게시글")
    assert svc.clear_ranges == ["'게시글'"]
    assert svc.tabs["게시글"] == []


def test_write_then_read_round_trips_every_row():
    """사용자가 겪은 증상 그대로 — 저장은 됐는데 분석이 '데이터 없음'이었다."""
    svc = FakeSheetsService()
    c = SheetsClient({}, service=svc)
    rows = [["id", "title"]] + [[f"p{i}", f"글 {i}"] for i in range(50)]
    c.write(FILE_ID, "게시글", rows)
    assert c.read(FILE_ID, "게시글") == rows


def test_write_shrinking_data_leaves_no_stale_rows():
    svc = FakeSheetsService()
    c = SheetsClient({}, service=svc)
    c.write(FILE_ID, "게시글", [["h"], ["1"], ["2"], ["3"]])
    c.write(FILE_ID, "게시글", [["h"], ["1"]])
    assert c.read(FILE_ID, "게시글") == [["h"], ["1"]]


def test_read_returns_rows():
    c = client(tabs={"게시글": [["id", "title"], ["p1", "글"]]})
    assert c.read(FILE_ID, "게시글") == [["id", "title"], ["p1", "글"]]


def test_read_missing_tab_returns_empty_not_error():
    """없는 탭은 400이 오는데, 빈 값으로 다뤄야 첫 실행이 매끄럽다."""
    assert client(tabs={"게시글": []}).read(FILE_ID, "없는탭") == []


def test_read_does_not_swallow_other_400s():
    """탭 없음이 아닌 400까지 빈 값으로 뭉개면 진짜 오류가 조용히 사라진다."""
    with pytest.raises(GSheetsError):
        client(read_error=FakeHttpError(400, "Invalid value at 'data'")).read(
            FILE_ID, "게시글")


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
# 탭 자가 보장
#
# 탭 생성이 "스토어를 열 때 한 번"에만 있으면, 그 결과가 캐시에 갇힌 사이
# 시트 쪽이 바뀌었을 때 이후 모든 쓰기가 실패한다. 쓰기가 스스로 지켜야 한다.
# ═══════════════════════════════════════════════════════════════

def test_is_missing_tab_only_matches_the_range_parse_400():
    assert _is_missing_tab(missing_tab_error("게시글"))
    assert not _is_missing_tab(FakeHttpError(400, "Invalid value at 'data'"))
    assert not _is_missing_tab(FakeHttpError(403, "Unable to parse range: 'x'!A1"))
    assert not _is_missing_tab(Exception("plain"))       # 본문도 status도 없음


def test_clear_reports_that_the_tab_was_missing():
    """예전에는 여기서 400을 삼켜 진단이 사라지고 다음 호출이 죽었다."""
    svc = FakeSheetsService(tabs={"게시글": [["a"]]})
    c = SheetsClient({}, service=svc)
    assert c.clear(FILE_ID, "게시글") is True
    assert c.clear(FILE_ID, "없는탭") is False


def test_write_creates_the_tab_it_needs():
    """이것이 이번 버그의 핵심 — 탭이 없어도 쓰기가 성사돼야 한다."""
    svc = FakeSheetsService()                       # 탭이 하나도 없는 새 시트
    SheetsClient({}, service=svc).write(FILE_ID, "게시글", [["id"], ["p1"]])

    added = [r["addSheet"]["properties"]["title"]
             for b in svc.batch_updates for r in b["requests"] if "addSheet" in r]
    assert "게시글" in added
    assert svc.tabs["게시글"] == [["id"], ["p1"]]


def test_append_creates_the_tab_it_needs():
    svc = FakeSheetsService()
    SheetsClient({}, service=svc).append(FILE_ID, "_수집이력", [["2026-07"]])
    assert svc.tabs["_수집이력"] == [["2026-07"]]


def test_retry_happens_only_once():
    """탭을 만들었는데도 같은 오류면 그대로 올린다 — 무한 재시도는 안 된다."""
    svc = FakeSheetsService(tabs={"게시글": []})

    class NeverWorks(FakeValues):
        def append(self, **kw):
            svc.appends.append(kw)
            return FakeReq(None, missing_tab_error("게시글"))

    class S(FakeSpreadsheets):
        def values(self):
            return NeverWorks(svc)

    svc.spreadsheets = lambda: S(svc)
    with pytest.raises(GSheetsError):
        SheetsClient({}, service=svc).append(FILE_ID, "게시글", [["a"]])
    assert len(svc.appends) == 2                    # 최초 1 + 재시도 1


def test_missing_tab_message_is_in_korean_and_actionable():
    """구글 원문 "Unable to parse range"는 범위 문법 오류처럼 읽힌다."""
    svc = FakeSheetsService(tabs={"게시글": []})

    class NeverWorks(FakeValues):
        def append(self, **kw):
            return FakeReq(None, missing_tab_error("게시글"))

    class S(FakeSpreadsheets):
        def values(self):
            return NeverWorks(svc)

    svc.spreadsheets = lambda: S(svc)
    with pytest.raises(GSheetsError, match="탭이 시트에 없습니다") as ei:
        SheetsClient({}, service=svc).append(FILE_ID, "게시글", [["a"]])
    assert "Unable to parse range" in str(ei.value)   # 원문도 남긴다


# ═══════════════════════════════════════════════════════════════
# find_or_create (Drive)
# ═══════════════════════════════════════════════════════════════

class FakeFiles:
    def __init__(self, parent):
        self.p = parent

    def list(self, **kw):
        self.p.list_calls.append(kw)
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
        self.list_calls, self.create_kwargs = [], None

    @property
    def list_kwargs(self):
        """이름으로 찾는 첫 질의 — 못 찾으면 정규화 폴백이 한 번 더 호출한다."""
        return self.list_calls[0] if self.list_calls else None

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


# ═══════════════════════════════════════════════════════════════
# 이름이 미묘하게 다를 때 — "만들었는데 못 찾는다"의 대부분
# ═══════════════════════════════════════════════════════════════

class NameQueryDrive(FakeDrive):
    """Drive처럼 `name = '…'` 절을 **정확히** 대조한다 (한 글자만 달라도 못 찾음)."""

    def __init__(self, names, create_error=None):
        super().__init__()
        self.names = dict(names)                    # 이름 → id
        self.create_error = create_error

    def files(self):
        outer = self

        class F(FakeFiles):
            def list(self, **kw):
                outer.list_calls.append(kw)
                q = kw["q"]
                if "name = " in q:
                    want = q.split("name = '")[1].split("'")[0]
                    hit = outer.names.get(want)
                    return FakeReq({"files": [{"id": hit, "name": want}] if hit else []})
                return FakeReq({"files": [{"id": i, "name": n}
                                          for n, i in outer.names.items()]})

            def create(self, **kw):
                return FakeReq({"id": "NEW_FILE_ID"}, outer.create_error)
        return F(self)


def test_mac_made_name_is_found_despite_unicode_decomposition():
    """맥에서 만든 이름은 자소가 분리(NFD)돼 저장돼 NFC 질의에 안 걸린다."""
    import unicodedata
    nfd = unicodedata.normalize("NFD", "다감노_raw")
    assert nfd != "다감노_raw"                        # 전제 확인
    drive = NameQueryDrive({nfd: "FILE_NFD"})
    assert store(drive).find_by_name("다감노_raw") == "FILE_NFD"


def test_trailing_space_in_the_sheet_name_is_absorbed():
    """이름 끝의 공백은 눈에 보이지도 않는다 — 그것 때문에 403을 내면 안 된다."""
    drive = NameQueryDrive({"다감노_raw ": "FILE_SPACE"})
    assert store(drive).find_by_name("다감노_raw") == "FILE_SPACE"


def test_genuinely_different_name_shows_what_the_folder_has():
    """비슷한 이름은 자동으로 못 고른다 — 대신 뭐가 있는지 보여 준다."""
    drive = NameQueryDrive({"다감노_RAW_백업": "OTHER"}, create_error=_api_403_quota())
    with pytest.raises(GSheetsError) as ei:
        store(drive).find_or_create("다감노_raw")
    assert "다감노_RAW_백업" in str(ei.value)         # 폴더에 실제로 있는 이름


def test_create_failure_lists_what_the_folder_actually_has():
    drive = FailingCreateDrive(_api_403_quota())
    drive.found = []
    with pytest.raises(GSheetsError) as ei:
        GoogleSheetsStore({}, folder_id="FOLDER1",
                          service=drive).find_or_create("다감노_raw")
    assert "스프레드시트를 하나도 못 찾았습니다" in str(ei.value)


def _api_403_quota():
    err = FakeHttpError(403, "quota")
    err.content = json.dumps({"error": {
        "code": 403, "message": "quota",
        "errors": [{"reason": "storageQuotaExceeded", "message": "quota"}]}}).encode()
    return err


class FailingCreateDrive(FakeDrive):
    def __init__(self, error):
        super().__init__(found=[])
        self.error = error

    def files(self):
        outer = self

        class F(FakeFiles):
            def create(self, **kw):
                return FakeReq(None, outer.error)
        return F(self)
