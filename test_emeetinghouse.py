"""Tests for emeetinghouse.py, one per Rule in the design spec.

Run with `python -m pytest test_emeetinghouse.py -v`, or run this file
directly and it will execute every test itself, with no dependency on
pytest being installed.
"""

import datetime as dt

from emeetinghouse import (
    Dismissal,
    InvalidTopic,
    Emeetinghouse,
    NotAParticipant,
    ParticipantDismissed,
    PollResult,
    RuleConfig,
    Subsection,
    VoteChoice,
)

START = dt.datetime(2026, 1, 1, 12, 0, 0)


def Days(count):
    """Build a timedelta of the given number of days, for readability."""
    return dt.timedelta(days=count)


def CreateHouseWithParticipants(count=8, now=START):
    """Build an Emeetinghouse with `count` Participants, each freshly signed."""
    house = Emeetinghouse()
    people = [house.RegisterParticipant(f"Participant {i}") for i in range(count)]
    for person in people:
        person.SignForm(signed_date=now.date() - Days(1))
    return house, people


def test_form_expires_after_a_year():
    house = Emeetinghouse()
    alice = house.RegisterParticipant("Alice")
    alice.SignForm(signed_date=START.date())
    assert alice.IsParticipant(START + Days(300))
    assert not alice.IsParticipant(START + Days(366))


def test_cannot_act_without_current_form():
    house = Emeetinghouse()
    alice = house.RegisterParticipant("Alice")
    try:
        house.CreateTopic(alice.id, Subsection.FORUM, "Buy new chairs", START)
        assert False, "should have raised"
    except NotAParticipant:
        pass


def test_forum_topic_cannot_close_before_three_weeks():
    house, people = CreateHouseWithParticipants(4)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Repaint the hall", START,
        poll_question="Shall we repaint the hall?",
    )
    for person in people:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Sounds good.", START)

    closed = house.Tick(START + Days(14))
    assert topic not in closed
    assert not topic.closed


def test_forum_topic_closes_and_passes_after_quiet_time():
    house, people = CreateHouseWithParticipants(4)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Repaint the hall", START,
        poll_question="Shall we repaint the hall?",
    )
    for person in people:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Sounds good.", START)

    now = START + Days(21) + Days(7) + dt.timedelta(hours=1)
    closed = house.Tick(now)
    assert topic in closed
    assert topic.closed
    assert topic.result is PollResult.PASSED


def test_forum_fails_on_low_turnout():
    house, people = CreateHouseWithParticipants(8)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "New logo", START, poll_question="Adopt the new logo?"
    )
    for person in people[1:]:
        house.MarkRead(topic.id, person.id, START)
    house.CastVote(topic.id, author.id, VoteChoice.YES, "Looks nice.", START)

    now = START + Days(21) + Days(7) + dt.timedelta(hours=1)
    house.Tick(now)
    assert topic.result is PollResult.FAILED


def test_forum_fails_on_too_much_opposition():
    house, people = CreateHouseWithParticipants(6)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Change the logo", START, poll_question="Adopt the new logo?"
    )
    for person in people[:3]:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "I like it.", START)
    for person in people[3:]:
        house.CastVote(topic.id, person.id, VoteChoice.NO, "I do not like it.", START)

    now = START + Days(21) + Days(7) + dt.timedelta(hours=1)
    house.Tick(now)
    assert topic.result is PollResult.FAILED


def test_activity_restarts_quiet_clock():
    house, people = CreateHouseWithParticipants(3)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Buy a rug", START,
        poll_question="Buy a rug for the entryway?",
    )

    vote_time = START + Days(20)
    for person in people:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Fine by me.", vote_time)

    almost_quiet = vote_time + Days(6) + dt.timedelta(hours=23)
    house.Tick(almost_quiet)
    assert not topic.closed

    house.CastVote(
        topic.id, people[0].id, VoteChoice.YES, "Still in favor, adding a note.", almost_quiet
    )

    would_have_closed = almost_quiet + dt.timedelta(hours=2)
    house.Tick(would_have_closed)
    assert not topic.closed

    house.Tick(almost_quiet + Days(7) + dt.timedelta(hours=1))
    assert topic.closed


def test_rules_topic_requires_target_subsection_and_poll():
    house, people = CreateHouseWithParticipants(4)
    try:
        house.CreateTopic(people[0].id, Subsection.RULES, "Loosen Forum quiet time", START)
        assert False, "should require a poll and target subsection"
    except InvalidTopic:
        pass


def test_passed_rule_change_only_affects_future_topics():
    house, people = CreateHouseWithParticipants(4)
    author = people[0]

    new_forum_rules = RuleConfig(
        quiet_time=dt.timedelta(days=1),
        min_life=dt.timedelta(weeks=3),
        voter_fraction=1 / 4,
        approval_fraction=2 / 3,
    )
    rules_topic = house.CreateTopic(
        author.id, Subsection.RULES, "Shorten Forum quiet time to 1 day", START,
        poll_question="Shall we shorten the Forum quiet time to 1 day?",
        target_subsection=Subsection.FORUM,
        proposed_rule_config=new_forum_rules,
    )
    old_forum_topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Old regime topic", START, poll_question="Approve?"
    )

    for person in people:
        house.CastVote(rules_topic.id, person.id, VoteChoice.YES, "Agreed.", START)

    close_time = START + Days(21) + Days(14) + dt.timedelta(hours=1)
    house.Tick(close_time)
    assert rules_topic.closed and rules_topic.result is PollResult.PASSED
    assert house.current_rules[Subsection.FORUM].quiet_time == dt.timedelta(days=1)

    new_forum_topic = house.CreateTopic(
        author.id, Subsection.FORUM, "New regime topic", close_time, poll_question="Approve?"
    )
    assert new_forum_topic.rules.quiet_time == dt.timedelta(days=1)
    assert old_forum_topic.rules.quiet_time == dt.timedelta(weeks=1)


def test_jury_topic_dismisses_participant_and_excludes_from_live_counts():
    house, people = CreateHouseWithParticipants(6)
    author = people[0]
    target = people[1]

    jury_topic = house.CreateTopic(
        author.id, Subsection.JURY, target.name, START, target_participant_id=target.id
    )
    assert jury_topic.poll_question == "Do we dismiss them?"

    forum_topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Buy new chairs", START, poll_question="Approve?"
    )
    house.CastVote(forum_topic.id, target.id, VoteChoice.NO, "I disagree.", START)

    for person in people[:3]:
        house.CastVote(jury_topic.id, person.id, VoteChoice.YES, "Time for a break.", START)

    close_time = START + Days(21) + Days(14) + dt.timedelta(hours=1)
    house.CastVote(
        forum_topic.id, people[2].id, VoteChoice.YES, "Still in favor.", close_time - Days(1)
    )
    house.Tick(close_time)

    assert jury_topic.closed and jury_topic.result is PollResult.PASSED
    assert not forum_topic.closed
    assert target.IsDismissed(close_time)
    assert len(target.dismissals) == 1
    assert target.dismissals[0].end - target.dismissals[0].start == dt.timedelta(weeks=2)

    counts = house.EffectiveVoteCounts(forum_topic, close_time)
    assert counts[VoteChoice.NO] == 0
    assert forum_topic.CurrentVote(target.id) is not None

    try:
        house.CastVote(forum_topic.id, target.id, VoteChoice.YES, "Trying anyway.", close_time)
        assert False, "should have raised"
    except ParticipantDismissed:
        pass
    try:
        house.CreateTopic(target.id, Subsection.FORUM, "Sneaky new topic", close_time)
        assert False, "should have raised"
    except ParticipantDismissed:
        pass


def test_second_dismissal_doubles_duration():
    house, people = CreateHouseWithParticipants(4)
    target = people[1]
    target.dismissals.append(
        Dismissal(start=START, end=START + dt.timedelta(weeks=2), ordinal=1, topic_id="fake")
    )
    assert target.NextDismissalDuration() == dt.timedelta(weeks=4)


def test_resuming_after_dismissal_requires_new_form():
    house, people = CreateHouseWithParticipants(4)
    target = people[1]
    dismissal_end = START + dt.timedelta(weeks=2)
    target.dismissals.append(
        Dismissal(start=START, end=dismissal_end, ordinal=1, topic_id="x")
    )

    just_after = dismissal_end + dt.timedelta(minutes=1)
    assert not target.IsDismissed(just_after)
    assert target.IsActive(just_after)


def test_search_archive_finds_topics_by_comment_text():
    house, people = CreateHouseWithParticipants(3)
    topic = house.CreateTopic(
        people[0].id, Subsection.FORUM, "Kitchen renovation", START,
        poll_question="Approve the renovation?",
    )
    house.CastVote(
        topic.id, people[1].id, VoteChoice.YES,
        "The dishwasher has been broken for months.", START,
    )
    hits = house.SearchArchive("dishwasher")
    assert topic in hits
    assert house.SearchArchive("nonexistent-term") == []


def test_daily_summary_lists_open_and_recently_closed():
    house, people = CreateHouseWithParticipants(3)
    author = people[0]
    topic = house.CreateTopic(
        author.id, Subsection.FORUM, "Buy a kettle", START, poll_question="Approve?"
    )
    for person in people:
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Sure.", START)

    close_time = START + Days(21) + Days(7) + dt.timedelta(hours=1)
    house.Tick(close_time)

    summary = house.DailySummary(author.id, close_time)
    assert summary["closed_today"][0]["topic_id"] == topic.id
    assert summary["open"] == []


def RunAllTests():
    """Run every test function in this module and report pass/fail counts."""
    import sys
    import traceback

    tests = [
        (name, value) for name, value in list(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failure_count = 0
    for name, test_function in tests:
        try:
            test_function()
            print(f"PASS  {name}")
        except Exception:
            failure_count += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failure_count}/{len(tests)} tests passed")
    sys.exit(1 if failure_count else 0)


if __name__ == "__main__":
    RunAllTests()
