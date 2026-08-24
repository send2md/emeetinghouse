"""Meetinghouse: an online emulation of a Quaker Meeting for Business.

The Meetinghouse has three subsections that share common Topic/Poll
mechanics: Forum (ordinary decisions), Rules (changing the Rules
themselves), and Jury (temporarily dismissing a Participant).

Time is passed in explicitly as a `now` argument to every action,
rather than read from the system clock inside this module. A caller
(a web handler, a periodic job, a test) supplies `now`; this keeps the
whole engine deterministic and easy to test by simply constructing
later datetimes, and keeps scheduling, persistence, and delivery of
the Daily Summary as someone else's job.

A few places in the design spec were ambiguous and required a
judgment call, documented at the point of the decision:

* A Rule is modeled as four numbers per subsection (quiet time,
  minimum life, and the two pass-criteria fractions), since those are
  the only Rule properties the spec actually quantifies.
* Revoking a Vote does not restart the Quiet-time clock, since it is a
  withdrawal of activity rather than new activity.
* "Voters" are distinct Participants with a live vote of any choice;
  "Readers" include anyone who has read the Topic or voted on it.
* A Topic/Poll with no Readers, or no live votes, FAILS rather than
  raising an error.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set


class Subsection(Enum):
    """Which of the Meetinghouse's three subsections a Topic belongs to."""

    FORUM = "Forum"
    RULES = "Rules"
    JURY = "Jury"


class VoteChoice(Enum):
    """The three ways a Participant may Vote on a Poll."""

    YES = "Yes"
    ABSTAIN = "Abstain"
    NO = "No"


class PollResult(Enum):
    """The outcome of an evaluated Poll."""

    PASSED = "PASSED"
    FAILED = "FAILED"


class EmeetinghouseError(Exception):
    """Base class for every domain error this module raises."""


class NotAParticipant(EmeetinghouseError):
    """Raised when a person without a current, signed form tries to act."""


class ParticipantDismissed(EmeetinghouseError):
    """Raised when a currently-dismissed Participant tries to act."""


class TopicClosed(EmeetinghouseError):
    """Raised when an action targets a Topic that has already closed."""


class NoPollOnTopic(EmeetinghouseError):
    """Raised when a Vote is attempted on a Topic that carries no Poll."""


class InvalidTopic(EmeetinghouseError):
    """Raised when a Topic is created without the fields its subsection requires."""


@dataclass(frozen=True)
class RuleConfig:
    """The tunable Rule governing one subsection.

    A Topic captures a snapshot of its subsection's RuleConfig at the
    moment it is created, and follows that snapshot for its entire
    lifetime, even if the Rule changes while the Topic is still open.
    """

    quiet_time: dt.timedelta
    min_life: dt.timedelta
    voter_fraction: float
    approval_fraction: float

    def __post_init__(self):
        if not 0 <= self.voter_fraction <= 1:
            raise ValueError("voter_fraction must be in [0, 1]")
        if not 0 <= self.approval_fraction <= 1:
            raise ValueError("approval_fraction must be in [0, 1]")


def DefaultRules():
    """Build the starting RuleConfig for each subsection, per the spec."""
    three_weeks = dt.timedelta(weeks=3)
    return {
        Subsection.FORUM: RuleConfig(
            quiet_time=dt.timedelta(weeks=1),
            min_life=three_weeks,
            voter_fraction=1 / 4,
            approval_fraction=2 / 3,
        ),
        Subsection.RULES: RuleConfig(
            quiet_time=dt.timedelta(weeks=2),
            min_life=three_weeks,
            voter_fraction=1 / 2,
            approval_fraction=51 / 100,
        ),
        Subsection.JURY: RuleConfig(
            quiet_time=dt.timedelta(weeks=2),
            min_life=three_weeks,
            voter_fraction=1 / 2,
            approval_fraction=1 / 5,
        ),
    }


@dataclass
class ParticipationForm:
    """A signed, snail-mailed form, scanned into the archive."""

    signed_date: dt.date
    scan_ref: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class Dismissal:
    """One period of temporary dismissal, imposed by a passed Jury Poll."""

    start: dt.datetime
    end: dt.datetime
    ordinal: int
    topic_id: str


@dataclass
class Participant:
    """A person who has signed a participation form for the Meetinghouse."""

    name: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    forms: List[ParticipationForm] = field(default_factory=list)
    dismissals: List[Dismissal] = field(default_factory=list)

    def SignForm(self, signed_date, scan_ref=""):
        """Record a freshly signed and scanned participation form."""
        form = ParticipationForm(signed_date=signed_date, scan_ref=scan_ref)
        self.forms.append(form)
        return form

    def LatestFormDate(self):
        """Return the date of this Participant's most recently signed form."""
        if not self.forms:
            return None
        return max(form.signed_date for form in self.forms)

    def HasCurrentForm(self, now):
        """Return whether the latest signed form is less than a year old."""
        latest_date = self.LatestFormDate()
        if latest_date is None:
            return False
        return now.date() - latest_date <= dt.timedelta(days=365)

    def IsDismissed(self, now):
        """Return whether this Participant is currently under dismissal."""
        return any(dismissal.start <= now < dismissal.end for dismissal in self.dismissals)

    def CurrentDismissal(self, now):
        """Return the Dismissal in effect right now, if any."""
        for dismissal in self.dismissals:
            if dismissal.start <= now < dismissal.end:
                return dismissal
        return None

    def NextDismissalDuration(self):
        """Return how long a new dismissal would last: 2 weeks, doubling each time."""
        if not self.dismissals:
            return dt.timedelta(weeks=2)
        most_recent = self.dismissals[-1]
        return (most_recent.end - most_recent.start) * 2

    def IsParticipant(self, now):
        """Return whether this person currently holds valid Participant status."""
        return self.HasCurrentForm(now)

    def IsActive(self, now):
        """Return whether this person may currently vote, comment, or author."""
        return self.IsParticipant(now) and not self.IsDismissed(now)


@dataclass
class VoteRecord:
    """One Vote, with its required explanatory comment.

    Revoking a Vote sets `revoked`; the record stays in the archive.
    """

    participant_id: str
    choice: VoteChoice
    comment: str
    timestamp: dt.datetime
    revoked: bool = False
    revoked_at: Optional[dt.datetime] = None


@dataclass
class Topic:
    """A named subject of discussion, with an optional Poll to vote on."""

    id: str
    subsection: Subsection
    author_id: str
    title: str
    description: Optional[str]
    poll_question: Optional[str]
    created_at: dt.datetime
    rules: RuleConfig
    target_subsection: Optional[Subsection] = None
    target_participant_id: Optional[str] = None
    proposed_rule_config: Optional[RuleConfig] = None
    readers: Dict[str, dt.datetime] = field(default_factory=dict)
    votes: Dict[str, List[VoteRecord]] = field(default_factory=dict)
    closed: bool = False
    closed_at: Optional[dt.datetime] = None
    result: Optional[PollResult] = None
    last_activity_at: dt.datetime = field(default=None)

    def __post_init__(self):
        if self.last_activity_at is None:
            self.last_activity_at = self.created_at

    def HasPoll(self):
        """Return whether this Topic carries a Poll question."""
        return self.poll_question is not None

    def CurrentVote(self, participant_id):
        """Return the given Participant's live (non-revoked) Vote, if any."""
        for record in reversed(self.votes.get(participant_id, [])):
            if not record.revoked:
                return record
        return None

    def RawVotes(self):
        """Return every Participant's live Vote, ignoring dismissal status."""
        results = []
        for history in self.votes.values():
            for record in reversed(history):
                if not record.revoked:
                    results.append(record)
                    break
        return results


class Emeetinghouse:
    """The engine coordinating Participants, Topics, Polls, and Rules."""

    def __init__(self, rules=None):
        self.participants: Dict[str, Participant] = {}
        self.topics: Dict[str, Topic] = {}
        self.current_rules: Dict[Subsection, RuleConfig] = rules or DefaultRules()
        self.event_log: List[dict] = []

    def RegisterParticipant(self, name):
        """Create and track a new Participant, with no signed form yet."""
        participant = Participant(name=name)
        self.participants[participant.id] = participant
        return participant

    def _RequireActive(self, participant_id, now):
        """Return the named Participant, raising if they may not currently act."""
        participant = self.participants.get(participant_id)
        if participant is None or not participant.IsParticipant(now):
            raise NotAParticipant(f"{participant_id} is not a current Participant")
        if participant.IsDismissed(now):
            dismissal = participant.CurrentDismissal(now)
            raise ParticipantDismissed(
                f"{participant.name} is dismissed until {dismissal.end.isoformat()}"
            )
        return participant

    def CreateTopic(
        self,
        author_id,
        subsection,
        title,
        now,
        description=None,
        poll_question=None,
        target_subsection=None,
        target_participant_id=None,
        proposed_rule_config=None,
    ):
        """Author a new Topic, enforcing each subsection's required fields."""
        author = self._RequireActive(author_id, now)

        if subsection is Subsection.RULES and poll_question is None:
            raise InvalidTopic("Every Rules Topic must carry a Poll.")
        if subsection is Subsection.RULES and target_subsection is None:
            raise InvalidTopic(
                "A Rules Topic's author must declare which subsection the Rule addresses."
            )
        if subsection is Subsection.JURY:
            if target_participant_id is None or target_participant_id not in self.participants:
                raise InvalidTopic("A Jury Topic must name an existing Participant.")
            poll_question = "Do we dismiss them?"
            title = self.participants[target_participant_id].name

        topic = Topic(
            id=str(uuid.uuid4()),
            subsection=subsection,
            author_id=author.id,
            title=title,
            description=description,
            poll_question=poll_question,
            created_at=now,
            rules=self.current_rules[subsection],
            target_subsection=target_subsection,
            target_participant_id=target_participant_id,
            proposed_rule_config=proposed_rule_config,
        )
        self.topics[topic.id] = topic
        self._Log("topic_created", now, topic_id=topic.id, author_id=author.id)
        self.MarkRead(topic.id, author.id, now)
        return topic

    def MarkRead(self, topic_id, participant_id, now):
        """Record that the given Participant has read this Topic."""
        topic = self._GetOpenTopic(topic_id)
        self._RequireActive(participant_id, now)
        topic.readers.setdefault(participant_id, now)

    def CastVote(self, topic_id, participant_id, choice, comment, now):
        """Cast, or replace, a Participant's Vote on this Topic's Poll.

        Every Vote must carry a comment explaining it.
        """
        if not comment or not comment.strip():
            raise InvalidTopic("Each Vote/ReVote must be accompanied by a comment.")
        topic = self._GetOpenTopic(topic_id)
        if not topic.HasPoll():
            raise NoPollOnTopic("This Topic has no Poll to vote on.")
        self._RequireActive(participant_id, now)

        topic.readers.setdefault(participant_id, now)
        record = VoteRecord(
            participant_id=participant_id, choice=choice, comment=comment, timestamp=now
        )
        topic.votes.setdefault(participant_id, []).append(record)
        topic.last_activity_at = now
        self._Log(
            "vote_cast", now, topic_id=topic_id, participant_id=participant_id, choice=choice.value
        )
        return record

    def RevokeVote(self, topic_id, participant_id, now):
        """Revoke a Participant's current Vote, returning them to Reader status.

        The revoked record stays in the archive, marked revoked, and does
        not restart the subsection's Quiet-time clock.
        """
        topic = self._GetOpenTopic(topic_id)
        self._RequireActive(participant_id, now)
        record = topic.CurrentVote(participant_id)
        if record is None:
            return
        record.revoked = True
        record.revoked_at = now
        self._Log("vote_revoked", now, topic_id=topic_id, participant_id=participant_id)

    def _GetOpenTopic(self, topic_id):
        """Return the named Topic, raising if it does not exist or has closed."""
        topic = self.topics.get(topic_id)
        if topic is None:
            raise InvalidTopic(f"No such Topic: {topic_id}")
        if topic.closed:
            raise TopicClosed(f"Topic {topic_id} is already closed.")
        return topic

    def EffectiveReaders(self, topic, now):
        """Return this Topic's Readers, excluding currently-dismissed Participants."""
        return {
            participant_id
            for participant_id in topic.readers
            if participant_id in self.participants
            and not self.participants[participant_id].IsDismissed(now)
        }

    def EffectiveVoteCounts(self, topic, now):
        """Return live Vote counts, excluding currently-dismissed Participants."""
        counts = {choice: 0 for choice in VoteChoice}
        for participant_id, history in topic.votes.items():
            record = next((r for r in reversed(history) if not r.revoked), None)
            if record is None:
                continue
            participant = self.participants.get(participant_id)
            if participant is None or participant.IsDismissed(now):
                continue
            counts[record.choice] += 1
        return counts

    def _EligibleForClosure(self, topic, now):
        """Return whether this Topic has satisfied its minimum life and Quiet time."""
        if topic.closed or not topic.HasPoll():
            return False
        if now - topic.created_at < topic.rules.min_life:
            return False
        return now - topic.last_activity_at >= topic.rules.quiet_time

    def Evaluate(self, topic, now):
        """Compute PASSED or FAILED for this Topic's Poll, per its Rule snapshot."""
        readers = self.EffectiveReaders(topic, now)
        counts = self.EffectiveVoteCounts(topic, now)
        voters = sum(counts.values())

        if not readers:
            return PollResult.FAILED
        if voters / len(readers) < topic.rules.voter_fraction:
            return PollResult.FAILED

        favorable = counts[VoteChoice.YES] + counts[VoteChoice.ABSTAIN]
        total_decided = favorable + counts[VoteChoice.NO]
        if total_decided == 0:
            return PollResult.FAILED

        approval = favorable / total_decided
        if approval >= topic.rules.approval_fraction:
            return PollResult.PASSED
        return PollResult.FAILED

    def Tick(self, now):
        """Close and evaluate every Topic whose Quiet time has elapsed.

        Returns the list of Topics closed by this call.
        """
        newly_closed = []
        for topic in self.topics.values():
            if self._EligibleForClosure(topic, now):
                self._CloseTopic(topic, now)
                newly_closed.append(topic)
        return newly_closed

    def _CloseTopic(self, topic, now):
        """Evaluate, close, and apply the side effects of a decided Topic."""
        topic.result = self.Evaluate(topic, now)
        topic.closed = True
        topic.closed_at = now
        self._Log("topic_closed", now, topic_id=topic.id, result=topic.result.value)

        if topic.result is not PollResult.PASSED:
            return
        if topic.subsection is Subsection.RULES:
            self._ApplyRuleChange(topic, now)
        if topic.subsection is Subsection.JURY:
            self._ApplyDismissal(topic, now)

    def _ApplyRuleChange(self, topic, now):
        """Install a passed Rules Topic's proposed RuleConfig for future Topics."""
        if topic.proposed_rule_config is not None:
            self.current_rules[topic.target_subsection] = topic.proposed_rule_config
        self._Log(
            "rule_changed", now, topic_id=topic.id, target_subsection=topic.target_subsection.value
        )

    def _ApplyDismissal(self, topic, now):
        """Record a new Dismissal for a passed Jury Topic's target Participant."""
        participant = self.participants[topic.target_participant_id]
        ordinal = len(participant.dismissals) + 1
        duration = participant.NextDismissalDuration()
        dismissal = Dismissal(start=now, end=now + duration, ordinal=ordinal, topic_id=topic.id)
        participant.dismissals.append(dismissal)
        self._Log(
            "participant_dismissed",
            now,
            topic_id=topic.id,
            participant_id=participant.id,
            ordinal=ordinal,
            duration_days=duration.days,
        )

    def SearchArchive(self, query):
        """Return every Topic whose title, description, Poll, or comments match."""
        needle = query.lower()
        hits = []
        for topic in self.topics.values():
            comments = (record.comment for history in topic.votes.values() for record in history)
            haystack = " ".join(
                filter(None, [topic.title, topic.description or "", topic.poll_question or "", *comments])
            ).lower()
            if needle in haystack:
                hits.append(topic)
        return hits

    def DailySummary(self, participant_id, now):
        """Build one Participant's Daily Summary: open Topics, and Topics closed today."""
        self._RequireActive(participant_id, now)
        today = now.date()

        open_items = []
        for topic in self.topics.values():
            if topic.closed:
                continue
            counts = self.EffectiveVoteCounts(topic, now)
            open_items.append(
                {
                    "topic_id": topic.id,
                    "subsection": topic.subsection.value,
                    "title": topic.title,
                    "age": now - topic.created_at,
                    "votes": {choice.value: count for choice, count in counts.items()},
                }
            )

        closed_today = []
        for topic in self.topics.values():
            if topic.closed and topic.closed_at.date() == today:
                closed_today.append(
                    {
                        "topic_id": topic.id,
                        "subsection": topic.subsection.value,
                        "title": topic.title,
                        "result": topic.result.value,
                    }
                )

        return {"date": today.isoformat(), "open": open_items, "closed_today": closed_today}

    def _Log(self, kind, now, **fields):
        """Append one entry to the in-memory event log, for archival/audit use."""
        self.event_log.append({"kind": kind, "at": now, **fields})
