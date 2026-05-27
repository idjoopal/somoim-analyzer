"""
다감노📸 소모임 분석 — Streamlit 앱

년/월을 선택하고 "분석 시작"을 누르면 somoim API에서 게시글·사진을 수집해
KPI·랭킹·월별 차트를 보여주고, 통계 엑셀을 다운로드한다.

실행:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import streamlit as st

from core.collector import collect_posts, collect_photos, GROUP_NAME
from core.excel_builder import build_excel


# ═══════════════════════════════════════════════════════════════
# 순수 계산 헬퍼 (Streamlit 비의존 — 단독 테스트 가능)
# ═══════════════════════════════════════════════════════════════

def compute_kpis(posts: list[dict], photos: list[dict]) -> dict[str, int]:
    """대시보드 KPI 6종. excel_builder의 분류 규칙과 동일."""
    posts_A = [p for p in posts if p["cat"] == "A"]
    return {
        "전체 게시글": len(posts),
        "진행 출사":  sum(1 for p in posts_A if not p["is_canceled"]),
        "취소 출사":  sum(1 for p in posts_A if p["is_canceled"]),
        "후기글":     sum(1 for p in posts if p["cat"] == "E"),
        "사진 업로드": len(photos),
        "테마 예상":  sum(1 for p in photos if p["has_comment"]),
    }


def monthly_table(posts: list[dict], photos: list[dict]) -> dict[str, list[int]]:
    """1~12월 12칸 시리즈 5종. 공지는 출사일 기준, 그 외는 작성일 기준."""
    posts_A  = [p for p in posts if p["cat"] == "A"]
    active   = [p for p in posts_A if not p["is_canceled"]]
    canceled = [p for p in posts_A if p["is_canceled"]]
    reviews  = [p for p in posts if p["cat"] == "E"]
    themed   = [p for p in photos if p["has_comment"]]

    def by_outing(items: list[dict]) -> list[int]:
        out = [0] * 12
        for x in items:
            if x["outing_date"]:
                out[date.fromisoformat(x["outing_date"]).month - 1] += 1
        return out

    def by_posted(items: list[dict]) -> list[int]:
        out = [0] * 12
        for x in items:
            out[x["posted_at"].month - 1] += 1
        return out

    return {
        "진행 출사": by_outing(active),
        "취소 출사": by_outing(canceled),
        "후기글":   by_posted(reviews),
        "사진":     by_posted(photos),
        "테마 예상": by_posted(themed),
    }


def top_posters(posts: list[dict], n: int = 10) -> list[dict]:
    """작성자별 게시글 합계 내림차순 TOP n."""
    agg: dict[str, dict] = {}
    for p in posts:
        s = agg.setdefault(p["author"], {
            "작성자": p["author"], "게시글": 0, "공지": 0, "후기": 0, "좋아요": 0,
        })
        s["게시글"] += 1
        if p["cat"] == "A":
            s["공지"] += 1
        elif p["cat"] == "E":
            s["후기"] += 1
        s["좋아요"] += p["likes"]
    return sorted(agg.values(), key=lambda x: -x["게시글"])[:n]


# ═══════════════════════════════════════════════════════════════
# 수집 파이프라인 (캐시)
# ═══════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600, show_spinner=False)
def run_analysis(year: int, month: int | None, _progress=None):
    """수집 + 엑셀 생성. _progress(밑줄)는 캐시 키에서 제외된다."""
    posts  = collect_posts(year, month, progress=_progress)
    photos = collect_photos(year, month, progress=_progress)
    xlsx   = build_excel(posts, photos, year, month)
    return posts, photos, xlsx


# ═══════════════════════════════════════════════════════════════
# 결과 렌더링
# ═══════════════════════════════════════════════════════════════

def render_results(year: int, month: int | None, posts: list[dict],
                   photos: list[dict], xlsx: bytes) -> None:
    period = f"{year}년" + (f" {month}월" if month else " 전체")
    st.subheader(f"📊 {period} 결과")

    kpis = compute_kpis(posts, photos)
    for col, (label, val) in zip(st.columns(len(kpis)), kpis.items()):
        col.metric(label, val)

    fname_period = f"{year}" + (f"_{month:02d}" if month else "")
    st.download_button(
        "📥 엑셀 다운로드",
        data=xlsx,
        file_name=f"다감노_{fname_period}_분석.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )

    st.markdown("#### 👤 게시글 TOP 10")
    top = top_posters(posts, 10)
    if top:
        df = pd.DataFrame(top)
        df.insert(0, "순위", range(1, len(df) + 1))
        st.dataframe(df, hide_index=True, use_container_width=True)
    else:
        st.info("해당 기간 게시글이 없습니다.")

    st.markdown("#### 📅 월별 활동")
    chart_df = pd.DataFrame(
        monthly_table(posts, photos),
        index=[f"{m}월" for m in range(1, 13)],
    )
    st.bar_chart(chart_df)


# ═══════════════════════════════════════════════════════════════
# 메인 UI
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    st.set_page_config(page_title="다감노 분석", page_icon="📸", layout="wide")
    st.title("📸 다감노 분석")
    st.caption(f"{GROUP_NAME} 게시글·사진을 수집해 통계 엑셀을 생성합니다.")

    current_year = datetime.now().year
    c_year, c_month = st.columns([1, 2])
    with c_year:
        year = st.selectbox("년도", list(range(current_year, current_year - 6, -1)))
    with c_month:
        month = None
        if st.checkbox("월 단위 분석"):
            month = st.selectbox("월", list(range(1, 13)))

    if st.button("분석 시작", type="primary"):
        progress_bar = st.progress(0.0, text="시작 준비 중…")
        with st.status("데이터 수집 중…", expanded=True) as status:
            def on_progress(msg: str, pct: float) -> None:
                progress_bar.progress(min(max(pct, 0.0), 1.0), text=msg)
                st.write(msg)

            try:
                posts, photos, xlsx = run_analysis(int(year), month, _progress=on_progress)
            except Exception as e:  # noqa: BLE001 — 사용자에게 그대로 노출
                status.update(label="수집 실패", state="error")
                st.error("수집 중 오류가 발생했습니다. (somoim API 응답/네트워크 확인)")
                st.exception(e)
                st.stop()

            progress_bar.progress(1.0, text="완료")
            status.update(
                label=f"완료 · 게시글 {len(posts)} / 사진 {len(photos)}",
                state="complete",
            )
        # 다운로드 클릭 시 rerun 되어도 결과가 유지되도록 세션에 보관
        st.session_state["result"] = (int(year), month, posts, photos, xlsx)

    if "result" in st.session_state:
        render_results(*st.session_state["result"])


if __name__ == "__main__":
    main()
