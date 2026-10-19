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

// ``actions`` (optional) is an element placed next to the SQL, e.g. an "Add to snippets" button.
function renderTable(event, actions) {
  const wrap = el('div', 'result-table');
  if (event.sql) wrap.append(withActions(sqlBlock('SQL query', event.sql), actions, event.result_id));
  else if (actions) wrap.append(actions);
  wrap.append(createDataTable({
    columns: event.columns.map(name => ({ name })),
    rows: event.rows,
    total: event.total,
    exportName: event.result_id ? `nemsplainer-${event.result_id}` : 'nemsplainer-table',
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
  vegaEmbed(target, event.spec, { actions: { export: true, source: false, compiled: false, editor: false } })
    .catch(err => target.append(el('div', 'error', `Plot failed to render: ${err}`)));
  return wrap;
}
