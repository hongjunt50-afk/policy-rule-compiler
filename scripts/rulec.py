"""rulec — 把带条款引用的规则源编译成可执行、可核对、可独立分发的核验工具。

子命令：
  init    <out.yaml>                          生成规则源骨架
  build   <rules.yaml> [--case c.json]        编译 + 静态校验 → <name>.ruleset.json
  run     <ruleset.json> <case.json>          执行，打印结论 JSON
  show    <ruleset.json>                      列出规则与其条款依据
  test    <suite.json> --rules <rules.yaml>   跑 fixture 回归（期望计数 / 必须命中）
  mutate  <suite.json> --rules <rules.yaml>   变异测试：自动改坏规则，看套件能否抓住
  export  <ruleset.json> -o tool.py           导出单文件独立工具（零第三方依赖）

规则源用 YAML（需 pyyaml）或等价的 JSON（纯标准库）。
字典选择器 `file.json#items[*].name` 相对 `--data-dir`（默认规则源所在目录）解析。
"""
from __future__ import annotations

import argparse
import ast
import copy
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kernel  # noqa: E402

SEVERITIES = {"E", "W", "I"}
KINDS = {"check", "overlap", "aggregate", "frequency"}
OPS = {"eq", "ne", "lt", "le", "gt", "ge", "in", "not_in"}
LOOSE = {"and", "or", "not", "any", "none", "present", "absent", "const", "ref",
         "param", "dict", "fn", "len", "select", "ref_field"}
SELECTOR = re.compile(r"^(?P<file>[\w.-]+\.json)#(?P<body>.+)$")
FLIP = {"eq": "ne", "ne": "eq", "lt": "le", "le": "lt", "gt": "ge", "ge": "gt",
        "in": "not_in", "not_in": "in"}


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- 读规则源

def load_source(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise BuildError("规则源是 YAML，需要 pyyaml：pip install pyyaml\n"
                             "（或把规则源写成 .json，那就只用标准库）")
        return yaml.safe_load(text)
    return json.loads(text)


# ---------------------------------------------------------------- 字典

def _select(doc, body: str):
    cur = doc
    for part in re.split(r"(?<!\])\s*\.\s*(?!\d)", body):
        m = re.match(r"^(?P<f>\w+)(?:\[(?P<i>\*|\d+|\w+=[^\]]+)\])?(?:\.(?P<r>\w+))?$", part)
        if not m:
            raise BuildError("看不懂的 selector 片段: %s" % part)
        if not isinstance(cur, dict) or m.group("f") not in cur:
            raise BuildError("selector 字段不存在: %s" % m.group("f"))
        cur = cur[m.group("f")]
        sel, rest = m.group("i"), m.group("r")
        if sel is not None and sel != "*":
            if "=" in sel:
                k, v = sel.split("=", 1)
                if not isinstance(cur, list):
                    raise BuildError("选择器 %s 用在了非列表上" % sel)
                cur = [r for r in cur if isinstance(r, dict) and str(r.get(k)) == v]
            else:
                if not isinstance(cur, list) or int(sel) >= len(cur):
                    raise BuildError("下标越界: %s" % sel)
                cur = cur[int(sel)]
        if rest:
            if not isinstance(cur, list):
                raise BuildError("字段选择 %s 用在了非列表上" % rest)
            cur = [r.get(rest) if isinstance(r, dict) else None for r in cur]
    return cur


def resolve_dictionaries(spec: dict, data_dir: Path) -> dict:
    out, cache = {}, {}
    for name, val in (spec.get("dictionaries") or {}).items():
        if isinstance(val, list):
            out[name] = val
            continue
        m = SELECTOR.match(str(val))
        if not m:
            raise BuildError("字典 %s 既不是字面量列表也不是 file.json#selector：%r" % (name, val))
        fn = m.group("file")
        if fn not in cache:
            path = data_dir / fn
            if not path.exists():
                raise BuildError("字典 %s 指向的 %s 不存在" % (name, path))
            cache[fn] = json.loads(path.read_text(encoding="utf-8"))
        try:
            got = _select(cache[fn], m.group("body"))
        except BuildError as e:
            raise BuildError("字典 %s：%s" % (name, e))
        if not isinstance(got, list) or not got:
            raise BuildError("字典 %s 从 %r 取到空值或非列表" % (name, val))
        bad = [i for i, g in enumerate(got) if g is None]
        if bad:
            raise BuildError("字典 %s 第 %d 项取到 null（selector 写错？）" % (name, bad[0]))
        out[name] = got
    return out


# ---------------------------------------------------------------- 静态校验

def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def case_paths(case: dict) -> set:
    """可用字段路径；数组下标归一化，records.items 与 records.0.items 视为同一路径。"""
    paths = set()

    def dive(prefix, val):
        paths.add(re.sub(r"\.\d+", "", prefix))
        if isinstance(val, dict):
            for k, v in val.items():
                dive("%s.%s" % (prefix, k), v)
        elif isinstance(val, list):
            for v in val:
                if isinstance(v, (dict, list)):
                    dive(prefix + ".0", v)

    for k, v in case.items():
        dive(k, v)
    return paths


def var_collections(spec: dict) -> dict:
    out = {}
    for rule in spec["rules"]:
        scope = rule["scope"]
        out[scope.get("as", "r")] = scope["collection"]
        for name, ref in (scope.get("bind") or {}).items():
            out[name] = ref.partition("@")[0]
        if rule["kind"] == "frequency" and "actual" in scope:
            out.setdefault("r", scope["actual"]["collection"])
    return out


def check_static(spec: dict, case: dict | None) -> list:
    problems = []
    ids = [r["id"] for r in spec["rules"]]
    for rule in spec["rules"]:
        if rule["kind"] == "frequency":
            for b in ("short", "over"):
                ids.append((rule["scope"].get(b) or {}).get("id", "%s.%s" % (rule["id"], b)))
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        problems.append("规则 id 重复: %s" % dup)

    for rule in spec["rules"]:
        rid = rule["id"]
        if rule["kind"] not in KINDS:
            problems.append("%s: 未知 kind %r（可选 %s）" % (rid, rule["kind"], sorted(KINDS)))
        if rule.get("severity") not in SEVERITIES:
            problems.append("%s: severity 必须是 E/W/I，实际 %r" % (rid, rule.get("severity")))
        if not str(rule.get("clause") or "").strip():
            problems.append("%s: 缺 clause（每条规则必须能指回文书条款）" % rid)
        if rule["kind"] == "check" and "record_ref" not in rule:
            problems.append("%s: check 规则需要 record_ref，否则结论无法定位到行" % rid)

    dynamic = {"group", "total", "count", "other", "d"}
    for rule in spec["rules"]:
        it = rule["scope"].get("iterate")
        if it:
            dynamic.add(it["as"])
    for node in walk(spec["rules"]):
        for key in ("select", "any", "none"):
            if isinstance(node.get(key), dict) and "as" in node[key]:
                dynamic.add(node[key]["as"])

    if case is not None:
        paths = case_paths(case)
        roots = {p.split(".")[0] for p in paths}
        var_coll = var_collections(spec)
        for node in walk(spec["rules"]):
            if "ref" not in node:
                continue
            ref = node["ref"]
            head = ref.split(".")[0]
            rest = ref[len(head) + 1:]
            if head in dynamic or not rest:
                continue
            if head in var_coll:
                base = "%s.%s" % (var_coll[head], rest)
            elif head in roots:
                base = ref
            else:
                problems.append("ref 指向未声明的作用域变量: %s" % ref)
                continue
            concrete = re.sub(r"\.\d+", "", base)
            if concrete not in paths:
                problems.append("ref 路径在用例 schema 里不存在: %s" % ref)

    for rule in spec["rules"]:
        for node in walk(rule):
            if isinstance(node, dict) and len(node) == 1:
                key, val = next(iter(node.items()))
                if isinstance(val, list) and key not in OPS | LOOSE:
                    problems.append("%s: 未知表达式算子 %r" % (rule["id"], key))
    return problems


def check_messages(spec: dict) -> list:
    implicit = {"check": set(), "overlap": {"record_no"},
                "aggregate": set(),
                "frequency": {"elder_id", "month", "item", "actual", "planned"}}
    problems = []
    for rule in spec["rules"]:
        scope = rule["scope"]
        known = set((rule.get("evidence") or {}).keys()) | implicit.get(rule["kind"], set())
        known |= {scope.get("as", "r")} | set(scope.get("group_names") or [])
        it = scope.get("iterate")
        if it:
            known.add(it["as"])
        msgs = ([rule["message"]] if "message" in rule else
                [(rule["scope"].get(b) or {}).get("message", "") for b in ("short", "over")])
        for msg in msgs:
            for name in re.findall(r"\{(\w+?)(?:\.head20)?\}", msg):
                if name not in known:
                    problems.append("%s: 消息模板引用了未定义的占位符 %r" % (rule["id"], name))
    return problems


def compile_ruleset(src: Path, case: dict | None, data_dir: Path | None) -> dict:
    raw = load_source(src)
    for key in ("ruleset", "version", "title", "rules"):
        if key not in raw:
            raise BuildError("规则源缺字段 %r" % key)
    spec = {
        "ruleset": raw["ruleset"], "version": raw["version"], "title": raw["title"],
        "effective_from": str(raw.get("effective_from", "")),
        "sources": raw.get("sources", []),
        "params": raw.get("params", {}),
        "keys": raw.get("keys", {}),
        "dictionaries": resolve_dictionaries(raw, data_dir or src.parent),
        "rules": raw["rules"],
    }
    problems = (check_static(spec, case) + check_messages(spec))
    if problems:
        raise BuildError("编译失败：\n  " + "\n  ".join(problems))
    return spec


# ---------------------------------------------------------------- 变异测试

def _expr_slots(node, trail=()):
    """枚举可改坏的单算子节点，返回 (相对路径, 节点)。"""
    if isinstance(node, dict):
        if len(node) == 1 and next(iter(node)) in OPS | {"present", "absent"}:
            yield trail, node
        for k, v in node.items():
            yield from _expr_slots(v, trail + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _expr_slots(v, trail + (i,))


def _cond_roots(rule):
    """一条规则里所有"判据树"的根：when 与 scope.filter。返回 {标签: 根节点}。"""
    roots = {"when": rule.get("when")}
    if rule["scope"].get("filter"):
        roots["filter"] = rule["scope"]["filter"]
    return roots


def _assign(root, trail, new_node):
    if not trail:
        return new_node
    cur = root
    for step in trail[:-1]:
        cur = cur[step]
    cur[trail[-1]] = new_node
    return root


def _mutate_rule(spec, idx, setter):
    """深拷贝整份 spec，只改 rules[idx]，由 setter(rule) 就地改。"""
    m = copy.deepcopy(spec)
    setter(m["rules"][idx])
    return m


def _set_in_cond(rule, label, trail, new_node):
    if label == "when":
        rule["when"] = _assign(rule["when"], trail, new_node)
    else:
        rule["scope"]["filter"] = _assign(rule["scope"]["filter"], trail, new_node)


def _mutants(spec):
    """生成 (说明, 变异 spec)。只改语义不改语法，否则不算有效变异。"""
    for idx, rule in enumerate(spec["rules"]):
        for label, root in _cond_roots(rule).items():
            for trail, node in _expr_slots(root):
                key = next(iter(node))
                val = node[key]
                if key in FLIP:
                    yield ("%s[%s]: %s→%s" % (rule["id"], label, key, FLIP[key]),
                           _mutate_rule(spec, idx, lambda r, l=label, t=trail, n={FLIP[key]: val}:
                                        _set_in_cond(r, l, t, n)))
                elif key in ("present", "absent"):
                    other = "absent" if key == "present" else "present"
                    yield ("%s[%s]: %s→%s" % (rule["id"], label, key, other),
                           _mutate_rule(spec, idx, lambda r, l=label, t=trail, n={other: val}:
                                        _set_in_cond(r, l, t, n)))
                elif key in ("and", "or") and len(val) > 1:
                    yield ("%s[%s]: %s 删掉最后一项" % (rule["id"], label, key),
                           _mutate_rule(spec, idx, lambda r, l=label, t=trail, n={key: val[:-1]}:
                                        _set_in_cond(r, l, t, n)))
                    if key == "and" and len(val) == 2:
                        yield ("%s[%s]: and 改成 or" % (rule["id"], label),
                               _mutate_rule(spec, idx, lambda r, l=label, t=trail, n={"or": val}:
                                            _set_in_cond(r, l, t, n)))
    for name, val in spec.get("params", {}).items():
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            for factor, tag in ((10, "×10"), (0.1, "÷10"), (0, "→0")):
                m = copy.deepcopy(spec)
                m["params"][name] = val * factor if factor else 0
                yield ("param %s %s" % (name, tag), m)
    for name, items in spec.get("dictionaries", {}).items():
        if isinstance(items, list) and items:
            for new_items, tag in (([items[0]], "砍到只剩 1 项"), ([], "清空")):
                m = copy.deepcopy(spec)
                m["dictionaries"][name] = new_items
                yield ("字典 %s %s" % (name, tag), m)
    for idx, rule in enumerate(spec["rules"]):
        if rule["kind"] == "frequency":
            # 交换两个分支（少执行的报成超出、反之亦然）：语法仍合法，语义被改坏
            m = copy.deepcopy(spec)
            sc = m["rules"][idx]["scope"]
            sc["short"], sc["over"] = sc["over"], sc["short"]
            yield ("%s: short/over 分支互换" % rule["id"], m)


# ---------------------------------------------------------------- 测试套

def load_suite(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["fixtures"] if isinstance(data, dict) else data


def _norm(findings):
    out = []
    for f in findings:
        key = json.dumps({"r": f["rule"], "rec": f.get("record_no"),
                          "sev": f["severity"],
                          "ev": {k: v for k, v in (f.get("evidence") or {}).items()
                                 if isinstance(v, (str, int, float))}},
                         ensure_ascii=False, sort_keys=True, default=str)
        out.append(key)
    return out


def run_suite(spec, suite_path: Path, verbose=True) -> tuple:
    """返回 (通过与否, 失败条目)。

    每个 fixture 可写的断言：
      expect={E,W,I}                     分严重度计数
      must_hit / must_not_hit            规则 id 名单
      findings=[[rule, record_no], ...]  结论集合逐条相等（最严：计数相同但换了记录也能抓到）
    """
    ok = True
    fails = []
    suite = load_suite(suite_path)
    for fx in suite:
        case = json.loads((suite_path.parent / fx["case"]).read_text(encoding="utf-8"))
        try:
            got = kernel.verify(spec, case, fx.get("params"))
        except Exception as e:                    # 规则改坏到跑不起来，同样算没通过
            ok = False
            fails.append((fx["name"], ["执行报错: %s" % e]))
            if verbose:
                print("  ✗ %-22s 执行报错: %s" % (fx["name"], e))
            continue
        got_ids = [f["rule"] for f in got["findings"]]
        problems = []
        exp = fx.get("expect")
        if exp:
            for sev, want in exp.items():
                have = got["summary"][{"E": "hard_violations", "W": "needs_review",
                                       "I": "data_quality"}[sev]]
                if have != want:
                    problems.append("%s 期望 %d 实得 %d" % (sev, want, have))
        for rid in fx.get("must_hit") or []:
            if rid not in got_ids:
                problems.append("应命中未命中 %s" % rid)
        for rid in fx.get("must_not_hit") or []:
            if rid in got_ids:
                problems.append("不应命中却命中 %s" % rid)
        if fx.get("findings") is not None:
            from collections import Counter as _C
            want = _C((str(a), "" if b is None else str(b)) for a, b in fx["findings"])
            have = _C((f["rule"], str(f.get("record_no") or "")) for f in got["findings"])
            if want != have:
                missing = list((want - have).elements())
                extra = list((have - want).elements())
                if missing:
                    problems.append("结论缺少 %s" % (missing[:4],))
                if extra:
                    problems.append("结论多出 %s" % (extra[:4],))
        if problems:
            ok = False
            fails.append((fx["name"], problems))
            if verbose:
                print("  ✗ %-22s %s" % (fx["name"], "; ".join(problems)))
        elif verbose:
            print("  ✓ %-22s %s" % (fx["name"], dict(got["summary"])))
    # 同一份用例两次运行结论必须一致（幂等）
    for fx in suite:
        case = json.loads((suite_path.parent / fx["case"]).read_text(encoding="utf-8"))
        a = _norm(kernel.verify(spec, case, fx.get("params"))["findings"])
        b = _norm(kernel.verify(spec, case, fx.get("params"))["findings"])
        if a != b:
            ok = False
            fails.append((fx["name"], ["两次运行结论不一致"]))
    return ok, fails


# ---------------------------------------------------------------- 导出

def _kernel_source() -> str:
    src = (HERE / "kernel.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    first = tree.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
            and isinstance(first.value.value, str):
        lines = lines[first.end_lineno:]
    body = "\n".join(lines).lstrip("\n")
    # 导出物要自带 load，去掉重复的 pathlib 依赖说明即可，无需改逻辑
    return body


def export_tool(spec: dict, out: Path, name: str) -> Path:
    srcs = "\n".join("  - %s%s" % (s.get("title", "?"),
                                   "（%s）" % s["doc_no"] if s.get("doc_no") else "")
                     for s in spec.get("sources", []))
    header = '''#!/usr/bin/env python3
"""{title}

自动生成，请勿手改。规则集 {ruleset} v{version}（生效日 {effective}）。
条款出处：
{sources}

用法：
    python {name} <case.json> [day_max_minutes=480 ...]   参数按需覆盖
    python {name} --show-rules                           打印规则清单与条款依据
仅依赖 Python 3.10+ 标准库。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

RULESET = {payload}

'''.format(title=spec["title"], ruleset=spec["ruleset"], version=spec["version"],
           effective=spec.get("effective_from") or "-", sources=srcs or "  （未填）",
           name=name, payload=json.dumps(spec, ensure_ascii=False, indent=1))

    footer = '''

def show_rules():
    for r in RULESET["rules"]:
        if r["kind"] == "frequency":
            for b in ("short", "over"):
                s = r["scope"][b]
                print("%s  %-28s %s" % (s["severity"], s["id"], s["clause"]))
        else:
            print("%s  %-28s %s" % (r["severity"], r["id"], r["clause"]))
    print("\\n参数（可用 k=v 覆盖）：")
    for k, v in RULESET.get("params", {}).items():
        print("  %s = %r" % (k, v))


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    if argv[0] == "--show-rules":
        show_rules()
        return 0
    params = {}
    for extra in argv[1:]:
        if "=" in extra:
            k, _, v = extra.partition("=")
            params[k.strip()] = int(v) if v.strip().lstrip("-").isdigit() else v.strip()
    case = json.loads(open(argv[0], encoding="utf-8").read())
    result = verify(RULESET, case, params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["summary"]["hard_violations"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + _kernel_source() + footer, encoding="utf-8")
    return out


# ---------------------------------------------------------------- CLI

SKELETON = '''ruleset: my-rules
version: 0.1.0
title: 待填：这批规则核验什么
effective_from: 2026-01-01
sources:
  - id: doc-1
    title: 待填：文书全名
    doc_no: 待填：文号

# 阈值全部显式列出，换地区改这里，不改规则
params:
  example_threshold: 10

# 字典：要么是字面量列表，要么是 file.json#items[*].field（相对 --data-dir）
dictionaries:
  allowed_values: [甲, 乙]

rules:
  - id: E_EXAMPLE
    kind: check                 # check | overlap | aggregate | frequency
    severity: E                 # E 硬违规 / W 需复核 / I 数据质量
    clause: 待填：这条判据出自哪份文书的哪一栏
    scope:
      collection: records       # 用例里的表名
      as: r                     # 表内一行的变量名
      # bind: {elder: elders@r.elder_id}      关联另一张表
      # iterate: {over: {ref: r.items}, as: item}  逐项展开
      # filter: {present: {ref: r.some_field}}     前置条件，不满足直接跳过
    when: {absent: {ref: r.some_field}}
    message: 待填：给机构看的说明，可内插 {some_field}
    evidence: {some_field: {ref: r.some_field}}
    record_ref: {ref: r.record_no}
'''


def cmd_init(args):
    out = Path(args.out)
    if out.exists() and not args.force:
        print("已存在，加 --force 覆盖：%s" % out)
        return 1
    out.write_text(SKELETON, encoding="utf-8")
    print("骨架已写入 %s" % out)
    return 0


def cmd_build(args):
    src = Path(args.rules)
    case = json.loads(Path(args.case).read_text(encoding="utf-8")) if args.case else None
    try:
        spec = compile_ruleset(src, case, Path(args.data_dir) if args.data_dir else None)
    except BuildError as e:
        print(e)
        return 1
    out = Path(args.out) if args.out else src.parent / (src.name.split(".")[0] + ".ruleset.json")
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    n_dict = sum(len(v) for v in spec["dictionaries"].values())
    print("编译通过：%d 条规则 / %d 个字典 / %d 个字面量 → %s"
          % (len(spec["rules"]), len(spec["dictionaries"]), n_dict, out))
    if case is None:
        print("提示：未传 --case，已跳过字段路径静态检查（拼错字段名不会被发现）")
    return 0


def cmd_run(args):
    spec = kernel.load(args.ruleset)
    case = json.loads(Path(args.case).read_text(encoding="utf-8"))
    params = {}
    for kv in args.param or []:
        k, _, v = kv.partition("=")
        params[k] = int(v) if v.lstrip("-").isdigit() else v
    result = kernel.verify(spec, case, params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["summary"]["hard_violations"] == 0 else 1


def cmd_show(args):
    spec = kernel.load(args.ruleset)
    for r in spec["rules"]:
        if r["kind"] == "frequency":
            for b in ("short", "over"):
                s = r["scope"][b]
                print("%s  %-28s %s" % (s["severity"], s["id"], s["clause"]))
        else:
            print("%s  %-28s %s" % (r["severity"], r["id"], r["clause"]))
    print("\n参数：%s" % spec.get("params", {}))
    print("字典：%s" % {k: len(v) for k, v in spec.get("dictionaries", {}).items()})
    return 0


def _compile_for_suite(args):
    src = Path(args.rules)
    suite = Path(args.suite)
    first = load_suite(suite)[0]
    case = json.loads((suite.parent / first["case"]).read_text(encoding="utf-8"))
    return compile_ruleset(src, case, Path(args.data_dir) if args.data_dir else None)


def cmd_test(args):
    try:
        spec = _compile_for_suite(args)
    except BuildError as e:
        print(e)
        return 1
    print("测试套 %s" % args.suite)
    ok, fails = run_suite(spec, Path(args.suite))
    print("\n%s" % ("全部通过" if ok else "%d 项失败" % len(fails)))
    return 0 if ok else 1


def cmd_mutate(args):
    try:
        spec = _compile_for_suite(args)
    except BuildError as e:
        print(e)
        return 1
    suite = Path(args.suite)
    base_ok, _ = run_suite(spec, suite, verbose=False)
    if not base_ok:
        print("基线测试套本身未通过，先修好再跑变异测试")
        return 1
    survivors = []
    total = 0
    for label, mutant in _mutants(spec):
        total += 1
        try:
            ok, _ = run_suite(mutant, suite, verbose=False)
        except Exception as e:                    # 变异导致内核报错也算"被抓住"
            ok = False
        if ok:
            survivors.append(label)
    caught = total - len(survivors)
    print("变异 %d 个，抓住 %d 个，存活 %d 个（存活率 %.0f%%）"
          % (total, caught, len(survivors), 100.0 * len(survivors) / max(total, 1)))
    if survivors:
        print("\n未被测试套抓住的变异（这些语义目前无人守）：")
        for s in survivors:
            print("  - %s" % s)
    return 0 if not survivors else 1


def cmd_export(args):
    spec = kernel.load(args.ruleset)
    out = Path(args.out)
    export_tool(spec, out, out.name)
    print("导出 %s（%d 字节，零第三方依赖）" % (out, out.stat().st_size))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="rulec", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("out"); s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("build")
    s.add_argument("rules"); s.add_argument("--case"); s.add_argument("--data-dir")
    s.add_argument("-o", "--out")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("run")
    s.add_argument("ruleset"); s.add_argument("case")
    s.add_argument("--param", action="append")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("show"); s.add_argument("ruleset"); s.set_defaults(func=cmd_show)

    for name, fn in (("test", cmd_test), ("mutate", cmd_mutate)):
        s = sub.add_parser(name)
        s.add_argument("suite"); s.add_argument("--rules", required=True)
        s.add_argument("--data-dir")
        s.set_defaults(func=fn)

    s = sub.add_parser("export")
    s.add_argument("ruleset"); s.add_argument("-o", "--out", required=True)
    s.set_defaults(func=cmd_export)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
