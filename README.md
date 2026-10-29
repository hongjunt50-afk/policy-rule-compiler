# Policy Rule Compiler · 政策规则编译器

把带条款引用的政策/文书写成规则源，编译成**可执行、可核对、可独立分发**的核验工具。

[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

```bash
python scripts/rulec.py build  my.rules.yaml --case 样例.json   # 规则源 → ruleset.json（带静态校验）
python scripts/rulec.py test   suite.json --rules my.rules.yaml # 回归：结论集合逐条比对
python scripts/rulec.py mutate suite.json --rules my.rules.yaml # 变异测试：自动改坏规则，看套件抓不抓得住
python scripts/rulec.py export my.ruleset.json -o 自查工具.py    # 单文件工具，机构拿去就能跑
```

内核零第三方依赖（`kernel.py` 只 import `datetime/json/pathlib`）。导出物是同一份内核 + 内嵌规则 JSON 的单文件。

`SKILL.md` 是给 AI agent 看的操作说明书（本项目同时也是 Claude Code / ZCode 等 harness 可加载的 skill），人读 `README.md` 就够。

---

## 它解决什么问题

政策合规核验的难点不是"写不出判断逻辑"，是写出来之后没人敢用：

| 痛点 | 本项目的做法 |
|---|---|
| 机构问"你凭什么判我违规" | 规则源里 `clause` 是**必填项**，缺了编译不过；`--show-rules` 直接打印每条规则对应的文书条款 |
| 政策一年改几次，改完不知道哪里变了 | 规则源是可读 diff；纯 Python 实现里那是看不懂的代码 diff |
| 各地阈值口径不同，不想重写 | 阈值全部进 `params`，换地区改参数不改规则 |
| 改了规则不知道有没有写错 | `mutate` 自动枚举语义变异，报出"目前没人守的语义" |

**反面结论也要说清楚**：DSL 并不比直接写 Python 短。同一套 16 条规则，规则源 324 行 vs 手写 `check_consistency` 217 行，**长 1.4 倍**——因为条款引用、证据字段、记录定位都得显式写。所以卖点不是"少写代码"，是上面那四条。

## 快速开始

```bash
pip install pyyaml   # 规则源用 YAML 时需要；写成 .json 则纯标准库即可
git clone <this repo> && cd policy-rule-compiler

# 1) 看一眼现成的长护险规则集（16 条，5 份用例）
python scripts/rulec.py test examples/ltc/suite.json --rules examples/ltc/ltc.rules.yaml

# 2) 变异测试：自动改坏规则，确认测试套真的守得住
python scripts/rulec.py mutate examples/ltc/suite.json --rules examples/ltc/ltc.rules.yaml

# 3) 编译 + 导出一份能直接交给机构的工具
python scripts/rulec.py build examples/ltc/ltc.rules.yaml --case examples/ltc/sample_case.json
python scripts/rulec.py export examples/ltc/ltc.ruleset.json -o ltc_verify.py
python ltc_verify.py examples/ltc/sample_case.json      # 有硬违规时退出码 1

# 4) 跑自检
python tests/test_rulec.py
```

## 七条命令

| 命令 | 作用 |
|---|---|
| `init <out.yaml>` | 生成规则源骨架，每种形态带注释 |
| `build <rules.yaml> --case <样例.json>` | 编译 + 静态校验 → `<name>.ruleset.json` |
| `run <ruleset.json> <case.json> [k=v ...]` | 执行，输出结论 JSON |
| `show <ruleset.json>` | 列出规则、条款、参数、字典大小 |
| `test <suite.json> --rules <rules.yaml>` | 回归测试 |
| `mutate <suite.json> --rules <rules.yaml>` | 变异测试 |
| `export <ruleset.json> -o <tool.py>` | 导出单文件独立工具 |

`--case` 建议必传：静态检查靠它知道真实字段路径，不传就查不出字段名拼错。

## 规则源

```yaml
ruleset: ltc-service-consistency
version: 0.2.0
title: 长护险服务过程一致性核验
sources:
  - {id: doc-2026, title: 长期护理保险服务管理文书（2026 年版）, issuer: 国家医保局办公室}

params:                        # 阈值显式列出，换地区改这里
  day_max_minutes: 480
  late_entry_days: 3

dictionaries:                  # 字面量列表，或 file.json#items[category=医疗护理类].name
  catalog_names: services_catalog.json#items[*].name
  certified_qualifications: [长期照护师, 护士, 医生, 医疗护理员, 养老护理员]

rules:
  - id: E_QUAL_MISMATCH
    kind: check                # check | overlap | aggregate | frequency
    severity: E                # E 硬违规 / W 需复核 / I 数据质量
    clause: 服务记录单·护理人员资质；质量评价表·其他类第 1 项   # 必填
    scope:
      collection: records
      as: r
      bind: {cg: caregivers@r.caregiver_id}     # 关联另一张表
      filter: {present: {ref: cg}}
    when:
      and:
        - any: {over: {ref: r.items}, as: item, test: {in: [{ref: item}, {dict: medical_item_names}]}}
        - not_in: [{fn: trim, args: [{ref: cg.qualification}]}, {dict: certified_qualifications}]
    message: 资质「{qualification}」不属于持证护理人员，却执行了医疗护理类项目 {items}
    evidence: {qualification: {ref: cg.qualification}, items: {ref: r.items}}
    record_ref: {ref: r.record_no}
```

### 四种规则形态

| kind | 干什么 | 必备字段 |
|---|---|---|
| `check` | 逐行（或逐项）比字段 | `scope`、`when`、`message`、`record_ref` |
| `overlap` | 同一人/同一天时段是否重叠 | `scope.group_by`、`scope.span`、`record_ref` |
| `aggregate` | 按组求和后与阈值比 | `scope.group_by`、`scope.measure`、`when` |
| `frequency` | 计划频次 vs 月度实际 | `scope.plan`、`scope.actual`、`short`/`over` 两分支 |

`frequency` 是"计划表对照"这类形状显式建模出来的，不是通用能力。新域如果形态不在这四种里，**先加形态再写规则**，不要在 `check` 里塞 Python 逻辑——那样又把"可核对"丢了。

### 算子与函数（全部可枚举，写错编译期就死）

- 比较：`eq ne lt le gt ge in not_in`
- 逻辑：`and or not any none present absent`
- 取值：`ref`（`r.field` / `cg.field` / `d.派生名` / `group.名` / `total` / `count` / `other`）、`const`、`param`、`dict`、`len`、`select`、`ref_field`（动态取字段）
- 函数：`minutes_between abs sub add date_diff_days month trim head size`

`fn` 任一参数取不到值时整体返回 `None`，不报违规——缺字段要报就用 `present`/`absent` 明说，别拿计算函数兜。

## 编译期拦得住什么

| 错误 | 例子 |
|---|---|
| ref 字段名拼错 | `r.duration_minutes`（真名是 `duration_min`）|
| ref 指向没声明的变量 | `cgx.qualification` |
| 算子拼错 | `gtt:`（想写 `gt`）|
| 缺条款引用 | `clause: ""` |
| severity 越界 | `severity: 致命` |
| 规则 id 重复 | 两个 `E_BAD_TIME` |
| 字典取空 / 取到 null | `items[*].nmae` |
| 消息占位符没定义 | `缺{labels}`（evidence 里是 `label`）|
| `check` 缺 `record_ref` | 结论无法定位到行 |

一次列全并带规则 id，不是改一个报一个。

## 测试套与变异测试

这是本项目最该看的部分。`suite.json` 每个 fixture 支持四种断言，`findings` 最严（结论集合逐条相等，计数相同但换了记录也能抓到）：

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

**fixture 至少四类，少一类就会漏：**

1. `clean` — 全合规，`findings: []`，证明不误报
2. `sample` — 埋满问题，证明每条规则都能触发
3. `edge` — 换另一条路径触发同一规则（单条不超但求和超、频次上下限）
4. `boundary` — 判据**恰好落在阈值上**（480 分整不报、481 分报、滞后 3 天整不报、容差 1 分钟不报）

`mutate` 枚举的变异：比较算子反向、`and` 删项或改 `or`、`present↔absent`、字典砍到 1 项或清空、数值参数 ×10/÷10/归零、双分支互换。

**实测曲线**：长护险 16 条规则 + 初版 3 份 fixture，57 个变异里只抓住 43 个（**存活率 25%**），其中"单日累计求和改成取最大值"和"多处 `>` 写成 `>=`"全部漏检——因为原 fixture 只造了超阈值的单条记录，求和与取最大值结果相同。补 `boundary` 与 `noplan` 两份后 **57 个全抓、存活 0**。

> **教训**：手挑几处变异通过，不等于测试有效。缺"阈值贴边"那份 fixture，比较算子写错就测不出来。

## 导出独立工具

```bash
python scripts/rulec.py export my.ruleset.json -o 自查工具.py
python 自查工具.py 台账.json                    # 出结论，有 E 退出码 1
python 自查工具.py 台账.json day_max_minutes=600 # 按当地阈值覆盖
python 自查工具.py --show-rules                  # 打印「严重度 规则 条款」与参数
```

生成物特性：单文件、只 import `datetime/json/sys/pathlib`、规则集以 JSON 字面量内嵌——机构拿到脚本就能看到判定依据，不需要相信我们。

## 示例

`examples/ltc/` — 长护险服务过程一致性核验，16 条规则（服务记录 / 护理计划 / 人员资质 / 36 项服务目录），规则由官方 PDF 抽取并在抽取时自校验。5 份用例全部为**虚构数据**：

| 文件 | 用途 |
|---|---|
| `sample_case.json` | 埋多类问题，9E / 9W / 1I |
| `clean_case.json` | 完全合规，假阳性必须为零 |
| `edge_case.json` | 长尾路径：求和超阈值、频次超计划、计划外项目 |
| `boundary_case.json` | 判据恰好落在阈值上 |
| `noplan_case.json` | 未建护理计划时不该误报 |

`examples/claims/` — 医保基金智能审核"两库"规则 4 条（药品区分性别使用 / 限儿童使用 / 超剂量 / 重复结算）。**换域不碰内核**：4 条规则 82 行 YAML，内核一行未改。

## 已知边界

- **机器可判面远小于文书字数给人的印象。** 长护险巡查记录表 30 项里，约 12 项能靠系统/台账核查，约 16 项是"制度是否建立""是否有突出贡献"这类**存在性判断**（缺的是证据模型，不是表达式），2 项需现场主观判断。经验值：一个文书包里约三成句子是机器可判的，其余是流程、制度、现场判断。**别对外宣称"导入文书就出工具"，否则那七成会变成沉默漏判。**
- **不做 LLM 自动抽取。** 稽核结论要能进报告，LLM 起草的规则必须人逐条签署；人签 16 条的成本低于调试 LLM 输出的成本。规则源的输入始终是人写的。
- **不写"永远返回 True"的假规则。** 现场判断类条款宁可留空，也不能假装机器判了。
- **`overlap` 跳过起止错乱的记录**（`end <= start`），交给 `check` 报，避免同一问题报两次。
- 相关但不同的事：Rules as Code 是成熟赛道（[OpenFisca](https://openfisca.org/)、[Catala](https://github.com/Catala/catala)、DMN / GoRules JDM）。本项目不宣称首创，只解决"规则结论要能指回条款、要能版本对照、要能按地区参数化"这三件事。

## 自检

```bash
python tests/test_rulec.py     # 21 项：编译拒绝 9 类错、执行、变异全抓、导出独立运行、跨域
```

## 许可

MIT，见 [LICENSE](LICENSE)。
