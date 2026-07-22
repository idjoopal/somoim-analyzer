"""core/ledger.py 단위 테스트 — 전부 네트워크 무관 (GitHub API는 모킹)."""

import base64
import json

import pytest

from core.ledger import (
    GithubLedgerStore,
    LedgerError,
    apply_attendee_overrides,
    apply_post_overrides,
    dumps_ledger,
    empty_ledger,
    ledger_counts,
    merge_ledgers,
    normalize_ledger,
    update_attendee_override,
    update_name_resolutions,
    update_post_override,
)

OUTING_CATS = ["인물", "인물&풍경", "풍경", "GN"]


# ═══════════════════════════════════════════════════════════════
# 스키마 정돈
# ═══════════════════════════════════════════════════════════════

def test_empty_ledger_shape():
    led = empty_ledger()
    assert set(led) == {"version", "name_resolution", "post_overrides", "attendee_overrides"}
    assert ledger_counts(led) == {"이름": 0, "공지": 0, "참석": 0}


def test_normalize_rejects_garbage():
    assert normalize_ledger(None) == empty_ledger()
    assert normalize_ledger([1, 2]) == empty_ledger()
    assert normalize_ledger({"name_resolution": "oops", "post_overrides": 3}) == empty_ledger()


def test_normalize_cleans_entries():
    led = normalize_ledger({
        "name_resolution": {"철수": "철수닉", "": "x", "빈값": ""},
        "post_overrides": {
            "p1": {"category": "인물", "outing_date": "2026-05-03",
                   "is_canceled": 1, "excluded": 0},
            "p2": "broken",
        },
        "attendee_overrides": {"r1": ["영희", "  ", "철수 "], "r2": "broken"},
    })
    assert led["name_resolution"] == {"철수": "철수닉"}
    assert led["post_overrides"] == {"p1": {
        "category": "인물", "outing_date": "2026-05-03",
        "is_canceled": True, "excluded": False,
    }}
    assert led["attendee_overrides"] == {"r1": ["영희", "철수"]}


def test_dumps_roundtrip():
    led = empty_ledger()
    update_name_resolutions(led, {"철수": "철수닉"})
    update_post_override(led, "p1", {"category": "풍경", "outing_date": "2026-01-02"})
    update_attendee_override(led, "r1", ["영희"])
    assert normalize_ledger(json.loads(dumps_ledger(led))) == normalize_ledger(led)


def test_merge_ledgers_ours_wins():
    base = normalize_ledger({"name_resolution": {"철수": "옛닉", "영희": "영희닉"}})
    ours = normalize_ledger({"name_resolution": {"철수": "새닉"}})
    merged = merge_ledgers(base, ours)
    assert merged["name_resolution"] == {"철수": "새닉", "영희": "영희닉"}


# ═══════════════════════════════════════════════════════════════
# 보정 → 원장 반영
# ═══════════════════════════════════════════════════════════════

def test_update_name_resolutions_add_change_remove():
    led = empty_ledger()
    assert update_name_resolutions(led, {"철수": "철수닉"}) is True
    assert update_name_resolutions(led, {"철수": "철수닉"}) is False   # 동일값 → 무변경
    assert update_name_resolutions(led, {"철수": "새닉"}) is True
    assert update_name_resolutions(led, {"철수": None}) is True        # 삭제
    assert led["name_resolution"] == {}
    assert update_name_resolutions(led, {"없는이름": None}) is False


def test_update_post_override_add_same_remove():
    led = empty_ledger()
    ov = {"category": "인물", "outing_date": "2026-05-03", "is_canceled": False}
    assert update_post_override(led, "p1", ov) is True
    assert update_post_override(led, "p1", ov) is False               # 동일값 → 무변경
    assert led["post_overrides"]["p1"]["excluded"] is False           # 기본값 채움
    assert update_post_override(led, "p1", None) is True
    assert update_post_override(led, "p1", None) is False


def test_update_attendee_override_normalizes_and_removes():
    led = empty_ledger()
    assert update_attendee_override(led, "r1", ["영희 ", "", "철수"]) is True
    assert led["attendee_overrides"]["r1"] == ["영희", "철수"]
    assert update_attendee_override(led, "r1", ["영희", "철수"]) is False
    assert update_attendee_override(led, "r1", None) is True
    assert led["attendee_overrides"] == {}


# ═══════════════════════════════════════════════════════════════
# 원장 → 데이터 적용
# ═══════════════════════════════════════════════════════════════

def _notice(pid, **kw):
    p = {"id": pid, "cat": "A", "category": None, "is_outing": False,
         "is_canceled": False, "outing_date": None,
         "needs_review": True, "review_reason": "출사일 미상"}
    p.update(kw)
    return p


def test_apply_post_overrides_sets_fields_and_clears_review():
    led = empty_ledger()
    update_post_override(led, "p1", {"category": "풍경", "outing_date": "2026-03-01",
                                     "is_canceled": True})
    posts = [_notice("p1"), _notice("p2"), {"id": "p1", "cat": "E"}]
    n = apply_post_overrides(posts, led, OUTING_CATS)
    assert n == 1
    p1 = posts[0]
    assert (p1["category"], p1["outing_date"], p1["is_canceled"]) == ("풍경", "2026-03-01", True)
    assert p1["is_outing"] is True
    assert p1["needs_review"] is False and p1["review_reason"] == ""
    assert p1["ledger_applied"] is True
    assert posts[1]["needs_review"] is True          # 보정 없는 공지는 그대로
    assert "ledger_applied" not in posts[2]          # cat=E는 건드리지 않음


def test_apply_post_overrides_excluded_blanks_date():
    led = empty_ledger()
    update_post_override(led, "p1", {"category": "인물", "outing_date": "2026-03-01",
                                     "excluded": True})
    posts = [_notice("p1", outing_date="2026-03-01", needs_review=True)]
    apply_post_overrides(posts, led, OUTING_CATS)
    assert posts[0]["outing_date"] is None           # 분석 제외 확정
    assert posts[0]["needs_review"] is False


def test_apply_attendee_overrides_idempotent_with_auto_stash():
    led = empty_ledger()
    update_attendee_override(led, "r1", ["보정1", "보정2"])
    posts = [{"id": "r1", "cat": "E", "attendees": ["자동1"],
              "unresolved_names": ["미상"]},
             {"id": "r2", "cat": "E", "attendees": ["자동2"]}]
    assert apply_attendee_overrides(posts, led) == 1
    assert posts[0]["attendees"] == ["보정1", "보정2"]
    assert posts[0]["_auto_attendees"] == ["자동1"]   # 자동값 보관 (복귀 감지용)
    assert posts[0]["unresolved_names"] == []
    # 재적용해도 _auto_attendees가 override 값으로 덮이지 않음 (멱등)
    apply_attendee_overrides(posts, led)
    assert posts[0]["_auto_attendees"] == ["자동1"]
    assert posts[1]["attendees"] == ["자동2"]


# ═══════════════════════════════════════════════════════════════
# GithubLedgerStore (requests 모킹)
# ═══════════════════════════════════════════════════════════════

class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeRequests:
    """requests.get/put 대체 — 호출 기록 + 준비된 응답 순차 반환."""

    RequestException = Exception

    def __init__(self):
        self.get_responses: list[FakeResp] = []
        self.put_responses: list[FakeResp] = []
        self.put_bodies: list[dict] = []

    def get(self, url, **kw):
        return self.get_responses.pop(0)

    def put(self, url, json=None, **kw):
        self.put_bodies.append(json)
        return self.put_responses.pop(0)


@pytest.fixture
def fake_api(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr("core.ledger.requests", fake)
    return fake


def _content_resp(ledger, sha="abc123"):
    raw = dumps_ledger(ledger).encode("utf-8")
    return FakeResp(200, {"content": base64.b64encode(raw).decode(), "sha": sha})


def test_store_requires_owner_repo():
    with pytest.raises(LedgerError):
        GithubLedgerStore(token="t", repo="no-slash")
    with pytest.raises(LedgerError):
        GithubLedgerStore(token="", repo="a/b")


def test_store_load_missing_file_returns_empty(fake_api):
    fake_api.get_responses = [FakeResp(404)]
    store = GithubLedgerStore(token="t", repo="o/r")
    led, sha = store.load()
    assert led == empty_ledger() and sha is None


def test_store_load_parses_content(fake_api):
    led = empty_ledger()
    update_name_resolutions(led, {"철수": "철수닉"})
    fake_api.get_responses = [_content_resp(led, sha="s1")]
    got, sha = GithubLedgerStore(token="t", repo="o/r").load()
    assert got["name_resolution"] == {"철수": "철수닉"} and sha == "s1"


def test_store_load_corrupt_content_falls_back_to_empty(fake_api):
    fake_api.get_responses = [FakeResp(200, {
        "content": base64.b64encode(b"not json").decode(), "sha": "s1"})]
    got, sha = GithubLedgerStore(token="t", repo="o/r").load()
    assert got == empty_ledger() and sha == "s1"     # sha 유지 → 저장 시 복구 덮어쓰기


def test_store_load_auth_error_raises(fake_api):
    fake_api.get_responses = [FakeResp(401)]
    with pytest.raises(LedgerError, match="토큰"):
        GithubLedgerStore(token="t", repo="o/r").load()


def test_store_save_success_returns_new_sha(fake_api):
    fake_api.put_responses = [FakeResp(200, {"content": {"sha": "new1"}})]
    store = GithubLedgerStore(token="t", repo="o/r")
    led = empty_ledger()
    assert store.save(led, "old", "msg") == "new1"
    body = fake_api.put_bodies[0]
    assert body["sha"] == "old" and body["branch"] == "main"


def test_store_save_conflict_merges_and_retries(fake_api):
    # 1차 put: 409 충돌 → 원격 로드 → 병합 → 2차 put 성공
    remote = empty_ledger()
    update_name_resolutions(remote, {"영희": "영희닉"})
    fake_api.put_responses = [FakeResp(409), FakeResp(200, {"content": {"sha": "new2"}})]
    fake_api.get_responses = [_content_resp(remote, sha="remote-sha")]

    store = GithubLedgerStore(token="t", repo="o/r")
    ours = empty_ledger()
    update_name_resolutions(ours, {"철수": "철수닉"})
    assert store.save(ours, "stale-sha", "msg") == "new2"

    # 재시도 커밋에는 원격 sha + 병합 내용이 실림, ours에도 병합 결과 반영
    retry = fake_api.put_bodies[1]
    assert retry["sha"] == "remote-sha"
    merged = json.loads(base64.b64decode(retry["content"]))
    assert merged["name_resolution"] == {"철수": "철수닉", "영희": "영희닉"}
    assert ours["name_resolution"] == {"철수": "철수닉", "영희": "영희닉"}
