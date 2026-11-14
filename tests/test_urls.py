from kom.urls import canonical_url, in_scope, page_relpath, parent_candidates, scope_segments, should_skip


def test_canonical_normalizes():
    assert canonical_url("HTTPS://Exemplo.COM:443/Docs/?q=1#x") == "https://exemplo.com/Docs"
    assert canonical_url("/a//b/./c/", base="https://ex.com/x/y") == "https://ex.com/a/b/c"
    assert canonical_url("https://ex.com/a/../b") == "https://ex.com/b"
    assert canonical_url("mailto:a@b.c") is None
    assert canonical_url("ftp://ex.com/") is None


def test_parent_chain_is_path_heritage():
    assert list(parent_candidates("https://ex.com/docs/guide/setup")) == [
        "https://ex.com/docs/guide",
        "https://ex.com/docs",
        "https://ex.com/",
    ]
    assert list(parent_candidates("https://ex.com/")) == []


def test_scope_is_path_prefix_not_string_prefix():
    scope = scope_segments("https://ex.com/docs")
    assert in_scope("https://ex.com/docs/x/y", "ex.com", scope)
    assert in_scope("https://ex.com/docs", "ex.com", scope)
    assert not in_scope("https://ex.com/docsnew", "ex.com", scope)
    assert not in_scope("https://other.com/docs", "ex.com", scope)


def test_skip_binary_extensions():
    assert should_skip("https://ex.com/logo.PNG")
    assert not should_skip("https://ex.com/docs/intro")


def test_page_relpath_mirrors_path():
    assert page_relpath("https://ex.com/docs/guide/setup") == "ex.com/docs/guide/setup/index.md"
    assert page_relpath("https://ex.com:8080/") == "ex.com_8080/index.md"
    assert ".." not in page_relpath("https://ex.com/%2e%2e/x").split("/")
