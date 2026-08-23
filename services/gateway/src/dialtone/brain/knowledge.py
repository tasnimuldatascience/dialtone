"""The company's own information, and getting the right piece of it into a reply.

WHY AN AGENT NEEDS THIS. A language model knows nothing about your opening hours, your prices,
or whether you take walk-ins. Without a source of truth it will either refuse everything (useless)
or invent an answer (worse than useless — a confidently wrong price quoted down a phone line is a
complaint, and possibly a contract).

So the operator uploads their documents, and the agent is given only the passages relevant to the
question, with an instruction to answer from them or admit it does not know.

THE PHONE CONSTRAINT SHAPES THE WHOLE DESIGN. A chat assistant can retrieve twelve passages and
let the reader skim. A phone agent has one or two sentences and roughly 250ms to find them, so:

  SMALL CHUNKS      A retrieved passage is read aloud, or paraphrased from. A 2000-character
                    chunk means the model paraphrases from mostly-irrelevant text.
  FEW OF THEM       Three passages, not twelve. Beyond that the model averages across them and
                    produces something vaguer than any single source.
  FAST              Embedding the query is the only model call before the LLM starts, and it
                    sits directly in the caller's silence.

Retrieval is embeddings plus keyword matching, fused. Pure embeddings miss exact strings — a
caller asking about "the Q4 pricing sheet" needs the document literally called that — and pure
keyword matching misses paraphrase, which is how people actually speak.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("dialtone.knowledge")

#: Small enough to read aloud, large enough to carry a complete fact. Roughly one short
#: paragraph, which is the unit an operator writes an FAQ answer in anyway.
CHUNK_CHARS = 480
CHUNK_OVERLAP = 80

#: Three passages fit inside a phone reply's context without the model averaging across them.
TOP_K = 3

#: RAW cosine similarity below which a passage is simply not about the question. Measured, not
#: guessed: on this encoder, genuinely relevant passages score 0.65-0.71 and unrelated ones
#: 0.33-0.39, so 0.50 sits in empty space between the two populations.
#:
#: This gate is applied BEFORE any normalising, and that ordering is the whole point. Min-max
#: normalisation maps the best candidate to 1.0 no matter how bad it is, so a relative threshold
#: can never reject "nothing here is relevant" -- ask a dental agent about dog food and the
#: opening-hours page comes back scoring 1.00. Absolute first, relative only for ranking what
#: survives.
MIN_RELEVANCE = 0.50

#: A passage can also earn its place on exact wording alone -- a caller naming a specific form or
#: product may paraphrase nothing. This is the share of the question's content words that must
#: appear in the passage for that to count.
MIN_TERM_OVERLAP = 0.6

#: Floor on the fused score, applied after ranking. Mostly redundant given the gate above; kept
#: because a passage can clear the gate and still rank last.
MIN_SCORE = 0.05

EMBED_MODEL = "BAAI/bge-small-en-v1.5"


@dataclass(slots=True)
class Chunk:
    id: str
    document_id: str
    document_title: str
    text: str
    #: Position in the source document, so a citation can point at where it came from.
    ordinal: int = 0
    embedding: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "document_title": self.document_title,
            "text": self.text,
            "ordinal": self.ordinal,
        }


@dataclass(slots=True)
class Hit:
    chunk: Chunk
    #: RAW cosine similarity, 0-1. Reported rather than the fused rank score because this is the
    #: number an operator can actually interpret: ~0.7 means "about this", ~0.35 means "not about
    #: this". The fused score is a within-result-set rank, so a lone survivor normalises to 0.0
    #: and looks like a failure when it is the correct answer.
    score: float
    #: Which signal found it. Surfaced in the studio so an operator can see WHY a passage was
    #: used -- "it matched the words" and "it matched the meaning" fail in different ways.
    via: str = "hybrid"


def split_document(text: str, *, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Break a document into passages, preferring paragraph and sentence boundaries.

    Splitting on a fixed character count is simpler and produces chunks that begin mid-sentence.
    On a phone call that matters more than usual: the model is paraphrasing the passage out loud,
    and a passage starting "…and we are closed on Sundays" reads as a non-sequitur.
    """
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []

    # Paragraphs first: an operator's FAQ is already chunked by the person who wrote it, and
    # honouring that beats any automatic boundary detection.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buffer = ""

    for paragraph in paragraphs:
        if len(paragraph) > size:
            if buffer:
                chunks.append(buffer.strip())
                buffer = ""
            chunks.extend(_split_long(paragraph, size, overlap))
            continue
        if len(buffer) + len(paragraph) + 2 <= size:
            buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        else:
            if buffer:
                chunks.append(buffer.strip())
            buffer = paragraph

    if buffer.strip():
        chunks.append(buffer.strip())
    return chunks


def _split_long(paragraph: str, size: int, overlap: int) -> list[str]:
    """A paragraph longer than one chunk: split on sentences, overlapping slightly."""
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    out: list[str] = []
    buffer = ""
    for sentence in sentences:
        # A single sentence longer than the chunk size does exist (a long list, a URL). Hard-cut
        # it rather than emitting an oversized chunk that the encoder would silently truncate.
        while len(sentence) > size:
            out.append(sentence[:size].strip())
            sentence = sentence[size - overlap:]
        if len(buffer) + len(sentence) + 1 <= size:
            buffer = f"{buffer} {sentence}".strip()
        else:
            if buffer:
                out.append(buffer)
            buffer = sentence
    if buffer:
        out.append(buffer)
    return out


# ── keyword scoring ──────────────────────────────────────────────────────────
_WORD = re.compile(r"[a-z0-9']+")
#: Words that carry no retrieval signal but appear in every spoken question. Left in the
#: embedding path (where they contribute to meaning) and removed from the keyword path.
_STOP = frozenset("""
a an the is are was were be been being am do does did have has had will would could should may
might can i you he she it we they there here of in on at for with from by about into over under
between through during and or but so if when while to my your our their this that these those
what which who how why where please thanks hi hello um uh
""".split())


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


class _Bm25:
    """Keyword scoring over the chunk set.

    BM25 rather than raw overlap because chunk lengths vary a lot in an uploaded document set --
    a one-line opening-hours note and a three-paragraph policy should compete fairly.
    """

    __slots__ = ("_df", "_docs", "_avg_len", "_n")

    def __init__(self, corpus: list[list[str]]) -> None:
        self._docs = corpus
        self._n = len(corpus) or 1
        self._avg_len = sum(len(d) for d in corpus) / self._n if corpus else 1.0
        self._df: Counter[str] = Counter()
        for doc in corpus:
            self._df.update(set(doc))

    def score(self, query: list[str], index: int, k1: float = 1.4, b: float = 0.72) -> float:
        doc = self._docs[index]
        if not doc:
            return 0.0
        counts = Counter(doc)
        total = 0.0
        for term in query:
            freq = counts.get(term, 0)
            if not freq:
                continue
            idf = math.log(1 + (self._n - self._df[term] + 0.5) / (self._df[term] + 0.5))
            denominator = freq + k1 * (1 - b + b * len(doc) / self._avg_len)
            total += idf * (freq * (k1 + 1)) / denominator
        return total


class KnowledgeBase:
    """Documents an operator uploaded, and the search over them.

    In-memory, rebuilt from the database on startup. A knowledge base for one business is
    kilobytes to a few megabytes of text; the moment it needs a vector database, this class is
    the wrong shape and should be replaced rather than grown.
    """

    def __init__(self, embed_model: str = EMBED_MODEL) -> None:
        self.embed_model = embed_model
        self.chunks: list[Chunk] = []
        self._encoder: Any = None
        self._tokenizer: Any = None
        self._bm25: _Bm25 | None = None
        self._terms: list[list[str]] = []
        self._lock = threading.Lock()

    # -- embedding ---------------------------------------------------------
    @property
    def embeddings_ready(self) -> bool:
        return self._encoder is not None

    def load_encoder(self) -> None:
        with self._lock:
            if self._encoder is not None:
                return
            import torch
            from transformers import AutoModel, AutoTokenizer

            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._tokenizer = AutoTokenizer.from_pretrained(self.embed_model)
            self._encoder = AutoModel.from_pretrained(self.embed_model).to(device).eval()
            log.info("knowledge encoder %s on %s", self.embed_model, device)

    def _embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        if self._encoder is None:
            self.load_encoder()
        import torch

        # bge models are trained with an instruction prefix on the QUERY side only. Omitting it
        # costs a few points of retrieval quality for free, and adding it to the documents too
        # costs the same in the other direction.
        prepared = [f"Represent this sentence for searching relevant passages: {t}" if is_query
                    else t for t in texts]
        with torch.inference_mode():
            enc = self._tokenizer(
                prepared, padding=True, truncation=True, max_length=512, return_tensors="pt"
            ).to(self._encoder.device)
            out = self._encoder(**enc).last_hidden_state[:, 0]      # CLS pooling, as bge expects
            out = torch.nn.functional.normalize(out, p=2, dim=1)
        return out.cpu().tolist()

    # -- building ----------------------------------------------------------
    def add_document(self, document_id: str, title: str, text: str) -> int:
        """Chunk, embed and index one document. Returns the number of chunks added."""
        pieces = split_document(text)
        if not pieces:
            return 0

        vectors = self._embed(pieces)
        for ordinal, (piece, vector) in enumerate(zip(pieces, vectors, strict=True)):
            self.chunks.append(Chunk(
                id=f"{document_id}:{ordinal}",
                document_id=document_id,
                document_title=title,
                text=piece,
                ordinal=ordinal,
                embedding=vector,
            ))
        self._reindex()
        return len(pieces)

    def remove_document(self, document_id: str) -> int:
        before = len(self.chunks)
        self.chunks = [c for c in self.chunks if c.document_id != document_id]
        self._reindex()
        return before - len(self.chunks)

    def _reindex(self) -> None:
        self._terms = [_terms(c.text + " " + c.document_title) for c in self.chunks]
        self._bm25 = _Bm25(self._terms)

    # -- searching ---------------------------------------------------------
    def search(self, query: str, *, k: int = TOP_K, min_score: float = MIN_SCORE) -> list[Hit]:
        """The passages most likely to answer this question, or nothing at all.

        Returning nothing is a first-class outcome. An agent given an irrelevant passage will try
        to answer from it, because that is what it was told the passage was for -- so the ability
        to say "we have nothing on that" has to live here, not in the prompt.
        """
        if not self.chunks or not query.strip():
            return []

        query_vector = self._embed([query], is_query=True)[0]
        dense = [_dot(query_vector, c.embedding) for c in self.chunks]

        query_terms = _terms(query)
        lexical = (
            [self._bm25.score(query_terms, i) for i in range(len(self.chunks))]
            if self._bm25 and query_terms else [0.0] * len(self.chunks)
        )

        # THE ABSOLUTE GATE, before anything is normalised. A passage stays in contention if it
        # is genuinely about the question (cosine) or literally contains it (term overlap).
        wanted = set(query_terms)
        candidates: list[int] = []
        for index in range(len(self.chunks)):
            relevant = dense[index] >= MIN_RELEVANCE
            overlap = (
                len(wanted & set(self._terms[index])) / len(wanted) if wanted else 0.0
            )
            if relevant or overlap >= MIN_TERM_OVERLAP:
                candidates.append(index)

        if not candidates:
            return []

        # Only now normalise, and only across what survived -- the relative scale is for ranking
        # the survivors against each other, never for deciding whether any of them belong.
        dense_n = _normalise([dense[i] for i in candidates])
        lexical_n = _normalise([lexical[i] for i in candidates])

        ranked: list[tuple[float, Hit]] = []
        for position, index in enumerate(candidates):
            # Weighted toward meaning: callers paraphrase, and the keyword half is there to
            # rescue exact names and numbers rather than to lead. This value orders the results
            # and is then discarded; what the caller sees is the raw similarity.
            combined = 0.7 * dense_n[position] + 0.3 * lexical_n[position]
            if combined < min_score and len(candidates) > 1:
                continue
            via = "keywords" if lexical_n[position] > dense_n[position] else "meaning"
            ranked.append((combined, Hit(chunk=self.chunks[index], score=dense[index], via=via)))

        ranked.sort(key=lambda pair: -pair[0])
        return [hit for _, hit in ranked[:k]]

    def context_for(self, query: str, *, k: int = TOP_K) -> tuple[str, list[Hit]]:
        """Retrieved passages formatted for a system prompt, plus the hits behind them.

        The hits come back alongside the text so the studio can show which document each answer
        came from. An agent that cites nothing is indistinguishable from one that is guessing.
        """
        hits = self.search(query, k=k)
        if not hits:
            return "", []
        blocks = [f"[{h.chunk.document_title}]\n{h.chunk.text}" for h in hits]
        return "\n\n".join(blocks), hits

    @property
    def stats(self) -> dict[str, Any]:
        documents = {c.document_id for c in self.chunks}
        return {
            "documents": len(documents),
            "chunks": len(self.chunks),
            "characters": sum(len(c.text) for c in self.chunks),
            "embeddings_ready": self.embeddings_ready,
        }


def _dot(a: list[float], b: list[float]) -> float:
    # Both sides are L2-normalised at embedding time, so the dot product IS cosine similarity
    # and there is no norm to recompute per query.
    return sum(x * y for x, y in zip(a, b, strict=False))


def _normalise(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        # Every candidate scored the same. Returning zeros rather than ones means a signal with
        # no opinion contributes nothing, instead of contributing its full weight to everything.
        return [0.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]
