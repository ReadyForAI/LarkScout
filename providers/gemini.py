"""Gemini LLM provider (default).

Reads credentials from the environment:
  GEMINI_API_KEY  or  GOOGLE_API_KEY  — required
  LARKSCOUT_LLM_MODEL                 — optional; defaults to gemini-2.5-flash
"""

import io
import logging
import os
import time

from providers.base import LLMProvider

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiProvider(LLMProvider):
    """LLM provider backed by the Google Gemini API (google-genai SDK)."""

    def __init__(self) -> None:
        self._client = None
        self._model = os.environ.get("LARKSCOUT_LLM_MODEL") or _DEFAULT_MODEL

    def _init(self) -> None:
        """Lazy-initialise the Gemini client on first use."""
        if self._client is not None:
            return

        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is not installed. Run: pip install google-genai"
            ) from exc

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Gemini API key not set. Export GEMINI_API_KEY or GOOGLE_API_KEY."
            )

        self._client = genai.Client(api_key=api_key)

    def summarize(self, text: str, prompt: str, max_retries: int = 2) -> str:
        """Generate a summary via Gemini Flash."""
        self._init()
        full_prompt = f"{prompt}\n\n---\n\n{text}"

        for attempt in range(max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=full_prompt,
                    config={"http_options": {"timeout": 60_000}},
                )
                return response.text.strip()
            except Exception as exc:
                if attempt < max_retries:
                    logger.warning("Gemini summarize retry (%d/%d): %s", attempt + 1, max_retries, exc)
                    time.sleep(2**attempt)
                else:
                    logger.error("Gemini summarize failed after %d retries: %s", max_retries, exc)
                    return "[summary generation failed]"
        return "[summary generation failed]"

    def ocr(self, image_bytes: bytes, page_num: int, max_retries: int = 2) -> str:
        """OCR a single page image via Gemini Vision."""
        self._init()

        import PIL.Image

        img = PIL.Image.open(io.BytesIO(image_bytes))
        ocr_prompt = "Extract all text from this image. Return only the extracted text, no commentary."

        for attempt in range(max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=[ocr_prompt, img],
                    config={"http_options": {"timeout": 60_000}},
                )
                return response.text.strip()
            except Exception as exc:
                if attempt < max_retries:
                    logger.warning("Gemini OCR retry (%d/%d) for page %d: %s", attempt + 1, max_retries, page_num, exc)
                    time.sleep(2**attempt)
                else:
                    logger.warning("Gemini OCR failed for page %d after %d retries: %s", page_num, max_retries, exc)
                    return f"[OCR failed for page {page_num}]"
        return f"[OCR failed for page {page_num}]"
