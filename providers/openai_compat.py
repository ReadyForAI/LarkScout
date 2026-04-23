"""OpenAI-compatible LLM provider.

Works with any OpenAI-compatible REST API: OpenAI, DeepSeek, local Ollama,
Together AI, Groq, etc.  No openai SDK is required — uses httpx directly.

Environment variables:
  LARKSCOUT_LLM_API_KEY   — API key (required; use "ollama" for local Ollama)
  LARKSCOUT_LLM_BASE_URL  — Base URL (default: https://api.openai.com/v1)
  LARKSCOUT_LLM_MODEL     — Model name (default: gpt-4o-mini)
"""

import base64
import logging
import os
import time

from providers.base import LLMProvider

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


class OpenAICompatProvider(LLMProvider):
    """LLM provider for any OpenAI-compatible REST API."""

    def __init__(self) -> None:
        self._api_key = os.environ.get("LARKSCOUT_LLM_API_KEY", "")
        self._base_url = os.environ.get("LARKSCOUT_LLM_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
        self._model = os.environ.get("LARKSCOUT_LLM_MODEL") or _DEFAULT_MODEL

        if not self._api_key:
            raise RuntimeError(
                "LARKSCOUT_LLM_API_KEY is not set. "
                "Export it before starting the service."
            )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _chat(self, messages: list, max_retries: int = 2) -> str:
        """POST /chat/completions and return the assistant text."""
        import httpx

        url = f"{self._base_url}/chat/completions"
        payload = {"model": self._model, "messages": messages}

        for attempt in range(max_retries + 1):
            try:
                resp = httpx.post(url, json=payload, headers=self._headers(), timeout=120)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except Exception as exc:
                if attempt < max_retries:
                    logger.warning(
                        "OpenAI-compat chat retry (%d/%d): %s", attempt + 1, max_retries, exc
                    )
                    time.sleep(2**attempt)
                else:
                    logger.error("OpenAI-compat chat failed after %d retries: %s", max_retries, exc)
                    return "[summary generation failed]"
        return "[summary generation failed]"

    def summarize(self, text: str, prompt: str, max_retries: int = 2) -> str:
        """Generate a summary via the OpenAI chat completions endpoint."""
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ]
        return self._chat(messages, max_retries=max_retries)

    def ocr(self, image_bytes: bytes, page_num: int) -> str:
        """OCR a page image via the OpenAI vision endpoint (base64-encoded)."""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Extract all text from this image. Return only the extracted text, no commentary.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ]
        result = self._chat(messages, max_retries=2)
        if result.startswith("["):
            logger.warning("OpenAI-compat OCR may have failed for page %d", page_num)
        return result
