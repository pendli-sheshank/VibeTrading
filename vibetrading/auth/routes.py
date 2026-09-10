from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users import exceptions

from vibetrading.auth.backend import (
    clear_session_cookie,
    current_dashboard_user,
    get_jwt_strategy,
    set_session_cookie,
)
from vibetrading.auth.manager import UserManager, get_user_manager
from vibetrading.auth.schemas import UserCreate
from vibetrading.dashboard.templating import templates
from vibetrading.rate_limit import limiter

router = APIRouter(include_in_schema=False)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, registered: str | None = None):
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "registered": bool(registered)}
    )


@router.post("/login", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def login_submit(
    request: Request,
    credentials: OAuth2PasswordRequestForm = Depends(),
    user_manager: UserManager = Depends(get_user_manager),
):
    user = await user_manager.authenticate(credentials)
    if user is None or not user.is_active:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect email or password.", "registered": False},
            status_code=400,
        )

    strategy = get_jwt_strategy()
    token = await strategy.write_token(user)
    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, token)
    return response


@router.post("/logout")
async def logout_submit(user=Depends(current_dashboard_user)):
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None})


@router.post("/register", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    user_manager: UserManager = Depends(get_user_manager),
):
    try:
        await user_manager.create(UserCreate(email=email, password=password))
    except exceptions.UserAlreadyExists:
        return templates.TemplateResponse(
            request, "register.html", {"error": "An account with that email already exists."}, status_code=400
        )
    except exceptions.InvalidPasswordException as exc:
        return templates.TemplateResponse(request, "register.html", {"error": exc.reason}, status_code=400)

    return RedirectResponse(url="/login?registered=1", status_code=303)
