"""OPT-IN live Groq smoke test. Skipped unless GROQ_API_KEY is set. One small request, bounded tokens.

Verifies the configured model answers through the real API with either text or a valid tool call.
"""
import json
import os

import pytest

from blastgate.agent import Finish, GroqPlanner, ToolCall

pytestmark = [pytest.mark.anyio, pytest.mark.live_provider,
              pytest.mark.skipif(not os.getenv("GROQ_API_KEY"), reason="GROQ_API_KEY not set; live Groq NOT tested")]


async def test_configured_model_returns_text_or_a_valid_tool_call(tool_specs):
    planner = GroqPlanner(os.getenv("AGENT_MODEL", "openai/gpt-oss-20b"), timeout_s=30)
    await planner.start({"pr_id": "PR-104", "question": "Which tool should run first?", "simulate": None},
                        tool_specs[:1])                       # one tool keeps the request small
    step = await planner.next([])
    if isinstance(step, Finish):
        assert step.summary.strip()
    else:
        assert step and all(isinstance(c, ToolCall) for c in step)
        assert step[0].name in {"inspect_change", "submit_report"} and step[0].parse_error is None
        json.dumps(step[0].arguments)
