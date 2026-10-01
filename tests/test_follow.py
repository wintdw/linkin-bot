from linkedin_bot.follow import (
    CREDITS_RE,
    INVITED_RE,
    SELECTED_RE,
    SUBMIT_NAME_RE,
    _row_name,
)


class _Checkbox:
    """Fake row checkbox whose ``evaluate`` returns its <li> innerText."""

    def __init__(self, row_text: str):
        self.row_text = row_text

    def evaluate(self, script, timeout=None):
        return self.row_text


def test_submit_name_matches_only_the_bulk_invite_button():
    assert SUBMIT_NAME_RE.match("Invite")
    assert SUBMIT_NAME_RE.match("Invite 3")
    assert SUBMIT_NAME_RE.match("invite 12")
    # never the modal title / page controls
    assert not SUBMIT_NAME_RE.match("Invite to follow")
    assert not SUBMIT_NAME_RE.match("Unselect all")
    assert not SUBMIT_NAME_RE.match("Show more results")
    assert not SUBMIT_NAME_RE.match("Invite 49 people who follow similar pages")


def test_credits_and_selected_parsing():
    assert CREDITS_RE.search("50/50 credits available · Credit refill: October 31, 2026").group(1) == "50"
    assert CREDITS_RE.search("3/50 credits available").group(1) == "3"
    assert SELECTED_RE.search("0 selected").group(1) == "0"
    assert SELECTED_RE.search("2 selected").group(1) == "2"
    assert SELECTED_RE.search("no counter here") is None


def test_invited_marker_matches_only_invited_rows():
    assert INVITED_RE.findall("Ryan Graham\nDirector | Principal Consultant\nInvited") == ["Invited"]
    # the modal title / bulk copy must not look like an invited row
    assert INVITED_RE.findall("Invite to follow") == []
    assert INVITED_RE.findall("Invite 49 people who follow similar pages") == []


def test_row_name_reads_the_person_not_the_select_label():
    row = "Hsu Ken Ooi\nCo-Founder and Managing Partner at Iterative\nSelect Hsu Ken Ooi"
    assert _row_name(_Checkbox(row), 1000) == "Hsu Ken Ooi"


def test_row_name_skips_selected_label_when_row_is_checked():
    row = "Selected Hsu Ken Ooi\nCo-Founder and Managing Partner at Iterative"
    assert _row_name(_Checkbox(row), 1000) == "Hsu Ken Ooi"


def test_row_name_none_when_only_select_labels():
    assert _row_name(_Checkbox("Select A\nSelect B"), 1000) is None
    assert _row_name(_Checkbox(""), 1000) is None
