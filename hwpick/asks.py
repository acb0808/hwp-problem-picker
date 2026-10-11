"""모델에게 물을 것을 에이전트의 가벼운 서브에이전트에게 맡기는 읽기 담당(opencode가 없는 환경).

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

import hashlib
import json
import re
import threading
import time
from pathlib import Path

SECTION_RE = re.compile(r"(?m)^[ \t]*@@[ \t]*\[?(\d+)\]?[ \t]*$")
TOC_FIRST = 16             # 차례는 책 앞쪽에 있다. 첫 번째로 맡길 때 앞의 이 쪽들을 차례로 읽혀 둔다(차례가 아니면 `없음`)
SETTLE = 2.0               # 답 파일이 다 쓰였는지 보려고 잠깐 더 기다리는 시간(초)

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
    """서브에이전트에게 뼈대를 읽히는 길을 켰는가. 기본은 끈다.

    가벼운 모델은 단원(차례)은 잘 읽지만 그림 글자로 된 단계 제목을 자주 놓쳐서, 지금은 구획이 틀린 뼈대가 나온다
    (2026-10-11 한 교재에서 구획 30개 가운데 맞은 것 0). 틀린 뼈대는 한 번 저장되면 계속 쓰이므로, 맞게 나올 때까지는
    작업 폴더 설정의 "outline": "subagent" 나 환경 변수 HWPICK_OUTLINE=subagent 로 켠 사람만 쓴다.
    """
    import os
    if os.environ.get("HWPICK_OUTLINE", "").strip().lower() == "subagent":
        return True
    try:
        from . import tasks
        cfg = json.loads((tasks.data_dir(workspace) / "config.json").read_text(encoding="utf-8"))
        return cfg.get("outline") == "subagent"
    except (OSError, ValueError):
        return False


class Pending(Exception):
    """답이 아직 없는 물음이 있다. 서브에이전트에게 맡겨야 한다."""


def _key(prompt: str, image: bytes) -> str:
    return hashlib.sha1(prompt.encode("utf-8") + b"\0" + image).hexdigest()[:16]


class PaperReader:
    """reader.Reader와 같은 꼴로 쓰는, 서브에이전트가 써 준 답을 돌려주는 읽기 담당."""

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
            got = self._sections(job["out"])
            for n, key in enumerate(job["keys"], 1):
                if n in got:
                    out[key] = got[n]
        return out

    def reload(self) -> None:
        self.state = self._state()
        self.answers = self._answers()

    def unfinished(self) -> list[dict]:
        """가장 최근에 맡긴 일들 가운데 답이 다 오지 않은 것."""
        last = [j for j in self.state["jobs"] if j["round"] == self.state["round"]]
        return [j for j in last if len(self._sections(j["out"])) < len(j["keys"])]

    def wait(self, seconds: float) -> list[dict]:
        """맡긴 일의 답이 다 올 때까지 기다린다(최대 seconds초). 끝나지 않은 일들을 돌려준다."""
        end = time.time() + seconds
        left = self.unfinished()
        while left and time.time() < end:
            time.sleep(1.0)
            left = self.unfinished()
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
            size = 3 if prompt in (outline.PROMPT, outline.TOC_PROMPT) else 8     # 여러 쪽을 모은 그림과 차례는 답이 길다
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
