"""한글(HWP) 조작 래퍼. COM 호출은 전부 이 파일에만 둔다.
요구: Windows + 한글 설치 + pip install pyhwpx

문단 위치는 (리스트ID, 문단번호)로 다룬다.
- 리스트 0 = 본문, 그 외 = 표 셀·글상자·머리말 등
"""
from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path

BODY = 0
YELLOW = 0x00FFFF        # 한글 색상은 BGR

# 문단 앞 기호: □ ○ - ※ 1. 1) (1) 가. 가) (가) ① 등 + 뒤 공백
MARKER = re.compile(
    r"^[ \t]*(?:[□■○●◦◎◇◆▶▷►•∙·※＊*\-–—]"
    r"|\d{1,2}[.)]|[가-하][.)]|\(\d{1,2}\)|\([가-하]\)|[①-⑳]|[ⅰ-ⅹⅠ-Ⅹ][.)]?)[ \t]+"
)

# 문서 구조에 쓰이는 기본 스타일은 미사용이어도 지우지 않음
PROTECTED_STYLES = {"바탕글", "쪽 번호", "머리말", "각주", "미주", "메모"}


class HwpDoc:
    def __init__(self, path: Path, visible: bool = False):
        from pyhwpx import Hwp  # 보안 모듈 등록까지 pyhwpx가 처리
        self._hwp = Hwp(visible=visible)
        self.com = getattr(self._hwp, "hwp", self._hwp)
        self.fmt = "HWPX" if path.suffix.lower() == ".hwpx" else "HWP"
        self._tmpdirs = []
        if not self.com.Open(str(path), self.fmt, "forceopen:true"):
            raise RuntimeError(f"파일 열기 실패: {path}")

    # ---------- 기본 ----------
    def run(self, action: str):
        return self.com.HAction.Run(action)

    def _key(self):
        """상태 표시줄: (쪽, 줄, 칸, 컨트롤이름)"""
        k = self.com.KeyIndicator()
        return k[3], k[5], k[6], (k[8] or "")

    def goto(self, lst: int, pid: int, pos: int = 0) -> bool:
        self.com.SetPos(lst, pid, pos)
        l, p, _ = self.com.GetPos()
        return l == lst and p == pid

    def table_cell_lists(self):
        """본문에 있는 표들의 셀 리스트ID 목록 (표 안의 표는 제외).
        리스트ID를 번호로 추측하지 않고, 표 개체를 하나씩 찾아 들어가 셀을 돈다."""
        found, seen = [], set()
        ctrl = self.com.HeadCtrl
        while ctrl:
            if ctrl.CtrlID == "tbl":
                try:
                    self.com.SetPosBySet(ctrl.GetAnchorPos(0))
                    self.com.FindCtrl()
                    self.run("ShapeObjTableSelCell")   # 첫 셀로 들어감
                    for _ in range(2000):                 # 행 단위로 왼→오, 위→아래
                        row_start = self.com.GetPos()[0]
                        for _ in range(200):
                            lst = self.com.GetPos()[0]
                            if lst not in seen:
                                seen.add(lst)
                                found.append(lst)
                            self.run("TableRightCell")
                            if self.com.GetPos()[0] == lst:
                                break
                        self.goto(row_start, 0)
                        self.run("TableLowerCell")
                        if self.com.GetPos()[0] == row_start:
                            break
                    self.run("Cancel")
                except Exception:
                    pass
            ctrl = ctrl.Next
        self.run("MoveDocBegin")
        return found

    def paragraphs(self, tables: bool = True):
        """(리스트ID, 문단번호, 위치구분)을 본문 → 표 순서로"""
        lists = [(BODY, "본문")]
        if tables:
            lists += [(l, "표") for l in self.table_cell_lists()]
        for lst, kind in lists:
            pid = 0
            while self.goto(lst, pid):
                yield lst, pid, kind
                pid += 1

    # ---------- 측정 ----------
    def page_of(self, lst: int, pid: int, pos: int = 0) -> int:
        """해당 위치의 쪽 번호"""
        self.goto(lst, pid, pos)
        return self._key()[0]

    def line_starts(self, lst: int, pid: int):
        """문단 안 각 줄의 시작 위치(글자 위치) 목록 + 문단 끝 위치"""
        self.goto(lst, pid)
        self.run("MoveParaBegin")
        starts = []
        while True:
            l, p, pos = self.com.GetPos()
            if l != lst or p != pid or (starts and pos <= starts[-1]):
                break
            starts.append(pos)
            self.run("MoveLineDown")
            self.run("MoveLineBegin")
        self.goto(lst, pid)
        self.run("MoveParaEnd")
        end = self.com.GetPos()[2]
        return starts, end

    def range_text(self, lst: int, pid: int, s: int, e: int) -> str:
        self.select_range(lst, pid, s, e)
        t = self.com.GetTextFile("UNICODE", "saveblock") or ""
        self.run("Cancel")
        return t.rstrip("\r\n")

    def lines(self, lst: int, pid: int):
        """[(시작위치, 줄 텍스트)] — 한글 화면에 보이는 줄 단위"""
        starts, end = self.line_starts(lst, pid)
        bounds = starts[1:] + [end]
        return [(s, self.range_text(lst, pid, s, e)) for s, e in zip(starts, bounds)]

    def raw_text(self, lst: int, pid: int) -> str:
        self.select_para(lst, pid)
        t = self.com.GetTextFile("UNICODE", "saveblock") or ""
        self.run("Cancel")
        return t.rstrip("\r\n")

    def text(self, lst: int, pid: int) -> str:
        return self.raw_text(lst, pid).strip()

    # ---------- 수정 ----------
    def select_para(self, lst: int, pid: int):
        self.goto(lst, pid)
        self.run("MoveParaBegin")
        self.run("MoveSelParaEnd")

    def select_range(self, lst: int, pid: int, s: int, e: int):
        """같은 문단 안 s~e 글자 위치를 블록 선택"""
        self.goto(lst, pid, s)
        self.com.SelectText(pid, s, pid, e)

    def spacing_range(self, lst: int, pid: int, s: int, e: int, delta: int):
        """선택 범위에 자간 좁게(Alt+Shift+N)/넓게(Alt+Shift+W)를 |delta|회"""
        action = "CharShapeSpacingDecrease" if delta < 0 else "CharShapeSpacingIncrease"
        self.select_range(lst, pid, s, e)
        for _ in range(abs(delta)):
            self.run(action)
        self.run("Cancel")

    def undo(self, n: int = 1):
        """되돌리기 n번 — 자간을 원래 값 그대로 복구할 때 사용"""
        for _ in range(n):
            self.run("Undo")

    def has_hanging_indent(self, lst: int, pid: int) -> bool:
        """문단 모양에 이미 내어쓰기(음수 들여쓰기)가 있는지"""
        self.goto(lst, pid)
        ps = self.com.HParameterSet.HParaShape
        self.com.HAction.GetDefault("ParagraphShape", ps.HSet)
        return ps.Indentation < 0

    def pos_after(self, lst: int, pid: int, prefix: str):
        """문단 앞 글자가 prefix와 같아지는 실제 커서 위치.
        숨은 조판 부호가 있으면 글자 수와 커서 위치가 달라서 직접 맞춰 본다"""
        for pos in range(1, len(prefix) + 60):
            got = self.range_text(lst, pid, 0, pos)
            if got == prefix:
                return pos
            if len(got) > len(prefix):
                break
        return None

    def indent_at(self, lst: int, pid: int, pos: int):
        """커서 위치 내어쓰기 = 본문 Shift+Tab, 표 안 Ctrl+Shift+Tab (같은 명령)"""
        self.goto(lst, pid, pos)
        self.run("ParagraphShapeIndentAtCaret")

    def mark(self, lst: int, pid: int, s: int, e: int, color: int = YELLOW):
        """사람이 봐야 할 부분(잘린 단어)에 형광펜"""
        self.select_range(lst, pid, s, e)
        ps = self.com.HParameterSet.HMarkpenShape
        self.com.HAction.GetDefault("MarkPenShape", ps.HSet)
        ps.Color = color
        self.com.HAction.Execute("MarkPenShape", ps.HSet)
        self.run("Cancel")

    # ---------- 파일에 저장된 줄 정보 (화면 줄 나눔과 동일) ----------
    def snapshot(self) -> Path:
        """현재 상태를 임시 HWPX로 저장하고 경로 반환 (줄 정보를 읽기 위함)"""
        if not getattr(self, "_snapdir", None):
            self._snapdir = Path(tempfile.mkdtemp())
            self._tmpdirs.append(self._snapdir)
        path = self._snapdir / "snap.hwpx"
        self.com.SaveAs(str(path), "HWPX", "")
        return path

    def find_nth(self, text: str, n: int):
        """문서 처음부터 text의 n번째(0부터) 위치 → (리스트, 문단, 글자위치, 쪽) 또는 None"""
        self.run("MoveDocBegin")
        ps = self.com.HParameterSet.HFindReplace
        self.com.HAction.GetDefault("RepeatFind", ps.HSet)
        ps.FindString = text
        ps.IgnoreMessage = 1
        ps.FindRegExp = 0
        try:
            ps.Direction = self.com.FindDir("Forward")
        except Exception:
            ps.Direction = 0
        for _ in range(n + 1):
            if not self.com.HAction.Execute("RepeatFind", ps.HSet):
                self.run("Cancel")
                return None
        sel = self.com.GetSelectedPos()
        page = self._key()[0]
        self.run("Cancel")
        ok, slist, spara, spos = sel[0], sel[1], sel[2], sel[3]
        return (slist, spara, spos, page) if ok else None

    # ---------- 스타일 ----------
    def style_usage(self):
        """임시 HWPX로 내보내 XML에서 (스타일 목록, 사용 중인 ID 집합)을 읽는다"""
        tmpdir = Path(tempfile.mkdtemp())
        tmp = tmpdir / "styles.hwpx"
        try:
            self.com.SaveAs(str(tmp), "HWPX", "")
            with zipfile.ZipFile(tmp) as z:
                header = z.read("Contents/header.xml").decode("utf-8")
                body = "".join(z.read(n).decode("utf-8") for n in z.namelist()
                               if n.startswith("Contents/section") and n.endswith(".xml"))
        finally:
            self._tmpdirs.append(tmpdir)  # 한글이 파일을 잡고 있어서 닫은 뒤 삭제
        styles = []  # (id, type, name)
        for m in re.finditer(r"<hh:style\b([^>]*)>|<hh:style\b([^>]*)/>", header):
            attrs = m.group(1) or m.group(2)
            get = lambda k: (re.search(rf'\b{k}="([^"]*)"', attrs) or [None, ""])[1]
            styles.append((int(get("id")), get("type"), get("name")))
        used = {int(x) for x in re.findall(r'\bstyleIDRef="(\d+)"', body)}
        return styles, used

    def unused_styles(self):
        """미사용 문단 스타일 [(id, name, 기본구조용 여부)]. 바탕글(0)은 삭제 불가라 제외"""
        styles, used = self.style_usage()
        return [(s[0], s[2], s[2] in PROTECTED_STYLES) for s in styles
                if s[1] == "PARA" and s[0] not in used and s[0] != 0]

    # ---------- 저장 ----------
    def save_as(self, path: Path):
        self.com.SaveAs(str(path), self.fmt, "")

    def close(self):
        try:
            self.com.Clear(1)
            self.com.Quit()
        except Exception:
            pass
        import shutil
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
