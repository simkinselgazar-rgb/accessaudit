"""The ONE LLM client for the entire WCAG Trusted Tester system.

No other module makes raw chat/completions calls. Every AI request in the
application -- visual AI, code AI, the judge, Phase 2 explorer, video
descriptions, crawl page selection, verification -- goes through LLMClient.

Responsibilities:
- Model routing by content type (text / images / video / audio)
- Global async lock (one LLM request in flight at a time)
- Rate limiting (RPM for cloud APIs, queue-depth polling for local vLLM)
- Retry with exponential backoff
- 413 payload-too-large batch splitting
- Fallback to a secondary model/endpoint on total failure
- Response parsing via functions.parser.parse_tool_response
"""
from __future__ import annotations

import asyncio
import threading
import json
import logging
import random
import time
from typing import Any

import httpx

from functions.rate_limit import _TokenBucket
from functions.media import encode_image, encode_video
from functions.parser import (
    describe_response_shape,
    get_content_text,
    parse_ai_response,
    parse_tool_response,
)

logger = logging.getLogger(__name__)


def _describe_parse_path(raw: dict[str, Any]) -> str:
    """Module-level alias so call_with_tools logging stays compact."""
    return describe_response_shape(raw)


_LLM_TRANSCRIPT_DIR = "llm_transcripts"
_llm_call_counter = 0
_llm_call_counter_lock = threading.Lock()


def _flag_evidence_issues(tool_name: str, result: dict[str, Any]) -> None:
    """Log prominently when a model response flags evidence problems.

    Two distinct situations:
    1. insufficient_evidence_reason — the model didn't have enough data.
       Action: we need to provide more information in the prompt/capture.
    2. conflicting_information — the data sources contradict each other.
       Action: we need to investigate our prompts/data for issues.
    """
    insuff = result.get("insufficient_evidence_reason")
    conflict = result.get("conflicting_information")
    if insuff:
        logger.warning(
            "EVIDENCE[%s] INSUFFICIENT DATA — model says: %s",
            tool_name, insuff,
        )
    if conflict:
        logger.warning(
            "EVIDENCE[%s] CONFLICTING DATA — model says: %s",
            tool_name, conflict,
        )


_SCHEMA_TYPE_TO_PY: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def _validate_tool_args_shape(
    result: dict[str, Any] | None,
    tool_schema: dict | None,
) -> list[str]:
    """Lightweight shape check against a tool's declared JSONSchema.

    Returns a list of violation strings. Empty list means the args
    conform. Non-empty means the cascade should retry with a
    corrective note -- typically catches cases where the model
    double-encoded an array as a JSON string, or vice versa.

    Deliberately top-level only: walks required fields and each
    declared property's ``type``, compares against the Python value.
    No recursive item-level checks -- a JSONSchema validator would
    do that, but pulling jsonschema in as a runtime dep for one
    callsite is overkill. The most common bug in practice is a list
    field being sent as a string, which this check catches.

    ``type`` in JSONSchema can be a string or an array of strings;
    both are handled.
    """
    if not isinstance(result, dict) or not isinstance(tool_schema, dict):
        return []
    fn = tool_schema.get("function") if "function" in tool_schema else tool_schema
    if not isinstance(fn, dict):
        return []
    params = fn.get("parameters") if "parameters" in fn else tool_schema.get("parameters")
    if not isinstance(params, dict):
        return []
    properties = params.get("properties") or {}
    required = set(params.get("required") or [])
    errors: list[str] = []

    for key in required:
        if key not in result:
            errors.append(f"missing required field '{key}'")

    for key, spec in properties.items():
        if key not in result:
            continue
        if not isinstance(spec, dict):
            continue
        declared_type = spec.get("type")
        if declared_type is None:
            continue
        allowed_types: tuple[type, ...] = ()
        declared_types = (
            declared_type if isinstance(declared_type, list) else [declared_type]
        )
        for dt in declared_types:
            allowed_types += _SCHEMA_TYPE_TO_PY.get(str(dt), ())
        if not allowed_types:
            continue
        value = result[key]
        # JSON numbers are int OR float; booleans are a subtype of int
        # in Python, so when the schema says ``integer`` we should
        # reject plain True/False. Special-case that common pitfall.
        if "integer" in declared_types and isinstance(value, bool):
            errors.append(f"field '{key}' is a boolean but schema requires integer")
            continue
        if not isinstance(value, allowed_types):
            errors.append(
                f"field '{key}' has type {type(value).__name__} "
                f"but schema requires {'/'.join(declared_types)}"
            )
    return errors


def _describe_message_shape(payload: dict[str, Any]) -> str:
    """One-line summary of a request payload's message structure, safe to
    paste into a log line. Enumerates content-part types per message
    (text/image_url/video_url/etc), prompt chars, and media byte sizes,
    so an HTTP 500 log line shows exactly what was sent WITHOUT dumping
    base64 into the log. The full payload still lands on disk via
    ``_save_llm_exchange``.
    """
    out_parts: list[str] = []
    for msg in payload.get("messages", []) or []:
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str):
            out_parts.append(f"{role}(text={len(content)}c)")
            continue
        if not isinstance(content, list):
            out_parts.append(f"{role}(?={type(content).__name__})")
            continue
        counts: dict[str, int] = {}
        sizes: dict[str, int] = {}
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type", "?"))
            counts[ptype] = counts.get(ptype, 0) + 1
            if ptype == "text":
                sizes["text"] = sizes.get("text", 0) + len(part.get("text", ""))
            elif ptype in ("image_url", "video_url"):
                blob = part.get(ptype, {})
                url = blob.get("url", "") if isinstance(blob, dict) else ""
                sizes[ptype] = sizes.get(ptype, 0) + (len(url) if isinstance(url, str) else 0)
        parts = []
        for ptype, n in sorted(counts.items()):
            if ptype == "text":
                parts.append(f"text={n}({sizes.get('text', 0)}c)")
            elif ptype in ("image_url", "video_url"):
                parts.append(f"{ptype}={n}({sizes.get(ptype, 0)}b64c)")
            else:
                parts.append(f"{ptype}={n}")
        out_parts.append(f"{role}({', '.join(parts)})")
    tool_names: list[str] = []
    for t in payload.get("tools") or []:
        try:
            tool_names.append(t.get("function", {}).get("name", "?"))
        except Exception:
            # best-effort summary helper; skip malformed tool entries
            pass
    tail = f"; tools=[{', '.join(tool_names)}]" if tool_names else ""
    return "; ".join(out_parts) + tail


def _extract_text_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a sidecar that lists the text prompts + image/video file
    references from a payload, separated from the bulky base64 blobs.

    The full payload (including base64) is still saved to disk verbatim;
    this sidecar just makes it cheap to read the prompts at a glance
    without scrolling past megabytes of base64. Returns a dict with:

    - ``system_prompt``: the system message text (full, no truncation)
    - ``user_text``: the text portion(s) of the user message (full)
    - ``image_count`` / ``video_count``: counts of attached media
    - ``image_byte_sizes``: per-attachment base64 byte counts so a reader
      can see at a glance how big each image/video was without opening
      the full file
    """
    summary: dict[str, Any] = {
        "system_prompt": "",
        "user_text": "",
        "image_count": 0,
        "video_count": 0,
        "image_byte_sizes": [],
    }
    for msg in payload.get("messages", []) or []:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            if role == "system":
                summary["system_prompt"] = content
            elif role == "user":
                summary["user_text"] = content
            continue
        if not isinstance(content, list):
            continue
        text_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                text_parts.append(part.get("text", ""))
            elif ptype == "image_url":
                summary["image_count"] += 1
                blob = part.get("image_url", {})
                if isinstance(blob.get("url"), str):
                    summary["image_byte_sizes"].append(len(blob["url"]))
            elif ptype == "video_url":
                summary["video_count"] += 1
                blob = part.get("video_url", {})
                if isinstance(blob.get("url"), str):
                    summary["image_byte_sizes"].append(len(blob["url"]))
        if role == "user" and text_parts:
            summary["user_text"] = "\n".join(text_parts)
        elif role == "system" and text_parts:
            summary["system_prompt"] = "\n".join(text_parts)
    return summary


def _save_llm_exchange(
    *,
    request_payload: dict[str, Any],
    raw_response: dict[str, Any] | None,
    target_url: str,
    label: str,
    error: BaseException | None = None,
) -> str | None:
    """Persist an LLM request/response pair to disk for forensic review.

    Every ``LLMClient.call()`` invocation goes through here. Files land in
    ``<latest_review_dir>/llm_transcripts/<seq>_<label>.json`` so we can
    audit exactly what every model saw and replied with -- prompts,
    parameters, retries, restructure calls, and responses.

    Nothing is truncated. Text prompts go in verbatim. Base64 image and
    video data goes in verbatim. Responses go in verbatim. Disk cost is
    real but visibility into model behavior is more important.

    A small ``summary`` field at the top of each file lists the text
    prompts and media counts so a human reader can see what was sent
    without scrolling past megabytes of base64.

    When ``error`` is passed, the exchange is saved with the exception
    type and message in an ``error`` field, the response slot is null,
    and the filename is suffixed ``_ERROR``. This guarantees we always
    have the prompt on disk even when the call never produced a reply.

    Returns the path written, or None if saving failed.
    """
    global _llm_call_counter
    try:
        import json as _json
        import os as _os

        base_dir = _os.environ.get("WCAG_LLM_TRANSCRIPT_DIR")
        if not base_dir:
            cwd = _os.getcwd()
            reviews_dir = _os.path.join(cwd, "reviews")
            if _os.path.isdir(reviews_dir):
                review_dirs = [
                    _os.path.join(reviews_dir, d)
                    for d in _os.listdir(reviews_dir)
                    if _os.path.isdir(_os.path.join(reviews_dir, d))
                ]
                if review_dirs:
                    base_dir = max(review_dirs, key=_os.path.getmtime)
            if not base_dir:
                base_dir = cwd

        out_dir = _os.path.join(base_dir, _LLM_TRANSCRIPT_DIR)
        _os.makedirs(out_dir, exist_ok=True)

        with _llm_call_counter_lock:
            _llm_call_counter += 1
            n = _llm_call_counter
        safe_label = "".join(
            c if c.isalnum() or c in "_-" else "_" for c in label
        )[:60]
        suffix = "_ERROR" if error is not None else ""
        path = _os.path.join(out_dir, f"{n:05d}_{safe_label}{suffix}.json")

        record: dict[str, Any] = {
            "seq": n,
            "label": label,
            "target_url": target_url,
            "summary": _extract_text_summary(request_payload),
            "request": request_payload,
            "response": raw_response,
        }
        if error is not None:
            record["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            # LLMError carries response_body and status_code
            if hasattr(error, "status_code"):
                record["error"]["status_code"] = getattr(error, "status_code", None)
            if hasattr(error, "response_body"):
                record["error"]["response_body"] = getattr(error, "response_body", "")

        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(record, fh, indent=2, ensure_ascii=False, default=str)
        return path
    except Exception:
        # The transcript guarantee is load-bearing (CLAUDE.md): a missing
        # transcript is the difference between "page was clean" and "the call
        # failed and we never noticed". A save failure must be visible.
        logger.warning("Could not save LLM exchange for %s", label, exc_info=True)
        return None


class LLMError(Exception):
    """Raised when an LLM call fails after all retries."""

    def __init__(self, message: str, status_code: int | None = None, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


_CLOUD_HOSTS: tuple[str, ...] = (
    "googleapis.com",
    "openai.com",
    "openrouter.ai",
    "anthropic.com",
)


def _is_cloud_url(url: str) -> bool:
    return any(h in url for h in _CLOUD_HOSTS)


def _is_local_url(url: str) -> bool:
    return "localhost" in url or "127.0.0.1" in url


class LLMClient:
    """Unified async LLM client. Every parameter can be overridden per-call."""

    # Process-wide in-flight gate. Serves BOTH modes:
    #   AI_MAX_CONCURRENT = 1  -> acts exactly like asyncio.Lock (serial)
    #   AI_MAX_CONCURRENT = N  -> allows N concurrent LLM calls in flight
    # Default in config.py is 1 for all backends, so the semaphore is a
    # drop-in replacement for the old asyncio.Lock until you opt in to
    # concurrent mode by setting ``ai_max_concurrent`` in settings.json.
    _global_sem: asyncio.Semaphore | None = None
    _global_bucket: _TokenBucket | None = None
    _last_call_time: float = 0.0

    def __init__(
        self,
        *,
        base_url: str | None = None,
        vision_url: str | None = None,
        model: str | None = None,
        vision_model: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        max_tokens: int | None = None,
        rpm: int | None = None,
    ) -> None:
        from config import (
            AI_API_BASE_URL,
            AI_API_KEY,
            AI_BACKEND,
            AI_MAX_RETRIES,
            AI_MAX_TOKENS,
            AI_MODEL,
            AI_RPM,
            AI_TIMEOUT,
            AI_VISION_API_URL,
            AI_VISION_MODEL,
        )

        self.backend = AI_BACKEND
        self.base_url = (base_url or AI_API_BASE_URL).rstrip("/")
        # An explicit base_url override must not leak to the vision endpoint.
        if base_url and not vision_url:
            self.vision_url = self.base_url
        else:
            self.vision_url = (vision_url or AI_VISION_API_URL).rstrip("/")
        self.model = model or AI_MODEL
        self.vision_model = vision_model or AI_VISION_MODEL
        self.api_key = api_key if api_key is not None else AI_API_KEY
        self.timeout = timeout or AI_TIMEOUT
        self.max_retries = max_retries or AI_MAX_RETRIES
        self.max_tokens = max_tokens or AI_MAX_TOKENS
        effective_rpm = rpm or AI_RPM
        self.min_delay = 60.0 / effective_rpm if effective_rpm and effective_rpm > 0 else 1.0

    # ── Public API ───────────────────────────────────────────────────────────

    async def call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: list[str] | None = None,
        video: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        model_override: str | None = None,
        endpoint_override: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        needs_audio: bool = False,
        timeout: float | None = None,
        max_retries: int | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Make an LLM API call and return the raw response dict.

        The caller must parse the response (typically by using
        ``functions.parser.parse_tool_response``). For a one-shot
        call-plus-parse, use :meth:`call_with_tools` instead.

        ``label`` overrides the transcript filename suffix. When the
        call uses a ``tools`` schema, the tool's function name is used
        automatically. For tool-less direct chat-completion calls
        (e.g. video description, audio probe), pass an explicit
        ``label`` so the saved transcript is named after its purpose
        rather than the generic ``call``.
        """
        has_images = bool(images)
        has_video = bool(video)

        chosen_model, target_url = self._select_model(
            has_images=has_images,
            has_video=has_video,
            needs_audio=needs_audio,
            model_override=model_override,
            endpoint_override=endpoint_override,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": self._build_user_content(user_prompt, images, video, has_images, has_video),
            },
        ]

        payload = self._build_payload(
            chosen_model,
            messages,
            tools,
            tool_choice,
            temperature,
            max_tokens or self.max_tokens,
            has_video,
            target_url=target_url,
        )

        # Resolve the label up front so it is available even if the call
        # raises -- we still want to save the prompt that triggered the
        # failure so the user can see what was being sent. Caller-passed
        # label wins; otherwise derive from tool schema; otherwise fall
        # back to "call" (rare — only for legacy callers that don't tag).
        if label is None:
            label = "call"
            if tools:
                for t in tools:
                    fn_name = t.get("function", {}).get("name") if isinstance(t, dict) else None
                    if fn_name:
                        label = fn_name
                        break

        response: dict[str, Any] | None = None
        error: BaseException | None = None
        try:
            async with self._get_lock():
                response = await self._execute_with_retry(
                    payload,
                    target_url,
                    images,
                    video,
                    user_prompt,
                    tools,
                    tool_choice,
                    temperature,
                    max_tokens or self.max_tokens,
                    max_retries or self.max_retries,
                    timeout or self.timeout,
                )
        except BaseException as exc:
            error = exc
            raise
        finally:
            # ALWAYS save the exchange, even on error. Full prompts, full
            # base64 media, full response (or error). No truncation. The
            # save is in a finally so a network failure or LLMError still
            # writes the prompt to disk -- the user must always be able
            # to see what was sent.
            _save_llm_exchange(
                request_payload=payload,
                raw_response=response,
                target_url=target_url,
                label=label,
                error=error,
            )

        return response

    async def call_with_tools(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_schema: dict,
        images: list[str] | None = None,
        video: str | None = None,
        temperature: float = 0.2,
        needs_audio: bool = False,
        model_override: str | None = None,
        endpoint_override: str | None = None,
        max_tool_attempts: int = 3,
        restructure_on_failure: bool = True,
    ) -> dict[str, Any] | None:
        """Make an LLM call with a tool schema and return the parsed arguments.

        Strategy (every structured-output call in the system uses this):
        1. Make an LLM call with ``tool_choice`` forcing the named tool.
        2. Try to parse the response as a tool call (handles OpenAI native
           format AND every malformed Gemma/Qwen variant via the parser's
           state-machine recovery in ``loose_json_loads``).
        3. If parsing fails, retry the SAME call up to ``max_tool_attempts``
           times. Each retry is a clean re-send of the original prompt --
           the model is never told it is retrying. Sampling temperature
           gives a different reply each time, so this often succeeds when
           the first attempt was a one-off prose hiccup.
        4. If every attempt still fails AND the LAST response has prose
           content, send that prose to the structuring model with the tool
           schema and a 'convert this into a tool call' instruction. We use
           prose from the actual response we observed, not a second
           free-form call (which would be non-deterministic relative to the
           response we are trying to recover).

        ``restructure_on_failure=False`` skips step 4 (used internally by
        the restructuring call itself to avoid infinite recursion).
        """
        tool_choice = self._tool_choice_for_backend(tool_name)
        chosen_model, chosen_url = self._select_model(
            has_images=bool(images),
            has_video=bool(video),
            needs_audio=needs_audio,
            model_override=model_override,
            endpoint_override=endpoint_override,
        )

        logger.info(
            "TOOLCALL[%s] start: model=%s endpoint=%s images=%d video=%s prompt_chars=%d max_attempts=%d",
            tool_name,
            chosen_model.split("/")[-1],
            chosen_url,
            len(images or []),
            "yes" if video else "no",
            len(user_prompt),
            max_tool_attempts,
        )

        # Warn when a prompt is uncomfortably large. Gemini 3.0 Flash
        # handles ~1M tokens (~4MB text), but if we're approaching that
        # the caller should consider chunking via functions.chunker.
        # A bare warning is enough -- the cascade still tries the call,
        # a 413 kicks in batch-split for images, and the user gets to
        # see in the log which criterion+page blew the budget.
        system_chars = len(system_prompt or "")
        user_chars = len(user_prompt)
        total_chars = system_chars + user_chars
        if total_chars > 800_000:
            logger.warning(
                "TOOLCALL[%s] oversized prompt: system=%d + user=%d = %d chars "
                "(~%.1fK tokens) -- approaching context limits; consider "
                "chunking via functions.chunker",
                tool_name, system_chars, user_chars, total_chars, total_chars / 4000,
            )

        last_content = ""
        for attempt in range(1, max_tool_attempts + 1):
            # On retries we keep the original system + user prompt unchanged
            # but append a short corrective note showing the model what it
            # just replied with and demanding a tool call. This works much
            # better than a blind resend because the model sees its own
            # malformed output and has explicit guidance to fix it.
            attempt_user_prompt = user_prompt
            if attempt > 1 and last_content:
                attempt_user_prompt = (
                    f"{user_prompt}\n\n"
                    f"--- PREVIOUS REPLY (rejected) ---\n"
                    f"{last_content}\n"
                    f"--- END PREVIOUS REPLY ---\n\n"
                    f"That reply was not a valid tool call. You MUST respond "
                    f"by calling the {tool_name} tool with the correct "
                    f"argument schema. Do not produce any prose. The tool "
                    f"call is your entire response."
                )
                logger.info(
                    "TOOLCALL[%s] attempt %d/%d: retrying with corrective note (prev reply was %d chars)",
                    tool_name, attempt, max_tool_attempts, len(last_content),
                )
            else:
                logger.info(
                    "TOOLCALL[%s] attempt %d/%d: sending request",
                    tool_name, attempt, max_tool_attempts,
                )

            raw = await self.call(
                system_prompt=system_prompt,
                user_prompt=attempt_user_prompt,
                images=images,
                video=video,
                tools=[tool_schema],
                tool_choice=tool_choice,
                temperature=temperature,
                needs_audio=needs_audio,
                model_override=model_override,
                endpoint_override=endpoint_override,
            )

            result = parse_tool_response(raw, tool_name)
            if result is not None:
                # Schema-shape validation: the parse succeeded (model
                # made a tool call and args were valid JSON), but the
                # args may still disagree with the tool's declared
                # schema -- e.g. an array field returned as a string,
                # an object field returned as a number. When this
                # happens, treat it like a parse failure so the retry
                # cascade and corrective-note path run instead of
                # silently accepting wrong-shape data.
                schema_errors = _validate_tool_args_shape(result, tool_schema)
                if schema_errors:
                    logger.warning(
                        "TOOLCALL[%s] attempt %d/%d: tool call parsed but "
                        "args shape does not match schema -- treating as "
                        "retryable error. Violations: %s",
                        tool_name, attempt, max_tool_attempts,
                        "; ".join(schema_errors),
                    )
                    # Synthesize a "previous reply" string so the
                    # retry's corrective note can include it. Prefer
                    # the raw args JSON over a canned string so the
                    # model sees exactly what it emitted.
                    import json as _json_mod
                    try:
                        last_content = _json_mod.dumps(result)
                    except Exception:
                        last_content = str(result)
                    # Append a schema-hint so retry 2/3 knows what
                    # specifically to fix.
                    last_content += (
                        "\n\n[schema violations: " + "; ".join(schema_errors) + "]"
                    )
                else:
                    logger.info(
                        "TOOLCALL[%s] PARSED on attempt %d/%d (path=%s)",
                        tool_name, attempt, max_tool_attempts,
                        _describe_parse_path(raw),
                    )
                    _flag_evidence_issues(tool_name, result)
                    return result
            else:
                last_content = get_content_text(raw)

            # Gemini-specific fast-fail: when the server rejects its own
            # function call with ``finish_reason: function_call_filter:
            # MALFORMED_FUNCTION_CALL``, immediate retries with the same
            # inputs spend ~2 minutes per attempt while Gemini internally
            # spins before giving up. Fresh sampling after a delay (from
            # the outer caller's retry loop, e.g. Phase 2 explorer's
            # "Retrying AI after 5s" path) has a much higher success rate.
            # Break out of the cascade immediately and return None so the
            # caller can re-invoke with a new sampling seed.
            finish_reason = ""
            try:
                finish_reason = (raw.get("choices") or [{}])[0].get("finish_reason", "") or ""
            except Exception:
                finish_reason = ""
            if "MALFORMED_FUNCTION_CALL" in finish_reason:
                logger.warning(
                    "TOOLCALL[%s] attempt %d/%d: Gemini rejected its own tool call "
                    "(finish_reason=%r). This is a Gemini-side schema-validation "
                    "bug, not a parse error -- fresh sampling after a delay usually "
                    "fixes it. Breaking out of cascade early; caller should retry.",
                    tool_name, attempt, max_tool_attempts, finish_reason,
                )
                return None

            logger.warning(
                "TOOLCALL[%s] attempt %d/%d FAILED to parse (%d chars of prose) "
                "-- request+response saved to llm_transcripts/",
                tool_name, attempt, max_tool_attempts, len(last_content),
            )

        if not last_content:
            logger.warning(
                "TOOLCALL[%s] gave up: empty response across %d attempts",
                tool_name, max_tool_attempts,
            )
            return None

        if not restructure_on_failure:
            logger.warning(
                "TOOLCALL[%s] gave up after %d attempts; restructure disabled",
                tool_name, max_tool_attempts,
            )
            return None

        logger.warning(
            "TOOLCALL[%s] all %d direct attempts failed -- routing %d chars of prose to LLM restructurer",
            tool_name, max_tool_attempts, len(last_content),
        )

        return await self._restructure_prose_into_tool_call(
            tool_name=tool_name,
            tool_schema=tool_schema,
            original_user_prompt=user_prompt,
            prose=last_content,
        )

    async def _restructure_prose_into_tool_call(
        self,
        *,
        tool_name: str,
        tool_schema: dict,
        original_user_prompt: str,
        prose: str,
    ) -> dict[str, Any] | None:
        """Send rejected prose back to the text model with the tool schema.

        Used when ``call_with_tools`` cannot parse the model's first reply.
        Routes to AI_FALLBACK_URL/MODEL when configured (so a strong text
        model like Qwen 35B handles structuring even if the original call
        went to a multimodal vision model that ignores tool_choice). Falls
        back to the primary model if no fallback is configured.

        The restructuring call passes ``restructure_on_failure=False`` to
        ``call_with_tools`` so a parse failure here cannot recurse.
        """
        from config import AI_FALLBACK_MODEL, AI_FALLBACK_URL

        target_model = AI_FALLBACK_MODEL or None
        target_url = AI_FALLBACK_URL or None

        logger.info(
            "RESTRUCTURE[%s] start: routing %d chars of prose to model=%s endpoint=%s",
            tool_name,
            len(prose),
            (target_model or self.model).split("/")[-1],
            target_url or self.base_url,
        )

        system_prompt = (
            "You are converting another model's prose analysis into a "
            "structured tool call. Read the analysis below and call the "
            f"{tool_name} tool with the data the analysis contains. Do not "
            "add commentary, do not invent fields, do not summarize -- just "
            "transcribe the analysis into the tool's argument schema. The "
            "tool call is your entire response."
        )
        user_prompt = (
            "ORIGINAL TASK GIVEN TO THE OTHER MODEL:\n"
            f"{original_user_prompt}\n\n"
            "THE OTHER MODEL'S PROSE REPLY (to be converted to a tool call):\n"
            f"{prose}\n\n"
            f"Now call the {tool_name} tool with the equivalent structured data."
        )

        from functions.bypass_log import (
            CATEGORY_FALLBACK_MODEL, SEVERITY_HIGH, log_bypass,
        )

        try:
            result = await self.call_with_tools(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tool_name=tool_name,
                tool_schema=tool_schema,
                temperature=0.0,
                model_override=target_model,
                endpoint_override=target_url,
                restructure_on_failure=False,
            )
        except Exception as exc:
            logger.warning(
                "RESTRUCTURE[%s] threw %s: %s",
                tool_name, type(exc).__name__, exc,
            )
            log_bypass(
                category=CATEGORY_FALLBACK_MODEL,
                severity=SEVERITY_HIGH,
                source="functions/llm.py:_restructure_prose_into_tool_call",
                event="restructure_exception",
                details={
                    "tool_name": tool_name,
                    "restructure_model": target_model or self.model,
                    "restructure_url": target_url or self.base_url,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                    "prose_chars": len(prose),
                },
                outcome="prose-to-tool restructure raised; caller receives None",
                data_lost=True,
            )
            return None

        if result is not None:
            logger.info("RESTRUCTURE[%s] SUCCEEDED", tool_name)
        else:
            logger.warning(
                "RESTRUCTURE[%s] FAILED to produce a tool call -- caller will see None",
                tool_name,
            )
            log_bypass(
                category=CATEGORY_FALLBACK_MODEL,
                severity=SEVERITY_HIGH,
                source="functions/llm.py:_restructure_prose_into_tool_call",
                event="restructure_returned_none",
                details={
                    "tool_name": tool_name,
                    "restructure_model": target_model or self.model,
                    "restructure_url": target_url or self.base_url,
                    "prose_chars": len(prose),
                },
                outcome="prose-to-tool restructure could not parse; caller receives None",
                data_lost=True,
            )
        return result

    async def check_health(self) -> dict[str, Any]:
        """Ping the configured model endpoints and report availability."""
        result: dict[str, Any] = {
            "status": "error",
            "text_model": False,
            "vision_model": False,
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=30.0),
        ) as client:
            for label, url in (("text", self.base_url), ("vision", self.vision_url)):
                try:
                    resp = await client.get(f"{url}/models", headers=self._headers(url))
                    resp.raise_for_status()
                    result[f"{label}_model"] = True
                    result[f"{label}_models"] = resp.json()
                except Exception as exc:
                    logger.warning("%s model health check failed: %s", label.title(), exc)
        if result["text_model"] or result["vision_model"]:
            result["status"] = "ok"
        return result

    async def close(self) -> None:
        """Kept for backward compatibility; no persistent client to close."""
        return None

    # ── Internals: locking and rate limiting ─────────────────────────────────

    @classmethod
    def _get_lock(cls) -> asyncio.Semaphore:
        """Return the process-wide in-flight gate.

        Concurrency is controlled by ``config.AI_MAX_CONCURRENT``:
          - ``1`` (the default) → serial, identical to the prior
            ``asyncio.Lock`` behavior. Safe for local vLLM/mlx-vlm
            endpoints that crash under concurrent load.
          - ``N > 1`` → up to N concurrent LLM calls. Use this ONLY with
            cloud providers (Gemini/OpenAI/Anthropic/OpenRouter) that
            handle hundreds of concurrent requests easily. Local vLLM
            will OOM. Do NOT set this without reading the caveats in
            ARCHITECTURE.md section 8.5 Default Model Fleet.

        The semaphore is lazily created so the asyncio event loop exists
        by the time we touch it (avoids "no running event loop" errors
        at import time).
        """
        if cls._global_sem is None:
            from config import AI_MAX_CONCURRENT
            n = max(1, int(AI_MAX_CONCURRENT))
            cls._global_sem = asyncio.Semaphore(n)
            if n > 1:
                logger.info(
                    "LLMClient in-flight gate: AI_MAX_CONCURRENT=%d "
                    "(concurrent mode). Do NOT use with local vLLM.",
                    n,
                )
        return cls._global_sem

    def _headers(self, target_url: str | None = None) -> dict[str, str]:
        key = self._resolve_key_for_url(target_url) if target_url else self.api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _resolve_key_for_url(self, target_url: str) -> str:
        """Pick the API key that matches ``target_url``.

        Image-bearing calls are routed by ``_select_model`` to
        ``AI_LOCAL_JUDGE_URL`` regardless of ``self.base_url``. When
        ``AI_LOCAL_JUDGE_API_KEY`` is set in settings.json (i.e. the
        operator runs vision on a different provider than text), the
        Authorization header must carry that key, not ``self.api_key``.
        Same logic for ``AI_VIDEO_API_URL`` and ``AI_VISION_API_URL``.

        Match against the *configured* endpoint URLs from settings --
        never against ``self.vision_url``, which the constructor aliases
        to ``self.base_url`` when only ``base_url`` is passed. In a
        text-only client (e.g. the judge built with base_url=OpenRouter)
        that aliasing would otherwise make every OpenRouter call
        wrongly resolve to the vision key.
        """
        from config import (
            AI_LOCAL_JUDGE_API_KEY,
            AI_LOCAL_JUDGE_URL,
            AI_VIDEO_API_KEY,
            AI_VIDEO_API_URL,
            AI_VISION_API_KEY,
            AI_VISION_API_URL,
        )
        url = target_url.rstrip("/")
        if AI_LOCAL_JUDGE_URL and url == AI_LOCAL_JUDGE_URL.rstrip("/"):
            return AI_LOCAL_JUDGE_API_KEY or self.api_key
        if AI_VIDEO_API_URL and url == AI_VIDEO_API_URL.rstrip("/"):
            return AI_VIDEO_API_KEY or self.api_key
        if AI_VISION_API_URL and url == AI_VISION_API_URL.rstrip("/"):
            return AI_VISION_API_KEY or self.api_key
        return self.api_key

    def _tool_choice_for_backend(self, tool_name: str) -> Any:
        if self.backend == "gemini":
            return "required"
        return {"type": "function", "function": {"name": tool_name}}

    @classmethod
    def _get_bucket(cls) -> _TokenBucket:
        """Return the process-wide rate-limit token bucket.

        The bucket is sized to ``config.AI_RPM`` and shared across every
        ``LLMClient`` instance in the process. Safe under concurrent
        mode (``AI_MAX_CONCURRENT > 1``) because each ``acquire()`` call
        holds the bucket's own asyncio.Lock just long enough to
        debit/refill, then releases it while sleeping.
        """
        if cls._global_bucket is None:
            from config import AI_RPM
            cls._global_bucket = _TokenBucket(rpm=AI_RPM)
        return cls._global_bucket

    async def _wait_for_availability(self, target_url: str) -> None:
        """Gate the call on two conditions:

        1. Local models: wait until the vLLM queue depth drops to 0
           (so we don't stack requests on a model that can't parallelize).
        2. Global rate limit: debit the ``_TokenBucket`` sized to AI_RPM.
           Safe under concurrent mode.
        """
        if _is_local_url(target_url):
            health_url = target_url.rsplit("/v1", 1)[0] + "/health"
            for _ in range(60):  # up to ~5 min
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(3)) as hc:
                        resp = await hc.get(health_url)
                        if resp.status_code == 200:
                            queue = resp.json().get("queue_depth", 0)
                            if queue == 0:
                                break
                            # Jittered sleep prevents N concurrent tasks from
                            # all waking up on the same tick and hammering
                            # the queue together when it drains.
                            await asyncio.sleep(5 + random.uniform(0, 2))
                        else:
                            break
                except Exception:
                    break

        # Token-bucket RPM enforcement. No-op when AI_RPM <= 0.
        await LLMClient._get_bucket().acquire()
        # Legacy fallback: honor per-call min_delay as a hard floor for
        # serial mode only. Under concurrent mode this gets ignored
        # because the bucket is the real limiter.
        if self.min_delay > 0:
            now = time.monotonic()
            elapsed = now - LLMClient._last_call_time
            if elapsed < self.min_delay:
                await asyncio.sleep(self.min_delay - elapsed)
            LLMClient._last_call_time = time.monotonic()

    # ── Internals: model routing ─────────────────────────────────────────────

    def _select_model(
        self,
        *,
        has_images: bool,
        has_video: bool,
        needs_audio: bool,
        model_override: str | None,
        endpoint_override: str | None,
    ) -> tuple[str, str]:
        """Pick the model + endpoint for a call based on content type.

        Routing is purely driven by ``config`` settings (which come from
        ``settings.json`` and can be edited through the front-end
        settings page). No caller-side pinning magic.

        Priority order:
        1. Per-call ``model_override`` / ``endpoint_override``
        2. Audio-capable video → prefer local ``AI_EXPLORER_*`` (Gemma 4
           E4B at port 11804) because the local mlx-vlm server hosting
           E4B DOES process the audio track. Cloud ``AI_VIDEO_*`` is a
           secondary option for redundancy only.
        3. Any video → ``AI_VIDEO_*`` (cloud) then vision fallback.
        4. Any image → ``AI_LOCAL_JUDGE_*`` (Gemma 26B, accuracy).
        5. Text-only → this client's ``self.model`` / ``self.base_url``.
        """
        if model_override and endpoint_override:
            return model_override, endpoint_override.rstrip("/")
        if model_override:
            return model_override, self.base_url

        from config import (
            AI_API_BASE_URL,
            AI_BACKEND,
            AI_EXPLORER_MODEL,
            AI_EXPLORER_URL,
            AI_LOCAL_JUDGE_MODEL,
            AI_LOCAL_JUDGE_URL,
            AI_VIDEO_API_URL,
            AI_VIDEO_MODEL,
        )

        if has_video and needs_audio:
            # Audio-bearing video: prefer the local Gemma 4 E4B (AI_EXPLORER_*),
            # whose mlx-vlm server processes the audio track (verified
            # 2026-04-22). Guard: only when AI_EXPLORER is a DISTINCT endpoint
            # from the plain text model. If it points at the same endpoint as
            # the text backend, it IS the text model and cannot handle a video
            # payload -- routing video there fails with auth/format errors
            # (verified 2026-05-28: explorer=deepseek/OpenRouter captured
            # audio-video traffic and 401'd). Fall through to the dedicated
            # cloud video model, which processes audio too.
            if (AI_EXPLORER_MODEL and AI_EXPLORER_URL
                    and AI_EXPLORER_URL.rstrip("/") != (AI_API_BASE_URL or "").rstrip("/")):
                return AI_EXPLORER_MODEL, AI_EXPLORER_URL.rstrip("/")
            if AI_VIDEO_MODEL and AI_VIDEO_API_URL:
                return AI_VIDEO_MODEL, AI_VIDEO_API_URL.rstrip("/")

        if has_video:
            if AI_VIDEO_MODEL and AI_VIDEO_API_URL:
                return AI_VIDEO_MODEL, AI_VIDEO_API_URL.rstrip("/")
            return self.vision_model, self.vision_url

        if has_images and AI_LOCAL_JUDGE_MODEL and AI_LOCAL_JUDGE_URL:
            # Image-bearing calls always go to the local judge (Gemma
            # 26B by default). Works for every backend -- removing
            # the former ``AI_BACKEND == "vllm"`` guard which silently
            # shipped images to ``self.vision_model`` on "openai"
            # backends (that was how Qwen3-VL kept getting image
            # traffic despite the rest of the project running on
            # Gemma). Now one policy: if it has images, Gemma sees
            # it.
            return AI_LOCAL_JUDGE_MODEL, AI_LOCAL_JUDGE_URL.rstrip("/")

        return self.model, self.base_url

    # ── Internals: message and payload building ──────────────────────────────

    def _build_user_content(
        self,
        text: str,
        images: list[str] | None,
        video: str | None,
        has_images: bool,
        has_video: bool,
    ) -> list[dict[str, Any]] | str:
        if not has_images and not has_video:
            return text

        parts: list[dict[str, Any]] = []

        if images:
            for path in images:
                parts.append({"type": "image_url", "image_url": {"url": encode_image(path)}})

        if video:
            data_uri = encode_video(video)
            from config import AI_VIDEO_API_URL

            vid_target = AI_VIDEO_API_URL or self.vision_url
            # Route content-part type by TARGET URL, not by nominal
            # backend setting. Operators commonly point an openai-compat
            # client at a local MLX server -- those servers expect the
            # ``video_url`` content-part type per Qwen/Gemma VLM API
            # conventions. Only true cloud providers (Gemini's OpenAI-
            # compat shim, OpenAI, Anthropic, OpenRouter) expect video
            # to be wrapped in ``image_url`` with a video data URI.
            if _is_cloud_url(vid_target):
                parts.append({"type": "image_url", "image_url": {"url": data_uri}})
            else:
                parts.append({"type": "video_url", "video_url": {"url": data_uri}})

        parts.append({"type": "text", "text": text})
        return parts

    def _build_payload(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice: str | dict | None,
        temperature: float,
        max_tokens: int,
        has_video: bool,
        target_url: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        # Endpoint-driven, not backend-string-driven: in mixed setups
        # (e.g. text on OpenRouter + vision on Google), self.backend says
        # "openrouter" while this specific call routes to Gemini. Decide
        # from the actual target URL so request-body shaping matches the
        # endpoint that will receive it.
        is_gemini_endpoint = bool(
            target_url and "generativelanguage.googleapis.com" in target_url
        ) or self.backend == "gemini"

        # Don't cap max_tokens — let the model respond with as many tokens
        # as it needs. The model's own context limit is the only constraint.
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                if is_gemini_endpoint and isinstance(tool_choice, dict):
                    payload["tool_choice"] = "required"
                else:
                    payload["tool_choice"] = tool_choice

            # Prevention-first: when we're forcing a specific tool call,
            # also ask the server for schema-constrained JSON output.
            # mlx-vlm honors ``response_format: {"type": "json_object"}``
            # and OpenAI honors ``{"type": "json_schema", ...}``. Gemini
            # rejects the field combined with ANY-mode forced function
            # calling ("Forced function calling (ANY mode) with a response
            # mime type: 'application/json' is unsupported"), so skip it
            # whenever the call goes to a Gemini endpoint.
            if not is_gemini_endpoint:
                payload["response_format"] = {"type": "json_object"}

        if has_video:
            from config import AI_VIDEO_API_URL

            vid_url = AI_VIDEO_API_URL or self.vision_url
            # Local MLX servers accept optional ``video_fps`` and
            # ``video_max_frames`` payload hints; cloud providers reject
            # those fields. Gate strictly by URL so an operator running
            # a local server with ``AI_BACKEND=openai`` (common when
            # reusing OpenAI SDK code) still gets the local hints.
            if not _is_cloud_url(vid_url):
                payload["video_fps"] = 1.0
                payload["video_max_frames"] = 30
        return payload

    # ── Internals: retry, fallback, and 413 batch splitting ──────────────────

    async def _post_chat_completion(
        self,
        target_url: str,
        payload: dict,
        call_timeout: float,
    ) -> "httpx.Response":
        """Issue a single POST to ``<target_url>/chat/completions``.

        Centralizes httpx.AsyncClient construction + headers + URL assembly so
        every LLM call in this file goes through one code path. Callers
        decide how to interpret the response (status codes, body parsing,
        429 backoff, 413 split).
        """
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(call_timeout, connect=30.0),
            headers=self._headers(target_url),
        ) as client:
            return await client.post(
                f"{target_url.rstrip('/')}/chat/completions",
                json=payload,
            )

    async def _execute_with_retry(
        self,
        payload: dict,
        target_url: str,
        images: list[str] | None,
        video: str | None,
        user_prompt: str,
        tools: list[dict] | None,
        tool_choice: str | dict | None,
        temperature: float,
        max_tokens: int,
        max_retries: int,
        call_timeout: float,
    ) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                await self._wait_for_availability(target_url)
                resp = await self._post_chat_completion(target_url, payload, call_timeout)

                if resp.status_code == 413:
                    body_text = resp.text
                    if images and len(images) > 1:
                        logger.warning(
                            "413 Payload Too Large -- splitting %d images into batches",
                            len(images),
                        )
                        return await self._batch_split_images(
                            payload,
                            target_url,
                            images,
                            video,
                            user_prompt,
                            call_timeout,
                        )
                    raise LLMError(
                        f"Payload too large for model (413): {body_text}",
                        status_code=413,
                        response_body=body_text,
                    )

                if resp.status_code == 429:
                    # Rate limited. Honor Retry-After if present, else use
                    # exponential backoff with jitter so N concurrent
                    # tasks that all hit 429 at the same instant don't
                    # retry on the same tick (thundering herd).
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait_s = float(retry_after)
                        except ValueError:
                            wait_s = 2 ** attempt
                    else:
                        wait_s = 2 ** attempt
                    wait_s += random.uniform(0, min(wait_s, 2.0))
                    logger.warning(
                        "429 Too Many Requests (target=%s, attempt %d/%d) "
                        "-- backing off %.1fs (Retry-After=%s)",
                        target_url, attempt, max_retries, wait_s, retry_after,
                    )
                    # Store the FULL response body in LLMError so the
                    # saved transcript contains the complete rate-limit
                    # response from Google/OpenAI (quota headers, retry
                    # guidance, debug info). No truncation.
                    full_body = resp.text
                    last_error = LLMError(
                        f"Rate limited (429): {full_body}",
                        status_code=429,
                        response_body=full_body,
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(wait_s)
                        continue
                    raise last_error

                resp.raise_for_status()
                response = resp.json()

                if not response.get("choices"):
                    raise LLMError(
                        "LLM returned no choices",
                        response_body=json.dumps(response),
                    )
                return response

            except asyncio.CancelledError:
                # CancelledError is BaseException, not Exception -- it skips
                # the retry loop on purpose (cancellation must propagate).
                # Log loudly so this isn't silently invisible in transcripts,
                # then re-raise so the parent task sees it.
                logger.warning(
                    "LLM call CANCELLED on attempt %d/%d (target=%s) -- "
                    "re-raising; retry loop does not retry cancellations",
                    attempt, max_retries, target_url,
                )
                raise
            except httpx.HTTPStatusError as exc:
                last_error = exc
                # Full diagnostic context so the failure is legible without
                # opening transcripts. No truncation: the server's error
                # body often contains the real reason (OOM, unsupported
                # codec, schema mismatch) and it is ALWAYS useful.
                try:
                    body_text = exc.response.text
                except Exception:
                    body_text = "<could not read response body>"
                try:
                    body_headers = dict(exc.response.headers)
                except Exception:
                    body_headers = {}
                # Summarize the request payload shape so we know what was
                # sent without dumping base64 media into the log.
                msg_shape = _describe_message_shape(payload)
                logger.warning(
                    "HTTP %d on attempt %d/%d\n"
                    "  target_url: %s\n"
                    "  request_model: %s\n"
                    "  request_messages: %s\n"
                    "  response_headers: %s\n"
                    "  response_body: %s",
                    exc.response.status_code, attempt, max_retries,
                    target_url,
                    payload.get("model", "?"),
                    msg_shape,
                    body_headers,
                    body_text,
                )
                # Persist the exchange immediately so the request payload
                # (full, including base64 media) and the server's error
                # body both land on disk under llm_transcripts/. Without
                # this, a 500 that eventually succeeds via retry leaves
                # no audit trail of the failure.
                _save_llm_exchange(
                    request_payload=payload,
                    raw_response={
                        "error": True,
                        "status_code": exc.response.status_code,
                        "response_body": body_text,
                        "response_headers": body_headers,
                        "attempt": attempt,
                        "max_retries": max_retries,
                    },
                    target_url=target_url,
                    label=f"http_{exc.response.status_code}_attempt{attempt}",
                    error=exc,
                )
                # Auth/permission/bad-request/not-found are deterministic:
                # retrying cannot fix a stale key or malformed request, it
                # only wastes calls and delays surfacing the misconfig. (A
                # stale key made the 2026-05-28 run log 105 identical 401
                # retries.) Fail fast -- fall through to fallback, then raise.
                if exc.response.status_code in (400, 401, 403, 404, 422):
                    logger.error(
                        "Non-retryable HTTP %d (target=%s) -- aborting retries "
                        "for this call; check API key / request shape",
                        exc.response.status_code, target_url,
                    )
                    break
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                logger.warning(
                    "Network error on attempt %d/%d to %s: %s: %s",
                    attempt, max_retries, target_url,
                    type(exc).__name__, exc,
                )

            if attempt < max_retries:
                # Jittered exponential backoff. Under concurrent mode
                # (AI_MAX_CONCURRENT > 1) the jitter prevents N tasks
                # from retrying on the same tick after a shared failure.
                backoff = 2 ** attempt
                backoff += random.uniform(0, min(backoff, 2.0))
                await asyncio.sleep(backoff)

        fallback = await self._try_fallback(payload, call_timeout)
        if fallback is not None:
            return fallback

        raise LLMError(
            f"LLM call failed after {max_retries} attempts: {last_error}",
            response_body=str(last_error),
        )

    async def _try_fallback(self, payload: dict, timeout: float) -> dict | None:
        from config import AI_FALLBACK_MODEL, AI_FALLBACK_URL
        from functions.bypass_log import (
            CATEGORY_FALLBACK_ENDPOINT, CATEGORY_RETRY_EXHAUSTED,
            SEVERITY_HIGH, SEVERITY_MEDIUM, log_bypass,
        )

        if not AI_FALLBACK_URL or AI_FALLBACK_URL == self.base_url:
            # Primary exhausted + no fallback configured -- the caller
            # will raise. Record this as a data-lost bypass.
            log_bypass(
                category=CATEGORY_RETRY_EXHAUSTED,
                severity=SEVERITY_HIGH,
                source="functions/llm.py:_try_fallback",
                event="no_fallback_configured",
                details={
                    "base_url": self.base_url,
                    "fallback_url": AI_FALLBACK_URL or None,
                    "request": _describe_message_shape(payload),
                    "model": payload.get("model", "?"),
                },
                outcome="primary retries exhausted; no fallback configured; caller raises LLMError",
                data_lost=True,
            )
            return None

        msg_shape = _describe_message_shape(payload)
        logger.warning(
            "Trying fallback model at %s (fallback_model=%s, request=%s)",
            AI_FALLBACK_URL, AI_FALLBACK_MODEL or "<unset>", msg_shape,
        )
        log_bypass(
            category=CATEGORY_FALLBACK_ENDPOINT,
            severity=SEVERITY_MEDIUM,
            source="functions/llm.py:_try_fallback",
            event="fallback_attempted",
            details={
                "primary_url": self.base_url,
                "fallback_url": AI_FALLBACK_URL,
                "fallback_model": AI_FALLBACK_MODEL or None,
                "request": msg_shape,
            },
            outcome=f"retrying on {AI_FALLBACK_URL} with model={AI_FALLBACK_MODEL}",
            data_lost=False,
        )
        # Only override the model when a fallback model is configured;
        # sending model=None would make the fallback endpoint 400.
        fallback_payload = {**payload, **({"model": AI_FALLBACK_MODEL} if AI_FALLBACK_MODEL else {})}
        try:
            await self._wait_for_availability(AI_FALLBACK_URL)
            resp = await self._post_chat_completion(AI_FALLBACK_URL, fallback_payload, timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            try:
                body_text = exc.response.text
            except Exception:
                body_text = "<could not read response body>"
            logger.warning(
                "Fallback also failed: HTTP %d from %s\n"
                "  response_body: %s",
                exc.response.status_code, AI_FALLBACK_URL, body_text,
            )
            log_bypass(
                category=CATEGORY_FALLBACK_ENDPOINT,
                severity=SEVERITY_HIGH,
                source="functions/llm.py:_try_fallback",
                event="fallback_http_error",
                details={
                    "fallback_url": AI_FALLBACK_URL,
                    "fallback_model": AI_FALLBACK_MODEL or None,
                    "status_code": exc.response.status_code,
                    "response_body": body_text,
                    "request": msg_shape,
                },
                outcome="fallback returned non-2xx; caller receives None",
                data_lost=True,
            )
            _save_llm_exchange(
                request_payload=fallback_payload,
                raw_response={
                    "error": True,
                    "status_code": exc.response.status_code,
                    "response_body": body_text,
                    "fallback": True,
                },
                target_url=AI_FALLBACK_URL,
                label=f"fallback_http_{exc.response.status_code}",
                error=exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "Fallback also failed: %s: %s", type(exc).__name__, exc,
            )
            log_bypass(
                category=CATEGORY_FALLBACK_ENDPOINT,
                severity=SEVERITY_HIGH,
                source="functions/llm.py:_try_fallback",
                event="fallback_exception",
                details={
                    "fallback_url": AI_FALLBACK_URL,
                    "fallback_model": AI_FALLBACK_MODEL or None,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                    "request": msg_shape,
                },
                outcome="fallback raised; caller receives None",
                data_lost=True,
            )
            return None

    async def _batch_split_images(
        self,
        original_payload: dict,
        target_url: str,
        images: list[str],
        video: str | None,
        user_prompt: str,
        timeout: float,
    ) -> dict[str, Any]:
        """Split an oversized image payload into smaller batches and merge results.

        The merged response mimics a single ``report_wcag_assessment`` tool
        call so the caller never sees the split.
        """
        all_findings: list[dict] = []
        conf_order = {
            "Not Applicable": -1,
            "Supports": 0,
            "Partially Supports": 1,
            "Does Not Support": 2,
        }
        worst_conformance = "Supports"

        async def _run_batch(batch: list[str], label: str) -> dict[str, Any]:
            parts: list[dict[str, Any]] = []
            for path in batch:
                parts.append({"type": "image_url", "image_url": {"url": encode_image(path)}})
            if video:
                vid_uri = encode_video(video)
                # Route by TARGET URL, not by nominal backend setting --
                # see _build_user_content for the same rule. Local MLX
                # servers want ``video_url``; only true cloud providers
                # want video wrapped as ``image_url``.
                if _is_cloud_url(target_url):
                    parts.append({"type": "image_url", "image_url": {"url": vid_uri}})
                else:
                    parts.append({"type": "video_url", "video_url": {"url": vid_uri}})
            parts.append({"type": "text", "text": f"[{label}]\n\n{user_prompt}"})

            batch_payload = {
                **original_payload,
                "messages": [
                    original_payload["messages"][0],
                    {"role": "user", "content": parts},
                ],
            }

            await self._wait_for_availability(target_url)
            resp = await self._post_chat_completion(target_url, batch_payload, timeout)

            if resp.status_code == 413 and len(batch) > 1:
                mid = len(batch) // 2
                logger.warning(
                    "413 inside batch '%s' (%d images) -- recursively splitting",
                    label, len(batch),
                )
                left = await _run_batch(batch[:mid], f"{label} L")
                right = await _run_batch(batch[mid:], f"{label} R")
                return {"_merged": [left, right]}

            if resp.status_code != 200:
                full_body = resp.text
                raise LLMError(
                    f"Batch '{label}' returned HTTP {resp.status_code}: {full_body}",
                    status_code=resp.status_code,
                    response_body=full_body,
                )
            return resp.json()

        async def _collect(raw: dict[str, Any]) -> None:
            nonlocal worst_conformance
            if "_merged" in raw:
                for sub in raw["_merged"]:
                    await _collect(sub)
                return
            parsed = parse_ai_response(raw)
            for f in parsed.get("findings", []):
                all_findings.append(f.to_dict() if hasattr(f, "to_dict") else f)
            level = parsed.get("conformance_level", "Supports")
            level_str = level.value if hasattr(level, "value") else str(level)
            if conf_order.get(level_str, 0) > conf_order.get(worst_conformance, 0):
                worst_conformance = level_str

        batch_size = max(len(images) // 2, 1)
        total_batches = (len(images) + batch_size - 1) // batch_size
        for i in range(0, len(images), batch_size):
            batch = images[i : i + batch_size]
            label = (
                f"BATCH {i // batch_size + 1}/{total_batches}: "
                f"images {i + 1}-{i + len(batch)} of {len(images)}"
            )

            last_exc: Exception | None = None
            for attempt in range(1, 4):
                try:
                    raw = await _run_batch(batch, label)
                    await _collect(raw)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "Batch '%s' attempt %d/3 failed: %s", label, attempt, exc
                    )
                    if attempt < 3:
                        await asyncio.sleep(2 ** attempt)

            if last_exc is not None:
                raise LLMError(
                    f"Batch '{label}' failed after 3 attempts: {last_exc}. "
                    f"Refusing to drop {len(batch)} image(s) -- no gaps allowed.",
                    response_body=str(last_exc),
                )

        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "report_wcag_assessment",
                                    "arguments": json.dumps(
                                        {
                                            "conformance_level": worst_conformance,
                                            "confidence": 0.7,
                                            "confidence_reasoning": "Split into batches due to payload size limit",
                                            "findings": all_findings,
                                            "summary": (
                                                f"Analysis split across batches. "
                                                f"{len(all_findings)} findings merged."
                                            ),
                                        }
                                    ),
                                }
                            }
                        ]
                    }
                }
            ]
        }
