// Plugin runtime in the page: the `nembot` API each plugin's client.js receives,
// the loader for installed plugins, plugin tabs, and the Plugins management page.

const pluginRegistry = { tabs: [], chatEvents: {}, resultActions: [], plugins: [] };
const pluginView = document.getElementById('plugin-view');
const pluginsPage = document.getElementById('plugins-page');

window.nembot = {
  // Called by the server-side wrapper around each plugin's client.js.
  definePlugin(name, body) {
    const base = `/plugins/${encodeURIComponent(name)}`;
    const api = {
      name,
      el,
      getJSON: path => getJSON(base + path),
      fetch: (path, options) => fetch(base + path, options),
      renderTable: table => renderTable({ sql: table.sql || '', columns: table.columns, rows: table.rows,
        total: table.total ?? table.rows.length }),
      renderPlot: plot => renderPlot({ sql: plot.sql || '', spec: plot.spec, plot_spec: plot.spec }),
      renderMarkdown: text => { const div = el('div'); div.innerHTML = renderMarkdown(text); return div; },
      askChat: text => askAbout(text),
      addTab(tab) {
        const route = `plugin/${name}/${tab.id}`;
        pluginRegistry.tabs.push({ ...tab, plugin: name, route });
        drawPluginNav();
        if (location.hash.slice(1) === route) window.dispatchEvent(new HashChangeEvent('hashchange'));
      },
      onChatEvent(kind, handler) { (pluginRegistry.chatEvents[name] ||= {})[kind] = handler; },
      addResultAction(factory) { pluginRegistry.resultActions.push({ plugin: name, factory }); },
    };
    try { body(api); } catch (err) { console.error(`plugin ${name} client.js failed`, err); }
  },
};

function drawPluginNav() {
  const nav = document.querySelector('nav');
  nav.querySelectorAll('a[data-plugin-tab]').forEach(a => a.remove());
  const before = nav.querySelector('a[data-view="plugins"]');
  for (const tab of pluginRegistry.tabs) {
    const link = el('a', '', tab.title);
    link.href = `#${tab.route}`;
    link.dataset.view = tab.route;
    link.dataset.pluginTab = tab.plugin;
    link.title = `From the ${tab.plugin} plugin`;
    nav.insertBefore(link, before);
  }
}

function showPluginTab(route) {
  const tab = pluginRegistry.tabs.find(t => t.route === route);
  pluginView.replaceChildren();
  if (!tab) { pluginView.append(el('p', 'empty', 'This plugin tab is not loaded (yet).')); return; }
  const container = el('div', 'plugin-tab');
  pluginView.append(container);
  try { tab.render(container); } catch (err) { container.append(el('div', 'error', `⚠️ ${tab.plugin}: ${err.message}`)); }
}

// Buttons plugins add next to chat tables and plots.
function pluginResultActions(result) {
  const out = [];
  for (const { plugin, factory } of pluginRegistry.resultActions) {
    try {
      const node = factory(result);
      if (node) out.push(node);
    } catch (err) { console.error(`plugin ${plugin} result action failed`, err); }
  }
  return out;
}

// Render a plugin's custom chat event; returns false when no plugin handles it.
function renderPluginEvent(event, place) {
  const handler = (pluginRegistry.chatEvents[event.plugin] || {})[event.kind];
  if (!handler) return false;
  try { handler(event, place); } catch (err) { place(el('div', 'error', `⚠️ ${event.plugin}: ${err.message}`)); }
  return true;
}

async function loadPlugins() {
  try {
    pluginRegistry.plugins = (await getJSON('/api/plugins')).plugins;
  } catch (err) { console.warn('plugins unavailable', err); return; }
  for (const plugin of pluginRegistry.plugins.filter(p => p.status === 'loaded')) {
    const base = `/plugins/${encodeURIComponent(plugin.name)}/_assets`;
    if (plugin.style) {
      const link = el('link'); link.rel = 'stylesheet'; link.href = `${base}/style.css`; document.head.append(link);
    }
    if (plugin.client) {
      const script = el('script'); script.src = `${base}/client.js`; script.async = false; document.body.append(script);
    }
  }
}

async function showPluginsPage() {
  pluginsPage.replaceChildren(el('h2', '', 'Plugins'));
  let data;
  try { data = await getJSON('/api/plugins'); } catch (err) { pluginsPage.append(el('div', 'error', err.message)); return; }
  const intro = el('p', 'muted', `Plugins add chat tools, tabs and buttons. They live in ${data.directory}. ` +
    'Each plugin is a folder with plugin.toml and optional plugin.py, client.js and style.css. ');
  const creatorButton = el('button', 'primary', '✨ Build or change a plugin (Creator mode)');
  creatorButton.onclick = () => startChat('creator');
  const reload = el('button', '', 'Reload plugins');
  reload.onclick = async () => { await fetch('/api/plugins/reload', { method: 'POST' }); location.reload(); };
  const bar = el('div', 'plugin-actions'); bar.append(creatorButton, reload);
  pluginsPage.append(intro, bar);
  if (!data.plugins.length) pluginsPage.append(el('p', 'empty', 'No plugins installed yet.'));
  for (const plugin of data.plugins) {
    const card = el('div', `plugin-card ${plugin.status}`);
    const head = el('div', 'plugin-head');
    const toggle = el('input'); toggle.type = 'checkbox'; toggle.checked = plugin.status !== 'disabled';
    toggle.title = 'Enable or disable';
    toggle.onchange = async () => {
      await fetch(`/api/plugins/${encodeURIComponent(plugin.name)}/enable`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: toggle.checked }) });
      location.reload();
    };
    head.append(toggle, el('b', '', plugin.title), el('span', 'muted', ` ${plugin.name} ${plugin.version}`),
      el('span', `badge ${plugin.status === 'loaded' ? 'ok' : plugin.status === 'error' ? 'notools' : 'missing'}`, plugin.status));
    card.append(head, el('p', '', plugin.description));
    const parts = [];
    if (plugin.tools.length) parts.push(`Chat tools: ${plugin.tools.map(t => t.name).join(', ')}`);
    if (plugin.routes.length) parts.push(`Routes: ${plugin.routes.join(', ')}`);
    if (plugin.client) parts.push('Adds UI (client.js)');
    if (parts.length) card.append(el('p', 'muted', parts.join(' · ')));
    if (plugin.error) card.append(el('pre', 'job-error', plugin.error));
    pluginsPage.append(card);
  }
  pluginsPage.append(el('p', 'muted acknowledgement',
    'The plugin system and Creator mode are modelled on DeepSeek Harness. Plugins run on your computer with ' +
    "nembot's permissions, so only install plugins you trust; Creator mode asks before installing."));
}

loadPlugins();
