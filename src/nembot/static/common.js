// Shared DOM and rendering helpers (classic script: globals shared with chat.js/explorer.js).

// "Data: AEMO" in the header: the attribution AEMO's terms ask for.
async function showAbout() {
  const info = await getJSON('/api/about');
  const backdrop = el('div', 'modal-backdrop');
  const box = el('div', 'modal');
  const link = el('a', '', 'AEMO Copyright Permissions'); link.href = info.copyright_url; link.target = '_blank'; link.rel = 'noopener';
  const close = el('button', 'primary', 'Close');
  close.onclick = () => backdrop.remove();
  backdrop.onclick = e => { if (e.target === backdrop) backdrop.remove(); };
  const buttons = el('div', 'buttons'); buttons.append(close);
  const terms = el('p', 'muted'); terms.append('See ', link, '.');
  box.append(el('h3', '', 'Data source'), el('p', '', info.credit), terms,
    el('p', 'muted', `Table documentation: ${info.data_model.title} v${info.data_model.version} (${info.data_model.date}), AEMO.`), buttons);
  backdrop.append(box);
  document.body.append(backdrop);
}
document.getElementById('about-button')?.addEventListener('click', showAbout);

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

function sqlBlock(title, sql) { return details(title, codeBlock(sql, 'sql')); }

// Syntax highlighting for SQL: CodeMirror's Lezer parser (MIT, loaded from esm.sh on first use)
// with marimo's editor colours (the .tok-* rules in app.css). The code shows as plain text at
// once and is highlighted when the parser arrives.
const LEZER = { highlight: '1.2.5', sql: '6.10.0' };
let highlighter = null;

function loadHighlighter() {
  highlighter ||= Promise.all([
    import(`https://esm.sh/@lezer/highlight@${LEZER.highlight}`),
    import(`https://esm.sh/@codemirror/lang-sql@${LEZER.sql}`),
  ]).then(([{ tags: t, tagHighlighter, highlightCode }, sql]) => ({
    highlightCode,
    // Tags to marimo's colour roles. marimo colours SQL functions (avg, range) like keywords.
    style: tagHighlighter([
      { tag: [t.keyword, t.operatorKeyword, t.modifier, t.typeName, t.standard(t.name)], class: 'tok-keyword' },
      { tag: [t.string, t.special(t.string)], class: 'tok-string' },
      { tag: t.number, class: 'tok-number' },
      { tag: t.bool, class: 'tok-bool' },
      { tag: t.null, class: 'tok-null' },
      { tag: t.operator, class: 'tok-operator' },
      { tag: t.propertyName, class: 'tok-property' },
      { tag: [t.comment, t.lineComment, t.blockComment], class: 'tok-comment' },
    ]),
    parsers: { sql: sql.PostgreSQL.language.parser },  // DuckDB's SQL is Postgres-like
  }));
  return highlighter;
}

function codeBlock(code, language) {
  const pre = el('pre', `code lang-${language}`, code);
  loadHighlighter().then(({ highlightCode, style, parsers }) => {
    const out = document.createDocumentFragment();
    highlightCode(code, parsers[language].parse(code), style,
      (text, classes) => out.append(classes ? el('span', classes, text) : text), () => out.append('\n'));
    pre.replaceChildren(out);
  }).catch(() => {});  // offline: the plain text stays
  return pre;
}

// JSON (e.g. a Vega-Lite spec) as a collapsible tree in the same colours. Click ▾ to fold an
// object or array; Alt+click folds or unfolds everything inside it too.
function jsonTree(value) {
  const pre = el('pre', 'code json-tree');
  pre.append(jsonNode(value, null, true));
  return pre;
}

function jsonNode(value, key, last) {
  const label = key === null ? [] : [el('span', 'tok-property', JSON.stringify(key)), ': '];
  const comma = last ? '' : ',';
  const isArray = Array.isArray(value);
  const entries = value !== null && typeof value === 'object' ? (isArray ? value.map(v => [null, v]) : Object.entries(value)) : null;
  if (!entries || !entries.length) {
    const line = el('div', 'jt-line');
    line.append(...label, entries ? (isArray ? '[]' : '{}') : jsonScalar(value), comma);
    return line;
  }
  const [open, close] = isArray ? ['[', ']'] : ['{', '}'];
  const node = el('details', 'jt');
  node.open = true;
  const summary = el('summary');
  summary.append(...label, open);
  const noun = isArray ? (entries.length === 1 ? 'item' : 'items') : (entries.length === 1 ? 'key' : 'keys');
  summary.dataset.folded = ` ${entries.length} ${noun} ${close}${comma}`;
  summary.onclick = e => {
    if (!e.altKey) return;
    e.preventDefault();
    const unfold = !node.open;
    for (const d of [node, ...node.querySelectorAll('details')]) d.open = unfold;
  };
  const body = el('div', 'jt-body');
  entries.forEach(([k, v], i) => body.append(jsonNode(v, isArray ? null : k, i === entries.length - 1)));
  node.append(summary, body, el('div', 'jt-close', close + comma));
  return node;
}

function jsonScalar(value) {
  if (value === null) return el('span', 'tok-null', 'null');
  const kind = { string: 'tok-string', number: 'tok-number', boolean: 'tok-bool' }[typeof value];
  return el('span', kind || '', JSON.stringify(value));
}

// Models write "~$370/MWh" for approximations; GFM would read paired single
// tildes as strikethrough, so keep strikethrough to the explicit ~~ form.
function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(escapeTildes(text)));
}

// The same for text that goes inside an existing paragraph (no <p> wrapper).
function renderInlineMarkdown(text) {
  return DOMPurify.sanitize(marked.parseInline(escapeTildes(text)));
}

function escapeTildes(text) { return text.replace(/(^|[^~\\])~(?!~)/g, '$1\\~'); }

// ``actions`` (optional) is an element placed next to the SQL, e.g. an "Add to snippets" button.
function renderTable(event, actions) {
  const wrap = el('div', 'result-table');
  if (event.sql) wrap.append(withActions(sqlBlock('SQL query', event.sql), actions, event.result_id));
  else if (actions) wrap.append(actions);
  wrap.append(createDataTable({
    columns: event.columns.map(name => ({ name })),
    rows: event.rows,
    total: event.total,
    exportName: event.result_id ? `nembot-${event.result_id}` : 'nembot-table',
  }));
  return wrap;
}

function withActions(node, actions, resultId) {
  if (!actions && !resultId) return node;
  const row = el('div', 'result-bar');
  row.append(node);
  if (resultId) {
    const tag = el('span', 'result-id', resultId);
    tag.title = `Result ${resultId}: ask for a report and the assistant can include it`;
    row.append(tag);
  }
  if (actions) row.append(actions);
  return row;
}

const FORMAT_LABELS = { html: 'HTML page', pdf: 'PDF', docx: 'Word document', pptx: 'PowerPoint deck',
  xlsx: 'Excel workbook', jupyter: 'Jupyter notebook', marimo: 'marimo notebook' };

// Links for a generated report: open/download, or launch the notebook server.
function reportLinks(report) {
  const links = el('span', 'report-links');
  const add = (text, href, download) => {
    const a = el('a', '', text); a.href = href; a.target = '_blank'; a.rel = 'noopener';
    if (download) a.setAttribute('download', report.file);
    links.append(a);
  };
  const file = `/reports/${encodeURIComponent(report.file)}`;
  if (report.format === 'jupyter' || report.format === 'marimo') {
    add(`Open in ${report.format === 'jupyter' ? 'JupyterLab' : 'marimo'} ↗`, `/notebooks/open/${encodeURIComponent(report.file)}`);
    add('Download', file, true);
  } else if (report.format === 'html' || report.format === 'pdf') {
    add('Open ↗', file);
    add('Download', file, true);
  } else {
    add('Download', file, true);
  }
  return links;
}

function renderPlot(event, actions) {
  const wrap = el('div');
  const spec = el('div');
  spec.append(el('p', '', 'Data is supplied by the plot SQL above.'), jsonTree(event.plot_spec));
  // The plot's SQL and its data together (older saved chats have no data here: it was shown at the end).
  const query = el('div', 'plot-query');
  query.append(codeBlock(event.sql, 'sql'));
  if (event.columns) query.append(createDataTable({ columns: event.columns.map(name => ({ name })), rows: event.rows,
    total: event.total, exportName: event.result_id ? `nembot-${event.result_id}` : 'nembot-plot-data' }));
  wrap.append(withActions(details(event.columns ? 'Plot SQL and data' : 'Plot SQL', query), actions, event.result_id),
    details('Plot specification', spec));
  const target = el('div');
  wrap.append(target);
  embedChart(target, event.spec);
  return wrap;
}

// A Vega-Lite chart in the page's light or dark theme. Charts are redrawn when the browser
// switches theme; they're tracked weakly, so charts removed from the page are simply freed.
const darkScheme = matchMedia('(prefers-color-scheme: dark)');
const themedCharts = new Set();  // WeakRefs to chart targets; each target keeps its spec and view

function embedChart(target, spec) {
  target.nbChart = { spec, view: null };
  themedCharts.add(new WeakRef(target));
  return drawChart(target);
}

function drawChart(target) {
  const chart = target.nbChart;
  return vegaEmbed(target, chart.spec, { actions: { export: true, source: false, compiled: false, editor: false },
    ...(darkScheme.matches && { theme: 'dark', config: { background: 'transparent' } }) })
    .then(result => { chart.view = result; })
    .catch(err => target.append(el('div', 'error', `Plot failed to render: ${err}`)));
}

darkScheme.addEventListener('change', () => {
  for (const ref of [...themedCharts]) {
    const target = ref.deref();
    if (!target || !target.isConnected) { themedCharts.delete(ref); continue; }
    target.nbChart.view?.finalize();  // release the old view's listeners and timers
    target.replaceChildren();
    drawChart(target);
  }
});

// Read a server-sent event stream of JSON `data:` lines, calling handle(event) for each.
async function streamEvents(response, handle) {
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += value;
    let cut;
    while ((cut = buffer.indexOf('\n\n')) >= 0) {
      const line = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      if (line.startsWith('data: ')) handle(JSON.parse(line.slice(6)));
    }
  }
}
