"""Armazena os tokens OAuth de cada loja no Postgres (tabela `lojas`).

Migrado do Firestore em 2026-07-28 — a lógica de OAuth/refresh do Mercado Livre
(services/meli_service.py, routers/auth.py) não mudou, só o backend de onde os
tokens são lidos/gravados. Mesmo estilo enxuto de services/db.py: sem pool/ORM,
conexão nova por chamada.
"""
from services.db import _get_connection

PROCESSED_ORDERS_TABLE = "pedidos_processados"


class TokenStore:
    async def save_store(self, user_id: str, company_key: str, sheet_tab: str,
                          access_token: str, refresh_token: str, nickname: str = "") -> None:
        conn = await _get_connection()
        try:
            await conn.execute(
                """INSERT INTO lojas (user_id, company_key, sheet_tab, access_token, refresh_token, nickname)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (user_id) DO UPDATE SET
                       company_key = EXCLUDED.company_key, sheet_tab = EXCLUDED.sheet_tab,
                       access_token = EXCLUDED.access_token, refresh_token = EXCLUDED.refresh_token,
                       nickname = EXCLUDED.nickname, atualizado_em = now()""",
                str(user_id), company_key, sheet_tab, access_token, refresh_token, nickname,
            )
        finally:
            await conn.close()

    async def update_tokens(self, user_id: str, access_token: str, refresh_token: str) -> None:
        conn = await _get_connection()
        try:
            await conn.execute(
                "UPDATE lojas SET access_token = $2, refresh_token = $3, atualizado_em = now() WHERE user_id = $1",
                str(user_id), access_token, refresh_token,
            )
        finally:
            await conn.close()

    async def get_store(self, user_id: str) -> dict | None:
        conn = await _get_connection()
        try:
            row = await conn.fetchrow("SELECT * FROM lojas WHERE user_id = $1", str(user_id))
            return dict(row) if row else None
        finally:
            await conn.close()

    async def get_by_company(self, company_key: str) -> dict | None:
        conn = await _get_connection()
        try:
            row = await conn.fetchrow("SELECT * FROM lojas WHERE company_key = $1 LIMIT 1", company_key)
            return dict(row) if row else None
        finally:
            await conn.close()

    async def get_by_company_or_nickname(self, value: str) -> dict | None:
        """Aceita tanto company_key quanto nickname do ML — a coluna "Empresa" da
        aba Vendas grava o nickname quando existe (ver OrderItem.to_sheet_row), então
        qualquer rota que recebe esse valor de volta (ex.: links de reimpressão) precisa
        resolver os dois, não só o company_key."""
        store = await self.get_by_company(value) or await self.get_by_company(value.upper())
        if store:
            return store
        for s in await self.list_stores():
            if (s.get("nickname") or "").lower() == value.lower():
                return s
        return None

    async def list_stores(self) -> list[dict]:
        conn = await _get_connection()
        try:
            rows = await conn.fetch("SELECT * FROM lojas ORDER BY company_key")
            return [dict(r) for r in rows]
        finally:
            await conn.close()

    async def delete_store(self, user_id: str) -> None:
        """Remove os tokens de uma loja (desconectar). Precisa reconectar via OAuth depois."""
        conn = await _get_connection()
        try:
            await conn.execute("DELETE FROM lojas WHERE user_id = $1", str(user_id))
        finally:
            await conn.close()

    async def set_color(self, user_id: str, cor: str) -> None:
        """Cor do card da loja (definida pelo Gerente em /gerente/lojas), usada pra
        diferenciar visualmente os pedidos de cada loja no Mural/Embalagem."""
        conn = await _get_connection()
        try:
            await conn.execute("UPDATE lojas SET cor = $2 WHERE user_id = $1", str(user_id), cor)
        finally:
            await conn.close()

    async def set_sku_prefixo(self, user_id: str, sku_prefixo: str) -> None:
        """Prefixo do SKU dessa loja (definido pelo Gerente em /gerente/lojas),
        usado só pra AGRUPAR visualmente a tela de Endereçamento por loja —
        o endereço em si não pertence a nenhuma loja (ver services/enderecos_db.py)."""
        conn = await _get_connection()
        try:
            await conn.execute("UPDATE lojas SET sku_prefixo = $2 WHERE user_id = $1", str(user_id), sku_prefixo)
        finally:
            await conn.close()

    async def get_cores_por_empresa(self) -> dict[str, str]:
        """company_key/nickname -> cor (hex), só pras lojas que já tiverem uma definida.

        A coluna "Empresa" da aba Vendas grava o nickname da conta ML quando ele
        existe (ver OrderItem.to_sheet_row) — indexa pelos dois pra não depender de
        company_key e nickname coincidirem (como acontece só por acaso na PREPLOG)."""
        cores = {}
        for l in await self.list_stores():
            cor = l.get("cor")
            if not cor:
                continue
            cores[l["company_key"]] = cor
            if l.get("nickname"):
                cores[l["nickname"]] = cor
        return cores

    async def claim_order(self, order_id: str) -> bool:
        """Reserva um order_id de forma ATÔMICA (anti-duplicata em webhooks concorrentes).

        Retorna True se for a 1ª vez (pode processar); False se já foi reservado.
        O UNIQUE + ON CONFLICT DO NOTHING do Postgres garante que só o 1º de N
        INSERTs simultâneos vence — mesma garantia que o ref.create() do Firestore.
        """
        conn = await _get_connection()
        try:
            row = await conn.fetchrow(
                f"INSERT INTO {PROCESSED_ORDERS_TABLE} (order_id) VALUES ($1) "
                "ON CONFLICT (order_id) DO NOTHING RETURNING order_id",
                str(order_id),
            )
            return row is not None
        finally:
            await conn.close()
