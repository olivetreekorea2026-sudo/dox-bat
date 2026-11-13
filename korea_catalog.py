"""DOX Korea product catalogue. Prices are for the single fixed-price set, not items."""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Model:
    code: str
    name: str
    category: str
    diopters: tuple[str, ...]
    info: str = ""


def _range(first: str, last: str, step: str) -> list[str]:
    first_d, last_d, step_d = Decimal(first), Decimal(last), Decimal(step)
    assert first_d >= last_d and step_d > 0
    out = []
    value = first_d
    while value >= last_d:
        out.append(f"{value:.2f}")
        value -= step_d
    return out


def _powers(*chunks, zero=True, extra=()):
    nums = {f"{Decimal(x):.2f}" for a, b, step in chunks for x in _range(a, b, step)}
    nums.update(f"{Decimal(x):.2f}" for x in extra)
    if zero:
        nums.add("0.00")
    return tuple(sorted(nums, key=lambda x: Decimal(x), reverse=True))


Z = ("0.00",)
P025 = _powers(("-0.50", "-5.00", "0.25"))
P050 = _powers(("-1.00", "-5.00", "0.50"))
P025_LONG = _powers(("-0.50", "-5.00", "0.25"), ("-5.50", "-10.00", "0.50"))
P050_LONG = _powers(("-1.00", "-5.00", "0.50"), ("-5.50", "-10.00", "0.50"))
P025_3_050 = _powers(("-0.50", "-3.00", "0.25"), ("-3.50", "-5.00", "0.50"))
P_CLEAR = _powers(("-0.50", "-20.00", "0.25"), zero=False)  # range only; confirm steps with manager

CATEGORIES = {
    "blue": ("🔵 Синие и голубые", "catalog_blue.jpg"),
    "brown": ("🟤 Коричневые и шоколадные", "catalog_brown.jpg"),
    "gray": ("⚪ Серые", "catalog_gray.jpg"),
    "green": ("🟢 Зелёные", "catalog_green.jpg"),
    "violet": ("🟣 Фиолетовые", "catalog_violet.jpg"),
    "special": ("✨ Прозрачные и специальные", "catalog_special.jpg"),
    "black": ("⚫ Чёрные", "catalog_black.jpg"),
}

# Names / diopter ranges transcribed from the seven catalog photos provided by the seller.
# Availability is *not* guaranteed: only store stock can confirm actual inventory.
_RAW = [
    ("FROST BLUE", "blue", P025_LONG, ""),
    ("CRYSTAL BLUE", "blue", P025, ""),
    ("UMI BLUE", "blue", _powers(("-0.50", "-5.00", "0.25"), extra=("-5.50", "-6.00", "-7.00")), ""),
    ("NOVA BLUE", "blue", _powers(("-0.50", "-5.00", "0.50")), ""),
    ("MIRA BLUE", "blue", P050, ""),
    ("MARINE BLUE", "blue", P050, ""),
    ("ICE BLUE", "blue", _powers(("-0.50", "-5.00", "0.25"), extra=("-5.50", "-6.00", "-6.50", "-7.00", "-7.50", "-8.00", "-9.00", "-10.00")), ""),
    ("BF02 BLUE", "blue", P025, ""),
    ("TOFFEE BLUE", "blue", P050, ""),
    ("AURA BLUE", "blue", P050, "С фиксированным бликом"),
    ("NEO BLUE-1", "blue", Z, "Только 0.00"),
    ("BT2 BROWN", "brown", _powers(extra=("-1.00", "-1.50", "-2.00", "-2.50", "-3.00", "-6.00", "-6.50", "-7.00", "-7.50", "-8.00")), ""),
    ("MD11 CHOCO", "brown", P025_LONG, ""),
    ("SORA CHOCO", "brown", P050, ""),
    ("KIKO HAZEL", "brown", P050, ""),
    ("K2020 CHOCO", "brown", _powers(("-0.50", "-5.00", "0.50")), ""),
    ("K2012 BROWN", "brown", _powers(("-1.00", "-4.00", "0.25")), ""),
    ("NATURAL BROWN", "brown", P050, ""),
    ("K2010 BROWN", "brown", P050, ""),
    ("ICE BROWN", "brown", P050, ""),
    ("HONEY BROWN", "brown", P050, ""),
    ("LILO BROWN", "brown", P025, ""),
    ("RIO BROWN", "brown", P050, ""),
    ("TOFFEE BROWN", "brown", P050, ""),
    ("AURA CHOCO", "brown", Z, "Только 0.00, с фиксированным бликом"),
    ("AURA BROWN", "brown", Z, "Только 0.00, с фиксированным бликом"),
    ("JS22 GRAY", "gray", P025_LONG, ""),
    ("BT2 GRAY", "gray", P025, ""),
    ("INK GRAY", "gray", P025, ""),
    ("NOVA GRAY", "gray", P025_3_050, ""),
    ("MIRA GRAY", "gray", P050, ""),
    ("AURORA GRAY", "gray", P050, ""),
    ("K2010 GRAY", "gray", P050, ""),
    ("ICE GRAY", "gray", _powers(("-1.00", "-5.00", "0.25")), ""),
    ("AURA GRAY", "gray", Z, "Только 0.00, с фиксированным бликом"),
    ("NOVA GREEN", "green", _powers(("-0.50", "-5.00", "0.50")), ""),
    ("VERA GREEN", "green", P050, ""),
    ("MIRA GREEN", "green", P050, ""),
    ("K2010 GREEN", "green", P050, ""),
    ("ICE GREEN", "green", _powers(("-1.00", "-5.00", "0.25")), ""),
    ("LILO GREEN", "green", P050, ""),
    ("MOON GREEN", "green", P025, ""),
    ("MOSS GREEN", "green", P025, ""),
    ("MINT GREEN", "green", P025, ""),
    ("SOFT GREEN", "green", P050, ""),
    ("JELLY GREEN", "green", Z, "Только 0.00"),
    ("AURA OLIVE", "green", P050, "С фиксированным бликом"),
    ("LILO VIOLET", "violet", P025, ""),
    ("DREAM VIOLET", "violet", Z, "Только 0.00"),
    ("NEO VIOLET-1", "violet", Z, "Только 0.00"),
    ("NEO YELLOW-1", "special", Z, "Только 0.00"),
    ("IRIS03", "special", Z, "Косметические линзы, только 0.00"),
    ("IRIS05", "special", Z, "Косметические линзы, только 0.00"),
    ("SCLERA 17.0", "special", Z, "Только 0.00"),
    ("NEO RED-1", "special", Z, "Только 0.00"),
    ("SCLERA 22.0", "special", Z, "Только 0.00"),
    ("ПРОЗРАЧНЫЕ", "special", P_CLEAR, "От -0.50 до -20.00; точный шаг и наличие согласует менеджер"),
    ("A106 BLACK", "black", P025_LONG, ""),
    ("MD11 BLACK", "black", P050, ""),
    ("MIST BLACK", "black", P025, ""),
    ("RING BLACK", "black", P025_3_050, ""),
    ("JS96 BLACK", "black", P050, ""),
]
MODELS = [Model(f"{i:02d}", n, cat, powers, note) for i, (n, cat, powers, note) in enumerate(_RAW, 1)]
BY_CODE = {m.code: m for m in MODELS}
BY_CATEGORY = {k: [m for m in MODELS if m.category == k] for k in CATEGORIES}
SET_PRICE_KRW = 100_000
REQUIRED_PAIRS = 3


def normalize_diopter(raw: str, model: Model) -> str | None:
    try:
        value = Decimal(raw.strip().replace(",", ".").replace("−", "-").replace("+", ""))
        if not value.is_finite():
            return None
        formatted = f"{value:.2f}"
        if value != Decimal(formatted):
            return None
        return formatted if formatted in model.diopters else None
    except (ValueError, ArithmeticError):
        return None


def diopter_hint(model: Model) -> str:
    if model.diopters == Z:
        return "Только 0.00"
    negative = model.diopters[-1]
    return f"Доступные значения: 0.00 или от {model.diopters[1]} до {negative}. Вводите, например, -2.50"
