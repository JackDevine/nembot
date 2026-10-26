// Chat view: model picker, web search toggle, streaming replies.

const log = document.getElementById('log');
const form = document.getElementById('form');
const input = document.getElementById('input');
const send = document.getElementById('send');
const stopButton = document.getElementById('stop');
const chatStats = document.getElementById('chat-stats');
let conversation = null;    // the chat on screen
let chatMode = 'analyst';  // 'creator' builds plugins; a conversation keeps one mode
// Each chat shown this session has its own pane in the log, so a reply keeps streaming into
// its chat while you look at another. Panes of finished chats are dropped once hidden (the
// saved chat is replayed if reopened). id -> {id, el, mode, webSearch, busy, waiting, firstText}
const panes = new Map();

const CREATOR_INTRO = 'Creator mode builds plugins: new chat tools, tabs and buttons for nembot. ' +
  'Describe what you want. The assistant writes the plugin as a draft, tests it in a separate process, ' +
  'and asks before installing it. Plugins run on your computer, so review what it shows you.';

function drawModeSwitch() {
  document.querySelectorAll('.mode-switch button').forEach(b => b.classList.toggle('active', b.dataset.mode === chatMode));
  document.getElementById('chat-view').classList.toggle('creator', chatMode === 'creator');
  input.placeholder = chatMode === 'creator'
    ? 'Describe a feature to add, e.g. "a tab showing negative-price hours by region"…'
    : 'Ask about NEM prices, demand or generation…';
  drawCompareChips();
}

function createPane(id, mode, webSearchOn) {
  const pane = { id, el: el('div', 'chat-pane'), mode, webSearch: webSearchOn, busy: false, waiting: false, firstText: null,
    stats: { requests: 0, input: 0, cacheRead: 0, cacheWrite: 0, output: 0, searches: 0, cost: 0, unpriced: false, plan: 0, genSeconds: 0, genOutput: 0 } };
  pane.el.hidden = true;
  if (mode === 'creator') pane.el.append(el('div', 'creator-banner', CREATOR_INTRO));
  log.append(pane.el);
  panes.set(id, pane);
  return pane;
}

const currentPane = () => panes.get(conversation);

// Put a chat's pane on screen, with its mode, web search setting and Send state.
function showPane(id) {
  const pane = panes.get(id);
  const previous = currentPane();
  if (previous && previous !== pane) previous.webSearch = webSearch.checked;
  conversation = id;
  chatMode = pane.mode;
  webSearch.checked = pane.webSearch;
  for (const other of [...panes.values()]) {
    other.el.hidden = other !== pane;
    if (other !== pane && !other.busy) { other.el.remove(); panes.delete(other.id); }
  }
  drawSend(pane);
  drawChatStats();
  drawModeSwitch();
  window.drawChatList?.();
  log.scrollTop = log.scrollHeight;
}

// Start a fresh conversation in ``mode`` (switching modes always starts a new chat).
function startChat(mode) {
  const id = crypto.randomUUID();
  createPane(id, mode, webSearch.checked);
  showPane(id);
  history.replaceState(null, '', '#chat');
  input.focus();
}

document.querySelectorAll('.mode-switch button').forEach(button => {
  button.onclick = () => {
    if (button.dataset.mode === chatMode) return;
    if (currentPane()?.el.querySelector('.turn') && !confirm('Switching modes starts a new chat. Continue?')) return;
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
// Extra models that answer alongside currentModel, shown side by side (a compare turn).
let compareModels = [];
const MAX_COMPARE = 4;
const compareChips = document.getElementById('compare-chips');

// Where a model runs: the Anthropic API, the user's Claude plan (through Claude Code), or OpenRouter.
// Replaced by the server's list (/api/models) once models load.
let PROVIDERS = [['subscription', 'Claude plan', 'Claude (your Claude plan, through Claude Code)'],
  ['claude', 'Claude', 'Claude (Anthropic API)'], ['openrouter', 'OpenRouter', 'OpenRouter']];
function providerLabel(provider) { return (PROVIDERS.find(p => p[0] === provider) || [, provider])[1]; }

function modelBadges(m) {
  const meta = el('span', 'meta');
  meta.append(m.vision ? el('span', 'badge image', 'text + image') : el('span', 'badge textonly', 'text only'));
  if (!m.tools) meta.append(el('span', 'badge notools', 'no tools'));
  if (m.provider === 'subscription') meta.append(el('span', 'badge', 'uses your plan'));
  if (m.price_in !== null && m.price_out !== null) meta.append(el('span', 'badge', `$${m.price_in}/$${m.price_out} per M`));
  return meta;
}

function setModel(m) {
  currentModel = m;
  localStorage.setItem('model', m.ref);
  modelButton.textContent = `${providerLabel(m.provider)} · ${m.name}`;
  modelButton.title = `${m.ref} — ${m.vision ? 'text + image input' : 'text-only input: cannot see plots'}`;
  setCompareModels(compareModels.filter(c => c.ref !== m.ref));
}

function setCompareModels(list) {
  compareModels = list;
  localStorage.setItem('compareModels', JSON.stringify(list.map(m => m.ref)));
  drawCompareChips();
}

// "vs <model> ×" chips beside the model button. Creator mode always uses one model.
function drawCompareChips() {
  compareChips.replaceChildren();
  compareChips.hidden = chatMode === 'creator' || !compareModels.length;
  for (const m of compareModels) {
    const chip = el('span', 'compare-chip', `vs ${m.name}`);
    chip.title = `${m.ref} also answers each message; the replies appear side by side`;
    const remove = el('button', '', '×');
    remove.type = 'button';
    remove.title = 'Stop comparing with this model';
    remove.onclick = () => setCompareModels(compareModels.filter(c => c !== m));
    chip.append(remove);
    compareChips.append(chip);
  }
}

// The models that answer the next message: the chat's model first.
function turnModels(pane) {
  return [currentModel, ...(pane.mode === 'creator' ? [] : compareModels)].filter(Boolean).map(m => m.ref);
}

function drawModels() {
  const query = modelSearch.value.trim().toLowerCase();
  modelList.replaceChildren();
  for (const [provider, , title] of PROVIDERS) {
    const group = models.filter(m => m.provider === provider &&
      (!query || m.name.toLowerCase().includes(query) || m.id.toLowerCase().includes(query)));
    if (!group.length) continue;
    modelList.append(el('h3', '', `${title} · ${group.length}`));
    for (const m of group) {
      const usable = m.tools && m.available;
      const compared = compareModels.some(c => c.ref === m.ref);
      const row = el('div', 'model' + (usable ? '' : ' disabled') + (currentModel && m.ref === currentModel.ref ? ' current' : '')
        + (compared ? ' compared' : ''));
      row.title = !m.available ? 'This service is not connected. Connect it on the Settings page.'
        : !m.tools ? 'This model does not support tool calling, so it cannot query the database.' : m.id;
      row.append(el('span', '', m.name), modelBadges(m));
      if (usable) {
        row.onclick = () => { setModel(m); picker.classList.remove('open'); input.focus(); };
        if (chatMode !== 'creator' && m.ref !== currentModel?.ref) {
          const add = el('button', 'compare-add', compared ? '✓ comparing' : '+ compare');
          add.type = 'button';
          add.title = compared ? 'Stop comparing with this model' : 'Also ask this model, and show the answers side by side';
          add.onclick = e => {
            e.stopPropagation();
            if (compared) setCompareModels(compareModels.filter(c => c.ref !== m.ref));
            else if (compareModels.length >= MAX_COMPARE - 1) return alert(`Compare at most ${MAX_COMPARE} models at once.`);
            else setCompareModels([...compareModels, m]);
            drawModels();
          };
          row.append(add);
        }
      }
      modelList.append(row);
    }
  }
}

async function loadModels() {
  const response = await fetch('/api/models');
  const body = await response.json();
  models = body.models;
  if (body.providers) PROVIDERS = body.providers.map(p => [p.name, p.label, p.title]);
  body.errors.forEach(message => console.warn(message));
  const saved = models.find(m => m.ref === localStorage.getItem('model') && m.tools && m.available);
  const fallback = models.find(m => m.ref === body.default) || { ref: body.default, provider: 'claude', name: 'Claude Opus 5.5', vision: true };
  const compared = JSON.parse(localStorage.getItem('compareModels') || '[]');  // read before setModel saves the list
  setModel(saved || fallback);
  setCompareModels(compared.map(ref => models.find(m => m.ref === ref && m.tools && m.available && m.ref !== currentModel.ref))
    .filter(Boolean).slice(0, MAX_COMPARE - 1));
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

// A blank chat to start with (without touching the address, which routing reads next).
showPane(createPane(crypto.randomUUID(), chatMode, webSearch.checked).id);

// Actions next to a chat table or plot: snippets plus any plugin buttons.
function resultActions(event, text) {
  const box = el('span', 'result-actions');
  const payload = { sql: event.sql, plot_spec: event.plot_spec || null, question: text };
  box.append(snippetButton(payload), docButton(payload), ...pluginResultActions(event, text));
  return box;
}
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});

// The message box grows with the message, and Send is dimmed while it's empty.
function fitInput() {
  form.classList.toggle('empty', !input.value.trim());
  input.style.height = 'auto';
  input.style.height = `${input.scrollHeight}px`;
}
input.addEventListener('input', fitInput);
fitInput();

// The popup for a request_download tool call: approve (optionally changing months) or decline.
function downloadRequestCard(event, chatId) {
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
      body: JSON.stringify({ conversation: chatId, request: event.id, approve: ok, start: from.value, end: to.value, recent: recent.checked }),
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
function pluginInstallCard(event, chatId) {
  const card = el('div', 'download-card install-card');
  card.dataset.request = event.id;
  card.append(el('div', 'download-card-head', `${event.replacing ? 'Update' : 'Install'} plugin “${event.plugin}”?`),
    el('p', 'reason', event.summary));
  const files = el('ul', 'install-files');
  for (const f of event.files) files.append(el('li', 'mono', `${f.path} (${f.bytes.toLocaleString()} bytes)`));
  card.append(files);
  const test = event.test || {};
  card.append(el('p', test.ok ? 'ok' : 'error', test.ok ? '✓ Loads cleanly in a test process.' : `✗ The last test found problems: ${test.load_error || test.client_js_error || test.error || 'see above'}`));
  card.append(el('p', 'muted', 'Plugins run on your computer with nembot’s permissions. Expand the 📝 entries above to review the code.'));
  const install = el('button', 'primary', event.replacing ? 'Update' : 'Install'), skip = el('button', '', 'Not now');
  const decide = async ok => {
    install.disabled = skip.disabled = true;
    await fetch('/api/chat/decision', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation: chatId, request: event.id, approve: ok }) });
  };
  install.onclick = () => decide(true);
  skip.onclick = () => decide(false);
  const buttons = el('div', 'buttons'); buttons.append(skip, install);
  card.append(buttons, el('div', 'install-status'));
  return card;
}

// The logo (static/logo.svg) as a throbber: the bolts pop in left to right, the robot hops
// and its eyes brighten with each one, then a shine sweeps across (option H in
// design/throbber-options.html). It's inline so CSS can animate its parts. The shine is
// clipped to copies of the shapes (a clip-path, not a mask, and a band no taller than the
// robot): with a mask, GPU rendering in Chromium drew faint lines along the masked area's
// top and bottom edges as the shine moved.

const LOGO_BOLT = 'M3,-16 L-7,2 L-1,2 L-4,16 L8,-4 L2,-4 L8,-16 Z';
let throbberCount = 0;

function throbber() {
  const id = `nb-shine-${++throbberCount}`;
  // The clip stays well inside the head's outline (only a pixel wide at 28px) and uses slightly
  // smaller bolts (shrink < 1), so the shine never lights the edges.
  const placed = [['left', 28, 34, -32, 1.05], ['middle', 60, 26, 0, 1.25], ['right', 92, 34, 32, 1.05]];
  const bolts = fill => placed
    .map(([k, x, y, r, s]) => `<g class="nb-bolt nb-bolt-${k}"><path d="${LOGO_BOLT}" transform="translate(${x} ${y}) rotate(${r}) scale(${s})" fill="${fill}"/></g>`)
    .join('');
  // A clip-path can't hold groups, so its bolts are plain paths with the placement built into
  // their points (and the pop-in class on the path itself).
  const clipBolts = shrink => placed.map(([k, x, y, r, s]) => {
    const a = r * Math.PI / 180, k2 = s * shrink;
    const d = LOGO_BOLT.replace(/(-?[\d.]+),(-?[\d.]+)/g, (_, px, py) =>
      `${(x + k2 * (px * Math.cos(a) - py * Math.sin(a))).toFixed(2)},${(y + k2 * (px * Math.sin(a) + py * Math.cos(a))).toFixed(2)}`);
    return `<path class="nb-bolt nb-bolt-${k}" d="${d}"/>`;
  }).join('');
  const wrap = el('span', 'throbber');
  wrap.innerHTML = `<svg viewBox="0 0 120 120" aria-hidden="true">
    <defs>
      <linearGradient id="${id}-head" gradientUnits="userSpaceOnUse" x1="0" y1="52" x2="0" y2="108"><stop offset="0" stop-color="#64748b"/><stop offset="1" stop-color="#1e293b"/></linearGradient>
      <linearGradient id="${id}-band"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".7"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
      <clipPath id="${id}-clip">
        <rect x="28" y="58" width="64" height="44" rx="10" fill="#fff"/>${clipBolts(0.88)}
      </clipPath>
    </defs>
    <g class="nb-body">
      <rect x="13" y="70" width="12" height="20" rx="4" fill="#334155"/>
      <rect x="95" y="70" width="12" height="20" rx="4" fill="#334155"/>
      <rect x="22" y="52" width="76" height="56" rx="16" fill="url(#${id}-head)" stroke="#475569" stroke-width="2"/>
      <circle cx="44" cy="78" r="8" fill="#38bdf8"/><circle cx="76" cy="78" r="8" fill="#38bdf8"/>
      <circle cx="46.5" cy="75.5" r="2.5" fill="#fff"/><circle cx="78.5" cy="75.5" r="2.5" fill="#fff"/>
      <g class="nb-eye-dim" fill="url(#${id}-head)"><circle cx="44" cy="78" r="8.5"/><circle cx="76" cy="78" r="8.5"/></g>
      <rect x="46" y="94" width="28" height="5" rx="2.5" fill="#38bdf8" opacity=".6"/>
      ${bolts('#f59e0b')}
      <g clip-path="url(#${id}-clip)"><g class="nb-shine"><rect x="-20" y="4" width="40" height="108" transform="skewX(-20)" fill="url(#${id}-band)"/></g></g>
    </g>
  </svg>`;
  return wrap;
}

// ---- The stats line under the chat: cost, tokens and speed for the whole chat ----
// Each model request reports a 'usage' event (tokens, cost, generation seconds); saved chats
// replay theirs, so the totals survive reopening a chat.
function addUsage(stats, u) {
  stats.requests += 1;
  stats.input += u.input || 0; stats.cacheRead += u.cache_read || 0; stats.cacheWrite += u.cache_write || 0;
  stats.output += u.output || 0; stats.searches += u.searches || 0;
  if (u.plan) stats.plan += 1;  // covered by the Claude plan: no per-token cost
  else if (u.cost === null || u.cost === undefined) stats.unpriced = true; else stats.cost += u.cost;
  if (u.seconds > 0) { stats.genSeconds += u.seconds; stats.genOutput += u.output || 0; }
}

function tokenCount(n) { return Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n); }
function formatCost(c) { return `$${c < 0.01 ? c.toFixed(4) : c < 1 ? c.toFixed(3) : c.toFixed(2)}`; }

function drawChatStats() {
  const s = currentPane()?.stats;
  if (!s || !s.requests) { chatStats.hidden = true; return; }
  const input = s.input + s.cacheRead + s.cacheWrite;
  const speed = s.genSeconds > 0 ? Math.round(s.genOutput / s.genSeconds) : null;
  chatStats.textContent = `This chat: ${s.unpriced ? '≥ ' : ''}${formatCost(s.cost)} · ${tokenCount(input)} tokens in · `
    + `${tokenCount(s.output)} out${speed === null ? '' : ` · ${speed} tokens/s`}`;
  chatStats.title = [
    `Input: ${s.input.toLocaleString()} new, ${s.cacheRead.toLocaleString()} read from cache, ${s.cacheWrite.toLocaleString()} written to cache`,
    `Output: ${s.output.toLocaleString()} tokens (including thinking)`,
    `Model requests: ${s.requests}` + (s.searches ? `; web searches: ${s.searches}` : ''),
    speed === null ? '' : 'Speed: output tokens per second while generating, averaged over the chat',
    s.unpriced ? 'Some replies came from models without price data, so the cost is a lower bound.' : '',
    s.plan ? `${s.plan === s.requests ? 'All' : s.plan} model requests ran on your Claude plan: they count toward its usage limits and aren't in the cost.` : '',
  ].filter(Boolean).join('\n');
  chatStats.hidden = false;
}

// One chat turn in ``pane``: the user's text, then the reply as its UI events arrive. With
// ``replay`` (redrawing a saved chat) nothing opens, downloads or waits for an answer.
// A compare turn ('compare' event) shows each model's reply in its own column; their events
// carry a ``branch`` index, and 'kept' says which reply the chat continues from.
function renderTurn(text, pane, { replay = false } = {}) {
  pane.el.querySelectorAll('.keep-branch').forEach(b => b.remove());  // earlier choices are final now
  const turn = el('div', 'turn');
  const reply = el('div', 'assistant');
  turn.append(el('div', 'user', text), reply);
  pane.el.append(turn);
  const main = replyRenderer(reply, pane, text, replay);
  let columns = null;

  function handle(event) {
    if (event.type === 'compare') { main.status.hidden = true; columns = compareColumns(turn, reply, pane, text, replay, event.models); }
    else if (event.type === 'kept') columns?.keep(event.branch);
    else if (columns && Number.isInteger(event.branch)) columns.renderers[event.branch].handle(event);
    else main.handle(event);
  }
  function finish() { main.finish(); columns?.renderers.forEach(r => r.finish()); }
  return { handle, finish };
}

function compareColumns(turn, reply, pane, text, replay, models) {
  const grid = el('div', 'compare-grid');
  grid.style.setProperty('--columns', models.length);
  turn.classList.add('compare-turn');
  turn.insertBefore(grid, reply);
  const cols = models.map((m, index) => {
    const col = el('div', 'compare-col');
    const head = el('div', 'compare-head');
    const keep = el('button', 'small keep-branch', 'Continue from this answer');
    keep.hidden = true;
    const stop = el('button', 'small stop-branch', 'Stop');
    stop.title = `Stop ${m.name}'s reply (the other models carry on)`;
    stop.hidden = replay;
    stop.onclick = async () => {
      stop.disabled = true;
      try { await sendJSON('/api/chat/stop', 'POST', { conversation: pane.id, branch: index }); }
      catch (err) { stop.disabled = false; alert(err.message); }
    };
    keep.title = 'Your next message follows on from this reply (and its query results)';
    keep.onclick = async () => {
      keep.disabled = true;
      try {
        await sendJSON('/api/chat/keep', 'POST', { conversation: pane.id, branch: index });
        mark(index);
      } catch (err) { alert(err.message); }
      keep.disabled = false;
    };
    head.append(el('b', '', `${providerLabel(m.provider)} · ${m.name}`), el('span', 'kept-label', '✓ continuing from this'), stop, keep);
    const body = el('div', 'assistant');
    col.append(head, body);
    grid.append(col);
    const renderer = replyRenderer(body, pane, text, replay);
    const handle = renderer.handle;
    renderer.handle = event => {  // the model finished or was stopped: no more Stop for it
      if (event.type === 'branch_done' || event.type === 'stopped') stop.hidden = true;
      handle(event);
    };
    return { col, keep, renderer };
  });
  const mark = kept => cols.forEach((c, i) => {
    c.col.classList.toggle('kept', i === kept);
    c.keep.hidden = i === kept || kept === null || kept === undefined;
  });
  return { renderers: cols.map(c => c.renderer), keep: mark };
}

// Draws one model's reply into ``reply`` as its events arrive.
function replyRenderer(reply, pane, text, replay) {
  const shown = () => conversation === pane.id;
  const waiting = value => { if (!replay && pane.waiting !== value) { pane.waiting = value; window.drawChatList?.(); } };
  const status = el('div', 'status');
  const label = el('span', '', 'Thinking…');
  status.append(throbber(), label);
  reply.append(status);
  let segment = null, markdown = '';
  const scroll = () => { if (shown()) log.scrollTop = log.scrollHeight; };
  const place = node => { status.parentNode === reply ? reply.insertBefore(node, status) : reply.append(node); scroll(); };
  // Reasoning streams into an open dropdown, which folds itself once the model moves on.
  function endSegment() {
    if (segment?.reasoning) {
      const seconds = segment.started ? Math.round((performance.now() - segment.started) / 1000) : null;
      segment.summary.textContent = seconds ? `Reasoned for ${seconds} s` : 'Reasoning';
      segment.open = false;
      segment.classList.remove('streaming');
    }
    segment = null;
  }

  function handle(event) {
    switch (event.type) {
      case 'block': endSegment(); break;
      case 'text':
        if (!segment || segment.className !== 'text') { endSegment(); segment = el('div', 'text'); markdown = ''; place(segment); }
        markdown += event.delta;
        segment.innerHTML = renderMarkdown(markdown);
        scroll();
        break;
      case 'progress':
        if (!segment?.reasoning) {
          endSegment();
          segment = el('details', 'reasoning streaming');
          segment.reasoning = true;
          segment.open = true;
          segment.started = replay ? null : performance.now();
          segment.summary = el('summary', '', 'Thinking…');
          segment.body = el('div', 'progress');
          segment.append(segment.summary, segment.body);
          place(segment);
        }
        segment.body.textContent += event.delta;
        segment.body.scrollTop = segment.body.scrollHeight;
        scroll();
        break;
      case 'model':
        reply.prepend(el('div', 'via', `${providerLabel(event.model.provider)} · ${event.model.name}`));
        break;
      case 'tool':
      case 'tool_running':
        endSegment();
        label.textContent = { visualize_mms: 'Building plot…', query_mms: 'Running query…',
          describe_table: 'Reading table docs…', request_download: 'Preparing a download request…',
          create_report: 'Building the report (re-running its queries)…',
          create_document: 'Writing the document…',
          inspect_runtime: 'Inspecting the app…', list_plugin_files: 'Reading the plugin draft…',
          read_plugin_file: 'Reading a plugin file…', write_plugin_file: 'Writing a plugin file…',
          delete_plugin_file: 'Deleting a plugin file…', test_plugin: 'Testing the plugin in a separate process…',
          install_plugin: 'Preparing to install…',
          web_search: 'Searching the web…', web_fetch: 'Reading a web page…', code_execution: 'Filtering web results…' }[event.name] || `Running ${event.name}…`;
        break;
      case 'plot': endSegment(); place(renderPlot(event, resultActions(event, text))); break;
      case 'sources': {
        endSegment();
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
      case 'table': endSegment(); place(renderTable(event, event.plugin ? null : resultActions(event, text))); break;
      case 'plugin_event':
        endSegment();
        if (!renderPluginEvent(event, place)) place(el('pre', 'muted', `${event.plugin} · ${event.kind}: ${JSON.stringify(event.data)}`));
        break;
      case 'plugin_file': {
        endSegment();
        const lines = event.content.split('\n').length;
        place(details(`📝 Wrote ${event.plugin}/${event.path} (${lines} line${lines === 1 ? '' : 's'})`, el('pre', '', event.content)));
        break;
      }
      case 'plugin_test': {
        endSegment();
        const r = event.report;
        const ok = r.ok;
        const summary = ok ? `✓ Test passed for ${event.plugin}` : `✗ Test found problems in ${event.plugin}`;
        const body = el('pre', '', JSON.stringify(r, null, 2));
        const box = details(summary, body);
        box.classList.add(ok ? 'test-ok' : 'test-failed');
        place(box);
        break;
      }
      case 'plugin_install_request':
        place(pluginInstallCard(event, pane.id)); label.textContent = 'Waiting for your answer…'; endSegment(); waiting(true);
        break;
      case 'plugin_install_result': {
        waiting(false);
        endSegment();
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
      case 'notice': label.textContent = event.message; break;
      case 'report': {
        endSegment();
        const card = el('div', 'report-card');
        card.append(el('span', 'report-icon', '📄'), el('b', '', event.title),
          el('span', 'muted', ` ${FORMAT_LABELS[event.format] || event.format} · ${formatBytes(event.bytes)}`), reportLinks(event));
        place(card);
        break;
      }
      case 'document': {
        endSegment();
        const card = el('div', 'report-card');
        const open = el('a', '', 'Open beside chat'); open.href = '#chat';
        open.onclick = e => { e.preventDefault(); openDocBeside(event.id); };
        const full = el('a', '', 'Full page ↗'); full.href = `#docs/${event.id}`;
        const links = el('span', 'report-links'); links.append(open, full);
        card.append(el('span', 'report-icon', '📝'), el('b', '', event.title), el('span', 'muted', ' shared document'), links);
        place(card);
        if (!replay && shown()) openDocBeside(event.id);
        break;
      }
      case 'download_request':
        endSegment();
        place(downloadRequestCard(event, pane.id));
        label.textContent = 'Waiting for your answer…';
        waiting(true);
        break;
      case 'download_started':
        if (!replay) refreshJobs();
        // falls through
      case 'download_progress':
      case 'download_finished':
      case 'download_declined': {
        waiting(false);
        const card = reply.querySelector(`.download-card[data-request="${CSS.escape(event.id)}"]`);
        if (card) updateDownloadCard(card, event);
        label.textContent = event.type === 'download_progress' || event.type === 'download_started' ? 'Downloading…' : 'Thinking…';
        break;
      }
      case 'error': endSegment(); place(el('div', 'error', `⚠️ ${event.message}`)); break;
      case 'stopped':  // the user pressed Stop: what was written stays, approval cards can't be answered
        endSegment();
        status.remove();
        reply.querySelectorAll('.download-card .buttons button, .install-card .buttons button').forEach(b => { b.disabled = true; });
        place(el('div', 'stopped-note', '■ Stopped'));
        waiting(false);
        break;
      case 'branch_done': status.remove(); break;  // one compared model finished; the others may still run
      case 'chat_title': renameChatInList(event.id, event.title); break;
      case 'usage': addUsage(pane.stats, event); if (shown()) drawChatStats(); break;
    }
  }

  function finish() {
    endSegment();
    status.remove();
    // Approval cards from a saved chat can't be answered any more.
    if (replay) reply.querySelectorAll('.download-card .buttons button').forEach(b => { b.disabled = true; });
  }
  return { handle, finish, status };
}

async function ask(text) {
  const pane = currentPane();
  pane.busy = true;
  pane.webSearch = webSearch.checked;
  pane.firstText ||= text;
  drawSend(pane);
  const { handle, finish } = renderTurn(text, pane);
  history.replaceState(null, '', `#chat/${pane.id}`);  // the chat has an address from its first message
  window.drawChatList?.();
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation: pane.id, message: text, models: turnModels(pane),
        web_search: pane.webSearch, mode: pane.mode }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${response.status}`);
    }
    await streamEvents(response, handle);
  } catch (err) {
    handle({ type: 'error', message: err.message });
  } finally {
    finish();
    pane.busy = pane.waiting = false;
    if (conversation === pane.id) drawSend(pane);
    else { pane.el.remove(); panes.delete(pane.id); }  // reopening replays the saved chat
    refreshChatList();
  }
}

// While a reply runs, Send becomes Stop: it stops every model answering (each compared model
// also has its own Stop in its column).
function drawSend(pane) {
  send.hidden = pane.busy;
  stopButton.hidden = !pane.busy;
  stopButton.disabled = false;
}

stopButton.onclick = async () => {
  const pane = currentPane();
  if (!pane?.busy) return;
  stopButton.disabled = true;
  try { await sendJSON('/api/chat/stop', 'POST', { conversation: pane.id }); }
  catch (err) { stopButton.disabled = false; alert(err.message); }
};

form.addEventListener('submit', async e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || currentPane().busy) return;
  input.value = '';
  fitInput();
  await ask(text);
  if (currentPane()?.id && !currentPane().busy) input.focus();
});

// Used by other views (e.g. the explorer's "Ask the chat" button).
function askAbout(text) {
  location.hash = '#chat';
  input.value = text;
  fitInput();
  input.focus();
  input.setSelectionRange(text.length, text.length);
}
