"""명령줄 진입점. 에이전트는 이 명령들만 실행하면 된다.

    python -m hwpick open            문제 선택 화면을 띄운다(서버가 없으면 백그라운드로 켠다)
    python -m hwpick task show ID    고른 문제 목록을 본다
    python -m hwpick make ID         고른 문제를 한글 파일로 만든다(그림을 읽어야 하면 서브에이전트에게 맡길 인자를 낸다)
    python -m hwpick finish ID       서브에이전트가 끝나기를 기다렸다가 검사하고 한글 파일을 만든다

말로 범위를 정해 달라고 할 때("1단원 B단계만 모아 줘")는 클릭 대신 이 명령들로 목록을 만든다.

    python -m hwpick books                     교재 목록
    python -m hwpick outline 교재              교재의 뼈대(단원·구획의 쪽 범위)
    python -m hwpick pick 교재 --section S2    그 구획의 문제를 박스에서 모아 목록을 만든다

교재는 PDF와 한글 문서(.hwp, .hwpx) 둘 다 된다. HWPick이 만든 한글 문서에서 다시 고를 때는 번호로 고른다.

    python -m hwpick pick 문서.hwp --numbers 3,5,7-10   그 번호의 문제만(문서에 든 내용을 그대로 꺼낸다)

박스를 못 찾았거나 잘못 잡은 문제는 에이전트가 그 쪽을 보고 직접 잡는다.

    python -m hwpick look 교재 --page 34                 눈금과 박스가 그려진 쪽 그림(좌표는 0~1000)
    python -m hwpick add 교재 --page 34 --box A:0412     그 영역을 문제로 담는다(덩어리 글자나 '왼쪽,위,오른쪽,아래')
    python -m hwpick drop ID 3,5                         목록에서 그 차례의 문제를 뺀다

finish가 하는 일을 단계별로 따로 할 수도 있다.

    python -m hwpick prepare ID      문제 그림을 잘라 전사 지시문(batch-N.md)을 만든다
    python -m hwpick check ID        서브에이전트가 쓴 전사 결과를 검사한다
    python -m hwpick build ID        양식에 넣어 한글 파일을 만든다
    python -m hwpick doctor          설치 상태를 점검한다
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import tasks
from .tasks import DEFAULT_PORT


def _ping(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            d = json.loads(r.read().decode("utf-8"))
            return d if d.get("app") == "hwpick" else None
    except Exception:
        return None


def _busy(port: int) -> bool:
    """그 포트를 누가 쓰고 있는가(HWPick이 아닌 다른 프로그램일 수도 있다)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def cmd_serve(args) -> int:
    from .server import serve
    serve(args.workspace, args.library, args.port)
    return 0


def _server(ws: Path, port: int = DEFAULT_PORT, libraries: list[str] | None = None) -> int:
    """이 작업 폴더의 선택 화면 서버가 떠 있게 하고 그 포트를 돌려준다(없으면 뒤에서 켠다)."""
    # 이 작업 폴더의 서버가 기본 포트가 아닌 곳에 떠 있을 수 있다(기본 포트를 남이 쓰고 있었던 경우). 적어 둔 포트부터 본다
    try:
        noted = json.loads((tasks.data_dir(ws) / "server.json").read_text(encoding="utf-8"))["port"]
        alive = _ping(noted)
        if alive is not None and Path(alive["workspace"]) == ws:
            port = noted
    except (OSError, ValueError, KeyError):
        pass
    # 같은 작업 폴더의 서버가 이미 떠 있으면 그대로 쓰고, 다른 폴더의 서버나 다른 프로그램이 쓰는 포트는 피한다
    for _ in range(20):
        alive = _ping(port)
        if (alive is not None and Path(alive["workspace"]) == ws) or (alive is None and not _busy(port)):
            break
        port += 1
    if _ping(port) is None:
        log = tasks.data_dir(ws) / "server.log"
        cmd = [sys.executable, "-m", "hwpick", "serve", "--workspace", str(ws), "--port", str(port)]
        for lib in libraries or []:
            cmd += ["--library", lib]
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONIOENCODING="utf-8")
        flags = 0
        if os.name == "nt":
            # DETACHED_PROCESS를 같이 주면 안 된다: 콘솔이 아예 없는 프로세스가 되어, 가상환경의 python.exe(진짜
            # 파이썬을 다시 띄우는 실행 파일)가 띄운 파이썬에 윈도우가 새 콘솔 창(까만 빈 창)을 만들어 준다.
            # CREATE_NO_WINDOW만 주면 보이지 않는 콘솔을 하나 갖고, 그 아래에서 뜨는 것들도 그것을 물려받는다
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        with open(log, "ab") as f:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                             env=env, creationflags=flags, close_fds=True)
        for _ in range(60):
            if _ping(port):
                break
            time.sleep(0.5)
        else:
            raise ValueError(f"선택 화면 서버를 켜지 못했습니다. 로그: {log}")
    else:
        for lib in libraries or []:              # 이미 떠 있는 서버에는 폴더를 따로 알려 준다
            _api(port, "library", {"path": lib})
    return port


def _api(port: int, path: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    """선택 화면 서버에 묻는다. 교재를 읽고 박스를 찾는 일은 그 서버가 한다(이미 읽어 둔 것을 그대로 쓴다)."""
    import urllib.error
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{path}", data=data,
                                 method="POST" if body is not None else "GET", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read().decode("utf-8")).get("error", str(e))
        except ValueError:
            message = str(e)
        raise ValueError(message) from None


def _book(port: int, text: str | None) -> dict:
    """교재를 이름의 일부나 ID 앞부분으로 찾는다. 주지 않으면 선택 화면에서 지금 열려 있는 교재."""
    pdfs = _api(port, "library", timeout=120)["pdfs"]
    if not text:
        focus = _api(port, "state").get("focus")
        found = [p for p in pdfs if p["id"] == focus]
        if not found:
            raise ValueError("어느 교재인지 알 수 없습니다(선택 화면에 열려 있는 교재가 없습니다). "
                             "books 명령으로 목록을 보고 교재를 정해 주세요")
        return found[0]
    def fold(name: str) -> str:
        return "".join(name.lower().split())

    def bare(name: str) -> str:
        return fold(name).removesuffix(".hwpx").removesuffix(".hwp")

    key = fold(text)
    # 이름이 똑같은 교재가 먼저다. 교재 이름으로 시작하는 한글 문서(그 교재에서 만든 결과물)가 같이 걸리지 않게 한다
    found = [p for p in pdfs if fold(p["name"]) == key] or [p for p in pdfs if bare(p["name"]) == bare(text)] \
        or [p for p in pdfs if len(text) >= 6 and p["id"].startswith(text)] or [p for p in pdfs if key in fold(p["name"])]
    if len(found) != 1:
        names = ", ".join(p["name"] for p in found[:8]) or "없음"
        raise ValueError(f"'{text}'에 맞는 교재가 {len(found)}권입니다({names}). books 명령에 나온 이름이나 ID를 그대로 써 주세요")
    return found[0]


def cmd_books(args) -> int:
    ws = Path(args.workspace).resolve()
    port = _server(ws)
    focus = _api(port, "state").get("focus")
    for p in _api(port, "library", timeout=120)["pdfs"]:
        pages = f"{p['pages']:>3}쪽" if p["pages"] else "  ?쪽"          # 한글 문서는 열어 보기 전에는 쪽 수를 모른다
        print(f"{p['id'][:6]}  {pages}  {p['name']}" + ("   ← 선택 화면에 열려 있음" if p["id"] == focus else ""))
    return 0


def cmd_outline(args) -> int:
    ws = Path(args.workspace).resolve()
    port = _server(ws)
    book = _book(port, args.book)
    res = _api(port, f"pdf/{book['id']}/outline?wait={args.wait:g}" + ("&refresh=1" if args.refresh else ""), timeout=900)
    if res.get("pending"):
        return _outline_hand_over(res, args)
    if res.get("document"):
        rest = res["problems"] - res.get("direct", res["problems"])
        print(f"{res['name']} ({res['pages']}쪽, 문제 {res['problems']}개). 한글 문서라 구획은 없고 문제 번호로 고릅니다."
              + (f" {rest}문제는 표·도형 때문에 문서에서 그대로 꺼내지 못해 그림을 읽어 옮깁니다." if rest else ""))
        for line in res["lines"]:
            print(line)
        for note in res.get("notes", []):
            print("확인할 점: " + note)
        if not res["problems"]:
            print("문제 박스가 없습니다. `look`으로 쪽을 보고 `add`로 직접 담거나, 선택 화면(open)의 `직접 그리기`로 담을 수 있습니다.")
            return 0
        print(f'모으려면: pick "{res["name"]}" --numbers 3,5,7-10 --name "목록 이름"   (전부면 --numbers 없이)')
        return 0
    print(f"{res['name']} ({res['pages']}쪽)의 뼈대. 한 줄이 구획 하나이고, 맨 앞은 구획 ID입니다.")
    if not res["units"]:
        print("(차례를 읽지 못해 단원 이름은 쪽에서 읽힌 대로입니다. 단원 구분이 틀릴 수 있습니다.)")
    for line in res["lines"]:
        print(line)
    if res.get("light"):
        print("(가벼운 서브에이전트가 쪽 그림을 읽어 만든 뼈대입니다. 구획의 시작 쪽이 틀린 곳이 있을 수 있으니, 보고할 때 고른 구획의 "
              "이름·쪽 범위·첫 번호~끝 번호를 밝혀 사용자가 확인할 수 있게 하세요. A·B·C 같은 표시는 그림이라 이름에서 빠져 있을 수 있습니다.)")
    print(f'모으려면: pick "{res["name"]}" --section <구획 ID> [--section …] --name "목록 이름"')
    return 0


def _outline_hand_over(res: dict, args) -> int:
    """교재의 뼈대를 알아내려고 쪽 그림을 읽는 일을 서브에이전트에게 넘기는 방법을 찍는다."""
    again = _again(args, f'outline "{res["name"]}"')
    if res["pending"] == "waiting":
        print(f"{res['name']}: 서브에이전트가 아직 끝나지 않았습니다(답이 덜 온 지시문 {len(res['jobs'])}개).")
        print(f"같은 명령을 다시 실행하세요(다시 기다립니다): {again}")
        return 2
    print(f"{res['name']} ({res['pages']}쪽): 단원 구성을 알아내려면 쪽 그림을 읽어야 합니다(교재마다 처음 한 번, 이번이 {res['round']}번째 읽기. "
          "보통 두세 번에 걸쳐 10분쯤 걸립니다).")
    if res["round"] == 1:
        print("먼저 사용자에게 한 줄로 알리세요: 처음 보는 교재라 단원 구성을 읽는 데 10분쯤 걸립니다(다음부터는 바로 됩니다).")
    print(f"그림 {res['questions']}장을 가벼운 서브에이전트 {len(res['jobs'])}개가 나눠 읽습니다. 쪽을 직접 열어 보지 않습니다.")
    print("1) 아래 한 줄을 인자로 invoke_subagent를 **한 번** 호출하세요(안티그래비티). 고치지 말고 그대로 넣습니다.")
    short = _dispatch(res["jobs"], READ_PAGES)
    if short:
        print(short)
        print("   (이 서브에이전트가 읽기 담당들을 대신 불러 줍니다. 그 파일을 열어 보거나 읽기 담당을 직접 부르지 않습니다.)")
    else:
        print(_invoke(res["jobs"], READ_PAGES))
        print("   (안티그래비티가 아니면: 위 Prompt 하나마다 새 서브에이전트 하나에게 그 문장을 그대로 주고, 모두 동시에 돌립니다.)")
    print("2) 호출한 뒤에는 서브에이전트를 기다리거나 상태를 살피지 말고 **곧바로** 같은 명령을 다시 실행하세요:")
    print(f"   {again}")
    print("   이 명령이 서브에이전트들이 끝날 때까지 기다렸다가 이어서 합니다. 앞의 답을 봐야 정해지는 쪽이 있어, 같은 안내가 한두 번 더 나올 수 있습니다.")
    return 0


def cmd_pick(args) -> int:
    ws = Path(args.workspace).resolve()
    port = _server(ws)
    book = _book(port, args.book)
    pages = None
    if args.pages:
        m = re.fullmatch(r"\s*(\d+)\s*(?:[-~∼～]\s*(\d+))?\s*쪽?\s*", args.pages)
        if not m:
            raise ValueError(f"쪽 범위를 알아볼 수 없습니다: {args.pages} (예: --pages 12-19)")
        pages = [int(m.group(1)), int(m.group(2) or m.group(1))]
    numbers = []
    for part in (args.numbers or "").replace(" ", "").split(","):
        a, dash, b = part.partition("-")
        if dash and a.isdigit() and b.isdigit():
            step = 1 if int(b) >= int(a) else -1
            numbers += [str(v).zfill(len(a)) for v in range(int(a), int(b) + step, step)]
        elif part:
            numbers.append(part)
    res = _api(port, "pick", {"pdf": book["id"], "sections": args.section or [], "pages": pages, "first": args.first,
                              "last": args.last, "numbers": numbers, "name": args.name or "", "dry": args.dry}, timeout=900)
    n, free = res["count"], res.get("free", 0)
    if n and free == n:
        cost = "문서에 든 내용을 그대로 꺼내므로 옮겨 적는 비용이 없습니다"
    else:
        cost = "옮겨 적기는 가벼운 서브에이전트가 맡습니다" \
            + (f", {free}문제는 문서에서 그대로 꺼냅니다" if free else "")
    print(f"{book['name']}에서 문제 {n}개를 모았습니다(한글 문서로 {-(-n // 4)}쪽쯤, {cost}).")
    for line in res["spans"]:
        print("  " + line)
    for note in res["notes"]:
        print("  확인할 점: " + note)
    if not n:
        print("모은 문제가 없어 목록을 만들지 않았습니다.")
        return 1
    if res["task"]:
        print(f"hwp-problem-picker 태스크 {res['task']} (문제 {n}개)")
        if any("빠진 번호" in note or "빠졌습니다" in note for note in res["notes"]):
            print(f'빠진 문제는 그 쪽을 보고 직접 담습니다(한글 파일을 만들기 전에): look "{book["name"]}" --page <쪽> --task {res["task"]}')
        print("선택 화면에서 이 목록을 열면 문제를 빼거나 순서를 바꿀 수 있습니다. 한글 파일로 만들려면:")
        print(f'  "{sys.executable}" -m hwpick --workspace "{ws}" make {res["task"]}')
    else:
        print("(--dry: 세어 보기만 하고 목록은 만들지 않았습니다)")
    return 0


def _positions(text: str) -> list[int]:
    """'3,5,7-9' → [3, 5, 7, 8, 9]"""
    out = []
    for part in text.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:[-~](\d+))?", part)
        if not m:
            raise ValueError(f"차례를 알아볼 수 없습니다: {part} (예: 3,5,7-9)")
        out += range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1)
    return out


def _coords(box: list[int]) -> str:
    return ",".join(str(v) for v in box)


def cmd_look(args) -> int:
    ws = Path(args.workspace).resolve()
    port = _server(ws)
    book = _book(port, args.book)
    res = _api(port, f"pdf/{book['id']}/look/{args.page}" + (f"?task={args.task}" if args.task else ""), timeout=900)
    print(f"{res['name']} {res['page']}쪽(전체 {res['pages']}쪽)의 그림: {res['file']}")
    print("이 그림을 열어 보세요. 가장자리의 눈금이 좌표입니다(왼쪽 위 0,0 · 오른쪽 아래 1000,1000). 아래 좌표는 왼쪽,위,오른쪽,아래.")
    if res["boxes"]:
        print(f"찾아 둔 박스 {len(res['boxes'])}개(파랑, 목록에 담긴 것은 초록):")
        for b in res["boxes"]:
            mark = "" if b["mark"] == b["label"] else f" [그림의 {b['mark']}]"
            print(f"  {b['label']}{mark}  {_coords(b['box'])}" + (f"  ← 목록의 {b['at']}번째" if b["at"] else ""))
    else:
        print("찾아 둔 박스가 없는 쪽입니다.")
    for b in res["manual"]:
        print(f"직접 잡은 영역(보라) {b['mark']}: {b['label']}  {_coords(b['box'])}  ← 목록의 {b['at']}번째")
    if res["free"]:
        print("박스가 없는 덩어리(주황 점선, 오른쪽 위에 글자). 빠진 문제는 대개 이 가운데 있습니다. 기계가 빈틈으로 나눈 것이라 "
              "제목·머리말이 문제에 붙어 있을 수 있습니다. 그럴 때 쓰라고 덩어리를 위아래 조각(왼쪽의 A1, A2 …)으로도 나눠 두었습니다.")
        for r in res["free"]:
            print(f"  {r['name']}  {_coords(r['box'])}")
            for p in r["parts"]:
                print(f"      조각 {p['name']}  {_coords(p['box'])}")
    target = f"--task {args.task}" if args.task else '--name "목록 이름"'
    print("담으려면: 덩어리가 문제 하나와 딱 맞으면 그 글자(A), 제목 등이 붙어 있으면 문제에 해당하는 조각만(A2+A3), "
          "어느 것도 맞지 않으면 눈금으로 읽은 좌표(왼쪽,위,오른쪽,아래)를 준다. 뒤에 :문제 번호. 여러 개면 --box를 되풀이한다.")
    print(f'  add "{res["name"]}" --page {res["page"]} --box <A | A2+A3 | 왼쪽,위,오른쪽,아래>:<문제 번호> {target}')
    return 0


def cmd_add(args) -> int:
    ws = Path(args.workspace).resolve()
    port = _server(ws)
    book = _book(port, args.book)

    def numbers(text: str) -> list[int]:
        vals = [v for v in re.split(r"[\s,]+", text.strip()) if v]
        if len(vals) != 4 or not all(re.fullmatch(r"\d+(\.\d+)?", v) for v in vals):
            raise ValueError(f"범위를 알아볼 수 없습니다: {text} (왼쪽,위,오른쪽,아래를 0~1000 눈금으로. 예: 60,520,480,790)")
        return [int(float(v)) for v in vals]

    boxes = []
    for spec in args.box:
        where, _, label = spec.partition(":")
        if re.fullmatch(r"\s*[A-Za-z]\d*(\s*\+\s*[A-Za-z]\d*)*\s*", where):
            boxes.append({"regions": [v.strip() for v in where.split("+")], "label": label.strip()})
        else:
            boxes.append({"box": numbers(where), "label": label.strip()})
    if args.stem:
        if len(boxes) != 1:
            raise ValueError("--stem(공통 발문)은 --box 하나와 함께 씁니다. 발문을 함께 쓰는 문제마다 따로 실행하세요")
        boxes[0]["stem"] = numbers(args.stem)
    res = _api(port, "add", {"pdf": book["id"], "page": args.page, "boxes": boxes, "task": args.task, "name": args.name or "",
                             "after": args.after, "replace": args.replace, "force": args.force}, timeout=900)
    for a in res["added"]:
        seen = {True: " 첫 줄에서 그 번호를 읽어 확인했습니다.", False: " 첫 줄에서 번호를 읽지는 못했습니다.", None: ""}[a["confirmed"]]
        print(f"담았습니다: {a['label']} → 목록의 {a['at']}번째, 맞춘 범위 {_coords(a['box'])}.{seen}")
        for note in a["notes"]:
            print(f"  확인할 점: {note}")
        print(f"  잡힌 모습: {a['preview']}")
    for r in res["refused"]:
        said = "+".join(r["said"]) if r["said"] and isinstance(r["said"][0], str) else _coords(r["said"])
        print(f"담지 않았습니다: {said}" + (f"({r['label']})" if r["label"] else "") + f" — {r['why']}")
    if res["task"]:
        print(f"hwp-problem-picker 태스크 {res['task']} (문제 {res['count']}개)")
    if res["added"]:
        print("잡힌 모습 그림을 열어 확인하세요: 빨간 테두리 안이 담긴 영역이고 둘레는 옅게 보입니다(눈금은 쪽 전체 기준의 좌표). "
              "문제 하나가 처음부터 끝까지(선택지·그림 포함) 테두리 안에 있고 다른 문제나 제목이 섞이지 않았어야 합니다. "
              "어긋났으면 그 눈금으로 좌표를 고쳐 --replace <목록의 차례>로 다시 잡습니다.")
    return 0 if res["added"] and not res["refused"] else 1


def cmd_drop(args) -> int:
    ws = Path(args.workspace).resolve()
    port = _server(ws)
    res = _api(port, f"tasks/{args.id}/drop", {"positions": _positions(args.positions)})
    print(f"뺐습니다: {', '.join(res['dropped'])}. 목록에 문제 {res['count']}개가 남았습니다.")
    return 0


def cmd_open(args) -> int:
    ws = Path(args.workspace).resolve()
    port = _server(ws, args.port, args.library)
    url = f"http://127.0.0.1:{port}/"
    if not args.no_browser:
        webbrowser.open(url)
    print(f"문제 선택 화면: {url}")
    print("사용자가 'hwp-problem-picker 태스크 T…' 문구를 붙여 넣고 한글 파일을 만들어 달라고 하면, 다른 것은 하지 말고 이 명령 하나만 실행하세요"
          "(T…는 문구에 있는 ID로 바꿉니다):")
    print(f'  "{sys.executable}" -m hwpick --workspace "{ws}" make T…')
    return 0


def cmd_task(args) -> int:
    ws = Path(args.workspace).resolve()
    if args.action == "list":
        for t in tasks.list_all(ws):
            print(f"{t['id']}\t{t['count']}문제\t{t['updated']}\t{t['name']}")
        return 0
    task = tasks.load(ws, args.id)
    if args.json:
        print(json.dumps(task, ensure_ascii=False, indent=1))
        return 0
    print(f"태스크 {task['id']} · {task.get('name', '')} · 문제 {len(task['items'])}개")
    for i, it in enumerate(task["items"], 1):
        extra = " (공통 발문 포함)" if it.get("stem") else ""
        extra += " (에이전트가 잡은 영역)" if it.get("by") == "agent" else " (직접 그린 영역)" if it.get("manual") else ""
        print(f"{i:>3}. {it['pdf_name']} {it['page']}쪽 {it['label']}{extra}  [{it['pid']}]")
    return 0


READER_AGENT = "hwpick-reader"      # 안티그래비티에 깔아 두는 전용 서브에이전트(명령을 실행하지 못한다. agents/hwpick-reader.md)
DISPATCH_AGENT = "hwpick-dispatch"  # 읽기 담당들을 대신 불러 주는 가벼운 서브에이전트(agents/hwpick-dispatch.md)
DISPATCH_READERS = 30               # 나눠 주기 담당 하나가 부르는 읽기 담당의 수
DISPATCH_LINE = 10                  # 그 담당이 한 번의 호출에 넣는 수(가벼운 모델이 긴 인자를 그대로 옮기다 틀리지 않게 끊는다)


def _installed(name: str) -> bool:
    return (Path.home() / ".gemini" / "config" / "agents" / f"{name}.md").is_file()


READ_PROBLEMS = ("수학 문제 옮겨 적기", "고른 문제를 글로 옮겨 적습니다", "문제 옮겨 적기")
READ_PAGES = ("교재 쪽 읽기", "교재의 단원 구성을 읽습니다", "단원 구성 읽기")


def _invoke(files: list[str], words: tuple[str, str, str] = READ_PROBLEMS) -> str:
    """안티그래비티의 invoke_subagent에 그대로 넣을 인자(한 줄 JSON). 지시문 파일마다 가벼운 서브에이전트 하나."""
    kind = READER_AGENT if _installed(READER_AGENT) else "self"
    subs = [{"Model": "flash_lite", "Prompt": f"다음 파일을 읽고 그 안의 지시를 그대로 따르세요: {f}", "Role": words[0],
             "TypeName": kind, "Workspace": "inherit"} for f in files]
    return json.dumps({"Subagents": subs, "toolAction": words[1], "toolSummary": words[2]},
                      ensure_ascii=False, separators=(",", ":"))


def _dispatch(files: list[str], words: tuple[str, str, str] = READ_PROBLEMS) -> str | None:
    """읽기 담당을 직접 부르지 않고 나눠 주기 담당에게 맡기는 인자. 전용 서브에이전트 둘이 깔려 있을 때만 쓴다(없으면 None).

    주 에이전트가 한도를 쓰는 것은 응답 횟수와 문맥 길이다. 읽기 담당 20개를 직접 부르면 인자 5천 자를 받아 적어야 하고
    (30개에 출력 4천 토큰), 담당들이 끝날 때마다 주 에이전트가 깬다. 나눠 주기 담당을 끼우면 인자는 쪽 수와 무관하게
    한두 줄이고, 깨는 것은 그 담당이다(가벼운 모델이라 한도를 거의 쓰지 않는다).
    """
    if not files or not (_installed(READER_AGENT) and _installed(DISPATCH_AGENT)):
        return None
    folder = Path(files[0]).parent
    subs = []
    for k in range(0, len(files), DISPATCH_READERS):
        group = files[k:k + DISPATCH_READERS]
        path = folder / f"dispatch-{k // DISPATCH_READERS + 1:02d}.txt"
        path.write_text("".join(_invoke(group[i:i + DISPATCH_LINE], words) + "\n" for i in range(0, len(group), DISPATCH_LINE)), encoding="utf-8")
        subs.append({"Model": "flash_lite", "Prompt": str(path), "Role": "일 나눠 주기", "TypeName": DISPATCH_AGENT, "Workspace": "inherit"})
    return json.dumps({"Subagents": subs, "toolAction": "읽는 일을 나눠 줍니다", "toolSummary": "일 나눠 주기"},
                      ensure_ascii=False, separators=(",", ":"))


def _again(args, command: str) -> str:
    """다음에 실행할 명령을 경로까지 채워서."""
    return f'"{sys.executable}" -m hwpick --workspace "{Path(args.workspace).resolve()}" {command}'


def _hand_over(state: dict, task_id: str, args) -> None:
    """준비한 일을 서브에이전트에게 넘기는 방법을 찍는다."""
    ready = sum(1 for p in state["problems"] if p.get("ready"))
    todo = len(state["problems"]) - ready
    print(f"문제 {len(state['problems'])}개를 준비했습니다." + (f" 그중 {ready}개는 글이 이미 준비되어 있습니다." if ready else ""))
    if not state["batches"]:
        print(f"옮겨 적을 문제가 없습니다. 바로 실행하세요: {_again(args, 'finish ' + task_id)}")
        return
    print(f"옮겨 적을 {todo}문제를 가벼운 서브에이전트 {len(state['batches'])}개가 나눠 읽습니다"
          f"(같은 문제를 따로 {state['reads']}번 읽혀 엔진이 자리마다 다수결로 합칩니다).")
    print("1) 아래 한 줄을 인자로 invoke_subagent를 **한 번** 호출하세요(안티그래비티). 고치지 말고 그대로 넣습니다.")
    short = _dispatch(state["batches"])
    if short:
        print(short)
        print("   (이 서브에이전트가 읽기 담당들을 대신 불러 줍니다. 그 파일을 열어 보거나 읽기 담당을 직접 부르지 않습니다.)")
    else:
        print(_invoke(state["batches"]))
        print("   (안티그래비티가 아니면: 위 Prompt 하나마다 새 서브에이전트 하나에게 그 문장을 그대로 주고, 모두 동시에 돌립니다.)")
    print("2) 호출한 뒤에는 서브에이전트를 기다리거나 상태를 살피지 말고 **곧바로** 실행하세요:")
    print(f"   {_again(args, 'finish ' + task_id)}")
    print("   이 명령이 서브에이전트들이 끝날 때까지 기다렸다가, 읽은 것을 합치고 검사해 한글 파일까지 만듭니다.")


def cmd_prepare(args) -> int:
    from . import pipeline
    state = pipeline.prepare(args.workspace, args.id, args.batch, args.reads)
    _hand_over(state, args.id, args)
    return 0


def cmd_finish(args) -> int:
    from . import pipeline
    from .hwp.writer import HwpError
    ws, run = Path(args.workspace).resolve(), pipeline.run_dir(args.workspace, args.id)
    if not (run / "run.json").is_file():
        print(f"오류: {args.id}는 아직 준비하지 않았습니다. 먼저: {_again(args, 'make ' + args.id)}")
        return 1
    waited = pipeline.wait_reads(ws, args.id, args.wait)
    if waited["short"]:
        print(f"서브에이전트가 아직 끝나지 않았습니다(지시문 {waited['jobs']}개 가운데 결과가 없는 것 {len(waited['pending'])}개, "
              f"덜 읽힌 문제 {len(waited['short'])}개).")
        print(f"같은 명령을 다시 실행하세요(다시 기다립니다): {_again(args, 'finish ' + args.id)}")
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        if state.get("waited", 0) >= 2:
            # 두 번 기다려도 모자라다: 결과가 없는 일만 읽기 담당에게 직접 다시 맡긴다
            files = [j["file"] for j in state.get("jobs", []) if j["name"] in waited["pending"]][:DISPATCH_READERS]
            print("서브에이전트가 멈춘 것 같습니다. 아래 한 줄을 인자로 invoke_subagent를 한 번 호출한 뒤 같은 finish 명령을 다시 실행하세요.")
            print(_invoke(files))
        return 2
    report = pipeline.check(ws, args.id, compare_ocr=True)
    state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    bad = [r for r in report["results"] if r["status"] in ("error", "missing")]
    if bad and not state.get("redone"):
        path = pipeline.redo_job(ws, args.id, [r["n"] for r in bad])
        print(f"{len(bad)}문제는 읽은 결과를 쓸 수 없어 다시 옮겨 적어야 합니다: {[r['n'] for r in bad]}")
        for r in bad[:8]:
            print(f"  {r['n']}번: {'; '.join(r['errors'])[:160]}")
        print("1) 아래 한 줄을 인자로 invoke_subagent를 한 번 호출하세요(다시 시키는 것은 이번 한 번뿐입니다).")
        print(_invoke([path]))
        print(f"2) 호출한 뒤 곧바로 다시 실행하세요: {_again(args, 'finish ' + args.id)}")
        return 1
    if bad:
        # 다시 시킨 문제의 결과도 기다려 본다(한 번 더 검사)
        deadline = time.time() + args.wait
        while time.time() < deadline and any(not (Path(r["folder"]) / "text.md").is_file() for r in bad if r["status"] == "missing"):
            time.sleep(1.0)
        report = pipeline.check(ws, args.id, compare_ocr=True)
    try:
        result = pipeline.build(ws, args.id, args.template, args.name, args.title)
    except HwpError as e:
        print(f"한글 파일을 만들지 못했습니다: {e}")
        return 1
    votes = [json.loads(f.read_text(encoding="utf-8")).get("status") for f in sorted((run / "problems").glob("*/votes.json"))]
    print(f"한글 파일: {result['file']}")
    line = f"문제 {len(state['problems'])}개, {result['pages']}쪽."
    if votes:
        line += (f" 따로 {state.get('reads')}번 읽혀 합쳤습니다(모두 같음 {votes.count('same')}, 다수결로 정함 {votes.count('majority')}, "
                 f"갈린 곳·정하지 못한 곳 있음 {votes.count('split') + votes.count('close')}).")
    print(line)
    if result["as_image"]:
        print(f"글로 옮기지 못해 문제 그림을 그대로 넣은 문제: {result['as_image']}")
    for r in report["results"]:
        if r["n"] in result["as_image"]:
            continue
        notes = r["warnings"] + (["낙서가 있었습니다"] if r.get("doodle") else [])
        if notes:
            print(f"  {r['n']}번 확인 필요: {'; '.join(notes)}")
    if result.get("with_figures"):
        print(f"교재에서 그림을 잘라 붙인 문제: {result['with_figures']} — 그림이 맞게 잘렸는지는 확인하지 않았습니다.")
    _layout_notes(result)
    print("미리보기 그림을 열어 확인하지 말고, 위에 적힌 번호들을 사용자가 결과물에서 직접 보고 고치도록 그대로 알리세요.")
    return 0


def cmd_check(args) -> int:
    from . import pipeline
    report = pipeline.check(args.workspace, args.id, compare_ocr=not args.no_ocr)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report["ok"] else 1
    mark = {"ok": "통과", "warn": "확인 필요", "error": "다시 해야 함", "missing": "결과 없음", "review": "다시 볼 곳 있음"}
    for r in report["results"]:
        print(f"{r['n']:>3}. {mark[r['status']]}  {r['from']}" + ("  (낙서 있음)" if r["doodle"] else ""))
        for e in r["errors"]:
            print(f"       오류: {e}")
        for w in r["warnings"]:
            print(f"       주의: {w}")
        if r.get("folder"):
            print(f"       고칠 파일: {Path(r['folder']) / 'text.md'}")
            if r.get("sheet"):
                print(f"       쪽 그림: {r['sheet']}  (형식·수식 오류는 그림 없이 글만 고칩니다. 내용을 알 수 없을 때만 한 번 엽니다)")
        if r["status"] == "review":
            print("       기계가 읽은 글과 한글이 다른 곳([ ] 안이 다른 부분. 기계가 틀렸을 수도 있다):")
            for m in r["mismatches"][:6]:
                print(f"         기계 '{m['machine']}'  ↔  옮긴 글 '{m['written']}'")
            print(f"       고칠 파일: {r['file']}")
            print(f"       쪽 그림: {r['sheet']}")
    bad = [r["n"] for r in report["results"] if r["status"] in ("error", "missing")]
    look = [r["n"] for r in report["results"] if r["status"] == "review"]
    if bad or look:
        if bad:
            print(f"다시 옮겨 적어야 하는 문제: {bad}")
        if look:
            print(f"그림에서 다시 봐야 하는 문제: {look} — 기계가 읽은 글과 한글이 다른 곳이 있습니다. 위에 적힌 곳만 쪽 그림에서 "
                  "확인해, 옮긴 글이 틀렸으면 고치고 맞으면 그대로 둡니다(이 안내는 한 번만 나옵니다).")
        print("이 문제들만 새 서브에이전트 하나에게 맡긴 뒤 check를 다시 실행하세요.")
        return 1
    print(f"모두 넣을 수 있습니다. 다음: python -m hwpick build {args.id}")
    return 0


def _layout_notes(result: dict, lead: str = "") -> None:
    """긴 문제를 여러 칸에 걸쳐 넣었으면 그 번호를, 그래도 넘쳤으면 그 사실을 찍는다."""
    if result.get("joined"):
        print(lead + "교재에서 나란히 놓여 있던 그림들은 한 장으로 묶어 넣었습니다(놓인 모습 그대로): " + ", ".join(f"{n}번" for n in result["joined"]))
    spread = result.get("spread") or {}
    if spread:
        print(lead + "길어서 칸 하나에 다 들어가지 않는 문제는 아래 칸과 합치거나 다음 칸에 이어 썼습니다: "
              + ", ".join(f"{n}번(칸 {c}개)" for n, c in spread.items()) + ". 그만큼 쪽 수가 늘 수 있습니다.")
    if result.get("overflow"):
        tall = result.get("too_tall") or []
        print(lead + f"주의: 내용이 칸을 넘쳐 쪽 수가 예상({result['expected_pages']}쪽)보다 많습니다"
              + (f"(쪼갤 수 없는 큰 덩어리가 있는 문제: {', '.join(str(n) + '번' for n in tall)})" if tall else "")
              + ". 사용자가 결과물에서 확인하도록 알리세요.")


def cmd_build(args) -> int:
    from . import pipeline
    from .hwp.writer import HwpError
    try:
        result = pipeline.build(args.workspace, args.id, args.template, args.name, args.title)
    except HwpError as e:
        print(f"한글 파일을 만들지 못했습니다: {e}")
        return 1
    print(f"한글 파일: {result['file']}")
    print(f"쪽 수: {result['pages']}쪽")
    if result["previews"]:
        print(f"미리보기 그림 {len(result['previews'])}장은 {Path(result['previews'][0]).parent} 에 있습니다(사용자가 볼 것. 열어 확인하지 않습니다).")
    if result["as_image"]:
        print(f"글로 옮기지 못해 그림으로 넣은 문제: {result['as_image']}")
    if result.get("with_figures"):
        print(f"교재에서 그림을 잘라 붙인 문제: {result['with_figures']} — 그림이 맞게 잘렸는지는 확인하지 않았습니다. "
              "사용자에게 이 번호들의 그림을 결과물에서 직접 보고 고쳐 달라고 알리세요.")
    _layout_notes(result)
    return 0


def cmd_make(args) -> int:
    from . import pipeline
    from .hwp.writer import HwpError
    try:
        r = pipeline.make(args.workspace, args.id, args.template, args.name, args.title, args.wait, args.preview,
                          args.again)
    except pipeline.NoReader:
        # 그림을 읽어야 하는 문제가 있다: 문제를 잘라 두고, 가벼운 서브에이전트에게 맡길 방법을 알려 준다
        print("옮겨 적기를 서브에이전트에게 맡깁니다(한글 파일은 아직 만들지 않았습니다).")
        _hand_over(pipeline.prepare(args.workspace, args.id), args.id, args)
        return 0
    except HwpError as e:
        print(f"한글 파일을 만들지 못했습니다: {e}")
        return 1
    print(f"한글 파일: {r['file']}")
    print(f"문제 {len(r['problems'])}개, {r['pages']}쪽. {r['total_seconds']}초 걸렸습니다.")
    if r.get("lifted"):
        print(f"  한글 문서에서 고른 문제 {r['lifted']}개는 문서에 든 글·수식·그림을 그대로 꺼냈습니다(다시 읽지 않음).")
    by_n = {p["n"]: p for p in r["problems"]}
    checks = {c["n"]: c for c in r["report"]["results"]}
    for n in r["as_image"]:
        why = "; ".join(by_n[n]["notes"]) or "; ".join(checks.get(n, {}).get("errors", [])) or "까닭을 알 수 없음"
        print(f"  {n}번은 글로 옮기지 못해 문제 그림을 그대로 넣었습니다({why}). 교재의 문제 번호가 그림에 보입니다.")
    if r["as_image"]:
        print("  같은 명령을 다시 실행하면 그 문제만 다시 옮겨 적습니다.")
    for n, c in checks.items():
        if n in r["as_image"]:
            continue
        notes = by_n[n]["notes"] + c["warnings"] + (["낙서가 있었습니다"] if c.get("doodle") else [])
        if notes:
            print(f"  {n}번 확인 필요: {'; '.join(notes)}")
    figs = [f"{p['n']}번 {len(p['figures'])}개" for p in r["problems"] if p["figures"] and p["n"] not in r["as_image"]]
    if figs:
        print(f"  교재에서 그림(도형·표·삽화)을 잘라 붙인 문제: {', '.join(figs)} — 글은 모두 옮겼습니다. 그림이 맞게 잘렸는지는 "
              "확인하지 않았으니, 사용자에게 결과물에서 직접 보고 고쳐 달라고 알리세요.")
    mine = [f"{n}번({it['pdf_name']} {it['page']}쪽 {it['label']})"
            for n, it in enumerate(tasks.load(args.workspace, args.id)["items"], 1) if it.get("by") == "agent"]
    if mine:
        print(f"  에이전트가 직접 잡은 영역: {', '.join(mine)} — 박스가 맞게 잡혔는지 결과물에서 확인해 달라고 알리세요.")
    _layout_notes(r)
    if r["previews"]:
        print("미리보기 그림: " + ", ".join(r["previews"]))
    return 0


def cmd_doctor(args) -> int:
    from . import doctor
    return doctor.run(fix=args.fix)


def _utf8_output() -> None:
    """에이전트는 명령 출력을 UTF-8로 읽는다. 윈도우에서 출력이 파이프로 나가면 기본값이 cp949라 한글이 깨진다."""
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    ap = argparse.ArgumentParser(prog="hwpick", description="스캔 교재에서 고른 문제를 한글 문서로 만든다")
    ap.add_argument("--workspace", default=os.getcwd(), help="태스크와 결과물을 둘 작업 폴더(기본: 현재 폴더)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="선택 화면 서버를 이 창에서 실행")
    p.add_argument("--workspace", default=argparse.SUPPRESS)
    p.add_argument("--library", action="append", help="PDF나 한글 문서가 있는 폴더(여러 번 지정 가능)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("open", help="선택 화면을 브라우저로 연다")
    p.add_argument("--library", action="append")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_open)

    p = sub.add_parser("books", help="교재 목록(선택 화면에 열려 있는 교재를 표시한다)")
    p.set_defaults(fn=cmd_books)

    p = sub.add_parser("outline", help="교재의 뼈대: 단원과 구획이 몇 쪽부터 몇 쪽인지(처음 보는 교재는 서브에이전트에게 쪽 그림을 읽힌다)")
    p.add_argument("book", nargs="?", help="교재 이름의 일부나 ID(생략하면 선택 화면에 열려 있는 교재)")
    p.add_argument("--refresh", action="store_true", help="저장해 둔 뼈대를 버리고 다시 만든다")
    p.add_argument("--wait", type=float, default=240.0, help="쪽 그림을 읽는 서브에이전트를 기다려 줄 최대 시간(초)")
    p.set_defaults(fn=cmd_outline)

    p = sub.add_parser("pick", help="구획이나 쪽 범위의 문제를 박스에서 모아 목록을 만든다")
    p.add_argument("book", nargs="?", help="교재 이름의 일부나 ID(생략하면 선택 화면에 열려 있는 교재)")
    p.add_argument("--section", action="append", help="outline이 보여 준 구획 ID(여러 번 쓸 수 있다)")
    p.add_argument("--pages", help="쪽 범위(파일의 쪽 번호). 예: 12-19. --section과 같이 주면 그 구획 가운데 그 쪽들만")
    p.add_argument("--first", help="이 번호의 문제부터")
    p.add_argument("--last", help="이 번호의 문제까지")
    p.add_argument("--numbers", help="이 번호의 문제들만, 적은 차례대로. 예: 3,5,7-10")
    p.add_argument("--name", help="목록 이름(한글 문서의 회차 이름 칸에도 들어간다)")
    p.add_argument("--dry", action="store_true", help="몇 문제인지 세어 보기만 한다")
    p.set_defaults(fn=cmd_pick)

    p = sub.add_parser("look", help="교재의 한 쪽을 눈금과 박스가 그려진 그림으로 만든다(에이전트가 직접 영역을 잡을 때)")
    p.add_argument("book", nargs="?", help="교재 이름의 일부나 ID(생략하면 선택 화면에 열려 있는 교재)")
    p.add_argument("--page", type=int, required=True, help="쪽(파일의 쪽 번호)")
    p.add_argument("--task", help="이 목록에 담긴 문제를 그림에 표시한다")
    p.set_defaults(fn=cmd_look)

    p = sub.add_parser("add", help="쪽에서 직접 잡은 영역을 문제로 목록에 담는다(범위는 잉크에 맞춰 다듬는다)")
    p.add_argument("book", nargs="?", help="교재 이름의 일부나 ID(생략하면 선택 화면에 열려 있는 교재)")
    p.add_argument("--page", type=int, required=True, help="쪽(파일의 쪽 번호)")
    p.add_argument("--box", action="append", required=True,
                   help="'왼쪽,위,오른쪽,아래[:문제 번호]'(0~1000 눈금) 또는 look이 알려 준 덩어리 'A[:문제 번호]', 'A+B'. 여러 번 쓸 수 있다")
    p.add_argument("--stem", help="이 문제가 쓰는 공통 발문의 범위('왼쪽,위,오른쪽,아래'). 문제 위에 붙여 함께 옮긴다")
    p.add_argument("--task", help="넣을 목록(생략하면 새 목록을 만든다)")
    p.add_argument("--name", help="새 목록의 이름")
    p.add_argument("--after", type=int, help="목록의 이 차례 뒤에 넣는다(0이면 맨 앞. 생략하면 문제 번호·쪽 차례에 맞춰 넣는다)")
    p.add_argument("--replace", type=int, help="목록의 이 차례에 있는 문제를 새 영역으로 바꾼다(잘못 잡힌 박스를 다시 잡을 때)")
    p.add_argument("--force", action="store_true", help="이미 있는 박스와 겹치거나 다른 번호가 읽혀도 담는다")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("drop", help="목록에서 문제를 뺀다")
    p.add_argument("id")
    p.add_argument("positions", help="뺄 차례(task show에 나온 차례). 예: 3,5,7-9")
    p.set_defaults(fn=cmd_drop)

    p = sub.add_parser("task", help="고른 문제 목록 보기")
    p.add_argument("action", choices=["show", "list"])
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_task)

    p = sub.add_parser("prepare", help="문제 그림을 자르고 서브에이전트에게 줄 지시문을 만든다(make가 알아서 부른다)")
    p.add_argument("id")
    p.add_argument("--batch", type=int, default=None, help="서브에이전트 하나에게 맡길 문제 수(기본 14, 쪽 단위로 묶는다)")
    p.add_argument("--reads", type=int, default=None, help="같은 문제를 따로 읽힐 횟수(기본 5)")
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("finish", help="서브에이전트들이 끝나기를 기다렸다가, 읽은 것을 합치고 검사해 한글 파일을 만든다")
    p.add_argument("id")
    p.add_argument("--wait", type=float, default=240.0, help="서브에이전트를 기다려 줄 최대 시간(초)")
    p.add_argument("--template", help="양식 파일(기본: 작업 폴더의 양식.hwp)")
    p.add_argument("--name", help="만들 파일 이름(확장자 제외)")
    p.add_argument("--title", help="양식의 회차 이름 칸에 넣을 글")
    p.set_defaults(fn=cmd_finish)

    p = sub.add_parser("check", help="전사 결과 검사")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-ocr", action="store_true", help="로컬 OCR과의 한글 대조를 건너뛴다")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("build", help="양식에 넣어 한글 파일을 만든다")
    p.add_argument("id")
    p.add_argument("--template", help="양식 파일(기본: 작업 폴더의 양식.hwp)")
    p.add_argument("--name", help="만들 파일 이름(확장자 제외)")
    p.add_argument("--title", help="양식의 회차 이름 칸에 넣을 글")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("make", help="고른 문제를 한글 파일로 만든다(그림을 읽어야 하는 문제는 서브에이전트에게 맡길 인자를 낸다)")
    p.add_argument("id")
    p.add_argument("--template", help="양식 파일(기본: 작업 폴더의 양식.hwp)")
    p.add_argument("--name", help="만들 파일 이름(확장자 제외)")
    p.add_argument("--title", help="양식의 회차 이름 칸에 넣을 글")
    p.add_argument("--wait", type=float, default=None, help="쓰지 않는다(예전 판의 인자를 받아만 둔다)")
    p.add_argument("--preview", action="store_true", help="미리보기 그림도 만든다(2초 더 걸린다)")
    p.add_argument("--again", action="store_true", help="검사를 통과하지 못한 채 저장된 문제도 다시 옮겨 적게 한다")
    p.set_defaults(fn=cmd_make)

    p = sub.add_parser("doctor", help="설치 상태 점검")
    p.add_argument("--fix", action="store_true", help="한글 자동화 보안 모듈이 없으면 등록한다")
    p.set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    if args.cmd == "task" and args.action == "show" and not args.id:
        ap.error("task show에는 태스크 ID가 필요합니다")
    try:
        return args.fn(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"오류: {e}")
        return 2
