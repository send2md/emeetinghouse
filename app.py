"""Web front end for the Meetinghouse engine.

This module owns everything emeetinghouse.py deliberately does not:
wall-clock time, HTTP, sessions/login, and calling into persistence.py
after each mutation. The single in-process `house` is the same
Meetinghouse object the engine's tests exercise; this file just feeds
it real time and real people, and keeps SQLite in sync.
"""

from __future__ import annotations

import datetime as dt
import os
import threading

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import persistence
import tick_runner
from emeetinghouse import (
    MeetinghouseError,
    Subsection,
    VoteChoice,
)

DB_PATH = os.environ.get("MEETINGHOUSE_DB", os.path.join(os.path.dirname(__file__), "emeetinghouse.db"))
ADMIN_PASSWORD = os.environ.get("MEETINGHOUSE_ADMIN_PASSWORD", "admin")
TICK_INTERVAL_SECONDS = int(os.environ.get("MEETINGHOUSE_TICK_SECONDS", "300"))

app = Flask(__name__)
app.secret_key = os.environ.get("MEETINGHOUSE_SECRET_KEY", "dev-secret-change-me")

LOCK = threading.Lock()
conn = persistence.Connect(DB_PATH)
house = persistence.LoadHouse(conn)


def Now():
    """The one place this app reads the wall clock, so it stays swappable."""
    return dt.datetime.utcnow()


def FlushNewEvents(prev_len):
    """Persist any event_log entries the engine appended since `prev_len`."""
    for entry in house.event_log[prev_len:]:
        persistence.LogEvent(conn, entry)


def RunTick():
    """Close every eligible Topic/Poll and persist the resulting changes."""
    with LOCK:
        return tick_runner.RunTick(house, conn, Now())


def SchedulerLoop():
    while True:
        threading.Event().wait(TICK_INTERVAL_SECONDS)
        try:
            RunTick()
        except Exception as exc:  # pragma: no cover - background safety net
            app.logger.exception("Scheduled Tick() failed: %s", exc)


def StartScheduler():
    thread = threading.Thread(target=SchedulerLoop, daemon=True)
    thread.start()


@app.template_filter("humanage")
def HumanAge(delta: dt.timedelta):
    days = delta.days
    if days >= 7:
        return f"{days // 7}w {days % 7}d"
    hours = delta.seconds // 3600
    return f"{days}d {hours}h"


@app.template_filter("fmt")
def FormatDateTime(value):
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M UTC")


@app.context_processor
def InjectHelpers():
    def participant_name(participant_id):
        participant = house.participants.get(participant_id)
        return participant.name if participant else "(unknown)"

    return {
        "participant_name": participant_name,
        "current_participant": g.get("participant", None),
        "Subsection": Subsection,
        "VoteChoice": VoteChoice,
    }


@app.before_request
def LoadCurrentParticipant():
    g.participant = None
    participant_id = session.get("participant_id")
    if participant_id and participant_id in house.participants:
        g.participant = house.participants[participant_id]


def RequireLogin():
    if g.participant is None:
        flash("Please log in first.")
        return redirect(url_for("Login", next=request.path))
    return None


def RequireAdmin():
    if not session.get("is_admin"):
        flash("Admin login required.")
        return redirect(url_for("AdminLogin"))
    return None


@app.route("/")
def Dashboard():
    guard = RequireLogin()
    if guard:
        return guard
    now = Now()
    open_by_subsection = {s: [] for s in Subsection}
    for topic in house.topics.values():
        if topic.closed:
            continue
        counts = house.EffectiveVoteCounts(topic, now)
        open_by_subsection[topic.subsection].append(
            {"topic": topic, "counts": counts, "age": now - topic.created_at, "readers": len(house.EffectiveReaders(topic, now))}
        )
    for items in open_by_subsection.values():
        items.sort(key=lambda item: item["topic"].created_at)
    return render_template("dashboard.html", open_by_subsection=open_by_subsection, now=now)


@app.route("/login", methods=["GET", "POST"])
def Login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = persistence.GetCredentialsByUsername(conn, username)
        if row is None or not check_password_hash(row["password_hash"], password):
            flash("Incorrect username or password.")
            return render_template("login.html")
        participant = house.participants.get(row["participant_id"])
        if participant is None or not participant.IsParticipant(Now()):
            flash("Your participation form has lapsed. Please send a fresh one.")
            return render_template("login.html")
        session["participant_id"] = participant.id
        flash(f"Welcome, {participant.name}.")
        return redirect(request.args.get("next") or url_for("Dashboard"))
    return render_template("login.html")


@app.route("/logout")
def Logout():
    session.pop("participant_id", None)
    return redirect(url_for("Login"))


@app.route("/subsection/<name>")
def SubsectionView(name):
    guard = RequireLogin()
    if guard:
        return guard
    try:
        subsection = Subsection(name.capitalize())
    except ValueError:
        abort(404)
    now = Now()
    topics = [t for t in house.topics.values() if t.subsection is subsection]
    topics.sort(key=lambda t: t.created_at, reverse=True)
    rows = []
    for topic in topics:
        counts = house.EffectiveVoteCounts(topic, now) if topic.HasPoll() else None
        rows.append({"topic": topic, "counts": counts})
    return render_template(
        "subsection.html", subsection=subsection, rows=rows, rule=house.current_rules[subsection]
    )


@app.route("/topic/new/<name>", methods=["GET", "POST"])
def NewTopic(name):
    guard = RequireLogin()
    if guard:
        return guard
    try:
        subsection = Subsection(name.capitalize())
    except ValueError:
        abort(404)

    other_participants = sorted(house.participants.values(), key=lambda p: p.name)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip() or None
        poll_question = request.form.get("poll_question", "").strip() or None
        target_subsection = None
        target_participant_id = None
        proposed_rule_config = None

        if subsection is Subsection.RULES:
            try:
                target_subsection = Subsection(request.form.get("target_subsection", ""))
            except ValueError:
                flash("Choose which subsection this Rule addresses.")
                return render_template("new_topic.html", subsection=subsection, participants=other_participants)
            current = house.current_rules[target_subsection]
            proposed_rule_config = _BuildProposedRule(current, request.form)
        elif subsection is Subsection.JURY:
            target_participant_id = request.form.get("target_participant_id") or None

        try:
            with LOCK:
                prev_len = len(house.event_log)
                topic = house.CreateTopic(
                    g.participant.id,
                    subsection,
                    title,
                    Now(),
                    description=description,
                    poll_question=poll_question,
                    target_subsection=target_subsection,
                    target_participant_id=target_participant_id,
                    proposed_rule_config=proposed_rule_config,
                )
                persistence.SaveTopic(conn, topic)
                FlushNewEvents(prev_len)
        except MeetinghouseError as exc:
            flash(str(exc))
            return render_template("new_topic.html", subsection=subsection, participants=other_participants)

        return redirect(url_for("TopicDetail", topic_id=topic.id))

    return render_template("new_topic.html", subsection=subsection, participants=other_participants)


def _BuildProposedRule(current, form):
    def _weeks(field, fallback_timedelta):
        raw = form.get(field, "").strip()
        if not raw:
            return fallback_timedelta
        return dt.timedelta(weeks=float(raw))

    def _fraction(field, fallback):
        raw = form.get(field, "").strip()
        if not raw:
            return fallback
        return float(raw)

    from emeetinghouse import RuleConfig

    quiet_time = _weeks("quiet_time_weeks", current.quiet_time)
    min_life = _weeks("min_life_weeks", current.min_life)
    voter_fraction = _fraction("voter_fraction", current.voter_fraction)
    approval_fraction = _fraction("approval_fraction", current.approval_fraction)

    if (quiet_time, min_life, voter_fraction, approval_fraction) == (
        current.quiet_time,
        current.min_life,
        current.voter_fraction,
        current.approval_fraction,
    ):
        return None
    return RuleConfig(
        quiet_time=quiet_time, min_life=min_life, voter_fraction=voter_fraction, approval_fraction=approval_fraction
    )


@app.route("/topic/<topic_id>")
def TopicDetail(topic_id):
    guard = RequireLogin()
    if guard:
        return guard
    topic = house.topics.get(topic_id)
    if topic is None:
        abort(404)
    now = Now()

    if g.participant.IsActive(now) and topic_id in house.topics and not topic.closed:
        with LOCK:
            try:
                house.MarkRead(topic_id, g.participant.id, now)
                persistence.SaveTopic(conn, topic)
            except MeetinghouseError:
                pass

    comments = []
    for participant_id, history in topic.votes.items():
        for record in history:
            comments.append(record)
    comments.sort(key=lambda r: r.timestamp)

    my_vote = topic.CurrentVote(g.participant.id) if topic.HasPoll() else None
    counts = house.EffectiveVoteCounts(topic, now) if topic.HasPoll() else None
    readers = house.EffectiveReaders(topic, now)

    return render_template(
        "topic.html",
        topic=topic,
        comments=comments,
        my_vote=my_vote,
        counts=counts,
        readers=readers,
        now=now,
        dismissed_ids={pid for pid in topic.votes if house.participants.get(pid) and house.participants[pid].IsDismissed(now)},
    )


@app.route("/topic/<topic_id>/vote", methods=["POST"])
def CastVote(topic_id):
    guard = RequireLogin()
    if guard:
        return guard
    choice_raw = request.form.get("choice", "")
    comment = request.form.get("comment", "")
    try:
        choice = VoteChoice(choice_raw)
    except ValueError:
        flash("Choose Yes, Abstain, or No.")
        return redirect(url_for("TopicDetail", topic_id=topic_id))

    try:
        with LOCK:
            prev_len = len(house.event_log)
            topic = house.topics[topic_id]
            house.CastVote(topic_id, g.participant.id, choice, comment, Now())
            persistence.SaveTopic(conn, topic)
            FlushNewEvents(prev_len)
    except MeetinghouseError as exc:
        flash(str(exc))
    return redirect(url_for("TopicDetail", topic_id=topic_id))


@app.route("/topic/<topic_id>/revoke", methods=["POST"])
def Revoke(topic_id):
    guard = RequireLogin()
    if guard:
        return guard
    try:
        with LOCK:
            prev_len = len(house.event_log)
            topic = house.topics[topic_id]
            house.RevokeVote(topic_id, g.participant.id, Now())
            persistence.SaveTopic(conn, topic)
            FlushNewEvents(prev_len)
    except MeetinghouseError as exc:
        flash(str(exc))
    return redirect(url_for("TopicDetail", topic_id=topic_id))


@app.route("/archive")
def Archive():
    guard = RequireLogin()
    if guard:
        return guard
    query = request.args.get("q", "").strip()
    results = house.SearchArchive(query) if query else []
    return render_template("archive.html", query=query, results=results)


@app.route("/summary")
def Summary():
    guard = RequireLogin()
    if guard:
        return guard
    summary = house.DailySummary(g.participant.id, Now())
    return render_template("summary.html", summary=summary)


@app.route("/admin/login", methods=["GET", "POST"])
def AdminLogin():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("Admin"))
        flash("Incorrect admin password.")
    return render_template("admin_login.html")


@app.route("/admin", methods=["GET"])
def Admin():
    guard = RequireAdmin()
    if guard:
        return guard
    now = Now()
    participants = sorted(house.participants.values(), key=lambda p: p.name)
    return render_template("admin.html", participants=participants, now=now, rules=house.current_rules)


@app.route("/admin/register", methods=["POST"])
def AdminRegister():
    guard = RequireAdmin()
    if guard:
        return guard
    name = request.form.get("name", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    scan_ref = request.form.get("scan_ref", "").strip()
    if not (name and username and password):
        flash("Name, username, and password are all required.")
        return redirect(url_for("Admin"))

    with LOCK:
        participant = house.RegisterParticipant(name)
        participant.SignForm(signed_date=Now().date(), scan_ref=scan_ref)
        persistence.SaveParticipant(conn, participant)
        persistence.SetCredentials(
            conn, participant.id, username, generate_password_hash(password, method="pbkdf2:sha256")
        )
    flash(f"Registered {name} with a signed form dated today.")
    return redirect(url_for("Admin"))


@app.route("/admin/sign_form", methods=["POST"])
def AdminSignForm():
    guard = RequireAdmin()
    if guard:
        return guard
    participant_id = request.form.get("participant_id")
    scan_ref = request.form.get("scan_ref", "").strip()
    participant = house.participants.get(participant_id)
    if participant is None:
        flash("No such Participant.")
        return redirect(url_for("Admin"))
    with LOCK:
        participant.SignForm(signed_date=Now().date(), scan_ref=scan_ref)
        persistence.SaveParticipant(conn, participant)
    flash(f"Recorded a fresh signed form for {participant.name}.")
    return redirect(url_for("Admin"))


@app.route("/admin/tick", methods=["POST"])
def AdminTick():
    guard = RequireAdmin()
    if guard:
        return guard
    closed = RunTick()
    flash(f"Ran Tick(): closed {len(closed)} Topic(s).")
    return redirect(url_for("Admin"))


@app.route("/admin/logout")
def AdminLogout():
    session.pop("is_admin", None)
    return redirect(url_for("AdminLogin"))


if __name__ == "__main__":
    if ADMIN_PASSWORD == "admin":
        app.logger.warning(
            "MEETINGHOUSE_ADMIN_PASSWORD not set; using the insecure default 'admin'. "
            "Set it before exposing this app beyond localhost."
        )
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" or not app.debug:
        StartScheduler()
    app.run(debug=os.environ.get("MEETINGHOUSE_DEBUG") == "1", host=os.environ.get("MEETINGHOUSE_HOST", "127.0.0.1"), port=int(os.environ.get("MEETINGHOUSE_PORT", "5000")))
