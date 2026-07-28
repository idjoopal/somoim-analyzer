"""구글 시트를 단일 저장소로 쓰는 도메인 계층.

## 왜 이 모듈이 있나

예전에는 엑셀 파일 하나가 **원본·보정·결과** 세 가지를 한꺼번에 담았고, 그 파일이
기간 단위로 묶여 있었다. 그래서 새 기간을 분석하면 이전 보정이 따라오지 못했다.
여기서는 셋을 물리적으로 나눈다.

    📗 raw 시트    ← 앱이 쓴다 (id 기준 upsert). 수집 결과.
    📕 보정 시트   ← 사람이 쓴다. 앱은 읽기 + 없는 키만 추가.
    결과          ← 항상 재계산. 저장하지 않는다.

**불변식: 수집은 보정 시트의 기존 값을 절대 건드리지 않는다.**
보정은 안정 키(게시글 id·이름 토큰)에 매달리므로 기간이 달라져도 같은 글·같은
이름이면 그대로 따라붙는다. 이것이 "다시 API를 가져와도 보정이 유지된다"의 전부다.

`SheetsClient`만 네트워크를 쓰고 나머지는 순수 함수라 네트워크 없이 테스트한다.

## 보정 시트에는 실명이 들어간다
링크 공유(`share_anyone_reader`)를 절대 태우지 말 것. `folder_id`로 지정한
비공개 폴더 안에 두어야 한다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from .collector import LEFT_MEMBER, NOT_A_NAME

# ── 파일·탭 이름 (고정 — URL을 붙여넣지 않고 이름으로 찾는다) ──
RAW_TITLE = "다감노_raw"
CORRECTION_TITLE = "다감노_보정"

TAB_POSTS = "게시글"
TAB_PHOTOS = "사진"
TAB_MEMBERS = "멤버"
TAB_BANNED = "탈퇴멤버"
TAB_JOIN_ALIASES = "가입인사매핑"
TAB_HISTORY = "_수집이력"
TAB_FIELDS = "_원본필드"
RAW_TABS = [TAB_POSTS, TAB_PHOTOS, TAB_MEMBERS, TAB_BANNED, TAB_JOIN_ALIASES,
            TAB_HISTORY, TAB_FIELDS]

TAB_NAME_MAP = "이름매핑"
TAB_POST_FIX = "공지보정"
TAB_ATTENDEE_FIX = "참석자보정"
CORRECTION_TABS = [TAB_NAME_MAP, TAB_POST_FIX, TAB_ATTENDEE_FIX]

# ── 컬럼 (헤더 행 = 이 순서) ──
POST_KEYS = [
    "id", "author", "wid", "title", "body", "outing_date", "posted_at",
    "cat", "cat_label", "category", "is_outing", "is_canceled",
    "likes", "comments", "images", "needs_review", "review_reason",
]
PHOTO_KEYS = [
    "id", "author", "wid", "posted_at", "likes", "comments", "has_comment",
    "url_large", "url_medium", "url_small", "url_thumb",
]
MEMBER_KEYS = ["mid", "mn", "is_admin", "joined_at", "last_visit", "os", "push"]
HISTORY_KEYS = ["수집시각", "시작월", "종료월", "게시글", "사진", "멤버"]
FIELD_COLS = ["필드", "사용중", "건수", "예시", "비고"]

NAME_MAP_COLS = ["이름토큰", "처리", "빈도", "비고"]
POST_FIX_COLS = ["공지 id", "제목", "카테고리", "출사일", "취소", "제외", "비고"]
ATTENDEE_FIX_COLS = ["후기 id", "제목", "참석자", "비고"]

_BOOL_KEYS = {"is_outing", "is_canceled", "needs_review", "has_comment",
              "is_admin", "push"}
_DT_KEYS = {"posted_at", "joined_at", "last_visit"}
_ISO_DATE_KEYS = {"outing_date"}

_CELL_MAX_LEN = 32000   # 구글은 5만 자까지 받지만 엑셀(32,767)과 맞춰 둔다
_EXCEL_EPOCH = datetime(1899, 12, 30)


# ═══════════════════════════════════════════════════════════════
# 타입 정규화
# ═══════════════════════════════════════════════════════════════
#
# 시트는 값을 문자열이나 숫자로 돌려준다. 그대로 흘리면 다운스트림의
# `p["posted_at"].month`가 터지고 `date.fromisoformat(outing_date)`가 깨진다.

def coerce_dt(v) -> Optional[datetime]:
    """datetime | date | ISO 문자열 | 엑셀 serial → datetime (실패 시 None)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return _EXCEL_EPOCH + timedelta(days=float(v))
        except (OverflowError, ValueError):
            return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").replace("/", "-"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def coerce_iso_date(v) -> Optional[str]:
    """date | datetime | 문자열 | serial → `"YYYY-MM-DD"` (실패 시 None)."""
    dt = coerce_dt(v)
    return dt.date().isoformat() if dt else None


def coerce_bool(v) -> bool:
    """시트가 돌려주는 `TRUE`/`Y`/`1`/`예` 등을 bool로."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("true", "1", "y", "yes", "예", "o", "▣")


# ═══════════════════════════════════════════════════════════════
# 시트 ↔ 레코드
# ═══════════════════════════════════════════════════════════════

def rows_to_records(rows: list[list], keys: Optional[list[str]] = None) -> list[dict]:
    """2차원 배열 → dict 목록. 첫 행을 헤더로 쓴다.

    구글은 뒤쪽 빈 셀을 생략해 돌려주므로 행 길이가 제각각이다 — 짧은 행은
    None으로 채워 키가 빠지지 않게 한다.
    """
    if not rows:
        return []
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    out: list[dict] = []
    for row in rows[1:]:
        if not row or all(c in (None, "") for c in row):
            continue
        rec = {h: (row[i] if i < len(row) else None)
               for i, h in enumerate(header) if h}
        out.append(normalize_record(rec, keys) if keys else rec)
    return out


def normalize_record(rec: dict, keys: list[str]) -> dict:
    """누락 키를 채우고 날짜·불리언을 파이프라인이 기대하는 타입으로."""
    out = {k: rec.get(k) for k in keys}
    for k in keys:
        if k in _DT_KEYS:
            out[k] = coerce_dt(out[k])
        elif k in _ISO_DATE_KEYS:
            out[k] = coerce_iso_date(out[k])
        elif k in _BOOL_KEYS:
            out[k] = coerce_bool(out[k])
        elif k == "review_reason":
            out[k] = "" if out[k] is None else str(out[k])
        elif k in ("likes", "comments", "images"):
            out[k] = _to_int(out[k])
    return out


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_cell(v) -> Any:
    """레코드 값 → 시트 셀. 시트에 넣을 수 없는 타입을 문자열로 눕힌다."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, (list, tuple, set)):
        return ", ".join(str(x) for x in v)
    s = str(v)
    return s if len(s) <= _CELL_MAX_LEN else s[:_CELL_MAX_LEN]


def records_to_rows(records: list[dict], keys: list[str]) -> list[list]:
    """dict 목록 → 헤더 포함 2차원 배열."""
    return [list(keys)] + [[_to_cell(r.get(k)) for k in keys] for r in records]


# ═══════════════════════════════════════════════════════════════
# upsert — 재수집해도 중복되지 않게
# ═══════════════════════════════════════════════════════════════

def upsert(existing: list[dict], incoming: list[dict], key: str = "id") -> list[dict]:
    """`key` 기준 병합. 같은 키는 새 값으로 교체, 없던 키는 뒤에 추가.

    기존 순서를 유지하므로 시트를 열었을 때 행이 튀지 않는다. 재수집 시 제목·
    좋아요 수 변경이 반영되고, 기간이 겹쳐도 중복이 생기지 않는다.
    """
    by_key = {str(r.get(key)): i for i, r in enumerate(existing) if r.get(key) not in (None, "")}
    merged = list(existing)
    for rec in incoming:
        k = str(rec.get(key))
        if k in (None, "", "None"):
            continue
        if k in by_key:
            merged[by_key[k]] = rec
        else:
            by_key[k] = len(merged)
            merged.append(rec)
    return merged


# ═══════════════════════════════════════════════════════════════
# 보정 — 파싱 · 적용 · 후보 계산
# ═══════════════════════════════════════════════════════════════

def parse_corrections(name_rows: list[list], post_rows: list[list],
                      att_rows: list[list]) -> dict:
    """보정 시트 3개 탭 → 적용 가능한 dict.

    사람이 손으로 채우는 시트라 빈칸·공백이 섞인다. **값이 비어 있는 행은
    "아직 보정 안 함"으로 보고 무시**한다(빈 문자열로 덮어쓰지 않는다).
    """
    names: dict[str, str] = {}
    for r in rows_to_records(name_rows):
        token = _clean(r.get("이름토큰"))
        target = _clean(r.get("처리"))
        if token and target:
            names[token] = target

    posts: dict[str, dict] = {}
    for r in rows_to_records(post_rows):
        pid = _clean(r.get("공지 id"))
        if not pid:
            continue
        fix: dict = {}
        if _clean(r.get("카테고리")):
            fix["category"] = _clean(r.get("카테고리"))
        if _clean(r.get("출사일")):
            iso = coerce_iso_date(r.get("출사일"))
            if iso:
                fix["outing_date"] = iso
        if _clean(r.get("취소")):
            fix["is_canceled"] = coerce_bool(r.get("취소"))
        if _clean(r.get("제외")):
            fix["excluded"] = coerce_bool(r.get("제외"))
        if fix:
            posts[pid] = fix

    attendees: dict[str, list[str]] = {}
    for r in rows_to_records(att_rows):
        rid = _clean(r.get("후기 id"))
        raw = _clean(r.get("참석자"))
        if rid and raw:
            attendees[rid] = [n.strip() for n in raw.split(",") if n.strip()]

    return {"names": names, "posts": posts, "attendees": attendees}


def _clean(v) -> str:
    return "" if v is None else str(v).strip()


def apply_corrections(posts: list[dict], corrections: dict) -> dict[str, int]:
    """보정을 게시글 목록에 in-place 적용. 적용 건수를 종류별로 반환.

    `excluded`로 표시된 공지는 목록에서 빼지 않고 `excluded=True`만 달아 둔다 —
    실제 제외는 호출부가 `filter_excluded`로 한다(무엇이 빠졌는지 셀 수 있게).
    """
    post_fix = corrections.get("posts") or {}
    att_fix = corrections.get("attendees") or {}
    counts = {"공지": 0, "참석자": 0}

    for p in posts:
        pid = str(p.get("id"))
        fix = post_fix.get(pid)
        if fix:
            if "category" in fix:
                p["category"] = fix["category"]
            if "outing_date" in fix:
                p["outing_date"] = fix["outing_date"]
            if "is_canceled" in fix:
                p["is_canceled"] = fix["is_canceled"]
            p["excluded"] = bool(fix.get("excluded", False))
            p["needs_review"] = False
            p["review_reason"] = ""
            counts["공지"] += 1
        if pid in att_fix:
            p["attendees"] = list(att_fix[pid])
            p["attendees_needs_review"] = False
            counts["참석자"] += 1
    return counts


def filter_excluded(posts: list[dict]) -> list[dict]:
    return [p for p in posts if not p.get("excluded")]


def correction_candidates(posts: list[dict], unresolved_freq: dict[str, int],
                          corrections: dict) -> dict[str, list[list]]:
    """보정 시트에 넣을 **후보 행**을 계산 (이미 보정된 것은 제외).

    사용자가 시트를 열었을 때 무엇을 채워야 하는지 보이게 하는 장치.
    반환값은 탭 이름 → 행 목록(헤더 없음).
    """
    done_names = corrections.get("names") or {}
    done_posts = corrections.get("posts") or {}
    done_att = corrections.get("attendees") or {}

    name_rows = [
        [tok, "", int(freq), ""]
        for tok, freq in sorted(unresolved_freq.items(), key=lambda kv: -kv[1])
        if tok not in done_names
    ]

    post_rows = [
        [str(p["id"]), _to_cell(p.get("title")), "", "", "", "", ""]
        for p in posts
        if p.get("cat") == "A" and p.get("needs_review")
        and str(p["id"]) not in done_posts
    ]

    att_rows = [
        [str(p["id"]), _to_cell(p.get("title")), "", ""]
        for p in posts
        if p.get("cat") == "E" and p.get("attendees_needs_review")
        and str(p["id"]) not in done_att
    ]

    return {TAB_NAME_MAP: name_rows, TAB_POST_FIX: post_rows,
            TAB_ATTENDEE_FIX: att_rows}


def missing_rows(existing_rows: list[list], candidate_rows: list[list]) -> list[list]:
    """후보 중 **시트에 아직 없는 키의 행만** 추린다.

    이 함수가 불변식을 지킨다 — 이미 있는 키는 사용자가 값을 채웠든 비워 뒀든
    건드리지 않는다. 비워 둔 행이 시딩으로 되살아나 중복되는 것도 막는다.
    """
    have = set()
    for row in (existing_rows or [])[1:]:      # 헤더 스킵
        if row and _clean(row[0]):
            have.add(_clean(row[0]))
    return [r for r in candidate_rows if _clean(r[0]) not in have]


def resolution_from_corrections(corrections: dict) -> dict[str, str]:
    """보정 시트의 이름매핑 → `annotate_attendees(resolution=...)` 형태.

    시트에는 `__LEFT__`·`__NOISE__`를 그대로 적게 하되, 사람이 쓰기 쉬운
    한글 표기도 받아 준다.
    """
    alias = {
        "탈퇴": LEFT_MEMBER, "탈퇴멤버": LEFT_MEMBER, "left": LEFT_MEMBER,
        "노이즈": NOT_A_NAME, "이름아님": NOT_A_NAME, "noise": NOT_A_NAME,
        "x": NOT_A_NAME, "❌": NOT_A_NAME,
    }
    out: dict[str, str] = {}
    for token, target in (corrections.get("names") or {}).items():
        out[token] = alias.get(target.strip().lower(), target)
    return out


# ═══════════════════════════════════════════════════════════════
# 스토어 (SheetsClient 주입 — 네트워크)
# ═══════════════════════════════════════════════════════════════

class RawStore:
    """수집 데이터를 담는 `다감노_raw` 스프레드시트."""

    def __init__(self, client, file_id: str):
        self.c, self.file_id = client, file_id

    def ensure(self) -> None:
        self.c.ensure_tabs(self.file_id, RAW_TABS)

    def load(self) -> dict:
        return {
            "posts": rows_to_records(self.c.read(self.file_id, TAB_POSTS), POST_KEYS),
            "photos": rows_to_records(self.c.read(self.file_id, TAB_PHOTOS), PHOTO_KEYS),
            "members": rows_to_records(self.c.read(self.file_id, TAB_MEMBERS), MEMBER_KEYS),
            "banned": {
                _clean(r[0]) for r in (self.c.read(self.file_id, TAB_BANNED) or [])[1:]
                if r and _clean(r[0])
            },
            "join_aliases": {
                _clean(r[0]): _clean(r[1])
                for r in (self.c.read(self.file_id, TAB_JOIN_ALIASES) or [])[1:]
                if r and len(r) > 1 and _clean(r[0]) and _clean(r[1])
            },
            "history": rows_to_records(self.c.read(self.file_id, TAB_HISTORY)),
        }

    def save(self, *, posts=None, photos=None, members=None, banned=None,
             join_aliases=None, period: Optional[tuple[int, int]] = None,
             now: Optional[datetime] = None) -> dict[str, int]:
        """기존 데이터와 upsert 병합해 저장하고 `_수집이력`에 한 줄 남긴다."""
        cur = self.load()
        merged_posts = upsert(cur["posts"], posts or [], "id")
        merged_photos = upsert(cur["photos"], photos or [], "id")
        merged_members = upsert(cur["members"], members or [], "mid")

        self.c.write(self.file_id, TAB_POSTS, records_to_rows(merged_posts, POST_KEYS))
        self.c.write(self.file_id, TAB_PHOTOS, records_to_rows(merged_photos, PHOTO_KEYS))
        if members is not None:
            self.c.write(self.file_id, TAB_MEMBERS,
                         records_to_rows(merged_members, MEMBER_KEYS))
        if banned is not None:
            merged_banned = sorted(cur["banned"] | set(banned))
            self.c.write(self.file_id, TAB_BANNED,
                         [["닉네임"]] + [[n] for n in merged_banned])
        if join_aliases is not None:
            merged_aliases = {**cur["join_aliases"], **join_aliases}
            self.c.write(self.file_id, TAB_JOIN_ALIASES,
                         [["실명", "닉네임"]] + [[k, v] for k, v in sorted(merged_aliases.items())])

        if period:
            stamp = (now or datetime.now()).isoformat(sep=" ", timespec="seconds")
            row = [stamp, period[0], period[1],
                   len(posts or []), len(photos or []), len(members or [])]
            hist = self.c.read(self.file_id, TAB_HISTORY)
            if not hist:
                self.c.write(self.file_id, TAB_HISTORY, [HISTORY_KEYS, row])
            else:
                self.c.append(self.file_id, TAB_HISTORY, [row])

        return {"게시글": len(merged_posts), "사진": len(merged_photos),
                "멤버": len(merged_members)}

    def save_field_report(self, report: dict) -> None:
        """API 응답 요약을 `_원본필드` 탭에 덮어쓴다 (진단용, 매 수집 최신으로 교체).

        본문 길이 분포를 맨 위에 둔다 — 서로 다른 글 수백 개가 정확히 같은 길이면
        API가 본문을 잘라 주는 것이고, 그러면 참석자 추출·출사일 추론이 절반만
        보고 판단한다는 뜻이라 가장 먼저 알아야 한다.
        """
        rows = [FIELD_COLS]
        body = report.get("body") or {}
        if body.get("건수"):
            note = ("⚠️ 잘림 의심 — 서로 다른 글이 같은 길이에서 끊깁니다"
                    if body.get("잘림_의심") else "")
            rows.append([
                "(본문 길이)", "", body["건수"],
                f"최소 {body['최소']} / 중앙 {body['중앙']} / 최대 {body['최대']} · "
                f"가장 흔한 길이 {body['최빈길이']}자가 {body['최빈길이_건수']}건",
                note,
            ])
        for r in report.get("fields") or []:
            rows.append([r.get(c, "") for c in FIELD_COLS])
        self.c.write(self.file_id, TAB_FIELDS, rows)


class CorrectionStore:
    """사람이 채우는 `다감노_보정` 스프레드시트.

    앱은 **읽기 + 없는 키 추가**만 한다. 기존 행은 절대 수정·삭제하지 않는다.
    """

    def __init__(self, client, file_id: str):
        self.c, self.file_id = client, file_id

    def ensure(self) -> None:
        """탭과 헤더를 만든다. 이미 내용이 있으면 헤더도 건드리지 않는다."""
        self.c.ensure_tabs(self.file_id, CORRECTION_TABS)
        for tab, cols in ((TAB_NAME_MAP, NAME_MAP_COLS),
                          (TAB_POST_FIX, POST_FIX_COLS),
                          (TAB_ATTENDEE_FIX, ATTENDEE_FIX_COLS)):
            if not self.c.read(self.file_id, tab):
                self.c.write(self.file_id, tab, [cols])

    def load(self) -> dict:
        return parse_corrections(
            self.c.read(self.file_id, TAB_NAME_MAP),
            self.c.read(self.file_id, TAB_POST_FIX),
            self.c.read(self.file_id, TAB_ATTENDEE_FIX),
        )

    def seed(self, candidates: dict[str, list[list]]) -> dict[str, int]:
        """후보 중 시트에 없는 키만 append. 추가한 행 수를 탭별로 반환.

        append만 쓰므로 사용자가 편집 중인 셀을 덮어쓸 일이 없다.
        """
        added: dict[str, int] = {}
        for tab, rows in candidates.items():
            existing = self.c.read(self.file_id, tab)
            new_rows = missing_rows(existing, rows)
            if new_rows:
                self.c.append(self.file_id, tab, new_rows)
            added[tab] = len(new_rows)
        return added

    def pending_count(self) -> int:
        """값이 비어 있는(=아직 안 채운) 행 수 — 사이드바 안내용."""
        n = 0
        for tab, val_col in ((TAB_NAME_MAP, 1), (TAB_POST_FIX, 2), (TAB_ATTENDEE_FIX, 2)):
            for row in (self.c.read(self.file_id, tab) or [])[1:]:
                if not row or not _clean(row[0]):
                    continue
                if len(row) <= val_col or not _clean(row[val_col]):
                    n += 1
        return n


def open_stores(drive_store, client) -> tuple[RawStore, CorrectionStore]:
    """폴더에서 두 파일을 이름으로 찾고(없으면 생성) 스토어를 돌려준다."""
    raw_id, _ = drive_store.find_or_create(RAW_TITLE)
    fix_id, _ = drive_store.find_or_create(CORRECTION_TITLE)
    raw, fix = RawStore(client, raw_id), CorrectionStore(client, fix_id)
    raw.ensure()
    fix.ensure()
    return raw, fix
