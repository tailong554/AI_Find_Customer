from api.name_lead_routes import _canonical_url, _heuristic_classification, _platform_for


def test_direct_personal_chinese_name_request_is_high_intent():
    result = _heuristic_classification({
        "title": "I want a Chinese name for myself",
        "snippet": "Can someone help me choose a meaningful Chinese name?",
    })
    assert result["intent_type"] == "direct_request"
    assert result["intent_score"] >= 70
    assert result["reply_draft"]


def test_general_name_research_is_not_a_direct_lead():
    result = _heuristic_classification({
        "title": "Popular Chinese names",
        "snippet": "A list of Chinese names and their meanings.",
    })
    assert result["intent_type"] in {"research", "irrelevant"}
    assert result["intent_score"] < 70
    assert result["reply_draft"] == ""


def test_canonical_url_removes_tracking_parameters():
    assert _canonical_url("https://www.reddit.com/r/ChineseLanguage/comments/abc/title/?utm_source=x") == (
        "https://reddit.com/r/ChineseLanguage/comments/abc/title"
    )


def test_reddit_source_is_detected():
    assert _platform_for("https://www.reddit.com/r/ChineseLanguage/comments/abc/")[0] == "Reddit"
