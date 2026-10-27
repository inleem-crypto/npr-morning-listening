"""Create a short, clearly labeled dictation lesson from NPR News Now audio.

The job uses local speech recognition on a GitHub Actions runner. It keeps the
previous lesson if the feed, audio, or transcription is unavailable.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "lesson.json"
FEED = "https://feeds.npr.org/500005/podcast.xml"
USER_AGENT = "NPR-Morning-Listening/4.0 (+https://github.com/inleem-crypto/npr-morning-listening)"
GENERATOR_VERSION = 6
PRIMARY_MODEL = "small.en"
CHECK_MODEL = "medium.en"
MAX_CANDIDATES = 12
STOPWORDS = {
    "about", "after", "again", "against", "along", "among", "because", "before",
    "being", "could", "every", "first", "going", "great", "other", "people",
    "really", "right", "should", "their", "there", "these", "those", "today",
    "under", "until", "where", "which", "while", "would",
}
WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def fetch_bytes(url: str, limit: int) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=45) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("Podcast download exceeded the size limit")
    return data


def latest_episode() -> dict:
    root = ElementTree.fromstring(fetch_bytes(FEED, 4_000_000))
    candidates = []
    for item in root.findall("./channel/item"):
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url", "") if enclosure is not None else ""
        if not audio_url.startswith("https://"):
            continue
        date_text = item.findtext("pubDate", "")
        try:
            published = parsedate_to_datetime(date_text).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        candidates.append({
            "id": (item.findtext("guid") or audio_url).strip()[:250],
            "title": (item.findtext("title") or "NPR News Now").strip(),
            "audioURL": audio_url,
            "date": published.isoformat(),
            "link": (item.findtext("link") or "https://www.npr.org/podcasts/500005/npr-news-now").strip(),
            "via": "NPR 공식 RSS",
        })
    if not candidates:
        raise RuntimeError("No audio enclosure found in NPR feed")
    return max(candidates, key=lambda item: item["date"])


def reliable_segment(segment, selected_words=None) -> bool:
    """Scores are heuristics, not calibrated probabilities of correctness."""
    limits = (("avg_logprob", -0.5, 0), ("no_speech_prob", 0, 0.3),
              ("compression_ratio", 0, 2.4))
    for field, low, high in limits:
        value = getattr(segment, field, None)
        if value is None or not math.isfinite(value) or not low <= value <= high:
            return False
    words = getattr(segment, "words", None) if selected_words is None else selected_words
    return bool(words) and all(
        math.isfinite(w.probability) and w.probability >= 0.65 for w in words)


def normalized_words(text: str) -> list[str]:
    return [w.casefold() for w in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+(?:[.,]\d+)*", text)]


def name_risk(sentence: str) -> bool:
    """Conservative heuristic; cannot detect every lowercased/misheard name."""
    words = WORD.findall(sentence)
    return (any(w[0].isupper() and w != "I" for w in words[1:])
            or bool(re.search(r"\b(?:reports|reporting)\b", sentence, re.I)))


def make_items(segments: list) -> list[dict]:
    """Use word boundaries; never join across a rejected or missing segment."""
    items, pending, seen = [], [], set()
    tainted = False
    previous_end = None
    for segment in segments:
        words = getattr(segment, "words", None)
        if not words:
            pending.clear()
            tainted = True
            continue
        if pending and previous_end is not None and segment.start - previous_end > 1.5:
            pending.clear()
            tainted = True
        previous_end = segment.end
        for word in words:
            pending.append(word)
            tainted = tainted or not reliable_segment(segment, [word]) or word.start < 25
            sentence = "".join(w.word for w in pending).strip()
            boundary = bool(re.search(r'[.!?]["\u201d\u2019\x27]?$', word.word.strip()))
            if len(sentence) > 210:
                tainted = True
            if not boundary:
                continue
            utterance = pending
            rejected = tainted
            pending, tainted = [], False
            tokens = list(WORD.finditer(sentence))
            if (rejected or not 9 <= len(tokens) <= 24
                    or not 2 <= utterance[-1].end - utterance[0].start <= 18
                    or not sentence.lstrip('"\u201c\u2018')[0].isupper()
                    or name_risk(sentence) or sentence.casefold() in seen):
                continue
            options = [m for m in tokens[2:-2] if 5 <= len(m.group()) <= 13
                       and m.group().casefold() not in STOPWORDS and m.group()[0].islower()]
            if not options:
                continue
            match = min(options, key=lambda m: abs(m.start() - len(sentence) / 2))
            # All occurrences of the answer must have strong word evidence.
            answer_words = [w for w in utterance if normalized_words(w.word) == [match.group().casefold()]]
            if not answer_words or min(w.probability for w in answer_words) < 0.85:
                continue
            answer = match.group()
            items.append({"sentence": sentence[:match.start()] + "____" + sentence[match.end():],
                          "answer": answer, "hint": f"첫 글자 {answer[0].upper()} · {len(answer)}글자",
                          "explanation": "자동 음성 전사 결과입니다. 실제 음성을 다시 들어 확인해 주세요.",
                          "start": round(utterance[0].start, 2), "end": round(utterance[-1].end, 2)})
            seen.add(sentence.casefold())
            if len(items) >= MAX_CANDIDATES:
                break
        if len(items) >= MAX_CANDIDATES:
            break
    return items


def check_items(items: list[dict], audio, model) -> list[dict]:
    """Require unprompted model agreement; never replace text with its guess."""
    accepted = []
    for item in items:
        if accepted and item["start"] - accepted[-1]["start"] < 14:
            continue
        # Context avoids creating an artificial utterance onset at the first word.
        start = max(0, int((item["start"] - 2) * 16000))
        end = min(len(audio), int((item["end"] + 2) * 16000))
        segments, _ = model.transcribe(audio[start:end], language="en", beam_size=5,
                                      temperature=0, word_timestamps=True,
                                      condition_on_previous_text=False)
        segments = list(segments)
        target_words = []
        good = True
        for segment in segments:
            words = [w for w in (segment.words or [])
                     if item["start"] - 0.1 <= start / 16000 + (w.start + w.end) / 2
                     <= item["end"] + 0.1]
            if words:
                target_words.extend(words)
                good = good and reliable_segment(segment, words)
        actual = "".join(w.word for w in target_words).strip()
        expected = item["sentence"].replace("____", item["answer"])
        if (target_words and good
                and not name_risk(actual)
                and normalized_words(actual) == normalized_words(expected)):
            accepted.append(item)
        if len(accepted) == 5:
            break
    if not accepted:
        raise RuntimeError("No questions passed both transcription quality checks")
    return accepted


def main() -> None:
    episode = latest_episode()
    if OUTPUT.exists():
        try:
            previous = json.loads(OUTPUT.read_text(encoding="utf-8"))
            if previous.get("episode", {}).get("id") == episode["id"] and previous.get("generator_version") == GENERATOR_VERSION:
                print("Latest episode already has a lesson")
                return
        except (OSError, ValueError):
            pass

    audio = fetch_bytes(episode["audioURL"], 30_000_000)
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio

    with tempfile.TemporaryDirectory() as directory:
        audio_path = Path(directory) / "news.mp3"
        audio_path.write_bytes(audio)
        decoded = decode_audio(str(audio_path))
        model = WhisperModel(PRIMARY_MODEL, device="cpu", compute_type="int8", cpu_threads=4)
        segments, _ = model.transcribe(decoded, language="en", beam_size=5, temperature=0,
                                       word_timestamps=True, condition_on_previous_text=False)
        candidates = make_items(list(segments))
        print(f"{len(candidates)} candidates passed word confidence and name checks", flush=True)
        if not candidates:
            raise RuntimeError("No reliable practice candidates; previous lesson retained")
        del model
        checker = WhisperModel(CHECK_MODEL, device="cpu", compute_type="int8", cpu_threads=4)
        items = check_items(candidates, decoded, checker)
        print(f"{len(items)} candidates agreed with {CHECK_MODEL}", flush=True)

    lesson = {
        "title": f"{episode['title']} · 자동 딕테이션",
        "kind": "auto_transcribed",
        "generator_version": GENERATOR_VERSION,
        "verified": False,
        "transcription": {"model": PRIMARY_MODEL, "check_model": CHECK_MODEL,
                          "policy": "word-confidence-name-exclusion-model-agreement-v1",
                          "human_verified": False},
        "sourceNote": "NPR 음원을 자동 전사해 만든 문제입니다. 전사와 정답은 검증되지 않았습니다.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episode": episode,
        "items": items,
    }
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(lesson, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)
    print(f"Saved {len(items)} questions for {episode['title']}")


if __name__ == "__main__":
    main()
