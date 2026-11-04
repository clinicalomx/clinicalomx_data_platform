"""An LLM role proposer over the OpenAI chat-completions API, and the prompt it sends.

Works with any endpoint speaking that API, including a local vLLM server. Two structured
output modes: `json_schema` (default, grammar-constrained on vLLM) and `tools` (function
calling).

The schema puts `choice` first, then `confidence` and `alternatives`, so a truncated response
still parses. The rationale is diagnostic text for a human, never evidence for a gate. A
regex fallback recovers the choice from a response that will not parse. `identity` asks the
model to expand the abbreviation first, with refusal allowed.

A reasoning model that runs out of tokens while thinking returns an empty answer with
`finish_reason="length"`; that is reported as truncation, never as a refusal.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass, field
from typing import Any, Literal

from ..domain import rules as rule_engine
from ..domain.marker import MarkerPredicate
from ..seed_spec import SegmentationRole
from .role import demoted
from .schema import (
    Attention,
    Candidate,
    Proposal,
    ProposalRequest,
    Triage,
    assess,
    ranked,
)

ROLE_SYSTEM = """\
You assign a segmentation role to an imaging channel.

Roles:
  nucleus          stains DNA; feeds the nuclear channel
  cyto_structural  outlines the cell; feeds whole-cell morphology
  marker           informative but not a boundary; excluded from segmentation
  exclude          not usable at all

Rules you must follow:
- What rules out cyto_structural is having NO structural localisation, not the presence of an
  organelle. Extra locations do not disqualify a channel: a protein listed as both "Cell
  membrane" and "Melanosome" still outlines the cell, and the best available structural
  markers often list unrelated compartments too.
- So: if a structural term is present (cell membrane, plasma membrane, cell junction,
  adherens, cytoskeleton, basolateral, sarcolemma), the channel is cyto_structural. Only when
  no such term appears does an organelle localisation (mitochondrion, lysosome, Golgi, ER)
  make it a marker.
- A soluble enzyme in "Cytoplasm" is not structural.
- nucleus means a DNA stain present in every cell, such as DAPI or Hoechst. A protein that
  merely localises to the nucleus is not nucleus: a transcription factor restricted to one
  lineage cannot segment nuclei it is absent from. The same holds for cyto_structural, which
  needs a channel every cell carries.
- Segmentation needs ubiquity, and localisation alone cannot show it. If the channel is a
  lineage or cell-state marker, answer marker however clear its compartment is.
- If you cannot justify a role from the evidence, answer marker. It is the safe wrong
  answer: the channel is excluded rather than fed to segmentation.
- Do not guess an identity you do not know. Refuse instead.

Answer with the choice first, then how likely it is correct as a confidence from 0 to 1.
Then rank every other role, most likely first, each with its own confidence; a reviewer uses
the runners-up to see how close the call was. Then a one-sentence reason of at most 25 words.
"""


def role_user(marker: str, evidence: str, locations: tuple[str, ...],
              steering: tuple[str, ...] = ()) -> str:
    parts = [f"Channel: {marker}"]
    if locations:
        parts.append(f"UniProt subcellular locations: {'; '.join(locations)}")
    if evidence:
        parts.append(f"Verbatim UniProt text: {evidence}")
    if steering:
        parts.append("Reviewer guidance from earlier attempts:")
        parts += [f"  - {s}" for s in steering]
    return "\n".join(parts)


#: The roles a model may answer with, as plain strings.
ROLES = tuple(role.value for role in SegmentationRole)

#: What the model may answer with: a role, or a refusal.
CHOICES = (*ROLES, "REFUSE")

#: Property order matters: the answer must survive truncation of the explanation.
ROLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "choice": {"type": "string", "enum": list(CHOICES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                       "description": "How likely the choice is correct, from 0 to 1."},
        "identity": {"type": "string",
                     "description": "Expand the channel abbreviation, or UNKNOWN. Do not guess."},
        "alternatives": {
            "type": "array",
            "description": "Every other role, most likely first, each with its confidence.",
            "items": {
                "type": "object",
                "properties": {"choice": {"type": "string", "enum": list(ROLES)},
                               "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
                "required": ["choice", "confidence"],
                "additionalProperties": False,
            },
        },
        "reasoning": {"type": "string", "description": "One sentence, at most 25 words."},
    },
    "required": ["choice", "confidence", "identity", "alternatives", "reasoning"],
    "additionalProperties": False,
}

TOOL = {
    "type": "function",
    "function": {
        "name": "propose_role",
        "description": "Assign a segmentation role to an imaging channel.",
        "parameters": ROLE_SCHEMA,
    },
}

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "role_proposal", "schema": ROLE_SCHEMA, "strict": True},
}

LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "host.docker.internal")


def _is_local(base_url: str | None) -> bool:
    """True for an endpoint that plausibly ignores the key. `None` means api.openai.com."""
    return bool(base_url) and any(host in base_url for host in LOCAL_HOSTS)


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_CHOICE = re.compile("|".join(re.escape(c) for c in CHOICES), re.IGNORECASE)


@dataclass
class LLMRoleProposer:
    """Propose a role with a model, then apply the promoted rules to what it said.

    Args:
        client: a preconfigured client. Overrides the arguments below.
        model: served model name. Defaults to `MARKER_LLM_MODEL`.
        base_url: OpenAI-compatible endpoint. Defaults to `OPENAI_BASE_URL`; unset means
            api.openai.com.
        api_key: defaults to `OPENAI_API_KEY`, then to `EMPTY` for local servers. The fallback
            warns against a non-local endpoint.
        mode: `json_schema` or `tools`.
        max_tokens: must also cover the thinking block on a reasoning model.
        thinking: `False` skips deliberation, `True` forces it, `None` leaves the template alone.
            Sent as `chat_template_kwargs`, which vLLM accepts and api.openai.com rejects.
    """

    client: Any = None
    model: str = ""
    base_url: str | None = None
    api_key: str | None = None
    mode: Literal["json_schema", "tools"] = "json_schema"
    temperature: float = 0.0
    max_tokens: int = 2048
    thinking: bool | None = None
    name: str = "llm"
    #: Where the key came from: "argument", "environment" or "default".
    api_key_source: str = field(default="", init=False)
    #: Raw payloads from each call, for inspection in a notebook.
    transcript: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.model = self.model or os.environ.get("MARKER_LLM_MODEL", "")
        if not self.model:
            raise ValueError("set `model` or MARKER_LLM_MODEL to the served model name")
        self.base_url = self.base_url or os.environ.get("OPENAI_BASE_URL")
        # Warn only when this proposer will use the key itself.
        key, self.api_key_source = self._resolve_api_key(warn=self.client is None)
        if self.client is None:
            from openai import OpenAI

            self.client = OpenAI(base_url=self.base_url, api_key=key)

    def _resolve_api_key(self, *, warn: bool) -> tuple[str, str]:
        """The key and where it came from, warning when the fallback is probably wrong."""
        if self.api_key:
            return self.api_key, "argument"
        from_env = os.environ.get("OPENAI_API_KEY")
        if from_env:
            return from_env, "environment"
        # vLLM ignores the key but the SDK requires one.
        if warn and not _is_local(self.base_url):
            warnings.warn(
                f"no api_key and no OPENAI_API_KEY; sending a placeholder to "
                f"{self.base_url or 'api.openai.com'}. Set one unless the endpoint ignores it.",
                stacklevel=3,
            )
        return "EMPTY", "default"

    def propose(self, request: ProposalRequest) -> list[Proposal]:
        payload = self._ask(request)
        self.transcript.append(payload)

        choice = str(payload.get("choice", "REFUSE"))
        refused = choice not in ROLES
        value = "" if refused else choice
        reasoning = str(payload.get("reasoning", ""))
        stated = _confidence(payload.get("confidence"))
        confidence = 0.0 if stated is None or refused else stated
        alternatives = ranked(_alternatives(payload.get("alternatives"), exclude=value))

        # The model's numbers are kept as stated; problems go in the triage reason.
        runner = alternatives[0] if alternatives else None
        if refused:
            triage = Triage(Attention.SCRUTINISE, f"the model refused: {reasoning or choice}")
        elif stated is None:
            triage = Triage(Attention.SCRUTINISE, "the model stated no usable confidence")
        elif runner is not None and runner.confidence > confidence:
            triage = Triage(Attention.SCRUTINISE,
                            f"the model ranked {runner.value!r} ({runner.confidence:.2f}) above "
                            f"its own choice ({confidence:.2f})")
        else:
            triage = assess(confidence, alternatives)

        proposal = Proposal(
            predicate=MarkerPredicate.SEGMENTATION_ROLE, value=value, confidence=confidence,
            reasoning=reasoning, alternatives=alternatives, triage=triage,
            identity=str(payload.get("identity", "")), refused=refused, notes={"raw": payload},
        )
        if not proposal.usable:
            return [proposal]

        # Established rules constrain generation as well as validation.
        subject_row = {"canonical_name": request.marker,
                       "compartment": " ".join(request.locations)}
        try:
            rule_engine.check(MarkerPredicate.SEGMENTATION_ROLE, proposal.value, subject_row)
        except ValueError as blocked:
            candidates = (Candidate(value, confidence, reasoning), *alternatives)
            return [demoted(candidates, value, blocked, identity=proposal.identity,
                            reasoning=f"model said {value!r}; demoted: {blocked}",
                            notes=proposal.notes)]
        return [proposal]

    def _ask(self, request: ProposalRequest) -> dict:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": ROLE_SYSTEM},
                {"role": "user", "content": role_user(
                    request.marker, request.evidence, request.locations, request.steering)},
            ],
        }
        if self.mode == "tools":
            kwargs["tools"] = [TOOL]
            kwargs["tool_choice"] = {"type": "function",
                                     "function": {"name": TOOL["function"]["name"]}}
        else:
            kwargs["response_format"] = RESPONSE_FORMAT
        if self.thinking is not None:
            # Not an SDK parameter, so it travels in extra_body.
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": self.thinking}}

        response = self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        return parse_message(choice.message, mode=self.mode,
                             finish_reason=getattr(choice, "finish_reason", None))


def _confidence(raw: Any) -> float | None:
    """A stated confidence in [0, 1], or None."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw) if 0.0 <= raw <= 1.0 else None


def _alternatives(raw: Any, *, exclude: str) -> list[Candidate]:
    """The model's runners-up that name a role and state a usable confidence, once each."""
    out: dict[str, Candidate] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        role, confidence = entry.get("choice"), _confidence(entry.get("confidence"))
        if role in ROLES and role != exclude and role not in out and confidence is not None:
            out[role] = Candidate(role, confidence)
    return list(out.values())


def _thinking_of(message: Any) -> str:
    """The deliberation channel: `reasoning` or `reasoning_content`, by vLLM version."""
    for attribute in ("reasoning", "reasoning_content"):
        found = getattr(message, attribute, None)
        if found:
            return str(found)
    return ""


def parse_message(message: Any, *, mode: str = "json_schema",
                  finish_reason: str | None = None) -> dict:
    """Structured payload from a chat message, degrading to a refusal rather than raising.

    `finish_reason` distinguishes a response cut off by the token budget from a refusal.
    """
    if mode == "tools":
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            return json.loads(calls[0].function.arguments)

    truncated = finish_reason == "length"
    text = _THINK.sub("", getattr(message, "content", None) or "").strip()

    if not text:
        thinking = _thinking_of(message)
        # A complete object in the thinking channel is the answer. Role names in its prose are not.
        recovered = _first_object(thinking)
        if recovered is not None:
            return recovered
        if thinking and truncated:
            why = (f"spent all {len(thinking)} characters of the token budget thinking and "
                   f"never answered; raise max_tokens or set thinking=False")
        elif thinking:
            why = "returned only deliberation, no answer"
        elif truncated:
            why = "empty response, truncated by the token budget"
        else:
            why = "empty response"
        return {"choice": "REFUSE", "identity": "", "reasoning": why, "truncated": truncated}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    recovered = _first_object(text)
    if recovered is not None:
        return recovered

    # Last resort: recover `choice` alone, which the grammar emits first.
    hit = _CHOICE.search(text)
    if hit:
        canonical = next(c for c in CHOICES if c.lower() == hit.group(0).lower())
        return {"choice": canonical, "identity": "",
                "reasoning": "recovered from a truncated response" if truncated
                             else "recovered from an unparseable response",
                "truncated": truncated}
    return {"choice": "REFUSE", "identity": "",
            "reasoning": "response truncated before any answer" if truncated
                         else "could not parse response",
            "truncated": truncated}


def _first_object(text: str) -> dict | None:
    """The first JSON object in `text` that parses, or None."""
    found = _JSON_OBJECT.search(text or "")
    if not found:
        return None
    try:
        parsed = json.loads(found.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
