"""The retrieval-evaluation maths, tested on stub backends so the metrics themselves cannot silently be wrong."""

from app.knowledge.docs import KDoc
from app.knowledge.search import Hit
from eval.run_eval import bootstrap_ci, evaluate, to_markdown, with_variant_queries


def doc(base):
    kind, _ = base.split(":")
    return KDoc(doc_id=f"{base}@en", base_id=base, kind=kind, lang="en", title="t", text="t", source="s")


class Fixed:
    def __init__(self, ranking):
        self.ranking, self.name = ranking, "fixed"

    def search(self, query, k=5, lang=None, kind=None):
        return [Hit(doc(b), 1.0, r) for r, b in enumerate(self.ranking[:k], 1)]


def q(rel, tier="direct", text="x"):
    return {"q": text, "lang": "en", "tier": tier, "rel": rel}


def test_recall_and_mrr_are_computed_per_fact():
    r = evaluate(
        Fixed(["faq:a", "faq:b", "faq:c", "faq:d"]), [q(["faq:a"]), q(["faq:c"]), q(["faq:z"]), q(["faq:a", "faq:b"])]
    )
    a = r["ALL"]
    assert a["R@1"] == round((1 + 0 + 0 + 0.5) / 4, 3)  # a found at rank 1; c not in top 1; z never; half of {a,b}
    assert a["R@3"] == round((1 + 1 + 0 + 1) / 4, 3)
    assert a["MRR"] == round((1 + 1 / 3 + 0 + 1) / 4, 3)


def test_results_are_split_by_tier_and_the_robustness_copies_do_not_count_overall():
    qs = [q(["faq:a"], "direct"), q(["faq:z"], "urdu"), q(["faq:z"], "urdu_variant")]
    r = evaluate(Fixed(["faq:a"]), qs)
    assert r["direct"]["R@1"] == 1.0 and r["urdu"]["R@1"] == 0.0 and r["urdu_variant"]["n"] == 1
    assert r["ALL"]["R@1"] == 0.5  # direct + urdu only


def test_bootstrap_interval_brackets_the_mean_and_shrinks_with_more_data():
    lo, hi = bootstrap_ci([1.0] * 30 + [0.0] * 30)
    assert lo < 0.5 < hi
    lo2, hi2 = bootstrap_ci([1.0] * 300 + [0.0] * 300)
    assert (hi2 - lo2) < (hi - lo)
    assert bootstrap_ci([0.7] * 20) == (0.7, 0.7)


def test_keyboard_variant_copies_use_arabic_letters():
    out = with_variant_queries(
        [{"q": "کیا کلینک کہاں ہے", "lang": "ur", "tier": "urdu", "rel": ["faq:location"]}, q(["faq:a"])]
    )
    variant = next(x for x in out if x["tier"] == "urdu_variant")
    assert "ي" in variant["q"] and "ك" in variant["q"] and "ه" in variant["q"] and "ی" not in variant["q"]
    assert len(out) == 3


def test_markdown_table_has_a_row_per_method_with_intervals():
    r = {"m": evaluate(Fixed(["faq:a"]), [q(["faq:a"]), q(["faq:b"])])}
    md = to_markdown(r)
    assert "| `m` |" in md and "95% CI" in md and "–" in md
