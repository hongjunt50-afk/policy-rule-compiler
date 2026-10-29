"""规则 IR 的执行内核。零第三方依赖，也不认识任何具体领域。

编译产物（ruleset.json）只含四种规则形态：check / overlap / aggregate / frequency。
导出独立工具时，本文件源码会被原样内联，因此这里不许 import 除标准库以外的东西。
"""
import json
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------- 取值原语

def _get(root, path):
    cur = root
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return None
    return cur


def _hm(s):
    t = datetime.strptime(s, "%H:%M")
    return t.hour * 60 + t.minute


FUNCS = {
    "minutes_between": lambda a, b: _hm(b) - _hm(a),
    "abs": abs,
    "sub": lambda a, b: a - b,
    "add": lambda a, b: a + b,
    "date_diff_days": lambda later, earlier: (
        datetime.strptime(later[:10], "%Y-%m-%d").date()
        - datetime.strptime(earlier[:10], "%Y-%m-%d").date()).days,
    "month": lambda s: s[:7] if s else None,
    "trim": lambda s: (s or "").strip(),
    "head": lambda s, n: s[:n],
    "size": lambda v: len(v) if isinstance(v, (list, dict, str)) else None,
}

KEYS = {"elders": "elder_id", "caregivers": "caregiver_id", "records": "record_no"}


class Env:
    """求值环境：作用域变量 + 参数 + 字典 + 用例数据。"""

    def __init__(self, spec, case, params):
        self.spec, self.case, self.params = spec, case, params
        self.dicts = spec.get("dictionaries", {})
        self.vars, self._index = {}, {}

    def child(self, **kw):
        e = Env.__new__(Env)
        e.spec, e.case, e.params = self.spec, self.case, self.params
        e.dicts, e._index = self.dicts, self._index
        e.vars = {**self.vars, **kw}
        return e

    def collection(self, name):
        return self.case.get(name, [])

    def index_of(self, collection):
        if collection not in self._index:
            k = self.spec.get("keys", {}).get(collection, KEYS.get(collection, "id"))
            self._index[collection] = {r.get(k): r for r in self.collection(collection)}
        return self._index[collection]


def resolve(node, env):
    if not isinstance(node, dict):
        return node
    if "const" in node:
        return node["const"]
    if "ref" in node:
        head = node["ref"].split(".")[0]
        if head in env.vars:
            rest = node["ref"][len(head) + 1:]
            return _get(env.vars[head], rest) if rest else env.vars[head]
        return None
    if "param" in node:
        return env.params.get(node["param"])
    if "dict" in node:
        return env.dicts.get(node["dict"], [])
    if "fn" in node:
        args = [resolve(a, env) for a in node.get("args", [])]
        if any(a is None for a in args):
            return None          # 缺字段不构成违规，交给 present/absent 表达
        return FUNCS[node["fn"]](*args)
    if "len" in node:
        v = resolve(node["len"], env)
        return len(v) if isinstance(v, (list, set, dict, str)) else None
    if "select" in node:
        s = node["select"]
        return [row for row in (resolve(s["over"], env) or [])
                if truth(s["test"], env.child(**{s["as"]: row}))]
    if "ref_field" in node:
        s = node["ref_field"]
        row, name = resolve(s["row"], env), resolve(s["field"], env)
        return row.get(name) if isinstance(row, dict) else None
    raise ValueError("未知取值节点: %r" % (node,))


# ---------------------------------------------------------------- 断言

OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "not_in")


def truth(expr, env):
    if expr is True or expr is False:
        return expr
    if not isinstance(expr, dict):
        return bool(expr)
    if "and" in expr:
        return all(truth(e, env) for e in expr["and"])
    if "or" in expr:
        return any(truth(e, env) for e in expr["or"])
    if "not" in expr:
        return not truth(expr["not"], env)
    if "any" in expr:
        return bool(resolve({"select": expr["any"]}, env))
    if "none" in expr:
        return not resolve({"select": expr["none"]}, env)
    if "present" in expr:
        return bool(resolve(expr["present"], env))
    if "absent" in expr:
        return not bool(resolve(expr["absent"], env))

    op = next(k for k in OPS if k in expr)
    a, b = resolve(expr[op][0], env), resolve(expr[op][1], env)
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if a is None or b is None:
        return False
    if op == "lt":
        return a < b
    if op == "le":
        return a <= b
    if op == "gt":
        return a > b
    if op == "ge":
        return a >= b
    if op == "in":
        return a in b
    if op == "not_in":
        return a not in b
    raise ValueError(op)


# ---------------------------------------------------------------- 消息

def _fmt(v):
    return "['" + "', '".join(str(x) for x in v) + "']" if isinstance(v, list) else str(v)


def render(template, values):
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + ".head20}", str(v or "")[:20])
        out = out.replace("{" + k + "}", _fmt(v))
    return out


# ---------------------------------------------------------------- 四种规则形态

def _emit(rid, sev, rec, msg, clause, ev, out):
    out.append({"rule": rid, "severity": sev, "message": msg,
                "clause": clause, "record_no": rec, "evidence": ev})


def _base(rs):
    return Env(rs["_spec"], rs["_case"], rs["_params"])


def _scoped(env, scope):
    for row in env.collection(scope["collection"]):
        e = env.child(**{scope["as"]: row})
        for name, ref in (scope.get("bind") or {}).items():
            coll, _, path = ref.partition("@")
            e = e.child(**{name: e.index_of(coll).get(resolve({"ref": path}, e))})
        d = {}
        for name, node in (scope.get("derive") or {}).items():
            d[name] = resolve(node, e.child(d=d))     # 后项可引用前项
        e = e.child(d=d)
        if scope.get("filter") and not truth(scope["filter"], e):
            continue
        yield row, e


def _check(rs, rule, out):
    scope = rule["scope"]
    for _, env in _scoped(_base(rs), scope):
        it = scope.get("iterate")
        targets = ([(el, env.child(**{it["as"]: el})) for el in (resolve(it["over"], env) or [])]
                   if it else [(None, env)])
        for el, e in targets:
            if not truth(rule["when"], e):
                continue
            ev = {k: resolve(n, e) for k, n in (rule.get("evidence") or {}).items()}
            values = dict(ev)
            if it:
                values[it["as"]] = el
            rec = resolve(rule["record_ref"], e) if rule.get("record_ref") else None
            _emit(rule["id"], rule["severity"], rec, render(rule["message"], values),
                  rule["clause"], ev, out)


def _overlap(rs, rule, out):
    scope = rule["scope"]
    groups = {}
    for row, env in _scoped(_base(rs), scope):
        span = [resolve({"ref": p}, env) for p in scope["span"]]
        if any(s is None for s in span) or _hm(span[1]) <= _hm(span[0]):
            continue      # 起止本身错乱的交给 check 规则报，避免重复计数
        key = tuple(resolve({"ref": p}, env) for p in scope["group_by"])
        groups.setdefault(key, []).append((span, row))
    for key, rows in groups.items():
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                (s1, e1), r1 = rows[i]
                (s2, e2), r2 = rows[j]
                if not (s1 < e2 and s2 < e1):
                    continue
                first, second = (r2, r1) if rule.get("report") == "later" else (r1, r2)
                env = _base(rs).child(**{scope["as"]: first}, other=second,
                                      group=dict(zip(scope["group_names"], key)))
                ev = {k: resolve(n, env) for k, n in (rule.get("evidence") or {}).items()}
                rec = resolve(rule["record_ref"], env)
                _emit(rule["id"], rule["severity"], rec,
                      render(rule["message"], {**ev, **env.vars["group"], "record_no": rec}),
                      rule["clause"], ev, out)


def _aggregate(rs, rule, out):
    scope = rule["scope"]
    acc = {}
    for row, env in _scoped(_base(rs), scope):
        val = resolve(scope["measure"], env)
        if val is None:
            continue
        slot = acc.setdefault(tuple(resolve({"ref": p}, env) for p in scope["group_by"]),
                              {"total": 0, "n": 0})
        slot["total"] += val
        slot["n"] += 1
    for key, slot in acc.items():
        group = dict(zip(scope["group_names"], key))
        env = _base(rs).child(group=group, total=slot["total"], count=slot["n"])
        if not truth(rule["when"], env):
            continue
        ev = {k: resolve(n, env) for k, n in (rule.get("evidence") or {}).items()}
        _emit(rule["id"], rule["severity"], None,
              render(rule["message"], {**ev, **group}), rule["clause"], ev, out)


def _frequency(rs, rule, out):
    """计划频次 vs 月度实际。这是"计划表对照"这类形态，显式建模，不假装通用。"""
    scope = rule["scope"]
    for elder in _base(rs).collection(scope["collection"]):
        env = _base(rs).child(**{scope["as"]: elder})
        if scope.get("filter") and not truth(scope["filter"], env):
            continue
        plan = [p for p in (resolve({"ref": scope["plan"]["rows"]}, env) or [])
                if p.get(scope["plan"]["target"]) is not None]
        if not plan:
            continue
        counts = {}
        for rec in _base(rs).collection(scope["actual"]["collection"]):
            renv = _base(rs).child(r=rec, **{scope["as"]: elder})
            if not truth(scope["actual"]["filter"], renv):
                continue
            ym = resolve(scope["actual"]["month"], renv)
            for item in resolve(scope["actual"]["items"], renv) or []:
                counts.setdefault(ym, {}).setdefault(item, 0)
                counts[ym][item] += 1
        for ym, counter in counts.items():
            for p in plan:
                name = p.get("name") or p.get("code")
                want = p[scope["plan"]["target"]]
                got = counter.get(name, 0)
                branch = (scope["short"] if got < want else
                          scope["over"] if got > want else None)
                if not branch:
                    continue
                ev = {"elder_id": elder.get("elder_id"), "month": ym, "item": name,
                      "planned": want, "actual": got}
                _emit(branch["id"], branch["severity"], None,
                      render(branch["message"], ev), branch["clause"], ev, out)


KINDS = {"check": _check, "overlap": _overlap,
         "aggregate": _aggregate, "frequency": _frequency}


def verify(spec, case, params=None):
    rs = {"_spec": spec, "_case": case,
          "_params": {**spec.get("params", {}), **(params or {})}}
    out = []
    for rule in spec["rules"]:
        KINDS[rule["kind"]](rs, rule, out)
    order = {"E": 0, "W": 1, "I": 2}
    out.sort(key=lambda f: order.get(f["severity"], 9))
    counts = {"E": 0, "W": 0, "I": 0}
    for f in out:
        counts[f["severity"]] += 1
    return {"ruleset": spec["ruleset"], "version": spec.get("version"),
            "records_checked": len(case.get("records", case.get("claims", []))),
            "summary": {"hard_violations": counts["E"], "needs_review": counts["W"],
                        "data_quality": counts["I"]},
            "findings": out}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
