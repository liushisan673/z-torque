# Bundled Chinese Embedding model

The Windows desktop package bundles **BAAI/bge-small-zh-v1.5** through FastEmbed/ONNX Runtime.

- Language: Chinese
- Vector dimension: 512
- Model license: MIT
- Approximate model payload: 90 MB
- Runtime: CPU, offline after installation
- Upstream model: https://huggingface.co/BAAI/bge-small-zh-v1.5
- FastEmbed model registry: https://qdrant.github.io/fastembed/examples/Supported_Models/

Model weights are intentionally not committed to Git. Run:

\`\`\`powershell
python -m pip install -e .
python scripts/download_embedding_model.py
\`\`\`

The desktop build script performs this preparation automatically and copies the cache into
\`dist/ZTorque\`. Distribute the complete onedir folder, not only \`ZTorque.exe\`.

At runtime, the application sets Hugging Face offline mode before loading the model. Missing
or corrupt model assets therefore produce a visible degraded knowledge-search state; the app
does not silently download model files or fabricate vectors. FTS5 remains available as a
lexical fallback.
