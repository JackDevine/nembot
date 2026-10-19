// Chat view: model picker, web search toggle, streaming replies.

const log = document.getElementById('log');
const form = document.getElementById('form');
const input = document.getElementById('input');
const send = document.getElementById('send');
let conversation = crypto.randomUUID();

// TODO: replace this toggle once there's a UI for choosing which tools are available.
const webSearch = document.getElementById('web-search');
webSearch.checked = localStorage.getItem('webSearch') !== 'off';
webSearch.onchange = () => localStorage.setItem('webSearch', webSearch.checked ? 'on' : 'off');

// ---- Model picker ----
const picker = document.getElementById('picker');
const modelButton = document.getElementById('model-button');
const modelSearch = document.getElementById('model-search');
const modelList = document.getElementById('model-list');
let models = [];
let currentModel = null;

function modelBadges(m) {
  const meta = el('span', 'meta');
  meta.append(m.vision ? el('span', 'badge image', 'text + image') : el('span', 'badge textonly', 'text only'));
  if (!m.tools) meta.append(el('span', 'badge notools', 'no tools'));
  if (m.price_in !== null && m.price_out !== null) meta.append(el('span', 'badge', `$${m.price_in}/$${m.price_out} per M`));
  return meta;
}

function setModel(m) {
  currentModel = m;
  localStorage.setItem('model', m.ref);
  modelButton.textContent = `${m.provider === 'claude' ? 'Claude' : 'OpenRouter'} · ${m.name}`;
  modelButton.title = `${m.ref} — ${m.vision ? 'text + image input' : 'text-only input: cannot see plots'}`;
}

function drawModels() {
  const query = modelSearch.value.trim().toLowerCase();
  modelList.replaceChildren();
  for (const [provider, title] of [['claude', 'Claude (Anthropic API)'], ['openrouter', 'OpenRouter']]) {
    const group = models.filter(m => m.provider === provider &&
      (!query || m.name.toLowerCase().includes(query) || m.id.toLowerCase().includes(query)));
    if (!group.length) continue;
    modelList.append(el('h3', '', `${title} · ${group.length}`));
    for (const m of group) {
      const usable = m.tools && m.available;
      const row = el('div', 'model' + (usable ? '' : ' disabled') + (currentModel && m.ref === currentModel.ref ? ' current' : ''));
      row.title = !m.available ? 'Provider credentials are not configured (OPENROUTER_API_KEY).'
        : !m.tools ? 'This model does not support tool calling, so it cannot query the database.' : m.id;
      row.append(el('span', '', m.name), modelBadges(m));
      if (usable) row.onclick = () => { setModel(m); picker.classList.remove('open'); input.focus(); };
      modelList.append(row);
    }
  }
}

async function loadModels() {
  const response = await fetch('/api/models');
  const body = await response.json();
  models = body.models;
  body.errors.forEach(message => console.warn(message));
  const saved = models.find(m => m.ref === localStorage.getItem('model') && m.tools && m.available);
  const fallback = models.find(m => m.ref === body.default) || { ref: body.default, provider: 'claude', name: 'Claude Opus 5.5', vision: true };
  setModel(saved || fallback);
}

modelButton.onclick = () => {
  picker.classList.toggle('open');
  if (picker.classList.contains('open')) { modelSearch.value = ''; drawModels(); modelSearch.focus(); }
};
modelSearch.oninput = drawModels;
document.addEventListener('click', e => {
  if (!picker.contains(e.target) && e.target !== modelButton) picker.classList.remove('open');
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') picker.classList.remove('open'); });
loadModels().catch(err => { modelButton.textContent = 'Models unavailable'; console.error(err); });

document.getElementById('reset').onclick = () => {
  conversation = crypto.randomUUID();
  log.replaceChildren();
};
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});

// The popup for a request_download tool call: approve (optionally changing months) or decline.
function downloadRequestCard(event) {
  const card = el('div', 'download-card');
  const plan = event.plan;
  const head = el('div', 'download-card-head');
  const link = el('a', '', event.table); link.href = `#explorer/${event.table}`;
  head.append(el('b', '', 'The assistant wants to download '), link);
  const from = el('input'), to = el('input');
  for (const input of [from, to]) { input.type = 'month'; input.min = plan.first_month; input.max = plan.latest_month; }
  from.value = event.start; to.value = event.end;
  const estimate = el('div', 'muted');
  const update = () => {
    if (!from.value || !to.value || from.value > to.value) { estimate.textContent = 'Choose a start month on or before the end month.'; return; }
    const months = monthsBetween(from.value, to.value);
    const already = plan.loaded_months.filter(m => m >= from.value && m <= to.value).length;
    estimate.textContent = `${months} month${months === 1 ? '' : 's'}` + (already ? ` (${already} already downloaded)` : '')
      + (plan.month_size === null ? '' : `, about ${formatBytes(plan.month_size * (months - already))}`)
      + (plan.exact_days ? '' : '. Whole months are downloaded.');
  };
  from.oninput = to.oninput = update;
  const months = el('div', 'months');
  for (const [label, input] of [['From', from], ['To', to]]) { const l = el('label', '', label); l.append(input); months.append(l); }
  const approve = el('button', 'primary', 'Download'), decline = el('button', '', 'Not now');
  const buttons = el('div', 'buttons'); buttons.append(decline, approve);
  const status = el('div', 'download-status');
  const decide = async ok => {
    approve.disabled = decline.disabled = from.disabled = to.disabled = true;
    const response = await fetch('/api/chat/decision', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation, request: event.id, approve: ok, start: from.value, end: to.value }),
    });
    if (!response.ok) status.textContent = (await response.json()).error;
  };
  approve.onclick = () => decide(true);
  decline.onclick = () => decide(false);
  card.append(head, el('p', 'reason', event.reason), el('p', 'muted', event.comment), months, estimate, buttons, status);
  update();
  card.dataset.request = event.id;
  return card;
}

function updateDownloadCard(card, event) {
  const status = card.querySelector('.download-status');
  card.querySelector('.buttons')?.remove();
  if (event.type === 'download_declined') {
    status.replaceChildren(el('span', 'muted', event.timeout ? 'No answer; the request expired.' : 'Declined.'));
    return;
  }
  const job = event.job;
  if (job.status === 'queued' || job.status === 'running') {
    const bar = el('progress'); bar.max = 100;
    const pct = jobPercent(job);
    if (pct !== null) bar.value = pct;
    const cancel = el('button', '', 'Cancel');
    cancel.onclick = () => { cancel.disabled = true; cancelDownload(job.id); };
    status.replaceChildren(bar, el('div', 'muted', jobSummary(job)), cancel);
  } else if (job.status === 'done') {
    status.replaceChildren(el('span', 'ok', `✓ Downloaded ${job.table} (${job.start.slice(0, 7)} to ${job.end.slice(0, 7)}).`));
  } else if (job.status === 'cancelled') {
    status.replaceChildren(el('span', 'muted', 'Download cancelled.'));
  } else {
    const box = el('div', 'job-panel failed');
    box.append(el('b', '', 'Download failed'), el('pre', 'job-error', job.error));
    status.replaceChildren(box);
  }
}

async function ask(text) {
  const turn = el('div', 'turn');
  const reply = el('div', 'assistant');
  turn.append(el('div', 'user', text), reply);
  log.append(turn);
  const status = el('div', 'status', 'Thinking…');
  reply.append(status);
  let segment = null, markdown = '';
  const scroll = () => { log.scrollTop = log.scrollHeight; };
  const place = node => { reply.insertBefore(node, status); scroll(); };

  function handle(event) {
    switch (event.type) {
      case 'block': segment = null; break;
      case 'text':
        if (!segment || segment.className !== 'text') { segment = el('div', 'text'); markdown = ''; place(segment); }
        markdown += event.delta;
        segment.innerHTML = renderMarkdown(markdown);
        scroll();
        break;
      case 'progress':
        if (!segment || segment.className !== 'progress') { segment = el('div', 'progress'); place(segment); }
        segment.textContent += event.delta;
        break;
      case 'model':
        reply.prepend(el('div', 'via', `${event.model.provider === 'claude' ? 'Claude' : 'OpenRouter'} · ${event.model.name}`));
        break;
      case 'tool':
      case 'tool_running':
        segment = null;
        status.textContent = { visualize_mms: 'Building plot…', query_mms: 'Running query…',
          describe_table: 'Reading table docs…', request_download: 'Preparing a download request…',
          web_search: 'Searching the web…', web_fetch: 'Reading a web page…', code_execution: 'Filtering web results…' }[event.name] || `Running ${event.name}…`;
        break;
      case 'plot': segment = null; place(renderPlot(event)); break;
      case 'sources': {
        segment = null;
        const box = el('div', 'sources', event.kind === 'cited' ? 'Sources' : 'Web pages consulted');
        const list = el('ol');
        for (const source of event.sources) {
          const a = el('a', '', source.title);
          a.href = source.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
          const li = el('li'); li.append(a); list.append(li);
        }
        box.append(list); place(box);
        break;
      }
      case 'table': segment = null; place(renderTable(event)); break;
      case 'notice': status.textContent = event.message; break;
      case 'download_request':
        segment = null;
        place(downloadRequestCard(event));
        status.textContent = 'Waiting for your answer…';
        break;
      case 'download_started':
        refreshJobs();
        // falls through
      case 'download_progress':
      case 'download_finished':
      case 'download_declined': {
        const card = reply.querySelector(`.download-card[data-request="${CSS.escape(event.id)}"]`);
        if (card) updateDownloadCard(card, event);
        status.textContent = event.type === 'download_progress' || event.type === 'download_started' ? 'Downloading…' : 'Thinking…';
        break;
      }
      case 'error': segment = null; place(el('div', 'error', `⚠️ ${event.message}`)); break;
    }
  }

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation, message: text, model: currentModel && currentModel.ref, web_search: webSearch.checked }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${response.status}`);
    }
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const line = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (line.startsWith('data: ')) handle(JSON.parse(line.slice(6)));
      }
    }
  } catch (err) {
    handle({ type: 'error', message: err.message });
  } finally {
    status.remove();
  }
}

form.addEventListener('submit', async e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || send.disabled) return;
  input.value = '';
  send.disabled = true;
  try { await ask(text); } finally { send.disabled = false; input.focus(); }
});

// Used by other views (e.g. the explorer's "Ask the chat" button).
function askAbout(text) {
  location.hash = '#chat';
  input.value = text;
  input.focus();
  input.setSelectionRange(text.length, text.length);
}
