import json
import urllib.error
import urllib.parse

import copyright_registration as cr
import pytest


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_cprs_registration_reads_post_1978_voyager_schema(monkeypatch):
    later_edition = {"hit": {
        "type_of_record": "registration",
        "type_of_work": "text",
        "registration_class": "TX",
        "public_records_id": "voyager_later",
        "registration_number": "TX0003864692",
        "publication_date_as_year": 1992,
        "primary_titles_list": [{
            "title_primary_title_title_proper": "The Color purple /",
        }],
        "display_names": {"persons": [{
            "name": "Walker, Alice", "roles": ["author"],
        }]},
    }}
    payload = {"data": [later_edition, {"hit": {
        "type_of_record": "registration",
        "system_of_origin": "voyager",
        "type_of_work": "text",
        "registration_class": "TX",
        "public_records_id": "voyager_13590678",
        "copyright_number_for_display": "TX0000987776",
        "registration_number": "TX0000987776",
        "registration_date": "1982-09-23",
        "publication_date_as_year": 1982,
        "primary_titles_list": [{
            "title_primary_title_title_proper": "The Color purple :",
            "title_primary_title_remainder_of_title": "a novel /",
        }],
        "display_names": {"persons": [{
            "name": "Walker, Alice",
            "roles": ["author", "claimant"],
        }]},
    }}]}
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        return _Response(payload)

    monkeypatch.setattr(cr.urllib.request, "urlopen", fake_urlopen)
    match = cr.cprs_registration("The Color Purple", "Alice Walker", 1982)

    assert match == {
        "source": "cprs",
        "reg_number": "TX0000987776",
        "title": "The Color purple :",
        "author": "Walker, Alice",
        "year": "1982",
        "record_id": "voyager_13590678",
    }
    query = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query)
    assert query["query"] == ['"The Color Purple" "Alice Walker"']


def test_cprs_registration_matches_fauna_and_flora_voyager_record(monkeypatch):
    payload = {"data": [{"hit": {
        "type_of_record": "registration",
        "system_of_origin": "voyager",
        "type_of_work": "text",
        "registration_class": "TX",
        "public_records_id": "voyager_13365801",
        "copyright_number_for_display": "TX0000520976",
        "registration_number": "TX0000520976",
        "registration_date": "1980-08-04",
        "publication_date_as_year": 1980,
        "primary_titles_list": [{
            "title_primary_title_title_proper":
                "Fauna and flora of the Bible /",
            "title_primary_title_statement_of_responsibility":
                "prepared in cooperation with the Committee on Translations "
                "of the United Bible Societies.",
        }],
        "display_names": {"organizations": [{
            "name": "United Bible Societies",
            "roles": ["author", "claimant"],
        }]},
        "author_statement_list": [{
            "author_full_name":
                "United Bible Societies, employer for hire.",
        }],
    }}]}
    monkeypatch.setattr(
        cr.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    match = cr.cprs_registration(
        "Fauna and Flora of the Bible", "United Bible Societies", 1980)

    assert match == {
        "source": "cprs",
        "reg_number": "TX0000520976",
        "title": "Fauna and flora of the Bible /",
        "author": "United Bible Societies, employer for hire.",
        "year": "1980",
        "record_id": "voyager_13365801",
    }


def test_cprs_registration_raises_when_network_is_unavailable(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(cr.urllib.request, "urlopen", unavailable)

    with pytest.raises(cr.RegistrationLookupError):
        cr.cprs_registration(
            "Fauna and Flora of the Bible", "United Bible Societies", 1980)


def test_cprs_registration_still_reads_historical_card_schema(monkeypatch):
    payload = {"data": [{"hit": {
        "type_of_record": "registration",
        "cc_type_of_work": "book",
        "all_type_of_work": ["book"],
        "author": ["An Herbalist"],
        "title_of_work": ["A Useful Herbal"],
        "registration_number": ["A 12345"],
        "fee_date_as_year": "1938",
        "public_records_id": "card_catalog_A12345",
    }}]}
    monkeypatch.setattr(
        cr.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    match = cr.cprs_registration("A Useful Herbal", "An Herbalist", 1938)
    assert match["reg_number"] == "A 12345"
    assert match["year"] == "1938"


def test_cprs_registration_rejects_non_text_post_1978_record(monkeypatch):
    payload = {"data": [{"hit": {
        "type_of_record": "registration",
        "type_of_work": "music",
        "registration_class": "PA",
        "primary_titles_list": [{
            "title_primary_title_title_proper": "A Useful Herbal",
        }],
        "display_names": {"persons": [{
            "name": "An Herbalist", "roles": ["author"],
        }]},
    }}]}
    monkeypatch.setattr(
        cr.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    assert cr.cprs_registration("A Useful Herbal", "An Herbalist") is None
