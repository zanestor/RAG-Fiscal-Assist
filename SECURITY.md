# Security and Privacy

## Public repository boundary

This repository contains implementation code and configuration templates only.
Do not commit API keys, `.env`, downloaded source documents, extracted text,
SQLite databases, `state.json`, chat history, generated reports, or internal
source paths.

Private source locations must be configured locally through environment
variables such as `FISCAL_RAG_GPA_PATH`. The public repository is not a copy of
the document corpus.

## Live deployment boundary

The included server is designed for trusted local use and binds to
`127.0.0.1`. Do not expose it directly to the public internet. A public
deployment must add authentication, HTTPS/TLS, rate limiting, access logging,
and an approved data-retention policy before external users are given access.

## Reporting a vulnerability

Do not open a public issue for secrets, personal data, or exploitable security
problems. Contact the repository maintainer privately with a description,
impact, reproduction steps, and affected commit or version.
