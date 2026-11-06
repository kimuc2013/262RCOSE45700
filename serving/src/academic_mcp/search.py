"""Small-corpus BM25 baseline. Korean character bigrams need no downloaded model."""
import math
import re
import unicodedata
from collections import Counter


def normalize(text: str):
    # Retain digits, negation and action words. Do not strip Korean endings heuristically.
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", text).lower())


def tokenize(text: str):
    result = []
    for word in re.findall(r"[가-힣]+|[a-z0-9]+", unicodedata.normalize("NFKC", text).lower()):
        if re.fullmatch(r"[가-힣]+", word) and len(word) > 1:
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
    return result


def bm25(query: str, chunks: list[dict]):
    if not chunks:
        return []
    bags = [Counter(tokenize(c['doc_title'] + ' ' + c['section'] + ' ' + c['text'])) for c in chunks]
    lengths = [sum(b.values()) for b in bags]
    avg = sum(lengths) / len(lengths)
    scores = []
    for i, bag in enumerate(bags):
        score = 0.0
        for token in set(tokenize(query)):
            count = bag[token]
            if count:
                df = sum(token in b for b in bags)
                idf = math.log(1 + (len(bags) - df + 0.5) / (df + 0.5))
                score += idf * count * 2.5 / (count + 1.5 * (0.25 + 0.75 * lengths[i] / max(avg, 1)))
        if score > 0:
            scores.append((i, score))
    return sorted(scores, key=lambda row: (-row[1], row[0]))


def cosine(a: list[float], b: list[float]):
    if len(a) != len(b) or not a:
        raise ValueError("Inconsistent embedding dimensions")
    denom = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / denom if denom else 0.0


def fuse(lexical, dense):
    """Reciprocal rank fusion avoids pretending BM25 and cosine share a scale."""
    totals = Counter()
    for ranking in (lexical, dense):
        for rank, (index, _) in enumerate(ranking, 1):
            totals[index] += 1 / (60 + rank)
    return sorted(totals.items(), key=lambda row: (-row[1], row[0]))
