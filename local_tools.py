"""Tools the host adds to the model's tool list, on top of the MCP server's.

Two reasons for them:
  get_results  - the MCP server has no tool that reads existing result objects.
  add_gravity  - the server's add_standard_gravity calls Analysis.AddStandardEarthGravity(),
                 which does not exist in every Mechanical version; this one picks whatever
                 gravity method the open version has.

From the model's point of view they look like any other tool. add_gravity is in
WRITE_NAMES, so it goes through the same approval step as the MCP write tools.
"""
from __future__ import annotations

import json

import mechanical
from mcp_host import McpHub, parse_json

_GRAVITY_SCRIPT = """
import json
_a = ExtAPI.DataModel.Project.Model.Analyses[{index}]
_gx, _gy, _gz = {gx}, {gy}, {gz}
_G = 9.80665
_errors = []
_obj = None
_used = ""
for _m in ("AddStandardEarthGravity", "AddAcceleration"):
    if hasattr(_a, _m):
        try:
            _obj = getattr(_a, _m)()
            _used = _m
            break
        except Exception as _e:
            _errors.append(_m + ": " + str(_e))
if _obj is None:
    _cand = [n for n in dir(_a) if n.startswith("Add") and ("Grav" in n or "Accel" in n)]
    print(json.dumps({{"ok": False, "error": "No gravity method on this Analysis",
                      "tried": _errors, "candidates": _cand}}))
else:
    _sign = 1.0 if _used == "AddStandardEarthGravity" else -1.0
    try:
        _obj.DefineBy = LoadDefineBy.Components
    except Exception as _e:
        _errors.append("DefineBy: " + str(_e))
    for _axis, _c in (("X", _gx), ("Y", _gy), ("Z", _gz)):
        try:
            getattr(_obj, _axis + "Component").Output.SetDiscreteValue(
                0, Quantity(_sign * _c * _G, "m s^-2"))
        except Exception as _e:
            _errors.append(_axis + ": " + str(_e))
    print(json.dumps({{"ok": True, "object": str(_obj.Name), "method": _used,
                      "direction": [_gx, _gy, _gz], "sign_convention": _sign,
                      "warnings": _errors}}))
"""

_EVALUATE_SCRIPT = """
import json
_a = ExtAPI.DataModel.Project.Model.Analyses[{index}]
_s = _a.Solution
_err = ""
try:
    _s.EvaluateAllResults()
except Exception as _e:
    _err = str(_e)
_res = []
for _r in _s.Children:
    if str(_r.GetType().Name) == "SolutionInformation":
        continue
    _d = {{"name": str(_r.Name), "state": str(_r.ObjectState)}}
    for _attr in ("Minimum", "Maximum"):
        try:
            _v = getattr(_r, _attr)
            if _v is not None:
                _d[_attr.lower()] = str(_v)
        except:
            pass
    _res.append(_d)
print(json.dumps({{"ok": _err == "", "error": _err, "status": str(_s.Status), "results": _res}}))
"""

_MESSAGES_SCRIPT = """
import json
_out = []
try:
    for _m in ExtAPI.Application.Messages:
        _d = {}
        for _a in ("Severity", "DisplayString", "Location", "TimeStamp", "StringID"):
            try:
                _v = getattr(_m, _a)
                if _v is not None and str(_v) != "":
                    _d[_a.lower()] = str(_v)
            except:
                pass
        _out.append(_d)
except Exception as _e:
    print(json.dumps({"ok": False, "error": str(_e), "messages": []}))
else:
    print(json.dumps({"ok": True, "count": len(_out), "messages": _out[-30:]}))
"""

_REFRESH_SCRIPT = """
import json
_model = ExtAPI.DataModel.Project.Model
_a = _model.Analyses[{index}]
_kids = []
for _c in _a.Children:
    _kids.append({{"name": str(_c.Name), "type": str(_c.GetType().Name),
                  "state": str(_c.ObjectState)}})
_done = []
for _call in ("ExtAPI.DataModel.Tree.Refresh()", "ExtAPI.DataModel.Tree.Activate([_a])",
              "_a.Activate()", "ExtAPI.Graphics.Redraw()"):
    try:
        eval(_call)
        _done.append(_call)
    except Exception as _e:
        pass
print(json.dumps({{"ok": True, "refreshed_with": _done, "analysis_children": _kids}}))
"""

_SESSION_SCRIPT = """
import json
_p = ExtAPI.DataModel.Project
_d = {}
for _a in ("Name", "FilePath", "ProjectDirectory", "Author", "UnitSystem"):
    try:
        _v = getattr(_p, _a)
        if _v is not None:
            _d[_a.lower()] = str(_v)
    except:
        pass
_an = []
for _i, _x in enumerate(_p.Model.Analyses):
    _an.append({"index": _i, "name": str(_x.Name),
                "objects": [str(_c.Name) for _c in _x.Children],
                "results": [str(_r.Name) for _r in _x.Solution.Children]})
print(json.dumps({"ok": True, "project": _d, "analyses": _an}))
"""

_SAVE_SCRIPT = """
import json
_err = ""
try:
    ExtAPI.DataModel.Project.Save()
except Exception as _e:
    _err = str(_e)
_path = ""
try:
    _path = str(ExtAPI.DataModel.Project.FilePath)
except:
    pass
print(json.dumps({"ok": _err == "", "error": _err, "file": _path}))
"""

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "session_info",
            "description": (
                "Show which Mechanical session these tools are attached to: the project file "
                "path and every analysis with the objects it contains. Use this when the user "
                "says a change is not visible in Mechanical. No arguments."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_project",
            "description": (
                "Save the Mechanical project, so changes made through these tools survive a "
                "restart and show up when the project is reopened. No arguments."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_tree",
            "description": (
                "Refresh the Mechanical tree in the GUI and return what the analysis actually "
                "contains right now (every object with its name, type and state). Call this after "
                "a change when the user says they cannot see it in Mechanical. Args: analysis_index."
            ),
            "parameters": {
                "type": "object",
                "properties": {"analysis_index": {"type": "integer", "default": 0}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_messages",
            "description": (
                "Read the Messages panel of Mechanical (errors, warnings and info). Call this "
                "whenever a solve does not end with status Done, a tool fails, or the user asks "
                "why something did not work. No arguments."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_results",
            "description": (
                "Evaluate the result objects of an already solved analysis and return their "
                "values. Use this after adding a result to an analysis that was solved before, "
                "instead of solving again. Args: analysis_index."
            ),
            "parameters": {
                "type": "object",
                "properties": {"analysis_index": {"type": "integer", "default": 0}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_gravity",
            "description": (
                "Add gravity to an analysis as a unit direction vector, e.g. z=-1 for gravity "
                "along -Z. Magnitude is standard earth gravity (9.80665 m/s2); do not pass it. "
                "Use this instead of add_standard_gravity. "
                "Args: analysis_index, x, y, z."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "analysis_index": {"type": "integer", "default": 0},
                    "x": {"type": "number", "default": 0},
                    "y": {"type": "number", "default": 0},
                    "z": {"type": "number", "default": -1},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_results",
            "description": (
                "List the result objects of an analysis (total deformation, equivalent stress, "
                "reaction probes, ...) with their evaluated minimum, maximum, mode and reported "
                "frequency. Use this for any question about result values. "
                "Args: analysis_index (0 = first analysis, -1 = every analysis)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "analysis_index": {
                        "type": "integer",
                        "description": "Index of the analysis. -1 returns all analyses.",
                        "default": 0,
                    }
                },
            },
        },
    }
]

NAMES = {s["function"]["name"] for s in SCHEMAS}
WRITE_NAMES = {"add_gravity", "evaluate_results", "save_project"}   # change the model, so they need approval


def _num(args: dict, key: str, default: float) -> float:
    try:
        return float(args.get(key, default))
    except (TypeError, ValueError):
        return default


def _add_gravity(args: dict, hub: McpHub) -> str:
    if not hub.has_tool("run_mechanical_script"):
        return json.dumps({"ok": False, "error": "Mechanical MCP server is not running"})
    script = _GRAVITY_SCRIPT.format(
        index=int(_num(args, "analysis_index", 0)),
        gx=_num(args, "x", 0.0), gy=_num(args, "y", 0.0), gz=_num(args, "z", -1.0))
    raw = parse_json(hub.call("run_mechanical_script", {"script": script}, timeout_s=180)) or {}
    data = parse_json(raw.get("output", "")) if isinstance(raw, dict) else None
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({"ok": False, "error": "Gravity could not be added",
                       "output": str(raw.get("output", ""))[:500] if isinstance(raw, dict) else ""})


def _evaluate(args: dict, hub: McpHub) -> str:
    if not hub.has_tool("run_mechanical_script"):
        return json.dumps({"ok": False, "error": "Mechanical MCP server is not running"})
    script = _EVALUATE_SCRIPT.format(index=int(_num(args, "analysis_index", 0)))
    raw = parse_json(hub.call("run_mechanical_script", {"script": script}, timeout_s=600)) or {}
    data = parse_json(raw.get("output", "")) if isinstance(raw, dict) else None
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({"ok": False, "error": "Evaluation failed",
                       "output": str(raw.get("output", ""))[:500] if isinstance(raw, dict) else ""})


def _messages(hub: McpHub) -> str:
    if not hub.has_tool("run_mechanical_script"):
        return json.dumps({"ok": False, "error": "Mechanical MCP server is not running"})
    raw = parse_json(hub.call("run_mechanical_script", {"script": _MESSAGES_SCRIPT}, timeout_s=120)) or {}
    data = parse_json(raw.get("output", "")) if isinstance(raw, dict) else None
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({"ok": False, "error": "Could not read the messages"})


def _refresh(args: dict, hub: McpHub) -> str:
    if not hub.has_tool("run_mechanical_script"):
        return json.dumps({"ok": False, "error": "Mechanical MCP server is not running"})
    script = _REFRESH_SCRIPT.format(index=int(_num(args, "analysis_index", 0)))
    raw = parse_json(hub.call("run_mechanical_script", {"script": script}, timeout_s=180)) or {}
    data = parse_json(raw.get("output", "")) if isinstance(raw, dict) else None
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({"ok": False, "error": "Could not refresh the tree"})


def _plain(hub: McpHub, script: str, fail: str) -> str:
    if not hub.has_tool("run_mechanical_script"):
        return json.dumps({"ok": False, "error": "Mechanical MCP server is not running"})
    raw = parse_json(hub.call("run_mechanical_script", {"script": script}, timeout_s=300)) or {}
    data = parse_json(raw.get("output", "")) if isinstance(raw, dict) else None
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({"ok": False, "error": fail})


def call(name: str, args: dict, hub: McpHub) -> str:
    if name == "session_info":
        return _plain(hub, _SESSION_SCRIPT, "Could not read the session")
    if name == "save_project":
        return _plain(hub, _SAVE_SCRIPT, "Could not save the project")
    if name == "refresh_tree":
        return _refresh(args, hub)
    if name == "get_messages":
        return _messages(hub)
    if name == "add_gravity":
        return _add_gravity(args, hub)
    if name == "evaluate_results":
        return _evaluate(args, hub)
    if name != "get_results":
        return json.dumps({"ok": False, "error": f"Unknown tool: {name}"})
    analyses = mechanical.results_overview(hub)
    if not analyses:
        return json.dumps({"ok": False, "error": "Could not read the results. Is Mechanical connected?"})
    index = args.get("analysis_index", 0)
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = 0
    if index >= 0:
        analyses = [a for a in analyses if a.get("index") == index]
        if not analyses:
            return json.dumps({"ok": False, "error": f"No analysis with index {index}"})
    return json.dumps({"ok": True, "analyses": analyses}, ensure_ascii=False)
