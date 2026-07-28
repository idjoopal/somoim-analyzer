"""구글 스프레드시트 입출력 — 분석 결과를 시트로 내보내고 다시 읽어온다.

## 두 가지 쓰임

1. **`SheetsClient`** — 탭 단위 셀 읽기/쓰기. `core.store`의 raw·보정 시트가
   이걸 쓴다. 자주 일부만 갱신하는 데이터라 파일 통째 조작은 맞지 않는다.
2. **`GoogleSheetsStore`** — Drive 파일 조작. 이름으로 찾기/만들기(`find_or_create`)와,
   결과 엑셀을 시트로 변환 업로드(`upload`)에 쓴다. 가시 시트 12~13개를 Sheets
   API로 다시 그리면 색·데이터바·차트를 전부 잃으므로, `build_excel()`이 만든
   xlsx를 Drive가 변환하게 둔다.

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
import unicodedata
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


def a1_tab(tab: str) -> str:
    """탭 **전체**를 가리키는 범위 — `'탭이름'`.

    셀을 붙이면(`'탭이름'!A1`) 구글은 딱 그 한 칸만 다룬다. 읽기·비우기에
    그걸 쓰면 **헤더 한 글자만 읽히고 데이터는 안 보인다.** 쓰기는 A1에서
    2차원 배열만큼 펼쳐지므로 멀쩡해 보여서, 저장은 되는데 못 읽는 상태가 된다.
    """
    return f"'{str(tab).replace(chr(39), chr(39) * 2)}'"


def a1_range(tab: str, start_row: int = 1) -> str:
    """`'탭이름'!A1` — **쓰기 시작 지점**을 가리키는 표기.

    `values.update`는 여기서부터 준 배열만큼 펼쳐 쓴다. 읽기·비우기에는
    쓰지 말 것 (`a1_tab` 참고).
    """
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
        if files:
            return files[0]["id"]
        return self._find_by_normalized_name(title)

    def _find_by_normalized_name(self, title: str) -> Optional[str]:
        """이름 정규화까지 맞춰 한 번 더 찾는다.

        Drive의 `name = '…'`는 바이트 단위 비교라, **맥에서 만든 이름은 자소가
        분리(NFD)돼 저장되어 NFC로 쓴 질의에 걸리지 않는다.** 겉보기에 똑같은
        이름인데 "파일이 없다"가 되는 흔한 함정이라 폴백을 둔다.
        """
        want = unicodedata.normalize("NFC", str(title)).strip().casefold()
        for name, fid in self.list_spreadsheets():
            if unicodedata.normalize("NFC", name).strip().casefold() == want:
                return fid
        return None

    def list_spreadsheets(self) -> list[tuple[str, str]]:
        """폴더 안 스프레드시트의 `(이름, id)`. 실패하면 빈 목록 (진단용이라 죽지 않는다)."""
        clauses = [f"mimeType = '{MIME_SHEET}'", "trashed = false"]
        if self.folder_id:
            clauses.append(f"'{self.folder_id}' in parents")
        try:
            res = self._service.files().list(
                q=" and ".join(clauses), fields="files(id,name)",
                orderBy="createdTime", pageSize=50,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
        except Exception:  # noqa: BLE001
            return []
        return [(f.get("name", ""), f["id"]) for f in (res.get("files") or [])]

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

        생성은 실패할 수 있는 것이 정상이다 — 서비스 계정은 드라이브 저장 용량이
        없어 파일을 소유할 수 없기 때문에, 개인 구글 계정 환경에서는 사람이 시트를
        미리 만들어 두는 쪽이 정석이다. 그래서 실패 시 그 방법을 안내한다.
        """
        existing = self.find_by_name(title)
        if existing:
            return existing, False
        try:
            return self.create_spreadsheet(title), True
        except GSheetsError as e:
            raise GSheetsError(
                f"{e}\n\n"
                f"**해결 방법** — 구글 드라이브에서 폴더"
                f"{f'(`{self.folder_id}`)' if self.folder_id else ''} 안에 "
                f"빈 스프레드시트를 만들고 이름을 정확히 **`{title}`** 로 지정하세요. "
                "앱이 그 파일을 찾아 그대로 사용합니다(탭은 자동으로 만듭니다).\n\n"
                + self._folder_contents_hint()
            ) from e

    def _folder_contents_hint(self) -> str:
        """폴더에 실제로 뭐가 있는지 보여 준다 — 뒤에 붙은 공백 하나까지 눈에 보이게.

        "만들었는데 못 찾는다"의 대부분은 이름 차이다. 목록을 보여 주면
        `다감노_raw ` 같은 것이 즉시 드러난다.
        """
        found = self.list_spreadsheets()
        if not found:
            return ("폴더에서 스프레드시트를 하나도 못 찾았습니다. `folder_id`가 맞는지, "
                    "그 폴더가 서비스 계정에 공유돼 있는지 확인하세요.")
        names = "\n".join(f"  · `{n}`" for n, _ in found[:20])
        return f"참고 — 이 폴더에서 앱이 보고 있는 스프레드시트:\n{names}"

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

    def sheet_ids(self, file_id: str) -> dict[str, int]:
        """탭 이름 → sheetId(숫자). 서식·메모·드롭다운은 전부 sheetId로 지정한다."""
        try:
            meta = self._service.spreadsheets().get(
                spreadsheetId=file_id, fields="sheets.properties(title,sheetId)",
            ).execute()
        except Exception as e:  # noqa: BLE001
            raise GSheetsError(_explain(e, "탭 정보 읽기")) from e
        return {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])}

    # ── 서식 (사람이 쓰는 시트를 쓰기 좋게) ──────────────────────
    def _batch(self, file_id: str, requests: list[dict], action: str) -> None:
        if not requests:
            return
        try:
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=file_id, body={"requests": requests},
            ).execute()
        except Exception as e:  # noqa: BLE001
            raise GSheetsError(_explain(e, action)) from e

    def set_header_notes(self, file_id: str, tab: str, notes: dict[int, str],
                         sheet_id: Optional[int] = None) -> None:
        """헤더 셀에 메모를 단다 (마우스를 올리면 뜸).

        헤더 **텍스트**는 파싱 키라 못 바꾸므로, 설명은 메모로 붙인다.
        """
        sid = sheet_id if sheet_id is not None else self.sheet_ids(file_id).get(tab)
        if sid is None:
            return
        self._batch(file_id, [{
            "updateCells": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "rows": [{"values": [{"note": note}]}],
                "fields": "note",
            }
        } for col, note in sorted(notes.items())], f"'{tab}' 메모 설정")

    def set_validation(self, file_id: str, tab: str, col: int, values: list[str],
                       strict: bool = False, sheet_id: Optional[int] = None) -> None:
        """열에 드롭다운을 건다. 설명을 읽게 하는 것보다 잘못 넣을 수 없게 하는 게 낫다.

        `strict=False`(경고만)가 기본이다 — 목록에 없는 새 닉네임을 입력조차
        못 하게 막으면 곤란하다.
        """
        sid = sheet_id if sheet_id is not None else self.sheet_ids(file_id).get(tab)
        if sid is None or not values:
            return
        self._batch(file_id, [{
            "setDataValidation": {
                "range": {"sheetId": sid, "startRowIndex": 1,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "rule": {
                    "condition": {"type": "ONE_OF_LIST",
                                  "values": [{"userEnteredValue": str(v)} for v in values]},
                    "showCustomUi": True,
                    "strict": bool(strict),
                },
            }
        }], f"'{tab}' 드롭다운 설정")

    def freeze_header(self, file_id: str, tab: str,
                      sheet_id: Optional[int] = None) -> None:
        """헤더 행 고정 — 아래로 스크롤해도 무슨 열인지 보이게."""
        sid = sheet_id if sheet_id is not None else self.sheet_ids(file_id).get(tab)
        if sid is None:
            return
        self._batch(file_id, [{
            "updateSheetProperties": {
                "properties": {"sheetId": sid,
                               "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        }], f"'{tab}' 헤더 고정")

    # ── 읽기 ────────────────────────────────────────────────────
    def read(self, file_id: str, tab: str) -> list[list]:
        """탭 전체를 2차원 배열로. 탭이 없으면 빈 리스트.

        구글은 뒤쪽 빈 셀을 생략해서 돌려주므로 행마다 길이가 다를 수 있다 —
        정규화는 호출부(`core.store.rows_to_records`)가 맡는다.
        """
        try:
            res = self._service.spreadsheets().values().get(
                spreadsheetId=file_id, range=a1_tab(tab),
            ).execute()
        except Exception as e:  # noqa: BLE001
            if _is_missing_tab(e):        # 존재하지 않는 탭 = 빈 탭으로 취급
                return []
            raise GSheetsError(_explain(e, f"'{tab}' 읽기")) from e
        return res.get("values") or []

    # ── 쓰기 ────────────────────────────────────────────────────
    def clear(self, file_id: str, tab: str) -> bool:
        """탭을 비운다. 탭이 **없었으면** False (호출부가 만들 수 있게 알린다).

        예전에는 여기서 400을 조용히 삼켰다. 그러면 "탭이 없다"는 정확한 진단이
        사라지고, 바로 다음 `_update`가 구글 원문 영어로 죽는다.
        """
        try:
            self._service.spreadsheets().values().clear(
                spreadsheetId=file_id, range=a1_tab(tab), body={},
            ).execute()
        except Exception as e:  # noqa: BLE001
            if _is_missing_tab(e):
                return False
            raise GSheetsError(_explain(e, f"'{tab}' 비우기")) from e
        return True

    def write(self, file_id: str, tab: str, rows: list[list]) -> None:
        """탭을 rows로 **완전히 교체**한다 (기존 내용 삭제 후 기록).

        행이 줄어드는 경우까지 반영하려면 먼저 비워야 한다. 페이로드가 크면
        `chunk_rows`로 나눠 여러 번 보낸다.
        """
        if not self.clear(file_id, tab):
            self.ensure_tabs(file_id, [tab])       # 없으면 만들고 계속 간다
        if not rows:
            return
        start = 1
        for chunk in chunk_rows(rows):
            self._write_chunk(file_id, tab, a1_range(tab, start), chunk)
            start += len(chunk)

    def append(self, file_id: str, tab: str, rows: list[list]) -> None:
        """탭 끝에 행을 덧붙인다 (기존 내용 보존)."""
        if not rows:
            return
        for chunk in chunk_rows(rows):
            self._retry_after_ensure(
                file_id, tab, lambda c=chunk: self._append_once(file_id, tab, c),
                f"'{tab}' 추가")

    # ── 탭 자가 보장 ────────────────────────────────────────────
    #
    # 탭 생성이 `open_stores` 한 곳에만 있으면, 그 결과가 캐시에 갇힌 사이
    # 시트 쪽이 바뀌었을 때 이후 모든 쓰기가 실패한다. 전제 조건은 그것을
    # 필요로 하는 쪽이 직접 지키는 편이 낫다.
    def _retry_after_ensure(self, file_id: str, tab: str, call, action: str) -> None:
        """탭이 없어 실패하면 탭을 만들고 **한 번만** 다시 시도한다."""
        try:
            call()
            return
        except Exception as e:  # noqa: BLE001
            if not _is_missing_tab(e):
                raise GSheetsError(_explain(e, action)) from e
        self.ensure_tabs(file_id, [tab])
        try:
            call()
        except Exception as e:  # noqa: BLE001 — 두 번째는 그대로 올린다 (루프 방지)
            raise GSheetsError(_explain(e, action)) from e

    def _write_chunk(self, file_id: str, tab: str, rng: str, rows: list[list]) -> None:
        self._retry_after_ensure(
            file_id, tab, lambda: self._update_once(file_id, rng, rows), f"'{tab}' 쓰기")

    def _update_once(self, file_id: str, rng: str, rows: list[list]) -> None:
        self._service.spreadsheets().values().update(
            spreadsheetId=file_id, range=rng,
            valueInputOption="RAW", body={"values": rows},
        ).execute()

    def _append_once(self, file_id: str, tab: str, rows: list[list]) -> None:
        self._service.spreadsheets().values().append(
            spreadsheetId=file_id, range=a1_tab(tab),
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

    def _update(self, file_id: str, rng: str, rows: list[list], action: str) -> None:
        try:
            self._update_once(file_id, rng, rows)
        except Exception as e:  # noqa: BLE001
            raise GSheetsError(_explain(e, action)) from e


def _status_of(exc: Exception):
    return getattr(getattr(exc, "resp", None), "status", None)


def _error_body(exc: Exception) -> dict:
    """구글 에러 응답 본문을 dict로 (없거나 깨졌으면 빈 dict).

    에러를 설명하다가 다시 죽으면 안 되므로 어떤 입력에도 예외를 내지 않는다.
    """
    content = getattr(exc, "content", None)
    if not content:
        return {}
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        body = json.loads(content)
        return body.get("error") or {} if isinstance(body, dict) else {}
    except (ValueError, AttributeError, TypeError):
        return {}


def _reason_of(exc: Exception) -> str:
    """구글 에러 본문에서 `reason`을 꺼낸다 (없으면 빈 문자열).

    같은 403이라도 `storageQuotaExceeded`(소유권 문제)와
    `insufficientFilePermissions`(공유 문제)는 조치가 완전히 다르다.
    이 값을 버리면 사용자는 무엇을 고쳐야 할지 알 수 없다.
    """
    errors = _error_body(exc).get("errors") or []
    try:
        return str(errors[0].get("reason") or "")
    except (IndexError, AttributeError, TypeError):
        return ""


def _detail_of(exc: Exception) -> str:
    """사람이 읽을 수 있는 원본 설명.

    `str(exc)`에 의존하지 않는다 — 클라이언트가 예외를 어떻게 포매팅하든
    본문의 `message`가 가장 구체적이고, 그게 조치의 실마리가 된다.
    """
    return str(_error_body(exc).get("message") or "") or str(exc)


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


def _is_missing_tab(exc: Exception) -> bool:
    """그 이름의 탭이 없어서 난 오류인가.

    Sheets API는 없는 탭을 400 `Unable to parse range: '게시글'!A1`로 돌려준다.
    "범위 문법이 틀렸다"처럼 읽히지만 실제 뜻은 **탭이 없다**이다.
    """
    return (_status_of(exc) == 400
            and "unable to parse range" in _detail_of(exc).lower())


def _explain(exc: Exception, action: str) -> str:
    """API 예외를 사용자가 조치할 수 있는 한국어 메시지로.

    `reason`별로 조치를 특정하고, 모르는 경우에도 **원본 메시지를 반드시 덧붙인다** —
    번역하다 원인을 삼켜 버리면 로그를 뒤져야만 알 수 있게 된다.
    """
    status, reason = _status_of(exc), _reason_of(exc)
    detail = _detail_of(exc)

    if reason == "storageQuotaExceeded":
        return (
            f"{action} 실패 — **서비스 계정은 구글 드라이브에 파일을 소유할 수 없습니다** "
            "(저장 용량 0). 폴더를 편집자로 공유해도 파일을 '만드는' 것은 안 됩니다.\n\n"
            "→ 공유해 둔 폴더 안에 빈 스프레드시트를 **직접 만들어** 주세요. "
            "그러면 소유자가 사용자가 되고 앱은 편집만 하므로 문제가 사라집니다."
        )
    if reason in ("insufficientFilePermissions", "forbidden"):
        return (
            f"{action} 실패 — 대상에 대한 권한이 없습니다. 서비스 계정 이메일이 "
            "그 폴더/시트에 **편집자**(뷰어 아님)로 공유돼 있는지 확인하세요."
        )
    if reason in ("accessNotConfigured", "SERVICE_DISABLED"):
        return (f"{action} 실패 — API가 켜져 있지 않습니다. GCP 콘솔에서 "
                "**Google Drive API**와 **Google Sheets API**를 모두 사용 설정하세요.\n\n"
                f"원본: {detail}")
    if reason in ("rateLimitExceeded", "userRateLimitExceeded") or status == 429:
        return f"{action} 실패 — 요청이 너무 잦습니다. 잠시 후 다시 시도하세요."
    if status in (401, 403):
        return (
            f"{action} 권한이 없습니다 (HTTP {status}, reason={reason or '알 수 없음'}). "
            "서비스 계정 이메일을 대상 시트/폴더에 **편집자**로 공유했는지, "
            f"Drive·Sheets API가 켜져 있는지 확인하세요.\n\n원본: {detail}"
        )
    if status == 404:
        return (f"{action} 실패 — 대상을 찾을 수 없습니다 (HTTP 404). "
                f"folder_id와 공유 설정을 확인하세요.\n\n원본: {detail}")
    if _is_missing_tab(exc):
        return (f"{action} 실패 — **그 이름의 탭이 시트에 없습니다.** "
                "앱이 탭을 만들어 다시 시도했는데도 실패했다면, 시트가 열려 있어 "
                "잠겼거나 서비스 계정에 편집 권한이 없을 수 있습니다.\n\n"
                f"원본: {detail}")
    return f"{action} 실패: {detail}"
