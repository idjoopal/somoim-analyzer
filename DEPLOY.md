# 배포 가이드 — Streamlit Community Cloud

다감노 분석 앱을 **무료**로 배포하는 방법. 서버 비용 0원, main 브랜치에 push하면 자동 재배포.

## 사전 준비 (이미 완료)

| 항목 | 값 |
|---|---|
| GitHub repo | `idjoopal/somoim-analyzer` (푸시 완료) |
| 앱 진입점 | `streamlit_app.py` (repo 루트) |
| 의존성 | `requirements.txt` — streamlit, pandas, requests, openpyxl |

> 로컬 검증 완료: `pip install -r requirements.txt` 성공, 앱 기동 200, 라이브 수집(게시글 257 / 사진 471) 정상.

## 배포 단계 (약 3분, 본인 계정으로 진행)

1. https://share.streamlit.io 접속 → **Sign in** → 본인 GitHub 계정으로 로그인 및 권한 인가
2. 우측 상단 **Create app** 클릭
3. **Deploy a public app from GitHub** 선택
4. 항목 입력:
   - **Repository**: `idjoopal/somoim-analyzer`
   - **Branch**: `main`
   - **Main file path**: `streamlit_app.py`
   - **App URL**: 원하는 서브도메인 (예: `dagamno-analyzer`)
5. (선택) **Advanced settings** → Python version **3.11 이상**
6. **Deploy** 클릭 → 1~3분 빌드 후 `https://<서브도메인>.streamlit.app` 발급
7. 발급된 URL 북마크 → 운영진에게 공유

## 운영

- `main`에 push하면 **자동 재배포**.
- 앱 우측 하단 **Manage app**에서 빌드/런타임 로그 확인.
- 일정 시간 미접속 시 sleep → 다음 접속 때 수십 초 내 자동 기동.

## 문제 해결

| 증상 | 점검 |
|---|---|
| 빌드 실패 | Manage app 로그에서 `requirements.txt` 설치 오류 확인 |
| 수집 0건 / 에러 | somoim API 응답 구조 변경 가능성 — `PLAN.md` "운영 시 주의사항" 참고 |
| 느린 첫 로딩 | 라이브 수집은 ~40초 소요. 동일 입력은 1시간 캐시(`@st.cache_data`)로 즉시 반환 |
| 메모리 초과 | Community Cloud 1GB. 현재 데이터 규모에선 여유 |

## 비고

- 실제 **Deploy 클릭은 본인 Streamlit/GitHub 계정**으로만 가능 (외부 대행 불가, 별도 배포 API 없음).
- 백업 경로(Colab 노트북)는 `notebook.ipynb` 작성 후 추가 예정.
