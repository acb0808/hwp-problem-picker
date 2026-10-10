"""한글 문서에서 꺼낸 문제들이 쪽 그림의 어디에 놓였는지 찾는다.

문제의 내용은 HWPX에서 꺼냈지만 쪽 위의 자리는 거기에 없다. 한글이 저장한 PDF의 글자층에서 줄 머리에 놓인
문제 번호를 찾아, 번호와 "번호 뒤에 이어지는 한글 몇 자"로 문제와 짝을 짓는다. 자동 문단 번호를 쓴 문서는
번호가 HWPX의 글에 없으므로, 이때는 차례와 뒤따르는 글로 짝을 짓고 번호를 PDF에서 읽어 온다.

짝을 지은 문제의 박스는 그 번호에서 같은 단의 다음 번호 앞(없으면 쪽 아래)까지다.
"""
from __future__ import annotations

import difflib
import re

import cv2
import numpy as np

from .source import Parsed

# PDF의 줄 머리 글(낱말 사이 빈칸은 붙여서 본다)에서 번호를 읽는 꼴. 문서에서 고른 번호 방식과 같은 이름을 쓴다
HEADS = {"번": re.compile(r"(\d{1,3})번[.．:]?"), ".": re.compile(r"(\d{1,3})[.．](?!\d)"),
         "문제": re.compile(r"(?:문제|문항)(\d{1,3})[.．:)]?"), "[]": re.compile(r"\[(\d{1,3})\]"),
         ")": re.compile(r"(\d{1,3})\)")}
AUTO = re.compile(r"(\d{1,3})[.．)](?!\d)")                  # 자동 번호가 찍힌 모습
EXTRA = re.compile(r"[\[<(【]?(서술형|서답형|단답형|주관식|논술형)(?:문제|문항)?(\d{1,2})번?[\]>)】]?[.．:)]?")
MIN_MATCHED = 0.7          # 문제 가운데 이만큼도 자리를 못 찾으면 그 문서는 박스를 내지 않는다(틀린 박스가 더 나쁘다)
MIN_AGREE = 0.7            # 꺼낸 글의 한글이 박스 안에 이만큼도 없는 문제가 절반을 넘으면 자리를 잘못 맞춘 것이다
BOTTOM = 0.955             # 쪽 아래 여백(쪽 번호·꼬리말)이 시작하는 곳


def _hangul(s: str) -> str:
    return "".join(re.findall(r"[가-힣]", s))


def _inside(mine: str, theirs: str) -> float:
    """mine의 한글 가운데 theirs에도 두 글자 이상 이어서 나오는 것의 비율."""
    blocks = difflib.SequenceMatcher(None, mine, theirs, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks if b.size >= 2) / max(1, len(mine))


def _alike(a: str, b: str) -> float:
    a, b = a[:10], b[:10]
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0


def _candidates(idx, pattern) -> list[dict]:
    """쪽마다 줄 머리에 놓인 번호들을 읽는 차례대로(쪽 → 왼쪽 단 먼저 → 위에서 아래)."""
    from ..index import PDF_LOCK
    out = []
    for p in range(idx.page_count):
        with PDF_LOCK:
            words = idx.doc[p].get_text("words")
        W = idx.sizes()[p][0]
        lines: dict[tuple[int, int], list] = {}
        for w in words:
            lines.setdefault((w[5], w[6]), []).append(w)
        for line in lines.values():
            line.sort(key=lambda v: v[0])
            w = line[0]
            # 한 줄이 여러 조각으로 나뉘어 읽히는 일이 있어, 같은 높이에서 바로 오른쪽에 이어지는 낱말들을 붙여 본다
            row = sorted((v for v in words if abs(v[1] - w[1]) <= 3 and w[0] <= v[0] <= w[0] + 130), key=lambda v: v[0])
            joined = "".join(v[4] for v in row[:5])
            m = EXTRA.match(joined)
            word = m.group(1) if m else ""
            number = int(m.group(2)) if m else None
            if not m:
                # 번호가 낱말 하나로 떨어져 있으면 그것만 본다(붙여 보면 '26.' 뒤의 수식 숫자와 이어져 소수처럼 보인다)
                m = pattern.fullmatch(re.sub(r"[-].*", "", w[4])) or pattern.match(joined)
                number = int(m.group(1)) if m else None
            if not m:
                continue
            top, bottom = w[1], w[3]
            line_top = min(v[1] for v in line if v[0] <= w[0] + 300)     # 그 줄에서 가장 높이 올라온 것(키 큰 수식)
            if re.search(r"[-]", w[4]):
                # 아랫줄의 키 큰 수식 조각이 번호 낱말에 붙어 읽힌 것. 번호 글자만 다시 찾아 잰다
                with PDF_LOCK:
                    hits = [r for r in idx.doc[p].search_for(re.sub(r"[-].*", "", w[4])) if abs(r.x0 - w[0]) < 3]
                if hits:
                    top, bottom = hits[0].y0, hits[0].y1
                    line_top = top
            near = [v[4] for v in words if w[0] - 2 <= v[0] <= w[0] + 230 and min(top, line_top) - 3 <= v[1] <= top + 52]
            follow = _hangul("".join(near))
            # 번호 뒤의 '[논술형 1]' 같은 표시는 문제의 글에서 떼어 냈으므로 여기서도 뗀다
            follow = re.sub(r"^(서술형|서답형|단답형|주관식|논술형)(문제|문항)?", "", follow)
            out.append({"page": p, "x": w[0], "top": top, "bottom": bottom, "number": number, "word": word,
                        "follow": follow[:14].lstrip("번"), "side": int(w[0] > W / 2), "line_top": min(top, line_top)})
    out.sort(key=lambda c: (c["page"], c["side"], c["top"]))
    return out


def _columns(cands: list[dict], need: int) -> list[float]:
    """번호가 줄 맞춰 놓이는 왼쪽 자리들(단의 왼쪽 끝). 여기에서 벗어난 번호는 문제 번호가 아니다."""
    xs = sorted(c["x"] for c in cands)
    groups: list[list[float]] = []
    for x in xs:
        if groups and x - groups[-1][-1] <= 6:
            groups[-1].append(x)
        else:
            groups.append([x])
    return [sum(g) / len(g) for g in groups if len(g) >= need]


def locate(idx, parsed: Parsed) -> tuple[dict[int, list[dict]], dict[str, object], list[str]]:
    """(쪽별 박스, 박스 ID → 문제, 알릴 점). 문제와 자리를 충분히 맞추지 못하면 박스를 내지 않는다."""
    slots = parsed.slots
    notes = list(parsed.notes)
    auto = parsed.family.startswith("auto:")
    cands = _candidates(idx, AUTO if auto else HEADS[parsed.family])
    # 문제가 몇 개 안 되는 문서(이 도구로 만든 3문제짜리 등)는 오른쪽 단에 번호가 하나뿐일 수 있다
    lefts = _columns(cands, 1 if len(slots) < 8 else max(2, len(slots) // 10))
    cands = [c for c in cands if any(abs(c["x"] - x) <= 6 for x in lefts)]
    placed: list[tuple[object, dict]] = []
    if auto:
        # 번호를 모르므로 차례로 맞춘다: 문서의 문제 차례와 쪽에 찍힌 번호의 차례를, 뒤따르는 글이 같은 것끼리 짝지어
        # 가장 많이 맞는 짝짓기를 고른다(글이 수식으로 시작해 견줄 한글이 없는 문제는 차례만으로 끼워 맞춘다)
        mine = [s for s in slots if not s.word]
        theirs = [c for c in cands if not c["word"]]

        def fit(s, c) -> float:
            if len(s.head) < 4 or len(c["follow"]) < 4:
                return 0.3
            score = _alike(s.head, c["follow"])
            return score if score >= 0.5 else -1.0

        n, m = len(mine), len(theirs)
        best = [[0.0] * (m + 1) for _ in range(n + 1)]
        for i in range(n - 1, -1, -1):
            for j in range(m - 1, -1, -1):
                both = fit(mine[i], theirs[j])
                best[i][j] = max(best[i + 1][j], best[i][j + 1], best[i + 1][j + 1] + both if both > 0 else 0.0)
        i = j = 0
        while i < n and j < m:
            both = fit(mine[i], theirs[j])
            if both > 0 and best[i][j] == best[i + 1][j + 1] + both:
                mine[i].number, mine[i].label = theirs[j]["number"], str(theirs[j]["number"])
                placed.append((mine[i], theirs[j]))
                i, j = i + 1, j + 1
            elif best[i][j] == best[i + 1][j]:
                i += 1
            else:
                j += 1
    pool: dict[tuple[str, int], list[dict]] = {}
    for c in cands:
        pool.setdefault((c["word"], c["number"]), []).append(c)
    wanted: dict[tuple[str, int], int] = {}
    for s in slots:
        wanted[(s.word, s.number)] = wanted.get((s.word, s.number), 0) + 1
    offered = {key: len(v) for key, v in pool.items()}
    shaky: set[str] = set()
    if True:
        for s in slots:
            if auto and not s.word:
                continue
            mine = pool.get((s.word, s.number), [])
            if not mine:
                continue
            scores = [_alike(s.head, c["follow"]) for c in mine]
            k = max(range(len(mine)), key=lambda i: scores[i])
            unsure = len(s.head) < 4 or len(mine[k]["follow"]) < 4
            if not (scores[k] >= 0.5 or unsure):
                continue
            if unsure and wanted[(s.word, s.number)] != offered.get((s.word, s.number), 0):
                # 글로 확인할 수 없는 문제(그림만 있는 칸)는 차례로 짝짓는데, 같은 번호의 문제 수와 쪽에 찍힌 번호 수가
                # 다르면 한 칸씩 밀려 다른 회차의 문제와 짝지어질 수 있다. 그 번호는 박스를 내지 않는다
                shaky.add(s.label)
                continue
            placed.append((s, mine.pop(0 if unsure else k)))
    if slots and len(placed) < MIN_MATCHED * len(slots):
        notes.append(f"문제 {len(slots)}개 가운데 {len(placed)}개만 쪽 그림에서 자리를 찾았습니다. 틀린 박스를 내지 않으려고 "
                     "이 문서는 박스를 치지 않았습니다(`look`·`add` 명령이나 선택 화면의 `직접 그리기`로 담을 수 있습니다)")
        return {}, {}, notes
    if shaky:
        notes.append("같은 번호가 여러 번 나오는데 개수가 맞지 않아 박스를 치지 않은 번호: " + ", ".join(sorted(shaky, key=lambda v: (len(v), v)))
                     + " (`look`·`add` 명령이나 선택 화면의 `직접 그리기`로 담을 수 있습니다)")
    if len(placed) < len(slots):
        got = {id(s) for s, _ in placed}
        lost = [s.label or "?" for s in slots if id(s) not in got]
        notes.append(f"문제 {len(lost)}개는 쪽 그림에서 자리를 찾지 못해 박스가 없습니다(번호: {', '.join(lost[:20])})")
    boxes: dict[int, list[dict]] = {}
    slot_of: dict[str, object] = {}
    used: set[str] = set()
    grays: dict[int, np.ndarray] = {}
    placed.sort(key=lambda sc: (sc[1]["page"], sc[1]["side"], sc[1]["top"]))
    for s, c in placed:
        p = c["page"]
        W, H = idx.sizes()[p]
        k = idx.scale(p)
        if p not in grays:
            grays.clear()                            # 쪽 그림은 한 장만 들고 있는다
            grays[p] = cv2.cvtColor(idx.page_image(p), cv2.COLOR_BGR2GRAY)
        gray = grays[p]
        right = min((x for x in lefts if x > c["x"] + 60), default=None)
        x0, x1 = c["x"] - 2, (right - 6) if right is not None else W - min(lefts) + 2
        below = min((o["line_top"] for o in cands if o["page"] == p and o["top"] > c["top"] + 3
                     and x0 - 8 <= o["x"] <= x1), default=H * BOTTOM)
        top = c["line_top"] - 1.5 if s.inline else c["bottom"] + 1
        for fl in s.floating:
            # 쪽을 기준으로 놓인 개체가 이 문제의 자리에 걸쳐 있으면 문제의 그림일 수 있다. 그대로는 못 옮기니 표시해 둔다
            in_x = fl["x"] is None or min(fl["x"][1], x1) - max(fl["x"][0], x0) > 0.3 * (fl["x"][1] - fl["x"][0])
            in_y = fl["y"] is None or min(fl["y"][1], below) - max(fl["y"][0], top) > 0.3 * (fl["y"][1] - fl["y"][0])
            if in_x and in_y and "쪽을 기준으로 놓인 개체가 이 문제의 자리에 있습니다" not in s.issues:
                s.issues.append("쪽을 기준으로 놓인 개체가 이 문제의 자리에 있습니다")
        a, b = max(0, int(top * k)), int((below - 2) * k)
        e, f = max(0, int(x0 * k)), min(gray.shape[1], int(x1 * k))
        dark = gray[a:b, e:f] < 200 if b > a and f > e else np.zeros((0, 0), bool)
        if dark.size and dark.shape[0] > 40 * k:
            # 단 사이의 세로줄이 범위 끝에 걸리면 박스가 아래로 끝까지 늘어난다. 범위 오른쪽 끝의 길게 이어진 세로줄은 지운다
            edge = max(0, dark.shape[1] - int(16 * k))
            long = dark[:, edge:].mean(axis=0) > 0.6
            dark[:, edge:][:, long] = False
        rows = np.flatnonzero(dark.any(axis=1)) if dark.size else np.empty(0, int)
        if not len(rows):
            continue                                 # 번호만 있고 내용이 빈 칸
        # 내용에서 멀리 떨어져 쪽 아래에 혼자 있는 작은 것(쪽 번호, 밑줄)은 문제의 것이 아니다
        gaps = np.flatnonzero(np.diff(rows) > 55 * k)
        if len(gaps) and rows[-1] - rows[gaps[-1] + 1] < 40 * k:
            rows = rows[:gaps[-1] + 1]
        cols = np.flatnonzero(dark[rows[0]:rows[-1] + 1].any(axis=0))
        pad = 3.0
        rect = [round(max(0.0, (e + cols[0]) / k - pad), 1), round(max(0.0, (a + rows[0]) / k - pad), 1),
                round(min(W, (e + cols[-1] + 1) / k + pad), 1), round(min(H, (a + rows[-1] + 1) / k + pad), 1)]
        bid = f"{idx.id[:6]}-{p + 1}-{s.label}"
        while bid in used:
            bid += "b"
        used.add(bid)
        slot_of[bid] = s
        boxes.setdefault(p, []).append({"id": bid, "label": s.label, "bbox": rect, "stem": None, "flags": []})
    # 마지막 확인: 문서에서 꺼낸 글이 정말 그 박스 안에 찍혀 있는가. 대부분 어긋나면 짝을 잘못 지은 문서다
    from ..index import PDF_LOCK
    scores, extra = [], []                       # 꺼낸 글이 박스 안에 있는 정도, 박스의 글이 꺼낸 글에 있는 정도
    cut_short = []
    for p, found in sorted(boxes.items()):
        with PDF_LOCK:
            words = idx.doc[p].get_text("words")
        for b in found:
            s = slot_of[b["id"]]
            x0, y0, x1, y1 = b["bbox"]
            theirs = _hangul("".join(w[4] for w in words if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= (w[1] + w[3]) / 2 <= y1))
            # 문제 끝에 딸려 온 짧은 제목 줄(다음 묶음의 이름 등): 박스 안에 찍혀 있지 않은데 나머지 글은 다 박스 안에 있으면,
            # 그 줄은 이 문제 자리가 아닌 곳(다음 단·쪽의 머리)에 찍힌 것이니 문제의 글에서 뗀다
            lines = s.text.split("\n")
            while len(lines) > 1:
                tail = lines[-1].strip()
                rest = _hangul(re.sub(r"\$[^$]*\$", "", "\n".join(lines[:-1])))
                short = "$" not in tail and len(tail) <= 14 and 2 <= len(_hangul(tail)) and not re.match(r"[①-⑳(:!<]", tail)
                if short and _hangul(tail) not in theirs and len(rest) >= 10 and _inside(rest, theirs) >= 0.9:
                    lines.pop()
                else:
                    break
            s.text = "\n".join(lines)
            mine = _hangul(re.sub(r"\$[^$]*\$", "", s.text))
            if len(mine) >= 10:
                scores.append(_inside(mine[:60], theirs))
            if len(theirs) >= 10:
                blocks = difflib.SequenceMatcher(None, theirs, mine, autojunk=False).get_matching_blocks()
                covered = sum(m.size for m in blocks if m.size >= 2)
                rest = theirs
                for m in sorted(blocks, key=lambda m: -m.a):
                    if m.size >= 2:
                        rest = rest[:m.a] + rest[m.a + m.size:]
                extra.append((b["id"], covered / len(theirs), rest))
            if s.issues and len(mine) >= 30 and _inside(mine, theirs) < 0.6:
                cut_short.append(f"{b['label']}번({p + 1}쪽)")
    if cut_short:
        notes.append("다음 단이나 쪽으로 이어지는데 문서에서 그대로 꺼낼 수도 없는 문제: " + ", ".join(cut_short)
                     + ". 이런 문제는 박스가 감싼 앞부분만 그림으로 읽힙니다. `look`·`add` 명령이나 선택 화면의 `직접 그리기`로 다시 담는 편이 낫습니다")
    if len(scores) >= 3 and (sorted(scores)[len(scores) // 2] < MIN_AGREE or sum(v < 0.5 for v in scores) > len(scores) / 4):
        notes.append("문서에서 꺼낸 글과 쪽 그림의 글이 서로 맞지 않습니다. 틀린 박스를 내지 않으려고 이 문서는 박스를 치지 않았습니다"
                     "(`look`·`add` 명령이나 선택 화면의 `직접 그리기`로 담을 수 있습니다)")
        return {}, {}, notes
    if len(extra) >= 3 and sorted(v for _, v, _ in extra)[len(extra) // 2] < MIN_AGREE:
        # 박스마다 문서에서 꺼낸 것보다 글이 훨씬 많다: 문제 칸 밖에 딸린 글이 있는 문서다.
        # 딸린 글이 정답·해설이면 문제만 꺼내는 것이 맞다(그림을 읽으면 정답까지 문제 글에 섞여 들어간다).
        # 그 밖의 글이면 꺼낸 것이 보이는 것의 일부뿐이라는 뜻이니 그림을 읽는 길로 보낸다
        answers = others = 0
        for bid, v, rest in extra:
            if v >= 0.6:
                continue
            if re.search(r"정답|해설|풀이", rest):
                answers += 1
            else:
                others += 1
                slot_of[bid].issues.append("박스 안에 문제 칸 밖의 글이 함께 있습니다")
        if answers:
            notes.append(f"문제 옆에 정답·해설이 딸린 문서입니다. {answers}문제는 정답·해설을 빼고 문제만 꺼냅니다")
        if others:
            notes.append(f"{others}문제는 문제 칸 밖에 딸린 글이 있어 문서에서 그대로 꺼내지 않고 그림을 읽어 옮깁니다")
    return boxes, slot_of, notes
