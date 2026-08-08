# SPDX-License-Identifier: AGPL-3.0-or-later
"""redirecall.textutil — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import re
from . import embeddings

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEXT CHUNKING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Sentence boundary: period / ! / ? followed by whitespace (lookbehind so
# the punctuation stays attached to the preceding sentence).
_SENT_END = re.compile(r'(?<=[.!?])\s+')

# English stop-words excluded from BM25 keyword extraction
_STOPWORDS = frozenset({
    "a","an","the","is","it","in","on","at","to","of","for","and","or",
    "but","not","with","from","by","as","be","are","was","were","been",
    "has","have","had","do","does","did","will","would","can","could",
    "should","may","might","this","that","these","those","i","you","he",
    "she","we","they","my","your","his","her","our","their","what","which",
    "who","how","when","where","why","all","any","each","if","then","than",
    "so","no","yes","also","just","about","into","up","out","more","some",
    "use","using","like","get","set","see","make","know","want","need",
})


def _keywords_for_bm25(query: str) -> list[str]:
    """Extract significant words for BM25/text search — drop stop-words and single characters.

    Two-character tokens are KEPT. They are exactly the identifiers the lexical
    leg exists to catch — error codes (``E7``), part numbers (``R2``), model
    designators (``V8``, ``3M``) — and an embedding has no way to rank them, so
    dropping them left the answer unreachable at any cosine threshold. English
    two-letter noise (``is``, ``of``, ``on``, …) is already covered by
    _STOPWORDS, and BM25's IDF term demotes whatever slips through.

    Single characters are still dropped: ``\\w+`` splits a hyphenated identifier
    ("F-16" → "f", "16"), and the orphaned letter matches everything while
    identifying nothing.
    """
    return [w for w in re.findall(r'\w+', query.lower())
            if len(w) > 1 and w not in _STOPWORDS]


# A chunk may never exceed this multiple of the configured size. Content with no
# sentence punctuation — CSV/XLSX rows, markdown tables, code — otherwise lands in
# the "single oversized sentence" branch below and becomes one unbounded chunk
# whose embedding represents only its first ~256 tokens.
_CHUNK_HARD_CAP_FACTOR = 2


def count_tokens(text: str) -> int:
    """Tokens the active embedding model will actually consume for ``text``.

    A word-count heuristic (words × 1.3) is close enough for English prose but
    wrong by more than an order of magnitude for the content that most needs
    splitting: CSV rows, code, URLs and JSON tokenise into many sub-word pieces,
    so a 195-word CSV chunk can be ~8 000 tokens. Falls back to the heuristic only
    if the tokenizer is unavailable.
    """
    try:
        tok = getattr(embeddings.get_embed_model(), "tokenizer", None)
        if tok is not None:
            # WordPiece maps any run longer than _MAX_CHARS_PER_WORD to a single
            # [UNK], so a 200 KB base64 blob counted as 3 tokens. Break the runs
            # first or the count is meaningless for exactly the input that needs
            # splitting most.
            return len(tok.encode(_break_long_runs(text), add_special_tokens=True))
    except Exception:
        pass
    return int(len(text.split()) * 1.3) + 2


def _model_token_limit(default: int = 256) -> int:
    """Max tokens the active embedding model encodes before truncating."""
    try:
        return int(getattr(embeddings.get_embed_model(), "max_seq_length", 0) or 0) or default
    except Exception:
        return default


# WordPiece maps any whitespace-free run longer than this to a single [UNK],
# which makes token counts meaningless (a 200 KB base64 blob counts as 3 tokens)
# and makes every such document embed to the SAME vector. Long runs are therefore
# broken up before tokenising.
_MAX_CHARS_PER_WORD = 100


def _break_long_runs(text: str, max_chars: int = _MAX_CHARS_PER_WORD) -> str:
    """Insert breaks into whitespace-free runs longer than ``max_chars``.

    Applies to base64 blobs, minified JS/JSON and long URLs. Without it those runs
    collapse to [UNK] and are neither counted nor split correctly.
    """
    if not text:
        return text
    out, run = [], 0
    for ch in text:
        if ch.isspace():
            run = 0
        else:
            run += 1
            if run > max_chars:
                out.append(" ")
                run = 1
        out.append(ch)
    return "".join(out)


def _split_by_tokens(unit: str, max_tokens: int) -> list[str]:
    """Split text so that no piece exceeds ``max_tokens`` real tokens.

    Uses ONE tokenizer call per piece with offset mapping and cuts on real token
    boundaries, rather than probing token counts per word. That is both correct
    for scripts without spaces (CJK, Thai — where a word-based split cannot cut at
    all) and far cheaper: the previous per-word approach re-tokenised a growing
    prefix and dominated ingest time.
    """
    if not unit or not unit.strip():
        return []
    prepared = _break_long_runs(unit)
    try:
        tok = getattr(embeddings.get_embed_model(), "tokenizer", None)
        if tok is None or not getattr(tok, "is_fast", False):
            raise RuntimeError("no fast tokenizer")
        enc = tok(prepared, return_offsets_mapping=True, add_special_tokens=False,
                  truncation=False, verbose=False)
        offsets = [o for o in enc["offset_mapping"] if o[1] > o[0]]
        if not offsets:
            return [unit]
        # Reserve room for the [CLS]/[SEP] the encoder adds at embed time.
        budget = max(1, max_tokens - 2)
        if len(offsets) <= budget:
            return [prepared]
        pieces: list[str] = []
        for i in range(0, len(offsets), budget):
            window = offsets[i:i + budget]
            piece = prepared[window[0][0]:window[-1][1]]
            if piece.strip():
                pieces.append(piece)
        # Slicing at a token boundary can re-segment the neighbouring subwords, so a
        # piece occasionally counts a few tokens more than the offsets predicted.
        # Verify each piece for real and shave the stragglers.
        repaired: list[str] = []
        for piece in pieces:
            shrink = budget
            while count_tokens(piece) > max_tokens and shrink > 8:
                shrink = int(shrink * 0.9)
                enc2 = tok(piece, return_offsets_mapping=True, add_special_tokens=False,
                           truncation=False, verbose=False)
                off2 = [o for o in enc2["offset_mapping"] if o[1] > o[0]]
                if not off2:
                    break
                piece = piece[off2[0][0]:off2[min(shrink, len(off2)) - 1][1]]
            repaired.append(piece)
        return repaired
    except Exception:
        # Tokenizer unavailable — fall back to a conservative character split so
        # oversized text is still broken up rather than passed through whole.
        approx = max(1, max_tokens) * 4
        return [prepared[i:i + approx] for i in range(0, len(prepared), approx)
                if prepared[i:i + approx].strip()]


def _split_oversized(unit: str, size: int) -> list[str]:
    """Break a single oversized 'sentence' into <= size-word pieces.

    Prefers line boundaries (a CSV/table row, a line of code) so rows stay whole,
    and falls back to a plain word split for a genuinely unbroken run of text.
    """
    out: list[str] = []
    buf: list[str] = []
    bufw = 0
    for line in unit.splitlines() or [unit]:
        lw = len(line.split())
        if lw > size:                      # single line longer than a whole chunk
            if buf:
                out.append("\n".join(buf)); buf, bufw = [], 0
            words = line.split()
            for k in range(0, len(words), size):
                out.append(" ".join(words[k:k + size]))
            continue
        if bufw + lw > size and buf:
            out.append("\n".join(buf)); buf, bufw = [], 0
        buf.append(line); bufw += lw
    if buf:
        out.append("\n".join(buf))
    return [c for c in out if c.strip()]


def chunk_text(text: str, size: int = 180, overlap: int = 32) -> list[str]:
    """
    Split text into overlapping chunks that respect sentence boundaries.

    Unlike naive word-count splitting, this first splits on sentence endings
    (./?/!) so no sentence is ever cut in the middle.  Sentences are then
    grouped into windows of approximately `size` words.  Overlap is achieved
    by carrying the last N words worth of sentences forward into the next chunk.

    A sentence longer than ``size * _CHUNK_HARD_CAP_FACTOR`` is split rather than
    emitted whole — see _split_oversized.

    size    — target words per chunk (approximate)
    overlap — words of sentence-level context shared between adjacent chunks
    """
    hard_cap = max(1, size) * _CHUNK_HARD_CAP_FACTOR
    raw_sentences = [s.strip() for s in _SENT_END.split(text) if s.strip()]
    # Normalise FIRST: any "sentence" over the hard cap (a whole CSV/table/code
    # block with no ./!/? in it) is broken down before windowing. Doing this here
    # rather than inside the loop matters — the accumulator admits any sentence
    # when the window is still empty, so an oversized one would otherwise pass
    # straight through as a single unbounded chunk.
    sentences: list[str] = []
    for s in raw_sentences:
        if len(s.split()) > hard_cap:
            sentences.extend(_split_oversized(s, size))
        else:
            sentences.append(s)
    if not sentences:
        # Fallback: no sentence punctuation found — split on whitespace
        words = text.split()
        result, i = [], 0
        while i < len(words):
            chunk = " ".join(words[i : i + size])
            if chunk.strip():
                result.append(chunk)
            i += max(1, size - overlap)
        return result

    chunks: list[str] = []
    i = 0
    while i < len(sentences):
        window: list[str] = []
        wc = 0
        j = i
        # Accumulate sentences until we reach the word target
        while j < len(sentences):
            s_words = len(sentences[j].split())
            if wc > 0 and wc + s_words > size:
                break
            window.append(sentences[j])
            wc += s_words
            j += 1
        # Safety: a sentence that alone exceeds `size` (but is under the hard cap,
        # so it was not pre-split) is still emitted whole rather than dropped.
        if not window:
            window.append(sentences[i])
            j = i + 1

        chunks.append(" ".join(window))

        if overlap == 0 or j >= len(sentences):
            i = j
        else:
            # Walk backwards from j to find enough sentences to cover `overlap` words
            carried_wc = 0
            new_i = j
            for k in range(j - 1, i, -1):
                carried_wc += len(sentences[k].split())
                if carried_wc >= overlap:
                    new_i = k
                    break
            i = max(new_i, i + 1)   # always advance at least one sentence

    # Final guard, in REAL tokens. Word-based windowing above is a good proxy for
    # prose but badly underestimates CSV/code/URL content, which tokenises into
    # many sub-word pieces — without this, the tail of such a chunk is silently
    # dropped by the encoder and becomes unsearchable.
    limit = _model_token_limit()
    guarded: list[str] = []
    for c in chunks:
        guarded.extend(_split_by_tokens(c, limit))
    return guarded

