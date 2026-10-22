nembot.onChatEvent("html_preview", (event, place) => {
  const data = event.data || {};
  const html = typeof data.html === "string" ? data.html : "";
  const height = Number(data.height) || 480;
  const el = nembot.el;

  const box = el("div", "html_preview-box");
  const bar = el("div", "html_preview-bar");
  bar.append(el("span", "html_preview-title", data.title || "HTML preview"));

  const actions = el("span", "html_preview-actions");
  const sourceBtn = el("button", "small", "Source");
  const copyBtn = el("button", "small", "Copy HTML");
  const reloadBtn = el("button", "small", "Reload");
  const fullBtn = el("button", "small", "Expand");
  actions.append(sourceBtn, copyBtn, reloadBtn, fullBtn);
  bar.append(actions);

  const frameWrap = el("div", "html_preview-frame-wrap");
  frameWrap.style.height = height + "px";
  const frame = document.createElement("iframe");
  frame.className = "html_preview-frame";
  // Scripts may run, but without allow-same-origin the page cannot touch nembot.
  frame.setAttribute("sandbox", "allow-scripts allow-modals allow-popups");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.title = data.title || "HTML preview";
  frame.srcdoc = html;
  frameWrap.append(frame);

  const source = el("pre", "html_preview-source");
  source.textContent = html;
  source.hidden = true;

  sourceBtn.onclick = () => {
    const showing = !source.hidden;
    source.hidden = showing;
    frameWrap.hidden = !showing;
    sourceBtn.textContent = showing ? "Source" : "Preview";
  };
  copyBtn.onclick = async () => {
    try {
      await navigator.clipboard.writeText(html);
      copyBtn.textContent = "Copied";
    } catch (e) {
      copyBtn.textContent = "Copy failed";
    }
    setTimeout(() => (copyBtn.textContent = "Copy HTML"), 1500);
  };
  reloadBtn.onclick = () => {
    frame.srcdoc = "";
    setTimeout(() => (frame.srcdoc = html), 0);
  };
  let expanded = false;
  fullBtn.onclick = () => {
    expanded = !expanded;
    box.classList.toggle("html_preview-expanded", expanded);
    fullBtn.textContent = expanded ? "Close" : "Expand";
  };
  document.addEventListener("keydown", (e) => {
    if (expanded && e.key === "Escape") fullBtn.click();
  });

  box.append(bar, frameWrap, source);
  place(box);
});
