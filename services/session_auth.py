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
    """Telas operacionais (Mural/Embalagem/Gaiolas/Endereços). A sessão de
    gerente também passa: quem tem a senha do Gerente já pode reverter status e
    desconectar loja, então barrá-lo no Mural só obrigava a logar duas vezes.
    O contrário continua valendo — `auth` NÃO abre a tela do Gerente."""
    if not (request.session.get("auth") or request.session.get("gerente")):
        raise NotAuthenticated("/login")


def check_gerente_password(senha: str) -> bool:
    return secrets.compare_digest(senha, get_settings().gerente_password)


def require_gerente(request: Request) -> None:
    if not request.session.get("gerente"):
        raise NotAuthenticated("/gerente/login")


def require_login_or_gerente(request: Request) -> None:
    """Sinônimo de `require_login` desde que a sessão de gerente passou a valer
    nas telas operacionais. Mantido pelo nome explícito nas rotas compartilhadas
    (impressão/assinatura QZ Tray), onde deixa claro que os dois entram."""
    require_login(request)
