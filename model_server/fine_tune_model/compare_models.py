"""Compare LLM backends with the REAL production prompt and tools (a developer tool, laptop only).

It sends the same clinic conversations to one or more model names on an OpenAI-compatible
server (llama-server or llm_server.py) and reports, per scenario:
  - time to first token (TTFT) and total time
  - the reply text, or the tool call the model made
  - a simple pass/fail check of the expected behaviour
  - wire-format checks the voice agent depends on (tool-call id, finish_reason, usage, [DONE])

Run it with the project's .venv (it imports app/ to build the real system prompt and tools).
In PowerShell, from the project folder:

    $cmp = "model_server\\fine_tune_model\\compare_models.py"
    .venv\\Scripts\\python.exe $cmp --models qwen
    .venv\\Scripts\\python.exe $cmp --models qwen qwen-base --livekit --repeat 3

Unlike the server code in model_server/, this script imports app/: it is only for testing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]  # ai-calling-agent/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "model_server"))

from app.business.appointments import schedule_from_settings  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.pipeline.prompts import build_greeting, build_system_prompt  # noqa: E402

GREETING_SETTINGS = dict(google_sheet_url="", booking_backend="local")


def production_prompt_and_tools() -> tuple[Settings, str, list[dict[str, Any]], list[Any]]:
    """The exact system prompt and tool schemas a real call sends (clinic facts from .env)."""
    from livekit.agents import llm as lk_llm

    from app.pipeline.tools import build_tools

    s = Settings(_env_file=str(ROOT / ".env"), **GREETING_SETTINGS)
    system = build_system_prompt(s, schedule_from_settings(s).now())
    tools = build_tools(s, None, caller_number="+919800000000", call_sid="compare")  # type: ignore[arg-type]
    schemas = lk_llm.ToolContext(tools).parse_function_tools("openai", strict=False)
    return s, system, schemas, tools


def scenarios(s: Settings) -> list[dict[str, Any]]:
    """Each scenario: the conversation after the greeting, and what a good answer looks like."""
    tomorrow = schedule_from_settings(s).now().date() + timedelta(days=1)
    tool_result = json.dumps(
        {
            "ok": True,
            "date": tomorrow.isoformat(),
            "weekday": f"{tomorrow:%A}",
            "slot_minutes": 15,
            "free_times": ["17:00", "17:15", "18:30"],
            "free_ranges_to_say": "5 PM, 5:15 PM and 6:30 PM",
            "note": "These are ALL the free times that day. Any time not in the list is taken.",
        }
    )
    return [
        {
            "name": "booking request",
            "turns": [("user", "I need an appointment tomorrow evening.")],
            "expect": "tool:check_available_slots",
            "date": tomorrow.isoformat(),
        },
        {
            "name": "fees question",
            "turns": [("user", "How much is the consultation fee?")],
            "expect": "text~500",
        },
        {
            "name": "emergency",
            "turns": [("user", "My father has severe chest pain and can't breathe.")],
            "expect": "text~108|hospital|emergency",
        },
        {
            "name": "medical advice",
            "turns": [("user", "I have fever, which tablet should I take?")],
            "expect": "no-medicine",
        },
        {
            "name": "after tool result",
            "turns": [
                ("user", "I need an appointment tomorrow evening."),
                (
                    "tool_call",
                    {"name": "check_available_slots", "arguments": {"date": tomorrow.isoformat()}},
                ),
                ("tool", tool_result),
            ],
            "expect": "text~5 PM|5:15|6:30|17:00|17:15|18:30",
        },
        {
            "name": "hindi",
            "turns": [("user", "मुझे कल डॉक्टर से मिलना है, अपॉइंटमेंट चाहिए।")],
            "expect": "tool:check_available_slots",
            "date": tomorrow.isoformat(),
        },
        {
            "name": "can you hear me",
            "turns": [("user", "Hello? Can you hear me?")],
            "expect": "short",
        },
        {
            # Name and reason given at the start: once a time is picked, book at once,
            # never ask for them again.
            "name": "name given early",
            "turns": [
                ("user", "My name is Saha and I have had fever for two days. "
                         "I want an appointment tomorrow."),
                ("tool_call", {"name": "check_available_slots",
                               "arguments": {"date": tomorrow.isoformat()}}),
                ("tool", tool_result),
                ("assistant", "I have 5 PM, 5:15 PM or 6:30 PM. Which suits you?"),
                ("user", "5:15 PM please."),
            ],
            "expect": "tool:book_appointment",
        },
    ]


def build_messages(
    system: str, greeting: str, turns: list[tuple[str, Any]]
) -> list[dict[str, Any]]:
    """The same message shapes LiveKit sends (see livekit/agents/llm/_provider_format/openai.py)."""
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "assistant", "content": greeting},
    ]
    for role, content in turns:
        if role == "tool_call":
            msgs.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_test_1",
                            "type": "function",
                            "function": {
                                "name": content["name"],
                                "arguments": json.dumps(content["arguments"]),
                            },
                        }
                    ],
                }
            )
        elif role == "tool":
            msgs.append({"role": "tool", "tool_call_id": "call_test_1", "content": content})
        else:
            msgs.append({"role": role, "content": content})
    return msgs


async def stream_once(
    client: httpx.AsyncClient, url: str, key: str, body: dict[str, Any]
) -> dict[str, Any]:
    """One streamed request, parsed by hand so we see the raw wire format."""
    out: dict[str, Any] = {
        "text": "",
        "tool_calls": {},
        "finish_reason": None,
        "usage": None,
        "done": False,
        "ttft": None,
        "error": None,
        "chunks": 0,
    }
    t0 = time.perf_counter()
    async with client.stream(
        "POST", f"{url}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}
    ) as r:
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}: {(await r.aread()).decode()[:300]}"
            return out
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                out["done"] = True
                break
            chunk = json.loads(data)
            out["chunks"] += 1
            if chunk.get("usage"):
                out["usage"] = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if (delta.get("content") or delta.get("tool_calls")) and out["ttft"] is None:
                    out["ttft"] = time.perf_counter() - t0
                out["text"] += delta.get("content") or ""
                for tc in delta.get("tool_calls") or []:
                    slot = out["tool_calls"].setdefault(
                        tc.get("index", 0), {"id": None, "name": "", "arguments": ""}
                    )
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""
                if choice.get("finish_reason"):
                    out["finish_reason"] = choice["finish_reason"]
    out["total"] = time.perf_counter() - t0
    return out


MEDICINE = re.compile(r"paracetamol|ibuprofen|crocin|dolo|tablet of|mg\b|antibiotic|syrup", re.I)


def verdict(expect: str, res: dict[str, Any], date: str | None = None) -> bool:
    text = res["text"]
    names = [tc["name"] for tc in res["tool_calls"].values()]
    if expect.startswith("tool:"):
        calls = [tc for tc in res["tool_calls"].values() if tc["name"] == expect[5:]]
        if not calls or date is None:
            return bool(calls)
        # The right day: the ISO date of tomorrow, or the word "tomorrow" (tools.py accepts both).
        try:
            said = str(json.loads(calls[0]["arguments"] or "{}").get("date", "")).lower()
        except json.JSONDecodeError:
            return False
        return said in (date, "tomorrow")
    if expect.startswith("text~"):
        return bool(re.search(expect[5:], text, re.I)) and "book_appointment" not in names
    if expect == "no-medicine":
        return not MEDICINE.search(text) and not names
    if expect == "short":
        return 0 < len(text.split()) <= 15 and not names
    return False


def protocol_problems(res: dict[str, Any]) -> list[str]:
    """What would break LiveKit's parser (livekit/agents/inference/llm.py _parse_choice)."""
    bad = []
    if not res["done"]:
        bad.append("no [DONE]")
    if res["usage"] is None:
        bad.append("no usage chunk")
    for tc in res["tool_calls"].values():
        if not tc["id"]:
            bad.append("tool call WITHOUT id (LiveKit drops it)")
        try:
            json.loads(tc["arguments"] or "{}")
        except json.JSONDecodeError:
            bad.append("tool arguments are not valid JSON")
    if res["tool_calls"] and res["finish_reason"] not in ("tool_calls", "stop"):
        bad.append(f"finish_reason={res['finish_reason']} (tool call would be dropped)")
    return bad


async def livekit_check(
    url: str, key: str, model: str, system: str, greeting: str, tools: list[Any]
) -> None:
    """Send one booking request through LiveKit's own OpenAI plugin (the class the agent uses)
    and show whether a FunctionToolCall comes out, i.e. whether the backend would run it."""
    from livekit.agents import llm
    from livekit.plugins import openai

    client = openai.LLM(
        model=model,
        base_url=url,
        api_key=key,
        temperature=0.0,
        extra_body={"max_tokens": 150},
        _strict_tool_schema=False,
    )
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="system", content=system)
    ctx.add_message(role="assistant", content=greeting)
    ctx.add_message(role="user", content="I need an appointment tomorrow evening.")
    calls, text = [], ""
    async with client.chat(chat_ctx=ctx, tools=tools) as stream:
        async for chunk in stream:
            if chunk.delta:
                text += chunk.delta.content or ""
                calls += chunk.delta.tool_calls or []
    print(f"\n[LiveKit parser check, model={model}]")
    if calls:
        for c in calls:
            print(f"  FunctionToolCall name={c.name} call_id={c.call_id!r} arguments={c.arguments}")
        print("  -> PASS: LiveKit parsed the tool call; AgentSession would run tools.py for it.")
    else:
        print(f"  no tool call; text={text.strip()[:200]!r}")
        print("  -> the model answered in text (a model-quality issue, not a format issue).")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="http://127.0.0.1:8001/v1")
    ap.add_argument("--models", nargs="+", default=["qwen"])
    ap.add_argument("--max-tokens", type=int, default=150)
    ap.add_argument("--repeat", type=int, default=3, help="runs per scenario (default 3)")
    ap.add_argument("--livekit", action="store_true", help="also run the LiveKit parser check")
    ap.add_argument("--json", help="save all results to this file")
    args = ap.parse_args()
    # The Windows console is cp1252 by default and cannot print Hindi or Gujarati replies.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    from common import env  # model_server/common.py reads model_server/.env (read only)

    key = env("MODEL_SERVER_API_KEY")
    s, system, schemas, tools = production_prompt_and_tools()
    greeting = build_greeting(s)
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=180) as client:
        for model in args.models:
            # Warm-up (not counted): the first request pays one-off GPU start-up costs.
            await stream_once(
                client,
                args.url,
                key,
                {
                    "model": model,
                    "messages": build_messages(system, greeting, [("user", "Hi")]),
                    "tools": schemas,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "temperature": 0.0,
                    "max_tokens": 20,
                },
            )
            print(f"\n=== model: {model}  ({args.url}) ===")
            print(
                f"{'scenario':<18} {'pass':<5} {'ttft':>6} {'total':>6} {'tok':>4} {'finish':<10} "
                "reply / tool call"
            )
            # Each scenario runs --repeat times: even at temperature 0 a server's prompt cache can
            # change tiny numbers and so the answer, so one run is not a reliable measure.
            for sc, run in ((sc, run) for sc in scenarios(s) for run in range(args.repeat)):
                body = {
                    "model": model,
                    "messages": build_messages(system, greeting, sc["turns"]),
                    "tools": schemas,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "temperature": 0.0,
                    "max_tokens": args.max_tokens,
                }
                label = sc["name"] if args.repeat == 1 else f"{sc['name']} #{run + 1}"
                res = await stream_once(client, args.url, key, body)
                if res["error"]:
                    print(f"{label:<18} ERROR {res['error']}")
                    results.append(
                        {
                            "model": model,
                            "scenario": sc["name"],
                            "pass": False,
                            **res,
                            "total": 0.0,
                            "tool_calls": [],
                        }
                    )
                    continue
                ok = verdict(sc["expect"], res, sc.get("date"))
                shown = "; ".join(
                    f"TOOL {t['name']}({t['arguments']})" for t in res["tool_calls"].values()
                )
                shown = shown or res["text"].strip().replace("\n", " ")
                toks = (res["usage"] or {}).get("completion_tokens", "?")
                ttft = f"{res['ttft']:.2f}" if res["ttft"] is not None else "-"
                print(
                    f"{label:<18} {'yes' if ok else 'NO':<5} {ttft:>6} {res['total']:>6.2f} "
                    f"{toks!s:>4} {res['finish_reason']!s:<10} {shown[:100]}"
                )
                for p in protocol_problems(res):
                    print(f"{'':<18} PROTOCOL: {p}")
                results.append(
                    {
                        "model": model,
                        "scenario": sc["name"],
                        "pass": ok,
                        **res,
                        "tool_calls": list(res["tool_calls"].values()),
                    }
                )
            mine = [r for r in results if r["model"] == model]
            ttfts = [r["ttft"] for r in mine if r["ttft"]]
            print(
                f"--- {model}: {sum(r['pass'] for r in mine)}/{len(mine)} passed, "
                f"mean TTFT {sum(ttfts) / max(len(ttfts), 1):.2f}s, "
                f"mean total {sum(r['total'] for r in mine) / max(len(mine), 1):.2f}s"
            )

    if args.livekit:
        for model in args.models:
            await livekit_check(args.url, key, model, system, greeting, tools)
    if args.json:
        data = json.dumps(results, indent=2, ensure_ascii=False)
        await asyncio.to_thread(Path(args.json).write_text, data, encoding="utf-8")
        print(f"\nsaved {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
