"""Armazena os tokens OAuth de cada loja no Firestore.

Estrutura no Firestore:
  coleção: meli_stores
    documento: <user_id>           (ID do vendedor no Mercado Livre)
      - user_id: str
      - company_key: str           (ex: "EMPRESA_A")
      - sheet_tab: str             (ex: "Empresa A")
      - access_token: str
      - refresh_token: str
      - nickname: str
      - updated_at: timestamp
"""
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.api_core.exceptions import AlreadyExists
from config.settings import get_settings

PROCESSED_ORDERS_COLLECTION = "erpex_processed_orders"


def _client() -> firestore.Client:
    settings = get_settings()
    kwargs = {"database": settings.firestore_database}
    if settings.gcp_project:
        kwargs["project"] = settings.gcp_project
    if settings.google_service_account_json.endswith(".json"):
        return firestore.Client.from_service_account_json(
            settings.google_service_account_json, **kwargs
        )
    return firestore.Client(**kwargs)


class TokenStore:
    def __init__(self):
        self.db = _client()
        self.col = self.db.collection(get_settings().firestore_collection)

    def save_store(self, user_id: str, company_key: str, sheet_tab: str,
                   access_token: str, refresh_token: str, nickname: str = "") -> None:
        self.col.document(str(user_id)).set({
            "user_id": str(user_id),
            "company_key": company_key,
            "sheet_tab": sheet_tab,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "nickname": nickname,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)

    def update_tokens(self, user_id: str, access_token: str, refresh_token: str) -> None:
        self.col.document(str(user_id)).update({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })

    def get_store(self, user_id: str) -> dict | None:
        doc = self.col.document(str(user_id)).get()
        return doc.to_dict() if doc.exists else None

    def get_by_company(self, company_key: str) -> dict | None:
        query = self.col.where(filter=FieldFilter("company_key", "==", company_key)).limit(1).stream()
        for doc in query:
            return doc.to_dict()
        return None

    def get_by_company_or_nickname(self, value: str) -> dict | None:
        """Aceita tanto company_key quanto nickname do ML — a coluna "Empresa" da
        aba Vendas grava o nickname quando existe (ver OrderItem.to_sheet_row), então
        qualquer rota que recebe esse valor de volta (ex.: links de reimpressão) precisa
        resolver os dois, não só o company_key."""
        store = self.get_by_company(value) or self.get_by_company(value.upper())
        if store:
            return store
        for s in self.list_stores():
            if (s.get("nickname") or "").lower() == value.lower():
                return s
        return None

    def list_stores(self) -> list[dict]:
        return [doc.to_dict() for doc in self.col.stream()]

    def delete_store(self, user_id: str) -> None:
        """Remove os tokens de uma loja (desconectar). Precisa reconectar via OAuth depois."""
        self.col.document(str(user_id)).delete()

    def set_color(self, user_id: str, cor: str) -> None:
        """Cor do card da loja (definida pelo Gerente em /gerente/lojas), usada pra
        diferenciar visualmente os pedidos de cada loja no Mural/Embalagem."""
        self.col.document(str(user_id)).update({"cor": cor})

    def set_sku_prefixo(self, user_id: str, sku_prefixo: str) -> None:
        """Prefixo do SKU dessa loja (definido pelo Gerente em /gerente/lojas),
        usado só pra AGRUPAR visualmente a tela de Endereçamento por loja —
        o endereço em si não pertence a nenhuma loja (ver services/enderecos_db.py)."""
        self.col.document(str(user_id)).update({"sku_prefixo": sku_prefixo})

    def get_cores_por_empresa(self) -> dict[str, str]:
        """company_key/nickname -> cor (hex), só pras lojas que já tiverem uma definida.

        A coluna "Empresa" da aba Vendas grava o nickname da conta ML quando ele
        existe (ver OrderItem.to_sheet_row) — indexa pelos dois pra não depender de
        company_key e nickname coincidirem (como acontece só por acaso na PREPLOG)."""
        cores = {}
        for l in self.list_stores():
            cor = l.get("cor")
            if not cor:
                continue
            cores[l["company_key"]] = cor
            if l.get("nickname"):
                cores[l["nickname"]] = cor
        return cores

    def claim_order(self, order_id: str) -> bool:
        """Reserva um order_id de forma ATÔMICA (anti-duplicata em webhooks concorrentes).

        Retorna True se for a 1ª vez (pode processar); False se já foi reservado.
        O `create()` do Firestore é atômico: só o primeiro de N chamadas simultâneas vence.
        """
        ref = self.db.collection(PROCESSED_ORDERS_COLLECTION).document(str(order_id))
        try:
            ref.create({"order_id": str(order_id), "at": firestore.SERVER_TIMESTAMP})
            return True
        except AlreadyExists:
            return False
