"""Impressão via QZ Tray: etiqueta de separação (nossa) e etiqueta final (ML + nosso QR).

A página de impressão usa o QZ Tray (ponte web→impressora, agnóstica de marca) para
enviar o ZPL cru direto pra impressora local (Zebra ou Bematech em emulação ZPL).
"""
import re
import json
import logging
from datetime import date, datetime
from urllib.parse import quote
from fastapi import APIRouter, BackgroundTasks, Query, HTTPException, Depends
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel
from services.token_store import TokenStore
from services.meli_service import MeliService, build_qr_zpl, build_gaiola_zpl, build_separacao_zpl
from services import vendas_db
from services.qz_signing import QZ_CERT, sign as qz_sign
from services.session_auth import require_login_or_gerente, require_gerente

# Qualquer rota aqui exige pelo menos sessão operacional OU de gerente. A etiqueta
# do ML especificamente (zpl/baixar/etiqueta) é restrita SÓ ao gerente — o fluxo
# normal pra imprimir a etiqueta do ML é bipar na tela /embalagem.
router = APIRouter(prefix="/print", tags=["print"], dependencies=[Depends(require_login_or_gerente)])
logger = logging.getLogger(__name__)

QZ_TRAY_CDN = "https://cdn.jsdelivr.net/npm/qz-tray@2.2.4/qz-tray.js"


class VendaIn(BaseModel):
    venda: str


class VendasIn(BaseModel):
    vendas: list[str]


class PrepararSeparacaoIn(BaseModel):
    venda: str
    company: str


async def _meli_for(company: str) -> MeliService:
    store = await TokenStore().get_by_company_or_nickname(company)
    if not store:
        raise HTTPException(404, f"Loja '{company}' não conectada.")
    return MeliService(store, token_store=TokenStore())


def _parse_data_br(valor: str) -> date | None:
    """'DD/MM/AAAA' -> date. None se vazio/inválido."""
    try:
        d, m, a = (valor or "").strip().split("/")
        return date(int(a), int(m), int(d))
    except Exception:
        return None


async def _gatilho_nf(company: str, venda: str) -> None:
    """Dispara o "gatilho" de emissão de NF-e (best-effort, nunca levanta) — usado
    em BackgroundTasks, não deve atrasar nem quebrar a impressão que o chamou."""
    try:
        meli = await _meli_for(company)
        order, _shipping_id = await meli.resolve_order_and_shipping(venda)
        if not order:
            return
        resultado = await meli.ensure_invoice(str(order["id"]))
        if resultado["erro"]:
            logger.warning("Gatilho NF-e (venda %s): erro — %s", venda, resultado["erro"])
        else:
            logger.info("Gatilho NF-e (venda %s): status=%s (já existia=%s)",
                        venda, resultado["status"], resultado["ja_existia"])
    except Exception:
        logger.exception("Gatilho NF-e (venda %s): falha inesperada.", venda)


async def _montar_zpl(numero: str, company: str, exped: str) -> tuple[str, str, str]:
    """Retorna (zpl_combinado, aviso, exped). Sempre inclui o nosso QR; ML se disponível."""
    meli = await _meli_for(company)
    aviso = ""
    ml_zpl = ""
    try:
        order, shipping_id = await meli.resolve_order_and_shipping(numero)
    except Exception as e:
        order, shipping_id = None, ""
        aviso = f"Pedido não encontrado no ML: {str(e)[:120]}"
    if shipping_id:
        try:
            ml_zpl = await meli.get_label_zpl(shipping_id)
        except Exception as e:
            aviso = f"Etiqueta do ML indisponível (pedido já enviado?): {str(e)[:120]}"
    elif order is not None:
        aviso = "Pedido sem envio associado — imprimindo só o QR interno."

    if not exped:
        try:
            exped = await vendas_db.get_expedition_id(numero)
        except Exception:
            pass
    qr_zpl = build_qr_zpl(exped)
    combinado = (ml_zpl.rstrip() + "\n" + qr_zpl) if ml_zpl else qr_zpl
    return combinado, aviso, exped


async def _montar_zpl_embalagem(pedido: dict, company: str) -> tuple[str, str, str, str, bool]:
    """Monta os ZPLs da Embalagem: nosso QR e a etiqueta do ML, SEPARADOS (vão para
    2 impressoras diferentes — ver tela /embalagem). Retorna
    (zpl_pedido, zpl_ml, aviso, order_id, bloqueia).

    `bloqueia=True` significa que o pedido TEM envio no ML mas a etiqueta falhou ao
    buscar (ex.: ML ainda não gerou — condição de corrida logo que o envio libera pro
    dia) — nesse caso o chamador NÃO deve deixar embalar sem a etiqueta real. Quando
    não existe `shipping_id` (pedido sem envio associado no ML), `bloqueia=False`:
    esse é um cenário legítimo e diferente (nunca vai ter etiqueta do ML mesmo).

    O gatilho de NF-e roda ANTES de buscar a etiqueta, de propósito: o ML só libera
    a etiqueta depois que a nota está vinculada ao envio (senão o shipment fica em
    "invoice_pending" e a busca vem 400/NOT_PRINTABLE_STATUS) — tentar emitir a nota
    só DEPOIS de já ter falhado a etiqueta (como era antes) nunca dava chance da nota
    sair, porque o bloqueio já tinha retornado antes de chegar nesse ponto.
    """
    exped_id = pedido.get("id_exped", "")
    zpl_pedido = build_gaiola_zpl(exped_id)
    zpl_ml = ""
    aviso = ""
    order_id = ""
    bloqueia = False
    meli = await _meli_for(company)
    numero = pedido.get("venda", "")
    try:
        order, shipping_id = await meli.resolve_order_and_shipping(numero)
    except Exception as e:
        order, shipping_id = None, ""
        aviso = f"Pedido não encontrado no ML: {str(e)[:120]}"
        bloqueia = True
    if order is not None:
        order_id = str(order.get("id", ""))
    if order_id:
        try:
            resultado = await meli.ensure_invoice(order_id)
            if resultado["erro"]:
                aviso = (aviso + " " if aviso else "") + f"NF-e: erro ao emitir ({resultado['erro'][:120]})."
            elif resultado["ja_existia"]:
                aviso = (aviso + " " if aviso else "") + "NF-e já emitida anteriormente."
            else:
                aviso = (aviso + " " if aviso else "") + "NF-e emitida."
        except Exception as e:
            logger.warning("Gatilho NF-e (venda %s): falha inesperada — %s", pedido.get("venda"), e)
    if shipping_id:
        try:
            zpl_ml = await meli.get_label_zpl(shipping_id)
        except Exception as e:
            aviso = (aviso + " " if aviso else "") + f"Etiqueta do ML indisponível ainda (tente bipar de novo em alguns segundos): {str(e)[:120]}"
            bloqueia = True
    elif order is not None:
        aviso = (aviso + " " if aviso else "") + "Pedido sem envio associado — só a etiqueta do nosso ID será impressa."
    return zpl_pedido, zpl_ml, aviso, order_id, bloqueia


def _nome_arquivo(exped: str, numero: str) -> str:
    base = (exped or numero or "etiqueta").strip() or "etiqueta"
    return re.sub(r"[^A-Za-z0-9._-]", "_", base) + ".zpl"


@router.get("/zpl/{numero}", dependencies=[Depends(require_gerente)])
async def print_zpl(numero: str, company: str = Query(...), exped: str = Query("")):
    """Baixa o ZPL (ML + nosso QR) como arquivo, nomeado com o ID de expedição.
    Só o Gerente acessa — o fluxo normal é bipar na tela /embalagem."""
    zpl, _aviso, exped = await _montar_zpl(numero, company, exped)
    return Response(
        content=zpl, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{_nome_arquivo(exped, numero)}"',
                 "Cache-Control": "no-store"})


@router.get("/baixar/{numero}", dependencies=[Depends(require_gerente)])
async def baixar_page(numero: str, company: str = Query(...), exped: str = Query("")):
    """Paginazinha que baixa o .zpl (via blob) e FECHA a aba sozinha (igual o imprimir).
    Só o Gerente acessa — o fluxo normal é bipar na tela /embalagem."""
    zpl, _aviso, exped = await _montar_zpl(numero, company, exped)
    html = _BAIXAR_PAGE.format(zpl_js=json.dumps(zpl), fname=_nome_arquivo(exped, numero))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/etiqueta/{numero}", dependencies=[Depends(require_gerente)])
async def print_page(numero: str, company: str = Query(...), exped: str = Query("")):
    """Página que imprime na hora (via QZ Tray) a etiqueta do ML + o nosso QR.

    Fallback manual só do Gerente (a impressão automática de verdade acontece na
    tela /embalagem, que dispara as 2 impressoras junto com a validação por bipagem).
    """
    zpl, aviso, exped = await _montar_zpl(numero, company, exped)
    querystring = f"?company={quote(company)}&exped={quote(exped)}"
    extra = f'<a class="btn sec" href="/print/baixar/{numero}{querystring}">Baixar .zpl</a>'
    html = _qz_print_page(
        titulo=f"Imprimir etiqueta — {numero}", zpl=zpl, aviso=aviso, extra_html=extra,
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/separacao/zpl/{venda}")
async def print_separacao_zpl(venda: str, company: str = Query(...)):
    """Baixa o .zpl da etiqueta de separação (sem imprimir nem confirmar nada) —
    padrão por enquanto, até as 2 impressoras reais estarem configuradas no galpão."""
    pedido = await vendas_db.get_pedido_by_venda(venda)
    if not pedido:
        raise HTTPException(404, f"Pedido '{venda}' não encontrado.")
    zpl = build_separacao_zpl(pedido)
    return Response(
        content=zpl, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{_nome_arquivo(pedido.get("id_exped", ""), venda)}"',
                 "Cache-Control": "no-store"})


@router.post("/separacao/preparar")
async def preparar_separacao(body: PrepararSeparacaoIn, background_tasks: BackgroundTasks):
    """Monta o ZPL da etiqueta de separação pra impressão INLINE (Mural imprime
    direto via QZ Tray na própria página, sem abrir aba/rota auxiliar — mesmo
    padrão de /embalagem/validar). Nunca levanta em erro esperado (pedido não
    encontrado/cancelado): devolve {"ok": False, "msg": ...} pro JS mostrar."""
    pedido = await vendas_db.get_pedido_by_venda(body.venda)
    if not pedido:
        return {"ok": False, "msg": f'Pedido "{body.venda}" não encontrado.'}
    if pedido["status"].strip().lower() == "cancelado":
        return {"ok": False, "msg": f'Pedido {body.venda} foi cancelado no Mercado Livre — não pode ser separado.'}

    # Gatilho de NF-e: só faz sentido tentar se a entrega for hoje — antes disso o
    # ML ainda não libera a emissão (mesma regra que libera a etiqueta do ML).
    data_limite = _parse_data_br(pedido.get("data_limite"))
    if data_limite is not None and data_limite <= date.today():
        background_tasks.add_task(_gatilho_nf, body.company, body.venda)

    zpl = build_separacao_zpl(pedido)
    return {"ok": True, "venda": body.venda, "zpl": zpl}


@router.post("/separacao/confirmar")
async def confirmar_separacao(body: VendaIn):
    """Chamado pelo JS só depois que o QZ Tray confirma a impressão da etiqueta
    de separação com sucesso. Grava Status="Separado" + o horário da impressão."""
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    n = await vendas_db.set_status_for_venda(body.venda, "Separado", impresso_em=agora)
    return {"ok": True, "linhas": n}


@router.post("/separacao-lote/preparar")
async def preparar_separacao_lote(body: VendasIn, background_tasks: BackgroundTasks):
    """Mesma ideia de /separacao/preparar, mas monta um job combinado (1 ZPL só,
    vários `^XA...^XZ` back-to-back) pra imprimir vários pedidos de uma vez, INLINE
    no Mural — sem abrir aba/rota auxiliar.

    Só reporta (aviso, não erro) pedidos não encontrados/cancelados — ex.: outra
    pessoa já avançou o status entre a seleção e o clique — sem travar o resto do lote."""
    vendas_pedidas = list(dict.fromkeys(v for v in body.vendas if v))
    pedidos = []
    faltando = []
    cancelados = []
    for v in vendas_pedidas:
        pedido = await vendas_db.get_pedido_by_venda(v)
        if not pedido:
            faltando.append(v)
        elif pedido["status"].strip().lower() == "cancelado":
            cancelados.append(v)
        else:
            pedidos.append(pedido)
    if not pedidos:
        return {"ok": False, "msg": "Nenhum dos pedidos selecionados foi encontrado (ou todos estão cancelados)."}

    # Mesmo gatilho de NF-e do print individual — faltava aqui, então pedido impresso
    # em lote nunca tinha a nota disparada, e o ML trava a etiqueta ("invoice_pending")
    # até a nota existir. Só dispara pros que já entram no dia de entrega.
    for pedido in pedidos:
        data_limite = _parse_data_br(pedido.get("data_limite"))
        if data_limite is not None and data_limite <= date.today():
            background_tasks.add_task(_gatilho_nf, pedido["empresa"], pedido["venda"])

    encontrados = [p["venda"] for p in pedidos]
    zpl = "\n".join(build_separacao_zpl(p) for p in pedidos)
    avisos = []
    if faltando:
        avisos.append(f"{len(faltando)} pedido(s) não encontrado(s) — não serão impressos: " + ", ".join(faltando))
    if cancelados:
        avisos.append(f"{len(cancelados)} pedido(s) cancelado(s) — não serão impressos: " + ", ".join(cancelados))
    aviso = " ".join(avisos)
    return {"ok": True, "vendas": encontrados, "zpl": zpl, "aviso": aviso}


@router.get("/separacao-lote/zpl")
async def print_separacao_lote_zpl(venda: list[str] = Query(...)):
    """Baixa o .zpl combinado do lote (sem imprimir nem confirmar nada)."""
    vendas_pedidas = list(dict.fromkeys(v for v in venda if v))
    pedidos = [p for p in [await vendas_db.get_pedido_by_venda(v) for v in vendas_pedidas] if p]
    if not pedidos:
        raise HTTPException(404, "Nenhum dos pedidos selecionados foi encontrado.")
    zpl = "\n".join(build_separacao_zpl(p) for p in pedidos)
    return Response(
        content=zpl, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="etiquetas_lote_{len(pedidos)}.zpl"',
                 "Cache-Control": "no-store"})


@router.post("/separacao-lote/confirmar")
async def confirmar_separacao_lote(body: VendasIn):
    """Chamado pelo JS só depois que o QZ Tray confirma a impressão do lote com
    sucesso — grava Status="Separado" + horário pra cada venda do lote.

    Cada venda é confirmada isoladamente (1 retry em caso de erro transitório da
    API do Sheets) — uma falha não trava as demais. Antes disso o endpoint sempre
    devolvia {"ok": true} mesmo quando uma venda não era encontrada/gravada (ex.:
    escrita retornando 0 linhas), e o JS só olha "ok" — resultado: a etiqueta saía
    impressa fisicamente mas o pedido ficava preso em "Separando", sem cair na fila
    da Embalagem, e ninguém era avisado. Agora o retorno lista exatamente quais
    vendas confirmaram e quais falharam, pro JS avisar e não fechar a aba sozinho."""
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    confirmados: list[str] = []
    falharam: list[str] = []
    for v in body.vendas:
        ok = False
        for tentativa in range(2):
            try:
                n = await vendas_db.set_status_for_venda(v, "Separado", impresso_em=agora)
                if n > 0:
                    ok = True
                else:
                    logger.warning("Confirmação em lote: venda '%s' não encontrada.", v)
                break
            except Exception:
                if tentativa == 0:
                    continue
                logger.exception("Confirmação em lote: falha ao gravar Status=Separado pra venda '%s'.", v)
        (confirmados if ok else falharam).append(v)
    return {"ok": not falharam, "confirmados": confirmados, "falharam": falharam}


@router.get("/sign")
async def sign_request(request: str = Query("")):
    """Assina o desafio do QZ Tray (habilita o 'lembrar' e some com os avisos)."""
    return Response(content=qz_sign(request), media_type="text/plain; charset=utf-8")


def _qz_print_page(titulo: str, zpl: str, aviso: str = "", extra_html: str = "",
                    extra_success_js: str = "", auto_download_url: str = "") -> str:
    """Página HTML que conecta no QZ Tray, assina (evita prompt repetido) e imprime
    o ZPL na impressora padrão do Windows assim que carrega. Usada só por
    /print/etiqueta hoje (fallback manual do Gerente) — a separação virou impressão
    INLINE direto no Mural (ver templates/mural.html), sem página auxiliar.

    `extra_success_js`, se informado, deve definir uma função JS `confirmarSucesso()`
    que retorna uma Promise — chamada depois que a impressão dá certo e ANTES da aba
    fechar sozinha (usado pra só confirmar um status no backend após sucesso real).

    `auto_download_url`, se informado, dispara o download do arquivo assim que a
    página carrega, EM PARALELO com a tentativa de impressão via QZ Tray — útil
    enquanto as impressoras reais não estão configuradas (o download sempre funciona;
    quando o QZ Tray estiver pronto, a impressão passa a funcionar também, sem
    precisar mudar nada aqui).
    """
    zpl_js = json.dumps(zpl)                     # string JS segura (escapa ^, \n, aspas)
    aviso_html = (f'<div class="warn">{aviso}</div>') if aviso else ""
    success_js = extra_success_js or "function confirmarSucesso() { return Promise.resolve(); }"
    download_js = ""
    if auto_download_url:
        download_js = (
            "(function() { var a = document.createElement('a'); a.href = "
            + json.dumps(auto_download_url)
            + "; a.download = ''; document.body.appendChild(a); a.click(); })();"
        )
    return _PAGE.format(
        titulo=titulo, cdn=QZ_TRAY_CDN, zpl_js=zpl_js, aviso=aviso_html,
        extra_html=extra_html, cert_js=json.dumps(QZ_CERT), extra_success_js=success_js,
        download_js=download_js,
    )


_PAGE = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo}</title>
<style>
  body {{ font-family:-apple-system,Segoe UI,Roboto,sans-serif; background:#f1f3f4;
          margin:0; padding:24px; color:#202124; }}
  .card {{ max-width:560px; margin:0 auto; background:#fff; border-radius:12px;
           box-shadow:0 1px 4px rgba(0,0,0,.15); overflow:hidden; }}
  .head {{ background:#1a1a1a; color:#fff; padding:18px 24px; font-size:20px; font-weight:700; }}
  .body {{ padding:24px; font-size:16px; line-height:1.5; }}
  #status {{ font-size:18px; font-weight:600; margin:12px 0; }}
  .warn {{ background:#fff4e5; color:#8a5300; padding:10px 12px; border-radius:8px; margin:12px 0; font-size:14px; }}
  button, a.btn {{ display:inline-block; margin:8px 8px 0 0; background:#1a1a1a; color:#fff;
           border:0; text-decoration:none; padding:12px 18px; border-radius:8px;
           font-size:15px; font-weight:600; cursor:pointer; }}
  a.btn.sec {{ background:#5f6368; }}
  .hint {{ color:#5f6368; font-size:13px; margin-top:16px; }}
</style></head><body><div class="card">
  <div class="head">{titulo}</div>
  <div class="body">
    {aviso}
    <div id="status">Conectando à impressora…</div>
    <button onclick="imprimir()">Imprimir novamente</button>
    {extra_html}
    <div class="hint">Requer o <b>QZ Tray</b> aberto neste computador. Na 1ª vez o QZ pede
      permissão — clique <b>Permitir</b> (pode marcar p/ lembrar). A impressão vai pra
      impressora <b>padrão</b> do Windows.</div>
  </div>
</div>
<script src="{cdn}"></script>
<script>
  var ZPL = {zpl_js};
  var CERT = {cert_js};
  function setStatus(t) {{ document.getElementById('status').textContent = t; }}
  {extra_success_js}
  {download_js}
  // Assinatura → habilita o "lembrar" do QZ e remove os avisos repetidos
  if (typeof qz !== 'undefined') {{
    qz.security.setCertificatePromise(function(resolve) {{ resolve(CERT); }});
    qz.security.setSignatureAlgorithm("SHA512");
    qz.security.setSignaturePromise(function(toSign) {{
      return function(resolve, reject) {{
        fetch('/print/sign?request=' + encodeURIComponent(toSign))
          .then(function(r) {{ return r.text(); }}).then(resolve).catch(reject);
      }};
    }});
  }}
  function imprimir() {{
    if (typeof qz === 'undefined') {{ setStatus('QZ Tray não carregou (sem internet?).'); return; }}
    setStatus('Conectando à impressora…');
    var conn = qz.websocket.isActive() ? Promise.resolve() : qz.websocket.connect();
    conn.then(function() {{ return qz.printers.getDefault(); }})
      .then(function(printer) {{
        setStatus('Enviando p/ ' + printer + '…');
        var cfg = qz.configs.create(printer);
        return qz.print(cfg, [{{ type: 'raw', format: 'plain', data: ZPL }}]);
      }})
      .then(function() {{
        setStatus('Impresso! Confirmando…');
        return confirmarSucesso();
      }})
      .then(function() {{
        setStatus('Impresso!');
        // imprimiu (e confirmou) → fecha a aba sozinha (hack window.open('_self') ajuda o close a passar)
        setTimeout(function() {{
          try {{ window.open('', '_self', ''); window.close(); }} catch (e) {{}}
          try {{ window.close(); }} catch (e) {{}}
          setStatus('Impresso! (pode fechar esta aba)');
        }}, 300);
      }})
      .catch(function(err) {{
        var m = String(err);
        if (m.indexOf('connection') > -1 || m.indexOf('QZ') > -1)
          setStatus('QZ Tray não está aberto neste PC. Abra o QZ Tray e clique "Imprimir novamente".');
        else setStatus(m);
      }});
  }}
  window.addEventListener('load', imprimir);
</script>
</body></html>"""


_BAIXAR_PAGE = """<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<title>Baixando…</title></head><body style="font-family:sans-serif;padding:20px">
Baixando etiqueta…
<script>
  var ZPL = {zpl_js};
  var a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([ZPL], {{ type: 'application/octet-stream' }}));
  a.download = "{fname}";
  document.body.appendChild(a); a.click();
  setTimeout(function() {{
    try {{ window.open('', '_self', ''); window.close(); }} catch (e) {{}}
    try {{ window.close(); }} catch (e) {{}}
  }}, 400);
</script></body></html>"""
