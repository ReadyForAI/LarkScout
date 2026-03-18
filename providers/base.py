"""Abstract base class for LLM providers."""

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Unified interface for LLM backends (summarisation + OCR).

    Implementations must override both ``summarize`` and ``ocr``.
    All concrete providers are expected to handle retries internally.
    """

    @abstractmethod
    def summarize(self, text: str, prompt: str, max_retries: int = 2) -> str:
        """Generate a text summary.

        Args:
            text:        The source text to summarise.
            prompt:      System/user prompt that instructs the model.
            max_retries: Number of retries on transient errors.

        Returns:
            The generated summary string.
        """

    @abstractmethod
    def ocr(self, image_bytes: bytes, page_num: int) -> str:
        """Extract text from a page image via vision.

        Args:
            image_bytes: Raw image bytes (PNG/JPEG).
            page_num:    1-based page number (used only for logging).

        Returns:
            Extracted text string.
        """
