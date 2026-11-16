import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import review_meanings as r

NOW = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)


def lesson():
    return {'episode': {'id': 'npr-public', 'audioURL': 'https://example.org/frozen.mp3',
                        'audio_sha256': 'unchanged'}, 'generated_at': 'old',
            'user_notes': 'PRIVATE LEARNER NOTE MUST NOT BE SENT',
            'items': [
                {'sentence': 'A ____ approved the proposal.', 'answer': 'federal judge',
                 'meaning': 'Old clue', 'start': 30, 'end': 31,
                 'meaning_context': {'text': 'A federal judge approved the proposal after a hearing.'}},
                {'sentence': 'They will meet for ____ next week.', 'answer': 'peace talks',
                 'meaning': 'Other clue', 'start': 50, 'end': 51,
                 'meaning_context': {'text': 'They will meet for peace talks next week after months of negotiations.'}}
            ]}


def response(suitable=True):
    rows = [
        {'id': 1, 'suitable': True, 'meaning_ko': '연방 판사', 'reason_ko': '판결을 내린 주체입니다.',
         'evidence': 'A federal judge approved the proposal'},
        {'id': 2, 'suitable': suitable, 'meaning_ko': '평화 협상', 'reason_ko': '대화의 목적을 나타냅니다.',
         'evidence': 'meet for peace talks next week'}]
    return {'status': 'completed', 'usage': {'input_tokens': 1000, 'output_tokens': 500},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'reviews': rows})}]}]}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(r, 'LESSON', self.root/'lesson.json'))
        self.enterContext(patch.object(r, 'LEDGER', self.root/'ledger.json'))
        self.enterContext(patch.object(r, 'STATUS', self.root/'status.json'))
        self.enterContext(patch.dict(os.environ, {'OPENAI_API_KEY': 'TEST_SECRET_DO_NOT_PRINT',
                                                  'NPR_AI_REVIEW_ENABLED': 'true'}))
        r.atomic_json(r.LESSON, lesson())
        self.original = r.LESSON.read_bytes()
        self.calls = []

    def sender(self, key, encoded):
        self.calls.append(json.loads(encoded))
        self.assertEqual(key, 'TEST_SECRET_DO_NOT_PRINT')
        return response()

    def test_no_key_and_disabled_mode_make_no_calls_and_preserve_lesson(self):
        for env, state in [({'OPENAI_API_KEY': ''}, 'needs_api_key'),
                           ({'NPR_AI_REVIEW_ENABLED': 'false'}, 'disabled')]:
            with patch.dict(os.environ, env):
                status = r.review_lesson(NOW, self.sender)
                self.assertEqual(status['state'], state)
        self.assertFalse(self.calls)
        self.assertEqual(r.LESSON.read_bytes(), self.original)

    def test_success_preserves_answer_audio_timing_and_does_not_send_notes_or_key(self):
        status = r.review_lesson(NOW, self.sender)
        updated = r.load_json(r.LESSON, {})
        self.assertEqual(status['state'], 'success')
        self.assertEqual(updated['items'][0]['meaning'], '연방 판사')
        self.assertEqual(updated['episode'], lesson()['episode'])
        for a, b in zip(updated['items'], lesson()['items']):
            for field in ['sentence', 'answer', 'start', 'end']:
                self.assertEqual(a[field], b[field])
        sent = json.dumps(self.calls)
        self.assertNotIn('PRIVATE LEARNER NOTE', sent)
        self.assertNotIn('TEST_SECRET', sent)
        self.assertIn('after a hearing', sent)
        self.assertFalse(self.calls[0]['store'])
        self.assertEqual(self.calls[0]['reasoning']['effort'], 'none')
        self.assertEqual(Decimal(status['month_accounted_usd']), Decimal('.008'))
        self.assertIsNone(status['cash_charge_usd'])
        self.assertNotIn('TEST_SECRET', r.STATUS.read_text()+r.LEDGER.read_text())

    def test_success_is_cached_without_another_call(self):
        r.review_lesson(NOW, self.sender)
        status = r.review_lesson(NOW, self.sender)
        self.assertEqual(status['state'], 'cached')
        self.assertEqual(len(self.calls), 1)

    def test_inappropriate_questions_are_filtered_without_rewriting_answers(self):
        status = r.review_lesson(NOW, lambda *_: response(suitable=False))
        updated = r.load_json(r.LESSON, {})
        self.assertEqual(len(updated['items']), 1)
        self.assertEqual(updated['items'][0]['answer'], 'federal judge')
        self.assertEqual(status['questions_rejected'], 1)
        self.assertEqual(r.review_lesson(NOW, self.sender)['state'], 'cached')
        self.assertFalse(self.calls)

    def test_timeout_keeps_cost_reserve_and_stops_retries_for_the_day(self):
        def timeout(*_):
            raise TimeoutError('could include a secret in an exception')
        status = r.review_lesson(NOW, timeout)
        self.assertEqual(status['state'], 'review_failed')
        self.assertGreater(Decimal(status['month_accounted_usd']), 0)
        self.assertEqual(r.LESSON.read_bytes(), self.original)
        self.assertEqual(r.review_lesson(NOW, self.sender)['state'], 'daily_limit_reached')
        self.assertFalse(self.calls)
        self.assertNotIn('could include', r.STATUS.read_text())

    def test_monthly_budget_checks_worst_case_before_calling(self):
        r.atomic_json(r.LEDGER, {'version': 1, 'entries': [{'month':'2026-11','day':'2026-11-15','accounted_usd':'.99'}]})
        status = r.review_lesson(NOW, self.sender)
        self.assertEqual(status['state'], 'monthly_limit_reached')
        self.assertFalse(self.calls)
        self.assertEqual(r.LESSON.read_bytes(), self.original)

    def test_unknown_usage_is_counted_conservatively_not_as_free(self):
        def missing_usage(*_):
            data = response()
            del data['usage']
            return data
        status = r.review_lesson(NOW, missing_usage)
        self.assertEqual(status['state'], 'success')
        self.assertGreater(Decimal(status['month_accounted_usd']), Decimal('.008'))

    def test_invalid_or_invented_reviews_never_mutate_lesson_but_record_usage(self):
        for mode in ['invented', 'duplicate', 'incomplete', 'all_rejected', 'answer_leak', 'non_korean']:
            with self.subTest(mode=mode):
                if r.LEDGER.exists(): r.LEDGER.unlink()
                data = response()
                rows = json.loads(data['output'][0]['content'][0]['text'])['reviews']
                if mode == 'invented': rows[0]['evidence'] = 'A different judge approved this'
                if mode == 'duplicate': rows[1]['id'] = 1
                if mode == 'incomplete': data['status'] = 'incomplete'
                if mode == 'all_rejected':
                    for row in rows: row['suitable'] = False
                if mode == 'answer_leak': rows[0]['meaning_ko'] = '연방 판사 (federal judge)'
                if mode == 'non_korean': rows[0]['meaning_ko'] = 'a judge'
                data['output'][0]['content'][0]['text'] = json.dumps({'reviews': rows})
                status = r.review_lesson(NOW, lambda *_: data)
                self.assertEqual(status['state'], 'review_failed')
                self.assertEqual(r.LESSON.read_bytes(), self.original)
                self.assertEqual(Decimal(status['month_accounted_usd']), Decimal('.008'))

    def test_corrupted_ledger_fails_closed_and_preserves_accounting(self):
        for content in ['not json', '{"entries": [{}]}', '{"entries":[{"month":"2026-11","day":"2026-11-15","accounted_usd":"NaN"}]}', '{"entries":[{"month":"2026-11","day":"2026-11-15","accounted_usd":"-1"}]}']:
            r.LEDGER.write_text(content)
            with patch.object(r, 'send_request', side_effect=AssertionError('must not call')):
                r.main()
            self.assertEqual(r.load_json(r.STATUS, {})['state'], 'accounting_error')
            self.assertEqual(r.LEDGER.read_text(), content)

    def test_daily_limit_uses_korea_date_and_oversized_context_makes_no_call(self):
        # 16:00 UTC is already the following day in Korea.
        after_midnight = datetime(2026,11,16,16,tzinfo=timezone.utc)
        r.atomic_json(r.LEDGER, {'version':1,'entries':[{'month':'2026-11','day':'2026-11-16','accounted_usd':'.01'}]})
        self.assertEqual(r.review_lesson(after_midnight, self.sender)['state'], 'success')
        self.assertEqual(r.load_json(r.LEDGER,{})['entries'][-1]['day'], '2026-11-17')
        large = lesson()
        large['items'][0]['meaning_context']['text'] = 'x'*7000
        r.atomic_json(r.LESSON, large)
        with self.assertRaises(ValueError):
            r.review_lesson(NOW, self.sender)
        self.assertEqual(len(self.calls), 1)

    def test_http_failure_status_does_not_log_body_or_key(self):
        def denied(*_):
            raise HTTPError(r.ENDPOINT, 401, 'TEST_SECRET_DO_NOT_PRINT', {}, None)
        status = r.review_lesson(NOW, denied)
        self.assertEqual(status['http_status'], 401)
        self.assertNotIn('TEST_SECRET', r.STATUS.read_text())


if __name__ == '__main__':
    unittest.main()
