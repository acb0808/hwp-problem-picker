"""문제 그림을 글로 옮겨 적는 모델을 엔진이 직접 부른다.

에이전트의 서브에이전트에게 맡기면 문제 다섯 개에 50초가 걸린다. opencode가 깔려 있으면 그것을 화면 없이
서버로 띄우고(`opencode serve`), 도구가 없는 가벼운 에이전트에게 문제 그림 한 장과 지시문을 보낸다.
문제 하나에 3~8초이고 여러 개를 한꺼번에 보낼 수 있다. 로그인 정보는 opencode가 가진 것을 그대로 쓰므로
이 프로그램은 열쇠를 읽지도 저장하지도 않는다.

가끔 요청 하나가 60초 넘게 늘어진다(서버 쪽 사정). 그래서 몇 초 안에 답이 없으면 같은 요청을 하나 더
보내고 먼저 온 답을 쓴다.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "opencode-go/muse-spark-1.3-contributor"
DEFAULT_VARIANT = "minimal"        # 추론 강도. 옮겨 적기에는 가장 약한 것으로 충분했다(20문제 대조)
AGENT = "hwpick-reader"
HEDGE_AFTER = (4.0, 4.0)           # 첫 요청 뒤 이만큼, 그 뒤 또 이만큼 지나도 답이 없으면 하나씩 더 보낸다
START_TIMEOUT = 25.0
AGENT_PROMPT = "You transcribe scanned math problems. Reply with the requested transcript only. Never use tools."


class ReaderError(Exception):
    """모델을 직접 부를 수 없다(opencode가 없거나, 서버가 뜨지 않거나, 요청이 실패했다)."""


def executable() -> str | None:
    if os.name == "nt":
        # 확장자 없는 'opencode'는 bash용 스크립트라 윈도우가 실행하지 못한다
        return shutil.which("opencode.exe") or shutil.which("opencode.cmd")
    return shutil.which("opencode")


def available(workspace: str | Path | None = None) -> bool:
    """엔진이 모델을 직접 부르도록 켜져 있고, 부를 수 있는가.

    기본은 부르지 않는다. 이 스킬은 opencode가 없는 환경을 기준으로 한다: 그림을 읽는 일은 에이전트의 가벼운
    서브에이전트가 맡는다. opencode가 깔려 있어도 알아서 쓰지 않는다(쓰는 사람의 요금이 나가고, 모델이 없거나
    답이 늦으면 오래 기다린 끝에 실패한다). 작업 폴더 설정에 "direct": true 가 있거나 환경 변수
    HWPICK_DIRECT=1 일 때만 opencode로 직접 부른다. 설정의 "direct": false 와 HWPICK_DIRECT=0 은 끈다.
    """
    env = os.environ.get("HWPICK_DIRECT", "").strip().lower()
    if env in ("0", "off", "false", "no"):
        return False
    wanted = env in ("1", "on", "true", "yes")
    if workspace is not None:
        try:
            from . import tasks
            cfg = json.loads((tasks.data_dir(workspace) / "config.json").read_text(encoding="utf-8"))
            if cfg.get("direct") is False:
                return False
            wanted = wanted or cfg.get("direct") is True
        except (OSError, ValueError):
            pass
    return wanted and executable() is not None


MODELS_TTL = 24 * 3600.0


def _model_known(model: str) -> bool:
    """이 PC의 opencode가 그 모델을 쓸 수 있는가(`opencode models`의 목록에 있는가). 목록은 하루 동안 적어 두고 쓴다."""
    import subprocess
    cache = _home() / "models.json"
    try:
        kept = json.loads(cache.read_text(encoding="utf-8"))
        if time.time() - kept["at"] < MODELS_TTL:
            return model in kept["models"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        out = subprocess.run([executable(), "models"], capture_output=True, timeout=40, stdin=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        models = [line.strip() for line in out.stdout.decode("utf-8", "replace").splitlines() if "/" in line]
    except (OSError, subprocess.SubprocessError):
        return False
    if not models:
        return False
    try:
        cache.write_text(json.dumps({"at": time.time(), "models": models}), encoding="utf-8")
    except OSError:
        pass
    return model in models


def settings(workspace: str | Path | None = None) -> tuple[str, str]:
    """쓸 모델과 추론 강도. 환경 변수(HWPICK_MODEL, HWPICK_VARIANT) > 작업 폴더의 설정 > 기본값."""
    model, variant = os.environ.get("HWPICK_MODEL"), os.environ.get("HWPICK_VARIANT")
    if workspace is not None and not (model and variant):
        try:
            from . import tasks
            cfg = json.loads((tasks.data_dir(workspace) / "config.json").read_text(encoding="utf-8"))
            model, variant = model or cfg.get("model"), variant or cfg.get("variant")
        except (OSError, ValueError):
            pass
    return model or DEFAULT_MODEL, variant or DEFAULT_VARIANT


def _home() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "HWPick" / "reader"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _user_mcp_names() -> list[str]:
    """사용자가 opencode에 붙여 둔 외부 도구 서버의 이름들. 옮겨 적기에는 필요 없어 이 서버에서만 꺼 둔다."""
    f = Path.home() / ".config" / "opencode" / "opencode.json"
    try:
        return list((json.loads(f.read_text(encoding="utf-8")).get("mcp") or {}).keys())
    except (OSError, ValueError, AttributeError):
        return []


def _kill_with_parent(proc: subprocess.Popen) -> object | None:
    """이 프로그램이 죽으면 띄운 서버도 같이 죽게 한다(윈도우의 작업 개체). 실패해도 동작에는 지장이 없다."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rt", "wt", "ot")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.LimitFlags = 0x2000                 # 작업 개체의 마지막 손잡이가 닫히면 안의 프로세스를 끝낸다
        if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        if not k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
            return None
        return job                                     # 들고 있어야 한다(놓으면 바로 닫힌다)
    except Exception:
        return None


class Reader:
    """화면 없이 띄운 opencode 서버 하나. 쓰고 나면 stop()으로 끈다."""

    def __init__(self, model: str = DEFAULT_MODEL, variant: str = DEFAULT_VARIANT):
        if "/" not in model:
            raise ReaderError(f"모델 이름은 '제공자/모델' 꼴이어야 합니다: {model}")
        self.provider, self.model = model.split("/", 1)
        self.variant = variant
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self._auth = ""
        self._job = None
        self._up = False
        self.calls = 0             # 보낸 요청 수(더 보낸 것 포함)
        self.made: set[str] = set()         # 이 객체가 만든 대화의 ID. 지울 때는 이것만 지운다
        self.cost = 0.0            # 받은 답들의 비용 합계(달러, opencode가 알려 주는 값)
        self.tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}   # 받은 답들의 토큰 합계
        self.lock = threading.Lock()

    # ---- 서버 띄우고 끄기

    def start(self) -> None:
        """서버를 띄우기 시작한다(기다리지 않는다). 준비됐는지는 wait()로 본다."""
        exe = executable()
        if not exe:
            raise ReaderError("opencode를 찾을 수 없습니다")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        password = secrets.token_urlsafe(24)
        self._auth = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()
        config = {
            "autoupdate": False,
            "share": "disabled",
            "snapshot": False,
            "mcp": {name: {"enabled": False} for name in _user_mcp_names()},
            "agent": {AGENT: {"description": "transcribe a math problem image", "mode": "primary",
                              "prompt": AGENT_PROMPT, "permission": {"*": "deny"}}},
        }
        # 설정은 환경 변수로만 넘긴다(사용자의 opencode 설정 파일은 건드리지 않는다). 한글은 넣지 않는다:
        # 윈도우에서 환경 변수의 한글이 깨져 들어간다
        env = dict(os.environ, OPENCODE_CONFIG_CONTENT=json.dumps(config), OPENCODE_SERVER_PASSWORD=password)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        home = _home()
        log = open(home / "serve.log", "wb")
        try:
            # --pure: 사용자가 깔아 둔 플러그인(알림 등)이 요청마다 끼어들지 않게 한다
            self.proc = subprocess.Popen([exe, "serve", "--pure", "--port", str(self.port), "--hostname", "127.0.0.1"],
                                         cwd=home, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, creationflags=flags)
        except OSError as e:
            raise ReaderError(f"opencode를 실행하지 못했습니다: {e}") from e
        finally:
            log.close()
        self._job = _kill_with_parent(self.proc)

    def wait(self, timeout: float = START_TIMEOUT) -> None:
        """서버가 요청을 받을 수 있을 때까지 기다린다."""
        if self._up:
            return
        if self.proc is None:
            self.start()
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise ReaderError("opencode 서버가 뜨자마자 꺼졌습니다(" + str(_home() / "serve.log") + ")")
            try:
                if self._call("GET", "/global/health", timeout=1.5).get("healthy"):
                    self._up = True
                    return
            except (OSError, ValueError, ReaderError):
                time.sleep(0.1)
        raise ReaderError("opencode 서버가 제때 뜨지 않았습니다")

    def purge(self) -> None:
        """이 객체가 만든 대화 가운데 아직 남은 것을 지운다(늦게 온 답을 버린 요청들의 대화).

        대화 목록을 받아 와서 고르지 않는다. opencode의 목록에는 git 저장소 밖에서 만든 사용자의 모든 대화가
        섞여 나오므로, 목록에서 골라 지우면 사용자의 대화를 지울 수 있다. 직접 만든 ID만 지운다.
        """
        for sid in list(self.made):
            try:
                self._call("DELETE", f"/session/{sid}", timeout=3)
                self.made.discard(sid)
            except Exception:
                pass

    def stop(self) -> None:
        if self._up and self.proc is not None and self.proc.poll() is None:
            self.purge()
        proc, self.proc, self._up = self.proc, None, False
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                # opencode.cmd → node → opencode.exe 로 이어진 것을 통째로 끝낸다
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def __enter__(self) -> "Reader":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---- 요청

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 120.0) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json", "Authorization": self._auth})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise ReaderError(f"opencode 서버가 요청을 거절했습니다({e.code}): {e.read()[:200]!r}") from e
        return json.loads(raw) if raw.strip() else {}

    def _attempt(self, parts: list[dict], out: "queue.Queue", gone: threading.Event, live: list[str],
                 variant: str | None = None) -> None:
        sid = ""
        try:
            sid = self._call("POST", "/session", {"title": "hwpick"}, timeout=20)["id"]
            self.made.add(sid)
            live.append(sid)
            if gone.is_set():
                return
            with self.lock:
                self.calls += 1
            msg = self._call("POST", f"/session/{sid}/message", {
                "model": {"providerID": self.provider, "modelID": self.model}, "agent": AGENT,
                "variant": variant or self.variant, "parts": parts}, timeout=150)
            err = (msg.get("info") or {}).get("error")
            with self.lock:
                self.cost += float((msg.get("info") or {}).get("cost") or 0)
                used = (msg.get("info") or {}).get("tokens") or {}
                for k in ("input", "output", "reasoning"):
                    self.tokens[k] += int(used.get(k) or 0)
                for k in ("read", "write"):
                    self.tokens["cache_" + k] += int((used.get("cache") or {}).get(k) or 0)
            text = "".join(p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text").strip()
            if err:
                out.put((False, ReaderError(f"모델이 답을 주지 못했습니다: {json.dumps(err, ensure_ascii=False)[:200]}")))
            else:
                out.put((True, text))      # 빈 답도 답이다(옮길 것이 없는 그림). 다시 보내도 같으므로 실패로 치지 않는다
        except Exception as e:
            out.put((False, e))
        finally:
            if sid:
                try:                                   # 쓰고 난 대화는 지운다(사용자의 opencode 기록에 쌓이지 않게)
                    self._call("DELETE", f"/session/{sid}", timeout=5)
                    self.made.discard(sid)
                    live.remove(sid)
                except Exception:
                    pass

    def _abort(self, sid: str) -> None:
        try:
            self._call("POST", f"/session/{sid}/abort", {}, timeout=5)
        except Exception:
            pass

    def ask(self, prompt: str, image: bytes | None = None, deadline: float | None = None,
            mime: str = "image/png", hedge: tuple[float, ...] = HEDGE_AFTER, variant: str | None = None) -> str:
        """지시문(과 그림 한 장)을 보내 답을 받는다. deadline(time.time() 기준)을 넘기면 TimeoutError."""
        self.wait()
        parts = [{"type": "text", "text": prompt}]
        if image is not None:
            parts.append({"type": "file", "mime": mime, "filename": "image." + mime.split("/")[-1],
                          "url": f"data:{mime};base64," + base64.b64encode(image).decode("ascii")})
        out: queue.Queue = queue.Queue()
        gone = threading.Event()
        live: list[str] = []
        running, sent, failures = 0, 0, []

        def launch() -> None:
            nonlocal running, sent
            threading.Thread(target=self._attempt, args=(parts, out, gone, live, variant), daemon=True).start()
            running += 1
            sent += 1

        launch()
        next_hedge = time.time() + (hedge[0] if hedge else 1e9)
        try:
            while True:
                now = time.time()
                if deadline is not None and now >= deadline:
                    raise TimeoutError("모델의 답이 제때 오지 않았습니다")
                limit = min(next_hedge, deadline) if deadline is not None else next_hedge
                try:
                    ok, value = out.get(timeout=max(0.05, limit - now))
                except queue.Empty:
                    if sent <= len(hedge):
                        launch()
                        next_hedge = time.time() + (hedge[sent - 1] if sent - 1 < len(hedge) else 1e9)
                    else:
                        next_hedge = time.time() + 1e9
                    continue
                running -= 1
                if ok:
                    return value
                failures.append(value)
                if running == 0:
                    if sent > len(hedge):
                        raise ReaderError(f"모델 요청이 모두 실패했습니다: {failures[-1]}")
                    launch()
        finally:
            gone.set()
            for sid in list(live):                     # 아직 답을 기다리는 나머지 요청은 그만두게 한다
                threading.Thread(target=self._abort, args=(sid,), daemon=True).start()
