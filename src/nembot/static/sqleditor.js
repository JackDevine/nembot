// The explorer's SQL editor (#explorer/<TABLE>/sql): CodeMirror 6 (MIT, from esm.sh on first
// open) with SQL highlighting in marimo's colours and completion of downloaded tables, their
// columns and DuckDB's functions (with hover help from duckdb_functions()). As you type, DuckDB
// itself checks the query without running it (/api/explorer/check) and problems are underlined.
// Ctrl/⌘+Enter runs the query; the first SQL_ROWS rows show in a data table. If CodeMirror can't
// load (offline), a plain textarea takes its place.

const SQL_EDITOR = { codemirror: '6.0.2' };
const sqlDrafts = new Map();  // table -> editor text, kept while the page is open
let sqlEditorModules = null;
let sqlFunctions = null;  // name -> DuckDB function info, fetched once

function loadSqlEditor() {
  sqlEditorModules ||= Promise.all([
    import(`https://esm.sh/codemirror@${SQL_EDITOR.codemirror}`),
    import(`https://esm.sh/@codemirror/lang-sql@${LEZER.sql}`),
    import('https://esm.sh/@codemirror/view@^6.0.0'),
    import('https://esm.sh/@codemirror/state@^6.0.0'),
    import('https://esm.sh/@codemirror/language@^6.0.0'),
    import('https://esm.sh/@codemirror/commands@^6.0.0'),
    import('https://esm.sh/@codemirror/autocomplete@^6.0.0'),
    import('https://esm.sh/@codemirror/lint@^6.0.0'),
    loadHighlighter(),
  ]).then(([cm, sql, view, state, language, commands, autocomplete, lint, highlight]) =>
    ({ cm, sql, view, state, language, commands, autocomplete, lint, highlight }));
  return sqlEditorModules;
}

function loadSqlFunctions() {
  sqlFunctions ||= getJSON('/api/explorer/functions')
    .then(data => new Map(data.functions.map(f => [f.name, f])))
    .catch(() => new Map());
  return sqlFunctions;
}

// Help for one DuckDB function: its signatures, description and an example.
function functionHelp(fn) {
  const box = el('div', 'sql-fn-help');
  const shown = fn.signatures.slice(0, 4);
  box.append(...shown.map(sig => el('div', 'sql-fn-sig', sig)));
  if (fn.signatures.length > shown.length) box.append(el('div', 'muted', `+${fn.signatures.length - shown.length} more`));
  if (fn.description) box.append(el('div', 'sql-fn-desc', fn.description));
  if (fn.examples.length) box.append(el('div', 'sql-fn-example', `e.g. ${fn.examples[0]}`));
  return box;
}

// Live checking: DuckDB parses and binds the query (nothing runs) and returns what's wrong.
async function sqlDiagnostics(view) {
  const sql = view.state.doc.toString();
  let problems;
  try {
    const response = await fetch('/api/explorer/check', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql }) });
    problems = (await response.json()).problems || [];
  } catch { return []; }
  if (view.state.doc.toString() !== sql) return [];  // edited meanwhile: the next check covers it
  return problems.map(p => ({ from: p.from, to: p.to, severity: 'error', source: 'DuckDB', message: p.message }));
}

function defaultSql(table) {
  return `SELECT *\nFROM ${table}\nLIMIT 100`;
}

// An editor in ``parent``; returns {value(), focus()}. ``run`` is called on Ctrl/⌘+Enter. Bare column
// names complete from ``table``; other tables' columns after `TABLE.`.
async function createSqlEditor(parent, text, schema, table, run, onChange) {
  try {
    const { cm, sql, view, state, language, commands, autocomplete, lint, highlight } = await loadSqlEditor();
    const fns = await loadSqlFunctions();
    // DuckDB's SQL is Postgres-like, but it matches names case-insensitively: complete REGIONID, not "REGIONID".
    const dialect = sql.SQLDialect.define({ ...sql.PostgreSQL.spec, caseInsensitiveIdentifiers: true });
    const functionOptions = [...fns.values()].map(f => ({
      label: f.name, type: 'function', detail: f.type === 'aggregate' ? 'aggregate' : f.type.replace('_', ' '),
      info: () => functionHelp(f), boost: -1,  // after tables and columns
    }));
    const completeFunctions = context => {
      const word = context.matchBefore(/\w+/);
      if (!word || (word.from === word.to && !context.explicit)) return null;
      return { from: word.from, options: functionOptions, validFor: /^\w*$/ };
    };
    // Hovering a function call (a name followed by "(") shows its help.
    const functionHover = view.hoverTooltip((v, pos) => {
      const range = v.state.wordAt(pos);
      if (!range) return null;
      const fn = fns.get(v.state.sliceDoc(range.from, range.to).toLowerCase());
      if (!fn || !/^\s*\(/.test(v.state.sliceDoc(range.to, range.to + 20))) return null;
      return { pos: range.from, end: range.to, above: true, create: () => ({ dom: functionHelp(fn) }) };
    });
    const editor = new cm.EditorView({
      parent,
      doc: text,
      extensions: [
        // Tab accepts the highlighted completion, otherwise indents (CodeMirror leaves Tab to move focus
        // by default). Escape then Tab still moves focus out of the editor.
        state.Prec.highest(view.keymap.of([{ key: 'Mod-Enter', run: () => { run(); return true; } },
          { key: 'Tab', run: autocomplete.acceptCompletion }, commands.indentWithTab])),
        cm.basicSetup,
        sql.sql({ dialect, schema, defaultTable: table, upperCaseKeywords: true }),
        dialect.language.data.of({ autocomplete: completeFunctions }),
        functionHover,
        lint.linter(sqlDiagnostics, { delay: 500 }),
        language.syntaxHighlighting(highlight.style),  // the .tok-* colours used for SQL everywhere else
        cm.EditorView.updateListener.of(update => { if (update.docChanged) onChange(update.state.doc.toString()); }),
        cm.EditorView.theme({
          '&': { backgroundColor: 'var(--surface)', color: 'var(--fg)', fontSize: '13px' },
          '.cm-content': { fontFamily: 'ui-monospace, monospace', caretColor: 'var(--fg)' },
          '.cm-cursor': { borderLeftColor: 'var(--fg)' },
          '.cm-gutters': { backgroundColor: 'var(--bg)', color: 'var(--faint)', borderRight: '1px solid var(--border)' },
          '.cm-activeLine, .cm-activeLineGutter': { backgroundColor: 'transparent' },
          '&.cm-focused': { outline: 'none' },
          '.cm-tooltip': { backgroundColor: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--fg)' },
          '.cm-tooltip-lint, .cm-tooltip-hover': { maxWidth: '560px' },
          '.cm-diagnostic': { whiteSpace: 'pre-wrap', fontSize: '12px' },
        }),
      ],
    });
    return { value: () => editor.state.doc.toString(), focus: () => editor.focus() };
  } catch (err) {
    console.warn('CodeMirror unavailable, using a plain editor', err);
    const area = el('textarea', 'sql-fallback');
    area.value = text;
    area.spellcheck = false;
    area.oninput = () => onChange(area.value);
    area.onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); } };
    parent.append(area);
    return { value: () => area.value, focus: () => area.focus() };
  }
}

async function drawSqlEditor(table) {
  const title = el('div', 'title-row');
  const back = el('a', '', `← ${table}`); back.href = `#explorer/${encodeURIComponent(table)}`;
  const runButton = el('button', 'primary', 'Run');
  runButton.title = 'Run the query (Ctrl+Enter or ⌘+Enter)';
  const snippetPayload = { sql: '', plot_spec: null, question: null };  // the last query that ran
  const save = snippetButton(snippetPayload);
  save.hidden = true;
  const ask = el('button', '', 'Ask the chat about this');
  const actions = el('span', 'actions');
  actions.append(save, ask, runButton);
  title.append(el('h2', '', 'SQL editor'), back, el('span', 'spacer'), actions);
  const help = el('p', 'muted', `Read-only: one SELECT at a time, showing the first ${(1000).toLocaleString()} rows. ` +
    'Tables, columns and DuckDB functions autocomplete as you type (Tab or Enter accepts); hover a function for help. ' +
    'DuckDB checks the query as you type and underlines problems. To leave the editor with the keyboard, press Escape, then Tab.');
  const host = el('div', 'sql-editor');
  const results = el('div', 'sql-results');
  tableView.replaceChildren(title, help, host, results);

  const schema = await getJSON('/api/explorer/schema').catch(() => ({}));
  if (explorer.current !== table) return;  // navigated away meanwhile
  const run = async () => {
    const sql = editor.value().trim();
    if (!sql) return;
    runButton.disabled = true;
    results.replaceChildren(el('div', 'status', 'Running…'));
    try {
      const response = await fetch('/api/explorer/sql', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sql }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const count = data.truncated ? `First ${data.rows.length.toLocaleString()} rows (there are more: aggregate or add a LIMIT)`
        : `${data.rows.length.toLocaleString()} row${data.rows.length === 1 ? '' : 's'}`;
      results.replaceChildren(el('div', 'muted sql-meta', `${count} · ${formatSeconds(data.seconds)}`),
        createDataTable({ columns: data.columns.map(name => ({ name })), rows: data.rows, total: data.rows.length,
          exportName: `nembot-${table.toLowerCase()}-query` }));
      snippetPayload.sql = sql;
      save.hidden = false;
    } catch (err) {
      results.replaceChildren(el('pre', 'error sql-error', err.message));
    }
    runButton.disabled = false;
  };
  const editor = await createSqlEditor(host, sqlDrafts.get(table) ?? defaultSql(table), schema, table, run,
    text => sqlDrafts.set(table, text));
  if (explorer.current !== table) return;
  runButton.onclick = run;
  ask.onclick = () => askAbout(`About this query on ${table}:\n\n${editor.value().trim()}\n\n`);
  editor.focus();
  run();
}
