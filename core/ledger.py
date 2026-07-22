"""
보정 원장(ledger) — 사용자 보정을 GitHub 비공개 리포지토리에 영구 저장/복원.

사용자가 앱에서 한 보정(이름 해소·공지 분류·참석자 편집)을 안정적인 키
(이름 토큰·게시글 id)에 매단 JSON 한 파일로 저장한다. 다음 수집 때 이 원장을
자동 적용하면 같은 보정을 다시 할 필요가 없고, 원하면 앱에서 언제든 재보정할
수 있다(재보정도 원장에 반영).

원장 스키마 (corrections.json):
{
  "version": 1,
  "name_resolution":  {"이름토큰": "닉네임" | "__LEFT__" | "__NOISE__", ...},
  "post_overrides":   {"<공지 id>": {"category": str|null, "outing_date": "YYYY-MM-DD"|null,
                                      "is_canceled": bool, "excluded": bool}, ...},
  "attendee_overrides": {"<후기 id>": ["이름", ...], ...}
}

GithubLedgerStore만 네트워크(GitHub Contents API)를 쓰고, 나머지는 전부
순수 함수라 네트워크 없이 단위 테스트한다. 보정 데이터에 실명이 들어가므로
저장 리포는 반드시 비공개(private)로 만들 것.
"""

from __future__ import annotations

import base64
import json
from typing import Optional

import requests

LEDGER_VERSION = 1
DEFAULT_PATH = "corrections.json"
DEFAULT_BRANCH = "main"

_API_ROOT = "https://api.github.com"


class LedgerError(RuntimeError):
    """원장 로드/저장 실패 (토큰·리포 설정 오류, 네트워크 등)."""


# ═══════════════════════════════════════════════════════════════
# 원장 구조 (순수 함수)
# ═══════════════════════════════════════════════════════════════

def empty_ledger() -> dict:
    return {
        "version": LEDGER_VERSION,
        "name_resolution": {},
        "post_overrides": {},
        "attendee_overrides": {},
    }


def normalize_ledger(data) -> dict:
    """외부에서 읽은 JSON을 스키마에 맞게 정돈. 알 수 없는 형태는 무시."""
    led = empty_ledger()
    if not isinstance(data, dict):
        return led

    nr = data.get("name_resolution")
    if isinstance(nr, dict):
        led["name_resolution"] = {
            str(k): str(v) for k, v in nr.items() if k and v
        }

    po = data.get("post_overrides")
    if isinstance(po, dict):
        for pid, ov in po.items():
            if not pid or not isinstance(ov, dict):
                continue
            led["post_overrides"][str(pid)] = {
                "category":    ov.get("category") or None,
                "outing_date": ov.get("outing_date") or None,
                "is_canceled": bool(ov.get("is_canceled", False)),
                "excluded":    bool(ov.get("excluded", False)),
            }

    ao = data.get("attendee_overrides")
    if isinstance(ao, dict):
        for pid, names in ao.items():
            if not pid or not isinstance(names, list):
                continue
            led["attendee_overrides"][str(pid)] = [
                str(n).strip() for n in names if str(n).strip()
            ]

    return led


def dumps_ledger(ledger: dict) -> str:
    """커밋용 안정 직렬화 (키 정렬 → 무의미한 diff 방지)."""
    return json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def ledger_counts(ledger: dict) -> dict[str, int]:
    return {
        "이름": len(ledger.get("name_resolution", {})),
        "공지": len(ledger.get("post_overrides", {})),
        "참석": len(ledger.get("attendee_overrides", {})),
    }


def merge_ledgers(base: dict, ours: dict) -> dict:
    """충돌 시 병합: base(원격) 위에 ours(이번 세션 보정)를 키 단위로 덮어씀."""
    out = normalize_ledger(base)
    ours = normalize_ledger(ours)
    out["name_resolution"].update(ours["name_resolution"])
    out["post_overrides"].update(ours["post_overrides"])
    out["attendee_overrides"].update(ours["attendee_overrides"])
    return out


# ═══════════════════════════════════════════════════════════════
# 원장 → 데이터 적용
# ═══════════════════════════════════════════════════════════════

def apply_post_overrides(posts: list[dict], ledger: dict,
                         outing_cats: list[str]) -> int:
    """cat=A 공지에 post_overrides를 in-place 적용. 적용 건수 반환.

    적용된 공지는 needs_review가 꺼져 ② 단계에서 다시 묻지 않는다.
    excluded=True는 '사용자가 분석 제외를 확정'한 공지 — 출사일을 비워
    apply_triage에서 자연히 제외되게 한다.
    """
    ov_map = ledger.get("post_overrides", {})
    if not ov_map:
        return 0
    n = 0
    for p in posts:
        if p.get("cat") != "A":
            continue
        ov = ov_map.get(str(p.get("id")))
        if not ov:
            continue
        p["category"] = ov.get("category")
        p["is_outing"] = p["category"] in outing_cats
        p["is_canceled"] = bool(ov.get("is_canceled"))
        p["outing_date"] = None if ov.get("excluded") else (ov.get("outing_date") or None)
        p["needs_review"] = False
        p["review_reason"] = ""
        p["ledger_applied"] = True
        n += 1
    return n


def apply_attendee_overrides(posts: list[dict], ledger: dict) -> int:
    """cat=E 후기에 attendee_overrides를 in-place 적용. 적용 건수 반환.

    자동 추출값은 `_auto_attendees`에 1회 보관해 두어, 이후 편집이
    자동값으로 되돌아오면 원장에서 override를 지울 수 있게 한다(재보정).
    여러 번 불려도 결과가 같다(멱등).
    """
    ov_map = ledger.get("attendee_overrides", {})
    if not ov_map:
        return 0
    n = 0
    for p in posts:
        if p.get("cat") != "E":
            continue
        ov = ov_map.get(str(p.get("id")))
        if ov is None:
            continue
        if "_auto_attendees" not in p:
            p["_auto_attendees"] = list(p.get("attendees", []))
        p["attendees"] = list(ov)
        p["unresolved_names"] = []
        n += 1
    return n


# ═══════════════════════════════════════════════════════════════
# 보정 → 원장 반영
# ═══════════════════════════════════════════════════════════════

def update_name_resolutions(ledger: dict,
                            decisions: dict[str, Optional[str]]) -> bool:
    """이름 해소 결정 반영. decisions: 이름 → 처리값(None이면 매핑 삭제).

    Returns: 원장이 실제로 바뀌었는지.
    """
    nr = ledger.setdefault("name_resolution", {})
    changed = False
    for name, choice in decisions.items():
        if not name:
            continue
        if choice is None:
            if name in nr:
                nr.pop(name)
                changed = True
        elif nr.get(name) != choice:
            nr[name] = choice
            changed = True
    return changed


def update_post_override(ledger: dict, post_id,
                         override: Optional[dict]) -> bool:
    """공지 분류 보정 반영. override=None이면 해당 공지의 보정 삭제."""
    po = ledger.setdefault("post_overrides", {})
    pid = str(post_id)
    if override is None:
        if pid in po:
            po.pop(pid)
            return True
        return False
    norm = {
        "category":    override.get("category") or None,
        "outing_date": override.get("outing_date") or None,
        "is_canceled": bool(override.get("is_canceled", False)),
        "excluded":    bool(override.get("excluded", False)),
    }
    if po.get(pid) == norm:
        return False
    po[pid] = norm
    return True


def update_attendee_override(ledger: dict, post_id,
                             attendees: Optional[list]) -> bool:
    """후기 참석자 보정 반영. attendees=None이면 보정 삭제(자동 추출로 복귀)."""
    ao = ledger.setdefault("attendee_overrides", {})
    pid = str(post_id)
    if attendees is None:
        if pid in ao:
            ao.pop(pid)
            return True
        return False
    vals = [str(n).strip() for n in attendees if str(n).strip()]
    if ao.get(pid) == vals:
        return False
    ao[pid] = vals
    return True


# ═══════════════════════════════════════════════════════════════
# GitHub 저장소 (Contents API)
# ═══════════════════════════════════════════════════════════════

class GithubLedgerStore:
    """GitHub 리포지토리의 JSON 파일 하나를 원장으로 쓰는 저장소.

    - 사용자(비개발자)는 GitHub을 볼 일이 없다 — 앱이 뒤에서 읽고 커밋한다.
    - 커밋 이력이 곧 보정 이력이라 잘못 저장해도 언제든 되돌릴 수 있다.
    - fine-grained 토큰(해당 리포 Contents read/write만) 사용을 권장.
    """

    def __init__(self, token: str, repo: str,
                 path: str = DEFAULT_PATH, branch: str = DEFAULT_BRANCH,
                 timeout: int = 10):
        if not token or not repo or "/" not in repo:
            raise LedgerError("ledger 설정 오류: token과 repo('owner/repo')가 필요합니다.")
        self.repo = repo
        self.path = path or DEFAULT_PATH
        self.branch = branch or DEFAULT_BRANCH
        self.timeout = timeout
        self._url = f"{_API_ROOT}/repos/{repo}/contents/{self.path}"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def load(self) -> tuple[dict, Optional[str]]:
        """원장 로드. Returns: (ledger, sha). 파일이 아직 없으면 (빈 원장, None)."""
        try:
            r = requests.get(self._url, headers=self._headers,
                             params={"ref": self.branch}, timeout=self.timeout)
        except requests.RequestException as e:
            raise LedgerError(f"원장 저장소에 연결하지 못했습니다: {e}") from e
        if r.status_code == 404:
            return empty_ledger(), None
        self._raise_for_status(r, "로드")
        data = r.json()
        try:
            content = base64.b64decode(data.get("content", "") or "")
            ledger = normalize_ledger(json.loads(content.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            # 손상된 파일 — 빈 원장으로 시작하되 기존 파일은 sha로 덮어써 복구
            ledger = empty_ledger()
        return ledger, data.get("sha")

    def save(self, ledger: dict, sha: Optional[str], message: str) -> str:
        """원장 커밋. sha 충돌(다른 곳에서 먼저 저장) 시 원격과 병합 후 1회 재시도.

        Returns: 새 sha.
        """
        new_sha = self._put(ledger, sha, message)
        if new_sha is not None:
            return new_sha
        # 충돌 → 원격을 다시 읽어 병합(원격 base + 이번 보정 우선) 후 재시도
        remote, remote_sha = self.load()
        merged = merge_ledgers(remote, ledger)
        new_sha = self._put(merged, remote_sha, message + " (병합)")
        if new_sha is None:
            raise LedgerError("원장 저장 충돌이 반복됩니다. 잠시 후 다시 시도하세요.")
        # 병합 결과를 호출자가 이어 쓰도록 반영
        ledger.clear()
        ledger.update(merged)
        return new_sha

    def _put(self, ledger: dict, sha: Optional[str], message: str) -> Optional[str]:
        """1회 커밋 시도. 성공 시 새 sha, sha 충돌 시 None."""
        body = {
            "message": message,
            "content": base64.b64encode(dumps_ledger(ledger).encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        try:
            r = requests.put(self._url, headers=self._headers, json=body,
                             timeout=self.timeout)
        except requests.RequestException as e:
            raise LedgerError(f"원장 저장소에 연결하지 못했습니다: {e}") from e
        if r.status_code in (409, 422):
            return None
        self._raise_for_status(r, "저장")
        return r.json().get("content", {}).get("sha")

    def _raise_for_status(self, r, action: str) -> None:
        if r.status_code < 400:
            return
        if r.status_code == 401:
            hint = "토큰이 유효하지 않습니다. secrets의 ledger.token을 확인하세요."
        elif r.status_code in (403, 404):
            hint = (f"리포지토리 '{self.repo}'에 접근할 수 없습니다. "
                    "repo 이름과 토큰의 리포 권한(Contents read/write)을 확인하세요.")
        else:
            hint = f"GitHub API 오류 (HTTP {r.status_code})"
        raise LedgerError(f"원장 {action} 실패 — {hint}")
