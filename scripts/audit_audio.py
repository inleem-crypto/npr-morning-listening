"""Reproduce ASR comparisons without changing a lesson or claiming human review."""
import argparse
import hashlib
import json
import time
from pathlib import Path

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from generate_lesson import fetch_bytes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lesson', default='lesson.json')
    parser.add_argument('--models', nargs='+', default=['small.en', 'medium.en'])
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    lesson = json.loads(Path(args.lesson).read_text(encoding='utf-8'))
    directory = Path('.audit')
    directory.mkdir(exist_ok=True)
    audio_path = directory / 'episode.mp3'
    previous_path = directory / 'comparison.json'
    if audio_path.exists():
        if not previous_path.exists():
            raise RuntimeError('Cached audio has no provenance; use a fresh .audit directory')
        previous = json.loads(previous_path.read_text(encoding='utf-8'))
        if previous['episode']['audioURL'] != lesson['episode']['audioURL']:
            raise RuntimeError('Cached audio belongs to another episode; use a fresh .audit directory')
    if not audio_path.exists():
        audio_path.write_bytes(fetch_bytes(lesson['episode']['audioURL'], 30_000_000))
    audio = decode_audio(str(audio_path))
    report = {'episode': lesson['episode'], 'audio_sha256': hashlib.sha256(audio_path.read_bytes()).hexdigest(),
              'human_verified': False, 'runs': []}
    if args.resume and (directory / 'comparison.json').exists():
        previous = json.loads((directory / 'comparison.json').read_text(encoding='utf-8'))
        if previous['episode']['id'] != lesson['episode']['id']:
            raise RuntimeError('Audit belongs to another episode')
        report['runs'] = previous['runs']
    for name in args.models:
        if any(run['model'] == name for run in report['runs']):
            continue
        started = time.perf_counter()
        model = WhisperModel(name, device='cpu', compute_type='int8', cpu_threads=4)
        loaded = time.perf_counter()
        # No name prompts: evidence must not be forced toward the proposed spelling.
        segments, _ = model.transcribe(audio, language='en', beam_size=5,
                                       temperature=0, word_timestamps=True,
                                       condition_on_previous_text=False)
        segments = list(segments)
        run = {'model': name, 'load_seconds': round(loaded-started, 2),
               'transcribe_seconds': round(time.perf_counter()-loaded, 2),
               'segments': []}
        from dataclasses import asdict
        run['segments'] = [asdict(s) for s in segments]
        report['runs'].append(run)
        (directory / 'comparison.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(name, run['load_seconds'], run['transcribe_seconds'], flush=True)
        for s in segments:
            if 108 < s.end < 130 or 222 < s.end < 245:
                print(s.start, s.end, s.text, s.avg_logprob, flush=True)
        del model


if __name__ == '__main__':
    main()
