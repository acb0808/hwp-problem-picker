"""문제 영역 안에서 그림(그래프·도형·표)을 찾는다. 그림은 OCR하지 않고 잘라서 그대로 붙인다.

완벽할 필요는 없다. 여기서 찾은 후보에 이름표(그림A, 그림B…)를 붙여 전사 담당 모델에게 보여 주고,
모델이 실제로 그림인 것만 본문의 제자리에 표시한다. 후보가 빠졌으면 모델이 알려 준다.
"""
from __future__ import annotations

import cv2
import numpy as np

from .layout import Layout

SEED_MIN_EM = 1.8      # 그림의 뼈대(이어진 획)는 가로·세로 모두 이보다 크다
LABEL_REACH_EM = 0.9   # 뼈대에서 이 거리 안의 짧은 글자는 그림의 라벨로 본다
LABEL_MAX_EM = 5.0     # 라벨로 볼 수 있는 글자 덩어리 묶음의 최대 폭
ROW_LABEL_MAX_EM = 12.0  # 그림의 뼈대와 겹쳐 있으면 이 폭까지의 글줄도 그림의 이름표로 본다(직선의 식 `ax+by+c=0` 따위)


def _hollow(comp: np.ndarray, em: float) -> bool:
    """잉크가 네 변에만 있는 빈 사각 틀인가."""
    h, w = comp.shape
    m = max(2, int(0.3 * em))
    if h <= 3 * m or w <= 3 * m:
        return False
    total = int(comp.sum())
    if total == 0 or int(comp[m:h - m, m:w - m].sum()) > 0.12 * total:
        return False
    sides = [comp[:m].any(axis=0).mean(), comp[h - m:].any(axis=0).mean(),
             comp[:, :m].any(axis=1).mean(), comp[:, w - m:].any(axis=1).mean()]
    return min(sides) > 0.6


def _open_frame(comp: np.ndarray, em: float) -> bool:
    """잉크가 가장자리의 두 변 이상에만 있고 안쪽은 빈 토막인가(둥근 글 상자의 테두리가 옅어 끊긴 조각).
    이런 조각을 그림으로 잡으면 조건 상자·증명 상자의 한 귀퉁이가 그림 후보가 된다."""
    h, w = comp.shape
    m = max(2, int(0.3 * em))
    if h <= 3 * m or w <= 3 * m:
        return False
    total = int(comp.sum())
    if total == 0 or int(comp[m:h - m, m:w - m].sum()) > 0.12 * total:
        return False
    sides = [comp[:m].any(axis=0).mean(), comp[h - m:].any(axis=0).mean(),
             comp[:, :m].any(axis=1).mean(), comp[:, w - m:].any(axis=1).mean()]
    return sum(s > 0.6 for s in sides) >= 2


def _kind(comp: np.ndarray, rules: np.ndarray, em: float, has_text: bool, open_frames: bool = False) -> str:
    """이어진 획 하나가 무엇인가: 'box'(글을 담은 보기·조건 상자), 'blank'(답 쓰는 빈칸), 'figure'(그림).

    상자는 사각 틀이거나, 윗변이 '보 기' 이름표로 끊겨 있어도 거의 직선으로만 되어 있다. 직선뿐이어도
    안쪽에 칸을 나누는 선이 있으면 표이고(표는 그림으로 붙인다), 글도 없이 크기만 하면 도형(직사각형)이다.
    """
    h, w = comp.shape
    total = int(comp.sum())
    if total == 0:
        return "figure"
    if not _hollow(comp, em):
        if open_frames and _open_frame(comp, em):
            # 글 상자의 테두리가 끊겨 한 토막만 따로 이어진 것(ㄷ·ㄱ 꼴). 그림의 뼈대가 아니다.
            # 그림 후보를 미리 찾을 때(find_figures)만 쓴다. 곡선 없는 좌표축도 이렇게 생겼을 수 있어,
            # 모델이 짚은 자리를 다듬는 snap에는 쓰지 않는다(거기서는 모델이 그림이라고 한 것이다)
            return "box" if has_text else "blank"
        if (comp & rules).sum() < 0.8 * total:
            return "figure"               # 곡선·빗금이 섞였으면 그림이다
        m = max(3, int(0.6 * em))
        top = max(m, int(1.8 * em))       # 위쪽은 이름표가 걸친 선이 있을 수 있어 넉넉히 뺀다
        inner = comp[top:h - m, m:w - m]
        if inner.size and ((inner.sum(axis=0) >= 0.6 * inner.shape[0]).any()
                           or (inner.sum(axis=1) >= 0.6 * inner.shape[1]).any()):
            return "figure"               # 표
    if has_text:
        return "box"
    return "blank" if min(h, w) <= 3.2 * em else "figure"


class _Scan:
    """문제 박스 안의 잉크를 한 번 훑어 둔 것. 좌표는 모두 넓힌 박스의 왼쪽 위(x0, y0) 기준이다."""

    def __init__(self, lay: Layout, box: tuple[int, int, int, int], open_frames: bool = False):
        em = self.em = lay.em
        H0, W0 = lay.ink.shape
        grow = int(0.4 * em)       # 박스 가장자리에 걸친 상자 테두리가 잘리지 않게 조금 넓혀서 본다
        x0, y0 = self.x0, self.y0 = max(0, box[0] - grow), max(0, box[1] - grow)
        x1, y1 = min(W0, box[2] + grow), min(H0, box[3] + grow)
        ink = lay.ink[y0:y1, x0:x1]
        self.shape = ink.shape
        n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
        rules = cv2.dilate((lay.rules_h | lay.rules_v)[y0:y1, x0:x1], np.ones((3, 3), np.uint8))
        words = [(b.x0 - x0, b.y0 - y0, b.x1 - x0, b.y1 - y0) for b in lay.blobs
                 if b.x0 >= x0 and b.x1 <= x1 and b.y0 >= y0 and b.y1 <= y1 and 0.5 * em <= b.h <= 2.2 * em]
        # 이어진 큰 획: (영역, 종류). 종류는 'figure'(그림의 뼈대), 'box'(글을 담은 틀), 'blank'(빈칸)
        self.strokes: list[tuple[list[int], str]] = []
        for i in range(1, n):
            sx, sy, sw, sh, _ = stats[i]
            if min(sw, sh) < SEED_MIN_EM * em:
                continue
            rect = [int(sx), int(sy), int(sx + sw), int(sy + sh)]
            inside = sum(b[2] - b[0] for b in words
                         if b[0] >= sx and b[2] <= sx + sw and b[1] >= sy and b[3] <= sy + sh)
            comp = (labels[sy:sy + sh, sx:sx + sw] == i).astype(np.uint8)
            self.strokes.append((rect, _kind(comp, rules[sy:sy + sh, sx:sx + sw], em, inside >= 0.3 * sw, open_frames)))
        self.pieces = [(int(c[0]), int(c[1]), int(c[0] + c[2]), int(c[1] + c[3])) for c in stats[1:n] if c[4] >= 3]
        # 뼈대 근처의 낱글자(꼭짓점 이름, 길이, 점선 조각)는 그림의 일부다. 어절 덩어리가 아니라 낱글자로 본다:
        # 밑변 아래의 'A … 2 … B'처럼 떨어진 글자들이 한 덩어리로 뭉치거나 글자가 외곽선과 뭉치면 놓치기 때문이다.
        self.glyphs = [p for p in self.pieces if max(p[2] - p[0], p[3] - p[1]) <= 2.2 * em]
        # 같은 줄에서 촘촘히 이어진 글자들은 한 묶음이다. 긴 묶음은 본문 줄이므로 라벨이 아니다
        chains: list[list[tuple[int, int, int, int]]] = []
        for g in sorted(self.glyphs, key=lambda g: g[0]):
            for c in chains:
                last = c[-1]
                if g[0] - last[2] < 0.5 * em \
                        and min(g[3], last[3]) - max(g[1], last[1]) > 0.4 * min(g[3] - g[1], last[3] - last[1]):
                    c.append(g)
                    break
            else:
                chains.append([g])
        near = [(min(g[0] for g in c), min(g[1] for g in c), max(g[2] for g in c), max(g[3] for g in c))
                for c in chains]
        # 낱말 사이 띄어쓰기로 이어지는 묶음들은 한 줄(문장)이다. 줄이 길면 그 낱말들은 본문이다.
        # 글자 높이가 안 되는 조각(점선 토막, 쉼표)은 줄을 잇지 못한다: 점선이 '4 cm' 같은 라벨을 문장으로 만들면 안 된다
        rows: list[list[int]] = []
        row_of: list[int | None] = []
        for r in near:
            if r[3] - r[1] < 0.45 * em:
                row_of.append(None)
                continue
            for k, row in enumerate(rows):
                if 0 <= r[0] - row[2] < 0.8 * em \
                        and min(r[3], row[3]) - max(r[1], row[1]) > 0.4 * min(r[3] - r[1], row[3] - row[1]):
                    row[1], row[2], row[3] = min(row[1], r[1]), r[2], max(row[3], r[3])
                    row_of.append(k)
                    break
            else:
                rows.append(list(r))
                row_of.append(len(rows) - 1)

        def marker(chain) -> bool:
            """'(1)', '(가)'처럼 괄호로 싼 짧은 묶음인가(작은 문제 번호). 괄호는 키가 크고 홀쭉하다."""
            ends = (chain[0], chain[-1])
            return 3 <= len(chain) <= 4 and all(g[3] - g[1] >= 0.7 * em and g[2] - g[0] <= 0.5 * (g[3] - g[1]) for g in ends)

        keep = [k for k, r in enumerate(near) if r[2] - r[0] <= LABEL_MAX_EM * em]
        self.labels = [near[k] for k in keep]
        # 그림에 딸린 글자가 아닌 것: 문장 속 낱말, 작은 문제 번호
        self.not_label = [marker(chains[k]) or (row_of[k] is not None
                                               and rows[row_of[k]][2] - rows[row_of[k]][0] > LABEL_MAX_EM * em) for k in keep]
        self.sentences = [tuple(r) for r in rows if r[2] - r[0] > LABEL_MAX_EM * em]   # 긴 글줄(본문 줄)

    def absorb_labels(self, regions: list[list[int]], strict: bool = False) -> None:
        """영역 근처의 라벨을 영역에 넣는다(제자리에서 넓힌다). strict면 문장 속 낱말과 작은 문제 번호는 넣지 않는다."""
        reach = LABEL_REACH_EM * self.em
        taken = [strict and s for s in self.not_label]
        # 두 번만 넓힌다(외곽선 → 점선 호 → 그 바깥의 길이 숫자). 더 돌리면 옆의 본문 줄까지 번질 수 있다
        for _ in range(2):
            base = [list(r) for r in regions]
            for li, (gx0, gy0, gx1, gy1) in enumerate(self.labels):
                if taken[li]:
                    continue
                for r, o in zip(regions, base):
                    if gx0 < o[2] + reach and gx1 > o[0] - reach and gy0 < o[3] + reach and gy1 > o[1] - reach:
                        r[0], r[1], r[2], r[3] = min(r[0], gx0), min(r[1], gy0), max(r[2], gx1), max(r[3], gy1)
                        taken[li] = True
                        break

    def absorb_rows(self, regions: list[list[int]]) -> None:
        """뼈대의 범위 안으로 들어와 있는 글줄을 영역에 넣는다(제자리에서 넓힌다).

        이름표는 폭 LABEL_MAX_EM까지만 치는데, 직선에 붙은 식(`ax+by+c=0`)은 그보다 조금 길어 본문 줄로 분류되고,
        그러면 그림의 가장자리가 그 식을 반으로 자른다. 본문은 그림을 피해 줄을 바꾸므로 뼈대의 범위와 겹치지 않는다.
        그래서 뼈대와 실제로 겹친 글줄(줄 높이의 절반 넘게, 폭의 4분의 1 넘게)은 그 그림의 이름표다.
        """
        base = [list(r) for r in regions]
        for row in self.sentences:
            width, height = row[2] - row[0], row[3] - row[1]
            if width > ROW_LABEL_MAX_EM * self.em:
                continue
            for r, o in zip(regions, base):
                w, h = min(row[2], o[2]) - max(row[0], o[0]), min(row[3], o[3]) - max(row[1], o[1])
                if h > 0.5 * height and w >= 0.25 * width:
                    r[0], r[1], r[2], r[3] = min(r[0], row[0]), min(r[1], row[1]), max(r[2], row[2]), max(r[3], row[3])
                    break

    def clean_edges(self, regions: list[list[int]]) -> None:
        """그림의 테두리가 글자를 반으로 가르면 안 된다. 걸친 글자는 통째로 넣는다(변마다 조금씩만 넓힌다)."""
        limit = int(1.2 * self.em)
        for m in regions:
            o = list(m)
            for g in self.glyphs:
                w, h = min(o[2], g[2]) - max(o[0], g[0]), min(o[3], g[3]) - max(o[1], g[1])
                inside = g[0] >= o[0] and g[1] >= o[1] and g[2] <= o[2] and g[3] <= o[3]
                if w > 0 and h > 0 and not inside and w * h >= 0.3 * (g[2] - g[0]) * (g[3] - g[1]):
                    m[0], m[1] = max(o[0] - limit, min(m[0], g[0])), max(o[1] - limit, min(m[1], g[1]))
                    m[2], m[3] = min(o[2] + limit, max(m[2], g[2])), min(o[3] + limit, max(m[3], g[3]))

    def to_page(self, regions: list[list[int]]) -> list[tuple[int, int, int, int]]:
        """여백을 조금 두고 쪽 기준 픽셀로 바꾼다."""
        pad = int(0.25 * self.em)
        H, W = self.shape
        return [(self.x0 + max(0, m[0] - pad), self.y0 + max(0, m[1] - pad),
                 self.x0 + min(W, m[2] + pad), self.y0 + min(H, m[3] + pad)) for m in regions]


def find_figures(lay: Layout, box: tuple[int, int, int, int],
                 skip: list[tuple[int, int, int, int]] | None = None) -> list[tuple[int, int, int, int]]:
    """쪽 레이아웃과 문제 박스(픽셀)를 받아 그림 영역들을 읽는 순서로 돌려준다(쪽 기준 픽셀).

    skip은 그림이 아닌 것으로 이미 아는 영역들이다(문제 번호가 든 색 배지 등).
    """
    em = lay.em
    scan = _Scan(lay, box, open_frames=True)
    seeds = [r for r, kind in scan.strokes if kind == "figure"]
    frames = [r for r, kind in scan.strokes if kind == "box"]
    # 틀 안에 뼈대가 있으면 틀째로 그림이고, 글만 있으면 보기 상자라 그림이 아니다
    regions: list[list[int]] = []
    used = [False] * len(seeds)
    for f in frames:
        inside = [k for k, s in enumerate(seeds) if s[0] >= f[0] and s[1] >= f[1] and s[2] <= f[2] and s[3] <= f[3]]
        if inside:
            regions.append(f)
            for k in inside:
                used[k] = True
    regions += [s for k, s in enumerate(seeds) if not used[k]]
    if not regions:
        return []
    scan.absorb_labels(regions)
    scan.absorb_rows(regions)
    # 겹치는 영역은 합친다
    merged: list[list[int]] = []
    for r in sorted(regions, key=lambda r: (r[1], r[0])):
        for m in merged:
            if r[0] < m[2] and r[2] > m[0] and r[1] < m[3] and r[3] > m[1]:
                m[0], m[1], m[2], m[3] = min(m[0], r[0]), min(m[1], r[1]), max(m[2], r[2]), max(m[3], r[3])
                break
        else:
            merged.append(list(r))
    scan.clean_edges(merged)
    out = scan.to_page(merged)

    def overlaps(r, o) -> bool:
        w, h = min(r[2], o[2]) - max(r[0], o[0]), min(r[3], o[3]) - max(r[1], o[1])
        return w > 0 and h > 0 and w * h >= 0.5 * min((r[2] - r[0]) * (r[3] - r[1]), (o[2] - o[0]) * (o[3] - o[1]))

    out = [r for r in out if not any(overlaps(r, o) for o in skip or [])]
    # 읽는 순서: 위에서 아래로, 같은 높이면 왼쪽부터
    out.sort(key=lambda r: (round(r[1] / (2 * em)), r[0]))
    return out


def snap(lay: Layout, box: tuple[int, int, int, int], want: tuple[int, int, int, int],
         skip: list[tuple[int, int, int, int]] | None = None) -> tuple[int, int, int, int] | None:
    """모델이 "여기를 그림으로 넣겠다"고 대충 짚은 영역(want)을 실제 잉크에 맞춘다(모두 쪽 기준 픽셀).

    넣을지 말지는 모델이 정하고, 어디서 자를지는 여기서 정한다. 모델이 짚는 좌표는 그림 크기의 10%쯤
    어긋나는 일이 흔해서, 그대로 자르면 꼭짓점 이름이 잘리거나 옆의 글이 딸려 온다. 짚은 영역에 든 큰 획
    (도형의 뼈대, 표의 틀, 말풍선)을 고르고, 거기에 딸린 라벨까지 넣은 범위를 돌려준다.

    skip은 그림이 아닌 것으로 이미 아는 영역들이다(문제 번호가 든 색 배지 등). 배지와 한 덩어리인 장식 틀은
    모델이 옆으로 넉넉히 짚어도 고르지 않는다.
    """
    scan = _Scan(lay, box)
    em = scan.em
    badges = [(b[0] - scan.x0, b[1] - scan.y0, b[2] - scan.x0, b[3] - scan.y0) for b in skip or []]

    def decorated(r) -> bool:
        for b in badges:
            w, h = min(r[2], b[2]) - max(r[0], b[0]), min(r[3], b[3]) - max(r[1], b[1])
            if w > 0 and h > 0 and w * h >= 0.5 * min((r[2] - r[0]) * (r[3] - r[1]), (b[2] - b[0]) * (b[3] - b[1])):
                return True
        return False
    wx0, wy0, wx1, wy1 = want[0] - scan.x0, want[1] - scan.y0, want[2] - scan.x0, want[3] - scan.y0
    tol = max(em, 0.06 * max(wx1 - wx0, wy1 - wy0))
    wx0, wy0, wx1, wy1 = wx0 - tol, wy0 - tol, wx1 + tol, wy1 + tol

    def share(r) -> float:
        w, h = min(wx1, r[2]) - max(wx0, r[0]), min(wy1, r[3]) - max(wy0, r[1])
        return max(0.0, w) * max(0.0, h) / max(1.0, (r[2] - r[0]) * (r[3] - r[1]))

    picked = [(list(r), kind) for r, kind in scan.strokes if share(r) >= 0.5]
    # 색을 칠한 도형(직육면체의 면)도 배지처럼 보인다. 배지를 빼고 남는 획이 없으면 모델이 짚은 것이 바로 그 도형이다
    picked = [(r, kind) for r, kind in picked if not decorated(r)] or picked
    chosen = [r for r, kind in picked if kind != "blank"]
    # 답 쓰는 빈칸은 그림 곁에 붙어 있을 때만 그림의 일부다. 모델이 옆으로 넉넉히 짚었다고 본문의 빈칸까지 넣지 않는다
    chosen += [r for r, kind in picked if kind == "blank" and (not chosen or any(
        r[0] < o[2] + em and r[2] > o[0] - em and r[1] < o[3] + em and r[3] > o[1] - em for o in chosen))]
    if chosen:
        # 그림 바로 위의 문제 글줄이나 옆의 '(1)'이 라벨처럼 딸려 오지 않게 한다
        scan.absorb_labels(chosen, strict=True)
        scan.absorb_rows(chosen)
    else:
        # 큰 획이 없다(작은 조각들로만 된 그림). 짚은 영역에 든 잉크를 모은다. 영역 밖에서부터 이어져 온
        # 긴 글줄은 그림 옆을 지나가는 본문이므로, 그 줄의 글자는 영역에 걸쳐 있어도 넣지 않는다
        def body(p) -> bool:
            return any((s[0] < wx0 - em or s[2] > wx1 + em) and p[0] >= s[0] and p[2] <= s[2]
                       and min(p[3], s[3]) - max(p[1], s[1]) > 0.5 * (p[3] - p[1]) for s in scan.sentences)
        chosen = [list(p) for p in scan.pieces if share(p) >= 0.7 and not body(p)]
        if not chosen:
            return None
    union = [min(r[0] for r in chosen), min(r[1] for r in chosen), max(r[2] for r in chosen), max(r[3] for r in chosen)]
    scan.clean_edges([union])
    return scan.to_page([union])[0]
