# SPDX-License-Identifier: AGPL-3.0-or-later
"""The grounded-context prompt must gate on RELEVANCE, not just instruct citation.

The old wording ("Answer using the numbered context below … say so rather than
filling the gap from general knowledge") had no relevance-judgement step, so a
weak vocabulary-overlap match (a real case: "Beginning of Old MacDonald" pulled a
43% Linux man-page chunk) made the model summarise the irrelevant chunk and
abstain instead of answering. The prompt now tells the model the context is
machine-retrieved, to ignore irrelevant context without narrating it, and shows
each chunk's match score so it can calibrate."""
from redirecall import rag


def _chunks():
    return [{"source": "https://man7.org/x.html", "text": "guarded storage…",
             "relevance": 0.426},
            {"source": "notes.md", "text": "streams are append-only", "score": 0.81}]


def test_grounded_prompt_contains_the_relevance_gate():
    p = rag.build_context_prompt(_chunks())
    assert "may or may not be relevant" in p
    assert "IGNORE it entirely" in p
    assert "do not describe or summarise it" in p.replace("\n", " ")
    # the general-knowledge fallback must be explicitly ALLOWED for the
    # irrelevant-context case (the old text forbade it outright)
    assert "answer the question from general knowledge" in p


def test_each_chunk_carries_its_match_score():
    p = rag.build_context_prompt(_chunks())
    assert "match 43%" in p     # relevance 0.426 → rounded
    assert "match 81%" in p     # falls back to score when relevance absent
    # a chunk with no usable score renders without the marker instead of crashing
    p2 = rag.build_context_prompt([{"source": "s", "text": "t", "score": "bogus"}])
    assert "match" not in p2.split("[1]")[1].split("\n")[0]


def test_citation_discipline_is_kept():
    p = rag.build_context_prompt(_chunks())
    assert "square brackets" in p and "[2]" in p


def test_empty_chunks_abstention_notice_unchanged():
    p = rag.build_context_prompt([])
    assert "No relevant context was found" in p
    assert "not grounded" in p
