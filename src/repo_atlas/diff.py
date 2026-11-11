"""Symbol-level change classification and unambiguous rename detection."""

from dataclasses import dataclass

from repo_atlas.models import ChangeKind, Symbol


@dataclass(frozen=True, slots=True)
class Delta:
    changes: dict[str, ChangeKind]
    renames: dict[str, str]


def diff_symbols(old: dict[str, Symbol], new: dict[str, Symbol]) -> Delta:
    changes: dict[str, ChangeKind] = {}
    unchanged_old = old.keys() & new.keys()
    for qualname in unchanged_old:
        previous, current = old[qualname], new[qualname]
        if previous.sig_hash != current.sig_hash:
            changes[qualname] = ChangeKind.SIGNATURE
        elif previous.body_hash != current.body_hash:
            changes[qualname] = ChangeKind.BODY
        elif previous.doc != current.doc:
            changes[qualname] = ChangeKind.DOC

    removed = {name: old[name] for name in old.keys() - new.keys()}
    added = {name: new[name] for name in new.keys() - old.keys()}
    removed_by_hash: dict[tuple[str, str], list[str]] = {}
    added_by_hash: dict[tuple[str, str], list[str]] = {}
    for qualname, symbol in removed.items():
        removed_by_hash.setdefault((symbol.sig_hash, symbol.body_hash), []).append(qualname)
    for qualname, symbol in added.items():
        added_by_hash.setdefault((symbol.sig_hash, symbol.body_hash), []).append(qualname)
    renames: dict[str, str] = {}
    for key, old_names in removed_by_hash.items():
        new_names = added_by_hash.get(key, [])
        if len(old_names) == len(new_names) == 1:
            old_name, new_name = old_names[0], new_names[0]
            renames[old_name] = new_name
            changes[new_name] = ChangeKind.RENAMED
    for qualname in removed.keys() - renames.keys():
        changes[qualname] = ChangeKind.REMOVED
    renamed_targets = set(renames.values())
    for qualname in added.keys() - renamed_targets:
        changes[qualname] = ChangeKind.ADDED
    return Delta(changes, renames)
