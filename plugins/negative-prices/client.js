nemsplainer.addTab({ id: "daily", title: "Negative prices", render(container) {
  container.append(nemsplainer.el("h2", "", "Hours per day with negative prices"));
  const note = nemsplainer.el("p", "", "Loading...");
  container.append(note);
  nemsplainer.getJSON("/daily").then(data => {
    note.textContent = "Hours per day with RRP below $0 (5-minute intervals, market time).";
    container.append(nemsplainer.renderTable({ columns: data.columns, rows: data.rows }));
  }).catch(e => { note.textContent = "Failed to load: " + e; });
} });
