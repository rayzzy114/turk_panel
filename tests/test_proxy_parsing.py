import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from import_data import parse_proxy_string


def test_parse_proxy_with_http_prefix():
    raw = "http://geo.iproyal.com:12321:AVF9IqAqUIsBW8dU:G9ukazsFGLcJ3QXC_country-tr_city-istanbul_session-RjhAO2rB_lifetime-30m"
    p = parse_proxy_string(raw)
    assert p.host == "geo.iproyal.com"
    assert p.port == 12321
    assert p.user == "AVF9IqAqUIsBW8dU"
    assert (
        p.password
        == "G9ukazsFGLcJ3QXC_country-tr_city-istanbul_session-RjhAO2rB_lifetime-30m"
    )


def test_parse_proxy_with_session_id():
    raw = "geo.iproyal.com:12321:AVF9IqAqUIsBW8dU:session-RjhAO2rB_lifetime-30m"
    p = parse_proxy_string(raw)
    assert p.session_id == "session-RjhAO2rB"


def test_parse_proxy_with_name():
    raw = "MyProxyName|1.2.3.4:8080:user:pass"
    p = parse_proxy_string(raw)
    assert p.name == "MyProxyName"
    assert p.host == "1.2.3.4"
    assert p.port == 8080
    assert p.user == "user"
    assert p.password == "pass"


def test_parse_proxy_standard():
    raw = "1.2.3.4:8080:user:pass"
    p = parse_proxy_string(raw)
    assert p.host == "1.2.3.4"
    assert p.port == 8080
    assert p.user == "user"
    assert p.password == "pass"
