from linkedin_bot.follow import (
    CREDITS_RE,
    DISMISS_NAME_RE,
    INVITED_RE,
    SELECTED_RE,
    SENT_RE,
    SUBMIT_NAME_RE,
    _row_name,
    run_budget,
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


def test_sent_marker_matches_only_the_post_submit_confirmation():
    # the state the dialog flips into after a batch is submitted
    assert SENT_RE.search("Dialog content start.\nInvitations sent\n1 connection is invited to follow your page")
    assert SENT_RE.search("invitation sent")
    # the invitee list itself must not look like the confirmation
    assert SENT_RE.search("Invite to follow") is None
    assert SENT_RE.search("Invite 49 people who follow similar pages") is None
    assert SENT_RE.search("1 connection is invited to follow your page") is None
    assert SENT_RE.search("Nicholas Bailey\nSelect Nicholas Bailey") is None


def test_dismiss_marker_matches_close_controls_not_the_list():
    assert DISMISS_NAME_RE.search("Dismiss")
    assert DISMISS_NAME_RE.search("Close")
    assert DISMISS_NAME_RE.search("Show more results") is None
    assert DISMISS_NAME_RE.search("Invite 3") is None


def test_run_budget_prefers_credits_over_the_follow_caps():
    # credits are the real budget: 37 credits beat a 10/day cap
    assert run_budget(37, 10) == (37, "credits")


def test_run_budget_falls_back_to_caps_when_credits_are_unreadable():
    assert run_budget(None, 10) == (10, "cap")


def test_run_budget_limit_binds_first_when_smaller():
    assert run_budget(50, 10, extra_limit=3) == (3, "limit")
    assert run_budget(None, 10, extra_limit=3) == (3, "limit")


def test_run_budget_limit_above_credits_keeps_the_credit_reason():
    assert run_budget(5, 10, extra_limit=60) == (5, "credits")
