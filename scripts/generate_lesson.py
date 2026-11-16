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
GENERATOR_VERSION = 13
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



# Conservative Korean study clues. An unknown expression is not translated by
# guessing: a single-word clue is labeled explicitly as a partial clue.
PHRASE_GLOSSES = {
    "ground stop": "항공기의 이륙을 일시적으로 전면 중단하는 조치",
    "quarterly earnings": "기업의 분기 실적",
    "interest rates": "금리",
    "federal government": "미국 연방정부",
    "climate change": "기후변화",
    "peace talks": "평화협상",
    "election results": "선거 결과",
    "voter turnout": "투표율 또는 유권자 투표 참여",
    "national security": "국가 안보",
    "state officials": "주정부 당국자들",
    "public health": "공중보건",
    "economic growth": "경제성장",
    "budget cuts": "예산 삭감",
    "legal challenges": "법적 이의제기 또는 법적 분쟁",
    "new restrictions": "새로운 제한 조치",
    "restrictions remain": "제한 조치가 계속 유지되다",
    "various types": "여러 종류",
    "there was": "~이 있었다",
    "those conversions": "그러한 전환·변환 (대상은 문맥 확인)",
    "like leaving": "떠나는 것처럼 또는 떠나는 것과 같은 (문맥 확인)",
    "press conference": "기자회견",
    "supreme court": "대법원",
    "senate vote": "상원 표결",
    "white house": "미국 백악관",
    "stock market": "주식시장",
    "local authorities": "지역 당국자들",
    "final decision": "최종 결정",
    "health care": "의료 서비스",
    "new policy": "새로운 정책",
    "supply chain": "공급망",
    "natural disasters": "자연재해",
    "air traffic": "항공 교통",
    "flight delays": "항공편 지연",
    "government shutdown": "정부 업무 중단",
    "court ruling": "법원 판결",
    "allegedly had": "~을 가지고 있었다고 주장되거나 전해졌다",
    "financial officer": "재무 담당 임원",
    "the confidence": "그 신뢰·믿음",
    "touts the": "그 ~을 적극 홍보한다 (뒤의 명사와 연결)",
    "federal judge": "연방 판사",
    "assisting with": "~을 돕거나 지원하는 중인",
    "taking place": "열리거나 진행되는",
    "had expected": "예상했었다",
    "public statement": "공개 성명",
    "voyage data": "항해 자료",
}
WORD_GLOSSES = {
    **NEWS_HINTS,
    "ground": "지상", "stop": "중단", "quarterly": "분기별의",
    "earnings": "기업 실적", "conversions": "전환·변환", "restrictions": "제한 조치",
    "remain": "남아 있다", "various": "여러 가지의", "types": "종류",
    "local": "지역의", "authorities": "당국자들", "final": "최종의",
    "decision": "결정", "airport": "공항", "airports": "공항들",
    "flight": "항공편", "flights": "항공편들", "delays": "지연",
    "cancelled": "취소되었다", "cancel": "취소하다", "payments": "지급액",
    "restrictions": "제한 조치", "increase": "증가", "increases": "증가",
    "decline": "감소", "declined": "감소했다", "rising": "상승하는",
    "falling": "하락하는", "growth": "성장", "quarterly": "분기별",
    "earnings": "실적", "report": "보도", "reports": "보도들",
    "decisions": "결정들", "announced": "발표했다",
    "talks": "협상", "agreement": "합의", "agreements": "합의들",
    "proposal": "제안", "proposals": "제안들", "reviewing": "검토하는",
    "investigation": "조사", "investigators": "조사관들",
    "review": "검토", "results": "결과", "restrictions": "제한",
    "available": "이용 가능한", "services": "서비스",
    "changes": "변화", "system": "시스템", "systems": "시스템들",
    "public": "공공의", "private": "민간의", "new": "새로운",
    "current": "현재의", "previous": "이전의", "future": "미래의",
    "official": "공식의", "statement": "성명", "deadline": "마감 시한",
    "evidence": "증거", "cause": "원인", "impact": "영향",
    "support": "지지·지원", "needed": "필요했다",
    "temporary": "일시적인", "permanent": "영구적인",
    "months": "개월", "weeks": "주", "years": "년", "days": "일",
}

def korean_study_clue(answer: str) -> str:
    """Offer an exact curated Korean gloss, or honestly label a partial clue."""
    answer = answer.casefold().strip()
    if answer in PHRASE_GLOSSES:
        return PHRASE_GLOSSES[answer]
    words = answer.split()
    known = [(word, WORD_GLOSSES[word]) for word in words if word in WORD_GLOSSES]
    if len(known) == len(words):
        return "핵심 단어 뜻: " + ", ".join(f"{word}={meaning}" for word, meaning in known) + " (전체 뜻은 문맥 확인)"
    if known:
        return "부분 단서: " + ", ".join(f"{word}={meaning}" for word, meaning in known) + " (나머지는 듣고 완성)"
    return "의미 단서를 확인하지 못했습니다. 지정된 짧은 음성을 듣고 두 단어를 적으세요."


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
            # Preserve audible context instead of replacing the entire prompt
            # with a bare blank. Never synthesize a sentence around the answer.
            left, right = max(0, i - 4), min(len(words), i + 6)
            context = words[left:right]
            before = "".join(w.word for w in words[left:i]).strip()
            after = "".join(w.word for w in words[i + 2:right]).strip()
            sentence = (before + " ____ " + after).strip()
            meaning = NEWS_HINTS.get(a) or NEWS_HINTS.get(b)
            hint = ((meaning + " 관련 표현 · ") if meaning else "해당 음성 구간의 표현 · ")
            hint += f"{a[0].upper()}… {b[0].upper()}… (2단어)"
            result.append({
                "sentence": sentence,
                "context_start": round(context[0].start, 2),
                "context_end": round(context[-1].end, 2),
                "answer": answer,
                "meaning": korean_study_clue(answer),
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
        clip_start = max(0.0, min(item["start"] - 2, item.get("context_start", item["start"]) - .5))
        clip_end = min(len(audio) / 16000, max(item["end"] + 2, item.get("context_end", item["end"]) + .5))
        sample = audio[int(clip_start * 16000):int(clip_end * 16000)]
        segments, _ = model.transcribe(
            sample, language="en", beam_size=5, temperature=0,
            word_timestamps=True, condition_on_previous_text=False)
        segments = list(segments)
        words = [(word, seg) for seg in segments for word in (seg.words or [])]
        expected = normalized_words(item["answer"])
        # The displayed context must also agree. A correct isolated answer is
        # insufficient if the surrounding prompt says something else.
        context_expected = normalized_words(item["sentence"].replace("____", item["answer"]))
        observed_context = [t for w, _ in words for t in normalized_words(w.word)]
        context_matches = any(observed_context[j:j + len(context_expected)] == context_expected
                              for j in range(len(observed_context) - len(context_expected) + 1))
        matched = False
        if not context_matches:
            if diagnostics is not None:
                diagnostics.append({"start": item["start"], "reason": "context_mismatch",
                                    "expected": item["sentence"].replace("____", item["answer"]),
                                    "context_text": "".join(w.word for w, _ in words).strip()})
            continue
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



# Bilingual aliases are deliberately explicit: this is a keyword rubric,
# not an LLM-based judgment of a learner's meaning or factual accuracy.
SUMMARY_TERMS = {
    "trump": ["trump", "트럼프"],
    "asia": ["asia", "asian", "아시아"],
    "summit": ["summit", "summits", "정상회담", "정상 회담", "정상회의", "정상 회의"],
    "china": ["china", "chinese", "중국"],
    "russia": ["russia", "russian", "러시아"],
    "nuclear": ["nuclear", "arms", "핵", "군축", "군비"],
    "trade": ["trade", "무역"],
    "ukraine": ["ukraine", "우크라이나"],
    "taiwan": ["taiwan", "대만"],

    "nvidia": ["nvidia", "엔비디아"],
    "petition": ["petition", "signatures", "청원", "서명"],
    "return": ["return", "returning", "복귀", "돌아"],
    "support": ["support", "지지", "지원"],
    "resigned": ["resign", "resigned", "resignation", "사임", "사퇴"],
    "relationship": ["relationship", "affair", "관계", "불륜"],
    "financial": ["financial", "finance", "재무", "금융"],
    "officer": ["officer", "executive", "임원", "책임자"],
    "confidence": ["confidence", "trust", "신뢰", "믿음"],
    "president": ["president", "대통령"],
    "government": ["government", "정부"],
    "election": ["election", "선거"],
    "court": ["court", "judge", "법원", "판사"],
    "military": ["military", "troops", "군사", "군대", "군인"],
    "agreement": ["agreement", "deal", "deals", "합의", "협정", "거래", "계약"],
    "company": ["company", "companies", "기업", "회사"],
    "investigation": ["investigation", "investigating", "조사", "수사"],
    "security": ["security", "안보", "보안"],
    "police": ["police", "경찰"],
    "killed": ["killed", "dead", "death", "deaths", "사망", "숨졌", "사살"],
    "prices": ["prices", "price", "물가", "가격"],
    "inflation": ["inflation", "인플레이션", "물가상승"],
    "health": ["health", "건강", "보건"],
    "school": ["school", "schools", "학교"],
    "climate": ["climate", "기후"],
    "attack": ["attack", "attacks", "공격", "공습"],
    "tariffs": ["tariffs", "tariff", "관세"],
    "shutdown": ["shutdown", "셧다운", "업무중단", "업무 중단"],
}

def summary_keywords(transcript):
    tokens = normalized_words(transcript)
    result = []
    for label, aliases in SUMMARY_TERMS.items():
        if any(t in tokens for t in aliases if re.fullmatch('[a-z]+', t)):
            korean = next((a for a in aliases if re.search('[가-힣]', a)), '')
            result.append({"label": label + (" · " + korean if korean else ''), "terms": aliases})
    return result[:6]


def make_summary_reviews(segments):
    """Separate stories at reporter handoffs, preserving contiguous timed words.

    Unknown story boundaries are left unavailable rather than labeling an
    arbitrary second time window as the second article.
    """
    words = [w for s in segments for w in (getattr(s, "words", None) or [])
             if math.isfinite(w.start) and math.isfinite(w.end) and w.end > w.start]
    if not words:
        return []
    opening_words = [w for w in words if w.start < 70]
    opening = "".join(w.word for w in opening_words)
    intro = re.search(r"(?:Live\s+)?from NPR News.*?(?:I'm|I am) [^.!?]{1,65}[.!?]", opening, re.I)
    start_index = 0
    if intro:
        position = 0
        for i, w in enumerate(opening_words):
            position += len(w.word)
            if position >= intro.end():
                start_index = i + 1
                break
    words = words[start_index:]
    sentences, pending = [], []
    for w in words:
        pending.append(w)
        token = w.word.strip().rstrip('"”’')
        abbreviation = bool(re.fullmatch(r"(?:[A-Z]\.){1,4}|(?:Mr|Mrs|Ms|Dr|St)\.", token))
        if re.search(r'[.!?]$', token) and not abbreviation:
            sentences.append(pending)
            pending = []
    if pending:
        sentences.append(pending)
    if not sentences:
        return []
    text = lambda group: "".join(w.word for w in group).strip()
    reporter = re.compile(r"\b(?:NPR|[A-Za-z]+ [VP]R)['’]?s?\s+[A-Z][A-Za-z.' -]{2,75}?\b(?:reports|explains|has (?:this|the) story)\b", re.I)
    starts = [0]
    for i, sentence in enumerate(sentences):
        content = text(sentence)
        match = reporter.search(content)
        if not match:
            continue
        # A stand-alone reporter credit belongs to the preceding headline.
        before = normalized_words(content[:match.start()])
        candidate = i - 1 if i and len(before) <= 2 else i
        if candidate and re.search(r'NPR News[.!?]?$', text(sentences[candidate]), re.I):
            candidate = i
        if (candidate > starts[-1] and
                sentences[candidate][0].start - sentences[starts[-1]][0].start >= 18):
            starts.append(candidate)
    # A reporter sign-off can establish a next-story boundary without another
    # explicit handoff, e.g. a short anchor-read second story.
    for i, sentence in enumerate(sentences[:-1]):
        if re.search(r'NPR News[.!?]?$', text(sentence), re.I):
            next_index = i + 1
            if all(abs(sentences[next_index][0].start - sentences[j][0].start) >= 12 for j in starts):
                starts.append(next_index)
    starts.sort()
    reviews = []
    for ordinal, begin in enumerate(starts[:2]):
        stop = starts[ordinal+1] if ordinal+1 < len(starts) else len(sentences)
        selected = []
        for sentence in sentences[begin:stop]:
            if sentence[-1].end - sentences[begin][0].start > 100:
                break
            selected.extend(sentence)
            if (selected[-1].end - selected[0].start > 15 and
                    re.search(r'NPR News[.!?]?$', text(sentence), re.I)):
                break
        transcript = text(selected)
        if len(normalized_words(transcript)) < 12:
            break
        reviews.append({"kind": "keyword_rubric", "article": ordinal+1,
                        "start": round(selected[0].start, 2), "end": round(selected[-1].end, 2),
                        "transcript": transcript, "keywords": summary_keywords(transcript),
                        "boundary": "reporter_handoff_estimate"})
    return reviews


def make_summary_review(segments):
    reviews = make_summary_reviews(segments)
    return reviews[0] if reviews else None


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
        primary_segments = list(segments)
        summary_reviews = make_summary_reviews(primary_segments)
        summary_review = summary_reviews[0] if summary_reviews else None
        candidates = make_short_items(primary_segments)
        report["candidates"] = candidates
        print(f"{len(candidates)} short-expression candidates passed primary audio checks", flush=True)
        if not candidates:
            raise RuntimeError("No reliable practice candidates; previous lesson retained")
        del model
        checker = WhisperModel(CHECK_MODEL, device="cpu", compute_type="int8", cpu_threads=4)
        items = check_short_items(candidates, decoded, checker, report["checks"])
        print(f"{len(items)} short expressions agreed with {CHECK_MODEL}", flush=True)

    # Publish the exact bytes used by BOTH recognizers. Podcast redirects can
    # insert different ads on another device, invalidating all word timestamps.
    digest = report["audio_sha256"]
    media = ROOT / "media"
    media.mkdir(exist_ok=True)
    snapshot = media / (digest + ".mp3")
    snapshot.write_bytes(audio)
    source_url = episode["audioURL"]
    episode = {**episode, "sourceAudioURL": source_url,
               "audioURL": "https://inleem-crypto.github.io/npr-morning-listening/media/" + snapshot.name,
               "audio_sha256": digest, "via": "문제 생성에 사용한 고정 음원"}
    lesson = {
        "title": f"{episode['title']} · 자동 딕테이션",
        "kind": "auto_transcribed",
        "generator_version": GENERATOR_VERSION,
        "verified": False,
        "transcription": {"model": PRIMARY_MODEL, "check_model": CHECK_MODEL,
                          "policy": "frozen-audio-context-agreement-v4",
                          "human_verified": False},
        "sourceNote": "문제를 만들 때 사용한 고정 음원을 재생합니다. 빈칸 앞뒤 영어 지문과 2단어 정답을 두 음성인식 모델이 비교했습니다. 자동 전사이므로 오류가 있을 수 있습니다.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episode": episode,
        "items": items,
        "summaryReview": summary_review,
        "summaryReviews": summary_reviews,
    }
    atomic_json(OUTPUT, lesson)
    for old in media.glob("*.mp3"):
        if old != snapshot:
            old.unlink()
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
