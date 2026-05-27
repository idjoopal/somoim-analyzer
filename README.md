# 다감노📸 소모임 분석 도구

다감노 소모임의 게시글·사진을 매월 수집해 통계 엑셀을 생성합니다.

## 빠른 시작

```bash
pip install -r requirements.txt

python -c "
from core import collect_posts, collect_photos, save_excel
posts  = collect_posts(year=2026)
photos = collect_photos(year=2026)
save_excel(posts, photos, year=2026)
"
```

특정 월만:
```python
collect_posts(year=2026, month=5)
```

## 다음 단계

상세 계획은 [PLAN.md](./PLAN.md) 참고.

- [ ] Streamlit 앱 작성 → Streamlit Cloud 배포
- [ ] Colab 노트북 작성 → GitHub 공유
- [ ] 사용 매뉴얼 작성

## 구조

```
somoim-analyzer/
├── PLAN.md                 # 프로젝트 계획 + 도메인 지식
├── README.md               # 이 파일
├── requirements.txt
├── core/
│   ├── collector.py        # 데이터 수집
│   └── excel_builder.py    # 엑셀 빌더
└── samples/
    └── 다감노_2026_분석.xlsx
```

## 핵심 동작 검증

수집된 결과 예시 (2026년 전체):
- 게시글 257개 (공지 137 / 후기 79 / 가입인사 41)
- 사진 471장 (그 중 댓글 있는 사진 96장 = 테마 참여 예상)
