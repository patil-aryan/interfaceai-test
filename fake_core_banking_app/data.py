"""Seed data for the fake core banking system. Entirely fictional."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Account:
    number: str
    kind: str  # SAVINGS, CHECKING, MONEY MARKET, HOLIDAY CLUB
    balance: Decimal
    status: str  # OPEN, CLOSED, RESTRICTED


@dataclass
class Hold:
    code: str
    description: str
    placed: str
    amount: Decimal


@dataclass
class Member:
    member_id: str
    surname: str
    given: str
    status: str  # ACTIVE, DORMANT, RESTRICTED
    branch: str
    joined: str
    address: str
    city_state_zip: str
    accounts: list[Account] = field(default_factory=list)
    holds: list[Hold] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.given} {self.surname}"


def _m(mid, surname, given, status, branch, joined, addr, csz, accounts, holds=()):
    return Member(mid, surname, given, status, branch, joined, addr, csz,
                  list(accounts), list(holds))


def _seed() -> dict[str, Member]:
    members = [
        # Three members share the surname VANCE. A search by surname returns all
        # three, so the automation must select the right row rather than the
        # first one.
        _m("100234", "VANCE", "ELEANOR R", "ACTIVE", "MAIN OFFICE", "2014-03-11",
           "418 CEDAR HOLLOW RD", "PINE BLUFF AR 71601", [
               Account("0001-100234-01", "SAVINGS", Decimal("4182.55"), "OPEN"),
               Account("0001-100234-02", "CHECKING", Decimal("912.08"), "OPEN"),
           ], [Hold("REG-D", "REGULATION D TRANSFER LIMIT REACHED", "2026-08-02", Decimal("0.00"))]),
        _m("100241", "VANCE", "ELEANOR M", "ACTIVE", "NORTHGATE", "2020-05-04",
           "77 LAUREL WAY APT 3", "PINE BLUFF AR 71603", [
               Account("0001-100241-01", "SAVINGS", Decimal("806.12"), "OPEN"),
           ]),
        _m("100252", "VANCE", "THEODORE J", "DORMANT", "WESTBROOK", "2009-09-15",
           "1290 OLD MILL RD", "SHERIDAN AR 72150", [
               Account("0001-100252-01", "SAVINGS", Decimal("112.00"), "OPEN"),
           ]),
        # Deliberately identical to 100252 on every field a search result shows.
        # A name is not a key; only the member number distinguishes these two.
        _m("100253", "VANCE", "THEODORE J", "ACTIVE", "WESTBROOK", "2022-04-02",
           "1290 OLD MILL RD", "SHERIDAN AR 72150", [
               Account("0001-100253-01", "SAVINGS", Decimal("6401.88"), "OPEN"),
           ]),

        _m("100235", "OYELARAN", "MARCUS T", "ACTIVE", "NORTHGATE", "2019-08-02",
           "5 HARBOUR POINT", "PINE BLUFF AR 71602", [
               Account("0001-100235-01", "SAVINGS", Decimal("27.40"), "OPEN"),
           ]),
        _m("100236", "RAGHAVAN", "PRIYA N", "DORMANT", "MAIN OFFICE", "2008-01-19",
           "3312 ASHFORD LN", "PINE BLUFF AR 71601", [
               Account("0001-100236-01", "SAVINGS", Decimal("15630.00"), "OPEN"),
           ], [Hold("DORM", "ACCOUNT DORMANT - NO ACTIVITY 24 MONTHS", "2025-02-11", Decimal("0.00"))]),
        _m("100244", "RAGHAVAN", "ANAND K", "ACTIVE", "MAIN OFFICE", "2017-04-23",
           "3312 ASHFORD LN", "PINE BLUFF AR 71601", [
               Account("0001-100244-01", "CHECKING", Decimal("3391.77"), "OPEN"),
           ]),
        _m("100237", "HALE", "DESMOND A", "RESTRICTED", "WESTBROOK", "2021-11-30",
           "902 QUARRY ST", "SHERIDAN AR 72150", [
               Account("0001-100237-01", "SAVINGS", Decimal("0.00"), "RESTRICTED"),
           ], [Hold("LEGAL", "LEVY - FUNDS RESTRAINED PENDING ORDER", "2026-06-30", Decimal("2500.00"))]),
        _m("100238", "SATO", "JUNE K", "ACTIVE", "NORTHGATE", "2016-06-07",
           "64 BIRCHFIELD AVE", "PINE BLUFF AR 71602", [
               Account("0001-100238-01", "CHECKING", Decimal("2204.19"), "OPEN"),
           ]),
        _m("100239", "OKONKWO", "ADAEZE C", "ACTIVE", "MAIN OFFICE", "2022-02-14",
           "18 STONEGATE CIR", "PINE BLUFF AR 71601", [
               Account("0001-100239-01", "SAVINGS", Decimal("745.30"), "OPEN"),
           ]),
        _m("100240", "BRENNAN", "COLM P", "ACTIVE", "WESTBROOK", "2011-07-21",
           "227 FOXGLOVE DR", "SHERIDAN AR 72150", [
               Account("0001-100240-01", "SAVINGS", Decimal("9012.44"), "OPEN"),
               Account("0001-100240-02", "MONEY MARKET", Decimal("25000.00"), "OPEN"),
           ]),
        _m("100242", "NAKAMURA", "HIRO S", "ACTIVE", "NORTHGATE", "2018-10-09",
           "830 ELMRIDGE TER", "PINE BLUFF AR 71602", [
               Account("0001-100242-01", "CHECKING", Decimal("488.02"), "OPEN"),
           ]),
        _m("100243", "DELACROIX", "MIREILLE", "ACTIVE", "MAIN OFFICE", "2013-12-01",
           "1 COURTHOUSE SQ", "PINE BLUFF AR 71601", [
               Account("0001-100243-01", "SAVINGS", Decimal("60.00"), "OPEN"),
           ]),
        _m("100245", "ABERNATHY", "WENDELL", "DORMANT", "WESTBROOK", "2006-03-30",
           "45 TANNERY RD", "SHERIDAN AR 72150", [
               Account("0001-100245-01", "SAVINGS", Decimal("3.19"), "OPEN"),
           ]),
        _m("100246", "FITZGERALD", "ROSEMARY", "ACTIVE", "NORTHGATE", "2015-05-18",
           "12 WILLOW BEND", "PINE BLUFF AR 71603", [
               Account("0001-100246-01", "SAVINGS", Decimal("1874.63"), "OPEN"),
           ]),
    ]
    return {m.member_id: m for m in members}


MEMBERS: dict[str, Member] = _seed()

OPERATORS = {
    "operator1": {"password": "changeme", "name": "A. TELLER", "can_open_accounts": True},
    "readonly1": {"password": "changeme", "name": "R. VIEWER", "can_open_accounts": False},
}


def search_by_surname(surname: str) -> list[Member]:
    needle = surname.strip().upper()
    matches = [m for m in MEMBERS.values() if needle in m.surname]
    return sorted(matches, key=lambda m: (m.surname, m.given))


def next_account_number(member_id: str) -> str:
    member = MEMBERS[member_id]
    return f"0001-{member_id}-{len(member.accounts) + 1:02d}"
