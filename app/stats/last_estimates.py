"""Bounded display values, separate from current library metadata and word bases."""

from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Value:
    remaining: float
    total: float | None = None
    missing: int = 0
    state: str = "available"

    @property
    def rest(self):
        return 0.0


@dataclass
class Account:
    identity: tuple
    values: OrderedDict = field(default_factory=OrderedDict)
    published: OrderedDict = field(default_factory=OrderedDict)


def keys_for(items, novels):
    keys = {("item", identity, None) for identity in items}
    keys.update(
        ("series", row["series_id"], None)
        for row in items.values()
        if row["series_id"] is not None
    )
    keys.update(("novel", identity, volume) for identity, volume in novels)
    return keys


async def identities(db, keys):
    targets = {(kind, identity) for kind, identity, _ in keys}
    targets.update(("volume", volume) for _, _, volume in keys if volume is not None)
    targets = sorted(targets)
    epoch, found = None, {}
    for start in range(0, len(targets), 400):
        batch = targets[start : start + 400]
        marks = ",".join("(?,?)" for _ in batch)
        rows = await (
            await db.execute(
                f"""SELECT t.kind,t.identity,t.incarnation,r.epoch
            FROM estimate_target_incarnations t CROSS JOIN estimate_revision r
            WHERE r.id=1 AND (t.kind,t.identity) IN ({marks})""",
                tuple(value for target in batch for value in target),
            )
        ).fetchall()
        for kind, identity, incarnation, current_epoch in rows:
            if epoch is not None and epoch != current_epoch:
                return None, {}
            epoch = current_epoch
            found[kind, identity] = incarnation
    return epoch, found


def binding(key, found):
    kind, identity, volume = key
    owner = found.get((kind, identity))
    part = found.get(("volume", volume)) if volume is not None else None
    return (owner, part) if owner and (volume is None or part) else None


class LastEstimates:
    def __init__(self, accounts, entries):
        self.max_accounts, self.max_entries = accounts, entries
        self.accounts = OrderedDict()

    def clear(self):
        self.accounts.clear()

    def forget(self, uid):
        self.accounts.pop(uid, None)

    def remember(self, uid, state):
        identity = (state.account, state.account_identity, state.revision[0][0])
        if not state.account_identity:
            self.forget(uid)
            return False
        account = self.accounts.get(uid)
        if account is None or account.identity != identity:
            account = Account(identity)
            self.accounts[uid] = account
        self.accounts.move_to_end(uid)
        while len(self.accounts) > self.max_accounts:
            self.accounts.popitem(last=False)
        current = {}
        for item, status in state.item_states.items():
            estimate = state.item_estimates.get(item)
            current["item", item, None] = (estimate, 0, status)
        for series, value in state.series.items():
            current["series", series, None] = (value, value.missing, value.state)
        for key, status in state.novel_states.items():
            current["novel", *key] = (state.novel_estimates.get(key), 0, status)
        published = OrderedDict(account.published)
        for key in list(published):
            if key not in current:
                del published[key]
        for key, (estimate, missing, status) in current.items():
            if estimate is None or estimate.remaining is None:
                if status != "calculating":
                    published[key] = (None, status)
                    account.values.pop(key, None)
                continue
            if status == "calculating" and key in account.values:
                continue
            value = Value(
                estimate.remaining, getattr(estimate, "total", None), missing, status
            )
            source = binding(key, state.identities)
            if source:
                account.values[key] = (source, value)
                account.values.move_to_end(key)
                while len(account.values) > self.max_entries:
                    account.values.popitem(last=False)
            published[key] = (value, status)
            published.move_to_end(key)
        while len(published) > self.max_entries:
            published.popitem(last=False)
        changed = dict(published) != dict(account.published)
        account.published = published
        return changed

    def valid(self, uid, state, key):
        account = self.accounts.get(uid)
        identity = (state.account, state.account_identity, state.revision[0][0])
        entry = (
            account.values.get(key)
            if account and account.identity == identity
            else None
        )
        return bool(entry and entry[0] == binding(key, state.identities))

    async def get(self, db, uid, estimator, keys=None, kind=None):
        account = self.accounts.get(uid)
        if account is None or account.identity[:2] != (
            estimator.cache_account,
            estimator.account_identity,
        ):
            return {}
        selected = (
            set(account.values) if keys is None else set(keys) & account.values.keys()
        )
        if kind is not None:
            selected = {key for key in selected if key[0] == kind}
        if not selected:
            return {}
        captured = {key: account.values[key] for key in selected}
        epoch, found = await identities(db, selected)
        if epoch != account.identity[2]:
            return {}
        return {
            key: value
            for key, (source, value) in captured.items()
            if source == binding(key, found)
        }
