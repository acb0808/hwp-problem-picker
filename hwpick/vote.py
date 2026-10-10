"""같은 문제를 여러 번 따로 읽은 결과를 하나로 합친다: 서로 다른 자리마다 다수결.

가벼운 모델은 한 번 읽을 때 100문제에 예닐곱 문제꼴로 틀린다(지수를 잘못 봄, 조사를 바꿈, 문장을 고쳐 씀).
그런데 틀리는 자리가 읽을 때마다 다르다. 그래서 따로 세 번 읽혀 자리마다 둘 이상이 같은 쪽을 고르면
무거운 모델 한 번과 비슷하게 맞는다(재 본 것: 한 번 93.5% → 세 번 98.5%). 셋이 다 다른 자리는 고르지 않고
사람이 보도록 알린다.

견줄 때는 표기 차이를 지운 꼴(norm)로 견주고, 결과로는 실제로 읽은 글 가운데 하나를 그대로 쓴다.
표기까지 섞어 새 글을 짓지 않으므로 `$` 짝이나 LaTeX가 깨지지 않는다.

그림 표시(`![](figure:A)`, `<!-- 그림 빠짐 -->`)는 다수결에 넣지 않는다. 재 보니 읽기마다 제각각이었다(테두리가 없는
그림을 한 번은 "빠짐"으로, 한 번은 없는 이름 `figure:A`로, 한 번은 아무 표시 없이). 그림은 말없이 빠지는 것이
가장 나쁘므로 한 번이라도 표시한 것은 살린다(marks).
"""
from __future__ import annotations

import difflib
import re

FIGURE_RE = re.compile(r"!\[\]\(figure:\s*\d*([A-Za-z])\s*\)")
MISSING_RE = re.compile(r"<!--\s*그림 빠짐\s*-->")
BOX_OPEN_RE = re.compile(r"^:::\s*box[ \t]*(.*)$", re.M)
BOX_CLOSE_RE = re.compile(r"^:::\s*$", re.M)
SWAPS = (
    ("\\dfrac", "\\frac"), ("\\tfrac", "\\frac"), ("\\left", ""), ("\\right", ""), ("\\displaystyle", ""),
    ("\\cdots", "…"), ("\\ldots", "…"), ("\\dots", "…"), ("...", "…"), ("\\cdot", "·"), ("\\times", "×"), ("\\div", "÷"),
    ("\\Rightarrow", "⇒"), ("\\rightarrow", "⇒"), ("\\to", "⇒"), ("➡", "⇒"), ("→", "⇒"), ("\\leqq", "≤"), ("\\geqq", "≥"),
    ("\\leq", "≤"), ("\\geq", "≥"), ("\\neq", "≠"), ("\\le", "≤"), ("\\ge", "≥"), ("\\ne", "≠"), ("\\bar{", "\\overline{"),
    ("\\lt", "<"), ("\\gt", ">"), ("\\%", "%"), ("\\circ", "°"), ("^°", "°"), ("^{°}", "°"),
)


def marks(text: str, known: set[str] | None = None) -> tuple[set[str], bool]:
    """글에 든 그림 표시: (넣겠다고 한 그림 이름들, 그림이 빠졌다는 표시가 있는가).
    known(잘라 둔 그림의 이름들)을 주면, 거기 없는 이름으로 그림을 넣겠다고 한 것은 "빠짐"으로 센다."""
    names = {m.group(1).upper() for m in FIGURE_RE.finditer(text)}
    missing = bool(MISSING_RE.search(text))
    if known is not None:
        missing = missing or bool(names - known)
        names &= known
    return names, missing


def with_marks(text: str, names: set[str], missing: bool, known: set[str] | None = None) -> str:
    """글의 그림 표시를 정한 대로 맞춘다: names의 그림은 넣고, 넣을 그림이 없는데 missing이면 "빠짐" 표시를 둔다.
    글에 없던 표시는 선택지 줄 바로 위(없으면 맨 끝)에 넣는다."""
    lines, have = [], set()
    for line in text.replace("\r\n", "\n").split("\n"):
        m = FIGURE_RE.fullmatch(line.strip())
        if m:
            name = m.group(1).upper()
            if name in names and name not in have:
                have.add(name)
                lines.append(f"![](figure:{name})")
            continue                                          # 모르는 이름·겹친 그림 줄은 뺀다
        if MISSING_RE.fullmatch(line.strip()):
            continue
        lines.append(MISSING_RE.sub("", FIGURE_RE.sub("", line)) if (FIGURE_RE.search(line) or MISSING_RE.search(line)) else line)
    extra = [f"![](figure:{n})" for n in sorted(names - have)]
    if missing and not names:
        extra.append("<!-- 그림 빠짐 -->")
    if extra:
        at = next((k for k, l in enumerate(lines) if l.lstrip().startswith("①")), len(lines))
        while at > 0 and not lines[at - 1].strip():
            at -= 1
        lines[at:at] = extra
    return "\n".join(lines)


def norm(text: str) -> str:
    """견주기용 꼴: 표기만 다른 것(공백, `$`의 자리, `\\dfrac`/`\\frac`, `\\mathrm{}` …)을 지운다. 내용이 다르면 달라진다.
    그림 표시는 뺀다(marks가 따로 본다)."""
    t = text.replace("\r\n", "\n")
    t = FIGURE_RE.sub("", t)
    t = MISSING_RE.sub("", t)
    t = BOX_OPEN_RE.sub(lambda m: f"⟦상자{m.group(1).strip()}⟧", t)
    t = BOX_CLOSE_RE.sub("⟦/상자⟧", t)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)                       # 낙서 표시 같은 메모
    t = re.sub(r"[㈀-㈛]", lambda m: "(" + "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ가나다라마바사아자차카타파하"[ord(m.group(0)) - 0x3200] + ")", t)   # ㈎ = (가)
    t = re.sub(r"```\w*|`", "", t)
    t = re.sub(r"\\(?:mathrm|text|textrm|rm|mathit|operatorname)\s*\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\[,;:!]|\\quad|\\qquad|\\ ", "", t)
    for a, b in SWAPS:
        t = t.replace(a, b)
    t = re.sub(r"\{\}([_^])", r"\1", t)
    t = re.sub(r"([_^])\{([^{}\\])\}", r"\1\2", t)
    return re.sub(r"[\s$]", "", t)


def _hunks(base: str, other: str) -> dict[tuple[int, int], str]:
    sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return {(i1, i2): other[j1:j2] for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"}


def hangul(text: str) -> str:
    return "".join(re.findall(r"[가-힣]", text))


def _settle(base: str, others: list[str], hint: str = "") -> tuple[str, list[dict]]:
    """base에서 출발해, 다른 읽기들의 과반이 같은 자리를 같은 글로 바꾸자고 하면 따른다.
    (합친 글, 정하지 못한 자리들 [{"at", "variants"}])

    hint는 로컬 글자 인식이 읽은 한글이다. 읽기들이 한글에서 갈린 자리(조사 `를`/`을`, `오른쪽`/`다음` …)는
    앞뒤 한글까지 붙인 대목이 hint에 그대로 있는 쪽을 따른다. 가벼운 모델은 조사를 제 말버릇대로 바꾸는 일이 있어
    여러 번 읽혀도 같은 쪽으로 틀리는데, 글자 인식은 한글 문장을 본 대로 읽는다(재 보니 읽기가 틀린 19곳 가운데
    14곳에서 맞는 쪽 대목만 있었고, 틀린 쪽 대목이 있던 적은 없다).
    """
    total = len(others) + 1
    proposals = [_hunks(base, o) for o in others]
    spans = sorted({s for h in proposals for s in h})
    out, pos, open_spots = [], 0, []

    def around(i1: int, i2: int, variants: list[str]) -> list[str]:
        lo, hi = max(0, i1 - 6), min(len(base), i2 + 6)
        return list(dict.fromkeys(base[lo:i1] + v + base[i2:hi] for v in variants))
    for (i1, i2) in spans:
        if i1 < pos:
            continue                                         # 앞의 자리와 겹치면 건너뛴다
        votes: dict[str, int] = {}
        touched = 0
        for h in proposals:
            hit = [s for s in h if s[0] < max(i2, i1 + 1) and max(s[1], s[0] + 1) > i1]
            if not hit:
                continue
            touched += 1
            if (i1, i2) in h:
                votes[h[(i1, i2)]] = votes.get(h[(i1, i2)], 0) + 1
        keep = total - touched                               # 이 자리를 base대로 읽은 수(base 포함)
        best = max(votes.items(), key=lambda kv: kv[1], default=("", 0))
        if hint and votes:
            left, right = hangul(base[:i1])[-2:], hangul(base[i2:])[:3]
            keys = {v: left + hangul(v) + right for v in [base[i1:i2], *votes]}
            seen = {k for k in keys.values() if len(k) >= 4 and k in hint}
            if len(set(keys.values())) > 1 and len(seen) == 1:
                # 한글이 갈린 자리이고 글자 인식에는 한쪽 대목만 있다: 그쪽으로 정한다
                if keys[base[i1:i2]] in seen:
                    continue
                best = max(((v, c) for v, c in votes.items() if keys[v] in seen), key=lambda kv: kv[1])
                out += [base[pos:i1], best[0]]
                pos = i2
                continue
        counts = sorted([keep, *votes.values()], reverse=True)
        if len(counts) > 1 and counts[0] * 2 > total and counts[1] >= 2:
            # 다수 쪽으로 정하지만, 두 읽기 이상이 똑같이 다른 글을 냈다: 우연히 겹친 실수가 아닐 수 있으니 사람이 보게 적어 둔다
            open_spots.append({"at": i1, "close": True, "variants": around(i1, i2, [base[i1:i2], *votes])})
        if best[1] * 2 > total:
            out += [base[pos:i1], best[0]]
            pos = i2
        elif keep * 2 > total:
            continue                                         # base가 과반
        else:
            open_spots.append({"at": i1, "variants": around(i1, i2, [base[i1:i2], *votes])})
    out.append(base[pos:])
    return "".join(out), open_spots


def same_problem(reads: list[str]) -> list[int]:
    """읽기들 가운데 서로 같은 문제를 옮긴 것들의 자리(가장 큰 무리). 나머지는 다른 문제의 글이다.

    가벼운 모델은 그림의 `[숫자]` 이름표를 무시하고 문제를 순서대로 세어 번호를 매기는 일이 있다(문제 하나를 건너뛰면
    그 뒤의 글이 모두 한 칸씩 밀린다). 그런 읽기는 그 번호의 다른 읽기들과 글이 통째로 다르다. 같은 방식으로 본 두 읽기가
    똑같이 밀리기도 해서, 둘이 서로 같다고 믿을 수는 없다. 그래서 가장 큰 무리만 남긴다(무리의 크기가 같으면 가려내지 않는다).
    """
    forms = [norm(r) for r in reads]
    n = len(forms)
    if n < 3:
        return list(range(n))
    close = [[i == j or difflib.SequenceMatcher(None, forms[i], forms[j], autojunk=False).ratio() >= 0.7 for j in range(n)] for i in range(n)]
    groups = sorted(({j for j in range(n) if close[i][j]} for i in range(n)), key=len, reverse=True)
    best = groups[0]
    rivals = [g for g in groups if not (g & best)]
    if len(best) < 2 or any(len(g) >= len(best) for g in rivals):
        return list(range(n))
    return sorted(best)


def combine(reads: list[str], known: set[str] | None = None, hint: str = "") -> dict:
    """따로 읽은 글들(앞에 둔 것일수록 믿는 읽기)을 하나로. known은 잘라 둔 그림의 이름들, hint는 글자 인식이 읽은 글.

    {"text": 고른 글, "used": 그 글이 몇 번째 읽기인지(0부터), "status", "open": 정하지 못한 자리들}
    status — same: 모두 같음 / majority: 다수결로 정함 / close: 다수결로 정했지만 둘 이상이 같은 다른 글을 낸 자리가 있음 /
    split: 정하지 못한 자리가 있음 / single: 읽기가 하나뿐
    """
    reads = [r for r in reads if r and r.strip()]
    if not reads:
        return {"text": "", "used": -1, "status": "none", "open": []}
    out = _combine(reads, hangul(hint))
    seen = [marks(r, known) for r in reads]
    # 잘라 둔 그림은 읽기의 반 이상이 넣겠다고 할 때 넣는다(글만 든 상자를 그림 후보로 잘못 잡은 것을 거르려고).
    # 그림이 빠졌다는 표시는 한 번이라도 있으면 살린다(말없이 빠지는 것이 가장 나쁘다)
    names = {n for n in set().union(*(s[0] for s in seen)) if sum(n in s[0] for s in seen) * 2 >= len(seen)}
    missing = any(s[1] for s in seen)
    out["text"] = with_marks(out["text"], names, missing, known)
    if "fallback" in out:
        out["fallback"] = with_marks(out["fallback"], names, missing, known)
    out["figures"] = {"names": sorted(names), "missing": missing and not names,
                      "agreed": all(s == seen[0] for s in seen)}
    return out


def _combine(reads: list[str], hint: str = "") -> dict:
    forms = [norm(r) for r in reads]
    if len(reads) == 1:
        return {"text": reads[0], "used": 0, "status": "single", "open": []}
    if all(f == forms[0] for f in forms):
        return {"text": reads[0], "used": 0, "status": "same", "open": []}
    if len(reads) == 2 and not hint:
        pass                                                 # 둘이 다르면 아래에서 '정하지 못함'이 된다
    # 뼈대는 나머지와 가장 가까운 읽기
    def far(k: int) -> float:
        return sum(1 - difflib.SequenceMatcher(None, forms[k], forms[j], autojunk=False).ratio() for j in range(len(forms)) if j != k)
    base = min(range(len(forms)), key=lambda k: (round(far(k), 6), k))
    agreed, open_spots = _settle(forms[base], [f for j, f in enumerate(forms) if j != base], hint)
    used = next((k for k, f in enumerate(forms) if f == agreed), None)
    text = reads[used] if used is not None else None
    if used is None:
        # 읽기마다 저마다 다른 곳을 틀려, 합의한 꼴과 똑같은 읽기가 없다. 가장 가까운 읽기의 글에서 틀린 자리만
        # 다른 읽기들의 글로 바꿔 본다. 그렇게 만든 글이 합의한 꼴과 같고 `$` 짝이 맞을 때만 쓴다
        used = max(range(len(forms)), key=lambda k: difflib.SequenceMatcher(None, forms[k], agreed, autojunk=False).ratio())
        tidy = [re.sub(r"[ \t]+", " ", r.replace("\r\n", "\n")).strip() for r in reads]
        patched, _ = _settle(tidy[used], [t for j, t in enumerate(tidy) if j != used], hint)
        if norm(patched) == agreed and patched.replace("$$", "").count("$") % 2 == 0:
            return {"text": patched, "used": -1, "fallback": reads[used], "status": _status(open_spots),
                    "open": open_spots, "agreed": agreed}
        text = reads[used]
        for (i1, i2), v in sorted(_hunks(agreed, forms[used]).items()):
            lo, hi = max(0, i1 - 6), min(len(agreed), i2 + 6)
            open_spots.append({"at": i1, "variants": [agreed[lo:hi], agreed[lo:i1] + v + agreed[i2:hi]], "kept": 1})
    return {"text": text, "used": used, "status": _status(open_spots), "open": open_spots, "agreed": agreed}


# 받침에 따라 갈리는 조사의 짝. 가벼운 모델은 수식 뒤의 조사를 제가 수식을 읽는 소리에 맞춰 바꿔 적는다(`…ₙPᵣ가` → `이`).
# 다섯 번 읽혀도 다섯 번 다 그렇게 적는 일이 있어 다수결로는 못 잡는다. 글자 인식은 찍힌 글자를 그대로 읽는다.
PARTICLES = (("이", "가"), ("을", "를"), ("은", "는"), ("과", "와"), ("으로", "로"), ("이라", "라"))
_SWAP = {a: b for a, b in PARTICLES} | {b: a for a, b in PARTICLES}


def fix_particles(text: str, hint: str) -> tuple[str, list[str]]:
    """글의 조사가 글자 인식(hint)과 같은 짝 안에서만 다르고 바로 뒤 두 글자가 같으면, 글자 인식 쪽으로 바꾼다.
    (바꾼 글, 바꾼 내역). 수식 안의 글자와 `(가)` 같은 표시는 건드리지 않는다.
    재 본 것: 다섯 실행 380문제에서 3곳을 바꿨고 셋 다 맞게 바뀌었다(틀리게 바꾼 것 0)."""
    masked = re.sub(r"\$\$.+?\$\$|\$[^$\n]+\$|\([가-힣]\)", lambda m: " " * len(m.group(0)), text, flags=re.S)
    pos = [i for i, ch in enumerate(masked) if "가" <= ch <= "힣"]
    mine, theirs = "".join(text[i] for i in pos), hangul(hint)
    edits = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, mine, theirs, autojunk=False).get_opcodes():
        a, b = mine[i1:i2], theirs[j1:j2]
        if op == "equal" or _SWAP.get(a) != b or len(mine[i2:i2 + 2]) < 2 or mine[i2:i2 + 2] != theirs[j2:j2 + 2]:
            continue
        if pos[i2 - 1] - pos[i1] != len(a) - 1:
            continue                                         # 글에서 붙어 있는 글자들이 아니다
        edits.append((pos[i1], pos[i2 - 1] + 1, a, b, mine[i2:i2 + 3]))
    for start, end, a, b, _ in reversed(edits):
        text = text[:start] + b + text[end:]
    return text, [f"{a}→{b} ({after} 앞)" for _, _, a, b, after in edits]


def show(piece: str) -> str:
    """견주기용 꼴의 한 토막을 사람이 읽을 글로(상자 표시를 풀어 쓴다)."""
    return re.sub(r"⟦상자(.*?)⟧", lambda m: f"[상자 {m.group(1)}]" if m.group(1) else "[상자]", piece).replace("⟦/상자⟧", "[상자 끝]")


def _status(open_spots: list[dict]) -> str:
    if any(not o.get("close") for o in open_spots):
        return "split"
    return "close" if open_spots else "majority"
