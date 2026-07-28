"""Drive 파일 조작 테스트 — 네트워크 무관 (Drive API 모킹).

셀 단위 읽기·쓰기(`SheetsClient`)와 `find_or_create`는 `tests/test_sheets_client.py`.
여기서는 결과 엑셀을 시트로 변환 업로드하는 경로와 자격증명 파싱만 다룬다.
"""

import json

import pytest

from core.gsheets import (
    _explain,
    _reason_of,
    MIME_SHEET,
    MIME_XLSX,
    GoogleSheetsStore,
    GSheetsError,
    default_title,
    parse_credentials,
    sheet_url,
)

SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


# ═══════════════════════════════════════════════════════════════
# 순수 함수
# ═══════════════════════════════════════════════════════════════

def test_sheet_url():
    assert sheet_url(SHEET_ID).endswith(f"/d/{SHEET_ID}/edit")


def test_default_title():
    assert default_title("202509-202603") == "다감노_202509-202603_분석"


def test_parse_credentials_accepts_json_string_and_dict():
    """secrets에는 JSON 문자열로도, TOML 테이블로도 넣을 수 있다."""
    info = {"client_email": "a@b.iam.gserviceaccount.com", "private_key": "-----KEY-----"}
    assert parse_credentials(info) == info
    assert parse_credentials(json.dumps(info)) == info


def test_parse_credentials_reports_missing_fields():
    with pytest.raises(GSheetsError, match="private_key"):
        parse_credentials({"client_email": "a@b.com"})


def test_parse_credentials_rejects_non_json():
    with pytest.raises(GSheetsError):
        parse_credentials("not json at all")


# ═══════════════════════════════════════════════════════════════
# Drive 호출 (모킹)
# ═══════════════════════════════════════════════════════════════

class FakeRequest:
    def __init__(self, result, error=None):
        self._result, self._error = result, error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


class FakeFiles:
    def __init__(self, parent):
        self.parent = parent

    def create(self, **kw):
        self.parent.create_kwargs = kw
        return FakeRequest({"id": SHEET_ID}, self.parent.create_error)


class FakePermissions:
    def __init__(self, parent):
        self.parent = parent

    def create(self, **kw):
        self.parent.perm_kwargs = kw
        return FakeRequest({"id": "perm1"}, self.parent.perm_error)


class FakeDrive:
    """Drive v3 서비스의 최소 대역 — 호출 인자를 기록해 검증한다."""

    def __init__(self, create_error=None, perm_error=None):
        self.create_error, self.perm_error = create_error, perm_error
        self.create_kwargs = self.perm_kwargs = None

    def files(self):
        return FakeFiles(self)

    def permissions(self):
        return FakePermissions(self)


class FakeHttpError(Exception):
    """googleapiclient.errors.HttpError 흉내 — `.resp.status`만 있으면 된다."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("R", (), {"status": status})()


def _store(drive, folder_id=None):
    return GoogleSheetsStore({}, folder_id=folder_id, service=drive)


def test_upload_requests_sheet_conversion():
    """mimeType을 google-apps.spreadsheet로 줘야 Drive가 xlsx를 시트로 변환한다."""
    drive = FakeDrive()
    file_id, url = _store(drive).upload(b"xlsx", "제목")

    assert file_id == SHEET_ID
    assert url == sheet_url(SHEET_ID)
    body = drive.create_kwargs["body"]
    assert body["mimeType"] == MIME_SHEET
    assert body["name"] == "제목"
    assert "parents" not in body


def test_upload_sends_xlsx_media_type():
    drive = FakeDrive()
    _store(drive).upload(b"xlsx", "제목")
    assert drive.create_kwargs["media_body"].mimetype() == MIME_XLSX


def test_upload_places_file_in_configured_folder():
    drive = FakeDrive()
    _store(drive, folder_id="FOLDER123").upload(b"xlsx", "제목")
    assert drive.create_kwargs["body"]["parents"] == ["FOLDER123"]


def test_share_anyone_reader():
    drive = FakeDrive()
    _store(drive).share_anyone_reader(SHEET_ID)
    assert drive.perm_kwargs["body"] == {"type": "anyone", "role": "reader"}


@pytest.mark.parametrize("status,hint", [(401, "권한"), (403, "권한"),
                                          (404, "찾을 수 없"), (429, "너무 잦")])
def test_api_errors_become_actionable_messages(status, hint):
    """raw HttpError 대신 무엇을 고쳐야 하는지 말해 주는 메시지로 바꾼다."""
    drive = FakeDrive(create_error=FakeHttpError(status))
    with pytest.raises(GSheetsError, match=hint):
        _store(drive).upload(b"xlsx", "제목")


# ═══════════════════════════════════════════════════════════════
# 403의 원인 구분 — 조치가 완전히 다르다
# ═══════════════════════════════════════════════════════════════

def _api_error(status, reason=None, message="boom"):
    """googleapiclient.errors.HttpError 흉내 — resp.status + JSON content."""
    err = FakeHttpError(status)
    body = {"error": {"code": status, "message": message}}
    if reason:
        body["error"]["errors"] = [{"reason": reason, "message": message}]
    err.content = json.dumps(body).encode()
    return err


def test_reason_of_extracts_google_reason():
    assert _reason_of(_api_error(403, "storageQuotaExceeded")) == "storageQuotaExceeded"


@pytest.mark.parametrize("exc", [
    FakeHttpError(403),                                  # content 없음
    _api_error(403),                                     # errors 배열 없음
    type("E", (Exception,), {"content": b"not json"})(),  # 본문이 JSON이 아님
    type("E", (Exception,), {"content": b'{"error":{}}'})(),
    Exception("plain"),
])
def test_reason_of_never_raises(exc):
    """에러를 설명하다가 다시 죽으면 안 된다."""
    assert _reason_of(exc) == ""


def test_storage_quota_explains_ownership_not_permission():
    """서비스 계정은 파일을 '소유'할 수 없다 — 공유 설정으로는 해결되지 않는다."""
    msg = _explain(_api_error(403, "storageQuotaExceeded"), "'다감노_raw' 생성")
    assert "소유할 수 없" in msg
    assert "직접 만들어" in msg


def test_insufficient_permission_explains_sharing():
    msg = _explain(_api_error(403, "insufficientFilePermissions"), "'다감노_raw' 생성")
    assert "편집자" in msg
    assert "소유할 수 없" not in msg          # 다른 원인과 섞이면 안 된다


def test_two_403_causes_give_different_advice():
    quota = _explain(_api_error(403, "storageQuotaExceeded"), "생성")
    perm = _explain(_api_error(403, "insufficientFilePermissions"), "생성")
    assert quota != perm


def test_api_disabled_names_both_apis():
    msg = _explain(_api_error(403, "accessNotConfigured"), "생성")
    assert "Drive API" in msg and "Sheets API" in msg


def test_unknown_reason_still_includes_original_message():
    """번역하다 원인을 삼키면 로그를 뒤져야만 알 수 있게 된다."""
    msg = _explain(_api_error(403, "somethingNew", message="구체적 원인"), "생성")
    assert "구체적 원인" in msg


def test_404_includes_original_message():
    assert "구체적 원인" in _explain(
        _api_error(404, message="구체적 원인"), "생성")


# ═══════════════════════════════════════════════════════════════
# find_or_create — 생성 실패는 정상 경로다
# ═══════════════════════════════════════════════════════════════

class FailingCreateDrive(FakeDrive):
    def __init__(self, error):
        super().__init__(create_error=error)
        self.found = []

    def files(self):
        class F(FakeFiles):
            def list(self, **kw):
                return FakeRequest({"files": []})
        return F(self)


def test_create_failure_tells_user_the_exact_file_name_to_make():
    """개인 구글 계정에서는 사람이 시트를 미리 만드는 게 정석 — 그 방법을 안내해야."""
    drive = FailingCreateDrive(_api_error(403, "storageQuotaExceeded"))
    with pytest.raises(GSheetsError) as ei:
        GoogleSheetsStore({}, folder_id="FOLDER1", service=drive).find_or_create("다감노_raw")
    msg = str(ei.value)
    assert "다감노_raw" in msg          # 만들어야 할 이름
    assert "FOLDER1" in msg             # 만들 위치
    assert "소유할 수 없" in msg        # 왜 실패했는지


def test_existing_file_never_attempts_create():
    drive = FakeDrive()
    drive.found = [{"id": SHEET_ID}]

    class F(FakeFiles):
        def list(self, **kw):
            return FakeRequest({"files": [{"id": SHEET_ID}]})

        def create(self, **kw):
            raise AssertionError("이미 있는데 생성을 시도하면 안 된다")

    drive.files = lambda: F(drive)
    assert GoogleSheetsStore({}, service=drive).find_or_create("다감노_raw") == (SHEET_ID, False)
