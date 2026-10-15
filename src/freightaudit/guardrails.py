"""Guardrails that sit between the LLM and the reader.

The model writes; these functions decide whether what it wrote is allowed out.
None of them trust the model. Each is unit-tested with deliberately fabricated output.

1. ``verify_numbers``  every number in the text must exist in the tool results (with
                       rounding / % / K / M variants). Invented figures are rejected.
2. ``verify_entities`` every category id or name the model mentions must exist in the data.
3. ``verify_citations``every recommendation must cite evidence ids that the tools returned.
4. ``wrap_untrusted``  third-party text (marketplace reviews, emails) is fenced as data.
   ``flag_injection``   and scanned for instruction-like phrasing, which is logged, not obeyed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

NUM_RE = re.compile(r"(?<![\w.])([-+]?\$?\d[\d,]*(?:\.\d+)?)(\s?(?:%|x|K|M|B|k|pp)\b|%)?")
# Identifiers and dates are not quantities: strip them before checking numbers
# (SPA-501, PRC-101, E-N042, PO-104233, S00012, 2026-W42, 2026-10-15).
ID_RE = re.compile(r"\b(?:[A-Z]{1,4}-?[A-Z]?\d+[A-Za-z\d-]*|\d{4}-W\d{2}|\d{4}-\d{2}-\d{2})\b")
SMALL_INT_OK = 10          # bare integers <= 10 ("top 3", "5 categories") are not checked
INJECTION_RE = re.compile(
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)|disregard (the )?(system|instructions)|"
    r"you are now|new instructions|system prompt|act as|recommend .* regardless", re.I)


@dataclass
class GuardrailReport:
    ok: bool = True
    unsupported_numbers: list[str] = field(default_factory=list)
    unknown_entities: list[str] = field(default_factory=list)
    bad_citations: list[str] = field(default_factory=list)
    policy_violations: list[str] = field(default_factory=list)

    def fail(self, attr, item):
        getattr(self, attr).append(item)
        self.ok = False

    def feedback(self) -> str:
        parts = []
        if self.unsupported_numbers:
            parts.append(f"These numbers do not appear in any tool result: {self.unsupported_numbers}")
        if self.unknown_entities:
            parts.append(f"These categories do not exist in the data: {self.unknown_entities}")
        if self.bad_citations:
            parts.append(f"These citations are invalid: {self.bad_citations}")
        if self.policy_violations:
            parts.append(f"Policy violations: {self.policy_violations}")
        return " ".join(parts)


def _flatten(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _flatten(v)
    else:
        yield obj


def fact_values(tool_results) -> dict[str, set[float]]:
    """Numeric values the tools returned, in each form a writer could legitimately use them.

    Unit-aware on purpose: "$4.2M" is only supported by a value near 4,200,000, never by a
    stray 4.2 elsewhere in the data. Loose matching is how grounding checks quietly pass.
    """
    forms = {"": set(), "%": set(), "K": set(), "M": set(), "B": set()}
    for x in _flatten(tool_results):
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            continue
        x = abs(float(x))                          # "-$972K contribution" is written as "$972K lost"
        for d in (0, 1, 2):
            forms[""].add(round(x, d))
            forms["%"].add(round(x * 100, d))      # 0.395 -> 39.5%
            forms["%"].add(round(x, d))            # already a percentage
            forms["K"].add(round(x / 1e3, d))
            forms["M"].add(round(x / 1e6, d))
            forms["B"].add(round(x / 1e9, d))
    return forms


def verify_numbers(text: str, tool_results, tol: float = 0.051) -> list[str]:
    facts = fact_values(tool_results)
    bad = []
    for m in NUM_RE.finditer(ID_RE.sub(" ", text)):
        tok, unit = m.group(1), (m.group(2) or "").strip()
        key = {"k": "K", "pp": "%", "x": ""}.get(unit, unit)
        v = abs(float(tok.replace("$", "").replace(",", "")))
        if not unit and "." not in tok and "$" not in tok and abs(v) <= SMALL_INT_OK:
            continue
        if not any(abs(v - f) <= tol for f in facts[key]):
            bad.append(tok + unit)
    return bad


def verify_entities(names: list[str], allowed: set[str]) -> list[str]:
    low = {a.lower() for a in allowed}
    return [n for n in names if n.lower() not in low]


def verify_citations(cited: list[str], issued: set[str]) -> list[str]:
    return [c for c in cited if c not in issued]


def wrap_untrusted(text: str, source: str) -> str:
    safe = text.replace("</untrusted", "<\\/untrusted")
    return f'<untrusted source="{source}">{safe}</untrusted>'


def flag_injection(text: str) -> bool:
    return bool(INJECTION_RE.search(text or ""))


def parse_json_block(text: str) -> dict | None:
    """Model output must be a single JSON object; anything else is rejected, not repaired by guesswork."""
    try:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception:
        return None
