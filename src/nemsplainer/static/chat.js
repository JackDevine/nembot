// Chat view: model picker, web search toggle, streaming replies.

const log = document.getElementById('log');
const form = document.getElementById('form');
const input = document.getElementById('input');
const send = document.getElementById('send');
let conversation = crypto.randomUUID();
let chatMode = 'analyst';  // 'creator' builds plugins; a conversation keeps one mode

const CREATOR_INTRO = 'Creator mode builds plugins: new chat tools, tabs and buttons for nemsplainer. ' +
  'Describe what you want. The assistant writes the plugin as a draft, tests it in a separate process, ' +
  'and asks before installing it. Plugins run on your computer, so review what it shows you.';

function drawModeSwitch() {
  document.querySelectorAll('.mode-switch button').forEach(b => b.classList.toggle('active', b.dataset.mode === chatMode));
  document.getElementById('chat-view').classList.toggle('creator', chatMode === 'creator');
  input.placeholder = chatMode === 'creator'
    ? 'Describe a feature to add, e.g. "a tab showing negative-price hours by region"…'
    : 'Ask about NEM prices, demand or generation…';
}

// Start a fresh conversation in ``mode`` (switching modes always starts a new chat).
function startChat(mode) {
  chatMode = mode;
  conversation = crypto.randomUUID();
  log.replaceChildren();
  if (mode === 'creator') log.append(el('div', 'creator-banner', CREATOR_INTRO));
  drawModeSwitch();
  location.hash = '#chat';
  input.focus();
}

document.querySelectorAll('.mode-switch button').forEach(button => {
  button.onclick = () => {
    if (button.dataset.mode === chatMode) return;
    if (log.querySelector('.turn') && !confirm('Switching modes starts a new chat. Continue?')) return;
    startChat(button.dataset.mode);
  };
});

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

document.getElementById('reset').onclick = () => startChat(chatMode);
drawModeSwitch();

// Actions next to a chat table or plot: snippets plus any plugin buttons.
function resultActions(event, text) {
  const box = el('span', 'result-actions');
  box.append(snippetButton({ sql: event.sql, plot_spec: event.plot_spec || null, question: text }),
    ...pluginResultActions(event));
  return box;
}
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
  const recent = el('input'); recent.type = 'checkbox'; recent.checked = true;
  const recentBox = el('label', 'toggle');
  if (event.recent && plan.live && plan.live.available) {
    recentBox.append(recent, ` Also fetch AEMO's recent reports after the monthly archive`
      + (plan.live.through ? ` (to ${plan.live.through.slice(0, 16)}, about ${formatBytes(plan.live.size)})` : ''));
  }
  const approve = el('button', 'primary', 'Download'), decline = el('button', '', 'Not now');
  const buttons = el('div', 'buttons'); buttons.append(decline, approve);
  const status = el('div', 'download-status');
  const decide = async ok => {
    approve.disabled = decline.disabled = from.disabled = to.disabled = recent.disabled = true;
    const response = await fetch('/api/chat/decision', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation, request: event.id, approve: ok, start: from.value, end: to.value, recent: recent.checked }),
    });
    if (!response.ok) status.textContent = (await response.json()).error;
  };
  approve.onclick = () => decide(true);
  decline.onclick = () => decide(false);
  card.append(head, el('p', 'reason', event.reason), el('p', 'muted', event.comment), months, estimate, recentBox, buttons, status);
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
    status.replaceChildren(el('span', 'ok', `✓ Downloaded ${job.table} (${jobRange(job)}).`));
  } else if (job.status === 'cancelled') {
    status.replaceChildren(el('span', 'muted', 'Download cancelled.'));
  } else {
    const box = el('div', 'job-panel failed');
    box.append(el('b', '', 'Download failed'), el('pre', 'job-error', job.error));
    status.replaceChildren(box);
  }
}

// Approval card for installing a plugin that Creator mode built.
function pluginInstallCard(event) {
  const card = el('div', 'download-card install-card');
  card.dataset.request = event.id;
  card.append(el('div', 'download-card-head', `${event.replacing ? 'Update' : 'Install'} plugin “${event.plugin}”?`),
    el('p', 'reason', event.summary));
  const files = el('ul', 'install-files');
  for (const f of event.files) files.append(el('li', 'mono', `${f.path} (${f.bytes.toLocaleString()} bytes)`));
  card.append(files);
  const test = event.test || {};
  card.append(el('p', test.ok ? 'ok' : 'error', test.ok ? '✓ Loads cleanly in a test process.' : `✗ The last test found problems: ${test.load_error || test.client_js_error || test.error || 'see above'}`));
  card.append(el('p', 'muted', 'Plugins run on your computer with nemsplainer’s permissions. Expand the 📝 entries above to review the code.'));
  const install = el('button', 'primary', event.replacing ? 'Update' : 'Install'), skip = el('button', '', 'Not now');
  const decide = async ok => {
    install.disabled = skip.disabled = true;
    await fetch('/api/chat/decision', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation, request: event.id, approve: ok }) });
  };
  install.onclick = () => decide(true);
  skip.onclick = () => decide(false);
  const buttons = el('div', 'buttons'); buttons.append(skip, install);
  card.append(buttons, el('div', 'install-status'));
  return card;
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
          create_report: 'Building the report (re-running its queries)…',
          inspect_runtime: 'Inspecting the app…', list_plugin_files: 'Reading the plugin draft…',
          read_plugin_file: 'Reading a plugin file…', write_plugin_file: 'Writing a plugin file…',
          delete_plugin_file: 'Deleting a plugin file…', test_plugin: 'Testing the plugin in a separate process…',
          install_plugin: 'Preparing to install…',
          web_search: 'Searching the web…', web_fetch: 'Reading a web page…', code_execution: 'Filtering web results…' }[event.name] || `Running ${event.name}…`;
        break;
      case 'plot': segment = null; place(renderPlot(event, resultActions(event, text))); break;
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
      case 'table': segment = null; place(renderTable(event, event.plugin ? null : resultActions(event, text))); break;
      case 'plugin_event':
        segment = null;
        if (!renderPluginEvent(event, place)) place(el('pre', 'muted', `${event.plugin} · ${event.kind}: ${JSON.stringify(event.data)}`));
        break;
      case 'plugin_file': {
        segment = null;
        const lines = event.content.split('\n').length;
        place(details(`📝 Wrote ${event.plugin}/${event.path} (${lines} line${lines === 1 ? '' : 's'})`, el('pre', '', event.content)));
        break;
      }
      case 'plugin_test': {
        segment = null;
        const r = event.report;
        const ok = r.ok;
        const summary = ok ? `✓ Test passed for ${event.plugin}` : `✗ Test found problems in ${event.plugin}`;
        const body = el('pre', '', JSON.stringify(r, null, 2));
        const box = details(summary, body);
        box.classList.add(ok ? 'test-ok' : 'test-failed');
        place(box);
        break;
      }
      case 'plugin_install_request': place(pluginInstallCard(event)); status.textContent = 'Waiting for your answer…'; segment = null; break;
      case 'plugin_install_result': {
        segment = null;
        const card = reply.querySelector(`.install-card[data-request="${CSS.escape(event.id)}"]`);
        const target = card ? card.querySelector('.install-status') : null;
        const message = event.status === 'loaded' ? el('span', 'ok', `✓ Installed ${event.plugin}. `)
          : event.status === 'declined' ? el('span', 'muted', 'Not installed; the draft is kept.')
          : el('pre', 'job-error', event.error || event.status);
        const nodes = [message];
        if (event.status === 'loaded') {
          const reload = el('button', 'small', 'Reload the page to load its UI');
          reload.onclick = () => location.reload();
          nodes.push(reload);
        }
        if (target) { card.querySelector('.buttons')?.remove(); target.replaceChildren(...nodes); }
        else nodes.forEach(place);
        break;
      }
      case 'notice': status.textContent = event.message; break;
      case 'report': {
        segment = null;
        const card = el('div', 'report-card');
        card.append(el('span', 'report-icon', '📄'), el('b', '', event.title),
          el('span', 'muted', ` ${FORMAT_LABELS[event.format] || event.format} · ${formatBytes(event.bytes)}`), reportLinks(event));
        place(card);
        break;
      }
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
      body: JSON.stringify({ conversation, message: text, model: currentModel && currentModel.ref, web_search: webSearch.checked, mode: chatMode }),
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
