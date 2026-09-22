from flask import render_template

from CTFd.admin import admin
from CTFd.models import Challenges, Fails, Solves, Teams, Tracking, Users, db
from CTFd.utils.decorators import admins_only
from CTFd.utils.logging import timed_block
from CTFd.utils.modes import get_model
from CTFd.utils.updates import update_check


@admin.route("/admin/statistics", methods=["GET"])
@admins_only
def statistics():
    update_check()

    Model = get_model()

    with timed_block("statistics.teams_registered"):
        teams_registered = Teams.query.count()

    with timed_block("statistics.users_registered"):
        users_registered = Users.query.count()

    with timed_block("statistics.wrong_count"):
        wrong_count = (
            Fails.query.join(Model, Fails.account_id == Model.id)
            .filter(Model.banned == False, Model.hidden == False)
            .count()
        )

    with timed_block("statistics.solve_count"):
        solve_count = (
            Solves.query.join(Model, Solves.account_id == Model.id)
            .filter(Model.banned == False, Model.hidden == False)
            .count()
        )

    with timed_block("statistics.challenge_count"):
        challenge_count = Challenges.query.count()

    with timed_block("statistics.total_points"):
        total_points = (
            Challenges.query.with_entities(db.func.sum(Challenges.value).label("sum"))
            .filter_by(state="visible")
            .first()
            .sum
        ) or 0

    with timed_block("statistics.ip_count"):
        ip_count = Tracking.query.with_entities(Tracking.ip).distinct().count()

    with timed_block("statistics.solves_per_challenge"):
        solves_sub = (
            db.session.query(
                Solves.challenge_id,
                db.func.count(Solves.challenge_id).label("solves_cnt"),
            )
            .join(Model, Solves.account_id == Model.id)
            .filter(Model.banned == False, Model.hidden == False)
            .group_by(Solves.challenge_id)
            .subquery()
        )

        solves = (
            db.session.query(
                solves_sub.columns.challenge_id,
                solves_sub.columns.solves_cnt,
                Challenges.name,
            )
            .join(Challenges, solves_sub.columns.challenge_id == Challenges.id)
            .all()
        )

    solve_data = {}
    for _chal, count, name in solves:
        solve_data[name] = count

    most_solved = None
    least_solved = None
    if len(solve_data):
        most_solved = max(solve_data, key=solve_data.get)
        least_solved = min(solve_data, key=solve_data.get)

    db.session.close()

    return render_template(
        "admin/statistics.html",
        user_count=users_registered,
        team_count=teams_registered,
        ip_count=ip_count,
        wrong_count=wrong_count,
        solve_count=solve_count,
        challenge_count=challenge_count,
        total_points=total_points,
        solve_data=solve_data,
        most_solved=most_solved,
        least_solved=least_solved,
    )
