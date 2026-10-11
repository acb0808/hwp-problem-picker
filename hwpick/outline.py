"""교재의 뼈대: 어느 쪽부터 어느 쪽까지가 어느 단원의 어느 구획인가.

"1단원 문제들 중에 B단계만 모아 줘" 같은 말을 들어주려면 이것을 알아야 한다. 스캔한 교재에는 책갈피도
글자도 없어서 모델에게 그림을 보여 주고 알아낸다. 교재마다 한 번만 해서 저장해 둔다.

    ① 쪽들을 작게 줄여 여덟 쪽씩 한 장에 모은 그림으로, 쪽마다 종류와 "여기서 시작하는 구획"을 묻는다.
    ② 차례 쪽을 큰 그림으로 읽어 단원의 이름과 시작 쪽을 얻는다(줄인 그림에서는 단원 이름이 안 읽힌다).
    ③ 구획 이름은 종류마다 한 쪽씩 큰 그림으로 다시 읽어 바로잡는다(줄인 그림에서는 쪽마다 다르게 읽힌다).

여기서는 낱말의 뜻을 풀지 않는다. "B단계"가 어느 구획인지는 뼈대 목록을 읽은 에이전트가 정하고,
문제를 모으는 일은 이 PC에서 찾아 둔 박스로 한다(모델을 쓰지 않는다).
"""
from __future__ import annotations

import difflib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VERSION = 12
PER_SHEET = 8              # 그림 한 장에 모으는 쪽 수
COLUMNS = 4
THUMB_WIDTH = 450          # 쪽 하나의 폭(픽셀). 큰 제목과 구획 이름표가 읽히는 크기
WORKERS = 12
CAREFUL = "low"            # 차례와 구획 이름처럼 몇 번 안 묻지만 틀리면 안 되는 것은 추론을 한 단계 올려 읽힌다
SLOW = (30.0, 30.0)        # 여러 쪽을 모은 그림은 답이 10~20초 걸린다. 그보다 훨씬 늦을 때만 같은 요청을 더 보낸다
KINDS = ("표지", "목차", "단원표지", "개념", "문제", "해설", "기타")
NONE_WORDS = ("-", "–", "—", "없음", "")

PROMPT = """첨부한 그림은 수학 교재의 여러 쪽을 작게 줄여 차례대로 늘어놓은 것입니다. 쪽마다 왼쪽 위에 빨간 숫자로 쪽 번호가 적혀 있습니다.
교재의 뼈대를 알아내려는 것이니, 빨간 번호가 붙은 쪽마다 한 줄씩 아래 형식으로 적으세요. 문제의 내용은 읽지 않아도 됩니다.

    쪽번호 | 종류 | 단원 | 구획

- 종류: `표지`, `목차`, `단원표지`, `개념`, `문제`, `해설`, `기타` 가운데 하나. 문제가 실린 쪽은 `문제`(문제 옆에 짧은 풀이가 섞여 있어도 `문제`), 개념 설명이 주인 쪽은 `개념`, 책 뒤쪽에 정답·풀이만 모아 둔 쪽은 `해설`, 큰 단원이 시작됨을 알리는 쪽은 `단원표지`, 책 전체의 차례는 `목차`.
- 단원: 그 쪽에 단원 이름이 큰 제목이나 쪽 위·아래 머리글로 보이면 번호까지 보이는 대로 적습니다(예: `Ⅰ 다항식`, `01 다항식의 연산`). 안 보이면 `-`.
- 구획: 그 쪽에서 문제 묶음의 큰 구분(단계)이 새로 시작되면 그 이름을 보이는 대로 적습니다. 단원마다 되풀이되는 큰 제목을 말하며, 이름 앞의 큰 글자 표시(A, B, C, STEP 1 등)도 함께 적습니다(예: `A 기본 다잡기`, `B 유형 뽀개기`, `STEP 2`, `연습문제`, `단원 마무리`). 개념 번호가 붙은 소제목(`01-4 …`)이나 유형 하나하나의 제목(`유형 03 …`)은 구획이 아닙니다. 시작되는 구획이 없으면 `-`.

설명은 붙이지 말고 줄만 적으세요.
"""

TOC_PROMPT = """첨부한 그림은 수학 교재의 차례(목차) 쪽입니다. 쪽 번호가 적힌 단원마다 한 줄씩 아래 형식으로 적으세요.

    단원 경로 | 시작 쪽

- 단원 경로: 가장 큰 단원부터 그 단원까지를 ` > ` 로 이어 적습니다. 번호와 이름을 보이는 대로 적습니다(예: `Ⅰ 다항식 > 01 다항식의 연산`, `Ⅰ 도형의 방정식 > 1 평면좌표 > 01 두 점 사이의 거리`). 큰 단원 구분이 없으면 그 단원만 적습니다.
- 시작 쪽: 차례에 적힌 쪽 번호(숫자만).

쪽 번호가 적힌 가장 작은 단원까지 적고, 차례에 나온 순서대로 적습니다. 정답·부록 같은 것은 적지 않습니다. 차례가 아닌 쪽이면 `없음` 이라고만 적으세요. 설명은 붙이지 마세요.
"""

PAGE_NUMBER_PROMPT = """첨부한 그림은 교재의 한 쪽입니다. 이 쪽의 아래나 위 귀퉁이에 인쇄된 쪽 번호를 숫자만 적으세요. 쪽 번호가 보이지 않으면 `없음` 이라고만 적으세요.
"""

NAME_PROMPT = """첨부한 그림은 수학 교재의 한 쪽입니다. 이 쪽에서 새로 시작하는 구획(문제 묶음)의 제목을 위에서부터 차례로 한 줄에 하나씩 적으세요. 앞에 붙은 큰 글자 표시(A, B, C, STEP 1 등)까지 보이는 그대로 적습니다(예: `A 기본 다잡기`, `STEP 2 유형 익히기`, `연습문제`).
개념 번호가 붙은 소제목(`01-4 …`)이나 유형 하나하나의 제목(`유형 03 …`)은 적지 않습니다. 그런 제목이 없으면 `없음` 이라고만 적으세요. 설명은 붙이지 마세요.
"""

BOUNDARY_PROMPT = """첨부한 그림은 수학 교재의 한 쪽입니다. 이 쪽에서 `{section}` 구획이 시작됩니다.
그 구획의 제목 바로 다음에 나오는 첫 문제의 번호를, 쪽에 인쇄된 그대로 한 줄로만 적으세요(예: `0040`, `01`, `1-1`).
구획 제목이 이 쪽에 보이지 않으면 `없음` 이라고만 적으세요. 설명은 붙이지 마세요.
"""


def _all(ex: ThreadPoolExecutor, fn, items) -> list:
    """ex.map처럼 차례대로 결과를 주되, 하나가 실패해도 나머지를 끝까지 돌린 뒤에 그 실패를 올린다.

    ex.map은 예외를 만나면 아직 시작하지 않은 일을 취소한다. 물음을 서브에이전트에게 맡기는 길(asks)에서는
    "답이 아직 없다"가 예외로 오므로, 취소되면 답이 없는 물음을 한 번에 다 모으지 못한다.
    """
    futures = [ex.submit(fn, x) for x in items]
    out, error = [], None
    for future in futures:
        try:
            out.append(future.result())
        except Exception as e:
            error = error or e
    if error is not None:
        raise error
    return out


def _file(idx) -> Path:
    return idx.dir / f"outline{VERSION}.json"


def load(idx) -> dict | None:
    try:
        data = json.loads(_file(idx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("version") == VERSION and len(data.get("pages", [])) == idx.page_count else None


def _save(idx, data: dict) -> None:
    from .index import _write
    _write(_file(idx), json.dumps(data, ensure_ascii=False))


def _thumb(idx, page: int):
    import numpy as np
    import pymupdf

    from .index import PDF_LOCK
    with PDF_LOCK:
        pg = idx.doc[page]
        z = THUMB_WIDTH / pg.rect.width
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
        img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
    return img[:, :, ::-1].copy()                  # RGB → BGR


def sheet(idx, pages: list[int]) -> bytes:
    """쪽들(0부터)을 작게 줄여 한 장에 모은 JPEG. 쪽마다 왼쪽 위에 빨간 쪽 번호(1부터)를 적는다."""
    import cv2
    import numpy as np
    cells = []
    for p in pages:
        img = _thumb(idx, p)
        cv2.rectangle(img, (0, 0), (92, 40), (255, 255, 255), -1)
        cv2.putText(img, str(p + 1), (5, 32), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
        cells.append(cv2.copyMakeBorder(img, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=(0, 0, 0)))
    h, w = max(c.shape[0] for c in cells), max(c.shape[1] for c in cells)
    cells = [cv2.copyMakeBorder(c, 0, h - c.shape[0], 0, w - c.shape[1], cv2.BORDER_CONSTANT, value=(255, 255, 255))
             for c in cells]
    rows = []
    for i in range(0, len(cells), COLUMNS):
        row = cells[i:i + COLUMNS] + [np.full((h, w, 3), 255, np.uint8)] * (COLUMNS - len(cells[i:i + COLUMNS]))
        rows.append(np.hstack(row))
    ok, buf = cv2.imencode(".jpg", np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buf.tobytes()


def _cells(line: str) -> list[str]:
    return [s.strip().strip("`") for s in line.strip().strip("|").split("|")]


def parse(text: str, pages: list[int]) -> dict[int, list[str]]:
    """모델의 답에서 쪽(0부터) → [종류, 단원, 구획]. 답에 없는 쪽은 넣지 않는다."""
    want = {p + 1 for p in pages}
    out: dict[int, list[str]] = {}
    for line in text.splitlines():
        parts = _cells(line)
        if len(parts) < 4 or not parts[0].isdigit() or int(parts[0]) not in want:
            continue
        kind = parts[1] if parts[1] in KINDS else "기타"
        unit, section = ("" if s in NONE_WORDS else s for s in parts[2:4])
        out[int(parts[0]) - 1] = [kind, unit, section]
    return out


def _stage_key(name: str) -> str:
    """구획 이름에서 앞의 글자 표시와 띄어쓰기를 뺀 것(같은 구획인지 견줄 때 쓴다)."""
    return re.sub(r"^(?:[A-Za-z]|STEP\s*\d+|Step\s*\d+|[0-9]{1,2})(?=[\s가-힣])", "", name.strip()).replace(" ", "")


def _alike(a: str, b: str, floor: float = 0.7) -> bool:
    return bool(a and b) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= floor)


def _indexes(name: str) -> list[str]:
    """구획 이름에 든 차례 표시들: 'B 유형 뽀개기' → ['B'], 'STEP 2 | 내신발전' → ['2'], 'Level A' → ['A'].

    'Theme 1~3'처럼 범위를 적은 수는 차례 표시가 아니다(같은 구획이 단원마다 범위만 바뀌어 나온다).
    """
    s = re.sub(r"\d+\s*[~\-–]\s*\d+", " ", name)
    return [v.upper() for v in re.findall(r"(?<![A-Za-z0-9])([A-Za-z]|\d{1,2})(?![A-Za-z0-9])", s)]


def _text(name: str) -> str:
    """구획 이름에서 차례 표시와 띄어쓰기·기호를 뺀 글자."""
    s = re.sub(r"\d+\s*[~\-–]\s*\d+", " ", name)
    s = re.sub(r"(?<![A-Za-z0-9])([A-Za-z]|\d{1,2})(?![A-Za-z0-9])", " ", s)
    return re.sub(r"[\s|·.,:\-–—()\[\]/]", "", s)


def _same_stage(a: str, b: str, floor: float = 0.7) -> bool:
    """같은 구획의 이름인가. 차례 표시가 서로 다르면(A와 B, STEP 1과 STEP 2, Level A와 Level B) 나머지 글자가
    아무리 비슷해도 다른 구획이다. 줄인 그림에서 표시가 안 읽힌 이름은 나머지 글자로 견준다."""
    ia, ib = _indexes(a), _indexes(b)
    if ia and ib and ia != ib:
        return False
    ta, tb = _text(a), _text(b)
    if not ta or not tb:
        return bool(ia) and ia == ib or (not ta and not tb and a.strip() == b.strip())
    if ia and ia == ib and (ta in tb or tb in ta):
        return True                                     # 'STEP 2'와 'STEP 2 | 내신발전'
    return _alike(ta, tb, floor)


def _clusters(names: dict[int, str]) -> list[list[int]]:
    """구획 시작 쪽들을 같은 구획끼리 묶는다. 줄인 그림에서는 같은 이름도 쪽마다 조금씩 다르게 읽힌다.

    글자 하나까지 똑같이 두 번 이상 읽힌 이름은 실제로 있는 이름이다(잘못 읽은 것은 쪽마다 다르게 틀린다).
    그런 이름끼리는 비슷해도 묶지 않는다: '중단원 EXERCISES'와 '대단원 EXERCISES'는 다른 구획이다.
    한 번만 읽힌 이름은 가장 비슷한 것에 붙인다.
    """
    exact: dict[tuple, list[int]] = {}
    for p in sorted(names):
        exact.setdefault((tuple(_indexes(names[p])), _text(names[p])), []).append(p)
    # 차례 표시가 안 읽힌 이름('실력 굳히기')은, 글자가 같고 표시가 붙은 이름이 하나뿐이면('C 실력 굳히기') 그것과 같다
    marks: dict[str, set[tuple]] = {}
    for idx, text in exact:
        if idx:
            marks.setdefault(text, set()).add(idx)
    joined: dict[tuple, list[int]] = {}
    for (idx, text), g in exact.items():
        key = (next(iter(marks[text])), text) if not idx and len(marks.get(text, ())) == 1 else (idx, text)
        joined.setdefault(key, []).extend(g)
    exact = joined
    groups = [list(g) for g in exact.values() if len(g) >= 2]
    sure = len(groups)
    for g in exact.values():
        if len(g) >= 2:
            continue
        p = g[0]
        fits = [k for k, o in enumerate(groups) if _same_stage(names[p], names[o[0]])]
        if fits:
            best = max(fits, key=lambda k: (k < sure, difflib.SequenceMatcher(None, _text(names[p]), _text(names[groups[k][0]])).ratio()))
            groups[best].append(p)
        else:
            groups.append([p])
    return sorted((sorted(g) for g in groups), key=lambda g: g[0])


def _read_toc(idx, reader, pages: list[list[str]], deadline: float | None) -> list[dict]:
    """차례 쪽을 큰 그림으로 읽어 단원 목록을 얻는다: [{"path": [큰 단원, …, 그 단원], "printed": 쪽}…]. 못 읽으면 빈 목록."""
    candidates = [p for p in range(min(len(pages), 24)) if pages[p][0] == "목차"][:4]
    units: list[dict] = []

    def read(p: int) -> str:
        return reader.ask(TOC_PROMPT, idx.page_file(p).read_bytes(), deadline, mime="image/jpeg", hedge=SLOW,
                          variant=CAREFUL)

    with ThreadPoolExecutor(max_workers=4) as ex:
        texts = _all(ex, read, candidates)
    for text in texts:
        for line in text.splitlines():
            parts = _cells(line)
            if len(parts) < 2 or not re.fullmatch(r"\d{1,3}", parts[-1]):
                continue
            path = [s.strip() for s in " | ".join(parts[:-1]).replace("|", ">").split(">") if s.strip() not in NONE_WORDS]
            if path:
                units.append({"path": path, "printed": int(parts[-1])})
    kept: list[dict] = []                               # 쪽 번호가 뒤로 가는 줄은 잘못 읽은 것이다
    for u in units:
        if not kept or u["printed"] > kept[-1]["printed"]:
            kept.append(u)
    return _tidy_paths(kept) if len(kept) >= 2 else []


def _tidy_paths(units: list[dict]) -> list[dict]:
    """차례에서 읽은 단원 경로의 깊이를 고르게 한다.

    차례는 쪽마다 따로 읽히고 줄마다 조금씩 다르게 적혀 온다: 큰 단원 이름이 빠진 줄('07 로그함수(1)')도 있고,
    바로 앞 단원 밑에 잘못 달린 줄('08 로그함수(2) > 연습문제')도 있다. 그대로 두면 "1단원 전체"를 모을 때
    큰 단원 이름이 빠진 줄들이 빠진다.
    """
    leaves = {_norm(u["path"][-1]) for u in units}
    for u in units:                                     # 다른 줄의 단원이 부모 자리에 온 것은 뗀다
        u["path"] = [s for s in u["path"][:-1] if _norm(s) not in leaves] + [u["path"][-1]]
    heads = {_norm(u["path"][0]) for u in units if len(u["path"]) >= 2}
    chain: list[str] = []                               # 지금 줄들이 속한 큰 단원들
    seen: dict[str, list[str]] = {}                     # 중간 단원 → 그 위의 단원들
    for u in units:
        path = u["path"]
        if len(path) >= 2:
            if _norm(path[0]) in seen:                  # 중간 단원부터 적힌 줄: 그 위를 채운다
                path = seen[_norm(path[0])] + path
            for k in range(1, len(path) - 1):
                seen[_norm(path[k])] = path[:k]
            chain = path[:-1]
        elif _norm(path[0]) in heads:
            chain = [path[0]]                           # 큰 단원 자체가 쪽 번호와 함께 적힌 줄
        elif chain:
            path = chain + path
        u["path"] = path
    return units


def _offset(idx, reader, deadline: float | None) -> int | None:
    """PDF 쪽 번호에서 인쇄된 쪽 번호를 뺀 값. 본문 몇 쪽의 쪽 번호를 읽혀서, 둘 이상이 같은 값을 가리키면 그것으로 한다."""
    n = idx.page_count
    probes = sorted({int(n * f) for f in (0.2, 0.3, 0.45, 0.55, 0.7)})

    def read(p: int) -> int | None:
        ans = reader.ask(PAGE_NUMBER_PROMPT, idx.page_file(p).read_bytes(), deadline, mime="image/jpeg")
        m = re.fullmatch(r"\D*(\d{1,3})\D*", ans.strip())
        return (p + 1) - int(m.group(1)) if m else None

    with ThreadPoolExecutor(max_workers=len(probes)) as ex:
        diffs = [d for d in _all(ex, read, probes) if d is not None]
    if not diffs:
        return None
    best = max(set(diffs), key=diffs.count)
    return best if diffs.count(best) >= 2 else None


def build(idx, reader, deadline: float | None = None) -> dict:
    """교재 전체의 뼈대를 모델에게 물어 저장한다. 이미 있으면 그것을 돌려준다."""
    data = load(idx)
    if data is not None:
        return data
    started, cost0, calls0 = time.time(), reader.cost, reader.calls
    groups = [list(range(i, min(i + PER_SHEET, idx.page_count))) for i in range(0, idx.page_count, PER_SHEET)]

    def one(group: list[int]) -> dict[int, list[str]]:
        image = sheet(idx, group)
        got: dict[int, list[str]] = {}
        for _ in range(2):                              # 쪽이 빠진 답이 오면 한 번 더 물어본다
            got.update(parse(reader.ask(PROMPT, image, deadline, mime="image/jpeg", hedge=SLOW), group))
            if len(got) == len(group):
                break
        return got

    reads: dict[str, list[str]] = {}                    # 큰 그림으로 다시 읽은 쪽 → 읽힌 제목들(나중에 따져 보려고 남긴다)

    def read_name(p: int) -> list[str]:
        """p쪽(1부터)에서 새로 시작하는 구획의 제목들. 위에서부터."""
        ans = reader.ask(NAME_PROMPT, idx.page_file(p - 1).read_bytes(), deadline, mime="image/jpeg")
        lines = [s.strip().strip("`").strip() for s in ans.splitlines()]
        reads[str(p)] = [s for s in lines if s and s not in NONE_WORDS][:6]
        return reads[str(p)]

    pages: list[list[str]] = [["기타", "", ""] for _ in range(idx.page_count)]
    missing = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for group, got in zip(groups, _all(ex, one, groups)):
            missing += len(group) - len(got)
            for p, info in got.items():
                pages[p] = info
        units = _read_toc(idx, reader, pages, deadline)
        offset = _offset(idx, reader, deadline) if units else None
        if offset is None:
            units = []
        names = {p: sec for p, (_, _, sec) in enumerate(pages, 1) if sec}
        clusters = _clusters(names)
        # 구획(단계)은 단원마다 되풀이된다. 여러 단원에 나오는 이름만 구획으로 삼는다: 줄인 그림에서는 유형 제목 같은
        # 소제목도 구획처럼 읽혀 오는데, 그런 것은 이름이 단원마다 달라서 되풀이되지 않는다
        starts_of_units = [u["printed"] + offset for u in units]

        def unit_of(p: int) -> int:
            return sum(1 for s in starts_of_units if s <= p)

        def recurring(g: list[int]) -> bool:
            return len({unit_of(p) for p in g}) >= 2 if units else len(g) >= 3

        stages = [g for g in clusters if recurring(g)]
        loose = sorted(p for g in clusters if not recurring(g) for p in g)
        # 줄인 그림이 "이 쪽에서 구획이 시작한다"고 짚은 쪽을 모두 큰 그림으로 다시 읽어, 둘을 함께 쓴다.
        # 줄인 그림만 믿으면 STEP 1이 STEP 2로, 한 쪽의 둘째 제목이 첫째 제목 대신 읽힌 쪽이 하나만 끼어도 그 단원의
        # 구획이 통째로 틀린 이름을 단다. 큰 그림만 믿으면 구획 제목 대신 개념 소제목만 읽어 오는 쪽에서 구획을 잃는다.
        probes = sorted(p for g in stages for p in g)
        checked = dict(zip(probes, _all(ex, read_name, probes)))
        titles = {(p, k): line for p in probes for k, line in enumerate(checked[p])}
        known: dict[str, str] = {}                      # 구획의 (견줄 이름 → 큰 그림에서 읽은 표기)
        canon: dict[tuple[int, int], str] = {}          # 큰 그림에서 읽힌 제목 가운데 여러 단원에 되풀이되는 것 → 표기
        for g in _clusters(titles):
            if not recurring(sorted({p for p, _ in g})):
                continue
            seen = [titles[x] for x in g]
            seen = [s for s in seen if _indexes(s)] or seen         # 표시(A, B, C)가 읽힌 표기를 쓴다
            name = max(sorted(set(seen)), key=seen.count)
            if seen.count(name) == 1 and len(set(seen)) > 1:
                # 'STEP UP Theme 1~3', 'STEP UP Theme 4~6'처럼 뒤쪽만 다른 이름은 같은 앞부분을 구획 이름으로 쓴다
                x, y = seen[0].split(), seen[1].split()
                head = x[:next((k for k, (v, w) in enumerate(zip(x, y)) if v != w), min(len(x), len(y)))]
                name = " ".join(head) or seen[0]
            known[_stage_key(name)] = name
            for key in g:
                canon[key] = name
        guess: dict[int, str] = {}                      # 줄인 그림이 붙인 이름을 큰 그림의 표기로 옮긴 것
        for g in stages:
            votes = [canon[(p, k)] for p in g for k in range(len(checked[p]))
                     if (p, k) in canon and _same_stage(titles[(p, k)], names[p], 0.5)]
            for p in g:
                guess[p] = max(sorted(set(votes)), key=votes.count) if votes else ""   # 어느 쪽에서도 확인되지 않으면 헛구획
        unit, current = -1, ""
        for p in probes:
            if unit_of(p) != unit:
                unit, current = unit_of(p), ""
            found = [canon[(p, k)] for k in range(len(checked[p])) if (p, k) in canon]
            if found:
                # 앞쪽에서 이어지는 구획의 머리글도 제목으로 읽혀 온다. 지금 구획과 다른 첫 제목이 여기서 시작하는 구획이다
                # (한 쪽에 구획 제목이 둘이면 위의 것)
                fresh = [v for v in found if v != current]
                # 단원 이름과 같은 제목('중단원 EXERCISES' 단원의 첫 쪽에 찍힌 그 제목)보다는 그 아래의 구획 제목을 쓴다
                leaf = units[unit - 1]["path"][-1] if units and unit >= 1 else ""
                inner = [v for v in fresh if not same(v, leaf)]
                pages[p - 1][2] = (inner or fresh or found)[0]
            else:
                pages[p - 1][2] = guess[p]              # 큰 그림에서 제목을 못 읽은 쪽은 줄인 그림을 믿는다
            current = pages[p - 1][2] or current
        for p in loose:
            pages[p - 1][2] = ""
        # 어떤 단원에 구획 하나가 빠져 있으면, 그 자리에서 다른 이름으로 읽힌 쪽이 사실은 그 구획일 수 있다.
        # 그런 쪽만 큰 그림으로 다시 읽는다(단원마다 몇 쪽)
        if units and known:
            bounds = starts_of_units + [len(pages) + 1]
            in_unit = [{_stage_key(pages[p - 1][2]) for p in range(max(1, a), min(b, len(pages) + 1)) if pages[p - 1][2]}
                       for a, b in zip(bounds, bounds[1:])]
            # 여러 단원에 두루 있는 구획만 '빠졌다'고 본다(대단원 끝에만 있는 구획은 없는 단원이 많은 것이 정상이다).
            # 단원 끝의 작은 구획('서술형 주관식')은 줄인 그림에서 절반쯤 놓치므로, 열에 넷 넘게 보이면 두루 있는 것으로 친다.
            # 차례에 같은 이름으로 여러 번 나오는 단원('중단원 EXERCISES')은 저희끼리 짜임이 같으므로 따로 견준다
            family = [_norm(u["path"][-1]) for u in units]
            family = [f if family.count(f) >= 2 else "" for f in family]

            def place(k: str, members: list[int]) -> float:
                """그 구획이 단원 안에서 보통 어디쯤(0~1)에서 시작하는가."""
                spots = [(p - bounds[i]) / max(1, bounds[i + 1] - bounds[i]) for i in members
                         for p in range(max(1, bounds[i]), min(bounds[i + 1], len(pages) + 1))
                         if _stage_key(pages[p - 1][2]) == k]
                return sum(spots) / max(1, len(spots))

            # 빠진 구획은 앞뒤 구획 사이의 어느 쪽에서 시작한다. 그 사이의 쪽들을 큰 그림으로 읽어 찾는다.
            # (빠뜨리면 앞 구획이 뒤 구획의 문제까지 차지해 버린다)
            suspects: list[int] = []
            for fam in sorted(set(family)):
                members = [i for i, f in enumerate(family) if f == fam]
                need = max(2.0, 0.4 * len(members))
                usual = {k for k in known if sum(1 for i in members if k in in_unit[i]) >= need}
                order = sorted(usual, key=lambda k: place(k, members))
                for m in members:
                    a, b, have = max(1, bounds[m]), min(bounds[m + 1], len(pages) + 1), in_unit[m]
                    found = {_stage_key(pages[p - 1][2]): p for p in range(b - 1, a - 1, -1) if pages[p - 1][2]}
                    for i, k in enumerate(order):
                        if k in have:
                            continue
                        lo = max([found[x] + 1 for x in order[:i] if x in found], default=a)
                        hi = min([found[x] for x in order[i + 1:] if x in found], default=b)
                        suspects += [p for p in range(lo, hi) if not pages[p - 1][2]][:8]
            suspects = sorted(set(suspects))[:40]
            for p, seen in zip(suspects, _all(ex, read_name, suspects)):
                # 그 단원에 이미 있는 구획의 이름은 앞쪽에서 이어지는 머리글이다. 아직 없는 구획의 첫 제목을 쓴다
                have = in_unit[unit_of(p) - 1] if unit_of(p) >= 1 else set()
                match = next((v for line in seen for v in known.values()
                              if _same_stage(v, line) and _stage_key(v) not in have), None)
                if match:
                    pages[p - 1][2] = match
    data = {"version": VERSION, "pages": pages, "units": units, "offset": offset,
            "bounds": {}, "reads": reads, "missing": missing, "requests": reader.calls - calls0,
            "seconds": round(time.time() - started, 1), "cost": round(reader.cost - cost0, 5),
            "model": f"{reader.provider}/{reader.model}", "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _save(idx, data)
    return data


def _unspace(name: str) -> str:
    """'연 습 문 제'처럼 글자 사이를 벌려 찍은 제목은 붙여 쓴다."""
    parts = name.split()
    return "".join(parts) if len(parts) >= 3 and all(len(s) == 1 for s in parts) else name


def _norm(s: str) -> str:
    return re.sub(r"[\s.·,:\-–—()\[\]]", "", s)


def same(a: str, b: str) -> bool:
    """같은 단원 이름인가. 쪽마다 조금씩 다르게 읽히므로 넉넉히 본다."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ma, mb = re.match(r"[0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX]+", na), re.match(r"[0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX]+", nb)
    if ma and mb:                                       # 번호가 붙어 있으면 번호로 가린다
        return ma.group(0) == mb.group(0) and difflib.SequenceMatcher(None, na, nb).ratio() >= 0.5
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.75


def _body_end(pages: list[list[str]]) -> int:
    """본문의 마지막 쪽(1부터). 책 뒤의 정답·풀이 모음은 뺀다."""
    n = len(pages)
    for start in range(int(n * 0.5), n - 5):
        tail = pages[start:]
        if pages[start][0] == "해설" and sum(1 for k, _, _ in tail if k == "해설") >= 0.8 * len(tail):
            return start
    return n


def entries(data: dict) -> list[dict]:
    """뼈대를 '단원 > 구획: 몇 쪽~몇 쪽' 목록으로 만든다(쪽 번호는 1부터).

    단원은 차례에서 읽은 것을 쓴다(쪽마다의 종류 판정은 틀리는 일이 있어 경계로 쓰지 않는다). 구획은 그 이름이
    보이는 쪽에서 시작해 다음 구획이나 다음 단원 앞에서 끝난다. 차례를 읽지 못한 교재는 쪽마다 읽힌 단원
    이름을 다음 이름이 나올 때까지 이어 가는 방식으로 대신한다.
    """
    pages = data["pages"]
    end_of_body = _body_end(pages)
    starts = {p: _unspace(sec) for p, (_, _, sec) in enumerate(pages, 1) if sec and p <= end_of_body}
    out: list[dict] = []

    def trim(a: int, b: int) -> int:
        while b > a and pages[b - 1][0] in ("단원표지", "기타", "목차", "표지"):
            b -= 1
        return b

    if data.get("units"):
        d = data["offset"]
        units = [dict(u, start=u["printed"] + d) for u in data["units"] if 1 <= u["printed"] + d <= end_of_body]
        ends = [units[i + 1]["start"] - 1 if i + 1 < len(units) else end_of_body for i in range(len(units))]
        # 구획은 단원마다 같은 차례로 나온다(A → B → C). 어떤 단원에서 앞 차례의 구획이 뒤 차례의 구획보다 나중에
        # 또 나오면 잘못 읽힌 것이다. 그대로 두면 뒤 구획이 거기서 끊기고 남은 문제가 앞 구획의 이름을 달게 된다.
        # 차례는 같은 짜임의 단원끼리(차례에 같은 이름으로 여러 번 나오는 단원은 저희끼리) 견준다
        family = [_norm(u["path"][-1]) for u in units]
        family = [f if family.count(f) >= 2 else "" for f in family]
        spots: dict[tuple[str, str], list[float]] = {}
        for i, u in enumerate(units):
            for p in starts:
                if u["start"] <= p <= ends[i]:
                    spots.setdefault((family[i], starts[p]), []).append((p - u["start"]) / max(1, ends[i] - u["start"]))
        rank = {k: sum(v) / len(v) for k, v in spots.items() if len(v) >= 2}
        for i, u in enumerate(units):
            top = -1.0
            for p in sorted(p for p in starts if u["start"] <= p <= ends[i]):
                r = rank.get((family[i], starts[p]))
                if r is None:
                    continue
                if r < top - 0.15:
                    del starts[p]
                else:
                    top = max(top, r)
        for i, u in enumerate(units):
            end = ends[i]
            cuts = sorted(p for p in starts if u["start"] <= p <= end)
            # 같은 이름이 잇달아 읽힌 것은 한 구획이다(제목 쪽과 그 다음 쪽이 둘 다 시작으로 읽히는 일이 있다)
            cuts = [c for k, c in enumerate(cuts) if k == 0 or starts[c] != starts[cuts[k - 1]]]
            segs = [(u["start"], cuts[0] - 1 if cuts else end, "")] if not cuts or cuts[0] > u["start"] else []
            segs += [(c, cuts[j + 1] - 1 if j + 1 < len(cuts) else end, starts[c]) for j, c in enumerate(cuts)]
            for a, b, name in segs:
                # 단원에 구획이 하나뿐이고 이름이 없거나 단원 이름과 같으면, 목록에는 단원 이름만 적는다
                alone = len(segs) == 1 and (not name or same(name, u["path"][-1]))
                out.append({"path": u["path"], "section": name, "start": a, "end": trim(a, b), "alone": alone})
    else:
        major = minor = ""
        cur: dict | None = None
        for p, (kind, unit, sec) in enumerate(pages[:end_of_body], 1):
            if kind in ("표지", "목차"):
                cur = None
                continue
            if kind == "단원표지":
                cur = None
                if unit:
                    major, minor = unit, ""
                continue
            fresh = bool(unit) and not same(unit, minor) and not same(unit, major)
            if fresh:
                minor = unit
            if sec or fresh or cur is None:
                cur = {"path": [s for s in (major, minor) if s], "section": sec, "start": p, "end": p}
                out.append(cur)
            cur["end"] = p
        for e in out:
            e["end"] = trim(e["start"], e["end"])
    for k, e in enumerate(out, 1):
        e["id"] = f"S{k}"
        e.setdefault("alone", False)
    return out


def describe(idx, data: dict) -> list[str]:
    """에이전트가 읽을 뼈대 목록. 한 줄에 구획 하나. 이미 박스를 찾아 둔 쪽들이면 문제 수와 번호도 적는다."""
    lines = []
    for e in entries(data):
        name = " > ".join(e["path"] + ([e["section"] or "(구획 이름 없음)"] if not e["alone"] else []))
        span = f"{e['start']}쪽" if e["start"] == e["end"] else f"{e['start']}~{e['end']}쪽"
        boxes = [idx.peek_boxes(p - 1) for p in range(e["start"], e["end"] + 1)]
        extra = ""
        if all(b is not None for b in boxes):
            found = [(p, b["label"]) for p, page in zip(range(e["start"], e["end"] + 1), boxes) for b in page]
            labels = [v for _, v in found]
            opening = data.get("bounds", {}).get(f"{e['start']}:{e['section']}", "")
            cut = _restart(found, bool(e["section"]), e["start"], opening)
            if cut is not None:
                extra = (f", 문제 {cut}개" + (f"({labels[0]}~{labels[cut - 1]})" if cut else "")
                         + f" — {found[cut][0]}쪽부터는 문제 번호가 새로 시작해({labels[cut]}) 이름표 없는 다른 묶음으로 봅니다")
            elif e["section"] and labels and found[0][0] > e["start"] and not opening:
                extra = (f", 문제 {len(labels)}개({labels[0]}~{labels[-1]}) — 구획이 시작하는 {e['start']}쪽에서 문제 박스를 "
                         "찾지 못해, 이 문제들이 이 구획의 것인지 확실하지 않습니다")
            else:
                extra = f", 문제 {len(labels)}개" + (f"({labels[0]}~{labels[-1]})" if labels else "")
        lines.append(f"{e['id']}  {name}: {span}{extra}")
    return lines


def boundary(idx, data: dict, reader, page: int, section: str) -> str:
    """page(1부터)에서 시작하는 구획의 첫 문제 번호. 구획이 쪽 중간에서 시작하는 교재에서 앞뒤 구획을 가르는 데 쓴다.

    쪽 전체 그림을 모델에게 한 번 보여 주고 답을 저장해 둔다. 알아내지 못하면 빈 글자.
    """
    key = f"{page}:{section}"
    if key in data.setdefault("bounds", {}):
        return data["bounds"][key]
    answer = reader.ask(BOUNDARY_PROMPT.format(section=section), idx.page_file(page - 1).read_bytes(),
                        time.time() + 60, mime="image/jpeg").strip().strip("`").split("\n")[0].strip()
    data["bounds"][key] = "" if answer in NONE_WORDS else answer
    _save(idx, data)
    return data["bounds"][key]


def _label_key(label: str) -> str:
    return re.sub(r"\s", "", label).lstrip("0") or "0"


def _shape(label: str) -> tuple[str, tuple[int, ...]]:
    """번호의 꼴과 수: '유제 02-1' → ('유제0-0', (2, 1))."""
    s = re.sub(r"\s", "", label)
    return re.sub(r"\d+", "0", s), tuple(int(v) for v in re.findall(r"\d+", s))


def _restart(found: list[tuple[int, str]], named: bool = False, start: int | None = None,
             first_label: str = "") -> int | None:
    """구획에서 모은 (쪽, 문제 번호)들에서, 다른 묶음이 시작되는 자리(없으면 None. 0이면 하나도 이 구획의 것이 아니다).

    구획은 이름표가 보이는 쪽에서 시작해 다음 이름표 앞까지로 잡는데, 이름표 없이 다른 묶음이 이어지는 교재가
    있다(한 쪽짜리 '개념 TEST' 뒤의 예제 쪽들). 한 구획의 번호는 같은 꼴로 한 줄로 이어지므로 두 가지를 본다.
    ① 같은 꼴의 번호가 구획의 첫 번호 이하로 돌아간다. 글자 인식이 번호 하나를 잘못 읽은 것과 가리려고,
       같은 꼴의 다음 번호도 낮게 이어질 때만 그렇게 본다.
    ② 어느 쪽부터 끝까지, 구획이 시작하는 쪽에 있던 꼴의 번호가 하나도 없고 다른 꼴의 번호만 둘 이상 나온다.
       이름이 붙은 구획(named)은 문제 묶음 하나라 번호의 꼴이 하나뿐이므로, 끝까지 보지 않고 그런 쪽이
       처음 나오는 데서 끊는다. 이름 없는 구획(개념 쪽들)은 예제와 유제처럼 여러 꼴이 번갈아 나오는 것이 정상이다.
    start는 구획이 시작하는 쪽, first_label은 모델이 읽어 준 제목 다음 첫 문제의 번호다. 시작하는 쪽에서 박스를
    못 찾았어도 이 번호의 꼴로 뒤쪽을 가릴 수 있다.
    """
    shapes = [_shape(v) for _, v in found]
    numbered = [i for i, (_, n) in enumerate(shapes) if n]
    if not numbered:
        return None
    cuts: list[int] = []
    first: dict[str, tuple[int, ...]] = {}
    top: dict[str, tuple[int, ...]] = {}
    for i, (s, n) in enumerate(shapes):
        if not n:
            continue
        if s in first and n <= first[s]:
            after = next((m for t, m in shapes[i + 1:] if t == s and m), None)
            if after is None or after < top[s]:
                cuts.append(i)
                break
            continue
        first.setdefault(s, n)
        top[s] = max(top.get(s, n), n)
    home = start if named and start is not None else found[numbered[0]][0]
    base = {shapes[i][0] for i in numbered if found[i][0] == home}
    if _shape(first_label)[1]:
        base.add(_shape(first_label)[0])
    if base:
        last = max((i for i in numbered if shapes[i][0] in base), default=-1)
        tail = [i for i in numbered if i > last]
        if len(tail) >= 2 and (last < 0 or found[tail[0]][0] > found[last][0]):
            cuts.append(next(i for i, (p, _) in enumerate(found) if p == found[tail[0]][0]))
        if named:
            for page in sorted({found[i][0] for i in numbered if found[i][0] > home}):
                here = [shapes[i][0] for i in numbered if found[i][0] == page]
                if len(here) >= 2 and not base & set(here):
                    cuts.append(next(i for i, (p, _) in enumerate(found) if p == page))
                    break
    return min(cuts) if cuts else None


def collect(idx, data: dict | None, reader, get_boxes, section_ids: list[str] | None = None,
            pages: tuple[int, int] | None = None, first: str | None = None, last: str | None = None) -> dict:
    """구획들(또는 쪽 범위)의 문제 박스를 차례대로 모은다. get_boxes(쪽, 0부터)는 그 쪽의 박스 목록을 준다.

    돌려주는 값: {"boxes": [(쪽(1부터), 박스)…], "notes": [사람이 확인할 점…], "spans": [무엇을 모았는지…]}
    """
    all_entries = entries(data) if data else []
    picked: list[tuple[int, dict]] = []
    notes: list[str] = []
    spans: list[str] = []

    def index_of(boxes: list[dict], label: str) -> int | None:
        want = _label_key(label)
        return next((k for k, b in enumerate(boxes) if _label_key(b["label"]) == want), None)

    ranges: list[tuple[int, int, dict | None]] = []
    for sid in section_ids or []:
        e = next((x for x in all_entries if x["id"].lower() == sid.lower()), None)
        if e is None:
            raise ValueError(f"그런 구획이 없습니다: {sid} (outline 명령으로 목록을 확인하세요)")
        if pages:
            # 구획과 쪽 범위를 같이 주면 "그 구획 가운데 그 쪽들"이다(둘을 합치면 구획 전체가 딸려 온다)
            a, b = max(e["start"], pages[0]), min(e["end"], pages[1])
            if a > b:
                notes.append(f"{sid} 구획({e['start']}~{e['end']}쪽)에는 {pages[0]}~{pages[1]}쪽이 없어 넣지 않았습니다")
                continue
            ranges.append((a, b, e if (a, b) == (e["start"], e["end"]) else None))
        else:
            ranges.append((e["start"], e["end"], e))
    if pages and not section_ids:
        ranges.append((pages[0], pages[1], None))
    if not ranges:
        raise ValueError("; ".join(notes) or "모을 범위가 없습니다. --section 이나 --pages 를 주세요")
    for start, end, e in ranges:
        if not (1 <= start <= end <= idx.page_count):
            raise ValueError(f"쪽 범위가 교재를 벗어납니다: {start}~{end} (전체 {idx.page_count}쪽)")
        name = " > ".join(e["path"] + ([e["section"]] if e["section"] else [])) if e else f"{start}~{end}쪽"
        before = len(picked)
        empty = []
        opening = ""                                    # 구획 제목 다음의 첫 문제 번호(모델이 읽어 준 것)
        for p in range(start, end + 1):
            boxes = get_boxes(p - 1)
            if e is not None and p == start and e["section"] and reader is not None:
                # 구획이 쪽 중간에서 시작할 수 있다. 제목 다음의 첫 문제부터 넣는다
                label = opening = boundary(idx, data, reader, p, e["section"])
                at = index_of(boxes, label) if label else None
                if not boxes:
                    notes.append(f"{p}쪽: '{e['section']}' 구획이 시작하는 쪽인데 문제 박스를 하나도 찾지 못했습니다"
                                 + (f"(첫 문제는 {label})" if label else "") + ". 그 쪽의 문제는 빠졌습니다 — "
                                 "`look`·`add` 명령이나 선택 화면의 `직접 그리기`로 넣을 수 있습니다")
                elif at is None and label:
                    notes.append(f"{p}쪽: '{e['section']}' 구획의 첫 문제({label})를 박스에서 찾지 못해 그 쪽의 문제를 모두 넣었습니다")
                boxes = boxes[at or 0:]
            if not boxes and data and data["pages"][p - 1][0] == "문제" and not (e is not None and p == start):
                empty.append(p)
            picked += [(p, b) for b in boxes]
        nxt = None
        if e is not None:
            k = all_entries.index(e)
            nxt = all_entries[k + 1] if k + 1 < len(all_entries) else None
        if nxt is not None and nxt["start"] == end + 1 and nxt["section"] and reader is not None:
            # 다음 구획이 시작하는 쪽의 위쪽에 이 구획의 마지막 문제들이 있을 수 있다
            boxes = get_boxes(end)
            label = boundary(idx, data, reader, end + 1, nxt["section"]) if boxes else ""
            at = index_of(boxes, label) if label else None
            if at:
                picked += [(end + 1, b) for b in boxes[:at]]
        got = picked[before:]
        cut = _restart([(p, b["label"]) for p, b in got], bool(e["section"]), start, opening) if e is not None else None
        if cut is not None:
            # 틀린 문제가 섞이는 것이 빠지는 것보다 나쁘다. 뒤쪽은 넣지 않고, 뺀 사실과 넣는 방법을 알린다
            rest = got[cut:]
            del picked[before + cut:]
            got = picked[before:]
            kept = f"{got[-1][0]}쪽의 {got[-1][1]['label']}번까지만 모았습니다" if got else "한 문제도 모으지 못했습니다"
            notes.append(f"'{name}' 구획은 {kept}. {rest[0][0]}쪽부터는 문제 번호가 새로 시작해({rest[0][1]['label']}) 이름표 없는 "
                         f"다른 묶음으로 보고 뺐습니다({rest[0][0]}~{rest[-1][0]}쪽, {len(rest)}문제). 그 문제들도 필요하면 "
                         f"--pages {rest[0][0]}-{rest[-1][0]} 로 따로 모으세요")
            end = got[-1][0] if got else start
        labels = [b["label"] for _, b in got]
        spans.append(f"{name}: {start}~{end}쪽, 문제 {len(got)}개" + (f"({labels[0]}~{labels[-1]})" if labels else ""))
        if empty:
            notes.append(f"문제 쪽인데 박스를 하나도 못 찾은 쪽: {', '.join(map(str, empty))}쪽")
    if first or last:
        labels = [b["label"] for _, b in picked]
        a = next((k for k, v in enumerate(labels) if _label_key(v) == _label_key(first)), None) if first else 0
        z = next((k for k in range(len(labels) - 1, -1, -1) if _label_key(labels[k]) == _label_key(last)), None) \
            if last else len(labels) - 1
        if a is None or z is None:
            raise ValueError(f"그 번호의 문제를 범위 안에서 찾지 못했습니다: {first if a is None else last}")
        picked = picked[a:z + 1]
    # 번호가 숫자로 이어지는 교재면, 건너뛴 번호를 알린다(박스를 못 찾은 문제)
    nums = [(b["label"], p) for p, b in picked if b["label"].isdigit()]
    gaps = []
    for (x, px), (y, py) in zip(nums, nums[1:]):
        if 1 < int(y) - int(x) <= 6:
            where = f"{px}쪽" if px == py else f"{px}~{py}쪽"
            gaps += [f"{str(v).zfill(len(x))}({where})" for v in range(int(x) + 1, int(y))]
    if gaps:
        notes.append("박스를 못 찾아 빠진 번호: " + ", ".join(gaps) + " — `look`·`add` 명령이나 선택 화면의 `직접 그리기`로 넣을 수 있습니다")
    # 범위의 맨 앞·맨 뒤에서 빠진 문제는 모은 번호끼리 견줘서는 안 보인다(쪽의 첫 문제, 마지막 문제). 앞뒤 쪽의 번호와 견준다
    if nums and not (first or last):
        edges = []
        (x, px), (y, py) = nums[0], nums[-1]
        before = [b["label"] for b in (get_boxes(px - 2) if px >= 2 else []) if b["label"].isdigit() and len(b["label"]) == len(x)]
        after = [b["label"] for b in (get_boxes(py) if py < idx.page_count else []) if b["label"].isdigit() and len(b["label"]) == len(y)]
        if before and 1 < int(x) - int(before[-1]) <= 6:
            edges += [f"{str(v).zfill(len(x))}({px - 1}~{px}쪽)" for v in range(int(before[-1]) + 1, int(x))]
        if after and 1 < int(after[0]) - int(y) <= 6:
            edges += [f"{str(v).zfill(len(y))}({py}~{py + 1}쪽)" for v in range(int(y) + 1, int(after[0]))]
        if edges:
            notes.append("범위의 맨 앞이나 맨 뒤에서 빠진 번호일 수 있습니다: " + ", ".join(edges) + " — 박스를 못 찾은 문제인데 "
                         "범위 안쪽 쪽에 있는지 바깥 쪽에 있는지는 모릅니다. `look`으로 범위 안의 쪽을 보고, 거기 있으면 `add`로 넣으세요")
    return {"boxes": picked, "notes": notes, "spans": spans}
