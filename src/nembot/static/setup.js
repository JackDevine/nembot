// Settings and first-run onboarding (#setup): connect AI services, then download some data.
// Opens by itself when no connected model can call tools. Keys are sent once, to be tested
// and saved by the server; they never come back to the page.

const setupPage = document.getElementById('setup-page');
const FIRST_DATA = [['DISPATCHPRICE', 'Prices'], ['DISPATCHREGIONSUM', 'Demand and generation']];
const SOURCE_LABELS = {
  environment: 'Key from an environment variable', file: 'Key saved in the config file', keychain: 'Key saved in the keychain',
  'ant profile': 'Signed in with `ant auth login`', setup: 'Added on this page', setting: 'Turned on in the config file',
};

async function showSetup(rest) {
  setupPage.replaceChildren(el('p', 'empty', 'Checking connections…'));
  let status;
  try { status = await getJSON('/api/setup'); }
  catch (err) { setupPage.replaceChildren(el('div', 'error', `⚠️ ${err.message}`)); return; }
  drawSetup(status, rest || '');
}

function drawSetup(status, rest) {
  const first = !status.usable;
  const page = [];
  page.push(el('h2', '', first ? 'Welcome to nembot' : 'Settings'));
  page.push(el('p', 'setup-lead', first
    ? 'nembot answers questions about Australia’s electricity market by querying AEMO data with an AI model. ' +
      'Connect one AI service to start. The model needs to support tool calling, which this page tests for you.'
    : 'The AI services nembot can use. Models from every connected service appear in the chat’s model picker.'));
  const [kind, ...detail] = rest.split('/');
  if (kind === 'connected') page.push(el('div', 'setup-banner ok', `✓ Connected to ${cardLabel(status, detail[0])}.`));
  if (kind === 'error') page.push(el('div', 'setup-banner error', `⚠️ ${decodeURIComponent(detail.join('/'))}`));

  page.push(el('h3', 'setup-step', '1. Connect an AI service'));
  const grid = el('div', 'setup-cards');
  for (const card of orderCards(status.cards)) grid.append(setupCard(card, status));
  page.push(grid);

  page.push(el('h3', 'setup-step', '2. Download some data'));
  page.push(firstDataStep(status));

  page.push(el('h3', 'setup-step', 'Where keys are kept'));
  page.push(keyStorage(status));

  page.push(el('h3', 'setup-step', 'Running code'));
  page.push(codeSafety(status));

  if (status.usable) {
    const go = el('a', 'setup-start', 'Start chatting →');
    go.href = '#chat';
    page.push(go);
  }
  setupPage.replaceChildren(...page);
}

// Connected services first, then the recommended start (OpenRouter), then what's running here,
// then the rest; local servers that aren't running go last.
function orderCards(cards) {
  const rank = c => c.connected ? 0 : c.name === 'openrouter' ? 1 : c.detected ? 2 : c.local ? 4 : 3;
  return [...cards].sort((a, b) => rank(a) - rank(b));
}

function cardLabel(status, name) { return status.cards.find(c => c.name === name)?.label || name; }

function setupCard(card, status) {
  const box = el('section', 'setup-card' + (card.connected ? ' connected' : ''));
  const head = el('div', 'setup-card-head');
  head.append(el('b', '', card.label));
  if (card.connected) head.append(el('span', 'badge ok', '✓ connected'));
  else if (card.name === 'openrouter' && !status.usable) head.append(el('span', 'badge accent', 'recommended'));
  else if (card.detected) head.append(el('span', 'badge', 'found on this computer'));
  if (!card.tested) {
    const badge = el('span', 'badge', 'community tested');
    badge.title = 'Not tested by the nembot developers. The connection test below checks it works for you.';
    head.append(badge);
  }
  box.append(head, el('p', 'setup-note', card.note));
  if (card.connected && card.source) box.append(el('p', 'setup-source', SOURCE_LABELS[card.source] || card.source));
  const result = el('div', 'setup-result');

  if (card.name === 'subscription') {
    box.append(result);
    return box;  // turned on and off in the config file only
  }
  const form = el('div', 'setup-form');
  let keyInput = null, addressInput = null;
  if (card.local) {
    addressInput = el('input');
    addressInput.value = card.base_url;
    addressInput.title = 'The OpenAI-compatible address of the server';
    addressInput.spellcheck = false;
    form.append(addressInput);
    if (card.detected === false && !card.connected) form.append(el('span', 'setup-hint', 'Not running at this address.'));
  } else if (!card.connected || card.source === 'file' || card.source === 'keychain') {
    if (card.name === 'openrouter' && !card.connected) form.append(openRouterSignIn(result), el('span', 'setup-hint', 'or paste a key:'));
    keyInput = el('input');
    keyInput.type = 'password';
    keyInput.autocomplete = 'off';
    keyInput.placeholder = card.connected ? 'Paste a new key to replace it' : 'Paste an API key';
    form.append(keyInput);
  }
  const actions = el('div', 'setup-actions');
  const connect = el('button', !card.connected && card.local && card.detected ? 'primary' : '',
    card.connected ? 'Test' : card.local ? 'Connect' : 'Connect with key');
  connect.type = 'button';
  connect.onclick = async () => {
    const key = keyInput?.value.trim();
    if (!card.connected && card.needs_key && !key) { keyInput.focus(); return; }
    connect.disabled = true;
    result.replaceChildren(el('div', 'status', 'Testing: listing models, then one small tool call…'));
    try {
      const outcome = await sendJSON('/api/setup/connect', 'POST',
        { name: card.name, key: key || null, base_url: addressInput?.value.trim() || null });
      showResult(result, outcome);
      if (outcome.saved) {
        if (keyInput) keyInput.value = '';
        window.loadModels?.().catch(() => {});
        setTimeout(() => showSetup(''), outcome.ok ? 1200 : 4000);
      }
    } catch (err) {
      showResult(result, { steps: [{ name: 'Connect', ok: false, detail: err.message }] });
    } finally { connect.disabled = false; }
  };
  actions.append(connect);
  if (card.key_url && !card.connected) {
    const link = el('a', 'setup-link', 'Get a key ↗');
    link.href = card.key_url; link.target = '_blank'; link.rel = 'noopener';
    actions.append(link);
  }
  if (card.connected && ['file', 'keychain', 'setup'].includes(card.source)) {
    const forget = el('button', 'link-like', 'Disconnect');
    forget.type = 'button';
    forget.onclick = async () => {
      if (!confirm(`Disconnect ${card.label}? Its saved key is deleted.`)) return;
      await sendJSON('/api/setup/forget', 'POST', { name: card.name });
      window.loadModels?.().catch(() => {});
      showSetup('');
    };
    actions.append(forget);
  } else if (card.connected && card.source === 'environment') {
    actions.append(el('span', 'setup-hint', `Set by ${card.env}; unset it to disconnect.`));
  }
  if (keyInput || addressInput) box.append(form);
  box.append(actions, result);
  return box;
}

function openRouterSignIn(result) {
  const signIn = el('button', 'primary setup-signin', 'Sign in with OpenRouter');
  signIn.type = 'button';
  signIn.title = 'Approve nembot on OpenRouter’s site; it creates a key for you, no copying needed';
  signIn.onclick = async () => {
    signIn.disabled = true;
    try { location.href = (await sendJSON('/api/setup/openrouter/start', 'POST', {})).url; }
    catch (err) { signIn.disabled = false; showResult(result, { steps: [{ name: 'Sign in', ok: false, detail: err.message }] }); }
  };
  return signIn;
}

function showResult(box, outcome) {
  const list = el('ul', 'setup-steps');
  for (const step of outcome.steps || []) {
    const item = el('li', step.ok ? 'ok' : 'fail');
    item.append(el('b', '', `${step.ok ? '✓' : '✗'} ${step.name}: `), document.createTextNode(step.detail));
    list.append(item);
  }
  if (outcome.saved) list.append(el('li', outcome.ok ? 'ok' : 'warn',
    outcome.ok ? 'Saved. Its models are now in the chat’s model picker.'
      : 'Saved, but the test model could not call tools. Try another model from this service in the chat.'));
  box.replaceChildren(list);
}

function firstDataStep(status) {
  const box = el('section', 'setup-card setup-data');
  if (status.has_data) {
    box.append(el('p', '', '✓ You already have data. Download more from the Explorer, or let the chat ask for what it needs.'));
    return box;
  }
  box.append(el('p', 'setup-note', 'nembot works on a local copy of AEMO’s data. Start with the latest month of '
    + 'five-minute prices and demand for every region; the chat can ask to download more when a question needs it.'));
  const row = el('div', 'setup-actions');
  const button = el('button', 'primary', 'Download the latest month');
  button.type = 'button';
  const info = el('span', 'setup-hint', '');
  row.append(button, info);
  const panels = el('div');
  box.append(row, panels);
  Promise.all(FIRST_DATA.map(([table]) => downloadPlan(table))).then(plans => {
    const bytes = plans.reduce((sum, p) => sum + (p.month_size || 0), 0);
    info.textContent = `${plans[0].latest_month}${bytes ? `, about ${formatBytes(bytes)} to download` : ''}`;
    button.onclick = async () => {
      button.disabled = true;
      try {
        for (const [i, [table]] of FIRST_DATA.entries()) {
          await startDownload(table, plans[i].latest_month, plans[i].latest_month, 'setup');
          panels.append(jobPanel(table));
        }
      } catch (err) { button.disabled = false; info.textContent = `⚠️ ${err.message}`; }
    };
  }).catch(err => { info.textContent = `⚠️ ${err.message}`; button.disabled = true; });
  return box;
}

function keyStorage(status) {
  const box = el('section', 'setup-card');
  box.append(el('p', 'setup-note', `Keys you add here are saved in ${status.config}, which only your user account can read. `
    + 'Environment variables (such as OPENROUTER_API_KEY) take priority over saved keys.'));
  const label = el('label', 'toggle');
  const box2 = el('input'); box2.type = 'checkbox'; box2.checked = status.keyring; box2.disabled = !status.keyring_available;
  label.append(box2, document.createTextNode(' Keep keys in the system keychain instead'));
  label.title = status.keyring_available ? 'Moves saved keys into your operating system’s keychain'
    : 'Needs the optional keyring package: uv sync --extra keyring';
  box2.onchange = async () => {
    try { await sendJSON('/api/setup/settings', 'POST', { keyring: box2.checked }); }
    catch (err) { box2.checked = !box2.checked; alert(err.message); }
  };
  box.append(label);
  if (!status.keyring_available) box.append(el('p', 'setup-hint', 'To use the keychain, install the optional package: uv sync --extra keyring'));
  return box;
}

function codeSafety(status) {
  const box = el('section', 'setup-card');
  box.append(el('p', 'setup-note', 'Assistants can write marimo and Jupyter notebooks. nembot runs each one once to check it '
    + 'works, which runs the assistant’s Python code on this computer. Python code can read, change or delete your files '
    + 'and use the network.'));
  const label = el('label', 'toggle');
  const box2 = el('input'); box2.type = 'checkbox'; box2.checked = status.confirm_code;
  label.append(box2, document.createTextNode(' Ask me before an assistant runs Python code'));
  box2.onchange = async () => {
    try { await sendJSON('/api/setup/settings', 'POST', { confirm_code: box2.checked }); }
    catch (err) { box2.checked = !box2.checked; alert(err.message); }
  };
  box.append(label);
  return box;
}

// First run: with no usable model, open the onboarding page instead of an empty chat.
(async () => {
  try {
    const status = await getJSON('/api/setup');
    const hash = location.hash.slice(1);
    if (!status.usable && (!hash || hash === 'chat')) location.hash = '#setup';
  } catch { /* the chat page shows its own errors */ }
})();
