// Reports page: every generated report and notebook, newest first.

const reportView = document.getElementById('report-view');

async function showReports() {
  reportView.replaceChildren(el('h2', '', 'Reports and notebooks'),
    el('p', 'muted', 'Ask the chat for a report (HTML, PDF, Word, PowerPoint, Excel) or a notebook (Jupyter, marimo). ' +
      'Each file re-runs its queries when it is built. Notebooks open on local Jupyter/marimo servers that query this app for data.'));
  let list;
  try {
    list = (await getJSON('/api/reports')).reports;
  } catch (err) {
    reportView.append(el('div', 'error', `⚠️ ${err.message}`));
    return;
  }
  if (!list.length) { reportView.append(el('p', 'empty', 'No reports yet.')); return; }
  const table = el('table', 'report-table');
  const head = el('tr');
  ['File', 'Type', 'Size', 'Created', ''].forEach(h => head.append(el('th', '', h)));
  table.append(head);
  for (const report of list) {
    const tr = el('tr');
    const links = el('td'); links.append(reportLinks(report));
    tr.append(el('td', 'mono', report.file), el('td', '', FORMAT_LABELS[report.format] || report.format),
      el('td', 'num', formatBytes(report.bytes)), el('td', '', report.created), links);
    table.append(tr);
  }
  reportView.append(table);
}
