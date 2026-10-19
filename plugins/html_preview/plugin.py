MAX_BYTES = 1_000_000  # 1 MB of HTML is plenty for a preview


def setup(nem):
    @nem.tool(
        "Render an HTML document inline in the current chat (sandboxed iframe) so the user can preview it "
        "without opening a new tab. Use when the user pastes HTML and asks to preview/render/see it, or asks "
        "you to write an HTML page/snippet and show it. Pass the complete HTML (a full document or a fragment).",
        {
            "type": "object",
            "properties": {
                "html": {"type": "string", "description": "The HTML to render (full document or fragment)."},
                "title": {"type": "string", "description": "Optional short label shown above the preview."},
                "height": {"type": "integer", "description": "Optional initial height in pixels (150-2000, default 480)."},
            },
            "required": ["html"],
        },
    )
    def show(args):
        html = args.get("html")
        if not isinstance(html, str) or not html.strip():
            return "Error: 'html' must be a non-empty string."
        size = len(html.encode("utf-8"))
        if size > MAX_BYTES:
            return f"Error: HTML is {size:,} bytes; the preview limit is {MAX_BYTES:,} bytes."
        title = args.get("title")
        title = title.strip()[:120] if isinstance(title, str) and title.strip() else "HTML preview"
        try:
            height = int(args.get("height") or 480)
        except (TypeError, ValueError):
            height = 480
        height = max(150, min(2000, height))
        return {
            "text": f"Rendered '{title}' ({size:,} bytes of HTML) as an inline preview in the chat.",
            "events": [{"kind": "html_preview", "data": {"html": html, "title": title, "height": height}}],
        }

    nem.prompt(
        "When the user wants to see/preview/render HTML in the chat, call html_preview_show with the full HTML "
        "instead of only printing the code."
    )
