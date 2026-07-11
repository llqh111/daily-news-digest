from digest.evidence import build_evidence_card, build_article_id, validate_evidence_card, normalize_url


def test_normalize_url():
    url = "HTTPS://www.Example.com:443/path/to//file/?utm_source=google&b=2&a=1#section"
    norm = normalize_url(url)
    assert norm == "https://www.example.com/path/to/file?a=1&b=2"
    

def test_build_article_id():
    # 相同 url 和 date
    article1 = {"link": "https://example.com/", "title": "Test 1"}
    article2 = {"link": "https://example.com/?utm_campaign=test", "title": "Test 2"}
    
    id1 = build_article_id(article1)
    id2 = build_article_id(article2)
    assert id1 == id2
    
    
def test_build_evidence_card():
    article = {
        "title": "Nvidia announces $2 billion revenue",
        "link": "https://example.com/nvidia",
        "fulltext": "Nvidia announced today that its Q3 revenue reached 2 billion dollars. The stock price went up 10%. Jensen Huang said they are happy.",
        "source": "TechNews"
    }
    
    card = build_evidence_card(article)
    
    assert card["article_id"].startswith("a1_")
    assert "nvidia" in card["entities"]
    assert "2 billion" in card["numbers"] or "10" in card["numbers"]
    
    assert len(card["confirmed_facts"]) > 0
    assert card["coverage"]["has_fulltext"] is True
    
    issues = validate_evidence_card(card)
    assert not issues
