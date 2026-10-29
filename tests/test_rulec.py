"""技能自检：编译/执行/导出/变异全链路。`python tests/test_rulec.py` 或 pytest。

只用标准库 unittest，不引第三方框架。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import kernel  # noqa: E402
import rulec  # noqa: E402

EX = ROOT / "examples" / "ltc"
RULES = EX / "ltc.rules.yaml"


def compile_ltc():
    case = json.loads((EX / "sample_case.json").read_text(encoding="utf-8"))
    return rulec.compile_ruleset(RULES, case, None)


class TestCompile(unittest.TestCase):
    def setUp(self):
        self.spec = compile_ltc()

    def test_dicts_expanded(self):
        self.assertEqual(len(self.spec["dictionaries"]["catalog_names"]), 36)
        self.assertEqual(len(self.spec["dictionaries"]["medical_item_names"]), 16)

    def test_rules_have_clause(self):
        for rule in self.spec["rules"]:
            self.assertTrue(rule["clause"].strip(), rule["id"])

    def _broken(self, old, new):
        """改一处 YAML 再编译，必须抛 BuildError。"""
        src = RULES.read_text(encoding="utf-8")
        self.assertIn(old, src)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.rules.yaml"
            p.write_text(src.replace(old, new, 1), encoding="utf-8")
            (Path(d) / "services_catalog.json").write_text(
                (EX / "services_catalog.json").read_text(encoding="utf-8"), encoding="utf-8")
            case = json.loads((EX / "sample_case.json").read_text(encoding="utf-8"))
            with self.assertRaises(rulec.BuildError):
                rulec.compile_ruleset(p, case, None)

    def test_rejects_typo_field(self):
        self._broken("{ref: r.duration_min}", "{ref: r.duration_minutes}")

    def test_rejects_unknown_operator(self):
        self._broken("{gt: [{ref: d.gap}, {param: duration_tolerance}]}",
                     "{gtt: [{ref: d.gap}, {param: duration_tolerance}]}")

    def test_rejects_empty_clause(self):
        self._broken("clause: 长期护理保险服务记录单·签字确认栏（一服务一记录，须双方签字）",
                     'clause: ""')

    def test_rejects_bad_severity(self):
        self._broken("    severity: E\n", "    severity: 致命\n")

    def test_rejects_duplicate_id(self):
        self._broken("id: W_LATE_ENTRY", "id: E_BAD_TIME")

    def test_rejects_unknown_placeholder(self):
        self._broken("缺{label}", "缺{labels}")

    def test_rejects_bad_dict_selector(self):
        self._broken("items[*].name", "items[*].nmae")

    def test_rejects_missing_record_ref(self):
        src = RULES.read_text(encoding="utf-8")
        self.assertIn("    record_ref: {ref: r.record_no}\n", src)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.rules.yaml"
            p.write_text(src.replace("    record_ref: {ref: r.record_no}\n", "", 1), encoding="utf-8")
            for f in ("services_catalog.json",):
                (Path(d) / f).write_text((EX / f).read_text(encoding="utf-8"), encoding="utf-8")
            case = json.loads((EX / "sample_case.json").read_text(encoding="utf-8"))
            with self.assertRaises(rulec.BuildError):
                rulec.compile_ruleset(p, case, None)


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.spec = compile_ltc()

    def run_case(self, name, params=None):
        case = json.loads((EX / name).read_text(encoding="utf-8"))
        return kernel.verify(self.spec, case, params)

    def test_sample_hits(self):
        self.assertEqual(self.run_case("sample_case.json")["summary"],
                         {"hard_violations": 9, "needs_review": 9, "data_quality": 1})

    def test_clean_zero(self):
        self.assertEqual(self.run_case("clean_case.json")["findings"], [])

    def test_boundary_exact_threshold_not_flagged(self):
        """480 分钟整、滞后 3 天整都不该报；481 和 4 天才该报。"""
        out = self.run_case("boundary_case.json")
        msgs = [f["message"] for f in out["findings"] if f["rule"] == "W_EXCESS_DAILY"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("481", msgs[0])
        self.assertFalse([f for f in out["findings"] if f["rule"] == "W_LATE_ENTRY"])

    def test_param_override(self):
        out = self.run_case("edge_case.json", {"day_max_minutes": 100000})
        self.assertFalse([f for f in out["findings"] if f["rule"] == "W_EXCESS_DAILY"])

    def test_every_finding_has_clause_and_severity(self):
        for f in self.run_case("sample_case.json")["findings"]:
            self.assertTrue(f["clause"].strip(), f["rule"])
            self.assertIn(f["severity"], ("E", "W", "I"))

    def test_idempotent(self):
        a = self.run_case("sample_case.json")["findings"]
        b = self.run_case("sample_case.json")["findings"]
        self.assertEqual(json.dumps(a, sort_keys=True, default=str),
                         json.dumps(b, sort_keys=True, default=str))


class TestSuiteAndMutate(unittest.TestCase):
    def setUp(self):
        self.spec = compile_ltc()

    def test_suite_passes(self):
        ok, fails = rulec.run_suite(self.spec, EX / "suite.json", verbose=False)
        self.assertTrue(ok, fails)

    def test_suite_catches_wrong_count(self):
        bad = json.loads((EX / "suite.json").read_text(encoding="utf-8"))
        bad["fixtures"][0]["findings"] = [["E_BAD_TIME", "R-0001"]]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "suite.json"
            p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
            for f in EX.glob("*.json"):
                if f.name != "suite.json":
                    (Path(d) / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            ok, _ = rulec.run_suite(self.spec, p, verbose=False)
        self.assertFalse(ok)

    def test_mutants_all_caught(self):
        """现有的 3 份 fixture 必须抓住全部语义变异；加了新规则或新测试后若回归，这里变红。"""
        survivors = []
        for label, mutant in rulec._mutants(self.spec):
            ok, _ = rulec.run_suite(mutant, EX / "suite.json", verbose=False)
            if ok:
                survivors.append(label)
        self.assertEqual(survivors, [], "未被抓住的语义变异：%s" % survivors)


class TestExport(unittest.TestCase):
    def test_export_runs_standalone(self):
        spec = compile_ltc()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "tool.py"
            rulec.export_tool(spec, out, "tool.py")
            text = out.read_text(encoding="utf-8")
            self.assertIn("RULESET = {", text)
            self.assertNotIn("import yaml", text)
            case = EX / "boundary_case.json"
            r = subprocess.run([sys.executable, str(out), str(case)],
                               capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(r.returncode, 1)          # 有 E，退出码非零
            got = json.loads(r.stdout)
            self.assertEqual(got["summary"]["hard_violations"], 4)
            r2 = subprocess.run([sys.executable, str(out), "--show-rules"],
                                capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(r2.returncode, 0)
            self.assertIn("E_UNKNOWN_SERVICE", r2.stdout)


class TestCrossDomain(unittest.TestCase):
    """换域不碰内核：医保两库规则必须直接跑通。"""

    def test_claims(self):
        ex = ROOT / "examples" / "claims"
        case = json.loads((ex / "claims_demo.json").read_text(encoding="utf-8"))
        spec = rulec.compile_ruleset(ex / "claims.rules.yaml", case, None)
        out = kernel.verify(spec, case)
        self.assertEqual(out["summary"]["hard_violations"], 4)
        self.assertEqual(out["summary"]["needs_review"], 1)
        ids = {f["rule"] for f in out["findings"]}
        for want in ("E_SEX_RESTRICTED_DRUG", "E_CHILD_ONLY_DRUG_ADULT",
                     "E_MULTI_CLAIM_OVERLAP", "W_DAILY_DOSE_EXCESS"):
            self.assertIn(want, ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
