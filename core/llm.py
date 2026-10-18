"""Step 5 Preview + ASI1 model clients with fallback. Owner: Prem."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Type

import sentry_sdk
from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel

# Import aspan lazily to avoid circular init; observability may not be inited yet
def _aspan(op, desc, **kw):
    try:
        from core.observability import aspan
        return aspan(op, desc, **kw)
    except Exception:
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _noop(**_):
            yield None
        return _noop()

logger = logging.getLogger(__name__)

load_dotenv()

def _step5_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.environ.get("STEPFUN_API_KEY", ""),
        base_url=os.environ.get("STEPFUN_BASE_URL", "https://api.stepfun.ai/v1"),
        max_retries=0,  # we control retries in plan(); SDK retries cause 3x timeout stacking
    )

def _asi1_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.environ.get("ASI_ONE_API_KEY", ""),
        base_url="https://api.asi1.ai/v1",
        max_retries=0,  # fast() is latency-sensitive; no SDK-level retries
    )

STEP_MODEL = os.environ.get("STEP_MODEL", "step-5-preview")
ASI1_MODEL = os.environ.get("ASI_ONE_MODEL", "asi1-mini")


def _first_json(text: str) -> Any:
    """Extract the first JSON object or array from a string."""
    match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if not match:
        raise ValueError(f"No JSON found in: {text[:200]}")
    return json.loads(match.group(1))


async def _call(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    *,
    schema: Type[BaseModel] | None,
    images: list[str] | None,
    timeout: float,
) -> Any:
    """Single model call; returns parsed pydantic object if schema given, else string."""
    # Attach images as image_url parts to the last user message (Step 5 only)
    if images:
        last = messages[-1]
        content: list[dict] = [{"type": "text", "text": last["content"]}]
        for url in images:
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages = messages[:-1] + [{"role": last["role"], "content": content}]

    kwargs: dict[str, Any] = {"model": model, "messages": messages, "timeout": timeout}

    if schema:
        try:
            kwargs["response_format"] = {"type": "json_object"}
            resp = await client.chat.completions.create(**kwargs)
            raw = resp.choices[0].message.content or ""
            return schema.model_validate(_first_json(raw))
        except Exception:
            # response_format rejected or parse failed — instruct JSON in prompt
            kwargs.pop("response_format", None)
            system_inject = (
                f"\nRespond with a single JSON object matching this schema: "
                f"{schema.model_json_schema()}"
            )
            msgs = list(messages)
            if msgs and msgs[0]["role"] == "system":
                msgs[0] = {"role": "system", "content": msgs[0]["content"] + system_inject}
            else:
                msgs = [{"role": "system", "content": system_inject}] + msgs
            kwargs["messages"] = msgs
            resp = await client.chat.completions.create(**kwargs)
            raw = resp.choices[0].message.content or ""
            return schema.model_validate(_first_json(raw))

    resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def plan(
    messages: list[dict],
    *,
    schema: Type[BaseModel] | None = None,
    images: list[str] | None = None,
    timeout: float = 30,
) -> tuple[Any, str]:
    """Step 5 first; on any failure retry once, then fall back to ASI1.

    Returns (result, model_used) where model_used is 'step5' or 'asi1'.
    """
    for attempt in range(2):
        try:
            t0 = time.monotonic()
            async with _aspan("llm.plan", STEP_MODEL, attempt=attempt) as s:
                result = await _call(_step5_client(), STEP_MODEL, messages,
                                     schema=schema, images=images, timeout=timeout)
                if s:
                    s.set_data("model", "step5")
            return result, "step5"
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            _warn = f"step5 attempt {attempt + 1} failed after {latency_ms}ms: {exc}"
            logger.warning(_warn)
            if attempt == 0:
                continue  # retry once
            break

    # Fall back to ASI1 (no images support)
    t0 = time.monotonic()
    try:
        async with _aspan("llm.plan.fallback", ASI1_MODEL) as s:
            result = await _call(_asi1_client(), ASI1_MODEL, messages,
                                 schema=schema, images=None, timeout=timeout)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if s:
                s.set_data("model", "asi1")
                s.set_data("latency_ms", latency_ms)
        logger.warning("plan() fell back to asi1 after step5 failures (%dms)", latency_ms)
        return result, "asi1"
    except Exception as exc:
        raise RuntimeError(f"Both Step5 and ASI1 failed: {exc}") from exc


async def fast(
    messages: list[dict],
    *,
    schema: Type[BaseModel] | None = None,
    timeout: float = 10,
) -> Any:
    """ASI1 only — fast intent classification and card copy."""
    async with _aspan("llm.fast", ASI1_MODEL):
        return await _call(_asi1_client(), ASI1_MODEL, messages,
                           schema=schema, images=None, timeout=timeout)


if __name__ == "__main__":
    import asyncio
    from pydantic import Field

    class Joke(BaseModel):
        setup: str
        punchline: str

    async def _demo() -> None:
        # plan() — Step 5 with schema
        msgs = [{"role": "user", "content": "Tell me a short programmer joke as JSON."}]
        result, model = await plan(msgs, schema=Joke)
        print(f"plan() [{model}]: {result}")

        # fast() — ASI1 only
        result2 = await fast([{"role": "user", "content": "What is 2+2? Reply in one word."}])
        print(f"fast() [asi1]: {result2}")

        # Fallback test — set STEPFUN_API_KEY=bad to trigger
        if os.environ.get("STEPFUN_API_KEY") == "bad":
            print("\nFallback test: Step5 key is bad, should fall back to ASI1")
            result3, model3 = await plan([{"role": "user", "content": "Say hello."}])
            print(f"plan() fallback [{model3}]: {result3}")

    asyncio.run(_demo())
