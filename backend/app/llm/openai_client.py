"""Single entry point for OpenAI calls (spec §5.1). OpenAI is the only model provider.

Roles map to models and reasoning effort from settings:
  answer -> ANSWER_MODEL / ANSWER_EFFORT      (answer generation, repair)
  fast   -> FAST_MODEL / FAST_EFFORT          (query analysis, rerank, extraction, translation)
  vision -> VISION_MODEL / high               (OCR)
  judge  -> JUDGE_MODEL / high                (eval)

Reasoning models reject temperature/top_p, so they are never sent. Astra and Sol don't accept
effort 'none'; it is mapped to 'low' for them.
"""

from dataclasses import dataclass, field
from functools import lru_cache
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)

NO_NONE_EFFORT_PREFIXES = ("gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol")


class LLMNotConfigured(RuntimeError):
    pass


@dataclass
class Usage:
    calls: list[dict] = field(default_factory=list)

    def add(self, model: str, effort: str, usage) -> None:
        if usage is None:
            return
        details = getattr(usage, "input_tokens_details", None)
        out_details = getattr(usage, "output_tokens_details", None)
        self.calls.append(
            {
                "model": model,
                "effort": effort,
                "input": getattr(usage, "input_tokens", 0),
                "cached": getattr(details, "cached_tokens", 0) if details else 0,
                "output": getattr(usage, "output_tokens", 0),
                "reasoning": getattr(out_details, "reasoning_tokens", 0) if out_details else 0,
            }
        )


def _retryable():
    from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

    return retry(
        retry=retry_if_exception_type((RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)),
        wait=wait_exponential(min=1, max=20),
        stop=stop_after_attempt(5),
        reraise=True,
    )


class LLM:
    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise LLMNotConfigured("OPENAI_API_KEY is not set in .env")
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key)

    def _model_effort(self, role: str, effort: str | None) -> tuple[str, str]:
        s = self.settings
        model, default_effort = {
            "answer": (s.answer_model, s.answer_effort),
            "escalate": (s.answer_escalate_model, "high"),
            "fast": (s.fast_model, s.fast_effort),
            "vision": (s.vision_model, "high"),
            "judge": (s.judge_model, "high"),
        }[role]
        effort = effort or default_effort
        if effort == "none" and model.startswith(NO_NONE_EFFORT_PREFIXES):
            effort = "low"
        return model, effort

    def structured(
        self,
        role: str,
        system: str,
        user: str,
        schema: type[T],
        effort: str | None = None,
        usage: Usage | None = None,
    ) -> T:
        """One Responses API call with a strict JSON schema; returns the parsed Pydantic object."""
        model, effort = self._model_effort(role, effort)

        @_retryable()
        def _call():
            return self.client.responses.parse(
                model=model,
                reasoning={"effort": effort},
                input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                text_format=schema,
            )

        response = _call()
        if usage is not None:
            usage.add(model, effort, response.usage)
        return response.output_parsed

    def vision_transcribe(self, prompt: str, image_b64: str, usage: Usage | None = None) -> str:
        model, effort = self._model_effort("vision", None)

        @_retryable()
        def _call():
            return self.client.responses.create(
                model=model,
                reasoning={"effort": effort},
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"},
                        ],
                    }
                ],
            )

        response = _call()
        if usage is not None:
            usage.add(model, effort, response.usage)
        return response.output_text

    def embed(self, texts: list[str]) -> list[list[float]]:
        @_retryable()
        def _call(batch: list[str]):
            return self.client.embeddings.create(
                model=self.settings.embed_model, input=batch, dimensions=self.settings.embed_dims
            )

        vectors: list[list[float]] = []
        for i in range(0, len(texts), 100):
            response = _call(texts[i : i + 100])
            vectors.extend(item.embedding for item in response.data)
        return vectors


@lru_cache
def get_llm() -> LLM:
    return LLM(get_settings())


def llm_available() -> bool:
    return bool(get_settings().openai_api_key)
