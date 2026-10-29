"""Spike 2: does retrieval over the FAA corpus return the right passages?

Experiment matrix: BM25, dense (several embedding models via OpenRouter), hybrid (reciprocal rank
fusion), LLM query rewriting, and Cohere reranking of the hybrid top 50. Two hand-labelled question
sets: retrieval_questions.json ('page', keyword-biased) and retrieval_questions_scenario.json.
Run: uv run python fetch_data.py && uv run python s2_retrieval.py [--embed-models ...] [--no-rerank]
"""

import argparse
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

import numpy as np
import openai
import pypdfium2 as pdfium
from rank_bm25 import BM25Okapi

from common import BASE_URL, PROVIDER_PREFS, ROOT, check_budget, client, record

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
        for attempt in range(6):  # providers return 429 "engine_overloaded" under load (seen with qwen3)
            try:
                r = c.embeddings.create(model=model, input=texts[i:i + 64], extra_body={"provider": PROVIDER_PREFS})
                break
            except (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError):
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt * 2)
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


REWRITE_MODEL = "anthropic/claude-haiku-5.5"
RERANK_MODEL = "cohere/rerank-4-pro"
REWRITE_PROMPT = (
    "Rewrite this aircraft maintenance question as a search query for FAA handbooks, AC 43.13-1B and "
    "Airworthiness Directives. Keep the original meaning and any aircraft make/model, and ADD the formal "
    "technical terms a manual would use (e.g. 'shiny bits in oil' -> 'metal particles, chip detector'). "
    "Reply with the query only, one line.\n\nQuestion: {q}"
)


def cached(name: str):
    path = ROOT / ".cache" / f"{name}.json"
    store = json.loads(path.read_text()) if path.exists() else {}
    return store, lambda: path.write_text(json.dumps(store))


def rewrite_all(c, questions: list[str]) -> list[str]:
    """LLM query rewriting: plain-language question -> question + technical vocabulary."""
    store, save = cached("rewrites")
    out = []
    for q in questions:
        if q not in store:
            check_budget()
            t = time.perf_counter()
            # max_tokens must leave room for reasoning: at 120, 23 of 40 rewrites came back empty.
            r = c.chat.completions.create(model=REWRITE_MODEL, max_tokens=600,
                                          messages=[{"role": "user", "content": REWRITE_PROMPT.format(q=q)}],
                                          extra_body={"provider": PROVIDER_PREFS, "reasoning": {"effort": "low"}})
            record("rewrite", REWRITE_MODEL, r.usage, time.perf_counter() - t)
            text = (r.choices[0].message.content or "").strip()
            if not text:
                raise RuntimeError(f"empty rewrite (finish_reason={r.choices[0].finish_reason}) for: {q}")
            store[q] = text
            save()
        out.append(f"{q} {store[q]}")
    return out


def rerank(query: str, candidates: list[int], chunks) -> list[int]:
    """Cohere rerank via OpenRouter's /rerank. Cached on (query, candidate ids)."""
    store, save = cached("rerank")
    key = hashlib.sha256((query + "|" + ",".join(map(str, candidates))).encode()).hexdigest()
    if key not in store:
        check_budget()
        body = {"model": RERANK_MODEL, "query": query, "top_n": len(candidates),
                "documents": [chunks[i]["text"] for i in candidates]}
        req = urllib.request.Request(f"{BASE_URL}/rerank", data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"})
        t = time.perf_counter()
        with urllib.request.urlopen(req, timeout=60) as resp:
            r = json.load(resp)
        record("rerank", RERANK_MODEL, r.get("usage", {}), time.perf_counter() - t)
        store[key] = [candidates[x["index"]] for x in r["results"]]
        save()
    return store[key]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed-models", nargs="+",
                    default=["voyageai/voyage-4-lite", "qwen/qwen3-embedding-4b", "voyageai/voyage-4-large"])
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--max-ads", type=int, default=None)
    ap.add_argument("--bm25-only", action="store_true", help="No API calls: keyword retrieval only")
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--out", default=str(ROOT / ".cache" / "s2_results.json"))
    args = ap.parse_args()

    # Two question sets: 'page' questions were written after reading the target page (keyword-biased);
    # 'scenario' questions were written first, in a technician's words, then the answering page was found.
    sets = {"page": json.loads((ROOT / "retrieval_questions.json").read_text()),
            "scenario": json.loads((ROOT / "retrieval_questions_scenario.json").read_text())}
    questions = [q | {"set": s} for s, qs in sets.items() for q in qs]

    t0 = time.perf_counter()
    chunks = load_corpus(args.max_ads)
    n_docs = len({ch["doc_id"] for ch in chunks})
    words = sum(len(ch["text"].split()) for ch in chunks)
    print(f"{len(chunks):,} chunks from {n_docs:,} documents, ~{words:,} words, parsed in {time.perf_counter() - t0:.0f}s")
    t0 = time.perf_counter()
    bm25 = BM25Okapi([tok(ch["text"]) for ch in chunks])
    print(f"BM25 index built in {time.perf_counter() - t0:.0f}s; {len(questions)} questions")

    def hit(q, ranking):
        """Rank (1-based) of the first chunk on an expected page of the expected doc, else None."""
        for r, idx in enumerate(ranking[: args.k], start=1):
            ch = chunks[idx]
            if ch["doc_id"] == q["doc_id"] and (ch["page"] is None or ch["page"] in q["pages"]):
                return r
        return None

    def top50_bm25(text):
        return [int(i) for i in np.argsort(-bm25.get_scores(tok(text)))[:50]]

    ranks: dict[str, list] = {}  # method -> rank per question (None = miss in top k)

    def add(method, qi, ranking):
        ranks.setdefault(method, [None] * len(questions))[qi] = hit(questions[qi], ranking)

    plain = [q["question"] for q in questions]
    bm_plain = [top50_bm25(t) for t in plain]
    for qi in range(len(questions)):
        add("bm25", qi, bm_plain[qi])

    if not args.bm25_only:
        c = client()
        rewritten = rewrite_all(c, plain)
        bm_rw = [top50_bm25(t) for t in rewritten]
        for qi in range(len(questions)):
            add("bm25+rewrite", qi, bm_rw[qi])
        for model in args.embed_models:
            short = model.split("/")[1]
            resume = ROOT / ".cache" / f"emb-{model.replace('/', '_')}-{len(chunks)}"
            resume.mkdir(parents=True, exist_ok=True)
            t0 = time.perf_counter()
            E = embed(c, model, [ch["text"] for ch in chunks], "embed-corpus", resume)
            print(f"{model}: corpus vectors ready in {time.perf_counter() - t0:.0f}s, dim={E.shape[1]}")
            Qp = embed(c, model, plain, "embed-query")
            Qr = embed(c, model, rewritten, "embed-query")
            for qi in range(len(questions)):
                d = [int(i) for i in np.argsort(-(E @ Qp[qi]))[:50]]
                dr = [int(i) for i in np.argsort(-(E @ Qr[qi]))[:50]]
                h, hr = rrf(bm_plain[qi], d)[:50], rrf(bm_rw[qi], dr)[:50]
                add(f"dense[{short}]", qi, d)
                add(f"hybrid[{short}]", qi, h)
                add(f"hybrid+rewrite[{short}]", qi, hr)
                if not args.no_rerank:
                    add(f"hybrid+rerank[{short}]", qi, rerank(plain[qi], h, chunks))
                    add(f"hybrid+rewrite+rerank[{short}]", qi, rerank(plain[qi], hr, chunks))
            del E

    def score(rs):
        return sum(r is not None for r in rs) / len(rs), sum(1 / r for r in rs if r) / len(rs)

    print(f"\nrecall@{args.k} / MRR@{args.k} (page-level match)")
    print(f"{'method':42s} {'page (n=%d)' % len(sets['page']):>16s} {'scenario (n=%d)' % len(sets['scenario']):>18s} {'all':>14s}")
    table = {}
    for m, rs in ranks.items():
        row = {}
        for s in (*sets, "all"):
            sub = [r for r, q in zip(rs, questions) if s == "all" or q["set"] == s]
            row[s] = score(sub)
        table[m] = row
        print(f"{m:42s} " + "  ".join(f"{row[s][0]:.2f} / {row[s][1]:.2f}".rjust(16) for s in (*sets, "all")))
    misses = {m: [questions[i]["question"][:60] for i, r in enumerate(rs) if r is None] for m, rs in ranks.items()}
    Path(args.out).write_text(json.dumps({"k": args.k, "table": table, "misses": misses,
                                          "n_chunks": len(chunks), "n_docs": n_docs}, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
