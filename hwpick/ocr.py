"""로컬 경량 OCR(rapidocr, 한국어 인식 모델). 문제 번호 찾기와 한글 전사 확인에만 쓴다.

쪽 전체를 읽으면 CPU에서 쪽당 20초가 걸리므로, 호출하는 쪽에서 필요한 조각만 잘라
`read_crops`에 넘긴다. 폭을 몇 단계로 묶어 한 번에 추론해 GPU(DirectML) 재컴파일을 피한다.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np

REC_H = 48
WIDTH_STEP = 64
MAX_W = 640
BATCH = 16
LOCK = threading.RLock()     # 인식 엔진은 한 번에 한 스레드만 쓴다(번호 찾기와 전사 확인이 서로 다른 스레드에서 부른다)


@dataclass
class Line:
    """OCR이 찾은 글줄 하나. 좌표는 입력 이미지의 픽셀."""
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    score: float


@lru_cache(maxsize=1)
def _engine():
    import onnxruntime as ort
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    return RapidOCR(params={
        "Global.log_level": "error",
        "Global.use_cls": False,
        "EngineConfig.onnxruntime.use_dml": "DmlExecutionProvider" in ort.get_available_providers(),
        "Det.ocr_version": OCRVersion.PPOCRV5,
        "Det.model_type": ModelType.MOBILE,
        "Det.limit_type": "max",
        "Det.limit_side_len": 1920,
        "Rec.lang_type": LangRec.KOREAN,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.model_type": ModelType.MOBILE,
    })


def read_crops(crops: list[np.ndarray]) -> list[tuple[str, float]]:
    """잘라 둔 글 조각들을 읽어 (글자, 신뢰도) 목록을 입력 순서대로 돌려준다."""
    if not crops:
        return []
    with LOCK:
        rec = _engine().text_rec
    sized = []
    for i, c in enumerate(crops):
        h, w = c.shape[:2]
        tw = max(16, int(math.ceil(REC_H * w / max(h, 1))))
        bucket = min(MAX_W, int(math.ceil(tw / WIDTH_STEP)) * WIDTH_STEP)
        sized.append((bucket, i, min(tw, bucket)))
    sized.sort()
    out: list[tuple[str, float]] = [("", 0.0)] * len(crops)
    pos = 0
    while pos < len(sized):
        bucket = sized[pos][0]
        group = [s for s in sized[pos:pos + BATCH] if s[0] == bucket]
        batch = np.zeros((len(group), 3, REC_H, bucket), dtype=np.float32)
        for k, (_, i, tw) in enumerate(group):
            c = crops[i]
            if c.ndim == 2:
                c = cv2.cvtColor(c, cv2.COLOR_GRAY2BGR)
            r = cv2.resize(c, (tw, REC_H), interpolation=cv2.INTER_AREA if c.shape[0] > REC_H else cv2.INTER_CUBIC)
            batch[k, :, :, :tw] = (r.astype(np.float32).transpose(2, 0, 1) / 255.0 - 0.5) / 0.5
        with LOCK:
            preds = rec.session(batch)
        ratios = [tw / REC_H for _, _, tw in group]
        results, _ = rec.postprocess_op(preds, False, wh_ratio_list=ratios, max_wh_ratio=bucket / REC_H)
        for (_, i, _), (text, score) in zip(group, results):
            out[i] = (text, float(score))
        pos += len(group)
    return out


def read_lines(img: np.ndarray) -> list[Line]:
    """이미지 전체에서 글줄을 찾아 읽는다(느림: 작은 문제 조각의 전사 확인용)."""
    with LOCK:
        out = _engine()(img)
    if out is None or out.boxes is None:
        return []
    lines = []
    for box, text, score in zip(out.boxes, out.txts, out.scores):
        b = np.asarray(box, dtype=float)
        lines.append(Line(float(b[:, 0].min()), float(b[:, 1].min()),
                          float(b[:, 0].max()), float(b[:, 1].max()), text, float(score)))
    return lines
