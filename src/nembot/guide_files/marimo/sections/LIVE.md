<!-- Adapted from marimo-team/marimo-pair (skills/marimo-pair/SKILL.md), Apache-2.0; see LICENSES/README.md. -->

# Working in an open notebook: run_in_marimo

`run_in_marimo(notebook, code)` runs Python in the scratchpad of a notebook the user has open in nembot's
marimo server. The notebook must be open in a browser tab. The user approves each run unless they turned
that off.

## Scratchpad scope

Notebook variables are available by name, but new top-level names in the scratchpad are discarded after each
call. Use it to inspect variables (`print(df.head())`) and test code. To make lasting changes, use code mode.

## Code mode (`cm`): marimo's private, unstable agent API

```python
import marimo._code_mode as cm

async with cm.get_context() as ctx:
    cid = ctx.create_cell("x = df.head()", hide_code=False)  # cell body only, no @app.cell wrapper
    ctx.run_cell(cid)
```

- `ctx.cells` lists cells (`.id`, `.code`, `.name`, `.status`, `.errors`); `ctx.cells["name"]`, `ctx.cells[0]`.
- `ctx.create_cell(code, after=cid, hide_code=False)`, `ctx.edit_cell(cid, code=...)`, `ctx.delete_cell(cid)`,
  `ctx.move_cell(cid, after=other)`, `ctx.run_cell(cid)`, `ctx.set_ui_value(element, value)`.
- `ctx.graph.cells[cid].defs/.refs`, `ctx.graph.descendants(cid)`, `ctx.graph.ancestors(cid)`.
- Mutations queue up and apply when the `async with` block exits cleanly; don't `await` them.
  Use top-level `async with` directly, not `asyncio.run`.
- If `help(cm)` shows a different API (it can change between marimo versions), follow `help(cm)`.

## Rules

- Each public name has one owning cell; edit that cell instead of redefining the name.
- Read a cell's current code (`ctx.cells[...].code`) before replacing it; the user may have edited it.
- `create_cell`/`edit_cell` only change structure; call `run_cell` to execute.
- Cells marked stale haven't run yet: run them (in order) before relying on their variables.
- Deleting a cell removes its variables; check `ctx.graph.descendants` first.
- Don't edit the .py file directly while it's open, and don't install packages.
