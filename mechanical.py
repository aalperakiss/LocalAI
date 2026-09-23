"""Mechanical-specific helpers used by the host (not exposed to the model).

connect_to_mechanical in the MCP server calls exit() on an existing session
before reconnecting, which can close the Mechanical instance. So the host owns
the connection: it checks first and only connects when there is no session.
"""
from __future__ import annotations

import socket
from pathlib import Path

from mcp_host import McpHub, parse_json


def status(hub: McpHub) -> dict:
    if not hub.has_tool("check_mechanical_connection"):
        return {"connected": False, "error": "Mechanical MCP server is not running"}
    data = parse_json(hub.call("check_mechanical_connection", {}, timeout_s=60)) or {}
    info = str(data.get("info", ""))
    alive = bool(data.get("connected")) and not info.startswith("Error:")
    return {
        "connected": alive,
        "port": data.get("port"),
        "info": info,
        "error": data.get("error") or (info if info.startswith("Error:") else None),
    }


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    """A dead gRPC port makes PyMechanical hang forever, which blocks the whole
    MCP server, so never call connect_to_mechanical without checking first."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def connect(hub: McpHub, port: int) -> dict:
    current = status(hub)
    if current["connected"] or not hub.has_tool("connect_to_mechanical"):
        return current
    if not port_open(port):
        return {"connected": False, "port": port,
                "error": f"Nothing is listening on port {port}. Start the gRPC server in "
                         f"Mechanical and enter the port it returns."}
    data = parse_json(hub.call("connect_to_mechanical", {"port": port}, timeout_s=120)) or {}
    if not data.get("ok"):
        return {"connected": False, "port": port, "error": data.get("error", "connection failed")}
    return status(hub)


# Read-only IronPython run inside Mechanical: every analysis with its solution
# objects and their evaluated values. Nothing is added, changed or evaluated.
_RESULTS_SCRIPT = r'''
import json
_model = ExtAPI.DataModel.Project.Model
_out = []
for _ai, _a in enumerate(_model.Analyses):
    _sol = _a.Solution
    _res = []
    for _r in _sol.Children:
        _d = {"name": str(_r.Name), "type": str(_r.GetType().Name)}
        for _attr in ("Minimum", "Maximum", "ReportedFrequency", "Mode"):
            try:
                _v = getattr(_r, _attr)
                if _v is not None and str(_v) != "":
                    _d[_attr.lower()] = str(_v)
            except:
                pass
        try:
            _d["state"] = str(_r.ObjectState)
        except:
            pass
        _res.append(_d)
    _out.append({"index": _ai, "name": str(_a.Name), "type": str(_a.AnalysisType),
                 "status": str(_sol.Status), "results": _res})
print(json.dumps({"analyses": _out}))
'''


# Read-only: walk the model tree, activate each object and export its view as PNG.
# Categories mirror the report sections: geometry, connections, mesh, named selections,
# the boundary conditions of each analysis, and the result objects under each Solution.
_IMAGE_SCRIPT = r'''
import json, os
_dir = r"{folder}"
_limit = {limit}
if not os.path.isdir(_dir):
    os.makedirs(_dir)
_model = ExtAPI.DataModel.Project.Model
_settings = None
try:
    _settings = Ansys.Mechanical.Graphics.GraphicsImageExportSettings()
    _settings.CurrentGraphicsDisplay = False
    _settings.Resolution = GraphicsResolutionType.EnhancedResolution
    _settings.Background = GraphicsBackgroundType.White
    _settings.Width = 1400
    _settings.Height = 900
except:
    _settings = None

_targets = []   # (category, analysis_index, object)
try:
    _targets.append(("geometry", -1, _model.Geometry))
except:
    pass
for _b in _model.GetChildren(DataModelObjectCategory.Body, True):
    _targets.append(("geometry", -1, _b))
try:
    for _g in _model.Connections.Children:
        _kids = list(getattr(_g, "Children", []))
        if _kids:
            for _c in _kids:
                _targets.append(("connections", -1, _c))
        else:
            _targets.append(("connections", -1, _g))
except:
    pass
try:
    _targets.append(("mesh", -1, _model.Mesh))
except:
    pass
try:
    for _ns in _model.NamedSelections.Children:
        _targets.append(("named_selections", -1, _ns))
except:
    pass
for _ai, _a in enumerate(_model.Analyses):
    for _c in _a.Children:
        _t = str(_c.GetType().Name)
        if _t in ("ANSYSAnalysisSettings", "AnalysisSettings", "Solution"):
            continue
        _targets.append(("boundary_conditions", _ai, _c))
    for _r in _a.Solution.Children:
        if str(_r.GetType().Name) == "SolutionInformation":
            continue
        _targets.append(("results", _ai, _r))

_out = []
for _i, _t in enumerate(_targets):
    if _i >= _limit:
        _out.append({"category": _t[0], "analysis": _t[1], "name": str(_t[2].Name),
                     "error": "image limit reached"})
        continue
    _cat, _ai, _obj = _t
    _path = os.path.join(_dir, "%s_%d_%d.png" % (_cat, _ai + 1, _i))
    try:
        _name = str(_obj.Name)
        _type = str(_obj.GetType().Name)
        _obj.Activate()
        try:
            ExtAPI.Graphics.Camera.SetFit()
        except:
            pass
        if _settings is None:
            ExtAPI.Graphics.ExportImage(_path, GraphicsImageExportFormat.PNG)
        else:
            ExtAPI.Graphics.ExportImage(_path, GraphicsImageExportFormat.PNG, _settings)
        if os.path.isfile(_path):
            _out.append({"category": _cat, "analysis": _ai, "name": _name,
                         "type": _type, "path": _path})
        else:
            _out.append({"category": _cat, "analysis": _ai, "name": _name,
                         "type": _type, "error": "no file written"})
    except Exception as _e:
        _out.append({"category": _cat, "analysis": _ai, "name": str(_obj.Name), "error": str(_e)})
print(json.dumps({"images": _out}))
'''


# Read-only: contacts, joints and springs with the properties worth reporting.
_CONNECTIONS_SCRIPT = r'''
import json
_model = ExtAPI.DataModel.Project.Model
_out = []
try:
    for _g in _model.Connections.Children:
        _kids = list(getattr(_g, "Children", []))
        _items = _kids if _kids else [_g]
        for _c in _items:
            _d = {"group": str(_g.Name), "name": str(_c.Name), "type": str(_c.GetType().Name)}
            for _a in ("ContactType", "Behavior", "FormulationType", "SuppressedByUser",
                       "Suppressed", "FrictionCoefficient", "ConnectionType"):
                try:
                    _v = getattr(_c, _a)
                    if _v is not None and str(_v) != "":
                        _d[_a] = str(_v)
                except:
                    pass
            _out.append(_d)
except Exception as _e:
    print(json.dumps({"connections": [], "error": str(_e)}))
else:
    print(json.dumps({"connections": _out}))
'''


def _script(hub: McpHub, script: str, key: str, timeout_s: float = 600.0) -> list[dict]:
    if not hub.has_tool("run_mechanical_script"):
        return []
    raw = parse_json(hub.call("run_mechanical_script", {"script": script}, timeout_s=timeout_s)) or {}
    data = parse_json(raw.get("output", "")) if isinstance(raw, dict) else None
    return data.get(key, []) if isinstance(data, dict) else []


def export_images(hub: McpHub, folder: Path, limit: int = 80) -> list[dict]:
    """Ask Mechanical to write one PNG per tree object. Returns what it wrote."""
    folder.mkdir(parents=True, exist_ok=True)
    # plain replace, not str.format: the script body contains JSON braces
    script = (_IMAGE_SCRIPT.replace("{folder}", str(folder).replace("\\", "\\\\"))
                           .replace("{limit}", str(int(limit))))
    return _script(hub, script, "images")


def connections(hub: McpHub) -> list[dict]:
    return _script(hub, _CONNECTIONS_SCRIPT, "connections", timeout_s=180)


def results_overview(hub: McpHub) -> list[dict]:
    if not hub.has_tool("run_mechanical_script"):
        return []
    raw = parse_json(hub.call("run_mechanical_script", {"script": _RESULTS_SCRIPT}, timeout_s=180)) or {}
    data = parse_json(raw.get("output", "")) if isinstance(raw, dict) else None
    if isinstance(data, dict):
        return data.get("analyses", [])
    return []
