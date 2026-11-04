"""The LLM proposer against a fake chat-completions client. No key, no server, no network.

Refusals are honoured, out-of-vocabulary values never become claims, promoted rules demote
the model, and an unparseable response degrades to a refusal.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from clinicalomx_markers.propose.llm import (
    CHOICES,
    ROLE_SCHEMA,
    LLMRoleProposer,
    _is_local,
    parse_message,
)
from clinicalomx_markers.propose.schema import ProposalRequest
from clinicalomx_markers.vocab import MarkerPredicate, SegmentationRole


@dataclass
class Function:
    name: str
    arguments: str


@dataclass
class ToolCall:
    function: Function


@dataclass
class Message:
    content: str | None = None
    tool_calls: list | None = None
    #: Deliberation from a reasoning parser; `content` stays null until it closes.
    reasoning: str | None = None


@dataclass
class Choice:
    message: Message
    finish_reason: str | None = "stop"


@dataclass
class Completion:
    choices: list


@dataclass
class FakeClient:
    """Mimics `client.chat.completions.create`, recording what it was sent."""

    replies: list
    calls: list = field(default_factory=list)
    finish_reason: str = "stop"

    def __post_init__(self):
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies[len(self.calls) - 1]
        message = reply if isinstance(reply, Message) else Message(content=json.dumps(reply))
        return Completion(choices=[Choice(message=message,
                                          finish_reason=self.finish_reason)])


def proposer(replies, **over):
    return LLMRoleProposer(client=FakeClient(replies=replies), model="qwen-local", **over)


def request_for(marker="NAKATPASE", locations=("Cell membrane",), **over):
    base = {"marker": marker, "evidence": "; ".join(locations), "locations": locations}
    base.update(over)
    return ProposalRequest(**base)


def test_choice_comes_first_in_the_schema():
    """Truncation costs the explanation, not the answer."""
    assert list(ROLE_SCHEMA["properties"]) == [
        "choice", "confidence", "identity", "alternatives", "reasoning"]
    assert set(ROLE_SCHEMA["properties"]["choice"]["enum"]) == set(CHOICES)


def test_a_model_name_is_required():
    import os

    os.environ.pop("MARKER_LLM_MODEL", None)
    with pytest.raises(ValueError, match="MARKER_LLM_MODEL"):
        LLMRoleProposer(client=FakeClient(replies=[]))


def test_json_schema_mode_sends_response_format():
    p = proposer([{"choice": "marker", "identity": "x", "reasoning": "y"}])
    p.propose(request_for())
    sent = p.client.calls[0]
    assert sent["response_format"]["json_schema"]["schema"] == ROLE_SCHEMA
    assert "tools" not in sent
    assert sent["temperature"] == 0.0


def test_tools_mode_sends_a_function_instead():
    p = proposer([Message(tool_calls=[ToolCall(Function(
        "propose_role", json.dumps({"choice": "nucleus", "identity": "DAPI",
                                    "reasoning": "DNA stain"})))])], mode="tools")
    assert p.propose(request_for("DAPI", ("Nucleus",)))[0].value == "nucleus"
    sent = p.client.calls[0]
    assert sent["tool_choice"]["function"]["name"] == "propose_role"
    assert "response_format" not in sent


def test_a_clean_proposal_passes_through():
    p = proposer([{"choice": "cyto_structural", "identity": "Na/K-ATPase",
                   "reasoning": "membrane protein outlining the cell"}])
    got = p.propose(request_for())[0]
    assert got.value == "cyto_structural"
    assert got.identity == "Na/K-ATPase"
    assert got.usable


def test_a_refusal_is_not_usable():
    got = proposer([{"choice": "REFUSE", "identity": "UNKNOWN",
                     "reasoning": "no evidence"}]).propose(request_for("CD9999"))[0]
    assert got.refused and not got.usable and got.value == ""


def test_an_out_of_vocabulary_choice_becomes_a_refusal():
    """The enum is not trusted: grammar constraints can be absent."""
    got = proposer([{"choice": "definitely_structural", "identity": "x",
                     "reasoning": "y"}]).propose(request_for())[0]
    assert got.refused and got.value == ""


def test_a_rule_demotes_the_model():
    got = proposer([{"choice": "cyto_structural", "identity": "citrate synthase",
                     "reasoning": "membrane-ish"}]).propose(
        request_for("CITRATE_SYNTHASE", ("Mitochondrion matrix",)))[0]
    assert got.value == "marker"
    assert "demoted" in got.reasoning


def test_steering_is_passed_to_the_model():
    p = proposer([{"choice": "marker", "identity": "LDHA", "reasoning": "soluble"}])
    p.propose(request_for("LDHA", ("Cytoplasm",),
                          steering=("consider metabolic state, not lineage",)))
    user = p.client.calls[0]["messages"][1]["content"]
    assert "consider metabolic state" in user


def test_every_proposal_is_in_the_controlled_set_or_refused():
    for choice in (*SegmentationRole, "REFUSE", "nonsense"):
        got = proposer([{"choice": choice, "identity": "x", "reasoning": "y"}]).propose(
            request_for())[0]
        assert got.value in tuple(SegmentationRole) or got.refused


# --- parsing what a local model actually emits -------------------------------------------

def test_a_thinking_block_is_stripped():
    """Qwen-style <think>...</think> before the answer is stripped."""
    message = Message(content='<think>The channel is a membrane pump, so...</think>\n'
                              '{"choice": "cyto_structural", "identity": "ATP1A1", '
                              '"reasoning": "membrane"}')
    assert parse_message(message)["choice"] == "cyto_structural"


def test_json_embedded_in_prose_is_recovered():
    message = Message(content='Here is my answer:\n{"choice": "marker", "identity": "LDHA", '
                              '"reasoning": "soluble"}\nHope that helps.')
    assert parse_message(message)["choice"] == "marker"


def test_an_unparseable_response_still_yields_the_choice():
    got = parse_message(Message(content="I think this one is cyto_structural, honestly"))
    assert got["choice"] == "cyto_structural"
    assert "unparseable" in got["reasoning"]


def test_a_response_with_no_recoverable_choice_refuses():
    got = parse_message(Message(content="I am not sure what you are asking."))
    assert got["choice"] == "REFUSE"


def test_an_empty_response_refuses():
    assert parse_message(Message(content=None))["choice"] == "REFUSE"
    assert parse_message(Message(content="   "))["choice"] == "REFUSE"


# --- reasoning models: a spent token budget is not an abstention -------------------------

def test_thinking_that_ate_the_budget_is_reported_as_truncation_not_refusal():
    """All tokens spent thinking with `content` null reads as truncation, not refusal."""
    got = parse_message(
        Message(content=None, reasoning="Here's a thinking process:\n1. Analyse the user"),
        finish_reason="length",
    )
    assert got["choice"] == "REFUSE"
    assert got["truncated"] is True
    assert "max_tokens" in got["reasoning"] and "thinking=False" in got["reasoning"]


def test_an_answer_stranded_in_the_reasoning_channel_is_still_recovered():
    message = Message(content=None, reasoning='deliberating... {"choice": "nucleus", '
                                              '"identity": "DAPI", "reasoning": "DNA stain"}')
    assert parse_message(message)["choice"] == "nucleus"


def test_a_candidate_weighed_mid_thought_never_becomes_a_proposal():
    """Role names inside deliberation are never recovered as an answer."""
    got = parse_message(
        Message(content=None,
                reasoning="Maybe cyto_structural? Though nucleus is also plausible, so"),
        finish_reason="length",
    )
    assert got["choice"] == "REFUSE", "a passing thought must not be promoted to a claim"


def test_deliberation_without_truncation_is_still_no_answer():
    got = parse_message(Message(content=None, reasoning="I considered the evidence."),
                        finish_reason="stop")
    assert got["choice"] == "REFUSE" and got["truncated"] is False
    assert "only deliberation" in got["reasoning"]


def test_a_truncated_but_answered_response_keeps_the_choice():
    got = parse_message(Message(content='{"choice": "cyto_structural", "identity": "ATP1'),
                        finish_reason="length")
    assert got["choice"] == "cyto_structural"
    assert "truncated" in got["reasoning"]


def test_truncation_reaches_the_caller_as_an_unusable_proposal():
    client = FakeClient(replies=[Message(content=None, reasoning="thinking hard")])
    client.finish_reason = "length"
    got = LLMRoleProposer(client=client, model="qwen-local").propose(request_for())[0]
    assert got.refused and not got.usable
    assert got.notes["raw"]["truncated"] is True


def test_thinking_is_not_sent_unless_asked():
    """`chat_template_kwargs` is vLLM-only; api.openai.com rejects unknown fields."""
    p = proposer([{"choice": "marker", "identity": "x", "reasoning": "y"}])
    p.propose(request_for())
    assert "extra_body" not in p.client.calls[0]


@pytest.mark.parametrize("setting", [True, False])
def test_thinking_is_forwarded_in_extra_body_when_set(setting):
    """It goes through extra_body: the SDK raises on unknown top-level keywords."""
    p = proposer([{"choice": "marker", "identity": "x", "reasoning": "y"}], thinking=setting)
    p.propose(request_for())
    assert p.client.calls[0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": setting}}


def test_the_default_budget_leaves_room_for_a_thinking_model():
    assert LLMRoleProposer(client=FakeClient(replies=[]), model="m").max_tokens >= 2048


def test_recovery_is_case_insensitive_but_canonicalised():
    got = parse_message(Message(content="probably NUCLEUS"))
    assert got["choice"] == "nucleus", "must match the controlled vocabulary exactly"


def test_the_proposer_records_a_transcript():
    p = proposer([{"choice": "marker", "identity": "x", "reasoning": "y"}])
    p.propose(request_for())
    assert p.transcript == [{"choice": "marker", "identity": "x", "reasoning": "y"}]


def test_predicate_is_always_the_role():
    got = proposer([{"choice": "nucleus", "identity": "DAPI", "reasoning": "DNA"}]).propose(
        request_for("DAPI", ("Nucleus",)))[0]
    assert got.predicate == MarkerPredicate.SEGMENTATION_ROLE


# --- the prompt must not contradict the deterministic rules -------------------------------

def test_the_prompt_lists_the_same_structural_terms_as_the_rules():
    """The prompt and the rules name the same structural terms."""
    from clinicalomx_markers.propose.llm import ROLE_SYSTEM
    from clinicalomx_markers.propose.role import STRUCTURAL_HINTS

    lowered = ROLE_SYSTEM.lower()
    missing = [hint for hint in STRUCTURAL_HINTS if hint not in lowered]
    assert not missing, f"structural terms the prompt never mentions: {missing}"


def test_the_prompt_does_not_teach_the_inverted_veto():
    from clinicalomx_markers.propose.llm import ROLE_SYSTEM

    assert "even if a membrane term also appears" not in ROLE_SYSTEM.lower()


# --- endpoint and credential resolution ---------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Clear the developer's own key and endpoint."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("MARKER_LLM_MODEL", "qwen-local")


@pytest.mark.parametrize("url,local", [
    ("http://localhost:8000/v1", True),
    ("http://127.0.0.1:8000/v1", True),
    ("http://0.0.0.0:8000/v1", True),
    ("http://host.docker.internal:8000/v1", True),
    ("https://api.groq.com/openai/v1", False),
    (None, False),                       # unset means api.openai.com, which needs a key
])
def test_local_endpoint_detection(url, local):
    assert _is_local(url) is local


def test_an_explicit_key_wins():
    p = LLMRoleProposer(client=FakeClient(replies=[]), model="m", api_key="sk-explicit")
    assert p.api_key_source == "argument"


def test_the_environment_is_the_next_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    p = LLMRoleProposer(client=FakeClient(replies=[]), model="m")
    assert p.api_key_source == "environment"


def test_a_local_endpoint_needs_no_key_and_stays_quiet(recwarn):
    p = LLMRoleProposer(client=FakeClient(replies=[]), model="m",
                        base_url="http://localhost:8000/v1")
    assert p.api_key_source == "default"
    assert not [w for w in recwarn if "api_key" in str(w.message)]


def test_a_remote_endpoint_without_a_key_warns(monkeypatch):
    """Only fires when the proposer builds the client itself."""
    built = {}
    monkeypatch.setattr("openai.OpenAI", lambda **kw: built.update(kw) or FakeClient(replies=[]))
    with pytest.warns(UserWarning, match="no api_key"):
        LLMRoleProposer(model="m", base_url="https://api.groq.com/openai/v1")
    assert built["api_key"] == "EMPTY"


def test_an_injected_client_does_not_warn(recwarn):
    p = LLMRoleProposer(client=FakeClient(replies=[]), model="m",
                        base_url="https://api.groq.com/openai/v1")
    assert p.api_key_source == "default"
    assert not [w for w in recwarn if "api_key" in str(w.message)]


def test_base_url_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:9001/v1")
    p = LLMRoleProposer(client=FakeClient(replies=[]), model="m")
    assert p.base_url == "http://localhost:9001/v1"


def test_the_model_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("MARKER_LLM_MODEL", "served-name")
    assert LLMRoleProposer(client=FakeClient(replies=[]), model="").model == "served-name"


def test_the_key_itself_is_not_stored_on_the_instance_after_use():
    """`api_key_source` is shown; the key itself is not stored."""
    p = LLMRoleProposer(client=FakeClient(replies=[]), model="m", api_key="sk-secret")
    assert "sk-secret" not in repr(p.api_key_source)
