"""Autenticação simples do web app (Mural/Gaiolas): 1 senha compartilhada + sessão."""
import secrets
from fastapi import Request
from config.settings import get_settings


class NotAuthenticated(Exception):
    """Levantada quando a rota exige sessão e ela não existe/expirou."""

    def __init__(self, redirect_to: str = "/login"):
        self.redirect_to = redirect_to
        super().__init__(redirect_to)


def check_password(senha: str) -> bool:
    return secrets.compare_digest(senha, get_settings().mural_password)


def require_login(request: Request) -> None:
    if not request.session.get("auth"):
        raise NotAuthenticated("/login")


def check_gerente_password(senha: str) -> bool:
    return secrets.compare_digest(senha, get_settings().gerente_password)


def require_gerente(request: Request) -> None:
    if not request.session.get("gerente"):
        raise NotAuthenticated("/gerente/login")


def require_login_or_gerente(request: Request) -> None:
    """Aceita sessão operacional OU de gerente — usado em rotas compartilhadas
    (ex.: impressão/assinatura QZ Tray) que qualquer um dos dois pode acessar."""
    if not (request.session.get("auth") or request.session.get("gerente")):
        raise NotAuthenticated("/login")
