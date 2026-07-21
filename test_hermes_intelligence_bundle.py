import json

from digest.hermes_intelligence_bundle import build_bundle


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _json(path, value):
    _write(path, json.dumps(value, ensure_ascii=False))


def _raw(title, url):
    return f'---\ntitle: "{title}"\nurl: "{url}"\nauthor: "Official"\nsource_type: official_changelog\ntopic: AI\n---\n\n# {title}\n'


def _candidate(raw_relative, score="25/30"):
    return f'---\nsource: {raw_relative}\ndate: 2026-07-14\nscore: {score}\npriority: high\nstatus: pending\n---\n\n# Proposal\n\n## One-line judgment\n\nWorth keeping.\n'


def test_bundle_merges_current_daily_and_routed_wiki_items(tmp_path):
    daily = tmp_path / "daily"
    wiki = tmp_path / "wiki"
    youtube = tmp_path / "youtube"
    _json(daily / "digests" / "hermes" / "2026-07-14-AM.json", {"date": "2026-07-14", "session": "AM", "delivery_successful": True, "items": [{"title": "Daily", "url": "https://example.com/daily", "evidence_complete": True}]})
    relative = "01_raw/ai_sources/official/2026-07-14__wiki.md"
    _write(wiki / relative, _raw("Wiki", "https://example.com/wiki"))
    _write(wiki / "03_review/candidates/wiki_candidates/20260714-wiki.md", _candidate(relative))

    output = build_bundle(daily, wiki_root=wiki, youtube_root=youtube, target_date="2026-07-14")
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert [item["title"] for item in payload["items"]] == ["Daily", "Wiki"]
    assert payload["sources"]["daily_news_digest"]["status"] == "present"
    assert payload["sources"]["llm_wiki"]["status"] == "present"


def test_bundle_rejects_stale_daily_and_unrouted_or_low_score_wiki(tmp_path):
    daily = tmp_path / "daily"
    wiki = tmp_path / "wiki"
    youtube = tmp_path / "youtube"
    _json(daily / "digests" / "hermes" / "2026-07-14-AM.json", {"date": "2026-07-13", "session": "AM", "delivery_successful": True, "items": [{"title": "Old", "url": "https://example.com/old"}]})
    raw_relative = "01_raw/ai_sources/official/2026-07-14__wiki.md"
    _write(wiki / raw_relative, _raw("Low", "https://example.com/low"))
    _write(wiki / "03_review/candidates/wiki_candidates/20260714-low.md", _candidate(raw_relative, "22/30"))

    output = build_bundle(daily, wiki_root=wiki, youtube_root=youtube, target_date="2026-07-14")
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["items"] == []
    assert payload["sources"]["daily_news_digest"]["status"] == "not_successful_am"
    assert payload["sources"]["llm_wiki"]["status"] == "empty"


def test_bundle_deduplicates_same_url_in_favor_of_daily_evidence(tmp_path):
    daily = tmp_path / "daily"
    wiki = tmp_path / "wiki"
    youtube = tmp_path / "youtube"
    _json(daily / "digests" / "hermes" / "2026-07-14-AM.json", {"date": "2026-07-14", "session": "AM", "delivery_successful": True, "items": [{"title": "Daily", "url": "https://example.com/same", "evidence_complete": True}]})
    relative = "01_raw/ai_sources/official/2026-07-14__wiki.md"
    _write(wiki / relative, _raw("Wiki", "https://example.com/same"))
    _write(wiki / "03_review/candidates/wiki_candidates/20260714-same.md", _candidate(relative))

    payload = json.loads(build_bundle(daily, wiki_root=wiki, youtube_root=youtube, target_date="2026-07-14").read_text(encoding="utf-8"))

    assert len(payload["items"]) == 1
    assert payload["items"][0]["source_system"] == "daily-news-digest"


def test_bundle_deduplicates_near_identical_titles_and_limits_to_five(tmp_path):
    daily = tmp_path / "daily"
    wiki = tmp_path / "wiki"
    youtube = tmp_path / "youtube"
    titles = [
        "OpenAI releases GPT Five",
        "Anthropic updates Claude API",
        "Google ships Gemini tooling",
        "Meta publishes model research",
        "Mistral releases coding model",
        "Hugging Face launches benchmark",
    ]
    items = [
        {"title": title, "url": f"https://example.com/{index}", "evidence_complete": True, "relevance_score": 10 - index}
        for index, title in enumerate(titles)
    ]
    items.append({"title": "OpenAI Releases GPT-5", "url": "https://example.com/duplicate", "evidence_complete": False})
    _json(daily / "digests" / "hermes" / "2026-07-14-AM.json", {"date": "2026-07-14", "session": "AM", "delivery_successful": True, "items": items})

    payload = json.loads(build_bundle(daily, wiki_root=wiki, youtube_root=youtube, target_date="2026-07-14").read_text(encoding="utf-8"))

    assert len(payload["items"]) == 5
    assert sum(item["title"] == "OpenAI releases GPT Five" for item in payload["items"]) == 1


def test_bundle_uses_only_previous_night_radar_and_labels_real_date(tmp_path):
    daily, wiki, youtube = tmp_path / "daily", tmp_path / "wiki", tmp_path / "youtube"
    _json(youtube / "digests/hermes/2026-07-13.json", {
        "date": "2026-07-13", "run_status": "ok",
        "items": [{"title": "Night radar", "url": "https://example.com/night", "evidence_complete": True}],
    })
    _json(youtube / "digests/hermes/2026-07-12.json", {
        "date": "2026-07-12", "run_status": "ok",
        "items": [{"title": "Too old", "url": "https://example.com/old", "evidence_complete": True}],
    })

    payload = json.loads(build_bundle(daily, wiki_root=wiki, youtube_root=youtube, target_date="2026-07-14").read_text(encoding="utf-8"))

    assert [item["title"] for item in payload["items"]] == ["Night radar"]
    assert payload["items"][0]["freshness_label"] == "previous_night"
    assert payload["items"][0]["source_artifact_date"] == "2026-07-13"
    assert payload["sources"]["youtube_digest"]["status"] == "previous_night"


def test_bundle_enriches_latest_news_with_recent_raw_context(tmp_path):
    daily, wiki, youtube = tmp_path / "daily", tmp_path / "wiki", tmp_path / "youtube"
    _json(daily / "digests/hermes/2026-07-14-AM.json", {
        "date": "2026-07-14", "session": "AM", "delivery_successful": True,
        "items": [{"title": "Model update", "url": "https://example.com/model", "evidence_complete": True}],
    })
    _write(wiki / "01_raw/ai_sources/official/2026-07-13__model.md", '''---
title: "Model update"
url: "https://example.com/model"
captured_at: "2026-07-13T22:10:00+08:00"
source_type: official_changelog
topic: inference
---

## 机器摘要

已有 RAW 背景说明该更新会影响本地推理工具链。
''')

    payload = json.loads(build_bundle(daily, wiki_root=wiki, youtube_root=youtube, target_date="2026-07-14").read_text(encoding="utf-8"))

    context = payload["items"][0]["knowledge_context"]
    assert context["topic"] == "inference"
    assert "本地推理工具链" in context["summary"]
    assert payload["sources"]["llm_wiki"]["status"] == "context_present"
    assert payload["sources"]["llm_wiki"]["raw_context_count"] == "1"
