"""SQLite persistence for the Meetinghouse engine.

emeetinghouse.py is a pure, in-memory domain engine by design (see its
module docstring): every action takes an explicit `now` and nothing in
it touches a clock, a disk, or a network. This module is the "someone
else's job" the engine docstring defers to: it snapshots each mutated
Participant/Topic to SQLite after every engine call, and can rebuild
an equivalent in-memory Emeetinghouse from those snapshots on startup.

Login credentials are a web-app concern, not a Meetinghouse domain
concept (the real Participation form arrives by mail, not a signup
form), so they live in their own table here rather than on the
Participant dataclass.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Optional

from emeetinghouse import (
    Dismissal,
    Emeetinghouse,
    Participant,
    ParticipationForm,
    PollResult,
    RuleConfig,
    Subsection,
    Topic,
    VoteChoice,
    VoteRecord,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    forms_json TEXT NOT NULL,
    dismissals_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    participant_id TEXT PRIMARY KEY REFERENCES participants(id),
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participant_profiles (
    participant_id TEXT PRIMARY KEY REFERENCES participants(id),
    address TEXT NOT NULL,
    mothers_maiden_name TEXT NOT NULL,
    age INTEGER NOT NULL,
    photo_path TEXT NOT NULL,
    thumbprint_path TEXT NOT NULL,
    charge_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    subsection TEXT PRIMARY KEY,
    quiet_time_seconds REAL NOT NULL,
    min_life_seconds REAL NOT NULL,
    voter_fraction REAL NOT NULL,
    approval_fraction REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    subsection TEXT NOT NULL,
    author_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    poll_question TEXT,
    created_at TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    target_subsection TEXT,
    target_participant_id TEXT,
    proposed_rule_config_json TEXT,
    readers_json TEXT NOT NULL,
    votes_json TEXT NOT NULL,
    closed INTEGER NOT NULL,
    closed_at TEXT,
    result TEXT,
    last_activity_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
"""


def Connect(path):
    """Open (and, if needed, initialize) the SQLite store at `path`."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _Iso(value):
    return None if value is None else value.isoformat()


def _ParseDateTime(value):
    return None if value is None else dt.datetime.fromisoformat(value)


def _ParseDate(value):
    return None if value is None else dt.date.fromisoformat(value)


def _RuleToDict(rule: RuleConfig):
    return {
        "quiet_time": rule.quiet_time.total_seconds(),
        "min_life": rule.min_life.total_seconds(),
        "voter_fraction": rule.voter_fraction,
        "approval_fraction": rule.approval_fraction,
    }


def _RuleFromDict(data):
    return RuleConfig(
        quiet_time=dt.timedelta(seconds=data["quiet_time"]),
        min_life=dt.timedelta(seconds=data["min_life"]),
        voter_fraction=data["voter_fraction"],
        approval_fraction=data["approval_fraction"],
    )


def _FormsToJson(forms):
    return json.dumps(
        [{"id": f.id, "signed_date": f.signed_date.isoformat(), "scan_ref": f.scan_ref} for f in forms]
    )


def _FormsFromJson(raw):
    return [
        ParticipationForm(signed_date=_ParseDate(f["signed_date"]), scan_ref=f["scan_ref"], id=f["id"])
        for f in json.loads(raw)
    ]


def _DismissalsToJson(dismissals):
    return json.dumps(
        [
            {"start": _Iso(d.start), "end": _Iso(d.end), "ordinal": d.ordinal, "topic_id": d.topic_id}
            for d in dismissals
        ]
    )


def _DismissalsFromJson(raw):
    return [
        Dismissal(start=_ParseDateTime(d["start"]), end=_ParseDateTime(d["end"]), ordinal=d["ordinal"], topic_id=d["topic_id"])
        for d in json.loads(raw)
    ]


def _ReadersToJson(readers):
    return json.dumps({pid: _Iso(when) for pid, when in readers.items()})


def _ReadersFromJson(raw):
    return {pid: _ParseDateTime(when) for pid, when in json.loads(raw).items()}


def _VotesToJson(votes):
    return json.dumps(
        {
            pid: [
                {
                    "participant_id": r.participant_id,
                    "choice": r.choice.value,
                    "comment": r.comment,
                    "timestamp": _Iso(r.timestamp),
                    "revoked": r.revoked,
                    "revoked_at": _Iso(r.revoked_at),
                }
                for r in records
            ]
            for pid, records in votes.items()
        }
    )


def _VotesFromJson(raw):
    votes = {}
    for pid, records in json.loads(raw).items():
        votes[pid] = [
            VoteRecord(
                participant_id=r["participant_id"],
                choice=VoteChoice(r["choice"]),
                comment=r["comment"],
                timestamp=_ParseDateTime(r["timestamp"]),
                revoked=r["revoked"],
                revoked_at=_ParseDateTime(r["revoked_at"]),
            )
            for r in records
        ]
    return votes


def SaveParticipant(conn, participant: Participant):
    """Persist the full current state of one Participant."""
    conn.execute(
        """
        INSERT INTO participants (id, name, forms_json, dismissals_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            forms_json = excluded.forms_json,
            dismissals_json = excluded.dismissals_json
        """,
        (participant.id, participant.name, _FormsToJson(participant.forms), _DismissalsToJson(participant.dismissals)),
    )
    conn.commit()


def SaveTopic(conn, topic: Topic):
    """Persist the full current state of one Topic (and its Poll)."""
    conn.execute(
        """
        INSERT INTO topics (
            id, subsection, author_id, title, description, poll_question, created_at,
            rules_json, target_subsection, target_participant_id, proposed_rule_config_json,
            readers_json, votes_json, closed, closed_at, result, last_activity_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            poll_question = excluded.poll_question,
            readers_json = excluded.readers_json,
            votes_json = excluded.votes_json,
            closed = excluded.closed,
            closed_at = excluded.closed_at,
            result = excluded.result,
            last_activity_at = excluded.last_activity_at
        """,
        (
            topic.id,
            topic.subsection.value,
            topic.author_id,
            topic.title,
            topic.description,
            topic.poll_question,
            _Iso(topic.created_at),
            json.dumps(_RuleToDict(topic.rules)),
            topic.target_subsection.value if topic.target_subsection else None,
            topic.target_participant_id,
            json.dumps(_RuleToDict(topic.proposed_rule_config)) if topic.proposed_rule_config else None,
            _ReadersToJson(topic.readers),
            _VotesToJson(topic.votes),
            int(topic.closed),
            _Iso(topic.closed_at),
            topic.result.value if topic.result else None,
            _Iso(topic.last_activity_at),
        ),
    )
    conn.commit()


def SaveRules(conn, subsection: Subsection, rule: RuleConfig):
    """Persist the current RuleConfig for one subsection."""
    d = _RuleToDict(rule)
    conn.execute(
        """
        INSERT INTO rules (subsection, quiet_time_seconds, min_life_seconds, voter_fraction, approval_fraction)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(subsection) DO UPDATE SET
            quiet_time_seconds = excluded.quiet_time_seconds,
            min_life_seconds = excluded.min_life_seconds,
            voter_fraction = excluded.voter_fraction,
            approval_fraction = excluded.approval_fraction
        """,
        (subsection.value, d["quiet_time"], d["min_life"], d["voter_fraction"], d["approval_fraction"]),
    )
    conn.commit()


def LogEvent(conn, entry: dict):
    """Append one Emeetinghouse event_log entry to the durable archive."""
    entry = dict(entry)
    kind = entry.pop("kind")
    at = entry.pop("at")
    conn.execute(
        "INSERT INTO event_log (kind, at, data_json) VALUES (?, ?, ?)",
        (kind, _Iso(at), json.dumps(entry, default=str)),
    )
    conn.commit()


def SetCredentials(conn, participant_id: str, username: str, password_hash: str):
    conn.execute(
        """
        INSERT INTO credentials (participant_id, username, password_hash)
        VALUES (?, ?, ?)
        ON CONFLICT(participant_id) DO UPDATE SET
            username = excluded.username,
            password_hash = excluded.password_hash
        """,
        (participant_id, username, password_hash),
    )
    conn.commit()


def GetCredentialsByUsername(conn, username: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM credentials WHERE username = ?", (username,)).fetchone()


def GetCredentialsForParticipant(conn, participant_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM credentials WHERE participant_id = ?", (participant_id,)).fetchone()


def SaveParticipantProfile(
    conn,
    participant_id: str,
    address: str,
    mothers_maiden_name: str,
    age: int,
    photo_path: str,
    thumbprint_path: str,
    created_at: dt.datetime,
):
    """Record the identity details a self-registering Participant supplied.

    These back up the paper form they print and mail in (see app.py's
    Register view) so the Meeting can manually cross-check a submission
    against what arrives by mail; they aren't used by the engine itself.
    """
    conn.execute(
        """
        INSERT INTO participant_profiles
            (participant_id, address, mothers_maiden_name, age, photo_path, thumbprint_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (participant_id, address, mothers_maiden_name, age, photo_path, thumbprint_path, _Iso(created_at)),
    )
    conn.commit()


def GetParticipantProfile(conn, participant_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM participant_profiles WHERE participant_id = ?", (participant_id,)
    ).fetchone()


def SetChargeStatus(conn, participant_id: str, status: str):
    conn.execute(
        "UPDATE participant_profiles SET charge_status = ? WHERE participant_id = ?",
        (status, participant_id),
    )
    conn.commit()


def LoadHouse(conn) -> Emeetinghouse:
    """Rebuild an Emeetinghouse instance from everything persisted so far."""
    rule_rows = conn.execute("SELECT * FROM rules").fetchall()
    rules = {
        Subsection(row["subsection"]): RuleConfig(
            quiet_time=dt.timedelta(seconds=row["quiet_time_seconds"]),
            min_life=dt.timedelta(seconds=row["min_life_seconds"]),
            voter_fraction=row["voter_fraction"],
            approval_fraction=row["approval_fraction"],
        )
        for row in rule_rows
    }
    house = Emeetinghouse(rules=rules or None)

    for row in conn.execute("SELECT * FROM participants"):
        participant = Participant(
            name=row["name"],
            id=row["id"],
            forms=_FormsFromJson(row["forms_json"]),
            dismissals=_DismissalsFromJson(row["dismissals_json"]),
        )
        house.participants[participant.id] = participant

    for row in conn.execute("SELECT * FROM topics"):
        topic = Topic(
            id=row["id"],
            subsection=Subsection(row["subsection"]),
            author_id=row["author_id"],
            title=row["title"],
            description=row["description"],
            poll_question=row["poll_question"],
            created_at=_ParseDateTime(row["created_at"]),
            rules=_RuleFromDict(json.loads(row["rules_json"])),
            target_subsection=Subsection(row["target_subsection"]) if row["target_subsection"] else None,
            target_participant_id=row["target_participant_id"],
            proposed_rule_config=_RuleFromDict(json.loads(row["proposed_rule_config_json"]))
            if row["proposed_rule_config_json"]
            else None,
            readers=_ReadersFromJson(row["readers_json"]),
            votes=_VotesFromJson(row["votes_json"]),
            closed=bool(row["closed"]),
            closed_at=_ParseDateTime(row["closed_at"]),
            result=PollResult(row["result"]) if row["result"] else None,
            last_activity_at=_ParseDateTime(row["last_activity_at"]),
        )
        house.topics[topic.id] = topic

    for subsection, rule in house.current_rules.items():
        if subsection not in rules:
            SaveRules(conn, subsection, rule)

    return house
