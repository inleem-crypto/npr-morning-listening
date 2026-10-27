"""Create a short, clearly labeled dictation lesson from NPR News Now audio.

The job uses local speech recognition on a GitHub Actions runner. It keeps the
previous lesson if the feed, audio, or transcription is unavailable.
"""

from __future__ import annotations

import json
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
GENERATOR_VERSION = 4
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


def make_items(segments: list) -> list[dict]:
    """Choose complete, short utterances with one answerable word each."""
    items = []
    last_start = -100.0
    seen = set()
    pending = []
    pending_start = 0.0
    for segment in segments:
        start = float(segment.start)
        end = float(segment.end)
        if start < 25 or end - start < 1:
            continue
        if getattr(segment, "no_speech_prob", 0) > 0.6 or getattr(segment, "avg_logprob", 0) < -1:
            pending.clear()
            continue
        part = " ".join(str(segment.text).strip().split())
        if not part:
            continue
        if not pending:
            pending_start = start
        pending.append(part)
        sentence = " ".join(pending)
        if len(sentence) > 210 or len(WORD.findall(sentence)) > 30:
            pending.clear()
            continue
        if not re.search(r"[.!?][\"']?$", part):
            continue
        pending.clear()
        if pending_start - last_start < 14:
            continue
        sentences = re.split(r"(?<=[.!?])\s+", sentence)
        complete = [part.strip(" —") for part in sentences if 9 <= len(WORD.findall(part)) <= 24]
        if not complete:
            continue
        sentence = max(complete, key=lambda part: len(WORD.findall(part)))
        words = list(WORD.finditer(sentence))
        if len(words) < 9 or len(words) > 24 or end - pending_start < 2:
            continue
        if re.search(r"\b(?:NPR|PR news)\b", sentence, re.IGNORECASE):
            continue
        key = sentence.casefold()
        if key in seen:
            continue
        options = [
            match for match in words[2:-2]
            if 5 <= len(match.group()) <= 13
            and match.group().casefold() not in STOPWORDS
            and match.group()[0].islower()
        ]
        if not options:
            continue
        middle = len(sentence) / 2
        answer_match = min(options, key=lambda match: abs(match.start() - middle))
        answer = answer_match.group()
        blanked = sentence[: answer_match.start()] + "____" + sentence[answer_match.end() :]
        items.append({
            "sentence": blanked,
            "answer": answer,
            "hint": f"첫 글자 {answer[0].upper()} · {len(answer)}글자",
            "explanation": "자동 음성 전사 결과입니다. 실제 음성을 다시 들어 확인해 주세요.",
            "start": round(pending_start, 2),
            "end": round(end, 2),
        })
        seen.add(key)
        last_start = pending_start
        if len(items) == 5:
            break
    if len(items) < 3:
        raise RuntimeError("Transcription did not yield enough reliable practice segments")
    return items


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

    with tempfile.TemporaryDirectory() as directory:
        audio_path = Path(directory) / "news.mp3"
        audio_path.write_bytes(audio)
        model = WhisperModel("small.en", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(audio_path), language="en", beam_size=5, condition_on_previous_text=False)
        items = make_items(list(segments))

    lesson = {
        "title": f"{episode['title']} · 자동 딕테이션",
        "kind": "auto_transcribed",
        "generator_version": GENERATOR_VERSION,
        "verified": False,
        "sourceNote": "NPR 음원을 자동 전사해 만든 문제입니다. 전사와 정답은 검증되지 않았습니다.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episode": episode,
        "items": items,
    }
    OUTPUT.write_text(json.dumps(lesson, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(items)} questions for {episode['title']}")


if __name__ == "__main__":
    main()
