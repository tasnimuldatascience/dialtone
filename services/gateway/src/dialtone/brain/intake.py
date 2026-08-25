"""What a caller has to give before an appointment can be made.

NOT HARDCODED, and that is the point. The first version had `BOOKING_FIELDS = ("name", "phone",
"email", "reason")` written into the memory module, which made this a dental-practice demo rather
than a product: a clinic needs a date of birth, a garage needs a registration, a restaurant needs
a party size, and none of them can express that without editing Python.

An operator declares the fields their agent needs. Everything else follows from that declaration:

    the form on screen        renders from it, field by field
    validation                dispatches on each field's KIND
    "what is still missing"   is computed from it, and is what the agent asks for next
    booking                   is blocked until every required field is present and valid

WHAT A FIELD KIND MEANS. It is a validation contract and an input mode, not a display hint. A
`phone` is ten digits in a working range; an `age` is a number a human can be. Getting that wrong
is not a cosmetic problem -- a mistyped phone number is a reminder that never arrives and a slot
nobody turns up for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .contact import Check, check_email, check_name, check_phone

#: The kinds a field can be. Each one is a validation rule plus the keyboard a phone should show.
KINDS = ("text", "name", "phone", "email", "number", "age", "date", "choice", "longtext")


@dataclass(slots=True)
class Field:
    """One thing the caller is asked for."""

    key: str
    label: str
    kind: str = "text"
    required: bool = True
    #: Shown under the input. For anything a caller could reasonably get wrong.
    help: str = ""
    #: For `choice`. Ignored otherwise.
    options: list[str] = field(default_factory=list)
    #: For `number` and `age`.
    minimum: float | None = None
    maximum: float | None = None
    #: Whether the agent may accept this from speech, or must wait for it to be typed.
    #: Off by default for anything with a shape: recognition mangles precisely those values.
    spoken_ok: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "kind": self.kind,
            "required": self.required, "help": self.help, "options": self.options,
            "minimum": self.minimum, "maximum": self.maximum, "spoken_ok": self.spoken_ok,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Field:
        kind = str(raw.get("kind", "text"))
        return cls(
            key=str(raw["key"]),
            label=str(raw.get("label") or raw["key"].replace("_", " ").title()),
            kind=kind if kind in KINDS else "text",
            required=bool(raw.get("required", True)),
            help=str(raw.get("help", "")),
            options=[str(o) for o in raw.get("options", [])],
            minimum=_as_number(raw.get("minimum")),
            maximum=_as_number(raw.get("maximum")),
            spoken_ok=bool(raw.get("spoken_ok", False)),
        )

    # -- validation --------------------------------------------------------
    def check(self, value: str) -> Check:
        """Is this usable? See `contact` for what "usable" can and cannot mean."""
        text = " ".join(str(value).split())
        if not text:
            return Check("", f"{self.label.lower()} is needed" if self.required else "")

        if self.kind == "name":
            return check_name(text)
        if self.kind == "phone":
            return check_phone(text)
        if self.kind == "email":
            return check_email(text)
        if self.kind in ("number", "age"):
            return self._check_number(text)
        if self.kind == "date":
            return self._check_date(text)
        if self.kind == "choice":
            return self._check_choice(text)
        if len(text) > 500:
            return Check(text[:500], f"{self.label.lower()} is too long")
        return Check(text)

    def _check_number(self, text: str) -> Check:
        digits = re.sub(r"[^\d.\-]", "", text)
        try:
            number = float(digits)
        except ValueError:
            return Check(text, f"{self.label.lower()} should be a number")

        # An age has bounds whether or not the operator set any. "150" is a typo, not a patient.
        low = self.minimum if self.minimum is not None else (0 if self.kind == "age" else None)
        high = self.maximum if self.maximum is not None else (120 if self.kind == "age" else None)
        if low is not None and number < low:
            return Check(text, f"{self.label.lower()} should be at least {_plain(low)}")
        if high is not None and number > high:
            return Check(text, f"{self.label.lower()} should be at most {_plain(high)}")

        # A whole number stays whole. "Age 34.0" reads as a bug.
        return Check(str(int(number)) if number == int(number) else str(number))

    def _check_date(self, text: str) -> Check:
        from datetime import date, datetime

        for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %B %Y", "%d %b %Y"):
            try:
                parsed = datetime.strptime(text, pattern).date()
            except ValueError:
                continue
            if parsed.year < 1900 or parsed > date.today():
                return Check(text, "that date does not look right")
            return Check(parsed.isoformat())
        return Check(text, "use a date like 1990-04-23")

    def _check_choice(self, text: str) -> Check:
        if not self.options:
            return Check(text)
        for option in self.options:
            if text.lower() == option.lower():
                return Check(option)
        return Check(text, f"choose one of: {', '.join(self.options)}")


def _as_number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _plain(number: float) -> str:
    return str(int(number)) if number == int(number) else str(number)


#: What a dental practice asks for. The default because the seeded agent is one, NOT because the
#: system knows anything about dentistry -- every field here is data an operator can change.
DEFAULT_INTAKE: list[Field] = [
    Field("name", "Full name", "name", help="As it should appear on the appointment."),
    Field("phone", "Phone", "phone", help="We will text a reminder the day before."),
    Field("email", "Email", "email", help="For the confirmation."),
    Field("age", "Age", "age", required=False,
          help="Under 18s need a parent or guardian present."),
    Field("reason", "What do you need?", "text", spoken_ok=True,
          help="A check-up, a cleaning, pain — whatever brings you in."),
]


def load(raw: Any) -> list[Field]:
    """An agent's intake schema, falling back to the default when it has none."""
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_INTAKE)
    out: list[Field] = []
    for item in raw:
        if isinstance(item, dict) and item.get("key"):
            out.append(Field.from_dict(item))
    return out or list(DEFAULT_INTAKE)


def dump(fields: list[Field]) -> list[dict[str, Any]]:
    return [f.as_dict() for f in fields]
