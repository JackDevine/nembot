// Explorer view: browse the MMS catalogue, table docs and downloaded data.

const explorer = {
  overview: null,      // /api/explorer response
  current: null,       // name of the open table
  detail: null,        // /api/explorer/tables/<name> response
  query: null,         // data-view filters and paging
  docsOpen: localStorage.getItem('docsOpen') === 'yes',
};
const packageList = document.getElementById('package-list');
const tableView = document.getElementById('table-view');
const tableSearch = document.getElementById('table-search');
const downloadedOnly = document.getElementById('downloaded-only');
const tooltip = document.getElementById('tooltip');

const KIND_LABELS = { interval: 'time series', effective: 'effective-dated reference', reference: 'reference' };

function compactNumber(n) {
  return n === null || n === undefined ? '' : Intl.NumberFormat(undefined, { notation: 'compact' }).format(n);
}

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso.replace(' ', 'T'));
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

function sourceLink(source, text) {
  const a = el('a', '', text);
  a.href = source.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
  a.title = `${source.title} v${source.version} (${source.date}), ${source.pages} pages`;
  return a;
}

async function getJSON(url) {
  const response = await fetch(url);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

// ---- Sidebar ----

function drawCatalog() {
  const { source, archive, packages } = explorer.overview;
  const line = document.getElementById('catalog-source');
  line.replaceChildren(
    el('span', '', `${source.title} `), el('b', '', `v${source.version}`), el('span', '', ` · ${formatDate(source.date)} · `),
    sourceLink(source, 'PDF ↗'), el('br'),
    el('span', '', `Tables published in the MMSDM monthly archive (latest: ${archive.month})`),
  );
  const query = tableSearch.value.trim().toUpperCase();
  packageList.replaceChildren();
  for (const pkg of packages) {
    const tables = pkg.tables.filter(t => (!query || t.name.includes(query) || pkg.name.includes(query))
      && (!downloadedOnly.checked || t.downloaded));
    if (!tables.length) continue;
    const box = el('details', 'package');
    const downloaded = tables.filter(t => t.downloaded).length;
    const summary = el('summary', '', pkg.name);
    summary.append(el('span', 'count', ` · ${tables.length}${downloaded ? `, ${downloaded} downloaded` : ''}`));
    box.append(summary);
    box.open = Boolean(query) || downloadedOnly.checked || tables.some(t => t.name === explorer.current);
    for (const t of tables) {
      const link = el('a', 'table-link' + (t.downloaded ? '' : ' missing') + (t.name === explorer.current ? ' current' : ''));
      link.href = `#explorer/${t.name}`;
      link.title = (t.downloaded ? '' : 'Not downloaded yet. ') + t.comment;
      link.append(el('span', '', t.name), el('span', 'rows', t.downloaded ? compactNumber(t.rows) : ''));
      box.append(link);
    }
    packageList.append(box);
  }
  if (!packageList.children.length) packageList.append(el('p', 'empty', 'No matching tables.'));
}

tableSearch.oninput = () => explorer.overview && drawCatalog();
downloadedOnly.onchange = () => explorer.overview && drawCatalog();

// ---- Table view ----

function docBlocks(blocks) {
  const box = el('div');
  for (const block of blocks) {
    if (block.type === 'text') {
      // Short labels such as "Source", "Volume" or "Note" introduce the paragraphs after them.
      const label = /^(Source|Volume|Notes?|Where:|Example:?)$/i.test(block.text);
      box.append(label ? el('h4', 'doc-label', block.text) : el('p', '', block.text));
    }
    else {
      const table = el('table');
      block.rows.forEach((row, i) => {
        const tr = el('tr');
        row.forEach(cell => tr.append(el(i === 0 ? 'th' : 'td', '', cell)));
        table.append(tr);
      });
      box.append(table);
    }
  }
  return box;
}

function columnsTable(detail) {
  const table = el('table', 'columns-table');
  const head = el('tr');
  ['Column', 'Type', 'Key', 'Mandatory', 'Description'].forEach(h => head.append(el('th', '', h)));
  table.append(head);
  for (const c of detail.columns) {
    const tr = el('tr');
    tr.append(el('td', 'mono', c.display_name || c.name),
      el('td', 'mono', `${c.oracle_type} → ${c.type}`),
      el('td', '', detail.primary_key.includes(c.name) ? 'PK' : ''),
      el('td', '', c.mandatory ? 'yes' : ''),
      el('td', '', c.comment));
    table.append(tr);
  }
  return table;
}

function docsPanel(detail) {
  const panel = el('div', 'docs');
  if (detail.description.length) panel.append(el('h3', '', 'Description'), docBlocks(detail.description));
  if (detail.notes.length) panel.append(el('h3', '', 'Notes'), ...detail.notes.map(n => el('p', '', n)));
  panel.append(el('h3', '', 'Keys'),
    el('p', '', `Primary key: ${detail.primary_key.join(', ') || 'none'}`),
    el('p', '', `Indexes: ${detail.indexes.join(', ') || 'none'}`),
    el('p', '', `Visibility: ${detail.visibility || 'not stated'}`));
  panel.append(el('h3', '', 'Columns'), columnsTable(detail));
  const src = el('div', 'doc-source', `Package ${detail.package}. From the ${detail.source.title} v${detail.source.version} (${formatDate(detail.source.date)}), `);
  src.append(sourceLink(detail.source, 'full report PDF ↗'));
  panel.append(src);
  return panel;
}

function columnTooltip(th, column, isKey) {
  th.onmouseenter = e => {
    tooltip.replaceChildren(el('b', '', column.display_name || column.name), el('br'),
      el('span', 'type', `${column.oracle_type} → ${column.type}${isKey ? ' · primary key' : ''}${column.mandatory ? ' · mandatory' : ''}`),
      el('br'), el('span', '', column.comment || 'No description in the data model.'));
    tooltip.style.display = 'block';
    const box = th.getBoundingClientRect();
    tooltip.style.left = `${Math.min(box.left, window.innerWidth - 400)}px`;
    tooltip.style.top = `${box.bottom + 6}px`;
  };
  th.onmouseleave = () => { tooltip.style.display = 'none'; };
}

function drawTable() {
  const d = explorer.detail;
  tableView.replaceChildren();
  const title = el('div', 'title-row');
  const help = el('button', 'help-button' + (explorer.docsOpen ? ' active' : ''), '?');
  help.title = 'Show the full table documentation';
  help.onclick = () => {
    explorer.docsOpen = !explorer.docsOpen;
    localStorage.setItem('docsOpen', explorer.docsOpen ? 'yes' : 'no');
    drawTable();
  };
  const ask = el('button', '', 'Ask the chat about this table');
  ask.onclick = () => askAbout(d.downloaded
    ? `Using the ${d.name} table, give me an overview of what it contains and one interesting insight from the data.`
    : `What does the ${d.name} table contain, and what questions could it help me answer?`);
  title.append(el('h2', '', d.name), el('span', 'badge', KIND_LABELS[d.kind]),
    el('span', d.downloaded ? 'badge ok' : 'badge missing', d.downloaded ? 'downloaded' : 'not downloaded'),
    el('span', 'spacer'), ask, help);
  tableView.append(title, el('p', 'comment', d.comment));

  const facts = el('div', 'facts');
  const fact = (label, value) => { const f = el('span', '', `${label}: `); f.append(el('b', '', value)); facts.append(f); };
  fact('Package', d.package);
  if (d.downloaded) {
    fact('Rows', d.rows.toLocaleString());
    if (d.range) fact(`${d.time_column} range`, `${d.range[0]} → ${d.range[1]}`);
    if (d.files) fact('Last downloaded', `${formatDate(d.files.last)} (${d.files.count} file${d.files.count === 1 ? '' : 's'})`);
  } else if (d.time_column) {
    fact('Time column', d.time_column);
  }
  tableView.append(facts);
  if (explorer.docsOpen) tableView.append(docsPanel(d));

  if (!d.downloaded) {
    tableView.append(el('p', 'empty', 'This table has not been downloaded yet.'));
    if (!explorer.docsOpen) tableView.append(el('h3', '', 'Columns'), columnsTable(d));
    return;
  }
  tableView.append(filterBar(d), el('div', 'tablewrap', ''), el('div', 'pager'));
  loadRows();
}

function filterBar(d) {
  const bar = el('form', 'filters');
  const q = explorer.query;
  const field = (label, control) => { const l = el('label', '', label); l.append(control); bar.append(l); return control; };
  if (d.time_column) {
    for (const [key, label] of [['start', 'From'], ['end', 'To']]) {
      const inputBox = el('input'); inputBox.type = 'date'; inputBox.value = q[key] || '';
      if (d.range) { inputBox.min = d.range[0].slice(0, 10); inputBox.max = d.range[1].slice(0, 10); }
      field(`${label} (${d.time_column})`, inputBox).onchange = () => { q[key] = inputBox.value; q.offset = 0; loadRows(); };
    }
  }
  for (const [column, values] of Object.entries(d.filters)) {
    let control;
    if (values) {
      control = el('select');
      control.append(new Option('All', ''), ...values.map(v => new Option(v, v)));
      control.value = q.values[column] || '';
      control.onchange = () => { q.values[column] = control.value; q.offset = 0; loadRows(); };
    } else {
      control = el('input'); control.type = 'search'; control.placeholder = 'exact match'; control.value = q.values[column] || '';
      let timer;
      control.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { q.values[column] = control.value.trim().toUpperCase(); q.offset = 0; loadRows(); }, 400); };
    }
    field(column, control);
  }
  const size = el('select');
  [10, 25, 50, 100].forEach(n => size.append(new Option(`${n} rows`, n)));
  size.value = q.limit;
  field('Page size', size).onchange = () => { q.limit = Number(size.value); q.offset = 0; loadRows(); };
  bar.onsubmit = e => e.preventDefault();
  return bar;
}

async function loadRows() {
  const d = explorer.detail, q = explorer.query;
  const params = new URLSearchParams({ offset: q.offset, limit: q.limit });
  if (q.start) params.set('start', q.start);
  if (q.end) params.set('end', q.end);
  for (const [k, v] of Object.entries(q.values)) if (v) params.set(k, v);
  const wrap = tableView.querySelector('.tablewrap'), pager = tableView.querySelector('.pager');
  if (!wrap) return;
  wrap.style.opacity = 0.5;
  let data;
  try {
    data = await getJSON(`/api/explorer/tables/${d.name}/rows?${params}`);
  } catch (err) {
    wrap.replaceChildren(el('div', 'error', `⚠️ ${err.message}`));
    wrap.style.opacity = 1;
    return;
  }
  if (explorer.detail !== d) return;  // navigated away meanwhile
  const columns = Object.fromEntries(d.columns.map(c => [c.name, c]));
  const table = el('table', 'data-grid');
  const head = el('tr');
  for (const name of data.columns) {
    const isKey = d.primary_key.includes(name);
    const th = el('th', isKey ? 'key' : '', name);
    if (columns[name]) columnTooltip(th, columns[name], isKey);
    head.append(th);
  }
  table.append(head);
  for (const row of data.rows) {
    const tr = el('tr');
    row.forEach(v => tr.append(el('td', typeof v === 'number' ? 'num' : '', formatCell(v))));
    table.append(tr);
  }
  wrap.replaceChildren(table);
  wrap.style.opacity = 1;
  const prev = el('button', '', '‹'), next = el('button', '', '›');
  prev.disabled = q.offset === 0; next.disabled = q.offset + q.limit >= data.total;
  prev.onclick = () => { q.offset = Math.max(0, q.offset - q.limit); loadRows(); };
  next.onclick = () => { q.offset += q.limit; loadRows(); };
  const first = data.total ? q.offset + 1 : 0, last = Math.min(q.offset + q.limit, data.total);
  pager.replaceChildren(prev, el('span', '', `Rows ${first.toLocaleString()}–${last.toLocaleString()} of ${data.total.toLocaleString()}`), next);
}

async function showExplorer(name) {
  try {
    if (!explorer.overview) explorer.overview = await getJSON('/api/explorer');
    if (name !== explorer.current || !explorer.detail) {
      explorer.current = name;
      explorer.detail = null;
      explorer.query = { offset: 0, limit: 25, start: '', end: '', values: {} };
      if (name) {
        tableView.replaceChildren(el('p', 'empty', `Loading ${name}…`));
        explorer.detail = await getJSON(`/api/explorer/tables/${encodeURIComponent(name)}`);
        drawTable();
      } else {
        tableView.replaceChildren(el('p', 'empty', 'Choose a table on the left. Faded tables have not been downloaded yet.'));
      }
    }
    drawCatalog();
  } catch (err) {
    tableView.replaceChildren(el('div', 'error', `⚠️ ${err.message}`));
  }
}
