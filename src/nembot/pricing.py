"""Claude API prices for the chat's cost display (USD per million tokens).

Taken from https://platform.claude.com/docs/en/about-claude/pricing on 2026-10-23; update the
table when prices change. The app uses the default 5-minute prompt cache, so cache writes use
the 5-minute rate. Web search costs $10 per 1,000 searches; web fetch has no extra charge.
OpenRouter reports each request's cost itself, so it needs no table.
"""

# model id (or id prefix, for dated ids) -> (input, 5-minute cache write, cache read, output)
PRICES = {
    'claude-fable-5-1': (10, 12.5, 0.25, 50),
    'claude-mythos-5-1': (10, 12.5, 0.25, 50),
    'claude-fable-5': (10, 12.5, 1, 50),
    'claude-mythos-5': (10, 12.5, 1, 50),
    'claude-opus-5-5': (4, 5, 0.20, 20),
    'claude-opus-5': (5, 6.25, 0.50, 25),
    'claude-opus-4-8': (5, 6.25, 0.50, 25),
    'claude-opus-4-7': (5, 6.25, 0.50, 25),
    'claude-opus-4-6': (5, 6.25, 0.50, 25),
    'claude-opus-4-5': (5, 6.25, 0.50, 25),
    'claude-opus-4-1': (15, 18.75, 1.50, 75),
    'claude-opus-4': (15, 18.75, 1.50, 75),
    'claude-sonnet-5-5': (2, 2.5, 0.10, 10),
    'claude-sonnet-5': (2, 2.5, 0.20, 10),
    'claude-sonnet-4-6': (3, 3.75, 0.30, 15),
    'claude-sonnet-4-5': (3, 3.75, 0.30, 15),
    'claude-sonnet-4': (3, 3.75, 0.30, 15),
    'claude-haiku-5-5': (0.10, 0.125, 0.01, 0.50),
    'claude-haiku-4-5': (1, 1.25, 0.10, 5),
    'claude-3-5-haiku': (0.80, 1, 0.08, 4),
}
# Claude Haiku 5.5 charges more for a request whose whole prompt (input + cache reads + writes) is over 100k tokens.
LONG_PROMPT = {'claude-haiku-5-5': (100_000, (0.50, 0.625, 0.05, 2.50))}
WEB_SEARCH = 10 / 1000  # USD per search


def _prices(model_id: str):
    """The table entry for a model id, matching the longest prefix (ids may carry a date suffix)."""
    matches = [key for key in PRICES if model_id == key or model_id.startswith(key + '-')]
    return max(matches, key=len) if matches else None


def claude_cost(model_id: str, *, input: int, cache_write: int, cache_read: int, output: int,
                searches: int = 0) -> float | None:
    """USD for one Claude request, or None for a model missing from the table."""
    key = _prices(model_id)
    if key is None:
        return None
    rates = PRICES[key]
    if key in LONG_PROMPT and input + cache_write + cache_read > LONG_PROMPT[key][0]:
        rates = LONG_PROMPT[key][1]
    tokens = (input, cache_write, cache_read, output)
    return sum(n * rate for n, rate in zip(tokens, rates)) / 1_000_000 + searches * WEB_SEARCH
