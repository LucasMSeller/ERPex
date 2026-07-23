from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderItem:
    company_key: str
    sku: str
    product_name: str
    quantity: int
    order_id: str
    deadline: str = ""
    address: str = ""         # preenchido via lookup na aba Endereçamento
    account_name: str = ""    # nome da conta ML (nickname) → coluna "Empresa"

    SHEET_COLUMNS = ["Empresa", "SKU", "Nome do Produto", "Quantidade", "Data limite", "Endereço"]

    def to_sheet_row(self) -> list:
        empresa = self.account_name or self.company_key.replace("_", " ").title()
        return [
            empresa,
            self.sku,
            self.product_name,
            self.quantity,
            self.deadline,
            self.address,
        ]

    @classmethod
    def from_meli_order(cls, order: dict, company_key: str, account_name: str = "",
                        deadline: str = "") -> list["OrderItem"]:
        items = []
        order_id = str(order.get("id", ""))

        for item in order.get("order_items", []):
            ml_item = item.get("item", {})
            attributes = {a["id"]: a.get("value_name", "") for a in ml_item.get("attributes", [])}
            # No pedido, o SKU costuma vir em seller_sku / seller_custom_field
            sku = (
                ml_item.get("seller_sku")
                or ml_item.get("seller_custom_field")
                or attributes.get("SELLER_SKU")
                or ml_item.get("id", "")
            )
            items.append(cls(
                company_key=company_key,
                sku=sku,
                product_name=ml_item.get("title", ""),
                quantity=item.get("quantity", 1),
                order_id=order_id,
                deadline=deadline,
                account_name=account_name,
            ))
        return items
