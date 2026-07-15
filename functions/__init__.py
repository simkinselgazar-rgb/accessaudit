"""Shared reusable functions -- the central hub for the WCAG Trusted Tester.

Every piece of reusable logic lives here. No other module makes raw HTTP
calls, parses tool responses, encodes images, or builds prompts. They all
import from this package.
"""
from functions.chunker import (
    chunk_elements,
    chunk_html_by_landmarks,
    chunk_images,
    chunk_text,
)
from functions.embeddings import (
    EmbeddingError,
    cluster_by_similarity,
    cosine_similarity,
    embed,
    embed_batch,
)
from functions.image_analysis import (
    analyze_image,
    describe_image,
    extract_text_from_image,
    verify_alt_text_semantic,
)
from functions.llm import LLMClient, LLMError
from functions.media import encode_image, encode_video, has_track, media_type
from functions.parser import (
    build_finding,
    clean_tool_call_args,
    conformance_from_finding_counts,
    describe_response_shape,
    extract_json_from_text,
    get_content_text,
    loose_json_loads,
    normalize_conformance_level,
    normalize_severity,
    normalize_wcag_assessment,
    parse_ai_response,
    parse_native_tool_call,
    parse_tool_response,
    strip_think_tags,
    worst_conformance,
)
from functions.prompt import (
    build_page_context_hint,
    build_system_prompt,
    build_user_prompt,
    format_elements_for_prompt,
    load_criterion_prompt,
    summarize_a11y_tree,
)
from functions.bypass_log import (
    CATEGORY_CONFIG_FALLBACK,
    CATEGORY_FALLBACK_ENDPOINT,
    CATEGORY_FALLBACK_MODEL,
    CATEGORY_HTTP_ERROR,
    CATEGORY_PARSE_FAIL,
    CATEGORY_RESUME_REUSE,
    CATEGORY_RETRY_EXHAUSTED,
    CATEGORY_SILENT_EXCEPT,
    CATEGORY_SKIPPED_DATA,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    StrictModeAbort,
    log_bypass,
    summarize_bypasses,
)
from functions.code_analyzer import analyze_page_code, findings_for_sc
from functions.js_ast_filter import (
    filter_accessibility_code,
)
from functions.sc_retrieval import (
    build_pattern_embeddings,
    format_retrieved_patterns,
    retrieve_for_sc,
)
from functions.tools import (
    CODE_PATTERN_INVENTORY_TOOL,
    EXPLORATION_TOOL,
    JUDGE_TOOL,
    LINK_EXTRACTOR_TOOL,
    PAGE_SELECTOR_TOOL,
    SYNTHESIS_TOOL,
    WCAG_ASSESSMENT_TOOL,
)

__all__ = [
    # LLM client
    "LLMClient",
    "LLMError",
    # Embeddings (cross-page consistency + dedup)
    "EmbeddingError",
    "cluster_by_similarity",
    "cosine_similarity",
    "embed",
    "embed_batch",
    # Image analysis (VLM captioning, OCR, alt-text verification)
    "analyze_image",
    "describe_image",
    "extract_text_from_image",
    "verify_alt_text_semantic",
    # Media
    "encode_image",
    "encode_video",
    "has_track",
    "media_type",
    # Parser
    "parse_tool_response",
    "parse_ai_response",
    "build_finding",
    "clean_tool_call_args",
    "strip_think_tags",
    "get_content_text",
    "normalize_conformance_level",
    "normalize_severity",
    "worst_conformance",
    "conformance_from_finding_counts",
    "loose_json_loads",
    "parse_native_tool_call",
    "describe_response_shape",
    "extract_json_from_text",
    "normalize_wcag_assessment",
    # Chunker
    "chunk_elements",
    "chunk_html_by_landmarks",
    "chunk_images",
    "chunk_text",
    # Prompt
    "build_system_prompt",
    "build_user_prompt",
    "format_elements_for_prompt",
    "build_page_context_hint",
    "load_criterion_prompt",
    "summarize_a11y_tree",
    # Tool schemas
    "WCAG_ASSESSMENT_TOOL",
    "JUDGE_TOOL",
    "EXPLORATION_TOOL",
    "LINK_EXTRACTOR_TOOL",
    "PAGE_SELECTOR_TOOL",
    "SYNTHESIS_TOOL",
    "CODE_PATTERN_INVENTORY_TOOL",
    # Code analyzer (Phase 1 per-page code cache)
    "analyze_page_code",
    "findings_for_sc",
    # Layer 2: JS AST / regex prefilter
    "filter_accessibility_code",
    # Layer 3: semantic retrieval for the judge
    "build_pattern_embeddings",
    "format_retrieved_patterns",
    "retrieve_for_sc",
    # Per-review bypass telemetry
    "log_bypass",
    "summarize_bypasses",
    "StrictModeAbort",
    "CATEGORY_CONFIG_FALLBACK",
    "CATEGORY_FALLBACK_ENDPOINT",
    "CATEGORY_FALLBACK_MODEL",
    "CATEGORY_HTTP_ERROR",
    "CATEGORY_PARSE_FAIL",
    "CATEGORY_RESUME_REUSE",
    "CATEGORY_RETRY_EXHAUSTED",
    "CATEGORY_SILENT_EXCEPT",
    "CATEGORY_SKIPPED_DATA",
    "SEVERITY_HIGH",
    "SEVERITY_MEDIUM",
    "SEVERITY_LOW",
]
