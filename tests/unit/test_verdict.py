from blastgate.verdict import Facts, Verdict, decide

EV = "synthetic: contract manifest"


def facts(**kw):
    base = dict(contract_found=True, field_found=True, mapping_status="complete", breaking=True)
    return Facts(**{**base, **kw})


def test_block_needs_vulnerable_consumer_and_critical_or_sla():
    assert decide(facts(consumers=(("a", False, EV),), critical_affected=("a",))).verdict is Verdict.BLOCK
    assert decide(facts(consumers=(("a", False, EV),), sla_ids=("SLA-1",))).verdict is Verdict.BLOCK


def test_vulnerable_consumer_without_critical_or_sla_is_warn():
    assert decide(facts(consumers=(("a", False, EV),))).verdict is Verdict.WARN


def test_block_wins_even_when_coverage_incomplete():
    d = decide(facts(mapping_status="incomplete", consumers=(("a", False, EV),), sla_ids=("S",)))
    assert d.verdict is Verdict.BLOCK


def test_empty_result_with_incomplete_coverage_is_warn_not_clear():
    for status in ("incomplete", "unknown", None):
        assert decide(facts(mapping_status=status)).verdict is Verdict.WARN


def test_missing_contract_or_field_is_warn():
    assert decide(facts(contract_found=False, field_found=False)).verdict is Verdict.WARN
    assert decide(facts(field_found=False)).verdict is Verdict.WARN


def test_consumers_with_fallback_and_complete_coverage_is_clear():
    assert decide(facts(consumers=(("a", True, EV),))).verdict is Verdict.CLEAR


def test_unknown_fallback_or_missing_evidence_is_warn():
    assert decide(facts(consumers=(("a", None, EV),))).verdict is Verdict.WARN
    assert decide(facts(consumers=(("a", True, None),))).verdict is Verdict.WARN
    assert decide(facts(consumers=(("a", True, ""),))).verdict is Verdict.WARN


def test_non_breaking_change_does_not_block_consumer_without_fallback():
    d = decide(facts(breaking=False, consumers=(("a", False, EV),), critical_affected=("a",)))
    assert d.verdict is Verdict.CLEAR


def test_complete_coverage_with_no_consumers_says_so():
    d = decide(facts())
    assert d.verdict is Verdict.CLEAR and "no consumer" in d.reasons[0]
