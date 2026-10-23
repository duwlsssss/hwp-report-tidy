"""HWPX 파일에 저장된 줄 나눔 정보(linesegarray) 읽기.
한글이 화면에 그린 줄 시작 위치가 그대로 들어 있어 커서로 줄을 읽는 것보다 정확하다."""
from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path

_P = re.compile(r"<hp:p [^>]*>(.*?)<hp:linesegarray>(.*?)</hp:linesegarray>", re.S)
_T = re.compile(r"<hp:t>(.*?)</hp:t>|<hp:t/>", re.S)


def paragraphs(path: Path):
    """문서 순서대로 [(문단 텍스트, [줄 시작 위치...])]. 표 안(중첩 포함) 문단도 포함"""
    out = []
    with zipfile.ZipFile(path) as z:
        secs = sorted(n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n))
        xml = "".join(z.read(n).decode("utf-8") for n in secs)
    for m in _P.finditer(xml):
        body, segs = m.group(1), m.group(2)
        if "<hp:p " in body:                       # 표를 품은 바깥 문단 → 안쪽 문단만
            body = body[body.rfind("<hp:p "):]
        if "</hp:tbl>" in body or "</hp:p>" in body:
            continue
        text = ""
        for t in _T.finditer(body):
            seg = t.group(1) or ""
            seg = re.sub(r"<hp:lineBreak\s*/>", "\n", seg)
            seg = re.sub(r"<hp:tab[^>]*/>", "\t", seg)
            seg = re.sub(r"<[^>]+>", "", seg)
            text += html.unescape(seg)
        pos = [int(x) for x in re.findall(r'textpos="(\d+)"', segs)]
        out.append((text, pos))
    return out


def lines_of(text: str, pos):
    """[(시작 위치, 줄 텍스트)] — 강제 줄바꿈 문자는 줄 끝에서 뺀다"""
    bounds = pos + [len(text)]
    return [(a, text[a:b].rstrip("\n")) for a, b in zip(bounds, bounds[1:]) if a <= len(text)]
