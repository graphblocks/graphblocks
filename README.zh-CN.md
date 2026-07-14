# GraphBlocks

[English](README.md) | [한국어](README.ko.md) | [简体中文](README.zh-CN.md)

> 不要重复造轮子。

GraphBlocks 是一套供应商中立的契约工具包，用于构建可移植、可测试且可治理的 AI
应用。它定义了类型化图、运行时行为、应用协议、策略与预算边界、软件包元数据以及符合性
配置文件，且不要求使用特定的模型供应商、数据库、解析器、服务器框架或部署平台。

本项目正在准备首个 1.0 候选版本。兼容性声明以符合性配置文件和可执行证据为依据，而不是
仅凭软件包或目录的存在；更高级别的配置文件和原生绑定仍处于预览阶段。

## 包含内容

- 纯 Python 的 `graphblocks` SDK，包括编写与验证功能、内置块、参考运行时、CLI
  以及与框架无关的服务器契约。
- 可选的原生 `graphblocks-runtime` Python 扩展。
- `graphblocks-testing` 发行包和共享 TCK 测试夹具。
- Rust schema、编译器、协议和运行时 crate。
- 版本化 schema 和供应商中立的软件包目录。
- 共享 TCK 测试夹具和可执行的验收应用。

## 开发快速入门

需要 Python 3.11 或更高版本，以及由 `rust-toolchain.toml` 指定的 Rust 工具链。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m graphblocks validate examples/01-enterprise-federated-rag/example.yaml
python examples/01-enterprise-federated-rag/run.py
python -m pytest
cargo test --workspace --all-targets
```

激活虚拟环境后，在仓库根目录进行的可编辑安装会提供 `graphblocks` 导入包、
`graphblocks` 命令以及 `python -m graphblocks`。内置块实现、CLI 和服务器契约都属于
该发行包；它们并不是独立的功能 wheel。extras 会添加实际的安装依赖：`runtime` 添加
原生绑定，`pdf` 添加 `pypdf`，`test` 添加 pytest。若要使用 `graphblocks-tck` 命令，
请安装 `graphblocks-testing`。

机器可读的软件包目录会区分发布产物与可移植组件标识和绑定标识。组件条目并不对应单独
发布的 Python wheel。Python 的发布范围包括 `graphblocks`、`graphblocks-runtime` 和
`graphblocks-testing`。

该仓库还会构建 `graphblocks-native`，这是一个不依赖 Python 的 Rust 可执行文件，用于
执行 `validate`、`plan` 和 `run`。它通过标准输入接收 JSON 或 YAML，可以从多文档 YAML
流中选择具名 `Graph`，并执行原生标准库块集。`graphblocksd` 是工作进程控制平面命令，
目前还不是监听 HTTP 的服务器进程。

## 文档

- [文档导览](docs/README.md)
- [安装](docs/getting-started/installation.md)
- [快速入门](docs/getting-started/quickstart.md)
- [架构](docs/concepts/architecture.md)
- [持续演进的规范](docs/specification/README.md)
- [符合性](docs/development/conformance.md)
- [实现状态](docs/project/status.md)
- [示例](examples/README.md)

## 项目与社区

GraphBlocks 采用 [Apache License 2.0](LICENSE) 许可。欢迎贡献；详情请参阅
[CONTRIBUTING.md](CONTRIBUTING.md)、[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)、
[SECURITY.md](SECURITY.md) 和 [GOVERNANCE.md](GOVERNANCE.md)。
