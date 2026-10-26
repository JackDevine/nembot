"""The questionnaire tool: questions shown in the chat, answers returned to the model (no network)."""
import asyncio
import unittest

from nembot.chat import QUESTIONNAIRE, Conversation, clean_answers, parse_questions, run_turn

from test_chat import GLM, DatabaseCase

QUESTIONS = [{'question': 'Which region?', 'header': 'Region', 'options': [{'label': 'NSW1'}, {'label': 'VIC1'}]},
             {'question': 'Which measures?', 'options': [{'label': 'Price', 'description': 'RRP'}, {'label': 'Demand'}],
              'multi_select': True}]


class Asker:
    """Asks the questions, then repeats the tool result it got back."""

    def __init__(self, questions=QUESTIONS):
        self.questions, self.seen_tools, self.result = questions, None, None

    async def respond(self, model, history, web_search=False, *, system=None, tools=None):
        self.seen_tools = [t['name'] for t in tools or []]
        if history[-1]['role'] == 'tool':
            self.result = history[-1]['results'][0]
            yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': 'Thanks.',
                   'tool_calls': [], 'raw': {}, 'stop': 'end'}
            return
        yield {'role': 'assistant', 'model': model.ref, 'model_name': model.name, 'text': '', 'raw': {},
               'tool_calls': [{'id': 'q1', 'name': 'questionnaire', 'input': {'questions': self.questions}}],
               'stop': 'tool_use'}


class Questionnaire(DatabaseCase):
    def turn(self, provider, conversation, reply=None, interactive=True):
        async def go():
            events = []
            async for event in run_turn({'openrouter': provider}, self.db, conversation, 'q', GLM,
                                        interactive=interactive):
                events.append(event)
                if event['type'] == 'questions':
                    conversation.pending[event['id']].set_result(reply)  # what POST /api/chat/answer does
            return events
        return asyncio.run(asyncio.wait_for(go(), 10))

    def test_answers_go_back_to_the_model(self):
        provider, conversation = Asker(), Conversation()
        reply = {'answers': [{'selected': ['VIC1', 'NSW1']}, {'selected': ['Price', 'Made up'], 'other': 'and interconnectors'}]}
        events = self.turn(provider, conversation, reply)
        shown = next(e for e in events if e['type'] == 'questions')
        self.assertEqual([q['header'] for q in shown['questions']], ['Region', ''])
        answered = next(e for e in events if e['type'] == 'questions_answered')
        self.assertEqual(answered['answers'], [{'selected': ['VIC1'], 'other': ''},  # single choice keeps one
                                               {'selected': ['Price'], 'other': 'and interconnectors'}])
        self.assertIn('Which region?\n   -> VIC1', provider.result['text'])
        self.assertIn('Price; (in their own words) and interconnectors', provider.result['text'])
        self.assertIn('questionnaire', provider.seen_tools)
        self.assertEqual(conversation.pending, {})

    def test_skipping_tells_the_model_to_assume(self):
        provider = Asker()
        events = self.turn(provider, Conversation(), {'skipped': True})
        self.assertIn({'type': 'questions_answered', 'id': 'q1', 'skipped': True}, events)
        self.assertIn('skipped', provider.result['text'])

    def test_bad_questions_are_returned_as_an_error(self):
        provider = Asker([{'question': 'Pick one?', 'options': [{'label': 'Only'}]}])
        events = self.turn(provider, Conversation())
        self.assertFalse(any(e['type'] == 'questions' for e in events))
        self.assertTrue(provider.result['is_error'])
        self.assertIn('2-4 options', provider.result['text'])

    def test_unattended_runs_never_get_the_tool(self):
        provider = Asker()
        self.turn(provider, Conversation(), interactive=False)
        self.assertNotIn('questionnaire', provider.seen_tools)


class Parsing(unittest.TestCase):
    def test_limits(self):
        self.assertIsInstance(parse_questions({'questions': QUESTIONS * 3}), str)  # 6 questions
        self.assertIsInstance(parse_questions({'questions': [{'question': 'x?', 'options': ['a', 'a']}]}), str)
        parsed = parse_questions({'questions': '[{"question": "x?", "options": ["a", "b"]}]'})  # JSON-string input
        self.assertEqual([o['label'] for o in parsed[0]['options']], ['a', 'b'])

    def test_answers_keep_only_offered_labels(self):
        questions = parse_questions({'questions': QUESTIONS})
        self.assertEqual(clean_answers(questions, 'nonsense'), [{'selected': [], 'other': ''}] * 2)

    def test_schema_matches_the_limits(self):
        items = QUESTIONNAIRE['input_schema']['properties']['questions']
        self.assertEqual((items['minItems'], items['maxItems']), (1, 4))


if __name__ == '__main__':
    unittest.main()
