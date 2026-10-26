# Third-party licences

- `marimo-LICENSE.txt`: Apache License 2.0, for code adapted from
  [marimo](https://github.com/marimo-team/marimo) (Copyright 2026 Marimo). The data
  table in `src/nembot/static/datatable.js`, and its styles under `.mo-*` in
  `src/nembot/static/app.css`, are ported from marimo's React data table
  (`frontend/src/components/data-table`, `frontend/src/components/ui/table.tsx`) and
  theme (`frontend/src/css`). The port rewrites it as plain DOM code without
  React, TanStack Table or Tailwind; the file header lists what changed. The code
  highlighting colours (`--tok-*` and `.tok-*` in `app.css`) are marimo's CodeMirror
  editor colours for its light and dark themes, read from marimo's rendered editor;
  SQL highlighting uses CodeMirror's Lezer SQL parser (MIT), loaded from esm.sh.
- `lucide-LICENSE.txt`: ISC licence, for the [Lucide](https://lucide.dev) icon paths
  embedded in `datatable.js`. These are the same icons marimo uses.
- `marimo-skills-LICENSE.txt` and `marimo-pair-LICENSE.txt`: Apache License 2.0, for the guide text in
  `src/nembot/guide_files/marimo/` adapted from marimo's agent skills
  ([marimo-team/skills](https://github.com/marimo-team/skills) `skills/marimo-notebook`, and
  [marimo-team/marimo-pair](https://github.com/marimo-team/marimo-pair) for `sections/LIVE.md`). Each file
  notes its source; the text was shortened and adapted to nembot (its tools, data access and environment).
