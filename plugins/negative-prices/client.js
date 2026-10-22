nembot.addTab({ id: "daily", title: "Negative prices", render(container) {
  container.append(nembot.el("h2", "", "Hours per day with negative prices"));
  const note = nembot.el("p", "", "Loading...");
  container.append(note);
  nembot.getJSON("/daily").then(data => {
    note.textContent = "Hours per day with RRP below $0 (5-minute intervals, market time).";
    container.append(nembot.renderTable({ columns: data.columns, rows: data.rows }));
  }).catch(e => { note.textContent = "Failed to load: " + e; });
} });
