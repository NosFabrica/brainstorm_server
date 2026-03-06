from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend

from app.core.config import settings
from app.core.database import engine
from app.db_models import (
    BrainstormRequest,
    BrainstormNsec,
    BrainstormNostrRelayTransfer,
)
from app.utils.auth.auth_util import (
    sql_admin_create_jwt_token,
    sql_admin_decrypt_jwt_token,
)


class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username, password = form["username"], form["password"]

        if (
            username == settings.sql_admin_username
            and password == settings.sql_admin_password
        ):
            sqladmin_token = sql_admin_create_jwt_token()
            request.session.update({"admin_panel_token": sqladmin_token})
            return True
        else:
            return False

    async def logout(self, request: Request) -> bool:
        # Usually you'd want to just clear the session
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> RedirectResponse | bool:
        sqladmin_token = request.session.get("admin_panel_token")

        if (
            not sqladmin_token
            or datetime.now() > sql_admin_decrypt_jwt_token(sqladmin_token).expires_date
        ):
            return RedirectResponse(request.url_for("admin:login"), status_code=302)

        return True


def add_sql_admin_panel(app: FastAPI):
    authentication_backend = AdminAuth(secret_key=settings.sql_admin_secret_key)
    admin = Admin(app, engine, authentication_backend=authentication_backend)

    class BrainstormRequestAdmin(ModelView, model=BrainstormRequest):
        column_list = [BrainstormRequest.private_id, BrainstormRequest.password, BrainstormRequest.status, BrainstormRequest.status_ta_publication, BrainstormRequest.status_internal_brainstorm_publication, BrainstormRequest.parameters, BrainstormRequest.algorithm, BrainstormRequest.count_values, BrainstormRequest.created_at, BrainstormRequest.updated_at]  # type: ignore

    admin.add_view(BrainstormRequestAdmin)

    class BrainstormNsecAdmin(ModelView, model=BrainstormNsec):
        column_list = BrainstormNsec.__table__.columns  # type: ignore

    admin.add_view(BrainstormNsecAdmin)

    class BrainstormNostrRelayTransferAdmin(
        ModelView, model=BrainstormNostrRelayTransfer
    ):
        column_list = BrainstormNostrRelayTransfer.__table__.columns  # type: ignore

    admin.add_view(BrainstormNostrRelayTransferAdmin)
