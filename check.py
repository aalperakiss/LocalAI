"""Checks each layer separately: Ollama + model + tool calling, MCP servers, Mechanical.

    python check.py
"""
import json
import sys
from pathlib import Path

import ollama

import agent
import mechanical
from mcp_host import McpHub

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text("utf-8"))
failed = False


def report(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed |= not ok
    print(f"[{'OK ' if ok else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))


# 1. Ollama and model
llm = agent.model_available(CFG)
report("Ollama + model", llm["ok"], llm["model"] if llm["ok"] else llm["error"])

# 2. Tool calling with the configured model
if llm["ok"]:
    tool = {"type": "function", "function": {
        "name": "get_mesh_statistics", "description": "Get mesh node and element counts.",
        "parameters": {"type": "object", "properties": {}}}}
    try:
        kwargs = {"think": CFG["ollama"]["think"]} if "think" in CFG["ollama"] else {}
        resp = ollama.Client(host=CFG["ollama"]["url"]).chat(
            model=CFG["ollama"]["model"], tools=[tool],
            messages=[{"role": "user", "content": "How many nodes does the mesh have? Use the tool."}],
            options={"temperature": 0}, **kwargs)
        calls = resp.message.tool_calls or []
        report("Model tool call", bool(calls), calls[0].function.name if calls else "model answered without a tool call")
    except Exception as exc:  # noqa: BLE001
        report("Model tool call", False, str(exc))

# 3. MCP servers
hub = McpHub(CFG["mcp_servers"], ROOT / CFG["app"].get("log_folder", "logs"))
try:
    hub.start()
except RuntimeError as exc:
    report("MCP host", False, str(exc))
for name, st in hub.status.items():
    detail = (f"{st['tools']} tools, {len(st['llm_tools'])} for chat"
              + (f", missing: {', '.join(st['missing_llm_tools'])}" if st["missing_llm_tools"] else "")
              if st["ok"] else st["error"] + f"  (see logs/mcp_{name}.log)")
    report(f"MCP server '{name}'", st["ok"], detail)

# 4. Mechanical
if hub.has_tool("check_mechanical_connection"):
    state = mechanical.connect(hub, CFG["mechanical"]["grpc_port"])
    report("Mechanical gRPC", state["connected"],
           (state.get("info") or "").strip() if state["connected"]
           else (state.get("error") or "not reachable") + f"  (port {CFG['mechanical']['grpc_port']})")
hub.stop()

print("\nAll checks passed." if not failed else "\nSome checks failed. Fix those first.")
sys.exit(1 if failed else 0)
