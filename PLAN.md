# 다감노📸 소모임 분석 도구 — 프로젝트 계획

## 🎯 프로젝트 목표

다감노 소모임의 게시글/사진을 매월 수집해 통계 엑셀을 자동 생성하는 도구.

- **주 사용자**: 비개발자 (모임 운영진)
- **사용 빈도**: 월 1회
- **요구사항**: 개발지식 불필요 / 서버비용 0 / 년도·월 입력만으로 동작
- **배포 방식**: Streamlit Cloud (메인) + Google Colab (백업)

---

## ✅ 완성된 부분

| 파일 | 역할 | 상태 |
|---|---|---|
| `core/collector.py` | 게시글·사진 데이터 수집 | ✅ 완료, 파라미터화됨 |
| `core/excel_builder.py` | 엑셀 파일 생성 (8개 시트) | ✅ 완료, 메모리 bytes 반환 |
| `requirements.txt` | 파이썬 의존성 | ✅ 완료 |

로컬에서 동작 검증 완료. 다감노 모임 기준 게시글 257개, 사진 471개 수집 확인.

---

## 🚧 TODO — Claude Code 작업 항목

### Task 1. Streamlit 앱 작성 (`streamlit_app.py`)

**UI 요구사항:**
- 제목: "📸 다감노 2026 분석"
- 입력 영역
  - 년도 선택 드롭다운 (기본: 현재 년도)
  - 월 선택 체크박스 + 드롭다운 ("월 단위 분석" 토글, 켜면 월 선택 활성화)
- 실행 버튼: "분석 시작"
- 진행 표시: `st.progress()` + `st.status()` (수집 진행 단계별 로그)
- 결과 미리보기
  - KPI 카드 6개: 게시글/진행/취소/후기/사진/테마예상
  - 사용자 랭킹 표 (게시글 TOP 10)
  - 월별 활동 차트
- 다운로드 버튼: 엑셀 파일 다운로드 (`st.download_button`)
- 캐싱: `@st.cache_data(ttl=3600)` 1시간 동일 입력 캐시

**구현 가이드:**
```python
from core.collector import collect_posts, collect_photos
from core.excel_builder import build_excel

# 진행 콜백 연결
def on_progress(msg, pct):
    progress_bar.progress(pct)
    status.write(msg)

posts  = collect_posts(year=year, month=month, progress=on_progress)
photos = collect_photos(year=year, month=month, progress=on_progress)
xlsx_bytes = build_excel(posts, photos, year=year, month=month)

st.download_button("📥 엑셀 다운로드", xlsx_bytes, file_name=f"다감노_{year}.xlsx")
```

### Task 2. Colab 노트북 작성 (`notebook.ipynb`)

**셀 구성:**
1. **마크다운 셀**: 사용법 (3줄 정도)
2. **설치 셀**: `!pip install openpyxl requests -q`
3. **입력 위젯 셀**: `ipywidgets.IntText`로 년도/월 입력
4. **실행 셀**: collector + builder 호출, `tqdm` 진행바
5. **결과 셀**: `pandas` DataFrame으로 미리보기 + `files.download()` 자동 다운로드

**구현 가이드:**
- 코드는 한 셀 안에서 import + 실행 (사용자 "런타임 → 모두 실행" 한 번이면 끝)
- core/ 모듈을 `%%writefile`로 셀에 인라인하거나 GitHub raw에서 `!wget`
- 또는 모듈을 직접 노트북 셀에 펼쳐 넣는 방식 (단일 파일 노트북)

### Task 3. 배포 가이드 (`DEPLOY.md`)

**Streamlit Cloud 배포:**
1. GitHub repo 생성 → 이 폴더 푸시
2. share.streamlit.io 가입 (GitHub 로그인)
3. New app → repo/branch/main file 선택
4. 배포 완료 후 URL 북마크

**Colab 공유:**
1. 노트북을 GitHub repo에 푸시
2. `https://colab.research.google.com/github/<user>/<repo>/blob/main/notebook.ipynb` 형식 URL 생성
3. 또는 노트북 상단 "공유" → 링크 복사

### Task 4. 사용 매뉴얼 (`USAGE.md`)

비개발자 대상 1페이지 매뉴얼. 스크린샷 포함.

---

## 📚 핵심 도메인 지식 (반드시 숙지)

### 소모임 API 구조

**1) 게시글 — `POST https://www.somoim.co.kr/api/articles`**

요청:
```json
{ "gid": "<group_id>", "wql": 20, "s_t": <cursor> }
```

응답:
```json
{
  "res": 100,
  "cs": [ /* 게시글 배열 */ ],
  "s_t": <next_cursor>,
  "eof": "Y" or null
}
```

게시글 객체:
- `id` 게시글 ID
- `wn` 작성자 닉네임
- `wid` 작성자 UUID
- `at` 제목 (article title)
- `c` 본문 (content)
- `cat` **유형**: `"A"`=공지/출사공지, `"E"`=후기, `"J"`=가입인사
- `w_t` 작성 시각 (공지 핀고정시 `2000000000`)
- `ot` 정렬 시각 (페이지네이션 커서)
- `lc` 좋아요 수
- `rn` 댓글 수
- `ic` 이미지 수

**2) 사진 — `POST https://www.somoim.co.kr/api/photos`**

요청 구조 동일. 응답 키만 `cs` → `ps`로 다름.

사진 객체:
- `id` 사진 ID
- `wn` 작성자, `wid` 작성자 UUID
- `w_t` 업로드 시각
- `lc` 좋아요 수
- `rn` **댓글 수** (내용은 ❌, 개수만 ✅)

사진 다운로드 URL (CDN):
- 고화질: `https://d3vo2hyhx9t76k.cloudfront.net/{photo_id}.png`
- 중간:   `https://d3vo2hyhx9t76k.cloudfront.net/{photo_id}m.png`
- 작은:   `https://d3vo2hyhx9t76k.cloudfront.net/{photo_id}s.png`
- 썸네일: `https://d3vo2hyhx9t76k.cloudfront.net/{photo_id}n.png`

### 인증 한계

- ✅ **익명 접근 가능**: 게시글 목록, 사진 목록, 좋아요/댓글 **개수**, 사진 다운로드 URL
- ❌ **인증 필요 (불가)**: 댓글 **내용** — `/api/comments`는 500 에러
- 결정: 댓글 내용 분석은 포기, **댓글 유무**(`rn > 0`)만 활용 → "테마 참여 예상" 표시

### 타임스탬프 변환

```python
unix_ts = w_t (또는 ot) + 1_000_000_000
```
경험적으로 발견한 epoch offset. 모든 시간 필드에 동일 적용.

### 비즈니스 룰

**1) 다감노 카테고리 분류** (제목의 `[XXX]` 태그):
- **출사**: 인물, 인풍, 풍경, 1:1인물, 1:1인물출사
- **비출사 활동**: 보정, GN, 문화

**2) 취소 표시**: 제목에 `(펑)` 또는 `[펑]` → `is_canceled`

**3) 출사일 추론 규칙** (cat=A 공지글):
- 내용에 `출사진행날짜 : YY.MM.DD` 명시 → 그대로 사용
- 제목에 `YYYY.MM.DD` 명시 → 그대로 사용
- 제목에 `MM.DD` 또는 `MM/DD` 또는 `N월 M일` (연도 없음) → 작성일 기반 추론
  - 같은 해 → 다음 해 순서로 시도
  - **출사일 ≥ 작성일** AND **(출사일 − 작성일) < 365일**

**4) 필터링 (TARGET_YEAR 기준)**:
- `cat=A` 공지글: **출사일** 기준 (작성일은 2025년이어도 출사가 2026년이면 포함)
- `cat=E` 후기, `cat=J` 가입인사: **작성일** 기준

**5) 사진 "테마 참여 예상" 판정**: `rn > 0` (댓글 있음) — 댓글 내용은 못 보지만, 댓글이 달렸다는 것 자체가 테마 이벤트 참여 신호로 추정

### 다감노 모임 식별

- 모임 페이지 URL: `https://www.somoim.co.kr/2d4b415a-d2f4-11eb-97b4-0a0d8e52bd411`
- `GROUP_ID` = `"2d4b415a-d2f4-11eb-97b4-0a0d8e52bd411"` (URL 마지막 `1` 포함)
- 이 프로젝트는 **다감노 한 곳만** 분석 (다른 모임 지원 X)

---

## 📁 파일 구조

```
somoim-analyzer/
├── PLAN.md                 ← 이 파일
├── README.md               ← 프로젝트 한줄 소개
├── requirements.txt        ← Python 의존성
│
├── core/
│   ├── __init__.py
│   ├── collector.py        ← 데이터 수집 (✅ 완성)
│   └── excel_builder.py    ← 엑셀 빌더 (✅ 완성)
│
├── streamlit_app.py        ← TODO: Streamlit 앱
├── notebook.ipynb          ← TODO: Colab 노트북
├── DEPLOY.md               ← TODO: 배포 가이드
├── USAGE.md                ← TODO: 사용 매뉴얼
│
└── samples/
    └── 다감노_2026_분석.xlsx ← 결과 예시
```

---

## 🧪 빠른 검증 방법

새 컴퓨터에서 처음 돌릴 때:

```bash
pip install -r requirements.txt
python -c "
from core.collector import collect_posts, collect_photos
from core.excel_builder import build_excel

posts  = collect_posts(year=2026)
photos = collect_photos(year=2026)
xlsx   = build_excel(posts, photos, year=2026)

with open('test.xlsx', 'wb') as f:
    f.write(xlsx)
print(f'게시글 {len(posts)}, 사진 {len(photos)}, 엑셀 {len(xlsx)} bytes')
"
```

기대 결과: 게시글 ~257개, 사진 ~471개, 엑셀 ~50KB 이상.

---

## ⚠️ 운영 시 주의사항

1. **소모임 API 변경 위험**: 비공식 사용이므로 언제든 응답 구조나 인증 정책이 바뀔 수 있음. 정기적으로 동작 확인 필요.
2. **CDN URL 만료 가능성**: 현재는 무기한이지만 향후 만료 정책 도입 가능. 다운로드 즉시 보관 권장.
3. **타임스탬프 epoch offset**: `1_000_000_000`은 실측 추정값. 큰 폭의 오차가 발견되면 재추정 필요.
4. **Streamlit Cloud 정책**: 무료 티어가 변경/종료될 수 있음 → Colab 백업 권장.
5. **이용약관**: 자동화된 데이터 수집이 소모임 이용약관과 충돌할 수 있음 → 개인 분석 용도로만 사용.
