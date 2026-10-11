"""양식 어댑터: 양식(HWPX)에 문제마다 "여기에 무엇을 넣을지" 표식 문단을 심는다.

지금 지원하는 양식은 프로젝트의 `양식.hwp`(A4, 쪽마다 2×2칸, 칸 머리에 'N번.')이다. 구조는 이렇다.
칸은 왼쪽 단을 위에서 아래로, 그다음 오른쪽 단을 위에서 아래로 채운다. 문제 하나가 칸 하나에 다 들어가지 않으면
(writer가 한글이 계산한 높이로 알아낸다) 아래 칸과 합친 긴 칸에 넣고, 그래도 넘치면 남은 부분을 다음 칸에
'N번. (이어서)'라는 머리를 달아 이어 쓴다(arrange).

    본문[0]  머리말 표(회차 이름 자리와, 문제 수가 들어갈 '(문제)' 자리 포함)
    본문[1]  첫 쪽의 2×2 표
    본문[2]  둘째 쪽부터 쓰는 2×2 표(칸이 더 높다)

기본으로는 첫 쪽(머리말과 그 아래의 낮은 표)을 쓰지 않고 둘째 쪽의 표만으로 문서를 만든다. 머리말은 학원의 일일 테스트용이라
(로고, 점수 칸) 교재에서 뽑은 문제 모음에는 맞지 않는다는 사용자의 판단이다. 첫 쪽을 쓰려면 scaffold에 first_page=True를 준다
(작업 폴더 설정의 "first_page": true).

글·수식·그림은 한글 프로그램이 직접 넣어야 모양이 맞으므로, 여기서는 자리만 잡는다. 다른 양식을 쓰려면
이 파일의 `scaffold`만 그 양식에 맞게 바꾸면 된다(돌려주는 값의 모양만 지키면 writer는 그대로 쓴다).
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree as E

import re

from ..transcript import CIRCLED, RAW_EQUATION, Boxed, Choices, Figure, Para, Run

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
SLOTS_PER_PAGE = 4
TITLE_PLACEHOLDER = "(여기에 회차 이름을 입력)"
COUNT_PLACEHOLDER = "(문제)"       # 머리말의 점수 칸 '    / (문제)'에서 문제 수가 들어갈 자리
BOX_BORDER = "2"            # 양식의 테두리 모양 가운데 사방이 실선인 것(보기 상자에 쓴다)
NO_BORDER = "3"             # 테두리가 없는 것(선택지를 줄 맞춰 늘어놓는 표에 쓴다)
UNIT_PER_HALF_EM = 500      # 본문 글자(10pt)에서 영문·숫자 한 글자가 차지하는 폭쯤(HWPUNIT)
MARKER = "HWPICK{:06d}END"


def _tag(name: str) -> str:
    return "{" + HP + "}" + name


FUNCTION_NAMES = ("sin", "cos", "tan", "log", "ln", "lim", "max", "min", "sec", "csc", "cot")
# 폭이 없는 명령(모양만 바꾸거나 다른 글자를 감싸는 것)
NO_WIDTH = ("frac", "left", "right", "mathrm", "mathit", "mathbf", "text", "overline", "bar", "vec", "hat",
            "begin", "end", "displaystyle", "limits")
FRAC_RE = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")


def _equation_width(latex: str) -> float:
    """수식의 폭 어림(영문·숫자 반 글자 폭 단위). 실제보다 조금 넉넉하게 잡는다."""
    if latex.startswith(RAW_EQUATION):
        # 한글 수식 문법 그대로인 수식: 낱말 명령(over, sqrt, TIMES …)은 기호 하나로 치고 나머지 글자를 센다
        script = latex[len(RAW_EQUATION):]
        words = re.findall(r"[A-Za-z]{2,}", script)
        body = re.sub(r"[A-Za-z]{2,}|[{}^_\s]", "", script)
        return 2.0 * len(words) + sum(2.0 if ch in "=+-<>" else 1.2 for ch in body)

    def frac(m):                                            # 분수의 폭은 분자·분모 가운데 긴 쪽
        return "0" * int(max(_equation_width(m.group(1)), _equation_width(m.group(2))) / 1.2 + 1)
    prev = None
    while prev != latex:
        prev, latex = latex, FRAC_RE.sub(frac, latex)
    half = 0.0
    for cmd in re.findall(r"\\([a-zA-Z]+)", latex):
        if cmd in FUNCTION_NAMES:
            half += 1.1 * len(cmd)
        elif cmd == "sqrt":
            half += 1.5
        elif cmd not in NO_WIDTH:
            half += 2.0                                     # 연산 기호(× ÷ ≤ …)나 그리스 문자
    body = re.sub(r"\\[a-zA-Z]+|[{}^_\\\s]", "", latex)
    return half + sum(2.0 if ch in "=+-<>" else 1.2 for ch in body)


def _width_guess(runs: list[Run]) -> int:
    """글과 수식이 차지할 폭을 어림한다(HWPUNIT). 선택지를 한 줄에 몇 개 놓을지 정할 때만 쓴다."""
    half = 0.0
    for r in runs:
        if r.eq:
            half += _equation_width(r.latex)
        else:
            half += sum(2 if ord(ch) > 0x2E80 else 1 for ch in r.text)
    return int(half * UNIT_PER_HALF_EM)


def choice_columns(options: list[list[Run]], width: int, pad: int = 0) -> int:
    """선택지를 한 줄에 몇 개씩 놓을지: 다 들어가면 한 줄, 아니면 3개씩, 그것도 안 되면 한 줄에 하나.

    pad는 칸 하나의 좌우 여백이다. 칸에는 번호(①)와 띄어쓰기도 들어간다.
    """
    widest = max((_width_guess(o) for o in options), default=0) + int(3.6 * UNIT_PER_HALF_EM) + pad
    for cols in (len(options), 3, 2):
        if cols <= len(options) and widest * cols <= width:
            return cols
    return 1


@dataclass
class Operation:
    """표식 하나에 넣을 내용."""
    marker: str
    block: Para | Choices | Figure
    problem: int                  # 몇 번째 문제인가(0부터)
    width: int                    # 이 자리에서 쓸 수 있는 폭(HWPUNIT, 1mm ≈ 283.5)


@dataclass
class Cell:
    """칸 하나에 넣을 것: 어느 문제(0부터)의 몇 번째 블록부터 몇 번째 블록 앞까지인가. start가 0이 아니면 앞 칸에서 이어지는 칸이다."""
    problem: int
    start: int
    end: int


@dataclass
class Scaffold:
    operations: list[Operation]
    pages: int
    columns: list[list]           # 단마다 [위 칸, 아래 칸] 또는 [합친 칸 하나]. 칸은 Cell이거나 빈칸(None)
    cells: list                   # 문서에 놓인 차례(쪽 → 줄 → 칸)대로 본 칸들(measure가 한글이 저장한 문서와 맞춰 보는 데 쓴다)
    room: list[tuple[int, int, int]]   # [첫 쪽, 그 뒤 쪽]의 (윗줄 칸 높이, 아랫줄 칸 높이, 칸의 위아래 여백 합)


CONTINUED = "{}번. (이어서)"   # 앞 칸에서 이어지는 칸의 머리(source.py가 이 글로 이어지는 칸을 알아본다)


def _set_text(par, text: str) -> None:
    """문단의 글자를 바꾼다(첫 run만 남긴다)."""
    runs = par.findall(_tag("run"))
    for r in runs[1:]:
        par.remove(r)
    for child in list(runs[0]):
        runs[0].remove(child)
    E.SubElement(runs[0], _tag("t")).text = text
    for seg in par.findall(_tag("linesegarray")):
        par.remove(seg)


def plain(problems: list[list]) -> list[list]:
    """문제 하나에 칸 하나씩인 보통의 배치."""
    cells: list = [Cell(i, 0, len(blocks)) for i, blocks in enumerate(problems)]
    cells += [None] * (-len(cells) % 2)
    return [cells[k:k + 2] for k in range(0, len(cells), 2)]


def scaffold(source: Path, destination: Path, problems: list[list], title: str, first_page: bool = False,
             layout: list[list] | None = None) -> Scaffold:
    """양식 HWPX(source)에 문제들의 표식을 심어 destination에 쓴다. problems는 문제별 블록 목록이다.
    layout은 단마다 넣을 칸들이다(arrange가 만든 것). 주지 않으면 문제 하나에 칸 하나씩 넣는다.

    first_page가 거짓이면 양식의 첫 쪽(머리말과 낮은 표)을 빼고 둘째 쪽의 표로 첫 쪽부터 채운다. 이때 회차 이름(title)은
    문서에 들어가지 않는다(머리말에 있던 자리다).
    """
    with ZipFile(source) as z:
        files = {name: z.read(name) for name in z.namelist()}
    root = E.fromstring(files["Contents/section0.xml"])
    if len(root) < 3 or root[1].find(".//" + _tag("tbl")) is None:
        raise ValueError("양식의 구조가 예상과 다릅니다(머리말, 첫 쪽 표, 다음 쪽 표가 차례로 있어야 합니다)")
    header, first, later = deepcopy(root[0]), deepcopy(root[1]), deepcopy(root[2])
    table_model = deepcopy(first.find(".//" + _tag("tbl")))
    cell_model = deepcopy(first.find(".//" + _tag("tc")))
    label_model, body_model = (deepcopy(p) for p in cell_model.find(_tag("subList")).findall(_tag("p"))[:2])
    cell_width = int(cell_model.find(_tag("cellSz")).get("width"))
    margin = cell_model.find(_tag("cellMargin"))
    inner = cell_width - int(margin.get("left")) - int(margin.get("right"))
    for node in list(root):
        root.remove(node)
    for t in header.iter(_tag("t")):
        if t.text and TITLE_PLACEHOLDER in t.text:
            t.text = t.text.replace(TITLE_PLACEHOLDER, title)
        if t.text and COUNT_PLACEHOLDER in t.text:
            t.text = t.text.replace(COUNT_PLACEHOLDER, str(len(problems)))      # '    / 8'처럼 맞은 수를 적는 칸의 분모
    if first_page:
        root.append(header)
    # 쪽 크기·여백·단 설정은 문서의 맨 첫 문단에 들어 있다(머리말 문단). 머리말을 빼면 첫 표의 문단으로 옮겨 준다
    setup = [] if first_page else [deepcopy(node) for run in header.findall(_tag("run")) for node in run
                                   if node.tag == _tag("secPr") or (node.tag == _tag("ctrl") and node.find(_tag("colPr")) is not None)]

    operations: list[Operation] = []
    next_id = 2040000000

    def body(text: str = ""):
        p = deepcopy(body_model)
        _set_text(p, text)
        return p

    def inner_table(width: int, rows: int, cols: int, border: str, valign: str = "TOP"):
        """문단 안에 글자처럼 놓는 작은 표. 칸마다 내용을 담을 자리(subList)를 줄 순서대로 돌려준다."""
        nonlocal next_id
        next_id += 1
        t = deepcopy(table_model)
        t.set("id", str(next_id))
        t.set("rowCnt", str(rows))
        t.set("colCnt", str(cols))
        t.set("repeatHeader", "0")
        t.set("noAdjust", "0")
        t.set("pageBreak", "NONE")
        t.set("borderFillIDRef", border)
        for tr in t.findall(_tag("tr")):
            t.remove(tr)
        height = 1400
        t.find(_tag("sz")).set("width", str(width))
        t.find(_tag("sz")).set("height", str(height * rows))
        t.find(_tag("pos")).set("treatAsChar", "1")
        for key in ("left", "right", "top", "bottom"):
            t.find(_tag("outMargin")).set(key, "0")
        subs = []
        for r in range(rows):
            tr = E.SubElement(t, _tag("tr"))
            for c in range(cols):
                cell = deepcopy(cell_model)
                cell.set("borderFillIDRef", border)
                cell.find(_tag("cellAddr")).set("rowAddr", str(r))
                cell.find(_tag("cellAddr")).set("colAddr", str(c))
                cell.find(_tag("cellSz")).set("width", str(width // cols))
                cell.find(_tag("cellSz")).set("height", str(height))
                sub = cell.find(_tag("subList"))
                sub.set("vertAlign", valign)
                for p in list(sub):
                    sub.remove(p)
                subs.append(sub)
                tr.append(cell)
        return t, subs

    pad = int(margin.get("left")) + int(margin.get("right"))

    def mark(sub, block, problem: int, width: int) -> None:
        operations.append(Operation(MARKER.format(len(operations) + 1), block, problem, width))
        sub.append(body(operations[-1].marker))

    def hold(sub, table) -> None:
        holder = body()
        holder.find(_tag("run")).insert(0, table)
        sub.append(holder)

    def place(sub, blocks: list, problem: int, width: int) -> None:
        for b in blocks:
            if isinstance(b, Boxed):
                t, (inside,) = inner_table(width, 1, 1, BOX_BORDER)
                if b.label:
                    mark(inside, Para(runs=[_label_run(b.label)], center=True), problem, width - pad)
                place(inside, b.blocks, problem, width - pad)
                if len(inside) == 0:
                    inside.append(body())
                hold(sub, t)
            elif isinstance(b, Choices):
                # 선택지는 테두리 없는 표에 한 칸씩 넣어 줄을 맞춘다
                # 원본 문서가 선택지를 한 줄에 몇 개씩 놓았는지 알면 따르되, 이 칸에 들어가는 만큼만(원본의 칸이 더 넓었을 수 있다)
                fits = choice_columns(b.options, width, pad)
                same = b.cols_width and abs(b.cols_width - width) <= 0.03 * width      # 원본의 칸 폭이 이 칸과 같다(이 양식으로 만든 문서)
                cols = b.cols if 0 < b.cols <= len(b.options) and (b.cols <= fits or same) else fits
                rows = -(-len(b.options) // cols)
                # 분수 선택지와 숫자 선택지가 한 줄에 섞이면 높이가 달라지므로 칸 가운데에 맞춘다
                t, cells = inner_table(width, rows, cols, NO_BORDER, "CENTER")
                for k, cell in enumerate(cells):
                    if k < len(b.options):
                        mark(cell, Para(runs=[Run(text=CIRCLED[k] + " ")] + b.options[k]), problem, width // cols - pad)
                    else:
                        cell.append(body())
                hold(sub, t)
            else:
                mark(sub, b, problem, width)

    def fill(cell, spec: Cell | None) -> None:
        sub = cell.find(_tag("subList"))
        for p in list(sub):
            sub.remove(p)
        if spec is None:
            sub.append(body())
            return
        label = deepcopy(label_model)
        _set_text(label, f"{spec.problem + 1}번." if spec.start == 0 else CONTINUED.format(spec.problem + 1))
        sub.append(label)
        place(sub, problems[spec.problem][spec.start:spec.end], spec.problem, inner)

    def height(cell) -> int:
        return int(cell.find(_tag("cellSz")).get("height"))

    def room_of(model) -> tuple[int, int, int]:
        rows = model.find(".//" + _tag("tbl")).findall(_tag("tr"))
        m = rows[0].find(_tag("tc")).find(_tag("cellMargin"))
        return height(rows[0].find(_tag("tc"))), height(rows[1].find(_tag("tc"))), int(m.get("top")) + int(m.get("bottom"))

    columns = [list(c) for c in (layout if layout is not None else plain(problems))] or [[None, None]]
    if len(columns) % 2:
        columns.append([None, None])
    cells: list = []
    pages = 0
    for start in range(0, len(columns), 2):
        page = deepcopy(first if start == 0 and first_page else later)
        page.set("pageBreak", "0" if start == 0 else "1")
        if start == 0 and setup:
            for k, node in enumerate(setup):
                page.find(_tag("run")).insert(k, node)
        next_id += 1
        table = page.find(".//" + _tag("tbl"))
        table.set("id", str(next_id))
        top, bottom = table.findall(_tag("tr"))
        grid = [top.findall(_tag("tc")), bottom.findall(_tag("tc"))]
        lower: list = []
        for col in (0, 1):                               # 왼쪽 단을 위에서 아래로 채운 뒤 오른쪽 단
            specs = columns[start + col]
            if len(specs) == 1:
                # 아래 칸과 합친 긴 칸: 위 칸이 두 줄을 차지하고, 아래 칸은 표에서 뺀다
                grid[0][col].find(_tag("cellSpan")).set("rowSpan", "2")
                grid[0][col].find(_tag("cellSz")).set("height", str(height(grid[0][col]) + height(grid[1][col])))
                bottom.remove(grid[1][col])
                fill(grid[0][col], specs[0])
            else:
                fill(grid[0][col], specs[0])
                fill(grid[1][col], specs[1])
                lower.append(specs[1])
        if not lower:
            # 두 단을 모두 합쳤으면 아랫줄에 칸이 하나도 없다. 한 줄짜리 표로 만든다
            table.remove(bottom)
            table.set("rowCnt", "1")
            for cell in grid[0]:
                cell.find(_tag("cellSpan")).set("rowSpan", "1")
        cells += [columns[start][0], columns[start + 1][0]] + lower
        root.append(page)
        pages += 1
    for node in list(root.iter(_tag("linesegarray"))):
        node.getparent().remove(node)
    files["Contents/section0.xml"] = E.tostring(root, encoding="utf-8", xml_declaration=True, standalone=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return Scaffold(operations, pages, columns, cells, [room_of(first if first_page else later), room_of(later)])


def measure(hwpx: Path, plan: Scaffold) -> dict[int, tuple[int, list[tuple[int, int]]]] | None:
    """내용을 다 넣고 한글이 저장한 HWPX에서, 문제마다 실제로 차지한 높이를 읽는다.

    돌려주는 값: {문제(0부터): (번호 줄의 높이, [블록마다 (높이, 그 가운데 마지막 줄 아래의 줄 간격)])}. 단위는 HWPUNIT.
    한글은 문단마다 줄의 자리(lineseg)를 적어 둔다. 수식·그림·선택지 표의 높이도 거기에 들어 있어서 어림할 필요가 없다.
    문서의 짜임이 심어 둔 것과 다르면(읽을 수 없으면) None.
    """
    with ZipFile(hwpx) as z:
        root = E.fromstring(z.read("Contents/section0.xml"))
    found = []
    for top in root:
        table = top.find(".//" + _tag("tbl"))
        if table is not None and int(table.get("colCnt", "0")) == 2 and table.find(_tag("tr")) is not None:
            found += [tc for tr in table.findall(_tag("tr")) for tc in tr.findall(_tag("tc"))]
    if len(found) != len(plan.cells):
        return None
    sizes: dict[int, tuple[int, dict[int, tuple[int, int]]]] = {}
    for tc, spec in zip(found, plan.cells):
        if spec is None:
            continue
        paras = tc.find(_tag("subList")).findall(_tag("p"))
        if len(paras) != 1 + spec.end - spec.start:
            return None
        got = []
        for p in paras:
            segs = p.findall(_tag("linesegarray") + "/" + _tag("lineseg"))
            if not segs:
                return None
            got.append((sum(int(s.get("vertsize")) + int(s.get("spacing")) for s in segs), int(segs[-1].get("spacing"))))
        label, blocks = sizes.setdefault(spec.problem, (got[0][0], {}))
        for k, size in zip(range(spec.start, spec.end), got[1:]):
            blocks[k] = size
    out = {}
    for problem, (label, blocks) in sizes.items():
        if sorted(blocks) != list(range(len(blocks))):
            return None
        out[problem] = (label, [blocks[k] for k in range(len(blocks))])
    return out


def arrange(sizes: dict[int, tuple[int, list[tuple[int, int]]]], count: int, room: list[tuple[int, int, int]],
            slack: int = 150) -> list[list]:
    """잰 높이로 문제들을 칸에 다시 나눈다. 차례는 바꾸지 않는다.

    칸 하나에 다 들어가는 문제는 그대로 한 칸이다. 넘치는 문제는
      - 윗칸에서 시작하면 아래 칸과 합친 긴 칸에 넣고, 그래도 남으면 다음 단의 윗칸에서 이어 쓴다(또 넘치면 또 합친다)
      - 아랫칸에서 시작하면 들어가는 블록까지만 넣고 다음 단의 윗칸에서 이어 쓴다. 첫 블록부터 안 들어가면 그 칸은 비우고 다음 단에서 시작한다
    블록(문단, 그림, 보기 상자, 선택지) 하나는 쪼개지 않는다. slack은 칸 높이에서 덜어 두는 여유다.
    """
    columns: list[list] = []
    cur: list = []

    def cap(tall: bool) -> int:
        top, bottom, pad = room[0 if len(columns) < 2 else 1]
        return (top + bottom if tall else (bottom if cur else top)) - pad - slack

    def close() -> None:
        nonlocal cur
        if len(cur) == 2:
            columns.append(cur)
            cur = []

    for i in range(count):
        label, blocks = sizes.get(i, (0, []))

        def need(a: int, b: int) -> int:
            return label + sum(h for h, _ in blocks[a:b]) - (blocks[b - 1][1] if b > a else 0)

        def reach(a: int, limit: int) -> int:
            b = a
            while b < len(blocks) and need(a, b + 1) <= limit:
                b += 1
            return b

        k = 0
        while True:
            if need(k, len(blocks)) <= cap(False):
                cur.append(Cell(i, k, len(blocks)))
                close()
                break
            if not cur:
                j = max(k + 1, reach(k, cap(True)))          # 윗칸: 아래 칸과 합친다
                columns.append([Cell(i, k, j)])
            else:
                j = reach(k, cap(False))                      # 아랫칸: 들어가는 데까지만
                cur.append(Cell(i, k, j) if j > k else None)
                close()
            k = j
            if k >= len(blocks) and k > 0:
                break
    if cur:
        cur.append(None)
        close()
    return columns


def _label_run(label: str) -> Run:
    """상자 이름을 '<보 기>'처럼 쓴다."""
    return Run(text="<" + " ".join(label) + ">" if len(label) <= 3 else f"<{label}>")
