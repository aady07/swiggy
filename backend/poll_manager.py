from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

ResolveFn = Callable[[int, str, dict[str, Any]], Awaitable[Any]]
BroadcastFn = Callable[[], Awaitable[None]]


@dataclass
class ActivePoll:
    poll_id: str
    chat_id: int
    draft_item_id: int
    options: list[dict[str, Any]]
    votes: dict[int, int] = field(default_factory=dict)
    finalized: bool = False
    timeout_task: asyncio.Task | None = None


class PollManager:
    def __init__(self) -> None:
        self._polls: dict[str, ActivePoll] = {}

    def register(self, poll: ActivePoll) -> None:
        self._polls[poll.poll_id] = poll

    def get(self, poll_id: str) -> ActivePoll | None:
        return self._polls.get(poll_id)

    def record_vote(self, poll_id: str, user_id: int, option_index: int) -> ActivePoll | None:
        poll = self._polls.get(poll_id)
        if not poll or poll.finalized:
            return None
        poll.votes[user_id] = option_index
        return poll

    def winner_index(self, poll: ActivePoll) -> int | None:
        if not poll.votes:
            return None
        counts = Counter(poll.votes.values())
        best = counts.most_common(1)[0][0]
        return best

    def remove(self, poll_id: str) -> None:
        poll = self._polls.pop(poll_id, None)
        if poll and poll.timeout_task and not poll.timeout_task.done():
            poll.timeout_task.cancel()


poll_manager = PollManager()
