"""PDF 하나의 검출 결과를 관리한다. 느린 단계(쪽 렌더링, 번호 후보 OCR, 번호 양식 학습)는 디스크에 캐시한다.

캐시는 서버와 명령줄 도구가 함께 쓰므로 파일은 임시 이름으로 쓴 뒤 바꿔치기한다.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from . import detect, layout, render
from .detect import PageTokens
from .layout import Layout, Token

# 토큰을 뽑는 방식이 바뀌면 올린다(이전 캐시를 무시하고 다시 읽는다)
TOKEN_VERSION = 10
# 번호 양식 학습이나 박스 만드는 방식이 바뀌면 올린다(OCR은 다시 하지 않는다)
DETECT_VERSION = 4
PROFILE_SAMPLE = 30
PAGE_JPEG_QUALITY = 88

# PyMuPDF는 여러 스레드에서 동시에 쓰면 안 된다(문서가 달라도 마찬가지). 문서를 건드리는 곳은 모두 이 잠금 안에서 한다.
PDF_LOCK = threading.RLock()


def cache_root() -> Path:
    env = os.environ.get("HWPICK_CACHE")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "HWPick" / "cache"


def _write(f: Path, data: bytes | str) -> None:
    tmp = f.with_name(f"{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    for attempt in range(6):
        try:
            os.replace(tmp, f)
            return
        except PermissionError:            # 윈도우: 다른 스레드·프로세스가 그 파일을 읽는 중이면 잠깐 막힌다
            if attempt == 5:
                tmp.unlink(missing_ok=True)
                if not f.exists():
                    raise
            time.sleep(0.05 * (attempt + 1))


def _token(d: dict) -> Token:
    return Token(**{**d, "color": tuple(d["color"]), "read": tuple(d["read"]) if d.get("read") else None})


def _load_tokens(f: Path) -> list[Token]:
    return [_token(d) for d in json.loads(f.read_text(encoding="utf-8"))]


def _save_tokens(f: Path, toks: list[Token]) -> None:
    _write(f, json.dumps([asdict(t) for t in toks], ensure_ascii=False))


def cached_pages(path: str | Path) -> int | None:
    """이미 열어 본 파일이면 쪽 수(한글 문서는 PDF로 바꿔 봐야 쪽 수를 알 수 있다). 아니면 None."""
    try:
        return int(json.loads((cache_root() / render.pdf_id(path) / "meta.json").read_text(encoding="utf-8"))["pages"])
    except (OSError, ValueError, KeyError):
        return None


class PdfIndex:
    """교재 하나(PDF, 또는 한글 문서). 한글 문서는 한글 프로그램으로 PDF를 만들어 그것으로 쪽 그림을 그린다."""

    def __init__(self, pdf_path: str | Path, known_id: str | None = None):
        from .hwp import source
        self.path = Path(pdf_path).resolve()
        self.document = source.is_document(self.path)
        if known_id and self.document and all(f.is_file() for f in source.converted(cache_root() / known_id)):
            self.id = known_id                # 문제를 고른 뒤에 문서를 고쳤거나 옮겼어도, 골랐을 때의 모습으로 만든다
        else:
            self.id = render.pdf_id(self.path)
        self.dir = cache_root() / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hwpx: Path | None = None
        shown = self.path
        if self.document:
            shown, self.hwpx = source.convert(self.path, self.dir)
        with PDF_LOCK:
            self.doc = pymupdf.open(str(shown))
            self.page_count = self.doc.page_count
        self._sizes: list[tuple[float, float]] | None = None
        self._profile: detect.Profile | None = None
        self._layouts: dict[int, Layout] = {}
        self._layout_lock = threading.Lock()
        self._slots: list | None = None
        self._slot_boxes: dict[int, list[dict]] | None = None
        self._slot_of: dict[str, object] = {}
        self._slot_lock = threading.Lock()
        self._notes: list[str] = []
        self._parsed = None

    # ---- 한글 문서: 문제의 내용은 HWPX에서, 쪽 위의 자리는 PDF 글자층의 문제 번호에서 얻는다(번호 양식을 익히지 않는다)

    def slots(self) -> list:
        """문서에서 꺼낸 문제들. 문제 번호를 알아볼 수 없는 문서(와 PDF)는 빈 목록이고, 그때는 여느 PDF처럼 다룬다."""
        if self._slots is None:
            from .hwp import source
            self._parsed = source.parse_document(self.hwpx) if self.hwpx else source.Parsed([])
            self._slots = self._parsed.slots
        return self._slots

    def slot(self, box_id: str):
        """박스에 해당하는 문제(내용을 문서에서 그대로 꺼낼 수 있는 것). 없으면 None."""
        if not self.document:
            return None
        self._all_slot_boxes()
        return self._slot_of.get(box_id)

    def slot_notes(self) -> list[str]:
        """문서를 문제로 나누면서 생긴, 사람이 알아야 할 점."""
        if not self.document:
            return []
        self._all_slot_boxes()
        return self._notes

    def _all_slot_boxes(self) -> dict[int, list[dict]]:
        """한글 문서의 쪽별 박스. 문제 번호를 알아볼 수 없는 문서는 박스가 없다(PDF처럼 번호 양식을 익히지 않는다:
        한글이 저장한 PDF에서는 그 방식이 엉뚱한 양식을 배운다)."""
        with self._slot_lock:
            if self._slot_boxes is None:
                if self.slots():
                    from .hwp import place
                    self._slot_boxes, self._slot_of, self._notes = place.locate(self, self._parsed)
                else:
                    self._slot_boxes, self._slot_of = {}, {}
                    self._notes = ["문제 번호를 알아볼 수 없는 문서라 박스를 치지 못했습니다(`look`·`add` 명령이나 선택 화면의 `직접 그리기`로 담을 수 있습니다)"]
            return self._slot_boxes

    # ---- 쪽 단위

    def sizes(self) -> list[tuple[float, float]]:
        """모든 쪽의 (폭, 높이) pt. 한 번 읽어 저장해 둔다."""
        with PDF_LOCK:
            if self._sizes is None:
                meta = self.dir / "meta.json"
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
                if len(data.get("sizes", [])) != self.page_count:
                    sizes = [[round(v, 2) for v in render.page_size_pt(self.doc[i])] for i in range(self.page_count)]
                    data = {"path": str(self.path), "pages": self.page_count, "sizes": sizes}
                    _write(meta, json.dumps(data, ensure_ascii=False))
                self._sizes = [tuple(s) for s in data["sizes"]]
            return self._sizes

    def scale(self, page: int) -> float:
        """검출 이미지의 픽셀/pt 배율."""
        w, h = self.sizes()[page]
        return render.DETECT_LONG_SIDE / max(w, h)

    def page_file(self, page: int) -> Path:
        """검출과 화면 표시에 함께 쓰는 쪽 이미지 파일. 큰 스캔은 렌더링이 느리므로 JPEG로 캐시한다."""
        f = self.dir / f"pg_{page:04d}.jpg"
        if not f.exists():
            with PDF_LOCK:
                if not f.exists():
                    img, _ = render.render_page(self.doc[page])
                    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, PAGE_JPEG_QUALITY])
                    if not ok:
                        raise OSError(f"쪽 이미지 인코딩 실패: {self.path} {page + 1}쪽")
                    _write(f, buf.tobytes())
        return f

    def page_image(self, page: int) -> np.ndarray:
        """검출용 쪽 이미지. 처음에도 저장본을 다시 읽어 돌려주므로 분석 결과가 실행마다 같다."""
        return render.imread(self.page_file(page))

    def layout(self, page: int) -> Layout:
        # 번호 인식 스레드와 미리 옮겨 적는 스레드가 함께 쓴다
        with self._layout_lock:
            lay = self._layouts.get(page)
            if lay is None:
                if len(self._layouts) >= 4:
                    self._layouts.pop(next(iter(self._layouts)))
                lay = self._layouts[page] = layout.analyze(self.page_image(page))
            return lay

    def page_tokens(self, page: int) -> PageTokens:
        """쪽의 번호 후보(줄 머리의 짧은 글자 + 색 배지)를 읽는다. OCR 결과는 캐시한다."""
        f = self.dir / f"tok{TOKEN_VERSION}_{page:04d}.json"
        if f.exists():
            d = json.loads(f.read_text(encoding="utf-8"))
            return PageTokens(d["em"], d["width"], d["height"], [_token(t) for t in d["tokens"]])
        lay = self.layout(page)
        toks = layout.line_start_tokens(lay) + layout.fill_tokens(lay) + layout.badge_tokens(lay)
        detect.read_tokens(lay, toks)
        H, W = lay.text.shape
        _write(f, json.dumps({"em": lay.em, "width": W, "height": H, "tokens": [asdict(t) for t in toks]},
                             ensure_ascii=False))
        return PageTokens(lay.em, W, H, toks)

    def tokens(self, page: int) -> tuple[Layout, list[Token]]:
        return self.layout(page), self.page_tokens(page).tokens

    def inline_tokens(self, page: int) -> list[Token]:
        """앞에 배지가 붙은 번호 후보. 번호 양식에 맞는 덩어리만 읽으므로 양식과 함께 캐시한다."""
        prof = self.profile()
        if not any(t.colored for t in prof.tracks):
            return []
        f = self.dir / f"inl{TOKEN_VERSION}_{DETECT_VERSION}_{page:04d}.json"
        if f.exists():
            return _load_tokens(f)
        lay = self.layout(page)
        toks = layout.inline_tokens(lay, prof.accepts_inline)
        detect.read_tokens(lay, toks)
        _save_tokens(f, toks)
        return toks

    # ---- 교재 단위

    def sample_pages(self, n: int = PROFILE_SAMPLE) -> list[int]:
        total = self.page_count
        if total <= n:
            return list(range(total))
        return sorted({int(round(v)) for v in np.linspace(0.06 * total, 0.94 * total - 1, n)})

    def _profile_file(self) -> Path:
        return self.dir / f"profile{TOKEN_VERSION}_{DETECT_VERSION}.json"

    def has_profile(self) -> bool:
        if self.document:
            return True                       # 한글 문서는 문서 속 구조에서 문제를 얻는다. 번호 양식을 익힐 것이 없다
        return self._profile is not None or self._profile_file().exists()

    def profile(self, *, relearn: bool = False, progress=None, trace: list[str] | None = None) -> detect.Profile:
        """교재의 번호 양식. 처음 한 번은 표본 쪽들을 읽어 학습한다(progress(완료, 전체)로 알린다)."""
        if self._profile is not None and not relearn:
            return self._profile
        f = self._profile_file()
        if f.exists() and not relearn:
            self._profile = detect.Profile.from_json(json.loads(f.read_text(encoding="utf-8")))
            return self._profile
        pages = {}
        sample = self.sample_pages()
        for i, p in enumerate(sample):
            pages[p] = self.page_tokens(p)
            if progress:
                progress(i + 1, len(sample))
        self._profile = detect.learn_profile(pages, self.layout, trace)
        for old in list(self.dir.glob(f"inl{TOKEN_VERSION}_{DETECT_VERSION}_*.json")) \
                + list(self.dir.glob(f"box{TOKEN_VERSION}_{DETECT_VERSION}_*.json")):
            old.unlink(missing_ok=True)
        _write(f, json.dumps(self._profile.to_json(), ensure_ascii=False, indent=1))
        return self._profile

    def detect(self, page: int) -> tuple[detect.PageResult, Layout]:
        prof = self.profile()
        lay, toks = self.tokens(page)
        return detect.detect_page(lay, toks, prof, self.inline_tokens(page)), lay

    def peek_boxes(self, page: int) -> list[dict] | None:
        """이미 찾아 둔 쪽이면 그 박스들, 아직이면 None(새로 찾지 않는다)."""
        if self.document:
            return self._all_slot_boxes().get(page, [])
        f = self.dir / f"box{TOKEN_VERSION}_{DETECT_VERSION}_{page:04d}.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def boxes(self, page: int) -> list[dict]:
        """쪽의 문항 박스. 좌표는 PDF 포인트(왼쪽 위 원점)이고, id는 '교재-쪽-번호'다. 결과는 캐시한다."""
        if self.document:
            return self._all_slot_boxes().get(page, [])
        f = self.dir / f"box{TOKEN_VERSION}_{DETECT_VERSION}_{page:04d}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        res, _ = self.detect(page)
        s = self.scale(page)

        def pt(rect) -> list[float]:
            return [round(v / s, 1) for v in rect]

        out, used = [], set()
        for b in res.boxes:
            pid = f"{self.id[:6]}-{page + 1}-{b.label}"
            while pid in used:
                pid += "b"
            used.add(pid)
            out.append({"id": pid, "label": b.label, "bbox": pt((b.x0, b.y0, b.x1, b.y1)),
                        "stem": pt(b.stem) if b.stem else None, "flags": b.flags})
        _write(f, json.dumps(out, ensure_ascii=False))
        return out
