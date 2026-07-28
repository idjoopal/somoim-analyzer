"""기간(YYYYMM) 헬퍼 단위 테스트 — 네트워크 무관.

이 함수들이 수집·집계·표시의 단일 진리원이라, 여기가 틀리면 2025-03과 2026-03이
한 칸에 합쳐지는 조용한 데이터 오염이 전 구간으로 번진다.
"""

from datetime import date, datetime

import pytest

from core.collector import (
    in_ym_range,
    is_multi_year,
    month_axis,
    period_label,
    period_tag,
    ym_add,
    ym_diff,
    ym_label,
    ym_of,
    ym_split,
    ym_valid,
)


# ═══════════════════════════════════════════════════════════════
# 변환·검증
# ═══════════════════════════════════════════════════════════════

def test_ym_of_from_date_and_datetime():
    assert ym_of(date(2026, 3, 15)) == 202603
    assert ym_of(datetime(2026, 3, 15, 21, 30)) == 202603
    assert ym_of(date(2025, 12, 31)) == 202512
    assert ym_of(date(2026, 1, 1)) == 202601


def test_ym_split():
    assert ym_split(202603) == (2026, 3)
    assert ym_split(202512) == (2025, 12)


def test_ym_valid():
    assert ym_valid(202601) and ym_valid(202612)
    assert not ym_valid(202600)      # 0월
    assert not ym_valid(202613)      # 13월
    assert not ym_valid("아무말")
    assert not ym_valid(None)


# ═══════════════════════════════════════════════════════════════
# 월 연산 — 연도 넘김이 핵심
# ═══════════════════════════════════════════════════════════════

def test_ym_add_crosses_year_boundary():
    assert ym_add(202601, -1) == 202512
    assert ym_add(202512, 1) == 202601
    assert ym_add(202601, -12) == 202501     # 수집 마진에서 쓰는 값
    assert ym_add(202603, 0) == 202603


def test_ym_add_multi_year_jumps():
    assert ym_add(202601, 25) == 202802
    assert ym_add(202601, -25) == 202312


def test_ym_add_lands_on_december_not_month_zero():
    """총월수 % 12 == 0 인 경계에서 0월이 나오지 않아야 한다."""
    for ymv in (202512, 202412, 202612):
        assert ym_valid(ym_add(ymv, 0))
        assert ym_valid(ym_add(ymv, 12))
        assert ym_valid(ym_add(ymv, -12))
    assert ym_add(202512, 12) == 202612
    assert ym_add(202512, -12) == 202412


def test_ym_diff():
    assert ym_diff(202603, 202601) == 2
    assert ym_diff(202601, 202512) == 1
    assert ym_diff(202601, 202601) == 0
    assert ym_diff(202512, 202601) == -1
    assert ym_diff(202612, 202501) == 23


def test_ym_add_and_diff_round_trip():
    for offset in range(-30, 31):
        assert ym_diff(ym_add(202601, offset), 202601) == offset


# ═══════════════════════════════════════════════════════════════
# 범위 판정 · 월 축
# ═══════════════════════════════════════════════════════════════

def test_in_ym_range_is_inclusive_on_both_ends():
    assert in_ym_range(202601, 202601, 202612)
    assert in_ym_range(202612, 202601, 202612)
    assert in_ym_range(202606, 202601, 202612)
    assert not in_ym_range(202512, 202601, 202612)
    assert not in_ym_range(202701, 202601, 202612)


def test_in_ym_range_single_month():
    assert in_ym_range(202605, 202605, 202605)
    assert not in_ym_range(202604, 202605, 202605)
    assert not in_ym_range(202606, 202605, 202605)


def test_month_axis_spans_year_boundary():
    assert month_axis(202511, 202602) == [202511, 202512, 202601, 202602]


def test_month_axis_single_month():
    assert month_axis(202605, 202605) == [202605]


def test_month_axis_full_year_has_12():
    axis = month_axis(202601, 202612)
    assert len(axis) == 12
    assert axis[0] == 202601 and axis[-1] == 202612


def test_month_axis_two_full_years_has_24_and_no_collisions():
    """다년 축에서 같은 '월'이 합쳐지지 않는지 — 이번 작업의 핵심 회귀."""
    axis = month_axis(202501, 202612)
    assert len(axis) == 24
    assert len(set(axis)) == 24
    assert 202503 in axis and 202603 in axis   # 두 3월이 별개로 존재


def test_month_axis_accepts_reversed_input():
    assert month_axis(202602, 202511) == month_axis(202511, 202602)


# ═══════════════════════════════════════════════════════════════
# 라벨
# ═══════════════════════════════════════════════════════════════

def test_is_multi_year():
    assert not is_multi_year(202601, 202612)
    assert not is_multi_year(202605, 202605)
    assert is_multi_year(202511, 202602)


def test_ym_label_includes_year_only_when_multi_year():
    assert ym_label(202603, multi_year=True) == "2026-03"
    assert ym_label(202603, multi_year=False) == "3월"
    assert ym_label(202612, multi_year=False) == "12월"


@pytest.mark.parametrize("start,end,expected", [
    (202601, 202612, "2026년 전체"),      # 한 해 전체 — 기존 표기 유지
    (202605, 202605, "2026년 5월"),       # 단일 월 — 기존 표기 유지
    (202603, 202608, "2026년 3~8월"),     # 한 해 안의 구간
    (202509, 202603, "2025-09 ~ 2026-03"),  # 다년
])
def test_period_label(start, end, expected):
    assert period_label(start, end) == expected


def test_period_tag():
    assert period_tag(202605, 202605) == "202605"
    assert period_tag(202509, 202603) == "202509-202603"
