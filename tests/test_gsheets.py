"""Drive 파일 조작 테스트 — 네트워크 무관 (Drive API 모킹).

셀 단위 읽기·쓰기(`SheetsClient`)와 `find_or_create`는 `tests/test_sheets_client.py`.
여기서는 결과 엑셀을 시트로 변환 업로드하는 경로와 자격증명 파싱만 다룬다.
"""

import json

import pytest

from core.gsheets import (
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
