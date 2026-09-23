from sqlalchemy import Select, select

from app.db_models import SupportTicket


def build_user_support_tickets_stmt(pubkey: str) -> Select:
    return (
        select(SupportTicket)
        .where(SupportTicket.pubkey == pubkey)
        .order_by(SupportTicket.last_message_at.desc(), SupportTicket.id.desc())
    )
