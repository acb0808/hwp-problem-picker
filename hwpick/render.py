"""PDF 쪽을 이미지로 렌더링한다. 좌표는 항상 PDF 포인트(pt) 기준으로 주고받는다."""
from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import pymupdf
import numpy as np

# 검출용 렌더링의 긴 변 픽셀 수. 본문 글자가 20px 안팎이 되어 경량 OCR이 읽을 수 있다.
DETECT_LONG_SIDE = 1800


def pdf_id(path: str | Path) -> str:
    """파일 내용으로 만든 짧은 식별자. 파일을 옮기거나 이름을 바꿔도 같다."""
    p = Path(path)
    h = hashlib.sha256()
    size = p.stat().st_size
    with p.open("rb") as f:
        h.update(f.read(1 << 20))
        if size > (2 << 20):
            f.seek(size - (1 << 20))
            h.update(f.read(1 << 20))
    h.update(str(size).encode())
    return h.hexdigest()[:10]


def page_size_pt(page: pymupdf.Page) -> tuple[float, float]:
    r = page.rect
    return float(r.width), float(r.height)


def render_page(page: pymupdf.Page, *, long_side: int | None = None, dpi: float | None = None,
                clip_pt: tuple[float, float, float, float] | None = None) -> tuple[np.ndarray, float]:
    """쪽(또는 clip 영역)을 BGR 이미지로 렌더링하고 (이미지, 픽셀/pt 배율)을 돌려준다."""
    w, h = page_size_pt(page)
    if dpi is not None:
        scale = dpi / 72.0
    else:
        scale = (long_side or DETECT_LONG_SIDE) / max(w, h)
    clip = pymupdf.Rect(*clip_pt) if clip_pt else None
    pm = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False, colorspace=pymupdf.csRGB)
    img = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width, 3)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR), scale


VECTOR_DPI = 600.0     # 스캔이 아닌 쪽(글자·선이 그대로 든 PDF)을 그림으로 자를 때의 해상도. 선이 살아 있어 높게 그릴수록 또렷하다
MIN_FIGURE_DPI = 300.0 # 스캔이 이보다 흐린 쪽도 이 해상도보다 낮게 넣지는 않는다(예전에 넣던 것보다 작아지지 않게)
MAX_SCAN_DPI = 1200.0  # 이보다 촘촘한 스캔은 이 해상도로 줄인다(한글 파일이 터무니없이 커지지 않게)
_decoded: dict[tuple[int, int], np.ndarray] = {}     # 방금 푼 쪽 그림(같은 쪽에서 그림을 여럿 자를 때 다시 풀지 않는다)


def scan_dpi(page: pymupdf.Page, clip_pt: tuple[float, float, float, float]) -> float | None:
    """clip에 걸친 스캔 그림의 해상도(dpi). 여럿이면 가장 촘촘한 것, 스캔 그림이 없으면 None."""
    clip = pymupdf.Rect(*clip_pt)
    best = None
    for info in page.get_image_info():
        box = pymupdf.Rect(info["bbox"])
        if box.is_empty or not info.get("width") or not info.get("height"):
            continue
        if page.rotation == 0:
            share = (box & clip).get_area() / max(1e-6, clip.get_area())
        else:                   # 돌려 놓은 쪽은 그림 자리의 좌표계가 달라 겹침을 믿지 않는다. 쪽을 넓게 덮은 그림만 본다
            share = box.get_area() / max(1e-6, page.rect.get_area())
        if share < 0.2:
            continue
        dpi = (info["width"] * info["height"] / box.get_area()) ** 0.5 * 72
        best = max(best or 0.0, dpi)
    return best


def scan_clip(page: pymupdf.Page, clip_pt: tuple[float, float, float, float]) -> tuple[np.ndarray, float]:
    """clip 영역을 교재에 스캔된 해상도 그대로 잘라 낸다. (BGR 이미지, 그 해상도 dpi)

    한글 문서에 붙일 그림에 쓴다. 600dpi로 스캔한 교재를 300dpi로 다시 그리면 화질이 절반이 되므로, 쪽에 든 스캔 그림의
    픽셀을 그대로 오려 낸다(쪽 전체가 그림 한 장이고 돌려 놓지 않은 보통의 스캔본). 그렇게 오린 것이 쪽을 그린 것과
    다르면(그림 위에 글자가 얹혀 있거나 자리가 어긋남) 스캔 해상도로 쪽을 그려서 쓴다. 스캔이 아닌 쪽은 VECTOR_DPI로 그린다.
    """
    clip = pymupdf.Rect(*clip_pt) & page.rect
    found = scan_dpi(page, tuple(clip))
    dpi = VECTOR_DPI if found is None else min(max(found, MIN_FIGURE_DPI), MAX_SCAN_DPI)
    if found is not None and found < 250 and (page.get_text("words", clip=clip) or page.get_drawings()):
        dpi = max(dpi, VECTOR_DPI)          # 흐린 바탕 그림 위에 글자·선이 따로 든 쪽: 글자가 뭉개지지 않게 넉넉히 그린다
    drawn, _ = render_page(page, dpi=dpi, clip_pt=tuple(clip))
    if found is None or dpi != found or page.rotation != 0:
        return drawn, dpi
    # 쪽 그림의 픽셀을 그대로 오린다: clip을 통째로 덮는 그림 하나가 반듯하게 놓여 있을 때만
    covers = [i for i in page.get_image_info(xrefs=True) if i.get("xref") and pymupdf.Rect(i["bbox"]).contains(clip)]
    if len(covers) != 1:
        return drawn, dpi
    info = covers[0]
    m = info["transform"]
    if abs(m[1]) > 1e-3 or abs(m[2]) > 1e-3 or m[0] <= 0 or m[3] <= 0:
        return drawn, dpi
    key = (id(page.parent), info["xref"])
    full = _decoded.get(key)
    if full is None:
        pix = pymupdf.Pixmap(page.parent, info["xref"])
        if pix.alpha or pix.colorspace is None or pix.colorspace.n not in (1, 3):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix) if pix.colorspace is not None and not pix.alpha else None
        if pix is None:
            return drawn, dpi
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        full = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR if pix.n == 1 else cv2.COLOR_RGB2BGR)
        _decoded.clear()
        _decoded[key] = full
    box = pymupdf.Rect(info["bbox"])
    sx, sy = full.shape[1] / box.width, full.shape[0] / box.height
    x0, y0 = int(round((clip.x0 - box.x0) * sx)), int(round((clip.y0 - box.y0) * sy))
    x1, y1 = int(round((clip.x1 - box.x0) * sx)), int(round((clip.y1 - box.y0) * sy))
    cut = full[max(0, y0):min(full.shape[0], y1), max(0, x0):min(full.shape[1], x1)]
    if cut.size == 0 or abs(cut.shape[0] - drawn.shape[0]) > 3 or abs(cut.shape[1] - drawn.shape[1]) > 3:
        return drawn, dpi
    a = cv2.cvtColor(cv2.resize(cut, (drawn.shape[1], drawn.shape[0])), cv2.COLOR_BGR2GRAY).astype(np.int16)
    b = cv2.cvtColor(drawn, cv2.COLOR_BGR2GRAY).astype(np.int16)
    if float(np.abs(a - b).mean()) > 6.0:
        return drawn, dpi                    # 오린 것과 그린 것이 다르다: 그림 위에 다른 것이 얹혀 있다
    return cut.copy(), dpi


def imwrite(path: str | Path, img: np.ndarray, quality: int = 90) -> None:
    """한글·공백 경로에서도 동작하는 이미지 저장."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix.lower() or ".png"
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if ext in (".jpg", ".jpeg") else []
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise OSError(f"이미지 인코딩 실패: {p}")
    buf.tofile(str(p))


def imread(path: str | Path) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise OSError(f"이미지를 열 수 없음: {path}")
    return img
