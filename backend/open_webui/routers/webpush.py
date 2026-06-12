"""
Web Push (VAPID) subscription + public-key router for Alfie.

Endpoints:
  * ``GET  /api/v1/webpush/vapid-public-key`` — public, returns the
    ``applicationServerKey`` the browser needs to subscribe.
  * ``POST /api/v1/webpush/subscribe`` — auth required; merges the posted browser
    ``PushSubscription`` into the caller's
    ``user.settings.ui.notifications.push_subscriptions`` (dedupe by endpoint).
  * ``DELETE /api/v1/webpush/subscribe`` — auth required; removes a subscription
    by endpoint.

Subscriptions are stored in user settings JSON — no DB migration. Persistence
reuses ``Users.update_user_settings_by_id`` (the same path the settings-update
endpoint uses), so a stored subscription survives exactly like any other UI
setting.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from open_webui.models.users import Users
from open_webui.utils.auth import get_verified_user
from open_webui.utils.webpush import get_public_key
from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)

router = APIRouter()


############################
# GetVapidPublicKey
############################


@router.get('/vapid-public-key')
async def get_vapid_public_key():
    # Public: the browser needs this before it can create a subscription. The
    # public key is non-secret (it is, by design, shipped to every client).
    return {'publicKey': get_public_key()}


############################
# Subscribe
############################


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str
    model_config = ConfigDict(extra='allow')


class PushSubscription(BaseModel):
    """A browser PushSubscription as produced by ``registration.pushManager``."""

    endpoint: str
    keys: PushSubscriptionKeys
    expirationTime: Optional[Any] = None
    model_config = ConfigDict(extra='allow')


def _notifications_with_subscriptions(settings) -> tuple[dict, dict, list]:
    """Return (ui, notifications, subscriptions) as plain copies from settings."""
    ui = {}
    if settings is not None:
        raw_ui = settings.ui if hasattr(settings, 'ui') else settings.get('ui')
        if isinstance(raw_ui, dict):
            ui = dict(raw_ui)
    notifications = dict(ui.get('notifications') or {})
    subscriptions = list(notifications.get('push_subscriptions') or [])
    return ui, notifications, subscriptions


@router.post('/subscribe')
async def subscribe(form_data: PushSubscription, user=Depends(get_verified_user)):
    subscription = form_data.model_dump(exclude_none=True)
    endpoint = subscription.get('endpoint')
    if not endpoint:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Missing subscription endpoint',
        )

    ui, notifications, subscriptions = _notifications_with_subscriptions(user.settings)

    # Dedupe by endpoint: drop any existing entry for this endpoint, then append
    # the fresh one (keys can rotate).
    subscriptions = [s for s in subscriptions if isinstance(s, dict) and s.get('endpoint') != endpoint]
    subscriptions.append(subscription)

    notifications['push_subscriptions'] = subscriptions
    ui['notifications'] = notifications

    updated = await Users.update_user_settings_by_id(user.id, {'ui': ui})
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to store push subscription',
        )

    return {'success': True, 'count': len(subscriptions)}


############################
# Unsubscribe
############################


class UnsubscribeForm(BaseModel):
    endpoint: str


@router.delete('/subscribe')
async def unsubscribe(form_data: UnsubscribeForm, user=Depends(get_verified_user)):
    ui, notifications, subscriptions = _notifications_with_subscriptions(user.settings)

    remaining = [s for s in subscriptions if isinstance(s, dict) and s.get('endpoint') != form_data.endpoint]

    if len(remaining) == len(subscriptions):
        # Nothing matched — idempotent success.
        return {'success': True, 'count': len(remaining)}

    notifications['push_subscriptions'] = remaining
    ui['notifications'] = notifications

    updated = await Users.update_user_settings_by_id(user.id, {'ui': ui})
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Failed to remove push subscription',
        )

    return {'success': True, 'count': len(remaining)}
