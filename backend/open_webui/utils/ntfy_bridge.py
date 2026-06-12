"""
In-process ntfy -> native-web-push bridge for Alfie.

A long-lived asyncio background task (started from the app lifespan) that
subscribes to the self-hosted ntfy server's per-user JSON message stream, maps
each message's ntfy topic to the owning OpenWebUI user, and re-delivers it as a
native web push via the existing ``send_web_push`` (utils/webpush.py). This
unifies push: opencode/terminal approvals and pod ``agent:end`` completions —
which already publish to ntfy — now arrive as native web push, exactly like chat
does. ntfy becomes a hidden transport; the publishers stay byte-for-byte
unchanged.

All configuration is read lazily from the environment (matching webpush.py's
``os.environ.get`` convention) so enabling/disabling and remapping is env-only —
no code change. The master switch is ``ALFIE_NTFY_BRIDGE_ENABLED``; when false
the task is a clean no-op (the rollback lever). The wiring in main.py is
unconditional.

Topic strings and the bridge token are SECRET; this module never logs them in
full (topics are redacted to a short prefix; the token is never logged).
"""

import asyncio
import json
import logging
import os
from typing import Optional

import aiohttp
from open_webui.env import DATA_DIR
from open_webui.models.users import Users
from open_webui.utils.webpush import send_web_push

log = logging.getLogger(__name__)


# Default suppress set: the one routine "turn idle" event. Everything else
# (approvals, questions, errors, "Hermes finished" completions) is an
# attention-event and is forwarded.
DEFAULT_SUPPRESS_TITLES = 'opencode turn finished'

# Cursor file: persists the last-handled ntfy message id so a reconnect resumes
# from where we left off instead of replaying the cache. DATA_DIR maps to a
# persistent Docker named volume.
CURSOR_PATH = DATA_DIR / 'ntfy_bridge.cursor'

# Bounded in-memory dedupe of recently-seen message ids.
DEDUPE_CAP = 512

# Reconnect backoff bounds (seconds).
BACKOFF_MIN = 3
BACKOFF_MAX = 30


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, 'True' if default else 'False').lower() == 'true'


def _csv(value: Optional[str]) -> list[str]:
    """Split a comma-separated env value, stripping whitespace and dropping empties."""
    if not value:
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


def _redact_topic(topic: str) -> str:
    """Redact a secret topic string for logging (short prefix + ellipsis)."""
    if len(topic) <= 6:
        return '…'
    return f'{topic[:6]}…'


def _load_cursor() -> Optional[str]:
    """Return the persisted last message id, or None if absent/unreadable."""
    try:
        with open(CURSOR_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        last_id = data.get('last_id')
        return last_id if isinstance(last_id, str) and last_id else None
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning(f'ntfy bridge: could not read cursor file, starting fresh: {e}')
        return None


def _save_cursor(last_id: str) -> None:
    """Persist the last-handled message id atomically (temp file + os.replace)."""
    try:
        tmp_path = f'{CURSOR_PATH}.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump({'last_id': last_id}, f)
        os.replace(tmp_path, CURSOR_PATH)
    except Exception as e:
        log.warning(f'ntfy bridge: failed to persist cursor: {e}')


def _build_topic_map(emails: list[str], topics: list[str]) -> Optional[dict[str, str]]:
    """Resolve email<->topic pairs into ``{exact_topic: user_id}``.

    Returns None on a fatal misconfiguration (length mismatch). Returns a
    possibly-empty dict otherwise (empty is the expected no-config state).
    """
    if not topics:
        # No topics configured (e.g. staging, where NTFY_TOPIC_* are unset so
        # TOPICS renders empty). Idle cleanly via the empty-map path rather than
        # treating it as a length-mismatch misconfiguration (which would log a
        # spurious ERROR on every staging boot).
        return {}
    if len(emails) != len(topics):
        log.error(
            'ntfy bridge: EMAILS and TOPICS lengths differ after dropping empties '
            f'({len(emails)} emails vs {len(topics)} topics) — refusing to guess a mapping'
        )
        return None
    return {}


async def _resolve_topic_map(emails: list[str], topics: list[str]) -> Optional[dict[str, str]]:
    """Build the ``{exact_topic: user_id}`` map, resolving each email to a user."""
    topic_map = _build_topic_map(emails, topics)
    if topic_map is None:
        return None

    for email, topic in zip(emails, topics):
        user = await Users.get_user_by_email(email)
        if user is None:
            log.warning(f'ntfy bridge: no OpenWebUI user for a configured email — skipping topic {_redact_topic(topic)}')
            continue
        # Key on the EXACT topic string (never a prefix/glob).
        topic_map[topic] = user.id

    return topic_map


async def _stream_once(
    base_url: str,
    topics_csv: str,
    headers: dict,
    since: Optional[str],
    topic_map: dict[str, str],
    suppress_titles: set[str],
    seen_ids: dict,
) -> None:
    """Open one streaming subscription and process lines until it ends.

    Raises ``asyncio.CancelledError`` through for clean shutdown; other
    exceptions propagate to the caller's reconnect/backoff loop.
    """
    url = f'{base_url}/{topics_csv}/json'
    params = {'since': since} if since else None

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status in (401, 403):
                # Auth/token problem — surface clearly, then let the caller back off.
                log.error(f'ntfy bridge: subscribe rejected (status={resp.status}); check ALFIE_NTFY_BRIDGE_TOKEN')
                raise RuntimeError(f'ntfy subscribe auth failed: {resp.status}')
            resp.raise_for_status()

            log.info(f'ntfy bridge: subscribed to {len(topic_map)} topic(s)')

            async for raw_line in resp.content:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    # A malformed line must not kill the loop.
                    log.warning('ntfy bridge: skipping unparseable stream line')
                    continue

                await _handle_message(msg, topic_map, suppress_titles, seen_ids)


async def _handle_message(
    msg: dict,
    topic_map: dict[str, str],
    suppress_titles: set[str],
    seen_ids: dict,
) -> None:
    """Route a single ntfy stream object to web push (message events only)."""
    if not isinstance(msg, dict):
        return

    # Ignore non-message events (open/keepalive/poll_request).
    if msg.get('event') != 'message':
        return

    msg_id = msg.get('id')
    if not isinstance(msg_id, str) or not msg_id:
        return

    # Dedupe by id against a bounded set (evict oldest).
    if msg_id in seen_ids:
        return
    seen_ids[msg_id] = None
    while len(seen_ids) > DEDUPE_CAP:
        seen_ids.pop(next(iter(seen_ids)))

    topic = msg.get('topic')
    user_id = topic_map.get(topic) if isinstance(topic, str) else None
    if user_id is None:
        # No EXACT topic match — not ours.
        return

    title = msg.get('title') or ''
    # Per-message INFO log (id + title only). The secret topic is never logged;
    # the body is never logged (it may carry sensitive command/question text).
    # This line is the server-side observable the deploy verify probe correlates
    # its synthetic nonce against (the nonce rides the message title).
    log.info(f'ntfy bridge: consumed msg id={msg_id} title={title!r}')
    if title in suppress_titles:
        # Routine event — advance the cursor and skip delivery.
        _save_cursor(msg_id)
        return

    # Fetch the user FRESH so newly-added phone subscriptions are seen.
    user = await Users.get_user_by_id(user_id)
    if user is None:
        _save_cursor(msg_id)
        return

    body = msg.get('message') or ''
    click = msg.get('click') or None

    try:
        await send_web_push(
            user,
            title,
            body,
            url=click,
            data={'source': 'ntfy-bridge'},
        )
    except Exception as e:
        # One delivery failure must not kill the stream.
        log.warning(f'ntfy bridge: send_web_push failed for a message: {e}')

    # Persist the cursor after handling (delivered or suppressed).
    _save_cursor(msg_id)


async def run_ntfy_bridge(app) -> None:
    """Long-lived bridge task: ntfy JSON stream -> native web push.

    Behaviour is fully gated on ``ALFIE_NTFY_BRIDGE_ENABLED`` so the lifespan
    wiring can stay unconditional. Reconnects with capped backoff on transient
    failures and resumes from the persisted cursor.
    """
    if not _env_bool('ALFIE_NTFY_BRIDGE_ENABLED', False):
        log.info('ntfy bridge disabled')
        return

    base_url = (os.environ.get('ALFIE_NTFY_BASE_URL') or 'http://ntfy').rstrip('/')
    token = os.environ.get('ALFIE_NTFY_BRIDGE_TOKEN') or None

    emails = _csv(os.environ.get('ALFIE_NTFY_BRIDGE_EMAILS'))
    topics = _csv(os.environ.get('ALFIE_NTFY_BRIDGE_TOPICS'))

    suppress_env = os.environ.get('ALFIE_NTFY_BRIDGE_SUPPRESS_TITLES')
    if suppress_env is None:
        suppress_titles = set(_csv(DEFAULT_SUPPRESS_TITLES))
    else:
        suppress_titles = set(_csv(suppress_env))

    topic_map = await _resolve_topic_map(emails, topics)
    if topic_map is None:
        # Fatal misconfiguration already logged at ERROR — do not guess.
        return
    if not topic_map:
        log.info('ntfy bridge: no users mapped, idle')
        return

    headers = {'Authorization': f'Bearer {token}'} if token else {}
    topics_csv = ','.join(topic_map.keys())
    # Shared across reconnects so a flapping connection doesn't re-deliver.
    seen_ids: dict = {}

    backoff = BACKOFF_MIN
    while True:
        since = _load_cursor()
        try:
            await _stream_once(
                base_url,
                topics_csv,
                headers,
                since,
                topic_map,
                suppress_titles,
                seen_ids,
            )
            # Stream ended cleanly (server closed) — reconnect promptly.
            backoff = BACKOFF_MIN
        except asyncio.CancelledError:
            # Clean shutdown — re-raise so the lifespan can cancel us.
            raise
        except Exception as e:
            log.warning(f'ntfy bridge: stream error, reconnecting in {backoff}s: {e}')
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
