"""Strict evidence matching for the one-time, human-reviewed release debt manifest."""

from __future__ import annotations

from collections import Counter

from scripts.code_health.config import TARGETS
from scripts.code_health.model import Hit, MODULE_SCOPE


def _substantive(value):
    return (isinstance(value, str) and len(value.strip()) >= 20
            and not value.strip().lower().startswith(("todo", "tbd", "pending", "unknown")))


def _reviewed(entry, read_blob, head):
    if entry.get("status") != "reviewed":
        return False
    review = entry.get("review", {})
    evidence = entry.get("validation", [])
    if (not _substantive(review.get("compatibility")) or not _substantive(review.get("receipt"))
            or not review.get("reviewer") or not isinstance(evidence, list) or not evidence):
        raise RuntimeError("Inherited debt needs substantive compatibility review and validation")
    for item in evidence:
        if not isinstance(item, dict) or item.get("outcome") != "passed" or not _substantive(item.get("claim")):
            raise RuntimeError("Pending or unsupported validation cannot accept inherited debt")
        reference = read_blob(head, item.get("path", ""))
        if reference is None or reference[0] != item.get("blob"):
            raise RuntimeError("Inherited-debt validation source changed; review is stale")
    return True


def _identity(entry, read_blob, head, heads, sources):
    path = entry.get("path", "")
    source = entry.get("source", {})
    revision, source_path = source.get("commit"), source.get("path", "")
    if source_path != path or source.get("scope") != entry.get("scope"):
        raise RuntimeError("Inherited-debt mappings cannot relocate or rename source units")
    candidate, original = read_blob(head, path), read_blob(revision, source_path)
    if candidate is None or candidate[0] != entry.get("candidate_blob"):
        raise RuntimeError("Reviewed inherited-debt candidate file changed")
    if original is None or original[0] != source.get("blob"):
        raise RuntimeError("Pinned inherited-debt source blob differs")
    current, old = heads.get(path), sources.get(revision, {}).get(source_path)
    if current is None or old is None or current.error or old.error:
        raise RuntimeError("Inherited debt requires successfully measured source and candidate")
    scope, source_scope = entry.get("scope"), source.get("scope")
    if candidate[0] != original[0]:
        if scope == MODULE_SCOPE:
            raise RuntimeError("Module-level inherited debt requires an unchanged source file")
        unit, old_unit = current.units.get(scope), old.units.get(source_scope)
        expected = source.get("unit_body_hash")
        if (unit is None or old_unit is None or not expected
                or unit.body_hash != expected or old_unit.body_hash != expected):
            raise RuntimeError("Modified-module inherited debt requires the exact reviewed parent unit")
        if not _substantive(entry.get("review", {}).get("module_bindings")):
            raise RuntimeError("Modified module imports/global bindings need independent compatibility review")
    return current, old, source_scope


def _budget(entry, current, old, source_scope):
    rule, scope = entry.get("rule"), entry.get("scope")
    if rule in TARGETS:
        if rule == "FILE_LINES":
            value, cap = current.metrics.get(rule), old.metrics.get(rule)
        else:
            unit, old_unit = current.units.get(scope), old.units.get(source_scope)
            value = unit.metrics.get(rule) if unit else None
            cap = old_unit.metrics.get(rule) if old_unit else None
        if value is None or cap is None or cap <= TARGETS[rule] or value > cap:
            raise RuntimeError("New metric growth cannot be classified as inherited debt")
        return (rule, source_scope, "metric"), 1
    code = entry.get("code")
    if not isinstance(code, str) or not code:
        raise RuntimeError("Inherited pattern debt requires an exact code fingerprint")
    count = old.hits[Hit(rule, source_scope, code)]
    if count <= 0 or not current.hits[Hit(rule, scope, code)]:
        raise RuntimeError("Claimed inherited pattern is absent from measured pinned source/candidate")
    return (rule, source_scope, code), count


def _matching(entry, findings, current):
    matches = []
    for finding in findings:
        if (finding.path != entry["path"] or finding.rule != entry["rule"]
                or finding.scope != entry["scope"] or finding.detail != entry.get("detail")
                or not finding.blocking or finding.allowed_reason is not None):
            continue
        if finding.rule not in TARGETS and current.code_line(finding.line) != entry.get("code"):
            continue
        matches.append(finding)
    return matches


def assign_entries(entries, findings, heads, sources, read_blob, head, digest):
    """A source occurrence pays once; copied debt/new rules never spend the same credit."""
    seen = set()
    spent = Counter()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Malformed inherited-debt entry")
        identity = entry.get("id")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise RuntimeError("Inherited-debt entries require unique reviewed identities")
        seen.add(identity)
        if not _reviewed(entry, read_blob, head):
            continue  # Proposal/pending entries remain in the raw blocking report.
        current, old, source_scope = _identity(entry, read_blob, head, heads, sources)
        budget_key, maximum = _budget(entry, current, old, source_scope)
        count = entry.get("max_occurrences")
        if type(count) is not int or count < 1 or count > maximum:
            raise RuntimeError("Inherited-debt count exceeds pinned measured source occurrences")
        matches = _matching(entry, findings, current)
        if len(matches) > count:
            raise RuntimeError("New inherited-debt occurrence requires review and remains blocking")
        source = entry["source"]
        key = source["commit"], source["path"], budget_key
        spent[key] += len(matches)
        if spent[key] > maximum:
            raise RuntimeError("Copied debt cannot reuse a pinned source occurrence")
        for finding in matches:
            if finding.inherited_debt is not None:
                raise RuntimeError("A finding cannot be classified twice")
            finding.inherited_debt = {"entry": identity, "manifest_sha256": digest,
                                      "source_commit": source["commit"], "source_path": source["path"],
                                      "review_receipt": entry["review"]["receipt"]}
