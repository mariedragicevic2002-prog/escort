"""Deposit screenshot upload (/d/<short_code>) and validation helpers."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


from typing import Any
from urllib.parse import quote, urlencode

from flask import jsonify, render_template, request, url_for

import config
from config import get_escort_name
from services.database_service import get_shared_db

from .blueprint import booking_bp
from .helpers import _fmt_sms_date, _format_time_value_for_confirmation, _safe_int_money
from .log import logger


def _positive_int_money(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        n = int(float(val))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_duration_minutes(raw) -> int:
    """Parse duration to minutes from int, float, or strings like '60', '1hr', '1.5hr', '90 mins'."""
    if raw is None:
        return 60
    if isinstance(raw, (int, float)):
        mins = int(raw)
        return mins if mins > 0 else 60
    import re as _re
    s = str(raw).strip().lower()
    if s.isdigit():
        mins = int(s)
        return mins if mins > 0 else 60
    m = _re.match(r"^(\d+(?:\.\d+)?)\s*hr", s)
    if m:
        return max(1, int(round(float(m.group(1)) * 60)))
    m = _re.match(r"^(\d+)\s*min", s)
    if m:
        mins = int(m.group(1))
        return mins if mins > 0 else 60
    digits = _re.sub(r"[^\d]", "", s)
    if digits:
        mins = int(digits)
        return mins if mins > 0 else 60
    return 60


def _booking_total_for_remaining_balance_sms(row, actual_deposit_amount: int) -> int | None:
    """Full booking total for deposit SMS: derive from rates config (calculate_price), with DB fallback."""
    from templates.confirmations import calculate_price
    from utils.row_utils import row_get

    tbc = _positive_int_money(row_get(row, "total_booking_cost", None))
    pri = _positive_int_money(row_get(row, "price", None))

    computed = None
    try:
        duration_raw = row_get(row, "duration", None)
        exp = row_get(row, "experience_type", None)
        incall = (row_get(row, "incall_outcall", None) or "").strip().lower() or "incall"
        dur = _parse_duration_minutes(duration_raw)
        bf_row = {
            "experience_type": exp,
            "incall_outcall": incall,
            "escort_supply_source": row_get(row, "escort_supply_source", None),
            "booking_type": row_get(row, "booking_type", None),
        }
        c = int(calculate_price(dur, experience_type=exp, incall_outcall=incall, booking_fields=bf_row))
        computed = c if c > 0 else None
    except Exception:
        computed = None

    # calculate_price is authoritative — if it succeeds, trust it over stored DB values.
    if computed is not None:
        return computed
    parts: list[int] = []
    if tbc:
        parts.append(tbc)
    if pri and pri > actual_deposit_amount:
        parts.append(pri)
    if not parts:
        return None
    return max(parts)


@booking_bp.route("/d/<short_code>", methods=["GET", "POST"])
def deposit_upload(short_code):
    """Deposit screenshot upload page.

    GET: Display upload form
    POST: Process uploaded screenshot with Vision API
    """
    token_data = _get_upload_token_data(short_code)

    if not token_data:
        return render_template(
            "upload.html",
            error="Invalid upload link. Please request a new one via SMS.",
            error_title="Link Not Found",
            error_icon="\U0001F517"
        ), 404

    if token_data.get('error'):
        error_icons = {'used': '\u2713', 'expired': '\u23F0', 'invalid': '\u26A0\uFE0F'}
        return render_template(
            "upload.html",
            error=token_data.get('message', 'Invalid link'),
            error_title="Link " + token_data.get('error', 'Invalid').title(),
            error_icon=error_icons.get(token_data.get('error'), '\u26A0\uFE0F')
        ), 400

    # GET request: Show upload form
    if request.method == "GET":
        return render_template(
            "upload.html",
            deposit_amount=token_data.get('deposit_amount', 100),
            attempts_remaining=token_data.get('attempts_remaining', 3),
            payment_reference=token_data.get('payment_reference'),
        )

    # POST request: Process upload
    if 'screenshot' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400

    file = request.files['screenshot']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    # Validate file type
    allowed_types = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/heic', 'image/heif'}
    filename_lower = file.filename.lower() if file.filename else ''
    valid_extensions = filename_lower.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif'))

    if file.content_type not in allowed_types and not valid_extensions:
        logger.warning(f"Invalid file type: {file.content_type}, filename: {file.filename}")
        return jsonify({'success': False, 'error': 'Please upload an image file (JPEG, PNG, GIF, WEBP)'}), 400

    # Validate file size (max 5MB)
    MAX_UPLOAD_SIZE = 5 * 1024 * 1024
    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)

    if file_size == 0:
        return jsonify({'success': False, 'error': 'Empty file uploaded. Please select a valid image.'}), 400

    if file_size > MAX_UPLOAD_SIZE:
        return jsonify({
            'success': False,
            'error': f'File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)}MB.'
        }), 400

    try:
        image_content = file.read()

        # Validate image format and dimensions using PIL, then normalize to JPEG for Vision.
        # If PIL can't read a non-HEIC image, we log and still let Vision try to read it.
        from io import BytesIO
        try:
            try:
                from PIL import Image
            except ImportError:
                logger.error(
                    "Pillow is not installed (pip install Pillow). Image validation skipped; install in the web app venv."
                )
                raise
            img = Image.open(BytesIO(image_content))
            img.verify()
            img = Image.open(BytesIO(image_content))
            width, height = img.size

            if width > 4096 or height > 4096:
                return jsonify({
                    'success': False,
                    'error': 'Image dimensions too large. Maximum 4096x4096 pixels.'
                }), 400

            if width < 100 or height < 100:
                return jsonify({
                    'success': False,
                    'error': 'Image too small. Please upload a larger, readable screenshot.'
                }), 400

            # Normalize to JPEG so Vision API always gets a well-formed image (avoids format/EXIF issues)
            image_content = _normalize_image_to_jpeg(img)
        except Exception as img_error:
            logger.warning(
                f"Image validation failed for {file.filename}: {type(img_error).__name__}: {img_error}",
                exc_info=True,
            )
            if filename_lower.endswith(('.heic', '.heif')):
                return jsonify({
                    'success': False,
                    'error': 'HEIC format not fully supported. Please take a screenshot and save as JPEG or PNG.'
                }), 400
            # For non-HEIC images, fall through and let Vision API attempt to read the original bytes.

        # Validate with Vision API (using normalized JPEG bytes when available)
        result = _validate_deposit_screenshot(
            image_content,
            token_data.get('phone_number'),
            token_data.get('deposit_amount', 100),
            expected_reference=token_data.get('payment_reference'),
        )

        if result.get('valid'):
            return _handle_valid_deposit(short_code, token_data, result)
        else:
            return _handle_invalid_deposit(short_code, token_data, result)

    except Exception as e:
        logger.error(f"Upload processing error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': 'Error processing image. Please try again.'}), 500


def _normalize_image_to_jpeg(pil_image):
    """Convert PIL Image to JPEG bytes so Vision API receives a well-formed image (avoids format/EXIF issues)."""
    from io import BytesIO

    from PIL import Image
    if pil_image.mode in ('RGBA', 'LA'):
        background = Image.new('RGB', pil_image.size, (255, 255, 255))
        if pil_image.mode == 'LA':
            pil_image = pil_image.convert('RGBA')
        background.paste(pil_image, mask=pil_image.split()[-1])
        pil_image = background
    elif pil_image.mode != 'RGB':
        pil_image = pil_image.convert('RGB')
    buf = BytesIO()
    pil_image.save(buf, format='JPEG', quality=95)
    return buf.getvalue()


def _get_upload_token_data(short_code):
    """Get upload token data from short code."""
    if not short_code:
        return None

    try:
        db = get_shared_db(config.DATABASE_URL)

        # Get token data
        result = db.execute_query("""
            SELECT phone_number, deposit_amount, used, upload_attempts, payment_reference
            FROM upload_tokens
            WHERE short_code = %s
        """, (short_code.upper(),), fetch=True)

        if not result:
            return None

        row = result[0]
        from utils.row_utils import row_get
        phone_number = row_get(row, 'phone_number', row_get(row, 0))
        deposit_amount = row_get(row, 'deposit_amount', row_get(row, 1))
        used = row_get(row, 'used', row_get(row, 2))
        upload_attempts = row_get(row, 'upload_attempts', row_get(row, 3, 0))
        payment_reference = row_get(row, 'payment_reference', row_get(row, 4, None))

        # Check if used
        if used:
            return {'error': 'used', 'message': 'This upload link has already been used.'}

        # Deposit upload links do not expire by age — clients pay and upload on their own timeline.

        # Check attempts (max 3)
        attempts_remaining = max(0, 3 - upload_attempts)
        if attempts_remaining == 0:
            return {'error': 'invalid', 'message': 'Maximum upload attempts exceeded. Please request a new link.'}

        return {
            'phone_number': phone_number,
            'deposit_amount': deposit_amount,
            'attempts_remaining': attempts_remaining,
            'payment_reference': payment_reference,
        }

    except Exception as e:
        logger.error(f"Error getting upload token: {e}")
        return None


def _validate_deposit_screenshot(image_content, phone_number, required_amount=100, expected_reference=None):
    """Validate deposit screenshot using Vision API.
    
    Args:
        image_content: Raw image bytes
        phone_number: Client's phone number
        required_amount: Required deposit amount (for validation logic)
    """
    result: dict[str, Any] = {
        "valid": False,
        "deposit_amount": 0,
        "error": None,
        "details": {}
    }

    try:
        # Check if Vision API is available
        from services.vision_service import validate_deposit_screenshot_from_bytes

        # Call Vision API validation with the required amount
        validation_result = validate_deposit_screenshot_from_bytes(
            image_content,
            phone_number,
            required_amount=required_amount,
            expected_reference=expected_reference,
        )
        return validation_result

    except ImportError:
        logger.warning("Vision service not available - accepting deposit screenshot")
        # If Vision API not configured, accept the screenshot
        result["valid"] = True
        result["deposit_amount"] = required_amount
        return result

    except Exception as e:
        logger.error(f"Vision API error: {e}")
        result["error"] = str(e)
        return result


def _handle_valid_deposit(short_code, token_data, validation_result):
    """Handle a validated deposit screenshot."""
    _mark_upload_token_used(short_code)

    phone = token_data.get('phone_number')
    # Vision may return deposit_amount: null; dict.get still yields None when key is present.
    _raw_dep = validation_result.get('deposit_amount')
    if _raw_dep is None:
        _raw_dep = token_data.get('deposit_amount')
    token_fallback = _safe_int_money(token_data.get('deposit_amount'), 100)
    actual_deposit_amount = _safe_int_money(_raw_dep, token_fallback)

    try:
        db = get_shared_db(config.DATABASE_URL)

        # Get pending booking from conversation_states (escort_supply_source may be missing on older DBs)
        try:
            pending = db.execute_query("""
                SELECT client_name, date, time, duration, experience_type, incall_outcall,
                       outcall_address, COALESCE(confirmed_event_id, graphite_event_id, peacock_event_id) as event_id,
                       travel_outbound_event_id, travel_return_event_id,
                       total_booking_cost, price,
                       booking_status, escort_supply_source, booking_type,
                       mmf_exploration_tags, mmf_male_sourcing_escort_notified
                FROM conversation_states
                WHERE phone_number = %s
            """, (phone,), fetch=True)
        except Exception as e:
            err = str(e).lower()
            if "escort_supply_source" not in err and "does not exist" not in err:
                raise
            logger.info(
                "conversation_states has no escort_supply_source column; using deposit flow without it"
            )
            pending = db.execute_query("""
                SELECT client_name, date, time, duration, experience_type, incall_outcall,
                       outcall_address, COALESCE(confirmed_event_id, graphite_event_id, peacock_event_id) as event_id,
                       travel_outbound_event_id, travel_return_event_id,
                       total_booking_cost, price,
                       booking_status
                FROM conversation_states
                WHERE phone_number = %s
            """, (phone,), fetch=True)

        if pending:
            row = pending[0]
            from utils.row_utils import row_get
            client_name = row_get(row, 'client_name', row_get(row, 0))
            date_str = str(row_get(row, 'date', row_get(row, 1)))
            time_raw = row_get(row, 'time', row_get(row, 2))
            time_str = _format_time_value_for_confirmation(time_raw)
            experience_type = (row_get(row, 'experience_type', row_get(row, 4)) or '')
            booking_status = row_get(row, 'booking_status', row_get(row, 12, '')) or ''
            escort_supply_source = row_get(row, 'escort_supply_source', row_get(row, 13, '')) or ''

            # Update deposit status in conversation_states
            is_optional = token_fallback <= 50
            try:
                if is_optional:
                    db.execute_query("""
                        UPDATE conversation_states
                        SET deposit_paid = true,
                            deposit_amount = %s,
                            optional_deposit_paid = true,
                            optional_deposit_amount = %s
                        WHERE phone_number = %s
                    """, (actual_deposit_amount, actual_deposit_amount, phone))
                else:
                    db.execute_query("""
                        UPDATE conversation_states
                        SET deposit_paid = true,
                            deposit_amount = %s
                        WHERE phone_number = %s
                    """, (actual_deposit_amount, phone))
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)
                # Fallback if optional_deposit columns don't exist yet
                db.execute_query("""
                    UPDATE conversation_states
                    SET deposit_paid = true,
                        deposit_amount = %s
                    WHERE phone_number = %s
                """, (actual_deposit_amount, phone))

            pay_ref = (token_data.get("payment_reference") or "").strip()
            if pay_ref:
                try:
                    db.execute_query(
                        """
                        UPDATE conversation_states
                        SET deposit_payment_reference = %s
                        WHERE phone_number = %s
                        """,
                        (pay_ref, phone),
                        fetch=False,
                    )
                except Exception as e:
                    logger.warning("Could not persist deposit_payment_reference: %s", e)

            total_booking_cal = _booking_total_for_remaining_balance_sms(row, actual_deposit_amount)

            # Update calendar event to confirmed (Basil green); outcall travel LAVENDER→GRAPE first.
            try:
                from services.calendar_service import confirm_calendar_event, confirm_travel_time_blocks
                from utils.row_utils import row_get
                event_id = row_get(row, 'event_id', row_get(row, 7, None))
                travel_out = row_get(row, 'travel_outbound_event_id', None)
                travel_ret = row_get(row, 'travel_return_event_id', None)
                if event_id:
                    pay_ref_calendar = pay_ref or None
                    _loc_raw = (row_get(row, "incall_outcall", "") or "").strip().lower()
                    _is_outcall = _loc_raw == "outcall"
                    calendar_ok = False
                    travel_ok = True
                    if _is_outcall and (travel_out or travel_ret):
                        travel_ok = bool(
                            confirm_travel_time_blocks(
                                travel_out,
                                travel_ret,
                            )
                        )
                    if travel_ok:
                        calendar_ok = bool(
                            confirm_calendar_event(
                                event_id,
                                actual_deposit_amount,
                                client_name,
                                is_outcall=_is_outcall,
                                experience_type=(
                                    (str(experience_type).strip()) if experience_type else None
                                ),
                                payment_reference=pay_ref_calendar,
                                total_booking_cost=total_booking_cal,
                            )
                        )
                    elif _is_outcall:
                        logger.warning(
                            "Web deposit: travel LAVENDER→GRAPE failed; left main calendar unchanged phone=%s",
                            phone,
                        )
                    if calendar_ok:
                        try:
                            db.execute_query(
                                """
                                UPDATE conversation_states
                                SET confirmed_event_id = %s,
                                    graphite_event_id = NULL,
                                    peacock_event_id = NULL
                                WHERE phone_number = %s
                                """,
                                (event_id, phone),
                                fetch=False,
                            )
                        except Exception as _cid_err:
                            logger.warning(
                                "Deposit verified but could not sync confirmed_event_id: %s",
                                _cid_err,
                            )
            except Exception as e:
                logger.warning(f"Could not update calendar event: {e}")

            # Send confirmation SMS
            try:
                from services.sms_service import send_sms

                total_booking = total_booking_cal
                if total_booking is not None:
                    remaining_balance = max(0, total_booking - actual_deposit_amount)
                else:
                    remaining_balance = 0
                name_greeting = f"Hi {client_name}! " if client_name else ""
                confirmation_msg = f"\u2705 {name_greeting}${actual_deposit_amount} deposit verified!\n\n\U0001F4C5 {_fmt_sms_date(date_str)} at {time_str}\n\U0001F4B0 Remaining balance: ${remaining_balance}\n\nYour booking is now confirmed. Looking forward to seeing you! - {get_escort_name()}"
                send_sms(phone, confirmation_msg)
            except Exception as e:
                logger.error(f"Failed to send deposit confirmation SMS: {e}")

            # If escort must source second provider: MMF male (specific SMS) vs other doubles.
            try:
                from booking.mmf_exploration import (
                    decode_mmf_exploration_tags,
                    escort_organises_male_for_mmf,
                    humanize_mmf_exploration_tags,
                )

                _merge_escort = {
                    "booking_type": str(row_get(row, "booking_type", "") or ""),
                    "experience_type": str(experience_type or ""),
                    "booking_status": str(booking_status or ""),
                    "escort_supply_source": str(escort_supply_source or ""),
                    "mmf_exploration_tags": row_get(row, "mmf_exploration_tags", None),
                }
                _dur_row = row_get(row, "duration", row_get(row, 3))
                try:
                    _dur_min_web = int(_dur_row)
                except (TypeError, ValueError):
                    _dur_min_web = None
                _already_mmf_src = bool(row_get(row, "mmf_male_sourcing_escort_notified", False))

                is_doubles = any(
                    token in str(experience_type).lower()
                    for token in ("double", "doubles", "threesome", "mmf", "mff")
                )
                escort_must_source = (
                    str(booking_status).strip().lower() == "doubles_supply_escort"
                    or str(escort_supply_source).strip().lower() == "escort"
                )

                if (
                    escort_organises_male_for_mmf(_merge_escort)
                    and decode_mmf_exploration_tags(_merge_escort.get("mmf_exploration_tags"))
                    and not _already_mmf_src
                ):
                    from services.notification_service import notify_escort_mmf_male_source_required

                    _tags_web = decode_mmf_exploration_tags(_merge_escort.get("mmf_exploration_tags"))
                    notify_escort_mmf_male_source_required(
                        client_phone=phone,
                        client_name=client_name or "Client",
                        experience_type=str(experience_type),
                        booking_date=str(date_str),
                        booking_time=str(time_str),
                        duration_minutes=_dur_min_web,
                        exploration_summary=humanize_mmf_exploration_tags(_tags_web),
                    )
                    try:
                        db.execute_query(
                            """
                            UPDATE conversation_states
                            SET mmf_male_sourcing_escort_notified = TRUE
                            WHERE phone_number = %s
                            """,
                            (phone,),
                            fetch=False,
                        )
                    except Exception as _mmf_flag_err:
                        logger.warning(
                            "Could not set mmf_male_sourcing_escort_notified: %s", _mmf_flag_err
                        )
                elif is_doubles and escort_must_source:
                    from services.notification_service import notify_escort_doubles_source_required

                    notify_escort_doubles_source_required(
                        client_phone=phone,
                        client_name=client_name or "Client",
                        experience_type=str(experience_type),
                        booking_date=str(date_str),
                        booking_time=str(time_str),
                    )
            except Exception as _escort_alert_err:
                logger.warning(
                    "Failed to send doubles/MMF source escort alert from web upload flow: %s",
                    _escort_alert_err,
                )

            logger.info(f"Deposit verified for {phone}: ${actual_deposit_amount}")

        else:
            # No pending row still gets payment reference when token carries it (schedule / SMS).
            pay_ref = (token_data.get("payment_reference") or "").strip()
            if pay_ref:
                try:
                    db.execute_query(
                        """
                        UPDATE conversation_states
                        SET deposit_payment_reference = %s
                        WHERE phone_number = %s
                        """,
                        (pay_ref, phone),
                        fetch=False,
                    )
                except Exception as e:
                    logger.warning("Could not persist deposit_payment_reference (no pending row): %s", e)
            try:
                from services.sms_service import send_sms
                send_sms(phone, f"\u2705 ${actual_deposit_amount} deposit verified! Your booking is confirmed.")
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)

        from core.hmac_security import (
            BOOKING_CONFIRM_PAGE_TTL_SECONDS,
            GATEWAY_BOOKING_CONFIRM,
            generate_signed_token,
            register_token,
        )
        _tok = generate_signed_token(
            phone, GATEWAY_BOOKING_CONFIRM, ttl_seconds=BOOKING_CONFIRM_PAGE_TTL_SECONDS
        )
        if not register_token(db, _tok, GATEWAY_BOOKING_CONFIRM):
            logger.error(
                "Deposit verified but link_tokens registration failed; confirmation page falls back to HMAC-only."
            )
        # Query-string "+" must be percent-encoded (%2B); raw "+" is decoded as space and breaks verify_signed_token.
        _confirm_base = url_for("booking.booking_confirmation_page", phone_number=phone)
        redirect_url = _confirm_base + "?" + urlencode({"tok": _tok}, quote_via=quote)

        return jsonify({
            'success': True,
            'message': f"${actual_deposit_amount} deposit verified! Your booking is now confirmed.",
            'redirect_url': redirect_url,
        })

    except Exception as e:
        logger.error(f"Error handling valid deposit: {e}")
        return jsonify({'success': False, 'error': f'Error confirming deposit. Please contact {get_escort_name()}.'}), 500


def _handle_invalid_deposit(short_code, token_data, result):
    """Handle an invalid deposit screenshot."""
    token_data.get('phone_number')

    try:
        db = get_shared_db(config.DATABASE_URL)

        # Increment upload attempts
        db.execute_query("""
            UPDATE upload_tokens
            SET upload_attempts = upload_attempts + 1
            WHERE short_code = %s
        """, (short_code.upper(),))

        # Get new attempts remaining
        attempts_result = db.execute_query("""
            SELECT upload_attempts FROM upload_tokens WHERE short_code = %s
        """, (short_code.upper(),), fetch=True)

        attempts = 0
        if attempts_result:
            attempts = attempts_result[0]['upload_attempts'] if isinstance(attempts_result[0], dict) else attempts_result[0][0]

        attempts_remaining = max(0, 3 - attempts)

        error_message = result.get('error', 'Could not verify deposit screenshot. Please ensure the image shows the full payment confirmation including PayID, amount, and date.')

        if attempts_remaining == 0:
            error_message += f" Maximum attempts exceeded. Please text {get_escort_name()} for a new upload link."

        return jsonify({
            'success': False,
            'error': error_message,
            'attempts_remaining': attempts_remaining
        }), 400

    except Exception as e:
        logger.error(f"Error handling invalid deposit: {e}")
        return jsonify({'success': False, 'error': 'Error processing upload. Please try again.'}), 500


def _mark_upload_token_used(short_code):
    """Mark an upload token as used."""
    if not short_code:
        return False

    try:
        db = get_shared_db(config.DATABASE_URL)
        db.execute_query("""
            UPDATE upload_tokens
            SET used = true, used_at = NOW()
            WHERE short_code = %s AND used = false
        """, (short_code.upper(),))
        return True
    except Exception as e:
        logger.warning(f"Failed to mark upload token as used: {e}")
        return False
