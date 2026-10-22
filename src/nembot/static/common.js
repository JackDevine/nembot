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

function sqlBlock(title, sql) { return details(title, el('pre', '', sql)); }

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
  spec.append(el('p', '', 'Data is supplied by the plot SQL above.'), el('pre', '', JSON.stringify(event.plot_spec, null, 2)));
  wrap.append(withActions(sqlBlock('Plot SQL', event.sql), actions, event.result_id), details('Plot specification', spec));
  const target = el('div');
  wrap.append(target);
  embedChart(target, event.spec);
  return wrap;
}

// A Vega-Lite chart in the page's light or dark theme.
function embedChart(target, spec) {
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  return vegaEmbed(target, spec, { actions: { export: true, source: false, compiled: false, editor: false },
    ...(dark && { theme: 'dark', config: { background: 'transparent' } }) })
    .catch(err => target.append(el('div', 'error', `Plot failed to render: ${err}`)));
}

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
