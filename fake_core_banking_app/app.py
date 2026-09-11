"""Meridian Core 8.x — a fictional credit union back-office system.

A deliberately legacy surface: iframe-partitioned layout, table-based markup,
no test IDs, and form field names that change on every restart.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import (
    Flask,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from fake_core_banking_app.data import (
    MEMBERS,
    OPERATORS,
    Account,
    next_account_number,
    search_by_surname,
)
from fake_core_banking_app.faults import FAULTS
from fake_core_banking_app.field_names import BUILD_ID, field_name
from fake_core_banking_app.institutions import DEFAULT_INSTITUTION, INSTITUTIONS

SESSION_MINUTES = 15
RESULTS_PER_PAGE = 5

app = Flask(__name__)
app.secret_key = "fixture-only-not-a-secret"
app.jinja_env.globals.update(field_name=field_name, build_id=BUILD_ID)


# --------------------------------------------------------------------------
# Institution selection and session handling
# --------------------------------------------------------------------------


def current_institution():
    slug = request.args.get("inst") or session.get("inst") or os.environ.get(
        "MERIDIAN_INSTITUTION", DEFAULT_INSTITUTION
    )
    if slug not in INSTITUTIONS:
        slug = DEFAULT_INSTITUTION
    session["inst"] = slug
    return INSTITUTIONS[slug]


def signed_in() -> bool:
    if FAULTS.session_expired:
        return False
    if FAULTS.note_request():
        return False
    expires = session.get("expires_at")
    if not expires:
        return False
    return datetime.now(UTC) < datetime.fromisoformat(expires)


def touch_session() -> None:
    session["expires_at"] = (
        datetime.now(UTC) + timedelta(minutes=SESSION_MINUTES)
    ).isoformat()


@app.context_processor
def inject_chrome():
    return {
        "inst": current_institution(),
        "operator": session.get("operator_name", ""),
        "today": datetime.now(UTC).strftime("%m/%d"),
    }


@app.before_request
def apply_faults():
    if request.path.startswith(("/_faults", "/static")):
        return None
    if FAULTS.slow_response_ms:
        time.sleep(FAULTS.slow_response_ms / 1000)
    if FAULTS.app_error:
        return render_template("error.html", code="MC-5001"), 500
    return None


def requires_session(view_name: str):
    """Return a redirect to the sign-on screen if the session is not live."""
    if not signed_in():
        return redirect(url_for("login", expired=1, next=view_name))
    touch_session()
    return None


# --------------------------------------------------------------------------
# Sign on
# --------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        user = request.form.get(field_name("user_id"), "").strip()
        pwd = request.form.get(field_name("password"), "")
        record = OPERATORS.get(user)
        if record and record["password"] == pwd:
            FAULTS.session_expired = False
            FAULTS._requests_since_sign_on = 0
            session["operator"] = user
            session["operator_name"] = record["name"]
            session["can_open_accounts"] = record["can_open_accounts"]
            touch_session()
            return redirect(url_for("shell"))
        error = "SIGN ON FAILED — USER ID OR PASSWORD NOT RECOGNISED"
    return render_template("login.html", error=error, expired=request.args.get("expired"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# The iframe-partitioned shell: banner, navigation, work area
# --------------------------------------------------------------------------


@app.route("/")
def shell():
    if (r := requires_session("shell")) is not None:
        return r
    return render_template("shell.html")


@app.route("/banner")
def banner():
    return render_template("banner.html")


@app.route("/nav")
def nav():
    return render_template("nav.html")


@app.route("/work")
def work_home():
    if (r := requires_session("work_home")) is not None:
        return r
    return render_template("work_home.html")


# --------------------------------------------------------------------------
# Member inquiry: search, then profile
# --------------------------------------------------------------------------


@app.route("/members/inquiry", methods=["GET", "POST"])
def member_inquiry():
    if (r := requires_session("member_inquiry")) is not None:
        return r

    if FAULTS.maintenance_interstitial or FAULTS.interstitial_once:
        FAULTS.interstitial_once = False  # a real notice is shown once, then gone
        return render_template("interstitial.html", target=url_for("member_inquiry"))

    if request.method == "POST":
        member_id = request.form.get(field_name("member_id"), "").strip()
        surname = request.form.get(field_name("surname"), "").strip()

        if member_id:
            if not member_id.isdigit() or len(member_id) != 6:
                return render_template(
                    "inquiry.html", entered=member_id, surname=surname,
                    error="INVALID ENTRY — MEMBER NUMBER MUST BE 6 DIGITS",
                )
            if FAULTS.force_not_found or member_id not in MEMBERS:
                return render_template("inquiry.html", not_found=member_id)
            return redirect(url_for("member_profile", member_id=member_id))

        if surname:
            if len(surname) < 2:
                return render_template(
                    "inquiry.html", surname=surname,
                    error="INVALID ENTRY — ENTER AT LEAST 2 CHARACTERS OF SURNAME",
                )
            return redirect(url_for("member_search", surname=surname.upper(), page=1))

        return render_template(
            "inquiry.html",
            error="INVALID ENTRY — SUPPLY A MEMBER NUMBER OR A SURNAME",
        )

    return render_template("inquiry.html")


@app.route("/members/search")
def member_search():
    """Paginated result list. The automation must pick the correct row."""
    if (r := requires_session("member_inquiry")) is not None:
        return r
    if FAULTS.maintenance_interstitial or FAULTS.interstitial_once:
        FAULTS.interstitial_once = False
        return render_template("interstitial.html", target=request.full_path)

    surname = request.args.get("surname", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    results = [] if FAULTS.force_not_found else search_by_surname(surname)
    if not results:
        return render_template("inquiry.html", not_found=surname, surname=surname)

    pages = (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
    page = min(page, pages)
    start = (page - 1) * RESULTS_PER_PAGE
    return render_template(
        "results.html", surname=surname, total=len(results), page=page, pages=pages,
        results=results[start : start + RESULTS_PER_PAGE],
        first_row=start + 1,
    )


@app.route("/members/<member_id>")
def member_profile(member_id: str):
    if (r := requires_session("member_inquiry")) is not None:
        return r
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("inquiry.html", not_found=member_id)
    tab = request.args.get("tab", "accounts")
    if tab not in ("accounts", "holds", "address"):
        tab = "accounts"
    return render_template("profile.html", member=member, tab=tab)


# --------------------------------------------------------------------------
# Open a sub-account: form, review, confirmation
# --------------------------------------------------------------------------


ACCOUNT_TYPES = ["SAVINGS", "MONEY MARKET", "HOLIDAY CLUB"]


@app.route("/members/<member_id>/subaccount/new", methods=["GET", "POST"])
def subaccount_new(member_id: str):
    if (r := requires_session("member_inquiry")) is not None:
        return r
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("inquiry.html", not_found=member_id)

    if FAULTS.permission_denied or not session.get("can_open_accounts", False):
        return render_template("denied.html", member=member), 403

    if request.method == "POST":
        kind = request.form.get(field_name("account_type"), "")
        raw_deposit = request.form.get(field_name("deposit"), "").strip()
        error = None
        if FAULTS.validation_error:
            error = "ENTRY REJECTED — OPENING DEPOSIT BELOW PRODUCT MINIMUM"
        elif kind not in ACCOUNT_TYPES:
            error = "INVALID ENTRY — SELECT AN ACCOUNT TYPE"
        else:
            try:
                deposit = Decimal(raw_deposit)
                if deposit < Decimal("25.00"):
                    error = "ENTRY REJECTED — OPENING DEPOSIT BELOW PRODUCT MINIMUM"
            except (InvalidOperation, ValueError):
                error = "INVALID ENTRY — OPENING DEPOSIT MUST BE NUMERIC"
        if error:
            return render_template(
                "subaccount_new.html", member=member, types=ACCOUNT_TYPES,
                error=error, kind=kind, deposit=raw_deposit,
            )
        return render_template(
            "subaccount_review.html", member=member, kind=kind, deposit=raw_deposit,
        )

    return render_template("subaccount_new.html", member=member, types=ACCOUNT_TYPES)


@app.route("/members/<member_id>/subaccount/confirm", methods=["POST"])
def subaccount_confirm(member_id: str):
    if (r := requires_session("member_inquiry")) is not None:
        return r
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("inquiry.html", not_found=member_id)

    kind = request.form.get(field_name("account_type"), "")
    deposit = Decimal(request.form.get(field_name("deposit"), "0"))
    number = next_account_number(member_id)
    member.accounts.append(Account(number, kind, deposit, "OPEN"))
    return render_template(
        "subaccount_confirm.html", member=member, kind=kind,
        deposit=deposit, number=number,
    )


# --------------------------------------------------------------------------
# Fault injection panel (a test harness, not part of the product being modelled)
# --------------------------------------------------------------------------


@app.route("/_faults", methods=["GET", "POST"])
def faults_panel():
    if request.method == "POST":
        if "clear" in request.form:
            FAULTS.clear()
        else:
            FAULTS.slow_response_ms = int(request.form.get("slow_response_ms") or 0)
            FAULTS.expire_after_requests = int(request.form.get("expire_after_requests") or 0)
            FAULTS._requests_since_sign_on = 0
            for flag in (
                "session_expired", "maintenance_interstitial", "interstitial_once",
                "validation_error", "permission_denied", "app_error", "force_not_found",
            ):
                setattr(FAULTS, flag, flag in request.form)
        return redirect(url_for("faults_panel"))
    return render_template("faults.html", faults=FAULTS.as_dict())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8081")), debug=False)
