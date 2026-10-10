"""문제 번호(앵커)를 찾아 문항 박스를 만든다.

1. 교재마다 번호 모양(크기·색·자릿수·앞에 붙는 낱말)이 다르므로, 몇십 쪽을 표본으로 읽어
   "한 쪽 안에서 1씩 이어지는 번호 무리"를 그 교재의 번호 양식(Track)으로 학습한다.
   번호가 붙었다고 다 문제는 아니므로(개념 정리의 소제목, 쪽 번호, 참고 목록), 그 번호로 만든 칸의
   글을 조금 읽어 "구하시오", "것은?" 같은 문제 말투가 나오는 양식만 받아들인다.
2. 각 쪽에서 양식에 맞는 번호만 앵커로 삼고, 단(세로 여백·세로선)과 다음 번호 위치로
   칸을 나눈 뒤 잉크가 있는 범위로 줄여 박스를 만든다.
3. '[0461~0462] 다음 …' 같은 공통 발문은 해당 번호의 박스에 따로 붙여 둔다.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable

import numpy as np

from . import layout, ocr
from .layout import Layout, Token

_DASH = "-‐‑–—−ㅡ一"                      # OCR이 하이픈을 읽어 내는 여러 모양
_NUM = rf"(\d{{1,4}})(?:[{_DASH}](\d{{1,2}}))?"     # 0461, 7, 02-1
NUM_RE = re.compile(rf"([A-Za-z]{{0,2}}){_NUM}([.)]?)")
LABEL_RE = re.compile(rf"([가-힣]{{1,5}}|[A-Za-z]{{3,10}}){_NUM}([.)]?)")    # 유제02-1, 예제03, EXAMPLE046
BADGE_RE = re.compile(r"\d{2,6}")       # 배지 안 번호. 구분선 '|'이 1로 읽혀 한두 자리 더 붙기도 한다
STEM_HEAD_RE = re.compile(r"\[[A-Za-z]{0,2}\d")
STEM_RE = re.compile(r"\[([A-Za-z]{0,2})(\d{1,4})\D{1,3}?([A-Za-z]{0,2})(\d{1,4})\]?")
CIRCLED = "①②③④⑤"
# 문제 번호 앞에 붙는 낱말. 이 낱말만 따로 읽혔으면 뒤의 번호까지 붙여서 다시 읽는다.
# '유형 01', '개념 004', 'REMARK 1'처럼 문제가 아닌 것의 번호는 받지 않는다.
LABEL_WORDS = {"유제", "예제", "문제", "확인", "필수", "기본", "발전", "대표", "기출", "연습", "실전", "심화",
               "응용", "유사", "변형", "도전", "서술형", "필수예제", "대표예제", "기본예제", "발전예제",
               "확인문제", "확인체크", "개념확인", "쌍둥이", "숫자바꾸기",
               "EXAMPLE", "EXERCISE", "CHECK", "TEST", "QUIZ", "PROBLEM"}
# 풀이가 시작되는 자리. 문제 아래에 풀이가 딸린 예제는 여기서 박스를 끝낸다.
SOLUTION_WORDS = {"풀이", "해설", "정답", "해답", "답", "GUIDE", "SOLUTION", "ANSWER", "SOL"}
# 문제의 말투. 번호 양식이 정말 문제 번호인지 확인할 때 쓴다.
CUE_RE = re.compile(r"시오|[하어여아]라(?:\.|$)|면\?|(?:것|값|합|곱|차|몫|수|범위|넓이|길이|크기|부피|거리|좌표|확률|기울기|식|해|근|답|나머지|계수|항|점)[은는]\?")

SIZE_TOL = 0.20        # 번호 높이(본문 글자 대비)의 허용 오차
LOOSE_SIZE_TOL = 0.40  # 낱말이 붙었거나 '02-1' 꼴인 번호는 글자 크기가 섞여 있어 높이가 들쭉날쭉하다
COLOR_TOL = 40.0       # 번호 색 차이(color_dist)의 허용 오차
GAP_MAX_EM = 3.5       # 문제 안에서 이보다 큰 세로 여백이 나오면 거기서 문제가 끝난 것으로 본다
MIN_GUTTER_EM = 0.7    # 단 사이 여백의 최소 폭
MARGIN = 0.02          # 쪽 좌우 가장자리의 이 비율은 내용으로 보지 않는다(옆 탭, 스캔 그림자)
EDGE_TAB = 0.03        # 번호의 중심이 좌우 가장자리 이 비율 안이면 쪽 옆의 색인 탭이다
HEADER_Y = 0.04        # 이 위와
FOOTER_Y = 0.92        # 이 아래의 숫자는 쪽 번호·머리말이다. 이 아래에서 따로 떨어진 줄은 꼬리말로 본다
MIN_PROBLEMS = 0.3     # 표본 쪽 중 문제가 보이는 쪽의 비율이 이보다 낮은 번호 양식은 버린다
MIN_PROBLEMS_MARKED = 0.5  # 낱말이 붙었거나 배지에 든 번호는 단원·유형 머리에도 흔히 쓰이므로 더 엄격히 본다


def _chroma(c) -> np.ndarray:
    c = np.asarray(c, dtype=float)
    return c - c.mean()


def color_dist(a, b) -> float:
    """색 차이. 스캔마다 밝기는 흔들리므로 색조 차이를 주로 보고 밝기 차이는 조금만 반영한다."""
    return float(np.linalg.norm(_chroma(a) - _chroma(b)) + 0.35 * abs(np.mean(a) - np.mean(b)))


# ---------------------------------------------------------------- 번호 읽기

@dataclass
class PageTokens:
    """한 쪽에서 읽어 둔 번호 후보와, 그것을 해석하는 데 필요한 쪽 정보."""
    em: float
    width: int
    height: int
    tokens: list[Token]


@dataclass
class Num:
    """번호로 읽힌 토큰."""
    tok: Token
    prefix: str                   # 번호에 붙은 영문자('F04'의 F)
    digits: str                   # 번호. '02-1' 꼴에서는 뒤쪽 숫자
    suffix: str
    h_rel: float
    guessed: bool = False         # 글자는 못 읽었지만 앞뒤 번호로 값을 정한 경우
    word: str = ""                # 번호 앞의 낱말('유제', '예제', 'EXAMPLE')
    major: str = ""               # '02-1' 꼴의 앞쪽 숫자

    @property
    def value(self) -> int:
        return int(self.digits)

    @property
    def key(self) -> tuple[str, int, int]:
        """같은 쪽에서 번호를 구별하는 값(낱말, 앞 숫자, 번호)."""
        return self.word, int(self.major) if self.major else -1, self.value

    @property
    def plain(self) -> bool:
        """낱말도 없고 '02-1' 꼴도 아닌, 한 줄로 이어지는 보통 번호인가."""
        return not self.word and not self.major

    @property
    def label(self) -> str:
        return f"{self.word}{self.prefix}{self.major + '-' if self.major else ''}{self.digits}"


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def read_tokens(lay: Layout, tokens: list[Token]) -> None:
    """토큰을 OCR로 읽는다. 번호의 앞부분만 읽힌 것으로 보이면 넓혀서 다시 읽는다.

    - '[0461'처럼 범위 표기의 머리 → 닫는 괄호까지
    - 'G', 'S'처럼 글자 사이를 띄운 영문 낱말의 첫 글자 → 낱말 끝까지('GUIDE', 'SOLUTION')
    - '유제', '예제'처럼 번호 앞에 붙는 낱말 → 뒤의 번호까지('유제 02-1')
    """
    def read(batch: list[Token]) -> None:
        for t, (text, score) in zip(batch, ocr.read_crops([layout.crop_token(lay, t) for t in batch])):
            t.text, t.score = text, score

    read(tokens)
    lines = [t for t in tokens if t.kind == "line" and not t.solid]
    heads = [t for t in lines if STEM_HEAD_RE.match(_squash(t.text))]
    for t in heads:
        layout.widen_token(lay, t)
    read(heads)
    spaced = [t for t in lines if re.fullmatch(r"[A-Z]", _squash(t.text)) and t.score >= 0.5]
    grown = []
    for t in spaced:
        x1 = t.x1
        layout.widen_token(lay, t, max_width_h=16.0, gap_h=1.3)
        if t.x1 > x1 + t.h:
            grown.append(t)
        else:
            t.x1 = x1
    read(grown)
    labels = []
    for t in lines:
        if t.score >= 0.6 and _squash(t.text).upper() in LABEL_WORDS:
            old = (t.x1, t.y0, t.y1, t.text, t.score)
            if layout.extend_word(lay, t):
                labels.append((t, old))
    read([t for t, _ in labels])
    for t, old in labels:
        if not LABEL_RE.fullmatch(_squash(t.text)):
            t.x1, t.y0, t.y1, t.text, t.score = old


def numbers_of(em: float, tokens: list[Token]) -> list[Num]:
    out = []
    for t in tokens:
        if t.score < 0.6:
            continue
        text = _squash(t.text)
        m = LABEL_RE.fullmatch(text)
        if m:
            if m.group(1).upper() in LABEL_WORDS and not (m.group(3) and len(m.group(2)) > 3):
                major, digits = (m.group(2), m.group(3)) if m.group(3) else ("", m.group(2))
                out.append(Num(t, "", digits, m.group(4), t.h / em, word=m.group(1).upper(), major=major))
            continue
        if t.kind == "fill":
            m = BADGE_RE.match(text)
            if m:
                out.append(Num(t, "", m.group(0), "", t.h / em))
            continue
        if t.x1 - t.x0 > layout.NUMBER_WIDTH_H * t.h and t.kind != "badge":
            continue                          # 넓은 토큰은 낱말이 붙은 번호일 때만 번호로 본다
        m = NUM_RE.fullmatch(text)
        if m and not (m.group(3) and len(m.group(2)) > 3):
            major, digits = (m.group(2), m.group(3)) if m.group(3) else ("", m.group(2))
            out.append(Num(t, m.group(1).upper(), digits, m.group(4), t.h / em, major=major))
    return out


def _in_body(t: Token, width: int, height: int) -> bool:
    """쪽 번호·머리말·옆 색인 탭이 놓이는 가장자리 띠가 아닌가."""
    cx, cy = (t.x0 + t.x1) / 2, (t.y0 + t.y1) / 2
    return EDGE_TAB * width < cx < (1 - EDGE_TAB) * width and HEADER_Y * height < cy < FOOTER_Y * height


# ---------------------------------------------------------------- 번호 양식

@dataclass
class Track:
    """한 교재에서 문제 번호로 쓰이는 글자 모양."""
    prefix_len: int
    suffix: str
    pad: int                      # 0이 아니면 앞을 0으로 채운 자릿수
    h_rel: float
    color: tuple[int, int, int]
    support: int = 0
    continuity: float = 0.0
    zero_len: int = 0             # pad가 0이어도 이 자릿수의 0 채움 번호(01~09)는 받아들인다
    aux: bool = False             # 혼자서는 이어지지 않고, 주 번호 사이에 끼어 나오는 번호
    style: str = "line"           # line(보통 글자) / solid(채운 네모 안 흰 글씨) / fill(알약 배지) / badge(큰 배지)
    word: str = ""                # 번호 앞의 낱말
    compound: bool = False        # '02-1' 꼴
    problems: float = 0.0         # 표본 쪽 중 문제가 보인 쪽의 비율
    h_min: float = 0.0            # 낱말이 붙었거나 '02-1' 꼴인 번호는 높이가 들쭉날쭉해서, 표본에서 본 범위를 적어 둔다
    h_max: float = 0.0

    @property
    def colored(self) -> bool:
        """검은 본문 글자와 색으로 구별되는 번호인가."""
        return float(np.linalg.norm(_chroma(self.color))) >= 30

    @property
    def bare(self) -> bool:
        """아무 꾸밈 없는 맨숫자인가. 표·목록·분수의 숫자와 헷갈리기 쉬워 더 엄격히 본다."""
        return not (self.pad or self.prefix_len or self.suffix or self.strong) and self.style in ("line", "solid")

    @property
    def strong(self) -> bool:
        """형식만으로도 뚜렷한 번호인가(낱말이 붙었거나 '02-1' 꼴)."""
        return bool(self.word) or self.compound

    def describe(self) -> str:
        """개발 도구에서 한 줄로 보여 줄 때 쓰는 요약."""
        form = f"{self.word}{'A' * self.prefix_len}{'0-0' if self.compound else '0' * (self.pad or 1)}{self.suffix}"
        return (f"{'AUX ' if self.aux else ''}{self.style} '{form}' h{self.h_rel:.2f} color{self.color} "
                f"n{self.support} cont{self.continuity} prob{self.problems}")

    def matches(self, n: Num) -> bool:
        if n.tok.style != self.style or n.word != self.word or bool(n.major) != self.compound:
            return False
        if len(n.prefix) != self.prefix_len or n.suffix != self.suffix:
            return False
        if self.pad:
            if len(n.digits) != self.pad:
                return False
        elif len(n.digits) > 1 and n.digits[0] == "0" and len(n.digits) != self.zero_len:
            return False
        elif len(n.digits) < self.zero_len:
            return False                      # 01, 02…로 적는 교재에서 맨숫자 1, 2는 문제 번호가 아니다
        return self.matches_style(n.h_rel, n.tok.color)

    def matches_style(self, h_rel: float, color: tuple[int, int, int]) -> bool:
        if self.strong and self.h_max:
            if not 0.88 * self.h_min <= h_rel <= 1.12 * self.h_max:
                return False
        elif abs(h_rel - self.h_rel) > (LOOSE_SIZE_TOL if self.strong else SIZE_TOL) * self.h_rel:
            return False
        return color_dist(color, self.color) <= COLOR_TOL


@dataclass
class Profile:
    tracks: list[Track] = field(default_factory=list)
    sampled_pages: int = 0

    def accepts_inline(self, h_rel: float, color: tuple[int, int, int]) -> bool:
        """앞에 배지가 붙은 번호를 찾을 때, 색 있는 번호 양식과 모양이 같은지 본다."""
        return any(t.colored and t.style == "line" and not t.strong and t.matches_style(h_rel, color)
                   for t in self.tracks)

    def to_json(self) -> dict:
        return {"tracks": [asdict(t) for t in self.tracks], "sampled_pages": self.sampled_pages}

    @staticmethod
    def from_json(d: dict) -> "Profile":
        tracks = [Track(**{**t, "color": tuple(t["color"])}) for t in d.get("tracks", [])]
        return Profile(tracks=tracks, sampled_pages=d.get("sampled_pages", 0))


def _style_clusters(nums: list[tuple[int, Num]], tol: float) -> list[list[tuple[int, Num]]]:
    """(쪽, 번호) 목록을 글자 크기와 색이 비슷한 것끼리 묶는다."""
    clusters: list[list[tuple[int, Num]]] = []
    for item in sorted(nums, key=lambda it: it[1].h_rel):
        n = item[1]
        for c in clusters:
            hs = np.median([m.h_rel for _, m in c])
            col = np.median([m.tok.color for _, m in c], axis=0)
            if abs(n.h_rel - hs) <= tol * hs and color_dist(n.tok.color, col) <= COLOR_TOL:
                c.append(item)
                break
        else:
            clusters.append([item])
    return clusters


def _continuity(items: list[tuple[int, Num]]) -> tuple[float, int]:
    """같은 쪽에 ±1 이웃 번호가 있는 번호의 비율과, 그런 번호의 개수."""
    by_page: dict[int, set[tuple[int, int]]] = {}
    for page, n in items:
        by_page.setdefault(page, set()).add(n.key[1:])
    linked = total = 0
    for keys in by_page.values():
        if len(keys) < 2:
            continue
        total += len(keys)
        linked += sum(1 for a, v in keys if (a, v + 1) in keys or (a, v - 1) in keys)
    return (linked / total if total else 0.0), linked


def _longest_runs(items: list[tuple[int, Num]]) -> list[int]:
    """쪽마다, 1씩 이어지는 번호가 가장 길게 몇 개 이어지는가."""
    by_page: dict[int, set[tuple[int, int]]] = {}
    for page, n in items:
        by_page.setdefault(page, set()).add(n.key[1:])
    out = []
    for keys in by_page.values():
        best = 0
        for a, v in keys:
            if (a, v - 1) not in keys:
                length = 1
                while (a, v + length) in keys:
                    length += 1
                best = max(best, length)
        out.append(best)
    return out


def _looks_like_main(cluster: list[tuple[int, Num]], track: "Track") -> float | None:
    """혼자서 문제 번호열을 이루면 연속 비율을, 아니면 None을 돌려준다."""
    cont, linked = _continuity(cluster)
    keys = [m.key for _, m in cluster]
    if track.word:
        # '예제 05'는 한 쪽에 하나씩 나오는 일이 많아 쪽 안에서 이어지는지를 볼 수 없다
        return cont if len(cluster) >= 5 and len(set(keys)) >= 4 else None
    # 맨숫자가 아니면 형식이 뚜렷하므로, 단원마다 1번부터 몇 개씩만 나오는 번호(예제 01~05)도 받아들인다
    if track.compound and track.h_rel >= 1.15 and len(cluster) >= 5 and len(set(keys)) >= 4 and linked < 4:
        # '02-1' 꼴의 예제 번호도 한 쪽에 하나씩 나온다. 본문 글자보다 큰 것만 받아 수식 조각('3-2')과 가른다
        return cont
    need = 4 if track.compound else 6
    if linked < need or cont < 0.6 or len(set(keys)) < (need if track.bare else 4):
        return None
    if not track.bare:
        return cont
    values = [m.value for _, m in cluster]
    # 몇 개의 값만 되풀이되면 우연히 이어진 숫자들이다(단원마다 1번부터 길게 이어지는 번호는 예외)
    top3 = sum(sorted((values.count(v) for v in set(values)), reverse=True)[:3])
    if top3 > 0.4 * len(values) and np.median(_longest_runs(cluster)) < 4:
        return None
    # 쪽마다 1부터 다시 시작하고 큰 수가 없으면 개념 정리의 소제목 번호다
    firsts: dict[int, int] = {}
    for p, m in cluster:
        firsts[p] = min(firsts.get(p, m.value), m.value)
    restart = sum(1 for v in firsts.values() if v == 1) / len(firsts)
    if max(values) <= 9 and restart >= 0.5:
        return None
    return cont


def learn_profile(pages: dict[int, PageTokens], get_layout: Callable[[int], Layout],
                  trace: list[str] | None = None) -> Profile:
    """표본 쪽들에서 읽은 번호 후보로 번호 양식을 학습한다. trace를 주면 후보마다 판정 과정을 적어 준다."""
    nums_by_page = {p: [n for n in numbers_of(pt.em, pt.tokens) if _in_body(n.tok, pt.width, pt.height)]
                    for p, pt in pages.items()}
    groups: dict[tuple, list[tuple[int, Num]]] = {}
    for page, nums in nums_by_page.items():
        for n in nums:
            if n.tok.kind == "fill" and not n.word:
                continue                      # 낱말 없는 색 배지 번호는 양식 없이 주 번호와의 인접으로 받아들인다
            groups.setdefault((n.tok.style, n.word, len(n.prefix), n.suffix, bool(n.major)), []).append((page, n))

    cands: list[tuple[Track, list[tuple[int, Num]]]] = []
    for (style, word, prefix_len, suffix, compound), nums in groups.items():
        strong = bool(word) or compound
        for cluster in _style_clusters(nums, LOOSE_SIZE_TOL if strong else SIZE_TOL):
            padded = [len(m.digits) for _, m in cluster if len(m.digits) > 1 and m.digits[0] == "0"]
            pad = 0
            if not compound and padded:
                # 01, 02…처럼 0으로 채워 적는 번호열: 채운 번호가 많거나, 그보다 짧은 맨숫자가 하나도 없다
                width = max(set(padded), key=padded.count)
                if len(padded) >= 0.25 * len(cluster) or not any(len(m.digits) < width for _, m in cluster):
                    pad = width
                    cluster = [(p, m) for p, m in cluster if len(m.digits) == pad]
            cluster = [(p, m) for p, m in cluster if m.value >= 1]
            if len(cluster) < 4:
                continue
            if not strong and style == "line" and np.median([m.h_rel for _, m in cluster]) < 0.8:
                continue                      # 본문보다 작은 숫자(분수, 그림 라벨)는 번호가 아니다
            cands.append((Track(
                prefix_len=prefix_len, suffix=suffix, pad=pad,
                h_rel=float(np.median([m.h_rel for _, m in cluster])),
                color=tuple(int(v) for v in np.median([m.tok.color for _, m in cluster], axis=0)),
                support=len(cluster), style=style, word=word, compound=compound,
                h_min=float(np.percentile([m.h_rel for _, m in cluster], 5)),
                h_max=float(np.percentile([m.h_rel for _, m in cluster], 95))), cluster))

    main: list[Track] = []

    def probe(track: Track, cluster: list[tuple[int, Num]]) -> tuple[float, float]:
        """이 번호로 박스를 만들어 보고 (두 줄 이상인 박스의 비율, 문제가 보이는 쪽의 비율)을 잰다.

        앞의 것은 목록·표의 숫자를, 뒤의 것은 개념 소제목·참고 목록처럼 문제가 아닌 번호를 거른다.
        이미 받아들인 번호 양식과 함께 박스를 만들어, 다른 문제의 글이 딸려 들어와 문제처럼 보이는 일을
        막는다('유형 ①' 머리 아래의 문제들). 박스가 아니라 쪽으로 세는 것은, 같은 모양의 번호가 문제 쪽과
        문제 아닌 쪽(문항 분석표 등)에 함께 쓰이는 교재가 있어서다. 문제 아닌 쪽은 검출할 때 다시 걸러진다.
        """
        sample = sorted({p for p, _ in cluster})
        step = max(1, len(sample) // 10)
        tall = total = good = seen = 0
        for p in sample[::step][:10]:
            lay = get_layout(p)
            boxes = detect_page(lay, pages[p].tokens, Profile(main + [track]), verify=False).boxes
            boxes = [b for b in boxes if b.anchor is not None and track.matches(b.anchor)]
            if not boxes:
                continue
            seen += 1
            total += len(boxes)
            tall += sum(1 for b in boxes if b.y1 - b.y0 >= 2.6 * lay.em)
            good += int(any(_reads_like_problem(lay, b, max_lines=8) for b in boxes[:3]))
        return (tall / total, good / seen) if seen else (0.0, 0.0)

    def alias_of(cluster: list[tuple[int, Num]]) -> Track | None:
        """같은 번호가 두 가지 모양으로 읽힌 것인가(알약 배지가 '채운 덩어리'로도, '배지'로도 잡히는 경우).

        이미 받아들인 양식의 번호와 자리·값이 거의 다 겹치면 그 양식을 돌려준다.
        """
        for t in main:
            same = sum(1 for p, n in cluster
                       if any(t.matches(m) and m.key == n.key and (_inside(n.tok, m.tok) or _inside(m.tok, n.tok))
                              for m in nums_by_page[p]))
            if same >= 0.7 * len(cluster):
                return t
        return None

    rest: list[tuple[Track, list[tuple[int, Num]]]] = []
    # 많이 나오는 번호부터 판정한다(뒤의 후보는 앞에서 받아들인 번호와 함께 박스를 만들어 본다)
    for track, cluster in sorted(cands, key=lambda c: -c[0].support):
        twin = alias_of(cluster)
        if twin is not None:
            track.continuity, track.problems = twin.continuity, twin.problems
            main.append(track)
            if trace is not None:
                trace.append(f"OK   {track.describe()} (같은 번호의 다른 모양)")
            continue
        cont = _looks_like_main(cluster, track)
        ok = cont is not None
        # 자릿수를 0으로 채우거나 글자·마침표·낱말이 붙은 번호는 형식만으로 뚜렷하다. 맨숫자 번호는 표·목록의
        # 숫자와 헷갈리므로 더 엄격히 본다(연속 비율 0.8 이상, 두 줄 이상의 내용이 딸려 올 것).
        if ok and track.bare and cont < 0.8:
            ok = False
        if ok:
            tall, track.problems = probe(track, cluster)
            track.problems = round(track.problems, 3)
            ok = track.problems >= (MIN_PROBLEMS_MARKED if track.word or track.style != "line" else MIN_PROBLEMS) \
                and (not track.bare or tall >= 0.35)
        if trace is not None:
            track.continuity = round(cont or 0.0, 3)
            trace.append(f"{'OK  ' if ok else 'drop'} {track.describe()} linked={_continuity(cluster)[1]}"
                         f" values={sorted({m.label for _, m in cluster})[:12]}")
        if ok:
            track.continuity = round(cont, 3)
            main.append(track)
        elif not track.strong and track.style == "line":
            rest.append((track, cluster))
    main.sort(key=lambda t: -t.support * t.continuity)
    for t in main:
        if not t.pad and not t.strong:
            for u in main:
                if u.pad and not u.strong and (u.style, u.prefix_len, u.suffix) == (t.style, t.prefix_len, t.suffix) \
                        and abs(u.h_rel - t.h_rel) <= SIZE_TOL * t.h_rel:
                    t.zero_len = u.pad

    # 주 번호 바로 앞뒤 값으로만 나오는 번호(색 배지의 대표문제 등)
    plain_main = [t for t in main if not t.strong]
    main_values = {p: {n.value for n in nums if any(t.matches(n) for t in plain_main)}
                   for p, nums in nums_by_page.items()}
    aux: list[Track] = []
    for track, cluster in rest:
        near = sum(1 for p, m in cluster
                   if m.value not in main_values[p] and any(abs(m.value - v) <= 2 for v in main_values[p]))
        if near >= 4 and near >= 0.7 * len(cluster):
            track.aux, track.continuity = True, round(near / len(cluster), 3)
            aux.append(track)
    return Profile(tracks=main + aux, sampled_pages=len(pages))


# ---------------------------------------------------------------- 쪽 검출

@dataclass
class Box:
    """문항 하나의 영역. 좌표는 검출 이미지의 픽셀(저장할 때 pt로 바꾼다)."""
    x0: int
    y0: int
    x1: int
    y1: int
    label: str
    value: int
    column: int
    stem: tuple[int, int, int, int] | None = None     # 공통 발문 영역
    choices: str = ""             # 박스 안에서 읽힌 선택지 번호(①~⑤)
    flags: list[str] = field(default_factory=list)
    plain: bool = True            # 한 줄로 이어지는 보통 번호인가(공통 발문 '[a~b]'는 이런 번호에만 딸린다)
    anchor: Num | None = field(default=None, repr=False, compare=False)     # 이 박스를 만든 번호


@dataclass
class Stem:
    """'[0461~0462] 다음 …' 같은 공통 발문의 머리."""
    tok: Token
    lo: int
    hi: int


@dataclass
class PageResult:
    boxes: list[Box]
    anchors: list[Num]
    columns: list[tuple[int, int]]
    em: float
    tokens: list[Token]


def _ax(t: Token) -> float:
    """번호가 어느 단에 속하는지 판단할 때 쓰는 x(번호의 왼쪽 글자 중심쯤)."""
    return t.x0 + 0.5 * min(t.h, t.x1 - t.x0)


def _column_cuts(lay: Layout, y_lo: int, y_hi: int) -> tuple[int, int, list[tuple[int, bool]]]:
    """내용의 좌우 끝과, 단을 가르는 x 위치들을 찾는다.

    단 구분은 (1) 길게 내려오는 세로선, (2) 글자 덩어리가 거의 걸치지 않는 세로 여백이다.
    꼬리말이나 제목처럼 한두 줄이 가로지르는 것은 무시한다.
    """
    H, W = lay.text.shape
    em = lay.em
    mx = int(MARGIN * W)
    diff = np.zeros(W + 1)
    rows = np.zeros(max(1, y_hi - y_lo), dtype=bool)
    for b in lay.blobs:
        if b.y1 <= y_lo or b.y0 >= y_hi or b.x1 <= mx or b.x0 >= W - mx:
            continue
        if b.w < 0.35 * em and b.h > 1.5 * em:      # 지워지지 않고 남은 세로선 조각
            continue
        a, z = max(b.y0, y_lo), min(b.y1, y_hi)
        diff[b.x0] += z - a
        diff[b.x1] -= z - a
        rows[a - y_lo:z - y_lo] = True
    cross = np.cumsum(diff)[:W]
    occ = cross > 0.06 * rows.sum()
    xs = np.flatnonzero(cross > 0)
    if len(xs) == 0:
        return mx, W - mx, []
    left, right = int(xs[0]), int(xs[-1]) + 1
    cuts: list[tuple[int, bool]] = []      # (x, 세로선인가)
    # (1) 세로선: 범위 높이의 35% 이상을 덮는 선
    cover = lay.rules_v[y_lo:y_hi].sum(axis=0)
    line_xs = np.flatnonzero(cover >= 0.35 * (y_hi - y_lo))
    if len(line_xs):
        for g in np.split(line_xs, np.flatnonzero(np.diff(line_xs) > 3) + 1):
            x = int(g.mean())
            if left + 3 * em < x < right - 3 * em:
                cuts.append((x, True))
    # (2) 세로 여백
    need = int(MIN_GUTTER_EM * em)
    start = None
    for x in range(left, right):
        if not occ[x]:
            start = x if start is None else start
        elif start is not None:
            if x - start >= need and not any(start - 2 <= c <= x + 2 for c, _ in cuts):
                cuts.append(((start + x) // 2, False))
            start = None
    return left, right, sorted(cuts)


def _columns(lay: Layout, marks: list[Token]) -> list[tuple[int, int]]:
    """번호가 있는 단들의 x 범위. 번호 없는 띠는 버리거나 왼쪽 단에 합친다."""
    H, W = lay.text.shape
    em = lay.em
    y_lo = max(0, min(t.y0 for t in marks) - int(em))
    y_hi = int(H * 0.97)
    left, right, cuts = _column_cuts(lay, y_lo, y_hi)
    # 번호 글자를 가로지르는 자리는 단의 경계가 아니다('유 제  07-3'처럼 글자 사이가 넓은 낱말)
    cuts = [c for c in cuts if not any(t.x0 < c[0] < t.x1 for t in marks)]
    left = min(left, min(t.x0 for t in marks))
    right = max(right, left + 1)
    edges = [left] + [c for c, _ in cuts] + [right]
    hard = [True] + [is_rule for _, is_rule in cuts]      # 각 단의 왼쪽 경계가 세로선인가

    def has_mark(x0: int, x1: int) -> bool:
        return any(x0 <= _ax(t) < x1 for t in marks)

    def tall(x0: int, x1: int) -> bool:
        rows = np.zeros(y_hi - y_lo, dtype=bool)
        for b in lay.blobs:
            if b.x0 >= x0 and b.x1 <= x1 and b.y1 > y_lo and b.y0 < y_hi:
                rows[max(0, b.y0 - y_lo):b.y1 - y_lo] = True
        return rows.mean() >= 0.25

    out: list[tuple[int, int]] = []
    for i in range(len(edges) - 1):
        x0, x1 = edges[i], edges[i + 1]
        if has_mark(x0, x1):
            out.append((x0, x1))
            continue
        if not out or out[-1][1] != x0:
            continue                       # 왼쪽에 붙은 번호 단이 없으면 문제 영역이 아니다
        px0, px1 = out[-1]
        central = 0.35 < (x0 - left) / (right - left) < 0.65
        if px1 - px0 < 8 * em:             # 번호(또는 큰 번호 배지)만 따로 서 있는 띠 + 본문
            out[-1] = (px0, x1)
        elif hard[i] or (central and tall(x0, x1)):
            continue                       # 번호 없는 별도의 단
        else:
            out[-1] = (px0, x1)
    return out


def _rows(anchors: list[Num]) -> list[list[Num]]:
    """세로로 겹치는 번호끼리 한 줄로 묶는다(한 단 안에 문제가 가로로 나란한 경우)."""
    rows: list[list[Num]] = []
    for a in sorted(anchors, key=lambda n: n.tok.y0):
        if rows:
            y0 = min(n.tok.y0 for n in rows[-1])
            y1 = max(n.tok.y1 for n in rows[-1])
            overlap = min(y1, a.tok.y1) - max(y0, a.tok.y0)
            if overlap >= 0.5 * min(a.tok.h, y1 - y0):
                rows[-1].append(a)
                continue
        rows.append([a])
    for r in rows:
        r.sort(key=lambda n: n.tok.x0)
    return rows


def _bands(has_ink: np.ndarray) -> list[tuple[int, int]]:
    """잉크가 있는 연속 행 구간들."""
    padded = np.concatenate(([False], has_ink, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2])]


def _trim_cell(lay: Layout, x0: int, x1: int, y0: int, y1: int, *,
               next_anchor: bool, clip: bool = False) -> tuple[int, int, int, int, list[str]]:
    """칸 안에서 실제 내용이 있는 범위로 줄인다. clip은 칸의 아래 끝이 다른 내용의 시작인지를 뜻한다."""
    H, W = lay.text.shape
    em = lay.em
    mx = int(MARGIN * W)
    x0, x1 = max(x0, mx), min(x1, W - mx)
    flags: list[str] = []
    if x1 <= x0 or y1 <= y0:
        return x0, y0, x1, y1, ["empty"]
    ink = lay.text[y0:y1, x0:x1] | lay.rules_h[y0:y1, x0:x1]
    bands = _bands(ink.sum(axis=1) > 1)
    if not bands:
        return x0, y0, x1, y1, ["empty"]
    keep = [bands[0]]
    for b in bands[1:]:
        gap = b[0] - keep[-1][1]
        # 큰 세로 여백(풀이 공간) 뒤나, 쪽 아래에 따로 떨어진 꼬리말은 문제 밖이다
        if gap >= GAP_MAX_EM * em or (y0 + b[0] >= FOOTER_Y * H and gap >= 0.8 * em):
            flags.append("gap_cut")
            break
        keep.append(b)
    # 칸이 다음 번호(또는 풀이 머리)에서 끝날 때, 칸 끝에 걸려 잘린 줄은 그 다음 줄의 윗부분이다
    # (큰 번호 글자보다 본문의 지수·괄호가 더 위로 올라온 경우)
    if clip and len(keep) >= 2 and keep[-1][1] >= (y1 - y0) - 1:
        keep.pop()
    # 다음 번호 바로 위에 붙은 줄(단원 태그 등)은 다음 문제의 머리이므로 뺀다
    if next_anchor:
        for _ in range(2):
            if len(keep) < 2:
                break
            below = (y1 - y0) - keep[-1][1]
            above = keep[-1][0] - keep[-2][1]
            if below < 1.0 * em and above >= max(1.2 * em, 2.0 * below) and keep[-1][1] - keep[-1][0] < 2.0 * em:
                keep.pop()
                flags.append("tag_cut")
            else:
                break
    top, bottom = keep[0][0], keep[-1][1]
    cols = np.flatnonzero(ink[top:bottom].sum(axis=0) > 0)
    pad = int(0.3 * em)
    bx0 = max(0, x0 + int(cols[0]) - pad) if len(cols) else x0
    bx1 = min(W, x0 + int(cols[-1]) + 1 + pad) if len(cols) else x1
    return bx0, max(0, y0 + top - pad), bx1, min(y1 if clip else H, y0 + bottom + pad), flags


def _inside(t: Token, outer: Token) -> bool:
    cx, cy = (t.x0 + t.x1) / 2, (t.y0 + t.y1) / 2
    return outer.x0 - 2 <= cx <= outer.x1 + 2 and outer.y0 - 2 <= cy <= outer.y1 + 2


def _find_anchors(em: float, tokens: list[Token], inline: list[Token], profile: Profile,
                  width: int, height: int) -> list[Num]:
    main_tracks = [t for t in profile.tracks if not t.aux]
    aux_tracks = [t for t in profile.tracks if t.aux]
    nums = [n for n in numbers_of(em, tokens) if n.value >= 1 and _in_body(n.tok, width, height)]
    anchors = [n for n in nums if any(t.matches(n) for t in main_tracks)]
    # 앞에 배지가 붙은 번호. 이미 잡힌 번호의 일부('157' 속의 '57')는 버린다
    for n in numbers_of(em, inline):
        if n.value >= 1 and _in_body(n.tok, width, height) and any(t.matches(n) for t in main_tracks) \
                and not any(_inside(n.tok, a.tok) for a in anchors):
            anchors.append(n)
    # 한 줄로 이어지는 보통 번호끼리는 값이 서로 가까워야 한다. 낱말이 붙었거나 '02-1' 꼴인 번호는 따로 둔다.
    plain = [a for a in anchors if a.plain]
    other = [a for a in anchors if not a.plain]
    # 같은 쪽의 다른 번호들과 동떨어진 값은 번호가 아니다(새 단원의 1번은 예외)
    if len(plain) >= 3:
        mid = float(np.median([a.value for a in plain]))
        plain = [a for a in plain if abs(a.value - mid) <= 3 * (len(plain) + 5)]
    if len(plain) >= 4:
        vals = [a.value for a in plain]
        plain = [a for a in plain
                 if a.value <= 2 or sum(1 for v in vals if 0 < abs(v - a.value) <= 3) >= 1]
    values = {a.value for a in plain}

    # 색 배지 안의 번호('0394 대표문제'): 주 번호와 자릿수가 맞고 값이 바로 앞뒤일 때만 받아들인다
    for n in nums:
        if n.tok.kind != "fill" or n.word:
            continue
        for t in main_tracks:
            if t.strong:
                continue
            digits = n.digits[:t.pad] if t.pad else n.digits[:4]
            if len(digits) < max(t.pad, 1) or (not t.pad and digits[0] == "0" and len(digits) != t.zero_len):
                continue
            v = int(digits)
            if v >= 1 and v not in values and any(abs(v - w) <= 3 for w in values):
                plain.append(Num(n.tok, "", digits, t.suffix, n.h_rel))
                values.add(v)
                break
    # 보조 양식의 번호
    for n in nums:
        if n.tok.kind != "fill" and n.plain and n.value not in values \
                and any(abs(n.value - v) <= 3 for v in values) and any(t.matches(n) for t in aux_tracks):
            plain.append(n)
            values.add(n.value)
    # '04-2' 꼴 번호를 앞 숫자를 0으로 채워 적는 교재에서, 앞 숫자의 자릿수가 다른 것('1-5')은 번호가 아니다.
    # 풀이 속 분수의 분자 `-1-5`가 줄 머리에 오면 번호처럼 읽혀, 풀이 한 토막이 문제 상자가 되곤 했다
    padded = {len(a.major) for a in other if a.major.startswith("0")}
    if len(padded) == 1:
        width_major = next(iter(padded))
        other = [a for a in other if not a.major or len(a.major) == width_major]
    # 같은 번호가 두 번 잡히면(배지와 그 안의 글자 등) 위쪽 것만 남긴다. 다만 쪽 머리의 단원 표시
    # ('Theme 11~12')에 든 숫자가 본문의 진짜 번호를 밀어내지 않도록, 쪽 맨 위 띠의 것은 뒤로 미룬다.
    seen: dict[tuple, Num] = {}
    for a in sorted(plain + other, key=lambda n: (n.tok.y0 < 0.09 * height, n.tok.y0, n.tok.x0)):
        seen.setdefault(a.key, a)
    return list(seen.values())


def _repair_sequence(lay: Layout, tokens: list[Token], anchors: list[Num], profile: Profile) -> list[Num]:
    """읽는 순서로 늘어놓았을 때 번호 하나가 비거나 틀린 자리를 메운다(보통 번호만).

    앞뒤 번호가 n, n+2이고 그 사이에 후보 토큰이 정확히 하나 있을 때만 그것을 n+1로 본다. 후보는
    (1) 글자는 못 읽었지만 번호와 모양(크기·색)이 같은 토큰(옅은 색의 '11' 등), (2) 모양은 어긋나도
    글자가 정확히 n+1로 읽힌 토큰(워터마크가 겹쳐 크기가 달라진 번호)이다.
    """
    em = lay.em
    H, W = lay.text.shape
    main_tracks = [t for t in profile.tracks if not t.aux and not t.strong and t.style == "line"]
    plain = [a for a in anchors if a.plain]
    other = [a for a in anchors if not a.plain]
    if len(plain) < 2 or not main_tracks:
        return anchors
    taken = [a.tok for a in anchors]
    cands: list[Num] = []
    for t in tokens:
        if t.kind != "line" or t.solid or t.x1 - t.x0 > layout.NUMBER_WIDTH_H * t.h or not _in_body(t, W, H) \
                or any(_inside(t, a) or _inside(a, t) for a in taken):
            continue
        text = _squash(t.text)
        m = NUM_RE.fullmatch(text)
        if m and m.group(3):
            continue                          # '1-2' 꼴은 한 줄로 이어지는 번호의 빈자리를 메우지 못한다
        if m and t.score >= 0.6:
            cands.append(Num(t, m.group(1).upper(), m.group(2), m.group(4), t.h / em, guessed=True))
        elif not re.search(r"[가-힣ㄱ-ㅎ①-⑮]", text) \
                and any(tr.matches_style(t.h / em, t.color) for tr in main_tracks):
            cands.append(Num(t, "", "0", "", t.h / em, guessed=True))
    columns = _columns(lay, taken)
    order: list[Num] = []
    for cx0, cx1 in columns:
        for row in _rows([n for n in plain + cands if cx0 <= _ax(n.tok) < cx1]):
            order.extend(row)
    out = list(plain)
    # 앞뒤 번호가 n, n+1로 이어지는 사이에 끼어든 엉뚱한 값은 번호가 아니다(단원 머리의 '유형 04' 등)
    real = [n for n in order if not n.guessed]
    for prev, n, nxt in zip(real, real[1:], real[2:]):
        if nxt.value == prev.value + 1 and not prev.value < n.value < nxt.value and n in out:
            out.remove(n)
            order.remove(n)
    real = [n for n in order if not n.guessed]
    for prev, nxt in zip(real, real[1:]):
        if nxt.value - prev.value != 2:
            continue
        want = prev.value + 1
        between = order[order.index(prev) + 1:order.index(nxt)]
        exact = [n for n in between if n.digits != "0" and n.value == want]      # 값이 정확히 읽힌 후보
        shaped = [n for n in between if n.digits == "0"]                         # 모양만 같은 후보
        pick = exact[0] if len(exact) == 1 else (shaped[0] if not exact and len(shaped) == 1 else None)
        if pick is not None:
            width = len(prev.digits) if prev.digits[0] == "0" or len(prev.digits) == len(nxt.digits) else 0
            out.append(Num(pick.tok, prev.prefix, str(want).zfill(width), prev.suffix, pick.h_rel, guessed=True))
    # 번호 자리인데 값이 틀리게 읽힌 것('11'을 '17'로): 앞뒤가 n, n+2이면 n+1로 고친다
    for prev, n, nxt in zip(real, real[1:], real[2:]):
        if nxt.value - prev.value == 2 and n.value != prev.value + 1 and n in out:
            out[out.index(n)] = Num(n.tok, n.prefix, str(prev.value + 1).zfill(len(n.digits)), n.suffix,
                                    n.h_rel, guessed=True)
    return out + other


def _repair_labels(em: float, tokens: list[Token], anchors: list[Num]) -> list[Num]:
    """낱말은 읽혔는데 뒤의 번호를 못 읽은 것('유제'만 읽힘)에, 같은 줄맞춤의 앞뒤 번호로 값을 매긴다."""
    out = list(anchors)
    for word in {a.word for a in anchors if a.word}:
        group = [a for a in anchors if a.word == word]
        cands = [t for t in tokens if t.kind == "line" and t.score >= 0.8 and _squash(t.text).upper() == word
                 and not any(_inside(t, a.tok) or _inside(a.tok, t) for a in anchors)]
        for t in sorted(cands, key=lambda t: t.y0):
            near = [a for a in group if abs(a.tok.x0 - t.x0) <= 0.6 * em]
            above = [a for a in near if a.tok.y0 < t.y0]
            below = [a for a in near if a.tok.y0 > t.y0]
            if above:
                ref = max(above, key=lambda a: a.tok.y0)
                value = ref.value + 1
            elif below:
                ref = min(below, key=lambda a: a.tok.y0)
                value = ref.value - 1
            else:
                continue
            digits = str(value).zfill(len(ref.digits) if ref.digits[0] == "0" else 0)
            n = Num(t, "", digits, ref.suffix, t.h / em, guessed=True, word=word, major=ref.major)
            if value >= 1 and not any(a.key == n.key for a in out):
                out.append(n)
                group.append(n)
    return out


def _find_stems(tokens: list[Token], anchors: list[Num]) -> list[Stem]:
    values = [a.value for a in anchors if a.plain]
    out = []
    for t in tokens:
        if t.kind != "line":
            continue
        m = STEM_RE.match(_squash(t.text))
        if not m:
            continue
        lo, hi = int(m.group(2)), int(m.group(4))
        if lo < hi <= lo + 12 and any(lo <= v <= hi for v in values):
            out.append(Stem(t, lo, hi))
    return out


def _solution_heads(tokens: list[Token]) -> list[Token]:
    """'풀이', 'SOLUTION'처럼 풀이가 시작되는 자리의 토큰."""
    out = []
    for t in tokens:
        if t.kind in ("line", "fill") and t.score >= 0.8:
            word = re.sub(r"[\s\[\]()<>【】|:·.]", "", t.text).upper()
            if word in SOLUTION_WORDS:
                out.append(t)
    return out


def _lonely(anchors: list[Num]) -> list[Num]:
    """같은 쪽에 앞뒤 번호가 없어, 번호열의 일부라는 증거가 없는 번호들."""
    out = []
    for a in anchors:
        if a.guessed:
            continue
        if a.major:
            alone = not any(b is not a and b.major and b.word == a.word for b in anchors)
        else:
            alone = not any(b.word == a.word and not b.major and abs(b.value - a.value) == 1 for b in anchors)
        if alone:
            out.append(a)
    return out


def _reads_like_problem(lay: Layout, b: "Box", max_lines: int = 14) -> bool:
    """박스가 문제처럼 보이는가: 선택지나 공통 발문이 있거나, 글에 문제 말투가 나온다.

    긴 문제는 '구하시오'가 끝에 나오고 그 뒤에 그림이 붙기도 하므로 앞줄과 끝줄을 함께 읽는다.
    한꺼번에 다 읽지 않고 조금씩 읽다가 말투가 나오면 멈춘다.
    """
    if len(b.choices) >= 3 or b.stem is not None:
        return True
    crops = layout.line_crops(lay, b.x0, b.y0, b.x1, b.y1, max_lines=max_lines, tail=4)
    for i in range(0, len(crops), 12):
        if any(score >= 0.5 and CUE_RE.search(_squash(text)) for text, score in ocr.read_crops(crops[i:i + 12])):
            return True
    return False


def detect_page(lay: Layout, tokens: list[Token], profile: Profile,
                inline: list[Token] | None = None, verify: bool = True) -> PageResult:
    """읽어 둔 토큰과 번호 양식으로 한 쪽의 문항 박스를 만든다.

    verify가 참이면, 같은 쪽에 앞뒤 번호 없이 혼자 나온 번호는 박스의 글을 조금 읽어 문제 말투가 있을 때만
    남긴다(단원 머리의 큰 번호, 개념 설명 속의 번호 등을 거른다).
    """
    H, W = lay.text.shape
    em = lay.em
    anchors = _find_anchors(em, tokens, inline or [], profile, W, H)
    if not anchors:
        return PageResult([], [], [], em, tokens)
    anchors = _repair_labels(em, tokens, _repair_sequence(lay, tokens, anchors, profile))
    ends = _solution_heads(tokens)
    y_floor = int(H * 0.97)
    pad_top = int(0.15 * em)

    def build(anchors: list[Num]) -> tuple[list[Box], list[tuple[int, int]]]:
        stems = _find_stems(tokens, anchors)
        columns = _columns(lay, [a.tok for a in anchors] + [s.tok for s in stems])
        boxes: list[Box] = []
        stem_rects: list[tuple[Stem, tuple[int, int, int, int]]] = []
        for ci, (cx0, cx1) in enumerate(columns):
            col_anchors = [a for a in anchors if cx0 <= _ax(a.tok) < cx1]
            col_stems = sorted((s for s in stems if cx0 <= _ax(s.tok) < cx1), key=lambda s: s.tok.y0)
            col_ends = [t.y0 for t in ends if cx0 <= t.x0 < cx1]
            rows = _rows(col_anchors)
            row_tops = [min(n.tok.y0 for n in r) for r in rows]

            def next_break(y: int) -> tuple[int, bool]:
                """y 아래에서 처음 나오는 번호 줄·공통 발문·풀이 머리의 위쪽. 뒤의 값은 그것이 다음 문제인가."""
                below = [v for v in row_tops + [s.tok.y0 for s in col_stems] if v > y + 2]
                y_next, is_next = (min(below), True) if below else (y_floor, False)
                cut = [v for v in col_ends if y + 0.5 * em < v < y_next]
                return (min(cut), False) if cut else (y_next, is_next)

            for s in col_stems:
                y_end, _ = next_break(s.tok.y0)
                x0, y0, x1, y1, _ = _trim_cell(lay, cx0, cx1, max(0, s.tok.y0 - pad_top), y_end, next_anchor=False,
                                               clip=y_end < y_floor)
                stem_rects.append((s, (x0, y0, x1, y1)))
            for ri, row in enumerate(rows):
                y_next, has_next = next_break(row_tops[ri])
                for ai, a in enumerate(row):
                    left = cx0 if ai == 0 else a.tok.x0 - int(0.3 * em)
                    right = row[ai + 1].tok.x0 - int(0.3 * em) if ai + 1 < len(row) else cx1
                    x0, y0, x1, y1, flags = _trim_cell(lay, left, right, max(0, a.tok.y0 - pad_top), y_next,
                                                        next_anchor=has_next, clip=y_next < y_floor)
                    box = Box(x0, y0, x1, y1, a.label, a.value, ci, flags=flags, plain=a.plain)
                    box.anchor = a
                    boxes.append(box)
        for b in boxes:
            for s, rect in stem_rects:
                if b.plain and s.lo <= b.value <= s.hi:
                    b.stem = rect
            # 박스 안의 선택지 번호
            seen = {t.text.strip()[:1] for t in tokens
                    if b.x0 <= t.x0 < b.x1 and b.y0 <= t.y0 < b.y1 and t.text.strip()[:1] in CIRCLED}
            b.choices = "".join(c for c in CIRCLED if c in seen)
            if "①" in seen and "⑤" not in seen and "④" not in seen:
                b.flags.append("choices_cut")
        return boxes, columns

    boxes, columns = build(anchors)
    if verify:
        lonely = _lonely(anchors)
        checked = {id(b.anchor): _reads_like_problem(lay, b) for b in boxes if any(b.anchor is a for a in lonely)}
        drop = [key for key, good in checked.items() if not good]
        if drop:
            anchors = [a for a in anchors if id(a) not in drop]
            if not anchors:
                return PageResult([], [], [], em, tokens)
            boxes, columns = build(anchors)
        # 남은 박스 가운데 문제처럼 읽히는 것이 하나도 없으면 문제 쪽이 아니다(책 뒤의 정답·풀이 쪽은
        # 번호 모양이 문제 쪽과 같다). 선택지나 공통 발문이 보이면 읽지 않고 넘어간다.
        if not any(checked.get(id(b.anchor)) or len(b.choices) >= 3 or b.stem is not None for b in boxes):
            rest = [b for b in boxes if id(b.anchor) not in checked][:3]
            if not any(_reads_like_problem(lay, b) for b in rest):
                return PageResult([], [], [], em, tokens)
    return PageResult(boxes, anchors, columns, em, tokens)
