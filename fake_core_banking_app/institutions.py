"""Two institutions running the same vendor product, configured differently."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Institution:
    slug: str
    name: str
    short_name: str
    product_version: str

    # Same vendor product, different configured wording. This is what the
    # institution override layer exists to absorb.
    member_id_label: str
    search_button_text: str
    savings_row_label: str
    accent: str


PINECREST = Institution(
    slug="pinecrest-cu",
    name="PINECREST CREDIT UNION",
    short_name="PINECREST CU",
    product_version="8.4",
    member_id_label="MEMBER ID",
    search_button_text="Search",
    savings_row_label="SAVINGS",
    accent="#1f3a68",
)

LAKESHORE = Institution(
    slug="lakeshore-fcu",
    name="LAKESHORE FEDERAL CREDIT UNION",
    short_name="LAKESHORE FCU",
    product_version="8.6",
    member_id_label="ACCOUNT NUMBER",
    search_button_text="Find",
    savings_row_label="REGULAR SHARES",
    accent="#5a3a1f",
)

INSTITUTIONS = {i.slug: i for i in (PINECREST, LAKESHORE)}
DEFAULT_INSTITUTION = PINECREST.slug
