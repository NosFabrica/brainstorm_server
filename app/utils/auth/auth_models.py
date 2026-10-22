from enum import Enum

from pydantic import BaseModel


class TokenType(str, Enum):
    USER = "USER"
    # ADMIN = "ADMIN"


class JWTData(BaseModel):
    nostr_pubkey: str
    token_type: TokenType = TokenType.USER
    is_admin: bool = False
    session_version: int = 0
