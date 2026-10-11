"""쪽 그림을 보고 답해야 하는 물음을 에이전트의 가벼운 서브에이전트에게 맡기는 읽기 담당.

교재의 뼈대(outline)는 `reader.ask(지시문, 그림) → 답`으로 짜여 있다. 여기서는 그 물음을 파일로 적어 두고,
서브에이전트들이 답을 파일로 써 주면 그 답을 돌려준다. 답이 아직 없는 물음을 만나면 Pending을 던진다.
부르는 쪽은 그때까지 쌓인 물음을 일(job)로 묶어 에이전트에게 넘기고, 답이 오면 같은 일을 처음부터 다시 돌린다.
이미 답이 있는 물음은 곧바로 답이 나오므로 다시 묻지 않는다. 앞의 답을 봐야 정해지는 물음(구획 제목을 다시
읽을 쪽)이 있어서 두세 번에 걸쳐 묻게 된다.

    작업 폴더/hwpick_data/outline/<교재>/asks.json      지금까지 맡긴 일들
    …/r1/job-01.md, answer-01.md                         첫 번째로 맡긴 일의 지시문과 답
    …/q/<물음>.jpg                                        물음의 그림
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import threading
import time
from pathlib import Path

SECTION_RE = re.compile(r"(?m)^[ \t]*@@[ \t]*\[?(\d+)\]?[ \t]*$")
TOC_FIRST = 16             # 차례는 책 앞쪽에 있다. 첫 번째로 맡길 때 앞의 이 쪽들을 차례로 읽혀 둔다(차례가 아니면 `없음`)
SETTLE = 2.0               # 답 파일이 다 쓰였는지 보려고 잠깐 더 기다리는 시간(초)
STALL = 45.0               # 답이 이만큼 더 오지 않으면 남은 서브에이전트는 멈춘 것으로 본다(초)

# 구획 제목을 다시 읽힐 때 제목 다음의 첫 문제 번호도 같이 적게 한다. 구획이 쪽 중간에서 시작하는 교재에서 앞뒤
# 구획을 가르는 데 쓰는 번호(outline.boundary)를, 나중에 따로 묻지 않고 이 답에서 꺼내려는 것이다
NAME_FIRST_PROMPT = """첨부한 그림은 수학 교재의 한 쪽입니다. 이 쪽에서 새로 시작하는 구획(문제 묶음)의 제목을 위에서부터 차례로 한 줄에 하나씩, 아래 형식으로 적으세요.

    제목 | 그 제목 바로 다음에 나오는 첫 문제의 번호

- 제목: 앞에 붙은 큰 글자 표시(A, B, C, STEP 1 등)까지 보이는 그대로 적습니다(예: `A 기본 다잡기`, `STEP 2 유형 익히기`, `연습문제`).
- 첫 문제의 번호: 쪽에 인쇄된 그대로 적습니다(예: `0040`, `01`, `1-1`). 그 제목 아래에 문제가 보이지 않으면 `-`.

개념 번호가 붙은 소제목(`01-4 …`)이나 유형 하나하나의 제목(`유형 03 …`)은 적지 않습니다. 그런 제목이 없으면 `없음` 이라고만 적으세요. 설명은 붙이지 마세요.
"""

JOB = """아래 [그림]들을 `view_file`로 **한 턴에 함께** 열고, 그림마다 [물음]에 답하세요. 그림 {count}장에 같은 물음입니다.

[물음]
{prompt}
[그림]
{images}

[저장]
답은 모두 아래 파일 하나에 `write_to_file`로 저장합니다. 그림마다 `@@ 번호` 줄을 쓰고(번호는 위 [그림]의 번호), 그 아래에 그 그림의 답을 적습니다. 번호를 건너뛰지 않습니다.
{out}

저장하는 꼴:
@@ 1
(1번 그림의 답)
@@ 2
(2번 그림의 답)
"""


def enabled(workspace) -> bool:
    """서브에이전트에게 교재의 뼈대를 읽히는가. 기본은 읽힌다.

    문제집 세 권에서 단원과 구획의 시작 쪽이 모두 맞았다(2026-10-11). 개념 설명과 예제가 섞인 개념서에서는 구획의
    시작 쪽이 틀리는 곳이 있어, 뼈대의 출력에 그 사실을 적고 에이전트가 고른 쪽 범위와 문제 번호를 보고하게 한다.
    끄려면 작업 폴더 설정에 "outline": "off" 를 적거나 환경 변수 HWPICK_OUTLINE=off 를 준다(그러면 쪽 범위를 묻는다).
    """
    import os
    env = os.environ.get("HWPICK_OUTLINE", "").strip().lower()
    if env:
        return env not in ("off", "0", "false", "no")
    try:
        from . import tasks
        cfg = json.loads((tasks.data_dir(workspace) / "config.json").read_text(encoding="utf-8"))
        return cfg.get("outline") != "off"
    except (OSError, ValueError):
        return True


class Pending(Exception):
    """답이 아직 없는 물음이 있다. 서브에이전트에게 맡겨야 한다."""


def _key(prompt: str, image: bytes) -> str:
    return hashlib.sha1(prompt.encode("utf-8") + b"\0" + image).hexdigest()[:16]


class PaperReader:
    """`ask(지시문, 그림) → 답`의 꼴로 쓰는, 서브에이전트가 써 준 답을 돌려주는 읽기 담당."""

    provider, model = "subagent", "flash_lite"

    def __init__(self, folder: Path, strict: bool = True):
        self.dir = Path(folder)
        self.strict = strict           # False면 답이 없는 물음에 `없음`을 돌려준다(새로 맡기지 않는다)
        self.cost = 0.0
        self.calls = 0
        self.lock = threading.Lock()
        self.asked: dict[str, dict] = {}                 # 이번에 답이 없던 물음: 열쇠 → {"prompt", "image"}
        self.state = self._state()
        self.answers = self._answers()

    # ---- 맡긴 일과 받은 답

    def _state(self) -> dict:
        try:
            return json.loads((self.dir / "asks.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"round": 0, "jobs": [], "waits": 0}

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "asks.json").write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    @staticmethod
    def _sections(path: str) -> dict[int, str]:
        """답 파일을 `@@ 번호` 줄로 나눈다: {번호: 답}. 파일이 없으면 빈 것."""
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, ValueError):
            return {}
        marks = list(SECTION_RE.finditer(text))
        return {int(m.group(1)): text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)].strip()
                for i, m in enumerate(marks)}

    def _answers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for job in self.state["jobs"]:                   # 뒤에 맡긴 일의 답이 앞의 것을 덮는다
            if not Path(job["out"]).is_file():
                continue
            # 답 파일은 서브에이전트가 끝에 한 번에 쓴다. 파일이 있으면 그 일은 끝난 것이고, 빠뜨린 번호는 빈 답으로 친다
            # (물음 하나 때문에 다시 맡기면 주 에이전트가 한 바퀴를 더 돈다)
            got = self._sections(job["out"])
            for n, key in enumerate(job["keys"], 1):
                if n in got or key not in out:
                    out[key] = got.get(n, "")
        return out

    def reload(self) -> None:
        self.state = self._state()
        self.answers = self._answers()

    def unfinished(self) -> list[dict]:
        """가장 최근에 맡긴 일들 가운데 답이 다 오지 않은 것."""
        last = [j for j in self.state["jobs"] if j["round"] == self.state["round"]]
        return [j for j in last if not Path(j["out"]).is_file()]

    def wait(self, seconds: float) -> list[dict]:
        """맡긴 일의 답이 다 올 때까지 기다린다(최대 seconds초). 끝나지 않은 일들을 돌려준다."""
        end = time.time() + seconds
        left = self.unfinished()
        total = sum(1 for j in self.state["jobs"] if j["round"] == self.state["round"])
        count, changed = len(left), time.time()
        while left and time.time() < end:
            time.sleep(1.0)
            left = self.unfinished()
            if len(left) != count:
                count, changed = len(left), time.time()
            elif len(left) <= total / 2 and time.time() - changed > STALL:
                break                                    # 절반 넘게 왔는데 한동안 더 오지 않는다: 멈춘 것으로 보고 그만 기다린다
        if not left and self.state["jobs"]:
            time.sleep(SETTLE)
        self.reload()
        return left

    # ---- 묻기

    def _take(self, prompt: str, image: bytes) -> str:
        key = _key(prompt, image)
        with self.lock:
            if key in self.answers:
                self.calls += 1
                return self.answers[key]
            if not self.strict:
                return "없음"
            path = self.dir / "q" / f"{key}.jpg"
            if not path.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(image)
            self.asked[key] = {"prompt": prompt, "image": str(path)}
        raise Pending(key)

    def ask(self, prompt: str, image: bytes | None = None, deadline: float | None = None, mime: str = "image/jpeg",
            hedge=None, variant=None) -> str:
        from . import outline
        if image is None:
            raise ValueError("그림 없는 물음은 서브에이전트에게 맡기지 않습니다")
        if prompt == outline.NAME_PROMPT:
            # 제목만 돌려준다(첫 문제 번호는 boundary 물음이 올 때 꺼내 쓴다)
            lines = [line.split("|")[0].strip() for line in self._take(NAME_FIRST_PROMPT, image).splitlines()]
            return "\n".join(line for line in lines if line)
        head, _, tail = outline.BOUNDARY_PROMPT.partition("{section}")
        if prompt.startswith(head) and prompt.endswith(tail):
            section = prompt[len(head):len(prompt) - len(tail)]
            for line in self._take(NAME_FIRST_PROMPT, image).splitlines():
                parts = [s.strip().strip("`").strip() for s in line.split("|")]
                if len(parts) >= 2 and outline._same_stage(parts[0], section):
                    return parts[1] if re.search(r"\d", parts[1]) else "없음"
            return "없음"
        return self._take(prompt, image)

    # ---- 답이 없던 물음을 일로 묶기

    def jobs(self) -> list[str]:
        """이번에 답이 없던 물음들을 서브에이전트의 일로 묶어 지시문 파일들을 만든다. 지시문 파일의 경로들."""
        from . import outline
        with self.lock:
            asked, self.asked = self.asked, {}
        if not asked:
            return []
        self.state["round"] += 1
        self.state["waits"] = 0
        rnd = self.state["round"]
        folder = self.dir / f"r{rnd}"
        folder.mkdir(parents=True, exist_ok=True)
        groups: dict[str, list[str]] = {}
        for key in sorted(asked):
            groups.setdefault(asked[key]["prompt"], []).append(key)
        files: list[str] = []
        for prompt, keys in groups.items():
            head = prompt.rstrip("\n")
            # 여러 쪽을 모은 그림과 차례는 답이 길어 조금씩, 한 줄로 답하는 물음은 많이 묶는다
            size = 4 if head == outline.PROMPT.rstrip("\n") else 5 if head == outline.TOC_PROMPT.rstrip("\n") else 10
            for i in range(0, len(keys), size):
                part = keys[i:i + size]
                n = len(files) + 1
                job, out = folder / f"job-{n:02d}.md", folder / f"answer-{n:02d}.md"
                images = "\n".join(f"{k}. {asked[key]['image']}" for k, key in enumerate(part, 1))
                job.write_text(JOB.format(count=len(part), prompt=prompt.rstrip() + "\n", images=images, out=out), encoding="utf-8")
                self.state["jobs"].append({"round": rnd, "name": job.stem, "file": str(job), "out": str(out), "keys": part})
                files.append(str(job))
        self._save()
        return files

    def waited(self) -> int:
        self.state["waits"] = self.state.get("waits", 0) + 1
        self._save()
        return self.state["waits"]

    def forget(self) -> None:
        """맡겼던 일의 기록을 지운다(뼈대를 처음부터 다시 만들 때). 답 파일은 남지만 더는 쓰이지 않는다."""
        self.state = {"round": self.state.get("round", 0), "jobs": [], "waits": 0}
        self.answers = {}
        if self.dir.is_dir():
            self._save()


def warm(idx, paper: PaperReader) -> None:
    """앞의 답에 기대지 않는 물음을 미리 물어 둔다(쪽 번호 확인, 책 앞쪽의 차례). 맡기는 횟수를 줄이려는 것이다."""
    from . import outline
    try:
        outline._offset(idx, paper, None)
    except Pending:
        pass
    for p in range(min(TOC_FIRST, idx.page_count)):
        try:
            paper.ask(outline.TOC_PROMPT, idx.page_file(p).read_bytes(), None, mime="image/jpeg", variant=outline.CAREFUL)
        except Pending:
            pass


# ---- 가벼운 모델에게 읽혀 뼈대 만들기
#
# 모델에게는 보이는 것을 적는 일만 시키고, 무엇이 구획인지는 엔진이 정한다.
#   ① 차례(세 번), 쪽 번호(두 번), 쪽의 종류와 구획 이름표(여덟 쪽씩 모은 그림 한 번), 쪽마다의 큰 제목(쪽 그림 두 번)
#   ② 같은 글자의 큰 제목이 여러 단원에 되풀이되면 구획이다. 한 쪽에서 셋 가운데 둘 이상이 그 제목을 읽으면 거기서 시작한다
#   ③ 다른 단원에는 있는데 어떤 단원에서만 시작 쪽이 안 잡힌 구획은, 그 이름을 대고 "이 쪽에 이 제목이 있는가"를 세 번 더 묻는다
# 그림마다 왼쪽 위에 빨간 쪽 번호를 찍고 답의 맨 앞에 그 번호를 적게 한다: 가벼운 모델은 그림 여러 장의 답을 서로
# 바꿔 적는 일이 있어서, 답이 적힌 자리(몇 번째)가 아니라 그 번호로 쪽을 가린다.

TITLE_PROMPT = """첨부한 그림은 수학 교재의 한 쪽입니다. 왼쪽 위에 빨간 숫자로 쪽 번호가 적혀 있습니다.
이 쪽에서 **본문 글자보다 훨씬 큰 글씨나 꾸민 글자로 된 큰 제목**을 찾으세요. 문제 묶음(단계)이 새로 시작할 때 붙는 제목이고, 큰 알파벳(A, B, C)이나 STEP 같은 표시가 그림처럼 붙어 있을 수 있습니다. 답은 아래 형식의 한 줄입니다.

    빨간 숫자 | 큰 제목 | 그 제목 바로 아래 처음 나오는 문제의 번호

- 큰 제목은 표시까지 붙여 보이는 그대로 적습니다(예: `B 유형 뽀개기`, `STEP 2 유형 익히기`, `연습문제`). 문제의 번호는 인쇄된 그대로 적고, 없으면 `-`.
- 큰 제목이 둘이면 위에서부터 한 줄에 하나씩, 줄마다 빨간 숫자부터 적습니다.
- 작은 소제목(`유형 01 …`, `01-2 …`)과 쪽 귀퉁이의 작은 머리글은 큰 제목이 아닙니다. 적지 않습니다.
- 큰 제목이 없으면 `빨간 숫자 | 없음` 이라고만 적습니다.

설명은 붙이지 마세요.
"""
FIND_PROMPT = """첨부한 그림은 수학 교재의 한 쪽입니다. 왼쪽 위에 빨간 숫자로 쪽 번호가 적혀 있습니다.
이 쪽에 `{name}` 이라는 큰 제목이 있는지 찾으세요. 본문 글자보다 훨씬 큰 글씨나 꾸민 글자로 되어 있고, 앞의 큰 글자 표시는 그림처럼 그려져 있을 수 있습니다. 쪽 귀퉁이에 작게 적힌 머리글은 세지 않습니다. 답은 아래 형식의 한 줄입니다.

    빨간 숫자 | 있음 | 그 제목 바로 아래 처음 나오는 문제의 번호(없으면 -)

그 제목이 없으면 `빨간 숫자 | 없음` 이라고만 적습니다. 비슷하지만 다른 제목은 `없음`입니다. 설명은 붙이지 마세요.
"""
TOC_READS = 3              # 차례는 세 번 읽혀, 둘 이상이 같게 읽은 줄만 쓴다
NUMBER_READS = 2           # 쪽 번호 확인
TITLE_READS = 2            # 쪽의 큰 제목(쪽 그림). 여덟 쪽씩 모은 그림에서 읽힌 이름표까지 세 표다
FIND_READS = 3             # 시작 쪽이 안 잡힌 구획을 이름을 대고 찾는 물음
GAP_PAGES = 8              # 그때 읽힐 쪽의 수(그 구획이 다른 단원에서 보통 시작하는 자리의 둘레)
# 큰 제목으로 잘못 읽혀 오는 소제목: 유형·개념·예제의 번호 제목, 개념 번호(01-2), 문제 번호만 있는 것
SUBTITLE_RE = re.compile(r"^\s*(?:(?:대표\s*)?유형|개념|필수\s*예제|예제|유제|확인\s*문제|문제)\s*\d|^\s*\d{1,2}\s*[-–]\s*\d|^\s*\d{3,}\s*$")


class NoOutline(Exception):
    """읽힌 것으로는 뼈대를 만들 수 없다(차례가 없거나 쪽 번호를 맞추지 못했다)."""


def _again(prompt: str, r: int) -> str:
    """같은 물음을 따로 한 번 더 읽히려고, 끝에 빈 줄을 붙여 다른 물음으로 만든다."""
    return prompt + "\n" * r


def stamped(idx, p: int) -> bytes:
    """p쪽(1부터)의 그림 왼쪽 위에 빨간 쪽 번호를 찍은 JPEG."""
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(idx.page_file(p - 1).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    cv2.rectangle(img, (0, 0), (170, 74), (255, 255, 255), -1)
    cv2.putText(img, str(p), (8, 58), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 255), 5)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


def _lines(answers) -> dict[int, list[list[str]]]:
    """한 번의 읽기에서 받은 답들을 줄마다 빨간 숫자로 가른다: {쪽: [그 뒤의 칸들…]}."""
    out: dict[int, list[list[str]]] = {}
    for ans in answers:
        for line in (ans or "").splitlines():
            parts = [s.strip().strip("`").strip() for s in line.strip().strip("|").split("|")]
            m = re.fullmatch(r"\D{0,3}(\d{1,4})\D{0,2}", parts[0]) if parts else None
            if m and len(parts) >= 2:
                out.setdefault(int(m.group(1)), []).append(parts[1:])
    return out


def _vote_toc(reads: list[str]) -> list[dict]:
    """차례 한 쪽을 여러 번 읽은 답에서, 둘 이상이 같게 읽은 줄만 고른다: [{"path": […], "printed": 쪽}]."""
    from . import outline
    parsed = []
    for text in reads:
        rows = []
        for line in text.splitlines():
            parts = outline._cells(line)
            if len(parts) < 2 or not re.fullmatch(r"\d{1,3}", parts[-1]):
                continue
            path = [s.strip() for s in " | ".join(parts[:-1]).replace("|", ">").split(">") if s.strip() not in outline.NONE_WORDS]
            if path:
                rows.append((path, int(parts[-1])))
        parsed.append(rows)
    need = 2 if len(reads) >= 2 else 1
    out, done = [], set()
    for rows in parsed:
        for path, printed in rows:
            if printed in done:
                continue
            alike = [p for other in parsed for p, n in other if n == printed and outline.same(p[-1], path[-1])]
            if len(alike) < need:
                continue
            done.add(printed)
            texts = [" > ".join(p) for p in alike]
            best = max(sorted(set(texts)), key=texts.count)          # 가장 여럿이 똑같이 적은 표기
            out.append({"path": best.split(" > "), "printed": printed})
    return sorted(out, key=lambda u: u["printed"])


def light(idx, paper: PaperReader) -> dict:
    """서브에이전트가 써 준 답으로 뼈대를 만들어 저장한다. 답이 모자라면 물음을 쌓아 두고 Pending을 던진다."""
    from . import outline
    n = idx.page_count
    waiting = False
    plain: dict[int, bytes] = {}
    marked: dict[int, bytes] = {}

    def page(p: int) -> bytes:                           # p는 1부터
        if p not in plain:
            plain[p] = idx.page_file(p - 1).read_bytes()
        return plain[p]

    def mark(p: int) -> bytes:
        if p not in marked:
            marked[p] = stamped(idx, p)
        return marked[p]

    def take(prompt: str, image: bytes) -> str | None:
        nonlocal waiting
        try:
            return paper._take(prompt, image)
        except Pending:
            waiting = True
            return None

    # ① 서로 기대지 않는 물음
    toc = {p: [take(_again(outline.TOC_PROMPT, r), page(p)) for r in range(TOC_READS)] for p in range(1, min(TOC_FIRST, n) + 1)}
    probes = sorted({int(n * f) + 1 for f in (0.2, 0.3, 0.45, 0.55, 0.7)})
    numbers = {p: [take(_again(outline.PAGE_NUMBER_PROMPT, r), page(p)) for r in range(NUMBER_READS)] for p in probes}
    groups = [list(range(i, min(i + outline.PER_SHEET, n))) for i in range(0, n, outline.PER_SHEET)]
    sheets = [take(outline.PROMPT, outline.sheet(idx, g)) for g in groups]
    seen = [_lines(take(_again(TITLE_PROMPT, r), mark(p)) for p in range(1, n + 1)) for r in range(TITLE_READS)]
    if waiting:
        raise Pending("first")

    # 단원: 차례에서. 쪽 번호가 뒤로 가는 줄은 잘못 읽은 것이다
    units: list[dict] = []
    for p in sorted(toc):
        for u in _vote_toc(toc[p]):
            if not units or u["printed"] > units[-1]["printed"]:
                units.append(u)
    if len(units) < 2:
        raise NoOutline("차례에서 단원을 읽지 못했습니다")
    units = outline._tidy_paths(units)
    diffs = []
    for p, answers in numbers.items():
        for ans in answers:
            m = re.fullmatch(r"\D*(\d{1,3})\D*", ans.strip())
            if m:
                diffs.append(p - int(m.group(1)))
    offset = max(set(diffs), key=diffs.count) if diffs else None
    if offset is None or diffs.count(offset) < 2:
        raise NoOutline("교재의 쪽 번호와 파일의 쪽 번호를 맞추지 못했습니다")
    starts_of_units = [u["printed"] + offset for u in units]

    def unit_of(p: int) -> int:
        return sum(1 for s in starts_of_units if s <= p)

    # 쪽의 종류는 책 뒤의 정답·풀이 모음을 가려내는 데만 쓴다(쪽 하나하나의 판정은 믿지 않는다)
    pages: list[list[str]] = [["문제", "", ""] for _ in range(n)]
    labels: dict[int, str] = {}                          # 여덟 쪽씩 모은 그림에서 읽힌 구획 이름표
    for g, ans in zip(groups, sheets):
        for p0, (kind, _, sec) in outline.parse(ans, g).items():
            pages[p0][0] = kind
            if sec:
                labels[p0 + 1] = sec
    end_of_body = outline._body_end(pages)
    for p0 in range(n):
        pages[p0][0] = "해설" if p0 >= end_of_body else "문제"
    bounds_of_units = starts_of_units + [end_of_body + 1]
    body = range(max(1, starts_of_units[0]), end_of_body + 1)

    # ② 구획: 같은 글자의 큰 제목이 여러 단원에 되풀이되는 것. 읽기마다 (쪽, 읽기) → 제목들
    read: dict[tuple[int, str], list[tuple[str, str]]] = {}
    for r, lines in enumerate(seen):
        for p in body:
            for cells in lines.get(p, []):
                if cells[0] and cells[0] not in outline.NONE_WORDS and not SUBTITLE_RE.search(cells[0]):
                    number = cells[1] if len(cells) > 1 and re.search(r"\d", cells[1]) else ""
                    read.setdefault((p, f"t{r}"), []).append((cells[0], number))
    for p in body:
        if p in labels and not SUBTITLE_RE.search(labels[p]):
            read.setdefault((p, "s"), []).append((labels[p], ""))
    # 같은 글자(차례 표시와 띄어쓰기는 뺀다)의 큰 제목끼리 모은다. 표시가 여러 가지로 붙어 나오는 글자는 따로 본다:
    #   표시가 넷 넘게 나오면 번호가 붙은 소제목 줄('필수 01', '필수 05' …)이라 구획이 아니다
    #   둘이나 셋이면 표시마다 다른 구획이다('STEP 1', 'STEP 2'). 그때 표시가 안 읽힌 것은 어느 쪽인지 몰라 버린다
    by_text: dict[str, dict[tuple, set]] = {}
    for key, titles in read.items():
        for name, _ in titles:
            text = outline._text(name)
            if len(text) >= 2:
                by_text.setdefault(text, {}).setdefault(tuple(outline._indexes(name)), set()).add(key)
    groups_by_text: dict[str, set[tuple[int, str]]] = {}
    for text, variants in by_text.items():
        marks = [m for m in variants if m]
        if len(marks) >= 4:
            continue
        if len(marks) <= 1:
            groups_by_text[text] = set().union(*variants.values())
        else:
            for m in marks:
                groups_by_text[text + "#" + "/".join(m)] = set(variants[m])

    def fits(name: str, stage: str) -> bool:
        base, _, mark = stage.partition("#")
        return outline._alike(outline._text(name), base, 0.8) and (not mark or "/".join(outline._indexes(name)) == mark)

    need = max(2, -(-3 * len(units) // 10))              # 단원 열에 셋은 되어야 한다(적어도 둘)
    real = {text: set(g) for text, g in groups_by_text.items() if len({unit_of(k[0]) for k in g if unit_of(k[0]) >= 1}) >= need}
    for text in list(real):                              # '유형 익히기'가 '유형'으로 잘려 읽힌 것은 구획이 아니다
        base = text.partition("#")[0]
        if len(base) <= 3 and any(x != text and x.partition("#")[0].startswith(base) for x in real):
            del real[text]
    for text, g in groups_by_text.items():               # 되풀이되지 않은 것: 아주 비슷한 구획이 있으면 거기에 붙인다
        if text not in real and "#" not in text:
            alike = [x for x in real if "#" not in x and outline._alike(text, x, 0.8)]
            if alike:
                real[max(alike, key=lambda x: difflib.SequenceMatcher(None, text, x).ratio())] |= g
    known: dict[str, str] = {}
    votes: dict[tuple[int, str], int] = {}               # (쪽, 구획) → 그 쪽에서 그 구획 제목을 읽은 읽기의 수
    rank: dict[tuple[int, str], int] = {}                # (쪽, 구획) → 그 쪽에서 몇 번째 제목으로 읽혔나(위에서부터)
    for text, g in real.items():
        names = [name for key in g for name, _ in read[key] if fits(name, text)]
        names = [s for s in names if outline._indexes(s)] or names             # 표시(A, B, C)가 읽힌 표기를 쓴다
        known[text] = max(sorted(set(names)), key=names.count) if names else text
        for p in {key[0] for key in g}:
            votes[(p, text)] = len({key[1] for key in g if key[0] == p})
            rank[(p, text)] = min([i for key in g if key[0] == p for i, (name, _) in enumerate(read[key]) if fits(name, text)], default=9)

    def firsts(extra: dict[tuple[int, str], int]) -> dict[tuple[int, str], int]:
        """(단원, 구획) → 시작 쪽: 그 단원에서 그 제목을 둘 이상이 읽은 첫 쪽."""
        out: dict[tuple[int, str], int] = {}
        total = dict(votes)
        for key, c in extra.items():
            total[key] = total.get(key, 0) + c
        for (p, stage), c in sorted(total.items()):
            if c >= 2 and unit_of(p) >= 1 and (unit_of(p), stage) not in out:
                out[(unit_of(p), stage)] = p
        return out

    # ③ 시작 쪽이 안 잡힌 구획: 그 이름을 대고, 한 표만 받은 쪽과 그 구획이 보통 시작하는 자리의 둘레를 더 읽힌다
    sure = firsts({})
    n_units = len(units)
    where = {s: [(sure[(u, s)] - bounds_of_units[u - 1]) / max(1, bounds_of_units[u] - bounds_of_units[u - 1])
                 for u in range(1, n_units + 1) if (u, s) in sure] for s in known}
    # 두 단원 넘게 시작 쪽이 잡힌 구획은 다른 단원에도 있다고 보고 찾아본다(없는 단원에서는 "없음"이 돌아온다)
    usual = [s for s in known if len(where[s]) >= 2]
    place = {s: sum(where[s]) / len(where[s]) for s in usual}
    order = sorted(usual, key=lambda s: place[s])
    asks_for: dict[str, list[int]] = {}
    for u in range(1, n_units + 1):
        a, b = max(1, bounds_of_units[u - 1]), min(bounds_of_units[u], end_of_body + 1)
        for i, s in enumerate(order):
            if (u, s) in sure:
                continue
            lo = max([sure[(u, x)] for x in order[:i] if (u, x) in sure], default=a)
            hi = min([sure[(u, x)] + 1 for x in order[i + 1:] if (u, x) in sure], default=b)
            mid = a + round(place[s] * (b - a))
            first = max(lo, min(mid - GAP_PAGES // 2, hi - GAP_PAGES))
            want = set(range(first, min(hi, first + GAP_PAGES))) | {p for (p, x), c in votes.items() if x == s and lo <= p < hi}
            asks_for.setdefault(s, []).extend(sorted(want))
    found: dict[tuple[int, str], int] = {}
    numbers_found: dict[tuple[int, str], list[str]] = {}
    for s, wanted in asks_for.items():
        prompt = FIND_PROMPT.format(name=known[s])
        for r in range(FIND_READS):
            for p, rows in _lines(take(_again(prompt, r), mark(p)) for p in wanted).items():
                if p in wanted and any(cells[0].startswith("있") for cells in rows):
                    found[(p, s)] = found.get((p, s), 0) + 1
                    numbers_found.setdefault((p, s), []).extend(c[1] for c in rows if len(c) > 1 and re.search(r"\d", c[1]))
    if waiting:
        raise Pending("more")
    # 이름을 대고 물으면 없는 것도 있다고 답하기 쉽다. 큰 제목 읽기에서 한 표도 못 받은 쪽은 셋 가운데 둘은 있어야 한다
    extra = {key: c for key, c in found.items() if c >= (1 if votes.get(key, 0) >= 1 else 2)}
    extra = {key: (2 if votes.get(key, 0) == 0 else c) for key, c in extra.items()}
    starts = firsts(extra)
    # 구획은 단원마다 같은 차례로 나온다. 이름을 대고 물어서만 잡힌 시작 쪽이 뒤 차례 구획의 시작보다 뒤에 있으면
    # 없는 것을 있다고 답한 것이다(이름을 대고 물으면 그런 답이 가끔 온다). 버린다
    for u in range(1, n_units + 1):
        for i, s in enumerate(order):
            p = starts.get((u, s))
            if p is not None and votes.get((p, s), 0) < 2 and any(starts.get((u, x), p + 1) <= p for x in order[i + 1:]):
                del starts[(u, s)]
    # 어떤 구획이 시작 쪽이 잡힌 단원(셋 이상)의 거의 모두에서 단원의 첫 쪽에서 시작한다면, 못 잡은 단원에서도 첫 쪽에서 시작한다고 본다
    # (개념 설명이 함께 실린 첫 쪽에서는 가벼운 모델이 단계 제목보다 개념 제목을 읽어 오는 일이 잦다)
    for s in known:
        at = [u for u in range(1, n_units + 1) if (u, s) in starts]
        if len(at) >= 3 and sum(starts[(u, s)] == bounds_of_units[u - 1] for u in at) >= 0.8 * len(at):
            for u in range(1, n_units + 1):
                p = bounds_of_units[u - 1]
                if (u, s) not in starts and 1 <= p <= end_of_body and p not in starts.values():
                    starts[(u, s)] = p
    bounds: dict[str, str] = {}
    # 한 쪽에서 구획이 둘 시작하면 위의 것을 그 쪽의 구획으로 삼는다(뼈대는 쪽마다 구획 하나만 적는다)
    # 그 쪽에서 몇 번째로 읽혔는지가 같으면, 다른 쪽들에서 보통 더 위에 읽힌 구획을 위의 것으로 본다
    usual_rank = {s: sum(r for (_, x), r in rank.items() if x == s) / max(1, sum(1 for (_, x) in rank if x == s)) for s in known}
    for (u, stage), p in sorted(starts.items(), key=lambda kv: (kv[1], -rank.get((kv[1], kv[0][1]), 9), -usual_rank[kv[0][1]])):
        pages[p - 1][2] = known[stage]
    for (u, stage), p in starts.items():
        if pages[p - 1][2] != known[stage]:
            continue
        nums = [num for (q, _), titles in read.items() if q == p for name, num in titles
                if num and fits(name, stage)] + numbers_found.get((p, stage), [])
        best = max(sorted(set(nums)), key=nums.count) if nums else ""
        if best and nums.count(best) >= 2:               # 둘 이상이 같게 읽은 번호만 쓴다
            bounds[f"{p}:{known[stage]}"] = best
    data = {"version": outline.VERSION, "pages": pages, "units": units, "offset": offset, "bounds": bounds,
            "reads": {str(p): sorted({name for (q, _), titles in read.items() if q == p for name, _ in titles}) for p in sorted({k[0] for k in read})},
            "missing": 0, "requests": paper.calls, "seconds": 0.0, "cost": 0.0, "light": True,
            "model": f"{paper.provider}/{paper.model}", "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    outline._save(idx, data)
    return data
