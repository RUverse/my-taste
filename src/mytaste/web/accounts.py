"""Setup, signing in and out, switching profiles, your own account, and managing people."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mytaste.accounts.models import (
    MIN_PASSWORD_LENGTH,
    ROLE_LABELS,
    SecretKind,
    User,
    username_from,
)
from mytaste.storage.preferences import DisplayPreferences
from mytaste.storage.users import UserRepository
from mytaste.web.signin import Throttle, safe_next, sign_in, sign_out

# Whether the sign-in page shows everyone's profiles to pick from, like a TV at home.
PROFILE_PICKER = "profile_picker"
_SECRET_KINDS: tuple[SecretKind, ...] = ("password", "pin", "")


def create_accounts_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()
    throttle = Throttle()

    def users_of(request: Request) -> UserRepository:
        return request.app.state.users

    def picker_on(request: Request) -> bool:
        return request.app.state.instance_settings.flag(PROFILE_PICKER, True)

    def render(
        request: Request, template: str, context: dict[str, object], *, status_code: int = 200
    ) -> HTMLResponse:
        preferences = request.app.state.preferences
        me = request.state.user
        shared: dict[str, object] = {
            "request": request,
            "current_path": request.url.path,
            "display": preferences.get_display() if me else DisplayPreferences(),
            "header_has_library": False,
            "header_media": "all",
            "search_query": "",
            "min_password": MIN_PASSWORD_LENGTH,
        }
        shared.update(context)
        return templates.TemplateResponse(
            request=request, name=template, context=shared, status_code=status_code
        )

    # Setup ------------------------------------------------------------------------------

    @router.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> Response:
        if users_of(request).count():
            return RedirectResponse("/login", status_code=303)
        return render(request, "signin.html", {"mode": "setup", "form": {}})

    @router.post("/setup", response_class=HTMLResponse)
    async def setup(request: Request) -> Response:
        users = users_of(request)
        if users.count():
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        name = str(form.get("name") or "")
        username = str(form.get("username") or "") or username_from(name)
        password = str(form.get("password") or "")
        values = {"name": name, "username": username}
        try:
            if password != str(form.get("confirm") or ""):
                raise ValueError("The passwords don't match")
            owner = users.create(
                name=name, username=username, role="owner", secret_kind="password", secret=password
            )
        except ValueError as exc:
            context = {"mode": "setup", "form": values, "error": str(exc)}
            return render(request, "signin.html", context, status_code=422)
        response = RedirectResponse("/settings", status_code=303)
        sign_in(request, response, owner, users)
        return response

    # Signing in -------------------------------------------------------------------------

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        users = users_of(request)
        if not users.count():
            return RedirectResponse("/setup", status_code=303)
        next_path = safe_next(request.query_params.get("next"))
        chosen = _int(request.query_params.get("user"))
        profile = users.get(chosen) if chosen else None
        if profile is not None and profile.secret_kind:
            return render(
                request, "signin.html", {"mode": "secret", "profile": profile, "next": next_path}
            )
        if picker_on(request):
            context = {"mode": "picker", "profiles": users.list(), "next": next_path}
            return render(request, "signin.html", context)
        return render(request, "signin.html", {"mode": "password", "next": next_path, "form": {}})

    @router.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Response:
        users = users_of(request)
        form = await request.form()
        next_path = safe_next(form.get("next"))
        secret = str(form.get("secret") or "")
        user_id = _int(form.get("user"))
        username = str(form.get("username") or "")
        picked = user_id is not None
        user = users.get(user_id) if picked else users.by_username(username)

        def failed(message: str, status: int = 401) -> HTMLResponse:
            if picked and user is not None and user.secret_kind:
                context: dict[str, object] = {"mode": "secret", "profile": user}
            elif picked:
                context = {"mode": "picker", "profiles": users.list()}
            else:
                context = {"mode": "password", "form": {"username": username}}
            context.update({"next": next_path, "error": message})
            return render(request, "signin.html", context, status_code=status)

        if user is None:
            return failed("That username or password is wrong" if not picked else "Pick a profile")
        if picked and not picker_on(request):
            return failed("Sign in with your username and password")
        if not user.secret_kind and not picked:
            return failed("This profile has no password; pick it on the profile screen")
        wait = throttle.wait(user.id)
        if wait:
            return failed(f"Too many wrong tries. Try again in {wait} seconds.", 429)
        if not users.verify(user.id, secret):
            throttle.failed(user.id)
            what = "PIN" if user.secret_kind == "pin" else "password"
            return failed(
                f"That {what} is wrong" if picked else "That username or password is wrong"
            )
        throttle.succeeded(user.id)
        response = RedirectResponse(next_path, status_code=303)
        sign_in(request, response, user, users)
        return response

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        response = RedirectResponse("/login", status_code=303)
        sign_out(request, response, users_of(request))
        return response

    # Your account -----------------------------------------------------------------------

    def account_context(request: Request, **extra: object) -> dict[str, object]:
        me: User = request.state.user
        context: dict[str, object] = {
            "secret_kinds": ("password",) if me.is_admin else _SECRET_KINDS,
            "saved": request.query_params.get("saved", ""),
        }
        context.update(extra)
        return context

    @router.get("/account", response_class=HTMLResponse)
    async def account_page(request: Request) -> HTMLResponse:
        return render(request, "account.html", account_context(request))

    @router.post("/account", response_class=HTMLResponse)
    async def save_account(request: Request) -> Response:
        me: User = request.state.user
        form = await request.form()
        try:
            users_of(request).update(me.id, name=str(form.get("name") or ""))
        except ValueError as exc:
            context = account_context(request, name_error=str(exc))
            return render(request, "account.html", context, status_code=422)
        return RedirectResponse("/account?saved=name", status_code=303)

    @router.post("/account/secret", response_class=HTMLResponse)
    async def save_account_secret(request: Request) -> Response:
        me: User = request.state.user
        users = users_of(request)
        form = await request.form()
        kind = str(form.get("kind") or "")
        secret = str(form.get("secret") or "")
        try:
            if kind not in _SECRET_KINDS:
                raise ValueError("Choose how to sign in")
            if me.secret_kind and not users.verify(me.id, str(form.get("current") or "")):
                raise ValueError("Your current password or PIN is wrong")
            if kind and secret != str(form.get("confirm") or ""):
                raise ValueError("The two entries don't match")
            users.set_secret(me.id, kind, secret)  # type: ignore[arg-type]
        except ValueError as exc:
            context = account_context(request, secret_error=str(exc), chosen_kind=kind)
            return render(request, "account.html", context, status_code=422)
        return RedirectResponse("/account?saved=secret", status_code=303)

    # People (admins) --------------------------------------------------------------------

    def people_context(request: Request, **extra: object) -> dict[str, object]:
        users = users_of(request)
        people = users.list()
        context: dict[str, object] = {
            "people": people,
            "access": {person.id: users.libraries_for(person.id) for person in people},
            "libraries": request.app.state.library.libraries(),
            "roles": ROLE_LABELS,
            "picker": picker_on(request),
            "saved": request.query_params.get("saved", ""),
            "new_person": {},
        }
        context.update(extra)
        return context

    def may_manage(me: User, person: User) -> bool:
        """The owner manages everyone; admins manage members and themselves."""

        return me.is_owner or person.role == "member" or person.id == me.id

    @router.get("/settings/people", response_class=HTMLResponse)
    async def people_page(request: Request) -> HTMLResponse:
        return render(request, "people.html", people_context(request))

    @router.post("/settings/people", response_class=HTMLResponse)
    async def add_person(request: Request) -> Response:
        me: User = request.state.user
        users = users_of(request)
        form = await request.form()
        name = str(form.get("name") or "")
        role = str(form.get("role") or "member")
        kind = str(form.get("kind") or "")
        values = {"name": name, "role": role, "kind": kind}
        try:
            if role not in ("admin", "member"):
                raise ValueError("Choose admin or member")
            if role == "admin" and not me.is_owner:
                raise ValueError("Only the owner can add admins")
            if kind not in _SECRET_KINDS:
                raise ValueError("Choose how they sign in")
            person = users.create(
                name=name,
                username=_free_username(users, str(form.get("username") or "") or name),
                role=role,  # type: ignore[arg-type]
                secret_kind=kind,  # type: ignore[arg-type]
                secret=str(form.get("secret") or ""),
            )
            users.set_libraries(person.id, _library_ids(form.getlist("libraries")))
        except ValueError as exc:
            context = people_context(request, add_error=str(exc), new_person=values)
            return render(request, "people.html", context, status_code=422)
        return RedirectResponse(f"/settings/people?saved={person.id}", status_code=303)

    async def managed(request: Request, person_id: int) -> tuple[User, User] | Response:
        me: User = request.state.user
        person = users_of(request).get(person_id)
        if person is None:
            return Response("No such person.", status_code=404)
        if not may_manage(me, person):
            return Response("Only the owner can change admins.", status_code=403)
        return me, person

    @router.post("/settings/people/{person_id:int}", response_class=HTMLResponse)
    async def update_person(person_id: int, request: Request) -> Response:
        found = await managed(request, person_id)
        if isinstance(found, Response):
            return found
        me, person = found
        users = users_of(request)
        form = await request.form()
        role = str(form.get("role") or person.role)
        try:
            if role != person.role and not me.is_owner:
                raise ValueError("Only the owner can change roles")
            users.update(person.id, name=str(form.get("name") or person.name), role=role)  # type: ignore[arg-type]
            if role == "member":
                users.set_libraries(person.id, _library_ids(form.getlist("libraries")))
        except ValueError as exc:
            context = people_context(request, person_error=str(exc), error_for=person.id)
            return render(request, "people.html", context, status_code=422)
        return RedirectResponse(f"/settings/people?saved={person.id}", status_code=303)

    @router.post("/settings/people/{person_id:int}/secret", response_class=HTMLResponse)
    async def reset_secret(person_id: int, request: Request) -> Response:
        found = await managed(request, person_id)
        if isinstance(found, Response):
            return found
        _me, person = found
        users = users_of(request)
        form = await request.form()
        kind = str(form.get("kind") or "")
        try:
            if kind not in _SECRET_KINDS:
                raise ValueError("Choose how they sign in")
            users.set_secret(person.id, kind, str(form.get("secret") or ""))  # type: ignore[arg-type]
            users.end_sessions(person.id)
        except ValueError as exc:
            context = people_context(request, person_error=str(exc), error_for=person.id)
            return render(request, "people.html", context, status_code=422)
        return RedirectResponse(f"/settings/people?saved={person.id}", status_code=303)

    @router.post("/settings/people/{person_id:int}/signout")
    async def sign_out_everywhere(person_id: int, request: Request) -> Response:
        found = await managed(request, person_id)
        if isinstance(found, Response):
            return found
        users_of(request).end_sessions(person_id)
        return RedirectResponse(f"/settings/people?saved={person_id}", status_code=303)

    @router.post("/settings/people/{person_id:int}/remove", response_class=HTMLResponse)
    async def remove_person(person_id: int, request: Request) -> Response:
        found = await managed(request, person_id)
        if isinstance(found, Response):
            return found
        me, person = found
        if person.id == me.id:
            context = people_context(
                request, person_error="You can't remove yourself", error_for=person.id
            )
            return render(request, "people.html", context, status_code=422)
        try:
            users_of(request).delete(person.id)
        except ValueError as exc:
            context = people_context(request, person_error=str(exc), error_for=person.id)
            return render(request, "people.html", context, status_code=422)
        return RedirectResponse("/settings/people", status_code=303)

    @router.post("/settings/people/{person_id:int}/owner", response_class=HTMLResponse)
    async def hand_over(person_id: int, request: Request) -> Response:
        me: User = request.state.user
        if not me.is_owner:
            return Response("Only the owner can hand over ownership.", status_code=403)
        try:
            users_of(request).make_owner(person_id)
        except ValueError as exc:
            context = people_context(request, person_error=str(exc), error_for=person_id)
            return render(request, "people.html", context, status_code=422)
        return RedirectResponse("/settings/people", status_code=303)

    @router.post("/settings/people/picker")
    async def save_picker(request: Request) -> Response:
        form = await request.form()
        request.app.state.instance_settings.save_flag(PROFILE_PICKER, form.get("picker") == "on")
        return RedirectResponse("/settings/people?saved=picker", status_code=303)

    return router


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _library_ids(values: list[object]) -> tuple[int, ...]:
    ids = tuple(number for number in (_int(value) for value in values) if number is not None)
    return ids


def _free_username(users: UserRepository, wanted: str) -> str:
    """``wanted`` as a username, numbered if someone already has it (sara, sara2, …)."""

    base = username_from(wanted)
    candidate, number = base, 1
    while users.by_username(candidate) is not None:
        number += 1
        candidate = f"{base[: 32 - len(str(number))]}{number}"
    return candidate
