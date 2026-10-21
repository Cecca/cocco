"""Test cases and the evaluator: where their files live under the storage root."""


def _testcase_dir(challenge_id: int) -> str:
    return f"challenge_{challenge_id}"


def _evaluator_dir(challenge_id: int) -> str:
    # A sibling of `_testcase_dir`, not a directory inside it: a test case file
    # may itself be named "evaluator", and then the two would disagree about
    # whether that name is a file or a directory. Test case paths never contain
    # a slash in the name, so these two trees cannot collide.
    return f"evaluator_{challenge_id}"
