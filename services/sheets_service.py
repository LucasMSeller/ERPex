import json
import re
import time
import threading
from datetime import date
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from config.settings import get_settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Linha onde os dados começam (após cabeçalho) em cada aba de empresa
COMPANY_DATA_START_ROW = 3   # linha 1 = título, linha 2 = cabeçalho, linha 3+ = dados
VENDAS_DATA_START_ROW = 3
ENDERECAMENTO_DATA_START_ROW = 3


def _parse_data_br(valor: str) -> date | None:
    """'DD/MM/AAAA' -> date. None se vazio/inválido."""
    try:
        d, m, a = (valor or "").strip().split("/")
        return date(int(a), int(m), int(d))
    except Exception:
        return None


def _build_service():
    settings = get_settings()
    sa_json = settings.google_service_account_json
    if sa_json.endswith(".json"):
        creds = Credentials.from_service_account_file(sa_json, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


class SheetsService:
    # Compartilhado por TODAS as instâncias/requests deste processo — a cota do
    # Sheets ("60 escritas/min") é da service account (1 usuário só pro Google),
    # não por instância, então o controle também precisa ser global, senão cada
    # SheetsService() novo (1 por request) acha que tem 60/min só pra si.
    _write_lock = threading.Lock()
    _write_times: list[float] = []

    def __init__(self):
        self.service = _build_service()
        self.sheet_id = get_settings().google_sheet_id
        self._svc = self.service.spreadsheets()

    def _throttle_escritas(self):
        """Paceia as escritas pra nunca passar de ~55/min (margem de segurança sob
        a cota real de 60/min/usuário) — evita bater na cota no meio de um sync
        grande (produtos/pedidos escrevem 1 linha por chamada) em vez de só reagir
        depois com retry, que se mostrou tarde demais pra catálogos grandes."""
        with self._write_lock:
            agora = time.monotonic()
            self._write_times[:] = [t for t in self._write_times if agora - t < 60]
            if len(self._write_times) >= 55:
                time.sleep(max(0, 60 - (agora - self._write_times[0]) + 0.5))
                agora = time.monotonic()
                self._write_times[:] = [t for t in self._write_times if agora - t < 60]
            self._write_times.append(agora)

    def _execute_with_retry(self, request, tentativas: int = 8):
        """Rede de segurança pro caso do pacing acima não bastar (ex.: outra
        instância do Cloud Run escrevendo ao mesmo tempo) — espera até ~2min no
        total antes de desistir, dando tempo da cota (janela de 1 min) liberar."""
        for tentativa in range(tentativas):
            try:
                return request.execute()
            except HttpError as e:
                if e.resp.status == 429 and tentativa < tentativas - 1:
                    time.sleep(min(30, 2 ** tentativa))
                    continue
                raise

    def _read(self, range_: str) -> list[list]:
        result = self._execute_with_retry(
            self._svc.values().get(spreadsheetId=self.sheet_id, range=range_)
        )
        return result.get("values", [])

    def _read_unformatted(self, range_: str) -> list[list]:
        """Igual `_read`, mas com `valueRenderOption=UNFORMATTED_VALUE` — células
        numéricas voltam como número de verdade (float), não a string já formatada
        (que vem truncada em notação científica pra número NUMBER-formatado com
        muitos dígitos, ex. "2,00002E+15" pra um order_id de 16 dígitos — ver
        [[feedback_sheets_batch_writes]]/INCIDENTE 2026-07-20 na memória do
        projeto). Usado só onde precisamos do valor exato (migração de Vendas)."""
        result = self._execute_with_retry(
            self._svc.values().get(spreadsheetId=self.sheet_id, range=range_,
                                    valueRenderOption="UNFORMATTED_VALUE")
        )
        return result.get("values", [])

    def _append(self, range_: str, values: list[list]) -> None:
        self._throttle_escritas()
        self._execute_with_retry(self._svc.values().append(
            spreadsheetId=self.sheet_id,
            range=range_,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ))

    def _update(self, range_: str, values: list[list]) -> None:
        self._throttle_escritas()
        self._execute_with_retry(self._svc.values().update(
            spreadsheetId=self.sheet_id,
            range=range_,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ))

    # ── Gestão de abas (criação por loja a partir do modelo) ──────────────────

    def _get_sheet_id(self, tab_name: str) -> int | None:
        meta = self._svc.get(spreadsheetId=self.sheet_id).execute()
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == tab_name:
                return s["properties"]["sheetId"]
        return None

    def tab_exists(self, tab_name: str) -> bool:
        return self._get_sheet_id(tab_name) is not None

    def ensure_store_tab(self, store_name: str) -> str:
        """Cria a aba da loja a partir do modelo, se ainda não existir.

        Duplica a aba-modelo (mantém formatação/cabeçalho), limpa os dados,
        e ajusta o título. Retorna o nome final da aba.
        """
        if self.tab_exists(store_name):
            return store_name

        template = get_settings().sheet_template_tab
        template_id = self._get_sheet_id(template)
        if template_id is None:
            raise ValueError(f"Aba-modelo '{template}' não encontrada na planilha.")

        # 1) duplica o modelo com o nome da loja
        self._svc.batchUpdate(spreadsheetId=self.sheet_id, body={"requests": [
            {"duplicateSheet": {"sourceSheetId": template_id, "newSheetName": store_name}}
        ]}).execute()

        # 2) limpa eventuais dados de exemplo abaixo do cabeçalho
        self._svc.values().clear(
            spreadsheetId=self.sheet_id,
            range=f"'{store_name}'!A{COMPANY_DATA_START_ROW}:U",
        ).execute()

        # 3) atualiza o título (linha 1)
        self._update(f"'{store_name}'!A1", [[f"CADASTRO DE PRODUTOS – {store_name.upper()}"]])

        # 4) limpa a formatação da área de dados (fundo branco, texto preto)
        new_id = self._get_sheet_id(store_name)
        if new_id is not None:
            self.reset_data_formatting(new_id)
        return store_name

    def reset_data_formatting(self, sheet_id: int, start_row: int = COMPANY_DATA_START_ROW) -> None:
        """Deixa a área de dados com fundo branco e texto preto (sem o azul do modelo)."""
        self._svc.batchUpdate(spreadsheetId=self.sheet_id, body={"requests": [{
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": start_row - 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                    "textFormat": {"foregroundColor": {"red": 0, "green": 0, "blue": 0}, "bold": False},
                }},
                "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.foregroundColor,userEnteredFormat.textFormat.bold",
            }
        }]}).execute()

    # ── Empresa (produto) ────────────────────────────────────────────────────
    # Índices das colunas que o ML "é dono" — só essas são tocadas no update.
    # Todas as demais (NCM, CFOP, fornecedor, observações...) são manuais e
    # NUNCA são sobrescritas pelo sync.
    _COL_SKU, _COL_NAME, _COL_EAN, _COL_PRICE, _COL_QTY, _COL_MLB = 1, 2, 6, 10, 11, 16
    _COL_NCM, _COL_CEST, _COL_ORIGIN = 4, 5, 7   # dados fiscais do anúncio (ML)

    def get_products(self, tab: str) -> list[list]:
        return self._read(f"'{tab}'!A{COMPANY_DATA_START_ROW}:U")

    def get_products_map(self, tab: str) -> dict[str, tuple[int, list]]:
        """Retorna {mlb_id: (row_num, linha_completa_21_cols)}."""
        rows = self.get_products(tab)
        result = {}
        for i, row in enumerate(rows):
            padded = (row + [""] * 21)[:21]
            if padded[self._COL_MLB]:
                result[padded[self._COL_MLB]] = (i + COMPANY_DATA_START_ROW, padded)
        return result

    def next_id(self, tab: str) -> int:
        rows = self.get_products(tab)
        ids = [int(r[0]) for r in rows if r and str(r[0]).isdigit()]
        return max(ids, default=0) + 1

    def _merge_ml_fields(self, existing: list, product) -> list:
        """Aplica os campos do ML sobre a linha existente, preservando o resto."""
        row = list(existing)
        row[self._COL_NAME] = product.name
        row[self._COL_PRICE] = product.price
        row[self._COL_QTY] = product.stock_qty
        if product.ean:                       # só sobrescreve EAN se o ML tiver
            row[self._COL_EAN] = product.ean
        if product.ncm:                       # dados fiscais: só sobrescreve se o ML tiver
            row[self._COL_NCM] = product.ncm
        if product.cest:
            row[self._COL_CEST] = product.cest
        if product.origin:
            row[self._COL_ORIGIN] = product.origin
        if product.sku_is_real:               # SKU real do ML sempre prevalece
            row[self._COL_SKU] = product.sku
        elif not row[self._COL_SKU]:          # senão, só preenche se estiver vazio
            row[self._COL_SKU] = product.sku  # (fallback p/ MLB)
        row[self._COL_MLB] = product.mlb_id
        return row

    def upsert_product(self, tab: str, product, products_map: dict, next_id: int) -> str:
        """Insere ou atualiza pelo MLB ID, preservando ID e campos manuais."""
        mlb_id = product.mlb_id
        if mlb_id and mlb_id in products_map:
            row_num, existing = products_map[mlb_id]
            merged = self._merge_ml_fields(existing, product)
            self._update(f"'{tab}'!A{row_num}:U{row_num}", [merged])
            return "updated"
        self._append(f"'{tab}'!A:U", [product.to_sheet_row(next_id)])
        return "inserted"

    # ── Endereçamento ────────────────────────────────────────────────────────

    def get_addresses(self) -> dict[str, str]:
        """Retorna {sku: 'Corredor - Estante - Prateleira'}."""
        rows = self._read(f"'Endereçamento'!A{ENDERECAMENTO_DATA_START_ROW}:D")
        result = {}
        for row in rows:
            if len(row) >= 1 and row[0]:
                parts = row[1:4]
                result[row[0]] = " - ".join(str(p) for p in parts if p not in (None, ""))
        return result

    def get_addresses_full(self, busca: str = "") -> list[dict]:
        """Retorna [{"sku","corredor","estante","prateleira"}] — pros campos editáveis
        da tela de cadastro (evita perder estrutura ao juntar/separar por " - ").
        `busca`, se informado, restringe aos SKUs que contêm o texto (case-insensitive)."""
        rows = self._read(f"'Endereçamento'!A{ENDERECAMENTO_DATA_START_ROW}:D")
        out = []
        busca = (busca or "").strip().lower()
        for row in rows:
            p = (list(row) + [""] * 4)[:4]
            if p[0] and (not busca or busca in p[0].lower()):
                out.append({"sku": p[0], "corredor": p[1], "estante": p[2], "prateleira": p[3]})
        return out

    def ensure_addresses_for_skus(self, skus: list[str]) -> int:
        """Garante que todo SKU único apareça na aba Endereçamento (endereço em branco
        para o usuário preencher). Retorna quantos SKUs novos foram adicionados."""
        rows = self._read(f"'Endereçamento'!A{ENDERECAMENTO_DATA_START_ROW}:A")
        existing = {r[0] for r in rows if r and r[0]}
        novos = [s for s in dict.fromkeys(skus) if s and s not in existing]
        if not novos:
            return 0
        next_row = ENDERECAMENTO_DATA_START_ROW + len(rows)
        self._update(
            f"'Endereçamento'!A{next_row}:A{next_row + len(novos) - 1}",
            [[s] for s in novos],
        )
        return len(novos)

    def set_address_for_sku(self, sku: str, corredor: str, estante: str, prateleira: str) -> bool:
        """Grava o endereço (corredor/estante/prateleira) de um SKU.

        Atualiza a linha existente, ou cria uma nova ao final se o SKU ainda não
        estiver cadastrado. Retorna True se criou uma linha nova, False se atualizou.
        """
        rows = self._read(f"'Endereçamento'!A{ENDERECAMENTO_DATA_START_ROW}:A")
        for i, r in enumerate(rows):
            if r and r[0] == sku:
                row_num = i + ENDERECAMENTO_DATA_START_ROW
                self._update(f"'Endereçamento'!B{row_num}:D{row_num}", [[corredor, estante, prateleira]])
                return False
        next_row = ENDERECAMENTO_DATA_START_ROW + len(rows)
        self._update(f"'Endereçamento'!A{next_row}:D{next_row}", [[sku, corredor, estante, prateleira]])
        return True

    def remove_mlb_addresses(self) -> int:
        """Remove do Endereçamento as linhas cujo SKU é um MLB de fallback (anúncio sem SKU)."""
        sid = self._get_sheet_id("Endereçamento")
        if sid is None:
            return 0
        rows = self._read(f"'Endereçamento'!A{ENDERECAMENTO_DATA_START_ROW}:A")
        a_apagar = [i + ENDERECAMENTO_DATA_START_ROW for i, r in enumerate(rows)
                    if r and re.fullmatch(r"MLB\d+", str(r[0]).strip())]
        if not a_apagar:
            return 0
        requests = [{"deleteDimension": {"range": {
            "sheetId": sid, "dimension": "ROWS", "startIndex": rn - 1, "endIndex": rn}}}
            for rn in sorted(a_apagar, reverse=True)]
        self._svc.batchUpdate(spreadsheetId=self.sheet_id, body={"requests": requests}).execute()
        return len(a_apagar)

    # ── Vendas ───────────────────────────────────────────────────────────────
    # Colunas: A=Empresa, B=SKU, C=Nome, D=Quantidade, E=Data limite, F=Endereço
    #          G=order_id (OCULTA, dedup), H=Status (workflow), I=Estado (ok)
    #          J=NF nº/série, K=Valor NF, L=CFOP, M=Chave de acesso,
    #          N=Comprador, O=CPF/CNPJ, P=Nº venda (ML)  (bate com o painel do ML)
    #          Q=ID Expedição (PLG+DDMMAA+seq, por venda), R=Gaiola (1..4, na embalagem)
    #          S=Impresso em (data/hora que a etiqueta de separação foi confirmada)
    _FISCAL_HEADERS = ["NF nº/série", "Valor NF", "CFOP", "Chave de acesso",
                       "Comprador", "CPF/CNPJ"]
    _VENDAS_EXTRA_HEADERS = ["Nº venda (ML)", "ID Expedição", "Gaiola", "Impresso em"]
    _COL_ORDER_ID = 6           # índice 0-based da coluna G (order_id)
    _COL_STATUS = 7             # coluna H (Status: Separando/Separado/Embalado/Enviado)
    _COL_ESTADO = 8             # coluna I (Estado: "ok" quando fiscal processado)
    _COL_VENDA_ML = 15          # coluna P (nº de venda/pack do painel do ML)
    _COL_ID_EXPED = 16          # coluna Q (ID de expedição / etiqueta física)
    _COL_GAIOLA = 17            # coluna R (nº da gaiola, definido na embalagem)
    _COL_IMPRESSO_EM = 18       # coluna S (data/hora da confirmação da etiqueta de separação)
    _VENDAS_COLS = 19           # A..S
    GAIOLAS = ["Gaiola 1", "Gaiola 2", "Gaiola 3", "Gaiola 4"]
    AGUARDANDO_BOX = "Aguardando box"

    def get_sale_order_ids(self) -> set[str]:
        rows = self._read(f"'Vendas'!G{VENDAS_DATA_START_ROW}:G")
        return {r[0] for r in rows if r}

    def ensure_vendas_fiscal_columns(self) -> None:
        """Garante que a aba Vendas tenha as colunas J..S (grade + cabeçalho)."""
        meta = self._svc.get(spreadsheetId=self.sheet_id).execute()
        sid = cols = None
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == "Vendas":
                sid = s["properties"]["sheetId"]
                cols = s["properties"].get("gridProperties", {}).get("columnCount", 0)
                break
        if sid is None:
            return
        if cols < self._VENDAS_COLS:
            self._svc.batchUpdate(spreadsheetId=self.sheet_id, body={"requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": sid,
                                   "gridProperties": {"columnCount": self._VENDAS_COLS}},
                    "fields": "gridProperties.columnCount",
                }
            }]}).execute()
        # cabeçalho fiscal (J..O) + Nº venda/ID Expedição/Gaiola/Impresso em (P..S) na linha 2
        expected = self._FISCAL_HEADERS + self._VENDAS_EXTRA_HEADERS   # J..S
        header = self._read("'Vendas'!J2:S2")
        atual = (header[0] if header else [])
        if atual[:len(expected)] != expected:
            self._update("'Vendas'!J2:S2", [expected])

        # Colunas de número GRANDE como TEXTO (senão o Sheets vira notação científica
        # e perde precisão): G=order_id(6), P=Nº venda(15), Q=ID Expedição(16), S=Impresso em(18). 0-based.
        self._svc.batchUpdate(spreadsheetId=self.sheet_id, body={"requests": [
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 2,
                          "startColumnIndex": c, "endColumnIndex": c + 1},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                "fields": "userEnteredFormat.numberFormat"}}
            for c in (6, 15, 16, 18)
        ]}).execute()

    def next_expedition_seq(self, prefix: str) -> int:
        """Maior sequencial+1 entre os IDs de expedição que começam com `prefix`.

        prefix = sigla da loja + DDMMAA (ex.: 'PLG010726'). Reinicia por loja/dia.
        """
        rows = self._read(f"'Vendas'!Q{VENDAS_DATA_START_ROW}:Q")
        mx = 0
        for r in rows:
            v = (r[0] if r else "").strip()
            if v.startswith(prefix):
                suf = v[len(prefix):]
                if suf.isdigit():
                    mx = max(mx, int(suf))
        return mx + 1

    def make_expedition_id(self, sigla: str, ddmmaa: str) -> str:
        """Monta o ID de expedição: sigla + DDMMAA + sequencial 3 dígitos (por loja/dia)."""
        prefix = f"{sigla}{ddmmaa}"
        return f"{prefix}{self.next_expedition_seq(prefix):03d}"

    def append_sale(self, row: list) -> None:
        """Escreve uma venda na próxima linha livre (A:R). row é preenchida até 18 cols."""
        existing = self._read(f"'Vendas'!B{VENDAS_DATA_START_ROW}:B")
        next_row = VENDAS_DATA_START_ROW + len(existing)
        padded = (list(row) + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
        self._update(f"'Vendas'!A{next_row}:S{next_row}", [padded])

    def backfill_venda_ml(self, mapa: dict) -> int:
        """Preenche a coluna P (Nº venda ML) das linhas cujo order_id está em `mapa`.

        mapa = {order_id: venda_ml}. Só escreve onde P está vazio. Retorna nº de linhas.
        """
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:P")
        n = 0
        for i, r in enumerate(rows):
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            order_id, venda_atual = p[6], p[15]      # G, P
            if order_id in mapa and not venda_atual:
                row_num = i + VENDAS_DATA_START_ROW
                self._update(f"'Vendas'!P{row_num}", [[mapa[order_id]]])
                n += 1
        return n

    def get_rows_missing_expedition(self) -> list[tuple[int, str, str]]:
        """Linhas com SKU mas SEM ID de expedição (col Q). Retorna [(row, order_id, venda_ml)]."""
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:R")
        out = []
        for i, r in enumerate(rows):
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            if p[1] and not p[16]:                    # tem SKU (B) e Q (índice 16) vazio
                out.append((i + VENDAS_DATA_START_ROW, p[6], p[15]))   # row, order_id(G), venda(P)
        return out

    def set_expedition_id_for_rows(self, row_nums: list[int], exped_id: str) -> None:
        for rn in row_nums:
            self._update(f"'Vendas'!Q{rn}", [[exped_id]])

    def get_expedition_id(self, numero: str) -> str:
        """ID de expedição (col Q) da venda cujo order_id (G) OU nº venda (P) == numero."""
        if not numero:
            return ""
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:R")
        for r in rows:
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            if p[6] == numero or p[15] == numero:      # G (order_id) ou P (nº venda)
                return p[16]                            # Q (ID de expedição)
        return ""

    def get_sale_order_ids_for_backfill(self) -> set[str]:
        """order_ids das linhas que ainda não têm Nº venda (ML) preenchido (col P)."""
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:P")
        faltando = set()
        for r in rows:
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            if p[6] and not p[15]:
                faltando.add(p[6])
        return faltando

    def get_sales_needing_fiscal(self) -> list[tuple[int, str, str]]:
        """Linhas de Vendas que ainda não têm NF (chave vazia) e não foram enviadas.

        Retorna [(row_num, empresa, order_id)] sem repetir order_id.
        """
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:O")
        pendentes: list[tuple[int, str, str]] = []
        vistos: set[str] = set()
        for i, r in enumerate(rows):
            padded = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            empresa, order_id = padded[0], padded[6]      # A, G
            estado, chave = padded[8], padded[12]         # I, M
            if not order_id or order_id in vistos:
                continue
            if str(estado).lower() == "ok":
                continue
            if chave:                                     # já tem NF
                continue
            vistos.add(order_id)
            pendentes.append((i + VENDAS_DATA_START_ROW, empresa, order_id))
        return pendentes

    def set_fiscal_for_order(self, order_id: str, fiscal_cols: list) -> int:
        """Preenche J..O em TODAS as linhas (itens) de um pedido. Retorna nº de linhas."""
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:O")
        n = 0
        for i, r in enumerate(rows):
            padded = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            if padded[6] == order_id:                     # coluna G
                row_num = i + VENDAS_DATA_START_ROW
                self._update(f"'Vendas'!J{row_num}:O{row_num}", [fiscal_cols])
                n += 1
        return n

    def dedupe_vendas(self) -> int:
        """Remove linhas duplicadas na aba Vendas (mesmo order_id + SKU).

        Mantém, entre as duplicatas, a linha com MAIS células preenchidas
        (preserva a que já tem NF/status). Retorna quantas linhas foram apagadas.
        """
        sid = self._get_sheet_id("Vendas")
        if sid is None:
            return 0
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:O")
        # agrupa por (order_id, sku) -> lista de (row_num, qtd_preenchidos)
        grupos: dict[tuple, list[tuple[int, int]]] = {}
        for i, r in enumerate(rows):
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            order_id, sku = p[6], p[1]            # G, B
            if not order_id or not sku:
                continue
            row_num = i + VENDAS_DATA_START_ROW
            filled = sum(1 for c in p if str(c).strip())
            grupos.setdefault((order_id, sku), []).append((row_num, filled))

        # define as linhas a apagar (todas menos a mais completa de cada grupo)
        a_apagar: list[int] = []
        for _key, ocorr in grupos.items():
            if len(ocorr) <= 1:
                continue
            ocorr.sort(key=lambda t: t[1], reverse=True)   # mais preenchida primeiro
            a_apagar.extend(rn for rn, _ in ocorr[1:])

        if not a_apagar:
            return 0
        # apaga de baixo p/ cima (para os índices não deslocarem)
        requests = [{
            "deleteDimension": {"range": {
                "sheetId": sid, "dimension": "ROWS",
                "startIndex": rn - 1, "endIndex": rn,    # 0-based
            }}
        } for rn in sorted(a_apagar, reverse=True)]
        self._svc.batchUpdate(spreadsheetId=self.sheet_id, body={"requests": requests}).execute()
        return len(a_apagar)

    # ── Mural de expedição + Gaiolas (web app) ────────────────────────────────
    # Tudo computado ao vivo a partir da Vendas — sem aba/estrutura paralela.

    def _agrupar_pedidos(self) -> dict[str, dict]:
        """Agrupa TODAS as linhas de Vendas por venda_ml/order_id (qualquer status).

        Um pedido pode ter várias linhas (vários SKUs do mesmo pack) — todas
        compartilham o mesmo ID de expedição e viram 1 pedido só. Base
        compartilhada por get_mural_pedidos/get_pedido_by_venda/get_pedido_by_exped.
        """
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:S")
        pedidos: dict[str, dict] = {}
        for i, r in enumerate(rows):
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            sku = p[1]
            if not sku:
                continue
            venda = p[self._COL_VENDA_ML] or p[self._COL_ORDER_ID]
            if not venda:
                continue
            row_num = i + VENDAS_DATA_START_ROW
            status_row = str(p[self._COL_STATUS] or "Separando").strip()
            pedido = pedidos.setdefault(venda, {
                "venda": venda,
                "order_id": p[self._COL_ORDER_ID],
                "empresa": p[0],
                "id_exped": p[self._COL_ID_EXPED],
                "data_limite": p[4],
                "status": status_row,
                "impresso_em": p[self._COL_IMPRESSO_EM],
                "gaiola": p[self._COL_GAIOLA],
                "itens": [],
            })
            if status_row.lower() == "enviado":
                # "Enviado" nunca regride: um pedido com várias linhas (SKUs) pode ter
                # ficado com status inconsistente entre linhas por edição manual na
                # planilha — se QUALQUER linha já foi enviada, o pedido inteiro some
                # das filas de "aguardando" (Mural/Embalagem/Gaiolas).
                pedido["status"] = "Enviado"
            pedido["itens"].append({
                "row": row_num, "sku": sku, "nome": p[2], "qtd": p[3], "endereco": p[5],
            })
        return pedidos

    def get_mural_pedidos(self) -> list[dict]:
        """Pedidos ainda não impressos (exclui Separado/Embalado/Enviado) — fila do mural.

        "Cancelado" fica de propósito FORA dessa exclusão: o card continua aparecendo
        (travado, em vermelho — ver _mural_card.html) pra sempre — o Status nunca muda
        de "Cancelado" (é o tipo real do pedido). Quem tira o card do Mural depois do
        Gerente finalizar é o flag `finalizado_por_gerente` no Postgres, checado em
        routers/mural.py::mural_pedidos — não um novo Status na planilha."""
        return [p for p in self._agrupar_pedidos().values()
                if p["status"].lower() not in ("separado", "embalado", "enviado")]

    def get_all_pedidos(self, status_filter: list[str] | None = None, busca: str = "",
                         loja: str = "", dia_de: str = "", dia_ate: str = "") -> list[dict]:
        """Todos os pedidos (qualquer status) — tela de auditoria do Gerente.

        `status_filter`, se informado e não vazio, restringe aos status listados
        (comparação case-insensitive). `busca`, se informado, restringe a pedidos cuja
        venda/order_id/empresa/ID de expedição/algum SKU contenha o texto (case-insensitive).
        `loja`, se informado, restringe à empresa exata (case-insensitive). `dia_de`/`dia_ate`,
        se informados (formato ISO "AAAA-MM-DD", o que um <input type="date"> manda),
        restringem por intervalo de data limite — qualquer um dos dois pode vir vazio
        (intervalo aberto de um lado só).
        """
        pedidos = list(self._agrupar_pedidos().values())
        if status_filter:
            alvo = {s.strip().lower() for s in status_filter}
            pedidos = [p for p in pedidos if p["status"].strip().lower() in alvo]
        if loja:
            pedidos = [p for p in pedidos if (p["empresa"] or "").strip().lower() == loja.strip().lower()]
        if dia_de or dia_ate:
            de = date.fromisoformat(dia_de) if dia_de else None
            ate = date.fromisoformat(dia_ate) if dia_ate else None

            def _no_intervalo(p: dict) -> bool:
                d = _parse_data_br(p.get("data_limite"))
                if d is None:
                    return False
                if de and d < de:
                    return False
                if ate and d > ate:
                    return False
                return True

            pedidos = [p for p in pedidos if _no_intervalo(p)]
        busca = (busca or "").strip().lower()
        if busca:
            def _match(p: dict) -> bool:
                campos = [p["venda"], p.get("order_id", ""), p["empresa"], p["id_exped"]]
                campos += [item["sku"] for item in p["itens"]]
                return any(busca in str(c).lower() for c in campos if c)
            pedidos = [p for p in pedidos if _match(p)]
        return pedidos

    def get_pedido_by_venda(self, venda_key: str) -> dict | None:
        """Pedido pelo venda_ml/order_id, independente do status (p/ imprimir a
        etiqueta de separação mesmo que já tenha avançado no fluxo)."""
        return self._agrupar_pedidos().get(venda_key)

    def get_pedido_by_order_id(self, order_id: str) -> dict | None:
        """Pedido pelo order_id bruto do ML — diferente de `get_pedido_by_venda`
        porque a chave do dict é o venda_ml (pack_id quando existe), que pode não
        bater com o order_id de uma notificação de webhook específica."""
        for pedido in self._agrupar_pedidos().values():
            if pedido.get("order_id") == order_id:
                return pedido
        return None

    def get_pedido_by_exped(self, exped_id: str) -> dict | None:
        """Pedido pelo ID de expedição (o que vem no código de barras bipado na
        embalagem/gaiolas). Comparação case-insensitive — alguns leitores de código
        de barras invertem maiúscula/minúscula (config de Caps Lock do teclado
        emulado), então não dá pra confiar na caixa exata do que foi lido."""
        alvo = (exped_id or "").strip().upper()
        for pedido in self._agrupar_pedidos().values():
            if pedido["id_exped"].strip().upper() == alvo:
                return pedido
        return None

    def set_status_for_venda(self, venda_key: str, status: str, estado: str | None = None,
                              impresso_em: str | None = None) -> int:
        """Grava Status (H, e Estado/I se informado) em todas as linhas do pedido.

        Casa por venda_ml (P) == venda_key, ou por order_id (G) == venda_key
        quando a linha não tem venda_ml (mesma chave usada no agrupamento do mural).
        `impresso_em`, se informado, grava também a coluna S (data/hora da confirmação
        da impressão da etiqueta de separação).
        """
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:R")
        n = 0
        for i, r in enumerate(rows):
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            venda = p[self._COL_VENDA_ML] or p[self._COL_ORDER_ID]
            if venda != venda_key:
                continue
            row_num = i + VENDAS_DATA_START_ROW
            if estado is not None:
                self._update(f"'Vendas'!H{row_num}:I{row_num}", [[status, estado]])
            else:
                self._update(f"'Vendas'!H{row_num}", [[status]])
            if impresso_em is not None:
                self._update(f"'Vendas'!S{row_num}", [[impresso_em]])
            n += 1
        return n

    def get_gaiolas_estado(self, gaiolas: list[str] | None = None) -> dict[str, list[dict]]:
        """Pacotes embalados (Status=="Embalado"), agrupados por ID de Expedição (Q)
        e organizados por zona: "Aguardando box" (sem gaiola) + cada gaiola.
        """
        gaiolas = gaiolas or self.GAIOLAS
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:R")

        # Um pedido com várias linhas (SKUs) pode ter ficado com status inconsistente
        # entre linhas por edição manual na planilha — se QUALQUER linha do mesmo ID de
        # expedição já foi enviada, o pedido inteiro some daqui, mesmo que outra linha
        # ainda diga "Embalado" (senão fica preso pra sempre em "Aguardando box").
        enviados: set[str] = set()
        for r in rows:
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            if str(p[self._COL_STATUS] or "").strip().lower() == "enviado" and p[self._COL_ID_EXPED]:
                enviados.add(p[self._COL_ID_EXPED])

        pacotes: dict[str, dict] = {}
        for r in rows:
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            if not p[1]:
                continue
            status = str(p[self._COL_STATUS] or "").strip().lower()
            if status != "embalado":
                continue
            exped_id = p[self._COL_ID_EXPED]
            if not exped_id or exped_id in enviados:
                continue
            pac = pacotes.setdefault(exped_id, {
                "id_exped": exped_id,
                "venda": p[self._COL_VENDA_ML] or p[self._COL_ORDER_ID],
                "empresa": p[0],
                "gaiola": p[self._COL_GAIOLA],
                "n_sku": 0,
            })
            pac["n_sku"] += 1

        estado: dict[str, list[dict]] = {self.AGUARDANDO_BOX: [], **{g: [] for g in gaiolas}}
        for pac in pacotes.values():
            zona = pac["gaiola"] if pac["gaiola"] in gaiolas else self.AGUARDANDO_BOX
            estado[zona].append(pac)
        return estado

    def mover_para_gaiola(self, id_exped: str, gaiola: str) -> dict:
        """Bipagem: acha as linhas Embalado com esse ID de expedição e grava a Gaiola (R).
        Comparação case-insensitive (ver get_pedido_by_exped) — leitor pode inverter caixa."""
        alvo = (id_exped or "").strip().upper()
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:R")
        matched: list[int] = []
        venda = empresa = ""
        for i, r in enumerate(rows):
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            status = str(p[self._COL_STATUS] or "").strip().lower()
            if p[self._COL_ID_EXPED].strip().upper() == alvo and status == "embalado":
                matched.append(i + VENDAS_DATA_START_ROW)
                if not venda:
                    venda = p[self._COL_VENDA_ML] or p[self._COL_ORDER_ID]
                    empresa = p[0]
        if not matched:
            return {"ok": False, "msg": f'ID "{id_exped}" não encontrado (aguardando embalagem).'}
        for row_num in matched:
            self._update(f"'Vendas'!R{row_num}", [[gaiola]])
        return {"ok": True, "venda": venda, "empresa": empresa, "n_sku": len(matched)}

    def remover_da_gaiola(self, venda_key: str) -> dict:
        """Limpa a Gaiola (R) de um pedido — usado pelo Gerente pra corrigir uma
        bipagem errada (ex.: item posto na gaiola errada). O pedido volta a aparecer
        em "Aguardando box" em /gaiolas, sem mudar o Status.

        Recusa se o pedido já foi Enviado — depois de enviado a gaiola é só
        histórico/auditoria, não faz sentido alterar.
        """
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:R")
        matched: list[int] = []
        for i, r in enumerate(rows):
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            venda = p[self._COL_VENDA_ML] or p[self._COL_ORDER_ID]
            if venda != venda_key:
                continue
            status = str(p[self._COL_STATUS] or "").strip().lower()
            if status == "enviado":
                return {"ok": False, "msg": "Pedido já foi enviado — não é possível alterar a gaiola."}
            matched.append(i + VENDAS_DATA_START_ROW)
        if not matched:
            return {"ok": False, "msg": f'Pedido "{venda_key}" não encontrado.'}
        for row_num in matched:
            self._update(f"'Vendas'!R{row_num}", [[""]])
        return {"ok": True, "n": len(matched)}

    def coletar_gaiola(self, gaiola: str) -> dict:
        """Marca Enviado+ok todas as linhas Embalado que estão na gaiola informada."""
        rows = self._read(f"'Vendas'!A{VENDAS_DATA_START_ROW}:R")
        matched: list[int] = []
        expeds: set[str] = set()
        for i, r in enumerate(rows):
            p = (r + [""] * self._VENDAS_COLS)[:self._VENDAS_COLS]
            status = str(p[self._COL_STATUS] or "").strip().lower()
            if status == "embalado" and p[self._COL_GAIOLA] == gaiola:
                matched.append(i + VENDAS_DATA_START_ROW)
                expeds.add(p[self._COL_ID_EXPED])
        for row_num in matched:
            self._update(f"'Vendas'!H{row_num}:I{row_num}", [["Enviado", "ok"]])
        return {"n_pacotes": len(expeds), "n_linhas": len(matched)}

    # ── Aba Fiscal (preenchida pelo time → enviada ao ML) ─────────────────────
    # Colunas: A=SKU, B=Produto, C=NCM, D=Origem, E=CEST, F=EAN,
    #          G=Loja (OCULTA), H=Status, I=Última sync
    FISCAL_TAB = "Fiscal"
    _FISCAL_TAB_HEADERS = ["SKU", "Produto", "NCM", "Origem", "CEST", "EAN",
                           "Lojas", "Status", "Última sync"]
    _FISCAL_TAB_COLS = 9
    _ORIGEM_OPTIONS = [
        "0 - Nacional", "1 - Estrangeira (importação direta)",
        "2 - Estrangeira (mercado interno)", "3 - Nacional (importação 40%-70%)",
        "4 - Nacional (processos produtivos básicos)", "5 - Nacional (importação ≤40%)",
        "6 - Estrangeira (importação direta, sem similar nacional)",
        "7 - Estrangeira (mercado interno, sem similar nacional)",
        "8 - Nacional (importação >70%)",
    ]

    def ensure_fiscal_tab(self) -> None:
        """Cria a aba Fiscal (título, cabeçalho, coluna Loja oculta, dropdown de Origem)."""
        sid = self._get_sheet_id(self.FISCAL_TAB)
        if sid is None:
            self._svc.batchUpdate(spreadsheetId=self.sheet_id, body={"requests": [
                {"addSheet": {"properties": {"title": self.FISCAL_TAB,
                                             "gridProperties": {"columnCount": self._FISCAL_TAB_COLS}}}}
            ]}).execute()
            sid = self._get_sheet_id(self.FISCAL_TAB)
            self._update(f"'{self.FISCAL_TAB}'!A1", [["DADOS FISCAIS – ENVIO AO MERCADO LIVRE"]])
            self._update(f"'{self.FISCAL_TAB}'!A2:I2", [self._FISCAL_TAB_HEADERS])
            self._svc.batchUpdate(spreadsheetId=self.sheet_id, body={"requests": [
                # negrito no cabeçalho
                {"repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold"}},
                # oculta a coluna Loja (G = índice 6)
                {"updateDimensionProperties": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS",
                              "startIndex": 6, "endIndex": 7},
                    "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
                # dropdown de Origem na coluna D (índice 3)
                {"setDataValidation": {
                    "range": {"sheetId": sid, "startRowIndex": 2, "startColumnIndex": 3, "endColumnIndex": 4},
                    "rule": {
                        "condition": {"type": "ONE_OF_LIST",
                                      "values": [{"userEnteredValue": o} for o in self._ORIGEM_OPTIONS]},
                        "showCustomUi": True, "strict": False}}},
                {"updateSheetProperties": {
                    "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 2}},
                    "fields": "gridProperties.frozenRowCount"}},
            ]}).execute()

    def sync_fiscal_tab(self, items: list[tuple[str, str, str, dict]]) -> int:
        """Sincroniza a aba Fiscal. items = [(sku, nome, loja, fiscal_dict)].

        - SKU novo: entra já PRÉ-PREENCHIDO com o que o ML tem (NCM/Origem/CEST/EAN);
          se o ML não tem, entra vazio.
        - SKU existente: completa apenas as células VAZIAS (nunca sobrescreve o que o
          time digitou). Retorna o nº de SKUs novos adicionados.
        """
        self.ensure_fiscal_tab()
        rows = self._read(f"'{self.FISCAL_TAB}'!A3:I")
        index: dict[str, tuple[int, list]] = {}
        for i, r in enumerate(rows):
            p = (r + [""] * self._FISCAL_TAB_COLS)[:self._FISCAL_TAB_COLS]
            if p[0]:
                index[p[0]] = (i + 3, p)

        novos = []
        vistos = set()
        for sku, nome, loja, fis in items:
            if not sku or sku in vistos:
                continue
            vistos.add(sku)
            fis = fis or {}
            ncm, origem = fis.get("ncm", ""), fis.get("origin", "")
            cest, ean = fis.get("cest", ""), fis.get("ean", "")
            if sku not in index:
                novos.append([sku, nome, ncm, origem, cest, ean, loja, "", ""])
                continue
            # SKU já existe: completa só o que estiver vazio
            row_num, p = index[sku]
            novo = [p[2] or ncm, p[3] or origem, p[4] or cest, p[5] or ean]
            if novo != [p[2], p[3], p[4], p[5]]:
                self._update(f"'{self.FISCAL_TAB}'!C{row_num}:F{row_num}", [novo])
            if not p[1] and nome:
                self._update(f"'{self.FISCAL_TAB}'!B{row_num}", [[nome]])
            # Acumula TODAS as lojas que têm esse SKU (envio vai pra todas)
            lojas_atual = [x.strip() for x in str(p[6]).split(",") if x.strip()]
            if loja and loja not in lojas_atual:
                lojas_atual.append(loja)
                self._update(f"'{self.FISCAL_TAB}'!G{row_num}", [[", ".join(lojas_atual)]])
                p[6] = ", ".join(lojas_atual)   # mantém o índice coerente neste run

        if novos:
            next_row = 3 + len(rows)
            self._update(f"'{self.FISCAL_TAB}'!A{next_row}:I{next_row + len(novos) - 1}", novos)
        return len(novos)

    def get_fiscal_rows(self) -> list[dict]:
        """Lê as linhas da aba Fiscal para envio. Retorna dicts com row_num e campos."""
        if self._get_sheet_id(self.FISCAL_TAB) is None:
            return []
        rows = self._read(f"'{self.FISCAL_TAB}'!A3:I")
        out = []
        for i, r in enumerate(rows):
            p = (r + [""] * self._FISCAL_TAB_COLS)[:self._FISCAL_TAB_COLS]
            if not p[0]:
                continue
            out.append({"row_num": i + 3, "sku": p[0], "nome": p[1], "ncm": p[2],
                        "origem": p[3], "cest": p[4], "ean": p[5],
                        "lojas": [x.strip() for x in str(p[6]).split(",") if x.strip()]})
        return out

    def set_fiscal_status(self, row_num: int, status: str, when: str) -> None:
        self._update(f"'{self.FISCAL_TAB}'!H{row_num}:I{row_num}", [[status, when]])

    # ── Tema visual unificado (UX coeso entre as abas) ────────────────────────
    _TH_TITLE_BG = {"red": 0.102, "green": 0.451, "blue": 0.910}   # #1a73e8
    _TH_TITLE_FG = {"red": 1, "green": 1, "blue": 1}
    _TH_HEADER_BG = {"red": 0.910, "green": 0.933, "blue": 0.969}  # #e8eef7
    _TH_BAND_BG = {"red": 0.953, "green": 0.969, "blue": 0.988}    # #f3f7fc

    def _theme_tab(self, tab: str, title: str, headers: list[str] | None,
                   widths: list[int] | None = None, hidden_cols: tuple = (),
                   header_row: int = 2) -> None:
        """Aplica um visual consistente: faixa de título, cabeçalho, congelamento,
        larguras, listras (zebra) e colunas ocultas. Não toca nos dados."""
        sid = self._get_sheet_id(tab)
        if sid is None:
            return
        n = len(headers) if headers else (len(widths) if widths else 1)
        if title:
            self._update(f"'{tab}'!A1", [[title]])
        if headers:
            last = chr(ord("A") + len(headers) - 1)
            self._update(f"'{tab}'!A{header_row}:{last}{header_row}", [headers])

        reqs = [
            # descongela colunas (senão a mesclagem do título cruza fronteira e dá erro)
            {"updateSheetProperties": {"properties": {"sheetId": sid,
                                                      "gridProperties": {"frozenColumnCount": 0}},
                                       "fields": "gridProperties.frozenColumnCount"}},
            # desfaz qualquer mesclagem antiga na linha 1 antes de remesclar
            {"unmergeCells": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                                        "startColumnIndex": 0, "endColumnIndex": n}}},
            {"mergeCells": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                                       "startColumnIndex": 0, "endColumnIndex": n},
                            "mergeType": "MERGE_ALL"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {
                                "backgroundColor": self._TH_TITLE_BG,
                                "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
                                "textFormat": {"foregroundColor": self._TH_TITLE_FG,
                                               "bold": True, "fontSize": 13}}},
                            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"}},
            {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS",
                                                     "startIndex": 0, "endIndex": 1},
                                           "properties": {"pixelSize": 40}, "fields": "pixelSize"}},
            {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": header_row - 1, "endRowIndex": header_row},
                            "cell": {"userEnteredFormat": {
                                "backgroundColor": self._TH_HEADER_BG, "verticalAlignment": "MIDDLE",
                                "textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat(backgroundColor,verticalAlignment,textFormat)"}},
            {"updateSheetProperties": {"properties": {"sheetId": sid,
                                                      "gridProperties": {"frozenRowCount": header_row}},
                                       "fields": "gridProperties.frozenRowCount"}},
        ]
        for idx, w in enumerate(widths or []):
            if w:
                reqs.append({"updateDimensionProperties": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": idx, "endIndex": idx + 1},
                    "properties": {"pixelSize": w}, "fields": "pixelSize"}})
        for c in hidden_cols:
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c, "endIndex": c + 1},
                "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
        self._svc.batchUpdate(spreadsheetId=self.sheet_id, body={"requests": reqs}).execute()

        # Listras (zebra) na área de dados — em lote separado (ignora se já existir)
        try:
            self._svc.batchUpdate(spreadsheetId=self.sheet_id, body={"requests": [{
                "addBanding": {"bandedRange": {
                    "range": {"sheetId": sid, "startRowIndex": header_row,
                              "startColumnIndex": 0, "endColumnIndex": n},
                    "rowProperties": {"firstBandColor": {"red": 1, "green": 1, "blue": 1},
                                      "secondBandColor": self._TH_BAND_BG}}}}]}).execute()
        except Exception:
            pass

    def apply_layout(self) -> dict:
        """Aplica o tema visual unificado às abas Vendas, Endereçamento e Fiscal."""
        out = {}
        if self._get_sheet_id("Vendas") is not None:
            self.ensure_vendas_fiscal_columns()
            self._theme_tab(
                "Vendas", "🛒 VENDAS",
                ["Empresa", "SKU", "Nome do Produto", "Quantidade", "Data limite",
                 "Endereço", "Pedido", "Status", "Estado", "NF nº/série", "Valor NF",
                 "CFOP", "Chave de acesso", "Comprador", "CPF/CNPJ", "Nº venda (ML)",
                 "ID Expedição", "Gaiola", "Impresso em"],
                widths=[110, 120, 280, 90, 95, 230, 0, 110, 75, 95, 90, 70, 300, 200, 140, 150,
                        140, 80, 140],
                hidden_cols=(6,))
            out["vendas"] = "ok"
        if self._get_sheet_id("Endereçamento") is not None:
            self._theme_tab(
                "Endereçamento", "📍 ENDEREÇAMENTO",
                ["SKU", "Corredor", "Estante", "Prateleira"],
                widths=[150, 120, 100, 100])
            out["enderecamento"] = "ok"
        self.ensure_fiscal_tab()
        self._theme_tab(
            self.FISCAL_TAB, "🧾 DADOS FISCAIS – ENVIO AO MERCADO LIVRE",
            self._FISCAL_TAB_HEADERS,
            widths=[130, 280, 110, 230, 110, 150, 0, 210, 140],
            hidden_cols=(6,))
        out["fiscal"] = "ok"
        return out
