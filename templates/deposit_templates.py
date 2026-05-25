"""

Deposit Templates
Templates for deposit requests, objections, and followups.
Includes both mandatory and non-mandatory deposit templates.

Deposit SMS are link-first: full PayID / amounts / upload live on /b/<code>/payment.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging

from config import get_account_name, get_escort_name, get_payid
from core.webform_security import get_webform_payment_url

logger = logging.getLogger(__name__)


def append_doubles_sourcing_deposit_notice_if_needed(text: str, booking_fields: dict | None) -> str:
    try:
        from core.rates_from_config import format_doubles_escort_sourcing_waits_for_verified_deposit_notice

        note = format_doubles_escort_sourcing_waits_for_verified_deposit_notice(booking_fields)
    except Exception as e:
        logger.warning("append_doubles_sourcing_deposit_notice_if_needed: %s", e)
        note = ""
    if note:
        return f"{text.rstrip()}\n\n{note}"
    return text


_PAYID_PLACEHOLDER = "[PayID not configured]"
_ACCOUNT_NAME_PLACEHOLDER = "[Account name not configured]"
_BOOKING_RECEIVED_ACK = "Thanks, your booking has been received."


def _deposit_incall():
    try:
        from core.rates_from_config import get_deposit_incall
        return get_deposit_incall()
    except Exception as e:
        logger.warning("Could not load deposit incall from rates config: %s", e)
        return 50


def _deposit_outcall():
    try:
        from core.rates_from_config import get_deposit_outcall
        return get_deposit_outcall()
    except Exception as e:
        logger.warning("Could not load deposit outcall from rates config: %s", e)
        return 100


# Placeholders: payid, account_name, upload_url, incall, outcall (amounts from Rates page)
# Kept for admin previews / legacy; SMS uses get_deposit_payment_page_url instead.
NON_MANDATORY_DEPOSIT_TEMPLATE = """Do you mind paying a small deposit of ${incall}-${outcall}? Its not mandatory at all, but its appreciated as it helps reassure my time wont be wasted.

PayID: {payid}
Account Name: {account_name}

Please upload your screenshot here:

{upload_url}

Thanks I look forward to seeing you soon"""


def get_deposit_payment_page_url(
    phone_number,
    mandatory=True,
    amount=None,
    reason="",
    outcall_address=None,
) -> str:
    """Build /b/<short_code>/payment URL for SMS (full deposit copy is on the page)."""
    return get_webform_payment_url(
        phone_number,
        mandatory=mandatory,
        amount=amount,
        reason=reason,
        outcall_address=outcall_address,
    )


def build_deposit_payment_page_context(
    phone_number: str,
    mode: str,
    amount_from_query,
    reason: str,
    outcall_address: str = "",
):
    """Template variables for deposit_payment_info.html."""
    mandatory = (mode or "mandatory").strip().lower() != "optional"
    payid = get_payid() or _PAYID_PLACEHOLDER
    account_name = get_account_name() or _ACCOUNT_NAME_PLACEHOLDER
    escort_name = get_escort_name() or ""
    incall = _deposit_incall()
    outcall = _deposit_outcall()
    reason_l = (reason or "").lower()

    display_amount = amount_from_query
    if display_amount is None:
        if reason_l == "overnight" or "overnight" in reason_l:
            try:
                from core.rates_from_config import get_deposit_overnight

                display_amount = get_deposit_overnight()
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                display_amount = 200
        elif mandatory:
            display_amount = outcall if "outcall" in reason_l else incall
        else:
            display_amount = None

    if mandatory:
        upload_amt = int(display_amount if display_amount is not None else outcall)
    else:
        upload_amt = incall

    upload_url = None
    payment_reference = None
    try:
        from core.deposit_upload_tokens import resolve_deposit_upload_and_reference

        u, r = resolve_deposit_upload_and_reference(phone_number, upload_amt)
        if u:
            upload_url = u
        if r:
            payment_reference = r
    except Exception as e:
        logger.warning("build_deposit_payment_page_context upload token failed: %s", e)

    if display_amount is not None:
        try:
            da = int(display_amount)
            amount_str = f"{da}.00"
        except (TypeError, ValueError):
            amount_str = str(display_amount)
    else:
        amount_str = ""

    return {
        "escort_name": escort_name,
        "payid": payid,
        "account_name": account_name,
        "mode": mode,
        "mandatory": mandatory,
        "reason": reason or "",
        "deposit_incall": incall,
        "deposit_outcall": outcall,
        "display_amount": display_amount,
        "amount_str": amount_str,
        "upload_url": upload_url,
        "payment_reference": payment_reference,
        "outcall_address": (outcall_address or "").strip(),
        "is_graphite": "graphite_hold" in reason_l,
        "is_overnight": reason_l == "overnight" or "overnight" in reason_l,
        "is_outcall": "outcall" in reason_l,
    }


def build_deposit_message(
    mandatory=True,
    followup=False,
    upload_url=None,
    phone_number=None,
    deposit_amount=None,
    *,
    reason: str = "",
    outcall_address: str | None = None,
    payment_reference: str | None = None,
    booking_fields: dict | None = None,
) -> str:
    """Build deposit request message with parameters.

    This replaces static deposit templates with a single parameterized function.
    Matches old folder implementation exactly.

    Args:
        mandatory: True for mandatory deposit ($100), False for optional ($50-100)
        followup: True if this is a followup reminder (30 min after initial request)
        upload_url: Optional upload link to include
        phone_number: Client's phone number (for generating upload link if not provided)
        deposit_amount: Deposit amount (defaults to $100 for mandatory, $50-$100 for optional)
        reason: Passed through to payment page URL (e.g. outcall, dinner_date)
        outcall_address: Optional address for payment page query string

    Returns:
        Formatted deposit message

    Examples:
        Short SMS with link to /b/<code>/payment (PayID and upload on the page).
    """
    r = reason or ""
    addr = outcall_address

    # Ensure upload URL + payment reference are available whenever possible.
    if phone_number and (not upload_url or not payment_reference):
        try:
            from core.deposit_upload_tokens import resolve_deposit_upload_and_reference

            token_amount = (
                int(deposit_amount)
                if deposit_amount is not None
                else (int(_deposit_outcall()) if mandatory else int(_deposit_incall()))
            )
            u, r = resolve_deposit_upload_and_reference(phone_number, token_amount)
            if u:
                upload_url = upload_url or u
            if r:
                payment_reference = payment_reference or r
        except Exception as e:
            logger.warning("build_deposit_message: upload token/ref generation failed: %s", e)

    if mandatory:
        if followup:
            pay_url = get_deposit_payment_page_url(
                phone_number,
                mandatory=True,
                amount=deposit_amount,
                reason=r,
                outcall_address=addr,
            )
            msg = "I haven't received your deposit yet. "
            msg += f"If you still want to secure your booking, please pay and upload here: {pay_url}"
            if payment_reference:
                msg += f"\nUse payment reference: {payment_reference}"
            msg += "\n\nIf not provided, your booking will not be reserved."
            return msg
        else:
            amount = deposit_amount or _deposit_outcall()
            pay_url = get_deposit_payment_page_url(
                phone_number,
                mandatory=True,
                amount=amount,
                reason=r,
                outcall_address=addr,
            )
            body = (
                f"A ${amount} deposit is required to confirm this booking.\n\n"
                f"Payment details and upload: {pay_url}\n"
                + (f"Use payment reference: {payment_reference}" if payment_reference else "")
            )
            return append_doubles_sourcing_deposit_notice_if_needed(body, booking_fields)
    else:
        if followup:
            pay_url = get_deposit_payment_page_url(
                phone_number, mandatory=False, amount=None, reason=""
            )
            ref_line = f"Use payment reference: {payment_reference}\n\n" if payment_reference else ""
            msg = (
                "Hey! Just following up on the optional deposit — no pressure, "
                "your booking is still confirmed either way.\n\n"
                f"Details and upload: {pay_url}\n\n"
                + ref_line
                + "If you don't wish to pay a deposit, reply NO and I won't ask again.\n\n"
                "See you soon!"
            )
            return msg
        else:
            pay_url = get_deposit_payment_page_url(
                phone_number, mandatory=False, amount=None, reason=""
            )
            ref_line = f"Use payment reference: {payment_reference}\n\n" if payment_reference else "\n"
            return (
                f"Do you mind paying a small deposit of ${_deposit_incall()}-${_deposit_outcall()}? "
                f"It's not mandatory at all, but it's appreciated as it helps reassure my time won't be wasted.\n\n"
                f"PayID, amounts, and upload: {pay_url}\n"
                + ref_line
                + "Thanks — I look forward to seeing you soon."
            )


def _format_mandatory_deposit_acknowledgement_line(
    booking_fields: dict, client_name: str | None = None
) -> str:
    """
    One line: Thanks Sam, your Dinner Date booking for Mon 13th April 2026 at 6:00pm has been received.
    """
    import datetime as _dt

    from templates.booking_collection_messages import _day_ordinal_suffix

    name = (client_name or "").strip() or (booking_fields.get("client_name") or "").strip()
    exp_raw = (booking_fields.get("experience_type") or "").strip()
    try:
        from templates.booking_reconfirmation import _format_experience
        exp_display = _format_experience(exp_raw)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        exp_display = exp_raw
    bt = (booking_fields.get("booking_type") or "").lower()
    if bt == "overnight" or (exp_raw and "overnight" in exp_raw.lower()):
        exp_str = "Overnight"
    elif exp_raw:
        exp_str = exp_display
    else:
        exp_str = ""

    date_val = booking_fields.get("date")
    time_val = booking_fields.get("time")
    if not date_val or time_val is None:
        if name:
            return f"Thanks {name}, your booking has been received."
        return _BOOKING_RECEIVED_ACK

    if isinstance(date_val, str):
        try:
            d = _dt.datetime.strptime(date_val[:10], "%Y-%m-%d").date()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            if name:
                return f"Thanks {name}, your booking has been received."
            return _BOOKING_RECEIVED_ACK
    elif isinstance(date_val, _dt.datetime):
        d = date_val.date()
    elif isinstance(date_val, _dt.date):
        d = date_val
    else:
        if name:
            return f"Thanks {name}, your booking has been received."
        return _BOOKING_RECEIVED_ACK

    try:
        from services.calendar.booking_window import parse_booking_time_hour_minute

        hm = parse_booking_time_hour_minute(time_val)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        hm = None
    if hm is None:
        if name:
            return f"Thanks {name}, your booking has been received."
        return _BOOKING_RECEIVED_ACK
    hour, minute = hm

    period = "am" if hour < 12 else "pm"
    h12 = hour % 12 or 12
    if minute:
        tstr = f"{h12}:{minute:02d}{period}"
    else:
        tstr = f"{h12}{period}"

    wd = d.strftime("%a")
    day_num = d.day
    suf = _day_ordinal_suffix(day_num)
    mon = d.strftime("%B")
    yr = d.year
    date_part = f"{wd} {day_num}{suf} {mon} {yr}"

    if exp_str:
        middle = f"your {exp_str} booking"
    else:
        middle = "your booking"

    if name:
        return f"Thanks {name}, {middle} for {date_part} at {tstr} has been received."
    return f"Thanks, {middle} for {date_part} at {tstr} has been received."


def build_mandatory_deposit_sms_message(
    phone_number: str | None,
    deposit_amount: int,
    booking_fields: dict,
    client_name: str | None = None,
    upload_url: str | None = None,
    reason: str = "",
    payment_reference: str | None = None,
) -> str:
    """
    Full mandatory deposit SMS: header, thanks + booking summary, PayID, account, upload link, sign-off.
    Used after the client confirms with YES (outcall / dinner date with mandatory deposit).
    """
    payid = get_payid() or _PAYID_PLACEHOLDER
    account_name = get_account_name() or _ACCOUNT_NAME_PLACEHOLDER
    escort_name = get_escort_name() or ""

    def _deposit_sms_signoff_escort() -> str:
        """Single-word sign-off avoids repeating 'Name + City' when the location line already names the city."""
        name = (escort_name or "").strip()
        if not name:
            return ""
        parts = name.split()
        if len(parts) >= 2:
            return parts[0]
        return name

    if phone_number and (not upload_url or not payment_reference):
        try:
            from core.deposit_upload_tokens import resolve_deposit_upload_and_reference

            u, r = resolve_deposit_upload_and_reference(phone_number, deposit_amount)
            if u:
                upload_url = u
            if r:
                payment_reference = r
        except Exception as e:
            logger.warning("build_mandatory_deposit_sms_message: upload token failed: %s", e)

    if not upload_url:
        upload_url = get_deposit_payment_page_url(
            phone_number,
            mandatory=True,
            amount=deposit_amount,
            reason=reason or "outcall",
            outcall_address=(booking_fields.get("outcall_address") or "").strip() or None,
        )

    try:
        amt = int(deposit_amount)
        amount_line = f"${amt}"
    except (TypeError, ValueError):
        amount_line = f"${deposit_amount}"

    ack = _format_mandatory_deposit_acknowledgement_line(booking_fields, client_name=client_name)

    pay_block = [f"PayID: {payid}", f"Account Name: {account_name}"]
    if payment_reference:
        pay_block.append(f"Payment Reference: {payment_reference}")

    lines: list[str] = [
        "💳 Deposit Required",
        "",
        ack,
        "",
        f"Please pay a {amount_line} deposit to secure your booking:",
        "",
        *pay_block,
    ]
    if payment_reference:
        lines.extend(["", "Use this payment reference in your transfer description."])
    lines.extend(
        [
            "",
            f"Once the deposit has been paid please take a screenshot of the payment and upload it here: {upload_url}",
            "",
            (
                f"Your booking will be confirmed once deposit is received. - {_deposit_sms_signoff_escort()}"
                if escort_name
                else "Your booking will be confirmed once deposit is received."
            ),
        ]
    )
    msg = "\n".join(lines)
    return append_doubles_sourcing_deposit_notice_if_needed(msg, booking_fields)


def get_non_mandatory_deposit_template(phone_number=None) -> str:
    """Non-mandatory deposit SMS showing PayID/account inline + upload link."""
    payid = get_payid() or _PAYID_PLACEHOLDER
    account_name = get_account_name() or _ACCOUNT_NAME_PLACEHOLDER

    upload_url = None
    payment_reference = None
    if phone_number:
        try:
            from core.deposit_upload_tokens import resolve_deposit_upload_and_reference

            u, r = resolve_deposit_upload_and_reference(phone_number, _deposit_incall())
            if u:
                upload_url = u
            if r:
                payment_reference = r
        except Exception as e:
            logger.warning("get_non_mandatory_deposit_template: upload token failed: %s", e)
    if not upload_url:
        upload_url = get_deposit_payment_page_url(
            phone_number, mandatory=False, amount=None, reason=""
        )

    ref_line = (
        f"Payment Reference: {payment_reference}\nUse this reference in your transfer description.\n\n"
        if payment_reference
        else ""
    )
    return (
        f"Do you mind paying a small deposit of ${_deposit_incall()}-${_deposit_outcall()}? "
        f"Its not mandatory at all, but its appreciated as it helps reassure my time wont be wasted.\n\n"
        f"💳 PayID: {payid}\n"
        f"Account Name: {account_name}\n\n"
        + ref_line
        + f"If you decide to pay a deposit then please upload your screenshot here:\n\n"
        f"{upload_url}\n\n"
        "Thanks babe, look forward to seeing you soon!"
    )
