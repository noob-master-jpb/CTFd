from typing import List  # noqa: I001

from flask import abort, render_template, request, url_for
from flask_restx import Namespace, Resource
from sqlalchemy.sql import and_

from CTFd.api.v1.helpers.request import validate_args
from CTFd.api.v1.helpers.schemas import sqlalchemy_to_pydantic
from CTFd.api.v1.schemas import APIDetailedSuccessResponse, APIListSuccessResponse
from CTFd.cache import clear_challenges, clear_standings
from CTFd.constants import RawEnum
from CTFd.models import ChallengeFiles as ChallengeFilesModel
from CTFd.models import Challenges
from CTFd.models import ChallengeTopics as ChallengeTopicsModel
from CTFd.models import Fails, Flags, Hints, HintUnlocks, Solves, Submissions, Tags, db

#adding containers, users and ports table to the file
from CTFd.models import Users, Containers, Ports

#importing a function from key_reader for api key
#make sure to make changes as per requirements to the function named api_key()
import CTFd.portainer as portainer 
from random import randint


from CTFd.plugins.challenges import CHALLENGE_CLASSES, get_chal_class
from CTFd.schemas.challenges import ChallengeSchema
from CTFd.schemas.flags import FlagSchema
from CTFd.schemas.hints import HintSchema
from CTFd.schemas.tags import TagSchema
from CTFd.utils import config, get_config
from CTFd.utils import user as current_user
from CTFd.utils.challenges import (
    get_all_challenges,
    get_solve_counts_for_challenges,
    get_solve_ids_for_user_id,
    get_solves_for_challenge_id,
)
from CTFd.utils.config.visibility import (
    accounts_visible,
    challenges_visible,
    scores_visible,
)
from CTFd.utils.dates import ctf_ended, ctf_paused, ctftime
from CTFd.utils.decorators import (
    admins_only,
    during_ctf_time_only,
    require_verified_emails,
)
from CTFd.utils.decorators.visibility import (
    check_account_visibility,
    check_challenge_visibility,
    check_score_visibility,
)
from CTFd.utils.humanize.words import pluralize
from CTFd.utils.logging import log
from CTFd.utils.security.signing import serialize
from CTFd.utils.user import (
    authed,
    get_current_team,
    get_current_team_attrs,
    get_current_user,
    get_current_user_attrs,
    is_admin,
)

challenges_namespace = Namespace(
    "challenges", description="Endpoint to retrieve Challenges"
)

ChallengeModel = sqlalchemy_to_pydantic(
    Challenges, include={"solves": int, "solved_by_me": bool}
)
TransientChallengeModel = sqlalchemy_to_pydantic(Challenges, exclude=["id"])


class ChallengeDetailedSuccessResponse(APIDetailedSuccessResponse):
    data: ChallengeModel


class ChallengeListSuccessResponse(APIListSuccessResponse):
    data: List[ChallengeModel]


challenges_namespace.schema_model(
    "ChallengeDetailedSuccessResponse", ChallengeDetailedSuccessResponse.apidoc()
)

challenges_namespace.schema_model(
    "ChallengeListSuccessResponse", ChallengeListSuccessResponse.apidoc()
)


@challenges_namespace.route("")
class ChallengeList(Resource):
    @check_challenge_visibility
    @during_ctf_time_only
    @require_verified_emails
    @challenges_namespace.doc(
        description="Endpoint to get Challenge objects in bulk",
        responses={
            200: ("Success", "ChallengeListSuccessResponse"),
            400: (
                "An error occured processing the provided or stored data",
                "APISimpleErrorResponse",
            ),
        },
    )
    @validate_args(
        {
            "name": (str, None),
            "max_attempts": (int, None),
            "value": (int, None),
            "category": (str, None),
            "type": (str, None),
            "state": (str, None),
            "q": (str, None),
            "field": (
                RawEnum(
                    "ChallengeFields",
                    {
                        "name": "name",
                        "description": "description",
                        "category": "category",
                        "type": "type",
                        "state": "state",
                    },
                ),
                None,
            ),
        },
        location="query",
    )
    def get(self, query_args):
        # Require a team if in teams mode
        # TODO: Convert this into a re-useable decorator
        # TODO: The require_team decorator doesnt work because of no admin passthru
        if get_current_user_attrs():
            if is_admin():
                pass
            else:
                if config.is_teams_mode() and get_current_team_attrs() is None:
                    abort(403)

        # Build filtering queries
        q = query_args.pop("q", None)
        field = str(query_args.pop("field", None))

        # Admins get a shortcut to see all challenges despite pre-requisites
        admin_view = is_admin() and request.args.get("view") == "admin"

        # Get a cached mapping of challenge_id to solve_count
        solve_counts = get_solve_counts_for_challenges(admin=admin_view)

        # Get list of solve_ids for current user
        if authed():
            user = get_current_user()
            user_solves = get_solve_ids_for_user_id(user_id=user.id)
        else:
            user_solves = set()

        # Aggregate the query results into the hashes defined at the top of
        # this block for later use
        if scores_visible() and accounts_visible():
            solve_count_dfl = 0
        else:
            # Empty out the solves_count if we're hiding scores/accounts
            solve_counts = {}
            # This is necessary to match the challenge detail API which returns
            # `None` for the solve count if visiblity checks fail
            solve_count_dfl = None

        chal_q = get_all_challenges(admin=admin_view, field=field, q=q, **query_args)

        # Iterate through the list of challenges, adding to the object which
        # will be JSONified back to the client
        response = []
        tag_schema = TagSchema(view="user", many=True)

        # Gather all challenge IDs so that we can determine invalid challenge prereqs
        all_challenge_ids = {
            c.id for c in Challenges.query.with_entities(Challenges.id).all()
        }
        for challenge in chal_q:
            if challenge.requirements:
                requirements = challenge.requirements.get("prerequisites", [])
                anonymize = challenge.requirements.get("anonymize")
                prereqs = set(requirements).intersection(all_challenge_ids)
                if user_solves >= prereqs or admin_view:
                    pass
                else:
                    if anonymize:
                        response.append(
                            {
                                "id": challenge.id,
                                "type": "hidden",
                                "name": "???",
                                "value": 0,
                                "solves": None,
                                "solved_by_me": False,
                                "category": "???",
                                "tags": [],
                                "template": "",
                                "script": "",
                            }
                        )
                    # Fallthrough to continue
                    continue

            try:
                challenge_type = get_chal_class(challenge.type)
            except KeyError:
                # Challenge type does not exist. Fall through to next challenge.
                continue

            # Challenge passes all checks, add it to response
            response.append(
                {
                    "id": challenge.id,
                    "type": challenge_type.name,
                    "name": challenge.name,
                    "value": challenge.value,
                    "solves": solve_counts.get(challenge.id, solve_count_dfl),
                    "solved_by_me": challenge.id in user_solves,
                    "category": challenge.category,
                    "tags": tag_schema.dump(challenge.tags).data,
                    "template": challenge_type.templates["view"],
                    "script": challenge_type.scripts["view"],
                }
            )

        db.session.close()
        return {"success": True, "data": response}

    @admins_only
    @challenges_namespace.doc(
        description="Endpoint to create a Challenge object",
        responses={
            200: ("Success", "ChallengeDetailedSuccessResponse"),
            400: (
                "An error occured processing the provided or stored data",
                "APISimpleErrorResponse",
            ),
        },
    )
    def post(self):
        data = request.form or request.get_json()

        # Load data through schema for validation but not for insertion
        schema = ChallengeSchema()
        response = schema.load(data)
        if response.errors:
            return {"success": False, "errors": response.errors}, 400

        challenge_type = data["type"]
        challenge_class = get_chal_class(challenge_type)
        challenge = challenge_class.create(request)
        response = challenge_class.read(challenge)

        clear_challenges()

        return {"success": True, "data": response}


@challenges_namespace.route("/types")
class ChallengeTypes(Resource):
    @admins_only
    def get(self):
        response = {}

        for class_id in CHALLENGE_CLASSES:
            challenge_class = CHALLENGE_CLASSES.get(class_id)
            response[challenge_class.id] = {
                "id": challenge_class.id,
                "name": challenge_class.name,
                "templates": challenge_class.templates,
                "scripts": challenge_class.scripts,
                "create": render_template(
                    challenge_class.templates["create"].lstrip("/")
                ),
            }
        return {"success": True, "data": response}


@challenges_namespace.route("/<challenge_id>")
class Challenge(Resource):
    @check_challenge_visibility
    @during_ctf_time_only
    @require_verified_emails
    @challenges_namespace.doc(
        description="Endpoint to get a specific Challenge object",
        responses={
            200: ("Success", "ChallengeDetailedSuccessResponse"),
            400: (
                "An error occured processing the provided or stored data",
                "APISimpleErrorResponse",
            ),
        },
    )
    def get(self, challenge_id):
        if is_admin():
            chal = Challenges.query.filter(Challenges.id == challenge_id).first_or_404()
        else:
            chal = Challenges.query.filter(
                Challenges.id == challenge_id,
                and_(Challenges.state != "hidden", Challenges.state != "locked"),
            ).first_or_404()

        try:
            chal_class = get_chal_class(chal.type)
        except KeyError:
            abort(
                500,
                f"The underlying challenge type ({chal.type}) is not installed. This challenge can not be loaded.",
            )

        if chal.requirements:
            requirements = chal.requirements.get("prerequisites", [])
            anonymize = chal.requirements.get("anonymize")
            # Gather all challenge IDs so that we can determine invalid challenge prereqs
            all_challenge_ids = {
                c.id for c in Challenges.query.with_entities(Challenges.id).all()
            }
            if challenges_visible():
                user = get_current_user()
                if user:
                    solve_ids = (
                        Solves.query.with_entities(Solves.challenge_id)
                        .filter_by(account_id=user.account_id)
                        .order_by(Solves.challenge_id.asc())
                        .all()
                    )
                else:
                    # We need to handle the case where a user is viewing challenges anonymously
                    solve_ids = []
                solve_ids = {value for value, in solve_ids}
                prereqs = set(requirements).intersection(all_challenge_ids)
                if solve_ids >= prereqs or is_admin():
                    pass
                else:
                    if anonymize:
                        return {
                            "success": True,
                            "data": {
                                "id": chal.id,
                                "type": "hidden",
                                "name": "???",
                                "value": 0,
                                "solves": None,
                                "solved_by_me": False,
                                "category": "???",
                                "tags": [],
                                "template": "",
                                "script": "",
                            },
                        }
                    abort(403)
            else:
                abort(403)

        tags = [
            tag["value"] for tag in TagSchema("user", many=True).dump(chal.tags).data
        ]

        unlocked_hints = set()
        hints = []
        if authed():
            user = get_current_user()
            team = get_current_team()

            # TODO: Convert this into a re-useable decorator
            if is_admin():
                pass
            else:
                if config.is_teams_mode() and team is None:
                    abort(403)

            unlocked_hints = {
                u.target
                for u in HintUnlocks.query.filter_by(
                    type="hints", account_id=user.account_id
                )
            }
            files = []
            for f in chal.files:
                token = {
                    "user_id": user.id,
                    "team_id": team.id if team else None,
                    "file_id": f.id,
                }
                files.append(
                    url_for("views.files", path=f.location, token=serialize(token))
                )
        else:
            files = [url_for("views.files", path=f.location) for f in chal.files]

        for hint in Hints.query.filter_by(challenge_id=chal.id).all():
            if hint.id in unlocked_hints or ctf_ended():
                hints.append(
                    {"id": hint.id, "cost": hint.cost, "content": hint.content}
                )
            else:
                hints.append({"id": hint.id, "cost": hint.cost})

        response = chal_class.read(challenge=chal)

        # Get list of solve_ids for current user
        if authed():
            user = get_current_user()
            user_solves = get_solve_ids_for_user_id(user_id=user.id)
        else:
            user_solves = []

        solves_count = get_solve_counts_for_challenges(challenge_id=chal.id)
        if solves_count:
            challenge_id = chal.id
            solve_count = solves_count.get(chal.id)
            solved_by_user = challenge_id in user_solves
        else:
            solve_count, solved_by_user = 0, False

        # Hide solve counts if we are hiding solves/accounts
        if scores_visible() is False or accounts_visible() is False:
            solve_count = None

        if authed():
            # Get current attempts for the user
            attempts = Submissions.query.filter_by(
                account_id=user.account_id, challenge_id=challenge_id
            ).count()
        else:
            attempts = 0

        response["solves"] = solve_count
        response["solved_by_me"] = solved_by_user
        response["attempts"] = attempts
        response["files"] = files
        response["tags"] = tags
        response["hints"] = hints

        response["view"] = render_template(
            chal_class.templates["view"].lstrip("/"),
            solves=solve_count,
            solved_by_me=solved_by_user,
            files=files,
            tags=tags,
            hints=[Hints(**h) for h in hints],
            max_attempts=chal.max_attempts,
            attempts=attempts,
            challenge=chal,
        )

        db.session.close()
        return {"success": True, "data": response}

    @admins_only
    @challenges_namespace.doc(
        description="Endpoint to edit a specific Challenge object",
        responses={
            200: ("Success", "ChallengeDetailedSuccessResponse"),
            400: (
                "An error occured processing the provided or stored data",
                "APISimpleErrorResponse",
            ),
        },
    )
    def patch(self, challenge_id):
        data = request.get_json()

        # Load data through schema for validation but not for insertion
        schema = ChallengeSchema()
        response = schema.load(data)
        if response.errors:
            return {"success": False, "errors": response.errors}, 400

        challenge = Challenges.query.filter_by(id=challenge_id).first_or_404()
        challenge_class = get_chal_class(challenge.type)
        challenge = challenge_class.update(challenge, request)
        response = challenge_class.read(challenge)

        clear_standings()
        clear_challenges()

        return {"success": True, "data": response}

    @admins_only
    @challenges_namespace.doc(
        description="Endpoint to delete a specific Challenge object",
        responses={200: ("Success", "APISimpleSuccessResponse")},
    )
    def delete(self, challenge_id):
        challenge = Challenges.query.filter_by(id=challenge_id).first_or_404()
        chal_class = get_chal_class(challenge.type)
        chal_class.delete(challenge)

        clear_standings()
        clear_challenges()

        return {"success": True}


@challenges_namespace.route("/attempt")
class ChallengeAttempt(Resource):
    @check_challenge_visibility
    @during_ctf_time_only
    @require_verified_emails
    def post(self):
        if authed() is False:
            return {"success": True, "data": {"status": "authentication_required"}}, 403

        if not request.is_json:
            request_data = request.form
        else:
            request_data = request.get_json()

        challenge_id = request_data.get("challenge_id")

        if current_user.is_admin():
            preview = request.args.get("preview", False)
            if preview:
                challenge = Challenges.query.filter_by(id=challenge_id).first_or_404()
                chal_class = get_chal_class(challenge.type)
                status, message = chal_class.attempt(challenge, request)

                return {
                    "success": True,
                    "data": {
                        "status": "correct" if status else "incorrect",
                        "message": message,
                    },
                }

        if ctf_paused():
            return (
                {
                    "success": True,
                    "data": {
                        "status": "paused",
                        "message": "{} is paused".format(config.ctf_name()),
                    },
                },
                403,
            )

        user = get_current_user()
        team = get_current_team()

        # TODO: Convert this into a re-useable decorator
        if config.is_teams_mode() and team is None:
            abort(403)

        fails = Fails.query.filter_by(
            account_id=user.account_id, challenge_id=challenge_id
        ).count()

        challenge = Challenges.query.filter_by(id=challenge_id).first_or_404()

        if challenge.state == "hidden":
            abort(404)

        if challenge.state == "locked":
            abort(403)

        if challenge.requirements:
            requirements = challenge.requirements.get("prerequisites", [])
            solve_ids = (
                Solves.query.with_entities(Solves.challenge_id)
                .filter_by(account_id=user.account_id)
                .order_by(Solves.challenge_id.asc())
                .all()
            )
            solve_ids = {solve_id for solve_id, in solve_ids}
            # Gather all challenge IDs so that we can determine invalid challenge prereqs
            all_challenge_ids = {
                c.id for c in Challenges.query.with_entities(Challenges.id).all()
            }
            prereqs = set(requirements).intersection(all_challenge_ids)
            if solve_ids >= prereqs:
                pass
            else:
                abort(403)

        chal_class = get_chal_class(challenge.type)

        # Anti-bruteforce / submitting Flags too quickly
        kpm = current_user.get_wrong_submissions_per_minute(user.account_id)
        kpm_limit = int(get_config("incorrect_submissions_per_min", default=10))
        if kpm > kpm_limit:
            if ctftime():
                chal_class.fail(
                    user=user, team=team, challenge=challenge, request=request
                )
            log(
                "submissions",
                "[{date}] {name} submitted {submission} on {challenge_id} with kpm {kpm} [TOO FAST]",
                name=user.name,
                submission=request_data.get("submission", "").encode("utf-8"),
                challenge_id=challenge_id,
                kpm=kpm,
            )
            # Submitting too fast
            return (
                {
                    "success": True,
                    "data": {
                        "status": "ratelimited",
                        "message": "You're submitting flags too fast. Slow down.",
                    },
                },
                429,
            )

        solves = Solves.query.filter_by(
            account_id=user.account_id, challenge_id=challenge_id
        ).first()

        # Challenge not solved yet
        if not solves:
            # Hit max attempts
            max_tries = challenge.max_attempts
            if max_tries and fails >= max_tries > 0:
                return (
                    {
                        "success": True,
                        "data": {
                            "status": "incorrect",
                            "message": "You have 0 tries remaining",
                        },
                    },
                    403,
                )

            status, message = chal_class.attempt(challenge, request)
            if status:  # The challenge plugin says the input is right
                if ctftime() or current_user.is_admin():
                    chal_class.solve(
                        user=user, team=team, challenge=challenge, request=request
                    )
                    clear_standings()
                    clear_challenges()

                log(
                    "submissions",
                    "[{date}] {name} submitted {submission} on {challenge_id} with kpm {kpm} [CORRECT]",
                    name=user.name,
                    submission=request_data.get("submission", "").encode("utf-8"),
                    challenge_id=challenge_id,
                    kpm=kpm,
                )
                return {
                    "success": True,
                    "data": {"status": "correct", "message": message},
                }
            else:  # The challenge plugin says the input is wrong
                if ctftime() or current_user.is_admin():
                    chal_class.fail(
                        user=user, team=team, challenge=challenge, request=request
                    )
                    clear_standings()
                    clear_challenges()

                log(
                    "submissions",
                    "[{date}] {name} submitted {submission} on {challenge_id} with kpm {kpm} [WRONG]",
                    name=user.name,
                    submission=request_data.get("submission", "").encode("utf-8"),
                    challenge_id=challenge_id,
                    kpm=kpm,
                )

                if max_tries:
                    # Off by one since fails has changed since it was gotten
                    attempts_left = max_tries - fails - 1
                    tries_str = pluralize(attempts_left, singular="try", plural="tries")
                    # Add a punctuation mark if there isn't one
                    if message[-1] not in "!().;?[]{}":
                        message = message + "."
                    return {
                        "success": True,
                        "data": {
                            "status": "incorrect",
                            "message": "{} You have {} {} remaining.".format(
                                message, attempts_left, tries_str
                            ),
                        },
                    }
                else:
                    return {
                        "success": True,
                        "data": {"status": "incorrect", "message": message},
                    }

        # Challenge already solved
        else:
            log(
                "submissions",
                "[{date}] {name} submitted {submission} on {challenge_id} with kpm {kpm} [ALREADY SOLVED]",
                name=user.name,
                submission=request_data.get("submission", "").encode("utf-8"),
                challenge_id=challenge_id,
                kpm=kpm,
            )
            return {
                "success": True,
                "data": {
                    "status": "already_solved",
                    "message": "You already solved this",
                },
            }


@challenges_namespace.route("/<challenge_id>/solves")
class ChallengeSolves(Resource):
    @check_challenge_visibility
    @check_account_visibility
    @check_score_visibility
    @during_ctf_time_only
    @require_verified_emails
    def get(self, challenge_id):
        response = []
        challenge = Challenges.query.filter_by(id=challenge_id).first_or_404()

        # TODO: Need a generic challenge visibility call.
        # However, it should be stated that a solve on a gated challenge is not considered private.
        if challenge.state == "hidden" and is_admin() is False:
            abort(404)

        freeze = get_config("freeze")
        if freeze:
            preview = request.args.get("preview")
            if (is_admin() is False) or (is_admin() is True and preview):
                freeze = True
            elif is_admin() is True:
                freeze = False

        response = get_solves_for_challenge_id(challenge_id=challenge_id, freeze=freeze)

        return {"success": True, "data": response}


@challenges_namespace.route("/<challenge_id>/files")
class ChallengeFiles(Resource):
    @admins_only
    def get(self, challenge_id):
        response = []

        challenge_files = ChallengeFilesModel.query.filter_by(
            challenge_id=challenge_id
        ).all()

        for f in challenge_files:
            response.append({"id": f.id, "type": f.type, "location": f.location})
        return {"success": True, "data": response}


@challenges_namespace.route("/<challenge_id>/tags")
class ChallengeTags(Resource):
    @admins_only
    def get(self, challenge_id):
        response = []

        tags = Tags.query.filter_by(challenge_id=challenge_id).all()

        for t in tags:
            response.append(
                {"id": t.id, "challenge_id": t.challenge_id, "value": t.value}
            )
        return {"success": True, "data": response}


@challenges_namespace.route("/<challenge_id>/topics")
class ChallengeTopics(Resource):
    @admins_only
    def get(self, challenge_id):
        response = []

        topics = ChallengeTopicsModel.query.filter_by(challenge_id=challenge_id).all()

        for t in topics:
            response.append(
                {
                    "id": t.id,
                    "challenge_id": t.challenge_id,
                    "topic_id": t.topic_id,
                    "value": t.topic.value,
                }
            )
        return {"success": True, "data": response}


@challenges_namespace.route("/<challenge_id>/hints")
class ChallengeHints(Resource):
    @admins_only
    def get(self, challenge_id):
        hints = Hints.query.filter_by(challenge_id=challenge_id).all()
        schema = HintSchema(many=True)
        response = schema.dump(hints)

        if response.errors:
            return {"success": False, "errors": response.errors}, 400

        return {"success": True, "data": response.data}


@challenges_namespace.route("/<challenge_id>/flags")
class ChallengeFlags(Resource):
    @admins_only
    def get(self, challenge_id):
        flags = Flags.query.filter_by(challenge_id=challenge_id).all()
        schema = FlagSchema(many=True)
        response = schema.dump(flags)

        if response.errors:
            return {"success": False, "errors": response.errors}, 400

        return {"success": True, "data": response.data}


@challenges_namespace.route("/<challenge_id>/requirements")
class ChallengeRequirements(Resource):
    @admins_only
    def get(self, challenge_id):
        challenge = Challenges.query.filter_by(id=challenge_id).first_or_404()
        return {"success": True, "data": challenge.requirements}

#api/v1/challenges/instance
@challenges_namespace.route("/instance")
class ChallengeStart(Resource):

    @require_verified_emails
    @check_challenge_visibility
    @during_ctf_time_only
    def get(self): #get request cannot be performed until logged in thus safe enough to check if a container is running or not

#not yet ready do not test
        
        headers = request.headers

        #converting headers to mapping``
        head = {}
        
        #key validation for headers
        try:
            head["Userid"] = headers["userId"]
        except KeyError:
            return {"status": "Userid header missing"}, 400

        try:
            head["Username"] = headers["userName"]
        except KeyError:
            return {"status": "Username header missing"}, 400

        try:
            head["Useremail"] = headers["userEmail"]
        except KeyError:
            return {"status": "Useremail header missing"}, 400

        try:
            head["Challengeid"] = headers["challengeId"]
        except KeyError:
            return {"status": "Challengeid header missing"}, 400




        #checking for empty values
        for key in head.keys():
            val = head[key]
            if (val == "") or (val == None):
                return {"status":f"{key} cannot be empty"},400
            


        #userid input validation 
        try:
            if int(head["Userid"]) < 0:
                return {"status":"Userid cannot be negative"},400
        except ValueError:
            return {"status":"Userid must be an integer"},400
        except TypeError:
            return {"status":"Userid must be an integer"},400


        #useremail input validation    
        if " " in head["Useremail"]:
            return {"status":"Useremail cannot contain spaces"},400
        if "@" not in head["Useremail"]:
            return {"status":"Invalid Useremail"},400
        

        #challengeid input validation
        try:
            if int(head["Challengeid"]) < 0:
                return {"status":"Challengeid cannot be negative"},400
        except ValueError:
            return {"status":"Challengeid must be an integer"},400
        except TypeError:
            return {"status":"Challengeid must be an integer"},400


       
        #querying users table for data for the user by useris
        try:
        #data for user|table |   command    | column and value | does what it says
        #   [       ] [     ][              ][                ] [     ]
            Usersdata= Users.query.filter_by(id=head["Userid"]).first()
        except:
            return {"error":"database"},503


        #user authetication
        if not Usersdata:
            return {"error":"User does not exist"},404
        
        if head["Username"] != Usersdata.name:
            return {"error":"Credentials does not match"},401

        if head["Useremail"] != Usersdata.email:
            return {"error":"Credentials does not match"},401
        

        #getting container data
        Containersdata = Containers.query.filter_by(user_id=head["Userid"]).first()
        if not Containersdata:
            return {"error":"User does not have a container"},404


        return {"connection":f"{Containersdata.connection}"},200

    @require_verified_emails
    @check_challenge_visibility
    @during_ctf_time_only
    def post(self):
        
        headers = request.headers

        #converting headers to mapping``
        head = {}
        
        #key validation for headers
        try:
            head["Userid"] = headers["userId"]
        except KeyError:
            return {"status": "Userid header missing"}, 400

        try:
            head["Username"] = headers["userName"]
        except KeyError:
            return {"status": "Username header missing"}, 400

        try:
            head["Useremail"] = headers["userEmail"]
        except KeyError:
            return {"status": "Useremail header missing"}, 400

        try:
            head["Challengeid"] = headers["challengeId"]
        except KeyError:
            return {"status": "Challengeid header missing"}, 400




        #checking for empty values
        for key in head.keys():
            val = head[key]
            if (val == "") or (val == None):
                return {"status":f"{key} cannot be empty"},400
            


        #userid input validation 
        try:
            if int(head["Userid"]) < 0:
                return {"status":"Userid cannot be negative"},400
        except ValueError:
            return {"status":"Userid must be an integer"},400
        except TypeError:
            return {"status":"Userid must be an integer"},400


        #useremail input validation    
        if " " in head["Useremail"]:
            return {"status":"Useremail cannot contain spaces"},400
        if "@" not in head["Useremail"]:
            return {"status":"Invalid Useremail"},400
        

        #challengeid input validation
        try:
            if int(head["Challengeid"]) < 0:
                return {"status":"Challengeid cannot be negative"},400
        except ValueError:
            return {"status":"Challengeid must be an integer"},400
        except TypeError:
            return {"status":"Challengeid must be an integer"},400


       
        #querying users table for data for the user by useris
        try:
        #data for user|table |   command    | column and value | does what it says
        #   [       ] [     ][              ][                ] [     ]
            Usersdata= Users.query.filter_by(id=head["Userid"]).first()
        except:
            return {"error":"database"},503


        #user authetication
        if not Usersdata:
            return {"error":"User does not exist"},404
        
        if head["Username"] != Usersdata.name:
            return {"error":"Credentials does not match"},401

        if head["Useremail"] != Usersdata.email:
            return {"error":"Credentials does not match"},401
        

        #cahllenge id validation
        try:
            chal_data = Challenges.query.filter_by(id=head["Challengeid"]).first()
        except:
            return {"Error":"database"},503
        

        if not chal_data:
            return {"status":"Challenge does not exist"},404
        
        if chal_data.state == "hidden":
            return {"status":"Improper challengeid"},423

        if chal_data.category != "web":
            return {"status":"Improper request for challenge"},400

        try:
            #checking if challenge has been solved by user
            if Solves.query.filter_by(user_id=head["Userid"],challenge_id=head["Challengeid"]).first():
                return {"status":"User has already solved this challenge"},429
        except:
            return{"Error":"database"},503

        
        #checking if user already has a container
        Containersdata = Containers.query.filter_by(user_id=head["Userid"]).first()
        if Containersdata:
            return {"status":"User already has a container. Delete it first before creating a new one","connection_id":Containersdata.connection},409

        #port assigning
        while True:
            start = 45000
            end = 55000
            port = randint(start, end)
            #checking if port is available
            if not Ports.query.filter_by(port=port).first():
                #add port to database
                try:
                    port_data = Ports(port=port,userid=head["Userid"],status="open")
                    db.session.add(port_data)
                    db.session.commit()
                except:
                    return {"Server Error":"could not add port to database"},500
                break

        #creating payload

        image_id = portainer.imageid(head["Challengeid"])
        if not image_id:
            return {"Server Error":"image id not found"},500

        payload = portainer.payload(port=port,image=image_id)
        if not payload:
            return {"Server Error":"payload not found"},500
        
        #loading api key
        try:
            api_key = portainer.api_key()
            if not api_key:
                return {"Server Error":"api key not found"},500
        except:
            return {"Server Error":"could not load api key"},500
        

        

        container_name = f"{head['Username']}_{port}"
        #endpoint id
            #not yet implemented thus hardcoded
        endpoint = portainer.endpoint()

        #creating container
        response_create = portainer.create_continers(
            endpoint=endpoint,
            key=api_key,
            name=container_name,
            payload = payload
        )
        
        if not response_create:
            return {"Server Error":f"could not create container  -> no response {response_create.text}"},501
        
        try:
            if int(response_create.status_code) not in [200,201,202,204]:
                return {"Server Error":f"could not create container -> status_code {response_create.status_code}"},500
        except ValueError:
            return {"Server Error":"Bad response from the internal server"},500
              

        try:
            container_id = response_create.json()["Id"]
            print(f"\n{container_id} container id")
        except KeyError:
            return {"Server Error":f"could not create container -> status_code {response_create.status_code}"},500
        
        #updating port status

        port_record = Ports.query.filter_by(port=port).first()
        if not port:
            return {"Server Error":"port was not assinged before container creation"},500
        
        port_record.status = "in use"
        db.session.commit()

        #starting container
        
        response_start = portainer.start_container(
            endpoint_id=endpoint,
            key=api_key,
            container_id=container_id
        )
        
        
        try:
            if int(response_start.status_code) not in [200,201,202,204]:
                return {"Server Error":f"could not start container -> status_code {response_start.status_code}"},500
        except ValueError:
            return {"Server Error":"Bad response from the internal server"},500
        
        ip = portainer.ip()

        # try:
        connnection = Containers(
            challenge_id=int(head["Challengeid"]),
            user_id=int(head["Userid"]),
            container_name=str(container_name),
            container_id=str(container_id),
            connection=f"{ip}:{port}", #change localhost to repective ip
            )

        db.session.add(connnection)
        db.session.commit()

        
        

        return {"status":"success","connection":f"{ip}:{port}"}, 200
        

    @require_verified_emails
    @check_challenge_visibility
    @during_ctf_time_only
    def delete(self):
        
        headers = request.headers

        #converting headers to mapping``
        head = {}
        
        #key validation for headers
        try:
            head["Userid"] = headers["userId"]
        except KeyError:
            return {"status": "Userid header missing"}, 400

        try:
            head["Username"] = headers["userName"]
        except KeyError:
            return {"status": "Username header missing"}, 400

        try:
            head["Useremail"] = headers["userEmail"]
        except KeyError:
            return {"status": "Useremail header missing"}, 400

        try:
            head["Challengeid"] = headers["challengeId"]
        except KeyError:
            return {"status": "Challengeid header missing"}, 400




        #checking for empty values
        for key in head.keys():
            val = head[key]
            if (val == "") or (val == None):
                return {"status":f"{key} cannot be empty"},400
            


        #userid input validation 
        try:
            if int(head["Userid"]) < 0:
                return {"status":"Userid cannot be negative"},400
        except ValueError:
            return {"status":"Userid must be an integer"},400
        except TypeError:
            return {"status":"Userid must be an integer"},400


        #useremail input validation    
        if " " in head["Useremail"]:
            return {"status":"Useremail cannot contain spaces"},400
        if "@" not in head["Useremail"]:
            return {"status":"Invalid Useremail"},400
        

        #challengeid input validation
        try:
            if int(head["Challengeid"]) < 0:
                return {"status":"Challengeid cannot be negative"},400
        except ValueError:
            return {"status":"Challengeid must be an integer"},400
        except TypeError:
            return {"status":"Challengeid must be an integer"},400


       
        #querying users table for data for the user by useris
        try:
        #data for user|table |   command    | column and value | does what it says
        #   [       ] [     ][              ][                ] [     ]
            Usersdata= Users.query.filter_by(id=head["Userid"]).first()
        except:
            return {"error":"database"},503


        #user authetication
        if not Usersdata:
            return {"error":"User does not exist"},404
        
        if head["Username"] != Usersdata.name:
            return {"error":"Credentials does not match"},401

        if head["Useremail"] != Usersdata.email:
            return {"error":"Credentials does not match"},401
        

        #challenge id validation
        try:
            chal_data = Challenges.query.filter_by(id=head["Challengeid"]).first()
        except:
            return {"Error":"database"},503
        

        
        #getting container data
        Containersdata = Containers.query.filter_by(user_id=head["Userid"]).first()
        if not Containersdata:
            return {"status":"User has  no contaier running "}, 404
        

        #loading api key
        try:
            api_key = portainer.api_key()
            if not api_key:
                return {"Server Error":"api key not found"},500
        except:
            return {"Server Error":"could not load api key"},500
        
        #loading endpoint
        endpoint = portainer.endpoint()

        #loading container id
        container_id = Containersdata.container_id

        #filtering port from connection
        port = str(Containersdata.connection).split(":")[1]
        
        #loading port data fromm Ports table
        Portdata = Ports.query.filter_by(port=port).first()

        #if port was not added to the database while creating 
        if not Portdata:
            #booking the port with status as closing
            port_add = Ports(port=port,userid=head["Userid"],status="closing")
            db.session.add(port_add)
            db.session.commit()
            Portdata = Ports.query.filter_by(port=port).first()
        
        

        #deleting container
        response_delete = portainer.delete_containers(
            endpoint=endpoint,
            key=api_key,
            id = container_id) 
        
        try:
            response_status = int(response_delete.status_code)
        except ValueError:
            return {'status':"unexpected status code"}
        
        #checking status code
        if (response_status in [400,404,409,500]):
            return {"error":f"could not delete container response -> {response_delete.status_code}"},500



        #updating port
        try:
            Portdata = Ports.query.filter_by(port=port).first()
            Portdata.status = "closed"
            db.session.commit()
        except:
            return {"error":"container deleted but port not updated"},207

        return {"status":"container deleted"},200

