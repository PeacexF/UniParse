from __future__ import annotations

from uparse.core.models import ValueType

# Canonical field -> tokens that name it in class names, ids, itemprops and data-* keys.
FIELD_TOKENS: dict[str, frozenset[str]] = {
    "title": frozenset(
        {
            "title",
            "name",
            "heading",
            "headline",
            "productname",
            "producttitle",
            "itemtitle",
            "label",
        }
    ),
    "description": frozenset(
        {
            "description",
            "desc",
            "summary",
            "excerpt",
            "snippet",
            "subtitle",
            "abstract",
            "teaser",
            "caption",
            "blurb",
        }
    ),
    "price": frozenset(
        {
            "price",
            "cost",
            "amount",
            "pricing",
            "currentprice",
            "saleprice",
            "ourprice",
            "finalprice",
            "pricevalue",
        }
    ),
    "old_price": frozenset(
        {
            "oldprice",
            "wasprice",
            "listprice",
            "msrp",
            "regularprice",
            "compareatprice",
            "strikethrough",
            "originalprice",
        }
    ),
    "currency": frozenset({"currency", "pricecurrency", "currencycode"}),
    "url": frozenset({"url", "link", "permalink", "href", "canonical", "itemurl", "producturl"}),
    "image": frozenset(
        {"image", "img", "thumb", "thumbnail", "photo", "picture", "cover", "poster", "avatar"}
    ),
    "date": frozenset(
        {
            "date",
            "published",
            "publishdate",
            "pubdate",
            "datetime",
            "time",
            "posted",
            "created",
            "updated",
            "datepublished",
            "timestamp",
        }
    ),
    "author": frozenset(
        {"author", "byline", "creator", "writer", "poster", "username", "seller", "vendor"}
    ),
    "brand": frozenset({"brand", "manufacturer", "make", "publisher"}),
    "rating": frozenset(
        {"rating", "stars", "score", "ratingvalue", "reviewscore", "averagerating"}
    ),
    "reviews": frozenset(
        {"reviews", "reviewcount", "ratingcount", "votes", "numreviews", "comments"}
    ),
    "category": frozenset(
        {"category", "cat", "section", "genre", "department", "topic", "tag", "kind", "type"}
    ),
    "sku": frozenset(
        {
            "sku",
            "mpn",
            "ean",
            "upc",
            "isbn",
            "gtin",
            "productcode",
            "articlenumber",
            "itemid",
            "productid",
            "identifier",
        }
    ),
    "availability": frozenset(
        {"availability", "stock", "instock", "stockstatus", "available", "inventory"}
    ),
    "location": frozenset(
        {"location", "address", "place", "city", "region", "venue", "country", "area", "district"}
    ),
    "phone": frozenset({"phone", "tel", "telephone", "mobile", "contactphone"}),
    "email": frozenset({"email", "mail", "emailaddress"}),
    "discount": frozenset({"discount", "sale", "percentoff", "savings", "badge"}),
    "duration": frozenset({"duration", "length", "runtime", "readtime"}),
    "salary": frozenset({"salary", "wage", "compensation", "pay", "payrange"}),
    "company": frozenset({"company", "employer", "organization", "org", "firm"}),
    "views": frozenset({"views", "viewcount", "plays", "reads"}),
}

TOKEN_TO_FIELD: dict[str, str] = {
    token: name for name, tokens in FIELD_TOKENS.items() for token in tokens
}

FIELD_TYPES: dict[str, ValueType] = {
    "title": ValueType.STRING,
    "description": ValueType.STRING,
    "price": ValueType.NUMBER,
    "old_price": ValueType.NUMBER,
    "currency": ValueType.STRING,
    "url": ValueType.URL,
    "image": ValueType.URL,
    "date": ValueType.DATETIME,
    "author": ValueType.STRING,
    "brand": ValueType.STRING,
    "rating": ValueType.NUMBER,
    "reviews": ValueType.INTEGER,
    "category": ValueType.STRING,
    "sku": ValueType.STRING,
    "availability": ValueType.STRING,
    "location": ValueType.STRING,
    "phone": ValueType.STRING,
    "email": ValueType.STRING,
    "discount": ValueType.NUMBER,
    "duration": ValueType.STRING,
    "salary": ValueType.STRING,
    "company": ValueType.STRING,
    "views": ValueType.INTEGER,
}

# schema.org / OpenGraph property names mapped onto canonical fields.
SCHEMA_MAP: dict[str, str] = {
    "name": "title",
    "headline": "title",
    "alternatename": "title",
    "description": "description",
    "abstract": "description",
    "price": "price",
    "lowprice": "price",
    "pricecurrency": "currency",
    "url": "url",
    "mainentityofpage": "url",
    "image": "image",
    "thumbnailurl": "image",
    "contenturl": "image",
    "datepublished": "date",
    "datecreated": "date",
    "datemodified": "date",
    "uploaddate": "date",
    "startdate": "date",
    "author": "author",
    "creator": "author",
    "brand": "brand",
    "manufacturer": "brand",
    "ratingvalue": "rating",
    "reviewcount": "reviews",
    "ratingcount": "reviews",
    "category": "category",
    "genre": "category",
    "sku": "sku",
    "mpn": "sku",
    "gtin13": "sku",
    "productid": "sku",
    "identifier": "sku",
    "availability": "availability",
    "address": "location",
    "addresslocality": "location",
    "jobLocation": "location",
    "telephone": "phone",
    "email": "email",
    "duration": "duration",
    "timerequired": "duration",
    "basesalary": "salary",
    "hiringorganization": "company",
    "publisher": "brand",
    "site_name": "brand",
    "type": "category",
    "canonical": "url",
}

DEFAULT_FIELDS = ("title", "url", "price", "image", "description", "date")

# Ubiquitous nav/footer strings that should never win a field.
BOILERPLATE = frozenset(
    {
        "read more",
        "learn more",
        "see more",
        "show more",
        "view all",
        "details",
        "add to cart",
        "buy now",
        "shop now",
        "subscribe",
        "sign in",
        "log in",
        "next",
        "previous",
        "prev",
        "home",
        "menu",
        "search",
        "close",
        "share",
    }
)


def field_for_token(token: str) -> str | None:
    return TOKEN_TO_FIELD.get(_squash(token))


def field_for_schema_key(key: str) -> str | None:
    squashed = _squash(key)
    return SCHEMA_MAP.get(squashed) or TOKEN_TO_FIELD.get(squashed)


def _squash(token: str) -> str:
    return "".join(ch for ch in token.lower() if ch.isalnum())


def tokens_of(*values: str | None) -> list[str]:
    out: list[str] = []
    for value in values:
        if not value:
            continue
        current = ""
        for ch in value:
            if ch.isalnum():
                current += ch
            else:
                if current:
                    out.append(current.lower())
                current = ""
        if current:
            out.append(current.lower())
    return out
