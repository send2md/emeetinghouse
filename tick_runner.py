"""The close-and-persist flow behind Meetinghouse.Tick().

Factored out of app.py so it can run two ways in production: inside
the Flask process on hosts that can keep a background thread alive,
or as a one-shot cron invocation (see tick.py) on hosts like TigerTech
shared hosting, where a FastCGI worker's lifetime isn't ours to
control and a thread inside it can't be relied on to keep running.
"""

from __future__ import annotations

import persistence
from emeetinghouse import PollResult, Subsection


def RunTick(house, conn, now):
    """Close every eligible Topic/Poll, persist the results, and return them."""
    prev_len = len(house.event_log)
    closed = house.Tick(now)
    for topic in closed:
        persistence.SaveTopic(conn, topic)
        if topic.result is PollResult.PASSED and topic.subsection is Subsection.RULES and topic.target_subsection:
            persistence.SaveRules(conn, topic.target_subsection, house.current_rules[topic.target_subsection])
        if topic.result is PollResult.PASSED and topic.subsection is Subsection.JURY:
            participant = house.participants[topic.target_participant_id]
            persistence.SaveParticipant(conn, participant)
    for entry in house.event_log[prev_len:]:
        persistence.LogEvent(conn, entry)
    return closed
