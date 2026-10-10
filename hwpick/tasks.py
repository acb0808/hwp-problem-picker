"""고른 문제 목록(태스크)을 작업 폴더에 저장하고 읽는다.

태스크에는 문제마다 PDF 경로·쪽·좌표가 통째로 들어 있어, 나중에 검출 방식이 바뀌어도
사용자가 고른 영역이 달라지지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

DATA_DIR = "hwpick_data"
DEFAULT_PORT = 8765        # 선택 화면 서버의 기본 포트


def data_dir(workspace: str | Path) -> Path:
    d = Path(workspace) / DATA_DIR
    (d / "tasks").mkdir(parents=True, exist_ok=True)
    return d


def _path(workspace: str | Path, task_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", task_id):
        raise ValueError(f"태스크 ID 형식이 아닙니다: {task_id}")
    return data_dir(workspace) / "tasks" / f"{task_id}.json"


def new_id(workspace: str | Path) -> str:
    day = time.strftime("%m%d")
    n = 1
    while _path(workspace, f"T{day}-{n}").exists():
        n += 1
    return f"T{day}-{n}"


def load(workspace: str | Path, task_id: str) -> dict:
    f = _path(workspace, task_id)
    if not f.exists():
        raise FileNotFoundError(f"태스크가 없습니다: {task_id} ({f})")
    return json.loads(f.read_text(encoding="utf-8"))


def save(workspace: str | Path, task: dict) -> dict:
    task["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    task.setdefault("created", task["updated"])
    f = _path(workspace, task["id"])
    tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")
    # 윈도우에서는 다른 쪽이 파일을 읽는 순간에 바꿔치기가 거절된다(선택 화면이 상태를 자주 물어본다). 잠깐 뒤 다시 한다
    for attempt in range(8):
        try:
            os.replace(tmp, f)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.03 * (attempt + 1))
    return task


def create(workspace: str | Path, name: str = "") -> dict:
    tid = new_id(workspace)
    return save(workspace, {"id": tid, "name": name or tid, "items": []})


def delete(workspace: str | Path, task_id: str) -> None:
    _path(workspace, task_id).unlink(missing_ok=True)


def list_all(workspace: str | Path) -> list[dict]:
    out = []
    for f in sorted((data_dir(workspace) / "tasks").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": t["id"], "name": t.get("name", t["id"]), "count": len(t.get("items", [])),
                        "updated": t.get("updated", "")})
        except (ValueError, KeyError):
            continue
    return out
