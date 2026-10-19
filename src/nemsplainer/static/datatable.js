/*
 * Data table in the style of marimo's data table, with its interactive features.
 *
 * Ported from marimo (https://github.com/marimo-team/marimo):
 *   frontend/src/components/ui/table.tsx and frontend/src/components/data-table/
 *   (renderers.tsx, columns.tsx, column-header.tsx, header-items.tsx, pagination.tsx,
 *   TableTopBar.tsx, TableBottomBar.tsx, sentinel-cell.tsx, utils.ts,
 *   range-focus/* (cell range selection, selection stats), context-menu.tsx,
 *   filters/* + filter-pills.tsx + filter-pill-editor.tsx + filter-by-values-picker.tsx +
 *   operator-labels.ts, column-visibility-dropdown.tsx, column-wrapping, row-viewer-panel)
 *   and frontend/src/css (globals.css, table.css).
 * Copyright 2026 Marimo. Licensed under the Apache License, Version 2.0; see
 * LICENSES/marimo-LICENSE.txt. Changes: rewritten from React + TanStack Table + Tailwind
 * into a dependency-free DOM component. Markup, styles (the .mo-* rules in app.css), the
 * light-theme colours, formatting, selection behaviour and stats, filter operators,
 * labels and pills, the context menu, column visibility, text wrapping, the row viewer,
 * search, export and pagination follow marimo. Not ported: frozen columns, column summary
 * charts, the column explorer, row selection checkboxes and virtualisation.
 *
 * Icons are Lucide icons (ISC licence, see LICENSES/lucide-LICENSE.txt), as used by marimo.
 *
 * createDataTable(options) -> HTMLElement
 *   columns: [{ name, type?: 'number'|'string'|'date'|'datetime'|'boolean', dtype?: label under the name }]
 *   rows: [[...]] for client-side data (search, filters, sort and paging happen here), or
 *   fetchPage: async ({ offset, limit, sort, search, filters }) => ({ rows, total }) for server-side data, with
 *   fetchValues: async ({ column, filters, search }) => [[value, count], ...] (top values, for "Filter by values")
 *   total: total rows when `rows` holds only the first part of a larger result
 *   pageSize (default 10), pageSizes, search (default true), exportName (enables Export, client mode)
 *   decorateHeader(th, column): e.g. attach a tooltip
 */

const MO_ICONS = {
  'chevrons-up-down': '<path d="m7 15 5 5 5-5"/><path d="m7 9 5-5 5 5"/>',
  'arrow-up-narrow-wide': '<path d="m3 8 4-4 4 4"/><path d="M7 4v16"/><path d="M11 12h4"/><path d="M11 16h7"/><path d="M11 20h10"/>',
  'arrow-down-wide-narrow': '<path d="m3 16 4 4 4-4"/><path d="M7 20V4"/><path d="M11 4h10"/><path d="M11 8h7"/><path d="M11 12h4"/>',
  'ellipsis': '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  'chevron-left': '<path d="m15 18-6-6 6-6"/>',
  'chevron-right': '<path d="m9 18 6-6-6-6"/>',
  'chevrons-left': '<path d="m11 17-5-5 5-5"/><path d="m18 17-5-5 5-5"/>',
  'chevrons-right': '<path d="m6 17 5-5-5-5"/><path d="m13 17 5-5-5-5"/>',
  'chevron-down': '<path d="m6 9 6 6 6-6"/>',
  'search': '<path d="m21 21-4.34-4.34"/><circle cx="11" cy="11" r="8"/>',
  'x': '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  'download': '<path d="M12 15V3"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>',
  'copy': '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  'funnel': '<path d="M10 20a1 1 0 0 0 .553.895l2 1A1 1 0 0 0 14 21v-7a2 2 0 0 1 .517-1.341L21.74 4.67A1 1 0 0 0 21 3H3a1 1 0 0 0-.742 1.67l7.225 7.989A2 2 0 0 1 10 14z"/>',
  'funnel-x': '<path d="M12.531 3H3a1 1 0 0 0-.742 1.67l7.225 7.989A2 2 0 0 1 10 14v6a1 1 0 0 0 .553.895l2 1A1 1 0 0 0 14 21v-7a2 2 0 0 1 .517-1.341l.427-.473"/><path d="m16.5 3.5 5 5"/><path d="m21.5 3.5-5 5"/>',
  'list-filter': '<path d="M2 5h20"/><path d="M6 12h12"/><path d="M9 19h6"/>',
  'plus': '<path d="M5 12h14"/><path d="M12 5v14"/>',
  'check': '<path d="M20 6 9 17l-5-5"/>',
  'trash': '<path d="M10 11v6"/><path d="M14 11v6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  'columns-3': '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/><path d="M15 3v18"/>',
  'panel-right': '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M15 3v18"/>',
  'eye': '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
  'eye-off': '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"/><path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/><path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143"/><path d="m2 2 20 20"/>',
  'text-wrap': '<path d="m16 16-3 3 3 3"/><path d="M3 12h14.5a1 1 0 0 1 0 7H13"/><path d="M3 19h6"/><path d="M3 5h18"/>',
  'text-align-justify': '<path d="M3 5h18"/><path d="M3 12h18"/><path d="M3 19h18"/>',
  'clipboard-copy': '<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M8 4H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/><path d="M16 4h2a2 2 0 0 1 2 2v4"/><path d="M21 14H11"/><path d="m15 10-4 4 4 4"/>',
};

function moIcon(name, size = 16, className = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [k, v] of Object.entries({ width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
    stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })) svg.setAttribute(k, v);
  svg.innerHTML = MO_ICONS[name];  // static icon markup above, never data
  if (className) svg.setAttribute('class', className);
  return svg;
}

// marimo utils.ts prettyNumber
function moPrettyNumber(n) {
  return n.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}

// marimo prettifyRowColumnCount: "1,234 rows, 5 columns"
function moRowColumnText(rows, columns, hidden = 0) {
  const columnText = hidden ? `${moPrettyNumber(columns - hidden)} visible` : `${moPrettyNumber(columns)} ${columns === 1 ? 'column' : 'columns'}`;
  return `${moPrettyNumber(rows)} ${rows === 1 ? 'row' : 'rows'}, ${columnText}`;
}

function moInferType(values) {
  const sample = values.filter(v => v !== null && v !== undefined).slice(0, 50);
  if (!sample.length) return 'string';
  if (sample.every(v => typeof v === 'number')) return 'number';
  if (sample.every(v => typeof v === 'boolean')) return 'boolean';
  if (sample.every(v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v))) return 'date';
  if (sample.every(v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(v))) return 'datetime';
  return 'string';
}

// marimo sentinel-cell.tsx: None / NaN / inf shown as a muted italic pill.
function moSentinel(text) {
  const outer = el('span', 'mo-sentinel');
  outer.append(el('span', 'mo-sentinel-text', text));
  return outer;
}

const moNumberFormat = new Intl.NumberFormat(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 100 });

function moCell(value, type) {
  if (value === null || value === undefined) return moSentinel('None');
  if (typeof value === 'number') {
    if (Number.isNaN(value)) return moSentinel('NaN');
    if (!Number.isFinite(value)) return moSentinel(value > 0 ? 'inf' : '-inf');
    return document.createTextNode(moNumberFormat.format(value));
  }
  if (typeof value === 'boolean') return document.createTextNode(value ? 'True' : 'False');
  if (typeof value === 'object') return document.createTextNode(JSON.stringify(value));
  if (type === 'datetime') return document.createTextNode(String(value).replace('T', ' '));
  return document.createTextNode(String(value));
}

function moCompare(a, b) {
  if (a === b) return 0;
  if (a === null || a === undefined) return 1;   // None sorts last
  if (b === null || b === undefined) return -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

function moCsvValue(v) {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function moText(v) { return v === null || v === undefined ? '' : String(v); }

// ---- Filters (marimo filters/operators.ts, operator-labels.ts, format.ts) ----

const MO_OPERATORS = {
  number: ['between', '==', '!=', '>', '>=', '<', '<=', 'in', 'not_in', 'is_null', 'is_not_null'],
  text: ['contains', 'equals', 'does_not_equal', 'regex', 'starts_with', 'ends_with', 'in', 'not_in', 'is_empty', 'is_null', 'is_not_null'],
  datetime: ['between', '==', '!=', '>', '>=', '<', '<=', 'is_null', 'is_not_null'],
  boolean: ['is_true', 'is_false', 'is_null', 'is_not_null'],
};
const MO_DEFAULT_OPERATOR = { number: 'between', text: 'contains', datetime: '==', boolean: 'is_true' };
const MO_OPERATOR_LABELS = {
  '==': 'Equals', equals: 'Equals', '!=': "Doesn't equal", does_not_equal: "Doesn't equal",
  '>': 'Greater than', '>=': 'Greater than or equal', '<': 'Less than', '<=': 'Less than or equal',
  between: 'Between', contains: 'Contains', starts_with: 'Starts with', ends_with: 'Ends with', regex: 'Matches regex',
  in: 'Is in', not_in: 'Not in', is_empty: 'Is empty', is_true: 'Is true', is_false: 'Is false',
  is_null: 'Is null', is_not_null: 'Is not null',
};
const MO_UNARY = new Set(['is_null', 'is_not_null', 'is_empty', 'is_true', 'is_false']);
const MO_TOP_K = 30;

function moFilterType(column) {
  if (column.type === 'number') return 'number';
  if (column.type === 'date' || column.type === 'datetime') return 'datetime';
  if (column.type === 'boolean') return 'boolean';
  return 'text';
}

function moPillOperator(op) {
  if (['==', '!=', '>', '>=', '<', '<='].includes(op)) return op;
  if (op === 'is_true') return 'is True';
  if (op === 'is_false') return 'is False';
  return MO_OPERATOR_LABELS[op].toLowerCase();
}

function moPillValue(filter, type) {
  if (MO_UNARY.has(filter.operator)) return null;
  if (filter.operator === 'between') return `${filter.min} - ${filter.max}`;
  if (filter.operator === 'in' || filter.operator === 'not_in') {
    const shown = filter.values.slice(0, 3).map(v => v === null ? 'None' : String(v));
    return shown.join(', ') + (filter.values.length > 3 ? ` +${filter.values.length - 3}` : '');
  }
  return type === 'text' ? `"${filter.value}"` : String(filter.value);
}

function moTime(v) {
  const t = Date.parse(String(v).replace(' ', 'T'));
  return Number.isNaN(t) ? null : t;
}

// Client-side evaluation, mirroring the server's SQL (explorer.conditions) and marimo's semantics.
function moMatches(value, filter, type) {
  const op = filter.operator;
  const missing = value === null || value === undefined;
  if (op === 'is_null') return missing;
  if (op === 'is_not_null') return !missing;
  if (op === 'in' || op === 'not_in') {
    const inside = filter.values.some(v => (v === null ? missing : !missing && String(v) === String(value)));
    if (op === 'in') return inside;
    return filter.values.includes(null) ? !inside && !missing : !inside || missing;
  }
  if (type === 'text') {
    const s = moText(value);
    if (op === 'contains') return s.includes(filter.value);
    if (op === 'starts_with') return s.startsWith(filter.value);
    if (op === 'ends_with') return s.endsWith(filter.value);
    if (op === 'regex') { try { return new RegExp(filter.value).test(s); } catch { return false; } }
    if (op === 'is_empty') return !missing && s === '';
    if (missing) return false;
    if (op === 'equals') return s === filter.value;
    if (op === 'does_not_equal') return s !== filter.value;
  }
  if (type === 'boolean') return !missing && value === (op === 'is_true');
  if (missing) return false;
  const num = type === 'datetime' ? moTime : Number;
  const v = num(value);
  if (op === 'between') return v >= num(filter.min) && v <= num(filter.max);
  const target = num(filter.value);
  return { '==': v === target, '!=': v !== target, '>': v > target, '>=': v >= target, '<': v < target, '<=': v <= target }[op] ?? true;
}

// ---- Popovers and menus ----

let moOpenMenu = null;
function moCloseMenu() {
  if (moOpenMenu) { const closing = moOpenMenu; moOpenMenu = null; closing.remove(); closing.onClose?.(); }
}
document.addEventListener('mousedown', e => { if (moOpenMenu && !moOpenMenu.contains(e.target)) moCloseMenu(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && moOpenMenu) moCloseMenu(); });

function moPlace(panel, anchor, at) {
  document.body.append(panel);
  const box = at || anchor.getBoundingClientRect();
  const width = panel.offsetWidth, height = panel.offsetHeight;
  panel.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - width - 8))}px`;
  const below = box.bottom + 4;
  panel.style.top = `${below + height > window.innerHeight - 8 ? Math.max(8, box.top - height - 4) : below}px`;
}

function moMenu(anchor, items, { at, onClose } = {}) {
  moCloseMenu();
  const menu = el('div', 'mo-menu');
  for (const item of items) {
    if (!item) continue;
    if (item === 'separator') { menu.append(el('div', 'mo-menu-sep')); continue; }
    if (item.label && !item.onClick) { menu.append(el('div', 'mo-menu-label', item.label)); continue; }
    const row = el('button', 'mo-menu-item' + (item.active ? ' active' : ''));
    row.type = 'button';
    if (item.icon) row.append(moIcon(item.icon, 14, 'mo-menu-icon'));
    row.append(el('span', '', item.text));
    row.onclick = e => { e.stopPropagation(); moCloseMenu(); item.onClick(); };
    menu.append(row);
  }
  menu.onClose = onClose;
  moOpenMenu = menu;
  moPlace(menu, anchor, at);
  return menu;
}

function moPopover(anchor, content, className = '') {
  moCloseMenu();
  const panel = el('div', `mo-menu mo-popover ${className}`);
  panel.append(content);
  moOpenMenu = panel;
  moPlace(panel, anchor);
  return panel;
}

function moToast(text) {
  const toast = el('div', 'mo-toast', text);
  document.body.append(toast);
  setTimeout(() => toast.remove(), 2000);
}

function moFormatStat(n) {  // marimo cell-selection-stats: 3 decimals, locale grouping
  return Number(n.toFixed(3)).toLocaleString(undefined, { maximumFractionDigits: 3 });
}

function moIsInteractive(target) {
  return target.closest('input, button, select, textarea, a, label, [role="checkbox"], [role="button"], [contenteditable="true"]');
}

function createDataTable(options) {
  const server = typeof options.fetchPage === 'function';
  const allRows = options.rows || [];
  const columns = options.columns.map((c, i) => ({
    ...c, index: i,
    type: c.type || moInferType(allRows.map(r => r[i])),
  }));
  columns.forEach(c => { c.filterType = moFilterType(c); });
  const byName = Object.fromEntries(columns.map(c => [c.name, c]));
  const state = {
    page: 0, pageSize: options.pageSize || 10, sort: null, search: '', filters: [],
    hidden: new Set(), wrapped: new Set(), rows: [], total: 0, offset: 0,
    anchor: null, focus: null, copied: false, dragging: false,
    viewer: { open: false, index: 0 },
  };
  const pageSizes = options.pageSizes || [5, 10, 25, 50, 100];
  const visible = () => columns.filter(c => !state.hidden.has(c.name));

  const wrapper = el('div', 'mo-table-wrapper');
  const pills = el('div', 'mo-pills');
  const body = el('div', 'mo-body');
  const frame = el('div', 'mo-table');
  frame.setAttribute('part', 'table-wrapper');
  const viewerPanel = el('aside', 'mo-viewer');
  body.append(frame, viewerPanel);
  wrapper.append(pills, body);

  // ---- Top bar: search, Columns, Explore, Export (marimo TableTopBar) ----
  const top = el('div', 'mo-topbar');
  const showSearch = options.search !== false && (server || allRows.length > 5);
  if (showSearch) {
    const search = el('div', 'mo-search');
    const input = el('input');
    input.placeholder = 'Search...';
    const clear = el('button', 'mo-icon-button mo-search-clear');
    clear.type = 'button';
    clear.append(moIcon('x', 12));
    clear.hidden = true;
    let timer;
    input.oninput = () => {
      clear.hidden = !input.value;
      clearTimeout(timer);
      timer = setTimeout(() => { state.search = input.value.trim(); state.page = 0; refresh(); }, 500);
    };
    input.onkeydown = e => { if (e.key === 'Escape') { input.value = ''; input.oninput(); input.blur(); } };
    clear.onclick = () => { input.value = ''; input.oninput(); };
    search.append(moIcon('search', 16, 'mo-muted'), input, clear);
    top.append(search);
  }
  const right = el('div', 'mo-topbar-right');
  const textButton = (iconName, text, onClick) => {
    const b = el('button', 'mo-text-button');
    b.type = 'button';
    b.append(moIcon(iconName, 14), el('span', '', text));
    b.onclick = e => { e.stopPropagation(); onClick(b); };
    right.append(b);
    return b;
  };
  textButton('columns-3', 'Columns', columnsPopover);
  const exploreButton = textButton('panel-right', 'Explore', () => toggleViewer());
  if (options.exportName && !server) {
    textButton('download', 'Export', button => moMenu(button, [
      { icon: 'download', text: 'Download CSV', onClick: () => download('csv') },
      { icon: 'download', text: 'Download JSON', onClick: () => download('json') },
      'separator',
      { icon: 'copy', text: 'Copy CSV', onClick: () => copyText(asText('csv'), 'Copied CSV to clipboard') },
      { icon: 'copy', text: 'Copy Markdown', onClick: () => copyText(asText('markdown'), 'Copied Markdown to clipboard') },
    ]));
  }
  top.append(right);
  frame.append(top);

  const scroll = el('div', 'mo-scroll');
  const table = el('table', 'mo-grid');
  const thead = el('thead');
  const tbody = el('tbody');
  table.append(thead, tbody);
  scroll.append(table);
  frame.append(scroll);
  const bottom = el('div', 'mo-bottombar');
  frame.append(bottom);
  const stats = el('div', 'mo-stats');

  // ---- Column visibility (marimo column-visibility-dropdown.tsx) ----
  function columnsPopover(anchor) {
    const content = el('div', 'mo-columns');
    const search = el('input', 'mo-popover-search');
    search.placeholder = 'Search columns...';
    const actions = el('div', 'mo-columns-actions');
    const list = el('div', 'mo-columns-list');
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const matching = columns.filter(c => !q || c.name.toLowerCase().includes(q));
      actions.replaceChildren();
      const action = (text, fn) => {
        const b = el('button', 'mo-link-button', text); b.type = 'button';
        b.onclick = () => { fn(); if (state.hidden.size >= columns.length) state.hidden.delete(columns[0].name); clearSelection(); refresh(false); draw(); };
        actions.append(b);
      };
      if (!q) {
        action('Show all', () => state.hidden.clear());
        action('Hide all', () => columns.slice(1).forEach(c => state.hidden.add(c.name)));
      } else {
        action(`Show only ${matching.length} matching`, () => columns.forEach(c => (matching.includes(c) ? state.hidden.delete(c.name) : state.hidden.add(c.name))));
        const anyHidden = matching.some(c => state.hidden.has(c.name));
        action(`${anyHidden ? 'Show' : 'Hide'} ${matching.length} matching`, () => matching.forEach(c => (anyHidden ? state.hidden.delete(c.name) : state.hidden.add(c.name))));
      }
      list.replaceChildren();
      for (const column of matching) {
        const row = el('button', 'mo-menu-item mo-column-toggle');
        row.type = 'button';
        const shown = !state.hidden.has(column.name);
        row.append(moIcon(shown ? 'eye' : 'eye-off', 14, 'mo-menu-icon'), el('span', shown ? '' : 'mo-muted', column.name));
        row.onclick = e => {
          e.stopPropagation();
          if (shown && visible().length === 1) return;  // keep at least one column
          shown ? state.hidden.add(column.name) : state.hidden.delete(column.name);
          clearSelection(); refresh(false); draw();
        };
        list.append(row);
      }
    };
    search.oninput = draw;
    content.append(search, actions, list);
    draw();
    moPopover(anchor, content, 'mo-columns-popover');
    search.focus();
  }

  // ---- Filters: pills row, editor and the values picker ----
  function setFilter(filter, replacing) {
    const others = state.filters.filter(f => f !== replacing && f.column !== filter.column);
    state.filters = [filter, ...others];  // new pills are prepended, as in marimo
    state.page = 0;
    clearSelection();
    refresh();
  }
  function removeFilter(filter) {
    state.filters = state.filters.filter(f => f !== filter);
    state.page = 0;
    clearSelection();
    refresh();
  }

  function drawPills() {
    pills.replaceChildren();
    pills.hidden = !state.filters.length;
    if (!state.filters.length) return;
    const add = el('button', 'mo-add-filter');
    add.type = 'button';
    add.title = 'Add filter';
    add.append(moIcon('plus', 14));
    add.onclick = e => { e.stopPropagation(); filterEditor(add, { column: visible()[0].name }); };
    pills.append(add);
    for (const filter of state.filters) {
      const column = byName[filter.column];
      const pill = el('span', 'mo-pill');
      pill.tabIndex = 0;
      const segment = (text, cls) => pill.append(el('span', cls, text));
      segment(filter.column, 'mo-pill-column');
      pill.append(el('span', 'mo-pill-sep'));
      segment(moPillOperator(filter.operator), 'mo-pill-op');
      const value = moPillValue(filter, column.filterType);
      if (value !== null) { pill.append(el('span', 'mo-pill-sep')); segment(value, 'mo-pill-value'); }
      pill.title = `${filter.column} ${moPillOperator(filter.operator)}${value === null ? '' : ' ' + value}`;
      const remove = el('button', 'mo-pill-remove');
      remove.type = 'button';
      remove.title = 'Remove filter';
      remove.append(moIcon('x', 12));
      remove.onclick = e => { e.stopPropagation(); removeFilter(filter); };
      pill.append(remove);
      pill.onclick = e => { e.stopPropagation(); filterEditor(pill, filter, filter); };
      pills.append(pill);
    }
    if (state.filters.length > 1) {
      const clearAll = el('button', 'mo-see-all', 'Clear all');
      clearAll.type = 'button';
      clearAll.onclick = () => { state.filters = []; state.page = 0; clearSelection(); refresh(); };
      pills.append(clearAll);
    }
  }

  async function topValues(column) {
    const others = state.filters.filter(f => f.column !== column.name);
    if (server) {
      if (!options.fetchValues) return [];
      return options.fetchValues({ column: column.name, filters: others, search: state.search });
    }
    const counts = new Map();
    for (const row of clientView(others)) {
      const key = row[column.index] ?? null;
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || moCompare(a[0], b[0])).slice(0, MO_TOP_K);
  }

  // marimo filter-by-values-picker.tsx
  function valuesPicker(column, chosen, onChange) {
    const box = el('div', 'mo-values');
    const search = el('input', 'mo-popover-search');
    const list = el('div', 'mo-values-list');
    const footer = el('div', 'mo-values-footer');
    let values = [];
    const key = v => JSON.stringify(v);
    const draw = () => {
      onChange();
      const q = search.value.trim().toLowerCase();
      const known = new Set(values.map(([v]) => key(v)));
      const rows = [...values, ...chosen.filter(v => !known.has(key(v))).map(v => [v, null])]
        .filter(([v]) => !q || moText(v === null ? 'None' : v).toLowerCase().includes(q));
      list.replaceChildren();
      if (!rows.length) {
        list.append(el('div', 'mo-values-empty', values.length ? 'No results found.' : 'No values available'));
      } else {
        const all = el('label', 'mo-values-row mo-values-head');
        const allBox = el('input'); allBox.type = 'checkbox';
        const picked = rows.filter(([v]) => chosen.some(c => key(c) === key(v))).length;
        allBox.checked = picked === rows.length; allBox.indeterminate = picked > 0 && picked < rows.length;
        allBox.onchange = () => {
          for (const [v] of rows) {
            const i = chosen.findIndex(c => key(c) === key(v));
            if (allBox.checked && i < 0) chosen.push(v);
            if (!allBox.checked && i >= 0) chosen.splice(i, 1);
          }
          draw();
        };
        all.append(allBox, el('b', 'mo-values-value', column.name), el('b', 'mo-values-count', 'Count'));
        list.append(all);
        for (const [v, n] of rows) {
          const row = el('label', 'mo-values-row');
          const check = el('input'); check.type = 'checkbox';
          check.checked = chosen.some(c => key(c) === key(v));
          check.onchange = () => {
            const i = chosen.findIndex(c => key(c) === key(v));
            if (check.checked && i < 0) chosen.push(v);
            if (!check.checked && i >= 0) chosen.splice(i, 1);
            draw();
          };
          const label = el('span', 'mo-values-value');
          label.append(v === null ? moSentinel('None') : document.createTextNode(String(v)));
          row.append(check, label, el('span', 'mo-values-count', n === null ? '—' : moPrettyNumber(n)));
          list.append(row);
        }
      }
      footer.textContent = values.length >= MO_TOP_K ? `Only showing the top ${MO_TOP_K} values` : '';
    };
    search.placeholder = column.filterType === 'text' ? 'Search or add a value…' : `Search among the top ${MO_TOP_K} values`;
    search.onkeydown = e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const text = search.value.trim();
        if (column.filterType === 'text' && text && !chosen.includes(text)) { chosen.push(text); search.value = ''; draw(); }
      }
    };
    search.oninput = draw;
    box.append(search, list, footer);
    list.append(el('div', 'mo-values-empty', 'Loading…'));
    topValues(column).then(v => { values = v; draw(); }).catch(err => { list.replaceChildren(el('div', 'mo-values-empty error', err.message)); });
    return box;
  }

  // marimo filter-pill-editor.tsx: Column, Operator, Value, then apply / close / remove.
  function filterEditor(anchor, initial, existing = null) {
    const form = el('form', 'mo-filter-editor');
    const field = (label, control) => { const f = el('label', 'mo-field'); f.append(el('span', '', label), control); return f; };
    const columnSelect = el('select');
    for (const c of columns) columnSelect.append(new Option(c.name, c.name));
    columnSelect.value = initial.column;
    const operatorSelect = el('select');
    const valueArea = el('div', 'mo-field-value');
    const draft = { ...initial, values: [...(initial.values || [])] };
    const apply = el('button', 'mo-icon-button mo-apply');
    apply.type = 'submit'; apply.title = 'Apply'; apply.append(moIcon('check', 14));
    const close = el('button', 'mo-icon-button');
    close.type = 'button'; close.title = 'Close without saving'; close.append(moIcon('x', 14));
    close.onclick = () => moCloseMenu();
    const actions = el('div', 'mo-editor-actions');
    actions.append(apply, close);
    if (existing) {
      const remove = el('button', 'mo-icon-button mo-remove');
      remove.type = 'button'; remove.title = 'Remove filter'; remove.append(moIcon('trash', 14));
      remove.onclick = () => { moCloseMenu(); removeFilter(existing); };
      actions.append(remove);
    }

    const validity = () => {
      const op = draft.operator;
      if (MO_UNARY.has(op)) return '';
      if (op === 'between') return draft.min !== undefined && draft.min !== '' && draft.max !== undefined && draft.max !== '' ? '' : 'Min and max are required';
      if (op === 'in' || op === 'not_in') return draft.values.length ? '' : 'Pick at least one value';
      return draft.value !== undefined && draft.value !== '' ? '' : 'Value is required';
    };
    const update = () => { const problem = validity(); apply.disabled = Boolean(problem); apply.title = problem || 'Apply'; };

    function drawValue() {
      const column = byName[draft.column];
      const type = column.filterType;
      valueArea.replaceChildren();
      const op = draft.operator;
      form.classList.remove('mo-has-picker');
      if (MO_UNARY.has(op)) { update(); return; }
      const input = (key, placeholder, cls) => {
        const box = el('input', cls || '');
        if (type === 'number') { box.type = 'number'; box.step = 'any'; }
        else if (type === 'datetime') { box.type = column.type === 'date' ? 'date' : 'datetime-local'; box.step = 1; }
        box.placeholder = placeholder;
        box.value = draft[key] ?? '';
        box.oninput = () => { draft[key] = type === 'number' && box.value !== '' ? Number(box.value) : box.value; update(); };
        return box;
      };
      if (op === 'between') {
        const row = el('div', 'mo-between');
        row.append(input('min', 'min'), el('span', 'mo-muted', '–'), input('max', 'max'));
        valueArea.append(field('Value', row));
      } else if (op === 'in' || op === 'not_in') {
        valueArea.append(field('Value', valuesPicker(column, draft.values, update)));
        form.classList.add('mo-has-picker');
      } else if (op === 'regex') {
        const row = el('div', 'mo-regex');
        row.append(el('span', 'mo-regex-slash', '/'), input('value', 'pattern'), el('span', 'mo-regex-slash', '/'));
        valueArea.append(field('Value', row));
      } else {
        valueArea.append(field('Value', input('value', type === 'text' ? 'Text…' : 'value')));
      }
      update();
      valueArea.querySelector('input')?.focus();
    }
    function drawOperators() {
      const type = byName[draft.column].filterType;
      operatorSelect.replaceChildren(...MO_OPERATORS[type].map(op => new Option(MO_OPERATOR_LABELS[op], op)));
      if (!MO_OPERATORS[type].includes(draft.operator)) draft.operator = MO_DEFAULT_OPERATOR[type];
      operatorSelect.value = draft.operator;
      drawValue();
    }
    columnSelect.onchange = () => { draft.column = columnSelect.value; draft.operator = null; draft.values = []; delete draft.value; delete draft.min; delete draft.max; drawOperators(); };
    operatorSelect.onchange = () => { draft.operator = operatorSelect.value; drawValue(); };
    form.onsubmit = e => {
      e.preventDefault();
      if (validity()) return;
      const filter = { column: draft.column, operator: draft.operator };
      if (draft.operator === 'between') Object.assign(filter, { min: draft.min, max: draft.max });
      else if (draft.operator === 'in' || draft.operator === 'not_in') filter.values = [...draft.values];
      else if (!MO_UNARY.has(draft.operator)) filter.value = draft.value;
      moCloseMenu();
      setFilter(filter, existing);
    };
    form.append(field('Column', columnSelect), field('Operator', operatorSelect), valueArea, actions);
    drawOperators();
    moPopover(anchor, form, 'mo-filter-popover');
  }

  // ---- Header (marimo columns.tsx DataTableColumnHeader + header-items.tsx) ----
  function setSort(column, desc) {
    state.sort = desc === null ? null : { column: column.name, index: column.index, desc };
    state.page = 0;
    clearSelection();
    refresh();
  }

  function drawHeader() {
    const tr = el('tr');
    const cols = visible();
    for (const column of cols) {
      const th = el('th', 'mo-th');
      const inner = el('div', 'mo-th-inner');
      const group = el('div', 'mo-th-group');
      const row = el('div', 'mo-th-row');
      row.append(el('span', 'mo-th-name', column.label || column.name));
      const filtered = state.filters.find(f => f.column === column.name);
      if (filtered) {
        const indicator = el('span', 'mo-filter-indicator');
        indicator.title = 'Filtered';
        indicator.append(moIcon('funnel', 12));
        row.append(indicator);
      }
      const sorted = state.sort && state.sort.column === column.name;
      const sortButton = el('button', 'mo-icon-button mo-sort' + (sorted ? ' sorted' : ''));
      sortButton.type = 'button';
      sortButton.title = sorted ? (state.sort.desc ? 'Sorted descending' : 'Sorted ascending') : 'Sort';
      sortButton.append(moIcon(sorted ? (state.sort.desc ? 'arrow-down-wide-narrow' : 'arrow-up-narrow-wide') : 'chevrons-up-down', 12));
      sortButton.onclick = e => {  // asc -> desc -> cleared, as in marimo
        e.stopPropagation();
        if (!sorted) setSort(column, false);
        else if (!state.sort.desc) setSort(column, true);
        else setSort(column, null);
      };
      const menuButton = el('button', 'mo-icon-button mo-column-menu');
      menuButton.type = 'button';
      menuButton.title = 'Column options';
      menuButton.append(moIcon('ellipsis', 14));
      menuButton.onclick = e => {
        e.stopPropagation();
        const wrapped = state.wrapped.has(column.name);
        const canPickValues = column.filterType === 'number' || column.filterType === 'text';
        moMenu(menuButton, [
          { label: column.dtype || column.type },
          'separator',
          { icon: 'arrow-up-narrow-wide', text: 'Asc', active: sorted && !state.sort.desc, onClick: () => setSort(column, sorted && !state.sort.desc ? null : false) },
          { icon: 'arrow-down-wide-narrow', text: 'Desc', active: sorted && state.sort.desc, onClick: () => setSort(column, sorted && state.sort.desc ? null : true) },
          { icon: 'chevrons-up-down', text: 'Clear sort', onClick: () => setSort(column, null) },
          { icon: 'copy', text: 'Copy column name', onClick: () => copyText(column.name, 'Copied column name') },
          { icon: wrapped ? 'text-align-justify' : 'text-wrap', text: wrapped ? 'No wrap text' : 'Wrap text',
            onClick: () => { wrapped ? state.wrapped.delete(column.name) : state.wrapped.add(column.name); refresh(false); } },
          cols.length > 1 && { icon: 'eye-off', text: 'Hide column', onClick: () => { state.hidden.add(column.name); clearSelection(); refresh(false); } },
          'separator',
          { icon: 'funnel', text: 'Filter', onClick: () => filterEditor(th, filtered || { column: column.name, operator: MO_DEFAULT_OPERATOR[column.filterType] }, filtered || null) },
          canPickValues && { icon: 'list-filter', text: 'Filter by values', onClick: () => filterEditor(th, filtered && filtered.values ? filtered : { column: column.name, operator: 'in', values: [] }, filtered || null) },
          filtered && { icon: 'funnel-x', text: 'Clear filter', onClick: () => removeFilter(filtered) },
        ]);
      };
      row.append(sortButton, menuButton);
      group.append(row);
      inner.append(group, el('span', 'mo-th-type', column.dtype || column.type));
      th.append(inner);
      if (options.decorateHeader) options.decorateHeader(th, column);
      tr.append(th);
    }
    if (cols.length <= 4) tr.append(el('th', 'mo-filler'));
    table.classList.toggle('mo-auto-width', cols.length <= 4);
    thead.replaceChildren(tr);
  }

  // ---- Client-side filtering / search / sort / paging ----
  function clientView(filters = state.filters) {
    let rows = allRows;
    for (const filter of filters) {
      const column = byName[filter.column];
      rows = rows.filter(r => moMatches(r[column.index], filter, column.filterType));
    }
    if (state.search) {
      const q = state.search.toLowerCase();
      rows = rows.filter(r => r.some(v => v !== null && v !== undefined && String(v).toLowerCase().includes(q)));
    }
    if (state.sort) {
      const i = state.sort.index, sign = state.sort.desc ? -1 : 1;
      rows = [...rows].sort((a, b) => {
        const order = moCompare(a[i], b[i]);
        return (a[i] === null || b[i] === null) ? order : sign * order;  // None stays last either way
      });
    }
    return rows;
  }

  async function refresh(fetch = true) {
    if (fetch) {
      if (server) {
        frame.classList.add('loading');
        try {
          const page = await options.fetchPage({ offset: state.page * state.pageSize, limit: state.pageSize,
            sort: state.sort, search: state.search, filters: state.filters });
          state.rows = page.rows;
          state.total = page.total;
        } catch (err) {
          drawPills(); drawHeader();
          tbody.replaceChildren();
          const tr = el('tr'); const td = el('td', 'mo-empty error', `⚠️ ${err.message}`);
          td.colSpan = visible().length + 1; tr.append(td); tbody.append(tr);
          return;
        } finally {
          frame.classList.remove('loading');
        }
      } else {
        const rows = clientView();
        state.total = rows.length;
        state.rows = rows.slice(state.page * state.pageSize, (state.page + 1) * state.pageSize);
      }
      state.offset = state.page * state.pageSize;
    }
    drawPills();
    drawHeader();
    drawBody();
    drawBottom();
    if (state.viewer.open) drawViewer();
  }

  // ---- Body with cell range selection (marimo range-focus) ----
  let cells = [];  // cells[r][c] -> td, over visible columns
  function drawBody() {
    tbody.replaceChildren();
    cells = [];
    const cols = visible();
    if (!state.rows.length) {
      const tr = el('tr'); const td = el('td', 'mo-empty', 'No results.');
      td.colSpan = cols.length + 1; tr.append(td); tbody.append(tr);
      return;
    }
    state.rows.forEach((row, r) => {
      const tr = el('tr', 'mo-row' + (state.viewer.open && state.offset + r === state.viewer.index ? ' mo-viewed' : ''));
      tr.dataset.r = r;
      cells.push([]);
      cols.forEach((column, c) => {
        const value = row[column.index];
        const td = el('td', 'mo-td' + (column.type === 'number' ? ' mo-num' : '') + (state.wrapped.has(column.name) ? ' mo-wrap' : ''));
        td.tabIndex = -1;
        td.dataset.r = r;
        td.dataset.c = c;
        const content = el('div', 'mo-cell');
        content.append(moCell(value, column.type));
        if (value !== null && value !== undefined && !state.wrapped.has(column.name)) td.title = String(value);
        td.append(content);
        tr.append(td);
        cells[r].push(td);
      });
      if (cols.length <= 4) tr.append(el('td', 'mo-filler'));
      tbody.append(tr);
    });
    paint();
  }

  function selectedRect() {
    if (!state.anchor || !state.focus) return null;
    return { r1: Math.min(state.anchor.r, state.focus.r), r2: Math.max(state.anchor.r, state.focus.r),
      c1: Math.min(state.anchor.c, state.focus.c), c2: Math.max(state.anchor.c, state.focus.c) };
  }
  function selectedCount() {
    const rect = selectedRect();
    return rect ? (rect.r2 - rect.r1 + 1) * (rect.c2 - rect.c1 + 1) : 0;
  }
  function selectedValues() {  // row-major, raw values
    const rect = selectedRect();
    if (!rect) return [];
    const cols = visible();
    const out = [];
    for (let r = rect.r1; r <= rect.r2; r++) {
      const row = [];
      for (let c = rect.c1; c <= rect.c2; c++) row.push(state.rows[r][cols[c].index]);
      out.push(row);
    }
    return out;
  }
  function clearSelection() { state.anchor = state.focus = null; }

  function paint() {
    const rect = selectedRect();
    cells.forEach((row, r) => row.forEach((td, c) => {
      const inside = rect && r >= rect.r1 && r <= rect.r2 && c >= rect.c1 && c <= rect.c2;
      td.classList.toggle('mo-selected', Boolean(inside));
      td.classList.toggle('mo-copied', Boolean(inside && state.copied));
    }));
    drawStats();
  }

  // marimo cell-selection-stats.tsx: Count / Sum / Average for 2+ selected cells.
  function drawStats() {
    stats.replaceChildren();
    const count = selectedCount();
    if (count < 2) {
      const none = el('span', 'mo-no-selection', 'No selection');
      none.title = 'Select multiple cells to see stats';
      stats.append(none);
      return;
    }
    const numbers = selectedValues().flat().filter(v => typeof v !== 'boolean' && v !== null && v !== undefined
      && !(typeof v === 'string' && v.trim() === '') && Number.isFinite(Number(v))).map(Number);
    const parts = [['Count', moPrettyNumber(count)]];
    if (numbers.length) {
      const sum = numbers.reduce((acc, n) => acc + n, 0);
      parts.push(['Sum', moFormatStat(sum)], ['Average', moFormatStat(sum / numbers.length)]);
    }
    for (const [label, value] of parts) stats.append(el('span', '', `${label}: ${value}`));
  }

  function cellAt(target) {
    const td = target.closest && target.closest('td.mo-td');
    return td && tbody.contains(td) ? { r: Number(td.dataset.r), c: Number(td.dataset.c), td } : null;
  }
  function focusCell(cell) {
    const td = cells[cell.r] && cells[cell.r][cell.c];
    if (!td) return;
    td.focus({ preventScroll: true });
    setTimeout(() => td.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' }), 0);
  }

  tbody.addEventListener('mousedown', e => {
    if (e.button !== 0 || moIsInteractive(e.target)) return;
    const cell = cellAt(e.target);
    if (!cell) return;
    if (e.shiftKey && state.anchor) {
      state.focus = { r: cell.r, c: cell.c };
      e.preventDefault();
    } else if (selectedCount() === 1 && state.anchor.r === cell.r && state.anchor.c === cell.c) {
      clearSelection();
    } else {
      state.anchor = state.focus = { r: cell.r, c: cell.c };
    }
    state.dragging = true;
    focusCell(cell);
    paint();
  });
  tbody.addEventListener('mouseover', e => {
    if (!state.dragging || e.buttons !== 1 || !state.anchor) return;
    const cell = cellAt(e.target);
    if (!cell || (state.focus.r === cell.r && state.focus.c === cell.c)) return;
    state.focus = { r: cell.r, c: cell.c };
    if (selectedCount() > 1) window.getSelection()?.removeAllRanges();
    paint();
  });
  document.addEventListener('mouseup', () => { state.dragging = false; });
  tbody.addEventListener('click', e => {
    const tr = e.target.closest('tr.mo-row');
    if (state.viewer.open && tr) { state.viewer.index = state.offset + Number(tr.dataset.r); refresh(false); }
  });

  tbody.addEventListener('keydown', e => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      if (!state.rows.length) return;
      state.anchor = { r: 0, c: 0 };
      state.focus = { r: state.rows.length - 1, c: visible().length - 1 };
      paint();
      return;
    }
    if (mod && e.key.toLowerCase() === 'c') {
      if (window.getSelection()?.toString()) return;  // let the browser copy a text selection
      e.preventDefault();
      copySelection();
      return;
    }
    if (e.key === 'Escape') { clearSelection(); paint(); return; }
    const moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    if (!moves[e.key]) return;
    e.preventDefault();
    const [dr, dc] = moves[e.key];
    const from = state.focus || { r: 0, c: 0 };
    const next = { r: Math.max(0, Math.min(state.rows.length - 1, from.r + dr)), c: Math.max(0, Math.min(visible().length - 1, from.c + dc)) };
    if (e.shiftKey && state.anchor) state.focus = next;
    else state.anchor = state.focus = next;
    focusCell(next);
    paint();
  });

  function copyText(text, message) {
    navigator.clipboard.writeText(text).then(() => message && moToast(message)).catch(() => moToast('Copy failed'));
  }
  function copySelection() {
    const values = selectedValues();
    if (!values.length) return;
    copyText(values.map(row => row.map(moText).join('\t')).join('\n'));
    state.copied = true;  // the green "flash" marimo shows on copy
    paint();
    setTimeout(() => { state.copied = false; paint(); }, 500);
  }

  // marimo context-menu.tsx
  tbody.addEventListener('contextmenu', e => {
    const cell = cellAt(e.target);
    if (!cell) return;
    e.preventDefault();
    const column = visible()[cell.c];
    const value = state.rows[cell.r][column.index];
    const filterable = column.filterType === 'number' || column.filterType === 'text';
    cell.td.classList.add('mo-context');
    moMenu(cell.td, [
      { icon: 'copy', text: 'Copy cell', onClick: () => copyText(moText(value)) },
      selectedCount() > 1 && { icon: 'clipboard-copy', text: 'Copy selected cells', onClick: copySelection },
      filterable && 'separator',
      filterable && { icon: 'funnel', text: 'Filter by this value', onClick: () => setFilter({ column: column.name, operator: 'in', values: [value] }) },
      filterable && { icon: 'funnel-x', text: 'Remove rows with this value', onClick: () => setFilter({ column: column.name, operator: 'not_in', values: [value] }) },
    ], { at: { left: e.clientX, top: e.clientY, bottom: e.clientY }, onClose: () => cell.td.classList.remove('mo-context') });
  });

  // ---- Bottom bar: row count, pagination, selection stats (marimo TableBottomBar / pagination.tsx) ----
  function drawBottom() {
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    const left = el('div', 'mo-bottom-left');
    let text = moRowColumnText(state.total, columns.length, state.hidden.size);
    if (!server && options.total && options.total > allRows.length && !state.search && !state.filters.length) {
      text = `${moRowColumnText(options.total, columns.length, state.hidden.size)} (first ${moPrettyNumber(allRows.length)} shown)`;
    }
    left.append(el('span', '', text));
    const center = el('div', 'mo-pagination');
    const size = el('select', 'mo-page-size');
    size.title = 'Rows per page';
    for (const n of pageSizes) size.append(new Option(`${n} / page`, n));
    size.value = state.pageSize;
    size.onchange = () => { state.pageSize = Number(size.value); state.page = 0; clearSelection(); refresh(); };
    const nav = el('div', 'mo-page-nav');
    const button = (iconName, title, target, disabled, wide) => {
      const b = el('button', 'mo-page-button' + (wide ? ' mo-wide-only' : ''));
      b.type = 'button'; b.title = title; b.disabled = disabled;
      b.append(moIcon(iconName, 16));
      b.onclick = () => { state.page = target; clearSelection(); refresh(); };
      return b;
    };
    const label = el('div', 'mo-page-label');
    const selector = el('button', 'mo-page-selector');
    selector.type = 'button';
    selector.disabled = pages <= 1;
    selector.append(el('span', '', moPrettyNumber(state.page + 1)), moIcon('chevron-down', 12, 'mo-half'));
    selector.onclick = e => {
      e.stopPropagation();
      const input = el('input');
      input.type = 'number'; input.min = 1; input.max = pages;
      input.placeholder = `Page (1–${moPrettyNumber(pages)})`;
      input.onkeydown = ev => {
        if (ev.key === 'Enter') {
          const n = Math.min(pages, Math.max(1, Number(input.value) || 1));
          moCloseMenu(); state.page = n - 1; clearSelection(); refresh();
        }
      };
      moPopover(selector, input, 'mo-page-popover');
      input.focus();
    };
    label.append(el('span', '', 'Page'), selector, el('span', 'mo-shrink-0', `of ${moPrettyNumber(pages)}`));
    const first = state.page === 0, last = state.page >= pages - 1;
    nav.append(button('chevrons-left', 'First page', 0, first, true), button('chevron-left', 'Previous page', state.page - 1, first),
      label, button('chevron-right', 'Next page', state.page + 1, last), button('chevrons-right', 'Last page', pages - 1, last, true));
    center.append(size, nav);
    const rightCell = el('div', 'mo-bottom-right');
    rightCell.append(stats);
    bottom.replaceChildren(left, center, rightCell);
    drawStats();
  }

  // ---- Row viewer (marimo row-viewer-panel) ----
  function toggleViewer() {
    state.viewer.open = !state.viewer.open;
    exploreButton.classList.toggle('active', state.viewer.open);
    wrapper.classList.toggle('mo-viewer-open', state.viewer.open);
    if (state.viewer.open) state.viewer.index = state.offset + (state.focus ? state.focus.r : 0);
    refresh(false);
  }

  async function viewerRow(index) {
    if (index >= state.offset && index < state.offset + state.rows.length) return state.rows[index - state.offset];
    if (server) {
      const page = await options.fetchPage({ offset: index, limit: 1, sort: state.sort, search: state.search, filters: state.filters });
      return page.rows[0];
    }
    return clientView()[index];
  }

  let viewerSearch = '';
  async function drawViewer() {
    viewerPanel.replaceChildren();
    if (!state.viewer.open) return;
    if (!state.total) { viewerPanel.append(el('p', 'mo-muted mo-viewer-empty', 'No rows selected')); return; }
    const index = Math.max(0, Math.min(state.total - 1, state.viewer.index));
    state.viewer.index = index;
    const head = el('div', 'mo-viewer-head');
    const go = (iconName, title, target, disabled) => {
      const b = el('button', 'mo-icon-button mo-viewer-nav');
      b.type = 'button'; b.title = title; b.disabled = disabled;
      b.append(moIcon(iconName, 16));
      b.onclick = () => moveViewer(target);
      return b;
    };
    head.append(go('chevrons-left', 'Go to first row', 0, index === 0), go('chevron-left', 'Previous row', index - 1, index === 0),
      el('span', 'mo-viewer-count', `Row ${moPrettyNumber(index + 1)} of ${moPrettyNumber(state.total)}`),
      go('chevron-right', 'Next row', index + 1, index >= state.total - 1), go('chevrons-right', 'Go to last row', state.total - 1, index >= state.total - 1));
    const close = el('button', 'mo-icon-button');
    close.type = 'button'; close.title = 'Close'; close.append(moIcon('x', 14));
    close.onclick = toggleViewer;
    head.append(close);
    const search = el('input', 'mo-popover-search');
    search.placeholder = 'Search';
    search.value = viewerSearch;
    const list = el('table', 'mo-viewer-table');
    viewerPanel.append(head, search, list);
    const row = await viewerRow(index);
    const draw = () => {
      viewerSearch = search.value;
      const q = search.value.trim().toLowerCase();
      list.replaceChildren();
      const headRow = el('tr');
      headRow.append(el('th', '', 'Column'), el('th', '', 'Value'));
      list.append(headRow);
      for (const column of columns) {
        const value = row ? row[column.index] : null;
        if (q && !column.name.toLowerCase().includes(q) && !moText(value).toLowerCase().includes(q)) continue;
        const tr = el('tr');
        const valueCell = el('td');
        valueCell.append(moCell(value, column.type));
        const copy = el('button', 'mo-icon-button mo-viewer-copy');
        copy.type = 'button'; copy.title = 'Copy value'; copy.append(moIcon('copy', 12));
        copy.onclick = () => copyText(moText(value), 'Copied value');
        valueCell.append(copy);
        tr.append(el('td', 'mo-viewer-name', column.name), valueCell);
        list.append(tr);
      }
    };
    search.oninput = draw;
    draw();
  }

  function moveViewer(index) {
    state.viewer.index = index;
    const page = Math.floor(index / state.pageSize);
    if (page !== state.page) { state.page = page; clearSelection(); refresh(); }
    else refresh(false);
  }

  document.addEventListener('keydown', e => {  // ←/→ move the viewed row, as in marimo
    if (!state.viewer.open || !wrapper.isConnected || e.target.closest('input, textarea, select') || tbody.contains(e.target)) return;
    if (e.key === 'ArrowLeft' && state.viewer.index > 0) moveViewer(state.viewer.index - 1);
    if (e.key === 'ArrowRight' && state.viewer.index < state.total - 1) moveViewer(state.viewer.index + 1);
  });

  // ---- Export (marimo export-actions.tsx, CSV / JSON / Markdown) ----
  function asText(format) {
    const rows = clientView();
    const names = columns.map(c => c.name);
    if (format === 'json') return JSON.stringify(rows.map(r => Object.fromEntries(names.map((n, i) => [n, r[i]]))), null, 1);
    if (format === 'markdown') {
      const esc = v => moText(v).replace(/\|/g, '\\|');
      return [`| ${names.map(esc).join(' | ')} |`, `| ${names.map(() => '---').join(' | ')} |`,
        ...rows.map(r => `| ${r.map(esc).join(' | ')} |`)].join('\n');
    }
    return [names.map(moCsvValue).join(','), ...rows.map(r => r.map(moCsvValue).join(','))].join('\n');
  }
  function download(format) {
    const blob = new Blob([asText(format)], { type: format === 'json' ? 'application/json' : 'text/csv' });
    const a = el('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${options.exportName}.${format}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    moToast(`${format.toUpperCase()} download started`);
  }

  wrapper.refresh = () => { state.page = 0; clearSelection(); return refresh(); };
  refresh();
  return wrapper;
}
