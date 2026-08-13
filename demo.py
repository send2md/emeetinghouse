#!/usr/bin/env python3
"""A runnable, narrated walkthrough of a Forum decision, a Rules change,
and a Jury dismissal.

Run: python3 demo.py, or ./demo.py if this file is executable.
"""

import datetime as dt

from meetinghouse import Meetinghouse, RuleConfig, Subsection, VoteChoice

START = dt.datetime(2026, 1, 1, 9, 0, 0)


def PrintDivider():
    """Print a horizontal rule between demo sections."""
    print("-" * 70)


def BuildHouse():
    """Create a Meetinghouse with four freshly signed Participants."""
    house = Meetinghouse()
    alice = house.RegisterParticipant("Alice")
    bob = house.RegisterParticipant("Bob")
    carol = house.RegisterParticipant("Carol")
    dave = house.RegisterParticipant("Dave")
    for person in (alice, bob, carol, dave):
        person.SignForm(signed_date=START.date())
    return house, alice, bob, carol, dave


def RunForumExample(house, alice, bob, carol, dave):
    """Alice proposes repainting the hall; the Forum approves it."""
    PrintDivider()
    print("1) Alice proposes a Forum Topic: repaint the meeting hall.")
    topic = house.CreateTopic(
        alice.id, Subsection.FORUM, "Repaint the meeting hall", START,
        description="The hall hasn't been painted in 12 years.",
        poll_question="Shall we repaint the meeting hall?",
    )
    house.CastVote(topic.id, bob.id, VoteChoice.YES, "Long overdue.", START)
    house.CastVote(
        topic.id, carol.id, VoteChoice.ABSTAIN, "No strong opinion, but won't block it.", START
    )
    house.CastVote(topic.id, dave.id, VoteChoice.YES, "Agreed.", START)

    close_time = START + dt.timedelta(weeks=4)
    house.Tick(close_time)
    print(f"   Result: {topic.result}  "
          f"(voters/readers={len(topic.votes)}/{len(topic.readers)})")
    return close_time


def RunRulesExample(house, alice, bob, carol, dave):
    """The group raises the Jury approval threshold from 1/5 to 1/2."""
    PrintDivider()
    print(
        "2) The group decides Jury's bar for dismissal (1/5 approval) feels "
        "low, and raises it to 1/2 via the Rules subsection."
    )
    new_jury_rules = RuleConfig(
        quiet_time=dt.timedelta(weeks=2),
        min_life=dt.timedelta(weeks=3),
        voter_fraction=1 / 2,
        approval_fraction=1 / 2,
    )
    topic = house.CreateTopic(
        alice.id, Subsection.RULES, "Raise the Jury approval threshold to 1/2", START,
        description="1/5 feels too easy to dismiss someone.",
        poll_question="Shall we raise the Jury approval threshold to 1/2?",
        target_subsection=Subsection.JURY,
        proposed_rule_config=new_jury_rules,
    )
    for person in (alice, bob, carol, dave):
        house.CastVote(topic.id, person.id, VoteChoice.YES, "Agreed, safer bar.", START)

    close_time = START + dt.timedelta(weeks=5)
    house.Tick(close_time)
    print(f"   Result: {topic.result}")
    print(
        "   New Jury approval_fraction is now: "
        f"{house.current_rules[Subsection.JURY].approval_fraction}"
    )
    return close_time, new_jury_rules


def RunJuryExample(house, alice, bob, carol, dave, start_time, new_jury_rules):
    """A Jury Topic dismisses Dave, who then cannot vote while dismissed."""
    PrintDivider()
    print("3) Someone raises a Jury Topic about Dave.")
    topic = house.CreateTopic(
        alice.id, Subsection.JURY, "", start_time, target_participant_id=dave.id
    )
    house.CastVote(topic.id, alice.id, VoteChoice.YES, "Repeated disruptions.", start_time)
    house.CastVote(topic.id, bob.id, VoteChoice.YES, "Agreed.", start_time)
    house.CastVote(topic.id, carol.id, VoteChoice.NO, "Too harsh, disagree.", start_time)

    close_time = start_time + dt.timedelta(weeks=5)
    house.Tick(close_time)
    print(f"   Poll question: {topic.poll_question!r}")
    print(
        f"   Result: {topic.result}  "
        f"(2 Yes / 1 No out of 3 readers = {2 / 3:.2f} approval, "
        f"needed {new_jury_rules.approval_fraction})"
    )
    if dave.IsDismissed(close_time):
        dismissal = dave.CurrentDismissal(close_time)
        days = (dismissal.end - dismissal.start).days
        print(
            f"   Dave is dismissed from {dismissal.start.date()} to "
            f"{dismissal.end.date()} ({days} days)."
        )
    return close_time


def RunDismissedVoteAttempt(house, alice, dave, now):
    """Show Dave being rejected when he tries to vote while dismissed."""
    PrintDivider()
    print("4) While dismissed, Dave tries to vote on a new Forum topic -- rejected:")
    topic = house.CreateTopic(alice.id, Subsection.FORUM, "Buy new chairs", now, poll_question="Approve?")
    try:
        house.CastVote(topic.id, dave.id, VoteChoice.YES, "I want to vote.", now)
    except Exception as error:
        print(f"   -> {type(error).__name__}: {error}")


def PrintDailySummary(house, alice, now):
    """Print Alice's Daily Summary right after the Jury Topic closes."""
    PrintDivider()
    print("5) Daily summary for Alice, right after Jury closes:")
    summary = house.DailySummary(alice.id, now)
    for item in summary["closed_today"]:
        title = item["title"] or "(Jury: Dave)"
        print(f"   [closed today] {item['subsection']}: {title} -> {item['result']}")
    for item in summary["open"]:
        print(
            f"   [open] {item['subsection']}: {item['title']} "
            f"age={item['age'].days}d votes={item['votes']}"
        )


def PrintArchiveSearch(house):
    """Print every Topic whose text mentions 'disruptions'."""
    PrintDivider()
    print("6) Archive search for 'disruptions':")
    for topic in house.SearchArchive("disruptions"):
        print(f"   Found Topic {topic.id} in {topic.subsection.value}: {topic.title!r}")


def main():
    """Run the full demo, section by section.

    Named lower-case, breaking the FunctionLabels rule, so that this
    file follows Python's own `if __name__ == "__main__": main()`
    convention.
    """
    house, alice, bob, carol, dave = BuildHouse()
    RunForumExample(house, alice, bob, carol, dave)
    rules_close_time, new_jury_rules = RunRulesExample(house, alice, bob, carol, dave)
    jury_close_time = RunJuryExample(
        house, alice, bob, carol, dave, rules_close_time, new_jury_rules
    )
    RunDismissedVoteAttempt(house, alice, dave, jury_close_time)
    PrintDailySummary(house, alice, jury_close_time)
    PrintArchiveSearch(house)


if __name__ == "__main__":
    main()
