"""3단계 텍스트 추출: 원문 HTML 본문과 첨부 PDF에서 글자와 표를 구조째 꺼냄.

    python src/extract.py

입력: data/raw/crawl_state.json, data/raw/html/, data/raw/attachments.json, data/raw/attachments/
결과
- data/extracted/{doc_id}.json    문서별 블록 목록 (6단계 청킹 입력)
- data/extracted/{doc_id}.md      눈으로 확인하는 미리보기
- data/reports/extract_{날짜}.json  문서별 블록 수, 글자 수, 표, 못 읽은 이미지 등

블록 하나 = 소제목 경로(section_path) 아래의 문단 / 목록 / 표 / 이미지 / iframe.
목록은 최상위 항목(items)을 따로 저장해 청킹에서 항목별로 나눌 수 있게 한다.
못 읽은 이미지·iframe·빈 PDF 쪽도 버리지 않고 extraction_method "none"으로 남긴다.
개인정보 제거, 메뉴 정리 등은 4단계(clean.py)에서 한다.
"""
import base64
import hashlib
import os
import re
import sys
import warnings
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
from bs4 import BeautifulSoup, NavigableString, Tag

from common import (CRAWL_STATE, DATA, RAW_HTML, REPORTS, Fetcher, load_sources, now_iso, read_json,
                    require_env, today, write_json)

EXTRACTED = DATA / "extracted"
ATTACH_DIR = DATA / "raw" / "attachments"
ATTACH_STATE = DATA / "raw" / "attachments.json"
VLM_DIR = DATA / "raw" / "vlm"

HEADING_LEVEL = {"h2": 1, "h3": 1, "h4": 1, "h5": 2, "h6": 3}
INLINE = {"a", "span", "strong", "b", "em", "i", "u", "font", "sup", "sub", "small", "mark", "label", "abbr"}
DECORATIVE_IMG_ALT = {"바로가기 버튼"}
MIN_PDF_PAGE_CHARS = 20

warnings.filterwarnings("ignore")        # pdfplumber의 글꼴 경고


def norm(text: str) -> str:
    """줄 안의 공백은 하나로, 줄바꿈은 유지, 빈 줄 제거."""
    lines = [" ".join(line.split()) for line in text.replace("\xa0", " ").split("\n")]
    return "\n".join(line for line in lines if line)


def inline_text(el) -> str:
    """인라인 요소의 글자. <br>은 줄바꿈으로."""
    if isinstance(el, NavigableString):
        return str(el)
    parts = []
    for node in el.descendants:
        if isinstance(node, NavigableString):
            parts.append(str(node))
        elif node.name == "br":
            parts.append("\n")
    return "".join(parts)


# ── HTML ────────────────────────────────────────────────────────────

def list_items(ul: Tag, depth: int = 0) -> list[str]:
    """목록의 항목별 글자. 하위 목록은 '  - '로 들여 써서 부모 항목 글자에 붙인다."""
    items = []
    for li in ul.find_all("li", recursive=False):
        own, subs = [], []
        for child in li.children:
            if isinstance(child, Tag) and child.name in ("ul", "ol"):
                subs.extend(list_items(child, depth + 1))
            elif isinstance(child, Tag) and child.name in ("table",):
                subs.append("  " * (depth + 1) + "[표] " + " / ".join(" | ".join(r) for r in table_rows(child)))
            else:
                own.append(inline_text(child))
        line = "  " * depth + "- " + " ".join(norm("".join(own)).split("\n"))
        items.append("\n".join([line] + subs) if own or subs else line)
    return [i for i in items if i.strip("- \n")]


def table_rows(table: Tag) -> list[list[str]]:
    """표를 2차원 배열로. rowspan/colspan은 같은 값을 반복해 칸을 맞춘다."""
    grid: dict[tuple[int, int], str] = {}
    for r, tr in enumerate(table.find_all("tr")):
        c = 0
        for cell in tr.find_all(["th", "td"], recursive=False):
            while (r, c) in grid:
                c += 1
            text = " ".join(norm(inline_text(cell)).split("\n"))
            rs, cs = int(cell.get("rowspan", 1) or 1), int(cell.get("colspan", 1) or 1)
            for dr in range(rs):
                for dc in range(cs):
                    grid[(r + dr, c + dc)] = text
            c += cs
    if not grid:
        return []
    n_rows = max(r for r, _ in grid) + 1
    n_cols = max(c for _, c in grid) + 1
    return [[grid.get((r, c), "") for c in range(n_cols)] for r in range(n_rows)]


class HtmlExtractor:
    def __init__(self, page_url: str):
        self.page_url = page_url
        self.path: list[str] = []
        self.blocks: list[dict] = []
        self.buffer: list[str] = []

    def add(self, block: dict):
        self.blocks.append({"section_path": list(self.path)} | block)

    def flush(self):
        text = norm("".join(self.buffer))
        self.buffer = []
        if text:
            self.add({"type": "paragraph", "text": text, "extraction_method": "html"})

    @staticmethod
    def is_sentence(el: Tag) -> bool:
        text = " ".join(norm(inline_text(el)).split("\n")).strip()
        return len(text) > 30 or text.endswith(("다.", "다"))

    def heading(self, el: Tag):
        self.flush()
        level = HEADING_LEVEL[el.name]
        title = " ".join(norm(inline_text(el)).split("\n"))
        if title:
            self.path = self.path[:level - 1] + [title]

    def image(self, img: Tag):
        alt = (img.get("alt") or "").strip()
        if alt in DECORATIVE_IMG_ALT:
            return
        self.flush()
        self.add({"type": "image", "text": None, "alt": alt or None,
                  "src": urljoin(self.page_url, img.get("src", "")), "extraction_method": "none"})

    def walk(self, el: Tag):
        for child in el.children:
            if isinstance(child, NavigableString):
                if not child.__class__.__name__ == "Comment":
                    self.buffer.append(str(child))
                continue
            name = child.name
            if name == "br":
                self.buffer.append("\n")
            elif name == "img":
                self.image(child)
            elif name in INLINE:
                if child.find(["img", "table", "ul", "ol"]):
                    self.walk(child)
                else:
                    self.buffer.append(inline_text(child))
            elif name in HEADING_LEVEL and name in ("h2", "h3") and self.is_sentence(child):
                self.buffer.append(inline_text(child) + "\n")  # 큰 글씨로 강조한 문장(h2·h3)은 제목이 아니라 문단 (학생증 등)
            elif name in HEADING_LEVEL:
                self.heading(child)
            elif name in ("ul", "ol") and child.find(list(HEADING_LEVEL)):
                self.flush()                                   # 목록 안에 소제목이 있는 페이지(학생증 등): 목록이 아니라 구획으로 읽음
                self.walk(child)
                self.flush()
            elif name in ("ul", "ol"):
                self.flush()
                items = list_items(child)
                if items:
                    self.add({"type": "list", "items": items, "extraction_method": "html"})
            elif name == "table":
                self.flush()
                rows = table_rows(child)
                caption = child.find("caption")
                if rows:
                    self.add({"type": "table", "caption": norm(caption.get_text()) if caption else None,
                              "rows": rows, "extraction_method": "html"})
            elif name == "iframe":
                self.flush()
                self.add({"type": "iframe", "text": None, "title": child.get("title"),
                          "src": urljoin(self.page_url, child.get("src", "")), "extraction_method": "none"})
            elif name in ("script", "style", "noscript", "select", "button", "form", "input"):
                continue
            else:                       # p, div, section, dl 등 블록 요소
                self.flush()
                self.walk(child)
                self.flush()


# 동작을 가리키는 버튼 글자 ("인터넷 증명서 신청", "증명서 위변조 확인", "수강신청 시스템" 등).
# 끝의 "바로가기"는 떼고 판단 ("소속학과(부) 행정팀 바로가기"는 부서 이름이라 남김, 정제 단계에서 "바로가기"만 뗌)
ACTION_BUTTON = re.compile(r"(?:신청|확인|조회|시스템)$")
GO_SUFFIX = re.compile(r"\s*바로\s?가기$")


def drop_button_links(soup: BeautifulSoup) -> None:
    """줄에 링크 버튼(a.btn-link, a.btn-link02)만 있고 글자가 동작을 가리키면 지운다 (2026-11-12).
    "소속학과(부) 행정팀", "외국어센터", "K-MOOC"처럼 버튼 글자가 곧 내용(부서·기관 이름)인 것은 남기고,
    문장 안에 끼어 있는 버튼은 문장이 깨지므로 남긴다. 첨부 다운로드 버튼(btn-down)은 첨부 제목이라 남긴다."""
    def is_button(node) -> bool:
        return isinstance(node, Tag) and node.name == "a" and any(
            c.startswith("btn-link") for c in node.get("class", [])) and bool(
            ACTION_BUTTON.search(GO_SUFFIX.sub("", node.get_text(" ", strip=True))))
    for parent in {a.parent for a in soup.find_all(is_button)}:
        rest = [n for n in parent.children
                if not is_button(n) and not (isinstance(n, Tag) and n.name == "br")
                and (n.get_text(strip=True) if isinstance(n, Tag) else str(n).strip())]
        if not rest:
            for a in parent.find_all(is_button, recursive=False):
                a.decompose()


def extract_html(meta: dict) -> list[dict]:
    soup = BeautifulSoup((RAW_HTML / meta["file"]).read_bytes(), "lxml", from_encoding="utf-8")
    drop_button_links(soup)
    main = soup.select_one("#jwxe_main_content")
    if main is None:
        return []
    ex = HtmlExtractor(meta["url"])      # 페이지 제목(h3)은 doc_title에 이미 있으므로 경로에 넣지 않음
    ex.walk(main)
    ex.flush()
    # 본문 영역 밖의 안내 상자 (6개 페이지, 예: 이중전공의 전공디자인센터 상담 안내). 2026-11-12 누락 발견
    for box in soup.select("section.content .border-box-red"):
        if main in box.parents:
            continue
        ex.path = []
        ex.walk(box)
        ex.flush()
    # 문의처는 본문 영역 밖의 별도 블록(.contact-wrap)에 있음 (59개 페이지). 부서 단위 안내라 함께 추출
    contact = soup.select_one(".contact-wrap")
    if contact is not None:
        ex.path = []
        ex.walk(contact.parent)
        ex.flush()
    return ex.blocks


# ── VLM (이미지) ────────────────────────────────────────────────────

class VlmReader:
    """sources.yaml의 vlm_images에 있는 이미지만 Qwen3-VL로 읽는다.
    결과는 이미지 SHA-256 + 모델 + 프롬프트 기준으로 data/raw/vlm/에 저장해 다시 호출하지 않는다."""

    def __init__(self, targets: list[dict]):
        self.targets = {t["src"]: t["prompt"] for t in targets}
        self.fetcher = Fetcher()
        self.client = None
        self.calls = 0

    def read(self, src: str) -> dict | None:
        prompt = self.targets.get(src)
        if prompt is None:
            return None
        res = self.fetcher.get(src)
        res.raise_for_status()
        image = res.content
        model = os.getenv("QWEN_VLM_MODEL", "furiosa-ai/Qwen3-VL-32B-Instruct")
        key = hashlib.sha256(image + model.encode() + prompt.encode()).hexdigest()
        cache = VLM_DIR / f"{key[:16]}.json"
        if cache.exists():
            return read_json(cache)

        if self.client is None:
            from openai import OpenAI
            env = require_env("QWEN_BASE_URL", "FURIOSA_API_KEY")
            self.client = OpenAI(base_url=env["QWEN_BASE_URL"], api_key=env["FURIOSA_API_KEY"], timeout=180)
        mime = res.headers.get("Content-Type", "image/png").split(";")[0]
        out = self.client.chat.completions.create(
            model=model, temperature=0, max_tokens=1500,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}},
                {"type": "text", "text": prompt}]}])
        self.calls += 1
        result = {"src": src, "model": model, "image_sha256": hashlib.sha256(image).hexdigest(),
                  "prompt": prompt, "text": norm(out.choices[0].message.content), "read_at": now_iso()}
        write_json(cache, result)
        return result


def apply_vlm(blocks: list[dict], vlm: VlmReader) -> list[dict]:
    for b in blocks:
        if b["type"] == "image" and b.get("src"):
            result = vlm.read(b["src"])
            if result:
                b.update(text=result["text"], extraction_method="vlm", vlm_model=result["model"])
    return blocks


# ── PDF ─────────────────────────────────────────────────────────────

# 표 영역 글자가 칸으로 하나라도 덜 뽑히면 칸 구조 대신 줄 단위 글자로 보존 (병합 칸 글자 손실 방지).
# 2026-11-02 기준 PDF 표 303개 중 233개는 칸 구조 유지, 70개는 줄 단위
MIN_TABLE_COVERAGE = 1.0

# 새 항목으로 시작하는 줄: 앞 줄과 이어 붙이지 않음 (항 ①, 목록 기호, 번호, 조·장·절)
NEW_ITEM = re.compile(r"^\s*([①-⑳]|[-*※•○□■◦▶☞]|\d+[.)]\s|\(\d+\)|[가-하][.)]\s|제\s*\d+\s*(조|장|절|관)|\[)")
WRAP_TOLERANCE = 0.12      # 줄 오른쪽 끝이 쪽 글자 영역 오른쪽 끝에서 이 비율 안이면 "자리가 없어 넘어간 줄" (4·8·12·16% 비교, 12%부터 그대로)


def join_pdf_lines(lines: list[dict], right_edge: float) -> str:
    """PDF 줄을 이어 붙임. 오른쪽 끝까지 찬 줄은 자리가 없어 넘어간 것이라 다음 줄과 이음
    (한글끼리는 붙이고 그 밖에는 띄움). 짧게 끝난 줄과 새 항목 앞은 줄바꿈 유지."""
    if not lines:
        return ""
    left = min(l["x0"] for l in lines)
    margin = (right_edge - left) * WRAP_TOLERANCE
    out = lines[0]["text"]
    for prev, cur in zip(lines, lines[1:]):
        wrapped = prev["x1"] >= right_edge - margin and not NEW_ITEM.match(cur["text"])
        if wrapped:
            a, b = out[-1:], cur["text"][:1]
            out += ("" if re.match(r"[가-힣]", a) and re.match(r"[가-힣]", b) else " ") + cur["text"]
        else:
            out += "\n" + cur["text"]
    return out


def char_coverage(source: str, extracted: str) -> float:
    """source의 글자(공백 제외) 중 extracted에도 있는 비율."""
    src = Counter("".join(source.split()))
    if not src:
        return 1.0
    lost = src - Counter("".join(extracted.split()))
    return 1 - sum(lost.values()) / sum(src.values())


def extract_pdf(path: Path) -> list[dict]:
    blocks = []
    with pdfplumber.open(path) as pdf:
        for no, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            boxes = [t.bbox for t in tables]
            table_blocks = []
            for t in tables:
                rows = [[" ".join(norm(c or "").split("\n")) for c in row] for row in t.extract()]
                rows = [r for r in rows if any(r)]
                region = page.crop(t.bbox).extract_text() or ""
                if char_coverage(region, " ".join(c for r in rows for c in r)) >= MIN_TABLE_COVERAGE:
                    if rows:
                        table_blocks.append((t.bbox[1], {"section_path": [], "page": no, "type": "table",
                                                         "caption": None, "rows": rows, "extraction_method": "pdf_text"}))
                elif norm(region):
                    # 칸 사이 선이 없어 칸을 못 나눈 표: 칸 구조 대신 표 영역 글자를 줄 단위로 보존
                    table_blocks.append((t.bbox[1], {"section_path": [], "page": no, "type": "table_text",
                                                     "text": norm(region), "extraction_method": "pdf_text"}))

            def outside_tables(obj, boxes=boxes):
                x0, top, x1, bottom = obj.get("x0", 0), obj.get("top", 0), obj.get("x1", 0), obj.get("bottom", 0)
                return not any(b[0] <= x0 and x1 <= b[2] and b[1] <= top and bottom <= b[3] for b in boxes)

            # 표 밖 글자를 줄 단위로 읽어, 표를 쪽 안의 실제 위치(위에서부터)에 끼워 넣음
            lines = page.filter(outside_tables).extract_text_lines()
            right_edge = max((l["x1"] for l in lines), default=0)
            pending = sorted(table_blocks, key=lambda x: x[0])
            buffer: list[dict] = []
            text_chars = 0

            def flush():
                nonlocal text_chars
                text = norm(join_pdf_lines(buffer, right_edge))
                buffer.clear()
                if text:
                    text_chars += len(text)
                    blocks.append({"section_path": [], "page": no, "type": "paragraph", "text": text,
                                   "extraction_method": "pdf_text"})

            for line in lines:
                while pending and pending[0][0] < line["top"]:
                    flush()
                    blocks.append(pending.pop(0)[1])
                buffer.append(line)
            flush()
            blocks.extend(b for _, b in pending)
            if text_chars < MIN_PDF_PAGE_CHARS and not tables:
                blocks.append({"section_path": [], "page": no, "type": "image", "text": None,
                               "alt": None, "src": None, "extraction_method": "none"})
    return blocks


# ── 저장 ────────────────────────────────────────────────────────────

def render_md(doc: dict) -> str:
    out = [f"# {doc['doc_title']}", f"{doc['url']}" + (f"\n첨부: {doc['attachment_url']}" if doc.get("attachment_url") else ""), ""]
    last_path = None
    for b in doc["blocks"]:
        path = " > ".join(b["section_path"])
        if path != last_path and path:
            out.append(f"\n## {path}")
        last_path = path
        page = f"[p.{b['page']}] " if b.get("page") else ""
        if b["type"] == "paragraph":
            out.append(page + b["text"])
        elif b["type"] == "table_text":
            out.append(page + "표(칸 구분 없이 줄 단위):\n" + b["text"])
        elif b["type"] == "image" and b.get("text"):
            out.append(f"{page}[이미지 · VLM으로 읽음] {b.get('alt') or ''}\n{b['text']}")
        elif b["type"] == "list":
            out.append(page + "\n".join(b["items"]))
        elif b["type"] == "table":
            out.append(page + (f"표: {b['caption']}\n" if b.get("caption") else "표:\n")
                       + "\n".join("| " + " | ".join(r) + " |" for r in b["rows"]))
        else:
            out.append(f"{page}[{b['type']} · 읽지 못함] {b.get('alt') or b.get('title') or ''} {b.get('src') or ''}")
        out.append("")
    return "\n".join(out)


def pdf_preservation(path: Path, blocks: list[dict]) -> float:
    """PDF 원래 글자 중 추출 블록(문단·표·줄 단위 표)에 들어간 비율. 1.0이어야 정상."""
    pages: dict[int, str] = {}
    for b in blocks:
        if b.get("page"):
            pages[b["page"]] = pages.get(b["page"], "") + (b.get("text") or "".join(c for r in b.get("rows", []) for c in r))
    with pdfplumber.open(path) as pdf:
        src = "".join(pg.extract_text() or "" for pg in pdf.pages)
    return round(char_coverage(src, "".join(pages.values())), 5)


def doc_stats(blocks: list[dict]) -> dict:
    chars = sum(len(b.get("text") or "") + sum(len(i) for i in b.get("items", []))
                + sum(len(c) for r in b.get("rows", []) for c in r) for b in blocks)
    return {"blocks": len(blocks), "chars": chars,
            "tables": sum(b["type"] == "table" for b in blocks),
            "table_text": sum(b["type"] == "table_text" for b in blocks),
            "vlm": sum(b["extraction_method"] == "vlm" for b in blocks),
            "unreadable": sum(b["extraction_method"] == "none" for b in blocks),
            "sections": len({tuple(b["section_path"]) for b in blocks if b["section_path"]})}


def main():
    EXTRACTED.mkdir(parents=True, exist_ok=True)
    crawl_state = read_json(CRAWL_STATE, {})
    attach_state = read_json(ATTACH_STATE, {})
    report = {"run_at": now_iso(), "docs": {}}
    vlm = VlmReader(load_sources().get("vlm_images", []))

    for doc_id, meta in crawl_state.items():
        if meta.get("kind") != "page" or not meta.get("ok"):
            continue
        blocks = apply_vlm(extract_html(meta), vlm)
        doc = {"doc_id": doc_id, "source_type": "html", "doc_title": meta["doc_title"], "url": meta["url"],
               "attachment_url": None, "scope": meta["scope"], "category": meta["category"],
               "collected_at": meta["fetched_at"][:10], "blocks": blocks}
        write_json(EXTRACTED / f"{doc_id}.json", doc)
        (EXTRACTED / f"{doc_id}.md").write_text(render_md(doc), encoding="utf-8")
        report["docs"][doc_id] = doc_stats(blocks)

    for url, att in attach_state.items():
        if att.get("excluded") or att.get("removed_from_pages") or not att.get("file"):
            continue
        page_meta = crawl_state[att["pages"][0]]
        doc_id = Path(att["file"]).stem
        preservation = None
        if att["file_type"] == "pdf":
            blocks, source_type = extract_pdf(ATTACH_DIR / att["file"]), "pdf"
            preservation = pdf_preservation(ATTACH_DIR / att["file"], blocks)
        else:                            # HWP 등 아직 읽지 못하는 형식: 제목·링크만
            blocks = [{"section_path": [], "page": None, "type": "file", "text": None, "alt": att.get("name"),
                       "src": url, "extraction_method": "none"}]
            source_type = att["file_type"]
        doc = {"doc_id": doc_id, "source_type": source_type,
               "doc_title": f"{page_meta['doc_title']} > {att.get('name') or att['file']}",
               "url": page_meta["url"], "attachment_url": url, "attachment_name": att.get("name"),
               "scope": att.get("scope") or page_meta["scope"], "category": page_meta["category"],
               "collected_at": (att.get("fetched_at") or att["checked_at"])[:10], "blocks": blocks}
        write_json(EXTRACTED / f"{doc_id}.json", doc)
        (EXTRACTED / f"{doc_id}.md").write_text(render_md(doc), encoding="utf-8")
        report["docs"][doc_id] = doc_stats(blocks) | {"pdf_char_preservation": preservation}

    docs = report["docs"]
    report["pdf_char_loss"] = {d: s["pdf_char_preservation"] for d, s in docs.items()
                               if s.get("pdf_char_preservation") is not None and s["pdf_char_preservation"] < 1.0}
    report |= {"total_docs": len(docs),
               "empty_docs": [d for d, s in docs.items() if s["chars"] == 0],
               "docs_with_unreadable": {d: s["unreadable"] for d, s in docs.items() if s["unreadable"]},
               "total_chars": sum(s["chars"] for s in docs.values()),
               "total_tables": sum(s["tables"] for s in docs.values())}
    write_json(REPORTS / f"extract_{today()}.json", report)
    print(f"문서 {len(docs)}개 추출: 글자 {report['total_chars']:,}, 표 {report['total_tables']}, "
          f"본문 없음 {len(report['empty_docs'])} {report['empty_docs']}, "
          f"읽지 못한 블록이 있는 문서 {len(report['docs_with_unreadable'])}, VLM 호출 {vlm.calls}회, "
          f"PDF 글자 누락 문서 {len(report['pdf_char_loss'])} {report['pdf_char_loss']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
