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



def test_platform_specific_public_reply_draft_is_generated():
    result = _heuristic_classification({
        "title": "I need a Chinese name",
        "snippet": "Can someone suggest one for me?",
        "source_platform": "TikTok",
    })
    assert result["intent_type"] == "direct_request"
    assert "✨" in result["reply_draft"]
    assert result["need_clarity_score"] == 3
    assert result["service_fit_score"] == 3


def test_research_content_has_low_intent_score():
    result = _heuristic_classification({
        "title": "Popular Chinese names",
        "snippet": "History of Chinese names and their meanings.",
        "source_platform": "Web",
    })
    assert result["intent_type"] in {"research", "irrelevant"}
    assert result["intent_score"] < 70


def test_platform_detection_covers_social_channels():
    assert _platform_for("https://www.tiktok.com/@user/video/123")[0] == "TikTok"
    assert _platform_for("https://www.instagram.com/p/abc/")[0] == "Instagram"
    assert _platform_for("https://www.facebook.com/groups/abc/posts/123")[0] == "Facebook"
