"""
Native Web Push (VAPID) sender for Alfie.

Alfie carries a single installed PWA; this module replaces the separate ntfy PWA
by pushing chat/channel notifications straight to the browser's Push API. It is
called beside the existing ``post_webhook`` hooks (middleware completion sites and
channel ``send_notification``) whenever a user is inactive, so the wiring at the
hot sites stays a one-line, additive call.

Subscriptions live in the user's settings — no DB migration — at
``user.settings.ui.notifications.push_subscriptions`` as a list of browser
``PushSubscription`` JSON dicts. Dead endpoints (HTTP 404/410) are pruned and the
cleaned list is persisted back through ``Users.update_user_settings_by_id``.

VAPID key format (the orchestrator generates keys to match this exactly):
  * ``WEBPUSH_VAPID_PRIVATE_KEY`` — the EC P-256 private key, in EITHER of:
      - base64url-encoded raw 32-byte private scalar (preferred; what
        ``py_vapid.Vapid.from_string`` consumes directly), OR
      - a PEM ``-----BEGIN PRIVATE KEY-----`` block (auto-detected here and fed
        through ``Vapid.from_pem``; newlines may be literal or ``\\n``-escaped).
  * ``WEBPUSH_VAPID_PUBLIC_KEY`` — base64url-encoded (no padding) uncompressed
    P-256 public point (65 bytes, ``0x04`` prefix). This is the exact value the
    browser passes as ``applicationServerKey`` and is returned verbatim by the
    public-key endpoint.
  * ``WEBPUSH_VAPID_SUBJECT`` — the VAPID ``sub`` claim, e.g.
    ``mailto:joshbilson@gmail.com``. Optional; defaults to a mailto below.

This module never raises into its caller: every failure path is logged and
swallowed so a push problem can never break a chat completion.
"""

import asyncio
import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)


DEFAULT_VAPID_SUBJECT = 'mailto:notifications@alfie.local'


def _vapid_private_key() -> Optional[str]:
    """Return the configured VAPID private key, normalizing PEM newlines.

    Env transports (compose, op-run-wrapped) commonly carry PEM blocks with
    literal ``\\n`` escapes; convert those to real newlines so ``Vapid.from_pem``
    accepts the value. base64url raw keys are returned untouched.
    """
    key = os.environ.get('WEBPUSH_VAPID_PRIVATE_KEY')
    if not key:
        return None
    key = key.strip()
    if 'BEGIN' in key and '\\n' in key:
        key = key.replace('\\n', '\n')
    return key


def is_configured() -> bool:
    """True when both VAPID keys are present in the environment."""
    return bool(os.environ.get('WEBPUSH_VAPID_PRIVATE_KEY') and os.environ.get('WEBPUSH_VAPID_PUBLIC_KEY'))


def get_public_key() -> Optional[str]:
    """The base64url applicationServerKey served to browsers, or None if unset."""
    return os.environ.get('WEBPUSH_VAPID_PUBLIC_KEY')


def _resolve_vapid_private_key(raw_key: str):
    """Build the value pywebpush expects for ``vapid_private_key``.

    pywebpush feeds a plain string through ``Vapid.from_string``, which only
    handles base64url raw/DER — not PEM. So when a PEM block is supplied we
    construct the ``Vapid`` instance ourselves via ``from_pem`` and hand that
    object to ``webpush`` (it accepts a ``Vapid`` instance directly). Otherwise
    we pass the base64url string straight through.
    """
    if 'BEGIN' in raw_key:
        from py_vapid import Vapid

        return Vapid.from_pem(raw_key.encode('utf-8'))
    return raw_key


def _subscription_endpoint(subscription: Any) -> Optional[str]:
    if isinstance(subscription, dict):
        return subscription.get('endpoint')
    return None


def _send_one(subscription: dict, payload: str, vapid_private_key, vapid_claims: dict) -> tuple[bool, Optional[int]]:
    """Blocking single-subscription send.

    Returns ``(ok, status_code)``. ``status_code`` is the HTTP status from the
    push service when the push library raised a ``WebPushException`` (used to
    decide 404/410 pruning); ``None`` otherwise.
    """
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=vapid_private_key,
            vapid_claims=dict(vapid_claims),
            ttl=600,
        )
        return True, None
    except WebPushException as e:
        status_code = getattr(getattr(e, 'response', None), 'status_code', None)
        # 404/410 = subscription gone; INFO not ERROR — expected churn.
        if status_code in (404, 410):
            log.info(f'web push subscription expired ({status_code}); pruning endpoint')
        else:
            log.warning(f'web push failed (status={status_code}): {e}')
        return False, status_code
    except Exception as e:
        log.warning(f'web push send error: {e}')
        return False, None


async def send_web_push(
    user,
    title: str,
    body: str,
    url: Optional[str] = None,
    data: Optional[dict] = None,
) -> None:
    """Send a web-push notification to every active subscription of ``user``.

    No-ops cleanly (and never raises) when VAPID is unconfigured or the user has
    no subscriptions. Endpoints that the push service reports as gone (404/410)
    are pruned and the cleaned list is persisted back to the user's settings.

    ``user`` is an ``open_webui.models.users.UserModel`` (as passed at the hook
    sites). ``data`` is merged into the payload's ``data`` object and is read by
    the service worker's ``notificationclick`` handler.
    """
    try:
        if not is_configured():
            return

        # user.settings is a UserSettings pydantic model (ui: dict) at the hook
        # sites; tolerate a plain dict too for robustness.
        settings = getattr(user, 'settings', None)
        if settings is None:
            return
        ui = settings.ui if hasattr(settings, 'ui') else settings.get('ui')
        if not isinstance(ui, dict):
            return
        notifications = ui.get('notifications') or {}
        subscriptions = notifications.get('push_subscriptions') or []
        if not isinstance(subscriptions, list) or not subscriptions:
            return

        raw_key = _vapid_private_key()
        if not raw_key:
            return

        try:
            vapid_private_key = _resolve_vapid_private_key(raw_key)
        except Exception as e:
            log.error(f'invalid WEBPUSH_VAPID_PRIVATE_KEY — web push disabled this call: {e}')
            return

        subject = os.environ.get('WEBPUSH_VAPID_SUBJECT') or DEFAULT_VAPID_SUBJECT

        payload = json.dumps(
            {
                'title': title,
                'body': body,
                'url': url,
                'data': data or {},
            }
        )

        surviving: list = []
        pruned = False

        for subscription in subscriptions:
            endpoint = _subscription_endpoint(subscription)
            if not endpoint:
                # Malformed entry — drop it.
                pruned = True
                continue

            claims = {'sub': subject}
            ok, status_code = await asyncio.to_thread(
                _send_one, subscription, payload, vapid_private_key, claims
            )

            if ok:
                surviving.append(subscription)
            elif status_code in (404, 410):
                # Gone — prune.
                pruned = True
            else:
                # Transient/unknown failure — keep the subscription for next time.
                surviving.append(subscription)

        if pruned:
            await _persist_subscriptions(user, ui, notifications, surviving)

    except Exception as e:
        # Absolute guarantee: never raise into a chat-completion hook.
        log.warning(f'send_web_push swallowed unexpected error: {e}')


async def _persist_subscriptions(user, ui: dict, notifications: dict, surviving: list) -> None:
    """Write the pruned subscription list back to the user's settings JSON."""
    try:
        from open_webui.models.users import Users

        new_notifications = dict(notifications)
        new_notifications['push_subscriptions'] = surviving

        new_ui = dict(ui)
        new_ui['notifications'] = new_notifications

        await Users.update_user_settings_by_id(user.id, {'ui': new_ui})
        log.info(f'pruned {user.id} web push subscriptions -> {len(surviving)} remaining')
    except Exception as e:
        log.warning(f'failed to persist pruned web push subscriptions for {getattr(user, "id", "?")}: {e}')
