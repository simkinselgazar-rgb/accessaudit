"""Universal parser for LLM responses.

ONE function parses any tool call from any backend:
- OpenAI standard ``tool_calls`` arrays (Gemini, OpenAI, local vLLM)
- Gemma native:  <|tool_call>call:name{json}<tool_call|>
- Qwen native:   <function=name><parameter=key>value</parameter></function>
- Freeform JSON in the content text
- Plain prose (returns None)

Everything else in this file exists to support ``parse_tool_response``.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from models import ConformanceLevel, Finding, Severity

logger = logging.getLogger(__name__)


# ── Think-tag stripping ──────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_CHANNEL_RE = re.compile(r"<\|channel\|?>thought.*?(?:\[result-id:.*?\]|$)", re.DOTALL)


def strip_think_tags(text: str) -> str:
    """Remove reasoning blocks from model output."""
    text = _THINK_RE.sub("", text)
    text = _CHANNEL_RE.sub("", text)
    return text.strip()


# ── Control token cleaning ───────────────────────────────────────────────────

_GEMMA_QUOTE_TOKEN = '<|"|>'


def _convert_gemma_quote_pairs(text: str) -> str:
    """Convert Gemma's ``<|"|>...<|"|>`` paired delimiters into proper JSON
    string literals, escaping any literal ``"`` characters inside the
    content.

    Gemma 26B emits string values wrapped in ``<|"|>`` control tokens
    instead of bare double quotes. The content between markers may itself
    contain literal ``"`` characters (e.g. CSS attribute selectors like
    ``data-bs-target="#foo"``). A naive ``replace('<|"|>', '"')`` would
    destroy the distinction between delimiter quotes and inner literals,
    leaving us with mismatched quotes that no JSON parser can recover.

    The fix: split on the marker. The marker is structurally distinct
    from a bare quote, so a split unambiguously separates structure
    (parts at even indices) from string content (parts at odd indices).
    For every content segment we backslash-escape literal quotes and
    wrap the result in real JSON quotes.

    Edge cases:
    - Odd marker count (truncated stream): the final unmatched content
      segment is still wrapped, producing recoverable-ish JSON that the
      downstream parser can attempt to repair further.
    - No markers at all: returns the input unchanged.
    """
    if _GEMMA_QUOTE_TOKEN not in text:
        return text

    parts = text.split(_GEMMA_QUOTE_TOKEN)
    out: list[str] = [parts[0]]
    for i in range(1, len(parts)):
        if i % 2 == 1:
            # String content (between marker i and marker i+1).
            # Escape everything JSON-strings are not allowed to carry
            # verbatim: backslashes first (so later escapes don't get
            # double-escaped), then literal double quotes, then the
            # control characters newline/carriage-return/tab (Gemma
            # emits literal newlines inside values like ``<p>text
            # more text</p>`` -- JSON requires ``\n``).
            content = parts[i]
            escaped = (
                content.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t")
            )
            out.append('"' + escaped + '"')
        else:
            # JSON structure between two complete pairs.
            out.append(parts[i])
    return "".join(out)


def clean_tool_call_args(text: str) -> str:
    """Strip provider control tokens (``<|...``) from tool-call JSON.

    Handles all known Gemma/MLX control token patterns:
    - ``<|"|>...<|"|>``  →  ``"...":  paired string delimiters with
      inner literal ``"`` characters properly escaped (the most common
      case, handled by ``_convert_gemma_quote_pairs``)
    - ``<|\\"|``  →  ``"``   (escaped-quote variant from older models)
    - ``<|\\``    →  (empty)
    - ``<|``      →  (empty)

    Safe for clean JSON: returns unchanged when no control tokens are present.
    """
    if "<|" not in text:
        return text

    # Most common Gemma pattern: <|"|> paired delimiters. Run this FIRST
    # so the pair-aware splitter sees the markers intact -- a destructive
    # replace would lose the pairing information and corrupt CSS selectors
    # and any other value containing literal " characters.
    text = _convert_gemma_quote_pairs(text)

    # Older / less common variants. These still use simple replace because
    # we have not seen them carry inner-literal quotes in the wild.
    PIPE_BSQ_PIPE = '<|' + chr(92) + chr(34) + '|'    # <|\"|
    PIPE_BQ = '<|' + chr(92) + chr(34)                # <|\"
    PIPE_BS = '<|' + chr(92)                          # <|\

    text = text.replace(PIPE_BSQ_PIPE, '"')
    text = text.replace(PIPE_BQ, '"')
    text = text.replace(PIPE_BS, "")

    # Generic <| cleanup (must be LAST -- catches any remaining)
    text = text.replace("<|", "")
    # Clean up any orphaned |> from partial token matches
    text = text.replace("|>", "")

    # Fix mangled booleans from token collision
    text = re.sub(r'"tru?true', "true", text)
    text = re.sub(r'"fal?false', "false", text)
    text = re.sub(r'"falsfalse', "false", text)
    text = re.sub(r'"trtrue', "true", text)

    # Fix missing commas between values
    text = re.sub(r'(true|false|null)\s*"', r'\1, "', text)
    return text


# ── Loose JSON recovery ──────────────────────────────────────────────────────

def loose_json_loads(text: str) -> Any:
    """Parse JSON-ish output from a model that doesn't quite emit valid JSON.

    Handles every malformed-output pattern we have seen in production:
    - Unquoted object keys (Gemma, Qwen native format)
    - Single-quoted strings
    - Trailing commas before ``}`` / ``]``
    - Stray double quotes inside string values (Gemma's ``<|"|>`` token gets
      stripped to ``"`` even when it was meant to be a literal inner quote
      inside a value, e.g. ``(alt=<|"|><|"|>)`` -> ``(alt="")`` -> JSON sees
      the empty ``""`` as ending and restarting the string)
    - Unescaped colons inside string values (CSS selectors like
      ``div:nth-of-type(1)`` look like keys to a naive regex)

    Strategy: try strict ``json.loads`` first. On failure, run a single
    state-machine pass over the text that simultaneously (a) wraps unquoted
    keys in double quotes when outside any string, and (b) escapes orphan
    double quotes when inside a string value. Both fixes need string-state
    awareness, which is why a single state machine handles them together --
    sequential regexes corrupt each other.

    Raises ``json.JSONDecodeError`` if recovery still fails.
    """
    if not isinstance(text, str):
        raise json.JSONDecodeError("not a string", str(text), 0)
    text = text.strip()

    # Strict pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Single state-aware pass that quotes bare keys + escapes inner quotes.
    # Do NOT post-process with regex -- the walk produces valid JSON, and
    # additional regex passes (e.g. quote-promotion or trailing-comma
    # stripping) would corrupt string values that legitimately contain the
    # patterns those regexes look for (e.g. a finding mentioning the page
    # heading 'FARTHER THAN EVER').
    repaired = _repair_loose_json(text)
    return json.loads(repaired)


_VALUE_END_CHARS = frozenset(",}]:")
_KEY_CHAR = re.compile(r"[A-Za-z_]")
_KEY_BODY = re.compile(r"[A-Za-z0-9_\-]")
_JSON_LITERALS = frozenset(("true", "false", "null"))
_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")


def _repair_loose_json(text: str) -> str:
    """Single-pass state-aware loose-JSON repair.

    Walks ``text`` keeping track of whether we are inside a string literal.

    Outside strings:
      - When we see a bareword followed by ``:``, we wrap the bareword in
        double quotes. Bareword = starts with ``[A-Za-z_]``, continues with
        ``[A-Za-z0-9_-]``. JSON values like ``true``/``false``/``null`` are
        also barewords but they appear after ``:`` not before, so the
        ``followed-by-:`` constraint excludes them.

    Inside strings:
      - Backslash escapes are preserved verbatim.
      - When we see a ``"``, we peek ahead past whitespace to the next
        non-whitespace char. If it's one of ``,}]:`` we treat the ``"`` as
        a closing string delimiter; otherwise it is an orphan inner quote
        that we escape to ``\\"`` to keep the JSON parseable.
    """
    out: list[str] = []
    i = 0
    in_string = False
    n = len(text)

    while i < n:
        ch = text[i]

        if not in_string:
            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
                continue

            # Try to match a bareword. Two cases:
            #   (a) followed by ``:`` -> bareword is a KEY; wrap it and
            #       consume the colon.
            #   (b) followed by ``,``/``}``/``]`` (optionally via
            #       whitespace) -> bareword is a VALUE; wrap it UNLESS
            #       it's a JSON literal (``true``/``false``/``null``)
            #       or a numeric. Gemma emits malformed values like
            #       ``pattern_type:_redundant_text,severity:low,`` with
            #       no quotes -- this branch recovers them.
            #   (c) otherwise fall through: the bareword belongs to a
            #       context we don't rewrite (e.g. inside raw HTML).
            if _KEY_CHAR.match(ch):
                j = i + 1
                while j < n and _KEY_BODY.match(text[j]):
                    j += 1
                # Look ahead past whitespace to the next significant char.
                k = j
                while k < n and text[k] in " \t":
                    k += 1
                # Case (a): key followed by colon.
                if k < n and text[k] == ":":
                    word = text[i:j]
                    out.append(f'"{word}"')
                    out.append(text[j:k + 1])
                    i = k + 1
                    continue
                # Case (b): value terminated by , } ] or newline.
                if k >= n or text[k] in ",}]\n\r":
                    word = text[i:j]
                    if word in _JSON_LITERALS or _NUMERIC_RE.match(word):
                        # Valid JSON literal/number -- emit as-is.
                        out.append(word)
                    else:
                        # Unquoted string value -- wrap.
                        out.append(f'"{word}"')
                    # Preserve whatever whitespace we walked past.
                    out.append(text[j:k])
                    i = k
                    continue

            out.append(ch)
            i += 1
            continue

        # Inside a string
        if ch == "\\":
            out.append(ch)
            if i + 1 < n:
                out.append(text[i + 1])
                i += 2
            else:
                i += 1
            continue

        if ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\n\r":
                j += 1
            if j >= n or text[j] in _VALUE_END_CHARS:
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


# ── Native tool call parsing ─────────────────────────────────────────────────

def parse_native_tool_call(content: str) -> dict | None:
    """Parse Gemma/Qwen-style native tool calls out of the assistant content."""
    # IMPORTANT: Run regex on RAW content BEFORE clean_tool_call_args,
    # because cleaning strips <| which would destroy the <|tool_call> tag.
    # Try full match first (with closing tag)
    gemma = re.search(
        r"<\|tool_call>_?call:(?:\w+:)*(\w+)\s*(\{.*\})\s*</?tool_call\|?>",
        content, re.DOTALL,
    )
    # Fallback: truncated response — no closing tag, grab everything after {
    if not gemma:
        gemma = re.search(
            r"<\|tool_call>_?call:(?:\w+:)*(\w+)\s*(\{.+)",
            content, re.DOTALL,
        )

    if gemma:
        fn_name, args_str = gemma.group(1), gemma.group(2)
        args_str = clean_tool_call_args(args_str)

        # ATTEMPT 1: loose_json_loads on the cleaned args as-is. This
        # is the happy path -- any complete Gemma output parses here.
        # Do NOT pre-repair brackets: ``raw_evidence`` values often
        # quote JavaScript source containing literal ``{`` ``}`` ``[``
        # ``]`` characters, so a naive structural-bracket count
        # FALSELY claims the JSON is truncated, and a pre-repair pass
        # corrupts already-valid output.
        try:
            return {"function": fn_name, "arguments": loose_json_loads(args_str)}
        except json.JSONDecodeError:
            pass

        # ATTEMPT 2: the args look truncated (no closing brace reached
        # the output). Try to repair by trimming to the last balanced
        # ``}`` and padding the remaining opens. Only triggers when
        # attempt 1 failed, so the bracket-count heuristic is at least
        # correctly applied to incomplete content.
        if not args_str.rstrip().endswith("}"):
            last_brace = args_str.rfind("}")
            if last_brace > 0:
                repaired = args_str[:last_brace + 1]
                repaired += "}" * max(0, repaired.count("{") - repaired.count("}"))
                repaired += "]" * max(0, repaired.count("[") - repaired.count("]"))
                logger.info("Repaired truncated Gemma tool call JSON")
                try:
                    return {
                        "function": fn_name,
                        "arguments": loose_json_loads(repaired),
                    }
                except json.JSONDecodeError:
                    pass

        # ATTEMPT 3: historical bare-fix escape hatch (regex-based
        # unquoted-key wrapping, single-quote substitution, trailing
        # comma strip). Less safe than the state-aware
        # ``loose_json_loads`` walk but occasionally recovers outputs
        # that walk can't.
        fixed = re.sub(r"(?<=[{,\s])(\w+)\s*:", r'"\1":', args_str)
        fixed = fixed.replace("'", '"')
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        try:
            return {"function": fn_name, "arguments": json.loads(fixed)}
        except json.JSONDecodeError:
            logger.debug("Gemma tool call JSON fix failed: %s", fixed)

    fn_match = re.search(r"<function=(\w+)>", content)
    if not fn_match:
        return None

    fn_name = fn_match.group(1)
    params: dict[str, Any] = {}
    for param_start in re.finditer(r"<parameter=(\w+)>", content):
        key = param_start.group(1)
        value_start = param_start.end()
        close_idx = content.find("</parameter>", value_start)

        if close_idx > 0:
            value = content[value_start:close_idx].strip()
        else:
            end = len(content)
            for boundary in ("<parameter=", "</function>", "</tool_call>"):
                idx = content.find(boundary, value_start)
                if 0 < idx < end:
                    end = idx
            value = content[value_start:end].strip()

        if not value:
            continue
        try:
            params[key] = json.loads(value)
        except json.JSONDecodeError:
            params[key] = _repair_truncated_json(value, key)

    if params:
        return {"function": fn_name, "arguments": params}
    return None


def _repair_truncated_json(value: str, key: str) -> Any:
    """Best-effort repair for truncated JSON arrays/objects from streaming output."""
    if value.startswith("["):
        last_brace = value.rfind("}")
        if last_brace > 0:
            try:
                result = json.loads(value[: last_brace + 1] + "]")
                logger.info("Repaired truncated %s array: %d items", key, len(result))
                return result
            except json.JSONDecodeError:
                pass
    elif value.startswith("{"):
        last_brace = value.rfind("}")
        if last_brace > 0:
            try:
                return json.loads(value[: last_brace + 1])
            except json.JSONDecodeError:
                pass
    return value


# ── JSON extraction from freeform text ───────────────────────────────────────

def extract_json_from_text(text: str) -> dict[str, Any]:
    """Extract a JSON object from freeform model prose."""
    cleaned = strip_think_tags(text)

    stripped = re.sub(
        r"```(?:json)?\s*\n?(.*?)\n?\s*```",
        r"\1",
        cleaned,
        flags=re.DOTALL,
    ).strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    candidates: list[str] = []
    depth, start = 0, -1
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(cleaned[start : i + 1])
                start = -1

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and ("conformance_level" in obj or "findings" in obj):
                return obj
        except json.JSONDecodeError:
            continue

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    first, last = cleaned.find("{"), cleaned.rfind("}")
    if first >= 0 and last > first:
        try:
            obj = json.loads(cleaned[first : last + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON object found in model response text")


# ── Universal tool response parser ───────────────────────────────────────────

def parse_tool_response(
    response_data: dict[str, Any],
    tool_name: str | None = None,
) -> dict[str, Any] | None:
    """Parse ANY chat-completions response and return the tool-call arguments.

    Args:
        response_data: Raw chat/completions response dict.
        tool_name: Expected tool function name. If None, accepts any tool call.

    Returns:
        The tool-call arguments as a dict, or None if nothing is parseable.
    """
    choices = response_data.get("choices")
    if not choices:
        return None

    message = choices[0].get("message", {})

    # ── Pass 1: exact-name tool_calls ───────────────────────────────────
    # If the model emitted an OpenAI-style tool_calls array with the
    # correct function name AND validly-parsed args, trust it
    # unconditionally. When the function name matches the requested
    # tool_name we do NOT gate on _is_substantive_payload: empty-but-
    # well-formed payloads (e.g. ``{"patterns": []}``, ``{"findings":
    # []}``) are a legitimate answer for inventory-style tools -- "this
    # chunk has no findings" is valid information, not abdication.
    # Dropping those triggered the 3-attempt cascade on perfectly-
    # correct responses and produced false "empty response" failures.
    # The _is_substantive_payload gate still applies to Pass 3 below,
    # where the function name is wrong and an empty payload genuinely
    # could be abdication.
    tool_calls = message.get("tool_calls") or []
    if tool_calls and tool_name:
        for tc in tool_calls:
            fn = tc.get("function", {})
            if fn.get("name", "") != tool_name:
                continue
            parsed = _parse_tool_args(fn.get("arguments", ""))
            # Accept keyed-but-empty payloads ({"findings": []}, {"patterns":
            # []}) -- "this chunk has no findings" is a valid answer. Reject a
            # bare {} with no keys: a forced tool returning zero fields is
            # abdication, and falling through retries it.
            if parsed is not None and parsed != {}:
                return parsed

    # ── Pass 2: native formats in content ───────────────────────────────
    # Many models (Gemma 26B/E4B) do not emit a tool_calls array at all;
    # the call lives in the content text as <|tool_call>...<tool_call|>.
    # These usually have the right function name but we accept any shape
    # because tool_choice forced a specific tool -- the model had exactly
    # one choice, so any native tool call in the content MUST be that one.
    content = message.get("content", "") or ""
    content_clean = strip_think_tags(content) if content else ""
    if content_clean and (
        "<tool_call>" in content_clean
        or "<|tool_call>" in content_clean
        or "<function=" in content_clean
    ):
        native = parse_native_tool_call(content_clean)
        if native and isinstance(native.get("arguments"), dict):
            parsed = _normalize_tool_args(native["arguments"])
            # Accept the native tool call when the function name MATCHES
            # the requested tool_name. Empty-but-well-formed payloads
            # (e.g. {"patterns": []}) are a legitimate answer for
            # inventory-style tools: "this chunk has no findings" is
            # valid information, not model abdication. Only fall
            # through to substantive-payload gating when tool_name is
            # unset or mismatched -- that path still guards against
            # empty {} abdications.
            if tool_name and native.get("function") == tool_name:
                return parsed
            if _is_substantive_payload(parsed):
                return parsed

    # ── Pass 3: wrong-name but substantive tool_calls ───────────────────
    # Qwen sometimes ignores tool_choice and emits a tool_calls entry
    # with an invented name ('link', 'function', etc) but the arguments
    # ARE the real structured data. Accept if the parsed args have at
    # least one meaningful field. Empty {} is rejected -- that means the
    # model abdicated and we should retry or fall through.
    if tool_calls and tool_name is None:
        # caller didn't specify a name; try any tool_call
        for tc in tool_calls:
            fn = tc.get("function", {})
            parsed = _parse_tool_args(fn.get("arguments", ""))
            if _is_substantive_payload(parsed):
                return parsed
    elif tool_calls:
        # Accept wrong-name tool_calls only if their args are substantive
        # AND there isn't a better content-based alternative further down.
        for tc in tool_calls:
            fn = tc.get("function", {})
            parsed = _parse_tool_args(fn.get("arguments", ""))
            if _is_substantive_payload(parsed):
                # defer: the content path below might still have a richer
                # answer. Cache this and check content first.
                deferred = parsed
                break
        else:
            deferred = None
    else:
        deferred = None

    # ── Pass 4: freeform JSON in content ────────────────────────────────
    if content_clean:
        try:
            candidate = _normalize_tool_args(extract_json_from_text(content_clean))
            if _is_substantive_payload(candidate):
                return candidate
        except ValueError:
            pass

    # ── Pass 5: last-resort deferred wrong-name tool_calls ──────────────
    if deferred is not None:
        return deferred

    return None


def _is_substantive_payload(parsed: Any) -> bool:
    """Return True if a parsed tool-call result carries real information.

    Qwen has been observed emitting ``function.name="link"`` with
    ``arguments="{}"`` -- a tool_calls entry that looks valid structurally
    but conveys zero data. Treating that as a parse success silently drops
    the real analysis (which lives in the ``content`` field instead). This
    check rejects empty dicts and lists so the cascade keeps searching.
    """
    if parsed is None:
        return False
    if isinstance(parsed, dict):
        # At least one non-empty field.
        for v in parsed.values():
            if v is None or v == "" or v == [] or v == {}:
                continue
            return True
        return False
    if isinstance(parsed, list):
        return len(parsed) > 0
    return True


def _parse_tool_args(args_raw: Any) -> dict | None:
    """Parse tool-call arguments whether they arrive as a dict or a JSON string.

    Every successfully-parsed result is run through ``_normalize_tool_args``
    so callers see consistent types regardless of which model produced the
    reply. Qwen tends to return numbers as strings (``"0.9"`` instead of
    ``0.9``) and double-encodes nested arrays (``"elements": "[{...}]"``
    as a string containing JSON). We handle both centrally here so nothing
    downstream has to care which model it's reading from.
    """
    if isinstance(args_raw, dict):
        return _normalize_tool_args(args_raw)
    if not isinstance(args_raw, str) or not args_raw.strip():
        return None
    cleaned = clean_tool_call_args(strip_think_tags(args_raw))
    try:
        parsed = loose_json_loads(cleaned)
        if isinstance(parsed, dict):
            return _normalize_tool_args(parsed)
    except json.JSONDecodeError:
        pass
    return None


def _normalize_tool_args(obj: Any) -> Any:
    """Normalize a parsed tool-call arguments dict for cross-model consistency.

    Two failure modes we've hit in production:

    1. **Numbers as strings** (Qwen 35B) -- ``{"confidence": "0.9"}`` should
       be ``{"confidence": 0.9}``. Downstream code uses
       ``float(confidence)`` which handles it, but severity/finding-count
       logic that uses ``>`` comparisons on strings misbehaves silently.
       We coerce ``"0.9"``, ``"1"``, ``"-2.5"`` to numbers whenever the
       string is a clean numeric literal.

    2. **Double-encoded inner arrays** (Qwen 35B restructure replies) --
       ``{"elements": "[{\"type\": \"button\", ...}]"}`` has the
       ``elements`` value as a STRING containing JSON, instead of an actual
       array. We detect strings that parse as JSON (``[...]`` or ``{...}``)
       and decode them in place.

    Applied recursively so nested structures get the same treatment.
    Never raises -- if a value cannot be coerced it's returned as-is.
    """
    if isinstance(obj, dict):
        return {k: _normalize_tool_args(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_tool_args(v) for v in obj]
    if isinstance(obj, str):
        stripped = obj.strip()
        if not stripped:
            return obj

        # Try to decode a string that looks like JSON (Qwen double-encoding)
        if stripped[0] in "[{":
            try:
                decoded = json.loads(stripped)
                if isinstance(decoded, (dict, list)):
                    return _normalize_tool_args(decoded)
            except json.JSONDecodeError:
                pass

        # Coerce pure numeric strings to numbers
        if _NUMERIC_RE.fullmatch(stripped):
            try:
                if "." in stripped or "e" in stripped.lower():
                    return float(stripped)
                return int(stripped)
            except ValueError:
                pass

        # Coerce bool-ish strings
        low = stripped.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low == "null":
            return None
    return obj


def describe_response_shape(response_data: dict[str, Any]) -> str:
    """Return a short label describing how an LLM response was structured.

    Used by the LLMClient logging to report which parser strategy is going
    to recover the tool call:

        ``openai_tool_calls``      - native OpenAI tool_calls array
        ``gemma_native``           - <|tool_call>...<tool_call|> in content
        ``qwen_native``            - <function=...><parameter=...> in content
        ``freeform_json``          - JSON object embedded in prose content
        ``prose``                  - plain text, no recognizable tool format
        ``empty``                  - no content at all
    """
    choices = response_data.get("choices", [])
    if not choices:
        return "empty"
    message = choices[0].get("message", {})
    if message.get("tool_calls"):
        return "openai_tool_calls"
    content = message.get("content") or ""
    if not content:
        return "empty"
    if "<|tool_call>" in content:
        return "gemma_native"
    if "<function=" in content:
        return "qwen_native"
    if "{" in content and "}" in content:
        return "freeform_json"
    return "prose"


def get_content_text(response_data: dict[str, Any]) -> str:
    """Extract plain text content from a chat-completions response."""
    choices = response_data.get("choices", [])
    if not choices:
        return ""
    content = choices[0].get("message", {}).get("content", "")
    return strip_think_tags(content) if content else ""


# ── Conformance + severity normalization ─────────────────────────────────────

_CONFORMANCE_ALIASES: dict[str, ConformanceLevel] = {
    "supports": ConformanceLevel.SUPPORTS,
    "partially supports": ConformanceLevel.PARTIALLY_SUPPORTS,
    "does not support": ConformanceLevel.DOES_NOT_SUPPORT,
    "not applicable": ConformanceLevel.NOT_APPLICABLE,
    "not evaluated": ConformanceLevel.NOT_EVALUATED,
    "pass": ConformanceLevel.SUPPORTS,
    "fail": ConformanceLevel.DOES_NOT_SUPPORT,
    "partial": ConformanceLevel.PARTIALLY_SUPPORTS,
    "partially_supports": ConformanceLevel.PARTIALLY_SUPPORTS,
    "does_not_support": ConformanceLevel.DOES_NOT_SUPPORT,
    "not_applicable": ConformanceLevel.NOT_APPLICABLE,
    "not_evaluated": ConformanceLevel.NOT_EVALUATED,
    "n/a": ConformanceLevel.NOT_APPLICABLE,
    "na": ConformanceLevel.NOT_APPLICABLE,
}

_SEVERITY_ALIASES: dict[str, Severity] = {
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
    "info": Severity.INFO,
    "critical": Severity.HIGH,
    "moderate": Severity.MEDIUM,
    "minor": Severity.LOW,
    "informational": Severity.INFO,
}


def normalize_conformance_level(value: str) -> ConformanceLevel:
    """Map a raw string to a ConformanceLevel enum."""
    if not value:
        raise ValueError("Empty conformance level string")
    key = value.strip().lower()
    result = _CONFORMANCE_ALIASES.get(key)
    if result is not None:
        return result
    for member in ConformanceLevel:
        if member.value.lower() == key:
            return member
    raise ValueError(f"Unrecognised conformance level: {value!r}")


def normalize_severity(value: str) -> Severity:
    """Map a raw severity string to a Severity enum, defaulting to MEDIUM."""
    result = _SEVERITY_ALIASES.get(str(value).strip().lower())
    if result is not None:
        return result
    logger.warning("Unrecognised severity %r, defaulting to MEDIUM", value)
    return Severity.MEDIUM


# ── Source-attribution integrity ─────────────────────────────────────────────

# Sources recognized as legitimate non-judge attributions. A judge-emitted
# finding claiming any of these MUST trace back to an input finding from the
# same source family — otherwise the claim is downgraded to "judge_inference"
# (the model added the finding from its own reasoning, not from a source).
#
# "htmlcs" and "ibm_eac" were added when those open-source rule engines were
# integrated alongside axe; the judge uses them the same way it uses axe --
# as deterministic input findings to consolidate, never as labels for its
# own visual inferences.
NON_JUDGE_SOURCE_TAGS = frozenset({
    "programmatic", "axe", "andi", "htmlcs", "ibm_eac",
    "visual_ai", "code_ai", "at_sim",
})

# "ai" is a legacy alias for "any AI source" — accept it as matching
# {visual_ai, code_ai, at_sim} when any of those produced a related finding.
_AI_ALIASES = frozenset({"visual_ai", "code_ai", "at_sim"})

# Issue-text overlap window for fuzzy matching (chars).
_ISSUE_MATCH_WINDOW = 30


def _normalize_text(s: Any) -> str:
    return (str(s) if s is not None else "").strip().lower()


def _normalize_selector(s: str) -> str:
    """Canonicalize selector forms so equivalent expressions compare equal.

    Converts ``[id="foo"]`` (attribute form) and ``*[id="foo"]`` to
    ``#foo`` (id selector form). Both reference the same DOM node;
    different upstream sources emit different forms (the SC 4.1.1
    duplicate-id check uses ``[id="foo"]``, the judge rewrites to
    ``#foo``), and a literal-string match demoted legitimate findings
    to judge_inference until this normalization landed (a university run
    f8765656 SC 4.1.1).
    """
    if not s:
        return s
    import re as _re
    # *[id="X"] -> #X  and  [id='X'] -> #X
    s = _re.sub(r"\*?\[id\s*=\s*[\"']([^\"']+)[\"']\]", r"#\1", s)
    return s


def _split_source_tags(source: Any) -> list[str]:
    """Split 'ai, programmatic' / 'programmatic + ai' / etc. into clean tags."""
    raw = _normalize_text(source)
    if not raw:
        return []
    # Replace common separators with spaces, then split
    for sep in (",", "+", "/", "|", ";"):
        raw = raw.replace(sep, " ")
    return [t for t in raw.split() if t]


def _findings_match(out_sel: str, out_elem: str, out_issue: str,
                    in_sel: str, in_elem: str, in_issue: str) -> bool:
    """Decide whether a judge-output finding plausibly descends from an input
    finding. A judge claim must be anchored to a real upstream finding via
    selector OR element identity; pure issue-text similarity is not enough.

    Match paths (in priority order):

    1. Selector match (strongest signal):
       - Direct equality after normalization
       - Substring containment when both sides are reasonably long
    2. Cross-field selector match for the legacy convention where the
       programmatic check stuffs its selector into the ``element`` field
       and leaves ``css_selector`` empty (e.g. checks_1_3.py for SC 1.3.1
       heading findings). Symmetric handling on both sides.
    3. Element-field equality combined with issue-text head overlap. The
       element field is often a free-text label like "Apply Now button" --
       equality alone over-matches across SCs. Requiring issue-text
       co-occurrence keeps legitimate consolidation but rejects laundered
       claims that share only a label.

    Selector normalization (``_normalize_selector``) bridges the
    attribute-form vs id-form divergence: SC 4.1.1's duplicate-id check
    emits ``[id="X"]`` while the judge rewrites to ``#X``. Both reference
    the same DOM node; without normalization the validator treats them as
    different and demotes legitimate findings. Verified on university runs
    f8d46924 (SC 1.3.1) and f8765656 (SC 4.1.1).

    What we explicitly REJECT (relative to the prior implementation):
    issue-text head overlap alone, with neither selector nor element
    anchor to an input finding. Issue text is often a generic phrase
    ("The button has insufficient...") that recurs across many findings
    and across many SCs; matching on it alone let the judge launder its
    own inferences as deterministic measurements (verified on the
    fairfaxva.gov run 20260514_205147_cb3b646c: SC 1.4.3 cited a 4.44:1
    contrast ratio with source='axe' when the prompt had no
    text-contrast measurement for that selector, and the closest number
    was the 4.45 focus-indicator ratio for a different SC).
    """
    # Normalize selectors so equivalent forms compare equal.
    out_sel_n = _normalize_selector(out_sel)
    in_sel_n = _normalize_selector(in_sel)
    out_elem_n = _normalize_selector(out_elem)
    in_elem_n = _normalize_selector(in_elem)

    # 1. Direct selector match (both sides have css_selector populated)
    if out_sel_n and in_sel_n:
        if out_sel_n == in_sel_n:
            return True
        if len(out_sel_n) >= 5 and len(in_sel_n) >= 5 and (out_sel_n in in_sel_n or in_sel_n in out_sel_n):
            return True
    # 2a. Cross-field selector match: input check stuffed the selector into
    # the element field and left css_selector empty.
    if out_sel_n and in_elem_n and not in_sel_n:
        if out_sel_n == in_elem_n:
            return True
        if len(out_sel_n) >= 5 and len(in_elem_n) >= 5 and (out_sel_n in in_elem_n or in_elem_n in out_sel_n):
            return True
    # 2b. Symmetric: output stuffed selector into element, input has it in css_selector
    if in_sel_n and out_elem_n and not out_sel_n:
        if in_sel_n == out_elem_n:
            return True
        if len(in_sel_n) >= 5 and len(out_elem_n) >= 5 and (in_sel_n in out_elem_n or out_elem_n in in_sel_n):
            return True
    # 3. Element-field equality PLUS issue-text head overlap. Element label
    # alone over-matches ("Apply Now button" recurs); requiring issue
    # co-occurrence keeps legitimate consolidation while rejecting
    # cross-SC label collisions.
    if out_elem and in_elem and out_elem == in_elem:
        if _issues_share_head(out_issue, in_issue):
            return True
    return False


def _issues_share_head(out_issue: str, in_issue: str) -> bool:
    """Issue-text head-overlap secondary signal. Both strings must be at
    least ``_ISSUE_MATCH_WINDOW`` chars and one's leading window must
    appear inside the other.
    """
    if not out_issue or not in_issue:
        return False
    if len(out_issue) < _ISSUE_MATCH_WINDOW or len(in_issue) < _ISSUE_MATCH_WINDOW:
        return False
    head = out_issue[:_ISSUE_MATCH_WINDOW]
    if head in in_issue:
        return True
    head = in_issue[:_ISSUE_MATCH_WINDOW]
    return head in out_issue


def _build_source_index(input_findings: list[Any]) -> dict[str, list[tuple[str, str, str]]]:
    """Group input findings by each tag in their source field.

    Each entry is (selector, element, issue) for matching. A finding tagged
    "ai, programmatic" appears in both buckets so the validator can confirm
    either claim.

    Tag mirroring is bidirectional between the legacy generic ``ai`` tag
    and the canonical ``visual_ai`` tag:

    1. ``visual_ai`` / ``code_ai`` / ``at_sim`` (specific) → also indexed
       under generic ``ai`` so older callers that ask for ``ai`` still work.
    2. Legacy ``ai`` (generic) → also indexed under ``visual_ai`` so a
       judge that correctly emits the canonical tag matches input findings
       that came from the legacy tagging path. Without this reverse mirror,
       any ``run_ai_analysis`` output saved before the 2026-05-08 fix
       (when ``run`` set ``f.source = "ai"`` for visual AI findings)
       would never match the judge's ``visual_ai`` claim and every such
       finding would be incorrectly demoted to ``judge_inference``.

       We mirror ONLY to ``visual_ai`` (not ``code_ai`` / ``at_sim``)
       because the historical legacy ``ai`` tag always meant the visual
       AI run; code AI and AT sim have always been tagged with their
       specific tags. Mirroring to all three would over-match and let
       a judge label its own visual inferences as ``code_ai``.
    """
    by_source: dict[str, list[tuple[str, str, str]]] = {}
    for f in input_findings:
        # Finding object or dict
        if hasattr(f, "source"):
            src_field = f.source
            sel = getattr(f, "css_selector", "") or getattr(f, "selector", "")
            elem = getattr(f, "element", "")
            issue = getattr(f, "issue", "")
        elif isinstance(f, dict):
            src_field = f.get("source", "")
            sel = f.get("css_selector") or f.get("selector") or ""
            elem = f.get("element") or ""
            issue = f.get("issue") or ""
        else:
            continue
        sel_n = _normalize_text(sel)
        elem_n = _normalize_text(elem)
        issue_n = _normalize_text(issue)
        for tag in _split_source_tags(src_field):
            by_source.setdefault(tag, []).append((sel_n, elem_n, issue_n))
            # Forward mirror: specific AI tag → generic "ai" bucket.
            if tag in _AI_ALIASES:
                by_source.setdefault("ai", []).append((sel_n, elem_n, issue_n))
            # Reverse mirror: legacy generic "ai" tag → canonical
            # "visual_ai" bucket, since the legacy tag historically came
            # from the visual AI run. Without this, a judge that emits the
            # canonical "visual_ai" tag finds an empty bucket and the
            # validator demotes legitimate findings to judge_inference.
            elif tag == "ai":
                by_source.setdefault("visual_ai", []).append((sel_n, elem_n, issue_n))
    return by_source


def validate_source_attribution(
    judge_output_findings: list[Any],
    input_findings: list[Any],
) -> tuple[list[dict[str, Any]], int]:
    """Enforce honest source labeling on judge-emitted findings.

    Reasoning: the multi-source pipeline tags every input finding with the
    source that produced it (programmatic, axe, andi, visual_ai, code_ai,
    at_sim). The judge then synthesizes those into a final list. The
    accuracy bug we're fixing: the judge sometimes EMITS new findings — its
    own inferences from the screenshots / DOM context — and labels them
    "programmatic", giving them deterministic gravitas they haven't earned
    (observed on a university 2.5.8: deterministic check returned 0 findings, judge
    emitted 9 with source="programmatic").

    This validator does NOT suppress the judge's autonomy to add findings
    the deterministic checks missed. It only forces honest attribution:
    if the judge claims a finding came from source X, an input finding
    from source X with a matching selector / element / issue must exist.
    Otherwise the claim is downgraded to "judge_inference" so a human
    reading the report can immediately see "this is a model inference,
    not a measurement."

    Args:
        judge_output_findings: list of dicts (or Finding objects) from
            the judge's `final_findings` array.
        input_findings: the multi-source finding pool the judge was
            given as input. Each is expected to have a `source` field
            tagging its origin.

    Returns:
        (validated_findings, count_findings_with_at_least_one_flip).
        Each output dict has its `source` field rewritten to keep only
        sources that pass validation; failed claims are replaced with
        "judge_inference". A finding with no surviving claims gets
        source="judge_inference".
    """
    by_source = _build_source_index(input_findings)
    validated: list[dict[str, Any]] = []
    findings_with_flips = 0

    for ff in judge_output_findings:
        # Coerce to dict view for uniform handling
        if isinstance(ff, dict):
            ff_dict = dict(ff)
        elif hasattr(ff, "to_dict"):
            ff_dict = dict(ff.to_dict())
        elif hasattr(ff, "source"):
            ff_dict = {
                "element": getattr(ff, "element", ""),
                "css_selector": getattr(ff, "css_selector", ""),
                "issue": getattr(ff, "issue", ""),
                "impact": getattr(ff, "impact", ""),
                "recommendation": getattr(ff, "recommendation", ""),
                "severity": getattr(getattr(ff, "severity", ""), "value", getattr(ff, "severity", "")),
                "source": getattr(ff, "source", ""),
            }
        else:
            validated.append(ff)
            continue

        out_sel = _normalize_text(ff_dict.get("css_selector") or ff_dict.get("selector") or "")
        out_elem = _normalize_text(ff_dict.get("element") or "")
        out_issue = _normalize_text(ff_dict.get("issue") or "")

        claimed_tags = _split_source_tags(ff_dict.get("source", ""))
        if not claimed_tags:
            # No source claimed — treat as judge inference
            ff_dict["source"] = "judge_inference"
            validated.append(ff_dict)
            findings_with_flips += 1
            continue

        any_flipped = False
        keepers: list[str] = []
        for tag in claimed_tags:
            if tag == "judge_inference" or tag == "judge":
                keepers.append("judge_inference")
                continue
            if tag not in NON_JUDGE_SOURCE_TAGS and tag != "ai":
                # Unknown / unrecognized tag — be conservative
                keepers.append("judge_inference")
                any_flipped = True
                continue
            # Verify against input findings of the same source
            candidates = by_source.get(tag, [])
            matched = False
            for in_sel, in_elem, in_issue in candidates:
                if _findings_match(out_sel, out_elem, out_issue, in_sel, in_elem, in_issue):
                    matched = True
                    break
            if matched:
                keepers.append(tag)
            else:
                # The finding does not trace to the source it claimed --
                # but it may genuinely trace to a DIFFERENT deterministic
                # source (the judge mislabeled which subsystem measured
                # it). Honest attribution means retagging to the real
                # source, not demoting a measured finding to
                # judge_inference. Verified bug (loudoun.gov SC 1.4.3):
                # 5 real ANDI contrast findings were labeled
                # "programmatic", failed the programmatic match, and were
                # demoted -- then dropped -- instead of retagged to andi.
                retagged = None
                for alt_tag, alt_candidates in by_source.items():
                    if alt_tag == tag or alt_tag not in NON_JUDGE_SOURCE_TAGS:
                        continue
                    for in_sel, in_elem, in_issue in alt_candidates:
                        if _findings_match(out_sel, out_elem, out_issue,
                                            in_sel, in_elem, in_issue):
                            retagged = alt_tag
                            break
                    if retagged:
                        break
                keepers.append(retagged or "judge_inference")
                any_flipped = True

        # Dedupe while preserving order
        seen: set[str] = set()
        final_tags: list[str] = []
        for t in keepers:
            if t not in seen:
                seen.add(t)
                final_tags.append(t)
        if not final_tags:
            final_tags = ["judge_inference"]
        ff_dict["source"] = ", ".join(final_tags)
        validated.append(ff_dict)
        if any_flipped:
            findings_with_flips += 1

    return validated, findings_with_flips


# ── Finding construction ─────────────────────────────────────────────────────

def build_finding(raw: dict[str, Any], source: str = "ai") -> Finding | None:
    """Create a Finding from a raw dict, handling field-name variation across models."""
    element = str(raw.get("element", raw.get("location", "(unknown)")))
    issue = str(raw.get("issue", raw.get("description", raw.get("problem", ""))))
    recommendation = str(
        raw.get(
            "recommendation",
            raw.get("way_to_fix", raw.get("fix", raw.get("remediation", ""))),
        )
    )
    impact = str(raw.get("impact", raw.get("user_impact", "")))
    css_selector = str(raw.get("css_selector", raw.get("selector", "")))

    if not issue and (not element or element == "(unknown)"):
        logger.warning("Dropping malformed finding (no issue/element)")
        return None

    if issue and not recommendation and not impact and len(issue) < 15:
        logger.warning("Dropping truncated finding")
        return None

    return Finding(
        id=str(uuid.uuid4()),
        element=element,
        css_selector=css_selector,
        issue=issue,
        impact=impact,
        recommendation=recommendation,
        severity=normalize_severity(raw.get("severity", raw.get("impact_level", "medium"))),
        source=source,
    )


# ── WCAG-specific response parsing (wraps parse_tool_response) ───────────────

def parse_ai_response(response_data: dict[str, Any]) -> dict[str, Any]:
    """Parse a chat-completions response into a normalized WCAG assessment dict.

    Returns a dict with: ``conformance_level``, ``confidence``,
    ``confidence_reasoning``, ``findings``, ``summary``.
    """
    payload = parse_tool_response(response_data, "report_wcag_assessment")
    if payload:
        return normalize_wcag_assessment(payload)

    content = get_content_text(response_data)
    if content:
        logger.info("Plain text fallback: extracting assessment from model prose")
        return _parse_plain_text_assessment(content)

    raise ValueError("AI response has no tool_calls and no content text")


def _parse_plain_text_assessment(text: str) -> dict[str, Any]:
    """Last-resort parser when the model writes prose instead of a tool call."""
    lower = text.lower()

    if re.search(r"\b(does not support|fail|non-conforman)", lower):
        conformance = ConformanceLevel.DOES_NOT_SUPPORT
    elif re.search(r"\b(partially support|partial conform|some issues)", lower):
        conformance = ConformanceLevel.PARTIALLY_SUPPORTS
    elif re.search(r"\b(not applicable|n/a|no .* content|no .* present)", lower):
        conformance = ConformanceLevel.NOT_APPLICABLE
    elif re.search(r"\b(support|pass|conform|meets)", lower):
        conformance = ConformanceLevel.SUPPORTS
    else:
        conformance = ConformanceLevel.NOT_EVALUATED

    return {
        "conformance_level": conformance,
        "confidence": 0.4,
        "confidence_reasoning": "Extracted from plain text (model did not produce structured output)",
        "findings": [],
        "summary": text.strip(),
    }


def normalize_wcag_assessment(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw assessment payload into canonical form."""
    conformance = normalize_conformance_level(
        str(payload.get("conformance_level", "Not Evaluated"))
    )

    confidence = payload.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0

    raw_findings = payload.get("findings", [])
    findings = [
        f
        for f in (build_finding(r) for r in raw_findings if isinstance(r, dict))
        if f is not None
    ]

    return {
        "conformance_level": conformance,
        "confidence": confidence,
        "confidence_reasoning": str(payload.get("confidence_reasoning", "")),
        "findings": findings,
        "summary": str(payload.get("summary", "")),
    }


# ── Conformance reconciliation (shared helper) ───────────────────────────────

def worst_conformance(levels: list[str | ConformanceLevel]) -> str:
    """Return the worst (most severe) conformance level from a list."""
    order = {
        "Not Applicable": -1,
        "Not Evaluated": -1,
        "Supports": 0,
        "Partially Supports": 1,
        "Does Not Support": 2,
    }
    worst = "Not Evaluated"
    worst_score = -2
    for level in levels:
        value = level.value if hasattr(level, "value") else str(level)
        score = order.get(value, -2)
        if score > worst_score:
            worst_score = score
            worst = value
    return worst


def conformance_from_finding_counts(findings: list[Any]) -> ConformanceLevel:
    """Derive a conformance level from a list of findings by severity.

    high  -> Does Not Support
    medium -> Partially Supports
    low/info only -> Supports
    empty -> Supports
    """
    if not findings:
        return ConformanceLevel.SUPPORTS

    severities = set()
    for f in findings:
        sev = getattr(f, "severity", None)
        if sev is None and isinstance(f, dict):
            sev = f.get("severity")
        if hasattr(sev, "value"):
            sev = sev.value
        severities.add(str(sev).lower() if sev else "medium")

    if "high" in severities or "critical" in severities:
        return ConformanceLevel.DOES_NOT_SUPPORT
    if "medium" in severities or "moderate" in severities:
        return ConformanceLevel.PARTIALLY_SUPPORTS
    return ConformanceLevel.SUPPORTS
