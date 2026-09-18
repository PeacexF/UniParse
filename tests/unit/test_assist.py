"""The assist layer: brief, defensive parsing, and the validation gate."""

from __future__ import annotations

import json

import pytest

from uparse.assist import brief as brief_mod
from uparse.assist.protocol import AssistError, Proposal, parse_payload
from uparse.assist.validate import check
from uparse.config.schema import AssistConfig, Config
from uparse.core.models import PageModel
from uparse.extraction.engine import extract

BASE = "https://shop.test/list"
LISTING = """
<ul class="products">
  <li class="product"><h3><a href="/p/1">Alpha Keyboard</a></h3>
    <span class="price">$49.99</span><span class="sc-x1">3 hrs ago</span>
    <span class="sc-y2">Add to basket</span></li>
  <li class="product"><h3><a href="/p/2">Beta Mouse</a></h3>
    <span class="price">$29.50</span><span class="sc-x1">5 hrs ago</span>
    <span class="sc-y2">Add to basket</span></li>
  <li class="product"><h3><a href="/p/3">Gamma Dock</a></h3>
    <span class="price">$99.00</span><span class="sc-x1">9 hrs ago</span>
    <span class="sc-y2">Add to basket</span></li>
</ul>
"""


@pytest.fixture
def analysed():
    page = PageModel(url=BASE, final_url=BASE, html=LISTING)
    result = extract(page)
    return page, result, brief_mod.build(page, result)


# --- the brief ------------------------------------------------------------


def test_the_brief_is_far_smaller_than_a_real_page():
    import gzip
    from pathlib import Path

    url = "https://www.bbc.com/news"
    raw = Path("tests/corpus/bbc_news_front.html.gz")
    html = gzip.decompress(raw.read_bytes()).decode()
    page = PageModel(url=url, final_url=url, html=html)
    size = len(brief_mod.build(page, extract(page)).to_json())
    assert size * 20 < len(html)  # measured ratios are 26:1 to 236:1


def test_named_and_unnamed_columns_both_appear(analysed):
    _, _, brief = analysed
    assert {c.named for c in brief.columns if c.named} >= {"title", "price"}
    assert any(c.named is None for c in brief.columns)


def test_every_column_has_a_citable_id(analysed):
    _, _, brief = analysed
    ids = [c.id for c in brief.columns]
    assert len(ids) == len(set(ids))
    assert all(brief.column(i) is not None for i in ids)


def test_a_page_with_no_structure_has_no_material():
    page = PageModel(url=BASE, final_url=BASE, html="<html><body><p>Nothing.</p></body></html>")
    assert not brief_mod.build(page, extract(page)).has_material


# --- parsing whatever a model actually says -------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        '{"fields": [{"name": "published", "column": "c1"}]}',
        'Sure!\n```json\n{"fields": [{"name": "published", "column": "c1"}]}\n```\nHope that helps',
        'Here you go: {"fields": [{"name": "published", "column": "c1"}]} — let me know',
    ],
)
def test_json_survives_fences_and_chatter(answer):
    assert parse_payload(answer)["fields"][0]["name"] == "published"


def test_braces_inside_strings_do_not_confuse_the_parser():
    assert parse_payload('{"notes": "a } brace", "fields": []}')["notes"] == "a } brace"


def test_an_answer_with_no_json_is_an_error():
    with pytest.raises(AssistError):
        parse_payload("I'm afraid I can't help with that.")


def test_malformed_field_entries_are_skipped_not_fatal():
    proposal = Proposal.from_payload(
        {"fields": [{"name": "ok", "column": "c1"}, {"name": 5}, "nonsense", {"column": "c2"}]}
    )
    assert [f.name for f in proposal.fields] == ["ok"]


# --- the validation gate --------------------------------------------------


def _verdict(analysed, fields, **kwargs):
    _, result, brief = analysed
    proposal = Proposal.from_payload({"fields": fields})
    return check(proposal, brief, result.records, **kwargs)


def test_a_real_column_is_accepted_and_measured(analysed):
    _, _, brief = analysed
    column = next(c for c in brief.columns if c.named is None and "ago" in "".join(c.samples))
    verdict = _verdict(analysed, [{"name": "published", "column": column.id}])
    assert [a.name for a in verdict.accepted] == ["published"]
    assert verdict.accepted[0].coverage == 1.0


def test_an_invented_column_id_is_rejected(analysed):
    verdict = _verdict(analysed, [{"name": "price", "column": "c999"}])
    assert not verdict.accepted
    assert "no such column" in verdict.rejected[0].reason


def test_a_selector_cannot_be_smuggled_in_as_a_column_id(analysed):
    verdict = _verdict(analysed, [{"name": "price", "column": "span.price"}])
    assert not verdict.accepted


def test_an_unusable_name_is_rejected(analysed):
    _, _, brief = analysed
    cid = brief.columns[0].id
    verdict = _verdict(analysed, [{"name": "Drop Table;--", "column": cid}])
    assert not verdict.accepted
    assert "usable field name" in verdict.rejected[0].reason


def test_a_name_in_the_user_s_own_words_is_reshaped_not_lost(analysed):
    """Free-tier models answer in the words of the request. `stars today` is a correct
    mapping in bad formatting, and losing it would be losing the field."""
    _, _, brief = analysed
    column = next(c for c in brief.columns if c.named == "price")
    verdict = _verdict(analysed, [{"name": "Unit Price", "column": column.id}])
    assert [a.name for a in verdict.accepted] == ["unit_price"]


def test_the_same_name_cannot_be_claimed_twice(analysed):
    _, _, brief = analysed
    a, b = brief.columns[0].id, brief.columns[1].id
    verdict = _verdict(analysed, [{"name": "sku", "column": a}, {"name": "sku", "column": b}])
    assert len(verdict.accepted) == 1
    assert "duplicate" in verdict.rejected[0].reason


def test_the_field_limit_is_enforced(analysed):
    _, _, brief = analysed
    fields = [{"name": f"f{i}", "column": c.id} for i, c in enumerate(brief.columns)]
    verdict = _verdict(analysed, fields, max_fields=2)
    assert len(verdict.accepted) == 2


def test_a_column_the_engine_already_names_is_marked_as_agreeing(analysed):
    _, _, brief = analysed
    column = next(c for c in brief.columns if c.named == "price")
    verdict = _verdict(analysed, [{"name": "price", "column": column.id}])
    assert verdict.accepted[0].agrees_with_engine


# --- config surface -------------------------------------------------------


def test_assist_defaults_to_no_provider():
    assert Config().assist.provider == "null"


@pytest.mark.parametrize("kwargs", [{"provider": "openai_compatible"}, {"provider": "command"}])
def test_a_provider_without_its_required_setting_is_refused(kwargs):
    with pytest.raises(ValueError):
        AssistConfig(**kwargs)


def test_a_key_is_named_never_stored():
    config = AssistConfig(
        provider="openai_compatible", base_url="https://x.test/v1", api_key_env="SOME_KEY"
    )
    assert "SOME_KEY" in json.dumps(config.model_dump(mode="json"))


# --- shapes a small model actually returns ---------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"fields": [{"name": "published", "column": "c1"}]},
        {"published": "c1"},
        {"published": {"column_id": "c1", "name": "published"}},
    ],
)
def test_the_three_shapes_a_model_reaches_for_all_parse(payload):
    """A free-tier model ignores the schema and answers in whatever shape it likes. Every
    id still has to exist in the brief, so being liberal here cannot be unsafe."""
    proposal = Proposal.from_payload(payload)
    assert [(f.name, f.column) for f in proposal.fields] == [("published", "c1")]


def test_a_flat_answer_does_not_swallow_keys_that_are_not_ids():
    proposal = Proposal.from_payload({"title": "c4", "notes": "looks like a shop", "extra": ["c1"]})
    assert [f.name for f in proposal.fields] == ["title"]


def test_columns_the_model_calls_interface_text_are_recorded(analysed):
    _, result, brief = analysed
    junk = next(c for c in brief.columns if "basket" in "".join(c.samples).lower())
    proposal = Proposal.from_payload({"fields": [], "drop": [junk.id, "c999"]})
    verdict = check(proposal, brief, result.records)
    assert [i.column for i in verdict.ignored] == [junk.id]
    assert "basket" in verdict.ignored[0].sample.lower()


def test_a_dropped_column_the_model_also_named_is_not_reported_as_dropped(analysed):
    _, result, brief = analysed
    column = next(c for c in brief.columns if c.named == "price")
    proposal = Proposal.from_payload(
        {"fields": [{"name": "price", "column": column.id}], "drop": [column.id]}
    )
    assert not check(proposal, brief, result.records).ignored


# --- renaming, rather than re-reading -------------------------------------


def test_a_renamed_field_is_measured_on_the_engine_s_own_value(analysed):
    """`{"field": "title"}` is what the config will carry, so the coverage and the sample
    have to describe that — not what re-running a selector would read off the page."""
    _, result, brief = analysed
    column = next(c for c in brief.columns if c.named == "title")
    verdict = _verdict(analysed, [{"name": "product_name", "column": column.id}])
    accepted = verdict.accepted[0]
    assert accepted.engine_name == "title"
    assert accepted.sample == result.records[0].get("title")
