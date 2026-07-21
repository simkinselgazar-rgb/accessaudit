"""JSON tool schemas for structured LLM output.

Every tool schema used anywhere in the system lives here. No file defines
its own schema inline.
"""
from __future__ import annotations


# ── Shared schema fragments ──────────────────────────────────────────────────

# Structured measurement claims on a finding. The model must report every
# concrete MEASURED numeric value its prose cites in this structured form,
# so the system can verify each value against the deterministic capture
# instead of regex-parsing prose. A finding that cites no measurement
# provides an empty array. WCAG threshold/requirement values (the "4.5:1"
# in "below the 4.5:1 minimum") are NOT measurements and must NOT appear
# here. The post-judge claim validator checks every entry against the
# page's captured measurements and demotes findings whose entries do not
# match.
_CITED_MEASUREMENTS_SCHEMA = {
    "type": "array",
    "description": (
        "Every concrete deterministic FACT this finding's `issue` text "
        "relies on. Two kinds:\n"
        "  1. MEASURED values -- contrast ratios, pixel dimensions, "
        "computed CSS values.\n"
        "  2. STATE facts -- a boolean or categorical fact the finding "
        "asserts about an element or about the page: whether a form "
        "field is required, whether an element's accessible name "
        "includes its visible text, whether the page has animation / "
        "auto-refresh / autoplay / marquee, an element's computed CSS "
        "position.\n"
        "Provide one entry per measured value OR asserted state the "
        "prose depends on. Use an empty array when the finding relies "
        "on no such fact. Record ONLY values read from the "
        "deterministic blocks in the prompt (DYNAMIC CONTENT, FORM "
        "FIELDS, ANDI INTERACTIVE AUDIT, the contrast / target-size "
        "blocks, etc.). NEVER record a WCAG threshold/requirement (the "
        "'4.5:1' in 'below the 4.5:1 minimum' is the requirement, not a "
        "measurement). Every entry is verified against the captured "
        "facts; an entry that contradicts the capture marks the finding "
        "an unverified inference."
    ),
    "items": {
        "type": "object",
        "required": ["selector", "metric", "value"],
        "properties": {
            "selector": {
                "type": "string",
                "description": (
                    "CSS selector of the element this fact is about. For "
                    "a page-level state fact (animation/auto-refresh), "
                    "use the page's body selector or an empty string."
                ),
            },
            "metric": {
                "type": "string",
                "description": (
                    "What was measured or asserted. Use the EXACT label "
                    "shown in the prompt's deterministic blocks. Numeric: "
                    "'contrast_ratio', 'target_width_px', "
                    "'target_height_px'. State: 'position' (computed CSS "
                    "position), 'required' (form-field required state, "
                    "from FORM FIELDS), 'name_inc_visible' (accessible "
                    "name includes visible text, from ANDI INTERACTIVE "
                    "AUDIT), 'hasAnimations' / 'hasAutoRefresh' / "
                    "'hasMarquee' / 'hasAutoplayVideo' / 'hasAutoplayAudio' "
                    "(page state, from DYNAMIC CONTENT)."
                ),
            },
            "value": {
                "type": "string",
                "description": (
                    "The measured value or asserted state as a string -- "
                    "e.g. '4.44' for a contrast ratio, '18' for a pixel "
                    "dimension, 'fixed' for a position, 'true' / 'false' "
                    "for a boolean state. Always a string so the field "
                    "has one stable type; the system interprets it "
                    "numerically when the metric is numeric."
                ),
            },
        },
    },
}


# ── WCAG Assessment (used by Visual AI and Code AI) ──────────────────────────

WCAG_ASSESSMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "report_wcag_assessment",
        "description": (
            "Report the result of a WCAG accessibility assessment for a single "
            "success criterion. Findings must meet Section 508 reporting "
            "standards: specific element location, clear issue description, "
            "user impact with affected disability groups, and a reference to "
            "the WCAG conformance requirement that is not met."
        ),
        "parameters": {
            "type": "object",
            "required": [
                "conformance_level",
                "confidence",
                "confidence_reasoning",
                "findings",
                "summary",
            ],
            "properties": {
                "conformance_level": {
                    "type": "string",
                    "enum": [
                        "Supports",
                        "Partially Supports",
                        "Does Not Support",
                        "Not Applicable",
                        "Not Evaluated",
                    ],
                    "description": (
                        "VPAT conformance level. Use 'Not Evaluated' when "
                        "you lack sufficient evidence rather than guessing."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Confidence 0-1 based on evidence quality and coverage.",
                },
                "confidence_reasoning": {
                    "type": "string",
                    "description": "Explain what evidence supports your assessment and any caveats.",
                },
                "findings": {
                    "type": "array",
                    "description": "Individual accessibility findings specific enough to locate and describe.",
                    "items": {
                        "type": "object",
                        "required": [
                            "element",
                            "css_selector",
                            "issue",
                            "impact",
                            "recommendation",
                            "severity",
                            "cited_measurements",
                        ],
                        "properties": {
                            "element": {
                                "type": "string",
                                "description": (
                                    "WHERE on the page, in prose a human reader can use to find it. "
                                    "Example: 'The \"Learn more\" link in the second paragraph of the "
                                    "main content area' or 'The search input in the top navigation'. "
                                    "Do NOT put a CSS selector here -- that goes in css_selector."
                                ),
                            },
                            "cited_measurements": _CITED_MEASUREMENTS_SCHEMA,
                            "css_selector": {
                                "type": "string",
                                "description": (
                                    "The most specific CSS selector or element ID you can copy from "
                                    "the HTML / programmatic data / element inventory in the user "
                                    "prompt. Examples: '#main-nav .search-btn', 'img[alt=\"\"]', "
                                    "'a.btn-primary:nth-of-type(2)'. If you absolutely cannot "
                                    "determine a selector from the evidence, use the empty string "
                                    "'' -- never omit the field and never invent selectors that "
                                    "don't appear in the source evidence."
                                ),
                            },
                            "issue": {
                                "type": "string",
                                "description": (
                                    "What is wrong. MUST cite the specific WCAG criterion number "
                                    "(e.g. 'WCAG 1.4.3 Contrast Minimum requires...') and name the "
                                    "specific failure condition. Include measured values where "
                                    "relevant (e.g. 'contrast ratio 2.1:1, below the 4.5:1 required "
                                    "by WCAG 1.4.3')."
                                ),
                            },
                            "impact": {
                                "type": "string",
                                "description": (
                                    "WHO is affected and HOW. Name specific disability groups "
                                    "(blind, low vision, color blind, motor impaired, cognitive) "
                                    "AND specific assistive technologies (JAWS, NVDA, VoiceOver, "
                                    "keyboard-only, screen magnifier) where applicable."
                                ),
                            },
                            "recommendation": {
                                "type": "string",
                                "description": (
                                    "The WCAG conformance requirement that is not met, stated as "
                                    "what passing looks like. Always name the criterion, e.g. "
                                    "'WCAG 2.4.7 requires a visible focus indicator on all "
                                    "keyboard-focusable elements'. Do NOT provide code fixes."
                                ),
                            },
                            "severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low", "info"],
                                "description": (
                                    "high = blocks access, medium = significant barrier, "
                                    "low = minor, info = best practice."
                                ),
                            },
                            "internal_remediation_note": {
                                "type": "string",
                                "description": (
                                    "INTERNAL ONLY: Provide a technical 'how-to-fix' guide "
                                    "for developers. Include specific code examples, ARIA "
                                    "attributes, or CSS properties needed to remediate "
                                    "the issue. This field will be stripped from client-facing "
                                    "exports."
                                ),
                            },
                        },
                    },
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "VPAT-style 'Remarks and Explanations' paragraph for the ACR report. "
                        "Summarize conformance status, observed issues, and affected areas."
                    ),
                },
                "insufficient_evidence_reason": {
                    "type": "string",
                    "description": (
                        "If you set conformance_level to 'Not Evaluated', explain what "
                        "evidence is missing or what information is contradictory that "
                        "prevents you from making a determination. Be specific about "
                        "what data you would need."
                    ),
                },
                "conflicting_information": {
                    "type": "string",
                    "description": (
                        "If the programmatic data contradicts what you see in the "
                        "screenshots, or if different data sources give conflicting "
                        "signals, describe the conflict here. For example: 'The element "
                        "inventory shows alt text present but the screenshot appears to "
                        "show no image content' or 'The tab walk marks this element as "
                        "VISIBLE but the focus screenshots show no change'."
                    ),
                },
            },
        },
    },
}


# ── Judge (final arbiter that produces the VPAT finding text) ────────────────

JUDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "report_judgment",
        "description": (
            "Report the final accessibility conformance judgment for the VPAT/ACR report."
        ),
        "parameters": {
            "type": "object",
            "required": [
                "conformance_level",
                "confidence",
                "reasoning",
                "final_findings",
                "rejected_findings",
                "vpat_summary",
            ],
            "properties": {
                "conformance_level": {
                    "type": "string",
                    "enum": [
                        "Supports",
                        "Partially Supports",
                        "Does Not Support",
                        "Not Applicable",
                        "Not Evaluated",
                    ],
                    "description": (
                        "Use 'Not Evaluated' ONLY when the evidence is "
                        "insufficient to judge at all (capture failure, "
                        "missing screenshots, unreadable data) -- never "
                        "for a borderline call you could still make from "
                        "the evidence provided."
                    ),
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reasoning": {
                    "type": "string",
                    "description": "Internal reasoning for the auditor's review, not for the VPAT.",
                },
                "final_findings": {
                    "type": "array",
                    "description": (
                        "The FINAL findings for this criterion, rewritten in professional "
                        "VPAT/ACR language. Each finding goes directly into the report. "
                        "Only include findings relevant to THIS criterion."
                    ),
                    "items": {
                        "type": "object",
                        "required": [
                            "element",
                            "css_selector",
                            "issue",
                            "impact",
                            "recommendation",
                            "severity",
                            "source",
                            "cited_measurements",
                        ],
                        "properties": {
                            "element": {
                                "type": "string",
                                "description": (
                                    "WHERE on the page -- visual description for a human reader. "
                                    "Example: 'In the top navigation bar, the search form input field'."
                                ),
                            },
                            "cited_measurements": _CITED_MEASUREMENTS_SCHEMA,
                            "css_selector": {
                                "type": "string",
                                "description": (
                                    "Technical CSS selector or element ID for developers. "
                                    "Example: '#UA_BrandBar_SearchBtn'."
                                ),
                            },
                            "issue": {
                                "type": "string",
                                "description": (
                                    "WHAT is wrong -- specific, evidence-based, references "
                                    "the WCAG requirement. Include measured values when "
                                    "available (e.g. 'contrast ratio is 2.1:1, below 4.5:1')."
                                ),
                            },
                            "impact": {
                                "type": "string",
                                "description": (
                                    "WHO is affected and HOW -- name specific disability groups "
                                    "(blind, low vision, motor impaired) AND specific assistive "
                                    "technologies (JAWS, NVDA, VoiceOver, keyboard-only)."
                                ),
                            },
                            "recommendation": {
                                "type": "string",
                                "description": (
                                    "The WCAG conformance requirement that is not met. "
                                    "State what passing looks like. Do NOT provide code fixes."
                                ),
                            },
                            "severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low", "info"],
                            },
                            "internal_remediation_note": {
                                "type": "string",
                                "description": (
                                    "INTERNAL ONLY: Technical remediation guide for developers. "
                                    "Include exact code fixes (e.g. 'Add aria-expanded=true'). "
                                    "This field is for internal team exports only and is "
                                    "automatically stripped from the final client VPAT."
                                ),
                            },
                            "source": {
                                "type": "string",
                                "pattern": (
                                    "^(programmatic|axe|andi|htmlcs|ibm_eac|visual_ai|"
                                    "code_ai|at_sim|judge_inference)"
                                    "(,\\s*(programmatic|axe|andi|htmlcs|ibm_eac|"
                                    "visual_ai|code_ai|at_sim|judge_inference))*$"
                                ),
                                "description": (
                                    "ATTRIBUTION INTEGRITY (load-bearing for verdict trust):\n"
                                    "One source tag, or a comma-separated list when one output "
                                    "finding merges corroborating inputs from multiple sources "
                                    "(e.g. 'axe, htmlcs, ibm_eac').\n"
                                    "Use the EXACT source tag from the input finding you are "
                                    "synthesizing/rewording. Do NOT relabel.\n"
                                    "- 'programmatic': came from a deterministic check\n"
                                    "- 'axe': came from axe-core (Deque)\n"
                                    "- 'andi': came from ANDI per-text-node analysis\n"
                                    "- 'htmlcs': came from HTML_CodeSniffer (Squiz Labs)\n"
                                    "- 'ibm_eac': came from IBM Equal Access checker\n"
                                    "- 'visual_ai': came from a visual AI source\n"
                                    "- 'code_ai': came from the code-pattern AI source\n"
                                    "- 'at_sim': came from a screen-reader / AT-simulation source "
                                    "that probed page behaviour with assistive tech. NOTE: "
                                    "keyboard-roundtrip findings (focus return, escape "
                                    "dismissibility, tab-trap detection) come from the "
                                    "deterministic Playwright probe -- label those "
                                    "'programmatic', NOT 'at_sim'. Reserve 'at_sim' for "
                                    "findings an AT simulator specifically produced.\n"
                                    "- 'judge_inference': YOU added this finding from your own "
                                    "  reasoning over the evidence -- no input source produced it. "
                                    "  Use this WHENEVER you are adding a finding that does not "
                                    "  exist verbatim in the input set. Do not hide your own "
                                    "  inferences under another tag."
                                ),
                            },
                        },
                    },
                },
                "rejected_findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "reason": {"type": "string"},
                            "correct_criterion": {"type": "string"},
                        },
                    },
                },
                "vpat_summary": {
                    "type": "string",
                    "description": "VPAT 'Remarks and Explanations' text (1-3 sentences).",
                },
            },
        },
    },
}


# ── Exploration (Phase 2 visual AI explorer) ─────────────────────────────────

EXPLORATION_TOOL = {
    "type": "function",
    "function": {
        "name": "report_exploration_result",
        "description": "Report what happened when an element was interacted with.",
        "parameters": {
            "type": "object",
            "required": ["interaction_response", "explore_deeper"],
            "properties": {
                "interaction_response": {
                    "type": "string",
                    "enum": [
                        "none",
                        "tooltip",
                        "dropdown",
                        "submenu",
                        "modal",
                        "accordion",
                        "overlay",
                        "state_change",
                        "tab_panel",
                        "carousel_change",
                        "navigation",
                        "focus_visible",
                        "error_message",
                    ],
                    "description": (
                        "What visual change occurred after the interaction. Compare the "
                        "screenshots carefully:\n"
                        "- 'none': no visible change between screenshots\n"
                        "- 'tooltip': a small text popup appeared near the element\n"
                        "- 'dropdown': a dropdown/select menu opened below the element\n"
                        "- 'submenu': a submenu expanded from a menu item\n"
                        "- 'modal': a dialog/modal/lightbox appeared over the page\n"
                        "- 'accordion': a content section expanded or collapsed\n"
                        "- 'overlay': an overlay appeared on the page\n"
                        "- 'state_change': visual state changed (color, size, content swap)\n"
                        "- 'tab_panel': a tab panel switched its visible content\n"
                        "- 'carousel_change': a carousel/slider advanced to a different slide\n"
                        "- 'navigation': the page navigated to a different URL\n"
                        "- 'focus_visible': a focus ring/outline appeared on the element\n"
                        "- 'error_message': an error message appeared\n"
                        "Pick the MOST SPECIFIC option that applies."
                    ),
                },
                "new_elements_found": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "selector": {
                                "type": "string",
                                "description": (
                                    "A UNIQUE CSS selector that points to THIS specific new "
                                    "element -- not its parent, not its container. If you saw "
                                    "7 new links inside a dropdown, each link must have its "
                                    "own selector identifying that link (e.g. "
                                    "'nav ul li:nth-of-type(1) ul li:nth-of-type(3) a'). "
                                    "NEVER reuse the parent button or container selector for "
                                    "multiple child items -- that produces ambiguous selectors "
                                    "the test pipeline cannot click."
                                ),
                            },
                            "type": {
                                "type": "string",
                                "description": (
                                    "Tag/role of the new element: 'a' for plain links, "
                                    "'button' for buttons, 'input' for fields, etc. Use the "
                                    "actual semantic, not the visual style."
                                ),
                            },
                            "text": {
                                "type": "string",
                                "description": "Visible text or accessible name of THIS element.",
                            },
                            "should_explore": {
                                "type": "boolean",
                                "description": (
                                    "true ONLY if this element will itself trigger another "
                                    "state change worth re-testing (a button that opens a "
                                    "deeper menu, a control that toggles a panel, a tab that "
                                    "swaps content). Plain navigation links (<a href> that "
                                    "just goes to another page), static images, and labels "
                                    "MUST be false -- they have no further state to explore."
                                ),
                            },
                        },
                        "required": ["selector", "text", "should_explore"],
                    },
                    "description": (
                        "New interactive elements that appeared after the action. Each entry "
                        "must describe ONE element with its OWN unique selector. Do not put "
                        "the same selector on multiple entries."
                    ),
                },
                "focus_indicator_visible": {
                    "type": "boolean",
                    "description": (
                        "TRUE when the focused element shows a visually "
                        "distinguishable focus indicator (a ring, outline, "
                        "border, background shift, or any clear visual "
                        "marker) that a sighted keyboard user would notice. "
                        "FALSE when no such indicator is visible.\n\n"
                        "CONSISTENCY RULE (must hold): when "
                        "interaction_response is set to 'focus_visible', "
                        "this boolean MUST be TRUE -- both fields are "
                        "answering the same question. When "
                        "interaction_response is 'none' AND the action "
                        "was 'focus', this boolean MUST be FALSE. For "
                        "other interaction_response values (modal, "
                        "dropdown, etc.) the boolean reflects whether a "
                        "focus indicator was ALSO visible alongside the "
                        "main change."
                    ),
                },
                "state_change_detected": {"type": "boolean"},
                "accessibility_observations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Any accessibility issues observed: missing focus indicator, "
                        "no keyboard access, tooltip with no role, dropdown without "
                        "aria-expanded, etc."
                    ),
                },
                "explore_deeper": {
                    "type": "boolean",
                    "description": "Should we explore the new elements that appeared?",
                },
            },
        },
    },
}


# ── Code pattern inventory (Phase 1 code analyzer) ─────────────────────────
#
# Used by ``functions.code_analyzer.analyze_page_code`` for the once-per-page
# code-pattern enumeration. Each pattern the model finds gets SC-tagged so
# per-SC judges can filter the cache instead of re-reading all source.

CODE_PATTERN_INVENTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "report_code_patterns",
        "description": (
            "Enumerate every accessibility-relevant pattern in this code "
            "chunk. For each pattern, list every WCAG SC it could be "
            "evidence of (sc_ids). Do NOT judge a specific criterion -- "
            "just produce a neutral inventory. The per-SC judge that "
            "consumes this output filters down to its own criterion."
        ),
        "parameters": {
            "type": "object",
            "required": ["patterns"],
            "properties": {
                "patterns": {
                    "type": "array",
                    "description": (
                        "Zero or more patterns found in this chunk. Empty "
                        "list is valid when the chunk has no accessibility "
                        "signal (pure analytics, webpack internals, etc.)."
                    ),
                    "items": {
                        "type": "object",
                        "required": [
                            "pattern_type",
                            "sc_ids",
                            "element",
                            "issue",
                            "raw_evidence",
                            "severity",
                        ],
                        "properties": {
                            "pattern_type": {
                                "type": "string",
                                "description": (
                                    "Short label describing the pattern, "
                                    "e.g. 'image_no_alt', 'onclick_no_keydown', "
                                    "'video_no_track', 'focus_outline_removed'."
                                ),
                            },
                            "sc_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "WCAG 2.2 criterion IDs this pattern is "
                                    "evidence for. A pattern MAY tag multiple "
                                    "SCs and should when the pattern legitimately "
                                    "implicates several (e.g. missing form label "
                                    "-> ['1.3.1','3.3.2','4.1.2']). Prefer "
                                    "over-tagging to under-tagging."
                                ),
                            },
                            "element": {
                                "type": "string",
                                "description": (
                                    "Spatial / semantic description of the "
                                    "element the pattern points at."
                                ),
                            },
                            "css_selector": {
                                "type": "string",
                                "description": (
                                    "Specific CSS selector copied verbatim "
                                    "from the code chunk. Empty when the "
                                    "pattern is about JS behavior with no "
                                    "single DOM anchor."
                                ),
                            },
                            "issue": {
                                "type": "string",
                                "description": (
                                    "One-sentence description of what the "
                                    "pattern is and why it matters for "
                                    "accessibility."
                                ),
                            },
                            "raw_evidence": {
                                "type": "string",
                                "description": (
                                    "Exact code snippet (3-20 lines) from "
                                    "this chunk that triggered the pattern. "
                                    "Quote verbatim -- do not paraphrase. "
                                    "The per-SC judge will verify the "
                                    "pattern against this snippet."
                                ),
                            },
                            "severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low", "info"],
                                "description": (
                                    "Initial severity estimate. The per-SC "
                                    "judge may adjust based on its own "
                                    "criterion rules."
                                ),
                            },
                        },
                    },
                },
            },
        },
    },
}


# ── Link extractor (crawl fallback) ─────────────────────────────────────────

FINDING_DEDUP_TOOL = {
    "type": "function",
    "function": {
        "name": "report_finding_clusters",
        "description": (
            "Group the supplied findings into clusters where each cluster "
            "represents ONE distinct accessibility issue. Two findings belong "
            "to the same cluster when they describe the SAME root cause on the "
            "SAME element -- regardless of how the css_selector or issue text "
            "is worded. The orchestrator merges each cluster mechanically: "
            "worst severity wins, sources are unioned, the most precise "
            "selector and clearest issue text are kept. Your only job is the "
            "semantic grouping decision."
        ),
        "parameters": {
            "type": "object",
            "required": ["clusters"],
            "properties": {
                "clusters": {
                    "type": "array",
                    "description": (
                        "One entry per distinct issue. Singletons are allowed "
                        "and expected -- a cluster of size 1 means that "
                        "finding does not duplicate any other."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["finding_indices"],
                        "properties": {
                            "finding_indices": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": (
                                    "0-based indices into the input findings "
                                    "list for the findings that belong to "
                                    "this cluster. Must be non-empty."
                                ),
                            },
                            "summary": {
                                "type": "string",
                                "description": (
                                    "Optional one-line description of the "
                                    "shared issue (auditor-readable, e.g. "
                                    "'hero image filename used as alt')."
                                ),
                            },
                        },
                    },
                },
            },
        },
    },
}


LINK_EXTRACTOR_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_navigation_links",
        "description": (
            "Extract every internal navigation URL from the rendered HTML "
            "provided in the user prompt. Inspect navigation menus, headers, "
            "footers, sidebars, hamburger/dropdown menus, and links embedded "
            "in JavaScript/onclick handlers or data-* attributes."
        ),
        "parameters": {
            "type": "object",
            "required": ["urls"],
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "description": (
                            "Absolute HTTP/HTTPS URL for one internal link. "
                            "Every entry must start with http:// or https:// "
                            "and point to the same site as the base URL."
                        ),
                    },
                    "description": "Every internal navigation URL found on the page.",
                },
            },
        },
    },
}


# ── Page selector (crawl) ────────────────────────────────────────────────────

PAGE_SELECTOR_TOOL = {
    "type": "function",
    "function": {
        "name": "select_pages",
        "description": (
            "Select the most important pages from a crawl for WCAG accessibility "
            "testing, with a per-page rationale."
        ),
        "parameters": {
            "type": "object",
            "required": ["selected", "rationale"],
            "properties": {
                "selected": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["url", "reason"],
                        "properties": {
                            "url": {"type": "string"},
                            "reason": {
                                "type": "string",
                                "description": "Why this specific page matters for THIS site's users.",
                            },
                        },
                    },
                },
                "rationale": {
                    "type": "string",
                    "description": "2-3 sentence overall explanation of why these pages were chosen.",
                },
            },
        },
    },
}


# ── Synthesis (executive summary across all criteria) ────────────────────────

SYNTHESIS_TOOL = {
    "type": "function",
    "function": {
        "name": "report_synthesis",
        "description": "Report the executive synthesis of all WCAG test results.",
        "parameters": {
            "type": "object",
            "required": ["executive_summary", "systemic_issues", "priority_order", "vpat_remarks"],
            "properties": {
                "executive_summary": {
                    "type": "string",
                    "description": "2-4 paragraph executive summary of conformance posture.",
                },
                "systemic_issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string"},
                            "affected_criteria": {"type": "array", "items": {"type": "string"}},
                            "severity": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                },
                "priority_order": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Criterion IDs ordered by remediation impact (highest first).",
                },
                "vpat_remarks": {
                    "type": "object",
                    "description": "Dict of criterion_id -> VPAT remarks (1-3 sentences each).",
                    "additionalProperties": {"type": "string"},
                },
            },
        },
    },
}


# ─── Final reviewer tools (one focused decision per call) ────────────────────
#
# These six tools are used by analysis/final_reviewer.py — a Pro-tier pass
# that runs ONCE per review on the complete ACR. Each tool corresponds to a
# single, narrow decision. Splitting the work this way keeps each model call
# focused and makes failure isolation possible: a malformed tone-rewrite
# response cannot poison the structural-completeness call.

REVIEWER_STRUCTURAL_TOOL = {
    "type": "function",
    "function": {
        "name": "report_structural_review",
        "description": (
            "Identify any SC results that are structurally incomplete: "
            "missing verdict, missing summary, impossible source-count "
            "combinations, or schema violations. Do NOT recalibrate verdicts "
            "here -- only flag missing or malformed structural fields."
        ),
        "parameters": {
            "type": "object",
            "required": ["missing_verdicts", "missing_summaries", "schema_violations"],
            "properties": {
                "missing_verdicts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "criterion_ids whose conformance_level is missing or invalid.",
                },
                "missing_summaries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "criterion_ids whose summary is empty when findings exist.",
                },
                "schema_violations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["criterion_id", "issue"],
                        "properties": {
                            "criterion_id": {"type": "string"},
                            "issue": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


REVIEWER_CALIBRATION_TOOL = {
    "type": "function",
    "function": {
        "name": "report_calibration_review",
        "description": (
            "For each SC, decide whether the final conformance_level matches "
            "the severity distribution of accepted findings. Suggest a "
            "different verdict ONLY when the existing one is clearly wrong "
            "given the finding severities. Rules: 0 findings or only "
            "info/low -> Supports; medium severity present -> Partially "
            "Supports; one or more high severity -> Does Not Support; "
            "criterion does not apply -> Not Applicable. Do NOT change "
            "verdicts that are already correct."
        ),
        "parameters": {
            "type": "object",
            "required": ["recalibrations"],
            "properties": {
                "recalibrations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["criterion_id", "current_verdict", "suggested_verdict", "reason"],
                        "properties": {
                            "criterion_id": {"type": "string"},
                            "current_verdict": {"type": "string"},
                            "suggested_verdict": {
                                "type": "string",
                                "enum": [
                                    "Supports", "Partially Supports", "Does Not Support",
                                    "Not Applicable", "Not Evaluated",
                                ],
                            },
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


REVIEWER_CONTRADICTION_TOOL = {
    "type": "function",
    "function": {
        "name": "report_contradiction_review",
        "description": (
            "Identify pairs or groups of findings that describe the SAME "
            "element in CONTRADICTORY ways across different SCs. Different "
            "SCs flagging the same element for different reasons is normal "
            "and NOT a contradiction. A contradiction is when one SC says "
            "the element does X and another SC says it does NOT do X."
        ),
        "parameters": {
            "type": "object",
            "required": ["contradictions"],
            "properties": {
                "contradictions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["element", "conflicting_findings", "explanation"],
                        "properties": {
                            "element": {"type": "string"},
                            "conflicting_findings": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["criterion_id", "claim"],
                                    "properties": {
                                        "criterion_id": {"type": "string"},
                                        "claim": {"type": "string"},
                                    },
                                },
                            },
                            "explanation": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


REVIEWER_CITATION_TOOL = {
    "type": "function",
    "function": {
        "name": "report_citation_review",
        "description": (
            "Inspect every WCAG citation, threshold, and normative claim in "
            "the findings. Flag every case where a number, threshold, or "
            "criterion reference is factually wrong (e.g. 'WCAG 1.4.10 "
            "requires 256px' -- it requires 320). Use your knowledge of "
            "WCAG 2.0/2.1/2.2 to verify each citation. Only flag clear "
            "factual errors, not stylistic differences."
        ),
        "parameters": {
            "type": "object",
            "required": ["citation_errors"],
            "properties": {
                "citation_errors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["criterion_id", "finding_index", "claim", "correct_value", "severity"],
                        "properties": {
                            "criterion_id": {"type": "string"},
                            "finding_index": {"type": "integer"},
                            "claim": {"type": "string"},
                            "correct_value": {"type": "string"},
                            "severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                    },
                },
            },
        },
    },
}


REVIEWER_TONE_TOOL = {
    "type": "function",
    "function": {
        "name": "report_tone_review",
        "description": (
            "Inspect each SC's vpat_summary and finding text for VPAT 2.5 "
            "ACR compliance: formal third-person voice, factual claims, no "
            "second-person, no colloquialisms, no implementation suggestions "
            "in issue/recommendation. Suggest verbatim rewrites only where "
            "the prose violates these conventions. Do NOT rewrite text that "
            "is already compliant."
        ),
        "parameters": {
            "type": "object",
            "required": ["rewrites"],
            "properties": {
                "rewrites": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["criterion_id", "field", "original", "suggested", "reason"],
                        "properties": {
                            "criterion_id": {"type": "string"},
                            "field": {
                                "type": "string",
                                "enum": ["summary", "issue", "impact", "recommendation", "internal_remediation_note"],
                            },
                            "finding_index": {"type": "integer"},
                            "original": {"type": "string"},
                            "suggested": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


WIDGET_DISCOVERY_TOOL = {
    "type": "function",
    "function": {
        "name": "report_widget_discovery",
        "description": (
            "Report interactive composite widgets found on a web page that require "
            "arrow-key navigation (not just Tab) per WAI-ARIA Authoring Practices. "
            "Call this after examining the page screenshot and DOM structure. "
            "Only include widgets NOT already in the 'already detected' list supplied "
            "in the prompt. Returns a list of widget objects each with selector, type, "
            "expected keyboard keys, and first-item selector for initial focus."
        ),
        "parameters": {
            "type": "object",
            "required": ["widgets"],
            "properties": {
                "widgets": {
                    "type": "array",
                    "description": (
                        "Composite widgets not yet detected. Empty array if all "
                        "widgets are already covered."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["type", "selector", "keys", "reason"],
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "tablist", "accordion", "menu", "combobox",
                                    "slider", "tree", "listbox", "grid",
                                    "carousel", "date_picker", "custom",
                                ],
                                "description": "Widget type per WAI-ARIA pattern.",
                            },
                            "selector": {
                                "type": "string",
                                "description": (
                                    "CSS selector for the widget container or, if "
                                    "there is no container, the first interactive item."
                                ),
                            },
                            "first_item_selector": {
                                "type": "string",
                                "description": (
                                    "CSS selector for the first focusable item inside "
                                    "the widget. Omit if same as selector."
                                ),
                            },
                            "keys": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Keyboard keys that should move focus/state inside "
                                    "this widget, e.g. ['ArrowRight', 'ArrowLeft'] "
                                    "for a tablist or ['ArrowDown', 'Escape'] for a "
                                    "combobox."
                                ),
                            },
                            "state_attr": {
                                "type": "string",
                                "description": (
                                    "ARIA attribute that tracks selection/expansion "
                                    "state, e.g. 'aria-selected', 'aria-expanded'. "
                                    "Leave empty if unsure."
                                ),
                            },
                            "reason": {
                                "type": "string",
                                "description": (
                                    "One sentence explaining why this widget needs "
                                    "arrow-key testing and how you identified it."
                                ),
                            },
                        },
                    },
                },
            },
        },
    },
}


WIDGET_EXPLORATION_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "keyboard_exploration_action",
        "description": (
            "Return the next keyboard interaction to perform on the page, or signal "
            "that all interactive widgets have been tested. The caller will execute "
            "the action via a real browser, take a screenshot of the result, and call "
            "this tool again with updated test results until 'done' is returned. "
            "Use this to drive iterative testing: hover to reveal hidden menus, "
            "click/Enter to open dropdowns before testing arrow keys inside them, "
            "Tab into a widget then arrow through all items, etc."
        ),
        "parameters": {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["continue", "done"],
                    "description": (
                        "'done' when all interactive composite widgets on the visible "
                        "page have been tested or there is nothing more to probe. "
                        "'continue' when there is still an untested or partially-tested "
                        "widget to probe. If you have no concrete next action — return "
                        "'done', NEVER 'continue' with an empty selector or empty keys."
                    ),
                },
                "selector": {
                    "type": "string",
                    "description": (
                        "CSS selector for the element to focus first. "
                        "REQUIRED and must be non-empty when status='continue'. "
                        "Omit entirely when status='done'."
                    ),
                },
                "pre_action": {
                    "type": "string",
                    "enum": ["none", "hover", "click", "Enter", "Space"],
                    "description": (
                        "Action to perform BEFORE pressing the navigation keys. "
                        "'hover' — reveal CSS-hover-triggered content. "
                        "'click' — open a dropdown/menu with a mouse click. "
                        "'Enter'/'Space' — activate the focused element via keyboard "
                        "before navigating inside it. "
                        "'none' — go straight to key testing from the focused state."
                    ),
                },
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "Keys to press in order. Use Playwright key names: "
                        "ArrowRight, ArrowLeft, ArrowUp, ArrowDown, Enter, Space, "
                        "Escape, Tab, Shift+Tab. Include enough presses to walk through "
                        "all items — e.g. 5 ArrowRight presses for a 6-tab tablist. "
                        "REQUIRED and must contain at least one key when status='continue'. "
                        "Omit entirely when status='done' — never send an empty array."
                    ),
                },
                "widget_type": {
                    "type": "string",
                    "description": (
                        "Type of widget being tested: tablist, menu, combobox, tree, "
                        "accordion, slider, carousel, grid, custom, etc."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence: what widget you spotted, why it needs this "
                        "specific interaction, and what correct behavior looks like."
                    ),
                },
            },
        },
    },
}


REVIEWER_SYNTHESIS_TOOL = {
    "type": "function",
    "function": {
        "name": "report_final_synthesis",
        "description": (
            "Write the executive summary, identify systemic issues spanning "
            "multiple SCs, and recommend a remediation priority order. You "
            "receive the outputs of the prior 5 reviewer calls so you can "
            "incorporate their flagged issues as known facts."
        ),
        "parameters": {
            "type": "object",
            "required": ["executive_summary", "systemic_issues", "priority_order"],
            "properties": {
                "executive_summary": {"type": "string"},
                "systemic_issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["pattern", "affected_criteria", "severity", "description", "root_cause"],
                        "properties": {
                            "pattern": {"type": "string"},
                            "affected_criteria": {"type": "array", "items": {"type": "string"}},
                            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                            "description": {"type": "string"},
                            "root_cause": {"type": "string"},
                        },
                    },
                },
                "priority_order": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["criterion_id", "rationale"],
                        "properties": {
                            "criterion_id": {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}
