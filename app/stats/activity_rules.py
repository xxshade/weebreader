"""Server-owned replacement bases for delayed first delivery of reading activity.

Reader opens only compute a reference. Mutations preserve the previous ordered
rules in their existing writer transaction. Keeping these small bases until the
account/work is deleted supports offline delivery and application restarts.
"""

from dataclasses import dataclass, replace
import hashlib
import json
import re

from app import text_replacements

_REFERENCE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class _Basis:
    user_id: int
    kind: str
    identity: int | None
    account_identity: str | None
    target_identity: str | None
    rules: list


def validate_reference(value):
    if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
        raise ValueError("Invalid reading rule reference.")


async def _basis(db, user_id, *, series_id=None, novel_id=None):
    kind, identity = ("series", series_id) if novel_id is None else ("novel", novel_id)
    if identity is None:
        return _Basis(user_id, kind, None, None, None, [])
    # One SQLite snapshot binds the ordered rules to their account and work.
    rows = await (
        await db.execute(
            """SELECT a.incarnation AS account_identity,
        t.incarnation AS target_identity,r.id,r.find_text,r.replace_text
        FROM stats_account_generations a
        JOIN estimate_target_incarnations t ON t.kind=? AND t.identity=?
        LEFT JOIN text_replacements r ON r.user_id=a.user_id
          AND r.series_id IS ? AND r.novel_id IS ?
        WHERE a.user_id=? ORDER BY r.id""",
            (kind, identity, series_id, novel_id, user_id),
        )
    ).fetchall()
    if not rows:
        raise ValueError("Reading rule scope no longer exists.")
    rules = [
        {"find": row["find_text"], "replace": row["replace_text"]}
        for row in rows
        if row["id"] is not None
    ]
    return _Basis(
        user_id,
        kind,
        identity,
        rows[0]["account_identity"],
        rows[0]["target_identity"],
        rules,
    )


def _reference(basis, version):
    if basis.identity is None:
        return None
    data = [
        1,
        basis.user_id,
        basis.account_identity,
        basis.kind,
        basis.identity,
        basis.target_identity,
        version,
        basis.rules,
    ]
    encoded = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


async def reader_context(
    db, user_id, *, series_id=None, novel_id=None, reader_order=None
):
    basis = await _basis(db, user_id, series_id=series_id, novel_id=novel_id)
    return {
        "replacements_json": json.dumps(
            text_replacements.client_rules(basis.rules, order=reader_order)
        ),
        "activity_rule_set_ref": _reference(basis, 2),
    }


async def preserve(db, user_id, *, series_id=None, novel_id=None):
    """Call before changing rules, inside the same writer transaction."""
    basis = await _basis(db, user_id, series_id=series_id, novel_id=novel_id)
    if basis.identity is None:
        return
    rules_json = json.dumps(basis.rules, separators=(",", ":"))
    for version in (1, 2):
        await db.execute(
            """INSERT OR IGNORE INTO activity_rule_sets
            (reference,user_id,series_id,novel_id,account_identity,target_identity,matching_version,rules_json)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                _reference(basis, version),
                user_id,
                series_id,
                novel_id,
                basis.account_identity,
                basis.target_identity,
                version,
                rules_json,
            ),
        )


async def preserve_rule(db, user_id, rule_id):
    row = await (
        await db.execute(
            "SELECT series_id,novel_id FROM text_replacements WHERE id=? AND user_id=?",
            (rule_id, user_id),
        )
    ).fetchone()
    if row:
        await preserve(
            db, user_id, series_id=row["series_id"], novel_id=row["novel_id"]
        )


async def resolve(db, user_id, value, *, series_id=None, novel_id=None):
    # Old activity keeps its original validation path. Never revive rejected
    # client records or invent a reference for text we did not record a basis for.
    if "rule_set_ref" not in value:
        return await text_replacements.rules_for(
            db, user_id, series_id=series_id, novel_id=novel_id
        )
    reference = value["rule_set_ref"]
    validate_reference(reference)
    version = value.get("replacement_version", 1)
    basis = await _basis(db, user_id, series_id=series_id, novel_id=novel_id)
    if reference == _reference(basis, version):
        return basis.rules
    row = await (
        await db.execute(
            """SELECT rules_json FROM activity_rule_sets
        WHERE reference=? AND user_id=? AND series_id IS ? AND novel_id IS ?
          AND account_identity=? AND target_identity=? AND matching_version=?""",
            (
                reference,
                user_id,
                series_id,
                novel_id,
                basis.account_identity,
                basis.target_identity,
                version,
            ),
        )
    ).fetchone()
    if row is None:
        raise ValueError("Unknown reading rule reference.")
    rules = json.loads(row["rules_json"])
    if (
        not isinstance(rules, list)
        or any(
            not isinstance(rule, dict)
            or set(rule) != {"find", "replace"}
            or any(not isinstance(value, str) for value in rule.values())
            for rule in rules
        )
        or reference != _reference(replace(basis, rules=rules), version)
    ):
        raise ValueError("Invalid stored reading rule set.")
    return rules
