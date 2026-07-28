"""소모임 API에서 **본문 전문**과 **댓글**을 주는 주소를 찾는다 (일회성 탐색).

제품 코드가 아니다. 한 번 돌려 결과를 보고 배선을 확정한 뒤 지운다.

왜 필요한가: 목록 API(`/api/articles`)는 본문을 미리보기로 잘라 주고 댓글은
개수(`rn`)만 준다. 전문과 댓글을 주는 주소는 코드에 없다. **이름을 추측해
제품 코드에 넣으면 조용히 아무것도 안 하는 코드가 되므로** 먼저 확인한다.

    python scripts/probe_article_detail.py

기준점은 **댓글이 여러 개 달린 글**을 고른다. 댓글 수를 이미 알고 있어야
찾은 배열이 진짜 댓글인지 검산할 수 있다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.collector import BASE_URL, GROUP_ID, HEADERS  # noqa: E402

TIMEOUT = 10
_SAMPLE = 200

# 목록 키가 `cs`(contents)라 `/api/content` 계열이 유력하다.
DETAIL_PATHS = ["/api/article", "/api/content", "/api/article_view",
                "/api/articleView", "/api/article_detail", "/api/contents"]
COMMENT_PATHS = ["/api/comments", "/api/comment", "/api/replies", "/api/reply",
                 "/api/article_comments", "/api/article_reply"]
ID_KEYS = ["aid", "cid", "id", "c_id", "aid_"]


def post(path: str, payload: dict):
    """(status, json 또는 None, 원문 앞부분)."""
    try:
        r = requests.post(BASE_URL + path, headers=HEADERS,
                          json=payload, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return None, None, f"요청 실패: {e}"
    try:
        return r.status_code, r.json(), ""
    except Exception:  # noqa: BLE001
        return r.status_code, None, r.text[:_SAMPLE]


def pick_reference() -> dict:
    """댓글이 가장 많이 달린 글 하나 — 검산 기준점."""
    r = requests.post(BASE_URL + "/api/articles", headers=HEADERS,
                      json={"gid": GROUP_ID, "wql": 20}, timeout=TIMEOUT)
    r.raise_for_status()
    items = r.json().get("cs") or []
    if not items:
        raise SystemExit("목록이 비었습니다 — GROUP_ID나 헤더를 확인하세요.")
    return max(items, key=lambda p: int(p.get("rn") or 0))


def long_strings(obj, longer_than: int, path="") -> list[tuple[str, int, str]]:
    """`longer_than`보다 긴 문자열 필드를 전부 찾는다 (필드명을 미리 정하지 않는다)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += long_strings(v, longer_than, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            out += long_strings(v, longer_than, f"{path}[{i}]")
    elif isinstance(obj, str) and len(obj) > longer_than:
        out.append((path, len(obj), obj[:80].replace("\n", " ")))
    return out


def arrays(obj, path="") -> list[tuple[str, int, object]]:
    """응답 안의 모든 배열 — 길이가 댓글 수와 맞는 게 있으면 그게 댓글이다."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += arrays(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        out.append((path, len(obj), obj[0] if obj else None))
        for i, v in enumerate(obj[:2]):
            out += arrays(v, f"{path}[{i}]")
    return out


def image_like(obj, path="") -> list[tuple[str, str]]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += image_like(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            out += image_like(v, f"{path}[{i}]")
    elif isinstance(obj, str) and (
            obj.startswith("http") and any(x in obj.lower() for x in (".jpg", ".jpeg", ".png", "image", "photo"))
            or (len(obj) > 20 and "/" in obj and obj.lower().endswith((".jpg", ".png")))):
        out.append((path, obj[:120]))
    return out


def report(label: str, path: str, key: str, data, body_len: int, n_comments: int) -> bool:
    """찾은 게 있으면 True. 어떤 필드였는지까지 남긴다."""
    hits = long_strings(data, body_len)
    arrs = [a for a in arrays(data) if a[1] > 0]
    imgs = image_like(data)
    matching = [a for a in arrs if a[1] == n_comments]

    if not (hits or matching or imgs):
        return False

    print(f"\n  ★ {label}: POST {path} {{{key}: …}}")
    print(f"    응답 키: {list(data)[:15] if isinstance(data, dict) else type(data).__name__}")
    for p, n, s in hits[:5]:
        print(f"    · 목록({body_len}자)보다 긴 필드  {p} = {n}자 | {s}…")
    for p, n, first in matching[:3]:
        print(f"    · 댓글 수({n_comments})와 길이가 같은 배열  {p} → {json.dumps(first, ensure_ascii=False)[:200]}")
    for p, n, _ in arrs[:6]:
        if (p, n) not in [(m[0], m[1]) for m in matching]:
            print(f"    · 배열  {p} (길이 {n})")
    for p, u in imgs[:5]:
        print(f"    · 이미지로 보이는 값  {p} = {u}")
    return True


def main() -> None:
    ref = pick_reference()
    pid = ref.get("id")
    body_len = len(str(ref.get("c") or ""))
    n_comments = int(ref.get("rn") or 0)

    print("=" * 70)
    print(f"기준 글  id={pid}  제목={str(ref.get('at'))[:40]}")
    print(f"목록이 준 본문 길이 = {body_len}자   댓글 수(rn) = {n_comments}")
    print(f"목록 응답의 키 = {sorted(ref)}")
    print("=" * 70)

    found = False
    for label, paths in (("본문", DETAIL_PATHS), ("댓글", COMMENT_PATHS)):
        print(f"\n### {label} 후보")
        for path in paths:
            for key in ID_KEYS:
                status, data, raw = post(path, {"gid": GROUP_ID, key: pid, "wql": 100})
                if status != 200 or data is None:
                    continue
                if report(label, path, key, data, body_len, n_comments):
                    found = True
                else:
                    print(f"    200이지만 새 정보 없음: {path} {{{key}}} "
                          f"→ {list(data)[:10] if isinstance(data, dict) else data}")

    print("\n### 폴백 — 글 웹페이지")
    url = f"{BASE_URL}/{GROUP_ID}1/{pid}"
    try:
        r = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=TIMEOUT)
        print(f"  GET {url} → {r.status_code}, {len(r.text)}자")
        if body_len and str(ref.get("c") or "")[:40] in r.text:
            print("  ★ HTML에 본문 앞부분이 있습니다 — 스크래핑 폴백 가능")
    except Exception as e:  # noqa: BLE001
        print(f"  실패: {e}")

    if not found:
        print("\n후보가 전부 빗나갔습니다. 브라우저 개발자도구(F12 → Network)에서 "
              "글 하나를 열 때 나가는 /api/ 요청의 주소·페이로드를 확인해야 합니다.")


if __name__ == "__main__":
    main()
