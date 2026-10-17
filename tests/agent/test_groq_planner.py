"""GroqPlanner unit tests. SCRIPTED MOCK Groq responses over httpx.MockTransport - no key, no network."""
import json

import groq
import httpx
import pytest

from blastgate.agent import Finish, GroqPlanner, Observation, ProviderError, ToolCall

pytestmark = pytest.mark.anyio
TASK = {"pr_id": "PR-104", "question": None, "simulate": None}


def completion(message: dict, finish="stop"):
    return {"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "openai/gpt-oss-20b",
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def tool_call(cid, name, args):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


def scripted(*replies):
    """Planner whose Groq client returns `replies` in order; records every request body."""
    sent, queue = [], list(replies)

    def handler(request: httpx.Request):
        sent.append(json.loads(request.content))
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        status, body, headers = r if isinstance(r, tuple) else (200, r, {})
        return httpx.Response(status, json=body, headers=headers)

    client = groq.AsyncGroq(api_key="test-key-not-real", max_retries=0,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return GroqPlanner("openai/gpt-oss-20b", client=client), sent


async def started(planner, tool_specs):
    await planner.start(TASK, tool_specs)
    return planner


async def test_parses_tool_calls_and_associates_results_by_id(tool_specs):
    p, sent = scripted(completion({"content": None, "tool_calls": [tool_call("call_1", "inspect_change",
                                                                            {"pr_id": "PR-104"})]}, "tool_calls"),
                       completion({"content": None, "tool_calls": [tool_call("call_2", "submit_report", {
                           "summary": "s", "recommended_verdict": "BLOCK"})]}, "tool_calls"))
    await started(p, tool_specs)
    step = await p.next([])
    assert step == [ToolCall(id="call_1", name="inspect_change", arguments={"pr_id": "PR-104"},
                             purpose="selected by model")]
    final = await p.next([Observation(call_id="call_1", name="inspect_change", result={"status": "success"})])
    assert isinstance(final, Finish) and final.recommended_verdict == "BLOCK"
    first, second = sent
    assert first["tool_choice"] == "auto" and first["model"] == "openai/gpt-oss-20b"
    assert {t["function"]["name"] for t in first["tools"]} == {s["name"] for s in tool_specs} | {"submit_report"}
    # Second request: the assistant turn carrying call_1, then the tool message answering call_1.
    assistant, tool_msg = second["messages"][-2:]
    assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["id"] == "call_1"
    assert tool_msg == {"role": "tool", "tool_call_id": "call_1", "name": "inspect_change",
                        "content": json.dumps({"status": "success"})}
    assert "reasoning" not in assistant


async def test_malformed_arguments_become_rejectable_calls(tool_specs):
    p, _ = scripted(completion({"content": None, "tool_calls": [tool_call("c1", "inspect_change", "{not json"),
                                                               tool_call("c2", "inspect_change", "[1,2]")]}))
    await started(p, tool_specs)
    calls = await p.next([])
    assert [c.parse_error for c in calls] == ["arguments were not valid JSON", "arguments must be a JSON object"]


async def test_plain_text_answer_is_a_final_answer(tool_specs):
    p, _ = scripted(completion({"content": "Change is risky because ..."}))
    await started(p, tool_specs)
    final = await p.next([])
    assert isinstance(final, Finish) and final.summary.startswith("Change is risky")


async def test_final_report_is_trimmed_not_rejected(tool_specs):
    args = {"summary": "x" * 5000, "inferences": ["i" * 900] * 20, "recommended_verdict": "MAYBE"}
    p, _ = scripted(completion({"content": None, "tool_calls": [tool_call("c", "submit_report", args)]}))
    await started(p, tool_specs)
    f = await p.next([])
    assert len(f.summary) == 1500 and len(f.inferences) == 10 and len(f.inferences[0]) == 500
    assert f.recommended_verdict is None


async def test_empty_answers_are_bounded(tool_specs):
    p, sent = scripted(completion({"content": ""}), completion({"content": "  "}))
    await started(p, tool_specs)
    with pytest.raises(ProviderError) as e:
        await p.next([])
    assert e.value.code == "provider_bad_response" and len(sent) == 2   # one nudge, then stop


@pytest.mark.parametrize("reply, code", [
    ((401, {"error": {"message": "Invalid API Key"}}, {}), "provider_auth"),
    ((403, {"error": {"message": "forbidden"}}, {}), "provider_auth"),
    ((404, {"error": {"message": "model not found"}}, {}), "provider_model_unavailable"),
    ((400, {"error": {"code": "invalid_request_error", "failed_generation": "SECRET-MODEL-OUTPUT"}}, {}),
     "provider_bad_request"),   # tool_use_failed has its own retry tests below
    ((500, {"error": {"message": "boom"}}, {}), "provider_error"),
    (httpx.ReadTimeout("slow"), "provider_timeout"),
    (httpx.ConnectError("down"), "provider_network"),
])
async def test_provider_errors_are_typed_and_safe(tool_specs, reply, code):
    p, _ = scripted(reply)
    await started(p, tool_specs)
    with pytest.raises(ProviderError) as e:
        await p.next([])
    assert e.value.code == code
    assert "SECRET-MODEL-OUTPUT" not in str(e.value) and "test-key" not in str(e.value)


async def test_rate_limit_surfaces_retry_after(tool_specs):
    p, _ = scripted((429, {"error": {"message": "Rate limit reached"}}, {"retry-after": "7"}))
    await started(p, tool_specs)
    with pytest.raises(ProviderError) as e:
        await p.next([])
    assert e.value.code == "provider_rate_limited" and e.value.retry_after == "7" and "7s" in str(e.value)


async def test_no_choices_is_a_bad_response(tool_specs):
    p, _ = scripted({**completion({"content": "x"}), "choices": []})
    await started(p, tool_specs)
    with pytest.raises(ProviderError, match="no choices"):
        await p.next([])


async def test_simulation_instructions_only_when_requested(tool_specs):
    p, _ = scripted()
    await p.start(TASK, tool_specs)
    assert "do not call simulate_mitigation" in p.messages[0]["content"]
    await p.start({**TASK, "simulate": {"consumer": "checkout-api", "assumption": "tolerant parser"}}, tool_specs)
    assert "consumer='checkout-api'" in p.messages[0]["content"]


# --- Regression: live PR-112 failure (Groq 400 tool_use_failed, submit_report missing `summary`) ---------

TOOL_USE_FAILED = (400, {"error": {"code": "tool_use_failed",
                                   "message": "parameters for tool submit_report did not match schema",
                                   "failed_generation": "SECRET-MODEL-OUTPUT"}}, {})
NEXT_CALL = completion({"content": None, "tool_calls": [tool_call("call_9", "inspect_change", {"pr_id": "PR-104"})]})


async def test_report_schema_stays_strict(tool_specs):
    p, sent = scripted(completion({"content": "done"}))
    await started(p, tool_specs)
    await p.next([])
    report = next(t for t in sent[0]["tools"] if t["function"]["name"] == "submit_report")["function"]["parameters"]
    assert report["required"] == ["summary"]   # the strict schema Groq enforces; not loosened


async def test_tool_use_failed_is_retried_once_at_temperature_zero(tool_specs):
    p, sent = scripted(TOOL_USE_FAILED, NEXT_CALL)
    await started(p, tool_specs)
    step = await p.next([])
    assert step[0].name == "inspect_change" and len(sent) == 2
    assert sent[0]["temperature"] == 0.2 and sent[1]["temperature"] == 0
    assert sent[0]["messages"] == sent[1]["messages"]   # same conversation; nothing invented


async def test_tool_use_failed_twice_is_a_safe_typed_error(tool_specs):
    p, sent = scripted(TOOL_USE_FAILED, TOOL_USE_FAILED)
    await started(p, tool_specs)
    with pytest.raises(ProviderError) as e:
        await p.next([])
    assert e.value.code == "provider_bad_request" and "tool_use_failed" in str(e.value)
    assert "SECRET-MODEL-OUTPUT" not in str(e.value) and len(sent) == 2   # bounded: exactly one retry


@pytest.mark.parametrize("reply", [
    (400, {"error": {"code": "invalid_request_error", "message": "bad"}}, {}),
    (429, {"error": {"message": "Rate limit reached"}}, {"retry-after": "3"}),
    (401, {"error": {"message": "Invalid API Key"}}, {}),
    (500, {"error": {"message": "boom"}}, {}),
])
async def test_unrelated_errors_are_not_retried(tool_specs, reply):
    p, sent = scripted(reply, NEXT_CALL)   # a retry would wrongly succeed via NEXT_CALL
    await started(p, tool_specs)
    with pytest.raises(ProviderError):
        await p.next([])
    assert len(sent) == 1


async def test_namespaced_tool_names_are_not_normalised(tool_specs):
    """Observed live: 'functions.submit_report'. It is passed through unchanged so the allowlist rejects it."""
    p, _ = scripted(completion({"content": None, "tool_calls": [tool_call("c1", "functions.submit_report",
                                                                          {"summary": "s"})]}))
    await started(p, tool_specs)
    step = await p.next([])
    assert isinstance(step, list) and step[0].name == "functions.submit_report"
