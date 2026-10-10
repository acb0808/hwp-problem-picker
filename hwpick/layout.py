"""쪽 이미지의 잉크(글자·선) 배치를 영상 처리로 분석한다. 모델을 쓰지 않아 쪽당 수십 ms다."""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

INK_DIFF = 32          # 배경보다 이만큼 어두우면 잉크. 옅은 회색 번호도 잡히고, 넓은 워터마크는 배경으로 흡수된다
RULE_LEN_EM = 3.5      # 이보다 긴 직선은 글자가 아니라 괘선
LEFT_CLEAR_EM = 1.2    # 왼쪽으로 이만큼 비어 있으면 줄(단)의 시작으로 본다


@dataclass
class Blob:
    """붙어 있는 글자 덩어리(대략 한 어절). 좌표는 픽셀."""
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0


@dataclass
class Layout:
    img: np.ndarray                 # BGR
    ink: np.ndarray                 # 배경보다 어두운 모든 획(0/1)
    text: np.ndarray                # 괘선을 뺀 잉크(0/1)
    rules_h: np.ndarray             # 가로 괘선(0/1)
    rules_v: np.ndarray             # 세로 괘선(0/1)
    em: float                       # 본문 글자 높이(픽셀)
    blobs: list[Blob] = field(default_factory=list)


def ink_mask(img: np.ndarray) -> np.ndarray:
    """배경보다 어두운 획. 색 글자도 잡히도록 가장 어두운 채널을 쓴다."""
    m = img.min(axis=2)
    k = max(31, (max(img.shape[:2]) // 40) | 1)
    bg = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    return (cv2.subtract(bg, m) > INK_DIFF).astype(np.uint8)


def _components(mask: np.ndarray) -> np.ndarray:
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return stats[1:n]


def analyze(img: np.ndarray) -> Layout:
    ink = ink_mask(img)
    em0 = max(img.shape[:2]) / 90.0

    # 괘선: 긴 가로·세로 직선(보기 상자, 단 구분선, 표)
    n = max(30, int(RULE_LEN_EM * em0))
    rules_h = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (n, 1)))
    # 세로선은 점선(단 구분선)도 잡도록 짧은 틈을 먼저 메운다. 글줄 사이 간격보다는 작게.
    gap = max(3, int(0.3 * em0)) | 1
    joined = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, gap)))
    rules_v = cv2.morphologyEx(joined, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 2 * n)))
    rules_v |= cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, n)))
    rules = cv2.dilate(rules_h | rules_v, np.ones((3, 3), np.uint8))
    text = ink & (1 - rules)
    # 스캔 가장자리의 그림자·옆 쪽 흔적은 글자가 아니다
    edge = max(4, int(0.5 * em0))
    text[:, :edge] = 0
    text[:, -edge:] = 0
    text[:edge, :] = 0
    text[-edge:, :] = 0
    # 먼지 같은 점은 지운다(마침표·i의 점보다 작은 것만)
    _, labels, cc, _ = cv2.connectedComponentsWithStats(text, connectivity=8)
    keep = cc[:, cv2.CC_STAT_AREA] >= 4
    keep[0] = False
    text = keep[labels].astype(np.uint8)

    # 본문 글자 높이: 어절 덩어리 높이의 중앙값
    def word_stats(em: float) -> np.ndarray:
        kw = max(3, int(round(0.3 * em))) | 1
        merged = cv2.dilate(text, cv2.getStructuringElement(cv2.MORPH_RECT, (kw, 3)))
        stats = _components(merged).copy()
        # 번진 만큼 되돌려 실제 잉크 범위로 맞춘다
        stats[:, cv2.CC_STAT_LEFT] += kw // 2
        stats[:, cv2.CC_STAT_WIDTH] = np.maximum(1, stats[:, cv2.CC_STAT_WIDTH] - 2 * (kw // 2))
        stats[:, cv2.CC_STAT_TOP] += 1
        stats[:, cv2.CC_STAT_HEIGHT] = np.maximum(1, stats[:, cv2.CC_STAT_HEIGHT] - 2)
        return stats

    stats = word_stats(em0)
    wide = stats[(stats[:, cv2.CC_STAT_WIDTH] > 1.5 * stats[:, cv2.CC_STAT_HEIGHT])
                 & (stats[:, cv2.CC_STAT_HEIGHT] > 0.4 * em0) & (stats[:, cv2.CC_STAT_HEIGHT] < 2.5 * em0)]
    em = float(np.median(wide[:, cv2.CC_STAT_HEIGHT])) if len(wide) >= 5 else em0
    if abs(em - em0) > 0.25 * em0:
        stats = word_stats(em)

    blobs = [Blob(int(s[0]), int(s[1]), int(s[0] + s[2]), int(s[1] + s[3]))
             for s in stats if s[cv2.CC_STAT_AREA] >= 12 and s[3] >= 0.3 * em]
    return Layout(img=img, ink=ink, text=text, rules_h=rules_h, rules_v=rules_v, em=em, blobs=blobs)


@dataclass
class Token:
    """문제 번호 후보. kind는 찾은 방법이다.

    line: 왼쪽이 비어 있는 짧은 글자 덩어리 / inline: 왼쪽에 배지 등이 붙은 덩어리 /
    fill: 색으로 채운 알약 모양 배지(흰 글씨 번호가 들어 있다) / badge: 넓게 칠한 큰 원·네모 안의 흰 번호
    """
    x0: int
    y0: int
    x1: int
    y1: int
    h: int                # 첫 덩어리의 높이(글자 크기)
    color: tuple[int, int, int] = (0, 0, 0)   # 획 가운데의 BGR
    text: str = ""
    score: float = 0.0
    kind: str = "line"
    solid: bool = False   # 색으로 채운 작은 네모·원 안에 흰 글씨로 든 것(개념 정리의 소제목 번호 등)
    read: tuple[int, int, int, int] | None = None     # OCR로 읽을 부분(토큰 전체가 아닐 때)

    @property
    def style(self) -> str:
        """번호 양식을 가를 때 쓰는 모양 구분."""
        if self.kind in ("fill", "badge"):
            return self.kind
        return "solid" if self.solid else "line"


def _candidate(lay: Layout, b: Blob) -> bool:
    return 0.55 * lay.em <= b.h <= 4.5 * lay.em and b.w >= 0.2 * lay.em


def _left_clear(lay: Layout, x0: int, y0: int, y1: int) -> bool:
    """왼쪽 LEFT_CLEAR_EM 안에 비슷한 크기의 글자 덩어리가 없는가.

    훨씬 작은 글자(여백의 워터마크 등)와 지워지다 남은 세로선 조각은 막는 것으로 치지 않는다.
    """
    em = lay.em
    h = y1 - y0
    pad = int(0.2 * h)
    lo, hi = y0 + pad, y1 - pad
    reach = x0 - int(LEFT_CLEAR_EM * em)
    for o in lay.blobs:
        if o.x0 < x0 and reach < o.x1 <= x0 + 1 and o.y0 < hi and o.y1 > lo:
            if o.h < 0.5 * h or (o.w < 0.35 * em and o.h > 1.5 * em):
                continue
            if max(o.w, o.h) <= 0.9 * em and 0.5 <= o.w / o.h <= 1.5                     and lay.ink[o.y0:o.y1, o.x0:o.x1].mean() >= 0.9:
                continue                       # 글머리의 작은 네모('■ EXAMPLE 046')
            return False
    return True


def _core_color(lay: Layout, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int]:
    """획 가운데의 색(BGR). 가장자리는 종이색과 섞여 옅어지므로 어두운 절반만 평균한다."""
    px = lay.img[y0:y1, x0:x1][lay.text[y0:y1, x0:x1].astype(bool)]
    if len(px) == 0:
        return (0, 0, 0)
    dark = px.min(axis=1)
    core = px[dark <= np.median(dark)]
    return tuple(int(v) for v in core.mean(axis=0))


def _is_solid(lay: Layout, b: Blob) -> bool:
    """덩어리가 색으로 채운 네모·원인가(흰 글씨가 든 배지). 위·아래 가장자리가 끊기지 않고 칠해져 있다."""
    if b.w < 0.75 * b.h or b.h < 4:
        return False
    ink = lay.ink[b.y0:b.y1, b.x0:b.x1]
    mid = ink[:, int(0.25 * b.w):max(int(0.25 * b.w) + 1, int(0.75 * b.w))]
    edge = max(2, int(0.12 * b.h))
    return bool(mid[1:1 + edge].mean() >= 0.85 and mid[b.h - 1 - edge:b.h - 1].mean() >= 0.85)


NUMBER_WIDTH_H = 3.4     # 번호만 있는 토큰의 최대 폭(글자 높이의 배수)
LABEL_WIDTH_H = 7.5      # '유제 02-1'처럼 낱말이 붙은 번호까지 받아들이는 최대 폭


def _token_at(lay: Layout, b: Blob) -> Token | None:
    """덩어리에서 시작해 오른쪽으로 빈 틈이 나올 때까지를 한 토큰으로 본다.

    0.6h 이상의 틈에서는 끊는다. 0.35h~0.6h의 틈은 숫자 1 때문에 벌어진 틈일 때만 넘어간다:
    굵은 숫자 글꼴에서 '311', 'D126'의 1은 좌우가 넓게 떨어진다. 틈의 한쪽이 1처럼 좁고 키 큰 글자이고,
    뒤따르는 글자가 같은 색·같은 키의 글자 하나여야 한다('312 집합'의 '집'은 색이 달라 끊긴다).
    """
    W = lay.text.shape[1]
    h = b.h
    if _is_solid(lay, b):
        return Token(b.x0, b.y0, b.x1, b.y1, h, _core_color(lay, b.x0, b.y0, b.x1, b.y1), solid=True)
    x_end = min(W, b.x0 + int(8 * h))
    band = lay.text[b.y0:b.y1, b.x0:x_end]
    cols = np.concatenate((band.any(axis=0), [False]))
    small, big = max(3, int(0.35 * h)), max(4, int(0.6 * h))
    last = len(cols) - 1

    def extent(a: int, z: int) -> int:
        rows = np.flatnonzero(band[:, a:z].any(axis=1))
        return int(rows[-1] - rows[0] + 1) if len(rows) else 0

    def like_one(a: int, z: int) -> bool:
        return z - a < 0.45 * h and extent(a, z) >= 0.75 * h

    def like_dash(a: int, z: int) -> bool:
        """글자 가운데 높이에 놓인 짧고 납작한 가로 획(하이픈)."""
        rows = np.flatnonzero(band[:, a:z].any(axis=1))
        if not len(rows) or not 0.12 * h <= z - a <= 0.7 * h:
            return False
        return rows[-1] - rows[0] + 1 <= 0.22 * h and 0.25 * h <= (rows[0] + rows[-1]) / 2 <= 0.8 * h

    end = last
    i = min(int(0.5 * h), max(1, b.w - 1))
    while i < last:
        if cols[i]:
            i += 1
            continue
        j = i
        while j < last and not cols[j]:
            j += 1
        if j - i >= small:
            p = i
            while p > 0 and cols[p - 1]:
                p -= 1
            k = j
            while k < last and cols[k]:
                k += 1
            bridge = (j - i < big and j < last and k - j < 0.75 * h and extent(j, k) >= 0.85 * h - 2
                      and (like_one(p, i) or like_one(j, k)))
            if bridge:
                from .detect import color_dist
                bridge = color_dist(_core_color(lay, b.x0 + p, b.y0, b.x0 + i, b.y1),
                                    _core_color(lay, b.x0 + j, b.y0, b.x0 + k, b.y1)) <= 40
            if not bridge and j - i < big and j < last:
                # 하이픈으로 이어진 번호('1-1', '02-3'): 하이픈 양옆의 틈은 번호 안의 틈이다
                if like_dash(j, k):
                    k2 = k
                    while k2 < last and not cols[k2]:
                        k2 += 1
                    k3 = k2
                    while k3 < last and cols[k3]:
                        k3 += 1
                    bridge = k2 - k < big and k2 < last and extent(k2, k3) >= 0.6 * h
                elif like_dash(p, i):
                    bridge = extent(j, k) >= 0.6 * h
            if not bridge:
                end = i
                break
        i = j
    # 바로 뒤에 붙은 작은 글자(출처 표기, 난이도 표시, '서술형' 꼬리표)는 다른 토큰이다
    for o in lay.blobs:
        if b.x0 + 0.5 * h < o.x0 < b.x0 + end and o.h < 0.75 * h and o.y0 < b.y1 and o.y1 > b.y0:
            a = z = o.x0 - b.x0
            while z < last and cols[z]:
                z += 1
            if like_dash(a, z):
                continue                       # 큰 숫자 뒤에 작게 붙은 '-1'은 같은 번호다
            end = o.x0 - b.x0 - 1
    if end > NUMBER_WIDTH_H * h:
        # 넓어도 '['로 시작하는 범위 표기('[0511~0514]')일 수 있다: 왼쪽 끝이 세로 막대인 것은 그대로 둔다
        bar = lay.text[b.y0:b.y1, b.x0:b.x0 + max(2, int(0.12 * h))].any(axis=1).mean()
        if end > 8 * h or bar < 0.85:
            # 낱말이 붙은 번호('유제 02-1')일 수도 있다. 넓은 조각을 다 읽으면 느리므로 첫 낱말만 남기고,
            # 읽어 보아 번호 앞에 붙는 낱말이면 그때 뒤를 붙여 다시 읽는다(detect.read_tokens).
            if end > LABEL_WIDTH_H * h:
                return None
            gap, run, first = max(2, int(0.25 * h)), 0, 0
            for q in range(1, end):
                run = run + 1 if not cols[q] else 0
                if run >= gap:
                    first = q - run + 1
                    break
            if first == 0 and end <= 5.5 * h:
                pass                           # 틈 없이 이어진 덩어리(바탕이 칠해진 '예제 02' 표지 등)는 통째로 읽는다
            elif not 0.5 * h <= first <= NUMBER_WIDTH_H * h:
                return None
            else:
                end = first
    x1 = b.x0 + end
    return Token(b.x0, b.y0, x1, b.y1, h, _core_color(lay, b.x0, b.y0, x1, b.y1))


def _across_rule(lay: Layout, t: Token) -> None:
    """가로선이 글자를 가로질러 지나가 위아래로 잘린 토큰이면, 선 너머의 나머지를 붙인다.

    쪽 위의 장식선에 걸쳐 인쇄된 큰 번호가 그렇다. 선에 바로 맞닿아 있고, 선 건너편에도 같은 자리에
    잉크가 바로 맞닿아 이어질 때만 붙인다(밑줄 아래의 다음 줄은 선에서 떨어져 있어 붙지 않는다).
    """
    H = lay.text.shape[0]
    w = t.x1 - t.x0
    for down in (True, False):
        edge = t.y1 if down else t.y0 - 1
        step = 1 if down else -1
        y = edge
        while 0 <= y < H and abs(y - edge) < 3 and not lay.rules_h[y, t.x0:t.x1].any():
            y += step
        if not (0 <= y < H) or not lay.rules_h[y, t.x0:t.x1].any():
            continue
        while 0 <= y < H and abs(y - edge) < 0.5 * t.h and lay.rules_h[y, t.x0:t.x1].any():
            y += step                           # 선의 굵기만큼 건너간다
        start = y
        while 0 <= y < H and abs(y - start) < 3 and lay.text[y, t.x0:t.x1].sum() < 0.25 * w:
            y += step
        first = y
        while 0 <= y < H and abs(y - first) <= 1.5 * t.h and lay.text[y, t.x0:t.x1].any():
            y += step
        if abs(y - first) >= 0.3 * t.h and abs(y - first) <= 1.5 * t.h and abs(first - start) < 3:
            if down:
                t.y1 = y
            else:
                t.y0 = y + 1
            t.h = t.y1 - t.y0


def line_start_tokens(lay: Layout) -> list[Token]:
    """왼쪽으로 LEFT_CLEAR_EM만큼 비어 있고, 뒤에 간격을 두고 끊기는 짧은 덩어리를 찾는다."""
    out = []
    for b in lay.blobs:
        if _candidate(lay, b) and _left_clear(lay, b.x0, b.y0, b.y1):
            t = _token_at(lay, b)
            if t:
                if not t.solid:
                    _across_rule(lay, t)
                out.append(t)
    return out


def inline_tokens(lay: Layout, accept) -> list[Token]:
    """왼쪽이 비어 있지 않은 덩어리(앞에 배지가 붙은 번호 등) 중 accept(높이비, 색)이 참인 것."""
    out = []
    for b in lay.blobs:
        if _candidate(lay, b) and not _left_clear(lay, b.x0, b.y0, b.y1):
            t = _token_at(lay, b)
            if t and accept(t.h / lay.em, t.color):
                t.kind = "inline"
                out.append(t)
    return out


def fill_tokens(lay: Layout) -> list[Token]:
    """색으로 채운 알약 모양 배지. 번호가 흰 글씨로 들어 있는 교재가 있다(예: '0394 대표문제').

    흰 글씨 구멍을 메운 뒤 굵은 영역만 남긴다. 굵은 글줄도 이렇게 뭉치지만, 배지는 위·아래 가장자리가
    끊기지 않고 칠해져 있다는 점으로 가른다.
    """
    em = lay.em
    k1, k2 = max(3, int(0.5 * em)) | 1, max(3, int(0.6 * em)) | 1
    solid = cv2.morphologyEx(lay.ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k1, k1)))
    solid = cv2.morphologyEx(solid, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k2, k2)))
    out = []
    for x, y, w, h, _ in _components(solid):
        if not (0.8 * em <= h <= 2.4 * em and 1.6 * h <= w <= 12 * em):
            continue
        ink = lay.ink[y:y + h, x:x + w]
        mid = ink[:, int(0.15 * w):max(int(0.15 * w) + 1, int(0.85 * w))]
        edge = max(2, int(0.12 * h))
        if ink.mean() < 0.5 or mid[1:1 + edge].mean() < 0.85 or mid[h - 1 - edge:h - 1].mean() < 0.85:
            continue
        if not _left_clear(lay, int(x), int(y), int(y + h)):
            continue
        patch = lay.img[y:y + h, x:x + w][ink.astype(bool)]
        color = tuple(int(v) for v in np.median(patch, axis=0)) if len(patch) else (0, 0, 0)
        out.append(Token(int(x), int(y), int(x + w), int(y + h), int(h), color, kind="fill"))
    return out


def badge_tokens(lay: Layout) -> list[Token]:
    """넓게 칠한 큰 원·네모 안에 흰 글씨로 번호가 든 배지(예: 보라색 원 안의 '예제 05').

    잉크 마스크는 넓게 칠한 면을 배경으로 치므로 밝기로 따로 찾는다. 배지 안의 큰 흰 글자(번호)의
    범위를 read에 담아 그 부분만 읽게 한다.
    """
    em = lay.em
    m = lay.img.min(axis=2)
    paper = float(np.percentile(m[::4, ::4], 80))
    dark = (m < paper - 50).astype(np.uint8)
    k1, k2 = max(3, int(0.7 * em)) | 1, max(3, int(1.8 * em)) | 1
    solid = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k1, k1)))
    solid = cv2.morphologyEx(solid, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k2, k2)))
    out = []
    for x, y, w, h, area in _components(solid):
        if not (2.4 * em <= h <= 10 * em and 2.4 * em <= w <= 10 * em and 0.6 <= w / h <= 1.7):
            continue
        if area < 0.6 * w * h:
            continue
        patch = m[y:y + h, x:x + w]
        inside = dark[y:y + h, x:x + w].astype(bool)
        if inside.sum() < 0.3 * w * h:
            continue
        level = float(np.median(patch[inside]))
        light = ((patch > level + 50) & solid[y:y + h, x:x + w].astype(bool)).astype(np.uint8)
        big = [c for c in _components(light) if c[3] >= 0.3 * h and c[2] <= 0.8 * w]
        if not big:
            continue
        gx0, gy0 = min(c[0] for c in big), min(c[1] for c in big)
        gx1, gy1 = max(c[0] + c[2] for c in big), max(c[1] + c[3] for c in big)
        color = tuple(int(v) for v in np.median(lay.img[y:y + h, x:x + w][inside], axis=0))
        out.append(Token(int(x), int(y), int(x + w), int(y + h), int(gy1 - gy0), color, kind="badge",
                         read=(int(x + gx0), int(y + gy0), int(x + gx1), int(y + gy1))))
    return out


def crop_token(lay: Layout, t: Token) -> np.ndarray:
    x0, y0, x1, y1 = t.read or (t.x0, t.y0, t.x1, t.y1)
    pad = max(2, int(0.15 * (y1 - y0 if t.read else t.h)))
    H, W = lay.text.shape
    return lay.img[max(0, y0 - pad):min(H, y1 + pad), max(0, x0 - pad):min(W, x1 + pad)]


def widen_token(lay: Layout, t: Token, max_width_h: float = 16.0, gap_h: float = 1.0) -> None:
    """토큰을 같은 줄에서 큰 틈(gap_h)이 나올 때까지 오른쪽으로 늘린다('[0461~0462]' 같은 범위 표기용)."""
    W = lay.text.shape[1]
    x_end = min(W, t.x0 + int(max_width_h * t.h))
    cols = lay.text[t.y0:t.y1, t.x0:x_end].any(axis=0)
    run = 0
    end = len(cols)
    for i in range(t.x1 - t.x0, len(cols)):
        run = run + 1 if not cols[i] else 0
        if run >= gap_h * t.h:
            end = i - run + 1
            break
    t.x1 = t.x0 + end


def extend_word(lay: Layout, t: Token, max_gap_h: float = 1.6, max_width_h: float = 12.0) -> bool:
    """토큰 바로 뒤의 한 덩어리(다음 틈 전까지)를 토큰에 붙인다('유제' + '02-1'). 붙였으면 True.

    뒤 덩어리는 글자가 더 크거나 기울어 있을 수 있으므로('02-' 뒤의 큰 '1', 기울인 '046') 위아래로
    조금 넓힌 띠에서 틈을 재고, 세로 범위도 그 글자에 맞춰 넓힌다.
    """
    H, W = lay.text.shape
    grow = int(0.7 * t.h)
    a, z = max(0, t.y0 - grow), min(H, t.y1 + grow)
    x_end = min(W, t.x0 + int(max_width_h * t.h))
    cols = lay.text[a:z, t.x0:x_end].any(axis=0)
    i = j = t.x1 - t.x0
    while j < len(cols) and not cols[j]:
        j += 1
    if j >= len(cols) or j - i > max_gap_h * t.h:
        return False
    run, end = 0, len(cols)
    for q in range(j, len(cols)):
        run = run + 1 if not cols[q] else 0
        if run >= 0.9 * t.h:
            end = q - run + 1
            break
    rows = lay.text[a:z, t.x0 + j:t.x0 + end].any(axis=1)
    top, bottom = t.y0 - a, t.y1 - a
    while top > 0 and rows[top - 1]:
        top -= 1
    while bottom < len(rows) and rows[bottom]:
        bottom += 1
    t.x1, t.y0, t.y1 = t.x0 + end, a + top, a + bottom
    return True


def line_crops(lay: Layout, x0: int, y0: int, x1: int, y1: int, max_lines: int = 6,
               tail: int = 0) -> list[np.ndarray]:
    """영역 안의 글줄을 잘라 낸다. OCR이 읽을 수 있는 폭에 맞춰 긴 줄은 낱말 사이에서 나눈다.

    줄이 max_lines보다 많으면 앞쪽 줄들과 맨 뒤 tail줄만 낸다(긴 문제는 '구하시오'가 끝에 나온다).

    글자 덩어리(어절)를 줄로 묶어 만들기 때문에, 옆에 큰 그림이나 번호 배지가 있어도 글줄이 따로 잡힌다.
    """
    em = lay.em
    H, W = lay.text.shape
    words = sorted((b for b in lay.blobs if b.x0 >= x0 and b.x1 <= x1 and b.y0 >= y0 and b.y1 <= y1
                    and 0.5 * em <= b.h <= 2.2 * em), key=lambda b: (b.y0 + b.y1) / 2)
    lines: list[list[Blob]] = []
    for b in words:
        if lines:
            top, bottom = min(o.y0 for o in lines[-1]), max(o.y1 for o in lines[-1])
            if min(bottom, b.y1) - max(top, b.y0) >= 0.5 * min(b.h, bottom - top):
                lines[-1].append(b)
                continue
        lines.append([b])
    out = []
    pad = max(2, int(0.15 * em))
    if len(lines) > max_lines:
        lines = lines[:max_lines - tail] + (lines[-tail:] if tail else [])
    for line in lines:
        line.sort(key=lambda b: b.x0)
        limit = 8 * max(b.h for b in line)
        chunk: list[Blob] = []
        for b in line + [None]:
            if chunk and (b is None or b.x1 - chunk[0].x0 > limit):
                out.append(lay.img[max(0, min(o.y0 for o in chunk) - pad):min(H, max(o.y1 for o in chunk) + pad),
                                   max(0, chunk[0].x0 - pad):min(W, chunk[-1].x1 + pad)])
                chunk = []
            if b is not None:
                chunk.append(b)
    return out
