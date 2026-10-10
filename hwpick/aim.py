"""에이전트가 쪽을 보고 문제 영역을 직접 잡을 수 있게 한다: 눈금이 달린 쪽 그림과, 대강 준 범위를 잉크에 맞추는 일.

좌표는 쪽의 왼쪽 위가 (0, 0), 오른쪽 아래가 (1000, 1000)이다(가로·세로를 각각 1000으로 나눈 눈금).
모델이 그림에서 읽어 주는 좌표는 몇 %씩 어긋난다. 그래서 받은 범위를 그대로 쓰지 않고, 글줄을 자르지 않는
자리로 가장자리를 옮긴 뒤 잉크에 맞춰 줄인다. 이미 있는 박스와 조금 겹치면 그 박스의 가장자리까지 물린다.
"""
from __future__ import annotations

import re

import cv2
import numpy as np

from .layout import Layout

UNIT = 1000
SHEET_LONG = 1400        # 에이전트에게 보여 줄 쪽 그림의 긴 변(픽셀). 글자가 읽히면서 값싸게 볼 수 있는 크기
MARGIN = (46, 26)        # 눈금을 적는 여백(왼쪽, 위)
MAX_FREE = 12            # 박스 없는 덩어리는 이만큼만 알려 준다
CLASH = 0.3              # 이미 있는 박스와 이 비율 넘게 겹치면 물리지 않고 겹친다고 알린다
COVER = 0.6              # 준 범위가 이미 있는 박스를 이만큼 넘게 덮으면, 그 박스를 덮으려 한 것으로 본다

Rect = tuple[int, int, int, int]


def to_px(box, shape) -> Rect:
    """0~1000 눈금의 범위를 쪽 그림의 픽셀로."""
    H, W = shape[:2]
    x0, y0, x1, y1 = box
    return (int(round(min(x0, x1) * W / UNIT)), int(round(min(y0, y1) * H / UNIT)),
            int(round(max(x0, x1) * W / UNIT)), int(round(max(y0, y1) * H / UNIT)))


def to_unit(rect, shape) -> list[int]:
    H, W = shape[:2]
    return [int(round(rect[0] * UNIT / W)), int(round(rect[1] * UNIT / H)),
            int(round(rect[2] * UNIT / W)), int(round(rect[3] * UNIT / H))]


def mask(lay: Layout) -> np.ndarray:
    """범위를 정할 때 보는 잉크. 문제의 것이 아닌 쪽 장식은 뺀다: 쪽을 위아래로 가르는 긴 세로줄(단 구분선),
    종이 끝에 닿은 색 띠·탭(단원 표시, 머리말 띠), 스캔 가장자리의 그림자, 먼지 같은 점."""
    m = lay.ink.copy()
    H, W = m.shape
    em = lay.em
    # 점선도 잡히게 세로로 이어 붙인 뒤, 쪽 높이의 3할 넘게 이어지면서 가는 것만 고른다(글줄이 이어 붙은 것은 굵다)
    joined = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(em) | 1)))
    tall = cv2.morphologyEx(joined, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(0.3 * H))))
    thick = cv2.morphologyEx(tall, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(0.6 * em)), 1)))
    m[cv2.dilate(tall & (1 - thick), np.ones((1, 7), np.uint8)) > 0] = 0
    # 스캔이 조금 기울면 실선도 한 칸에 길게 이어지지 않는다. 괘선 조각들이 이어져 길고 가는 것도 같은 것으로 본다
    n, labels, stats, _ = cv2.connectedComponentsWithStats(lay.rules_v, connectivity=8)
    for i in range(1, n):
        x, y, w, h = (int(v) for v in stats[i][:4])
        if h >= 0.3 * H and w <= em:
            m[max(0, y - int(2 * em)):y + h + int(2 * em), max(0, x - 3):x + w + 3] = 0     # 선의 양 끝 꼬리까지
    # 종이 끝에 닿은 것 가운데 작은 것(탭)과 납작한 것(머리말 띠). 쪽을 두른 큰 테두리는 건드리지 않는다
    n, labels, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(lay.ink, np.ones((3, 3), np.uint8)), connectivity=8)
    near = max(3, int(0.01 * max(H, W)))
    for i in range(1, n):
        x, y, w, h = (int(v) for v in stats[i][:4])
        if (x <= near or y <= near or x + w >= W - near or y + h >= H - near) and \
                ((w <= 0.12 * W and h <= 0.35 * H) or h <= 0.06 * H):
            m[y:y + h, x:x + w][labels[y:y + h, x:x + w] == i] = 0
    # 먼지: 풀이 칸으로 비워 둔 곳의 점 하나가 박스를 늘이지 않게. 그림의 점(점 여러 개로 된 그림)은 이보다 커서 남는다
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    small = stats[:, cv2.CC_STAT_AREA] < 0.02 * em * em
    small[0] = False
    lone = small[labels] & (cv2.blur(m.astype(np.float32), (int(3 * em) | 1, int(3 * em) | 1)) * (int(3 * em) | 1) ** 2 < 0.1 * em * em)
    m[lone] = 0
    edge = max(4, int(0.5 * em))
    m[:, :edge] = 0
    m[:, -edge:] = 0
    m[:edge, :] = 0
    m[-edge:, :] = 0
    return m


def _slices(m: np.ndarray, rect: Rect, em: float) -> list[Rect]:
    """덩어리를 가로 빈틈에서 위아래 조각으로 나눈다(제목 띠 / 문제 글 / 선택지 줄). 줄 간격이 넓은 글이 줄마다
    나뉘지 않게, 조각이 여섯을 넘으면 더 넓은 빈틈에서만 나눈다."""
    x0, y0, x1, y1 = rect
    rows = m[y0:y1, x0:x1].any(axis=1)
    out: list[Rect] = []
    for gap in (1.0, 1.3, 1.7):
        out, start, blank = [], None, 0
        for i, v in enumerate(list(rows) + [False] * int(2 * em + 2)):
            if v:
                start, blank = (i if start is None else start), 0
            elif start is not None:
                blank += 1
                if blank > gap * em:
                    xs = np.nonzero(m[y0 + start:y0 + i - blank + 1, x0:x1].any(axis=0))[0]
                    out.append((x0 + int(xs.min()), y0 + start, x0 + int(xs.max()) + 1, y0 + i - blank + 1))
                    start = None
        if len(out) <= 6:
            break
    return out


def slices(lay: Layout, rect: Rect) -> list[Rect]:
    """범위 안의 글을 위아래 조각으로 나눈 것."""
    return _slices(mask(lay), rect, lay.em)


def free_regions(lay: Layout, boxes: list[Rect]) -> list[tuple[Rect, list[Rect]]]:
    """박스가 덮지 않은 글·그림 덩어리(위에서 아래로, 왼쪽 단 먼저)와, 덩어리마다 그것을 위아래로 나눈 조각들.

    빠진 문제는 대개 이 덩어리 가운데 하나다. 덩어리에 제목이 붙어 있으면 조각으로 골라낼 수 있다.
    모델은 좌표를 읽는 것보다 표시된 것 가운데서 고르는 것을 훨씬 잘한다(재 보니 좌표는 7번에 4번 어긋났고 고르기는 다 맞았다).
    """
    m = mask(lay)
    pad = int(0.3 * lay.em)
    for x0, y0, x1, y1 in boxes:
        m[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad] = 0
    return _regions(m, lay.em)[:MAX_FREE]


def _regions(m: np.ndarray, em: float) -> list[tuple[Rect, list[Rect]]]:
    """잉크 지도 m의 덩어리들과 그 조각들."""
    H, W = m.shape
    grown = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_RECT, (int(1.5 * em) | 1, int(1.6 * em) | 1)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(grown, connectivity=8)
    parts = []
    for i in range(1, n):
        x, y, w, h = (int(v) for v in stats[i][:4])
        ys, xs = np.nonzero(m[y:y + h, x:x + w] & (labels[y:y + h, x:x + w] == i).astype(np.uint8))
        if len(xs) >= 0.3 * em * em:
            parts.append([x + int(xs.min()), y + int(ys.min()), x + int(xs.max()) + 1, y + int(ys.max()) + 1, len(xs)])
    # 번지는 범위 밖이지만 같은 문제인 조각을 붙인다: 오른쪽으로 붙인 마지막 줄('(단, k는 상수이다.)')처럼 바로 아래에
    # 있는 것, 그리고 띄엄띄엄 놓여 저희끼리 이어지지 않는 선택지('① 1/2   ② 1   ③ 3/2')처럼 조금 아래의 작은 것
    def together(a: list, b: list) -> bool:
        if min(a[2], b[2]) - max(a[0], b[0]) < 0.3 * min(a[2] - a[0], b[2] - b[0]):
            return False
        gap = max(a[1], b[1]) - min(a[3], b[3])
        low = a if a[1] > b[1] else b
        return gap <= 1.2 * em or (gap <= 2.2 * em and low[2] - low[0] < 6 * em and low[3] - low[1] < 3 * em)

    merged = True
    while merged:
        merged = False
        for a in parts:
            for b in parts:
                if a is not b and together(a, b):
                    a[:] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]), a[4] + b[4]]
                    parts.remove(b)
                    merged = True
                    break
            if merged:
                break
    out = []
    for x0, y0, x1, y1, ink in parts:
        middle = (y0 + y1) / 2
        if ink < 1.5 * em * em or x1 - x0 < 6 * em or y1 - y0 < 1.2 * em or middle < 0.04 * H or middle > 0.92 * H:
            continue                                 # 쪽 번호·머리말 띠와 낱말 몇 개짜리 조각은 문제가 아니다
        out.append((x0, y0, x1, y1))
    out.sort(key=lambda r: (r[0] > 0.45 * W, r[1]))
    return [(r, _slices(m, r, em)) for r in out]


def _fits(c: Rect, rect: Rect, em: float) -> bool:
    """c가 준 범위에서 크게 벗어나지 않는가(가로 3글자, 세로 2줄쯤까지는 좌표가 어긋난 것으로 본다)."""
    return c[0] >= rect[0] - 3 * em and c[2] <= rect[2] + 3 * em and c[1] >= rect[1] - 2 * em and c[3] <= rect[3] + 2 * em


def _match(m: np.ndarray, rect: Rect, regions: list[tuple[Rect, list[Rect]]], em: float) -> Rect | None:
    """준 범위와 가장 잘 겹치는 덩어리(또는 한 덩어리 안에서 이어진 조각들). 마땅한 것이 없으면 None.

    준 범위보다 훨씬 큰 것(위의 제목까지 든 덩어리)이나, 준 범위에 든 잉크를 많이 놓치는 것(선택지가 빠진 조각)은
    고르지 않는다. 좌표를 준 쪽이 일부러 넣거나 뺀 것을 뒤집게 되기 때문이다.
    """
    def ink(r: Rect) -> int:
        return int(m[r[1]:r[3], r[0]:r[2]].sum())

    best, score, whole = None, 0.45, max(1, ink(rect))
    for r, parts in regions:
        runs = [parts[i:j] for i in range(len(parts)) for j in range(i + 1, len(parts) + 1)] if len(parts) > 1 else []
        for c in [r] + [(min(p[0] for p in run), run[0][1], max(p[2] for p in run), run[-1][3]) for run in runs]:
            iw, ih = min(rect[2], c[2]) - max(rect[0], c[0]), min(rect[3], c[3]) - max(rect[1], c[1])
            if iw <= 0 or ih <= 0 or not _fits(c, rect, em):
                continue
            iou = iw * ih / ((rect[2] - rect[0]) * (rect[3] - rect[1]) + (c[2] - c[0]) * (c[3] - c[1]) - iw * ih)
            both = (max(rect[0], c[0]), max(rect[1], c[1]), min(rect[2], c[2]), min(rect[3], c[3]))
            if iou > score and whole - ink(both) <= 0.25 * em * em:      # 준 범위 안에 글자 하나만큼도 남기지 않는 것만
                best, score = c, iou
    return best


def _clear(line: np.ndarray, at: int, reach: int, run: int, prefer: int) -> int | None:
    """line[at]이 잉크면, 가장 가까운 빈 자리(run칸 이상 빈 곳)를 찾는다. 양쪽이 같은 거리면 prefer 쪽. 없으면 None."""
    n = len(line)
    at = max(0, min(n - 1, at))

    def empty(i: int) -> bool:
        return 0 <= i and i + run <= n and not line[i:i + run].any()

    if not line[at]:
        return at
    for d in range(1, reach + 1):
        for step in (prefer, -prefer):
            i = at + step * d
            if empty(i if step > 0 else i - run + 1):
                return i
    return None


def _shrink(m: np.ndarray, rect: Rect, pad: int) -> Rect | None:
    x0, y0, x1, y1 = rect
    ys, xs = np.nonzero(m[y0:y1, x0:x1])
    if len(xs) == 0:
        return None
    H, W = m.shape
    return (max(0, x0 + int(xs.min()) - pad), max(0, y0 + int(ys.min()) - pad),
            min(W, x0 + int(xs.max()) + 1 + pad), min(H, y0 + int(ys.max()) + 1 + pad))


def _blocks(m: np.ndarray, em: float) -> tuple[np.ndarray, np.ndarray]:
    """글줄이 붙어 한 덩어리가 된 것(문단, 그림, 선택지 줄)의 번호 지도와 덩어리별 잉크 양."""
    grown = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_RECT, (int(1.5 * em) | 1, int(0.9 * em) | 1)))
    n, labels = cv2.connectedComponents(grown, connectivity=8)
    labels[m == 0] = 0
    return labels, np.bincount(labels.ravel(), minlength=n)


def snap(lay: Layout, rect: Rect, others: list[tuple[Rect, str]] | None = None,
         force: bool = False) -> tuple[Rect | None, list[str], list[str]]:
    """대강 준 범위(픽셀)를 잉크에 맞춘다. (맞춘 범위, 알릴 점, 겹치는 박스의 이름)을 돌려준다. 담을 것이 없으면 범위가 None.

    others는 이미 있는 박스들(범위, 이름)이다. 그 안의 잉크는 다른 문제의 것이라 보지 않는다(force면 본다).

    모델이 준 좌표는 한두 줄씩 어긋난다(재 보니 가벼운 모델은 7번에 4번). 그래서 좌표를 곧이곧대로 쓰지 않는다.
    ① 쪽 그림에 표시해 준 덩어리나 이어진 조각들 가운데 준 범위와 잘 겹치는 것이 있으면 그것으로 한다.
    ② 없으면 작은 덩어리(문단, 그림, 선택지 줄) 단위로 고른다: 절반 넘게 범위에 든 것은 통째로 넣고 아니면 뺀다.
    ③ 글이 빽빽해 덩어리가 범위보다 훨씬 큰 곳에서만 줄 단위로 자른다.
    """
    m = mask(lay)
    H, W = m.shape
    em = lay.em
    pad = int(0.35 * em)
    x0, y0, x1, y1 = max(0, rect[0]), max(0, rect[1]), min(W, rect[2]), min(H, rect[3])
    if x1 - x0 < em or y1 - y0 < 0.5 * em:
        return None, [], []
    others = others or []
    covered = [name for (a0, b0, a1, b1), name in others
               if max(0, min(x1, a1) - max(x0, a0)) * max(0, min(y1, b1) - max(y0, b0)) >= COVER * (a1 - a0) * (b1 - b0)]
    if covered and not force:
        return None, [], covered
    if not force:
        for (a0, b0, a1, b1), _ in others:
            m[max(0, b0 - pad):b1 + pad, max(0, a0 - pad):a1 + pad] = 0
    notes: list[str] = []
    labels, total = _blocks(m, em)
    inside = np.bincount(labels[y0:y1, x0:x1].ravel(), minlength=len(total))
    inside[0] = 0
    if not inside.any():
        return None, [], []
    ids = [int(i) for i in np.nonzero(inside)[0]]
    chosen = [i for i in ids if 2 * inside[i] >= total[i]]
    given = (x0, y0, x1, y1)
    marked = _match(m, given, _regions(m, em), em)
    if marked is not None:
        x0, y0, x1, y1 = marked
    elif sum(inside[i] for i in chosen) >= 0.6 * inside.sum():
        # 고른 덩어리가 준 범위에서 크게 벗어나지 않으면 통째로, 벗어나면 준 범위 안의 것만(아래의 줄 단위가 마저 다듬는다)
        a0, b0, a1, b1 = max(0, x0 - int(4 * em)), max(0, y0 - int(3 * em)), min(W, x1 + int(4 * em)), min(H, y1 + int(3 * em))
        near = labels[b0:b1, a0:a1]
        boxes = []
        for i in chosen:
            ys, xs = np.nonzero(near == i)
            c = (a0 + int(xs.min()), b0 + int(ys.min()), a0 + int(xs.max()) + 1, b0 + int(ys.max()) + 1)
            if len(xs) < total[i] or not _fits(c, given, em):
                c = (max(c[0], given[0]), max(c[1], given[1]), min(c[2], given[2]), min(c[3], given[3]))
            boxes.append(c)
        # 절반은 안 되지만 꽤 걸친 덩어리(문제 번호가 위의 제목과 한 덩어리가 된 경우 등)는 준 범위 안의 것만 넣는다
        for i in ids:
            if i not in chosen and 10 * inside[i] >= 3 * total[i]:
                ys, xs = np.nonzero(labels[given[1]:given[3], given[0]:given[2]] == i)
                boxes.append((given[0] + int(xs.min()), given[1] + int(ys.min()), given[0] + int(xs.max()) + 1, given[1] + int(ys.max()) + 1))
            elif i not in chosen and inside[i] >= max(0.15 * total[i], em * em):
                notes.append("범위의 가장자리에 조금 걸친 덩어리는 넣지 않았습니다")
        notes = sorted(set(notes))
        x0, y0, x1, y1 = min(c[0] for c in boxes), min(c[1] for c in boxes), max(c[2] for c in boxes), max(c[3] for c in boxes)
    run = max(2, int(0.2 * em))
    # 위아래: 가장자리가 글줄을 지나가면 가장 가까운 줄 사이로 옮긴다(같은 거리면 넓히는 쪽)
    rows = m[:, x0:x1].any(axis=1)
    top, bottom = _clear(rows, y0, int(1.2 * em), run, -1), _clear(rows, y1 - 1, int(1.2 * em), run, 1)
    if top is None:
        notes.append("위쪽 가장자리가 글자나 그림을 지나갑니다")
    elif top > y0:
        notes.append("위쪽 가장자리에 걸린 글줄은 뺐습니다")
    if bottom is None:
        notes.append("아래쪽 가장자리가 글자나 그림을 지나갑니다")
    elif bottom < y1 - 1:
        notes.append("아래쪽 가장자리에 걸린 글줄은 뺐습니다")
    y0, y1 = (y0 if top is None else top), (y1 if bottom is None else bottom + 1)
    # 양옆: 글을 자르고 있으면 가장 가까운 세로 빈 곳(단 사이, 여백)으로 옮긴다. 글줄 가운데에는 세로로 내리 빈 곳이
    # 없으므로, 자기 글을 자른 가장자리는 바깥으로 나가고 옆 단을 조금 침범한 가장자리는 안으로 들어온다
    cols = m[y0:y1].any(axis=0)
    gap = max(3, int(0.6 * em))
    for side in (-1, 1):
        at = x0 if side < 0 else x1 - 1
        found = _clear(cols, at, int(6 * em), gap, side)
        if found is None:
            notes.append(("왼쪽" if side < 0 else "오른쪽") + " 가장자리가 글자나 그림을 지나갑니다")
        elif side < 0:
            x0 = found
        else:
            x1 = found + 1
    got = _shrink(m, (x0, y0, x1, y1), pad)
    if got is None:
        return None, [], []
    # 이미 있는 박스와 조금 겹치면 가장 적게 잃는 쪽으로 물린다. 많이 겹치면 그대로 두고 이름을 알린다
    clash: list[str] = []
    for (a0, b0, a1, b1), name in others:
        x0, y0, x1, y1 = got
        iw, ih = min(x1, a1) - max(x0, a0), min(y1, b1) - max(y0, b0)
        if iw <= 0 or ih <= 0:
            continue
        if iw * ih >= CLASH * min((x1 - x0) * (y1 - y0), (a1 - a0) * (b1 - b0)):
            clash.append(name)
            continue
        cuts = [((b1 - y0) * (x1 - x0), (x0, b1, x1, y1)), ((y1 - b0) * (x1 - x0), (x0, y0, x1, b0)),
                ((a1 - x0) * (y1 - y0), (a1, y0, x1, y1)), ((x1 - a0) * (y1 - y0), (x0, y0, a0, y1))]
        cuts = [(loss, r) for loss, r in cuts if r[2] - r[0] > em and r[3] - r[1] > 0.5 * em]
        if not cuts:
            clash.append(name)
            continue
        got = _shrink(m, min(cuts)[1], pad) or got
    return got, notes, clash


def closeup(lay: Layout, rect: Rect, others: list[Rect] | None = None) -> np.ndarray:
    """잡힌 영역을 둘레와 함께 크게 보여 준다: 무엇이 잘렸고 무엇이 섞였는지 보이게. 눈금은 쪽 전체 기준의 좌표다.

    잡힌 영역은 빨간 테두리, 둘레는 옅게, 이미 있는 다른 박스는 파란 테두리.
    """
    H, W = lay.img.shape[:2]
    em = lay.em
    x0, y0 = max(0, rect[0] - int(3 * em)), max(0, rect[1] - int(5 * em))
    x1, y1 = min(W, rect[2] + int(3 * em)), min(H, rect[3] + int(5 * em))
    view = cv2.addWeighted(lay.img[y0:y1, x0:x1], 0.5, np.full((y1 - y0, x1 - x0, 3), 255, np.uint8), 0.5, 0)
    view[rect[1] - y0:rect[3] - y0, rect[0] - x0:rect[2] - x0] = lay.img[rect[1]:rect[3], rect[0]:rect[2]]
    for a0, b0, a1, b1 in others or []:
        cv2.rectangle(view, (a0 - x0, b0 - y0), (a1 - x0, b1 - y0), (200, 80, 0), 2)
    cv2.rectangle(view, (rect[0] - x0, rect[1] - y0), (rect[2] - x0, rect[3] - y0), (0, 0, 230), 3)
    f = min(1.0, 1100 / max(view.shape[:2]))
    if f < 1.0:
        view = cv2.resize(view, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    h, w = view.shape[:2]
    ml, mt = MARGIN
    out = np.full((h + mt, w + ml, 3), 255, np.uint8)
    out[mt:, ml:] = view
    for axis, (lo, hi, size, full) in enumerate(((x0, x1, w, W), (y0, y1, h, H))):
        a, b = lo * UNIT / full, hi * UNIT / full
        step = next(s for s in (5, 10, 20, 50, 100) if (b - a) / s <= 14)
        for u in range(int(a // step + 1) * step, int(b) + 1, step):
            at = int(round((u - a) / (b - a) * size))
            text = str(u)
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            if axis == 0:
                cv2.line(out, (ml + at, mt - 5), (ml + at, mt), (0, 0, 0), 1)
                cv2.putText(out, text, (min(ml + at - tw // 2, w + ml - tw), mt - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
            else:
                cv2.line(out, (ml - 4, mt + at), (ml, mt + at), (0, 0, 0), 1)
                cv2.putText(out, text, (ml - tw - 5, min(mt + at + th // 2, h + mt - 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
    return out


def head_digits(lay: Layout, rect: Rect) -> list[str]:
    """박스 첫 줄 머리에서 읽히는 숫자들(문제 번호를 확인하려는 것). 읽지 못하면 빈 목록."""
    from . import layout, ocr
    crops = layout.line_crops(lay, *rect, max_lines=1)
    text = " ".join(t for t, s in ocr.read_crops(crops[:1]) if s >= 0.5)
    return re.findall(r"\d+", text)


def _tag(img: np.ndarray, text: str, corner: tuple[int, int], color: tuple[int, int, int], size: float = 0.55) -> None:
    """이름표. corner는 이름표의 오른쪽 위 구석이다(박스에는 오른쪽 위에 달아 왼쪽 위의 문제 번호를 가리지 않는다)."""
    thick = 2 if size > 0.5 else 1
    (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, size, thick)
    x1, y0 = corner
    x0 = max(0, x1 - w - 8)
    cv2.rectangle(img, (x0, y0), (x0 + w + 8, y0 + h + 8), color, -1)
    cv2.putText(img, text, (x0 + 4, y0 + h + 3), cv2.FONT_HERSHEY_SIMPLEX, size, (255, 255, 255), thick, cv2.LINE_AA)


def _dashed(img: np.ndarray, rect: Rect, color: tuple[int, int, int], dash: int = 12) -> None:
    x0, y0, x1, y1 = rect
    for a in range(x0, x1, 2 * dash):
        cv2.line(img, (a, y0), (min(a + dash, x1), y0), color, 2)
        cv2.line(img, (a, y1), (min(a + dash, x1), y1), color, 2)
    for b in range(y0, y1, 2 * dash):
        cv2.line(img, (x0, b), (x0, min(b + dash, y1)), color, 2)
        cv2.line(img, (x1, b), (x1, min(b + dash, y1)), color, 2)


def sheet(lay: Layout, boxes: list[tuple[Rect, str, str]], free: list[tuple[Rect, str, list[Rect]]]) -> np.ndarray:
    """에이전트가 볼 쪽 그림. 가장자리에 0~1000 눈금, 100마다 옅은 선.

    boxes는 (범위, 이름표, 종류)이고 종류는 found(찾아 둔 박스, 파랑)·picked(목록에 담긴 것, 초록)·agent(직접 잡은 것, 보라).
    free는 박스 없는 덩어리(범위, 글자, 조각들)다. 덩어리는 주황 점선과 오른쪽 위의 글자, 조각은 가는 선과 왼쪽의 'A1'.
    """
    H, W = lay.img.shape[:2]
    f = SHEET_LONG / max(H, W)
    w, h = int(round(W * f)), int(round(H * f))
    page = cv2.resize(lay.img, (w, h), interpolation=cv2.INTER_AREA)
    lines = page.copy()
    for k in range(1, 10):
        cv2.line(lines, (w * k // 10, 0), (w * k // 10, h), (230, 150, 60), 1)
        cv2.line(lines, (0, h * k // 10), (w, h * k // 10), (230, 150, 60), 1)
    page = cv2.addWeighted(lines, 0.4, page, 0.6, 0)
    colors = {"found": (200, 80, 0), "picked": (40, 150, 0), "agent": (170, 30, 150)}
    for rect, name, kind in boxes:
        r = [int(round(v * f)) for v in rect]
        cv2.rectangle(page, (r[0], r[1]), (r[2], r[3]), colors[kind], 2)
        _tag(page, name, (r[2], r[1]), colors[kind])
    for rect, name, parts in free:
        r = [int(round(v * f)) for v in rect]
        _dashed(page, r, (0, 110, 240))
        _tag(page, name, (r[2], r[1]), (0, 110, 240))
        for k, part in enumerate(parts if len(parts) > 1 else [], 1):
            q = [int(round(v * f)) for v in part]
            cv2.rectangle(page, (r[0] + 3, q[1]), (r[2] - 3, q[3]), (60, 170, 255), 1)
            _tag(page, f"{name}{k}", (max(r[0] - 2, 30), q[1]), (60, 150, 240), 0.42)
    ml, mt = MARGIN
    out = np.full((h + mt, w + ml, 3), 255, np.uint8)
    out[mt:, ml:] = page
    for k in range(0, 11):
        x, y = ml + w * k // 10, mt + h * k // 10
        text = str(k * 100)
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        cv2.putText(out, text, (min(max(ml, x - tw // 2), w + ml - tw), mt - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.putText(out, text, (ml - tw - 5, min(max(mt + th, y + th // 2), h + mt - 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.line(out, (x, mt - 5), (x, mt), (0, 0, 0), 1)
        cv2.line(out, (ml - 4, y), (ml, y), (0, 0, 0), 1)
    return out
