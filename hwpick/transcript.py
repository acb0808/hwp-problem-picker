"""전사 결과(Markdown)를 한글 문서에 넣을 블록으로 바꾼다.

전사 형식(서브에이전트가 문제 하나에 파일 하나로 쓴다):

    두 다항식 $A$, $B$에 대하여 ... 의 값은? [4점]
    $$x^2 + 2x + 1 = 0$$                 ← 한 줄을 통째로 차지하는 가운데 수식
    ![](figure:A)                         ← 잘라 둔 그림 A가 들어갈 자리
    ::: box 보기                          ← 테두리 상자(보기·조건). 이름은 없어도 된다
    ㄱ. $a > 0$
    ㄴ. $b < 0$
    :::
    ① $1$  ② $2$  ③ $3$  ④ $4$  ⑤ $5$      ← 선택지(한 줄이든 여러 줄이든)

빈 줄은 무시하고, 줄 하나가 문단 하나다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from latex_to_hwpeqn import convert

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
RAW_EQUATION = "\\hwpeq"          # `$\hwpeq …$`: LaTeX가 아니라 한글 수식 문법 그대로인 수식(한글 문서에서 꺼낸 것)
COLS_RE = re.compile(r"<!--\s*cols\s*:\s*(\d+)(?:\s+w\s*:\s*(\d+))?\s*-->")
FIGURE_RE = re.compile(r"!\[[^\]]*\]\(figure:([A-Za-z0-9_-]+)\)")
INLINE_RE = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.S)
# 빈칸 표시: □, \square, \Box, 속이 빈 \boxed{}
BLANK_RE = re.compile(r"□|\\(?:square|Box)(?![a-zA-Z])|\\boxed\{(?:\s|\\[,;:! ]|\\q?quad(?![a-zA-Z])|\\phantom\{[^{}]*\})*\}")
BLANK_STAND_IN = r"\Xi"
# 안에 표시가 든 빈칸(교재에서 네모 안에 (가)가 들어 있는 것): \boxed{(가)} → 한글 수식의 BOX{ }. 변환기는 \boxed를 모르므로
# 속을 따로 바꾼 뒤, 그 자리에 대역 글자를 넣어 통과시키고 되돌린다
BOX_STAND_IN, BOX_NAME = r"\Psi", "Psi"
# 모델이 표시 든 빈칸을 제각각 적어 온다(□(가), \square_{(가)}, \square\text{(가)}\square, \text{□(가)}). 모두 \boxed{(가)}로 모은다.
# 표시는 (가)·(ㄱ)·㉠ 꼴만 본다. □ 뒤의 (1)은 작은 문제 번호일 수 있어 건드리지 않는다
BLANK_LABEL = r"(?:\(\s*[가-힣ㄱ-ㅎ]\s*\)|[㉠-㉻㈀-㈛])"
_BLANK = r"(?:□|\\square(?![a-zA-Z])|\\Box(?![a-zA-Z]))"
LABELLED_BLANKS = [re.compile(p) for tl in (rf"\\(?:text|mathrm)\s*\{{\s*({BLANK_LABEL})\s*\}}", rf"({BLANK_LABEL})") for p in (
    rf"{_BLANK}\s*_\s*\{{\s*{tl}\s*\}}", rf"{_BLANK}\s*{tl}\s*{_BLANK}", rf"{_BLANK}\s*{tl}")]
LABELLED_BLANKS.append(re.compile(rf"\\(?:text|mathrm)\s*\{{\s*□\s*({BLANK_LABEL})\s*□?\s*\}}"))
PRESCRIPT_RE = re.compile(r"(^|[=+\-<>(\[,;:{]|\\(?:cdot|times|div|leq?|geq?|neq|therefore|because|q?quad|to|pm|mp)(?![a-zA-Z])|\\[,;:! ])"
                          r"(\s*)(?=[_^]\s*[{\\A-Za-z0-9])")
TEXT_BLANK_RE = re.compile(rf"□\s*({BLANK_LABEL})(?:\s*□)?")
# 수식 안에 맨몸으로 적힌 표시 `(가)`: 수식 속의 (가)는 빈칸 상자일 수밖에 없다(가벼운 모델은 상자 없이 표시만 적어 오는 일이 잦다)
BARE_LABEL_RE = re.compile(rf"\\boxed\s*\{{\s*{BLANK_LABEL}\s*\}}|\\(?:text|mathrm)\s*\{{\s*({BLANK_LABEL})\s*\}}|({BLANK_LABEL})")


def _box_bare(src: str) -> str:
    """수식 하나에서 맨몸 표시를 `\\boxed{(가)}`로 바꾼다. 수식이 표시 하나뿐이면(글 속의 `$(가)$`) 그대로 둔다."""
    src = re.sub(r"\(\s*\\(?:text|mathrm)\s*\{\s*([가-힣ㄱ-ㅎ])\s*\}\s*\)", r"(\1)", src)      # `(\text{가})`도 같은 표시다
    if LABEL_ONLY_RE.fullmatch(src):
        return src
    src = re.sub(rf"\\boxed\s*\{{\s*\\(?:text|mathrm)\s*\{{\s*({BLANK_LABEL})\s*\}}\s*\}}", lambda m: "\\boxed{" + m.group(1) + "}", src)
    return BARE_LABEL_RE.sub(lambda m: m.group(0) if not (m.group(1) or m.group(2)) else "\\boxed{" + (m.group(1) or m.group(2)) + "}", src)


LABEL_ONLY_RE = re.compile(rf"\s*(?:\\(?:text|mathrm)\s*\{{)?\s*({BLANK_LABEL})\s*\}}?\s*")


def box_labels(text: str) -> str:
    """글 전체에서, 수식 안의 맨몸 표시를 상자로 바꾼다(여러 번 읽은 것을 견주기 전에 표기를 맞추는 데 쓴다).
    표시 하나뿐인 수식(`$\\text{(가)}$`)은 수식을 벗겨 글자 `(가)`로 둔다."""
    def fix(m: re.Match) -> str:
        mark, body = ("$$", m.group(1)) if m.group(1) is not None else ("$", m.group(2))
        body = re.sub(r"\(\s*\\(?:text|mathrm)\s*\{\s*([가-힣ㄱ-ㅎ])\s*\}\s*\)", r"(\1)", body)
        only = LABEL_ONLY_RE.fullmatch(body)
        return only.group(1) if only else mark + _box_bare(body) + mark
    text = re.sub(r"\$\$(.+?)\$\$|\$([^$\n]+)\$", fix, text, flags=re.S)
    # 수식과 수식 사이에 혼자 나와 있는 표시(`$… \\cdot$ (가) $= …$`)도 빈칸 상자다. 문장 속의 표시(뒤에 한글이나 쉼표가 온다)와
    # 조건 목록의 머리 `(가) $f(x)$…`(앞에 수식이 없다)는 건드리지 않는다
    return OUTSIDE_LABEL_RE.sub(lambda m: m.group(1) + "$\\boxed{" + m.group(2) + "}$", text)


OUTSIDE_LABEL_RE = re.compile(rf"(?<=\$)([ \t]*)({BLANK_LABEL})(?=[ \t]*(?:\$|[=+\-·×÷]|$))", re.M)


# 변환기가 모르는 것을, 초·중·고 수학에 나오지 않는 글자로 바꿔 통과시킨 뒤 한글 수식의 표기로 되돌린다:
# (LaTeX에서 찾을 것, 대신 넣을 명령, 변환 결과에 나오는 이름, 한글 수식 표기)
STAND_INS = [(BLANK_RE, BLANK_STAND_IN, "Xi", "□"),
             (re.compile(r"\\parallel(?![a-zA-Z])"), r"\Upsilon", "Upsilon", "PARALLEL")]
UPRIGHT_FUNCTIONS = ("sinh", "cosh", "tanh", "sin", "cos", "tan", "sec", "csc", "cot", "log", "ln", "exp")
# 변환기가 모르는 흔한 표기를 같은 뜻의 아는 표기로 바꾼다
LATEX_FIXES = [(r"\\dfrac", r"\\frac"), (r"\\tfrac", r"\\frac"), (r"\\displaystyle", ""), (r"\\mid(?![a-zA-Z])", "|"),
               (r"\\lvert|\\rvert", "|"), (r"\\leqq", r"\\le"), (r"\\geqq", r"\\ge"), (r"\\ne(?![a-zA-Z])", r"\\neq"),
               (r"\\text\{", r"\\mathrm{"), (r"\\operatorname\{", r"\\mathrm{"),
               # 모델이 명령 대신 기호 글자를 그대로 적는 경우
               ("∠", r"\\angle "), ("△", r"\\triangle "), ("°", r"^\\circ "), ("⊥", r"\\perp "), ("∥", r"\\parallel ")]


@dataclass
class Run:
    """문단 안의 한 조각. eq가 비어 있으면 글자, 아니면 수식이다."""
    text: str = ""
    latex: str = ""
    eq: str = ""                  # 한글 수식 편집기 문법


@dataclass
class Para:
    runs: list[Run]
    center: bool = False


@dataclass
class Figure:
    name: str


@dataclass
class Choices:
    options: list[list[Run]]
    cols: int = 0                 # 한 줄에 놓을 개수(0이면 폭을 어림해서 정한다)
    cols_width: int = 0           # 그 개수로 놓였던 원본 표의 폭(HWPUNIT). 지금 칸의 폭과 같으면 어림과 달라도 그대로 따른다


@dataclass
class Boxed:
    label: str
    blocks: list = field(default_factory=list)


@dataclass
class Problem:
    blocks: list
    errors: list[str] = field(default_factory=list)       # 고쳐야 넣을 수 있는 문제
    warnings: list[str] = field(default_factory=list)     # 확인이 필요한 점


def _group_scripts(latex: str) -> tuple[str, bool]:
    """묶지 않은 여러 자리 지수·첨자를 묶는다: x^10 → x^{10}, 2^-1 → 2^{-1}, a_12 → a_{12}.

    LaTeX 규칙대로면 x^10은 'x의 1제곱에 0'이라 한글에도 그렇게 들어간다. 옮겨 적는 쪽은 거의 언제나
    'x의 10제곱'을 뜻하므로 묶어 주되, 고쳤다는 사실은 호출한 쪽이 알 수 있게 돌려준다.
    """
    fixed = re.sub(r"([\^_])(-\d+|\d{2,})", r"\1{\2}", latex)
    return fixed, fixed != latex


# 역슬래시가 빠졌는지 볼 명령어 이름들(네 글자 이상). 수식 안의 맨 낱말에 이 가운데 하나가 들어 있으면 오류로 본다
COMMAND_NAMES = ("frac", "sqrt", "times", "cdot", "dots", "left", "right", "alpha", "beta", "gamma", "delta", "theta", "lambda",
                 "sigma", "omega", "infty", "overline", "underline", "mathrm", "mathbf", "mathit", "text", "angle", "triangle",
                 "circ", "boxed", "begin", "cases", "quad", "therefore", "because", "parallel", "perp", "approx", "equiv",
                 "prod", "subset", "notin", "forall", "exists", "square", "displaystyle", "operatorname", "bmatrix", "pmatrix",
                 "array", "align", "leqq", "geqq", "leqslant", "geqslant", "sinh", "cosh", "tanh", "limits", "binom", "widehat")


def to_hwp_equation(latex: str) -> tuple[str, str]:
    """LaTeX 수식을 한글 수식으로 바꾼다. (한글 수식, 오류 설명)을 돌려주고, 실패하면 한글 수식이 비어 있다."""
    src = latex.strip()
    if re.search(r"[\x00-\x1f]", latex):
        return "", "수식에 깨진 문자가 들어 있습니다(역슬래시가 다른 문자로 바뀐 것 같습니다)"
    # 명령어 앞의 역슬래시가 빠지면(frac{1}{2}) 변환기는 글자들의 곱으로 알고 그냥 통과시킨다
    # \begin{cases}의 이름이나 \mathrm{kcal} 같은 글자 묶음은 원래 낱말이 들어가는 자리라 빼고 본다
    words = re.sub(r"\\(?:begin|end|mathrm|text|operatorname|mathit|mathbf)\s*\{[^{}]*\}", " ", src)
    # 문자들의 곱(`abcd`, `xyzw`)은 낱말이 아니다. 명령어 이름이 든 것만 잡는다
    bare = next((w for w in re.findall(r"(?<![\\a-zA-Z])[a-z]{4,}", words) if any(name in w for name in COMMAND_NAMES)), None)
    if bare:
        return "", f"수식 안에 역슬래시 없는 낱말이 있습니다: {bare}"
    for pat in LABELLED_BLANKS:
        src = pat.sub(lambda m: "\\boxed{" + m.group(1) + "}", src)
    src = _box_bare(src)
    src, boxes, err = _take_boxes(src)
    if err:
        return "", err
    for pat, rep in LATEX_FIXES:
        src = re.sub(pat, rep, src)
    # 앞에 붙는 첨자(순열·조합의 `_{n}\mathrm{P}_{r}`)를 모델이 빈 밑(`{}`) 없이 적어 오면 변환기가 받지 못한다.
    # 첨자가 붙을 글자가 없는 자리(맨 앞, 연산 기호·여는 괄호 뒤)의 첨자에만 빈 밑을 넣는다
    src = PRESCRIPT_RE.sub(lambda m: m.group(1) + m.group(2) + "{}", src)
    for pattern, stand_in, _, _ in STAND_INS:
        src = pattern.sub(lambda m, s=stand_in: s + " ", src)
    res = convert(src)
    if res.ok and res.hwpeqn:
        eq = _hwp_quirks(res.hwpeqn)
        if boxes:
            if len(re.findall(rf"(?<![A-Za-z]){BOX_NAME}(?![A-Za-z])", eq)) != len(boxes):
                return "", "상자(\\boxed)를 넣을 자리를 찾지 못했습니다"
            todo = iter(boxes)
            eq = re.sub(rf"(?<![A-Za-z]){BOX_NAME}(?![A-Za-z])", lambda m: "BOX{~" + next(todo) + "~}", eq)
        return eq, ""
    return "", str(res.error or "변환 실패").replace(BLANK_STAND_IN, "□").replace(BOX_STAND_IN, "\\boxed{…}")


def _take_boxes(src: str) -> tuple[str, list[str], str]:
    """속이 있는 \\boxed{…}를 대역 글자로 바꾸고, 속을 한글 수식으로 바꾼 것들을 차례대로 돌려준다. (수식, 속들, 오류)

    속이 빈 \\boxed{}는 그냥 빈칸(□)이라 건드리지 않는다.
    """
    out, boxes, pos = [], [], 0
    for m in re.finditer(r"\\boxed\s*\{", src):
        if m.start() < pos:
            continue
        depth, k = 1, m.end()
        while k < len(src) and depth:
            depth += {"{": 1, "}": -1}.get(src[k], 0) if src[k - 1] != "\\" else 0
            k += 1
        if depth:
            return src, [], "\\boxed{ 의 괄호가 닫히지 않았습니다"
        whole, inner = src[m.start():k], src[m.end():k - 1]
        if BLANK_RE.fullmatch(whole):
            continue
        eq, err = to_hwp_equation(inner)
        if not eq and not re.search(r'[\\{}"]', inner) and inner.strip():
            eq = '{rm "' + inner.strip() + '" it}'          # ㉠처럼 변환기가 모르는 글자 하나: 글자 그대로 넣는다
        if not eq:
            return src, [], f"상자 안의 내용을 바꾸지 못했습니다: {inner.strip()} ({err})"
        out.append(src[pos:m.start()] + BOX_STAND_IN + " ")
        boxes.append(eq)
        pos = k
    return "".join(out) + src[pos:], boxes, ""


def _hwp_quirks(eq: str) -> str:
    """변환기가 만든 한글 수식을, 한글이 실제로 그리는 모양에 맞게 다듬는다(시험 문서로 확인한 것들)."""
    for _, _, name, mark in STAND_INS:
        eq = re.sub(rf"(?<![A-Za-z]){name}(?![A-Za-z])", mark, eq)
    # 도(°): ^{CIRC}로 쓰면 작은 동그라미가 숫자에서 떨어져 찍힌다. 글자 °를 그대로 쓰면 붙어서 찍힌다.
    eq = re.sub(r"\s*\^\{CIRC\}", "°", eq)
    # TIMES·DIV·LEQ 같은 낱말 기호 바로 뒤의 함수 이름은 한글이 변수로 알고 기울여 그린다(× tan A의 tan).
    # 함수 이름을 늘 곧은 글자로 못 박아 둔다. lim은 그러면 아래 첨자가 옆으로 가므로 건드리지 않는다.
    eq = re.sub(rf"(?<![A-Za-z\\])({'|'.join(UPRIGHT_FUNCTIONS)})(?![A-Za-z])", r"rm \1 it", eq)
    return re.sub(r"(?<= it)(?=[_^])", " ", eq)             # it_{2}처럼 붙으면 it가 명령으로 읽히지 않는다


def _math(latex: str, problem: Problem) -> list[Run]:
    """수식 하나를 넣을 조각으로 바꾼다. 바꾸지 못하면 문제에 오류를 적고 글자 그대로 둔다."""
    if latex.lstrip().startswith(RAW_EQUATION):
        # 한글 문서에서 꺼낸 수식은 이미 한글 수식 문법이다. 손대지 않고 그대로 넣는다
        script = latex.lstrip()[len(RAW_EQUATION):].strip()
        return [Run(latex=latex.strip(), eq=script)] if script else []
    latex, grouped = _group_scripts(latex)
    if grouped:
        problem.warnings.append(f"여러 자리 지수·첨자를 하나로 묶어 넣었습니다(맞는지 확인): ${latex.strip()}$")
    eq, err = to_hwp_equation(latex)
    if eq:
        return [Run(latex=latex, eq=eq)]
    problem.errors.append(f"수식을 바꾸지 못했습니다: ${latex.strip()}$ ({err})")
    return [Run(text=latex)]


def _text_blanks(line: str) -> str:
    """수식 밖의 글에 적힌 '□(가)'를 수식 `$\\boxed{(가)}$`로 바꾼다(상자 안에 (가)가 든 모양으로 그리려는 것)."""
    out, pos = [], 0
    for m in INLINE_RE.finditer(line):
        out += [TEXT_BLANK_RE.sub(lambda t: "$\\boxed{" + t.group(1) + "}$", line[pos:m.start()]), m.group(0)]
        pos = m.end()
    return "".join(out) + TEXT_BLANK_RE.sub(lambda t: "$\\boxed{" + t.group(1) + "}$", line[pos:])


def _runs(line: str, problem: Problem) -> list[Run]:
    line = _text_blanks(line)
    out: list[Run] = []
    pos = 0
    for m in INLINE_RE.finditer(line):
        if m.start() > pos:
            out.append(Run(text=line[pos:m.start()]))
        out += _math(m.group(1) or m.group(2), problem)
        pos = m.end()
    if pos < len(line):
        out.append(Run(text=line[pos:]))
    if line.count("$") % 2:
        problem.errors.append(f"$ 기호의 짝이 맞지 않습니다: {line.strip()[:40]}")
    return [r for r in out if r.eq or r.text]


def _choices(lines: list[str], problem: Problem) -> Choices:
    text = " ".join(l.strip() for l in lines)
    parts = re.split(f"([{CIRCLED}])", text)
    marks = parts[1::2]
    options = [_runs(body.strip(), problem) for body in parts[2::2]]
    expect = list(CIRCLED[:len(marks)])
    if marks != expect:
        problem.errors.append(f"선택지 번호가 차례대로가 아닙니다: {''.join(marks)}")
    if len(marks) not in (4, 5):
        problem.warnings.append(f"선택지가 {len(marks)}개입니다")
    if any(not o for o in options):
        problem.errors.append("내용이 빈 선택지가 있습니다")
    return Choices(options)


def parse(markdown: str, figures: set[str] | None = None) -> Problem:
    """전사 Markdown을 블록으로 바꾸고, 넣기 전에 걸러야 할 문제를 함께 적는다.

    figures는 이 문제에 쓸 수 있는 그림 이름들이다(주면 없는 그림을 가리키는지, 빠뜨린 그림이 없는지 본다).
    """
    problem = Problem(blocks=[])
    if re.search(r"[\x00-\x08\x0b-\x1f]", markdown):
        problem.errors.append("글에 깨진 문자가 들어 있습니다")
    stack: list[list] = [problem.blocks]
    lines = [l.rstrip() for l in markdown.replace("\r\n", "\n").split("\n")]
    used: list[str] = []
    cols, cols_width = 0, 0                   # 바로 다음 선택지를 한 줄에 몇 개씩 놓을지(<!-- cols:3 w:25626 -->)
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or re.fullmatch(r"<!--.*-->", line):
            hint = COLS_RE.fullmatch(line)
            if hint:
                cols, cols_width = int(hint.group(1)), int(hint.group(2) or 0)
            continue                          # 빈 줄과 메모(<!-- 낙서 있음 -->)는 내용이 아니다
        if line.startswith(":::"):
            head = line[3:].strip()
            if head.lower().startswith("box"):
                box = Boxed(label=head[3:].strip())
                stack[-1].append(box)
                stack.append(box.blocks)
            elif not head and len(stack) > 1:
                stack.pop()
            else:
                problem.errors.append(f"알 수 없는 묶음 표시입니다: {line}")
            continue
        fig = FIGURE_RE.fullmatch(line)
        if fig:
            used.append(fig.group(1))
            stack[-1].append(Figure(fig.group(1)))
            continue
        if line[0] in CIRCLED and line[0] == "①":
            group = [line]
            while i < len(lines) and lines[i].strip()[:1] in tuple(CIRCLED):
                group.append(lines[i].strip())
                i += 1
            # 선택지마다 딸린 줄이 있어(① 식 / ➡ 몫 …) 번호가 줄을 건너 이어지면, 표로 묶지 않고 적힌 대로 줄마다 넣는다
            marks = len(re.findall(f"[{CIRCLED}]", " ".join(group)))
            if marks < 4 and any(l.strip()[:1] == CIRCLED[marks] for l in lines[i:]):
                for one in group:
                    stack[-1].append(Para(_runs(one, problem)))
                continue
            choices = _choices(group, problem)
            choices.cols, choices.cols_width, cols, cols_width = cols, cols_width, 0, 0
            stack[-1].append(choices)
            continue
        display = re.fullmatch(r"\$\$(.+)\$\$", line, re.S)
        if display:
            runs = _math(display.group(1), problem)
            if runs:
                stack[-1].append(Para(runs, center=True))
            continue
        if FIGURE_RE.search(line):
            problem.errors.append("그림 자리 표시는 한 줄에 그것만 써야 합니다")
        runs = _runs(line, problem)
        if runs:
            stack[-1].append(Para(runs))
    if len(stack) > 1:
        problem.errors.append("::: box 묶음을 닫는 ::: 가 없습니다")
    if not problem.blocks:
        problem.errors.append("내용이 비어 있습니다")
    if figures is not None:
        for name in used:
            if name not in figures:
                problem.errors.append(f"없는 그림을 가리킵니다: figure:{name}")
        for name in sorted(figures - set(used)):
            problem.warnings.append(f"그림 {name}을(를) 쓰지 않았습니다")
        for name in {n for n in used if used.count(n) > 1}:
            problem.errors.append(f"그림 {name}을(를) 두 번 넣었습니다")
    return problem


def plain_text(blocks: list) -> str:
    """블록에서 글자만 뽑는다(수식은 원래 LaTeX로). 검사와 미리보기에 쓴다."""
    out = []
    for b in blocks:
        if isinstance(b, Para):
            out.append("".join(r.text or f"${r.latex}$" for r in b.runs))
        elif isinstance(b, Choices):
            out.append("  ".join(CIRCLED[k] + " " + "".join(r.text or f"${r.latex}$" for r in o)
                                 for k, o in enumerate(b.options)))
        elif isinstance(b, Boxed):
            out.append(f"[{b.label}]" if b.label else "[상자]")
            out.append(plain_text(b.blocks))
        elif isinstance(b, Figure):
            out.append(f"(그림 {b.name})")
    return "\n".join(out)
