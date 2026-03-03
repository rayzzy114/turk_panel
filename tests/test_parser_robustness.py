import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from import_data import detect_gender, extract_user_agent, _parse_colon_credentials


def test_gender_detection_edge_cases():
    # Test mixed markers
    assert detect_gender("male_female.txt") == "ANY"

    # Test case sensitivity and unusual separators
    assert detect_gender("MALE-Account_01.txt") == "M"
    assert detect_gender("Female.Account.2024.txt") == "F"

    # Test Cyrillic markers
    assert detect_gender("аккаунт_мужской.txt") == "M"
    assert detect_gender("девушка_123.txt") == "F"


def test_display_name_gender_detection_edge_cases():
    # Test names not in hardcoded list but with markers
    assert detect_gender("source.txt", "John Doe (male)") == "M"
    assert detect_gender("source.txt", "Jane Smith - female") == "F"

    # Test ambiguous names
    assert detect_gender("source.txt", "Alex") == "ANY"

    # Test empty or None
    assert detect_gender("source.txt", None) == "ANY"
    assert detect_gender("source.txt", "") == "ANY"


def test_user_agent_extraction_robustness():
    # Test multiple Mozilla signatures
    # The new logic prefers the first one with a hint, or the first one overall.
    content = "UA: Mozilla/5.0 (Windows NT 10.0) ...\nSome other text\nBrowser: Mozilla/5.0 (Linux) ..."
    ua = extract_user_agent(content)
    # Both have hints ("ua:" and "browser"). It should return the first one.
    assert ua and "Windows NT 10.0" in ua

    # Test no UA
    assert extract_user_agent("Just some text without UA") is None


def test_colon_credentials_robustness():
    # Test with potential false positives
    lines = ["URL: https://facebook.com", "login:password", "Name: John"]
    creds = _parse_colon_credentials(lines)
    assert creds == ("login", "password")

    # Test with space in login (should be skipped)
    lines = ["invalid login:password"]
    assert _parse_colon_credentials(lines) is None
