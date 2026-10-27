"""Measure production candidate checks against an existing local audio audit."""
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from generate_lesson import make_items, check_items, CHECK_MODEL


def main():
    directory = Path('.audit')
    report = json.loads((directory / 'comparison.json').read_text(encoding='utf-8'))
    run = next(r for r in report['runs'] if r['model'] == 'small.en')
    segments = [NS(**{**s, 'words': [NS(**w) for w in s['words']]}) for s in run['segments']]
    candidates = make_items(segments)
    audio = decode_audio(str(directory / 'episode.mp3'))
    started = time.perf_counter()
    model = WhisperModel(CHECK_MODEL, device='cpu', compute_type='int8', cpu_threads=4)
    loaded = time.perf_counter()
    items = check_items(candidates, audio, model)
    result = {'candidate_count': len(candidates), 'accepted_count': len(items),
              'load_seconds': round(loaded-started, 2),
              'check_seconds': round(time.perf_counter()-loaded, 2), 'items': items}
    (directory / 'checked-items.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
