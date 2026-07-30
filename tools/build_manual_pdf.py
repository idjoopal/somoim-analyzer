#!/usr/bin/env python3
"""`docs/사용설명서.md` → `docs/사용설명서.pdf`.

원본 PDF는 헤드리스 크로미움의 print-to-PDF로 뽑은 것이었는데 생성 스크립트가
없어서, 설명서를 고칠 때마다 PDF가 뒤처졌다. 그래서 **같은 양식이 나오도록**
스크립트로 고정한다 — A4, 나눔 글꼴, 표지 한 장, `n / N` 꼬리말, `##` 마다
새 페이지.

    pip install markdown
    python tools/build_manual_pdf.py

크로미움은 이 환경에 이미 깔린 것을 쓴다(`--print-to-pdf`로는 꼬리말을 못
넣어서 CDP로 `Page.printToPDF`를 부른다). 표준 라이브러리 밖의 의존성은
`markdown`과 `websockets` 둘뿐이고, 앱 실행에는 필요 없다.
"""
from __future__ import annotations

import asyncio
import base64
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import markdown  # type: ignore
import websockets  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "사용설명서.md"
OUT = ROOT / "docs" / "사용설명서.pdf"

GREEN = "#2f6f4f"          # 제목 밑줄·소제목
INK = "#1a1a1a"            # 본문
PANEL = "#f7f9f8"          # 표 머리·코드 블록 바탕
LINE = "#c9c9c9"           # 표 테두리

CSS = f"""
body {{
  font-family: "NanumBarunGothic", "Noto Sans KR", sans-serif;
  font-size: 15px; line-height: 1.8; color: {INK};
  margin: 0; word-break: keep-all;
}}

/* ── 표지 ────────────────────────────────────────────── */
.cover {{
  height: 60vh; display: flex; flex-direction: column;
  align-items: center; justify-content: center; text-align: center;
  page-break-after: always;
}}
.cover h1 {{
  font-size: 40px; font-weight: 700; line-height: 1.35;
  margin: 0 0 26px; border: 0; color: {INK};
}}
.cover .lead {{ font-size: 16px; font-weight: 700; margin: 0 0 16px; }}
.cover .note {{ font-size: 15px; color: #555; margin: 0; max-width: 80%; }}

/* ── 제목 ────────────────────────────────────────────── */
h2 {{
  font-size: 21px; font-weight: 700; color: {INK};
  margin: 36px 0 13px; padding-bottom: 9px;
  border-bottom: 2.2pt solid {GREEN};
  page-break-before: always; page-break-after: avoid;
}}
h3 {{
  font-size: 16px; font-weight: 700; color: {GREEN};
  margin: 22px 0 8px; page-break-after: avoid;
}}
h4 {{ font-size: 15px; font-weight: 700; margin: 18px 0 6px; }}

p {{ margin: 0 0 10px; }}
strong {{ font-weight: 700; }}

ul, ol {{ margin: 0 0 10px; padding-left: 22px; }}
li {{ margin-bottom: 4px; }}
li > ul, li > ol {{ margin-top: 4px; }}

hr {{ border: 0; border-top: 1px solid #d9d9d9; margin: 26px 0; }}

blockquote {{
  margin: 10px 0 12px; padding: 2px 0 2px 13px;
  border-left: 2.5pt solid #cfdcd6; color: #444;
}}
blockquote p {{ margin: 0 0 6px; }}
blockquote p:last-child {{ margin-bottom: 0; }}

/* ── 표 ──────────────────────────────────────────────── */
table {{
  border-collapse: collapse; width: 100%;
  margin: 6px 0 14px; font-size: 13px; line-height: 1.7;
  page-break-inside: avoid;
}}
th, td {{
  border: 1px solid {LINE}; padding: 6px 9px;
  text-align: left; vertical-align: top;
}}
th {{ background: {PANEL}; font-weight: 700; }}

/* ── 코드 ────────────────────────────────────────────── */
code {{
  font-family: "NanumGothicCoding", "DejaVu Sans Mono", monospace;
  font-size: 13px; background: #eef1f0; padding: 1px 4px; border-radius: 3px;
}}
pre {{
  background: {PANEL}; border-radius: 5px; padding: 14px 16px;
  margin: 6px 0 14px; page-break-inside: avoid;
}}
pre code {{
  background: none; padding: 0; font-size: 12.5px; line-height: 1.65;
  white-space: pre-wrap;
}}

a {{ color: {INK}; text-decoration: none; }}
"""

FOOTER = """
<div style="width:100%;font-family:'NanumBarunGothic',sans-serif;font-size:8px;
            color:#666;text-align:center;margin:0 0 3mm;">
  <span class="pageNumber"></span> / <span class="totalPages"></span>
</div>
"""


def split_front_matter(md: str) -> tuple[dict, str]:
    """맨 앞의 `# 제목` + 안내 문단을 표지로 떼어 낸다.

    표지는 본문과 조판이 전혀 달라서(가운데 정렬·큰 글씨) 마크다운 흐름에
    그대로 두면 규칙이 지저분해진다.
    """
    lines = md.splitlines()
    assert lines[0].startswith("# "), "첫 줄은 `# 제목`이어야 한다"
    end = next(i for i, ln in enumerate(lines) if ln.strip() == "---")
    head = [ln for ln in lines[1:end] if ln.strip()]
    lead = head[0].strip().strip("*")
    return {"title": lines[0][2:].strip(), "lead": lead,
            "note": " ".join(x.strip() for x in head[1:])}, \
        "\n".join(lines[end + 1:])


def cover_html(front: dict) -> str:
    # 이모지와 글자를 줄바꿈으로 나눈다 — 원본 표지가 두 줄이다.
    title = html.escape(front["title"]).replace(" 사용설명서", "<br>사용설명서")
    return (f'<div class="cover"><h1>{title}</h1>'
            f'<p class="lead">{html.escape(front["lead"])}</p>'
            f'<p class="note">{html.escape(front["note"])}</p></div>')


def build_html(md_text: str) -> str:
    front, body_md = split_front_matter(md_text)
    body = markdown.markdown(
        body_md, extensions=["tables", "fenced_code", "sane_lists"])
    # 첫 `##`(목차)까지 표지 뒤에서 또 페이지를 넘기면 빈 장이 생긴다.
    body = body.replace("<h2>", '<h2 style="page-break-before:auto">', 1)
    return (f'<meta charset="utf-8"><title>{html.escape(front["title"])}</title>'
            f"<style>{CSS}</style>{cover_html(front)}{body}")


def find_chromium() -> str:
    for cand in ("google-chrome", "chromium", "chromium-browser"):
        if path := shutil.which(cand):
            return path
    # 플레이라이트가 받아 둔 것(이 저장소의 개발 환경에 이미 있다)
    hits = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    if hits:
        return hits[-1]
    sys.exit("크로미움을 찾지 못했습니다.")


async def _print(ws_url: str, page_url: str) -> bytes:
    async with websockets.connect(ws_url, max_size=None) as ws:
        async def call(method: str, params: dict | None = None,
                       _id=[0]) -> dict:
            _id[0] += 1
            await ws.send(json.dumps(
                {"id": _id[0], "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == _id[0]:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg["result"]

        await call("Page.enable")
        await call("Page.navigate", {"url": page_url})
        # 글꼴·레이아웃이 자리 잡을 시간. loadEventFired를 기다려도 되지만
        # 로컬 파일 한 장이라 이쪽이 짧고 확실하다.
        await asyncio.sleep(2.5)
        res = await call("Page.printToPDF", {
            "paperWidth": 8.2767, "paperHeight": 11.7067,   # 원본과 같은 A4 치수
            "marginTop": 0.55, "marginBottom": 0.5,
            "marginLeft": 0.63, "marginRight": 0.55,
            "printBackground": True,
            "displayHeaderFooter": True,
            "headerTemplate": "<div></div>", "footerTemplate": FOOTER,
        })
        return base64.b64decode(res["data"])


def render(html_text: str) -> bytes:
    # 크로미움이 종료 직후에도 프로필 폴더를 만지작거려서, 지우다 실패하는
    # 것으로 PDF를 통째로 날리지 않게 한다.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "manual.html"
        page.write_text(html_text, encoding="utf-8")
        profile = Path(tmp) / "profile"
        proc = subprocess.Popen(
            [find_chromium(), "--headless=new", "--disable-gpu",
             "--no-sandbox", "--remote-debugging-port=0",
             f"--user-data-dir={profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            port = _wait_for_port(proc)
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list") as r:
                targets = json.load(r)
            ws_url = next(t["webSocketDebuggerUrl"] for t in targets
                          if t["type"] == "page")
            return asyncio.run(_print(ws_url, page.as_uri()))
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def _wait_for_port(proc: subprocess.Popen) -> int:
    """크로미움이 stderr로 흘리는 디버깅 주소에서 포트를 읽는다."""
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stderr.readline().decode("utf-8", "replace")
        if m := re.search(r"ws://127\.0\.0\.1:(\d+)/", line):
            return int(m.group(1))
        if proc.poll() is not None:
            sys.exit("크로미움이 바로 종료됐습니다.")
    sys.exit("크로미움 디버깅 포트를 찾지 못했습니다.")


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    pdf = render(build_html(SRC.read_text(encoding="utf-8")))
    out.write_bytes(pdf)
    print(f"{out} ({len(pdf):,} bytes)")


if __name__ == "__main__":
    os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")
    main()
