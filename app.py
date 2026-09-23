"""opensourceMCP web app: chat with the open Mechanical model and build reports.

    python app.py            start on the host/port in config.json
"""
from __future__ import annotations

import json
import shutil
import socket
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import agent
import local_tools
import mechanical
import report
from mcp_host import McpHub

ROOT = Path(__file__).resolve().parent
if not (ROOT / "config.json").exists():   # first run of a fresh clone
    shutil.copy(ROOT / "config.example.json", ROOT / "config.json")
    print("config.json created from config.example.json - set your mechanical-mcp paths in it.")
CFG = json.loads((ROOT / "config.json").read_text("utf-8"))
OUT_DIR = ROOT / CFG["app"].get("output_folder", "output")
LOG_DIR = ROOT / CFG["app"].get("log_folder", "logs")

hub = McpHub(CFG["mcp_servers"], LOG_DIR, descriptions=CFG.get("tool_descriptions"))
_mech_lock = threading.Lock()
_mech_last: dict = {"connected": False, "error": "checking"}   # last known Mechanical state

WRITE_CFG = CFG.get("write_mode", {})
STATE = {"write": bool(WRITE_CFG.get("enabled") and WRITE_CFG.get("default_on")),
         "auto_approve": False}   # "Run all" in an approval card sets this for the session
_approvals: dict[str, dict] = {}


def _needs_approval(_name: str) -> bool:
    return not STATE["auto_approve"]


def _approver(call_id: str, _name: str, _args: dict) -> bool:
    """Blocks the chat stream until the user answers in the browser."""
    event = threading.Event()
    _approvals[call_id] = {"event": event, "approved": False}
    answered = event.wait(WRITE_CFG.get("approval_timeout_s", 300))
    return answered and _approvals.pop(call_id, {}).get("approved", False)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    hub.start()
    if CFG["mechanical"].get("auto_connect"):
        threading.Thread(
            target=lambda: mechanical.connect(hub, CFG["mechanical"]["grpc_port"]), daemon=True
        ).start()
    yield
    hub.stop()


app = FastAPI(title=CFG["app"]["title"], lifespan=lifespan)


class ChatRequest(BaseModel):
    messages: list[dict]


class ConnectRequest(BaseModel):
    port: int | None = None


class ModeRequest(BaseModel):
    write: bool | None = None
    think: bool | None = None
    auto_approve: bool | None = None
    require_approval: bool | None = None


class ModelRequest(BaseModel):
    model: str


class ApprovalRequest(BaseModel):
    id: str
    approved: bool
    always: bool = False   # approve every write tool for the rest of the session


def _ndjson(events):
    for event in events:
        yield json.dumps(event, ensure_ascii=False) + "\n"


@app.get("/")
def index():
    # no-store: the UI changes often and a cached copy hides new controls
    return FileResponse(ROOT / "static" / "index.html",
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/api/status")
def api_status():
    # never block the UI: if a connect or a tool call holds the lock, answer with
    # the last known state instead of waiting for Mechanical
    if _mech_lock.acquire(timeout=3):
        try:
            _mech_last.update(mechanical.status(hub))
            _mech_last["busy"] = False
        finally:
            _mech_lock.release()
    else:
        _mech_last["busy"] = True
    mech = dict(_mech_last)
    return {
        "title": CFG["app"]["title"],
        "llm": agent.model_available(CFG),
        "servers": hub.status,
        "local_tools": sorted(local_tools.NAMES),
        "write_mode": {"enabled": bool(WRITE_CFG.get("enabled")), "on": STATE["write"],
                       "require_approval": bool(WRITE_CFG.get("require_approval", True)),
                       "auto_approve": STATE["auto_approve"]},
        "think": bool(CFG["ollama"].get("think")),
        "mechanical": mech,
        "grpc_port": CFG["mechanical"]["grpc_port"],
    }


@app.post("/api/connect")
def api_connect(req: ConnectRequest | None = None):
    port = (req.port if req and req.port else None) or CFG["mechanical"]["grpc_port"]
    with _mech_lock:
        state = mechanical.connect(hub, port)
    live = state.get("port") or port
    if state["connected"] and live != CFG["mechanical"]["grpc_port"]:
        CFG["mechanical"]["grpc_port"] = live  # Mechanical picks a new port on every restart
        (ROOT / "config.json").write_text(json.dumps(CFG, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return state


@app.post("/api/mode")
def api_mode(req: ModeRequest):
    if req.write is not None:
        STATE["write"] = bool(req.write and WRITE_CFG.get("enabled"))
        if not STATE["write"]:
            STATE["auto_approve"] = False
    if req.auto_approve is not None:
        STATE["auto_approve"] = bool(req.auto_approve)
    if req.require_approval is not None:
        WRITE_CFG["require_approval"] = bool(req.require_approval)   # persisted below
        CFG["write_mode"] = WRITE_CFG
        if req.require_approval:
            STATE["auto_approve"] = False
        (ROOT / "config.json").write_text(json.dumps(CFG, indent=2, ensure_ascii=False) + "\n", "utf-8")
    if req.think is not None:
        CFG["ollama"]["think"] = bool(req.think)
        (ROOT / "config.json").write_text(json.dumps(CFG, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return {"write": STATE["write"], "auto_approve": STATE["auto_approve"],
            "require_approval": bool(WRITE_CFG.get("require_approval", True)),
            "think": bool(CFG["ollama"].get("think"))}


@app.post("/api/model")
def api_model(req: ModelRequest):
    available = agent.model_available(CFG).get("available", [])
    if req.model not in available:
        raise HTTPException(400, f"{req.model} is not installed in Ollama")
    CFG["ollama"]["model"] = req.model
    (ROOT / "config.json").write_text(json.dumps(CFG, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return {"model": req.model}


@app.post("/api/approve")
def api_approve(req: ApprovalRequest):
    item = _approvals.get(req.id)
    if not item:
        raise HTTPException(404, "This approval is no longer waiting")
    if req.approved and req.always:
        STATE["auto_approve"] = True
    item["approved"] = req.approved
    item["event"].set()
    return {"ok": True, "auto_approve": STATE["auto_approve"]}


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    events = agent.run_chat(req.messages, hub, CFG, write=STATE["write"], approver=_approver,
                            needs_approval=_needs_approval)
    return StreamingResponse(_ndjson(events), media_type="application/x-ndjson")


@app.post("/api/report")
def api_report():
    return StreamingResponse(_ndjson(report.run_report(hub, CFG, OUT_DIR)),
                             media_type="application/x-ndjson")


@app.get("/api/reports")
def api_reports():
    files = sorted(OUT_DIR.glob("model_report_*.docx"), reverse=True)[:10]
    return [{"name": f.name, "time": f.stat().st_mtime} for f in files]


@app.get("/files/{name}")
def files(name: str):
    path = (OUT_DIR / name).resolve()
    if path.parent != OUT_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=path.name)


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


if __name__ == "__main__":
    host, port = CFG["app"]["host"], CFG["app"]["port"]
    if not _port_free(host, port):
        print(f"\nPort {port} is already in use - the app is probably still running in another\n"
              f"window. Close that window, or free the port:\n"
              f"    netstat -ano | findstr :{port}\n"
              f"    taskkill /PID <pid> /F\n")
        raise SystemExit(1)
    threading.Timer(2.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
