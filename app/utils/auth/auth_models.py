from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class TokenType(str, Enum):
    USER = "USER"
    # ADMIN = "ADMIN"


class JWTData(BaseModel):
    nostr_pubkey: str
    token_type: TokenType = TokenType.USER
    expires_date: datetime
    is_admin: bool = False


class JWTSQLAdminData(BaseModel):
    expires_date: datetime
