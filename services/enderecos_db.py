"""Endereçamento (SKU -> corredor/estante/prateleira) — Postgres, fase 1 da
migração pra fora do Sheets. SKU é chave única global (sem loja): o endereço é
do item físico guardado no galpão, não da conta ML que o vende — se o mesmo
SKU for vendido por 2 lojas, o endereço continua sendo 1 só. Quem anuncia o quê
mora na `sku_lojas` (1 linha por par SKU↔loja), e é de lá que saem as gavetas
da tela.

Mesmo estilo enxuto de services/db.py: sem pool/ORM, conexão nova por chamada.
"""
from services.db import _get_connection


async def get_addresses() -> dict[str, str]:
    """Retorna {sku: 'Corredor - Estante - Prateleira'}."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch("SELECT sku, corredor, estante, prateleira FROM enderecos")
        result = {}
        for r in rows:
            partes = [r["corredor"], r["estante"], r["prateleira"]]
            result[r["sku"]] = " - ".join(p for p in partes if p)
        return result
    finally:
        await conn.close()


async def get_addresses_full(busca: str = "") -> list[dict]:
    """Retorna [{"sku","corredor","estante","prateleira"}] — pros campos editáveis
    da tela de cadastro. `busca`, se informado, restringe aos SKUs que contêm o
    texto (case-insensitive)."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            """SELECT sku, corredor, estante, prateleira FROM enderecos
               WHERE ($1 = '' OR sku ILIKE '%' || $1 || '%')
               ORDER BY sku""",
            busca or "",
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def ensure_addresses_for_skus(skus: list[str]) -> int:
    """Garante que todo SKU único apareça na tabela (endereço em branco pro
    usuário preencher). Retorna quantos SKUs novos foram adicionados."""
    unicos = [s for s in dict.fromkeys(skus) if s]
    if not unicos:
        return 0
    conn = await _get_connection()
    try:
        existentes = await conn.fetch(
            "SELECT sku FROM enderecos WHERE sku = ANY($1::text[])", unicos,
        )
        existentes_set = {r["sku"] for r in existentes}
        novos = [s for s in unicos if s not in existentes_set]
        if not novos:
            return 0
        await conn.executemany(
            "INSERT INTO enderecos (sku) VALUES ($1) ON CONFLICT (sku) DO NOTHING",
            [(s,) for s in novos],
        )
        return len(novos)
    finally:
        await conn.close()


async def set_address_for_sku(sku: str, corredor: str, estante: str, prateleira: str) -> bool:
    """Grava o endereço de um SKU (upsert). Retorna True se criou uma linha
    nova, False se atualizou uma existente."""
    conn = await _get_connection()
    try:
        existente = await conn.fetchrow("SELECT sku FROM enderecos WHERE sku = $1", sku)
        await conn.execute(
            """INSERT INTO enderecos (sku, corredor, estante, prateleira, atualizado_em)
               VALUES ($1, $2, $3, $4, now())
               ON CONFLICT (sku) DO UPDATE SET
                   corredor = EXCLUDED.corredor, estante = EXCLUDED.estante,
                   prateleira = EXCLUDED.prateleira, atualizado_em = now()""",
            sku, corredor, estante, prateleira,
        )
        return existente is None
    finally:
        await conn.close()


async def get_vinculos() -> dict[str, dict[str, bool]]:
    """Quais SKUs cada loja anuncia: {loja_user_id: {sku: ativo}}.

    É o que monta as gavetas da tela de Endereçamento. Substitui o "adivinhar
    a loja pelo prefixo do nome do SKU", que errava assim que uma conta passou
    a vender SKU de outra linha (ver `sku_lojas` em services/db.py)."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch("SELECT sku, loja_user_id, ativo FROM sku_lojas")
        out: dict[str, dict[str, bool]] = {}
        for r in rows:
            out.setdefault(r["loja_user_id"], {})[r["sku"]] = r["ativo"]
        return out
    finally:
        await conn.close()


async def set_vinculos_da_loja(loja_user_id: str, skus: list[str]) -> list[str]:
    """Grava o catálogo atual de uma loja: insere os pares novos, apaga os que
    saíram do catálogo e **nunca escreve na coluna `ativo` de par que já
    existe** — o interruptor é decisão do usuário, não do Mercado Livre.

    Os endereços dos `skus` já precisam existir (a FK exige) — chame
    `ensure_addresses_for_skus` antes. Retorna os SKUs que essa loja deixou de
    anunciar, que são os candidatos a virar endereço órfão."""
    unicos = {s for s in skus if s}
    conn = await _get_connection()
    try:
        async with conn.transaction():
            antes = {r["sku"] for r in await conn.fetch(
                "SELECT sku FROM sku_lojas WHERE loja_user_id = $1", loja_user_id)}
            sairam = list(antes - unicos)
            if sairam:
                await conn.execute(
                    "DELETE FROM sku_lojas WHERE loja_user_id = $1 AND sku = ANY($2::text[])",
                    loja_user_id, sairam)
            novos = [s for s in unicos if s not in antes]
            if novos:
                # DO NOTHING em vez de DO UPDATE: garante estruturalmente que
                # reconciliar nunca religa um SKU que o usuário desligou.
                await conn.executemany(
                    """INSERT INTO sku_lojas (sku, loja_user_id) VALUES ($1, $2)
                       ON CONFLICT (sku, loja_user_id) DO NOTHING""",
                    [(s, loja_user_id) for s in novos])
        return sairam
    finally:
        await conn.close()


async def set_vinculo_ativo(sku: str, loja_user_id: str, ativo: bool) -> None:
    """Liga/desliga um SKU numa loja só. Não toca no endereço nem nas outras
    lojas — a chave da tabela é o par (sku, loja)."""
    conn = await _get_connection()
    try:
        await conn.execute(
            """UPDATE sku_lojas SET ativo = $3, atualizado_em = now()
                WHERE sku = $1 AND loja_user_id = $2""",
            sku, loja_user_id, ativo)
    finally:
        await conn.close()


async def remover_orfaos(skus: list[str]) -> int:
    """Apaga os endereços de `skus` que nenhuma loja anuncia mais. Um SKU que
    ainda esteja vinculado a qualquer loja sobrevive — é o que impede uma loja
    de apagar o endereço de um SKU que a vizinha vende."""
    if not skus:
        return 0
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            """DELETE FROM enderecos e
                WHERE e.sku = ANY($1::text[])
                  AND NOT EXISTS (SELECT 1 FROM sku_lojas v WHERE v.sku = e.sku)
             RETURNING e.sku""",
            list({s for s in skus if s}))
        return len(rows)
    finally:
        await conn.close()


_SEM_VINCULO = """
    FROM enderecos e
   WHERE NOT EXISTS (SELECT 1 FROM sku_lojas v WHERE v.sku = e.sku)
"""


async def enderecos_sem_vinculo() -> list[str]:
    """SKUs que têm endereço mas nenhuma loja anuncia — nem ligado, nem
    desligado à mão. São os que entraram pelo sync de Produtos ou pelo "Novo
    SKU" da tela e nunca foram confirmados por um catálogo do Mercado Livre.

    Cuidado ao usar como base pra apagar: loja que ainda não teve o "Vincular
    SKUs" clicado não tem vínculo nenhum, então TODO SKU dela cai aqui."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch("SELECT e.sku " + _SEM_VINCULO + " ORDER BY e.sku")
        return [r["sku"] for r in rows]
    finally:
        await conn.close()


async def remover_enderecos_sem_vinculo() -> int:
    """Apaga de uma vez os endereços sem vínculo (ver a ressalva acima)."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch("DELETE " + _SEM_VINCULO + " RETURNING e.sku")
        return len(rows)
    finally:
        await conn.close()


async def bulk_upsert(linhas: list[dict]) -> None:
    """Migração pontual: grava várias linhas numa conexão só (evita abrir 1
    conexão por linha, lento demais no Cloud SQL Connector pra centenas de
    linhas). Cada item de `linhas` é {"sku","corredor","estante","prateleira"}."""
    if not linhas:
        return
    conn = await _get_connection()
    try:
        await conn.executemany(
            """INSERT INTO enderecos (sku, corredor, estante, prateleira, atualizado_em)
               VALUES ($1, $2, $3, $4, now())
               ON CONFLICT (sku) DO UPDATE SET
                   corredor = EXCLUDED.corredor, estante = EXCLUDED.estante,
                   prateleira = EXCLUDED.prateleira, atualizado_em = now()""",
            [(e["sku"], e["corredor"], e["estante"], e["prateleira"]) for e in linhas],
        )
    finally:
        await conn.close()


async def remove_mlb_addresses() -> int:
    """Remove os endereços cujo SKU é um MLB de fallback (anúncio sem SKU)."""
    conn = await _get_connection()
    try:
        rows = await conn.fetch(r"DELETE FROM enderecos WHERE sku ~ '^MLB\d+$' RETURNING sku")
        return len(rows)
    finally:
        await conn.close()
