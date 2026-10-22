# Third-party licences

- `marimo-LICENSE.txt`: Apache License 2.0, for code adapted from
  [marimo](https://github.com/marimo-team/marimo) (Copyright 2026 Marimo). The data
  table in `src/nembot/static/datatable.js`, and its styles under `.mo-*` in
  `src/nembot/static/app.css`, are ported from marimo's React data table
  (`frontend/src/components/data-table`, `frontend/src/components/ui/table.tsx`) and
  theme (`frontend/src/css`). The port rewrites it as plain DOM code without
  React, TanStack Table or Tailwind; the file header lists what changed.
- `lucide-LICENSE.txt`: ISC licence, for the [Lucide](https://lucide.dev) icon paths
  embedded in `datatable.js`. These are the same icons marimo uses.
