"""Domain models for the shop."""

from dataclasses import dataclass


@dataclass
class Product:
    """A product on sale."""

    sku: str
    price: int

    @property
    def label(self) -> str:
        """Human-readable label."""
        return f"{self.sku} ({self.price})"

    @label.setter
    def label(self, value: str) -> None:
        self.sku = value

    async def reserve(
        self,
        quantity: int = 1,
    ) -> bool:
        """Reserve stock for this product."""

        def available() -> int:
            return quantity

        return available() > 0
