"""Create a short, clearly labeled dictation lesson from NPR News Now audio.

The job uses local speech recognition on a GitHub Actions runner. It keeps the
previous lesson if the feed, audio, or transcription is unavailable.
"""

from __future__ import annotations

import json
import hashlib
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
STATUS = ROOT / "generation-status.json"
REPORT = ROOT / "generation-report.json"
FEED = "https://feeds.npr.org/500005/podcast.xml"
USER_AGENT = "NPR-Morning-Listening/4.0 (+https://github.com/inleem-crypto/npr-morning-listening)"
GENERATOR_VERSION = 9
PRIMARY_MODEL = "small.en"
CHECK_MODEL = "medium.en"
MAX_CANDIDATES = 16
TARGET_ITEMS = 7
SHORT_CANDIDATES = 32
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


def check_items(items: list[dict], audio, model, diagnostics=None) -> list[dict]:
    """Require unprompted model agreement; never replace text with its guess."""
    accepted = []
    for item in items:
        if accepted and item["start"] - accepted[-1]["start"] < 14:
            if diagnostics is not None:
                diagnostics.append({"start": item["start"], "reason": "spacing"})
            continue
        # Context avoids creating an artificial utterance onset at the first word.
        start = max(0, int((item["start"] - 2) * 16000))
        end = min(len(audio), int((item["end"] + 2) * 16000))
        segments, _ = model.transcribe(audio[start:end], language="en", beam_size=5,
                                      temperature=0, word_timestamps=True,
                                      condition_on_previous_text=False)
        segments = list(segments)
        target_words, evidence = [], []
        for segment in segments:
            evidence.extend((w, segment) for w in (segment.words or []))
            words = [w for w in (segment.words or [])
                     if item["start"] - 0.1 <= start / 16000 + (w.start + w.end) / 2
                     <= item["end"] + 0.1]
            if words:
                target_words.extend(words)
        actual = "".join(w.word for w in target_words).strip()
        expected = item["sentence"].replace("____", item["answer"])
        # Match the complete, contiguous sentence in the contextual transcript.
        # Never edit/substitute/drop tokens to improve agreement. Word clocks from
        # separate decodes can drift; bound both edges and require sentence edges.
        tokens = [(token, w, s) for w, s in evidence for token in normalized_words(w.word)]
        wanted = normalized_words(expected)
        matches = []
        text_matches = 0
        for index in range(len(tokens) - len(wanted) + 1):
            chunk = tokens[index:index + len(wanted)]
            if [t[0] for t in chunk] != wanted:
                continue
            text_matches += 1
            first, last = chunk[0][1], chunk[-1][1]
            if (abs(start / 16000 + first.start - item["start"]) > .75
                    or abs(start / 16000 + last.end - item["end"]) > .75):
                continue
            # Do not select a substring inside a word or a longer sentence.
            if (index and (tokens[index-1][1] is first or not re.search(r'[.!?]["”’\x27]?$', tokens[index-1][1].word.strip()))):
                continue
            if index + len(wanted) < len(tokens) and tokens[index + len(wanted)][1] is last:
                continue
            if not re.search(r'[.!?]["”’\x27]?$', last.word.strip()):
                continue
            matches.append(chunk)
        reason = "boundary_mismatch" if text_matches else "text_mismatch"
        selected = matches[0] if len(matches) == 1 else []
        if len(matches) > 1:
            reason = "ambiguous_alignment"
        if selected:
            selected_text = "".join(w.word for w, s in evidence if any(w is t[1] for t in selected)).strip()
            left = next(i for i, s in enumerate(segments) if s is selected[0][2])
            right = next(i for i, s in enumerate(segments) if s is selected[-1][2])
            clocks = all(math.isfinite(w.start) and math.isfinite(w.end)
                         and 0 <= w.start <= w.end for _, w, _ in selected)
            clocks = clocks and all(a[1].start <= b[1].start and b[1].start - a[1].end <= 1.5
                                    for a, b in zip(selected, selected[1:]))
            good = (clocks and all(s.words for s in segments[left:right+1])
                    and all(reliable_segment(s, [w]) for _, w, s in selected))
            if not good:
                reason = "low_confidence"
            elif name_risk(selected_text):
                reason = "name_risk"
            else:
                reason = "accepted" if normalized_words(actual) == wanted else "accepted_boundary_recovery"
        if reason.startswith("accepted"):
            accepted.append(item)
        entry = {"start": item["start"], "end": item["end"], "expected": expected,
                 "legacy_window_text": actual, "context_text": "".join(w.word for w, s in evidence).strip(),
                 "exact_match_count": len(matches), "reason": reason,
                 "context_text_match_count": text_matches,
                 "words": [{"word": w.word, "start": start / 16000 + w.start,
                            "end": start / 16000 + w.end, "probability": w.probability,
                            "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob,
                            "compression_ratio": s.compression_ratio} for w, s in evidence]}
        if diagnostics is not None:
            diagnostics.append(entry)
        print(json.dumps({k: v for k, v in entry.items() if k != "words"}, ensure_ascii=True), flush=True)
        if len(accepted) == TARGET_ITEMS:
            break
    if not accepted:
        raise RuntimeError("No questions passed both transcription quality checks")
    return accepted



# A two-word, audio-grounded alternative to long-sentence dictation.
# Seven answers quote at most 14 English words from the selected episode.
NEWS_HINTS = {
    "president": "대통령", "congress": "의회", "senate": "상원", "house": "하원",
    "government": "정부", "officials": "당국자들", "election": "선거", "voters": "유권자",
    "vote": "투표", "voting": "투표", "policy": "정책", "policies": "정책",
    "bill": "법안", "law": "법률", "court": "법원", "judge": "판사",
    "federal": "연방정부의", "state": "주의", "states": "주들", "national": "국가의",
    "economy": "경제", "economic": "경제의", "market": "시장", "markets": "시장들",
    "prices": "가격", "price": "가격", "inflation": "물가상승", "rates": "금리",
    "jobs": "일자리", "workers": "근로자", "business": "기업", "companies": "기업들",
    "trade": "무역", "tariffs": "관세", "taxes": "세금", "budget": "예산",
    "health": "건강", "medical": "의료의", "hospital": "병원", "patients": "환자들",
    "education": "교육", "school": "학교", "schools": "학교들",
    "war": "전쟁", "peace": "평화", "military": "군의", "security": "안보",
    "troops": "군인들", "attack": "공격", "attacks": "공격들", "conflict": "갈등",
    "foreign": "외국의", "international": "국제적인", "leaders": "지도자들",
    "climate": "기후", "energy": "에너지", "weather": "날씨",
    "technology": "기술", "artificial": "인공의", "intelligence": "지능",
    "data": "데이터", "research": "연구", "scientists": "과학자들",
    "investigation": "조사", "police": "경찰", "charges": "혐의",
    "report": "보도", "reported": "보도했다", "announced": "발표했다",
    "expected": "예상되는", "continued": "계속됐다", "concerns": "우려",
    "support": "지지", "decision": "결정", "changes": "변화", "increase": "증가",
    "decrease": "감소", "major": "주요한", "latest": "최근의",
}
PHRASE_EXCLUDED = {
    "npr", "news", "now", "morning", "evening", "reporter", "reporting",
    "says", "said", "according", "today", "yesterday", "tomorrow",
}


def _phrase_token(word):
    """Reject digits, punctuation-only tokens and capitalized proper-name risks."""
    raw = str(word).strip().strip(".,!?;:()[]{}\"'“”‘’")
    if not re.fullmatch(r"[A-Za-z]{3,14}", raw):
        return ""
    if raw[0].isupper() or raw.casefold() in PHRASE_EXCLUDED:
        return ""
    return raw.casefold()


def make_short_items(segments):
    """Make 2-word prompts from contiguous high-confidence audio, not invented text."""
    result, seen = [], set()
    last = -100.0
    for segment in segments:
        words = getattr(segment, "words", None) or []
        for i in range(len(words) - 1):
            first, second = words[i:i + 2]
            start, end = first.start, second.end
            if (not math.isfinite(start) or not math.isfinite(end)
                    or start < 25 or end <= start or end - start > 2.5
                    or start - last < 7):
                continue
            if (not all(reliable_segment(segment, [w])
                        and w.probability >= .80 and math.isfinite(w.start)
                        and math.isfinite(w.end) and w.end >= w.start
                        for w in (first, second))
                    or second.start - first.end > .65):
                continue
            a, b = _phrase_token(first.word), _phrase_token(second.word)
            if (not a or not b or a == b
                    or (a in STOPWORDS and b in STOPWORDS)):
                continue
            answer = a + " " + b
            if answer in seen:
                continue
            meaning = NEWS_HINTS.get(a) or NEWS_HINTS.get(b)
            hint = ((meaning + " 관련 표현 · ") if meaning else "해당 음성 구간의 표현 · ")
            hint += f"{a[0].upper()}… {b[0].upper()}… (2단어)"
            result.append({
                "sentence": "____",
                "answer": answer,
                "hint": hint,
                "explanation": "두 음성인식 모델이 해당 짧은 표현에 일치한 자동 전사 결과입니다. 원음을 다시 들어 확인하세요.",
                "start": round(start, 2),
                "end": round(end, 2),
            })
            seen.add(answer)
            last = start
            if len(result) >= SHORT_CANDIDATES:
                return result
    return result


def check_short_items(items, audio, model, diagnostics=None):
    """Confirm the same two words, in order, at the same audio location."""
    accepted = []
    for item in items:
        if accepted and item["start"] - accepted[-1]["start"] < 18:
            continue
        clip_start = max(0.0, item["start"] - 2)
        clip_end = min(len(audio) / 16000, item["end"] + 2)
        sample = audio[int(clip_start * 16000):int(clip_end * 16000)]
        segments, _ = model.transcribe(
            sample, language="en", beam_size=5, temperature=0,
            word_timestamps=True, condition_on_previous_text=False)
        segments = list(segments)
        words = [(word, seg) for seg in segments for word in (seg.words or [])]
        expected = normalized_words(item["answer"])
        matched = False
        for i in range(len(words) - len(expected) + 1):
            group = words[i:i + len(expected)]
            observed = [t for w, _ in group for t in normalized_words(w.word)]
            if observed != expected:
                continue
            a, b = group[0][0], group[-1][0]
            if not (math.isfinite(a.start) and math.isfinite(b.end)
                    and abs(clip_start + a.start - item["start"]) <= 1.05
                    and abs(clip_start + b.end - item["end"]) <= 1.05
                    and all(reliable_segment(seg, [w]) and w.probability >= .68
                            for w, seg in group)):
                continue
            if len(group) == 2 and group[1][0].start - group[0][0].end > .9:
                continue
            matched = True
            break
        reason = "accepted_short_phrase" if matched else "phrase_or_time_mismatch"
        if diagnostics is not None:
            diagnostics.append({
                "start": item["start"], "end": item["end"],
                "expected": item["answer"], "reason": reason,
                "context_text": "".join(w.word for w, _ in words).strip(),
            })
        if matched:
            accepted.append(item)
            if len(accepted) >= TARGET_ITEMS:
                break
    if not accepted:
        raise RuntimeError("No short expressions passed both transcription quality checks")
    return accepted



def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def generate(report) -> dict:
    episode = latest_episode()
    report["episode"] = episode
    if OUTPUT.exists():
        try:
            previous = json.loads(OUTPUT.read_text(encoding="utf-8"))
            if previous.get("episode", {}).get("id") == episode["id"] and previous.get("generator_version") == GENERATOR_VERSION:
                print("Latest episode already has a lesson")
                return previous
        except (OSError, ValueError):
            pass

    audio = fetch_bytes(episode["audioURL"], 30_000_000)
    report["audio_sha256"] = hashlib.sha256(audio).hexdigest()
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio

    with tempfile.TemporaryDirectory() as directory:
        audio_path = Path(directory) / "news.mp3"
        audio_path.write_bytes(audio)
        decoded = decode_audio(str(audio_path))
        model = WhisperModel(PRIMARY_MODEL, device="cpu", compute_type="int8", cpu_threads=4)
        segments, _ = model.transcribe(decoded, language="en", beam_size=5, temperature=0,
                                       word_timestamps=True, condition_on_previous_text=False)
        candidates = make_short_items(list(segments))
        report["candidates"] = candidates
        print(f"{len(candidates)} short-expression candidates passed primary audio checks", flush=True)
        if not candidates:
            raise RuntimeError("No reliable practice candidates; previous lesson retained")
        del model
        checker = WhisperModel(CHECK_MODEL, device="cpu", compute_type="int8", cpu_threads=4)
        items = check_short_items(candidates, decoded, checker, report["checks"])
        print(f"{len(items)} short expressions agreed with {CHECK_MODEL}", flush=True)

    lesson = {
        "title": f"{episode['title']} · 자동 딕테이션",
        "kind": "auto_transcribed",
        "generator_version": GENERATOR_VERSION,
        "verified": False,
        "transcription": {"model": PRIMARY_MODEL, "check_model": CHECK_MODEL,
                          "policy": "short-phrase-word-confidence-time-alignment-v3",
                          "human_verified": False},
        "sourceNote": "동일 음원의 짧은 2단어 표현을 두 음성인식 모델이 교차 확인했습니다. 사람 검수 대본은 아니며 단어 오류 가능성이 있습니다. 7개 정답의 총 영어 원문은 최대 14단어입니다.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episode": episode,
        "items": items,
    }
    atomic_json(OUTPUT, lesson)
    print(f"Saved {len(items)} questions for {episode['title']}")
    return lesson


def main() -> None:
    report = {"attempted_at": datetime.now(timezone.utc).isoformat(),
              "generator_version": GENERATOR_VERSION, "checks": []}
    try:
        lesson = generate(report)
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    else:
        report.update(status="success", lesson_episode_id=lesson["episode"]["id"],
                      lesson_generated_at=lesson["generated_at"])
    finally:
        # Separate health metadata: a failed attempt must never mutate lesson.json.
        atomic_json(REPORT, report)
        atomic_json(STATUS, {k: v for k, v in report.items()
                            if k not in {"checks", "candidates", "audio_sha256"}})


if __name__ == "__main__":
    main()
