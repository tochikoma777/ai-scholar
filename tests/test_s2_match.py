"""S2 记录身份校验（★ 防 id 串台把别的论文的引用数写进来）。

背景（2026-11-12 实测）：`data/signals/2026-11.jsonl` 里一篇 first_seen 仅 8 天的
新文得到 `citationCount=26`（同日 481 篇新文中位数 0、次高 2），并因此被定级。
`enrich` / `signals` 此前**不校验** S2 返回记录与请求 id 是否同一篇。
"""

from __future__ import annotations

from ai_scholar.collectors.s2 import S2Record, matches_requested


def test_arxiv_id_match_tolerates_version_suffix() -> None:
    record = S2Record(arxiv_id="2611.02620v2", title="Different Title")
    assert matches_requested(record, "ARXIV:2611.02620", paper_title="Whatever")


def test_arxiv_id_mismatch_is_rejected() -> None:
    record = S2Record(arxiv_id="2401.00001", title="Some Other Paper")
    assert not matches_requested(
        record, "ARXIV:2611.02620", paper_title="Adaptive Localization of Knowledge Negation"
    )


def test_missing_external_id_falls_back_to_title() -> None:
    title = "Adaptive Localization of Knowledge Negation for Continual LLM Unlearning"
    record = S2Record(title=title)
    assert matches_requested(
        record,
        "ARXIV:2611.02620",
        paper_title=title,
    )


def test_unrelated_title_without_external_id_is_rejected() -> None:
    record = S2Record(title="A Quasipolynomial Algorithm for Matrix Multiplication")
    assert not matches_requested(
        record,
        "ARXIV:2611.02620",
        paper_title="Adaptive Localization of Knowledge Negation for Continual LLM Unlearning",
    )


def test_doi_must_match_when_present() -> None:
    ok = S2Record(doi="10.1000/ABC", title="x")
    bad = S2Record(doi="10.1000/other", title="x")
    assert matches_requested(ok, "DOI:10.1000/abc")
    assert not matches_requested(bad, "DOI:10.1000/abc")
