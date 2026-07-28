"""구글 스프레드시트 입출력 — 분석 결과를 시트로 내보내고 다시 읽어온다.

## 왜 이렇게 만들었나

가시 시트 12~13개를 Sheets API로 다시 그리는 것은 막대한 작업이고 색·데이터바·
차트를 전부 잃는다. 대신 **Google Drive의 xlsx ↔ Sheets 자동 변환**에 얹는다:

    내보내기: build_excel() → xlsx bytes → files.create(
                 mimeType="application/vnd.google-apps.spreadsheet")
    불러오기: 시트 URL → files.export(
                 mimeType="…spreadsheetml.sheet") → load_excel_bundle()

`load_excel_bundle`은 시트 **이름**으로 찾아 값만 읽으므로, 숨김 시트
(`_메타`·`_원본_게시글` 등)가 변환을 왕복해도 그대로 복원된다. 즉 기존 생성/복원
코드를 건드리지 않고 시트 지원이 붙는다.

변환이 날짜 셀의 타입을 바꿔 놓을 수 있는데, 그건 `excel_builder._coerce_dt` /
`_coerce_iso_date`가 흡수한다.

## 설정

`.streamlit/secrets.toml`:

    [gsheets]
    credentials = '''{ ...서비스 계정 JSON... }'''
    # folder_id = "1AbC..."   # 선택 — 지정하면 그 폴더 안에 만든다

미설정이면 앱에서 이 기능이 조용히 숨겨진다.

서비스 계정이 만든 파일은 **서비스 계정 소유**라 사용자 드라이브 목록에는 뜨지
않는다. 그래서 `folder_id`(사용자가 서비스 계정에 편집자로 공유해 둔 폴더)를
쓰거나, 링크 공유를 켜서 URL로 접근하게 한다.
"""

from __future__ import annotations

import io
import json
import re
from typing import Optional

MIME_SHEET = "application/vnd.google-apps.spreadsheet"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_FOLDER = "application/vnd.google-apps.folder"

# drive 스코프 하나로 Drive·Sheets API를 모두 쓸 수 있다(Sheets API가 drive를 허용).
# 앱이 만든 파일만 다루도록 좁히려면 ".../auth/drive.file"로 바꾸면 되지만,
# 그 경우 사람이 손으로 만든 시트는 앱이 볼 수 없다 — README 참고.
SCOPES = ["https://www.googleapis.com/auth/drive"]

# 한 번의 values 쓰기 요청에 담을 최대 바이트(구글 요청 한도보다 넉넉히 아래).
MAX_WRITE_BYTES = 5 * 1024 * 1024

# /spreadsheets/d/<id>/edit, /file/d/<id>/view, ?id=<id> 등 흔한 형태를 모두 받는다.
_ID_IN_PATH = re.compile(r"/d/([A-Za-z0-9_-]{15,})")
_ID_IN_QUERY = re.compile(r"[?&]id=([A-Za-z0-9_-]{15,})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{15,}$")


class GSheetsError(RuntimeError):
    """구글 시트 연동 실패 — 사용자에게 그대로 보여줄 수 있는 메시지를 담는다."""


# ═══════════════════════════════════════════════════════════════
# 순수 함수 (네트워크 무관 — 단독 테스트 가능)
# ═══════════════════════════════════════════════════════════════

def parse_sheet_id(url_or_id: str) -> str:
    """시트 URL 또는 ID 문자열에서 파일 ID를 뽑는다.

    >>> parse_sheet_id("https://docs.google.com/spreadsheets/d/1AbC.../edit#gid=0")
    '1AbC...'

    Raises:
        GSheetsError — 형태를 알아볼 수 없을 때.
    """
    s = (url_or_id or "").strip()
    if not s:
        raise GSheetsError("시트 주소가 비어 있습니다.")
    for pat in (_ID_IN_PATH, _ID_IN_QUERY):
        m = pat.search(s)
        if m:
            return m.group(1)
    if _BARE_ID.match(s):
        return s
    raise GSheetsError(
        "구글 시트 주소를 알아볼 수 없습니다. "
        "`https://docs.google.com/spreadsheets/d/.../edit` 형태의 링크를 붙여넣어 주세요."
    )


def sheet_url(file_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{file_id}/edit"


def parse_credentials(raw) -> dict:
    """secrets에 담긴 서비스 계정 자격증명을 dict로 정규화.

    TOML에서는 JSON 문자열로도, 인라인 테이블로도 넣을 수 있어 둘 다 받는다.
    """
    if isinstance(raw, dict):
        info = dict(raw)
    else:
        try:
            info = json.loads(str(raw))
        except (TypeError, ValueError) as e:
            raise GSheetsError(f"서비스 계정 JSON을 읽을 수 없습니다: {e}") from e
    missing = [k for k in ("client_email", "private_key") if not info.get(k)]
    if missing:
        raise GSheetsError(
            f"서비스 계정 JSON에 필수 항목이 없습니다: {', '.join(missing)}"
        )
    return info


def default_title(period_tag_str: str) -> str:
    return f"다감노_{period_tag_str}_분석"


def a1_range(tab: str, start_row: int = 1) -> str:
    """`'탭이름'!A1` 형태의 A1 표기. 탭 이름의 작은따옴표는 두 번 써서 이스케이프."""
    safe = str(tab).replace("'", "''")
    return f"'{safe}'!A{int(start_row)}"


def chunk_rows(rows: list[list], max_bytes: int = MAX_WRITE_BYTES) -> list[list[list]]:
    """행 목록을 요청 페이로드 크기 기준으로 나눈다.

    행 수가 아니라 **바이트**로 자르는 이유: 게시글 본문이 최대 32,000자라
    1,000행 고정으로 묶으면 한 요청이 수십 MB가 되어 거부된다.
    한 행이 홀로 한도를 넘으면 그 행만 담은 청크로 내보낸다(자르지 않음).
    """
    out: list[list[list]] = []
    cur: list[list] = []
    cur_bytes = 0
    for row in rows:
        size = sum(len(str(c).encode("utf-8")) for c in row) + 16 * len(row)
        if cur and cur_bytes + size > max_bytes:
            out.append(cur)
            cur, cur_bytes = [], 0
        cur.append(row)
        cur_bytes += size
    if cur:
        out.append(cur)
    return out


# ═══════════════════════════════════════════════════════════════
# Drive 클라이언트
# ═══════════════════════════════════════════════════════════════

class GoogleSheetsStore:
    """Drive API 얇은 래퍼. 네트워크 호출은 전부 여기 모아 두고 테스트에서는 모킹한다."""

    def __init__(self, credentials_info: dict, folder_id: Optional[str] = None,
                 service=None):
        """
        Args:
            credentials_info: 서비스 계정 JSON(dict).
            folder_id: 파일을 만들 폴더. 지정하면 그 폴더 안에 생성한다.
            service: 주입용 Drive 서비스 객체 (테스트에서 사용). None이면 실제 생성.
        """
        self.folder_id = folder_id or None
        self._service = service if service is not None else _build_service(credentials_info)

    # ── 내보내기 ────────────────────────────────────────────────
    def upload(self, xlsx: bytes, title: str) -> tuple[str, str]:
        """xlsx bytes를 구글 시트로 변환 업로드. `(file_id, url)` 반환."""
        from googleapiclient.http import MediaIoBaseUpload

        meta: dict = {"name": title, "mimeType": MIME_SHEET}
        if self.folder_id:
            meta["parents"] = [self.folder_id]
        media = MediaIoBaseUpload(io.BytesIO(xlsx), mimetype=MIME_XLSX, resumable=False)
        try:
            created = self._service.files().create(
                body=meta, media_body=media, fields="id",
                supportsAllDrives=True,
            ).execute()
        except Exception as e:  # noqa: BLE001 — API 예외 종류가 다양해 메시지로 변환
            raise GSheetsError(_explain(e, "시트 생성")) from e
        file_id = created["id"]
        return file_id, sheet_url(file_id)

    # ── 불러오기 ────────────────────────────────────────────────
    def download(self, url_or_id: str) -> bytes:
        """구글 시트를 xlsx bytes로 내려받는다 (`load_excel_bundle`에 그대로 넣을 수 있음)."""
        file_id = parse_sheet_id(url_or_id)
        try:
            return self._service.files().export(
                fileId=file_id, mimeType=MIME_XLSX,
            ).execute()
        except Exception as e:  # noqa: BLE001
            raise GSheetsError(_explain(e, "시트 읽기")) from e

    # ── 이름으로 찾기/만들기 ────────────────────────────────────
    def find_by_name(self, title: str) -> Optional[str]:
        """폴더 안에서 이름이 정확히 일치하는 스프레드시트의 id. 없으면 None.

        여러 개가 걸리면 가장 먼저 만들어진 것을 쓴다 — 사람이 사본을 만들어 둔
        경우에도 원본을 계속 쓰게 해서 데이터가 갈라지지 않도록.
        """
        clauses = [
            f"name = '{str(title).replace(chr(39), chr(92) + chr(39))}'",
            f"mimeType = '{MIME_SHEET}'",
            "trashed = false",
        ]
        if self.folder_id:
            clauses.append(f"'{self.folder_id}' in parents")
        try:
            res = self._service.files().list(
                q=" and ".join(clauses),
                fields="files(id,name,createdTime)",
                orderBy="createdTime",
                pageSize=10,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
        except Exception as e:  # noqa: BLE001
            raise GSheetsError(_explain(e, f"'{title}' 찾기")) from e
        files = res.get("files") or []
        return files[0]["id"] if files else None

    def create_spreadsheet(self, title: str) -> str:
        """빈 스프레드시트를 만들고 id를 돌려준다 (업로드 변환이 아니라 신규 생성)."""
        meta: dict = {"name": title, "mimeType": MIME_SHEET}
        if self.folder_id:
            meta["parents"] = [self.folder_id]
        try:
            created = self._service.files().create(
                body=meta, fields="id", supportsAllDrives=True,
            ).execute()
        except Exception as e:  # noqa: BLE001
            raise GSheetsError(_explain(e, f"'{title}' 생성")) from e
        return created["id"]

    def find_or_create(self, title: str) -> tuple[str, bool]:
        """이름으로 찾고 없으면 만든다. `(file_id, 새로_만들었는지)` 반환.

        URL을 매번 붙여넣지 않아도 되도록 **고정된 이름**으로 파일을 관리하는 진입점.
        """
        existing = self.find_by_name(title)
        if existing:
            return existing, False
        return self.create_spreadsheet(title), True

    # ── 공유 ────────────────────────────────────────────────────
    def share_anyone_reader(self, file_id: str) -> None:
        """링크가 있는 사람은 볼 수 있게 한다.

        서비스 계정 소유 파일은 기본적으로 사용자에게 보이지 않으므로,
        `folder_id`를 쓰지 않는 환경에서는 이걸 켜야 링크로 열 수 있다.
        """
        try:
            self._service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
                supportsAllDrives=True,
            ).execute()
        except Exception as e:  # noqa: BLE001
            raise GSheetsError(_explain(e, "링크 공유 설정")) from e


class SheetsClient:
    """Sheets values API 얇은 래퍼 — 탭 단위 읽기/쓰기.

    Drive의 파일 통째 업로드/내보내기(`GoogleSheetsStore`)와 달리, 원장처럼
    일부만 자주 갱신하는 데이터는 셀 단위로 다뤄야 한다.
    """

    def __init__(self, credentials_info: dict, service=None):
        self._service = (service if service is not None
                         else _build_service(credentials_info, api="sheets"))

    # ── 탭 ──────────────────────────────────────────────────────
    def tab_names(self, file_id: str) -> list[str]:
        try:
            meta = self._service.spreadsheets().get(
                spreadsheetId=file_id, fields="sheets.properties.title",
            ).execute()
        except Exception as e:  # noqa: BLE001
            raise GSheetsError(_explain(e, "탭 목록 읽기")) from e
        return [s["properties"]["title"] for s in meta.get("sheets", [])]

    def ensure_tabs(self, file_id: str, tabs: list[str]) -> list[str]:
        """없는 탭만 만든다. 새로 만든 탭 이름 목록 반환 (기존 탭은 건드리지 않음)."""
        have = set(self.tab_names(file_id))
        missing = [t for t in tabs if t not in have]
        if not missing:
            return []
        requests = [{"addSheet": {"properties": {"title": t}}} for t in missing]
        try:
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=file_id, body={"requests": requests},
            ).execute()
        except Exception as e:  # noqa: BLE001
            raise GSheetsError(_explain(e, "탭 생성")) from e
        return missing

    # ── 읽기 ────────────────────────────────────────────────────
    def read(self, file_id: str, tab: str) -> list[list]:
        """탭 전체를 2차원 배열로. 탭이 없으면 빈 리스트.

        구글은 뒤쪽 빈 셀을 생략해서 돌려주므로 행마다 길이가 다를 수 있다 —
        정규화는 호출부(`core.store.rows_to_records`)가 맡는다.
        """
        try:
            res = self._service.spreadsheets().values().get(
                spreadsheetId=file_id, range=a1_range(tab),
            ).execute()
        except Exception as e:  # noqa: BLE001
            if _status_of(e) == 400:      # 존재하지 않는 탭
                return []
            raise GSheetsError(_explain(e, f"'{tab}' 읽기")) from e
        return res.get("values") or []

    # ── 쓰기 ────────────────────────────────────────────────────
    def clear(self, file_id: str, tab: str) -> None:
        try:
            self._service.spreadsheets().values().clear(
                spreadsheetId=file_id, range=a1_range(tab), body={},
            ).execute()
        except Exception as e:  # noqa: BLE001
            if _status_of(e) == 400:
                return
            raise GSheetsError(_explain(e, f"'{tab}' 비우기")) from e

    def write(self, file_id: str, tab: str, rows: list[list]) -> None:
        """탭을 rows로 **완전히 교체**한다 (기존 내용 삭제 후 기록).

        행이 줄어드는 경우까지 반영하려면 먼저 비워야 한다. 페이로드가 크면
        `chunk_rows`로 나눠 여러 번 보낸다.
        """
        self.clear(file_id, tab)
        if not rows:
            return
        start = 1
        for chunk in chunk_rows(rows):
            self._update(file_id, a1_range(tab, start), chunk, f"'{tab}' 쓰기")
            start += len(chunk)

    def append(self, file_id: str, tab: str, rows: list[list]) -> None:
        """탭 끝에 행을 덧붙인다 (기존 내용 보존)."""
        if not rows:
            return
        for chunk in chunk_rows(rows):
            try:
                self._service.spreadsheets().values().append(
                    spreadsheetId=file_id, range=a1_range(tab),
                    valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                    body={"values": chunk},
                ).execute()
            except Exception as e:  # noqa: BLE001
                raise GSheetsError(_explain(e, f"'{tab}' 추가")) from e

    def _update(self, file_id: str, rng: str, rows: list[list], action: str) -> None:
        try:
            self._service.spreadsheets().values().update(
                spreadsheetId=file_id, range=rng,
                valueInputOption="RAW", body={"values": rows},
            ).execute()
        except Exception as e:  # noqa: BLE001
            raise GSheetsError(_explain(e, action)) from e


def _status_of(exc: Exception):
    return getattr(getattr(exc, "resp", None), "status", None)


def _build_service(credentials_info: dict, api: str = "drive"):
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GSheetsError(
            "구글 연동 패키지가 없습니다. `pip install -r requirements.txt`로 "
            "google-api-python-client · google-auth를 설치해 주세요."
        ) from e
    try:
        creds = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
    except Exception as e:  # noqa: BLE001
        raise GSheetsError(f"서비스 계정 인증에 실패했습니다: {e}") from e
    name, version = ("sheets", "v4") if api == "sheets" else ("drive", "v3")
    return build(name, version, credentials=creds, cache_discovery=False)


def _explain(exc: Exception, action: str) -> str:
    """API 예외를 사용자가 조치할 수 있는 한국어 메시지로."""
    status = _status_of(exc)
    if status in (401, 403):
        return (
            f"{action} 권한이 없습니다 (HTTP {status}). 서비스 계정 이메일을 "
            "대상 시트/폴더에 **편집자**로 공유했는지, Drive API가 켜져 있는지 확인하세요."
        )
    if status == 404:
        return f"{action} 실패 — 시트를 찾을 수 없습니다. 주소와 공유 설정을 확인하세요."
    if status == 429:
        return f"{action} 실패 — 요청이 너무 잦습니다. 잠시 후 다시 시도하세요."
    return f"{action} 실패: {exc}"
