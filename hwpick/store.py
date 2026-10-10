"""문제 하나하나의 준비물을 모아 두는 곳(hwpick_data/store/<열쇠>/).

선택 화면에서 문제를 담는 순간 여기에 문제 그림을 잘라 넣고 모델에게 옮겨 적게 한다. 그러면 사용자가
"한글 파일로 만들어 줘"라고 할 때는 글이 이미 준비되어 있어 문서만 만들면 된다. 태스크나 순서가 아니라
문제 자체(교재·쪽·영역)로 찾으므로, 순서를 바꾸거나 다른 목록에 다시 담아도 다시 옮겨 적지 않는다.

    clean.png        문제 그림(공통 발문이 있으면 위에 붙인다)
    meta.json        그림 속 위치를 쪽 위치로 바꾸는 데 쓸 값
    fig-<해시>.png   모델이 넣기로 한 그림
    state.json       결과: 문서에 넣을 글(text)과 그것을 저장 형식으로 바꾼 것(reading, hwp-exam-restoration의
                     restoration-reading/1과 같은 꼴), 그림 목록, 오류·주의, 모델의 답 원문

그림을 넣을지 말지와 어디가 그림인지는 모델이 정한다. 모델이 짚은 범위는 대략적이어서, 실제로 자를
범위는 figures.snap이 잉크에 맞춰 정한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from . import reading, tasks, transcript

VERSION = 4                # 지시문이나 결과 형식을 바꾸면 올린다(예전 결과를 다시 만들게 된다)
LOCAL = 3                  # 모델의 답을 다듬는 방식(그림 자르기 등)을 바꾸면 올린다(답은 그대로 두고 다듬기만 다시 한다)
#                            2~3: 붙일 그림을 교재의 스캔 해상도 그대로 자른다(예전에는 300dpi로 다시 그렸다). 스캔이 아닌 쪽은 600dpi로
BLANK = 2                  # 빈칸 적는 법이 바뀌면 올린다(빈칸이 든 문제만 다시 옮겨 적게 된다)
BLANK_MARK_RE = re.compile(r"□|\\square|\\Box|\\boxed")
LAYOUT_LOCK = threading.Lock()      # 쪽 레이아웃 계산은 한 번에 하나씩(여러 스레드가 같은 쪽을 동시에 풀지 않게)
FIGURE_RE = re.compile(r"!\[[^\]]*\]\(\s*figure\s*:\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)")
MATH_RE = re.compile(r"\$\$.+?\$\$|\$.+?\$", re.S)
# 글 속에 맨몸으로 적힌 수. 괄호 번호 '(1)', 배점 '[4점]', 영문·숫자에 붙은 것은 건드리지 않는다
BARE_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.,(\[])\d+(?:\.\d+)?(?![A-Za-z0-9)\]]|[.,]\d)")

PROMPT = """첨부한 그림은 수학 교재의 문제 하나입니다. 보이는 내용을 아래 규칙대로 글로 옮겨 적으세요.
문제를 풀거나 고치지 말고 보이는 대로만 옮깁니다. 옮겨 적은 내용만 답으로 내고, 설명·인사말·코드 블록 표시(```)는 붙이지 않습니다.

1. **버릴 것**: 문제 번호, 맨 위 줄 앞의 번호 범위(`[0020~0021]`), 난이도 표시(★, 상·중·하, 표정 그림), `대표문제`·`서술형`·`고난도`·`예제`·`유제` 같은 꼬리표, 출처(○○고, 2021학년도 수능 등), 쪽 안내, QR코드.
2. **남길 것**: 문제 글, 수식, 선택지, 보기·조건 상자, 배점(`[4점]`).
3. **수식**은 LaTeX로 적고 `$` 로 감쌉니다. 예: `$x^2 + 2x - 1$`, `$\\frac{1}{2}$`, `$\\sin A$`. 글자 하나짜리 문자(`$x$`, `$A$`)와 숫자(`$3$`)도 수식으로 적습니다.
   도형의 점·변·도형 이름처럼 **기울지 않고 곧게 선 알파벳**은 `\\mathrm{}` 으로 감쌉니다. 예: `$\\mathrm{ABC}$`, `$\\overline{\\mathrm{AB}}$`, `$\\angle \\mathrm{B}$`. 기울어진 글자는 `$A$` 그대로 둡니다.
4. 한 줄을 통째로 차지하는 가운데 수식은 그 줄에 `$$ ... $$` 만 적습니다.
5. **선택지**는 `① ... ② ... ③ ... ④ ... ⑤ ...` 로 한 줄에 적습니다.
6. **테두리 상자**(보기, 조건)는 `::: box 보기` 줄로 시작하고 `:::` 줄로 끝냅니다. 이름이 없는 상자는 `::: box` 로 시작합니다.
7. **그림**: 도형·그래프·표·삽화처럼 글로 옮길 수 없는 것을 문서에 그림으로 넣을지는 직접 판단하세요. 문제를 이해하거나 푸는 데 쓰이면 넣고, 꾸밈일 뿐이면 넣지 않습니다. 넣을 때는 그림이 놓인 자리에 `![](figure: 왼쪽,위,오른쪽,아래)` 만 한 줄로 적습니다. 네 숫자는 첨부한 그림 전체의 가로와 세로를 각각 0~1000으로 봤을 때 그 그림이 차지하는 범위입니다.
   - 그림에 딸린 글자(꼭짓점 이름, 길이, 눈금, 표 안의 숫자, 말풍선 속 말)는 범위에 함께 넣고, 따로 옮겨 적지 않습니다.
   - 서로 이어진 한 장면(여러 사람과 말풍선 등)은 범위 하나로 묶습니다.
   - 글과 수식만 든 보기·조건 상자와 답을 쓰는 빈칸은 그림이 아닙니다. 글로 옮깁니다.
8. 줄 하나가 문단 하나입니다. 교재에서 자리가 모자라 다음 줄로 넘어갔을 뿐인 문장은 줄을 바꾸지 말고 이어서 한 줄로 적습니다. `(1)`, `(2)` 같은 작은 문제는 줄을 바꿔 적습니다. 답을 써넣는 빈칸은 `□` 로 적습니다. 빈칸 안에 `(가)`, `(나)`, `㉠` 같은 표시가 들어 있으면 그 표시를 넣어 `$\\boxed{(가)}$` 로 적습니다(수식 안이면 `\\frac{n!}{\\boxed{(가)}}` 처럼). 표시를 빼먹거나 빈칸 옆에 따로 적지 않습니다.
9. 손으로 쓴 낙서(풀이 흔적, 동그라미, 밑줄)는 옮기지 않습니다. 낙서가 있었다면 맨 끝 줄에 `<!-- 낙서 있음 -->` 라고 적습니다.
10. **수의 자릿수에 주의**: 수 바로 뒤에 한글이 붙어 있을 때(`24이고`, `1이 아닌`, `3의 배수`) 한글 '이'·'의'의 동그라미(ㅇ)를 숫자 0으로 읽으면 안 됩니다. `24이고`를 `240이고`로, `1이`를 `10이`로 적지 않도록 수의 자릿수를 그림에서 다시 세어 확인합니다.

예시 1. 그림 오른쪽에 삼각형이 있는 문제:

    그림과 같이 $\\overline{\\mathrm{AB}} = 6$, $\\angle \\mathrm{B} = 90^\\circ$인 직각삼각형 $\\mathrm{ABC}$가 있다. $\\overline{\\mathrm{BC}} = 4$일 때, 삼각형 $\\mathrm{ABC}$의 넓이는? [4점]
    ![](figure: 640,60,985,720)
    ① $6$  ② $8$  ③ $10$  ④ $12$  ⑤ $14$

예시 2. 보기 상자가 있는 문제:

    보기에서 옳은 것만을 있는 대로 고른 것은?
    ::: box 보기
    ㄱ. $a > 0$
    ㄴ. $b^2 - 4ac < 0$
    :::
    ① ㄱ  ② ㄴ  ③ ㄱ, ㄴ
"""

RETRY = """

---
앞서 아래와 같이 옮겨 적었는데 검사에서 오류가 났습니다. 그림을 다시 보고, 오류를 고친 전체 내용을 처음부터 다시 적으세요.

[앞서 적은 것]
{answer}

[오류]
{errors}
"""


def key(item: dict) -> str:
    raw = json.dumps([item["pdf_id"], item["page"], [round(float(v), 1) for v in item["bbox"]],
                      [round(float(v), 1) for v in item["stem"]] if item.get("stem") else None])
    return f"{item['pdf_id'][:6]}-{item['page']}-{hashlib.sha1(raw.encode()).hexdigest()[:10]}"


def folder(workspace: str | Path, item: dict) -> Path:
    return tasks.data_dir(workspace) / "store" / key(item)


def _write(path: Path, data: bytes | str) -> None:
    """다른 쪽(선택 화면 서버, make 명령)이 읽는 도중에 반쯤 쓰인 파일을 보지 않게 한다."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def load(folder: Path) -> dict | None:
    """저장된 결과. 없거나 예전 형식이면 None."""
    try:
        st = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if st.get("version") != VERSION:
        return None
    # 표시가 든 빈칸을 적는 법을 정하기 전(BLANK 1)에 옮긴 것 가운데 빈칸이 있는 문제는 표시가 빠졌을 수 있다. 그것만 다시 옮긴다
    if st.get("model") != LIFTED and st.get("blank", 1) != BLANK and BLANK_MARK_RE.search(st.get("answer", "")):
        return None
    return st


def refresh(folder: Path, item: dict, idx) -> dict | None:
    """모델의 답은 그대로 두고, 그 답을 문서에 넣을 글로 바꾸는 일(그림 자르기, 수 바로잡기)만 지금 방식으로 다시 한다."""
    st = load(folder)
    if st is None or st.get("local") == LOCAL or not st.get("answer", "").strip():
        return st
    res = resolve(folder, item, idx, st["answer"])
    st.update(res, status="error" if res["errors"] else "ok", local=LOCAL)
    st.pop("reading", None)
    st.pop("extras", None)
    if not res["errors"]:
        st.update(as_reading(item, idx, st))
    _write(folder / "state.json", json.dumps(st, ensure_ascii=False, indent=1))
    return st


def ready(folder: Path) -> bool:
    st = load(folder)
    return bool(st and st.get("status") == "ok")


def prepare(folder: Path, item: dict, idx) -> None:
    """문제 그림을 잘라 둔다(이미 있으면 그대로 둔다)."""
    if (folder / "clean.png").is_file() and (folder / "meta.json").is_file():
        return
    import cv2

    from .pipeline import SOURCE_DPI, _clip, _stack
    folder.mkdir(parents=True, exist_ok=True)
    page = item["page"] - 1
    rects = ([item["stem"]] if item.get("stem") else []) + [item["bbox"]]
    parts = [_clip(idx, page, r, SOURCE_DPI) for r in rects]
    image = _stack(parts)
    segments, y = [], 0
    for part, rect in zip(parts, rects):
        segments.append({"y": y, "h": int(part.shape[0]), "rect": [float(v) for v in rect]})
        y += part.shape[0] + 12                # _stack이 조각 사이에 두는 간격
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("문제 그림을 만들지 못했습니다")
    _write(folder / "clean.png", buf.tobytes())
    _write(folder / "meta.json", json.dumps({"size": [int(image.shape[1]), int(image.shape[0])], "dpi": SOURCE_DPI,
                                             "segments": segments}, ensure_ascii=False))


LIFTED = "문서에서 그대로 꺼냄"


def as_reading(item: dict, idx, state: dict) -> dict:
    """결과의 글을 저장 형식으로 바꾼 것: {"reading": 문서, "extras": 그 형식에 자리가 없는 HWPick의 힌트}.
    넣지 못하는 글이면 빈 것을 돌려준다."""
    parsed = transcript.parse(state.get("text") or "", figures={f["name"] for f in state.get("figures", [])})
    if parsed.errors:
        return {}
    from .pipeline import DOODLE_NOTE
    doc, extras = reading.document(
        item, parsed.blocks, sha256=reading.source_sha256(idx.path, idx.dir), worker=state.get("model") or "unknown",
        page_width=idx.sizes()[item["page"] - 1][0], uncertain=bool(state.get("warnings")),
        issues=[DOODLE_NOTE] if DOODLE_NOTE in (state.get("answer") or "") + (state.get("text") or "") else [])
    return {"reading": doc, "extras": extras}


def ensure_reading(folder: Path, item: dict, idx) -> dict | None:
    """예전에 저장한 결과에 저장 형식이 없으면 글에서 만들어 채운다(모델을 다시 부르지 않는다)."""
    st = load(folder)
    if st is None or "reading" in st or st.get("status") != "ok":
        return st
    st.update(as_reading(item, idx, st))
    if "reading" in st:
        _write(folder / "state.json", json.dumps(st, ensure_ascii=False, indent=1))
    return st


def lift_stale(state: dict | None) -> bool:
    """문서에서 꺼내 둔 결과인데 꺼내는 방식이 그 뒤로 바뀌었는가(그러면 다시 꺼낸다)."""
    from .hwp import source
    return bool(state) and state.get("model") == LIFTED and state.get("lift", 1) != source.VERSION


def lift(folder: Path, item: dict, idx) -> dict | None:
    """한글 문서에서 고른 문제면, 문서에 든 글·수식·그림을 그대로 꺼내 결과로 저장한다(모델을 부르지 않는다).

    꺼낼 수 없는 문제(PDF의 문제, 직접 그린 영역, 그대로 옮길 수 없는 표나 도형이 든 칸)는 None을 돌려주고,
    그런 문제는 여느 문제처럼 그림을 모델이 읽는다.
    """
    slot = idx.slot(item["pid"]) if getattr(idx, "document", False) else None
    if slot is None or slot.issues:
        return None
    from .hwp import source
    prepare(folder, item, idx)
    figs = []
    for f in slot.figures:
        data = source.extract(idx.hwpx, f["member"])
        suffix = Path(f["member"]).suffix.lower()
        if f.get("part"):
            # 문서에서 그림의 일부만 보이게 잘라 둔 것: 그 부분만 잘라 낸다
            import cv2
            import numpy as np
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
            if img is None:
                return None
            h, w = img.shape[:2]
            l, t, r, b = f["part"]
            ok, buf = cv2.imencode(".png", img[int(t * h):max(int(t * h) + 1, int(round(b * h))),
                                               int(l * w):max(int(l * w) + 1, int(round(r * w)))])
            if not ok:
                return None
            data, suffix = buf.tobytes(), ".png"
        name = f"fig-{hashlib.sha1(data).hexdigest()[:10]}{suffix}"
        if not (folder / name).is_file():
            _write(folder / name, data)
        figs.append({"name": f["name"], "file": name, "width_mm": f["width_mm"], "height_mm": f["height_mm"]})
    parsed = transcript.parse(slot.text, figures={f["name"] for f in figs})
    if parsed.errors:
        return None
    state = {"version": VERSION, "local": LOCAL, "status": "ok", "text": slot.text, "figures": figs, "errors": [],
             "warnings": [], "fixes": [], "answer": "", "tries": 0, "seconds": 0.0, "model": LIFTED, "lift": source.VERSION,
             "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    state.update(as_reading(item, idx, state))
    _write(folder / "state.json", json.dumps(state, ensure_ascii=False, indent=1))
    return state


def tidy(text: str) -> str:
    """모델의 답을 다듬는다: 코드 블록 표시를 걷고, 글 속에 맨몸으로 적힌 수를 수식으로 감싼다.

    추론을 약하게 돌리면 '1이 아닌'처럼 수를 글자로 적는 일이 잦다. 그대로 두면 같은 문서 안에서 수의
    글꼴이 들쭉날쭉해진다.
    """
    from .pipeline import _clean
    out = []
    for line in _clean(text).split("\n"):
        if line.strip().startswith((":::", "![", "<!--")):
            out.append(line)
            continue
        pos, pieces = 0, []
        for m in MATH_RE.finditer(line):
            pieces.append(BARE_NUMBER_RE.sub(lambda n: f"${n.group(0)}$", line[pos:m.start()]))
            pieces.append(m.group(0))
            pos = m.end()
        pieces.append(BARE_NUMBER_RE.sub(lambda n: f"${n.group(0)}$", line[pos:]))
        out.append("".join(pieces))
    return "\n".join(out).strip()


ZERO_RE = re.compile(r"(\d+)0(\$?)([가-힣])")


def fix_zeros(text: str, local: str) -> tuple[str, list[str], list[str]]:
    """모델이 수 뒤의 한글 '이'를 0으로 한 번 더 읽는 버릇을 로컬 글자 인식과 대조해 바로잡는다.

    쓰는 모델은 `24이고`를 `240이고`로 읽는 일이 잦고, 추론을 세게 돌려도 그림을 키워도 그대로다(같은 문제를
    여섯 번씩 시켜 확인). 로컬 인식은 수식에는 약하지만 이런 글줄은 잘 읽는다. 그래서 "0으로 끝나는 수 +
    ㅇ으로 시작하는 한글"이 나오면 로컬 인식이 읽은 글과 대조한다: 로컬에 0 없는 꼴만 있으면 0을 빼고,
    어느 쪽도 없으면 사람이 보라고 알린다. (고친 글, 고친 내용, 확인이 필요한 곳)을 돌려준다.
    """
    flat = re.sub(r"[\s$]", "", local)
    fixed, unsure = [], []

    def repl(m: re.Match) -> str:
        number, dollar, syllable = m.groups()
        if (ord(syllable) - 0xAC00) // 588 != 11:          # 첫소리가 ㅇ인 글자만
            return m.group(0)
        tail = re.sub(r"[^가-힣]", "", text[m.end():m.end() + 6])[:1]     # 뒤따르는 한글 한 글자까지 맞춰 본다
        bad, good = f"{number}0{syllable}{tail}", f"{number}{syllable}{tail}"
        if bad in flat:
            return m.group(0)
        if good in flat:
            fixed.append(f"{number}0{syllable} → {number}{syllable}")
            return f"{number}{dollar}{syllable}"
        unsure.append(f"{number}0{syllable}")
        return m.group(0)

    return ZERO_RE.sub(repl, text), fixed, unsure


def _local_text(lay, box: tuple[int, int, int, int]) -> str:
    """쪽에서 문제 영역의 글줄을 로컬 글자 인식으로 읽은 것(수식은 믿을 수 없다)."""
    from . import layout, ocr
    crops = layout.line_crops(lay, *box, max_lines=60)
    return " ".join(t for t, s in ocr.read_crops(crops) if s >= 0.5)


def _page_rect(meta: dict, box: tuple[float, float, float, float]) -> tuple[list[float], list[float]] | None:
    """모델이 적은 범위(0~1000)를 쪽 위치(pt)로 바꾼다. (쪽 위의 범위, 그 범위가 속한 조각의 범위)를 돌려준다."""
    W, H = meta["size"]
    x0, x1 = sorted((box[0] / 1000 * W, box[2] / 1000 * W))
    y0, y1 = sorted((box[1] / 1000 * H, box[3] / 1000 * H))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    mid = (y0 + y1) / 2
    seg = min(meta["segments"], key=lambda s: 0 if s["y"] <= mid <= s["y"] + s["h"] else min(abs(mid - s["y"]), abs(mid - s["y"] - s["h"])))
    k = 72 / meta["dpi"]
    rx0, ry0, rx1, ry1 = seg["rect"]
    y0, y1 = max(y0, seg["y"]), min(y1, seg["y"] + seg["h"])
    return ([min(rx1, rx0 + x0 * k), ry0 + (y0 - seg["y"]) * k, min(rx1, rx0 + x1 * k), ry0 + (y1 - seg["y"]) * k],
            seg["rect"])


def resolve(folder: Path, item: dict, idx, answer: str) -> dict:
    """모델의 답을 문서에 넣을 글로 바꾼다: 그림 자리의 범위를 잉크에 맞춰 자르고 이름을 붙인다."""
    import cv2

    from . import figures, layout
    from .pipeline import MAX_FIGURE_MM, MM_PER_PT, _scan_clip
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    page = item["page"] - 1
    text = tidy(answer)
    warnings: list[str] = []
    fixes: list[str] = []
    found: list[tuple[int, int, int, int]] = []      # 넣기로 한 그림들(쪽 기준 픽셀)
    lines = []
    with LAYOUT_LOCK:
        lay, s = idx.layout(page), idx.scale(page)
        badges = [(t.x0, t.y0, t.x1, t.y1) for t in layout.badge_tokens(lay) + layout.fill_tokens(lay)]
        if any((ord(m.group(3)) - 0xAC00) // 588 == 11 for m in ZERO_RE.finditer(text)):
            local = " ".join(_local_text(lay, tuple(int(round(v * s)) for v in seg["rect"])) for seg in meta["segments"])
            text, fixes, unsure = fix_zeros(text, local)
            warnings += [f"'{u}'의 수를 확인하세요(0이 잘못 들어갔을 수 있습니다)" for u in unsure]
        for line in text.split("\n"):
            m = FIGURE_RE.search(line)
            if not m:
                lines.append(line)
                continue
            where = _page_rect(meta, tuple(float(v) for v in m.groups()))
            rect = None
            if where:
                want, part = where
                rect = figures.snap(lay, tuple(int(round(v * s)) for v in part), tuple(int(round(v * s)) for v in want),
                                    badges)
            if rect is None:
                warnings.append("모델이 그림으로 넣으려던 자리에서 그림을 찾지 못해 넣지 않았습니다")
                continue
            # 같은 그림을 두 번 짚었거나 한 장면을 나눠 짚었으면 하나로 합친다
            for k, o in enumerate(found):
                gap = lay.em
                if rect[0] < o[2] + gap and rect[2] > o[0] - gap and rect[1] < o[3] + gap and rect[3] > o[1] - gap:
                    found[k] = (min(o[0], rect[0]), min(o[1], rect[1]), max(o[2], rect[2]), max(o[3], rect[3]))
                    break
            else:
                found.append(rect)
                lines.append(f"![](figure:F{len(found)})")
    figs = []
    for k, r in enumerate(found, 1):
        rect_pt = [r[0] / s, r[1] / s, r[2] / s, r[3] / s]
        ok, buf = cv2.imencode(".png", _scan_clip(idx, page, rect_pt))
        data = buf.tobytes()
        name = f"fig-{hashlib.sha1(data).hexdigest()[:10]}.png"
        if not (folder / name).is_file():
            _write(folder / name, data)
        w_mm, h_mm = (rect_pt[2] - rect_pt[0]) * MM_PER_PT, (rect_pt[3] - rect_pt[1]) * MM_PER_PT
        shrink = min(1.0, MAX_FIGURE_MM[0] / w_mm, MAX_FIGURE_MM[1] / h_mm)
        figs.append({"name": f"F{k}", "file": name, "width_mm": round(w_mm * shrink, 1),
                     "height_mm": round(h_mm * shrink, 1), "rect": [round(v, 1) for v in rect_pt]})
    text = "\n".join(lines).strip()
    parsed = transcript.parse(text, figures={f["name"] for f in figs})
    # warnings는 여기서만 알 수 있는 것(그림 자리, 수 확인)이다. 글 자체의 주의는 검사(check)가 다시 찾는다
    return {"text": text, "figures": figs, "errors": parsed.errors, "warnings": warnings, "fixes": fixes}


def read(folder: Path, item: dict, idx, reader, deadline: float | None = None) -> dict:
    """모델에게 문제 그림을 보내 옮겨 적게 하고 결과를 저장한다. 검사에 걸리면 오류를 알려 한 번 더 시킨다."""
    started = time.time()
    image = (folder / "clean.png").read_bytes()
    answer = reader.ask(PROMPT, image, deadline)
    tries = 1
    if not answer.strip():
        # 버릴 것(번호, 꼬리표)을 빼니 남는 것이 없다는 뜻이다. 다시 시키면 버릴 것을 억지로 적어 온다
        res = {"text": "", "figures": [], "warnings": [], "fixes": [],
               "errors": ["옮길 내용이 없습니다(문제 번호나 꼬리표만 있는 영역으로 보입니다)"]}
    else:
        res = resolve(folder, item, idx, answer)
    if res["errors"] and answer.strip():
        tries = 2
        again = reader.ask(PROMPT + RETRY.format(answer=answer, errors="\n".join("- " + e for e in res["errors"])),
                           image, deadline)
        second = resolve(folder, item, idx, again)
        if len(second["errors"]) <= len(res["errors"]):
            res, answer = second, again
    state = {"version": VERSION, "local": LOCAL, "blank": BLANK, "status": "error" if res["errors"] else "ok", **res, "answer": answer,
             "tries": tries, "seconds": round(time.time() - started, 1), "model": f"{reader.provider}/{reader.model}",
             "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not res["errors"]:
        state.update(as_reading(item, idx, state))
    _write(folder / "state.json", json.dumps(state, ensure_ascii=False, indent=1))
    return state
