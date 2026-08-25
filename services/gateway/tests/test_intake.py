"""What a caller is asked for, and whether what they typed is usable.

TWO THINGS ARE BEING TESTED AND THEY PULL IN OPPOSITE DIRECTIONS.

THE SCHEMA IS THE OPERATOR'S. The first version had `BOOKING_FIELDS = ("name", "phone", "email",
"reason")` written into the memory module, which made this a dental demo rather than a product: a
clinic needs a date of birth, a garage needs a registration, a restaurant needs a party size, and
none of them could say so without editing Python.

THE VALIDATION IS NOT NEGOTIABLE. Before this, `"hello there"` was accepted as a phone number and
`"not-an-email"` as an email address, and both went into an appointment. That failure is invisible
until the day nobody turns up.

AND ONE HONEST LIMIT, stated here because it is easy to oversell: syntax is not existence.
`sam@northgate-dental.com` is perfectly well formed and may belong to nobody. Nothing in this file
can tell you an address is real -- only that it is the shape of one, and that it is not a common
typo. Establishing the rest needs a round trip.
"""

from __future__ import annotations

from datetime import date

import pytest

from dialtone.brain.contact import check_email, check_name, check_phone
from dialtone.brain.intake import DEFAULT_INTAKE, Field, dump, load
from dialtone.brain.memory import CallMemory

MONDAY = date(2026, 3, 2)


class TestEmail:
    @pytest.mark.parametrize("value", [
        "not-an-email", "a@b", "sam@", "@example.com", "sam example com",
        "sam@@example.com", "sam@.com", "sam..hassan@example.com", "sam@example.",
    ])
    def test_a_broken_address_is_refused(self, value: str):
        assert not check_email(value).ok, f"{value!r} was accepted"

    @pytest.mark.parametrize("value", [
        "sam@example.com", "sam.hassan@northgate-dental.co.uk", "s+tag@gmail.com",
        "SAM@EXAMPLE.COM", "sam_h99@sub.domain.org",
    ])
    def test_a_real_address_goes_through(self, value: str):
        assert check_email(value).ok, f"{value!r} was refused"

    def test_it_is_normalised(self):
        assert check_email("  SAM@Example.COM ").value == "sam@example.com"

    def test_a_dictated_address_is_repaired(self):
        """Speech recognition writes an address as words. A caller who dictated it has no idea
        why the form is complaining, so it is repaired rather than refused."""
        assert check_email("sam at gmail dot com").value == "sam@gmail.com"

    @pytest.mark.parametrize("typo,meant", [
        ("sam@gmial.com", "gmail.com"), ("sam@hotmial.com", "hotmail.com"),
        ("sam@yaho.com", "yahoo.com"), ("sam@example.con", "example.com"),
    ])
    def test_a_likely_typo_is_flagged_not_refused(self, typo: str, meant: str):
        """A warning, not an error. The address might genuinely be at that domain, and refusing
        it would trap a caller who cannot convince the form they know their own email."""
        result = check_email(typo)
        assert result.ok
        assert meant in result.warning


class TestPhone:
    @pytest.mark.parametrize("value", [
        "12", "hello there", "", "0000000000", "1111111111",
        "0125550142",          # area code starting 0
        "2120550142",          # exchange starting 0
        "21255501",            # too short
        "212555014299",        # too long
    ])
    def test_an_unusable_number_is_refused(self, value: str):
        assert not check_phone(value).ok, f"{value!r} was accepted"

    @pytest.mark.parametrize("value", [
        "2125559876", "(212) 555-9876", "212-555-9876", "+1 212 555 9876",
        "1 (212) 555 9876", "212.555.9876",
    ])
    def test_a_real_number_goes_through(self, value: str):
        assert check_phone(value).ok, f"{value!r} was refused"

    def test_it_is_normalised_to_one_shape(self):
        """So the same number typed three ways is one number in the database."""
        shapes = ["2125559876", "+1 212 555 9876", "212.555.9876", "(212)5559876"]
        assert len({check_phone(s).value for s in shapes}) == 1

    def test_the_reserved_range_is_flagged(self):
        """555-01xx is fictional. Allowed, because the seed data uses it and a demo that rejects
        its own examples is worse than one that flags them."""
        result = check_phone("(212) 555-0142")
        assert result.ok
        assert "test number" in result.warning


class TestName:
    @pytest.mark.parametrize("value", ["x", "", "42", "Sam 42"])
    def test_it_is_refused(self, value: str):
        assert not check_name(value).ok

    def test_a_flat_name_is_capitalised(self):
        assert check_name("sam hassan").value == "Sam Hassan"

    def test_a_name_that_is_already_right_is_left_alone(self):
        """"McDonald" and "van der Berg" are correct as typed. Title-casing them is not a fix."""
        for value in ("Sam McDonald", "Anna van der Berg", "Seán Ó Briain"):
            assert check_name(value).value == value

    def test_one_word_is_a_warning_not_a_rejection(self):
        """Plenty of people go by one name, and refusing them is a worse failure than a first
        name with no surname."""
        result = check_name("Sam")
        assert result.ok
        assert result.warning


class TestTheSchemaIsTheOperators:
    def test_an_agent_can_ask_for_whatever_it_needs(self):
        fields = load([
            {"key": "registration", "label": "Registration", "kind": "text"},
            {"key": "mileage", "label": "Mileage", "kind": "number", "maximum": 500000},
        ])
        assert [f.key for f in fields] == ["registration", "mileage"]

    def test_an_agent_with_no_schema_gets_a_working_default(self):
        """So an existing database keeps working, and an operator opts in by editing the fields
        rather than by having to declare them before the agent will run at all."""
        assert load(None) == DEFAULT_INTAKE
        assert load([]) == DEFAULT_INTAKE
        assert load("nonsense") == DEFAULT_INTAKE

    def test_it_survives_a_round_trip(self):
        assert load(dump(DEFAULT_INTAKE)) == DEFAULT_INTAKE

    def test_an_unknown_kind_falls_back_rather_than_crashing(self):
        assert load([{"key": "x", "kind": "wormhole"}])[0].kind == "text"

    def test_missing_is_computed_from_the_schema(self):
        memory = CallMemory(today=MONDAY, intake=load([
            {"key": "registration", "label": "Registration"},
            {"key": "mileage", "label": "Mileage", "kind": "number", "required": False},
        ]))
        assert memory.missing == ["registration"]          # the optional one is not missing
        memory.tell("registration", "AB12 CDE")
        assert memory.missing == []

    def test_the_agent_knows_what_to_ask_for_next(self):
        memory = CallMemory(today=MONDAY)
        assert memory.next_question == "Full name"
        memory.tell("name", "Sam Hassan")
        assert memory.next_question == "Phone"

    def test_only_fields_the_operator_allows_may_be_spoken(self):
        """Which values recognition is trusted with depends on what is being asked for. "What
        brings you in" is fine spoken; a registration number is not."""
        memory = CallMemory(today=MONDAY, intake=load([
            {"key": "reason", "label": "Reason", "spoken_ok": True},
            {"key": "registration", "label": "Registration", "spoken_ok": False},
        ]))
        memory.facts.clear()
        memory.tell("reason", "a cleaning", source="spoken")
        memory.tell("registration", "AB12 CDE", source="spoken")
        assert memory.unconfirmed == ["registration"]


class TestFieldKinds:
    def test_an_age_has_bounds_even_when_nobody_set_them(self):
        """"150" is a typo, not a patient."""
        age = Field("age", "Age", "age")
        assert not age.check("150").ok
        assert not age.check("-3").ok
        assert age.check("34").ok

    def test_a_whole_number_stays_whole(self):
        assert Field("age", "Age", "age").check("34").value == "34"

    def test_a_number_respects_the_operators_bounds(self):
        party = Field("party", "Party size", "number", minimum=1, maximum=12)
        assert not party.check("0").ok
        assert not party.check("40").ok
        assert party.check("6").ok

    def test_a_choice_must_be_one_of_the_choices(self):
        pick = Field("plan", "Plan", "choice", options=["PPO", "HMO", "Self-pay"])
        assert not pick.check("Medicare").ok
        assert pick.check("ppo").value == "PPO"          # matched case-insensitively, stored right

    def test_a_date_is_parsed_or_explained(self):
        dob = Field("dob", "Date of birth", "date")
        assert dob.check("1990-04-23").value == "1990-04-23"
        assert dob.check("23/04/1990").value == "1990-04-23"
        assert not dob.check("sometime in the nineties").ok
        assert not dob.check("2099-01-01").ok            # in the future

    def test_a_required_field_left_empty_says_so(self):
        assert not Field("name", "Full name", "name").check("").ok

    def test_an_optional_field_left_empty_is_fine(self):
        assert Field("age", "Age", "age", required=False).check("").ok
