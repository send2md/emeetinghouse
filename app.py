"""Web front end for the Meetinghouse engine.

This module owns everything emeetinghouse.py deliberately does not:
wall-clock time, HTTP, sessions/login, and calling into persistence.py
after each mutation. The single in-process `house` is the same
Emeetinghouse object the engine's tests exercise; this file just feeds
it real time and real people, and keeps SQLite in sync.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import sqlite3
import threading

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import persistence
import tick_runner
from emeetinghouse import (
    EmeetinghouseError,
    Subsection,
    VoteChoice,
)

DB_PATH = os.environ.get("EMEETINGHOUSE_DB", os.path.join(os.path.dirname(__file__), "emeetinghouse.db"))
ADMIN_PASSWORD = os.environ.get("EMEETINGHOUSE_ADMIN_PASSWORD", "admin")
TICK_INTERVAL_SECONDS = int(os.environ.get("EMEETINGHOUSE_TICK_SECONDS", "300"))

# Self-registration uploads (photo + thumbprint) live outside ~/html, next to
# the SQLite file, so they're never web-accessible except through the
# admin-gated AdminParticipantImage route below.
UPLOAD_DIR = os.environ.get("EMEETINGHOUSE_UPLOADS", os.path.join(os.path.dirname(DB_PATH), "uploads"))
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg"}

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("EMEETINGHOUSE_SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # cap uploads (photo + thumbprint) at 8 MB total

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


def _AllowedImage(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def _ImageMimeType(filename):
    """The MIME subtype for a data: URI — "jpg" isn't a registered one, "jpeg" is."""
    ext = filename.rsplit(".", 1)[1].lower()
    return "jpeg" if ext == "jpg" else ext


def _SaveUpload(file_storage, participant_id, field_name):
    """Save an uploaded photo/thumbprint under UPLOAD_DIR and return (path, raw bytes).

    Returning the bytes too lets the caller embed the same image inline
    (as a data: URI) on the one-time printable confirmation page, without
    a second disk read.
    """
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    data = file_storage.read()
    participant_dir = os.path.join(UPLOAD_DIR, participant_id)
    os.makedirs(participant_dir, exist_ok=True)
    dest = os.path.join(participant_dir, f"{field_name}.{ext}")
    with open(dest, "wb") as f:
        f.write(data)
    return dest, data


def _SuggestUsername(base):
    """The next available `root2`, `root3`, ... login for a taken username.

    Strips any trailing digits from `base` first, so suggesting an
    alternative for an already-suffixed "janedoe2" offers "janedoe3"
    rather than piling on a second suffix ("janedoe22").
    """
    root = base.rstrip("0123456789") or base
    suffix = 2
    candidate = f"{root}{suffix}"
    while persistence.GetCredentialsByUsername(conn, candidate) is not None:
        suffix += 1
        candidate = f"{root}{suffix}"
    return candidate


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
    """Logged-out visitors see a plain login form here, nothing else.

    Unlike RequireLogin() (used by every other protected page), this
    doesn't redirect or flash "please log in" — "/" IS the login page
    for a logged-out visitor, not a bounce through one.
    """
    if g.participant is None:
        return render_template("login.html")
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
    """Log in, or fall through to self-registration for an unrecognized login.

    A username that matches nobody is treated as free to claim (straight
    to Register); a username that matches someone but the wrong password
    was given goes to LoginRetry, which asks whether that was a typo or
    a different person wanting the same login.
    """
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = persistence.GetCredentialsByUsername(conn, username)
        if row is not None and check_password_hash(row["password_hash"], password):
            participant = house.participants.get(row["participant_id"])
            if participant is None or not participant.IsParticipant(Now()):
                flash("Your participation form has lapsed. Please send a fresh one.")
                return render_template("login.html")
            session["participant_id"] = participant.id
            flash(f"Welcome, {participant.name}.")
            return redirect(request.args.get("next") or url_for("Dashboard"))
        if row is not None:
            return render_template("login_retry.html", username=username)
        session["pending_username"] = username
        return redirect(url_for("Register"))
    return render_template("login.html")


@app.route("/logout")
def Logout():
    session.pop("participant_id", None)
    return redirect(url_for("Login"))


@app.route("/login/claim", methods=["POST"])
def LoginClaim():
    """"This is a new login": `username` is taken, so offer an alternative."""
    username = request.form.get("username", "").strip()
    if not username:
        return redirect(url_for("Login"))
    return render_template("username_taken.html", taken_username=username, suggestion=_SuggestUsername(username))


@app.route("/register/start", methods=["POST"])
def RegisterStart():
    """Claim `username` for a new Participant, or bounce back if it's (now) taken."""
    username = request.form.get("username", "").strip()
    if not username:
        flash("Choose a login.")
        return redirect(url_for("Login"))
    if persistence.GetCredentialsByUsername(conn, username) is not None:
        return render_template("username_taken.html", taken_username=username, suggestion=_SuggestUsername(username))
    session["pending_username"] = username
    return redirect(url_for("Register"))


@app.route("/register", methods=["GET", "POST"])
def Register():
    """The self-service "you are a new Participant" form: no admin involved.

    Collects everything the printed, mailed-in form needs (see
    register_complete.html): a name, a password the Participant chooses,
    and the identity details a Quaker Meeting has always wanted on that
    paper form (address, mother's maiden name, age) plus a photo and a
    thumbprint, uploaded here and printed back out on the confirmation
    page so the same images go in the envelope.
    """
    username = session.get("pending_username")
    if not username:
        flash("Start from the login page to register.")
        return redirect(url_for("Login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        address = request.form.get("address", "").strip()
        mothers_maiden_name = request.form.get("mothers_maiden_name", "").strip()
        age_raw = request.form.get("age", "").strip()
        photo = request.files.get("photo")
        thumbprint = request.files.get("thumbprint")

        errors = []
        if not name:
            errors.append("Whole real name is required.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if not address:
            errors.append("Address is required.")
        if not mothers_maiden_name:
            errors.append("Mother's maiden name is required.")
        if not age_raw.isdigit():
            errors.append("Age must be a whole number.")
        if not photo or not photo.filename or not _AllowedImage(photo.filename):
            errors.append("A photo (jpg or png) is required.")
        if not thumbprint or not thumbprint.filename or not _AllowedImage(thumbprint.filename):
            errors.append("A thumbprint image (jpg or png) is required.")
        if errors:
            for error in errors:
                flash(error)
            return render_template("register.html", username=username)

        # The availability check and the insert both happen under LOCK, so a
        # second request racing on the same username can't slip past the
        # check between here and SetCredentials; the UNIQUE constraint is
        # still a backstop in case this ever runs with more than one worker.
        with LOCK:
            if persistence.GetCredentialsByUsername(conn, username) is not None:
                flash("That login was just taken. Please choose another.")
                return render_template(
                    "username_taken.html", taken_username=username, suggestion=_SuggestUsername(username)
                )
            participant = house.RegisterParticipant(name)
            participant.SignForm(signed_date=Now().date())
            persistence.SaveParticipant(conn, participant)
            try:
                persistence.SetCredentials(
                    conn, participant.id, username, generate_password_hash(password, method="pbkdf2:sha256")
                )
            except sqlite3.IntegrityError:
                flash("That login was just taken. Please choose another.")
                return render_template(
                    "username_taken.html", taken_username=username, suggestion=_SuggestUsername(username)
                )
            photo_path, photo_bytes = _SaveUpload(photo, participant.id, "photo")
            thumbprint_path, thumbprint_bytes = _SaveUpload(thumbprint, participant.id, "thumbprint")
            persistence.SaveParticipantProfile(
                conn, participant.id, address, mothers_maiden_name, int(age_raw), photo_path, thumbprint_path, Now()
            )

        session.pop("pending_username", None)
        session["participant_id"] = participant.id
        return render_template(
            "register_complete.html",
            participant=participant,
            username=username,
            address=address,
            mothers_maiden_name=mothers_maiden_name,
            age=age_raw,
            photo_data_uri=f"data:image/{_ImageMimeType(photo.filename)};base64,"
            + base64.b64encode(photo_bytes).decode("ascii"),
            thumbprint_data_uri=f"data:image/{_ImageMimeType(thumbprint.filename)};base64,"
            + base64.b64encode(thumbprint_bytes).decode("ascii"),
        )

    return render_template("register.html", username=username)


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
        except EmeetinghouseError as exc:
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
            except EmeetinghouseError:
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
    except EmeetinghouseError as exc:
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
    except EmeetinghouseError as exc:
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


@app.route("/admin/participant/<participant_id>")
def AdminParticipantProfile(participant_id):
    """The self-registration details for one Participant, for manual cross-checking.

    Compare these against whatever arrives in the mail (see Register in
    this module and register_complete.html) — the admin never had to
    type in a password to get here, and still can't see one.
    """
    guard = RequireAdmin()
    if guard:
        return guard
    participant = house.participants.get(participant_id)
    if participant is None:
        abort(404)
    profile = persistence.GetParticipantProfile(conn, participant_id)
    return render_template("admin_participant.html", participant=participant, profile=profile)


@app.route("/admin/participant/<participant_id>/<field>")
def AdminParticipantImage(participant_id, field):
    guard = RequireAdmin()
    if guard:
        return guard
    if field not in ("photo", "thumbprint"):
        abort(404)
    profile = persistence.GetParticipantProfile(conn, participant_id)
    if profile is None:
        abort(404)
    path = profile["photo_path"] if field == "photo" else profile["thumbprint_path"]
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path)


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
            "EMEETINGHOUSE_ADMIN_PASSWORD not set; using the insecure default 'admin'. "
            "Set it before exposing this app beyond localhost."
        )
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" or not app.debug:
        StartScheduler()
    app.run(debug=os.environ.get("EMEETINGHOUSE_DEBUG") == "1", host=os.environ.get("EMEETINGHOUSE_HOST", "127.0.0.1"), port=int(os.environ.get("EMEETINGHOUSE_PORT", "5000")))
