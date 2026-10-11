"""한글 문서(HWP·HWPX)를 문제의 출처로 쓴다.

화면에 보여 줄 쪽 그림과 문제의 자리는 한글 프로그램이 저장한 PDF에서 얻고, 문제의 내용(글·수식·그림)은
같이 저장한 HWPX에서 그대로 꺼낸다. 그래서 한글 문서에서 고른 문제는 모델이 다시 읽지 않는다(비용도, 옮겨
적다 생기는 오류도 없다). 사용자가 한글에서 고친 내용도 그대로 따라온다. 그대로 옮길 수 없는 것(직접 그린
도형, 여러 칸짜리 표)이 든 문제만 여느 PDF 문제처럼 그림을 모델이 읽는다.

원본 파일은 열지 않는다. 사본을 만들어 한글로 열고, 결과는 파일 내용으로 정한 캐시 폴더에 둔다.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import ZipFile

from lxml import etree as E

from ..transcript import CIRCLED
from ..transcript import RAW_EQUATION as RAW     # 전사 글에서 "이 수식은 이미 한글 수식 문법"이라는 표시

SUFFIXES = (".hwp", ".hwpx")
VERSION = 6                  # 꺼내는 방식을 바꾸면 올린다(변환해 둔 PDF·HWPX는 그대로 쓴다)
HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HH = "http://www.hancom.co.kr/hwpml/2011/head"
UNIT_PER_MM = 7200 / 25.4    # HWPUNIT
CONVERT_LOCK = threading.Lock()


def is_document(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUFFIXES


def _tag(name: str, ns: str = HP) -> str:
    return "{" + ns + "}" + name


def converted(cache: Path) -> tuple[Path, Path]:
    return cache / "doc.pdf", cache / "doc.hwpx"


def convert(path: Path, cache: Path) -> tuple[Path, Path]:
    """문서를 PDF와 HWPX로 바꿔 cache에 둔다(이미 있으면 그대로 쓴다). 한글 프로그램을 잠깐 띄운다."""
    from . import writer
    pdf, hwpx = converted(cache)
    if pdf.is_file() and hwpx.is_file():
        return pdf, hwpx
    with CONVERT_LOCK:
        if pdf.is_file() and hwpx.is_file():
            return pdf, hwpx
        cache.mkdir(parents=True, exist_ok=True)
        # 한글은 사용자 폴더의 AppData 아래(임시 폴더는 빼고)에 있는 파일을 열지 못한다. 그래서 임시 폴더에서 바꾼다
        work = Path(tempfile.mkdtemp(prefix="hwpick-"))
        try:
            copy = work / ("source" + path.suffix.lower())
            try:
                shutil.copyfile(path, copy)
            except PermissionError as e:
                raise writer.HwpError(f"문서를 읽지 못했습니다. 한글에서 열려 있으면 닫고 다시 해 주세요: {path.name}") from e
            with writer.Session() as s:
                s.open(copy, "HWPX" if path.suffix.lower() == ".hwpx" else "HWP")
                s.save(work / "doc.hwpx", "HWPX")
                s.save(work / "doc.pdf", "PDF")
            for made, kept in ((work / "doc.hwpx", hwpx), (work / "doc.pdf", pdf)):
                tmp = kept.with_name(f"{kept.name}.{os.getpid()}.tmp")
                shutil.copyfile(made, tmp)
                os.replace(tmp, kept)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return pdf, hwpx


# ---- 문서에서 문제 꺼내기
#
# 한글 문서마다 문제 번호를 적는 방식이 다르다. 실제 문서들에서 본 것은 네 가지다.
#   가. 표의 칸 머리에 'N번.' 문단이 혼자 있고 그 아래가 문제(HWPick·학원 양식)
#   나. 표의 칸 안에서 'N. 글…'처럼 번호와 글이 한 문단(시험지 복원본)
#   다. 본문에 'N번. 글…', 'N. 글…'이 이어지는 것(1단·2단 시험지)
#   라. 본문에 자동 문단 번호를 쓴 것(번호가 문서 속 글에는 없고 찍힌 모습에만 있다. 모의고사 양식)
# 문서마다 이 가운데 "내용이 있는 문제를 가장 많이 만들어 내는" 방식 하나를 골라 그 방식으로 문제를 나눈다.

FAMILIES = [("번", re.compile(r"\s*(\d{1,3})\s*번\s*[.．:]?\s*")),
            (".", re.compile(r"\s*(\d{1,3})\s*[.．](?!\d)\s*")),
            ("문제", re.compile(r"\s*(?:문제|문항)\s*(\d{1,3})\s*[.．:)]?\s*")),
            ("[]", re.compile(r"\s*\[\s*(\d{1,3})\s*\]\s*")),
            (")", re.compile(r"\s*(\d{1,3})\s*\)\s*"))]
# 따로 번호를 매기는 서술형 문제('서술형 1', '[서답형 2]'). 어느 방식의 문서에서나 문제의 시작으로 본다
EXTRA_RE = re.compile(r"\s*[\[<(【]?\s*(서술형|서답형|단답형|주관식|논술형)\s*(?:문제|문항)?\s*(\d{1,2})\s*번?\s*[\]>)】]?\s*[.．:)]?\s*")
# 문제 뒤에 붙어 딸려 오는 것들: 문제 묶음의 제목과 끝 표시
HEADING_RE = re.compile(r"[\W_]*(서술형|서답형|단답형|주관식|논술형|선택형|객관식|선다형|5지\s*선다형)")
STOP_RE = re.compile(r"[\W_]*(수고하셨|끝\s*[-–—\]>)]|이하\s*여백|확인\s*사항)")
SUBJECT_RE = re.compile(r"[\W_]*(미분과\s*적분|확률과\s*통계|이산\s*수학|기하와\s*벡터|기하|미적분)[\W_]*")   # 선택 과목의 제목 줄
# 여러 문제가 함께 쓰는 발문의 머리('[3~4] 다음 글을 읽고 …')
RANGE_RE = re.compile(r"\s*[※*]?\s*[\[(〔［]\s*(\d{1,3})\s*[~∼～\-–]\s*(\d{1,3})\s*[\])〕］]")
# 이 도구가 긴 문제를 다음 칸에 이어 쓸 때 다는 머리('3번. (이어서)'). 그 칸의 내용은 3번의 뒷부분이다
CONTINUED_RE = re.compile(r"\s*(\d{1,3})\s*번\s*[.．]?\s*\(\s*이어서\s*\)\s*$")
ANSWER_RE = re.compile(r"[\W_]*(정답\s*및\s*해설|빠른\s*정답|정답|해설)")       # 문서 끝에 붙는 정답·해설의 제목
DRAWINGS = ("rect", "ellipse", "line", "arc", "polygon", "curve", "container", "connectLine", "textart", "ole", "video", "chart")
NOTES = ("header", "footer", "footNote", "endNote")


@dataclass
class Slot:
    """문서의 문제 하나."""
    number: int | None                                      # 문제 번호(자동 번호 문서는 쪽 그림에서 읽어 채운다)
    text: str = ""                                          # 전사 글(수식은 한글 수식 문법 그대로)
    figures: list[dict] = field(default_factory=list)       # {"name", "member", "width_mm", "height_mm"}
    issues: list[str] = field(default_factory=list)         # 그대로 옮기지 못하는 것(있으면 그림에서 다시 읽는다)
    label: str = ""                                         # 화면에 보일 번호('3', '서술형2')
    inline: bool = False                                    # 번호와 같은 줄에서 글이 시작하는가(아니면 번호만 한 줄)
    head: str = ""                                          # 내용 맨 앞의 한글 몇 자(쪽 그림에서 자리를 찾을 때 대조한다)
    floating: list[dict] = field(default_factory=list)      # 쪽을 기준으로 놓인 개체들의 자리({"x": (왼, 오) | None, "y": …}, pt)
    word: str = ""                                          # 따로 번호를 매기는 묶음의 이름('서술형'). 보통은 빈 값


@dataclass
class Parsed:
    slots: list[Slot]
    family: str = ""                                        # 고른 번호 방식('번', '.', 'auto:…')
    notes: list[str] = field(default_factory=list)          # 사람이 알아야 할 점


class _Reader:
    def __init__(self, centered: set[str], members: dict[str, str], max_mm: tuple[float, float],
                 margins: tuple[float, float] = (0.0, 0.0)):
        self.centered, self.members, self.max_mm, self.margins = centered, members, max_mm, margins
        self.floating: list[dict] = []
        self.lines: list[str] = []
        self.figures: list[dict] = []
        self.issues: list[str] = []
        self.strip: dict[int, int] = {}          # 문단 → 맨 앞에서 뗄 글자 수(문제 번호)

    @staticmethod
    def text_of(t) -> str:
        """글자 요소의 글. 줄 나눔은 줄바꿈으로, 탭과 특수 빈칸은 빈칸으로 본다."""
        out = [t.text or ""]
        for child in t:
            name = E.QName(child).localname
            out.append("\n" if name == "lineBreak" else " " if name in ("tab", "nbSpace", "fwSpace") else "")
            out.append(child.tail or "")
        return "".join(out)

    def _pieces(self, par) -> list[tuple[str, object]]:
        """문단의 내용을 차례대로: ("text", 글) / ("eq", 수식) / ("pic", 요소) / ("tbl", 요소) / ("rect", 글상자)."""
        out: list[tuple[str, object]] = []
        cut = self.strip.get(id(par), 0)
        for run in par.findall(_tag("run")):
            for child in run:
                name = E.QName(child).localname
                if name == "t":
                    text = self.text_of(child)
                    if cut:
                        whole = len(text)
                        text, cut = text[cut:], max(0, cut - whole)
                    if text:
                        out.append(("text", text))
                    continue
                if name not in ("ctrl", "secPr") and not _page_anchored(child):
                    cut = 0
                if _page_anchored(child):
                    # 쪽을 기준으로 놓은 것은 대개 머리 표·쪽 번호 상자·꾸밈 도형이라 문제의 내용이 아니다. 다만 문제의
                    # 그림을 그렇게 놓은 문서도 있어서, 자리를 적어 두었다가 문제의 자리와 겹치는지 나중에 본다(place)
                    where = _paper_rect(child, self.margins)
                    if where is not None:
                        self.floating.append(where)
                    continue
                if name == "equation":
                    script = " ".join((child.findtext(_tag("script")) or "").split())
                    if script:
                        out.append(("eq", script))
                elif name in ("pic", "tbl"):
                    out.append((name, child))
                elif name == "rect" and child.find(_tag("drawText")) is not None:
                    out.append(("rect", child))
                elif name in DRAWINGS:
                    self.issues.append("직접 그린 도형이 있습니다")
                elif name not in ("ctrl", "secPr"):
                    self.issues.append(f"그대로 옮길 수 없는 개체가 있습니다({name})")
        return out

    def _inline(self, pieces: list[tuple[str, object]]) -> str:
        return "".join(f"${RAW} {v}$" if kind == "eq" else str(v).replace("$", "＄") for kind, v in pieces)

    def _figure(self, pic) -> None:
        img = next((e for e in pic.iter() if E.QName(e).localname == "img"), None)
        member = self.members.get(img.get("binaryItemIDRef", "")) if img is not None else None
        size = pic.find(_tag("sz"))
        if member is None or size is None:
            self.issues.append("그림 파일을 찾지 못했습니다")
            return
        clip, dim = pic.find(_tag("imgClip")), pic.find(_tag("imgDim"))
        part = None                              # 그림에서 보이게 둔 부분(왼쪽, 위, 오른쪽, 아래를 전체에 대한 비율로)
        if clip is not None and dim is not None and int(dim.get("dimwidth", 0)) > 0 and int(dim.get("dimheight", 0)) > 0:
            dw, dh = int(dim.get("dimwidth")), int(dim.get("dimheight"))
            edges = [int(clip.get("left", 0)) / dw, int(clip.get("top", 0)) / dh,
                     int(clip.get("right", dw)) / dw, int(clip.get("bottom", dh)) / dh]
            if edges[0] > 0.005 or edges[1] > 0.005 or edges[2] < 0.995 or edges[3] < 0.995:
                part = [round(max(0.0, min(1.0, v)), 4) for v in edges]
                if part[2] - part[0] < 0.01 or part[3] - part[1] < 0.01:
                    self.issues.append("보이는 부분이 없는 그림이 있습니다")
                    return
        rot, flip = pic.find(_tag("rotationInfo")), pic.find(_tag("flip"))
        if (rot is not None and int(rot.get("angle", 0)) % 360) or (
                flip is not None and (flip.get("horizontal") == "1" or flip.get("vertical") == "1")):
            self.issues.append("돌리거나 뒤집은 그림이 있습니다")
        w, h = int(size.get("width")) / UNIT_PER_MM, int(size.get("height")) / UNIT_PER_MM
        if w <= 0 or h <= 0:
            self.issues.append("크기를 알 수 없는 그림이 있습니다")
            return
        shrink = min(1.0, self.max_mm[0] / w, self.max_mm[1] / h)
        name = f"F{len(self.figures) + 1}"
        self.figures.append({"name": name, "member": member, "width_mm": round(w * shrink, 1),
                             "height_mm": round(h * shrink, 1), "part": part})
        self.lines.append(f"![](figure:{name})")

    def _boxed(self, paras) -> None:
        """테두리 상자(보기·조건). 첫 줄이 '<보 기>' 꼴이면 상자의 이름이다."""
        label = ""
        if paras:
            head = self._pieces(paras[0])
            m = re.fullmatch(r"\s*[<〈]\s*(.+?)\s*[>〉]\s*", head[0][1]) if len(head) == 1 and head[0][0] == "text" else None
            if m:
                inner = m.group(1)
                label = inner.replace(" ", "") if all(len(w) == 1 for w in inner.split()) else inner
                paras = paras[1:]
        self.lines.append(f"::: box {label}".rstrip())
        before = len(self.lines)
        self.paragraphs(paras)
        if len(self.lines) == before:
            self.lines.pop()                     # 빈 상자는 넣지 않는다
        else:
            self.lines.append(":::")

    def _table(self, tbl) -> None:
        cells = [tc.find(_tag("subList")).findall(_tag("p")) for tr in tbl.findall(_tag("tr")) for tc in tr.findall(_tag("tc"))]
        filled = [[q for q in (self._pieces(p) for p in cell) if q] for cell in cells]
        options = [cell for cell in filled if cell]
        marks = [cell[0][0][1].lstrip()[:1] if cell[0][0][0] == "text" else "" for cell in options]
        if options and marks == list(CIRCLED[:len(options)]) and all(
                len(cell) == 1 and all(kind in ("text", "eq") for kind, _ in cell[0]) for cell in options):
            # 선택지를 줄 맞춰 늘어놓은 표: 한 줄로 적고, 한 줄에 몇 개씩 놓였는지도 남긴다
            parts = []
            for cell in options:
                first, rest = cell[0][0], cell[0][1:]
                body = first[1].lstrip()
                parts.append(body[:1] + " " + self._inline([("text", body[1:].lstrip())] + rest).strip())
            size = tbl.find(_tag("sz"))
            self.lines.append(f"<!-- cols:{tbl.get('colCnt')}" + (f" w:{size.get('width')}" if size is not None else "") + " -->")
            self.lines.append("  ".join(parts).replace("\n", " "))
            return
        if len(cells) == 1:
            self._boxed(cells[0])
            return
        shown = [[[q for q in para if q[0] != "text" or str(q[1]).strip()] for para in cell] for cell in filled]
        if sum(1 for cell in shown if cell) >= 2 and all(
                len(cell) == 1 and len(cell[0]) == 1 and cell[0][0][0] == "pic" for cell in shown if cell):
            # 그림만 한 칸에 하나씩 든 표: 나란히 놓은 그림들이다. 줄마다 몇 개씩인지 남기고 그림을 차례대로 적는다
            cols = int(tbl.get("colCnt") or 1)
            counts = [sum(1 for cell in shown[k:k + cols] if cell) for k in range(0, len(shown), cols)]
            before = len(self.lines)
            self.lines.append("<!-- figures:" + ",".join(str(c) for c in counts if c) + " -->")
            for cell in shown:
                if cell:
                    self._figure(cell[0][0][1])
            if len(self.lines) - before - 1 != sum(counts):
                del self.lines[before]               # 못 꺼낸 그림이 있으면 배치는 적지 않는다(까닭은 _figure가 적었다)
            return
        full = [cell for cell, got in zip(cells, filled) if got]
        if len(full) == 1:
            self._boxed(full[0])                 # 내용이 한 칸에만 있고 나머지는 테두리를 꾸미는 빈 칸인 표
            return
        if len(full) == 2:
            first = [q for q in (self._pieces(p) for p in full[0]) if q]
            if len(first) == 1 and len(first[0]) == 1 and first[0][0][0] == "text" \
                    and re.fullmatch(r"\s*[<〈]\s*.+?\s*[>〉]\s*", first[0][0][1]):
                self._boxed(list(full[0]) + list(full[1]))
                return
        self.issues.append("선택지도 상자도 아닌 표가 있습니다")

    def paragraphs(self, paras) -> None:
        for par in paras:
            run: list[tuple[str, object]] = []

            def flush() -> None:
                if not run:
                    return
                only = [p for p in run if p[0] == "eq" or str(p[1]).strip()]
                if len(only) == 1 and only[0][0] == "eq" and par.get("paraPrIDRef") in self.centered:
                    self.lines.append(f"$${RAW} {only[0][1]}$$")             # 한 줄을 통째로 차지하는 가운데 수식
                else:
                    self.lines += [l.strip() for l in self._inline(run).split("\n") if l.strip()]
                run.clear()

            for kind, v in self._pieces(par):
                if kind in ("text", "eq"):
                    run.append((kind, v))
                    continue
                flush()
                if kind == "pic":
                    self._figure(v)
                elif kind == "rect":
                    sub = v.find(_tag("drawText")).find(_tag("subList"))
                    self._boxed(sub.findall(_tag("p")) if sub is not None else [])
                else:
                    self._table(v)
            flush()


def _page_anchored(obj) -> bool:
    """글자처럼 놓이지 않고 쪽(종이)을 기준으로 자리를 잡은 개체인가."""
    pos = obj.find(_tag("pos"))
    return pos is not None and pos.get("treatAsChar") != "1" and pos.get("vertRelTo") in ("PAPER", "PAGE")


def _paper_rect(obj, margins: tuple[float, float]) -> dict | None:
    """쪽을 기준으로 놓인 개체가 종이 위에서 차지하는 범위(pt). 알 수 없는 방향은 None.

    문제의 내용일 리 없는 것은 None을 돌려준다: 쪽 아래에 맞춰 놓은 것(안내 상자, 쪽 번호)과 가느다란 줄(단 사이의 세로줄).
    """
    pos, size = obj.find(_tag("pos")), obj.find(_tag("sz"))
    w, h = (int(size.get("width", 0)) / 100, int(size.get("height", 0)) / 100) if size is not None else (0.0, 0.0)
    if pos.get("vertAlign") == "BOTTOM" or (size is not None and min(w, h) < 3):
        return None
    out = {"x": None, "y": None}
    for axis, rel, align, offset, extent, margin in (("x", "horzRelTo", "horzAlign", "horzOffset", w, margins[0]),
                                                     ("y", "vertRelTo", "vertAlign", "vertOffset", h, margins[1])):
        if pos.get(align) in ("LEFT", "TOP") and pos.get(rel) in ("PAPER", "PAGE") and extent > 0:
            start = int(pos.get(offset, 0)) / 100 + (margin if pos.get(rel) == "PAGE" else 0.0)
            out[axis] = (start, start + extent)
    return out


def _lead(par) -> str:
    """문단 맨 앞의 글(첫 수식·개체 앞까지). 문제 번호가 여기에 적힌다."""
    out = []
    for run in par.findall(_tag("run")):
        for child in run:
            name = E.QName(child).localname
            if name == "t":
                out.append(_Reader.text_of(child))
            elif name not in ("ctrl", "secPr") and not _page_anchored(child):
                return "".join(out)
    return "".join(out)


def _under_anchored(e, top) -> bool:
    """e가 top 문단 안에서 쪽에 붙박인 개체(또는 그 안)인가."""
    while e is not None and e is not top:
        if _page_anchored(e):
            return True
        e = e.getparent()
    return False


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _first_line(par) -> str:
    """문단의 첫 줄 글. 표나 글상자만 든 문단이면 그 안의 첫 줄(수식으로 시작하는 문단은 그 문단 자신의 것이라 안을 보지 않는다)."""
    text = _lead(par).strip()
    own = any(E.QName(c).localname == "equation" for r in par.findall(_tag("run")) for c in r)
    if not text and not own:
        inner = next((q for q in par.iter(_tag("p")) if q is not par), None)
        text = _lead(inner).strip() if inner is not None else ""
    return text


def _weight(par, skip: int = 0) -> int:
    """문단에 든 내용의 양(글자 수 + 수식·그림). 번호만 있는 칸과 문제가 든 칸을 가르는 데 쓴다."""
    chars = sum(len("".join(t.itertext()).strip()) for t in par.iter(_tag("t")))
    return max(0, chars - skip) + 3 * sum(1 for _ in par.iter(_tag("equation"))) + 20 * sum(1 for _ in par.iter(_tag("pic")))


class _Splitter:
    """고른 번호 방식 하나로 문서를 문제들로 나눈다."""

    def __init__(self, family: str, headings: dict[str, str]):
        self.family, self.headings = family, headings
        self.pattern = dict(FAMILIES).get(family)
        self.found: list[dict] = []              # {"number", "word", "strip", "paras", "inline"}
        self.continued: list[dict] = []          # 앞 칸에서 이어지는 칸들: {"number", "paras"}
        self.notes: list[str] = []
        self._cache: dict[int, tuple | None] = {}
        self.dotted = False                      # 'N번' 방식에서: 이 문서의 번호에는 뒤에 점이 붙는가

    def calibrate(self, paras) -> None:
        """문서의 번호 꼴을 익힌다. 'N번' 뒤에 거의 늘 점이 붙는 문서면, 점 없이 글이 이어지는 줄은 번호로 보지 않는다."""
        if self.family != "번":
            return
        leads = [m.group(0) for m in (self.pattern.match(_lead(p)) for p in paras) if m]
        self.dotted = len(leads) >= 3 and sum(1 for v in leads if re.search(r"[.．:]", v)) >= 0.8 * len(leads)

    def label(self, par) -> tuple | None:
        """문단이 문제의 시작이면 (번호, 묶음 이름, 뗄 글자 수)."""
        key = id(par)
        if key not in self._cache:
            lead = _lead(par)
            res = None
            m = self.pattern.match(lead) if self.pattern is not None and not CONTINUED_RE.match(lead) else None
            if m and self.dotted and not re.search(r"[.．:]", m.group(0)) and lead[m.end():].strip():
                m = None                         # 문서의 번호는 '3번.'인데 이 줄은 '5번 던질 때…': 번호가 아니라 문장이다
            if m:
                tag = EXTRA_RE.match(lead[m.end():])
                res = (int(m.group(1)), "", m.end() + (tag.end() if tag else 0))
            elif self.pattern is None and self.headings.get(par.get("paraPrIDRef")) == self.family:
                tag = EXTRA_RE.match(lead)           # 자동 번호 뒤에 붙은 '[논술형 1]' 표시는 번호와 함께 뗀다
                res = (None, "", tag.end() if tag else 0)
            else:
                m = EXTRA_RE.match(lead)
                if m:
                    res = (int(m.group(2)), m.group(1), m.end())
            self._cache[key] = res
        return self._cache[key]

    def has_labels(self, tbl) -> bool:
        """표의 칸에 문제가 들어 있는가: 번호로 시작하는 문단이 있고 그 칸에 번호 말고도 내용이 있어야 한다."""
        for tc in tbl.iter(_tag("tc")):
            sub = tc.find(_tag("subList"))
            paras = sub.findall(_tag("p")) if sub is not None else []
            for k, par in enumerate(paras):
                if CONTINUED_RE.match(_lead(par)):
                    return True                  # 앞 칸에서 이어지는 칸만 있는 표(긴 문제가 한 쪽을 다 차지한 경우)
                lab = self.label(par)
                if lab and _weight(par, lab[2]) + sum(_weight(p) for p in paras[k + 1:]) >= 6:
                    return True
        return False

    def walk(self, paras, top: bool = False) -> None:
        labels = [self.label(p) for p in paras]
        mains = [(k, lab[0]) for k, lab in enumerate(labels) if lab and lab[1] == "" and lab[0] is not None]

        def continues(k: int, number: int) -> bool:
            """이 번호 다음에 나오는 번호가 그 뒤를 잇는가(혼자 튀는 번호는 문제 안의 줄이다)."""
            after = next((n for j, n in mains if j > k), None)
            return after is not None and number < after <= number + 2

        def run_length(k: int) -> int:
            """여기서부터 번호가 몇 개나 차례대로 이어지는가."""
            run = [n for j, n in mains if j >= k]
            size = 1
            while size < len(run) and run[size - 1] < run[size] <= run[size - 1] + 2:
                size += 1
            return size

        def light(k: int) -> bool:
            """여기서 다시 시작하는 묶음이 번호마다 내용이 거의 없는가(정답표)."""
            run = [j for j, _ in mains if j >= k][:7]
            weights = sorted(_weight(paras[a], labels[a][2]) + sum(_weight(p) for p in paras[a + 1:b])
                             for a, b in zip(run, run[1:]))
            return bool(weights) and weights[len(weights) // 2] < 12

        current = None
        last = None                              # 이 흐름에서 마지막으로 본 번호
        for k, par in enumerate(paras):
            lab = labels[k]
            more = CONTINUED_RE.match(_lead(par))
            if more and _weight(par) <= len(_lead(par).strip()):
                # 앞 칸에서 이어지는 칸의 머리: 이 뒤의 문단들은 그 번호의 문제에 붙인다(칸을 도는 차례가 달라 문제가 뒤에 나올 수도 있다)
                current = {"number": int(more.group(1)), "paras": []}
                self.continued.append(current)
                continue
            if lab:
                number, word, strip = lab
                if number is not None and word == "" and last is not None and last + 1 < number <= last + 10 \
                        and not continues(k, number) and any(j > k for j, _ in mains):
                    # 번호를 건너뛰었는데 그 뒤가 이어지지도 않는다: 문제 안의 줄이다('5번 던질 때…')
                    if current is not None:
                        current["paras"].append(par)
                    continue
                if number is not None and word == "" and last is not None and not last < number <= last + 10:
                    tail = [_squash(_first_line(p)) for p in (current["paras"][1:] if current else [])][-3:]
                    heading = any(HEADING_RE.match(t) and len(t) < 30 for t in tail)
                    if not heading and run_length(k) < 3:
                        # 차례에 맞지 않고 뒤로 길게 이어지지도 않는 번호는 문제의 시작이 아니라 문제 안의 줄이다('2. …'로 시작하는 풀이 단계 등)
                        if current is not None:
                            current["paras"].append(par)
                        continue
                    if number == 1 and not heading and (any(ANSWER_RE.match(t) and len(t) < 30 for t in tail) or light(k)):
                        # 1번부터 다시 시작하고 번호마다 내용이 거의 없다: 정답표가 이어지는 것이다
                        if top:
                            self.notes.append(f"{last}번 뒤에서 번호가 1번으로 다시 시작합니다. 그 뒤는 정답으로 보고 넣지 않았습니다")
                        return
                    if top and not heading:
                        note = "번호가 다시 시작하는 묶음이 있어 같은 번호의 문제가 여러 개입니다(번호로 고를 때는 쪽 범위를 같이 주세요)"
                        if note not in self.notes:
                            self.notes.append(note)
                if word == "" and number is not None:
                    last = number
                current = {"number": number, "word": word, "strip": strip, "paras": [par]}
                self.found.append(current)
                continue
            shared = RANGE_RE.match(_lead(par))
            if shared:
                # 공통 발문은 앞 문제의 것이 아니다. 앞 문제에 붙이지 않고, 뒤 문제들에 발문이 빠진다는 사실을 알린다
                current = None
                self.notes.append(f"{shared.group(1)}~{shared.group(2)}번은 함께 쓰는 발문이 있는 문제입니다. 발문은 문서에서 그대로 꺼내지 "
                                  "못하니, 이 문제들은 `look`으로 그 쪽을 보고 `add --stem`(발문의 범위)으로 발문과 함께 담거나, 선택 화면의 "
                                  "`직접 그리기`로 발문까지 감싸서 담아 주세요")
                continue
            grids = [t for t in par.findall(_tag("run") + "/" + _tag("tbl")) if not _page_anchored(t) and self.has_labels(t)]
            if grids:
                current = None                   # 문제들을 칸에 나눠 담은 표: 칸마다 따로 본다
                for tbl in grids:
                    for tr in tbl.findall(_tag("tr")):
                        for tc in tr.findall(_tag("tc")):
                            sub = tc.find(_tag("subList"))
                            self.walk(sub.findall(_tag("p")) if sub is not None else [])
                continue
            if current is not None:
                current["paras"].append(par)

    def tidy(self) -> None:
        """문제 뒤에 딸려 온 것(끝 표시와 그 뒤, 다음 묶음의 제목)을 떼어 낸다."""
        first_line = _first_line
        for more in self.continued:
            owner = next((f for f in reversed(self.found) if f["number"] == more["number"] and not f["word"]), None)
            if owner is not None:
                owner["paras"].extend(more["paras"])
        self.continued = []
        # 문제마다 '정답'·'해설' 줄이 있으면 정답·해설 문서다. 그런 문서에서는 그 줄이 내용이므로 자르지 않는다
        answered = sum(1 for f in self.found if any(ANSWER_RE.match(first_line(p)) for p in f["paras"][1:]))
        cut_answers = answered < max(2, len(self.found) // 2)
        for f in self.found:
            paras = f["paras"]
            for k in range(1, len(paras)):
                text = first_line(paras[k])
                if len(text) < 30 and (STOP_RE.match(text) or (cut_answers and f is self.found[-1] and ANSWER_RE.match(text))):
                    del paras[k:]
                    break
            while len(paras) > 1:
                # 글자 사이를 띄워 쓴 제목('단 답 형')이나 한 칸짜리 표에 든 제목도 알아보게, 빈칸을 지우고 안의 글까지 본다
                last = paras[-1]
                mine = [e for e in last.iter() if isinstance(e.tag, str) and not _under_anchored(e, last)]   # 쪽에 붙박인 안내 상자는 빼고 본다
                kinds = {E.QName(e).localname for e in mine}
                text = _squash("".join(_Reader.text_of(e) for e in mine if E.QName(e).localname == "t")) if "equation" not in kinds else "x" * 99
                if not text and not kinds & {"pic", "tbl"}:
                    paras.pop()                  # 빈 문단
                elif (HEADING_RE.match(text) and len(text) < 30 and "pic" not in kinds) or SUBJECT_RE.fullmatch(text):
                    paras.pop()
                else:
                    break

    def score(self) -> tuple[int, int]:
        """(차례대로 이어지는 번호가 가장 길게 몇 개인가, 내용이 있는 문제가 몇 개인가).

        개수만 보면 문제마다 딸린 작은 문제 번호(1), 2))가 문제 번호(1번~20번)를 이긴다. 그래서 길게 이어지는 쪽이 먼저다.
        """
        solid = [f for f in self.found if _weight(f["paras"][0], f["strip"]) + sum(_weight(p) for p in f["paras"][1:]) >= 6]
        numbers = sorted({f["number"] for f in solid if f["number"] is not None and not f["word"]})
        best = run = 0
        for a, b in zip([None] + numbers, numbers):
            run = run + 1 if a is not None and b == a + 1 else 1
            best = max(best, run)
        auto = sum(1 for f in solid if f["number"] is None)
        return (max(best, auto), len(solid))


def parse_document(hwpx: Path, max_figure_mm: tuple[float, float] = (84.0, 75.0)) -> Parsed:
    """HWPX에서 문제들을 문서에 놓인 차례대로 꺼낸다. 문제 번호를 알아볼 수 없는 문서면 문제가 없는 결과를 준다."""
    with ZipFile(hwpx) as z:
        names = set(z.namelist())
        parts = sorted((n for n in names if re.fullmatch(r"Contents/section\d+\.xml", n)), key=lambda n: int(re.search(r"\d+", n).group()))
        sections = [E.fromstring(z.read(n)) for n in parts]
        head = E.fromstring(z.read("Contents/header.xml"))
        package = E.fromstring(z.read("Contents/content.hpf")) if "Contents/content.hpf" in names else None
    centered: set[str] = set()
    headings: dict[str, str] = {}                # 문단 모양 → 자동 번호 방식의 이름
    for p in head.iter(_tag("paraPr", HH)):
        align = next(iter(p.iter(_tag("align", HH))), None)
        if align is not None and align.get("horizontal") == "CENTER":
            centered.add(p.get("id"))
        h = next(iter(p.iter(_tag("heading", HH))), None)
        if h is not None and h.get("type", "NONE") in ("NUMBER", "OUTLINE"):
            headings[p.get("id")] = f"auto:{h.get('type')}:{h.get('level')}"
    margins = (0.0, 0.0)                         # 쪽의 본문이 시작하는 자리(왼쪽, 위). 쪽 기준 개체의 자리를 종이 기준으로 바꿀 때 쓴다
    page = next((e for root in sections for e in root.iter(_tag("margin")) if e.get("header") is not None), None)
    if page is not None:
        margins = (int(page.get("left", 0)) / 100, (int(page.get("top", 0)) + int(page.get("header", 0))) / 100)
    members = {}
    for item in (package.iter() if package is not None else []):
        if E.QName(item).localname == "item" and item.get("href", "") in names:
            members[item.get("id")] = item.get("href")

    # 아래에서 문단을 id()로 기억해 둔다. lxml은 붙들고 있지 않은 요소의 파이썬 객체를 버렸다가 다시 만들기 때문에,
    # 모든 문단을 여기서 붙들어 두지 않으면 서로 다른 문단이 같은 id를 갖게 된다
    keep = [p for root in sections for p in root.iter(_tag("p"))]

    def bodies() -> list[list]:
        return [root.findall(_tag("p")) for root in sections]

    used = {headings[p.get("paraPrIDRef")] for root in sections for p in root.iter(_tag("p")) if p.get("paraPrIDRef") in headings}
    best = None
    for family in [f for f, _ in FAMILIES] + sorted(used):
        sp = _Splitter(family, headings)
        sp.calibrate(keep)
        for body in bodies():
            sp.walk(body, top=True)
        sp.tidy()
        if best is None or sp.score() > best.score():
            best = sp
    # 문제가 하나뿐이면 우연히 번호처럼 보이는 줄일 수 있다. 칸 머리에 'N번.'만 한 줄 있는 꼴(이 도구가 만든 문서)일 때만 받아들인다
    alone = best is not None and best.score()[1] == 1 and best.family == "번" and len(best.found) == 1 \
        and _weight(best.found[0]["paras"][0], best.found[0]["strip"]) == 0
    if best is None or (best.score()[1] < 2 and not alone):
        return Parsed([])
    slots = []
    for f in best.found:
        rd = _Reader(centered, members, max_figure_mm, margins)
        rd.strip[id(f["paras"][0])] = f["strip"]
        rd.paragraphs(f["paras"])
        text = "\n".join(rd.lines)
        if not text.strip():
            if not (rd.issues or rd.floating):
                continue                         # 번호만 있고 내용이 없는 칸
            # 글 없이 도형이나 쪽 기준 그림만 있는 문제: 빼 버리면 뒤 문제들의 자리가 한 칸씩 밀린다. 그림을 읽는 길로 보낸다
            rd.issues.append("글 없이 도형·그림만 있는 문제입니다")
        plain = re.sub(r"\$[^$]*\$|<!--.*?-->|!\[\]\([^)]*\)|^:::.*$", "", text, flags=re.M)
        slots.append(Slot(f["number"], text, rd.figures, rd.issues,
                          label=(f["word"] + str(f["number"])) if f["word"] else (str(f["number"]) if f["number"] is not None else ""),
                          inline=_weight(f["paras"][0], f["strip"]) > 0, head="".join(re.findall(r"[가-힣]", plain))[:14],
                          word=f["word"], floating=rd.floating))
    del keep
    return Parsed(slots, best.family, best.notes)


def parse(hwpx: Path, max_figure_mm: tuple[float, float] = (84.0, 75.0)) -> list[Slot]:
    return parse_document(hwpx, max_figure_mm).slots


def extract(hwpx: Path, member: str) -> bytes:
    with ZipFile(hwpx) as z:
        return z.read(member)
