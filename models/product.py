from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Product:
    sku: str
    name: str
    company_key: str
    mlb_id: str = ""
    variation: str = ""
    ncm: str = ""
    cest: str = ""
    ean: str = ""
    origin: str = ""
    cst: str = ""
    cfop: str = ""
    price: float = 0.0
    stock_qty: int = 0
    min_stock: int = 0
    stock_status: str = ""
    supplier: str = ""
    supplier_code: str = ""
    asin: str = ""
    shopee_id: str = ""
    registered_at: str = field(default_factory=lambda: datetime.now().strftime("%d/%m/%Y"))
    notes: str = ""
    sku_is_real: bool = False   # True se o SKU veio do ML; False se é fallback p/ MLB

    # Colunas na aba do Sheets (ordem idêntica à planilha)
    SHEET_COLUMNS = [
        "ID", "SKU", "Nome do Produto", "Variação", "NCM", "CEST",
        "EAN / GTIN", "Origem", "CST / CSOSN", "CFOP", "Preço Unitário",
        "Qtd. Estoque", "Qtd. Mínima", "Status Estoque", "Fornecedor",
        "Cód. Fornecedor", "MLB (Mercado Livre)", "ASIN (Amazon)",
        "ID Shopee", "Data de Cadastro", "Observações",
    ]

    def to_sheet_row(self, row_id: int) -> list:
        return [
            row_id, self.sku, self.name, self.variation, self.ncm,
            self.cest, self.ean, self.origin, self.cst, self.cfop,
            self.price, self.stock_qty, self.min_stock, self.stock_status,
            self.supplier, self.supplier_code, self.mlb_id,
            self.asin, self.shopee_id, self.registered_at, self.notes,
        ]

    @classmethod
    def from_meli_item(cls, item: dict, company_key: str) -> "Product":
        """Constrói um Product a partir da resposta da API do ML."""
        attributes = {a["id"]: a.get("value_name", "") for a in item.get("attributes", [])}

        # O SKU pode vir: do user-product (já resolvido em _resolved_sku),
        # de seller_custom_field, do atributo SELLER_SKU, ou da 1ª variação.
        # MLB é o último recurso.
        sku = (
            item.get("_resolved_sku")
            or item.get("seller_custom_field")
            or attributes.get("SELLER_SKU")
        )
        if not sku:
            for var in item.get("variations", []) or []:
                if var.get("seller_custom_field"):
                    sku = var["seller_custom_field"]
                    break
                var_attrs = {a["id"]: a.get("value_name", "") for a in var.get("attributes", [])}
                if var_attrs.get("SELLER_SKU"):
                    sku = var_attrs["SELLER_SKU"]
                    break
        sku_is_real = bool(sku)
        if not sku:
            sku = item.get("id", "")

        return cls(
            sku=sku,
            name=item.get("title", ""),
            company_key=company_key,
            mlb_id=item.get("id", ""),
            ean=attributes.get("GTIN", ""),
            price=float(item.get("price", 0)),
            stock_qty=item.get("available_quantity", 0),
            sku_is_real=sku_is_real,
        )
