// The Chats column: saved chats, newest first. nembot names each chat from its first message;
// rename with ✎ or a double-click (your name is kept), delete with 🗑. Opening a saved chat
// replays its UI events through renderTurn (chat.js); a chat whose reply is still streaming
// keeps its live pane, and the list marks chats that are replying or waiting for an answer.

const chatList = document.getElementById('chat-list');
const chatSearch = document.getElementById('chat-search');
let savedChats = [];

async function refreshChatList() {
  try { savedChats = (await getJSON('/api/chats')).chats; } catch { return; }
  drawChatList();
}

function drawChatList() {
  const query = chatSearch.value.trim().toLowerCase();
  // A new chat's first reply is saved when it finishes; list it meanwhile under its first message.
  const unsaved = [...panes.values()].filter(p => p.busy && !savedChats.some(c => c.id === p.id))
    .map(p => ({ id: p.id, title: null, placeholder: p.firstText, mode: p.mode }));
  const shown = [...unsaved, ...savedChats].filter(c => !query || (c.title || c.placeholder || '').toLowerCase().includes(query));
  chatList.replaceChildren(...shown.map(chatRow));
  if (!shown.length) chatList.append(el('p', 'empty', savedChats.length ? 'No matching chats.' : 'Your chats will appear here.'));
}

function chatRow(chat) {
  const row = el('a', 'chat-link' + (chat.id === conversation ? ' current' : ''));
  row.href = `#chat/${chat.id}`;
  row.title = chat.title || 'Untitled chat';
  const name = el('span', 'chat-name', chat.title || chat.placeholder || 'Naming…');
  if (chat.mode === 'creator') name.prepend(el('span', 'chat-mode', 'Creator'));
  const pane = panes.get(chat.id);
  const state = pane && pane.waiting ? el('span', 'chat-waiting', 'needs you') : pane && pane.busy ? el('span', 'chat-busy') : null;
  if (pane && pane.waiting) row.title = 'nembot is waiting for your answer in this chat';
  else if (pane && pane.busy) row.title = 'nembot is replying…';
  const rename = el('button', 'chat-action', '✎');
  rename.title = 'Rename';
  rename.onclick = e => { e.preventDefault(); e.stopPropagation(); editChatName(row, chat); };
  const remove = el('button', 'chat-action', '🗑');
  remove.title = 'Delete';
  remove.onclick = async e => {
    e.preventDefault(); e.stopPropagation();
    if (!confirm(`Delete “${chat.title || 'this chat'}”? This can't be undone.`)) return;
    const response = await fetch(`/api/chats/${encodeURIComponent(chat.id)}`, { method: 'DELETE' });
    if (!response.ok) return alert((await response.json().catch(() => ({}))).error || 'Delete failed');
    savedChats = savedChats.filter(c => c.id !== chat.id);
    if (chat.id === conversation) startChat(chatMode);
    else drawChatList();
  };
  row.ondblclick = e => { e.preventDefault(); editChatName(row, chat); };
  row.append(name, ...(state ? [state] : []), rename, remove);
  return row;
}

function editChatName(row, chat) {
  const field = el('input', 'chat-rename');
  field.value = chat.title || '';
  const done = async save => {
    field.onblur = null;
    const title = field.value.trim();
    if (save && title && title !== chat.title) {
      try {
        const saved = await sendJSON(`/api/chats/${encodeURIComponent(chat.id)}`, 'PATCH', { title });
        chat.title = saved.title;
      } catch (err) { alert(err.message); }
    }
    drawChatList();
  };
  field.onkeydown = e => {
    if (e.key === 'Enter') { e.preventDefault(); done(true); }
    if (e.key === 'Escape') { e.preventDefault(); done(false); }
  };
  field.onblur = () => done(true);
  field.onclick = e => { e.preventDefault(); e.stopPropagation(); };
  row.replaceChildren(field);
  field.focus();
  field.select();
}

function renameChatInList(id, title) {
  const chat = savedChats.find(c => c.id === id);
  if (chat) { chat.title = title; drawChatList(); }
}

// #chat/<id>: reopen a saved chat (or stay on the current one).
async function openChat(id) {
  if (id === conversation) return drawChatList();
  if (panes.has(id)) { showPane(id); return input.focus(); }  // still replying: its live pane
  let chat;
  try { chat = await getJSON(`/api/chats/${encodeURIComponent(id)}`); } catch (err) {
    startChat(chatMode);
    currentPane().el.append(el('div', 'error', `Couldn't open that chat: ${err.message}`));
    return;
  }
  if (panes.has(id)) return showPane(id);  // opened meanwhile
  // The chat's web search setting keeps its tools the same for the model.
  const pane = createPane(chat.id, chat.mode, chat.web_search);
  for (const turn of chat.transcript) {
    const { handle, finish } = renderTurn(turn.text, pane, { replay: true });
    for (const event of turn.events) handle(event);
    finish();
  }
  showPane(chat.id);
  input.focus();
}

function showChat(id) {
  if (id) openChat(id);
  else if (currentPane()?.el.querySelector('.turn')) history.replaceState(null, '', `#chat/${conversation}`);
}

chatSearch.oninput = drawChatList;
document.getElementById('new-chat').onclick = () => startChat(chatMode);
refreshChatList();
