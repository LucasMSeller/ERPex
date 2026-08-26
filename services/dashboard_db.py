"""Agregações do dashboard do Gerente — leitura pura, nada aqui escreve.

Tudo é contado e somado NO BANCO. Isso é deliberado: em 10/08/2026 o Gerente
chegava a 8,8s por carregar todos os pedidos e filtrar em Python (ver
`vendas_db._fetch_pedidos`), e um painel de números seria o lugar mais fácil
de repetir o mesmo erro. Uma conexão só serve todas as consultas — o projeto
não usa pool, então abrir uma por métrica custaria mais que as próprias queries.

Sobre datas: `criado_em` é TIMESTAMPTZ, mas `data_limite`, `impresso_em` e
`enviado_em` são TEXT herdados do Sheets ('DD/MM/AAAA' e 'DD/MM/AAAA HH:MM').
Cada um só é convertido quando casa com o formato esperado — linha meia
preenchida existe de verdade e não pode derrubar a tela. O fuso usa offset fixo
de -3h, mesma escolha do `_BR_TZ` em meli_service (não depende de tzdata).
"""
from datetime import date
from services.db import _get_connection

# Brasil sem horário de verão desde 2019 — o mesmo -3h fixo usado no resto do app.
_LOCAL = "(v.criado_em - interval '3 hours')"

# Os 5 parâmetros são sempre os mesmos e nessa ordem:
# de, ate, lojas, origens, incluir_cancelados.
_FILTRO = f"""
        ($1::date IS NULL OR {_LOCAL}::date >= $1)
    AND ($2::date IS NULL OR {_LOCAL}::date <= $2)
    AND ($3::text[] IS NULL OR v.empresa = ANY($3))
    AND ($4::text[] IS NULL OR v.origem = ANY($4))
    AND ($5::boolean OR lower(v.status) <> 'cancelado')
"""

# Igual, menos a origem: o gráfico "Expedição × Full" precisa enxergar os dois
# lados mesmo quando o filtro de origem está restringindo o resto da tela.
_FILTRO_SEM_ORIGEM = f"""
        ($1::date IS NULL OR {_LOCAL}::date >= $1)
    AND ($2::date IS NULL OR {_LOCAL}::date <= $2)
    AND ($3::text[] IS NULL OR v.empresa = ANY($3))
"""

# Sem recorte de período — só loja e origem ($1 e $2). "Na fila agora" e "atrasados"
# são estados do PRESENTE, não do intervalo escolhido: um pedido que entrou há três
# semanas e continua parado é o que MAIS precisa aparecer, e filtrar por data de
# criação fazia justamente ele sumir do painel (foi assim que USE250726001, de 25/07,
# ficou invisível numa janela de 14 dias enquanto seguia embalado e vencido).
_FILTRO_ESTADO = """
        ($1::text[] IS NULL OR v.empresa = ANY($1))
    AND ($2::text[] IS NULL OR v.origem = ANY($2))
"""

_FMT_DATA = r"^\d{2}/\d{2}/\d{4}$"
_FMT_DATA_HORA = r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$"

_FILA = ("separando", "separado", "embalado")


async def carregar(de: date | None, ate: date | None,
                   lojas: list[str] | None, origens: list[str] | None,
                   incluir_cancelados: bool = False) -> dict:
    """Todos os números do painel numa ida só ao banco.

    `lojas`/`origens` vazios significam "sem restrição" — quem chama manda None,
    não lista vazia (uma lista vazia com `= ANY` não casaria com nada e a tela
    apareceria zerada em vez de completa).

    `incluir_cancelados` é False por padrão: cancelado não é venda realizada, e
    contá-lo no volume infla o número que mais se olha na tela."""
    args = (de, ate, lojas or None, origens or None, incluir_cancelados)
    conn = await _get_connection()
    try:
        por_status = await conn.fetch(
            f"SELECT v.status, count(*)::int AS n FROM vendas v WHERE {_FILTRO} GROUP BY v.status", *args)

        serie = await conn.fetch(
            f"""SELECT {_LOCAL}::date AS dia, v.empresa, count(*)::int AS n
                FROM vendas v WHERE {_FILTRO}
                GROUP BY 1, 2 ORDER BY 1""", *args)

        skus = await conn.fetch(
            f"""SELECT vi.sku, sum(vi.qtd)::int AS n
                FROM venda_itens vi JOIN vendas v ON v.venda = vi.venda
                WHERE {_FILTRO}
                GROUP BY vi.sku ORDER BY n DESC, vi.sku LIMIT 8""", *args)

        origem = await conn.fetch(
            f"""SELECT v.origem, count(*)::int AS n
                FROM vendas v
                WHERE {_FILTRO_SEM_ORIGEM}
                  AND ($4::boolean OR lower(v.status) <> 'cancelado')
                GROUP BY v.origem""",
            de, ate, lojas or None, incluir_cancelados)

        # Sem recorte de período de propósito — ver _FILTRO_ESTADO.
        fila_agora = await conn.fetchval(
            f"""SELECT count(*)::int FROM vendas v
                WHERE {_FILTRO_ESTADO} AND lower(v.status) IN ('separando', 'separado', 'embalado')""",
            lojas or None, origens or None)

        atrasados = await conn.fetch(
            f"""SELECT v.venda, v.id_exped, v.empresa, v.status, v.data_limite,
                       (current_date - to_date(v.data_limite, 'DD/MM/YYYY'))::int AS dias,
                       (SELECT count(*)::int FROM venda_itens i WHERE i.venda = v.venda) AS n_itens
                FROM vendas v
                WHERE {_FILTRO_ESTADO}
                  AND lower(v.status) NOT IN ('enviado', 'cancelado')
                  AND v.data_limite ~ '{_FMT_DATA}'
                  AND to_date(v.data_limite, 'DD/MM/YYYY') < current_date
                ORDER BY to_date(v.data_limite, 'DD/MM/YYYY') LIMIT 50""",
            lojas or None, origens or None)

        # Tempo de ciclo: só entra no cálculo o pedido que tem o carimbo, e a
        # contagem de quantos entraram volta junto — sem isso a mediana esconde
        # que metade da base não tinha o dado (`enviado_em` só existe desde 06/08).
        ciclo = await conn.fetchrow(
            f"""WITH base AS (
                    SELECT v.criado_em,
                           CASE WHEN v.impresso_em ~ '{_FMT_DATA_HORA}'
                                THEN to_timestamp(v.impresso_em, 'DD/MM/YYYY HH24:MI') END AS t_imp,
                           CASE WHEN v.enviado_em ~ '{_FMT_DATA_HORA}'
                                THEN to_timestamp(v.enviado_em, 'DD/MM/YYYY HH24:MI') END AS t_env
                    FROM vendas v WHERE {_FILTRO}
                )
                SELECT count(*)::int AS total,
                       count(t_imp)::int AS com_impressao,
                       count(t_env)::int AS com_envio,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (t_imp - criado_em)) / 3600.0)
                           FILTER (WHERE t_imp IS NOT NULL AND t_imp >= criado_em) AS h_ate_impressao,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (t_env - t_imp)) / 3600.0)
                           FILTER (WHERE t_env IS NOT NULL AND t_imp IS NOT NULL AND t_env >= t_imp)
                           AS h_impressao_ate_envio,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (t_env - criado_em)) / 3600.0)
                           FILTER (WHERE t_env IS NOT NULL AND t_env >= criado_em) AS h_total
                FROM base""", *args)

        devolucoes = await conn.fetchval(
            """SELECT count(*)::int FROM devolucoes d
               WHERE ($1::date IS NULL OR (d.criado_em - interval '3 hours')::date >= $1)
                 AND ($2::date IS NULL OR (d.criado_em - interval '3 hours')::date <= $2)
                 AND ($3::text[] IS NULL OR d.empresa = ANY($3))""",
            de, ate, lojas or None)
    finally:
        await conn.close()

    status = {r["status"]: r["n"] for r in por_status}
    total = sum(status.values())
    enviados = sum(n for s, n in status.items() if s.strip().lower() == "enviado")

    return {
        "total": total,
        "fila": fila_agora or 0,
        # Quanto da fila entrou dentro do período: se for menor que `fila`, existe
        # pedido parado de antes do intervalo — e é isso que o rodapé do KPI avisa.
        "fila_no_periodo": sum(n for s, n in status.items() if s.strip().lower() in _FILA),
        "enviados": enviados,
        "pct_enviados": round(enviados * 100 / total, 1) if total else 0.0,
        "devolucoes": devolucoes or 0,
        "pct_devolucoes": round((devolucoes or 0) * 100 / total, 1) if total else 0.0,
        "atrasados": [dict(r) for r in atrasados],
        "por_status": _ordenar_status(status),
        "serie": _montar_serie(serie),
        "skus": [dict(r) for r in skus],
        "origem": {r["origem"]: r["n"] for r in origem},
        "ciclo": _montar_ciclo(ciclo),
    }


def _ordenar_status(status: dict[str, int]) -> list[dict]:
    """Na ordem real do fluxo, não alfabética nem por volume — a leitura é
    "onde os pedidos estão parados", e isso só faz sentido em sequência."""
    ordem = ["Separando", "Separado", "Embalado", "Enviado", "Cancelado"]
    conhecidos = [{"nome": s, "n": status.get(s, 0)} for s in ordem if status.get(s)]
    extras = [{"nome": s, "n": n} for s, n in sorted(status.items()) if s not in ordem]
    return conhecidos + extras


def _montar_serie(rows) -> dict:
    """Vira {dias: [...], lojas: {loja: [n por dia]}} — com zero nos dias sem
    venda, senão a linha do gráfico "pula" o dia parado e distorce a tendência."""
    dias = sorted({r["dia"] for r in rows})
    lojas = sorted({r["empresa"] for r in rows if r["empresa"]})
    idx = {d: i for i, d in enumerate(dias)}
    series = {loja: [0] * len(dias) for loja in lojas}
    for r in rows:
        if r["empresa"]:
            series[r["empresa"]][idx[r["dia"]]] = r["n"]
    return {
        "dias": [d.strftime("%d/%m") for d in dias],
        "lojas": [{"nome": loja, "valores": series[loja]} for loja in lojas],
    }


def _montar_ciclo(row) -> dict:
    def horas(v):
        return round(float(v), 1) if v is not None else None
    return {
        "total": row["total"] or 0,
        "com_impressao": row["com_impressao"] or 0,
        "com_envio": row["com_envio"] or 0,
        "ate_impressao": horas(row["h_ate_impressao"]),
        "impressao_ate_envio": horas(row["h_impressao_ate_envio"]),
        "total_horas": horas(row["h_total"]),
    }
