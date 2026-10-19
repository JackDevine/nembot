// Example nemsplainer plugin UI: a "Spikes" tab and a renderer for the tool's summary event.

nemsplainer.addTab({
  id: 'spikes',
  title: 'Spikes',
  render(container) {
    const input = nemsplainer.el('input');
    input.type = 'number';
    input.value = 300;
    input.className = 'price-spikes-threshold';
    const output = nemsplainer.el('div');
    const load = async () => {
      output.replaceChildren(nemsplainer.el('p', 'muted', 'Loading…'));
      const data = await nemsplainer.getJSON(`/daily?threshold=${encodeURIComponent(input.value)}`);
      output.replaceChildren(data.rows.length
        ? nemsplainer.renderTable({ columns: data.columns, rows: data.rows })
        : nemsplainer.el('p', 'muted', 'No intervals above that price.'));
    };
    input.onchange = load;
    const label = nemsplainer.el('label', 'price-spikes-label', 'Days with 5-minute prices above $/MWh ');
    label.append(input);
    container.append(nemsplainer.el('h2', '', 'Price spikes'), label, output);
    load();
  },
});

nemsplainer.onChatEvent('summary', (event, place) => {
  place(nemsplainer.el('div', 'price-spikes-badge', `⚡ ${event.data.count.toLocaleString()} intervals above $${event.data.threshold}/MWh`));
});
