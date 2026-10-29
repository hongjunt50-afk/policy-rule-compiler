---
name: policy-rule-compiler
description: 把带条款引用的政策/文书规则写成规则源（YAML/JSON），编译成可执行、可核对、可独立分发的核验工具。用户要"把政策文本变成能跑的自查/稽核工具""规则要能指回文书条款""换个地区只改阈值""这批规则到底有没有测到"时走这里。产出 ruleset.json + 单文件 Python 核验器（零第三方依赖），并给出防假阳性、防规则改坏的变异测试。不用于纯文档问答、不做失能等级评估这类判级。
---

# 政策规则编译（policy-rule-compiler）

**一句话**：政策条文写成规则源 → 编译期拦住写错的地方 → 产出机构能自己核对的独立工具 → 用变异测试证明规则真的被测住了。

价值不在"少写代码"（DSL 比直接写 Python 还长），在三件做不到就卖不出去的事：**每条结论能指回条款**、**政策改版能看可读 diff**、**换地区只改参数**。

## 什么时候用

| 用户说 | 走这里 |
|---|---|
| "把这份管理办法做成自查工具""规则要能引用文中条款" | ✅ |
| "下个地区政策阈值不一样，不想重写" | ✅ |
| "这套规则到底测住没有""怕改了没报错" | ✅ `mutate` |
| "导入文书自动出工具"（不要人审） | ❌ 先说清：机器可判面通常只占文书三成，其余是制度/现场判断，硬上会沉默漏判 |
| 失能等级判定、诊断结论 | ❌ 不碰 |

## 七条命令

脚本 `$RC = <skill_dir>/scripts/rulec.py`。

```bash
RC="<skill_dir>/scripts/rulec.py"      # 装在本机技能目录下时换成实际路径

python "$RC" init  my.rules.yaml                                  # 规则源骨架（带注释说明每种形态）
python "$RC" build my.rules.yaml --case 样例.json                  # 编译 + 静态校验 → my.ruleset.json
python "$RC" run   my.ruleset.json 台账.json [day_max_minutes=480]  # 执行，打印结论 JSON
python "$RC" show  my.ruleset.json                                # 规则清单 + 条款 + 参数（给人核对用）
python "$RC" test  suite.json --rules my.rules.yaml               # 回归：计数/必命中/结论集合逐条比对 + 幂等
python "$RC" mutate suite.json --rules my.rules.yaml              # 变异测试：自动改坏规则，看套件抓不抓得住
python "$RC" export my.ruleset.json -o 长护自查工具.py             # 单文件工具，机构拿去就能跑
```

`--case` 必传：静态检查靠它知道真实字段路径，不传就查不出字段名拼错。

## 规则源长什么样

```yaml
ruleset: ltc-service-consistency
version: 0.2.0
title: 长护险服务过程一致性核验
sources:
  - {id: doc-2026, title: 长期护理保险服务管理文书（2026 年版）, issuer: 国家医保局办公室}

params:                       # 阈值全部显式列出，换地区改这里
  day_max_minutes: 480
  late_entry_days: 3

dictionaries:                 # 字典：字面量列表，或 file.json#items[category=医疗护理类].name
  catalog_names: services_catalog.json#items[*].name
  certified_qualifications: [长期照护师, 护士, 医生, 医疗护理员, 养老护理员]

rules:
  - id: E_QUAL_MISMATCH
    kind: check               # check | overlap | aggregate | frequency
    severity: E               # E 硬违规 / W 需复核 / I 数据质量
    clause: 服务记录单·护理人员资质；质量评价表·其他类第 1 项   # 必填，缺了编译不过
    scope:
      collection: records     # 用例里的表
      as: r                   # 一行的变量名
      bind: {cg: caregivers@r.caregiver_id}    # 关联另一张表
      filter: {present: {ref: cg}}             # 前置条件，不满足跳过
    when:                     # 判据：and/or/not/any/none/present/absent + 比较算子
      and:
        - any: {over: {ref: r.items}, as: item, test: {in: [{ref: item}, {dict: medical_item_names}]}}
        - not_in: [{fn: trim, args: [{ref: cg.qualification}]}, {dict: certified_qualifications}]
    message: 资质「{qualification}」不属于持证护理人员，却执行了医疗护理类项目 {items}
    evidence: {qualification: {ref: cg.qualification}, items: {ref: r.items}}
    record_ref: {ref: r.record_no}              # check 必填，结论要能定位到行
```

### 四种形态，别硬凑

| kind | 干什么 | 必备字段 |
|---|---|---|
| `check` | 逐行（或逐项）比字段 | `scope`、`when`、`message`、`record_ref` |
| `overlap` | 同一人/同一天时段是否重叠 | `scope.group_by`、`scope.span`、`record_ref` |
| `aggregate` | 按组求和后与阈值比 | `scope.group_by`、`scope.measure`、`when` |
| `frequency` | 计划频次 vs 月度实际 | `scope.plan`、`scope.actual`、`short`/`over` 两分支 |

`frequency` 是"计划表对照"这类形状显式建模出来的，不是通用能力。新域如果形态不在这四种里，**先加形态再写规则**，不要在 `check` 里塞 Python 逻辑。

### 算子与函数（全部可枚举，写错编译期就死）

比较：`eq ne lt le gt ge in not_in`　逻辑：`and or not any none present absent`
取值：`ref`（`r.field` / `cg.field` / `d.derive名` / `group.名` / `total` / `count` / `other`）、`const`、`param`、`dict`、`len`、`select`、`ref_field`（动态取字段）
函数：`minutes_between abs sub add date_diff_days month trim head size`

`fn` 任一参数取不到值时整体返回 `None`，不报违规——缺字段要报就用 `present`/`absent` 明说，别用计算函数兜。

## 编译期能拦住什么（这是本技能的主要产出）

| 错误 | 例子 |
|---|---|
| ref 字段名拼错 | `r.duration_minutes`（真名是 `duration_min`）|
| ref 指向没声明的变量 | `cgx.qualification` |
| 算子拼错 | `gtt:`（想写 `gt`）|
| 缺条款引用 | `clause: ""` |
| severity 越界 | `severity: 致命` |
| 规则 id 重复 | 两个 `E_BAD_TIME` |
| 字典取空/null | `items[*].nmae` |
| 消息占位符没定义 | `缺{labels}`（evidence 里是 `label`）|
| check 缺 record_ref | 结论无法定位到行 |

每条都带规则 id 输出，一次列全，不是改一个报一个。

## 测试套 + 变异测试：本项目最容易翻车的地方

`suite.json` 每个 fixture 支持四种断言，**`findings` 最严**（结论集合逐条相等，计数相同但换了记录也能抓到）：

```json
{"fixtures": [
  {"name": "命中面", "case": "sample_case.json",
   "expect": {"E": 9, "W": 9, "I": 1},
   "findings": [["E_BAD_TIME", "R-0004"], ["W_FREQ_SHORT", null]]},
  {"name": "假阳性为零", "case": "clean_case.json", "findings": []},
  {"name": "阈值放宽后应消失", "case": "edge_case.json",
   "params": {"day_max_minutes": 100000}, "must_not_hit": ["W_EXCESS_DAILY"]}
]}
```

**fixture 至少四类**，少一类就会漏：

1. `clean` — 全合规，`findings: []`，证明不误报
2. `sample` — 埋满问题，证明每条规则都能触发
3. `edge` — 换另一条路径触发同一规则（单条不超但求和超、频次上下限）
4. `boundary` — 判据**恰好落在阈值上**（480 分整不报、481 分报、滞后 3 天不报、容差 1 分钟不报）。没有这一份，`>` 写成 `>=` 测不出来

`mutate` 的价值：把规则自动改坏（算子反向、`and` 删一项、字典砍断、参数 ×10/÷10/归零、频次分支互换），看测试套抓不抓得住。**存活的变异 = 目前没人守的语义**。

实测：长护险 16 条规则 + 初版 3 份 fixture，存活率 25%；补 `edge`、`boundary`、`noplan` 三份后降到 0%。缺 `boundary` 时 `>` 写成 `>=` 全程无感——这是最容易骗过自己的坑。

**新加规则必须同时加 fixture，否则 `mutate` 会告诉你是哪条没人守。**

## 导出独立工具

```bash
python "$RC" export my.ruleset.json -o 长护自查工具.py
python 长护自查工具.py 机构台账.json                 # 出结论，有 E 退出码 1
python 长护自查工具.py 台账.json day_max_minutes=600  # 按当地阈值覆盖
python 长护自查工具.py --show-rules                   # 逐步打印「严重度 规则 条款」
```

生成物特性：单文件、只 import `datetime/json/sys/pathlib`、规则集以 JSON 字面量内嵌（机构能看到判定依据，不必信我们）。`parity` 实测与手写引擎在 2 份 fixture 上逐规则零差异。

## 已有示例

```bash
cd <skill_dir>/examples
# 长护险 16 条（服务记录/护理计划/人员资质/36 项目录）
python ../scripts/rulec.py test ltc/suite.json --rules ltc/ltc.rules.yaml
python ../scripts/rulec.py mutate ltc/suite.json --rules ltc/ltc.rules.yaml
# 医保两库 4 条（性别限定/儿童限定/超剂量/重复结算）——换域不动内核
python ../scripts/rulec.py build claims/claims.rules.yaml --case claims/claims_demo.json
python ../scripts/rulec.py run claims/claims.ruleset.json claims/claims_demo.json
```

`examples/ltc/` 里 5 份 fixture：`sample`（埋问题）、`clean`（零误报）、`edge`（长尾路径）、`boundary`（阈值贴边）、`noplan`（无计划不误报）。全部虚构数据。

## 坑

- **`*.ruleset.json` 是编译产物，不入库**（`.gitignore` 已排除）；用前先 `build`。
- **YAML 需 pyyaml**；只想要标准库就把规则源写成 `.json`（同结构，`rulec` 两样都认）。
- **字典文件路径相对规则源所在目录**，或用 `--data-dir` 指定。`examples/ltc/` 里四份 derived JSON 和规则源放在同一层。
- **`check` 的 `record_ref` 别省**：少了这条规则编译就过不去，因为结论不能定位的核验没法整改。
- **`fn` 遇到 `None` 返回 `None`**：`absent` 的字段不会因为算不成时长就报 E，这是故意的——否则每个字段缺失都会炸出一堆 E。
- **`overlap` 会跳过起止错乱的记录**（`end <= start`），那是 `check` 该报的，避免同一问题报两次。
- **别把"人现场判断"的条款硬编成规则**：巡查表 30 项里约一半是"制度是否建立""是否有突出贡献"，机器只能查有没有附件，不能判断内容。规则源里宁可留空，也不要写个永远返回 True 的假规则。
- **`mutate` 报存活先别急着加 fixture**：先确认那个变异是不是等价变异（比如 `filter: gt(len(plan),0)` 与内核里 `if not plan: continue` 重复，删掉才对）。

## 开源仓库

<https://github.com/hongjunt50-afk/policy-rule-compiler>（MIT）。仓库里同时有 `README.md`（给人看）与 `SKILL.md`（给 agent 看），两份都要跟着改，别只改一份。

## 自检

```bash
python <skill_dir>/tests/test_rulec.py     # 21 项：编译拒绝 9 类错、执行、变异全抓、导出独立运行、跨域
```
