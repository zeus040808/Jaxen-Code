# SWE-bench 评测环境泄露：发现与修复（2026-11-02）

## 结论

2026-11-02 之前的三轮 SWE-bench Verified 结果（easy20 20/20、medium10 10/10、hard10 8/10），以及 SWE-rebench
试点的前两题，**都是在有泄露的环境里跑的，不能作为能力证据**。agent 在不少题里查到了"未来的"官方修复并照抄。
已堵住两个泄露口，隔离后的复测见下文；三轮已在隔离环境里重跑，结果是 32/40，见 [隔离重跑](2026-11-02-swebench-isolated.md)。

## 泄露口与证据

| 泄露口 | 证据 |
|---|---|
| 镜像里的 git 历史包含"未来"的提交 | Epoch 的 django-13212 镜像有 423 个 git 引用，其中包括这道题自己的官方修复 `83fbaa9231 Fixed #31806 -- Made validators include the value in ValidationErrors`；agent 用 `git log --all --grep` 搜到了它。本地构建的 SWE-rebench 镜像是完整克隆，同样有这个问题 |
| 容器可以联网 | pypa/build-1027：agent 下载了上游 main 分支的 `_util.py` 和 PR #1027 的 diff，最终补丁与官方补丁的 git blob 哈希完全相同；pylint-4551 用 curl 拿了 v2.9.0 的文件；django-11138/11265/12325/15280 用 `pip download` 下载了修复后的 Django 版本 |

trace 中出现 `git log --all`、`pip download`、`curl`/`urllib` 访问外网等命令的题数：easy20 5/20、medium10 5/10、hard10 8/10。
出现这类命令不等于一定抄到了答案，逐题判定需要更细的分析。

此前把"补丁逐字复现官方新增行"归因于"模型训练时见过（数据污染）"，**归因不完整**：至少一部分来自上述泄露。
模型本身是否见过这些题，要在隔离环境里重跑后再看。

## 修复

只改评测环境，没有改 agent。

| 泄露口 | 做法 | 位置 |
|---|---|---|
| git 历史 | agent 开始前把 `/testbed` 的 `.git` 删除重建，只剩当前代码这一个提交（镜像自带的工作区改动也在这个提交里，之后的 diff 只含 agent 的改动） | `scripts/swebench/run_hh_predictions.py::reset_history` |
| 联网 | agent 容器接入 `--internal` 的 Docker 网络（没有通往外网的路由）；唯一出口是 `egress_proxy.py`，只接受白名单里的 CONNECT 目标（DeepSeek API）。模型客户端用 urllib，读 `HTTPS_PROXY`，agent 代码不用改；agent 的 shell 命令继承同样的设置。每题记录被拒绝的请求数 `egress_denied` | `scripts/swebench/egress_proxy.py`、`run_hh_predictions.py::start_egress` |

只在提示词里写"不许联网"不可靠，模型照样会去查；去掉代理环境变量直连也不行，因为内网没有 DNS 和外网路由。

## 验证

在 django-13212 镜像里逐项检查：

| 检查 | 结果 |
|---|---|
| 官方修复提交 `83fbaa9231` 是否还在 | 不在 |
| 经代理访问 DeepSeek API | 能连上（无密钥返回 401） |
| 经代理访问 raw.githubusercontent.com | 被拒绝（403） |
| `pip download django==4.2` | 失败，代理日志记录了被拒绝的 pypi.org |
| 去掉代理变量直连 pypi.org | 失败（无法解析域名） |

端到端复测 pypa/build-1027（SWE-rebench，2026-04）：

| | 泄露环境 | 隔离环境 |
|---|---|---|
| 结果 | resolved | **resolved** |
| 请求 / 耗时 | 14 次 / 39 秒 | 30 次 / 450 秒 |
| 与官方新增行逐字相同的比例 | 90%（git 哈希相同） | 15%（独立实现，思路相同：按 PEP 610 检查安装来源） |
| 被拒绝的上网请求 | — | 0（agent 仍尝试了 `git log --all`，查不到任何东西） |

回归测试：`tests/test_eval_isolation.py`（代理的白名单解析、拒绝与放行路径，不需要联网）。
