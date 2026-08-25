"""Checking a name, a phone number and an email address before booking on them.

WHAT THIS CAN AND CANNOT DO, because the distinction is the whole design and it is usually
glossed over.

    SYNTAX          free, instant, and catches most of what actually goes wrong. "hello there"
                    is not a phone number. "sam@gmial.com" is a typo somebody makes every day.
    EXISTENCE       cannot be established from the string. `sam@northgate-dental.com` is
                    perfectly well formed and may belong to nobody.
    REACHABILITY    cannot be established at all without using it -- sending a message and
                    seeing what happens.

So this file does the first, flags the likely typos in the second, and is explicit that the third
needs a round trip. An input that passes everything here is WELL FORMED, which is a weaker claim
than correct, and the UI says so rather than showing a green tick that means more than it should.

WHY IT MATTERS HERE MORE THAN USUAL. This is the confirmation channel for an appointment. A
mistyped digit is not a validation error the caller sees and fixes -- it is a reminder that never
arrives and a slot nobody turns up for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Check:
    """The result of looking at one field."""

    #: The value as it should be stored -- trimmed, normalised, formatted.
    value: str
    #: What is wrong with it, in words a caller would understand. Empty when it is usable.
    problem: str = ""
    #: Usable, but worth a second look. A likely typo rather than a definite error.
    warning: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem


# ── email ────────────────────────────────────────────────────────────────────
# Deliberately not RFC 5322. The full grammar admits addresses no mail provider accepts and
# rejecting on it would be theatre; this is the shape of an address a person actually has.
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}$")

#: Domains people mistype daily. The value is what they meant.
_TYPOS = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gmail.co": "gmail.com",
    "gmail.con": "gmail.com", "gmaill.com": "gmail.com", "gnail.com": "gmail.com",
    "hotmial.com": "hotmail.com", "hotmai.com": "hotmail.com", "hotmail.co": "hotmail.com",
    "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com", "yahoo.co": "yahoo.com",
    "outlok.com": "outlook.com", "outloo.com": "outlook.com",
    "iclould.com": "icloud.com", "iclod.com": "icloud.com",
}

#: Endings that are almost always a slip for ".com".
_BAD_TLDS = {"con": "com", "cmo": "com", "vom": "com", "xom": "com", "comm": "com"}


def check_email(raw: str) -> Check:
    value = raw.strip().lower()
    if not value:
        return Check("", "an email address is needed for the confirmation")

    # Speech recognition writes an address as words. Worth repairing rather than rejecting: a
    # caller who dictated it has no idea why the form is complaining.
    value = value.replace(" at ", "@").replace(" dot ", ".").replace(" ", "")

    if value.count("@") != 1:
        return Check(raw.strip(), "that does not look like an email address")
    if ".." in value or value.startswith(".") or value.endswith("."):
        return Check(raw.strip(), "that does not look like an email address")
    if not _EMAIL.match(value):
        return Check(raw.strip(), "that does not look like an email address")

    domain = value.split("@", 1)[1]
    if domain in _TYPOS:
        return Check(value, warning=f"did you mean {_TYPOS[domain]}?")

    tld = domain.rsplit(".", 1)[-1]
    if tld in _BAD_TLDS:
        fixed = f"{domain[: -len(tld)]}{_BAD_TLDS[tld]}"
        return Check(value, warning=f"did you mean {fixed}?")

    return Check(value)


# ── phone ────────────────────────────────────────────────────────────────────
def check_phone(raw: str) -> Check:
    """North American numbers, which is what this practice takes.

    A real deployment localises this. The point of the checks below is not the country -- it is
    that "12", "hello there" and "(000) 000-0000" all used to be accepted and written into an
    appointment, where the first anyone finds out is when nobody answers.
    """
    value = raw.strip()
    if not value:
        return Check("", "a phone number is needed so we can reach you")

    digits = re.sub(r"\D", "", value)
    if not digits:
        return Check(value, "that does not look like a phone number")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        got = f"{len(digits)} digit{'s' if len(digits) != 1 else ''}"
        return Check(value, f"a phone number has ten digits; that has {got}")

    area, exchange = digits[:3], digits[3:6]
    # NANP: neither the area code nor the exchange may begin with 0 or 1.
    if area[0] in "01" or exchange[0] in "01":
        return Check(value, "that is not a working phone number")
    if len(set(digits)) == 1:
        return Check(value, "that is not a working phone number")
    # 555-01xx is the reserved fictional range. Allowed, because the seed data uses it and a
    # demo that rejects its own examples is worse than one that flags them.
    warning = ""
    if exchange == "555" and digits[6:8] == "01":
        warning = "that is a reserved test number"

    return Check(f"({area}) {exchange}-{digits[6:]}", warning=warning)


# ── name ─────────────────────────────────────────────────────────────────────
def check_name(raw: str) -> Check:
    value = " ".join(raw.split())
    if not value:
        return Check("", "a name is needed for the appointment")
    if len(value) < 2 or not re.search(r"[A-Za-z]", value):
        return Check(value, "that does not look like a name")
    if re.search(r"\d", value):
        return Check(value, "a name should not contain numbers")

    # One word is a warning, never a rejection. Plenty of people go by one name, and refusing
    # them is a worse failure than a first name with no surname.
    warning = "" if " " in value else "just a first name? a surname helps us find you"
    # Title case, but only where the caller typed it flat. "McDonald" and "van der Berg" are
    # already right and must not be "corrected".
    if value.islower() or value.isupper():
        value = " ".join(w.capitalize() for w in value.split())
    return Check(value, warning=warning)


CHECKS = {"name": check_name, "phone": check_phone, "email": check_email}


def check(field: str, value: str) -> Check:
    """Check one field by name. Anything unrecognised is passed through untouched."""
    checker = CHECKS.get(field)
    return checker(value) if checker else Check(value.strip())
