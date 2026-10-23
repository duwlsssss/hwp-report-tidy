"""(선택) 미해결 문단에 대한 단어 수정 '제안'만 받기. HWP 파일은 절대 수정하지 않는다.

사용: python ai_suggest.py "보고서_리포트_정리_xxx.csv"   (경로 생략 시 가장 최근 리포트)
- 문단마다 '전송될 내용'을 먼저 보여주고 y를 눌러야만 전송
- 해당 문단 한 개만 전송 (문서 전체·파일명 전송 안 함)
- 결과는 검토표(.md)로 저장 → 사람이 한글에서 직접 고침
필요: pip install anthropic, 환경변수 ANTHROPIC_API_KEY
"""
import os
import sys
from datetime import datetime
from pathlib import Path

MODEL = os.environ.get("HWP_AI_MODEL", "claude-sonnet-5-5")

PROMPT = """공공기관 보고서의 한 문단입니다. 줄 끝에서 '{word}' 단어가 '{cut}'처럼 잘립니다.
이 단어가 잘리지 않도록 그 앞부분 표현을 {n}글자 이상 줄이는 수정안을 주세요.
조건: 의미·수치·고유명사·법령명·기관명은 바꾸지 말 것. 중복 표현, 군더더기 조사·어미 위주로 줄일 것.
수정안 2개를 아래 형식으로만 답하세요.
1) "원래 표현" → "새 표현" (−k자)
2) "원래 표현" → "새 표현" (−k자)

문단:
{text}"""


def ask_ai(client, rec):
    content = PROMPT.format(word=rec["word"], cut=rec["cut"],
                            n=len(rec["cut"].split("|")[1]), text=rec["text"])
    args = dict(model=MODEL, max_tokens=8000, messages=[{"role": "user", "content": content}])
    try:
        msg = client.messages.create(thinking={"type": "disabled"}, **args)  # 짧은 수정안이라 생각 과정 불필요
    except Exception:
        msg = client.messages.create(**args)
    # 모델이 생각(thinking) 블록을 먼저 보낼 수 있어서, 텍스트 블록만 모은다
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    if getattr(msg, "stop_reason", "") == "max_tokens":
        text += "\n(답변이 길이 제한으로 잘렸습니다)"
    return text or "(제안을 받지 못했습니다)"


def load_report(path: Path):
    """fix_spacing.py가 만든 리포트 CSV → dict 목록"""
    import csv
    from fix_spacing import REPORT_COLS
    key = {h: k for k, h, _ in REPORT_COLS}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{key.get(h, h): v for h, v in row.items()} for row in csv.DictReader(f)]


def main():
    arg = " ".join(sys.argv[1:]).strip().strip('"').strip("'")
    if arg:
        path = Path(arg)
    else:
        # 경로를 안 주면 이 폴더와 상위 폴더에서 가장 최근 리포트 CSV를 쓴다
        here = Path(__file__).resolve().parent
        found = sorted(list(here.glob("*_리포트_*.csv")) + list(here.parent.glob("*_리포트_*.csv")),
                       key=lambda f: f.stat().st_mtime)
        if not found:
            sys.exit("리포트 CSV를 찾지 못했습니다. 경로를 따옴표 한 쌍으로 감싸서 넣어 주세요.\n"
                     '예) python ai_suggest.py "C:\\...\\보고서_리포트_미리보기_날짜.csv"')
        path = found[-1]
        print(f"가장 최근 리포트 사용: {path.name}")
    if not path.is_file():
        sys.exit(f"파일이 아닙니다: {path}\n경로를 따옴표 한 쌍(\"...\")으로만 감싸 주세요.")
    records = load_report(path)
    targets = [r for r in records if str(r["result"]).startswith("미해결")]
    if not targets:
        sys.exit("미해결 행이 없습니다. 테스트하려면 --check --max-steps 1 로 리포트를 다시 만드세요.")
    try:
        import anthropic  # noqa: F401
    except ImportError:
        sys.exit("anthropic 패키지가 없습니다. 먼저 설치하세요: pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("API 키가 없습니다. 먼저 설정하세요: $env:ANTHROPIC_API_KEY = \"sk-ant-...\"")
    print(f"미해결 문단 {len(targets)}개. 문단마다 전송 여부를 묻습니다.\n")

    client = None
    lines = ["# 단어 수정 제안 검토표", f"- 리포트: {path.name}", f"- 모델: {MODEL}", ""]
    for r in targets:
        print("=" * 60)
        print(f"[{r['page']}쪽 / {r.get('where','본문')} \"{r.get('start','')}\" {r['line']}줄] 잘린 단어 '{r['cut']}'")
        print("▼ 외부 AI로 전송될 내용 (이 문단만)\n" + r["text"] + "\n")
        a = input("전송할까요? [y]예 [Enter]건너뜀 [q]종료: ").strip().lower()
        if a == "q":
            break
        head = f"## {r['page']}쪽 / {r.get('where','본문')} \"{r.get('start','')}\" {r['line']}줄 (잘린 단어 '{r['cut']}')"
        if a != "y":
            lines += [head, "- 전송 안 함 (직접 수정)", ""]
            continue
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        reply = ask_ai(client, r)
        print("\n▼ AI 제안\n" + reply + "\n")
        has = [k for k in ("1", "2") if f"{k})" in reply]
        opts = " / ".join([f"{k} = {k}안 채택" for k in has] + ["e = 직접 고쳐 쓰기", "Enter = 보류"])
        print("→ 검토표에 어떻게 처리할지 기록합니다. 한글 문서는 바뀌지 않습니다.")
        d = input(f"선택 ({opts}): ").strip().lower()
        decision = {"1": "1안 채택", "2": "2안 채택"}.get(d, "보류")
        if d == "e":
            decision = "직접 수정: " + input("수정 내용: ")
        lines += [head, "**전송 내용**", "", "> " + r["text"], "", "**AI 제안**", "", reply, "",
                  f"**판단**: {decision}  ← 한글에서 직접 반영", ""]

    out = path.with_name(f"{path.stem}_단어제안_{datetime.now():%Y%m%d_%H%M}.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n검토표 저장: {out}")


if __name__ == "__main__":
    main()
