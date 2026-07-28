"""상세 조회 테스트 — 네트워크 무관 (`fetch`를 주입).

목록 API가 본문을 잘라 주는 탓에 참석자·출사일이 절반만 보고 판단되고 있었다.
여기서 검증하는 것은 **무엇을 다시 받지 않는가**와 **실패가 번지지 않는가**다.
둘 다 틀리면 수집이 몇 배로 느려지거나 통째로 죽는다.
"""

from datetime import datetime

from core.collector import enrich_posts


def post(pid, cat="E", body="잘린 본문…", **kw):
    base = {"id": pid, "cat": cat, "body": body, "title": f"글 {pid}"}
    base.update(kw)
    return base


def detail(body="전문 본문입니다 " * 5, images=None, comments=None):
    return {"body": body, "image_urls": images or [], "comments": comments or []}


class Fetcher:
    """호출된 id를 기록하는 가짜 — 무엇을 받았는지가 검증 대상이다."""

    def __init__(self, by_id=None, fail_on=()):
        self.by_id, self.fail_on = by_id or {}, set(fail_on)
        self.called = []

    def __call__(self, pid):
        self.called.append(pid)
        if pid in self.fail_on:
            raise RuntimeError("상세 조회 실패")
        return self.by_id.get(pid, detail())


# ═══════════════════════════════════════════════════════════════
# 무엇을 받고 무엇을 건너뛰는가
# ═══════════════════════════════════════════════════════════════

def test_only_notices_and_reviews_are_fetched():
    """참석자는 후기에서, 출사일은 공지에서만 뽑는다 — 나머지는 받을 이유가 없다."""
    posts = [post("a", "A"), post("e", "E"), post("j", "J"), post("x", "")]
    f = Fetcher()
    enrich_posts(posts, f, workers=1)
    assert sorted(f.called) == ["a", "e"]


def test_already_fetched_posts_are_not_fetched_again():
    """raw는 id 기준 upsert다 — 한 번 받아 두면 재수집 때 다시 받을 이유가 없다."""
    posts = [post("a"), post("b")]
    f = Fetcher()
    enrich_posts(posts, f, known_full={"a"}, workers=1)
    assert f.called == ["b"]


def test_detail_at_on_the_record_also_skips():
    """시트에서 읽어 온 글은 detail_at을 달고 온다."""
    posts = [post("a", detail_at="2026-07-28 10:00:00"), post("b")]
    f = Fetcher()
    enrich_posts(posts, f, workers=1)
    assert f.called == ["b"]


def test_nothing_to_do_makes_no_calls():
    posts = [post("a", detail_at="2026-07-28 10:00:00")]
    f = Fetcher()
    assert enrich_posts(posts, f, workers=1) == (0, [])
    assert f.called == []


# ═══════════════════════════════════════════════════════════════
# 받아 온 것을 어떻게 넣는가
# ═══════════════════════════════════════════════════════════════

def test_full_body_replaces_the_truncated_one():
    posts = [post("a", body="앞부분만…")]
    filled, _ = enrich_posts(posts, Fetcher({"a": detail("전문 전체 내용")}), workers=1)
    assert filled == 1
    assert posts[0]["body"] == "전문 전체 내용"
    assert posts[0]["detail_at"]


def test_shorter_body_never_overwrites_a_longer_one():
    """상세가 더 짧게 오면 그건 우리가 이미 가진 것보다 나쁜 데이터다."""
    posts = [post("a", body="이미 갖고 있는 긴 본문입니다")]
    enrich_posts(posts, Fetcher({"a": detail("짧음")}), workers=1)
    assert posts[0]["body"] == "이미 갖고 있는 긴 본문입니다"
    assert posts[0]["detail_at"]                  # 그래도 다시 받지는 않는다


def test_images_are_joined_not_mixed_into_the_photo_board():
    posts = [post("a")]
    enrich_posts(posts, Fetcher({"a": detail(images=["u1", " u2 ", ""])}), workers=1)
    assert posts[0]["image_urls"] == "u1, u2"


def test_comments_carry_the_post_id():
    """post_id가 없으면 어느 글의 댓글인지 잃는다 — 이게 유일한 연결고리다."""
    posts = [post("a")]
    _, comments = enrich_posts(posts, Fetcher({"a": detail(comments=[
        {"id": "c1", "author": "정원석", "body": "참석합니다"},
        {"id": "c2", "author": "나무", "body": "저도요"},
    ])}), workers=1)
    assert [c["post_id"] for c in comments] == ["a", "a"]
    assert [c["id"] for c in comments] == ["c1", "c2"]
    assert comments[0]["body"] == "참석합니다"


def test_comments_without_an_id_are_dropped():
    """id가 없으면 upsert 키가 없어 재수집마다 중복이 쌓인다."""
    posts = [post("a")]
    _, comments = enrich_posts(posts, Fetcher({"a": detail(comments=[
        {"author": "익명", "body": "키 없음"}, {"id": "c1", "body": "정상"},
    ])}), workers=1)
    assert [c["id"] for c in comments] == ["c1"]


# ═══════════════════════════════════════════════════════════════
# 실패가 번지지 않는가 — 수백 건 중 하나가 죽어도 수집은 끝나야 한다
# ═══════════════════════════════════════════════════════════════

def test_one_failure_does_not_stop_the_rest():
    posts = [post("a"), post("b"), post("c")]
    filled, _ = enrich_posts(posts, Fetcher(fail_on={"b"}), workers=1)
    assert filled == 2


def test_failed_post_keeps_its_truncated_body_and_stays_retryable():
    """detail_at을 찍어 버리면 다음 수집에서 영영 다시 안 받는다."""
    posts = [post("b", body="잘린 채로 남음")]
    enrich_posts(posts, Fetcher(fail_on={"b"}), workers=1)
    assert posts[0]["body"] == "잘린 채로 남음"
    assert "detail_at" not in posts[0]


def test_timestamp_is_injectable_for_deterministic_tests():
    posts = [post("a")]
    enrich_posts(posts, Fetcher(), now=datetime(2026, 7, 28, 9, 0), workers=1)
    assert posts[0]["detail_at"] == "2026-07-28 09:00:00"
