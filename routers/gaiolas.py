"""Gaiolas: bipagem de pacotes embalados e coleta por gaiola.

A coleta exige uma guia de retirada (motorista/CPF/placa) por segurança
jurídica — impressa numa impressora comum (Wi-Fi, não térmica) via QZ Tray,
e só marca os pacotes como Enviado depois que a impressão confirma sucesso
(mesmo padrão "só confirma depois de imprimir" já usado na separação/
embalagem). O registro da guia fica permanente no Postgres (services/db.py).
"""
import html
import json
from datetime import datetime
from fastapi import APIRouter, Request, Form, Depends
from pydantic import BaseModel
from templates_engine import templates
from services import vendas_db
from services import db as db_service
from services.session_auth import require_login
from services.qz_signing import QZ_CERT
from services.meli_service import _BR_TZ
from config.settings import get_settings
from routers.print_labels import QZ_TRAY_CDN

router = APIRouter(prefix="/gaiolas", tags=["gaiolas"], dependencies=[Depends(require_login)])


class RetiradaIn(BaseModel):
    motorista_nome: str = ""
    motorista_cpf: str = ""
    placa: str = ""
    transportadora: str = ""
    copias: int = 1
    motorista_interno: bool = False
    romaneio_id: str = ""   # preenchido pelo /preparar, devolvido pelo cliente no /confirmar


def _dados_motorista(body: RetiradaIn) -> tuple[str, str, str, str]:
    """Retirada por funcionário interno não precisa de motorista/CPF/placa/
    transportadora — esses são os valores gravados/impressos nesse caso,
    ignorando o que vier no corpo (mesmo se o cliente mandar algo por engano)."""
    if body.motorista_interno:
        return "Funcionário interno", "", "", ""
    return (body.motorista_nome.strip(), body.motorista_cpf.strip(),
            body.placa.strip(), body.transportadora.strip())


async def _grid_context(request: Request, mensagem: str = "", erro: bool = False) -> dict:
    estado = await vendas_db.get_gaiolas_estado()
    return {"request": request, "estado": estado, "mensagem": mensagem, "erro": erro}


# Layout do Termo de Retirada (modelo aprovado pelo jurídico, 2026-07-31): a
# relação de encomendas é 1 tabela com N grupos de (Nº, ID da venda) lado a
# lado — GRUPOS_POR_LINHA grupos por linha. A 1ª folha carrega o cabeçalho +
# dados do motorista, então cabe menos linhas que as folhas de continuação
# (só a lista). Nº nunca reinicia entre folhas. Declaração/assinatura sempre
# na ÚLTIMA folha, isolada (nunca dividida no meio por causa de quebra de
# página). Constantes abaixo são estimativa pra A4 — ainda sem teste em
# impressora real (mesma ressalva já registrada pro `type:'pixel'` do QZ Tray).
_GRUPOS_POR_LINHA = 3
_LINHAS_PAGINA_1 = 70     # 70 linhas x 3 grupos = 210 itens (com cabeçalho+motorista)
_LINHAS_CONTINUACAO = 95  # 95 linhas x 3 grupos = 285 itens (folha enxuta)

_CSS_TERMO = """
  * { box-sizing: border-box; }
  body { font-family: Arial, Helvetica, sans-serif; color: #111; margin: 0; font-size: 9px; }
  .pagina { position: relative; padding: 12px 16px; page-break-after: always; }
  .pagina:last-child { page-break-after: auto; }
  .romaneio-tag { position: absolute; top: 6px; left: 10px; font-size: 9px; font-weight: bold; color: #333; }
  .pagina-tag { position: absolute; top: 16px; left: 10px; font-size: 8px; color: #666; }
  .cabecalho { margin-top: 15px; }
  h1 { font-size: 14px; margin: 0 0 1px; }
  .sub { font-size: 9px; color: #555; margin-bottom: 6px; }
  h2 { font-size: 9.5px; margin: 6px 0 3px; text-transform: uppercase; letter-spacing: .03em; }
  table { width: 100%; border-collapse: collapse; font-size: 9px; margin-bottom: 4px; line-height: 1.1; }
  th, td { border: 1px solid #999; padding: 1px 4px; text-align: left; }
  th { background: #eee; }
  .motorista-table td { padding: 2px 5px; }
  .motorista-table td:first-child { font-weight: bold; width: 110px; background: #f7f7f7; }
  .itens-table th.par { border-left: 2px solid #444; }
  .itens-table td.par { border-left: 2px solid #444; }
  .total { font-size: 10px; margin: 6px 0; }
  .declaracao { font-size: 10px; line-height: 1.4; margin: 10px 0 32px; text-align: justify; }
  .assinaturas { display: flex; gap: 60px; margin-top: 24px; }
  .assinatura .linha { border-top: 1px solid #333; width: 260px; padding-top: 4px; font-size: 10px; }
"""


def _linha_romaneio_pagina(romaneio_id: str, pagina: int, total_paginas: int) -> str:
    return (f'<div class="romaneio-tag">Romaneio Nº {html.escape(romaneio_id)}</div>'
            f'<div class="pagina-tag">Página {pagina} de {total_paginas}</div>')


def _tabela_itens_html(ids_pagina: list[str], numero_inicial: int) -> str:
    linhas_html = []
    for i in range(0, len(ids_pagina), _GRUPOS_POR_LINHA):
        grupo = ids_pagina[i:i + _GRUPOS_POR_LINHA]
        celulas = []
        for g, id_exped in enumerate(grupo):
            numero = numero_inicial + i + g
            classe = ' class="par"' if g > 0 else ""
            celulas.append(f'<td{classe}>{numero}</td><td>{html.escape(id_exped)}</td>')
        # completa a última linha (grupo incompleto) com células vazias, mantendo a grade
        for g in range(len(grupo), _GRUPOS_POR_LINHA):
            classe = ' class="par"' if g > 0 else ""
            celulas.append(f'<td{classe}></td><td></td>')
        linhas_html.append(f"<tr>{''.join(celulas)}</tr>")
    cabecalho = "".join(
        f'<th{" class=\"par\"" if g > 0 else ""}>Nº</th><th{" class=\"par\"" if g > 0 else ""}>ID da Venda</th>'
        for g in range(_GRUPOS_POR_LINHA)
    )
    return (f'<table class="itens-table"><thead><tr>{cabecalho}</tr></thead>'
            f'<tbody>{"".join(linhas_html)}</tbody></table>')


def _montar_guia_retirada_html(romaneio_id: str, gaiola: str, motorista_nome: str, motorista_cpf: str,
                                placa: str, transportadora: str, copias: int, pacotes: list[dict],
                                motorista_interno: bool = False) -> str:
    """Termo de Retirada de Encomendas — modelo aprovado pelo jurídico (2026-07-31),
    documento de página inteira (não ZPL), impresso via QZ Tray na impressora
    comum configurada em PRINTER_GUIA_NAME. Pagina automaticamente quando a
    gaiola tem mais pacotes do que cabe numa folha — Declaração/assinatura
    sempre isoladas na última folha."""
    agora = datetime.now(_BR_TZ).strftime("%d/%m/%Y %H:%M")
    ids = [p["id_exped"] for p in pacotes]

    # Fatia os IDs em folhas: a 1ª (menor, tem cabeçalho+motorista) e as demais (maiores).
    folhas: list[list[str]] = []
    cap_pagina1 = _LINHAS_PAGINA_1 * _GRUPOS_POR_LINHA
    folhas.append(ids[:cap_pagina1])
    resto = ids[cap_pagina1:]
    cap_continuacao = _LINHAS_CONTINUACAO * _GRUPOS_POR_LINHA
    for i in range(0, len(resto), cap_continuacao):
        folhas.append(resto[i:i + cap_continuacao])
    total_paginas = len(folhas) + 1   # +1 = folha final da Declaração/assinatura

    if motorista_interno:
        meta_html = '<tr><td>Retirada</td><td colspan="3">Funcionário interno (sem motorista externo)</td></tr>'
    else:
        transportadora_html = html.escape(transportadora) if transportadora else "—"
        meta_html = (
            f'<tr><td>Nome</td><td colspan="3">{html.escape(motorista_nome)}</td></tr>'
            f'<tr><td>CPF</td><td>{html.escape(motorista_cpf)}</td>'
            f'<td>Placa do veículo</td><td>{html.escape(placa)}</td></tr>'
            f'<tr><td>Transportadora</td><td>{transportadora_html}</td>'
            f'<td>Data e hora</td><td>{agora}</td></tr>'
        )
    meta_html += f'<tr><td>Identificação da gaiola</td><td colspan="3">{html.escape(gaiola)}</td></tr>'

    paginas_html = []
    numero_atual = 1
    for idx, ids_pagina in enumerate(folhas):
        pagina_num = idx + 1
        tag = _linha_romaneio_pagina(romaneio_id, pagina_num, total_paginas)
        tabela = _tabela_itens_html(ids_pagina, numero_atual)
        numero_atual += len(ids_pagina)
        if idx == 0:
            corpo = f"""
    <div class="cabecalho">
      <h1>Termo de Retirada de Encomendas</h1>
      <div class="sub">Comprovante de coleta de encomendas pela transportadora</div>
    </div>
    <h2>Dados do motorista e da coleta</h2>
    <table class="motorista-table"><tbody>{meta_html}</tbody></table>
    <h2>Relação de encomendas retiradas</h2>
    {tabela}"""
        else:
            corpo = f'<div class="cabecalho">{tabela}</div>'
        paginas_html.append(f'<div class="pagina">{tag}{corpo}</div>')

    if motorista_interno:
        assinatura_motorista_html = ""
    else:
        assinatura_motorista_html = """
      <div class="assinatura">
        <div class="linha">Assinatura do motorista<br>Nome e CPF</div>
      </div>"""
    tag_final = _linha_romaneio_pagina(romaneio_id, total_paginas, total_paginas)
    paginas_html.append(f"""<div class="pagina">{tag_final}
    <div class="cabecalho">
      <div class="total">Total de itens retirados: <b>{len(ids)}</b></div>
      <h2>Declaração</h2>
      <div class="declaracao">Declaro, para os devidos fins, que recebi e conferi a totalidade das
        encomendas relacionadas neste documento, identificadas pelos respectivos IDs, assumindo a
        responsabilidade pela guarda, transporte e entrega dos referidos itens a partir deste momento.</div>
      <div class="assinaturas">{assinatura_motorista_html}
        <div class="assinatura">
          <div class="linha">Responsável pela expedição (empresa)</div>
        </div>
      </div>
    </div>
  </div>""")

    return f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<title>Termo de Retirada — {html.escape(romaneio_id)}</title>
<style>{_CSS_TERMO}</style></head><body>
{"".join(paginas_html)}
</body></html>"""


@router.get("")
async def gaiolas_page(request: Request):
    settings = get_settings()
    return templates.TemplateResponse("gaiolas.html", {
        "request": request,
        "cdn": QZ_TRAY_CDN,
        "cert_js": json.dumps(QZ_CERT),
        "printer_guia_js": json.dumps(settings.printer_guia_name),
    })


@router.get("/estado")
async def gaiolas_estado(request: Request):
    return templates.TemplateResponse("_gaiolas_grid.html", await _grid_context(request))


@router.post("/bipar")
async def bipar(request: Request, codigo: str = Form(...)):
    partes = codigo.strip().split("|", 1)
    if len(partes) < 2:
        ctx = await _grid_context(request, mensagem='Formato inválido. Use "N|ID" (ex.: 2|PLG260726001).', erro=True)
        return templates.TemplateResponse("_gaiolas_grid.html", ctx)

    prefixo, id_exped = partes[0].strip(), partes[1].strip()
    gaiola = f"Gaiola {prefixo}" if prefixo.isdigit() else prefixo

    resultado = await vendas_db.mover_para_gaiola(id_exped, gaiola)
    if resultado["ok"]:
        mensagem = f'{id_exped} → {gaiola} ({resultado["n_sku"]} item(ns))'
        ctx = await _grid_context(request, mensagem=mensagem, erro=False)
    else:
        ctx = await _grid_context(request, mensagem=resultado["msg"], erro=True)
    return templates.TemplateResponse("_gaiolas_grid.html", ctx)


@router.post("/{gaiola}/retirada/preparar")
async def preparar_retirada(gaiola: str, body: RetiradaIn):
    """Monta a guia (HTML pra imprimir) com o snapshot atual da gaiola. NÃO
    marca nada como Enviado nem grava a guia ainda — só depois que o QZ Tray
    confirma a impressão é que /retirada/confirmar faz isso de verdade."""
    nome, cpf, placa, transportadora = _dados_motorista(body)
    if not body.motorista_interno and (not nome or not cpf or not placa):
        return {"ok": False, "msg": "Preencha nome do motorista, CPF e placa (ou marque \"funcionário interno\")."}
    pacotes = await vendas_db.get_pacotes_da_gaiola(gaiola)
    if not pacotes:
        return {"ok": False, "msg": f'Gaiola "{gaiola}" está vazia — nada pra retirar.'}
    romaneio_id = await db_service.get_next_romaneio_id()
    guia_html = _montar_guia_retirada_html(
        romaneio_id, gaiola, nome, cpf, placa, transportadora, body.copias, pacotes, body.motorista_interno)
    return {"ok": True, "html": guia_html, "n_pacotes": len(pacotes), "romaneio_id": romaneio_id}


@router.post("/{gaiola}/retirada/confirmar")
async def confirmar_retirada(request: Request, gaiola: str, body: RetiradaIn):
    """Chamado só depois que o QZ Tray confirma a impressão da guia. Registra
    a guia (permanente, nunca editada depois) e SÓ ENTÃO marca os pacotes como
    Enviado — re-busca a gaiola na hora (não confia na lista que o cliente
    tinha no /preparar) pra garantir que o snapshot gravado bate exatamente
    com o que foi de fato coletado."""
    pacotes = await vendas_db.get_pacotes_da_gaiola(gaiola)
    if not pacotes:
        ctx = await _grid_context(request, mensagem=f'Gaiola "{gaiola}" já estava vazia.', erro=True)
        return templates.TemplateResponse("_gaiolas_grid.html", ctx)

    nome, cpf, placa, transportadora = _dados_motorista(body)
    resultado = await vendas_db.coletar_gaiola(gaiola)
    await db_service.registrar_guia_retirada(
        body.romaneio_id, gaiola, nome, cpf, placa, transportadora, body.copias,
        [{"venda": p["venda"], "id_exped": p["id_exped"], "empresa": p["empresa"], "n_sku": p["n_sku"]}
         for p in pacotes],
    )
    mensagem = (f'{gaiola}: {resultado["n_pacotes"]} pacote(s) coletado(s). '
                f'Romaneio {body.romaneio_id} registrado.')
    ctx = await _grid_context(request, mensagem=mensagem, erro=False)
    return templates.TemplateResponse("_gaiolas_grid.html", ctx)
