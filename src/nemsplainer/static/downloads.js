// Download jobs: polling, header indicator, per-table progress panel and the download dialog.

const downloads = { jobs: [], timer: null, listeners: [], plans: {} };
const indicator = document.getElementById('download-indicator');

function formatBytes(n) {
  if (n === null || n === undefined) return '?';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function monthsBetween(start, end) {
  const [y1, m1] = start.split('-').map(Number), [y2, m2] = end.split('-').map(Number);
  return (y2 - y1) * 12 + (m2 - m1) + 1;
}

function shiftMonth(month, offset) {
  const [y, m] = month.split('-').map(Number);
  const d = new Date(Date.UTC(y, m - 1 + offset, 1));
  return d.toISOString().slice(0, 7);
}

function activeJob() { return downloads.jobs.find(j => j.status === 'running') || downloads.jobs.find(j => j.status === 'queued'); }

function jobPercent(job) {
  const p = job.progress || {};
  if (!p.files) return null;
  const within = p.size ? Math.min(1, (p.bytes || 0) / p.size) : 0;
  const fraction = ((p.file || 1) - 1 + (p.stage === 'load' ? 1 : within)) / p.files;
  return Math.round(100 * fraction);
}

function jobSummary(job) {
  const p = job.progress || {};
  if (job.status === 'queued') return 'Waiting for the current download to finish…';
  if (p.stage === 'listing' || !p.stage) return 'Checking which archive files are needed…';
  if (p.stage === 'resolved') return `${p.files} file${p.files === 1 ? '' : 's'} to fetch (${formatBytes(p.size)})…`;
  if (p.stage === 'download') return `Downloading ${p.month} (file ${p.file} of ${p.files}): ${formatBytes(p.bytes)} of ${formatBytes(p.size)}`;
  if (p.stage === 'load') return `Loading ${p.month || ''} into the database (file ${p.file || '?'} of ${p.files || '?'})…`;
  return 'Finishing…';
}

function drawIndicator() {
  const job = activeJob();
  const failed = downloads.jobs.filter(j => j.status === 'failed');
  indicator.className = 'download-indicator' + (failed.length && !job ? ' failed' : '');
  if (job) {
    const pct = jobPercent(job);
    indicator.textContent = `⬇ ${job.table}${pct === null ? '' : ` · ${pct}%`}`;
    indicator.href = `#explorer/${job.table}`;
    indicator.title = jobSummary(job);
  } else if (failed.length) {
    indicator.textContent = `⚠ Download failed: ${failed[0].table}`;
    indicator.href = `#explorer/${failed[0].table}`;
    indicator.title = failed[0].error;
  } else {
    indicator.textContent = '';
  }
}

async function refreshJobs() {
  const before = Object.fromEntries(downloads.jobs.map(j => [j.id, j.status]));
  try {
    downloads.jobs = (await getJSON('/api/downloads')).jobs;
  } catch (err) {
    console.warn('downloads unavailable', err);
  }
  const finished = downloads.jobs.filter(j => before[j.id] && before[j.id] !== j.status && !['queued', 'running'].includes(j.status));
  drawIndicator();
  downloads.listeners.forEach(fn => fn(finished));
  clearTimeout(downloads.timer);
  if (activeJob()) downloads.timer = setTimeout(refreshJobs, 1000);
}

function onDownloadsChange(fn) { downloads.listeners.push(fn); }

async function startDownload(table, start, end, reason = 'explorer') {
  const response = await fetch('/api/downloads', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ table, start, end, reason }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  await refreshJobs();
  return body;
}

async function cancelDownload(id) {
  await fetch(`/api/downloads/${id}`, { method: 'POST' });
  refreshJobs();
}

async function dismissDownload(id) {
  await fetch(`/api/downloads/${id}`, { method: 'DELETE' });
  refreshJobs();
}

// The latest job for a table, rendered as a progress bar, an error or a note.
function jobPanel(table) {
  const job = [...downloads.jobs].reverse().find(j => j.table === table);
  if (!job || job.status === 'done') return null;
  const panel = el('div', `job-panel ${job.status}`);
  if (job.status === 'failed') {
    panel.append(el('b', '', `Download of ${job.table} (${job.start.slice(0, 7)} to ${job.end.slice(0, 7)}) failed`),
      el('pre', 'job-error', job.error), el('p', '', 'Files that finished before the error were kept; nothing from the failed file was loaded.'));
    const dismiss = el('button', '', 'Dismiss');
    dismiss.onclick = () => dismissDownload(job.id);
    panel.append(dismiss);
  } else if (job.status === 'cancelled') {
    panel.append(el('span', '', 'Download cancelled. Files that finished before cancelling were kept. '));
    const dismiss = el('button', '', 'Dismiss');
    dismiss.onclick = () => dismissDownload(job.id);
    panel.append(dismiss);
  } else {
    const pct = jobPercent(job);
    const bar = el('progress'); bar.max = 100;
    if (pct !== null) bar.value = pct;
    const cancel = el('button', '', 'Cancel');
    cancel.onclick = () => { cancel.disabled = true; cancelDownload(job.id); };
    panel.append(el('div', '', `${job.table}: ${job.start.slice(0, 7)} to ${job.end.slice(0, 7)}`), bar, el('div', 'muted', jobSummary(job)), cancel);
  }
  return panel;
}

// Modal dialog asking for a month range; resolves when the job is submitted (or dismissed).
function downloadDialog(plan, { start, end, title, reason = 'explorer', note = '' } = {}) {
  return new Promise(resolve => {
    const backdrop = el('div', 'modal-backdrop');
    const box = el('form', 'modal');
    const from = el('input'), to = el('input');
    for (const input of [from, to]) { input.type = 'month'; input.min = plan.first_month; input.max = plan.latest_month; input.required = true; }
    from.value = start || plan.latest_month; to.value = end || plan.latest_month;
    const estimate = el('p', 'estimate');
    const error = el('p', 'error');
    const update = () => {
      if (!from.value || !to.value || from.value > to.value) { estimate.textContent = 'Choose a start month on or before the end month.'; return; }
      const months = monthsBetween(from.value, to.value);
      const already = plan.loaded_months.filter(m => m >= from.value && m <= to.value).length;
      const total = plan.month_size === null ? null : plan.month_size * (months - already);
      estimate.replaceChildren(el('span', '', `${months} month${months === 1 ? '' : 's'}`
        + (already ? ` (${already} already downloaded, skipped)` : '')
        + (total === null ? '' : `: about ${formatBytes(total)} to download (${formatBytes(plan.month_size)} for ${plan.latest_month}).`)));
      estimate.className = 'estimate' + (total > 10e9 ? ' huge' : total > 1e9 ? ' large' : '');
      if (total > 1e9) estimate.append(el('br'), el('b', '', total > 10e9
        ? 'This is a very large download. It will take a long time and a lot of disk space.'
        : 'This is a large download.'));
    };
    from.oninput = to.oninput = update;
    const label = (text, input) => { const l = el('label', '', text); l.append(input); return l; };
    const cancel = el('button', '', 'Cancel'); cancel.type = 'button';
    const ok = el('button', 'primary', 'Download'); ok.type = 'submit';
    const close = value => { backdrop.remove(); resolve(value); };
    cancel.onclick = () => close(null);
    window.addEventListener('hashchange', () => close(null), { once: true });
    backdrop.onclick = e => { if (e.target === backdrop) close(null); };
    box.onsubmit = async e => {
      e.preventDefault();
      ok.disabled = true;
      try { close(await startDownload(plan.table, from.value, to.value, reason)); }
      catch (err) { error.textContent = err.message; ok.disabled = false; }
    };
    const semantics = plan.exact_days
      ? 'AEMO publishes monthly archives; rows are kept for the market days in the months you choose.'
      : 'AEMO publishes monthly archives, so downloads cover whole months.';
    box.append(el('h3', '', title || `Download ${plan.table}`));
    if (note) box.append(el('p', '', note));
    box.append(el('div', 'months', ''), estimate, el('p', 'muted', `${semantics} Published months: ${plan.first_month} to ${plan.latest_month}.`
      + (plan.loaded_months.length ? ` Already downloaded: ${plan.loaded_months[0]} to ${plan.loaded_months.at(-1)}.` : '')), error);
    box.querySelector('.months').append(label('From', from), label('To', to));
    const buttons = el('div', 'buttons'); buttons.append(cancel, ok); box.append(buttons);
    backdrop.append(box);
    document.body.append(backdrop);
    update();
    from.focus();
  });
}

async function downloadPlan(table, fresh = false) {
  if (fresh || !downloads.plans[table]) downloads.plans[table] = await getJSON(`/api/downloads/plan/${table}`);
  return downloads.plans[table];
}

refreshJobs();
