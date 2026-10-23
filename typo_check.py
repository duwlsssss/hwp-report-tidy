"""오탈자 검사 (AI 없이 규칙 + 사전). 문서는 고치지 않고 찾기만 한다.

사전 추가: 이 파일과 같은 폴더의 typos.txt 에 한 줄씩 '틀린말=맞는말'
  예) 역활=역할
"""
from __future__ import annotations

import re
from pathlib import Path

# 자주 틀리는 표기 (뜻이 갈리는 말은 일부러 뺌: 결재/결제, 바램/바람 등)
BUILTIN = {
    "몇일": "며칠", "금새": "금세", "됬": "됐", "웬지": "왠지", "일일히": "일일이",
    "틈틈히": "틈틈이", "깨끗히": "깨끗이", "오랫만": "오랜만", "희안": "희한",
    "역활": "역할", "댓가": "대가", "갯수": "개수", "촛점": "초점", "어의없": "어이없",
    "설겆이": "설거지", "곰곰히": "곰곰이", "꼼꼼이": "꼼꼼히", "번번히": "번번이",
    "몇 일": "며칠", "할려고": "하려고", "할께": "할게", "구지": "굳이",
}

PAIRS = [("(", ")"), ("[", "]"), ("{", "}"), ("「", "」"), ("『", "』"),
         ("‘", "’"), ("“", "”"), ("<", ">"), ("《", "》"), ("〈", "〉")]

RULES = [
    # (유형, 정규식, 제안)
    ("연속 공백", re.compile(r"(?<=\S) {2,}(?=\S)"), "공백 1칸"),
    ("반복 단어", re.compile(r"(?<![\w])(\w{2,})\s+\1(?![\w])"), "중복 삭제"),
    # 콜론(:)은 '구분 : 내용'처럼 일부러 띄우는 경우가 많아 제외
    ("문장부호 앞 공백", re.compile(r"(?<=\S) +[.,;!?)\]」』’”]"), "공백 삭제"),
    ("중복 문장부호", re.compile(r"(?<!\.)\.\.(?!\.)|,,|。。"), "하나만"),
]


def load_dict(folder: Path) -> dict:
    words = dict(BUILTIN)
    f = folder / "typos.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                wrong, right = (x.strip() for x in line.split("=", 1))
                if wrong:
                    words[wrong] = right
    return words


def check(text: str, words: dict):
    """[(유형, 찾은 부분(앞뒤 포함), 제안)]"""
    out = []

    def ctx(s, e, pad=6):
        return text[max(0, s - pad):s] + "[" + text[s:e] + "]" + text[e:e + pad]

    for name, rx, hint in RULES:
        if name == "연속 공백" and len(text.strip()) <= 10:
            continue  # '구  분', '비  고' 같은 표 머리글의 일부러 띄운 공백
        for m in rx.finditer(text):
            out.append((name, ctx(m.start(), m.end()), hint))

    for wrong, right in words.items():
        for m in re.finditer(re.escape(wrong), text):
            out.append(("자주 틀리는 말", ctx(m.start(), m.end()), right))

    for a, b in PAIRS:
        if text.count(a) != text.count(b):
            out.append(("괄호·따옴표 짝", f"{a} {text.count(a)}개 / {b} {text.count(b)}개", "짝 맞추기"))
    for q in ('"', "'"):
        if text.count(q) % 2:
            out.append(("괄호·따옴표 짝", f"{q} {text.count(q)}개(홀수)", "짝 맞추기"))
    return out
