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
2. 개인 연락처: 이메일 주소 삭제. 전화번호는 모두 부서·업무 단위(사람 이름 없음)라 남김 (2026-11-02 확인)
3. 버튼·메뉴 글자: '고려대학교 홈페이지 바로가기' 같은 버튼만 있는 줄 삭제, 줄 끝 '바로가기'·'☜클릭' 삭제
4. PDF 쪽 번호('- 1 -') 삭제
PDF 줄바꿈은 합치지 않는다: 단어 중간 줄바꿈인지 띄어쓰기 자리인지 PDF에서 알 수 없어서, 잘못 붙이면 오히려 단어가 틀어짐.
"""
import re
import sys
from collections import Counter

from common import DATA, REPORTS, now_iso, read_json, today, write_json
from extract import EXTRACTED, doc_stats, render_md

CLEAN = DATA / "clean"

BUTTON_LINES = {"고려대학교 홈페이지 바로가기", "교육과정표 바로가기", "새창으로 열기", "포털시스템", "FAQ 바로가기"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?:SEClipBoard_synap)?")
PAGE_NO = re.compile(r"^-\s*\d+\s*-$")
SEJONG_PAREN = re.compile(r"\s*\(세종[^()]*\)")                 # (세종: 044-860-1126), (세종)
SEJONG_SLASH = re.compile(r"\s*/\s*[^/]*\(세종\)[^/]*?(?=\s*/|$)")  # / 학부-학생생활지원팀(세종) ☜클릭
SEJONG_COMMA = re.compile(r",\s*세종[^,)/]*")                    # , 세종 학생복지팀

removed = Counter()


def is_sejong_only(text: str) -> bool:
    return "세종" in text and "서울" not in text


def clean_line(line: str, html: bool) -> str | None:
    """줄 하나를 정제. 지워야 하면 None."""
    indent = re.match(r"^\s*(-\s)?", line).group(0)
    body = line[len(indent):].strip()

    if body in BUTTON_LINES:
        removed["버튼 줄"] += 1
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
        drop_cols = {i for i, c in enumerate(header) if "세종" in c and "서울" not in c}
        if drop_cols:
            removed["세종 열 삭제"] += len(drop_cols)
            rows = [[c for i, c in enumerate(r) if i not in drop_cols] for r in rows]
        kept = []
        for r in rows:
            if any(is_sejong_only(c) for c in r) and not any("서울" in c for c in r):
                removed["세종 행 삭제"] += 1
                continue
            kept.append([re.sub(r"서울\s*,\s*세종", "서울", c) for c in r])
        rows = kept
    rows = [[clean_line(c, html) or "" for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    return block | {"rows": rows} if rows else None


def clean_block(block: dict, html: bool) -> dict | None:
    if html and any("세종캠퍼스" in p for p in block["section_path"]):
        removed["세종 블록 삭제"] += 1
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


def main():
    CLEAN.mkdir(parents=True, exist_ok=True)
    report = {"run_at": now_iso(), "docs": {}}
    for path in sorted(EXTRACTED.glob("*.json")):
        doc = read_json(path)
        html = doc["source_type"] == "html"
        before = doc_stats(doc["blocks"])["chars"]
        blocks = [b for b in (clean_block(b, html) for b in doc["blocks"]) if b]
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
