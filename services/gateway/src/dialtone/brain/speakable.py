"""Turning written text into something a voice engine reads correctly.

WHY THIS IS NEEDED EVEN WITH A GOOD MODEL. The system prompt asks for spoken forms — "forty five
pounds", not "£45" — and a small model complies most of the time. Most of the time is not good
enough here, because the failure is silent: nothing errors, the transcript looks perfect, and the
caller hears "pound forty five" or "eight P M" or, on some engines, the digits read individually.

The knowledge base makes this worse rather than better. An operator writes their price list as
"forty five pounds", the model reads it, and helpfully converts it to "£45" on the way out.

So this is a last pass over the reply, after the model and before the synthesiser. It is small,
boring, and it is the difference between an agent that sounds like a person and one that sounds
like a screen reader.

SCOPE: en-GB and en-US conventions. Currency, times, dates, ordinals, common abbreviations, and
symbols. Anything not handled is left alone — a wrong rewrite is worse than no rewrite, because
the caller hears it either way and only one of them is recoverable.
"""

from __future__ import annotations

import re

_UNITS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()

_CURRENCY = {"£": ("pound", "pounds"), "$": ("dollar", "dollars"), "€": ("euro", "euros")}
_SUBUNIT = {"£": ("penny", "pence"), "$": ("cent", "cents"), "€": ("cent", "cents")}


def number_to_words(n: int) -> str:
    """Cardinal numbers up to the millions, the way they are said aloud.

    Includes the "and" that British English uses and American English drops: "one hundred and
    twenty" rather than "one hundred twenty". A dental practice in Leeds quoting prices is the
    use case, and the American form reads as a typo when spoken here.
    """
    if n < 0:
        return "minus " + number_to_words(-n)
    if n < 20:
        return _UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        return _TENS[tens] + (f" {_UNITS[unit]}" if unit else "")
    if n < 1_000:
        hundreds, rest = divmod(n, 100)
        out = f"{_UNITS[hundreds]} hundred"
        return f"{out} and {number_to_words(rest)}" if rest else out
    if n < 1_000_000:
        thousands, rest = divmod(n, 1_000)
        out = f"{number_to_words(thousands)} thousand"
        if not rest:
            return out
        # "two thousand and fifty", but "two thousand one hundred and fifty".
        joiner = " and " if rest < 100 else " "
        return out + joiner + number_to_words(rest)
    millions, rest = divmod(n, 1_000_000)
    out = f"{number_to_words(millions)} million"
    return f"{out} {number_to_words(rest)}" if rest else out


def _say_time(hour: int, minute: int, meridiem: str = "") -> str:
    """Times the way people say them, not the way clocks display them."""
    display = hour if 1 <= hour <= 12 else (hour - 12 if hour > 12 else 12)
    suffix = ""
    if meridiem:
        if meridiem.lower().startswith("a"):
            suffix = " in the morning"
        else:
            # Afternoon runs from noon to five; evening starts at six. Testing the raw hour
            # instead of the display hour called 12:15pm "the evening", because 12 >= 6.
            suffix = " in the afternoon" if display == 12 or display < 6 else " in the evening"

    if minute == 0:
        return f"{number_to_words(display)} o'clock{suffix}" if not suffix else \
               f"{number_to_words(display)}{suffix}"
    if minute == 15:
        return f"quarter past {number_to_words(display)}{suffix}"
    if minute == 30:
        return f"half past {number_to_words(display)}{suffix}"
    if minute == 45:
        nxt = display + 1 if display < 12 else 1
        return f"quarter to {number_to_words(nxt)}{suffix}"
    # "ten thirty", not "ten and thirty" -- the minutes are said as their own number.
    return f"{number_to_words(display)} {number_to_words(minute)}{suffix}"


_ORDINALS = {
    1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth", 9: "ninth", 12: "twelfth",
    20: "twentieth", 30: "thirtieth", 40: "fortieth", 50: "fiftieth",
}


def _ordinal(n: int) -> str:
    if n in _ORDINALS:
        return _ORDINALS[n]
    if n < 20:
        return _UNITS[n] + "th"
    tens, unit = divmod(n, 10)
    if unit == 0:
        return _ORDINALS.get(n, _TENS[tens] + "ieth")
    return f"{_TENS[tens]} {_ORDINALS.get(unit, _UNITS[unit] + 'th')}"


#: Written forms that a synthesiser mispronounces or spells out letter by letter.
_ABBREVIATIONS = {
    r"\bMon\b": "Monday", r"\bTue(s)?\b": "Tuesday", r"\bWed\b": "Wednesday",
    r"\bThu(r|rs)?\b": "Thursday", r"\bFri\b": "Friday", r"\bSat\b": "Saturday",
    r"\bSun\b": "Sunday",
    r"\bJan\b": "January", r"\bFeb\b": "February", r"\bMar\b": "March", r"\bApr\b": "April",
    r"\bJun\b": "June", r"\bJul\b": "July", r"\bAug\b": "August", r"\bSep(t)?\b": "September",
    r"\bOct\b": "October", r"\bNov\b": "November", r"\bDec\b": "December",
    r"\be\.g\.": "for example", r"\bi\.e\.": "that is", r"\betc\.": "and so on",
    r"\bappt\b": "appointment", r"\bmins?\b": "minutes", r"\bhrs?\b": "hours",
    r"\bapprox\.?\b": "approximately", r"\bNo\.\s*": "number ",
    r"\bvs\.?\b": "versus", r"\b24/7\b": "twenty four hours a day, seven days a week",
}


def speakable(text: str) -> str:
    """Rewrite a reply so a speech engine reads it the way a person would say it.

    Order matters throughout: currency before bare numbers (so "£45" is not first rewritten to
    "£forty five"), times before dates (so "10:30" is not read as a ratio), and symbols last.
    """
    if not text:
        return text

    out = text

    # ── currency ──────────────────────────────────────────────────────────
    def money(match: re.Match[str]) -> str:
        symbol, whole, fraction = match.group(1), match.group(2), match.group(3)
        amount = int(whole.replace(",", ""))
        singular, plural = _CURRENCY[symbol]
        unit = singular if amount == 1 else plural
        said = f"{number_to_words(amount)} {unit}"
        if fraction and int(fraction):
            sub_single, sub_plural = _SUBUNIT[symbol]
            pennies = int(fraction.ljust(2, "0")[:2])
            said += f" {number_to_words(pennies)} {sub_single if pennies == 1 else sub_plural}"
        return said

    # The digit group must not be able to end on a comma. `[\d,]+` matched "45," in "£45, which
    # includes...", swallowing the comma along with the amount -- which removed a clause boundary
    # the synthesiser splits on and flattened the intonation. Thousands separators still work:
    # a comma only counts when three digits follow it.
    out = re.sub(r"([£$€])\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{1,2}))?", money, out)

    # ── times ─────────────────────────────────────────────────────────────
    out = re.sub(
        r"\b(\d{1,2}):(\d{2})\s*([ap])\.?m\.?\b",
        lambda m: _say_time(int(m.group(1)), int(m.group(2)), m.group(3)),
        out, flags=re.IGNORECASE,
    )
    out = re.sub(
        r"\b(\d{1,2})\s*([ap])\.?m\.?\b",
        lambda m: _say_time(int(m.group(1)), 0, m.group(2)),
        out, flags=re.IGNORECASE,
    )
    out = re.sub(
        r"\b(\d{1,2}):(\d{2})\b",
        lambda m: _say_time(int(m.group(1)), int(m.group(2))),
        out,
    )

    # ── dates and ordinals ────────────────────────────────────────────────
    out = re.sub(
        r"\b(\d{1,2})(st|nd|rd|th)\b",
        lambda m: _ordinal(int(m.group(1))),
        out, flags=re.IGNORECASE,
    )

    # ── percentages ───────────────────────────────────────────────────────
    out = re.sub(
        r"\b(\d+)\s?%",
        lambda m: f"{number_to_words(int(m.group(1)))} percent",
        out,
    )

    # ── abbreviations ─────────────────────────────────────────────────────
    for pattern, replacement in _ABBREVIATIONS.items():
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)

    # ── bare numbers ──────────────────────────────────────────────────────
    # ONE AND TWO DIGITS ONLY, and the limit is doing real work. A bare number of three digits
    # or more is almost always an identifier rather than a quantity -- a phone number, a
    # reference, a room. Converting those produced "call one hundred and eleven" for the NHS
    # number 111, which is both wrong and the single worst sentence this agent could say.
    #
    # Genuine three-digit quantities reach the caller correctly anyway, because currency,
    # percentages, times and ordinals were all rewritten by their own rules above. What is left
    # here is exactly the set that should stay as digits, and every speech engine already reads
    # a bare digit run one digit at a time.
    out = re.sub(
        r"(?<![\d:.-])(\d{1,2})(?![\d:.-])",
        lambda m: number_to_words(int(m.group(1))),
        out,
    )

    # ── symbols ───────────────────────────────────────────────────────────
    out = out.replace("&", " and ").replace("/", " or ").replace("+", " plus ")
    # Markdown occasionally survives the prompt. Read aloud it becomes "star star urgent".
    out = re.sub(r"[*_`#]+", "", out)

    return " ".join(out.split())
