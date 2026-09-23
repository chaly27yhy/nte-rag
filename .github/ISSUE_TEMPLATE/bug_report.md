---
name: 缺陷报告
about: 报告一个可复现的缺陷（不是安全问题；安全问题请见 SECURITY.md）
title: '[缺陷] '
labels: ['bug']
assignees: ''
---

<!--
提交前请先确认：
1. 你用的是最新发行版，或用最新代码 `.venv\Scripts\python.exe run.py` 复现；
2. 已经跑过 `.venv\Scripts\python.exe run.py --selftest`，并把结果贴在下面对应位置；
3. 正文与截图里【没有】API Key 或个人文件路径。
-->

## 发生了什么

<!-- 简述现象，再补关键细节；有报错就贴原文。 -->

## 你期望的结果

<!-- 你原本以为会发生什么。 -->

## 复现步骤

1.
2.
3.

<!-- 是否稳定复现？每次 / 偶尔 / 只出现过一次 -->

## 版本与运行环境

- 程序版本（窗口标题栏，或 `.venv\Scripts\python.exe run.py --version` 的输出）：
- Windows 版本（例如 Windows 11 23H2 x64）：
- 运行方式：单文件 exe / 解压后的 onedir / 源码运行（`.venv\Scripts\python.exe run.py`）
- 是否已安装 WebView2 运行时？Windows 11 通常自带；打不开窗口多半是缺它
- 便携模式？（exe 同目录有 `portable.flag`）
- 数据目录：`%APPDATA%\NTE-RAG` 还是自定义的 `NTE_RAG_DATA_DIR=`：

## 自检结果

<!--
请运行 `.venv\Scripts\python.exe run.py --selftest`（源码方式）或 `NTE-RAG.exe --selftest`（发行版），
把结尾几行贴进来；它会在数据目录下写一份 JSON 报告，需要的话也一并附上。
-->

```
（粘贴 --selftest 的输出）
```

## 日志

<!--
请贴 `%APPDATA%\NTE-RAG\logs\app.log` 的【末尾几十行】（便携模式下在 `data\logs\app.log`）。
日志里的密钥会被 app/core/secrets.py 自动脱敏，可以放心贴。
-->

```
（粘贴日志末尾）
```

## 补充说明

<!-- 已经排除的可能、改过的配置、同时装了哪些会拦网络的软件等。 -->

---

- 不需要提供 API Key。日志里的密钥形状会被自动脱敏，贴日志不会泄露 Key。
- 截图注意：设置页和请求日志上可能显示 Key，带 Key 的截图不要上传。
  界面只显示掩码，但你自己拼过的配置或第三方工具可能会显示完整值。
- 如果是安全问题，比如能读到本不该读的数据、能绕过鉴权、能远程触发行为等，
  请不要用公开 issue，按 [SECURITY.md](../../SECURITY.md) 的私下渠道上报。
