// Shared documents: a Tiptap editor with table/chart snapshots and images, comment threads,
// and nembot's tracked suggestions. Tiptap (MIT) loads from esm.sh the first time a document
// opens. A document opens either on the Docs page (#docs/<id>) or beside the chat; only one
// editor is open per document so their saves never race.

const TIPTAP_VERSION = '3.32.0';
const docsState = { list: null, page: null, side: null };
const docList = document.getElementById('doc-list');
const docPage = document.getElementById('doc-page');
const docSearch = document.getElementById('doc-search');
const docPanel = document.getElementById('doc-panel');
let tiptap = null;

// Block types that can carry a whole-block suggestion (inserted by nembot, pending review).
const SUGGESTABLE = ['paragraph', 'heading', 'bulletList', 'orderedList', 'blockquote', 'codeBlock', 'horizontalRule', 'image', 'table'];

function loadTiptap() {
  tiptap ||= Promise.all([
    import(`https://esm.sh/@tiptap/core@${TIPTAP_VERSION}`),
    import(`https://esm.sh/@tiptap/starter-kit@${TIPTAP_VERSION}`),
    import(`https://esm.sh/@tiptap/extension-image@${TIPTAP_VERSION}`),
    import(`https://esm.sh/@tiptap/extension-table@${TIPTAP_VERSION}`),
    import(`https://esm.sh/@tiptap/pm@${TIPTAP_VERSION}/state`),
    import(`https://esm.sh/@tiptap/pm@${TIPTAP_VERSION}/view`),
  ]).then(([core, kit, image, table, pmState, pmView]) =>
    ({ core, extensions: buildExtensions(core, kit.default, image.default, table, pmState, pmView) }));
  return tiptap;
}

function buildExtensions({ Node, Mark, Extension }, StarterKit, Image, { Table, TableRow, TableHeader, TableCell },
  { Plugin }, { Decoration, DecorationSet }) {
  const marks = (name, cls, attr) => Mark.create({
    name, inclusive: false, ...(name === 'comment' && { excludes: '' }),  // comments may overlap
    addAttributes() {
      return { [attr]: { default: null, parseHTML: e => e.getAttribute(`data-${attr}`), renderHTML: a => ({ [`data-${attr}`]: a[attr] }) } };
    },
    parseHTML() { return [{ tag: `span.${cls}` }]; },
    renderHTML({ HTMLAttributes }) { return ['span', { ...HTMLAttributes, class: cls }, 0]; },
  });
  const Suggestions = Extension.create({
    name: 'blockSuggestions',
    addGlobalAttributes() {
      return [{ types: SUGGESTABLE, attributes: { suggestion: {
        default: null, parseHTML: e => e.getAttribute('data-suggestion'),
        renderHTML: a => (a.suggestion ? { 'data-suggestion': a.suggestion } : {}) } } }];
    },
  });
  // Outline whole blocks nembot suggested. A decoration works for every block type, including
  // tables, whose node view ignores the suggestion attribute.
  const SuggestedBlocks = Extension.create({
    name: 'suggestedBlocks',
    addProseMirrorPlugins() {
      return [new Plugin({ props: { decorations(state) {
        const decorations = [];
        state.doc.descendants((node, pos) => {
          if (!node.attrs.suggestion || node.type.name === 'dataBlock') return true;
          decorations.push(Decoration.node(pos, pos + node.nodeSize, { class: 'nb-suggested-block', 'data-suggestion': node.attrs.suggestion }));
          return false;
        });
        return DecorationSet.create(state.doc, decorations);
      } } })];
    },
  });
  const DataBlock = Node.create({
    name: 'dataBlock', group: 'block', atom: true, selectable: true,
    addAttributes() {
      const keys = ['kind', 'title', 'sql', 'plot_spec', 'columns', 'rows', 'total', 'taken_at', 'suggestion'];
      return Object.fromEntries(keys.map(k => [k, { default: null }]));
    },
    parseHTML() { return [{ tag: 'div[data-nb-block]', getAttrs: e => JSON.parse(e.getAttribute('data-nb-block') || '{}') }]; },
    renderHTML({ node }) { return ['div', { 'data-nb-block': JSON.stringify(node.attrs) }]; },
    addNodeView() { return props => dataBlockView(props); },
  });
  return [StarterKit.configure({ link: { openOnClick: false } }), Image, DataBlock, Suggestions, SuggestedBlocks,
    Table.configure({ resizable: false }), TableRow, TableHeader, TableCell,
    marks('comment', 'nb-comment', 'thread'), marks('suggestInsert', 'nb-ins', 'suggestion'),
    marks('suggestDelete', 'nb-del', 'suggestion')];
}

// ---- Table and chart blocks ----

function blockRecords(attrs) {
  return attrs.rows.map(row => Object.fromEntries(attrs.columns.map((c, i) => [c, row[i]])));
}

function dataBlockView({ node, getPos, editor }) {
  const dom = el('figure', 'nb-block');
  dom.contentEditable = 'false';
  let attrs = node.attrs;
  const setAttrs = next => {
    const pos = getPos();
    if (typeof pos === 'number') editor.view.dispatch(editor.state.tr.setNodeMarkup(pos, undefined, { ...attrs, ...next }));
  };
  const render = () => {
    dom.classList.toggle('nb-suggested', Boolean(attrs.suggestion));
    const head = el('figcaption', 'nb-block-head');
    const title = el('input', 'nb-block-title');
    title.value = attrs.title || '';
    title.placeholder = attrs.kind === 'chart' ? 'Chart title' : 'Table title';
    title.onchange = () => setAttrs({ title: title.value.trim() });
    const shown = attrs.total > attrs.rows.length ? `${attrs.rows.length.toLocaleString()} of ${attrs.total.toLocaleString()} rows` : `${attrs.total.toLocaleString()} rows`;
    const meta = el('span', 'muted', `${attrs.kind === 'chart' ? 'Chart' : 'Table'} · ${shown} · snapshot ${formatStamp(attrs.taken_at)}`);
    const refresh = el('button', 'small', 'Refresh');
    refresh.title = 'Re-run the SQL and replace this snapshot with the latest data';
    refresh.onclick = async () => {
      refresh.disabled = true; refresh.textContent = 'Refreshing…';
      try {
        const fresh = await sendJSON('/api/docs/block', 'POST', { sql: attrs.sql, plot_spec: attrs.plot_spec, title: attrs.title });
        setAttrs({ ...fresh, suggestion: attrs.suggestion });
      } catch (err) { alert(`Refresh failed: ${err.message}`); refresh.disabled = false; refresh.textContent = 'Refresh'; }
    };
    const remove = el('button', 'small', '✕');
    remove.title = 'Remove from the document';
    remove.onclick = () => {
      const pos = getPos();
      if (typeof pos === 'number') editor.view.dispatch(editor.state.tr.delete(pos, pos + editor.state.doc.nodeAt(pos).nodeSize));
    };
    head.append(title, meta, el('span', 'spacer'), refresh, remove);
    const body = el('div', 'nb-block-body');
    if (attrs.kind === 'chart') {
      // Single-view charts fill the block; composite ones keep the sizes in their spec.
      const spec = attrs.plot_spec, composite = ['hconcat', 'vconcat', 'concat', 'facet', 'repeat'].some(k => k in spec);
      body.classList.add('nb-chart');
      embedChart(body, { ...spec, ...(!composite && { width: 'container', autosize: { type: 'fit-x', contains: 'padding' } }),
        data: { values: blockRecords(attrs) } });
    }
    else body.append(createDataTable({ columns: attrs.columns.map(name => ({ name })), rows: attrs.rows,
      total: attrs.rows.length, exportName: (attrs.title || 'nembot-table').replace(/[^\w-]+/g, '-') }));
    dom.replaceChildren(head, body, sqlBlock('SQL', attrs.sql));
  };
  render();
  return {
    dom,
    update(next) {
      if (next.type !== node.type) return false;
      if (next.attrs !== attrs) { attrs = next.attrs; render(); }
      return true;
    },
    stopEvent: () => true,       // the table and inputs handle their own events
    ignoreMutation: () => true,
  };
}

function formatStamp(stamp) {
  if (!stamp) return '';
  const d = new Date(stamp.replace(' ', 'T'));
  return isNaN(d) ? stamp : d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

// A create_document seed: markdown (rendered like the chat) and table/chart snapshots.
function seedHtml(seed) {
  const attr = text => text.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  return (seed || []).map(part => part.block ? `<div data-nb-block="${attr(JSON.stringify(part.block))}"></div>`
    : renderMarkdown(part.markdown || '')).join('');
}

// ---- Reading the document for nembot ----

// Text of a textblock as the model sees it, with the doc position of each character.
// Text that is itself a pending insertion is skipped, so suggestions match the original wording.
function visibleText(node, pos) {
  let text = '';
  const map = [];
  node.forEach((child, offset) => {
    const at = pos + 1 + offset;
    if (child.isText) {
      if (child.marks.some(m => m.type.name === 'suggestInsert')) return;
      for (let i = 0; i < child.text.length; i++) { text += child.text[i]; map.push(at + i); }
    } else { text += ' '; map.push(at); }
  });
  return { text, map };
}

function readBlocks(doc) {
  const blocks = [];
  const push = (node, pos, kind, text, label) => blocks.push({ id: `B${blocks.length + 1}`, pos, kind, text, label });
  doc.descendants((node, pos, parent) => {
    const type = node.type.name;
    if (type === 'dataBlock') {
      push(node, pos, 'block', `[${node.attrs.kind}${node.attrs.title ? `: ${node.attrs.title}` : ''}] SQL: ${node.attrs.sql.replace(/\s+/g, ' ')}`);
      return false;
    }
    if (type === 'image') { push(node, pos, 'block', `[image${node.attrs.alt ? `: ${node.attrs.alt}` : ''}]`); return false; }
    if (!node.isTextblock) return true;
    const container = parent && parent.type.name;
    const label = type === 'heading' ? `heading ${node.attrs.level}` : type === 'codeBlock' ? 'code'
      : container === 'listItem' ? 'list item' : container === 'blockquote' ? 'quote'
      : container === 'tableHeader' ? 'table header cell' : container === 'tableCell' ? 'table cell' : '';
    push(node, pos, 'text', visibleText(node, pos).text, label);
    return false;
  });
  return blocks;
}

// ---- The editor ----

class DocEditor {
  constructor(container, docId, { beside = false } = {}) {
    this.container = container;
    this.id = docId;
    this.beside = beside;
    this.threads = [];
    this.showResolved = false;
    this.asking = new Map();       // thread id -> AbortController
    this.trackers = new Set();     // live block-position maps for in-flight asks
    this.saveTimer = null;
    this.dirty = false;
    this.touched = false;          // has the user put the cursor in the document?
    this.ready = this.open();
  }

  async open() {
    this.container.replaceChildren(el('p', 'empty', 'Opening the document…'));
    const [{ core, extensions }, doc] = await Promise.all([loadTiptap(), getJSON(`/api/docs/${this.id}`)]);
    if (this.destroyed) return;
    this.core = core;
    this.title = doc.title;
    this.threads = doc.threads || [];
    this.build(doc);
    this.editor = new core.Editor({
      element: this.editorHost, extensions,
      content: doc.content || seedHtml(doc.seed),
      editorProps: {
        handlePaste: (view, event) => this.dropFiles(event.clipboardData?.files),
        handleDrop: (view, event) => {
          const at = view.posAtCoords({ left: event.clientX, top: event.clientY });
          return this.dropFiles(event.dataTransfer?.files, at && at.pos);
        },
        handleClick: (view, pos, event) => {
          const link = event.target.closest && event.target.closest('a[href]');
          if (link && (event.ctrlKey || event.metaKey)) { window.open(link.href, '_blank', 'noopener'); return true; }
          const mark = view.state.doc.resolve(pos).marks().find(m => m.type.name === 'comment');
          if (mark) this.focusThread(mark.attrs.thread, false);
          return false;
        },
      },
    });
    this.editor.on('transaction', ({ transaction }) => {
      for (const tracker of this.trackers) for (const [id, pos] of tracker) tracker.set(id, transaction.mapping.map(pos, 1));
    });
    this.editor.on('update', () => { this.scheduleSave(); this.scheduleThreads(); });
    this.editor.on('selectionUpdate', () => this.updateBubble());
    this.editor.on('transaction', () => this.updateToolbar());
    this.editor.on('blur', () => setTimeout(() => this.updateBubble(), 150));
    this.editor.on('focus', () => { this.touched = true; this.updateBubble(); });
    this.renderThreads();
    this.updateToolbar();
    // A seeded document is stored as JSON straight away, but never replace a seed with an empty document.
    if (!doc.content && !this.editor.isEmpty) this.save();
  }

  build(doc) {
    const shell = el('div', 'doc-shell' + (this.beside ? ' beside' : ''));
    const head = el('div', 'doc-head');
    const title = el('input', 'doc-title');
    title.value = doc.title;
    title.oninput = () => { this.title = title.value; this.scheduleSave(); };
    this.status = el('span', 'muted doc-status', 'Saved');
    head.append(title, this.status);
    if (this.beside) {
      const full = el('a', 'small-link', 'Open full page ↗'); full.href = `#docs/${this.id}`;
      const close = el('button', 'small', '✕'); close.title = 'Close the document'; close.onclick = () => closeDocBeside();
      head.append(full, close);
    } else {
      const beside = el('button', 'small', 'Open beside chat');
      beside.onclick = () => { location.hash = '#chat'; openDocBeside(this.id); };
      const del = el('button', 'small', 'Delete');
      del.onclick = async () => {
        if (!confirm(`Delete “${this.title}”? This can't be undone.`)) return;
        await fetch(`/api/docs/${this.id}`, { method: 'DELETE' });
        docsState.list = null;
        this.destroy();
        location.hash = '#docs';
      };
      head.append(beside, del);
    }
    this.toolbar = this.buildToolbar();
    const body = el('div', 'doc-body');
    this.scroller = el('div', 'doc-scroll');
    this.editorHost = el('div', 'doc-editor');
    this.bubble = el('button', 'doc-bubble', '💬 Ask nembot');
    this.bubble.title = 'Comment on the selected text, or ask nembot about it';
    this.bubble.onmousedown = e => e.preventDefault();  // keep the selection
    this.bubble.onclick = () => this.startThread();
    this.scroller.append(this.editorHost, this.bubble);
    this.threadPane = el('aside', 'doc-threads');
    body.append(this.scroller, this.threadPane);
    shell.append(head, this.toolbar, body);
    this.container.replaceChildren(shell);
  }

  buildToolbar() {
    const bar = el('div', 'doc-toolbar');
    this.toolButtons = [];
    const add = (label, title, run, active) => {
      const b = el('button', 'tool', label);
      b.type = 'button'; b.title = title;
      b.onmousedown = e => e.preventDefault();
      b.onclick = () => run(this.editor.chain().focus()).run();
      this.toolButtons.push([b, active]);
      bar.append(b);
      return b;
    };
    add('B', 'Bold (Ctrl+B)', c => c.toggleBold(), e => e.isActive('bold'));
    add('I', 'Italic (Ctrl+I)', c => c.toggleItalic(), e => e.isActive('italic'));
    add('H2', 'Heading', c => c.toggleHeading({ level: 2 }), e => e.isActive('heading', { level: 2 }));
    add('H3', 'Subheading', c => c.toggleHeading({ level: 3 }), e => e.isActive('heading', { level: 3 }));
    add('• List', 'Bulleted list', c => c.toggleBulletList(), e => e.isActive('bulletList'));
    add('1. List', 'Numbered list', c => c.toggleOrderedList(), e => e.isActive('orderedList'));
    add('“ ”', 'Quote', c => c.toggleBlockquote(), e => e.isActive('blockquote'));
    add('▦ Table', 'Insert a table', c => c.insertTable({ rows: 3, cols: 3, withHeaderRow: true }));
    // Row and column controls, shown while the cursor is in a table.
    this.tableButtons = [['+ Row', 'Add a row below', c => c.addRowAfter()], ['+ Col', 'Add a column to the right', c => c.addColumnAfter()],
      ['− Row', 'Delete this row', c => c.deleteRow()], ['− Col', 'Delete this column', c => c.deleteColumn()],
      ['✕ Table', 'Delete the table', c => c.deleteTable()]].map(([label, title, run]) => {
      const button = add(label, title, run);
      button.classList.add('table-tool');
      return button;
    });
    add('↶', 'Undo (Ctrl+Z)', c => c.undo());
    add('↷', 'Redo (Ctrl+Shift+Z)', c => c.redo());
    const file = el('input'); file.type = 'file'; file.accept = 'image/png,image/jpeg,image/gif,image/webp'; file.hidden = true;
    file.onchange = () => { this.dropFiles(file.files); file.value = ''; };
    const image = el('button', 'tool', '🖼 Image');
    image.type = 'button'; image.title = 'Insert an image (you can also paste or drop one)';
    image.onclick = () => file.click();
    bar.append(image, file, el('span', 'muted doc-hint', 'Select text to comment or ask nembot. Ctrl+click a link to open it.'));
    return bar;
  }

  updateToolbar() {
    if (!this.editor) return;
    for (const [button, active] of this.toolButtons) button.classList.toggle('active', Boolean(active && active(this.editor)));
    const inTable = this.editor.isActive('table');
    for (const button of this.tableButtons) button.hidden = !inTable;
  }

  updateBubble() {
    const { state, view } = this.editor;
    const { from, to, empty } = state.selection;
    const show = !empty && view.hasFocus() && state.doc.textBetween(from, to).trim() && !state.selection.node;
    this.bubble.style.display = show ? 'block' : 'none';
    if (!show) return;
    const end = view.coordsAtPos(to), box = this.scroller.getBoundingClientRect();
    this.bubble.style.top = `${end.bottom - box.top + this.scroller.scrollTop + 6}px`;
    this.bubble.style.left = `${Math.max(8, Math.min(end.left - box.left, box.width - 140))}px`;
  }

  // ---- Saving ----

  scheduleSave() {
    clearTimeout(this.saveTimer);
    this.dirty = true;
    this.status.textContent = 'Editing…';
    this.saveTimer = setTimeout(() => this.save(), 700);
  }

  async save() {
    clearTimeout(this.saveTimer);
    if (!this.editor) return;
    this.dirty = false;
    this.status.textContent = 'Saving…';
    try {
      const saved = await sendJSON(`/api/docs/${this.id}`, 'PUT', { title: this.title, content: this.editor.getJSON(), threads: this.threads });
      this.status.textContent = 'Saved';
      const entry = docsState.list && docsState.list.find(d => d.id === this.id);
      if (entry) {
        Object.assign(entry, { title: saved.title, updated_at: saved.updated_at });
        docsState.list.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
        drawDocList();
      } else docsState.list = null;
    } catch (err) { this.dirty = true; this.status.textContent = `Not saved: ${err.message}`; }
  }

  destroy() {
    this.destroyed = true;
    if (this.dirty && this.editor) this.save();
    for (const controller of this.asking.values()) controller.abort();
    this.editor?.destroy();
    this.container.replaceChildren();
  }

  // ---- Images ----

  dropFiles(files, pos) {
    const images = [...(files || [])].filter(f => f.type.startsWith('image/'));
    if (!images.length) return false;
    (async () => {
      for (const file of images) {
        try {
          const response = await fetch(`/api/docs/${this.id}/images`, { method: 'POST', headers: { 'Content-Type': file.type }, body: file });
          const body = await response.json();
          if (!response.ok) throw new Error(body.error);
          const chain = this.editor.chain().focus();
          (typeof pos === 'number' ? chain.insertContentAt(pos, { type: 'image', attrs: { src: body.url, alt: file.name } })
            : chain.setImage({ src: body.url, alt: file.name })).run();
        } catch (err) { alert(`Image upload failed: ${err.message}`); }
      }
    })();
    return true;
  }

  // ---- Tables and charts from the chat ----

  insertBlock(attrs) {
    const { state } = this.editor;
    const sel = state.selection;
    // After the block holding the cursor once the user has worked in the document, otherwise at the end.
    const pos = this.touched ? (sel.$to.depth ? sel.$to.after(1) : sel.to) : state.doc.content.size;
    this.editor.chain().insertContentAt(pos, { type: 'dataBlock', attrs }).run();
    this.scroller.querySelectorAll('.nb-block')[this.blockIndexAt(pos)]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  blockIndexAt(pos) {
    let index = -1, found = -1;
    this.editor.state.doc.descendants((node, at) => {
      if (node.type.name === 'dataBlock') { index++; if (at >= pos && found < 0) found = index; }
    });
    return found;
  }

  // ---- Comment threads ----

  markRanges(name, attr, value) {
    const ranges = [];
    this.editor.state.doc.descendants((node, pos) => {
      if (node.isText && node.marks.some(m => m.type.name === name && m.attrs[attr] === value)) ranges.push([pos, pos + node.nodeSize]);
    });
    return ranges;
  }

  threadPosition(thread) {
    const ranges = this.markRanges('comment', 'thread', thread.id);
    return ranges.length ? ranges[0][0] : Infinity;
  }

  startThread() {
    const { from, to } = this.editor.state.selection;
    const thread = { id: crypto.randomUUID().slice(0, 12), quote: this.editor.state.doc.textBetween(from, to, ' '),
      created_at: new Date().toISOString(), resolved: false, messages: [], suggestions: [], draft: true };
    this.editor.chain().setMark('comment', { thread: thread.id }).setTextSelection(to).run();
    this.threads.push(thread);
    this.renderThreads();
    this.focusThread(thread.id, true);
  }

  focusThread(id, edit) {
    const card = this.threadPane.querySelector(`[data-thread="${CSS.escape(id)}"]`);
    this.threadPane.querySelectorAll('.doc-thread.focused').forEach(c => c.classList.remove('focused'));
    this.editorHost.querySelectorAll('.nb-comment.focused').forEach(c => c.classList.remove('focused'));
    this.editorHost.querySelectorAll(`.nb-comment[data-thread="${CSS.escape(id)}"]`).forEach(c => c.classList.add('focused'));
    if (!card) return;
    card.classList.add('focused');
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    if (edit) card.querySelector('textarea')?.focus();
  }

  scheduleThreads() {
    clearTimeout(this.threadTimer);
    this.threadTimer = setTimeout(() => this.renderThreads(), 300);
  }

  renderThreads() {
    if (!this.editor) return;
    const open = this.threads.filter(t => !t.resolved);
    const resolved = this.threads.length - open.length;
    const head = el('div', 'doc-threads-head');
    head.append(el('b', '', open.length ? `Comments (${open.length})` : 'Comments'));
    if (resolved) {
      const toggle = el('button', 'link-like', this.showResolved ? 'Hide resolved' : `Show resolved (${resolved})`);
      toggle.onclick = () => { this.showResolved = !this.showResolved; this.renderThreads(); };
      head.append(toggle);
    }
    const focused = this.threadPane.querySelector('.doc-thread.focused')?.dataset.thread;
    const typing = [...this.threadPane.querySelectorAll('textarea')].map(t => [t.closest('.doc-thread')?.dataset.thread, t.value, t === document.activeElement]);
    const shown = (this.showResolved ? this.threads : open).slice().sort((a, b) => this.threadPosition(a) - this.threadPosition(b));
    this.threadPane.replaceChildren(head, ...shown.map(t => this.threadCard(t)));
    if (!shown.length) this.threadPane.append(el('p', 'muted', 'Select some text in the document to comment on it or ask nembot.'));
    // Keep half-typed replies and focus across redraws.
    for (const [id, value, active] of typing) {
      const box = this.threadPane.querySelector(`[data-thread="${CSS.escape(id || '')}"] textarea`);
      if (box) { box.value = value; if (active) box.focus(); }
    }
    if (focused) this.threadPane.querySelector(`[data-thread="${CSS.escape(focused)}"]`)?.classList.add('focused');
  }

  threadCard(thread) {
    const card = el('div', 'doc-thread' + (thread.resolved ? ' resolved' : ''));
    card.dataset.thread = thread.id;
    card.onclick = e => {
      if (e.target.closest('button, textarea, a')) return;
      this.focusThread(thread.id, false);
      this.editorHost.querySelector(`.nb-comment[data-thread="${CSS.escape(thread.id)}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    };
    card.append(el('blockquote', 'doc-quote', thread.quote.length > 160 ? `${thread.quote.slice(0, 160)}…` : thread.quote));
    for (const message of thread.messages) {
      const box = el('div', `doc-message ${message.author}`);
      const who = el('div', 'doc-author', message.author === 'nembot' ? 'nembot' : 'You');
      if (message.model) who.append(el('span', 'muted', ` · ${message.model}`));
      const text = el('div', 'doc-text');
      if (message.author === 'nembot') text.innerHTML = renderMarkdown(message.text || '');
      else text.textContent = message.text;
      box.append(who, text);
      if (message.error) box.append(el('div', 'error', message.error));
      card.append(box);
    }
    for (const s of thread.suggestions) card.append(this.suggestionRow(thread, s));
    if (this.asking.has(thread.id)) {
      const status = el('div', 'status doc-asking');
      status.append(throbber(), el('span', '', thread.pendingLabel || 'Thinking…'));
      card.append(status);
    } else if (!thread.resolved) {
      const box = el('textarea');
      box.rows = 2;
      box.placeholder = thread.messages.length ? 'Reply, or ask nembot a follow-up…' : 'Comment, or ask nembot about this…';
      const ask = el('button', 'small primary', 'Ask nembot');
      const note = el('button', 'small', thread.messages.length ? 'Reply' : 'Comment');
      const send = toNembot => {
        const text = box.value.trim();
        if (!text) return box.focus();
        box.value = '';
        if (toNembot) this.ask(thread, text);
        else { thread.messages.push({ author: 'user', text, at: new Date().toISOString() }); this.threadChanged(thread); }
      };
      ask.onclick = () => send(true);
      note.onclick = () => send(false);
      box.onkeydown = e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(true); } };
      const actions = el('div', 'doc-actions');
      const resolve = el('button', 'link-like', thread.messages.length ? 'Resolve' : 'Cancel');
      resolve.onclick = () => this.resolveThread(thread);
      actions.append(resolve, el('span', 'spacer'), note, ask);
      card.append(box, actions);
    } else {
      const reopen = el('button', 'link-like', 'Reopen');
      reopen.onclick = () => { thread.resolved = false; this.threadChanged(thread); };
      card.append(reopen);
    }
    return card;
  }

  threadChanged(thread) {
    delete thread.draft;
    this.renderThreads();
    this.scheduleSave();
  }

  resolveThread(thread) {
    if (!thread.messages.length) {  // an empty draft: drop it and its highlight
      this.removeComment(thread.id);
      this.threads = this.threads.filter(t => t !== thread);
    } else {
      thread.resolved = true;
    }
    this.threadChanged(thread);
  }

  removeComment(threadId) {
    const type = this.editor.schema.marks.comment;
    const tr = this.editor.state.tr;
    for (const [from, to] of this.markRanges('comment', 'thread', threadId)) tr.removeMark(from, to, type.create({ thread: threadId }));
    this.editor.view.dispatch(tr);
  }

  // ---- Asking nembot ----

  async ask(thread, request) {
    const prior = thread.messages.map(({ author, text }) => ({ author, text }));
    thread.messages.push({ author: 'user', text: request, at: new Date().toISOString() });
    delete thread.draft;
    const blocks = readBlocks(this.editor.state.doc);
    const tracker = new Map(blocks.map(b => [b.id, b.pos]));
    this.trackers.add(tracker);
    const firstMark = this.threadPosition(thread);
    const quoteBlock = Number.isFinite(firstMark) ? blocks.filter(b => b.pos <= firstMark).pop() : null;
    const controller = new AbortController();
    this.asking.set(thread.id, controller);
    const reply = { author: 'nembot', text: '', at: new Date().toISOString() };
    thread.pendingLabel = 'Thinking…';
    this.renderThreads();
    this.save();
    const relabel = label => {
      thread.pendingLabel = label;
      const span = this.threadPane.querySelector(`[data-thread="${CSS.escape(thread.id)}"] .doc-asking span:last-child`);
      if (span) span.textContent = label;
    };
    try {
      const response = await fetch(`/api/docs/${this.id}/ask`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify({ title: this.title, blocks: blocks.map(({ id, kind, text, label }) => ({ id, kind, text, label })),
          quote: thread.quote, quote_block: quoteBlock && quoteBlock.id, messages: prior, request,
          model: currentModel && currentModel.ref, web_search: webSearch.checked }),
      });
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || `HTTP ${response.status}`);
      await streamEvents(response, event => {
        if (event.type === 'model') reply.model = event.model.name;
        else if (event.type === 'text') { reply.text += event.delta; relabel('Writing…'); }
        else if (event.type === 'block') { if (reply.text && !reply.text.endsWith('\n')) reply.text += '\n\n'; }
        else if (event.type === 'tool' || event.type === 'tool_running') relabel(DOC_TOOL_LABELS[event.name] || `Running ${event.name}…`);
        else if (event.type === 'doc_suggestion') this.applySuggestion(thread, event, tracker);
        else if (event.type === 'error') reply.error = event.message;
      });
    } catch (err) {
      if (err.name !== 'AbortError') reply.error = err.message;
    } finally {
      this.trackers.delete(tracker);
      this.asking.delete(thread.id);
      delete thread.pendingLabel;
      if (!this.destroyed) {
        reply.text = reply.text.trim();
        if (reply.text || reply.error) thread.messages.push(reply);
        this.threadChanged(thread);
      }
    }
  }

  // ---- nembot's suggestions ----

  // HTML to ProseMirror content with this editor's schema (block or inline, as the HTML is).
  parse(html) {
    return this.core.createNodeFromContent(html, this.editor.schema, { slice: true });
  }

  afterPos(tracker, after) {
    const { doc } = this.editor.state;
    if (after === 'START' || !tracker.has(after)) return 0;
    const pos = tracker.get(after);
    const $pos = doc.resolve(pos);
    return $pos.depth === 0 ? pos + doc.nodeAt(pos).nodeSize : $pos.after(1);
  }

  applySuggestion(thread, s, tracker) {
    if (s.kind === 'insert') return this.applyInsert(thread, s, tracker);
    if (s.kind === 'replace') return this.applyReplace(thread, s, tracker);
    const { state, schema } = this.editor;
    const tr = state.tr.insert(this.afterPos(tracker, s.after), schema.nodes.dataBlock.create(s.attrs));
    this.editor.view.dispatch(tr);
    const label = `Add ${s.attrs.kind === 'chart' ? 'chart' : 'table'}${s.attrs.title ? ` “${clip(s.attrs.title)}”` : ''}`;
    thread.suggestions.push({ id: s.id, kind: s.kind, label, status: 'pending' });
    this.renderThreads();
  }

  // Strike out the old wording and insert the new (inline markdown, already HTML) right after it.
  applyReplace(thread, s, tracker) {
    const { state, schema } = this.editor;
    const pos = tracker.get(s.block);
    const node = typeof pos === 'number' && state.doc.nodeAt(pos);
    const { text, map } = node && node.isTextblock ? visibleText(node, pos) : { text: '', map: [] };
    const index = text.indexOf(s.find);
    if (index < 0) {
      thread.suggestions.push({ id: s.id, kind: s.kind, label: `Couldn't apply: “${clip(s.find)}” was edited meanwhile`, status: 'failed' });
      return this.renderThreads();
    }
    const from = map[index], to = map[index + s.find.length - 1] + 1;
    const comments = new Set();
    state.doc.nodesBetween(from, to, n => { for (const m of n.marks) if (m.type.name === 'comment') comments.add(m.attrs.thread); });
    const tr = state.tr.addMark(from, to, schema.marks.suggestDelete.create({ suggestion: s.id }));
    let added = 0;
    if (s.replace.trim()) {
      // Sized exactly (other plugins may add their own steps after this transaction).
      const content = this.parse(renderInlineMarkdown(s.replace.replace(/\s+/g, ' ').trim()));
      tr.insert(to, content);
      added = content.size;
      tr.removeMark(to, to + added, schema.marks.suggestDelete);
      tr.addMark(to, to + added, schema.marks.suggestInsert.create({ suggestion: s.id }));
      // The new wording keeps any comment highlights on the old, so threads survive an accept.
      for (const id of comments) tr.addMark(to, to + added, schema.marks.comment.create({ thread: id }));
    }
    this.editor.view.dispatch(tr);
    const replacement = added ? this.editor.state.doc.textBetween(to, to + added, ' ') : '';
    const label = replacement ? `Replace “${clip(s.find)}” with “${clip(replacement)}”` : `Delete “${clip(s.find)}”`;
    thread.suggestions.push({ id: s.id, kind: s.kind, label, status: 'pending' });
    this.renderThreads();
  }

  // New paragraphs/headings/lists: Tiptap parses the HTML, then the new top-level blocks are tagged.
  applyInsert(thread, s, tracker) {
    const at = this.afterPos(tracker, s.after);
    const content = this.parse(renderMarkdown(s.markdown));
    const tr = this.editor.state.tr.insert(at, content);
    tr.doc.nodesBetween(at, at + content.size, (node, pos) => {
      if (SUGGESTABLE.includes(node.type.name)) tr.setNodeMarkup(pos, undefined, { ...node.attrs, suggestion: s.id });
      return false;
    });
    this.editor.view.dispatch(tr);
    const text = this.editor.state.doc.textBetween(at, at + content.size, ' ');
    thread.suggestions.push({ id: s.id, kind: s.kind, label: `Insert “${clip(text)}”`, status: 'pending' });
    this.renderThreads();
  }

  suggestionRow(thread, s) {
    const row = el('div', `doc-suggestion ${s.status}`);
    row.append(el('span', 'doc-suggestion-label', s.label));
    if (s.status === 'pending') {
      const accept = el('button', 'small primary', 'Accept');
      const reject = el('button', 'small', 'Reject');
      accept.onclick = () => this.settle(thread, s, true);
      reject.onclick = () => this.settle(thread, s, false);
      row.append(accept, reject);
      row.onmouseenter = () => this.editorHost.querySelectorAll(`[data-suggestion="${CSS.escape(s.id)}"]`).forEach(n => n.classList.add('hover'));
      row.onmouseleave = () => this.editorHost.querySelectorAll('.hover').forEach(n => n.classList.remove('hover'));
    } else if (s.status !== 'failed') {
      row.append(el('span', 'muted', s.status === 'accepted' ? '✓ accepted' : 'rejected'));
    }
    return row;
  }

  // Accept: drop struck-out text and keep insertions. Reject: the reverse.
  settle(thread, s, accept) {
    const { state, schema } = this.editor;
    const tr = state.tr;
    const drop = accept ? 'suggestDelete' : 'suggestInsert';
    const keep = accept ? 'suggestInsert' : 'suggestDelete';
    const removals = this.markRanges(drop, 'suggestion', s.id);
    const nodes = [];
    state.doc.descendants((node, pos) => { if (node.attrs && node.attrs.suggestion === s.id) nodes.push([pos, node]); });
    for (const [from, to] of this.markRanges(keep, 'suggestion', s.id)) tr.removeMark(from, to, schema.marks[keep]);
    const deletions = [...removals, ...(accept ? [] : nodes.map(([pos, node]) => [pos, pos + node.nodeSize]))]
      .sort((a, b) => b[0] - a[0]);
    if (accept) for (const [pos, node] of nodes) tr.setNodeMarkup(pos, undefined, { ...node.attrs, suggestion: null });
    for (const [from, to] of deletions) tr.delete(tr.mapping.map(from), tr.mapping.map(to));
    this.editor.view.dispatch(tr);
    s.status = accept ? 'accepted' : 'rejected';
    this.threadChanged(thread);
  }
}

const DOC_TOOL_LABELS = { query_mms: 'Running query…', describe_table: 'Reading table docs…', suggest_edit: 'Suggesting an edit…',
  suggest_insert: 'Suggesting new text…', suggest_table: 'Building a table…', suggest_chart: 'Building a chart…',
  web_search: 'Searching the web…', web_fetch: 'Reading a web page…' };

function clip(text, n = 60) {
  text = text.replace(/\s+/g, ' ').trim();
  return text.length > n ? `${text.slice(0, n)}…` : text;
}

// ---- Where documents open ----

function openEditor(container, id, options) {
  // One editor per document: close it wherever else it is open.
  for (const key of ['page', 'side']) {
    if (docsState[key] && docsState[key].id === id) { docsState[key].destroy(); docsState[key] = null; }
  }
  return new DocEditor(container, id, options);
}

function openDocBeside(id) {
  if (docsState.side && docsState.side.id === id) return docsState.side;
  docsState.side?.destroy();
  document.getElementById('chat-view').classList.add('with-doc');
  docPanel.hidden = false;
  docsState.side = openEditor(docPanel, id, { beside: true });
  return docsState.side;
}

function closeDocBeside() {
  docsState.side?.destroy();
  docsState.side = null;
  docPanel.hidden = true;
  document.getElementById('chat-view').classList.remove('with-doc');
}

async function createDoc(title) {
  docsState.list = null;
  return sendJSON('/api/docs', 'POST', { title });
}

// "＋ Add to doc" next to chat tables and plots.
function docButton(payload) {
  const button = el('button', 'small', '＋ Add to doc');
  button.title = 'Add a snapshot of this table or chart to a shared document';
  button.onclick = async e => {
    e.stopPropagation();
    if (docsState.side) return addToDoc(docsState.side, payload, button);
    const menu = el('div', 'doc-menu');
    const choose = async id => { menu.remove(); await openDocBeside(id).ready; addToDoc(docsState.side, payload, button); };
    const fresh = el('button', 'doc-menu-item', '＋ New document');
    fresh.onclick = async () => choose((await createDoc(payload.question ? clip(payload.question, 60) : 'Untitled document')).id);
    menu.append(fresh);
    try {
      for (const doc of (await getJSON('/api/docs')).docs.slice(0, 8)) {
        const item = el('button', 'doc-menu-item', doc.title);
        item.onclick = () => choose(doc.id);
        menu.append(item);
      }
    } catch { /* offline: offer a new document only */ }
    document.body.append(menu);
    const box = button.getBoundingClientRect();
    menu.style.left = `${Math.min(box.left, innerWidth - 260)}px`;
    menu.style.top = `${box.bottom + 4}px`;
    setTimeout(() => document.addEventListener('click', function away(ev) {
      if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('click', away); }
    }), 0);
  };
  return button;
}

async function addToDoc(editor, payload, button) {
  await editor.ready;
  const label = button.textContent;
  button.disabled = true; button.textContent = 'Adding…';
  try {
    editor.insertBlock(await sendJSON('/api/docs/block', 'POST', { sql: payload.sql, plot_spec: payload.plot_spec }));
    button.textContent = '✓ Added';
  } catch (err) {
    alert(`Couldn't add it: ${err.message}`);
    button.textContent = label;
  } finally {
    button.disabled = false;
    setTimeout(() => { button.textContent = label; }, 2000);
  }
}

// ---- The Docs page ----

async function showDocs(id) {
  if (!docsState.list) {
    try { docsState.list = (await getJSON('/api/docs')).docs; } catch (err) { docList.replaceChildren(el('p', 'error', err.message)); return; }
  }
  drawDocList(id);
  const docId = id ? Number(id) : null;
  if (!docId) {
    docsState.page?.destroy();
    docsState.page = null;
    docPage.replaceChildren(el('p', 'empty', docsState.list.length ? 'Choose a document on the left.' : 'No documents yet. Create one, or ask nembot in the chat to write something up.'));
    return;
  }
  if (docsState.page && docsState.page.id === docId) return;
  docsState.page?.destroy();
  if (docsState.side && docsState.side.id === docId) closeDocBeside();
  docsState.page = openEditor(docPage, docId, {});
  docsState.page.ready.catch(err => docPage.replaceChildren(el('p', 'error', `Couldn't open the document: ${err.message}`)));
}

function drawDocList(current) {
  if (!docsState.list) return;
  const active = current ?? (docsState.page && String(docsState.page.id));
  const query = docSearch.value.trim().toLowerCase();
  const items = docsState.list.filter(d => !query || d.title.toLowerCase().includes(query));
  docList.replaceChildren(...items.map(d => {
    const link = el('a', 'snippet-link' + (String(d.id) === String(active) ? ' current' : ''));
    link.href = `#docs/${d.id}`;
    link.append(el('span', '', d.title), el('span', 'rows', formatStamp(d.updated_at)));
    return link;
  }));
  if (!items.length && docsState.list.length) docList.append(el('p', 'empty', 'No matching documents.'));
}

docSearch.oninput = () => drawDocList();
document.getElementById('new-doc').onclick = async () => {
  const doc = await createDoc('Untitled document');
  location.hash = `#docs/${doc.id}`;
};
window.addEventListener('beforeunload', () => { for (const key of ['page', 'side']) if (docsState[key]?.dirty) docsState[key].save(); });
