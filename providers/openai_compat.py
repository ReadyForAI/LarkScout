"""OpenAI-compatible LLM provider.

Works with OpenAI and OpenAI-compatible REST APIs such as local Ollama,
Together AI, Groq, and similar providers via the official OpenAI SDK.

Environment variables:
  LARKSCOUT_LLM_VENDOR    — Vendor profile for compatible APIs (openai, zhipu, kimi)
  LARKSCOUT_LLM_API_KEY   — API key (required; use "ollama" for local Ollama)
  LARKSCOUT_LLM_BASE_URL  — Base URL override (defaults to vendor profile URL)
  LARKSCOUT_LLM_MODEL     — Model name override (defaults to vendor profile model)
  LARKSCOUT_OCR_MODEL     — OCR vision model override (defaults to vendor OCR model or text model)
  LARKSCOUT_OCR_IMAGE_INPUT_MODE — OCR image serialization mode: data_url, plain_base64, remote_url_only
"""

import base64
import json
import logging
import os
import time

from providers.base import LLMProvider
from providers.vendor_profiles import get_vendor_profile

logger = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    """LLM provider backed by the official OpenAI SDK."""

    def __init__(self) -> None:
        from openai import OpenAI

        self._vendor = get_vendor_profile(os.environ.get("LARKSCOUT_LLM_VENDOR"))
        self._api_key = os.environ.get("LARKSCOUT_LLM_API_KEY", "")
        base_url = os.environ.get("LARKSCOUT_LLM_BASE_URL") or self._vendor.base_url
        self._base_url = base_url.rstrip("/")
        self._model = (
            os.environ.get("LARKSCOUT_LLM_MODEL")
            or self._vendor.default_text_model
            or "gpt-4o-mini"
        )
        self._ocr_model = (
            os.environ.get("LARKSCOUT_OCR_MODEL")
            or self._vendor.default_ocr_model
            or self._model
        )
        self._ocr_image_input_mode = self._resolve_image_input_mode(
            os.environ.get("LARKSCOUT_OCR_IMAGE_INPUT_MODE") or self._vendor.image_input_mode
        )
        self._chat_extra_body = self._merge_extra_body(
            self._vendor.extra_chat_body,
            os.environ.get("LARKSCOUT_LLM_EXTRA_BODY_JSON"),
            env_name="LARKSCOUT_LLM_EXTRA_BODY_JSON",
        )
        self._ocr_extra_body = self._merge_extra_body(
            self._vendor.extra_ocr_body,
            os.environ.get("LARKSCOUT_OCR_EXTRA_BODY_JSON"),
            env_name="LARKSCOUT_OCR_EXTRA_BODY_JSON",
        )

        if not self._api_key:
            raise RuntimeError(
                "LARKSCOUT_LLM_API_KEY is not set. "
                "Export it before starting the service."
            )

        self._client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
            timeout=120,
        )

    @staticmethod
    def _resolve_image_input_mode(raw_mode: str | None) -> str:
        mode = (raw_mode or "data_url").strip().lower()
        allowed = {"data_url", "plain_base64", "remote_url_only"}
        if mode not in allowed:
            raise RuntimeError(
                "LARKSCOUT_OCR_IMAGE_INPUT_MODE must be one of: "
                "data_url, plain_base64, remote_url_only."
            )
        return mode

    @staticmethod
    def _merge_extra_body(
        base: dict,
        raw_json: str | None,
        *,
        env_name: str,
    ) -> dict:
        merged = dict(base)
        if not raw_json:
            return merged
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{env_name} must be valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{env_name} must decode to a JSON object.")
        merged.update(parsed)
        return merged

    @staticmethod
    def _message_text(message_content) -> str:
        if isinstance(message_content, str):
            return message_content.strip()
        if isinstance(message_content, list):
            parts: list[str] = []
            for item in message_content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if text:
                        parts.append(str(text).strip())
            return "\n".join(part for part in parts if part).strip()
        return str(message_content).strip()

    def _build_ocr_image_part(self, image_bytes: bytes) -> dict:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        if self._ocr_image_input_mode == "data_url":
            url = f"data:image/png;base64,{b64}"
        elif self._ocr_image_input_mode == "plain_base64":
            url = b64
        else:
            raise RuntimeError(
                "LARKSCOUT_OCR_IMAGE_INPUT_MODE=remote_url_only is not supported by the "
                "current OCR pipeline because it renders pages in-memory and does not have "
                "a hosted image URL to send upstream."
            )
        return {
            "type": "image_url",
            "image_url": {"url": url},
        }

    def _chat(
        self,
        messages: list,
        max_retries: int = 2,
        model: str | None = None,
        extra_body: dict | None = None,
    ) -> str:
        """Call chat.completions.create and return the assistant text."""

        for attempt in range(max_retries + 1):
            try:
                kwargs = {
                    "model": model or self._model,
                    "messages": messages,
                }
                if extra_body:
                    kwargs.update(extra_body)
                resp = self._client.chat.completions.create(
                    **kwargs,
                )
                return self._message_text(resp.choices[0].message.content)
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
        return self._chat(
            messages,
            max_retries=max_retries,
            extra_body=self._chat_extra_body,
        )

    def ocr(self, image_bytes: bytes, page_num: int) -> str:
        """OCR a page image via the OpenAI vision endpoint (base64-encoded)."""
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Extract all text from this image. Return only the extracted text, no commentary.",
                    },
                    self._build_ocr_image_part(image_bytes),
                ],
            }
        ]
        result = self._chat(
            messages,
            max_retries=2,
            model=self._ocr_model,
            extra_body=self._ocr_extra_body,
        )
        if result.startswith("["):
            logger.warning("OpenAI-compat OCR may have failed for page %d", page_num)
        return result
