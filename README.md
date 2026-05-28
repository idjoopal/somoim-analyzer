# 다감노📸 소모임 분석

[소모임](https://www.somoim.co.kr) **다감노** 모임의 게시글·사진을 한 해(또는 특정 월) 단위로 모아 출사·사진·테마 활동 인사이트를 보여주고, 종합 통계 엑셀을 내려받는 웹 앱입니다. 년/월만 고르면 되고 개발 지식은 필요 없습니다.

## 주요 기능

- **자동 수집 또는 JSON 번들 업로드** — somoim API에서 모으거나, 이전에 받은 번들을 올리면 API 호출 없이 즉시 분석.
- **3단계 보정 워크플로** — ① 멤버 마스터 확정 → ② 분류·참석자 보정 → ③ 인사이트. 모든 보정은 `st.data_editor`로 직접 편집.
- **인사이트 대시보드** (탭 구성)
  - 📊 **개요** — 진행/취소·카테고리 도넛, 월별 활동 추이, 핵심 숫자
  - 📌 **출사** — 월별 공지, 작성자 순위, 취소(펑) 순위, 공지 목록
  - 👥 **참석** — 후기 본문 기반 실제 참석자 매칭, 멤버별/월별 매트릭스
  - 📷 **사진** — 업로드 순위, 월별 업로드, 인기 사진 갤러리
  - 🎨 **테마사진** — 월별 제출 인원 + 그 달 사진 미리보기, 매트릭스, 참여자 순위
  - 🏷️ 카테고리 · 👤 사용자 · 📋 데이터(원본 표 + CSV)
- **엑셀 + JSON 번들 다운로드** — 11개 시트 통계 엑셀과, 다음 세션에 재사용할 수 있는 JSON 번들(원본 + 확정 마스터).

## 사용법

### 배포된 앱
사이드바에서 **API 수집** → 년/월 → **분석 시작**(첫 수집 ~40초), 또는 이전에 받은 **JSON 번들**을 업로드. 본문에서 ① 마스터 확정 → ② 분류·참석자 보정 → ③ 인사이트. 사이드바에서 **JSON 번들 / 엑셀** 다운로드.

> JSON 번들을 보관하면 다음에 API 호출 없이 같은 데이터로 즉시 분석/공유할 수 있습니다.

### 로컬 실행
```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

### 코드로 직접 (선택)
```python
from core import collect_posts, collect_photos, save_excel

posts  = collect_posts(year=2026)      # 특정 월만: month=5
photos = collect_photos(year=2026)
save_excel(posts, photos, year=2026)   # → 다감노_2026_분석.xlsx 저장
```

## 배포 (Streamlit Community Cloud · 무료)

서버 비용 0원이며 `main`에 push하면 자동 재배포됩니다. **Deploy 클릭은 본인 GitHub/Streamlit 계정**으로 진행해야 합니다(대행 불가).

1. https://share.streamlit.io → **Sign in** (GitHub 로그인·권한 인가)
2. **Create app** → **Deploy a public app from GitHub**
3. 입력
   - Repository: `idjoopal/somoim-analyzer`
   - Branch: `main`
   - Main file path: `streamlit_app.py`
   - App URL: 원하는 서브도메인
4. (선택) Advanced settings → Python 3.11 이상
5. **Deploy** → 1~3분 빌드 후 `https://<서브도메인>.streamlit.app` 발급 → 공유

운영 중에는 앱 우측 하단 **Manage app**에서 빌드·런타임 로그를 볼 수 있습니다. 일정 시간 미접속 시 sleep 되고, 다음 접속 때 수십 초 내 자동 기동됩니다.

## 집계 기준

- **기간** — 출사 공지(`cat=A`)는 **출사일** 기준, 후기·가입인사·사진은 **작성일** 기준.
- **카테고리** (제목 `[태그]`) — 출사: 인물(1:1인물·1:1인물출사 포함)·인물&풍경(구 인풍)·풍경·GN / 활동: 보정·문화.
- **취소(펑)** — 제목에 `(펑)`·`[펑]` 포함 시 취소로 집계.
- **테마사진** — 댓글이 달린 사진(`rn>0`)을 테마 참여로 **추정**합니다. 댓글 내용은 비공개라 개수만 활용하므로, 테마사진 탭의 월별 미리보기로 실제 여부를 눈으로 확인할 수 있습니다.
- **인기** — 좋아요 수로 정렬하고 댓글 수를 함께 표기.

## 저장소 구조

```
somoim-analyzer/
├── streamlit_app.py        # Streamlit 앱 (UI · 인사이트 · 분류 검토)
├── core/
│   ├── collector.py        # 소모임 데이터 수집·분류
│   └── excel_builder.py    # 11개 시트 엑셀 생성
├── requirements.txt        # streamlit · pandas · altair · requests · openpyxl
└── .streamlit/config.toml  # 테마
```

## 주의사항

- **비공식 API 기반** — 소모임 응답 구조나 인증 정책이 바뀌면 수집이 멈출 수 있어 정기적인 동작 확인이 필요합니다. 댓글 **내용**은 인증 한계로 가져오지 않습니다(개수만).
- **타임스탬프 보정값**(`+1_000_000_000`)은 실측 추정값이라, 큰 오차가 보이면 재추정이 필요합니다.
- **사진 CDN URL**은 현재 무기한이지만 만료될 수 있으니 필요한 사진은 즉시 보관을 권장합니다.
- 분석 대상은 **다감노 모임 한 곳**이며 **한 해 단위**입니다.
- **이용약관** — 자동 수집이 소모임 약관과 충돌할 수 있으므로 개인 분석 용도로만 사용하세요.
