#!/usr/bin/env python3
"""Isolated PaddleOCR JSONL worker.

The parent process communicates over stdin/stdout using one JSON object per
line. All third-party stdout noise is redirected to stderr so protocol output
stays parseable.
"""

from __future__ import annotations

import base64
import io
import importlib.metadata
import json
import os
import sys
from typing import Any


_protocol_out: Any | None = None


def _setup_protocol_output() -> None:
    global _protocol_out
    if _protocol_out is None:
        _protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr


def _write(message: dict[str, Any]) -> None:
    if _protocol_out is None:
        _setup_protocol_output()
    _protocol_out.write(json.dumps(message, ensure_ascii=False) + "\n")
    _protocol_out.flush()


def _flatten_paddle_ocr_result(result: Any) -> str:
    lines: list[str] = []
    blocks = result if isinstance(result, list) else [result]
    for block in blocks:
        if isinstance(block, dict):
            texts = block.get("rec_texts") or []
            for text in texts:
                value = str(text).strip()
                if value:
                    lines.append(value)
            continue
        if isinstance(block, list):
            for item in block:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                payload = item[1]
                if isinstance(payload, (list, tuple)) and payload:
                    text = str(payload[0]).strip()
                else:
                    text = str(payload).strip()
                if text:
                    lines.append(text)
    return "\n".join(lines).strip()


def _build_engine():
    os.environ.setdefault("FLAGS_enable_pir_api", "0")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

    from paddleocr import PaddleOCR

    v2_kwargs: dict[str, Any] = {
        "use_angle_cls": False,
        "lang": os.environ.get("LARKSCOUT_LOCAL_OCR_LANG", "ch"),
        "show_log": False,
    }
    try:
        major = int(importlib.metadata.version("paddleocr").split(".", 1)[0])
    except Exception:
        major = 0
    if major and major < 3:
        return PaddleOCR(**v2_kwargs), "v2"

    v3_kwargs: dict[str, Any] = {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "text_detection_model_name": os.environ.get(
            "LARKSCOUT_LOCAL_OCR_DET_MODEL", "PP-OCRv5_mobile_det"
        ),
        "text_recognition_model_name": os.environ.get(
            "LARKSCOUT_LOCAL_OCR_REC_MODEL", "PP-OCRv5_mobile_rec"
        ),
    }
    if os.environ.get("LARKSCOUT_LOCAL_OCR_ENABLE_HPI", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        v3_kwargs["enable_hpi"] = True
    device = os.environ.get("LARKSCOUT_LOCAL_OCR_DEVICE", "").strip()
    if device:
        v3_kwargs["device"] = device
    try:
        engine = PaddleOCR(**v3_kwargs)
    except TypeError:
        return PaddleOCR(**v2_kwargs), "v2"
    if hasattr(engine, "predict"):
        return engine, "v3"
    return PaddleOCR(**v2_kwargs), "v2"


def _predict(engine: Any, api_version: str, image_array: Any) -> Any:
    if api_version == "v3":
        return engine.predict(
            image_array,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return engine.ocr(image_array, cls=False)


def main() -> int:
    _setup_protocol_output()
    try:
        import numpy as np
        from PIL import Image

        engine, api_version = _build_engine()
    except BaseException as exc:
        _write({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
        return 2

    _write({"type": "ready"})

    for line in sys.stdin:
        try:
            request = json.loads(line)
            page_num = int(request.get("page_num") or 0)
            image_bytes = base64.b64decode(str(request.get("image_b64") or ""), validate=True)
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            result = _predict(engine, api_version, np.asarray(image))
            text = _flatten_paddle_ocr_result(result)
            _write({"ok": True, "page_num": page_num, "text": text})
        except BaseException as exc:
            _write(
                {
                    "ok": False,
                    "page_num": int(request.get("page_num") or 0) if "request" in locals() else 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
