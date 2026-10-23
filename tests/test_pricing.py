"""Cost and usage reporting for the chat's stats line (no network)."""
from types import SimpleNamespace as NS
import unittest

from nembot import chats, claude, openrouter, pricing
from nembot.models import ModelInfo


class Prices(unittest.TestCase):
    def test_longest_prefix_wins_for_dated_ids(self):
        self.assertEqual(pricing._prices('claude-opus-4-8'), 'claude-opus-4-8')
        self.assertEqual(pricing._prices('claude-opus-4-20250514'), 'claude-opus-4')
        self.assertEqual(pricing._prices('claude-sonnet-5'), 'claude-sonnet-5')
        self.assertEqual(pricing._prices('claude-sonnet-4-5-20250929'), 'claude-sonnet-4-5')
        self.assertIsNone(pricing._prices('gpt-unknown'))

    def test_cost_counts_cache_rates_and_searches(self):
        # Opus 5.5: $4 in, $5 cache write, $0.20 cache read, $20 out; $10 per 1,000 searches.
        cost = pricing.claude_cost('claude-opus-5-5', input=1000, cache_write=10000, cache_read=50000, output=2000, searches=2)
        self.assertAlmostEqual(cost, (4000 + 50000 + 10000 + 40000) / 1e6 + 0.02)
        self.assertIsNone(pricing.claude_cost('claude-new', input=1, cache_write=0, cache_read=0, output=1))

    def test_haiku_long_prompts_use_the_higher_rates(self):
        short = pricing.claude_cost('claude-haiku-5-5', input=90_000, cache_write=0, cache_read=0, output=1000)
        long = pricing.claude_cost('claude-haiku-5-5', input=90_000, cache_write=0, cache_read=20_000, output=1000)
        self.assertAlmostEqual(short, (90_000 * 0.10 + 1000 * 0.50) / 1e6)
        self.assertAlmostEqual(long, (90_000 * 0.50 + 20_000 * 0.05 + 1000 * 2.50) / 1e6)


class UsageEvents(unittest.TestCase):
    def test_claude_usage_is_priced_by_the_serving_model(self):
        usage = NS(input_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=1000, output_tokens=50,
                   server_tool_use=NS(web_search_requests=1))
        event = claude.usage_event(NS(model='claude-sonnet-5-5', usage=usage), started=10.0, finished=12.5)
        self.assertEqual((event['type'], event['input'], event['cache_read'], event['output'], event['searches'], event['seconds']),
                         ('usage', 100, 1000, 50, 1, 2.5))
        self.assertAlmostEqual(event['cost'], (100 * 2 + 1000 * 0.10 + 50 * 10) / 1e6 + 0.01)

    def test_openrouter_uses_its_reported_cost_or_the_catalogue_price(self):
        model = ModelInfo('openrouter:x/y', 'openrouter', 'x/y', 'Y', False, True, True, price_in=1.0, price_out=2.0)
        reported = NS(prompt_tokens=1000, completion_tokens=100, prompt_tokens_details=NS(cached_tokens=400),
                      model_extra={'cost': 0.0123})
        event = openrouter.usage_event(model, reported, started=1.0, finished=3.0)
        self.assertEqual((event['input'], event['cache_read'], event['output'], event['cost'], event['seconds']),
                         (600, 400, 100, 0.0123, 2.0))
        estimated = NS(prompt_tokens=1000, completion_tokens=100, prompt_tokens_details=None, model_extra={})
        self.assertAlmostEqual(openrouter.usage_event(model, estimated, None, 3.0)['cost'], (1000 + 200) / 1e6)
        self.assertIsNone(openrouter.usage_event(model, estimated, None, 3.0)['seconds'])

    def test_saved_chats_keep_usage_events(self):
        events = [{'type': 'usage', 'input': 1, 'output': 2, 'cost': 0.1}, {'type': 'tool_running', 'name': 'q'}]
        self.assertEqual(chats.compact(events), [events[0]])


if __name__ == '__main__':
    unittest.main()
