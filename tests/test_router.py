"""依查詢複雜度選擇模型。"""
from partpilot.router import choose_model

def test_route_off_uses_default():
    assert choose_model("任何問題", "opus", "haiku", "off") == "opus"

def test_route_auto():
    assert choose_model("廠區 C FGD101-01 400 台缺哪些料？", "opus", "haiku", "auto") == "haiku"
    assert choose_model("交期拉長對生產有什麼影響？我們該怎麼應變？", "opus", "haiku", "auto") == "opus"
    assert choose_model("任何問題", "opus", "", "auto") == "opus"
