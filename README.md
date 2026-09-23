# OpenSourceAI MCP Mechanical

A local chat and reporting front end for the ANSYS Mechanical model that is open on your
computer. An open-weight language model runs on your machine through [Ollama](https://ollama.com),
talks to Mechanical through the
[mechanical-mcp](https://github.com/codersag/mechanical-mcp) server over stdio, and Mechanical
itself is driven through PyMechanical's gRPC interface. No internet connection is used at
runtime and nothing leaves the machine.

Two things it does:

- **Ask questions about the open model** - analyses, bodies, materials, mesh, named selections,
  supports and loads, result values, natural frequencies, solver messages.
- **Build a Word report** - tables read straight from Mechanical, one contour image per tree
  object, and prose sections written by the local model from the same data.

Optionally it can also **change** the model (loads, supports, materials, mesh, results, solve),
with an approval step for every call.

---

## How it works

```
browser  ->  FastAPI app  ->  Ollama (local model)
                  |
                  +-------->  mechanical-mcp (stdio)  ->  gRPC  ->  ANSYS Mechanical
```

| File | Role |
|---|---|
| `app.py` | FastAPI app: chat streaming, approvals, report, status |
| `mcp_host.py` | Starts the MCP servers, keeps the sessions open, one call at a time per server |
| `agent.py` | Chat loop: local model + the allowlisted tools |
| `local_tools.py` | Extra tools the host adds on top of the MCP server's |
| `mechanical.py` | Connection handling, image export, read-only tree scripts |
| `report.py` | Collects data, asks the model for the prose, writes the `.docx` |
| `static/index.html` | Single-file UI |

The model never sees the full tool list of the MCP server. `config.json` decides which tools
are offered for reading (`llm_tools`) and which for writing (`write_tools`), and the write set
is only offered when write mode is on.

### Tools added by this project

The MCP server has no tool for reading result values, so these are provided by the host and run
read-only scripts inside Mechanical:

| Tool | What it does |
|---|---|
| `get_results` | Result objects of an analysis with their evaluated min, max, mode and frequency |
| `get_messages` | The Messages panel of Mechanical (errors and warnings) |
| `session_info` | Which project and session the tools are attached to, and what each analysis contains |
| `refresh_tree` | Refreshes the GUI tree and reports what the analysis really holds |
| `evaluate_results` | `EvaluateAllResults()` on an already solved analysis (write) |
| `add_gravity` | Gravity as a direction vector, picking whatever gravity method the Mechanical version has (write) |
| `save_project` | Saves the project so tool changes survive a restart (write) |

---

## Requirements

- Windows with ANSYS Mechanical (tested with 2026 R1)
- [mechanical-mcp](https://github.com/codersag/mechanical-mcp) installed in its own venv
- [Ollama](https://ollama.com) with a tool-calling model
- Python 3.10+

## Setup

1. Clone the repository and copy the example config:

   ```
   copy config.example.json config.json
   ```

   Edit `config.json` and set `mcp_servers.mechanical.command` and `args` to your
   `mechanical-mcp` checkout. `setup.bat` copies the example for you if `config.json` is missing.

2. Run `setup.bat`. It creates `.venv`, installs the libraries and runs `check.py`.

3. Pull a model:

   ```
   ollama pull gpt-oss:20b
   ```

   or `pull_model.bat gpt-oss:20b`.

4. Open your project in Mechanical, keep the window open, and start the gRPC server from the
   Workbench command window:

   ```python
   port = GetSystem("SYS").GetContainer(ComponentName="Model").StartGrpcServer()
   print(port)
   ```

   The port is assigned dynamically and a new one is issued every time Mechanical restarts.

5. Run `start.bat`, type that port into the sidebar and press **Connect**. The port is written
   back to `config.json` on a successful connection.

`check.bat` tests each layer on its own: Ollama and model, tool calling, MCP servers, Mechanical.

---

## Using it

**Read-only chat** is the default. Ask in any language; answers come from the tools.

**Write mode** is a switch in the sidebar. With it on, the write tools are offered and every
call that changes the model shows an approval card with the tool name and all arguments:
*Run it*, *Run all this session*, *Reject*. Unticking **Ask before each change** turns approvals
off permanently (saved in `config.json`). Save your Workbench project before working in write
mode: there is no undo beyond Mechanical's own.

**Reasoning** is a switch too. Thinking models are slower but choose tools more reliably; turn
it off for simple questions.

**Create Word report** writes `output\model_report_<timestamp>.docx` plus the raw `.json` the
tables were built from. Sections: summary, model and bodies, connections, mesh, named
selections, per-analysis supports and loads, per-analysis results, discussion, notes. A section
with more than `report.full_size_up_to` images switches to a grid of thumbnails, so a model with
a hundred contacts stays readable. Keep the Mechanical window visible while the report runs;
image export uses the graphics window.

---

## Model choice

Anything with tool calling works. On a laptop with 6 GB VRAM and 32 GB RAM:

| Model | Notes |
|---|---|
| `qwen3:8b` | Fits in VRAM, answers simple questions in seconds. Good default for reading. |
| `gpt-oss:20b` | MoE, ~13 GB, partly on CPU. Better at multi-step tool chains. |
| `qwen3:30b-a3b` | MoE, ~18 GB, mostly on CPU. Slow here but picks tools well. |

Keep `num_ctx` at 8192 unless tool outputs are large, and leave `keep_alive` at an hour so a big
model is not reloaded from disk between questions. To run the model on another machine, point
`ollama.url` at it; nothing else changes.

---

## Known issues

These come from the layers below this project:

- **The gRPC port dies with Mechanical.** Restarting Mechanical means starting the server again
  and entering the new port. The app checks the port with a TCP probe before connecting, because
  PyMechanical hangs forever on a dead port and that blocks the whole MCP server.
- **The Mechanical tree does not repaint** after changes made over gRPC. The objects are in the
  data model (`refresh_tree` or `list_boundary_conditions` will show them) but the Outline may
  not show them until you collapse and expand it. Use `save_project` so changes survive a restart.
- **`add_standard_gravity` in mechanical-mcp** calls `Analysis.AddStandardEarthGravity()`, which
  does not exist in every Mechanical version. Use `add_gravity` from this project instead.
- **`solve_analysis` reports success even when the solve did not run** (`Solve complete. Status:
  SolveRequired`). The agent treats `SolveRequired` and `SolveFailed` as failures and asks the
  model to read `get_messages`.
- The local model can still pick the wrong tool or the wrong argument. Every write is shown
  before it runs for exactly that reason.

## License

MIT - see [LICENSE](LICENSE).
