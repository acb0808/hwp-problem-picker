"""한글 프로그램을 자동으로 움직여 양식에 글·수식·그림을 넣는다(윈도우 + 한글 필요).

한글은 항상 새로 하나를 띄워서 쓰고, 끝나면 그것만 닫는다. 사용자가 이미 열어 둔 한글 창이나 문서는
건드리지 않는다. 양식 원본도 열지 않고 사본으로만 작업한다.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from ..transcript import Choices, Figure, Para
from . import template

SECURITY_MODULE = "FilePathCheckerModule"
EQUATION_FONT = "HancomEQN"
FONT_UNIT = 1000            # 양식 본문 글자 크기(10pt)를 한글 단위로


class HwpError(RuntimeError):
    pass


@dataclass
class Picture:
    path: Path
    width_mm: float
    height_mm: float


def _hwp_pids() -> set[int]:
    import psutil
    return {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "hwp.exe"}


class Session:
    """새로 띄운 한글 하나. with 문을 벗어나면 이 세션이 띄운 한글만 닫는다.

    한글이 이미 떠 있을 때 자동화 연결이 그 창에 붙어 버리면, 문서를 비우거나 닫는 명령이 사용자의
    문서에 가해진다. 그래서 연결한 한글이 "방금 새로 생긴 프로세스"임을 확인하고, 확인하지 못하면
    아무것도 건드리지 않고 그만둔다.
    """

    def __init__(self) -> None:
        self.hwp = None
        self.pid = 0
        self.owned = False

    def __enter__(self) -> "Session":
        import pythoncom
        import win32com.client as win32
        import win32process

        pythoncom.CoInitialize()
        before = _hwp_pids()
        try:
            self.hwp = win32.DispatchEx("HWPFrame.HwpObject")
        except Exception as e:
            raise HwpError(f"한글 프로그램을 띄우지 못했습니다(한글이 설치되어 있어야 합니다): {e}") from e
        pid = 0
        try:
            hwnd = self.hwp.XHwpWindows.Active_XHwpWindow.WindowHandle
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        except Exception:
            pass
        fresh = _hwp_pids() - before
        if pid not in fresh:
            # 새 프로세스가 아니면 사용자가 쓰던 한글일 수 있다. 연결만 놓고 물러난다
            self.hwp = None
            raise HwpError("한글을 따로 새로 띄우지 못했습니다. 열려 있는 한글 문서를 건드리지 않기 위해 작업을 멈췄습니다. "
                           "한글을 모두 닫고 다시 시도해 주세요.")
        self.pid, self.owned = pid, True
        if not self.hwp.RegisterModule("FilePathCheckDLL", SECURITY_MODULE):
            self.__exit__(None, None, None)
            raise HwpError("한글 자동화 보안 모듈이 등록되어 있지 않습니다. `python -m hwpick doctor --fix`를 실행하세요.")
        # 확인 창이 떠서 멈추는 일이 없도록, 물음에는 자동으로 '아니오/취소'를 고른다
        self.hwp.SetMessageBoxMode(0x00000001 | 0x00000020 | 0x00004000 | 0x00020000 | 0x00200000)
        return self

    def __exit__(self, *exc) -> None:
        if not self.owned or self.hwp is None:
            return
        try:
            self.hwp.Clear(1)
            self.hwp.Quit()
        except Exception:
            pass
        self.hwp = None
        self._end_process()

    def _end_process(self) -> None:
        """Quit 뒤에도 프로세스가 남으면(자동화에서 가끔 그렇다) 이 세션이 띄운 것만 끝낸다."""
        import psutil
        try:
            proc = psutil.Process(self.pid)
            if proc.name().lower() != "hwp.exe":
                return
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.terminate()
        except psutil.NoSuchProcess:
            pass

    # ---- 문서 열고 닫기

    def open(self, path: Path, fmt: str) -> None:
        self.hwp.Clear(1)
        if not self.hwp.Open(str(path), fmt, ""):
            raise HwpError(f"문서를 열지 못했습니다: {path}")

    def save(self, path: Path, fmt: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.hwp.SaveAs(str(path), fmt, "") is False or not path.is_file():
            raise HwpError(f"문서를 저장하지 못했습니다: {path}")

    @property
    def pages(self) -> int:
        return int(self.hwp.PageCount)

    # ---- 내용 넣기

    def _find(self, marker: str) -> None:
        hwp = self.hwp
        hwp.Run("MoveDocBegin")
        p = hwp.HParameterSet.HFindReplace
        hwp.HAction.GetDefault("RepeatFind", p.HSet)
        p.FindString = marker
        p.Direction = hwp.FindDir("AllDoc")
        p.IgnoreMessage = 1
        p.FindRegExp = 0
        p.FindType = 1
        p.MatchCase = 1
        p.WholeWordOnly = 0
        if not hwp.HAction.Execute("RepeatFind", p.HSet):
            raise HwpError(f"양식에 심어 둔 자리를 찾지 못했습니다: {marker}")

    def _text(self, value: str) -> None:
        if not value:
            return
        hwp = self.hwp
        p = hwp.HParameterSet.HInsertText
        hwp.HAction.GetDefault("InsertText", p.HSet)
        p.Text = value
        if not hwp.HAction.Execute("InsertText", p.HSet):
            raise HwpError("글자를 넣지 못했습니다")

    def _equation(self, script: str) -> None:
        hwp = self.hwp
        before = tuple(hwp.GetPos())
        p = hwp.HParameterSet.HEqEdit
        hwp.HAction.GetDefault("EquationCreate", p.HSet)
        p.EqFontName = EQUATION_FONT
        p.string = script
        p.BaseUnit = FONT_UNIT
        p.HSet.SetItem("TreatAsChar", 1)
        if not hwp.HAction.Execute("EquationCreate", p.HSet):
            raise HwpError(f"수식을 넣지 못했습니다: {script}")
        after = tuple(hwp.GetPos())
        if before[:2] != after[:2] or after[2] <= before[2]:
            raise HwpError(f"수식을 넣은 뒤 커서가 엉뚱한 곳으로 갔습니다: {script}")

    def _center(self) -> None:
        hwp = self.hwp
        p = hwp.HParameterSet.HParaShape
        hwp.HAction.GetDefault("ParagraphShape", p.HSet)
        p.AlignType = hwp.HAlign("Center")
        if not hwp.HAction.Execute("ParagraphShape", p.HSet):
            raise HwpError("문단을 가운데로 맞추지 못했습니다")

    def _picture(self, pic: Picture) -> None:
        ctrl = self.hwp.InsertPicture(str(pic.path.resolve()), True, 1, False, False, 0, pic.width_mm, pic.height_mm)
        if ctrl is None:
            raise HwpError(f"그림을 넣지 못했습니다: {pic.path}")
        props = ctrl.Properties
        props.SetItem("TreatAsChar", 1)
        ctrl.Properties = props

    def fill(self, operations: list[template.Operation], pictures: dict[tuple[int, str], Picture],
             progress=None) -> None:
        """표식마다 내용을 넣는다. pictures는 (문제 순번, 그림 이름) → 그림."""
        for n, op in enumerate(operations):
            self._find(op.marker)
            self.hwp.Run("Delete")
            start = tuple(self.hwp.GetPos())
            b = op.block
            if isinstance(b, Figure):
                pic = pictures.get((op.problem, b.name))
                if pic is None:
                    raise HwpError(f"{op.problem + 1}번 문제의 그림 {b.name} 파일이 없습니다")
                room = op.width * 25.4 / 7200         # HWPUNIT → mm
                if pic.width_mm > room > 0:           # 자리보다 넓은 그림은 자리에 맞춰 줄인다(표의 한 칸, 상자 안)
                    pic = Picture(pic.path, round(room, 1), round(pic.height_mm * room / pic.width_mm, 1))
                self._center()
                self._picture(pic)
            elif isinstance(b, Para):
                if b.center:
                    self._center()
                for r in b.runs:
                    if r.eq:
                        self._equation(r.eq)
                    else:
                        self._text(r.text)
            else:
                raise HwpError(f"넣을 줄 모르는 내용입니다: {type(b).__name__}")
            if tuple(self.hwp.GetPos())[:2] != start[:2]:
                raise HwpError(f"{op.problem + 1}번 문제를 넣다가 커서가 칸 밖으로 나갔습니다")
            if progress:
                progress(n + 1, len(operations))


def _template_hwpx(template_hwp: Path, work: Path, s: Session) -> Path:
    """양식을 HWPX로 바꾼 것. 같은 양식이면 한 번 바꿔 둔 것을 다시 쓴다(양식 원본은 열지 않고 사본을 연다)."""
    import hashlib
    if template_hwp.suffix.lower() == ".hwpx":
        return template_hwp                       # 이미 HWPX인 양식(스킬에 든 기본 양식)은 바꿀 것이 없다
    digest = hashlib.sha1(template_hwp.read_bytes()).hexdigest()[:16]
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "HWPick" / "cache" / "templates"
    kept = base / f"{digest}.hwpx"
    if not kept.is_file():
        copy = work / "template.hwp"
        shutil.copyfile(template_hwp, copy)
        s.open(copy, "HWP")
        s.save(work / "template.hwpx", "HWPX")
        base.mkdir(parents=True, exist_ok=True)
        tmp = base / f"{digest}.{os.getpid()}.tmp"
        shutil.copyfile(work / "template.hwpx", tmp)
        os.replace(tmp, kept)
    return kept


def write_document(template_hwp: Path, out_dir: Path, name: str, title: str, problems: list[list],
                   pictures: dict[tuple[int, str], Picture], progress=None, session: Session | None = None,
                   preview: bool = True, first_page: bool = False) -> dict:
    """양식 사본에 문제들을 넣어 out_dir에 <name>.hwp를 만든다. first_page가 거짓이면 양식의 첫 쪽(머리말)을 쓰지 않는다.

    session을 주면 이미 띄워 둔 한글을 쓴다(닫는 것은 준 쪽의 일이다). preview가 거짓이면 미리보기용
    PDF는 내용이 칸을 넘쳤을 때만 만든다(PDF 저장에 2초가 든다).

    돌려주는 값: {"hwp", "pdf", "pages", "expected_pages", "spread", "too_tall"}.
    내용이 칸을 넘치면(쪽 수가 예상보다 많아진다) 한글이 계산한 높이를 읽어, 넘친 문제를 아래 칸과 합치거나 다음 칸으로
    이어 쓰게 다시 나눠 넣는다. spread는 그렇게 여러 칸을 쓴 문제({문제 번호: 칸 수})다. 그래도 쪽 수가 예상보다 많으면
    쪼갤 수 없는 덩어리 하나가 두 칸보다 큰 것이다(too_tall에 그 문제 번호. 호출한 쪽에서 사용자에게 알린다).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    started = time.time()
    s = session or Session().__enter__()
    try:
        model = _template_hwpx(template_hwp, work, s)
        plan = template.scaffold(model, work / "scaffold.hwpx", problems, title, first_page)
        s.open(work / "scaffold.hwpx", "HWPX")
        s.fill(plan.operations, pictures, progress)
        pages = s.pages
        too_tall: list[int] = []
        if pages > plan.pages:
            # 어느 칸이 넘쳤다. 문제마다 실제 높이를 읽어 칸을 다시 나눈다(여유를 늘려 한 번 더 해 본다)
            s.save(work / "measure.hwpx", "HWPX")
            sizes = template.measure(work / "measure.hwpx", plan)
            for slack in (150, 1500) if sizes else ():
                layout = template.arrange(sizes, len(problems), plan.room, slack)
                if layout == plan.columns:
                    break
                plan = template.scaffold(model, work / "scaffold.hwpx", problems, title, first_page, layout)
                s.open(work / "scaffold.hwpx", "HWPX")
                s.fill(plan.operations, pictures, progress)
                pages = s.pages
                if pages <= plan.pages:
                    break
            if sizes and pages > plan.pages:
                limit = sum(plan.room[1][:2]) - plan.room[1][2]
                too_tall = [i + 1 for i, (label, blocks) in sorted(sizes.items()) if any(label + h > limit for h, _ in blocks)]
        used: dict[int, int] = {}
        for column in plan.columns:
            for cell in column:
                if cell is not None:
                    used[cell.problem + 1] = used.get(cell.problem + 1, 0) + (2 if len(column) == 1 else 1)
        hwp_path, pdf_path = out_dir / f"{name}.hwp", out_dir / f"{name}.pdf"
        pdf_path.unlink(missing_ok=True)
        s.save(hwp_path, "HWP")
        s.open(hwp_path, "HWP")                      # 저장한 파일을 다시 열어 쪽 수가 그대로인지 본다
        if s.pages != pages:
            raise HwpError(f"저장한 파일을 다시 열었더니 쪽 수가 달라졌습니다({pages} → {s.pages})")
        if preview or pages > plan.pages:
            s.save(pdf_path, "PDF")
        pid = s.pid
    finally:
        if session is None:
            s.__exit__(None, None, None)
    return {"hwp": str(hwp_path), "pdf": str(pdf_path) if pdf_path.is_file() else "", "pages": pages,
            "expected_pages": plan.pages, "seconds": round(time.time() - started, 1), "hwp_pid": pid,
            "spread": {str(n): c for n, c in sorted(used.items()) if c > 1}, "too_tall": too_tall}
