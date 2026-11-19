"""Offline deployment preparation; never invoked from a user request."""
import json
import os

from .retrieval import Retriever
from .store import Store


def ensure_index(store, retriever):
    revision, chunks = store.table_snapshot('chunks')
    # Check existence without decoding the entire index. The retrieval module
    # validates vector contents during search, including the deployment smoke.
    with store.connect() as db:
        exists = db.execute('SELECT 1 FROM vectors WHERE revision=? AND model=?',
                            (revision, retriever.index_key)).fetchone()
    if exists:
        return {'changed': False, 'revision': revision, 'chunks': len(chunks)}
    return {'changed': True, **retriever.build_index()}


def main():
    if os.getenv('MODEL_MODE', 'local') != 'retrieval':
        print(json.dumps({'skipped': True}))
        return
    store = Store(os.getenv('ACADEMIC_DB', 'data/academic.sqlite3'))
    retriever = Retriever.from_env(store)
    try:
        print(json.dumps(ensure_index(store, retriever), ensure_ascii=False))
    finally:
        retriever.close()


if __name__ == '__main__':
    main()
