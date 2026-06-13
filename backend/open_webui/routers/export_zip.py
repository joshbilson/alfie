"""
Markdown ZIP conversation export router for Alfie (council B22).

Endpoint:
  * ``GET /api/v1/export/zip`` — auth required; streams a ``.zip`` archive
    containing one readable Markdown file per conversation owned by the caller,
    plus an ``index.md`` table of contents linking each file.

This COMPLEMENTS the existing JSON export (``GET /api/v1/chats/all`` →
``application/x-ndjson``): the JSON export is a machine-readable backup/import
format, while this is a human-readable Markdown archive. The two do not collide
(different prefix, different content-type, different shape).

A2 isolation: scoped strictly to ``user.id`` — only the authenticated caller's
own chats are ever read (``Chats.get_chats_by_user_id(user.id)``).

No new dependencies: the archive is built in memory with the stdlib
``zipfile`` + ``io.BytesIO``.
"""

import io
import logging
import re
import time
import zipfile

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from open_webui.models.chats import Chats
from open_webui.utils.auth import get_verified_user
from open_webui.utils.misc import get_content_from_message, get_message_list

log = logging.getLogger(__name__)

router = APIRouter()

# Cap the total UNCOMPRESSED markdown built in memory so a pathological history
# can't spike the open-webui container on the shared box (the ZIP is assembled in
# RAM). Generous for a family stack (kilobyte–low-MB chats); past it we stop and
# note the truncation in index.md rather than OOM. Override via env if ever needed.
_MAX_TOTAL_BYTES = 50 * 1024 * 1024


# Filenames are reserved on Windows and must not collide with index.md or use
# path separators. We keep this conservative so the archive is portable.
_UNSAFE_FILENAME_CHARS = re.compile(r'[^A-Za-z0-9._ \-]+')
_WINDOWS_RESERVED = {
    'con', 'prn', 'aux', 'nul',
    *(f'com{i}' for i in range(1, 10)),
    *(f'lpt{i}' for i in range(1, 10)),
}


def _sanitize_filename_stem(title: str, fallback: str) -> str:
    """Turn an arbitrary chat title into a safe, portable filename stem.

    - strips path separators / control chars / anything not in a safe set,
    - collapses whitespace, trims leading/trailing dots+spaces (Windows hates
      trailing dots),
    - guards against empty / reserved (con, nul, …) names,
    - caps length so very long titles don't blow the 255-byte name limit.

    De-duplication across the archive is handled by the caller.
    """
    stem = (title or '').strip()
    stem = _UNSAFE_FILENAME_CHARS.sub('_', stem)
    stem = re.sub(r'\s+', ' ', stem).strip()
    stem = stem.strip('. ')

    if not stem or stem.lower() in _WINDOWS_RESERVED:
        stem = fallback

    # Leave headroom for a " (N)" dedupe suffix + the ".md" extension.
    return stem[:120]


def _render_chat_markdown(title: str, messages: list[dict], updated_at: int | None) -> str:
    """Render one conversation as readable Markdown: a title heading, a metadata
    line, then a ``### Role`` heading + content block per message in order."""
    lines: list[str] = [f'# {title or "Untitled chat"}', '']

    if updated_at:
        try:
            stamp = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(int(updated_at)))
            lines.append(f'*Last updated: {stamp}*')
            lines.append('')
        except (ValueError, OSError, OverflowError):
            pass

    if not messages:
        lines.append('_(no messages)_')
        lines.append('')

    for message in messages:
        role = (message.get('role') or 'unknown').strip().capitalize()
        content = get_content_from_message(message)
        if content is None:
            content = ''
        lines.append(f'### {role}')
        lines.append('')
        lines.append(content if content.strip() else '_(empty message)_')
        lines.append('')

    return '\n'.join(lines)


############################
# ExportChatsAsMarkdownZip
############################


@router.get('/zip')
async def export_chats_as_markdown_zip(user=Depends(get_verified_user)):
    """Stream a ZIP of one Markdown file per chat + an index.md TOC.

    A2: only the caller's own chats — ``Chats.get_chats_by_user_id(user.id)``.
    """
    # A2 scoping: this is the ONLY chat source; it filters by user_id in SQL.
    chat_list = await Chats.get_chats_by_user_id(user.id)
    chats = chat_list.items

    buffer = io.BytesIO()
    index_lines: list[str] = ['# Chat Export', '']
    index_lines.append(
        f'{len(chats)} conversation(s), exported '
        f'{time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}.'
    )
    index_lines.append('')

    used_names: set[str] = set()
    total_bytes = 0
    truncated = 0

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for chat in chats:
            chat_data = chat.chat if isinstance(chat.chat, dict) else {}
            history = chat_data.get('history') or {}
            messages_map = history.get('messages') or {}
            current_id = history.get('currentId')

            messages = get_message_list(messages_map, current_id)
            # Fall back to insertion order if the chain couldn't be rebuilt but
            # messages exist (e.g. a chat saved without a currentId).
            if not messages and messages_map:
                messages = list(messages_map.values())

            stem = _sanitize_filename_stem(chat.title, fallback=f'chat-{chat.id}')

            # De-dup: "index" is reserved for the TOC; append " (N)" on clash.
            candidate = stem
            suffix = 1
            while candidate.lower() in used_names or candidate.lower() == 'index':
                suffix += 1
                candidate = f'{stem} ({suffix})'
            used_names.add(candidate.lower())

            filename = f'{candidate}.md'
            markdown = _render_chat_markdown(chat.title, messages, chat.updated_at)
            mbytes = len(markdown.encode('utf-8'))
            # Stop before exceeding the in-memory cap; record how many were skipped.
            if total_bytes + mbytes > _MAX_TOTAL_BYTES and total_bytes > 0:
                truncated = len(chats) - len(used_names)
                break
            total_bytes += mbytes
            zf.writestr(filename, markdown)

            # TOC entry: link the file (URL-encode spaces) + show the raw title.
            link_target = filename.replace(' ', '%20')
            index_lines.append(f'- [{chat.title or "Untitled chat"}]({link_target})')

        if truncated > 0:
            index_lines.append('')
            index_lines.append(
                f'> _Note: {truncated} more conversation(s) omitted — export hit the '
                f'{_MAX_TOTAL_BYTES // (1024 * 1024)} MB size cap. Export in smaller '
                f'batches or use the JSON export for a complete archive._'
            )
        index_lines.append('')
        zf.writestr('index.md', '\n'.join(index_lines))

    payload = buffer.getvalue()
    download_name = f'alfie-chat-export-{int(time.time())}.zip'

    return Response(
        content=payload,
        media_type='application/zip',
        headers={
            'Content-Disposition': f'attachment; filename="{download_name}"',
            'Content-Length': str(len(payload)),
        },
    )
