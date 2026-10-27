import copy
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


if __name__ == '__main__':
    unittest.main()
