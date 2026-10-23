"""한글 보고서 정리 자동화 (AI 사용 안 함)

순서: ① 내어쓰기 정렬 → ② 잘린 단어 자간 조정 → 끝에 체크리스트 출력
(내어쓰기가 줄바꿈을 바꾸므로 자간보다 먼저)

사용:
  python fix_spacing.py 보고서.hwp --check      # 미리 보기: 표 + 체크리스트 + CSV (문서 수정 없음)
  python fix_spacing.py 보고서.hwp              # 실제 적용: 정리된 한글 파일 + CSV
  기능 골라 실행: --only 자간 | 내어쓰기 | 오탈자  (기본: 자간 + 오탈자, 내어쓰기는 지정할 때만)
  표 제외: --body-only

규칙
- 원본은 열지 않는다: 임시 복사본 작업 → '원본명_정리_날짜.hwp'
- 본문 + 표 안 문단 처리 (글상자·머리말 제외)
- 내어쓰기: 여러 줄 문단 중 □ ○ - 1. 가. ① 등 기호로 시작하고 아직 내어쓰기가 없는 문단만,
  기호 뒤 첫 글자에 맞춤 (본문 Shift+Tab / 표 Ctrl+Shift+Tab과 같은 명령)
- 자간: 줄 나눔 기준 '글자'에서 줄 끝 단어가 다음 줄로 잘린 곳을 찾아(한글·영어·숫자)
  ① 그 줄 + 잘린 단어를 좁혀(Alt+Shift+N) 뒷부분을 윗줄로 올리고,
  ② 안 되면 그 줄만 넓혀(Alt+Shift+W) 앞부분을 아랫줄로 내린다. 각각 --max-steps까지.
  단어가 한 줄로 모이고 문단 줄 수가 늘지 않으면 해결. 안 되면 원상복구 + 잘린 단어 형광펜 + '미해결(직접 확인 필요)'
- 오탈자: 연속 공백·반복 단어·문장부호·괄호 짝·자주 틀리는 말(typos.txt로 추가)을 찾기만 한다.
  고치지 않음 → CSV와 체크리스트로 알려주면 사람이 판단
- 스타일: 삭제하지 않는다. 안 쓰는 스타일을 체크리스트로 알려주면 사람이 직접 정리
"""
import argparse
import csv
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

from hwp_ops import MARKER, HwpDoc
from hwpx_lines import lines_of, paragraphs
from typo_check import check as typo_scan, load_dict


def do_indent(doc, lst, pid, dry_run):
    if len(doc.line_starts(lst, pid)[0]) < 2:
        return None  # 한 줄짜리
    m = MARKER.match(doc.raw_text(lst, pid))
    if not m or doc.has_hanging_indent(lst, pid):
        return None  # 기호 문단이 아니거나 이미 내어쓰기 돼 있음 → 건드리지 않음
    pos = doc.pos_after(lst, pid, m.group(0))
    if pos is None:
        return None  # 위치를 정확히 못 찾으면 건드리지 않음
    if not dry_run:
        doc.indent_at(lst, pid, pos)
    return m.group(0).strip()


def is_word(c: str) -> bool:
    """단어를 이루는 글자: 한글·영문·숫자 (공백·문장부호는 아님)"""
    return c.isalnum()


def find_split(lines, start=0):
    """줄 끝에서 단어가 잘린 첫 줄 번호 i (i줄 끝 + i+1줄 앞이 한 단어). 없으면 None"""
    for i in range(start, len(lines) - 1):
        a, b = lines[i][1], lines[i + 1][1]
        if a and b and is_word(a[-1]) and is_word(b[0]):
            return i
    return None


def split_word(lines, i):
    """잘린 단어 (앞부분, 뒷부분)"""
    a, b = lines[i][1], lines[i + 1][1]
    k = len(a)
    while k > 0 and is_word(a[k - 1]):
        k -= 1
    m = 0
    while m < len(b) and is_word(b[m]):
        m += 1
    return a[k:], b[:m]


def word_ok(lines, w0, w1, before) -> bool:
    """[w0, w1) 단어 사이에 줄 시작이 없고, 문단 줄 수가 늘지 않았으면 해결"""
    return len(lines) <= before and not any(w0 < a < w1 for a, _ in lines)


def context_of(text, a, b, tail):
    """한글 '찾기'에 넣을 문자열: 잘린 지점 앞 최대 12자 + 잘린 단어 뒷부분 (줄바꿈·탭 제외)"""
    start = max(a, b - 12, text.rfind("\n", 0, b) + 1, text.rfind("\t", 0, b) + 1)
    return start, text[start:b + len(tail)]


def nth_in_doc(paras, pi, start, ctx):
    """ctx가 문서에서 몇 번째로 나오는지 (pi번째 문단의 start 위치 기준, 0부터)"""
    n = sum(t.count(ctx) for t, _ in paras[:pi])
    return n + paras[pi][0][:start + len(ctx) - 1].count(ctx)


def progress(done, total, width=30):
    """한 줄 진행 막대: [██████░░░░] 60% (30/50)"""
    frac = done / total if total else 1
    fill = int(width * frac)
    # 끝에 공백을 붙여 이전에 더 길게 찍힌 글자가 남지 않게 함
    print(f"\r  [{'█' * fill}{'░' * (width - fill)}] {frac:4.0%} ({done}/{total})    ", end="", flush=True)


def fix_splits(doc, max_steps, dry_run, mark, tables):
    """파일에 저장된 줄 정보로 잘린 단어를 찾아 하나씩 처리.
    반환: (처리 기록 목록, 표 출력용 묶음)"""
    records, groups, tried = [], {}, set()
    last = None  # (기록, 문단 번호, 원래 줄 위치) — 되돌린 뒤 원래대로인지 확인용
    for _ in range(2000):
        paras = paragraphs(doc.snapshot())
        if last:
            rec, pi0, orig = last
            if paras[pi0][1] != orig and (dry_run or rec["result"].startswith("미해결")):
                rec["warn"] = "줄 배치가 바뀌었을 수 있음 - 원본과 비교 필요"
            last = None

        # 남은 잘린 단어 전부 세기 (앞 단어를 고치면 뒤 줄이 밀려 새로 잘리는 단어가 생길 수 있음)
        target, left = None, 0
        for pi, (text, pos) in enumerate(paras):
            lines = lines_of(text, pos)
            cur = 0
            while (i := find_split(lines, cur)) is not None:
                head, tail = split_word(lines, i)
                key = (pi, lines[i + 1][0] - len(head))
                if key not in tried:
                    left += 1
                    if not target:
                        target = (pi, text, pos, lines, i, head, tail, key)
                cur = i + 1
        progress(len(tried), len(tried) + left)
        if not target:
            break

        pi, text, pos, lines, i, head, tail, key = target
        tried.add(key)
        a, b = lines[i][0], lines[i + 1][0]
        w0, w1 = b - len(head), b + len(tail)
        start, ctx = context_of(text, a, b, tail)
        loc = doc.find_nth(ctx, nth_in_doc(paras, pi, start, ctx))
        rec = {"type": "잘린 단어", "page": loc[3] if loc else "", "where": "",
               "start": text[:15], "line": i + 1, "cut": f"{head}|{tail}", "word": head + tail,
               "steps": 0, "result": "", "warn": "", "text": text}
        if not loc:
            rec["result"] = "미해결(직접 확인 필요)"
            rec["warn"] = "문서에서 위치를 찾지 못함 - 직접 확인 필요"
            records.append(rec)
            continue
        lst, pid, spos = loc[0], loc[1], loc[2]
        if lst != 0 and not tables:
            tried.add(key)
            continue
        rec["where"] = "본문" if lst == 0 else "표"
        pb = spos + (b - start)                    # 잘린 지점의 실제 커서 위치
        s_, e_ = pb - (b - a), pb + len(tail)      # 잘린 줄 시작 ~ 잘린 단어 끝
        before = len(lines)

        tries = [("좁힘", s_, e_, -1), ("넓힘", s_, pb, +1)]
        solved, how = 0, ""
        for name, x0, x1, d in tries:
            for step in range(1, max_steps + 1):
                doc.spacing_range(lst, pid, x0, x1, d)
                now = lines_of(*paragraphs(doc.snapshot())[pi])
                if word_ok(now, w0, w1, before):
                    solved, how = step, name
                    break
            if solved:
                break
            doc.undo(max_steps)                    # 이번 방향 시도 전부 취소

        if solved:
            rec["steps"] = solved if how == "좁힘" else -solved
            rec["result"] = f"{'해결 가능' if dry_run else '해결'}({how})"
            if dry_run:
                doc.undo(solved)
        else:
            rec["result"] = "미해결(직접 확인 필요)"
            if mark and not dry_run:
                doc.mark(lst, pid, pb - len(head), e_)
        records.append(rec)
        last = (rec, pi, pos)

        g = groups.setdefault(pi, [rec["page"], rec["where"], "", lines, []])
        g[4].append(rec)
    print("\n")  # 진행 막대 다음 줄 + 빈 줄
    return records, [tuple(g) for g in groups.values()]


def joined(lines, i, word) -> bool:
    """잘렸던 단어가 이제 i줄 또는 i+1줄 한 줄 안에 온전히 있는지"""
    return any(word in t for _, t in lines[i:i + 2])


def has_object(lines) -> bool:
    """줄 텍스트에 줄바꿈이 섞여 있으면 글상자·표 등 개체가 들어 있는 문단 → 건너뜀"""
    return any("\n" in t or "\r" in t for _, t in lines)


def _w(t: str) -> int:
    """터미널 표시 폭 (한글 등 전각 = 2칸)"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in t)


def _fit(t: str, width: int, keep_end: bool = False) -> str:
    """폭에 맞게 자른다. keep_end면 앞을 잘라 끝부분을 남긴다"""
    t = t.replace("\t", " ")
    if _w(t) <= width:
        return t
    src = t[::-1] if keep_end else t
    out = ""
    for c in src:
        if _w(out + c) > width - 1:
            break
        out += c
    return "…" + out[::-1] if keep_end else out + "…"


def _cell(t: str, width: int) -> str:
    t = _fit(t, width)
    return t + " " * (width - _w(t))


HL, END = "\x1b[30;43m", "\x1b[0m"   # 노란 바탕 검은 글씨


def _line_cell(t: str, width: int, head: str = "", tail: str = "", color: bool = True) -> str:
    """줄 내용 칸. head = 줄 끝 잘린 앞부분, tail = 줄 앞 잘린 뒷부분 → 강조"""
    room = width if color else width - 2 * (bool(head) + bool(tail))  # [ ] 자리 확보
    t = _fit(t, room, keep_end=bool(head))  # 잘린 부분이 끝에 있으면 끝을 남김
    pad = " " * (room - _w(t))
    on, off = (HL, END) if color else ("[", "]")
    if tail and t.startswith(tail):
        t = on + tail + off + t[len(tail):]
    if head and t.endswith(head):
        t = t[: len(t) - len(head)] + on + head + off
    return t + pad


def print_table(groups, n, text_width=46):
    """잘린 단어가 있는 문단을 표로 출력 (한 행 = 한글 화면의 한 줄)
    groups: [(쪽, 위치, 기호, 줄 배치, 처리 기록)]"""
    if not groups:
        print("잘린 단어가 있는 문단 없음")
        return
    color = sys.stdout.isatty()
    if color and os.name == "nt":
        os.system("")  # Windows 터미널 색상 켜기
    cols = [("쪽", 4), ("위치", 4), ("기호", 4), ("줄", 3), ("줄 내용", text_width),
            ("잘린 단어", 16), ("결과", 16)]
    bar = "-+-".join("-" * w for _, w in cols)
    print(" | ".join(_cell(name, w) for name, w in cols))
    print(bar)
    for page, kind, mk, lines, recs in groups[:n]:
        by_line = {r["line"] - 1: r for r in recs}
        heads, tails = {}, {}
        for i, r in by_line.items():
            h, t = r["cut"].split("|", 1)
            heads[i], tails[i + 1] = h, t
        for no, (_, t) in enumerate(lines):
            first, r = no == 0, by_line.get(no)
            res = ""
            if r:
                res = "⚠ 직접 확인 필요" if r["result"].startswith("미해결") else \
                    f"{'좁힘' if r['steps'] > 0 else '넓힘'} {abs(r['steps'])}단계"
            row = [str(page) if first else "", kind if first else "", mk if first else "",
                   str(no + 1), None, r["cut"] if r else "", res]
            print(" | ".join(_cell(v, w) if v is not None else
                             _line_cell(t, w, heads.get(no, ""), tails.get(no, ""), color)
                             for v, (_, w) in zip(row, cols)))
        print(bar)
    if len(groups) > n:
        print(f"... 외 {len(groups) - n}개 문단은 CSV 리포트에서 확인 (표에 더 보기: --show-n 100)")
    print("→ 구분선(---) 사이 = 한 문단, 그 안의 각 행 = 한글 화면에서 보이는 한 줄. 노란색(또는 [ ]) = 잘린 단어.")


def print_typos(typos, n):
    """오탈자 의심 목록 표"""
    color = sys.stdout.isatty()
    cols = [("쪽", 4), ("위치", 4), ("유형", 14), ("찾은 부분", 40), ("제안", 12)]
    bar = "-+-".join("-" * w for _, w in cols)
    print("\n[오탈자 의심]")
    print(" | ".join(_cell(name, w) for name, w in cols))
    print(bar)
    for t in typos[:n]:
        found = _fit(t["cut"], 40)
        pad = " " * (40 - _w(found))
        if color and "[" in found and "]" in found:
            found = found.replace("[", HL, 1).replace("]", END, 1)
            pad += "  "  # [ ] 두 칸이 색으로 바뀐 만큼
        row = [_cell(str(t["page"]), 4), _cell(t["where"], 4), _cell(t["kind"], 14),
               found + pad, _cell(t["suggest"], 12)]
        print(" | ".join(row))
    if len(typos) > n:
        print(f"... 외 {len(typos) - n}개는 CSV에서 확인")


OK, BAD = "✅", "🚨"


def checklist(items):
    """[(통과 여부, 내용, 주의, 할 일)] → 한 항목 한 줄로 출력"""
    print("\n[검사 결과]")
    for ok, text, note, todo in items:
        line = f"{OK if ok else BAD} {text}"
        if note:
            line += f" (⚠️ {note})"
        print(line)
        if todo and not ok:
            print(f"   → {todo}")


def style_item(doc):
    unused = doc.unused_styles()
    if not unused:
        return (True, "안 쓰는 스타일 없음", "", "")
    names = [n for _, n, _ in unused]
    shown = ", ".join(names[:10]) + (f" 외 {len(names) - 10}개" if len(names) > 10 else "")
    return (False, f"안 쓰는 스타일 {len(names)}개: {shown}", "",
            "F6(스타일) 창에서 직접 확인 후 삭제하세요")


REPORT_COLS = [  # (키, CSV 머리글, 열 너비)
    ("type", "구분", 8), ("page", "쪽", 6), ("where", "위치", 6), ("start", "문단 시작", 22),
    ("line", "줄", 5), ("cut", "찾은 부분", 20), ("word", "단어", 12),
    ("suggest", "제안", 10), ("steps", "조정 자간 단계", 12), ("result", "결과", 16),
    ("warn", "주의", 24), ("text", "문단 전체", 60),
]


def write_report(records, path: Path):
    """리포트를 CSV로 저장 (utf-8-sig라 엑셀에서 바로 열면 한글 안 깨짐)"""
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([h for _, h, _ in REPORT_COLS])
        for r in records:
            row = dict(r)
            st = r.get("steps")
            if isinstance(st, int):
                row["steps"] = "" if st == 0 else f"{'좁힘' if st > 0 else '넓힘'} {abs(st)}단계"
            w.writerow([row.get(k, "") for k, _, _ in REPORT_COLS])


def main():
    ap = argparse.ArgumentParser(description="한글 보고서 정리 자동화")
    ap.add_argument("src")
    ap.add_argument("--check", action="store_true",
                    help="미리 보기: 실제로 해보고 되돌림. 표 + 체크리스트 + CSV (문서 수정 없음)")
    ap.add_argument("--show-n", type=int, default=30, help="--check 표에 보여줄 문단 수")
    ap.add_argument("--max-steps", type=int, default=10, help="단어 하나당 자간 좁게 최대 횟수")
    ap.add_argument("--body-only", action="store_true", help="표 안 문단 제외")
    ap.add_argument("--only", nargs="+", metavar="기능",
                    help="이 기능만 실행: 자간 내어쓰기 오탈자 (기본: 자간 오탈자)")
    ap.add_argument("--no-mark", action="store_true", help="미해결 단어 형광펜 생략")
    ap.add_argument("--show", action="store_true", help="한글 창 띄우기")
    args = ap.parse_args()
    check = args.check

    feature = {"자간": "spacing", "spacing": "spacing", "내어쓰기": "indent", "indent": "indent",
               "오탈자": "typo", "typo": "typo"}
    if args.only:
        bad = [x for x in args.only if x not in feature]
        if bad:
            sys.exit(f"--only에 쓸 수 있는 값: 자간, 내어쓰기, 오탈자 (입력: {bad})")
        run = {feature[x] for x in args.only}
    else:
        run = {"spacing", "typo"}  # 내어쓰기는 --only 내어쓰기 로 지정할 때만

    src = Path(args.src).resolve()
    if not src.exists():
        sys.exit(f"파일 없음: {src}")

    work = Path(tempfile.mkdtemp()) / src.name
    shutil.copy2(src, work)  # 원본 보호
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    tag = "미리보기" if check else "정리"
    tables = not args.body_only

    doc = HwpDoc(work, visible=args.show)
    records, groups, typos, indented, items = [], [], [], 0, []
    done, changed = False, False
    try:
        # ① 내어쓰기 (--check면 대상만 셈, 실제 적용은 --only 내어쓰기 일 때만)
        apply_indent = "indent" in run and not check
        count_indent = "indent" in run or (check and not args.only)
        if count_indent:
            for lst, pid, kind in list(doc.paragraphs(tables)):
                mk = do_indent(doc, lst, pid, not apply_indent)
                if mk:
                    indented += 1
                    if apply_indent:
                        print(f"[내어쓰기] {doc.page_of(lst, pid)}쪽 {kind} \"{doc.text(lst, pid)[:15]}\" → '{mk}' 뒤에 맞춤")

        # ② 자간
        if "spacing" in run:
            print("잘린 단어를 찾아 자간 조정을 " + ("시험하는 중... (문서는 바뀌지 않습니다)" if check else "적용하는 중...") + "\n")
            records, groups = fix_splits(doc, args.max_steps, check, not args.no_mark, tables)
            groups = [(pg, wh, (MARKER.match(ls[0][1]).group(0).strip() if ls and MARKER.match(ls[0][1]) else ""), ls, rs)
                      for pg, wh, _, ls, rs in groups]
            if check:
                print_table(groups, args.show_n)
            else:
                for r in records:
                    print(f"[자간] {r['page']}쪽 {r['where']} \"{r['start']}\" {r['line']}줄 '{r['cut']}' → {r['result']}")

        # ③ 오탈자 (찾기만) — 파일에서 문단을 읽어 표 안의 표까지 검사
        if "typo" in run:
            words = load_dict(Path(__file__).resolve().parent)
            paras = paragraphs(doc.snapshot())
            for pi, (text, _) in enumerate(paras):
                flat = text.replace("\n", " ")
                found = typo_scan(flat, words) if flat.strip() else []
                if not found:
                    continue
                key = re.split(r"[\n\t]", text)[0][:20] or flat[:20]
                loc = doc.find_nth(key, nth_in_doc(paras, pi, 0, key)) if key.strip() else None
                if loc and loc[0] != 0 and not tables:
                    continue
                page, kind = (loc[3], "본문" if loc[0] == 0 else "표") if loc else ("", "")
                for name, where, hint in found:
                    word = where[where.find("[") + 1:where.find("]")] if "[" in where else ""
                    typos.append({"type": "오탈자", "page": page, "where": kind, "start": flat[:15],
                                  "cut": where, "word": word, "kind": name, "result": name,
                                  "suggest": hint, "text": flat})
            if typos:
                print_typos(typos, args.show_n)

        # 체크리스트
        bad = [r for r in records if r["result"].startswith("미해결")]
        tight = [r for r in records if not r["result"].startswith("미해결") and abs(r["steps"]) >= 8]
        warns = [r for r in records if r["warn"]]
        if "spacing" in run:
            ok_n = len(records) - len(bad)
            notes = []
            if tight:
                notes.append(f"자간을 8단계 이상 바꾸는 곳 {len(tight)}개")
            if warns:
                notes.append(f"되돌린 뒤 줄바꿈이 달라진 곳 {len(warns)}개")
            text = (f"잘린 단어 {len(records)}개 중 {ok_n}개 {'해결 가능' if check else '해결'}"
                    if records else "잘린 단어 없음")
            if bad:
                text += f", {len(bad)}개 직접 확인 필요"
            items.append((not bad, text, ", ".join(notes),
                          "CSV 결과='미해결' 행" + ("" if check else " / 문서의 노란 형광펜") +
                          "을 직접 다듬거나 ai_suggest.py로 제안 받기"))
        if apply_indent:
            items.append((True, f"내어쓰기 적용 {indented}개 (이미 내어쓰기 된 문단은 건너뜀)",
                          "목록에 없는 기호로 시작하는 문단은 확인 불가", ""))
        elif check and count_indent:
            items.append((indented == 0, f"내어쓰기 안 된 기호 문단 {indented}개",
                          "목록에 없는 기호로 시작하는 문단은 확인 불가",
                          "직접 Shift+Tab 하거나 --only 내어쓰기 로 적용"))
        if "typo" in run:
            if typos:
                kinds = {}
                for t in typos:
                    kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
                detail = ", ".join(f"{k} {v}" for k, v in kinds.items())
                items.append((False, f"오탈자 의심 {len(typos)}개: {detail}", "",
                              "CSV 구분='오탈자' 행을 보고 직접 수정 (자동으로 고치지 않음)"))
            else:
                items.append((True, "오탈자 의심 없음", "", ""))
        items.append(style_item(doc))

        changed = any(not r["result"].startswith("미해결") for r in records) or indented \
            or (records and not args.no_mark)
        if not check and changed:
            out = src.parent / f"{src.stem}_{tag}_{stamp}{src.suffix}"
            doc.save_as(out)
            print(f"\n저장: {out}")
        done = True
    finally:
        doc.close()
        shutil.rmtree(work.parent, ignore_errors=True)

    if done:
        checklist(items)
        if records or typos:
            report = src.parent / f"{src.stem}_리포트_{tag}_{stamp}.csv"
            write_report(records + typos, report)
            print(f"리포트: {report}")
        else:
            print("확인할 항목이 없어 리포트를 만들지 않았습니다.")
        if not check and not changed:
            print("바꿀 곳이 없어 한글 파일을 따로 저장하지 않았습니다.")
        if check and records:
            print("실제 적용: --check 없이 같은 명령 실행")


if __name__ == "__main__":
    main()
