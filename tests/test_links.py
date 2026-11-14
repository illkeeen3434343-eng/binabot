import pytest

from app.bina.links import LinkError, LinkMap, parse_link
from app.config import ROOT

M = LinkMap.load(str(ROOT / "bina_map.json"))


def kind(url, **kw):
    with pytest.raises(LinkError) as e:
        parse_link(url, M, **kw)
    return e.value.kind


@pytest.mark.parametrize("url", [
    "https://bina.az.example.com/baki/alqi-satqi/menziller",
    "https://examplebina.az/baki/alqi-satqi/menziller",
    "https://bina.az@evil.com/baki/alqi-satqi/menziller",
    "https://evil.com/?u=https://bina.az/baki/alqi-satqi/menziller",
    "https://bina.az:8443/baki/alqi-satqi/menziller",
    "https://user:pw@bina.az/baki/alqi-satqi/menziller",
    "ftp://bina.az/baki/alqi-satqi/menziller",
    "javascript:alert(1)", "salam", "", "https://", "http://[::1", "bina.az /baki",
    "https://tap.az/elanlar", "https://bina.az.evil.az/alqi-satqi",
])
def test_invalid(url):
    assert kind(url) == "invalid"


@pytest.mark.parametrize("url", [
    "https://bina.az/", "https://bina.az/items/123456",
    "https://bina.az/baki/alqi-satqi/unknown-category",
    "https://bina.az/baki/alqi-satqi/menziller/extra",
    "https://bina.az/baki/alqi-satqi/menziller?mystery=1",
    "https://bina.az/baki/alqi-satqi/menziller?price_to=abc",
    "https://bina.az/baki/alqi-satqi/menziller?room_ids[]=x",
])
def test_unsupported(url):
    assert kind(url) == "unsupported"


def test_existing_project_url_translates_like_monitor_py():
    p = parse_link("https://bina.az/baki/alqi-satqi/menziller?has_bill_of_sale=true&has_repair=true"
                   "&location_ids%5B%5D=51&location_ids%5B%5D=100&location_ids%5B%5D=16&price_to=200000", M)
    assert p.filter == {"cityId": "1", "categoryId": "1", "leased": False, "hasBillOfSale": True,
                        "locationIds": ["16", "51", "100"], "priceTo": 200000}
    assert p.client_filter == {"has_repair": True}


def test_normalisation_makes_equal_searches_identical():
    a = parse_link("HTTP://WWW.Bina.az/baki/alqi-satqi/menziller/?room_ids[]=3&room_ids[]=2"
                   "&price_to=150000.0&page=4&utm_source=x", M)
    b = parse_link("bina.az/Baki/alqi-satqi/menziller?price_to=150000&room_ids%5B%5D=2"
                   "&room_ids%5B%5D=3&room_ids%5B%5D=3", M)
    assert a.url == b.url == ("https://bina.az/baki/alqi-satqi/menziller"
                              "?price_to=150000&room_ids%5B%5D=2&room_ids%5B%5D=3")
    assert a.filter == b.filter


def test_rent_and_no_city():
    p = parse_link("https://bina.az/kiraye/menziller", M)
    assert p.filter == {"leased": True, "categoryId": "1"}
    assert parse_link("https://bina.az/alqi-satqi", M).filter == {"leased": False}


def test_unknown_param_policy_ignore():
    p = parse_link("https://bina.az/baki/alqi-satqi/menziller?mystery=1", M, unknown="ignore")
    assert p.ignored == ("mystery",) and "mystery" not in p.url


def test_subcategory_link_reported_by_user():
    p = parse_link("https://bina.az/baki/alqi-satqi/menziller/yeni-tikili?room_ids%5B%5D=2&room_ids%5B%5D=3"
                   "&room_ids%5B%5D=4&has_bill_of_sale=true&has_mortgage=true&has_repair=true&floor_first=false"
                   "&location_ids%5B%5D=51&location_ids%5B%5D=2&location_ids%5B%5D=33&location_ids%5B%5D=54"
                   "&location_ids%5B%5D=52&location_ids%5B%5D=4&location_ids%5B%5D=53&location_ids%5B%5D=1", M)
    assert p.filter == {"cityId": "1", "leased": False, "categoryId": "2", "hasBillOfSale": True,
                        "hasMortgage": True, "floorFirst": False, "roomIds": ["2", "3", "4"],
                        "locationIds": ["1", "2", "4", "33", "51", "52", "53", "54"]}
    assert p.client_filter == {"has_repair": True}
    assert p.url.startswith("https://bina.az/baki/alqi-satqi/menziller/yeni-tikili?")
    old = parse_link("https://bina.az/baki/alqi-satqi/menziller/kohne-tikili", M)
    assert old.filter["categoryId"] == "3" and old.url != parse_link("https://bina.az/baki/alqi-satqi/menziller", M).url
