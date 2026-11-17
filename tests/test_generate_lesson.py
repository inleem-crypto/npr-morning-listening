import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from scripts import generate_lesson as g

TEXT = "The local authorities are carefully reviewing the proposal before the final decision."


def segment(text=TEXT, start=30, probability=.99, **scores):
    words = [NS(word=' ' + w, start=start+i*.4, end=start+(i+1)*.4,
                probability=probability) for i, w in enumerate(text.split())]
    fields = dict(start=start, end=words[-1].end, text=text, words=words,
                  avg_logprob=-.1, no_speech_prob=.01, compression_ratio=1.2)
    fields.update(scores)
    return NS(**fields)


class QualityTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(g, 'STATUS', Path(directory) / 'status.json'))
        self.enterContext(patch.object(g, 'REPORT', Path(directory) / 'report.json'))

    def test_good_sentence_uses_word_times_and_warning(self):
        items = g.make_items([segment()])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['start'], 30)
        self.assertEqual(items[0]['end'], 34.8)
        self.assertIn('실제 음성', items[0]['explanation'])

    def test_reported_errors_are_excluded_even_with_high_scores(self):
        for text in [
            "His main challenger is center-right Gotti Eizencoat, a former Israeli military chief of staff.",
            "Come February, Jay-Z will take center stage, and Piers Isabella Gomez-Admiento reports.",
            "His main challenger is Gadi Eisenkot, a former military chief of staff.",
            "The network says Isabella Gomez Sarmiento reports on the musical event.",
        ]:
            with self.subTest(text=text):
                self.assertEqual(g.make_items([segment(text)]), [])

    def test_missing_low_or_nonfinite_evidence_is_rejected(self):
        for field, value in [('avg_logprob', -.6), ('avg_logprob', float('nan')),
                             ('no_speech_prob', .4), ('compression_ratio', 2.5),
                             ('words', None), ('avg_logprob', None)]:
            self.assertEqual(g.make_items([segment(**{field: value})]), [])
        self.assertEqual(g.make_items([segment(probability=.64)]), [])

    def test_answer_requires_stronger_probability(self):
        self.assertEqual(g.make_items([segment(probability=.8)]), [])

    def test_bad_middle_segment_cannot_be_spliced_out(self):
        first = segment('The local authorities are', 30)
        bad = segment('carefully reviewing', first.end, avg_logprob=-2)
        last = segment('the proposal before the final decision.', bad.end)
        self.assertEqual(g.make_items([first, bad, last]), [])
        bad.words = None
        self.assertEqual(g.make_items([first, bad, last]), [])

    def test_multiple_sentences_get_separate_timestamps(self):
        s = segment(TEXT + ' ' + TEXT.replace('local', 'regional'))
        items = g.make_items([s])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]['start'], items[0]['end'])

    def test_silence_after_complete_sentence_is_safe(self):
        items = g.make_items([segment(), segment(TEXT.replace('local', 'regional'), 50)])
        self.assertEqual(len(items), 2)

    def test_low_word_in_another_complete_sentence_does_not_taint_candidate(self):
        s = segment('Well. ' + TEXT)
        s.words[0].probability = .1
        self.assertEqual(len(g.make_items([s])), 1)

    def test_checker_uses_context_but_compares_only_target_words(self):
        items = g.make_items([segment()])
        s = segment(start=2)
        s.words.insert(0, NS(word=' earlier.', start=0, end=.3, probability=.1))
        model = NS(transcribe=lambda *a, **k: (iter([s]), None))
        self.assertEqual(g.check_items(items, [0]*600000, model), items)

    def test_gap_inside_sentence_taints_the_remainder(self):
        self.assertEqual(g.make_items([segment('The local authorities are', 30),
                                      segment('carefully reviewing the proposal before the final decision.', 50)]), [])

    def test_checker_never_rewrites_disagreement(self):
        items = g.make_items([segment()])
        original = copy.deepcopy(items)
        model = NS(transcribe=lambda *a, **k: (iter([segment(TEXT.replace('proposal', 'project'), start=2)]), None))
        with self.assertRaises(RuntimeError):
            g.check_items(items, [0]*600000, model)
        self.assertEqual(items, original)

    def test_numeric_disagreement_is_not_ignored(self):
        self.assertNotEqual(g.normalized_words('about 30 people'),
                            g.normalized_words('about 40 people'))

    def test_checker_accepts_only_strong_agreement(self):
        items = g.make_items([segment()])
        model = NS(transcribe=lambda *a, **k: (iter([segment(start=2)]), None))
        self.assertEqual(g.check_items(items, [0]*600000, model), items)
        model.transcribe = lambda *a, **k: (iter([segment(start=2, probability=.1)]), None)
        with self.assertRaises(RuntimeError):
            g.check_items(items, [0]*600000, model)

    def test_checker_caps_at_seven_without_padding(self):
        template = g.make_items([segment()])[0]
        items = [{**template, 'start': 30 + i*20, 'end': 34.8 + i*20}
                 for i in range(9)]
        model = NS(transcribe=lambda *a, **k: (iter([segment(start=2)]), None))
        accepted = g.check_items(items, [0]*4000000, model)
        self.assertEqual(len(accepted), 7)
        self.assertEqual(accepted, items[:7])

    def test_boundary_drift_recovers_complete_sentence_without_changing_text(self):
        items = g.make_items([segment()])
        for drift in [-.6, .6]:
            with self.subTest(drift=drift):
                diagnostics = []
                model = NS(transcribe=lambda *a, **k: (iter([segment(start=2+drift)]), None))
                self.assertEqual(g.check_items(items, [0]*600000, model, diagnostics), items)
                self.assertEqual(diagnostics[0]['reason'], 'accepted_boundary_recovery')
                self.assertNotEqual(g.normalized_words(diagnostics[0]['legacy_window_text']),
                                    g.normalized_words(TEXT))

    def test_alignment_rejects_distant_match_and_extra_words(self):
        items = g.make_items([segment()])
        for s, reason in [(segment(start=3), 'boundary_mismatch'),
                          (segment(TEXT.replace('proposal', 'new proposal'), start=2), 'text_mismatch'),
                          (segment('Actually ' + TEXT, start=1.6), 'boundary_mismatch'),
                          (segment(TEXT.replace('decision.', 'decision tomorrow.'), start=2), 'boundary_mismatch')]:
            diagnostics = []
            model = NS(transcribe=lambda *a, **k: (iter([s]), None))
            with self.assertRaises(RuntimeError):
                g.check_items(items, [0]*600000, model, diagnostics)
            self.assertEqual(diagnostics[0]['reason'], reason)

    def test_boundary_recovery_still_rejects_low_confidence_and_name_risk(self):
        items = g.make_items([segment()])
        for s, reason in [(segment(start=2.6, probability=.64), 'low_confidence'),
                          (segment(TEXT.replace('local', 'Local'), start=2.6), 'name_risk')]:
            diagnostics = []
            model = NS(transcribe=lambda *a, **k: (iter([s]), None))
            with self.assertRaises(RuntimeError):
                g.check_items(items, [0]*600000, model, diagnostics)
            self.assertEqual(diagnostics[0]['reason'], reason)

    def test_numbers_must_agree_in_checker(self):
        text = TEXT.replace('local authorities', '30 local authorities')
        items = g.make_items([segment(text)])
        self.assertTrue(items)
        model = NS(transcribe=lambda *a, **k: (iter([segment(text.replace('30', '40'), start=2)]), None))
        with self.assertRaises(RuntimeError):
            g.check_items(items, [0]*600000, model)

    def test_checker_rejects_missing_evidence_and_invalid_clocks(self):
        items = g.make_items([segment()])
        first, last = segment('The local authorities are', start=2), segment(
            'carefully reviewing the proposal before the final decision.', start=3.6)
        missing = NS(words=None)
        broken = segment(start=2)
        broken.words[4].start = float('nan')
        for segments in [[first, missing, last], [broken]]:
            model = NS(transcribe=lambda *a, **k: (iter(segments), None))
            with self.assertRaises(RuntimeError):
                g.check_items(items, [0]*600000, model)

    def test_failure_preserves_previous_lesson(self):
        with patch.object(g, 'latest_episode', side_effect=RuntimeError('offline')):
            before = g.OUTPUT.read_bytes()
            with self.assertRaises(RuntimeError):
                g.main()
            self.assertEqual(g.OUTPUT.read_bytes(), before)

    def test_rejected_audio_preserves_previous_lesson(self):
        model = NS(transcribe=lambda *a, **k: (iter([segment(avg_logprob=-2)]), None))
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / 'lesson.json'
            output.write_text('{"generator_version": 5}', encoding='utf-8')
            modules = {'faster_whisper': NS(WhisperModel=lambda *a, **k: model),
                       'faster_whisper.audio': NS(decode_audio=lambda *a: [0])}
            with patch.object(g, 'OUTPUT', output), patch.object(g, 'latest_episode', return_value={'id':'new'}), \
                    patch.object(g, 'fetch_bytes', return_value=b'audio'), patch.dict(sys.modules, modules):
                # Include the real URL field, but never use the network.
                with patch.object(g, 'latest_episode', return_value={'id':'new', 'audioURL':'https://example.org/audio'}):
                    with self.assertRaises(RuntimeError):
                        g.main()
            self.assertEqual(output.read_text(encoding='utf-8'), '{"generator_version": 5}')


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(g, "ROOT", self.directory))
        for name, file in [('OUTPUT', 'lesson.json'), ('STATUS', 'status.json'), ('REPORT', 'report.json')]:
            self.enterContext(patch.object(g, name, self.directory / file))
        g.OUTPUT.write_bytes(b'{"previous":"unchanged"}\n')
        self.before = g.OUTPUT.read_bytes()
        self.episode = {'id': 'new', 'audioURL': 'https://example.org/audio', 'title': 'News',
                        'date': '2026-11-02T20:00:00+00:00'}
        self.enterContext(patch.object(g, 'latest_episode', return_value=self.episode))
        self.enterContext(patch.object(g, 'fetch_bytes', return_value=b'audio'))

    def models(self, checker_text=TEXT, primary=None):
        primary = primary or segment()
        def model(name, **kwargs):
            s = primary if name == g.PRIMARY_MODEL else segment(checker_text, start=2.6)
            return NS(transcribe=lambda *a, **k: (iter([s]), None))
        self.enterContext(patch.dict(sys.modules, {
            'faster_whisper': NS(WhisperModel=model),
            'faster_whisper.audio': NS(decode_audio=lambda *a: [0]*600000)}))

    def test_first_two_articles_are_separate_and_use_correct_keywords(self):
        first = "President Trump visits Asia. NPR's Pat Smith reports from China. " + ("The president will negotiate a nuclear agreement with Russia and China. " * 4)
        second = "The former head of Nvidia resigned last week. NPR's Robin Jones explains. " + ("A petition supports his return to the company after the relationship became public. " * 4)
        third = "A federal court has approved a police investigation. NPR's Sam Lee has the story."
        reviews = g.make_summary_reviews([segment(first + second + third, start=30)])
        self.assertEqual(len(reviews), 2)
        self.assertIn("President Trump", reviews[0]['transcript'])
        self.assertNotIn("Nvidia", reviews[0]['transcript'])
        self.assertIn("Nvidia", reviews[1]['transcript'])
        self.assertNotIn("federal court", reviews[1]['transcript'])
        self.assertLessEqual(reviews[0]['end'], reviews[1]['start'])
        self.assertTrue(any(k['label'].startswith('nvidia') for k in reviews[1]['keywords']))

    def test_unknown_second_article_is_not_invented(self):
        reviews = g.make_summary_reviews([segment("The president and the government announced a new agreement for the company.")])
        self.assertEqual(len(reviews), 1)

    def test_summary_reference_skips_intro_and_stops_at_signoff(self):
        text = "Live from NPR News, I'm Pat Smith. An officer resigned following a relationship with another executive. Pat Smith, NPR News. The president announced tariffs."
        result = g.make_summary_review([segment(text, start=0)])
        self.assertIsNotNone(result)
        self.assertNotIn("Live from", result['transcript'])
        self.assertTrue(any(k['label'].startswith('resigned') for k in result['keywords']))
        self.assertTrue(all(k['terms'] for k in result['keywords']))

    def test_interpretation_context_is_wider_than_playback_and_crosses_segments(self):
        segments = [segment('Earlier context about military negotiations.', 12),
                    segment('They are discussing a new arms -control deal with the government.', 30),
                    segment('Later context explains the nuclear agreement.', 42)]
        context = g.meaning_context(segments, 32, 33)
        self.assertIn('Earlier context', context['text'])
        self.assertIn('Later context', context['text'])
        self.assertLess(context['start'], 30)
        self.assertGreater(context['end'], 40)
        self.assertIn('군비 통제', g.korean_study_clue('new arms', context['text']))

    def test_compound_and_function_word_fragments_are_not_questions(self):
        pairs = g.make_short_items([segment('There is a new arms -control deal and the plan has the support.', 30)])
        answers = {item['answer'] for item in pairs}
        self.assertNotIn('new arms', answers)
        self.assertNotIn('and the', answers)
        self.assertNotIn('has the', answers)

    def test_articles_are_visible_context_but_never_answers(self):
        items = g.make_short_items([
            segment('The flooding from the continued rainfall disrupted a vital road for an entire district.', 30)
        ])
        self.assertTrue(items)
        self.assertIn('the flooding from the', items[0]['sentence'].casefold())
        self.assertTrue(all(not set(item['answer'].split()) & g.ARTICLES for item in items))

    def test_seven_leading_words_can_cross_contiguous_segments(self):
        earlier = segment('The agency said the emergency crews had', 22.2)
        current = segment('staggered arrivals for staff members.', 25)
        item = g.make_short_items([earlier, current])[0]
        self.assertEqual(item['sentence'], 'The agency said the emergency crews had ____ for staff members.')
        self.assertEqual(item['context_start'], 22.2)
        self.assertEqual(g.make_short_items([earlier, segment(current.text, 30)])[0]['context_start'], 30)

    def test_contextual_meanings_do_not_guess_polysemous_fragments(self):
        self.assertIn('장기적', g.korean_study_clue('money over', 'made people money over the long term'))
        self.assertNotIn('장기적', g.korean_study_clue('money over', 'money over here'))
        self.assertIn('소개 표현', g.korean_study_clue('has the', 'NPR reporter has the story.'))
        self.assertIn('대피', g.korean_study_clue('were safely', 'All people were safely evacuated.'))
        self.assertIn('비밀 투표', g.korean_study_clue('secret ballot'))

    def test_short_expression_candidate_has_conservative_shape(self):
        candidates = g.make_short_items([segment()])
        self.assertGreaterEqual(len(candidates), 1)
        item = candidates[0]
        self.assertIn("____", item["sentence"])
        self.assertNotEqual(item["sentence"], "____")
        self.assertLessEqual(item["context_start"], item["start"])
        self.assertGreaterEqual(item["context_end"], item["end"])
        self.assertTrue(item['meaning'])
        self.assertEqual(len(item["answer"].split()), 2)
        self.assertGreaterEqual(item["start"], 25)
        self.assertLess(item["start"], item["end"])

    def test_korean_clue_marks_uncertain_meanings(self):
        self.assertIn('분기 실적', g.korean_study_clue('quarterly earnings'))
        self.assertIn('부분 단서', g.korean_study_clue('unlisted climate'))
        self.assertIn('의미 단서를 확인하지', g.korean_study_clue('unlisted unknown'))

    def test_short_expression_checker_confirms_exact_two_words(self):
        candidates = g.make_short_items([segment()])
        self.assertTrue(candidates)
        first = candidates[0]
        words = TEXT.split()
        pair = first["answer"]
        match_at = next((i for i in range(len(words)-1)
                         if " ".join(words[i:i+2]).casefold() == pair), None)
        self.assertIsNotNone(match_at)
        # The primary timestamps start at 30s. The cropped audio begins at
        # first.start - 2s, so the checker offsets its words by the same amount.
        cropped_start = max(0, first["start"] - 2)
        checker = segment(start=30 - cropped_start)
        model = NS(transcribe=lambda *a, **k: (iter([checker]), None))
        accepted = g.check_short_items([first], [0]*600000, model)
        self.assertEqual(accepted, [first])

    def test_correct_answer_with_wrong_context_is_rejected(self):
        item = g.make_short_items([segment()])[0]
        model = NS(transcribe=lambda *a, **k: (
            iter([segment(TEXT.replace("reviewing", "rejecting"), start=2)]), None))
        diagnostics = []
        with self.assertRaises(RuntimeError):
            g.check_short_items([item], [0]*600000, model, diagnostics)
        self.assertEqual(diagnostics[0]['reason'], 'context_mismatch')

    def test_correct_context_at_another_occurrence_does_not_validate_the_clip(self):
        item = g.make_short_items([segment()])[0]
        clip_start = max(0.0, min(item["start"] - 2, item["context_start"] - .5))
        local_start = item["start"] - clip_start
        misleading = segment(item["answer"] + " in that area.", start=local_start)
        complete = segment(TEXT, start=local_start + 2)
        model = NS(transcribe=lambda *a, **k: (iter([misleading, complete]), None))
        diagnostics = []
        with self.assertRaisesRegex(RuntimeError, "No short expressions"):
            g.check_short_items([item], [0]*600000, model, diagnostics)
        self.assertEqual(diagnostics[0]['reason'], 'phrase_or_time_mismatch')

    def test_unknown_korean_meaning_does_not_make_a_candidate(self):
        text = 'Newcomers casually observe distant islands during unusually mild afternoons.'
        self.assertEqual(g.make_short_items([segment(text, start=30)]), [])

    def test_short_expression_mismatch_does_not_invent_an_answer(self):
        candidates = g.make_short_items([segment()])
        self.assertTrue(candidates)
        model = NS(transcribe=lambda *a, **k: (
            iter([segment(TEXT.replace("local", "regional").replace("authorities", "officials"), start=2)]),
            None))
        with self.assertRaisesRegex(RuntimeError, "No short expressions"):
            g.check_short_items([candidates[0]], [0]*600000, model)

    def test_success_publishes_only_agreed_items_and_success_status(self):
        self.models()
        g.main()
        lesson = json.loads(g.OUTPUT.read_text(encoding='utf-8'))
        self.assertEqual(lesson['episode']['id'], 'new')
        self.assertFalse(lesson['verified'])
        self.assertEqual(lesson['fullTranscript']['transcript'], TEXT)
        self.assertEqual(lesson['fullTranscript']['kind'], 'full_broadcast')
        self.assertIn('____', lesson['items'][0]['sentence'])
        self.assertNotEqual(lesson['items'][0]['sentence'], '____')
        snapshot = self.directory / 'media' / (lesson['episode']['audio_sha256'] + '.mp3')
        self.assertEqual(snapshot.read_bytes(), b'audio')
        self.assertIn(snapshot.name, lesson['episode']['audioURL'])
        self.assertEqual(lesson['episode']['sourceAudioURL'], self.episode['audioURL'])
        self.assertEqual(len(lesson['items'][0]['answer'].split()), 2)
        self.assertLessEqual(len(lesson['items']), 7)
        self.assertEqual(json.loads(g.STATUS.read_text(encoding='utf-8'))['status'], 'success')
        self.assertEqual(json.loads(g.REPORT.read_text(encoding='utf-8'))['checks'][0]['reason'], 'accepted_short_phrase')
        before = g.OUTPUT.read_bytes()
        g.main()
        self.assertEqual(g.OUTPUT.read_bytes(), before)

    def test_second_check_failure_preserves_bytes_and_records_differences(self):
        self.models(checker_text=TEXT.replace('local authorities', 'regional officials'))
        with self.assertRaisesRegex(RuntimeError, 'No short expressions'):
            g.main()
        self.assertEqual(g.OUTPUT.read_bytes(), self.before)
        self.assertEqual(json.loads(g.STATUS.read_text(encoding='utf-8'))['status'], 'failed')
        report = json.loads(g.REPORT.read_text(encoding='utf-8'))
        self.assertEqual(report['checks'][0]['reason'], 'context_mismatch')
        self.assertIn('officials', report['checks'][0]['context_text'])
        self.assertIn('audio_sha256', report)

    def test_no_primary_candidates_preserves_lesson_and_reports_failure(self):
        self.models(primary=segment(avg_logprob=-2))
        with self.assertRaisesRegex(RuntimeError, 'No reliable practice'):
            g.main()
        self.assertEqual(g.OUTPUT.read_bytes(), self.before)
        self.assertEqual(json.loads(g.STATUS.read_text(encoding='utf-8'))['status'], 'failed')


if __name__ == '__main__':
    unittest.main()

