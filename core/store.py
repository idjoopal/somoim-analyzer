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

from .collector import ALL_CATS, LEFT_MEMBER, NOT_A_NAME, OUTING_CATS

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

TAB_MEMBER_NAMES = "이름매핑1"      # ① 멤버 실명 — 나머지 보정의 전제
TAB_NAME_MAP = "후기이름매핑"        # ② 실명으로도 안 풀린 나머지
TAB_POST_FIX = "공지보정"
TAB_ATTENDEE_FIX = "참석자보정"
TAB_GUIDE = "_사용법"
CORRECTION_TABS = [TAB_GUIDE, TAB_MEMBER_NAMES, TAB_NAME_MAP,
                   TAB_POST_FIX, TAB_ATTENDEE_FIX]

# 이름이 바뀌기 전의 탭. 사람이 채워 둔 값을 살리려면 새로 만들지 말고
# 이름만 갈아 끼워야 한다 (`CorrectionStore.ensure`).
LEGACY_NAME_MAP = "이름매핑"

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

MEMBER_NAME_COLS = ["멤버 id", "닉네임", "실명", "비고"]
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
                # 카테고리를 고치면 출사 여부도 따라가야 한다. 안 그러면
                # `일반공지`로 바꿔도 출사로 계속 집계된다.
                p["is_outing"] = fix["category"] in OUTING_CATS
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
                          corrections: dict, members: Optional[list[dict]] = None,
                          join_aliases: Optional[dict] = None,
                          ) -> dict[str, list[list]]:
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

    out = {TAB_NAME_MAP: name_rows, TAB_POST_FIX: post_rows,
           TAB_ATTENDEE_FIX: att_rows}
    if members:
        out[TAB_MEMBER_NAMES] = member_name_candidates(members, join_aliases)
    return out


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


# ═══════════════════════════════════════════════════════════════
# 보정 시트 가이드
#
# 보정이 이 설계의 핵심인데, 정작 보정하는 화면에 설명이 없으면 아무도
# 제대로 채울 수 없다. 설명은 세 겹으로 넣는다.
#   ① `_사용법` 탭      — 전체 규칙
#   ② 헤더 셀 메모      — 그 칸에 뭘 넣는지 (헤더 텍스트는 파싱 키라 못 바꿈)
#   ③ 드롭다운          — 애초에 잘못 넣을 수 없게. 설명보다 이게 세다
# ═══════════════════════════════════════════════════════════════

GUIDE_ROWS: list[list[str]] = [
    ["📕 다감노 보정 시트 사용법"],
    [""],
    ["이 시트는 사람이 채우는 곳입니다. 앱은 여기 값을 읽기만 하고,"],
    ["빠진 항목을 새로 추가할 뿐 이미 적으신 값은 절대 건드리지 않습니다."],
    ["→ 다시 수집해도, 다른 기간을 수집해도 보정은 그대로 유지됩니다."],
    [""],
    ["■ 공통 규칙"],
    ["  · 빈칸 = \"아직 보정 안 함\". 비워 두면 앱의 자동 판단을 그대로 씁니다."],
    ["  · 행을 지우지 마세요. 지워도 다음 수집 때 다시 추가됩니다."],
    ["  · 회색 참고 열(제목·빈도)은 알아보기 쉬우라고 앱이 채워 둔 값입니다. 고쳐도 반영되지 않습니다."],
    ["  · 첫 열(id·이름토큰)이 보정을 붙이는 열쇠입니다. 절대 고치지 마세요."],
    ["  · 각 탭 헤더 칸에 마우스를 올리면 그 칸 설명이 뜹니다."],
    [""],
    ["■ ① 이름매핑1 — 멤버 실명 (여기부터 하세요)"],
    ["  앱은 닉네임만 알고 실명을 모릅니다. 그런데 후기 본문에는 실명이 더 자주 나옵니다."],
    ["  실명을 채워 두면 나머지 보정에서 손댈 것이 크게 줄어듭니다."],
    ["  · 실명      실제 이름. 가입인사에서 찾은 것은 미리 채워 두었습니다"],
    ["  · 닉네임 순으로 정렬돼 있어 갖고 계신 명단을 '실명' 열에 통째로 붙여넣을 수 있습니다"],
    ["  · '멤버 id'는 고치지 마세요 — 닉네임이 바뀌어도 이 값으로 같은 사람을 따라갑니다"],
    [""],
    ["■ ② 후기이름매핑 — 실명 명단으로도 안 풀린 나머지"],
    ["  ①을 채우고 나면 여기 남는 것은 오타·탈퇴자·이름이 아닌 것들입니다."],
    ["  '처리' 칸에 셋 중 하나를 넣습니다 (드롭다운에서 고르세요)."],
    ["  · 마스터 닉네임    같은 사람의 다른 표기일 때. 멤버 명단에 있는 닉네임 그대로"],
    [f"  · {LEFT_MEMBER}         탈퇴한 멤버. 집계에서 빠집니다. \"탈퇴\"라고 써도 됩니다"],
    [f"  · {NOT_A_NAME}         이름이 아님(조사·오탈자 등). \"노이즈\"·\"❌\"도 됩니다"],
    ["  목록에 없는 새 닉네임도 직접 입력할 수 있습니다(경고만 뜹니다)."],
    [""],
    ["■ ③ 공지보정 — 출사 공지의 카테고리·날짜가 자동 판단으로 안 잡힐 때"],
    ["  · 카테고리   " + " / ".join(ALL_CATS)],
    ["                어느 것도 아닌 전체 공지라면 `일반공지`를 고르세요 — 출사 집계에서 빠집니다"],
    ["  · 출사일     YYYY-MM-DD (예: 2026-03-14). 공지 작성일이 아니라 실제 출사한 날"],
    ["  · 취소       출사가 취소됐으면 TRUE"],
    ["  · 제외       출사 공지가 아니거나 집계에서 빼야 하면 TRUE"],
    ["  넷 중 채운 칸만 덮어씁니다. 카테고리만 고치고 싶으면 카테고리만 채우세요."],
    [""],
    ["■ ④ 참석자보정 — 후기 본문에서 참석자를 못 뽑았거나 틀리게 뽑았을 때"],
    ["  · 참석자     쉼표로 구분한 닉네임. 예: 원석사진, 나무, 바다"],
    ["  · 여기에 적으면 본문에서 뽑은 결과를 통째로 대체합니다(더하지 않습니다)."],
    ["  · 작성자도 참석했다면 작성자 닉네임을 함께 적어 주세요."],
    [""],
    ["■ 작업 순서"],
    ["  1. 앱에서 수집  → 앱이 채울 후보 행을 이 시트에 추가"],
    ["  2. 이 시트에서 ①부터 차례로 빈칸 채우기"],
    ["  3. 앱에서 [새로고침] → 보정이 반영된 분석 결과"],
    ["  다음부터는 1을 다시 해도 2가 유지되므로 3만 누르면 됩니다."],
    [""],
    ["⚠️ 이 시트와 분석 결과 파일에는 실명이 들어갑니다. 공유에 주의하세요."],
]

# 열 인덱스 → 헤더 셀 메모. 헤더 텍스트를 바꾸면 파싱이 깨지므로 메모로 붙인다.
HEADER_NOTES: dict[str, dict[int, str]] = {
    TAB_MEMBER_NAMES: {
        0: "멤버 고유 id. 닉네임이 바뀌어도 이 값으로 같은 사람을 따라갑니다.\n"
           "고치지 마세요 — 보정을 붙이는 열쇠입니다.",
        1: "수집 시점의 닉네임. 앱이 채운 참고값이라 이후 갱신되지 않습니다.",
        2: "**이 칸을 채우시면 됩니다.** 실제 이름.\n"
           "가입인사에서 찾은 것은 미리 채워 두었습니다. 나머지를 채우세요.\n"
           "닉네임 순으로 정렬돼 있어 기존 명단을 통째로 붙여넣을 수 있습니다.",
        3: "자유 메모. 앱은 읽지 않습니다.",
    },
    TAB_NAME_MAP: {
        0: "후기 본문에서 나온 이름 표기. 보정을 붙이는 열쇠이므로 고치지 마세요.",
        1: f"마스터 닉네임 / {LEFT_MEMBER}(탈퇴) / {NOT_A_NAME}(이름 아님) 중 하나.\n"
           "비워 두면 '아직 보정 안 함'으로 봅니다.",
        2: "이 표기가 나온 횟수. 앱이 채운 참고값입니다.",
        3: "자유 메모. 앱은 읽지 않습니다.",
    },
    TAB_POST_FIX: {
        0: "공지 게시글 id. 보정을 붙이는 열쇠이므로 고치지 마세요.",
        1: "공지 제목. 앱이 채운 참고값입니다.",
        2: "카테고리: " + " / ".join(ALL_CATS) + "\n"
           "어느 것도 아닌 전체 공지는 `일반공지` — 출사 집계에서 빠집니다.",
        3: "실제 출사한 날짜 YYYY-MM-DD (예: 2026-03-14). 공지 작성일이 아닙니다.",
        4: "출사가 취소됐으면 TRUE.",
        5: "출사 공지가 아니거나 집계에서 빼야 하면 TRUE.",
        6: "자유 메모. 앱은 읽지 않습니다.",
    },
    TAB_ATTENDEE_FIX: {
        0: "후기 게시글 id. 보정을 붙이는 열쇠이므로 고치지 마세요.",
        1: "후기 제목. 앱이 채운 참고값입니다.",
        2: "쉼표로 구분한 참석자 닉네임 (예: 원석사진, 나무).\n"
           "본문에서 뽑은 결과를 통째로 대체합니다. 작성자도 참석했으면 함께 적으세요.",
        3: "자유 메모. 앱은 읽지 않습니다.",
    },
}

BOOL_CHOICES = ["TRUE", "FALSE"]


def dropdowns(master_names=None) -> list[tuple[str, int, list[str]]]:
    """(탭, 열 인덱스, 목록). 설명을 읽게 하는 것보다 잘못 넣을 수 없게 하는 게 낫다.

    `이름매핑`의 목록은 멤버 명단이 바뀌면 달라지므로 `seed()` 때마다 갱신한다.
    다만 목록에 없는 새 닉네임을 입력조차 못 하게 막으면 곤란하므로
    `set_validation`은 `strict=False`(경고만)로 건다.
    """
    out: list[tuple[str, int, list[str]]] = []
    names = sorted({str(n).strip() for n in (master_names or []) if str(n).strip()})
    if names:
        out.append((TAB_NAME_MAP, 1, names + [LEFT_MEMBER, NOT_A_NAME]))
    # `이름매핑1`의 `실명`에는 걸지 않는다 — 목록이 있을 수 없는 자유 입력이다.
    out.append((TAB_POST_FIX, 2, list(ALL_CATS)))
    out.append((TAB_POST_FIX, 4, BOOL_CHOICES))
    out.append((TAB_POST_FIX, 5, BOOL_CHOICES))
    return out


# ═══════════════════════════════════════════════════════════════
# 멤버 실명 — 보정의 1단계
#
# raw `멤버` 탭에는 닉네임만 있고 실명이 없다. 그런데 후기 본문에는 실명이
# 더 자주 나온다. 그래서 실명 명단이 없으면 `후기이름매핑`에 "사실 멤버인데
# 못 알아본 이름"과 "진짜 문제(오타·탈퇴자·이름 아님)"가 뒤섞여 쌓인다.
#
# 키는 `mid`다. 닉네임은 바뀌고 중복될 수 있어 키로 쓸 수 없다.
# ═══════════════════════════════════════════════════════════════

def member_name_candidates(members: list[dict],
                           join_aliases: Optional[dict] = None) -> list[list]:
    """`이름매핑1`에 깔 행 — 전 멤버, 닉네임 오름차순.

    `실명`은 가입인사에서 뽑은 것(실명→닉네임)을 미리 채운다. 나머지는 사람이
    채우는데, 정렬이 안정적이라 기존 명단을 열에 통째로 붙여넣을 수 있다.
    """
    real_by_nick: dict[str, str] = {}
    for real, nick in (join_aliases or {}).items():
        if _clean(real) and _clean(nick):
            real_by_nick.setdefault(_clean(nick), _clean(real))

    rows = []
    for m in members or []:
        mid, nick = _clean(m.get("mid")), _clean(m.get("mn"))
        if not mid:
            continue
        rows.append([mid, nick, real_by_nick.get(nick, ""), ""])
    return sorted(rows, key=lambda r: (r[1], r[0]))


def parse_member_names(rows: list[list]) -> dict[str, str]:
    """`이름매핑1` → `{mid: 실명}`. 빈칸은 "아직 안 채움"이라 건너뛴다."""
    out: dict[str, str] = {}
    for r in rows_to_records(rows):
        mid, real = _clean(r.get("멤버 id")), _clean(r.get("실명"))
        if mid and real:
            out[mid] = real
    return out


def real_name_resolution(member_names: dict[str, str],
                         members: list[dict]) -> dict[str, str]:
    """`{실명: 현재 닉네임}` — `annotate_attendees(resolution=…)`에 넣을 형태.

    닉네임은 시트의 참고 열이 아니라 **raw 멤버의 현재 값**에서 가져온다.
    그래서 닉네임이 바뀌어도 매칭이 따라간다.
    """
    nick_by_mid = {_clean(m.get("mid")): _clean(m.get("mn"))
                   for m in members or [] if _clean(m.get("mid"))}
    out: dict[str, str] = {}
    for mid, real in (member_names or {}).items():
        nick = nick_by_mid.get(_clean(mid))
        if nick and real != nick:      # 실명==닉네임이면 매핑할 것이 없다
            out[real] = nick
    return out


def real_by_nickname(member_names: dict[str, str],
                     members: list[dict]) -> dict[str, str]:
    """`{닉네임: 실명}` — 화면·엑셀 병기용."""
    out: dict[str, str] = {}
    for m in members or []:
        mid, nick = _clean(m.get("mid")), _clean(m.get("mn"))
        real = _clean((member_names or {}).get(mid))
        if nick and real:
            out[nick] = real
    return out


def display_name(nick, real_by_nick: Optional[dict] = None) -> str:
    """`닉네임(실명)`. 실명을 모르거나 같으면 닉네임 그대로."""
    n = _clean(nick)
    real = _clean((real_by_nick or {}).get(n))
    return f"{n}({real})" if real and real != n else n


# 표시용으로 `닉네임(실명)`으로 바꿀 필드. 스칼라와 목록을 나눠 둔다.
_NAME_FIELDS = ("author", "mn")
_NAME_LIST_FIELDS = ("attendees",)


def relabel_names(records: list[dict],
                  real_by_nick: Optional[dict] = None) -> list[dict]:
    """표시용 **사본** — 이름을 전부 `닉네임(실명)`으로.

    게시글·사진·멤버 어디에나 쓴다. 참석 횟수·업로더 랭킹·선호 카테고리가
    모두 이 필드들에서 파생되므로 여기 한 곳만 바꾸면 표·엑셀이 한꺼번에 따라온다.

    원본 값은 `_raw_<필드>`에 남긴다 — 동명이인 마킹처럼 **표시가 아니라
    동일성 판정**에 쓰는 곳이 있어서, 그쪽은 계속 원래 닉네임을 봐야 한다.
    원본 리스트는 건드리지 않는다(매칭 키가 깨진다).
    """
    if not real_by_nick:
        return records
    out = []
    for r in records or []:
        changed = {}
        for f in _NAME_FIELDS:
            v = r.get(f)
            if v:
                shown = display_name(v, real_by_nick)
                if shown != v:
                    changed[f] = shown
                    changed[f"_raw_{f}"] = v
        for f in _NAME_LIST_FIELDS:
            v = r.get(f)
            if v:
                changed[f] = [display_name(n, real_by_nick) for n in v]
        out.append({**r, **changed} if changed else r)
    return out


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

    def ensure(self, master_names=None, members=None, join_aliases=None) -> None:
        """탭·헤더를 만들고, 멤버 명단을 깔고, 안내를 갱신한다.

        **멤버 명단은 여기서 깐다.** raw에 이미 있는 데이터에서 파생되는 것이라
        API 재수집을 기다릴 이유가 없다 — 앱을 여는 것만으로 채울 준비가 돼야
        보정 1단계를 바로 시작할 수 있다.

        이미 내용이 있으면 헤더도 건드리지 않는다. 안내(사용법 탭·메모·드롭다운·
        헤더 고정)는 사용자가 입력한 셀에 닿지 않으므로 매번 갱신한다.
        """
        self.migrate()
        self.c.ensure_tabs(self.file_id, CORRECTION_TABS)
        for tab, cols in ((TAB_MEMBER_NAMES, MEMBER_NAME_COLS),
                          (TAB_NAME_MAP, NAME_MAP_COLS),
                          (TAB_POST_FIX, POST_FIX_COLS),
                          (TAB_ATTENDEE_FIX, ATTENDEE_FIX_COLS)):
            if not self.c.read(self.file_id, tab):
                self.c.write(self.file_id, tab, [cols])
        if members:
            self.seed({TAB_MEMBER_NAMES:
                       member_name_candidates(members, join_aliases)})
        self.write_guide(master_names)

    def migrate(self) -> None:
        """`이름매핑` → `후기이름매핑`. 탭 이름만 갈아 끼워 내용을 살린다.

        `ensure_tabs`보다 **먼저** 돌아야 한다. 순서가 뒤집히면 빈
        `후기이름매핑`이 먼저 생겨 이름 변경이 건너뛰어지고, 사람이 채워 둔
        값이 옛 탭에 고립된다.
        """
        try:
            self.c.rename_tab(self.file_id, LEGACY_NAME_MAP, TAB_NAME_MAP)
        except Exception:  # noqa: BLE001 — 이관 실패가 보정을 막아서는 안 된다
            pass

    def write_guide(self, master_names=None) -> None:
        """사용법 탭·헤더 메모·드롭다운·헤더 고정.

        서식은 부가 기능이라 실패해도 보정 자체를 막아서는 안 된다 —
        구버전 시트나 권한 문제로 서식 요청이 거절돼도 조용히 넘어간다.
        """
        try:
            ids = self.c.sheet_ids(self.file_id)
            self.c.write(self.file_id, TAB_GUIDE, GUIDE_ROWS)
            for tab, notes in HEADER_NOTES.items():
                self.c.set_header_notes(self.file_id, tab, notes,
                                        sheet_id=ids.get(tab))
                self.c.freeze_header(self.file_id, tab, sheet_id=ids.get(tab))
            for tab, col, values in dropdowns(master_names):
                self.c.set_validation(self.file_id, tab, col, values,
                                      sheet_id=ids.get(tab))
        except Exception:  # noqa: BLE001
            pass

    def load(self) -> dict:
        out = parse_corrections(
            self.c.read(self.file_id, TAB_NAME_MAP),
            self.c.read(self.file_id, TAB_POST_FIX),
            self.c.read(self.file_id, TAB_ATTENDEE_FIX),
        )
        out["member_names"] = parse_member_names(
            self.c.read(self.file_id, TAB_MEMBER_NAMES))
        return out

    def seed(self, candidates: dict[str, list[list]],
             master_names=None) -> dict[str, int]:
        """후보 중 시트에 없는 키만 append. 추가한 행 수를 탭별로 반환.

        append만 쓰므로 사용자가 편집 중인 셀을 덮어쓸 일이 없다.
        `master_names`를 주면 이름 드롭다운 목록을 최신 멤버 명단으로 갱신한다.
        """
        added: dict[str, int] = {}
        for tab, rows in candidates.items():
            existing = self.c.read(self.file_id, tab)
            new_rows = missing_rows(existing, rows)
            if new_rows:
                self.c.append(self.file_id, tab, new_rows)
            added[tab] = len(new_rows)
        # 안내는 **행을 붙인 뒤에** 갱신한다. 먼저 걸면 나중에 붙는 행이
        # 드롭다운에서 빠진다 (330행부터만 드롭다운이 있던 원인).
        if master_names:
            self.write_guide(master_names)
        return added

    def pending_count(self) -> dict[str, int]:
        """탭별 미기입 행 수 — 사이드바 안내용.

        `이름매핑1`이 맨 앞이다. 실명 명단이 먼저 서야 나머지 보정이 줄어든다.
        """
        out: dict[str, int] = {}
        for tab, val_col in ((TAB_MEMBER_NAMES, 2), (TAB_NAME_MAP, 1),
                             (TAB_POST_FIX, 2), (TAB_ATTENDEE_FIX, 2)):
            n = 0
            for row in (self.c.read(self.file_id, tab) or [])[1:]:
                if not row or not _clean(row[0]):
                    continue
                if len(row) <= val_col or not _clean(row[val_col]):
                    n += 1
            out[tab] = n
        return out


def open_stores(drive_store, client, raw_file_id=None,
                correction_file_id=None) -> tuple[RawStore, CorrectionStore]:
    """폴더에서 두 파일을 이름으로 찾고(없으면 생성) 스토어를 돌려준다.

    파일 id를 직접 주면 이름 탐색을 건너뛴다. 이름 매칭은 글자 하나(뒤에 붙은
    공백, 자소 분리)만 달라도 "파일이 없다 → 만들려다 403"으로 나타나므로,
    한 번 자리를 잡은 뒤에는 id로 고정하는 편이 안전하다.
    """
    raw_id = raw_file_id or drive_store.find_or_create(RAW_TITLE)[0]
    fix_id = correction_file_id or drive_store.find_or_create(CORRECTION_TITLE)[0]
    raw, fix = RawStore(client, raw_id), CorrectionStore(client, fix_id)
    raw.ensure()
    # raw를 읽어 멤버 명단과 이름 드롭다운을 **수집 없이도** 채운다.
    # 둘 다 이미 저장된 데이터에서 파생되므로 API를 다시 부를 이유가 없다.
    try:
        cur = raw.load()
        members, join_aliases = cur.get("members") or [], cur.get("join_aliases") or {}
    except Exception:  # noqa: BLE001 — raw를 못 읽어도 보정 시트는 열려야 한다
        members, join_aliases = [], {}
    fix.ensure({m["mn"] for m in members if m.get("mn")},
               members=members, join_aliases=join_aliases)
    return raw, fix
