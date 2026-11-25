from uuid import uuid4

from app.builtin_plugins.knowledge.vector_store import LocalVectorStore
from app.db.session import SessionLocal
from app.kernel.ports.vector_store import VectorRecord


def test_local_vector_store_upsert_search_and_delete():
    store = LocalVectorStore(SessionLocal)
    namespace = "test:" + str(uuid4())
    store.upsert(
        namespace=namespace,
        records=[
            VectorRecord("x", [1.0, 0.0], {"chunk_id": "x"}),
            VectorRecord("y", [0.0, 1.0], {"chunk_id": "y"}),
            VectorRecord("z", [-1.0, 0.0], {"chunk_id": "z"}),
        ],
    )
    matches = store.search(namespace=namespace, query=[0.9, 0.1], top_k=2)
    assert [item.id for item in matches] == ["x", "y"]
    assert matches[0].score > matches[1].score
    assert matches[0].metadata == {"chunk_id": "x"}

    store.delete(namespace=namespace, ids=["x", "missing"])
    remaining = store.search(namespace=namespace, query=[1.0, 0.0], top_k=5)
    assert [item.id for item in remaining] == ["y", "z"]


def test_local_vector_store_keeps_namespaces_isolated():
    store = LocalVectorStore(SessionLocal)
    namespace_a = "test:" + str(uuid4())
    namespace_b = "test:" + str(uuid4())
    store.upsert(namespace=namespace_a, records=[VectorRecord("same", [1.0, 0.0])])
    store.upsert(namespace=namespace_b, records=[VectorRecord("same", [0.0, 1.0])])
    assert store.search(namespace=namespace_a, query=[1.0, 0.0])[0].score == 1.0
    assert store.search(namespace=namespace_b, query=[1.0, 0.0])[0].score == 0.0
