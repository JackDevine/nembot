// Shared DOM and rendering helpers (classic script: globals shared with chat.js/explorer.js).

async function getJSON(url) {
  const response = await fetch(url);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function details(title, body) {
  const d = el('details', 'fold');
  d.append(el('summary', '', title), body);
  return d;
}

function sqlBlock(title, sql) { return details(title, el('pre', '', sql)); }

// Models write "~$370/MWh" for approximations; GFM would read paired single
// tildes as strikethrough, so keep strikethrough to the explicit ~~ form.
function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text.replace(/(^|[^~\\])~(?!~)/g, '$1\\~')));
}

function formatCell(v) {
  if (v === null) return '';
  if (typeof v === 'number' && !Number.isInteger(v)) return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return String(v);
}

// ``actions`` (optional) is an element placed next to the SQL, e.g. an "Add to snippets" button.
function renderTable(event, actions) {
  const wrap = el('div');
  wrap.append(withActions(sqlBlock('SQL query', event.sql), actions));
  const tableWrap = el('div', 'tablewrap');
  const pager = el('div', 'pager');
  wrap.append(tableWrap, pager);
  const pageSize = 10;
  const pages = Math.max(1, Math.ceil(event.rows.length / pageSize));
  let page = 0;
  function draw() {
    const table = el('table');
    const head = el('tr');
    event.columns.forEach(c => head.append(el('th', '', c)));
    table.append(head);
    event.rows.slice(page * pageSize, (page + 1) * pageSize).forEach(row => {
      const tr = el('tr');
      row.forEach(v => tr.append(el('td', typeof v === 'number' ? 'num' : '', formatCell(v))));
      table.append(tr);
    });
    tableWrap.replaceChildren(table);
    const prev = el('button', '', '‹'), next = el('button', '', '›');
    prev.disabled = page === 0; next.disabled = page >= pages - 1;
    prev.onclick = () => { page--; draw(); };
    next.onclick = () => { page++; draw(); };
    const shown = event.total > event.rows.length ? ` (first ${event.rows.length} of ${event.total} rows)` : ` (${event.total} rows)`;
    pager.replaceChildren(prev, el('span', '', `Page ${page + 1} of ${pages}${shown}`), next);
  }
  draw();
  return wrap;
}

function withActions(node, actions) {
  if (!actions) return node;
  const row = el('div', 'result-bar');
  row.append(node, actions);
  return row;
}

function renderPlot(event, actions) {
  const wrap = el('div');
  const spec = el('div');
  spec.append(el('p', '', 'Data is supplied by the plot SQL above.'), el('pre', '', JSON.stringify(event.plot_spec, null, 2)));
  wrap.append(withActions(sqlBlock('Plot SQL', event.sql), actions), details('Plot specification', spec));
  const target = el('div');
  wrap.append(target);
  vegaEmbed(target, event.spec, { actions: { export: true, source: false, compiled: false, editor: false } })
    .catch(err => target.append(el('div', 'error', `Plot failed to render: ${err}`)));
  return wrap;
}
