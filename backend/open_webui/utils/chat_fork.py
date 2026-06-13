"""Conversation forking — build a new chat's message subset from a source chat.

Alfie fork feature (council B2): "Fork from here". Given a source chat's
``history`` blob (the ``{messages: {id: msg}, currentId}`` tree that OpenWebUI
stores in ``chat.chat``) and a target ``message_id``, produce a *new* history
blob carrying the conversation up to (and including) that message.

All real logic lives here so the router/model hot paths stay tiny (fork
discipline — see docs/DEVELOPMENT.md churn map). Pure functions, no DB / no
auth: the caller (routers/chats.py) is responsible for scoping the source chat
to the authenticated user before calling in.

History modes
-------------
The chat is stored as a full message *tree* (each node has ``parentId`` and
``childrenIds``). "Up to message X" is interpreted against that tree:

- ``path``  — the direct root→X ancestor chain only. Linear, ChatGPT/Claude
              "branch from here". Default.
- ``siblings`` — the ancestor chain plus, at each step on the path, the sibling
              branches (alternate edits/regenerations) that diverge *at or
              before* X, including their descendant subtrees. The path frontier
              (X and its ancestors) keeps the branch points; ``currentId`` still
              points at the forked copy of X so the chat opens on the chosen
              branch.
- ``full``  — the entire tree (equivalent to a plain clone), with
              ``currentId`` repointed at the forked copy of X.

All three are supported because OpenWebUI's chat model *does* track branches
(the ``childrenIds`` tree). The IDs of copied messages are preserved (the tree
is internally consistent and ``currentId`` is valid in every mode), matching
how the existing ``/clone`` endpoint copies the blob verbatim.
"""

from __future__ import annotations

import copy

FORK_MODES = ('path', 'siblings', 'full')


def _ancestor_path_ids(messages: dict, message_id: str) -> list[str]:
    """Return root→message_id id chain by walking ``parentId`` (cycle-safe)."""
    chain: list[str] = []
    seen: set[str] = set()
    current = messages.get(message_id)
    while current is not None:
        cid = current.get('id')
        if cid is None or cid in seen:
            break
        seen.add(cid)
        chain.append(cid)
        parent_id = current.get('parentId')
        current = messages.get(parent_id) if parent_id else None
    chain.reverse()
    return chain


def _collect_subtree_ids(messages: dict, root_id: str) -> set[str]:
    """Return root_id plus all descendant ids (cycle-safe BFS over childrenIds)."""
    collected: set[str] = set()
    stack = [root_id]
    while stack:
        node_id = stack.pop()
        if node_id in collected or node_id not in messages:
            continue
        collected.add(node_id)
        for child_id in messages[node_id].get('childrenIds') or []:
            if child_id not in collected:
                stack.append(child_id)
    return collected


def _select_message_ids(messages: dict, message_id: str, mode: str) -> set[str]:
    """Pick which message ids to carry into the fork, per mode."""
    path_ids = _ancestor_path_ids(messages, message_id)
    if not path_ids:
        return set()

    if mode == 'path':
        return set(path_ids)

    if mode == 'full':
        return set(messages.keys())

    # mode == 'siblings': the path, plus every *off-path* subtree hanging off an
    # ancestor of X (alternate edits/regenerations that diverge strictly before
    # X). X itself is the fork frontier: its own descendants come *after* X and
    # are never included, and the on-path continuation child of each ancestor is
    # skipped too. currentId stays on X.
    on_path = set(path_ids)
    selected: set[str] = set(path_ids)
    for pid in path_ids[:-1]:  # exclude X (the last id) — its subtree is "after"
        for child_id in messages.get(pid, {}).get('childrenIds') or []:
            if child_id in on_path:
                continue  # the path continuation — already counted, not a sibling
            selected |= _collect_subtree_ids(messages, child_id)
    return selected


def build_forked_history(history: dict, message_id: str, mode: str = 'path') -> dict | None:
    """Build a new ``history`` blob forked from ``message_id``.

    Returns ``{messages: {...}, currentId: <message_id>}`` with the selected
    subset deep-copied and parent/child links pruned to stay internally
    consistent, or ``None`` if ``message_id`` is absent from the tree.

    Message ids are preserved (like ``/clone``). The returned dict is freshly
    constructed — the caller may mutate it freely.
    """
    if mode not in FORK_MODES:
        mode = 'path'

    messages = (history or {}).get('messages') or {}
    if message_id not in messages:
        return None

    keep = _select_message_ids(messages, message_id, mode)
    if not keep:
        return None

    new_messages: dict[str, dict] = {}
    for mid in keep:
        src = messages.get(mid)
        if not isinstance(src, dict):
            continue
        # Deep copy + prune link arrays to the kept set so the tree stays
        # internally consistent (no dangling parent/child references) AND the
        # fork shares NO nested references with the source — editing the fork's
        # content/meta can never mutate the original chat (makes the docstring's
        # immutability guarantee real, not just latent-safe).
        copied = copy.deepcopy(src)
        parent_id = copied.get('parentId')
        copied['parentId'] = parent_id if parent_id in keep else None
        copied['childrenIds'] = [cid for cid in (copied.get('childrenIds') or []) if cid in keep]
        new_messages[mid] = copied

    return {'messages': new_messages, 'currentId': message_id}
