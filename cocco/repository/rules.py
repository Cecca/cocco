from sqlalchemy import (
    and_,
    func,
    literal_column,
)

from cocco.model import (
    Challenge,
    Course,
)


# A challenge is archived when it says so, or when its course does. That `or`
# is the whole of "archiving a course archives its challenges", and this is
# the one place it is written: the page queries select it and the routes that
# need the answer for one row read it, rather than re-deriving it.
#
# Nothing downstream - no route, no template - may spell it a second time, the
# way nothing downstream spells `effective_course_role`'s promotion again. A
# template that reads `challenge.archived_at` for a badge silently un-archives
# every challenge under an archived course.
def _effective_archived_at():
    """When a challenge became read-only: its own `archived_at`, or its
    course's. Only valid in a statement that has both tables in its FROM.

    A page query exposes it as `effective_archived_at`, never `AS
    archived_at`: `challenge.archived_at` rides along in the same record, and
    two keys of one name in a `row_to_json` resolve last-one-wins."""
    return func.coalesce(Challenge.c.archived_at, Course.c.archived_at)


# Whether a challenge is open to the class yet: `challenge.visible_from` is
# NULL for a hidden one, a past instant for a live one and a future one for a
# challenge that opens by itself at that moment.
#
# The neighbour of `_effective_archived_at` above and written under the same
# discipline: once, here, and read by the page queries and the single-row
# gates alike. Nothing downstream may spell it a second time.
#
# Two differences from its neighbour. It does not cascade from the course - a
# course has no hidden state to cascade, so this needs only `challenge` in its
# FROM - and it is a *boolean* rather than a timestamp, because "when" and
# "whether" come apart here: a page that has to say "opens on the 1st" reads
# `visible_from` itself, and a gate that has to decide reads this.
#
# `now()` and never a Python clock, for the reason `model.py` gives for every
# timestamp in the schema: the transaction's clock is the one that counts.
def _challenge_visible():
    """Whether the class may see a challenge at all. A page query exposes it
    as `is_visible`, never `AS visible`."""
    return and_(
        Challenge.c.visible_from.is_not(None),
        Challenge.c.visible_from <= func.now(),
    )


# A challenge's `number`, the "Challenge 03" a page prints: its rank within its
# course in the order the course page lists it. Not `challenge.position`, which
# is the sort key the course editor's arrows write and has gaps after a move.
# `position, id` because `position` defaults to 0: challenges that have never
# been reordered stay in the order they were created in.
#
# A window, so it ranks whatever rows it is selected over. Both callers select
# it over every challenge of the course, before anything is filtered out.
def _challenge_number():
    return func.row_number().over(
        partition_by=Challenge.c.course_id,
        order_by=(Challenge.c.position, Challenge.c.id),
    )


# What a page query's `coalesce` falls back on when a `json_agg` had no rows to
# aggregate - which makes it NULL, not an empty array - so a template can loop
# over the field without asking whether it is there.
_EMPTY_JSON_ARRAY = literal_column("'[]'::json")


def _json_object(**fields):
    """`json_build_object('key', value, ...)`, with the keys - which are
    Python identifiers, never request data - written into the SQL: a bound
    key would reach the variadic `"any"` with no type to resolve."""
    args = []
    for key, value in fields.items():
        args += [literal_column(f"'{key}'"), value]
    return func.json_build_object(*args)
