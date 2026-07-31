from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    google_sheet_id: str
    google_service_account_json: str = "service_account.json"
    sheet_template_tab: str = "Empresa A"   # aba usada como modelo p/ novas lojas

    # App única do Mercado Livre (mesma para todas as lojas)
    meli_client_id: str = ""
    meli_client_secret: str = ""
    meli_redirect_uri: str = "http://localhost:8080/auth/meli/callback"

    # Firestore (infere o projeto da service account; opcional sobrescrever)
    gcp_project: str = ""
    firestore_database: str = "(default)"   # use "erpex" se o banco for nomeado
    firestore_collection: str = "meli_stores"

    webhook_secret: str = ""
    port: int = 8080

    # Web app (Mural/Gaiolas/Embalagem): login por senha única compartilhada + cookie de sessão
    mural_password: str = ""
    session_secret_key: str = ""
    session_cookie_https_only: bool = True

    # Tela do Gerente (auditoria/reversão de status/conexão de lojas): senha própria,
    # separada da operacional — operador do galpão não deve acessar essa tela.
    gerente_password: str = ""

    # Nomes exatos das impressoras (Windows) usadas na tela de Embalagem — QZ Tray
    # manda cada etiqueta pra sua impressora. Vazio = usa a impressora padrão do Windows.
    printer_pedido_name: str = ""
    printer_meli_name: str = ""
    # Impressora comum (Wi-Fi, não térmica) usada pra guia de retirada de gaiola —
    # documento de página inteira (HTML), não etiqueta ZPL.
    printer_guia_name: str = ""

    # Cloud SQL (Postgres) — dados de notificação/cancelamento (services/db.py).
    # Não substitui o Sheets; é um banco à parte só pra esse tipo de dado.
    cloudsql_instance_connection_name: str = ""
    cloudsql_db: str = "erpex"
    cloudsql_user: str = ""
    cloudsql_password: str = ""

    class Config:
        env_file = ".env"
        extra = "allow"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Mapeia chave da empresa → nome da aba no Sheets (ex: EMPRESA_A → "Empresa A")
def company_key_to_sheet_tab(company_key: str) -> str:
    return company_key.replace("_", " ").title()
