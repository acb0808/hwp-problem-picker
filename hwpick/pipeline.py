"""고른 문제(태스크)를 한글 문서로 만드는 세 단계.

    prepare  문제마다 PDF에서 그림을 잘라 내고, 전사를 맡길 서브에이전트용 지시문을 만든다
    check    서브에이전트가 쓴 전사 결과를 검사한다(형식, 수식, 선택지, 그림, 한글 대조)
    build    검사를 통과한 내용을 양식에 넣어 한글 파일을 만든다

작업물은 작업 폴더의 hwpick_data/runs/<태스크 ID>/ 에 모인다.

    run.json                 문제 목록과 그림 정보, 서브에이전트에게 줄 일(jobs)
    sheets/026d5e-p12.jpg    서브에이전트가 볼 쪽 그림(고른 문제에 번호, 그림 후보에 빨간 테두리)
    sheets/026d5e-p12-h1.png 그 쪽을 문제 서너 개씩으로 나눈 조각, …-n07.png 는 문제 하나짜리 조각
    reads/r1-01.md           ← 서브에이전트가 쓰는 읽기 결과(`@@ 번호` 줄로 문제를 나눈다). 같은 문제를 따로 여러 번 읽힌다
    problems/01/votes.json   여러 번 읽은 것을 합친 기록(모두 같았는지, 정하지 못한 자리)
    problems/01/clean.png    문제 그림(글로 옮기지 못했을 때 문서에 통째로 넣는다)
    problems/01/figA.png     본문에 붙일 그림 조각
    problems/01/ocr.txt      로컬 글자 인식이 읽은 글(서브에이전트의 참고용, 검사의 한글 대조용)
    problems/01/text.md      여러 번 읽은 것을 자리마다 다수결로 합친 전사 결과(vote.py)
    problems/01/reading.json 그 결과를 저장 형식으로 바꾼 것(hwp-exam-restoration과 같은 restoration-reading/1)
    batches/r1-01.md         서브에이전트에게 줄 지시문(읽기 r1의 첫 묶음)
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import string
import time
from pathlib import Path

from . import tasks, transcript, vote

# 그림을 다루는 무거운 모듈(cv2, pymupdf, 글자 인식)은 쓰는 함수 안에서 불러온다. 글이 이미 준비된
# 문제들로 문서만 만들 때는 필요 없고, 불러오는 데만 1초 넘게 걸리기 때문이다.

SOURCE_DPI = 220           # 전사 모델에게 보여 줄 문제 그림의 해상도
SHEET_LONG_PX = 2600       # 서브에이전트에게 줄 쪽 그림의 긴 변 한도(넘으면 해상도를 낮춘다)
# 서브에이전트 길: 같은 문제를 따로 여러 번 읽혀 자리마다 다수결로 합친다. 가벼운 모델은 한 번에 100문제 중 예닐곱을
# 틀리지만 틀리는 자리가 읽을 때마다 달라서, 재 보니 세 번이면 98%, 다섯 번이면 받아 둔 읽기에서는 모두 맞았다.
# 안티그래비티에서 가벼운 서브에이전트는 한도를 거의 쓰지 않는다(주 에이전트의 응답 횟수가 한도를 쓴다).
READS = 5                  # 따로 읽히는 횟수(작업 폴더 설정의 "reads"로 바꾼다)
VIEWS = ("half", "each", "page", "half", "each")    # 읽기마다 그림을 다르게 나눠 준다(같은 그림이면 같은 데를 틀리기 쉽다)
PIECE_MAX = 4              # half: 그림 한 장에 담는 문제 수의 한도(많이 담으면 지수·윗줄 같은 작은 글자를 틀린다)
JOB_PROBLEMS = 14          # 서브에이전트 하나가 맡는 문제 수(쪽 단위로 묶는다. 교재 두 쪽쯤)
MIN_READS = 3              # 문제마다 이만큼은 읽혀야 합친다(모자라면 더 기다린다)
STRAGGLER_WAIT = 40.0      # 이만큼(초) 새 결과가 없고 모든 문제가 MIN_READS+1번 읽혔으면 늦는 서브에이전트를 기다리지 않는다
MISSING_FIGURE = "그림 빠짐"
FIGURE_DPI = 300           # (예전 값) 한글에 붙일 그림은 이제 교재에 스캔된 해상도 그대로 자른다(_scan_clip). 스캔이 아닌 쪽만 이 값으로 그린다
MAX_FIGURE_MM = (84.0, 75.0)   # 양식 한 칸에 들어가는 그림의 최대 폭·높이
MM_PER_PT = 25.4 / 72
DOODLE_NOTE = "낙서 있음"
MIN_HANGUL_MATCH = 0.5     # 로컬 OCR이 읽은 한글 가운데 옮긴 글에 있는 비율이 이보다 낮으면 확인하라고 알린다
DROPPED_TAGS = ("대표문제", "필수예제", "대표예제", "기본예제", "발전예제", "확인문제", "서술형", "고난도", "예제", "유제")
# 공통 발문 앞의 번호 범위('[0020~0021] 다음을 …'). 문제 번호와 마찬가지로 문서에 넣지 않는다
STEM_RANGE_RE = re.compile(r"^\s*\[[A-Za-z]{0,2}\d{1,4}\s*[~∼～-]\s*[A-Za-z]{0,2}\d{1,4}\]\s*", re.M)


def run_dir(workspace: str | Path, task_id: str) -> Path:
    return tasks.data_dir(workspace) / "runs" / task_id


def _clip(idx, page: int, rect_pt: list[float], dpi: int):
    from . import render
    from .index import PDF_LOCK
    with PDF_LOCK:
        img, _ = render.render_page(idx.doc[page], dpi=dpi, clip_pt=tuple(rect_pt))
    return img


def _scan_clip(idx, page: int, rect_pt: list[float]):
    """한글 문서에 붙일 그림: 교재에 스캔된 해상도 그대로 오려 낸다(600dpi 스캔본을 300dpi로 다시 그리면 화질이 절반이 된다)."""
    from . import render
    from .index import PDF_LOCK
    with PDF_LOCK:
        img, _ = render.scan_clip(idx.doc[page], tuple(rect_pt))
    return img


def _stack(images: list) -> "np.ndarray":
    import cv2
    import numpy as np
    w = max(i.shape[1] for i in images)
    padded = [cv2.copyMakeBorder(i, 0, 12 if k < len(images) - 1 else 0, 0, w - i.shape[1], cv2.BORDER_CONSTANT,
                                 value=(255, 255, 255)) for k, i in enumerate(images)]
    return np.vstack(padded)


def _index(item: dict, opened: dict):
    """문제가 든 교재를 연다(같은 교재는 한 번만). 한글 문서는 문제를 골랐을 때 바꿔 둔 것이 있으면 그것을 쓴다."""
    from .hwp import source
    from .index import PdfIndex, cache_root
    path = item["pdf_path"]
    key = (path, item["pdf_id"])                 # 같은 자리의 파일이라도 고친 전후는 다른 교재다
    if key not in opened:
        kept = source.is_document(path) and all(f.is_file() for f in source.converted(cache_root() / item["pdf_id"]))
        if not kept and not Path(path).is_file():
            raise FileNotFoundError(f"교재 파일을 찾을 수 없습니다: {path}")
        opened[key] = PdfIndex(path, item["pdf_id"])
    return opened[key]


def lift_documents(workspace: str | Path, items: list[dict]) -> int:
    """한글 문서에서 고른 문제는 문서에 든 글·수식·그림을 그대로 꺼내 둔다. 꺼낸 문제 수를 돌려준다.

    이렇게 준비된 문제는 모델이 읽을 문제에서 빠진다. 한글 문서를 아직 바꿔 두지 않았으면 여기서 한글을 잠깐 띄운다.
    """
    from . import store
    from .hwp import source
    opened: dict = {}
    done = 0
    for it in items:
        folder = store.folder(workspace, it)
        kept = store.load(folder)
        if (kept is None or store.lift_stale(kept)) and source.is_document(it["pdf_path"]) and not it.get("manual"):
            if store.lift(folder, it, _index(it, opened)) is None and kept is not None:
                # 예전 방식으로 꺼내 둔 것인데 지금 방식으로는 그대로 꺼낼 수 없는 문제다. 예전 결과를 쓰지 않게 지운다
                (folder / "state.json").unlink(missing_ok=True)
        kept = store.load(folder)
        done += kept is not None and kept.get("model") == store.LIFTED
    return done


def _sheet(idx, page: int, members: list[dict]):
    """서브에이전트에게 보여 줄 쪽 그림 한 장: 그 쪽에서 고른 문제들에 목록의 번호를 붙이고, 나머지는 옅게 흐린다.

    그림을 여는 횟수가 곧 비용이라, 문제마다 한 장씩이 아니라 쪽마다 한 장을 준다. 글자가 작아지지 않게 문제 그림을
    따로 자를 때와 같은 해상도로 그리고, 고른 문제들이 있는 부분만 잘라 낸다.
    """
    import cv2
    import numpy as np

    w_pt, h_pt = idx.sizes()[page]
    rects = [m["bbox"] for m in members] + [m["stem"] for m in members if m.get("stem")]
    pad = 16.0
    x0, y0 = max(0.0, min(r[0] for r in rects) - pad), max(0.0, min(r[1] for r in rects) - 2 * pad)
    x1, y1 = min(w_pt, max(r[2] for r in rects) + pad), min(h_pt, max(r[3] for r in rects) + pad)
    dpi = min(SOURCE_DPI, SHEET_LONG_PX * 72 / max(x1 - x0, y1 - y0))
    img = _clip(idx, page, [x0, y0, x1, y1], dpi)
    z = dpi / 72

    def px(r: list[float], grow: int = 0) -> tuple[int, int, int, int]:
        return (max(0, int((r[0] - x0) * z) - grow), max(0, int((r[1] - y0) * z) - grow),
                min(img.shape[1], int((r[2] - x0) * z) + grow), min(img.shape[0], int((r[3] - y0) * z) + grow))

    out = cv2.addWeighted(img, 0.35, np.full_like(img, 255), 0.65, 0)
    for r in rects:
        a = px(r, 5)
        out[a[1]:a[3], a[0]:a[2]] = img[a[1]:a[3], a[0]:a[2]]

    def tag(text: str, x: int, y: int, color: tuple[int, int, int], size: float, above: bool) -> None:
        thick = 3 if size >= 1.0 else 2
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, size, thick)
        top = y - th - 10 if above and y - th - 10 >= 0 else y
        cv2.rectangle(out, (x, top), (x + tw + 10, top + th + 10), color, -1)
        cv2.putText(out, text, (x + 5, top + th + 5), cv2.FONT_HERSHEY_SIMPLEX, size, (255, 255, 255), thick, cv2.LINE_AA)

    for m in members:
        a = px(m["bbox"], 5)
        cv2.rectangle(out, (a[0], a[1]), (a[2], a[3]), (200, 80, 0), 3)
        tag(f"[{m['n']}]", a[0], a[1], (200, 80, 0), 1.1, True)
        if m.get("stem"):
            b = px(m["stem"], 5)
            cv2.rectangle(out, (b[0], b[1]), (b[2], b[3]), (200, 80, 0), 1)
            tag(f"[{m['n']}]+", b[0], b[1], (200, 80, 0), 0.8, True)
        for f in m["figures"]:
            c = px(f["rect"], 3)
            cv2.rectangle(out, (c[0], c[1]), (c[2], c[3]), (40, 40, 230), 2)
            tag(f"{m['n']}{f['name']}", c[0], c[1], (40, 40, 230), 0.7, True)
    return out, (x0, y0, z)


def _crop(img, geo: tuple[float, float, float], members: list[dict]):
    """쪽 그림에서 이 문제들(이름표와 발문까지)이 든 부분만 오려 낸다."""
    x0, y0, z = geo
    rects = [m["bbox"] for m in members] + [m["stem"] for m in members if m.get("stem")]
    left, top = int((min(r[0] for r in rects) - x0) * z) - 12, int((min(r[1] for r in rects) - y0) * z) - 58
    right, bottom = int((max(r[2] for r in rects) - x0) * z) + 12, int((max(r[3] for r in rects) - y0) * z) + 12
    return img[max(0, top):min(img.shape[0], bottom), max(0, left):min(img.shape[1], right)]


def _pieces(img, geo: tuple[float, float, float], members: list[dict]) -> list[tuple]:
    """쪽 그림을 문제 서너 개짜리 조각으로 나눈다: 단(왼쪽·오른쪽)으로 가르고, 한 단에 문제가 많으면 위아래로 더 가른다."""
    width = img.shape[1] / geo[2]
    cols: list[list[dict]] = []
    last = None
    for m in sorted(members, key=lambda m: m["bbox"][0] + m["bbox"][2]):
        c = (m["bbox"][0] + m["bbox"][2]) / 2
        if last is not None and c - last < 0.25 * width:
            cols[-1].append(m)
        else:
            cols.append([m])
        last = c
    out = []
    for col in cols:
        col.sort(key=lambda m: m["bbox"][1])
        size = -(-len(col) // -(-len(col) // PIECE_MAX))
        for i in range(0, len(col), size):
            out.append((_crop(img, geo, col[i:i + size]), col[i:i + size]))
    return out


def _config(workspace: str | Path) -> dict:
    try:
        return json.loads((tasks.data_dir(workspace) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def prepare(workspace: str | Path, task_id: str, batch_size: int | None = None, reads: int | None = None) -> dict:
    """태스크의 문제들을 잘라 전사 준비물을 만든다. 이미 있던 준비물은 지우고 새로 만든다.

    같은 문제를 서브에이전트(가벼운 모델) 여럿이 따로 읽게 일을 나눈다(jobs). 읽기마다 그림을 다르게 나눠 준다:
    쪽을 문제 서너 개씩으로 나눈 조각(half), 문제마다 한 장(each), 쪽 한 장(page). 그림 조각이 맞게 잘렸는지는
    서브에이전트가 보지 않는다(사람이 결과물에서 본다). 로컬 글자 인식이 읽은 글은 문제마다 적어 두기만 하고(ocr.txt)
    옮겨 적은 뒤의 검사가 쓴다.
    """
    from . import figures, layout, reading, render, store
    task = tasks.load(workspace, task_id)
    if not task["items"]:
        raise ValueError("태스크에 담긴 문제가 없습니다")
    run = run_dir(workspace, task_id)
    if run.exists():
        shutil.rmtree(run)
    (run / "batches").mkdir(parents=True)
    (run / "sheets").mkdir()
    (run / "reads").mkdir()
    indexes: dict = {}
    opened: dict[str, object] = {}
    lift_documents(workspace, task["items"])
    problems, todo = [], []
    for n, item in enumerate(task["items"], 1):
        pdf = Path(item["pdf_path"])
        idx = opened[item["pdf_id"]] = _index(item, indexes)
        page = item["page"] - 1
        folder = run / "problems" / f"{n:02d}"
        folder.mkdir(parents=True)
        entry = {"n": n, "pid": item["pid"], "from": f"{item['pdf_name']} {item['page']}쪽 {item['label']}",
                 "pdf_path": str(pdf), "pdf_id": item["pdf_id"], "page": item["page"], "label": item["label"],
                 "bbox": item["bbox"], "stem": item.get("stem"), "folder": f"problems/{n:02d}",
                 "page_width": idx.sizes()[page][0], "sha256": reading.source_sha256(idx.path, idx.dir)}
        kept = store.ensure_reading(store.folder(workspace, item), item, idx)
        if kept is not None and kept["status"] == "ok":
            # 글이 이미 준비된 문제(한글 문서에서 그대로 꺼냈거나 앞서 옮겨 적어 둔 것): 서브에이전트에게 맡기지 않는다
            src = store.folder(workspace, item)
            shutil.copyfile(src / "clean.png", folder / "clean.png")
            (folder / "text.md").write_text(kept["text"], encoding="utf-8")
            for f in kept["figures"]:
                shutil.copyfile(src / f["file"], folder / f["file"])
            problems.append({**entry, "figures": kept["figures"], "ready": True, "worker": kept.get("model") or "unknown"})
            continue
        s = idx.scale(page)
        lay = idx.layout(page)
        box_px = tuple(int(round(v * s)) for v in item["bbox"])
        badges = [(t.x0, t.y0, t.x1, t.y1) for t in layout.badge_tokens(lay) + layout.fill_tokens(lay)]
        found = figures.find_figures(lay, box_px, skip=badges)
        parts = []
        if item.get("stem"):
            parts.append(_clip(idx, page, item["stem"], SOURCE_DPI))
        parts.append(_clip(idx, page, item["bbox"], SOURCE_DPI))
        # 깨끗한 문제 그림: 글로 옮기지 못했을 때 문서에 통째로 넣는다
        render.imwrite(folder / "clean.png", _stack(parts))
        figs = []
        for k, (fx0, fy0, fx1, fy1) in enumerate(found[:len(string.ascii_uppercase)]):
            name = string.ascii_uppercase[k]
            rect_pt = [fx0 / s, fy0 / s, fx1 / s, fy1 / s]
            render.imwrite(folder / f"fig{name}.png", _scan_clip(idx, page, rect_pt))
            w_mm, h_mm = (rect_pt[2] - rect_pt[0]) * MM_PER_PT, (rect_pt[3] - rect_pt[1]) * MM_PER_PT
            shrink = min(1.0, MAX_FIGURE_MM[0] / w_mm, MAX_FIGURE_MM[1] / h_mm)
            figs.append({"name": name, "file": f"fig{name}.png", "width_mm": round(w_mm * shrink, 1),
                         "height_mm": round(h_mm * shrink, 1), "rect": [round(v, 1) for v in rect_pt]})
        # 로컬 글자 인식이 읽은 글. 서브에이전트에게는 주지 않는다(재 보니 가벼운 모델이 그림 대신 이 글의 틀린 숫자를
        # 베꼈다: 3√6 → 36). 옮겨 적은 뒤에 검사(check)가 이 글과 한글을 견줘, 어긋난 대목만 다시 보게 한다
        seen = [store._local_text(lay, tuple(int(round(v * s)) for v in r)) for r in ([item["stem"]] if item.get("stem") else []) + [item["bbox"]]]
        (folder / "ocr.txt").write_text("\n".join(seen), encoding="utf-8")
        problems.append({**entry, "figures": figs})
        todo.append(problems[-1])
    pages: dict[tuple[str, int], list[dict]] = {}
    for p in todo:
        pages.setdefault((p["pdf_id"], p["page"]), []).append(p)
    views: dict[tuple[str, int], dict[str, list]] = {}
    for (pdf_id, page), members in pages.items():
        img, geo = _sheet(opened[pdf_id], page - 1, members)
        stem = f"{pdf_id[:6]}-p{page}"
        sheet = run / "sheets" / f"{stem}.jpg"
        render.imwrite(sheet, img, 88)
        made = {"page": [(sheet, members)], "half": [], "each": []}
        for k, (piece, group) in enumerate(_pieces(img, geo, members), 1):
            render.imwrite(run / "sheets" / f"{stem}-h{k}.png", piece)
            made["half"].append((run / "sheets" / f"{stem}-h{k}.png", group))
        for m in members:
            one = run / "sheets" / f"{stem}-n{m['n']:02d}.png"
            render.imwrite(one, _crop(img, geo, [m]))
            made["each"].append((one, [m]))
            m["sheet"], m["crop"] = str(sheet), str(one)
        views[(pdf_id, page)] = made
    reads = max(1, int(reads or _config(workspace).get("reads", READS)))
    size = batch_size or JOB_PROBLEMS
    jobs = []
    for k in range(1, reads + 1):
        view = VIEWS[(k - 1) % len(VIEWS)]
        group: list = []
        for key in list(pages) + [None]:
            if key is not None:
                group.append(key)
            if group and (key is None or sum(len(pages[g]) for g in group) >= size):
                name = f"r{k}-{sum(1 for j in jobs if j['read'] == k) + 1:02d}"
                images = [im for g in group for im in views[g][view]]
                job = {"name": name, "read": k, "view": view, "file": str(run / "batches" / f"{name}.md"),
                       "out": str(run / "reads" / f"{name}.md"), "problems": [m["n"] for _, ms in images for m in ms]}
                Path(job["file"]).write_text(_instructions(images, view, out=job["out"]), encoding="utf-8")
                jobs.append(job)
                group = []
    state = {"task": task_id, "name": task.get("name", task_id), "prepared": time.strftime("%Y-%m-%d %H:%M:%S"),
             "problems": problems, "batches": [j["file"] for j in jobs], "jobs": jobs, "reads": reads if jobs else 0,
             "sheets": len(pages)}
    (run / "run.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return state


INTRO = {
    "page": "아래 쪽 그림을 열어 보고, 그림에 번호가 붙은 문제들을",
    "half": "아래 그림들을 열어 보고, 그림에 번호가 붙은 문제들을",
    "each": "아래 문제 그림들을 열어 보고, 그림마다 번호가 붙은 문제 하나씩을",
}


def _instructions(images: list[tuple], view: str = "page", out: str | Path | None = None, run: Path | None = None) -> str:
    """전사 담당 서브에이전트(가벼운 모델)에게 줄 지시문. 짧고 구체적으로, 예시를 붙인다.

    images는 [(그림 파일, 그 그림에 든 문제들)]. out을 주면 옮긴 글을 그 파일 하나에 `@@ 번호` 줄로 나눠 적게 하고
    (파일 쓰기를 한 번만 하게 하려는 것이다. 문제마다 파일을 쓰게 하면 차례가 늘고, 명령으로 한꺼번에 쓰려다 `$`를
    날려 먹은 일이 있었다), 안 주면 문제마다 run 아래의 text.md에 적게 한다(다시 시킬 때).
    """
    lines = [
        "# 수학 문제 옮겨 적기",
        "",
        f"{INTRO[view]} 보이는 그대로 글로 옮겨 적어 " + ("파일 하나에 저장하세요." if out else "지정한 파일에 저장하세요."),
        "문제를 풀거나 고치지 말고, 보이는 대로만 옮깁니다. 번호가 붙은 문제를 빠짐없이 처리하세요.",
        "필요한 것은 이 파일에 다 있습니다. 스킬을 불러오거나 명령을 실행하지 말고, 그림을 열어 보고 파일을 쓰기만 하세요.",
        "",
        f"**그림은 모두 {len(images)}장이고, 한 번씩만 엽니다(한꺼번에 열어도 됩니다).** 같은 그림을 다시 열거나 다른 그림 파일을 찾아 열지 않습니다.",
        "",
        "## 할 일",
        "",
    ]
    if out:
        lines += [f"옮긴 글은 **파일 하나** `{out}` 에 모두 적습니다. 파일 쓰기 도구(`write_to_file`)를 **한 번만** 호출해 저장하고, 명령(셸)으로 파일을 쓰지 않습니다.",
                  "문제마다 첫 줄에 `@@ 번호` 를 적고(예: `@@ 12`), 그 아래 줄부터 그 문제를 옮겨 적습니다. 번호는 그림의 `[숫자]` 이름표의 숫자입니다.",
                  "**번호를 순서대로 세어 매기지 않습니다.** 파란 테두리 위의 이름표를 읽어 그 숫자를 씁니다. 예제처럼 보이는 문제도 이름표가 붙어 있으면 빠뜨리지 않습니다.", ""]
    for image, members in images:
        lines += [f"### 그림 `{image}`", ""]
        for p in members:
            names = ", ".join(f"{p['n']}{f['name']}" for f in p["figures"]) or "없음"
            where = "" if out else f" → `{run / p['folder'] / 'text.md'}` 에 저장"
            lines += [f"- **[{p['n']}]**{where} (이름표 붙은 그림: {names})"
                      + ("  ※ `[%d]+` 테두리는 이 문제가 함께 쓰는 발문입니다. 문제 글 맨 앞에 옮겨 적습니다." % p["n"] if p.get("stem") else "")]
        lines += [""]
    lines += [
        "## 그림 보는 법",
        "",
        "- 파란 테두리와 `[숫자]` 이름표가 문제 하나의 범위입니다. 옅게 흐려진 부분이나 테두리 밖에 걸쳐 보이는 글은 고르지 않은 것이니 옮기지 않습니다.",
        "- 그림에 적힌 문장을 **글자 그대로** 옮깁니다. 낱말·조사·어미를 바꾸거나 줄이거나 보태지 않습니다. 부정하는 말(`않은`, `아닌`, `없는`)과 `이상`·`이하` 같은 말도 그림에 적힌 그대로 적습니다.",
        "- 수식 바로 뒤에 붙은 조사도 그림의 글자를 그대로 적습니다. 수식을 소리 내어 읽었을 때 어울리는 조사로 바꾸지 않습니다.",
        "- 수식은 지수(`x^8`), 근호(`3\\sqrt{6}`), 윗줄(`\\overline{\\mathrm{AB}}`)을 빠뜨리지 않습니다. 그림에 윗줄이 없는 `선분 AB`에는 윗줄을 붙이지 않습니다.",
        "",
        "## 적는 규칙",
        "",
        "1. **버릴 것**: 문제 번호, 맨 위 줄 앞의 번호 범위(`[0020~0021]`), 난이도 표시(★, 상·중·하), `대표문제`·`서술형`·`고난도` 같은 꼬리표, 출처(○○고, 2021학년도 수능 등), 쪽 안내, QR코드. 적지 않습니다.",
        "2. **남길 것**: 문제 글, 수식, 선택지, 보기·조건 상자, 배점(`[4점]`).",
        "3. **수식**은 LaTeX로 적고 `$` 로 감쌉니다. 예: `$x^2 + 2x - 1$`, `$\\frac{1}{2}$`, `$\\sin A$`. 글자 하나짜리 문자(`$x$`, `$A$`)와 숫자가 들어간 식도 수식으로 적습니다.",
        "   도형의 점·변·도형 이름처럼 **기울지 않고 곧게 선 알파벳**은 `\\mathrm{}` 으로 감쌉니다. 예: `$\\mathrm{ABC}$`, `$\\overline{\\mathrm{AB}}$`, `$\\angle \\mathrm{B}$`. 기울어진 글자는 `$A$` 그대로 둡니다.",
        "4. 한 줄을 통째로 차지하는 가운데 수식은 그 줄에 `$$ ... $$` 만 적습니다.",
        "5. **선택지**는 `① ... ② ... ③ ... ④ ... ⑤ ...` 로 한 줄에 적습니다.",
        "6. **테두리 상자**(보기, 조건)는 `::: box 보기` 줄로 시작하고 `:::` 줄로 끝냅니다. 이름이 없는 상자는 `::: box` 로 시작합니다.",
        "7. **그림**: 빨간 테두리와 이름표(`12A`, `12B`처럼 문제 번호 + 글자)가 붙은 부분은 그림입니다. 그림을 문서에 넣을지는 직접 판단하세요: 문제를 이해하거나 푸는 데 쓰이면 넣고, 꾸밈일 뿐이면 넣지 않습니다. 넣을 때는 그림이 놓인 자리에 `![](figure:A)` 처럼 **글자만** 써서 한 줄로 적고, 그림 속 글자는 옮기지 않습니다.",
        "   - 빨간 테두리가 그림을 제대로 감쌌는지는 확인하지 않습니다(나중에 사람이 봅니다).",
        "   - 다만 빨간 테두리 안에 **글과 수식뿐**이면(보기·조건·증명 상자나 그 일부를 감싼 것) 그림이 아닙니다. `![](figure:…)`를 적지 않고 그 내용을 글로 옮깁니다.",
        "   - 문제를 푸는 데 필요한 그림인데 빨간 테두리가 없으면, 그 자리에 `<!-- 그림 빠짐 -->` 이라고만 한 줄 적습니다. 그 그림 속의 글(말풍선 속 말 등)은 글로 옮겨 적습니다.",
        "   - 세로로 쓴 나눗셈 과정, 표, 도형을 `-----` 나 빈칸으로 글자 그림처럼 그리지 않습니다. 빨간 테두리가 있으면 `![](figure:A)`, 없으면 `<!-- 그림 빠짐 -->` 입니다.",
        "8. 줄 하나가 문단 하나입니다. 교재에서 자리가 모자라 다음 줄로 넘어갔을 뿐인 문장은 줄을 바꾸지 말고 이어서 한 줄로 적습니다. `(1)`, `(2)` 같은 작은 문제는 줄을 바꿔 적습니다. 답을 써넣는 빈칸은 `□` 로 적습니다. 빈칸 안에 `(가)`, `(나)`, `㉠` 같은 표시가 들어 있으면 그 표시를 넣어 `$\\boxed{(가)}$` 로 적습니다(수식 안이면 `\\frac{n!}{\\boxed{(가)}}` 처럼). 표시를 빼먹거나 빈칸 옆에 따로 적지 않습니다.",
        f"9. 손으로 쓴 낙서(풀이 흔적, 동그라미, 밑줄)는 옮기지 않습니다. 낙서가 있었다면 맨 끝 줄에 `<!-- {DOODLE_NOTE} -->` 라고 적습니다.",
        ("10. 파일에는 `@@ 번호` 줄과 옮긴 내용만 적습니다." if out else "10. 파일 하나에 문제 하나입니다. 파일에는 옮긴 내용만 적습니다.")
        + " 설명, 인사말, 코드 블록 표시(```)는 넣지 않고, 수식을 백틱(`)으로 감싸지 않습니다.",
        "",
        "## 예시",
        "",
        "`[12]` 테두리 안에 `0123 ★★ 그림과 같이 … 삼각형 ABC가 있다. … 넓이는? (빨간 테두리 12A) ① 6 ② 8 ③ 10 ④ 12 ⑤ 14` 가 보이면 "
        + ("이렇게 적습니다." if out else "12번의 파일에 이렇게 적습니다."),
        "",
        *(["    @@ 12"] if out else []),
        "    그림과 같이 $\\overline{\\mathrm{AB}} = 6$, $\\angle \\mathrm{B} = 90^\\circ$인 직각삼각형 $\\mathrm{ABC}$가 있다. $\\overline{\\mathrm{BC}} = 4$일 때, 삼각형 $\\mathrm{ABC}$의 넓이는?",
        "    ![](figure:A)",
        "    ① $6$  ② $8$  ③ $10$  ④ $12$  ⑤ $14$",
        "",
        "보기 상자가 있는 문제는 이렇게 적습니다.",
        "",
        "    보기에서 옳은 것만을 있는 대로 고른 것은?",
        "    ::: box 보기",
        "    ㄱ. $a > 0$",
        "    ㄴ. $b^2 - 4ac < 0$",
        "    :::",
        "    ① ㄱ  ② ㄴ  ③ ㄱ, ㄴ",
        "",
        "## 끝나면",
        "",
        "옮긴 문제 수만 한 줄로 알려 주세요. 예: `5문제 저장`",
        "",
    ]
    return "\n".join(lines)


def _hangul(text: str) -> str:
    return "".join(re.findall(r"[가-힣]", text))


def _local_hangul(folder: Path) -> str:
    """문제 그림을 로컬 OCR로 읽어 한글만 모은다(수식은 믿을 수 없으므로 보지 않는다)."""
    if (folder / "ocr.txt").is_file():               # 준비할 때 쪽 레이아웃으로 읽어 둔 것이 있으면 그것을 쓴다
        text = (folder / "ocr.txt").read_text(encoding="utf-8")
        for tag in DROPPED_TAGS:
            text = text.replace(tag, "")
        return _hangul(text)
    from . import layout, ocr, render
    img = render.imread(folder / "clean.png")
    lay = layout.analyze(img)
    h, w = img.shape[:2]
    crops = layout.line_crops(lay, 0, 0, w, h, max_lines=60)
    text = "".join(t for t, s in ocr.read_crops(crops) if s >= 0.5)
    for tag in DROPPED_TAGS:                       # 옮겨 적을 때 버리기로 한 꼬리표는 없는 것이 맞다
        text = text.replace(tag, "")
    return _hangul(text)


# 한 글자만 달라도 뜻이 뒤집히는 말. 기계가 읽은 글에 더 많이 나오면 옮긴 글에서 빠졌거나 바뀐 것으로 보고 알린다
TURNING_WORDS = ("이상", "이하", "초과", "미만", "최대", "최소", "않은", "않는", "아닌", "없는", "모든", "적어도")


def _written_hangul(blocks: list) -> str:
    """옮긴 글에서 그림에도 글자로 찍혀 있을 것만 모은 한글: 글과 선택지, 상자의 이름. 그림 자리 표시는 뺀다."""
    out = []
    for b in blocks:
        if isinstance(b, transcript.Para):
            out += [r.text or r.latex for r in b.runs]
        elif isinstance(b, transcript.Choices):
            out += [r.text or r.latex for o in b.options for r in o]
        elif isinstance(b, transcript.Boxed):
            out += [b.label, _written_hangul(b.blocks)]
    return _hangul(" ".join(out))


def hangul_mismatches(machine_text: str, blocks: list) -> list[dict]:
    """로컬 글자 인식이 읽은 글과 옮긴 글의 한글이 어긋난 대목들: [{"machine": 기계 쪽, "written": 옮긴 글 쪽}].

    어긋난 곳은 [ ]로 싸고 앞뒤 네 글자를 붙인다. 한글만 본다: 기계는 수식·숫자를 못 읽으므로 그것까지 견주면
    틀린 숫자를 정답처럼 들이밀게 된다(가벼운 모델이 그대로 베낀다). 기계가 한 줄을 통째로 놓치는 일이 잦아
    헛짚는 것도 섞인다. 그래서 이 목록은 "여기를 그림에서 다시 보라"는 표시이지 고칠 답이 아니다.
    두 글자 넘게 어긋난 곳만 낸다. 한 글자 차이는 거의 다 기계의 잘못 읽기라(몫→못, 숫→슷, 켤→컬) 넣으면 헛짚는 문제가
    두 배가 된다. 한 글자로 뜻이 뒤집히는 말(이상/이하, 않은 …)은 따로 세어 본다(TURNING_WORDS).
    빈칸의 표시 (가)·(나)만 다른 곳도 뺀다(기계는 상자 속 표시를 못 읽는다).
    """
    for tag in DROPPED_TAGS:
        machine_text = machine_text.replace(tag, "")
    a, b = _hangul(re.sub(r"^\s*\S{0,6}?\d{2,4}\s*[·•.]?\s*[상중하]?\s*[·•.]?", "", machine_text)), _written_hangul(blocks)
    if len(a) < 12:
        return []
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal" or max(i2 - i1, j2 - j1) < 2 or not (a[i1:i2] + b[j1:j2]).strip("가나다라마바사"):
            continue
        out.append({"machine": a[max(0, i1 - 4):i1] + "[" + a[i1:i2] + "]" + a[i2:i2 + 4],
                    "written": b[max(0, j1 - 4):j1] + "[" + b[j1:j2] + "]" + b[j2:j2 + 4]})
    for word in TURNING_WORDS:
        if a.count(word) > b.count(word) and not any(word in m["machine"].replace("[", "").replace("]", "") for m in out):
            out.append({"machine": f"'{word}'가 {a.count(word)}번", "written": f"'{word}'가 {b.count(word)}번"})
    return out


def _found_in(mine: str, theirs: str) -> float:
    """로컬 OCR이 읽은 한글(mine) 가운데, 옮긴 글(theirs)에도 두 글자 이상 이어서 나오는 것의 비율.

    로컬 OCR은 줄을 통째로 놓치는 일이 잦아서, 양쪽이 얼마나 같은지를 보면 멀쩡한 전사에도 경고가 난다.
    그래서 "읽힌 조각이 옮긴 글 안에 있는가"만 본다. 다른 문제를 옮겼다면 이 값이 낮게 나온다.
    """
    blocks = difflib.SequenceMatcher(None, mine, theirs, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks if b.size >= 2) / max(1, len(mine))


# 글이 그림을 가리키는 말. 이런 말이 있는데 그림도 "빠짐" 표시도 없으면 그림이 말없이 빠진 것이다
FIGURE_WORDS_RE = re.compile(r"(?:오른쪽|왼쪽|아래|위|다음|위의|아래의)\s*그림|그림과\s*같|그림은|그림에서|그림의")


def _guess_figure(run: Path, p: dict, opened: dict) -> dict | None:
    """그림 후보를 못 찾았는데 그림이 있다는 문제: 문제 상자 안에서 큰 획이 모인 곳을 그림으로 잘라 본다(figures.snap).
    색칠된 입체도형처럼 find_figures가 놓치는 그림을 이렇게 건진다. 가는 선뿐인 그림은 못 건진다(상자 전체가 나오면 버린다)."""
    from . import figures, layout, render
    try:
        idx = _index(p, opened)
        page = p["page"] - 1
        s, lay = idx.scale(page), idx.layout(page)
    except (OSError, ValueError, KeyError):
        return None
    box = tuple(int(round(v * s)) for v in p["bbox"])
    badges = [(t.x0, t.y0, t.x1, t.y1) for t in layout.badge_tokens(lay) + layout.fill_tokens(lay)]
    rect = figures.snap(lay, box, box, badges)
    if rect is None or (rect[2] - rect[0]) * (rect[3] - rect[1]) > 0.5 * (box[2] - box[0]) * (box[3] - box[1]):
        return None
    rect_pt = [v / s for v in rect]
    render.imwrite(run / p["folder"] / "figA.png", _scan_clip(idx, page, rect_pt))
    w_mm, h_mm = (rect_pt[2] - rect_pt[0]) * MM_PER_PT, (rect_pt[3] - rect_pt[1]) * MM_PER_PT
    shrink = min(1.0, MAX_FIGURE_MM[0] / w_mm, MAX_FIGURE_MM[1] / h_mm)
    return {"name": "A", "file": "figA.png", "width_mm": round(w_mm * shrink, 1), "height_mm": round(h_mm * shrink, 1),
            "rect": [round(v, 1) for v in rect_pt], "guessed": True}


# 표를 글자로 옮겨 적은 줄(`| 칸 | 칸 |`, `|---|---|`). 양식에는 표를 글로 넣을 자리가 없어 그대로 두면 막대기가 섞인 글이 된다
TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")


def _drop_tables(text: str) -> str:
    """읽기 하나에서 표를 글자로 옮긴 줄을 뺀다. 그 읽기에 그림 표시가 없었으면 '그림 빠짐' 표시를 대신 둔다."""
    lines = text.split("\n")
    kept = [line for line in lines if not TABLE_ROW_RE.match(line)]
    if len(kept) == len(lines):
        return text
    out = "\n".join(kept)
    return out if vote.FIGURE_RE.search(out) or vote.MISSING_RE.search(out) else out.rstrip() + f"\n<!-- {MISSING_FIGURE} -->"


SECTION_RE = re.compile(r"(?m)^[ \t]*@@[ \t]*\[?(\d+)\]?[ \t]*$")


def _split_reads(state: dict) -> dict[int, list[tuple[int, str]]]:
    """서브에이전트들이 쓴 읽기 결과(`@@ 번호` 줄로 나뉜 파일)를 문제별로 모은다: {문제 차례: [(읽기 번호, 글)]}."""
    out: dict[int, list[tuple[int, str]]] = {}
    for job in state.get("jobs", []):
        f = Path(job["out"])
        if not f.is_file():
            continue
        parts = SECTION_RE.split(f.read_text(encoding="utf-8-sig"))
        for k in range(1, len(parts) - 1, 2):
            n, body = int(parts[k]), parts[k + 1].strip()
            if n in job["problems"] and body and not any(r == job["read"] for r, _ in out.get(n, [])):
                out.setdefault(n, []).append((job["read"], body))
    return out


def wait_reads(workspace: str | Path, task_id: str, wait: float = 0.0) -> dict:
    """서브에이전트들이 읽기 결과를 다 쓸 때까지 기다린다(최대 wait초).

    {"jobs": 일의 수, "pending": 아직 결과가 없는 일의 이름들, "short": 아직 덜 읽힌 문제들}
    short가 비어 있으면 합칠 수 있다(늦는 서브에이전트 한둘은 기다리지 않아도 된다). 처음 두 번은 문제마다
    MIN_READS+1번 읽힐 때까지 기다린다: 재 보니 세 번만 읽힌 채 합친 문제들에 틀린 글이 남았고, 늦게 온 읽기까지
    넣으니 모두 맞았다. 두 번 기다려도 모자라면 MIN_READS번으로 합친다.
    """
    run = run_dir(workspace, task_id)
    state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    jobs = state.get("jobs", [])
    todo = [p["n"] for p in state["problems"] if not p.get("ready")]
    plenty = min(MIN_READS + 1, state.get("reads") or 1)
    need = plenty if state.get("waited", 0) < 2 else min(MIN_READS, state.get("reads") or 1)
    deadline = time.time() + wait
    last, quiet = None, time.time()
    while True:
        pending = [j["name"] for j in jobs if not Path(j["out"]).is_file() or not Path(j["out"]).stat().st_size]
        if not pending or time.time() >= deadline:
            break
        if len(pending) != last:
            last, quiet = len(pending), time.time()
        elif time.time() - quiet > STRAGGLER_WAIT:
            # 한동안 새 결과가 없다. 남은 한둘은 늦는 서브에이전트다(재 보니 30개 중 2개가 7분 늦었다).
            # 모든 문제가 넉넉히 읽혔으면 더 기다리지 않는다
            got = _split_reads(state)
            if all(len(got.get(n, [])) >= plenty for n in todo):
                break
        time.sleep(1.0)
    if jobs and wait:
        time.sleep(1.5)                                  # 방금 생긴 파일이 다 쓰일 틈
    got = _split_reads(state)
    short = [n for n in todo if len(got.get(n, [])) < need and not (run / "problems" / f"{n:02d}" / "text.md").is_file()] if jobs else []
    if short and wait:
        state["waited"] = state.get("waited", 0) + 1
        (run / "run.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"jobs": len(jobs), "pending": pending, "short": short}


def collect(workspace: str | Path, task_id: str) -> dict[int, dict]:
    """따로 읽은 결과들을 문제마다 하나로 합쳐 text.md로 적는다(vote.combine). {문제 차례: 합친 기록}

    형식이 깨진 읽기와 수식이 통째로 빠진 읽기는 빼고 합친다. 읽기가 그대로면 다시 합치지 않는다(합친 뒤에 고쳐 쓴
    text.md를 덮어쓰지 않으려는 것이다).
    """
    run = run_dir(workspace, task_id)
    state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    if not state.get("jobs"):
        return {}
    reads = _split_reads(state)
    report = {}
    opened: dict = {}
    changed = False
    for p in state["problems"]:
        if p.get("ready"):
            continue
        folder = run / p["folder"]
        got = sorted(reads.get(p["n"], []))
        known = {f["name"] for f in p["figures"] if not f.get("guessed")}
        key = hashlib.sha1("\x1e".join(f"{k}:{raw}" for k, raw in got).encode("utf-8")).hexdigest()
        note = folder / "votes.json"
        if note.is_file() and (folder / "text.md").is_file():
            old = json.loads(note.read_text(encoding="utf-8"))
            if old.get("key") == key:
                report[p["n"]] = old
                continue
        valid = []
        for k, raw in got:
            text = _drop_tables(transcript.box_labels(_written(_clean(raw), p)))
            names, _ = vote.marks(text, known)
            if not transcript.parse(vote.with_marks(text, names, False, known), figures=known).errors:
                valid.append((k, text))
        if len(valid) >= 3:
            # 숫자가 다른 읽기들의 절반도 안 되는 읽기: 수식이 통째로 빠진 것이다(파일을 명령으로 써서 `$…`가 사라진 일이 있었다)
            digits = [len(re.findall(r"\d", text)) for _, text in valid]
            mid = sorted(digits)[len(digits) // 2]
            valid = [v for v, d in zip(valid, digits) if mid < 4 or d >= 0.5 * mid]
        kept = vote.same_problem([text for _, text in valid])
        strays = len(valid) - len(kept)                    # 다른 문제의 글을 이 번호에 적은 읽기
        valid = [valid[i] for i in kept]
        info = {"key": key, "reads": len(got), "valid": len(valid), "status": "none", "open": [], "strays": strays}
        if valid:
            seen = (folder / "ocr.txt").read_text(encoding="utf-8") if (folder / "ocr.txt").is_file() else ""
            for tag in DROPPED_TAGS:
                seen = seen.replace(tag, "")
            res = vote.combine([text for _, text in valid], known, seen)
            text = res["text"]
            if res["used"] < 0 and transcript.parse(text, figures=known).errors:
                text = res["fallback"]
            names, missing = set(res["figures"]["names"]), res["figures"]["missing"]
            if not names and not missing and FIGURE_WORDS_RE.search(text):
                missing = True                              # 글은 그림을 가리키는데 아무 읽기도 표시하지 않았다
            if p["figures"] and not known:
                p["figures"], changed = [], True            # 지난번에 어림으로 자른 그림은 버리고 다시 본다
            if missing and not names and not known:
                fig = _guess_figure(run, p, opened)
                if fig:
                    p["figures"], changed = [fig], True
                    names, missing, info["figure_guess"] = {"A"}, False, True
            text = vote.with_marks(text, names, missing, known | names)
            text, swapped = vote.fix_particles(text, seen)
            if swapped:
                info["particles"] = swapped
            (folder / "text.md").write_text(text.strip() + "\n", encoding="utf-8")
            info.update({"status": res["status"], "open": [[vote.show(x) for x in o["variants"]] for o in res["open"]][:6],
                         "figures_agreed": res["figures"]["agreed"]})
        note.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        report[p["n"]] = info
    if changed:
        (run / "run.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def redo_job(workspace: str | Path, task_id: str, numbers: list[int]) -> str:
    """검사를 통과하지 못한 문제들만 다시 옮겨 적게 하는 지시문을 만들고 그 경로를 돌려준다(문제마다 그림 한 장, text.md에 바로 쓴다)."""
    run = run_dir(workspace, task_id)
    state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    images = [(p.get("crop") or p.get("sheet") or str(run / p["folder"] / "clean.png"), [p]) for p in state["problems"] if p["n"] in numbers]
    path = run / "batches" / "redo.md"
    path.write_text(_instructions(images, "each", run=run), encoding="utf-8")
    for p in state["problems"]:
        if p["n"] in numbers:
            (run / p["folder"] / "text.md").unlink(missing_ok=True)     # 새로 쓴 것이 생겼는지 알 수 있게
    state["redone"] = True
    (run / "run.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(path)


def check(workspace: str | Path, task_id: str, compare_ocr: bool = True) -> dict:
    """전사 결과를 검사한다. 문제마다 {"n", "status": ok|warn|error|missing, "errors", "warnings"}.
    여러 번 읽힌 실행이면 먼저 읽기들을 합친다(collect)."""
    run = run_dir(workspace, task_id)
    votes = collect(workspace, task_id)
    state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    results = []
    for p in state["problems"]:
        folder = run / p["folder"]
        res = {"n": p["n"], "from": p["from"], "errors": [], "warnings": [], "doodle": False}
        md = folder / "text.md"
        v = votes.get(p["n"])
        if not md.is_file() or not md.read_text(encoding="utf-8-sig").strip():
            res["folder"] = str(folder)
            res["sheet"] = p.get("crop") or p.get("sheet", "")
            res["status"] = "missing"
            res["errors"].append("읽은 결과가 없거나 모두 형식에 맞지 않습니다" if v else "전사 결과 파일이 없습니다")
            results.append(res)
            continue
        if v and v["status"] in ("split", "close"):
            res["warnings"].append(("여러 번 읽은 것이 서로 달라 정하지 못한 곳이 있습니다" if v["status"] == "split" else
                                    "읽기가 갈린 곳이 있습니다(많은 쪽을 골랐습니다)") + "(결과물에서 확인): " + "; ".join(
                " / ".join(f"'{x}'" for x in spot) for spot in v["open"][:3]))
        elif v and v["status"] == "single" and state.get("reads", 1) > 1:
            res["warnings"].append("한 번만 제대로 읽혀 견주지 못했습니다(결과물에서 확인)")
        if v and v.get("figure_guess"):
            res["warnings"].append("그림 자리를 기계가 어림으로 잡아 잘라 넣었습니다(결과물에서 그림이 맞는지 확인)")
        voted = bool(v) and v.get("valid", 0) >= 3
        raw = _clean(md.read_text(encoding="utf-8-sig"))
        text = _written(raw, p)
        res["doodle"] = DOODLE_NOTE in text
        parsed = transcript.parse(text, figures={f["name"] for f in p["figures"]})
        res["errors"] += parsed.errors
        res["warnings"] += parsed.warnings
        if re.search(r"^\s*[-=_─━]{5,}\s*$", raw, re.M):
            res["errors"].append("그림(세로 셈, 표 등)을 글자로 그려 넣었습니다. 글자 그림을 지우고 그 자리에 `<!-- 그림 빠짐 -->`만 적으세요")
        if MISSING_FIGURE in text:
            res["warnings"].append("문제에 필요한 그림을 기계가 찾지 못해 빠졌습니다(사람이 결과물에 넣어야 합니다)")
        if not parsed.errors:
            _save_reading(folder, p, parsed, res)
        if re.match(r"\s*\d{2,4}[.)]?\s+[가-힣]", raw):
            res["warnings"].append("문제 번호가 그대로 들어간 것 같습니다")
        if compare_ocr and not res["errors"]:
            mine, theirs = _local_hangul(folder), _hangul(transcript.plain_text(parsed.blocks))
            if len(mine) >= 12:
                found = _found_in(mine, theirs)
                res["hangul_match"] = round(found, 2)
                if found < MIN_HANGUL_MATCH:
                    res["warnings"].append("그림에서 읽힌 한글이 옮긴 글에 거의 없습니다(다른 문제를 옮겼을 수 있으니 확인하세요)")
            if (folder / "ocr.txt").is_file() and not voted:
                # 세 번 넘게 읽혀 다수결을 거친 글은 기계 글과 다시 견주지 않는다(헛짚는 것이 더 많다)
                res["mismatches"] = hangul_mismatches((folder / "ocr.txt").read_text(encoding="utf-8"), parsed.blocks)
        if res["errors"]:
            res["folder"] = str(folder)
            res["sheet"] = p.get("crop") or p.get("sheet", "")
        if res.get("mismatches"):
            res["sheet"] = p.get("sheet", "")
            res["file"] = str(md)
            if state.get("reviewed"):
                # 한 번 다시 보게 한 뒤에도 남은 것: 기계가 틀렸거나 고치지 못한 것이다. 더 시키지 않고 사람에게 넘긴다
                res["warnings"].append("기계가 읽은 글과 한글이 다른 대목이 남아 있습니다: " + "; ".join(
                    f"기계 '{m['machine']}' / 옮긴 글 '{m['written']}'" for m in res["mismatches"][:4]))
        # '다시 볼 곳 있음'은 한 번만 읽히는 예전 방식에서만 쓴다. 여러 번 읽히는 방식은 다수결과 사람에게 알리기로 끝낸다
        review = bool(res.get("mismatches")) and not state.get("reviewed") and not state.get("jobs")
        if res.get("mismatches") and state.get("jobs"):
            res["warnings"].append("기계가 읽은 글과 한글이 다른 대목이 있습니다(결과물에서 확인): " + "; ".join(
                f"기계 '{m['machine']}' / 옮긴 글 '{m['written']}'" for m in res["mismatches"][:3]))
        res["status"] = "error" if res["errors"] else "review" if review else ("warn" if res["warnings"] else "ok")
        results.append(res)
    if any(r["status"] == "review" for r in results):
        # 다시 보라고 알리는 것은 한 번뿐이다. 다음 검사부터는 남은 것을 사람이 볼 것으로 넘긴다
        state["reviewed"] = True
        (run / "run.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    report = {"task": task_id, "checked": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results,
              "ok": all(r["status"] in ("ok", "warn") for r in results)}
    (run / "check.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def _clean(text: str) -> str:
    """모델이 습관적으로 붙이는 코드 블록 표시를 걷어 낸다. 그림 이름은 쪽 그림의 이름표(12A)로 적어 와도 받는다."""
    lines = [l for l in text.replace("\r\n", "\n").split("\n") if not l.strip().startswith("```")]
    lines = [re.sub(r"`+(\$[^`\n]*?\$)`+", r"\1", l) for l in lines]
    return re.sub(r"\(figure:\s*\d+([A-Z])\s*\)", r"(figure:\1)", STEM_RANGE_RE.sub("", "\n".join(lines)))


def _written(text: str, p: dict) -> str:
    """서브에이전트가 쓴 글이면 다듬는다(글 속의 맨몸 수를 수식으로 감싼다).
    이미 다듬어 저장해 둔 글이나 한글 문서에서 꺼낸 글은 그대로 둔다."""
    if p.get("ready") or "store" in p:
        return text
    from . import store
    return store.tidy(text)


def _save_reading(folder: Path, p: dict, parsed, res: dict) -> None:
    """옮겨 적은 내용을 저장 형식(reading.json)으로 적어 둔다. 글을 고친 뒤 다시 검사하면 새로 적는다.
    한 번에 만들기(make)는 저장해 둔 것을 직접 옮겨 적으므로 여기서 하지 않는다(그 목록에는 sha256이 없다)."""
    from . import reading
    if "sha256" not in p:
        return
    (folder / "extras.json").unlink(missing_ok=True)
    doc, extras = reading.document(p, parsed.blocks, sha256=p["sha256"], worker=p.get("worker") or "subagent",
                                   page_width=p["page_width"], uncertain=bool(res["warnings"]),
                                   issues=[DOODLE_NOTE] if res["doodle"] else [])
    (folder / "reading.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    if extras:
        (folder / "extras.json").write_text(json.dumps(extras, ensure_ascii=False), encoding="utf-8")


def find_template(workspace: str | Path, template: str | Path | None = None) -> Path:
    ws = Path(workspace).resolve()
    # 작업 폴더에 둔 양식이 먼저다. 없으면 스킬에 든 기본 양식(HWPX로 들어 있다)을 쓴다
    assets = Path(__file__).parent / "assets"
    tpl = Path(template) if template else next((p for p in (ws / "양식.hwp", ws / "양식.hwpx", assets / "양식.hwp", assets / "양식.hwpx")
                                                if p.is_file()), None)
    if tpl is None or not tpl.is_file():
        raise FileNotFoundError("양식 파일(양식.hwp)을 찾을 수 없습니다. --template 으로 지정하세요")
    return tpl


def _png_size(path: Path) -> tuple[int, int]:
    """PNG의 가로·세로(픽셀). 그림을 통째로 풀지 않고 머리만 읽는다."""
    with path.open("rb") as f:
        head = f.read(24)
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def build(workspace: str | Path, task_id: str, template: str | Path | None = None, name: str | None = None,
          title: str | None = None, progress=None, session=None, preview: bool = True) -> dict:
    """전사 결과를 양식에 넣어 한글 파일을 만든다. 글로 옮기지 못한 문제는 문제 그림을 통째로 넣는다.

    session은 미리 띄워 둔 한글(writer.Session)이다. preview가 거짓이면 미리보기 그림은 칸이 넘쳤을 때만 만든다.
    """
    from .hwp import writer

    ws = Path(workspace).resolve()
    run = run_dir(ws, task_id)
    state = json.loads((run / "run.json").read_text(encoding="utf-8"))
    tpl = find_template(ws, template)
    problems, pictures, as_image, with_figures = [], {}, [], []
    books: dict = {}
    joined: list[int] = []                       # 교재에서 나란히 놓인 그림들을 한 장으로 묶어 넣은 문제
    for i, p in enumerate(state["problems"]):
        folder = run / p["folder"]
        md = folder / "text.md"
        parsed = None
        if md.is_file():
            parsed = transcript.parse(_written(_clean(md.read_text(encoding="utf-8-sig")), p), figures={f["name"] for f in p["figures"]})
        if parsed is None or parsed.errors:
            # 글로 옮기지 못했으면 문제 그림을 그대로 넣는다(문서가 비는 것보다 낫다).
            # 모델에게 보여 주려고 잘라 둔 clean.png는 220dpi라, 붙일 것은 교재의 스캔 해상도로 다시 자른다
            whole, w_mm = _whole_scan(folder, p, books)
            w_px, h_px = _png_size(whole)
            pictures[(i, "source")] = writer.Picture(whole, round(w_mm, 1), round(w_mm * h_px / w_px, 1))
            problems.append([transcript.Figure("source")])
            as_image.append(p["n"])
            continue
        for f in p["figures"]:
            pictures[(i, f["name"])] = writer.Picture(folder / f["file"], f["width_mm"], f["height_mm"])
        blocks, together = _merge_figures(_from_reading(folder, parsed.blocks), p, i, folder, pictures, books)
        if together:
            joined.append(p["n"])
        problems.append(blocks)
        if any(f["name"] in used for f in p["figures"] for used in [_figure_names(parsed.blocks)]):
            with_figures.append(p["n"])
    base = re.sub(r'[\\/:*?"<>|]', "_", name or state.get("name") or task_id).strip() or task_id
    out = run / "out"
    for old in out.glob("page-*.png") if out.is_dir() else []:
        old.unlink(missing_ok=True)
    result = writer.write_document(tpl, out, base, title or state.get("name") or task_id, problems, pictures, progress,
                                   session=session, preview=preview, first_page=bool(_config(ws).get("first_page", False)))
    # 작업 폴더에도 한 벌 둔다(이미 같은 이름이 있으면 번호를 붙인다)
    final = ws / f"{base}.hwp"
    k = 2
    while final.exists():
        final = ws / f"{base} ({k}).hwp"
        k += 1
    shutil.copyfile(result["hwp"], final)
    previews = []
    if result["pdf"]:
        import pymupdf
        with pymupdf.open(result["pdf"]) as doc:
            for n, page in enumerate(doc, 1):
                path = out / f"page-{n}.png"
                page.get_pixmap(dpi=110).save(str(path))
                previews.append(str(path))
    result.update({"file": str(final), "previews": previews, "as_image": as_image, "with_figures": with_figures, "joined": joined,
                   "overflow": result["pages"] > result["expected_pages"]})
    (run / "build.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def _whole_scan(folder: Path, p: dict, books: dict) -> tuple[Path, float]:
    """글로 옮기지 못한 문제를 통째로 붙일 그림과 그 폭(mm). 교재를 열 수 있으면 스캔 해상도로 새로 자르고(whole.png),
    열 수 없으면 잘라 둔 clean.png를 쓴다."""
    from . import render
    w_px, _ = _png_size(folder / "clean.png")
    fallback = folder / "clean.png", min(MAX_FIGURE_MM[0], w_px / SOURCE_DPI * 25.4)
    if not p.get("bbox") or not p.get("pdf_path"):
        return fallback
    try:
        idx = _index(p, books)
        rects = ([p["stem"]] if p.get("stem") else []) + [p["bbox"]]
        render.imwrite(folder / "whole.png", _stack([_scan_clip(idx, p["page"] - 1, r) for r in rects]))
    except (OSError, ValueError, KeyError, RuntimeError):
        return fallback
    return folder / "whole.png", min(MAX_FIGURE_MM[0], max(r[2] - r[0] for r in rects) * MM_PER_PT)


def _figure_groups(names: list[str], rects: dict[str, list[float]]) -> list[list[list[str]]]:
    """잇달아 적힌 그림들을 교재에 놓인 모습대로 묶는다: [묶음 → 줄 → 왼쪽부터의 그림 이름].

    세로로 겹치는(같은 높이에 놓인) 그림들이 한 줄이다. 바로 아래 줄이 윗줄과 칸의 왼쪽 끝을 맞춰 놓여 있고 그림 크기가
    비슷하면 같은 묶음의 다음 줄이다(그림으로 된 선택지 ①②③ / ④⑤). 혼자 놓인 그림은 그것만으로 한 묶음이다.
    """
    rows: list[list[str]] = []
    for name in names:
        x0, y0, x1, y1 = rects[name]
        if rows:
            top, bottom = min(rects[n][1] for n in rows[-1]), max(rects[n][3] for n in rows[-1])
            shared = min(y1, bottom) - max(y0, top)
            apart = all(x0 >= rects[n][2] - 2 or x1 <= rects[n][0] + 2 for n in rows[-1])
            if apart and shared >= 0.6 * min(y1 - y0, bottom - top):
                rows[-1].append(name)
                continue
        rows.append([name])
    rows = [sorted(r, key=lambda n: rects[n][0]) for r in rows]

    def width(n: str) -> float:
        return rects[n][2] - rects[n][0]

    def height(n: str) -> float:
        return rects[n][3] - rects[n][1]

    groups: list[list[list[str]]] = []
    for row in rows:
        if groups:
            last = groups[-1][-1]
            everyone = [n for r in groups[-1] for n in r] + row
            ws, hs = sorted(width(n) for n in everyone), sorted(height(n) for n in everyone)
            alike = ws[-1] <= 1.35 * ws[0] and hs[-1] <= 1.35 * hs[0]
            gap = min(rects[n][1] for n in row) - max(rects[n][3] for n in last)
            short, long = sorted((row, last), key=len)
            lined = all(any(abs(rects[n][0] - rects[m][0]) <= 0.25 * ws[len(ws) // 2] for m in long) for n in short)
            if max(len(row), len(last)) >= 2 and alike and lined and -2 <= gap <= hs[len(hs) // 2]:
                groups[-1].append(row)
                continue
        groups.append([row])
    return groups


def _side_by_side(blocks: list, rects: dict[str, list[float]], make) -> list:
    """잇달아 적힌 그림 가운데 교재에서 나란히 놓여 있던 것들을 make(줄들)이 돌려준 블록 하나로 바꾼다(None이면 그대로 둔다)."""
    out: list = []
    run: list[str] = []

    def flush() -> None:
        for group in _figure_groups(run, rects) if run else []:
            made = make(group) if sum(len(r) for r in group) > 1 else None
            out.extend([made] if made is not None else [transcript.Figure(n) for r in group for n in r])
        run.clear()

    for b in blocks:
        if isinstance(b, transcript.Figure) and b.name in rects:
            run.append(b.name)
            continue
        flush()
        if isinstance(b, transcript.Boxed):
            b = transcript.Boxed(b.label, _side_by_side(b.blocks, rects, make))
        out.append(b)
    flush()
    return out


def _between(idx, page: int, rects: list[list[float]]) -> int:
    """그림들을 한꺼번에 감싸는 네모 안에서, 그림들 밖에 있는 글자 크기의 잉크 덩어리 수(그림 사이에 낀 글이 있는지 본다)."""
    lay, s = idx.layout(page), idx.scale(page)
    x0, y0 = min(r[0] for r in rects) * s, min(r[1] for r in rects) * s
    x1, y1 = max(r[2] for r in rects) * s, max(r[3] for r in rects) * s
    pad = 0.3 * lay.em
    count = 0
    for b in lay.blobs:
        cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
        if not (x0 <= cx <= x1 and y0 <= cy <= y1) or not 0.4 * lay.em <= b.h <= 1.6 * lay.em:
            continue
        if not any(r[0] * s - pad <= cx <= r[2] * s + pad and r[1] * s - pad <= cy <= r[3] * s + pad for r in rects):
            count += 1
    return count


def _merge_figures(blocks: list, p: dict, i: int, folder: Path, pictures: dict, books: dict) -> tuple[list, bool]:
    """교재에서 나란히(또는 격자로) 놓여 있던 그림들을 한 장으로 다시 오려 넣는다. (바꾼 블록들, 묶은 것이 있는가)

    그림은 문단 하나에 하나씩 들어가 세로로 쌓인다. 그래서 좌우로 나란한 그림이나 그림으로 된 선택지(①②③ / ④⑤)는
    따로 넣으면 교재의 배치가 사라진다. 그런 그림들을 한꺼번에 감싸는 범위를 교재에서 한 장으로 오리면 놓인 모습과
    그림에 붙은 번호가 그대로 남는다(낱개로 표에 나눠 넣는 방식도 해 봤다. 모양은 비슷하지만 그렇게 만든 문서에서 문제를
    다시 고를 때 "표가 있는 문제"가 되어 그대로 꺼내지 못했다). 그림 사이에 글이 끼어 있으면 묶지 않는다.
    """
    from . import render
    from .hwp import writer
    rects = {f["name"]: f["rect"] for f in p["figures"] if f.get("rect")}
    if len(rects) < 2 or not p.get("pdf_path") or not p.get("page"):
        return blocks, False
    done = []

    def merged(group: list[list[str]]):
        names = [n for row in group for n in row]
        members = [rects[n] for n in names]
        union = [min(r[0] for r in members), min(r[1] for r in members), max(r[2] for r in members), max(r[3] for r in members)]
        try:
            idx = _index(p, books)
            if _between(idx, p["page"] - 1, members) > 6:
                return None
            path = folder / f"fig-{'+'.join(names)}.png"
            render.imwrite(path, _scan_clip(idx, p["page"] - 1, union))
        except (OSError, ValueError, KeyError, RuntimeError):
            return None                               # 교재를 열 수 없으면 따로따로 넣는다
        w_mm, h_mm = (union[2] - union[0]) * MM_PER_PT, (union[3] - union[1]) * MM_PER_PT
        shrink = min(1.0, MAX_FIGURE_MM[0] / w_mm, MAX_FIGURE_MM[1] / h_mm)
        pictures[(i, "+".join(names))] = writer.Picture(path, round(w_mm * shrink, 1), round(h_mm * shrink, 1))
        done.append(names)
        return transcript.Figure("+".join(names))

    return _side_by_side(blocks, rects, merged), bool(done)


def _figure_names(blocks: list) -> set[str]:
    out: set[str] = set()
    for b in blocks:
        if isinstance(b, transcript.Figure):
            out.add(b.name)
        elif isinstance(b, transcript.Boxed):
            out |= _figure_names(b.blocks)
    return out


def _from_reading(folder: Path, blocks: list) -> list:
    """저장 형식(reading.json)에서 되살린 블록들. 글에서 만든 것과 같을 때만 쓰고, 다르면 글에서 만든 것을 쓴다
    (상자 안의 선택지처럼 저장 형식에 자리가 없는 것이 든 문제)."""
    from . import reading
    try:
        doc = json.loads((folder / "reading.json").read_text(encoding="utf-8"))
        extras = json.loads((folder / "extras.json").read_text(encoding="utf-8")) if (folder / "extras.json").is_file() else {}
        back, errors = reading.blocks_of(doc["questions"][0]["content"], extras)
    except (OSError, ValueError, KeyError, IndexError):
        return blocks
    return back if not errors and reading.flat(back) == reading.flat(blocks) else blocks


class NoReader(Exception):
    """그림을 읽어야 하는 문제가 있다. 서브에이전트에게 맡기는 길(prepare → check → build)로 가야 한다."""


def make(workspace: str | Path, task_id: str, template: str | Path | None = None, name: str | None = None,
         title: str | None = None, wait: float | None = None, preview: bool = False, again: bool = False) -> dict:
    """고른 문제의 글이 모두 준비되어 있으면 한글 파일을 바로 만든다.

    문제의 글은 store에서 가져온다. 한글 문서에서 고른 문제는 문서에 든 글·수식·그림을 그대로 꺼내 두므로
    여기서 바로 만들 수 있다. 그림을 읽어야 하는 문제(교재의 문제)가 하나라도 있으면 NoReader를 올린다:
    엔진은 모델을 부르지 않고, 옮겨 적는 일은 에이전트의 가벼운 서브에이전트가 맡는다.
    again이면 검사를 통과하지 못한 채 저장된 문제도 다시 읽혀야 하는 것으로 친다. wait는 쓰지 않는다(예전 인자).
    """
    from . import store
    from .hwp import writer

    t0 = time.time()
    ws = Path(workspace).resolve()
    task = tasks.load(ws, task_id)
    items = task["items"]
    if not items:
        raise ValueError("태스크에 담긴 문제가 없습니다")
    tpl = find_template(ws, template)
    folders = [store.folder(ws, it) for it in items]
    lifted = lift_documents(ws, items)
    if any(store.load(f) is None or (again and not store.ready(f)) for f in folders):
        raise NoReader("그림을 읽어야 하는 문제가 있습니다")
    session = writer.Session().__enter__()
    try:
        run = run_dir(ws, task_id)
        if run.exists():
            shutil.rmtree(run)
        problems = []
        redo: dict[str, object] = {}
        for n, (it, src) in enumerate(zip(items, folders), 1):
            dst = run / "problems" / f"{n:02d}"
            dst.mkdir(parents=True)
            if not (src / "clean.png").is_file():
                raise FileNotFoundError(f"{n}번째 문제의 그림이 없습니다")
            shutil.copyfile(src / "clean.png", dst / "clean.png")
            st = store.load(src)
            stale = st is not None and st.get("local") != store.LOCAL and st.get("answer", "").strip()
            if stale and Path(it["pdf_path"]).is_file():
                # 예전 방식으로 다듬어 둔 결과다. 모델을 다시 부르지 않고, 받아 둔 답에서 그림 자르기만 다시 한다
                st = store.refresh(src, it, _index(it, redo))
            entry = {"n": n, "pid": it["pid"], "from": f"{it['pdf_name']} {it['page']}쪽 {it['label']}",
                     "folder": f"problems/{n:02d}", "figures": [], "store": src.name,
                     "fresh": False, "notes": [],
                     # 글로 옮기지 못해 통째로 붙일 때 스캔 해상도로 다시 자르는 데 쓴다(_whole_scan)
                     "pdf_path": it["pdf_path"], "pdf_id": it["pdf_id"], "page": it["page"], "bbox": it["bbox"], "stem": it.get("stem")}
            if st is None:
                entry["notes"].append("준비해 둔 글을 읽지 못했습니다")
            else:
                if "reading" not in st and st["status"] == "ok":
                    try:
                        st = store.ensure_reading(src, it, _index(it, redo))
                    except (OSError, ValueError):
                        pass                          # 교재가 제자리에 없으면 저장 형식 없이 글로 만든다
                (dst / "text.md").write_text(st["text"], encoding="utf-8")
                if st.get("reading"):
                    (dst / "reading.json").write_text(json.dumps(st["reading"], ensure_ascii=False, indent=1), encoding="utf-8")
                    if st.get("extras"):
                        (dst / "extras.json").write_text(json.dumps(st["extras"], ensure_ascii=False), encoding="utf-8")
                for f in st["figures"]:
                    shutil.copyfile(src / f["file"], dst / f["file"])
                entry["figures"] = st["figures"]
                entry["seconds"] = st.get("seconds")
                entry["notes"] += st.get("warnings", [])
                if st["status"] != "ok":
                    entry["notes"] += st["errors"]
                if st.get("fixes"):
                    entry["notes"].append("모델이 잘못 읽은 수를 로컬 글자 인식과 대조해 바로잡았습니다(" + ", ".join(st["fixes"]) + ")")
                if st.get("tries", 1) > 1:
                    entry["notes"].append("검사에 걸려 한 번 다시 옮겨 적었습니다")
            problems.append(entry)
        state = {"task": task_id, "name": task.get("name", task_id), "prepared": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "problems": problems, "batches": []}
        (run / "run.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        report = check(ws, task_id, compare_ocr=False)
        result = build(ws, task_id, tpl, name, title, session=session, preview=preview)
    finally:
        session.__exit__(None, None, None)
    result.update({"report": report, "problems": problems, "total_seconds": round(time.time() - t0, 1),
                   "read_seconds": 0.0, "already": len(items), "fresh": 0, "by_server": 0, "lifted": lifted,
                   "requests": 0})
    (run / "build.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result
