// Example nembot plugin UI: a "Spikes" tab and a renderer for the tool's summary event.

nembot.addTab({
  id: 'spikes',
  title: 'Spikes',
  render(container) {
    const input = nembot.el('input');
    input.type = 'number';
    input.value = 300;
    input.className = 'price-spikes-threshold';
    const output = nembot.el('div');
    const load = async () => {
      output.replaceChildren(nembot.el('p', 'muted', 'Loading…'));
      const data = await nembot.getJSON(`/daily?threshold=${encodeURIComponent(input.value)}`);
      output.replaceChildren(data.rows.length
        ? nembot.renderTable({ columns: data.columns, rows: data.rows })
        : nembot.el('p', 'muted', 'No intervals above that price.'));
    };
    input.onchange = load;
    const label = nembot.el('label', 'price-spikes-label', 'Days with 5-minute prices above $/MWh ');
    label.append(input);
    container.append(nembot.el('h2', '', 'Price spikes'), label, output);
    load();
  },
});

nembot.onChatEvent('summary', (event, place) => {
  place(nembot.el('div', 'price-spikes-badge', `⚡ ${event.data.count.toLocaleString()} intervals above $${event.data.threshold}/MWh`));
});
