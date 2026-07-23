import io
import zipfile
from datetime import datetime, timezone, timedelta
import httpx
from config.settings import get_settings

MELI_API = "https://api.mercadolibre.com"
MELI_AUTH = "https://auth.mercadolivre.com.br/authorization"
MELI_TOKEN_URL = f"{MELI_API}/oauth/token"

# Brasil não usa horário de verão desde 2019 → offset fixo (evita depender de tzdata)
_BR_TZ = timezone(timedelta(hours=-3))


def _fmt_date_br(iso: str) -> str:
    """Formata uma data ISO do ML em DD/MM/AAAA no fuso de Brasília (evita erro de 1 dia)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_BR_TZ)
        return dt.strftime("%d/%m/%Y")
    except Exception:
        d = iso[:10]
        try:
            ano, mes, dia = d.split("-")
            return f"{dia}/{mes}/{ano}"
        except ValueError:
            return d


def _ddmmaa_br(iso: str) -> str:
    """Data ISO -> 'DDMMAA' no fuso de Brasília (p/ o ID de expedição). Vazio se falhar."""
    br = _fmt_date_br(iso)                      # DD/MM/AAAA
    return br[0:2] + br[3:5] + br[8:10] if len(br) == 10 else ""


def build_qr_zpl(exped_id: str) -> str:
    """Monta uma etiqueta ZPL com o ID de expedição em texto + QR Code do mesmo ID.

    A impressora (Zebra/Bematech em emulação ZPL) desenha o QR a partir do texto —
    não geramos imagem. Ajuste ^FO/^BQ conforme o tamanho da etiqueta física.

    Sem ^PW/^LL de propósito: usada também no combo manual do Gerente (`_montar_zpl`),
    concatenada LOGO APÓS a etiqueta do ML no mesmo job — nesse caso ela herda o
    tamanho de página que a etiqueta do ML já configurou (mesmo rolo físico). Pro
    QR isolado na impressora pequena da gaiola, ver `build_gaiola_zpl`.
    """
    eid = (exped_id or "").strip() or "SEM-ID"
    return (
        "^XA\n"
        "^CI28\n"                                   # UTF-8
        f"^FO40,35^A0N,45,45^FDExpedicao {eid}^FS\n"
        f"^FO40,100^BQN,2,7^FDLA,{eid}^FS\n"        # QR do ID (correção L, modo A)
        "^XZ\n"
    )


def build_gaiola_zpl(exped_id: str) -> str:
    """Etiqueta da gaiola (impressora dedicada, ZD220-BOX): formato físico fixo
    50x30mm, 1 coluna (203dpi ≈ 400x240 pontos). ^PW/^LL explícitos pra caber
    sem cortar — a versão de `build_qr_zpl` (dimensionada pro rolo maior da
    etiqueta do ML) saía cortada nessa impressora menor (reportado pelo usuário
    em teste real, 2026-07-21). Texto centralizado via ^FB (largura = a página
    toda); QR com magnificação maior (7) e X calculado pra ficar centralizado —
    o ID de expedição tem SEMPRE 12 caracteres (sigla 3 + DDMMAA 6 + sequencial
    3, ver SheetsService.make_expedition_id), então cabe em QR versão 1 (21
    módulos) e a estimativa de tamanho usada aqui pra centralizar é estável.
    """
    eid = (exped_id or "").strip() or "SEM-ID"
    return (
        "^XA\n"
        "^PW400\n"
        "^LL240\n"
        "^CI28\n"
        f"^FO0,28^A0N,32,32^FB400,1,0,C^FDExped {eid}^FS\n"
        f"^FO125,68^BQN,2,7^FDLA,{eid}^FS\n"
        "^XZ\n"
    )


def _zpl_safe(texto) -> str:
    """Remove caracteres de controle do ZPL (^ e ~) de texto vindo de dados reais
    (endereço, SKU, nome) — evita que um valor com esses símbolos quebre a etiqueta."""
    return str(texto or "").replace("^", "").replace("~", "")


def build_separacao_zpl(pedido: dict) -> str:
    """Etiqueta de expedição/separação, formato físico fixo 100x50mm (203dpi =
    800x400 pontos, landscape — larga como a etiqueta do ML, só mais curta).
    Coluna esquerda: venda + empresa + data limite + horário de impressão + código
    de barras do ID de expedição (só barras — sem QR, os leitores da bancada de
    embalagem são só de código de barras). Coluna direita: lista de itens (SKU,
    quantidade, endereço), aproveitando a largura da etiqueta. Pedidos com muitos
    itens podem ultrapassar a etiqueta (limitação física do formato pequeno).

    Código de barras em Code 39 (^B3), não Code 128 (^BC): o Code 128 tem 3
    sub-conjuntos de caracteres (A/B/C) e pode alternar automaticamente pro B
    (que inclui minúsculas) pra compactar — foi isso que causou leitura em
    minúsculas não reconhecida pelo sistema. O ID de expedição é sempre maiúsculo
    (ex.: PLG080626001), e o Code 39 simplesmente não tem minúsculas no alfabeto
    dele, então elimina esse problema de vez. A comparação na leitura também é
    case-insensitive (ver SheetsService.get_pedido_by_exped), como defesa extra
    contra leitores que invertem maiúscula/minúscula.
    """
    venda = _zpl_safe(pedido.get("venda"))
    empresa = _zpl_safe(pedido.get("empresa"))
    data_limite = _zpl_safe(pedido.get("data_limite"))
    exped_id = _zpl_safe(pedido.get("id_exped")) or "SEM-ID"
    horario = datetime.now(_BR_TZ).strftime("%d/%m %H:%M")
    itens = pedido.get("itens") or []

    itens_zpl = []
    y = 40
    for item in itens:
        sku = _zpl_safe(item.get("sku"))
        qtd = _zpl_safe(item.get("qtd"))
        endereco = _zpl_safe(item.get("endereco")) or "Sem endereco"
        itens_zpl.append(f"^FO420,{y}^A0N,20,20^FDSKU {sku}  Qtd {qtd}^FS")
        itens_zpl.append(f"^FO420,{y + 22}^A0N,16,16^FD{endereco}^FS")
        y += 46

    return (
        "^XA\n"
        "^PW800\n"
        "^LL400\n"
        "^CI28\n"
        f"^FO0,128^A0N,32,32^FB400,1,0,C^FDVenda {venda}^FS\n"
        f"^FO0,166^A0N,24,24^FB400,1,0,C^FD{empresa}^FS\n"
        f"^FO0,194^A0N,18,18^FB400,1,0,C^FDEntrega {data_limite}  Imp {horario}^FS\n"
        f"^FO20,218^BY2,2,55\n"
        f"^FO20,218^B3N,N,55,Y,N^FD{exped_id}^FS\n"
        f"^FO420,10^A0N,18,18^FDItens^FS\n"
        + "\n".join(itens_zpl) + "\n"
        "^XZ\n"
    )


def _extract_zpl_from_label(content: bytes) -> str:
    """A etiqueta zpl2 do ML vem zipada (Etiqueta de envio.txt). Extrai o ZPL cru.

    Levanta erro se vier PDF em vez de ZPL — acontece pra envios que não têm
    etiqueta térmica disponível (ex.: já enviado/cancelado, ou método de envio
    sem suporte a zpl2). Sem essa checagem, o PDF era decodificado como latin-1
    e devolvido como se fosse ZPL válido (bytes preservados, então o "ZPL" abria
    como PDF de verdade ao salvar) — o chamador trata esse erro como "etiqueta
    do ML indisponível", que é o comportamento tolerante já esperado.
    """
    if content[:4] == b"%PDF":
        raise ValueError("ML devolveu PDF em vez de ZPL (etiqueta térmica indisponível pra este envio).")
    if content[:2] == b"PK":                        # ZIP
        zf = zipfile.ZipFile(io.BytesIO(content))
        nome = next((n for n in zf.namelist() if n.lower().endswith(".txt")), zf.namelist()[0])
        return zf.read(nome).decode("latin-1")
    return content.decode("latin-1")                # já veio ZPL cru


# Tabela SEFAZ de origem da mercadoria (campo "Origem" da NF)
_ORIGEM_SEFAZ = {
    "0": "0 - Nacional",
    "1": "1 - Estrangeira (importação direta)",
    "2": "2 - Estrangeira (mercado interno)",
    "3": "3 - Nacional (importação 40%-70%)",
    "4": "4 - Nacional (processos produtivos básicos)",
    "5": "5 - Nacional (importação ≤40%)",
    "6": "6 - Estrangeira (importação direta, sem similar nacional)",
    "7": "7 - Estrangeira (mercado interno, sem similar nacional)",
    "8": "8 - Nacional (importação >70%)",
}


def _origem_label(origin_detail) -> str:
    """Traduz o código de origem (0-8) do ML para texto legível na planilha."""
    if origin_detail in (None, ""):
        return ""
    code = str(origin_detail).strip()
    return _ORIGEM_SEFAZ.get(code, code)


def _origem_code(value) -> str:
    """Extrai o código (0-8) de um texto de Origem (ex.: '0 - Nacional' -> '0')."""
    if value in (None, ""):
        return ""
    s = str(value).strip()
    return s[0] if s and s[0].isdigit() else s


def _parse_billing(billing: dict) -> tuple[str, str]:
    """Extrai (nome_comprador, documento) do billing_info, tolerante a formatos.

    O ML pode devolver o bloco fiscal em billing.billing_info,
    billing.buyer.billing_info ou no próprio topo. O nome pode vir como
    razão social (BUSINESS_NAME) ou nome+sobrenome (FIRST_NAME/LAST_NAME).
    """
    if not billing:
        return "", ""
    bi = (billing.get("billing_info")
          or (billing.get("buyer") or {}).get("billing_info")
          or billing)

    doc_type = bi.get("doc_type") or bi.get("identification", {}).get("type") or ""
    doc_number = bi.get("doc_number") or bi.get("identification", {}).get("number") or ""
    documento = f"{doc_type} {doc_number}".strip()

    info = {i.get("type"): i.get("value")
            for i in (bi.get("additional_info") or []) if i.get("value")}
    nome = (
        info.get("BUSINESS_NAME")
        or " ".join(x for x in [info.get("FIRST_NAME"), info.get("LAST_NAME")] if x).strip()
        or bi.get("business_name")
        or bi.get("name")
        or ""
    )
    return nome.strip(), documento


class MeliService:
    """Cliente da API do ML para UMA loja já autenticada.

    `store` é o dict vindo do Firestore (user_id, access_token, refresh_token, ...).
    Ao renovar o token, persiste de volta no Firestore via `token_store`.
    """

    def __init__(self, store: dict, token_store=None):
        self.store = store
        self.token_store = token_store
        self._access_token = store.get("access_token", "")
        self._refresh_token = store.get("refresh_token", "")
        self.user_id = str(store.get("user_id", ""))
        self.settings = get_settings()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def refresh_token(self) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(MELI_TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": self.settings.meli_client_id,
                "client_secret": self.settings.meli_client_secret,
                "refresh_token": self._refresh_token,
            })
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            self._refresh_token = data.get("refresh_token", self._refresh_token)
            if self.token_store and self.user_id:
                self.token_store.update_tokens(self.user_id, self._access_token, self._refresh_token)
            return self._access_token

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{MELI_API}{path}", headers=self._headers(), params=params)
            if resp.status_code == 401:
                await self.refresh_token()
                resp = await client.get(f"{MELI_API}{path}", headers=self._headers(), params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_user_id(self) -> str:
        if self.user_id:
            return self.user_id
        data = await self._get("/users/me")
        self.user_id = str(data["id"])
        return self.user_id

    async def _post(self, path: str, json_body: dict) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{MELI_API}{path}", headers=self._headers(), json=json_body)
            if resp.status_code == 401:
                await self.refresh_token()
                resp = await client.post(f"{MELI_API}{path}", headers=self._headers(), json=json_body)
            resp.raise_for_status()
            return resp.json()

    async def get_all_items(self) -> list[dict]:
        """Retorna todos os anúncios ativos da loja."""
        user_id = await self.get_user_id()

        # 1) coleta todos os IDs (paginado)
        all_ids: list[str] = []
        offset = 0
        limit = 50
        while True:
            data = await self._get(f"/users/{user_id}/items/search", {
                "status": "active", "offset": offset, "limit": limit,
            })
            ids = data.get("results", [])
            if not ids:
                break
            all_ids.extend(ids)
            offset += limit
            if offset >= data.get("paging", {}).get("total", 0):
                break

        # 2) busca detalhes em lotes de 20 (limite do endpoint multiget do ML)
        items = []
        for i in range(0, len(all_ids), 20):
            chunk = all_ids[i:i + 20]
            detail = await self._get("/items", {
                "ids": ",".join(chunk),
                "attributes": "id,title,price,available_quantity,attributes,"
                              "seller_custom_field,variations,user_product_id",
            })
            for entry in detail:
                if entry.get("code") == 200:
                    items.append(entry["body"])

        # 3) resolve o SKU via "user products" (catálogo de SKU do ML novo)
        #    para itens que não têm SKU nos campos antigos do anúncio.
        cache: dict[str, str] = {}
        for it in items:
            if it.get("seller_custom_field"):
                continue
            upid = it.get("user_product_id")
            if not upid:
                for v in it.get("variations") or []:
                    if v.get("user_product_id"):
                        upid = v["user_product_id"]
                        break
            if upid:
                sku = await self._user_product_sku(upid, cache)
                if sku:
                    it["_resolved_sku"] = sku
        return items

    async def _user_product_sku(self, user_product_id: str, cache: dict) -> str:
        """Lê o SELLER_SKU de um user-product (com cache por id)."""
        if user_product_id in cache:
            return cache[user_product_id]
        sku = ""
        try:
            up = await self._get(f"/user-products/{user_product_id}")
            for a in up.get("attributes", []):
                if a.get("id") == "SELLER_SKU" and a.get("values"):
                    sku = a["values"][0].get("name") or ""
                    break
        except Exception:
            sku = ""
        cache[user_product_id] = sku
        return sku

    async def get_item_fiscal(self, sku: str, cache: dict | None = None) -> dict:
        """Dados fiscais do anúncio pelo SKU: NCM, CEST, EAN, Origem.

        Lê GET /items/fiscal_information?sku=... (recurso fiscal do anúncio no ML).
        Tolerante a falha; campos ausentes voltam "". Cache opcional por SKU.
        """
        out = {"ncm": "", "cest": "", "ean": "", "origin": ""}
        if not sku:
            return out
        if cache is not None and sku in cache:
            return cache[sku]
        try:
            data = await self._get("/items/fiscal_information", {"sku": sku})
            tax = data.get("tax_information") or {}
            out["ncm"] = tax.get("ncm") or ""
            out["cest"] = tax.get("cest") or ""
            out["ean"] = tax.get("ean") or ""
            out["origin"] = _origem_label(tax.get("origin_detail"))
        except Exception:
            pass
        if cache is not None:
            cache[sku] = out
        return out

    async def find_tax_rule_id(self, skus: list[str]) -> int | None:
        """Acha a 'Regra Basica' da loja lendo o 1º SKU já configurado (com tax_rule_id)."""
        for sku in skus:
            if not sku:
                continue
            try:
                atual = await self._get("/items/fiscal_information", {"sku": sku})
                trid = (atual.get("tax_information") or {}).get("tax_rule_id")
                if trid:
                    return trid
            except Exception:
                continue
        return None

    async def send_fiscal(self, sku: str, ncm: str = "", origin_detail: str = "",
                          cest: str = "", ean: str = "",
                          default_tax_rule_id: int | None = None) -> dict:
        """Envia (PUT) os dados fiscais de um SKU, preservando o que já existe no ML.

        Sobrescreve só NCM/Origem/CEST/EAN; mantém type, tax_rule_id (Regra),
        pesos e demais campos. Retorna {ok, message}.
        """
        base: dict = {}
        cur_type = "single"
        try:
            atual = await self._get("/items/fiscal_information", {"sku": sku})
            base = dict(atual.get("tax_information") or {})
            cur_type = atual.get("type") or "single"
        except Exception:
            pass

        if ncm:
            base["ncm"] = ncm
        if cest:
            base["cest"] = cest
        if ean:
            base["ean"] = ean
        if origin_detail not in (None, ""):
            base["origin_detail"] = str(origin_detail)
        base.setdefault("origin_type", "reseller")
        if not base.get("tax_rule_id") and default_tax_rule_id:
            base["tax_rule_id"] = default_tax_rule_id
        base.pop("empty", None)   # campo só de leitura

        body = {"type": cur_type, "tax_information": base}
        url = f"{MELI_API}/items/fiscal_information/{sku}"
        headers = {**self._headers(), "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient() as client:
                r = await client.put(url, headers=headers, json=body)
                if r.status_code == 401:
                    await self.refresh_token()
                    headers = {**self._headers(), "Content-Type": "application/json"}
                    r = await client.put(url, headers=headers, json=body)
            if 200 <= r.status_code < 300:
                return {"ok": True, "message": "enviado"}
            try:
                detail = r.json()
                msg = detail.get("message") or str(detail)
            except Exception:
                msg = r.text[:200]
            return {"ok": False, "message": f"{r.status_code}: {msg}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def get_order(self, order_id: str) -> dict:
        return await self._get(f"/orders/{order_id}")

    async def get_claim(self, claim_id: str) -> dict:
        """Rota antiga /claims/{id} foi descontinuada pelo ML (deprecated maio/2024) —
        só funciona via /post-purchase/v1/claims/{id} agora (confirmado ao vivo)."""
        return await self._get(f"/post-purchase/v1/claims/{claim_id}")

    async def get_claim_reason(self, reason_id: str) -> str:
        """Traduz o código do motivo (ex.: "PDD9939") pro texto real que o ML mostra
        pro comprador (campo `detail`) — via o catálogo /claims/reasons/{id}. Se a
        consulta falhar por qualquer motivo, devolve o próprio código bruto (nunca
        quebra o registro da devolução por causa disso)."""
        try:
            r = await self._get(f"/post-purchase/v1/claims/reasons/{reason_id}")
            return r.get("detail") or reason_id
        except Exception:
            return reason_id

    async def get_return_detail(self, claim_id: str) -> dict:
        """Detalhe do envio de volta (etiqueta gerada/postado/entregue) — rota
        separada do claim em si, só existe pra claims do tipo "returns"."""
        return await self._get(f"/post-purchase/v2/claims/{claim_id}/returns")

    async def get_devolucao_fase(self, claim_id: str) -> str:
        """Fase física da devolução, pro claim ainda ABERTO (não chamar depois de
        fechado — nesse caso a fase já é "Concluído", direto do status do claim,
        sem precisar desta consulta extra). Mapeamento (confirmado ao vivo em
        2 casos reais): status "label_generated" = etiqueta gerada, comprador ainda
        não postou = "Em preparação"; "delivered" = chegou fisicamente = "A revisar".
        Qualquer outro status observado (ainda não vimos um caso real passando por
        "postado"/"em trânsito") cai no fallback "A caminho"."""
        try:
            detail = await self.get_return_detail(claim_id)
        except Exception:
            return "Em preparação"
        status = (detail or {}).get("status", "")
        if status == "label_generated":
            return "Em preparação"
        if status == "delivered":
            return "A revisar"
        return "A caminho"

    async def get_shipment(self, shipping_id: str) -> dict:
        return await self._get(f"/shipments/{shipping_id}")

    async def get_label_zpl(self, shipping_id: str) -> str:
        """Retorna o ZPL da etiqueta de envio do ML (descompactado). Lança em erro."""
        async with httpx.AsyncClient() as client:
            url = f"{MELI_API}/shipment_labels"
            params = {"shipment_ids": shipping_id, "response_type": "zpl2"}
            resp = await client.get(url, headers=self._headers(), params=params)
            if resp.status_code == 401:
                await self.refresh_token()
                resp = await client.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            return _extract_zpl_from_label(resp.content)

    async def resolve_order_and_shipping(self, numero: str) -> tuple[dict, str]:
        """Aceita order_id OU nº de venda/pack do painel. Retorna (order, shipping_id)."""
        try:
            order = await self.get_order(numero)
        except Exception:
            pack = await self._get(f"/packs/{numero}")     # era um pack: resolve o pedido
            orders = pack.get("orders") or []
            if not orders:
                raise
            order = await self.get_order(str(orders[0].get("id")))
        shipping_id = str((order.get("shipping") or {}).get("id") or "")
        return order, shipping_id

    async def get_invoice_data(self, shipping_id: str) -> dict:
        return await self._get(f"/shipments/{shipping_id}/invoice_data", {"siteId": "MLB"})

    async def get_invoice_status(self, order_id: str) -> dict:
        """Status da NF-e emitida pelo "Faturador" do ML pra esse pedido (não
        confundir com get_invoice_data, que lê a NF via o shipment). None em
        "status" significa que ainda não foi emitida nenhuma nota pra esse pedido."""
        user_id = await self.get_user_id()
        try:
            return await self._get(f"/users/{user_id}/invoices/orders/{order_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {"status": None}
            raise

    async def emit_invoice(self, order_id: str) -> dict:
        """Aciona a emissão da NF-e pelo Faturador do ML (mesma ação do botão
        "Emitir NF-e" no painel) — POST /users/{user_id}/invoices/orders.
        Retorna {"ok": bool, "message": str}, tolerante a erro (ex.: dado fiscal
        faltando no anúncio, retorna 400 com mensagem do ML)."""
        user_id = await self.get_user_id()
        try:
            data = await self._post(f"/users/{user_id}/invoices/orders", {"orders": [int(order_id)]})
            return {"ok": True, "message": "emitida", "data": data}
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.json()
                msg = detail.get("message") or str(detail)
            except Exception:
                msg = e.response.text[:200]
            return {"ok": False, "message": f"{e.response.status_code}: {msg}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def ensure_invoice(self, order_id: str) -> dict:
        """"Validador + gatilho" da NF-e: consulta o status atual e só aciona a
        emissão se ainda não existir nenhuma tentativa em andamento/concluída —
        evita re-emitir/duplicar quando o gatilho dispara em mais de um ponto
        (separação + embalagem) pro mesmo pedido. Retorno uniforme:
        {"status": str|None, "ja_existia": bool, "erro": str|None}."""
        try:
            atual = await self.get_invoice_status(order_id)
        except Exception as e:
            return {"status": None, "ja_existia": False, "erro": str(e)}

        status_atual = (atual or {}).get("status")
        if (status_atual or "").strip().lower() in ("pending", "processing", "authorized"):
            return {"status": status_atual, "ja_existia": True, "erro": None}

        resultado = await self.emit_invoice(order_id)
        if resultado["ok"]:
            novo_status = (resultado.get("data") or {}).get("status") or "pending"
            return {"status": novo_status, "ja_existia": False, "erro": None}
        return {"status": None, "ja_existia": False, "erro": resultado["message"]}

    async def get_billing_info(self, order_id: str) -> dict:
        """Dados fiscais do comprador (CPF/CNPJ, nome, endereço) p/ emissão da NF.

        Usa o endpoint /orders/{id}/billing_info com header x-version:2.
        """
        async with httpx.AsyncClient() as client:
            url = f"{MELI_API}/orders/{order_id}/billing_info"
            headers = {**self._headers(), "x-version": "2"}
            resp = await client.get(url, headers=headers)
            if resp.status_code == 401:
                await self.refresh_token()
                headers = {**self._headers(), "x-version": "2"}
                resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get_fiscal_summary(self, order: dict) -> dict:
        """Reúne os dados fiscais de um pedido (NF emitida + comprador).

        Tolerante a falhas: campos que o ML não tiver voltam como "".
        Chaves: nf_numero_serie, nf_valor, nf_cfop, nf_chave, comprador, documento.
        """
        order_id = str(order.get("id", ""))
        shipping_id = str((order.get("shipping") or {}).get("id") or "")
        out = {"nf_numero_serie": "", "nf_valor": "", "nf_cfop": "",
               "nf_chave": "", "comprador": "", "documento": ""}

        # 1) NF emitida (invoice_data do shipment) — mesmos campos da página de NF
        if shipping_id:
            try:
                inv = await self.get_invoice_data(shipping_id)
                num = inv.get("invoice_number") or ""
                serie = inv.get("invoice_serie") or ""
                if num or serie:
                    out["nf_numero_serie"] = f"{num}/{serie}"
                out["nf_chave"] = inv.get("fiscal_key") or ""
                out["nf_cfop"] = (inv.get("additional_data") or {}).get("cfop") or ""
                val = inv.get("invoice_amount")
                out["nf_valor"] = val if val is not None else ""
            except Exception:
                pass

        # 2) Dados do comprador (billing_info)
        if order_id:
            try:
                nome, doc = _parse_billing(await self.get_billing_info(order_id))
                out["comprador"] = nome
                out["documento"] = doc
            except Exception:
                pass
        return out

    async def get_delivery_deadline(self, order: dict) -> str:
        """Retorna o prazo de DESPACHO (DD/MM/AAAA) — o "despachar até" que o ML cobra.

        Usa o SLA dedicado do envio (`/shipments/{id}/sla` → expected_date), que é o
        mesmo prazo do painel. Se falhar, cai na data de entrega ao cliente (fallback).
        """
        shipping_id = (order.get("shipping") or {}).get("id")
        if not shipping_id:
            return ""
        # 1) Prazo de despacho ("despachar até") — SLA do envio
        try:
            sla = await self._get(f"/shipments/{shipping_id}/sla")
            if sla.get("expected_date"):
                return _fmt_date_br(sla["expected_date"])
        except Exception:
            pass
        # 2) Fallback: entrega ao cliente (comportamento antigo)
        try:
            sh = await self.get_shipment(shipping_id)
        except Exception:
            return ""
        opt = sh.get("shipping_option") or {}
        raw = (
            (opt.get("estimated_delivery_limit") or {}).get("date")
            or (opt.get("estimated_delivery_time") or {}).get("date")
            or (opt.get("estimated_delivery_final") or {}).get("date")
        )
        return _fmt_date_br(raw)

    async def get_recent_orders(self, max_orders: int = 50) -> list[dict]:
        """Busca os pedidos pagos mais recentes da loja (para backfill)."""
        user_id = await self.get_user_id()
        orders = []
        offset = 0
        limit = 50
        while len(orders) < max_orders:
            data = await self._get("/orders/search", {
                "seller": user_id,
                "order.status": "paid",
                "sort": "date_desc",
                "offset": offset,
                "limit": limit,
            })
            results = data.get("results", [])
            if not results:
                break
            orders.extend(results)
            offset += limit
            if offset >= data.get("paging", {}).get("total", 0):
                break
        return orders[:max_orders]

    # ── OAuth (sem token ainda — usa a app única) ─────────────────────────────

    @staticmethod
    def get_oauth_url(company_key: str) -> str:
        settings = get_settings()
        return (
            f"{MELI_AUTH}?response_type=code"
            f"&client_id={settings.meli_client_id}"
            f"&redirect_uri={settings.meli_redirect_uri}"
            f"&state={company_key}"
        )

    @staticmethod
    async def exchange_code(code: str) -> dict:
        settings = get_settings()
        async with httpx.AsyncClient() as client:
            resp = await client.post(MELI_TOKEN_URL, data={
                "grant_type": "authorization_code",
                "client_id": settings.meli_client_id,
                "client_secret": settings.meli_client_secret,
                "code": code,
                "redirect_uri": settings.meli_redirect_uri,
            })
            resp.raise_for_status()
            return resp.json()
