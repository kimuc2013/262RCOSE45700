"""4단계 정제: 추출 결과에서 답과 무관한 부분과 개인 연락처, 범위 밖(세종캠퍼스) 내용을 지움.

    python src/clean.py

입력: data/extracted/{doc_id}.json
결과
- data/clean/{doc_id}.json, .md      정제된 블록 (구조는 추출 결과와 같음)
- data/reports/clean_{날짜}.json      규칙별로 지운 건수, 문서별 글자 수 변화

규칙
1. 세종캠퍼스 (HTML만. 학사운영규정 PDF의 조항은 두 캠퍼스 공통 규정이라 남김)
   - 소제목 경로에 '세종캠퍼스'가 있는 블록 삭제 (문의처 > 세종캠퍼스)
   - 첫 줄에 '세종캠퍼스' 또는 '(세종)'이 있고 '서울'이 없는 목록 항목 삭제
   - 줄 안의 세종 부분만 삭제: '(세종: 044-…)', '/ …(세종)', ', 세종 …'
   - 그래도 '세종'이 남고 '서울'이 없는 줄 삭제
   - 표: 캡션에 '세종캠퍼스'가 있으면 표 삭제, 머리글에 '세종'이 있는 열 삭제,
     '세종'만 있는 행 삭제, '서울, 세종' 칸은 '서울'로
   - '서울 및 세종캠퍼스 …'처럼 두 캠퍼스를 함께 말하는 문장은 남김
1-1. 대학원 (HTML만, 2026-11-12): 머리글이 대학원인 표 열, 첫 칸이 대학원이고 학부가 없는 표 행, "대학원"으로 시작하는 줄,
     학부 언급 없는 대학원 신입생 일정 줄 삭제. "학부 및 대학원"처럼 함께 말하는 문장과 학·석사 연계과정은 남김
2. 개인 연락처: 이메일 주소 삭제. 전화번호는 모두 부서·업무 단위(사람 이름 없음)라 남김 (2026-11-02 확인)
3. 버튼·메뉴 글자: '고려대학교 홈페이지 바로가기' 같은 버튼만 있는 줄 삭제, 줄 끝 '바로가기'·'☜클릭' 삭제
4. PDF 쪽 번호('- 1 -') 삭제
5. sources.yaml의 clean.drop_ranges에 지정한 부분, clean.drop_lines에 지정한 줄 삭제 (예: 현장실습 PDF의 '6. 운영 교과목 안내')
6. 영어 번역본 삭제 (2026-11-02 결정: 같은 내용이 한국어·영어로 다 있으면 한국어만)
   - PDF: 한국어 쪽이 있는 문서에서 한글 비율 20% 미만인 쪽
   - HTML: 소제목에 한글이 없고 내용의 한글 비율 5% 미만인 섹션 (등록금 페이지의 영어 섹션)
   - 한국어 소제목 아래 영어 이름 목록(협정 대학 등)은 번역이 아니므로 남김
PDF 줄바꿈은 합치지 않는다: 단어 중간 줄바꿈인지 띄어쓰기 자리인지 PDF에서 알 수 없어서, 잘못 붙이면 오히려 단어가 틀어짐.
"""
import re
import sys
from collections import Counter

from common import DATA, REPORTS, load_sources, now_iso, read_json, today, write_json
from extract import EXTRACTED, doc_stats, render_md

CLEAN = DATA / "clean"

BUTTON_LINES = {"고려대학교 홈페이지 바로가기", "교육과정표 바로가기", "새창으로 열기", "포털시스템", "FAQ 바로가기", "시청하기"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?:SEClipBoard_synap)?")
PAGE_NO = re.compile(r"^-\s*\d+\s*-$")
SEJONG_PAREN = re.compile(r"\s*\(세종[^()]*\)")                 # (세종: 044-860-1126), (세종)
SEJONG_SLASH = re.compile(r"\s*/\s*[^/]*\(세종\)[^/]*?(?=\s*/|$)")  # / 학부-학생생활지원팀(세종) ☜클릭
SEJONG_COMMA = re.compile(r",\s*세종[^,)/]*")                    # , 세종 학생복지팀

removed = Counter()
DROP_LINES: set[str] = set()          # sources.yaml의 clean.drop_lines (main에서 읽음)


def is_sejong_only(text: str) -> bool:
    return "세종" in text and "서울" not in text


def is_graduate_only(text: str) -> bool:
    """대학원에만 해당하는 줄·칸 (학부 공통 페이지 안의 대학원 안내). 학부와 함께 말하는 문장은 남김."""
    t = text.strip().lstrip("-*※· ").strip()
    if "대학원" not in t or "학부" in t:
        return False
    return t.startswith("대학원") or ("신입생" in t and "대학원" in t)


def clean_line(line: str, html: bool) -> str | None:
    """줄 하나를 정제. 지워야 하면 None."""
    indent = re.match(r"^\s*(-\s)?", line).group(0)
    body = line[len(indent):].strip()

    if body in BUTTON_LINES:
        removed["버튼 줄"] += 1
        return None
    if body in DROP_LINES:
        removed["지정한 줄"] += 1
        return None
    if PAGE_NO.match(body):
        removed["PDF 쪽 번호"] += 1
        return None

    if "☜" in body:
        body = re.sub(r"\s*☜\s*클릭", "", body).replace("☜", "")
        removed["☜클릭"] += 1
    if body.endswith("바로가기"):
        body = body[: -len("바로가기")].rstrip()
        removed["줄 끝 바로가기"] += 1
    if EMAIL.search(body):
        removed["이메일"] += len(EMAIL.findall(body))
        body = EMAIL.sub("", body)
        body = re.sub(r"\s*,\s*(,|$)", r"\1", body).strip(" ,/:")
        body = re.sub(r"\(\s*e-?mail\s*:?\s*\)", "", body, flags=re.I).strip()   # "(e-mail: )"처럼 주소만 빠진 괄호
        if re.fullmatch(r"(?i)(e-?mail|이메일|메일)\s*:?", body):
            return None                                        # 주소가 빠지고 이름표만 남은 줄

    if html and "세종" in body:
        before = body
        body = SEJONG_SLASH.sub("", body)
        if "서울" in body:
            body = SEJONG_COMMA.sub("", body)
        if not is_sejong_only(before):                       # 두 캠퍼스를 함께 말하는 줄: 세종 부분만 지움
            pass
        else:
            body = SEJONG_PAREN.sub("", body)
        if body != before:
            removed["세종 부분 삭제"] += 1
        if is_sejong_only(body):
            removed["세종 줄 삭제"] += 1
            return None

    if html and is_graduate_only(body):
        removed["대학원 줄 삭제"] += 1
        return None

    body = body.strip()
    return indent + body if body else None


def clean_text(text: str, html: bool) -> str:
    lines = [clean_line(l, html) for l in text.split("\n")]
    return "\n".join(l for l in lines if l)


def clean_item(item: str, html: bool) -> str | None:
    """목록 항목 하나. 첫 줄이 세종 전용이면 하위 줄까지 통째로 삭제."""
    first = item.split("\n", 1)[0]
    if html and ("세종캠퍼스" in first or "(세종)" in first) and "서울" not in first:
        removed["세종 항목 삭제"] += 1
        return None
    text = clean_text(item, html)
    if text and not text.lstrip().startswith("-"):
        return None
    return text or None


def clean_table(block: dict, html: bool) -> dict | None:
    rows = [list(r) for r in block["rows"]]
    if html:
        if block.get("caption") and "세종캠퍼스" in block["caption"]:
            removed["세종 표 삭제"] += 1
            return None
        header = rows[0] if rows else []
        # 세종 지역번호(044)로만 구분되는 표: "세종" 글자 없이 연락처(044) 머리글만 있음 (2026-11-12 발견)
        if any("(044)" in c for c in header) and not any("(02)" in c for c in header):
            removed["세종 표 삭제"] += 1
            return None
        drop_cols = {i for i, c in enumerate(header) if "세종" in c and "서울" not in c}
        if drop_cols:
            removed["세종 열 삭제"] += len(drop_cols)
            rows = [[c for i, c in enumerate(r) if i not in drop_cols] for r in rows]
        kept = []
        for r in rows:
            if any(is_sejong_only(c) for c in r) and not any("서울" in c for c in r):
                removed["세종 행 삭제"] += 1
                continue
            if any(re.search(r"(?<!\d)044-\d", c) for c in r) and not any(re.search(r"(?<!\d)0?2-\d{3,4}-", c) for c in r):
                removed["세종 행 삭제(044)"] += 1                # 세종 지역번호만 있는 행
                continue
            kept.append([re.sub(r"서울\s*,\s*세종", "서울", c) for c in r])
        rows = kept
        # 대학원: 머리글이 대학원인 열, 첫 칸(빈칸 제외)이 대학원이고 학부가 없는 행 삭제 (2026-11-12)
        header = rows[0] if rows else []
        grad_cols = {i for i, c in enumerate(header) if "대학원" in c and "학부" not in c}
        if grad_cols and len(grad_cols) < len(header):
            removed["대학원 열 삭제"] += len(grad_cols)
            rows = [[c for i, c in enumerate(r) if i not in grad_cols] for r in rows]
        kept = []
        for i, r in enumerate(rows):
            first = next((c for c in r if c.strip()), "")
            if i > 0 and "대학원" in first and not any("학부" in c for c in r):
                removed["대학원 행 삭제"] += 1
                continue
            kept.append(r)
        rows = kept
    rows = [[clean_line(c, html) or "" for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    return block | {"rows": rows} if rows else None


def clean_block(block: dict, html: bool) -> dict | None:
    if html and any("세종캠퍼스" in p for p in block["section_path"]):
        removed["세종 블록 삭제"] += 1
        return None
    if html and any(p.startswith("대학원") for p in block["section_path"]):
        removed["대학원 블록 삭제"] += 1                         # 소제목 자체가 "대학원"인 섹션 (휴학 신청방법 > 대학원)
        return None
    t = block["type"]
    if t == "list":
        items = [i for i in (clean_item(i, html) for i in block["items"]) if i]
        return block | {"items": items} if items else None
    if t == "table":
        return clean_table(block, html)
    if t in ("paragraph", "table_text"):
        text = clean_text(block["text"], html)
        return block | {"text": text} if text else None
    if t == "image" and block.get("text"):                    # VLM으로 읽은 이미지
        return block | {"text": clean_text(block["text"], html)}
    return block                                               # 읽지 못한 이미지·iframe: 제목·링크 유지


def apply_drop_ranges(blocks: list[dict], ranges: list[dict]) -> list[dict]:
    """PDF 쪽 단위 잘라내기. from이 있으면 그 줄부터 그 쪽 끝까지, 없으면 쪽 전체."""
    out = []
    for b in blocks:
        rule = next((r for r in ranges if r["page"] == b.get("page")), None)
        if rule is None:
            out.append(b)
            continue
        if "from" not in rule or rule.get("_started"):
            removed["지정 범위 삭제"] += 1
            continue
        text = b.get("text") or ""
        if rule["from"] in text:
            rule["_started"] = True
            kept = text[: text.index(rule["from"])].rstrip()
            removed["지정 범위 삭제"] += 1
            if kept:
                out.append(b | {"text": kept})
            continue
        out.append(b)                                          # 시작 줄 앞의 블록은 유지
    missing = [r for r in ranges if "from" in r and not r.get("_started")]
    if missing:
        raise SystemExit(f"drop_ranges의 시작 줄을 찾지 못함: {missing} (원문이 바뀌었는지 확인)")
    return out


def hangul_ratio(text: str) -> float | None:
    letters = re.findall(r"[가-힣A-Za-z]", text)
    if not letters:
        return None
    return sum(1 for c in letters if "가" <= c <= "힣") / len(letters)


def block_text(b: dict) -> str:
    return (b.get("text") or "") + "\n".join(b.get("items", [])) + " ".join(c for r in b.get("rows", []) for c in r)


def drop_english_translation(blocks: list[dict], html: bool) -> list[dict]:
    if html:
        out = []
        for b in blocks:
            heading = b["section_path"][-1] if b["section_path"] else ""
            ratio = hangul_ratio(block_text(b))
            if heading and not re.search(r"[가-힣]", heading) and ratio is not None and ratio < 0.05:
                removed["영어 번역 섹션 삭제"] += 1
                continue
            out.append(b)
        return out

    pages: dict[int, str] = {}
    for b in blocks:
        if b.get("page"):
            pages[b["page"]] = pages.get(b["page"], "") + block_text(b)
    ratios = {p: hangul_ratio(t) for p, t in pages.items()}
    if not any(r is not None and r >= 0.5 for r in ratios.values()):   # 한국어 쪽이 없으면 번역본이 아님
        return blocks
    english = {p for p, r in ratios.items() if r is not None and r < 0.2}
    removed["영어 번역 쪽 삭제"] += len(english)
    return [b for b in blocks if b.get("page") not in english]


def main():
    CLEAN.mkdir(parents=True, exist_ok=True)
    clean_cfg = load_sources().get("clean", {})
    drop_ranges = clean_cfg.get("drop_ranges", {})
    DROP_LINES.update(clean_cfg.get("drop_lines", []))
    report = {"run_at": now_iso(), "docs": {}}
    for path in sorted(EXTRACTED.glob("*.json")):
        doc = read_json(path)
        html = doc["source_type"] == "html"
        before = doc_stats(doc["blocks"])["chars"]
        blocks = doc["blocks"]
        if doc["doc_id"] in drop_ranges:
            blocks = apply_drop_ranges(blocks, [dict(r) for r in drop_ranges[doc["doc_id"]]])
        blocks = drop_english_translation(blocks, html)
        blocks = [b for b in (clean_block(b, html) for b in blocks) if b]
        doc = doc | {"blocks": blocks}
        write_json(CLEAN / path.name, doc)
        (CLEAN / f"{path.stem}.md").write_text(render_md(doc), encoding="utf-8")
        after = doc_stats(blocks)["chars"]
        report["docs"][doc["doc_id"]] = {"chars_before": before, "chars_after": after, "blocks": len(blocks)}

    report["removed"] = dict(removed)
    report["empty_docs"] = [d for d, s in report["docs"].items() if s["blocks"] == 0]
    write_json(REPORTS / f"clean_{today()}.json", report)
    b = sum(s["chars_before"] for s in report["docs"].values())
    a = sum(s["chars_after"] for s in report["docs"].values())
    print(f"문서 {len(report['docs'])}개 정제: 글자 {b:,} → {a:,} ({a / b:.1%}), 빈 문서 {report['empty_docs']}")
    for k, v in removed.most_common():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
