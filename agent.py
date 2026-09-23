"""Chat agent: local Ollama model + allowlisted MCP tools.

run_chat() is a generator of event dicts the web app streams to the browser:
  {"type": "approval", "id", "name", "args"} a write tool is waiting for the user
  {"type": "approval_result", "id", "approved"}
  {"type": "tool", "name", "args", "write"} a tool call is starting
  {"type": "tool_result", "name", "ok", "preview"}
  {"type": "token", "text"}                 answer text
  {"type": "error", "message"}
  {"type": "done"}
"""
from __future__ import annotations

import json
import uuid
from typing import Callable, Iterator

import ollama

import local_tools
from mcp_host import McpHub, parse_json

SYSTEM_PROMPT = """You are {title}, a local assistant that answers questions about the \
ANSYS Mechanical model that is open on this computer.

Rules:
- Get every fact about the model from the tools. Never guess names, counts, values or units.
{mode_rule}
- A model can hold several analyses. Most tools take analysis_index (0 = first). \
Call get_model_info first when you do not know which analyses exist.
- If a tool says Mechanical is not connected, tell the user to press "Connect" in the sidebar.
- get_results returns the result objects that already exist in the tree with their values. \
If a result the user asks about is not in that list, say it is not in the model; do not say the \
analysis is unsolved unless the status says so.
- Never invent a tool. If nothing in your tool list does what the user asks, say so plainly and \
call NO tool. Never call an unrelated tool instead.
- Quote values with the units the tool returned. Use a table when listing more than three items.
- Never repeat a tool call whose answer you already have in this conversation. If a call fails, \
do not retry it with the same arguments: report the error text to the user in one sentence.
- Be concise and technical. No filler.
- Answer in the language the user writes in."""


READ_ONLY_RULE = """- The tools are read-only. If the user asks you to add, change, delete, mesh \
or solve anything, call NO tool at all: answer in one sentence that write mode is off and the \
change has to be made in Mechanical or with the Write mode switch."""

WRITE_RULE = """- Write mode is on: some tools change the open model (loads, supports, materials, \
mesh, results, solve). Every one of them is shown to the user for approval before it runs, so \
propose the call instead of asking for permission in text. Change only what the user asked for, \
one step at a time. Order matters: set up the model (supports, loads, mesh) first, then SOLVE, \
and add the result objects AFTER the solve, because the add_* result tools evaluate the result \
the moment they create it and can only return a value once the analysis is solved. \
If a result object ends up without a value, call evaluate_results. \
After a solve, check the status in the tool output. Status Done means solved; any other status \
(SolveRequired, SolveFailed) means the solve did NOT run - then call get_messages and report the \
errors and warnings it returns. Never claim an analysis is solved without a Done status. \
Never report a change as done because the write tool said so: after every change, verify it with \
a read tool (list_boundary_conditions for supports and loads, get_results for results, \
get_solve_status for a solve) and tell the user what the model actually contains now. \
Check the model first (named selections, analyses) when an argument depends on it. If the user rejects a call, do not retry it; ask what to change."""


def _client(cfg: dict) -> ollama.Client:
    return ollama.Client(host=cfg["ollama"]["url"], timeout=cfg["ollama"].get("timeout_s", 600))


def _options(cfg: dict) -> dict:
    o = cfg["ollama"]
    return {"num_ctx": o.get("num_ctx", 16384), "temperature": o.get("temperature", 0.2)}


def _chat_kwargs(cfg: dict) -> dict:
    kwargs = {"model": cfg["ollama"]["model"], "options": _options(cfg)}
    if "think" in cfg["ollama"]:
        kwargs["think"] = cfg["ollama"]["think"]
    if cfg["ollama"].get("keep_alive"):
        # keeps the model in memory between questions; reloading 30b costs minutes
        kwargs["keep_alive"] = cfg["ollama"]["keep_alive"]
    return kwargs


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


# The MCP server reports script failures inside its own "ok": true envelope, so the
# text has to be inspected as well before a call counts as successful.
_FAILURE_MARKERS = ("script error", "solverequired", "solvefailed")


def _tool_ok(text: str) -> bool:
    data = parse_json(text)
    if isinstance(data, dict) and data.get("ok") is False:
        return False
    blob = (json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(text)).lower()
    return not any(m in blob for m in _FAILURE_MARKERS)


def run_chat(history: list[dict], hub: McpHub, cfg: dict, write: bool = False,
             approver: Callable[[str, str, dict], bool] | None = None,
             needs_approval: Callable[[str], bool] | None = None) -> Iterator[dict]:
    client = _client(cfg)
    tools = hub.llm_tools(write) + local_tools.SCHEMAS
    need_approval = write and cfg.get("write_mode", {}).get("require_approval", True)
    limit = cfg["ollama"].get("tool_result_max_chars", 6000)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(
            title=cfg["app"]["title"], mode_rule=WRITE_RULE if write else READ_ONLY_RULE)}
    ]
    messages += [m for m in history if m.get("role") in ("user", "assistant") and m.get("content")]

    seen: dict[str, str] = {}   # one attempt per identical call, to stop retry loops
    max_rounds = cfg["ollama"].get("max_tool_rounds", 8)
    for round_no in range(max_rounds + 1):
        allow_tools = round_no < max_rounds
        content, calls = "", []
        try:
            stream = client.chat(
                messages=messages,
                tools=tools if allow_tools else None,
                stream=True,
                **_chat_kwargs(cfg),
            )
            for chunk in stream:
                msg = chunk.message
                if getattr(msg, "thinking", None):
                    yield {"type": "thinking", "text": msg.thinking}
                if msg.content:
                    content += msg.content
                    yield {"type": "token", "text": msg.content}
                if msg.tool_calls:
                    calls.extend(msg.tool_calls)
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"Model call failed: {exc}"}
            yield {"type": "done"}
            return

        if not calls:
            yield {"type": "done"}
            return

        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {"function": {"name": c.function.name, "arguments": dict(c.function.arguments or {})}}
                for c in calls
            ],
        })
        for call in calls:
            name = call.function.name
            args = dict(call.function.arguments or {})
            is_write = hub.is_write_tool(name) or name in local_tools.WRITE_NAMES
            key = name + json.dumps(args, sort_keys=True)
            if key in seen:
                yield {"type": "tool_result", "name": name, "ok": False,
                       "preview": "Repeated call, not run again."}
                messages.append({"role": "tool", "tool_name": name, "content": json.dumps(
                    {"ok": False, "error": "You already made this exact call. Its result is above. "
                                           "Do not repeat it; answer the user or change the arguments."})})
                continue
            ask = need_approval and (needs_approval is None or needs_approval(name))
            if is_write and ask:
                call_id = uuid.uuid4().hex[:8]
                yield {"type": "approval", "id": call_id, "name": name, "args": args}
                approved = bool(approver and approver(call_id, name, args))
                yield {"type": "approval_result", "id": call_id, "approved": approved}
                if not approved:
                    messages.append({"role": "tool", "tool_name": name, "content": json.dumps(
                        {"ok": False, "error": "The user rejected this call. Do not retry it."})})
                    continue
            yield {"type": "tool", "name": name, "args": args, "write": is_write}
            if name in local_tools.NAMES:
                result = local_tools.call(name, args, hub)
            elif hub.is_llm_tool(name, write):
                result = hub.call(name, args)
            else:
                result = json.dumps({"ok": False, "error": f"Tool '{name}' is not available."})
            ok = _tool_ok(result)
            if not ok:
                result = json.dumps({"ok": False, "tool_output": _trim(result, 1500),
                                     "note": "This call did NOT do what it claims. Report the "
                                             "message above to the user instead of saying it worked."})
            yield {"type": "tool_result", "name": name, "ok": ok, "preview": _trim(result, 1500)}
            seen[key] = result
            messages.append({"role": "tool", "content": _trim(result, limit), "tool_name": name})

    yield {"type": "done"}


def model_available(cfg: dict) -> dict:
    """Ollama server + configured model check for the status panel."""
    try:
        listed = _client(cfg).list()
        names = {m.model for m in listed.models}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "model": cfg["ollama"]["model"], "available": [],
                "error": f"Ollama not reachable: {exc}"}
    model = cfg["ollama"]["model"]
    present = model in names or f"{model}:latest" in names
    return {"ok": present, "model": model, "available": sorted(names),
            "error": None if present else f"{model} is not installed"}
