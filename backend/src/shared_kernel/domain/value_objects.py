"""Universal value objects. Immutable, self-validating, no identity."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Iranian mobile numbers in E.164: +989XXXXXXXXX
_IR_MOBILE_RE = re.compile(r"^\+989\d{9}$")
# Common local forms we normalize from: 09XXXXXXXXX, 9XXXXXXXXX, 00989...
_LOCAL_PREFIXES = ("0098", "098", "98", "0")

# URL slug: lowercase ascii words joined by single hyphens.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class InvalidMobileError(ValueError):
    pass


class InvalidMoneyError(ValueError):
    pass


class InvalidSlugError(ValueError):
    pass


@dataclass(frozen=True)
class Slug:
    value: str

    def __post_init__(self) -> None:
        if not _SLUG_RE.match(self.value):
            raise InvalidSlugError(
                f"invalid slug {self.value!r}: use lowercase a-z, 0-9, single hyphens"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Mobile:
    """An Iranian mobile number, always stored normalized as +989XXXXXXXXX."""

    value: str

    def __post_init__(self) -> None:
        if not _IR_MOBILE_RE.match(self.value):
            raise InvalidMobileError(f"invalid Iranian mobile: {self.value!r}")

    @classmethod
    def parse(cls, raw: str) -> "Mobile":
        digits = re.sub(r"[\s\-()]", "", raw.strip())
        if digits.startswith("+"):
            return cls(digits)
        for p in _LOCAL_PREFIXES:
            if digits.startswith(p):
                digits = digits[len(p):]
                break
        return cls("+98" + ("9" + digits if not digits.startswith("9") else digits))

    @property
    def masked(self) -> str:
        # +989123****89
        return self.value[:6] + "****" + self.value[-2:]

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Money:
    """Iranian Rial is integer-only — no minor units."""

    amount: int
    currency: str = "IRR"

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise InvalidMoneyError("amount cannot be negative")

    def __add__(self, other: "Money") -> "Money":
        self._assert_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._assert_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, qty: int) -> "Money":
        return Money(self.amount * qty, self.currency)

    def _assert_same_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise InvalidMoneyError("currency mismatch")
