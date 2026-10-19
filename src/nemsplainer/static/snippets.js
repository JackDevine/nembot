// Snippets: the "Add to snippets" button on chat results, and the Snippets page.

const snippetsState = { list: null, current: null };
const snippetList = document.getElementById('snippet-list');
const snippetView = document.getElementById('snippet-view');
const snippetSearch = document.getElementById('snippet-search');

async function sendJSON(url, method, body) {
  const response = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function nameEditor(snippet, onSaved) {
  const field = el('input', 'snippet-name');
  field.value = snippet.name;
  field.title = 'Edit the name; changes save automatically';
  field.onchange = async () => {
    try {
      const saved = await sendJSON(`/api/snippets/${snippet.id}`, 'PATCH', { name: field.value });
      field.value = saved.name;
      snippetsState.list = null;  // refresh the Snippets page next time
      if (onSaved) onSaved(saved);
    } catch (err) { alert(err.message); }
  };
  return field;
}

// Button shown next to chat tables and plots.
function snippetButton(payload) {
  const box = el('span', 'snippet-action');
  const button = el('button', 'small', '☆ Add to snippets');
  button.title = 'Save this SQL (and plot) to the snippets schema; it re-runs on the latest data whenever you open it';
  button.onclick = async () => {
    button.disabled = true;
    button.textContent = 'Naming…';
    try {
      const snippet = await sendJSON('/api/snippets', 'POST',
        { ...payload, conversation, model: currentModel && currentModel.ref });
      const link = el('a', '', 'open'); link.href = `#snippets/${snippet.id}`;
      box.replaceChildren(el('span', 'muted', '★ Saved as '), nameEditor(snippet), link);
      snippetsState.list = null;
    } catch (err) {
      button.disabled = false;
      button.textContent = '☆ Add to snippets';
      alert(`Could not save the snippet: ${err.message}`);
    }
  };
  box.append(button);
  return box;
}

function drawSnippetList() {
  const query = snippetSearch.value.trim().toLowerCase();
  snippetList.replaceChildren();
  const shown = snippetsState.list.filter(s => !query || s.name.toLowerCase().includes(query) || s.sql.toLowerCase().includes(query));
  if (!shown.length) {
    snippetList.append(el('p', 'empty', snippetsState.list.length ? 'No matching snippets.' : 'No snippets yet. Use “☆ Add to snippets” under a table or plot in the chat.'));
    return;
  }
  for (const s of shown) {
    const link = el('a', 'snippet-link' + (String(s.id) === String(snippetsState.current) ? ' current' : ''));
    link.href = `#snippets/${s.id}`;
    link.append(el('span', '', `${s.plot_spec ? '📈' : '▦'} ${s.name}`), el('span', 'rows', formatDate(s.created_at)));
    snippetList.append(link);
  }
}

snippetSearch.oninput = () => snippetsState.list && drawSnippetList();

async function drawSnippet(id) {
  snippetView.replaceChildren(el('p', 'empty', 'Re-running snippet…'));
  let data;
  try {
    data = await getJSON(`/api/snippets/${id}/run`);
  } catch (err) {
    snippetView.replaceChildren(el('div', 'error', `⚠️ ${err.message}`));
    return;
  }
  if (String(snippetsState.current) !== String(id)) return;
  const s = data.snippet;
  const title = el('div', 'title-row');
  const remove = el('button', '', 'Delete');
  remove.onclick = async () => {
    if (!confirm(`Delete “${s.name}”?`)) return;
    await fetch(`/api/snippets/${s.id}`, { method: 'DELETE' });
    snippetsState.list = null;
    location.hash = '#snippets';
  };
  const ask = el('button', '', 'Ask the chat about this');
  ask.onclick = () => askAbout(`About my snippet “${s.name}”:\n\n${s.sql}\n\n`);
  title.append(nameEditor(s, saved => { const item = snippetsState.list.find(x => x.id === saved.id); if (item) item.name = saved.name; drawSnippetList(); }),
    el('span', 'spacer'), ask, remove);
  const meta = el('div', 'facts');
  const fact = (label, value) => { const f = el('span', '', `${label}: `); f.append(el('b', '', value)); meta.append(f); };
  fact('Saved', s.created_at);
  if (s.model) fact('Model', s.model);
  if (data.ran_at) fact('Re-ran', `${data.ran_at} (live data)`);
  snippetView.replaceChildren(title, meta);
  if (s.question) snippetView.append(el('p', 'muted', `From the question: “${s.question}”`));
  if (data.error) {
    const box = el('div', 'job-panel failed');
    box.append(el('b', '', 'This snippet no longer runs'), el('pre', 'job-error', data.error),
      el('p', '', 'A table it uses may not be downloaded, or the data model may have changed.'));
    snippetView.append(box);
    if (!data.events.length) snippetView.append(sqlBlock('SQL', s.sql));
  }
  for (const event of data.events) snippetView.append(event.type === 'plot' ? renderPlot(event) : renderTable(event));
}

async function showSnippets(id) {
  try {
    if (!snippetsState.list) snippetsState.list = (await getJSON('/api/snippets')).snippets;
    snippetsState.current = id;
    drawSnippetList();
    if (id) await drawSnippet(id);
    else snippetView.replaceChildren(el('p', 'empty', snippetsState.list.length ? 'Choose a snippet on the left.' : 'No snippets yet.'));
  } catch (err) {
    snippetView.replaceChildren(el('div', 'error', `⚠️ ${err.message}`));
  }
}
