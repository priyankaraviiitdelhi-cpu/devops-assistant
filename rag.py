"""
rag.py - the knowledge base (RAG = Retrieval-Augmented Generation).

1. Reads every .md / .txt file in data/docs/
2. Splits each file into small chunks
3. Stores the chunks in ChromaDB (a local vector database)
4. search() finds the chunks most related to a question

Build or rebuild the knowledge base with:  python rag.py
"""
from pathlib import Path
import chromadb

DOCS_DIR = Path("data/docs")
DB_DIR = "chroma_db"
COLLECTION = "devops_docs"
MAX_CHUNK_CHARS = 800


def chunk_text(text, max_chars=MAX_CHUNK_CHARS):
    """Group paragraphs together until a chunk reaches about max_chars."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        if current and len(current) + len(p) > max_chars:
            chunks.append(current.strip())
            current = ""
        current += p + "\n\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


def build_index():
    """Read all docs, chunk them and store them in ChromaDB (starts fresh each time)."""
    client = chromadb.PersistentClient(path=DB_DIR)
    try:
        client.delete_collection(COLLECTION)
    except Exception:
        pass
    collection = client.create_collection(COLLECTION)

    files = sorted(list(DOCS_DIR.glob("*.md")) + list(DOCS_DIR.glob("*.txt")))
    ids, documents, metadatas = [], [], []
    for f in files:
        for i, chunk in enumerate(chunk_text(f.read_text(encoding="utf-8"))):
            ids.append(f"{f.name}-{i}")
            documents.append(chunk)
            metadatas.append({"source": f.name})

    if not documents:
        print("No documents found in data/docs/")
        return
    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    print(f"Indexed {len(documents)} chunks from {len(files)} files.")


def search(question, k=4):
    """Return the k chunks most similar to the question."""
    client = chromadb.PersistentClient(path=DB_DIR)
    collection = client.get_or_create_collection(COLLECTION)
    if collection.count() == 0:
        return []
    results = collection.query(query_texts=[question], n_results=min(k, collection.count()))
    return [
        {"text": doc, "source": meta["source"]}
        for doc, meta in zip(results["documents"][0], results["metadatas"][0])
    ]


if __name__ == "__main__":
    build_index()
    print("\nTest search: 'Which EC2 instance types are allowed?'")
    for r in search("Which EC2 instance types are allowed?"):
        print(" -", r["source"], "|", r["text"][:70].replace("\n", " "), "...")