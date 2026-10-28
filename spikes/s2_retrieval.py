"""Spike 2: does retrieval over a few manuals return the right passages?

Compares BM25, dense embeddings (via OpenRouter) and a simple hybrid (reciprocal rank fusion).
Questions and expected pages live in retrieval_questions.json, written by hand from the PDFs.
Run: uv run python fetch_data.py && uv run python s2_retrieval.py [--embed-model voyageai/voyage-4-lite]
"""

import argparse
import json
import re
import time

import numpy as np
import pypdfium2 as pdfium
from rank_bm25 import BM25Okapi

from common import PROVIDER_PREFS, ROOT, check_budget, client, record

CHUNK_WORDS, OVERLAP = 220, 40


def split(words: list[str]):
    for i, start in enumerate(range(0, max(len(words), 1), CHUNK_WORDS - OVERLAP)):
        text = " ".join(words[start:start + CHUNK_WORDS])
        if len(text) > 80:
            yield i, text


def handbook_chunks(path):
    doc_id = path.stem
    # pypdfium2, not pypdf: pypdf raises LimitReachedError on the image-heavy FAA handbook pages.
    for pno, page in enumerate(pdfium.PdfDocument(path), start=1):
        for i, text in split(page.get_textpage().get_text_range().split()):
            yield {"chunk_id": f"{doc_id}:p{pno}:c{i}", "doc_id": doc_id, "page": pno, "text": text}


def ad_chunks(path):
    doc = json.loads(path.read_text())
    # Prefix every chunk with the AD title so make/model survives chunking.
    title = doc["title"].removeprefix("Airworthiness Directives; ")
    for i, text in split(doc["text"].split()):
        yield {"chunk_id": f"ad:{doc['document_number']}:c{i}", "doc_id": f"ad:{doc['document_number']}",
               "page": None, "text": f"[AD: {title}] {text}"}


def load_corpus(max_ads: int | None):
    chunks = []
    for p in sorted((ROOT / "data" / "faa").glob("*.pdf")):
        chunks.extend(handbook_chunks(p))
    for p in sorted((ROOT / "data" / "ads").glob("*.json"))[:max_ads]:
        chunks.extend(ad_chunks(p))
    return chunks


def tok(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def embed(c, model: str, texts: list[str], kind: str, resume_dir=None) -> np.ndarray:
    """Embed in batches of 64. With resume_dir, each batch is saved as it lands, so a run that
    stops (402 on credits, budget cap) resumes instead of paying for the same batches again."""
    out = []
    for i in range(0, len(texts), 64):
        part = resume_dir / f"{i:07d}.npy" if resume_dir else None
        if part and part.exists():
            out.extend(np.load(part))
            continue
        check_budget()
        t = time.perf_counter()
        r = c.embeddings.create(model=model, input=texts[i:i + 64], extra_body={"provider": PROVIDER_PREFS})
        record(kind, model, r.usage, time.perf_counter() - t)
        batch = [d.embedding for d in r.data]
        if part:
            np.save(part, np.array(batch, dtype=np.float32))
        out.extend(batch)
    v = np.array(out, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def rrf(*rankings: list[int], k: int = 60) -> list[int]:
    score: dict[int, float] = {}
    for ranking in rankings:
        for r, idx in enumerate(ranking):
            score[idx] = score.get(idx, 0) + 1 / (k + r + 1)
    return sorted(score, key=score.get, reverse=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed-model", default="voyageai/voyage-4-lite")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--max-ads", type=int, default=None)
    ap.add_argument("--bm25-only", action="store_true", help="No API calls: keyword retrieval only")
    args = ap.parse_args()

    questions = json.loads((ROOT / "retrieval_questions.json").read_text())
    t0 = time.perf_counter()
    chunks = load_corpus(args.max_ads)
    n_docs = len({ch["doc_id"] for ch in chunks})
    words = sum(len(ch["text"].split()) for ch in chunks)
    print(f"{len(chunks):,} chunks from {n_docs:,} documents, ~{words:,} words, parsed in {time.perf_counter() - t0:.0f}s")

    t0 = time.perf_counter()
    bm25 = BM25Okapi([tok(ch["text"]) for ch in chunks])
    print(f"BM25 index built in {time.perf_counter() - t0:.0f}s")
    if args.bm25_only:
        E = Q = None
    else:
        c = client()
        resume = ROOT / ".cache" / f"emb-{args.embed_model.replace('/', '_')}-{len(chunks)}"
        resume.mkdir(parents=True, exist_ok=True)
        E = embed(c, args.embed_model, [ch["text"] for ch in chunks], "embed-corpus", resume)
        Q = embed(c, args.embed_model, [q["question"] for q in questions], "embed-query")

    def hit(q, ranking):
        """Rank (1-based) of the first chunk on an expected page of the expected doc, else None."""
        for r, idx in enumerate(ranking[: args.k], start=1):
            ch = chunks[idx]
            if ch["doc_id"] == q["doc_id"] and (ch["page"] is None or ch["page"] in q["pages"]):
                return r
        return None

    methods = {"bm25": []} if E is None else {"bm25": [], "dense": [], "hybrid": []}
    for qi, q in enumerate(questions):
        b = list(np.argsort(-bm25.get_scores(tok(q["question"])))[:50])
        if E is None:
            d, h = [], b
        else:
            d = list(np.argsort(-(E @ Q[qi]))[:50])
            h = rrf(b, d)
        row = {m: hit(q, r) for m, r in (("bm25", b), ("dense", d), ("hybrid", h)) if m in methods}
        for m, v in row.items():
            methods[m].append(v)
        top = chunks[h[0]]
        print(f"- {q['question'][:70]:70s} {row}  top={top['chunk_id']}")

    print(f"\nrecall@{args.k} and MRR@{args.k} over {len(questions)} questions (page-level match):")
    for m, ranks in methods.items():
        recall = sum(r is not None for r in ranks) / len(ranks)
        mrr = sum(1 / r for r in ranks if r) / len(ranks)
        print(f"  {m:6s} recall@{args.k}={recall:.2f}  MRR={mrr:.2f}")


if __name__ == "__main__":
    main()
