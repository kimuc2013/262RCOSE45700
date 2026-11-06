"""Build dense vectors before serving, rather than during a user request."""
import os

from .models import RemoteModels
from .store import Store


def main():
    store = Store(os.getenv("ACADEMIC_DB", "data/academic.sqlite3"))
    models = RemoteModels()
    revision, chunks, _ = store.snapshot()
    if not chunks:
        raise RuntimeError("Ingest documents before building vectors")
    vectors = []
    for offset in range(0, len(chunks), 32):
        batch = chunks[offset:offset + 32]
        vectors.extend(models.embed([c['doc_title'] + '\n' + c['section'] + '\n' + c['text'] for c in batch]))
    if len({len(v) for v in vectors}) != 1:
        raise ValueError("Embedding dimensions changed between batches")
    store.save_vectors(revision, models.base + '|' + models.embedding, vectors)
    print(f"Indexed {len(vectors)} chunks, revision={revision}")


if __name__ == "__main__":
    main()
