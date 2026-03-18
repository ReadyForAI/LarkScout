"""Tests for the Multi-LLM Provider Abstraction (TASK-006)."""

import os
from unittest.mock import MagicMock, patch

import pytest

import providers as providers_module
from providers import get_provider, reset_provider
from providers.base import LLMProvider


@pytest.fixture(autouse=True)
def _reset():
    """Reset the cached provider before every test."""
    reset_provider()
    yield
    reset_provider()


# ── AC-1: abstract base class ──────────────────────────────────────────────────

class TestLLMProviderInterface:
    def test_is_abstract(self):
        """LLMProvider cannot be instantiated directly."""
        with pytest.raises(TypeError):
            LLMProvider()  # type: ignore[abstract]

    def test_concrete_must_implement_both_methods(self):
        """A subclass that skips summarize or ocr raises TypeError."""
        class BadProvider(LLMProvider):
            def summarize(self, text, prompt, max_retries=2):
                return ""
            # ocr not implemented

        with pytest.raises(TypeError):
            BadProvider()

    def test_concrete_provider_satisfies_interface(self):
        """A fully implemented subclass can be instantiated."""
        class GoodProvider(LLMProvider):
            def summarize(self, text, prompt, max_retries=2):
                return "ok"

            def ocr(self, image_bytes, page_num):
                return "text"

        p = GoodProvider()
        assert isinstance(p, LLMProvider)


# ── AC-2: Gemini provider ─────────────────────────────────────────────────────

class TestGeminiProvider:
    def test_get_provider_returns_gemini_by_default(self, monkeypatch):
        """With no env var, get_provider() returns a GeminiProvider."""
        monkeypatch.delenv("LARKSCOUT_LLM_PROVIDER", raising=False)
        p = get_provider()
        assert "gemini" in type(p).__name__.lower()

    def test_gemini_provider_is_llm_provider(self, monkeypatch):
        monkeypatch.delenv("LARKSCOUT_LLM_PROVIDER", raising=False)
        p = get_provider()
        assert isinstance(p, LLMProvider)

    def test_gemini_summarize_delegates_to_sdk(self, monkeypatch):
        """GeminiProvider.summarize() calls client.models.generate_content."""
        monkeypatch.delenv("LARKSCOUT_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

        mock_response = MagicMock()
        mock_response.text = "  summary result  "
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        mock_genai = MagicMock()
        mock_genai.Client.return_value = mock_client

        with patch.dict("sys.modules", {"google": MagicMock(genai=mock_genai), "google.genai": mock_genai}):
            p = get_provider()
            result = p.summarize("some text", "summarise this")

        assert result == "summary result"
        mock_client.models.generate_content.assert_called_once()

    def test_gemini_ocr_delegates_to_sdk(self, monkeypatch):
        """GeminiProvider.ocr() calls client.models.generate_content with an image."""
        monkeypatch.delenv("LARKSCOUT_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

        mock_response = MagicMock()
        mock_response.text = "extracted text"
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        mock_genai = MagicMock()
        mock_genai.Client.return_value = mock_client

        # Minimal 1×1 PNG bytes
        import base64
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        )
        image_bytes = base64.b64decode(png_b64)

        mock_pil_image = MagicMock()
        mock_pil_module = MagicMock()
        mock_pil_module.Image.open.return_value = mock_pil_image

        with patch.dict(
            "sys.modules",
            {
                "google": MagicMock(genai=mock_genai),
                "google.genai": mock_genai,
                "PIL": mock_pil_module,
                "PIL.Image": mock_pil_module.Image,
            },
        ):
            p = get_provider()
            result = p.ocr(image_bytes, page_num=1)

        assert result == "extracted text"
        mock_client.models.generate_content.assert_called_once()


# ── AC-3: OpenAI-compat provider ──────────────────────────────────────────────

class TestOpenAICompatProvider:
    def test_get_provider_returns_openai_compat(self, monkeypatch):
        monkeypatch.setenv("LARKSCOUT_LLM_PROVIDER", "openai")
        monkeypatch.setenv("LARKSCOUT_LLM_API_KEY", "sk-test")
        p = get_provider()
        assert "openai" in type(p).__name__.lower()

    def test_openai_compat_is_llm_provider(self, monkeypatch):
        monkeypatch.setenv("LARKSCOUT_LLM_PROVIDER", "openai")
        monkeypatch.setenv("LARKSCOUT_LLM_API_KEY", "sk-test")
        p = get_provider()
        assert isinstance(p, LLMProvider)

    def test_openai_compat_missing_api_key_raises(self, monkeypatch):
        monkeypatch.setenv("LARKSCOUT_LLM_PROVIDER", "openai")
        monkeypatch.delenv("LARKSCOUT_LLM_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="LARKSCOUT_LLM_API_KEY"):
            get_provider()

    def test_openai_compat_summarize_calls_http(self, monkeypatch):
        """OpenAICompatProvider.summarize() uses httpx to POST /chat/completions."""
        monkeypatch.setenv("LARKSCOUT_LLM_PROVIDER", "openai")
        monkeypatch.setenv("LARKSCOUT_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LARKSCOUT_LLM_BASE_URL", "https://api.example.com/v1")
        monkeypatch.setenv("LARKSCOUT_LLM_MODEL", "gpt-test")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "  great summary  "}}]
        }
        mock_resp.raise_for_status = MagicMock()

        mock_httpx = MagicMock()
        mock_httpx.post.return_value = mock_resp

        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            p = get_provider()
            result = p.summarize("body text", "system prompt")

        assert result == "great summary"
        mock_httpx.post.assert_called_once()
        call_kwargs = mock_httpx.post.call_args
        assert "chat/completions" in call_kwargs.args[0]

    def test_openai_compat_ocr_sends_base64_image(self, monkeypatch):
        """OpenAICompatProvider.ocr() encodes image as base64 and posts to vision endpoint."""
        monkeypatch.setenv("LARKSCOUT_LLM_PROVIDER", "openai")
        monkeypatch.setenv("LARKSCOUT_LLM_API_KEY", "sk-test")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "page text"}}]
        }
        mock_resp.raise_for_status = MagicMock()

        mock_httpx = MagicMock()
        mock_httpx.post.return_value = mock_resp

        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            p = get_provider()
            result = p.ocr(b"\x89PNG", page_num=2)

        assert result == "page text"
        payload = mock_httpx.post.call_args.kwargs["json"]
        messages = payload["messages"]
        assert any(
            isinstance(m.get("content"), list) for m in messages
        ), "Expected a multipart (list) content for vision"


# ── AC-4: provider caching ────────────────────────────────────────────────────

class TestProviderCaching:
    def test_same_instance_returned_on_repeated_calls(self, monkeypatch):
        monkeypatch.delenv("LARKSCOUT_LLM_PROVIDER", raising=False)
        p1 = get_provider()
        p2 = get_provider()
        assert p1 is p2

    def test_reset_clears_cache(self, monkeypatch):
        monkeypatch.delenv("LARKSCOUT_LLM_PROVIDER", raising=False)
        p1 = get_provider()
        reset_provider()
        p2 = get_provider()
        assert p1 is not p2


# ── AC-5: unknown provider raises ─────────────────────────────────────────────

class TestUnknownProvider:
    def test_unknown_provider_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("LARKSCOUT_LLM_PROVIDER", "anthropic")
        with pytest.raises(ValueError, match="anthropic"):
            get_provider()
