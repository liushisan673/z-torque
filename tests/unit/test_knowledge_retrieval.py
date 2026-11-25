from app.builtin_plugins.knowledge.plugin import _fts_query, _query_terms


def test_cjk_query_is_split_into_trigrams_for_substring_search():
    terms = _query_terms("请解释中文知识库检索方式")
    assert "中文知" in terms
    assert "文知识" in terms
    assert "知识库" in terms
    expression = _fts_query("请解释中文知识库检索方式")
    assert '"中文知"' in expression
    assert '"知识库"' in expression


def test_english_query_keeps_whole_words_for_trigram_phrase_matching():
    expression = _fts_query("SQLite knowledge retrieval")
    assert '"sqlite"' in expression
    assert '"knowledge"' in expression
    assert '"retrieval"' in expression


def test_short_query_uses_like_fallback_instead_of_invalid_fts_expression():
    assert _fts_query("AI") == ""
