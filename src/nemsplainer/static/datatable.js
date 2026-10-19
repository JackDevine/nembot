/*
 * Data table in the style of marimo's data table.
 *
 * Ported from marimo (https://github.com/marimo-team/marimo):
 *   frontend/src/components/ui/table.tsx, frontend/src/components/data-table/
 *   (renderers.tsx, columns.tsx, column-header.tsx, header-items.tsx, pagination.tsx,
 *   TableTopBar.tsx, TableBottomBar.tsx, SearchBar.tsx, sentinel-cell.tsx, utils.ts)
 *   and frontend/src/css (globals.css, table.css).
 * Copyright 2026 Marimo. Licensed under the Apache License, Version 2.0; see
 * LICENSES/marimo-LICENSE.txt. Changes: rewritten from React + TanStack Table + Tailwind
 * into a dependency-free DOM component. The markup, the Tailwind classes (resolved to
 * concrete CSS in app.css under .mo-*), the light-theme colours, number and None
 * formatting, the sort button and column menu, search, export, pagination and the
 * row-count text follow marimo's implementation. Not every marimo feature is ported
 * (no cell-range selection, filter pills, column pinning or virtualisation).
 *
 * Icons are Lucide icons (ISC licence, see LICENSES/lucide-LICENSE.txt), the same set marimo uses.
 *
 * createDataTable(options) -> HTMLElement
 *   columns: [{ name, type?: 'number'|'string'|'date'|'datetime'|'boolean', dtype?: label shown under the name }]
 *   rows: [[...]] for client-side data (search, sort and paging happen here), or
 *   fetchPage: async ({ offset, limit, sort: { column, desc } | null }) => ({ rows, total }) for server-side data
 *   total: total rows when `rows` holds only the first part of a larger result
 *   pageSize (default 10), pageSizes, search (client mode, default true), exportName (enables Export)
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
function moRowColumnText(rows, columns) {
  return `${moPrettyNumber(rows)} ${rows === 1 ? 'row' : 'rows'}, ${moPrettyNumber(columns)} ${columns === 1 ? 'column' : 'columns'}`;
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

let moOpenMenu = null;
document.addEventListener('click', e => {
  if (moOpenMenu && !moOpenMenu.contains(e.target)) { moOpenMenu.remove(); moOpenMenu = null; }
});

function moMenu(anchor, items) {
  if (moOpenMenu) moOpenMenu.remove();
  const menu = el('div', 'mo-menu');
  for (const item of items) {
    if (item === 'separator') { menu.append(el('div', 'mo-menu-sep')); continue; }
    if (item.label && !item.onClick) { menu.append(el('div', 'mo-menu-label', item.label)); continue; }
    const row = el('button', 'mo-menu-item' + (item.active ? ' active' : ''));
    row.type = 'button';
    if (item.icon) row.append(moIcon(item.icon, 14, 'mo-menu-icon'));
    row.append(el('span', '', item.text));
    row.onclick = e => { e.stopPropagation(); menu.remove(); moOpenMenu = null; item.onClick(); };
    menu.append(row);
  }
  const box = anchor.getBoundingClientRect();
  menu.style.left = `${Math.min(box.left, window.innerWidth - 200)}px`;
  menu.style.top = `${box.bottom + 4}px`;
  document.body.append(menu);
  moOpenMenu = menu;
}

function moToast(text) {
  const toast = el('div', 'mo-toast', text);
  document.body.append(toast);
  setTimeout(() => toast.remove(), 2000);
}

function createDataTable(options) {
  const server = typeof options.fetchPage === 'function';
  const allRows = options.rows || [];
  const columns = options.columns.map((c, i) => ({
    ...c, index: i,
    type: c.type || moInferType(allRows.map(r => r[i])),
  }));
  const state = { page: 0, pageSize: options.pageSize || 10, sort: null, search: '', rows: [], total: 0, loading: false };
  const pageSizes = options.pageSizes || [5, 10, 25, 50, 100];

  const wrapper = el('div', 'mo-table-wrapper');
  const frame = el('div', 'mo-table');
  frame.setAttribute('part', 'table-wrapper');
  wrapper.append(frame);

  // ---- Top bar: search and export (marimo TableTopBar / SearchBar) ----
  const showSearch = !server && options.search !== false && allRows.length > 5;
  const showExport = Boolean(options.exportName) && !server;
  if (showSearch || showExport) {
    const top = el('div', 'mo-topbar');
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
        timer = setTimeout(() => { state.search = input.value.trim().toLowerCase(); state.page = 0; refresh(); }, 500);
      };
      input.onkeydown = e => { if (e.key === 'Escape') { input.value = ''; input.oninput(); input.blur(); } };
      clear.onclick = () => { input.value = ''; input.oninput(); };
      search.append(moIcon('search', 16, 'mo-muted'), input, clear);
      top.append(search);
    }
    if (showExport) {
      const right = el('div', 'mo-topbar-right');
      const exportButton = el('button', 'mo-text-button');
      exportButton.type = 'button';
      exportButton.append(moIcon('download', 14), el('span', '', 'Export'));
      exportButton.onclick = e => {
        e.stopPropagation();
        moMenu(exportButton, [
          { icon: 'download', text: 'Download CSV', onClick: () => download('csv') },
          { icon: 'download', text: 'Download JSON', onClick: () => download('json') },
          'separator',
          { icon: 'copy', text: 'Copy CSV', onClick: () => copy('csv') },
          { icon: 'copy', text: 'Copy Markdown', onClick: () => copy('markdown') },
        ]);
      };
      right.append(exportButton);
      top.append(right);
    }
    frame.append(top);
  }

  const scroll = el('div', 'mo-scroll');
  const table = el('table', 'mo-grid' + (columns.length <= 4 ? ' mo-auto-width' : ''));
  const thead = el('thead');
  const tbody = el('tbody');
  table.append(thead, tbody);
  scroll.append(table);
  frame.append(scroll);
  const bottom = el('div', 'mo-bottombar');
  frame.append(bottom);

  // ---- Header (marimo columns.tsx DataTableColumnHeader) ----
  function setSort(column, desc) {
    state.sort = desc === null ? null : { column: column.name, index: column.index, desc };
    state.page = 0;
    refresh();
  }

  function drawHeader() {
    const tr = el('tr');
    for (const column of columns) {
      const th = el('th', 'mo-th');
      const inner = el('div', 'mo-th-inner');
      const group = el('div', 'mo-th-group');
      const row = el('div', 'mo-th-row' + (column.type === 'number' && options.alignNumberHeaders ? ' reverse' : ''));
      row.append(el('span', 'mo-th-name', column.label || column.name));
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
        moMenu(menuButton, [
          { label: column.dtype || column.type },
          'separator',
          { icon: 'arrow-up-narrow-wide', text: 'Asc', active: sorted && !state.sort.desc, onClick: () => setSort(column, sorted && !state.sort.desc ? null : false) },
          { icon: 'arrow-down-wide-narrow', text: 'Desc', active: sorted && state.sort.desc, onClick: () => setSort(column, sorted && state.sort.desc ? null : true) },
          { icon: 'chevrons-up-down', text: 'Clear sort', onClick: () => setSort(column, null) },
          { icon: 'copy', text: 'Copy column name', onClick: () => navigator.clipboard.writeText(column.name).then(() => moToast('Copied column name')) },
        ]);
      };
      row.append(sortButton, menuButton);
      group.append(row);
      inner.append(group, el('span', 'mo-th-type', column.dtype || column.type));
      th.append(inner);
      if (options.decorateHeader) options.decorateHeader(th, column);
      tr.append(th);
    }
    if (columns.length <= 4) tr.append(el('th', 'mo-filler'));
    thead.replaceChildren(tr);
  }

  // ---- Client-side search / sort / paging ----
  function clientView() {
    let rows = allRows;
    if (state.search) rows = rows.filter(r => r.some(v => v !== null && String(v).toLowerCase().includes(state.search)));
    if (state.sort) {
      const i = state.sort.index, sign = state.sort.desc ? -1 : 1;
      rows = [...rows].sort((a, b) => {
        const order = moCompare(a[i], b[i]);
        return (a[i] === null || b[i] === null) ? order : sign * order;  // None stays last either way
      });
    }
    return rows;
  }

  async function refresh() {
    if (server) {
      state.loading = true;
      frame.classList.add('loading');
      try {
        const page = await options.fetchPage({ offset: state.page * state.pageSize, limit: state.pageSize, sort: state.sort });
        state.rows = page.rows;
        state.total = page.total;
      } catch (err) {
        tbody.replaceChildren();
        const tr = el('tr'); const td = el('td', 'mo-empty error', `⚠️ ${err.message}`);
        td.colSpan = columns.length + 1; tr.append(td); tbody.append(tr);
        return;
      } finally {
        state.loading = false;
        frame.classList.remove('loading');
      }
    } else {
      const rows = clientView();
      state.total = rows.length;
      state.rows = rows.slice(state.page * state.pageSize, (state.page + 1) * state.pageSize);
    }
    drawHeader();
    drawBody();
    drawBottom();
  }

  function drawBody() {
    tbody.replaceChildren();
    if (!state.rows.length) {
      const tr = el('tr'); const td = el('td', 'mo-empty', 'No results.');
      td.colSpan = columns.length + 1; tr.append(td); tbody.append(tr);
      return;
    }
    for (const row of state.rows) {
      const tr = el('tr', 'mo-row');
      columns.forEach((column, i) => {
        const value = row[i];
        const td = el('td', 'mo-td' + (column.type === 'number' ? ' mo-num' : ''));
        td.tabIndex = 0;
        const content = el('div', 'mo-cell');
        content.append(moCell(value, column.type));
        if (value !== null && value !== undefined) td.title = String(value);
        td.append(content);
        tr.append(td);
      });
      if (columns.length <= 4) tr.append(el('td', 'mo-filler'));
      tbody.append(tr);
    }
  }

  // ---- Bottom bar: row count and pagination (marimo TableBottomBar / pagination.tsx) ----
  function drawBottom() {
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    const left = el('div', 'mo-bottom-left');
    let text = moRowColumnText(state.total, columns.length);
    if (!server && options.total && options.total > allRows.length && !state.search) {
      text = `${moRowColumnText(options.total, columns.length)} (first ${moPrettyNumber(allRows.length)} shown)`;
    }
    left.append(el('span', '', text));
    const center = el('div', 'mo-pagination');
    const size = el('select', 'mo-page-size');
    size.title = 'Rows per page';
    for (const n of pageSizes) size.append(new Option(`${n} / page`, n));
    size.value = state.pageSize;
    size.onchange = () => { state.pageSize = Number(size.value); state.page = 0; refresh(); };
    const nav = el('div', 'mo-page-nav');
    const button = (iconName, title, target, disabled, wide) => {
      const b = el('button', 'mo-page-button' + (wide ? ' mo-wide-only' : ''));
      b.type = 'button'; b.title = title; b.disabled = disabled;
      b.append(moIcon(iconName, 16));
      b.onclick = () => { state.page = target; refresh(); };
      return b;
    };
    const label = el('div', 'mo-page-label');
    const selector = el('button', 'mo-page-selector');
    selector.type = 'button';
    selector.disabled = pages <= 1;
    selector.append(el('span', '', moPrettyNumber(state.page + 1)), moIcon('chevron-down', 12, 'mo-half'));
    selector.onclick = e => {
      e.stopPropagation();
      if (moOpenMenu) moOpenMenu.remove();
      const pop = el('div', 'mo-menu mo-page-popover');
      const input = el('input');
      input.type = 'number'; input.min = 1; input.max = pages;
      input.placeholder = `Page (1–${moPrettyNumber(pages)})`;
      input.onkeydown = ev => {
        if (ev.key === 'Enter') {
          const n = Math.min(pages, Math.max(1, Number(input.value) || 1));
          pop.remove(); moOpenMenu = null; state.page = n - 1; refresh();
        }
      };
      pop.append(input);
      const box = selector.getBoundingClientRect();
      pop.style.left = `${box.left}px`;
      pop.style.top = `${box.top - 48}px`;
      document.body.append(pop);
      moOpenMenu = pop;
      input.focus();
    };
    label.append(el('span', '', 'Page'), selector, el('span', 'mo-shrink-0', `of ${moPrettyNumber(pages)}`));
    const first = state.page === 0, last = state.page >= pages - 1;
    nav.append(button('chevrons-left', 'First page', 0, first, true), button('chevron-left', 'Previous page', state.page - 1, first),
      label, button('chevron-right', 'Next page', state.page + 1, last), button('chevrons-right', 'Last page', pages - 1, last, true));
    center.append(size, nav);
    bottom.replaceChildren(left, center, el('div', 'mo-bottom-right'));
  }

  // ---- Export (marimo export-actions.tsx, CSV / JSON / Markdown) ----
  function asText(format) {
    const rows = clientView();
    const names = columns.map(c => c.name);
    if (format === 'json') return JSON.stringify(rows.map(r => Object.fromEntries(names.map((n, i) => [n, r[i]]))), null, 1);
    if (format === 'markdown') {
      const esc = v => (v === null || v === undefined ? '' : String(v)).replace(/\|/g, '\\|');
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
  function copy(format) {
    navigator.clipboard.writeText(asText(format)).then(() => moToast(`Copied ${format === 'csv' ? 'CSV' : 'Markdown'} to clipboard`));
  }

  wrapper.refresh = () => { state.page = 0; return refresh(); };
  refresh();
  return wrapper;
}
