import pytest
from pydantic import ValidationError

from pacer_base.models import ClientIn, EntryIn, slugify


@pytest.mark.parametrize(
    "name,slug",
    [("S&S", "s-and-s"), ("Living Planet", "living-planet"), ("  Odd!! Name  ", "odd-name"), ("A+B", "a-plus-b")],
)
def test_slugify(name, slug):
    assert slugify(name) == slug


def test_client_slug_from_name():
    assert ClientIn(name="S&S").resolved_slug() == "s-and-s"
    assert ClientIn(name="S&S", slug="sns").resolved_slug() == "sns"
    with pytest.raises(ValidationError):
        ClientIn(name="S&S", slug="Not A Slug")


def test_domains_normalised():
    assert ClientIn(name="x", domains=["@Example.COM ", ""]).domains == ["example.com"]


def test_entry_url_must_be_http():
    assert EntryIn(url="https://docs.google.com/x").url == "https://docs.google.com/x"
    for bad in ["javascript:alert(1)", "ftp://x", "https://has space"]:
        with pytest.raises(ValidationError):
            EntryIn(url=bad)


def test_entry_tags_cleaned_and_capped():
    assert EntryIn(tags=["Weekly Call", "weekly call", " "]).tags == ["weekly-call"]
    with pytest.raises(ValidationError):
        EntryIn(tags=[f"t{i}" for i in range(21)])


def test_entry_kind_is_checked():
    with pytest.raises(ValidationError):
        EntryIn(kind="meeting")


def test_new_entry_needs_content():
    with pytest.raises(ValueError):
        EntryIn(kind="call", title="Empty").require_new()
    EntryIn(kind="call", title="Has a link", url="https://x.example").require_new()


def test_occurred_at_needs_timezone():
    with pytest.raises(ValidationError):
        EntryIn(occurred_at="2026-11-04T10:00:00")
    assert EntryIn(occurred_at="2026-11-04T10:00:00Z").occurred_at.tzinfo is not None


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        EntryIn(titel="typo")
