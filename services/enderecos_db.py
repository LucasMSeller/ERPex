"""Endereçamento (SKU -> corredor/estante/prateleira) — Postgres, fase 1 da
migração pra fora do Sheets. SKU é chave única global (sem loja): o endereço é
do item físico guardado no galpão, não da conta ML que o vende — se o mesmo
SKU um dia for vendido por 2 lojas, o endereço continua sendo 1 só.

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


async def sync_skus(skus_atuais: list[str], sku_prefixo: str | None = None) -> dict:
    """Reconcilia: insere os SKUs novos (endereço em branco), mantém os já
    existentes (não toca em corredor/estante/prateleira), e remove os que não
    aparecem mais em `skus_atuais`.

    Sem `sku_prefixo` (reconciliação GLOBAL, união de TODAS as lojas): seguro
    chamar assim, já que qualquer SKU realmente removido de qualquer loja some
    da união. Com `sku_prefixo` (reconciliação de 1 loja com prefixo
    configurado): a remoção só considera SKUs que começam com esse prefixo —
    nunca mexe no espaço de SKUs de outra loja, já que a tabela não tem coluna
    de loja."""
    unicos = list(dict.fromkeys(s for s in skus_atuais if s))
    atuais_set = set(unicos)
    conn = await _get_connection()
    try:
        existentes = await conn.fetch("SELECT sku FROM enderecos")
        existentes_set = {r["sku"] for r in existentes}

        novos = [s for s in unicos if s not in existentes_set]
        candidatos_remocao = (
            {s for s in existentes_set if s.startswith(sku_prefixo)} if sku_prefixo else existentes_set
        )
        removidos = list(candidatos_remocao - atuais_set)

        if novos:
            await conn.executemany(
                "INSERT INTO enderecos (sku) VALUES ($1) ON CONFLICT (sku) DO NOTHING",
                [(s,) for s in novos],
            )
        if removidos:
            await conn.execute("DELETE FROM enderecos WHERE sku = ANY($1::text[])", removidos)

        return {"novos": len(novos), "removidos": len(removidos), "total": len(unicos)}
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


async def get_vinculo_status(lojas: list[str]) -> dict[str, dict]:
    """Estado atual do botão "Vincular SKUs" pra cada loja (loja sem linha
    ainda = nunca rodou, tratado como "ocioso" pelo chamador)."""
    if not lojas:
        return {}
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            "SELECT * FROM enderecos_vinculo_status WHERE loja = ANY($1::text[])", lojas,
        )
        return {r["loja"]: dict(r) for r in rows}
    finally:
        await conn.close()


async def set_vinculo_status(loja: str, status: str, novos: int | None = None,
                              removidos: int | None = None, total: int | None = None,
                              erro: str | None = None) -> None:
    conn = await _get_connection()
    try:
        await conn.execute(
            """INSERT INTO enderecos_vinculo_status (loja, status, novos, removidos, total, erro, atualizado_em)
               VALUES ($1, $2, $3, $4, $5, $6, now())
               ON CONFLICT (loja) DO UPDATE SET
                   status = EXCLUDED.status, novos = EXCLUDED.novos, removidos = EXCLUDED.removidos,
                   total = EXCLUDED.total, erro = EXCLUDED.erro, atualizado_em = now()""",
            loja, status, novos, removidos, total, erro,
        )
    finally:
        await conn.close()
