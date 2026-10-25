# SOFI × Pdots 文献周报

本扩展基于 X-PG13/paper-digest，保留原作者、MIT LICENSE、原 daily CLI 及 OpenAI API 模块。
基线 commit：`8906f9a12309956913eab29dade75c01cb7d0771`。
开发分支：`feat/sofi-pdots-weekly`。第一版采用独立 weekly 编排，复用上游 Paper、标识规范化、Crossref/PubMed/OpenAlex/arXiv 解析器、网络重试、EmailConfig 和 SMTP 凭证处理；没有拼接其他项目。

## 本机快速使用

项目：`~/projects/sofi-pdots-digest`。
系统 Python 3.9 不适用；已用本机 Conda Python 3.14.8 创建 `.venv`。

```sh
cd ~/projects/sofi-pdots-digest
.venv/bin/python -m paper_digest.weekly --help
.venv/bin/python -m paper_digest.weekly doctor
.venv/bin/python -m paper_digest.weekly run
.venv/bin/python -m paper_digest.weekly status
```

本机采用正常 wheel 安装，避免 Python 3.14 忽略被环境标成隐藏的 editable `.pth` 文件。修改代码后在项目根目录运行时直接使用源码；如需从其他目录使用安装版，重新运行上述 pip install。

`run` 检索、分析、生成预览，**不发送**。`fetch` 只检索。`send` 发送上次完成的周报。
也可用 `paper-digest weekly ...`（同一参数）。迁移安装：Python 3.12+，`python -m venv .venv`，`.venv/bin/python -m pip install '.[weekly]'`。

```sh
# 首次复制一次；不要覆盖已有配置
cp weekly.example.toml weekly.local.toml
# 复用已归档的真实检索结果，摘要缓存自动复用
.venv/bin/python -m paper_digest.weekly run --reuse /绝对路径/检索目录
# 发送预览；首次部署测试用 send --test；仅在 SMTP 接受之后标记论文 sent
.venv/bin/python -m paper_digest.weekly send
# 指定归档
.venv/bin/python -m paper_digest.weekly send --archive /绝对路径/周报目录
```

## QQ 邮箱配置

在 QQ 邮箱网页设置中开启 SMTP，生成授权码。授权码不等于 QQ 登录密码。
双击 `scripts/配置QQ邮箱.command`，或运行：

```sh
.venv/bin/python -m paper_digest.weekly setup-qq
```

脚本使用 `smtp.qq.com:465`、SSL，交互输入完整发件邮箱及授权码（不回显）。
凭据保存为 `~/.config/sofi-pdots/smtp.json`，目录 700、文件 600，位于仓库之外。
它是本地受限权限的凭据文件，运行时载入环境变量，不是加密保险箱；需要更高隔离时可自行通过系统密钥管理器注入相同变量。
程序不打印凭据；不要把凭据文件、授权码或环境转储提交 Git。

也支持通过环境变量提供 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USERNAME`、`SMTP_PASSWORD`、`SMTP_FROM`、`SMTP_SECURITY`（`ssl` 或 `starttls`）。环境变量优先于本地文件。
非 QQ 邮箱须自行提供官方 SMTP 配置，程序不会根据收件地址猜测。
公开示例占位收件人是 **reader@example.com**，可在 TOML 改动。

`doctor` 检查 Codex 登录状态和 SMTP 配置齐全性；它不代表 SMTP 登录或收件箱投递已经验证。
正式 `send` 的成功只代表 SMTP 接受。收件箱、垃圾箱和退信仍须查看。

## 关键词与研究画像

编辑 `weekly.local.toml`：

- `[search].sofi`：A 通道，含完整英文名、balanced SOFI、SOFI＋光学上下文。
- `[search].pdots`：B 通道，聚合物点及半导体／共轭聚合物纳米粒子＋荧光上下文。
- `[search].methods` 和 `qualifiers`：C 通道，显微成像方法与伪影／置信度／物理约束等组合。
- `extra_priority_terms`：给规则排序增加方法关键词，不等于强制纳入。
- `min_score`：排序阈值；`max_papers` 默认 8，最多 10，可少于 5。
- `[analysis].max_candidates`：每轮最多相关性判断 12 篇；只有筛选通过者生成详细摘要。
- `max_fulltexts` 默认 3；通过 Europe PMC 官方 OA XML 尝试全文，不绕付费墙。

研究画像写在 `paper_digest/weekly/provider.py` 的 `PROFILE`：200–500 帧 TIFF、约1000帧可能性、MATLAB、2–4阶及高阶、少硬件改动、偏算法与统计。修改这里可改变模型的研究偏好。
评分综合规则与模型价值判断；不是论文真实度概率。没有出版社白名单，不要求同时包含 SOFI、Pdots 和超分辨。金融同名、碳点／碳化聚合物点不因名称相似直接当 Pdots；background correction 不排除。新成果优先进入有限的判断队列，近期高价值补充仍保留。

## 检索与日期

默认首次 90 天，后续每个来源使用其上次完整成功水位减去30天的重叠窗口；仍保留90天内已发现但未推送候选。
每次分别保存：发表日期和 `publication_precision`、索引／入库日期、首次发现时间。来源未给索引日期时留空，绝不用发现时间填充。
PubMed 使用 `crdt` 发现新入库文献。PubMed `ArticleDate`／期刊 `PubDate` 用于发表时间；Crossref published-online／published-print／published／issued 用于真实发表时间。**上游原始 Paper 的 `published_at` 对这两源仍保持 Entered/Indexed 兼容语义；weekly 适配器不会把它直接显示为发表日期。**
只有年／月时保留精度；不能确定处于时间窗口内的条目保留在候选归档，不冒充本周新发表。
DOI（大小写／URL规范化）、PMID、去版本后 arXiv ID 构成别名集合，跨源桥接合并；不做自动标题模糊合并。预印本和正式论文通过显式 relation 关联，保留各自元数据；正式版本如关联已推送预印本则归为版本更新。

各源独立构造查询，原始响应与不含 key 的请求参数保存在 `evidence/`：

- Europe PMC：三个布尔通道、FIRST_PDATE / FIRST_IDATE、cursorMark。覆盖其收录的生物医学及预印本，并不等于全部 bioRxiv。
- PubMed：Title/Abstract 语法、crdt、esearch/efetch 分页。
- Crossref：逐概念 query.bibliographic 词法搜索，不发送布尔式；cursor 分页、索引日期窗口，最终按真实发表时间过滤。
- OpenAlex：独立 search/filter；可选 `OPENALEX_API_KEY`。无 key 时只取基础查询允许的第一页，有后续结果则明确标 partial。程序不购买额度、不申请付费计划；使用自行配置的 key 时请在服务端关闭额外计费。
- arXiv：all 字段布尔式、submittedDate，至少 3.1 秒请求间隔，start 分页。
- bioRxiv：官方日期区间 API，30条一页，所有类别本地筛选。90天结果较多时会受配置分页上限限制；不会假装完整。
- Semantic Scholar：可选 `sources` 加 `semantic_scholar`，词法查询、独立 offset。可选 `SEMANTIC_SCHOLAR_API_KEY`。

可选 NCBI key 为 `NCBI_API_KEY`；无 key 限速请求。默认不启用收费服务。
所有源有 timeout、有限重试、max_pages、max_requests_per_source；某源故障会继续其他源，故障查询与后续跳过情况记录在 sources.json。
只有所有配置检索正常且无分析失败、确无合格成果时，才允许“本周未发现重要新成果”。本系统不承诺全网覆盖、不漏检。

## Codex CLI 与证据边界

已检查本机 `codex --version`、`codex exec --help`、`codex login status`。
使用 `exec --sandbox read-only --output-schema ... --output-last-message ... -`，参数数组＋stdin，不使用 shell 插值，也不解析事件 JSONL 为摘要。
复用 ChatGPT 登录，不把订阅转换成 API key。子进程移除 OPENAI_API_KEY/CODEX_API_KEY 与 SMTP 凭据变量；忽略用户工具／MCP配置，仅继承本机配置中的 model 名称，可在 TOML 显式指定 model。不启用额外付费 API 回退。
模型工作目录为临时目录，禁用 shell 工具和网页搜索；提示词明确论文证据不是指令。JSON Schema 验证最终结果，论文报告的实验数值和作者局限必须带可在输入中找到的逐字证据；新增数字不在证据中则拒绝。
摘要输入长度、超时、重试数量有限。全文超长则回退摘要，并且不会标“已读全文”。
代码／数据链接仅从已取得证据提取并实际请求核查。HTTP可访问不等于代码可复现、数据完整。未找到则写“未找到公开链接”，不声称没有代码。当前自动链接发现限常见公开仓库（GitHub/GitLab/Zenodo/Figshare/OSF/Dryad）；其他地址可能漏查。
模型判断仍需科研人员复核；逐字引文检查不是对解释正确性的数学证明。全文覆盖、非数值结论及方法适用性尤其应人工审阅。

## 状态、恢复与防重复

`weekly-output/state.sqlite3`：papers 保存 fetched / summarized_at / sent_at；别名跨轮去重；cache 保存筛选和摘要；source_watermarks 保存逐源水位；outbox 保存 Message-ID 和投递阶段。
并发进程用文件锁串行化，SQLite 事务保存状态。预览永不标记 sent。
已明确拒收／登录失败为 retryable；DATA阶段断线为 uncertain；进程中断留下 sending，下次启动转 uncertain。遇到 uncertain 自动任务停止重发。
人工核对发件服务结果后：

```sh
.venv/bin/python -m paper_digest.weekly status
.venv/bin/python -m paper_digest.weekly reconcile --message-id '<原消息ID>' --outcome accepted
# 或仅在确认未接受时：
.venv/bin/python -m paper_digest.weekly reconcile --message-id '<原消息ID>' --outcome not-sent
.venv/bin/python -m paper_digest.weekly send --archive /原周报目录
```

明确成功后持久化论文 sent 与 outbox accepted，QUIT 失败不会撤销接受状态。相同日期、收件人和论文集合不会重复发送。不要删除数据库后重发既有归档。
本地备份建议同时保留整个 weekly-output 与配置；数据库用 SQLite backup API 或停任务后拷贝，不在写入中直接复制单个 sqlite 文件。

## 每周调度（本机仅 launchd）

默认 Asia/Shanghai 周五09:00。需成功发送一封测试周报后才可启用：

```sh
.venv/bin/python -m paper_digest.weekly enable
.venv/bin/python -m paper_digest.weekly pause
.venv/bin/python -m paper_digest.weekly resume
.venv/bin/python -m paper_digest.weekly remove
# 立即生成并按到期规则发送（常规手动预览用 run）
.venv/bin/python -m paper_digest.weekly tick
```

修改 `[schedule]` 的 weekday（周一0、周五4）、hour、minute 或 timezone，再运行 enable 重新加载。
任务：`~/Library/LaunchAgents/org.local.sofi-pdots.weekly.plist`。每周定时触发，另有每小时轻量检查和登录补跑；程序内部按指定时区判断是否到期。休眠／关机／断网时无法保证准时，恢复在线并登录后补跑；失败重试间隔至少6小时。
`weekly-output/scheduler.log` / `scheduler-error.log` 保存运行日志。
暂停保留 plist；remove 卸载并删除 plist，代码与归档不会删除。

## 验证

```sh
.venv/bin/python -m unittest tests.test_weekly -v
.venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check paper_digest/weekly tests/test_weekly.py --select F
```

测试的网络故障、SMTP成功／失败均为隔离模拟，不能代替真实发信。真实检索、CLI摘要、邮件接受和调度状态见本次交付记录与本机 outbox。

## 核查过的官方资料（2026-10-25）

- Codex 非交互输出：[官方说明](https://learn.chatgpt.com/docs/non-interactive-mode)，并以本机 help 为准。
- [PubMed 帮助：Create Date](https://pubmed.ncbi.nlm.nih.gov/help/)。
- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)。
- [OpenAlex 当前认证](https://help.openalex.org/api/authentication/)：其2026年早期博客与当前文档关于无key查询有变化，以当前认证页及真实响应为准。
- [arXiv API](https://info.arxiv.org/help/api/user-manual.html)。
- [bioRxiv API](https://api.biorxiv.org/) 日期区间和游标。
- [Europe PMC REST](https://europepmc.org/RestfulWebService)：浏览器抓取受限，官方REST端点真实请求已验证。

## 运行监督与维护

launchd通过监督器启动任务，单轮默认最多90分钟；超时会结束子进程组并记录失败，下轮按重试间隔恢复。每天用SQLite一致性备份保留最近7个备份文件。`weekly-output/health.json`提供最近开始、结束、退出码、是否逾期、SMTP不确定投递数量；`worker.log`提供任务日志。失败与延期不会被标为完成。全源故障且无可用结果时不发送空邮件来冒充本周完成。

定期维护监测是额外的只读检查，不是第二个发信任务。它检查launchd、健康记录、来源故障、登录失败及CI，必要时提醒用户；SMTP结果不确定时必须人工核查，不自动重发。电脑关机、断网、服务故障或登录/额度失效时仍无法保证准点完成。

公开仓库的SOFI weekly checks只做无凭据的单元测试与lint。继承的上游维护/发布/日报工作流仅允许在上游仓库执行。不要把Codex登录文件或SMTP凭据上传GitHub，也不要在公开仓库启用可访问本机凭据的自托管runner。
