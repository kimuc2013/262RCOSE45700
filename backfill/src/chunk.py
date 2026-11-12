"""6단계 청킹: 정제된 문서를 검색 근거 단위(청크)로 나눠 넘겨줄 파일을 만듦.

    python src/chunk.py

입력: data/clean/{doc_id}.json, data/calendar.jsonl, sources.yaml의 index(색인 대상)
결과 (data/export/, S3로만 전달)
- chunks.jsonl      청크 (CLAUDE.md "넘겨주는 데이터 형식", 한 줄에 하나). embedding은 null (모델 담당 검색 모듈에서 생성)
- calendar.jsonl    학사일정 (data/calendar.jsonl 사본)
- manifest.json     파일별 건수·SHA-256, dataset_version
공개 저장소용: data/chunks.sample.jsonl (형식 예시 몇 개)
리포트: data/reports/chunk_{날짜}.json

청킹 규칙
- 같은 소제목(HTML h4 > h5, PDF 규정의 제N조, 그 외 PDF의 번호 제목) 아래 내용을 순서대로 이어 붙여 청크 하나로. MAX_CHARS를 넘으면 목록 항목·문단·표 경계에서 나눔
- PDF 규정(제N조가 있는 문서): 조 하나 = 청크 하나. 길면 항(①②…) 경계에서 나눔
- PDF 그 외: "1. 제목" 같은 번호 제목 단위. 쪽마다 반복되는 머리글 줄은 뺌
- 청크 하나는 MAX_CHARS 이하. 넘으면 줄 경계에서 나눔 (bge-m3 한도 8,192토큰보다 훨씬 작게)
- chunk_id = {doc_id}#{section}. 소제목 하나가 청크 여러 개로 나뉠 때만 -1, -2 (같은 section 안에서만 셈). 소제목이 없으면 section은 null, ID는 "본문"
- 읽지 못한 이미지·iframe(extraction_method none)은 text·embedding을 null로 두고 제목·링크만
- sources.yaml index.exclude_sections의 소제목(학과별 연락처 긴 표 등)은 청크로 만들지 않음
"""
import hashlib
import json
import os
import re
import shutil
import statistics
import sys
from collections import Counter, defaultdict

from clean import CLEAN
from common import DATA, REPORTS, load_sources, now_iso, read_json, today, write_json, write_jsonl

EXPORT = DATA / "export"
MAX_CHARS = 2000           # 청크 글자 상한 (2026-11-12 1,200 → 2,000: 소제목 단위로 묶으면서. bge-m3 한도 8,192토큰보다 훨씬 작음)
MERGE_PARAGRAPH_CHARS = 600
SHORT_ITEM_CHARS = 30      # 항목이 모두 이보다 짧은 목록(절차·나열)은 목록 전체를 청크 하나로
CARRY_CHARS = 20           # 이보다 짧은 문단(제목·링크 글자)은 같은 소제목의 다음 청크 앞에 붙임
SECTION_MAX_CHARS = 20     # section·chunk_id에 쓰는 소제목 이름의 최대 길이 (2026-11-12 결정: 짧은 이름, 세부는 text로)

ARTICLE = re.compile(r"^제\s*\d+\s*조(?:의\s*\d+)?\s*\([^)]{1,40}\)")       # 제1조(목적)
ARTICLE_NO = re.compile(r"^제\s*(\d+)\s*조(?:의\s*(\d+))?")
QUESTION_TITLE = re.compile(r"^(?:-\s*)?(.{2,18}?)\s*(?:이란|란)\s*\?$")      # "금융 학생증이란?"
DELETED_ARTICLE = re.compile(r"^제\s*\d+\s*조(?:의\s*\d+)?\s*\([^)]*\)\s*<삭제>$")
CONTACT_ONLY = re.compile(r"^\s*-?\s*((Tel|TEL|Fax|FAX|E-?mail|전화|팩스)\s*[:.]?\s*[\d\-~ ,()]*"
                          r"|수정일자\s*:\s*[\d\-. ]+)$")                    # 연락처·수정일자 줄
CHAPTER = re.compile(r"^제\s*\d+\s*(장|절|관)\s*\S")                         # 제1장 총 칙
NUMBERED = re.compile(r"^\d{1,2}\.\s*[가-힣A-Za-z]")                        # 1. 현장실습 교과목 종류
CLAUSE = re.compile(r"^[①-⑳]")
SEMESTER = [re.compile(r"(20\d{2})\s*학년도\s*제?\s*([12])\s*학기"),
            re.compile(r"(?<!\d)(\d{2})년\s*([12])\s*학기")]


# 소제목 끝의 형태 설명 괄호: "(처리절차 이미지)", "(그림)", "(도식)", "(표)" 등
FORM_NOTE = re.compile(r"\s*\([^()]*(?:이미지|그림|도식|표)\)\s*$")


# 소제목 끝의 "~이란?", "~란?" ("유연학기제란?" → "유연학기제")
QUESTION_SUFFIX = re.compile(r"\s*(?:이란|란)\s*\?$")


def shorten(title: str) -> str | None:
    """소제목을 SECTION_MAX_CHARS 이하의 이름으로. 그대로 짧으면 그대로, 길면 쉼표·괄호 등 앞까지,
    그래도 길면 괄호 안 이름. 줄일 수 없으면 None."""
    title = title.strip()
    title = FORM_NOTE.sub("", title).strip() or title   # "(처리절차 이미지)"처럼 형태 설명 괄호는 뗌
    title = QUESTION_SUFFIX.sub("", title).strip() or title
    if len(title) <= SECTION_MAX_CHARS:
        return title
    for sep in (",", "(", " -", ":", " : "):
        cut = title.split(sep)[0].strip()
        if 2 <= len(cut) <= SECTION_MAX_CHARS:
            return cut
    m = re.search(r"\(([^()]{2,%d})\)" % SECTION_MAX_CHARS, title)
    return m.group(1).strip() if m else None


def section_label(path: list[str]) -> tuple[str | None, list[str], int]:
    """소제목 경로 → (section 이름, text 앞에 넣을 원래 제목 줄, 이름으로 쓴 소제목의 깊이).
    맨 위 소제목(h4)을 줄여 쓰고, 줄일 수 없으면 줄일 수 있는 가장 깊은 하위 소제목을 씀.
    줄였거나 대신 쓴 원래 제목은 text 첫 줄에 남김."""
    if not path:
        return None, [], 0
    top = shorten(path[0])
    if top:
        return top, ([path[0]] if top != path[0] else []), 1
    for depth in range(len(path), 1, -1):
        sub = shorten(path[depth - 1])
        if sub:
            return sub, path[:depth - 1] + ([path[depth - 1]] if sub != path[depth - 1] else []), depth
    return path[0], [], 1          # 줄일 수 없으면 글자를 자르지 않고 원래 제목 그대로 (2026-11-12, 예: tutorial)


def target_semester(text: str | None) -> str | None:
    """청크에 학기가 하나만 나오면 그 학기, 아니면 null."""
    if not text:
        return None
    found = set()
    for pat in SEMESTER:
        for y, t in pat.findall(text):
            found.add(f"{int(y) + 2000 if len(y) == 2 else y}-{t}")
    return found.pop() if len(found) == 1 else None


def split_lines(text: str, limit: int = MAX_CHARS) -> list[str]:
    """limit을 넘는 글을 줄 경계에서 나눔. 항(①…)이나 목록 기호로 시작하는 줄에서 끊는 것을 우선."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], []
    for line in text.split("\n"):
        size = sum(len(l) + 1 for l in cur)
        starts_item = bool(CLAUSE.match(line) or re.match(r"^\s*(-|\*|※|\d+\)|[가-하]\.)", line))
        if cur and (size + len(line) > limit or (starts_item and size > limit * 0.6)):
            parts.append("\n".join(cur))
            cur = []
        while len(line) > limit:                               # 한 줄이 너무 길면 글자 수로 자름
            parts.append(line[:limit])
            line = line[limit:]
        cur.append(line)
    if cur:
        parts.append("\n".join(cur))
    return [p for p in parts if p.strip()]


def fill_merged_cells(rows: list[list[str]]) -> list[list[str]]:
    """PDF 표: 왼쪽부터 빈칸이 있는 열만 위 칸 값으로 채움 (병합 칸이 빈칸으로 나오는 문제). 첫 행은 머리글."""
    if len(rows) < 3:
        return rows
    rows = [list(r) for r in rows]
    for c in range(len(rows[0])):
        data = rows[1:]
        if not any(not r[c] for r in data):
            break                                              # 빈칸 없는 열이 나오면 그 오른쪽은 채우지 않음
        for i in range(2, len(rows)):
            if not rows[i][c] and rows[i - 1][c] and any(rows[i]):
                rows[i][c] = rows[i - 1][c]
    return rows


def table_chunks(rows: list[list[str]], caption: str | None) -> list[str]:
    """표를 '| a | b |' 줄로. 길면 행을 나누고 각 청크에 머리글 행을 다시 붙임."""
    line = lambda r: "| " + " | ".join(r) + " |"
    head = (f"[표] {caption}\n" if caption else "[표]\n") + line(rows[0])
    body = [line(r) for r in rows[1:]]
    if not body:
        return [head]
    out, cur = [], []
    for b in body:
        if cur and len(head) + sum(len(x) + 1 for x in cur) + len(b) > MAX_CHARS:
            out.append(head + "\n" + "\n".join(cur))
            cur = []
        cur.append(b)
    out.append(head + "\n" + "\n".join(cur))
    return out


class Chunker:
    def __init__(self, doc: dict, exclude_sections: set[str], rename: dict[str, str] | None = None,
                 heading_only: set[str] | None = None):
        self.doc = doc
        self.exclude = exclude_sections
        self.heading_only = heading_only or set()
        self.rename = rename or {}
        self.counter = Counter()
        self.chunks: list[dict] = []
        self.skipped_excluded = 0
        self.carry: tuple[str | None, str, str, int | None] | None = None   # (section, text, method, page)

    def hold(self, section: str | None, text: str, method: str, page: int | None = None):
        """짧은 제목·링크 글자: 같은 소제목의 다음 청크 앞에 붙이려고 잠시 보관."""
        self.release()
        self.carry = (section, text, method, page)

    def release(self):
        """보관한 짧은 글을 붙일 곳이 없을 때: 소제목이 없거나 소제목을 반복한 글(링크·버튼 글자), 제목 줄만 있는 글은 버리고,
        소제목 아래의 짧은 답("5월, 11월 중" 등)은 따로 청크로 남김."""
        if self.carry:
            section, text, method, page = self.carry
            self.carry = None
            if section and text.strip() != section.split(" > ")[-1] and method != "_heading":
                self.emit(section, text, method, page)

    def emit(self, section: str | None, text: str | None, method: str, page: int | None = None):
        if self.carry:
            if text and self.carry[0] == section:
                text = self.carry[1] + "\n" + text
                page = self.carry[3] if self.carry[3] is not None else page
                self.carry = None
            else:
                self.release()
        if section and section in self.exclude:               # PDF 소제목 제외 (HTML은 html()에서 원래 경로로 판단)
            self.skipped_excluded += 1
            return
        texts = split_lines(text) if text else [None]
        for t in texts:
            key = section or "본문"
            self.counter[key] += 1
            d = self.doc
            self.chunks.append({
                "chunk_id": f"{d['doc_id']}#{key}-{self.counter[key]}",
                "doc_id": d["doc_id"],
                "doc_title": d["doc_title"],
                "section": section,
                "url": d["url"],
                "attachment_url": d.get("attachment_url"),
                "page": page,
                "text": t,
                "embedding": None,
                "extraction_method": method if t else "none",
                "scope": d["scope"],
                "target_semester": target_semester(t),
                "collected_at": d["collected_at"],
            })

    def finalize(self):
        """같은 소제목의 조각(목록 항목·문단·표)을 순서대로 이어 붙여 청크 하나로 만듦 (2026-11-12 결정).
        MAX_CHARS를 넘으면 조각 경계에서 나눔. 추출 방식(html·pdf_text·vlm)이 다른 조각과 읽지 못한 조각(none)은 따로 둠.
        그다음 소제목별 순번을 다시 매김."""
        final, pack = [], None
        for c in self.chunks:
            if not c["text"]:                                  # 읽지 못한 이미지·iframe: 청크로 넘기지 않음 (2026-11-12 결정)
                continue
            if DELETED_ARTICLE.match(c["text"].strip()):       # 규정의 삭제된 조항 "제128조(자격과 신청) <삭제>"
                continue
            if all(CONTACT_ONLY.match(l) for l in c["text"].split("\n") if l.strip()):
                continue                                       # 전화·팩스 줄만 있는 청크 (문의처와 같은 연락처)
            methods = {c["extraction_method"]} | ({pack["extraction_method"]} if pack else set())
            same = (pack is not None and pack["section"] == c["section"]
                    and (len(methods) == 1 or methods == {"vlm", "html"}))
            if same and len(pack["text"]) + len(c["text"]) + 1 <= MAX_CHARS:
                pack["text"] += "\n" + c["text"]
                if "vlm" in methods:                           # 이미지(VLM)와 같은 소제목의 HTML 줄은 한 청크로 (2026-11-12,
                    pack["extraction_method"] = "vlm"          # 예: 교환학생 협정 대학 지도 + 협정교 참고 줄)
                continue
            if pack is not None:
                final.append(pack)
            pack = dict(c)
        if pack is not None:
            final.append(pack)
        # 소제목 첫 조각이 아주 짧고 혼자 남은 경우(다음 조각이 1,200자를 꽉 채움 등)는 그대로 둠
        # chunk_id: 소제목에 청크가 하나면 번호 없이 "{doc_id}#{section}", 둘 이상으로 나뉘면 "-1", "-2" (2026-11-12 결정)
        total = Counter(c["section"] or "본문" for c in final)
        counter = Counter()
        for c in final:
            key = c["section"] or "본문"
            counter[key] += 1
            suffix = f"-{counter[key]}" if total[key] > 1 else ""
            c["chunk_id"] = f"{c['doc_id']}#{key}{suffix}"
            c["target_semester"] = target_semester(c["text"])
        self.chunks = final

    # ── HTML ──
    def html(self):
        para, para_section = [], None

        heading_lines: set[str] = set()

        def flush():
            nonlocal para
            text = "\n".join(para)
            if para and all(p in heading_lines for p in para):
                self.hold(para_section, text, "_heading")      # 제목 줄만 있으면 다음 청크(목록·표·VLM) 앞에 붙임
            elif text and len(text) < CARRY_CHARS:
                self.hold(para_section, text, "html")
            elif text:
                self.emit(para_section, text, "html")
            para = []

        last_path, last_label = [], None
        label_owner: dict[str, str] = {}                       # section 이름 → 그 이름을 처음 쓴 원래 소제목
        heading_only_done: set[str] = set()
        question_label = None                                  # 소제목 없는 곳의 "OO이란?" 줄 → 섹션 이름 "OO"
        for b in self.doc["blocks"]:
            path = b["section_path"]
            if not path:
                first = ((b.get("text") or (b.get("items") or [""])[0]).split("\n")[0]).strip()
                m = QUESTION_TITLE.match(first)
                if m:
                    question_label = m.group(1).strip()
                if question_label:
                    path = [question_label]
            else:
                question_label = None
            if path and " > ".join(path) in self.heading_only:  # 내용(긴 연락처 표)은 빼고 소제목 줄만 청크로
                if " > ".join(path) not in heading_only_done:
                    heading_only_done.add(" > ".join(path))
                    self.emit(section_label(path)[0], path[-1], "html")
                self.skipped_excluded += 1
                continue
            if path and (path[0] in self.exclude or " > ".join(path) in self.exclude):
                self.skipped_excluded += 1                     # 제외 소제목은 원래 경로로 판단
                continue
            section, context, depth = section_label(path)
            if path and section in label_owner and label_owner[section] != path[0]:
                section, context, depth = path[0], [], 1       # 줄인 이름이 다른 소제목과 같아지면 원래 제목 그대로
            elif path:                                         # (예: "…본교 수학(INCOMING)"과 "…본교 수학(학점교류) 기준")
                label_owner.setdefault(section, path[0])
            if section in self.rename:                         # sources.yaml의 index.section_names
                if path and path[0] not in context:
                    context = [path[0]] + list(context)
                section = self.rename[section]
            if path != last_path:                              # 하위 소제목·원래 제목을 text 안의 줄로
                heads = list(context) if section != last_label else []
                for i in range(depth, len(path)):
                    if section != last_label or i >= len(last_path) or path[i] != last_path[i]:
                        heads.append(path[i])
                if heads:
                    if section != para_section:
                        flush()
                    para_section = section
                    heading_lines.update(heads)
                    para.extend(h for h in heads if h not in para)
                last_path, last_label = path, section
            if b["type"] != "paragraph" or section != para_section:
                flush()
            para_section = section
            if b["type"] == "paragraph":
                if para and sum(len(p) for p in para) + len(b["text"]) > MERGE_PARAGRAPH_CHARS:
                    flush()
                para.append(b["text"])
            elif b["type"] == "list":
                if all(len(i) <= SHORT_ITEM_CHARS and "\n" not in i for i in b["items"]):
                    self.emit(section, "\n".join(b["items"]), "html")      # 짧은 절차·나열은 목록 전체가 청크 하나
                else:
                    for item in b["items"]:
                        self.emit(section, item, "html")
            elif b["type"] == "table":
                for t in table_chunks(b["rows"], b.get("caption")):
                    self.emit(section, t, "html")
            elif b["type"] == "image" and b.get("text"):
                self.emit(section, (f"[이미지] {b['alt']}\n" if b.get("alt") else "") + b["text"], "vlm")
            else:                                              # 읽지 못한 이미지·iframe: 제목·링크만
                self.emit(section, None, "none")
        flush()
        self.release()

    # ── PDF ──
    def pdf(self):
        blocks = self.doc["blocks"]
        page_first_lines = Counter()
        pages = {b["page"] for b in blocks if b.get("page")}
        seen_pages = set()
        for b in blocks:
            if b["type"] == "paragraph" and b["page"] not in seen_pages:
                seen_pages.add(b["page"])
                page_first_lines[b["text"].split("\n")[0].strip()] += 1
        headers = {l for l, n in page_first_lines.items() if n >= max(2, len(pages) / 2)}  # 쪽마다 반복되는 머리글

        all_text = "\n".join(b.get("text") or "" for b in blocks)
        regulation = sum(1 for l in all_text.split("\n") if ARTICLE.match(l.strip())) >= 3
        heading = ARTICLE if regulation else NUMBERED

        section, heading_line, start_page, buf = None, None, None, []

        def flush():
            nonlocal buf
            text = "\n".join(buf).strip()
            if text and len(text) < CARRY_CHARS or (heading_line and text == heading_line):
                self.hold(section, text, "pdf_text", start_page)   # 제목 한 줄만 있으면 다음 청크(표 등) 앞에 붙임
            elif text:
                self.emit(section, text, "pdf_text", start_page)
            buf = []

        for b in blocks:
            if b["type"] == "paragraph":
                for line in b["text"].split("\n"):
                    s = line.strip()
                    if s in headers or (regulation and CHAPTER.match(s) and len(s) < 30):
                        continue                               # 쪽 머리글, 장·절·관 제목은 청크 본문에서 뺌
                    m = heading.match(s)
                    if m:
                        flush()
                        heading_line = s
                        if regulation:                         # 제2조(시행세칙, 내규, 지침) → "제2조" (제목은 text 첫 줄에 있음)
                            n = ARTICLE_NO.match(s)
                            section = f"제{n.group(1)}조" + (f"의{n.group(2)}" if n.group(2) else "")
                        else:                                  # 1. 현장실습 교과목 종류 → "현장실습 교과목 종류"
                            title = re.sub(r"^\d{1,2}\.\s*", "", s)
                            section = shorten(title) or title
                        start_page = b["page"]
                    if not buf:
                        start_page = b["page"]
                    buf.append(line)
            elif b["type"] in ("table", "table_text"):
                flush()
                if b["type"] == "table":
                    for t in table_chunks(fill_merged_cells(b["rows"]), b.get("caption")):
                        self.emit(section, t, "pdf_text", b["page"])
                else:
                    self.emit(section, "[표]\n" + b["text"], "pdf_text", b["page"])
            else:                                              # 그림 쪽(서식 등): 조항에 붙이지 않고 소제목 없이 제목·링크만
                flush()
                self.emit(None, None, "none", b.get("page"))
        flush()
        self.release()


def main():
    src = load_sources()
    index = src["index"]
    exclude_sections = set(index.get("exclude_sections", []))
    exclude_chunks = set(index.get("exclude_chunks", []))
    heading_only = set(index.get("heading_only_sections", []))
    merge_chunks = index.get("merge_chunks", {})
    chunks, per_doc, excluded = [], {}, 0
    for path in sorted(CLEAN.glob("*.json")):
        doc = read_json(path)
        if doc["scope"] not in index["include_scopes"] or doc["doc_id"] in index["exclude_docs"]:
            continue
        ch = Chunker(doc, exclude_sections, index.get("section_names", {}).get(doc["doc_id"]), heading_only)
        ch.html() if doc["source_type"] == "html" else ch.pdf()
        ch.finalize()
        by_id = {c["chunk_id"]: c for c in ch.chunks}
        for src_id, dst_id in merge_chunks.items():            # 청크 text를 다른 청크 뒤에 붙이고 원래 청크는 뺌
            if src_id in by_id and dst_id in by_id:
                by_id[dst_id]["text"] += "\n" + by_id[src_id]["text"]
                ch.chunks.remove(by_id[src_id])
        dropped = [c for c in ch.chunks if c["chunk_id"] in exclude_chunks]
        ch.chunks = [c for c in ch.chunks if c["chunk_id"] not in exclude_chunks]
        excluded += len(dropped)
        chunks.extend(ch.chunks)
        per_doc[doc["doc_id"]] = len(ch.chunks)
        excluded += ch.skipped_excluded

    ids = [c["chunk_id"] for c in chunks]
    dup_ids = [k for k, n in Counter(ids).items() if n > 1]
    if dup_ids:
        print(f"[ERR] chunk_id 중복: {dup_ids[:5]}")
        return 1

    EXPORT.mkdir(parents=True, exist_ok=True)
    write_jsonl(EXPORT / "chunks.jsonl", chunks)
    shutil.copyfile(DATA / "calendar.jsonl", EXPORT / "calendar.jsonl")

    def file_info(name):
        data = (EXPORT / name).read_bytes()
        return {"path": name, "count": data.count(b"\n"), "sha256": hashlib.sha256(data).hexdigest()}

    files = {"chunks": file_info("chunks.jsonl"), "calendar": file_info("calendar.jsonl")}
    created = now_iso()
    content_id = hashlib.sha256((files["chunks"]["sha256"] + files["calendar"]["sha256"]).encode()).hexdigest()[:8]
    manifest = {
        "schema_version": "1.0",
        "dataset_version": f"{today()}-{content_id}",       # 내용이 같으면 같은 버전 (서빙 캐시의 데이터 버전 구분용)
        "created_at": created,
        # 합의 형식(embedding_model을 청크에서 manifest로 옮김). 이 모델로 청크·질의를 임베딩함.
        # 벡터는 모델 담당 검색 모듈이 생성하므로 chunks의 embedding은 null. 값은 Furiosa RAG API 가이드와 2026-11-02 실제 호출 기준
        "embedding": {
            "model": os.getenv("BGE_MODEL", "furiosa-ai/bge-m3"),
            "revision": os.getenv("BGE_REVISION", "v2026.4"),
            "dimension": 1024,
            "query_prefix": "",
            "document_prefix": "",
            "normalized": True,
        },
        "files": files,
    }
    write_json(EXPORT / "manifest.json", manifest)

    # 공개 저장소용 형식 예시: 종류별로 하나씩 (HTML 목록, HTML 표, PDF 조항, VLM, 읽지 못함)
    by_id = {c["chunk_id"]: c for c in chunks}
    preferred = ["major_double#신청자격", "certificate_kind#학생증명서 종류", "info_caution_field#현장실습 교과목 종류"]
    sample = [by_id[i] for i in preferred if i in by_id]
    for method in ("vlm",):
        sample += [c for c in chunks if c["extraction_method"] == method][:1]
    write_jsonl(DATA / "chunks.sample.jsonl", sample)

    lengths = [len(c["text"]) for c in chunks if c["text"]]
    report = {
        "run_at": created,
        "dataset_version": manifest["dataset_version"],
        "chunks": len(chunks),
        "docs": len(per_doc),
        "excluded_section_chunks": excluded,
        "by_method": dict(Counter(c["extraction_method"] for c in chunks)),
        "length": {"min": min(lengths), "median": statistics.median(lengths), "max": max(lengths),
                   "over_limit": sum(1 for n in lengths if n > MAX_CHARS)},
        "with_target_semester": sum(1 for c in chunks if c["target_semester"]),
        "chunks_per_doc": dict(sorted(per_doc.items(), key=lambda x: -x[1])),
    }
    write_json(REPORTS / f"chunk_{today()}.json", report)
    print(f"청크 {len(chunks)}개 (문서 {len(per_doc)}개, 제외 소제목 {excluded}개) → {EXPORT / 'chunks.jsonl'}")
    print(f"  종류: {report['by_method']}, 학기 표시 {report['with_target_semester']}개")
    print(f"  길이: 최소 {report['length']['min']}, 중간 {report['length']['median']}, 최대 {report['length']['max']}자 "
          f"(상한 {MAX_CHARS} 초과 {report['length']['over_limit']})")
    print(f"  dataset_version {manifest['dataset_version']}, 예시 {len(sample)}개 → data/chunks.sample.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
