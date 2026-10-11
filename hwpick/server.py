"""문제 선택 화면을 띄우는 로컬 웹 서버. 표준 라이브러리만 쓰고 이 PC(127.0.0.1)에서만 접속된다.

느린 일(번호 인식, 번호 양식 학습)은 작업 스레드 하나가 한 쪽씩 처리한다. 쪽 그림을 보내는 일은
그와 따로 돌아서, 인식이 밀려 있어도 화면은 바로 뜬다. 작업 순서는 항상 "지금 화면에 보이는 것"이 먼저다.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from . import aim, asks
from . import outline as outlines
from . import render, store, tasks
from .hwp import source
from .index import PDF_LOCK, PdfIndex, cache_root, cached_pages

DEFAULT_PORT = tasks.DEFAULT_PORT
IDLE_EXIT_HOURS = 8
STAMP = tasks.code_stamp()   # 이 서버가 뜰 때의 코드. 스킬을 업데이트한 뒤에도 옛 서버가 남아 있으면 명령이 이것을 보고 새로 띄운다
READ_WORKERS = 4         # 한꺼번에 꺼내는 한글 문서의 문제 수
WEB_DIR = Path(__file__).parent / "web"
MAX_WAITING = 6          # 화면이 기다리는 쪽은 이만큼만 기억한다(빨리 넘기며 지나친 쪽은 버린다)
WARM_AHEAD = 8           # 보고 있는 쪽 다음 몇 쪽을 미리 읽어 둘지
BOX_WAIT_SEC = 120
GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class Cancelled(Exception):
    """기다리던 쪽이 더 새로운 요청에 밀려났다."""


class App:
    def __init__(self, workspace: str | Path, libraries: list[str] | None = None):
        self.workspace = Path(workspace).resolve()
        self.data = tasks.data_dir(self.workspace)
        self.cv = threading.Condition()         # 아래 상태들을 지킨다
        self.index_lock = threading.Lock()
        self.indexes: dict[str, PdfIndex] = {}
        self.pdfs: dict[str, dict] = {}
        self.seen_files: dict[tuple[str, int, float], str] = {}
        self.learn: dict[str, dict] = {}        # 교재별 학습 상태
        self.box_cache: dict[tuple[str, int], dict] = {}
        self.waiting: list[tuple[str, int]] = []    # 화면이 기다리는 쪽(뒤가 최신)
        self.warm: list[tuple[str, int]] = []       # 미리 읽어 둘 쪽
        self.focus: str | None = None           # 지금 보고 있는 교재
        self.last_request = time.time()
        self.read_lock = threading.Lock()       # 아래 두 가지를 지킨다
        self.reading: set[str] = set()          # 지금 꺼내고 있는 문제(store의 열쇠)
        self.read_failed: dict[str, str] = {}   # 꺼내지 못한 문제와 그 까닭
        self.read_pool = ThreadPoolExecutor(max_workers=READ_WORKERS, thread_name_prefix="read")
        self.outline_locks: dict[str, threading.Lock] = {}
        cfg = self._config()
        for lib in libraries or []:
            p = str(Path(lib).resolve())
            if p not in cfg["libraries"]:
                cfg["libraries"].append(p)
        self._save_config(cfg)
        self.scan()
        threading.Thread(target=self._work_loop, daemon=True).start()

    # ---- 설정과 교재 목록

    def _config(self) -> dict:
        f = self.data / "config.json"
        cfg = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        cfg.setdefault("libraries", [])
        return cfg

    def _save_config(self, cfg: dict) -> None:
        (self.data / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")

    def folders(self) -> list[str]:
        return [str(self.workspace)] + [p for p in self._config()["libraries"] if p != str(self.workspace)]

    def add_folder(self, path: str) -> None:
        p = Path(path.strip().strip('"')).expanduser()
        if not p.is_dir():
            raise ValueError(f"폴더를 찾을 수 없습니다: {path}")
        cfg = self._config()
        if str(p.resolve()) not in cfg["libraries"]:
            cfg["libraries"].append(str(p.resolve()))
            self._save_config(cfg)
        self.scan()

    def scan(self) -> None:
        """작업 폴더와 추가한 폴더에서 PDF와 한글 문서를 찾는다(두 단계 아래까지).

        한글 문서는 열어 보기 전에는 쪽 수를 모른다(한글 프로그램으로 바꿔 봐야 안다). 그때까지는 0쪽으로 둔다.
        """
        found: dict[str, dict] = {}
        for folder in self.folders():
            root = Path(folder)
            if not root.is_dir():
                continue
            files = [f for ext in ("pdf",) + tuple(s[1:] for s in source.SUFFIXES)
                     for f in list(root.glob(f"*.{ext}")) + list(root.glob(f"*/*.{ext}")) + list(root.glob(f"*/*/*.{ext}"))]
            for f in sorted(files):
                document = source.is_document(f)
                if tasks.DATA_DIR in f.parts or (document and (f.stem == "양식" or f.name.startswith("~"))):
                    continue
                try:
                    st = f.stat()
                    key = (str(f), st.st_size, st.st_mtime)
                    pid = self.seen_files.get(key) or render.pdf_id(f)
                    self.seen_files[key] = pid
                    if pid in self.pdfs:
                        found[pid] = self.pdfs[pid]
                        continue
                    if document:
                        found[pid] = {"id": pid, "name": f.name, "path": str(f), "pages": cached_pages(f) or 0, "kind": "hwp"}
                        continue
                    with PDF_LOCK, pymupdf.open(str(f)) as doc:
                        found[pid] = {"id": pid, "name": f.stem, "path": str(f), "pages": doc.page_count, "kind": "pdf"}
                except Exception:
                    continue
        self.pdfs = found

    def index(self, pid: str) -> PdfIndex:
        with self.index_lock:
            if pid in self.indexes:
                return self.indexes[pid]
            if pid not in self.pdfs:
                raise KeyError(f"모르는 교재입니다: {pid}")
            path = self.pdfs[pid]["path"]
        idx = PdfIndex(path)                 # 한글 문서를 처음 열 때는 오래 걸린다. 다른 교재를 막지 않게 잠금 밖에서 한다
        with self.index_lock:
            idx = self.indexes.setdefault(pid, idx)
            if pid in self.pdfs:
                self.pdfs[pid]["pages"] = idx.page_count
            return idx

    # ---- 교재 상태(처음 여는 교재는 번호 양식을 익힌다)

    def _open_document(self, pid: str) -> None:
        """한글 문서를 한글 프로그램으로 바꿔 연다(수십 초 걸릴 수 있어 요청과 따로 돈다)."""
        st = self.learn[pid]
        try:
            idx = self.index(pid)
            with self.cv:
                st.update(state="ready" if idx.has_profile() else "learning", total=len(idx.sample_pages()))
                self.cv.notify_all()
        except Exception as e:
            traceback.print_exc()
            with self.cv:
                st["state"], st["error"] = "error", str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"

    def status(self, pid: str) -> dict:
        with self.cv:
            st = self.learn.get(pid)
            waiting = self.pdfs[pid].get("kind") == "hwp" and pid not in self.indexes and not all(
                f.is_file() for f in source.converted(cache_root() / pid))
            if st is None and waiting:
                st = self.learn[pid] = {"state": "opening", "done": 0, "total": 0, "error": ""}
                threading.Thread(target=self._open_document, args=(pid,), daemon=True).start()
            if st is not None and st["state"] == "error" and self.pdfs[pid].get("kind") == "hwp":
                del self.learn[pid]             # 한글에서 문서를 닫고 다시 누르면 다시 해 볼 수 있게 한다
                return dict(st)
        if st is None:
            idx = self.index(pid)
            fresh = {"state": "ready" if idx.has_profile() else "learning", "done": 0,
                     "total": len(idx.sample_pages()), "error": ""}
            with self.cv:
                st = self.learn.setdefault(pid, fresh)
        with self.cv:
            self.focus = pid
            self.cv.notify_all()
            return dict(st)

    def info(self, pid: str) -> dict:
        idx = self.index(pid)
        return {"id": pid, "name": self.pdfs[pid]["name"], "pages": idx.page_count,
                "sizes": [[round(w, 1), round(h, 1)] for w, h in idx.sizes()]}

    # ---- 쪽 단위

    def page_jpeg(self, pid: str, page: int) -> bytes:
        return self.index(pid).page_file(page).read_bytes()

    def boxes(self, pid: str, page: int) -> dict:
        """쪽의 문항 박스. 아직 읽지 않은 쪽이면 작업 스레드가 끝낼 때까지 기다린다."""
        key = (pid, page)
        end = time.time() + BOX_WAIT_SEC
        with self.cv:
            self.focus = pid
            if key not in self.box_cache:
                if key in self.waiting:
                    self.waiting.remove(key)
                self.waiting.append(key)
                del self.waiting[:-MAX_WAITING]
                self.cv.notify_all()
            while key not in self.box_cache:
                if key not in self.waiting:
                    raise Cancelled("더 새로운 요청이 있어 건너뛰었습니다")
                left = end - time.time()
                if left <= 0:
                    raise TimeoutError("문제를 찾는 데 너무 오래 걸립니다")
                self.cv.wait(left)
            total = self.pdfs[pid]["pages"]
            self.warm = [(pid, p) for p in range(page + 1, min(page + 1 + WARM_AHEAD, total))
                         if (pid, p) not in self.box_cache]
            self.cv.notify_all()
            res = self.box_cache[key]
            if res.get("error"):
                del self.box_cache[key]        # 실패한 결과는 붙들어 두지 않는다(화면에서 다시 시도할 수 있게)
            return res

    def crop_jpeg(self, pid: str, page: int, rects: list[list[float]], max_w: int = 640) -> bytes:
        """쪽 이미지에서 영역들을 잘라 세로로 이어 붙인 미리보기."""
        idx = self.index(pid)
        img = render.imread(idx.page_file(page))
        s = idx.scale(page)
        parts = []
        for x0, y0, x1, y1 in rects:
            a, b = max(0, int(y0 * s)), min(img.shape[0], int(y1 * s))
            c, d = max(0, int(x0 * s)), min(img.shape[1], int(x1 * s))
            if b > a and d > c:
                parts.append(img[a:b, c:d])
        if not parts:
            parts = [np.full((20, 20, 3), 255, np.uint8)]
        w = max(p.shape[1] for p in parts)
        parts = [cv2.copyMakeBorder(p, 0, 0, 0, w - p.shape[1], cv2.BORDER_CONSTANT, value=(255, 255, 255))
                 for p in parts]
        out = np.vstack(parts)
        if out.shape[1] > max_w:
            out = cv2.resize(out, (max_w, int(out.shape[0] * max_w / out.shape[1])), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes()

    # ---- 미리 꺼내 두기: 한글 문서에서 담은 문제는 담는 순간 뒤에서 문서의 글·수식·그림을 꺼내 둔다.
    #      교재(PDF)의 문제는 여기서 옮겨 적지 않는다. 그림을 읽는 일은 에이전트의 가벼운 서브에이전트가 맡는다(make → finish)

    def prefetch(self, task: dict) -> None:
        for item in task.get("items", []):
            if not source.is_document(item["pdf_path"]):
                continue
            folder = store.folder(self.workspace, item)
            with self.read_lock:
                if folder.name in self.reading or folder.name in self.read_failed or store.load(folder) is not None:
                    continue
                self.reading.add(folder.name)
            self.read_pool.submit(self._read_one, item, folder)

    def read_states(self, task: dict) -> dict:
        """담은 문제마다 글을 준비해 둔 상태: ok(준비됨), working(꺼내는 중), error(꺼내지 못함), none.
        available은 늘 거짓이다(화면이 '미리 옮겨 적는 중' 표시를 하지 않게 한다. 교재의 문제는 미리 옮겨 적지 않는다)."""
        states = {}
        for item in task.get("items", []):
            folder = store.folder(self.workspace, item)
            st = store.load(folder)
            with self.read_lock:
                if folder.name in self.reading:
                    states[item["pid"]] = "working"
                elif st is not None:
                    states[item["pid"]] = "ok" if st["status"] == "ok" else "error"
                else:
                    states[item["pid"]] = "error" if folder.name in self.read_failed else "none"
        return {"available": False, "states": states}

    def _read_one(self, item: dict, folder: Path) -> None:
        try:
            idx = self.index(item["pdf_id"]) if item["pdf_id"] in self.pdfs else PdfIndex(item["pdf_path"], item["pdf_id"])
            if not item.get("manual"):
                store.lift(folder, item, idx)
        except Exception as e:
            traceback.print_exc()
            with self.read_lock:
                self.read_failed[folder.name] = f"{type(e).__name__}: {e}"
        finally:
            with self.read_lock:
                self.reading.discard(folder.name)

    # ---- 말로 범위를 정해 문제 모으기: 교재의 뼈대(단원·구획의 쪽 범위)와, 그 범위의 박스로 목록 만들기

    def _paper(self, pid: str, strict: bool = True) -> asks.PaperReader:
        """쪽 그림을 읽는 물음의 담당: 물음을 서브에이전트에게 맡기고 그 답을 쓴다(엔진은 모델을 직접 부르지 않는다)."""
        return asks.PaperReader(tasks.data_dir(self.workspace) / "outline" / pid, strict)

    def outline(self, pid: str, refresh: bool = False, wait: float = 0.0) -> dict:
        idx = self.index(pid)
        if idx.document:
            # 한글 문서: 구획이 따로 없고 문제 번호가 곧 차례다. 문서에서 직접 읽으므로 모델을 부를 일이 없다
            lines = []
            seen: list[str] = []
            total = direct = 0
            for p in range(idx.page_count):
                boxes = idx.boxes(p)
                labels = [b["label"] for b in boxes]
                seen += labels
                total += len(boxes)
                direct += sum(1 for b in boxes if not idx.slot(b["id"]).issues)
                if not labels:
                    continue
                run = all(v.isdigit() for v in labels) and [int(v) for v in labels] == list(range(int(labels[0]), int(labels[0]) + len(labels)))
                shown = (f"{labels[0]}번~{labels[-1]}번" if run and len(labels) > 1 else ", ".join(labels))
                lines.append(f"{p + 1}쪽: {shown} ({len(labels)}문제)")
            notes = idx.slot_notes()
            if len(set(seen)) < len(seen) and not any("같은 번호" in n for n in notes):
                notes = notes + ["같은 번호의 문제가 여러 개 있습니다(여러 회차를 묶었거나 선택 과목이 나뉜 문서). 번호로 고를 때는 "
                                 "--pages 로 쪽 범위를 같이 주세요"]
            return {"name": self.pdfs[pid]["name"], "pages": idx.page_count, "lines": lines, "units": True, "seconds": 0,
                    "cost": 0, "at": "", "document": True, "problems": total, "direct": direct, "notes": notes}
        with self.index_lock:
            lock = self.outline_locks.setdefault(pid, threading.Lock())
        with lock:                                   # 같은 교재를 두 번 만들지 않는다
            if refresh:
                outlines._file(idx).unlink(missing_ok=True)
                self._paper(pid).forget()
            data = outlines.load(idx)
            if data is None and not asks.enabled(self.workspace):
                raise ValueError("이 교재의 뼈대(단원과 구획이 몇 쪽인지)가 아직 없고, 설정에서 뼈대 읽기를 꺼 두어 단원 이름으로는 범위를 정할 수 없습니다. "
                                 "쪽 범위나 문제 번호를 알면 pick --pages 12-19 (또는 --first 0040 --last 0096)로 바로 모을 수 있고, "
                                 "선택 화면(open)에서 직접 고를 수도 있습니다")
            elif data is None:
                # 쪽 그림을 읽는 물음을 서브에이전트에게 맡기고, 답이 오면 이어서 한다
                paper = self._paper(pid)
                left = paper.wait(wait)
                asked = sum(1 for j in paper.state["jobs"] if j["round"] == paper.state["round"])
                # 답이 하나도 안 왔으면 한두 번 더 기다리게 한다. 일부만 안 온 것은 그것만 다시 맡긴다(아래에서 다시 묻게 된다)
                if left and len(left) == asked and paper.waited() <= 2:
                    return {"name": self.pdfs[pid]["name"], "pages": idx.page_count, "pending": "waiting",
                            "jobs": [j["file"] for j in left], "round": paper.state["round"]}
                try:
                    data = asks.light(idx, paper)
                except asks.NoOutline as e:
                    raise ValueError(f"이 교재의 뼈대를 만들지 못했습니다: {e}. 쪽 범위나 문제 번호를 알면 pick --pages 12-19 "
                                     "(또는 --first 0040 --last 0096)로 바로 모을 수 있고, 선택 화면(open)에서 직접 고를 수도 있습니다")
                except asks.Pending:
                    questions = len(paper.asked)
                    return {"name": self.pdfs[pid]["name"], "pages": idx.page_count, "pending": "ask",
                            "jobs": paper.jobs(), "questions": questions, "round": paper.state["round"]}
        return {"name": self.pdfs[pid]["name"], "pages": idx.page_count, "lines": outlines.describe(idx, data),
                "units": bool(data.get("units")), "seconds": data["seconds"], "cost": data["cost"], "at": data["at"],
                "light": bool(data.get("light"))}

    def pick(self, body: dict) -> dict:
        """구획들(또는 쪽 범위)의 문제 박스를 모아 목록(태스크)을 만든다. 모델은 구획의 경계를 확인할 때만 쓴다."""
        pid = body["pdf"]
        seen = self.focus                            # 사용자가 화면에서 보고 있던 교재(이 명령 때문에 바뀌면 안 된다)
        try:
            return self._pick(body, pid)
        finally:
            with self.cv:
                self.focus = seen

    def _pick(self, body: dict, pid: str) -> dict:
        idx = self.index(pid)
        sections = body.get("sections") or []
        numbers = [str(n) for n in body.get("numbers") or []]
        if idx.document and not sections and not body.get("pages"):
            body = dict(body, pages=[1, idx.page_count])       # 만든 문서는 범위를 주지 않으면 문서 전체에서 고른다
        data = outlines.load(idx)
        if sections and data is None:
            raise ValueError("이 교재의 뼈대가 아직 없습니다. 먼저 outline 명령을 실행하세요")
        end = time.time() + 600                      # 처음 보는 교재는 번호 양식부터 익혀야 한다(1~2분)
        while self.status(pid)["state"] != "ready":
            if self.status(pid)["state"] == "error" or time.time() > end:
                raise ValueError("이 교재의 문제 번호 양식을 익히지 못했습니다: " + self.status(pid).get("error", "시간 초과"))
            time.sleep(1.0)

        def get_boxes(page: int) -> list[dict]:
            for _ in range(3):
                try:
                    return self.boxes(pid, page)["boxes"]
                except Cancelled:
                    time.sleep(0.2)
            return self.boxes(pid, page)["boxes"]

        # 구획의 첫 문제 번호는 뼈대를 만들 때 받아 둔 답에서 꺼낸다(새로 묻지 않는다)
        reader = self._paper(pid, strict=False) if sections else None
        pages = tuple(body["pages"]) if body.get("pages") else None
        got = outlines.collect(idx, data, reader, get_boxes, sections, pages, body.get("first"), body.get("last"))
        if numbers:
            # 번호를 콕 집어 준 경우: 준 차례대로 그 문제들만 남긴다
            by: dict[str, list] = {}
            for p, b in got["boxes"]:
                by.setdefault(outlines._label_key(b["label"]), []).append((p, b))
            chosen, absent, twice = [], [], []
            many = {k: len(v) for k, v in by.items() if len(v) > 1}
            for n in numbers:
                hits = by.get(outlines._label_key(n))
                if hits:
                    if many.get(outlines._label_key(n)) == len(hits):
                        twice.append(f"{n}번이 {len(hits)}개 있어 앞의 것({hits[0][0]}쪽)을 골랐습니다")
                    chosen.append(hits.pop(0))
                else:
                    absent.append(n)
            got["boxes"] = chosen
            got["spans"] = [f"번호로 고름: {', '.join(b['label'] for _, b in chosen)}"] if chosen else []
            got["notes"] = ([f"범위 안에서 찾지 못한 번호: {', '.join(absent)}"] if absent else []) \
                + ([", ".join(twice) + ". 다른 것을 원하면 --pages 로 쪽 범위를 좁혀 주세요"] if twice else [])
        got["notes"] = list(got["notes"]) + idx.slot_notes()
        pdf = self.pdfs[pid]
        items = [{"pid": b["id"], "pdf_id": pid, "pdf_name": pdf["name"], "pdf_path": pdf["path"], "page": p,
                  "label": b["label"], "bbox": b["bbox"], "stem": b.get("stem"), "manual": False}
                 for p, b in got["boxes"]]
        free = sum(1 for it in items if (s := idx.slot(it["pid"])) is not None and not s.issues)
        result = {"count": len(items), "spans": got["spans"], "notes": got["notes"], "task": None,
                  "labels": [it["label"] for it in items], "free": free}
        if items and not body.get("dry"):
            task = tasks.create(self.workspace, body.get("name") or "")
            task["items"] = items
            result["task"] = tasks.save(self.workspace, task)["id"]
        return result

    # ---- 에이전트가 쪽을 보고 직접 영역 잡기: 박스를 못 찾았거나 잘못 잡은 문제를 사람에게 넘기지 않으려는 것

    def _found(self, pid: str, page: int) -> list[dict]:
        """그 쪽에서 찾아 둔 박스. 처음 보는 교재면 번호 양식을 익힐 때까지 기다리고, 익히지 못한 교재는 박스가 없다.
        화면에 열려 있는 교재 표시는 건드리지 않는다."""
        seen = self.focus
        try:
            end = time.time() + 600
            while (state := self.status(pid)["state"]) != "ready":
                if state == "error" or time.time() > end:
                    return []
                time.sleep(1.0)
            for _ in range(3):
                try:
                    return self.boxes(pid, page)["boxes"]
                except Cancelled:
                    time.sleep(0.2)
            return self.boxes(pid, page)["boxes"]
        finally:
            with self.cv:
                self.focus = seen

    def look(self, pid: str, page: int, task_id: str | None = None) -> dict:
        """에이전트가 볼 쪽 그림을 만들어 두고, 그 쪽의 박스와 박스 없는 덩어리를 눈금 좌표(0~1000)로 알려 준다."""
        idx = self.index(pid)
        found = self._found(pid, page)
        lay, s = idx.layout(page), idx.scale(page)
        shape = lay.img.shape

        def px(bbox) -> tuple:
            return tuple(int(round(v * s)) for v in bbox)

        items = tasks.load(self.workspace, task_id)["items"] if task_id else []
        where = {it["pid"]: k for k, it in enumerate(items, 1)}
        marks, rows, mine = [], [], []
        for k, b in enumerate(found, 1):
            name = b["label"] if b["label"].isascii() else f"#{k}"
            marks.append((px(b["bbox"]), name, "picked" if b["id"] in where else "found"))
            rows.append({"label": b["label"], "mark": name, "box": aim.to_unit(px(b["bbox"]), shape), "at": where.get(b["id"])})
        for k, it in enumerate(items, 1):
            if it.get("manual") and it["pdf_id"] == pid and it["page"] == page + 1:
                marks.append((px(it["bbox"]), f"T{k}", "agent"))
                mine.append({"label": it["label"], "mark": f"T{k}", "box": aim.to_unit(px(it["bbox"]), shape), "at": k})
        free = [(r, chr(65 + k), parts) for k, (r, parts) in enumerate(aim.free_regions(lay, [px(b["bbox"]) for b in found]))]
        out = self.data / "look" / f"{pid[:6]}-p{page + 1}.jpg"
        render.imwrite(out, aim.sheet(lay, marks, free), 82)
        return {"file": str(out), "name": self.pdfs[pid]["name"], "page": page + 1, "pages": idx.page_count, "boxes": rows,
                "manual": mine,
                "free": [{"name": n, "box": aim.to_unit(r, shape),
                          "parts": [{"name": f"{n}{k}", "box": aim.to_unit(p, shape)} for k, p in enumerate(parts, 1)]
                          if len(parts) > 1 else []} for r, n, parts in free]}

    @staticmethod
    def _order(items: list[dict], new: dict) -> int:
        """새 문제를 목록의 어디에 넣을지: 같은 교재의 문제 번호 차례, 번호가 없으면 쪽과 자리의 차례."""
        same = [(k, it) for k, it in enumerate(items) if it["pdf_id"] == new["pdf_id"]]
        nums = [(k, int(it["label"])) for k, it in same if it["label"].isdigit()]
        if new["label"].isdigit() and nums:
            n = int(new["label"])
            below = [k for k, v in nums if v < n]
            return below[-1] + 1 if below else nums[0][0]

        def before(a: dict, b: dict) -> bool:
            if a["page"] != b["page"]:
                return a["page"] < b["page"]
            ax, bx = (a["bbox"][0] + a["bbox"][2]) / 2, (b["bbox"][0] + b["bbox"][2]) / 2
            if ax < b["bbox"][0] or bx < a["bbox"][0]:
                return ax < bx                       # 다른 단: 왼쪽 단이 먼저
            return a["bbox"][1] < b["bbox"][1]

        earlier = [k for k, it in same if before(it, new)]
        return earlier[-1] + 1 if earlier else (same[0][0] if same else len(items))

    def add(self, body: dict) -> dict:
        """에이전트가 준 범위(0~1000 눈금, 또는 look이 알려 준 덩어리의 글자)를 잉크에 맞춰 문제로 담는다.

        틀린 박스가 빠진 박스보다 나쁘다. 그래서 이미 있는 박스와 많이 겹치거나, 박스에서 다른 문제의 번호가 읽히면
        담지 않고 까닭을 알린다(force로만 넘어간다).
        """
        pid, page = body["pdf"], int(body["page"]) - 1
        idx = self.index(pid)
        if not 0 <= page < idx.page_count:
            raise ValueError(f"쪽 번호가 교재를 벗어납니다: {page + 1} (전체 {idx.page_count}쪽)")
        found = self._found(pid, page)
        lay, s = idx.layout(page), idx.scale(page)
        shape = lay.img.shape
        em = lay.em

        def px(bbox) -> tuple:
            return tuple(int(round(v * s)) for v in bbox)

        task = tasks.load(self.workspace, body["task"]) if body.get("task") else None
        items = list(task["items"]) if task else []
        replace, after, force = body.get("replace"), body.get("after"), bool(body.get("force"))
        for what, v in (("--replace", replace), ("--after", after)):
            if v is not None and not (what == "--after" and v == 0) and not 1 <= v <= len(items):
                raise ValueError(f"{what} {v}: 목록에 그런 차례가 없습니다(문제 {len(items)}개)")
        gone = items[replace - 1]["pid"] if replace else None
        others = [(px(b["bbox"]), f"찾아 둔 {b['label']}번 박스") for b in found if b["id"] != gone] \
            + [(px(it["bbox"]), f"목록의 {k}번째(직접 잡은 영역)") for k, it in enumerate(items, 1)
               if it.get("manual") and it["pdf_id"] == pid and it["page"] == page + 1 and it["pid"] != gone]
        free: dict[str, tuple] = {}
        for k, (r, parts) in enumerate(aim.free_regions(lay, [px(b["bbox"]) for b in found])):
            free[chr(65 + k)] = r
            free.update({f"{chr(65 + k)}{n}": p for n, p in enumerate(parts, 1)})
        pdf = self.pdfs[pid]
        made, refused = [], []
        for spec in body.get("boxes") or []:
            label = str(spec.get("label") or "").strip()
            said = spec.get("regions") or spec.get("box")
            try:
                if spec.get("regions"):
                    parts = [free[n.upper()] for n in spec["regions"]]
                    rect = (min(r[0] for r in parts), min(r[1] for r in parts), max(r[2] for r in parts), max(r[3] for r in parts))
                else:
                    rect = aim.to_px(spec["box"], shape)
            except KeyError as e:
                refused.append({"said": said, "label": label, "why": f"이 쪽에 그런 덩어리가 없습니다: {e.args[0]}"})
                continue
            got, notes, clash = aim.snap(lay, rect, others, force)
            why = ""
            if clash and not force:
                why = f"{', '.join(clash)}와 겹칩니다. 좌표를 고치거나, 그 박스 대신 이 영역을 쓰려면 --force(목록에 든 것이면 --replace 차례)"
            elif got is None:
                why = "그 범위에 (이미 박스가 있는 문제의 것 말고는) 글이나 그림이 없습니다"
            confirmed = None
            if not why and label.isdigit():
                n = int(label)

                def near(text: str) -> bool:
                    return text.isdigit() and len(text) == len(label) and 1 <= abs(int(text) - n) <= 3

                inside = [] if idx.document else [
                    t.text.strip() for t in idx.page_tokens(page).tokens
                    if near(t.text.strip()) and got[0] <= (t.x0 + t.x1) / 2 <= got[2] and got[1] <= (t.y0 + t.y1) / 2 <= got[3]
                    and t.x0 - got[0] < 3 * em]
                head = aim.head_digits(lay, got)
                confirmed = any(d.isdigit() and int(d) == n for d in head)
                wrong = sorted(set(inside) | {d for d in head if near(d)})
                if wrong and not force:
                    why = (f"박스 안에서 다른 문제 번호({', '.join(wrong)})가 읽혔습니다. {label}번 하나만 들어가게 좌표를 고치세요"
                           "(잘못 읽은 것이 확실하면 --force)")
                elif not confirmed and not force:
                    # 번호가 첫머리가 아니라 아래 조각에서 읽히면, 그 위는 제목이나 개념 설명이 섞인 것이다
                    for k, part in enumerate(aim.slices(lay, got)[1:], 2):
                        if any(d.isdigit() and int(d) == n for d in aim.head_digits(lay, part)):
                            why = (f"문제 번호({label})가 박스의 첫머리가 아니라 위에서 {k}번째 조각에서 읽혔습니다. 그 위는 제목이나 "
                                   "다른 내용으로 보입니다. 번호가 있는 조각부터 담으세요(이대로가 맞으면 --force)")
                            break
            if why:
                refused.append({"said": said, "label": label, "why": why})
                continue
            stem = None
            if spec.get("stem"):
                stem, more, _ = aim.snap(lay, aim.to_px(spec["stem"], shape), force=True)
                if stem is None:
                    refused.append({"said": said, "label": label, "why": "공통 발문의 범위에 글이 없습니다"})
                    continue
                notes += ["공통 발문: " + v for v in more]
            item = {"pid": f"{pid[:6]}-{page + 1}-a{len(items)}{int(time.time() * 1000) % 100000}", "pdf_id": pid,
                    "pdf_name": pdf["name"], "pdf_path": pdf["path"], "page": page + 1, "label": label or "직접",
                    "bbox": [round(v / s, 1) for v in got], "stem": [round(v / s, 1) for v in stem] if stem else None,
                    "manual": True, "by": "agent"}
            if replace is not None and not made:
                at = replace - 1
                items[at] = item
            else:
                if made and (replace is not None or after is not None):
                    at = items.index(made[-1]["item"]) + 1       # 자리를 정해 준 경우, 여러 개는 그 뒤로 차례대로
                else:
                    at = after if after is not None else self._order(items, item)
                items.insert(at, item)
            others.append((got, f"방금 넣은 {item['label']}"))
            made.append({"item": item, "label": item["label"], "box": aim.to_unit(got, shape), "notes": notes,
                         "confirmed": confirmed, "rect": got})
        if made:
            if task is None:
                task = tasks.create(self.workspace, body.get("name") or "")
            task["items"] = items
            task = tasks.save(self.workspace, task)
            self.prefetch(task)
            for m in made:
                it = m.pop("item")
                m["at"] = items.index(it) + 1
                shot = self.data / "look" / f"{task['id']}-{it['pid']}.jpg"
                render.imwrite(shot, aim.closeup(lay, m.pop("rect"), [px(b["bbox"]) for b in found if b["id"] != gone]), 85)
                m["preview"] = str(shot)
        return {"task": task["id"] if task else None, "count": len(items) if task else 0, "added": made, "refused": refused}

    def drop(self, task_id: str, positions: list[int]) -> dict:
        """목록에서 그 차례의 문제들을 뺀다."""
        task = tasks.load(self.workspace, task_id)
        items = task["items"]
        bad = [p for p in positions if not 1 <= p <= len(items)]
        if bad:
            raise ValueError(f"목록에 그런 차례가 없습니다: {bad} (문제 {len(items)}개)")
        gone = [f"{it['label']}({it['page']}쪽)" for k, it in enumerate(items, 1) if k in positions]
        task["items"] = [it for k, it in enumerate(items, 1) if k not in positions]
        tasks.save(self.workspace, task)
        return {"dropped": gone, "count": len(task["items"])}

    # ---- 작업 스레드: 번호 인식과 학습은 여기서만 한다

    def _next_job(self) -> tuple | None:
        """다음에 할 일. 화면이 기다리는 쪽 > 보고 있는 교재의 학습 > 미리 읽기 > 다른 교재의 학습."""
        for key in reversed(self.waiting):
            if self.learn.get(key[0], {}).get("state") == "ready":
                return ("boxes", key)
        if self.focus and self.learn.get(self.focus, {}).get("state") == "learning":
            return ("learn", self.focus)
        self.warm = [k for k in self.warm if k not in self.box_cache]
        if self.warm and self.learn.get(self.warm[0][0], {}).get("state") == "ready":
            return ("boxes", self.warm[0])
        for pid, st in self.learn.items():
            if st["state"] == "learning":
                return ("learn", pid)
        return None

    def _work_loop(self) -> None:
        while True:
            with self.cv:
                job = self._next_job()
                while job is None:
                    self.cv.wait()
                    job = self._next_job()
            try:
                if job[0] == "boxes":
                    self._do_boxes(job[1])
                else:
                    self._do_learn_step(job[1])
            except Exception:
                traceback.print_exc()
                time.sleep(0.5)

    def _do_boxes(self, key: tuple[str, int]) -> None:
        pid, page = key
        try:
            idx = self.index(pid)
            w, h = idx.sizes()[page]
            res = {"page": page + 1, "width": round(w, 1), "height": round(h, 1), "boxes": idx.boxes(page)}
        except Exception as e:
            traceback.print_exc()
            res = {"page": page + 1, "boxes": [], "error": f"{type(e).__name__}: {e}"}
        with self.cv:
            self.box_cache[key] = res
            if key in self.waiting:
                self.waiting.remove(key)
            self.cv.notify_all()

    def _do_learn_step(self, pid: str) -> None:
        """표본 쪽 하나를 읽는다. 다 읽었으면 번호 양식을 정한다."""
        st = self.learn[pid]
        try:
            idx = self.index(pid)
            sample = idx.sample_pages()
            if st["done"] < len(sample):
                idx.page_tokens(sample[st["done"]])
                with self.cv:
                    st["done"] += 1
                return
            idx.profile()
            with self.cv:
                st["state"] = "ready"
                self.cv.notify_all()
        except Exception as e:
            traceback.print_exc()
            with self.cv:
                st["state"], st["error"] = "error", f"{type(e).__name__}: {e}"


class Handler(BaseHTTPRequestHandler):
    app: App
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200, cache: bool = False) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)
        except GONE:
            self.close_connection = True       # 화면이 이미 다른 쪽으로 넘어가 연결을 끊었다

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _handle(self, method: str) -> None:
        app = self.app
        app.last_request = time.time()
        local = ("127.0.0.1", "localhost")
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        origin = self.headers.get("Origin")
        if host not in local or (origin and urllib.parse.urlsplit(origin).hostname not in local):
            return self._json({"error": "이 PC의 선택 화면에서 보낸 요청만 받습니다"}, 403)
        url = urllib.parse.urlsplit(self.path)
        parts = [urllib.parse.unquote(p) for p in url.path.strip("/").split("/")]
        q = dict(urllib.parse.parse_qsl(url.query))
        try:
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send((WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if parts[0] != "api":
                return self._json({"error": "not found"}, 404)
            route = parts[1:]
            if route == ["ping"]:
                return self._json({"ok": True, "app": "hwpick", "workspace": str(app.workspace), "stamp": STAMP})
            if route == ["state"]:
                return self._json({"focus": app.focus})
            if route == ["pick"] and method == "POST":
                return self._json(app.pick(self._body()))
            if route == ["add"] and method == "POST":
                return self._json(app.add(self._body()))
            if route[0] == "tasks" and len(route) == 3 and route[2] == "drop" and method == "POST":
                return self._json(app.drop(route[1], [int(v) for v in self._body().get("positions", [])]))
            if route == ["library"] and method == "GET":
                app.scan()
                return self._json({"folders": app.folders(), "pdfs": sorted(app.pdfs.values(), key=lambda p: p["name"])})
            if route == ["library"] and method == "POST":
                app.add_folder(self._body().get("path", ""))
                return self._json({"ok": True})
            if route[0] == "pdf" and len(route) >= 3:
                pid = route[1]
                if pid not in app.pdfs:
                    return self._json({"error": "모르는 교재입니다"}, 404)
                if route[2] == "status":
                    return self._json(app.status(pid))
                if route[2] == "info":
                    return self._json(app.info(pid))
                if route[2] == "outline":
                    return self._json(app.outline(pid, refresh=q.get("refresh") == "1", wait=float(q.get("wait") or 0)))
                page = int(route[3]) - 1
                if not 0 <= page < app.pdfs[pid]["pages"]:
                    return self._json({"error": "쪽 번호가 범위를 벗어났습니다"}, 404)
                if route[2] == "image":
                    return self._send(app.page_jpeg(pid, page), "image/jpeg", cache=True)
                if route[2] == "look":
                    return self._json(app.look(pid, page, q.get("task")))
                if route[2] == "boxes":
                    if app.status(pid)["state"] != "ready":
                        return self._json({"error": "아직 준비 중입니다"}, 409)
                    return self._json(app.boxes(pid, page))
            if route == ["crop"]:
                rects = [[float(v) for v in r.split(",")] for r in q["rects"].split(";") if r]
                return self._send(app.crop_jpeg(q["pdf"], int(q["page"]) - 1, rects), "image/jpeg", cache=True)
            if route == ["tasks"] and method == "GET":
                return self._json(tasks.list_all(app.workspace))
            if route == ["tasks"] and method == "POST":
                return self._json(tasks.create(app.workspace, self._body().get("name", "")))
            if route[0] == "tasks" and len(route) == 3 and route[2] == "ready" and method == "GET":
                task = tasks.load(app.workspace, route[1])
                app.prefetch(task)
                return self._json(app.read_states(task))
            if route[0] == "tasks" and len(route) == 2:
                if method == "GET":
                    return self._json(tasks.load(app.workspace, route[1]))
                if method == "PUT":
                    task = self._body()
                    task["id"] = route[1]
                    saved = tasks.save(app.workspace, task)
                    app.prefetch(saved)
                    return self._json(saved)
                if method == "DELETE":
                    tasks.delete(app.workspace, route[1])
                    return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)
        except Cancelled as e:
            return self._json({"error": str(e), "cancelled": True}, 409)
        except TimeoutError as e:
            return self._json({"error": str(e)}, 503)
        except (KeyError, FileNotFoundError, ValueError) as e:
            return self._json({"error": str(e)}, 400)
        except GONE:
            self.close_connection = True
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    # 윈도우에서는 이 값이 참이면 남이 쓰고 있는 포트에도 겹쳐 붙는다(요청은 먼저 있던 프로그램이 받는다)
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address) -> None:
        if not isinstance(sys.exc_info()[1], GONE):
            super().handle_error(request, client_address)


def serve(workspace: str | Path, libraries: list[str] | None = None, port: int = DEFAULT_PORT,
          log: str | Path | None = None) -> None:
    if log:
        # 따로 띄운 서버는 출력을 받아 줄 곳이 없다. 스스로 로그 파일에 적는다(파이썬 밖에서 찍는 글까지)
        out = open(log, "a", encoding="utf-8", buffering=1)
        for stream, fd in ((sys.stdout, 1), (sys.stderr, 2)):
            try:
                if stream is not None:
                    stream.flush()
                os.dup2(out.fileno(), fd)
            except (OSError, ValueError):
                pass
        sys.stdout = sys.stderr = out
    app = App(workspace, libraries)
    Handler.app = app
    httpd = Server(("127.0.0.1", port), Handler)
    (app.data / "server.json").write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")

    def idle_watch() -> None:
        while True:
            time.sleep(30)
            if time.time() - app.last_request > IDLE_EXIT_HOURS * 3600:
                httpd.shutdown()
                return

    threading.Thread(target=idle_watch, daemon=True).start()
    print(f"hwp-problem-picker 선택 화면: http://127.0.0.1:{port}/  (작업 폴더: {app.workspace})", flush=True)
    httpd.serve_forever()
